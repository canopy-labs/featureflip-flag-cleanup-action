"""A C# test helper whose ``bool`` parameter chooses a wrapper stub's value.

The C# port of :mod:`flag_cleanup.ts_param_pins`::

    private Svc Make(bool on = true)
    {
        gate.Setup(g => g.IsXEnabled).Returns(on);
        ...
    }
    [Fact] public void Off() { var s = Make(on: false); ... }

No pattern can say which branch ``Returns(on)`` exercises: each CALL says it,
with its argument or the parameter's default. This module proves the shape,
so that the test-double pass can delete the stub, the parameter and every
argument passed to it, and judge each calling test as if it had stubbed its
literal itself.

Everything that has to hold, each checked rather than assumed:

* The stub's value names a plain ``bool`` parameter of its enclosing method
  (no ``ref``/``in``/``out``/``params``, no attribute), with a ``true`` or
  ``false`` default or none, and nothing else in the method mentions it.
* The method is ``private``, has no attribute, is not a test, not
  ``partial``/``extern``/``abstract``, not generic, and its type is not
  ``partial`` (another part could call it).
* Every other mention of the method's name in the file is the callee of a
  plain call, never a method group, a declaration, a qualified call or a
  string (reflection). Each call passes ``true``/``false`` at the parameter
  (by name anywhere, or positionally as the last argument passed) or
  nothing, and nothing needs a missing default.
* No span the edit deletes holds a comment: it is the customer's, and
  nothing here can say where it belongs. Nor does a comment above the
  method name the parameter: a dangling ``<param>`` or ``<paramref>`` is
  CS1572/CS1734.

Whether each calling test can then go, or keep its call, is the caller's
question (:func:`flag_cleanup.mock_stubs.resolve`), which refuses the pin
whole when any one cannot.

One call may pass a parameter instead of a literal: an xunit ``[Theory]``'s
own, so that each ``[InlineData]`` row says the value (:func:`theory_at`).
The caller then cuts the rows of the removed value, that column from the
others and the parameter, or deletes the theory when no row is left.

Or another private helper's own ``bool`` parameter, read nowhere else in it
(``Make(bool on = true) => new(Policy(on))``): that helper is then pinned
the same way, by its calls (:func:`chained`), and its parameter goes too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Node

_BOOLEANS = frozenset({"true", "false"})
#: Every string literal node type, where a name is reflection's to read.
_STRINGS = frozenset(
    {"string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression"}
)
#: Method modifiers under which a body or a caller may be out of sight.
_OPAQUE = frozenset({"partial", "extern", "abstract", "public", "internal", "protected", "virtual", "override"})


#: The value :func:`calls` gives a call whose ``[Theory]`` rows say it.
ROWS = "rows"
#: How many helpers deep a pin follows a parameter passed on (:func:`chained`).
_CHAIN_DEPTH = 4


@dataclass(frozen=True)
class TheoryRows:
    """A ``[Theory]`` passing its own ``bool`` parameter to a pinned call."""

    method: Node
    name: str
    parameter: Node
    #: ``(attribute list, its argument at the parameter's position, literal)``.
    rows: tuple[tuple[Node, Node, str], ...]


@dataclass(frozen=True)
class CsPin:
    method: str
    parameter: str
    index: int
    #: ``"true"``/``"false"``, or ``None`` when the parameter has none.
    default: str | None
    method_start: int


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _method_of(node: Node) -> Node | None:
    while node is not None and node.type != "method_declaration":
        node = node.parent
    return node


def _modifiers(node: Node, data: bytes) -> set[str]:
    return {_text(c, data) for c in node.children if c.type == "modifier"}


def _parameters(method: Node) -> list[Node]:
    parameters = method.child_by_field_name("parameters")
    return [p for p in parameters.named_children if p.type == "parameter"] if parameters is not None else []


def _default(parameter: Node, data: bytes) -> tuple[bool, str | None]:
    """``(well-formed, default)`` of a parameter: tree-sitter-c-sharp gives a
    default as the expression following the name, with no node of its own."""
    name = parameter.child_by_field_name("name")
    after = [c for c in parameter.named_children if name is not None and c.start_byte >= name.end_byte]
    if not after:
        return True, None
    if len(after) != 1 or after[0].type != "boolean_literal":
        return False, None
    return True, _text(after[0], data)


def find(root: Node, data: bytes, statement: Node, parameter: str, depth: int = 0) -> CsPin | None:
    """The pin ``statement`` (a stub returning ``parameter``) makes, or
    ``None`` when any of the module's conditions does not hold."""
    from flag_cleanup import mock_stubs

    method = _method_of(statement)
    if method is None or mock_stubs._csharp_any_test(method, data):
        return None
    words = _modifiers(method, data)
    if "private" not in words or words & _OPAQUE:
        return None
    if any(c.type in ("type_parameter_list", "attribute_list") for c in method.children):
        return None
    owner = method.parent.parent if method.parent is not None else None
    if owner is None or "partial" in _modifiers(owner, data):
        return None
    name_node = method.child_by_field_name("name")
    body = method.child_by_field_name("body")
    # A construction pin may sit in an expression body (`=> new(new Fake(on))`).
    if name_node is None or body is None or body.type not in ("block", "arrow_expression_clause"):
        return None
    name = _text(name_node, data)
    parameters = _parameters(method)
    index = next(
        (
            i
            for i, p in enumerate(parameters)
            if (n := p.child_by_field_name("name")) is not None and _text(n, data) == parameter
        ),
        None,
    )
    if index is None:
        return None
    declared = parameters[index]
    type_node = declared.child_by_field_name("type")
    if type_node is None or _text(type_node, data) != "bool":
        return None
    if any(c.type in ("modifier", "attribute_list") for c in declared.children):
        return None
    # A `params` array is no `parameter` node of its own: the list would not
    # say where the pinned one ends.
    listed = method.child_by_field_name("parameters")
    if any(c.type not in ("parameter", "comment") for c in listed.named_children):
        return None
    ok, default = _default(declared, data)
    if not ok:
        return None
    # Nothing but the stub mentions the parameter.
    for node in _walk(body):
        if node.type == "identifier" and _text(node, data).removeprefix("@") == parameter:
            if not (statement.start_byte <= node.start_byte < statement.end_byte):
                return None
    # A doc comment naming the parameter (`<param name="on">`, `<paramref>`)
    # would dangle: CS1572/CS1734, an error under TreatWarningsAsErrors.
    named = re.compile(rf"(?<![\w$@]){re.escape(parameter)}(?![\w$])")
    previous = method.prev_sibling
    while previous is not None and previous.type == "comment":
        if named.search(_text(previous, data)):
            return None
        previous = previous.prev_sibling
    word = re.compile(rf"(?<![\w$@]){re.escape(name)}(?![\w$])")
    if any(n.type in _STRINGS and word.search(_text(n, data)) for n in _walk(root)):
        return None
    pin = CsPin(name, parameter, index, default, method.start_byte)
    if calls(root, data, pin, depth) is None or edits(root, data, pin, depth) is None:
        return None
    return pin


def _argument_at(call: Node, pin: CsPin, data: bytes) -> tuple[Node | None, bool]:
    """``(the argument passed to the pinned parameter, whether it may be
    deleted)``: one named after it anywhere, or the positional one at its
    index when it is the last argument passed. ``(None, True)`` when none
    is passed; ``(None, False)`` when the list cannot be read."""
    arguments = call.child_by_field_name("arguments")
    items = [a for a in arguments.named_children if a.type == "argument"] if arguments is not None else []
    if arguments is None or any(c.type not in ("argument", "comment") for c in arguments.named_children):
        return None, False
    for item in items:
        label = item.child_by_field_name("name")
        if label is not None and _text(label, data) == pin.parameter:
            return item, True
    if len(items) > pin.index and items[pin.index].child_by_field_name("name") is None:
        item = items[pin.index]
        return item, items[-1].id == item.id
    return None, True


def calls(root: Node, data: bytes, pin: CsPin, depth: int = 0) -> list[tuple[Node, str]] | None:
    """Every call of the helper with the literal it pins; ``None`` when any
    mention is not a plain call, or a call cannot be pinned. A call passing
    another helper's parameter on stands for that helper's calls."""
    found: list[tuple[Node, str]] = []
    for node in _walk(root):
        if node.type != "identifier" or _text(node, data).removeprefix("@") != pin.method:
            continue
        parent = node.parent
        if parent is not None and parent.type == "method_declaration" and parent.start_byte == pin.method_start:
            if parent.child_by_field_name("name") == node:
                continue
        if parent is None or parent.type != "invocation_expression" or parent.child_by_field_name("function") != node:
            return None
        argument, deletable = _argument_at(parent, pin, data)
        if argument is None:
            if pin.default is None or not deletable:
                return None
            found.append((parent, pin.default))
            continue
        value = argument.named_children[-1] if argument.named_children else None
        if value is not None and value.type == "identifier" and deletable and theory_at(parent, pin, data) is not None:
            found.append((parent, ROWS))
            continue
        if value is not None and value.type == "identifier" and deletable:
            outer = _outer(root, data, parent, argument, value, depth)
            through = calls(root, data, outer, depth + 1) if outer is not None else None
            # A theory's rows would say the value of the OUTER pin, which the
            # caller asks of this one.
            if through is None or any(v == ROWS for _, v in through):
                return None
            found += through
            continue
        if value is None or value.type != "boolean_literal" or not deletable:
            return None
        found.append((parent, _text(value, data)))
    return found


def _outer(root: Node, data: bytes, call: Node, argument: Node, value: Node, depth: int) -> CsPin | None:
    """The pin of the helper around ``call`` that passes its own parameter
    ``value`` as ``argument``, read nowhere else in it; ``None`` past
    :data:`_CHAIN_DEPTH`, which also ends a cycle."""
    if depth >= _CHAIN_DEPTH:
        return None
    return find(root, data, argument, _text(value, data).removeprefix("@"), depth + 1)


def chained(root: Node, data: bytes, pin: CsPin, depth: int = 0) -> list[CsPin]:
    """The helpers whose parameter ``pin``'s calls pass on, at any depth:
    each is pinned with it, and its parameter and arguments go too."""
    found: list[CsPin] = []
    for node in _walk(root):
        if node.type != "invocation_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or function.type != "identifier" or _text(function, data).removeprefix("@") != pin.method:
            continue
        argument, deletable = _argument_at(node, pin, data)
        value = argument.named_children[-1] if argument is not None and argument.named_children else None
        if value is None or value.type != "identifier" or not deletable or theory_at(node, pin, data) is not None:
            continue
        outer = _outer(root, data, node, argument, value, depth)
        if outer is not None:
            found += [outer, *chained(root, data, outer, depth + 1)]
    return found


def _simple_attribute(attribute: Node, data: bytes) -> str:
    name = attribute.child_by_field_name("name")
    spelled = _text(name, data) if name is not None else ""
    return re.split(r"[.:]", spelled)[-1].strip().removesuffix("Attribute")


def theory_at(call: Node, pin: CsPin, data: bytes) -> TheoryRows | None:
    """The ``[Theory]`` whose own parameter ``call`` passes at the pin, when
    its rows can be cut: the method is marked ``[Theory]`` and ``[InlineData]``
    and nothing else, one attribute per list, each row naming no property
    and holding a ``true``/``false`` literal at the parameter's position, the
    parameter a plain ``bool`` with no default, read nowhere but at the call,
    and another parameter left once it goes (a theory with none is
    xUnit1006). ``None`` otherwise."""
    argument, _ = _argument_at(call, pin, data)
    value = argument.named_children[-1] if argument is not None and argument.named_children else None
    if value is None or value.type != "identifier":
        return None
    return _theory(value, argument.child_by_field_name("name"), data)


def theory_write(statement: Node, parameter: str, data: bytes) -> TheoryRows | None:
    """The ``[Theory]`` whose own parameter ``statement`` writes to a test
    fake's knob (``_gate.Enabled = on;``), as :func:`theory_at` judges one:
    the rows say the value, and the write goes with the parameter."""
    value = None
    for node in _walk(statement):
        if node.type == "assignment_expression":
            right = node.child_by_field_name("right")
            if right is not None and right.type == "identifier" and _text(right, data).removeprefix("@") == parameter:
                value = right
    if value is None:
        return None
    return _theory(value, None, data)


def _theory(value: Node, label: Node | None, data: bytes) -> TheoryRows | None:
    """:func:`theory_at`'s question, for the parameter ``value`` reads."""
    spelled = _text(value, data)
    method = _method_of(value)
    if method is None:
        return None
    listed = method.child_by_field_name("parameters")
    if listed is None or any(c.type not in ("parameter", "comment") for c in listed.named_children):
        return None
    parameters = _parameters(method)
    index = next(
        (i for i, p in enumerate(parameters) if (n := p.child_by_field_name("name")) is not None and _text(n, data) == spelled),
        None,
    )
    if index is None or len(parameters) < 2:
        return None
    declared = parameters[index]
    type_node = declared.child_by_field_name("type")
    if type_node is None or _text(type_node, data) != "bool":
        return None
    if any(c.type in ("modifier", "attribute_list") for c in declared.children):
        return None
    ok, default = _default(declared, data)
    if not ok or default is not None:
        return None
    theory = False
    rows: list[tuple[Node, Node, str]] = []
    for attribute_list in (c for c in method.children if c.type == "attribute_list"):
        attributes = [a for a in attribute_list.named_children if a.type == "attribute"]
        if len(attributes) != 1 or len(attribute_list.named_children) != 1:
            return None
        [attribute] = attributes
        arguments = next((c for c in attribute.named_children if c.type == "attribute_argument_list"), None)
        kind = _simple_attribute(attribute, data)
        if kind == "Theory" and arguments is None:
            theory = True
            continue
        if kind != "InlineData" or arguments is None:
            return None
        items = [a for a in arguments.named_children if a.type == "attribute_argument"]
        if len(items) != len(parameters) or any(
            c.type in ("=", ":") for item in items for c in item.children
        ):
            return None
        literal = items[index].named_children[-1] if items[index].named_children else None
        if literal is None or literal.type != "boolean_literal":
            return None
        rows.append((attribute_list, items[index], _text(literal, data)))
    if not theory or not rows:
        return None
    body = method.child_by_field_name("body")
    reads = [
        n
        for n in (_walk(body) if body is not None else ())
        if n.type == "identifier"
        and _text(n, data).removeprefix("@") == spelled
        and (label is None or not (label.start_byte <= n.start_byte < label.end_byte))
    ]
    if len(reads) != 1 or reads[0].id != value.id:
        return None
    name = method.child_by_field_name("name")
    return TheoryRows(method, _text(name, data) if name is not None else "", declared, tuple(rows))


def theory_spans(theory: TheoryRows, data: bytes, kept: str) -> list[tuple[int, int]] | None:
    """What cutting ``theory``'s rows takes: each row of the other value with
    its whole line, the column from each row of ``kept``, and the
    parameter. ``None`` when a row shares its line or a cut holds a comment;
    an empty list when no row is ``kept`` (the theory goes whole)."""
    if not any(value == kept for _, _, value in theory.rows):
        return []
    spans = [_list_span(theory.parameter)]
    for attribute_list, argument, value in theory.rows:
        if value == kept:
            spans.append(_list_span(argument))
            continue
        start = data.rfind(b"\n", 0, attribute_list.start_byte) + 1
        end = data.find(b"\n", attribute_list.end_byte)
        end = len(data) if end < 0 else end + 1
        if data[start : attribute_list.start_byte].strip() or data[attribute_list.end_byte : end].strip():
            return None
        spans.append((start, end))
    root = theory.method
    while root.parent is not None:
        root = root.parent
    if any(_holds_comment(root, start, end) for start, end in spans):
        return None
    return spans


def _list_span(item: Node) -> tuple[int, int]:
    """``item``'s span in its list with one separator: the one before it, or
    for the first item the one after it; the whole inside of the brackets
    for the only item."""
    container = item.parent
    items = [c for c in container.named_children if c.type == item.type]
    position = next(i for i, c in enumerate(items) if c.id == item.id)
    if len(items) == 1:
        return container.children[0].end_byte, container.children[-1].start_byte
    if position > 0:
        return items[position - 1].end_byte, item.end_byte
    return item.start_byte, items[1].start_byte


def _holds_comment(root: Node, start: int, end: int) -> bool:
    return any(
        n.type == "comment" and start < n.end_byte and n.start_byte < end for n in _walk(root)
    )


def edits(root: Node, data: bytes, pin: CsPin, depth: int = 0) -> list[tuple[int, int]] | None:
    """The parameter's span and each passed argument's span, with
    separators; ``None`` when one holds a comment, or a helper the pin's
    calls pass the parameter on to cannot be edited."""
    method = next(
        (n for n in _walk(root) if n.type == "method_declaration" and n.start_byte == pin.method_start), None
    )
    if method is None:
        return None
    spans = [_list_span(_parameters(method)[pin.index])]
    for call in _own_calls(root, data, pin):
        argument, _deletable = _argument_at(call, pin, data)
        if argument is not None:
            spans.append(_list_span(argument))
    if any(_holds_comment(root, start, end) for start, end in spans):
        return None
    if any(edits(root, data, outer, depth + 1) is None for outer in chained(root, data, pin, depth)):
        return None
    return spans


def _own_calls(root: Node, data: bytes, pin: CsPin) -> list[Node]:
    """Every call of the helper itself, those passing another helper's
    parameter on included."""
    found = []
    for node in _walk(root):
        if node.type != "invocation_expression":
            continue
        function = node.child_by_field_name("function")
        if function is not None and function.type == "identifier" and _text(function, data).removeprefix("@") == pin.method:
            found.append(node)
    return found
