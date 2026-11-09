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
  flag's key, with arguments that cannot do work (names, selectors, literals);
* the very next statement is ``if err != nil { … }`` with no initializer and no
  ``else``, and its block holds no ``goto`` or label;
* ``err`` is spelled nowhere in the outermost enclosing function but in the
  declaration, that condition and that block; ``on`` is spelled nowhere before
  the declaration (so ``:=`` declares it fresh); neither is a parameter or a
  named result of any enclosing function.

The pass is undone with the rest of the run (it is a :class:`mock_stubs.Prepass`),
and :func:`survivors` names a rewritten read the engine did not fold, which
would leave ``on := read(…)`` standing: the run refuses it rather than ship it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
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


def _text(node: Node) -> str:
    return node.text.decode("utf-8")


@dataclass(frozen=True)
class _Pair:
    declaration: Node
    names: Node
    guard: Node | None
    call: Node


def find(
    source: str,
    flag_key: str,
    accessors: Iterable[str],
    key_texts: Iterable[str] = (),
    repo_dir: str | None = None,
) -> list[_Pair]:
    """The ``on, err := read(…)`` pairs of ``source`` this pass may rewrite."""
    if flag_key not in source and not any(t in source for t in key_texts):
        return []
    accessors = list(accessors)
    readers = key_const._GO_FLAG_READ_CALLEES | {a.split("[", 1)[0] for a in accessors}
    spellings = {t for t in key_texts}
    root = _parse(source, "go")
    comments = [n for n in _walk(root) if n.type == "comment"]
    found: list[_Pair] = []
    for node in _walk(root):
        if node.type != "short_var_declaration":
            continue
        pair = _pair(node, flag_key, readers, spellings)
        if pair is None or _has_comment(pair, comments, source) or not _returns_bool_error(pair.call, repo_dir):
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
    ]


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


def _pair(node: Node, flag_key: str, readers: set[str], spellings: set[str]) -> _Pair | None:
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
        return None
    if _occurrences(function, on, before=node.start_byte):
        return None
    if err == "_":
        return _Pair(node, left, None, call)
    siblings = [c for c in parent.named_children if c.type != "comment"]
    index = next((i for i, c in enumerate(siblings) if c.id == node.id), None)
    if index is None or index + 1 >= len(siblings):
        return None
    guard = siblings[index + 1]
    if not _is_nil_guard(guard, err, parent, node):
        return None
    allowed = [(node.start_byte, node.end_byte), (guard.start_byte, guard.end_byte)]
    stray = [
        n for n in _occurrences(function, err)
        if not any(lo <= n.start_byte and n.end_byte <= hi for lo, hi in allowed)
    ]
    if stray:
        return None
    return _Pair(node, left, guard, call)


def _is_read(call: Node, flag_key: str, readers: set[str], spellings: set[str]) -> bool:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "selector_expression":
        function = function.child_by_field_name("field")
    if function is None or _text(function) not in readers:
        return False
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return False
    named = [c for c in arguments.named_children if c.type != "comment"]
    if any(c.type not in _INERT_ARGUMENTS for c in named):
        return False
    for argument in named:
        if argument.type in ("interpreted_string_literal", "raw_string_literal"):
            if _text(argument)[1:-1] == flag_key:
                return True
        elif _text(argument) in spellings:
            return True
    return False


def _is_nil_guard(guard: Node, err: str, parent: Node, declaration: Node) -> bool:
    if guard.type != "if_statement":
        return False
    if guard.child_by_field_name("initializer") is not None or guard.child_by_field_name("alternative") is not None:
        return False
    condition = guard.child_by_field_name("condition")
    block = guard.child_by_field_name("consequence")
    if condition is None or block is None:
        return False
    parts = [c for c in condition.children if c.type != "comment"]
    if len(parts) != 3 or _text(parts[0]) != err or _text(parts[1]) != "!=" or parts[2].type != "nil":
        return False
    return not any(n.type in ("goto_statement", "labeled_statement") for n in _walk(block))


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
            edits.append((pair.declaration.end_byte, pair.guard.end_byte, b""))
        calls.append(" ".join(_text(pair.call).split()))
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8"), calls


@dataclass
class Prepass(mock_stubs.Prepass):
    """Rewrites each safe two-value read before the engine and owns the undo."""

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
            if not calls:
                continue
            self.spelled[key] = path
            self.originals[key] = source
            self.removals[key] = [mock_stubs.Removal("two-value read", c) for c in calls]
            self.edited[key] = resolved
            mock_stubs._write_exact(path, resolved)

    def report(self, repo_dir: str) -> tuple[tuple[str, str, str], ...]:
        return ()


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
