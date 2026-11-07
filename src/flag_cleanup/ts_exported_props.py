"""Remove a prop the fold made one literal at every element of an EXPORTED component.

:mod:`flag_cleanup.ts_constant_props` removes such a prop when the component
is private to its file, so every element is in front of it. An exported
component is rendered from other files, so the same edit needs every one of
them, and the proof is by NAME across the checkout, as
:mod:`flag_cleanup.ctor_param_ts` proves a class's callers: every spelling of
the component's name in the repository's TypeScript and JavaScript must be one
of a short list of shapes, and anything else refuses.

Allowed spellings:

* in the declaring file: the declaration (``export function Row``,
  ``export const Row = memo(function Row …)``, or an unexported declaration
  with one ``export { Row }``), and JSX element names;
* in any other file: one ``import { Row } from "<the declaring file>"``,
  without ``as``, and JSX element names.

So a re-export (``export { Row } from``), a default export, a namespace
import (``M.Row``), ``typeof Row``, ``Row.displayName`` or a mention in an
ignored file, a ``.vue``/``.svelte``/``.astro``/``.mdx`` or HTML file refuses:
each is a way to render the component this pass cannot see.

A module can also reach the component WITHOUT spelling its name:
``import * as M`` then ``M[name]`` or ``Object.values(M)``, a default import,
``import()``/``require()``, ``export * as``, or any of these through a barrel
that ``export *``s the declaring file. So every specifier in the checkout that
resolves to the declaring file or to such a barrel must be a plain import of
names, a side-effect import, a named re-export, an ``export *`` (its module
then counts as a barrel), or a test runner's ``mock(…)``; anything else
refuses, and so does a bundler glob anywhere, or a computed
``import()``/``require()`` in a file that could load the component's package:
the same package, one that depends on it by name, or a file in no package. Specifiers the resolver cannot follow (a package's self-reference,
``package.json`` ``imports``) are not seen.

Every element across those files then has to pass the prop once as the same
literal, with no spread, and the output has to pass it at more elements than
the input did (only a literal the transform wrote counts, as in the
single-file pass). The declaring side is edited exactly as
:mod:`flag_cleanup.ts_constant_props` edits it (the parameter, the props type
it owns, a marker at each read), and each other file loses the attribute.
Every file edited must be one this run's engine processes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_ts, source_index, ts_scope
from flag_cleanup.ctor_param_ts import _extensions, markup_files
from flag_cleanup.ts_constant_props import (
    _ELEMENT_NAMES,
    _attributes,
    _elements,
    _is_module,
    _item_span,
    _literal_of,
    _passes,
    _plan,
    _text,
    _unwrap,
)
from flag_cleanup.ts_syntax import _parse, _walk

_NAMES = frozenset(
    {
        "identifier",
        "type_identifier",
        "property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
    }
)
_JSX = ("tsx", "js")

Edit = tuple[int, int, bytes]


@dataclass(eq=False)
class _File:
    """One file the proof read: its path as the caller spells it, the
    customer's text, the current text and its tree."""

    path: str
    original: str
    current: str
    root: Node
    data: bytes


def inline_exported_props(
    files: Mapping[str, tuple[str, str]],
    language: str,
    marker: str,
    repo_dir: str,
    in_run: Callable[[str], bool],
) -> dict[str, tuple[str, str]]:
    """The files to rewrite, as ``path -> (previous, rewritten)``, removing
    one prop of one exported component, or ``{}``.

    ``files`` maps each file of the run to ``(customer's text, current
    text)``; a file outside it is read from disk, where it is still the
    customer's. ``in_run(path)`` says whether the engine processes ``path``
    in this run.
    """
    if language not in _JSX:
        return {}
    by_resolved = {str(Path(p).resolve()): p for p in files}
    for path, (original, current) in sorted(files.items()):
        if original == current or not in_run(path):
            continue
        for component in _candidates(original, current, ts_scope.grammar(path)):
            result = _remove(component, path, files, by_resolved, marker, repo_dir, in_run)
            if result:
                return result
    return {}


def _candidates(original: str, current: str, grammar: str) -> list[str]:
    """Components with an element passing a literal the output passes more
    often than the input did, in this file."""
    if grammar not in _JSX:
        return []
    root = _parse(current, grammar)
    old_root = _parse(original, grammar)
    if root.has_error or old_root.has_error:
        return []
    data, old_data = current.encode("utf-8"), original.encode("utf-8")
    old = _elements(old_root, old_data)
    found = []
    for component, elements in _elements(root, data).items():
        for literal in ("true", "false"):
            props = {
                _text(a.named_children[0], data)
                for e in elements
                for a in e.node.named_children
                if a.type == "jsx_attribute" and a.named_children[0].type == "property_identifier"
            }
            if any(
                sum(_passes(e, p, literal, data) for e in elements)
                > sum(_passes(e, p, literal, old_data) for e in old.get(component, []))
                for p in props
            ):
                found.append(component)
                break
    return found


def _read(path: str, files, by_resolved) -> tuple[str, str, str] | None:
    """``(path as spelled, customer's text, current text)``."""
    spelled = by_resolved.get(str(Path(path).resolve()))
    if spelled is not None:
        return spelled, *files[spelled]
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return path, text, text


def _remove(component, reader, files, by_resolved, marker, repo_dir, in_run):
    decl_path = _declaring_file(component, reader, files, by_resolved, repo_dir)
    if decl_path is None:
        return {}
    found = _read(decl_path, files, by_resolved)
    if found is None:
        return {}
    spelled, original, current = found
    grammar = ts_scope.grammar(spelled)
    if grammar not in _JSX or not in_run(spelled):
        return {}
    root = _parse(current, grammar)
    if root.has_error or not _is_module(root):
        return {}
    data = current.encode("utf-8")
    declaration = _exported_function(root, data, component)
    if declaration is None:
        return {}
    function, allowed = declaration
    decl = _File(spelled, original, current, root, data)
    proven = _users(component, decl, allowed, files, by_resolved, repo_dir, in_run)
    if proven is None:
        return {}
    elements = [(f, e) for f in proven for e in _elements(f.root, f.data).get(component, [])]
    if not elements or any(e.spreads for _, e in elements):
        return {}
    plan = _plan_prop(component, decl, function, proven, elements, marker)
    if plan is None:
        return {}
    result = {}
    for file, edits in plan.items():
        if not edits:
            continue
        data = file.data
        for start, end, text in sorted(edits, reverse=True):
            data = data[:start] + text + data[end:]
        result[file.path] = (file.current, data.decode("utf-8"))
    return result


def _plan_prop(component, decl, function, proven, elements, marker):
    """``file -> edits`` removing the first prop that qualifies, or None."""
    names: dict[str, None] = {}
    for file, element in elements:
        for a in element.node.named_children:
            if a.type == "jsx_attribute" and a.named_children[0].type == "property_identifier":
                names.setdefault(_text(a.named_children[0], file.data), None)
    for prop in names:
        literals = {_literal_of([e], prop, f.data) for f, e in elements}
        if len(literals) != 1 or None in literals:
            continue
        literal = literals.pop()
        passed = sum(_passes(e, prop, literal, f.data) for f, e in elements)
        earlier = 0
        for file in proven:
            old_root = _parse(file.original, ts_scope.grammar(file.path))
            old_data = file.original.encode("utf-8")
            earlier += sum(
                _passes(e, prop, literal, old_data)
                for e in _elements(old_root, old_data).get(component, [])
            )
        if passed <= earlier:
            continue
        own = [e for f, e in elements if f is decl]
        declared = _plan(decl.root, decl.data, component, prop, literal, own, marker, function)
        if declared is None:
            continue
        plan: dict[_File, list[Edit]] = {decl: list(declared)}
        for file, element in elements:
            if file is decl:
                continue
            span = _item_span(_attributes(element, prop, file.data)[0], file.data, separator=None)
            if span is None:
                plan = None
                break
            plan.setdefault(file, []).append((*span, b""))
        if plan is None:
            continue
        if any(_overlap(edits) for edits in plan.values()):
            continue
        return plan
    return None


def _overlap(edits: list[Edit]) -> bool:
    ordered = sorted(edits)
    return any(start < end for (_, end, _), (start, _, _) in zip(ordered, ordered[1:]))


def _declaring_file(component, reader, files, by_resolved, repo_dir) -> str | None:
    """The file ``component`` is declared in, as seen from ``reader``: the
    reader itself when it declares it at the top level, else the file its one
    unaliased named import of it resolves to."""
    _, current = files[reader]
    root = _parse(current, ts_scope.grammar(reader))
    if root.has_error:
        return None
    data = current.encode("utf-8")
    if _exported_function(root, data, component) is not None:
        return reader
    sources = [
        spec
        for n in _walk(root)
        if n.type == "import_specifier"
        and n.child_by_field_name("alias") is None
        and (name := n.child_by_field_name("name")) is not None
        and _text(name, data) == component
        and (spec := _source(n, data)) is not None
    ]
    if len(sources) != 1:
        return None
    return cross_file_ts.resolve_specifier(reader, sources[0], repo_dir)


def _source(specifier: Node, data: bytes) -> str | None:
    statement = specifier.parent
    while statement is not None and statement.type not in ("import_statement", "export_statement"):
        statement = statement.parent
    source = statement.child_by_field_name("source") if statement is not None else None
    if source is None:
        return None
    return "".join(_text(c, data) for c in source.named_children if c.type == "string_fragment")


def _exported_function(
    root: Node, data: bytes, component: str, private: bool = False
) -> tuple[Node, set[int]] | None:
    """The function an exported top-level ``component`` is, with the ids of
    the name nodes that declare and export it, or None. With ``private``, a
    declaration nothing exports qualifies too."""
    found: list[tuple[Node, Node, Node | None]] = []  # (name, function, export specifier)
    exports = [
        n
        for n in root.named_children
        if n.type == "export_statement"
        and n.child_by_field_name("source") is None
        and n.child_by_field_name("declaration") is None
    ]
    specifiers = [
        s
        for e in exports
        for s in _walk(e)
        if s.type == "export_specifier"
        and (name := s.child_by_field_name("name")) is not None
        and _text(name, data) == component
    ]
    for statement in root.named_children:
        exported = statement.type == "export_statement"
        if exported and any(c.type == "default" for c in statement.children):
            continue
        declaration = statement.child_by_field_name("declaration") if exported else statement
        if declaration is None:
            continue
        if declaration.type == "function_declaration":
            name = declaration.child_by_field_name("name")
            if name is not None and _text(name, data) == component:
                found.append((name, declaration, exported))
        elif declaration.type == "lexical_declaration":
            declarators = [d for d in declaration.named_children if d.type == "variable_declarator"]
            for declarator in declarators:
                name = declarator.child_by_field_name("name")
                if name is None or _text(name, data) != component:
                    continue
                if len(declarators) != 1 or declarator.child_by_field_name("type") is not None:
                    return None
                function = _unwrap(declarator.child_by_field_name("value"), data)
                if function is None:
                    return None
                found.append((name, function, exported))
    if len(found) != 1:
        return None
    name, function, exported = found[0]
    allowed = {name.id}
    if exported:
        if specifiers:
            return None
    elif specifiers or not private:
        if len(specifiers) != 1 or specifiers[0].child_by_field_name("alias") is not None:
            return None
        allowed.add(specifiers[0].child_by_field_name("name").id)
    if function.type == "function_expression":
        inner = function.child_by_field_name("name")
        if inner is not None:
            allowed.add(inner.id)
    if any(n.type == "identifier" and n.text in (b"eval", b"arguments") for n in _walk(function)):
        return None
    return function, allowed


def _element_name(node: Node) -> bool:
    parent = node.parent
    return (
        node.type == "identifier"
        and parent is not None
        and parent.type in _ELEMENT_NAMES
        and parent.child_by_field_name("name") == node
    )


def _users(
    component,
    decl,
    allowed,
    files,
    by_resolved,
    repo_dir,
    in_run,
    use=_element_name,
    solution=False,
    exempt=None,
    commented: list[str] | None = None,
    homonyms: list[str] | None = None,
) -> list[_File] | None:
    """Every file spelling ``component`` (the declaring one first), each
    proven to spell it only as allowed, or None. ``use`` says which other
    spelling is a use the caller can account for: a JSX element name by
    default. ``solution`` resolves imports through a solution-style
    ``tsconfig.json`` (see :func:`flag_cleanup.cross_file_ts.resolve_specifier`).
    ``exempt(file)`` names a file whose spellings need no proof because the
    caller deletes it whole; it is still returned. With ``commented``, a file
    other than the declaring one that spells ``component`` only inside
    comments is no use of it: its path is appended there instead, for the
    caller to report. With ``homonyms``, likewise a file whose spellings all
    name a same-named component that is not this one (:func:`homonym`)."""
    word = re.compile(rf"(?<![\w$]){re.escape(component)}(?![\w$])")
    decl_resolved = str(Path(decl.path).resolve())
    users = [decl]
    seen_decl = False
    sources: list[tuple[str, str]] = []
    for listed in source_index.files(repo_dir, with_ignored=True):
        if not str(listed.path).endswith(_extensions()):
            continue
        found = _read(str(listed.path), files, by_resolved)
        if found is None:
            return None
        spelled, original, current = found
        sources.append((spelled, current))
        if not word.search(current) and not word.search(original):
            continue
        if (
            commented is not None
            and listed.resolved != decl_resolved
            and only_in_comments(spelled, (original, current), component)
        ):
            commented.append(spelled)
            continue
        if (
            homonyms is not None
            and listed.resolved != decl_resolved
            and homonym(spelled, (original, current), component, decl_resolved, repo_dir, solution)
        ):
            homonyms.append(spelled)
            continue
        if listed.resolved == decl_resolved:
            if listed.ignored:
                return None
            file = decl
            seen_decl = True
        else:
            if listed.ignored or not in_run(spelled):
                return None
            root = _parse(current, ts_scope.grammar(spelled))
            if root.has_error:
                return None
            file = _File(spelled, original, current, root, current.encode("utf-8"))
        if file is not decl and exempt is not None and exempt(file):
            users.append(file)
            continue
        if not _accounted(
            component, file, file is decl, allowed, decl_resolved, repo_dir, use, solution
        ):
            return None
        if file is not decl:
            users.append(file)
    if not seen_decl:
        # The declaring file's own spellings went unchecked: outside the listing.
        return None
    markup: list[tuple[str, str]] = []
    for listed in markup_files(repo_dir):
        try:
            text = listed.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if word.search(text):
            return None
        markup.append((str(listed.path), text))
    if _reached_unspelled(decl_resolved, sources, markup, repo_dir, solution):
        return None
    return users


def only_in_comments(path: str, texts, name: str) -> bool:
    """Whether every spelling of ``name`` (as a word) in each of ``texts``,
    read as ``path``'s grammar, lies inside a comment, with at least one.

    A comment loads nothing, renders nothing and is read by no test runner,
    so such a file uses nothing of what it names. A triple-slash directive
    (``/// <reference path="./Row.tsx" />``) is no such comment: the compiler
    reads it, so a spelling there is a use. A text that does not parse
    cleanly is not judged so."""
    word = re.compile(rb"(?<![\w$])" + re.escape(name.encode("utf-8")) + rb"(?![\w$])")
    grammar = ts_scope.grammar(path)
    seen = False
    for text in texts:
        data = text.encode("utf-8")
        matches = [m.span() for m in word.finditer(data)]
        if not matches:
            continue
        root = _parse(text, grammar)
        if root.has_error:
            return False
        comments = [
            (n.start_byte, n.end_byte) for n in _walk(root) if n.type == "comment" and not is_directive(n.text)
        ]
        if not all(any(s <= start and end <= e for s, e in comments) for start, end in matches):
            return False
        seen = True
    return seen


_DIRECTIVE = re.compile(rb"\A///\s*<")


def is_directive(comment: bytes) -> bool:
    """Whether a comment's text is a triple-slash directive (``/// <reference
    …/>``, ``/// <amd-dependency …/>``), which the compiler reads."""
    return _DIRECTIVE.match(comment) is not None


#: Top-level declarations that bind their ``name`` field to a value or type
#: of the file's own.
_OWN_DECLARATIONS = frozenset({
    "function_declaration", "generator_function_declaration", "class_declaration",
    "abstract_class_declaration", "enum_declaration", "interface_declaration",
    "type_alias_declaration",
})


def _declares_own(root: Node, name: str) -> bool:
    """Whether ``root``'s top level declares ``name`` itself (not by an import)."""
    for statement in root.named_children:
        node = statement.child_by_field_name("declaration") if statement.type == "export_statement" else statement
        if node is None:
            continue
        declared = node.child_by_field_name("name") if node.type in _OWN_DECLARATIONS else None
        if declared is not None and declared.text.decode("utf-8", "replace") == name:
            return True
        if node.type in ("lexical_declaration", "variable_declaration"):
            for declarator in node.named_children:
                target = declarator.child_by_field_name("name") if declarator.type == "variable_declarator" else None
                if target is not None and target.type == "identifier" and target.text.decode("utf-8", "replace") == name:
                    return True
    return False


def homonym(path: str, texts, component: str, decl_resolved: str, repo_dir: str, solution: bool = False) -> bool:
    """Whether ``path`` spells ``component`` only for a same-named component
    of its own or of another file, never the one ``decl_resolved`` declares.

    In each of ``texts`` the file binds the name exactly once, by a top-level
    declaration of its own or by an import; every import or re-export of the
    name reads a file other than the declaring one that declares the name
    itself (a module cannot also re-export another's under a name it
    declares); and the name is never a property (``m.Row``), a shorthand or
    a string (a lookup). Every other way to reach the declaring file's
    export unspelled is refused for the whole proof by
    :func:`_reached_unspelled`."""
    from flag_cleanup import ts_tests

    word = re.compile(rf"(?<![\w$]){re.escape(component)}(?![\w$])")
    grammar = ts_scope.grammar(path)
    seen = False
    for text in texts:
        if not word.search(text):
            continue
        root = _parse(text, grammar)
        if root.has_error:
            return False
        data = text.encode("utf-8")
        titles = ts_tests.title_nodes(root, data)
        # The binding scan counts a JSX element's name as a binding of it.
        binders = [
            n
            for n in ts_scope.bindings(root, component)
            if n.parent is None
            or n.parent.type not in ("jsx_opening_element", "jsx_closing_element", "jsx_self_closing_element")
        ]
        if len(binders) != 1:
            return False
        binder = binders[0]
        if not _in_import_statement(binder):
            parent = binder.parent
            own = parent is not None and (
                (parent.type in _OWN_DECLARATIONS and parent.child_by_field_name("name") == binder)
                or (parent.type == "variable_declarator" and parent.child_by_field_name("name") == binder)
            )
            if not own:
                return False
        for node in _walk(root):
            if node.type == "comment":
                if is_directive(node.text) and word.search(_text(node, data)):
                    return False
                continue
            if node.type in ("string_fragment", "template_string") and word.search(_text(node, data)):
                # Only a test's title, which nothing looks up, or a module
                # path to another file: `'./Row'` of its own import, or
                # `vi.mock('./Row')` of its own module.
                if node.type == "string_fragment" and node.parent is not None and node.parent.id in titles:
                    continue
                value = cross_file_ts._string_value(node.parent) if node.type == "string_fragment" else None
                target = (
                    cross_file_ts.resolve_specifier(path, value, repo_dir, solution=solution) if value else None
                )
                if target is None or str(Path(target).resolve()) == decl_resolved:
                    return False
                continue
            if node.type not in _NAMES or _text(node, data) != component:
                continue
            if node.type in ("property_identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"):
                return False
            parent = node.parent
            if parent is not None and parent.type in ("import_specifier", "export_specifier"):
                spec = _source(parent, data)
                if spec is None:
                    if parent.type == "import_specifier":
                        return False
                    continue  # `export { Row }`: the file's own binding
                imported = _text(parent.child_by_field_name("name"), data)
                target = cross_file_ts.resolve_specifier(path, spec, repo_dir, solution=solution)
                if target is None or str(Path(target).resolve()) == decl_resolved:
                    return False
                try:
                    other = Path(target).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return False
                other_root = _parse(other, ts_scope.grammar(target))
                if other_root.has_error or not _declares_own(other_root, imported):
                    return False
            elif parent is not None and parent.type in ("import_clause", "namespace_import", "import_require_clause"):
                return False
        seen = True
    return seen


def _in_import_statement(node: Node) -> bool:
    while node is not None:
        if node.type == "import_statement":
            return True
        node = node.parent
    return False


#: A module specifier as an import or an export spells it, or as the first
#: argument of any call (``import(…)``, ``require(…)``, ``requireActual(…)``,
#: ``importActual(…)``, ``mock(…)``). A superset: anything it finds resolving
#: to a reached module that the parse below does not account for refuses.
_SPECIFIER = re.compile(r"""(?:\bfrom|\bimport|\()\s*(['"`])([^'"`\s$]+)\1""")

#: Where a computed ``import(…)``/``require(…)`` can be.
_LOAD = re.compile(r"\b(?:import|require)\s*\(")

#: Bundler globs that load every module a pattern matches.
_GLOB = re.compile(r"\bimport\.meta\.glob(?:Eager)?\b|\brequire\.context\b")

#: Test-runner calls that name a module and replace it: what they return is
#: not the module, and a test still calls what it imports by name.
_MOCKS = frozenset({"mock", "doMock", "unmock", "doUnmock"})


def _reached_unspelled(decl_resolved, sources, markup, repo_dir, solution) -> bool:
    """Whether a module can reach the declaring file's exports without
    spelling the name the caller proof follows.

    The name scan in :func:`_users` sees every file that SPELLS the name. A
    file can also reach it unspelled: ``import * as m`` (then ``m[key]`` or
    ``Object.values(m)``), a default import, ``import()`` or ``require()``,
    ``export * as``, or any of those through a barrel that ``export *``s the
    declaring file. So every specifier in the checkout that resolves to the
    declaring file, or to such a barrel (transitively), must be a plain
    import of names, a side-effect import, a named re-export, or an
    ``export *`` (which makes its module a barrel). A computed ``import()``
    or ``require()`` can reach any module, and refuses too, wherever it could
    load the declaring file's package (:func:`_may_load_package`). A bundler
    glob (``import.meta.glob``, ``require.context``) refuses anywhere: its
    pattern can name another package's files by a relative path.
    """
    cache: dict[tuple[str, str], str | None] = {}
    reached = {decl_resolved}
    aliases = _exact_aliases(repo_dir)

    def reaches(reader: str, specifier: str) -> bool:
        if not _plausible(specifier, reached, aliases):
            return False
        key = (os.path.dirname(reader), specifier)
        if key not in cache:
            target = cross_file_ts.resolve_specifier(reader, specifier, repo_dir, solution=solution)
            cache[key] = str(Path(target).resolve()) if target is not None else None
        return cache[key] in reached

    found: list[tuple[str, str, str, list[str]]] = []
    packages = _Packages(repo_dir)
    for path, text in sources:
        if _GLOB.search(text):
            return True
        if _LOAD.search(text) and _computed_load(path, text) and packages.may_load(path, decl_resolved):
            return True
        specifiers = [m.group(2) for m in _SPECIFIER.finditer(text)]
        if specifiers:
            found.append((str(Path(path).resolve()), path, text, specifiers))
    while True:
        barrels: set[str] = set()
        for resolved, path, text, specifiers in found:
            hits = sum(1 for spec in specifiers if reaches(path, spec))
            if not hits:
                continue
            verdict = _classify(path, text, lambda spec, p=path: reaches(p, spec))
            if verdict is None or verdict[0] != hits:
                # A reaching specifier the parse did not account for.
                return True
            if verdict[1]:
                barrels.add(resolved)
        if barrels <= reached:
            break
        reached |= barrels
    for path, text in markup:
        if any(reaches(path, m.group(2)) for m in _SPECIFIER.finditer(text)):
            return True
    return False


#: What a specifier may end in beyond the module's own name.
_SPECIFIER_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".d.ts")


def _plausible(specifier: str, reached: set[str], aliases: frozenset[str] | None) -> bool:
    """Whether ``specifier`` can resolve to one of ``reached`` at all, decided
    without the resolver, which is too slow to ask of every string in a
    checkout. A superset: its last segment names a reached module (``x`` for
    ``x.ts``, ``dir`` for ``dir/index.ts``), it ends in ``.``, ``..`` or
    ``/``, or it is an exact ``paths`` alias, which can name any file (all
    bare specifiers when the configs could not be read)."""
    last = specifier.rstrip("/").rsplit("/", 1)[-1] if not specifier.endswith("/") else ""
    if last in ("", ".", ".."):
        return True
    for suffix in _SPECIFIER_SUFFIXES:
        if last.endswith(suffix):
            last = last[: -len(suffix)]
            break
    for target in reached:
        module = Path(target)
        stem = module.name.split(".", 1)[0]
        if last == stem or stem == "index" and last == module.parent.name:
            return True
    if specifier.startswith((".", "/")):
        return False
    return aliases is None or specifier in aliases


def _exact_aliases(repo_dir: str) -> frozenset[str] | None:
    """Every ``paths`` key without a ``*`` in the checkout's ``tsconfig.json``
    and ``jsconfig.json`` files (and the files they are named alike), or None
    when one cannot be read."""
    root = source_index.checkout_root(repo_dir)
    keys: set[str] = set()
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = [d for d in subdirectories if d not in _SKIPPED_DIRECTORIES]
        for name in names:
            if not (name.startswith(("tsconfig", "jsconfig")) and name.endswith(".json")):
                continue
            try:
                text = Path(directory, name).read_text(encoding="utf-8")
                data = json.loads(cross_file_ts._strip_jsonc(text))
            except (OSError, UnicodeDecodeError, ValueError):
                return None
            options = data.get("compilerOptions") if isinstance(data, dict) else None
            paths = options.get("paths") if isinstance(options, dict) else None
            if paths is None:
                continue
            if not isinstance(paths, dict):
                return None
            keys.update(key for key in paths if "*" not in key)
    return frozenset(keys)


#: Directories :func:`_exact_aliases` does not walk into.
_SKIPPED_DIRECTORIES = frozenset({"node_modules", ".git", "dist", "build", "coverage", ".next"})


def _classify(path: str, text: str, reaches) -> tuple[int, bool] | None:
    """For the statements and calls of ``path`` whose specifier ``reaches``:
    how many there are and whether one is a plain ``export *`` (the file is
    a barrel), or None when one can reach an export unspelled."""
    root = _parse(text, ts_scope.grammar(path))
    count, barrel = 0, False
    for node in _walk(root):
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            spec = _specifier(source)
            if spec is None or not reaches(spec):
                continue
            count += 1
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if any(c.type == "import_require_clause" for c in node.named_children):
                return None
            if clause is not None and any(c.type != "named_imports" for c in clause.named_children):
                # A default import or `* as m`.
                return None
        elif node.type == "export_statement" and node.child_by_field_name("source") is not None:
            spec = _specifier(node.child_by_field_name("source"))
            if spec is None or not reaches(spec):
                continue
            count += 1
            if any(c.type == "namespace_export" for c in node.named_children):
                return None
            if not any(c.type == "export_clause" for c in node.named_children):
                barrel = True
        elif node.type == "call_expression":
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None or arguments is None or arguments.type != "arguments":
                continue
            passed = [c for c in arguments.named_children if c.type != "comment"]
            spec = _specifier(passed[0]) if passed else None
            if spec is None or not reaches(spec):
                continue
            count += 1
            if not (callee.type == "member_expression" and _member_name(callee) in _MOCKS):
                # `import()`, `require()`, `requireActual()`, `importActual()`
                # or anything else handed the module: it can index it.
                return None
    return count, barrel


def _member_name(callee: Node) -> str | None:
    member = callee.child_by_field_name("property")
    return member.text.decode("utf-8") if member is not None else None


def _specifier(node: Node | None) -> str | None:
    """A plain string's or substitution-free template's text."""
    if node is not None and node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.named_children):
            return None
        return node.text.decode("utf-8")[1:-1]
    return cross_file_ts._string_value(node)


_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


class _Packages:
    """The ``package.json`` each file belongs to, read once per directory.

    A computed ``import()`` loads what its package can name: its own files,
    and the packages it depends on. One in a sibling package that does not
    depend on the declaring file's (an SDK's test fixture beside a web app,
    in a monorepo) cannot load the declaring file."""

    #: A manifest that cannot be read: it could declare anything.
    UNREADABLE = object()

    def __init__(self, repo_dir: str) -> None:
        self.root = cross_file_ts._stop_for(repo_dir)
        self._owner: dict[Path, Path | None] = {}
        self._manifest: dict[Path, object] = {}

    def owner(self, path: str) -> Path | None:
        """The directory of the nearest ``package.json`` above ``path``
        within the checkout, or None."""
        directory = Path(path).resolve().parent
        walked: list[Path] = []
        found: Path | None = None
        while True:
            if directory in self._owner:
                found = self._owner[directory]
                break
            walked.append(directory)
            if (directory / "package.json").is_file():
                found = directory
                break
            if directory == self.root or directory.parent == directory or not directory.is_relative_to(self.root):
                break
            directory = directory.parent
        for each in walked:
            self._owner[each] = found
        return found

    def manifest(self, directory: Path) -> object:
        if directory not in self._manifest:
            try:
                data = json.loads((directory / "package.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                data = self.UNREADABLE
            self._manifest[directory] = data if isinstance(data, dict) else self.UNREADABLE
        return self._manifest[directory]

    def may_load(self, loader: str, declaring: str) -> bool:
        """Whether a computed load in ``loader`` may load ``declaring``: the
        same package, a package depending on the declaring one by name, a
        file in no package, or anything unreadable on the way. Without a
        package around the declaring file, anything may."""
        home = self.owner(declaring)
        if home is None:
            return True
        here = self.owner(loader)
        if here is None or here == home:
            return True
        theirs, ours = self.manifest(home), self.manifest(here)
        if theirs is self.UNREADABLE or ours is self.UNREADABLE:
            return True
        name = theirs.get("name")
        if not isinstance(name, str):
            return False
        for field_name in _DEPENDENCY_FIELDS:
            section = ours.get(field_name)
            if section is not None and not isinstance(section, dict):
                return True
            if isinstance(section, dict) and name in section:
                return True
        return False


def _computed_load(path: str, text: str) -> bool:
    """Whether ``text`` has an ``import(…)`` or ``require(…)`` whose module is
    computed, which can reach any module."""
    for node in _walk(_parse(text, ts_scope.grammar(path))):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None or not (
            callee.type == "import" or callee.type == "identifier" and callee.text == b"require"
        ):
            continue
        arguments = node.child_by_field_name("arguments")
        passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
        if not passed or _specifier(passed[0]) is None:
            return True
    return False


def _accounted(
    component, file, declaring, allowed, decl_resolved, repo_dir, use=_element_name, solution=False
) -> bool:
    imported = 0
    for node in _walk(file.root):
        if node.type in ("string_fragment", "template_string") and _text(node, file.data) == component:
            # `components["Row"]`: a lookup by name.
            return False
        if node.type not in _NAMES or _text(node, file.data) != component:
            continue
        if use(node):
            continue
        if declaring:
            if node.id not in allowed:
                return False
            continue
        parent = node.parent
        if (
            parent is not None
            and parent.type == "import_specifier"
            and parent.child_by_field_name("name") == node
            and parent.child_by_field_name("alias") is None
            and (spec := _source(parent, file.data)) is not None
            and (
                target := cross_file_ts.resolve_specifier(file.path, spec, repo_dir, solution=solution)
            )
            is not None
            and str(Path(target).resolve()) == decl_resolved
        ):
            imported += 1
            continue
        return False
    return declaring or imported == 1

