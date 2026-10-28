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
  implemented outside the repository when its project is not packed: no step
  that packs it (see :mod:`flag_cleanup.csharp_packing`), and no packing property (``IsPackable`` true,
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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from xml.etree import ElementTree

from tree_sitter import Node

from flag_cleanup import cross_file_csharp as xcs
from flag_cleanup import cross_file_keys, key_const, mock_stubs, source_index, syntax
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
    """Whether ``body`` is ``=> <read>`` or ``{ return <read>; }``, the
    ``return`` optionally after statements that only build the read's
    evaluation context (:func:`_context_preamble`)."""
    if body is None:
        return False
    if body.type == "arrow_expression_clause":
        parts = _named(body)
        return len(parts) == 1 and is_read(parts[0])
    if body.type != "block":
        return False
    statements = _named(body)
    if not statements or statements[-1].type != "return_statement":
        return False
    returned = _named(statements[-1])
    return len(returned) == 1 and is_read(returned[0]) and _context_preamble(statements[:-1], returned[0])


#: What a context-building statement's expressions may not hold: each one
#: writes, waits, throws or hands code on, which is more than computing a
#: value for the read.
_PREAMBLE_FORBIDDEN = frozenset(
    {
        "assignment_expression", "postfix_unary_expression", "await_expression", "throw_expression",
        "lambda_expression", "anonymous_method_expression", "declaration_expression", "ref_expression",
        "query_expression", "stackalloc_expression", "implicit_stackalloc_expression",
    }
)
_INCREMENTS = frozenset({"++", "--"})


def _computes_only(node: Node, out_locals: bool = False) -> bool:
    """Whether evaluating ``node`` writes nothing, waits for nothing and
    throws nowhere it says so: it may call, and what a call returns is a
    value of the context being built. No argument is passed ``ref``, ``out``
    or ``in`` (each may write the variable it names), except, with
    ``out_locals``, an ``out var x``, which writes only the fresh local it
    declares."""
    for part in cross_file_keys.walk(node):
        if part.type == "argument" and any(not c.is_named and _text(c) in ("ref", "out", "in") for c in part.children):
            fresh = any(c.type == "declaration_expression" for c in part.named_children)
            if not (out_locals and fresh and any(not c.is_named and _text(c) == "out" for c in part.children)):
                return False
        if (
            out_locals
            and part.type == "declaration_expression"
            and part.parent is not None
            and part.parent.type == "argument"
            and any(not c.is_named and _text(c) == "out" for c in part.parent.children)
        ):
            continue
        if (
            part.type == "assignment_expression"
            and part.parent is not None
            and part.parent.type == "initializer_expression"
            and part.parent.parent is not None
            and part.parent.parent.type in ("object_creation_expression", "implicit_object_creation_expression")
        ):
            continue  # `new T { P = v }` sets a member of the value being built
        if part.type in _PREAMBLE_FORBIDDEN or part.type == "ERROR" or part.is_missing:
            return False
        if part.type == "prefix_unary_expression" and any(_text(c) in _INCREMENTS for c in part.children):
            return False
    return True


def _context_preamble(statements: list[Node], read: Node) -> bool:
    """Whether ``statements``, run before ``return <read>;``, only build
    values the read alone consumes: the evaluation context the SDK targets
    with. Each one declares a local, or calls a method on (or sets a member
    of) such a local, optionally under an ``if`` whose condition only
    computes; every local declared is read nowhere but in these statements
    and the read's arguments. The fold already drops the read's arguments,
    whatever they call, so deleting the member with these statements drops
    the same kind of computation and nothing else.

    No loop, ``try``, ``using``, local function, jump or ``else`` is
    accepted, and a local's initializer only computes (:func:`_computes_only`).
    A local that a statement changes must hold a value built right there
    (``new …``): one read from elsewhere may be shared, and the change seen
    by other code."""
    if not statements:
        return True
    locals_: set[str] = set()
    built: set[str] = set()

    def mutates_local(statement: Node) -> bool:
        if statement.type != "expression_statement" or len(_named(statement)) != 1:
            return False
        expression = _named(statement)[0]
        if expression.type == "invocation_expression":
            function = expression.child_by_field_name("function")
            arguments = expression.child_by_field_name("arguments")
            receiver = function.child_by_field_name("expression") if function is not None else None
            return (
                function is not None
                and function.type == "member_access_expression"
                and receiver is not None
                and receiver.type == "identifier"
                and _text(receiver) in built
                and (arguments is None or _computes_only(arguments))
            )
        if expression.type == "assignment_expression":
            left = expression.child_by_field_name("left")
            right = expression.child_by_field_name("right")
            operator = expression.child_by_field_name("operator")
            receiver = left.child_by_field_name("expression") if left is not None else None
            return (
                operator is not None
                and _text(operator) == "="
                and left is not None
                and left.type == "member_access_expression"
                and receiver is not None
                and receiver.type == "identifier"
                and _text(receiver) in built
                and right is not None
                and _computes_only(right)
            )
        return False

    for statement in statements:
        if statement.type == "local_declaration_statement":
            if any(not c.is_named and _text(c) in ("using", "await") for c in statement.children):
                return False
            if any(c.type == "modifier" for c in statement.children):
                return False
            declaration = _child(statement, "variable_declaration")
            if declaration is None:
                return False
            for declarator in declaration.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name = _name_of(declarator)
                if name is None:
                    return False
                values = [c for c in _named(declarator) if c.id != declarator.child_by_field_name("name").id]
                if any(not _computes_only(v) for v in values):
                    return False
                locals_.add(name)
                if len(values) == 1 and values[0].type in ("object_creation_expression", "implicit_object_creation_expression"):
                    built.add(name)
        elif statement.type == "if_statement":
            condition = statement.child_by_field_name("condition")
            consequence = statement.child_by_field_name("consequence")
            if statement.child_by_field_name("alternative") is not None or condition is None or consequence is None:
                return False
            if not _computes_only(condition):
                return False
            inner = _named(consequence) if consequence.type == "block" else [consequence]
            if not inner or not all(mutates_local(s) for s in inner):
                return False
        elif not mutates_local(statement):
            return False
    # Every local feeds only the context: it reaches the read's arguments,
    # itself or through another local's initializer, and nothing outside
    # these statements and those arguments reads it.
    method = read.parent
    while method is not None and method.type not in _WRAPPER_DECLARATIONS and method.type != "accessor_declaration":
        method = method.parent
    arguments = read.child_by_field_name("arguments")
    if method is None or arguments is None:
        return False
    allowed = [*statements, arguments]
    for node in cross_file_keys.walk(method):
        if node.type == "identifier" and _text(node) in locals_:
            if not any(a.start_byte <= node.start_byte < a.end_byte for a in allowed):
                return False

    def names_in(node: Node) -> set[str]:
        return {_text(n) for n in cross_file_keys.walk(node) if n.type == "identifier"} & locals_

    def receiver_of(statement: Node) -> str | None:
        expression = _named(statement)[0]
        target = expression.child_by_field_name("function" if expression.type == "invocation_expression" else "left")
        receiver = target.child_by_field_name("expression") if target is not None else None
        return _text(receiver) if receiver is not None else None

    # What each local is computed from: its initializer, and every
    # statement that changes it (with the condition guarding one).
    sources: dict[str, set[str]] = {}
    for statement in statements:
        if statement.type == "local_declaration_statement":
            declaration = _child(statement, "variable_declaration")
            for declarator in declaration.named_children if declaration is not None else ():
                if declarator.type == "variable_declarator" and (name := _name_of(declarator)) is not None:
                    sources.setdefault(name, set()).update(names_in(declarator) - {name})
            continue
        guard = names_in(statement.child_by_field_name("condition")) if statement.type == "if_statement" else set()
        consequence = statement.child_by_field_name("consequence") if statement.type == "if_statement" else None
        inner = [statement] if consequence is None else (_named(consequence) if consequence.type == "block" else [consequence])
        for part in inner:
            changed = receiver_of(part)
            if changed is not None:
                sources.setdefault(changed, set()).update((names_in(part) | guard) - {changed})
    feeding = names_in(arguments)
    grown = True
    while grown:
        more = set().union(*(sources.get(n, set()) for n in feeding)) - feeding
        grown = bool(more)
        feeding |= more
    return feeding >= locals_


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
    #: Byte span of the whole type declaration.
    start: int = 0
    end: int = 0


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
    #: The wrapper's :class:`~flag_cleanup.csharp_family.Family`, once qualified.
    family: object | None = None
    #: The run's treatment, when qualified for a run: what a test fake's
    #: knob left set to the kept value is harmless at.
    treatment: bool | None = None

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
        # A string may name the member in another case, or by a prefix,
        # where a lookup reads it so (`string_mentions`).
        from flag_cleanup import csharp_family

        known = set(index.parsed)
        index.parsed += [p for p in csharp_family.loose_files(repo_dir) if p not in known]
        # A file spelling only an accessor's name (`"get_IsX"`), which no
        # word match of the member's name finds.
        accessors = [f"get_{wrapper.name}", f"set_{wrapper.name}"]
        known = set(index.parsed)
        index.parsed += [
            p
            for p in cross_file_keys.mention_files(repo_dir, accessors, exclude=())
            if p.endswith(EXTENSIONS) and p not in known
        ]
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
                start=node.start_byte,
                end=node.end_byte,
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
    """The file's text with its line endings as they are (see
    :func:`flag_cleanup.wrapper_inline._read`)."""
    from flag_cleanup import mock_stubs

    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


class _Readers:
    """One namespace-lookup reader per file, for walks that cross files."""

    def __init__(self, repo_dir: str):
        self.repo_dir = repo_dir
        self._by_path: dict[str, xcs._Reader | None] = {}
        #: Whether a typed service-locator lookup (``sp.GetRequiredService<T>()``)
        #: can be trusted in the file being read; see :func:`_locator_ok`.
        self.locator = False
        #: Likewise for ``Substitute.For<T>()``; see :func:`_substitute_ok`.
        self.substitute = False

    def factories(self) -> frozenset[str]:
        """The typed-factory shapes whose declared type may be trusted here."""
        return frozenset(
            name for name, on in (("locator", self.locator), ("substitute", self.substitute)) if on
        )

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
        nested = _nested_type_full(node, name, reader)
        if nested is not None:
            return nested or None
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


def _supertypes_of(readers: _Readers, path: str, type_node: Node, seen: dict | None = None) -> list | None:
    """Every base type of ``type_node``, followed transitively, as checkout
    index entries, each resolved by namespace from the file declaring it;
    ``None`` unless every one is a repository type. One that is not may
    declare the wrapper's name, and C# implements an interface member with
    no keyword at all. A simple name shared with an unrelated repository
    type is not enough: the lookup has to reach it.

    A base declared only in a file that does not parse whole is missing from
    the checkout index, so it is refused here like a library type."""
    seen = {} if seen is None else seen
    reader = readers.get(path)
    if reader is None:
        return None
    checkout = xcs.csharp_index(readers.repo_dir)
    for base in _base_nodes(type_node):
        full = _type_full(base, reader)
        infos = [i for i in checkout.by_full.get(full or "", ()) if not i.generic]
        if full is None or len(infos) != 1 or infos[0].partial:
            return None
        if full in seen:
            continue
        seen[full] = infos[0]
        declaring = readers.get(infos[0].path)
        declaration = _find_type(declaring.root, infos[0].chain) if declaring is not None else None
        if declaration is None or _supertypes_of(readers, infos[0].path, declaration, seen) is None:
            return None
    return list(seen.values())


def _bases_in_repository(readers: _Readers, path: str, type_node: Node) -> bool:
    """Whether every base type of ``type_node``, transitively, is a
    repository type (see :func:`_supertypes_of`)."""
    return _supertypes_of(readers, path, type_node) is not None


#: Project properties that pack a project, or describe the package it packs to.
_PACKING = re.compile(
    r"<(?:IsPackable\b[^>]*>\s*(?!false\b)|PackageId\b|GeneratePackageOnBuild\b[^>]*>\s*(?!false\b)"
    r"|PackageLicense\w*\b|PackageProjectUrl\b|PackageReadmeFile\b|PackageTags\b|PackageIcon\b"
    r"|Import\b)",
    re.IGNORECASE,
)
_FRIENDS = re.compile(r"InternalsVisibleTo\b")


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
    ``.targets`` between it and the repository root, and from the checkout's
    pack steps (:mod:`flag_cleanup.csharp_packing`): a step that packs this
    project, or one that cannot be tied to what it packs, publishes it. So
    does any project packed either way that references it, directly or
    through others: its public types ship with that package, bundled into it
    or as a dependency it names (a test project marked only by an
    ``<Import>`` is not taken as packed). Anything unreadable, imported, a
    file outside every project, or a reference graph that cannot be trusted
    while something is packed, counts as published.
    """
    from flag_cleanup import csharp_family, csharp_packing

    projects = xcs._project_dirs(repo_dir)
    project = xcs._project_of(path, projects)
    if project is None:
        return True
    # A file a project links in from outside its directory ships with that
    # project too.
    holders = {project.resolve(), *(p.resolve() for p in csharp_family._enclosing(path, projects, repo_dir))}
    packed = csharp_packing.packed_projects(repo_dir)
    if packed is csharp_packing.ALL or holders & set(packed) or any(_packing_mark(h, repo_dir) for h in holders):
        return True
    roots = set(packed)
    for other in projects:
        if other.resolve() in holders:
            continue
        mark = _packing_mark(other, repo_dir)
        # An `<Import>` alone may make a project packable; a test project is
        # not packed (its test SDK makes it unpackable) unless a step packs it.
        # Only the SDK's own marks count here: a library that merely
        # references a test framework (an assertion helper) may well be packed.
        if mark == "package" or (mark == "import" and _project_is_test(other, repo_dir, sdk_only=True) is not True):
            roots.add(other.resolve())
    if not roots:
        return False
    graph = csharp_family.project_graph(repo_dir)
    if graph is None:
        return True
    shipped: set[Path] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in shipped:
            continue
        shipped.add(current)
        pending.extend(graph.get(current, ()))
    return bool(holders & shipped)


def _packing_mark(project: Path, repo_dir: str) -> str | None:
    """How a project's own ``.csproj``, or a ``Directory.Build`` file between
    it and the repository root, may make it packable (``_PACKING``):
    ``"package"`` for a package property or metadata, or a file that cannot
    be read; ``"import"`` for nothing but an ``<Import>``, whose file may set
    one; ``None`` for neither."""
    from flag_cleanup import source_index

    root = Path(source_index.checkout_root(repo_dir)).resolve()
    files = sorted(project.glob("*.csproj"))
    for directory in (project, *project.parents):
        files += [directory / name for name in ("Directory.Build.props", "Directory.Build.targets")]
        if directory == root:
            break
    mark = None
    for file in files:
        if not file.exists():
            continue
        text = _read(str(file))
        if text is None:
            return "package"
        for match in _PACKING.finditer(text):
            if not match.group(0).lower().startswith("<import"):
                return "package"
            mark = "import"
    return mark


# --- Sibling implementations ------------------------------------------------------

#: A test framework's package, or a test SDK, by its name's case-insensitive
#: prefix.
_TEST_PACKAGES = (
    "xunit", "nunit", "mstest", "microsoft.net.test.sdk", "microsoft.testing.platform", "tunit", "expecto", "fixie",
)
#: An MSBuild property, item or metadata expression: a value computed when
#: the project is built, which reading the file cannot know.
_MSBUILD_EXPRESSION = re.compile(r"[$@%]\(")
#: A property by which MSBuild imports a file no ``<Import>`` names, or skips
#: one it would import: ``CustomAfterMicrosoftCommonTargets``,
#: ``DirectoryBuildPropsPath``, ``ImportDirectoryBuildProps`` and the like,
#: matched by the lower-cased tag.
_IMPORT_HOOK = re.compile(
    r"custom(?:before|after)\w*|directorybuild\w*path|directorypackagespropspath|importdirectory(?:build|packages)\w*"
)
#: A test method's attribute, by its simple name with any ``Attribute`` dropped.
_TEST_ATTRIBUTES = frozenset({"Fact", "Theory", "Test", "TestMethod", "TestCase"})


def _nearest(directory: Path, name: str, root: Path) -> Path | None:
    """The file MSBuild imports implicitly by ``name``: the first one found
    from ``directory`` up to the checkout root."""
    for current in (directory, *directory.parents):
        candidate = current / name
        if candidate.is_file():
            return candidate
        if current == root:
            return None
    return None


def _test_project(path: str, repo_dir: str) -> bool | None:
    """Whether ``path``'s project is a test project (:func:`_project_is_test`).
    A file outside every project is not in one."""
    project = xcs._project_of(path, xcs._project_dirs(repo_dir))
    return False if project is None else _project_is_test(project, repo_dir)


def _project_is_test(project: Path, repo_dir: str, sdk_only: bool = False) -> bool | None:
    """Whether the C# project in ``project`` is a test project, read from its
    project file and everything that file imports, ``Directory.Build`` and
    ``Directory.Packages`` files included, each parsed as XML
    (:func:`_test_project_file`). ``None`` when that cannot be read or parsed
    whole, or the project files of the directory disagree. ``sdk_only``: only
    what makes the .NET SDK treat it as a test project counts
    (:func:`_test_project_file`)."""
    from flag_cleanup import csharp_family, source_index

    root = Path(source_index.checkout_root(repo_dir)).resolve()
    implicit = [
        f
        for f in (
            _nearest(project, n, root)
            for n in ("Directory.Build.props", "Directory.Build.targets", "Directory.Packages.props")
        )
        if f
    ]
    answers = set()
    for csproj in sorted(project.glob("*.csproj")):
        reached = csharp_family._msbuild_files([csproj.resolve()], implicit, closure_only=True)
        if reached is None:
            return None
        found = {_test_project_file(text, sdk_only) for text in reached[1].values()}
        answers.add(True if True in found else None if None in found else False)
    return answers.pop() if len(answers) == 1 else None


def _test_project_file(text: str, sdk_only: bool = False) -> bool | None:
    """Whether one MSBuild file, parsed, makes its project a test project: a
    ``PackageReference`` or ``GlobalPackageReference`` whose ``Include``
    names a test framework's package, an ``Sdk`` attribute or ``<Sdk Name>``
    naming a test SDK, or ``<IsTestProject>true``. An ``Update`` never
    counts: it edits an item the project already has (central version
    pinning in a ``Directory.Build.targets``) and adds the package to no
    project. Names and attributes are matched without regard to case,
    quoting or an XML namespace. ``None`` when the file does not parse, one
    of those values is computed at build time, or it sets an import-hook
    property (``_IMPORT_HOOK``): what the file MSBuild then imports, or
    skips, adds is not read here. ``sdk_only``: a package counts only when it
    is ``Microsoft.NET.Test.Sdk``, the one whose props make the SDK treat the
    project as a test project (and unpackable); a test SDK and
    ``<IsTestProject>`` count as before."""
    try:
        document = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    answers: set[bool | None] = set()
    for element in document.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if _IMPORT_HOOK.fullmatch(tag):
            return None
        attributes = {k.rsplit("}", 1)[-1].lower(): v for k, v in element.attrib.items()}
        names = [attributes["sdk"]] if "sdk" in attributes else []
        packages: list[str] = []
        if tag in ("packagereference", "globalpackagereference") and "include" in attributes:
            packages.append(attributes["include"])
        elif tag == "sdk" and "name" in attributes:
            names.append(attributes["name"])
        elif tag == "istestproject":
            value = (element.text or "").strip()
            answers.add(None if _MSBUILD_EXPRESSION.search(value) else value.lower() == "true")
        if sdk_only:
            for package in packages:
                if _MSBUILD_EXPRESSION.search(package):
                    answers.add(None)
                    continue
                listed = (n.strip().split("/", 1)[0].lower() for n in package.split(";"))
                answers.add(any(n == "microsoft.net.test.sdk" for n in listed))
        else:
            names += packages
        for name in names:
            if _MSBUILD_EXPRESSION.search(name):
                answers.add(None)
                continue
            listed = (n.strip().split("/", 1)[0].lower() for n in name.split(";"))
            answers.add(any(n.startswith(_TEST_PACKAGES) for n in listed if n))
    return True if True in answers else None if None in answers else False


def _holds_test_attribute(root: Node) -> bool:
    """Whether a file holds an attribute named like a test method's."""
    for node in cross_file_keys.walk(root):
        if node.type != "attribute":
            continue
        name = node.child_by_field_name("name")
        last = _text(name).rsplit(".", 1)[-1].rsplit("::", 1)[-1] if name is not None else ""
        if last.removesuffix("Attribute") in _TEST_ATTRIBUTES or last in _TEST_ATTRIBUTES:
            return True
    return False


#: A Razor or MVC view ``@using`` alias: ``@using G = App.Flags.IFeatureFlags``.
_GUEST_ALIAS = re.compile(r"@using\s+@?(\w+)\s*=\s*((?:global::)?[\w.]+)")


def _aliases(repo_dir: str, name: str) -> frozenset[str] | None:
    """``name`` and every ``using`` alias (in a C# file or a project
    ``<Using>`` item) that names it, or an alias of it; ``None`` when
    something that may declare one cannot be read."""
    usings, readable = xcs.msbuild_usings(repo_dir)
    if not readable:
        return None
    names = {name}
    while True:
        before = frozenset(names)
        for using in usings:
            if using.kind == "alias" and using.alias and using.target and using.target.rsplit(".", 1)[-1] in before:
                names.add(using.alias)
        for path in cross_file_keys.mention_files(repo_dir, sorted(before), exclude=()):
            if path.lower().endswith(_GUESTS):
                # Razor's `@using X = ...`, in the file or in an
                # `_Imports.razor` / `_ViewImports.cshtml` it inherits.
                text = _read(path)
                if text is None:
                    return None
                for match in _GUEST_ALIAS.finditer(text):
                    if match.group(2).rsplit(".", 1)[-1].rsplit("::", 1)[-1] in before:
                        names.add(match.group(1))
                continue
            if not path.endswith(EXTENSIONS):
                continue
            source = _read(path)
            if source is None:
                return None
            for node in cross_file_keys.walk(parse(source)):
                alias = node.child_by_field_name("name") if node.type == "using_directive" else None
                if alias is None:
                    continue
                target = next((c for c in reversed(node.named_children) if c.id != alias.id), None)
                if target is not None and xcs._simple_name(target) in before:
                    names.add(_text(alias).removeprefix("@"))
        if names == before:
            return before


def _guest_extends(repo_dir: str, names: frozenset[str]) -> bool:
    """Whether a .NET guest file (Razor, XAML, Web Forms) extends or
    implements a type by any of ``names``. Unreadable counts."""
    for path in cross_file_keys.mention_files(repo_dir, sorted(names), exclude=()):
        if path.lower().endswith(_GUESTS):
            text = _read(path)
            if text is None or any(_guest_implementations(text, n) for n in names):
                return True
    return False


def _find_type_in(root: Node, namespace: str, chain: str) -> Node | None:
    """The one type declaration of ``chain`` in ``namespace``, or ``None``."""
    parts = chain.split(".")
    found = [
        n
        for n in cross_file_keys.walk(root)
        if n.type in _TYPES and xcs._type_chain(n, strict=False) == parts and xcs._namespace_of(n) == namespace
    ]
    return found[0] if len(found) == 1 else None


def _returned(body: Node | None) -> Node | None:
    """The one expression a body is: ``=> e`` or ``{ return e; }``."""
    if body is None:
        return None
    if body.type == "arrow_expression_clause":
        parts = _named(body)
        return parts[0] if len(parts) == 1 else None
    statements = _named(body)
    if len(statements) != 1 or statements[0].type != "return_statement":
        return None
    returned = _named(statements[0])
    return returned[0] if len(returned) == 1 else None


def _bodiless(prop: Node, names: set[str] | None = None) -> bool:
    """An auto-property: no expression body, and accessors with neither a
    body nor a modifier; exactly ``names`` of them, when given."""
    accessors = prop.child_by_field_name("accessors")
    if accessors is None:
        return False
    parts = _named(accessors)
    if not parts or any(
        c.type != "accessor_declaration"
        or c.child_by_field_name("body") is not None
        or _child(c, "modifier") is not None
        for c in parts
    ):
        return False
    if names is None:
        return True
    words = [c.child_by_field_name("name") for c in parts]
    return sorted(_text(w) for w in words if w is not None) == sorted(names) and len(words) == len(names)


def _own_state(type_node: Node, name: str) -> Node | None:
    """The declared type of ``name`` when it is a field or an auto-property
    the type itself declares, once (a record's positional parameter is
    one): reading it runs no code. Not one that may be, or replace, another
    member (``virtual``, ``abstract``, ``override``, ``extern``, ...), whose
    read a subclass may answer with anything; and a base's member is not the
    type's own. A declaration inside an ``#if`` is not seen at all, so
    which branch compiles never matters."""
    found: list[Node | None] = []
    body = type_node.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        if member.type == "field_declaration":
            declaration = _child(member, "variable_declaration")
            bound = _declarator_type(declaration, name) if declaration is not None else None
            if bound is not None:
                found.append(bound if isinstance(bound, Node) else None)
        elif member.type == "property_declaration" and _name_of(member) == name:
            plain = _bodiless(member) and not xcs._modifiers(member) & _OVERRIDES
            found.append(member.child_by_field_name("type") if plain else None)
        elif name in _member_names(member):
            found.append(None)
    if not found and type_node.type in ("record_declaration", "record_struct_declaration"):
        bound = _primary_parameter(type_node, name)
        found.append(bound if isinstance(bound, Node) else None)
    return found[0] if len(found) == 1 else None


def _read_chain(expression: Node) -> tuple[Node, bool, list[str]] | None:
    """``(root, through this, the names read after it)`` of ``e``, ``e.A.B``
    or ``this.e.A``: a tower of member reads over one identifier, with no
    call, no ``?.``, no ``!`` and no parentheses."""
    steps: list[str] = []
    node = expression
    while node.type == "member_access_expression":
        name = node.child_by_field_name("name")
        inner = node.child_by_field_name("expression")
        if name is None or inner is None or name.type != "identifier":
            return None
        if inner.type == "this":
            return name, True, steps[::-1]
        steps.append(_text(name).removeprefix("@"))
        node = inner
    return (node, False, steps[::-1]) if node.type == "identifier" else None


def _is_plain_bool(type_node: Node | None) -> bool:
    return type_node is not None and type_node.type == "predefined_type" and _text(type_node) == "bool"


def _plain_bool_chain(
    type_node: Node, steps: list[str], reader: xcs._Reader, readers: _Readers, family: frozenset[str]
) -> bool:
    """Whether reading ``steps`` from a value of ``type_node`` runs no code
    and yields a ``bool``: each step a field or auto-property declared by a
    class, struct or record of the checkout (never a library type, an
    interface, or a family type), none of which declares an operator, and
    the last one literally ``bool``."""
    checkout = xcs.csharp_index(readers.repo_dir)
    for step in steps:
        full = _type_full(type_node, reader)
        infos = checkout.by_full.get(full or "", ())
        if full is None or full in family or len(infos) != 1 or infos[0].generic or infos[0].partial:
            return False
        reader = readers.get(infos[0].path)
        node = _find_type_in(reader.root, infos[0].namespace, infos[0].chain) if reader is not None else None
        if node is None or node.type not in _OWNERS:
            return False
        body = node.child_by_field_name("body")
        if any(
            m.type in ("operator_declaration", "conversion_operator_declaration")
            for m in (body.named_children if body is not None else ())
        ):
            return False
        member = _own_state(node, step)
        if member is None:
            return False
        type_node = member
    return _is_plain_bool(type_node)


def _inert_member(
    member: Node, type_node: Node, test: bool, reader: xcs._Reader, readers: _Readers, family: frozenset[str]
) -> tuple[str, str | None] | None:
    """``(what it returns, its knob)`` when the member is inert, else ``None``.

    Inert: a ``bool`` literal; a read of the type's own field or
    auto-property, or a tower of reads through one, that runs no code and
    yields a ``bool`` (see :func:`_plain_bool_chain`); or an auto-property
    with a setter or ``init`` (its own knob). A get-only auto-property is
    not: a constructor may set it from anything. In a test sibling a bare
    read of a ``bool`` primary-constructor parameter is inert too, and the
    parameter is the knob; so is a get-only auto-property initialized with
    exactly that parameter, which then holds it unless another constructor
    sets it (:func:`_knob_default` refuses a type declaring one). A method may ignore
    its parameters, but never read one."""
    if xcs._modifiers(member) & _OVERRIDES:
        return None
    parameters: set[str] = set()
    if member.type == "property_declaration":
        if _bodiless(member, {"get", "set"}) or _bodiless(member, {"get", "init"}):
            if member.child_by_field_name("value") is not None:
                return None  # an initializer may be anything
            return "auto-property", _name_of(member)
        initial = member.child_by_field_name("value")
        if _bodiless(member, {"get"}) and initial is not None:
            # Another constructor could set it: `_knob_default` refuses a
            # primary-constructor knob in a type that declares one.
            word = _text(initial).removeprefix("@")
            bound = _primary_parameter(type_node, word)
            if (
                test
                and type_node.type not in ("record_declaration", "record_struct_declaration")
                and isinstance(bound, Node)
                and _is_plain_bool(bound)
            ):
                return _text(initial), word
            return None
        body = _getter_body(member)
    else:
        listed = member.child_by_field_name("parameters")
        parameters = {
            n.removeprefix("@") for p in (listed.named_children if listed is not None else ()) if (n := _name_of(p))
        }
        body = member.child_by_field_name("body")
    expression = _returned(body)
    if expression is None:
        return None
    returns = " ".join(_text(expression).split())
    if expression.type == "boolean_literal":
        return returns, None
    read = _read_chain(expression)
    if read is None:
        return None
    root, through_this, steps = read
    word = _text(root).removeprefix("@")
    if word in parameters:
        return None
    state = _own_state(type_node, word)
    primary = not through_this and type_node.type not in ("record_declaration", "record_struct_declaration") and (
        _primary_parameter(type_node, word) is not None
    )
    bare = expression.type == "identifier" or (through_this and not steps)
    if state is not None and not primary:
        if not _plain_bool_chain(state, steps, reader, readers, family):
            return None
        return returns, word if bare else None
    if primary and state is None and test and bare:
        bound = _primary_parameter(type_node, word)
        return (returns, word) if isinstance(bound, Node) and _is_plain_bool(bound) else None
    return None


def _siblings(
    wrapper: wi.Wrapper, interface: TypeDecl, index: Index, readers: _Readers
) -> tuple[tuple[tuple[wi.Sibling, ...], tuple[wi.SharedFake, ...]] | tuple[()], str | wi.Refused | None]:
    """``(siblings, shared fakes)``: every other implementation of
    ``interface``, each inert; one whose member another supertype also
    declares is a :class:`~flag_cleanup.wrapper_inline.SharedFake` when that
    member returns a literal, refused otherwise. Or the reason
    one is not (``interface-published`` for one this cannot see: a guest
    file's or a derived interface's).

    Found through the checkout index, by the interface's name or any alias of
    it, and kept when its base resolves to the interface. An implementation
    is refused unless it is a top-level, sealed or never extended,
    non-generic, non-partial class or struct, every base of it (transitively)
    is a repository type, none but the interface declares the name, no
    caller outside the repository can reach it, it declares the member
    once, inertly, and whether it is test code can be told. One in the
    wrapper's or the interface's own file is refused too: close-out deletes
    the name there in one edit of its own, which cannot also take the
    sibling's member, so the wrapper would end partial after its sibling's
    tests went. A refusal tied to one implementation is a
    :class:`~flag_cleanup.wrapper_inline.Refused` naming it."""
    full = _full_name(interface.namespace, interface.name)
    names = _aliases(index.repo_dir, interface.name)
    if names is None:
        return (), wi.SIBLING_NOT_INERT
    if _guest_extends(index.repo_dir, names):
        return (), wi.INTERFACE_PUBLISHED
    checkout = xcs.csharp_index(index.repo_dir)
    if names != {interface.name} and checkout.incomplete:
        return (), wi.SIBLING_NOT_INERT
    candidates = {
        (str(Path(info.path).resolve()), info.namespace, info.chain): info
        for infos in checkout.by_full.values()
        for info in infos
        if names & set(info.supertypes)
    }
    wrapper_path = str(Path(wrapper.path).resolve())
    held = {wrapper_path, str(Path(interface.path).resolve())}
    family = frozenset(
        {full, _full_name(wrapper.package, wrapper.owner)}
        | {_full_name(namespace, chain) for _, namespace, chain in candidates}
    )
    found: list[wi.Sibling] = []
    shared: list[wi.SharedFake] = []
    for (path, namespace, chain), info in sorted(candidates.items()):
        if path == wrapper_path and chain == wrapper.owner and namespace == wrapper.package:
            continue
        reader = readers.get(path)
        node = _find_type_in(reader.root, namespace, chain) if reader is not None else None

        def refused(reason: str, at: Node | None = None) -> tuple[tuple[wi.Sibling, ...], wi.Refused]:
            where = at if at is not None else node
            return (), wi.Refused(reason, path, wi.line_of(where) if where is not None else 1, chain)

        if node is None:
            return refused(wi.SIBLING_NOT_INERT)
        named = [b for b in _base_nodes(node) if xcs._simple_name(b) in names]
        bases = [_type_full(b, reader) for b in named]
        if full not in bases:
            if any(b is None for b in bases):
                return refused(wi.SIBLING_NOT_INERT)
            continue  # another type of the same simple name
        if node.type not in _OWNERS:
            return refused(wi.INTERFACE_PUBLISHED)  # a derived interface
        words = xcs._modifiers(node)
        if (
            ("." in chain and words & {"public", "internal", "protected", "file"})
            or words & {"abstract", "partial", "static"}
            or _child(node, "type_parameter_list") is not None
            or _extended(node, info.last, index.repo_dir)
            or (
                (words & {"public", "protected"} or _has_friends(path, index.repo_dir))
                and published(path, index.repo_dir)
            )
        ):
            return refused(wi.SIBLING_NOT_INERT)
        supertypes = _supertypes_of(readers, path, node)
        if supertypes is None:
            return refused(wi.SIBLING_NOT_INERT)
        # Another supertype declaring the name: the one member implements
        # both, so it cannot go with the wrapper (see wi.SharedFake).
        serves = sorted({s.last for s in supertypes if s.full != full and wrapper.name in s.members})
        body = node.child_by_field_name("body")
        members = [m for m in (body.named_children if body is not None else ()) if wrapper.name in _member_names(m)]
        kind = "property_declaration" if wrapper.kind == "property" else "method_declaration"
        if len(members) != 1 or members[0].type != kind:
            return refused(wi.SIBLING_NOT_INERT)
        if path in held:
            return refused(wi.SIBLING_NOT_INERT, members[0])
        in_project = _test_project(path, index.repo_dir)
        if in_project is None:
            return refused(wi.SIBLING_NOT_INERT, members[0])
        # Only the project says test code. A production project's file that
        # also holds tests may test the sibling, and nothing edits tests
        # outside a test project, so deleting its member could leave them
        # asserting the removed branch: refuse.
        if not in_project and _holds_test_attribute(reader.root):
            return refused(wi.SIBLING_NOT_INERT, members[0])
        # A nested implementation is a private test fake, or refused: only
        # its outer type can name it, and only a test project's may.
        if "." in chain and not in_project:
            return refused(wi.SIBLING_NOT_INERT)
        test = in_project
        inert = _inert_member(members[0], node, test, reader, readers, family)
        if inert is None:
            return refused(wi.SIBLING_NOT_INERT, members[0])
        returns, knob = inert
        if serves:
            # Only a literal can stay for both: qualify_all holds it to the
            # value the run keeps, and refuses it when that is not known.
            shared.append(
                wi.SharedFake(
                    path, info.last, namespace, wi.line_of(members[0]), returns, tuple(serves),
                    chain if "." in chain else None,
                )
            )
            continue
        default: str | None = None
        primary = False
        parameter: str | None = None
        if test and returns not in ("true", "false"):
            # A test sets a fake through its knob, and the knob is all that
            # can be followed: one read some other way is not inert here.
            if knob is None:
                return refused(wi.SIBLING_NOT_INERT, members[0])
            if returns != "auto-property":
                known = _knob_default(node, members[0], knob, supertypes)
                if known is None:
                    return refused(wi.SIBLING_NOT_INERT, members[0])
                default, primary, parameter = known
        found.append(
            wi.Sibling(
                path, info.last, namespace, wi.line_of(members[0]),
                members[0].start_byte, members[0].end_byte, returns, knob if test else None, test,
                default, primary, parameter, chain if "." in chain else None,
                _primary_arity(node) if primary else None,
                _primary_index(node, knob) if primary else None,
            )
        )
    return (tuple(found), tuple(shared)), None


def _knob_default(
    type_node: Node, member: Node, knob: str, supertypes: list
) -> tuple[str, bool, str | None] | None:
    """``(default, primary, parameter)`` of a test fake's ``knob``
    (``wi.Sibling.knob_default``, ``knob_primary``, ``knob_parameter``), when
    the knob is all that chooses ``member``'s value: declared once, by the
    fake itself and no supertype of it, never ``static``, with a literal
    initializer or none, and mentioned nowhere in the fake but its
    declaration and ``member``'s read (no other reader, no write of the
    fake's own). A primary constructor's parameter qualifies only in a type
    declaring no other constructor, which could pass it anything. A struct
    can always be built as ``default``, which no initializer reaches, so its
    knob defaults to ``false`` and takes none.

    A member knob may also be written by the class's one constructor, when
    that constructor takes one plain ``bool`` and does nothing but store it
    in the knob (``Stub(bool enabled) => Enabled = enabled;``). Every
    construction then passes the knob's value, so it has no default
    (``""``), and the parameter is returned. ``None`` otherwise."""
    if any(knob in s.members for s in supertypes):
        return None
    struct = type_node.type in ("struct_declaration", "record_struct_declaration")
    body = type_node.child_by_field_name("body")
    declared: list[tuple[Node, str | None, bool]] = []  # (name node, default, primary)
    parameters = _child(type_node, "parameter_list")
    for parameter in parameters.named_children if parameters is not None else ():
        if parameter.type == "parameter" and _name_of(parameter) == knob:
            if _child(parameter, "modifier") is not None or _child(parameter, "attribute_list") is not None:
                return None
            name = parameter.child_by_field_name("name")
            after = [c for c in parameter.named_children if c.start_byte >= name.end_byte]
            if after and (len(after) != 1 or after[0].type != "boolean_literal"):
                return None
            if any(m.type == "constructor_declaration" for m in (body.named_children if body is not None else ())):
                return None
            if struct and after:
                return None
            default = _text(after[0]) if after else ("false" if struct else "")
            declared.append((name, default, True))
    for part in body.named_children if body is not None else ():
        if part.type == "field_declaration":
            declaration = _child(part, "variable_declaration")
            for declarator in declaration.named_children if declaration is not None else ():
                if declarator.type != "variable_declarator" or _name_of(declarator) != knob:
                    continue
                name = declarator.child_by_field_name("name")
                values = [c for c in declarator.named_children if c.start_byte >= name.end_byte]
                declared.append((name, _initial(values[0] if values else None, struct), False))
                if "static" in xcs._modifiers(part):
                    return None
        elif part.type == "property_declaration" and _name_of(part) == knob:
            if "static" in xcs._modifiers(part):
                return None
            declared.append((part.child_by_field_name("name"), _initial(part.child_by_field_name("value"), struct), False))
    if len(declared) != 1 or declared[0][1] is None:
        return None
    name_node, default, primary = declared[0]
    constructor = None if primary or struct else _knob_constructor(type_node, knob)
    write = constructor[1] if constructor is not None else None
    for node in cross_file_keys.walk(type_node):
        if node.type != "identifier" or _text(node).removeprefix("@") != knob or node.id == name_node.id:
            continue
        if write is not None and node.id == write.id:
            continue
        if not (member.start_byte <= node.start_byte < member.end_byte):
            return None
    if constructor is not None:
        return "", False, constructor[0]
    return default, primary, None


def _knob_constructor(type_node: Node, knob: str) -> tuple[str, Node] | None:
    """``(parameter, the knob's identifier it writes)`` when ``type_node``
    declares exactly one constructor, an instance one with no attribute, no
    ``this(…)``/``base(…)`` initializer and exactly one plain ``bool``
    parameter (no default, modifier or attribute), whose whole body is
    ``Knob = parameter`` or ``this.Knob = parameter``, as an expression body
    or a block of that one statement."""
    body = type_node.child_by_field_name("body")
    constructors = [m for m in (body.named_children if body is not None else ()) if m.type == "constructor_declaration"]
    if len(constructors) != 1:
        return None
    constructor = constructors[0]
    if (
        xcs._modifiers(constructor) & {"static", "extern", "unsafe"}
        or _child(constructor, "attribute_list") is not None
        or _child(constructor, "constructor_initializer") is not None
    ):
        return None
    listed = constructor.child_by_field_name("parameters")
    parameters = [p for p in (listed.named_children if listed is not None else ()) if p.type != "comment"]
    if (
        len(parameters) != 1
        or parameters[0].type != "parameter"
        or not _is_plain_bool(parameters[0].child_by_field_name("type"))
    ):
        return None
    parameter = parameters[0]
    name = parameter.child_by_field_name("name")
    if (
        name is None
        or _child(parameter, "modifier") is not None
        or _child(parameter, "attribute_list") is not None
        or any(c.start_byte >= name.end_byte for c in parameter.named_children)
    ):
        return None
    code = constructor.child_by_field_name("body")
    if code is None:
        return None
    if code.type == "arrow_expression_clause":
        expressions = [c for c in code.named_children if c.type != "comment"]
    elif code.type == "block":
        statements = [c for c in code.named_children if c.type != "comment"]
        if len(statements) != 1 or statements[0].type != "expression_statement":
            return None
        expressions = [c for c in statements[0].named_children if c.type != "comment"]
    else:
        return None
    if len(expressions) != 1 or expressions[0].type != "assignment_expression":
        return None
    assignment = expressions[0]
    left = assignment.child_by_field_name("left")
    right = assignment.child_by_field_name("right")
    operator = assignment.child_by_field_name("operator")
    if left is None or right is None or operator is None or _text(operator) != "=":
        return None
    if left.type == "member_access_expression":
        receiver = left.child_by_field_name("expression")
        if receiver is None or receiver.type not in ("this", "this_expression"):
            return None
        left = left.child_by_field_name("name")
    if left is None or left.type != "identifier" or _text(left).removeprefix("@") != knob:
        return None
    if right.type != "identifier" or _text(right) != _text(name):
        return None
    return _text(name).removeprefix("@"), left


def _initial(value: Node | None, struct: bool) -> str | None:
    """The default of a field or auto-property initialized with ``value``:
    ``false`` with none, the literal for a ``bool`` literal, ``None`` for
    anything else, or for any initializer of a struct's."""
    if value is None:
        return "false"
    if struct or value.type != "boolean_literal":
        return None
    return _text(value)


def _extended(node: Node, name: str, repo_dir: str) -> bool:
    """Whether a class may have a subclass: it is not sealed (a struct never
    has one), and some C# type lists its name or an alias of it as a base, or
    a guest file extends it. Unreadable counts."""
    if node.type in ("struct_declaration", "record_struct_declaration") or "sealed" in xcs._modifiers(node):
        return False
    names = _aliases(repo_dir, name)
    if names is None:
        return True
    checkout = xcs.csharp_index(repo_dir)
    if any(names & set(info.supertypes) for infos in checkout.by_full.values() for info in infos):
        return True
    return _guest_extends(repo_dir, names)


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
    supertypes = _supertypes_of(readers, wrapper.path, owner_node) if owner_node is not None else None
    if supertypes is None:
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
            or (
                (interface.public or _has_friends(interface.path, index.repo_dir))
                and published(interface.path, index.repo_dir)
            )
        ):
            return None, wi.INTERFACE_PUBLISHED
        implementations, why = _siblings(wrapper, interface, index, readers)
        if why is not None:
            return None, why
        siblings, shared = implementations
        start, end = declared_in(interface)[wrapper.name]
        member = replace(
            wrapper,
            interface=wi.InterfaceMember(interface.path, interface.name, interface.namespace, start, end),
            siblings=siblings,
            shared=shared,
        )
        break
    # Every supertype but the implemented interface must be silent on the
    # name: a call through another interface declaring it (a second base, or
    # a base of the interface) dispatches to the wrapper as well, and is not
    # a family type here.
    chosen = None if member.interface is None else _full_name(member.interface.package, member.interface.name)
    if any(info.full != chosen and wrapper.name in info.members for info in supertypes):
        return None, wi.NAME_NOT_UNIQUE
    interface_at = None if member.interface is None else (member.interface.path, member.interface.start)
    declared = [d for d in index.members.get(wrapper.name, []) if d != interface_at]
    family_types = [owner]
    if member.interface is not None:
        family_types += [t for t in index.types.get(member.interface.name, []) if t.path == member.interface.path]
    family_types += [
        t for s in member.siblings for t in index.types.get(s.owner, []) if t.path == s.path and t.namespace == s.namespace
    ]
    inside_family: set[tuple[str, int]] = set()
    for decl in family_types:
        inside = [d for d in declared if d[0] == decl.path and decl.start <= d[1] < decl.end]
        inside_family.update(inside)
        expected = 0 if decl.kind == "interface" else 1
        if len(inside) != expected:
            return None, wi.NAME_NOT_UNIQUE
    if _extension_declared([d for d in declared if d not in inside_family], wrapper.name):
        return None, wi.NAME_NOT_UNIQUE
    if owner.open or wrapper.owner in index.subclassed:
        return None, wi.OVERRIDABLE
    from flag_cleanup import csharp_family

    index.family = csharp_family.family_of(index.repo_dir, member, index)
    fakes = fakes_of([member])
    if fakes:
        # A test sets a fake's knob without naming the wrapper, its class or
        # its interface; it spells the fake, and that file is judged too.
        owners = {s.owner for group in fakes.values() for s in group}
        for path in cross_file_keys.mention_files(index.repo_dir, owners, exclude=()):
            if not path.endswith(EXTENSIONS) or path in index.parsed:
                continue
            if _read(path) is None:
                return None, wi.SIBLING_NOT_INERT
            index.parsed.append(path)
    return member, None


def _full_name(namespace: str, name: str) -> str:
    return f"{namespace}.{name}" if namespace else name


def _extension_declared(declarations: list[tuple[str, int]], name: str) -> bool:
    """Whether a declaration of ``name`` outside the family may be an
    extension member: a method with a ``this`` parameter, or anything in a
    file that may hold an ``extension`` block (which the grammar misreads).
    Overload resolution falls back to an extension method when the
    instance member does not fit the call, so a call that looks like the
    wrapper's may be the extension's. Unreadable counts as one."""
    from flag_cleanup import csharp_family

    for path in sorted({p for p, _ in declarations}):
        source = _read(path)
        if source is None or csharp_family._EXTENSION_BLOCK.search(source):
            return True
        starts = {start for p, start in declarations if p == path}
        for node in cross_file_keys.walk(parse(source)):
            if node.start_byte not in starts or node.type != "method_declaration" or _name_of(node) != name:
                continue
            parameters = node.child_by_field_name("parameters")
            first = next((p for p in (parameters.named_children if parameters else ()) if p.type == "parameter"), None)
            if first is not None and any(c.type == "modifier" and _text(c) == "this" for c in first.children):
                return True
    return False


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


_LOCATORS = frozenset({"GetRequiredService", "GetService", "GetRequiredKeyedService", "GetKeyedService"})
_DI_NAMESPACE = "Microsoft.Extensions.DependencyInjection"
_SUBSTITUTE_NAMESPACE = "NSubstitute"


def _single_type_argument(generic: Node | None, names: frozenset[str]) -> Node | None:
    """``T`` in ``Name<T>`` when ``generic`` is a ``generic_name`` whose name
    is one of ``names`` and which has exactly one type argument."""
    if generic is None or generic.type != "generic_name" or not generic.named_children:
        return None
    if _text(generic.named_children[0]) not in names:
        return None
    arguments = _child(generic, "type_argument_list")
    types = list(arguments.named_children) if arguments is not None else []
    return types[0] if len(types) == 1 else None


def _called_member(value: Node | None) -> Node | None:
    """The ``member_access_expression`` an invocation calls."""
    if value is None or value.type != "invocation_expression":
        return None
    function = value.child_by_field_name("function")
    return function if function is not None and function.type == "member_access_expression" else None


def _locator_type(value: Node | None) -> Node | None:
    """``T`` in ``<recv>.GetRequiredService<T>(...)`` (and the other lookups)."""
    function = _called_member(value)
    return None if function is None else _single_type_argument(function.child_by_field_name("name"), _LOCATORS)


def _substitute_type(value: Node | None) -> Node | None:
    """``T`` in ``Substitute.For<T>(...)``."""
    function = _called_member(value)
    if function is None:
        return None
    receiver = function.child_by_field_name("expression")
    if receiver is None or receiver.type != "identifier" or _text(receiver) != "Substitute":
        return None
    return _single_type_argument(function.child_by_field_name("name"), frozenset({"For"}))


def _namespace_in_scope(at: Node, reader: xcs._Reader, namespace: str) -> bool:
    """Whether ``namespace`` is certainly imported at ``at``: items that may
    or may not reach the file only ever refuse, so they do not count."""
    return any(
        u.kind == "namespace" and u.certain and u.target == namespace
        for level in xcs._levels(at, reader.root, reader.unit_extra)
        for u in level.usings
    )


def _locator_ok(at: Node, reader: xcs._Reader) -> bool:
    """Whether a ``GetRequiredService<T>()`` here can only be the DI lookup:
    its namespace is in scope, and no repository type declares a method of
    one of those names."""
    if any(reader.index.spelled_where_unparsed(n) for n in _LOCATORS):
        return False
    if not _namespace_in_scope(at, reader, _DI_NAMESPACE):
        return False
    return not any(
        _LOCATORS & info.members for infos in reader.index.by_last.values() for info in infos
    )


def _substitute_ok(at: Node, reader: xcs._Reader, readers: _Readers) -> bool:
    """Whether ``Substitute.For<T>()`` here can only be NSubstitute's: the
    namespace is in scope, no repository type named ``Substitute`` has a
    member ``For``, no alias is named ``Substitute``, and no local, parameter
    or member shadows the name at a ``Substitute.For`` receiver."""
    if reader.index.spelled_where_unparsed("Substitute") or not _namespace_in_scope(
        at, reader, _SUBSTITUTE_NAMESPACE
    ):
        return False
    if any("For" in info.members for info in reader.index.by_last.get("Substitute", ())):
        return False
    for node in reader.occurrences("Substitute"):
        if node.type != "identifier":
            continue
        use = node.parent
        if use is not None and use.type == "member_access_expression":
            if any(
                u.kind == "alias" and u.alias == "Substitute"
                for level in xcs._levels(node, reader.root, reader.unit_extra)
                for u in level.usings
            ):
                return False
            if use.child_by_field_name("expression") == node and (
                _binding(node, "Substitute", readers, reader.path) is not None
            ):
                return False
    return True


def _creation_type(value: Node | None, factories: frozenset[str] = frozenset()) -> Node | str:
    """The type ``var x = <value>`` binds: a ``new C(...)``'s ``C``, or, where
    the matching factory is enabled, the ``T`` of a typed lookup."""
    if value is not None and value.type == "object_creation_expression":
        type_node = value.child_by_field_name("type")
        if type_node is not None:
            return type_node
    if "locator" in factories:
        found = _locator_type(value)
        if found is not None:
            return found
    if "substitute" in factories:
        found = _substitute_type(value)
        if found is not None:
            return found
    return _UNTYPED


def _deconstructed(declarator: Node) -> list[Node] | None:
    """The identifiers (or ``_`` discards) of a flat ``var (a, b) = …``
    declarator of a local declaration statement, else ``None``. Such a
    local is scoped by its block like any other, unlike a pattern's."""
    pattern = _named(declarator)[0] if _named(declarator) else None
    declaration = declarator.parent
    if (
        pattern is None
        or pattern.type != "tuple_pattern"
        or declaration is None
        or declaration.type != "variable_declaration"
        or declaration.parent is None
        or declaration.parent.type != "local_declaration_statement"
    ):
        return None
    type_node = declaration.child_by_field_name("type")
    parts = _named(pattern)
    if type_node is None or type_node.type != "implicit_type" or any(p.type not in ("identifier", "discard") for p in parts):
        return None
    return parts


def _deconstructed_type(declarator: Node, name: str, readers: _Readers | None, path: str | None) -> Node | str:
    """The type ``var (…, name, …) = Helper(…)`` binds ``name`` to: the
    element at its position of the tuple ``Helper`` is declared to return,
    when ``Helper`` can only be one method -- declared once by the innermost
    type around the call, no local or parameter of the name in scope, not
    generic, and no base of the type or another part of it may declare the
    name too. ``_UNTYPED`` otherwise."""
    parts = _deconstructed(declarator)
    values = [c for c in _named(declarator)[1:]]
    if parts is None or readers is None or path is None or len(values) != 1:
        return _UNTYPED
    call = values[0]
    function = call.child_by_field_name("function") if call.type == "invocation_expression" else None
    if function is None or function.type != "identifier":
        return _UNTYPED
    method_name = _text(function)
    if _binding(call, method_name, readers, path, locals_only=True) is not None:
        return _UNTYPED
    scope = call.parent
    while scope is not None and scope.type not in _SCOPE_TYPES:
        scope = scope.parent
    if scope is None or _inherits(scope, method_name, readers, path):
        return _UNTYPED
    body = scope.child_by_field_name("body")
    declared = [m for m in (body.named_children if body is not None else ()) if method_name in _member_names(m)]
    if len(declared) != 1 or declared[0].type != "method_declaration":
        return _UNTYPED
    method = declared[0]
    returns = method.child_by_field_name("returns")
    if _child(method, "type_parameter_list") is not None or returns is None or returns.type != "tuple_type":
        return _UNTYPED
    elements = [c for c in _named(returns) if c.type == "tuple_element"]
    if len(elements) != len(parts) or len(elements) != len(_named(returns)):
        return _UNTYPED
    for part, element in zip(parts, elements):
        if part.type == "identifier" and _text(part) == name:
            return element.child_by_field_name("type") or _UNTYPED
    return _UNTYPED


def _declarator_type(
    declaration: Node,
    name: str,
    factories: frozenset[str] = frozenset(),
    readers: _Readers | None = None,
    path: str | None = None,
) -> Node | str | None:
    """The type a ``variable_declaration`` binds ``name`` to, if it binds it.
    A deconstructing local (``var (a, b) = Helper()``) is typed only when
    ``readers`` can tell what ``Helper`` returns."""
    type_node = declaration.child_by_field_name("type")
    for declarator in declaration.named_children:
        if declarator.type != "variable_declarator":
            continue
        parts = _deconstructed(declarator)
        if parts is not None and any(p.type == "identifier" and _text(p) == name for p in parts):
            return _deconstructed_type(declarator, name, readers, path)
        if _name_of(declarator) != name:
            continue
        if type_node is None:
            return _UNTYPED
        if type_node.type == "implicit_type":
            values = [c for c in _named(declarator) if c.id != declarator.child_by_field_name("name").id]
            return _creation_type(values[-1] if values else None, factories)
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
            if parent.type == "tuple_pattern" and parent.parent is not None and _deconstructed(parent.parent) is not None:
                continue  # a local, scoped by its block (see _declarator_type)
            if (
                parent.type.endswith(("_pattern", "_designation"))
                or parent.type in ("declaration_expression", "from_clause", "let_clause", "join_clause")
                or parent.type == "query_continuation"
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


def _local_binding(
    statements: list[Node],
    at: Node,
    name: str,
    factories: frozenset[str] = frozenset(),
    readers: _Readers | None = None,
    path: str | None = None,
) -> Node | str | None:
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
                found = _declarator_type(declaration, name, factories, readers, path) or found
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
                found = _local_binding(_statements_in_scope(scope), at, name, readers.factories(), readers, path)
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
                        found = _declarator_type(part, name, readers.factories()) or found
            elif scope.type == "compilation_unit" and _in_global_statement(at):
                found = _local_binding(
                    [c for c in scope.named_children if c.type == "global_statement"],
                    at,
                    name,
                    readers.factories(),
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


#: Moq setup and verification methods whose lambda parameter is the mocked type.
_MOQ_METHODS = frozenset({"Setup", "SetupGet", "Verify", "VerifyGet"})


def _moq_mock(access: Node, name: str, readers: _Readers, path: str) -> tuple[str, Node] | None:
    """For ``name`` a lambda parameter in ``<m>.Setup(name => name.M...)``,
    where ``<m>``'s declared type is ``Mock<T>``: ``(<m>, T)``."""
    lam = access.parent
    while lam is not None and lam.type != "lambda_expression":
        if lam.type in _MEMBERS:
            return None
        lam = lam.parent
    if lam is None or _parameter_type(lam.child_by_field_name("parameters"), name) is None:
        return None
    argument = lam.parent
    arguments = argument.parent if argument is not None and argument.type == "argument" else None
    call = arguments.parent if arguments is not None else None
    if call is None or call.type != "invocation_expression":
        return None
    if [c for c in arguments.named_children if c.type == "argument"][:1] != [argument]:
        return None
    function = call.child_by_field_name("function")
    if function is None or function.type != "member_access_expression":
        return None
    method = function.child_by_field_name("name")
    mock = function.child_by_field_name("expression")
    if method is None or _text(method) not in _MOQ_METHODS or mock is None or mock.type != "identifier":
        return None
    bound = _binding(function, _text(mock), readers, path)
    if bound is None or isinstance(bound, str):
        return None
    argument_type = _moq_argument(bound, function, readers.get(path))
    return None if argument_type is None else (_text(mock), argument_type)


def _moq_argument(node: Node, at: Node, reader: xcs._Reader | None) -> Node | None:
    """``T`` when declared type ``node`` can only be Moq's ``Mock<T>`` at ``at``."""
    if reader is None or reader.index.may_declare("Mock"):
        return None
    if node.type == "qualified_name":
        qualifier = node.child_by_field_name("qualifier")
        if qualifier is None or _text(qualifier) != "Moq":
            return None
        node = node.child_by_field_name("name")
    elif not _namespace_in_scope(at, reader, "Moq"):
        return None
    return _single_type_argument(node, frozenset({"Mock"}))


def _moq_call_receiver(access: Node) -> str | None:
    """``m`` when ``access`` sits in the lambda of ``m.Setup(…)`` (or
    ``SetupGet``, ``Verify``, ``VerifyGet``, ``this.m.…``), whatever ``m``
    is bound to."""
    lam = access.parent
    while lam is not None and lam.type != "lambda_expression":
        if lam.type in _MEMBERS:
            return None
        lam = lam.parent
    argument = lam.parent if lam is not None else None
    call = argument.parent.parent if argument is not None and argument.parent is not None else None
    if argument is None or argument.type != "argument" or call is None or call.type != "invocation_expression":
        return None
    function = call.child_by_field_name("function")
    if function is None or function.type != "member_access_expression":
        return None
    method = function.child_by_field_name("name")
    if method is None or _text(method) not in _MOQ_METHODS:
        return None
    mock = _unwrap(function.child_by_field_name("expression"))
    if mock is not None and mock.type == "identifier":
        return _text(mock)
    if mock is not None and mock.type == "member_access_expression":
        inner, name = mock.child_by_field_name("expression"), mock.child_by_field_name("name")
        if inner is not None and inner.type == "this" and name is not None and name.type == "identifier":
            return _text(name)
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


def _nested_type_full(at: Node, name: str, reader: xcs._Reader) -> str | None:
    """The full name of the type ``name`` at ``at`` means when that is a
    type an enclosing type declares in its own body (C# finds it there
    before any namespace): ``""`` when something nearer may claim the name
    first (a type parameter, a base's member, another part of a type) or the
    one claiming it is ambiguous or generic, and ``None`` when no enclosing
    type declares it."""
    if name in reader.part_members or xcs._has_markup_part(reader.path):
        return ""
    scope = at.parent
    while scope is not None:
        parameters = _child(scope, "type_parameter_list")
        if parameters is not None and any(_text(p) == name for p in parameters.named_children):
            return ""
        if scope.type in _TYPES:
            body = scope.child_by_field_name("body")
            own = [m for m in (body.named_children if body is not None else ()) if m.type in _TYPES and _name_of(m) == name]
            if own:
                chain = xcs._type_chain(own[0], strict=False)
                if len(own) != 1 or chain is None or _child(own[0], "type_parameter_list") is not None:
                    return ""
                return _full_name(xcs._namespace_of(own[0]), ".".join(chain))
            supertypes = xcs._supertypes(scope)
            if supertypes and name in reader.index.inherited_members(supertypes):
                return ""
        scope = scope.parent
    return None


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


def _enclosing_test_method(at: Node, data: bytes) -> bool:
    """Whether ``at`` sits in a C# test method, parameterised included."""
    node = at.parent
    while node is not None and node.type != "method_declaration":
        node = node.parent
    return node is not None and mock_stubs._csharp_any_test(node, data)


def _in_nameof(at: Node) -> bool:
    scope = at.parent
    while scope is not None and scope.type not in _MEMBERS:
        if scope.type == "invocation_expression":
            function = scope.child_by_field_name("function")
            if function is not None and function.type == "identifier" and _text(function) == "nameof":
                return True
        scope = scope.parent
    return False


#: A self-closing ``<see>`` or ``<seealso>`` whose only attribute is
#: ``cref``: the one shape rewritten to ``<c>…</c>`` when its member goes.
_SEE = re.compile(r"""<(?:see|seealso)\s+cref\s*=\s*(["'])([^"'<>]*)\1\s*/>""")
#: A cref target judged here: a dotted name, a method's empty parameter list
#: allowed. An ID string (``P:Ns.T.M``), generics and ``global::`` are not.
_CREF_TARGET = re.compile(r"@?[^\W\d]\w*(?:\.@?[^\W\d]\w*)*(?:\(\))?")


def _documents_a_type(comment: Node) -> bool:
    """Whether a doc comment documents a type (whose own members its crefs
    see first), or nothing this can tell."""
    node = comment.next_named_sibling
    while node is not None and node.type == "comment":
        node = node.next_named_sibling
    return node is None or node.type in _TYPES


def _cref_owner(target: str, at: Node, wrapper: wi.Wrapper, index: Index) -> str | None:
    """What a cref naming a member called the wrapper's name refers to:
    ``"family"`` when its type is the interface, the wrapper's class or a
    sibling (for an unqualified one, the type its comment sits in); a
    ``"foreign"`` member when every type of its qualifier's name declares
    the member itself (or, unqualified, the enclosing type does), which
    hides any inherited one; ``None`` when this cannot tell."""
    family = index.family
    if family is None or _CREF_TARGET.fullmatch(target) is None:
        return None
    if "(" in target and wrapper.kind == "property":
        return None
    parts = [p.removeprefix("@") for p in target.split("(", 1)[0].split(".")]
    if len(parts) == 1:
        scope = None if _documents_a_type(at) else _enclosing_type(at)
        if scope is None:
            return None
        if _is_family_type(scope, family):
            return "family"
        return "foreign" if _member_binding(scope, wrapper.name) is not None else None
    qualifier = parts[-2]
    checkout = xcs.csharp_index(index.repo_dir)
    infos = checkout.by_last.get(qualifier, ())
    if qualifier in {n.removeprefix("@") for n in family.simple_names}:
        # Another repository type of the name may be the one meant.
        return "family" if all(info.full in family.full_names for info in infos) else None
    if checkout.incomplete or not infos:
        return None
    return "foreign" if all(wrapper.name in info.members and not info.partial for info in infos) else None


def _crefs(root: Node, wrapper: wi.Wrapper, index: Index) -> list[tuple[Node, str | None]]:
    """Each comment whose ``cref`` names a member called the wrapper's name,
    with its text once every reference there to a family member is plain
    ``<c>`` text; ``None`` when one of them cannot be rewritten (an
    ``<inheritdoc>``, a ``<see>`` with content, any other tag) or may bind
    to the member through a type this cannot tell. Deleting the member would
    leave that one a broken reference (CS1574). A comment whose every such
    reference names another type's own member is left out."""
    found: list[tuple[Node, str | None]] = []
    for node in cross_file_keys.walk(root):
        if node.type != "comment":
            continue
        text = _text(node)
        pieces: list[tuple[int, int, str]] = []
        mentioned = keep = False
        for match in xcs._CREF.finditer(text):
            target = match.group(1)
            if target.split("(", 1)[0].rsplit(".", 1)[-1] != wrapper.name:
                continue
            mentioned = True
            owner = _cref_owner(target, node, wrapper, index)
            if owner == "foreign":
                continue
            tag = next(
                (m for m in _SEE.finditer(text) if m.start() <= match.start() and match.end() <= m.end()), None
            )
            if owner == "family" and tag is not None:
                pieces.append((tag.start(), tag.end(), f"<c>{target}</c>"))
            else:
                keep = True
        if not mentioned:
            continue
        if keep:
            found.append((node, None))
        elif pieces:
            for start, end, replacement in sorted(pieces, reverse=True):
                text = text[:start] + replacement + text[end:]
            found.append((node, text))
    return found


def doc_references(path: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Reference]:
    """The doc comments of a file whose crefs to the member are rewritten to
    plain text when it goes (:func:`_crefs`), where a family value may be."""
    from flag_cleanup import csharp_family

    source = _read(path)
    if source is None or "cref" not in source:
        return []
    root = parse(source)
    family = index.family
    if family is not None and (
        not csharp_family.reaches(index.repo_dir, path, family) or csharp_family.family_free(source, root, family)
    ):
        return []
    return [wi.Reference(path, wi.line_of(n), _text(n), after) for n, after in _crefs(root, wrapper, index) if after]


def names_only(node: Node, kind: str = "method") -> bool:
    """Whether an identifier only names something: the declared name of a
    method or property, a record's positional parameter (a property), or a
    named-argument label."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _WRAPPER_DECLARATIONS and parent.child_by_field_name("name") == node:
        return True
    if (
        parent.type == "parameter"
        and parent.child_by_field_name("name") == node
        and parent.parent is not None
        and parent.parent.type == "parameter_list"
        and parent.parent.parent is not None
        and parent.parent.parent.type in ("record_declaration", "record_struct_declaration")
    ):
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


# --- Foreign mentions -----------------------------------------------------------
#
# Each of these only ever DROPS a mention, as provably not the wrapper's
# member; none marks one. A wrong answer here lets the wrapper be deleted
# under a read that was really of it, so every one returns "not foreign"
# whenever it cannot tell.


def _unwrap(receiver: Node | None) -> Node | None:
    """``x`` in ``x!`` (any number of them); ``(x)`` stays as written."""
    while receiver is not None and receiver.type == "postfix_unary_expression" and _text(receiver).endswith("!"):
        receiver = receiver.named_children[0] if receiver.named_children else None
    return receiver


def _foreign_type(type_node: Node, reader: xcs._Reader, family) -> bool:
    """Whether a declared type certainly is no family type: it does not
    resolve to one, its simple name is no family type's (nor any alias of
    one's, which :class:`~flag_cleanup.csharp_family.Family` collects over
    the checkout), no alias in scope here takes that name, and nothing
    nearer -- a type parameter, a nested type -- may claim it. A type
    parameter is never foreign: its constraints may name a family type."""
    if _type_full(type_node, reader) in family.full_names:
        return False
    node: Node | None = type_node
    while node is not None and node.type in ("nullable_type", "qualified_name") and node.named_children:
        node = node.child_by_field_name("name") if node.type == "qualified_name" else node.named_children[0]
    if node is not None and node.type == "generic_name":
        node = node.named_children[0] if node.named_children else None
    if node is None:
        return False
    if node.type == "predefined_type":
        return True
    if node.type != "identifier":
        return False
    spelled = _text(node)
    simple = spelled.removeprefix("@")
    if simple == "dynamic" or _type_shadowed(node, spelled, reader) or _type_shadowed(node, simple, reader):
        return False
    if simple in {n.removeprefix("@") for n in family.simple_names}:
        return False
    return not any(
        u.kind == "alias" and (u.alias or "").removeprefix("@") == simple
        for level in xcs._levels(type_node, reader.root, reader.unit_extra)
        for u in level.usings
    )


def _is_family_type(declaration: Node, family) -> bool:
    """Whether a type declaration may be a family type; unknown counts."""
    chain = xcs._type_chain(declaration, strict=False)
    name = _name_of(declaration)
    if chain is None or name is None:
        return True
    namespace = xcs._namespace_of(declaration)
    full = ".".join([namespace, *chain]) if namespace else ".".join(chain)
    return full in family.full_names or name.removeprefix("@") in {
        n.removeprefix("@") for n in family.simple_names
    }


def _in_pattern_path(node: Node) -> bool:
    """Whether ``node`` is a segment of an extended property pattern's
    dotted name (``x is T { A.B: true }``), at any position."""
    top = node
    while top.parent is not None and top.parent.type == "member_access_expression":
        top = top.parent
    if top.id == node.id:
        return False  # a lone name is a plain property subpattern
    holder = top.parent
    first = _named(holder)[:1] if holder is not None and holder.type == "subpattern" else []
    return bool(first) and first[0].id == top.id


def _target_object_type(node: Node, readers: _Readers, path: str) -> Node | None:
    """The declared type of the OTHER object a bare name is a member of: the
    created type of an object initializer, the bound type of a ``with`` or
    ``?.`` receiver, the type of a property pattern. Only the direct parent
    chain counts (``new T { Inner = { X = 1 } }`` sets ``Inner``'s ``X``,
    not ``T``'s). ``None`` when unknown."""
    parent = node.parent
    if parent is None:
        return None
    inner: Node | None = None
    if parent.type == "assignment_expression" and _field(parent, node) == "left":
        initializer = parent.parent
        creation = initializer.parent if initializer is not None and initializer.type == "initializer_expression" else None
        if creation is None or creation.type != "object_creation_expression" or _field(creation, initializer) != "initializer":
            return None
        return creation.child_by_field_name("type")
    if parent.type in ("with_initializer", "subpattern"):
        first = _named(parent)[:1]
        if not first or first[0].id != node.id:
            return None  # the value, not the member set or matched
        holder = parent.parent
        if parent.type == "subpattern":
            pattern = holder.parent if holder is not None and holder.type == "property_pattern_clause" else None
            return pattern.child_by_field_name("type") if pattern is not None and pattern.type == "recursive_pattern" else None
        if holder is None or holder.type != "with_expression" or not _named(holder):
            return None
        inner = _named(holder)[0]
    elif parent.type == "member_binding_expression" and _field(parent, node) == "name":
        access = parent.parent
        if access is None or access.type != "conditional_access_expression" or _field(access, parent) == "condition":
            return None
        inner = access.child_by_field_name("condition")
    if inner is None or inner.type != "identifier":
        return None
    bound = _binding(node, _text(inner), readers, path)
    return bound if bound is not None and not isinstance(bound, str) else None


def _declaring_scope(at: Node, name: str) -> Node | None:
    """The node whose scope declares the ``name`` read at ``at``: a block, a
    function or lambda with that parameter, a ``for``/``foreach``/``using``
    header, or the enclosing type when no local declares it. ``None`` when
    that cannot be told: top-level statements, or a primary-constructor
    parameter of the name (which wins in initializers, not in bodies). A
    pattern, ``out`` or query variable is the caller's to rule out."""
    scope = at.parent
    while scope is not None:
        if scope.type in ("block", "switch_section"):
            if _local_binding(_statements_in_scope(scope), at, name) is not None:
                return scope.parent if scope.type == "switch_section" else scope
        elif scope.type in _FUNCTIONS:
            if _parameter_type(scope.child_by_field_name("parameters"), name) is not None:
                return scope
        elif scope.type == "accessor_declaration" and name == "value":
            return scope
        elif scope.type == "foreach_statement":
            left = scope.child_by_field_name("left")
            if left is not None and left.type == "identifier" and _text(left) == name:
                return scope
        elif scope.type in ("for_statement", "using_statement", "fixed_statement"):
            for part in scope.named_children:
                if part.type == "variable_declaration" and part.start_byte < at.start_byte and _declarator_type(part, name):
                    return scope
        elif scope.type == "compilation_unit":
            return None
        if scope.type in _SCOPE_TYPES:
            return None if _primary_parameter(scope, name) is not None else scope
        scope = scope.parent
    return None


#: Operators a method group can never be an operand of.
_VALUE_OPERATORS = frozenset({"&&", "||", "<", ">", "<=", ">=", "*", "/", "%", "&", "|", "^", "<<", ">>", ">>>"})
#: Literals of value types: ``==`` against one is never a delegate comparison.
_VALUE_LITERALS = frozenset({"integer_literal", "real_literal", "character_literal", "boolean_literal"})


def _value_use(access: Node) -> bool:
    """Whether ``x.Other`` here can only be a field, property or event, never
    a method group: a group converts to a delegate (an extension method's
    included), so ``Func<int> f = x.Other`` is no evidence of a member."""
    parent = access.parent
    if parent is None:
        return False
    if parent.type in ("member_access_expression", "element_access_expression"):
        return _field(parent, access) == "expression"
    if parent.type == "conditional_access_expression":
        return _field(parent, access) == "condition"
    if parent.type == "prefix_unary_expression":
        return bool(parent.children) and parent.children[0].type in ("!", "~", "-")
    if parent.type == "binary_expression":
        operator = parent.child_by_field_name("operator")
        op = _text(operator) if operator is not None else ""
        if op in _VALUE_OPERATORS:
            return True
        if op in ("==", "!="):
            left, right = parent.child_by_field_name("left"), parent.child_by_field_name("right")
            other = right if left is not None and left.id == access.id else left
            return other is not None and other.type in _VALUE_LITERALS
    return False


def _group_position(access: Node) -> bool:
    """Whether ``x.Other`` here is an argument or an operand of ``==``/``!=``:
    a position a method group may take, and a field, property or event too."""
    parent = access.parent
    if parent is None:
        return False
    if parent.type == "argument":
        return True
    if parent.type == "binary_expression":
        operator = parent.child_by_field_name("operator")
        return operator is not None and _text(operator) in ("==", "!=")
    return False


def _co_used(at: Node, name: str, family, repo_dir: str) -> bool:
    """Whether, where ``name`` is declared, the same binding of ``name`` is
    also read as ``name.Other`` for a field, property or event no family
    type declares or inherits: a value of a family type has no ``Other``.

    ``name.Other`` passed as an argument, or compared with ``==``/``!=``,
    counts too while no file of the checkout may declare or call a method
    ``Other`` (:func:`csharp_family.spells_method`): a method group there
    would need a library's extension method of that name taking any type,
    which is assumed away.

    Judged in the declaring block or function, or, for a name no local
    declares, in the member around ``at``; a nested lambda, local function
    or block declaring its own ``name`` is another binding and does not
    count. Never inside a generic declaration, where the receiver may be a
    type parameter constrained to a family type and to a type with
    ``Other``. The caller rules out ``dynamic`` and extension blocks."""
    if _pattern_binds(at, name):
        return False
    scope = _declaring_scope(at, name)
    if scope is None:
        return False
    region: Node | None = scope
    if scope.type in _SCOPE_TYPES:
        region = at.parent
        while region is not None and region.type not in _MEMBERS:
            if region.type in _SCOPE_TYPES:
                return False
            region = region.parent
        if region is None:
            return False
    ancestor = at.parent
    while ancestor is not None:
        if _child(ancestor, "type_parameter_list") is not None:
            return False
        ancestor = ancestor.parent
    from flag_cleanup import csharp_family

    known = {m.removeprefix("@") for m in family.members}
    for node in cross_file_keys.walk(region):
        if node.type != "member_access_expression":
            continue
        expression = node.child_by_field_name("expression")
        member = node.child_by_field_name("name")
        if expression is None or member is None or expression.type != "identifier" or member.type != "identifier":
            continue
        if _text(expression).removeprefix("@") != name.removeprefix("@"):
            continue
        other = _text(member).removeprefix("@")
        if other in known:
            continue
        if not _value_use(node) and not (
            _group_position(node) and not csharp_family.spells_method(repo_dir, other)
        ):
            continue
        same = _declaring_scope(expression, _text(expression))
        if same is not None and same.id == scope.id:
            return True
    return False


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    root = parse(source)
    data = source.encode("utf-8")
    reader = xcs._Reader(root, path, index.repo_dir)
    readers = _Readers(index.repo_dir)
    readers.add(reader)
    readers.locator = _locator_ok(root, reader)
    readers.substitute = _substitute_ok(root, reader, readers)
    from flag_cleanup import csharp_family

    family = index.family
    if family is not None and (
        not csharp_family.reaches(index.repo_dir, path, family)
        or csharp_family.family_free(source, root, family)
    ):
        # No value of a family type can be here: no mention is the wrapper's.
        return []
    # A member name is evidence of a type only where nothing can bind any
    # name -- no `dynamic` and no extension block anywhere in the checkout
    # (a `var` takes its type from a member of any file) -- and where every
    # family type's members are known: no file that does not parse whole
    # may declare a family type or a supertype of one, and no sibling is
    # based on a type outside the repository.
    co_usage = (
        family is not None
        and not family.extension_blocks
        and not family.dynamic_anywhere
        and not family.opaque
        and not family.sibling_bases_external
        and family.members_complete
    )
    # Nor through a receiver in a member of the file whose own text leads to
    # none (proof 2 per member). It needs what co-usage needs but a complete
    # index: proof 2 reads an unparsed region as opaque when it spells a
    # name, and every supertype of a family type is known, or the wrapper or
    # sibling would not have qualified.
    free = (
        [(m.start_byte, m.end_byte) for m in csharp_family.free_members(source, root, family, index.repo_dir)]
        if family is not None
        and not family.extension_blocks
        and not family.dynamic_anywhere
        and not family.opaque
        and not family.sibling_bases_external
        else []
    )
    targets = {f"{wrapper.package}.{wrapper.owner}" if wrapper.package else wrapper.owner}
    if wrapper.interface is not None:
        interface = wrapper.interface
        targets.add(f"{interface.package}.{interface.name}" if interface.package else interface.name)
    # A read through a shared fake binds the member it keeps (wi.SharedFake).
    shared = {_full_name(s.namespace, s.chain or s.owner) for s in wrapper.shared}
    found: list[wi.Occurrence] = [
        wi.Occurrence(path, wi.line_of(c), wi.text_of(c), None, wi.UNPARSED_MENTION)
        for c, after in _crefs(root, wrapper, index)
        if after is None
    ]
    found += _knob_leftovers(path, source, root, reader, readers, wrapper, index)
    test_project: list[bool] = []
    #: receiver name -> reads of the member through it in the file; per test
    #: method, and how many of those were proven sibling reads.
    mentions: dict[str, int] = defaultdict(int)
    scoped_mentions: dict[tuple[str | None, str], int] = defaultdict(int)
    sibling_reads: dict[tuple[str | None, str], int] = defaultdict(int)
    methods = _methods_by_name(root)

    def sibling_read(access: Node, line: int, name: str | None) -> wi.Occurrence:
        """A read through a receiver typed as a sibling, whose member goes
        with the wrapper. Never marked: in production it keeps the wrapper;
        in a test method of a test project the test-double pass may delete
        the whole test (``in_test_double``)."""
        if not test_project:
            test_project.append(_test_project(path, index.repo_dir) is True)
        in_test = test_project[0] and _enclosing_test_method(access, data)
        scope = _method_scope(access, methods)
        if name is not None:
            sibling_reads[(scope, name)] += 1
        return wi.Occurrence(
            path, line, wi.text_of(access.parent or access), None, wi.SIBLING_RECEIVER, name, in_test,
            scope=scope,
        )

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
        for spelled_receiver in (_receiver_name(node), _moq_call_receiver(node)):
            if spelled_receiver is not None:
                mentions[spelled_receiver] += 1
                scoped_mentions[(_method_scope(node, methods), spelled_receiver)] += 1
        # Counted above all the same: the test-double pass matches mocks by
        # name, and a name read here is not proven anyone's. A bare name may
        # bind through a nested or inherited scope or a `using static`.
        if (
            parent is not None
            and parent.type == "member_access_expression"
            and _field(parent, node) == "name"
            and any(start <= node.start_byte < end for start, end in free)
        ):
            continue
        if _in_pattern_path(node):
            # `{ A.Name: … }` matches a member PATH of the tested value: no
            # receiver in it is bound where it is written.
            found.append(wi.Occurrence(path, line, wi.text_of(parent or node), None, wi.RECEIVER_UNPROVEN))
            continue
        receiver: Node | None = None
        access: Node
        if parent is not None and parent.type == "member_access_expression" and _field(parent, node) == "name":
            access = parent
            receiver = _unwrap(parent.child_by_field_name("expression"))
        else:
            access = node
        call = access.parent if access.parent is not None and access.parent.type == "invocation_expression" else None
        called = call is not None and call.child_by_field_name("function") == access
        if family is not None:
            assigned = (
                access.parent is not None
                and access.parent.type == "assignment_expression"
                and _field(access.parent, access) == "left"
            )
            # No family member can be written, or (a property) invoked.
            if (assigned and not family.settable) or (called and wrapper.kind == "property"):
                continue
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
            if family is not None and receiver is None and parent is not None:
                # A member of another object: drop it when that object's
                # type is certainly no family type.
                target = _target_object_type(node, readers, path)
                if target is not None and _foreign_type(target, reader, family):
                    continue
            found.append(wi.Occurrence(path, line, wi.text_of(parent or node), None, wi.RECEIVER_UNPROVEN))
            continue
        reason: str | None = None
        name: str | None = None
        if receiver is None or receiver.type == "this":
            in_owner = _in_owner(access, wrapper, path)
            if receiver is None and family is not None and not in_owner:
                # A non-family type declaring the name itself: its own member.
                enclosing = _enclosing_type(access)
                if (
                    enclosing is not None
                    and not _is_family_type(enclosing, family)
                    and _member_binding(enclosing, wrapper.name) is not None
                ):
                    continue
            bound = (
                _binding(access, wrapper.name, readers, path, locals_only=True) if receiver is None else None
            )
            if not in_owner or bound is not None:
                reason = wi.RECEIVER_UNPROVEN
        elif receiver.type == "identifier":
            name = _text(receiver)
            bound = _binding(access, name, readers, path)
            if family is not None and bound is not None and not isinstance(bound, str):
                if _foreign_type(bound, reader, family) or _type_full(bound, reader) in shared:
                    continue
                if _type_full(bound, reader) in family.sibling_full_names:
                    found.append(sibling_read(access, line, name))
                    continue
            if bound is None:
                static_type = wrapper.static and _resolves(receiver, reader, targets) is None
                reason = None if static_type else wi.RECEIVER_UNPROVEN
                name = None if static_type else name
            elif isinstance(bound, str):
                moq = _moq_mock(access, name, readers, path) if bound == _UNTYPED else None
                if moq is not None and _resolves(moq[1], reader, targets) is None:
                    found.append(
                        wi.Occurrence(path, line, wi.text_of(access.parent or access), None, None, moq[0], True)
                    )
                    continue
                if moq is not None and family is not None and _foreign_type(moq[1], reader, family):
                    continue  # the mock of another type
                reason = wi.RECEIVER_UNPROVEN
            else:
                reason = _resolves(bound, reader, targets)
            if (
                reason is not None
                and co_usage
                and (bound is None or bound == _UNTYPED)
                # Assumed, as proof 2 per member assumes it: no library member
                # hands a family value back as `dynamic`. The repository's own
                # cannot, co-usage being off while any file spells it.
                and _co_used(access, name, family, index.repo_dir)
            ):
                continue
        elif (
            receiver.type == "member_access_expression"
            and (inner := receiver.child_by_field_name("expression")) is not None
            and inner.type == "this"
            and (field_name := receiver.child_by_field_name("name")) is not None
            and field_name.type == "identifier"
        ):
            name = _text(field_name)
            bound = _binding(access, name, readers, path, class_only=True)
            if family is not None and bound is not None and not isinstance(bound, str):
                if _foreign_type(bound, reader, family) or _type_full(bound, reader) in shared:
                    continue
                if _type_full(bound, reader) in family.sibling_full_names:
                    found.append(sibling_read(access, line, name))
                    continue
            reason = (
                wi.RECEIVER_UNPROVEN
                if bound is None or isinstance(bound, str)
                else _resolves(bound, reader, targets)
            )
        elif receiver.type == "object_creation_expression":
            created = receiver.child_by_field_name("type")
            if family is not None and created is not None and (
                _foreign_type(created, reader, family) or _type_full(created, reader) in shared
            ):
                continue
            if family is not None and created is not None and _type_full(created, reader) in family.sibling_full_names:
                found.append(sibling_read(access, line, None))
                continue
            reason = wi.RECEIVER_UNPROVEN
        else:
            reason = wi.RECEIVER_UNPROVEN
        if reason is None and called and _argument_has_call(call):
            reason = wi.ARGUMENT_HAS_CALL
        shown = call if called else access
        double = _in_test_double(shown, data)
        span = (access.start_byte, access.end_byte) if reason is None else None
        suffix = "()" if wrapper.kind == "property" and reason is None else ""
        found.append(wi.Occurrence(path, line, wi.text_of(shown), span, reason, name, double, suffix))
    # The test-double pass deletes a test by the receiver's NAME, so a name
    # counts only where it means the sibling everywhere in its test method:
    # every read of the member through it there is a sibling read, and every
    # binding it may have there -- the method's own, and its classes'
    # members' -- is typed as a sibling. A local cannot shadow another in an
    # enclosing scope, so the read binds to one of those. Otherwise the read
    # stays, reported.
    proven = {
        (scope, name)
        for (scope, name), count in sibling_reads.items()
        if scope is not None
        and count == scoped_mentions[(scope, name)]
        and _bound_only_as_sibling(_scope_nodes(methods[scope]), name, reader, readers, family)
    }
    # The same for a mock's or substitute's name, which the test-double pass
    # also matches by name: every read through it is proven the wrapper's,
    # and every binding of it is a mock of, or typed as, the wrapper's class
    # or interface.
    bound_reads: dict[str, int] = defaultdict(int)
    for o in found:
        if o.receiver is not None and o.reason in (None, wi.ARGUMENT_HAS_CALL):
            bound_reads[o.receiver] += 1
    mocks = {
        name
        for name, count in bound_reads.items()
        if count == mentions[name] and _bound_only_as_mock(root, name, reader, readers, targets)
    }
    found = [
        replace(o, receiver=None, scope=None)
        if o.receiver is not None
        and (
            (o.reason == wi.SIBLING_RECEIVER and (o.scope, o.receiver) not in proven)
            or (o.in_test_double and o.reason in (None, wi.ARGUMENT_HAS_CALL) and o.receiver not in mocks)
        )
        else o
        for o in found
    ]
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def _receiver_name(node: Node) -> str | None:
    """The receiver name a read of member ``node`` is spelled through:
    ``gate`` in ``gate.M`` and in ``this.gate.M``."""
    parent = node.parent
    if parent is None or parent.type != "member_access_expression" or _field(parent, node) != "name":
        return None
    receiver = _unwrap(parent.child_by_field_name("expression"))
    if receiver is None:
        return None
    if receiver.type == "identifier":
        return _text(receiver)
    if receiver.type == "member_access_expression":
        inner = receiver.child_by_field_name("expression")
        name = receiver.child_by_field_name("name")
        if inner is not None and inner.type == "this" and name is not None and name.type == "identifier":
            return _text(name)
    return None


#: Where an identifier is the name a binding introduces, with a declared
#: type (field name of the type in the parent, or ``None``: never typed).
_TYPED_BINDINGS = {
    "parameter": "type",
    "property_declaration": "type",
    "declaration_pattern": "type",
    "declaration_expression": "type",
    "catch_declaration": "type",
    "foreach_statement": "type",
    "tuple_element": "type",
}
#: Every other construct whose identifiers may introduce a binding: none of
#: them is proven typed, so a name bound there is never proven a sibling.
_UNTYPED_BINDINGS = frozenset(
    {
        "from_clause", "let_clause", "join_clause", "join_into_clause", "query_continuation",
        "group_clause", "tuple_pattern", "recursive_pattern", "parenthesized_variable_designation",
        "var_pattern", "lambda_expression", "anonymous_method_expression", "local_function_statement",
        "method_declaration", "event_declaration", "enum_member_declaration", "using_statement",
        "fixed_statement", "labeled_statement", "type_parameter",
    }
)


def _methods_by_name(root: Node) -> dict[str, Node]:
    """The file's methods whose name no other method of the file shares: a
    test is told apart by its name alone."""
    counts: dict[str, list[Node]] = defaultdict(list)
    for node in cross_file_keys.walk(root):
        if node.type == "method_declaration" and (name := _name_of(node)) is not None:
            counts[name].append(node)
    return {name: nodes[0] for name, nodes in counts.items() if len(nodes) == 1}


def _method_scope(at: Node, methods: dict[str, Node]) -> str | None:
    """The name of the method ``at`` sits in (through lambdas and local
    functions), when no other method of the file has it."""
    node = at.parent
    while node is not None and node.type != "method_declaration":
        node = node.parent
    name = _name_of(node) if node is not None else None
    return name if name is not None and name in methods and methods[name].id == node.id else None


def _scope_nodes(method: Node) -> Iterator[Node]:
    """Every node where a simple name read in ``method`` may be bound: the
    method itself, and the member-level bindings of each type around it
    (field, event and property names, other members' names, primary
    constructor and record parameters), but not what other members bind
    inside them."""
    yield from cross_file_keys.walk(method)
    scope = method.parent
    while scope is not None:
        if scope.type in _TYPES:
            parameters = _child(scope, "parameter_list")
            if parameters is not None:
                yield from cross_file_keys.walk(parameters)
            body = scope.child_by_field_name("body")
            for member in body.named_children if body is not None else ():
                if member.type in ("field_declaration", "event_field_declaration"):
                    declaration = _child(member, "variable_declaration")
                    for declarator in declaration.named_children if declaration is not None else ():
                        name = declarator.child_by_field_name("name") if declarator.type == "variable_declarator" else None
                        if name is not None:
                            yield name
                elif (name := member.child_by_field_name("name")) is not None:
                    yield name
        scope = scope.parent


def _bound_only_as_sibling(
    nodes: Iterable[Node], name: str, reader: xcs._Reader, readers: _Readers, family
) -> bool:
    """Whether every binding of ``name`` among ``nodes`` is declared with a
    type that resolves to a sibling (:func:`_bound_only_as`)."""
    if family is None:
        return False
    siblings = family.sibling_full_names
    return _bound_only_as(nodes, name, readers, lambda t, at: _type_full(t, reader) in siblings, reader.path)


def _bound_only_as_mock(root: Node, name: str, reader: xcs._Reader, readers: _Readers, targets: set[str]) -> bool:
    """Whether every binding of ``name`` in the file is declared as Moq's
    ``Mock<T>``, or as ``T`` itself (an NSubstitute substitute's type), for
    ``T`` the wrapper's class or interface (:func:`_bound_only_as`)."""

    def mock(type_node: Node, at: Node) -> bool:
        if _resolves(type_node, reader, targets) is None:
            return True
        argument = _moq_argument(type_node, at, reader)
        return argument is not None and _resolves(argument, reader, targets) is None

    return _bound_only_as(cross_file_keys.walk(root), name, readers, mock, reader.path)


def _bound_only_as(
    nodes: Iterable[Node],
    name: str,
    readers: _Readers,
    accept: Callable[[Node, Node], bool],
    path: str | None = None,
) -> bool:
    """Whether every binding of ``name`` among ``nodes`` -- a field, local,
    parameter, property, pattern, ``out var``, ``foreach`` or ``catch``
    variable -- is declared with a type ``accept`` takes. A lambda
    parameter, a query variable or any other binding whose type is not
    written is not. A local deconstructed from a helper's tuple (``var (a,
    b) = Helper()``) is typed by the helper's declared return, given the
    file's ``path``. A tuple type's element name binds nothing, and is
    judged by its type like a binding, which can only refuse."""
    bare = name.removeprefix("@")

    def accepted(type_node, at: Node) -> bool:
        return (
            type_node is not None
            and not isinstance(type_node, str)
            and type_node.type != "implicit_type"
            and accept(type_node, at)
        )

    for node in nodes:
        if node.type not in ("identifier", "implicit_parameter") or _text(node).removeprefix("@") != bare:
            continue
        if node.type == "implicit_parameter":
            return False
        parent = node.parent
        if parent is None:
            continue
        field_name = _field(parent, node)
        if parent.type == "variable_declarator" and field_name == "name":
            declaration = parent.parent
            if declaration is None or declaration.type != "variable_declaration":
                return False
            if not accepted(_declarator_type(declaration, _text(node), readers.factories()), node):
                return False
        elif parent.type == "tuple_pattern" and parent.parent is not None and _deconstructed(parent.parent) is not None:
            declaration = parent.parent.parent
            if not accepted(_declarator_type(declaration, _text(node), readers.factories(), readers, path), node):
                return False
        elif parent.type in _TYPED_BINDINGS and field_name in ("name", "left"):
            if not accepted(parent.child_by_field_name(_TYPED_BINDINGS[parent.type]), node):
                return False
        elif parent.type in _UNTYPED_BINDINGS:
            return False
    return True


# --- Test fakes: where a knob is set ------------------------------------------------


def candidate_fake_owners(repo_dir: str, names: Iterable[str]) -> set[str]:
    """Simple names of the checkout's types declaring a member of one of
    ``names``: a superset of every fake a wrapper of those names can have."""
    wanted = set(names)
    return {
        info.last
        for infos in xcs.csharp_index(repo_dir).by_full.values()
        for info in infos
        if info.members & wanted
    }


def fakes_of(wrappers: Iterable[wi.Wrapper]) -> dict[str, tuple[wi.Sibling, ...]]:
    """Full name -> each test fake of the wrappers that a knob sets: one
    sibling per wrapper the fake serves, each with its own knob."""
    found: dict[str, list[wi.Sibling]] = defaultdict(list)
    for wrapper in wrappers:
        for s in wrapper.siblings:
            if s.test and s.knob:
                found[_full_name(s.namespace, s.type_chain)].append(s)
    return {full: tuple(group) for full, group in found.items()}


@dataclass(frozen=True)
class _Setting:
    """One place that sets, or may set, a test fake's knob."""

    node: Node
    #: ``fake`` in ``fake.Knob = …`` or ``this.fake.Knob = …``; ``host.Fake``
    #: in ``host.Fake.Knob = …`` (see :func:`_member_holder`).
    receiver: str | None
    #: The ``bool`` literal set, when it is plainly one.
    value: str | None
    #: The fake the setting's target is typed as, a sibling per wrapper it
    #: serves; ``None`` when that type is not known here (it may be any).
    fake: tuple[wi.Sibling, ...] | None
    #: The member written (``None`` for a primary constructor's argument).
    knob: str | None
    #: For a ``host.Fake`` receiver: ``(host, the full name of its type)``.
    holder: tuple[str, str] | None = None

    @property
    def sets_knob(self) -> bool:
        """Whether it sets ``fake``'s knob, or may set some fake's: a
        construction (``knob`` ``None``) sets one built through its
        constructor, any other setting the member it writes."""
        if self.fake is None:
            return True
        if self.knob is None:
            return any(s.construction[1] for s in self.fake)
        return any(not s.knob_primary and self.knob == s.knob for s in self.fake)


def _write_target(node: Node) -> Node | None:
    """What writes ``node`` (an assignment, a ``ref``/``out`` argument, a
    ``ref`` expression), or ``None`` for a read."""
    child, parent = node, node.parent
    while parent is not None and parent.type == "parenthesized_expression":
        child, parent = parent, parent.parent
    if parent is None:
        return None
    if parent.type == "assignment_expression" and _field(parent, child) == "left":
        return parent
    if parent.type == "ref_expression":
        return parent
    if parent.type == "argument" and any(not c.is_named and _text(c) in ("ref", "out") for c in parent.children):
        return parent
    while parent is not None and parent.type in ("argument", "tuple_expression"):
        child, parent = parent, parent.parent
    if parent is not None and parent.type == "assignment_expression" and _field(parent, child) == "left":
        return parent
    return None


def _plain_value(target: Node) -> str | None:
    """The literal a plain ``=`` assignment sets, else ``None``."""
    if target.type != "assignment_expression":
        return None
    operator = target.child_by_field_name("operator")
    right = target.child_by_field_name("right")
    if operator is None or _text(operator) != "=" or right is None or right.type != "boolean_literal":
        return None
    return _text(right)


def _member_holder(
    receiver: Node, at: Node, reader: xcs._Reader, readers: _Readers, path: str
) -> tuple[str, Node, xcs._Reader, str, str] | None:
    """For a knob write's receiver ``host.Fake`` -- ``host`` a local,
    parameter or member of the file whose declared type is a class, struct or
    record of the checkout, and ``Fake`` a field or auto-property that type
    declares itself -- ``(receiver text, Fake's declared type, the reader of
    the file declaring it, host, host's type's full name)``. A member read
    runs no code, so its declared type is the value's static type: the one
    that decides whether the write sets a fake. ``None`` otherwise."""
    inner = receiver.child_by_field_name("expression")
    name = receiver.child_by_field_name("name")
    if inner is None or name is None or inner.type != "identifier" or name.type != "identifier":
        return None
    host = _text(inner)
    bound = _binding(at, host, readers, path)
    if bound is None or isinstance(bound, str) or bound.type == "implicit_type":
        return None
    full = _type_full(bound, reader)
    infos = xcs.csharp_index(readers.repo_dir).by_full.get(full or "", ())
    if full is None or len(infos) != 1 or infos[0].generic or infos[0].partial:
        return None
    declaring = readers.get(infos[0].path)
    node = _find_type_in(declaring.root, infos[0].namespace, infos[0].chain) if declaring is not None else None
    if node is None or node.type not in _OWNERS:
        return None
    member = _own_state(node, _text(name).removeprefix("@"))
    if member is None:
        return None
    return f"{host}.{_text(name)}", member, declaring, host, full


def _primary_arity(type_node: Node) -> tuple[int, int]:
    """``(fewest, most)`` arguments a primary constructor takes: a parameter
    with a default may be left out (tree-sitter-c-sharp gives the default as
    an expression after the name)."""
    listed = _child(type_node, "parameter_list")
    parameters = [p for p in (listed.named_children if listed is not None else ()) if p.type == "parameter"]
    defaulted = 0
    for parameter in parameters:
        name = parameter.child_by_field_name("name")
        if name is not None and any(c.start_byte >= name.end_byte for c in parameter.named_children):
            defaulted += 1
    return len(parameters) - defaulted, len(parameters)


def _primary_index(type_node: Node, name: str | None) -> int | None:
    """The position of the primary constructor's parameter ``name``."""
    listed = _child(type_node, "parameter_list")
    parameters = [p for p in (listed.named_children if listed is not None else ()) if p.type == "parameter"]
    return next((i for i, p in enumerate(parameters) if _name_of(p) == name), None)


def _could_build(sibling: wi.Sibling, node: Node, passed: list[Node], path: str) -> bool:
    """Whether the construction ``node``, whose type is not known, may build
    ``sibling`` through its positional knob. A private nested fake is built
    only inside its outer type; a primary constructor takes as many
    arguments as its parameters allow; a constructor writing a member knob
    takes one ``bool``."""
    if sibling.chain is not None:
        outer = sibling.chain.split(".")[0]
        scope = node.parent
        while scope is not None and not (scope.type in _OWNERS and _name_of(scope) == outer):
            scope = scope.parent
        if scope is None or str(Path(path).resolve()) != str(Path(sibling.path).resolve()):
            return False
    if sibling.primary_arity is not None:
        fewest, most = sibling.primary_arity
        if not fewest <= len(passed) <= most:
            return False
        # The argument the knob takes, named or at its position, must be able
        # to be a `bool`: `new(new Svc())` sets no knob.
        named = [a for a in passed if (label := a.child_by_field_name("name")) is not None and _text(label) == sibling.knob]
        index = sibling.primary_knob_index
        at = named[0] if named else (passed[index] if index is not None and index < len(passed) else None)
        return at is None or not _not_bool(at)
    return len(passed) == 1 and not _not_bool(passed[0])


#: ``(repo_dir, fake owners)`` -> every name that may denote one of them
#: (:func:`_aliases`), or ``None``. Cleared by ``csharp_family.reset_cache``.
_FAKE_NAMES: dict[tuple[str, frozenset[str]], frozenset[str] | None] = {}


def _fake_names(repo_dir: str, owners: frozenset[str]) -> frozenset[str] | None:
    key = (repo_dir, owners)
    if key not in _FAKE_NAMES:
        names: set[str] = set()
        for owner in sorted(owners):
            aliases = _aliases(repo_dir, owner)
            if aliases is None:
                _FAKE_NAMES[key] = None
                break
            names |= aliases
        else:
            _FAKE_NAMES[key] = frozenset(names)
    return _FAKE_NAMES[key]


def _never_a_fake(declared: Node | None, node: Node, siblings: list[wi.Sibling], repo_dir: str) -> bool:
    """Whether the target-typed ``new(…)`` at ``node`` certainly builds no
    fake: the type its context declares (``declared``, or one
    ``ctor_param_csharp._target_name`` reads off a method's arrow body or
    ``return``) is generic, which a fake and an alias never are, or spells no
    name that may denote a fake (its name or an alias of it)."""
    from flag_cleanup import ctor_param_csharp

    if declared is not None:
        last = declared.child_by_field_name("name") if declared.type == "qualified_name" else declared
        if last is not None and last.type == "generic_name":
            return True
        name = xcs._simple_name(declared)
    else:
        name = ctor_param_csharp._target_name(node, "") if node.parent is not None and node.parent.type != "initializer_expression" else None
    if name is None:
        return False
    names = _fake_names(repo_dir, frozenset(s.owner for s in siblings))
    return names is not None and name.removeprefix("@") not in names


def _typed_fake(
    type_node: Node | str | None, reader: xcs._Reader, fakes: dict[str, tuple[wi.Sibling, ...]]
) -> tuple[bool, tuple[wi.Sibling, ...] | None]:
    """``(the type is known, the fake it is)``."""
    if type_node is None or isinstance(type_node, str) or type_node.type == "implicit_type":
        return False, None
    full = _type_full(type_node, reader)
    if full is None:
        return False, None
    return True, fakes.get(full)


def _knob_settings(
    root: Node, reader: xcs._Reader, readers: _Readers, path: str, fakes: dict[str, tuple[wi.Sibling, ...]]
) -> list[_Setting]:
    """Every place in the file that sets, or may set, a fake's knob: a write
    of ``x.Knob`` (any write: an assignment, compound or deconstructing, a
    ``ref``/``out``), an initializer's ``Knob = …``, a ``with``, and a
    construction of a fake set by its primary constructor. A target whose
    type is known and is no fake is not one."""
    siblings = [s for group in fakes.values() for s in group]
    member_knobs = {s.knob for s in siblings if not s.knob_primary}
    owners = {s.owner for s in siblings}
    found: list[_Setting] = []
    for node in cross_file_keys.walk(root):
        if node.type == "member_access_expression":
            name = node.child_by_field_name("name")
            knob = _text(name).removeprefix("@") if name is not None and name.type == "identifier" else None
            if knob not in member_knobs:
                continue
            target = _write_target(node)
            if target is None:
                continue
            receiver_node = _unwrap(node.child_by_field_name("expression"))
            receiver, bound, holder, typed_in = None, None, None, reader
            if receiver_node is not None and receiver_node.type == "identifier":
                receiver = _text(receiver_node)
                bound = _binding(node, receiver, readers, path)
            elif (
                receiver_node is not None
                and receiver_node.type == "member_access_expression"
                and (inner := receiver_node.child_by_field_name("expression")) is not None
                and inner.type == "this"
                and (field_name := receiver_node.child_by_field_name("name")) is not None
                and field_name.type == "identifier"
            ):
                receiver = _text(field_name)
                bound = _binding(node, receiver, readers, path, class_only=True)
            elif receiver_node is not None and receiver_node.type == "member_access_expression":
                held = _member_holder(receiver_node, node, reader, readers, path)
                if held is not None:
                    receiver, bound, typed_in, host, host_full = held
                    holder = (host, host_full)
            known, fake = _typed_fake(bound, typed_in, fakes)
            if known and fake is None:
                continue
            found.append(_Setting(target, receiver, _plain_value(target), fake, knob, holder))
        elif node.type == "assignment_expression" and node.parent is not None and node.parent.type == "initializer_expression":
            left = node.child_by_field_name("left")
            knob = _text(left).removeprefix("@") if left is not None and left.type == "identifier" else None
            if knob not in member_knobs:
                continue
            holder = node.parent.parent
            if holder is not None and holder.type == "object_creation_expression":
                known, fake = _typed_fake(holder.child_by_field_name("type"), reader, fakes)
                if known and fake is None:
                    continue
            elif holder is not None and holder.type == "anonymous_object_creation_expression":
                continue
            else:
                fake = None
            found.append(_Setting(node, None, _plain_value(node), fake, knob))
        elif node.type == "with_initializer":
            for child in node.named_children:
                if child.type == "identifier" and _text(child).removeprefix("@") in member_knobs:
                    found.append(_Setting(node, None, None, None, _text(child).removeprefix("@")))
        elif node.type in ("object_creation_expression", "implicit_object_creation_expression"):
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                known, fake = _typed_fake(type_node, reader, fakes)
                simple = xcs._simple_name(type_node)
                primary = fake is not None and any(s.construction[1] for s in fake)
                if (known and not primary) or (not known and simple not in owners):
                    continue
            else:
                # Target-typed `new(…)`: the declaration it initializes may
                # still spell the type.
                declarator = node.parent
                declaration = declarator.parent if declarator is not None and declarator.type == "variable_declarator" else None
                declared = declaration.child_by_field_name("type") if declaration is not None else None
                if declarator is not None and declarator.type == "property_declaration" and declarator.child_by_field_name("value") == node:
                    declared = declarator.child_by_field_name("type")
                known, fake = _typed_fake(declared, reader, fakes)
                if known and fake is None:
                    continue
                if not any(s.construction[1] for s in siblings):
                    continue
                if not known and _never_a_fake(declared, node, siblings, readers.repo_dir):
                    continue
            arguments = node.child_by_field_name("arguments")
            if arguments is None:
                arguments = _child(node, "argument_list")
            passed = [a for a in arguments.named_children if a.type != "comment"] if arguments is not None else []
            if fake is None and not passed:
                continue  # `new()`: no argument chooses anything
            if fake is None and not any(_could_build(s, node, passed, path) for s in siblings if s.construction[1]):
                # `new(2026, 1, 1)` or `new(new[] { x })` builds no fake taking
                # one `bool`, and a private nested fake is built only in its
                # outer type.
                continue
            value: str | None = None
            if not passed and fake is not None:
                value = next((s.knob_default for s in fake if s.knob_primary), None) or None
            elif len(passed) == 1 and passed[0].named_children and passed[0].named_children[-1].type == "boolean_literal":
                value = _text(passed[0].named_children[-1])
            found.append(_Setting(node, None, value, fake, None))
        elif node.type == "ERROR":
            text = _text(node)
            if any(re.search(rf"\b{re.escape(w)}\b", text) for w in member_knobs | owners):
                found.append(_Setting(node, None, None, None, None))
    return found


#: Argument shapes that can never be a ``bool``.
_NOT_BOOL = frozenset(
    {
        "object_creation_expression", "implicit_object_creation_expression", "array_creation_expression",
        "implicit_array_creation_expression", "string_literal", "verbatim_string_literal", "raw_string_literal",
        "interpolated_string_expression", "integer_literal", "real_literal", "character_literal", "null_literal",
        "lambda_expression", "anonymous_method_expression", "anonymous_object_creation_expression",
    }
)


def _not_bool(argument: Node) -> bool:
    """Whether ``argument`` is plainly not a ``bool``, by its shape alone."""
    value = argument.named_children[-1] if argument.type == "argument" and argument.named_children else argument
    return value.type in _NOT_BOOL


def _knob_scan(path: str, source: str, fakes: dict[str, tuple[wi.Sibling, ...]], repo_dir: str):
    """``(root, reader, readers, settings)`` of a test project's file, or
    ``None`` when there is no fake set by a knob or the file is no test code."""
    if not fakes or _test_project(path, repo_dir) is not True:
        return None
    root = parse(source)
    reader = xcs._Reader(root, path, repo_dir)
    readers = _Readers(repo_dir)
    readers.add(reader)
    readers.substitute = _substitute_ok(root, reader, readers)
    return root, reader, readers, _knob_settings(root, reader, readers, path, fakes)


def knob_receivers(
    path: str, source: str, fakes: dict[str, tuple[wi.Sibling, ...]], repo_dir: str
) -> frozenset[str]:
    """Receivers in ``path``, a test project's file, typed as a test fake
    there and written through its knob (``stub.Enabled = false``): the
    test-double pass resolves those writes. A name counts only where every
    binding of it in the file is typed as a fake set by a knob (the bar
    :func:`_bound_only_as` sets for siblings), and every write through it of
    any fake's knob -- ``fakes`` holds every wrapper's of the run -- writes
    its own fake's."""
    scan = _knob_scan(path, source, fakes, repo_dir)
    if scan is None:
        return frozenset()
    root, reader, readers, settings = scan
    members = {f for f, group in fakes.items() if any(not s.knob_primary for s in group)}
    names = {s.receiver for s in settings if s.receiver is not None}

    def bound(name: str) -> bool:
        holders = {s.holder for s in settings if s.receiver == name}
        if "." not in name:
            return holders == {None} and _bound_only_as(
                cross_file_keys.walk(root), name, readers, lambda t, at: _type_full(t, reader) in members, reader.path
            )
        # `host.Fake`: every binding of `host` in the file is the one type
        # whose own member `Fake` is typed as a fake.
        if len(holders) != 1 or None in holders:
            return False
        ((host, full),) = holders
        return _bound_only_as(
            cross_file_keys.walk(root), host, readers, lambda t, at: _type_full(t, reader) == full, reader.path
        )

    return frozenset(
        name
        for name in names
        if all(s.fake is not None and s.sets_knob for s in settings if s.receiver == name) and bound(name)
    )


def test_code_facts(
    repo_dir: str, knobs: frozenset[str], owners: frozenset[str]
) -> tuple[bool, frozenset[str]]:
    """``(flippable, test types)`` over EVERY C# file of a test project in
    the checkout, not only those naming a fake: a helper that never spells the
    fake's type (``static void On(dynamic g) => g.Enabled = true``) can still
    set its knob.

    ``flippable``: some such file writes a member named like one of
    ``knobs`` from where no test sees it happen -- outside every test
    method's body, or inside a lambda, an anonymous method or a local
    function. An object initializer builds a new object and is not one;
    neither is a write inside one of the fakes themselves (``owners``, whose
    knob qualification proved is written by nothing else), nor one in the
    instance constructor of a class that holds tests, which runs before
    each. The receiver is not resolved: any member of that name counts.

    ``test types``: the simple name of every class, struct and record those
    files declare. Handing a fake to one being constructed may store it
    where something later sets its knob; handing it to anything else built
    (the code under test) cannot."""
    flippable = False
    types: set[str] = set()
    named = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in sorted(knobs)) + r")\b") if knobs else None
    for listed in source_index.files(repo_dir, (".cs",)):
        path = str(listed.path)
        if _test_project(path, repo_dir) is not True:
            continue
        try:
            source = listed.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            flippable = True
            continue
        root = parse(source)
        data = source.encode("utf-8")
        for node in cross_file_keys.walk(root):
            if node.type in ("class_declaration", "struct_declaration", "record_declaration", "record_struct_declaration"):
                name = node.child_by_field_name("name")
                if name is not None:
                    types.add(_text(name))
        if flippable or named is None or not named.search(source):
            continue
        flippable = any(_unseen_knob_write(node, data, knobs, owners) for node in cross_file_keys.walk(root))
    return flippable, frozenset(types)


def _unseen_knob_write(node: Node, data: bytes, knobs: frozenset[str], owners: frozenset[str]) -> bool:
    """Whether ``node`` writes a member named like a knob from where no test
    sees it; see :func:`test_code_facts`."""
    if node.type == "assignment_expression":
        left = node.child_by_field_name("left")
        targets = [left] if left is not None else []
    elif node.type == "argument" and any(not c.is_named and _text(c) in ("ref", "out") for c in node.children):
        targets = list(node.named_children)
    else:
        return False
    written = any(
        t.type == "member_access_expression"
        and (name := t.child_by_field_name("name")) is not None
        and _text(name).removeprefix("@") in knobs
        for target in targets
        for t in cross_file_keys.walk(target)
    )
    if not written:
        return False
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in ("lambda_expression", "anonymous_method_expression", "local_function_statement"):
            return True
        if ancestor.type in ("class_declaration", "struct_declaration", "record_declaration"):
            name = ancestor.child_by_field_name("name")
            if name is not None and _text(name) in owners:
                return False
        if ancestor.type == "method_declaration":
            return mock_stubs._csharp_test(ancestor, data) is None
        if ancestor.type == "constructor_declaration":
            if "static" in xcs._modifiers(ancestor):
                return True
            holder = ancestor.parent
            while holder is not None and holder.type not in ("class_declaration", "struct_declaration", "record_declaration"):
                holder = holder.parent
            body = holder.child_by_field_name("body") if holder is not None else None
            return not any(
                m.type == "method_declaration" and mock_stubs._csharp_test(m, data) is not None
                for m in (body.named_children if body is not None else ())
            )
        ancestor = ancestor.parent
    return True


def knob_owners(
    path: str, source: str, fakes: dict[str, tuple[wi.Sibling, ...]], repo_dir: str
) -> frozenset[tuple[str, str, bool]]:
    """The fakes (``mock_stubs.WrapperStub.sibling_owners``) whose simple
    name means them at every construction spelled with it in ``path``, a
    test project's file."""
    scan = _knob_scan(path, source, fakes, repo_dir)
    if scan is None:
        return frozenset()
    root, reader, _readers, _settings = scan
    found = set()
    for full, group in fakes.items():
        built = [
            n.child_by_field_name("type")
            for n in cross_file_keys.walk(root)
            if n.type == "object_creation_expression"
            and n.child_by_field_name("type") is not None
            and xcs._simple_name(n.child_by_field_name("type")) == group[0].owner
        ]
        if built and all(_type_full(t, reader) == full for t in built):
            found |= {(s.owner, *s.construction) for s in group}
    return frozenset(found)


def _knob_leftovers(
    path: str, source: str, root: Node, reader: xcs._Reader, readers: _Readers, wrapper: wi.Wrapper, index: Index
) -> list[wi.Occurrence]:
    """Every knob setting the test-double pass left that may still choose
    the removed value: one through a receiver typed as the fake, or of a type
    not known here in a file that spells a fake's name, the wrapper's, its
    class's or its interface's. Only a plain write of
    the kept literal is harmless, the knob's default being the kept value
    (qualification refuses one that is not). A write of the member itself
    (an auto-property fake) is a mention of the wrapper's name, judged with
    the rest."""
    fakes = fakes_of([wrapper])
    if not fakes or index.treatment is None:
        return []
    kept = "true" if index.treatment else "false"
    # Deciding what STAYS: a plain search. A file naming a fake, the wrapper,
    # its class or its interface may hold a fake whatever its receivers say.
    names = {s.owner for group in fakes.values() for s in group} | {wrapper.name, wrapper.owner}
    if wrapper.interface is not None:
        names.add(wrapper.interface.name)
    spelled = any(re.search(rf"(?<![\w$@]){re.escape(n)}(?![\w$])", source) for n in names)
    data = source.encode("utf-8")
    found: list[wi.Occurrence] = []
    test_project: list[bool] = []
    for setting in _knob_settings(root, reader, readers, path, fakes):
        if not setting.sets_knob or setting.knob == wrapper.name or setting.value == kept:
            continue
        if setting.fake is None and not spelled:
            continue
        if not test_project:
            test_project.append(_test_project(path, index.repo_dir) is True)
        in_test = test_project[0] and _enclosing_test_method(setting.node, data)
        found.append(
            wi.Occurrence(
                path, wi.line_of(setting.node), wi.text_of(setting.node), None, wi.SIBLING_RECEIVER, None, in_test
            )
        )
    return found


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


#: APIs that reach a member by its name in a string, given any value: none
#: of them needs the value's type spelled. ``nameof`` is not one: it names a
#: member at compile time.
_BY_NAME = re.compile(
    r"\bGetType\s*\("
    r"|\bGet(?:Runtime)?(?:Propert(?:y|ies)|Methods?|Fields?|Members?)\b"
    r"|\bInvokeMember\b|\bTypeDescriptor\b|\bPropertyDescriptor\b|\bDataBinder\b"
    r"|\bBinding\s*\("
    r"|\bExpression\s*\.\s*(?:Property|PropertyOrField)\b"
)


def mention_counts(index: Index, path: str, kind: str) -> bool:
    """Whether a mention of ``kind`` in ``path`` may be the wrapper's member.

    ``"unparsed"``: a file nothing here parses. Only a .NET guest (Razor,
    XAML, Web Forms, a C# script) binds C# members by name; a file of another
    language cannot call one. Configuration and documentation are not in the
    source listing at all, so they never reach this.

    ``"string"``: a string literal of a C# file. It counts only where a value
    of a family type may be: the project reaches a family type, and the file
    is not family-free or spells a route that reaches a member by name
    without naming its type (``o.GetType()``, ``TypeDescriptor``, a
    ``Binding``, an expression tree's ``Expression.Property``). Anywhere the
    family can be, when any such file passes a route a name that is not a
    literal (:func:`~flag_cleanup.csharp_family.computed_name_route`): the
    name may then be a string of any file. Reflection by assembly scanning,
    and by-name APIs not listed there, are not caught. Unreadable counts."""
    from flag_cleanup import csharp_family

    family = index.family
    if family is None:
        return True
    if kind == "unparsed":
        return path.lower().endswith(_GUESTS)
    source = _read(path)
    if source is None:
        return True
    if not csharp_family.reaches(index.repo_dir, path, family):
        return False
    if csharp_family.computed_name_route(index.repo_dir, family):
        return True
    return _BY_NAME.search(source) is not None or not csharp_family.family_free(source, parse(source), family)


def string_mentions(index: Index, path: str, wrapper: wi.Wrapper) -> list[tuple[int, str]]:
    """The strings of a parsed C# file that may name the member, where
    :func:`mention_counts` counts the file's strings at all.

    A ``nameof(...)`` whose constant is the name is one: it is a string
    literal written another way. In a family-free file, while no route
    anywhere the family can be takes a computed name, only a by-name
    route's own NAME argument counts
    (:func:`~flag_cleanup.csharp_family.route_names`): no other string can
    then flow into a route, and a string a by-name API this does not know
    takes is the same known limit as in a file spelling no route."""
    from flag_cleanup import csharp_family

    if not mention_counts(index, path, "string"):
        return []
    source = _read(path)
    if source is None:
        return []
    root = parse(source)
    prefix = r"\$?" if wrapper.kind == "property" else ""
    # A lookup ignoring case or by a prefix: any spelling counts, and so
    # does a prefix pattern the name matches, in every string of the file.
    loose = csharp_family.loose_names(source)
    # An accessor's name (`get_IsX`, `set_IsX`) is the member's too; a
    # backing field's (`<IsX>k__BackingField`) is spelled as a word already.
    word = re.compile(
        rf"(?<![\w$])(?:{prefix}|get_|set_){re.escape(wrapper.name)}(?![\w$])", re.IGNORECASE if loose else 0
    )

    # A prefix pattern names a member only for a lookup that takes one.
    patterns = loose and csharp_family.pattern_routes(index.repo_dir)

    def prefix_of_name(node: Node) -> bool:
        text = _plain_string(node)
        return patterns and text is not None and text.endswith("*") and wrapper.name.lower().startswith(text[:-1].lower())

    found = [
        node
        for node in cross_file_keys.walk(root)
        if (node.type in _STRINGS and (word.search(_text(node)) or prefix_of_name(node)))
        or (csharp_family.nameof_value(node) or "").lower() == wrapper.name.lower() and (
            loose or csharp_family.nameof_value(node) == wrapper.name
        )
    ]
    family = index.family
    if (
        found
        and family is not None
        and not csharp_family.computed_name_route(index.repo_dir, family)
        and csharp_family.family_free(source, root, family)
    ):
        names = csharp_family.route_names(root, source)
        if names is not None:
            spans = {(n.start_byte, n.end_byte) for n in names}
            # Where names may be compared ignoring case, a string still
            # counts unless it can only leave the file.
            aliased = _aliased(index.repo_dir) if loose else None
            unbound = _qualifiers_only(root) - aliased if aliased is not None else frozenset()
            found = [
                n
                for n in found
                if (n.start_byte, n.end_byte) in spans or (loose and not _passed_out(n, unbound))
            ]
    return [(wi.line_of(n), wi.text_of(n)) for n in found]


_IGNORE_CASE = re.compile(r"IgnoreCase\b")
_LITERALS = frozenset({"string_literal", "verbatim_string_literal", "raw_string_literal"})
#: Where a name written in a file stands for a type, a namespace or another
#: type's member, never for something the file binds.
_QUALIFIER_PARENTS = frozenset(
    {"member_access_expression", "qualified_name", "typeof_expression", "type_argument_list", "generic_name"}
)


def _aliased(repo_dir: str) -> frozenset[str] | None:
    """Every name a ``global using`` or project ``<Using>`` item aliases;
    None when an item cannot be read or a C# 14 ``extension`` block may add
    a static member to any type: then nothing is passed out."""
    from flag_cleanup import csharp_family

    items, readable = xcs.msbuild_usings(repo_dir)
    if not readable or csharp_family.declares_extension_blocks(repo_dir):
        return None
    return frozenset(
        u.alias.removeprefix("@") for u in (*xcs.csharp_index(repo_dir).global_usings, *items) if u.alias
    )


def _qualifiers_only(root: Node) -> frozenset[str]:
    """The names a file spells only as a qualifier, a type or another
    type's member (:data:`_QUALIFIER_PARENTS`, or a node's ``type``): never
    as anything it declares (a type, a method, a local, a parameter, a field,
    an alias, a pattern variable) or reads as a value."""
    seen: set[str] = set()
    bound: set[str] = set()
    for node in cross_file_keys.walk(root):
        if node.type != "identifier":
            continue
        name = _text(node).removeprefix("@")
        seen.add(name)
        parent = node.parent
        if parent is None or not (
            parent.type in _QUALIFIER_PARENTS
            or _field(parent, node) == "type"
            or parent.type == "attribute" and _field(parent, node) == "name"
        ):
            bound.add(name)
    return frozenset(seen - bound)


def _passed_out(node: Node, unbound: frozenset[str]) -> bool:
    """Whether a string's value can only leave its file, so no comparison
    of the file sees it: it is an argument of a call ``Q.M(...)``, whose
    qualifier ``Q`` names nothing the file binds (``unbound``), whose method
    is no by-name route, which spells no ``IgnoreCase`` and whose other
    arguments read only such names, and the call is a statement of its own
    or, by the same rules, an argument of one. The method is then another
    file's, and what it does with the value is judged there, as for a file
    that spells no ``IgnoreCase``."""
    from flag_cleanup import csharp_family

    current = node
    while current.parent is not None and current.parent.type in _LITERALS:
        current = current.parent
    while True:
        argument = current.parent
        arguments = argument.parent if argument is not None and argument.type == "argument" else None
        call = arguments.parent if arguments is not None and arguments.type == "argument_list" else None
        if call is None or call.type != "invocation_expression":
            return False
        function = call.child_by_field_name("function")
        if function is None or function.type != "member_access_expression":
            return False
        member = function.child_by_field_name("name")
        method = xcs._simple_name(member) if member is not None else None
        if (
            method is None
            or method in csharp_family._NAME_ARGUMENT
            or method in _declared_methods(current)
            or not _qualifier(function.child_by_field_name("expression"), unbound)
            or _IGNORE_CASE.search(_text(call))
            or any(
                other.id != argument.id and not _reads_only(other, unbound) for other in arguments.named_children
            )
        ):
            return False
        if call.parent is not None and call.parent.type == "expression_statement":
            return True
        current = call


def _declared_methods(node: Node) -> set[str]:
    """The methods and local functions declared in ``node``'s file: a
    qualifier may still reach one (a base class's property of a type this
    file declares)."""
    root = node
    while root.parent is not None:
        root = root.parent
    return {
        _text(n).removeprefix("@")
        for d in cross_file_keys.walk(root)
        if d.type in ("method_declaration", "local_function_statement")
        and (n := d.child_by_field_name("name")) is not None
    }


def _qualifier(node: Node | None, unbound: frozenset[str]) -> bool:
    """Whether ``node`` is ``A`` or ``A.B.C`` for an ``A`` in ``unbound``."""
    while node is not None and node.type == "member_access_expression":
        name = node.child_by_field_name("name")
        if name is None or name.type != "identifier":
            return False
        node = node.child_by_field_name("expression")
    return node is not None and node.type == "identifier" and _text(node).removeprefix("@") in unbound


def _reads_only(node: Node, unbound: frozenset[str]) -> bool:
    """Whether an argument holds no function and every name it spells is in
    ``unbound`` or is a member of what is (``Kind.X``)."""
    for child in cross_file_keys.walk(node):
        if child.type in ("lambda_expression", "anonymous_method_expression"):
            return False
        if child.type == "identifier" and _text(child).removeprefix("@") not in unbound:
            parent = child.parent
            if parent is None or parent.type != "member_access_expression" or _field(parent, child) != "name":
                return False
    return True


def leftovers(repo_dir: str, wrapper: wi.Wrapper, removable: frozenset[str] = frozenset()) -> list[tuple[str, int, str]]:
    """Every mention still in the repository that may be the member, read
    fresh, by the same verdicts as :func:`occurrences` and
    :func:`mention_counts`: a mention proven to be another type's does not
    keep the wrapper. A Moq stub the test-double pass left, and a call that
    was not folded, are leftovers too. A file that could not be read, or
    whose mention sits where it does not parse, keeps the wrapper."""
    from flag_cleanup import csharp_family

    index = Index.build(repo_dir, wrapper)
    if index.unreadable:
        return [(wrapper.path, wrapper.line, wrapper.label)]
    index.family = csharp_family.family_of(repo_dir, wrapper, index)
    found: list[tuple[str, int, str]] = []
    for path in index.foreign:
        found += [(path, n, t) for n, t in wi._foreign_lines(path, wrapper.name)]
    for path in index.unparsed:
        if mention_counts(index, path, "unparsed"):
            found += [(path, n, t) for n, t in wi._foreign_lines(path, wrapper.name)]
    for path in index.parsed:
        source = _read(path)
        if source is None:
            found.append((path, 1, ""))
            continue
        found += [(path, n, t) for n, t in string_mentions(index, path, wrapper)]
        found += [(o.path, o.line, o.text) for o in occurrences(path, source, wrapper, index)]
    return found


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
        mention_counts=mention_counts,
        string_mentions=string_mentions,
        leftovers=leftovers,
        doc_references=doc_references,
    )
)
