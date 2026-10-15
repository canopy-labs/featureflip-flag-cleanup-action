"""Go profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

A wrapper is a top-level function, or a method on a named non-generic type,
with one unnamed ``bool`` result and no type parameters, whose whole body is
``{ return <read> }``. The read is one ``rules/go.toml`` folds in this run:
``BoolVariation`` (or a named accessor) with the key first, as a literal or
a same-file constant where Gate 2 let the const rules run, or OpenFeature's
``Boolean(ctx, key, ...)`` with a literal key.

Go has no inheritance, so the static type of a receiver decides which method
a call reaches, and nothing can override it. What Go has instead:

* **Structural interfaces.** A type satisfies an interface by having the
  method, with nothing declaring so. An interface method of the wrapper's
  name anywhere in the repository refuses it (``interface-structural``). An
  unexported method can only satisfy an interface of its own package, so
  that scan is complete for it. An exported one may satisfy an interface
  outside the repository, so its callers are inlined and the method itself is
  never deleted (:func:`keep`).
* **Packages, not classes.** A function is called bare in its own package
  (same directory, same ``package`` clause) or through an import of the
  package's path, which the nearest ``go.mod`` decides
  (:mod:`flag_cleanup.go_module`). Scopes and receiver types are in
  :mod:`flag_cleanup.go_scope`.

A template cannot reach anything this deletes. Go templates call exported
methods by reflection, and those are never deleted here. They reach
functions only through a ``FuncMap`` built in Go code, which mentions the
function as a value and so keeps it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, go_module, go_scope, key_const, syntax, unused
from flag_cleanup import wrapper_inline as wi

logger = logging.getLogger(__name__)

EXTENSIONS = (".go",)
#: A mention inside a string is reflection or a testify ``On("Name")``,
#: never a call.
_STRINGS = frozenset({"interpreted_string_literal", "raw_string_literal"})
#: ``F`` in a bare call is an ``identifier``; ``F`` in ``pkg.F`` and ``M`` in
#: ``s.M`` are ``field_identifier``s.
MENTION_TYPES = frozenset({"identifier", "field_identifier"})
_READ_CALLEES = frozenset({"BoolVariation"})
#: OpenFeature's read takes the context first and the key second.
_OPENFEATURE = "Boolean"
#: Comments through which something other than Go code calls a function.
_DIRECTIVES = re.compile(r"//(export|go:linkname)\s+(\S+)")
_DECLARATIONS = ("function_declaration", "method_declaration")


def parse(source: str) -> Node:
    return syntax._parse(source, "go")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _plain_string(node: Node) -> str | None:
    if node.type != "interpreted_string_literal":
        return None
    parts = node.named_children
    if len(parts) != 1 or parts[0].type != "interpreted_string_literal_content":
        return None
    return _text(parts[0])


def _callee_name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "selector_expression":
        function = function.child_by_field_name("field")
    if function is None or function.type not in ("identifier", "field_identifier"):
        return None
    return _text(function)


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return [] if arguments is None else [c for c in arguments.named_children if c.type != "comment"]


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
    """Whether a ``call_expression`` is a read of ``flag_key`` this run folds.

    ``external`` is accepted for the profile's signature and ignored: Go has
    no cross-file key constants. ``loose`` accepts any identifier as the key,
    a superset for widening the undo snapshot."""
    root = parse(source)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "go", flag_key)}
        if const_ok
        else set()
    )
    names = _READ_CALLEES | set(accessors)

    def is_read(node: Node) -> bool:
        if node.type != "call_expression" or node.child_by_field_name("type_arguments") is not None:
            return False
        name = _callee_name(node)
        args = _arguments(node)
        if name in names and args:
            first = args[0]
            if _plain_string(first) == flag_key:
                return True
            return first.type == "identifier" and (loose or _text(first) in consts)
        if name == _OPENFEATURE and len(args) >= 2:
            return _plain_string(args[1]) == flag_key
        return False

    return is_read


def _statements(block: Node) -> list[Node]:
    found: list[Node] = []
    for child in block.named_children:
        if child.type == "statement_list":
            found += [c for c in child.named_children if c.type != "comment"]
        elif child.type != "comment":
            found.append(child)
    return found


def _single_read(body: Node | None, is_read: Callable[[Node], bool]) -> bool:
    """Whether ``body`` is exactly ``{ return <read> }``."""
    if body is None or body.type != "block":
        return False
    statements = _statements(body)
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    values = [c for c in statements[0].named_children if c.type != "comment"]
    if len(values) != 1 or values[0].type != "expression_list":
        return False
    returned = [c for c in values[0].named_children if c.type != "comment"]
    return len(returned) == 1 and is_read(returned[0])


def _returns_bool(declaration: Node) -> bool:
    result = declaration.child_by_field_name("result")
    return result is not None and result.type == "type_identifier" and _text(result) == "bool"


def receiver_type(method: Node) -> str | None:
    """``T`` for a method on ``T`` or ``*T``; ``None`` for a generic one."""
    receiver = method.child_by_field_name("receiver")
    params = [] if receiver is None else [c for c in receiver.named_children if c.type == "parameter_declaration"]
    if len(params) != 1:
        return None
    written = params[0].child_by_field_name("type")
    if written is not None and written.type == "pointer_type":
        written = next(iter(written.named_children), None)
    return _text(written) if written is not None and written.type == "type_identifier" else None


def _directives(declaration: Node, data: bytes, name: str) -> tuple[str, ...]:
    """``export`` / ``go:linkname`` for each comment touching the top of
    ``declaration`` that names it: cgo or the linker calls it, not Go code."""
    found: list[str] = []
    start = declaration.start_byte
    previous = declaration.prev_sibling
    while (
        previous is not None
        and previous.type == "comment"
        and data[previous.end_byte : start].count(b"\n") == 1
        and not data[previous.end_byte : start].strip()
    ):
        match = _DIRECTIVES.match(_text(previous))
        if match and match.group(2) == name:
            found.append(match.group(1))
        start = previous.start_byte
        previous = previous.prev_sibling
    return tuple(reversed(found))


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    clause = go_module.package_clause(root)
    if clause is None:
        return []
    data = source.encode("utf-8")
    found: list[wi.Wrapper] = []
    for declaration in root.named_children:
        if declaration.type not in _DECLARATIONS or declaration.child_by_field_name("type_parameters") is not None:
            continue
        name_node = declaration.child_by_field_name("name")
        if name_node is None or not _returns_bool(declaration):
            continue
        if not _single_read(declaration.child_by_field_name("body"), is_read):
            continue
        name = _text(name_node)
        if declaration.type == "function_declaration":
            kind, owner = "function", clause
        else:
            owner = receiver_type(declaration)
            if owner is None:
                continue
            kind = "method"
        found.append(
            wi.Wrapper(
                path=path,
                language="go",
                name=name,
                owner=owner,
                package=clause,
                line=wi.line_of(declaration),
                start=declaration.start_byte,
                end=declaration.end_byte,
                annotations=_directives(declaration, data, name),
                kind=kind,
            )
        )
    return found


# --- The repository index -----------------------------------------------------


@dataclass
class Index:
    repo_dir: str = ""
    #: ``(path, start byte)`` of every package-level declaration of the
    #: wrapper's name: a function, a method, a struct field, or a package-level
    #: ``var``, ``const`` or ``type``. Locals are the call-site scope walk's.
    declarations: list[tuple[str, int]] = field(default_factory=list)
    #: Every interface method of the wrapper's name, in any interface type.
    interface_methods: list[tuple[str, int]] = field(default_factory=list)
    #: A file mentioning the name could not be read, or holds it inside a
    #: region it could not parse: nothing about uniqueness can be trusted.
    unreadable: bool = False
    #: Every Go file mentioning the name, resolved: where call sites are.
    parsed: list[str] = field(default_factory=list)
    #: Go has no guest language this tool rewrites around it.
    foreign: list[str] = field(default_factory=list)
    #: Source files of any other kind that spell the name.
    unparsed: list[str] = field(default_factory=list)
    #: Unused: Go has no second spelling of a name.
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)
    #: Parsed files and packages, shared by every proof of this wrapper.
    packages: go_scope.Packages | None = None

    @classmethod
    def build(cls, repo_dir: str, wrapper: wi.Wrapper) -> Index:
        from flag_cleanup.piranha_runner import checkout_root

        index = cls(repo_dir=repo_dir, packages=go_scope.Packages(Path(checkout_root(repo_dir))))
        word = _word(wrapper.name)
        for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=()):
            text = _read(path)
            if text is None:
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            if path.endswith(EXTENSIONS):
                index.parsed.append(path)
                index._add(path, text, wrapper.name)
            elif word.search(text):
                index.unparsed.append(path)
        return index

    def _add(self, path: str, text: str, name: str) -> None:
        root = parse(text)
        word = _word(name)
        if root.has_error and any(
            n.type == "ERROR" and word.search(_text(n)) for n in cross_file_keys.walk(root)
        ):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        for node in cross_file_keys.walk(root):
            kind = _declares(node, name)
            if kind == "interface":
                self.interface_methods.append((path, node.start_byte))
            elif kind is not None:
                self.declarations.append((path, node.start_byte))


_PACKAGE_LEVEL = ("var_declaration", "const_declaration", "type_declaration")


def _package_level(spec: Node) -> bool:
    parent = spec.parent
    while parent is not None and parent.type.endswith("_spec_list"):
        parent = parent.parent
    return (
        parent is not None
        and parent.type in _PACKAGE_LEVEL
        and parent.parent is not None
        and parent.parent.type == "source_file"
    )


def _declares(node: Node, name: str) -> str | None:
    """``interface`` for an interface method of ``name``, ``member`` for any
    other package-level declaration of it, else ``None``."""
    if node.type in _DECLARATIONS or node.type in ("method_elem", "method_spec"):
        found = node.child_by_field_name("name")
        if found is None or _text(found) != name:
            return None
        return "member" if node.type in _DECLARATIONS else "interface"
    if node.type == "field_declaration" or (
        node.type in ("var_spec", "const_spec", "type_spec", "type_alias") and _package_level(node)
    ):
        return "member" if any(_text(n) == name for n in node.children_by_field_name("name")) else None
    return None


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | None]:
    """The wrapper, or why it does not qualify.

    An interface method of the name anywhere refuses first: Go interfaces are
    structural, so neither their implementations nor the calls made through
    them can be counted. Then the name must be declared exactly once."""
    if index.interface_methods:
        return None, wi.INTERFACE_STRUCTURAL
    if index.unreadable or len(index.declarations) != 1:
        return None, wi.NAME_NOT_UNIQUE
    return wrapper, None


def keep(wrapper: wi.Wrapper) -> str | None:
    """Why a wrapper whose callers all folded is still not deleted.

    An exported method may satisfy an interface declared outside the
    repository (a value of its type handed to a library). Nothing in the
    repository spells the method at that conversion, so deleting it would
    break the build with no mention to find. An exported function is kept
    when an example test is named after it."""
    if wrapper.kind == "method" and wrapper.name[:1].isupper():
        return wi.INTERFACE_STRUCTURAL
    if wrapper.kind == "function" and wrapper.name[:1].isupper() and _has_example(wrapper):
        return wi.UNPARSED_MENTION
    return None


def _has_example(wrapper: wi.Wrapper) -> bool:
    """A test file of the wrapper's package declares ``Example<Name>``.

    ``go vet``, which ``go test`` runs, fails an example named after an
    identifier the package no longer declares. The name is spelled inside
    another identifier, so no mention scan finds it, and an example belongs
    to the package of its own directory, so that is the only place to look."""
    example = re.compile(rf"^func\s+Example{re.escape(wrapper.name)}(?:_\w+)?\s*\(", re.MULTILINE)
    for path in sorted(Path(wrapper.path).parent.glob("*_test.go")):
        text = _read(str(path))
        if text is None or example.search(text):
            return True
    return False


# --- Call sites -----------------------------------------------------------------


def names_only(node: Node, kind: str = "method") -> bool:
    """Whether an identifier only names something: a function's or method's
    own declared name. Uniqueness leaves the wrapper's as the only one."""
    parent = node.parent
    return parent is not None and parent.type in _DECLARATIONS and parent.child_by_field_name("name") == node


def members(root: Node, name: str) -> list[Node]:
    """Every top-level function or method of ``name``."""
    return [
        n
        for n in root.named_children
        if n.type in _DECLARATIONS and (found := n.child_by_field_name("name")) is not None and _text(found) == name
    ]


def _plain_chain(node: Node) -> bool:
    """An identifier, or ``.field`` selections on one: nothing to evaluate."""
    if node.type == "identifier":
        return True
    if node.type != "selector_expression":
        return False
    operand = node.child_by_field_name("operand")
    field_node = node.child_by_field_name("field")
    return operand is not None and field_node is not None and field_node.type == "field_identifier" and _plain_chain(operand)


def _argument_has_call(call: Node) -> bool:
    """A call, a receive or a dereference anywhere in the arguments: each does
    work (or can panic) that inlining the call would drop."""
    arguments = call.child_by_field_name("arguments")
    for node in [] if arguments is None else cross_file_keys.walk(arguments):
        if node.type == "call_expression":
            return True
        if node.type == "unary_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and _text(operator) in ("<-", "*"):
                return True
    return False


def _removable(imported: go_scope.Import) -> bool:
    """Whether the stranded-import pass can delete this import once its last
    use folds. It guesses an unaliased import's name from the path, and an
    unused import does not build, so a guess that misses must not be relied on."""
    return imported.alias is not None or unused.go_import_name(imported.path) == imported.clause


def _same_package(file: go_scope.File, home: go_scope.File) -> bool:
    return file.directory == home.directory and file.clause == home.clause


def _qualifier_reason(qualifier: Node, file: go_scope.File, home: go_scope.File, packages: go_scope.Packages) -> str | None:
    """Why ``pkg`` in ``pkg.F`` is not this file's removable import of the
    wrapper's package; ``None`` when it is."""
    name = _text(qualifier)
    if go_scope.local_binding(qualifier, name) is not None:
        return wi.RECEIVER_UNPROVEN
    imported = packages.imported(file, name)
    if imported is None or imported.directory != home.directory or imported.clause != home.clause:
        return wi.IMPORT_UNRESOLVED
    return None if _removable(imported) else wi.IMPORT_UNRESOLVED


def _function_reason(node, receiver, wrapper, file, home, packages) -> str | None:
    if receiver is None:
        if node.type != "identifier":
            return wi.RECEIVER_UNPROVEN
        if not _same_package(file, home):
            # A dot import, or a call that does not compile.
            return wi.IMPORT_UNRESOLVED
        return None if go_scope.local_binding(node, wrapper.name) is None else wi.RECEIVER_UNPROVEN
    if receiver.type != "identifier":
        return wi.RECEIVER_UNPROVEN
    return _qualifier_reason(receiver, file, home, packages)


def _method_reason(receiver, wrapper, file, home, packages) -> str | None:
    if receiver is None or not _plain_chain(receiver):
        return wi.RECEIVER_UNPROVEN
    head = receiver
    while head.type == "selector_expression" and head.child_by_field_name("operand").type == "selector_expression":
        head = head.child_by_field_name("operand")
    if head.type == "selector_expression":
        # `pkg.V.M()`: folding drops this use of `pkg`.
        operand = head.child_by_field_name("operand")
        if go_scope.local_binding(operand, _text(operand)) is None:
            imported = packages.imported(file, _text(operand))
            if imported is not None and not _removable(imported):
                return wi.IMPORT_UNRESOLVED
    target = go_scope.TypeRef(home.directory, home.clause, wrapper.owner)
    return None if packages.static_type(receiver, file) == target else wi.RECEIVER_UNPROVEN


#: ``Example<Name>``, the form ``go vet`` resolves ``Name`` for. ``Example``
#: and ``Example_suffix`` name the package, which always resolves.
_NAMED_EXAMPLE = re.compile(r"Example[^_]")


def _example_reason(file: go_scope.File, packages: go_scope.Packages) -> str | None:
    """Why no call in an external test package with a named example folds.

    ``go vet``, which ``go test`` runs, resolves ``ExampleName`` in the test
    package and, for ``pkg_test``, in ``pkg`` as long as some file of the
    test package still imports it. A fold there can take that last import
    with it (the call goes, then the local holding the value, then the
    import), and the example then names an unknown identifier. The chain is
    long enough that the whole test package is left alone; a test file in the
    package's own clause has no import to lose."""
    clause = file.clause
    if clause is None or not clause.endswith("_test") or not file.path.name.endswith("_test.go"):
        return None
    for test_file in packages.package(file.directory, clause):
        for declaration in test_file.root.named_children:
            name = declaration.child_by_field_name("name") if declaration.type == "function_declaration" else None
            if name is not None and _NAMED_EXAMPLE.match(_text(name)):
                return wi.UNPARSED_MENTION
    return None


def _literal_key(node: Node) -> bool:
    """``c`` in ``Flags{c: v}``: a field name, not a read of anything."""
    element = node.parent
    keyed = element.parent if element is not None else None
    return (
        element is not None
        and element.type == "literal_element"
        and keyed is not None
        and keyed.type == "keyed_element"
        and keyed.child_by_field_name("key") == element
    )


def _struct_field_key(node: Node, file: go_scope.File, packages: go_scope.Packages) -> bool:
    """Whether ``node`` is a key of a literal provably of a struct type, so
    names a field rather than reading anything.

    The same ``{k: v}`` reads a local ``k`` in a map literal, and a slice's
    key is an index expression. A literal whose type is elided (an element
    of an outer literal), generic, or not declared once as a struct is not
    proved, and its key is then judged as the read it may be."""
    if not _literal_key(node):
        return False
    literal_value = node.parent.parent.parent
    composite = literal_value.parent if literal_value is not None else None
    if composite is None or composite.type != "composite_literal":
        return False
    written = composite.child_by_field_name("type")
    if written is None:
        return False
    if written.type == "struct_type":
        return True
    owner = packages.resolve_type(written, file)
    return owner is not None and packages.is_struct(owner)


def _is_read(leaf: Node) -> bool:
    """Whether Go counts this mention of a local as a use of it.

    Assigning to it (``x = v``, ``x += v``, ``x++``) is not a use, and neither
    is a declaration's own name: Go still reports ``declared and not used``
    with only those left."""
    parent = leaf.parent
    if parent is None or parent.type in ("inc_statement", "dec_statement") or _literal_key(leaf):
        return False
    if leaf in parent.children_by_field_name("name"):
        return False
    if parent.type == "expression_list" and parent.parent is not None:
        holder = parent.parent
        return holder.child_by_field_name("left") != parent and holder.child_by_field_name("alias") != parent
    return True


#: Statements declaring variables the stranded-binding cleanup never
#: removes: ``for v := range``, ``case v := <-ch`` and ``switch v := x.(type)``.
_UNCLEANED_HOLDERS = ("range_clause", "receive_statement", "type_switch_statement")


def _uncleaned_variable(binder: Node) -> bool:
    """Whether ``binder`` names a ``range``, ``select``-receive or type-switch
    variable. Go reports each unused once its last read folds, and nothing
    here removes or blanks one."""
    parent = binder.parent
    holder = parent.parent if parent is not None else None
    return (
        parent is not None
        and parent.type == "expression_list"
        and holder is not None
        and holder.type in _UNCLEANED_HOLDERS
        and (holder.child_by_field_name("left") == parent or holder.child_by_field_name("alias") == parent)
    )


def _declaration_of(binder: Node) -> Node | None:
    """The ``var`` spec or ``:=`` that declares a local variable whose name
    token is ``binder``. ``None`` for anything else: a parameter, a constant
    or a type, and the variables :func:`_uncleaned_variable` names."""
    parent = binder.parent
    if parent is None:
        return None
    if parent.type == "var_spec":
        return parent
    holder = parent.parent
    if parent.type == "expression_list" and holder is not None and holder.type == "short_var_declaration":
        return holder if holder.child_by_field_name("left") == parent else None
    return None


def _import_reason(name: str, file: go_scope.File) -> str | None:
    """Why the import ``name`` names in ``file`` would stay once unused.

    The stranded-import cleanup finds an import by its alias, or by the name
    it guesses from the path (:func:`flag_cleanup.unused.go_import_name`).
    A name neither spells is bound by an import whose package clause differs
    from its path, and that import would be left behind."""
    for spec in go_scope.import_specs(file.root):
        path_node = spec.child_by_field_name("path")
        alias_node = spec.child_by_field_name("name")
        if path_node is None:
            continue
        alias = None if alias_node is None else _text(alias_node)
        if alias == name or (alias is None and unused.go_import_name(_text(path_node).strip('"`')) == name):
            return None
    return wi.IMPORT_UNRESOLVED


#: Type names every Go file can spell without an import.
_PREDECLARED_TYPES = frozenset({
    "any", "bool", "byte", "comparable", "complex64", "complex128", "error", "float32", "float64",
    "int", "int8", "int16", "int32", "int64", "rune", "string", "uint", "uint8", "uint16", "uint32",
    "uint64", "uintptr",
})


def _dot_import(file: go_scope.File) -> bool:
    return any(
        (alias := spec.child_by_field_name("name")) is not None and _text(alias) == "."
        for spec in go_scope.import_specs(file.root)
    )


def _local_reason(binders: list[Node], file: go_scope.File, packages: go_scope.Packages, seen: set) -> str | None:
    """Why the local declared at ``binders`` could be left unused by the fold.

    The cleanup removes it only when its count of mentions reaches zero, and
    that count includes comments, struct keys, a shadowing local's reads and
    assignments to it. Go stops seeing a use sooner. So every mention it
    counts must be a read Go counts too, of this declaration, or the call is
    refused. Removing the declaration drops what it reads in turn."""
    if len(binders) != 1:
        return wi.UNREMOVABLE_LOCAL
    binder = binders[0]
    declaration = _declaration_of(binder)
    key = (binder.start_byte, binder.end_byte)
    if declaration is None:
        # Go builds with an unused parameter, receiver, constant or type.
        return wi.UNREMOVABLE_LOCAL if _uncleaned_variable(binder) else None
    if key in seen:
        return None
    seen.add(key)
    reads = unused.go_local_reads(file.source, key)
    if reads is None:
        return wi.UNREMOVABLE_LOCAL
    name = _text(binder)
    for start, end in reads:
        leaf = file.root.descendant_for_byte_range(start, end)
        if (
            leaf is None
            or leaf.type != "identifier"
            or not _is_read(leaf)
            or [(b.start_byte, b.end_byte) for b in go_scope.local_binders(leaf, name)] != [key]
        ):
            return wi.UNREMOVABLE_LOCAL
    read = [declaration.child_by_field_name(f) for f in ("type", "value", "right")]
    return _strand_reason([n for n in read if n is not None], file, packages, seen)


def _strand_reason(
    dropped: list[Node], file: go_scope.File, packages: go_scope.Packages, seen: set | None = None
) -> str | None:
    """Why folding away ``dropped`` (a call's receiver and arguments) could
    leave a local or an import unused that the stranded-binding cleanup would
    not remove. Go does not build with either, so such a call is refused."""
    seen = set() if seen is None else seen
    dot = _dot_import(file)
    for top in dropped:
        for node in cross_file_keys.walk(top):
            reason = None
            if node.type == "package_identifier":
                reason = _import_reason(_text(node), file)
            elif node.type == "type_identifier" and dot and _text(node) not in _PREDECLARED_TYPES:
                name = _text(node)
                if not go_scope.local_binders(node, name) and not (
                    file.clause is not None and packages.declares(file.directory, file.clause, name)
                ):
                    reason = wi.IMPORT_UNRESOLVED
            elif node.type == "identifier" and not _struct_field_key(node, file, packages):
                name = _text(node)
                binders = go_scope.local_binders(node, name)
                parent = node.parent
                if binders:
                    reason = _local_reason(binders, file, packages, seen)
                elif file.clause is not None and packages.declares(file.directory, file.clause, name):
                    reason = None
                elif parent is not None and parent.type == "selector_expression" and parent.child_by_field_name("operand") == node:
                    reason = _import_reason(name, file)
                elif dot:
                    # Possibly a name of the dot-imported package, whose import
                    # the cleanup never removes.
                    reason = wi.IMPORT_UNRESOLVED
            if reason is not None:
                return reason
    return None


def _judge(path: str, node: Node, wrapper: wi.Wrapper, file, home, packages) -> wi.Occurrence:
    parent = node.parent
    callee, receiver = node, None
    if node.type == "field_identifier":
        if parent is None or parent.type != "selector_expression" or parent.child_by_field_name("field") != node:
            return wi.Occurrence(path, wi.line_of(node), wi.text_of(parent or node), None, wi.RECEIVER_UNPROVEN)
        callee, receiver = parent, parent.child_by_field_name("operand")
    call = callee.parent
    if (
        call is None
        or call.type != "call_expression"
        or call.child_by_field_name("function") != callee
        or call.child_by_field_name("type_arguments") is not None
        or (call.parent is not None and call.parent.type in ("go_statement", "defer_statement"))
    ):
        return wi.Occurrence(path, wi.line_of(node), wi.text_of(callee), None, wi.RECEIVER_UNPROVEN)
    if wrapper.kind == "function":
        reason = _function_reason(node, receiver, wrapper, file, home, packages)
    else:
        reason = _method_reason(receiver, wrapper, file, home, packages)
    if reason is None:
        reason = _example_reason(file, packages)
    if reason is None and _argument_has_call(call):
        reason = wi.ARGUMENT_HAS_CALL
    if reason is None:
        arguments = call.child_by_field_name("arguments")
        reason = _strand_reason([n for n in (receiver, arguments) if n is not None], file, packages)
    span =(callee.start_byte, callee.end_byte) if reason is None else None
    return wi.Occurrence(path, wi.line_of(node), wi.text_of(call), span, reason)


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    packages = index.packages if index.packages is not None else go_scope.Packages(Path(index.repo_dir))
    file = packages.file(Path(path), source)
    home = packages.file(Path(wrapper.path))
    if file is None or home is None:
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    word = _word(wrapper.name)
    found = [
        wi.Occurrence(path, wi.line_of(c), wi.text_of(c), None, wi.UNPARSED_MENTION)
        for c in cross_file_keys.walk(file.root)
        if c.type == "comment" and _text(c).startswith("//go:linkname") and word.search(_text(c))
    ]
    for node in cross_file_keys.walk(file.root):
        if node.type not in MENTION_TYPES or _text(node) != wrapper.name or names_only(node):
            continue
        found.append(_judge(path, node, wrapper, file, home, packages))
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


wi.register(
    wi.Profile(
        language="go",
        extensions=EXTENSIONS,
        parse=parse,
        read_test=read_test,
        discover=discover,
        occurrences=occurrences,
        index=_index,
        qualify=qualify,
        strings=_STRINGS,
        foreign_extensions=(),
        names_only=names_only,
        members=members,
        mention_types=MENTION_TYPES,
        keep=keep,
    )
)
