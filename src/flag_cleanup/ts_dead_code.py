"""Dead code the TypeScript family's fold leaves behind that its lint rejects.

The fold folds an expression and the cascade folds the ``if`` around it, but a
flag read can sit in positions where what is left over is a construct, not an
expression, and ``eslint:recommended`` (or ``tsc``) fails each one:

* ``for (init; false; update) …`` — ``no-constant-condition``. The loop never
  runs its body, but its INITIALISER does, so the loop is replaced by whatever
  of it runs rather than deleted outright. The engine does this for every
  initialiser it can judge from a query; this finishes the ``let``/``const``
  and expression forms it declines.
* ``outer: …`` whose last ``break outer``/``continue outer`` the fold removed —
  ``no-unused-labels``, and ``tsc`` reports TS7028. The label goes, the
  statement it labelled stays.

The true treatment's ``for (; true; )`` and ``do … while (true)`` are the
engine's to rewrite, and are not here, and so are ``while (false)``, every
``do … while (false)`` (the engine unwraps it, or declines and Gate 1 refuses)
and a ``static {}`` the fold emptied.

Every pass is DIFFERENTIAL: nothing is touched when the input already had one
of the shapes, because then the fold's cannot be told from the customer's. What a pass declines is
counted by :func:`constant_condition_loops`, which Gate 1 refuses on, so a loop
this cannot finish is a refusal rather than a pull request whose lint fails.
:func:`constant_condition_ifs` counts the ``if`` the engine leaves the same way.
The loops go in the stranded-statement window; the labels after the other text passes (see
:func:`remove_orphans`).
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk
from flag_cleanup.unused import _subtree_is_inert

#: Parents a statement may be removed from or replaced in: each holds a list
#: of statements, so one fewer is always valid there.
_STATEMENT_LISTS = frozenset({"statement_block", "program"})


def remove_dead_loops(before: str, after: str, language: str) -> tuple[str, list[str]]:
    """Replace each dead ``for`` loop the fold made with what of it runs.

    Returns the text of every loop removed, for the pull request body, in the
    same list the engine's own dead-loop deletions are reported in. A ``for``
    reaches this only when the engine declined it.
    """
    if after == before or _dead(before, language):
        return after, []
    return _remove_dead_loops(after, language)


def remove_orphans(before: str, after: str, language: str) -> str:
    """Drop each label the earlier passes left with nothing to jump to it.

    The engine deletes one whose last jump it removed within its reach; this
    finishes one whose ``break outer`` was folded away deeper in the loop. Not
    listed in the pull request body: a label nothing jumps to is nothing the
    customer could want kept.
    """
    return _remove_unused_labels(before, after, language)


def constant_condition_loops(source: str, language: str) -> int:
    """Loops whose condition is a literal ``eslint:recommended`` rejects.

    ``no-constant-condition`` defaults to ``checkLoops: "allExceptWhileTrue"``:
    every loop with a constant condition fails it except ``while (true)``. So
    this counts a ``for`` or ``do`` with either literal and a ``while (false)``,
    which is the set the fold must not leave behind. The engine finishes the
    true ones and most false ones, :func:`remove_dead_loops` what it declines
    of the rest; what is still left is counted here, and Gate 1 refuses when
    the count grows.
    """
    count = 0
    for node in _walk(_parse(source, language)):
        if node.type not in ("for_statement", "do_statement", "while_statement"):
            continue
        literal = _condition_literal(node)
        if literal is None:
            continue
        if node.type == "while_statement" and literal == "true":
            continue
        count += 1
    return count


def constant_condition_ifs(source: str, language: str) -> int:
    """``if`` statements whose condition is a boolean literal.

    The engine folds every one it makes, except a dead branch that declares a
    ``var``: the name is scoped to the function, so deleting the branch could
    break a read after the ``if``, and the fold declines. What it leaves is the
    flag half-removed and ``no-constant-condition`` failing, so Gate 1 refuses
    when this count grows.
    """
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.type == "if_statement" and _condition_literal(node) is not None
    )


def _condition_literal(loop: Node) -> str | None:
    condition = loop.child_by_field_name("condition")
    while condition is not None and condition.type == "parenthesized_expression":
        inner = [c for c in condition.named_children if c.type != "comment"]
        condition = inner[0] if len(inner) == 1 else None
    if condition is None or condition.type not in ("true", "false"):
        return None
    return condition.type


def _dead(source: str, language: str) -> list[Node]:
    return [
        node
        for node in _walk(_parse(source, language))
        if node.type == "for_statement" and _condition_literal(node) == "false"
    ]


def _remove_dead_loops(after: str, language: str) -> tuple[str, list[str]]:
    data = after.encode("utf-8")
    # One loop per round: a replacement moves every offset after it, and a
    # dead loop can sit inside another one.
    for loop in _dead(after, language):
        replacement = _replacement(loop, data)
        if replacement is None:
            continue
        removed = data[loop.start_byte : loop.end_byte].decode("utf-8")
        if replacement:
            data = data[: loop.start_byte] + replacement + data[loop.end_byte :]
        else:
            start, end = _whole_lines(data, loop.start_byte, loop.end_byte)
            data = data[:start] + data[end:]
        rest, more = _remove_dead_loops(data.decode("utf-8"), language)
        return rest, [removed, *more]
    return after, []


def _replacement(loop: Node, data: bytes) -> bytes | None:
    """What ``loop`` becomes: ``b""`` to delete it, or None to leave it standing."""
    if loop.parent is None or loop.parent.type not in _STATEMENT_LISTS:
        return None
    return _for_replacement(loop, data)


def _for_replacement(loop: Node, data: bytes) -> bytes | None:
    """``for (init; false; update)`` keeps exactly what runs: its initialiser.

    What the engine leaves: a ``let``/``const`` with more than one declarator
    or a value that is not a plain literal or name, and an expression it
    declines (``await``, ``using``, or a start ASI could join to the line
    above). Every ``let``/``const`` value that cannot do anything leaves
    nothing, the binding being scoped to the loop; one holding a call is
    declined, since as a statement it would leak its binding into the
    enclosing block. An expression stays as an expression statement, and
    Gate 1 judges the result. No initialiser and ``var`` are the engine's
    alone, so they are declined here, which Gate 1 refuses. So is a body that
    declares a ``var``: the name is scoped to the function, and a read after
    the loop would throw once the declaration is gone.
    """
    initializer = loop.child_by_field_name("initializer")
    if initializer is None or initializer.type in ("empty_statement", "variable_declaration"):
        return None
    body = loop.child_by_field_name("body")
    if body is not None and _declares_var(body):
        return None
    text = data[initializer.start_byte : initializer.end_byte]
    if initializer.type == "lexical_declaration":
        values = [
            value
            for declarator in initializer.named_children
            if declarator.type == "variable_declarator"
            and (value := declarator.child_by_field_name("value")) is not None
        ]
        return b"" if all(_cannot_run_code(v) for v in values) else None
    return text + b";"


def _declares_var(node: Node) -> bool:
    """A ``var`` anywhere under ``node``, ``for (var k in o)`` included.

    A ``var`` in a nested function counts too, which over-refuses: the safe
    direction, and the same scan the engine's dead-branch rules make.
    """
    return any(
        inner.type == "variable_declaration"
        or (
            inner.type == "for_in_statement"
            and (kind := inner.child_by_field_name("kind")) is not None
            and kind.type == "var"
        )
        for inner in _walk(node)
    )


def _cannot_run_code(value: Node) -> bool:
    """``unused``'s inertness, without property reads: a getter runs code.

    ``unused`` accepts a member read as inert, which is the trade it makes for
    a binding that stays declared. Here the whole initialiser is deleted, so
    ``obj.size`` must count as a call that might do something.
    """
    return _subtree_is_inert(value, "ts") and not any(
        node.type in ("member_expression", "subscript_expression") for node in _walk(value)
    )


def _whole_lines(data: bytes, start: int, end: int) -> tuple[int, int]:
    """The span widened to its lines when nothing else shares them."""
    line_start = data.rfind(b"\n", 0, start) + 1
    newline = data.find(b"\n", end)
    line_end = len(data) if newline == -1 else newline + 1
    if data[line_start:start].strip() or data[end:line_end].strip():
        return start, end
    return line_start, line_end


def _unused_labels(source: str, language: str) -> list[Node]:
    """``labeled_statement`` nodes whose label no jump in their body names.

    A same-named label nested inside counts as a use: a jump under it binds to
    the inner one, and telling the two apart is not worth the risk of deleting
    a label something still targets.
    """
    unused: list[Node] = []
    for node in _walk(_parse(source, language)):
        if node.type != "labeled_statement":
            continue
        label = node.child_by_field_name("label")
        body = node.child_by_field_name("body")
        if label is None or body is None:
            continue
        if not any(
            inner.type in ("break_statement", "continue_statement", "labeled_statement")
            and (named := inner.child_by_field_name("label")) is not None
            and named.text == label.text
            for inner in _walk(body)
        ):
            unused.append(node)
    return unused


def _remove_unused_labels(before: str, after: str, language: str) -> str:
    if after == before or _unused_labels(before, language):
        return after
    data = after.encode("utf-8")
    for node in sorted(_unused_labels(after, language), key=lambda n: -n.start_byte):
        colon = next((c for c in node.children if c.type == ":"), None)
        if colon is None:  # pragma: no cover - `label:` always carries one
            continue
        # The label, its colon and the whitespace after them: the statement
        # then starts where the label did, on its line or on the next.
        end = colon.end_byte
        while end < len(data) and data[end : end + 1] in (b" ", b"\t", b"\n", b"\r"):
            end += 1
        data = data[: node.start_byte] + data[end:]
    return data.decode("utf-8")
