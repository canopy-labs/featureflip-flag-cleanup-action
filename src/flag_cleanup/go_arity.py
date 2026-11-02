"""A Go flag read that returned several values, folded to one literal.

Several Go SDKs read a boolean flag through a call that returns two values,
``(bool, error)``: ``on, err := client.BoolVariation("KEY", ctx, false)``, or
``return client.BoolVariation(…)`` from a function with two results. A single
literal cannot stand in for that call: ``on, err := true`` and ``return true``
from such a function are ``assignment mismatch`` / ``not enough return values``
to the compiler. The result still parses, so nothing else in the run sees it.

Gate 1 refuses such a file (see :func:`refusal_reason`). It asks the question
Go asks of the result: does every statement that gives values to several names,
or returns from a function with several results, get as many values as it
needs? One expression may supply several values only when it is a call, a map
index (``v, ok := m[k]``), a type assertion (``v, ok := x.(T)``) or a channel
receive (``v, ok := <-ch``). Anything else on its own there, a folded literal
included, is a count Go rejects. The question is asked of the customer's source
too, per function, and only a mismatch the run added is the run's doing.
"""

from __future__ import annotations

from collections import Counter

from tree_sitter import Node

from flag_cleanup.syntax import _parse, _walk

_FUNCTIONS = frozenset({"function_declaration", "method_declaration", "func_literal"})
#: The expressions that may give several values on their own.
_MULTI_VALUED = frozenset({"call_expression", "index_expression", "type_assertion_expression"})


def refusal_reason(before: str, after: str) -> str | None:
    """Why the run's result ``after`` of ``before`` would not build: a
    statement it left with fewer values than names or results. ``None`` when
    it left none."""
    after_found = _mismatches(after)
    gained = Counter(after_found) - Counter(_mismatches(before))
    if not gained:
        return None
    functions = ", ".join(dict.fromkeys(f for f in after_found if gained[f] > 0))
    return (
        f"the flag is read in {functions} through a call that returns several "
        "values (`on, err := client.BoolVariation(…)`, or a `return` of it from "
        "a function with several results); one literal cannot stand in for it, "
        "and Go does not build the result (`assignment mismatch`)"
    )


def _mismatches(source: str) -> list[str]:
    """The functions holding a statement that gives one single-valued
    expression to several names or results, once per statement, in source
    order."""
    root = _parse(source, "go")
    found: list[tuple[int, str]] = []
    for node in _walk(root):
        if node.type in ("short_var_declaration", "assignment_statement"):
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if _count(left) >= 2 and _single_valued(right):
                found.append((node.start_byte, _function_name(node)))
        elif node.type == "var_spec":
            names = [c for c in node.children_by_field_name("name")]
            if len(names) >= 2 and _single_valued(node.child_by_field_name("value")):
                found.append((node.start_byte, _function_name(node)))
        elif node.type == "return_statement":
            values = next((c for c in node.named_children if c.type == "expression_list"), None)
            if values is not None and _single_valued(values) and _results(node) >= 2:
                found.append((node.start_byte, _function_name(node)))
    return [name for _, name in sorted(found)]


def _expressions(values: Node | None) -> list[Node]:
    if values is None:
        return []
    return [c for c in values.named_children if c.type != "comment"]


def _count(values: Node | None) -> int:
    return len(_expressions(values))


def _single_valued(values: Node | None) -> bool:
    """Whether ``values`` is one expression that can only ever be one value."""
    expressions = _expressions(values)
    if len(expressions) != 1:
        return False
    expression = expressions[0]
    while expression.type == "parenthesized_expression":
        inner = [c for c in expression.named_children if c.type != "comment"]
        if len(inner) != 1:
            return False
        expression = inner[0]
    if expression.type in _MULTI_VALUED:
        return False
    if expression.type == "unary_expression":
        operator = expression.child_by_field_name("operator")
        return operator is None or operator.type != "<-"
    return True


def _results(node: Node) -> int:
    """How many results the function ``node`` returns from declares."""
    function = node.parent
    while function is not None and function.type not in _FUNCTIONS:
        function = function.parent
    result = None if function is None else function.child_by_field_name("result")
    if result is None:
        return 0
    if result.type != "parameter_list":
        return 1
    total = 0
    for declaration in result.named_children:
        if declaration.type not in ("parameter_declaration", "variadic_parameter_declaration"):
            continue
        total += max(1, len(declaration.children_by_field_name("name")))
    return total


def _function_name(node: Node) -> str:
    """The named function or method around ``node``, as the customer would
    look for it: a line number would be the rewritten file's."""
    parent = node.parent
    while parent is not None and parent.type not in ("function_declaration", "method_declaration"):
        parent = parent.parent
    name = parent.child_by_field_name("name") if parent is not None else None
    return "a function" if name is None else f"`{name.text.decode('utf-8', 'replace')}`"
