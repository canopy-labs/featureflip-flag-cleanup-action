"""The family of types a C# keyless wrapper's member belongs to.

The wrapper's member, the interface member it implements, and that member in
every sibling implementation are one member as far as callers can tell: any
of them may be bound behind the interface. A mention of the same NAME on any
other type is not this member, and C# lets that be proven without type
inference, in two ways that hold for any code that compiles:

* **Project reachability.** A file whose project neither is nor references
  (through ``<ProjectReference>``, transitively) a project declaring a family
  type cannot name a family type, so it cannot hold a value of one.
* **Family-free files.** An expression gets a family static type only from
  something that names a family type: a declaration, a cast, a ``typeof``, a
  generic argument, or a member whose signature names one. A file that spells
  no name that can bring one in, and has no ``dynamic``, holds no value of a
  family type. The names that can are collected over the whole checkout:

  - the family types' own names, and every ``using`` alias (in a file or a
    project ``<Using>`` item) whose target spells one of them;
  - *tainting* type names: a type whose non-private header (its base list,
    primary constructor, constraints, a delegate's signature) or any
    non-private member's header (return, property, field or event type, any
    parameter type, of methods, constructors, indexers, operators and
    conversions) spells a name already collected; the type an extension
    method extends, when that method's header does; an alias spelling one;
  - *family-returning* member names: the non-private members whose header
    spells a name collected, a record's positional parameters among them.

  This is a fixpoint, because a value can come through any chain of such
  types: ``o.Shelf()[0].IsEnabled`` names no family type when ``Shelf``
  returns a ``Bag`` whose indexer returns the interface. A parameter type
  counts as much as a return type: ``Run(f => f.IsEnabled)`` types ``f``
  from ``Run``'s parameter, and ``Take(new(...))`` builds a value of the
  parameter's type without naming it. A type taints by its name as well as
  its members' names, since an indexer, an operator, a conversion and the
  pattern methods ``foreach``, ``await``, deconstruction and collection
  initializers call are used without spelling any name.

  A private member is visible only inside its own type, so it is left out,
  except in a ``partial`` type, whose other parts may live in other files,
  and except a field, property or method with an attribute, which a source
  generator may turn into a public member of another name.

  A pattern extension method (``GetEnumerator``, ``Deconstruct``, ``Add``,
  ...) on a type outside the repository, or any extension of a type
  parameter, can hand a family value to code that spells no name, so it
  makes no file family-free; so does a declaration this cannot read.

Everything that cannot be read errs toward reachable and tainted.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_csharp as xcs
from flag_cleanup import cross_file_keys

_DYNAMIC = re.compile(r"\bdynamic\b")
_PARTIAL = re.compile(r"\bpartial\b")
#: Members every type has: never evidence of another type.
_OBJECT_MEMBERS = frozenset({"Equals", "GetHashCode", "GetType", "ToString"})


@dataclass(frozen=True)
class Family:
    #: Full names of the interface, the wrapper's class and every sibling.
    full_names: frozenset[str]
    #: Their simple names, plus every alias of one of them.
    simple_names: frozenset[str]
    #: Every member name a family type declares or inherits in the repository.
    members: frozenset[str]
    #: Some family type declares the member with a setter or ``init``.
    settable: bool
    #: Directories of the projects compiling a family type's file.
    projects: frozenset[Path]
    #: Names of non-private members (anywhere) whose header spells a family
    #: or tainting type's name.
    returning: frozenset[str]
    #: Full names of the siblings alone.
    sibling_full_names: frozenset[str] = frozenset()
    #: Names of the other types (and aliases) a value of a family type can be
    #: reached through without spelling a family name: a file spelling one is
    #: not family-free. See the module docstring.
    tainting: frozenset[str] = frozenset()
    #: Some family type is declared outside every project, or the wrapper's
    #: own class was not found, so project reachability proves nothing.
    homeless: bool = False
    #: Something that may bring a family value in without a name could not be
    #: read: an unreadable C# file or project ``<Using>`` item, a type with a
    #: parse error, a declaration the grammar misreads (an ``extension``
    #: block), or a pattern extension method on a type outside the
    #: repository or on a type parameter, spelling one of these names. No
    #: file is then family-free.
    opaque: bool = False
    #: A Razor, XAML or other .NET markup file spells one of these names. Such
    #: a file compiles into a ``partial`` class this cannot read, so a file
    #: declaring a ``partial`` type is not family-free.
    guest_named: bool = False
    #: Some C# file of the checkout may declare a C# 14 ``extension`` block
    #: (or could not be read): a member access may then bind to an extension
    #: member no type declares, so a member name is no evidence of a type.
    extension_blocks: bool = True
    #: Some C# or Razor file of the checkout spells ``dynamic`` (or could
    #: not be read): a ``var`` may then hold a value of any type through a
    #: member of another file, so a member name is no evidence of a type.
    dynamic_anywhere: bool = True
    #: Some sibling has a base, at any depth, outside the repository (or
    #: could not be read): ``members`` may then miss what that base declares,
    #: so a member name is no evidence of a type.
    sibling_bases_external: bool = True


def _full(namespace: str, name: str) -> str:
    return f"{namespace}.{name}" if namespace else name


def family_of(repo_dir: str, wrapper, index) -> Family:
    """The family of ``wrapper`` (qualified, so ``interface`` and ``siblings``
    are filled in), judged on ``index``."""
    decls = [t for t in index.types.get(wrapper.owner, []) if t.namespace == wrapper.package]
    # Without the wrapper's own class, its project is unknown.
    lost = not decls
    if wrapper.interface is not None:
        decls += [
            t
            for t in index.types.get(wrapper.interface.name, [])
            if t.namespace == wrapper.interface.package and t.path == wrapper.interface.path
        ]
    sibling_full = frozenset(_full(s.namespace, s.owner) for s in wrapper.siblings)
    decls += [
        t
        for s in wrapper.siblings
        for t in index.types.get(s.owner, [])
        if t.namespace == s.namespace and t.path == s.path
    ]
    full = frozenset(_full(t.namespace, t.name) for t in decls)
    simple = frozenset(t.name for t in decls)
    checkout = xcs.csharp_index(repo_dir)
    members: set[str] = set(_OBJECT_MEMBERS)
    supertypes: list[str] = []
    for name in full:
        for info in checkout.by_full.get(name, ()):
            members |= info.members
            supertypes.extend(info.supertypes)
    members |= checkout.inherited_members(supertypes)
    projects_all = xcs._project_dirs(repo_dir)
    homes = [_enclosing(t.path, projects_all) for t in decls]
    taint = _taint(repo_dir, simple, frozenset(checkout.by_last), frozenset(checkout.by_full))
    return Family(
        full_names=full,
        simple_names=taint.family,
        members=frozenset(members),
        settable=any(_settable(t.path, t.start, t.end, wrapper.name) for t in decls),
        projects=frozenset(p for home in homes for p in home),
        returning=taint.returning,
        sibling_full_names=sibling_full,
        tainting=taint.tainting,
        homeless=lost or any(not home for home in homes),
        opaque=taint.opaque,
        guest_named=taint.guest_named,
        extension_blocks=declares_extension_blocks(repo_dir),
        dynamic_anywhere=spells_dynamic(repo_dir),
        sibling_bases_external=_sibling_bases_external(repo_dir, wrapper.siblings),
    )


def _sibling_bases_external(repo_dir: str, siblings) -> bool:
    from flag_cleanup import wrapper_csharp as wc

    readers = wc._Readers(repo_dir)
    for sibling in siblings:
        reader = readers.get(sibling.path)
        node = wc._find_type_in(reader.root, sibling.namespace, sibling.owner) if reader is not None else None
        if node is None or not wc._bases_in_repository(readers, sibling.path, node):
            return True
    return False


def _settable(path: str, start: int, end: int, name: str) -> bool:
    from flag_cleanup import wrapper_csharp as wc

    source = wc._read(path) if start < end else None
    if source is None:
        return True  # cannot tell: the member may have a setter
    root = wc.parse(source)
    for node in cross_file_keys.walk(root):
        if not (start <= node.start_byte < end):
            continue
        if node.type in ("record_declaration", "record_struct_declaration"):
            # A positional parameter is an `init` (or, in a record struct, a
            # `set`) property.
            parameters = wc._child(node, "parameter_list")
            if parameters is not None and any(
                wc._name_of(p) == name for p in parameters.named_children if p.type == "parameter"
            ):
                return True
            continue
        if node.type != "property_declaration" or wc._name_of(node) != name:
            continue
        accessors = node.child_by_field_name("accessors")
        for accessor in accessors.named_children if accessors is not None else ():
            word = accessor.child_by_field_name("name")
            if word is not None and wc._text(word) in ("set", "init"):
                return True
    return False


# --- the names that can bring a family value into a file ----------------------

_TYPE_DECLARATIONS = frozenset(
    {
        "class_declaration",
        "struct_declaration",
        "record_declaration",
        "record_struct_declaration",
        "interface_declaration",
        "delegate_declaration",
    }
)
_RECORDS = ("record_declaration", "record_struct_declaration")
#: Members whose header can carry a type: named ones and the nameless
#: (constructor, indexer, operator, conversion).
_MEMBER_DECLARATIONS = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "property_declaration",
        "indexer_declaration",
        "operator_declaration",
        "conversion_operator_declaration",
        "event_declaration",
        "event_field_declaration",
        "field_declaration",
    }
)
#: Private members a source generator commonly turns into public ones.
_GENERATOR_INPUTS = frozenset({"field_declaration", "property_declaration", "method_declaration"})
_NAMED_BY_FIELD = frozenset({"method_declaration", "property_declaration", "event_declaration"})
#: What follows a header: never part of a member's signature.
_BODIES = frozenset(
    {"block", "arrow_expression_clause", "accessor_list", "declaration_list", "attribute_list"}
)
_BODY_FIELDS = frozenset({"body", "value", "accessors"})
_WORD = re.compile(r"@?([^\W\d]\w*)")


@dataclass(frozen=True)
class _Header:
    """One declaration's signature, and what it adds when it spells a name."""

    #: ``member``, ``type``, ``alias``, or ``opaque``: a declaration this
    #: cannot read, which makes no file family-free when it spells a name.
    kind: str
    text: str
    #: The member's (or record parameter's) name; the type's for ``type``;
    #: the alias's for ``alias``.
    name: str | None = None
    #: The declaring type's simple name, for a member.
    owner: str | None = None
    #: Repository types an extension method extends.
    extends: tuple[str, ...] = ()
    #: An alias target's simple name.
    target: str | None = None


@dataclass(frozen=True)
class _Taint:
    family: frozenset[str]
    tainting: frozenset[str]
    returning: frozenset[str]
    opaque: bool
    guest_named: bool


# Keyed by the name set itself, so an entry is never stale: a grown set is a
# new key. Bounded only to cap memory across the run's wrappers.
@functools.lru_cache(maxsize=256)
def _words(names: frozenset[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")


def _spells(text: str, names: frozenset[str]) -> bool:
    pattern = _words(names)
    return pattern is not None and pattern.search(text) is not None


def _taint(
    repo_dir: str, simple: frozenset[str], repo_types: frozenset[str], repo_full: frozenset[str]
) -> _Taint:
    from flag_cleanup import source_index
    from flag_cleanup import wrapper_csharp as wc

    # Read again each round rather than held: the checkout may be large.
    paths = [
        f.resolved
        for f in source_index.files(repo_dir, {".cs"}, with_ignored=True)
        if f.path.name.endswith(".cs")
    ]
    family, tainting, returning = set(simple), set[str](), set[str]()
    headers: list[_Header] = []
    opaque = False
    guests = [(path, stem, wc._read(path)) for path, stem in _guests(repo_dir)]
    stemmed: set[str] = set()
    guest_named = False
    usings, readable = xcs.msbuild_usings(repo_dir)
    opaque |= not readable
    for using in usings:
        if using.kind == "alias" and using.alias is not None and using.target is not None:
            headers.append(
                _Header("alias", using.target, using.alias, target=using.target.rsplit(".", 1)[-1])
            )
    parsed: set[str] = set()
    while True:
        before = frozenset(family | tainting)
        for path in paths:
            if path in parsed:
                continue
            text = wc._read(path)
            if text is None:
                # It may declare anything.
                parsed.add(path)
                opaque = True
                continue
            if not _spells(text, before):
                continue
            parsed.add(path)
            root = wc.parse(text)
            headers.extend(_headers(root, text, repo_types, repo_full, _Binder(root, path, repo_dir)))
        _close(headers, family, tainting, returning)
        names = frozenset(family | tainting)
        for path, stem, text in guests:
            if path not in stemmed and (text is None or _spells(text, names)):
                stemmed.add(path)
                guest_named = True
                # The markup's class: whatever names it can reach its
                # members, and a C# member returning it brings it in.
                tainting.add(stem)
        if family | tainting == before:
            break
    names = frozenset(family | tainting)
    opaque |= any(h.kind == "opaque" and _spells(h.text, names) for h in headers)
    tainting -= family
    return _Taint(
        frozenset(family),
        frozenset(tainting),
        frozenset(returning - family),
        opaque,
        guest_named,
    )


class _Binder:
    """C#'s namespace lookup for a pattern extension's receiver, in one file,
    held to the strict reading: a binding counts only where no other assembly
    can supply the name first.

    Any assembly may add types to any namespace, so a simple name is certain
    only when the INNERMOST enclosing namespace itself declares it, which the
    lookup searches first (a type in source wins over an imported one of the
    same full name), or when there is no enclosing namespace and the lookup
    binds it through the file's usings. A qualified name is certain only when
    ``global::``-rooted, or written outside every namespace with a first name
    no alias rebinds. Nothing is certain when the index is incomplete or a
    project ``<Using>`` item cannot be read."""

    def __init__(self, root: Node, path: str, repo_dir: str):
        self._args = (root, path, repo_dir)
        self._built: xcs._Reader | None = None

    def _reader(self) -> xcs._Reader | None:
        if self._built is None:
            self._built = xcs._Reader(*self._args)
        reader = self._built
        return None if reader.index.incomplete or not reader.projects_readable else reader

    def simple(self, node: Node) -> str | None:
        """The repository type the identifier ``node`` certainly binds to."""
        from flag_cleanup import wrapper_csharp as wc

        reader = self._reader()
        if reader is None:
            return None
        name = wc._text(node)
        if wc._type_shadowed(node, name, reader):
            return None
        levels = xcs._levels(node, reader.root, reader.unit_extra)
        if levels[0].unit:
            return wc._type_full(node, reader)
        found = reader.resolve(name, [replace(levels[0], usings=())])
        if found is None:
            return None
        namespace, chain = found
        return f"{namespace}.{chain}" if namespace else chain

    def qualified(self, node: Node) -> str | None:
        """The repository type the qualified name ``node`` certainly names."""
        from flag_cleanup import wrapper_csharp as wc

        reader = self._reader()
        dotted = xcs._dotted(node)
        if reader is None or dotted is None:
            return None
        if wc._text(node).startswith("global::"):
            return dotted
        levels = xcs._levels(node, reader.root, reader.unit_extra)
        if not levels[0].unit:
            return None  # an enclosing namespace may hold the first name
        first = dotted.split(".", 1)[0]
        if any(u.kind == "alias" and u.alias == first for level in levels for u in level.usings):
            return None
        return wc._type_full(node, reader)


def _close(headers: list[_Header], family: set[str], tainting: set[str], returning: set[str]) -> None:
    """Apply every header that spells a collected name, until none adds one."""
    while True:
        size = len(family) + len(tainting) + len(returning)
        names = frozenset(family | tainting)
        for header in headers:
            if header.kind == "opaque" or not _spells(header.text, names):
                continue
            if header.kind == "alias":
                (family if header.target in family else tainting).add(header.name)
            elif header.kind == "type":
                tainting.add(header.name)
            else:
                if header.name is not None:
                    returning.add(header.name)
                if header.owner is not None:
                    tainting.add(header.owner)
                tainting.update(header.extends)
        if len(family) + len(tainting) + len(returning) == size:
            return


def _guests(repo_dir: str) -> list[tuple[str, str]]:
    """``(path, class name)`` of every .NET markup file in the checkout."""
    from flag_cleanup import source_index
    from flag_cleanup.cross_file_dotnet_guest import DOTNET_GUEST_EXTENSIONS

    suffixes = DOTNET_GUEST_EXTENSIONS["dotnet_guest"]
    found: list[tuple[str, str]] = []
    for source in source_index.files(repo_dir, None, with_ignored=True):
        if source.path.name.lower().endswith(suffixes):
            found.append((source.resolved, source.path.name.split(".", 1)[0]))
    return found


def _header_text(node: Node) -> str:
    """``node``'s signature: everything but its attributes and its body,
    accessors or initializer; a field's or event's type alone."""
    from flag_cleanup import wrapper_csharp as wc

    parts: list[str] = []
    for i, child in enumerate(node.children):
        if not child.is_named or child.type in _BODIES or node.field_name_for_child(i) in _BODY_FIELDS:
            continue
        if child.type == "variable_declaration":
            type_node = child.child_by_field_name("type")
            if type_node is not None:
                parts.append(wc._text(type_node))
            continue
        parts.append(wc._text(child))
    return " ".join(parts)


def _enclosing_type(node: Node) -> Node | None:
    current = node.parent
    while current is not None and current.type not in _TYPE_DECLARATIONS:
        current = current.parent
    return current


def _in_partial(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type in _TYPE_DECLARATIONS and "partial" in xcs._modifiers(current):
            return True
        current = current.parent
    return False


def _private(node: Node) -> bool:
    """Visible only inside its own type: ``private`` (not ``private
    protected``) or ``file``, or no access modifier in a class, struct or
    record. A top-level declaration defaults to ``internal``, an interface
    member to ``public``. A member of a ``partial`` type is visible to its
    other parts, which may be in other files, so it never counts as private."""
    words = xcs._modifiers(node)
    if "file" in words:
        return True
    if words & {"public", "internal", "protected"}:
        return False
    if "private" not in words:
        parent = node.parent
        holder = parent.parent if parent is not None and parent.type == "declaration_list" else None
        if holder is None or holder.type not in _TYPE_DECLARATIONS - {"interface_declaration"}:
            return False
    return not _in_partial(node)


#: Methods the language calls by name without the code spelling it:
#: ``foreach``, ``await``, deconstruction, collection initializers, ``using``,
#: ``fixed``, ranges, and query expressions.
_PATTERN_METHODS = frozenset(
    {
        "GetEnumerator",
        "GetAsyncEnumerator",
        "GetAwaiter",
        "GetResult",
        "Deconstruct",
        "Add",
        "Dispose",
        "DisposeAsync",
        "GetPinnableReference",
        "Slice",
        "Select",
        "SelectMany",
        "Where",
        "OrderBy",
        "OrderByDescending",
        "ThenBy",
        "ThenByDescending",
        "GroupBy",
        "Join",
        "GroupJoin",
        "Cast",
    }
)


def _unambiguous(
    type_node: Node,
    repo_types: frozenset[str],
    repo_full: frozenset[str],
    generic: set[str],
    lookup: _Binder,
) -> bool:
    """Whether a type names a repository type for certain, by
    :class:`_Binder`'s strict lookup: ``Range`` in ``namespace Core`` does,
    ``Range`` in ``namespace Core.Sub`` or beside ``using Core;`` in
    ``namespace Ext`` does not (``Core.Sub.Range`` or ``Ext.Range`` may come
    from any assembly), and ``System.Range`` never does.

    Only a whole dotted name counts, never one that qualifies a longer one:
    in ``Core.Holder.Enumerator`` the type is ``Enumerator``, which a
    repository ``Holder`` may inherit from a library base, so the name is
    the repository's only when the index knows ``Core.Holder.Enumerator``
    itself (a nested type the repository declares)."""
    from flag_cleanup import wrapper_csharp as wc

    def qualifies(name: Node) -> bool:
        outer = name.parent
        if outer is None or outer.type not in ("qualified_name", "alias_qualified_name"):
            return False
        index = next(i for i, c in enumerate(outer.children) if c.id == name.id)
        return outer.field_name_for_child(index) in ("qualifier", "alias")

    for node in cross_file_keys.walk(type_node):
        if node.type not in ("identifier", "generic_name"):
            continue
        parent = node.parent
        if node.type == "identifier" and parent is not None and parent.type == "generic_name":
            continue  # the generic name is the unit
        simple = xcs._simple_name(node)
        if simple is None or simple in generic or simple not in repo_types:
            continue
        if qualifies(node):
            continue  # part of a qualifier, not a type here
        if parent is None or parent.type not in ("qualified_name", "alias_qualified_name"):
            if node.type == "identifier" and lookup.simple(node) in repo_full:
                return True
            continue
        if qualifies(parent):
            continue  # `Core.Holder` in `Core.Holder.Enumerator`
        if parent.type == "alias_qualified_name":
            alias = parent.child_by_field_name("alias")
            if alias is not None and wc._text(alias) == "global" and simple in repo_full:
                return True
            continue
        if lookup.qualified(parent) in repo_full:
            return True
    return False


def _extension(
    method: Node,
    name: str | None,
    repo_types: frozenset[str],
    repo_full: frozenset[str],
    lookup: _Binder,
) -> tuple[tuple[str, ...], bool]:
    """``(extended, opaque)`` for a method: the repository types its ``this``
    parameter names, and whether a value it returns can reach a file through
    a receiver that names nothing collectable. That is so for an extension of
    a type parameter, and for a pattern method (called with no name spelled)
    extending a type that is not certainly the repository's."""
    from flag_cleanup import wrapper_csharp as wc

    parameters = method.child_by_field_name("parameters")
    listed = parameters.named_children if parameters is not None else []
    first = next((p for p in listed if p.type == "parameter"), None)
    if first is None or "this" not in xcs._modifiers(first):
        return (), False
    type_node = first.child_by_field_name("type")
    if type_node is None:
        return (), True
    words = _WORD.findall(wc._text(type_node))
    type_parameters = wc._child(method, "type_parameter_list")
    generic = {
        n for t in (type_parameters.named_children if type_parameters is not None else ()) if (n := wc._name_of(t))
    }
    extended = tuple(w for w in words if w in repo_types and w not in generic)
    if words and words[0] in generic and len(words) == 1:
        return extended, True  # `this T`, `this T?`, `this T[]`
    if name not in _PATTERN_METHODS:
        return extended, False
    return extended, not _unambiguous(type_node, repo_types, repo_full, generic, lookup)


def _error_scopes(root: Node, source: str) -> list[str]:
    """The text of the nearest type declaration around every ERROR or
    MISSING node, the whole file's for one outside any type. Recovery moves
    declarations out of the ERROR node, so the node itself may spell
    nothing while a declaration it displaced was lost."""
    from flag_cleanup import wrapper_csharp as wc

    scopes: dict[int, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            scope = _enclosing_type(node)
            key = scope.id if scope is not None else -1
            if key not in scopes:
                scopes[key] = wc._text(scope) if scope is not None else source
        stack.extend(c for c in node.children if c.has_error or c.is_missing)
    return list(scopes.values())


def _headers(
    root: Node,
    source: str,
    repo_types: frozenset[str],
    repo_full: frozenset[str],
    lookup: _Binder,
) -> list[_Header]:
    from flag_cleanup import wrapper_csharp as wc

    found: list[_Header] = []
    if root.has_error:
        found.extend(_Header("opaque", text) for text in _error_scopes(root, source))
    for node in cross_file_keys.walk(root):
        if node.type == "using_directive":
            alias = node.child_by_field_name("name")
            if alias is None:
                continue
            target = next((c for c in reversed(node.named_children) if c.id != alias.id), None)
            if target is not None:
                simple = xcs._simple_name(target)
                name = wc._text(alias).removeprefix("@")
                found.append(_Header("alias", wc._text(target), name, target=simple))
            continue
        if node.type in _TYPE_DECLARATIONS:
            name = wc._name_of(node)
            if name is None or _private(node):
                continue
            found.append(_Header("type", _header_text(node), name.removeprefix("@")))
            parameters = wc._child(node, "parameter_list") if node.type in _RECORDS else None
            for parameter in parameters.named_children if parameters is not None else ():
                # A record's positional parameter is a public property.
                if parameter.type == "parameter" and (pname := wc._name_of(parameter)):
                    found.append(_Header("member", wc._text(parameter), pname.removeprefix("@"), name))
            continue
        if node.type not in _MEMBER_DECLARATIONS:
            continue
        holder = _enclosing_type(node)
        owner = wc._name_of(holder) if holder is not None else None
        owner = owner.removeprefix("@") if owner else None
        if node.type == "constructor_declaration" and (wc._name_of(node) or "").removeprefix("@") != owner:
            # Not a constructor: a shape this grammar misreads as one, such as
            # an `extension(...) { ... }` block, whose members it then reads
            # as local functions and statements.
            found.append(_Header("opaque", wc._text(node)))
            continue
        if _private(node) and not (node.type in _GENERATOR_INPUTS and wc._child(node, "attribute_list")):
            # A private member with an attribute may be a source generator's
            # input (`[ObservableProperty] private T _x` emits a public `X`),
            # and what it emits is not seen here: count it as public, so its
            # type taints by name.
            continue
        text = _header_text(node)
        if node.type in ("field_declaration", "event_field_declaration"):
            declaration = wc._child(node, "variable_declaration")
            for declarator in declaration.named_children if declaration is not None else ():
                if declarator.type == "variable_declarator" and (dname := wc._name_of(declarator)):
                    found.append(_Header("member", text, dname.removeprefix("@"), owner))
            continue
        name = wc._name_of(node) if node.type in _NAMED_BY_FIELD else None
        name = name.removeprefix("@") if name else None
        extends: tuple[str, ...] = ()
        if node.type == "method_declaration":
            extends, unreadable = _extension(node, name, repo_types, repo_full, lookup)
            if unreadable:
                found.append(_Header("opaque", text))
        found.append(_Header("member", text, name, owner, extends))
    return found


# --- project reachability -----------------------------------------------------

_REFERENCE_TAG = re.compile(r"<ProjectReference\b([^>]*)>", re.IGNORECASE)
_ASSEMBLY_TAG = re.compile(r"<Reference\b([^>]*)>", re.IGNORECASE)
_ANY_REFERENCE = re.compile(r"<(?:Project)?Reference\b", re.IGNORECASE)
_HINT_PATH = re.compile(r"<HintPath\s*>([^<]*)</HintPath\s*>", re.IGNORECASE)
_PATH_SHAPED = re.compile(r"[\\/]|\.(?:dll|exe)\s*$", re.IGNORECASE)
_COMPILE_TAG = re.compile(r"<Compile\b([^>]*)>", re.IGNORECASE)
_IMPORT_TAG = re.compile(r"<Import\b([^>]*)>", re.IGNORECASE)
_IMPORTED_ANYWHERE = (".props", ".targets")
_THIS_DIRECTORY = re.compile(r"\$\(MSBuildThisFileDirectory\)", re.IGNORECASE)
_PROJECT_DIRECTORY = re.compile(r"\$\(MSBuildProjectDirectory\)", re.IGNORECASE)
#: Properties naming the project being built. In a file a project imports
#: they name the importing project, which the file's own path does not tell.
_PROJECT_RELATIVE = re.compile(
    r"\$\(\s*(?:MSBuildProject\w*|ProjectDir|ProjectPath|ProjectName|ProjectFileName|ProjectExt)\s*\)",
    re.IGNORECASE,
)
_EXISTS_GUARD = re.compile(r"""\s*Exists\(\s*(?:'([^']*)'|"([^"]*)")\s*\)\s*""", re.IGNORECASE)


def _attributes(tag: str) -> dict[str, str]:
    return {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in xcs._ATTRIBUTE.finditer(tag)
    }


def _paths(value: str) -> list[str]:
    return [p.strip() for p in value.split(";") if p.strip()]


def _relative(value: str, base: Path) -> Path | None:
    """``value`` resolved against ``base``; None when it is not a plain path."""
    value = _THIS_DIRECTORY.sub(lambda _m: str(base) + "/", value)
    value = _PROJECT_DIRECTORY.sub(lambda _m: str(base), value)
    if any(c in value for c in "$@%*?"):
        return None
    return (base / value.replace("\\", "/")).resolve()


def _compiles_outside(text: str, project: Path | None) -> bool:
    """Whether a ``<Compile Include>`` may bring in a file from outside the
    project's directory. ``project`` is None for a file any project may
    import, whose items resolve against the importing project: only a plain
    relative path with no ``..`` stays inside it."""
    for tag in _COMPILE_TAG.finditer(text):
        include = _attributes(tag.group(1)).get("include")
        if include is None:
            continue  # `Remove` or `Update`: never adds a file
        for value in _paths(include):
            if project is None:
                plain = value.replace("\\", "/")
                if (
                    any(c in plain for c in "$@%")
                    or plain.startswith("/")
                    or re.match(r"[A-Za-z]:", plain)
                    or ".." in plain.split("/")
                ):
                    return True
                continue
            resolved = _relative(value.replace("*", "x").replace("?", "x"), project)
            if resolved is None or not cross_file_keys.within(resolved, project):
                return True
    return False


_ASSEMBLY_NAME = re.compile(r"<AssemblyName\b[^>]*>([^<]*)</AssemblyName\s*>", re.IGNORECASE)
_OUTPUT_DIRECTORIES = frozenset({"bin", "obj"})


@dataclass(frozen=True)
class _Assemblies:
    """What the checkout's own projects build."""

    #: Lowercased assembly names: every ``.csproj`` stem and every
    #: ``<AssemblyName>`` (both are kept: either may be the one in force).
    names: frozenset[str]
    #: An ``<AssemblyName>`` computed from a property: any name may be one.
    any_name: bool
    projects: frozenset[Path]


def _assemblies(projects: set[Path], texts: Iterable[str]) -> _Assemblies:
    """What ``projects`` build, named by their stems and by every
    ``<AssemblyName>`` in any MSBuild file they reach (``texts``)."""
    names = {f.stem.lower() for f in projects}
    any_name = False
    for text in texts:
        for match in _ASSEMBLY_NAME.finditer(text):
            value = match.group(1).strip()
            if "$(" in value or "@(" in value:
                any_name = True
            elif value:
                names.add(value.lower())
    return _Assemblies(frozenset(names), any_name, frozenset(f.parent for f in projects))


def _msbuild_files(
    listed: list[Path], anywhere: list[Path], closure_only: bool = False
) -> tuple[set[Path], dict[Path, str], set[Path]] | None:
    """``(projects, texts, user files)``: every project reachable from
    ``listed`` by ``<ProjectReference>`` (of any language: a VB project can
    reference a family project and carry it on), and the text of every MSBuild
    file reached from them and from ``anywhere`` (the checkout's
    ``.props``/``.targets``, any project may import one) through ``<Import>``,
    transitively, ``<project>.user`` files included. None when a file cannot
    be read, a reference cannot be read, or an import other than a
    ``.props``/``.targets`` cannot be resolved (one that is a toolchain or
    package file; the checkout's own are in ``anywhere``). Also None for an
    import that resolves to no file, unless :func:`_guarded` by its
    existence, and for an import in any file but a project or its ``.user``
    file whose path names the project being built
    (``$(MSBuildProjectDirectory)``, ``$(ProjectDir)`` and the like): there
    it is the importing project's, which may be any project.

    ``closure_only``: the import closure of ``listed`` and ``anywhere`` alone,
    for a question about those projects themselves: no ``<ProjectReference>``
    is followed, and an import that cannot be resolved is None even when it
    names a ``.props`` or ``.targets`` file."""
    from flag_cleanup import wrapper_csharp as wc

    projects: set[Path] = set()
    users: set[Path] = set()
    texts: dict[Path, str] = {}
    pending = [(f, True) for f in listed] + [(f, False) for f in anywhere]
    while pending:
        path, is_project = pending.pop()
        if path in texts and (not is_project or path in projects):
            continue
        text = texts.get(path) if path in texts else wc._read(str(path))
        if text is None:
            return None
        texts[path] = text
        if is_project:
            projects.add(path)
            targets = [] if closure_only else _references(text, path.parent)
            if targets is None:
                return None
            pending.extend((t, True) for t in targets)
            # MSBuild imports `<project file>.user` beside every project.
            user = path.with_name(path.name + ".user")
            if user.exists():
                users.add(user)
                pending.append((user, False))
        # Only a project, and the `.user` file beside it, knows which
        # project is being built.
        knows_project = is_project or path in users
        for tag in _IMPORT_TAG.finditer(text):
            attributes = _attributes(tag.group(1))
            project = attributes.get("project")
            if project is None:
                return None
            if "sdk" in attributes:
                continue  # resolved in the named SDK, as `<Project Sdk>` is
            for value in _paths(project):
                if not knows_project and _PROJECT_RELATIVE.search(value):
                    return None  # the importing project's path: any project's
                resolved = _relative(value, path.parent)
                if resolved is None:
                    if value.lower().endswith(_IMPORTED_ANYWHERE) and not closure_only:
                        continue
                    return None
                found = _existing(resolved)
                if found is None:
                    return None  # several files match it ignoring case
                if found:
                    pending.append((found[0], False))
                elif not _guarded(attributes.get("condition"), resolved, path.parent, knows_project):
                    return None  # an import this cannot see may hold anything
    return projects, texts, users


def _existing(path: Path) -> list[Path] | None:
    """``[path]`` when it exists, else the one file whose path matches it
    ignoring case (``[]`` when none does; ``None`` when several do). MSBuild
    on Windows opens that file for an ``<Import>``, and its ``Exists()`` is
    true for it there, so a build on Windows imports what a case-sensitive
    look here would miss."""
    if path.exists():
        return [path]
    base = path
    while not base.exists() and base != base.parent:
        base = base.parent
    candidates = [base]
    for part in path.relative_to(base).parts:
        following: list[Path] = []
        for candidate in candidates:
            try:
                following += [c for c in candidate.iterdir() if c.name.lower() == part.lower()]
            except OSError:
                continue
        candidates = following
    files = [c for c in candidates if c.is_file()]
    return files if len(files) <= 1 else None


def _guarded(condition: str | None, resolved: Path, base: Path, knows_project: bool) -> bool:
    """Whether an ``<Import>`` of the missing file ``resolved`` is skipped
    by its ``Condition``: only a lone ``Exists(...)`` of that same path is
    read. Any other condition (a negation, a conjunction, a property test)
    may hold, and MSBuild then imports a file this never saw or fails. The
    paths are compared ignoring case, as Windows compares them."""
    match = _EXISTS_GUARD.fullmatch(condition) if condition is not None else None
    if match is None:
        return False
    guard = match.group(1) if match.group(1) is not None else match.group(2)
    if not knows_project and _PROJECT_RELATIVE.search(guard):
        return False
    checked = _relative(guard, base)
    return checked is not None and str(checked).lower() == str(resolved).lower()


def _in_project(directory: Path, built: _Assemblies, root: Path) -> bool:
    """Whether ``directory`` is in a project of the checkout: a known one, or
    any directory up to the root holding a project file of any language (the
    source listing holds only ``.csproj``)."""
    if any(cross_file_keys.within(directory, d) for d in built.projects):
        return True
    for current in (directory, *directory.parents):
        if not cross_file_keys.within(current, root):
            return False
        try:
            if any(f.is_file() for f in current.glob("*.*proj")):
                return True
        except OSError:
            return True
    return False


def _assembly_inside(text: str, project: Path | None, root: Path, built: _Assemblies) -> bool:
    """Whether a ``<Reference>`` may name an assembly a project of the
    checkout builds, which a family project can be.

    So it may when its ``HintPath`` or path-like ``Include`` ends in a file
    named for such an assembly (``Company.Flags.dll``) and does not resolve
    outside the checkout, or runs through a ``bin``/``obj`` directory of a
    project; and when its ``Include`` is such an assembly's name with no path,
    which MSBuild may resolve from an output directory. A package's assembly
    (``packages\\Newtonsoft.Json\\lib\\Newtonsoft.Json.dll``) does not. In a
    file any project may import (``project`` None) a relative path cannot be
    resolved, so any ``bin`` or ``obj`` segment counts."""
    values = [m.group(1).strip() for m in _HINT_PATH.finditer(text)]
    for tag in _ASSEMBLY_TAG.finditer(text):
        include = _attributes(tag.group(1)).get("include")
        for value in _paths(include) if include is not None else ():
            if any(m in value for m in ("$(", "@(", "%(")):
                return True  # a value this cannot read
            if _PATH_SHAPED.search(value):
                values.append(value)
            elif built.any_name or value.split(",", 1)[0].strip().lower() in built.names:
                return True
    for value in values:
        plain = value.replace("\\", "/")
        last = plain.rsplit("/", 1)[-1]
        if "$(" in last or "@(" in last or "%(" in last:
            return True  # the file's own name is not readable
        if built.any_name or Path(last).stem.lower() in built.names:
            return True  # wherever it lives: an output path can leave the checkout
        resolved = _relative(value, project) if project is not None else None
        if resolved is None:
            if any(seg.lower() in _OUTPUT_DIRECTORIES for seg in plain.split("/")):
                return True
            continue
        if not cross_file_keys.within(resolved, root):
            continue
        if any(
            out.name.lower() in _OUTPUT_DIRECTORIES and _in_project(out.parent, built, root)
            for out in resolved.parents
        ):
            return True
    return False


def _references(text: str, base: Path) -> list[Path] | None:
    """The project files ``text``'s ``<ProjectReference>`` items name; None
    when one cannot be read exactly."""
    found: list[Path] = []
    for tag in _REFERENCE_TAG.finditer(text):
        attributes = _attributes(tag.group(1))
        include = attributes.get("include")
        if include is None:
            if "remove" in attributes or "update" in attributes:
                continue  # never adds a reference
            return None
        for value in _paths(include):
            resolved = _relative(value, base)
            if resolved is None or not resolved.is_file():
                return None
            found.append(resolved)
    return found


_GRAPHS: dict[str, dict[Path, frozenset[Path]] | None] = {}


def project_graph(repo_dir: str) -> dict[Path, frozenset[Path]] | None:
    """project directory -> directories it references; ``None`` when the
    graph cannot be trusted, and everything is then reachable.

    ``None`` when some reference cannot be read (a property or a wildcard in
    its path, a missing or unreadable file), when a ``<ProjectReference>``
    comes from anywhere but a project file (any ``.props`` or ``.targets`` in
    the checkout, ``Directory.Build`` ones included, or another file a project
    imports, a ``<project>.user`` file included), when a ``<Reference>`` may
    name an assembly built in the checkout (see :func:`_assembly_inside`), or
    when a ``<Compile Include>`` brings in a file from outside a project's
    directory, so that a file's directory no longer says which
    projects compile it. A referenced project of another language is followed
    too, since references are transitive.

    Cached per ``repo_dir`` for the run: project files do not change while it
    runs. :func:`reset_cache` is for tests that rewrite them.
    """
    if repo_dir not in _GRAPHS:
        _GRAPHS[repo_dir] = _graph(repo_dir)
    return _GRAPHS[repo_dir]


def _graph(repo_dir: str) -> dict[Path, frozenset[Path]] | None:
    from flag_cleanup import source_index

    root = Path(source_index.checkout_root(repo_dir)).resolve()
    listed = [
        Path(f.resolved)
        for f in source_index.files(repo_dir, {".csproj"}, with_ignored=True)
        if f.path.name.endswith(".csproj")
    ]
    anywhere = [Path(f.resolved) for f in source_index.files(repo_dir, set(_IMPORTED_ANYWHERE), with_ignored=True)]
    reached = _msbuild_files(listed, anywhere)
    if reached is None:
        return None
    projects, texts, users = reached
    built = _assemblies(projects, texts.values())
    for path, text in texts.items():
        if path in users:
            # Per developer and usually untracked: any reference at all counts.
            if _ANY_REFERENCE.search(text) or _compiles_outside(text, path.parent):
                return None
        elif path not in projects and (
            _REFERENCE_TAG.search(text)
            or _assembly_inside(text, None, root, built)
            or _compiles_outside(text, None)
        ):
            return None  # a file any project may import
    graph: dict[Path, set[Path]] = {}
    for project in projects:
        text = texts[project]
        directory = project.parent
        targets = _references(text, directory) or []
        if _compiles_outside(text, directory) or _assembly_inside(text, directory, root, built):
            return None
        graph.setdefault(directory, set()).update(t.parent for t in targets)
    return {k: frozenset(v) for k, v in graph.items()}


#: ``extension(`` or ``extension<T>(``: the header of a C# 14 extension block.
#: A method named ``extension`` matches too, which only errs toward True.
_EXTENSION_BLOCK = re.compile(r"\bextension\s*(?:<[^<>{};]*>\s*)?\(")
#: repo_dir -> (an extension block, a `dynamic`) somewhere in its C# files.
_SCANS: dict[str, tuple[bool, bool]] = {}


#: Markup that compiles to C# members other files can call: a ``dynamic``
#: there is as much a ``dynamic`` as one in a ``.cs`` file.
_DYNAMIC_HOSTS = (".razor", ".cshtml")


def _scan(repo_dir: str) -> tuple[bool, bool]:
    """One read of every C# file of the checkout, and of the Razor files for
    ``dynamic``, ignored ones included; an unreadable file counts as both.
    Cached per ``repo_dir``, like the graph."""
    if repo_dir not in _SCANS:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        extension = dynamic = False
        for f in source_index.files(repo_dir, {".cs", *_DYNAMIC_HOSTS}, with_ignored=True):
            csharp = f.path.name.endswith(".cs")
            if not csharp and not f.path.name.lower().endswith(_DYNAMIC_HOSTS):
                continue
            text = wc._read(f.resolved)
            if csharp:
                extension |= text is None or _EXTENSION_BLOCK.search(text) is not None
            dynamic |= text is None or _DYNAMIC.search(text) is not None
            if extension and dynamic:
                break
        _SCANS[repo_dir] = (extension, dynamic)
    return _SCANS[repo_dir]


def declares_extension_blocks(repo_dir: str) -> bool:
    """Whether any C# file of the checkout may declare an extension block."""
    return _scan(repo_dir)[0]


def spells_dynamic(repo_dir: str) -> bool:
    """Whether any C# or Razor file of the checkout spells ``dynamic``,
    comments, strings and markup included (which only errs toward True)."""
    return _scan(repo_dir)[1]


#: Names of the APIs that take a member's name as an argument and reach that
#: member on any value. ``GetType()`` alone is not one: it takes no name, and
#: ``GetType().Name`` is everywhere. Other by-name APIs -- Dynamic LINQ
#: strings, a JSON object's indexer, ``Activator``, a project's own helpers
#: -- are not seen at all: a known limit of judging strings.
_NAME_TAKING = frozenset(
    {"GetProperty", "GetMethod", "GetField", "GetMember", "GetRuntimeProperty", "InvokeMember"}
)
#: Types whose lookups take a name through an indexer or a ``Find`` this
#: cannot tie to a call: spelling one is enough.
_LOOKUP_TYPES = frozenset({"TypeDescriptor", "PropertyDescriptor"})
#: The NAME argument of each route: its position among the positional
#: arguments, and the parameter names it may be passed by.
_NAME_ARGUMENT: dict[str, tuple[int, frozenset[str]]] = {
    **{name: (0, frozenset({"name"})) for name in _NAME_TAKING},
    "Property": (1, frozenset({"propertyName"})),
    "PropertyOrField": (1, frozenset({"propertyOrFieldName"})),
    "Eval": (1, frozenset({"expression"})),
    "GetPropertyValue": (1, frozenset({"propName"})),
    "Binding": (0, frozenset({"path"})),
}
#: Types whose members or constructor are routes: an alias or a ``using
#: static`` of one hides the calls from :func:`_computed_route`, so either
#: directive is a route by itself.
_ROUTE_TYPES = frozenset({"Expression", "DataBinder", "Binding", *_LOOKUP_TYPES})
#: A file worth parsing for :func:`_computed_route`.
_ROUTE_TEXT = re.compile(
    r"\b(?:GetProperty|GetMethod|GetField|GetMember|GetRuntimeProperty|InvokeMember"
    r"|TypeDescriptor|PropertyDescriptor|DataBinder|Binding|Property|PropertyOrField|Expression)\b"
)
#: A route word in a region that does not parse.
_ROUTE_IN_ERROR = re.compile(
    r"\b(?:GetProperty|GetMethod|GetField|GetMember|GetRuntimeProperty|InvokeMember"
    r"|TypeDescriptor|PropertyDescriptor|DataBinder|Binding|PropertyOrField)\b|\bExpression\s*\.\s*Property\b"
)
_STRING_LITERALS = frozenset({"string_literal", "verbatim_string_literal", "raw_string_literal"})
#: repo_dir -> the C# files (resolved) holding a by-name route whose name is
#: not a string literal; see :func:`computed_name_route`.
_ROUTES: dict[str, tuple[str, ...]] = {}
#: In :data:`_ROUTES`: a route that may reach every project.
_ANY_PROJECT = ""
#: ``(repo_dir, family)`` -> :func:`computed_name_route`'s answer.
_ROUTED: dict[tuple[str, "Family"], bool] = {}


def _literal_name(call: Node | None, route: str) -> bool:
    """Whether a call passes ``route``'s NAME argument as a plain string
    literal: the one named by a parameter name it may take, else the one
    at its position among the positional arguments. A literal anywhere else
    (a format, a binder flag) says nothing; a name that cannot be placed --
    no argument list, too few arguments, an unknown named one -- is
    computed."""
    arguments = call.child_by_field_name("arguments") if call is not None else None
    if arguments is None:
        return False
    position, names = _NAME_ARGUMENT[route]
    positional: list[Node] = []
    for argument in arguments.named_children:
        if argument.type != "argument":
            continue
        label = argument.child_by_field_name("name")
        values = [c for c in argument.named_children if label is None or c.id != label.id]
        value = values[-1] if values else None
        if label is not None:
            if xcs._text(label).removeprefix("@") in names:
                return value is not None and value.type in _STRING_LITERALS
            continue
        positional.append(value)
    if len(positional) <= position:
        return False
    value = positional[position]
    return value is not None and value.type in _STRING_LITERALS


def _route_directive(node: Node) -> bool:
    """Whether a ``using`` alias or ``using static`` names a route type (by
    its last segment); one whose target cannot be read counts if its text
    spells one."""
    using = xcs._using(node)
    if using.kind not in ("alias", "static"):
        return False
    if using.target is None:
        return any(re.search(rf"\b{t}\b", xcs._text(node)) for t in _ROUTE_TYPES)
    return using.target.rsplit(".", 1)[-1] in _ROUTE_TYPES


def _computed_route(root: Node) -> bool:
    """Whether a parsed file holds a by-name route whose name is computed:
    any use of a name-taking method that is not a call passing its name as
    a literal (a method group counts); ``Expression.Property`` or
    ``PropertyOrField``, a ``DataBinder`` member or ``new Binding(...)``
    likewise; any use of a descriptor lookup type; an alias or ``using
    static`` of a route type; or a route spelled where the file does not
    parse."""
    from flag_cleanup import wrapper_csharp as wc

    for node in cross_file_keys.walk(root):
        if node.type == "ERROR" or node.is_missing:
            if _ROUTE_IN_ERROR.search(wc._text(node)):
                return True
            continue
        if node.type == "using_directive":
            if _route_directive(node):
                return True
            continue
        if node.type == "object_creation_expression":
            created = node.child_by_field_name("type")
            if created is not None and xcs._simple_name(created) == "Binding" and not _literal_name(node, "Binding"):
                return True
            continue
        if node.type != "identifier":
            continue
        name = wc._text(node).removeprefix("@")
        parent = node.parent
        if parent is None or (parent.type in _NAMED_DECLARATIONS and xcs._field(parent, node) == "name"):
            continue  # a declaration of the name, not a use
        if parent.type in ("using_directive", "qualified_name") and any(
            a.type == "using_directive" for a in xcs._ancestors(node)
        ):
            continue  # a directive is judged whole, above
        if name in _LOOKUP_TYPES:
            return True
        access = parent if parent.type == "member_access_expression" and xcs._field(parent, node) == "name" else None
        target = access if access is not None else node
        call = target.parent if target.parent is not None and target.parent.type == "invocation_expression" else None
        if call is not None and xcs._field(call, target) != "function":
            call = None
        if name in _NAME_TAKING:
            if not _literal_name(call, name):
                return True
        elif name in ("Property", "PropertyOrField") and access is not None:
            receiver = access.child_by_field_name("expression")
            spelled = wc._text(receiver) if receiver is not None else ""
            if (spelled == "Expression" or spelled.endswith(".Expression")) and not _literal_name(call, name):
                return True
        elif name == "DataBinder":
            # `DataBinder.M(...)` or `System.Web.UI.DataBinder.M(...)`.
            holder = access if access is not None else node
            member_access = holder.parent
            if member_access is None or member_access.type != "member_access_expression" or (
                xcs._field(member_access, holder) != "expression"
            ):
                return True
            member = member_access.child_by_field_name("name")
            method = wc._text(member) if member is not None else ""
            outer = member_access.parent
            if (
                method not in ("Eval", "GetPropertyValue")
                or outer is None
                or outer.type != "invocation_expression"
                or not _literal_name(outer, method)
            ):
                return True
    return False


#: Declarations whose ``name`` field only names something.
_NAMED_DECLARATIONS = frozenset(
    {
        "method_declaration",
        "local_function_statement",
        "property_declaration",
        "variable_declarator",
        "parameter",
        "class_declaration",
        "struct_declaration",
        "interface_declaration",
        "record_declaration",
    }
)


def _route_files(repo_dir: str) -> tuple[str, ...]:
    """Every C# file of the checkout, ignored ones included, holding a
    computed-name route; an unreadable one counts. Read once per
    ``repo_dir``, like the scan."""
    if repo_dir not in _ROUTES:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        found: list[str] = []
        for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
            if not f.path.name.endswith(".cs"):
                continue
            text = wc._read(f.resolved)
            if text is None:
                found.append(f.resolved)
            elif _ROUTE_TEXT.search(text) and _computed_route(wc.parse(text)):
                found.append(f.resolved)
        # A project `<Using>` item is a `global using` for its project's
        # files. One in a `.props`/`.targets` file may reach any project, as
        # may an item this cannot read.
        items, readable = xcs.msbuild_usings(repo_dir)
        if not readable:
            found.append(_ANY_PROJECT)
        for item in items:
            if item.kind in ("alias", "static") and (item.target or "").rsplit(".", 1)[-1] in _ROUTE_TYPES:
                found.append(item.path if item.path.endswith(".csproj") else _ANY_PROJECT)
        _ROUTES[repo_dir] = tuple(found)
    return _ROUTES[repo_dir]


def computed_name_route(repo_dir: str, family: Family) -> bool:
    """Whether a file that may hold a family value reaches members by a
    name it does not spell as a literal: then a string anywhere the family
    can be may be that name, wherever it is written (a constants class,
    another part of a ``partial`` type)."""
    key = (repo_dir, family)
    if key not in _ROUTED:
        _ROUTED[key] = any(
            path == _ANY_PROJECT or reaches(repo_dir, path, family) for path in _route_files(repo_dir)
        )
    return _ROUTED[key]


def reset_cache() -> None:
    """For tests that rebuild a tree under the same path."""
    _GRAPHS.clear()
    _SCANS.clear()
    _ROUTES.clear()
    _ROUTED.clear()


def _enclosing(path: str, projects: frozenset[Path]) -> list[Path]:
    """Every project whose directory holds ``path``: an SDK project compiles
    every ``.cs`` file below it, a nested project's included, unless told
    otherwise, which is not read here."""
    return [parent for parent in Path(path).parents if parent in projects]


#: A file-based app's directive (``#:project ../App/App.csproj``,
#: ``#:package Lib@1.0``): ``dotnet run app.cs`` builds the file as a project
#: of its own, which no project file names. Matched on any line, which only
#: errs toward a file-based app.
_APP_DIRECTIVE = re.compile(r"^[ \t]*#:", re.MULTILINE)


def _file_based_app(path: str) -> bool:
    """Whether ``path`` holds a file-based app's directive. A path with no
    file to read is not one: every caller has read the file already, and
    keeps the wrapper itself when it cannot."""
    from flag_cleanup import wrapper_csharp as wc

    source = wc._read(path)
    return source is not None and _APP_DIRECTIVE.search(source) is not None


def reaches(repo_dir: str, path: str, family: Family) -> bool:
    """Whether the file ``path`` may hold a value of a family type: its
    project reaches a family project, or it is a file-based app, whose
    references no project file says."""
    if family.homeless:
        return True
    graph = project_graph(repo_dir)
    if graph is None:
        return True
    pending = _enclosing(str(Path(path).resolve()), xcs._project_dirs(repo_dir))
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in family.projects:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return _file_based_app(path)


# --- family-free files --------------------------------------------------------


def family_free(source: str, root: Node, family: Family) -> bool:
    """Whether nothing in the file can introduce a value of a family type.

    Judged on the raw text, comments and strings included, which can only
    over-taint; ``root`` is the caller's parse of ``source``, unused here. A
    file-based app is never family-free: a ``#:package`` may bring in
    anything.
    """
    del root
    if family.opaque or _DYNAMIC.search(source) or _APP_DIRECTIVE.search(source):
        return False
    if _spells(source, family.simple_names | family.tainting | family.returning):
        return False
    return not (family.guest_named and _PARTIAL.search(source))
