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
    A name only static members add (``Protectors.Create()``, no extension
    method) is reached through its owner, a tainting name already, so proof
    2 does not count it in a file without a ``using static``; inside the
    owner's own declaration its header puts the name back.

  ``dynamic`` is collected from the start, as a type that can hold a family
  value: ``var x = D.Get(); x.IsEnabled`` names no family type when ``Get``
  returns ``dynamic``, and the read binds to the wrapper's member at run
  time. So a member typed ``dynamic`` taints its name and its owner, like
  one returning the interface. It is never a name in the result: a file that
  spells it is not family-free anyway.

  This is a fixpoint, because a value can come through any chain of such
  types: ``o.Shelf()[0].IsEnabled`` names no family type when ``Shelf``
  returns a ``Bag`` whose indexer returns the interface. A parameter type
  counts as much as a return type: ``Run(f => f.IsEnabled)`` types ``f``
  from ``Run``'s parameter, and ``Take(new(...))`` builds a value of the
  parameter's type without naming it. A type taints by its name as well as
  its members' names, since an indexer, an operator, a conversion and the
  pattern methods ``foreach``, ``await``, deconstruction and collection
  initializers call are used without spelling any name.

  Proof 2 alone (:func:`family_free`, not the reflection bounds) leaves out
  a constructor's by-value parameter whose type is the family's interface,
  spelled plainly (``IGate``, ``Ns.IGate``, ``IGate?``): it takes a family
  value in and hands none back. A caller has to hold the value already,
  which taints the caller by itself; a constructor is never a method group,
  so its parameter types never reach a generic inference; and an interface
  cannot be built by a target-typed ``new(...)`` whose initializer might set
  the member. So ``new Job(gate)`` is no evidence against a file that names
  ``Job`` when that constructor is all ``Job`` spells. A method's parameter
  still counts (``var a = job.Use;`` is an ``Action<IGate>``), as do
  ``ref``/``out``/``in``/``params``, a generic or delegate type (a lambda's
  parameter takes it), a family class (``new(...) { IsEnabled = ... }``),
  and an interface with attributes (``[CoClass]`` makes ``new IGate()``
  legal).

  The taint reads the checkout's headers only, so one assumption is taken:
  a library member does not hand a family value back as ``dynamic``. It
  fails where a library member typed ``dynamic`` is reached unspelled, and
  that is an inherited one: MVC's ``Controller.ViewBag`` is a slot another
  file may store the gate in (``ViewBag.Gate = _gate``), read back as
  ``ViewBag.Gate.IsEnabled`` in a file that names nothing. Such a member is
  reached unspelled only inside a type that inherits it, so a read inside a
  type with a library class up its base chain, through the checkout's own
  bases, is judged even in a family-free file (:func:`exposed_spans`) when
  its receiver may start at an inherited member. A
  base spelled like an interface (``I`` and a capital) is taken as one,
  whose members a class does not inherit unspelled. A library member reached
  through a name the file spells is not seen: a static ``dynamic`` property
  of a library type, or ``ViewBag`` read off an instance of a repository
  controller from outside it. Nor are strings and crefs: in a family-free
  file a string counts only as a by-name route's own name argument, whatever
  value the route is given.

  A private member is visible only inside its own type, so it is left out,
  except in a ``partial`` type, whose other parts may live in other files,
  and except a field, property or method with an attribute, which a source
  generator may turn into a public member of another name.

  A pattern extension method (``GetEnumerator``, ``Deconstruct``, ``Add``,
  ...) on a type outside the repository, or any extension of a type
  parameter, can hand a family value to code that spells no name, so it
  makes no file family-free; so does a declaration this cannot read, and so
  does a ``global using static`` (or a project ``<Using Static="true">``)
  spelling a collected name, which brings that type's members into every file
  of its project by their bare names.

Everything that cannot be read errs toward reachable and tainted.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_csharp as xcs
from flag_cleanup import cross_file_keys

_DYNAMIC = re.compile(r"\bdynamic\b")
#: Collected from the start of the taint fixpoint, never returned by it.
_DYNAMIC_NAME = "dynamic"
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
    #: or reaching type's name.
    returning: frozenset[str]
    #: Full names of the siblings alone.
    sibling_full_names: frozenset[str] = frozenset()
    #: Names of the other types (and aliases) a value of a family type can be
    #: reached through without spelling a family name, reflection included:
    #: a route bound to one of them may reach a family member.
    tainting: frozenset[str] = frozenset()
    #: The same, as :func:`family_free` judges a file: without types that take
    #: a family value only through a constructor. ``None`` means ``tainting``.
    reaching: frozenset[str] | None = None
    #: ``(name, owner)`` for each name of :attr:`returning` that only static
    #: members add (``TestProtectors.Create()``): reached through the owner's
    #: name, a reaching one, except inside the owner or under a ``using
    #: static`` (see :func:`family_free`).
    static_returning: frozenset[tuple[str, str]] = frozenset()
    #: Some family type is declared outside every project, or the wrapper's
    #: own class was not found, so project reachability proves nothing.
    homeless: bool = False
    #: Something that may bring a family value in without a name could not be
    #: read: an unreadable C# file or project ``<Using>`` item, a type with a
    #: parse error, a declaration the grammar misreads, an ``extension``
    #: block, or a pattern extension method on a type outside the
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
    #: No C# file that could not be read, or does not parse whole, may
    #: declare a type under the name of a family type or of any supertype up
    #: their chains (:func:`~flag_cleanup.cross_file_csharp._declarable_words`),
    #: so ``members`` holds every member they declare. A file that fails to
    #: parse elsewhere does not matter here.
    members_complete: bool = False
    #: Some C# or Razor file of the checkout spells ``dynamic`` (or could
    #: not be read): a ``var`` may then hold a value of any type through a
    #: member of another file, so a member name is no evidence of a type.
    dynamic_anywhere: bool = True
    #: Some sibling has a base, at any depth, outside the repository (or
    #: could not be read): ``members`` may then miss what that base declares,
    #: so a member name is no evidence of a type.
    sibling_bases_external: bool = True
    #: Whether a lookup of the member by name can tell a deleted member from
    #: an attribute-less one only by its attributes: no deleted declaration
    #: carries an attribute or is ``virtual``/``abstract``/``override``/
    #: ``new``, no type up a family class's chain declares the name (or is
    #: unknown), and nothing derives from a family class. See
    #: :func:`_attribute_only`.
    attributes_unobservable: bool = False
    #: Simple names of the checkout's types, and of those among them with a
    #: library class up their base chains (:func:`_library_based`). A base
    #: in neither reads as a library class unless it is spelled like an
    #: interface.
    repo_types: frozenset[str] = frozenset()
    library_based: frozenset[str] = frozenset()
    #: Full names of the checkout's types: a qualified base spelling none of
    #: them names a library class, whatever its simple name.
    repo_full_types: frozenset[str] = frozenset()


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
    sibling_full = frozenset(_full(s.namespace, s.type_chain) for s in wrapper.siblings)
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
    walked = set(simple)
    pending = list(supertypes)
    while pending:
        name = pending.pop()
        if name not in walked:
            walked.add(name)
            pending.extend(s for info in checkout.by_last.get(name, ()) for s in info.supertypes)
    projects_all = xcs._project_dirs(repo_dir)
    homes = [_enclosing(t.path, projects_all, repo_dir) for t in decls]
    inbound = _inbound(wrapper.interface, checkout)
    taint = _taint(repo_dir, simple, frozenset(checkout.by_last), frozenset(checkout.by_full), inbound)
    return Family(
        full_names=full,
        simple_names=taint.family,
        members=frozenset(members),
        members_complete=not checkout.unreadable
        and not any(words is None or name in words for name in walked for words in checkout.unparsed_types),
        settable=any(_settable(t.path, t.start, t.end, wrapper.name) for t in decls),
        projects=frozenset(p for home in homes for p in home),
        returning=taint.returning,
        static_returning=taint.static_returning,
        sibling_full_names=sibling_full,
        tainting=taint.tainting,
        reaching=taint.reaching,
        homeless=lost or any(not home for home in homes),
        opaque=taint.opaque,
        guest_named=taint.guest_named,
        extension_blocks=declares_extension_blocks(repo_dir),
        dynamic_anywhere=spells_dynamic(repo_dir),
        sibling_bases_external=_sibling_bases_external(repo_dir, wrapper.siblings),
        attributes_unobservable=_attributes_unobservable(wrapper, checkout, full),
        repo_types=frozenset(checkout.by_last),
        library_based=library_based(checkout),
        repo_full_types=frozenset(checkout.by_full),
    )


def library_based(checkout) -> frozenset[str]:
    """Simple names of the checkout's types that may inherit a library
    class's members: a base the checkout does not declare and that is not
    spelled like an interface, at any depth through its own types. A type a
    file that does not parse whole may declare is one, since its base list
    may be lost."""
    memo: dict[str, bool] = {}

    def based(name: str, seen: frozenset[str]) -> bool:
        if name in memo:
            return memo[name]
        infos = checkout.by_last.get(name, ())
        if not infos:
            found = _INTERFACE_NAME.match(name) is None
        elif any(words is None or name in words for words in checkout.unparsed_types):
            found = True
        elif name in seen:
            return False  # a cycle does not compile
        else:
            found = any(based(s, seen | {name}) for info in infos for s in info.supertypes)
        memo[name] = found
        return found

    return frozenset(name for name in checkout.by_last if based(name, frozenset()))


#: A type header with a base list, as text: for a file that does not parse
#: whole, whose base lists may be lost. A constraint (``where T : new()``)
#: matches too, which only errs toward based.
_BASED_HEADER = re.compile(r"\b(?:class|struct|record)\s+@?[^\W\d]\w*[^{;=]*?:")


def _inherits_library(node: Node, family: Family) -> bool:
    """Whether the type declaration ``node`` may inherit a library class's
    members (see :attr:`Family.library_based`)."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import wrapper_csharp as wc

    if node.type == "interface_declaration":
        return False
    # Another part of a partial type may be the one with the base.
    if (wc._name_of(node) or "").removeprefix("@") in family.library_based:
        return True
    bases = next((c for c in node.named_children if c.type == "base_list"), None)
    for child in bases.named_children if bases is not None else ():
        target = (
            child.named_children[0]
            if child.type == "primary_constructor_base_type" and child.named_children
            else child
        )
        base = xcs._simple_name(target)
        if base is None:
            continue
        if base in family.library_based:
            return True
        # Spelled qualified, a base is the checkout's only under a full name
        # of the checkout's ending so: `Microsoft.AspNetCore.Mvc.Controller`
        # is no `enum Controller` the checkout declares.
        dotted = xcs._dotted(target) if target.type in ("qualified_name", "alias_qualified_name") else None
        ours = base in family.repo_types and (
            dotted is None or any(f == dotted or f.endswith("." + dotted) for f in family.repo_full_types)
        )
        if not ours and _INTERFACE_NAME.match(base) is None:
            return True
    return False


def exposed_spans(source: str, root: Node, family: Family) -> list[tuple[int, int]]:
    """The byte spans of the file's types that may inherit a library class's
    members, nested ones inside them included: there an inherited member
    typed ``dynamic`` (MVC's ``ViewBag``) is reached unspelled, and may hold
    a family value another file stored. The whole file when it does not
    parse whole and some type header has a base list, or it spells a type
    another file may give a library base (a ``partial`` part)."""
    if root.has_error:
        based = _BASED_HEADER.search(source) or _spells(source, family.library_based)
        return [(0, len(source.encode("utf-8")))] if based else []
    spans: list[tuple[int, int]] = []
    for node in cross_file_keys.walk(root):
        if node.type in _TYPE_DECLARATIONS and _inherits_library(node, family):
            if not any(start <= node.start_byte < end for start, end in spans):
                spans.append((node.start_byte, node.end_byte))
    return spans


#: Modifiers under which a deleted member's lookup may differ from an
#: attribute-less member's: another declaration of the name is in play.
_HIDING = frozenset({"virtual", "abstract", "override", "new", "extern", "partial"})


def _attributes_unobservable(wrapper, checkout, full: frozenset[str]) -> bool:
    """See :attr:`Family.attributes_unobservable`. Anything unreadable, a
    partial type, a base outside the repository and a name an unparsed file
    may declare all answer no."""
    from flag_cleanup import wrapper_csharp as wc

    # Found by its type, not its byte span: close-out asks again after the
    # run has rewritten these files, and every span may have moved.
    owners = [(wrapper.path, wrapper.package, wrapper.owner)]
    if wrapper.interface is not None:
        owners.append((wrapper.interface.path, wrapper.interface.package, wrapper.interface.name))
    owners += [(s.path, s.namespace, s.type_chain) for s in wrapper.siblings]
    for path, namespace, chain in owners:
        source = wc._read(path)
        if source is None:
            return False
        owner = wc._find_type_in(wc.parse(source), namespace, chain)
        body = wc._child(owner, "declaration_list") if owner is not None else None
        declarations = [
            n
            for n in (body.named_children if body is not None else ())
            if n.type in ("property_declaration", "method_declaration")
            and (wc._name_of(n) or "").removeprefix("@") == wrapper.name
        ]
        if len(declarations) != 1:
            return False
        declaration = declarations[0]
        if wc._child(declaration, "attribute_list") is not None or xcs._modifiers(declaration) & _HIDING:
            return False
    family_types = {
        name for name in full if any(i.full == name for i in checkout.by_full.get(name, ()))
    }
    classes = [
        full_name
        for full_name in family_types
        if not (wrapper.interface is not None and full_name == f"{wrapper.interface.package}.{wrapper.interface.name}".lstrip("."))
    ]
    simple = {c.rsplit(".", 1)[-1] for c in classes}
    if any(checkout.spelled_where_unparsed(name) for name in simple):
        return False  # an unparsed file may declare a type deriving from one
    for infos in checkout.by_last.values():
        for info in infos:
            if info.full not in family_types and set(info.supertypes) & simple:
                return False  # derives from a family class
    for full_name in classes:
        pending = [s for info in checkout.by_full.get(full_name, ()) for s in info.supertypes]
        if any(info.partial for info in checkout.by_full.get(full_name, ())):
            return False
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            infos = checkout.by_last.get(name, ())
            if not infos or checkout.spelled_where_unparsed(name):
                return False  # a base this cannot read may declare the name
            for info in infos:
                if info.full in family_types:
                    continue
                if wrapper.name in info.members or info.partial:
                    return False
                pending.extend(info.supertypes)
    return True


def _sibling_bases_external(repo_dir: str, siblings) -> bool:
    from flag_cleanup import wrapper_csharp as wc

    readers = wc._Readers(repo_dir)
    for sibling in siblings:
        reader = readers.get(sibling.path)
        node = wc._find_type_in(reader.root, sibling.namespace, sibling.type_chain) if reader is not None else None
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
    #: ``text`` as proof 2 reads it, when that differs: a constructor's
    #: inbound parameter types left out. See the module docstring.
    narrow: str | None = None
    #: A member reached only through its owner's name (:func:`_qualified_only`).
    static: bool = False


@dataclass(frozen=True)
class _Taint:
    family: frozenset[str]
    tainting: frozenset[str]
    #: What proof 2 judges a file by: ``_close`` read narrowly.
    returning: frozenset[str]
    opaque: bool
    guest_named: bool
    reaching: frozenset[str] | None = None
    #: Of ``returning``, the names only static members add, each reached
    #: through its owner's name; ``(name, owner)`` pairs.
    static_returning: frozenset[tuple[str, str]] = frozenset()


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
    repo_dir: str,
    simple: frozenset[str],
    repo_types: frozenset[str],
    repo_full: frozenset[str],
    inbound: frozenset[str] = frozenset(),
) -> _Taint:
    from flag_cleanup import source_index
    from flag_cleanup import wrapper_csharp as wc

    # Read again each round rather than held: the checkout may be large.
    paths = [
        f.resolved
        for f in source_index.files(repo_dir, {".cs"}, with_ignored=True)
        if f.path.name.endswith(".cs")
    ]
    family, tainting, returning = set(simple), {_DYNAMIC_NAME}, set[str]()
    headers: list[_Header] = []
    opaque = False
    guests = [(path, stem, wc._read(path)) for path, stem in _guests(repo_dir)]
    stemmed: set[str] = set()
    stemmed_names: set[str] = set()
    guest_named = False
    usings, readable = xcs.msbuild_usings(repo_dir)
    opaque |= not readable
    for using in usings:
        if using.kind == "alias" and using.alias is not None and using.target is not None:
            headers.append(
                _Header("alias", using.target, using.alias, target=using.target.rsplit(".", 1)[-1])
            )
        elif using.kind == "static" and using.target is not None:
            headers.append(_Header("opaque", using.target))
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
            headers.extend(_headers(root, text, repo_types, repo_full, _Binder(root, path, repo_dir), inbound))
        _close(headers, family, tainting, returning)
        names = frozenset(family | tainting)
        for path, stem, text in guests:
            if path not in stemmed and (text is None or _spells(text, names)):
                stemmed.add(path)
                stemmed_names.add(stem)
                guest_named = True
                # The markup's class: whatever names it can reach its
                # members, and a C# member returning it brings it in.
                tainting.add(stem)
        if family | tainting == before:
            break
    names = frozenset(family | tainting)
    opaque |= any(h.kind == "opaque" and _spells(h.text, names) for h in headers)
    # Proof 2's closure, over the same headers: the full one read every file
    # the narrow one can need. A markup class it reached stays in. An alias
    # spelled like the interface may mean anything (its file spells the name,
    # so it was read): then the narrow reading proves nothing.
    near, reaching, returning = set(simple), {_DYNAMIC_NAME, *stemmed_names}, set[str]()
    statics: set[tuple[str, str]] = set()
    instance: set[str] = set()
    rebound = any(h.kind == "alias" and h.name in inbound for h in headers)
    _close(headers, near, reaching, returning, narrow=not rebound, statics=statics, instance=instance)
    tainting -= family | {_DYNAMIC_NAME}
    return _Taint(
        frozenset(family),
        frozenset(tainting),
        frozenset(returning - near),
        opaque,
        guest_named,
        frozenset(reaching - near - {_DYNAMIC_NAME}),
        frozenset((name, owner) for name, owner in statics if name not in instance and name not in near),
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


def _close(
    headers: list[_Header],
    family: set[str],
    tainting: set[str],
    returning: set[str],
    narrow: bool = False,
    statics: set[tuple[str, str]] | None = None,
    instance: set[str] | None = None,
) -> None:
    """Apply every header that spells a collected name, until none adds one;
    ``narrow`` reads each header as proof 2 does. ``statics`` collects
    ``(name, owner)`` of each returning name a static member adds, and
    ``instance`` each one any other member adds."""
    while True:
        size = len(family) + len(tainting) + len(returning)
        names = frozenset(family | tainting)
        for header in headers:
            text = header.narrow if narrow and header.narrow is not None else header.text
            if header.kind == "opaque" or not _spells(text, names):
                continue
            if header.kind == "alias":
                (family if header.target in family else tainting).add(header.name)
            elif header.kind == "type":
                tainting.add(header.name)
            else:
                if header.name is not None:
                    returning.add(header.name)
                    if header.static and header.owner is not None:
                        if statics is not None:
                            statics.add((header.name, header.owner))
                    elif instance is not None:
                        instance.add(header.name)
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


#: A type spelled as a plain name: ``IGate``, ``Ns.IGate``, ``global::Ns.IGate``, ``IGate?``.
_PLAIN_TYPE = re.compile(r"(?:@?[^\W\d]\w*\s*(?:::|\.)\s*)*@?([^\W\d]\w*)\s*\??")


def _header_text(node: Node, inbound: frozenset[str] = frozenset()) -> str:
    """``node``'s signature: everything but its attributes and its body,
    accessors or initializer; a field's or event's type alone. A
    constructor's by-value parameter whose type is plainly one of
    ``inbound`` is left out, but for its attributes and default."""
    from flag_cleanup import wrapper_csharp as wc

    parts: list[str] = []
    for i, child in enumerate(node.children):
        if not child.is_named or child.type in _BODIES or node.field_name_for_child(i) in _BODY_FIELDS:
            continue
        if inbound and node.type == "constructor_declaration" and child.type == "parameter_list":
            parts.extend(_parameter_text(p, inbound) for p in child.named_children)
            continue
        if child.type == "variable_declaration":
            type_node = child.child_by_field_name("type")
            if type_node is not None:
                parts.append(wc._text(type_node))
            continue
        parts.append(wc._text(child))
    return " ".join(parts)


def _parameter_text(parameter: Node, inbound: frozenset[str]) -> str:
    """A parameter's text, without its type when that type only takes one
    of ``inbound`` in: by value, spelled plainly."""
    from flag_cleanup import wrapper_csharp as wc

    whole = wc._text(parameter)
    type_node = parameter.child_by_field_name("type")
    if parameter.type != "parameter" or type_node is None or any(c.type == "modifier" for c in parameter.children):
        return whole
    spelled = wc._text(type_node)
    plain = _PLAIN_TYPE.fullmatch(spelled)
    if plain is None or plain.group(1) not in inbound:
        return whole
    start, end = type_node.start_byte - parameter.start_byte, type_node.end_byte - parameter.start_byte
    data = parameter.text or b""
    return (data[:start] + data[end:]).decode("utf-8", "replace")


def _inbound(interface, checkout) -> frozenset[str]:
    """The interface a constructor parameter may take in without tainting
    its type for proof 2: one plain ``interface`` declaration of the name in
    its file, with no attribute (``[CoClass]`` makes ``new I()`` legal), not
    ``partial``, and the checkout's only type of that simple name, since the
    parameter is matched by its spelling. Empty otherwise. A library type of
    the name is no concern: it cannot hand out a value of a repository type
    without a generic argument, and a plain name has none."""
    from flag_cleanup import wrapper_csharp as wc

    if (
        interface is None
        or len(checkout.by_last.get(interface.name, ())) != 1
        or checkout.spelled_where_unparsed(interface.name)
    ):
        return frozenset()
    source = wc._read(interface.path)
    if source is None:
        return frozenset()
    root = wc.parse(source)
    if root.has_error:
        return frozenset()
    declarations = [
        n
        for n in cross_file_keys.walk(root)
        if n.type in _TYPE_DECLARATIONS and (wc._name_of(n) or "").removeprefix("@") == interface.name
    ]
    if len(declarations) != 1:
        return frozenset()
    [declaration] = declarations
    if (
        declaration.type != "interface_declaration"
        or wc._child(declaration, "attribute_list") is not None
        or any(c.type == "modifier" and wc._text(c) == "partial" for c in declaration.children)
    ):
        return frozenset()
    return frozenset({interface.name})


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
    inbound: frozenset[str] = frozenset(),
) -> list[_Header]:
    from flag_cleanup import wrapper_csharp as wc

    found: list[_Header] = []
    if root.has_error:
        found.extend(_Header("opaque", text) for text in _error_scopes(root, source))
    for node in cross_file_keys.walk(root):
        if node.type == "using_directive":
            alias = node.child_by_field_name("name")
            if alias is None:
                using = xcs._using(node)
                if using.kind == "static" and using.is_global and node.named_children:
                    # Its type's members, by their bare names, in every file of
                    # the project: no file can be judged by what it spells.
                    found.append(_Header("opaque", wc._text(node.named_children[-1])))
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
        if node.type == "extension_declaration":
            # A C# 14 block adds members to a type declared elsewhere, so what
            # it spells can be reached without naming this class.
            found.append(_Header("opaque", wc._text(node)))
            continue
        if node.type not in _MEMBER_DECLARATIONS:
            continue
        holder = _enclosing_type(node)
        owner = wc._name_of(holder) if holder is not None else None
        owner = owner.removeprefix("@") if owner else None
        if node.type == "constructor_declaration" and (wc._name_of(node) or "").removeprefix("@") != owner:
            # Not a constructor: a shape this grammar misreads as one.
            found.append(_Header("opaque", wc._text(node)))
            continue
        if _private(node) and not (node.type in _GENERATOR_INPUTS and wc._child(node, "attribute_list")):
            # A private member with an attribute may be a source generator's
            # input (`[ObservableProperty] private T _x` emits a public `X`),
            # and what it emits is not seen here: count it as public, so its
            # type taints by name.
            continue
        text = _header_text(node)
        static = _qualified_only(node, holder)
        if node.type in ("field_declaration", "event_field_declaration"):
            declaration = wc._child(node, "variable_declaration")
            for declarator in declaration.named_children if declaration is not None else ():
                if declarator.type == "variable_declarator" and (dname := wc._name_of(declarator)):
                    found.append(_Header("member", text, dname.removeprefix("@"), owner, static=static))
            continue
        name = wc._name_of(node) if node.type in _NAMED_BY_FIELD else None
        name = name.removeprefix("@") if name else None
        extends: tuple[str, ...] = ()
        if node.type == "constructor_declaration" and inbound:
            narrow = _header_text(node, inbound)
            if narrow != text:
                found.append(_Header("member", text, None, owner, narrow=narrow))
                continue
        if node.type == "method_declaration":
            extends, unreadable = _extension(node, name, repo_types, repo_full, lookup)
            if unreadable:
                found.append(_Header("opaque", text))
        found.append(_Header("member", text, name, owner, extends, static=static))
    return found


def _qualified_only(node: Node, holder: Node | None) -> bool:
    """Whether the member ``node`` is reached only through its owner's name:
    a ``static`` field, property, event or method (no extension method,
    which ``x.M()`` calls) of a class, struct or record that is neither
    ``abstract`` nor ``virtual`` (``T.M()`` reaches an interface's). Outside
    the owner, a type deriving from it and a ``using static`` of it, C#
    reaches one only as ``Owner.M``."""
    from flag_cleanup import wrapper_csharp as wc

    if holder is None or holder.type not in ("class_declaration", "struct_declaration", *_RECORDS):
        return False
    if node.type not in ("field_declaration", "event_field_declaration", "property_declaration", "method_declaration", "event_declaration"):
        return False
    words = xcs._modifiers(node)
    if "static" not in words or words & {"abstract", "virtual", "override", "extern"}:
        return False
    if node.type == "method_declaration":
        parameters = node.child_by_field_name("parameters")
        first = next((p for p in parameters.named_children if p.type == "parameter"), None) if parameters else None
        if first is not None and any(not c.is_named and wc._text(c) == "this" for c in first.children):
            return False
        if first is not None and any(c.type == "modifier" and wc._text(c) == "this" for c in first.children):
            return False
    return True


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
    when a ``<Compile Include>`` in a file a project imports (or a ``.user``
    file) brings in one from outside it. A project's own ``<Compile
    Include>`` of a file outside its directory is recorded instead, when it
    resolves to a file of the checkout (no property, no wildcard): the file
    is then compiled by that project too, and :func:`_enclosing` says so.
    One that does not resolve is ``None``. A referenced project of another
    language is followed too, since references are transitive.

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
    linked: dict[Path, set[Path]] = {}
    for project in projects:
        text = texts[project]
        directory = project.parent
        targets = _references(text, directory) or []
        if _assembly_inside(text, directory, root, built):
            return None
        for outside in _linked_files(text, directory):
            if outside is None or not cross_file_keys.within(outside, root):
                return None
            linked.setdefault(outside, set()).add(directory)
        graph.setdefault(directory, set()).update(t.parent for t in targets)
    _LINKED[repo_dir] = {k: frozenset(v) for k, v in linked.items()}
    return {k: frozenset(v) for k, v in graph.items()}


#: ``repo_dir`` -> file -> the projects linking it in from outside their
#: directory (:func:`project_graph`).
_LINKED: dict[str, dict[Path, frozenset[Path]]] = {}


def _linked_files(text: str, project: Path) -> list[Path | None]:
    """Every file a project's ``<Compile Include>`` brings in from outside its
    directory, resolved; ``None`` for one that cannot be (a property, a
    wildcard)."""
    found: list[Path | None] = []
    for tag in _COMPILE_TAG.finditer(text):
        include = _attributes(tag.group(1)).get("include")
        if include is None:
            continue  # `Remove` or `Update`: never adds a file
        for value in _paths(include):
            if any(c in value for c in "*?"):
                resolved = _relative(value.replace("*", "x").replace("?", "x"), project)
                if resolved is None or not cross_file_keys.within(resolved, project):
                    found.append(None)
                continue
            resolved = _relative(value, project)
            if resolved is None or not cross_file_keys.within(resolved, project):
                found.append(resolved)
    return found


#: ``extension(`` or ``extension<T>(``: the header of a C# 14 extension block.
#: A method named ``extension`` matches too, which only errs toward True.
_EXTENSION_BLOCK = re.compile(r"\bextension\s*(?:<[^<>{};]*>\s*)?\(")
#: repo_dir -> (an extension block, a `dynamic`) somewhere in its C# files.
_SCANS: dict[str, tuple[bool, bool]] = {}


#: Markup that compiles to C# members other files can call: a ``dynamic``
#: there is as much a ``dynamic`` as one in a ``.cs`` file.
_DYNAMIC_HOSTS = (".razor", ".cshtml")


_LINKED_COMPILE = re.compile(r"<Compile\b[^>]*\bInclude\s*=", re.IGNORECASE)
_MSBUILD_SUFFIXES = (".csproj", ".props", ".targets")


def _scan(repo_dir: str) -> tuple[bool, bool]:
    """One read of every C# file of the checkout, and of the Razor files for
    ``dynamic``, ignored ones included; an unreadable file counts as both.
    A file outside every project directory compiles into no project, so it
    is skipped, unless a project links it in (``<Compile Include>``, as
    :func:`project_graph` records it). Without a graph, such a file is
    skipped only when no MSBuild file links one in and every one can be
    read. In a C# file only a token counts as ``dynamic``
    (:func:`_dynamic_token`); in markup the word anywhere does. Cached per
    ``repo_dir``, like the graph."""
    if repo_dir not in _SCANS:
        from flag_cleanup import cross_file_csharp as xcs
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        projects = xcs._project_dirs(repo_dir)
        links = _LINKED.get(repo_dir, {}) if project_graph(repo_dir) is not None else None
        everywhere = not projects
        if links is None:
            for f in source_index.files(repo_dir, set(_MSBUILD_SUFFIXES), with_ignored=True):
                if f.path.name.lower().endswith(_MSBUILD_SUFFIXES):
                    text = wc._read(f.resolved)
                    everywhere |= text is None or _LINKED_COMPILE.search(text) is not None
        extension = dynamic = False
        for f in source_index.files(repo_dir, {".cs", *_DYNAMIC_HOSTS}, with_ignored=True):
            csharp = f.path.name.endswith(".cs")
            if not csharp and not f.path.name.lower().endswith(_DYNAMIC_HOSTS):
                continue
            if (
                not everywhere
                and xcs._project_of(f.resolved, projects) is None
                and (links is None or Path(f.resolved).resolve() not in links)
            ):
                continue
            text = wc._read(f.resolved)
            if csharp:
                extension |= text is None or _EXTENSION_BLOCK.search(text) is not None
                dynamic |= text is None or _dynamic_token(text)
            else:
                dynamic |= text is None or _DYNAMIC.search(text) is not None
            if extension and dynamic:
                break
        _SCANS[repo_dir] = (extension, dynamic)
    return _SCANS[repo_dir]


def _dynamic_token(text: str) -> bool:
    """Whether C# ``text`` may hold a ``dynamic``: an identifier token
    spelling it (``@dynamic`` too), or any spelling of it in a file that
    does not parse whole. One inside a comment or a string is no type."""
    from flag_cleanup import wrapper_csharp as wc

    if _DYNAMIC.search(text) is None:
        return False
    root = wc.parse(text)
    if root.has_error:
        return True
    return any(
        node.type == "identifier" and wc._text(node).removeprefix("@") == "dynamic"
        for node in cross_file_keys.walk(root)
    )


#: (repo_dir, name) -> whether some file may declare or call a method ``name``.
_METHOD_NAMES: dict[tuple[str, str], bool] = {}


def spells_method(repo_dir: str, name: str) -> bool:
    """Whether a method called ``name`` may be declared or called anywhere in
    the checkout, ignored files included: a method or local function of
    that name, or a call ``name(…)`` or ``x.name(…)`` (type arguments
    allowed), in a C# file. A constructor, an object creation, a comment or
    a string is none. A C# file that does not parse whole, a Razor file or
    an unreadable one counts when it spells ``name`` followed by ``(``."""
    key = (repo_dir, name)
    if key not in _METHOD_NAMES:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        call = re.compile(rf"(?<![\w@])@?{re.escape(name)}\s*(?:<[^<>;{{}}()]*>\s*)?\(")
        found = False
        for f in source_index.files(repo_dir, {".cs", *_DYNAMIC_HOSTS}, with_ignored=True):
            csharp = f.path.name.endswith(".cs")
            if not (csharp or f.path.name.lower().endswith(_DYNAMIC_HOSTS)):
                continue
            text = wc._read(f.resolved)
            if text is not None and call.search(text) is None:
                continue
            if text is None or not csharp or _method_named(wc.parse(text), name):
                found = True
                break
        _METHOD_NAMES[key] = found
    return _METHOD_NAMES[key]


def _method_named(root: Node, name: str) -> bool:
    """Whether ``root`` may declare or call a method ``name`` (see
    :func:`spells_method`); always, when it does not parse whole."""
    from flag_cleanup import wrapper_csharp as wc

    if root.has_error:
        return True
    for node in cross_file_keys.walk(root):
        if node.type in ("method_declaration", "local_function_statement"):
            if wc._name_of(node) == name:
                return True
        elif node.type == "invocation_expression":
            function = node.child_by_field_name("function")
            if function is not None and function.type == "member_access_expression":
                function = function.child_by_field_name("name")
            if function is not None and function.type == "generic_name":
                function = function.named_children[0] if function.named_children else None
            if function is not None and function.type == "identifier" and wc._text(function).removeprefix("@") == name:
                return True
    return False


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
#: Every string a reflection name may be written in, an interpolated one included.
_STRINGS = _STRING_LITERALS | {"interpolated_string_expression"}
#: repo_dir -> the C# files (resolved) holding a by-name route whose name is
#: not a string literal, each with the types its every such route is bound
#: to, or None when one may reach any type; see :func:`computed_name_route`.
_ROUTES: dict[str, dict[str, frozenset[str] | None]] = {}
#: repo_dir -> the C# files (resolved) whose only computed-name routes are
#: lookups tested for an attribute alone (:func:`_attribute_only`).
_ATTRIBUTE_ROUTES: dict[str, tuple[str, ...]] = {}
#: In :data:`_ROUTES`: a route that may reach every project.
_ANY_PROJECT = ""
#: ``(repo_dir, family)`` -> :func:`computed_name_route`'s answer.
_ROUTED: dict[tuple[str, "Family"], bool] = {}


def nameof_value(node: Node | None) -> str | None:
    """The constant a ``nameof(...)`` expression is, the last simple name of
    its argument (``nameof(A.B<T>)`` is ``"B"``); None for anything else,
    ``@nameof(...)`` included, which is always a call."""
    if node is None or node.type != "invocation_expression":
        return None
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if function is None or function.type != "identifier" or xcs._text(function) != "nameof" or arguments is None:
        return None
    listed = [a for a in arguments.named_children if a.type == "argument"]
    if len(listed) != 1 or len(listed[0].named_children) != 1:
        return None
    value = listed[0].named_children[0]
    if value.type not in ("identifier", "generic_name", "member_access_expression", "qualified_name"):
        return None
    return xcs._last_name(value) or None


def _name_value(call: Node | None, route: str) -> Node | None:
    """The value a call passes as ``route``'s NAME argument: the one named by
    a parameter name it may take, else the one at its position among the
    positional arguments. None when it cannot be placed: no argument list,
    too few arguments, an unknown named one."""
    arguments = call.child_by_field_name("arguments") if call is not None else None
    if arguments is None:
        return None
    position, names = _NAME_ARGUMENT[route]
    positional: list[Node | None] = []
    for argument in arguments.named_children:
        if argument.type != "argument":
            continue
        label = argument.child_by_field_name("name")
        values = [c for c in argument.named_children if label is None or c.id != label.id]
        value = values[-1] if values else None
        if label is not None:
            if xcs._text(label).removeprefix("@") in names:
                return value
            continue
        positional.append(value)
    return positional[position] if len(positional) > position else None


def _constant(value: Node | None, nameof: bool = True) -> bool:
    """A plain string literal or, with ``nameof``, a ``nameof(...)``."""
    return value is not None and (value.type in _STRING_LITERALS or (nameof and nameof_value(value) is not None))


def _literal_name(call: Node | None, route: str, nameof: bool = True, repo_dir: str | None = None) -> bool:
    """Whether a call passes ``route``'s NAME argument (:func:`_name_value`)
    as a constant (:func:`_constant`), judged afterwards like a literal (see
    :func:`string_mentions`), or as a parameter that can hold nothing else
    (:func:`name_sources`). A literal anywhere else (a format, a binder
    flag) says nothing; a name that cannot be placed is computed."""
    value = _name_value(call, route)
    if _constant(value, nameof):
        return True
    return value is not None and name_sources(value, nameof, repo_dir) is not None


#: Method modifiers under which a caller may be out of sight.
_CALLED_FROM_ANYWHERE = frozenset(
    {"partial", "extern", "abstract", "public", "internal", "protected", "virtual", "override"}
)
_THEORY_ATTRIBUTES = frozenset({"Theory", "InlineData"})
#: A binding the parameter's name may also have in its method.
_BINDING_PARENTS = frozenset(
    {
        "variable_declarator", "parameter", "foreach_statement", "tuple_pattern", "declaration_expression",
        "declaration_pattern", "catch_declaration", "single_variable_designation", "local_function_statement",
        "from_clause", "let_clause", "join_clause", "join_into_clause", "query_continuation", "group_clause",
        "lambda_expression", "anonymous_method_expression", "parenthesized_variable_designation", "var_pattern",
        "recursive_pattern", "labeled_statement", "type_parameter", "using_statement", "fixed_statement",
    }
)


#: Binding constructs whose bound identifiers carry no field name.
_UNNAMED_BINDINGS = frozenset(
    {
        "tuple_pattern", "lambda_expression", "parenthesized_variable_designation", "from_clause", "let_clause",
        "join_clause", "join_into_clause", "query_continuation", "group_clause", "var_pattern",
        "recursive_pattern", "anonymous_method_expression", "labeled_statement", "single_variable_designation",
    }
)


#: An escape that spells one character: ``\uXXXX`` and ``\UXXXXXXXX`` in an
#: identifier or a string, ``\x`` with one to four hex digits in a string.
_ESCAPES = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|x([0-9A-Fa-f]{1,4}))")


def _decoded(text: str) -> str:
    """``text`` with every escape replaced by the character it spells and the
    formatting characters (Unicode ``Cf``, a soft hyphen) the compiler drops
    from an identifier taken out. Applied to any text, a string's or a
    comment's included, it can only add spellings: ``\\\\u0061`` is decoded
    too, and a string keeps its formatting characters at run time."""
    if "\\" in text:
        text = _ESCAPES.sub(
            lambda m: (lambda v: chr(v) if v <= 0x10FFFF else "")(int(m.group(1) or m.group(2) or m.group(3), 16)),
            text,
        )
    if not text.isascii():
        text = _format_characters().sub("", text)
    return text


@functools.cache
def _format_characters() -> re.Pattern[str]:
    """Every Unicode ``Cf`` character, as one character class."""
    return re.compile(
        "[" + "".join(re.escape(chr(c)) for c in range(0x110000) if unicodedata.category(chr(c)) == "Cf") + "]"
    )


def _ident(node: Node) -> str:
    """The name an identifier node spells to the compiler: escapes decoded,
    formatting characters and a leading ``@`` dropped."""
    return _decoded(xcs._text(node)).removeprefix("@")


def _only_binding(scope: Node, name: str, site: Node) -> bool:
    """Whether ``site`` is the one place in ``scope`` that binds ``name``:
    any other identifier of the name a binding construct holds directly is
    another binding, which the name might mean instead."""
    for node in cross_file_keys.walk(scope):
        if node.type == "implicit_parameter" and _ident(node) == name:
            return False
        if node.type != "identifier" or node.id == site.id or _ident(node) != name:
            continue
        parent = node.parent
        if parent is None or parent.type not in _BINDING_PARENTS:
            continue
        field = xcs._field(parent, node)
        if field in ("name", "left") or (field is None and parent.type in _UNNAMED_BINDINGS):
            return False
    return True


def _written(scope: Node, name: str) -> bool:
    """Whether anything in ``scope`` may write ``name``, erring toward yes:
    any mention on the left of an assignment (a deconstruction's tuple
    included), an increment, a ``ref`` alias of it, or passing it by
    ``ref``, ``out`` or ``in``."""
    for node in cross_file_keys.walk(scope):
        if node.type != "identifier" or _ident(node) != name:
            continue
        parent = node.parent
        if parent is None:
            continue
        if parent.type in ("prefix_unary_expression", "postfix_unary_expression", "ref_expression"):
            return True
        if parent.type == "argument" and any(c.type in ("ref", "out", "in") for c in parent.children):
            return True
        child, above = node, parent
        while above is not None and above.id != scope.id:
            if above.type == "assignment_expression" and xcs._field(above, child) == "left":
                return True
            child, above = above, above.parent
    return False


def _attribute_names(declaration: Node) -> list[str]:
    return [
        (xcs._last_name(a.child_by_field_name("name")) or "").removesuffix("Attribute")
        for c in declaration.children
        if c.type == "attribute_list"
        for a in c.named_children
        if a.type == "attribute"
    ]


def name_sources(name: Node, nameof: bool = True, repo_dir: str | None = None, depth: int = 0) -> list[Node] | None:
    """The values a route's NAME can hold when it is a parameter of its
    method that nothing writes and nothing else in the method binds, and
    every value passed to it is a constant (:func:`_constant`): each one, or
    None when the NAME is anything else.

    Values are passed only by the calls in its file of a ``private`` method
    reached no other way (no method group, qualified call, overload or
    string spelling its name; no attribute; its type not ``partial``), or by
    the ``[InlineData]`` rows of an xunit ``[Theory]`` nothing calls: with
    ``repo_dir``, no other mention of its name in the checkout. Without it,
    that is not checked, which only widens the answer.

    So may a ``foreach`` variable over an array of constants, written in
    place, held by a local nothing writes, passes or returns, or held by a
    private field only its class iterates (with ``repo_dir``, no other file
    spelling its name); and a value passed may itself be any of these."""
    if name.type != "identifier" or depth > 2:
        return None
    looped = _looped_constants(name, nameof, repo_dir)
    if looped is not None:
        return looped
    parameter = _ident(name)
    method = name.parent
    while method is not None and method.type != "method_declaration":
        method = method.parent
    listed = method.child_by_field_name("parameters") if method is not None else None
    method_name = method.child_by_field_name("name") if method is not None else None
    if listed is None or method_name is None or any(c.type not in ("parameter", "comment") for c in listed.named_children):
        return None
    parameters = [p for p in listed.named_children if p.type == "parameter"]
    names = [_ident(n) if (n := p.child_by_field_name("name")) is not None else "" for p in parameters]
    index = next(
        (
            i
            for i, p in enumerate(parameters)
            if (n := p.child_by_field_name("name")) is not None and _ident(n) == parameter
        ),
        None,
    )
    if index is None:
        return None
    declared = parameters[index]
    if any(c.type in ("modifier", "attribute_list") for c in declared.children):
        return None
    if not _only_binding(method, parameter, declared.child_by_field_name("name")) or _written(method, parameter):
        return None
    root = method
    while root.parent is not None:
        root = root.parent
    called = _ident(method_name)
    word = re.compile(rf"(?<![\w@]){re.escape(called)}(?![\w])")
    attributes = _attribute_names(method)
    sources: list[Node] = []
    if "Theory" in attributes and set(attributes) <= _THEORY_ATTRIBUTES:
        for row in (a for c in method.children if c.type == "attribute_list" for a in c.named_children):
            if (xcs._last_name(row.child_by_field_name("name")) or "").removesuffix("Attribute") != "InlineData":
                continue
            listed_values = next((c for c in row.named_children if c.type == "attribute_argument_list"), None)
            values = [a for a in listed_values.named_children if a.type == "attribute_argument"] if listed_values else []
            if len(values) != len(parameters) or any(len(v.named_children) != 1 for v in values):
                return None  # a named argument, or a row xunit cannot bind
            value = values[index].named_children[0]
            if not _constant(value, nameof):
                return None
            sources.append(value)
        if repo_dir is not None:
            mentioning = _spelling_files(repo_dir, called)
            if len(mentioning) != 1 or len(word.findall(_decoded(xcs._text(root)))) != 1:
                return None
        return sources
    words = xcs._modifiers(method)
    owner = method.parent.parent if method.parent is not None else None
    if (
        attributes
        or "private" not in words
        or words & _CALLED_FROM_ANYWHERE
        or owner is None
        or "partial" in xcs._modifiers(owner)
    ):
        return None
    for node in cross_file_keys.walk(root):
        if node.type in _STRINGS and word.search(_decoded(xcs._text(node))):
            return None  # reflection may call it
        if node.type != "identifier" or node.id == method_name.id or _ident(node) != called:
            continue
        callee = node.parent if node.parent is not None and node.parent.type == "generic_name" else node
        call = callee.parent
        if call is None or call.type != "invocation_expression" or xcs._field(call, callee) != "function":
            return None
        value = _argument_for(call, parameter, index, names)
        if value is False:
            return None
        if value is None:
            # Nothing passed: the default, which is a constant or nothing.
            after = [c for c in declared.named_children if c.start_byte >= declared.child_by_field_name("name").end_byte]
            value = after[0] if len(after) == 1 else None
        if _constant(value, nameof):
            sources.append(value)
            continue
        passed = name_sources(value, nameof, repo_dir, depth + 1) if value is not None else None
        if passed is None:
            return None
        sources.extend(passed)
    return sources


def _binding_loop(name: Node, variables: Callable[[Node], list[Node]]) -> tuple[Node, Node] | None:
    """``(foreach, the identifier it binds)`` for the nearest ``foreach``
    whose BODY holds ``name`` and whose ``variables(left)`` bind its name,
    when nothing inside that loop binds the name again. A use in the loop's
    ``in`` expression is outside the loop's scope."""
    variable = _ident(name)
    child, loop = name, name.parent
    while loop is not None:
        if loop.type == "foreach_statement" and xcs._field(loop, child) == "body":
            left = loop.child_by_field_name("left")
            site = next(
                (n for n in variables(left) if _ident(n) == variable), None
            ) if left is not None else None
            if site is not None:
                return (loop, site) if _only_binding(loop, variable, site) else None
        child, loop = loop, loop.parent
    return None


def _looped_constants(name: Node, nameof: bool, repo_dir: str | None = None) -> list[Node] | None:
    """The elements a ``foreach`` variable ``name`` takes, when it iterates
    an array of constants (:func:`_constant_elements`) and nothing else in
    its method binds the name: written in place, held by a local nothing
    writes, passes or returns, or held by a private field only its class
    iterates (:func:`_read_only_field`). A file that does not parse whole
    proves nothing: a formatting character inside a name (a soft hyphen)
    is an error to the grammar, and the name to the compiler.

    With ``repo_dir``, ``var`` counts only while no file of the checkout may
    declare a type of that name (:func:`declares_var`)."""
    found = _binding_loop(name, lambda left: [left] if left.type == "identifier" else [])
    method = found[0] if found is not None else None
    while method is not None and method.type not in ("method_declaration", "local_function_statement", "constructor_declaration"):
        method = method.parent
    if found is None or method is None:
        return None
    root = method
    while root.parent is not None:
        root = root.parent
    if root.has_error:
        return None
    # `foreach (Name n in names)` converts each string, by a conversion that
    # may be the repository's and return any name.
    looped = found[0].child_by_field_name("type")
    if looped is None or not (_implicit(looped, repo_dir) or _is_string(looped)):
        return None
    collection = found[0].child_by_field_name("right")
    if collection is not None and collection.type == "identifier":
        held = collection
        collection = _read_only_local(held, method, _ARRAY_READS)
        if collection is None:
            collection = _read_only_field(held, repo_dir)
    return _constant_elements(collection, nameof, repo_dir)


def _implicit(node: Node, repo_dir: str | None) -> bool:
    """Whether a declared type is ``var`` that infers its type: no file of
    the checkout may declare a type ``var`` (unknown without ``repo_dir``,
    where an answer only widens a list of names)."""
    return node.type == "implicit_type" and (repo_dir is None or not declares_var(repo_dir))


#: Text that may declare a type ``var``: a type keyword before it, a type
#: parameter list holding it, or a ``using`` alias. Only a filter: a file
#: it finds is parsed (:func:`_declares_var_in`).
_VAR_TYPE = re.compile(
    r"\b(?:class|struct|interface|enum|record|delegate\b[^;{}()]*?)\s+@?var\b"
    r"|[<,]\s*(?:(?:in|out)\s+)?@?var\s*[>,]"
    r"|\busing\s+@?var\s*="
)
#: Declarations whose ``name`` field can name a type.
_VAR_DECLARATIONS = frozenset(
    {
        "class_declaration", "struct_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "delegate_declaration", "type_parameter", "using_directive",
    }
)
#: repo_dir -> :func:`declares_var`'s answer.
_VAR: dict[str, bool] = {}


def _declares_var_in(path: str, text: str) -> bool:
    """Whether one file may declare a type named ``var``. A C# file that
    parses whole does when a type, a delegate, a type parameter or a
    ``using`` alias is named ``var`` (escapes decoded, ``@var`` included);
    any other file whose decoded text :data:`_VAR_TYPE` finds may."""
    from flag_cleanup import wrapper_csharp as wc

    if _VAR_TYPE.search(_decoded(text)) is None:
        return False
    if not path.lower().endswith(".cs"):
        return True
    root = wc.parse(text)
    if root.has_error:
        return True
    return any(
        n.type in _VAR_DECLARATIONS and (name := n.child_by_field_name("name")) is not None and _ident(name) == "var"
        for n in cross_file_keys.walk(root)
    )


def declares_var(repo_dir: str) -> bool:
    """Whether a C# or .NET guest file of the checkout, ignored ones
    included, may declare a type named ``var`` (:func:`_declares_var_in`),
    a project ``<Using Alias="var">`` item gives one, an item or a file
    cannot be read, or the index holds such a type. ``var`` then
    names that type, and a ``foreach (var n in names)`` converts each
    string by its conversions. Cached per ``repo_dir``."""
    if repo_dir not in _VAR:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        index = xcs.csharp_index(repo_dir)
        items, readable = xcs.msbuild_usings(repo_dir)
        declared = (
            bool(index.by_last.get("var"))
            or index.unreadable
            or not readable
            or any(i.alias is not None and _decoded(i.alias).removeprefix("@") == "var" for i in items)
        )
        if not declared:
            for f in source_index.files(repo_dir, None, with_ignored=True):
                if not f.path.name.lower().endswith((".cs", *wc._GUESTS)):
                    continue
                text = wc._read(f.resolved)
                if text is None or _declares_var_in(f.resolved, text):
                    declared = True
                    break
        _VAR[repo_dir] = declared
    return _VAR[repo_dir]


#: The one member of a ``string[]`` that only reads it. ``Count`` is no
#: member of an array: it binds to an extension of the repository's, which
#: may write the array.
_ARRAY_READS = frozenset({"Length"})


#: (repo_dir, name) -> :func:`_spelling_files`.
_SPELLINGS: dict[tuple[str, str], list[str]] = {}


def _spelling_files(repo_dir: str, name: str) -> list[str]:
    """Every file of the checkout, ignored ones included, that may spell
    ``name`` as a word: as written (:func:`cross_file_keys.mention_files`),
    or with escapes decoded and formatting characters dropped. Cached per
    ``(repo_dir, name)``."""
    if (repo_dir, name) not in _SPELLINGS:
        _SPELLINGS[(repo_dir, name)] = _scan_spellings(repo_dir, name)
    return _SPELLINGS[(repo_dir, name)]


def _scan_spellings(repo_dir: str, name: str) -> list[str]:
    found = set(cross_file_keys.mention_files(repo_dir, [name], exclude=(), with_ignored=True))
    word = re.compile(rf"(?<![\w@]){re.escape(name)}(?![\w])")
    for path, text in _decoded_files(repo_dir):
        if path not in found and (text is None or word.search(text)):
            found.add(path)
    return sorted(found)


#: repo_dir -> :func:`_decoded_files`.
_DECODED: dict[str, tuple[tuple[str, str | None], ...]] = {}


def _decoded_files(repo_dir: str) -> tuple[tuple[str, str | None], ...]:
    """Every file of the checkout, ignored ones included, whose text an
    escape or a formatting character changes, with its decoded text
    (:func:`_decoded`); None for one that cannot be read. Read once per
    ``repo_dir``: every other file spells a name only as written."""
    if repo_dir not in _DECODED:
        from flag_cleanup import source_index

        found: list[tuple[str, str | None]] = []
        for f in source_index.files(repo_dir, None, with_ignored=True):
            text = cross_file_keys._searchable_text(f.path, ["a"])
            if text is None:
                found.append((f.resolved, None))
            elif _ESCAPES.search(text) is not None or (not text.isascii() and _format_characters().search(text)):
                found.append((f.resolved, _decoded(text)))
        _DECODED[repo_dir] = tuple(found)
    return _DECODED[repo_dir]


def _is_string(node: Node | None) -> bool:
    return node is not None and node.type == "predefined_type" and xcs._text(node) == "string"


def _string_array(node: Node | None) -> bool:
    """Whether a type node is ``string[]``: one rank, no size, ``string``."""
    return (
        node is not None
        and node.type == "array_type"
        and _is_string(node.child_by_field_name("type"))
        and "".join(xcs._text(node.child_by_field_name("rank") or node).split()) == "[]"
    )


def _constant_elements(collection: Node | None, nameof: bool, repo_dir: str | None = None) -> list[Node] | None:
    """The elements of a ``string[]`` written in place of constants
    (:func:`_constant`): ``new[] { ... }``, ``new string[] { ... }``, or a
    collection expression initialising a ``string[]``. When it initialises
    a local or field, that is declared ``var`` or ``string[]``. Any other
    type may convert a string by the repository's own conversion, or
    enumerate anything. A spread refuses."""
    if collection is None:
        return None
    declarator = collection.parent
    declaration = declarator.parent if declarator is not None and declarator.type == "variable_declarator" else None
    declared = declaration.child_by_field_name("type") if declaration is not None and declaration.type == "variable_declaration" else None
    if declared is not None and not (_implicit(declared, repo_dir) or _string_array(declared)):
        return None
    if collection.type in ("implicit_array_creation_expression", "array_creation_expression"):
        if collection.type == "array_creation_expression" and not _string_array(collection.child_by_field_name("type")):
            return None
        initializer = next((c for c in collection.named_children if c.type == "initializer_expression"), None)
        elements = list(initializer.named_children) if initializer is not None else []
    elif collection.type == "collection_expression":
        if not _string_array(declared):
            return None
        elements = []
        for element in collection.named_children:
            values = element.named_children if element.type == "collection_element" else []
            if len(values) != 1 or values[0].type != "expression_element" or len(values[0].named_children) != 1:
                return None
            elements.append(values[0].named_children[0])
    else:
        return None
    if not elements or not all(_constant(e, nameof) for e in elements):
        return None
    return elements


#: The file :func:`_route_files` is judging, which :func:`_read_only_field`
#: names its field by; None outside that scan.
_ROUTE_FILE: str | None = None
#: The file :func:`_field_routes` is reading, while it reads one: then
#: :func:`_read_only_field` only records the field a route's names come from
#: in :data:`_FED_BY` and proves nothing.
_TABLE_FILE: str | None = None
_FED_BY: list[tuple[str, str, str]] = []
#: Types whose private fields :func:`_read_only_field` judges.
_FIELD_OWNERS = frozenset({"class_declaration", "struct_declaration", "record_declaration"})
_ACCESS_WORDS = frozenset({"public", "internal", "protected"})


def _read_only_field(name: Node, repo_dir: str | None) -> Node | None:
    """The initializer of the field ``name`` (a ``foreach``'s collection)
    reads, when it is a ``private`` field of the nearest type around it and
    only that type's own text can reach it, and every mention there only
    iterates it or reads its ``Length`` (:data:`_ARRAY_READS`).

    Every other spelling of the name in the type refuses, so ``name`` binds
    the field: nothing there binds the name again (a local, a parameter, a
    pattern, a primary constructor's parameter). The field is ``private`` (spelled or
    by default), carries no attribute, and its type is not ``partial``: no
    other text but reflection can name it. No string of its file spells its
    name, and with ``repo_dir`` no other file of the checkout does either
    (:func:`_spelling_files`). Every name is compared as the compiler reads
    it (:func:`_decoded`: ``N\\u0061mes`` is ``Names``).

    With ``repo_dir`` no route of the checkout that can reach a field may
    take a loose or computed name unless it is bounded away from the
    owner's type, nor a constant naming the field (:func:`_field_reachable`);
    the proof is made only inside :func:`_route_files`, which names the
    file. Reflection that reaches it by enumerating fields in a loop is the
    limit :func:`name_sources` accepts for a private method.

    While :func:`_field_routes` reads a route (:data:`_TABLE_FILE`), the
    field is only recorded as the route's source, after the checks inside
    the type: what may reach it is judged when it is itself proven."""
    if _TABLE_FILE is None and repo_dir is not None and _ROUTE_FILE is None:
        return None
    field_name = _ident(name)
    owner = name.parent
    while owner is not None and owner.type != "declaration_list":
        owner = owner.parent
    owner = owner.parent if owner is not None else None
    if owner is None or owner.type not in _FIELD_OWNERS or "partial" in xcs._modifiers(owner):
        return None
    body = owner.child_by_field_name("body") or next((c for c in owner.children if c.type == "declaration_list"), None)
    declarators = [
        d
        for f in (body.named_children if body is not None else [])
        if f.type == "field_declaration"
        for v in f.named_children
        if v.type == "variable_declaration"
        for d in v.named_children
        if d.type == "variable_declarator"
        and (n := d.child_by_field_name("name")) is not None
        and _ident(n) == field_name
    ]
    if len(declarators) != 1:
        return None
    declarator = declarators[0]
    field = declarator.parent.parent
    if xcs._modifiers(field) & _ACCESS_WORDS or any(c.type == "attribute_list" for c in field.children):
        return None
    declared = declarator.child_by_field_name("name")
    for node in cross_file_keys.walk(owner):
        if node.type != "identifier" or node.id == declared.id or _ident(node) != field_name:
            continue
        parent = node.parent
        if parent is not None and parent.type == "foreach_statement" and xcs._field(parent, node) == "right":
            if any(c.type == "ref" for c in parent.children):
                return None
            continue
        if (
            parent is not None
            and parent.type == "member_access_expression"
            and xcs._field(parent, node) == "expression"
            and _ident(parent.child_by_field_name("name")) in _ARRAY_READS
            and not (
                parent.parent is not None
                and parent.parent.type in ("invocation_expression", "assignment_expression")
                and xcs._field(parent.parent, parent) in ("function", "left")
            )
        ):
            continue
        return None  # written, passed, returned, qualified or used some other way
    owner_name = owner.child_by_field_name("name")
    if owner_name is None:
        return None
    if _TABLE_FILE is not None:
        _FED_BY.append((_TABLE_FILE, _ident(owner_name), field_name))
        values = [c for c in declarator.named_children if c.id != declared.id]
        return values[0] if len(values) == 1 else None
    root = owner
    while root.parent is not None:
        root = root.parent
    word = re.compile(rf"(?<![\w@]){re.escape(field_name)}(?![\w])")
    if any(
        n.type in _STRINGS and word.search(_decoded(xcs._text(n)))
        for n in cross_file_keys.walk(root)
    ):
        return None  # reflection may reach it
    if repo_dir is not None:
        if len(_spelling_files(repo_dir, field_name)) != 1:
            return None
        if _field_reachable(repo_dir, (_ROUTE_FILE, _ident(owner_name), field_name)):
            return None
    values = [c for c in declarator.named_children if c.id != declared.id]
    return values[0] if len(values) == 1 else None


#: Lookups that take the name of a member that may be a field. A static
#: ``RuntimeReflectionExtensions.GetRuntimeField(t, n)`` passes the type
#: first: every argument is judged, so the position does not matter.
_FIELD_NAMED = frozenset(
    {
        "GetField", "GetRuntimeField", "GetDeclaredField", "GetMember", "InvokeMember",
        # Test and library accessors of private state by name (MSTest's
        # `PrivateType`, helpers named so).
        "GetStaticField", "SetStaticField", "GetFieldOrProperty",
    }
)
#: Expression-tree members that take a field's name (on ``Expression``).
_FIELD_TREES = frozenset({"Field", "PropertyOrField"})
#: Lookups that list members, a field among them. Looped over whole
#: (``foreach (var f in t.GetFields())``) they are the documented limit;
#: indexed, filtered or handed on they may pick any field.
_FIELD_LISTS = frozenset({"GetFields", "GetRuntimeFields", "DeclaredFields", "GetMembers", "DeclaredMembers", "GetDefaultMembers"})
#: Lookups by a filter or a pattern: any name.
_FIELD_FILTERS = frozenset({"FindMembers", "FilterName", "FilterNameIgnoreCase"})
_FIELD_WORDS = _FIELD_NAMED | _FIELD_TREES | _FIELD_LISTS | _FIELD_FILTERS | {"UnsafeAccessor", "UnsafeAccessorAttribute"}
_FIELD_ROUTE_TEXT = re.compile(r"\b(?:" + "|".join(sorted(_FIELD_WORDS)) + r")\b")
#: The members of the framework enums a lookup's selector may name, each
#: under its namespace. ``IgnoreCase`` is left out: it widens the name.
_FLAG_MEMBERS: dict[str, tuple[str, frozenset[str]]] = {
    "BindingFlags": (
        "System.Reflection",
        frozenset(
            {
                "Default", "DeclaredOnly", "Instance", "Static", "Public", "NonPublic", "FlattenHierarchy",
                "InvokeMethod", "CreateInstance", "GetField", "SetField", "GetProperty", "SetProperty",
                "PutDispProperty", "PutRefDispProperty", "ExactBinding", "SuppressChangeType",
                "OptionalParamBinding", "IgnoreReturn", "DoNotWrapExceptions",
            }
        ),
    ),
    "MemberTypes": (
        "System.Reflection",
        frozenset({"Constructor", "Event", "Field", "Method", "Property", "TypeInfo", "Custom", "NestedType", "All"}),
    ),
    "CallingConventions": ("System.Reflection", frozenset({"Standard", "VarArgs", "Any", "HasThis", "ExplicitThis"})),
    "UnsafeAccessorKind": (
        "System.Runtime.CompilerServices",
        frozenset({"Constructor", "Method", "StaticMethod", "Field", "StaticField"}),
    ),
}
#: Every enum of :data:`_FLAG_MEMBERS`: what :func:`_plain_flag` trusts no
#: name of when it cannot see the checkout.
_ALL_FLAG_ENUMS = frozenset(_FLAG_MEMBERS)


def _plain_flag(text: str, shadowed: frozenset[str]) -> bool:
    """Whether ``text`` (decoded, spaces dropped) is one real member of a
    framework enum of :data:`_FLAG_MEMBERS` (``BindingFlags.Public``,
    ``System.Reflection.BindingFlags.Public``, ``global::…``), the enum not
    being one the checkout may shadow (:func:`_shadowed_enums`): a type, an
    alias or an extension block of the repository's could give
    ``BindingFlags.Hidden`` any value."""
    parts = text.removeprefix("global::").split(".")
    if len(parts) < 2:
        return False
    enum, member, namespace = parts[-2], parts[-1], ".".join(parts[:-2])
    known = _FLAG_MEMBERS.get(enum)
    return (
        known is not None
        and enum not in shadowed
        and member in known[1]
        and namespace in ("", known[0])
    )


#: repo_dir -> :func:`_shadowed_enums`.
_SHADOWED: dict[str, frozenset[str]] = {}


def _shadowed_enums(repo_dir: str | None) -> frozenset[str]:
    """The enums of :data:`_FLAG_MEMBERS` whose members a selector may not
    be read by: all of them without ``repo_dir``, or when the checkout
    declares an extension block (a static extension member can be named on
    any type) or its aliases cannot be read; else those a checkout type
    may be named after (:meth:`may_declare_type`) or an alias names."""
    if repo_dir is None:
        return _ALL_FLAG_ENUMS
    if repo_dir not in _SHADOWED:
        aliases = _alias_names(repo_dir)
        if aliases is None or declares_extension_blocks(repo_dir):
            _SHADOWED[repo_dir] = _ALL_FLAG_ENUMS
        else:
            index = xcs.csharp_index(repo_dir)
            _SHADOWED[repo_dir] = frozenset(
                e for e in _FLAG_MEMBERS if index.may_declare_type(e) or e in aliases
            )
    return _SHADOWED[repo_dir]


@dataclass(frozen=True)
class _FieldRoute:
    """One use of a lookup that may reach a field (:func:`_field_routes`)."""

    path: str
    #: The types its receiver can only be (:func:`_receiver_types`), as
    #: written; None: any type.
    bounds: frozenset[str] | None
    #: A name it takes that is not a known constant, any case or a pattern.
    loose: bool
    #: The decoded text of every constant it passes.
    constants: tuple[str, ...]
    #: The private fields its constants were read from (path, type, field).
    fed_by: frozenset[tuple[str, str, str]]
    #: Whether it may return a non-public field (:func:`_public_only`).
    private: bool = True
    #: Whether it compares a name ignoring case (an expression tree's).
    ignore_case: bool = False


#: repo_dir -> :func:`_field_routes`.
_FIELD_ROUTES: dict[str, tuple[_FieldRoute, ...]] = {}
#: repo_dir -> :func:`_alias_names`.
_ALIASES: dict[str, frozenset[str] | None] = {}
_ALIAS = re.compile(r"\busing\s+(?:static\s+)?@?(\w+)\s*=")


def _alias_names(repo_dir: str) -> frozenset[str] | None:
    """Every name a ``using X = …`` (``global`` or not, in any C# or .NET
    guest file, escapes decoded) or a project ``<Using Alias>`` item gives
    a type or namespace; None when an item cannot be read."""
    if repo_dir not in _ALIASES:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        items, readable = xcs.msbuild_usings(repo_dir)
        names: set[str] | None = {_decoded(i.alias).removeprefix("@") for i in items if i.alias} if readable else None
        if names is not None:
            for f in source_index.files(repo_dir, None, with_ignored=True):
                if not f.path.name.lower().endswith((".cs", *wc._GUESTS)):
                    continue
                text = wc._read(f.resolved)
                if text is None:
                    names = None
                    break
                names.update(_ALIAS.findall(_decoded(text)))
        _ALIASES[repo_dir] = frozenset(names) if names is not None else None
    return _ALIASES[repo_dir]


def _bounded_from(bound: str, owner: str, repo_dir: str) -> bool:
    """Whether a lookup on ``typeof(bound)`` cannot see the private fields
    of a type named ``owner``: ``bound``, escapes decoded, is a plain
    (qualified) name with no segment an alias may stand for, whose last
    segment is neither ``owner`` nor a type that may extend it."""
    text = _decoded(bound).replace(" ", "")
    text = re.sub(r"<.*>", "", text.removeprefix("global::"))
    segments = [s.removeprefix("@") for s in re.split(r"::|\.", text)]
    aliases = _alias_names(repo_dir)
    if aliases is None or any(not re.fullmatch(r"\w+", s) or s in aliases for s in segments):
        return False
    return segments[-1] != owner and not xcs.csharp_index(repo_dir).reaches([segments[-1]], owner)


def _selects(value: Node, shadowed: frozenset[str] = _ALL_FLAG_ENUMS) -> bool:
    """Whether a lookup's argument only selects, and names nothing: ``null``,
    a number, ``typeof``, a framework flag other than ``IgnoreCase``
    (:func:`_plain_flag`, against ``shadowed``) or ``|`` of them, an array
    of ``typeof``s, or ``Type.EmptyTypes``."""
    kind = value.type
    if kind in ("null_literal", "integer_literal", "boolean_literal", "typeof_expression", "default_expression"):
        return True
    if kind == "parenthesized_expression":
        return all(_selects(c, shadowed) for c in value.named_children)
    if kind == "binary_expression":
        return _operator(value) == "|" and all(_selects(c, shadowed) for c in value.named_children)
    if kind == "member_access_expression":
        text = _decoded(xcs._text(value)).replace(" ", "")
        return _plain_flag(text, shadowed) or text in ("Type.EmptyTypes", "System.Type.EmptyTypes")
    if kind in ("implicit_array_creation_expression", "array_creation_expression"):
        initializer = next((c for c in value.named_children if c.type == "initializer_expression"), None)
        return initializer is not None and all(c.type == "typeof_expression" for c in initializer.named_children)
    return False


def _route_arguments(call: Node | None, repo_dir: str) -> tuple[bool, list[str]] | None:
    """``(loose, constants)`` for a lookup's arguments: each a constant name
    (a literal, a ``nameof``, or what :func:`name_sources` proves), or one
    that only selects (``null``, a number, ``typeof``, a framework flag
    other than ``IgnoreCase`` or ``|`` of them, an array of ``typeof``s,
    ``Type.EmptyTypes``). Anything else, a ``ref``/``out`` argument or an
    ``IgnoreCase`` anywhere in it is loose. None when the call has no
    argument list."""
    arguments = call.child_by_field_name("arguments") if call is not None else None
    if arguments is None:
        return None
    if "IgnoreCase" in _decoded(xcs._text(arguments)):
        return (True, [])
    constants: list[str] = []
    loose = False
    shadowed = _shadowed_enums(repo_dir)

    def selects(value: Node) -> bool:
        return _selects(value, shadowed)

    for argument in arguments.named_children:
        if argument.type != "argument":
            continue
        if any(c.type in ("ref", "out", "in") for c in argument.children):
            return (True, [])
        label = argument.child_by_field_name("name")
        values = [c for c in argument.named_children if label is None or c.id != label.id]
        value = values[-1] if values else None
        if value is None:
            return (True, [])
        if value.type in _STRING_LITERALS:
            constants.append(_decoded(xcs._text(value)))
        elif (n := nameof_value(value)) is not None:
            constants.append(_decoded(n))
        elif selects(value):
            continue
        elif value.type == "identifier" and (sources := name_sources(value, True, repo_dir)) is not None:
            constants.extend(_decoded(nameof_value(s) or xcs._text(s)) for s in sources)
        else:
            loose = True
    return (loose, constants)


#: The ``Type`` lookups whose default binding is ``Public``, and which take
#: at most one ``BindingFlags`` argument.
_PUBLIC_BY_DEFAULT = frozenset({"GetField", "GetFields", "GetMember", "GetMembers"})
_FLAGGED = _PUBLIC_BY_DEFAULT | {"FindMembers"}


def _public_only(
    word: str, call: Node | None, declared: frozenset[str], shadowed: frozenset[str] = _ALL_FLAG_ENUMS,
    repo_dir: str | None = None,
) -> bool:
    """Whether a ``Type`` lookup can return public members only: no method
    of the checkout takes its name, and either its one ``BindingFlags``
    argument is a plain selector (:func:`_selects`) without ``NonPublic``,
    or it is ``GetFields()``/``GetMembers()`` with no argument, or
    ``GetField``/``GetMember`` with one plainly string argument on a
    receiver that is plainly a ``System.Type`` (:func:`_type_receiver`): a
    library's by-name accessor of private state (MSTest's
    ``PrivateObject.GetField(string)``) takes the same shape."""
    arguments = call.child_by_field_name("arguments") if call is not None else None
    if word not in _FLAGGED or word in declared or arguments is None:
        return False
    text = _decoded(xcs._text(arguments))
    if "NonPublic" in text:
        return False
    values = []
    for argument in arguments.named_children:
        if argument.type != "argument":
            continue
        label = argument.child_by_field_name("name")
        kept = [c for c in argument.named_children if label is None or c.id != label.id]
        if not kept:
            return False
        values.append(kept[-1])
    flags = [v for v in values if "BindingFlags" in _decoded(xcs._text(v))]
    if flags:
        return len(flags) == 1 and _selects(flags[0], shadowed)
    if word not in _PUBLIC_BY_DEFAULT:
        return False
    if word in ("GetFields", "GetMembers"):
        return not values
    # `GetField(string)` and `GetMember(string)`: one argument that is
    # plainly a string binds the instance overload before any extension.
    function = call.child_by_field_name("function")
    receiver = function.child_by_field_name("expression") if function is not None and function.type == "member_access_expression" else None
    return len(values) == 1 and _stringly(values[0]) and _type_receiver(receiver, repo_dir)


def _type_receiver(receiver: Node | None, repo_dir: str | None) -> bool:
    """Whether a lookup's receiver is plainly a ``System.Type``: a
    ``typeof(…)``, ``x.GetType()`` or ``Type.GetType(…)`` (no checkout
    method named ``GetType``), or a parameter or local of the member
    around it declared ``System.Type``, or ``Type`` while no checkout type
    may be named ``Type``, that nothing else in the member binds."""
    while receiver is not None and (
        receiver.type == "parenthesized_expression"
        or (receiver.type == "postfix_unary_expression" and xcs._text(receiver).rstrip().endswith("!"))
    ):
        receiver = receiver.named_children[0] if receiver.named_children else None
    if receiver is None or repo_dir is None:
        return False
    if receiver.type == "typeof_expression":
        return True
    index = xcs.csharp_index(repo_dir)
    if receiver.type == "invocation_expression":
        function = receiver.child_by_field_name("function")
        name = function.child_by_field_name("name") if function is not None and function.type == "member_access_expression" else None
        if name is None or _ident(name) != "GetType" or "GetType" in _checkout_method_names(repo_dir):
            return False
        spelled = _decoded(xcs._text(function.child_by_field_name("expression"))).replace(" ", "")
        arguments = receiver.child_by_field_name("arguments")
        count = len([a for a in arguments.named_children if a.type == "argument"]) if arguments is not None else 0
        return count == 0 or spelled in ("Type", "System.Type", "global::System.Type")
    if receiver.type != "identifier":
        return False
    variable = _ident(receiver)
    member = receiver.parent
    while member is not None and member.type not in (
        "method_declaration", "local_function_statement", "constructor_declaration", "accessor_declaration",
        "lambda_expression",
    ):
        member = member.parent
    while member is not None and member.parent is not None and member.parent.type not in ("declaration_list",):
        member = member.parent
    if member is None:
        return False
    declared = [
        n
        for n in cross_file_keys.walk(member)
        if n.type in ("parameter", "variable_declarator")
        and (name := n.child_by_field_name("name")) is not None
        and _ident(name) == variable
    ]
    if len(declared) != 1 or not _only_binding(member, variable, declared[0].child_by_field_name("name")):
        return False
    holder = declared[0] if declared[0].type == "parameter" else declared[0].parent
    written = holder.child_by_field_name("type") if holder is not None else None
    text = _decoded(xcs._text(written)).replace(" ", "") if written is not None else ""
    return text in ("System.Type", "global::System.Type") or (text == "Type" and not index.may_declare_type("Type"))


#: repo_dir -> every method or local function name the checkout's C# declares
#: among the lookups :func:`_type_receiver` trusts.
_METHOD_WORDS: dict[str, frozenset[str]] = {}


def _checkout_method_names(repo_dir: str) -> frozenset[str]:
    """Which of ``GetType`` the checkout declares as a method or local
    function (a file that cannot be read or parsed may)."""
    if repo_dir not in _METHOD_WORDS:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        found: set[str] = set()
        for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
            if not f.path.name.endswith(".cs"):
                continue
            text = wc._read(f.resolved)
            if text is None:
                found.add("GetType")
                break
            if "GetType" not in _decoded(text):
                continue
            root = wc.parse(text)
            if root.has_error or any(
                d.type in ("method_declaration", "local_function_statement")
                and (n := d.child_by_field_name("name")) is not None
                and _ident(n) == "GetType"
                for d in cross_file_keys.walk(root)
            ):
                found.add("GetType")
                break
        _METHOD_WORDS[repo_dir] = frozenset(found)
    return _METHOD_WORDS[repo_dir]


def _stringly(value: Node) -> bool:
    """Whether an expression is plainly a ``string``: a literal, an
    interpolation, a ``nameof``, or ``+`` with a string literal operand."""
    if value.type in _STRINGS or nameof_value(value) is not None:
        return True
    if value.type == "parenthesized_expression":
        return any(_stringly(c) for c in value.named_children)
    return value.type == "binary_expression" and _operator(value) == "+" and any(
        _stringly(c) for c in value.named_children
    )


def _outside_comments(root: Node, text: str) -> str:
    """``text`` with every comment of ``root`` blanked out."""
    data = bytearray(text.encode("utf-8"))
    for node in cross_file_keys.walk(root):
        if node.type == "comment":
            data[node.start_byte : node.end_byte] = b" " * (node.end_byte - node.start_byte)
    return data.decode("utf-8", "replace")


def _field_routes(repo_dir: str) -> tuple[_FieldRoute, ...]:
    """Every use, in a C# or .NET guest file of the checkout (ignored ones
    and every project included), of a lookup that may reach a field: a
    lookup by name (:data:`_FIELD_NAMED`, ``Expression.Field`` and
    ``PropertyOrField``), a member list not looped over whole
    (:data:`_FIELD_LISTS`), a filter (:data:`_FIELD_FILTERS`) or an
    ``[UnsafeAccessor]``. A guest file, or a C# file that does not parse,
    spelling one outside its comments is a route of any name on any type.
    A bare call to a method the enclosing type declares under such a name
    is that method. Read once per ``repo_dir``."""
    if repo_dir not in _FIELD_ROUTES:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        global _TABLE_FILE
        found: list[_FieldRoute] = []
        anything = lambda path: _FieldRoute(path, None, True, (), frozenset())  # noqa: E731
        parsed: list[tuple[str, Node]] = []
        declared: set[str] = set()
        for f in source_index.files(repo_dir, None, with_ignored=True):
            name = f.path.name.lower()
            if not name.endswith((".cs", *wc._GUESTS)):
                continue
            text = wc._read(f.resolved)
            if text is None:
                found.append(anything(f.resolved))
                continue
            if _FIELD_ROUTE_TEXT.search(_decoded(text)) is None:
                continue
            root = wc.parse(text) if name.endswith(".cs") else None
            if _directive_hides(text, root):
                found.append(anything(f.resolved))
                continue
            if root is not None and root.has_error:
                if _FIELD_ROUTE_TEXT.search(_decoded(_outside_comments(root, text))) is not None:
                    found.append(anything(f.resolved))
                continue
            if root is None:
                found.append(anything(f.resolved))
                continue
            parsed.append((f.resolved, root))
            # A method of the checkout named like a lookup: its calls are
            # not the framework's, whatever they bind.
            declared.update(
                _ident(n)
                for d in cross_file_keys.walk(root)
                if d.type in ("method_declaration", "local_function_statement")
                and (n := d.child_by_field_name("name")) is not None
                and _ident(n) in _FIELD_WORDS
            )
        for path, root in parsed:
            _TABLE_FILE = path
            f_resolved = path
            try:
                for node in cross_file_keys.walk(root):
                    if node.type != "identifier" or (word := _ident(node)) not in _FIELD_WORDS:
                        continue
                    parent = node.parent
                    if parent is None or (parent.type in _NAMED_DECLARATIONS and xcs._field(parent, node) == "name"):
                        continue
                    _FED_BY.clear()
                    access = parent if parent.type == "member_access_expression" and xcs._field(parent, node) == "name" else None
                    target = access if access is not None else node
                    if target.parent is not None and target.parent.type == "generic_name":
                        target = target.parent
                    call = target.parent if target.parent is not None and target.parent.type == "invocation_expression" else None
                    if call is not None and xcs._field(call, target) != "function":
                        call = None
                    bounds = _receiver_types(access, repo_dir) if access is not None else None
                    if access is None and call is not None and _own_method(node, word):
                        continue  # the enclosing type's own method of the name
                    if word in ("UnsafeAccessor", "UnsafeAccessorAttribute"):
                        attribute = parent if parent.type == "attribute" else None
                        listed = next((c for c in attribute.named_children if c.type == "attribute_argument_list"), None) if attribute else None
                        constants: list[str] = []
                        loose = attribute is None
                        for argument in listed.named_children if listed is not None else []:
                            values = [c for c in argument.named_children if c.type != "identifier" or c.next_sibling is None or c.next_sibling.type != "="]
                            value = values[-1] if values else None
                            if value is not None and value.type in _STRING_LITERALS:
                                constants.append(_decoded(xcs._text(value)))
                            elif value is None or not _plain_flag(_decoded(xcs._text(value)).replace(" ", ""), _shadowed_enums(repo_dir)):
                                loose = True
                        holder = attribute.parent.parent if attribute is not None and attribute.parent is not None else None
                        if holder is not None and (method := holder.child_by_field_name("name")) is not None:
                            constants.append(_ident(method))
                        found.append(_FieldRoute(f_resolved, None, loose, tuple(constants), frozenset()))
                        continue
                    tree = word in _FIELD_TREES
                    if tree:
                        receiver = access.child_by_field_name("expression") if access is not None else None
                        spelled = _decoded(xcs._text(receiver)) if receiver is not None else "Expression"
                        if spelled != "Expression" and not spelled.endswith(".Expression"):
                            continue
                        bounds = None
                    private = access is None or not _public_only(
                        word, call, frozenset(declared), _shadowed_enums(repo_dir), repo_dir
                    )
                    if word in _FIELD_LISTS:
                        holder = call if call is not None else target
                        if holder.parent is not None and holder.parent.type == "foreach_statement" and xcs._field(holder.parent, holder) == "right":
                            continue  # looped over whole: the documented limit
                        found.append(_FieldRoute(f_resolved, bounds, True, (), frozenset(), private))
                        continue
                    if word in _FIELD_FILTERS:
                        outer = call if call is not None else target
                        holder = outer.parent.parent if outer.parent is not None and outer.parent.type == "argument" else None
                        holder = holder.parent if holder is not None and holder.type == "argument_list" else None
                        callee = holder.child_by_field_name("function") if holder is not None and holder.type == "invocation_expression" else None
                        if word != "FindMembers" and callee is not None and _decoded(xcs._last_name(callee) or "") == "FindMembers":
                            continue  # judged with its FindMembers
                        found.append(_FieldRoute(f_resolved, bounds if word == "FindMembers" else None, True, (), frozenset(), private))
                        continue
                    judged = _route_arguments(call, repo_dir)
                    loose, constants = judged if judged is not None else (True, [])
                    loose = loose or any("*" in c for c in constants)
                    found.append(_FieldRoute(f_resolved, bounds, loose, tuple(constants), frozenset(_FED_BY), private, tree))
            finally:
                _TABLE_FILE = None
                _FED_BY.clear()
        _FIELD_ROUTES[repo_dir] = tuple(found)
    return _FIELD_ROUTES[repo_dir]


def _field_reachable(repo_dir: str, field: tuple[str | None, str, str]) -> bool:
    """Whether some route of the checkout (:func:`_field_routes`) may reach
    the private field ``(path, type, name)``: one not bounded away from the
    type (:func:`_bounded_from`) that takes a loose name, a name read from
    another field, or a constant spelling the field's name."""
    path, owner, name = field
    word = re.compile(rf"(?<![\w@]){re.escape(name)}(?![\w])")
    folded = re.compile(word.pattern, re.IGNORECASE)
    for route in _field_routes(repo_dir):
        if not route.private:
            continue
        if route.bounds is not None and all(_bounded_from(b, owner, repo_dir) for b in route.bounds):
            continue
        if route.loose or route.fed_by - {(path, owner, name)}:
            return True
        if any((folded if route.ignore_case else word).search(c) for c in route.constants):
            return True
    return False


def _own_method(node: Node, word: str) -> bool:
    """Whether a bare call's callee ``node`` names a method or local
    function an enclosing type or member declares: member lookup finds it
    before any ``using static`` import, and the file's own helper is no
    framework lookup."""
    scope = node.parent
    while scope is not None:
        if scope.type in ("declaration_list", "block"):
            for child in scope.named_children:
                if child.type in ("local_function_statement", "method_declaration"):
                    name = child.child_by_field_name("name")
                    if name is not None and _ident(name) == word:
                        return True
        scope = scope.parent
    return False


def _argument_for(call: Node, parameter: str, index: int, names: list[str]) -> Node | None | bool:
    """The value a call passes to ``parameter`` (by name, or in its place
    ``index``), None when it passes none, or False when its argument list
    cannot be read: a positional argument after a named one out of its
    place cannot be mapped to a parameter."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return False
    listed = [a for a in arguments.named_children if a.type != "comment"]
    if any(a.type != "argument" for a in listed):
        return False
    reordered = False
    for place, argument in enumerate(listed):
        label = argument.child_by_field_name("name")
        values = [c for c in argument.named_children if label is None or c.id != label.id]
        value = values[-1] if values else None
        if label is not None:
            spelled = _ident(label)
            if spelled == parameter:
                return value
            reordered |= place >= len(names) or names[place] != spelled
            continue
        if reordered:
            return False
        if place == index:
            return value
    return None


#: By-name words that reach a member without a NAME argument to judge: a
#: lookup that enumerates members (and is filtered by a string compared
#: with ``.Name``), one this does not place a NAME for (``FindMembers`` with
#: ``Type.FilterName``, ``TypeInfo``'s declared lookups, an expression
#: tree's ``Field`` or ``Call``), a descriptor type, and ``GetType()``,
#: whose ``Type`` may go on to any by-name API.
_UNNAMED_ROUTE = re.compile(
    r"\bGetType\s*\("
    r"|\bGet(?:Runtime)?(?:Properties|Methods|Fields|Members)\b"
    r"|\bGetRuntime(?:Method|Field)\b"
    r"|\bFindMembers\b|\bFilterName\b"
    r"|\bGetDeclared\w*|\bDeclared(?:Properties|Methods|Fields|Members|Events|NestedTypes|Constructors)\b"
    r"|\bExpression\s*\.\s*(?:Field|Call|Property|PropertyOrField|MakeMemberAccess)\b"
    r"|\bTypeDescriptor\b|\bPropertyDescriptor\b"
)
#: A lookup that matches a name ignoring case (``BindingFlags.IgnoreCase``,
#: ``StringComparison.OrdinalIgnoreCase``, ...), or by a prefix
#: (``GetMember`` takes ``"IsX*"``): a string reaches a member no
#: case-sensitive word of its name matches.
_LOOSE_NAME = re.compile(r"IgnoreCase\b|\"\w*\*\"")


def loose_names(source: str, repo_dir: str | None = None) -> bool:
    """:func:`_loose_names` against the enums ``repo_dir`` may shadow
    (:func:`_shadowed_enums`; every one of them without it)."""
    return _loose_names(source, _shadowed_enums(repo_dir))


@functools.lru_cache(maxsize=512)
def _loose_names(source: str, shadowed: frozenset[str]) -> bool:
    """Whether a file may look a member up ignoring case or by a prefix:
    it spells ``IgnoreCase`` or a ``"…*"`` pattern (escapes decoded), or a
    by-name lookup taking binding flags (:data:`_NAME_TAKING`) passes an
    argument besides its NAME that is not a plain selector
    (:func:`_selects`): flags held elsewhere (``Flags.Loose``, a local, a
    ``using static``) may include ``IgnoreCase``. A file that does not parse
    and spells a lookup may too."""
    decoded = _decoded(source)
    if _LOOSE_NAME.search(decoded) is not None:
        return True
    if _ROUTE_TEXT.search(decoded) is None:
        return False
    from flag_cleanup import wrapper_csharp as wc

    root = wc.parse(source)
    if root.has_error:
        return True
    for node in cross_file_keys.walk(root):
        if node.type != "invocation_expression":
            continue
        function = node.child_by_field_name("function")
        callee = _decoded(xcs._last_name(function) or "") if function is not None else ""
        if callee not in _NAME_TAKING:
            continue
        named = _name_value(node, callee)
        arguments = node.child_by_field_name("arguments")
        for argument in arguments.named_children if arguments is not None else []:
            if argument.type != "argument":
                continue
            label = argument.child_by_field_name("name")
            values = [c for c in argument.named_children if label is None or c.id != label.id]
            value = values[-1] if values else None
            if value is None or (named is not None and value.id == named.id):
                continue
            if not _selects(value, shadowed):
                return True
    return False


#: repo_dir -> the C# files (resolved) that may look a member up ignoring
#: case or by a prefix (:func:`loose_names`), or cannot be read.
_LOOSE: dict[str, tuple[str, ...]] = {}


def loose_files(repo_dir: str) -> tuple[str, ...]:
    """Every C# file of the checkout, ignored ones included, whose strings
    may name a member in another case or by a prefix: its strings are judged
    even where it never spells the name. Read once per ``repo_dir``."""
    if repo_dir not in _LOOSE:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        found: list[str] = []
        for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
            if f.path.name.endswith(".cs") and (text := wc._read(f.resolved)) is not None and loose_names(text, repo_dir):
                found.append(f.resolved)
        _LOOSE[repo_dir] = tuple(found)
    return _LOOSE[repo_dir]


#: The by-name lookups that take a PREFIX (``GetMember("IsX*")``,
#: ``FindMembers`` with ``Type.FilterName``): the only way a string ending in
#: ``*`` reaches a member it does not spell.
_PATTERN_ROUTE = re.compile(r"\bGetMembers?\b|\bFindMembers\b|\bFilterName(?:IgnoreCase)?\b")
#: repo_dir -> whether a C# file of the checkout spells a prefix lookup.
_PATTERNS: dict[str, bool] = {}


def pattern_routes(repo_dir: str) -> bool:
    """Whether any C# file of the checkout, ignored ones included, spells a
    lookup that takes a name prefix (:data:`_PATTERN_ROUTE`), or cannot be
    read. Without one, ``"*"`` or ``"IsX*"`` names no member, wherever a
    string may flow. Read once per ``repo_dir``, like the scan."""
    if repo_dir not in _PATTERNS:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        _PATTERNS[repo_dir] = any(
            (text := wc._read(f.resolved)) is None or _PATTERN_ROUTE.search(text) is not None
            for f in source_index.files(repo_dir, {".cs"}, with_ignored=True)
            if f.path.name.endswith(".cs")
        )
    return _PATTERNS[repo_dir]


def route_names(root: Node, source: str) -> list[Node] | None:
    """The NAME argument of every call in a parsed file whose callee is
    spelled like a by-name route (``GetProperty``, ``Expression.Property``,
    ``DataBinder.Eval``, ``Binding``, ...), whatever its receiver, which
    only over-counts. None when a string of the file may reach a member by
    name some other way: the file does not parse, or spells a route of
    :data:`_UNNAMED_ROUTE`.

    Asked only where no route anywhere the family can be takes a computed
    name: every NAME there is a literal or a ``nameof``, so no other string
    of the file can flow into one."""
    if root.has_error or _UNNAMED_ROUTE.search(source):
        return None
    found: list[Node] = []
    for node in cross_file_keys.walk(root):
        if node.type == "invocation_expression":
            function = node.child_by_field_name("function")
            callee = xcs._last_name(function) if function is not None else ""
        elif node.type == "object_creation_expression":
            created = node.child_by_field_name("type")
            callee = (xcs._simple_name(created) or "") if created is not None else ""
        else:
            continue
        value = _name_value(node, callee) if callee in _NAME_ARGUMENT else None
        if value is not None:
            # A parameter's constants are the route's NAME, wherever passed.
            found.extend(name_sources(value) or [value])
    return found


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


_TYPE_NAMES = frozenset({"identifier", "qualified_name", "generic_name", "alias_qualified_name", "predefined_type"})
#: Members of a local collection that only read it.
_READ_ONLY_MEMBERS = frozenset({"Count", "Length"})


def _typeof_type(node: Node | None) -> str | None:
    """The type a ``typeof(...)`` names, as written, when it is a plain
    (possibly qualified or generic) type name that no enclosing generic
    declaration's type parameter can be."""
    if node is None or node.type != "typeof_expression":
        return None
    type_node = node.child_by_field_name("type")
    if type_node is None or type_node.type not in _TYPE_NAMES:
        return None
    spelled = set(_WORD.findall(xcs._text(type_node)))
    scope = node.parent
    while scope is not None:
        # A method's or an interface's list is a field; a class's, struct's
        # or record's is an unnamed child.
        parameters = scope.child_by_field_name("type_parameters") or next(
            (c for c in scope.children if c.type == "type_parameter_list"), None
        )
        if parameters is not None and spelled & {
            xcs._text(n) for t in parameters.named_children if (n := t.child_by_field_name("name")) is not None
        }:
            return None
        scope = scope.parent
    return xcs._text(type_node)


def _receiver_types(access: Node | None, repo_dir: str | None) -> frozenset[str] | None:
    """The types whose ``Type`` alone a name-taking call's receiver can be:
    a ``typeof(C)``, or a ``foreach`` variable over a local ``Dictionary<K,
    Type>`` (deconstructed, its value) or array built only of ``typeof`` expressions,
    which nothing writes, passes or returns. None for anything else.

    ``Type.GetProperty`` and the like on ``C`` see ``C``'s members and its
    base classes', never an interface's: a family member is reached only
    through a ``C`` that is, or leads to, a family type."""
    if access is None or access.type != "member_access_expression":
        return None
    receiver = access.child_by_field_name("expression")
    single = _typeof_type(receiver)
    if single is not None:
        return frozenset({single})
    if receiver is None or receiver.type != "identifier":
        return None
    def variables(left: Node) -> list[Node]:
        if left.type == "identifier":
            return [left]
        return [n for n in left.named_children if n.type == "identifier"] if left.type == "tuple_pattern" else []

    found = _binding_loop(receiver, variables)
    method = found[0] if found is not None else None
    while method is not None and method.type not in ("method_declaration", "local_function_statement", "constructor_declaration"):
        method = method.parent
    if found is None or method is None:
        return None
    loop, site = found
    names = variables(loop.child_by_field_name("left"))
    if len(names) not in (1, 2):
        return None
    collection = loop.child_by_field_name("right")
    if collection is not None and collection.type == "identifier":
        collection = _read_only_local(collection, method)
    return _typeof_elements(collection, len(names), names.index(site), repo_dir)


def _read_only_local(name: Node, method: Node, members: frozenset[str] = _READ_ONLY_MEMBERS) -> Node | None:
    """The initializer of the local ``name`` reads, when it is a local of
    ``method`` that nothing else binds, and every other mention of it only
    reads one of ``members`` (:data:`_READ_ONLY_MEMBERS`, the ``Count`` of a
    framework collection included; an array's caller passes
    :data:`_ARRAY_READS`) or is iterated by a ``foreach``. Names are
    compared as the compiler reads them (:func:`_ident`)."""
    local = _ident(name)
    declarator = None
    for node in cross_file_keys.walk(method):
        if node.type != "identifier" or _ident(node) != local or node.id == name.id:
            continue
        parent = node.parent
        if parent is not None and parent.type == "variable_declarator" and xcs._field(parent, node) == "name":
            if declarator is not None:
                return None
            declarator = parent
            continue
        if parent is not None and parent.type == "foreach_statement" and xcs._field(parent, node) == "right":
            continue  # iterated again
        if (
            parent is not None
            and parent.type == "member_access_expression"
            and xcs._field(parent, node) == "expression"
            and _ident(parent.child_by_field_name("name")) in members
            and not (
                parent.parent is not None
                and parent.parent.type in ("invocation_expression", "assignment_expression")
                and xcs._field(parent.parent, parent) in ("function", "left")
            )
        ):
            continue
        return None  # written, passed, returned or used some other way
    if declarator is None or not _only_binding(method, local, declarator.child_by_field_name("name")):
        return None
    values = [c for c in declarator.named_children if c.id != declarator.child_by_field_name("name").id]
    return values[0] if len(values) == 1 else None


def _typeof_elements(collection: Node | None, arity: int, position: int, repo_dir: str | None) -> frozenset[str] | None:
    """The types of a collection built only of ``typeof`` expressions, read by a
    ``foreach`` of ``arity`` variables at ``position``: an array's elements
    (one variable), or a ``Dictionary``'s values (deconstructed, the second
    of two)."""
    if collection is None:
        return None
    initializer = collection.child_by_field_name("initializer") or next(
        (c for c in collection.named_children if c.type == "initializer_expression"), None
    )
    if initializer is None:
        return None
    if collection.type in ("implicit_array_creation_expression", "array_creation_expression"):
        if arity != 1:
            return None
        elements = list(initializer.named_children)
    elif collection.type == "object_creation_expression":
        created = collection.child_by_field_name("type")
        generic = created.child_by_field_name("name") if created is not None and created.type == "qualified_name" else created
        arguments = next((c for c in generic.named_children if c.type == "type_argument_list"), None) if generic else None
        if (
            generic is None
            or generic.type != "generic_name"
            or xcs._simple_name(generic) != "Dictionary"
            or arguments is None
            or len(arguments.named_children) != 2
            or (arity, position) != (2, 1)
            or repo_dir is None
            or xcs.csharp_index(repo_dir).may_declare_type("Dictionary")
            or any(c.type == "argument_list" and c.named_children for c in collection.children)
        ):
            return None
        elements = []
        for entry in initializer.named_children:
            if entry.type == "assignment_expression" and (left := entry.child_by_field_name("left")) is not None and (
                left.type == "element_binding_expression"
            ):
                elements.append(entry.child_by_field_name("right"))
            elif entry.type == "initializer_expression" and len(entry.named_children) == 2:
                elements.append(entry.named_children[1])
            else:
                return None
    else:
        return None
    types = [_typeof_type(e) for e in elements]
    if not types or None in types:
        return None
    return frozenset(t for t in types if t is not None)


#: Lookups of one property by name, whose result is that property or null.
#: Not ``GetMethod`` or ``GetField``: an auto-property's accessors and
#: backing field carry attributes the compiler adds (``[CompilerGenerated]``),
#: and an accessor may carry its own, none of which the property's
#: declaration shows.
_SINGLE_LOOKUPS = frozenset({"GetProperty", "GetRuntimeProperty"})


def _operator(node: Node) -> str:
    return next((xcs._text(c) for c in node.children if not c.is_named), "")


def _attribute_test(receiver: Node) -> bool:
    """Whether ``receiver`` (a lookup's result) is only asked, through
    ``?.``, whether it carries an attribute, so that a missing member and an
    attribute-less one answer alike: ``r?.GetCustomAttribute<T>()``
    compared with ``null``, or ``r?.IsDefined(typeof(T))`` compared with
    ``true`` or defaulted with ``?? false``."""
    access = receiver.parent
    if access is None or access.type != "conditional_access_expression" or xcs._field(access, receiver) != "condition":
        return False
    bindings = [c for c in access.named_children if c.id != receiver.id]
    call = access.parent
    if (
        len(bindings) != 1
        or bindings[0].type != "member_binding_expression"
        or call is None
        or call.type != "invocation_expression"
        or xcs._field(call, access) != "function"
    ):
        return False
    method = bindings[0].child_by_field_name("name")
    arguments = [a for a in call.child_by_field_name("arguments").named_children if a.type == "argument"]
    values = [a.named_children[-1] if a.named_children else None for a in arguments]
    if any(a.child_by_field_name("name") is not None for a in arguments):
        return False
    test = call.parent
    if test is None:
        return False

    def compared_with(literal: str) -> bool:
        if test.type == "binary_expression" and _operator(test) in ("==", "!="):
            other = test.child_by_field_name("right" if xcs._field(test, call) == "left" else "left")
            return other is not None and xcs._text(other) == literal
        if test.type == "is_pattern_expression" and xcs._field(test, call) == "expression":
            pattern = test.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "negated_pattern" and pattern.named_children:
                pattern = pattern.named_children[0]
            return pattern is not None and pattern.type == "constant_pattern" and xcs._text(pattern) == literal
        return False

    if method is not None and method.type == "generic_name" and xcs._simple_name(method) == "GetCustomAttribute":
        type_arguments = next((c for c in method.named_children if c.type == "type_argument_list"), None)
        return (
            type_arguments is not None
            and len(type_arguments.named_children) == 1
            and all(v is not None and v.type == "boolean_literal" for v in values)
            and len(values) <= 1
            and compared_with("null")
        )
    if method is not None and method.type == "identifier" and xcs._text(method) == "IsDefined":
        if not values or values[0] is None or values[0].type != "typeof_expression" or len(values) > 2:
            return False
        if len(values) == 2 and (values[1] is None or values[1].type != "boolean_literal"):
            return False
        if test.type == "binary_expression" and _operator(test) == "??" and xcs._field(test, call) == "left":
            right = test.child_by_field_name("right")
            return right is not None and xcs._text(right) == "false"
        return compared_with("true")
    return False


def _attribute_only(call: Node) -> bool:
    """Whether a single-member lookup's result is used only by
    :func:`_attribute_test`: directly, or held by a local of its method each
    of whose mentions is (so it is never written, passed or returned)."""
    if _attribute_test(call):
        return True
    declarator = call.parent
    if declarator is None or declarator.type != "variable_declarator":
        return False
    name = declarator.child_by_field_name("name")
    values = [c for c in declarator.named_children if name is None or c.id != name.id]
    method = declarator.parent
    while method is not None and method.type not in ("method_declaration", "local_function_statement", "constructor_declaration", "accessor_declaration"):
        method = method.parent
    if name is None or values != [call] or method is None:
        return False
    local = xcs._text(name).removeprefix("@")
    uses = [
        n
        for n in cross_file_keys.walk(method)
        if n.type == "identifier" and n.id != name.id and xcs._text(n).removeprefix("@") == local
    ]
    return bool(uses) and all(_attribute_test(n) for n in uses)


def _computed_route(
    root: Node,
    nameof: bool = True,
    repo_dir: str | None = None,
    bounds: set[str] | None = None,
    attribute_only: list[Node] | None = None,
) -> bool:
    """Whether a parsed file holds a by-name route whose name is computed:
    any use of a name-taking method that is not a call passing its name as
    a literal (a method group counts); ``Expression.Property`` or
    ``PropertyOrField``, a ``DataBinder`` member or ``new Binding(...)``
    likewise; any use of a descriptor lookup type; an alias or ``using
    static`` of a route type; or a route spelled where the file does not
    parse. ``nameof``: a ``nameof(...)`` name is a literal.

    With ``bounds``, a name-taking call whose receiver can only be the
    ``Type`` of some types (:func:`_receiver_types`) is not one: each of
    those types is added to ``bounds`` instead, for the caller to judge
    against a family. ``repo_dir`` lets a parameter name be proven
    (:func:`name_sources`) and a collection's type be checked.

    With ``attribute_only``, a property lookup whose result is only tested
    for an attribute (:func:`_attribute_only`) is not one either: it is
    added there instead, for the caller to judge against a family."""
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
            if (
                created is not None
                and xcs._simple_name(created) == "Binding"
                and not _literal_name(node, "Binding", nameof, repo_dir)
            ):
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
            if not _literal_name(call, name, nameof, repo_dir):
                types = _receiver_types(access, repo_dir) if bounds is not None and call is not None else None
                if types is not None:
                    bounds.update(types)
                elif attribute_only is not None and call is not None and name in _SINGLE_LOOKUPS and _attribute_only(call):
                    attribute_only.append(call)
                else:
                    return True
        elif name in ("Property", "PropertyOrField") and access is not None:
            receiver = access.child_by_field_name("expression")
            spelled = wc._text(receiver) if receiver is not None else ""
            if (spelled == "Expression" or spelled.endswith(".Expression")) and not _literal_name(
                call, name, nameof, repo_dir
            ):
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
                or not _literal_name(outer, method, nameof, repo_dir)
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


#: What ends a line to the C# compiler, and the whitespace it allows before a
#: directive's ``#`` (Unicode ``Zs``, tab, vertical tab, form feed).
_CS_LINE_BREAK = re.compile("\r\n|[\r\n\x85  ]")
_CS_SPACE = "\t\x0b\x0c \xa0  -   　"
#: A directive the grammar took into a comment: a line break inside the
#: comment, then whitespace, then ``#``.
_SWALLOWED = re.compile("[\r\n\x85  ][" + _CS_SPACE + "]*#")
_DIRECTIVE_LINE = re.compile("[" + _CS_SPACE + "]*#")


def _directive_hides(text: str, root: Node | None) -> bool:
    """Whether the grammar and the compiler may disagree about which text
    is code, so code may hide from every tree walk: a comment of ``root``
    holds a line that starts, after whitespace, with ``#`` (the grammar
    swallowed a directive, whatever its condition: ``#if NEVER /* …``,
    ``#else`` of ``#if true``), or a directive (a ``preproc`` node, or a
    line of ``text`` split at C#'s line breaks only that starts, after its
    whitespace, with ``#``) holds ``/*``, which the compiler reads as the
    directive's own text (``#region /*``). A file it finds is judged as
    spelling anything."""
    if root is not None and any(
        (n.type == "comment" and _SWALLOWED.search(xcs._text(n)))
        or (n.type.startswith("preproc") and "/*" in xcs._text(n))
        for n in cross_file_keys.walk(root)
    ):
        return True
    return any(
        _DIRECTIVE_LINE.match(line) is not None and "/*" in line for line in _CS_LINE_BREAK.split(text)
    )


def _route_files(repo_dir: str) -> dict[str, frozenset[str] | None]:
    """Every C# file of the checkout, ignored ones included, holding a
    computed-name route, with the types its routes are bound to (None: any
    type, see :func:`_computed_route`); an unreadable one counts as any.
    Read once per ``repo_dir``, like the scan."""
    if repo_dir not in _ROUTES:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        found: dict[str, frozenset[str] | None] = {}
        attributed: list[str] = []
        #: What each file's routes are when ``nameof`` is not the operator,
        #: where that differs.
        by_nameof: dict[str, frozenset[str] | None] = {}
        global _ROUTE_FILE
        for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
            if not f.path.name.endswith(".cs"):
                continue
            text = wc._read(f.resolved)
            if text is None:
                found[f.resolved] = None
            elif _ROUTE_TEXT.search(text):
                root = wc.parse(text)
                if _directive_hides(text, root):
                    found[f.resolved] = None
                    continue
                bounds: set[str] = set()
                tested: list[Node] = []
                _ROUTE_FILE = f.resolved
                try:
                    if _computed_route(root, repo_dir=repo_dir, bounds=bounds, attribute_only=tested):
                        found[f.resolved] = None
                        continue
                    if bounds:
                        found[f.resolved] = frozenset(bounds)
                    loose: set[str] = set()
                    if _computed_route(root, nameof=False, repo_dir=repo_dir, bounds=loose, attribute_only=tested):
                        by_nameof[f.resolved] = None
                    elif loose != bounds:
                        by_nameof[f.resolved] = frozenset(loose)
                    if tested:
                        attributed.append(f.resolved)
                finally:
                    _ROUTE_FILE = None
        if by_nameof and declares_nameof(repo_dir):
            found.update(by_nameof)
        _ATTRIBUTE_ROUTES[repo_dir] = tuple(p for p in attributed if p not in found)
        # A project `<Using>` item is a `global using` for its project's
        # files. One in a `.props`/`.targets` file may reach any project, as
        # may an item this cannot read.
        items, readable = xcs.msbuild_usings(repo_dir)
        if not readable:
            found[_ANY_PROJECT] = None
        for item in items:
            if item.kind in ("alias", "static") and (item.target or "").rsplit(".", 1)[-1] in _ROUTE_TYPES:
                found[item.path if item.path.endswith(".csproj") else _ANY_PROJECT] = None
        _ROUTES[repo_dir] = found
    return _ROUTES[repo_dir]


_NAMEOF_WORD = re.compile(r"@?\bnameof\b")
#: repo_dir -> whether something of the checkout may be named ``nameof``.
_NAMEOF: dict[str, bool] = {}


def declares_nameof(repo_dir: str) -> bool:
    """Whether a C# file of the checkout may declare something named
    ``nameof`` (a method, a local, a delegate-typed field): ``nameof(x)``
    then calls it wherever it is in scope, and is no constant. Every
    identifier ``nameof`` must be the callee of an invocation, written
    without ``@``; a file that cannot be read, or that spells the word where
    it does not parse, may declare one. Cached per ``repo_dir``, like the
    scan."""
    if repo_dir not in _NAMEOF:
        from flag_cleanup import source_index
        from flag_cleanup import wrapper_csharp as wc

        declared = False
        for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
            if not f.path.name.endswith(".cs"):
                continue
            text = wc._read(f.resolved)
            if text is None:
                declared = True
                break
            if _NAMEOF_WORD.search(text) is None:
                continue
            for node in cross_file_keys.walk(wc.parse(text)):
                if node.type == "ERROR" or node.is_missing:
                    if _NAMEOF_WORD.search(wc._text(node)):
                        declared = True
                elif node.type == "identifier" and wc._text(node) in ("nameof", "@nameof"):
                    parent = node.parent
                    declared |= (
                        wc._text(node) != "nameof"
                        or parent is None
                        or parent.type != "invocation_expression"
                        or xcs._field(parent, node) != "function"
                    )
                if declared:
                    break
            if declared:
                break
        _NAMEOF[repo_dir] = declared
    return _NAMEOF[repo_dir]


def computed_name_route(repo_dir: str, family: Family) -> bool:
    """Whether a file that may hold a family value reaches members by a
    name it does not spell as a literal: then a string anywhere the family
    can be may be that name, wherever it is written (a constants class,
    another part of a ``partial`` type)."""
    key = (repo_dir, family)
    if key not in _ROUTED:
        routes = _route_files(repo_dir)
        attributed = () if family.attributes_unobservable else _ATTRIBUTE_ROUTES.get(repo_dir, ())
        _ROUTED[key] = any(
            (path == _ANY_PROJECT or reaches(repo_dir, path, family)) and not _bounded_away(bounds, family)
            for path, bounds in routes.items()
        ) or any(reaches(repo_dir, path, family) for path in attributed)
    return _ROUTED[key]


def _bounded_away(bounds: frozenset[str] | None, family: Family) -> bool:
    """Whether routes bound to these types cannot reach a family member: no
    type spells a family or tainting name (one that is, derives from, or
    leads to a family type), and every name that could is known."""
    return (
        bounds is not None
        and not family.opaque
        and not any(_spells(t, family.simple_names | family.tainting) for t in bounds)
    )


def reset_cache() -> None:
    """For tests that rebuild a tree under the same path."""
    from flag_cleanup import ctor_param_csharp, wrapper_csharp

    ctor_param_csharp._UNPARSED.clear()
    ctor_param_csharp._CONVERSIONS.clear()

    wrapper_csharp._FAKE_NAMES.clear()
    wrapper_csharp._LOCATOR_DECLARED.clear()
    wrapper_csharp._EXTENSION_NAMES.clear()
    _GRAPHS.clear()
    _LINKED.clear()
    _SCANS.clear()
    _METHOD_NAMES.clear()
    _ROUTES.clear()
    _ATTRIBUTE_ROUTES.clear()
    _ROUTED.clear()
    _NAMEOF.clear()
    _VAR.clear()
    _FIELD_ROUTES.clear()
    _SPELLINGS.clear()
    _SHADOWED.clear()
    _METHOD_WORDS.clear()
    _DECODED.clear()
    _ALIASES.clear()
    _LOOSE.clear()
    _PATTERNS.clear()


def _enclosing(path: str, projects: frozenset[Path], repo_dir: str | None = None) -> list[Path]:
    """Every project whose directory holds ``path``: an SDK project compiles
    every ``.cs`` file below it, a nested project's included, unless told
    otherwise, which is not read here. With ``repo_dir``, also every project
    that links the file in with a ``<Compile Include>`` (see
    :func:`project_graph`)."""
    found = [parent for parent in Path(path).parents if parent in projects]
    if repo_dir is not None and project_graph(repo_dir) is not None:
        found += [d for d in sorted(_LINKED.get(repo_dir, {}).get(Path(path).resolve(), ())) if d not in found]
    return found


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
    return family.homeless or _reaches_projects(repo_dir, path, family.projects)


def sees(repo_dir: str, path: str, declared: str) -> bool:
    """Whether the file ``path`` may see a type declared in the file
    ``declared``, by :func:`reaches`' walk. A type outside every project
    may be seen anywhere."""
    homes = frozenset(_enclosing(str(Path(declared).resolve()), xcs._project_dirs(repo_dir), repo_dir))
    return not homes or _reaches_projects(repo_dir, path, homes)


def _reaches_projects(repo_dir: str, path: str, targets: frozenset[Path]) -> bool:
    """Whether a project holding ``path`` is, or references, one of
    ``targets``, or ``path`` is a file-based app. An unreadable project
    graph reaches everything."""
    graph = project_graph(repo_dir)
    if graph is None:
        return True
    pending = _enclosing(str(Path(path).resolve()), xcs._project_dirs(repo_dir), repo_dir)
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in targets:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return _file_based_app(path)


# --- family-free files --------------------------------------------------------


_THIS = re.compile(r"(?<![\w@])(?:this|base)\b")
#: Declarations whose body is judged on its own by :func:`free_members`.
_JUDGED_MEMBERS = frozenset(
    {
        "method_declaration", "constructor_declaration", "destructor_declaration", "property_declaration",
        "indexer_declaration", "operator_declaration", "conversion_operator_declaration", "event_declaration",
        "field_declaration", "event_field_declaration",
    }
)


def _declared_names(node: Node) -> list[str]:
    from flag_cleanup import wrapper_csharp as wc

    if node.type in ("field_declaration", "event_field_declaration"):
        declaration = wc._child(node, "variable_declaration")
        return [
            n.removeprefix("@")
            for d in (declaration.named_children if declaration is not None else ())
            if d.type == "variable_declarator" and (n := wc._name_of(d))
        ]
    name = wc._name_of(node)
    return [name.removeprefix("@")] if name else []


#: .NET's naming convention for an interface: a library base spelled so is
#: taken as one, whose members a class cannot reach without a cast.
_INTERFACE_NAME = re.compile(r"I[A-Z]")


def free_members(source: str, root: Node, family: Family, repo_dir: str) -> list[Node]:
    """The members of the file's types whose own text can introduce no value
    of a family type: proof 2 (:func:`family_free`) judged per member, for a
    file that is not family-free as a whole.

    Inside a member's body a value gets a type from what the member spells
    (its header, locals, casts, calls) or from what is in scope unspelled:
    the members of its enclosing types, private ones included, their primary
    constructors' parameters, and ``this``. So a member is free when its text
    spells no family, reaching or family-returning name, nor any name a
    private member or nested type of an enclosing type leads to (collected to
    a fixpoint over the outermost type), nor ``dynamic``, and no enclosing
    type is one of those names. Inherited and ``using static`` members are
    non-private, so already among the returning names. A ``partial`` type may
    get members from another file, so nothing in it is free; nor is anything
    in a file that does not parse whole or holds an app directive.

    One assumption is taken that the whole-file proof also makes: a library
    member does not hand a family value back as ``dynamic``. Two guards keep
    it narrow. Nothing is free while any file of the checkout spells
    ``dynamic`` (a repository member returning it is not among the names).
    And inside a type with a library class up its base chain, whose
    inherited members (MVC's ``ViewBag``) are reached unspelled, the caller
    still judges a read whose receiver may start at one
    (:func:`exposed_spans`)."""
    from flag_cleanup import wrapper_csharp as wc

    if family.opaque or family.dynamic_anywhere or root.has_error or _APP_DIRECTIVE.search(source):
        return []
    reaching = family.tainting if family.reaching is None else family.reaching
    family_types = {n.removeprefix("@") for n in family.simple_names}
    # A static member's bare name inside its owner is among ``leading``: the
    # owner is declared here, its header spelling a collected name.
    known = family_types | set(reaching) | set(_returning_outside(family, source))
    free: list[Node] = []
    outermost = [
        n for n in cross_file_keys.walk(root)
        if n.type in _TYPE_DECLARATIONS and _enclosing_type(n) is None
    ]
    for top in outermost:
        types = [n for n in cross_file_keys.walk(top) if n.type in _TYPE_DECLARATIONS]
        if any(_PARTIAL.search(_header_text(t)) for t in types) or (family.guest_named and _PARTIAL.search(wc._text(top))):
            continue
        members = [n for n in cross_file_keys.walk(top) if n.type in _JUDGED_MEMBERS]
        leading: set[str] = set()
        while True:
            names = frozenset(known | leading)
            size = len(leading)
            for t in types:
                name = wc._name_of(t)
                # Its header holds its base list and primary-constructor
                # parameters, which are in scope in every member.
                if name and _spells(_header_text(t), names):
                    leading.add(name.removeprefix("@"))
            for member in members:
                if _spells(_header_text(member), names):
                    leading.update(_declared_names(member))
                    # A type leads by its name to what its members lead to.
                    owner = _enclosing_type(member)
                    if owner is not None and (name := wc._name_of(owner)):
                        leading.add(name.removeprefix("@"))
            if len(leading) == size:
                break
        names = frozenset(known | leading)
        for member in members:
            text = wc._text(member)
            # An enclosing family type's members are the family's. Any other
            # enclosing type is reached unspelled only as `this`/`base` (its
            # indexer, a pattern method, a conversion); its members by name,
            # which `names` holds.
            this = _THIS.search(text) is not None
            holder = _enclosing_type(member)
            tainted = False
            while holder is not None:
                name = (wc._name_of(holder) or "").removeprefix("@")
                # A type whose own header leads to one (its base list, say) is
                # one: its members are reached with an implicit `this`.
                tainted |= (
                    name in family_types
                    or _spells(_header_text(holder), names - {name})
                    or (this and name in names)
                )
                holder = _enclosing_type(holder)
            if not tainted and not _spells(text, names) and not _DYNAMIC.search(text):
                free.append(member)
    return free


#: A ``using static`` directive, in a file or as a global one.
_USING_STATIC = re.compile(r"\busing(?:\s|/\*.*?\*/|//[^\n]*)+static\b", re.DOTALL)


def _returning_outside(family: Family, source: str) -> frozenset[str]:
    """The family-returning names a file can reach a member by, outside the
    members' owners: all of them, less those only static members add, each
    reached as ``Owner.M`` (``Owner`` is a reaching name, which the file
    then spells). A file with a ``using static`` reaches them all bare."""
    if not family.static_returning or _USING_STATIC.search(source):
        return family.returning
    return family.returning - frozenset(n for n, _ in family.static_returning)


def family_free(source: str, root: Node, family: Family) -> bool:
    """Whether nothing in the file can introduce a value of a family type.

    Judged on the raw text, comments and strings included, which can only
    over-taint; ``root`` is the caller's parse of ``source``, unused here. A
    file-based app is never family-free: a ``#:package`` may bring in
    anything. A read inside a type inheriting a library class's members is
    judged all the same (:func:`exposed_spans`).
    """
    del root
    if family.opaque or _DYNAMIC.search(source) or _APP_DIRECTIVE.search(source):
        return False
    reaching = family.tainting if family.reaching is None else family.reaching
    if _spells(source, family.simple_names | reaching | _returning_outside(family, source)):
        return False
    return not (family.guest_named and _PARTIAL.search(source))
