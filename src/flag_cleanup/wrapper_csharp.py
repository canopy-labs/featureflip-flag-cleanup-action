"""C# profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

A wrapper is a ``bool`` method or a get-only ``bool`` property, declared in a
non-generic top-level class, struct or record, whose whole body is one
synchronous read of this flag (``=> read``, ``{ return read; }``, or a lone
``get`` accessor of either shape). The read is one the rules fold in this run:
a literal key, a same-file key constant where Gate 2 let the const rules run,
or a constant of another file by a text the cross-file resolution proved.
``GetBooleanValueAsync`` never qualifies: it is folded only under ``await``,
and an ``async`` member returns a ``Task<bool>``, not a ``bool``.

C# needs three things the JVM profiles do not:

* **Implicit interface implementation.** A public member satisfies a
  same-named interface member without any keyword, so a member of a class
  with a base type outside the repository may be implementing it, and
  deleting the member would break the build. Every base type, transitively,
  must be declared in the repository (``overridable`` otherwise).
* **A property read has no call.** It is marked as ``SENTINEL()``, the shape
  the seed rules at the top of ``rules/csharp.toml`` fold.
* **A public interface is not necessarily published.** It cannot be
  implemented outside the repository when its project is not packed: no
  ``.nuspec`` beside it, and no packing property (``IsPackable`` true,
  ``PackageId``, ``GeneratePackageOnBuild``, package metadata) or ``<Import>``
  in its ``.csproj`` or a ``Directory.Build`` file above it.

A receiver's declared type is resolved by C#'s own namespace lookup, which
:mod:`flag_cleanup.cross_file_csharp` already implements: a type name means
the wrapper's class only when the lookup reaches the wrapper's namespace and
nothing closer -- a nested type, a type parameter, a nearer namespace, an
alias -- claims the name. Two ``using`` directives both supplying it do not
compile (CS0104), so a file that compiles can only mean ours.

A base type outside the repository is not inspected for a member or a nested
type that would shadow a name, which :mod:`flag_cleanup.cross_file_csharp`
accepts too.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_csharp as xcs
from flag_cleanup import cross_file_keys, key_const, mock_stubs, syntax
from flag_cleanup import wrapper_inline as wi
from flag_cleanup.cross_file_dotnet_guest import CASE_INSENSITIVE_SUFFIXES, DOTNET_GUEST_EXTENSIONS

logger = logging.getLogger(__name__)

EXTENSIONS = (".cs",)
#: .NET languages that can call a C# member but that this tool never rewrites.
FOREIGN_EXTENSIONS = (".vb", ".vbhtml", ".fs", ".fsx", ".fsi")
#: Files besides C# that can implement, extend or call a C# type: Razor
#: (``@implements``, ``@inherits``), XAML, Web Forms, scripts, templates.
_GUESTS = DOTNET_GUEST_EXTENSIONS["dotnet_guest"]

#: The synchronous read. ``GetBooleanValueAsync`` is folded only awaited.
_READ_CALLEES = frozenset({"BoolVariation"})
_COMMENTS = frozenset({"comment"})
_STRINGS = frozenset({"string_literal", "verbatim_string_literal", "raw_string_literal", "string_content"})

_OWNERS = frozenset(
    {"class_declaration", "struct_declaration", "record_declaration", "record_struct_declaration"}
)
_TYPES = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "record_declaration": "class",
    "record_struct_declaration": "struct",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "delegate_declaration": "other",
}
#: Member declarations whose ``name`` field declares a name in a type.
_NAMED_MEMBERS = frozenset(
    {
        "method_declaration",
        "property_declaration",
        "event_declaration",
        "enum_member_declaration",
        "local_function_statement",
    }
)
#: What only a wrapper's declaration of this kind can be.
_WRAPPER_DECLARATIONS = ("method_declaration", "property_declaration")
_SCOPE_TYPES = _OWNERS | {"interface_declaration"}
_ACCESS = frozenset({"public", "protected", "internal", "private", "file"})
#: Modifiers through which a member may be, or replace, another member.
_OVERRIDES = frozenset({"virtual", "override", "abstract", "new", "sealed", "extern", "partial"})


def parse(source: str) -> Node:
    return syntax._parse(source, "csharp")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _named(node: Node) -> list[Node]:
    return [c for c in node.named_children if c.type not in _COMMENTS]


def _child(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def _name_of(node: Node) -> str | None:
    name = node.child_by_field_name("name")
    return _text(name) if name is not None and name.type == "identifier" else None


def _top_level(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("compilation_unit", "file_scoped_namespace_declaration"):
        return True
    return (
        parent.type == "declaration_list"
        and parent.parent is not None
        and parent.parent.type == "namespace_declaration"
    )


def _attributes(member: Node) -> tuple[str, ...]:
    """Simple names of the member's attributes, any ``Attribute`` suffix dropped."""
    names: list[str] = []
    for attribute_list in member.named_children:
        if attribute_list.type != "attribute_list":
            continue
        for attribute in attribute_list.named_children:
            name = attribute.child_by_field_name("name")
            if attribute.type != "attribute" or name is None:
                continue
            simple = re.split(r"[.:]", _text(name))[-1]
            names.append(simple.removesuffix("Attribute") or simple)
    return tuple(sorted(names))


# --- Discovery ------------------------------------------------------------------


def _plain_string(node: Node) -> str | None:
    if node.type != "string_literal":
        return None
    parts = _named(node)
    if len(parts) != 1 or parts[0].type != "string_literal_content":
        return None
    return _text(parts[0])


def _name_text(node: Node) -> str | None:
    """``NAME`` or ``Owner.Chain.NAME`` exactly as written, else ``None``."""
    if node.type == "identifier":
        return _text(node)
    if node.type == "member_access_expression":
        parts = xcs._canonical(node)
        if parts is not None and _text(node) == ".".join(parts):
            return _text(node)
    return None


def read_test(
    path: str,
    source: str,
    flag_key: str,
    accessors: tuple[str, ...],
    const_ok: bool,
    external: Iterable[str],
    *,
    loose: bool = False,
) -> Callable[[Node], bool]:
    """Whether an ``invocation_expression`` is a synchronous read of
    ``flag_key`` this run folds.

    ``loose`` also accepts any name or ``Owner.NAME`` as the key -- a
    superset, for widening the undo snapshot before anything is resolved."""
    root = parse(source)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "csharp", flag_key)}
        if const_ok
        else set()
    )
    proven = set(external)
    names = _READ_CALLEES | set(accessors)

    def is_read(node: Node) -> bool:
        if node.type != "invocation_expression":
            return False
        function = node.child_by_field_name("function")
        if function is not None and function.type == "member_access_expression":
            function = function.child_by_field_name("name")
        arguments = node.child_by_field_name("arguments")
        if function is None or function.type != "identifier" or arguments is None:
            return False
        if _text(function) not in names:
            return False
        args = [c for c in arguments.named_children if c.type == "argument"]
        if not args or not args[0].named_children:
            return False
        first = args[0].named_children[-1]
        if _plain_string(first) == flag_key:
            return True
        text = _name_text(first)
        return text is not None and (
            loose or text in proven or (first.type == "identifier" and text in consts)
        )

    return is_read


def _single_read(body: Node | None, is_read: Callable[[Node], bool]) -> bool:
    """Whether ``body`` is ``=> <read>`` or ``{ return <read>; }``."""
    if body is None:
        return False
    if body.type == "arrow_expression_clause":
        parts = _named(body)
        return len(parts) == 1 and is_read(parts[0])
    if body.type != "block":
        return False
    statements = _named(body)
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    returned = _named(statements[0])
    return len(returned) == 1 and is_read(returned[0])


def _getter_body(prop: Node) -> Node | None:
    """The body of a get-only property: its expression body, or its lone
    ``get`` accessor's. ``None`` for anything with a setter, an ``init``, an
    initializer or more than one accessor."""
    value = prop.child_by_field_name("value")
    accessors = prop.child_by_field_name("accessors")
    if value is not None:
        return value if value.type == "arrow_expression_clause" and accessors is None else None
    if accessors is None:
        return None
    parts = [c for c in _named(accessors) if c.type == "accessor_declaration"]
    if len(parts) != 1 or len(_named(accessors)) != 1:
        return None
    accessor = parts[0]
    name = accessor.child_by_field_name("name")
    if name is None or _text(name) != "get" or _child(accessor, "modifier") is not None:
        return None
    return accessor.child_by_field_name("body")


def _plain_parameters(method: Node) -> bool:
    """No extension ``this`` and no ``ref``/``out`` parameter."""
    parameters = method.child_by_field_name("parameters")
    for parameter in parameters.named_children if parameters is not None else ():
        if parameter.type != "parameter":
            continue
        if any(c.type == "modifier" and _text(c) in ("this", "ref", "out") for c in parameter.children):
            return False
    return True


def _is_bool(type_node: Node | None) -> bool:
    return type_node is not None and type_node.type == "predefined_type" and _text(type_node) == "bool"


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    found: list[wi.Wrapper] = []
    for owner in cross_file_keys.walk(root):
        if owner.type not in _OWNERS or not _top_level(owner):
            continue
        owner_name = _name_of(owner)
        body = owner.child_by_field_name("body")
        if owner_name is None or body is None or _child(owner, "type_parameter_list") is not None:
            continue
        namespace = xcs._namespace_of(owner)
        for member in body.named_children:
            if member.type not in _WRAPPER_DECLARATIONS:
                continue
            words = xcs._modifiers(member)
            name = _name_of(member)
            if (
                name is None
                or name.startswith("@")
                or words & {"abstract", "extern", "partial", "async"}
                or _child(member, "explicit_interface_specifier") is not None
                or _child(member, "type_parameter_list") is not None
            ):
                continue
            if member.type == "method_declaration":
                if not _is_bool(member.child_by_field_name("returns")) or not _plain_parameters(member):
                    continue
                if not _single_read(member.child_by_field_name("body"), is_read):
                    continue
                kind = "method"
            else:
                if not _is_bool(member.child_by_field_name("type")):
                    continue
                if not _single_read(_getter_body(member), is_read):
                    continue
                kind = "property"
            found.append(
                wi.Wrapper(
                    path=path,
                    language="csharp",
                    name=name,
                    owner=owner_name,
                    package=namespace,
                    line=wi.line_of(member),
                    start=member.start_byte,
                    end=member.end_byte,
                    static="static" in words,
                    annotations=_attributes(member),
                    kind=kind,
                )
            )
    return found


# --- The repository index -----------------------------------------------------


@dataclass
class TypeDecl:
    path: str
    namespace: str
    name: str
    #: ``class``, ``struct``, ``interface``, ``enum`` or ``other``.
    kind: str
    top_level: bool
    #: Reachable from another assembly: ``public``/``protected``, or a member
    #: of an interface with no access modifier of its own.
    public: bool
    #: ``abstract``: something may, and must, extend it.
    open: bool
    #: Declared ``partial``: another part may add members and bases.
    partial: bool
    generic: bool
    supertypes: tuple[str, ...]
    #: member name -> byte span, kept apart by kind so a method never
    #: matches a property.
    methods: dict[str, tuple[int, int]] = field(default_factory=dict)
    properties: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class Index:
    repo_dir: str = ""
    #: member name -> ``(path, start byte)`` of every declaration of it: a
    #: method, property, field, event, enum member, local function or record
    #: parameter.
    members: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    types: dict[str, list[TypeDecl]] = field(default_factory=lambda: defaultdict(list))
    #: Simple names something in the repository extends or implements.
    subclassed: set[str] = field(default_factory=set)
    #: type simple name -> how many types list it as a base.
    implementations: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Modifier words on the wrapper's own declaration.
    wrapper_words: set[str] = field(default_factory=set)
    #: A file mentioning a word could not be read, or holds the word inside a
    #: region it could not parse: nothing about uniqueness can be trusted.
    unreadable: bool = False
    #: Every C# file mentioning a word, resolved: where call sites are.
    parsed: list[str] = field(default_factory=list)
    #: VB and F# files mentioning a word.
    foreign: list[str] = field(default_factory=list)
    #: Files of any other kind that mention the wrapper's name: Razor, XAML,
    #: a config file. Nothing here can parse them.
    unparsed: list[str] = field(default_factory=list)
    #: Unused here: C# has no second spelling of a member's name.
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)

    @classmethod
    def build(cls, repo_dir: str, wrapper: wi.Wrapper) -> Index:
        index = cls(repo_dir=repo_dir)
        source = _read(wrapper.path)
        if source is None:
            index.unreadable = True
            return index
        root = parse(source)
        owner = next(
            (t for t in _types_in(wrapper.path, root) if t.name == wrapper.owner and t.top_level), None
        )
        member = next(
            (n for n in cross_file_keys.walk(root) if n.start_byte == wrapper.start and n.type in _WRAPPER_DECLARATIONS),
            None,
        )
        index.wrapper_words = xcs._modifiers(member) if member is not None else set()
        words = {wrapper.name, wrapper.owner, *(owner.supertypes if owner else ())}
        types = words - {wrapper.name}
        for path in cross_file_keys.mention_files(repo_dir, words, exclude=()):
            if path.endswith(EXTENSIONS):
                text = _read(path)
                if text is None:
                    logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                    index.unreadable = True
                    continue
                index.parsed.append(path)
                index._add(path, text, words)
                continue
            text = _read(path)
            if text is None:
                index.unreadable = True
                continue
            fold = re.IGNORECASE if path.lower().endswith(CASE_INSENSITIVE_SUFFIXES) else 0
            if path.lower().endswith(_GUESTS):
                for name in types:
                    count = _guest_implementations(text, name)
                    if count:
                        index.subclassed.add(name)
                        index.implementations[name] += count
            if path.lower().endswith(FOREIGN_EXTENSIONS):
                index.foreign.append(path)
            elif re.search(rf"(?<![\w$]){re.escape(wrapper.name)}(?![\w$])", text, fold):
                index.unparsed.append(path)
        return index

    def _add(self, path: str, text: str, words: set[str]) -> None:
        root = parse(text)
        if root.has_error and _word_in_error(root, words):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        for decl in _types_in(path, root):
            self.types[decl.name].append(decl)
            for supertype in decl.supertypes:
                self.subclassed.add(supertype)
                self.implementations[supertype] += 1
        for node in cross_file_keys.walk(root):
            for name in _member_names(node):
                # `@IsEnabled` declares `IsEnabled`.
                self.members[name.removeprefix("@")].append((path, node.start_byte))


def _guest_implementations(text: str, name: str) -> int:
    """How many types a file this tool does not parse declares as extending or
    implementing ``name``: VB ``Inherits``/``Implements`` (any case), Razor
    ``@inherits``/``@implements``, F# ``inherit X`` and ``interface X with``,
    and a Web Forms ``Inherits="X"``. Not parsed: a word match on the
    declaration forms, which errs toward counting one too many."""
    word = re.escape(name)
    qualified = rf"(?:global::)?(?:[\w]+\.)*{word}\b"
    forms = (
        rf"(?<![\w.])@?(?:inherits|implements|inherit)\s+(?:[\w.<>(),\s]+,\s*)?{qualified}",
        rf"\binterface\s+{qualified}\s+with\b",
        rf"\binherits\s*=\s*[\"'][\w.]*\b{word}[\"']",
    )
    return sum(len(re.findall(form, text, re.IGNORECASE)) for form in forms)


def _member_names(node: Node) -> list[str]:
    """The member names a declaration node binds."""
    if node.type in _NAMED_MEMBERS:
        name = _name_of(node)
        return [name] if name is not None else []
    if node.type == "variable_declarator":
        declaration = node.parent
        holder = declaration.parent if declaration is not None else None
        if holder is not None and holder.type in ("field_declaration", "event_field_declaration"):
            name = _name_of(node)
            return [name] if name is not None else []
        return []
    if node.type == "parameter_list" and node.parent is not None and node.parent.type in (
        "record_declaration",
        "record_struct_declaration",
    ):
        # A record's positional parameters are its properties.
        return [n for p in node.named_children if p.type == "parameter" and (n := _name_of(p))]
    return []


def _types_in(path: str, root: Node) -> list[TypeDecl]:
    found: list[TypeDecl] = []
    for node in cross_file_keys.walk(root):
        kind = _TYPES.get(node.type)
        name = _name_of(node)
        if kind is None or name is None:
            continue
        words = xcs._modifiers(node)
        in_interface = (
            node.parent is not None
            and node.parent.type == "declaration_list"
            and node.parent.parent is not None
            and node.parent.parent.type == "interface_declaration"
        )
        body = node.child_by_field_name("body")
        methods: dict[str, tuple[int, int]] = {}
        properties: dict[str, tuple[int, int]] = {}
        for member in body.named_children if body is not None else ():
            member_name = _name_of(member) if member.type in _WRAPPER_DECLARATIONS else None
            if member_name is None:
                continue
            target = methods if member.type == "method_declaration" else properties
            target.setdefault(member_name, (member.start_byte, member.end_byte))
        found.append(
            TypeDecl(
                path=path,
                namespace=xcs._namespace_of(node),
                name=name,
                kind=kind,
                top_level=_top_level(node),
                public=bool(words & {"public", "protected"}) or (in_interface and not words & _ACCESS),
                open="abstract" in words,
                partial="partial" in words,
                generic=_child(node, "type_parameter_list") is not None,
                supertypes=xcs._supertypes(node),
                methods=methods,
                properties=properties,
            )
        )
    return found


def _word_in_error(root: Node, words: set[str]) -> bool:
    for node in cross_file_keys.walk(root):
        if node.type == "ERROR" or node.is_missing:
            text = _text(node)
            if any(word in text for word in words):
                return True
    return False


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class _Readers:
    """One namespace-lookup reader per file, for walks that cross files."""

    def __init__(self, repo_dir: str):
        self.repo_dir = repo_dir
        self._by_path: dict[str, xcs._Reader | None] = {}

    def add(self, reader: xcs._Reader) -> None:
        self._by_path[reader.path] = reader

    def get(self, path: str) -> xcs._Reader | None:
        if path not in self._by_path:
            source = _read(path)
            self._by_path[path] = (
                None if source is None else xcs._Reader(parse(source), path, self.repo_dir)
            )
        return self._by_path[path]


def _base_nodes(type_node: Node) -> list[Node]:
    """The type nodes of a declaration's base list."""
    bases = _child(type_node, "base_list")
    found: list[Node] = []
    for child in bases.named_children if bases is not None else ():
        if child.type == "primary_constructor_base_type" and child.named_children:
            child = child.named_children[0]
        if child.type not in _COMMENTS and child.type != "argument_list":
            found.append(child)
    return found


def _type_full(type_node: Node, reader: xcs._Reader) -> str | None:
    """The full name of the repository type ``type_node`` can only mean, by
    C#'s namespace lookup from where it is written; ``None`` when it may be a
    library type, a generic one, or something nearer."""
    node = type_node
    while node.type == "nullable_type" and node.named_children:
        node = node.named_children[0]
    if node.type == "identifier":
        name = _text(node)
        if _type_shadowed(node, name, reader):
            return None
        resolved = reader.resolve(name, xcs._levels(node, reader.root, reader.unit_extra))
        if resolved is None:
            return None
        namespace, chain = resolved
        return f"{namespace}.{chain}" if namespace else chain
    if node.type in ("qualified_name", "alias_qualified_name"):
        dotted = xcs._dotted(node)
        if dotted is None:
            return None
        first = dotted.split(".", 1)[0]
        if node.type == "alias_qualified_name" or (
            not _type_shadowed(node, first, reader) and _first_clear(first, node, reader)
        ):
            return dotted
    return None


def _find_type(root: Node, chain: str) -> Node | None:
    parts = chain.split(".")
    return next(
        (n for n in cross_file_keys.walk(root) if n.type in _TYPES and xcs._type_chain(n, strict=False) == parts),
        None,
    )


def _bases_in_repository(readers: _Readers, path: str, type_node: Node, seen: set[str] | None = None) -> bool:
    """Whether every base type of ``type_node``, followed transitively, is a
    repository type, resolved by namespace from the file declaring each. One
    that is not may declare the wrapper's name, and C# implements an interface
    member with no keyword at all. A simple name shared with an unrelated
    repository type is not enough: the lookup has to reach it.

    A base declared only in a file that does not parse whole is missing from
    the checkout index, so it is refused here like a library type."""
    seen = set() if seen is None else seen
    reader = readers.get(path)
    if reader is None:
        return False
    checkout = xcs.csharp_index(readers.repo_dir)
    for base in _base_nodes(type_node):
        full = _type_full(base, reader)
        infos = [i for i in checkout.by_full.get(full or "", ()) if not i.generic]
        if full is None or len(infos) != 1 or infos[0].partial:
            return False
        if full in seen:
            continue
        seen.add(full)
        declaring = readers.get(infos[0].path)
        declaration = _find_type(declaring.root, infos[0].chain) if declaring is not None else None
        if declaration is None or not _bases_in_repository(readers, infos[0].path, declaration, seen):
            return False
    return True


#: Project properties that pack a project, or describe the package it packs to.
_PACKING = re.compile(
    r"<(?:IsPackable\b[^>]*>\s*(?!false\b)|PackageId\b|GeneratePackageOnBuild\b[^>]*>\s*(?!false\b)"
    r"|PackageLicense\w*\b|PackageProjectUrl\b|PackageReadmeFile\b|PackageTags\b|PackageIcon\b"
    r"|Import\b)",
    re.IGNORECASE,
)
#: A build script or workflow that packs or pushes a package: any project may
#: be the one it packs.
_PACK_STEP = re.compile(r"\bdotnet\s+pack\b|\bnuget\s+(?:push|pack)\b|[-/]t(?:arget)?:\s*pack\b", re.IGNORECASE)
_SCRIPTS = {".yml", ".yaml", ".sh", ".ps1", ".psm1", ".cmd", ".bat", ".cake", ".props", ".targets", ".proj"}
_FRIENDS = re.compile(r"InternalsVisibleTo\b")


def _packs_anywhere(repo_dir: str) -> bool:
    """Whether the checkout holds a ``.nuspec`` or a step that packs.

    Walked rather than taken from the source listing, which holds source
    suffixes only; the walk prunes the same generated directories."""
    from flag_cleanup import piranha_runner, source_index

    root = Path(source_index.checkout_root(repo_dir))
    for path in piranha_runner.source_files(root, _SCRIPTS | {".nuspec"}, piranha_runner._SKIP_DIRS):
        if path.suffix.lower() == ".nuspec":
            return True
        text = _read(str(path))
        if text is None or _PACK_STEP.search(text):
            return True
    return False


def _has_friends(path: str, repo_dir: str) -> bool:
    """Whether the project ``path`` belongs to grants its internals to another
    assembly (``InternalsVisibleTo``), which may live outside the repository.
    A file outside every project is judged to have friends."""
    from flag_cleanup import source_index

    projects = xcs._project_dirs(repo_dir)
    project = xcs._project_of(path, projects)
    if project is None:
        return True
    for source in source_index.files(repo_dir, {".cs", ".csproj"}, with_ignored=False):
        resolved = Path(source.resolved)
        if xcs._project_of(str(resolved), projects) != project:
            continue
        text = _read(str(resolved))
        if text is None or _FRIENDS.search(text):
            return True
    return False


def published(path: str, repo_dir: str) -> bool:
    """Whether the project ``path`` belongs to may be packed, and its public
    types implemented outside the repository.

    Answered from its ``.csproj`` and every ``Directory.Build.props`` or
    ``.targets`` between it and the repository root, and from the whole
    checkout: a ``.nuspec`` or a build step that packs (``dotnet pack``,
    ``nuget push``, ``-t:Pack``) anywhere may pack this project. Anything
    unreadable, imported, or a file outside every project, counts as
    published.
    """
    from flag_cleanup import source_index

    projects = xcs._project_dirs(repo_dir)
    project = xcs._project_of(path, projects)
    if project is None or _packs_anywhere(repo_dir):
        return True
    root = Path(source_index.checkout_root(repo_dir)).resolve()
    files = sorted(project.glob("*.csproj"))
    for directory in (project, *project.parents):
        files += [directory / name for name in ("Directory.Build.props", "Directory.Build.targets")]
        if directory == root:
            break
    for file in files:
        if not file.exists():
            continue
        text = _read(str(file))
        if text is None or _PACKING.search(text):
            return True
    return False


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | None]:
    """The wrapper with its interface member filled in, or why it does not qualify."""
    if index.unreadable:
        return None, wi.NAME_NOT_UNIQUE
    owners = index.types.get(wrapper.owner, [])
    if len(owners) != 1 or owners[0].partial:
        return None, wi.NAME_NOT_UNIQUE
    owner = owners[0]
    if index.wrapper_words & _OVERRIDES:
        return None, wi.OVERRIDABLE
    readers = _Readers(index.repo_dir)
    reader = readers.get(wrapper.path)
    owner_node = _find_type(reader.root, wrapper.owner) if reader is not None else None
    if owner_node is None or not _bases_in_repository(readers, wrapper.path, owner_node):
        return None, wi.OVERRIDABLE
    member = wrapper
    declared_in = (lambda c: c.properties) if wrapper.kind == "property" else (lambda c: c.methods)
    for base in _base_nodes(owner_node):
        full = _type_full(base, reader) or ""
        namespace, _, simple = full.rpartition(".")
        candidates = [
            t for t in index.types.get(simple, []) if t.namespace == namespace and t.top_level
        ]
        if not candidates:
            # A nested base type: anything it declares under this name is a
            # member the wrapper may implement or hide.
            info = xcs.csharp_index(index.repo_dir).by_full.get(full, ())
            if any(wrapper.name in i.members for i in info):
                return None, wi.OVERRIDABLE
            continue
        if not any(wrapper.name in declared_in(c) for c in candidates):
            continue
        if len(candidates) != 1:
            return None, wi.NAME_NOT_UNIQUE
        interface = candidates[0]
        if interface.kind != "interface":
            # A base class in the repository declares the name.
            return None, wi.OVERRIDABLE
        if (
            interface.partial
            or interface.generic
            or interface.supertypes
            or index.implementations.get(simple, 0) != 1
            or (
                (interface.public or _has_friends(interface.path, index.repo_dir))
                and published(interface.path, index.repo_dir)
            )
        ):
            return None, wi.INTERFACE_PUBLISHED
        start, end = declared_in(interface)[wrapper.name]
        member = replace(
            wrapper,
            interface=wi.InterfaceMember(interface.path, interface.name, interface.namespace, start, end),
        )
        break
    interface_at = None if member.interface is None else (member.interface.path, member.interface.start)
    declared = [d for d in index.members.get(wrapper.name, []) if d != interface_at]
    if len(declared) != 1:
        return None, wi.NAME_NOT_UNIQUE
    if owner.open or wrapper.owner in index.subclassed:
        return None, wi.OVERRIDABLE
    return member, None


# --- Call sites -----------------------------------------------------------------

_STOP = "stop"
_UNTYPED = "untyped"
#: Scopes with parameters of their own.
_FUNCTIONS = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "local_function_statement",
        "operator_declaration",
        "conversion_operator_declaration",
        "indexer_declaration",
        "lambda_expression",
        "anonymous_method_expression",
        "delegate_declaration",
    }
)
#: The member a pattern variable's scope is judged within.
_MEMBERS = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "destructor_declaration",
        "operator_declaration",
        "conversion_operator_declaration",
        "indexer_declaration",
        "property_declaration",
        "field_declaration",
        "event_declaration",
        "accessor_declaration",
    }
)
_SIDE_EFFECTS = frozenset(
    {
        "invocation_expression",
        "object_creation_expression",
        "implicit_object_creation_expression",
        "anonymous_object_creation_expression",
        "array_creation_expression",
        "implicit_array_creation_expression",
        "stackalloc_expression",
        "implicit_stackalloc_expression",
        "assignment_expression",
        "postfix_unary_expression",
        "lambda_expression",
        "anonymous_method_expression",
        "await_expression",
        "element_access_expression",
        "element_binding_expression",
        "with_expression",
        "throw_expression",
        "query_expression",
        "declaration_expression",
    }
)


def _field(parent: Node, child: Node) -> str | None:
    return xcs._field(parent, child)


def _creation_type(value: Node | None) -> Node | str:
    """The type ``var x = <value>`` binds: a ``new C(...)``'s ``C``."""
    if value is not None and value.type == "object_creation_expression":
        type_node = value.child_by_field_name("type")
        if type_node is not None:
            return type_node
    return _UNTYPED


def _declarator_type(declaration: Node, name: str) -> Node | str | None:
    """The type a ``variable_declaration`` binds ``name`` to, if it binds it."""
    type_node = declaration.child_by_field_name("type")
    for declarator in declaration.named_children:
        if declarator.type != "variable_declarator" or _name_of(declarator) != name:
            continue
        if type_node is None:
            return _UNTYPED
        if type_node.type == "implicit_type":
            values = [c for c in _named(declarator) if c.id != declarator.child_by_field_name("name").id]
            return _creation_type(values[-1] if values else None)
        return type_node
    return None


def _parameter_type(parameters: Node | None, name: str) -> Node | str | None:
    if parameters is None:
        return None
    if parameters.type == "implicit_parameter":
        return _UNTYPED if _text(parameters) == name else None
    if parameters.type == "identifier":
        return _UNTYPED if _text(parameters) == name else None
    for parameter in parameters.named_children:
        if parameter.type in ("parameter", "implicit_parameter") and _name_of(parameter) == name:
            return parameter.child_by_field_name("type") or _UNTYPED
        if parameter.type == "implicit_parameter" and _text(parameter) == name:
            return _UNTYPED
    return None


def _member_binding(scope: Node, name: str) -> Node | str | None:
    """What a type's own members bind ``name`` to: a field's or property's
    type, ``_UNTYPED`` for any other member of that name."""
    body = scope.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        if member.type in ("field_declaration", "event_field_declaration"):
            declaration = _child(member, "variable_declaration")
            found = _declarator_type(declaration, name) if declaration is not None else None
            if found is not None:
                return found if member.type == "field_declaration" else _UNTYPED
        elif member.type == "property_declaration" and _name_of(member) == name:
            return member.child_by_field_name("type") or _UNTYPED
        elif member.type in (*_NAMED_MEMBERS, *_TYPES) and _name_of(member) == name:
            return _UNTYPED
    return None


def _primary_parameter(scope: Node, name: str) -> Node | str | None:
    return _parameter_type(_child(scope, "parameter_list"), name)


def _inherits(scope: Node, name: str, readers: _Readers, path: str) -> bool:
    """Whether a base of the type, or another part of it, may declare ``name``."""
    if "partial" in xcs._modifiers(scope):
        return True
    if not _base_nodes(scope):
        return False
    if not _bases_in_repository(readers, path, scope):
        return True
    return name in xcs.csharp_index(readers.repo_dir).inherited_members(xcs._supertypes(scope))


def _in_global_statement(at: Node) -> bool:
    scope = at.parent
    while scope is not None:
        if scope.type == "global_statement":
            return True
        if scope.type in _TYPES:
            return False
        scope = scope.parent
    return False


def _pattern_binds(at: Node, name: str) -> bool:
    """Whether a pattern, ``out var``, deconstruction or query variable named
    ``name`` is declared in the member around ``at`` (or anywhere in the
    file's top-level statements, for a read among them). Their scopes follow
    flow rather than blocks, so any one makes the name unprovable."""
    scope = at.parent
    while scope is not None and scope.type not in _MEMBERS and scope.type not in _SCOPE_TYPES:
        if scope.type == "compilation_unit":
            break
        scope = scope.parent
    if scope is None or scope.type in _SCOPE_TYPES:
        return False
    if scope.type == "compilation_unit":
        if not _in_global_statement(at):
            return False
        regions = [c for c in scope.named_children if c.type == "global_statement"]
    else:
        regions = [scope]
    for region in regions:
        for node in cross_file_keys.walk(region):
            if node.type != "identifier" or _text(node) != name or node.parent is None:
                continue
            parent = node.parent
            if (
                parent.type.endswith(("_pattern", "_designation"))
                or parent.type in ("declaration_expression", "from_clause", "let_clause", "join_clause")
                or parent.type == "query_continuation"
                or (parent.type == "foreach_statement" and _field(parent, node) == "left")
                or (parent.type == "catch_declaration")
            ):
                return True
    return False


def _statements_in_scope(scope: Node) -> list[Node]:
    """The statements one local-variable scope holds. A switch block is ONE
    scope across its sections: a local declared under one label is in scope
    under the next."""
    if scope.type == "switch_section" and scope.parent is not None:
        return [
            statement
            for section in scope.parent.named_children
            if section.type == "switch_section"
            for statement in section.named_children
        ]
    return list(scope.named_children)


def _local_binding(statements: list[Node], at: Node, name: str) -> Node | str | None:
    """What the locals and local functions among ``statements`` bind ``name``
    to at ``at``. A local declared after ``at`` cannot be read there, so only
    one declared before counts; a local function is in scope throughout."""
    found: Node | str | None = None
    for statement in statements:
        if statement.type == "global_statement" and statement.named_children:
            statement = statement.named_children[0]
        if statement.type == "local_function_statement" and _name_of(statement) == name:
            return _UNTYPED
        if statement.type == "local_declaration_statement" and statement.start_byte < at.start_byte:
            declaration = _child(statement, "variable_declaration")
            if declaration is not None:
                found = _declarator_type(declaration, name) or found
    return found


def _binding(
    at: Node,
    name: str,
    readers: _Readers,
    path: str,
    *,
    class_only: bool = False,
    locals_only: bool = False,
) -> Node | str | None:
    """The type ``name`` is bound to at ``at``: a type node, ``_UNTYPED``,
    ``_STOP``, or ``None`` when nothing in the file binds it.

    ``locals_only`` stops at the first type without looking at its members,
    for a bare read of the wrapper inside its own class; there a primary
    constructor parameter of the name still counts, since it wins over the
    member in initializers."""
    if not class_only and _pattern_binds(at, name):
        return _UNTYPED
    scope = at.parent
    while scope is not None:
        found: Node | str | None = None
        if not class_only:
            if scope.type in ("block", "switch_section"):
                found = _local_binding(_statements_in_scope(scope), at, name)
            elif scope.type in ("lambda_expression", "anonymous_method_expression"):
                # A lambda's parameter, typed or not, may be the parameter of
                # an expression tree, where the call is the payload.
                if _parameter_type(scope.child_by_field_name("parameters"), name) is not None:
                    found = _UNTYPED
            elif scope.type in _FUNCTIONS:
                found = _parameter_type(scope.child_by_field_name("parameters"), name)
            elif scope.type == "accessor_declaration" and name == "value":
                found = _UNTYPED
            elif scope.type == "foreach_statement":
                left = scope.child_by_field_name("left")
                if left is not None and left.type == "identifier" and _text(left) == name:
                    type_node = scope.child_by_field_name("type")
                    found = type_node if type_node is not None and type_node.type != "implicit_type" else _UNTYPED
            elif scope.type in ("for_statement", "using_statement", "fixed_statement"):
                # Only the statement's own header declares here; a nested
                # block's locals are out of scope at ``at``.
                for part in scope.named_children:
                    if part.type == "variable_declaration" and part.start_byte < at.start_byte:
                        found = _declarator_type(part, name) or found
            elif scope.type == "compilation_unit" and _in_global_statement(at):
                found = _local_binding(
                    [c for c in scope.named_children if c.type == "global_statement"], at, name
                )
        if scope.type in _SCOPE_TYPES:
            if locals_only:
                return found if found is not None else (_UNTYPED if _primary_parameter(scope, name) is not None else None)
            if found is None:
                member = _member_binding(scope, name)
                parameter = _primary_parameter(scope, name)
                if member is not None:
                    # A member shadows the parameter in bodies; the parameter
                    # wins in initializers. Both: which one is unprovable here.
                    found = _UNTYPED if parameter is not None else member
                elif _inherits(scope, name, readers, path):
                    return _STOP
                else:
                    found = parameter
            class_only = True
        if found is not None:
            return found
        scope = scope.parent
    return None


def _enclosing_type(at: Node) -> Node | None:
    scope = at.parent
    while scope is not None and scope.type not in _TYPES:
        scope = scope.parent
    return scope


def _in_owner(at: Node, wrapper: wi.Wrapper, path: str) -> bool:
    """Whether ``at`` sits directly in the wrapper's own class: not in a
    nested type, whose own members and bases come first."""
    scope = _enclosing_type(at)
    return (
        scope is not None
        and path == wrapper.path
        and _top_level(scope)
        and scope.start_byte <= wrapper.start < scope.end_byte
    )


def _type_shadowed(at: Node, name: str, reader: xcs._Reader) -> bool:
    """Whether something nearer than a namespace may be the type ``name``
    at ``at``: a type parameter, or a nested type of an enclosing type, its
    other parts (a markup half included) or its repository bases."""
    if name in reader.part_members or xcs._has_markup_part(reader.path):
        return True
    scope = at.parent
    while scope is not None:
        parameters = _child(scope, "type_parameter_list")
        if parameters is not None and any(_text(p) == name for p in parameters.named_children):
            return True
        if scope.type in _TYPES:
            body = scope.child_by_field_name("body")
            for member in body.named_children if body is not None else ():
                if member.type in _TYPES and _name_of(member) == name:
                    return True
            supertypes = xcs._supertypes(scope)
            if supertypes and name in reader.index.inherited_members(supertypes):
                return True
        scope = scope.parent
    return False


def _first_clear(first: str, at: Node, reader: xcs._Reader) -> bool:
    """Whether the first name of a qualified type can only mean the global
    namespace of that name: no nearer namespace, type or alias claims it."""
    levels = xcs._levels(at, reader.root, reader.unit_extra)
    for level in levels:
        if any(u.kind == "alias" and u.alias == first for u in level.usings):
            return False
        if level.unit:
            continue
        if f"{level.name}.{first}" in reader.index.namespaces or reader._types_named(level.name, first):
            return False
    return True


def _resolves(type_node: Node, reader: xcs._Reader, targets: set[str]) -> str | None:
    """``None`` when a declared type, resolved where it is written, can only
    mean the wrapper's class or interface (``targets``, full names);
    otherwise the reason it does not prove the call."""
    if _type_full(type_node, reader) in targets:
        return None
    node = type_node
    while node.type == "nullable_type" and node.named_children:
        node = node.named_children[0]
    spelled = xcs._dotted(node) if node.type != "identifier" else _text(node)
    simple = spelled.rsplit(".", 1)[-1] if spelled else None
    names = {target.rsplit(".", 1)[-1] for target in targets}
    return wi.IMPORT_UNRESOLVED if simple in names else wi.RECEIVER_UNPROVEN


#: Parents under which a bare identifier names a member of ANOTHER object: a
#: ``?.`` binding, an object or ``with`` initializer's target, a property
#: pattern's subpattern.
_FOREIGN_MEMBER_PARENTS = frozenset({"member_binding_expression", "subpattern", "with_initializer"})


def _names_a_member(access: Node) -> bool:
    """Whether a read's own spelling also names something: an anonymous
    object member or a tuple element with no explicit name takes its name
    from the read (``new { f.IsX }``), and a literal there does not compile."""
    parent = access.parent
    if parent is None:
        return False
    if parent.type == "anonymous_object_creation_expression":
        previous = access.prev_sibling
        return previous is None or previous.type != "="
    return (
        parent.type == "argument"
        and parent.child_by_field_name("name") is None
        and parent.parent is not None
        and parent.parent.type == "tuple_expression"
    )


def _argument_has_call(call: Node) -> bool:
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return False
    for node in cross_file_keys.walk(arguments):
        if node.id == arguments.id:
            continue
        if node.type in _SIDE_EFFECTS:
            return True
        if node.type == "prefix_unary_expression" and any(c.type in ("++", "--") for c in node.children):
            return True
        if node.type == "argument" and any(c.type in ("ref", "out") for c in node.children if not c.is_named):
            return True
    return False


def _in_test_double(at: Node, data: bytes) -> bool:
    """Whether ``at`` sits in a statement of a mocking DSL.

    The statement is the nearest one directly in a block; an expression-bodied
    member or a field initializer is judged on its own declaration rather than
    on the whole file, which is what a walk to the next block would reach."""
    node = at
    while node.parent is not None and node.parent.type != "block" and node.type not in _MEMBERS:
        node = node.parent
    return bool(mock_stubs._DSL.search(data[node.start_byte : node.end_byte].decode("utf-8", "replace")))


def _in_nameof(at: Node) -> bool:
    scope = at.parent
    while scope is not None and scope.type not in _MEMBERS:
        if scope.type == "invocation_expression":
            function = scope.child_by_field_name("function")
            if function is not None and function.type == "identifier" and _text(function) == "nameof":
                return True
        scope = scope.parent
    return False


def _cref_mentions(root: Node, name: str) -> list[Node]:
    """Comments whose ``cref`` names a member called ``name``: deleting the
    member would leave a broken reference (CS1574)."""
    found: list[Node] = []
    for node in cross_file_keys.walk(root):
        if node.type != "comment":
            continue
        for match in xcs._CREF.finditer(_text(node)):
            target = match.group(1).split("(", 1)[0]
            if target.rsplit(".", 1)[-1] == name:
                found.append(node)
                break
    return found


def names_only(node: Node, kind: str = "method") -> bool:
    """Whether an identifier only names something: the declared name of a
    method or property, or a named-argument label."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _WRAPPER_DECLARATIONS and parent.child_by_field_name("name") == node:
        return True
    return parent.type == "argument" and _field(parent, node) == "name"


def members(root: Node, name: str) -> list[Node]:
    """Every method or property of ``name`` declared in a type body."""
    return [
        n
        for n in cross_file_keys.walk(root)
        if n.type in _WRAPPER_DECLARATIONS
        and _name_of(n) == name
        and n.parent is not None
        and n.parent.type == "declaration_list"
    ]


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    root = parse(source)
    data = source.encode("utf-8")
    reader = xcs._Reader(root, path, index.repo_dir)
    readers = _Readers(index.repo_dir)
    readers.add(reader)
    targets = {f"{wrapper.package}.{wrapper.owner}" if wrapper.package else wrapper.owner}
    if wrapper.interface is not None:
        interface = wrapper.interface
        targets.add(f"{interface.package}.{interface.name}" if interface.package else interface.name)
    found: list[wi.Occurrence] = [
        wi.Occurrence(path, wi.line_of(c), wi.text_of(c), None, wi.UNPARSED_MENTION)
        for c in _cref_mentions(root, wrapper.name)
    ]
    # A verbatim identifier (`f.@IsXEnabled`) is the same name; it is not
    # proven, and it keeps the wrapper.
    found += [
        wi.Occurrence(path, wi.line_of(n), wi.text_of(n.parent or n), None, wi.RECEIVER_UNPROVEN)
        for n in reader.occurrences("@" + wrapper.name)
        if n.type == "identifier" and not names_only(n)
    ]
    for node in reader.occurrences(wrapper.name):
        if node.type != "identifier" or names_only(node) or xcs._in_directive(node):
            continue
        parent = node.parent
        line = wi.line_of(node)
        receiver: Node | None = None
        access: Node
        if parent is not None and parent.type == "member_access_expression" and _field(parent, node) == "name":
            access = parent
            receiver = parent.child_by_field_name("expression")
        else:
            access = node
        call = access.parent if access.parent is not None and access.parent.type == "invocation_expression" else None
        called = call is not None and call.child_by_field_name("function") == access
        if wrapper.kind == "method":
            shape_ok = called
        else:
            shape_ok = not called and not _in_nameof(access) and not _names_a_member(access)
        if (
            parent is None
            or not shape_ok
            or (
                receiver is None
                and (
                    access.parent is None
                    or xcs._type_position(node)
                    or parent.type in _FOREIGN_MEMBER_PARENTS
                    or (parent.type == "assignment_expression" and _field(parent, node) == "left")
                )
            )
        ):
            found.append(wi.Occurrence(path, line, wi.text_of(parent or node), None, wi.RECEIVER_UNPROVEN))
            continue
        reason: str | None = None
        name: str | None = None
        if receiver is None or receiver.type == "this":
            bound = (
                _binding(access, wrapper.name, readers, path, locals_only=True) if receiver is None else None
            )
            if not _in_owner(access, wrapper, path) or bound is not None:
                reason = wi.RECEIVER_UNPROVEN
        elif receiver.type == "identifier":
            name = _text(receiver)
            bound = _binding(access, name, readers, path)
            if bound is None:
                static_type = wrapper.static and _resolves(receiver, reader, targets) is None
                reason = None if static_type else wi.RECEIVER_UNPROVEN
                name = None if static_type else name
            elif isinstance(bound, str):
                reason = wi.RECEIVER_UNPROVEN
            else:
                reason = _resolves(bound, reader, targets)
        elif (
            receiver.type == "member_access_expression"
            and (inner := receiver.child_by_field_name("expression")) is not None
            and inner.type == "this"
            and (field_name := receiver.child_by_field_name("name")) is not None
            and field_name.type == "identifier"
        ):
            name = _text(field_name)
            bound = _binding(access, name, readers, path, class_only=True)
            reason = (
                wi.RECEIVER_UNPROVEN
                if bound is None or isinstance(bound, str)
                else _resolves(bound, reader, targets)
            )
        else:
            reason = wi.RECEIVER_UNPROVEN
        if reason is None and called and _argument_has_call(call):
            reason = wi.ARGUMENT_HAS_CALL
        shown = call if called else access
        double = _in_test_double(shown, data)
        span = (access.start_byte, access.end_byte) if reason is None else None
        suffix = "()" if wrapper.kind == "property" and reason is None else ""
        found.append(wi.Occurrence(path, line, wi.text_of(shown), span, reason, name, double, suffix))
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


wi.register(
    wi.Profile(
        language="csharp",
        extensions=EXTENSIONS,
        parse=parse,
        read_test=read_test,
        discover=discover,
        occurrences=occurrences,
        index=_index,
        qualify=qualify,
        strings=_STRINGS,
        foreign_extensions=FOREIGN_EXTENSIONS,
        names_only=names_only,
        members=members,
    )
)
