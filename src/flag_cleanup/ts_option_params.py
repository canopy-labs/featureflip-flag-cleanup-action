"""One option of a destructured options parameter, and what each call passes for it.

A TypeScript helper often takes its settings as one object::

    function renderGate({ ssoFlag = false } = {}) { … }

    renderGate();
    renderGate({ ssoFlag: true });

Removing such an option means three edits that have to agree: the binding in
the parameter's pattern (with its member in an inline object type), the
property at every call that sets it, and, when it was the only option, the
whole parameter and the argument each call passed for it. This module reads
the shape and computes those deletions. It decides nothing about flags: which
option goes, and whether every call is in sight, is the caller's proof.

What it accepts is narrow on purpose, and anything else is ``None``:

* **The binding** is a shorthand member with a default, ``{ name = <value> }``,
  of an object pattern that is a whole ``required_parameter``. No other member
  of the pattern names the option, and none is a rest element, which would
  collect the property once the binding went. The parameter defaults to
  ``{}`` or to nothing: any other default would decide the option's value for
  a call that passes no argument, out of sight of the binding's own default.
* **Each argument** is absent or an object literal whose members are plain
  properties: a key that is an identifier, a plain string or a number, or a
  shorthand. A spread, a computed key or a method could set the option without
  spelling it, so they refuse. The option itself appears at most once.
* **No deletion takes a comment with it.**
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

#: The members of a call's object literal that spell their key.
_PLAIN_KEYS = frozenset({"property_identifier", "string", "number"})


@dataclass(frozen=True)
class Option:
    """The option ``name`` as the parameter ``parameter`` binds it."""

    name: str
    #: The ``required_parameter`` whose pattern binds the option.
    parameter: Node
    #: The ``object_assignment_pattern`` member, ``name = <default>``.
    member: Node
    #: Whether the option is the pattern's only member.
    sole: bool

    @property
    def binder(self) -> Node:
        """The leaf that binds the name."""
        return self.member.child_by_field_name("left")

    @property
    def default(self) -> Node:
        """The default's value node."""
        return self.member.child_by_field_name("right")


@dataclass(frozen=True)
class Passed:
    """What one call passes for an option."""

    #: The object literal passed at the parameter's position; ``None`` when
    #: the call passes no argument there.
    argument: Node | None = None
    #: The property setting the option; ``None`` when the call does not set it.
    pair: Node | None = None

    @property
    def value(self) -> Node | None:
        """The value the call sets the option to, if it sets it."""
        return self.pair.child_by_field_name("value") if self.pair is not None else None


def option(parameter: Node, name: str, data: bytes) -> Option | None:
    """``parameter``'s option ``name``, or ``None`` when it is not one this reads."""
    if parameter.type != "required_parameter":
        return None
    pattern = parameter.child_by_field_name("pattern")
    if pattern is None or pattern.type != "object_pattern":
        return None
    default = parameter.child_by_field_name("value")
    if default is not None and (default.type != "object" or default.named_children):
        return None
    members = elements(pattern)
    found: Node | None = None
    for member in members:
        if member.type == "rest_pattern":
            return None
        if _binds(member, data) != name:
            continue
        left = member.child_by_field_name("left")
        if (
            found is not None
            or member.type != "object_assignment_pattern"
            or left is None
            or left.type != "shorthand_property_identifier_pattern"
            or member.child_by_field_name("right") is None
        ):
            return None
        found = member
    if found is None:
        return None
    return Option(name, parameter, found, sole=len(members) == 1)


def passed(argument: Node | None, name: str, data: bytes) -> Passed | None:
    """What ``argument`` passes for the option ``name``; ``None`` when unreadable."""
    if argument is None:
        return Passed()
    if argument.type != "object":
        return None
    pair: Node | None = None
    for member in elements(argument):
        if member.type == "shorthand_property_identifier":
            if _text(member, data) == name:
                return None
            continue
        if member.type != "pair":
            return None
        key = member.child_by_field_name("key")
        if key is None or key.type not in _PLAIN_KEYS:
            return None
        spelled = _key_text(key, data)
        if spelled is None:
            return None
        if spelled == name:
            if pair is not None:
                return None
            pair = member
    return Passed(argument, pair)


def declaration_spans(found: Option, data: bytes) -> list[tuple[int, int]] | None:
    """The bytes removing the option from its declaration takes.

    The whole parameter when it was the only option, which the caller must
    have proven is the LAST one. Otherwise the member, and its property in an
    inline object type annotation; a named type is left alone, so it refuses
    rather than leaving the type and the pattern disagreeing.
    """
    if found.sole:
        if _holds_comment(found.parameter):
            return None
        span = element_span(found.parameter)
        return None if span is None else [span]
    spans: list[tuple[int, int]] = []
    span = element_span(found.member)
    if span is None or _holds_comment(found.member):
        return None
    spans.append(span)
    annotation = found.parameter.child_by_field_name("type")
    if annotation is not None:
        types = annotation.named_children
        if len(types) != 1 or types[0].type != "object_type":
            return None
        members = [
            m
            for m in elements(types[0])
            if (key := m.child_by_field_name("name")) is not None
            and key.type in _PLAIN_KEYS
            and _key_text(key, data) == found.name
        ]
        if len(members) > 1:
            return None
        if members:
            span = element_span(members[0])
            if span is None or _holds_comment(members[0]):
                return None
            spans.append(span)
    return spans


def argument_spans(found: Option, call: Passed) -> list[tuple[int, int]] | None:
    """The bytes removing the option from one call's argument takes.

    Nothing when the call does not set it. When the option was the only one,
    the whole argument goes, so it must hold nothing else and be the call's
    last. Otherwise the property goes, and the argument with it when that
    empties an argument the call passes last to a parameter defaulting to
    ``{}``: ``f({})`` then means what ``f()`` does.
    """
    argument = call.argument
    if argument is None:
        return []
    arguments = argument.parent
    last = (
        arguments is not None
        and arguments.type == "arguments"
        and elements(arguments)[-1].id == argument.id
    )
    members = elements(argument)
    if found.sole:
        if not last or any(call.pair is None or m.id != call.pair.id for m in members):
            return None
        if _holds_comment(argument):
            return None
        span = element_span(argument)
        return None if span is None else [span]
    if call.pair is None:
        return []
    defaulted = found.parameter.child_by_field_name("value") is not None
    if len(members) == 1 and last and defaulted:
        if _holds_comment(argument):
            return None
        span = element_span(argument)
    else:
        span = None if _holds_comment(call.pair) else element_span(call.pair)
    return None if span is None else [span]


def element_span(node: Node) -> tuple[int, int] | None:
    """The bytes deleting ``node`` from its comma-separated list removes.

    The separator BEFORE it goes too, or after it when it is the first; the
    sole element takes the whole interior with it, so ``{ 'k': on }`` becomes
    ``{}`` and ``f(x)`` becomes ``f()``. ``None`` when the span holds a comment.
    """
    container = node.parent
    if container is None:
        return None
    members = elements(container)
    position = next((n for n, e in enumerate(members) if e.id == node.id), None)
    if position is None:
        return None
    if len(members) == 1:
        start, end = container.children[0].end_byte, container.children[-1].start_byte
    elif position > 0:
        start, end = members[position - 1].end_byte, node.end_byte
    else:
        start, end = node.start_byte, members[1].start_byte
    if any(
        child.type == "comment" and start <= child.start_byte and child.end_byte <= end
        for child in container.children
    ):
        return None
    return start, end


def elements(container: Node | None) -> list[Node]:
    """A list's elements, without the comments the grammar names alongside them."""
    if container is None:
        return []
    return [child for child in container.named_children if child.type != "comment"]


def _binds(member: Node, data: bytes) -> str | None:
    """The property name a pattern member destructures, when it spells one."""
    if member.type == "shorthand_property_identifier_pattern":
        return _text(member, data)
    if member.type == "object_assignment_pattern":
        left = member.child_by_field_name("left")
        return _text(left, data) if left is not None else None
    if member.type == "pair_pattern":
        key = member.child_by_field_name("key")
        return _key_text(key, data) if key is not None else None
    return None


def _key_text(key: Node, data: bytes) -> str | None:
    """The name a plain key spells; ``None`` for a string it cannot read plainly."""
    if key.type != "string":
        return _text(key, data)
    fragments = key.named_children
    if not fragments:
        return ""
    if len(fragments) == 1 and fragments[0].type == "string_fragment":
        return _text(fragments[0], data)
    return None


def _holds_comment(node: Node) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "comment":
            return True
        stack.extend(current.children)
    return False


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")
