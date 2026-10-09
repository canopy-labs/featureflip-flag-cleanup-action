"""Which hoisted key constants another file could possibly read.

Deleting a key constant's declaration is only unsafe when something outside the
file can name it. A ``private`` member, a non-exported module binding or a local
cannot be named from another file, so a textual hit for the same identifier
elsewhere (the next class's own ``FlagKey``) says nothing about it. This module
answers, per declaration, whether it is visible outside its file and what a
reader in another file would have to mention to reach it:

* the constant's NAME, always;
* the enclosing TYPE's name for a type member (qualified access, a static
  import, ``using static``, inheritance and interface implementation all name
  the owner);
* nothing about the directory, except for an unexported Go name, which only the
  declaring package (the same directory) can read.

The result is deliberately an over-approximation of "could be read": a
declaration whose visibility is unclear is reported visible, because the cost
of a wrong "visible" is one declaration kept and the cost of a wrong "not
visible" is a dangling reference.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from tree_sitter import Node

from flag_cleanup.key_const import KeyConst


class VisibleKeyConst(NamedTuple):
    """One declaration another file might read."""

    name: str
    #: The enclosing type's name, or ``None`` for a file/module/package-level one.
    owner: str | None
    #: True when only files in the declaring file's directory can read it.
    same_directory_only: bool = False


_TYPE_NODES: dict[str, frozenset[str]] = {
    "csharp": frozenset(
        {
            "class_declaration",
            "struct_declaration",
            "interface_declaration",
            "record_declaration",
            "record_struct_declaration",
            "enum_declaration",
        }
    ),
    "java": frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }
    ),
    "kt": frozenset({"class_declaration", "object_declaration"}),
    "php": frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "trait_declaration",
            "enum_declaration",
        }
    ),
    "swift": frozenset({"class_declaration", "protocol_declaration"}),
    "dart": frozenset(
        {
            "class_definition",
            "enum_declaration",
            "mixin_declaration",
            "extension_declaration",
            "extension_type_declaration",
        }
    ),
    "ruby": frozenset({"class", "module"}),
}

#: Ancestors that make a declaration a local one.
_LOCAL_SCOPES = frozenset(
    {
        "function_declaration",
        "function_definition",
        "function_body",
        "method_declaration",
        "method_definition",
        "constructor_declaration",
        "lambda_literal",
        "anonymous_function",
        "local_function_statement",
        "statement_block",
        "compound_statement",
        "block",
        "statements",
    }
)

_NAME_CHILDREN = frozenset(
    {"identifier", "type_identifier", "simple_identifier", "name", "constant"}
)
_TS_LANGUAGES = frozenset({"ts", "tsx", "js"})


def _tokens(node: Node, kinds: tuple[str, ...]) -> set[str]:
    """Words of the direct children of ``node`` whose type is in ``kinds``."""
    words: set[str] = set()
    for child in node.children:
        if child.type in kinds:
            words.update((child.text or b"").decode("utf-8", "replace").split())
    return words


def _is_local(declaration: Node) -> bool:
    node = declaration.parent
    while node is not None:
        # A braced PHP namespace body is a `compound_statement` too, but it is
        # file scope, not a function's.
        namespace_body = (
            node.type == "compound_statement"
            and node.parent is not None
            and node.parent.type == "namespace_definition"
        )
        if node.type in _LOCAL_SCOPES and not namespace_body:
            return True
        node = node.parent
    return False


def _owner_name(node: Node) -> str | None:
    named = node.child_by_field_name("name")
    if named is None:
        for child in node.children:
            if child.type in _NAME_CHILDREN:
                named = child
                break
    if named is None:
        return None
    text = (named.text or b"").decode("utf-8", "replace")
    # `class A::B` (Ruby) and `Outer.Inner` read as their last segment.
    return re.split(r"::|\.", text)[-1] or None


def _enclosing_types(declaration: Node, language: str) -> list[Node]:
    kinds = _TYPE_NODES.get(language, frozenset())
    found: list[Node] = []
    node = declaration.parent
    while node is not None:
        if node.type in kinds:
            found.append(node)
        node = node.parent
    return found


def _owner(declaration: Node, language: str) -> str | None:
    for node in _enclosing_types(declaration, language):
        name = _owner_name(node)
        if name is not None:
            return name
    return None


def _is_interface(type_node: Node) -> bool:
    return type_node.type == "interface_declaration" or any(
        child.type == "interface" for child in type_node.children
    )


def _owner_is_interface(declaration: Node, language: str) -> bool:
    """Whether the nearest named enclosing type is an interface.

    A constant on an interface is reachable bare from every implementer and
    sub-interface, none of which has to name the interface, so only the
    constant's own name can be asked of a reader.
    """
    for node in _enclosing_types(declaration, language):
        if _owner_name(node) is not None:
            return _is_interface(node)
    return False


_EXPORT_OBJECTS = ("module.exports", "exports.", "exports[")


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _names_in(node: Node, name: str) -> bool:
    return any(
        n.type in ("identifier", "shorthand_property_identifier")
        and (n.text or b"").decode("utf-8", "replace") == name
        for n in _walk(node)
    )


def _is_exported(declaration: Node, name: str) -> bool:
    """Whether a TS/JS binding is exported, decided from the syntax tree.

    Either its own declaration says ``export``, or some other export statement
    (``export { K }``, ``export { K as X }``, ``export default K``) names it,
    or a CommonJS assignment to ``module.exports`` / ``exports.x`` /
    ``exports[...]`` mentions it on the right-hand side, on one line or many.
    """
    parent = declaration.parent
    if parent is not None and parent.type == "export_statement":
        return True
    if _is_local(declaration):
        return False
    root = declaration
    while root.parent is not None:
        root = root.parent
    for node in _walk(root):
        if node.type == "export_statement" and _names_in(node, name):
            return True
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None:
                continue
            text = (left.text or b"").decode("utf-8", "replace")
            if text.startswith(_EXPORT_OBJECTS) and _names_in(right, name):
                return True
    return False


def _in_partial_type(declaration: Node, language: str) -> bool:
    return any(
        "partial" in _tokens(type_node, ("modifier",))
        for type_node in _enclosing_types(declaration, language)
    )


def _has_part_directive(declaration: Node) -> bool:
    root = declaration
    while root.parent is not None:
        root = root.parent
    return any(c.type in ("part_directive", "part_of_directive") for c in root.children)


def _is_visible(const: KeyConst, language: str) -> bool:
    node = const.declaration
    name = const.name
    if language in _TS_LANGUAGES:
        return _is_exported(node, name)
    if language == "csharp":
        field = node.parent
        while field is not None and field.type != "field_declaration":
            field = field.parent
        if field is None:
            return False  # a local const
        if _owner_type_node_is(field, "interface_declaration"):
            return True
        if _in_partial_type(node, language):
            return True  # another part of the same class can read a private member
        return bool(_tokens(field, ("modifier",)) & {"public", "internal", "protected"})
    if language == "java":
        if node.type == "local_variable_declaration" or _is_local(node):
            return False
        return "private" not in _tokens(node, ("modifiers",))
    if language == "kt":
        if _is_local(node):
            return False
        return "private" not in _tokens(node, ("modifiers",))
    if language == "php":
        if _is_local(node):
            return False
        return "private" not in _tokens(node, ("visibility_modifier",))
    if language == "swift":
        if _is_local(node):
            return False
        return not (_tokens(node, ("modifiers",)) & {"private", "fileprivate"})
    if language == "dart":
        # A part file shares the library's private namespace.
        return not name.startswith("_") or _has_part_directive(node)
    if language == "go":
        return not _is_local(node)
    return True  # python, ruby: every constant is reachable


def _owner_type_node_is(node: Node, kind: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in _TYPE_NODES["csharp"]:
            return parent.type == kind
        parent = parent.parent
    return False


def visible_key_consts(
    consts: list[KeyConst], language: str
) -> tuple[VisibleKeyConst, ...]:
    """Each of ``consts`` another file could read, with what a reader must name."""
    found: set[VisibleKeyConst] = set()
    for const in consts:
        if not _is_visible(const, language):
            continue
        same_dir = language == "go" and not const.name[:1].isupper()
        owner = (
            None
            if language == "go"
            or _owner_is_interface(const.declaration, language)
            or (language == "dart" and const.name.startswith("_"))
            else _owner(const.declaration, language)
        )
        found.add(VisibleKeyConst(const.name, owner, same_dir))
    return tuple(sorted(found, key=lambda v: (v.name, v.owner or "", v.same_directory_only)))
