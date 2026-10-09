"""Fold the literal arms Piranha leaves behind in a subject-less Kotlin ``when``.

Kotlin's multi-way guard is ``when`` written with NO subject::

    return when {
        isAdmin(ctx)                                     -> "admin"
        client.boolVariation("old-checkout", ctx, false) -> "legacy"
        else                                             -> "new"
    }

Each condition is evaluated in order and the first true one wins, which makes
it an ``if``/``else if`` chain by another spelling — the same construct as Go's
tagless ``switch`` and Ruby's subject-less ``case`` (see :mod:`flag_cleanup.go_fold`
for why no Piranha rule can finish any of them). The engine's built-ins fold
the ``if`` family only, so the read became ``true ->``/``false ->`` and nothing
else moved: a dead arm, or a dead chain below an arm that always matches, left
standing with the flag key gone from the file.

**Kotlin is shaped like Ruby, not like PHP.** A ``when`` arm cannot fall into
the next one, and ``break``/``continue`` bind to the enclosing loop, never to
the ``when``, so every arm is an independent edit and lifting a body out of a
``when`` cannot change which construct a jump leaves. What Kotlin adds is
EXHAUSTIVENESS: a ``when`` used as an expression must end in ``else`` or it
does not compile. Every rewrite here keeps that true:

* **false, any position** — delete the arm. An ``else`` below it survives, so
  an exhaustive ``when`` stays exhaustive. A ``when`` whose only arm goes is a
  statement by construction (it had no ``else``), and goes with it — but only
  where it is unmistakably a statement, as a direct child of a block.
* **true, first arm** — the ``when`` always takes it, so it collapses to that
  arm's body, where that is expressible without changing meaning:

  - an expression body replaces the ``when`` only in a slot that takes any
    whole expression (an initializer, a ``return``, an argument, a statement).
    As an OPERAND — ``1 + when { … }``, ``when { … }.length`` — splicing
    ``a ?: b`` would rebind to the operator around it, so that position is
    promoted instead;
  - a braced body is spliced only in statement position, and only when it
    declares nothing: a ``val`` lifted out of the arm's block into the
    enclosing one can collide with, or silently shadow, a name the rest of
    the function uses. In expression position a brace pair is a LAMBDA, so
    lifting the block there would change the value's type outright.

  Anything else takes the next rewrite, which is correct in every position.
* **true, not first** (or a first arm that cannot be spliced) — the arms ABOVE
  still take precedence, so the arm becomes the terminal ``else`` and every arm
  below it goes, a pre-existing ``else`` included. Splicing here would be WRONG
  rather than untidy: an input an earlier condition claimed would start taking
  the flag's branch. Promoting also MAKES the ``when`` exhaustive, which is what
  keeps an expression-position ``when`` compiling.

**What is declined**, and left to `syntax.py`'s residue gate to refuse:

* an ``else`` above the flag's arm (Kotlin rejects a misplaced ``else``, and
  promoting would emit a second one);
* an arm listing several conditions (``a, true ->``), where the literal is one
  alternative rather than the whole condition;
* an arm sharing its line with another (``when { true -> 1; else -> 2 }``),
  where a whole-line cut would take a neighbour with it.

A ``when (subject) { true -> … }`` compares a VALUE and is ordinary customer
code; nothing here touches a ``when`` that has a subject.

The grammar is the wheel-installed ``tree-sitter-kotlin`` — the one Gate 1's
Kotlin profile parses with — not the engine's bundled one. In it a boolean
literal parses as a plain ``identifier``, so a literal is recognised by TEXT,
which is unambiguous only because ``true``/``false`` are hard keywords that
cannot name anything.
"""

from __future__ import annotations

import tree_sitter_kotlin
from tree_sitter import Language, Node, Parser

from flag_cleanup.guard_fold import (
    dedented,
    fold_to_fixpoint,
    line_indent,
    own_lines,
    replace,
)

_LANGUAGE = Language(tree_sitter_kotlin.language())
_PARSER = Parser(_LANGUAGE)

_COMMENTS = frozenset({"line_comment", "block_comment"})

#: Parents in which a ``when`` holds a whole expression, so replacing it with
#: any other expression cannot rebind an operator around it. Deliberately a
#: short allow-list: a position missing here costs a splice (the arm is
#: promoted to ``else`` instead, which is always correct), never correctness.
_WHOLE_EXPRESSION_SLOTS = frozenset(
    {
        "block",
        "lambda_literal",
        "function_body",
        "property_declaration",
        "assignment",
        "return_expression",
        "value_argument",
        "parenthesized_expression",
    }
)

#: Parents in which a ``when`` is a statement among statements, the only place
#: a braced body's statements can be lifted to.
_STATEMENT_SLOTS = frozenset({"block", "lambda_literal"})

#: Top-level statements that bind a name in the block holding them. Lifting
#: one out of an arm's block widens its scope, which can collide with or shadow
#: a name the enclosing block already uses.
_DECLARATIONS = frozenset(
    {
        "property_declaration",
        "function_declaration",
        "class_declaration",
        "object_declaration",
        "type_alias",
    }
)


def fold_literal_whens(before: str, after: str) -> str:
    """Fold every subject-less ``when`` arm the transform left with a literal."""
    return fold_to_fixpoint(
        before, after, literal_arms=literal_arms, fold_once=_fold_once
    )


def literal_arms(source: str) -> int:
    """How many subject-less ``when`` arms in ``source`` carry a boolean literal.

    Also what `syntax.py` counts for Kotlin's residue gate, so the shapes this
    module declines and the shapes the gate refuses are one definition.

    An arm counts when ANY of its conditions is a literal, not only a sole one:
    ``isAdmin(ctx), false ->`` is an arm the fold declines, and counting it is
    what makes declining it a refusal rather than a silent residue.
    """
    root = _PARSER.parse(source.encode("utf-8")).root_node
    return sum(
        1
        for node in _iter(root)
        if _is_subjectless(node)
        for entry in _entries(node)
        if any(_boolean(condition) is not None for condition in _conditions(entry))
    )


def _iter(node: Node):
    yield node
    for child in node.children:
        yield from _iter(child)


def _is_subjectless(node: Node) -> bool:
    return node.type == "when_expression" and not any(
        child.type == "when_subject" for child in node.children
    )


def _entries(when: Node) -> list[Node]:
    return [child for child in when.named_children if child.type == "when_entry"]


def _conditions(entry: Node) -> list[Node]:
    return [
        child
        for index, child in enumerate(entry.children)
        if entry.field_name_for_child(index) == "condition"
    ]


def _is_else(entry: Node) -> bool:
    return any(child.type == "else" for child in entry.children)


def _literal(entry: Node) -> bool | None:
    """``True``/``False`` if this arm's sole condition is a boolean literal."""
    conditions = _conditions(entry)
    return _boolean(conditions[0]) if len(conditions) == 1 else None


def _boolean(node: Node) -> bool | None:
    while node.type == "parenthesized_expression" and node.named_child_count == 1:
        node = node.named_children[0]
    if node.type != "identifier":
        return None
    text = node.text.decode("utf-8")
    return True if text == "true" else False if text == "false" else None


def _arrow(entry: Node) -> Node | None:
    return next((child for child in entry.children if child.type == "->"), None)


def _body(entry: Node) -> Node | None:
    """The arm's body: the first non-comment named child after its ``->``."""
    arrow = _arrow(entry)
    if arrow is None:
        return None
    return next(
        (
            child
            for child in entry.named_children
            if child.start_byte >= arrow.end_byte and child.type not in _COMMENTS
        ),
        None,
    )


def _extent_end(source: str, entry: Node, body: Node) -> int:
    """Where the arm ends for editing: its body, plus a comment on that line.

    This grammar attaches every comment up to the next arm to the PREVIOUS one,
    including a comment on a line of its own that a reader would take as the
    next arm's heading. Ending at the body keeps that comment where it is.
    """
    end = body.end_byte
    line_end = source.find("\n", end)
    for child in entry.named_children:
        if (
            child.type in _COMMENTS
            and child.start_byte >= end
            and (line_end == -1 or child.start_byte < line_end)
        ):
            end = child.end_byte
    return end


def _fold_once(source: str) -> str | None:
    root = _PARSER.parse(source.encode("utf-8")).root_node
    for when in _iter(root):
        if not _is_subjectless(when):
            continue
        entries = _entries(when)
        for index, entry in enumerate(entries):
            verdict = _literal(entry)
            if verdict is None:
                continue
            rewritten = (
                _delete_arm(source, when, entry, entries)
                if verdict is False
                else _fold_true_arm(source, when, entry, entries, index)
            )
            if rewritten is not None:
                return rewritten
    return None


def _delete_arm(
    source: str, when: Node, entry: Node, entries: list[Node]
) -> str | None:
    """Drop an arm that can never match."""
    if len(entries) == 1:
        # No `else`, so this `when` was a statement; with no arm left it does
        # nothing at all. Removed only as a direct child of a block -- a
        # lambda's last statement is its VALUE, and dropping it would change
        # the lambda's type.
        if when.parent is None or when.parent.type != "block":
            return None
        span = own_lines(source, when.start_byte, when.end_byte)
    else:
        body = _body(entry)
        if body is None:
            return None
        span = own_lines(source, entry.start_byte, _extent_end(source, entry, body))
    if span is None:
        return None
    return replace(source, *span, "")


def _fold_true_arm(
    source: str, when: Node, entry: Node, entries: list[Node], index: int
) -> str | None:
    """An arm that always matches: collapse to it, or make it the ``else``."""
    if any(_is_else(other) for other in entries[:index]):
        return None
    body = _body(entry)
    if body is None:
        return None
    if index == 0:
        spliced = _splice(source, when, body)
        if spliced is not None:
            return spliced
    return _promote_to_else(source, entry, body, entries, index)


def _splice(source: str, when: Node, body: Node) -> str | None:
    """Replace the whole ``when`` with ``body``, or ``None`` where that is unsafe."""
    parent = when.parent.type if when.parent is not None else None
    base = line_indent(source, when.start_byte)
    if body.type != "block":
        if parent not in _WHOLE_EXPRESSION_SLOTS:
            return None
        text = dedented(source, body.start_byte, body.end_byte, base)
        return replace(source, when.start_byte, when.end_byte, text)
    if parent not in _STATEMENT_SLOTS:
        return None
    if own_lines(source, when.start_byte, when.end_byte) is None:
        return None
    statements = body.named_children
    if any(statement.type in _DECLARATIONS for statement in statements):
        return None
    if not statements:
        start, end = own_lines(source, when.start_byte, when.end_byte)
        return replace(source, start, end, "")
    text = dedented(source, statements[0].start_byte, statements[-1].end_byte, base)
    return replace(source, when.start_byte, when.end_byte, text)


def _promote_to_else(
    source: str, entry: Node, body: Node, entries: list[Node], index: int
) -> str | None:
    """Make ``entry`` the terminal ``else`` and drop every arm below it.

    Only the condition is replaced, so the body keeps the indentation it already
    had. Resuming after the LAST arm keeps the closing brace and its
    indentation, which sit outside every arm node.
    """
    arrow = _arrow(entry)
    if arrow is None:  # pragma: no cover - `_body` already required one
        return None
    promoted = source[: entry.start_byte] + "else " + source[arrow.start_byte :]
    if index == len(entries) - 1:
        return promoted
    shift = len("else ") - (arrow.start_byte - entry.start_byte)
    end = _extent_end(source, entry, body) + shift
    tail = entries[-1].end_byte + shift
    return promoted[:end] + promoted[tail:]
