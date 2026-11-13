"""Find a call the run kept only as a discard (`_ = f(x)`), so the PR can say so.

A Go local whose last reader was the flag read goes with the fold. When its
initializer can have a side effect (any call), it is not deleted: it is
rewritten to `_ = <initializer>`, so whatever the call does still runs. That is
the only sound choice without knowing what the callee does, and very often the
callee does nothing a reviewer wants kept: a getter that fetched the SDK client,
or built the evaluation context, for the read that just went. The rewrite is
correct either way, but a reviewer reading the diff has to notice each one to
decide, so they are named in the pull request.

Found as a differential over the run's output, like the failing assertions: a
discard the customer's file already held is theirs and is not reported, and it
does not matter which layer wrote the new one.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache

import tree_sitter_go
from tree_sitter import Language, Node, Parser

#: The ``bindings`` kind of a call the run kept running as a discard.
DISCARDED_CALL = "discarded call"

#: The ``bindings`` kind of a receiver or argument of a folded read that calls
#: something, kept as ``_ = <expr>`` before the statement (``go_read_effects``).
HOISTED_CALL = "hoisted call"


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_go.language()))


def _holds_call(node: Node) -> bool:
    if node.type == "call_expression":
        return True
    # A function literal's body does not run where it is written.
    if node.type == "func_literal":
        return False
    return any(_holds_call(child) for child in node.named_children)


def _discard_value(node: Node) -> Node | None:
    """The value ``node`` discards, when it is `_ = v` (a statement, or a spec
    in a `var` declaration) with one blank target and one value."""
    if node.type == "assignment_statement":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        operator = node.child_by_field_name("operator")
        if operator is not None and operator.type != "=":
            return None
    elif node.type == "var_spec":
        names = node.children_by_field_name("name")
        if len(names) != 1 or node.child_by_field_name("type") is not None:
            return None
        left, right = names[0], node.child_by_field_name("value")
    else:
        return None
    if left is None or right is None:
        return None
    targets = left.named_children if left.type == "expression_list" else [left]
    values = right.named_children if right.type == "expression_list" else [right]
    if len(targets) != 1 or len(values) != 1:
        return None
    if targets[0].type != "identifier" or targets[0].text != b"_":
        return None
    return values[0]


def _discards(source: str) -> list[str]:
    """Each `_ = <value holding a call>` in ``source``, as normalized text, in
    source order."""
    found: list[str] = []
    stack = [_parser().parse(source.encode("utf-8")).root_node]
    while stack:
        node = stack.pop()
        value = _discard_value(node)
        if value is not None and _holds_call(value):
            found.append((node.start_byte, " ".join(node.text.decode("utf-8", "replace").split())))
            continue
        stack.extend(node.named_children)
    return [text for _start, text in sorted(found)]


def introduced(before: str, after: str) -> list[str]:
    """Each discard ``after`` holds more of than ``before`` did, in order."""
    extra = Counter(_discards(after)) - Counter(_discards(before))
    rows: list[str] = []
    for text in _discards(after):
        if extra[text] > 0:
            rows.append(text)
            extra[text] -= 1
    return rows
