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

import json
import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup.ts_constant_props import (
    _FUNCTIONS,
    _is_module,
    _item_span,
    _references,
    _text,
)
from flag_cleanup.ts_tests import is_test_file
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
        spliced = _splice(data, edits, language)
        if spliced is None:
            continue
        return spliced
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
    """The bytes to delete to remove ``node`` from its list.

    A lone item takes its trailing comma and the whitespace around it with
    it, so ``f(\n  true,\n)`` becomes ``f()`` and never ``f(\n  ,\n)``.
    """
    if len(items) == 1:
        line_start = data.rfind(b"\n", 0, node.start_byte) + 1
        newline = data.find(b"\n", node.end_byte)
        line = data[line_start : len(data) if newline == -1 else newline]
        if b"//" in line or b"/*" in line:
            return None
        if node.start_point[0] != node.end_point[0]:
            return None
        parent = node.parent
        if parent is None or parent.child_count < 2:
            return None
        opening, closing = parent.children[0], parent.children[-1]
        if opening.type != "(" or closing.type != ")":
            return node.start_byte, node.end_byte
        before = data[opening.end_byte : node.start_byte]
        after = data[node.end_byte : closing.start_byte]
        if before.strip() or after.strip(b" \t\r\n,") or after.count(b",") > 1:
            return None
        return opening.end_byte, closing.start_byte
    return _item_span(node, data, separator=b",")


def _orphan_comma(root: Node) -> bool:
    """Whether an argument or parameter list holds a ``,`` with no item before
    it (``f(\n  ,\n)``). tree-sitter parses that without an error, so a splice
    that leaves one would be written to disk unseen."""
    for node in _walk(root):
        if node.type not in ("arguments", "formal_parameters"):
            continue
        previous = None
        for child in node.children:
            if child.type == "comment":
                continue
            if child.type == "," and (previous is None or previous.type in ("(", ",")):
                return True
            previous = child
    return False


def _splice(data: bytes, edits: list[Edit], language: str) -> str | None:
    """``data`` with ``edits`` applied, or None when the result would not parse
    or would hold an orphaned comma."""
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    try:
        result = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    root = _parse(result, language)
    if root.has_error or _orphan_comma(root):
        return None
    return result


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
            spliced = _splice(data, edits, language)
            if spliced is None:
                continue
            return spliced
    return after


def drop_empty_object_params(
    before: str,
    after: str,
    language: str,
    path: str | None = None,
    repo_dir: str | None = None,
) -> str:
    """Remove a helper's last parameter when every call passes ``{}`` to it.

    The case the literal inliner above cannot reach: removing a flag turns the
    override a test helper took (``renderWithFlags({ 'flag': true })``) into
    ``renderWithFlags({})``, and the parameter then carries nothing. Each
    qualifying parameter loses its argument at every call and its declaration,
    and each read, which can only be a JSX attribute value (``flags={x}``),
    becomes ``{}`` (``flags={{}}``). That leaves the literal where
    :mod:`flag_cleanup.ts_default_arguments` and
    :mod:`flag_cleanup.ts_test_providers` look for it, so both run on it.

    Which parameters qualify, and why:

    * **The function is private to a scope.** A ``function`` or a ``const``
      bound to a function, declared directly in a module or in a block (a
      helper inside ``describe``), whose name is spelled in that scope only at
      its declaration and as the callee of a plain call. A sibling block may
      declare the same name; each is judged on its own.
    * **It is the last parameter**, a required one with a plain name, and its
      default, when it has one, is ``{}``.
    * **Every call passes ``{}``** (or omits the argument, which needs the
      ``= {}`` default), spreading nothing.
    * **Its declared type proves it, or the run made it so.** ``Record<string,
      never>`` admits only ``{}``, so the parameter is empty whoever wrote it.
      Any other type needs the run's own hand in it: the output passes ``{}``
      more often than the customer's file did.
    * **Every read is a JSX attribute's whole value**, so ``{}`` stands in for
      the name where a component only reads it. With one read outside any
      nested function that is all it takes. With several, or one inside a
      closure (``wrapper: ({ children }) => <P flags={x} />``), ``{}`` would
      be a fresh object per read or per render, so each must be an attribute
      :func:`flag_cleanup.ts_default_arguments._attribute_span` proves it can
      leave out as the component's own ``{}`` default; the component then
      makes the object, and its identity is the component's choice. That
      proof needs ``path`` and ``repo_dir`` to follow an imported component.
    * The element a read goes to has no type arguments and is not a generic
      component declared in the file (``{}`` would change what ``T`` infers).
    * **The file is a test file** (it declares a titled ``it``/``test``/
      ``describe``). Production code may hand the same object to a context
      provider, where a fresh ``{}`` per render is a behaviour change.
    * **The edit parses and leaves no stray comma** in any list it touched.
    """
    if after == before:
        return after
    root = _parse(after, language)
    if root.has_error or not _is_module(root):
        return after
    old_root = _parse(before, language)
    if old_root.has_error:
        return after
    if not is_test_file(after, language):
        return after
    data, old_data = after.encode("utf-8"), before.encode("utf-8")
    accepted: list[Edit] = []
    for name, function, allowed, scope in _scoped_functions(root, data):
        planned = _plan_empty(function, name, allowed, scope, old_root, data, old_data)
        if planned is None:
            continue
        planned, shared = planned
        candidate = _splice(data, planned, language)
        if candidate is None:
            continue
        if shared and not _all_left_out(planned, candidate, language, path, repo_dir):
            continue
        spans = sorted((s, e) for s, e, _ in planned)
        taken = sorted((s, e) for s, e, _ in accepted)
        if any(s < te and ts < e for s, e in spans for ts, te in taken):
            continue
        accepted.extend(planned)
    if not accepted:
        return after
    return _splice(data, accepted, language) or after


def _scoped_functions(root: Node, data: bytes) -> list[tuple[str, Node, set[int], Node]]:
    """Each function or function-valued ``const`` declared directly in a module
    or a block: its name, node, declaring name ids and the scope it is in."""
    found = []
    for scope in _walk(root):
        if scope.type not in ("program", "statement_block"):
            continue
        for statement in scope.named_children:
            if statement.type == "function_declaration":
                name = statement.child_by_field_name("name")
                if name is not None:
                    found.append((_text(name, data), statement, {name.id}, scope))
            elif statement.type == "lexical_declaration":
                declarators = [d for d in statement.named_children if d.type == "variable_declarator"]
                if len(declarators) != 1:
                    continue
                name = declarators[0].child_by_field_name("name")
                value = declarators[0].child_by_field_name("value")
                if (
                    name is not None
                    and name.type == "identifier"
                    and declarators[0].child_by_field_name("type") is None
                    and value is not None
                    and value.type in _FUNCTIONS
                ):
                    allowed = {name.id}
                    inner = value.child_by_field_name("name")
                    if inner is not None:
                        allowed.add(inner.id)
                    found.append((_text(name, data), value, allowed, scope))
    return found


def _empty_object(node: Node) -> bool:
    return node.type == "object" and not node.named_children


def _is_empty_record(annotation: Node | None, data: bytes) -> bool:
    """Whether a parameter's annotation is ``Record<string, never>``."""
    if annotation is None:
        return False
    written = _text(annotation, data).lstrip(":")
    return "".join(written.split()) == "Record<string,never>"


_NESTED = _FUNCTIONS | frozenset(
    {
        "function_declaration",
        "generator_function",
        "generator_function_declaration",
        "method_definition",
        "class_declaration",
        "class",
    }
)


def _generic_element(root: Node, data: bytes, element: Node) -> bool:
    """Whether the JSX ``element`` has type arguments or names a component
    declared in this file with type parameters."""
    if element.child_by_field_name("type_arguments") is not None:
        return True
    name = element.child_by_field_name("name")
    if name is None:
        return True
    written = _text(name, data)
    for node in _walk(root):
        if node.type in ("function_declaration", "class_declaration", "generator_function_declaration"):
            declared = node.child_by_field_name("name")
            if declared is not None and _text(declared, data) == written:
                if node.child_by_field_name("type_parameters") is not None:
                    return True
        elif node.type == "variable_declarator":
            declared = node.child_by_field_name("name")
            if declared is not None and _text(declared, data) == written:
                value = node.child_by_field_name("value")
                if value is None or value.type not in _FUNCTIONS:
                    # `const Picker: Generic = …`, `memo(function <T>…)`: unknown.
                    if any(n.child_by_field_name("type_parameters") is not None for n in _walk(node)):
                        return True
                elif value.child_by_field_name("type_parameters") is not None:
                    return True
    return False


def _plan_empty(function, name, allowed, scope, old_root, data, old_data) -> tuple[list[Edit], bool] | None:
    if any(n.type == "identifier" and n.text in (b"eval", b"arguments") for n in _walk(function)):
        return None
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return None
    params = _items(parameters)
    # A `this` parameter is not an argument, so it takes no index.
    positional = [p for p in params if not _is_this(p)]
    if not positional:
        return None
    index = len(positional) - 1
    parameter = positional[index]
    if parameter is not params[-1]:
        return None
    pattern = parameter.child_by_field_name("pattern")
    if parameter.type != "required_parameter" or pattern is None or pattern.type != "identifier":
        return None
    default = parameter.child_by_field_name("value")
    if default is not None and not _empty_object(default):
        return None
    param = _text(pattern, data)
    if sum(1 for n in binding_nodes(function) if _text(n, data) == param) != 1:
        return None
    body = function.child_by_field_name("body")
    if body is None:
        return None
    references = _references(body, data, param)
    if not references:
        return None
    shared = len(references) > 1
    root = _tree_root(scope)
    for reference in references:
        holder = reference.parent
        if (
            holder is None
            or holder.type != "jsx_expression"
            or len(holder.named_children) != 1
            or holder.parent is None
            or holder.parent.type != "jsx_attribute"
        ):
            return None
        ancestor = reference.parent
        while ancestor is not None and ancestor.id != function.id:
            # Up to the function itself: a curried `(flags) => ({ children }) => …`
            # has the inner arrow AS its body.
            if ancestor.type in _NESTED:
                shared = True
            ancestor = ancestor.parent
        element = holder.parent.parent
        if element is None or _generic_element(root, data, element):
            return None
    calls = _calls(scope, data, name, allowed)
    if not calls:
        return None
    if any(c.type == "spread_element" for a in calls for c in _items(a)):
        return None
    explicit = 0
    for arguments in calls:
        items = _items(arguments)
        if len(items) > index + 1:
            return None
        if len(items) == index + 1:
            if not _empty_object(items[index]):
                return None
            explicit += 1
        elif default is None:
            return None
    if not _is_empty_record(parameter.child_by_field_name("type"), data):
        old = sum(
            1
            for a in _plain_calls(old_root, old_data, name)
            if len(_items(a)) > index and _empty_object(_items(a)[index])
        )
        if explicit <= old:
            return None
    edits: list[Edit] = []
    for arguments in calls:
        items = _items(arguments)
        if len(items) == index + 1:
            span = _list_item_span(items[index], items, data)
            if span is None:
                return None
            edits.append((*span, b""))
    span = _list_item_span(parameter, params, data)
    if span is None:
        return None
    edits.append((*span, b""))
    edits.extend((r.start_byte, r.end_byte, b"{}") for r in references)
    ordered = sorted(edits)
    if any(s < e for (_, e, _), (s, _, _) in zip(ordered, ordered[1:])):
        return None
    return edits, shared


#: The components outside the checkout whose handling of a prop is audited:
#: ``(package, exported name) -> the prop``. The React SDK's test provider
#: builds a new client from ``flags`` on every render, so the identity of the
#: object it is handed changes nothing.
_AUDITED = {("@featureflip/react", "TestFeatureflipProvider"): "flags"}

#: Wrappers that change an expression's type and nothing at run time.
_TYPE_ONLY = frozenset(
    {"as_expression", "satisfies_expression", "non_null_expression", "parenthesized_expression"}
)

_MAX_FORWARDS = 4


def _all_left_out(edits: list[Edit], candidate: str, language: str, path: str | None, repo_dir: str | None) -> bool:
    """Whether the identity of every ``{}`` the edits wrote cannot matter.

    Each one is an attribute of an audited component (:data:`_AUDITED`), or one
    the receiving component's own ``{}`` default makes redundant AND whose
    component only hands the prop on, unchanged, to a receiver that qualifies
    in turn (:func:`_identity_free`). A component that reads the prop any other
    way (a hook's dependency list, a spread, a member access, a call argument)
    is refused: it would see a new object where the helper's closure held one.
    """
    from flag_cleanup.ts_default_arguments import _attribute_span, _Resolver

    data = candidate.encode("utf-8")
    root = _parse(candidate, language)
    resolver = _Resolver(root, data, path, repo_dir, language)
    shift = 0
    written = []
    for start, end, text in sorted(edits):
        if text == b"{}":
            written.append(start + shift)
        shift += len(text) - (end - start)
    for position in written:
        node = root.descendant_for_byte_range(position, position + 2)
        if node is None or node.type != "object" or node.start_byte != position:
            return False
        holder = node.parent
        if holder is None or holder.type != "jsx_expression" or holder.parent is None:
            return False
        attribute = holder.parent
        if _audited(resolver, attribute, data):
            continue
        if _attribute_span(attribute, node, resolver, data) is None:
            return False
        if not _identity_free(resolver, attribute, data, repo_dir, 0):
            return False
    return True


def _audited(resolver, attribute: Node, data: bytes) -> bool:
    """Whether ``attribute`` is the audited prop of an audited component."""
    from flag_cleanup.ts_default_arguments import _sole_binder

    element = attribute.parent
    tag = element.child_by_field_name("name") if element is not None else None
    name = attribute.named_children[0] if attribute.named_children else None
    if tag is None or tag.type != "identifier" or name is None or name.type != "property_identifier":
        return False
    binder = _sole_binder(_text(tag, data), resolver.root, data)
    if binder is None or binder.parent is None or binder.parent.type != "import_specifier":
        return False
    specifier = binder.parent
    statement = specifier.parent
    while statement is not None and statement.type != "import_statement":
        statement = statement.parent
    source = statement.child_by_field_name("source") if statement is not None else None
    imported = specifier.child_by_field_name("name")
    if source is None or imported is None or imported.type != "identifier":
        return False
    if any(c.type == "type" for c in statement.children) or any(c.type == "type" for c in specifier.children):
        return False
    package = _text(source, data).strip("\"'`")
    if _AUDITED.get((package, _text(imported, data))) != _text(name, data):
        return False
    # The audit read the real package; a mock of it is some other component.
    return not _mocks_package(resolver, package) and not _aliased_package(resolver, package)


def _mocks_package(resolver, package: str) -> bool:
    """Whether a mock of ``package`` can reach the file the resolver reads (the
    helper's own, or a forwarder module further down the chain).

    Refused, because the audit read the real package:

    * the file calls a mocker (``jest.mock``, ``vi.mock``, ``doMock``,
      ``unstable_mockModule``) naming it, or naming something this pass cannot
      read as a plain string (a template literal, a variable);
    * any NON-test source file in the checkout does either: a setup file or a
      helper such as ``import "./mocks"`` applies to every test, and the
      configuration that loads it is not parsed;
    * the checkout holds ``__mocks__/<package>.*`` or
      ``__mocks__/<package>/index.*``, jest's automock.

    A test file that mocks the package affects only itself (each test file has
    its own module registry), so another test file's mock does not count.
    """
    from flag_cleanup import source_index
    from flag_cleanup.ts_default_arguments import _MOCKERS, _called_name, _module_specifier
    from flag_cleanup.ts_tests import is_test_file

    cached = resolver.__dict__.setdefault("_mocked_packages", {})
    if package in cached:
        return cached[package]

    from flag_cleanup import ts_mock_config as config

    def names(root: Node) -> bool:
        for call in _walk(root):
            if call.type != "call_expression":
                continue
            function = call.child_by_field_name("function")
            if config.automock_call(function):
                return True
            node_mock = config.node_mocker(function) == "module"
            if not node_mock and _called_name(function) not in _MOCKERS:
                continue
            arguments = call.child_by_field_name("arguments")
            passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
            if not passed:
                continue
            specifier = _module_specifier(passed[0])
            if specifier == package:
                return True
            # An unreadable name counts only for the test runners' own mockers:
            # `fetchMock.mock(url, 200)` is something else.
            if specifier is None and (node_mock or _is_module_mocker(function, root)):
                return True
        return False

    mocked = names(resolver.root) or (
        resolver.repo_dir is not None and config.everything_mocked(resolver.repo_dir, automock_only=True)
    )
    if not mocked and resolver.repo_dir is not None:
        for _path, text, grammar in resolver._scan():
            if "mock" in text.lower() and not is_test_file(text, grammar) and names(_parse(text, grammar)):
                mocked = True
                break
        wanted = (f"__mocks__/{package}.", f"__mocks__/{package}/index.")
        for listed in source_index.files(
            resolver.repo_dir, (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
        ):
            if mocked:
                break
            written = str(listed.path).replace("\\", "/") + "."
            mocked = any(w in written for w in wanted)
    cached[package] = mocked
    return mocked


#: The modules whose bare ``mock``/``doMock`` import is a test runner's.
_RUNNER_MODULES = frozenset({"vitest", "@jest/globals"})


def _is_module_mocker(function: Node | None, root: Node) -> bool:
    """Whether the callee is ``jest.<mocker>``/``vi.<mocker>``, or a bare
    mocker imported from ``vitest`` or ``@jest/globals``."""
    from flag_cleanup import cross_file_ts

    if function is None:
        return False
    if function.type == "member_expression":
        receiver = function.child_by_field_name("object")
        return receiver is not None and receiver.type == "identifier" and receiver.text in (b"jest", b"vi")
    if function.type != "identifier":
        return False
    for statement in root.named_children:
        if statement.type != "import_statement":
            continue
        source = statement.child_by_field_name("source")
        if source is None or cross_file_ts._string_value(source) not in _RUNNER_MODULES:
            continue
        for specifier in _walk(statement):
            if specifier.type != "import_specifier":
                continue
            local = specifier.child_by_field_name("alias") or specifier.child_by_field_name("name")
            if local is not None and local.text == function.text:
                return True
    return False


_PLAIN_VERSION = re.compile(r"[0-9A-Za-z.+\-^~<>=|* ]*")
_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def _literal(node: Node, data: bytes) -> str | None:
    """The text a string or regex literal holds, or None for anything else."""
    text = data[node.start_byte : node.end_byte].decode("utf-8", "replace")
    if node.type == "string" and len(text) >= 2:
        return text[1:-1].replace("\\\\", "\\")
    if node.type == "regex":
        return text[1 : text.rindex("/")]
    return None


def _literal_or_none(node: Node | None, data: bytes) -> tuple[str | None, bool] | None:
    """``(text, is_regex)`` for a plain string, a substitution-free template or
    a regex literal; ``None`` for anything this pass cannot read (a variable,
    a call, ``new RegExp(…)``, a template with a substitution)."""
    if node is None:
        return None
    if node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.named_children):
            return None
        text = data[node.start_byte + 1 : node.end_byte - 1].decode("utf-8", "replace")
        return text.replace("\\\\", "\\"), False
    text = _literal(node, data)
    if text is None:
        return None
    return text, node.type == "regex"


def _alias_keys(root: Node, data: bytes) -> list[tuple[str | None, bool]]:
    """Each pattern a config uses to redirect a module: the keys of
    ``moduleNameMapper`` (regular expressions), the keys of an ``alias`` object
    and the ``find`` of an alias entry (a plain name, or a regular expression
    when written as a regex literal). ``(text, is_regex)``; ``(None, True)``
    stands for one this pass cannot read, which counts as a redirect."""
    found: list[tuple[str | None, bool]] = []

    def take(node: Node | None, regex: bool) -> None:
        read = _literal_or_none(node, data)
        found.append((read[0], regex or read[1]) if read is not None else (None, True))

    def keys(obj: Node | None, regex: bool) -> None:
        if obj is None or obj.type != "object":
            found.append((None, True))
            return
        for member in obj.named_children:
            if member.type == "pair":
                key = member.child_by_field_name("key")
                if key is None:
                    found.append((None, True))
                elif key.type in ("property_identifier", "identifier", "number"):
                    found.append((data[key.start_byte : key.end_byte].decode("utf-8", "replace"), regex))
                elif key.type == "computed_property_name":
                    take(key.named_children[0] if key.named_children else None, regex)
                else:
                    take(key, regex)
            elif member.type == "shorthand_property_identifier":
                found.append((data[member.start_byte : member.end_byte].decode("utf-8", "replace"), regex))
            elif member.type != "comment":
                found.append((None, True))  # a spread, a method

    for node in _walk(root):
        if node.type != "pair":
            continue
        key = node.child_by_field_name("key")
        value = node.child_by_field_name("value")
        name = _literal(key, data) if key is not None and key.type == "string" else (
            data[key.start_byte : key.end_byte].decode("utf-8", "replace") if key is not None else None
        )
        if value is None:
            continue
        if name == "moduleNameMapper":
            keys(value, True)
        elif name == "alias":
            if value.type == "array":
                for entry in value.named_children:
                    if entry.type == "comment":
                        continue
                    if entry.type != "object":
                        found.append((None, True))
                        continue
                    if not any(
                        m.type == "pair"
                        and (k := m.child_by_field_name("key")) is not None
                        and data[k.start_byte : k.end_byte].decode("utf-8", "replace").strip("\"'") == "find"
                        for m in entry.named_children
                    ):
                        found.append((None, True))
            else:
                keys(value, False)
        elif name == "find":
            take(value, False)
    return found


#: Letters JavaScript treats as an escape and Python's ``re`` does too.
_SHARED_ESCAPES = frozenset("dDwWsSbBnrtfv0xu")


def _js_pattern(text: str) -> str:
    """``text`` with each ``\\<letter>`` that is an identity escape in
    JavaScript but an escape of another meaning (or none) in Python's ``re``
    (``\\a``, ``\\A``, ``\\Z``, ``\\e``, ``\\z``) written as the bare letter."""

    def bare(match: re.Match) -> str:
        char = match.group(1)
        return match.group(0) if not char.isalpha() or char in _SHARED_ESCAPES else char

    return re.sub(r"\\(.)", bare, text, flags=re.DOTALL)


def _redirects(patterns: list[tuple[str | None, bool]], package: str) -> bool:
    """Whether any pattern could send ``package`` elsewhere: one this pass
    cannot read, a regular expression that does not compile or matches the
    name (ignoring case, which can only add refusals), or a plain alias that
    is the name or a prefix of it. An alias of a different ``@featureflip/``
    package does not count (the SDK's own repository aliases its siblings)."""
    for text, regex in patterns:
        if text is None:
            return True
        if not regex:
            lowered = text.lower()
            if package == lowered or package.startswith(lowered.rstrip("/") + "/"):
                return True
            continue
        try:
            if re.search(_js_pattern(text), package, re.IGNORECASE):
                return True
        except re.error:
            return True
    return False


def _vendored(text: str, package: str) -> bool:
    """Whether a ``package.json`` points ``package`` somewhere but the registry
    (a path, ``file:``, ``link:``, ``workspace:``, ``npm:``, a git URL) or
    patches it (pnpm's ``patchedDependencies``)."""
    try:
        manifest = json.loads(text)
    except ValueError:
        return package in text
    stack = [manifest]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith(package) and isinstance(value, str) and not _PLAIN_VERSION.fullmatch(value):
                    return True
                if key == "patchedDependencies" and isinstance(value, dict) and any(package in k for k in value):
                    return True
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _aliased_package(resolver, package: str) -> bool:
    """Whether the checkout can replace ``package`` by another module without
    a mock call: a ``moduleNameMapper`` or bundler alias (or a Babel
    ``module-resolver`` alias) in configuration, a ``tsconfig`` path, a
    vendored or patched copy of the package.

    Config means a non-test JavaScript/TypeScript file named like a config or
    workspace file or ``.babelrc``, the ``jest`` object of a ``package.json``,
    ``tsconfig*.json`` ``paths``. In those the package name written in a
    string counts (comments are not read, so a mention in one does not); the
    alias patterns count more widely (:func:`_redirects`), since a wildcard
    ``^@featureflip/(.*)$`` never spells the full name. A configuration file
    that is not named like a config and is loaded by ``--config``, and
    ``.storybook/main.*``, are not read.
    """
    from flag_cleanup import source_index
    from flag_cleanup.piranha_runner import _SKIP_DIRS, source_files
    from flag_cleanup.ts_default_arguments import _grammar
    from flag_cleanup.ts_tests import is_test_file

    cached = resolver.__dict__.setdefault("_aliased_packages", {})
    if package in cached:
        return cached[package]
    needle = package.lstrip("@")

    def spells(root: Node, data: bytes) -> bool:
        for node in _walk(root):
            if node.type in ("string", "template_string", "regex"):
                if needle in data[node.start_byte : node.end_byte].decode("utf-8", "replace").replace("\\", ""):
                    return True
        return False

    def below(root: Node, data: bytes, key: str) -> bool:
        """Whether a ``key: …`` pair anywhere in the tree spells the needle,
        or (for ``jest``) redirects the package."""
        for node in _walk(root):
            if node.type != "pair":
                continue
            name = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            if name is None or value is None or name.type not in ("string", "property_identifier"):
                continue
            if data[name.start_byte : name.end_byte].decode("utf-8", "replace").strip("\"'") != key:
                continue
            if spells(value, data) or (key == "jest" and _redirects(_alias_keys(value, data), package)):
                return True
        return False

    aliased = False
    if resolver.repo_dir is not None:
        suffixes = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
        candidates: list[tuple[Path, str]] = []
        for listed in source_index.files(resolver.repo_dir, suffixes):
            name = listed.path.name.lower()
            if "config" in name or "workspace" in name or name.startswith(".babelrc"):
                candidates.append((listed.path, "code"))
        root_dir = Path(source_index.checkout_root(resolver.repo_dir))
        for found in source_files(root_dir, {".json", ".patch", ""}, _SKIP_DIRS):
            name = found.name.lower()
            if name.endswith(".patch"):
                if name.startswith(("@featureflip+react", "@featureflip__react")):
                    aliased = True
            elif name == "package.json":
                candidates.append((found, "jest"))
            elif name.startswith("tsconfig"):
                candidates.append((found, "paths"))
            elif "config" in name or "workspace" in name or name.startswith(".babelrc"):
                candidates.append((found, "all"))
        for path, kind in [] if aliased else candidates:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if kind == "jest" and _vendored(text, package):
                aliased = True
                break
            lowered = text.lower()
            if "featureflip" not in lowered and not (
                kind != "paths" and any(w in text for w in ("moduleNameMapper", "alias", "find"))
            ):
                continue
            json_like = path.suffix == ".json" or path.suffix == ""
            grammar = "tsx" if json_like else _grammar(str(path))
            source = f"({text}\n)" if json_like else text
            if kind == "code" and is_test_file(text, grammar):
                continue
            tree = _parse(source, grammar)
            body = source.encode("utf-8")
            if tree.has_error:
                aliased = True  # cannot read it, and it names the package
            elif kind in ("code", "all"):
                aliased = spells(tree, body) or _redirects(_alias_keys(tree, body), package)
            else:
                aliased = below(tree, body, kind)
            if aliased:
                break
    cached[package] = aliased
    return aliased


def _identity_free(resolver, attribute: Node, data: bytes, repo_dir: str | None, depth: int) -> bool:
    """Whether the component ``attribute`` goes to uses the prop only to hand
    it on, unchanged, to an audited component or to one that does the same."""
    from flag_cleanup.ts_default_arguments import _grammar, _parameters, _Resolver

    if depth > _MAX_FORWARDS:
        return False
    element = attribute.parent
    tag = element.child_by_field_name("name") if element is not None else None
    if tag is None or tag.type != "identifier":
        return False
    callee = resolver.component(tag)
    if callee is None or callee.memoized or callee.function.parent is None:
        return False
    if callee.function.parent.type == "arguments":
        return False  # wrapped in memo(…) or forwardRef(…)
    prop = _text(attribute.named_children[0], data)
    members = [m for m in _parameters(callee.function) if m.type != "comment"]
    pattern = members[0].child_by_field_name("pattern") if members else None
    if pattern is None or pattern.type != "object_pattern":
        return False
    local = _bound_prop(pattern, prop, callee.data)
    body = callee.function.child_by_field_name("body")
    if local is None or body is None:
        return False
    if len({n.start_byte for n in binding_nodes(callee.function) if _text(n, callee.data) == local}) != 1:
        return False
    references = _references(body, callee.data, local)
    if references is None:
        return False
    onward = _Resolver(callee.root, callee.data, callee.path or None, repo_dir, _grammar(callee.path or ""))
    for reference in references:
        node = reference
        while node.parent is not None and node.parent.type in _TYPE_ONLY:
            node = node.parent
        holder = node.parent
        if (
            holder is None
            or holder.type != "jsx_expression"
            or len(holder.named_children) != 1
            or holder.parent is None
            or holder.parent.type != "jsx_attribute"
        ):
            return False
        forwarded = holder.parent
        if forwarded.named_children[0].type != "property_identifier":
            return False
        if not (_audited(onward, forwarded, callee.data) or _identity_free(onward, forwarded, callee.data, repo_dir, depth + 1)):
            return False
    return True


def _bound_prop(pattern: Node, prop: str, data: bytes) -> str | None:
    """The local name a destructuring ``pattern`` binds ``prop`` to, when it
    does so exactly once, by a plain name (``{ prop }``, ``{ prop = d }``,
    ``{ prop: local }``)."""
    found = []
    for member in pattern.named_children:
        if member.type == "shorthand_property_identifier_pattern" and _text(member, data) == prop:
            found.append(prop)
        elif member.type == "object_assignment_pattern":
            left = member.child_by_field_name("left")
            if left is not None and left.type == "shorthand_property_identifier_pattern" and _text(left, data) == prop:
                found.append(prop)
        elif member.type == "pair_pattern":
            key = member.child_by_field_name("key")
            value = member.child_by_field_name("value")
            if key is not None and _text(key, data) == prop:
                if value is not None and value.type == "assignment_pattern":
                    value = value.child_by_field_name("left")
                if value is None or value.type != "identifier":
                    return None
                found.append(_text(value, data))
    return found[0] if len(found) == 1 else None


def _is_this(parameter: Node) -> bool:
    pattern = parameter.child_by_field_name("pattern")
    return parameter.type == "required_parameter" and pattern is not None and pattern.type == "this"


def _tree_root(scope: Node) -> Node:
    node = scope
    while node.parent is not None:
        node = node.parent
    return node


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
