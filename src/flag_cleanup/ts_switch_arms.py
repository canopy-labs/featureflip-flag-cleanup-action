"""Count the literal ``case`` arms a fold leaves in a TypeScript-family ``switch``.

TypeScript and JavaScript have no subject-less ``switch``, so the multi-way
guard is spelled ``switch (true)``: each ``case`` value is compared against
``true`` with ``===``, so each is evaluated as a condition and the first one
that holds wins::

    switch (true) {
      case isAdmin(ctx):
        return "admin";
      case client.boolVariation("old-checkout", ctx, false):
        return "legacy";
      default:
        return "new";
    }

A read can also be the SUBJECT -- ``switch (<read>) { case true: … }`` -- and
then folding it leaves ``switch (true)`` or ``switch (false)``.

The engine folds both shapes itself: a ``case`` whose value can never match is
deleted, and one that always matches becomes ``default:`` with every arm below
it gone. It declines whatever it cannot prove safe -- a JavaScript ``case``
FALLS THROUGH BY DEFAULT, so an arm above the flag's that does not end in a
jump, a ``default:`` above it, or a declaration that would change scope all
leave the literal arm in place.

What it leaves is residue, and residue must be refused rather than shipped:
``case true:`` under ``switch (false)`` does not even type-check (TS2678).
:func:`literal_arms` is that count, for the residue gate in `syntax.py`.

A ``switch (value) { case true: … }`` compares a value and is ordinary customer
code; nothing here counts a switch whose subject is not a boolean literal.
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse

_ARMS = frozenset({"switch_case", "switch_default"})


def literal_arms(source: str, language: str) -> int:
    """How many arms of a ``switch (true)``/``switch (false)`` carry a boolean literal."""
    return sum(
        1
        for node in _iter(_parse(source, language))
        if _subject(node) is not None
        for arm in _arms(node)
        if _literal(arm) is not None
    )


def _iter(node: Node):
    yield node
    for child in node.children:
        yield from _iter(child)


def _unparenthesised(node: Node) -> Node:
    while node.type == "parenthesized_expression" and node.named_child_count == 1:
        node = node.named_children[0]
    return node


def _subject(node: Node) -> bool | None:
    """The boolean literal ``node`` switches on, or None for any other node."""
    if node.type != "switch_statement":
        return None
    value = node.child_by_field_name("value")
    return None if value is None else _boolean(value)


def _boolean(node: Node) -> bool | None:
    kind = _unparenthesised(node).type
    return True if kind == "true" else False if kind == "false" else None


def _arms(switch: Node) -> list[Node]:
    body = switch.child_by_field_name("body")
    if body is None:
        return []
    return [child for child in body.named_children if child.type in _ARMS]


def _literal(arm: Node) -> bool | None:
    if arm.type != "switch_case":
        return None
    value = arm.child_by_field_name("value")
    return None if value is None else _boolean(value)
