"""Kotlin profile for cross-file key constants.

A declaration is a ``const val NAME = "<key>"`` (the pre-pass's own reading of
a key constant) that another file can name, as decided by
:mod:`flag_cleanup.key_const_visibility`, and that sits at the top level of
its file (no owner), directly in a top-level ``object`` (that object is the
owner), or directly in the ``companion object`` of a top-level class (the
class is the owner, and the companion's name -- ``Companion`` unless it has
one -- is kept with it). An interface's companion is not offered: every
implementer reads its members bare. Its module is the file's package. A file
whose parse has errors declares nothing here: what the constant sits in cannot
be trusted.

A companion member is read as ``Owner.NAME`` or ``Owner.Companion.NAME``
(the companion's own name, never ``Companion`` for a named one), and imported
as ``import <package>.Owner.Companion.NAME``: ``import <package>.Owner.NAME``
does not compile. Unlike an object, the class can be extended, and a subclass
reads the member bare without naming it; the supertype checks below catch
that, following a supertype spelled through a renaming import or a typealias
to what it stands for.

Reads are not folded by generated rules. The sentinel pre-pass rewrites the
callee of each read this profile proves, and ``kt.toml`` folds it exactly as
it folds a literal key; :class:`flag_cleanup.kotlin_sentinel.SentinelPrePass`
learns the argument texts that mean this flag's key from the references
proven here, per file.

A mention in another file is proven only when that file can mean nothing else:

* ``Owner.NAME`` (or ``Owner.Companion.NAME``) -- ``Owner`` is bound once, by ``import <package>.Owner`` or
  by the reader being in the declaring package with no import of that simple
  name, and nothing else in the reader binds ``Owner`` (a class, object,
  typealias, property, parameter, ...). No star import of another package may
  be present, and no supertype the repository declares for a type in the
  reader may have a member or nested type of that name;
* ``NAME`` or an alias of it -- bound exactly once, by
  ``import <package>.Owner.NAME``, ``import <package>.Owner.Companion.NAME`` or
  ``import <package>.NAME`` (optionally
  ``as Alias``) or, for a top-level constant, by the reader being in the
  declaring package with no import of the name. Nothing else in the file may
  spell it: a second import, a local, a parameter or destructured name, a
  member or type of that name, a star import of another package, a member of
  an in-repository supertype. A bare read inside a lambda, an anonymous
  function or an extension is refused too: an implicit receiver resolves an
  unqualified name before any import does, and nothing here can tell what the
  receiver declares.

A bare ``NAME`` that is not bound to the declaration that way is not a
mention at all when nothing can bring the declaration into scope. For a
top-level constant: not its package, and no star import of it. For a member:
no star or member import from the owner or from a repository class that may
extend a Java owner (a library class cannot), no repository supertype
declaring the name, and no implicit receiver around it. It is another
declaration of the same name -- a sibling object's own ``FLAG_KEY`` -- and is
skipped without a refusal. So is ``Other.NAME`` when ``Other`` can only be a
repository type that is neither the owner nor extends it. Otherwise the
mention is refused.

Inside a type body, a top-level extension property of the same name (any
package, any receiver: ``val Any.K``) resolves on ``this`` before an import or
a same-package declaration does, so a bare read whose name one spells is
refused (``rebound``), and so is a qualified read whose OWNER one spells
(``import-unresolved``). A qualified read inside a lambda, an anonymous
function or an extension is refused too when any repository type, Kotlin or
Java, declares a member spelled like the owner: the receiver's type is not
known here, and its member would win over the import.

Supertypes are looked up among the repository's own Kotlin and Java files,
transitively; one declared outside the repository (a library class) is not
inspected, as for Java. An index that could not read or parse every file fails
closed for a reader that has any supertype, and for any read that runs with an
implicit receiver.

Only the first argument of a non-optional ``boolVariation(...)`` or configured
accessor call is a read -- the calls the pre-pass marks. Every other mention
of the name refuses, and one refusal drops the whole file's reads: an
assignment, a string template or any string spelling the name, a callable
reference, a named argument, a qualifier this profile cannot prove (a package
path, ``?.``, another object), an import left with no read to remove (it would
name a deleted declaration), a read in a ``.kts`` script (its host supplies
an implicit receiver), and a file whose parse has errors.

Residual, as for Java: a type declared outside the repository (a library
supertype, or a library receiver of a lambda) is not inspected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const, key_const_visibility, kotlin_sentinel

_READ_CALLEES = frozenset({kotlin_sentinel.SDK_FUNCTION_NAME})

#: Every node that declares a type with a body and supertypes.
_TYPE_DECLS = frozenset(
    {"class_declaration", "object_declaration", "companion_object", "object_literal"}
)

#: Parents whose DIRECT ``identifier`` child is the name they bind.
_BINDERS = frozenset(
    {
        "variable_declaration",
        "parameter",
        "class_parameter",
        "function_declaration",
        "class_declaration",
        "object_declaration",
        "companion_object",
        "type_alias",
        "enum_entry",
        "catch_block",
        "type_parameter",
        "setter",
        "parameter_with_optional_type",
    }
)

#: Children that may come before a declaration's name without making it an
#: extension: everything else there is a receiver type.
_NOT_A_RECEIVER = frozenset({"modifiers", "type_parameters"})


#: Comments are named nodes, and the grammar puts one INSIDE whatever it
#: interrupts: `Owner./*c*/NAME` is a navigation with three named children.
_COMMENTS = frozenset({"line_comment", "block_comment"})


def _text(node: Node) -> str:
    """A node's text, with an identifier's backticks dropped.

    `` `Flags` `` and ``Flags`` are one name, in an import, a qualifier, a
    supertype or a declaration alike. Compared with backticks kept, a
    backticked spelling of the owner or the name matches nothing, and a read
    spelled that way is skipped as somebody else's.
    """
    text = node.text.decode("utf-8", "replace")
    if node.type == "identifier" and len(text) > 2 and text[0] == text[-1] == "`":
        return text[1:-1]
    return text


def _parts(node: Node) -> list[Node]:
    """``node``'s named children, comments left out."""
    return [c for c in node.named_children if c.type not in _COMMENTS]


def _parse(source: str) -> Node:
    return kotlin_sentinel._parser().parse(source.encode("utf-8")).root_node


_walk = xk.walk


def _dotted(node: Node) -> str:
    return ".".join(_text(c) for c in node.named_children if c.type == "identifier") or _text(node)


def _package(root: Node) -> str:
    for child in root.named_children:
        if child.type == "package_header":
            named = [c for c in child.named_children if c.type == "qualified_identifier"]
            return _dotted(named[0]) if named else ""
    return ""


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    root = _parse(source)
    if root.has_error:
        return []
    package = _package(root)
    found: list[xk.KeyDeclaration] = []
    for const in key_const.key_consts(root, source.encode("utf-8"), "kt", flag_key):
        if "`" in const.name:
            # The mention scan searches for the name as written, so a reader
            # spelling it without backticks would never be looked at.
            continue
        visible = key_const_visibility.visible_key_consts([const], "kt")
        if not visible:
            continue  # private: no other file can name it
        decl = const.declaration
        parent = decl.parent
        if parent is None:
            continue
        if parent.type == "source_file":
            found.append(xk.KeyDeclaration(path, const.name, None, package))
            continue
        holder = parent.parent
        if parent.type != "class_body" or holder is None:
            continue
        companion = None
        if holder.type == "companion_object":
            named = next((c for c in holder.named_children if c.type == "identifier"), None)
            companion = _text(named) if named is not None else "Companion"
            body = holder.parent
            holder = body.parent if body is not None else None
            if body is None or body.type not in ("class_body", "enum_class_body"):
                continue
            if holder is None or holder.type != "class_declaration":
                continue
        elif holder.type != "object_declaration":
            continue  # a class member that is not in its companion
        if holder.parent is None or holder.parent.type != "source_file":
            continue  # nested: out of scope
        # An interface's companion has no owner here: every implementer reads
        # its members bare, so only the name could be asked of a reader.
        owner = visible[0].owner
        name = next((c for c in holder.named_children if c.type == "identifier"), None)
        if owner is None or name is None or _text(name) != owner:
            continue
        found.append(xk.KeyDeclaration(path, const.name, owner, package, companion))
    entries, _refused = _enum_entries(path, root, source, flag_key)
    return found + entries


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every top-level property, and every member of a top-level type, here.

    Whatever the value or the modifiers, and whatever the type: a companion's
    member is reached as ``Type.NAME`` exactly like an object's, and a nested
    type or enum entry of that name is what the qualifier would mean instead.
    Over-reporting only keeps a declaration.
    """
    root = _parse(source)
    if root.has_error:
        raise xk.DeclarationsUnreadable(path)
    package = _package(root)
    found: set[xk.QualifiedName] = set()
    for node in root.named_children:
        if node.type == "property_declaration":
            for var in _walk(node):
                if var.type == "variable_declaration":
                    name = next((c for c in var.named_children if c.type == "identifier"), None)
                    if name is not None:
                        found.add((package, None, _text(name)))
        elif node.type in ("class_declaration", "object_declaration"):
            owner = next((c for c in node.named_children if c.type == "identifier"), None)
            if owner is None:
                continue
            members, _ = _body_names(node)
            found |= {(package, _text(owner), member) for member in members}
    return found


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """``(package, name)`` of every top-level class, interface, object or typealias.

    ``enum class``, ``data class``, ``sealed`` and ``interface`` all parse as a
    ``class_declaration``. The file's own JVM facade counts as well, since a
    Java reader names it like a class: ``@file:JvmName("Name")`` when given.
    """
    root = _parse(source)
    if root.has_error:
        raise xk.DeclarationsUnreadable(path)
    package = _package(root)
    found: set[tuple[str, str]] = set()
    for node in root.named_children:
        if node.type in ("class_declaration", "object_declaration", "type_alias"):
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            if name is not None:
                found.add((package, _text(name)))
        elif node.type == "file_annotation":
            found |= {(package, name) for name in _jvm_names(path, node)}
    return found


def _jvm_names(path: str, annotation: Node) -> set[str]:
    """The names a ``@file:JvmName("...")`` annotation gives the file's facade.

    Only one plain string literal is read as a name. Anything else -- a
    concatenation, a template, a reference to a constant, a named argument --
    could spell any facade name at all, so the file's declarations are
    unreadable rather than guessed at.
    """
    names: set[str] = set()
    for node in _walk(annotation):
        if node.type != "constructor_invocation":
            continue
        user_type = next((c for c in node.named_children if c.type == "user_type"), None)
        parts = [c for c in user_type.named_children if c.type == "identifier"] if user_type else []
        if not parts or _text(parts[-1]) != "JvmName":
            continue
        name = _plain_string_argument(node)
        if name is None:
            raise xk.DeclarationsUnreadable(path)
        names.add(name)
    return names


def _plain_string_argument(invocation: Node) -> str | None:
    """The text of an invocation's sole argument when it is one plain string literal."""
    arguments = next((c for c in invocation.named_children if c.type == "value_arguments"), None)
    if arguments is None or len(arguments.named_children) != 1:
        return None
    argument = arguments.named_children[0]
    if argument.type != "value_argument" or len(argument.named_children) != 1:
        return None
    literal = argument.named_children[0]
    if literal.type not in ("string_literal", "multiline_string_literal"):
        return None
    parts = literal.named_children
    if len(parts) != 1 or parts[0].type != "string_content":
        return None
    text = _text(parts[0])
    if not text or "\\" in text or "$" in text:
        return None
    return text


# --- enum entries -------------------------------------------------------------


def _is_enum_class(node: Node) -> bool:
    modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
    return modifiers is not None and any(
        c.type == "class_modifier" and _text(c) == "enum" for c in modifiers.named_children
    )


def _plain_value(argument: Node) -> str | None:
    """A positional argument's value when it is one plain string literal."""
    if argument.type != "value_argument" or len(_parts(argument)) != 1:
        return None
    literal = _parts(argument)[0]
    if literal.type != "string_literal":
        return None
    pieces = literal.named_children
    if len(pieces) != 1 or pieces[0].type != "string_content":
        return None
    text = _text(pieces[0])
    return None if "\\" in text or "$" in text else text


def _enum_properties(enum: Node, body: Node) -> dict[int, str]:
    """Per primary-constructor position, the ``val`` property it declares.

    Only a ``val`` (never a ``var``) that is not private or protected. An
    enum with a secondary constructor proves nothing: which constructor an
    entry calls is overload resolution.
    """
    if any(c.type == "secondary_constructor" for c in body.named_children):
        return {}
    constructor = next((c for c in enum.named_children if c.type == "primary_constructor"), None)
    parameters = (
        next((c for c in constructor.named_children if c.type == "class_parameters"), None)
        if constructor is not None
        else None
    )
    found: dict[int, str] = {}
    for position, parameter in enumerate(
        p for p in (parameters.named_children if parameters is not None else []) if p.type == "class_parameter"
    ):
        modifiers = next((c for c in parameter.named_children if c.type == "modifiers"), None)
        hidden = modifiers is not None and any(w in _text(modifiers).split() for w in ("private", "protected"))
        name = next((c for c in parameter.named_children if c.type == "identifier"), None)
        if any(c.type == "val" for c in parameter.children) and not hidden and name is not None:
            found[position] = _text(name)
    return found


def _enum_entries(
    path: str, root: Node, source: str, flag_key: str
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    package = _package(root)
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for enum in _walk(root):
        if enum.type != "class_declaration" or not _is_enum_class(enum):
            continue
        body = next((c for c in enum.named_children if c.type == "enum_class_body"), None)
        entries = [e for e in body.named_children if e.type == "enum_entry"] if body else []
        held = xk.key_token(flag_key)
        holding = [
            e
            for e in entries
            if (args := next((c for c in e.named_children if c.type == "value_arguments"), None)) is not None
            and held.search(args.text.decode("utf-8", "replace"))
        ]
        if not holding:
            continue
        owner_node = next((c for c in enum.named_children if c.type == "identifier"), None)
        owner = _text(owner_node) if owner_node is not None else "?"
        top_level = enum.parent is not None and enum.parent.type == "source_file"
        properties = _enum_properties(enum, body) if top_level else {}
        for entry_node in holding:
            name = _text(next(c for c in entry_node.named_children if c.type == "identifier"))
            entry = f"{owner}.{name}"
            if not top_level:
                refused.append(xk.refusal(path, entry_node, xk.UNSUPPORTED_FORM, source, entry))
                continue
            arguments = next(c for c in entry_node.named_children if c.type == "value_arguments")
            spellings = {
                properties[i]
                for i, argument in enumerate(_parts(arguments))
                if i in properties and _plain_value(argument) == flag_key
            }
            named = any(len(_parts(a)) != 1 for a in _parts(arguments) if a.type == "value_argument")
            has_body = any(c.type == "class_body" for c in entry_node.named_children)
            if root.has_error or named or has_body or not spellings or "`" in name:
                refused.append(xk.refusal(path, entry_node, xk.ENUM_UNPROVEN, source, entry))
                continue
            found.append(
                xk.KeyDeclaration(
                    path, name, owner, package, kind=xk.ENUM_ENTRY, accessors=frozenset(spellings)
                )
            )
    return found, refused


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """Unprovable entries, and reads of proven ones inside their own file."""
    root = _parse(source)
    found, refused = _enum_entries(path, root, source, flag_key)
    for decl in found:
        for node in _walk(root):
            if (
                node.type == "identifier"
                and _text(node) == decl.name
                and (node.parent is None or node.parent.type != "enum_entry")
            ):
                refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry))
                break
    return refused


@dataclass(frozen=True)
class _Import:
    target: str
    star: bool
    #: The simple name the import binds in the file, or None for a star import.
    local: str | None
    node: Node


def _imports(root: Node) -> list[_Import]:
    found: list[_Import] = []
    for node in root.named_children:
        if node.type != "import":
            continue
        named = node.named_children
        if not named:
            continue
        target = _dotted(named[0])
        star = any(c.type == "*" for c in node.children)
        alias = named[-1] if len(named) > 1 and named[-1].type == "identifier" else None
        local = None if star else (_text(alias) if alias is not None else target.rsplit(".", 1)[-1])
        found.append(_Import(target, star, local, node))
    return found


def _in_header(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in ("import", "package_header"):
            return True
        parent = parent.parent
    return False


#: Parents under which a DIRECT ``identifier`` child surely names no value:
#: a type being used or declared, a function being declared.
_NOT_A_VALUE = frozenset(
    {
        "user_type",
        "class_declaration",
        "object_declaration",
        "companion_object",
        "function_declaration",
    }
)


#: Every node that declares a type name.
_DECLARED_TYPES = ("class_declaration", "object_declaration", "companion_object", "type_alias")


def _may_be_value(node: Node) -> bool:
    """Whether this ``identifier`` (outside the header) can name a value.

    Decided by elimination, not by listing binders: a type use or a type or
    function declaration cannot, and neither can the qualifier of a member
    access or the callee of a call, which only USE the name. Anything else --
    a property, a parameter, a destructured or pattern name, an enum entry, a
    use as a value, a binder form this list has never heard of -- can.
    """
    parent = node.parent
    if parent is None:
        return True
    if parent.type in _NOT_A_VALUE:
        return False
    if parent.type in ("navigation_expression", "call_expression"):
        return not (parent.named_children and parent.named_children[0].id == node.id)
    return True


def _is_binding(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type in _BINDERS


def _is_extension(declaration: Node, name_types: tuple[str, ...]) -> bool:
    for child in declaration.named_children:
        if child.type in name_types:
            return False
        if child.type not in _NOT_A_RECEIVER:
            return True
    return False


def _under_implicit_receiver(node: Node) -> bool:
    """Whether a receiver Kotlin consults before imports may be in scope here."""
    parent = node.parent
    while parent is not None:
        if parent.type in ("lambda_literal", "anonymous_function"):
            return True
        if parent.type == "function_declaration" and _is_extension(parent, ("identifier",)):
            return True
        if parent.type == "property_declaration" and _is_extension(
            parent, ("variable_declaration", "multi_variable_declaration")
        ):
            return True
        parent = parent.parent
    return False


def _in_type_body(node: Node) -> bool:
    """Whether ``node`` runs with a type's ``this`` as an implicit receiver."""
    parent = node.parent
    while parent is not None:
        if parent.type in _TYPE_DECLS:
            return True
        parent = parent.parent
    return False


def _is_read(value: Node, accessors: frozenset[str]) -> bool:
    """``value`` is the whole first argument of a call the pre-pass marks."""
    argument = value.parent
    if argument is None or argument.type != "value_argument":
        return False
    if [c.id for c in argument.named_children] != [value.id]:
        return False  # a named argument, or a spread
    arguments = argument.parent
    if arguments is None or arguments.type != "value_arguments":
        return False
    if not arguments.named_children or arguments.named_children[0].id != argument.id:
        return False
    call = arguments.parent
    if call is None or call.type != "call_expression" or not call.named_children:
        return False
    first_arguments = next(
        (c for c in call.named_children if c.type == "value_arguments"), None
    )
    if first_arguments is None or first_arguments.id != arguments.id:
        return False
    callee = call.named_children[0]
    if kotlin_sentinel._is_optional_call(callee):
        return False
    return kotlin_sentinel._method_name(callee) in (_READ_CALLEES | accessors)


def _labelled(value: Node) -> bool:
    """Whether ``value`` sits in a named argument (as its value or its label)."""
    parent = value.parent
    return (
        parent is not None
        and parent.type == "value_argument"
        and [c.id for c in parent.named_children] != [value.id]
    )


def _operator(navigation: Node) -> str:
    return "".join(c.type for c in navigation.children if not c.is_named)


def _entry_read(
    head: Node, decl: xk.KeyDeclaration, accessors: frozenset[str]
) -> tuple[bool, str]:
    """Whether ``head.<property>`` is a proven accessor read used as a read's key."""
    outer = head.parent
    if outer is None or outer.type != "navigation_expression":
        return False, ""
    named = _parts(outer)
    if len(named) != 2 or named[0].id != head.id or named[1].type != "identifier":
        return False, ""
    if _operator(outer) != ".":
        return False, ""
    accessor = _text(named[1])
    if accessor not in decl.accessors:
        return False, ""
    return _is_read(outer, accessors) and not _labelled(outer), accessor


def _is_when_condition(node: Node) -> bool:
    """Whether ``node`` is a branch condition of a ``when`` (before the ``->``)."""
    entry = node.parent
    if entry is None or entry.type != "when_entry":
        return False
    arrow = next((c for c in entry.children if c.type == "->"), None)
    return arrow is not None and node.end_byte <= arrow.start_byte


def _names_companion(node: Node, decl: xk.KeyDeclaration) -> bool:
    """Whether ``node`` is ``<identifier>.<decl's companion>``, joined by a plain ``.``.

    Only the companion's own name counts: a named companion is not reachable
    as ``Owner.Companion``. Whether the identifier is the owner is the
    caller's question.
    """
    if decl.companion is None or node.type != "navigation_expression":
        return False
    named = _parts(node)
    return (
        len(named) == 2
        and all(n.type == "identifier" for n in named)
        and _operator(node) == "."
        and _text(named[1]) == decl.companion
    )


# --- supertypes declared in the repository ---------------------------------------


@dataclass(frozen=True)
class _TypeInfo:
    #: Every name a subtype's body can reach unqualified through this type:
    #: properties, functions, nested types, enum entries, companion members.
    names: frozenset[str]
    supertypes: tuple[str, ...]
    #: The file's package, and whether the type sits at the top of its file
    #: (so ``package.Name`` names it).
    package: str = ""
    top_level: bool = False


@dataclass(frozen=True)
class _TypeIndex:
    by_name: dict[str, tuple[_TypeInfo, ...]]
    #: Some file could not be read or fully parsed. What it declares is then
    #: answered by the three fields below, never by ``by_name``.
    incomplete: bool
    #: Every top-level extension property, in any package. Inside a type body
    #: one whose receiver matches `this` is found BEFORE any import or
    #: same-package declaration, and its receiver can be `Any`.
    extensions: frozenset[str] = frozenset()
    #: Every name any type here declares in its body: what the receiver of a
    #: lambda (`with(cfg) { ... }`) can supply before an import does.
    members: frozenset[str] = frozenset()
    #: Every top-level property and every typealias, in any package: a name
    #: that can stand for an object (`val Keys = RuntimeKeys`) without being
    #: a type of its own.
    values: frozenset[str] = frozenset()
    #: Typealias name -> the simple names it may stand for, in any package. A
    #: supertype spelled through one (`class Sub : Base()` with
    #: `typealias Base = Flags`) is that type, and inherits what it does.
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: A file could not be read, or its scan could not name what it declares:
    #: it may declare anything.
    unread: bool = False
    #: Every name an unparseable file may declare a type (or typealias) under.
    unparsed_types: frozenset[str] = frozenset()
    #: Every identifier an unparseable file spells: any member, extension or
    #: top-level property it declares is among them.
    unparsed_words: frozenset[str] = frozenset()

    def may_declare_type(self, name: str) -> bool:
        """Whether a file this index could not see whole may declare type ``name``."""
        return self.unread or name in self.unparsed_types

    def may_declare_name(self, name: str) -> bool:
        """Whether a file this index could not see whole may declare anything ``name``."""
        return self.unread or name in self.unparsed_words


#: A keyword that declares a named type, and what follows it. Over-approximates
#: (a keyword in a string or comment counts too), which only keeps a declaration.
_TYPE_KEYWORD = re.compile(r"\b(?:class|interface|object|typealias)\b\s*(\S?)")
_NAME_AT = re.compile(r"`([^`\n]+)`|([^\W\d]\w*)")
_WORD = re.compile(r"[^\W\d]\w*")
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def _unparsed_names(text: str) -> tuple[frozenset[str] | None, frozenset[str]]:
    """(type names, words) a file the grammar cannot parse whole may declare.

    Read from the text, so they hold however the parse went wrong: Kotlin
    names a type straight after its keyword, and no declaration of any name
    can avoid spelling it. Type names are None when a comment sits between a
    keyword and what follows, which hides the name: the file may then declare
    any type. Anything else after the keyword (`object :`, `::class.java`)
    declares no named type.
    """
    types: set[str] = set()
    for match in _TYPE_KEYWORD.finditer(text):
        if match.group(1) == "/":
            return None, frozenset()
        name = _NAME_AT.match(text, match.start(1))
        if name is not None:
            types.add(name.group(1) or name.group(2))
    words = set(_WORD.findall(text)) | set(_BACKTICKED.findall(text))
    return frozenset(types), frozenset(words)


def _renames(root: Node) -> dict[str, str]:
    """Local name -> simple name of what it imports, for each renaming import here.

    ``import app.Flags as Base`` makes ``Base`` mean ``Flags`` in this file,
    a supertype position included.
    """
    found: dict[str, str] = {}
    for imp in _imports(root):
        simple = imp.target.rsplit(".", 1)[-1]
        if imp.local is not None and imp.local != simple:
            found[imp.local] = simple
    return found


def _with_renames(names: list[str], renames: dict[str, str]) -> list[str]:
    """``names``, plus what each renaming import among them stands for."""
    return names + [renames[n] for n in names if n in renames]


def _alias_target(alias: Node, renames: dict[str, str]) -> list[str]:
    """The simple names a ``typealias`` may stand for, through a renaming import too."""
    target = next((c for c in alias.named_children if c.type == "user_type"), None)
    parts = [c for c in target.named_children if c.type == "identifier"] if target else []
    return _with_renames([_text(parts[-1])], renames) if parts else []


def _local_aliases(root: Node) -> dict[str, list[str]]:
    """Typealias name -> simple names it may stand for, declared in this file."""
    renames = _renames(root)
    found: dict[str, list[str]] = {}
    for node in _walk(root):
        if node.type != "type_alias":
            continue
        name = next((c for c in node.named_children if c.type == "identifier"), None)
        if name is not None:
            found.setdefault(_text(name), []).extend(_alias_target(node, renames))
    return found


def _supertypes(type_node: Node) -> list[str]:
    names: list[str] = []
    for child in type_node.named_children:
        if child.type != "delegation_specifiers":
            continue
        for user_type in _walk(child):
            if user_type.type != "user_type":
                continue
            if user_type.parent is not None and user_type.parent.type == "type_projection":
                continue  # a type argument, not a supertype
            parts = [c for c in user_type.named_children if c.type == "identifier"]
            if parts:
                names.append(_text(parts[-1]))
    return names


def _property_names(declaration: Node) -> set[str]:
    names: set[str] = set()
    for var in _walk(declaration):
        if var.type == "variable_declaration":
            name = next((c for c in var.named_children if c.type == "identifier"), None)
            if name is not None:
                names.add(_text(name))
    return names


def _body_names(type_node: Node) -> tuple[set[str], list[str]]:
    """(names a subtype reaches, extra supertypes) of one type's body."""
    names: set[str] = set()
    extra: list[str] = []
    for child in type_node.named_children:
        if child.type == "primary_constructor":
            for param in _walk(child):
                if param.type == "class_parameter":
                    name = next((c for c in param.named_children if c.type == "identifier"), None)
                    if name is not None:
                        names.add(_text(name))
        if child.type not in ("class_body", "enum_class_body"):
            continue
        for member in child.named_children:
            if member.type == "property_declaration":
                for var in _walk(member):
                    if var.type == "variable_declaration":
                        name = next((c for c in var.named_children if c.type == "identifier"), None)
                        if name is not None:
                            names.add(_text(name))
            elif member.type in ("function_declaration", "class_declaration", "object_declaration", "type_alias", "enum_entry"):
                name = next((c for c in member.named_children if c.type == "identifier"), None)
                if name is not None:
                    names.add(_text(name))
            elif member.type == "companion_object":
                name = next((c for c in member.named_children if c.type == "identifier"), None)
                names.add(_text(name) if name is not None else "Companion")
                more, more_super = _body_names(member)
                names |= more
                extra.extend(_supertypes(member))
                extra.extend(more_super)
    return names, extra


@dataclass(frozen=True)
class _FileShare:
    """One Kotlin file's share of the type index."""

    types: tuple[tuple[str, _TypeInfo], ...]
    extensions: frozenset[str]
    members: frozenset[str]
    values: frozenset[str]
    aliases: tuple[tuple[str, tuple[str, ...]], ...]
    #: For a file that did not parse whole, what :func:`_unparsed_names` read
    #: from its text; ``(None, ...)`` when it may declare any type.
    unparsed: tuple[frozenset[str] | None, frozenset[str]] | None = None


def _share_of(text: str) -> tuple[_FileShare, bool]:
    """One Kotlin file's share of the type index, and whether it parsed cleanly."""
    tree = _parse(text)
    renames = _renames(tree)
    types: list[tuple[str, _TypeInfo]] = []
    extensions: set[str] = set()
    members: set[str] = set()
    values: set[str] = set()
    aliases: list[tuple[str, tuple[str, ...]]] = []
    for node in tree.named_children:
        if node.type == "property_declaration":
            values |= _property_names(node)
            if _is_extension(node, ("variable_declaration", "multi_variable_declaration")):
                extensions |= _property_names(node)
    for node in _walk(tree):
        if node.type == "type_alias":
            alias = next((c for c in node.named_children if c.type == "identifier"), None)
            if alias is not None:
                values.add(_text(alias))
                aliases.append((_text(alias), tuple(_alias_target(node, renames))))
            continue
        if node.type not in ("class_declaration", "object_declaration", "companion_object"):
            continue
        name = next((c for c in node.named_children if c.type == "identifier"), None)
        names, extra = _body_names(node)
        members |= names
        if name is None:
            continue
        types.append(
            (
                _text(name),
                _TypeInfo(
                    frozenset(names),
                    tuple(_with_renames(_supertypes(node) + extra, renames)),
                    _package(tree),
                    node.parent is not None and node.parent.type == "source_file",
                ),
            )
        )
    share = _FileShare(
        tuple(types),
        frozenset(extensions),
        frozenset(members),
        frozenset(values),
        tuple(aliases),
        _unparsed_names(text) if tree.has_error else None,
    )
    return share, not tree.has_error


def _combine(
    shares: list[tuple[str, _FileShare]], incomplete: bool, unreadable: bool
) -> _TypeIndex:
    by_name: dict[str, list[_TypeInfo]] = {}
    unread = unreadable
    unparsed_types: set[str] = set()
    unparsed_words: set[str] = set()
    extensions: set[str] = set()
    members: set[str] = set()
    values: set[str] = set()
    aliases: dict[str, list[str]] = {}
    for _path, share in shares:
        for name, info in share.types:
            by_name.setdefault(name, []).append(info)
        extensions |= share.extensions
        members |= share.members
        values |= share.values
        for alias, targets in share.aliases:
            aliases.setdefault(alias, []).extend(targets)
        if share.unparsed is not None:
            types, words = share.unparsed
            if types is None:
                unread = True
            else:
                unparsed_types |= types
                unparsed_words |= words
    return _TypeIndex(
        {k: tuple(v) for k, v in by_name.items()},
        incomplete,
        frozenset(extensions),
        frozenset(members),
        frozenset(values),
        {k: tuple(v) for k, v in aliases.items()},
        unread,
        frozenset(unparsed_types),
        frozenset(unparsed_words),
    )


#: Every Kotlin type, extension property, member name, top-level value and
#: typealias in a checkout.
_kotlin_index = xk.CheckoutIndex("Kotlin", (".kt", ".kts"), _share_of, _combine)


def _inherited(root: Node, repo_dir: str) -> tuple[frozenset[str], bool]:
    """(names, unknown) the reader's types reach through repository supertypes.

    Every type in the reader counts, object expressions and companions
    included, and each simple name is followed through every repository type
    of that name, Kotlin or Java: over-collecting only keeps a declaration.
    ``unknown`` is True when the reader has a supertype and the Java index is
    incomplete, or when a Kotlin file that could not be parsed whole may
    declare a type anywhere up the chain: that type's members and supertypes
    are then unknown. A file that cannot declare any of them changes nothing.

    A supertype spelled through a renaming import or a typealias is followed
    to what it stands for, here and further up the chain: a subclass reads a
    companion's members bare whatever it called the class.
    """
    from flag_cleanup import cross_file_java

    renames = _renames(root)
    pending: list[str] = []
    for node in _walk(root):
        if node.type in _TYPE_DECLS:
            pending.extend(_with_renames(_supertypes(node), renames))
    if not pending:
        return frozenset(), False
    local_aliases = _local_aliases(root)
    kotlin = _kotlin_index(repo_dir)
    java = cross_file_java._type_index(repo_dir)
    names: set[str] = set()
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(kotlin.aliases.get(name, ()))
        pending.extend(local_aliases.get(name, ()))
        for info in kotlin.by_name.get(name, ()):
            names |= info.names
            pending.extend(info.supertypes)
        for info in java.by_name.get(name, ()):
            names |= info.fields | info.member_types
            pending.extend(info.supertypes)
    return frozenset(names), java.incomplete or any(kotlin.may_declare_type(n) for n in seen)


class _ReceiverNames:
    """What an implicit receiver may supply before an import, asked lazily.

    The indexes are only consulted for a read that has a receiver in scope,
    so a reader of plain top-level functions never walks the repository.
    """

    def __init__(self, repo_dir: str):
        self._repo_dir = repo_dir
        self._facts: tuple[frozenset[str], frozenset[str], bool] | None = None
        self._kotlin: _TypeIndex | None = None

    def _may_declare(self, name: str) -> bool:
        """Whether a Kotlin file not seen whole may declare a member or extension ``name``."""
        if self._kotlin is None:
            self._kotlin = _kotlin_index(self._repo_dir)
        return self._kotlin.may_declare_name(name)

    def _get(self) -> tuple[frozenset[str], frozenset[str], bool]:
        if self._facts is None:
            from flag_cleanup import cross_file_java

            kotlin = _kotlin_index(self._repo_dir)
            java = cross_file_java._type_index(self._repo_dir)
            members = set(kotlin.members)
            for infos in java.by_name.values():
                for info in infos:
                    members |= info.fields | info.member_types
            self._facts = (
                kotlin.extensions,
                frozenset(members),
                not java.incomplete,
            )
        return self._facts

    def claim_bare(self, node: Node, name: str) -> bool:
        """An extension property on a type's ``this`` can be ``name`` instead."""
        if not _in_type_body(node):
            return False
        extensions, _members, complete = self._get()
        return not complete or name in extensions or self._may_declare(name)

    def claim_owner(self, navigation: Node, owner: str) -> bool:
        """Something reached through an implicit receiver can be ``owner`` instead.

        Inside a type, an extension property of that name. Inside a lambda,
        an anonymous function or an extension, any member of that name a
        repository type declares too: the receiver's type is not known here.
        Either question fails closed when the Java index is incomplete, or
        when a Kotlin file that could not be parsed whole spells ``owner``.
        """
        in_type = _in_type_body(navigation)
        in_receiver = _under_implicit_receiver(navigation)
        if not (in_type or in_receiver):
            return False
        extensions, members, complete = self._get()
        if not complete or owner in extensions or self._may_declare(owner):
            return True
        return in_receiver and owner in members


# --- the profile -----------------------------------------------------------------


_refusal = xk.refusal


def _member_target(decl: xk.KeyDeclaration) -> str:
    """What a member import of ``decl`` names.

    A companion member is imported through its companion, never through the
    class alone: ``import app.Flags.NAME`` does not compile.
    """
    parts = [decl.module, decl.owner, decl.companion, decl.name]
    return ".".join(p for p in parts if p)


def _owner_target(decl: xk.KeyDeclaration) -> str:
    return ".".join(p for p in (decl.module, decl.owner) if p)


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    root = _parse(source)
    if root.has_error:
        lines = source.splitlines()

        def spelled_at(names: set[str]) -> int | None:
            return next((i for i, t in enumerate(lines) if any(n in t for n in names)), None)

        def refusal_at(index: int, entry: str) -> xk.Refusal:
            return xk.Refusal(
                path, index + 1, xk.UNSUPPORTED_FORM, lines[index].strip() if lines else "", entry
            )

        found: list[xk.Refusal] = []
        constants = {d.name for d in declarations if d.kind != xk.ENUM_ENTRY}
        if not declarations:
            found.append(refusal_at(0, ""))
        elif constants and (at := spelled_at(constants)) is not None:
            found.append(refusal_at(at, ""))
        for d in declarations:
            if d.kind == xk.ENUM_ENTRY and (at := spelled_at({d.name})) is not None:
                found.append(refusal_at(at, d.entry))
        return [], found
    package = _package(root)
    imports = _imports(root)
    by_local: dict[str, list[_Import]] = {}
    for imp in imports:
        if imp.local is not None:
            by_local.setdefault(imp.local, []).append(imp)
    inherited, inherits_unknown = _inherited(root, repo_dir)
    # A script's top level runs against a receiver its host supplies (a
    # Gradle `Project`, say), and that receiver's members come before imports.
    script = path.endswith(".kts")
    receivers = _ReceiverNames(repo_dir)
    from flag_cleanup import cross_file_java

    jvm = cross_file_java._JvmTypes(repo_dir)
    # Types this file declares: at its top, and everywhere else (nested,
    # local, companion, typealias), which can shadow a top-level type.
    own_top_level_types: set[str] = set()
    nested_types: set[str] = set()
    for node in _walk(root):
        if node.type not in _DECLARED_TYPES:
            continue
        named = next((c for c in node.named_children if c.type == "identifier"), None)
        if named is None:
            continue
        top = node.parent is not None and node.parent.type == "source_file"
        own = top and node.type != "type_alias"
        (own_top_level_types if own else nested_types).add(_text(named))
    identifiers = [
        n for n in _walk(root) if n.type == "identifier" and not _in_header(n)
    ]
    strings = [n for n in _walk(root) if n.type == "string_content"]

    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []
    for decl in declarations:
        # Any star import of another package might supply either name; the
        # declaring package's own is harmless.
        foreign_star = any(imp.star and imp.target != decl.module for imp in imports)
        is_entry = decl.kind == xk.ENUM_ENTRY

        owner_ok = False
        if decl.owner is not None:
            owner_imports = [imp.target for imp in by_local.get(decl.owner, [])]
            # A renaming import of the owner hides its own name, even from
            # the declaring package.
            renamed = any(
                imp.target == _owner_target(decl) and imp.local != decl.owner
                for imp in imports
            )
            owner_ok = (
                owner_imports == [_owner_target(decl)]
                or (not owner_imports and not renamed and package == decl.module)
            ) and not (
                foreign_star
                or decl.owner in inherited
                or inherits_unknown
                or any(
                    _text(n) == decl.owner and _is_binding(n) for n in identifiers
                )
            )

        # The simple names that mean this declaration here, each bound once.
        target = _member_target(decl)
        bound: dict[str, _Import | None] = {}
        for imp in imports:
            if not imp.star and imp.target == target and imp.local is not None:
                bound[imp.local] = imp
        if (
            decl.owner is None
            and package == decl.module
            and decl.name not in by_local
            and not bound  # a renaming import hides the declared name
        ):
            bound[decl.name] = None
        rebound = {
            local
            for local, imp in bound.items()
            if len(by_local.get(local, [])) != (1 if imp is not None else 0)
            or foreign_star
            or local in inherited
            or inherits_unknown
        }
        spelled = {decl.name} | set(bound)

        def class_may_hold(target: str, decl: xk.KeyDeclaration = decl) -> bool:
            """Whether importing from ``target`` can bring ``decl``'s member in."""
            return jvm.may_hold(target.rsplit(".", 1)[-1], decl.owner)

        def bare_reachable(node: Node, decl: xk.KeyDeclaration = decl) -> bool:
            """Whether an unbound bare ``NAME`` here could still mean ``decl``.

            A top-level constant is reached unimported only from its own
            package or through a star import of it. An object member, through
            a star or member import from the owner (or a repository class
            that may extend a Java owner -- a library class cannot), through
            a repository supertype that declares the name, or through an
            implicit receiver that may be the owning object (a lambda with
            receiver, an extension, a script's host). A mention none of those
            reaches is another declaration of the same name -- a sibling
            object's own ``FLAG_KEY`` -- and is skipped without a refusal.
            """
            if script:
                return True
            if decl.owner is None:
                return package == decl.module or any(
                    imp.star and imp.target == decl.module for imp in imports
                )
            if _under_implicit_receiver(node) or decl.name in inherited or inherits_unknown:
                return True
            for imp in imports:
                if imp.star and class_may_hold(imp.target):
                    return True
                if (
                    not imp.star
                    and imp.local == decl.name
                    and "." in imp.target
                    and class_may_hold(imp.target.rsplit(".", 1)[0])
                ):
                    return True
            return False

        def unrelated_qualifier(
            navigation: Node, name: str, decl: xk.KeyDeclaration = decl
        ) -> bool:
            """Whether ``name.NAME`` surely reads another type's member, not ``decl``.

            ``name`` must mean one top-level repository type here and nothing
            else. No identifier spelling it in the file may be a value
            (decided by elimination, see :func:`_may_be_value`), and no nested
            or local type of that name may be declared in the file. It must
            resolve to that type: imported by exactly one import whose target
            is a top-level repository type of that package (a property or a
            renamed member is not one), or -- only with no import of it --
            declared at the top of this file or a top-level repository type of
            this package. It must not be reachable through a supertype or an
            implicit receiver, and no top-level property or typealias of that
            name may exist anywhere. That type must be neither the owner nor
            extend it, with every JVM file indexed. A sibling object's
            ``SearchGate.FLAG_KEY`` is the case this exists for.
            """
            if decl.owner is None or inherits_unknown or name in inherited:
                return False
            if any(_text(n) == name and _may_be_value(n) for n in identifiers):
                return False  # a value of that name (a type of it is the point)
            if name in nested_types:
                return False
            if receivers.claim_owner(navigation, name):
                return False
            if not resolves_to_repository_type(name):
                return False
            return jvm.unrelated_type(name, decl.owner)

        def resolves_to_repository_type(name: str) -> bool:
            # An explicit import outranks every package member in Kotlin's
            # file scope, this file's own top-level declarations included,
            # so an import binding the name decides it.
            named = by_local.get(name, [])
            if len(named) > 1:
                return False
            if named:
                target = named[0].target
                if "." not in target or target.rsplit(".", 1)[-1] != name:
                    return False
                return jvm.top_level_type(name, target.rsplit(".", 1)[0])
            return name in own_top_level_types or jvm.top_level_type(name, package)

        proven_locals: set[str] = set()
        for node in identifiers:
            name = _text(node)
            if name not in spelled:
                continue
            parent = node.parent
            parts = _parts(parent) if parent is not None else []
            if is_entry:
                entry = decl.entry
                qualified = (
                    parent is not None
                    and parent.type == "navigation_expression"
                    and len(parts) == 2
                    and parts[1].id == node.id
                )
                if qualified:
                    if name != decl.name:
                        continue  # `x.Alias`: a member of something else
                    left = parts[0]
                    operator = _operator(parent)
                    read, accessor = _entry_read(parent, decl, accessors)
                    if (
                        left.type == "identifier"
                        and _text(left) != decl.owner
                        and operator == "."
                        and unrelated_qualifier(parent, _text(left))
                    ):
                        continue  # another type's own member of the same name
                    if left.type != "identifier" or _text(left) != decl.owner or operator != ".":
                        refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source, entry))
                    elif not owner_ok or receivers.claim_owner(parent, decl.owner):
                        refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source, entry))
                    elif not read:
                        refused.append(_refusal(path, node, xk.NOT_A_READ, source, entry))
                    elif script:
                        refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source, entry))
                    else:
                        reads.append(xk.KeyReference(path, decl.owner, decl.name, accessor, decl))
                    continue
                if parent is not None and parent.type == "navigation_expression" and parts[0].id != node.id:
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source, entry))
                    continue
                if parent is not None and parent.type == "callable_reference":
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source, entry))
                    continue
                if name == decl.name and _is_when_condition(node):
                    # Resolved from the subject's type, with no import needed.
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source, entry))
                    continue
                if name not in bound:
                    if name == decl.name and bare_reachable(node):
                        reason = xk.REBOUND if _is_binding(node) else xk.IMPORT_UNRESOLVED
                        refused.append(_refusal(path, node, reason, source, entry))
                    continue
                read, accessor = _entry_read(node, decl, accessors)
                if (
                    _is_binding(node)
                    or name in rebound
                    or _under_implicit_receiver(node)
                    or receivers.claim_bare(node, name)
                ):
                    refused.append(_refusal(path, node, xk.REBOUND, source, entry))
                elif not read:
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source, entry))
                elif script:
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source, entry))
                else:
                    reads.append(xk.KeyReference(path, None, name, accessor, decl))
                    proven_locals.add(name)
                continue
            if (
                parent is not None
                and parent.type == "navigation_expression"
                and parts[0].id != node.id
            ):
                if name != decl.name:
                    continue  # `x.Alias`: a member of something else
                if len(parts) != 2 or parts[1].id != node.id:
                    # A member position this profile cannot read. It is never
                    # a bare name, so it must not fall through to the bare
                    # branch and be waved off as another declaration's.
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                    continue
                left = parts[0]
                operator = _operator(parent)
                # `Owner.Companion.NAME` is `Owner.NAME` spelled out: the
                # owner is what has to be proven either way.
                qualifier = (
                    _parts(left)[0] if _names_companion(left, decl) else left
                )
                if (
                    qualifier.id == left.id
                    and left.type == "identifier"
                    and _text(left) != decl.owner
                    and operator in (".", "::")
                    and unrelated_qualifier(parent, _text(left))
                ):
                    continue  # another type's own member of the same name
                if (
                    decl.owner is None
                    or qualifier.type != "identifier"
                    or _text(qualifier) != decl.owner
                    or operator not in (".", "::")
                ):
                    # A qualifier this profile cannot prove: a package path,
                    # `?.`, another object or an alias of ours.
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                elif not owner_ok or receivers.claim_owner(parent, decl.owner):
                    refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source))
                elif _labelled(parent):
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                elif operator == "::" or not _is_read(parent, accessors):
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source))
                elif script:
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                else:
                    owner_text = (
                        decl.owner
                        if qualifier.id == left.id
                        else f"{decl.owner}.{decl.companion}"
                    )
                    reads.append(xk.KeyReference(path, owner_text, decl.name))
                continue
            if parent is not None and parent.type == "callable_reference":
                # `Owner::NAME` or `::NAME`: the constant handed on, not read.
                refused.append(_refusal(path, node, xk.NOT_A_READ, source))
            elif name not in bound:
                if not bare_reachable(node):
                    continue  # another declaration of the same name
                reason = xk.REBOUND if _is_binding(node) else xk.IMPORT_UNRESOLVED
                refused.append(_refusal(path, node, reason, source))
            elif (
                _is_binding(node)
                or name in rebound
                or _under_implicit_receiver(node)
                or receivers.claim_bare(node, name)
            ):
                refused.append(_refusal(path, node, xk.REBOUND, source))
            elif _labelled(node):
                refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
            elif not _is_read(node, accessors):
                refused.append(_refusal(path, node, xk.NOT_A_READ, source))
            elif script:
                refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
            else:
                reads.append(xk.KeyReference(path, None, name))
                proven_locals.add(name)
        # `"$NAME"` has no identifier node, and a string naming the constant
        # may be a reflective lookup of it.
        word = xk.mention_pattern(spelled)
        for node in strings:
            if word.search(_text(node)):
                refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry))
        # An import of the member is removed with the last read it served.
        # With no read to remove, it would name a deleted declaration.
        for local, imp in bound.items():
            if not is_entry and imp is not None and local not in proven_locals:
                refused.append(_refusal(path, imp.node, xk.NOT_A_READ, source))
    if refused:
        return [], refused
    return reads, refused


def drop_stranded_imports(
    source: str, declarations: tuple[xk.KeyDeclaration, ...]
) -> str:
    """``source`` without each import of a resolved constant nothing uses any more.

    The fold removes a proven read, and with the last one the import that
    bound it names a declaration the same run deletes. The import of an
    OWNER that only a folded ``Owner.NAME`` read used is stranded the same
    way, though harmlessly, and goes too. Only a whole-line import is removed,
    and only when no identifier, string or comment in the file still spells
    the name it binds; anything else is left for the caller's still-mentions
    check to refuse, or left in place.
    """
    targets = {_member_target(d) for d in declarations}
    targets |= {_owner_target(d) for d in declarations if d.owner is not None}
    root = _parse(source)
    if root.has_error:
        return source
    imports = [imp for imp in _imports(root) if not imp.star and imp.target in targets]
    if not imports:
        return source
    used = {
        _text(n) for n in _walk(root) if n.type == "identifier" and not _in_header(n)
    }
    contents = " ".join(
        _text(n)
        for n in _walk(root)
        if n.type in ("string_content", "line_comment", "block_comment")
    )
    raw = source.encode("utf-8")
    spans: list[tuple[int, int]] = []
    for imp in imports:
        if imp.local is None or imp.local in used:
            continue
        if xk.mention_pattern([imp.local]).search(contents):
            continue
        start = raw.rfind(b"\n", 0, imp.node.start_byte) + 1
        end = raw.find(b"\n", imp.node.end_byte)
        end = len(raw) if end == -1 else end + 1
        if raw[start:end].strip() != raw[imp.node.start_byte : imp.node.end_byte].strip():
            continue  # shares its line with something else
        spans.append((start, end))
    for start, end in sorted(spans, reverse=True):
        raw = raw[:start] + raw[end:]
    return raw.decode("utf-8")


_DYNAMIC_MEMBERS = frozenset({"entries", "values", "valueOf"})
_DYNAMIC_FUNCTIONS = frozenset({"enumValues", "enumValueOf", "enumEntries"})
#: ``<Flags>`` or ``<app.Flags>`` right after a dynamic function's name.
_TYPE_ARGUMENT = re.compile(rb"\s*<\s*(?:`?\w+`?\s*\.\s*)*`?(\w+)`?\s*>")


def _last_name(node: Node) -> str | None:
    """The simple name ``Flags``, ``app.Flags`` (an expression) or ``app.Flags`` (a type) ends in."""
    while node.type in ("navigation_expression", "user_type") and _parts(node):
        node = _parts(node)[-1]
    return _text(node) if node.type == "identifier" else None


def enum_dynamic_uses(
    path: str, source: str, entries: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """``Flags.entries``/``values()``/``valueOf()``, ``enumValues<Flags>()``,
    ``enumValueOf<Flags>(…)`` and ``Flags::class`` in ``path``, matched on the
    owner's simple name, qualified (``app.Flags.entries``) or through an
    import alias (``import app.Flags as F``)."""
    root = _parse(source)
    by_owner: dict[str, list[xk.KeyDeclaration]] = {}
    for decl in entries:
        if decl.owner is not None:
            by_owner.setdefault(decl.owner, []).append(decl)
    # `import app.Flags as F`: `F.entries` is the enum's too.
    aliases: dict[str, str] = {}
    for imported in _imports(root):
        if imported.local is not None:
            aliases.setdefault(imported.local, imported.target.rsplit(".", 1)[-1])
    raw = source.encode("utf-8")
    notes: list[xk.Refusal] = []
    for node in _walk(root):
        names: set[str] = set()
        if node.type == "navigation_expression":
            named = _parts(node)
            if (
                len(named) == 2
                and named[1].type == "identifier"
                and (
                    (_operator(node) == "." and _text(named[1]) in _DYNAMIC_MEMBERS)
                    or (_operator(node) == "::" and _text(named[1]) in _DYNAMIC_MEMBERS | {"class"})
                )
            ):
                names.add(_last_name(named[0]) or "")
        elif node.type == "callable_reference" and node.children:
            # `Flags::class` / `app.Flags::valueOf` where the grammar reads
            # the receiver as a type rather than an expression.
            member = node.children[-1]
            if member.type == "class" or (
                member.type == "identifier" and _text(member) in _DYNAMIC_MEMBERS
            ):
                receiver = next((c for c in _parts(node) if c.id != member.id), None)
                names.add(_last_name(receiver) if receiver is not None else "")
        elif node.type == "identifier" and _text(node) in _DYNAMIC_FUNCTIONS:
            # `enumValueOf<Flags>("X")` parses as a comparison wherever the
            # grammar cannot tell it from one, so the type argument is read
            # off the text after the name, whatever node holds it.
            match = _TYPE_ARGUMENT.match(raw, node.end_byte)
            if match is not None:
                names.add(match.group(1).decode("utf-8"))
        owners = names | {aliases[n] for n in names if n in aliases}
        for decl in (d for owner in sorted(owners) for d in by_owner.get(owner, ())):
            notes.append(xk.refusal(path, node, xk.ENUM_DYNAMIC, source, decl.entry))
    unique: dict[tuple[int, str], xk.Refusal] = {}
    for note in notes:
        unique.setdefault((note.line, note.entry), note)
    return [unique[k] for k in sorted(unique)]


xk.register(
    "kt",
    xk.Profile(
        language_family=frozenset({"kt"}),
        declarations=declarations,
        references=references,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        reaches=frozenset({"java", "groovy", "scala"}),
        entry_findings=entry_findings,
        enum_dynamic_uses=enum_dynamic_uses,
    ),
)
