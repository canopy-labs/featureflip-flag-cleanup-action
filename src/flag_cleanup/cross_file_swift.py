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
"""

from __future__ import annotations

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


_PLACED: dict[str, tuple[tuple[int, int], dict[tuple[str | None, str], _Placement]]] = {}


def _placements_of(path: str) -> dict[tuple[str | None, str], _Placement]:
    """Every offerable ``let`` of the file on disk, by ``(owner, name)``."""
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
            if node.type != "property_declaration":
                continue
            names = _bound(node)
            placed = _placement(node) if len(names) == 1 else None
            if placed is not None:
                found[(placed.owner, names[0])] = placed
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


@dataclass(frozen=True)
class _Share:
    #: Every top-level declared name, repeats kept.
    top: tuple[str, ...]
    types: tuple[_Type, ...]
    aliases: frozenset[str]
    #: Modules this file re-exports with ``@_exported import``.
    exported: tuple[str, ...]
    complete: bool


def _last_name(target: Node | None) -> str:
    """The last name of a written type; "" for one that is not a plain name
    (a composition ``A & B``, an optional, a function type)."""
    if target is None or target.type != "user_type":
        return ""
    names = [c for c in target.named_children if c.type == "type_identifier"]
    return _ident(names[-1]) if names else ""


def _super_name(spec: Node) -> str:
    return _last_name(spec.child_by_field_name("inherits_from"))


def _self_constraints(node: Node) -> list[str]:
    """The types a ``where Self: X`` or ``where Self == X`` clause of ``node``
    names, each as :func:`_last_name` reads it. Inside an extension or a
    protocol carrying one, or a method of either, X's members are in
    unqualified scope exactly as a supertype's are."""
    found: list[str] = []
    for clause in (c for c in node.named_children if c.type == "type_constraints"):
        for constraint in (c for c in clause.named_children if c.type == "type_constraint"):
            for inner in constraint.named_children:
                if inner.type not in ("inheritance_constraint", "equality_constraint"):
                    continue
                if _ident(inner.child_by_field_name("constrained_type")) == "Self":
                    found.append(_last_name(inner.child_by_field_name("name")))
    return found


def _supers(node: Node) -> list[str]:
    """Every supertype a type declaration names: its inheritance clause and
    its ``where Self`` constraints."""
    return [
        *(_super_name(c) for c in node.named_children if c.type == "inheritance_specifier"),
        *_self_constraints(node),
    ]


def _names_declared(node: Node) -> list[str]:
    """The names a top-level declaration introduces into its module."""
    kind = _kind(node)
    if kind is not None:
        name = _type_name(node)
        return [] if kind == "extension" or name is None else [name]
    if node.type in ("typealias_declaration", "function_declaration", "macro_declaration"):
        name = node.child_by_field_name("name")
        return [] if name is None else [_ident(name)]
    if node.type == "property_declaration":
        return _bound(node)
    return []


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
    return _Type(name, kind, tuple(_supers(node)), members, _written_access(node), found_holders is None)


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
    for child in program.named_children:
        top.extend(_names_declared(child))
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
    share = _Share(tuple(top), tuple(types), frozenset(aliases), tuple(exported), not program.has_error)
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
        every: list[_Type] = []
        for path, share in shares:
            for module in layout.modules_of(path) or ():
                self._files.setdefault(module, []).append(share)
            self.aliases |= share.aliases
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

    def types_in(self, module, full: str) -> list[_Type]:
        return [entry for path, entry in self.by_full.get(full, ()) if module in (self.layout.modules_of(path) or ()) and not entry.local]

    def offerable(self, path: str, placed: _Placement, name: str) -> bool:
        """Whether a constant ``name`` placed so in ``path`` can be offered at all."""
        modules = self.layout.modules_of(path)
        if modules is None or any(self.broken(m) for m in modules):
            return False
        if placed.owner is None:
            return True
        parts = placed.owner.split(".")
        for part in parts:
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
        """The declaration's effective access level; None when unreadable."""
        placed = _placements_of(decl.path).get((decl.owner, decl.name))
        if placed is None:
            return None
        if placed.extended is None:
            return placed.access
        modules = self.layout.modules_of(decl.path) or frozenset()
        caps = [cap for m in modules for cap in self._caps(m, placed.extended)]
        if not caps or any(cap not in _RANK for cap in caps):
            return None
        return min([placed.access, *caps], key=_RANK.__getitem__)

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
    return found


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
        #: (engine owner text, member) of every read-position mention left
        #: unproven, for the clone check in :meth:`finish`.
        self.unproven: list[tuple[str | None, str, Node]] = []
        self.relied: set[str] = set()
        self.read_nodes: set[int] = set()
        self._occurrences: dict[str, list[Node]] = {}
        self._memo: dict[tuple, str] = {}

    def occurrences(self, name: str) -> list[Node]:
        if name not in self._occurrences:
            self._occurrences[name] = [
                n for n in xk.walk(self.program)
                if n.type in ("simple_identifier", "type_identifier") and _ident(n) == name
            ]
        return self._occurrences[name]

    def refuse(self, node: Node, reason: str) -> None:
        self.refused.append(xk.refusal(self.path, node, reason, self.source))

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
        if world.top_count(module, head):
            return OTHER  # the module's own declaration wins over every import
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
        return PROVEN if routed else OTHER

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
                self.unproven.append((_engine_owner(value), decl.name, segments[-1]))
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
        self.relied.add(head)

    # -- the identifiers a proven read relies on ------------------------------

    def accepted(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type in ("value_argument_label", "navigation_suffix"):
            return True  # a label, or a member of something else
        if node.type == "simple_identifier":
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
        for name in sorted(self.relied):
            for node in self.occurrences(name):
                if node.id in self.read_nodes or self.accepted(node):
                    continue
                self.refuse(node, xk.REBOUND if _is_binding(node) else xk.NOT_A_READ)

    def finish(self) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
        self.eliminate()
        # Defence in depth: a reader's clone rewrites every spelling of its
        # owner and member in the file, so an unproven read-position mention
        # with the same spelling would be folded too.
        proven = {(r.owner, r.member) for r in self.reads}
        for owner, member, node in self.unproven:
            if (owner, member) in proven:
                self.refuse(node, xk.UNSUPPORTED_FORM)
        if self.refused:
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
    words = {d.name for d in declarations} | {d.owner.split(".")[0] for d in declarations if d.owner}
    if program.has_error:
        return [], [_whole_file(path, source, words)]
    world = _world(str(Path(source_index.checkout_root(repo_dir)).resolve()))
    modules = None if world is None else world.layout.modules_of(path)
    if modules is None:
        return [], [_whole_file(path, source, words)]
    judgement = _Judgement(path, data, program, modules, world, accessors)
    for decl in declarations:
        judgement.judge(decl)
    return judgement.finish()


def _whole_file(path: str, source: str, words: set[str]) -> xk.Refusal:
    token = xk.mention_pattern(words)
    lines = source.splitlines()
    number = next((i for i, line in enumerate(lines) if token.search(line)), 0)
    return xk.Refusal(path, number + 1, xk.UNSUPPORTED_FORM, lines[number].strip() if lines else "")


xk.register(
    "swift",
    xk.Profile(
        language_family=frozenset({"swift"}),
        declarations=declarations,
        references=references,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
    ),
)
