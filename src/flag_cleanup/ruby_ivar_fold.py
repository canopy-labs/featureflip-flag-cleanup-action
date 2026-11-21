"""Fold the reads of a Ruby instance variable the run bound to a literal.

A flag read stored in a local is inlined by the engine. Stored in an instance
variable, it stops at the assignment::

    @preview = Flipper.enabled?(:loadout_preview)   # -> @preview = false
    @ports = (model.ports if @preview) || []        # left as written

An instance variable is visible to every method of the object and, in a Rails
controller, to the view, so the engine is right not to inline it on its own.
This pass proves the reads it rewrites, and only those:

* **The run made the assignment a literal.** A plain ``@x = <read>`` statement
  directly in a method's body (not in a branch, a block or a ``begin``) whose
  value is ``true``/``false`` in the output and was not in the input, in a
  method that is the only one of its name on each side.
* **Every write of ``@x`` in the checkout stores that same literal.** Any other
  write anywhere (another value, ``||=``, ``+=``, a multiple assignment,
  ``rescue => @x``, ``for @x in``), a write inside a string or comment that an
  ``eval`` could run, any ``instance_variable_set`` or
  ``remove_instance_variable``, and an ``attr_writer``/``attr_accessor`` naming
  ``x`` all withhold the file. A method called between the assignment and a
  read can only write ``@x`` through one of those, so after the assignment
  ``@x`` holds the literal.
* **A read is folded only after the assignment, in the same method body**: in a
  later statement of that body (not its ``rescue``/``else``/``ensure`` clause),
  and not inside a block, a lambda, a nested ``def``, a class body or a string.
  A block may run with another ``self`` (``instance_exec``); a nested ``def``
  runs on another call. A write of ``@x`` among those statements stops the pass.
  ``defined?(@x)`` is never rewritten, and neither is a read inside a pair of
  parentheses holding a modifier, ``and``/``or`` or ``not`` in a file that also
  holds such code unparenthesised (Gate 1 refuses that fold, flag-wide, for a
  direct read too: the read is left as written rather than turned into a
  refusal).

The assignment itself is always KEPT: a view, another method, ``instance_values``
or a serializer may read it. It is reported (:func:`kept_assignments`).

Each folded read becomes ``inlined<marker>(<literal>)``, and the caller hands the
file back to the engine, whose marker rule in ``ruby.toml`` creates the literal
inside the cascade, so the guard folds as a read written inline would.

Placement: split. Which reads qualify and the checkout-wide write census are R1
and R3 (policy, and facts from every file); the fold is the engine's existing
cascade (R4), reached through the marker rule.

Not seen: a gem that writes the same instance variable on the same object.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index, syntax
from flag_cleanup.unused import _parse, _walk

#: The ``bindings`` kind of a kept assignment the run made a literal.
KEPT_KIND = "kept instance variable"

_LITERALS = frozenset({"true", "false"})
_SUFFIXES = {".rb", ".rake", ".erb"}
#: A read under one of these is not folded: another ``self``, another call, or text.
_STOPS = frozenset(
    {
        "block",
        "do_block",
        "lambda",
        "method",
        "singleton_method",
        "class",
        "module",
        "singleton_class",
        "string",
        "heredoc_body",
        "subshell",
        "regex",
        "delimited_symbol",
        "string_array",
        "symbol_array",
    }
)
_CLAUSES = frozenset({"rescue", "else", "ensure"})
#: Spellings anywhere in the checkout that can write any instance variable.
_REFLECTIVE_WRITES = re.compile(r"\b(?:instance_variable_set|remove_instance_variable)\b")


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _key(method: Node) -> tuple[tuple[str, ...], str]:
    names: list[str] = []
    current = method.parent
    while current is not None:
        if current.type in ("class", "module"):
            names.append(_text(current.child_by_field_name("name")))
        current = current.parent
    receiver = method.child_by_field_name("object")
    prefix = _text(receiver) + "." if receiver is not None else ""
    return tuple(reversed(names)), prefix + _text(method.child_by_field_name("name"))


def _methods(root: Node) -> dict[tuple[tuple[str, ...], str], list[Node]]:
    found: dict[tuple[tuple[str, ...], str], list[Node]] = {}
    for node in _walk(root):
        if node.type in ("method", "singleton_method"):
            found.setdefault(_key(node), []).append(node)
    return found


def _statements(method: Node) -> list[Node]:
    body = method.child_by_field_name("body")
    if body is None or body.type != "body_statement":
        return []
    return list(body.named_children)


def _assigned(statement: Node) -> tuple[str, Node] | None:
    """``(@name, value)`` for a plain ``@name = value`` statement."""
    if statement.type != "assignment":
        return None
    left = statement.child_by_field_name("left")
    right = statement.child_by_field_name("right")
    if left is None or right is None or left.type != "instance_variable":
        return None
    return _text(left), right


def _literal_assignments(before: Node, after: Node) -> list[tuple[Node, str, str]]:
    """``(statement, @name, literal)`` for each assignment the run made a
    literal, directly in the body of a method unique to its key on each side."""
    old = _methods(before)
    found: list[tuple[Node, str, str]] = []
    for key, nodes in _methods(after).items():
        if len(nodes) != 1 or len(old.get(key, ())) != 1:
            continue
        was: dict[str, list[Node]] = {}
        for statement in _statements(old[key][0]):
            pair = _assigned(statement)
            if pair is not None:
                was.setdefault(pair[0], []).append(pair[1])
        now: dict[str, list[tuple[Node, Node]]] = {}
        for statement in _statements(nodes[0]):
            pair = _assigned(statement)
            if pair is not None:
                now.setdefault(pair[0], []).append((statement, pair[1]))
        for name, writes in now.items():
            if len(writes) != 1 or len(was.get(name, ())) != 1:
                continue
            statement, value = writes[0]
            if value.type in _LITERALS and was[name][0].type not in _LITERALS:
                found.append((statement, name, value.type))
    return found


def _is_write(node: Node) -> bool:
    """Whether an ``instance_variable`` node is written, not read."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("assignment", "operator_assignment"):
        return parent.child_by_field_name("left") == node
    if parent.type in ("left_assignment_list", "destructured_left_assignment", "rest_assignment"):
        return True
    if parent.type == "exception_variable":
        return True
    if parent.type == "for":
        return parent.child_by_field_name("pattern") == node
    return False


_MODIFIERS = frozenset({"rescue_modifier", "if_modifier", "unless_modifier", "while_modifier", "until_modifier"})


def _loose(node: Node) -> bool:
    """A node whose meaning a pair of parentheses can hold: a modifier, an
    ``and``/``or``, a ``not``."""
    if node.type in _MODIFIERS:
        return True
    operator = node.child_by_field_name("operator")
    if node.type == "binary" and operator is not None:
        return operator.type in ("and", "or")
    if node.type == "unary" and operator is not None:
        return operator.type == "not"
    return False


def _paired(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == "parenthesized_statements":
            return True
        current = current.parent
    return False


def _exposed_loose(root: Node) -> bool:
    """Whether the file holds a modifier, ``and``/``or`` or ``not`` outside
    every pair of parentheses."""
    return any(_loose(n) and not _paired(n) for n in _walk(root))


def _in_held_pair(node: Node, statement: Node) -> bool:
    """Whether ``node`` sits in a pair of parentheses, within ``statement``,
    that holds a modifier, ``and``/``or`` or ``not``. Folding there changes
    the pair's content, which Gate 1 refuses in a file that also holds such
    code unparenthesised; the read is left as written instead."""
    current = node.parent
    while current is not None and current.id != statement.id:
        if current.type == "parenthesized_statements" and any(_loose(n) for n in _walk(current)):
            return True
        current = current.parent
    return False


def _reads(method: Node, assignment: Node, name: str, *, held_pairs: bool = True) -> list[Node] | None:
    """The reads of ``name`` this pass may fold: after ``assignment``, in the
    same body, outside every construct in ``_STOPS`` (and, unless
    ``held_pairs``, outside a pair holding a modifier). None when the body
    writes ``name`` again after the assignment."""
    statements = _statements(method)
    index = next(i for i, s in enumerate(statements) if s.id == assignment.id)
    found: list[Node] = []
    for statement in statements[index + 1 :]:
        if statement.type in _CLAUSES:
            break
        for node in _walk(statement):
            if node.type != "instance_variable" or _text(node) != name:
                continue
            if _is_write(node):
                return None
            if _stopped(node, statement) or _under_defined(node):
                continue
            if not held_pairs and _in_held_pair(node, statement):
                continue
            found.append(node)
    return found


def _stopped(node: Node, statement: Node) -> bool:
    current = node.parent
    while current is not None and current.id != statement.id:
        if current.type in _STOPS:
            return True
        current = current.parent
    return statement.type in _STOPS


def _under_defined(node: Node) -> bool:
    current = node.parent
    while current is not None and current.type == "parenthesized_statements":
        current = current.parent
    return current is not None and current.type == "unary" and _text(current).startswith("defined?")


class _Census:
    """Every write of an instance variable in the checkout's Ruby, ERB and
    other Ruby-bearing files, read from disk once."""

    def __init__(self, repo_dir: str) -> None:
        suffixes = _SUFFIXES | set(source_index.RUBY_SUFFIXES) | set(source_index.NO_SUFFIX)
        self.texts: dict[str, str] = {}
        for f in source_index.files(repo_dir, suffixes, with_ignored=True):
            try:
                self.texts[f.resolved] = f.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                self.texts[f.resolved] = ""

    def reflective(self) -> bool:
        return any(_REFLECTIVE_WRITES.search(text) for text in self.texts.values())

    def only_writes(self, name: str, literal: str, path: str, current: str) -> bool:
        """Whether every write of ``name`` in the checkout stores ``literal``.
        ``path``'s text is ``current`` rather than what is on disk."""
        resolved = str(Path(path).resolve())
        bare = name[1:]
        accessor = re.compile(
            r"\battr_(?:writer|accessor)\b[^\n]*(?::|[\"'])" + re.escape(bare) + r"(?![\w?!=])"
        )
        texts = dict(self.texts)
        texts[resolved] = current
        for file, text in texts.items():
            if name not in text and bare not in text:
                continue
            if accessor.search(text):
                return False
            if not _writes_only(name, literal, file, text):
                return False
        return True


#: Any assignment operator right after a name: `=`, `+=`, `||=`, `**=`, ...
_ASSIGNS = r"\s*(?:[-+*/%&|^]|\*\*|<<|>>|&&|\|\|)?=(?![=~>])"


def _writes_only(name: str, literal: str, file: str, text: str) -> bool:
    """Whether every write of ``name`` in one file's ``text`` is a plain
    ``name = literal``. A parsed file is judged on its tree, and the text
    inside its strings and comments (an ``eval`` can run it) on its words;
    anything else on its words alone, a following comma counting as a write."""
    token = re.compile(r"(?<![\w@])" + re.escape(name) + r"(?!\w)")
    source, language = text, "ruby"
    if file.endswith(".erb"):
        view = syntax.host_code_view(text, "erb")
        if view is None:
            return _words_only(token, literal, text, comma_writes=True)
        source = view[0]
    elif not (file.endswith((".rb", ".rake")) or Path(file).suffix in source_index.RUBY_SUFFIXES):
        return _words_only(token, literal, text, comma_writes=True)
    root = _parse(source, language)
    if root.has_error:
        return _words_only(token, literal, source, comma_writes=True)
    for node in _walk(root):
        if node.type == "instance_variable" and _text(node) == name and _is_write(node):
            parent = node.parent
            if parent.type != "assignment" or _text(parent.child_by_field_name("right")) != literal:
                return False
        if node.type in ("string_content", "heredoc_content", "comment"):
            if not _words_only(token, literal, _text(node), comma_writes=False):
                return False
    return True


def _words_only(token: re.Pattern[str], literal: str, text: str, *, comma_writes: bool) -> bool:
    for match in token.finditer(text):
        rest = text[match.end() :]
        before = text[: match.start()].rstrip()
        if before.endswith("=>") or re.search(r"\bfor\s*$", before) or re.search(r"\bfor\s+[^\n]*,\s*$", before):
            return False
        assigned = re.match(_ASSIGNS, rest)
        if assigned:
            value = re.match(r"\s*(\w+)", rest[assigned.end() :])
            if rest.lstrip().startswith("=") and value is not None and value.group(1) == literal:
                continue
            return False
        if comma_writes and re.match(r"\s*,", rest):
            return False
    return True


def inline_ivar_reads(before: str, after: str, marker: str, *, path: str, repo_dir: str) -> str:
    """``after`` with each provable read of an instance variable the run bound
    to a literal replaced by ``inlined<marker>(<literal>)``; ``after`` itself
    when nothing qualifies, or when it already holds the marker."""
    if after == before or marker in after:
        return after
    root = _parse(after, "ruby")
    if root.has_error:
        return after
    candidates = _literal_assignments(_parse(before, "ruby"), root)
    if not candidates:
        return after
    census = _Census(repo_dir)
    if census.reflective():
        return after
    data = after.encode("utf-8")
    held_pairs = not _exposed_loose(root)
    edits: list[tuple[int, int, bytes]] = []
    for statement, name, literal in candidates:
        method = statement.parent.parent
        reads = _reads(method, statement, name, held_pairs=held_pairs)
        if not reads or not census.only_writes(name, literal, path, after):
            continue
        replacement = f"inlined{marker}({literal})".encode("utf-8")
        edits.extend((n.start_byte, n.end_byte, replacement) for n in reads)
    if not edits:
        return after
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    result = data.decode("utf-8")
    return result if not _parse(result, "ruby").has_error else after


def kept_assignments(before: str, after: str) -> tuple[str, ...]:
    """Each instance variable assignment the run made a literal, as
    ``@name = literal``, in source order: kept, because something outside the
    method may read it."""
    root = _parse(after, "ruby")
    if root.has_error:
        return ()
    rows = sorted(
        (statement.start_byte, f"{name} = {literal}")
        for statement, name, literal in _literal_assignments(_parse(before, "ruby"), root)
    )
    return tuple(text for _, text in rows)
