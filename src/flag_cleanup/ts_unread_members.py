"""Find the TypeScript and JavaScript private members a fold left unread.

``constructor(private client: Client) {}`` declares a property only its class
can read. When every read of it was a flag read the run folded, the property
stays with nothing reading it, and ``tsc`` reports it under ``noUnusedLocals``:
TS6138 for a constructor parameter property, TS6133 for a field, method or
accessor. A ``#client`` member is TS6133 too, and ESLint's recommended
``no-unused-private-class-members`` reports it in JavaScript as well. An
Angular or NestJS service that injects a flags client only to call it is the
usual shape.

This module only finds the member. A field's initializer, or the constructor
statement that assigns it, may do work, so a field is removed only when
:mod:`flag_cleanup.ts_field_removal` proves neither does. A parameter property
changes the constructor every ``new Svc(…)`` and every dependency-injection
registration calls, so it is removed only when
:mod:`flag_cleanup.ctor_param_removal` can prove every one of those callers.
Anything else is named in the pull request.

The same differential as :mod:`flag_cleanup.primary_constructor` decides it:
**a member is reported only if it was read BEFORE the fold and is read nowhere
AFTER it**, so a member the customer had already left unread is not blamed on
this run. Only ``private`` and ``#`` members qualify, since anything else may
be read from another file. Reads are counted inside the class, nested classes
included (they can reach an outer instance's private members), and the count
errs towards "still read": any property name, shorthand or string spelling the
member counts, except the member access a plain ``=`` assigns to, which
``tsc`` does not count as a read either. A mistake costs a missed note. A bare
identifier is NOT a read of a parameter property: inside the constructor it
names the parameter, and ``tsc`` still reports the property (measured).

A class with no name is skipped, and so is a class name declared twice in one
file, or a member name declared twice in one class (a getter and setter, an
overload set): the pairing would be a guess.

:func:`unread_constructor_parameters` asks the same differential of a PLAIN constructor
parameter, ``constructor(c: Client)``, which is a local of the constructor and
not a property: TS6133 under ``noUnusedParameters``, and ESLint's
``no-unused-vars`` when it is the last one. Its reads are bare names inside
the constructor. The usual way the run strands one is not a fold but a
removal: a subclass whose ``super(c, l)`` loses the argument ``c`` once the
base class's parameter goes.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from flag_cleanup import ctor_param_ts, ts_scope
from flag_cleanup.unused import _parse

_CLASSES = frozenset({"class_declaration", "abstract_class_declaration", "class"})

#: Nodes whose text, when it spells a member's name, is counted as a read.
_NAMES = frozenset(
    {
        "property_identifier",
        "private_property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "string_fragment",
    }
)

_PARAMETERS = frozenset({"required_parameter", "optional_parameter"})
#: Any of these makes a constructor parameter a property (`readonly flags`).
_PROPERTY_MODIFIERS = frozenset({"accessibility_modifier", "readonly", "override_modifier"})


def _walk(node: Node) -> Iterator[Node]:
    """Pre-order, in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _is_private(member: Node, name: Node) -> bool:
    return name.type == "private_property_identifier" or any(
        child.type == "accessibility_modifier" and child.text == b"private"
        for child in member.children
    )


def _declarations(body: Node) -> tuple[list[tuple[str, Node]], set[str]]:
    """``([(name, name_node)], duplicated)``: each private member of ``body``
    in source order, and every name the body declares more than once."""
    private: list[tuple[str, Node]] = []
    seen: set[str] = set()
    duplicated: set[str] = set()

    def declare(name_node: Node, member: Node) -> None:
        name = name_node.text.decode("utf-8", "replace")
        if name in seen:
            duplicated.add(name)
        seen.add(name)
        if _is_private(member, name_node):
            private.append((name, name_node))

    for member in body.named_children:
        name_node = member.child_by_field_name("name")
        if name_node is None:
            continue
        if member.type == "method_definition" and name_node.text == b"constructor":
            parameters = member.child_by_field_name("parameters")
            for parameter in parameters.named_children if parameters else ():
                pattern = parameter.child_by_field_name("pattern")
                if (
                    parameter.type in _PARAMETERS
                    and pattern is not None
                    and pattern.type == "identifier"
                    and any(c.type == "accessibility_modifier" for c in parameter.children)
                ):
                    declare(pattern, parameter)
            continue
        declare(name_node, member)
    return private, duplicated


def _assigned(node: Node) -> bool:
    """Whether ``node`` is the property of a member access a plain ``=``
    assigns to: ``this.client = c`` writes ``client`` without reading it."""
    access = node.parent
    if access is None or access.type != "member_expression":
        return False
    if access.child_by_field_name("property") != node:
        return False
    assignment = access.parent
    return (
        assignment is not None
        and assignment.type == "assignment_expression"
        and assignment.child_by_field_name("left") == access
    )


def _reads(body: Node, names: set[str], declared: set[tuple[int, int]]) -> dict[str, int]:
    counts = dict.fromkeys(names, 0)
    for node in _walk(body):
        if node.type not in _NAMES:
            continue
        text = node.text.decode("utf-8", "replace")
        if text not in counts or (node.start_byte, node.end_byte) in declared:
            continue
        if _assigned(node):
            continue
        counts[text] += 1
    return counts


def _members(source: str, grammar: str) -> dict[tuple[str, str], int] | None:
    """``(class, member) -> reads`` for each private member of a named class
    in ``source``, in source order. ``None`` when the file does not parse."""
    root = _parse(source, grammar)
    if root.has_error:
        return None
    found: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    duplicated: set[str] = set()
    for declaration in _walk(root):
        if declaration.type not in _CLASSES:
            continue
        name_node = declaration.child_by_field_name("name")
        body = declaration.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        class_name = name_node.text.decode("utf-8", "replace")
        if class_name in seen:
            duplicated.add(class_name)
        seen.add(class_name)
        private, twice = _declarations(body)
        private = [(name, node) for name, node in private if name not in twice]
        declared = {(node.start_byte, node.end_byte) for _, node in private}
        reads = _reads(body, {name for name, _ in private}, declared)
        for name, _ in private:
            found[(class_name, name)] = reads[name]
    return {key: value for key, value in found.items() if key[0] not in duplicated}


def unread_members(before: str, after: str, grammar: str) -> list[str]:
    """Each private member read in ``before`` and not in ``after``.

    Returned as ``Class.member`` (``Svc.client``, ``Svc.#client``) in the
    order they appear in ``after``. ``grammar`` is a key of the parser table
    in :mod:`flag_cleanup.unused`: ``"ts"``, ``"tsx"`` or ``"js"``.
    """
    old = _members(before, grammar)
    new = _members(after, grammar)
    if old is None or new is None:
        return []
    return [
        f"{class_name}.{name}"
        for (class_name, name), reads in new.items()
        if reads == 0 and old.get((class_name, name), 0) > 0
    ]


def _parameters(source: str, grammar: str) -> dict[tuple[str, str], int] | None:
    """``(class, parameter) -> reads`` for each plain parameter of a named
    class's one constructor in ``source``. ``None`` when the file does not parse."""
    root = _parse(source, grammar)
    if root.has_error:
        return None
    found: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    duplicated: set[str] = set()
    for declaration in _walk(root):
        if declaration.type not in _CLASSES:
            continue
        name_node = declaration.child_by_field_name("name")
        body = declaration.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        class_name = name_node.text.decode("utf-8", "replace")
        if class_name in seen:
            duplicated.add(class_name)
        seen.add(class_name)
        ctors = ctor_param_ts._constructors(body)
        if len(ctors) != 1 or ctors[0].type != "method_definition":
            continue  # none, or an overload set: which signature is unclear
        parameters = ctors[0].child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters else ():
            pattern = parameter.child_by_field_name("pattern")
            if (
                parameter.type not in _PARAMETERS
                or pattern is None
                or pattern.type != "identifier"
                or any(c.type in _PROPERTY_MODIFIERS for c in parameter.children)
            ):
                continue
            name = pattern.text.decode("utf-8", "replace")
            if any(b != pattern for b in ts_scope.bindings(ctors[0], name)):
                # Another binding of the name inside the constructor (a
                # callback's parameter, a local, an assignment): which reads
                # are the parameter's is a scope question this does not ask.
                continue
            found[(class_name, name)] = sum(
                1
                for node in _walk(ctors[0])
                # `eval` can read any local by name, so it counts as a read.
                if node.type in ("identifier", "shorthand_property_identifier")
                and (node.text == pattern.text or node.text == b"eval")
                and node != pattern
            )
    return {key: value for key, value in found.items() if key[0] not in duplicated}


def unread_constructor_parameters(before: str, after: str, grammar: str) -> list[str]:
    """Each plain constructor parameter read in ``before`` and not in ``after``.

    Returned as ``Class(parameter)`` (``D(c)``), in the order they appear in
    ``after``. Any spelling of the name in the constructor counts as a read,
    and so does ``eval``. A parameter whose name the constructor binds a
    second time, anywhere, is skipped on both sides: counting the shadowing
    binding's reads BEFORE the run would blame a parameter the customer had
    already left unread. Either mistake costs a missed note.
    """
    old = _parameters(before, grammar)
    new = _parameters(after, grammar)
    if old is None or new is None:
        return []
    return [
        f"{class_name}({name})"
        for (class_name, name), reads in new.items()
        if reads == 0 and old.get((class_name, name), 0) > 0
    ]
