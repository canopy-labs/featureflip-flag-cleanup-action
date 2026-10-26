"""Strip an override whose value arrives through a test helper's parameter.

The entry rules strip ``{ 'k': true }`` because its value is a boolean, which
is what makes the literal provably about flags. A helper that lets each test
choose the value hands the literal an identifier instead::

    const createWrapper = (seatCapOn = true) => ({ children }) => (
      <TestFeatureflipProvider flags={{ 'k': seatCapOn }}>…
    );

    it('falls back to a toast when k is off', () => {
      render(<Invite />, { wrapper: createWrapper(false) });
      …

No rule can see that ``seatCapOn`` is a boolean, so the entry used to stay, the
file stayed unchanged, and with it the test about the removed branch: it was
never chosen, because the pin it relies on is an ARGUMENT, two hops from the
literal. That test then asserts behaviour the code can no longer produce.

This module proves the shape and undoes it whole: the entry, the parameter
that fed it, and the matching argument at every call site. Each call then pins
the flag as plainly as a literal would — to its argument, or to the
parameter's default when it passes none — which is what lets
:func:`flag_cleanup.ts_tests.find_pinned_tests` choose the tests about the
removed branch exactly as it does for an inline pin.

Everything that has to hold, and each is checked rather than assumed:

* **The value is a boolean.** The parameter defaults to ``true``/``false`` or
  is annotated ``boolean``; the same bar the stub prong sets for a literal.
* **The parameter is the helper's LAST one**, a plain name, and nothing in the
  helper reads it except the entries keyed by this flag. Removing a last
  parameter shifts no other argument.
* **Every call is visible.** The helper is a ``const`` or a function
  declaration, not exported, declared once, and every other mention of its
  name is the callee of a call — never a value that could be called out of
  sight. Each call passes a boolean literal at the parameter's position, or
  nothing, and no spread.
* **No deletion takes a comment with it.** A comment between two parameters
  or arguments is the customer's, and nothing here can say where it belongs.

A shape that fails any of them is left exactly as written, and the residual
scan reports the file as it always did.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import ts_list_join
from flag_cleanup.ts_syntax import _parse, _walk

#: Nodes whose parameters bind names, as the TypeScript grammars spell them.
_FUNCTIONS = frozenset(
    {
        "arrow_function",
        "function_declaration",
        "function_expression",
        "generator_function",
        "generator_function_declaration",
        "method_definition",
    }
)

#: A function bound to a name by a ``const`` declarator, rather than declaring one.
_DECLARED_VALUES = frozenset({"arrow_function", "function_expression"})

#: Every node type that can name a binding in an expression or a pattern.
_NAMES = frozenset(
    {"identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"}
)

_BOOLEANS = frozenset({"true", "false"})


@dataclass(frozen=True)
class ParameterPin:
    """A helper whose parameter ``index`` named ``parameter`` feeds the flag's entry.

    ``default`` is the parameter's default literal, ``"true"`` or ``"false"``,
    or ``None`` when it has none — a call passing nothing then pins nothing.
    """

    function: str
    parameter: str
    index: int
    default: str | None


def find_parameter_pins(source: str, language: str, flag_key: str) -> list[ParameterPin]:
    """Every helper in ``source`` whose parameter is proven to feed ``flag_key``'s entry."""
    data = source.encode("utf-8")
    root = _parse(source, language)
    found: list[ParameterPin] = []
    for pair in _walk(root):
        value = pair.child_by_field_name("value") if pair.type == "pair" else None
        if value is None or value.type != "identifier" or not _keyed(pair, data, flag_key):
            continue
        pin = _qualify(root, data, flag_key, pair, _text(value, data))
        if pin is not None and pin not in found and _edits(root, data, flag_key, pin):
            found.append(pin)
    return found


def pinned_calls(source: str, language: str, flag_key: str) -> dict[tuple[int, int], str]:
    """``{(start, end): literal}`` for each call that pins the flag through a helper.

    The span is the call's byte range in ``source``; the literal is the
    argument it passes at the parameter's position, or the parameter's default
    when it passes none. A call relying on no default pins nothing.
    """
    pins = find_parameter_pins(source, language, flag_key)
    if not pins:
        return {}
    data = source.encode("utf-8")
    root = _parse(source, language)
    calls: dict[tuple[int, int], str] = {}
    for pin in pins:
        for call in _calls(root, data, pin.function):
            arguments = _elements(call.child_by_field_name("arguments"))
            value = (
                arguments[pin.index].type if len(arguments) > pin.index else pin.default
            )
            if value in _BOOLEANS:
                calls[(call.start_byte, call.end_byte)] = value
    return calls


def strip_parameter_pins(
    source: str,
    language: str,
    flag_key: str,
    pins: list[ParameterPin],
    width: int | None = None,
) -> tuple[str, list[str]]:
    """``source`` without each pin's entries, parameter and arguments.

    ``pins`` were found on the customer's bytes, and ``source`` may be the
    engine's output: an entry beside another flag's key is the registry
    prong's to delete, and only its parameter and arguments are then left
    here. Each pin is re-proven against ``source`` and skipped whole when it
    no longer holds. Returns the entries this deleted, as written.

    A parameter or argument list a deletion leaves with one element is put
    back on one line when it fits ``width``; see :mod:`flag_cleanup.ts_list_join`.
    """
    entries: list[str] = []
    for pin in pins:
        data = source.encode("utf-8")
        root = _parse(source, language)
        edits = _edits(root, data, flag_key, pin)
        if not edits:
            continue
        for start, end, entry in edits:
            if entry:
                entries.append(_entry_text(data, start, end))
        source = ts_list_join.splice(
            source, [(start, end) for start, end, _ in edits], language, width
        )
    return source, sorted(entries)


def _qualify(
    root: Node, data: bytes, flag_key: str, pair: Node, name: str
) -> ParameterPin | None:
    binder = _binder(pair, name, data)
    if binder is None:
        return None
    parameters = _elements(binder.child_by_field_name("parameters"))
    index = next(
        (n for n, p in enumerate(parameters) if _parameter_name(p, data) == name), None
    )
    if index is None or index != len(parameters) - 1:
        return None
    default = _boolean_evidence(parameters[index], data)
    if default is False:
        return None
    function = _helper_name(binder, data)
    if function is None:
        return None
    pin = ParameterPin(function, name, index, default or None)
    if _declaration(root, data, pin) != binder:
        return None
    return pin


def _edits(
    root: Node, data: bytes, flag_key: str, pin: ParameterPin
) -> list[tuple[int, int, bool]] | None:
    """The deletions undoing ``pin`` — ``(start, end, is_entry)`` — or ``None``.

    ``None`` whenever any one of them cannot be made cleanly: the pin is all or
    nothing, since a parameter removed without its arguments shifts nothing but
    leaves a call passing one argument too many, and the reverse breaks the call.
    """
    binder = _declaration(root, data, pin)
    if binder is None:
        return None
    parameters = _elements(binder.child_by_field_name("parameters"))
    if len(parameters) != pin.index + 1:
        return None
    parameter = parameters[pin.index]
    if _parameter_name(parameter, data) != pin.parameter:
        return None
    pattern = parameter.child_by_field_name("pattern")

    entries = [
        node.parent
        for node in _walk(binder)
        if node.type == "identifier"
        and _text(node, data) == pin.parameter
        and node.parent is not None
        and node.parent.type == "pair"
        and node.parent.child_by_field_name("value") == node
        and _keyed(node.parent, data, flag_key)
    ]
    entry_values = {entry.child_by_field_name("value").id for entry in entries}
    for node in _walk(binder):
        if (
            node.type in _NAMES
            and _text(node, data) == pin.parameter
            and node.id != pattern.id
            and node.id not in entry_values
        ):
            return None

    spans: list[tuple[int, int, bool]] = []
    for entry in entries:
        span = _element_span(entry)
        if span is None:
            return None
        spans.append((*span, True))
    span = _element_span(parameter)
    if span is None:
        return None
    spans.append((*span, False))
    for call in _calls(root, data, pin.function):
        arguments = _elements(call.child_by_field_name("arguments"))
        if len(arguments) <= pin.index:
            continue
        span = _element_span(arguments[pin.index])
        if span is None:
            return None
        spans.append((*span, False))

    ordered = sorted(spans)
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
        return None
    return spans


def _binder(pair: Node, name: str, data: bytes) -> Node | None:
    """The nearest function enclosing ``pair`` whose parameters bind ``name``."""
    ancestor = pair.parent
    while ancestor is not None:
        if ancestor.type in _FUNCTIONS:
            parameters = ancestor.child_by_field_name("parameters")
            if parameters is None:
                single = ancestor.child_by_field_name("parameter")
                if single is not None and _text(single, data) == name:
                    return ancestor
            elif any(
                node.type in _NAMES and _text(node, data) == name
                for node in _walk(parameters)
            ):
                return ancestor if parameters.type == "formal_parameters" else None
        ancestor = ancestor.parent
    return None


def _parameter_name(parameter: Node, data: bytes) -> str | None:
    """The name a ``required_parameter``/``optional_parameter`` binds, if a plain one."""
    if parameter.type not in ("required_parameter", "optional_parameter"):
        return None
    pattern = parameter.child_by_field_name("pattern")
    if pattern is None or pattern.type != "identifier":
        return None
    return _text(pattern, data)


def _boolean_evidence(parameter: Node, data: bytes) -> str | bool:
    """The default literal; ``""`` for a ``boolean`` annotation alone; ``False`` for neither.

    A default that is not a boolean literal is ``False`` even beside a
    ``boolean`` annotation: whatever it computes is a value this cannot read.
    """
    default = parameter.child_by_field_name("value")
    if default is not None:
        return default.type if default.type in _BOOLEANS else False
    annotation = parameter.child_by_field_name("type")
    if annotation is None:
        return False
    named = annotation.named_children
    if len(named) == 1 and named[0].type == "predefined_type" and _text(named[0], data) == "boolean":
        return ""
    return False


def _helper_name(binder: Node, data: bytes) -> str | None:
    """The name ``binder`` is declared under, when nothing outside the file can call it."""
    if binder.type == "function_declaration":
        holder = binder
        name = binder.child_by_field_name("name")
    elif binder.type in _DECLARED_VALUES:
        declarator = binder.parent
        if (
            declarator is None
            or declarator.type != "variable_declarator"
            or declarator.child_by_field_name("value") != binder
        ):
            return None
        holder = declarator.parent
        if holder is None or holder.type != "lexical_declaration" or holder.child(0).type != "const":
            return None
        name = declarator.child_by_field_name("name")
    else:
        return None
    if name is None or name.type != "identifier":
        return None
    if holder.parent is not None and holder.parent.type == "export_statement":
        return None
    return _text(name, data)


def _declaration(root: Node, data: bytes, pin: ParameterPin) -> Node | None:
    """The function declared as ``pin.function``, when every other mention calls it.

    Every mention of the name that is not the declaration itself must be the
    callee of a call whose arguments are all visible at the parameter's
    position and before it, and supply a boolean literal there or nothing.
    """
    declarations: list[tuple[Node, Node]] = []
    for node in _walk(root):
        if node.type == "function_declaration":
            name = node.child_by_field_name("name")
        elif node.type == "variable_declarator":
            name = node.child_by_field_name("name")
        else:
            continue
        if name is not None and name.type == "identifier" and _text(name, data) == pin.function:
            declarations.append((node, name))
    if len(declarations) != 1:
        return None
    holder, declared = declarations[0]
    binder = holder if holder.type == "function_declaration" else holder.child_by_field_name("value")
    if binder is None or binder.type not in _FUNCTIONS or _helper_name(binder, data) != pin.function:
        return None

    for node in _walk(root):
        if node.type not in _NAMES or _text(node, data) != pin.function or node.id == declared.id:
            continue
        call = node.parent
        if (
            call is None
            or call.type != "call_expression"
            or call.child_by_field_name("function") != node
        ):
            return None
        arguments = call.child_by_field_name("arguments")
        if arguments is None or arguments.type != "arguments":
            return None
        elements = _elements(arguments)
        if any(e.type == "spread_element" for e in elements[: pin.index + 1]):
            return None
        if len(elements) > pin.index and elements[pin.index].type not in _BOOLEANS:
            return None
    return binder


def _calls(root: Node, data: bytes, function: str) -> list[Node]:
    return [
        node
        for node in _walk(root)
        if node.type == "call_expression"
        and (callee := node.child_by_field_name("function")) is not None
        and callee.type == "identifier"
        and _text(callee, data) == function
    ]


def _element_span(node: Node) -> tuple[int, int] | None:
    """The bytes deleting ``node`` from its comma-separated list removes.

    The separator BEFORE it goes too, or after it when it is the first; the
    sole element takes the whole interior with it, so ``{ 'k': on }`` becomes
    ``{}`` as the entry rules leave it. ``None`` when the span holds a comment.
    """
    container = node.parent
    if container is None:
        return None
    elements = _elements(container)
    position = next((n for n, e in enumerate(elements) if e.id == node.id), None)
    if position is None:
        return None
    if len(elements) == 1:
        start, end = container.children[0].end_byte, container.children[-1].start_byte
    elif position > 0:
        start, end = elements[position - 1].end_byte, node.end_byte
    else:
        start, end = node.start_byte, elements[1].start_byte
    if any(
        child.type == "comment" and start <= child.start_byte and child.end_byte <= end
        for child in container.children
    ):
        return None
    return start, end


def _elements(container: Node | None) -> list[Node]:
    """A list's elements, without the comments the grammar names alongside them."""
    if container is None:
        return []
    return [child for child in container.named_children if child.type != "comment"]


def _keyed(pair: Node, data: bytes, flag_key: str) -> bool:
    """Whether ``pair``'s key is a plain string literal spelling ``flag_key``."""
    key = pair.child_by_field_name("key")
    if key is None or key.type != "string":
        return False
    fragments = key.named_children
    return (
        len(fragments) == 1
        and fragments[0].type == "string_fragment"
        and _text(fragments[0], data) == flag_key
    )


def _entry_text(data: bytes, start: int, end: int) -> str:
    """The entry a span deleted, without the separator and space that went with it."""
    return data[start:end].decode("utf-8").strip().strip(",").strip()


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")
