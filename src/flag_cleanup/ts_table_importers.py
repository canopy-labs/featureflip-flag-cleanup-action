"""Who can load an exported flag table, and does each of them read it by a literal key?

:mod:`flag_cleanup.ts_table_reads` proves a table's own file. An EXPORTED table
is also read from other modules, and a read there by a runtime key
(``tables[name]``) would change when the entry goes. This module decides
whether every module that can load the declaring file is RESOLVED and reads the
table by literals only. Anything unresolved or unexplained holds.

* A static import, re-export, ``require('lit')`` or ``import('lit')`` anywhere
  in the checkout (TS/JS files and the script blocks of Vue, Svelte, Astro and
  MDX files) is resolved with the checkout's own resolver
  (:func:`flag_cleanup.cross_file_ts._resolve`: relative paths, tsconfig
  ``paths``, workspace packages). One that resolves to the declaring file, or to
  a barrel that re-exports it, is an importer. One that resolves to nothing is
  harmless only when it is provably an external package
  (:func:`~flag_cleanup.cross_file_ts.package_outside`: no config can alias it,
  no workspace names it) or a relative path that names no module of the table;
  otherwise it holds.
* A dynamic load (``import(expr)``, ``require(expr)``, ``import.meta.glob``,
  ``require.context``, ``createRequire``) anywhere in the checkout holds.
* In an importer, a named import must be read as ``N.lit``, ``N['lit']`` or
  ``N[CONST]``; a namespace import ``NS`` only as ``NS.NAME.lit`` (and
  ``NS.other``); everything else holds, and so does ``eval``, ``Function`` or
  ``with`` in the importer.
* Any other file that is not TS/JS, a component, or inert prose and that names
  the declaring module's path or package holds.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_ts, source_index, ts_flag_maps, ts_syntax, ts_table_reads

_TRANSPARENT = frozenset(
    {"parenthesized_expression", "as_expression", "non_null_expression", "satisfies_expression", "type_assertion"}
)
_OBJECT_MEMBERS = frozenset(
    {
        "valueOf", "toString", "toLocaleString", "hasOwnProperty", "isPrototypeOf", "propertyIsEnumerable",
        "constructor", "__proto__", "__defineGetter__", "__defineSetter__", "__lookupGetter__", "__lookupSetter__",
    }
)
_SCRIPT_SUFFIXES = (".ts", ".mts", ".cts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_INERT = frozenset({".md", ".markdown", ".rst", ".txt", ".lock", ".log"})
_DYNAMIC_NAMES = ("import.meta.glob", "import.meta.globEager", "require.context")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _outer(node: Node) -> Node:
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        node = node.parent
    return node


def _string_value(node: Node | None) -> str | None:
    if node is None:
        return None
    while node.type in _TRANSPARENT and node.named_children:
        node = node.named_children[0]
    if any(c.type == "escape_sequence" for c in node.named_children):
        return None  # a string spelled with an escape is not the text it shows
    if node.type == "string":
        return "".join(_text(c) for c in node.named_children if c.type == "string_fragment")
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.named_children):
        return _text(node).strip("`")
    return None


class _Unreadable:
    """A module specifier spelled with an escape or a substitution
    (``'.\\x2fflags'``): not the text it shows, so where it loads from cannot
    be read. :func:`_specs` and :func:`_export_form` hand it on in place of the
    specifier, and every caller holds on it, so no check depends on another
    having run first."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNREADABLE"


UNREADABLE = _Unreadable()
_UNREADABLE_REASON = "a module specifier spelled with an escape or a substitution"


def _real(path: str) -> str:
    return os.path.realpath(path)


@dataclass
class _Unit:
    """One body of code: a TS/JS file, or one script block of a component."""

    path: str
    text: str  # the whole file, for the mention scan
    root: Node
    shift: int  # byte offset of the code within ``text``
    whole: bool  # the code IS the file (not a block)


def _units(repo_dir: str, files: dict[str, tuple[str, Node]]) -> tuple[list[_Unit], str | None]:
    """Every body of code in the checkout, or ``(units, reason)`` when a component's code cannot be told apart."""
    units = [_Unit(path, source, root, 0, True) for path, (source, root) in files.items()]
    for listed in source_index.files(repo_dir, source_index.COMPONENT_SUFFIXES):
        location = listed.path if listed.path.is_absolute() else Path(repo_dir) / listed.path
        try:
            text = location.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return units, f"{location} cannot be read"
        regions = cross_file_ts._script_regions(str(location), text)
        outside = text
        if regions is not None:
            pieces, at = [], 0
            for tag, _start, end in regions:
                pieces.append(text[at:tag])
                at = end
            pieces.append(text[at:])
            outside = "".join(pieces)
        if _LOAD_CALL.search(outside):
            return units, f"the markup of {location} loads modules (`import(`, `require(`, `import.meta` or another loader)"
        if regions is None:
            if re.search(r"\bimport\b|\brequire\b", text):
                return units, f"the code of {location} cannot be told from its markup"
            continue
        for _tag, start, end in regions:
            code = text[start:end]
            if code.strip():
                units.append(_Unit(str(location), text, ts_syntax._parse(code, "ts"), len(text[:start].encode("utf-8")), False))
    return units, None


def hold_reason(
    repo_dir: str,
    files: dict[str, tuple[str, Node]],
    declaring: str,
    names: set[str],
) -> str | None:
    """Why the exported table ``names`` of ``declaring`` cannot be proven read only by literals, else ``None``."""
    stop = cross_file_ts._stop_for(repo_dir)
    units, why = _units(repo_dir, files)
    if why is not None:
        return why
    resolved: dict[tuple[str, str], str | None] = {}

    def resolve(path: str, spec: str) -> str | None:
        key = (path, spec)
        if key not in resolved:
            found = cross_file_ts._resolve(path, spec, stop, workspace=True)
            resolved[key] = _real(found) if found else None
        return resolved[key]

    carrying: dict[str, set[str]] = {_real(declaring): set(names)}
    stems = _stems(declaring)
    # Dynamic loads, anywhere.
    for unit in units:
        why = _dynamic_hazard(unit)
        if why is not None:
            return f"{why} in {unit.path}"
    # Re-exports, to a fixpoint: the files that hand the table on, and its names there.
    changed = True
    while changed:
        changed = False
        for unit in units:
            here = _real(unit.path)
            for node in ts_syntax._walk(unit.root):
                kind = _export_form(node)
                if kind is None:
                    continue
                spec, items = kind
                if spec is UNREADABLE:
                    return f"{_UNREADABLE_REASON} in {unit.path}"
                source_module = resolve(unit.path, spec) if spec is not None else None
                if spec is not None:
                    if source_module not in carrying:
                        continue
                    wanted = carrying[source_module]
                    if any(c.type == "namespace_export" for c in node.named_children):
                        return f"`export * as` in {unit.path} hands the module on as a namespace"
                    for imported, exported in items:
                        if imported == "*":
                            new = set(wanted)
                        elif imported in wanted:
                            new = {exported}
                        else:
                            continue
                        if not new <= carrying.setdefault(here, set()):
                            carrying[here] |= new
                            changed = True
                else:
                    # `export { L as A }` of a name this file imported from a carrying module.
                    for imported, exported in items:
                        origin = _imported_from(unit, imported, resolve, carrying)
                        if origin and exported not in carrying.setdefault(here, set()):
                            carrying[here].add(exported)
                            changed = True
    carrying = {k: v for k, v in carrying.items() if v}
    for module in carrying:
        stems |= _stems(module)
    for unit in units:
        why = _unit_reason(unit, repo_dir, resolve, carrying, stems, declaring)
        if why is not None:
            return why
    return _other_files(repo_dir, declaring, stems)


def _stems(path: str) -> set[str]:
    p = Path(path)
    return {p.stem}


def _export_form(node: Node) -> tuple[str | _Unreadable | None, list[tuple[str, str]]] | None:
    """``(source or None, [(imported, exported)])`` for an ``export`` statement
    of names, and ``(UNREADABLE, [])`` for any ``export … from`` whose source
    cannot be read."""
    if node.type != "export_statement":
        return None
    source = node.child_by_field_name("source")
    spec = _string_value(source) if source is not None else None
    if source is not None and spec is None:
        return UNREADABLE, []
    items: list[tuple[str, str]] = []
    star = any(c.type in ("*", "namespace_export") for c in node.children)
    if star:
        return spec, [("*", "*")]
    for clause in node.named_children:
        if clause.type == "export_clause":
            for part in clause.named_children:
                if part.type != "export_specifier":
                    continue
                inner, alias = part.child_by_field_name("name"), part.child_by_field_name("alias")
                if inner is not None:
                    items.append((_text(inner), _text(alias) if alias is not None else _text(inner)))
    if not items:
        return None
    return spec, items


def _imported_from(unit: _Unit, local: str, resolve, carrying: dict[str, set[str]]) -> bool:
    """Whether ``local`` is a named import in ``unit`` of a name a carrying module exports."""
    for node in ts_syntax._walk(unit.root):
        if node.type != "import_statement":
            continue
        source = node.child_by_field_name("source")
        if source is None:
            continue  # `import x = require(...)`: read by `_specs`
        spec = _string_value(source)
        if spec is None:
            return True  # unreadable: it may be a carrying module
        module = resolve(unit.path, spec)
        if module not in carrying:
            continue
        for part in ts_syntax._walk(node):
            if part.type == "import_specifier":
                inner, alias = part.child_by_field_name("name"), part.child_by_field_name("alias")
                if inner is not None and _text(inner) in carrying[module] and _text(alias or inner) == local:
                    return True
    return False


_META_ALLOWED = frozenset({"env", "url", "hot", "dirname", "filename"})
_NAME_NODES = frozenset({"identifier", "property_identifier", "shorthand_property_identifier"})


def _dynamic_hazard(unit: _Unit) -> str | None:
    """A way to load a module this cannot resolve, found by token.

    Held: any import of ``module`` or ``node:module``; the name ``require``
    (identifier or property) anywhere but as the callee of ``require('lit')``;
    the names ``createRequire``, ``System`` and ``__non_webpack_require__``; the
    identifier ``module`` anywhere but as the object of ``module.exports``;
    a string subscript spelling any of those (``module['require']``);
    ``import()`` of anything but one string; and ``import.meta`` members outside
    ``env``, ``url``, ``hot``, ``dirname`` and ``filename``.
    """
    for node in ts_syntax._walk(unit.root):
        if node.type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
            if source is not None and _string_value(source) is None:
                return "a module specifier spelled with an escape or computed"
            for child in node.named_children:
                if child.type == "import_require_clause":
                    for part in child.named_children:
                        if part.type == "string" and _string_value(part) is None:
                            return "a module specifier spelled with an escape"
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is not None and (_text(function) == "require" or function.type == "import") and arguments is not None:
                first = arguments.named_children[0] if arguments.named_children else None
                if first is not None and first.type in ("string", "template_string") and _string_value(first) is None:
                    return "a module specifier spelled with an escape or substitution"
        if node.type in _NAME_NODES and node.child_count == 0:
            word = _text(node)
            if word == "require":
                if node.type != "identifier" or not _plain_require(node):
                    return "a use of `require` other than a call with one string"
            elif word in ("createRequire", "System", "__non_webpack_require__"):
                return f"`{word}`"
            elif word == "module" and node.type == "identifier" and not _module_exports(node):
                return "`module` used other than as `module.exports`"
        elif node.type == "subscript_expression":
            key = _string_value(node.child_by_field_name("index"))
            if key in ("require", "createRequire", "System", "module", "__non_webpack_require__"):
                return f"a `[\"{key}\"]` subscript"
        elif node.type == "meta_property":
            parent = node.parent
            member = None
            if parent is not None and parent.type == "member_expression" and parent.child_by_field_name("object") == node:
                prop = parent.child_by_field_name("property")
                member = _text(prop) if prop is not None else None
            if member not in _META_ALLOWED:
                return "an `import.meta` member that can load modules"
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is not None and function.type == "import":
                args = arguments.named_children if arguments is not None else []
                if len(args) != 1 or _string_value(args[0]) is None:
                    return "an `import()` that is not a single string"
    for _node, spec in _specs(unit):
        if spec is UNREADABLE:
            return _UNREADABLE_REASON
        if spec in ("module", "node:module"):
            return "a load of `module` (`import`, `export`, `require` or `import()`)"
    return None


def _module_exports(identifier: Node) -> bool:
    """``module`` as the object of ``module.exports``."""
    parent = identifier.parent
    if parent is None or parent.type != "member_expression" or parent.child_by_field_name("object") != identifier:
        return False
    prop = parent.child_by_field_name("property")
    return prop is not None and _text(prop) == "exports"


def _plain_require(identifier: Node) -> bool:
    """``require`` as the callee of a call whose only argument is a string literal."""
    parent = identifier.parent
    if parent is None or parent.type != "call_expression" or parent.child_by_field_name("function") != identifier:
        return False
    arguments = parent.child_by_field_name("arguments")
    args = arguments.named_children if arguments is not None else []
    return len(args) == 1 and _string_value(args[0]) is not None


#: Explicit extensions of files that are not scripts: a relative import of one
#: cannot name the table's module.
_ASSET_SUFFIXES = frozenset(
    {
        ".css", ".scss", ".sass", ".less", ".styl", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
        ".json", ".md", ".txt", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".webm", ".wasm",
        ".html", ".yaml", ".yml", ".graphql", ".gql", ".csv",
    }
)


def _provably_elsewhere(path: str, spec: str, repo_dir: str) -> bool:
    """A specifier that resolves to no file is harmless only when it provably names no module of the table.

    A relative path the resolver cannot map holds (a React Native ``.ios`` file,
    a directory ``package.json`` ``main`` and the like), unless it names a file
    with an explicit non-script extension. A bare name must be declared with a
    version in a ``package.json`` above the reader and be unaliasable by any
    config this reads.
    """
    if spec.startswith((".", "/")):
        return Path(spec.split("?", 1)[0]).suffix.lower() in _ASSET_SUFFIXES
    return cross_file_ts.package_outside(path, spec, repo_dir, declared_only=True)


def _specs(unit: _Unit) -> Iterator[tuple[Node, str | _Unreadable]]:
    """Every module specifier of the unit, with its node: the static ones
    (``import``, ``export … from``, ``import x = require``) and the first
    argument of each ``require(…)`` / ``import(…)``. One that is not a plain
    string is :data:`UNREADABLE`, never skipped."""
    for node in ts_syntax._walk(unit.root):
        if node.type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
            if source is not None:
                spec = _string_value(source)
                yield node, UNREADABLE if spec is None else spec
            for child in node.named_children:
                if child.type == "import_require_clause":
                    for part in child.named_children:
                        if part.type == "string":
                            value = _string_value(part)
                            yield node, UNREADABLE if value is None else value
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is not None and (_text(function) == "require" or function.type == "import") and arguments is not None:
                first = next((c for c in arguments.named_children if c.type != "comment"), None)
                if first is None:
                    continue  # `require()` loads nothing
                value = _string_value(first)
                yield node, UNREADABLE if value is None else value


def _unit_reason(unit: _Unit, repo_dir: str, resolve, carrying: dict[str, set[str]], stems: set[str], declaring: str) -> str | None:
    """Why this unit's use of the table is not provably literal, else ``None``."""
    locals_: set[str] = set()
    namespaces: dict[str, set[str]] = {}
    binders: set[int] = set()
    for node, spec in _specs(unit):
        if spec is UNREADABLE:
            return f"{_UNREADABLE_REASON} in {unit.path}"
        module = resolve(unit.path, spec)
        if module is None:
            if not _provably_elsewhere(unit.path, spec, repo_dir):
                return f"`{spec}` in {unit.path} cannot be resolved and is not provably an external package"
            continue
        if module not in carrying:
            continue
        wanted = carrying[module]
        if node.type == "call_expression":
            declarator = node.parent
            while declarator is not None and declarator.type in ("await_expression", *_TRANSPARENT):
                declarator = declarator.parent
            target = declarator.child_by_field_name("name") if declarator is not None and declarator.type == "variable_declarator" else None
            if target is None:
                return f"the module is loaded by `{_text(node)[:40]}` in {unit.path}, whose result is not bound to a name"
            if target.type == "identifier":
                namespaces[_text(target)] = wanted
                binders.add(target.id)
            elif target.type == "object_pattern":
                for part in target.named_children:
                    if part.type == "shorthand_property_identifier_pattern" and _text(part) in wanted:
                        locals_.add(_text(part))
                        binders.add(part.id)
                    elif part.type == "pair_pattern":
                        key, value = part.child_by_field_name("key"), part.child_by_field_name("value")
                        if key is not None and _text(key) in wanted:
                            if value is None or value.type != "identifier":
                                return f"a destructuring of the module in {unit.path} cannot be followed"
                            locals_.add(_text(value))
                            binders.add(value.id)
                    elif part.type in ("rest_pattern", "object_assignment_pattern"):
                        return f"the module is destructured with a rest in {unit.path}"
            else:
                return f"a destructuring of the module in {unit.path} cannot be followed"
            continue
        if node.type != "import_statement":
            continue  # a re-export: followed by the fixpoint
        for clause in node.named_children:
            if clause.type == "import_require_clause":
                ident = next((c for c in clause.named_children if c.type == "identifier"), None)
                if ident is not None:
                    namespaces[_text(ident)] = wanted
                    binders.add(ident.id)
                continue
            if clause.type != "import_clause":
                continue
            for item in clause.named_children:
                if item.type == "identifier":
                    # A default import: the table only if the module hands it on as `default`.
                    if "default" in wanted:
                        locals_.add(_text(item))
                        binders.add(item.id)
                    continue
                if item.type not in ("namespace_import", "named_imports"):
                    return f"an import clause in {unit.path} that is not understood"
                if item.type == "namespace_import":
                    ident = next((c for c in item.named_children if c.type == "identifier"), None)
                    if ident is not None:
                        namespaces[_text(ident)] = wanted
                        binders.add(ident.id)
                elif item.type == "named_imports":
                    for part in item.named_children:
                        if part.type != "import_specifier":
                            continue
                        inner, alias = part.child_by_field_name("name"), part.child_by_field_name("alias")
                        if inner is not None and _text(inner) in wanted:
                            local = alias or inner
                            locals_.add(_text(local))
                            binders.add(local.id)
                            binders.add(inner.id)
    if not locals_ and not namespaces:
        return None
    for node in ts_syntax._walk(unit.root):
        if node.type == "with_statement":
            return f"`with` in {unit.path}"
        if node.type == "identifier" and node.child_count == 0 and _text(node) in ("eval", "Function"):
            return f"`eval` or `Function` in {unit.path}"
    every = locals_ | set(namespaces)
    pattern = re.compile("(?<![\\w$])(?:" + "|".join(re.escape(n) for n in sorted(every, key=len, reverse=True)) + ")(?![\\w$])")
    source = unit.text
    matches = {len(source[: m.start()].encode("utf-8")) for m in pattern.finditer(source)}
    explained = _explained(unit, locals_, namespaces, binders, carrying.get(_real(unit.path), set()))
    ranges = _specifier_ranges(unit)
    if {m for m in matches - explained if not any(a <= m < b for a, b in ranges)}:
        return f"the table is read in {unit.path} in a way that cannot be followed"
    return None


def _explained(unit: _Unit, locals_: set[str], namespaces: dict[str, set[str]], binders: set[int], handed_on: set[str]) -> set[int]:
    explained: set[int] = set()
    constants = ts_table_reads.string_constants(unit.root)
    for node in ts_syntax._walk(unit.root):
        if node.type != "identifier" or node.child_count != 0:
            continue
        name = _text(node)
        if name not in locals_ and name not in namespaces:
            continue
        start = node.start_byte + unit.shift
        if node.id in binders:
            explained.add(start)
            continue
        parent = node.parent
        if parent is not None and parent.type == "export_specifier" and parent.child_by_field_name("name") == node:
            alias = parent.child_by_field_name("alias")
            exported = _text(alias) if alias is not None else name
            if name in locals_ and exported in handed_on:
                explained.add(start)  # a re-export the fixpoint followed
            continue
        if parent is not None and parent.type == "import_specifier":
            explained.add(start)
            continue
        outer = _outer(node)
        holder = outer.parent
        if holder is None:
            continue
        if name in locals_:
            if _literal_use(outer, holder, constants):
                explained.add(start)
            continue
        wanted = namespaces[name]
        if holder.type == "member_expression" and holder.child_by_field_name("object") == outer:
            prop = holder.child_by_field_name("property")
            if prop is None or prop.type != "property_identifier":
                continue
            member = _text(prop)
            if member in _OBJECT_MEMBERS:
                continue
            if member not in wanted:
                explained.add(start)
                continue
            top = _outer(holder)
            above = top.parent
            if above is not None and _literal_use(top, above, constants):
                explained.add(start)
    return explained


def _literal_use(outer: Node, holder: Node, constants: dict[str, str]) -> bool:
    """``outer`` is read as ``outer.lit`` / ``outer['lit']`` / ``outer[CONST]`` by ``holder``."""
    if holder.type == "member_expression" and holder.child_by_field_name("object") == outer:
        prop = holder.child_by_field_name("property")
        return prop is not None and prop.type == "property_identifier" and _text(prop) not in _OBJECT_MEMBERS
    if holder.type == "subscript_expression" and holder.child_by_field_name("object") == outer:
        index = holder.child_by_field_name("index")
        key = _string_value(index)
        if key is None and index is not None and index.type == "identifier":
            key = constants.get(_text(index))
        return key is not None and key not in _OBJECT_MEMBERS
    return False


def _specifier_ranges(unit: _Unit) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for node, _spec in _specs(unit):
        for part in ts_syntax._walk(node):
            if part.type == "string" and (part.parent == node or (part.parent is not None and part.parent.type in ("import_require_clause", "arguments"))):
                ranges.append((part.start_byte + unit.shift, part.end_byte + unit.shift))
    return ranges


#: A module load in text this does not parse: component markup outside its
#: script blocks, and other text files. Inside a script these are tokens
#: (:func:`_dynamic_hazard`); here they are spellings.
_LOAD_CALL = re.compile(
    r"import\s*\(|require\s*\(|import\.meta"
    r"|\bcreateRequire\b|\b__non_webpack_require__\b"
    r"|\brequire\s*\.\s*context\b|\bmodule\s*\.\s*require\b|\bSystem\s*\.\s*import\b"
)


def _manifest_remaps(text: str, stems: set[str]) -> str | None:
    """Whether a ``package.json`` can redirect a module: an object-valued
    ``browser`` or ``react-native`` field, any ``imports`` field, or an
    ``exports`` field with a value naming one of the table module's files."""
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return "cannot be parsed"
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("browser"), dict) or isinstance(data.get("react-native"), dict):
        return "remaps modules (`browser` or `react-native`)"
    if "imports" in data:
        return "declares `imports`"

    def values(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from values(v)
        elif isinstance(node, list):
            for v in node:
                yield from values(v)
        elif isinstance(node, str):
            yield node

    for value in values(data.get("exports")):
        leaf = value.rstrip("/").rsplit("/", 1)[-1]
        if Path(leaf).stem in stems:
            return "exports a file of the module"
    return None


_INERT_NAMES = frozenset({"CHANGELOG", "LICENSE", "LICENCE", "COPYING"})
_MANIFESTS = frozenset({"package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "pnpm-workspace.yaml"})


def _other_files(repo_dir: str, declaring: str, stems: set[str]) -> str | None:
    """A text file the parser did not read that names the module's path or package.

    Every file git lists (tracked or not ignored) is read, whatever its
    extension, except binary files, prose (``.md``, ``.rst``) and the usual
    CHANGELOG/LICENSE files. Code in the TS/JS family and component script
    blocks was judged as code.
    """
    root = Path(source_index.checkout_root(repo_dir)).resolve()
    declared = Path(declaring).resolve()
    package = _package_name(declared, root)
    needles = [re.compile(r"""(?:['"`/])(?:%s)(?:\.\w+)?(?:['"`?#]|$|\s)""" % "|".join(re.escape(s) for s in sorted(stems)))]
    named = re.compile("['\"`]" + re.escape(package) + "(?:/[^'\"`]*)?['\"`]") if package else None
    scripts = set(_SCRIPT_SUFFIXES) | set(source_index.COMPONENT_SUFFIXES)
    for name in ts_flag_maps.listing(str(root)):
        location = Path(name)
        suffix = location.suffix.lower()
        if suffix in scripts or suffix in _INERT or location.stem.upper() in _INERT_NAMES:
            continue
        try:
            data = location.read_bytes()
        except OSError:
            return f"{location} cannot be read"
        if b"\0" in data[:8192]:
            continue  # binary
        text = data.decode("utf-8", "replace")
        if location.name == "package.json":
            why = _manifest_remaps(text, stems)
            if why is not None:
                return f"{location} {why}"
            continue
        if location.name in _MANIFESTS:
            continue  # lockfiles and workspace lists
        if _LOAD_CALL.search(text):
            return f"{location}, a file the parser does not read, loads modules (`import(`, `require(`, `import.meta` or another loader)"
        if any(n.search(text) for n in needles):
            return f"{location}, a file the parser does not read, names the module"
        if named is not None and named.search(text):
            return f"{location}, a file the parser does not read, names the package"
    return None


def _package_name(path: Path, root: Path) -> str | None:
    import json

    directory = path.parent
    while True:
        manifest = directory / "package.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                return None
            return data.get("name") if isinstance(data, dict) and isinstance(data.get("name"), str) else None
        if directory == root or directory.parent == directory:
            return None
        directory = directory.parent
