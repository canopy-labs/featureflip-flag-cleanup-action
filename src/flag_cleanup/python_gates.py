"""A Python decorator that gates what it decorates: ``@flag_required("k")``.

Django projects put a flag on a whole view with a decorator rather than an
``if``:

.. code-block:: python

    @waffle_flag("new-checkout")
    def checkout(request): ...

    @method_decorator(waffle_flag("new-checkout"), name="dispatch")
    class CheckoutView(View): ...

The customer names the decorator in ``accessors``, as for a function. Unlike
a C# attribute, a decorator IS a call, so the accessor rule would reach it and
write ``@True``, which parses and raises ``TypeError`` when the module is
imported. The accessor rules therefore leave a read alone when it sits in a
decorator that calls the same accessor, or in a ``method_decorator``, and it
is decided here, on the parsed tree.

While the flag stays on, the gate is open and the decorator does nothing, so
:func:`delete_gates` removes its line. While the flag stays off the view is
unreachable, and removing it reaches routes and tests, which a removal cannot
see from here. So that treatment is REFUSED, naming the decorator; see
:func:`refusal_reason`.

Only a decorator whose one argument is the key is removed, alone or as the
first argument of ``method_decorator`` (with at most a ``name=`` beside it).
An accessor named with the position of its key (``flag_required[1]``) reads
the key in that slot, and its decorator may hold plain names, attributes and
literals in the slots before it, which a removal drops without running
anything. Any other decorator holding the read is refused in both
treatments, because what the extra arguments do when the gate is open is not
something this run can read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import key_const, syntax

#: Django's adapter from a function decorator to a method or class one.
_METHOD_DECORATOR = "method_decorator"

#: What a slot before the key may hold for its decorator to be removed:
#: evaluating any of these runs no code of the customer's (an attribute's
#: object is held to the same set).
_INERT = frozenset({"identifier", "string", "integer", "float", "true", "false", "none"})


@dataclass(frozen=True)
class Gate:
    """A decorator holding a read of this flag."""

    decorator: Node
    #: Why it cannot be removed in either treatment; ``None`` when it can.
    unsupported: str | None


def _text(node: Node) -> str:
    assert node.text is not None
    return node.text.decode("utf-8")


def _callee(call: Node) -> str | None:
    """The name a call calls: ``f`` for ``f(…)`` and ``m.f(…)``."""
    function = call.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "attribute":
        function = function.child_by_field_name("attribute")
    if function is None or function.type != "identifier":
        return None
    return _text(function)


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    if arguments is None or arguments.type != "argument_list":
        return []
    return [c for c in arguments.named_children if c.type != "comment"]


def _is_key(node: Node, flag_key: str, key_texts: frozenset[str]) -> bool:
    """Whether ``node`` spells this flag's key: a plain string, or a constant
    the resolution proved or this file declares."""
    if node.type == "string":
        parts = [c for c in node.named_children if c.type not in ("string_start", "string_end")]
        start = node.named_children[0] if node.named_children else None
        prefix = _text(start).lower() if start is not None else ""
        return (
            "f" not in prefix
            and "b" not in prefix
            and len(parts) == 1
            and parts[0].type == "string_content"
            and _text(parts[0]) == flag_key
        )
    return node.type in ("identifier", "attribute") and _text(node) in key_texts


class _Names:
    """The accessors, and the slot each reads its key from: 0 unless it was
    configured with one (``flag_required[1]``)."""

    def __init__(self, names: frozenset[str], positions: Mapping[str, int]) -> None:
        self.names = names
        self.positions = positions

    def __contains__(self, callee: object) -> bool:
        return callee in self.names

    def slot(self, callee: str | None) -> int:
        return self.positions.get(callee, 0) if callee is not None else 0


def _key_argument(node: Node, names: _Names, flag_key: str, key_texts: frozenset[str]) -> Node | None:
    """The argument that spells the key, when ``node`` is a call to an
    accessor holding this flag's key in the accessor's slot."""
    if node.type != "call":
        return None
    callee = _callee(node)
    if callee not in names:
        return None
    slot = names.slot(callee)
    arguments = _arguments(node)
    if len(arguments) <= slot or any(a.type == "keyword_argument" for a in arguments[: slot + 1]):
        return None
    return arguments[slot] if _is_key(arguments[slot], flag_key, key_texts) else None


def _reads(node: Node, names: _Names, flag_key: str, key_texts: frozenset[str]) -> bool:
    """Whether ``node`` is a call to an accessor whose key slot holds the key."""
    return _key_argument(node, names, flag_key, key_texts) is not None


def _only_the_key(call: Node, names: _Names) -> bool:
    """Whether a read passes nothing but the key, and before it only what
    evaluating runs no code: what a removal can drop."""
    arguments = _arguments(call)
    slot = names.slot(_callee(call))
    return len(arguments) == slot + 1 and all(_inert(a) for a in arguments[:slot])


def _inert(node: Node) -> bool:
    while node.type == "attribute":
        found = node.child_by_field_name("object")
        if found is None:
            return False
        node = found
    if node.type == "string":
        return not any(c.type == "interpolation" for c in node.named_children)
    return node.type in _INERT


def _holds_read(node: Node, names: _Names, flag_key: str, key_texts: frozenset[str]) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if _reads(current, names, flag_key, key_texts):
            return True
        stack.extend(current.named_children)
    return False


def _expression(decorator: Node) -> Node | None:
    found = [c for c in decorator.named_children if c.type != "comment"]
    return found[0] if len(found) == 1 else None


def _classify(
    decorator: Node, names: _Names, flag_key: str, key_texts: frozenset[str]
) -> Gate | None:
    """The gate ``decorator`` is on this flag, or ``None`` when it is not one.

    The positions are the ones the accessor rules leave alone: a decorator
    calling an accessor, and a ``method_decorator``.
    """
    expression = _expression(decorator)
    if expression is None or expression.type != "call":
        return None
    callee = _callee(expression)
    if callee in names:
        if not _holds_read(expression, names, flag_key, key_texts):
            return None
        if _reads(expression, names, flag_key, key_texts) and _only_the_key(expression, names):
            return Gate(decorator, None)
        return Gate(decorator, "it passes the decorator more than the flag key")
    if callee != _METHOD_DECORATOR or not _holds_read(expression, names, flag_key, key_texts):
        return None
    arguments = _arguments(expression)
    rest = arguments[1:]
    if arguments and _reads(arguments[0], names, flag_key, key_texts) and not _only_the_key(arguments[0], names):
        return Gate(decorator, "it passes the decorator more than the flag key")
    if (
        arguments
        and _reads(arguments[0], names, flag_key, key_texts)
        and all(_is_name_keyword(argument) for argument in rest)
        and len(rest) <= 1
    ):
        return Gate(decorator, None)
    return Gate(decorator, "the flag's decorator sits among other decorators or arguments")


def _is_name_keyword(argument: Node) -> bool:
    """``name="dispatch"``: which method a class decorator applies to."""
    if argument.type != "keyword_argument":
        return False
    name = argument.child_by_field_name("name")
    value = argument.child_by_field_name("value")
    return name is not None and _text(name) == "name" and value is not None and value.type == "string"


def _key_texts(root: Node, data: bytes, flag_key: str, names: frozenset[str], key_texts: frozenset[str]) -> frozenset[str]:
    """``key_texts`` plus this file's own constants holding the key, where the
    key-constant rules may run: elsewhere the name may stand for another
    value by the time the decorator reads it."""
    if not key_const.path_is_safe(root, data, "python", flag_key, names):
        return key_texts
    return key_texts | frozenset(c.name for c in key_const.key_consts(root, data, "python", flag_key))


def find_gates(
    source: str,
    flag_key: str,
    names: frozenset[str],
    key_texts: frozenset[str] = frozenset(),
    positions: Mapping[str, int] | None = None,
) -> list[Gate]:
    """Each decorator holding a read of this flag through ``names``, in source
    order. ``positions`` gives the slot of the key for an accessor whose key
    is not its first argument."""
    if not names:
        return []
    root = syntax._parse(source, "python")
    data = source.encode("utf-8")
    texts = _key_texts(root, data, flag_key, names, key_texts)
    accessors = _Names(names, positions or {})
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "decorator":
            continue
        gate = _classify(node, accessors, flag_key, texts)
        if gate is not None:
            found.append(gate)
    return sorted(found, key=lambda gate: gate.decorator.start_byte)


def gate_texts(
    source: str,
    flag_key: str,
    names: frozenset[str],
    key_texts: frozenset[str] = frozenset(),
    positions: Mapping[str, int] | None = None,
) -> frozenset[str]:
    """How this file's removable gates spell the key.

    Asked of the customer's bytes, and what :func:`delete_gates` needs after
    the engine: by then a constant of this file may be gone, and its name no
    longer says it was the key.
    """
    accessors = _Names(names, positions or {})
    spelled: set[str] = set()
    for gate in find_gates(source, flag_key, names, key_texts, positions):
        if gate.unsupported is not None:
            continue
        stack = [gate.decorator]
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type == "call" and _callee(node) in names:
                arguments = _arguments(node)
                slot = accessors.slot(_callee(node))
                if len(arguments) > slot:
                    spelled.add(_text(arguments[slot]))
    return frozenset(spelled)


def _where(gate: Gate) -> str:
    text = " ".join(_text(gate.decorator).split())
    return f"{text} on line {gate.decorator.start_point[0] + 1}"


def refusal_reason(
    source: str,
    flag_key: str,
    names: frozenset[str],
    treatment: bool,
    key_texts: frozenset[str] = frozenset(),
    positions: Mapping[str, int] | None = None,
) -> str | None:
    """Why ``treatment`` cannot remove this file's gates, or ``None``."""
    for gate in find_gates(source, flag_key, names, key_texts, positions):
        if gate.unsupported is not None:
            return f"{_where(gate)} uses the flag read as a decorator, and {gate.unsupported}"
        if not treatment:
            return (
                f"{_where(gate)} gates what it decorates on this flag, and removing that for "
                "good reaches routes and tests, which this run does not do"
            )
    return None


def delete_gates(
    source: str,
    flag_key: str,
    names: frozenset[str],
    key_texts: frozenset[str],
    positions: Mapping[str, int] | None = None,
) -> str:
    """``source`` without its gates on ``flag_key``, for the ``true`` treatment.

    ``key_texts`` comes from :func:`gate_texts`.
    """
    # One gate per parse: each edit moves every byte after it.
    while True:
        found = [
            gate
            for gate in find_gates(source, flag_key, names, key_texts, positions)
            if gate.unsupported is None
        ]
        if not found:
            return source
        data = source.encode("utf-8")
        start, end = _span(found[0].decorator, data)
        source = (data[:start] + data[end:]).decode("utf-8")


def _span(decorator: Node, data: bytes) -> tuple[int, int]:
    """The decorator's whole lines, with a comment after it on its last line."""
    start = data.rfind(b"\n", 0, decorator.start_byte) + 1
    end = data.find(b"\n", decorator.end_byte)
    end = len(data) if end < 0 else end + 1
    return start, end
