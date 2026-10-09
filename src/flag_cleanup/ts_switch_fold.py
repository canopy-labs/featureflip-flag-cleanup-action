"""Fold the literal ``case`` arms Piranha leaves behind in ``switch (true)``.

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

The same construct as Go's tagless ``switch``, Ruby's subject-less ``case`` and
PHP's own ``switch (true)`` — see :mod:`flag_cleanup.go_fold` for why no Piranha
rule can finish any of them. The engine's built-ins fold the ``if`` family
only, so the read became ``case true:``/``case false:`` and nothing else moved.

**This module mirrors PHP's design, not Go's, because the semantics are PHP's.**
A JavaScript ``case`` FALLS THROUGH BY DEFAULT: an arm whose body does not end
in a jump runs straight into the next arm's body, whatever that arm's
condition. Nothing in the source marks it, so deleting a dead arm below a
non-terminating one makes the arm above fall into a DIFFERENT body, and
promoting an arm that does not terminate drops the bodies it fell into. The
fold therefore requires every arm but the last to end in an unambiguous jump,
which makes each arm an independent edit; any other ``switch (true)`` is
declined and left to the residue gate, which refuses it.

**``throw`` IS counted here, which is where this departs from PHP.** PHP's
grammar parses ``throw`` and ``die()`` as a plain expression statement,
indistinguishable from an assignment, so PHP's fold cannot count them. This
grammar gives ``throw`` its own ``throw_statement`` node — measured, in the TS
and TSX grammars alike — and a ``throw`` cannot fall through. ``process.exit()``
and other calls that never return are NOT counted: they are calls like any
other, and only the type checker knows they do not return.

A body ending in a braced block counts when the block itself ends in a jump:
``case a: { …; break; }`` is the idiomatic way to scope a ``const`` to one arm.

**Why this never splices.** A body typically ends in ``break;``, which outside
a switch is a syntax error at best — and inside an enclosing loop it is valid
and silently becomes a LOOP break. Promoting the arm to ``default:`` keeps
every jump inside the switch and is correct in every position, so it is the
only true-treatment rewrite, exactly as in PHP:

* **false, any position** — delete the arm.
* **true, any position** — the arm becomes ``default:`` and every arm below it
  goes; arms above keep their precedence. A ``default:`` ABOVE the flag's arm is
  declined: promoting would emit a second one, which does not parse.

A ``switch (value) { case true: … }`` compares a value and is ordinary customer
code; nothing here touches a switch whose subject is anything but ``true``.
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup.guard_fold import fold_to_fixpoint, own_lines, replace
from flag_cleanup.ts_syntax import _parse

#: Statements after which control cannot reach the next arm.
_TERMINATORS = frozenset(
    {"return_statement", "break_statement", "continue_statement", "throw_statement"}
)

_ARMS = frozenset({"switch_case", "switch_default"})


def fold_switch_true(before: str, after: str, language: str) -> str:
    """Fold every ``switch (true)`` arm the transform left with a literal value."""
    return fold_to_fixpoint(
        before,
        after,
        literal_arms=lambda source: literal_arms(source, language),
        fold_once=lambda source: _fold_once(source, language),
    )


def literal_arms(source: str, language: str) -> int:
    """How many ``switch (true)`` arms in ``source`` carry a boolean literal.

    Also what `syntax.py` counts for the TypeScript family's residue gate, so
    the shapes this module declines and the shapes the gate refuses are one
    definition.
    """
    return sum(
        1
        for node in _iter(_parse(source, language))
        if _is_true_switch(node)
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


def _is_true_switch(node: Node) -> bool:
    if node.type != "switch_statement":
        return False
    value = node.child_by_field_name("value")
    return value is not None and _unparenthesised(value).type == "true"


def _arms(switch: Node) -> list[Node]:
    body = switch.child_by_field_name("body")
    if body is None:
        return []
    return [child for child in body.named_children if child.type in _ARMS]


def _literal(arm: Node) -> bool | None:
    if arm.type != "switch_case":
        return None
    value = arm.child_by_field_name("value")
    if value is None:
        return None
    kind = _unparenthesised(value).type
    return True if kind == "true" else False if kind == "false" else None


def _statements(node: Node, field: str | None) -> list[Node]:
    return [
        child
        for index, child in enumerate(node.children)
        if child.is_named
        and child.type != "comment"
        and (field is None or node.field_name_for_child(index) == field)
    ]


def _ends_in_jump(statements: list[Node]) -> bool:
    if not statements:
        return False
    last = statements[-1]
    if last.type == "statement_block":
        return _ends_in_jump(_statements(last, None))
    return last.type in _TERMINATORS


def _arm_terminates(arm: Node) -> bool:
    return _ends_in_jump(_statements(arm, "body"))


def _fold_once(source: str, language: str) -> str | None:
    for switch in _iter(_parse(source, language)):
        if not _is_true_switch(switch):
            continue
        arms = _arms(switch)
        if not arms or not all(_arm_terminates(arm) for arm in arms[:-1]):
            continue
        for index, arm in enumerate(arms):
            verdict = _literal(arm)
            if verdict is None:
                continue
            rewritten = (
                _delete_arm(source, switch, arm, arms)
                if verdict is False
                else _promote_to_default(source, arm, arms, index)
            )
            if rewritten is not None:
                return rewritten
    return None


def _delete_arm(source: str, switch: Node, arm: Node, arms: list[Node]) -> str | None:
    """Drop an arm that can never match.

    When it was the only arm the whole statement goes: ``switch (true) {}``
    evaluates a constant and does nothing. Only as a direct child of a block,
    since a labelled or unbraced position would be left holding nothing.
    """
    if len(arms) == 1:
        if switch.parent is None or switch.parent.type not in (
            "statement_block",
            "program",
        ):
            return None
        span = own_lines(source, switch.start_byte, switch.end_byte)
    else:
        span = own_lines(source, arm.start_byte, arm.end_byte)
    if span is None:
        return None
    return replace(source, *span, "")


def _promote_to_default(
    source: str, arm: Node, arms: list[Node], index: int
) -> str | None:
    """Make ``arm`` the ``default:`` and drop every arm below it."""
    if any(other.type == "switch_default" for other in arms[:index]):
        return None
    colon = next((c for c in arm.children if c.type == ":"), None)
    if colon is None:  # pragma: no cover - `case <value>:` always carries one
        return None
    promoted = source[: arm.start_byte] + "default:" + source[colon.end_byte :]
    if index == len(arms) - 1:
        return promoted
    shift = len("default:") - (colon.end_byte - arm.start_byte)
    return promoted[: arm.end_byte + shift] + promoted[arms[-1].end_byte + shift :]
