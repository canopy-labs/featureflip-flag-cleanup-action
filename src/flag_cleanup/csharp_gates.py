"""A C# attribute that gates what it decorates: ``[FeatureGate("k")]``.

Microsoft.FeatureManagement and in-house filters put a flag on a whole
controller or action with an attribute rather than an ``if``:

.. code-block:: csharp

    [FeatureGate("new-checkout")]
    public class CheckoutController : Controller

The customer names the attribute in ``accessors``, as for a function. An
attribute is not a call, so no accessor rule reaches it, and it is handled here
on the parsed tree.

While the flag stays on, the gate is open and the attribute does nothing, so
:func:`delete_gates` removes it, and its ``[…]`` when that is left empty. While
the flag stays off the gated action is unreachable, and removing it reaches
routes and tests, which a removal cannot see from here. So that treatment is
REFUSED, naming the attribute; see :func:`refusal_reason`.

Only an attribute whose one positional argument is the key counts. With more
than one, ``[FeatureGate("a", "b")]``, what is left depends on how they
combine, and it stands as any other mention of the key does.
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup import key_const, syntax


def _value(attribute: Node) -> Node | None:
    """The attribute's sole positional argument."""
    for child in attribute.named_children:
        if child.type == "attribute_argument_list":
            if len(child.named_children) == 1 and len(child.named_children[0].named_children) == 1:
                return child.named_children[0].named_children[0]
    return None


def _means_key(value: Node, flag_key: str, key_texts: frozenset[str], constants: frozenset[str]) -> bool:
    """Whether ``value`` spells this flag's key: a literal, a constant the
    resolution proved, or one declared in this file."""
    assert value.text is not None
    text = value.text.decode("utf-8")
    if value.type == "string_literal":
        return text == f'"{flag_key}"'
    if text in key_texts or text in constants:
        return True
    name = value.child_by_field_name("name") if value.type == "member_access_expression" else None
    return (
        name is not None
        and name.text is not None
        and name.text.decode("utf-8") in constants
        and key_const._csharp_qualified_by_owner(value)
    )


def find_gates(
    source: str, flag_key: str, names: frozenset[str], key_texts: frozenset[str]
) -> list[Node]:
    """Each attribute in ``names`` whose sole argument is this flag's key, in source order."""
    root = syntax._parse(source, "csharp")
    data = source.encode("utf-8")
    # A constant counts only where the key-const rules may run: it may stand
    # for another class's `Key` otherwise, and they would not delete it.
    constants = (
        frozenset(c.name for c in key_const.key_consts(root, data, "csharp", flag_key))
        if key_const.path_is_safe(root, data, "csharp", flag_key, names)
        else frozenset()
    )
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "attribute" or key_const._csharp_attribute_name(node) not in names:
            continue
        value = _value(node)
        if value is not None and _means_key(value, flag_key, key_texts, constants):
            found.append(node)
    return sorted(found, key=lambda node: node.start_byte)


def gate_texts(
    source: str, flag_key: str, names: frozenset[str], key_texts: frozenset[str]
) -> frozenset[str]:
    """How this file's gates spell the key.

    Asked of the customer's bytes, and what :func:`delete_gates` needs after
    the engine: by then a constant of this file is gone, and its name no longer
    says it was the key.
    """
    found = (_value(node) for node in find_gates(source, flag_key, names, key_texts))
    return frozenset(value.text.decode("utf-8") for value in found if value is not None and value.text)


def refusal_reason(
    source: str, flag_key: str, names: frozenset[str], key_texts: frozenset[str]
) -> str | None:
    """Why the ``false`` treatment cannot remove this file's gates, or ``None``."""
    found = find_gates(source, flag_key, names, key_texts)
    if not found:
        return None
    first = found[0]
    assert first.text is not None
    return (
        f"[{first.text.decode('utf-8')}] on line {first.start_point[0] + 1} gates what it "
        "decorates on this flag, and removing that for good reaches routes and tests, "
        "which this run does not do"
    )


def delete_gates(
    source: str, flag_key: str, names: frozenset[str], key_texts: frozenset[str]
) -> str:
    """``source`` without its gates on ``flag_key``, for the ``true`` treatment.

    ``key_texts`` comes from :func:`gate_texts`.
    """
    # One gate per parse: each edit moves every byte after it.
    while True:
        found = find_gates(source, flag_key, names, key_texts)
        if not found:
            return source
        data = source.encode("utf-8")
        start, end = _span(found[0], data)
        source = (data[:start] + data[end:]).decode("utf-8")


def _span(attribute: Node, data: bytes) -> tuple[int, int]:
    """The bytes to delete for ``attribute``."""
    listed = attribute.parent
    assert listed is not None
    attributes = [c for c in listed.named_children if c.type == "attribute"]
    if len(attributes) > 1:
        index = next(i for i, c in enumerate(attributes) if c.id == attribute.id)
        if index + 1 < len(attributes):
            return attribute.start_byte, attributes[index + 1].start_byte
        return attributes[index - 1].end_byte, attribute.end_byte
    start = data.rfind(b"\n", 0, listed.start_byte) + 1
    end = data.find(b"\n", listed.end_byte)
    end = len(data) if end < 0 else end + 1
    if not data[start : listed.start_byte].strip() and not data[listed.end_byte : end].strip():
        return start, end
    end = listed.end_byte
    while data[end : end + 1] in (b" ", b"\t"):
        end += 1
    return listed.start_byte, end
