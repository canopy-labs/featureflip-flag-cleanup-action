"""Fold the literal ``when`` arms Piranha leaves behind in a subject-less ``case``.

The same construct, and the same structural gap, as :mod:`flag_cleanup.go_fold`
— see that module for the query-engine limitation both share. Ruby's version of
Go's tagless switch is ``case`` written with **no subject**::

    case
    when admin?(ctx)  then admin_path
    when <read>       then legacy_path
    else                   new_path
    end

Each ``when`` pattern is evaluated as a condition and the first truthy one wins,
which makes it an ``if``/``elsif`` chain by another spelling. The engine's
built-ins fold the ``if`` family only, so the read became ``when false`` and
nothing else moved.

**Ruby's gap was worse than Go's, and silently so.** Go at least refused: its
profile carries a ``literal_residues`` entry, so the dead arm reached Gate 1 and
the flag was abandoned loudly. Ruby's profile carried NO residue entry at all,
so the dead arm — with the flag key already gone from the file — was written
into the customer's pull request and no later run could ever revisit it. This
module and the matching ``("when", "pattern")`` entry in `syntax.py` close that
together: the fold handles what it can, the entry refuses what it cannot.

**Two hazards Go has and Ruby does not**, which is why this module is shorter:

* There is no ``fallthrough``. An arm's body never runs into the next one, so
  every arm is an independent edit.
* ``break`` does not bind to ``case``. It binds to the nearest enclosing loop,
  and ``case`` is not one, so lifting a body out of a ``case`` cannot change
  which construct a ``break`` leaves.

``case`` is an EXPRESSION in Ruby — ``x = case … end`` is ordinary — and both
rewrites preserve that: collapsing to the winning arm's body yields the value
that arm produced, and promoting an arm to ``else`` leaves a ``case`` behind.

A ``case v`` WITH a subject is ordinary customer code comparing values, and is
matched by nothing here: every rewrite requires the ``value`` field to be
absent, which is what "subject-less" means.
"""

from __future__ import annotations

import tree_sitter_ruby
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_ruby.language())
_PARSER = Parser(_LANGUAGE)

_MAX_ROUNDS = 100


def fold_literal_whens(before: str, after: str) -> str:
    """Fold every subject-less ``when`` the transform left with a literal pattern.

    Nothing happens if ``before`` already had one, exactly as in ``go_fold``,
    ``php_fold`` and ``python_fold``: ``when true`` is real code somebody may
    have written, and once the file contains one this cannot tell the
    customer's from the fold's.
    """
    if after == before or _literal_whens(before):
        return after
    for _ in range(_MAX_ROUNDS):
        rewritten = _fold_once(after)
        if rewritten is None:
            return after
        after = rewritten
    return after


def _literal_whens(source: str) -> int:
    """How many subject-less ``when`` arms in ``source`` carry a boolean literal."""
    root = _PARSER.parse(source.encode("utf-8")).root_node
    return sum(
        1
        for case in _iter(root)
        if _is_subjectless(case)
        for arm in case.named_children
        if _literal(arm) is not None
    )


def _iter(node: Node):
    yield node
    for child in node.children:
        yield from _iter(child)


def _is_subjectless(node: Node) -> bool:
    """A ``case`` with no subject expression -- Ruby's conditional form."""
    return node.type == "case" and node.child_by_field_name("value") is None


def _patterns(arm: Node) -> list[Node]:
    return [
        child
        for index, child in enumerate(arm.children)
        if arm.field_name_for_child(index) == "pattern"
    ]


def _literal(arm: Node) -> bool | None:
    """``True``/``False`` if this arm's sole pattern is a boolean literal.

    ``when a, b`` is a list of alternatives rather than one condition, so a
    literal among several is deliberately not resolved -- the same reading
    ``go_fold`` gives a multi-value ``case a, b:``.
    """
    if arm.type != "when":
        return None
    patterns = _patterns(arm)
    if len(patterns) != 1 or not patterns[0].named_children:
        return None
    kind = patterns[0].named_children[0].type
    return True if kind == "true" else False if kind == "false" else None


def _line_indent(data: bytes, position: int) -> str:
    start = data.rfind(b"\n", 0, position) + 1
    prefix = data[start:position]
    return prefix[: len(prefix) - len(prefix.lstrip())].decode("utf-8")


def _fold_once(source: str) -> str | None:
    """Apply the first foldable literal arm found; ``None`` if there is none."""
    data = source.encode("utf-8")
    root = _PARSER.parse(data).root_node
    for case in _iter(root):
        if not _is_subjectless(case):
            continue
        arms = case.named_children
        for index, arm in enumerate(arms):
            verdict = _literal(arm)
            if verdict is None:
                continue
            rewritten = (
                _delete_arm(data, case, arm, arms)
                if verdict is False
                else _fold_true_arm(data, case, arm, arms, index)
            )
            if rewritten is not None:
                return rewritten
    return None


def _delete_arm(data: bytes, case: Node, arm: Node, arms: list[Node]) -> str | None:
    """Drop an arm that can never match.

    A ``case`` whose only arm goes is removed entirely rather than left as
    ``case\\nend``. That is not merely tidier: a subject-less ``case`` with no
    surviving ``when`` evaluates to ``nil``, so leaving the husk keeps a value
    the customer's code may still be assigning from -- but so does deleting it
    only when nothing else remains to produce one, which is why the husk goes
    only when there is no ``else`` either.
    """
    if len(arms) == 1:
        return _cut_statement(data, case)
    return _cut(data, arm.start_byte, arm.end_byte)


def _fold_true_arm(
    data: bytes, case: Node, arm: Node, arms: list[Node], index: int
) -> str | None:
    """An arm that always matches: collapse to it, or make it the ``else``."""
    body = arm.child_by_field_name("body")
    if body is None:
        return None
    if index == 0:
        return _splice(data, case, body)
    # Arms above still take precedence, so this becomes the terminal `else`.
    return _promote_to_else(data, arm, body, arms, index)


def _splice(data: bytes, case: Node, body: Node) -> str:
    """Replace the whole ``case`` with ``body``, re-indented to its column.

    The ``body`` field carries its own leading newline and indentation (it is a
    ``then`` node spanning from just after the pattern), so the first line is
    recovered by stripping that rather than by reading the node's text raw.
    """
    base = _line_indent(data, case.start_byte)
    text = data[body.start_byte : body.end_byte].decode("utf-8")
    if text.startswith("then "):
        return _replace(data, case.start_byte, case.end_byte, text[len("then ") :])
    lines = text.lstrip("\n").splitlines()
    if not lines:
        return _replace(data, case.start_byte, case.end_byte, "")
    inner = lines[0][: len(lines[0]) - len(lines[0].lstrip())]
    extra = inner[len(base) :] if inner.startswith(base) else ""
    out = []
    for position, line in enumerate(lines):
        stripped = (
            base + line[len(base) + len(extra) :]
            if line.startswith(base + extra)
            else line
        )
        out.append(stripped[len(base) :] if position == 0 else stripped)
    return _replace(data, case.start_byte, case.end_byte, "\n".join(out).rstrip())


def _promote_to_else(
    data: bytes, arm: Node, body: Node, arms: list[Node], index: int
) -> str:
    """Make ``arm`` the terminal ``else`` and drop every arm below it.

    Only the ``when <pattern>`` header is replaced, so the body keeps the
    indentation it already had and nothing needs re-computing.
    """
    source = data.decode("utf-8")
    promoted = source[: arm.start_byte] + "else"
    if index == len(arms) - 1:
        return promoted + source[body.start_byte :]
    # Everything below an arm that always matches is unreachable, including a
    # pre-existing `else`. Resuming after the LAST arm keeps the `end` keyword
    # and its indentation, which sit outside every arm node.
    return (
        promoted
        + source[body.start_byte : arm.end_byte]
        + source[arms[-1].end_byte :]
    )


def _cut(data: bytes, start: int, end: int) -> str:
    """Remove ``[start, end)`` along with the line the arm opened on."""
    source = data.decode("utf-8")
    line_start = source.rfind("\n", 0, start) + 1
    tail = end + 1 if source[end : end + 1] == "\n" else end
    return source[:line_start] + source[tail:]


def _cut_statement(data: bytes, node: Node) -> str:
    source = data.decode("utf-8")
    start = source.rfind("\n", 0, node.start_byte) + 1
    end = node.end_byte
    if source[end : end + 1] == "\n":
        end += 1
    return source[:start] + source[end:]


def _replace(data: bytes, start: int, end: int, text: str) -> str:
    source = data.decode("utf-8")
    return source[:start] + text + source[end:]
