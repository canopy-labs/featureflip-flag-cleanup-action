"""A Go flag read that returns ``(bool, error)``, folded with its error handling.

``on, err := client.BoolVariation("KEY", ctx, false)`` followed by
``if err != nil { return …, err }`` cannot take one literal: ``on, err := true``
does not build (see :mod:`flag_cleanup.go_arity`, which still refuses what this
leaves alone). Once the flag is fixed the call cannot fail, so the check that
follows it is dead, and ``err`` has no other business in the function. This pass
runs BEFORE the engine, on the customer's bytes, and rewrites such a pair into
the single-valued shape the engine already folds::

    on, err := read(ctx, "KEY", org)        on := read(ctx, "KEY", org)
    if err != nil { return "", err }   ->
    use(on)                                 use(on)

A pair is rewritten only when every condition below holds; otherwise the file is
left whole and the existing arity refusal speaks, exactly as before:

* the read is a bool reader (``BoolVariation`` or a configured accessor) and
  every declaration of it in the checkout returns ``(bool, error)``;
* no comment sits between the declaration and the end of the guard or trails the
  declaration's line;
* two plain names on the left (the second may be ``_``, which needs no handling
  at all), declared with ``:=`` as a statement of a block;
* a single call on the right whose callee is a flag read and that names the
  flag's key. Its arguments either cannot do work (names, selectors, literals,
  operators on them) or are calls, which the fold would drop: each such call
  (and a receiver that does work) is kept by :mod:`flag_cleanup.go_read_effects`,
  which runs after this pass, as ``_ = call`` on the line before the
  declaration, in order. Any other argument that can do work (an index, a
  receive, a call inside an operator, a function literal) is a refusal that
  says so;
* the very next statement is ``if err != nil { … }`` with no initializer, whose
  block holds no ``goto`` or label, and either no ``else`` or an ``else`` block
  of plain statements (no declaration, label or multi-line raw string, and no
  ``err``): the check is dead, so that block's statements take its place;
* ``err`` is not a parameter or named result of any enclosing function, and is
  spelled in the outermost enclosing function only in the declaration, that
  condition and that block. Or it is declared EARLIER (``o, err := lookup()``
  and then ``on, err := read()``, which assigns the first ``err``): then an
  earlier use of it must exist (or deleting the check leaves it unused), no
  closure and no loop may see it, and its next mention after the check must be
  a plain assignment statement in the same block, so nothing reads the value
  the read would have left in it;
* ``on`` is spelled nowhere before the declaration (so ``:=`` declares it
  fresh) and is not a parameter or named result.

The pass is undone with the rest of the run (it is a :class:`mock_stubs.Prepass`),
and :func:`survivors` names a rewritten read the engine did not fold, which
would leave ``on := read(…)`` standing: the run refuses it rather than ship it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import key_const, mock_stubs, source_index
from flag_cleanup.syntax import _parse, _walk

_FUNCTIONS = ("function_declaration", "method_declaration", "func_literal")
#: Argument node types that cannot do work.
_INERT_ARGUMENTS = frozenset(
    {
        "identifier", "selector_expression", "interpreted_string_literal", "raw_string_literal",
        "int_literal", "float_literal", "true", "false", "nil", "rune_literal", "imaginary_literal",
    }
)
#: Operator expressions that do no work when none of their operands does.
_PURE_OPERATORS = frozenset({"unary_expression", "binary_expression", "parenthesized_expression", "composite_literal"})


def _text(node: Node) -> str:
    return node.text.decode("utf-8")


@dataclass(frozen=True)
class _Pair:
    declaration: Node
    names: Node
    guard: Node | None
    call: Node
    #: Calls among the read's arguments, kept by ``go_read_effects`` after this pass.
    hoisted: tuple[Node, ...] = ()
    #: The ``else`` block of the guard, whose statements replace it.
    otherwise: Node | None = None


def find(
    source: str,
    flag_key: str,
    accessors: Iterable[str],
    key_texts: Iterable[str] = (),
    repo_dir: str | None = None,
) -> list[_Pair]:
    """The ``on, err := read(…)`` pairs of ``source`` this pass may rewrite."""
    return scan(source, flag_key, accessors, key_texts, repo_dir)[0]


def declined(
    source: str,
    flag_key: str,
    accessors: Iterable[str],
    key_texts: Iterable[str] = (),
    repo_dir: str | None = None,
) -> list[str]:
    """Why each two-value read of ``source`` this pass left whole was left, in
    source order, for the run to say instead of the generic count error."""
    return scan(source, flag_key, accessors, key_texts, repo_dir)[1]


def scan(
    source: str,
    flag_key: str,
    accessors: Iterable[str],
    key_texts: Iterable[str] = (),
    repo_dir: str | None = None,
) -> tuple[list[_Pair], list[str]]:
    if flag_key not in source and not any(t in source for t in key_texts):
        return [], []
    accessors = list(accessors)
    readers = key_const._GO_FLAG_READ_CALLEES | {a.split("[", 1)[0] for a in accessors}
    spellings = {t for t in key_texts}
    root = _parse(source, "go")
    comments = [n for n in _walk(root) if n.type == "comment"]
    found: list[_Pair] = []
    reasons: list[str] = []
    for node in _walk(root):
        if node.type != "short_var_declaration":
            continue
        pair = _pair(node, source, flag_key, readers, spellings)
        if isinstance(pair, str):
            reasons.append(pair)
            continue
        if pair is None or not _returns_bool_error(pair.call, repo_dir):
            continue
        if _has_comment(pair, comments, source):
            reasons.append(_reason(pair.call, "a comment sits inside the read and its error check, and would be lost"))
            continue
        found.append(pair)
    # A pair inside another pair's guard goes with it: rewriting both would
    # edit the same bytes twice.
    spans = [(p.declaration.start_byte, (p.guard or p.declaration).end_byte) for p in found]
    return [
        p for p, (lo, hi) in zip(found, spans)
        if not any(
            (olo, ohi) != (lo, hi) and olo <= lo and hi <= ohi
            for olo, ohi in spans
        )
    ], reasons


def _reason(call: Node, why: str) -> str:
    return (
        f"a read that returns (bool, error), `{' '.join(_text(call).split())}`, was left as it is: {why}"
    )


def _has_comment(pair: _Pair, comments: list[Node], source: str) -> bool:
    """A comment between the declaration and the end of the guard, or trailing
    the declaration's line, would be lost or orphaned."""
    start = pair.declaration.start_byte
    end = (pair.guard or pair.declaration).end_byte
    data = source.encode("utf-8")
    line_end = data.find(b"\n", pair.declaration.end_byte)
    line_end = len(data) if line_end == -1 else line_end
    return any(start <= c.start_byte < max(end, line_end) for c in comments)


_RESULT_TYPES = ("bool", "error")


def _returns_bool_error(call: Node, repo_dir: str | None) -> bool:
    """Whether every declaration of the read's callee visible in the checkout
    returns ``(bool, error)``. A callee declared nowhere in it (a package) is
    taken at its name, which is the bool reader the run was told about."""
    if repo_dir is None:
        return True
    function = call.child_by_field_name("function")
    if function is not None and function.type == "selector_expression":
        function = function.child_by_field_name("field")
    if function is None:
        return False
    name = _text(function)
    for listed in source_index.files(repo_dir, (".go",)):
        location = listed.path if listed.path.is_absolute() else Path(repo_dir) / listed.path
        try:
            text = location.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if name not in text:
            continue
        for node in _walk(_parse(text, "go")):
            if node.type not in ("function_declaration", "method_declaration", "method_elem"):
                continue
            declared = node.child_by_field_name("name")
            if declared is None or _text(declared) != name:
                continue
            if _declared_results(node) != _RESULT_TYPES:
                return False
    return True


def _declared_results(declaration: Node) -> tuple[str, ...]:
    result = declaration.child_by_field_name("result")
    if result is None:
        return ()
    if result.type != "parameter_list":
        return (_text(result),)
    types: list[str] = []
    for entry in result.named_children:
        if entry.type != "parameter_declaration":
            continue
        kind = entry.child_by_field_name("type")
        count = max(1, len(entry.children_by_field_name("name")))
        types.extend([_text(kind) if kind is not None else ""] * count)
    return tuple(types)


def _pair(node: Node, source: str, flag_key: str, readers: set[str], spellings: set[str]) -> _Pair | str | None:
    """The pair at ``node``; a reason string when it is a two-value read of the
    flag that cannot be rewritten; ``None`` when it is not one at all."""
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None:
        return None
    names = [c for c in left.named_children if c.type != "comment"]
    values = [c for c in right.named_children if c.type != "comment"]
    if len(names) != 2 or len(values) != 1 or any(n.type != "identifier" for n in names):
        return None
    on, err = _text(names[0]), _text(names[1])
    call = values[0]
    if on == "_" or on == err or call.type != "call_expression" or not _is_read(call, flag_key, readers, spellings):
        return None
    parent = node.parent
    if parent is None or parent.type not in ("statement_list", "block"):
        return None
    function = _outermost_function(node)
    if function is None or _binds(node, {on, err}):
        return _reason(call, f"`{on}` or `{err}` is a parameter or a named result")
    if _occurrences(function, on, before=node.start_byte):
        return _reason(call, f"`{on}` is already spelled before the read")
    hoisted = _argument_calls(call)
    if isinstance(hoisted, str):
        return _reason(call, hoisted)
    if hoisted and not _starts_its_line(source, node):
        return _reason(call, "the read shares its line with another statement, so the calls among its arguments cannot be kept")
    if err == "_":
        if hoisted:
            return _Pair(node, left, None, call, tuple(hoisted))
        return _Pair(node, left, None, call)
    siblings = [c for c in parent.named_children if c.type != "comment"]
    index = next((i for i, c in enumerate(siblings) if c.id == node.id), None)
    if index is None or index + 1 >= len(siblings):
        return None
    guard = siblings[index + 1]
    shape = _guard_shape(guard, err)
    if isinstance(shape, str):
        return _reason(call, shape) if shape else None
    condition, block, otherwise = shape
    allowed = [(node.start_byte, node.end_byte), (condition.start_byte, condition.end_byte),
               (block.start_byte, block.end_byte)]
    if otherwise is not None:
        why = _plain_else(source, node, otherwise, err)
        if why:
            return _reason(call, why)
    stray = [
        n for n in _occurrences(function, err)
        if not any(lo <= n.start_byte and n.end_byte <= hi for lo, hi in allowed)
    ]
    earlier = [n for n in stray if n.start_byte < node.start_byte]
    if earlier or any(n.start_byte >= guard.end_byte for n in stray):
        why = _err_reuse(function, node, guard, parent, err, stray)
        if why:
            return _reason(call, why)
    elif stray:
        return _reason(call, f"`{err}` is used inside the else block of the check")
    return _Pair(node, left, guard, call, tuple(hoisted), otherwise)


def _is_read(call: Node, flag_key: str, readers: set[str], spellings: set[str]) -> bool:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "selector_expression":
        function = function.child_by_field_name("field")
    if function is None or _text(function) not in readers:
        return False
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return False
    for argument in (c for c in arguments.named_children if c.type != "comment"):
        if argument.type in ("interpreted_string_literal", "raw_string_literal"):
            if _text(argument)[1:-1] == flag_key:
                return True
        elif argument.type in ("identifier", "selector_expression") and _text(argument) in spellings:
            return True
    return False


def _argument_calls(call: Node) -> list[Node] | str:
    """The calls among the read's arguments, which the fold would drop, or why
    some argument cannot be dropped."""
    arguments = call.child_by_field_name("arguments")
    found: list[Node] = []
    for argument in (c for c in (arguments.named_children if arguments is not None else []) if c.type != "comment"):
        if argument.type in _INERT_ARGUMENTS:
            continue
        if argument.type == "call_expression":
            found.append(argument)
            continue
        if argument.type in _PURE_OPERATORS and not any(
            n.type in ("call_expression", "func_literal", "index_expression", "slice_expression",
                       "type_assertion_expression")
            or (n.type == "unary_expression" and _text(n).startswith("<-"))
            for n in _walk(argument)
        ):
            continue
        return (
            f"the argument `{' '.join(_text(argument).split())}` can do work the fold would drop, and "
            "only a plain call, name or literal among them is kept"
        )
    return found


def _starts_its_line(source: str, node: Node) -> bool:
    data = source.encode("utf-8")
    line = data.rfind(b"\n", 0, node.start_byte) + 1
    return data[line:node.start_byte].strip(b" \t") == b""


def _guard_shape(guard: Node, err: str) -> tuple[Node, Node, Node | None] | str:
    """``(condition, block, else block)`` of an ``if err != nil`` guard; a
    reason when it is one the pass does not take; ``""`` when it is no guard."""
    if guard.type != "if_statement":
        return ""
    if guard.child_by_field_name("initializer") is not None:
        return ""
    condition = guard.child_by_field_name("condition")
    block = guard.child_by_field_name("consequence")
    if condition is None or block is None:
        return ""
    parts = [c for c in condition.children if c.type != "comment"]
    if len(parts) != 3 or _text(parts[0]) != err or _text(parts[1]) != "!=" or parts[2].type != "nil":
        return ""
    if any(n.type in ("goto_statement", "labeled_statement") for n in _walk(block)):
        return "the error check holds a `goto` or a label"
    alternative = guard.child_by_field_name("alternative")
    if alternative is not None and alternative.type != "block":
        return "the error check continues with an `else if`"
    return condition, block, alternative


def _plain_else(source: str, declaration: Node, otherwise: Node, err: str) -> str:
    """Why the statements of ``otherwise`` cannot simply take the check's place
    (``""`` when they can)."""
    inner = [c for c in otherwise.named_children if c.type != "comment"]
    statements = inner[0].named_children if len(inner) == 1 and inner[0].type == "statement_list" else inner
    if any(
        s.type in ("short_var_declaration", "var_declaration", "const_declaration", "type_declaration",
                   "labeled_statement", "goto_statement")
        for s in statements
    ):
        return "the else block of the check declares a name, which would then belong to the enclosing block"
    if any(n.type == "raw_string_literal" and b"\n" in n.text for n in _walk(otherwise)):
        return "the else block of the check holds a multi-line raw string, which cannot be re-indented"
    if any(n.type == "identifier" and _text(n) == err for n in _walk(otherwise)):
        return f"`{err}` is used inside the else block of the check"
    if _dedented(source, declaration, otherwise) is None:
        return "the else block of the check is not indented one level deeper than the check"
    return ""


def _indent_of(source: str, node: Node) -> str:
    data = source.encode("utf-8")
    line = data.rfind(b"\n", 0, node.start_byte) + 1
    return data[line:node.start_byte].decode("utf-8")


def _dedented(source: str, declaration: Node, otherwise: Node) -> str | None:
    """The statements of ``otherwise`` as lines at the declaration's own
    indent (each preceded by a newline), or ``None`` when its layout is not the
    one gofmt writes."""
    indent = _indent_of(source, declaration)
    if indent.strip(" \t") != "":
        return None
    text = otherwise.text.decode("utf-8")
    if not text.startswith("{\n") or not text.rstrip().endswith("\n" + indent + "}"):
        return None
    body = text[2:text.rstrip().rindex("\n" + indent + "}")]
    out = ""
    for line in body.split("\n"):
        if line.strip() == "":
            out += "\n" if out else ""
            continue
        if not line.startswith(indent + "\t"):
            return None
        out += "\n" + indent + line[len(indent) + 1:]
    return out


def _is_write_target(node: Node) -> bool:
    """Whether the identifier is assigned or declared there, not read."""
    parent = node.parent
    if parent is not None and parent.type == "expression_list":
        grand = parent.parent
        left = grand.child_by_field_name("left") if grand is not None else None
        return left is not None and left.id == parent.id and grand.type in (
            "short_var_declaration", "assignment_statement"
        ) and (grand.type == "short_var_declaration" or _text(grand.child_by_field_name("operator") or grand) == "=")
    if parent is not None and parent.type in ("var_spec", "parameter_declaration"):
        return node.id in {n.id for n in parent.children_by_field_name("name")}
    return False


def _err_reuse(
    function: Node, declaration: Node, guard: Node, parent: Node, err: str, stray: list[Node]
) -> str:
    """Why ``on, err := read(…)`` cannot drop ``err`` when it is spelled
    elsewhere in the function (``""`` when nothing can observe the difference).

    The read would have left ``nil`` in ``err``; the fold leaves whatever it
    held. That is unobservable only if the earlier ``err`` is read somewhere
    (else deleting the check leaves it unused), nothing earlier or in a closure
    or a loop can see it later, and the next mention after the check
    overwrites it as a statement of this very block.
    """
    earlier = [n for n in stray if n.start_byte < declaration.start_byte]
    later = sorted((n for n in stray if n.start_byte >= guard.end_byte), key=lambda n: n.start_byte)
    if not earlier:
        return f"`{err}` is used after the check"
    if all(_is_write_target(n) for n in earlier):
        return f"the earlier `{err}` is never read, so deleting the check would leave it unused"
    if any(n.type == "goto_statement" or n.type == "labeled_statement" for n in _walk(function)):
        return "the function uses a `goto` or a label"
    current = declaration.parent
    while current is not None and current.id != function.id:
        if current.type == "for_statement":
            return f"the read is inside a loop and `{err}` is spelled before it"
        current = current.parent
    for n in earlier:
        owner = n.parent
        while owner is not None and owner.id != function.id:
            if owner.type == "func_literal" and not (
                owner.start_byte <= declaration.start_byte and declaration.end_byte <= owner.end_byte
            ):
                return f"`{err}` is captured by a closure that can read it after the read"
            owner = owner.parent
    if not later:
        return ""
    first = later[0]
    statement = first.parent
    while statement is not None and statement.parent is not None and statement.parent.id != parent.id:
        statement = statement.parent
    if (
        statement is None or statement.parent is None or statement.parent.id != parent.id
        or statement.type not in ("short_var_declaration", "assignment_statement")
        or not _is_write_target(first)
        or any(
            n.type == "identifier" and _text(n) == err
            for n in _walk(statement.child_by_field_name("right") or statement)
        )
    ):
        return f"`{err}` is used after the check before it is assigned again"
    return ""


def _outermost_function(node: Node) -> Node | None:
    found = None
    current = node.parent
    while current is not None:
        if current.type in _FUNCTIONS:
            found = current
        current = current.parent
    return found


def _binds(node: Node, names: set[str]) -> bool:
    """Whether a parameter or named result of an enclosing function is one of ``names``."""
    current = node.parent
    while current is not None:
        if current.type in _FUNCTIONS:
            for key in ("parameters", "result", "receiver"):
                part = current.child_by_field_name(key)
                if part is None:
                    continue
                for n in _walk(part):
                    if n.type == "identifier" and _text(n) in names:
                        return True
        current = current.parent
    return False


def _occurrences(function: Node, name: str, before: int | None = None) -> list[Node]:
    return [
        n for n in _walk(function)
        if n.type in ("identifier", "field_identifier") and n.child_count == 0
        and _text(n) == name and (before is None or n.start_byte < before)
        and not (n.type == "field_identifier")
    ]


def rewrite(
    source: str,
    flag_key: str,
    accessors: Iterable[str],
    key_texts: Iterable[str] = (),
    repo_dir: str | None = None,
) -> tuple[str, list[str]]:
    """``(rewritten source, the read calls' texts)``."""
    pairs = find(source, flag_key, accessors, key_texts, repo_dir)
    if not pairs:
        return source, []
    data = source.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    calls: list[str] = []
    for pair in pairs:
        first = pair.names.named_children[0]
        edits.append((first.end_byte, pair.names.end_byte, b""))
        if pair.guard is not None:
            replacement = ""
            if pair.otherwise is not None:
                replacement = _dedented(source, pair.declaration, pair.otherwise) or ""
            edits.append((pair.declaration.end_byte, pair.guard.end_byte, replacement.encode("utf-8")))
        calls.append(" ".join(_text(pair.call).split()))
    for start, end, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8"), calls


@dataclass
class Prepass(mock_stubs.Prepass):
    """Rewrites each safe two-value read before the engine and owns the undo."""

    #: Per file, why each two-value read it left whole was left.
    declines: dict[str, list[str]] = field(default_factory=dict)

    def apply(self, paths: list[str]) -> None:
        for path in paths:
            try:
                source = mock_stubs._read_exact(path)
            except (OSError, UnicodeDecodeError):
                continue
            key = os.path.abspath(path)
            if key in self.originals:
                continue
            texts = self.key_texts.get(path, frozenset())
            resolved, calls = rewrite(source, self.flag_key, self.accessors, texts, self.repo_dir)
            why = declined(source, self.flag_key, self.accessors, texts, self.repo_dir)
            if why:
                self.declines[key] = why
            if not calls:
                continue
            self.spelled[key] = path
            self.originals[key] = source
            self.removals[key] = [mock_stubs.Removal("two-value read", c) for c in calls]
            self.edited[key] = resolved
            mock_stubs._write_exact(path, resolved)

    def report(self, repo_dir: str) -> tuple[tuple[str, str, str], ...]:
        return ()


def declined_reason(passes: Iterable[Prepass], path: str) -> str | None:
    """What this pass says about a two-value read of ``path`` it left whole,
    which the generic count refusal would otherwise describe wrongly."""
    key = os.path.abspath(path)
    for prepass in passes:
        reasons = prepass.declines.get(key)
        if reasons:
            return reasons[0]
    return None


def survivors(passes: Iterable[Prepass], path: str, content: str) -> str | None:
    """Why ``content`` (``path``'s result) is refused: a read this pass rewrote
    to ``on := read(…)`` that the engine did not fold, so it would not build."""
    key = os.path.abspath(path)
    flat = " ".join(content.split())
    for prepass in passes:
        for removal in prepass.removals.get(key, ()):
            if removal.kind == "two-value read" and f":= {removal.text}" in flat:
                return (
                    f"a read that returns (bool, error), `{removal.text}`, could not be "
                    "folded after its error check was removed, so the result would not build"
                )
    return None
