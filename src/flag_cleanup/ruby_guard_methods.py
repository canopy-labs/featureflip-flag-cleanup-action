"""Delete a Rails callback guard that this run's fold emptied.

A one-purpose ``before_action`` guard gated on a flag folds to a method whose
whole body is a bare ``return``, or to nothing:

    before_action :ensure_pages_enabled

    def ensure_pages_enabled
      return if Feature.active?(:custom_pages, current_user)   # served on
      render_response(false, message: "You do not have access.")
    end

Three things are done, each only for a method that had a body before the run:

* A bare ``return`` with no work left is dropped, in any ``def`` or
  ``def self.`` (parameters or not): when it is the body's only statement
  (both return ``nil``, so nothing can tell them apart), and when it is the
  last statement right after a modifier ``return if c`` / ``return unless c``
  (that statement is ``nil`` when its ``return`` does not run). A trailing
  bare ``return`` after any other statement is never touched: dropping it
  would change which value the method returns.
* An empty method named by a ``before_action`` is deleted together with every
  such callback, when the name is spelled nowhere else in the checkout.
* An emptied method that stays is reported by name: a caller, a callback
  registered from another file, a route or a view may still reach it.

The second one is deliberately narrow, because a Rails method is reached by
name from places no parse of one file sees:

* the callback must be a plain statement of the SAME class body as the method
  (``before_action :name``, with or without ``only:`` / ``except:`` / ``if:``
  options, and no other symbol, no block, no receiver): a callback named by a
  parent class or a concern is that file's business;
* the method takes no parameters, is a plain ``def`` directly in the class
  body, and has no comment inside it or directly above it, or above any of its
  callbacks (the comment would be left describing nothing, or deleted without
  anyone having decided that);
* the name is spelled nowhere else: not in the file (a ``skip_before_action``,
  a ``send(:name)``, a ``respond_to?``, a direct call, a string or a comment),
  and not in any Ruby or ERB file of the checkout (a subclass's
  ``skip_before_action :name`` raises ``ArgumentError`` once the callback is
  gone). A method named by no callback is never removed: it may be an action
  or a hook the framework calls by name.

Differential: a method that was already empty (or a bare ``return``) before
the run is the customer's own, and stays.

Placement: Action. Which methods the run emptied and the repository-wide
absence proof are R1/R3 (policy and facts only the Action has); the edit is a
line deletion this pass writes itself, because the engine has no primitive that
deletes a method together with its sibling callbacks (R4, R6): split it onto the
engine once such a primitive exists.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import file_deletion, git_ops, source_index
from flag_cleanup.unused import _parse, _walk

#: The binding kind of each row: a deleted method, reported with the others.
KIND = "guard method"
#: The kind of a method the run emptied and this pass kept: reported, never
#: edited further (a caller, a callback in another file, a route or a view may
#: still name it).
EMPTIED_KIND = "emptied method"

_CALLBACKS = frozenset(
    {
        "before_action",
        "before_filter",
        "prepend_before_action",
        "prepend_before_filter",
        "append_before_action",
        "append_before_filter",
    }
)
_SUFFIXES = {".rb", ".rake", ".erb"}


def remove(
    summaries: list, repo_dir: str, rewrite: Callable
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """Empty and delete the guard methods the run's fold left behind in the
    Ruby files it changed. Returns the summaries and a
    ``(path, "guard method", name)`` row for each deleted method, and a
    ``(path, "emptied method", label)`` row for each emptied one kept."""
    candidates = [
        s
        for s in summaries
        if s.path.endswith(".rb") and not file_deletion.deleted(s) and s.original_content != s.content
    ]
    if not candidates:
        return summaries, ()
    spelled = _Checkout(repo_dir)
    out: list = []
    rows: list[tuple[str, str, str]] = []
    changed = {s.path for s in candidates}
    for summary in summaries:
        if summary.path not in changed:
            out.append(summary)
            continue
        on_disk = Path(summary.path).read_bytes()
        if on_disk != summary.content.encode("utf-8"):
            out.append(summary)
            continue
        text, names = _rewrite(summary.original_content, summary.content, summary.path, spelled)
        relative = os.path.relpath(summary.path, repo_dir)
        rows.extend((relative, EMPTIED_KIND, label) for label in emptied(summary.original_content, text))
        if text == summary.content:
            out.append(summary)
            continue
        git_ops.before_writing([summary.path])
        Path(summary.path).write_bytes(text.encode("utf-8"))
        out.append(rewrite(summary.path, summary.original_content, text))
        rows.extend((relative, KIND, name) for name in names)
    return out, tuple(rows)


class _Checkout:
    """Every Ruby and ERB file of the checkout, read once per call."""

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir
        self._texts: dict[str, str] | None = None

    def texts(self) -> dict[str, str]:
        if self._texts is None:
            suffixes = _SUFFIXES | set(source_index.RUBY_SUFFIXES) | set(source_index.NO_SUFFIX)
            found: dict[str, str] = {}
            for f in source_index.files(self.repo_dir, suffixes, with_ignored=True):
                try:
                    found[f.resolved] = f.path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    found[f.resolved] = ""
            self._texts = found
        return self._texts

    def spells_outside(self, name: str, path: str) -> bool:
        """Whether any file but ``path`` spells ``name``."""
        word = _word(name)
        resolved = str(Path(path).resolve())
        return any(word.search(text) for other, text in self.texts().items() if other != resolved)


def _word(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w$@:])" + re.escape(name) + r"(?![\w?!=])|:" + re.escape(name) + r"(?![\w?!=])")


def _rewrite(before: str, after: str, path: str, checkout: _Checkout) -> tuple[str, tuple[str, ...]]:
    """``after`` with its emptied guards deleted, and the names deleted."""
    root = _parse(after, "ruby")
    if root.has_error:
        return after, ()
    had_body = _bodied_methods(_parse(before, "ruby"))
    data = after.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    names: list[str] = []
    deleted: set[int] = set()
    for container in _walk(root):
        if container.type not in ("class", "module"):
            continue
        body = container.child_by_field_name("body")
        if body is None:
            continue
        scope = _scope(container)
        deleted_here: set[int] = set()
        for method in body.named_children:
            if method.type != "method":
                continue
            name = _text(method.child_by_field_name("name"))
            if (scope, name) not in had_body or method.child_by_field_name("parameters") is not None:
                continue
            kind = _body_kind(method)
            if kind is None:
                continue
            callbacks = _callbacks(body, name)
            plan = (
                _deletion(data, method, callbacks)
                if callbacks is not None and callbacks and _non_public(body, method, name)
                else None
            )
            if plan is not None and _only_mention(after, method, callbacks, name) and not checkout.spells_outside(name, path):
                edits.extend(plan)
                names.append(name)
                deleted_here.add(method.id)
        if deleted_here:
            edits.extend(_dangling_modifiers(data, body, deleted_here))
            deleted.update(deleted_here)
    drops = _returns(_parse(before, "ruby"), root, data, deleted)
    if not edits and not drops:
        return after, ()
    edits = sorted([*_tidy(data, edits), *drops])
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:])):
        return after, ()
    for start, end, text in reversed(edits):
        data = data[:start] + text + data[end:]
    result = data.decode("utf-8")
    return (result, tuple(names)) if not _parse(result, "ruby").has_error else (after, ())


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _scope(container: Node) -> tuple[str, ...]:
    """The class and module names around (and including) ``container``."""
    names: list[str] = []
    current: Node | None = container
    while current is not None:
        if current.type in ("class", "module"):
            names.append(_text(current.child_by_field_name("name")))
        current = current.parent
    return tuple(reversed(names))


def _bodied_methods(root: Node) -> set[tuple[tuple[str, ...], str]]:
    """The ``(scope, name)`` of each method of the file that had a body."""
    found: set[tuple[tuple[str, ...], str]] = set()
    for container in _walk(root):
        if container.type not in ("class", "module"):
            continue
        body = container.child_by_field_name("body")
        for method in body.named_children if body is not None else ():
            if method.type == "method" and _body_kind(method) is None:
                found.add((_scope(container), _text(method.child_by_field_name("name"))))
    return found


def _body_kind(method: Node) -> str | None:
    """``"empty"`` for a method with no statement, ``"return"`` for one whose
    only statement is a bare ``return``, None for anything else (a comment
    anywhere inside counts as something else)."""
    if any(n.type == "comment" for n in _walk(method)):
        return None
    body = method.child_by_field_name("body")
    if body is None:
        return "empty"
    statements = body.named_children
    if len(statements) == 1 and statements[0].type == "return" and not statements[0].named_children:
        return "return"
    return None


#: Options a callback may carry. ``if``/``unless`` only as a plain symbol: a
#: lambda, proc or block may do more than decide, and a string is eval'd on
#: Rails before 5.2.
_OPTIONS = frozenset({"only", "except", "if", "unless", "prepend"})
_CONDITIONS = frozenset({"if", "unless"})
_MODIFIERS = frozenset({"private", "protected"})


def _options_are_inert(arguments: Node) -> bool:
    for pair in (a for a in arguments.named_children if a.type == "pair"):
        key = _text(pair.child_by_field_name("key")).rstrip(":").lstrip(":").strip("\"'")
        value = pair.child_by_field_name("value")
        if key not in _OPTIONS:
            return False
        if key in _CONDITIONS and (value is None or value.type != "simple_symbol"):
            return False
    return True


def _non_public(body: Node, method: Node, name: str) -> bool:
    """Whether ``method`` is private or protected: after a bare ``private`` /
    ``protected``, or named by ``private :name``. A public method may be routed
    to by name, so it is never deleted."""
    mode = "public"
    for statement in body.named_children:
        if statement.id == method.id:
            return mode in _MODIFIERS
        if statement.type == "identifier":
            mode = _text(statement) if _text(statement) in (*_MODIFIERS, "public") else mode
    return False


def _dangling_modifiers(data: bytes, body: Node, deleted: set[int]) -> list[tuple[int, int, bytes]]:
    """The lines of a bare ``private``/``protected`` that nothing stands under
    once ``deleted`` methods go: only deleted methods lie between it and the
    next access modifier or the end of the body (RuboCop's
    Lint/UselessAccessModifier). A comment line and a ``class << self`` between
    them are not members of the section (the modifier does not reach a
    singleton class's methods). One with a comment directly above it stays.
    A ``public`` the drop leaves with no non-public section above it goes too."""
    children = body.named_children
    dropped: dict[int, tuple[int, int]] = {}
    for index, statement in enumerate(children):
        if statement.type != "identifier" or _text(statement) not in _MODIFIERS:
            continue
        segment = []
        for later in children[index + 1 :]:
            if later.type == "identifier" and _text(later) in (*_MODIFIERS, "public"):
                break
            if later.type == "comment" or later.type == "singleton_class":
                continue
            segment.append(later)
        if segment and all(c.id in deleted for c in segment):
            span = _line_span(data, statement)
            if span is not None and not _commented_above(data, span[0]):
                dropped[statement.id] = span
    if not dropped:
        return []
    before = after = "public"
    for statement in children:
        if statement.type != "identifier" or _text(statement) not in (*_MODIFIERS, "public"):
            continue
        mode = _text(statement)
        if mode == "public" and before != "public" and after == "public":
            span = _line_span(data, statement)
            if span is not None and not _commented_above(data, span[0]):
                dropped[statement.id] = span
                before = mode
                continue
        before = mode
        if statement.id not in dropped:
            after = mode
    return [(start, end, b"") for start, end in dropped.values()]


def _callbacks(body: Node, name: str) -> list[Node] | None:
    """The plain callback statements of ``body`` naming ``name`` and no other
    symbol; None when one of them carries an option that is not inert."""
    found = []
    for statement in body.named_children:
        if statement.type != "call" or statement.child_by_field_name("receiver") is not None:
            continue
        if _text(statement.child_by_field_name("method")) not in _CALLBACKS:
            continue
        if statement.child_by_field_name("block") is not None:
            continue
        arguments = statement.child_by_field_name("arguments")
        symbols = [a for a in arguments.named_children if a.type != "pair"] if arguments is not None else []
        if len(symbols) == 1 and symbols[0].type == "simple_symbol" and _text(symbols[0]) == f":{name}":
            if not _options_are_inert(arguments):
                return None
            found.append(statement)
    return found


def _only_mention(source: str, method: Node, callbacks: list[Node], name: str) -> bool:
    """Whether the file spells ``name`` nowhere but the method's own name and
    the callbacks' symbols."""
    skip = [method.child_by_field_name("name"), *(c.child_by_field_name("arguments").named_children[0] for c in callbacks)]
    data = source.encode("utf-8")
    word = _word(name)
    ranges = sorted((n.start_byte, n.end_byte) for n in skip)
    text = data
    for start, end in reversed(ranges):
        text = text[:start] + b" " * (end - start) + text[end:]
    return not word.search(text.decode("utf-8", errors="replace"))


def _line_span(data: bytes, node: Node) -> tuple[int, int] | None:
    """The whole lines ``node`` occupies, when nothing else shares them."""
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    end = data.find(b"\n", node.end_byte)
    end = len(data) if end == -1 else end + 1
    if data[start : node.start_byte].strip() or data[node.end_byte : end].strip():
        return None
    return start, end


def _commented_above(data: bytes, start: int) -> bool:
    previous = data[:start].rstrip(b"\n").rsplit(b"\n", 1)[-1]
    return previous.lstrip().startswith(b"#") and bool(data[:start])


def _deletion(data: bytes, method: Node, callbacks: list[Node]) -> list[tuple[int, int, bytes]] | None:
    """The line spans that delete ``method`` and its ``callbacks``, or None."""
    edits: list[tuple[int, int, bytes]] = []
    for node in (*callbacks, method):
        span = _line_span(data, node)
        if span is None or _commented_above(data, span[0]):
            return None
        start, end = span
        edits.append((start, end, b""))
    return edits


def _tidy(data: bytes, edits: list[tuple[int, int, bytes]]) -> list[tuple[int, int, bytes]]:
    """Join the deleted line spans that only blank lines separate, and let
    each joined span take ONE blank line with it: the one before it when
    there is one, else the one after."""
    joined: list[tuple[int, int, bytes]] = []
    for start, end, text in sorted(edits):
        if text:
            joined.append((start, end, text))
        elif joined and not joined[-1][2] and not data[joined[-1][1] : start].strip():
            joined[-1] = (joined[-1][0], max(end, joined[-1][1]), b"")
        else:
            joined.append((start, end, text))
    out: list[tuple[int, int, bytes]] = []
    for start, end, text in joined:
        if not text:
            before = data[:start]
            if before.endswith(b"\n\n"):
                start -= 1
            elif data[end : end + 1] == b"\n":
                end += 1
        out.append((start, end, text))
    return out


def _bare_return(node: Node) -> bool:
    return node.type == "return" and not node.named_children


def _key(method: Node) -> tuple[tuple[str, ...], str]:
    """A method's class/module scope and its name, ``self.`` (or another
    receiver) included for a singleton method."""
    receiver = method.child_by_field_name("object")
    prefix = _text(receiver) + "." if receiver is not None else ""
    return _scope(method), prefix + _text(method.child_by_field_name("name"))


def _methods(root: Node) -> dict[tuple[tuple[str, ...], str], list[Node]]:
    """Every ``def`` and ``def self.`` of the file, at any depth, by key."""
    found: dict[tuple[tuple[str, ...], str], list[Node]] = {}
    for node in _walk(root):
        if node.type in ("method", "singleton_method"):
            found.setdefault(_key(node), []).append(node)
    return found


def _statements(method: Node) -> list[Node] | None:
    """The statements of a method's ``def … end`` body (an empty list for an
    empty one), or None for an endless ``def m = …``. A ``rescue`` /
    ``ensure`` clause is not a statement, so a body holding one is None too."""
    body = method.child_by_field_name("body")
    if body is None:
        return [] if method.text.rstrip().endswith(b"end") else None
    if body.type != "body_statement":
        return None
    statements = body.named_children
    if any(s.type in ("rescue", "else", "ensure") for s in statements):
        return None
    return statements


def _returns(before: Node, after: Node, data: bytes, deleted: set[int]) -> list[tuple[int, int, bytes]]:
    """The edits that drop a bare ``return`` the run left with no work to do,
    in a method (at any depth, with parameters or not) the run changed.

    Two places, both value-neutral, because a method's value is its last
    statement's:

    * the method's ONLY statement: ``def m(x); return; end`` and
      ``def m(x); end`` both return ``nil``;
    * the LAST statement, after a modifier ``return if c`` / ``return unless c``:
      that statement is ``nil`` when its ``return`` does not run, so the method
      returns ``nil`` either way.

    Differential, per method: the same key names exactly one method on each
    side, and before the run that method had a body that did not end in a bare
    ``return`` (one that did is the customer's own). A comment anywhere in the
    method withholds it."""
    old = _methods(before)
    drops: list[tuple[int, int, bytes]] = []
    for key, nodes in _methods(after).items():
        if len(nodes) != 1 or len(old.get(key, ())) != 1:
            continue
        method, previous = nodes[0], old[key][0]
        if method.id in deleted or any(n.type == "comment" for n in _walk(method)):
            continue
        was = _statements(previous)
        if not was or _bare_return(was[-1]) or previous.text == method.text:
            continue
        now = _statements(method)
        if not now or not _bare_return(now[-1]):
            continue
        if len(now) > 1:
            tail = now[-2]
            if tail.type not in ("if_modifier", "unless_modifier"):
                continue
            guarded = tail.child_by_field_name("body")
            if guarded is None or not _bare_return(guarded):
                continue
        span = _line_span(data, now[-1])
        if span is not None:
            drops.append((span[0], span[1], b""))
    return drops


def emptied(before: str, after: str) -> tuple[str, ...]:
    """The methods ``after`` leaves with an empty body that had one before the
    run, each as ``Scope#name`` (``Scope.name`` for a singleton method). They
    are kept: something may still call them by name."""
    root = _parse(after, "ruby")
    if root.has_error:
        return ()
    old = _methods(_parse(before, "ruby"))
    labels: list[tuple[int, str]] = []
    for key, nodes in _methods(root).items():
        if len(nodes) != 1 or len(old.get(key, ())) != 1:
            continue
        if _statements(nodes[0]) != [] or not _statements(old[key][0]):
            continue
        scope, name = key
        owner = "::".join(scope)
        if "." in name:
            label = f"{owner}.{name.split('.', 1)[1]}" if owner else name
        else:
            label = f"{owner}#{name}" if owner else name
        labels.append((nodes[0].start_byte, label))
    return tuple(label for _, label in sorted(labels))
