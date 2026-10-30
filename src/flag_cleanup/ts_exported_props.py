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
each is a way to render the component this pass cannot see. A key computed at
run time (``components[name]``) is never spelled, and cannot be seen.

Every element across those files then has to pass the prop once as the same
literal, with no spread, and the output has to pass it at more elements than
the input did (only a literal the transform wrote counts, as in the
single-file pass). The declaring side is edited exactly as
:mod:`flag_cleanup.ts_constant_props` edits it (the parameter, the props type
it owns, a marker at each read), and each other file loses the attribute.
Every file edited must be one this run's engine processes.
"""

from __future__ import annotations

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


def _exported_function(root: Node, data: bytes, component: str) -> tuple[Node, set[int]] | None:
    """The function an exported top-level ``component`` is, with the ids of
    the name nodes that declare and export it, or None."""
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
    else:
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


def _users(component, decl, allowed, files, by_resolved, repo_dir, in_run) -> list[_File] | None:
    """Every file spelling ``component`` (the declaring one first), each
    proven to spell it only as allowed, or None."""
    word = re.compile(rf"(?<![\w$]){re.escape(component)}(?![\w$])")
    decl_resolved = str(Path(decl.path).resolve())
    users = [decl]
    seen_decl = False
    for listed in source_index.files(repo_dir, with_ignored=True):
        if not str(listed.path).endswith(_extensions()):
            continue
        found = _read(str(listed.path), files, by_resolved)
        if found is None:
            return None
        spelled, original, current = found
        if not word.search(current) and not word.search(original):
            continue
        if listed.resolved == decl_resolved:
            file = decl
            seen_decl = True
        else:
            if listed.ignored or not in_run(spelled):
                return None
            root = _parse(current, ts_scope.grammar(spelled))
            if root.has_error:
                return None
            file = _File(spelled, original, current, root, current.encode("utf-8"))
        if not _accounted(component, file, file is decl, allowed, decl_resolved, repo_dir):
            return None
        if file is not decl:
            users.append(file)
    if not seen_decl:
        # The declaring file's own spellings went unchecked: outside the listing.
        return None
    for listed in markup_files(repo_dir):
        try:
            if word.search(listed.path.read_text(encoding="utf-8", errors="replace")):
                return None
        except OSError:
            return None
    return users


def _accounted(component, file, declaring, allowed, decl_resolved, repo_dir) -> bool:
    imported = 0
    for node in _walk(file.root):
        if node.type in ("string_fragment", "template_string") and _text(node, file.data) == component:
            # `components["Row"]`: a lookup by name.
            return False
        if node.type not in _NAMES or _text(node, file.data) != component:
            continue
        if _element_name(node):
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
            and (target := cross_file_ts.resolve_specifier(file.path, spec, repo_dir)) is not None
            and str(Path(target).resolve()) == decl_resolved
        ):
            imported += 1
            continue
        return False
    return declaring or imported == 1

