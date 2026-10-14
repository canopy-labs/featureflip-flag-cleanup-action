"""Find the C# primary-constructor parameters a fold left unread.

``class Svc(IClient client)`` declares a parameter the whole type can read.
When every read of it was a flag read the run folded, the parameter stays in
the constructor with nothing reading it, and the compiler warns CS9113
(``Parameter 'client' is unread``) — an error in any project built with
``TreatWarningsAsErrors``. A field assigned from a constructor parameter has no
such warning, so this is the one place a fold that leaves a client behind
breaks the build.

The parameter is reported, never deleted: removing it changes the constructor
every ``new Svc(…)`` and every registration that constructs the type calls,
and those live in files this run may not even list. The pull request names it
so the reviewer can make that change.

The same differential as :mod:`flag_cleanup.unused` decides it: **a parameter
is reported only if it was read BEFORE the fold and is read nowhere AFTER
it**, so a parameter the customer had already left unread is not blamed on
this run. Counting is by identifier, and errs towards "still read": a local or
a member of the same name counts as a read, so a mistake costs a missed note. A ``record`` is excluded (its parameters become properties,
so they are never unread), and so is a ``partial`` type, whose other parts can
read the parameter from files this pass does not see.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from flag_cleanup.unused import _parse

#: The declarations whose primary-constructor parameters can be left unread.
#: ``record_declaration`` is deliberately absent; see the module docstring.
_TYPES = frozenset({"class_declaration", "struct_declaration"})

#: Every type declaration. A nested type cannot read its container's
#: primary-constructor parameters (it has no instance of the container), so
#: none of its identifiers is counted as a read of them.
_NESTED = _TYPES | {
    "record_declaration",
    "interface_declaration",
    "enum_declaration",
    "delegate_declaration",
}


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _primary_parameters(declaration: Node) -> Node | None:
    for child in declaration.children:
        if child.type == "parameter_list":
            return child
    return None


def _is_partial(declaration: Node) -> bool:
    return any(
        child.type == "modifier" and child.text == b"partial"
        for child in declaration.children
    )


def _reads(declaration: Node, parameters: Node, name: str) -> int:
    """Identifiers spelling ``name`` in ``declaration``, outside its own
    parameter list, outside nested types and outside the member name of a
    ``x.name`` access."""
    count = 0
    stack = [child for child in declaration.children if child != parameters]
    while stack:
        node = stack.pop()
        if node.type in _NESTED:
            continue
        stack.extend(node.children)
        if node.type != "identifier" or node.text.decode("utf-8", "replace") != name:
            continue
        parent = node.parent
        if (
            parent is not None
            and parent.type == "member_access_expression"
            and parent.child_by_field_name("name") == node
        ):
            continue
        count += 1
    return count


def _parameters(source: str) -> dict[tuple[str, str], tuple[int, str]]:
    """``(type, parameter) -> (reads, label)`` for each primary-constructor
    parameter of a non-partial class or struct in ``source``.

    The key is by name, not position, so an edit elsewhere in the file cannot
    pair a parameter with the wrong one. A type name declared twice in the
    file (nested in two different types) is dropped: the pairing would be a
    guess.
    """
    found: dict[tuple[str, str], tuple[int, str]] = {}
    seen: set[str] = set()
    duplicated: set[str] = set()
    for declaration in _walk(_parse(source, "csharp")):
        if declaration.type not in _TYPES:
            continue
        type_name_node = declaration.child_by_field_name("name")
        parameters = _primary_parameters(declaration)
        if type_name_node is None or parameters is None:
            continue
        type_name = type_name_node.text.decode("utf-8", "replace")
        if type_name in seen:
            duplicated.add(type_name)
        seen.add(type_name)
        if _is_partial(declaration):
            continue
        for parameter in parameters.named_children:
            if parameter.type != "parameter":
                continue
            name_node = parameter.child_by_field_name("name")
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8", "replace")
            label = f"{type_name}({parameter.text.decode('utf-8', 'replace')})"
            found[(type_name, name)] = (_reads(declaration, parameters, name), label)
    return {key: value for key, value in found.items() if key[0] not in duplicated}


def unread_parameters(before: str, after: str) -> list[str]:
    """Each primary-constructor parameter read in ``before`` and not in ``after``.

    Returned as ``Type(<parameter as written>)`` — ``Svc(IClient client)`` —
    in the order the types appear in ``after``.
    """
    old = _parameters(before)
    return [
        label
        for key, (reads, label) in _parameters(after).items()
        if reads == 0 and old.get(key, (0, ""))[0] > 0
    ]
