"""Go ``if`` initializers that went with code the flag kept from running.

Gate 1 counts Go's ``if`` initializers before and after the run and refuses
when the count falls (see ``_lost_side_effects`` in :mod:`flag_cleanup.syntax`):
``if v := prime(); <read> { … }`` runs ``prime()`` whatever the flag says, and
folding that ``if`` away loses the call. A whole-file count cannot tell that
initializer from one inside code the fold deleted because it never ran:
``if err := run(); err != nil {`` in the removed branch, or in the tail after a
kept ``return``. Those went with their code, and that is the point of the
removal.

:func:`dead_code_initializers` counts the second kind from the engine's own
edits, which Gate 1 then excuses. Each edit replaces a matched node with its
replacement, and the initializers in the first and not the second went with
that edit. One of them is NOT excused: the matched node's OWN initializer,
when the node is an ``if`` the edit folded rather than an unreachable statement
it deleted. That is the ``if v := prime(); <read>`` shape, and it is what the
count is for. Anything else that lowers the count (an edit made outside the
engine, a match the parse below cannot read) is not excused either, so the
count still refuses it.
"""

from __future__ import annotations

from collections.abc import Iterable

from tree_sitter import Node

from flag_cleanup.syntax import _parse, _walk

#: The engine's Go rules that delete a statement because nothing can reach it.
#: Its own initializer never runs, so it goes with the statement. Any other
#: rule that matches an ``if`` folds it, and must keep the initializer.
_UNREACHABLE_RULES = frozenset(
    {
        "delete_statement_after_return",
        "delete_statement_after_returning_block",
        "delete_statement_after_terminating_switch",
    }
)

#: The engine's Go rules that drop an ``if`` initializer binding a boolean
#: literal, after inlining it (``if on := true; on {`` is ``if true {``). The
#: literal ran nothing, so its initializer is excused like dead code.
_INLINED_LITERAL_RULES = frozenset({"delete_if_initializer_declaration"})


def dead_code_initializers(edits: Iterable[tuple[str, str, str]]) -> int:
    """How many ``if`` initializers the engine's ``(rule, matched, replacement)``
    edits removed along with code that could not run."""
    total = 0
    for rule, matched, replacement in edits:
        if rule in _INLINED_LITERAL_RULES:
            total += 1
            continue
        removed = _initializers(matched) - _initializers(replacement)
        if rule not in _UNREACHABLE_RULES and _own_initializer(matched):
            removed -= 1
        total += max(0, removed)
    return total


def _tree(fragment: str) -> Node:
    """``fragment`` parsed as statements of a function body, or as the
    clauses of a ``switch`` when it starts with one."""
    text = fragment.strip()
    if text.startswith(("case ", "case\t", "default:", "default ")):
        text = f"switch {{\n{text}\n}}"
    return _parse(f"package p\n\nfunc _() {{\n{text}\n}}\n", "go")


def _initializers(fragment: str) -> int:
    if "if" not in fragment:
        return 0
    return sum(
        1
        for node in _walk(_tree(fragment))
        if node.type == "if_statement" and node.child_by_field_name("initializer") is not None
    )


def _own_initializer(matched: str) -> bool:
    """Whether the matched node is itself an ``if`` with an initializer. Any
    doubt answers yes, which excuses less."""
    text = matched.lstrip()
    if not text.startswith("if"):
        return False
    for node in _walk(_tree(matched)):
        if node.type == "if_statement":
            return node.child_by_field_name("initializer") is not None or node.has_error
    return True
