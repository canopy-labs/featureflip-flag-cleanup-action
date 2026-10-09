"""Java profile for cross-file key constants.

A declaration is a `static final String` field (or an interface constant)
whose initializer is exactly the key, in a top-level class or interface, and
visible outside its file (see :mod:`flag_cleanup.key_const_visibility`, the
one place that decides visibility). Its owner is that type; its module is the
file's package.

A mention in another file is proven when the file can only mean that
declaration:

* ``Owner.NAME`` -- ``Owner`` is visible because the reader is in the same
  package, or imports ``<package>.Owner``, or ``<package>.*``, and nothing in
  the reader (a type or variable of that name, a single-type or static import
  of another ``Owner``, or for the package and wildcard routes any other static
  wildcard import), in its own package (a type of that name, for the wildcard)
  or in a supertype (a member type or field of that name) can make ``Owner``
  mean something else;
* ``NAME`` -- the reader statically imports ``<package>.Owner.NAME`` or
  ``<package>.Owner.*``, binds ``NAME`` nowhere itself, imports no other
  ``NAME`` statically, and inherits no field ``NAME`` from a supertype.

A bare ``NAME`` the reader has not statically imported is not a mention at
all when nothing can bring the field into scope unqualified: no static import
(single or wildcard) from a class that is the owner or a repository type that
may extend it -- a library class such as ``java.util.Objects`` cannot -- and no
supertype path from a type in the reader to the owner. Supertypes are followed
through the repository's Java and Kotlin types, and only while every one of
those files could be read and parsed. It is another declaration of the same
name -- a sibling class's own ``FLAG_KEY`` -- and is skipped without a
refusal. So is ``Other.NAME`` when ``Other`` can only be a repository type
that is neither the owner nor extends it (no variable, field or static import
of that name, no Kotlin property or typealias of it anywhere). Otherwise the
mention is refused.

Supertypes are looked up among the repository's own Java and Kotlin files,
transitively. A supertype declared outside the repository (a library class)
is not inspected, so a member it contributes can still shadow an imported name.

A fully qualified ``app.RuntimeKeys.NAME``, or ``NAME`` reached through any
other qualifier, is refused as an unsupported form rather than resolved: the
reader rules know two shapes, and a qualifier this profile cannot prove might
mean another type's member of the same name.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const, key_const_visibility, syntax

_READ_CALLEES = frozenset({"boolVariation"})
_TYPE_DECLS = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _package(root: Node) -> str:
    for child in root.named_children:
        if child.type == "package_declaration":
            return _text(child.named_children[0])
    return ""


_walk = xk.walk


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    root = syntax._parse(source, "java")
    package = _package(root)
    found: list[xk.KeyDeclaration] = []
    for const in key_const.key_consts(root, source.encode("utf-8"), "java", flag_key):
        visible = key_const_visibility.visible_key_consts([const], "java")
        if not visible:
            continue  # private or local: no other file can name it
        decl = const.declaration
        if decl.type not in ("field_declaration", "constant_declaration"):
            continue
        owner_node = decl.parent.parent if decl.parent is not None else None
        if owner_node is None or owner_node.type not in _TYPE_DECLS:
            continue
        if owner_node.parent is None or owner_node.parent.type != "program":
            continue  # nested types are out of scope
        mods = next((c for c in decl.children if c.type == "modifiers"), None)
        words = _text(mods).split() if mods is not None else []
        if owner_node.type != "interface_declaration" and "static" not in words:
            continue  # an instance field is read through an instance
        # An interface constant is reported owner-less for the in-file gate,
        # because implementers read it bare; here the owner is what a
        # qualified read or a static import has to name.
        owner = visible[0].owner or key_const_visibility._owner(decl, "java")
        if owner is None:
            continue
        found.append(xk.KeyDeclaration(path, const.name, owner, package))
    entries, _refused = _enum_entries(path, root, source, flag_key)
    return found + entries


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every member of a top-level type here, whatever its value or modifiers.

    Fields, interface constants, enum constants and member types alike: any of
    them is what ``Owner.NAME`` or a static import of it can mean, so another
    file declaring one under the same qualified name competes with ours.
    """
    root = syntax._parse(source, "java")
    if root.has_error:
        raise xk.DeclarationsUnreadable(path)
    package = _package(root)
    found: set[xk.QualifiedName] = set()
    for node in root.named_children:
        if node.type not in _TYPE_DECLS:
            continue
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        fields, types = _members(body)
        found |= {(package, _text(name), member) for member in fields | types}
    return found


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """``(package, name)`` of every top-level class, interface, enum or record here."""
    root = syntax._parse(source, "java")
    if root.has_error:
        raise xk.DeclarationsUnreadable(path)
    package = _package(root)
    found: set[tuple[str, str]] = set()
    for node in root.named_children:
        if node.type in _TYPE_DECLS:
            name = node.child_by_field_name("name")
            if name is not None:
                found.add((package, _text(name)))
    return found


# --- enum entries -------------------------------------------------------------


def _words(declaration: Node) -> list[str]:
    mods = next((c for c in declaration.children if c.type == "modifiers"), None)
    return _text(mods).split() if mods is not None else []


def _plain_string(node: Node) -> str | None:
    """A string literal's value when it is one plain fragment: no escape, no text block."""
    if node.type != "string_literal":
        return None
    parts = node.named_children
    if len(parts) != 1 or parts[0].type != "string_fragment":
        return None
    return _text(parts[0])


def _enum_accessors(enum: Node) -> tuple[dict[int, frozenset[str]], bool]:
    """Per constructor-parameter position, the accessor spellings proven to return it.

    Proven means: the enum has exactly one constructor, and a statement directly
    in its body assigns the parameter to a ``final`` instance field
    (``this.f = p;`` or ``f = p;``). With one constructor and a ``final``
    field, javac's definite-assignment rules make that the field's only value.
    The field is an accessor when it is not private. A non-private,
    non-static, parameterless method whose whole body is ``return f;`` or
    ``return this.f;`` is an accessor too, under any name. The second value is
    False when nothing can be proven at all.
    """
    body = enum.child_by_field_name("body")
    extra = (
        next((c for c in body.named_children if c.type == "enum_body_declarations"), None)
        if body is not None
        else None
    )
    members = extra.named_children if extra is not None else []
    constructors = [m for m in members if m.type == "constructor_declaration"]
    if len(constructors) != 1:
        return {}, False
    parameters = constructors[0].child_by_field_name("parameters")
    if parameters is None or any(p.type != "formal_parameter" for p in parameters.named_children):
        return {}, False  # varargs or a receiver parameter
    names = [_text(p.child_by_field_name("name")) for p in parameters.named_children]
    finals: dict[str, bool] = {}  # field -> private
    for member in members:
        if member.type != "field_declaration":
            continue
        words = _words(member)
        if "final" not in words or "static" in words:
            continue
        for declarator in member.named_children:
            if declarator.type == "variable_declarator" and declarator.child_by_field_name("value") is None:
                finals[_text(declarator.child_by_field_name("name"))] = "private" in words
    assigned: dict[str, int] = {}
    constructor_body = constructors[0].child_by_field_name("body")
    for node in _walk(constructor_body) if constructor_body is not None else []:
        if node.type == "assignment_expression":
            target = node.child_by_field_name("left")
        elif node.type == "update_expression":
            target = next((c for c in node.named_children), None)
        else:
            continue
        if target is not None and target.type == "identifier" and _text(target) in names:
            return {}, False  # a parameter that is written is not the value passed in
    for statement in constructor_body.named_children if constructor_body is not None else []:
        if statement.type != "expression_statement" or not statement.named_children:
            continue
        expression = statement.named_children[0]
        if expression.type != "assignment_expression":
            continue
        operator = expression.child_by_field_name("operator")
        left = expression.child_by_field_name("left")
        right = expression.child_by_field_name("right")
        if operator is None or _text(operator) != "=" or left is None or right is None:
            continue
        if left.type == "field_access" and left.child_by_field_name("object").type == "this":
            field = _text(left.child_by_field_name("field"))
        elif left.type == "identifier" and _text(left) not in names:
            field = _text(left)
        else:
            continue
        if right.type == "identifier" and _text(right) in names and field in finals:
            assigned[field] = names.index(_text(right))
    positions: dict[int, set[str]] = {}
    for field, position in assigned.items():
        spellings = positions.setdefault(position, set())
        if not finals[field]:
            spellings.add(field)
    for member in members:
        if member.type != "method_declaration":
            continue
        words = _words(member)
        if {"private", "static", "abstract"} & set(words):
            continue
        params = member.child_by_field_name("parameters")
        method_body = member.child_by_field_name("body")
        if params is None or params.named_children or method_body is None:
            continue
        statements = [s for s in method_body.named_children if "comment" not in s.type]
        if len(statements) != 1 or statements[0].type != "return_statement":
            continue
        value = statements[0].named_children[0] if statements[0].named_children else None
        if value is not None and value.type == "identifier":
            field = _text(value)
        elif (
            value is not None
            and value.type == "field_access"
            and value.child_by_field_name("object").type == "this"
        ):
            field = _text(value.child_by_field_name("field"))
        else:
            continue
        if field in assigned:
            positions.setdefault(assigned[field], set()).add(
                _text(member.child_by_field_name("name")) + "()"
            )
    proven = {p: frozenset(s) for p, s in positions.items() if s}
    return proven, bool(proven)


def _enum_entries(
    path: str, root: Node, source: str, flag_key: str
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """The provable entries holding ``flag_key``, and a refusal for every other one."""
    package = _package(root)
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for enum in _walk(root):
        if enum.type != "enum_declaration":
            continue
        body = enum.child_by_field_name("body")
        constants = [c for c in body.named_children if c.type == "enum_constant"] if body else []
        held = xk.key_token(flag_key)
        holding = [
            c for c in constants
            if (args := c.child_by_field_name("arguments")) is not None and held.search(_text(args))
        ]
        if not holding:
            continue
        owner = _text(enum.child_by_field_name("name"))
        top_level = enum.parent is not None and enum.parent.type == "program"
        positions, proven = _enum_accessors(enum) if top_level else ({}, False)
        for constant in holding:
            entry = f"{owner}.{_text(constant.child_by_field_name('name'))}"
            if not top_level:
                refused.append(xk.refusal(path, constant, xk.UNSUPPORTED_FORM, source, entry))
                continue
            args = constant.child_by_field_name("arguments")
            spellings: set[str] = set()
            for position, arg in enumerate(args.named_children):
                if _plain_string(arg) == flag_key:
                    spellings |= positions.get(position, frozenset())
            if (
                root.has_error
                or not proven
                or constant.child_by_field_name("body") is not None
                or not spellings
            ):
                refused.append(xk.refusal(path, constant, xk.ENUM_UNPROVEN, source, entry))
                continue
            found.append(
                xk.KeyDeclaration(
                    path,
                    _text(constant.child_by_field_name("name")),
                    owner,
                    package,
                    kind=xk.ENUM_ENTRY,
                    accessors=frozenset(spellings),
                )
            )
    return found, refused


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """The entries that cannot be proven, and reads of proven ones in their own file.

    A read in the declaring file is outside the reader invocation, which never
    rewrites the file that declares the entry, so it is reported rather than
    left silently.
    """
    root = syntax._parse(source, "java")
    found, refused = _enum_entries(path, root, source, flag_key)
    for decl in found:
        for node in _walk(root):
            if node.type != "identifier" or _text(node) != decl.name:
                continue
            parent = node.parent
            if parent is not None and parent.type == "enum_constant":
                continue  # the entry itself
            refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry))
            break
    return refused


def _imports(root: Node) -> tuple[set[str], set[str]]:
    """(plain imports, static imports) as dotted text, `*` kept as the last segment."""
    plain, static = set(), set()
    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        is_static = any(c.type == "static" for c in node.children)
        target = next(
            (c for c in node.named_children if c.type in ("scoped_identifier", "identifier")),
            None,
        )
        if target is None:
            continue
        dotted = _text(target) + (".*" if any(c.type == "asterisk" for c in node.children) else "")
        (static if is_static else plain).add(dotted)
    return plain, static


def _binds(root: Node, name: str) -> bool:
    """Whether the file binds ``name`` itself: a field, local, parameter, ..."""
    return any(
        node.type == "identifier" and _text(node) == name and key_const._java_is_binding(node)
        for node in _walk(root)
    )


#: Where an ``identifier`` names something that is surely NOT a variable: a
#: type, method or constructor being declared, a method being called, or the
#: qualifier of a member access or call (which is what is being asked about).
_NOT_A_VALUE = {
    "class_declaration": ("name",),
    "interface_declaration": ("name",),
    "enum_declaration": ("name",),
    "record_declaration": ("name",),
    "annotation_type_declaration": ("name",),
    "annotation_type_element_declaration": ("name",),
    "method_declaration": ("name",),
    "constructor_declaration": ("name",),
    "method_invocation": ("name", "object"),
    "field_access": ("object",),
}


def _may_be_variable(root: Node, name: str) -> bool:
    """Whether ``name`` can mean a variable anywhere in the file.

    Decided by elimination, not by listing binders: every ``identifier``
    spelled ``name`` must sit where only a type, a method or a qualifier can
    (see ``_NOT_A_VALUE``), or in the package or import header. Any other
    occurrence -- a local, a field, a parameter, a pattern or resource
    variable, an enum constant, a lambda parameter, a use as a value, a
    binder form this list has never heard of -- makes it a possible variable,
    and in ``name.MEMBER`` a variable obscures a type of the same name.
    """
    for node in _walk(root):
        if node.type != "identifier" or _text(node) != name or _in_import(node):
            continue
        parent = node.parent
        fields = _NOT_A_VALUE.get(parent.type, ()) if parent is not None else ()
        if not any(
            (child := parent.child_by_field_name(field)) is not None and child.id == node.id
            for field in fields
        ):
            return True
    return False


def _shadowing_type_names(root: Node) -> set[str]:
    """Type names declared in the file that can shadow a top-level type.

    Every type parameter (of a class, interface, record, method or
    constructor) and every type that is not at the top of the file: a
    nested, member, local or anonymous-body type. Collected file-wide rather
    than by scope, which only ever keeps a mention.
    """
    names: set[str] = set()
    for node in _walk(root):
        if node.type == "type_parameter":
            first = next(
                (c for c in node.named_children if c.type in ("type_identifier", "identifier")),
                None,
            )
            if first is not None:
                names.add(_text(first))
        elif node.type in _TYPE_DECLS and (node.parent is None or node.parent.type != "program"):
            named = node.child_by_field_name("name")
            if named is not None:
                names.add(_text(named))
    return names


def _declares_type(root: Node, name: str) -> bool:
    for node in _walk(root):
        if node.type in _TYPE_DECLS:
            named = node.child_by_field_name("name")
            if named is not None and _text(named) == name:
                return True
    return False


@dataclass(frozen=True)
class _TypeInfo:
    """One type declared in the repository, as an inheriting reader sees it."""

    package: str
    fields: frozenset[str]
    member_types: frozenset[str]
    supertypes: tuple[str, ...]
    #: Declared at the top of its file, so ``package.Name`` names it.
    top_level: bool = False


@dataclass(frozen=True)
class _TypeIndex:
    by_name: dict[str, tuple[_TypeInfo, ...]]
    #: A Java file that could not be read, or whose parse has errors: it might
    #: declare anything, and a declaration inside it may be invisible here.
    incomplete: bool


def _simple_type_names(node: Node) -> list[str]:
    """The simple names of the types a supertype clause names, generics dropped."""
    if node.type == "type_identifier":
        return [_text(node)]
    if node.type == "scoped_type_identifier":
        last = [c for c in node.named_children if c.type == "type_identifier"]
        return [_text(last[-1])] if last else []
    if node.type == "generic_type":
        return _simple_type_names(node.named_children[0]) if node.named_children else []
    names: list[str] = []
    if node.type in ("type_list", "superclass", "super_interfaces", "extends_interfaces"):
        for child in node.named_children:
            names.extend(_simple_type_names(child))
    return names


def _supertypes(type_node: Node) -> list[str]:
    names: list[str] = []
    for child in type_node.named_children:
        if child.type in ("superclass", "super_interfaces", "extends_interfaces"):
            names.extend(_simple_type_names(child))
    return names


def _members(body: Node) -> tuple[set[str], set[str]]:
    fields: set[str] = set()
    types: set[str] = set()
    for child in body.named_children:
        if child.type == "enum_body_declarations":
            more_fields, more_types = _members(child)
            fields |= more_fields
            types |= more_types
        elif child.type in ("field_declaration", "constant_declaration"):
            for declarator in child.named_children:
                if declarator.type == "variable_declarator":
                    name = declarator.child_by_field_name("name")
                    if name is not None:
                        fields.add(_text(name))
        elif child.type == "enum_constant":
            name = child.child_by_field_name("name")
            if name is not None:
                fields.add(_text(name))
        elif child.type in _TYPE_DECLS:
            name = child.child_by_field_name("name")
            if name is not None:
                types.add(_text(name))
    return fields, types


def _types_of(text: str) -> tuple[list[tuple[str, _TypeInfo]], bool]:
    """One Java file's share of the type index, and whether it parsed cleanly."""
    tree = syntax._parse(text, "java")
    package = _package(tree)
    types: list[tuple[str, _TypeInfo]] = []
    for node in _walk(tree):
        if node.type not in _TYPE_DECLS:
            continue
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None:
            continue
        fields, member_types = _members(body) if body is not None else (set(), set())
        types.append(
            (
                _text(name),
                _TypeInfo(
                    package,
                    frozenset(fields),
                    frozenset(member_types),
                    tuple(_supertypes(node)),
                    node.parent is not None and node.parent.type == "program",
                ),
            )
        )
    return types, not tree.has_error


def _combine(
    shares: list[tuple[str, list[tuple[str, _TypeInfo]]]], incomplete: bool, _unreadable: bool
) -> _TypeIndex:
    by_name: dict[str, list[_TypeInfo]] = {}
    for _path, types in shares:
        for name, info in types:
            by_name.setdefault(name, []).append(info)
    return _TypeIndex({k: tuple(v) for k, v in by_name.items()}, incomplete)


#: Every Java type in a checkout, by simple name.
_type_index = xk.CheckoutIndex("Java", (".java",), _types_of, _combine)


def _package_declares_type(repo_dir: str, package: str, name: str) -> bool:
    """Whether a file of ``package`` declares a type called ``name``.

    A type of the reader's own package shadows every on-demand import, so
    ``import app.*`` proves nothing about ``Owner`` while one exists.
    Over-approximated (a nested type counts too, and so does an unreadable
    file): a wrong "yes" only keeps a declaration.
    """
    index = _type_index(repo_dir)
    return index.incomplete or any(
        info.package == package for info in index.by_name.get(name, ())
    )


def _inherited(
    root: Node, repo_dir: str
) -> tuple[frozenset[str], frozenset[str], bool]:
    """(fields, member types, unknown) the reader's types inherit in the repository.

    Every type in the reader file counts, anonymous class bodies included, and
    the chain is followed through every repository type of each simple name,
    Java or Kotlin: a reader cannot be told apart from the type it extends by
    name alone, and over-collecting only keeps a declaration.

    A Kotlin type contributes every name its body declares, as a field and as
    a member type alike: a companion's ``const val`` or ``@JvmField`` is a
    static field of the outer class to Java, a class-body ``@JvmField`` is an
    instance field, and a nested class is a member type. A Kotlin supertype
    spelled through a typealias is followed to what it stands for.

    ``unknown`` is True when the reader has a supertype and the Java index is
    incomplete, or when a Kotlin file that could not be parsed whole may
    declare a type anywhere up the chain: that type's members and supertypes
    are then unknown, so any name may be inherited. It fails closed.
    """
    from flag_cleanup import cross_file_kotlin

    pending: list[str] = []
    for node in _walk(root):
        if node.type in _TYPE_DECLS:
            pending.extend(_supertypes(node))
        elif node.type == "object_creation_expression" and any(
            c.type == "class_body" for c in node.named_children
        ):
            created = node.child_by_field_name("type")
            if created is not None:
                pending.extend(_simple_type_names(created))
    if not pending:
        return frozenset(), frozenset(), False
    index = _type_index(repo_dir)
    kotlin = cross_file_kotlin._kotlin_index(repo_dir)
    fields: set[str] = set()
    types: set[str] = set()
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for info in index.by_name.get(name, ()):
            fields |= info.fields
            types |= info.member_types
            pending.extend(info.supertypes)
        pending.extend(kotlin.aliases.get(name, ()))
        for info in kotlin.by_name.get(name, ()):
            fields |= info.names
            types |= info.names
            pending.extend(info.supertypes)
    return (
        frozenset(fields),
        frozenset(types),
        index.incomplete or any(kotlin.may_declare_type(n) for n in seen),
    )


def _supertype_names(root: Node) -> list[str]:
    """The simple names every type in the file names as a supertype."""
    pending: list[str] = []
    for node in _walk(root):
        if node.type in _TYPE_DECLS:
            pending.extend(_supertypes(node))
        elif node.type == "object_creation_expression" and any(
            c.type == "class_body" for c in node.named_children
        ):
            created = node.child_by_field_name("type")
            if created is not None:
                pending.extend(_simple_type_names(created))
    return pending


class _JvmTypes:
    """Every Java and Kotlin type in the checkout, as supertype edges by simple name.

    Built lazily, once per reader, from the two cached indexes: a reader that
    never asks pays nothing. A Java owner can be extended from either
    language, so a chain is followed through both.
    """

    def __init__(self, repo_dir: str):
        self._repo_dir = repo_dir
        self._facts: tuple[dict[str, list[tuple[str, ...]]], frozenset[str], bool] | None = None

    def _suspect(self, name: str) -> bool:
        """Whether a file neither index saw whole may declare type ``name``.

        Any Java file, or a Kotlin file that names a type ``name``. Each
        question below walks UP from names it already holds, so a Kotlin file
        that cannot declare one of them cannot change its answer.
        """
        from flag_cleanup import cross_file_kotlin

        return _type_index(self._repo_dir).incomplete or cross_file_kotlin._kotlin_index(
            self._repo_dir
        ).may_declare_type(name)

    def _get(self) -> tuple[dict[str, list[tuple[str, ...]]], frozenset[str], bool]:
        """(supertype edges, Kotlin values, whether either index is incomplete).

        The flag is the whole-checkout answer, for a caller that walks DOWN to
        every type extending a name; the methods here ask :meth:`_suspect`.
        """
        if self._facts is None:
            from flag_cleanup import cross_file_kotlin

            java = _type_index(self._repo_dir)
            kotlin = cross_file_kotlin._kotlin_index(self._repo_dir)
            supers: dict[str, list[tuple[str, ...]]] = {}
            for by_name in (java.by_name, kotlin.by_name):
                for name, infos in by_name.items():
                    supers.setdefault(name, []).extend(info.supertypes for info in infos)
            # A Kotlin typealias is an edge to what it stands for: a type
            # extending `Base` with `typealias Base = Flags` extends `Flags`.
            for alias, targets in kotlin.aliases.items():
                supers.setdefault(alias, []).append(targets)
            self._facts = (supers, kotlin.values, java.incomplete or kotlin.incomplete)
        return self._facts

    def reaches(self, names, owner: str) -> bool:
        """Whether a type of one of these simple names may be ``owner`` or extend it.

        Fails closed: True once the walk meets a name a file not seen whole
        may declare.
        """
        names = list(names)
        if not names:
            return False
        supers, _values, _incomplete = self._get()
        seen: set[str] = set()
        while names:
            name = names.pop()
            if name == owner:
                return True
            if name in seen:
                continue
            if self._suspect(name):
                return True
            seen.add(name)
            for supertypes in supers.get(name, ()):
                names.extend(supertypes)
        return False

    def may_hold(self, simple: str, owner: str | None) -> bool:
        """Whether a class a static import names can supply ``owner``'s member.

        It can when it is the owner, or a repository type that may extend it.
        A class the repository does not declare is a library's, and a library
        cannot extend a repository type. An owner-less constant (a Kotlin
        top-level one, read from Java through a facade this cannot name) is
        reachable through any class.
        """
        if owner is None or simple == owner:
            return True
        supers, _values, _incomplete = self._get()
        if self._suspect(simple):
            return True
        return simple in supers and self.reaches([simple], owner)

    def top_level_type(self, name: str, package: str) -> bool:
        """Whether a Java or Kotlin file declares top-level type ``package.name``."""
        from flag_cleanup import cross_file_kotlin

        java = _type_index(self._repo_dir)
        kotlin = cross_file_kotlin._kotlin_index(self._repo_dir)
        return any(
            info.top_level and info.package == package
            for info in (*java.by_name.get(name, ()), *kotlin.by_name.get(name, ()))
        )

    def unrelated_type(self, name: str, owner: str) -> bool:
        """Whether ``name`` is surely a repository type that is not ``owner`` nor extends it.

        False when no repository type has that name (a library type may be
        anything), when a Kotlin top-level property or typealias of that name
        exists anywhere, which could stand for the owner, or when a file
        neither index saw whole may declare either, or a type up its chain.
        """
        from flag_cleanup import cross_file_kotlin

        supers, values, _incomplete = self._get()
        if name not in supers or name in values:
            return False
        if cross_file_kotlin._kotlin_index(self._repo_dir).may_declare_name(name):
            return False
        return not self.reaches([name], owner)


def _read_call(arg: Node, accessors: frozenset[str]) -> bool:
    args = arg.parent
    if args is None or args.type != "argument_list" or not args.named_children:
        return False
    if args.named_children[0].id != arg.id:
        return False
    call = args.parent
    if call is None or call.type != "method_invocation":
        return False
    name = call.child_by_field_name("name")
    return name is not None and _text(name) in (_READ_CALLEES | accessors)


def _entry_read(
    head: Node, decl: xk.KeyDeclaration, accessors: frozenset[str]
) -> tuple[str | None, str]:
    """How ``head`` (``Owner.NAME`` or ``NAME``) is used through an accessor.

    ``(verdict, accessor)``. The verdict is None for a proven read: a proven
    accessor of ``decl`` applied to ``head``, as the first argument of a read
    call. Otherwise it is the refusal reason. A call whose argument list is
    anything but ``()`` exactly is unsupported, because the reader rule matches
    that text.
    """
    outer = head.parent
    if outer is not None and outer.type == "method_invocation":
        obj = outer.child_by_field_name("object")
        name = outer.child_by_field_name("name")
        args = outer.child_by_field_name("arguments")
        if obj is None or obj.id != head.id or name is None or args is None:
            return xk.NOT_A_READ, ""
        accessor = _text(name) + "()"
        if accessor not in decl.accessors:
            return xk.NOT_A_READ, ""
        if _text(args) != "()":
            return xk.UNSUPPORTED_FORM, accessor
    elif outer is not None and outer.type == "field_access":
        obj = outer.child_by_field_name("object")
        field = outer.child_by_field_name("field")
        if obj is None or obj.id != head.id or field is None:
            return xk.NOT_A_READ, ""
        accessor = _text(field)
        if accessor not in decl.accessors:
            return xk.NOT_A_READ, ""
    else:
        return xk.NOT_A_READ, ""
    return (None if _read_call(outer, accessors) else xk.NOT_A_READ), accessor


def _in_import(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in ("import_declaration", "package_declaration"):
            return True
        parent = parent.parent
    return False


_refusal = xk.refusal


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    root = syntax._parse(source, "java")
    package = _package(root)
    plain, static = _imports(root)
    static_single_imports = {imp for imp in static if not imp.endswith(".*")}
    static_wildcards = {imp[:-2] for imp in static if imp.endswith(".*")}
    inherited_fields, inherited_types, inherits_unknown = _inherited(root, repo_dir)
    jvm = _JvmTypes(repo_dir)
    supertype_names = _supertype_names(root)
    shadowing_types = _shadowing_type_names(root)

    def unrelated_qualifier(name: str, decl: xk.KeyDeclaration) -> bool:
        """Whether ``name.NAME`` surely reads another type's member, not ``decl``.

        ``name`` must mean a type here and nothing else -- nothing in the file
        may be a variable of that name (decided by elimination, see
        :func:`_may_be_variable`), nor a field of a repository supertype or of
        a static import -- and that type must be a repository type that is
        neither the owner nor extends it, with every JVM file indexed. A
        sibling class's ``SearchGate.FLAG_KEY`` is the case this exists for.
        """
        if decl.owner is None or inherits_unknown:
            return False
        if _may_be_variable(root, name) or name in inherited_fields:
            return False
        if name in inherited_types or name in shadowing_types:
            return False  # a type variable or a nested type of that name wins
        if any(imp.rsplit(".", 1)[-1] == name for imp in static_single_imports):
            return False
        index = _type_index(repo_dir)
        for wildcard in static_wildcards:
            for info in index.by_name.get(wildcard.rsplit(".", 1)[-1], ()):
                if name in info.fields:
                    return False
        if not resolves_to_repository_type(name):
            return False
        return jvm.unrelated_type(name, decl.owner)

    def resolves_to_repository_type(name: str) -> bool:
        """Whether the simple type name ``name`` here is a top-level repository type.

        Declared at the top of this file, or imported by a single-type import
        of a top-level repository type of that package, or -- with no such
        import -- a top-level repository type of this file's own package.
        Reaching it only through an on-demand import or ``java.lang`` is not
        enough: that could be a library type of the same simple name.
        """
        if any(
            node.type in _TYPE_DECLS
            and node.child_by_field_name("name") is not None
            and _text(node.child_by_field_name("name")) == name
            for node in root.named_children
        ):
            return True
        singles = [
            imp for imp in plain if not imp.endswith(".*") and imp.rsplit(".", 1)[-1] == name
        ]
        if len(singles) > 1:
            return False
        home = singles[0].rsplit(".", 1)[0] if singles else package
        return any(
            info.top_level and info.package == home
            for info in _type_index(repo_dir).by_name.get(name, ())
        )
    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []
    for decl in declarations:
        own_owner = f"{decl.module}.{decl.owner}"
        is_entry = decl.kind == xk.ENUM_ENTRY
        # Another `Owner` the reader could mean instead: one it declares,
        # binds as a variable or inherits, or one it imports by name from
        # elsewhere -- as a type, or statically as a member type.
        foreign_import = any(
            imp.rsplit(".", 1)[-1] == decl.owner and imp != own_owner for imp in plain
        ) or any(
            imp.rsplit(".", 1)[-1] == decl.owner and imp.rsplit(".", 1)[0] != decl.module
            for imp in static_single_imports
        )
        owner_shadowed = (
            foreign_import
            or _declares_type(root, decl.owner)
            or _binds(root, decl.owner)
            or decl.owner in inherited_types
            or decl.owner in inherited_fields
            or inherits_unknown
        )
        explicit = own_owner in plain
        # The package and wildcard routes are the weakest: any other static
        # wildcard import might bring in a member type of the same name.
        other_static_wildcard = any(w != own_owner for w in static_wildcards)
        owner_visible = not owner_shadowed and (
            explicit
            or (
                not other_static_wildcard
                and (
                    package == decl.module
                    or (
                        f"{decl.module}.*" in plain
                        and not _package_declares_type(repo_dir, package, decl.owner)
                    )
                )
            )
        )
        static_single = f"{own_owner}.{decl.name}" in static
        bare_visible = static_single or f"{own_owner}.*" in static
        # A single static import of another `NAME` beats ours on demand, and a
        # field `NAME` declared or inherited by the reader beats any import.
        foreign_static_name = any(
            imp.rsplit(".", 1)[-1] == decl.name and imp.rsplit(".", 1)[0] != own_owner
            for imp in static_single_imports
        )
        rebound = bare_visible and (
            _binds(root, decl.name)
            or foreign_static_name
            or decl.name in inherited_fields
            or inherits_unknown
        )
        # Whether a bare `NAME` the reader has not imported can still be
        # ours. Java has no top-level fields, so only a static import or
        # inheritance can bring the field into scope unqualified: any static
        # import of that name or any static wildcard (whose class might itself
        # inherit it), or a supertype path to the owner. A bare mention none
        # of those reaches is another declaration's business entirely -- a
        # sibling class's own `FLAG_KEY` -- and is neither a read nor a reason
        # to keep ours.
        bare_reachable = (
            any(jvm.may_hold(w.rsplit(".", 1)[-1], decl.owner) for w in static_wildcards)
            or any(
                imp.rsplit(".", 1)[-1] == decl.name
                and jvm.may_hold(imp.rsplit(".", 2)[-2] if imp.count(".") else imp, decl.owner)
                for imp in static_single_imports
            )
            or (
                decl.owner is not None
                and bool(supertype_names)
                and jvm.reaches(supertype_names, decl.owner)
            )
        )
        first_import: Node | None = None
        proven_bare = False
        refused_before = len(refused)
        for node in _walk(root):
            if node.type != "identifier" or _text(node) != decl.name:
                continue
            if _in_import(node):
                first_import = first_import or node
                continue
            parent = node.parent
            if key_const._java_is_binding(node):
                continue  # the binding itself; `rebound` is reported at the reads
            if is_entry:
                field = (
                    parent.child_by_field_name("field")
                    if parent is not None and parent.type == "field_access"
                    else None
                )
                qualified = field is not None and field.id == node.id
                head = parent if qualified else node
                verdict, accessor = _entry_read(head, decl, accessors)
                entry = decl.entry
                if qualified:
                    obj = parent.child_by_field_name("object")
                    if (
                        obj is not None
                        and obj.type == "identifier"
                        and _text(obj) != decl.owner
                        and unrelated_qualifier(_text(obj), decl)
                    ):
                        continue  # another type's own member of the same name
                    if obj is None or obj.type != "identifier" or _text(obj) != decl.owner:
                        refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source, entry))
                    elif not owner_visible:
                        refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source, entry))
                    elif verdict is not None:
                        refused.append(_refusal(path, node, verdict, source, entry))
                    else:
                        reads.append(xk.KeyReference(path, decl.owner, decl.name, accessor, decl))
                    continue
                if parent is not None and parent.type == "scoped_identifier":
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source, entry))
                    continue
                if parent is not None and parent.type == "switch_label":
                    # A case label names the constant with no import and whatever
                    # the subject's type, so visibility proves nothing here.
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source, entry))
                    continue
                if not bare_visible:
                    if bare_reachable:
                        refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source, entry))
                    continue
                if rebound:
                    refused.append(_refusal(path, node, xk.REBOUND, source, entry))
                elif verdict is not None:
                    refused.append(_refusal(path, node, verdict, source, entry))
                else:
                    reads.append(xk.KeyReference(path, None, decl.name, accessor, decl))
                    proven_bare = True
                continue
            if parent is not None and parent.type == "field_access":
                field = parent.child_by_field_name("field")
                if field is None or field.id != node.id:
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source))
                    continue
                obj = parent.child_by_field_name("object")
                if (
                    obj is not None
                    and obj.type == "identifier"
                    and _text(obj) != decl.owner
                    and unrelated_qualifier(_text(obj), decl)
                ):
                    continue  # another type's own member of the same name
                if obj is None or obj.type != "identifier" or _text(obj) != decl.owner:
                    # A qualifier this profile cannot prove: a package path, a
                    # subclass, an instance. It may well be ours.
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                elif not owner_visible:
                    refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source))
                elif not _read_call(parent, accessors):
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source))
                else:
                    reads.append(xk.KeyReference(path, decl.owner, decl.name))
                continue
            if parent is not None and parent.type in ("scoped_identifier", "method_invocation"):
                refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                continue
            if not bare_visible:
                if bare_reachable:
                    refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source))
                continue
            elif rebound:
                refused.append(_refusal(path, node, xk.REBOUND, source))
            elif not _read_call(node, accessors):
                refused.append(_refusal(path, node, xk.NOT_A_READ, source))
            else:
                reads.append(xk.KeyReference(path, None, decl.name))
                proven_bare = True
        # `import static <pkg>.Owner.NAME;` is removed with the last read it
        # served. With no read to remove, nothing removes it, and it would
        # name a deleted field.
        if (
            not is_entry
            and static_single
            and first_import is not None
            and not proven_bare
            and len(refused) == refused_before
        ):
            refused.append(_refusal(path, first_import, xk.NOT_A_READ, source))
    if refused:
        return [], refused
    return reads, refused


#: Calls on an enum type that reach its entries by position or by name.
_DYNAMIC_CALLS = frozenset({"values", "valueOf"})


def _simple_type_name(node: Node) -> str | None:
    """The simple name ``Flags``, ``app.Flags`` or ``app.Flags`` (as a type) spells."""
    if node.type in ("identifier", "type_identifier"):
        return _text(node)
    if node.type == "field_access":
        field = node.child_by_field_name("field")
        return _text(field) if field is not None else None
    if node.type == "scoped_type_identifier" and node.named_children:
        last = node.named_children[-1]
        return _text(last) if last.type == "type_identifier" else None
    return None


def _enclosing_enum(node: Node) -> str | None:
    """The name of the enum whose body ``node`` sits in, if any."""
    current = node.parent
    while current is not None:
        if current.type == "enum_declaration":
            name = current.child_by_field_name("name")
            return _text(name) if name is not None else None
        current = current.parent
    return None


def enum_dynamic_uses(
    path: str, source: str, entries: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """``Flags.values()``, ``Flags.valueOf(...)``, ``Flags::valueOf`` and
    ``Flags.class`` in ``path``, qualified (``app.Flags.values()``) or not,
    and a bare ``values()`` or ``valueOf(...)`` inside the enum itself.

    Matched on the owner's simple name, without proving which type it means:
    a note only asks a reviewer to look, so over-reporting costs a line and
    under-reporting costs a missed read.
    """
    root = syntax._parse(source, "java")
    by_owner: dict[str, list[xk.KeyDeclaration]] = {}
    for decl in entries:
        if decl.owner is not None:
            by_owner.setdefault(decl.owner, []).append(decl)
    notes: list[xk.Refusal] = []
    for node in _walk(root):
        owner: str | None = None
        if node.type == "method_invocation":
            obj = node.child_by_field_name("object")
            name = node.child_by_field_name("name")
            if name is not None and _text(name) in _DYNAMIC_CALLS:
                owner = _simple_type_name(obj) if obj is not None else _enclosing_enum(node)
        elif node.type == "method_reference" and len(node.named_children) == 2:
            target, member = node.named_children
            if member.type == "identifier" and _text(member) in _DYNAMIC_CALLS:
                owner = _simple_type_name(target)
        elif node.type == "class_literal" and node.named_children:
            owner = _simple_type_name(node.named_children[0])
        for decl in by_owner.get(owner or "", ()):
            notes.append(xk.refusal(path, node, xk.ENUM_DYNAMIC, source, decl.entry))
    return sorted(set(notes), key=lambda n: (n.line, n.entry))


xk.register(
    "java",
    xk.Profile(
        language_family=frozenset({"java"}),
        declarations=declarations,
        references=references,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        reaches=frozenset({"kt", "groovy", "scala"}),
        entry_findings=entry_findings,
        enum_dynamic_uses=enum_dynamic_uses,
    ),
)
