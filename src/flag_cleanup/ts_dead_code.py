"""Dead code the TypeScript family's fold leaves behind that its lint rejects.

The fold folds an expression and the cascade folds the ``if`` around it, but a
flag read can sit in positions where what is left over is a construct, not an
expression, and ``eslint:recommended`` (or ``tsc``) fails each one:

* ``for (init; false; update) …`` — ``no-constant-condition``. The loop never
  runs its body, but its INITIALISER does, so the loop is replaced by whatever
  of it runs rather than deleted outright.
* ``do { … } while (false);`` — ``no-constant-condition``. The body runs
  exactly once, so the loop becomes its body, unless a ``break`` or
  ``continue`` in the body jumps to the ``do`` itself: unwrapped, that jump
  would bind to whatever encloses it instead.
* ``outer: …`` whose last ``break outer``/``continue outer`` the fold removed —
  ``no-unused-labels``, and ``tsc`` reports TS7028. The label goes, the
  statement it labelled stays.
* ``static { }`` that the fold emptied — ``no-empty-static-block``. The block
  goes.

The true treatment's ``for (; true; )`` and ``do … while (true)`` are the
engine's to rewrite, and are not here: those are folds of a condition, these are
deletions of a construct, and only this side can see the whole file to decide
them.

Every pass is DIFFERENTIAL in the way :func:`flag_cleanup.syntax.remove_dead_loops`
is: nothing is touched when the input already had one of the shapes, because
then the fold's cannot be told from the customer's. What a pass declines is
counted by :func:`constant_condition_loops`, which Gate 1 refuses on, so a loop
this cannot finish is a refusal rather than a pull request whose lint fails.
The loops go in the stranded-statement window, beside the ``while (false)``
deletion; the labels and blocks after the reindent pass (see
:func:`remove_orphans`).
"""

from __future__ import annotations

import re

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk
from flag_cleanup.unused import _subtree_is_inert

#: Loops a ``continue`` binds to. A ``switch`` also takes an unlabelled
#: ``break``, which is the only difference between the two jumps here.
_LOOPS = frozenset(
    {"for_statement", "for_in_statement", "while_statement", "do_statement"}
)

#: Statements whose scope is the block they are declared in. Splicing one of
#: them out of the ``do``'s body would move it into the enclosing scope, where
#: it can collide with a binding of the same name, so such a body keeps its
#: braces.
_BLOCK_SCOPED = frozenset(
    {
        "lexical_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "function_declaration",
        "generator_function_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "module",
        "internal_module",
    }
)

#: Parents a statement may be removed from or spliced into: each holds a list
#: of statements, so one fewer (or several more) is always valid there.
_STATEMENT_LISTS = frozenset({"statement_block", "program"})


def remove_dead_loops(before: str, after: str, language: str) -> tuple[str, list[str]]:
    """Replace each dead ``for``/``do`` loop the fold made with what of it runs.

    Returns the text of every loop removed, for the pull request body, in the
    same list :func:`flag_cleanup.syntax.remove_dead_loops` feeds for
    ``while (false)``. That pass leaves these two alone: a ``for`` has an
    initialiser that runs and a ``do`` a body that runs, so neither is plain
    dead code.
    """
    if after == before or _dead(before, language):
        return after, []
    return _remove_dead_loops(after, language)


def remove_orphans(before: str, after: str, language: str) -> str:
    """Drop each label and ``static {}`` block the earlier passes left with nothing to do.

    Run AFTER the reindent pass. Both edits change a line the reindent pass
    reads as a folded header, and run before it they shifted the next lines out
    a level. Neither is listed in the pull request body: a label nothing jumps
    to and an empty block are nothing the customer could want kept.
    """
    after = _remove_unused_labels(before, after, language)
    return _remove_empty_static_blocks(before, after, language)


def constant_condition_loops(source: str, language: str) -> int:
    """Loops whose condition is a literal ``eslint:recommended`` rejects.

    ``no-constant-condition`` defaults to ``checkLoops: "allExceptWhileTrue"``:
    every loop with a constant condition fails it except ``while (true)``. So
    this counts a ``for`` or ``do`` with either literal and a ``while (false)``,
    which is the set the fold must not leave behind. The engine finishes the
    true ones and :func:`remove_dead_loops` the false ones; what either declines is counted
    here, and Gate 1 refuses when the count grows.
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
        if node.type in ("for_statement", "do_statement")
        and _condition_literal(node) == "false"
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
    if loop.type == "for_statement":
        return _for_replacement(loop, data)
    return _do_replacement(loop, data)


def _for_replacement(loop: Node, data: bytes) -> bytes | None:
    """``for (init; false; update)`` keeps exactly what runs: its initialiser.

    No initialiser, or a ``let``/``const`` whose values cannot do anything,
    leaves nothing: the binding is scoped to the loop. A ``var`` declaration
    stays as a statement, because it is hoisted and its value is visible after
    the loop; an expression stays as an expression statement. A ``let`` or
    ``const`` holding a call is declined: as a statement it would leak its
    binding into the enclosing block.
    """
    initializer = loop.child_by_field_name("initializer")
    if initializer is None or initializer.type == "empty_statement":
        return b""
    text = data[initializer.start_byte : initializer.end_byte]
    if initializer.type == "lexical_declaration":
        values = [
            value
            for declarator in initializer.named_children
            if declarator.type == "variable_declarator"
            and (value := declarator.child_by_field_name("value")) is not None
        ]
        return b"" if all(_cannot_run_code(v) for v in values) else None
    if initializer.type == "variable_declaration":
        return text
    return text + b";"


def _cannot_run_code(value: Node) -> bool:
    """``unused``'s inertness, without property reads: a getter runs code.

    ``unused`` accepts a member read as inert, which is the trade it makes for
    a binding that stays declared. Here the whole initialiser is deleted, so
    ``obj.size`` must count as a call that might do something.
    """
    return _subtree_is_inert(value, "ts") and not any(
        node.type in ("member_expression", "subscript_expression") for node in _walk(value)
    )


def _do_replacement(loop: Node, data: bytes) -> bytes | None:
    """``do { … } while (false);`` becomes its body, which runs once either way."""
    body = loop.child_by_field_name("body")
    if body is None or body.type != "statement_block" or _jumps_to(loop, body):
        return None
    statements = [c for c in body.named_children]
    if not statements:
        return b""
    if any(_scoped_to_the_block(c, data) for c in statements) or _holds_a_multiline_token(body):
        return data[body.start_byte : body.end_byte]
    return _dedented(data, statements[0].start_byte, statements[-1].end_byte, loop)


#: ``using r = open();`` disposes ``r`` when its block ends, so it must keep its
#: block. This grammar parses it as an expression statement, so its node type
#: says nothing; the leading keyword does.
_USING = re.compile(rb"(?:await\s+)?using\s")


def _scoped_to_the_block(statement: Node, data: bytes) -> bool:
    return statement.type in _BLOCK_SCOPED or bool(
        _USING.match(data, statement.start_byte)
    )


#: Tokens whose text may run across lines. Re-indenting a line inside one
#: changes the program: the whitespace is part of a string's value.
_MULTILINE_TOKENS = frozenset({"template_string", "string", "regex"})


def _holds_a_multiline_token(body: Node) -> bool:
    """Whether moving ``body`` out a level would re-indent the inside of a token.

    The body then keeps its braces, which needs no re-indenting at all.
    """
    return any(
        node.type in _MULTILINE_TOKENS and node.start_point[0] != node.end_point[0]
        for node in _walk(body)
    )


def _jumps_to(loop: Node, body: Node) -> bool:
    """Whether a ``break`` or ``continue`` in ``body`` targets ``loop``.

    Only an UNLABELLED jump can: the loop sits directly in a statement list
    (see :func:`_replacement`), so it has no label of its own, and a labelled
    jump in its body names some enclosing statement. An unlabelled ``break``
    binds to the nearest loop or ``switch``, an unlabelled ``continue`` to the
    nearest loop. A function inside the body needs no stop of its own: a jump
    in it that reached no loop of its own would not parse.
    """
    for node in _walk(body):
        if node.type not in ("break_statement", "continue_statement"):
            continue
        if node.child_by_field_name("label") is not None:
            continue
        stops = _LOOPS | ({"switch_statement"} if node.type == "break_statement" else set())
        enclosing = node.parent
        while enclosing is not None and enclosing.type not in stops:
            enclosing = enclosing.parent
        if enclosing is not None and enclosing.id == loop.id:
            return True
    return False


def _dedented(data: bytes, start: int, end: int, loop: Node) -> bytes:
    """``data[start:end]`` moved out one level, to the ``do``'s own column."""
    base = _indent(data, loop.start_byte)
    inner = _indent(data, start)
    extra = inner[len(base) :] if inner.startswith(base) else b""
    lines = data[start:end].split(b"\n")
    out = lines[:1]
    for line in lines[1:]:
        prefix = base + extra
        out.append(base + line[len(prefix) :] if line.startswith(prefix) else line)
    return b"\n".join(out)


def _indent(data: bytes, position: int) -> bytes:
    start = data.rfind(b"\n", 0, position) + 1
    prefix = data[start:position]
    return prefix[: len(prefix) - len(prefix.lstrip())]


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


def _empty_static_blocks(source: str, language: str) -> list[Node]:
    return [
        node
        for node in _walk(_parse(source, language))
        if node.type == "class_static_block"
        and (body := node.child_by_field_name("body")) is not None
        and body.named_child_count == 0
    ]


def _remove_empty_static_blocks(before: str, after: str, language: str) -> str:
    if after == before or _empty_static_blocks(before, language):
        return after
    data = after.encode("utf-8")
    for node in sorted(_empty_static_blocks(after, language), key=lambda n: -n.start_byte):
        start, end = _whole_lines(data, node.start_byte, node.end_byte)
        # The blank line that set the block apart goes with it when the class
        # body closes right after, or it would be left standing before `}`.
        if (
            start != node.start_byte
            and data[end:].lstrip(b" \t").startswith(b"}")
            and data[:start].endswith((b"\n\n", b"\n\r\n"))
        ):
            start -= 1 if data[:start].endswith(b"\n\n") else 2
        data = data[:start] + data[end:]
    return data.decode("utf-8")
