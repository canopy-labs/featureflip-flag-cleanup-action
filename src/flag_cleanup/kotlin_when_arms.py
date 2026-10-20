"""Count the literal arms a fold leaves in a subject-less Kotlin ``when``.

Kotlin's multi-way guard is ``when`` written with NO subject::

    return when {
        isAdmin(ctx)                                     -> "admin"
        client.boolVariation("old-checkout", ctx, false) -> "legacy"
        else                                             -> "new"
    }

Each condition is evaluated in order and the first true one wins, which makes
it an ``if``/``else if`` chain by another spelling. The engine folds it itself:
a ``false ->`` arm is deleted, a leading ``true ->`` arm collapses the ``when``
to its body where the body can stand in its place, and any other ``true ->``
arm becomes the terminal ``else`` with every arm below it gone, which also
keeps a ``when`` used as a value exhaustive.

It declines whatever it cannot prove safe -- an arm listing several conditions
(``isAdmin(ctx), false ->``), where the literal is one alternative rather than
the whole condition, or a ``when`` whose only arm is dead but which is not a
statement of a block (a lambda's last statement is its VALUE). What it leaves
is residue, and residue must be refused rather than shipped: the flag key is
gone from the file, so no later run would look again. :func:`literal_arms` is
that count, for the residue gate in `syntax.py`.

A ``when (subject) { true -> … }`` compares a VALUE and is ordinary customer
code, so its arms are never counted. Its SUBJECT is, by :func:`literal_subjects`:
``when (<read>) { true -> …; false -> … }`` folds to ``when (true) { … }``,
which the engine leaves standing.

The grammar is the wheel-installed ``tree-sitter-kotlin`` -- the one Gate 1's
Kotlin profile parses with. In it a boolean literal parses as a plain
``identifier``, so a literal is recognised by TEXT, which is unambiguous only
because ``true``/``false`` are hard keywords that cannot name anything.
"""

from __future__ import annotations

import tree_sitter_kotlin
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_kotlin.language())
_PARSER = Parser(_LANGUAGE)


def literal_arms(source: str) -> int:
    """How many subject-less ``when`` arms in ``source`` carry a boolean literal.

    An arm counts when ANY of its conditions is a literal, not only a sole one:
    ``isAdmin(ctx), false ->`` is an arm the engine declines, and counting it is
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


def literal_subjects(source: str) -> int:
    """How many ``when`` expressions in ``source`` switch on a boolean literal.

    The value is the subject's last named child, so ``when (val on = true)``
    counts as well as ``when (true)``.
    """
    root = _PARSER.parse(source.encode("utf-8")).root_node
    return sum(
        1
        for node in _iter(root)
        if node.type == "when_subject"
        and node.named_child_count
        and _boolean(node.named_children[-1]) is not None
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


def _boolean(node: Node) -> bool | None:
    while node.type == "parenthesized_expression" and node.named_child_count == 1:
        node = node.named_children[0]
    if node.type != "identifier":
        return None
    text = node.text.decode("utf-8")
    return True if text == "true" else False if text == "false" else None
