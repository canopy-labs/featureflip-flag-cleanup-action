"""Evaluate a Jest/Vitest ``expect(<literal>)`` assertion the fold produced.

A flag read inside ``expect`` folds to a boolean, whatever the matcher asserts:
``expect(useFlag("k")).toBeUndefined()`` becomes ``expect(true).toBeUndefined()``.
The trivial-assertion rules delete the passing ones (``expect(true).toBe(true)``);
this module finds the failing ones, which would ship as a red test.

Only a fixed matcher set is evaluated, so a verdict is never a guess:
``toBe``, ``toEqual`` and ``toStrictEqual`` with ``true``, ``false``,
``undefined``, ``null`` or ``void 0``; ``toBeTruthy``, ``toBeFalsy``,
``toBeDefined``, ``toBeUndefined`` and ``toBeNull``, each optionally after
``.not``. Anything else, or an ``expect`` that is not a whole statement, has no
verdict.
"""

from __future__ import annotations

from collections import Counter

from tree_sitter import Node

from flag_cleanup import ts_syntax

_WRAPPERS = frozenset({"parenthesized_expression", "as_expression", "non_null_expression", "satisfies_expression"})
_VALUE_MATCHERS = frozenset({"toBe", "toEqual", "toStrictEqual"})
_NULLARY = frozenset({"toBeTruthy", "toBeFalsy", "toBeDefined", "toBeUndefined", "toBeNull"})


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _unwrap(node: Node) -> Node:
    while node.type in _WRAPPERS and node.named_children:
        node = node.named_children[0]
    return node


def _callee_name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    return _text(function) if function is not None and function.type == "identifier" else None


def literal(node: Node | None) -> tuple[bool, object]:
    """``(known, value)`` of a literal an assertion compares with: a bool, or
    the strings ``"undefined"`` / ``"null"``."""
    if node is None:
        return False, None
    text = " ".join(_text(_unwrap(node)).split())
    if text == "true":
        return True, True
    if text == "false":
        return True, False
    if text in ("undefined", "void 0"):
        return True, "undefined"
    if text == "null":
        return True, "null"
    return False, None


def holds(matcher: str, argument: Node | None, value: bool) -> bool | None:
    """Whether ``expect(<value>).<matcher>(argument)`` passes; None when it
    cannot be said."""
    if matcher == "toBeTruthy":
        return value
    if matcher == "toBeFalsy":
        return not value
    if matcher in ("toBeUndefined", "toBeNull"):
        return False
    if matcher == "toBeDefined":
        return True
    if matcher in _VALUE_MATCHERS:
        known, expected = literal(argument)
        return (expected is value) if known else None  # identity: bools
    return None


def evaluate_expectation(expect: Node, value: bool) -> bool | None:
    """Whether the statement ``expect(<x>)[.not].<matcher>(<literal>?)`` passes
    when ``<x>`` is ``value``; None when ``expect`` is not exactly the subject
    of such a statement, or its matcher is not one this evaluates."""
    chain = expect.parent
    negated = False
    while chain is not None and chain.type == "member_expression":
        label = chain.child_by_field_name("property")
        if label is not None and _text(label) == "not" and chain.parent is not None and chain.parent.type == "member_expression":
            negated = not negated
            chain = chain.parent
            continue
        break
    if chain is None or chain.type != "member_expression":
        return None
    matcher_call = chain.parent
    if matcher_call is None or matcher_call.type != "call_expression" or matcher_call.child_by_field_name("function") != chain:
        return None
    statement = matcher_call.parent
    if statement is not None and statement.type == "await_expression":
        statement = statement.parent
    if statement is None or statement.type != "expression_statement":
        return None
    label = chain.child_by_field_name("property")
    call_arguments = matcher_call.child_by_field_name("arguments")
    named = call_arguments.named_children if call_arguments is not None else []
    if label is None or len(named) > 1:
        return None
    method = _text(label)
    if method not in _VALUE_MATCHERS and method not in _NULLARY:
        return None
    if (method in _NULLARY) != (not named):
        return None
    held = holds(method, named[0] if named else None, value)
    return None if held is None else held != negated


def _failing(source: str, language: str) -> Counter[str]:
    """Each ``expect(true|false)…`` statement that cannot pass, by its text."""
    found: Counter[str] = Counter()
    if "expect" not in source:
        return found
    root = ts_syntax._parse(source, language)
    for node in ts_syntax._walk(root):
        if node.type != "call_expression" or _callee_name(node) != "expect":
            continue
        arguments = node.child_by_field_name("arguments")
        if arguments is None or len(arguments.named_children) != 1:
            continue
        known, value = literal(arguments.named_children[0])
        if not known or not isinstance(value, bool):
            continue
        if evaluate_expectation(node, value) is False:
            statement = node
            while statement.parent is not None and statement.parent.type != "expression_statement":
                statement = statement.parent
            found[" ".join(_text(statement.parent or statement).split())] += 1
    return found


def failing(before: str, after: str, language: str) -> list[str]:
    """The assertions ``after`` holds, more than ``before`` did, that can only
    fail: ``expect(true)`` / ``expect(false)`` with a matcher the literal
    cannot satisfy. One the customer already wrote is in ``before`` too."""
    if before == after or language not in ("ts", "tsx", "js"):
        return []
    old = _failing(before, language)
    return [
        text
        for text, count in _failing(after, language).items()
        for _ in range(count - old.get(text, 0))
    ]
