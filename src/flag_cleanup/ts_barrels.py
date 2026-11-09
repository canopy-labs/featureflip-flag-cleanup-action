"""Which module a name imported through a barrel is declared in.

A flag registry is usually re-exported from one ``keys`` or ``index`` module
(``export * from "./definitions"``, ``export { X } from "./x"``), so a reader
imports the key constant from a file that does not declare it. This module
follows such a chain to the file that does, or says it cannot.

The answer is positive only when it is certain. The questions below decide
what a module exports, and every one that cannot be answered from the source
makes the whole answer "unknown", which the caller treats as it treated every
barrel before: the import stays unresolved and the declaration stays.

* A name the module declares itself, or exports by ``export { x }`` without a
  source, is the module's own. Exporting it by ``export { x as N }`` of a local
  is the same, and is never followed.
* ``export { N } from "./m"`` hands the question to ``./m``. A renamed
  re-export (``export { M as N } from``) hands ``./m`` the question for ``M``;
  the importers of ``N`` are found by a second name scan
  (:func:`alias_names`).
* ``export * from "./m"`` hands it to ``./m`` too, but only if EVERY star
  target of the module resolves (a target outside the checkout might export
  the name as well) and exactly one of them provides it: ECMAScript drops a
  name two star exports both supply. A star target that is a package outside
  the checkout (``outside``) provides nothing of ours and is skipped.
* ``export = x``, ``module.exports`` and ``exports.x`` make the module's
  exports something this reading cannot list.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import ts_syntax

#: How many re-exports are followed in a row.
MAX_HOPS = 16

#: The files (resolved) the run in progress can edit. A re-export line is judged
#: removable only in one of them: the resolution would otherwise let a
#: constant go while a barrel the run never writes still names it. Set by the
#: runner around the resolution; unset, no line is removable.
editable: ContextVar[frozenset[str] | None] = ContextVar("editable_barrels", default=None)

#: ``Resolve(from_file, specifier)``: the file the specifier names, if one.
Resolve = Callable[[str, str], "str | None"]


@dataclass(frozen=True)
class Reexport:
    """One ``export { name } from "source"`` specifier of a module."""

    statement: Node
    specifier: Node
    source: str


class _Unknown(Exception):
    """The module's exports cannot be listed from its source."""


def _language(path: str) -> str:
    if path.endswith(".tsx"):
        return "tsx"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "js"
    return "ts"


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _string(node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    parts = node.named_children
    if len(parts) != 1 or parts[0].type != "string_fragment":
        return None
    return _text(parts[0])


def _bound_names(declaration: Node) -> list[str]:
    """The names an exported declaration introduces; raises for one it cannot list."""
    kind = declaration.type
    if kind in ("lexical_declaration", "variable_declaration"):
        names: list[str] = []
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            target = declarator.child_by_field_name("name")
            if target is None or target.type != "identifier":
                raise _Unknown  # a destructuring: any name
            names.append(_text(target))
        return names
    name = declaration.child_by_field_name("name")
    if name is None:
        raise _Unknown
    return [_text(name)]


def read(path: str) -> tuple[Node, str] | None:
    """The parsed module, or None when it cannot be read or parsed cleanly."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    root = ts_syntax._parse(source, _language(path))
    if root.has_error or _commonjs(root):
        return None
    return root, source


def _walk(root: Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _commonjs(root: Node) -> bool:
    """Whether the code (never a comment or a string) uses ``module.exports``
    or ``exports``, which list a module's exports in a way this reading cannot."""
    for node in _walk(root):
        if node.type == "identifier" and _text(node) == "exports":
            return True
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if (
                obj is not None
                and prop is not None
                and obj.type == "identifier"
                and _text(obj) == "module"
                and _text(prop) == "exports"
            ):
                return True
    return False


def without_specifiers(text: str, path: str) -> str:
    """``text`` with every module specifier string (``from "x"``, ``import "x"``,
    ``import("x")``, ``require("x")``, ``import m = require("x")``) blanked, which is
    what a path is: not a mention of anything. Unchanged when it does not parse."""
    root = ts_syntax._parse(text, _language(path))
    if root.has_error:
        return text
    raw = text.encode("utf-8")
    spans: list[tuple[int, int]] = []
    for node in _walk(root):
        source: Node | None = None
        if node.type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
        elif node.type == "import_require_clause":
            source = next((c for c in node.named_children if c.type == "string"), None)
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if (
                function is not None
                and args is not None
                and (function.type == "import" or (function.type == "identifier" and _text(function) == "require"))
            ):
                first = next((c for c in args.named_children if c.type != "comment"), None)
                if first is not None and first.type == "string":
                    source = first
        if source is not None and source.type == "string":
            spans.append((source.start_byte, source.end_byte))
    for start, end in spans:
        raw = raw[:start] + b'""' + b" " * (end - start - 2) + raw[end:]
    return raw.decode("utf-8")


def _clause_of(statement: Node) -> Node | None:
    return next((c for c in statement.named_children if c.type == "export_clause"), None)


def _is_star(statement: Node) -> bool:
    return any(c.type == "*" for c in statement.children) and not any(
        c.type == "namespace_export" for c in statement.named_children
    )


def _specifiers(clause: Node):
    for specifier in clause.named_children:
        if specifier.type != "export_specifier":
            continue
        name = specifier.child_by_field_name("name")
        alias = specifier.child_by_field_name("alias")
        yield specifier, name, alias


def _spelled(node: Node | None) -> str | None:
    if node is None:
        return None
    return _string(node) if node.type == "string" else _text(node)


@dataclass(frozen=True)
class _Exports:
    declares: bool
    #: ``export { name } from source``, not renamed
    named: Reexport | None
    #: every ``export * from`` source
    stars: tuple[str, ...]
    #: ``export { original as name } from source``
    renamed: tuple[Reexport, str] | None = None


def _exports_of(root: Node, name: str) -> _Exports:
    """What the top level of a module says about the export ``name``."""
    declares = False
    named: Reexport | None = None
    renamed: tuple[Reexport, str] | None = None
    stars: list[str] = []
    for statement in root.named_children:
        if statement.type != "export_statement":
            continue
        source = _string(statement.child_by_field_name("source"))
        if statement.child_by_field_name("source") is not None and source is None:
            raise _Unknown
        clause = _clause_of(statement)
        if _is_star(statement):
            if source is None:
                raise _Unknown
            stars.append(source)
            continue
        if any(c.type == "namespace_export" for c in statement.named_children):
            # ``export * as ns from``: exports only ``ns``
            space = next(c for c in statement.named_children if c.type == "namespace_export")
            exported = next((c for c in space.named_children), None)
            if _spelled(exported) == name:
                raise _Unknown
            continue
        if clause is not None:
            type_only = any(c.type == "type" for c in statement.children)
            for specifier, spec_name, alias in _specifiers(clause):
                exported = _spelled(alias if alias is not None else spec_name)
                if exported != name:
                    continue
                if source is None or type_only or any(c.type == "type" for c in specifier.children):
                    raise _Unknown  # a local re-exported (and perhaps renamed), or a type
                if named is not None or declares or renamed is not None:
                    raise _Unknown
                if alias is not None:
                    original = _spelled(spec_name)
                    if original is None:
                        raise _Unknown
                    renamed = (Reexport(statement, specifier, source), original)
                else:
                    named = Reexport(statement, specifier, source)
            continue
        if any(c.type == "=" for c in statement.children):
            raise _Unknown  # ``export = x``
        declaration = statement.child_by_field_name("declaration")
        if declaration is None:
            # ``export default ...`` and the rest export no ``name``, unless
            # the text says otherwise.
            if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", _text(statement)) and not any(
                c.type == "default" for c in statement.children
            ):
                raise _Unknown
            continue
        if name in _bound_names(declaration):
            if named is not None or declares or renamed is not None:
                raise _Unknown
            declares = True
    return _Exports(declares, named, tuple(stars), renamed)


#: ``Outside(from_file, specifier)``: whether the specifier names a package that
#: cannot be a file of the checkout (see ``cross_file_ts.package_outside``).
Outside = Callable[[str, str], bool]


def provider(
    module: str, name: str, resolve: Resolve, outside: Outside | None = None
) -> str | None | bool:
    """The module that declares what ``module`` exports as ``name``, following
    no rename: ``None`` when the chain renames the name on the way.

    The path of that module; ``False`` when ``module`` provably does not export
    ``name``; ``None`` when that cannot be said.
    """
    found = provider_of(module, name, resolve, outside)
    if not isinstance(found, tuple):
        return found
    return found[0] if found[1] == name else None


def provider_of(
    module: str,
    name: str,
    resolve: Resolve,
    outside: Outside | None = None,
    *,
    _seen: tuple[str, ...] = (),
) -> tuple[str, str] | None | bool:
    """``(module, name)`` of the declaration behind what ``module`` exports as
    ``name`` (a rename on the way changes the name); ``False`` when ``module``
    provably does not export ``name``; ``None`` when that cannot be said."""
    if module in _seen:
        return False  # a cycle adds nothing new
    if len(_seen) >= MAX_HOPS:
        return None
    parsed = read(module)
    if parsed is None:
        return None
    try:
        exports = _exports_of(parsed[0], name)
    except _Unknown:
        return None
    trail = (*_seen, module)
    if exports.declares:
        return module, name
    followed = exports.named
    wanted = name
    if exports.renamed is not None:
        followed, wanted = exports.renamed
    if followed is not None:
        target = resolve(module, followed.source)
        if target is None:
            return None
        found = provider_of(target, wanted, resolve, outside, _seen=trail)
        return found if found is not False else None
    found_in: list[tuple[str, str]] = []
    for source in exports.stars:
        target = resolve(module, source)
        if target is None:
            if outside is not None and outside(module, source):
                continue  # a package outside the checkout supplies none of ours
            return None  # it might export the name as well
        found = provider_of(target, name, resolve, outside, _seen=trail)
        if found is None:
            return None
        if found is not False:
            found_in.append(found)
    if not found_in:
        return False
    return found_in[0] if len(set(found_in)) == 1 else None


def generated_hop(
    module: str, name: str, resolve: Resolve, generated: Callable[[str, str], bool], *, _seen: tuple[str, ...] = ()
) -> tuple[str, str] | None:
    """``(barrel, specifier)`` of the first re-export, on the way from ``module``
    to where ``name`` is declared, whose relative target is absent from the
    checkout and gitignored: a generated barrel. None when the chain has no such
    hop (or cannot be read)."""
    if module in _seen or len(_seen) >= MAX_HOPS:
        return None
    parsed = read(module)
    if parsed is None:
        return None
    try:
        exports = _exports_of(parsed[0], name)
    except _Unknown:
        return None
    trail = (*_seen, module)
    hops: list[tuple[str, str]] = []
    if exports.renamed is not None:
        hops.append((exports.renamed[0].source, exports.renamed[1]))
    elif exports.named is not None:
        hops.append((exports.named.source, name))
    else:
        hops.extend((source, name) for source in exports.stars)
    for source, wanted in hops:
        target = resolve(module, source)
        if target is None:
            if generated(module, source):
                return module, source
            continue
        found = generated_hop(target, wanted, resolve, generated, _seen=trail)
        if found is not None:
            return found
    return None


def alias_names(sources: dict[str, str], names: set[str]) -> set[str]:
    """The names ``names`` are re-exported under, by ``export { X as Y } from``
    in ``sources`` (path to text), through any number of renames: the names a
    scan must also look for to find the importers. Matched by spelling, not
    resolved, since a name found wrongly only costs a scan."""
    found: set[str] = set()
    parsed: list[Node] = []
    for path, text in sources.items():
        if "export" not in text or " as " not in text:
            continue
        root = ts_syntax._parse(text, _language(path))
        if not root.has_error:
            parsed.append(root)
    known = set(names)
    changed = True
    while changed:
        changed = False
        for root in parsed:
            for statement in root.named_children:
                clause = _clause_of(statement) if statement.type == "export_statement" else None
                if clause is None or statement.child_by_field_name("source") is None:
                    continue
                for _specifier, spec_name, alias in _specifiers(clause):
                    original, renamed = _spelled(spec_name), _spelled(alias)
                    if alias is not None and original in known and renamed is not None and renamed not in known:
                        known.add(renamed)
                        found.add(renamed)
                        changed = True
    return found


def reexport_of(specifier: Node, resolve: Resolve, path: str) -> tuple[str, str] | None:
    """``(name, target module)`` when ``specifier`` is an ``export { name }`` or
    ``export { name as other } from "source"`` specifier of ``path``: not a
    type, and its source resolves. ``name`` is what the target is asked for.
    None for anything else."""
    if specifier.type != "export_specifier":
        return None
    if any(c.type == "type" for c in specifier.children):
        return None
    clause = specifier.parent
    statement = clause.parent if clause is not None else None
    if statement is None or statement.type != "export_statement":
        return None
    if any(c.type == "type" for c in statement.children):
        return None
    source = _string(statement.child_by_field_name("source"))
    name = specifier.child_by_field_name("name")
    if source is None or name is None or name.type != "identifier":
        return None
    target = resolve(path, source)
    return (_text(name), target) if target is not None else None


@dataclass(frozen=True)
class Hop:
    """A re-export line to remove along with the constant it re-exports."""

    #: the barrel file, spelled as the run lists it
    path: str
    name: str
    #: the specifier text after ``from``
    source: str
    #: the file declaring ``name``, at the end of the chain
    module: str
    #: the name the specifier asks its source for (``name`` unless the chain renamed it)
    spelled: str = ""


#: Every line :func:`removable` judged removable while the resolution ran:
#: ``(resolved barrel, name, source text, declaring module)``. The runner
#: hands it to :func:`apply`, which refuses the run when a line judged
#: removable (and so allowed to take its constant with it) was never planned.
judged: ContextVar[set[tuple[str, str, str, str]] | None] = ContextVar("judged_barrels", default=None)

_DIRECTIVE = re.compile(
    r"^\s*(?://\s*(?:@ts-expect-error|@ts-ignore|@ts-nocheck|eslint-disable-next-line|prettier-ignore)"
    r"|/\*\s*(?:eslint-disable-next-line|prettier-ignore))",
)
_PUBLIC_FIELDS = ("main", "module", "types", "typings", "exports", "browser", "bin")


def public_entry(path: str) -> bool:
    """Whether ``path`` may be part of a published package's API.

    Sound by construction, with no path matching: the NEAREST ``package.json``
    above the file decides. Unless it is ``private``, a manifest that declares
    any of ``main``, ``module``, ``types``, ``typings``, ``exports``,
    ``browser`` or ``bin`` publishes something, and a build can map any source
    file to it (``dist/esm/index.js`` from ``src/index.ts``, ``./*`` subpath
    patterns, a directory ``main``), so every barrel under it is public. One
    that cannot be read counts as public.
    """
    import json

    for directory in Path(path).resolve().parents:
        manifest = directory / "package.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if not isinstance(data, dict):
            return True
        if data.get("private") is True:
            return False
        return any(field in data for field in _PUBLIC_FIELDS)
    return False


def removable(
    specifier: Node,
    path: str,
    resolve: Resolve,
    declared: set[tuple[str, str]],
    outside: Outside | None = None,
) -> Hop | None:
    """The :class:`Hop` for ``specifier`` when the run may remove its line along
    with the constant it re-exports, else None. THE one predicate: the
    resolution (:func:`flag_cleanup.cross_file_ts.references`) lets such a line
    go unrefused, and :func:`plan` plans exactly these.

    Requires a barrel that parses cleanly and is not CommonJS (:func:`read`), a
    ``export { NAME } from`` (or ``export { NAME as OTHER } from``) whose chain
    ends in a declared constant, an
    edit this module can make byte for byte (no comment inside the clause; a
    sole specifier's statement is not preceded by a directive comment), and a
    file that no published package names as an entry point."""
    found = reexport_of(specifier, resolve, path)
    if found is None:
        return None
    name, target = found
    end = provider_of(target, name, resolve, outside)
    if not isinstance(end, tuple) or end not in declared:
        return None
    clause = specifier.parent
    statement = clause.parent if clause is not None else None
    if clause is None or statement is None:
        return None
    root = statement
    while root.parent is not None:
        root = root.parent
    if root.has_error or _commonjs(root):
        return None
    if any(c.type == "comment" for c in clause.children):
        return None
    siblings = [c for c in clause.named_children if c.type == "export_specifier"]
    if len(siblings) == 1 and _directive_above(statement):
        return None
    if public_entry(path):
        return None
    source = _string(statement.child_by_field_name("source")) or ""
    return Hop(path, end[1], source, end[0], name)


def _directive_above(statement: Node) -> bool:
    """Whether any comment in the run directly above ``statement`` (blank lines
    included, back to the previous statement or the start of the file) is a
    directive."""
    previous = statement.prev_sibling
    while previous is not None and previous.type == "comment":
        if _DIRECTIVE.match(_text(previous)):
            return True
        previous = previous.prev_sibling
    return False


def plan(
    paths: list[str],
    declared: set[tuple[str, str]],
    resolve: Resolve,
    outside: Outside | None = None,
) -> list[Hop]:
    """Every line of ``paths`` that :func:`removable` accepts."""
    names = {name for _module, name in declared}
    texts: dict[str, str] = {}
    for path in paths:
        if path.endswith((".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")):
            try:
                texts[path] = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    names |= alias_names(texts, names)
    hops: list[Hop] = []
    for path in paths:
        if not path.endswith((".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")):
            continue
        parsed = read(path)
        if parsed is None:
            continue
        root, source = parsed
        if not any(re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", source) for n in names):
            continue
        for statement in root.named_children:
            clause = _clause_of(statement) if statement.type == "export_statement" else None
            if clause is None:
                continue
            for specifier, _name, _alias in _specifiers(clause):
                hop = removable(specifier, path, resolve, declared, outside)
                if hop is not None:
                    hops.append(hop)
    return hops


def _has_module_syntax(root: Node) -> bool:
    return any(c.type in ("import_statement", "export_statement") for c in root.named_children)


def _without(source: str, statement: Node, specifier: Node, clause: Node) -> str:
    """``source`` with ``specifier`` taken out of ``statement``, or the whole
    statement when it was the only one. Raises ``ValueError`` when a comment
    sits in the clause, whose placement this would not keep."""
    raw = source.encode("utf-8")
    if any(c.type == "comment" for c in clause.children):
        raise ValueError("a comment inside the export clause")
    siblings = [c for c in clause.named_children if c.type == "export_specifier"]
    if len(siblings) == 1:
        start, end = statement.start_byte, statement.end_byte
        trailing = re.compile(rb"[ \t]*(?://[^\r\n]*|/\*(?:(?!\*/)[^\r\n])*\*/)?[ \t]*")
        end = trailing.match(raw, end).end()
        if raw[end : end + 2] == b"\r\n":
            end += 2
        elif raw[end : end + 1] == b"\n":
            end += 1
        return (raw[:start] + raw[end:]).decode("utf-8")
    index = siblings.index(specifier)
    if index + 1 < len(siblings):
        start, end = specifier.start_byte, siblings[index + 1].start_byte
    else:
        start, end = siblings[index - 1].end_byte, specifier.end_byte
    return (raw[:start] + raw[end:]).decode("utf-8")


def apply(
    summaries: list,
    hops: list[Hop],
    declared: set[tuple[str, str]],
    resolve: Resolve,
    rewrite: Callable,
    marker: str,
    star_reached: Callable[[str], bool],
    judged_lines: set[tuple[str, str, str, str]] | None = None,
) -> tuple[list, dict[str, str]]:
    """Remove each hop whose constant the run deleted.

    Returns the summaries, with a barrel the run edited added or updated, and
    the reasons for files this could not edit (a hop to a constant that is
    gone, left in place, breaks the build in another file). A file left with
    no ``import`` or ``export`` gets ``marker`` so it stays a module, and so
    does a declaring file emptied of its constant that an ``export *`` still
    reaches (``star_reached``).

    ``judged_lines`` are the lines the resolution let go unrefused
    (:data:`judged`). One whose constant is gone and that was not planned is a
    mismatch between the two, and the run is refused rather than shipped with
    a re-export of a constant that no longer exists.
    """
    from flag_cleanup import cross_file_ts, git_ops

    summaries = list(summaries)
    index = {str(Path(s.path).resolve()): n for n, s in enumerate(summaries)}
    reasons: dict[str, str] = {}

    def current(path: str) -> str | None:
        n = index.get(str(Path(path).resolve()))
        if n is not None:
            return summaries[n].content
        try:
            return Path(path).read_bytes().decode("utf-8")  # bytes: keep CRLF
        except (OSError, UnicodeDecodeError):
            return None

    def newline(text: str) -> str:
        return "\r\n" if "\r\n" in text else "\n"

    def store(path: str, before: str, text: str) -> None:
        git_ops.before_writing([path])
        Path(path).write_bytes(text.encode("utf-8"))
        n = index.get(str(Path(path).resolve()))
        if n is None:
            summaries.append(rewrite(path, before, text))
            index[str(Path(path).resolve())] = len(summaries) - 1
        else:
            summaries[n] = rewrite(path, summaries[n].original_content, text)

    def gone(module: str, name: str) -> bool:
        text = current(module)
        return text is not None and (module, None, name) not in cross_file_ts.declared_names(module, text)

    planned = {(str(Path(h.path).resolve()), h.name, h.source, h.module) for h in hops}
    for line in sorted(judged_lines or ()):
        if line not in planned and gone(line[3], line[1]):
            reasons[line[0]] = (
                "a re-export of a deleted key constant was judged removable but not planned"
            )
    for hop in hops:
        if not gone(hop.module, hop.name):
            continue  # the constant stays: so does the line
        path = hop.path
        before = current(path)
        if before is None:
            reasons[path] = "a re-export of a deleted key constant could not be read"
            continue
        root = ts_syntax._parse(before, _language(path))
        done = False
        for statement in root.named_children:
            clause = _clause_of(statement) if statement.type == "export_statement" else None
            if clause is None or _string(statement.child_by_field_name("source")) != hop.source:
                continue
            for specifier, _n, _a in _specifiers(clause):
                found = reexport_of(specifier, resolve, path)
                if found is None or found[0] != (hop.spelled or hop.name):
                    continue
                try:
                    after = _without(before, statement, specifier, clause)
                except ValueError as err:
                    reasons[path] = f"a re-export of a deleted key constant could not be removed ({err})"
                    after = None
                if after is not None:
                    if not _has_module_syntax(ts_syntax._parse(after, _language(path))):
                        nl = newline(before)
                        after = (after.rstrip("\r\n") + nl if after.strip() else "") + marker + nl
                    store(path, before, after)
                done = True
                break
            if done:
                break
        if not done and path not in reasons:
            reasons[path] = "a re-export of a deleted key constant was not found to remove"
    # A module the constant left empty is still a module to anything re-exporting it whole.
    for module in sorted({m for m, n in declared if gone(m, n)}):
        text = current(module)
        if text is None or _has_module_syntax(ts_syntax._parse(text, _language(module))):
            continue
        if star_reached(module):
            nl = newline(text)
            store(module, text, (text.rstrip("\r\n") + nl if text.strip() else "") + marker + nl)
    return summaries, reasons


_STAR = re.compile(r"\bexport\s*(?:type\s*)?\*")


def star_reach(repo_dir: str, resolve: Resolve) -> Callable[[str], bool]:
    """A question: does any module in the checkout ``export *`` from ``module``?

    The listing is read at most once, and only if asked.
    """
    from flag_cleanup import cross_file_keys

    targets: set[str] | None = None

    def reached(module: str) -> bool:
        nonlocal targets
        if targets is None:
            targets = set()
            for path in cross_file_keys.mention_files(repo_dir, (), exclude=(), also=_STAR):
                parsed = read(path)
                if parsed is None:
                    continue
                for statement in parsed[0].named_children:
                    if statement.type == "export_statement" and _is_star(statement):
                        source = _string(statement.child_by_field_name("source"))
                        found = resolve(path, source) if source is not None else None
                        if found is not None:
                            targets.add(str(Path(found).resolve()))
        return str(Path(module).resolve()) in targets

    return reached
