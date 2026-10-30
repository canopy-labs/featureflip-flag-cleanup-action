"""Remove a function parameter the fold made the same literal at every call.

The positional twin of :mod:`flag_cleanup.ts_constant_props`::

    label(item, compactRows)   // before
    label(item, true)          // after the fold, at every call of `label`

The parameter now carries no information. :func:`inline_constant_params`
deletes the argument at every call and the parameter from the declaration, and
writes ``<marker>(true)`` at each read, so ``rules/ts.toml``'s marker rule
creates the literal inside the engine's cascade and what it guarded folds as a
read written inline would.

Which parameters qualify, and why:

* **The transform made it a literal.** Summed over the calls, the output
  passes the literal at that position more often than the input did. A
  literal the customer already wrote at every call is theirs, and stays.
* **The function is module-private.** A top-level ``function`` (no overload
  signatures) or a ``const`` bound to a function or arrow function, in a file
  that is a module, not exported, and spelled nowhere but its declaration
  (and a function expression's own name) and as the callee of a plain call
  ``f(…)``. Anything else (``export { f }``, ``f.call``, ``items.map(f)``) is
  a call this pass cannot see.
* **Every call passes the literal at that position, spreading nothing.**
* **The parameter and every one before it are plain** (a name, no default,
  not optional, not a rest), so the position means the same thing at every
  call. It is bound once in the function, and every read of it is an ordinary
  expression; a function reading ``arguments`` or calling ``eval`` refuses.
* **Each deleted argument and parameter has its line to itself, or shares it
  only with its neighbours in the same list.**

A refusal leaves the literal argument standing, which is what the run
produced before this pass existed.
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup.ts_constant_props import (
    _FUNCTIONS,
    _is_module,
    _item_span,
    _references,
    _text,
)
from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

_LITERALS = frozenset({"true", "false"})
_NAMES = frozenset(
    {
        "identifier",
        "type_identifier",
        "property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
    }
)

Edit = tuple[int, int, bytes]


def inline_constant_params(before: str, after: str, language: str, marker: str) -> str:
    """Remove one parameter the transform made one literal at every call.

    ``before`` is the customer's file and ``after`` the current result.
    Returns ``after`` unchanged when nothing qualifies (see the module
    docstring), and when ``after`` already holds ``marker``.
    """
    if after == before or marker in after:
        return after
    root = _parse(after, language)
    if root.has_error or not _is_module(root):
        return after
    old_root = _parse(before, language)
    if old_root.has_error:
        return after
    data, old_data = after.encode("utf-8"), before.encode("utf-8")
    for name, function, allowed in _functions(root, data):
        calls = _calls(root, data, name, allowed)
        if not calls:
            continue
        old_calls = _plain_calls(old_root, old_data, name)
        edits = _plan(function, calls, old_calls, data, old_data, marker)
        if edits is None:
            continue
        for start, end, text in sorted(edits, reverse=True):
            data = data[:start] + text + data[end:]
        return data.decode("utf-8")
    return after


def _functions(root: Node, data: bytes) -> list[tuple[str, Node, set[int]]]:
    """Each unexported top-level function: its name, node and the ids of the
    name nodes that declare it."""
    found: dict[str, list[tuple[Node, Node]]] = {}
    for statement in root.named_children:
        if statement.type in ("function_declaration", "function_signature"):
            name = statement.child_by_field_name("name")
            if name is not None:
                found.setdefault(_text(name, data), []).append((name, statement))
        elif statement.type == "lexical_declaration":
            declarators = [d for d in statement.named_children if d.type == "variable_declarator"]
            for declarator in declarators:
                name = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name is None or name.type != "identifier":
                    continue
                if (
                    len(declarators) == 1
                    and declarator.child_by_field_name("type") is None
                    and value is not None
                    and value.type in _FUNCTIONS
                ):
                    found.setdefault(_text(name, data), []).append((name, value))
                else:
                    found.setdefault(_text(name, data), []).append((name, declarator))
    functions = []
    for name, declared in found.items():
        if len(declared) != 1:
            continue
        name_node, function = declared[0]
        if function.type not in ("function_declaration", *_FUNCTIONS):
            continue
        allowed = {name_node.id}
        if function.type == "function_expression":
            inner = function.child_by_field_name("name")
            if inner is not None:
                allowed.add(inner.id)
        functions.append((name, function, allowed))
    return functions


def _plain_calls(root: Node, data: bytes, name: str) -> list[Node]:
    """Every ``name(…)`` call's argument list."""
    return [
        arguments
        for node in _walk(root)
        if node.type == "call_expression"
        and (callee := node.child_by_field_name("function")) is not None
        and callee.type == "identifier"
        and _text(callee, data) == name
        and (arguments := node.child_by_field_name("arguments")) is not None
        and arguments.type == "arguments"
    ]


def _calls(root: Node, data: bytes, name: str, allowed: set[int]) -> list[Node] | None:
    """The argument list of every call of ``name``, or None when the name is
    spelled anywhere but its declaration and a plain call's callee."""
    calls = _plain_calls(root, data, name)
    callees = {a.parent.child_by_field_name("function").id for a in calls}
    for node in _walk(root):
        if node.type in _NAMES and _text(node, data) == name:
            if node.id not in allowed and node.id not in callees:
                return None
    return calls


def _items(arguments: Node) -> list[Node]:
    return [c for c in arguments.named_children if c.type != "comment"]


def _plan(function, calls, old_calls, data, old_data, marker) -> list[Edit] | None:
    if any(n.type == "identifier" and n.text in (b"eval", b"arguments") for n in _walk(function)):
        return None
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return None
    params = _items(parameters)
    if any(c.type == "spread_element" for a in calls for c in _items(a)):
        return None
    for index, parameter in enumerate(params):
        if not _plain(parameter):
            # A later position no longer means the same thing at every call.
            return None
        literal = _literal_at(calls, index, data)
        if literal is None:
            continue
        passed = len(calls)
        earlier = sum(1 for a in old_calls if _argument_literal(a, index, old_data) == literal)
        if passed <= earlier:
            continue
        name = _text(parameter.child_by_field_name("pattern") or parameter, data)
        if sum(1 for n in binding_nodes(function) if _text(n, data) == name) != 1:
            continue
        body = function.child_by_field_name("body")
        if body is None:
            continue
        references = _references(body, data, name)
        if references is None:
            continue
        edits: list[Edit] = []
        for arguments in calls:
            span = _list_item_span(_items(arguments)[index], _items(arguments), data)
            if span is None:
                edits = None
                break
            edits.append((*span, b""))
        if edits is None:
            continue
        span = _list_item_span(parameter, params, data)
        if span is None:
            continue
        edits.append((*span, b""))
        replacement = f"{marker}({literal})".encode("utf-8")
        edits.extend((r.start_byte, r.end_byte, replacement) for r in references)
        ordered = sorted(edits)
        if any(s < e for (_, e, _), (s, _, _) in zip(ordered, ordered[1:])):
            continue
        return edits
    return None


def _plain(parameter: Node) -> bool:
    """A required parameter that is a bare name with no default."""
    if parameter.type == "identifier":  # JavaScript's bare formal parameter
        return True
    if parameter.type != "required_parameter":
        return False
    pattern = parameter.child_by_field_name("pattern")
    return (
        pattern is not None
        and pattern.type == "identifier"
        and parameter.child_by_field_name("value") is None
    )


def _argument_literal(arguments: Node, index: int, data: bytes) -> str | None:
    items = _items(arguments)
    if len(items) <= index or items[index].type not in _LITERALS:
        return None
    return _text(items[index], data)


def _literal_at(calls: list[Node], index: int, data: bytes) -> str | None:
    literals = {_argument_literal(a, index, data) for a in calls}
    if len(literals) != 1 or None in literals:
        return None
    return literals.pop()


def _list_item_span(node: Node, items: list[Node], data: bytes) -> tuple[int, int] | None:
    """The bytes to delete to remove ``node`` from its list."""
    if len(items) == 1:
        line_start = data.rfind(b"\n", 0, node.start_byte) + 1
        newline = data.find(b"\n", node.end_byte)
        line = data[line_start : len(data) if newline == -1 else newline]
        if b"//" in line or b"/*" in line:
            return None
        if node.start_point[0] != node.end_point[0]:
            return None
        return node.start_byte, node.end_byte
    return _item_span(node, data, separator=b",")


def drop_unread_params(before: str, after: str, language: str) -> str:
    """Remove a parameter of a module-private function the run left unread.

    The positional twin of :func:`flag_cleanup.ts_unread_props.drop_unread_props`:
    ``formatRow(name, owner)`` whose only read of ``owner`` the fold deleted.
    The function qualifies as for :func:`inline_constant_params`; the
    parameter was read in the customer's file and is read nowhere now, is
    bound once in the function before and after, and every call passes it
    a value that can only read (a literal, a name, a property access off a
    name). Every parameter before it is plain, so its position is the same at
    every call; a call that passes fewer arguments needs no edit there.
    """
    from flag_cleanup.ctor_param_ts import _inert

    if after == before:
        return after
    root = _parse(after, language)
    if root.has_error or not _is_module(root):
        return after
    old_root = _parse(before, language)
    if old_root.has_error:
        return after
    data, old_data = after.encode("utf-8"), before.encode("utf-8")
    old_functions = {name: function for name, function, _ in _functions(old_root, old_data)}
    for name, function, allowed in _functions(root, data):
        old_function = old_functions.get(name)
        calls = _calls(root, data, name, allowed)
        if old_function is None or calls is None:
            continue
        if any(n.type == "identifier" and n.text in (b"eval", b"arguments") for n in _walk(function)):
            continue
        if any(c.type == "spread_element" for a in calls for c in _items(a)):
            continue
        parameters = function.child_by_field_name("parameters")
        params = _items(parameters) if parameters is not None else []
        for index, parameter in enumerate(params):
            if not _plain(parameter):
                break
            pattern = parameter.child_by_field_name("pattern") or parameter
            prop = _text(pattern, data)
            if _reads(function, data, prop) != 0 or _reads(old_function, old_data, prop) < 1:
                continue
            if not all(
                sum(1 for n in binding_nodes(f) if _text(n, d) == prop) == 1
                for f, d in ((function, data), (old_function, old_data))
            ):
                continue
            edits: list[Edit] | None = []
            for arguments in calls:
                items = _items(arguments)
                if len(items) <= index:
                    continue
                if not _inert(items[index]):
                    edits = None
                    break
                span = _list_item_span(items[index], items, data)
                if span is None:
                    edits = None
                    break
                edits.append((*span, b""))
            if edits is None:
                continue
            span = _list_item_span(parameter, params, data)
            if span is None:
                continue
            edits.append((*span, b""))
            for start, end, text in sorted(edits, reverse=True):
                data = data[:start] + text + data[end:]
            return data.decode("utf-8")
    return after


def _reads(function: Node, data: bytes, name: str) -> int:
    """How many times the function's body spells ``name`` as a value."""
    body = function.child_by_field_name("body")
    if body is None:
        return -1
    return sum(
        1
        for n in _walk(body)
        if n.type in ("identifier", "shorthand_property_identifier") and _text(n, data) == name
    )
