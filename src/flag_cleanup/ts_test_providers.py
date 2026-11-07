"""Unwrap a Featureflip test provider that no longer serves a flag.

A React test renders its subject inside the SDK's test provider so that a
flag read in that tree gets a value:

.. code-block:: tsx

    render(
      <TestFeatureflipProvider flags={{ 'new-checkout': true }}>
        <CheckoutPage />
      </TestFeatureflipProvider>
    );

Removing ``new-checkout`` strips the entry, and a provider that served nothing
else is left serving nothing (``flags={{}}``, or no ``flags`` at all where the
override's default is ``{}``). Whether it can go depends on what it wraps: a
component that still reads ANY flag, however deep, needs the provider, and
without one the SDK's hooks throw. So it is unwrapped (replaced by what it
wrapped, and its import left to the stranded-binding cleanup) only when every
module the test file loads, followed transitively, provably never loads a
Featureflip module. Which modules a file loads is read from its own text and
resolved the way the inert-load proof resolves a module graph
(:func:`flag_cleanup.cross_file_ts.resolve_specifier`); a module that cannot be resolved,
read or parsed refuses, and so does a load spelled with anything but a string.

The provider is the SDK's own ``TestFeatureflipProvider`` (from
``@featureflip/react``), or a wrapper of it in the customer's repository: an
exported function whose whole body returns the SDK's provider around
``{children}``. Only a provider THIS run emptied counts: every element of it
in the file had an override for the flag, and has nothing left but an empty
one. Any other element of it in the file, or any other use of its name (a
``wrapper:`` option), leaves them all in place.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, cross_file_ts, ts_gates, ts_scope

#: The ``bindings`` kind of a provider this run unwrapped. Text: its line.
UNWRAPPED_PROVIDER = "unwrapped test provider"

_SDK = ("@featureflip/react", "@featureflip/react/testing")
_PROVIDER = "TestFeatureflipProvider"

#: How many `export { x } from` barrels a provider's import may pass through.
_HOPS = 4

#: How many modules one proof may read before it gives up (and keeps the provider).
_BUDGET = 5000

#: An npm package name, optionally with a subpath. Anything else that is not
#: relative (`@/lib/x`, `~/x`, `#x`) is an alias, never a package.
_PACKAGE = re.compile(r"^(?:node:)?(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:/.*)?$")

#: Files a bundler loads as data, never as code that could render a component.
_ASSETS = frozenset(
    """
    .css .scss .sass .less .styl .json .svg .png .jpg .jpeg .gif .webp .avif .ico .bmp
    .woff .woff2 .ttf .otf .eot .mp3 .mp4 .webm .txt
    """.split()
)

#: Node's own modules, loadable without being declared.
_NODE_BUILTINS = frozenset(
    "assert buffer child_process crypto events fs http https os path stream url util zlib".split()
)


class _Unproven(Exception):
    """The proof could not be completed."""


def covers(grammar: str) -> bool:
    return grammar in ("tsx", "js")


def unwrap(before: str, after: str, path: str, repo_dir: str, flag_key: str) -> tuple[str, list[str]]:
    """``after`` with each Featureflip test provider this run emptied replaced
    by its children, when nothing the file loads reaches a Featureflip
    module; and the line of each element unwrapped. ``after`` unchanged
    otherwise."""
    grammar = ts_scope.grammar(path)
    if not covers(grammar) or before == after:
        return after, []
    root = ts_scope.parse(after, path)
    if root.has_error:
        return after, []
    data = after.encode("utf-8")
    for local, statement, solo in _providers(root, data, path, repo_dir):
        elements = _elements(root, data, local)
        if elements is None or not elements:
            continue
        old_root = ts_scope.parse(before, path)
        old_elements = _elements(old_root, before.encode("utf-8"), local)
        if old_elements is None or len(old_elements) != len(elements):
            continue
        if not all(_overrides(o, before.encode("utf-8"), flag_key) for o in old_elements):
            continue
        if not all(_emptied(e, data) for e in elements):
            continue
        try:
            _prove(root, data, path, repo_dir, statement if solo else None)
        except (_Unproven, OSError, RecursionError, UnicodeDecodeError):
            continue
        return _unwrapped(after, path, local)
    return after, []


def _unwrapped(source: str, path: str, local: str) -> tuple[str, list[str]]:
    lines: list[str] = []
    for _ in range(1000):
        data = source.encode("utf-8")
        elements = _elements(ts_scope.parse(source, path), data, local) or []
        if not elements:
            break
        element = elements[0]
        lines.append(data[element.start_byte :].split(b"\n", 1)[0].decode("utf-8").strip())
        data = data[: element.start_byte] + ts_gates._children_text(element, data) + data[element.end_byte :]
        source = data.decode("utf-8")
    return source, lines


def _providers(root: Node, data: bytes, path: str, repo_dir: str) -> list[tuple[str, Node, bool]]:
    """``(local name, import statement, sole binding of it)`` of each binding
    of a Featureflip test provider in the file."""
    found = []
    for statement in root.named_children:
        imported = _named_imports(statement)
        if imported is None:
            continue
        specifier, names, count = imported
        for name, local in names:
            if not _bound_once(root, local):
                continue
            if _is_provider(specifier, name, path, repo_dir):
                found.append((local, statement, count == 1))
    return found


_TAGS = frozenset({"jsx_opening_element", "jsx_closing_element", "jsx_self_closing_element"})


def _bound_once(root: Node, name: str) -> bool:
    """Whether the file binds ``name`` exactly once. A JSX tag spelling it is
    a use, though the grammar gives it the field a binding has."""
    return len([n for n in ts_scope.bindings(root, name) if n.parent is None or n.parent.type not in _TAGS]) == 1


def _resolve(reader: str, specifier: str, repo_dir: str) -> str | None:
    """The file ``specifier`` names from ``reader``, read as the inert-load
    proof reads a module graph: through a solution config's references too."""
    return cross_file_ts.resolve_specifier(reader, specifier, repo_dir, solution=True)


def _named_imports(statement: Node) -> tuple[str, list[tuple[str, str]], int] | None:
    """``(specifier, [(imported, local)], bindings)`` of a value import."""
    if statement.type != "import_statement" or any(c.type == "type" for c in statement.children):
        return None
    specifier = _specifier(statement.child_by_field_name("source"))
    clause = next((c for c in statement.children if c.type == "import_clause"), None)
    if specifier is None or clause is None:
        return None
    names: list[tuple[str, str]] = []
    count = 0
    for child in clause.named_children:
        if child.type == "named_imports":
            for item in child.named_children:
                if item.type != "import_specifier":
                    continue
                count += 1
                if any(c.type == "type" for c in item.children):
                    continue
                name = item.child_by_field_name("name")
                alias = item.child_by_field_name("alias")
                if name is not None:
                    names.append((ts_scope.text(name), ts_scope.text(alias or name)))
        else:
            count += 1
    return specifier, names, count


def _specifier(node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    fragments = node.named_children
    if len(fragments) != 1 or fragments[0].type != "string_fragment":
        return None
    return ts_scope.text(fragments[0])


def _is_provider(specifier: str, name: str, path: str, repo_dir: str) -> bool:
    """Whether ``name`` from ``specifier`` is the SDK's test provider or a
    repository wrapper of it."""
    if specifier in _SDK:
        return name == _PROVIDER and cross_file_ts.names_package(path, specifier, repo_dir)
    reader = path
    for _ in range(_HOPS):
        target = _resolve(reader, specifier, repo_dir)
        if target is None or not target.endswith(ts_scope.FAMILY_EXTENSIONS) or target.endswith(".d.ts"):
            return False
        try:
            source = Path(target).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        root = ts_scope.parse(source, target)
        if root.has_error:
            return False
        data = source.encode("utf-8")
        function = _exported_function(root, name)
        if function is not None:
            sdk = {
                local
                for statement in root.named_children
                if (imported := _named_imports(statement)) is not None and imported[0] in _SDK
                for imported_name, local in imported[1]
                if imported_name == _PROVIDER and _bound_once(root, local)
            }
            return bool(sdk) and _returns_provider_of_children(function, data, sdk)
        # A barrel: `export { name } from './module'` (or `name as other`).
        hop = _reexport(root, name)
        if hop is None:
            return False
        reader, (specifier, name) = target, hop
    return False


def _reexport(root: Node, name: str) -> tuple[str, str] | None:
    """``(specifier, original name)`` when the file exports ``name`` by
    exactly one ``export { original as name } from 'specifier'``, and does
    not declare or export it any other way."""
    found: tuple[str, str] | None = None
    for statement in root.named_children:
        if statement.type != "export_statement":
            continue
        declaration = statement.child_by_field_name("declaration")
        if declaration is not None:
            declared = {
                ts_scope.text(n)
                for n in cross_file_keys.walk(declaration)
                if n.type in ("identifier", "type_identifier")
                and n.parent is not None
                and n.parent.type in ("function_declaration", "class_declaration", "variable_declarator")
                and n.parent.child_by_field_name("name") is not None
                and n.parent.child_by_field_name("name").start_byte == n.start_byte
            }
            if name in declared:
                return None  # declared here too: two bindings of the name
            continue
        clause = next((c for c in statement.named_children if c.type == "export_clause"), None)
        source = _specifier(statement.child_by_field_name("source"))
        if clause is None:
            continue
        if any(c.type == "type" for c in statement.children):
            continue
        for item in clause.named_children:
            if item.type != "export_specifier" or any(c.type == "type" for c in item.children):
                continue
            original = item.child_by_field_name("name")
            alias = item.child_by_field_name("alias")
            exported = ts_scope.text(alias or original) if original is not None else None
            if exported != name:
                continue
            if source is None or found is not None or original is None:
                return None
            found = (source, ts_scope.text(original))
    return found


def _exported_function(root: Node, name: str) -> Node | None:
    """``export function <name>(…) {…}``, declared once."""
    found = None
    for statement in root.named_children:
        declaration = statement.child_by_field_name("declaration") if statement.type == "export_statement" else None
        if declaration is None or declaration.type != "function_declaration":
            continue
        declared = declaration.child_by_field_name("name")
        if declared is not None and ts_scope.text(declared) == name:
            if found is not None:
                return None
            found = declaration
    if found is None or not _bound_once(root, name):
        return None
    return found


def _returns_provider_of_children(function: Node, data: bytes, sdk: set[str]) -> bool:
    """Whether ``function``'s body is ``return <P …>{children}</P>;`` with
    ``P`` the SDK's provider and ``children`` its own destructured prop."""
    body = function.child_by_field_name("body")
    parameters = function.child_by_field_name("parameters")
    if body is None or parameters is None:
        return False
    statements = [c for c in body.named_children if c.type != "comment"]
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    value = statements[0].named_children[0] if statements[0].named_children else None
    while value is not None and value.type == "parenthesized_expression":
        value = value.named_children[0] if value.named_children else None
    if value is None or value.type != "jsx_element":
        return False
    open_tag = value.child_by_field_name("open_tag")
    name = open_tag.child_by_field_name("name") if open_tag is not None else None
    if name is None or ts_scope.text(name) not in sdk:
        return False
    children = ts_gates._children(value, data)
    if len(children) != 1 or children[0].type != "jsx_expression":
        return False
    inner = children[0].named_children
    if len(inner) != 1 or inner[0].type != "identifier" or ts_scope.text(inner[0]) != "children":
        return False
    return b"children" in data[parameters.start_byte : parameters.end_byte]


def _elements(root: Node, data: bytes, local: str) -> list[Node] | None:
    """Every ``<local>…</local>`` in the file, outermost first; ``None`` when
    the name is used any other way (self-closing, as a value, …)."""
    elements: list[Node] = []
    names = 0
    for node in cross_file_keys.walk(root):
        if node.type == "jsx_element":
            open_tag = node.child_by_field_name("open_tag")
            tag = open_tag.child_by_field_name("name") if open_tag is not None else None
            if tag is not None and tag.type == "identifier" and ts_scope.text(tag) == local:
                elements.append(node)
        elif node.type in ("identifier", "nested_identifier") and ts_scope.text(node) == local:
            parent = node.parent
            if parent is not None and parent.type in ("import_specifier",):
                continue
            if parent is not None and parent.type in ("jsx_opening_element", "jsx_closing_element"):
                names += 1
                continue
            return None
    if names != 2 * len(elements):
        return None
    if any(o is not e and ts_gates._contains(o, e) for o in elements for e in elements):
        return None
    return sorted(elements, key=lambda n: n.start_byte)


def _attributes(element: Node) -> list[Node]:
    open_tag = element.child_by_field_name("open_tag")
    return [c for c in (open_tag.named_children if open_tag is not None else []) if c.type in ("jsx_attribute", "jsx_expression")]


def _flags_value(attribute: Node) -> Node | None:
    """The object literal of ``flags={{…}}``."""
    if attribute.type != "jsx_attribute":
        return None
    name = attribute.named_children[0] if attribute.named_children else None
    if name is None or ts_scope.text(name) != "flags":
        return None
    value = attribute.named_children[1] if len(attribute.named_children) > 1 else None
    if value is None or value.type != "jsx_expression" or len(value.named_children) != 1:
        return None
    literal = value.named_children[0]
    return literal if literal.type == "object" else None


def _overrides(element: Node, data: bytes, flag_key: str) -> bool:
    """Whether ``element`` gave ``flag_key`` a value in its ``flags``."""
    for attribute in _attributes(element):
        literal = _flags_value(attribute)
        if literal is None:
            continue
        for pair in literal.named_children:
            key = pair.child_by_field_name("key") if pair.type == "pair" else None
            if key is not None and (_specifier(key) == flag_key or ts_scope.text(key) == flag_key):
                return True
    return False


def _emptied(element: Node, data: bytes) -> bool:
    """Whether ``element`` carries nothing but an empty ``flags={{}}``, or nothing."""
    for attribute in _attributes(element):
        literal = _flags_value(attribute)
        if literal is None or [c for c in literal.named_children if c.type != "comment"]:
            return False
    return True


# --- The proof: nothing the file loads reaches a Featureflip module ----------


def _prove(
    root: Node, data: bytes, path: str, repo_dir: str, own: Node | None, hit: re.Pattern[str] | None = None
) -> None:
    """Raise :class:`_Unproven` unless no module the test file loads, other
    than the provider's own import statement ``own``, reaches a Featureflip
    module, followed through every module it loads in turn."""
    text = data.decode("utf-8")
    skip = (own.start_byte, own.end_byte) if own is not None else None
    outside = text if skip is None else text[: _chars(data, skip[0])] + text[_chars(data, skip[1]) :]
    if (hit.search(outside) if hit is not None else "@featureflip/" in outside):
        raise _Unproven  # a mock, an `importOriginal`, a second import
    seen: set[str] = set()
    pending = [(path, s) for s in _loads(root, skip)]
    while pending:
        reader, specifier = pending.pop()
        target = _module(reader, specifier, repo_dir, path)
        if target is None or target in seen:
            continue
        seen.add(target)
        if len(seen) > _BUDGET:
            raise _Unproven
        source = Path(target).read_text(encoding="utf-8")
        if hit.search(source) if hit is not None else "@featureflip/" in source:
            raise _Unproven
        module = ts_scope.parse(source, target)
        if module.has_error:
            raise _Unproven
        pending.extend((target, s) for s in _loads(module, None))


#: Every module that is, or wraps, a flag client: the audited Featureflip SDKs
#: and the clients a project may call through instead.
FLAG_CLIENTS = re.compile(
    r"@featureflip/|openfeature|launchdarkly|flagsmith|unleash|growthbook|statsig|optimizely|posthog|split\.?io",
    re.IGNORECASE,
)


def loads_no_flag_client(path: str, source: str, repo_dir: str) -> bool:
    """Whether the file ``path`` (as ``source``) provably loads no flag-client
    module, followed through everything it loads in turn, by the same proof a
    test provider's unwrapping uses. A module that cannot be resolved, read or
    parsed, a load not spelled as a string, or a budget overrun is a ``False``."""
    try:
        root = ts_scope.parse(source, path)
        if root.has_error:
            return False
        _prove(root, source.encode("utf-8"), path, repo_dir, None, FLAG_CLIENTS)
    except (_Unproven, OSError, RecursionError, UnicodeDecodeError, ValueError):
        return False
    return True


def _chars(data: bytes, offset: int) -> int:
    return len(data[:offset].decode("utf-8"))


def _module(reader: str, specifier: str, repo_dir: str, test: str) -> str | None:
    """The TS-family file ``specifier`` loads, ``None`` for a package or a
    file that is not code; raises when it cannot be told which.

    A bare specifier is a package when a ``package.json`` declares it from the
    registry and no compiler config can read it as a file, asked of
    ``reader`` or, for a helper no config compiles (a ``tests/`` module
    outside every ``include``), of the test file ``test``, which the same
    test run loads it for."""
    if specifier.startswith("@featureflip/"):
        raise _Unproven
    target = _resolve(reader, specifier, repo_dir)
    if target is None and not specifier.startswith((".", "/")):
        # An alias, read the way the test run that loads it reads it.
        target = _resolve(test, specifier, repo_dir)
    if target is None:
        if specifier.startswith("."):
            # A stylesheet, an image, a font or JSON, named with its extension.
            # Anything else (`.mdx`, `.vue`, `.svelte`, …) may be code.
            asset = (Path(reader).parent / specifier.split("?", 1)[0]).resolve()
            if asset.suffix.lower() in _ASSETS and asset.is_file():
                return None
            raise _Unproven
        if specifier.startswith("/") or not _PACKAGE.match(specifier):
            raise _Unproven
        if specifier.startswith("node:") or specifier.split("/")[0] in _NODE_BUILTINS:
            return None
        if not (
            cross_file_ts.names_package(reader, specifier, repo_dir)
            or cross_file_ts.names_package(test, specifier, repo_dir)
        ) or not _registry_dependency(reader, specifier, repo_dir):
            raise _Unproven
        return None
    resolved = str(Path(target).resolve())
    if resolved.endswith((".d.ts", ".d.mts", ".d.cts")):
        raise _Unproven
    if resolved.endswith(ts_scope.FAMILY_EXTENSIONS):
        return resolved
    if Path(resolved).suffix.lower() in _ASSETS:
        return None
    raise _Unproven


def _registry_dependency(reader: str, specifier: str, repo_dir: str) -> bool:
    """Whether a ``package.json`` from ``reader``'s directory up to the
    checkout root declares ``specifier``'s package with a registry range, and
    none declares it any other way (a workspace, a path, a tarball, which can
    be the repository's own code). Without one, a bare specifier may be a
    bundler alias for a file this proof never read."""
    import json

    from flag_cleanup.inert_load import _DEPENDENCY_FIELDS, _REGISTRY_RANGE
    from flag_cleanup.piranha_runner import checkout_root

    parts = specifier.removeprefix("node:").split("/")
    package = "/".join(parts[:2]) if parts[0].startswith("@") else parts[0]
    stop = Path(checkout_root(repo_dir)).resolve()
    declared = False
    for directory in Path(reader).resolve().parents:
        manifest = directory / "package.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except ValueError:
                return False
            for field in _DEPENDENCY_FIELDS:
                dependencies = data.get(field) if isinstance(data, dict) else None
                if isinstance(dependencies, dict) and package in dependencies:
                    spec = dependencies[package]
                    if not isinstance(spec, str) or not _REGISTRY_RANGE.match(spec):
                        return False
                    declared = True
        if directory == stop or not directory.is_relative_to(stop):
            break
    return declared


_MOCK_METHODS = frozenset({"mock", "doMock", "unstable_mockModule"})
_ACTUAL_METHODS = frozenset({"importActual", "requireActual", "importMock", "requireMock"})


def _loads(root: Node, skip: tuple[int, int] | None) -> list[str]:
    """Every specifier ``root`` loads: value imports, re-exports, dynamic
    ``import()`` and ``require()``; raises on one that is not a string."""
    found: list[str] = []
    for node in cross_file_keys.walk(root):
        if skip is not None and skip[0] <= node.start_byte and node.end_byte <= skip[1]:
            continue
        if node.type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
            if source is None and node.type == "import_statement":
                # `import Mod = require('./m')` keeps its source on the clause.
                clause = next((c for c in node.named_children if c.type == "import_require_clause"), None)
                source = clause.child_by_field_name("source") if clause is not None else None
                if source is None:
                    raise _Unproven
            if source is None:
                continue
            if any(c.type == "type" for c in node.children):
                continue
            specifier = _specifier(source)
            if specifier is None:
                raise _Unproven
            found.append(specifier)
        elif node.type == "call_expression":
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            named = [] if arguments is None else [c for c in arguments.named_children if c.type != "comment"]
            method = callee.child_by_field_name("property") if callee is not None and callee.type == "member_expression" else None
            method_name = ts_scope.text(method) if method is not None else None
            if method_name in _MOCK_METHODS and named and _specifier(named[0]) is not None:
                if len(named) < 2:
                    # An automock, which can render a `__mocks__` file this
                    # proof never read.
                    raise _Unproven
                continue
            if method_name in _ACTUAL_METHODS:
                specifier = _specifier(named[0]) if named else None
                if specifier is None:
                    raise _Unproven
                found.append(specifier)
                continue
            if callee is None or not (
                callee.type == "import" or (callee.type == "identifier" and ts_scope.text(callee) == "require")
            ):
                continue
            specifier = _specifier(named[0]) if len(named) == 1 else None
            if specifier is None:
                raise _Unproven
            found.append(specifier)
    return found
