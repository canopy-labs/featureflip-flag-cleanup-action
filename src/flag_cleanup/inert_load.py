"""Whether loading a local TS/JS module provably does nothing.

A stranded import statement is cut down to a bare ``import "./m";`` so the
module still loads, because loading it may do work this pass cannot see (see
``unused._ts_import_candidates``). For a specifier that names a file in the
checkout, a relative path or a ``tsconfig.json`` ``paths`` alias, the
module's source is right there. When every module the load reaches only
DECLARES things, the load has no effect and the bare import can go too.

The answer is an allow-list, never a deny-list. A top-level statement counts
as inert only if it is one of the shapes below, and an expression only if it
builds a value without running anything the module did not write as a
literal. Everything else is treated as running code: a call, ``new``, a JSX
element (a factory call), a decorator, a class static block, an ``extends``
clause (it evaluates its operand), a computed key, a spread (it runs an
iterator or getters), an ambient or ``namespace`` block (augmentation the
program may depend on), a triple-slash reference, a read of a name the module
did not bind earlier in the file (a global may not exist, and an import may be
in its temporal dead zone in a cycle), and any operator that can call
``valueOf``/``toString`` on an object or throw on a primitive.

A load the module makes in turn is followed: every non-type import and every
``export … from``. A package specifier cannot be read without
``node_modules``, which the checkout does not have, so it is never inert,
except for the Featureflip SDKs in :data:`SIDE_EFFECT_FREE_PACKAGES`. Neither
is a specifier the resolver cannot pin to one file, a declaration file
(the runtime loads a different file), a file outside the checkout, or a file
that does not parse.

A specifier can also be DECLARED inert by the customer, through the
``side-effect-free-modules`` input, for the loads no proof here can reach (a
module graph that ends in a package, or code at module scope that only looks
like work). That is their word, not a proof: :func:`declaring` holds the list
for one run, :func:`declared` asks it, and the caller reports every deletion
it allows separately from the proven ones. The match is on the exact
spelling of the specifier, never a pattern.

The question is asked of the files on disk at the moment the pass runs. The
run only ever removes code from them, which can make a module more inert but
never less, so a module judged inert stays inert in the tree the pull request
proposes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache, partial
from pathlib import Path

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from flag_cleanup import cross_file_ts

_DECLARATION_FILES = (".d.ts", ".d.mts", ".d.cts")

#: The Featureflip SDK specifiers whose load does nothing: every file each one
#: exports, under every published version and every ``exports`` condition, adds
#: no global, patches no built-in, schedules no timer and registers no
#: listener when it loads. Their publisher makes that claim, so it needs no
#: ``node_modules`` to check. Named one by one, never matched by the
#: ``@featureflip/`` scope, so a package that does work when it loads is never
#: deleted by default. Each is trusted only where it can be nothing but the
#: published package: no compiler config can read it as a file
#: (:func:`cross_file_ts.names_package`), and nothing in the checkout installs,
#: patches or aliases something else under its name (:func:`_substituted`).
SIDE_EFFECT_FREE_PACKAGES = frozenset({
    "@featureflip/js",
    "@featureflip/node",
    "@featureflip/browser",
    "@featureflip/react",
    "@featureflip/react/testing",
    "@featureflip/edge",
})

#: The manifest fields where naming a package only says which published
#: version to install, as long as the version spells a registry range.
_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")

#: The packages behind :data:`SIDE_EFFECT_FREE_PACKAGES`. Most of them load
#: each other (`node` loads `js`, `react` loads `browser`), so a substitute for
#: any one of them reaches a load of another. `edge` loads none of them, and
#: is checked with the rest anyway: one answer for all of them is simpler and
#: only ever keeps more bare imports.
_SDK_PACKAGES = frozenset("/".join(s.split("/")[:2]) for s in SIDE_EFFECT_FREE_PACKAGES)

#: A registry range or tag: version characters only, so no protocol or path
#: (`file:`, `npm:other@1`, `link:`, `workspace:`, `patch:`, `user/repo`, a
#: URL) and no local tarball (`shim.tgz`), each of which installs something
#: other than the published package under its name.
_REGISTRY_RANGE = re.compile(r"^(?!.*\.(tgz|tar|tar\.gz)$)[\w.\-^~<>=|*+ ]*$", re.IGNORECASE)

#: Files where a tool can alias a module name (`resolve.alias`,
#: `moduleNameMapper`, a Babel module resolver, an import map, a pnpm hook).
#: Matched loosely on purpose: a false match only keeps a bare import.
_TOOL_CONFIG = re.compile(
    r"((^|[^a-z])config|webpack|vite|rollup|babel|jest|vitest|esbuild|tsup|parcel|metro|craco"
    r"|pnpmfile|deno|import.?map|swcrc)[^/]*\.([cm]?[jt]sx?|jsonc?)$"
    r"|^\.(babelrc|swcrc)|\.html?$",
    re.IGNORECASE,
)

#: Top-level statements that declare and run nothing, checked no further.
_DECLARES_ONLY = frozenset({
    "empty_statement", "hash_bang_line", "type_alias_declaration",
    "interface_declaration", "function_signature",
})

#: Hoisted, and their bodies do not run when the module loads.
_FUNCTIONS = frozenset({"function_declaration", "generator_function_declaration"})

_CLASSES = frozenset({"class_declaration", "abstract_class_declaration"})

#: Class members that run nothing when the class is defined. A field's
#: initializer runs per instance, except a static one, which is checked.
_INERT_MEMBERS = frozenset({
    "method_definition", "method_signature", "abstract_method_signature",
    "public_field_definition", "index_signature", "comment",
})

#: Literals whose value is a primitive.
_PRIMITIVE_LITERALS = frozenset({"string", "true", "false", "null"})

#: Globals that are always defined and that no write can replace. A module
#: can still DECLARE one of these names, so its own bindings are asked first.
_PRIMITIVE_GLOBALS = frozenset({"undefined", "NaN", "Infinity"})

#: Wrappers that are the expression inside them at runtime.
_TRANSPARENT = frozenset({
    "parenthesized_expression", "as_expression", "satisfies_expression",
    "non_null_expression",
})

#: Expressions that build a closure or an object and run nothing.
_CLOSURES = frozenset({
    "arrow_function", "function_expression", "function", "generator_function",
})

#: Binary operators that only compute when both sides are primitives. `in` and
#: `instanceof` throw on a primitive right-hand side; BigInt operands are never
#: counted as primitives here, because mixing them with a number throws.
_PRIMITIVE_OPERATORS = frozenset({
    "+", "-", "*", "/", "%", "**", "<<", ">>", ">>>", "&", "|", "^",
    "==", "!=", "===", "!==", "<", ">", "<=", ">=", "&&", "||", "??",
})

_PRIMITIVE_UNARY = frozenset({"-", "+", "!", "~"})

_PRIMITIVE = "primitive"
_VALUE = "value"


class _NotInert(Exception):
    pass


@lru_cache(maxsize=None)
def _parser(grammar: str) -> Parser:
    language = (
        tree_sitter_typescript.language_typescript()
        if grammar == "ts"
        else tree_sitter_typescript.language_tsx()
    )
    return Parser(Language(language))


def _grammar(path: str) -> str:
    return "ts" if path.endswith((".ts", ".mts", ".cts")) else "tsx"


#: The ``bindings`` kind of an import deleted only because its specifier was
#: declared (:func:`declared`); its text is the specifier.
DECLARED_LOAD = "declared side-effect-free import"

#: The specifiers the running pass was told load nothing; see :func:`declaring`.
_declared: frozenset[str] = frozenset()


@contextmanager
def declaring(specifiers: Iterable[str]) -> Iterator[None]:
    """Treat a load of each of ``specifiers`` as doing nothing, for the scope.

    Set once per run from the customer's ``side-effect-free-modules`` input,
    like :func:`flag_cleanup.source_index.run_scope`, rather than threaded
    through every call down to the stranded-binding pass."""
    global _declared
    previous = _declared
    _declared = frozenset(specifiers)
    try:
        yield
    finally:
        _declared = previous


def declared(specifier: str) -> bool:
    """Whether the customer declared that loading ``specifier``, spelled
    exactly so, does nothing."""
    return specifier in _declared


def is_inert(reader_path: str, specifier: str, repo_dir: str) -> bool:
    """Whether loading ``specifier`` from ``reader_path`` provably does nothing."""
    from flag_cleanup.piranha_runner import checkout_root

    stop = Path(checkout_root(repo_dir)).resolve()
    seen: set[str] = set()
    try:
        _check_load(reader_path, specifier, repo_dir, stop, seen)
    except (_NotInert, RecursionError, OSError):
        # Recursion follows the data (an operand chain, an import chain), and
        # generated code can outrun it; a directory may not be readable.
        # Unproven, so the bare import stays.
        return False
    return True


def _check_load(reader: str, specifier: str, repo_dir: str, stop: Path, seen: set[str]) -> None:
    if (
        specifier in SIDE_EFFECT_FREE_PACKAGES
        and cross_file_ts.names_package(reader, specifier, repo_dir)
        and not _substituted(str(stop))
    ):
        return
    target = cross_file_ts.resolve_specifier(reader, specifier, repo_dir, solution=True)
    if target is None or target.endswith(_DECLARATION_FILES):
        raise _NotInert
    resolved = Path(target).resolve()
    if not resolved.is_relative_to(stop):
        raise _NotInert
    if str(resolved) in seen:
        return
    seen.add(str(resolved))
    try:
        source = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise _NotInert from None
    root = _parser(_grammar(str(resolved))).parse(source.encode("utf-8")).root_node
    if root.has_error or _declaring_script(root):
        raise _NotInert
    for loaded in _module_loads(root):
        _check_load(str(resolved), loaded, repo_dir, stop, seen)


def _sdk(reader: str, specifier: str, repo_dir: str, stop: Path) -> bool:
    """Whether ``specifier`` is an audited SDK whose load does nothing."""
    return (
        specifier in SIDE_EFFECT_FREE_PACKAGES
        and cross_file_ts.names_package(reader, specifier, repo_dir)
        and not _substituted(str(stop))
    )


def coverage(reader_path: str, repo_dir: str) -> Callable[[str, tuple[int, int], str], bool]:
    """A check of whether a load ``reader_path`` is about to drop is COVERED.

    ``covered(source, own, specifier)`` asks it of the import statement
    spanning the bytes ``own`` of ``source``, the file as it now stands, which
    loads ``specifier``. Covered means dropping that statement drops no
    module that may do work when it loads: every such module its load reaches
    is loaded anyway by another import that ``source`` keeps
    (:func:`_certain_loads`), directly or through the files those load.
    Modules that only declare things (the allow-list :func:`is_inert` applies)
    are walked through rather than asked for, since loading them does nothing
    either way.

    The module may now load a few imports later in this file than it did,
    when only an import below this one reaches it. That is the one change in
    behaviour, and it shows only when a module relies on another's load-time
    work without importing it. A module loaded by another FILE does not
    count: whether that file is in the same program, or loads before this
    one, is not knowable here.

    Files are read once per check made with this ``coverage``, which serves
    one file's pass: a pass over the next file sees what this one wrote.
    """
    stop = Path(_checkout_root(repo_dir)).resolve()
    reader = str(Path(reader_path).resolve())
    files: dict[str, Node | None] = {}

    def parsed(path: str) -> Node | None:
        if path not in files:
            try:
                source = Path(path).read_bytes()
            except OSError:
                files[path] = None
            else:
                root = _parser(_grammar(path)).parse(source).root_node
                files[path] = None if root.has_error else root
        return files[path]

    def identity(importer: str, specifier: str) -> str | tuple[str, str] | None:
        """What loading ``specifier`` from ``importer`` loads, comparable
        across importers: a file's path, or a package with the directory its
        lookup starts from. ``None`` when that cannot be told."""
        target = cross_file_ts.resolve_specifier(importer, specifier, repo_dir, solution=True)
        if target is not None:
            # A declaration file is not what the runtime loads.
            return None if target.endswith(_DECLARATION_FILES) else str(Path(target).resolve())
        scope = cross_file_ts.package_scope(importer, specifier, repo_dir)
        return None if scope is None else (specifier, scope)

    def reached(source: str, own: tuple[int, int]) -> set[str | tuple[str, str]]:
        root = _parser(_grammar(reader)).parse(source.encode("utf-8")).root_node
        if root.has_error:
            return set()
        found: set[str | tuple[str, str]] = set()
        pending = [
            (reader, specifier)
            for specifier in _certain_loads(root, own, lambda s, n: value_export(reader, s, n))
        ]
        while pending:
            importer, specifier = pending.pop()
            target = identity(importer, specifier)
            if target is None or target in found:
                continue
            found.add(target)
            if isinstance(target, str) and target != reader and target.startswith(str(stop) + os.sep):
                file = parsed(target)
                if file is not None:
                    values = partial(value_export, target)
                    pending.extend((target, s) for s in _certain_loads(file, None, values))
        return found

    def value_export(importer: str, specifier: str, name: str, depth: int = 0) -> bool:
        """Whether the module ``specifier`` names from ``importer`` exports
        ``name`` as a value, which is when a compiler keeps an
        ``export { name } from`` of it. False when unsure."""
        target = cross_file_ts.resolve_specifier(importer, specifier, repo_dir, solution=True)
        if target is None or target.endswith(_DECLARATION_FILES) or depth > _EXPORT_DEPTH:
            return False
        file = parsed(str(Path(target).resolve()))
        return file is not None and _exports_value(
            file, name, lambda s, n: value_export(target, s, n, depth + 1)
        )

    def check(importer: str, specifier: str, loaded: set, seen: set[str]) -> None:
        target = identity(importer, specifier)
        if target is not None and target in loaded:
            return
        if _sdk(importer, specifier, repo_dir, stop):
            return
        if not isinstance(target, str) or not Path(target).is_relative_to(stop):
            raise _NotInert
        if target == reader or target in seen:
            # The reader is mid-load whenever its own import reaches it.
            return
        seen.add(target)
        file = parsed(target)
        if file is None or _declaring_script(file):
            raise _NotInert
        for load in _module_loads(file):
            check(target, load, loaded, seen)

    def covered(source: str, own: tuple[int, int], specifier: str) -> bool:
        try:
            check(reader, specifier, reached(source, own), set())
        except (_NotInert, RecursionError, OSError, UnicodeDecodeError):
            return False
        return True

    return covered


def _checkout_root(repo_dir: str) -> str:
    from flag_cleanup.piranha_runner import checkout_root

    return checkout_root(repo_dir)


#: How many ``export … from`` hops :func:`coverage` follows to find a value.
_EXPORT_DEPTH = 16

#: Where an identifier leaf DECLARES a name rather than reading it, keyed by
#: its parent's type: the field it must sit in, or ``None`` for any. Over-
#: inclusive on purpose, since a name declared anywhere else in a file may
#: shadow an import (:func:`_kept_binding`).
_DECLARING = {
    "variable_declarator": "name", "assignment_pattern": "left",
    "object_assignment_pattern": "left", "pair_pattern": "value",
    "required_parameter": "pattern", "optional_parameter": "pattern",
    "formal_parameters": None, "rest_pattern": None, "array_pattern": None,
    "object_pattern": None, "function_declaration": "name",
    "function_expression": "name", "function": "name",
    "generator_function_declaration": "name", "generator_function": "name",
    "class_declaration": "name", "abstract_class_declaration": "name",
    "class": "name", "catch_clause": "parameter", "arrow_function": "parameter",
    "for_in_statement": "left", "enum_declaration": "name",
    "internal_module": "name", "module": "name", "import_specifier": None,
    "namespace_import": None, "import_clause": None,
}

#: Nodes whose subtree the compiler erases, so a name under one is not read.
_ERASED = frozenset({
    "type_annotation", "type_arguments", "type_parameters", "type_query",
    "nested_type_identifier", "implements_clause", "extends_type_clause",
    "type_alias_declaration", "interface_declaration", "ambient_declaration",
})

_JSX_ELEMENTS = frozenset({"jsx_opening_element", "jsx_closing_element", "jsx_self_closing_element"})

#: Declarations that create a runtime value of their name.
_VALUE_DECLARATIONS = frozenset({
    "function_declaration", "generator_function_declaration",
    "class_declaration", "abstract_class_declaration", "enum_declaration",
})


def _certain_loads(
    root: Node, own: tuple[int, int] | None, value_export: Callable[[str, str], bool]
) -> list[str]:
    """The specifiers ``root``'s top level loads under every compiler setting,
    skipping the statement that overlaps the bytes ``own``.

    An import counts when it binds nothing, or when one of its names is read
    as a value and declared nowhere else in the file: a name whose every read
    is erased or may be shadowed lets the compiler drop the whole statement,
    as ``tsc`` does. ``export * from`` always counts, and
    ``export { a } from`` only when ``a`` is a value there (``value_export``);
    a type re-export is dropped. ``import type``, ``export type`` and
    ``import x = require`` never count.
    """
    loads = []
    for statement in root.named_children:
        if own is not None and statement.start_byte < own[1] and statement.end_byte > own[0]:
            continue
        tokens = {c.type for c in statement.children if not c.is_named}
        source = statement.child_by_field_name("source")
        if source is None or "type" in tokens:
            continue
        specifier = _source(statement)
        if statement.type == "import_statement":
            clause = next((c for c in statement.named_children if c.type == "import_clause"), None)
            if clause is None or any(_kept_binding(root, statement, n) for n in _bound(clause)):
                loads.append(specifier)
        elif statement.type == "export_statement":
            if "*" in tokens or any(c.type == "namespace_export" for c in statement.named_children):
                loads.append(specifier)
                continue
            if any(value_export(specifier, name) for name, _ in _export_specifiers(statement)):
                loads.append(specifier)
    return loads


def _bound(clause: Node) -> list[str]:
    """The names an ``import_clause`` binds, minus inline ``type`` ones."""
    names = []
    for child in clause.named_children:
        if child.type == "identifier":
            names.append(child)
        elif child.type == "namespace_import":
            names.extend(c for c in child.named_children if c.type == "identifier")
        elif child.type == "named_imports":
            for specifier in child.named_children:
                if specifier.type != "import_specifier" or any(c.type == "type" for c in specifier.children):
                    continue
                names.append(specifier.child_by_field_name("alias") or specifier.child_by_field_name("name"))
    return [n.text.decode("utf-8", "replace") for n in names if n is not None]


def _export_specifiers(statement: Node) -> list[tuple[str, str]]:
    """Each ``(local, exported)`` name pair an ``export { … }`` clause lists,
    minus inline ``type`` ones."""
    pairs = []
    for clause in (c for c in statement.named_children if c.type == "export_clause"):
        for specifier in clause.named_children:
            if specifier.type != "export_specifier" or any(c.type == "type" for c in specifier.children):
                continue
            name = specifier.child_by_field_name("name")
            alias = specifier.child_by_field_name("alias") or name
            if name is not None:
                pairs.append(
                    (name.text.decode("utf-8", "replace"), alias.text.decode("utf-8", "replace"))
                )
    return pairs


def _kept_binding(root: Node, statement: Node, name: str) -> bool:
    """Whether a compiler keeps ``statement`` for its binding ``name``: the
    file reads it as a value, and declares the name nowhere else, so that no
    read can be a shadowing local's."""
    read = False
    stack = [root]
    while stack:
        node = stack.pop()
        if node.id == statement.id:
            continue
        stack.extend(node.children)
        if node.child_count or node.text.decode("utf-8", "replace") != name:
            continue
        if node.type == "shorthand_property_identifier_pattern" or _declares(node):
            return False
        if node.type in {"identifier", "shorthand_property_identifier"} and not _erased(node):
            read = True
    return read


def _declares(node: Node) -> bool:
    parent = node.parent
    if parent is None or node.type != "identifier" or parent.type not in _DECLARING:
        return False
    field = _DECLARING[parent.type]
    if field is None:
        return True
    held = parent.child_by_field_name(field)
    return held is not None and held.id == node.id


def _erased(node: Node) -> bool:
    """Whether the compiler emits nothing that reads ``node``: it sits in a
    type, names an intrinsic JSX element (``<div>`` is a string), or is
    exported as is (``export { a }``, ``export default a``), which keeps
    the import only when the name is a value there, unknown here."""
    parent = node.parent
    if parent is not None and parent.type in _JSX_ELEMENTS and node.text[:1].islower():
        return True
    if parent is not None and parent.type in {"export_specifier", "export_statement"}:
        return True
    current = parent
    while current is not None:
        if current.type in _ERASED:
            return True
        current = current.parent
    return False


def _exports_value(root: Node, name: str, value_export: Callable[[str, str], bool]) -> bool:
    """Whether the module ``root`` exports ``name`` as a value. A re-export
    asks ``value_export(specifier, name)`` of the module it names."""
    values = {n for top in root.named_children for n in _value_names(top)}
    for statement in root.named_children:
        if statement.type != "export_statement":
            continue
        tokens = {c.type for c in statement.children if not c.is_named}
        if "type" in tokens:
            continue
        source = statement.child_by_field_name("source")
        declaration = statement.child_by_field_name("declaration")
        value = statement.child_by_field_name("value")
        if "default" in tokens:
            if name != "default":
                continue
            if declaration is not None:
                return bool(_value_names(declaration)) or declaration.type in _VALUE_EXPRESSIONS
            # `export default T` is erased when `T` names only a type.
            return value is not None and (
                value.type != "identifier" or value.text.decode("utf-8", "replace") in values
            )
        namespace = next((c for c in statement.named_children if c.type == "namespace_export"), None)
        if namespace is not None:
            if any(c.text.decode("utf-8", "replace") == name for c in namespace.named_children):
                return True
            continue
        if source is not None and "*" in tokens:
            if name != "default" and value_export(_source(statement), name):
                return True
            continue
        if declaration is not None:
            if name in _value_names(declaration):
                return True
            continue
        for local, exported in _export_specifiers(statement):
            if exported == name and (
                value_export(_source(statement), local) if source is not None else local in values
            ):
                return True
    return False


#: An ``export default`` declaration with no name that is still a value.
_VALUE_EXPRESSIONS = frozenset({"function_expression", "function", "class", "generator_function"})


def _value_names(declaration: Node) -> list[str]:
    """The names a top-level declaration binds to runtime values."""
    if declaration.type in _VALUE_DECLARATIONS:
        if declaration.type == "enum_declaration" and any(c.type == "const" for c in declaration.children):
            return []
        return [_name(declaration)]
    if declaration.type in {"lexical_declaration", "variable_declaration"}:
        return [
            d.child_by_field_name("name").text.decode("utf-8", "replace")
            for d in declaration.named_children
            if d.type == "variable_declarator" and d.child_by_field_name("name").type == "identifier"
        ]
    return []


@lru_cache(maxsize=None)
def _substituted(root: str) -> bool:
    """Whether anything in the checkout can make an SDK package load
    something other than what its publisher shipped.

    Every ``package.json`` is read, since a hoisted workspace dependency can
    serve a file in another package. One counts when it IS an SDK package (a
    workspace member, a vendored copy, the package's own repository), when it
    depends on one with anything but a registry range, or when it names one
    anywhere else (``overrides``, ``resolutions``, ``browser``, ``alias``,
    ``patchedDependencies``, a ``jest`` mapper). So does a patch-package patch
    for one, a tool config or HTML page that mentions one at all, a
    ``node_modules`` committed with the ``@featureflip`` scope in it, and a
    directory symlinked to somewhere outside the checkout, which the walk
    does not follow. Anything unreadable counts too. Aliases written in other
    files, such as a build script, are not seen.
    """
    names = [package.encode() for package in _SDK_PACKAGES]
    patches = tuple(package.replace("/", "+") + "+" for package in _SDK_PACKAGES)
    top = Path(root).resolve()

    def refuse(error: OSError) -> None:
        raise error

    try:
        for directory, subdirectories, files in os.walk(root, onerror=refuse):
            if "node_modules" in subdirectories and os.path.lexists(
                os.path.join(directory, "node_modules", "@featureflip")
            ):
                return True
            subdirectories[:] = [d for d in subdirectories if d not in {"node_modules", ".git"}]
            for name in subdirectories:
                path = os.path.join(directory, name)
                if os.path.islink(path) and not Path(path).resolve().is_relative_to(top):
                    return True
            for name in files:
                path = os.path.join(directory, name)
                if name.startswith(patches):
                    return True
                if name == "package.json":
                    if _manifest_substitutes(path):
                        return True
                elif _TOOL_CONFIG.search(name):
                    with open(path, "rb") as config:
                        text = config.read()
                    if any(package in text for package in names):
                        return True
    except OSError:
        return True
    return False


def _manifest_substitutes(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as manifest:
            data = json.load(manifest)
    except (OSError, UnicodeDecodeError, ValueError):
        return True
    if not isinstance(data, dict) or data.get("name") in _SDK_PACKAGES:
        return True
    rest = dict(data)
    for field in _DEPENDENCY_FIELDS:
        dependencies = rest.get(field)
        if not isinstance(dependencies, dict):
            continue
        for package in _SDK_PACKAGES & dependencies.keys():
            spec = dependencies[package]
            if not isinstance(spec, str) or not _REGISTRY_RANGE.match(spec):
                return True
        rest[field] = {k: v for k, v in dependencies.items() if k not in _SDK_PACKAGES}
    text = json.dumps(rest, ensure_ascii=False)
    return any(package in text for package in _SDK_PACKAGES)


def _declaring_script(root: Node) -> bool:
    """A file with no ``import`` or ``export`` is a global SCRIPT to the
    compiler, so whatever it declares is global, and this import may be the
    only thing putting it in the program. One that declares nothing (what a
    keys module becomes once the run deletes its last constant) is not."""
    statements = [
        c for c in root.named_children
        if c.type not in {"comment", "empty_statement", "hash_bang_line"}
        and not (c.type == "expression_statement" and [n.type for n in c.named_children] == ["string"])
    ]
    return bool(statements) and not any(
        c.type in {"import_statement", "export_statement"} for c in statements
    )


def _module_loads(root: Node) -> list[str]:
    """The specifiers ``root`` loads, raising if any top-level statement runs."""
    bound = {
        _name(statement)
        for statement in _top_level_declarations(root)
        if statement.type in _FUNCTIONS
    }
    primitive: set[str] = set()
    loads: list[str] = []
    for statement in root.named_children:
        loads.extend(_statement(statement, bound, primitive))
    return loads


def _top_level_declarations(root: Node) -> list[Node]:
    found = []
    for statement in root.named_children:
        if statement.type == "export_statement":
            declaration = statement.child_by_field_name("declaration")
            if declaration is not None:
                found.append(declaration)
        else:
            found.append(statement)
    return found


def _name(node: Node) -> str:
    name = node.child_by_field_name("name")
    return name.text.decode("utf-8", "replace") if name is not None else ""


def _source(node: Node) -> str:
    return node.child_by_field_name("source").text.decode("utf-8", "replace")[1:-1]


def _statement(node: Node, bound: set[str], primitive: set[str]) -> list[str]:
    kind = node.type
    if kind == "comment":
        if node.text.startswith(b"///") and node.text[3:].lstrip().startswith(b"<reference"):
            raise _NotInert
        return []
    if kind in _DECLARES_ONLY or kind in _FUNCTIONS:
        return []
    if kind == "expression_statement":
        # A directive prologue entry ("use strict", "use client").
        if [c.type for c in node.named_children] == ["string"]:
            return []
        raise _NotInert
    if kind == "import_statement":
        if any(c.type == "type" for c in node.children):
            return []
        if any(c.type == "import_require_clause" for c in node.named_children):
            raise _NotInert
        return [_source(node)]
    if kind == "export_statement":
        return _export(node, bound, primitive)
    if kind in _CLASSES:
        _class(node, bound, primitive)
        bound.add(_name(node))
        return []
    if kind in {"lexical_declaration", "variable_declaration"}:
        _variables(node, bound, primitive)
        return []
    if kind == "enum_declaration":
        _enum(node, bound, primitive)
        bound.add(_name(node))
        return []
    raise _NotInert


def _export(node: Node, bound: set[str], primitive: set[str]) -> list[str]:
    tokens = {c.type for c in node.children if not c.is_named}
    if "decorator" in {c.type for c in node.named_children} or "=" in tokens or {"as", "namespace"} <= tokens:
        raise _NotInert
    if "type" in tokens:
        return []
    if node.child_by_field_name("source") is not None:
        return [_source(node)]
    declaration = node.child_by_field_name("declaration")
    if declaration is not None:
        return _statement(declaration, bound, primitive)
    value = node.child_by_field_name("value")
    if value is not None:
        if _kind(value, bound, primitive) is None:
            raise _NotInert
        return []
    if all(c.type in {"export_clause", "comment"} for c in node.named_children):
        return []
    raise _NotInert


def _class(node: Node, bound: set[str], primitive: set[str]) -> None:
    if _contains(node, "decorator"):
        raise _NotInert
    for heritage in (c for c in node.named_children if c.type == "class_heritage"):
        if any(c.type == "extends_clause" for c in heritage.named_children):
            raise _NotInert
    body = node.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        if member.type not in _INERT_MEMBERS:
            raise _NotInert
        name = member.child_by_field_name("name")
        if name is not None and name.type == "computed_property_name":
            raise _NotInert
        value = member.child_by_field_name("value")
        static = any(c.type == "static" for c in member.children)
        if static and value is not None and _kind(value, bound, primitive) is None:
            raise _NotInert


def _variables(node: Node, bound: set[str], primitive: set[str]) -> None:
    for declarator in node.named_children:
        if declarator.type == "comment":
            continue
        if declarator.type != "variable_declarator":
            raise _NotInert
        name = declarator.child_by_field_name("name")
        if name is None or name.type != "identifier":
            raise _NotInert
        value = declarator.child_by_field_name("value")
        text = name.text.decode("utf-8", "replace")
        if value is None:
            # `var x;` again keeps whatever `x` already holds.
            kind = _VALUE if text in bound and text not in primitive else _PRIMITIVE
        else:
            kind = _kind(value, bound, primitive)
        if kind is None:
            raise _NotInert
        bound.add(text)
        if kind == _PRIMITIVE:
            primitive.add(text)
        else:
            primitive.discard(text)


def _enum(node: Node, bound: set[str], primitive: set[str]) -> None:
    body = node.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        if member.type == "enum_assignment":
            value = member.child_by_field_name("value")
            if value is None or _kind(value, bound, primitive) != _PRIMITIVE:
                raise _NotInert
        elif member.type not in {"property_identifier", "string", "comment"}:
            raise _NotInert


def _contains(node: Node, kind: str) -> bool:
    return node.type == kind or any(_contains(c, kind) for c in node.named_children)


def _kind(node: Node, bound: set[str], primitive: set[str]) -> str | None:
    """``"primitive"``, ``"value"`` or ``None`` (evaluating it may run code)."""
    kind = node.type
    if kind in _PRIMITIVE_LITERALS:
        return _PRIMITIVE
    if kind == "number":
        return _VALUE if node.text.lower().endswith(b"n") else _PRIMITIVE
    if kind == "regex":
        return _VALUE
    if kind in {"identifier", "undefined", "shorthand_property_identifier"}:
        name = node.text.decode("utf-8", "replace")
        # The module's own binding first: it may shadow a global's name.
        if name in primitive:
            return _PRIMITIVE
        if name in bound:
            return _VALUE
        return _PRIMITIVE if name in _PRIMITIVE_GLOBALS else None
    if kind == "template_string":
        for part in node.named_children:
            if part.type == "template_substitution":
                inner = [c for c in part.named_children if c.type != "comment"]
                if len(inner) != 1 or _kind(inner[0], bound, primitive) != _PRIMITIVE:
                    return None
            elif part.type not in {"string_fragment", "escape_sequence"}:
                return None
        return _PRIMITIVE
    if kind in _TRANSPARENT:
        inner = [c for c in node.named_children if c.type != "comment"]
        return _kind(inner[0], bound, primitive) if inner else None
    if kind == "unary_expression":
        operator = node.child_by_field_name("operator").type
        operand = _kind(node.child_by_field_name("argument"), bound, primitive)
        if operator in {"typeof", "void"}:
            return _PRIMITIVE if operand is not None else None
        if operator in _PRIMITIVE_UNARY and operand == _PRIMITIVE:
            return _PRIMITIVE
        return None
    if kind == "binary_expression":
        operator = node.child_by_field_name("operator").type
        left = _kind(node.child_by_field_name("left"), bound, primitive)
        right = _kind(node.child_by_field_name("right"), bound, primitive)
        if operator in _PRIMITIVE_OPERATORS and left == right == _PRIMITIVE:
            return _PRIMITIVE
        return None
    if kind in _CLOSURES:
        return _VALUE
    if kind == "array":
        for element in node.named_children:
            if element.type != "comment" and _kind(element, bound, primitive) is None:
                return None
        return _VALUE
    if kind == "object":
        for member in node.named_children:
            if member.type == "comment":
                continue
            if member.type == "shorthand_property_identifier":
                if _kind(member, bound, primitive) is None:
                    return None
                continue
            if member.type not in {"pair", "method_definition"}:
                return None
            key = member.child_by_field_name("key" if member.type == "pair" else "name")
            if key is None or key.type == "computed_property_name" or _contains(member, "decorator"):
                return None
            if member.type == "pair" and _kind(member.child_by_field_name("value"), bound, primitive) is None:
                return None
        return _VALUE
    return None
