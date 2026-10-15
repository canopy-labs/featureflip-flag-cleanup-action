"""Swift's profile for :mod:`flag_cleanup.cross_file_keys`.

Swift has no import of a file's own module: every top-level name and every
type a module declares is visible in every file of it, and no file says which
module it is in. So the unit of resolution is the MODULE, read from the build
files (:mod:`~flag_cleanup.swift_modules`). A file no build file places is in
an unknown module, and nothing in it is offered or proven.

A read is proven by asking, in every module the reader is compiled into, what
the name it spells means there:

1. a binding in the file itself, decided by ELIMINATION as in the Dart
   profile: every occurrence of a relied identifier must sit in a position
   known to look nothing up;
2. a member of an enclosing type, of any extension of it, of a supertype the
   checkout declares, or of an extension of a protocol it conforms to. A
   ``where Self: X`` (or ``Self == X``) clause on an extension or a protocol
   names a supertype too, and so, conservatively, does one on a method. A supertype the checkout does not declare
   cannot be inspected: a bare read inside such a type is refused, and a
   qualified one accepted, as Java accepts it;
3. a top-level name of the module itself, which wins over every import;
4. an imported module, which must be the declaring one, imported plainly, at
   an access level that allows the read; a second imported checkout module
   that declares the name or re-exports another is refused, since the
   compiler can prefer it without a diagnostic.

Names are compared without the backticks a declaration or a read may wrap
them in (:func:`_ident`); a read or a declaration spelled with them is never
proven or offered, since the rules compare the text as written.

A metatype held in a variable (``let k = type(of: holder)``) is not followed.
Such a read is missed, never folded, and a missed read of a deleted
declaration is a build error, never a different value.

Enum cases are a second kind of declaration
(:data:`~flag_cleanup.cross_file_keys.ENUM_ENTRY`): a case of an offerable
enum holding the key, read as ``Owner.X.rawValue`` (a ``String`` raw value)
or ``Owner.X.p`` (a computed ``String`` property whose getter is one
``switch self``). A case is never deleted. ``rawValue`` is not a language
fact: a member of that name in the enum's body, in an extension of it or of
any protocol, or in a protocol it conforms to wins over the synthesized one,
so the proof rules each out (:func:`_raw_value_proven`). A switch property's own declaration cannot be
overridden by a protocol or by an extension in its module; one in the reading module wins there, which
the reader proof checks.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const, source_index, swift_modules, syntax

#: ``KeyDeclaration.module`` for every Swift declaration: a qualified name is
#: checkout-wide, so the same ``Owner.NAME`` in two modules competes.
_CHECKOUT = ""

_OWNER_KINDS = frozenset({"struct", "class", "enum", "actor"})
_TYPE_KEYWORDS = _OWNER_KINDS | {"extension"}
_BODIES = frozenset({"class_body", "enum_class_body", "protocol_body"})
_RANK = {"private": 0, "fileprivate": 1, "internal": 2, "package": 3, "public": 4, "open": 4}
_IMPORT_KINDS = frozenset({"struct", "class", "enum", "protocol", "typealias", "func", "let", "var"})

#: The member a ``String`` raw-value enum's cases are read through.
_RAW_VALUE = "rawValue"
#: How a raw type, and a computed accessor's type, must be written.
_STRING = frozenset({"String", "Swift.String"})
#: Conformances a raw-value enum may carry, bare or ``Swift.``-qualified: none
#: declares a ``rawValue`` of its own. Any other must be a protocol the
#: checkout declares, because an extension of an outside protocol, in a
#: library this run cannot read, can declare one that wins over the
#: synthesized ``rawValue``.
_ALLOWED_CONFORMANCES = frozenset({
    "Equatable", "Hashable", "Comparable", "CaseIterable", "Codable", "Encodable",
    "Decodable", "CodingKey", "Sendable", "Identifiable", "CustomStringConvertible",
    "CustomDebugStringConvertible",
})
#: Protocols an attributed extension of reaches a raw-value enum whatever its
#: clause says: those it conforms to implicitly, every one it may conform to
#: (and the protocols they inherit), kept whole rather than followed through
#: the standard library's inheritance.
_REACHED = _ALLOWED_CONFORMANCES | {"RawRepresentable", "Encodable", "Decodable"}
#: Member attributes that cannot be an attached macro. Measured on swiftc 6.0
#: against a declared macro of each name: the compiler resolves these itself.
#: Any other attribute on a member may be a peer macro adding declarations
#: beside it, ``rawValue`` among them.
_BUILTIN_ATTRIBUTES = frozenset({
    "available", "objc", "nonobjc", "discardableResult", "inlinable", "usableFromInline", "inline",
    "frozen", "preconcurrency", "warn_unqualified_access", "_disfavoredOverload", "Sendable",
})
#: Member attributes that name a type, so a macro of the same name declared
#: in the checkout wins over them (measured); built in only when none is.
_TYPE_ATTRIBUTES = frozenset({"MainActor"})
_COMMENTS = frozenset({"comment", "multiline_comment"})


def parse(source: bytes) -> Node:
    """``source`` parsed with the gate's Swift grammar, errors and all."""
    return syntax._parser("swift").parse(source).root_node


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _ident(node: Node | None) -> str:
    """The name an identifier spells. Backticks let a keyword be a name, and
    around any other name they change nothing, but the grammar keeps them in
    the node's text. Every NAME this profile reads goes through here, or a
    backticked binding of the constant's name is invisible to the proof."""
    text = _text(node)
    if len(text) > 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1]
    return text


def _code(node: Node) -> list[Node]:
    """``node``'s named children, comments left out."""
    return [c for c in node.named_children if c.type not in _COMMENTS]


def _kind(node: Node) -> str | None:
    """``struct``/``class``/``enum``/``actor``/``extension``/``protocol`` for
    a type declaration, None for any other node."""
    if node.type == "protocol_declaration":
        return "protocol"
    if node.type != "class_declaration":
        return None
    return next((c.type for c in node.children if c.type in _TYPE_KEYWORDS), None)


def _type_name(node: Node) -> str | None:
    """What a type declares, dotted for an extension of a nested type
    (``extension Flags.Checkout``); None for a generic or computed one."""
    name = node.child_by_field_name("name")
    if name is None:
        return None
    if name.type == "type_identifier":
        return _ident(name)
    if name.type == "user_type" and name.named_children and all(
        c.type == "type_identifier" for c in name.named_children
    ):
        return ".".join(_ident(c) for c in name.named_children)
    return None


def _holders(node: Node) -> list[Node] | None:
    """The type declarations whose bodies hold ``node``, outermost first;
    None when anything else (a function, a closure, a property) does."""
    holders: list[Node] = []
    current = node
    while current.parent is not None and current.parent.type != "source_file":
        body = current.parent
        holder = body.parent
        if body.type not in _BODIES or holder is None or _kind(holder) is None:
            return None
        holders.append(holder)
        current = holder
    holders.reverse()
    return holders


def _modifier_nodes(node: Node) -> list[Node]:
    modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
    return [] if modifiers is None else list(modifiers.named_children)


def _written_access(node: Node) -> str | None:
    for modifier in _modifier_nodes(node):
        if modifier.type == "visibility_modifier" and "(" not in _text(modifier):
            return _text(modifier).strip()  # `private(set)` is the setter's
    return None


def _is_static(node: Node) -> bool:
    return any(m.type == "property_modifier" and _text(m) == "static" for m in _modifier_nodes(node))


def _in_conditional(node: Node) -> bool:
    """Whether ``node`` sits between ``#if`` and ``#endif`` in any body that
    encloses it. The grammar keeps compiler-control directives as flat
    ``directive`` siblings, not as wrappers, so this counts the ``#if``s open
    before each ancestor among its siblings."""
    current = node
    while current.parent is not None:
        depth = 0
        for sibling in current.parent.children:
            if sibling.id == current.id:
                break
            if sibling.type == "directive":
                # By prefix: the grammar keeps `#if!DEBUG` and `#if(DEBUG)` as one word.
                text = _text(sibling).lstrip()
                if text.startswith("#endif"):
                    depth -= 1
                elif text.startswith("#if"):
                    depth += 1
        if depth > 0:
            return True
        current = current.parent
    return False


def _bound(node: Node) -> list[str]:
    """The names a property declaration, protocol property or enum case binds."""
    if node.type == "enum_entry":
        return [_ident(c) for c in node.named_children if c.type == "simple_identifier"]
    found: list[str] = []
    for pattern in (c for c in node.named_children if c.type == "pattern"):
        found.extend(_ident(n) for n in xk.walk(pattern) if n.type == "simple_identifier")
    return found


@dataclass(frozen=True)
class _Placement:
    owner: str | None
    #: The written or default access, capped by every enclosing type's. An
    #: extension member is capped by its type's as well (:meth:`_World.access`).
    access: str
    #: For an extension member, the extended type, which must be declared in
    #: the module.
    extended: str | None


def _placement(declaration: Node) -> _Placement | None:
    """Where a ``let`` sits, when another file can read it: top level, or
    ``static`` directly in a struct, class, enum, actor or extension body,
    nested types giving a dotted owner. None for anything else, for one with
    an attribute or inside ``#if``, and for one whose access (its own or an
    enclosing type's) is private or fileprivate."""
    if any(m.type == "attribute" for m in _modifier_nodes(declaration)):
        return None
    if _in_conditional(declaration):
        return None
    holders = _holders(declaration)
    if holders is None or _is_static(declaration) != bool(holders):
        return None
    kinds = [_kind(h) for h in holders]
    if any(k not in _TYPE_KEYWORDS for k in kinds) or "extension" in kinds[1:]:
        return None
    names = [_type_name(h) for h in holders]
    if any(n is None for n in names):
        return None
    extension = holders[0] if holders and kinds[0] == "extension" else None
    default = (None if extension is None else _written_access(extension)) or "internal"
    levels = [_written_access(declaration) or default]
    for holder, kind in zip(holders, kinds):
        written = _written_access(holder)
        if written is not None:
            levels.append(written)
        elif kind != "extension":
            levels.append("internal")
    if any(level not in _RANK for level in levels) or min(_RANK[l] for l in levels) < _RANK["internal"]:
        return None
    return _Placement(
        ".".join(names) if holders else None,  # type: ignore[arg-type]
        min(levels, key=_RANK.__getitem__),
        names[0] if extension is not None else None,
    )


def _entry_placement(enum: Node) -> _Placement | None:
    """Where an enum sits, when another file can read its cases: as
    :func:`_placement` places a ``static let``, with the enum as the innermost
    owner. None besides for an attribute on the enum or on any type around it
    (an attached macro may add members) and for a backticked name in the
    chain."""
    holders = _holders(enum)
    if holders is None or _in_conditional(enum):
        return None
    chain = [*holders, enum]
    if any(m.type == "attribute" for holder in chain for m in _modifier_nodes(holder)):
        return None
    kinds = [_kind(h) for h in chain]
    if any(k not in _TYPE_KEYWORDS for k in kinds) or "extension" in kinds[1:]:
        return None
    names = [_type_name(h) for h in chain]
    if any(n is None for n in names) or any("`" in _text(h.child_by_field_name("name")) for h in chain):
        return None
    outer = kinds[:-1]
    extension = holders[0] if outer and outer[0] == "extension" else None
    default = (None if extension is None else _written_access(extension)) or "internal"
    levels = [_written_access(enum) or default]
    for holder, kind in zip(holders, outer):
        written = _written_access(holder)
        if written is not None:
            levels.append(written)
        elif kind != "extension":
            levels.append("internal")
    if any(level not in _RANK for level in levels) or min(_RANK[l] for l in levels) < _RANK["internal"]:
        return None
    return _Placement(
        ".".join(names),  # type: ignore[arg-type]
        min(levels, key=_RANK.__getitem__),
        names[0] if extension is not None else None,
    )


def _owner_text(enum: Node) -> str:
    """The enum's dotted name from its file's top level, for a report."""
    return ".".join([*(_type_name(h) or "?" for h in (_holders(enum) or [])), _type_name(enum) or "?"])


def _enum_of(node: Node) -> Node | None:
    """The enum whose body declares ``node`` directly."""
    body = node.parent
    enum = None if body is None or body.type != "enum_class_body" else body.parent
    return enum if enum is not None and _kind(enum) == "enum" else None


def _member_placement(prop: Node) -> _Placement | None:
    """An instance property declared in an enum's body, placed as its enum
    and read at the narrower of the two accesses."""
    enum = _enum_of(prop)
    if enum is None or _is_static(prop):
        return None
    placed = _entry_placement(enum)
    written = _written_access(prop) or "internal"
    if placed is None or written not in _RANK:
        return None
    return _Placement(placed.owner, min([placed.access, written], key=_RANK.__getitem__), placed.extended)


_PLACED: dict[str, tuple[tuple[int, int], dict[tuple[str | None, str], _Placement]]] = {}


def _narrower(found: dict[tuple[str | None, str], _Placement], key: tuple[str | None, str], placed: _Placement) -> None:
    """Record ``placed`` under ``key``, at the narrowest access of every
    member placed there. A static and an instance member share the key, and
    which of them a read finds is not this record's to decide."""
    seen = found.get(key)
    if seen is None:
        found[key] = placed
        return
    found[key] = _Placement(
        placed.owner,
        min([seen.access, placed.access], key=_RANK.__getitem__),
        seen.extended or placed.extended,
    )


def _placements_of(path: str) -> dict[tuple[str | None, str], _Placement]:
    """Every offerable ``let`` and enum case of the file on disk, by
    ``(owner, name)``, each at the narrowest access of its name's members."""
    try:
        info = Path(path).stat()
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    stamp = (info.st_mtime_ns, info.st_size)
    cached = _PLACED.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    found: dict[tuple[str | None, str], _Placement] = {}
    program = parse(text.encode("utf-8"))
    if not program.has_error:
        for node in xk.walk(program):
            if node.type == "enum_entry":
                # A case: placed as its enum, read at the enum's access.
                enum = _enum_of(node)
                names = _bound(node)
                placed = _entry_placement(enum) if enum is not None and len(names) == 1 else None
                if placed is not None:
                    _narrower(found, (placed.owner, names[0]), placed)
                continue
            if node.type != "property_declaration":
                continue
            names = _bound(node)
            placed = (_placement(node) or _member_placement(node)) if len(names) == 1 else None
            if placed is not None:
                _narrower(found, (placed.owner, names[0]), placed)
    _PLACED[path] = (stamp, found)
    return found


# --- the checkout's Swift index ------------------------------------------------


@dataclass(frozen=True)
class _Type:
    #: Dotted from the file's top level (``Outer.Inner``); an extension's as written.
    name: str
    kind: str
    #: The last name of each inheritance-clause type; "" for one that is not a plain name.
    supers: tuple[str, ...]
    members: frozenset[str]
    access: str | None
    #: Declared inside a function or other non-type scope: indexed for scope
    #: lookups, but never a type another file can extend.
    local: bool = False
    #: Each of ``supers`` as written, dotted names whole (``Swift.Codable``);
    #: "" for one that is not a plain name. What a conformance allow-list reads.
    clause: tuple[str, ...] = ()
    #: Carries an attribute (an attached macro may add members to what it extends).
    attributed: bool = False
    #: The attributes on its own members (:func:`_member_attributes`).
    member_attributes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Share:
    #: Every top-level declared name, repeats kept.
    top: tuple[str, ...]
    types: tuple[_Type, ...]
    aliases: frozenset[str]
    #: Modules this file re-exports with ``@_exported import``.
    exported: tuple[str, ...]
    complete: bool
    #: Every top-level macro name, which ``top`` leaves out.
    macros: frozenset[str] = frozenset()


def _last_name(target: Node | None) -> str:
    """The last name of a written type; "" for one that is not a plain name
    (a composition ``A & B``, an optional, a function type)."""
    if target is None or target.type != "user_type":
        return ""
    names = [c for c in target.named_children if c.type == "type_identifier"]
    return _ident(names[-1]) if names else ""


def _written(target: Node | None) -> str:
    """A written type as dotted names (``Swift.String``); "" for one that is
    not a plain name (generic arguments, a composition, an optional)."""
    if target is None or target.type != "user_type" or not target.named_children:
        return ""
    if any(c.type != "type_identifier" for c in target.named_children):
        return ""
    return ".".join(_ident(c) for c in target.named_children)


def _self_constraints(node: Node, read=_last_name) -> list[str]:
    """The types a ``where Self: X`` or ``where Self == X`` clause of ``node``
    names, each as ``read`` reads it. Inside an extension or a protocol
    carrying one, or a method of either, X's members are in unqualified scope
    exactly as a supertype's are."""
    found: list[str] = []
    for clause in (c for c in node.named_children if c.type == "type_constraints"):
        for constraint in (c for c in clause.named_children if c.type == "type_constraint"):
            for inner in constraint.named_children:
                if inner.type not in ("inheritance_constraint", "equality_constraint"):
                    continue
                if _ident(inner.child_by_field_name("constrained_type")) == "Self":
                    found.append(read(inner.child_by_field_name("name")))
    return found


def _supers(node: Node, read=_last_name) -> list[str]:
    """Every supertype a type declaration names: its inheritance clause and
    its ``where Self`` constraints, each as ``read`` reads it."""
    return [
        *(read(c.child_by_field_name("inherits_from")) for c in node.named_children if c.type == "inheritance_specifier"),
        *_self_constraints(node, read),
    ]


def _names_declared(node: Node) -> list[str]:
    """The names a top-level declaration introduces into its module."""
    kind = _kind(node)
    if kind is not None:
        name = _type_name(node)
        return [] if kind == "extension" or name is None else [name]
    if node.type in ("typealias_declaration", "function_declaration"):
        name = node.child_by_field_name("name")
        return [] if name is None else [_ident(name)]
    if node.type == "property_declaration":
        return _bound(node)
    # Not a macro: measured, one in the reader's module hides no imported
    # type or constant of its name (:func:`_macro_name` keeps it apart).
    return []


def _macro_name(node: Node) -> str | None:
    """The name a top-level macro declaration introduces. The grammar gives
    it no field: it is the identifier after ``macro``."""
    if node.type != "macro_declaration":
        return None
    name = next((c for c in node.children if c.type == "simple_identifier"), None)
    return None if name is None else _ident(name)


def _member_names(node: Node) -> list[str]:
    if node.type in ("property_declaration", "protocol_property_declaration", "enum_entry"):
        return _bound(node)
    if node.type in (
        "function_declaration", "protocol_function_declaration",
        "typealias_declaration", "associatedtype_declaration",
    ):
        name = node.child_by_field_name("name")
        return [] if name is None else [_ident(name)]
    kind = _kind(node)
    if kind is not None and kind != "extension":
        name = _type_name(node)
        return [] if name is None else [name]
    return []


def _member_attributes(body: Node | None) -> frozenset[str]:
    """The name of every attribute on a declaration directly in ``body``
    (``inline`` for ``@inline(__always)``): a case, a property, a function, a
    nested type."""
    return frozenset(
        _text(a).lstrip("@").split("(")[0].strip()
        for child in ([] if body is None else body.named_children)
        for a in _modifier_nodes(child)
        if a.type == "attribute"
    )


def _param_name(node: Node) -> Node | None:
    """The name a generic parameter introduces; a parameter pack wraps it."""
    if node.type == "type_identifier":
        return node
    return next((n for n in xk.walk(node) if n.type == "type_identifier"), None)


def _type_entry(node: Node, kind: str) -> _Type | None:
    own = _type_name(node)
    if own is None:
        return None
    found_holders = _holders(node)
    holders = found_holders or []
    name = ".".join([*(_type_name(h) or "?" for h in holders), own])
    body = node.child_by_field_name("body")
    members = frozenset(
        m for child in ([] if body is None else body.named_children) for m in _member_names(child)
    ) | frozenset(  # generic parameters shadow exactly as members do
        _ident(ident)
        for c in node.named_children if c.type == "type_parameters"
        for p in c.named_children if p.type == "type_parameter" and p.named_children
        for ident in [_param_name(p.named_children[0])] if ident is not None
    )
    return _Type(
        name, kind, tuple(_supers(node)), members, _written_access(node), found_holders is None,
        tuple(_supers(node, _written)),
        any(m.type == "attribute" for m in _modifier_nodes(node)),
        _member_attributes(body),
    )


@dataclass(frozen=True)
class _Import:
    segments: tuple[str, ...]
    testable: bool
    exported: bool
    #: Not a plain top-level ``import M``: inside ``#if``, or a scoped
    #: ``import struct M.X`` or a submodule ``import M.Sub``.
    unsure: bool


def _import(node: Node) -> _Import | None:
    if node.type != "import_declaration":
        return None
    identifier = next((c for c in node.named_children if c.type == "identifier"), None)
    segments = () if identifier is None else tuple(
        _ident(c) for c in identifier.named_children if c.type == "simple_identifier"
    )
    if not segments:
        return None
    attributes = {
        _text(a).lstrip("@").split("(")[0].strip() for a in _modifier_nodes(node) if a.type == "attribute"
    }
    scoped = len(segments) > 1 or any(not c.is_named and c.type in _IMPORT_KINDS for c in node.children)
    nested = node.parent is None or node.parent.type != "source_file"
    return _Import(segments, "testable" in attributes, "_exported" in attributes,
                   scoped or nested or _in_conditional(node))


def _share(text: str) -> tuple[_Share, bool]:
    program = parse(text.encode("utf-8"))
    top: list[str] = []
    exported: list[str] = []
    macros: set[str] = set()
    for child in program.named_children:
        top.extend(_names_declared(child))
        macro = _macro_name(child)
        if macro is not None:
            macros.add(macro)
        imported = _import(child)
        if imported is not None and imported.exported:
            exported.append(imported.segments[0])
    types: list[_Type] = []
    aliases: set[str] = set()
    for node in xk.walk(program):
        kind = _kind(node)
        if kind is not None:
            entry = _type_entry(node, kind)
            if entry is not None:
                types.append(entry)
        elif node.type == "typealias_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                aliases.add(_ident(name))
    share = _Share(
        tuple(top), tuple(types), frozenset(aliases), tuple(exported), not program.has_error, frozenset(macros)
    )
    return share, share.complete


def _combine(shares, _incomplete: bool, unreadable: bool):
    """Every file's share by path; None when a file could not be read, since
    it might declare anything anywhere."""
    return None if unreadable else tuple(shares)


_INDEX = xk.CheckoutIndex("Swift", (".swift",), _share, _combine)


class _World:
    """The checkout's Swift declarations, seen through its module layout."""

    def __init__(self, layout: swift_modules.Layout, shares) -> None:
        self.layout = layout
        self._files: dict[swift_modules.Module, list[_Share]] = {}
        self.by_last: dict[str, list[_Type]] = {}
        self.by_full: dict[str, list[tuple[str, _Type]]] = {}
        self.aliases: set[str] = set()
        #: Every macro the checkout declares, in any module.
        self.macros: set[str] = set()
        every: list[_Type] = []
        for path, share in shares:
            for module in layout.modules_of(path) or ():
                self._files.setdefault(module, []).append(share)
            self.aliases |= share.aliases
            self.macros |= share.macros
            every.extend(share.types)
            for entry in share.types:
                self.by_last.setdefault(entry.name.rsplit(".", 1)[-1], []).append(entry)
                self.by_full.setdefault(entry.name, []).append((path, entry))
        #: Every entry that spells a typealias ANYWHERE in its dotted name
        #: (``extension NS.Keys``, ``extension Outer.J.Keys``). ``by_last``
        #: files ``extension NS.Keys`` under ``Keys``, so a lookup of the
        #: alias's own name never finds it, and the alias may name any type.
        self.through_alias: tuple[_Type, ...] = tuple(
            entry for entry in every
            if not entry.local and any(seg in self.aliases for seg in entry.name.split("."))
        )
        self._top: dict[swift_modules.Module, Counter] = {}

    def broken(self, module) -> bool:
        return any(not share.complete for share in self._files.get(module, ()))

    def top_count(self, module, name: str) -> int:
        if module not in self._top:
            self._top[module] = Counter(n for share in self._files.get(module, ()) for n in share.top)
        return self._top[module][name]

    def exported(self, module) -> set[str]:
        return {n for share in self._files.get(module, ()) for n in share.exported}

    def aliases_in(self, module) -> set[str]:
        """Every typealias name ``module`` declares, members' included."""
        return {n for share in self._files.get(module, ()) for n in share.aliases}

    def types_in(self, module, full: str) -> list[_Type]:
        return [entry for path, entry in self.by_full.get(full, ()) if module in (self.layout.modules_of(path) or ()) and not entry.local]

    def offerable(self, path: str, placed: _Placement, name: str, *, for_enum: bool = False) -> bool:
        """Whether a constant ``name`` placed so in ``path`` can be offered at
        all. For an enum's case (``for_enum``), the enum's own conformances
        are judged by the shape that proves the case, not here; the types
        around it are judged as a constant's owners are."""
        modules = self.layout.modules_of(path)
        if modules is None or any(self.broken(m) for m in modules):
            return False
        if placed.owner is None:
            return True
        parts = placed.owner.split(".")
        for part in parts[:-1] if for_enum else parts:
            # By last name, whatever the spelling: `extension App.Keys: P {}` adds a conformance too.
            if any(entry.supers for entry in self.by_last.get(part, ())):
                return False  # a witness, a superclass's static, KVC: reads that never spell the owner
        for entry in self.through_alias:
            segments = entry.name.split(".")
            last = segments[-1]
            if entry.kind != "extension" and (
                last not in parts or not self.aliases.intersection(segments[:-1])
            ):
                # A type declared in its own right counts only when it is
                # nested in an aliased extension and named like an owner
                # segment (`extension NS { enum Keys { ... } }`); the owner's
                # own declaration is not one, even when some typealias
                # elsewhere happens to share its name.
                continue
            if entry.kind == "extension" and entry.supers:
                return False  # a conformance added through a typealias
            if name in entry.members:
                # A member added through a typealias of the owner (or of a
                # type around it) competes under the alias's spelling, not
                # the owner's, and in a module that imports the owner it wins
                # over the declaration.
                return False
            if any(last == parts[i] and parts[i + 1] in entry.members for i in range(len(parts) - 1)):
                return False  # the next owner segment redeclared through an alias
        if placed.extended is not None:
            return all(
                any(entry.kind in _OWNER_KINDS for entry in self.types_in(m, placed.extended))
                and all(cap in _RANK and _RANK[cap] >= _RANK["internal"] for cap in self._caps(m, placed.extended))
                for m in modules
            )
        return True


    def custom_attributed(self, names: frozenset[str]) -> bool:
        """Whether any of the attribute ``names`` on a member may be an
        attached macro: one not in :data:`_BUILTIN_ATTRIBUTES`, or one in
        :data:`_TYPE_ATTRIBUTES` the checkout declares a macro of."""
        return any(
            name not in _BUILTIN_ATTRIBUTES and (name not in _TYPE_ATTRIBUTES or name in self.macros)
            for name in names
        )

    def macro_extended(self, enum: Node) -> bool:
        """Whether an attached macro could add a member to ``enum``,
        ``rawValue`` among them: on a member of its body (a peer macro adds
        declarations beside the one it is attached to), or on an extension
        that reaches it or on a member of one (:meth:`custom_attributed`).

        A checkout enum gains members only through itself, a typealias of
        it, or a protocol it conforms to, so an extension counts when its
        extended name (any dotted spelling) ends in the enum's own name,
        names a typealias or a protocol the checkout declares, is one of
        :data:`_REACHED` (bare or ``Swift.``), or is a type the enum's clause
        or an extension of it names (bare or ``Swift.``), the raw type
        included: ``@M extension String`` reaches a ``String`` enum. An
        attributed extension of any other name (``View``, ``Array``) cannot."""
        if self.custom_attributed(_member_attributes(enum.child_by_field_name("body"))):
            return True
        own = _type_name(enum) or ""
        extensions = [e for e in self.by_last.get(own, ()) if e.kind == "extension"]
        clause = [
            _written(c.child_by_field_name("inherits_from"))
            for c in enum.named_children
            if c.type == "inheritance_specifier"
        ]
        conforms = {c.removeprefix("Swift.") for c in (*clause, *(c for e in extensions for c in e.clause)) if c}
        for entries in self.by_last.values():
            for entry in entries:
                if entry.kind != "extension" or not (
                    entry.attributed or self.custom_attributed(entry.member_attributes)
                ):
                    continue
                last = entry.name.rsplit(".", 1)[-1]
                bare = entry.name.removeprefix("Swift.")
                if (
                    last == own
                    or last in self.aliases
                    or any(t.kind == "protocol" for t in self.by_last.get(last, ()))
                    or bare in _REACHED
                    or bare in conforms
                ):
                    return True
        return False

    def shadows(self, name: str) -> bool:
        """Whether the checkout declares a type or typealias named like a
        standard one (a ``struct String``): the standard type is then not
        what the name means."""
        return name in self.aliases or any(entry.kind != "extension" for entry in self.by_last.get(name, ()))

    def extends_any_with(self, member: str) -> bool:
        """Whether any extension in the checkout, of any name or spelling,
        declares ``member``. Matching what an extension extends (a typealias,
        ``Swift.RawRepresentable``, a protocol named like a nested type) is
        where proofs went wrong, and an extension adding ``rawValue`` to an
        unrelated type is rare enough that refusing on it costs little."""
        return any(
            entry.kind == "extension" and member in entry.members
            for entries in self.by_last.values()
            for entry in entries
        )

    def extends_with(self, module, owner: str, member: str) -> bool:
        """Whether ``module`` holds an extension that may extend ``owner`` (by
        its last name, or spelled through a typealias) declaring ``member``.
        Measured: such an extension in the reading module wins there over the
        enum's own member, silently."""
        last = owner.rsplit(".", 1)[-1]
        return any(
            entry.kind == "extension"
            and member in entry.members
            and (
                entry.name.rsplit(".", 1)[-1] == last
                or any(segment in self.aliases for segment in entry.name.split("."))
            )
            for share in self._files.get(module, ())
            for entry in share.types
        )

    def _caps(self, module, extended: str) -> list[str]:
        """The written access of the extended type and of each type enclosing it."""
        parts = extended.split(".")
        return [
            entry.access or "internal"
            for end in range(1, len(parts) + 1)
            for entry in self.types_in(module, ".".join(parts[:end]))
            if entry.kind in _OWNER_KINDS
        ]

    def access(self, decl: xk.KeyDeclaration) -> str | None:
        """The declaration's effective access level; None when unreadable.

        A case read through a computed property is read at that property's
        access too: where it is not visible, the read finds whatever else of
        that name the reader sees."""
        placements = _placements_of(decl.path)
        placed = placements.get((decl.owner, decl.name))
        if placed is None:
            return None
        levels = [placed.access]
        for accessor in sorted(decl.accessors - {_RAW_VALUE}):
            member = placements.get((decl.owner, accessor))
            if member is None:
                return None
            levels.append(member.access)
        if placed.extended is not None:
            modules = self.layout.modules_of(decl.path) or frozenset()
            caps = [cap for m in modules for cap in self._caps(m, placed.extended)]
            if not caps or any(cap not in _RANK for cap in caps):
                return None
            levels += caps
        return min(levels, key=_RANK.__getitem__)

    def type_scope(self, name: str, head: str, seen: set[str] | None = None) -> str | None:
        """``member`` when a checkout type named ``name`` (its extensions,
        checkout supertypes and their protocols' extensions) has a member
        ``head``; ``outside`` when part of what it inherits cannot be seen;
        None when neither."""
        seen = set() if seen is None else seen
        if name in seen:
            return None
        seen.add(name)
        found = self.by_last.get(name, [])
        if any(head in entry.members for entry in found):
            return "member"
        verdict = None if any(entry.kind != "extension" for entry in found) else "outside"
        for entry in found:
            for parent in entry.supers:
                if parent in self.aliases:
                    inner = "alias"  # an alias may name any type
                else:
                    inner = self.type_scope(parent, head, seen) if parent else "outside"
                if inner == "member":
                    return "member"
                if inner == "alias" or (inner == "outside" and verdict is None):
                    verdict = inner if verdict != "alias" else verdict
        return verdict


_WORLDS: dict[str, tuple[object, object, _World]] = {}


def _world(root: str) -> _World | None:
    layout = swift_modules.layout(root)
    shares = _INDEX(root)
    if shares is None:
        return None
    cached = _WORLDS.get(root)
    if cached is not None and cached[0] is layout and cached[1] is shares:
        return cached[2]
    world = _World(layout, shares)
    _WORLDS[root] = (layout, shares, world)
    return world


def _root_of(path: str) -> str:
    return str(Path(source_index.checkout_root(path)).resolve())


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    """The constants in ``path`` holding ``flag_key`` that another file can read.
    And the enum cases holding it that are proven (see :func:`_enum_entries`).

    What the in-file rules delete, narrowed to what another file can reach and
    this profile can follow: see :func:`_placement` and
    :meth:`_World.offerable`. Nothing is offered from a file whose parse has
    errors or whose module is unknown.
    """
    data = source.encode("utf-8")
    program = parse(data)
    if program.has_error:
        return []
    world = _world(_root_of(path))
    if world is None:
        return []
    found: list[xk.KeyDeclaration] = []
    for const in key_const.key_consts(program, data, "swift", flag_key):
        if any(_text(n) != _ident(n) for n in const.name_nodes):
            continue  # backticked: the in-file rules compare the name as written
        placed = _placement(const.declaration)
        if placed is None or not world.offerable(path, placed, const.name):
            continue
        found.append(xk.KeyDeclaration(path, const.name, placed.owner, _CHECKOUT))
    entries, _refused = _enum_entries(path, source, program, flag_key, world)
    return found + entries


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every property and enum case ``source`` declares at top level or in a
    type, whatever its value, keyed checkout-wide."""
    program = parse(source.encode("utf-8"))
    if program.has_error:
        raise xk.DeclarationsUnreadable(path)
    found: set[xk.QualifiedName] = set()
    for node in xk.walk(program):
        if node.type not in ("property_declaration", "protocol_property_declaration", "enum_entry"):
            continue
        holders = _holders(node)
        if holders is None:
            continue  # a local
        names = [_type_name(h) for h in holders]
        owner = None if not holders else ".".join(n or "?" for n in names)
        keyed = [owner]
        if owner is not None and holders and _kind(holders[0]) == "extension":
            parts = owner.split(".")  # an extension may spell its type with a module prefix
            keyed += [".".join(parts[i:]) for i in range(1, len(parts))]
        for name in _bound(node):
            found.update((_CHECKOUT, o, name) for o in keyed)
    return found


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """Empty: two unrelated types of one name in two modules do not compete;
    the lookup tells them apart per module. A file that does not parse still
    fails closed."""
    if parse(source.encode("utf-8")).has_error:
        raise xk.DeclarationsUnreadable(path)
    return set()


@dataclass(frozen=True)
class _Case:
    name: str
    #: The name as written, backticks included.
    node: Node
    #: The ``enum_entry`` clause declaring it.
    entry: Node
    #: Its raw value; None when it states none.
    value: Node | None
    #: Its clause declares only it, with no associated values.
    alone: bool


def _cases(body: Node) -> list[_Case]:
    """Every case an enum's body declares, each name of a clause paired with
    its own raw value."""
    found: list[_Case] = []
    for entry in (c for c in body.named_children if c.type == "enum_entry"):
        pairs: list[list] = []
        for index, child in enumerate(entry.children):
            field = entry.field_name_for_child(index)
            if field == "name":
                pairs.append([child, None])
            elif field == "raw_value" and pairs:
                pairs[-1][1] = child
        alone = len(pairs) == 1 and entry.child_by_field_name("data_contents") is None
        found.extend(_Case(_ident(name), name, entry, value, alone) for name, value in pairs)
    return found


def _plain_literal(node: Node | None) -> str | None:
    """The content of a single-line string literal with no interpolation and
    no escape; None for anything else, a multi-line or raw literal included."""
    if node is None or node.type != "line_string_literal":
        return None
    parts = node.named_children
    if not parts:
        return ""
    if len(parts) != 1 or parts[0].type != "line_str_text":
        return None
    return _text(parts[0])


def _conformance_allowed(world: _World, written: str, seen: set[str]) -> bool:
    """Whether a conformance, as written, cannot hand a raw-value enum a
    ``rawValue``: a ``Swift.``-qualified or undeclared name on
    :data:`_ALLOWED_CONFORMANCES`, or a protocol the checkout declares with no
    member of that name whose own conformances pass the same test. Any other
    name is an outside protocol (or a typealias), whose extensions in a
    library this run cannot see."""
    if written.startswith("Swift.") and written.removeprefix("Swift.") in _ALLOWED_CONFORMANCES:
        return True
    if not written or "." in written or written in world.aliases:
        return False
    declared = world.by_last.get(written, [])
    if all(entry.kind == "extension" for entry in declared):
        # Not declared here; its extensions in the checkout are judged with
        # every other one (:meth:`_World.extends_any_with`).
        return written in _ALLOWED_CONFORMANCES
    if any(entry.kind not in ("protocol", "extension") for entry in declared):
        return False
    if written in seen:
        return True
    seen.add(written)
    return all(
        _RAW_VALUE not in entry.members and all(_conformance_allowed(world, c, seen) for c in entry.clause)
        for entry in declared
    )


def _raw_value_proven(world: _World, enum: Node, value: Node | None, flag_key: str) -> bool:
    """Whether ``Owner.X.rawValue`` reads the literal ``value`` the case states.

    Measured on Swift 6.0, a member named ``rawValue`` wins over the
    synthesized one when it is declared in the enum's body, in an extension
    of the enum (in another file of its module, or in the reading module),
    in an extension of a protocol the enum conforms to (in its clause or
    added later), and in an extension of ``RawRepresentable``. So the first
    inheritance item must be ``String``, every other conformance (the enum's
    own and any an extension spelled with its name adds) must pass
    :func:`_conformance_allowed`, and the checkout may declare no such member
    in the body or in any extension at all (:meth:`_World.extends_any_with`).
    An extension in a library cannot be seen.
    """
    if _plain_literal(value) != flag_key:
        return False
    clause = [
        _written(c.child_by_field_name("inherits_from"))
        for c in enum.named_children
        if c.type == "inheritance_specifier"
    ]
    if not clause or clause[0] not in _STRING:
        return False
    extensions = [e for e in world.by_last.get(_type_name(enum) or "", ()) if e.kind == "extension"]
    conformances = [*clause[1:], *(c for e in extensions for c in e.clause)]
    if not all(_conformance_allowed(world, c, set()) for c in conformances):
        return False
    body = enum.child_by_field_name("body")
    if body is None or any(_RAW_VALUE in _member_names(c) for c in body.named_children):
        return False
    return not world.extends_any_with(_RAW_VALUE)


def _switch_arms(prop: Node) -> list[tuple[list[str], Node | None]] | None:
    """The arms of a property written ``var p: String { switch self { … } }``
    (or with the switch in a single ``get { }``): each arm's ``.name``
    patterns (none for ``default:``) and its statements. None for any other
    property: ``static``, attributed, ``private`` or ``fileprivate`` (from
    another file a protocol extension's member of the name would win),
    inside ``#if`` (a build without the condition reads that member
    instead), stored, with a setter or effects, with anything but one
    ``switch self``, or with any arm pattern but ``.name`` (a binding,
    ``where``, ``_``)."""
    binding = next((c for c in prop.named_children if c.type == "value_binding_pattern"), None)
    if binding is None or _text(binding) != "var" or _is_static(prop) or _in_conditional(prop):
        return None
    if any(m.type == "attribute" or _text(m) == "class" for m in _modifier_nodes(prop)):
        return None
    if _written_access(prop) in ("private", "fileprivate") or "`" in _text(prop.child_by_field_name("name")):
        return None
    annotation = next((c for c in prop.named_children if c.type == "type_annotation"), None)
    if annotation is None or _written(annotation.child_by_field_name("name")) not in _STRING:
        return None
    computed = prop.child_by_field_name("computed_value")
    if computed is None or prop.child_by_field_name("value") is not None:
        return None
    parts = _code(computed)
    if len(parts) == 1 and parts[0].type == "computed_getter":
        specifier = next((c for c in parts[0].named_children if c.type == "getter_specifier"), None)
        if specifier is None or _text(specifier) != "get":
            return None
        parts = [c for c in _code(parts[0]) if c.type != "getter_specifier"]
    if len(parts) != 1 or parts[0].type != "statements":
        return None
    statements = _code(parts[0])
    if len(statements) != 1 or statements[0].type != "switch_statement":
        return None
    switch = statements[0]
    subject = switch.child_by_field_name("expr")
    if subject is None or subject.type != "self_expression":
        return None
    arms: list[tuple[list[str], Node | None]] = []
    for arm in (c for c in switch.named_children if c.type == "switch_entry"):
        if any(c.type in ("modifiers", "where_keyword") for c in arm.named_children):
            return None
        code = next((c for c in arm.named_children if c.type == "statements"), None)
        if any(c.type == "default_keyword" for c in arm.named_children):
            arms.append(([], code))
            continue
        names: list[str] = []
        for pattern in (c for c in arm.named_children if c.type == "switch_pattern"):
            inner = _code(pattern)
            if len(inner) != 1 or inner[0].type != "pattern":
                return None
            tokens = [c for c in inner[0].children if c.type not in _COMMENTS]
            if [c.type for c in tokens] != [".", "simple_identifier"] or "`" in _text(tokens[1]):
                return None
            names.append(_text(tokens[1]))
        if not names:
            return None
        arms.append((names, code))
    return arms


def _arm_literal(statements: Node | None) -> str | None:
    """The content of an arm whose body is exactly ``return "lit"`` or ``"lit"``."""
    code = [] if statements is None else _code(statements)
    if len(code) != 1:
        return None
    only = code[0]
    if only.type == "control_transfer_statement":
        if not only.children or only.children[0].type != "return":
            return None
        return _plain_literal(only.child_by_field_name("result"))
    return _plain_literal(only)


def _switch_properties(body: Node, cases: list[_Case], members: Counter) -> dict[str, dict[str, str | None]]:
    """Each property of the enum's body a case may be read through, with the
    literal each case it names maps to (None when that case's arm is not a
    plain literal, or two arms name it). The first matching arm wins, and an
    arm naming a static member equal to a case matches that case, so a
    property whose arms name anything but the enum's own cases is none."""
    names = {c.name for c in cases}
    found: dict[str, dict[str, str | None]] = {}
    for prop in (c for c in body.named_children if c.type == "property_declaration"):
        bound = _bound(prop)
        arms = _switch_arms(prop) if len(bound) == 1 and members[bound[0]] == 1 else None
        if arms is None or any(n not in names for named, _statements in arms for n in named):
            continue
        seen = Counter(n for named, _statements in arms for n in named)
        found[bound[0]] = {
            n: (_arm_literal(code) if seen[n] == 1 else None) for named, code in arms for n in named
        }
    return found


def _arm_holders(program: Node, enum: Node, key: re.Pattern[str]) -> list[str]:
    """The names a switch arm holding the key spells as ``.name``, in the
    enum or in an extension spelled with its name in the same file: a case
    such an arm maps holds the key, proven or not."""
    last = _type_name(enum)
    scopes = [
        enum,
        *(n for n in xk.walk(program) if _kind(n) == "extension" and (_type_name(n) or "").rsplit(".", 1)[-1] == last),
    ]
    found: list[str] = []
    for scope in scopes:
        for arm in xk.walk(scope):
            if arm.type != "switch_entry":
                continue
            code = next((c for c in arm.named_children if c.type == "statements"), None)
            if code is None or not key.search(_text(code)):
                continue
            for pattern in (c for c in arm.named_children if c.type == "switch_pattern"):
                found.extend(
                    _ident(n)
                    for n in xk.walk(pattern)
                    if n.type == "simple_identifier" and n.prev_sibling is not None and n.prev_sibling.type == "."
                )
    return found


def _enum_entries(
    path: str, source: str, program: Node, flag_key: str, world: _World | None
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """The cases holding ``flag_key`` whose accessor provably returns it, and
    refusals about every case holding it: ``enum-unproven`` for one that
    cannot be proven, and each mention of a proven one in its own file.

    A case is proven when its enum is offerable as a constant's owner would
    be (:func:`_entry_placement`, :meth:`_World.offerable`), its clause
    declares only it with no associated values, no other member of the enum
    shares its name, no checkout type shadows ``String``, no attached macro
    could add a member to the enum (:meth:`_World.macro_extended`), and
    either its raw value is proven
    (:func:`_raw_value_proven`) or a switch property maps it to the key
    (:func:`_switch_properties`). A case holds the key through its raw value
    or through a switch arm naming it.
    """
    key = xk.key_token(flag_key)
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for enum in xk.walk(program):
        if _kind(enum) != "enum":
            continue
        body = enum.child_by_field_name("body")
        if body is None:
            continue
        cases = _cases(body)
        names = {c.name for c in cases}
        holding = [c.name for c in cases if c.value is not None and key.search(_text(c.value))]
        holding += [n for n in _arm_holders(program, enum, key) if n in names]
        if not holding:
            continue
        placed = None if program.has_error or world is None else _entry_placement(enum)
        owner = _owner_text(enum) if placed is None else placed.owner
        members = Counter(m for child in body.named_children for m in _member_names(child))
        properties = _switch_properties(body, cases, members)
        for name in dict.fromkeys(holding):
            mine = [c for c in cases if c.name == name]
            accessors: set[str] = set()
            if (
                placed is not None
                and len(mine) == 1
                and mine[0].alone
                and members[name] == 1
                and "`" not in _text(mine[0].node)
                and not _in_conditional(mine[0].entry)
                and not world.shadows("String")
                and not world.macro_extended(enum)
                and world.offerable(path, placed, name, for_enum=True)
            ):
                if _raw_value_proven(world, enum, mine[0].value, flag_key):
                    accessors.add(_RAW_VALUE)
                accessors |= {prop for prop, mapped in properties.items() if mapped.get(name) == flag_key}
            reported = f"{owner}.{name}"
            if not accessors:
                refused.append(xk.refusal(path, mine[0].entry, xk.ENUM_UNPROVEN, source, reported))
                continue
            decl = xk.KeyDeclaration(
                path, name, owner, _CHECKOUT, kind=xk.ENUM_ENTRY, accessors=frozenset(accessors)
            )
            found.append(decl)
            refused.extend(_in_file_entry_mentions(path, source, program, decl, enum))
    return found, refused


def _implicit(node: Node) -> bool:
    """Whether a name is written ``.X`` with no owner: an implicit member
    expression (a ``prefix_expression``), or a case pattern (``case .X:``,
    ``if case .X = v``, whose ``.`` is the name's previous sibling)."""
    previous = node.prev_sibling
    parent = node.parent
    if previous is not None and previous.type == "." and parent is not None and parent.type != "navigation_suffix":
        return True
    return _implicit_chain(node)


def _implicit_chain(node: Node) -> bool:
    """Whether a name is the head of ``.X.member``: it parses as a prefix
    ``.`` over a navigation chain whose innermost target is the name."""
    top = node
    while top.parent is not None and top.parent.type == "navigation_expression":
        target = top.parent.child_by_field_name("target")
        if target is None or target.id != top.id:
            return False
        top = top.parent
    parent = top.parent
    return (
        top.id != node.id
        and parent is not None
        and parent.type == "prefix_expression"
        and _text(parent.child_by_field_name("operation")) == "."
    )


def _within(node: Node, ancestor: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.id == ancestor.id:
            return True
        current = current.parent
    return False


def _in_file_entry_mentions(
    path: str, source: str, program: Node, decl: xk.KeyDeclaration, enum: Node
) -> list[xk.Refusal]:
    """Every mention of a proven case in its own file. The reader invocation
    never rewrites the declaring file, so a read here is reported, not left.

    A ``case .X`` pattern inside the enum is part of its own definition (a
    switch property's arms among them) and reads no key; one elsewhere in
    the file is ``not-a-read``. The names declarations introduce are not
    mentions."""
    found: list[xk.Refusal] = []
    for node in xk.walk(program):
        if node.type != "simple_identifier" or _ident(node) != decl.name:
            continue
        parent = node.parent
        if parent is None:
            continue
        if _implicit(node) and parent.type != "prefix_expression" and not _implicit_chain(node):
            if _within(node, enum):
                continue
            reason = xk.NOT_A_READ
        elif _is_binding(node) or (parent is not None and parent.type == "value_argument_label"):
            continue
        else:
            reason = xk.UNSUPPORTED_FORM
        refusal = xk.refusal(path, node, reason, source, decl.entry)
        if refusal not in found:
            found.append(refusal)
    return found


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """The cases holding ``flag_key`` that cannot be proven, and every
    mention of a proven one in its own file."""
    program = parse(source.encode("utf-8"))
    world = None if program.has_error else _world(_root_of(path))
    return _enum_entries(path, source, program, flag_key, world)[1]


#: What a lookup answers: the name reaches the declaration; it means
#: something else; or something that may reach it cannot be followed.
PROVEN, OTHER, UNKNOWN = "proven", "other", "unknown"

_READ_CALLEES = frozenset({"boolVariation"})
#: A member after a type name that hands the type itself on.
_METATYPE = frozenset({"self", "Self", "Type", "Protocol", "init"})
#: Parents under which a type_identifier DECLARES its name.
_TYPE_BINDERS = frozenset({
    "class_declaration", "protocol_declaration", "typealias_declaration",
    "type_parameter", "associatedtype_declaration",
})


def _is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if node.type == "type_identifier":
        return parent.type in _TYPE_BINDERS
    return key_const._swift_is_binding(node) or parent.type in ("enum_entry", "class_declaration")


def _segments(nav: Node) -> list[Node] | None:
    """The names of a dotted chain ``a.b.c``; None when its head is not a
    plain name (``self``, a call, a key path, ``type(of:)``)."""
    target = nav.child_by_field_name("target")
    suffix = nav.child_by_field_name("suffix")
    member = None if suffix is None else suffix.child_by_field_name("suffix")
    if target is None or member is None or member.type != "simple_identifier":
        return None
    if target.type == "simple_identifier":
        return [target, member]
    if target.type == "navigation_expression":
        head = _segments(target)
        return None if head is None else [*head, member]
    return None


def _is_read(value: Node, accessors: frozenset[str]) -> bool:
    """Whether ``value`` is the whole key argument of a read the seeds match:
    the first argument of ``recv.boolVariation(...)``, a bare
    ``boolVariation(...)`` or an accessor, plain or negated once; never
    optional-chained, and never behind a comment."""
    argument = value.parent
    if argument is None or argument.type != "value_argument":
        return False
    slot = argument.child_by_field_name("value")
    if slot is None or slot.id != value.id:
        return False
    arguments = argument.parent
    if arguments is None or arguments.type != "value_arguments":
        return False
    if not arguments.named_children or arguments.named_children[0].id != argument.id:
        return False
    suffix = arguments.parent
    call = suffix.parent if suffix is not None and suffix.type == "call_suffix" else None
    if call is None or call.type != "call_expression" or _text(call).startswith("!!"):
        return False
    callee = call.named_children[0]
    # The gate grammar wraps a negated callee in a prefix `!`; the engine's
    # nests it differently, and both seed rules accept both.
    if callee.type == "prefix_expression" and _text(callee.child_by_field_name("operation")) == "!":
        callee = callee.child_by_field_name("target")
    if callee is None:
        return False
    if callee.type == "navigation_expression":
        if "?" in _text(callee):
            return False
        suffix_node = callee.child_by_field_name("suffix")
        name = None if suffix_node is None else suffix_node.child_by_field_name("suffix")
    elif callee.type == "simple_identifier":
        name = callee
    else:
        return False
    return name is not None and name.type == "simple_identifier" and (
        _text(name) in _READ_CALLEES or _text(name) in accessors
    )


def _engine_owner(value: Node) -> str | None:
    """The text the reader rules compare as the owner: the target of the
    outermost member access, or None for a bare read."""
    if value.type != "navigation_expression":
        return None
    return _text(value.child_by_field_name("target"))


def _allows(access: str | None, testable: bool, module, declaring) -> bool:
    if access is None:
        return False
    if _RANK[access] >= _RANK["public"]:
        return True
    if access == "package":
        return module.package is not None and any(d.package == module.package for d in declaring)
    return access == "internal" and testable


class _Judgement:
    """One reader against one set of declarations."""

    def __init__(self, path, data, program, modules, world, accessors):
        self.path = path
        self.source = data.decode("utf-8", "replace")
        self.program = program
        self.modules = modules
        self.world = world
        self.accessors = accessors
        self.imports = tuple(i for n in xk.walk(program) if (i := _import(n)) is not None)
        self.reads: list[xk.KeyReference] = []
        self.refused: list[xk.Refusal] = []
        #: (engine owner text, member, node, entry) of every read-position
        #: mention left unproven, for the clone check in :meth:`finish`.
        self.unproven: list[tuple[str | None, str, Node, str]] = []
        #: Each name a proven read relies on, and the entries read through it
        #: (``""`` for a constant), so a refusal of it names what it keeps.
        self.relied: dict[str, set[str]] = {}
        self.read_nodes: set[int] = set()
        #: Each proven case read and the node it is reported at, so a file
        #: refused for anything still names every case it read.
        self.read_sites: list[tuple[xk.KeyReference, Node]] = []
        #: Module names a proven ``Module.Owner.X`` read relies on: their
        #: occurrences in an ``import`` look nothing else up.
        self.module_prefixes: set[str] = set()
        self._occurrences: dict[str, list[Node]] = {}
        self._memo: dict[tuple, str] = {}

    def occurrences(self, name: str) -> list[Node]:
        if name not in self._occurrences:
            self._occurrences[name] = [
                n for n in xk.walk(self.program)
                if n.type in ("simple_identifier", "type_identifier") and _ident(n) == name
            ]
        return self._occurrences[name]

    def refuse(self, node: Node, reason: str, decl: xk.KeyDeclaration | None = None) -> None:
        entry = "" if decl is None else decl.entry
        self.refused.append(xk.refusal(self.path, node, reason, self.source, entry))

    # -- what a name means, per module ------------------------------------

    def answer(self, decl: xk.KeyDeclaration, head: str) -> str:
        key = (decl.path, decl.owner, decl.name, head)
        if key not in self._memo:
            found = {self.lookup(decl, head, module) for module in self.modules}
            self._memo[key] = found.pop() if len(found) == 1 else UNKNOWN
        return self._memo[key]

    def lookup(self, decl: xk.KeyDeclaration, head: str, module) -> str:
        world = self.world
        declaring = world.layout.modules_of(decl.path) or frozenset()
        if world.broken(module):
            return UNKNOWN
        if module in declaring:
            return PROVEN if world.top_count(module, head) == 1 else UNKNOWN
        # A typealias of the name in the reader's module may name the owner
        # itself (`typealias Flags = Kit.Flags`), at the top level or in a
        # type the read sits in, so the name is not proven to mean another.
        aliased = head in world.aliases_in(module)
        if world.top_count(module, head):
            return UNKNOWN if aliased else OTHER  # the module's own declaration wins over every import
        access = world.access(decl)
        names = {m.name for m in declaring}
        # An `@_exported import` anywhere in the module is an import of every file of it.
        imports = [*self.imports, *(_Import((n,), False, False, False) for n in sorted(world.exported(module)))]
        routed = False
        for imported in imports:
            first = imported.segments[0]
            if first in names:
                # Matched by identity: another checkout module of this name may be the one imported.
                if not world.layout.named(first) <= declaring:
                    return UNKNOWN
                if imported.unsure or not _allows(access, imported.testable, module, declaring):
                    return UNKNOWN
                routed = True
                continue
            others = world.layout.named(first)
            if others:
                for other in others:
                    if world.broken(other) or world.top_count(other, head):
                        return UNKNOWN
                    if any(world.layout.named(n) or n in world.layout.opaque_names for n in world.exported(other)):
                        return UNKNOWN
                continue
            if first in world.layout.opaque_names or world.layout.unnamed:
                return UNKNOWN
        if routed:
            return PROVEN
        return UNKNOWN if aliased else OTHER

    def prefixed(self, decl: xk.KeyDeclaration, prefix: str) -> str:
        """What ``prefix.Owner`` means in every module the reader is compiled into."""
        key = (decl.path, decl.owner, decl.name, "module", prefix)
        if key not in self._memo:
            found = {self.module_lookup(decl, prefix, module) for module in self.modules}
            self._memo[key] = found.pop() if len(found) == 1 else UNKNOWN
        return self._memo[key]

    def module_lookup(self, decl: xk.KeyDeclaration, prefix: str, module) -> str:
        """``prefix.Owner`` in ``module``: the declaring module's own top-level
        ``Owner`` when ``prefix`` can only name that module.

        Measured on Swift 6.0: a module-qualified name skips the reader
        module's own ``Owner``, but a declaration named like the module wins
        over the module, whether the reader's module or any module it imports
        declares it (an enclosing type's member too, which :meth:`scope`
        checks). An outside module cannot be read: one declaring a type named
        like a checkout module is not seen. The module must be the reader's
        own or imported plainly, at an access that allows the read."""
        world = self.world
        declaring = world.layout.modules_of(decl.path) or frozenset()
        named = world.layout.named(prefix)
        if not named or prefix in world.layout.opaque_names:
            return UNKNOWN
        head = decl.owner.split(".")[0]
        if not named & declaring:
            # Another module's own `Owner`, unless a typealias of that name
            # anywhere (in that module, or in a type named like it) may be
            # this one.
            return UNKNOWN if head in world.aliases else OTHER
        if not named <= declaring or world.broken(module):
            return UNKNOWN
        if world.top_count(module, prefix):
            return UNKNOWN
        imports = [*self.imports, *(_Import((n,), False, False, False) for n in sorted(world.exported(module)))]
        for imported in imports:
            first = imported.segments[0]
            others = world.layout.named(first)
            if not others and (first in world.layout.opaque_names or world.layout.unnamed):
                return UNKNOWN
            for other in others:
                if world.broken(other) or world.top_count(other, prefix):
                    return UNKNOWN
                if any(world.layout.named(n) or n in world.layout.opaque_names for n in world.exported(other)):
                    return UNKNOWN
        if any(world.broken(d) or world.top_count(d, head) != 1 for d in declaring):
            return UNKNOWN
        if module in declaring:
            return PROVEN
        access = world.access(decl)
        route = [i for i in imports if i.segments[0] == prefix]
        if not route or any(i.unsure or not _allows(access, i.testable, module, declaring) for i in route):
            return UNKNOWN
        return PROVEN

    def scope(self, node: Node, head: str, bare: bool) -> str | None:
        """A refusal when an enclosing type can claim ``head``."""
        current = node.parent
        while current is not None:
            if _kind(current) is not None:
                name = _type_name(current)
                if name is None:
                    return xk.UNSUPPORTED_FORM
                # Every type an extension names is in scope inside it, outer ones included.
                parts = name.split(".")
                if _kind(current) == "extension" and any(p in self.world.aliases for p in parts):
                    return xk.UNSUPPORTED_FORM  # an alias may reach any type's members and parameters
                for part in parts:
                    verdict = self.world.type_scope(part, head)
                    if verdict == "member":
                        return xk.REBOUND
                    if verdict == "alias" or (verdict == "outside" and bare):
                        return xk.UNSUPPORTED_FORM
            elif current.type == "function_declaration":
                # `func f() where Self: Base` in a protocol extension. Swift
                # 6.0 measures Base's members as NOT in unqualified scope
                # there, unlike under the same clause on the extension, but
                # nothing in the language reference pins that, so it is
                # treated as a supertype: at worst a read that was safe is
                # refused.
                for parent in _self_constraints(current):
                    verdict = self.world.type_scope(parent, head) if parent else "outside"
                    if parent in self.world.aliases:
                        verdict = "alias"
                    if verdict == "member":
                        return xk.REBOUND
                    if verdict == "alias" or (verdict == "outside" and bare):
                        return xk.UNSUPPORTED_FORM
            current = current.parent
        return None

    # -- the mentions of one declaration ------------------------------------

    def judge(self, decl: xk.KeyDeclaration) -> None:
        if decl.kind == xk.ENUM_ENTRY:
            self.judge_entry(decl)
            return
        for node in self.occurrences(decl.name):
            parent = node.parent
            if node.type == "type_identifier" or parent is None or parent.type == "value_argument_label":
                continue
            if parent.type == "navigation_suffix":
                nav = parent.parent
                segments = None if nav is None else _segments(nav)
                if segments is None:
                    if decl.owner is not None:
                        self.refuse(node, xk.NOT_A_READ)  # `self.NAME`, `type(of: x).NAME`
                    continue
                self.chain(decl, segments, nav)
                continue
            if parent.type == "prefix_expression" and _text(parent.child_by_field_name("operation")) == ".":
                if decl.owner is not None and _is_read(parent, self.accessors):
                    self.refuse(node, xk.UNSUPPORTED_FORM)  # `.NAME` in the key slot
                continue
            if decl.owner is not None:
                if self.in_owner(decl, node):
                    self.refuse(node, xk.UNSUPPORTED_FORM)  # a bare read inside the owner
                continue
            if _is_binding(node):
                if self.answer(decl, decl.name) != OTHER:
                    self.refuse(node, xk.REBOUND)
                continue
            self.chain(decl, [node], node)

    def in_owner(self, decl: xk.KeyDeclaration, node: Node) -> bool:
        last = decl.owner.rsplit(".", 1)[-1]
        current = node.parent
        while current is not None:
            if _kind(current) is not None:
                name = (_type_name(current) or "").rsplit(".", 1)[-1]
                supers = set(_supers(current))
                if name == last or last in supers or self.descends(name, supers, last):
                    return True
            elif current.type == "function_declaration":
                supers = set(_self_constraints(current))  # `func f() where Self: Keys`
                if last in supers or self.descends("", supers, last):
                    return True
            current = current.parent
        return False

    def descends(self, name: str, supers: set[str], last: str) -> bool:
        """Whether ``last`` is reachable through the supertypes of a type."""
        seen: set[str] = set()
        pending = [s for s in supers if s]
        if name:
            pending.append(name)
        while pending:
            current = pending.pop()
            if current == last:
                return True
            if current in seen:
                continue
            seen.add(current)
            for entry in self.world.by_last.get(current, ()):
                pending.extend(s for s in entry.supers if s)
        return False

    def shadowed_owner(self, decl: xk.KeyDeclaration) -> bool:
        """Whether an intermediate segment of a dotted owner is also declared
        as a member by another entry of the same name (an extension adding a
        typealias, say), so the segment may not mean the nested type."""
        parts = decl.owner.split(".")
        for i in range(len(parts) - 1):
            entries = self.world.by_last.get(parts[i], ())
            if sum(parts[i + 1] in entry.members for entry in entries) > 1:
                return True
            # An extension spelled through a typealias anywhere in its name
            # (`extension NS.Inner`, `extension K`) may extend any type.
            if any(e.kind == "extension" and parts[i + 1] in e.members for e in self.world.through_alias):
                return True
        return False

    def qualifies(self, decl: xk.KeyDeclaration, qualifier: list[str]) -> bool:
        """Whether ``Q.NAME``, with ``Q`` not the owner, may still read the
        owner's constant: a metatype, the owner's last name from inside it, a
        typealias, or a subclass."""
        last = qualifier[-1]
        if any(q in _METATYPE for q in qualifier) or last == decl.owner.rsplit(".", 1)[-1]:
            return True
        if last in self.world.aliases:
            return True
        return any(entry.kind == "class" and entry.supers for entry in self.world.by_last.get(last, ()))

    def chain(self, decl: xk.KeyDeclaration, segments: list[Node], value: Node) -> None:
        texts = [_ident(s) for s in segments]
        if len(segments) > 1 and texts[0] in self.world.layout.names:
            self.refuse(segments[-1], xk.UNSUPPORTED_FORM)  # `Module.Owner.NAME`
            return
        if decl.owner is None:
            if len(segments) != 1:
                return  # `x.NAME`: a member of something else
            head, owner_text = decl.name, None
        else:
            qualifier = ".".join(texts[:-1])
            if qualifier != decl.owner:
                if self.qualifies(decl, texts[:-1]):
                    self.refuse(segments[-1], xk.UNSUPPORTED_FORM)
                return
            head, owner_text = texts[0], qualifier
        answer = self.answer(decl, head)
        read = _is_read(value, self.accessors)
        if answer == OTHER:
            if read:
                self.unproven.append((_engine_owner(value), decl.name, segments[-1], ""))
            return
        if answer == UNKNOWN:
            self.refuse(segments[-1], xk.IMPORT_UNRESOLVED)
            return
        if decl.owner is not None and "." in decl.owner and self.shadowed_owner(decl):
            self.refuse(segments[-1], xk.UNSUPPORTED_FORM)
            return
        reason = self.scope(segments[0], head, bare=decl.owner is None)
        if reason is not None:
            self.refuse(segments[-1], reason)
            return
        if not read:
            self.refuse(segments[-1], xk.NOT_A_READ)
            return
        if _text(value) != ".".join(texts):
            self.refuse(segments[-1], xk.UNSUPPORTED_FORM)
            return
        self.reads.append(xk.KeyReference(self.path, owner_text, decl.name))
        self.read_nodes.add(segments[0].id)
        self.relied.setdefault(head, set()).add("")

    def judge_entry(self, decl: xk.KeyDeclaration) -> None:
        """Every mention of an enum case's name in a reader. Only
        ``Owner.X.accessor`` can be proven (:meth:`entry_chain`); ``.X``
        reaches the case without spelling its owner, so it is refused
        wherever it is spelled."""
        for node in self.occurrences(decl.name):
            parent = node.parent
            if node.type == "type_identifier" or parent is None or parent.type == "value_argument_label":
                continue
            if parent.type == "navigation_suffix":
                nav = parent.parent
                segments = None if nav is None else _segments(nav)
                if segments is None:
                    self.refuse(node, xk.NOT_A_READ, decl)  # `self.X`, `f().X`
                else:
                    self.entry_chain(decl, segments, nav)
            elif _implicit(node):
                # `.X` as a value, or a `case .X` / `if case .X` pattern.
                reason = xk.UNSUPPORTED_FORM if parent.type == "prefix_expression" or _implicit_chain(node) else xk.NOT_A_READ
                self.refuse(node, reason, decl)
            elif self.in_owner(decl, node):
                self.refuse(node, xk.UNSUPPORTED_FORM, decl)  # a bare `X` in the enum or an extension of it

    def entry_chain(self, decl: xk.KeyDeclaration, segments: list[Node], nav: Node) -> None:
        """``Owner.X`` naming a case (``nav`` is that expression): proven only
        as the whole key argument followed by one of the case's accessors,
        with the owner resolved as a constant's is, and no extension in the
        reader's module declaring a member named like the accessor."""
        texts = [_ident(s) for s in segments]
        parts = decl.owner.split(".")
        qualifier = texts[:-1]
        prefix = None
        if qualifier != parts:
            if qualifier[1:] == parts and qualifier[0] in self.world.layout.names:
                prefix = qualifier[0]
            else:
                if self.qualifies(decl, qualifier):
                    self.refuse(segments[-1], xk.UNSUPPORTED_FORM, decl)
                return
        accessor = self.accessor_after(nav)
        value = nav if accessor is None else nav.parent
        read = accessor is not None and _is_read(value, self.accessors)
        owner_text = ".".join(qualifier)
        head = texts[0]
        answer = self.answer(decl, head) if prefix is None else self.prefixed(decl, prefix)
        if answer == OTHER:
            if read:
                self.unproven.append((owner_text, decl.name, segments[-1], decl.entry))
            return
        if answer == UNKNOWN:
            self.refuse(segments[-1], xk.IMPORT_UNRESOLVED, decl)
            return
        if "." in decl.owner and self.shadowed_owner(decl):
            self.refuse(segments[-1], xk.UNSUPPORTED_FORM, decl)
            return
        reason = self.scope(segments[0], head, bare=False)
        if reason is not None:
            self.refuse(segments[-1], reason, decl)
            return
        if accessor is None:
            self.refuse(segments[-1], xk.NOT_A_READ, decl)  # the case as a value, or `case Owner.X:`
            return
        name = _ident(accessor)
        if name not in decl.accessors:
            self.refuse(segments[-1], xk.UNSUPPORTED_FORM, decl)  # another member of the case
            return
        if not read:
            self.refuse(segments[-1], xk.NOT_A_READ, decl)
            return
        if _text(value) != f"{'.'.join(texts)}.{name}":
            self.refuse(segments[-1], xk.UNSUPPORTED_FORM, decl)  # spaced or backticked
            return
        if any(self.world.extends_with(module, decl.owner, name) for module in self.modules):
            self.refuse(segments[-1], xk.UNSUPPORTED_FORM, decl)
            return
        ref = xk.KeyReference(self.path, owner_text, decl.name, name, decl)
        self.reads.append(ref)
        self.read_sites.append((ref, segments[-1]))
        self.read_nodes.add(segments[0].id)
        self.relied.setdefault(head, set()).add(decl.entry)
        if prefix is not None:
            self.module_prefixes.add(prefix)

    @staticmethod
    def accessor_after(nav: Node) -> Node | None:
        """The member read off the case in ``Owner.X.member``; None when
        nothing is."""
        outer = nav.parent
        if outer is None or outer.type != "navigation_expression":
            return None
        target = outer.child_by_field_name("target")
        suffix = outer.child_by_field_name("suffix")
        member = None if suffix is None else suffix.child_by_field_name("suffix")
        if target is None or target.id != nav.id or member is None or member.type != "simple_identifier":
            return None
        return member

    # -- the identifiers a proven read relies on ------------------------------

    def accepted(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type in ("value_argument_label", "navigation_suffix"):
            return True  # a label, or a member of something else
        if parent.type == "identifier" and parent.parent is not None and parent.parent.type == "import_declaration":
            # `import Kit`: names the module a proven `Kit.Owner.X` relies on.
            return _ident(node) in self.module_prefixes
        if node.type == "simple_identifier":
            if parent.type == "call_expression":
                # `Owner(rawValue: s)`: a dynamic use of the type, which
                # reads no case by name and never blocks a fold.
                return parent.children[0].id == node.id
            if parent.type != "navigation_expression":
                return False
            target = parent.child_by_field_name("target")
            suffix = parent.child_by_field_name("suffix")
            member = None if suffix is None else suffix.child_by_field_name("suffix")
            return (
                target is not None and target.id == node.id
                and member is not None and member.type == "simple_identifier"
                and _ident(member) not in _METATYPE
            )
        if parent.type != "user_type":
            return False  # a key path, or a declaration's name
        names = [c for c in parent.named_children if c.type == "type_identifier"]
        index = next(i for i, c in enumerate(names) if c.id == node.id)
        follower = names[index + 1] if index + 1 < len(names) else None
        return follower is None or _ident(follower) not in _METATYPE

    def eliminate(self) -> None:
        for name, entries in sorted(self.relied.items()):
            for node in self.occurrences(name):
                if node.id in self.read_nodes or self.accepted(node):
                    continue
                reason = xk.REBOUND if _is_binding(node) else xk.NOT_A_READ
                for entry in sorted(entries):
                    self.refused.append(xk.refusal(self.path, node, reason, self.source, entry))

    def finish(self) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
        self.eliminate()
        # Defence in depth: a reader's clone rewrites every spelling of its
        # owner and member in the file, so an unproven read-position mention
        # with the same spelling would be folded too.
        proven = {(r.owner, r.member) for r in self.reads}
        for owner, member, node, entry in self.unproven:
            if (owner, member) in proven:
                self.refused.append(xk.refusal(self.path, node, xk.UNSUPPORTED_FORM, self.source, entry))
        if self.refused:
            # Every read goes with the file, so a case read here is named by a
            # refusal too, or it would be reported kept.
            named = {r.entry for r in self.refused}
            for ref, node in self.read_sites:
                entry = ref.declaration.entry
                if entry not in named:
                    named.add(entry)
                    self.refused.append(xk.refusal(self.path, node, xk.UNSUPPORTED_FORM, self.source, entry))
            return [], self.refused
        return list(dict.fromkeys(self.reads)), []


def references(path, source, declarations, accessors, repo_dir):
    """The proven reads of ``declarations`` in ``path``, and its refusals.

    A file with any refusal yields no reads: what it cannot prove keeps the
    declaration, and a reader left holding an unproven mention could not be
    told apart from one the rules missed. A file whose parse has errors, or
    whose module is unknown, is refused whole.
    """
    data = source.encode("utf-8")
    program = parse(data)
    if program.has_error:
        return [], _whole_file(path, source, declarations)
    world = _world(str(Path(source_index.checkout_root(repo_dir)).resolve()))
    modules = None if world is None else world.layout.modules_of(path)
    if modules is None:
        return [], _whole_file(path, source, declarations)
    judgement = _Judgement(path, data, program, modules, world, accessors)
    for decl in declarations:
        judgement.judge(decl)
    return judgement.finish()


def _whole_file(path: str, source: str, declarations) -> list[xk.Refusal]:
    """A file refused whole, at the first line spelling anything it might
    read: once for the constants, and once per case it spells, so a case a
    refused file may read is reported left rather than kept."""
    words = {d.name for d in declarations} | {d.owner.split(".")[0] for d in declarations if d.owner}
    token = xk.mention_pattern(words)
    lines = source.splitlines()
    number = next((i for i, line in enumerate(lines) if token.search(line)), 0)
    text = lines[number].strip() if lines else ""
    entries = [""] if any(d.kind != xk.ENUM_ENTRY for d in declarations) else []
    entries += sorted({
        d.entry
        for d in declarations
        if d.kind == xk.ENUM_ENTRY and xk.mention_pattern({d.name, *d.owner.split(".")}).search(source)
    })
    return [xk.Refusal(path, number + 1, xk.UNSUPPORTED_FORM, text, entry) for entry in entries or [""]]


def _last_segment(node: Node | None) -> str:
    """The last name of a dotted expression (``Flags``, ``App.Flags``)."""
    if node is None:
        return ""
    if node.type == "simple_identifier":
        return _ident(node)
    if node.type == "navigation_expression":
        suffix = node.child_by_field_name("suffix")
        member = None if suffix is None else suffix.child_by_field_name("suffix")
        return "" if member is None else _ident(member)
    return ""


def _by_raw_value(call: Node) -> bool:
    """Whether ``call``'s first argument is labelled ``rawValue:``."""
    suffix = next((c for c in call.named_children if c.type == "call_suffix"), None)
    arguments = None if suffix is None else next((c for c in suffix.named_children if c.type == "value_arguments"), None)
    first = None if arguments is None else next((c for c in arguments.named_children if c.type == "value_argument"), None)
    label = None if first is None else first.child_by_field_name("name")
    return label is not None and _text(label).strip() == "rawValue"


def _sees(world: _World | None, path: str, imported: set[str], decl: xk.KeyDeclaration) -> bool:
    """Whether a file can see ``decl``'s module: compiled into it, or importing
    it by name, here or through an ``@_exported import``. True when either
    module set is unknown: a note only asks a reviewer to look."""
    if world is None:
        return True
    mine = world.layout.modules_of(path)
    declaring = world.layout.modules_of(decl.path)
    if mine is None or declaring is None or mine & declaring:
        return True
    names = {m.name for m in declaring}
    return bool(names & imported) or any(names & world.exported(m) for m in mine)


def enum_dynamic_uses(path, source, entries, repo_dir) -> list[xk.Refusal]:
    """The ``enum-dynamic`` notes for ``path``: ``Owner.allCases``,
    ``Owner(rawValue:)`` and ``Owner.init(rawValue:)``, and a bare
    ``allCases`` inside the enum or an extension of it, through which
    iteration and a lookup by value reach every case. Matched on the owner's
    last name, in a file that can see the declaring module, without proving
    which enum it means: a note only asks a reviewer to look."""
    program = parse(source.encode("utf-8"))
    if program.has_error:
        return []
    world = _world(str(Path(source_index.checkout_root(repo_dir)).resolve()))
    imported = {i.segments[0] for n in xk.walk(program) if (i := _import(n)) is not None}
    by_owner: dict[str, list[xk.KeyDeclaration]] = {}
    for decl in entries:
        if decl.owner is not None and _sees(world, path, imported, decl):
            by_owner.setdefault(decl.owner.rsplit(".", 1)[-1], []).append(decl)
    notes: list[xk.Refusal] = []

    def note(node: Node, owner: str) -> None:
        for decl in by_owner.get(owner, ()):
            found = xk.refusal(path, node, xk.ENUM_DYNAMIC, source, decl.entry)
            if found not in notes:
                notes.append(found)

    for node in xk.walk(program):
        if node.type == "simple_identifier" and _ident(node) == "allCases":
            parent = node.parent
            if parent is not None and parent.type == "navigation_suffix":
                nav = parent.parent
                note(node, _last_segment(None if nav is None else nav.child_by_field_name("target")))
            elif not _is_binding(node):
                holder = node.parent
                while holder is not None and _kind(holder) not in ("enum", "extension"):
                    holder = holder.parent
                if holder is not None:
                    note(node, (_type_name(holder) or "").rsplit(".", 1)[-1])
        elif node.type == "call_expression" and node.named_children and _by_raw_value(node):
            callee = node.named_children[0]
            if callee.type == "navigation_expression" and _last_segment(callee) == "init":
                callee = callee.child_by_field_name("target")
            note(node, _last_segment(callee))
    return notes


xk.register(
    "swift",
    xk.Profile(
        language_family=frozenset({"swift"}),
        declarations=declarations,
        references=references,
        entry_findings=entry_findings,
        enum_dynamic_uses=enum_dynamic_uses,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
    ),
)
