"""A key constant whose declaration the run deleted must be named nowhere.

The key-constant rules fold a read and then delete the constant's declaration.
Which references are reads those rules fold is decided twice, by the Gate 2
predicates (`key_const._ts_is_removable_read`, `cross_file_ts._is_read`) and by
the rules' own filters, and a disagreement leaves a reference standing with
nothing to point at. This check does not ask either: it reads the bytes the run
produced and refuses the flag when a file that can reach the deleted constant
still spells its name.

"Can reach" is computed from the ORIGINAL text of every module, as a fixpoint:
a module exposes the constant's name under that name or, through
``export { X as Y } from``, ``export { X } from`` and ``export * from``, under
the names its re-exports give it. A file is then a survivor when it TAKES a name an exposing module exports,
in one of the forms that bind it: a named import (or ``export { N } from``), a
member of a namespace import or ``require`` (``ns.Y``, ``ns["Y"]``, a type
``ns.Y``), a destructured ``require`` or ``import()``, or a member of the
load itself (``typeof import("m").Y``). A property of an unrelated object that
happens to be called ``Y`` is not one. In a component file (``.vue``,
``.svelte``, ``.astro``, ``.mdx``) only the script code is read, in the same
forms.

Cost: files are read as text first. Only a file that spells a name some module
exposes is parsed to look for a survivor, and the fixpoint over re-exports
parses only the files that re-export from a module already exposing a name
(judged by the last segment of the specifier, a safe over-approximation); every
parse is memoized for the run.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flag_cleanup import cross_file_ts, key_const, source_index, ts_barrels, ts_workspace
from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_NAMES = (
    "identifier",
    "property_identifier",
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
    "type_identifier",
)
#: A module specifier in text no parser here reads: a component file's script.
_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|\bimport\s+)['"]([^'"]+)['"]""")


def refusals(changed, repo_dir: str, flag_key: str, grammar_of, deleted) -> dict[str, str]:
    """``{path: reason}`` for each file still naming a deleted key constant.

    ``changed`` are the run's summaries (``path``, ``original_content``,
    ``content``); ``grammar_of(path)`` is the file's grammar; ``deleted(s)``
    says a summary's file was deleted whole."""
    reach = _reach(changed, repo_dir, flag_key, grammar_of, deleted)
    if reach is None:
        return {}
    files, exposed, resolve, parse, grammar_for = (
        reach.files, reach.exposed, reach.resolve, reach.parse, reach.grammar_for
    )
    every = set().union(*exposed.values())

    found: dict[str, str] = {}
    for path, resolved, text in files:
        if not text or not any(name in text for name in every):
            continue  # cheap: a file spelling none of the names cannot be a survivor
        hit = _survivor(path, resolved, text, grammar_for(path), exposed, resolve, parse)
        if hit is not None:
            found[path] = _reason(hit)
    return found


def deleted_names(changed, repo_dir: str, flag_key: str, grammar_of, deleted) -> dict[str, frozenset[str]]:
    """Per resolved path, the names of the key constants the run deleted that
    a comment there can mean.

    Same arguments as :func:`refusals`. A key constant's name is not unique
    (every module may have its own ``FLAG_KEY``), so a name is scoped to a
    file only on proof that the name there IS the deleted constant: the
    declaring file itself, and a file whose ORIGINAL text takes the name from
    a module exposing it (see :func:`_taken_names`) and binds it no other way.
    Anything else, a component file or a file that does not parse included,
    is not scoped, and its comments are kept."""
    reach = _reach(changed, repo_dir, flag_key, grammar_of, deleted)
    if reach is None:
        return {}
    scoped: dict[str, set[str]] = {}
    for declaring, name in reach.gone:
        scoped.setdefault(declaring, set()).add(name)
    every = set().union(*reach.exposed.values())
    for path, resolved, _text in reach.files:
        original = reach.originals.get(resolved, "")
        if not original or not any(name in original for name in every):
            continue
        if Path(path).suffix in source_index.COMPONENT_SUFFIXES or not _is_code(path):
            continue
        root = reach.parse(original, reach.grammar_for(path))
        if root.has_error:
            continue
        names = _taken_names(root, path, reach.exposed, reach.resolve)
        if names:
            scoped.setdefault(resolved, set()).update(names)
    return {path: frozenset(names) for path, names in scoped.items()}


def _taken_names(root, path: str, exposed, resolve) -> set[str]:
    """The local names under which this parsed file takes a deleted constant
    from a module exposing it, and binds no other way.

    WHITELIST. The forms are: a named import (its local name, so the alias
    when there is one) or ``export { N } from`` (``N``); a member read
    ``ns.N`` off a namespace import, ``import ns = require``, or a
    ``const ns = require(...)``/``await import(...)`` whose name is bound
    nowhere else in the file; and a destructured ``require``/``import()``
    (``{ N }``, or the local of ``{ N: local }``). A name is then dropped if
    anything else in the file binds it too (a declaration, a parameter, an
    import from another module): the comment may mean that one."""

    def names_of(spec: str | None) -> set[str]:
        module = resolve(path, spec) if spec is not None else None
        return exposed.get(module, set()) if module is not None else set()

    def text_of(node) -> str:
        return node.text.decode("utf-8").strip("'\"")

    takes: list[tuple[str, int | None]] = []  # (local name, its binding node)
    namespaces: dict[str, tuple[int, set[str]]] = {}
    for node in _walk(root):
        if node.type in ("import_statement", "export_statement"):
            clause = next((c for c in node.named_children if c.type == "import_require_clause"), None)
            if clause is not None:
                have = names_of(cross_file_ts._string_value(next((c for c in clause.named_children if c.type == "string"), None)))
                local = next((c for c in clause.named_children if c.type == "identifier"), None)
                if have and local is not None:
                    namespaces[text_of(local)] = (local.id, have)
                continue
            have = names_of(cross_file_ts._string_value(node.child_by_field_name("source")))
            if not have:
                continue
            for sub in _walk(node):
                if sub.type == "import_specifier" and node.type == "import_statement":
                    name, alias = sub.child_by_field_name("name"), sub.child_by_field_name("alias")
                    if name is not None and text_of(name) in have:
                        local = alias if alias is not None else name
                        takes.append((text_of(local), local.id))
                elif sub.type == "export_specifier" and node.type == "export_statement":
                    name = sub.child_by_field_name("name")
                    if name is not None and text_of(name) in have:
                        takes.append((text_of(name), name.id))
                elif sub.type == "namespace_import":
                    local = next((c for c in sub.named_children if c.type == "identifier"), None)
                    if local is not None:
                        namespaces[text_of(local)] = (local.id, have)
        elif node.type == "call_expression":
            have = names_of(_load_spec(node))
            if not have:
                continue
            top = node
            while top.parent is not None and top.parent.type in ("await_expression", "parenthesized_expression"):
                top = top.parent
            consumer = top.parent
            if consumer is None or consumer.type != "variable_declarator" or consumer.child_by_field_name("value") != top:
                continue
            target = consumer.child_by_field_name("name")
            if target is not None and target.type == "identifier":
                namespaces[text_of(target)] = (target.id, have)
            elif target is not None and target.type == "object_pattern":
                for prop in target.named_children:
                    if prop.type == "shorthand_property_identifier_pattern" and text_of(prop) in have:
                        takes.append((text_of(prop), prop.id))
                    elif prop.type == "pair_pattern":
                        key, value = prop.child_by_field_name("key"), prop.child_by_field_name("value")
                        if key is not None and value is not None and value.type == "identifier" and text_of(key) in have:
                            takes.append((text_of(value), value.id))

    bindings = _all_bindings(root)
    if namespaces:
        for node in _walk(root):
            if node.type != "member_expression":
                continue
            obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
            if obj is None or prop is None or obj.type != "identifier":
                continue
            bound = namespaces.get(text_of(obj))
            if bound is None or text_of(prop) not in bound[1]:
                continue
            # The namespace must be bound once, by its load: a shadowing
            # binding could make this `ns` some other object.
            if [b for b in bindings if text_of(b) == text_of(obj) and b.id != bound[0]]:
                continue
            takes.append((text_of(prop), None))

    found: set[str] = set()
    for local, own in takes:
        if not any(text_of(b) == local and b.id != own for b in bindings):
            found.add(local)
    return found


def _all_bindings(root) -> list:
    """Every node binding a name in this file: :func:`binding_nodes` plus the
    names an import introduces (an alias, a default, a namespace,
    ``import x = require``), which carry no binding field. Over-collecting
    only keeps a comment."""
    found = {n.id: n for n in binding_nodes(root)}
    for node in _walk(root):
        if node.type == "import_specifier":
            local = node.child_by_field_name("alias") or node.child_by_field_name("name")
            if local is not None:
                found[local.id] = local
        elif node.type in ("import_clause", "namespace_import", "import_require_clause"):
            for child in node.named_children:
                if child.type == "identifier":
                    found[child.id] = child
    return list(found.values())


@dataclass(frozen=True)
class _Reach:
    """What :func:`refusals` and :func:`deleted_names` share: the deleted
    constants, every module's text, and which modules expose each name."""

    gone: list[tuple[str, str]]
    files: list[tuple[str, str, str]]  # (path, resolved, current text)
    originals: dict[str, str]
    exposed: dict[str, set[str]]
    resolve: Callable[[str, str], str | None]
    parse: Callable
    grammar_for: Callable[[str], str]


def _reach(changed, repo_dir: str, flag_key: str, grammar_of, deleted) -> _Reach | None:
    """The run's deleted key constants and the modules exposing them; None
    when the run deleted none."""
    summaries = {str(Path(s.path).resolve()): s for s in changed}
    gone = _gone(summaries, flag_key, grammar_of, deleted)
    if not gone:
        return None

    suffixes = _SUFFIXES + tuple(sorted(source_index.COMPONENT_SUFFIXES))
    files: list[tuple[str, str, str]] = []  # (path, resolved, current text)
    originals: dict[str, str] = {}
    for listed in source_index.files(repo_dir, suffixes):
        resolved = str(Path(listed.path).resolve())
        s = summaries.get(resolved)
        if s is not None:
            originals[resolved] = s.original_content
            text = "" if deleted(s) else s.content
        else:
            try:
                text = listed.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            originals[resolved] = text
        files.append((str(listed.path), resolved, text))
    listed_paths = {resolved for _p, resolved, _t in files}
    for resolved, s in summaries.items():  # a changed file the listing missed
        if resolved not in listed_paths:
            originals[resolved] = s.original_content
            files.append((s.path, resolved, "" if deleted(s) else s.content))

    parse = _parser()

    def grammar_for(path: str) -> str:
        return "component" if Path(path).suffix in source_index.COMPONENT_SUFFIXES else grammar_of(path)

    resolved_cache: dict[tuple[str, str], str | None] = {}

    # Resolution is against the files as they were BEFORE the run: a specifier
    # naming a file this run deleted whole still reaches it.
    gone_files = {r for r, s in summaries.items() if deleted(s)}
    stop = cross_file_ts._stop_for(repo_dir)

    def virtual(path: str, specifier: str) -> str | None:
        if not gone_files:
            return None
        bases: list[str] = []
        if specifier.startswith("."):
            bases.append(os.path.normpath(Path(path).parent / specifier))
        else:
            try:
                options = cross_file_ts._config(Path(path).parent, stop, True, None)
                bases.extend(os.path.normpath(t) for t in (options.targets(specifier) or ()) if options is not None)
            except (cross_file_ts._Unknown, OSError, ValueError, AttributeError):
                return None
        for base in bases:
            stem = re.sub(r"\.(?:js|jsx|mjs|cjs)$", "", base)
            for candidate in (
                base,
                *(stem + ext for ext in _SUFFIXES),
                *(f"{stem}/index{ext}" for ext in _SUFFIXES),
            ):
                if str(Path(candidate).resolve()) in gone_files:
                    return str(Path(candidate).resolve())
        return None

    def resolve(path: str, specifier: str) -> str | None:
        key = (str(Path(path).parent), specifier)
        if key not in resolved_cache:
            package = None
            try:
                found = cross_file_ts.resolve_specifier(path, specifier, repo_dir, solution=True, workspace=True)
                if found is None:
                    # A package of the checkout, whoever imports it: its proven
                    # entry, or else every module under it (this check may only
                    # over-attribute).
                    package = ts_workspace.reach(specifier, repo_dir)
                    if package is not None and package.entry is not None:
                        found, package = package.entry, None
            except (OSError, ValueError):
                found = None
            if package is not None:
                resolved_cache[key] = _Exposed.under(package.directories)
            else:
                resolved_cache[key] = str(Path(found).resolve()) if found is not None else virtual(path, specifier)
        return resolved_cache[key]

    exposed: dict[str, set[str]] = _Exposed()
    for declaring, name in gone:
        exposed.setdefault(declaring, set()).add(name)
    _expose(files, originals, grammar_of, resolve, exposed, parse)
    return _Reach(gone, files, originals, exposed, resolve, parse, grammar_for)


def _gone(summaries, flag_key: str, grammar_of, deleted) -> list[tuple[str, str]]:
    """``(resolved path, name)`` of each key constant a declaring file lost."""
    gone: list[tuple[str, str]] = []
    for resolved, s in summaries.items():
        grammar = grammar_of(s.path)
        before = _constants(s.original_content, grammar, flag_key)
        after = set() if deleted(s) else _constants(s.content, grammar, flag_key)
        gone.extend((resolved, name) for name in sorted(before - after))
    return gone


def _reason(name: str) -> str:
    return (
        f"the key constant `{name}` was deleted but a reference to it survived "
        "the rewrite, which would not build"
    )


def _constants(text: str, grammar: str, flag_key: str) -> set[str]:
    root = _parse(text, grammar)
    return {c.name for c in key_const._ts_key_consts(root, text.encode("utf-8"), flag_key)}


def _parser():
    """A memoized ``(text, grammar) -> root`` for the run: the same text of the
    same grammar is parsed once."""
    memo = source_index.run_memo("ts_survivor_parse")
    cache = memo if memo is not None else {}

    def parse(text: str, grammar: str):
        key = (grammar, text)
        if key not in cache:
            cache[key] = _parse(text, grammar)
        return cache[key]

    return parse


def _is_code(path: str) -> bool:
    return path.endswith(_SUFFIXES)


_REEXPORT_SOURCE = re.compile(
    r"""\bexport\s*(?:type\s+)?(?:\*(?:\s*as\s+\w+)?|\{[^}]*\})\s*from\s*['"]([^'"]+)['"]"""
)


def _segment(specifier: str) -> str:
    """The last path segment of a specifier, without a script extension."""
    return re.sub(r"\.(?:[cm]?[jt]sx?)$", "", specifier.rstrip("/").rsplit("/", 1)[-1])


def _stems(module: str) -> set[str]:
    path = Path(module)
    stem = re.sub(r"\.(?:d\.)?[cm]?[jt]sx?$", "", path.name)
    return {stem, path.parent.name} if stem == "index" else {stem}


class _Exposed(dict):
    """``{module: names it exposes}``, also answering for a key made by
    :meth:`under`: every module under some directories, which a specifier
    into a package whose entry is not proven may load."""

    _PREFIX = "\0under:"

    @classmethod
    def under(cls, directories) -> str:
        return cls._PREFIX + "\0".join(str(Path(d).resolve()) for d in directories)

    def _union(self, key: str) -> set[str]:
        roots = [Path(d) for d in key[len(self._PREFIX) :].split("\0")]
        found: set[str] = set()
        for module, names in dict.items(self):
            if any(Path(module).is_relative_to(r) for r in roots):
                found |= names
        return found

    def get(self, key, default=None):
        if isinstance(key, str) and key.startswith(self._PREFIX):
            return self._union(key) or default
        return dict.get(self, key, default)

    def __contains__(self, key) -> bool:
        if isinstance(key, str) and key.startswith(self._PREFIX):
            return bool(self._union(key))
        return dict.__contains__(self, key)


def _expose(files, originals, grammar_of, resolve, exposed: dict[str, set[str]], parse) -> None:
    """Grow ``exposed`` (module to the names it exports that lead to a deleted
    constant) through the re-exports of the original text of every module,
    to a fixpoint. A file is parsed only when it has an ``export ... from``
    whose last specifier segment names a module already exposing something
    (or is an alias path, which cannot be judged by name)."""
    candidates: list[tuple[str, str, str, set[str]]] = []
    for path, resolved, _text in files:
        text = originals.get(resolved, "")
        if "export" not in text or not _is_code(path):
            continue
        sources = _REEXPORT_SOURCE.findall(text)
        if sources:
            candidates.append((path, resolved, text, set(sources)))
    #: What each module exports by name itself: an explicit export shadows a star's.
    own: dict[str, set[str]] = {}
    parsed: dict[str, list[tuple[str, list[tuple[str | None, str | None]]]]] = {}

    def reexports(path: str, resolved: str, text: str):
        if resolved in parsed:
            return parsed[resolved]
        found: list[tuple[str, list[tuple[str | None, str | None]]]] = []
        parsed[resolved] = found
        root = parse(text, grammar_of(path))
        if root.has_error:
            return found
        taken = own.setdefault(resolved, set())
        for statement in root.named_children:
            if statement.type != "export_statement":
                continue
            declaration = statement.child_by_field_name("declaration")
            if declaration is not None:
                try:
                    taken.update(ts_barrels._bound_names(declaration))
                except ts_barrels._Unknown:
                    pass
            for node in _walk(statement):
                if node.type == "export_specifier":
                    exported = node.child_by_field_name("alias") or node.child_by_field_name("name")
                    if exported is not None:
                        taken.add(exported.text.decode("utf-8").strip("'\""))
            specifier = cross_file_ts._string_value(statement.child_by_field_name("source"))
            target = resolve(path, specifier) if specifier is not None else None
            if target is None:
                continue
            pairs: list[tuple[str | None, str | None]] = []
            if any(c.type == "*" for c in statement.children) and not any(
                c.type == "namespace_export" for c in statement.named_children
            ):
                pairs.append((None, None))
            for node in _walk(statement):
                if node.type == "export_specifier":
                    name, alias = node.child_by_field_name("name"), node.child_by_field_name("alias")
                    if name is not None:
                        original = name.text.decode("utf-8").strip("'\"")
                        exported = (alias if alias is not None else name).text.decode("utf-8").strip("'\"")
                        pairs.append((original, exported))
            found.append((target, pairs))
        return found

    changed = True
    while changed:
        changed = False
        stems = set().union(*(_stems(m) for m in exposed))
        for path, resolved, text, segments in candidates:
            # A relative specifier is judged by its last segment; any other one
            # (a `paths` alias, a workspace package) only counts when the resolver
            # maps it onto a module that already exposes something.
            if not any(
                (_segment(src) in stems) if src.startswith(".") else (resolve(path, src) in exposed)
                for src in segments
            ):
                continue
            for target, pairs in reexports(path, resolved, text):
                have = exposed.get(target)
                if not have:
                    continue
                for original, exported in pairs:
                    fresh = (
                        set(have) - own.get(resolved, set())
                        if original is None
                        else ({exported} if original in have else set())
                    )
                    if fresh - exposed.get(resolved, set()):
                        exposed.setdefault(resolved, set()).update(fresh)
                        changed = True


def _load_spec(node) -> str | None:
    """The specifier of ``require("m")`` / ``import("m")``, seen through
    ``await`` and parentheses; None for anything else."""
    while node is not None and node.type in ("await_expression", "parenthesized_expression"):
        inner = [c for c in node.named_children if c.type != "comment"]
        node = inner[0] if len(inner) == 1 else None
    if node is None or node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")
    if function is None or args is None:
        return None
    if function.type != "import" and not (function.type == "identifier" and function.text == b"require"):
        return None
    first = next((c for c in args.named_children if c.type == "string"), None)
    return cross_file_ts._string_value(first) if first is not None else None


def _survivor(path, resolved, text, grammar, exposed, resolve, parse) -> str | None:
    """A name this file takes from a module exposing a deleted constant, else None."""
    if grammar == "component":
        code = _component_code(path, text)
        if not code or not any(n in code for names in exposed.values() for n in names):
            return None
        for lang in ("ts", "tsx"):
            root = parse(code, lang)
            if not root.has_error:
                return _takes(root, path, resolved, exposed, resolve)
        # Code the parser cannot read: any exposed name beside a specifier that loads its module.
        loaded = {m for spec in _SPECIFIER.findall(code) if (m := resolve(path, spec))}
        for module in loaded:
            for name in sorted(exposed.get(module, ())):
                if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", code):
                    return name
        return None
    root = parse(text, grammar)
    if root.has_error:
        # Unreadable: fall back to every name-like node, which can only over-refuse.
        loaded = {resolved} | {m for spec in _specifiers(root) if (m := resolve(path, spec))}
        names = set().union(*(exposed.get(m, set()) for m in loaded))
        for node in _walk(root):
            if node.type in _NAMES and node.text.decode("utf-8") in names:
                return node.text.decode("utf-8")
        return None
    return _takes(root, path, resolved, exposed, resolve)


def _takes(root, path: str, resolved: str, exposed, resolve) -> str | None:
    """A name this parsed file takes from an exposing module, else None.

    WHITELIST + FALLBACK. The forms understood exactly are: a named import (or
    `export { N } from`), a destructured `require`/`import()` (plain keys only),
    a member of the load itself, and a namespace or `require` binding used ONLY
    as `ns.NAME`, `ns["NAME"]` or a type qualifier `ns.NAME`. If anything the
    file gets from an exposing module is used any other way (destructured from
    the namespace, passed along, assigned, returned, a `.then` callback,
    `export * as`, `import f = require`'s value escaping...), the file is judged
    by the coarse rule instead: it is a survivor if it spells any name those
    modules expose.
    """
    bound = {n.id for n in binding_nodes(root)}
    own = exposed.get(resolved, set())
    if own:
        for node in _walk(root):
            if (
                node.type in ("identifier", "shorthand_property_identifier")
                and node.id not in bound
                and node.text.decode("utf-8") in own
            ):
                return node.text.decode("utf-8")

    def names_of(spec: str | None) -> set[str]:
        module = resolve(path, spec) if spec is not None else None
        return exposed.get(module, set()) if module is not None else set()

    def text_of(node) -> str:
        return node.text.decode("utf-8").strip("'\"")

    loaded: set[str] = set()  # every name an exposing module loaded here exposes
    namespaces: dict[str, set[str]] = {}
    declared: set[int] = set()  # the binding nodes of those loads, not uses
    fallback = False

    for node in _walk(root):
        if node.type in ("import_statement", "export_statement"):
            clause = next((c for c in node.named_children if c.type == "import_require_clause"), None)
            if clause is not None:
                have = names_of(cross_file_ts._string_value(next((c for c in clause.named_children if c.type == "string"), None)))
                local = next((c for c in clause.named_children if c.type == "identifier"), None)
                if have and local is not None:
                    loaded |= have
                    namespaces.setdefault(text_of(local), set()).update(have)
                    declared.add(local.id)
                continue
            have = names_of(cross_file_ts._string_value(node.child_by_field_name("source")))
            if not have:
                continue
            loaded |= have
            if any(c.type == "namespace_export" for c in node.named_children):
                fallback = True  # `export * as ns from`: the namespace escapes
                continue
            for sub in _walk(node):
                if sub.type in ("import_specifier", "export_specifier"):
                    name = sub.child_by_field_name("name")
                    if name is not None and text_of(name) in have:
                        return text_of(name)
                elif sub.type == "namespace_import":
                    local = next((c for c in sub.named_children if c.type == "identifier"), None)
                    if local is not None:
                        namespaces.setdefault(text_of(local), set()).update(have)
                        declared.add(local.id)
        elif node.type == "call_expression":
            have = names_of(_load_spec(node))
            if not have:
                continue
            loaded |= have
            top = node
            while top.parent is not None and top.parent.type in ("await_expression", "parenthesized_expression"):
                top = top.parent
            consumer = top.parent
            if consumer is None:
                fallback = True
            elif consumer.type == "member_expression" and consumer.child_by_field_name("object") == top:
                prop = consumer.child_by_field_name("property")
                spelled = text_of(prop) if prop is not None else ""
                if spelled in have:
                    return spelled
                if spelled in ("then", "catch", "finally") or prop is None:
                    fallback = True  # a callback receives the module
            elif consumer.type == "variable_declarator" and consumer.child_by_field_name("value") == top:
                target = consumer.child_by_field_name("name")
                if target is not None and target.type == "identifier":
                    namespaces.setdefault(text_of(target), set()).update(have)
                    declared.add(target.id)
                elif target is not None and target.type == "object_pattern":
                    for prop in target.named_children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            key = prop
                        elif prop.type == "pair_pattern":
                            key = prop.child_by_field_name("key")
                        else:
                            fallback = True  # a rest element takes the lot
                            continue
                        if key is not None and text_of(key) in have:
                            return text_of(key)
                else:
                    fallback = True
            else:
                fallback = True  # passed along, assigned, returned...

    # A namespace or require binding must be used only as a member read.
    if namespaces and not fallback:
        for node in _walk(root):
            if node.type != "identifier" or node.id in declared:
                continue
            have = namespaces.get(node.text.decode("utf-8"))
            if have is None:
                continue
            parent = node.parent
            if parent is not None and parent.type == "member_expression" and parent.child_by_field_name("object") == node:
                prop = parent.child_by_field_name("property")
                if prop is not None and text_of(prop) in have:
                    return text_of(prop)
            elif parent is not None and parent.type == "subscript_expression" and parent.child_by_field_name("object") == node:
                index = parent.child_by_field_name("index")
                value = cross_file_ts._string_value(index) if index is not None else None
                if value is None:
                    fallback = True
                elif value in have:
                    return value
            elif parent is not None and parent.type == "nested_type_identifier" and parent.child_by_field_name("module") == node:
                name = parent.child_by_field_name("name")
                if name is not None and text_of(name) in have:
                    return text_of(name)
            elif node.id in bound:
                continue  # a shadowing binding
            else:
                fallback = True
    if fallback:
        for node in _walk(root):
            if node.type in _NAMES and text_of(node) in loaded:
                return text_of(node)
    return None


def _specifiers(root) -> list[str]:
    """Every module specifier a parsed file loads."""
    found: list[str] = []
    for node in _walk(root):
        source = None
        if node.type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
        elif node.type == "import_require_clause":
            source = next((c for c in node.named_children if c.type == "string"), None)
        elif node.type == "call_expression":
            spec = _load_spec(node)
            if spec is not None:
                found.append(spec)
            continue
        value = cross_file_ts._string_value(source) if source is not None else None
        if value is not None:
            found.append(value)
    return found


def _component_code(path: str, text: str) -> str:
    """The script code of a component file: its ``<script>`` blocks (and an
    Astro frontmatter), or an MDX file's ``import``/``export`` lines."""
    if path.endswith(".mdx"):
        out: list[str] = []
        open_statement = False
        for line in text.splitlines():
            if open_statement or re.match(r"(?:import|export)\b", line):
                out.append(line)
                open_statement = not re.search(r"""(?:;|from\s*['"][^'"]+['"]|^\s*$)\s*$""", line)
        return "\n".join(out)
    blocks = [m.group(1) for m in re.finditer(r"<script\b[^>]*>(.*?)</script\s*>", text, re.DOTALL | re.IGNORECASE)]
    if path.endswith(".astro"):
        front = re.match(r"\s*---\s*\n(.*?)\n---", text, re.DOTALL)
        if front:
            blocks.append(front.group(1))
    return "\n".join(blocks)
