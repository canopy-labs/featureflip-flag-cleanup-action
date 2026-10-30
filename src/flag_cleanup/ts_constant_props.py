"""Remove a JSX prop the fold made the same literal at every call site.

The fold resolves a flag read where a component is rendered, so the read's
value becomes a literal passed as a prop::

    <FlagRow flag={flag} showExpiry={expiryEnabled} />   // before
    <FlagRow flag={flag} showExpiry={true} />            // after the fold

The prop now carries no information: it is ``true`` wherever the component is
used, and inside the component it still guards code as if it could be
``false``. A reviewer drops it from the props type, the destructuring and the
call, and folds what it guarded. :func:`inline_constant_props` does that edit
and hands the result back to the engine the way
:func:`flag_cleanup.ts_fold.inline_literal_bindings` does: each read of the
prop inside the component becomes ``<marker>(true)``, and ``rules/ts.toml``'s
marker rule creates the literal inside the cascade, so ``showExpiry &&
isExpired(x)`` folds to ``isExpired(x)`` exactly as a read written inline
would.

Which props qualify, and why each condition is there:

* **The transform made it a literal.** Per component and prop, the output must
  pass that literal more often than the input did. A prop the customer already
  wrote as ``{true}`` (or a previous removal left) is theirs, and stays.
* **Every element of the component passes it, as that same literal, and
  spreads nothing.** A spread attribute can set the prop, so it is not known
  to be constant.
* **The component is module-private.** A function declaration or a ``const``
  bound to a function, an arrow function or ``memo(…)`` of one, at the top
  level of a file that is a module (it has an ``import`` or ``export``), not
  exported, and spelled nowhere but its declaration and the names of JSX
  elements. Any other mention (``export { Row }``, ``Row.displayName``,
  ``render(Row)``) means a call site this pass cannot see.
* **Its one parameter destructures the prop by name**, with no default value
  and no renaming, and nothing in the component binds the name again.
* **The props type is the component's own.** An inline object type, or an
  interface or object type alias declared in the file, not exported, extending
  nothing, and spelled nowhere else. Otherwise removing the member could break
  another user of the type, and keeping it would leave a required prop no
  call passes.
* **Every read of the prop is an ordinary expression**, as
  :mod:`flag_cleanup.ts_fold` requires of a binding it inlines.
* **Each deleted attribute, entry and member has its line(s) to itself**, or,
  for an attribute or a destructured entry, sits on a line it shares only with
  its neighbours in the same list.

A refusal leaves the literal prop standing, which is what the run produced
before this pass existed.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup.ts_fold import _NON_EXPRESSION_PARENTS, _TYPE_CONTEXTS
from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

_LITERALS = frozenset({"true", "false"})
_ELEMENTS = frozenset({"jsx_opening_element", "jsx_self_closing_element"})
_ELEMENT_NAMES = frozenset({"jsx_opening_element", "jsx_closing_element", "jsx_self_closing_element"})
_FUNCTIONS = frozenset({"function_expression", "arrow_function"})
_MEMO = frozenset({"memo", "React.memo"})
_COMPONENT = re.compile(r"[A-Z][A-Za-z0-9_$]*")


@dataclass(frozen=True)
class _Element:
    node: Node
    #: A spread attribute (``{...rest}``) can set any prop.
    spreads: bool


def inline_constant_props(before: str, after: str, language: str, marker: str) -> str:
    """Remove every prop the transform made one literal at every call site.

    ``before`` is the customer's file and ``after`` the engine's output.
    Returns ``after`` unchanged when nothing qualifies (see the module
    docstring), and when ``after`` already holds ``marker``, for the reason
    :func:`~flag_cleanup.ts_fold.inline_literal_bindings` gives.
    """
    if after == before or marker in after or language == "ts":
        return after
    root = _parse(after, language)
    if root.has_error or not _is_module(root):
        return after
    data = after.encode("utf-8")
    old_root = _parse(before, language)
    old_data = before.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    for component, elements in _elements(root, data).items():
        old = _elements(old_root, old_data).get(component, [])
        for prop in _constant_props(elements, data):
            literal = _literal_of(elements, prop, data)
            passed = sum(1 for e in elements if _passes(e, prop, literal, data))
            earlier = sum(1 for e in old if _passes(e, prop, literal, old_data))
            if passed <= earlier:
                continue
            planned = _plan(root, data, component, prop, literal, elements, marker)
            if planned is None:
                continue
            edits.extend(planned)
            # One prop per round: the next one is judged on the rewritten
            # file, after the engine has folded this one.
            break
        if edits:
            break
    if not edits:
        return after
    edits.sort()
    for (_, end, _), (start, _, _) in zip(edits, edits[1:]):
        if start < end:
            return after
    for start, end, text in reversed(edits):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8")


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def _is_module(root: Node) -> bool:
    return any(c.type in {"import_statement", "export_statement"} for c in root.named_children)


def _elements(root: Node, data: bytes) -> dict[str, list[_Element]]:
    """Every JSX element whose name is a capitalised identifier, by that name."""
    found: dict[str, list[_Element]] = {}
    for node in _walk(root):
        if node.type not in _ELEMENTS:
            continue
        name = node.child_by_field_name("name")
        if name is None or name.type != "identifier" or not _COMPONENT.fullmatch(_text(name, data)):
            continue
        found.setdefault(_text(name, data), []).append(_element(node))
    return found


def _element(node: Node) -> _Element:
    return _Element(node, any(c.type == "jsx_expression" for c in node.named_children))


def _attributes(element: _Element, prop: str, data: bytes) -> list[Node]:
    return [
        child
        for child in element.node.named_children
        if child.type == "jsx_attribute"
        and child.named_children
        and child.named_children[0].type == "property_identifier"
        and _text(child.named_children[0], data) == prop
    ]


def _value(attribute: Node, data: bytes) -> str | None:
    """``true``/``false`` when the attribute passes that literal, else None.
    A bare attribute (``<Row compact />``) passes ``true``."""
    values = attribute.named_children[1:]
    if not values:
        return "true"
    if len(values) != 1 or values[0].type != "jsx_expression":
        return None
    inner = values[0].named_children
    if len(inner) != 1 or inner[0].type not in _LITERALS:
        return None
    return _text(inner[0], data)


def _passes(element: _Element, prop: str, literal: str, data: bytes) -> bool:
    attributes = _attributes(element, prop, data)
    return len(attributes) == 1 and _value(attributes[0], data) == literal


def _constant_props(elements: list[_Element], data: bytes) -> Iterator[str]:
    """Each prop every element passes once as one boolean literal, none spreading."""
    if any(e.spreads for e in elements):
        return
    names = Counter()
    for element in elements:
        for child in element.node.named_children:
            if child.type == "jsx_attribute" and child.named_children[0].type == "property_identifier":
                names[_text(child.named_children[0], data)] += 1
    for prop in names:
        if _literal_of(elements, prop, data) is not None:
            yield prop


def _literal_of(elements: list[_Element], prop: str, data: bytes) -> str | None:
    literals = set()
    for element in elements:
        attributes = _attributes(element, prop, data)
        if len(attributes) != 1:
            return None
        literals.add(_value(attributes[0], data))
    if len(literals) != 1 or None in literals:
        return None
    return literals.pop()


def _plan(
    root: Node,
    data: bytes,
    component: str,
    prop: str,
    literal: str,
    elements: list[_Element],
    marker: str,
    function: Node | None = None,
) -> list[tuple[int, int, bytes]] | None:
    """The edits removing ``prop`` from ``component``, or None to refuse.
    ``function`` is the component when the caller has already proven it;
    otherwise it must be module-private (:func:`_component_function`)."""
    if function is None:
        function = _component_function(root, data, component, elements)
    if function is None:
        return None
    parameters = function.child_by_field_name("parameters")
    if parameters is None or len(parameters.named_children) != 1:
        return None
    parameter = parameters.named_children[0]
    if parameter.type != "required_parameter":
        return None
    pattern = parameter.child_by_field_name("pattern")
    if pattern is None or pattern.type != "object_pattern":
        return None
    entries = [c for c in pattern.named_children if c.type != "comment"]
    if len(entries) < 2:
        return None
    mine = [
        c for c in entries if c.type == "shorthand_property_identifier_pattern" and _text(c, data) == prop
    ]
    if len(mine) != 1:
        return None
    if any(
        c.type in {"object_assignment_pattern", "pair_pattern"} and _names(c, data, prop) for c in entries
    ):
        return None
    entry = mine[0]
    body = function.child_by_field_name("body")
    if body is None or sum(1 for n in binding_nodes(function) if _text(n, data) == prop) != 1:
        return None
    references = _references(body, data, prop)
    if references is None:
        return None
    member = _member(root, data, parameter.child_by_field_name("type"), prop)
    if member is False:
        return None

    edits: list[tuple[int, int, bytes]] = []
    for element in elements:
        span = _item_span(_attributes(element, prop, data)[0], data, separator=None)
        if span is None:
            return None
        edits.append((*span, b""))
    span = _item_span(entry, data, separator=b",")
    if span is None:
        return None
    edits.append((*span, b""))
    if member is not None:
        span = _own_lines(member, data)
        if span is None:
            return None
        edits.append((_with_doc_comment(member, data, span[0]), span[1], b""))
    replacement = f"{marker}({literal})".encode("utf-8")
    edits.extend((ref.start_byte, ref.end_byte, replacement) for ref in references)
    return edits


def _names(node: Node, data: bytes, prop: str) -> bool:
    """Whether a pattern entry destructures the property ``prop``."""
    key = node.child_by_field_name("key") or node.child_by_field_name("left")
    return key is not None and _text(key, data) == prop


def _component_function(root: Node, data: bytes, component: str, elements: list[_Element]) -> Node | None:
    """The function a module-private top-level ``component`` is, or None."""
    declarations: list[tuple[Node, Node, Node]] = []  # (statement, name node, function)
    for statement in root.named_children:
        if statement.type == "function_declaration":
            name = statement.child_by_field_name("name")
            if name is not None and _text(name, data) == component:
                declarations.append((statement, name, statement))
        elif statement.type == "lexical_declaration":
            for declarator in statement.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name = declarator.child_by_field_name("name")
                if name is None or _text(name, data) != component:
                    continue
                if len([d for d in statement.named_children if d.type == "variable_declarator"]) != 1:
                    return None
                if declarator.child_by_field_name("type") is not None:
                    return None
                function = _unwrap(declarator.child_by_field_name("value"), data)
                if function is None:
                    return None
                declarations.append((statement, name, function))
    if len(declarations) != 1:
        return None
    _statement, name, function = declarations[0]
    allowed = {name.id}
    if function.type == "function_expression":
        inner = function.child_by_field_name("name")
        if inner is not None:
            allowed.add(inner.id)
    element_names = {
        child.id
        for element in elements
        for child in [element.node.child_by_field_name("name")]
        if child is not None
    }
    for node in _walk(root):
        if node.type not in {"identifier", "type_identifier", "shorthand_property_identifier",
                             "shorthand_property_identifier_pattern", "property_identifier"}:
            continue
        if _text(node, data) != component or node.id in allowed or node.id in element_names:
            continue
        if node.type == "property_identifier":
            # `obj.Row` or `{ Row: 1 }` names a property, not the binding.
            if node.parent is not None and node.parent.type in {"member_expression", "pair"}:
                continue
            return None
        if node.type == "identifier" and node.parent is not None and node.parent.type in _ELEMENT_NAMES:
            # A closing tag's name: its opening element is one of `elements`.
            if node.parent.type == "jsx_closing_element":
                continue
        return None
    if any(n.type == "identifier" and n.text in (b"eval", b"arguments") for n in _walk(function)):
        return None
    return function


def _unwrap(value: Node | None, data: bytes) -> Node | None:
    """The function a ``const`` is bound to, through one ``memo(…)``."""
    if value is None:
        return None
    if value.type in _FUNCTIONS:
        return value
    if value.type != "call_expression":
        return None
    callee = value.child_by_field_name("function")
    arguments = value.child_by_field_name("arguments")
    if callee is None or arguments is None or _text(callee, data) not in _MEMO:
        return None
    passed = [c for c in arguments.named_children if c.type != "comment"]
    if len(passed) != 1 or passed[0].type not in _FUNCTIONS:
        return None
    return passed[0]


def _references(body: Node, data: bytes, prop: str) -> list[Node] | None:
    """Every read of ``prop`` in the component's body, or None if one cannot
    be rewritten."""
    references = []
    for node in _walk(body):
        if node.type in {"shorthand_property_identifier", "shorthand_property_identifier_pattern", "type_identifier"}:
            if _text(node, data) == prop:
                return None
            continue
        if node.type != "identifier" or _text(node, data) != prop:
            continue
        if node.parent is None or node.parent.type in _NON_EXPRESSION_PARENTS:
            return None
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.type in _TYPE_CONTEXTS:
                return None
            ancestor = ancestor.parent
        references.append(node)
    return references


def _member(root: Node, data: bytes, annotation: Node | None, prop: str) -> Node | None | bool:
    """The props type's member for ``prop``: None when the parameter has no
    type, False to refuse."""
    if annotation is None:
        return None
    types = [c for c in annotation.named_children if c.type != "comment"]
    if len(types) != 1:
        return False
    written = types[0]
    if written.type == "object_type":
        body = written
    elif written.type == "type_identifier":
        body = _own_type(root, data, _text(written, data))
        if body is None:
            return False
    else:
        return False
    members = [
        c
        for c in body.named_children
        if c.type == "property_signature"
        and (name := c.child_by_field_name("name")) is not None
        and name.type == "property_identifier"
        and _text(name, data) == prop
    ]
    others = [c for c in body.named_children if c.type not in {"property_signature", "comment"}]
    if len(members) != 1 or others:
        return False
    return members[0]


def _own_type(root: Node, data: bytes, name: str) -> Node | None:
    """The body of a top-level, unexported interface or object type alias
    ``name`` spelled nowhere but its declaration and one annotation."""
    spellings = sum(
        1 for n in _walk(root) if n.type in {"type_identifier", "identifier"} and _text(n, data) == name
    )
    if spellings != 2:
        return None
    for statement in root.named_children:
        declared = statement.child_by_field_name("name")
        if declared is None or _text(declared, data) != name:
            continue
        if statement.child_by_field_name("type_parameters") is not None:
            return None
        if statement.type == "interface_declaration":
            if any(c.type == "extends_type_clause" for c in statement.named_children):
                return None
            return statement.child_by_field_name("body")
        if statement.type == "type_alias_declaration":
            value = statement.child_by_field_name("value")
            return value if value is not None and value.type == "object_type" else None
        return None
    return None


def _own_lines(node: Node, data: bytes) -> tuple[int, int] | None:
    """The byte span of the node's whole lines, or None if it shares one."""
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    newline = data.find(b"\n", node.end_byte)
    end = len(data) if newline == -1 else newline + 1
    if data[start : node.start_byte].strip():
        return None
    if data[node.end_byte : end].strip() not in (b"", b";", b","):
        return None
    return start, end


def _with_doc_comment(member: Node, data: bytes, start: int) -> int:
    """``start``, moved up over a ``/** … */`` comment that has its own lines
    directly above ``member``: it documents the member, and would otherwise
    describe the one below."""
    comment = member.prev_sibling
    if (
        comment is None
        or comment.type != "comment"
        or not comment.text.startswith(b"/**")
        or comment.end_point[0] + 1 != member.start_point[0]
    ):
        return start
    span = _own_lines(comment, data)
    return start if span is None else span[0]


def _item_span(node: Node, data: bytes, separator: bytes | None) -> tuple[int, int] | None:
    """The bytes to delete to remove one item of a list: its whole line when
    it has one to itself, else the item with the separator after it (or
    before it, for the last) and the space between."""
    own = _own_lines(node, data)
    if own is not None:
        return own
    line_start = data.rfind(b"\n", 0, node.start_byte) + 1
    newline = data.find(b"\n", node.end_byte)
    line_end = len(data) if newline == -1 else newline
    if b"//" in data[line_start:line_end] or b"/*" in data[line_start:line_end]:
        return None
    if separator is None:
        start = node.start_byte
        while start > line_start and data[start - 1 : start] in (b" ", b"\t"):
            start -= 1
        if start == line_start and line_start > 0:
            # First on a line it shares with what follows it (`compact />`):
            # that joins the line above.
            start = line_start - 1
            if data[start - 1 : start] == b"\r":
                start -= 1
        if start == node.start_byte:
            return None
        return start, node.end_byte
    end = node.end_byte
    after = end
    while after < line_end and data[after : after + 1] in (b" ", b"\t"):
        after += 1
    if data[after : after + 1] == separator:
        after += 1
        while after < line_end and data[after : after + 1] in (b" ", b"\t"):
            after += 1
        return node.start_byte, after
    start = node.start_byte
    while start > line_start and data[start - 1 : start] in (b" ", b"\t"):
        start -= 1
    if data[start - 1 : start] != separator:
        return None
    return start - 1, end
