"""Dart's profile for :mod:`flag_cleanup.cross_file_keys`.

A Dart library IS its file, so a constant's qualified name is the file plus,
for a static member, the type holding it, and no other file can declare the
same one. What a reader has to prove is that the name it spells reaches that
file: an ``import`` resolving to it (a relative URI or ``package:<own>/``),
``show``/``hide`` honoured, unprefixed or through ``as p``. Everything else
that could reach it is refused rather than followed: a barrel's ``export``, a
conditional or deferred import, another package of the checkout, a part.

Two facts about Dart, measured with the real analyzer, are why this profile
has no type index where Java's and C#'s have one: static members are NOT
inherited (``Sub.NAME``, and a bare ``NAME`` in a subclass, are errors), and
an imported top-level name WINS over an inherited instance member of the same
name. So no supertype can bring a constant into scope or hide it. A typedef
does carry a class's statics (``typedef K = Keys; K.NAME``), which is why a
qualifier that may be one is refused.

Shadowing is decided by ELIMINATION, never by a list of binding forms: once a
route is proven, every occurrence of each identifier it relies on must sit in
a position known to look nothing up, and anything else refuses. Files are
parsed with the gate's grammar, like :mod:`~flag_cleanup.key_const` and
:mod:`~flag_cleanup.dart_namespace`; the reader rules query the engine's
grammar, and the canonical-spelling requirement keeps the two in step.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import dart_namespace, key_const, source_index

#: The types whose static members the in-file delete rules remove. An
#: ``enum`` or ``extension`` body is not one: a constant offered there would
#: have its readers folded and its declaration left standing.
_OWNER_TYPES = frozenset({"class_definition", "mixin_declaration", "extension_type_declaration"})


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _part_of(program: Node) -> Node | None:
    return next((c for c in program.named_children if c.type == "part_of_directive"), None)


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    """The constants in ``path`` holding ``flag_key`` that another file can
    read, and the enum entries holding it that are proven (see
    :func:`_enum_entries`).

    The set the in-file rules delete, narrowed to what an import can reach:
    a top-level ``const``/``final``, or a ``static`` one directly in a named
    class, mixin or extension type. A private name or owner is never offered,
    nor anything in a part (it belongs to another file's library) or in a
    file whose parse has errors.
    """
    data = source.encode("utf-8")
    program = dart_namespace.parse(data)
    entries, _refused = _enum_entries(path, source, program, flag_key)
    if program.has_error or _part_of(program) is not None:
        return entries
    found: list[xk.KeyDeclaration] = []
    for const in key_const.key_consts(program, data, "dart", flag_key):
        listing = const.declaration.parent
        if listing is None or listing.type != "static_final_declaration_list":
            continue
        if listing.named_child_count != 1:
            continue  # the delete rules take a single declarator only
        offered, owner = _placement(listing)
        if not offered or const.name.startswith("_") or (owner or "").startswith("_"):
            continue
        found.append(xk.KeyDeclaration(path, const.name, owner, path))
    return found + entries


def _placement(listing: Node) -> tuple[bool, str | None]:
    """Whether a declaration list is offered, and the type it is a member of."""
    parent = listing.parent
    if parent is not None and parent.type == "program":
        return True, None
    if parent is None or parent.type != "declaration":
        return False, None
    if not any(c.type == "static" for c in parent.children):
        return False, None
    body = parent.parent
    holder = body.parent if body is not None and body.type == "class_body" else None
    if holder is None or holder.type not in _OWNER_TYPES:
        return False, None
    name = holder.child_by_field_name("name") or next(
        (c for c in holder.named_children if c.type == "identifier"), None
    )
    return (name is not None), (None if name is None else _text(name))


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """The entries holding ``flag_key`` that cannot be proven, and every
    mention of a proven one in its own library: the reader invocation never
    rewrites the declaring file, so such a read is reported, not left."""
    program = dart_namespace.parse(source.encode("utf-8"))
    return _enum_entries(path, source, program, flag_key)[1]


#: Every kind of constructor an enum body can declare. An enum's entries all
#: invoke one generative constructor; any other is a second way to make one.
_CONSTRUCTORS = frozenset({
    "constant_constructor_signature",
    "constructor_signature",
    "factory_constructor_signature",
    "redirecting_factory_constructor_signature",
})


def _enum_entries(
    path: str, source: str, program: Node, flag_key: str
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """The entries of public top-level enums holding ``flag_key`` whose
    accessor provably returns it, and refusals about every entry holding it.

    An entry is proven when the enum has exactly one constructor, a ``const``
    generative one (the analyzer allows no other kind for an entry), that
    stores the parameter the entry passes the key in as a plain string in a
    final field the enum declares with no initializer: an initializing formal
    (``this.key``, positional or named) or a parameter exactly one
    ``field_initializer`` assigns. Enum fields are final and enums cannot be
    extended, so the field holds that argument for good; the enum's own field
    also wins over a mixin's or an extension's member of the same name.
    """
    key = xk.key_token(flag_key)
    unreadable = program.has_error or _part_of(program) is not None
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for enum in xk.walk(program):
        if enum.type != "enum_declaration":
            continue
        name = enum.child_by_field_name("name")
        body = enum.child_by_field_name("body")
        if name is None or body is None:
            continue
        owner = _text(name)
        holding = [c for c in body.named_children if c.type == "enum_constant" and key.search(_text(c))]
        if not holding:
            continue
        top = enum.parent is not None and enum.parent.type == "program"
        slots = None if unreadable or not top or owner.startswith("_") else _key_slots(body)
        accessors = _accessors(body) if slots is not None else {}
        for constant in holding:
            entry_name = _text(constant.child_by_field_name("name"))
            entry = f"{owner}.{entry_name}"
            spellings: set[str] = set()
            arguments = _entry_arguments(constant) if slots is not None else None
            if arguments is not None and not entry_name.startswith("_"):
                for slot, value in arguments:
                    field = slots.get(slot)
                    quoted = key_const._DART_QUOTED.match(_text(value)) if value is not None else None
                    if field is not None and quoted is not None and quoted.group(2) == flag_key:
                        spellings |= accessors.get(field, set())
            if not spellings:
                refused.append(xk.refusal(path, constant, xk.ENUM_UNPROVEN, source, entry))
                continue
            decl = xk.KeyDeclaration(
                path, entry_name, owner, path, kind=xk.ENUM_ENTRY, accessors=frozenset(spellings)
            )
            found.append(decl)
            refused.extend(_in_file_entry_mentions(path, source, program, decl, constant))
    return found, refused


def _entry_arguments(constant: Node) -> list[tuple[int | str, Node | None]] | None:
    """``(position or label, value)`` for each argument an entry passes; None
    for type arguments or anything but a plain argument list."""
    holder = constant
    part = next((c for c in constant.named_children if c.type == "argument_part"), None)
    if part is not None:
        if any(c.type == "type_arguments" for c in part.named_children):
            return None
        holder = part
    arguments = next((c for c in holder.named_children if c.type == "arguments"), None)
    if arguments is None:
        return []
    found: list[tuple[int | str, Node | None]] = []
    position = 0
    for argument in arguments.named_children:
        if argument.type in _COMMENTS:
            continue
        if argument.type == "argument":
            value = next((c for c in argument.named_children if c.type not in _COMMENTS), None)
            found.append((position, value))
            position += 1
        elif argument.type == "named_argument":
            label = next((c for c in argument.named_children if c.type == "label"), None)
            ident = None if label is None else next((c for c in label.named_children if c.type == "identifier"), None)
            if ident is None:
                return None
            found.append((_text(ident), argument.named_children[-1]))
        else:
            return None
    return found


def _fields(body: Node) -> frozenset[str]:
    """The instance fields the enum declares once, ``final`` and with no initializer."""
    names: list[str] = []
    for declaration in body.named_children:
        if declaration.type != "declaration" or any(c.type == "static" for c in declaration.children):
            continue
        if not any(c.type == "final_builtin" for c in declaration.named_children):
            continue
        listing = next((c for c in declaration.named_children if c.type == "initialized_identifier_list"), None)
        if listing is None or listing.named_child_count != 1:
            continue
        ident = listing.named_children[0]
        if any(c.type == "=" for c in ident.children):
            continue
        name = next((c for c in ident.named_children if c.type == "identifier"), None)
        if name is not None:
            names.append(_text(name))
    return frozenset(n for n in names if names.count(n) == 1)


def _key_slots(body: Node) -> dict[int | str, str] | None:
    """Which field each argument slot (a position, or a named parameter's
    label) of the enum's one constructor stores; None when the enum does not
    have exactly one constructor, a ``const`` generative one."""
    signatures = [
        (holder, c)
        for holder in body.named_children
        if holder.type in ("declaration", "method_signature")
        for c in holder.named_children
        if c.type in _CONSTRUCTORS
    ]
    if len(signatures) != 1 or signatures[0][1].type != "constant_constructor_signature":
        return None
    holder, signature = signatures[0]
    fields = _fields(body)
    assigned: dict[str, list[str]] = {}
    for entry in xk.walk(holder):
        if entry.type == "field_initializer":
            names = [c for c in entry.named_children if c.type == "identifier"]
            if names:
                field = _text(names[0])
                value = entry.named_children[-1]
                assigned.setdefault(field, []).append(_text(value) if value.type == "identifier" else "")
    parameters = next((c for c in signature.named_children if c.type == "formal_parameter_list"), None)
    slots: dict[int | str, str] = {}
    position = 0

    def stored(parameter: Node) -> str | None:
        this = next((c for c in parameter.named_children if c.type == "constructor_param"), None)
        if this is not None:
            ident = next((c for c in this.named_children if c.type == "identifier"), None)
            field = None if ident is None else _text(ident)
            return field if field in fields and field not in assigned else None
        ident = [c for c in parameter.named_children if c.type == "identifier"]
        name = _text(ident[-1]) if ident else None
        hits = [f for f, values in assigned.items() if name in values]
        if len(hits) != 1 or assigned[hits[0]] != [name] or hits[0] not in fields:
            return None
        return hits[0]

    for child in (parameters.named_children if parameters is not None else ()):
        if child.type == "formal_parameter":
            field = stored(child)
            if field is not None:
                slots[position] = field
            position += 1
        elif child.type == "optional_formal_parameters":
            named = bool(child.children) and child.children[0].type == "{"
            for parameter in (c for c in child.named_children if c.type == "formal_parameter"):
                field = stored(parameter)
                if named:
                    ident = [c for c in xk.walk(parameter) if c.type == "identifier"]
                    if field is not None and ident:
                        slots[_text(ident[-1])] = field
                else:
                    if field is not None:
                        slots[position] = field
                    position += 1
    return slots


def _returns_field(function_body: Node | None, fields: frozenset[str]) -> str | None:
    """The field a body returns when it is exactly ``=> field`` or ``{ return field; }``."""
    if function_body is None or function_body.type != "function_body":
        return None
    if not function_body.children or function_body.children[0].type not in ("=>", "block"):
        return None  # `async`, `async*`, `sync*`: the body returns a Future or a stream
    expression = [c for c in function_body.named_children if c.type not in _COMMENTS]
    if len(expression) == 1 and expression[0].type == "block":
        statements = [c for c in expression[0].named_children if c.type not in _COMMENTS]
        if len(statements) != 1 or statements[0].type != "return_statement":
            return None
        expression = [c for c in statements[0].named_children if c.type not in _COMMENTS]
    if len(expression) == 1 and expression[0].type == "identifier" and _text(expression[0]) in fields:
        return _text(expression[0])
    return None


def _accessors(body: Node) -> dict[str, set[str]]:
    """For each field, the spellings that read it: the field itself when
    public, each public getter and each public method with no parameters
    whose whole body returns it (``m()`` for a method)."""
    fields = _fields(body)
    found: dict[str, set[str]] = {f: ({f} if not f.startswith("_") else set()) for f in fields}
    children = body.named_children
    for index, member in enumerate(children):
        if member.type != "method_signature" or any(c.type == "static" for c in member.children):
            continue
        following = children[index + 1] if index + 1 < len(children) else None
        field = _returns_field(following, fields)
        if field is None:
            continue
        signature = member.named_children[0] if member.named_children else None
        if signature is None or any(c.type == "type_parameters" for c in signature.named_children):
            continue  # `m<T>()`: a type parameter can shadow the field
        idents = [c for c in signature.named_children if c.type == "identifier"]
        name = _text(idents[-1]) if idents else ""
        if not name or name.startswith("_"):
            continue
        if signature.type == "getter_signature":
            found[field].add(name)
        elif signature.type == "function_signature":
            parameters = next((c for c in signature.named_children if c.type == "formal_parameter_list"), None)
            if parameters is not None and not parameters.named_children:
                found[field].add(f"{name}()")
    return found


def _in_file_entry_mentions(
    path: str, source: str, program: Node, decl: xk.KeyDeclaration, constant: Node
) -> list[xk.Refusal]:
    """Every mention of a proven entry in its own library, other than its
    declaration and the names other declarations introduce."""
    own = constant.child_by_field_name("name")
    found: list[xk.Refusal] = []
    for node in xk.walk(program):
        if node.type not in _OCCURRENCES or _text(node) != decl.name or node.id == own.id:
            continue
        if _is_binding(node):
            continue
        refusal = xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry)
        if refusal not in found:
            found.append(refusal)
    return found


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Empty: a library is its file, so no other file can declare one of its
    names. A file that does not parse still fails closed."""
    if dart_namespace.parse(source.encode("utf-8")).has_error:
        raise xk.DeclarationsUnreadable(path)
    return set()


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """Empty, for the reason :func:`declared_names` gives."""
    if dart_namespace.parse(source.encode("utf-8")).has_error:
        raise xk.DeclarationsUnreadable(path)
    return set()


#: What a lookup answers: a direct import of the declaring file brings the
#: name; nothing that can reach the file does (the name means something
#: else); or something that may reach it cannot be followed.
PROVEN, OTHER, UNKNOWN = "proven", "other", "unknown"

#: Where a URI leads when it is not a file of the checkout.
_OUTSIDE = "outside"  # a library that cannot reach any checkout file
_UNKNOWN = "unknown"  # one that may, and cannot be followed


@dataclass(frozen=True)
class _Import:
    directive: Node  # the `import_or_export` node, removed whole by the import pass
    uris: tuple[str | None, ...]  # the URI, then each configuration URI
    prefix: str | None
    deferred: bool
    show: frozenset[str] | None
    hide: frozenset[str]

    def brings(self, name: str) -> bool:
        return name not in self.hide and (self.show is None or name in self.show)


def _imports(program: Node) -> list[_Import]:
    found: list[_Import] = []
    for child in program.named_children:
        if child.type != "import_or_export":
            continue
        spec = next((n for n in xk.walk(child) if n.type == "import_specification"), None)
        if spec is None:
            continue  # an export
        uris = tuple(
            dart_namespace.uri_text(n)
            for n in sorted(xk.walk(spec), key=lambda n: n.start_byte)
            if n.type == "uri"
        )
        prefix = None
        children = spec.children
        for index, token in enumerate(children[:-1]):
            if token.type == "as" and children[index + 1].type == "identifier":
                prefix = _text(children[index + 1])
        show: set[str] | None = None
        hide: set[str] = set()
        for combinator in (c for c in spec.named_children if c.type == "combinator"):
            listed = {_text(c) for c in combinator.named_children if c.type == "identifier"}
            if combinator.children and combinator.children[0].type == "show":
                show = listed if show is None else show & listed
            else:
                hide |= listed
        found.append(
            _Import(
                child,
                uris,
                prefix,
                any(c.type == "deferred" for c in spec.children),
                None if show is None else frozenset(show),
                frozenset(hide),
            )
        )
    return found


@dataclass(frozen=True)
class _Scope:
    """The imports and top-level names a file's lookups see: its library's."""

    library: Path
    imports: tuple[_Import, ...]
    #: Top-level names of the library file and every part; None if any of
    #: them cannot be read, which makes every unprefixed lookup unknown.
    own: frozenset[str] | None
    #: The library has parts: this file is one, or declares one.
    parts: bool


def _scope(me: Path, program: Node, root: Path) -> _Scope | None:
    """``me``'s scope, or None when it is a part whose library cannot be
    confirmed, or a library whose parts cannot all be located."""
    part_of = _part_of(program)
    library, library_program = me, program
    if part_of is not None:
        uri_node = next((c for c in part_of.named_children if c.type == "uri"), None)
        uri = None if uri_node is None else dart_namespace.uri_text(uri_node)
        target = None if uri is None else dart_namespace.locate(uri, me, root)
        library_program = None if target is None else dart_namespace.read(target)
        if library_program is None:
            return None
        library = target
    parts = _part_paths(library_program, library, root)
    if parts is None or (part_of is not None and me not in parts):
        return None
    return _Scope(
        library,
        tuple(_imports(library_program)),
        _own_names(library_program, parts),
        bool(parts),
    )


def _part_paths(program: Node, library: Path, root: Path) -> list[Path] | None:
    paths: list[Path] = []
    for child in program.named_children:
        if child.type != "part_directive":
            continue
        uri_node = next((c for c in child.named_children if c.type == "uri"), None)
        uri = None if uri_node is None else dart_namespace.uri_text(uri_node)
        target = None if uri is None else dart_namespace.locate(uri, library, root)
        if target is None:
            return None
        paths.append(target)
    return paths


def _own_names(program: Node, parts: list[Path]) -> frozenset[str] | None:
    names = dart_namespace.declared_names(program)
    if names is None:
        return None
    found = set(names.names)
    for part in parts:
        part_program = dart_namespace.read(part)
        part_names = None if part_program is None else dart_namespace.declared_names(part_program)
        if part_names is None:
            return None
        found |= part_names.names
    return frozenset(found)


def _checkout_packages(root: Path) -> frozenset[str] | None:
    """The ``name:`` of every pubspec the checkout holds (tracked or not,
    ignored ones aside); None when one cannot be read, since any
    ``package:`` URI might then be it."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", ":(glob)**/pubspec.yaml"],
            capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace").split("\0")
        homes = {(root / p).parent for p in listed if p}
    except (OSError, subprocess.CalledProcessError):
        homes = {p.parent for p in root.rglob("pubspec.yaml")}
    names: set[str] = set()
    for home in homes:
        name = dart_namespace.package_name(home)
        if name is None:
            return None
        names.add(name)
    return frozenset(names)


class _Targets:
    """Where each import URI of one library leads, memoised for one file."""

    def __init__(self, library: Path, root: Path, packages: frozenset[str] | None) -> None:
        self._library = library
        self._root = root
        self._packages = packages
        home = dart_namespace.package_root(library, root)
        self._own = None if home is None else dart_namespace.package_name(home)
        self._namespaces: dict[Path, dart_namespace.Namespace | None] = {}

    def target(self, uri: str | None) -> Path | str:
        if uri is None:
            return _UNKNOWN
        if uri.startswith("dart:"):
            return _OUTSIDE
        if uri.startswith("package:"):
            name = uri[len("package:"):].partition("/")[0]
            if name != self._own:
                if self._packages is None or name in self._packages:
                    return _UNKNOWN
                return _OUTSIDE
        elif ":" in uri.split("/", 1)[0]:
            return _UNKNOWN
        found = dart_namespace.locate(uri, self._library, self._root)
        return found if found is not None else _UNKNOWN

    def namespace(self, path: Path) -> dart_namespace.Namespace | None:
        if path not in self._namespaces:
            self._namespaces[path] = dart_namespace.library_names(path, self._root)
        return self._namespaces[path]


def _lookup(scope: _Scope, targets: _Targets, name: str, prefix: str | None, declaring: Path) -> str:
    """What ``name`` (or ``prefix.name``) means in ``scope``, as far as the
    declaring file is concerned. A library outside the checkout that also
    brings it cannot win silently: two imports of different declarations of a
    name make its use a compile error."""
    if prefix is None:
        if scope.own is None:
            return UNKNOWN
        if name in scope.own:
            return OTHER
        if any(imp.prefix == name for imp in scope.imports):
            # `import 'x.dart' as Name` hides an imported `Name`: `Name.member`
            # then reads x's library, and a bare `Name` does not compile.
            return UNKNOWN
    direct = False
    for imp in scope.imports:
        if imp.prefix != prefix or not imp.brings(name):
            continue
        leads = [targets.target(uri) for uri in imp.uris]
        if not leads:
            return UNKNOWN
        if imp.deferred or len(leads) > 1:
            if any(lead != _OUTSIDE for lead in leads):
                return UNKNOWN
            continue
        lead = leads[0]
        if lead == _OUTSIDE:
            continue
        if lead == _UNKNOWN:
            return UNKNOWN
        if lead == declaring:
            direct = True
            continue
        names = targets.namespace(lead)
        if names is None or name in names.names:
            return UNKNOWN
    return PROVEN if direct else OTHER


_READ_CALLEES = frozenset({"boolVariation"})
_COMMENTS = frozenset({"comment", "documentation_comment"})
#: Parents under which an identifier names a member after a dot, a cascade
#: target or a label: none of them looks the name up in scope.
_NOT_A_LOOKUP = frozenset({
    "unconditional_assignable_selector",
    "conditional_assignable_selector",
    "cascade_selector",
    "label",
})
_OCCURRENCES = frozenset({"identifier", "identifier_dollar_escaped"})
#: A dotted name outside an expression: a constant pattern's ``qualified``
#: (``case k.NAME:``), an annotation's ``scoped_identifier`` (``@k.NAME``).
_DOTTED = frozenset({"qualified", "scoped_identifier"})


def _typedef_share(text: str) -> tuple[frozenset[str], bool]:
    program = dart_namespace.parse(text.encode("utf-8"))
    names = {
        _text(c)
        for alias in program.named_children
        if alias.type == "type_alias"
        for c in alias.named_children
        if c.type == "type_identifier"
    }
    return frozenset(names), not program.has_error


def _typedef_names(shares, incomplete: bool, _unreadable: bool) -> frozenset[str] | None:
    if incomplete:
        return None
    return frozenset().union(*(share for _path, share in shares))


#: Every name a typedef in the checkout may declare (every type name in its
#: head, the safe direction), or None when a file could not be read: then any
#: qualifier may be one.
_TYPEDEFS = xk.CheckoutIndex("Dart", (".dart",), _typedef_share, _typedef_names)


def _chain(head: Node) -> list[Node]:
    """``head`` and the identifier of every ``.name`` selector after it."""
    segments = [head]
    sibling = head.next_named_sibling
    while sibling is not None:
        if sibling.type in _COMMENTS:
            sibling = sibling.next_named_sibling
            continue
        if sibling.type != "selector":
            break
        member = next((c for c in sibling.named_children if c.type not in _COMMENTS), None)
        if member is None or member.type != "unconditional_assignable_selector":
            break
        name = next((c for c in member.named_children if c.type == "identifier"), None)
        if name is None:
            break
        segments.append(name)
        sibling = sibling.next_named_sibling
    return segments


def _head_of(member: Node) -> list[Node] | None:
    """The chain a member-position identifier ends, when every link before it
    is a plain ``.name``; None after a call, an index, ``this`` or ``?.``."""
    if member.parent is None or member.parent.type != "unconditional_assignable_selector":
        return None
    # The head is the nearest sibling before the selectors, never the
    # container's first child: in `final x = K.NAME` that is `x`.
    head = member.parent.parent
    while head is not None and (head.type == "selector" or head.type in _COMMENTS):
        head = head.prev_named_sibling
    if head is None or head.type != "identifier":
        return None
    chain = _chain(head)
    for index, segment in enumerate(chain):
        if segment.id == member.id:
            return chain[: index + 1]
    return None


def _null_aware_chain(member: Node) -> list[Node] | None:
    """The names from the head to ``member``, across ``.`` and ``?.``
    selectors; None after a call, an index or anything but a name."""
    segments = [member]
    link = member.parent.parent if member.parent is not None else None
    link = link.prev_named_sibling if link is not None else None
    while link is not None:
        if link.type in _COMMENTS:
            link = link.prev_named_sibling
            continue
        if link.type == "identifier":
            return [link] + segments
        if link.type != "selector":
            return None
        inner = next((c for c in link.named_children if c.type not in _COMMENTS), None)
        if inner is None or inner.type not in ("unconditional_assignable_selector", "conditional_assignable_selector"):
            return None
        name = next((c for c in inner.named_children if c.type == "identifier"), None)
        if name is None:
            return None
        segments.insert(0, name)
        link = link.prev_named_sibling
    return None


def _dotted_segments(node: Node) -> list[Node] | None:
    """Every name of a ``qualified``/``scoped_identifier``, in order; None
    if it holds anything else (a comment, say)."""
    segments: list[Node] = []
    for child in node.named_children:
        if child.type == "scoped_identifier":
            inner = _dotted_segments(child)
            if inner is None:
                return None
            segments.extend(inner)
        elif child.type in {"identifier", "type_identifier"}:
            segments.append(child)
        else:
            return None
    return segments


def _dotted_chain(member: Node) -> list[Node] | None:
    """The dotted name ``member`` ends, from its outermost holder; None when
    ``member`` is not its last name or the holder holds anything else."""
    holder = member.parent
    while holder.parent is not None and holder.parent.type in _DOTTED:
        holder = holder.parent
    segments = _dotted_segments(holder)
    if segments is None or segments[-1].id != member.id:
        return None
    return segments


def _is_dotted_head(node: Node) -> bool:
    """Whether ``node`` is the first name of a dotted pattern or annotation
    name: a lookup of that name, like the head of a ``.name`` chain."""
    holder = node.parent
    if holder is None or holder.type not in _DOTTED:
        return False
    while holder.parent is not None and holder.parent.type in _DOTTED:
        holder = holder.parent
    segments = _dotted_segments(holder)
    return segments is not None and len(segments) > 1 and segments[0].id == node.id


def _is_read(chain: list[Node], accessors: frozenset[str]) -> bool:
    """Whether ``chain`` is a whole argument in the key slot of a read the
    seeds match: the first argument (one comment before it at most) of
    ``recv.boolVariation(...)``, a bare ``boolVariation(...)``, or an
    accessor; never a ``?.`` or cascade call."""
    argument = chain[0].parent
    if argument is None or argument.type != "argument":
        return False
    if _chain(chain[0])[-1].id != chain[-1].id:
        return False  # `Owner.NAME.trim()`: the key is not the whole argument
    arguments = argument.parent
    if arguments is None or arguments.type != "arguments":
        return False
    named = arguments.named_children
    first = 1 if named and named[0].type in _COMMENTS else 0
    if len(named) <= first or named[first].id != argument.id:
        return False
    part = arguments.parent
    selector = part.parent if part is not None and part.type == "argument_part" else None
    if selector is None or selector.type != "selector" or selector.parent is None:
        return False
    previous = None
    for child in selector.parent.named_children:
        if child.id == selector.id:
            break
        if child.type not in _COMMENTS:
            previous = child
    if previous is None:
        return False
    callee = None
    if previous.type == "identifier":
        callee = previous
    elif previous.type == "selector":
        member = next((c for c in previous.named_children if c.type not in _COMMENTS), None)
        if member is not None and member.type == "unconditional_assignable_selector":
            callee = next((c for c in member.named_children if c.type == "identifier"), None)
    return callee is not None and (_text(callee) in _READ_CALLEES or _text(callee) in accessors)


def _in_import(node: Node) -> Node | None:
    """The ``import_specification`` or ``library_export`` holding ``node``."""
    current = node.parent
    while current is not None:
        if current.type in {"import_specification", "library_export"}:
            return current
        current = current.parent
    return None


#: Nodes whose name (their ``name`` field, else their first identifier)
#: introduces a binding, beyond the ones the in-file pass already knows.
_BINDERS = frozenset({
    "formal_parameter", "for_loop_parts", "type_parameter", "function_signature",
    "getter_signature", "setter_signature", "initialized_identifier",
    "static_final_declaration", "class_definition", "mixin_declaration",
    "enum_constant", "declared_identifier", "initialized_variable_definition",
    # `this.name` / `super.name`: names a field or a super parameter, never a
    # top-level lookup.
    "constructor_param", "super_formal_parameter",
})


def _is_binding(node: Node) -> bool:
    """Whether ``node`` declares its name rather than looking it up. Only the
    NAME of a declaration binds: an initializer's identifier is a use."""
    if key_const._dart_is_binding(node):
        return True
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "catch_parameters":
        return True
    if parent.type not in _BINDERS:
        return False
    name = parent.child_by_field_name("name") or next(
        (c for c in parent.named_children if c.type == "identifier"), None
    )
    return name is not None and name.id == node.id


class _Judgement:
    """One reader against one set of declarations."""

    def __init__(self, path, data, program, scope, targets, accessors, repo_dir):
        self.path = path
        self.data = data
        self.source = data.decode("utf-8", "replace")
        self.program = program
        self.scope = scope
        self.targets = targets
        self.accessors = accessors
        self.repo_dir = repo_dir
        self.prefixes = {imp.prefix for imp in scope.imports if imp.prefix is not None}
        self.reads: list[xk.KeyReference] = []
        self.refused: list[xk.Refusal] = []
        #: (engine owner text, member) of every read-position mention left
        #: unproven, for the clone check in :meth:`finish`.
        self.unproven: list[tuple[str | None, str, Node]] = []
        #: `show NAME` on an import of the declaring file, kept only if a read
        #: of NAME is proven.
        self.shown: list[tuple[xk.KeyDeclaration, Node]] = []
        #: Each name a proven route relies on, and the entries read through it
        #: (``""`` for a constant), so a refusal of it names what it keeps.
        self.relied: dict[str, set[str]] = {}
        self.reachable: set[xk.KeyDeclaration] = set()
        self._occurrences: dict[str, list[Node]] = {}
        self._memo: dict[tuple, str] = {}

    def occurrences(self, name: str) -> list[Node]:
        if name not in self._occurrences:
            self._occurrences[name] = [
                n for n in xk.walk(self.program) if n.type in _OCCURRENCES and _text(n) == name
            ]
        return self._occurrences[name]

    def lookup(self, decl, name, prefix) -> str:
        key = (decl.path, name, prefix)
        if key not in self._memo:
            self._memo[key] = _lookup(self.scope, self.targets, name, prefix, Path(decl.path))
        return self._memo[key]

    def refuse(self, node, reason, decl=None):
        entry = decl.entry if decl is not None else ""
        self.refused.append(xk.refusal(self.path, node, reason, self.source, entry))

    # -- the mentions of one declaration --------------------------------

    def judge(self, decl: xk.KeyDeclaration) -> None:
        for node in self.occurrences(decl.name):
            parent = node.parent
            if parent is not None and parent.type == "label":
                continue
            if decl.kind == xk.ENUM_ENTRY and parent is not None and parent.type == "dot_shorthand":
                # `.newCheckout` reaches the entry wherever the context type is
                # the enum, which no import tells.
                self.reachable.add(decl)
                self.refuse(node, xk.UNSUPPORTED_FORM, decl)
                continue
            holder = _in_import(node)
            if holder is not None:
                self.combinator(decl, node, holder)
                continue
            if node.type == "identifier_dollar_escaped" or (
                parent is not None and parent.type not in _NOT_A_LOOKUP and _is_binding(node)
            ):
                if decl.owner is None and self.lookup(decl, decl.name, None) != OTHER:
                    self.reachable.add(decl)
                    reason = xk.REBOUND if node.type == "identifier" else xk.NOT_A_READ
                    self.refuse(node, reason, decl)
                continue
            if parent is not None and parent.type in _NOT_A_LOOKUP:
                chain = _head_of(node)
                if chain is not None:
                    self.chain(decl, chain)
                elif parent.type == "conditional_assignable_selector":
                    self.null_aware(decl, node)
                continue
            if parent is not None and parent.type in _DOTTED and not _is_dotted_head(node):
                chain = _dotted_chain(node)
                if chain is None:
                    self.reachable.add(decl)
                    self.refuse(node, xk.UNSUPPORTED_FORM, decl)
                else:
                    self.chain(decl, chain)
                continue
            self.chain(decl, [node])
        self.doc_comments(decl)

    def null_aware(self, decl, node) -> None:
        """``Owner?.NAME`` or ``p.Owner?.NAME``: still a read of the
        declaration, which no rule folds, so a route reaching it refuses."""
        segments = _null_aware_chain(node)
        if segments is None or decl.owner is None or len(segments) not in (2, 3):
            return
        texts = [_text(s) for s in segments]
        prefix = texts[0] if len(segments) == 3 else None
        if prefix is not None and prefix not in self.prefixes:
            return  # `obj.Owner?.NAME`: a member of an instance
        if texts[-2] != decl.owner:
            typedefs = _TYPEDEFS(self.repo_dir)
            if not (len(segments) == 2 and texts[0] in self.prefixes) and (
                typedefs is None or texts[-2] in typedefs
            ):
                self.reachable.add(decl)
                self.refuse(node, xk.UNSUPPORTED_FORM, decl)
            return
        if self.lookup(decl, decl.owner, prefix) != OTHER:
            self.reachable.add(decl)
            self.refuse(node, xk.UNSUPPORTED_FORM, decl)

    def combinator(self, decl, node, holder) -> None:
        if decl.owner is not None or node.parent is None or node.parent.type != "combinator":
            return
        if holder.type == "library_export":
            self.refuse(node, xk.NOT_A_READ, decl)
            return
        directive = holder.parent.parent if holder.parent is not None else None
        imp = next(
            (i for i in self.scope.imports if directive is not None and i.directive.id == directive.id),
            None,
        )
        if imp is None:
            return
        declaring = Path(decl.path)
        leads = [self.targets.target(uri) for uri in imp.uris]
        if leads == [declaring]:
            keyword = node.parent.children[0].type if node.parent.children else ""
            if keyword == "hide":
                self.refuse(node, xk.NOT_A_READ, decl)
            elif keyword == "show":
                self.shown.append((decl, node))
            return
        # Through a barrel, another package or a conditional import: once the
        # declaration goes, a combinator naming it is `undefined_hidden_name`
        # or `undefined_shown_name`, unless nothing it leads to can hold it.
        for lead in leads:
            if lead == _OUTSIDE:
                continue
            names = None if not isinstance(lead, Path) or lead == declaring else self.targets.namespace(lead)
            if names is None or decl.name in names.names:
                self.reachable.add(decl)
                self.refuse(node, xk.NOT_A_READ, decl)
                return

    def chain(self, decl, chain: list[Node]) -> None:
        if decl.kind == xk.ENUM_ENTRY:
            self.entry_chain(decl, chain)
            return
        texts = [_text(s) for s in chain]
        route = None
        if decl.owner is None:
            if len(chain) == 1:
                route = (decl.name, None, None)
            elif len(chain) == 2:
                route = (decl.name, texts[0], texts[0])
        elif texts[-1] == decl.name and len(chain) in (2, 3):
            owner_text = ".".join(texts[:-1])
            prefix = texts[0] if len(chain) == 3 else None
            if len(chain) == 3 and prefix not in self.prefixes:
                return  # `obj.Owner.NAME`: a member of an instance
            if texts[-2] == decl.owner:
                route = (decl.owner, prefix, owner_text)
            elif len(chain) == 2 and texts[0] in self.prefixes:
                return  # `p.NAME`: a top-level of p's library, not this static
            else:
                typedefs = _TYPEDEFS(self.repo_dir)
                if typedefs is None or texts[-2] in typedefs:
                    self.reachable.add(decl)
                    self.refuse(chain[-1], xk.UNSUPPORTED_FORM, decl)
                return
        if route is None:
            return
        name, prefix, owner_text = route
        answer = self.lookup(decl, name, prefix)
        read = _is_read(chain, self.accessors)
        if answer == OTHER:
            if read:
                self.unproven.append((self.engine_owner(chain), decl.name, chain[-1]))
            return
        self.reachable.add(decl)
        if answer == UNKNOWN:
            self.refuse(chain[-1], xk.IMPORT_UNRESOLVED, decl)
            return
        if not read:
            self.refuse(chain[-1], xk.NOT_A_READ, decl)
            return
        argument = chain[0].parent
        if _text(argument) != ".".join(texts):
            self.refuse(chain[-1], xk.UNSUPPORTED_FORM, decl)
            return
        if self.targets.namespace(Path(decl.path)) is None:
            # The import pass could not tell whether the fold strands the
            # import, and an unused import fails analysis.
            self.refuse(chain[-1], xk.UNSUPPORTED_FORM, decl)
            return
        self.reads.append(xk.KeyReference(self.path, owner_text, decl.name))
        if prefix is not None:
            self.relied.setdefault(prefix, set()).add(decl.entry)
        if decl.owner is not None:
            self.relied.setdefault(decl.owner, set()).add(decl.entry)

    def entry_chain(self, decl, chain: list[Node]) -> None:
        """``Owner.X`` or ``p.Owner.X`` naming an enum entry: proven only as
        the whole key argument followed by one of the entry's accessors."""
        texts = [_text(s) for s in chain]
        if texts[-1] != decl.name or len(chain) not in (2, 3):
            return
        prefix = texts[0] if len(chain) == 3 else None
        if prefix is not None and prefix not in self.prefixes:
            return  # `obj.Owner.X`: a member of an instance
        if texts[-2] != decl.owner:
            if len(chain) == 2 and texts[0] in self.prefixes:
                return  # `p.X`: a top-level name of p's library
            typedefs = _TYPEDEFS(self.repo_dir)
            if typedefs is None or texts[-2] in typedefs:
                self.reachable.add(decl)
                self.refuse(chain[-1], xk.UNSUPPORTED_FORM, decl)
            return
        owner_text = ".".join(texts[:-1])
        accessor, tail = self.accessor_after(chain)
        read = tail is not None and _is_read(chain + [tail], self.accessors)
        answer = self.lookup(decl, decl.owner, prefix)
        if answer == OTHER:
            if read:
                self.unproven.append((owner_text, decl.name, chain[-1]))
            return
        self.reachable.add(decl)
        if answer == UNKNOWN:
            self.refuse(chain[-1], xk.IMPORT_UNRESOLVED, decl)
            return
        if accessor is None:
            self.refuse(chain[-1], xk.NOT_A_READ, decl)  # the entry itself
            return
        if accessor not in decl.accessors:
            self.refuse(chain[-1], xk.UNSUPPORTED_FORM, decl)  # `.name`, `.index`, another member
            return
        if not read:
            self.refuse(chain[-1], xk.NOT_A_READ, decl)
            return
        if _text(chain[0].parent) != ".".join(texts) + "." + accessor:
            # A spelling the engine clone would match but the proof did not see.
            self.refuse(chain[-1], xk.UNSUPPORTED_FORM, decl)
            return
        if self.targets.namespace(Path(decl.path)) is None:
            self.refuse(chain[-1], xk.UNSUPPORTED_FORM, decl)
            return
        self.reads.append(xk.KeyReference(self.path, owner_text, decl.name, accessor, decl))
        if prefix is not None:
            self.relied.setdefault(prefix, set()).add(decl.entry)
        self.relied.setdefault(decl.owner, set()).add(decl.entry)

    @staticmethod
    def accessor_after(chain: list[Node]) -> tuple[str | None, Node | None]:
        """The member read off the entry (``key``, or ``m()`` when called) and
        its name node; ``(None, None)`` when nothing follows the entry."""
        full = _chain(chain[0])
        index = next((i for i, s in enumerate(full) if s.id == chain[-1].id), None)
        if index is None or index + 1 >= len(full):
            return None, None
        name = full[index + 1]
        selector = name.parent.parent if name.parent is not None else None
        following = selector.next_named_sibling if selector is not None else None
        while following is not None and following.type in _COMMENTS:
            following = following.next_named_sibling
        called = (
            following is not None
            and following.type == "selector"
            and any(c.type == "argument_part" for c in following.named_children)
        )
        return _text(name) + ("()" if called else ""), name

    def engine_owner(self, chain: list[Node]) -> str | None:
        if len(chain) < 2:
            return None
        argument = chain[0].parent
        return self.data[argument.start_byte:chain[-2].end_byte].decode("utf-8", "replace")

    def doc_comments(self, decl) -> None:
        if decl not in self.reachable and not self.reads:
            return
        pattern = re.compile(r"\[(?:[A-Za-z_$][\w$]*\.){0,2}%s\]" % re.escape(decl.name))
        for node in xk.walk(self.program):
            if node.type in _COMMENTS and _text(node).startswith(("///", "/**")):
                if pattern.search(_text(node)):
                    self.refuse(node, xk.NOT_A_READ, decl)

    # -- the identifiers a proven route relies on ------------------------

    def eliminate(self) -> None:
        for name, entries in sorted(self.relied.items()):
            for node in self.occurrences(name):
                reason = self.lookup_use(node, name)
                if reason is not None:
                    for entry in sorted(entries):
                        self.refused.append(xk.refusal(self.path, node, reason, self.source, entry))

    def lookup_use(self, node, name) -> str | None:
        """None when an occurrence of a relied-on name looks nothing else up."""
        parent = node.parent
        if parent is None or parent.type in _NOT_A_LOOKUP:
            return None
        if _in_import(node) is not None:
            if parent.type == "combinator" or (parent.type == "import_specification" and name in self.prefixes):
                return None
            return xk.REBOUND
        if node.type == "identifier_dollar_escaped":
            return xk.NOT_A_READ
        if _is_binding(node):
            return xk.REBOUND
        if len(_chain(node)) < 2 and not _is_dotted_head(node):
            return xk.NOT_A_READ
        return None

    def finish(self) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
        for decl, node in self.shown:
            if not any(r.member == decl.name for r in self.reads):
                self.refuse(node, xk.NOT_A_READ)
        self.eliminate()
        # Defence in depth: a reader's clone rewrites every spelling of its
        # owner and member in the file, so an unproven read-position mention
        # with the same spelling would be folded too. No test reaches it: a
        # lookup answers once per file, and a second declaration of the name
        # that an import brings makes the lookup UNKNOWN, not OTHER.
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
    told apart from one the rules missed.
    """
    data = source.encode("utf-8")
    program = dart_namespace.parse(data)
    if program.has_error:
        return [], _whole_file(path, source, declarations)
    root = Path(source_index.checkout_root(repo_dir)).resolve()
    me = Path(path).resolve()
    scope = _scope(me, program, root)
    if scope is None:
        return [], _whole_file(path, source, declarations)
    own = [d for d in declarations if Path(d.path).resolve() == scope.library]
    if own and xk.mention_pattern({d.name for d in own}).search(source):
        # A part of the declaring library sees the constant with no import
        # at all, and nothing here folds it: deleting would break the part.
        return [], _whole_file(path, source, declarations)
    targets = _Targets(scope.library, root, _checkout_packages(root))
    judgement = _Judgement(path, data, program, scope, targets, accessors, repo_dir)
    for decl in declarations:
        judgement.judge(decl)
    reads, refused = judgement.finish()
    if scope.parts and (reads or refused):
        # A part's reads would need its library's import edited, and a
        # part can declare what shadows a name: refused whole.
        return [], _whole_file(path, source, declarations)
    return reads, refused


def _whole_file(path: str, source: str, declarations) -> list[xk.Refusal]:
    """A file refused whole, at the first line spelling anything it might
    read: once for the constants, and once per entry spelled, so an entry a
    refused file may read is reported left rather than kept."""
    words = {d.name for d in declarations} | {d.owner for d in declarations if d.owner}
    token = xk.mention_pattern(words)
    lines = source.splitlines()
    number = next((i for i, line in enumerate(lines) if token.search(line)), 0)
    text = lines[number].strip() if lines else ""
    entries = [""] if any(d.kind != xk.ENUM_ENTRY for d in declarations) else []
    entries += sorted({
        d.entry for d in declarations
        if d.kind == xk.ENUM_ENTRY and xk.mention_pattern({d.name, d.owner}).search(source)
    })
    return [xk.Refusal(path, number + 1, xk.UNSUPPORTED_FORM, text, entry) for entry in entries or [""]]


def enum_dynamic_uses(path, source, entries, repo_dir) -> list[xk.Refusal]:
    """The ``enum-dynamic`` notes for ``path``: each ``Owner.values`` (or
    ``p.Owner.values``), and a bare ``values`` inside the enum itself, through
    which ``byName``, ``firstWhere`` and iteration reach every entry. Matched
    on the owner's name without proving which enum it means: a note only asks
    a reviewer to look."""
    program = dart_namespace.parse(source.encode("utf-8"))
    if program.has_error:
        return []
    by_owner: dict[str, list[xk.KeyDeclaration]] = {}
    for decl in entries:
        if decl.owner is not None:
            by_owner.setdefault(decl.owner, []).append(decl)
    notes: list[xk.Refusal] = []

    def note(node, owner):
        for decl in by_owner.get(owner, ()):
            found = xk.refusal(path, node, xk.ENUM_DYNAMIC, source, decl.entry)
            if found not in notes:
                notes.append(found)

    for node in xk.walk(program):
        if node.type != "identifier" or _text(node) != "values":
            continue
        parent = node.parent
        if parent is not None and parent.type in _NOT_A_LOOKUP:
            chain = _head_of(node)
            if chain is not None and len(chain) >= 2:
                note(node, _text(chain[-2]))
            continue
        enum = node.parent
        while enum is not None and enum.type != "enum_declaration":
            enum = enum.parent
        if enum is not None and not _is_binding(node):
            name = enum.child_by_field_name("name")
            if name is not None:
                note(node, _text(name))
    return notes


#: A ``dart:mirrors`` URI as an import or export spells it.
_MIRRORS = re.compile(r"""(['"])dart:mirrors\1""")


def constant_escapes(path, source, constants, repo_dir) -> list[tuple[str, xk.Refusal]]:
    """``(declaring file, refusal)`` for every declaring file of ``constants``
    when ``path`` imports or exports ``dart:mirrors``.

    A mirror reaches a constant without spelling it, and not only through its
    owner: ``reflectClass(FeatureKeys).declarations``, a library's top-level
    constants through any class of it (``reflectClass(C).owner``), and every
    loaded library through ``currentMirrorSystem().libraries``. Which
    libraries a mirrors program loads is not decided here, so its file keeps
    every key constant of the run (``unsupported-form``). Matched on the text,
    so it holds in a file that does not parse, and a mention in a comment
    only keeps a declaration. Mirrors exist on the Dart VM alone, so a
    Flutter or AOT-compiled package never has one."""
    hit = _MIRRORS.search(source)
    if hit is None:
        return []
    line = source.count("\n", 0, hit.start())
    text = source.splitlines()[line].strip()
    refusal = xk.Refusal(path, line + 1, xk.UNSUPPORTED_FORM, text)
    return [(holder, refusal) for holder in sorted({d.path for d in constants})]


def constant_module_pattern(constants, repo_dir) -> re.Pattern[str]:
    """What a file's text holds when :func:`constant_escapes` can find
    something in it: the ``dart:mirrors`` URI."""
    return _MIRRORS


def rewrite_stranded_imports(path, content, declarations, references, repo_dir) -> str:
    """``content`` with each import a proven read went through trimmed or cut.

    Run after the folds and BEFORE the stranded-binding pass, which reads the
    declaring library from disk after its declaration was deleted and so
    cannot see that the import was ever used. A ``show`` name the library no
    longer declares is dropped (``undefined_shown_name`` fails analysis), and
    the directive goes when nothing it still brings is spelled: names, the
    prefix, or an extension member (which applies wherever it is called).
    """
    data = content.encode("utf-8")
    program = dart_namespace.parse(data)
    if program.has_error:
        return content
    root = Path(source_index.checkout_root(repo_dir)).resolve()
    me = Path(path).resolve()
    declared: dict[Path, set[str]] = {}
    for decl in declarations:
        declared.setdefault(Path(decl.path).resolve(), set()).add(decl.name)
    owners = {(d.owner, d.name) for d in declarations if d.owner is not None}
    routes: set[str | None] = set()
    for ref in references:
        parts = (ref.owner or "").split(".")
        if ref.owner is None or (len(parts) == 1 and (ref.owner, ref.member) in owners):
            routes.add(None)
        else:
            routes.add(parts[0])
    targets = _Targets(me, root, _checkout_packages(root))
    names_spelled = [
        n
        for n in xk.walk(program)
        if n.type in {"identifier", "type_identifier", "identifier_dollar_escaped"}
        and _in_import(n) is None
    ]
    # Every spelling counts for an extension member (it applies wherever it
    # is called); a top-level name only where it is looked up, not after a
    # dot, as a label or as the name a declaration introduces.
    any_spelled = {_text(n) for n in names_spelled}
    spelled = {
        _text(n)
        for n in names_spelled
        if (n.parent is None or n.parent.type not in _NOT_A_LOOKUP) and not _is_binding(n)
    }
    edits: list[tuple[int, int, str]] = []
    for imp in _imports(program):
        if imp.prefix not in routes or imp.deferred or len(imp.uris) != 1:
            continue
        lead = targets.target(imp.uris[0])
        if not isinstance(lead, Path) or lead not in declared:
            continue
        names = dart_namespace.library_names(lead, root)
        if names is None:
            continue
        gone = {n for n in (imp.show or ()) if n in declared[lead] and n not in names.names}
        shown = None if imp.show is None else imp.show - gone
        brought = {n for n in names.names if n not in imp.hide and (shown is None or n in shown)}
        if imp.prefix is not None:
            used = imp.prefix in spelled or bool(any_spelled & names.members)
        else:
            used = bool(spelled & brought) or bool(any_spelled & names.members)
        if (shown is not None and not shown) or not used:
            edits.append(_line_span(data, imp.directive) + ("",))
            continue
        for combinator in xk.walk(imp.directive):
            if combinator.type != "combinator" or not combinator.children:
                continue
            if combinator.children[0].type != "show":
                continue
            kept = [_text(c) for c in combinator.named_children if c.type == "identifier" and _text(c) not in gone]
            if len(kept) != sum(1 for c in combinator.named_children if c.type == "identifier"):
                edits.append((combinator.start_byte, combinator.end_byte, "show " + ", ".join(kept)))
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text.encode("utf-8") + data[end:]
    return data.decode("utf-8")


def _line_span(data: bytes, node: Node) -> tuple[int, int]:
    """``node``'s bytes, widened to its whole line when nothing else shares it."""
    start, end = node.start_byte, node.end_byte
    line_start = data.rfind(b"\n", 0, start) + 1
    line_end = data.find(b"\n", end)
    line_end = len(data) if line_end == -1 else line_end + 1
    if not data[line_start:start].strip() and not data[end:line_end].strip():
        return line_start, line_end
    return start, end


xk.register(
    "dart",
    xk.Profile(
        language_family=frozenset({"dart"}),
        declarations=declarations,
        references=references,
        entry_findings=entry_findings,
        enum_dynamic_uses=enum_dynamic_uses,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        # `dart:mirrors` reaches a constant without spelling it.
        constant_escapes=constant_escapes,
        constant_module_pattern=constant_module_pattern,
    ),
)
