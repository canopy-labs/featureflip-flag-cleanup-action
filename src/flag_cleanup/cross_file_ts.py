"""TypeScript, TSX and JavaScript profile for cross-file key constants.

A declaration is a module-level ``const NAME = "<key>"`` (Gate 2's own reading
of a key constant) that another file can import, as decided by
:mod:`flag_cleanup.key_const_visibility`. Its module is the declaring file's
absolute path. Whether the declaring file may then lose it is not this
profile's question: a declaration exported any way other than ``export const``
(``export { K as X }``, ``export default K``, ``module.exports = ...``) is a
non-read reference in its own file, and the in-file check keeps it.

A member of an ``export enum`` holding the key as a plain string is an entry
(``Flags.EnableX``), read through a named import or destructured require of
the enum, or through a namespace of its file (``ns.Flags.EnableX``). An entry
is never deleted; a mention of it that is not a proven read is refused with
the entry named. The enum itself is a value that reaches every entry, so
every use of its file and of the enum, in any file, must be one
:func:`enum_escapes` follows. Any other refuses every entry of the enum.

A property of a top-level ``export const Flags = { EnableX: "<key>" } as
const`` is an entry the same way, and everything said of an enum holds for
the object. Only ``as const`` qualifies, and only an object of plain
``name: value`` pairs whose names are all known: a spread, a computed key, a
shorthand, a method or an accessor can supply or replace a property unseen,
and so can a second pair of the same name.

A mention in another file is proven only when that file can mean nothing else:

* ``NAME`` or an alias of it -- bound by ``import { NAME }``,
  ``import { NAME as X }`` or ``const { NAME } = require(...)`` whose
  specifier resolves to the declaring file, at the top level of the file, and
  bound NOWHERE else in the
  file: not by a variable, a destructuring, a parameter of any function, arrow
  or method, a catch clause, a class, function, enum or namespace name, an
  enum member, an assignment, or a second import of any kind (an imported
  ``require`` included, which also stops the require route);
* ``ns.NAME`` -- ``ns`` bound exactly once, by ``import * as ns`` or
  ``const ns = require(...)`` resolving to the declaring file, and used
  nowhere except as ``ns.<member>``: a namespace object that escapes (passed
  along, enumerated, destructured, indexed) can reach the constant unseen.

Only the call shapes the reader rules in ``rules/ts_xconst.toml`` rewrite are
reads, and those are the shapes the shipped seeds fold: the first argument of a
non-optional ``boolVariation(...)``, ``useFeatureFlag(...)`` or
``useBooleanFlagValue(...)`` call, bare or as
a method, of ``await getBooleanValue(...)``, or of a configured accessor.

A bare name that the file binds only itself -- a local, a parameter, its own
``export const`` -- or imports by name from another module that declares that
name itself is another constant sharing the name, and is skipped without a
refusal. The check is file-wide, not scope-aware: a mention free where it
stands is skipped as well while the file binds the name somewhere, which is
safe because a free name can only be a global and an export never is. A
destructuring, a name bound nowhere in the file, or an import from a module
that may re-export ours is refused.

Everything else that names the constant is refused, and one refusal drops the
whole file's reads: a type-only import (never a read, never a binding of a
value), a renaming require destructure (``const { NAME: X } = require(...)``:
the fold removes the read but nothing removes ``NAME:`` from the pattern), a
default import, a re-export through a barrel (``export ... from``),
an import that resolves elsewhere or nowhere, an import left with no read to
remove (it would name a deleted export), a qualifier this profile cannot
prove, an alias of the constant (``import K = m.NAME``: no read through ``K``
is judged), the name inside a string (a dynamic member lookup), a ``with``
statement, an enum member whose name cannot be read (an escape in it, or a
computed name other than a plain string), any mention inside a TS
``namespace``/``module`` block (declaration
merging can supply the name from another file), and a file whose parse has
errors.

Specifiers resolve as the TypeScript compiler resolves them for relative paths
and for ``paths`` in the nearest ``tsconfig.json``/``jsconfig.json``, written
there or inherited through a relative ``extends``. Where the compiler and a
runtime could disagree -- two candidate files, a React Native platform file or
a ``.json`` beside the specifier or the answer (``keys.ios.ts`` beside
``keys/index.ts``, ``keys.json``), a directory with its own ``package.json``, a
config that extends a package or cannot otherwise be read for certain -- the
specifier does not resolve, and the mention is refused. A bare package specifier never
resolves to a file in the repository.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const, key_const_visibility, source_index, ts_syntax

#: Callees whose FIRST argument the reader rules rewrite. `getBooleanValue`
#: only when awaited: the bare call is a `Promise<boolean>`, exactly as in
#: `ts.toml` and `key_const._ts_is_removable_read`.
_READ_CALLEES = frozenset({"boolVariation", "useFeatureFlag", "useBooleanFlagValue", "getBooleanValue"})
_AWAITED_ONLY_CALLEES = frozenset({"getBooleanValue"})

#: What the compiler tries for an extensionless specifier, in its order.
_PLAIN_EXTENSIONS = (
    ".ts", ".tsx", ".d.ts", ".mts", ".cts", ".d.mts", ".d.cts",
    ".js", ".jsx", ".mjs", ".cjs",
)
#: A specifier written with a JavaScript extension names the TypeScript source
#: it compiles from, or the JavaScript file itself.
_WRITTEN_EXTENSIONS = {
    ".js": (".ts", ".tsx", ".d.ts", ".js", ".jsx"),
    ".jsx": (".tsx", ".jsx"),
    ".mjs": (".mts", ".d.mts", ".mjs"),
    ".cjs": (".cts", ".d.cts", ".cjs"),
    ".ts": (".ts",),
    ".tsx": (".tsx",),
    ".mts": (".mts",),
    ".cts": (".cts",),
}
#: The declaration file the compiler reads for a JavaScript file beside it,
#: by the JavaScript file's extension.
DECLARATION_SUFFIXES = {".js": ".d.ts", ".jsx": ".d.ts", ".mjs": ".d.mts", ".cjs": ".d.cts"}
#: React Native platform suffixes: a `<stem>.<platform>.<ext>` beside the
#: compiler's answer is what the bundler picks on that platform.
_PLATFORMS = ("ios", "android", "native", "web", "windows", "macos")
_CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")
_JSONC_NOISE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.DOTALL)
_TRAILING_COMMA = re.compile(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])')
#: A TS `namespace`/`module` block. Inside one a bare name can resolve to a
#: member of the same namespace declared in ANOTHER file (declaration merging
#: in a global script), which no reading of this file can rule out.
_NAMESPACE_BLOCKS = ("internal_module", "module")
_NAME_LEAVES = frozenset({"identifier", "shorthand_property_identifier", "type_identifier"})
#: Expressions that hand their operand on unchanged at run time: `(X)`, `X!`,
#: `X as T`, `X satisfies T`, `<T>X`.
_TRANSPARENT = (
    "parenthesized_expression",
    "non_null_expression",
    "as_expression",
    "satisfies_expression",
    "type_assertion",
)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _language(path: str) -> str:
    """The `ts_syntax` grammar key the engine parses ``path`` with."""
    if path.endswith(".tsx"):
        return "tsx"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "js"  # the TSX grammar, as the engine's JavaScript arm uses
    return "ts"


def _unwrap(node: Node | None) -> Node | None:
    """``node`` with every transparent wrapper (``(X)``, ``X!``, ``X as T``) taken off."""
    while node is not None and node.type in _TRANSPARENT:
        values = [c for c in node.named_children if c.type != "comment"]
        if not values:
            return None
        # `<T>X` puts the type first; every other wrapper, the operand.
        node = values[-1] if node.type == "type_assertion" else values[0]
    return node


def _member_spellings(root: Node, name: str) -> list[Node]:
    """Every node naming member ``name`` of some object, in any form.

    ``X.name``, ``X["name"]``, and a destructuring key ``{ name }`` or
    ``{ name: y }``.
    """
    found: list[Node] = []
    for node in _walk(root):
        parent = node.parent
        if parent is None:
            continue
        if node.type == "property_identifier" and _text(node) == name:
            if parent.type == "member_expression" and parent.child_by_field_name("property") == node:
                found.append(node)
            elif parent.type == "pair_pattern" and parent.child_by_field_name("key") == node:
                found.append(node)
        elif node.type == "shorthand_property_identifier_pattern" and _text(node) == name:
            found.append(node)
        elif (
            node.type == "string"
            and _string_value(node) == name
            and parent.type == "subscript_expression"
            and parent.child_by_field_name("index") == node
        ):
            found.append(node)
    return found


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    language = _language(path)
    root = ts_syntax._parse(source, language)
    if root.has_error:
        return []  # an export hidden in the broken part could rename it
    found: list[xk.KeyDeclaration] = []
    for const in key_const.key_consts(root, source.encode("utf-8"), language, flag_key):
        if not key_const_visibility.visible_key_consts([const], language):
            continue  # not exported: no other file can name it
        found.append(xk.KeyDeclaration(path, const.name, None, path))
    entries, _refused = _enum_entries(path, root, source, flag_key)
    return found + entries


def _enum_entries(
    path: str, root: Node, source: str, flag_key: str
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """The provable members holding ``flag_key``, and a refusal for every other one.

    Provable: an ``export enum`` (or ``export const enum``) at the top of the
    file, not ``declare``d, whose name nothing else in the file declares (a
    second enum or a namespace of that name merges into it), and a member
    with an unquoted name whose initialiser is the key as a plain string.

    Or a property of an ``export const`` at the top of the file whose value
    is an ``as const`` object literal (:func:`as_const_object`) made only of
    ``name: value`` pairs with known names, the property's name unquoted and
    given once, and its value the key as a plain string. A top-level object
    that is not, or a ``let``, is refused like an unprovable enum; an object
    anywhere else, or without ``as const``, is not considered at all.
    """
    names: dict[str, int] = {}
    for node in _walk(root):
        if node.type in ("enum_declaration", "internal_module", "module"):
            named = node.child_by_field_name("name")
            if named is not None:
                names[_text(named)] = names.get(_text(named), 0) + 1
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for enum in _walk(root):
        if enum.type != "enum_declaration":
            continue
        body = enum.child_by_field_name("body")
        members = [m for m in body.named_children if m.type == "enum_assignment"] if body else []
        held = xk.key_token(flag_key)
        holding = [m for m in members if held.search(_text(m.child_by_field_name("value") or m))]
        if not holding:
            continue
        named = enum.child_by_field_name("name")
        owner = _text(named) if named is not None else "?"
        # `export declare enum` nests the enum in an `ambient_declaration`,
        # so its parent is not the export.
        exported = (
            enum.parent is not None
            and enum.parent.type == "export_statement"
            and enum.parent.parent is not None
            and enum.parent.parent.type == "program"
        )
        provable = exported and named is not None and names.get(owner, 0) == 1 and not root.has_error
        # Every member's name, to tell the entry's apart: a second member of
        # the same name (compiled under `@ts-ignore`) is assigned later and
        # wins, however it is spelled.
        spelled = [
            _key_name(_unbracketed(m.child_by_field_name("name") if m.type == "enum_assignment" else m))
            for m in (body.named_children if body else [])
            if m.type != "comment"
        ]
        for member in holding:
            name = member.child_by_field_name("name")
            if name is None or name.type != "property_identifier":
                label = _string_value(name) if name is not None else None
                refused.append(
                    xk.refusal(path, member, xk.UNSUPPORTED_FORM, source, f"{owner}.{label or '?'}")
                )
                continue
            entry = f"{owner}.{_text(name)}"
            if (
                not provable
                or None in spelled
                or spelled.count(_text(name)) != 1
                or _string_value(member.child_by_field_name("value")) != flag_key
            ):
                refused.append(xk.refusal(path, member, xk.ENUM_UNPROVEN, source, entry))
                continue
            found.append(
                xk.KeyDeclaration(
                    path, _text(name), owner, path, kind=xk.ENUM_ENTRY, accessors=frozenset({""})
                )
            )
    for statement in root.named_children:
        exported = statement.type == "export_statement"
        declaration = _declared(statement) if exported else statement
        if declaration is None or declaration.type not in ("lexical_declaration", "variable_declaration"):
            continue
        constant = any(c.type == "const" for c in declaration.children)
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            obj = as_const_object(declarator.child_by_field_name("value"))
            if obj is None:
                continue
            pairs = [p for p in obj.named_children if p.type == "pair"]
            held = xk.key_token(flag_key)
            holding = [p for p in pairs if held.search(_text(p.child_by_field_name("value") or p))]
            if not holding:
                continue
            named = declarator.child_by_field_name("name")
            owner = _text(named) if named is not None and named.type == "identifier" else "?"
            keys = [_key_name(p.child_by_field_name("key")) for p in pairs]
            # Only plain `name: value` pairs, each name known: a spread, a
            # computed key, a shorthand, a method or an accessor can supply or
            # replace a property under a name this reading cannot see, and a
            # `__proto__` pair gives the object a prototype whose getters can
            # hand the object itself out (`Flags.all`).
            closed = (
                len(pairs) == sum(1 for c in obj.named_children if c.type != "comment")
                and None not in keys
                and "__proto__" not in keys
            )
            # An annotation (`Flags: Record<string, string>`) is the type
            # other files see in place of the `as const` one, and it lets them
            # write the property.
            annotated = declarator.child_by_field_name("type") is not None
            provable = (
                exported
                and constant
                and not annotated
                and owner != "?"
                and closed
                and owner not in names
                and not root.has_error
            )
            for member in holding:
                key = member.child_by_field_name("key")
                if key is None or key.type != "property_identifier":
                    label = _string_value(key) if key is not None else None
                    refused.append(
                        xk.refusal(path, member, xk.UNSUPPORTED_FORM, source, f"{owner}.{label or '?'}")
                    )
                    continue
                entry = f"{owner}.{_text(key)}"
                if (
                    not provable
                    or keys.count(_text(key)) != 1
                    or _string_value(member.child_by_field_name("value")) != flag_key
                ):
                    refused.append(xk.refusal(path, member, xk.ENUM_UNPROVEN, source, entry))
                    continue
                found.append(
                    xk.KeyDeclaration(
                        path, _text(key), owner, path, kind=xk.ENUM_ENTRY, accessors=frozenset({""})
                    )
                )
    return found, refused


def as_const_object(value: Node | None) -> Node | None:
    """The object literal of ``{ ... } as const``, or None.

    Also behind ``satisfies T`` (``{ ... } as const satisfies T``) and inside
    parentheses. Only ``as const`` makes the object a fixed set of entries to
    the compiler: a bare object literal is mutable, and nothing else about it
    tells a property it holds from one assigned later.
    """
    if value is not None and value.type == "satisfies_expression":
        value = next((c for c in value.named_children if c.type != "comment"), None)
    if value is None or value.type != "as_expression" or not any(c.type == "const" for c in value.children):
        return None
    operand = next((c for c in value.named_children if c.type != "comment"), None)
    while operand is not None and operand.type == "parenthesized_expression":
        inner = [c for c in operand.named_children if c.type != "comment"]
        operand = inner[0] if len(inner) == 1 else None
    return operand if operand is not None and operand.type == "object" else None


def _key_name(key: Node | None) -> str | None:
    """The property name a pair's key spells; None for a computed key or an
    escape, quoted or not (``Enabl\\u0065X``), which could spell any name."""
    if key is None:
        return None
    if key.type in ("property_identifier", "number"):
        return None if "\\" in _text(key) else _text(key)
    return _string_value(key)


def _unbracketed(name: Node | None) -> Node | None:
    """An enum member's name with a ``["K"]`` bracket taken off; anything
    else computed stays, and has no name :func:`_key_name` can read."""
    if name is not None and name.type == "computed_property_name":
        inner = [c for c in name.named_children if c.type != "comment"]
        if len(inner) == 1 and inner[0].type == "string":
            return inner[0]
    return name


def _declared(export: Node) -> Node | None:
    """What an ``export`` statement declares, past any comment before it
    (``export /* c */ enum Flags``)."""
    return next((c for c in export.named_children if c.type != "comment"), None)


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """What the declaring file says against its own entries.

    Every member holding the key that cannot be proven, and the first
    mention the file spells of a proven one (``X.<entry>``, ``X["<entry>"]``,
    ``{ <entry> } = X``): no reader judges the declaring file's own mentions,
    so one there is reported, never folded. Inside an enum body a member is
    also named bare: ``Legacy = EnableX`` gives ``Flags.Legacy`` the key, and
    no read of ``Flags.Legacy`` is ever judged against the entry.
    """
    root = ts_syntax._parse(source, _language(path))
    found, refused = _enum_entries(path, root, source, flag_key)
    for decl in found:
        spellings = _member_spellings(root, decl.name) + [
            node
            for node in _walk(root)
            if node.type == "identifier"
            and _text(node) == decl.name
            and _within(node, ("enum_body",)) is not None
        ]
        if spellings:
            first = min(spellings, key=lambda n: n.start_byte)
            refused.append(xk.refusal(path, first, xk.UNSUPPORTED_FORM, source, decl.entry))
    return refused


# --- specifier resolution ---------------------------------------------------------


class _Unknown(Exception):
    """A specifier whose target cannot be told with certainty."""


def declaration_file(path: str) -> str | None:
    """The hand-written declaration file of a JavaScript file: ``flags.d.ts``
    for ``flags.js`` (or ``flags.jsx``), ``.d.mts`` for ``.mjs``, ``.d.cts``
    for ``.cjs``; ``None`` for any other file."""
    suffix = Path(path).suffix
    if suffix not in DECLARATION_SUFFIXES:
        return None
    return path[: len(path) - len(suffix)] + DECLARATION_SUFFIXES[suffix]


def _candidates(base: str, beside_declarations: bool = False) -> list[str]:
    """The one existing file ``base`` names, as a one-item list, or ``[]``.

    Raises :class:`_Unknown` when more than one file could be meant. With
    ``beside_declarations``, a JavaScript file and its own declaration file
    (``flags.js`` and ``flags.d.ts``) are one file, the JavaScript: the
    compiler types the import from the declarations, and the code that runs
    is the JavaScript.
    """
    path = Path(base)
    suffix = path.suffix
    if suffix in _WRITTEN_EXTENSIONS and not base.endswith(".d.ts"):
        tries = [str(path.with_suffix(ext)) for ext in _WRITTEN_EXTENSIONS[suffix]]
    else:
        tries = [base + ext for ext in _PLAIN_EXTENSIONS]
        if path.is_dir():
            if (path / "package.json").exists():
                raise _Unknown(base)
            tries += [str(path / f"index{ext}") for ext in _PLAIN_EXTENSIONS]
    found = [t for t in tries if Path(t).is_file()]
    hits = sorted({str(Path(t).resolve()) for t in found})
    if beside_declarations and len(hits) == 2:
        runs = [h for h in hits if declaration_file(h) is not None and declaration_file(h) in hits]
        hits = runs if len(runs) == 1 else hits
    if len(hits) > 1:
        raise _Unknown(base)
    # The specifier's own stem, and the stem of the file the compiler picked.
    # They differ for a directory index: React Native's bundler tries the
    # FILE `keys.ios.ts` before the directory `keys/index.ts` the compiler
    # settles on, so both are asked.
    stems = {base}
    if suffix in _WRITTEN_EXTENSIONS:
        stems.add(str(path.with_suffix("")))
    if found:
        ext = max((e for e in _PLAIN_EXTENSIONS if found[0].endswith(e)), key=len, default="")
        stems.add(found[0][: len(found[0]) - len(ext)])
    if any(_bundler_alternative(stem) for stem in stems):
        raise _Unknown(base)
    return hits


def _bundler_alternative(stem: str) -> bool:
    """Whether a bundler could load another file than the compiler for ``stem``.

    React Native's bundler resolves `./keys` to `keys.ios.ts` on iOS and to
    `keys.ts` only where no platform file exists, so the compiler's answer is
    one platform's at best; and it tries `.json` before `.ts`, so a
    `keys.json` (or `keys.ios.json`) wins there too. Only the platform names
    count: `keys.test.ts` is not a candidate for `./keys` anywhere.
    """
    if Path(f"{stem}.json").is_file():
        return True
    return any(
        Path(f"{stem}.{platform}{e}").is_file()
        for platform in _PLATFORMS
        for e in (*_PLAIN_EXTENSIONS, ".json")
    )


def _strip_jsonc(text: str) -> str:
    text = _JSONC_NOISE.sub(lambda m: m.group(1) or "", text)
    return _TRAILING_COMMA.sub(lambda m: m.group(1) or m.group(2), text)


@dataclass(frozen=True)
class _Options:
    """The module-resolution options a config compiles with, read through its
    ``extends`` chain (:func:`_options`)."""

    #: The ``paths`` in force, each target as the directory it resolves from
    #: and the rest as written, which may hold the ``*``.
    paths: dict[str, list[tuple[Path, str]]]
    #: The explicit ``baseUrl`` in force, as a directory; None when no config
    #: in the chain sets one.
    base_url: Path | None

    def targets(self, specifier: str) -> list[str] | None:
        """The paths of the pattern the compiler picks for ``specifier``,
        substituted; None when no pattern fits."""
        fit = _fit(specifier, self.paths)
        if fit is None:
            return None
        pattern, star = fit
        return [str(origin / rest.replace("*", star)) for origin, rest in self.paths[pattern]]


def _options(config: Path, stop: Path, strict: bool = True) -> _Options:
    """``config``'s ``paths`` and ``baseUrl``, as the compiler reads them.

    Each is the value of the nearest config in the ``extends`` chain that sets
    it (:func:`_inheriting`). ``baseUrl`` resolves from the config that
    declares it. A ``paths`` target resolves from the ``baseUrl`` in force, or
    else from the config that declares ``paths``. A ``${configDir}`` at the
    start of either is ``config``'s own directory. Raises :class:`_Unknown`
    for whatever :func:`_inheriting` and :func:`_in_force` refuse, for a
    ``paths`` that is not a map of string lists, for a misspelt
    ``${configDir}``, and for any ``moduleSuffixes`` but ``[""]``, under which
    ``keys.mobile.ts`` can answer ``@/keys``.

    Not ``strict``, it reads what a config MAY alias, for the questions that
    may only over-attribute a specifier to a file (:func:`_alias_keys`,
    :func:`_enum_escapes`): ``references``, ``moduleSuffixes``, a repeated key
    and U+2028/U+2029 are read past rather than refused. A refusal there would
    hide an alias, and with it a use of the file, which fails open.
    """
    try:
        chain = _inheriting(config, stop, (), strict)
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError) as error:
        # RuntimeError: a symlink loop, from `Path.resolve`.
        raise _Unknown(str(config)) from error
    paths, paths_in = _in_force(chain, "paths", {})
    if not isinstance(paths, dict):
        raise _Unknown(str(paths_in))
    for targets in paths.values():
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise _Unknown(str(paths_in))
    suffixes, suffixes_in = _in_force(chain, "moduleSuffixes", [""])
    if strict and suffixes != [""]:
        raise _Unknown(str(suffixes_in))
    written, base_in = _in_force(chain, "baseUrl", None)
    base_url = None
    if base_in is not None:
        if not isinstance(written, str):
            raise _Unknown(str(base_in))
        base_url = _option_path(written, base_in.parent, config, base_in)
    origin = base_url if base_url is not None else paths_in.parent if paths_in is not None else config.parent
    resolved = {key: [_target(target, origin, config, paths_in) for target in targets] for key, targets in paths.items()}
    return _Options(resolved, base_url)


def _target(value: str, origin: Path, config: Path, declared_in: Path | None) -> tuple[Path, str]:
    """The directory path option ``value`` resolves from, and the rest of it:
    ``origin``, or, after a leading ``${configDir}``, ``config``'s directory.
    A misspelt template raises :class:`_Unknown`: the compiler tests for it
    ignoring case and replaces it only as written, so it would resolve from a
    third place."""
    rest = value[len(_CONFIG_DIR) :] if value.startswith(_CONFIG_DIR) else None
    if _CONFIG_DIR.lower() in (value if rest is None else rest).lower():
        raise _Unknown(str(declared_in))
    if rest is None:
        return origin, value
    # The compiler replaces the template with `./` and resolves that from the
    # config compiled with, wherever the option is written.
    return config.parent, f"./{rest}"


def _option_path(value: str, origin: Path, config: Path, declared_in: Path | None) -> Path:
    """The directory path option ``value`` names (see :func:`_target`)."""
    directory, rest = _target(value, origin, config, declared_in)
    return directory / rest


def _config(
    reader_dir: Path, stop: Path, strict: bool = True, solution: Path | None = None
) -> _Options | None:
    """The options of the nearest config, or None when there is none.

    Raises :class:`_Unknown` when it cannot be read for certain (see
    :func:`_options`), or when two configs sit side by side. With
    ``solution`` (the reader's own path), a nearest config that delegates
    through ``references`` is read through the configs it names
    (:func:`_solution_options`) instead of being refused.
    """
    current = reader_dir
    while True:
        present = [current / name for name in _CONFIG_NAMES if (current / name).is_file()]
        if len(present) > 1:
            raise _Unknown(str(current))
        if present:
            if solution is not None and strict:
                delegated = _solution_options(present[0], solution, stop)
                if delegated is not None:
                    return delegated
            return _options(present[0], stop, strict)
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _strict_config(config: Path) -> dict:
    """``config`` parsed the way :func:`_inheriting` parses it strictly."""
    try:
        text = config.read_text(encoding="utf-8")
        if " " in text or " " in text:
            raise _Unknown(str(config))
        data = json.loads(_strip_jsonc(text), object_pairs_hook=_no_repeats)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise _Unknown(str(config)) from error
    if not isinstance(data, dict):
        raise _Unknown(str(config))
    return data


def _solution_options(config: Path, reader: Path, stop: Path) -> _Options | None:
    """The options ``reader`` compiles with under a SOLUTION config, or None
    when ``config`` has no ``references``.

    A solution config (`"files": []` plus ``references``, Vite's default
    layout) compiles nothing itself: each file belongs to whichever referenced
    config includes it. Every referenced config that MAY compile ``reader``
    (:func:`_may_compile`, which over-matches) is read strictly, and the
    answer stands only when they all read the same options. Raises
    :class:`_Unknown` when none may, when they disagree, when the solution
    config may compile ``reader`` itself, or when a reference cannot be read.
    """
    data = _strict_config(config)
    if "references" not in data:
        return None
    references = data["references"]
    if not isinstance(references, list) or _may_compile(config, data, reader):
        raise _Unknown(str(config))
    found: list[_Options] = []
    for reference in references:
        written = reference.get("path") if isinstance(reference, dict) else None
        if not isinstance(written, str):
            raise _Unknown(str(config))
        target = Path(os.path.normpath(config.parent / written))
        if target.is_dir():
            target = target / "tsconfig.json"
        if not target.is_file() or not target.resolve().is_relative_to(stop):
            raise _Unknown(str(config))
        if _may_compile(target, _strict_config(target), reader):
            found.append(_options(target, stop))
    if not found or any(options != found[0] for options in found):
        raise _Unknown(str(config))
    return found[0]


def _may_compile(config: Path, data: dict, reader: Path) -> bool:
    """Whether ``config``'s ``files`` or ``include`` may hold ``reader``.

    Over-matches on purpose, since every config that may hold it has to
    agree: an ``include`` pattern counts as the directory before its first
    wildcard, ``exclude`` is ignored, and a config that sets neither but
    ``extends`` another counts, its inherited lists unread.
    """
    files, include = data.get("files"), data.get("include")
    target = reader.resolve()
    if files is None and include is None:
        return "extends" in data or target.is_relative_to(config.parent.resolve())
    if files is not None:
        if not isinstance(files, list):
            return True
        for entry in files:
            if not isinstance(entry, str) or Path(os.path.normpath(config.parent / entry)).resolve() == target:
                return True
    if include is not None:
        if not isinstance(include, list):
            return True
        for pattern in include:
            if not isinstance(pattern, str):
                return True
            pattern = pattern.replace("\\", "/")
            if pattern.startswith(_CONFIG_DIR):
                pattern = "." + pattern[len(_CONFIG_DIR) :]
            literal = []
            for part in pattern.split("/"):
                if any(ch in part for ch in "*?[{$"):
                    break
                literal.append(part)
            base = Path(os.path.normpath(config.parent.joinpath(*literal))).resolve()
            if target == base or target.is_relative_to(base):
                return True
    return False


def _fit(specifier: str, patterns: Iterable[str]) -> tuple[str, str] | None:
    """The ``paths`` pattern the compiler picks for ``specifier``, with what
    its ``*`` stands for; None when none fits."""
    patterns = list(patterns)
    if specifier in patterns:
        return specifier, ""
    best: tuple[int, str, str] | None = None
    for pattern in patterns:
        if pattern.count("*") != 1:
            continue
        prefix, suffix = pattern.split("*")
        if (
            specifier.startswith(prefix)
            and specifier.endswith(suffix)
            and len(specifier) >= len(prefix) + len(suffix)
            and (best is None or len(prefix) > best[0])
        ):
            best = (len(prefix), pattern, specifier[len(prefix) : len(specifier) - len(suffix)])
    return None if best is None else (best[1], best[2])


def _resolve(
    reader_path: str,
    specifier: str,
    stop: Path,
    beside_declarations: bool = False,
    base_url: Callable[[str, str], bool] | None = None,
    strict: bool = True,
    solution: bool = False,
) -> str | None:
    reader_dir = Path(reader_path).parent
    try:
        if specifier in (".", "..") or specifier.startswith(("./", "../")):
            hits = _candidates(os.path.normpath(reader_dir / specifier), beside_declarations)
            return hits[0] if hits else None
        if specifier.startswith("/") or not specifier:
            return None
        options = _config(reader_dir, stop, strict, Path(reader_path) if solution else None)
        if options is None:
            return None
        targets = options.targets(specifier)
        for target in targets or ():
            hits = _candidates(os.path.normpath(target), beside_declarations)
            if hits:
                return hits[0]
        if targets is None and base_url is not None and options.base_url is not None:
            # No `paths` pattern fits, so the compiler tries the specifier
            # under an explicit `baseUrl` before any package. Where a pattern
            # fits and names nothing, it is left there: which of the two the
            # compiler falls back to is not worth betting a rewrite on.
            hits = _candidates(os.path.normpath(options.base_url / specifier), beside_declarations)
            if hits and base_url(reader_path, specifier):
                return hits[0]
    except _Unknown:
        return None
    return None


def _stop_for(repo_dir: str) -> Path:
    from flag_cleanup.piranha_runner import checkout_root

    return Path(checkout_root(repo_dir)).resolve()


def resolve_specifier(
    reader_path: str,
    specifier: str,
    repo_dir: str,
    *,
    beside_declarations: bool = False,
    base_url: Callable[[str, str], bool] | None = None,
    solution: bool = False,
) -> str | None:
    """The absolute path of the file ``specifier`` imports from ``reader_path``.

    ``None`` for a package, for anything the compiler and a runtime could read
    differently, and for anything that does not exist. The nearest
    ``tsconfig.json``/``jsconfig.json`` is looked for up to the root of the git
    checkout holding ``repo_dir``, and read with the options it inherits
    through ``extends`` (:func:`_options`). A bare specifier resolves only
    through ``paths`` (or an explicit ``baseUrl``, below), which both module
    resolution modes try first, so classic resolution's directory walk never
    changes an answer given here.

    Two readings are off unless asked for, since the key-constant
    resolution's candidate scans (:func:`enum_module_pattern`) do not account
    for them. ``beside_declarations`` takes a JavaScript file to be the answer
    where its own declaration file sits beside it (see :func:`_candidates`).
    ``base_url(reader_path, specifier)`` lets a bare specifier that no
    ``paths`` pattern fits resolve under the config's explicit ``baseUrl``,
    when it returns true: a runtime may still read such a specifier as a
    package, which only the caller can rule out. ``solution`` reads a nearest
    config that delegates through ``references`` by the configs it names
    (:func:`_solution_options`); otherwise such a config answers nothing.
    """
    return _resolve(
        reader_path, specifier, _stop_for(repo_dir), beside_declarations, base_url, solution=solution
    )


#: Every config a project can compile with, not only the two names
#: :func:`_config` looks for: `tsc -p tsconfig.app.json` is a Vite default.
_ANY_CONFIG = re.compile(r"^[tj]sconfig.*\.jsonc?$", re.IGNORECASE)


def names_package(reader_path: str, specifier: str, repo_dir: str) -> bool:
    """Whether no compiler config can read bare ``specifier`` as anything but
    a package.

    Which config compiles a file is not decidable here (a solution config's
    ``references``, a ``tsconfig.app.json`` beside the ``tsconfig.json``, a
    nested config that may or may not be the one in use), so EVERY
    ``tsconfig*.json`` and ``jsconfig*.json`` from the reader's directory up to
    the checkout root is asked, with the options it inherits through
    ``extends`` (:func:`_inheriting`). False when any of them has a ``paths``
    pattern that fits the specifier, whether or not it names a file, or a file
    under its ``baseUrl`` that answers it, and when any of them, or any config
    it extends, cannot be read for certain: unparseable, delegating
    (``references``) to another, or extending a package or a file outside the
    checkout. Also False when a file answers the name in the reader's
    directory or one above it, which is where classic module resolution looks.
    """
    if not specifier or specifier.startswith((".", "/", "#")):
        return False
    current = Path(reader_path).parent
    stop = _stop_for(repo_dir)
    try:
        while True:
            if _candidates(os.path.normpath(current / specifier)):
                return False
            for config in sorted(current.iterdir()):
                if _ANY_CONFIG.match(config.name) and config.is_file() and _answers(config, specifier, stop):
                    return False
            if current == stop or current.parent == current:
                return True
            current = current.parent
    except (_Unknown, OSError, UnicodeDecodeError, ValueError):
        return False


def package_scope(reader_path: str, specifier: str, repo_dir: str) -> str | None:
    """The directory of the ``package.json`` a runtime looks bare
    ``specifier`` up from, when ``reader_path``'s compiler can read it only as
    a package; ``None`` otherwise.

    The nearest config, read as :func:`resolve_specifier` reads it with
    ``solution``, must have no ``paths`` pattern that fits the specifier and
    no ``baseUrl`` file that answers it, and no directory from the reader's
    up to that ``package.json`` may hold a file answering it (classic
    resolution) or, below the package's own, a ``node_modules`` (a nearer
    install). Two importers with one scope then load one module for it.
    """
    if not specifier or specifier.startswith((".", "/")):
        return None
    reader = Path(reader_path)
    stop = _stop_for(repo_dir)
    try:
        options = _config(reader.parent, stop, True, reader)
        if options is not None and (
            options.targets(specifier) is not None
            or options.base_url is not None
            and _candidates(os.path.normpath(options.base_url / specifier))
        ):
            return None
        current = reader.parent.resolve()
        while True:
            if _candidates(os.path.normpath(current / specifier)):
                return None
            if (current / "package.json").is_file():
                return str(current)
            if os.path.lexists(current / "node_modules") or current == stop or current.parent == current:
                return None
            current = current.parent
    except (_Unknown, OSError):
        return None


def _answers(config: Path, specifier: str, stop: Path) -> bool:
    """Whether ``config`` can read ``specifier`` as a file; raises
    :class:`_Unknown` when that cannot be told."""
    options = _options(config, stop)
    if options.targets(specifier) is not None:
        return True
    return options.base_url is not None and bool(_candidates(os.path.normpath(options.base_url / specifier)))


#: The options naming what a classic-runtime JSX element and fragment call.
JSX_FACTORY_OPTIONS = ("jsxFactory", "jsxFragmentFactory")


def jsx_factories(reader_path: str, repo_dir: str) -> dict[str, frozenset[str | None]]:
    """Each of :data:`JSX_FACTORY_OPTIONS`, as the configs nearest
    ``reader_path`` set it, read through their ``extends`` chains.

    The nearest configs are every ``tsconfig*.json`` and ``jsconfig*.json`` in
    the first directory holding one, from the reader's up to the checkout
    root: which of them compiles the file is not decidable here (see
    :func:`names_package`). ``None`` among the values stands for the compiler's
    default. It is there when no config is found, when one leaves the option
    unset or sets it to anything but a string, and when one or a config it
    extends cannot be read: the caller keeps an import for every name in the
    answer, so an answer too wide costs a missed tidy and one too narrow
    breaks the build.
    """
    values: dict[str, set[str | None]] = {option: set() for option in JSX_FACTORY_OPTIONS}
    try:
        current = Path(reader_path).resolve().parent
        stop = _stop_for(repo_dir)
        while True:
            configs = sorted(c for c in current.iterdir() if _ANY_CONFIG.match(c.name) and c.is_file())
            if configs or current == stop or current.parent == current:
                break
            current = current.parent
    except (OSError, RuntimeError):
        # RuntimeError: a symlink loop, from `Path.resolve`. A config that
        # cannot be looked for may be the nearest, so none farther is used.
        return {option: frozenset({None}) for option in JSX_FACTORY_OPTIONS}
    for config in configs:
        chain = _loose_chain(config, stop, ())
        for option in JSX_FACTORY_OPTIONS:
            for options in chain:
                if options is None:
                    # An unreadable link could set it: its default stands in,
                    # and what a later link sets is read on as well.
                    values[option].add(None)
                    continue
                if option in options:
                    value = options[option]
                    values[option].add(value if isinstance(value, str) else None)
                    break
            else:
                values[option].add(None)
    return {option: frozenset(found or {None}) for option, found in values.items()}


#: The ``jsx`` settings of the automatic runtime, lowercased as the compiler
#: compares them: an element calls a function the compiler imports itself.
AUTOMATIC_JSX = frozenset({"react-jsx", "react-jsxdev"})


def jsx_automatic(reader_path: str, repo_dir: str) -> bool:
    """Whether every config that may compile ``reader_path`` sets ``jsx`` to
    the automatic runtime (:data:`AUTOMATIC_JSX`), read through its
    ``extends`` chain.

    The configs are those :func:`jsx_factories` reads, narrowed to the ones
    whose ``files`` or ``include`` may hold the reader (:func:`_may_compile`,
    which over-matches): a solution config compiling nothing and a build
    script's config beside the app's do not count. False when none may
    compile it, when one leaves ``jsx`` unset or sets anything else, and when
    one or a config it extends cannot be read: the caller keeps imports a
    classic-runtime element could read unless this is True, so an answer
    wrongly False costs a missed tidy and one wrongly True breaks the build.
    """
    try:
        reader = Path(reader_path).resolve()
        current = reader.parent
        stop = _stop_for(repo_dir)
        while True:
            configs = sorted(c for c in current.iterdir() if _ANY_CONFIG.match(c.name) and c.is_file())
            if configs or current == stop or current.parent == current:
                break
            current = current.parent
        compiling = []
        for config in configs:
            data = json.loads(_strip_jsonc(config.read_text(encoding="utf-8")))
            if not isinstance(data, dict):
                return False
            if _may_compile(config, data, reader):
                compiling.append(config)
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
        return False
    if not compiling:
        return False
    for config in compiling:
        for options in _loose_chain(config, stop, ()):
            if options is None:
                return False
            if "jsx" in options:
                value = options["jsx"]
                if not isinstance(value, str) or value.lower() not in AUTOMATIC_JSX:
                    return False
                break
        else:
            return False
    return True


def _loose_chain(config: Path, stop: Path, below: tuple[Path, ...]) -> list[dict | None]:
    """``config``'s ``compilerOptions`` and those of every config it
    ``extends``, nearest first as :func:`_inheriting` orders them, with None
    standing for each link that cannot be read or followed (a package, a
    missing file, a cycle) rather than failing the whole chain."""
    try:
        identity = config.resolve()
        if identity in below or len(below) >= _EXTENDS_DEPTH:
            return [None]
        data = json.loads(_strip_jsonc(config.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
        return [None]
    if not isinstance(data, dict):
        return [None]
    options = data.get("compilerOptions", {})
    extends = data.get("extends", [])
    if isinstance(extends, str):
        extends = [extends]
    chain: list[dict | None] = [options if isinstance(options, dict) else None]
    if not isinstance(extends, list):
        return [*chain, None]
    for name in reversed(extends):
        try:
            extended = _extended(config, name, stop) if isinstance(name, str) else None
        except (_Unknown, OSError, RuntimeError):
            extended = None
        chain.extend([None] if extended is None else _loose_chain(extended, stop, (*below, identity)))
    return chain


#: The template TypeScript 5.5 replaces, at the start of a path option, with
#: the directory of the config a project is compiled with.
_CONFIG_DIR = "${configDir}"

#: How many configs deep an ``extends`` chain is followed before it counts as
#: unreadable.
_EXTENDS_DEPTH = 16


def _inheriting(config: Path, stop: Path, below: tuple[Path, ...], strict: bool = True) -> list[tuple[Path, dict]]:
    """``config``'s ``compilerOptions`` and those of every config it
    ``extends``, nearest first: the first that sets an option sets it.

    An array ``extends`` merges left to right, so its last entry comes first.
    Raises :class:`_Unknown` when any config in the chain cannot be read for
    certain, delegates (``references``), or extends what :func:`_extended`
    cannot follow, and on a cycle or a chain deeper than
    :data:`_EXTENDS_DEPTH`. ``below`` is the chain that led here, so a base
    two configs share is not taken for a cycle. Not ``strict``,
    ``references``, a repeated key and U+2028/U+2029 are read past (see
    :func:`_options`).
    """
    identity = config.resolve()
    if identity in below or len(below) >= _EXTENDS_DEPTH:
        raise _Unknown(str(config))
    text = config.read_text(encoding="utf-8")
    if strict and ("\u2028" in text or "\u2029" in text):
        # The compiler ends a line comment there; `_strip_jsonc` does not.
        raise _Unknown(str(config))
    data = json.loads(_strip_jsonc(text), object_pairs_hook=_no_repeats if strict else None)
    if not isinstance(data, dict) or (strict and "references" in data):
        raise _Unknown(str(config))
    options = data.get("compilerOptions", {})
    extends = data.get("extends", [])
    if isinstance(extends, str):
        extends = [extends]
    if (
        not isinstance(options, dict)
        or not isinstance(extends, list)
        or not all(isinstance(e, str) for e in extends)
    ):
        raise _Unknown(str(config))
    chain = [(config, options)]
    for name in reversed(extends):
        chain.extend(_inheriting(_extended(config, name, stop), stop, (*below, identity), strict))
    return chain


def _no_repeats(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object, refusing a repeated key: the compiler merges two
    ``compilerOptions`` objects where a JSON reader keeps the last."""
    data = dict(pairs)
    if len(data) != len(pairs):
        raise _Unknown("a repeated key")
    return data


def _extended(config: Path, name: str, stop: Path) -> Path:
    """The config file ``config``'s ``extends`` entry ``name`` names.

    Only a relative path is followed, as the compiler reads it: from
    ``config``'s directory, with ``.json`` added when the name as written is
    not a file and does not end in it. Anything else is a package, which lives
    in a ``node_modules`` the checkout does not hold, or an absolute path; both
    raise :class:`_Unknown`, as does a file that is missing or outside the
    checkout.
    """
    spec = name.replace("\\", "/")
    if not spec.startswith(("./", "../")):
        raise _Unknown(name)
    target = Path(os.path.normpath(config.parent / spec))
    if not target.is_file() and not spec.endswith(".json"):
        target = target.with_name(f"{target.name}.json")
    if not target.is_file() or not target.resolve().is_relative_to(stop):
        raise _Unknown(name)
    return target


def _in_force(chain: list[tuple[Path, dict]], option: str, default: object) -> tuple[object, Path | None]:
    """The value of ``option`` the first config in ``chain`` to set it gives,
    with that config; ``default`` and None when none sets it. An option set to
    ``null`` raises :class:`_Unknown`: whether that clears an inherited value or
    leaves it in force is not worth betting a deletion on."""
    for config, options in chain:
        if option in options:
            if options[option] is None:
                raise _Unknown(str(config))
            return options[option], config
    return default, None


# --- what the reader binds --------------------------------------------------------


@dataclass(frozen=True)
class _Binding:
    """One import or require that introduces a local name."""

    node: Node
    local: str
    #: The declaration a named binding is of; None for a namespace or a name
    #: that is not one of the declarations.
    declaration: xk.KeyDeclaration | None
    #: The declaring module a namespace binding is of.
    namespace_of: str | None
    #: A value binding at the top of the file: what a proof needs, besides
    #: being the ONLY binding of its name.
    provable: bool
    type_only: bool = False
    #: A require of the declaring file through a `require` the file binds
    #: itself: whatever it returns, it is not proven to be that module.
    require_rebound: bool = False
    #: A form the reader rules cannot clean up after: the declaration's name
    #: would survive the fold in the file, naming an export the run deletes.
    unsupported: bool = False
    #: The file a named import resolves to, whatever it is.
    module: str | None = None
    #: A named import or destructured require of an enum holding a key:
    #: ``Flags`` in ``Flags.EnableX``. (module, enum name).
    enum_of: tuple[str, str] | None = None


def _string_value(node: Node | None) -> str | None:
    """The text of a plain string literal; None for one with escapes or none."""
    if node is None or node.type != "string":
        return None
    parts = node.named_children
    if not parts:
        return ""
    if len(parts) != 1 or parts[0].type != "string_fragment":
        return None
    return _text(parts[0])


def _import_bindings(
    statement: Node,
    resolve,
    declared: dict[tuple[str, str], xk.KeyDeclaration],
    modules: frozenset[str],
    enums: dict[tuple[str, str], dict[str, xk.KeyDeclaration]],
) -> list[_Binding]:
    top = statement.parent is not None and statement.parent.type == "program"
    type_only = any(c.type == "type" for c in statement.children)
    spec = _string_value(statement.child_by_field_name("source"))
    module = resolve(spec) if spec is not None else None
    found: list[_Binding] = []
    for part in statement.named_children:
        if part.type == "import_require_clause":
            name = next((c for c in part.named_children if c.type == "identifier"), None)
            if name is not None:
                found.append(_Binding(name, _text(name), None, None, False))
            continue
        if part.type != "import_clause":
            continue
        for clause in part.named_children:
            if clause.type == "identifier":  # a default import: out of scope
                found.append(_Binding(clause, _text(clause), None, None, False))
            elif clause.type == "namespace_import":
                name = next((c for c in clause.named_children if c.type == "identifier"), None)
                if name is None:
                    continue
                ours = module if module in modules else None
                found.append(
                    _Binding(name, _text(name), None, ours, top and not type_only and ours is not None, type_only)
                )
            elif clause.type == "named_imports":
                for specifier in clause.named_children:
                    if specifier.type != "import_specifier":
                        continue
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias")
                    local = alias if alias is not None else imported
                    if imported is None or local is None or local.type != "identifier":
                        continue
                    spec_type_only = type_only or any(c.type == "type" for c in specifier.children)
                    decl = (
                        declared.get((module, _text(imported)))
                        if module is not None and imported.type == "identifier"
                        else None
                    )
                    enum_of = (
                        (module, _text(imported))
                        if decl is None and module is not None and (module, _text(imported)) in enums
                        else None
                    )
                    found.append(
                        _Binding(
                            local,
                            _text(local),
                            decl,
                            None,
                            top and not spec_type_only and (decl is not None or enum_of is not None),
                            spec_type_only,
                            module=module,
                            enum_of=enum_of,
                        )
                    )
    return found


def _require_bindings(
    declarator: Node,
    resolve,
    declared: dict[tuple[str, str], xk.KeyDeclaration],
    modules: frozenset[str],
    require_is_global: bool,
    enums: dict[tuple[str, str], dict[str, xk.KeyDeclaration]],
) -> list[_Binding]:
    value = declarator.child_by_field_name("value")
    if value is None or value.type != "call_expression":
        return []
    function = value.child_by_field_name("function")
    args = value.child_by_field_name("arguments")
    if function is None or function.type != "identifier" or _text(function) != "require":
        return []
    if args is None or len(args.named_children) != 1:
        return []
    spec = _string_value(args.named_children[0])
    module = resolve(spec) if spec is not None else None
    if module not in modules:  # `modules` holds the enums' files as well
        return []
    lexical = declarator.parent
    provable = (
        require_is_global
        and lexical is not None
        and lexical.type == "lexical_declaration"
        and any(c.type == "const" for c in lexical.children)
        and lexical.parent is not None
        and lexical.parent.type == "program"
    )
    target = declarator.child_by_field_name("name")
    found: list[_Binding] = []
    if target is None:
        return found
    rebound = not require_is_global
    if target.type == "identifier":
        found.append(_Binding(target, _text(target), None, module, provable, require_rebound=rebound))
    elif target.type == "object_pattern":
        for prop in target.named_children:
            if prop.type == "shorthand_property_identifier_pattern":
                decl = declared.get((module, _text(prop)))
                enum_of = (module, _text(prop)) if decl is None and (module, _text(prop)) in enums else None
                found.append(
                    _Binding(
                        prop,
                        _text(prop),
                        decl,
                        None,
                        provable and (decl is not None or enum_of is not None),
                        require_rebound=rebound,
                        enum_of=enum_of,
                    )
                )
            elif prop.type == "pair_pattern":
                key = prop.child_by_field_name("key")
                local = prop.child_by_field_name("value")
                if key is None or local is None or local.type != "identifier":
                    continue
                decl = (
                    declared.get((module, _text(key)))
                    if key.type == "property_identifier"
                    else None
                )
                # `{ NAME: X }`: the fold removes the read of `X`, and nothing
                # removes `NAME:` from the pattern, so the file would go on
                # naming an export the same run deletes. An enum is never
                # deleted, so `{ Flags: F }` names nothing that goes away.
                enum_of = (
                    (module, _text(key))
                    if decl is None and key.type == "property_identifier" and (module, _text(key)) in enums
                    else None
                )
                found.append(
                    _Binding(
                        local,
                        _text(local),
                        decl,
                        None,
                        provable and (decl is not None or enum_of is not None),
                        require_rebound=rebound,
                        unsupported=decl is not None,
                        enum_of=enum_of,
                    )
                )
    return found


def _within(node: Node, kinds: tuple[str, ...]) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type in kinds:
            return parent
        parent = parent.parent
    return None


def _is_read(argument: Node, accessors: frozenset[str]) -> bool:
    """First argument of a call the reader rules rewrite.

    Mirrors `key_const._ts_is_removable_read` and the seeds in `ts.toml`: no
    optional call, no optional member callee, and `getBooleanValue` only under
    an `await`.
    """
    args = argument.parent
    if args is None or args.type != "arguments" or not args.named_children:
        return False
    if args.named_children[0].id != argument.id:
        return False
    call = args.parent
    if call is None or call.type != "call_expression":
        return False
    if any(c.type == "?." for c in call.children):
        return False
    function = call.child_by_field_name("function")
    if function is None:
        return False
    if function.type == "member_expression":
        if function.child_by_field_name("optional_chain") is not None:
            return False
        if any(c.type == "?." for c in function.children):
            return False
        function = function.child_by_field_name("property")
    if function is None or function.type not in ("identifier", "property_identifier"):
        return False
    callee = _text(function)
    if callee not in _READ_CALLEES and callee not in accessors:
        return False
    if callee in _AWAITED_ONLY_CALLEES and callee not in accessors:
        parent = call.parent
        return parent is not None and parent.type == "await_expression"
    return True


def _literal(node: Node | None) -> str | None:
    """A plain string's text, or a template literal's with nothing substituted."""
    if node is not None and node.type == "template_string":
        parts = node.named_children
        if not parts:
            return ""
        if len(parts) == 1 and parts[0].type == "string_fragment":
            return _text(parts[0])
        return None
    return _string_value(node)


def _is_take(node: Node | None, member: str) -> bool:
    """Whether ``node`` takes ``member`` off something: ``<expr>.member`` or
    ``<expr>["member"]``."""
    if node is None:
        return False
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return prop is not None and _text(prop) == member
    if node.type == "subscript_expression":
        return _literal(node.child_by_field_name("index")) == member
    return False


#: What a binder may sit in on its way up to the declaration binding it.
_PATTERN_PARTS = frozenset(
    {"object_pattern", "array_pattern", "pair_pattern", "object_assignment_pattern", "assignment_pattern", "rest_pattern"}
)


def _takes_member(binder: Node, member: str) -> bool:
    """Whether ``binder`` is declared as ``member`` taken off something:
    ``const F = <expr>.member`` or ``= <expr>["member"]``.

    Only the one take a name may hold, ``const F = require("./flags").Flags``,
    is followed to the reads through it. Every other take is refused where it
    is taken, this one included when it is not that exact shape."""
    holder = binder.parent
    return (
        holder is not None
        and holder.type == "variable_declarator"
        and holder.child_by_field_name("name") == binder
        and _is_take(_unwrap(holder.child_by_field_name("value")), member)
    )


#: Nodes below which a name is a type, erased before the code runs.
_TYPE_CONTEXTS = frozenset(
    {
        "type_annotation", "adding_type_annotation", "opting_type_annotation",
        "omitting_type_annotation", "type_predicate_annotation", "type_query",
        "type_arguments", "type_parameters", "type_parameter", "type_alias_declaration",
        "interface_declaration", "nested_type_identifier", "index_type_query",
        "implements_clause", "extends_type_clause", "mapped_type_clause",
        "ambient_declaration", "array_type", "conditional_type", "constructor_type",
        "default_type", "existential_type", "flow_maybe_type", "function_type",
        "generic_type", "infer_type", "intersection_type", "literal_type", "lookup_type",
        "object_type", "optional_type", "parenthesized_type", "predefined_type",
        "readonly_type", "rest_type", "template_literal_type", "template_type",
        "this_type", "tuple_type", "union_type",
    }
)


def _in_type(node: Node) -> bool:
    """Whether ``node`` sits in a type (``let k: Flags.X``, ``keyof typeof Flags``)."""
    current: Node | None = node
    while current is not None:
        if current.type in _TYPE_CONTEXTS:
            return True
        current = current.parent
    return False


def _member_of(node: Node) -> Node | None:
    """The ``node.<name>`` that ``node`` is the object of, if any: never ``node[...]``."""
    parent = node.parent
    if parent is None or parent.type != "member_expression":
        return None
    obj = parent.child_by_field_name("object")
    prop = parent.child_by_field_name("property")
    if obj is None or obj.id != node.id or prop is None or prop.type != "property_identifier":
        return None
    return parent


def _hands_on_member(member: Node) -> bool:
    """Whether the access ``member`` hands on only the member's value.

    Not when it is written (assigned, a pattern's target, updated, deleted),
    and not when it is called: ``F.valueOf()`` runs with the object as
    ``this`` and hands it straight back.
    """
    outer = member
    while outer.parent is not None and outer.parent.type in _TRANSPARENT:
        outer = outer.parent
    holder = outer.parent
    if holder is None:
        return True
    if holder.type in ("assignment_expression", "augmented_assignment_expression", "for_in_statement"):
        return holder.child_by_field_name("left") != outer
    if holder.type in _PATTERN_PARTS or holder.type == "update_expression":
        return False
    if holder.type == "unary_expression":
        operator = holder.child_by_field_name("operator")
        return operator is None or _text(operator) != "delete"
    if holder.type == "call_expression":
        return holder.child_by_field_name("function") != outer
    return True


#: The extensions a specifier of a TypeScript or JavaScript module can end in.
_MODULE_EXTENSIONS = frozenset({"", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"})


def _may_be_module(spec: str) -> bool:
    """Whether ``spec`` can name a script at all: ``./C.module.css`` or
    ``./en.json`` is a stylesheet or data, whatever a bundler does with it."""
    return Path(spec.rstrip("/")).suffix in _MODULE_EXTENSIONS


def _type_only(node: Node) -> bool:
    """``import type ...``, ``export type ... from``, or an ``import { type X }`` specifier."""
    return any(c.type == "type" for c in node.children)


def _single_const(declarator: Node) -> bool:
    """``declarator`` is the only one of a ``const`` that is not exported."""
    lexical = declarator.parent
    return (
        lexical is not None
        and lexical.type == "lexical_declaration"
        and any(c.type == "const" for c in lexical.children)
        and [c.id for c in lexical.named_children if c.type == "variable_declarator"] == [declarator.id]
        and (lexical.parent is None or lexical.parent.type != "export_statement")
    )


def _is_qualified(node: Node) -> bool:
    """``ns.member`` as an expression, or as the value of ``import K = ns.member``.

    The grammar spells the alias's value as a ``nested_identifier`` with the
    same fields. One elsewhere (a ``namespace a.b`` name, an ``a.b.T`` type)
    is a declaration or a type, and stays the general walk's.
    """
    if node.type == "member_expression":
        return True
    return node.type == "nested_identifier" and node.parent is not None and node.parent.type == "import_alias"


def _is_plain_member(node: Node) -> bool:
    """``ns.member`` with nothing optional about it."""
    return node.child_by_field_name("optional_chain") is None and not any(
        c.type == "?." for c in node.children
    )


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    root = ts_syntax._parse(source, _language(path))
    lines = source.splitlines()
    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []

    def refuse(node: Node, reason: str, entry: str = "") -> None:
        line = node.start_point[0]
        text = lines[line].strip() if line < len(lines) else ""
        refused.append(xk.Refusal(path, line + 1, reason, text, entry))

    # Enum entries are judged apart from the constants: every name, module
    # and binding below the split is a constant's.
    entries = tuple(d for d in declarations if d.kind == xk.ENUM_ENTRY)
    declarations = tuple(d for d in declarations if d.kind != xk.ENUM_ENTRY)
    enums: dict[tuple[str, str], dict[str, xk.KeyDeclaration]] = {}
    for decl in entries:
        enums.setdefault((decl.module, decl.owner), {})[decl.name] = decl
    entry_members = {d.name for d in entries}

    names = {d.name for d in declarations}
    if root.has_error:
        # A mention may sit in the part the parser could not read. Each
        # refusal is labelled by what it may be a mention of: the constants
        # once, and each entry the file spells on its own.
        mention = xk.mention_pattern(names)
        first = next((n for n, line in enumerate(lines) if mention.search(line)), None)
        if first is not None or not entries:
            first = first or 0
            refused.append(
                xk.Refusal(path, first + 1, xk.UNSUPPORTED_FORM, lines[first].strip() if lines else "")
            )
        for decl in entries:
            spelled = xk.mention_pattern([decl.name])
            hit = next((n for n, line in enumerate(lines) if spelled.search(line)), None)
            if hit is not None:
                refused.append(xk.Refusal(path, hit + 1, xk.UNSUPPORTED_FORM, lines[hit].strip(), decl.entry))
        return [], refused

    declared = {(d.module, d.name): d for d in declarations}
    # A namespace import of the enum's file is a namespace binding too, so
    # that `ns.Flags.EnableX` is judged instead of skipped.
    constant_modules = frozenset(d.module for d in declarations)
    modules = constant_modules | frozenset(m for m, _owner in enums)
    stop = _stop_for(repo_dir)
    resolved: dict[str, str | None] = {}

    def resolve(specifier: str) -> str | None:
        if specifier not in resolved:
            resolved[specifier] = _resolve(path, specifier, stop)
        return resolved[specifier]

    # Every binding OCCURRENCE in the file, by node. `binding_nodes` reads the
    # grammar's binding fields; an import names its locals without one, and an
    # import or export specifier's `name` is the other module's name, not a
    # binding here, so imports are collected separately.
    binding_ids: set[int] = set()
    count: dict[str, int] = {}

    binders: dict[str, list[Node]] = {}

    def bind(node: Node, local: str) -> None:
        if node.id not in binding_ids:
            binding_ids.add(node.id)
            count[local] = count.get(local, 0) + 1
            binders.setdefault(local, []).append(node)

    for node in ts_syntax.binding_nodes(root):
        if _within(node, ("import_statement", "export_clause", "import_alias")) is not None:
            continue
        bind(node, _text(node))
    undecodable_members: list[Node] = []
    # An enum member is a binding too: inside the enum's initialisers it
    # shadows any outer name (`B = f(K)` reads `E.K` once `K` is a member).
    # The grammar names members with `property_identifier`, which no binding
    # field reaches.
    for node in _walk(root):
        if node.type != "enum_body":
            continue
        for member in node.named_children:
            if member.type == "comment":
                continue
            name = member.child_by_field_name("name") if member.type == "enum_assignment" else member
            literal = name
            if literal is not None and literal.type == "computed_property_name":
                # `["K"] = 0` is member `K` as well; anything else computed
                # names a member this reading cannot know.
                inner = literal.named_children
                literal = inner[0] if len(inner) == 1 else None
            if literal is not None and literal.type == "property_identifier":
                bind(literal, _text(literal))
                continue
            # `"K" = 0` is member `K` too, and shadows `K` the same way.
            value = _string_value(literal) if literal is not None else None
            if value is not None:
                bind(literal, value)
            else:
                # An escape in the name, or a name that is not a literal: the
                # member could be any name, so skipping it would fail open.
                undecodable_members.append(member)

    bindings: list[_Binding] = []
    for node in _walk(root):
        if node.type == "import_statement":
            bindings.extend(_import_bindings(node, resolve, declared, modules, enums))
        elif node.type == "import_alias":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            if name is not None:
                bindings.append(_Binding(name, _text(name), None, None, False))
    for binding in bindings:
        bind(binding.node, binding.local)
    # Only now, with every import counted: `import require from "./loader"`
    # makes `require` a local like any other.
    require_is_global = count.get("require", 0) == 0
    requires: list[_Binding] = []
    for node in _walk(root):
        if node.type == "variable_declarator":
            requires.extend(
                _require_bindings(node, resolve, declared, modules, require_is_global, enums)
            )
    for binding in requires:
        bind(binding.node, binding.local)
    bindings.extend(requires)

    ours: dict[str, list[_Binding]] = {}
    for binding in bindings:
        if binding.declaration is not None or binding.namespace_of is not None or binding.enum_of is not None:
            ours.setdefault(binding.local, []).append(binding)

    def proof(local: str) -> _Binding | None:
        candidates = ours.get(local, [])
        if count.get(local, 0) == 1 and len(candidates) == 1 and candidates[0].provable:
            return candidates[0]
        return None

    def unproven_reason(local: str) -> str:
        candidates = ours.get(local, [])
        if candidates and (count.get(local, 0) > 1 or any(c.require_rebound for c in candidates)):
            return xk.REBOUND
        if candidates and all(c.type_only for c in candidates):
            return xk.NOT_A_READ
        return xk.IMPORT_UNRESOLVED

    by_binding_node = {b.node.id: b for b in bindings}
    own_exports: dict[tuple[str, str], bool] = {}

    def declares_itself(module: str | None, name: str | None) -> bool:
        """Whether another module declares ``name`` itself (so it re-exports no other)."""
        if module is None or name is None or module in constant_modules:
            return False
        if (module, name) not in own_exports:
            try:
                text = Path(module).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                own_exports[(module, name)] = False
            else:
                tree = ts_syntax._parse(text, _language(module))
                own_exports[(module, name)] = not tree.has_error and (
                    (module, None, name) in declared_names(module, text)
                )
        return own_exports[(module, name)]

    def foreign_own(binding: _Binding) -> bool:
        """A named import of another module's OWN constant of that name."""
        return (
            binding.declaration is None
            and binding.namespace_of is None
            and declares_itself(binding.module, _imported_name(binding.node))
        )

    def local_only(name: str) -> bool:
        """Whether every binding of ``name`` here is one that cannot be ours.

        A module-level constant reaches another file only through an import
        or a require of it, so a name this file binds itself -- a local, a
        parameter, its own ``export const`` -- is another constant that
        shares the name. Not a destructuring (``{ NAME } = require(...)``
        of a module that may re-export ours), and not an import unless the
        imported module declares the name itself.

        The question is asked of the whole FILE, not of the mention's scope:
        a mention that is free where it stands (outside the function that
        binds the name, say) is skipped too, as long as the file binds the
        name somewhere. That is safe because a free name resolves to a
        global, and our declaration is a module's export, which is never a
        global: only an import or a require reaches it. A name bound nowhere
        in the file is still refused, as the conservative choice.
        """
        nodes = binders.get(name, [])
        if not nodes:
            return False
        for node in nodes:
            binding = by_binding_node.get(node.id)
            if binding is not None:
                if not foreign_own(binding):
                    return False
            elif _within(node, ("object_pattern",)) is not None:
                return False
        return True

    def judge_entry(node: Node, parent: Node) -> bool:
        """Judge ``X.member`` against the enum entries; True when it was about one."""
        obj = parent.child_by_field_name("object")
        target = _unwrap(obj)
        plain = obj is not None and target is not None and target.id == obj.id
        if target is not None and target.type == "member_expression":
            return judge_qualified(node, _text(node), target, plain)
        if target is None or target.type != "identifier":
            return False
        return judge_owned(node, _text(node), target, plain, plain and _is_plain_member(parent))

    def judge_qualified(node: Node, member: str, qualifier: Node, plain: bool = False) -> bool:
        """Judge ``member`` of ``ns.Flags`` (``qualifier``); True when it was about an entry.

        ``plain``: spelled ``ns.Flags.member`` itself, with no wrapper. A read
        through it with nothing optional about either access is then proven
        like ``Flags.member`` through a named import, and folded with
        ``ns.Flags`` as its owner's text.
        """
        inner = qualifier.child_by_field_name("object")
        owner_prop = qualifier.child_by_field_name("property")
        if owner_prop is None or owner_prop.type != "property_identifier":
            return False
        owned = [
            members[member]
            for (module, owner), members in enums.items()
            if owner == _text(owner_prop) and member in members
        ]
        if not owned:
            return False
        # `ns.Flags.EnableX`: proven or not, the namespace may be ours.
        bound = proof(_text(inner)) if inner is not None and inner.type == "identifier" else None
        if bound is not None and bound.namespace_of is not None:
            enum = enums.get((bound.namespace_of, _text(owner_prop)))
            if enum is None:
                # A module of our constants: it may re-export the enum.
                for decl in owned:
                    refuse(node, xk.IMPORT_UNRESOLVED, decl.entry)
            elif member in enum:
                decl = enum[member]
                if not plain or _within(node, _NAMESPACE_BLOCKS) is not None:
                    refuse(node, xk.UNSUPPORTED_FORM, decl.entry)
                elif (
                    _is_plain_member(node.parent)
                    and _is_plain_member(qualifier)
                    and _is_read(node.parent, accessors)
                ):
                    owner = f"{_text(inner)}.{_text(owner_prop)}"
                    reads.append(xk.KeyReference(path, owner, member, "", decl))
                else:
                    refuse(node, xk.NOT_A_READ, decl.entry)
        elif inner is not None and inner.type == "identifier" and _text(inner) in namespace_locals:
            for decl in owned:
                refuse(node, unproven_reason(_text(inner)), decl.entry)
        else:
            for decl in owned:
                refuse(node, xk.IMPORT_UNRESOLVED, decl.entry)
        return True

    def judge_owned(node: Node, member: str, target: Node, plain: bool, foldable: bool) -> bool:
        """Judge ``member`` of the identifier ``target``; True when it was about an entry.

        ``plain``: spelled ``target.member`` itself, so a mention that is not
        a read is just that. Any other spelling -- through a wrapper, a
        string subscript, a destructuring -- is a form no reader rule
        rewrites, and is refused as one even where it reads the key.
        ``foldable``: a plain member with nothing optional about it.
        """
        local = _text(target)
        bound = proof(local)
        if bound is not None and bound.enum_of is not None:
            decl = enums[bound.enum_of].get(member)
            if decl is None:
                return True  # another member of our enum: another flag
            if _within(node, _NAMESPACE_BLOCKS) is not None:
                refuse(node, xk.UNSUPPORTED_FORM, decl.entry)
            elif not plain:
                refuse(node, xk.UNSUPPORTED_FORM, decl.entry)
            elif foldable and _is_read(node.parent, accessors):
                reads.append(xk.KeyReference(path, local, member, "", decl))
            else:
                refuse(node, xk.NOT_A_READ, decl.entry)
            return True
        candidates = [b for b in ours.get(local, []) if b.enum_of is not None]
        if candidates:
            decl = enums[candidates[0].enum_of].get(member)
            if decl is not None:
                refuse(node, unproven_reason(local), decl.entry)
            return True
        # Named after our enum but imported from somewhere that may re-export
        # it, destructured from a require, or bound nowhere at all.
        for (module, owner), members in enums.items():
            if member not in members or not may_be_enum(local, module, owner):
                continue
            refuse(node, xk.IMPORT_UNRESOLVED, members[member].entry)
            return True
        return False

    def judge_spelled(node: Node, member: str, source_node: Node | None) -> bool:
        """Judge ``member`` taken off ``source_node`` by a subscript or a destructuring."""
        target = _unwrap(source_node)
        if target is None:
            return False
        if target.type == "member_expression":
            return judge_qualified(node, member, target)
        if target.type != "identifier":
            return False
        return judge_owned(node, member, target, False, False)

    def judge_pattern_key(node: Node, member: str, pattern: Node) -> bool:
        """Judge a destructuring key naming ``member`` in ``pattern``."""
        holder = pattern.parent
        if holder is None:
            return False
        if holder.type == "variable_declarator" and holder.child_by_field_name("name") == pattern:
            return judge_spelled(node, member, holder.child_by_field_name("value"))
        if holder.type == "assignment_expression" and holder.child_by_field_name("left") == pattern:
            return judge_spelled(node, member, holder.child_by_field_name("right"))
        if holder.type == "pair_pattern" and holder.child_by_field_name("value") == pattern:
            # `{ Flags: { EnableX } } = ...`: a nested pattern under the
            # enum's name, out of whatever the outer source is.
            key = holder.child_by_field_name("key")
            owned = [
                members[member]
                for (module, owner), members in enums.items()
                if key is not None and owner == _text(key) and member in members
            ]
            for decl in owned:
                refuse(node, xk.UNSUPPORTED_FORM, decl.entry)
            return bool(owned)
        return False

    def may_be_enum(local: str, module: str, owner: str) -> bool:
        """Whether ``local``, not proven to be the enum, could still be it."""
        nodes = binders.get(local, [])
        if not nodes:
            return local == owner  # bound nowhere: the conservative choice
        for binder in nodes:
            binding = by_binding_node.get(binder.id)
            if binding is not None and _within(binding.node, ("import_statement",)) is not None:
                # Resolving elsewhere, nowhere, or to a file that may
                # re-export it. A default import goes by its local name.
                imported = _imported_name(binding.node) or binding.local
                if imported == owner and not declares_itself(binding.module, owner):
                    return True
                continue
            if _takes_member(binder, owner):
                # `const F = require("./flags").Flags`, `= ns["Flags"]`: the
                # enum off whatever the value is, which may be its module.
                return True
            if _within(binder, ("object_pattern",)) is None:
                continue
            # `{ Flags }` or `{ Flags: F }` out of something that may be the
            # enum's module or a module re-exporting it.
            pair = binder.parent if binder.parent is not None and binder.parent.type == "pair_pattern" else None
            key = pair.child_by_field_name("key") if pair is not None else binder
            if key is not None and _text(key) == owner:
                return True
        return False

    named_locals = {b.local for bs in ours.values() for b in bs if b.declaration is not None}
    namespace_locals = {b.local for bs in ours.values() for b in bs if b.namespace_of is not None}
    token = xk.mention_pattern(names)
    read_locals: set[str] = set()
    owner_names = {owner for _module, owner in enums}
    all_entries = sorted((d for members in enums.values() for d in members.values()), key=lambda d: d.entry)

    def unresolved_require(call: Node) -> bool:
        """``require("<specifier>")`` naming no file: it may be an enum's module."""
        function = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        if function is None or function.type != "identifier" or _text(function) != "require":
            return False
        spec = _literal(args.named_children[0]) if args is not None and args.named_children else None
        return spec is not None and _may_be_module(spec) and resolve(spec) is None

    # Names a module object no specifier resolves for is bound to: a namespace
    # or default import, `import m = require(...)`, or `m = require(...)`.
    # An enum's own module resolves, and every use of it is the escape scan's.
    module_locals: set[str] = set()
    for node in _walk(root) if owner_names else ():
        if node.type == "import_statement":
            clause = next((c for c in node.named_children if c.type == "import_require_clause"), None)
            source_node = node.child_by_field_name("source") or (
                clause.child_by_field_name("source") if clause is not None else None
            )
            spec = _literal(source_node)
            if spec is None or not _may_be_module(spec) or resolve(spec) is not None:
                continue
            for part in _walk(node):
                if part.type in ("namespace_import", "import_require_clause"):
                    module_locals.update(_text(c) for c in part.named_children if c.type == "identifier")
                elif part.type == "import_clause":
                    module_locals.update(_text(c) for c in part.named_children if c.type == "identifier")
        elif node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = _unwrap(node.child_by_field_name("value"))
            if name is not None and name.type == "identifier" and value is not None and value.type == "call_expression":
                if unresolved_require(value):
                    module_locals.add(_text(name))

    def allowed_take(take: Node) -> bool:
        """``const F = require("./flags").Flags``: the one take a name may hold.

        A ``const`` with that one declarator, not exported, through the global
        ``require`` of the file declaring that enum. The escape scan follows
        ``F``; the reads through it are judged here, at ``F.<member>``.
        """
        holder = take.parent
        call = take.child_by_field_name("object")
        prop = take.child_by_field_name("property")
        if (
            take.type != "member_expression"
            or holder is None
            or holder.type != "variable_declarator"
            or holder.child_by_field_name("value") != take
            or not _single_const(holder)
            or (holder.child_by_field_name("name") or holder).type != "identifier"
            or call is None
            or call.type != "call_expression"
            or prop is None
            or not require_is_global
        ):
            return False
        function = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        if function is None or function.type != "identifier" or _text(function) != "require":
            return False
        if args is None or len(args.named_children) != 1:
            return False
        spec = _string_value(args.named_children[0])
        module = resolve(spec) if spec is not None else None
        return module is not None and (module, _text(prop)) in enums

    def taken_owner(node: Node) -> str | None:
        """The enum ``node`` takes off something, if any.

        ``<expr>.Flags``, ``<expr>["Flags"]`` or the ``m.Flags`` of
        ``import F = m.Flags``, in no type position and not followed by a
        member of it (``ns.Flags.X`` is judged as a qualified mention of
        ``X``). Except for :func:`allowed_take`, a take is refused where it is
        taken, whatever then holds it: a name bound to the enum can hand it on
        in more ways than any reading of the file follows.
        """
        if node.type == "nested_identifier":
            if node.parent is None or node.parent.type != "import_alias":
                return None
            prop = node.child_by_field_name("property")
            owner = _text(prop) if prop is not None else None
        elif node.type == "member_expression":
            prop = node.child_by_field_name("property")
            owner = _text(prop) if prop is not None else None
        elif node.type == "subscript_expression":
            owner = _literal(node.child_by_field_name("index"))
        else:
            return None
        if owner not in owner_names or _in_type(node) or _within(node, ("import_statement",)) is not None:
            return None
        outer = node
        while outer.parent is not None and outer.parent.type in _TRANSPARENT:
            outer = outer.parent
        holder = outer.parent
        if holder is not None and holder.child_by_field_name("object") == outer:
            if holder.type == "member_expression":
                return None
            if holder.type == "subscript_expression" and _string_value(holder.child_by_field_name("index")) is not None:
                return None  # `ns.Flags["X"]`: judged as a spelling of the member
        if allowed_take(node):
            return None
        return owner

    def judge_take(node: Node, owner: str) -> None:
        """Refuse every entry of an enum taken off something.

        The enum object taken (``fn(require("./flags").Flags)``,
        ``o.F = m.Flags``, ``const F = pkg.Flags``) can reach every entry
        through a name no reading here follows, so each is refused with the
        reason a constant gets for the same spelling: ``unsupported-form`` off
        anything but a name, ``not-a-read`` off a proven namespace of the
        enum's file, and ``import-unresolved`` (or ``rebound``) off any other
        name.
        """
        everyone = [d for (_m, o), members in enums.items() if o == owner for d in members.values()]
        obj = node.child_by_field_name("object")
        if obj is None or obj.type != "identifier":
            reason, owned = xk.UNSUPPORTED_FORM, everyone
        else:
            local = _text(obj)
            bound = proof(local)
            if bound is None or bound.namespace_of is None:
                reason = unproven_reason(local) if local in namespace_locals else xk.IMPORT_UNRESOLVED
                owned = everyone
            elif (bound.namespace_of, owner) in enums:
                reason, owned = xk.NOT_A_READ, list(enums[(bound.namespace_of, owner)].values())
            else:
                reason, owned = xk.IMPORT_UNRESOLVED, everyone  # a module that may re-export it
        at = node.child_by_field_name("property") or node.child_by_field_name("index") or node
        for decl in sorted(owned, key=lambda d: d.entry):
            refuse(at, reason, decl.entry)

    def judge_lookup(node: Node) -> None:
        """``m[k]``, ``k`` not a string, off a module no specifier resolves.

        The enum, taken by a name computed at run time. A constant's twin is
        refused for spelling its name in a string; the name an enum is looked
        up by need not be spelled anywhere near.
        """
        if _literal(node.child_by_field_name("index")) is not None or _in_type(node):
            return
        target = _unwrap(node.child_by_field_name("object"))
        if target is None:
            return
        if target.type == "identifier" and _text(target) in module_locals:
            reason = xk.IMPORT_UNRESOLVED
        elif target.type == "call_expression" and unresolved_require(target):
            reason = xk.UNSUPPORTED_FORM
        else:
            return
        for decl in all_entries:
            refuse(node, reason, decl.entry)

    maybe: dict[str, tuple[xk.KeyDeclaration, ...]] = {}

    def judge_value(node: Node) -> None:
        """A bare name that may be the enum, used as anything but ``F.<member>``.

        A name proven to be the enum is the escape scan's to follow. One that
        is not, but may be (imported from a file that does not resolve, or may
        re-export it), is refused like a constant's: ``use(Flags)`` can hand
        the enum to code no reading here sees.
        """
        local = _text(node)
        if _in_type(node) or _within(node, ("import_statement",)) is not None:
            return
        parent = node.parent
        if parent is not None and parent.child_by_field_name("object") == node:
            if parent.type == "member_expression":
                return  # `F.member`: each member is judged on its own
            if parent.type == "subscript_expression" and _string_value(parent.child_by_field_name("index")) is not None:
                return  # `F["member"]`: judged as a spelling of the member
        bound = proof(local)
        if bound is not None and bound.enum_of is not None:
            return
        candidates = [b for b in ours.get(local, []) if b.enum_of is not None]
        if candidates:
            for decl in sorted(enums[candidates[0].enum_of].values(), key=lambda d: d.entry):
                refuse(node, unproven_reason(local), decl.entry)
            return
        if local not in maybe:
            maybe[local] = next(
                (
                    tuple(sorted(members.values(), key=lambda d: d.entry))
                    for (module, owner), members in enums.items()
                    if may_be_enum(local, module, owner)
                ),
                (),
            )
        for decl in maybe[local]:
            refuse(node, xk.IMPORT_UNRESOLVED, decl.entry)

    for node in _walk(root):
        kind = node.type
        if owner_names:
            taken = taken_owner(node)
            if taken is not None:
                judge_take(node, taken)
            elif kind == "subscript_expression":
                judge_lookup(node)
            elif kind in ("identifier", "shorthand_property_identifier") and node.id not in binding_ids:
                judge_value(node)
        if kind == "property_identifier" and _text(node) in entry_members:
            parent = node.parent
            prop = (
                parent.child_by_field_name("property")
                if parent is not None and parent.type == "member_expression"
                else None
            )
            if prop is not None and prop.id == node.id and judge_entry(node, parent):
                continue
            if (
                parent is not None
                and parent.type == "pair_pattern"
                and parent.child_by_field_name("key") == node
                and parent.parent is not None
                and judge_pattern_key(node, _text(node), parent.parent)
            ):
                continue
        if (
            kind == "shorthand_property_identifier_pattern"
            and _text(node) in entry_members
            and node.parent is not None
            and judge_pattern_key(node, _text(node), node.parent)
        ):
            continue
        if kind == "string" and _string_value(node) in entry_members:
            # `Flags["EnableX"]`. Its fragment is judged below for constants.
            parent = node.parent
            if (
                parent is not None
                and parent.type == "subscript_expression"
                and parent.child_by_field_name("index") == node
            ):
                judge_spelled(node, _string_value(node), parent.child_by_field_name("object"))
        if kind == "with_statement":
            refuse(node, xk.UNSUPPORTED_FORM)
            continue
        if kind == "string_fragment":
            if token.search(_text(node)) and _within(node, ("import_specifier",)) is None:
                refuse(node, xk.UNSUPPORTED_FORM)
            continue
        if kind == "property_identifier":
            parent = node.parent
            if (
                parent is None
                or not _is_qualified(parent)
                or _text(node) not in names
            ):
                continue
            prop = parent.child_by_field_name("property")
            if prop is None or prop.id != node.id:
                continue
            if _within(node, _NAMESPACE_BLOCKS) is not None:
                refuse(node, xk.UNSUPPORTED_FORM)
                continue
            obj = parent.child_by_field_name("object")
            if obj is None or obj.type != "identifier":
                refuse(node, xk.UNSUPPORTED_FORM)
                continue
            owner = _text(obj)
            bound = proof(owner)
            if bound is None or bound.namespace_of is None:
                if owner in namespace_locals:
                    refuse(node, unproven_reason(owner))
                else:
                    refuse(node, xk.IMPORT_UNRESOLVED)
            elif (bound.namespace_of, _text(node)) not in declared:
                refuse(node, xk.IMPORT_UNRESOLVED)  # another module's member
            elif _is_plain_member(parent) and _is_read(parent, accessors):
                reads.append(xk.KeyReference(path, owner, _text(node)))
            else:
                refuse(node, xk.NOT_A_READ)
            continue
        if kind not in _NAME_LEAVES or node.id in binding_ids:
            continue
        text = _text(node)
        if text not in names and text not in named_locals and text not in namespace_locals:
            continue
        if _within(node, ("import_statement",)) is not None:
            continue  # the import itself; judged from its binding below
        if text in namespace_locals and not any(
            b.namespace_of in constant_modules for b in ours.get(text, [])
        ):
            continue  # the enum's module only: its object is no constant's business
        if _within(node, _NAMESPACE_BLOCKS) is not None:
            refuse(node, xk.UNSUPPORTED_FORM)
            continue
        if text in namespace_locals:
            parent = node.parent
            if (
                parent is not None
                and _is_qualified(parent)
                and parent.child_by_field_name("object") is not None
                and parent.child_by_field_name("object").id == node.id
                and parent.child_by_field_name("property") is not None
                and parent.child_by_field_name("property").type == "property_identifier"
            ):
                continue  # `ns.member`: the member is judged on its own
            bound = proof(text)
            refuse(node, xk.NOT_A_READ if bound is not None else unproven_reason(text))
            continue
        export = _within(node, ("export_statement",))
        if export is not None and export.child_by_field_name("source") is not None:
            refuse(node, xk.IMPORT_UNRESOLVED)  # a re-export through a barrel
            continue
        bound = proof(text)
        if bound is None and text in names and not ours.get(text) and local_only(text):
            continue  # this file's own constant of the same name
        if bound is None or bound.declaration is None:
            refuse(node, unproven_reason(text))
        elif kind == "identifier" and _is_read(node, accessors):
            reads.append(xk.KeyReference(path, None, text))
            read_locals.add(text)
        else:
            refuse(node, xk.NOT_A_READ)

    for member in undecodable_members:
        refuse(member, xk.UNSUPPORTED_FORM)

    # The imports and requires, each judged once.
    for binding in bindings:
        if binding.enum_of is not None:
            continue  # judged at each `Flags.<member>`; the import is the fold's to strand
        if binding.namespace_of is not None:
            if binding.namespace_of in constant_modules and (
                count.get(binding.local, 0) > 1 or binding.require_rebound
            ):
                refuse(binding.node, xk.REBOUND)
            continue
        imported_name = _imported_name(binding.node)
        if binding.declaration is None:
            # A declared name imported from anywhere else: that module may
            # re-export ours.
            if (
                imported_name in names
                and _within(binding.node, ("import_statement",))
                and not foreign_own(binding)
            ):
                refuse(binding.node, xk.IMPORT_UNRESOLVED)
            continue
        if binding.unsupported:
            refuse(binding.node, xk.UNSUPPORTED_FORM)
        elif binding.type_only:
            refuse(binding.node, xk.NOT_A_READ)
        elif count.get(binding.local, 0) > 1 or binding.require_rebound:
            refuse(binding.node, xk.REBOUND)
        elif not binding.provable:
            refuse(binding.node, xk.IMPORT_UNRESOLVED)
        elif binding.local not in read_locals:
            # Nothing removes an import with no read to remove, and it would
            # name an export the same run deletes.
            refuse(binding.node, xk.NOT_A_READ)

    if refused:
        return [], refused
    return reads, refused


def _imported_name(local: Node) -> str | None:
    """The exported name an import specifier asks for, given its local node."""
    specifier = local.parent
    if specifier is None or specifier.type != "import_specifier":
        return None
    name = specifier.child_by_field_name("name")
    if name is None:
        return None
    if name.type == "string":
        return _string_value(name)
    return _text(name)


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every name ``path`` exports through ``export const``/``let``/``var``/``enum``.

    Keyed by the file's own path, which is a declaration's module here, so no
    other file can ever declare the same qualified name: a specifier resolves
    to one file, never to whichever of two a build picks.
    """
    root = ts_syntax._parse(source, _language(path))
    found: set[xk.QualifiedName] = set()
    for node in root.named_children:
        if node.type != "export_statement":
            continue
        for declaration in node.named_children:
            if declaration.type == "enum_declaration":
                name = declaration.child_by_field_name("name")
                if name is not None:
                    found.add((path, None, name.text.decode("utf-8", "replace")))
                continue
            if declaration.type not in ("lexical_declaration", "variable_declaration"):
                continue
            for declarator in declaration.named_children:
                name = declarator.child_by_field_name("name")
                if declarator.type == "variable_declarator" and name is not None:
                    found.add((path, None, name.text.decode("utf-8", "replace")))
    return found


# --- where the enum object goes ---------------------------------------------------
#
# A TypeScript enum is an object, and code holding it can reach every entry
# without spelling one. Its entries are folded or reported `kept` only when
# every use of the file declaring it, and of the enum, anywhere in the
# repository is one of the shapes below. Anything else is an escape, and
# refuses every entry of the enum at the line that lets it out.

#: What a use of a name may be spelled as.
_USE_LEAVES = ("identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern")
#: A quoted string anywhere in a file that could not be parsed.
_QUOTED = re.compile(r"""(["'`])([^"'`\\\n]*)\1""")
#: A specifier whose last segment is `.` or `..` (`"."`, `"../.."`,
#: `"./sub/.."`): it names a directory without spelling its name.
_DOTS = r"""(?P<q>["'`])(?:[^"'`\n]*/)?\.\.?/?(?P=q)"""


#: A call that loads every file a pattern or a directory matches. Spelled out
#: for the prefilter: the pattern need not name the file it loads. Astro's
#: ``Astro.glob`` is ``import.meta.glob`` in a component's frontmatter.
_GLOB_LOADERS = (
    r"import\s*\.\s*meta\s*\.\s*(?:glob|webpackContext)|require\s*\.\s*context|\bAstro\s*\.\s*glob\b"
)
#: Where a glob pattern stops being a literal directory.
_GLOB_MAGIC = re.compile(r"[*?\[{(]")
#: What makes an import or require specifier a glob (``import * as all from
#: "./*.ts"``) rather than a path.
_GLOB_SPECIFIER = re.compile(r"[*?{\[]")
#: A string with glob magic in it where a specifier is judged as a glob:
#: after ``from``, ``import`` or ``require``, past any spaces, comments and
#: ``(``. For the prefilter: such a specifier need not name the file it loads.
_GLOB_STRINGS = (
    r"""\b(?:from|import|require)(?:\s|\(|/\*[\s\S]*?\*/|//[^\n]*)*"""
    r"""(?:["'][^"'\n]*[*?{\[]|`[^`$]*[*?{\[])"""
)
#: Members that load the module their argument names, as ``require`` does: a
#: test runner's way past its mocks (``jest.requireActual(...)``,
#: ``vi.importActual(...)``). Keyed by the member's name alone, because a
#: runner's global can be imported under another name. ``module.require``
#: is the other such member, and only off ``module``.
_MEMBER_LOADERS = frozenset({"requireActual", "requireMock", "importActual", "importMock"})
#: An ``import(...)``, ``require(...)`` or member loader call whose specifier
#: is not one plain string (``import(`./pages/${p}`)``, ``require(path)``,
#: ``module.require(p)``), ``require`` or a member loader taken as anything
#: but a call (``const r = require``, ``(0, require)(...)``,
#: ``const c = require.context``, ``const r = module.require``), or
#: ``createRequire``, which makes a loader under any name. For the
#: prefilter: none of them need name the file it loads.
_COMPUTED_LOADS = (
    r"""\b(?:import|require|requireActual|requireMock|importActual|importMock)\s*\((?!\s*(?:/\*[\s\S]*?\*/\s*)*"""
    r"""(?:"[^"\\\n]*"|'[^'\\\n]*'|`[^`\\$\n]*`)\s*[,)])"""
    r"""|\b(?:require|requireActual|requireMock|importActual|importMock)\b"""
    r"""(?!\s*(?:\(|\.\s*(?:resolve|main|cache)\b))"""
    r"""|\bcreateRequire\b"""
)
#: Every character a glob gives a meaning to, for reading the literal text
#: a pattern ends in.
_GLOB_SPECIAL = re.compile(r"[*?\[\]{}()!+@|\\]")
#: The extensions of a file that can be a script, and so can import a module.
_SCRIPT_EXTENSIONS = tuple(sorted((_MODULE_EXTENSIONS - {""}) | source_index.COMPONENT_SUFFIXES))
#: A brace with no brace inside it, expanded first.
_BRACE = re.compile(r"\{([^{}]*)\}")


#: A call in a component's markup that loads a module: Svelte's
#: ``{#await import("./flags") then m}``, or a loader.
_MARKUP_LOADS = re.compile(r"\b(?:import|require)\s*\(|" + _GLOB_LOADERS)
_MARKUP_LOADER = re.compile(_GLOB_LOADERS)
#: A static specifier in a component's markup: ``from "./flags"`` or
#: ``import "./flags"``, past any spaces and comments. Markup holds one when
#: its code was cut short, as an Astro frontmatter is by a ``---`` line
#: inside a template literal.
_MARKUP_SPECIFIER = re.compile(r"""\b(?:from|import)(?:\s|/\*[\s\S]*?\*/|//[^\n]*)*(["'`])[^"'`\n]*\1""")
_SCRIPT_OPEN = re.compile(r"<script\b", re.IGNORECASE)
#: A whole ``<script ...>`` opening tag, past quoted attribute values.
_SCRIPT_TAG = re.compile(r"""<script\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script", re.IGNORECASE)
#: An Astro frontmatter fence: a line of ``---``.
_FENCE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)
#: A ``<template ...>`` opening tag, and a ``src`` attribute in one: Vue
#: loads that block's markup from another file.
_TEMPLATE_TAG = re.compile(r"""<template\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
_SRC = re.compile(r"\ssrc\s*=", re.IGNORECASE)
#: A script tag's ``lang`` attribute.
_LANG = re.compile(r"""\blang\s*=\s*["']?([\w-]+)""", re.IGNORECASE)


def _script_regions(path: str, source: str) -> list[tuple[int, int, int]] | None:
    """The ``(tag, start, end)`` offsets of a component file's code: each
    ``<script ...>...</script>`` block, from its opening tag at ``tag`` (a
    ``src`` attribute loads a file) and its code from ``start`` to ``end``,
    and in an Astro file its frontmatter between the leading ``---`` fences,
    which has no tag (``tag`` is ``start``). None when they cannot be told
    apart from the markup: a ``<script`` whose tag or block is never closed,
    or a ``---`` fence that does not close or has markup before it."""
    regions: list[tuple[int, int, int]] = []
    start = 0
    if path.endswith(".astro"):
        opener = _FENCE.search(source)
        if opener is not None:
            closer = _FENCE.search(source, opener.end())
            if source[: opener.start()].strip() or closer is None:
                return None
            regions.append((opener.end(), opener.end(), closer.start()))
            start = closer.end()
    while (opened := _SCRIPT_OPEN.search(source, start)) is not None:
        tag = _SCRIPT_TAG.match(source, opened.start())
        close = _SCRIPT_CLOSE.search(source, tag.end()) if tag is not None else None
        if tag is None or close is None:
            return None
        regions.append((tag.start(), tag.end(), close.start()))
        start = close.end()
    return regions


def component_match(path: str, source: str, patterns: tuple[re.Pattern[str], ...]) -> int | None:
    """Where the markup of a Vue, Svelte or Astro file can first use an enum:
    a match of ``patterns`` in a script's opening tag, a loader call or an
    ``import(...)``/``require(...)`` whose argument they match, as in
    Svelte's ``{#await import("./flags")}``, or a static ``from``/``import``
    specifier they match. The last is code the region finder lost: an Astro
    frontmatter ends at the first ``---`` line, even one inside a template
    literal, and the imports after it land in the markup. Markup text
    (``<h1>Feature flags</h1>``) is no use. Its code is judged by
    :func:`component_escapes`. When that code cannot be found (see
    :func:`_script_regions`), a match anywhere is one."""
    regions = _script_regions(path, source)
    if regions is None:
        return min((m.start() for p in patterns if (m := p.search(source))), default=None)
    hits = [m.start() for tag, start, _end in regions for p in patterns if (m := p.search(source, tag, start))]
    edges = [0] + [offset for tag, _start, end in regions for offset in (tag, end)] + [len(source)]
    for begin, end in zip(edges[::2], edges[1::2]):
        for load in _MARKUP_LOADS.finditer(source, begin, end):
            if _MARKUP_LOADER.match(load.group()):
                hits.append(load.start())
                break
            close = source.find(")", load.end(), end)
            stop = end if close < 0 else close + 1
            if any(p.search(source, load.start(), stop) for p in patterns):
                hits.append(load.start())
                break
        for spec in _MARKUP_SPECIFIER.finditer(source, begin, end):
            if any(p.search(source, spec.start(), spec.end()) for p in patterns):
                hits.append(spec.start())
                break
    return min(hits, default=None)


def _relative_root(reader: str, spec: str | None) -> Path | None:
    """The directory ``spec`` names from ``reader``'s, followed through links;
    None when it is not a plain relative path (computed, an alias, root-relative)."""
    if spec is None or not (spec in (".", "..") or spec.startswith(("./", "../"))):
        return None
    return Path(os.path.normpath(Path(reader).parent / spec)).resolve()


def _glob_root(reader: str, pattern: str | None, base: Path | None = None) -> Path | None:
    """The literal directory a glob pattern starts in: ``./widgets`` for
    ``./widgets/**/*.ts``. None when that cannot be told: a computed pattern,
    an alias, or one that can climb out of that directory again (a ``{`` or a
    ``..`` after it, as in ``./{x,../flags}/*.ts``), or one with no literal
    directory at all (``**/*.ts``). A pattern starting with
    ``/`` starts in ``base``, when given, unless its directory climbs with a
    ``..``: then it is None too."""
    if pattern is None:
        return None
    magic = _GLOB_MAGIC.search(pattern)
    literal = pattern[: magic.start()] if magic else pattern
    cut = literal.rfind("/")
    if cut < 0:
        # Not a path: Vite runs a leading ``**`` from ``/``.
        return None
    directory, rest = pattern[:cut], pattern[cut:]
    if "{" in rest or ".." in rest:
        return None
    if base is not None and pattern.startswith("/"):
        if ".." in directory.split("/"):
            return None
        return Path(os.path.normpath(base / directory.lstrip("/"))).resolve()
    return _relative_root(reader, directory)


def _expanded(pattern: str, limit: int = 64) -> list[str] | None:
    """``pattern`` with its braces expanded (``*.{json,ts}`` is ``*.json`` and
    ``*.ts``); None past ``limit`` alternatives."""
    done: list[str] = []
    todo = [pattern]
    while todo:
        current = todo.pop()
        brace = _BRACE.search(current)
        if brace is None:
            done.append(current)
        else:
            todo.extend(current[: brace.start()] + alt + current[brace.end() :] for alt in brace.group(1).split(","))
        if len(done) + len(todo) > limit:
            return None
    return done


def _may_load_script(pattern: str) -> bool:
    """Whether a glob ``pattern`` can match a script at all.

    It cannot when every name it matches ends in a literal extension that is
    not a script's: ``*.json``, ``**/*.md``, ``*.{json,yaml}``,
    ``logo.svg?url``. The literal text a file name pattern ends in must end
    in a script's extension, or, after a wildcard, be the end of one
    (``*.[jt]s``).
    """
    expansions = _expanded(pattern)
    if expansions is None:
        return True
    for expansion in expansions:
        name = expansion.rpartition("/")[2]
        parts = _GLOB_SPECIAL.split(name)
        tail = parts[-1].lower()
        if any(tail.endswith(ext) or (len(parts) > 1 and ext.endswith(tail)) for ext in _SCRIPT_EXTENSIONS):
            return True
    return False


def _computed_pattern(node: Node | None) -> str | None:
    """The glob a computed specifier can match, read as a bundler reads one:
    its literal text kept, and each part computed at run time a ``*``
    (``./pages/*`` for ``import(`./pages/${p}`)`` or
    ``require("./pages/" + p)``). None when it does not start with literal
    text, or a string in it has an escape."""
    parts: list[str] = []

    def add(part: Node | None) -> bool:
        part = _unwrap(part)
        if part is None:
            return False
        operator = part.child_by_field_name("operator") if part.type == "binary_expression" else None
        if operator is not None and _text(operator) == "+":
            return add(part.child_by_field_name("left")) and add(part.child_by_field_name("right"))
        if part.type == "string":
            value = _string_value(part)
            if value is None:
                return False
            parts.append(value)
        elif part.type == "template_string":
            for child in part.named_children:
                if child.type == "string_fragment":
                    parts.append(_text(child))
                elif child.type == "template_substitution":
                    parts.append("*")
                else:
                    return False
        else:
            parts.append("*")
        return True

    if not add(node):
        return None
    pattern = "".join(parts)
    return None if not pattern or pattern.startswith("*") else pattern


#: An argument that holds nothing a call could hand back.
_INERT_LITERALS = frozenset({"string", "number", "true", "false", "null", "regex"})


def _inert_value(node: Node) -> bool:
    """Whether an argument can be handed nothing: a literal, an object of
    them (``{ virtual: true }``), or an arrow function that takes no
    parameter (Jest's ``() => ({})`` factory). A function that takes one can
    be given a module, as Vitest's factory is given the original
    (``importOriginal``); so can a name, which may be such a function, and a
    ``function`` can read its ``arguments`` without naming a parameter."""
    if node.type in _INERT_LITERALS:
        return True
    if node.type == "object":
        return all(
            prop.type == "pair"
            and (key := prop.child_by_field_name("key")) is not None
            and key.type in ("property_identifier", "string", "number")
            and (value := prop.child_by_field_name("value")) is not None
            and _inert_value(value)
            for prop in node.named_children
            if prop.type != "comment"
        )
    if node.type == "arrow_function":
        parameters = node.child_by_field_name("parameters")
        return parameters is not None and not any(c.type != "comment" for c in parameters.named_children)
    return False


def _statement_argument(node: Node) -> bool:
    """Whether ``node`` is an argument of a call made for its side effect
    alone (``jest.mock("./flags");``, ``vi.mock(import("./flags"));``),
    and nothing beside it could be handed what the call loads."""
    parent = node.parent
    call = parent.parent if parent is not None and parent.type == "arguments" else None
    if call is None or call.parent is None or call.parent.type != "expression_statement":
        return False
    return all(_inert_value(c) for c in parent.named_children if c.type != "comment" and c.id != node.id)


#: Where ``createRequire`` can be spelled: called, imported, destructured or
#: taken off its module.
_CREATE_REQUIRE_LEAVES = (
    "identifier",
    "property_identifier",
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
)


def _member_loader(node: Node | None) -> bool:
    """Whether ``node`` is ``module.require`` or a member of
    :data:`_MEMBER_LOADERS`, off any object."""
    if node is None or node.type != "member_expression":
        return False
    obj = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    name = _text(prop) if prop is not None else ""
    if name == "require":
        return obj is not None and obj.type == "identifier" and _text(obj) == "module"
    return name in _MEMBER_LOADERS


def _roots_above(module: str) -> list[Path]:
    """Every directory a root-relative glob (``import.meta.glob("/src/*.ts")``)
    can start in and still reach ``module`` by a path that follows no link:
    the bundler's root, which its config can set to any directory, above the
    checkout too (``root: ".."``), so each one from ``module``'s own up to
    ``/``."""
    return [Path(module).parent, *Path(module).parent.parents]


@dataclass(frozen=True)
class _Links:
    """What the checkout's symlinks let a glob reach (:func:`_links_to`)."""

    #: ``(directory, recursive)`` for each linked directory holding a module.
    roots: tuple[tuple[Path, bool], ...]
    #: Each module's file links, by the path a glob finds them at.
    aliases: dict[str, tuple[Path, ...]]
    #: Whether a link gives a module a name no script has (``flags.txt``).
    disguised: bool


def _links_to(modules: Iterable[str], repo_dir: str) -> _Links:
    """The links in ``repo_dir``'s checkout that lead to a module.

    A linked directory holding one (``public/shared -> ../src``) lets a
    root-relative glob reach anything under it once the root is
    ``public``; a glob of a directory that IS the link resolves through it
    already. A link to a module's file (``x/f.ts -> ../flags.ts``) is the
    module, found where the link is. A bundler loads it by its real path, so
    a link with a name no script has (``flags.txt``) means no pattern is ruled
    out by its extension. Every link counts, whatever its name, since the root
    can be any directory."""
    paths = {m: Path(m) for m in modules}
    roots: list[tuple[Path, bool]] = []
    aliases: dict[str, list[Path]] = {}
    disguised = False
    for link in source_index.symlinks(repo_dir):
        try:
            target = link.resolve()
            found_at = link.parent.resolve() / link.name
        except (OSError, RuntimeError):
            continue
        for module, path in paths.items():
            if target == path:
                aliases.setdefault(module, []).append(found_at)
                disguised = disguised or not link.name.lower().endswith(_SCRIPT_EXTENSIONS)
            elif target in path.parents:
                roots.append((target, True))
    return _Links(tuple(dict.fromkeys(roots)), {m: tuple(a) for m, a in aliases.items()}, disguised)


def _base_option(options: Node | None) -> str | None:
    """The ``base`` of an ``import.meta.glob`` options object, which moves the
    directory a ``./`` or ``../`` pattern starts in: ``""`` when there is
    none, None when it cannot be told (a computed value, a spread, a computed
    key, or options that are not an object literal)."""
    if options is None:
        return ""
    if options.type != "object":
        return None
    base = ""
    for prop in options.named_children:
        if prop.type == "comment":
            continue
        if prop.type == "shorthand_property_identifier":
            if _text(prop) == "base":
                return None
            continue
        if prop.type != "pair":
            return None
        key = prop.child_by_field_name("key")
        if key is None or key.type not in ("property_identifier", "string"):
            return None
        if (_text(key) if key.type == "property_identifier" else _literal(key)) == "base":
            value = _literal(prop.child_by_field_name("value"))
            if value is None:
                return None
            base = value
    return base


def _recursive_option(options: Node | None) -> bool:
    """The ``recursive`` of a ``{ recursive, regExp }`` options object: true
    unless it is plainly the literal ``false``. A spread, a computed key or an
    options value that is not an object literal can say anything."""
    if options is None:
        return True
    if options.type != "object":
        return True
    recursive = True
    for prop in options.named_children:
        if prop.type == "comment":
            continue
        if prop.type == "shorthand_property_identifier" and _text(prop) != "recursive":
            continue
        if prop.type != "pair":
            return True
        key = prop.child_by_field_name("key")
        if key is None or key.type not in ("property_identifier", "string"):
            return True
        if (_text(key) if key.type == "property_identifier" else _literal(key)) == "recursive":
            value = _unwrap(prop.child_by_field_name("value"))
            recursive = value is None or value.type != "false"
    return recursive


def _stem(path: str) -> str:
    """``path``'s file name without the extension a specifier may leave off."""
    name = Path(path).name
    ext = max((e for e in _PLAIN_EXTENSIONS if name.endswith(e)), key=len, default="")
    return name[: len(name) - len(ext)] if ext else Path(name).stem


def _alias_keys(config: Path, stop: Path) -> list[str]:
    """The ``paths`` patterns in force for ``config``, its own or inherited
    through ``extends``; none when it cannot resolve anything.

    Read by :func:`_options`, not ``strict``: every alias the resolver
    follows is here, and so is every alias :func:`_enum_escapes` may see
    in a config the resolver refuses.
    """
    try:
        return list(_options(config, stop, strict=False).paths)
    except _Unknown:
        return []


def enum_module_pattern(entries: tuple[xk.KeyDeclaration, ...], repo_dir: str) -> re.Pattern[str]:
    """What a file's text holds when one of its specifiers can name an enum's file.

    A file reaches another only through a specifier, and a specifier the
    resolver takes to a file ends in that file's stem (``./flags``,
    ``./flags.js``), or, for an index file, in its directory's name,
    ``index``, the name of a symlink to its directory, or a ``.`` or ``..``
    segment (``"."``, ``"./sub/.."``). A glob loader (``import.meta.glob``,
    ``import.meta.webpackContext``, ``require.context``) or a glob specifier
    (``"./*.ts"``) need not name the file at all, so every file that calls one
    or imports or requires a string with glob magic in it is a candidate too,
    and so is every file that imports or requires anything but a plain string
    or takes ``require`` as a value, or names ``createRequire`` (see
    :data:`_COMPUTED_LOADS`). A
    component file's directory counts as a family file's for finding the
    configs above it. Through a ``paths`` alias
    it starts with the alias's text before its ``*``; an alias of nothing
    but ``*`` can name any file, and makes every file a candidate. A symlink
    to the file is spelled by its own name. Over-matching only parses a file
    that names none of them.
    """
    modules = {d.module for d in entries}
    tokens = {_stem(m) for m in modules}
    literals: set[str] = set()
    indexes = False
    suffixes = {Path(e).suffix for e in _PLAIN_EXTENSIONS} & set(source_index.all_suffixes())
    suffixes |= source_index.COMPONENT_SUFFIXES
    stop = _stop_for(repo_dir)
    directories: set[Path] = set()
    for source in source_index.files(repo_dir, suffixes, with_ignored=True):
        if source.resolved in modules:
            tokens.add(_stem(str(source.path)))
        parent = Path(source.resolved).parent
        while parent not in directories:
            directories.add(parent)
            if parent == stop or parent.parent == parent:
                break
            parent = parent.parent
    for module in modules:
        if _stem(module) == "index":
            indexes = True
            tokens.add(Path(module).parent.name)
            for link in source_index.symlinks(repo_dir):
                try:
                    if str(link.resolve()) == str(Path(module).parent):
                        tokens.add(link.name)
                except (OSError, RuntimeError):
                    continue
    for directory in directories:
        for name in _CONFIG_NAMES:
            config = directory / name
            if not config.is_file():
                continue
            for key in _alias_keys(config, stop):
                prefix = key.split("*", 1)[0]
                if not prefix:
                    return re.compile("")
                literals.add(prefix)
    parts = [xk.mention_pattern(tokens).pattern, _GLOB_LOADERS, _GLOB_STRINGS, _COMPUTED_LOADS]
    parts += [re.escape(text) for text in sorted(literals)]
    if indexes:
        parts.append(_DOTS)
    return re.compile("|".join(parts))


def enum_escapes(
    path: str, source: str, entries: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """Every use of an enum's file, or of the enum, in ``path`` that hands the enum on.

    In the file declaring the enum (or the ``as const`` object, which is
    judged as one throughout), the enum's name may only be the object of
    a member access ``Flags.<member>`` or a type; ``export default Flags``,
    ``export { Flags as X }`` and any other use escapes. Anywhere else, each
    import or ``require`` of that file is one of:

    * ``import { Flags }`` or ``import { Flags as F }``, and ``F`` is only ever
      the object of ``F.<member>`` or a type;
    * ``import * as ns``, and ``ns`` is only ever ``ns.<export>``; when that
      export is the enum, ``ns.Flags.<member>``;
    * a ``const`` with one declarator, not exported, of
      ``{ Flags } = require(...)``, ``{ Flags: F } = require(...)``,
      ``F = require(...).Flags`` (``F`` as above) or ``m = require(...)``
      (``m`` as ``ns`` above), through the file's global ``require``;
    * type-only, a side effect alone (``import "./flags"``,
      ``require("./flags");``), or of other names than the enum's.

    ``F.<member>`` is a member access that is not written (assigned, updated,
    deleted) and not called: ``F.valueOf()`` hands the object back. Every
    other use escapes: a re-export in any form, a dynamic ``import()`` (but
    Vitest's typed ``vi.mock(import("./flags"));``, which is a path),
    ``import m = require(...)``, a ``let`` or ``var``, any computed member, the
    enum or its module passed, stored, spread or assigned. So does a
    specifier given as a template literal. The check is file-wide, not
    scope-aware: a name bound again somewhere in the file is still judged as
    the import, which can only find an escape that is not one.

    A specifier that does not resolve is not one of these, whatever file it
    names: a mention of the enum through it is judged as a constant's would be
    (see :func:`references`). One that is a glob (``"./*.ts"``) is judged as
    a glob loader's pattern, and so is one computed at run time
    (``import(`./pages/${p}`)``, ``require("./pages/" + p)``), read as the
    glob a bundler makes of it (``./pages/*``). Like a bundler, it takes a
    computed part not to climb out of the directory the literal text in
    front of it names. One with no literal text in front (``require(path)``)
    or with an escape in a string can load any file, and so can ``require``
    taken as anything but a call (``const r = require``,
    ``(0, require)(...)``, ``const c = require.context``). A member loader
    (``module.require``, ``jest.requireActual``, ``vi.importActual``; see
    :data:`_MEMBER_LOADERS`) is judged as ``require`` is, and any mention of
    ``createRequire`` can load any file: what it returns is a loader under
    whatever name it is given.

    A declaration file (``.d.ts``) never runs, so nothing in it hands the
    enum to code: it is not scanned.
    """
    return _enum_escapes(path, source, entries, repo_dir, _language(path))


def _enum_escapes(
    path: str,
    source: str,
    entries: tuple[xk.KeyDeclaration, ...],
    repo_dir: str,
    language: str,
    bound: dict[str, list[xk.KeyDeclaration]] | None = None,
    judged: list[tuple[int, int]] | None = None,
) -> list[xk.Refusal]:
    """:func:`enum_escapes`, parsing ``source`` as ``language``. Given
    ``bound``, the code is a component's, which is never rewritten: a read of
    an entry there escapes too, and each name bound to the enum or its file
    is put in ``bound`` with the entries it can reach. Given ``judged``, the
    byte span of each import or ``require`` of the enum's file and of each
    use followed from one is put in it."""
    if path.endswith((".d.ts", ".d.mts", ".d.cts")):
        return []
    by_module: dict[str, dict[str, list[xk.KeyDeclaration]]] = {}
    for decl in entries:
        if decl.owner is not None:
            by_module.setdefault(decl.module, {}).setdefault(decl.owner, []).append(decl)
    if not by_module:
        return []
    stop = _stop_for(repo_dir)
    resolved: dict[str, str | None] = {}
    aliases: list[_Options | None] = []

    def module_named(spec: str | None) -> str | None:
        if not spec or "\n" in spec:
            return None
        if spec not in resolved:
            if spec in (".", "..") or spec.startswith(("./", "../")):
                resolved[spec] = _resolve(path, spec, stop)
            else:
                # Every string is asked, so the nearest config is read once:
                # without a `paths` pattern that fits, nothing else resolves.
                # Read leniently: a use is only ever over-attributed here, and
                # an alias the fold refuses to follow still reaches the file.
                if not aliases:
                    try:
                        aliases.append(_config(Path(path).parent, stop, strict=False))
                    except _Unknown:
                        aliases.append(None)
                fits = aliases[0] is not None and aliases[0].targets(spec) is not None
                resolved[spec] = _resolve(path, spec, stop, strict=False) if fits else None
        module = resolved[spec]
        return module if module in by_module else None

    def glob_named(spec: str | None) -> bool:
        """Whether ``spec`` is a glob rather than a path to a file."""
        return (
            spec is not None
            and _GLOB_SPECIFIER.search(spec) is not None
            and module_named(spec) is None
            and resolved.get(spec) is None
        )

    def every(module: str) -> list[xk.KeyDeclaration]:
        return [d for decls in by_module[module].values() for d in decls]

    escapes: dict[tuple[int, str], xk.Refusal] = {}

    def escape(node: Node, decls: list[xk.KeyDeclaration]) -> None:
        for decl in decls:
            escapes.setdefault(
                (node.start_point[0], decl.entry),
                xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry),
            )

    root = ts_syntax._parse(source, language)
    if root.has_error:
        # The part the parser could not read may use the file any way at all:
        # every string that names it is an escape, and so is every load that
        # need not name it.
        lines = source.splitlines()

        def unread(offset: int, modules) -> None:
            line = source.count("\n", 0, offset)
            for module in modules:
                for decl in every(module):
                    escapes.setdefault(
                        (line, decl.entry),
                        xk.Refusal(path, line + 1, xk.UNSUPPORTED_FORM, lines[line].strip(), decl.entry),
                    )

        for match in _QUOTED.finditer(source):
            module = module_named(match.group(2))
            if module is not None:
                unread(match.start(), [module])
        load = re.search("|".join((_GLOB_LOADERS, _GLOB_STRINGS, _COMPUTED_LOADS)), source)
        if load is not None:
            unread(load.start(), sorted(by_module))
        return [escapes[k] for k in sorted(escapes)]

    def foreign(node: Node) -> bool:
        """A name that is not a use of this file's binding of it."""
        if _within(node, ("import_statement",)) is not None:
            return True  # an import binds names; it never uses one
        export = _within(node, ("export_statement",))
        if export is not None and export.child_by_field_name("source") is not None:
            return True  # `export { X } from "..."` names the other module's X
        parent = node.parent
        return parent is not None and parent.type == "export_specifier" and parent.child_by_field_name("alias") == node

    def uses(local: str, binder: Node | None):
        for node in _walk(root):
            if node.type in _USE_LEAVES and _text(node) == local and (binder is None or node.id != binder.id):
                if not foreign(node):
                    yield node

    def read(member: Node, decls: list[xk.KeyDeclaration]) -> bool:
        """Whether ``member`` reads one of ``decls`` in a component."""
        prop = member.child_by_field_name("property")
        return bound is not None and prop is not None and _text(prop) in {d.name for d in decls}

    def judge(node: Node | None) -> None:
        if judged is not None and node is not None:
            judged.append((node.start_byte, node.end_byte))

    def follow_enum(local: str, binder: Node | None, decls: list[xk.KeyDeclaration]) -> None:
        """``local`` is the enum: only ``local.<member>`` and types may use it."""
        if bound is not None:
            bound.setdefault(local, []).extend(decls)
        if judged is not None:
            for node in _walk(root):
                if node.type == "type_identifier" and _text(node) == local:
                    judge(node)  # `let k: Flags`: a type, never the value
        for node in uses(local, binder):
            judge(node)
            member = _member_of(node)
            if _in_type(node) or (member is not None and _hands_on_member(member) and not read(member, decls)):
                continue
            escape(node, decls)

    def follow_module(local: str, binder: Node | None, module: str) -> None:
        """``local`` is the module object: only ``local.<export>`` may use it."""
        owners = by_module[module]
        if bound is not None:
            bound.setdefault(local, []).extend(every(module))
        for node in uses(local, binder):
            if _in_type(node):
                continue
            member = _member_of(node)
            if member is None:
                escape(node, every(module))
                continue
            owner = _text(member.child_by_field_name("property"))
            if owner not in owners:
                continue  # another export: never the enum, which only escapes in its file
            judge(member)
            taken = _member_of(member)
            if taken is None or not _hands_on_member(taken) or read(taken, owners[owner]):
                escape(member, owners[owner])

    def vitest_vi(name: Node) -> bool:
        """``import { vi } from "vitest"``, and no other import of the name."""
        specifier = name.parent
        statement = _within(name, ("import_statement",))
        return (
            specifier is not None
            and specifier.type == "import_specifier"
            and specifier.child_by_field_name("name") == name
            and specifier.child_by_field_name("alias") is None
            and statement is not None
            and _literal(statement.child_by_field_name("source")) == "vitest"
        )

    # `vi` is Vitest's only when the file binds it to nothing else: the global
    # its config provides, or `import { vi } from "vitest"`. Assumed: nothing
    # outside Vitest sets a global `vi`.
    vi_foreign = any(
        _text(n) == "vi" and _within(n, ("import_statement",)) is None for n in ts_syntax.binding_nodes(root)
    ) or any(
        _text(n) == "vi" and not vitest_vi(n)
        for n in _walk(root)
        if n.type == "identifier" and _within(n, ("import_statement",)) is not None
    )

    def vitest_mock(load: Node) -> bool:
        """Whether ``load``, an ``import(...)``, is what Vitest's typed mock
        takes (``vi.mock(import("./flags"));``). Vitest rewrites it to the
        path before anything runs; handed to any other call, it is the module."""
        call = load.parent.parent if load.parent is not None else None
        function = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
        if function is None or function.type != "member_expression" or vi_foreign:
            return False
        obj = function.child_by_field_name("object")
        prop = function.child_by_field_name("property")
        return (
            obj is not None
            and obj.type == "identifier"
            and _text(obj) == "vi"
            and prop is not None
            and _text(prop) in ("mock", "doMock")
            and _statement_argument(load)
        )

    require_bound = any(_text(n) == "require" for n in ts_syntax.binding_nodes(root)) or any(
        _text(n) == "require"
        for n in _walk(root)
        if n.type == "identifier" and _within(n, ("import_statement",)) is not None
    )

    def judge_import(statement: Node) -> None:
        clause = next((c for c in statement.named_children if c.type == "import_require_clause"), None)
        source_node = statement.child_by_field_name("source") or (
            clause.child_by_field_name("source") if clause is not None else None
        )
        spec = _literal(source_node)
        module = module_named(spec)
        if module is not None:
            judge(statement)
        if _type_only(statement):
            return
        if module is None:
            binds = clause is not None or any(c.type == "import_clause" for c in statement.named_children)
            if binds and glob_named(spec):
                judge_glob_specifier(statement, spec)  # `import * as all from "./*.ts"`
            return
        if clause is not None:
            escape(statement, every(module))  # `import m = require("./flags")`
            return
        for part in statement.named_children:
            if part.type != "import_clause":
                continue
            for item in part.named_children:
                if item.type == "namespace_import":
                    name = next((c for c in item.named_children if c.type == "identifier"), None)
                    if name is not None:
                        follow_module(_text(name), name, module)
                elif item.type == "named_imports":
                    for specifier in item.named_children:
                        if specifier.type != "import_specifier" or _type_only(specifier):
                            continue
                        imported = specifier.child_by_field_name("name")
                        local = specifier.child_by_field_name("alias") or imported
                        name = _literal(imported) if imported is not None and imported.type == "string" else (
                            _text(imported) if imported is not None else None
                        )
                        if name in by_module[module] and local is not None:
                            follow_enum(_text(local), local, by_module[module][name])
                # A default import: `export default Flags` escapes in the enum's file.

    def judge_require(call: Node, module: str) -> None:
        judge(_within(call, ("variable_declarator",)) or call.parent)
        args = call.child_by_field_name("arguments")
        specifier = args.named_children[0] if args is not None and args.named_children else None
        if require_bound or specifier is None or specifier.type != "string" or len(args.named_children) != 1:
            escape(call, every(module))
            return
        parent = call.parent
        if parent is not None and parent.type == "expression_statement":
            return  # `require("./flags");`: run for its side effects
        if (
            parent is not None
            and parent.type == "variable_declarator"
            and parent.child_by_field_name("value") == call
            and _single_const(parent)
        ):
            target = parent.child_by_field_name("name")
            if target is not None and target.type == "identifier":
                follow_module(_text(target), target, module)
                return
            if target is not None and target.type == "object_pattern":
                for prop in target.named_children:
                    if prop.type == "comment":
                        continue
                    if prop.type == "shorthand_property_identifier_pattern":
                        if _text(prop) in by_module[module]:
                            follow_enum(_text(prop), prop, by_module[module][_text(prop)])
                        continue
                    key = prop.child_by_field_name("key") if prop.type == "pair_pattern" else None
                    value = prop.child_by_field_name("value") if prop.type == "pair_pattern" else None
                    if key is not None and key.type == "property_identifier" and value is not None and value.type == "identifier":
                        if _text(key) in by_module[module]:
                            follow_enum(_text(value), value, by_module[module][_text(key)])
                        continue
                    escape(prop, every(module))  # a rest, a default, a nested or computed key
                return
        if parent is not None and parent.type == "member_expression" and parent.child_by_field_name("object") == call:
            prop = parent.child_by_field_name("property")
            holder = parent.parent
            target = holder.child_by_field_name("name") if holder is not None and holder.type == "variable_declarator" else None
            if (
                prop is not None
                and prop.type == "property_identifier"
                and _text(prop) in by_module[module]
                and holder.child_by_field_name("value") == parent
                and _single_const(holder)
                and target is not None
                and target.type == "identifier"
            ):
                follow_enum(_text(target), target, by_module[module][_text(prop)])
                return
        escape(call, every(module))

    def judge_specifier(node: Node) -> None:
        """A string naming the enum's file anywhere but where it is judged.

        An import or export source, the specifier of ``require(...)`` or
        ``import(...)``, a type, and a call made for its side effect alone
        (``jest.mock("./flags");``) are judged or inert, the last only while
        nothing beside the string can be handed the module back: Vitest
        gives a factory taking a parameter the original module
        (``vi.mock("./flags", async (importOriginal) => ...)``). Anywhere
        else the string hands the file to code that may load it:
        ``module.require(...)``, ``jest.requireActual(...)``,
        ``import.meta.glob(...)``, ``require.cache[require.resolve(...)]``.
        """
        module = module_named(_literal(node))
        if module is None or _in_type(node) or _within(node, ("import_statement", "export_clause")) is not None:
            return
        parent = node.parent
        if parent is not None and parent.type == "export_statement" and parent.child_by_field_name("source") == node:
            return  # `export * from "./flags"`: judged as a re-export
        if parent is not None and parent.type == "arguments":
            call = parent.parent
            function = call.child_by_field_name("function") if call is not None else None
            if (
                parent.named_children[0].id == node.id
                and function is not None
                and (function.type == "import" or (function.type == "identifier" and _text(function) == "require"))
            ):
                return  # judged as a module reference
            if _statement_argument(node):
                return  # a call made for its side effect alone
        escape(node, every(module))

    globbed: set[str] = set()

    def reached(node: Node, reach: list[tuple[Path | None, bool]], also: str | None = None) -> None:
        """Escape at ``node`` every module a ``(directory, recursive)`` of
        ``reach`` can load (any, for a directory of None), and ``also``."""
        if not reach and also is None:
            return
        for module in sorted(by_module):
            targets = (Path(module), *links().aliases.get(module, ()))
            if module == also or any(
                root is None or (root in target.parents if recursive else root == target.parent)
                for root, recursive in reach
                for target in targets
            ):
                globbed.add(module)
                escape(node, every(module))

    linked: list[_Links] = []

    def links() -> _Links:
        if not linked:
            linked.append(_links_to(by_module, repo_dir))
        return linked[0]

    def pattern_reach(text: str | None, rooted: bool, base: str | None = "") -> list[tuple[Path | None, bool]]:
        """Where a glob pattern can load from: nothing for an exclusion
        (``!``) or a pattern that matches no script. ``base`` is the
        ``import.meta.glob`` option, as :func:`_base_option` reads it."""
        if text is not None and text.startswith("!"):
            return []
        if text is not None and not _may_load_script(text) and not links().disguised:
            return []
        if rooted and text is not None and text.startswith(("./", "../")):
            if base is None:
                return [(None, True)]
            if base:
                text = base.rstrip("/") + "/" + text.removeprefix("./")
        if rooted and text is not None and text.startswith("/"):
            if "node_modules" in text.split("/"):
                # A workspace link there is not listed (`source_index.symlinks`
                # skips the directory), and it can lead anywhere.
                return [(None, True)]
            # The bundler's config can make any of these directories its root.
            found = [*by_module, *(str(a) for aliases in links().aliases.values() for a in aliases)]
            bases = {base for module in found for base in _roots_above(module)}
            return [(_glob_root(path, text, base), True) for base in sorted(bases)] + list(links().roots)
        return [(_glob_root(path, text), True)]

    def judge_glob(call: Node, loader: str) -> None:
        """``import.meta.glob(...)``, ``import.meta.webpackContext(...)`` or
        ``require.context(...)``: every file its pattern or directory can match
        is loaded and handed back.

        Matching is approximated from the literal directory a pattern starts in
        (an ancestor of the enum's file can reach it, whatever the rest of the
        pattern says), or the context's directory: itself only when
        ``recursive`` is ``false``. The ``regexp`` is not read. A pattern whose
        every match ends in an extension no script has (``*.json``) matches
        nothing. Otherwise a pattern or a directory that is not a plain
        relative literal matches everything, except that a glob pattern
        starting with ``/`` starts in the bundler's root. That can be any
        directory above the enum's, or one holding a link into it
        (:func:`_links_to`). Vite joins such a pattern to the root before
        it reads ``resolve.alias``, so no alias moves it. A ``base`` option
        moves a ``./`` or ``../`` pattern, and one that cannot be read makes
        it match everything.
        """
        args = call.child_by_field_name("arguments")
        items = [c for c in args.named_children if c.type != "comment"] if args is not None else []
        reach: list[tuple[Path | None, bool]] = []
        if loader == "context":
            recursive = not (len(items) > 1 and items[1].type == "false")
            reach.append((_relative_root(path, _literal(items[0]) if items else None), recursive))
        elif loader == "webpackContext":
            recursive = _recursive_option(items[1] if len(items) > 1 else None)
            reach.append((_relative_root(path, _literal(items[0]) if items else None), recursive))
        else:
            first = items[0] if items else None
            patterns = first.named_children if first is not None and first.type == "array" else [first]
            base = _base_option(items[1] if len(items) > 1 else None)
            for pattern in patterns:
                reach += pattern_reach(_literal(pattern), rooted=True, base=base)
        reached(call, reach)

    def judge_glob_specifier(node: Node, spec: str) -> None:
        """An import or require of a glob (``"./*.ts"``), as a bundler that
        expands one loads it. A ``?`` can also start a query (``"./flags?raw"``):
        the file before it is loaded then."""
        base = spec.partition("?")[0]
        queried = module_named(base) if base != spec and _GLOB_SPECIFIER.search(base) is None else None
        reached(node, pattern_reach(spec, rooted=False), queried)

    binders = {n.id for n in ts_syntax.binding_nodes(root)}

    def judge_require_name(node: Node) -> None:
        """``require`` anywhere but as a call, or the object of a call of one
        of its members: a loader handed on (``const r = require``,
        ``(0, require)(...)``, ``const c = require.context``) can load any
        file. ``typeof require`` and ``require.resolve`` load nothing."""
        parent = node.parent
        if parent is None or node.id in binders or foreign(node) or _in_type(node):
            return
        if parent.type == "call_expression" and parent.child_by_field_name("function") == node:
            return  # judged as a call
        if parent.type == "unary_expression":
            operator = parent.child_by_field_name("operator")
            if operator is not None and _text(operator) == "typeof":
                return
        if parent.type == "member_expression" and parent.child_by_field_name("object") == node:
            prop = parent.child_by_field_name("property")
            if prop is None or _text(prop) != "context":
                return  # `require.resolve`, `require.main`, `require.cache[...]`
            holder = parent.parent
            if holder is not None and holder.type == "call_expression" and holder.child_by_field_name("function") == parent:
                return  # judged as a glob loader
        reached(node, [(None, True)])

    # Direct `eval` reads the bindings around it, and `with` makes any name
    # an object's member: either can reach the enum, or a name bound to it,
    # without a use this scan sees. Judged once every module the file names
    # is known.
    unseen: list[Node] = []

    # The declaring file: its enum's name, or its `as const` object's, is a
    # use like any other.
    for node in root.named_children:
        declared = _declared(node) if node.type == "export_statement" else None
        if declared is None:
            continue
        if declared.type == "enum_declaration":
            names = [declared.child_by_field_name("name")]
        elif declared.type == "lexical_declaration":
            names = [d.child_by_field_name("name") for d in declared.named_children if d.type == "variable_declarator"]
        else:
            continue
        for name in names:
            if name is not None and name.type == "identifier" and _text(name) in by_module.get(path, {}):
                follow_enum(_text(name), name, by_module[path][_text(name)])
    for node in _walk(root):
        if node.type == "import_statement":
            judge_import(node)
        elif node.type == "export_statement":
            spec = _literal(node.child_by_field_name("source"))
            module = module_named(spec)
            if not _type_only(node):
                if module is not None:
                    escape(node, every(module))
                elif glob_named(spec):
                    judge_glob_specifier(node, spec)
        elif node.type == "with_statement":
            unseen.append(node)
        elif node.type in ("identifier", "shorthand_property_identifier") and _text(node) == "require":
            judge_require_name(node)
        elif node.type in _CREATE_REQUIRE_LEAVES and _text(node) == "createRequire":
            if not _in_type(node):
                reached(node, [(None, True)])  # makes a loader, under any name
        elif node.type == "member_expression" and _member_loader(node):
            parent = node.parent
            called = parent is not None and parent.type == "call_expression" and parent.child_by_field_name("function") == node
            if not called and not _in_type(node):
                reached(node, [(None, True)])  # `const r = module.require`
        elif node.type in ("string", "template_string"):
            judge_specifier(node)
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function is not None and function.type == "identifier" and _text(function) == "eval":
                unseen.append(node)
            if function is not None and function.type == "member_expression":
                obj = function.child_by_field_name("object")
                prop = function.child_by_field_name("property")
                name = _text(prop) if prop is not None else ""
                if obj is not None and obj.type == "meta_property" and name in ("glob", "globEager"):
                    judge_glob(node, "glob")
                elif obj is not None and obj.type == "meta_property" and name == "webpackContext":
                    judge_glob(node, "webpackContext")
                elif obj is not None and obj.type == "identifier" and _text(obj) == "require" and name == "context":
                    judge_glob(node, "context")
                elif obj is not None and obj.type == "identifier" and _text(obj) == "Astro" and name == "glob":
                    judge_glob(node, "glob")
            args = node.child_by_field_name("arguments")
            # Past a comment: `import(/* webpackChunkName: "x" */ ...)`.
            first = next((c for c in args.named_children if c.type != "comment"), None) if args is not None else None
            spec = _literal(first)
            module = module_named(spec)
            if function is None:
                continue
            if module is None:
                # `require("./*.ts")`, `import(`./pages/${p}`)`,
                # `module.require(p)`; a bare `require(...);` only runs the files.
                required = (function.type == "identifier" and _text(function) == "require") or _member_loader(function)
                side_effect = required and node.parent is not None and node.parent.type == "expression_statement"
                if (function.type == "import" or required) and not side_effect and first is not None and not _in_type(node):
                    if glob_named(spec):
                        judge_glob_specifier(node, spec)
                    elif spec is None:
                        reached(node, pattern_reach(_computed_pattern(first), rooted=False))
                continue
            if _in_type(node):
                continue  # `typeof import("./flags")` is a type
            if function.type == "import":
                if not vitest_mock(node):
                    escape(node, every(module))
            elif function.type == "identifier" and _text(function) == "require":
                judge_require(node, module)
    named = {m for m in resolved.values() if m in by_module} | globbed | ({path} if path in by_module else set())
    for node in unseen:
        for module in sorted(named):
            escape(node, every(module))
    return [escapes[k] for k in sorted(escapes)]


def component_escapes(
    path: str, source: str, entries: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """The escapes in the code of a Vue, Svelte or Astro file.

    Its scripts and Astro frontmatter (see :func:`_script_regions`) are read
    as one TypeScript module, the markup blanked out around them, and judged
    as :func:`enum_escapes` judges a file, except that a read of an entry
    escapes too: a component is never rewritten, so that read would stay
    live. Nor is it read by ``references``, so the enum's name spelled
    anywhere in the code but an import or ``require`` of its file, or a use
    followed from one, escapes: through an import that does not resolve
    (``$lib/flags`` under a generated config) or one a framework adds
    unwritten, the name is all there is to go on. The markup is not parsed,
    so a name the code binds to the enum or its file escapes wherever the
    markup spells it, hyphenated too (Vue's ``:feature-flags`` binds
    ``featureFlags``), and so does Svelte's ``$F``, which subscribes to ``F``
    itself; at a Vue ``<template src>``, whose markup is another file's,
    every such name escapes. Loads in the
    markup are :func:`component_match`'s, and so is a file whose code cannot
    be found: then this finds nothing.
    """
    regions = _script_regions(path, source)
    if regions is None:
        return []
    parts: list[str] = []
    done = 0
    language = "ts"
    for tag, start, end in regions:
        lang = _LANG.search(source, tag, start)
        if lang is not None and lang.group(1).lower() in ("tsx", "jsx"):
            language = "tsx"
        parts += [re.sub(r"[^\n]", " ", source[done:start]), source[start:end]]
        done = end
    parts.append(re.sub(r"[^\n]", " ", source[done:]))
    code = "".join(parts)
    bound: dict[str, list[xk.KeyDeclaration]] = {}
    judged: list[tuple[int, int]] = []
    lines = source.split("\n")
    found: dict[tuple[int, str], xk.Refusal] = {}
    for refusal in _enum_escapes(path, code, entries, repo_dir, language, bound, judged):
        text = lines[refusal.line - 1].strip()
        found.setdefault((refusal.line, refusal.entry), replace(refusal, text=text))

    def refuse(line: int, decls) -> None:
        for decl in decls:
            found.setdefault(
                (line + 1, decl.entry),
                xk.Refusal(path, line + 1, xk.UNSUPPORTED_FORM, lines[line].strip(), decl.entry),
            )

    def escape(offset: int, local: str) -> None:
        refuse(source.count("\n", 0, offset), bound.get(local, ()))

    raw = code.encode("utf-8")
    owners = sorted({d.owner for d in entries if d.owner is not None})
    spelled_owner = re.compile(xk.mention_pattern(owners).pattern.encode("utf-8"))
    for mention in spelled_owner.finditer(raw):
        if not any(start <= mention.start() < end for start, end in judged):
            owner = mention.group().decode("utf-8")
            refuse(raw.count(b"\n", 0, mention.start()), [d for d in entries if d.owner == owner])

    if bound:
        for template in _TEMPLATE_TAG.finditer(source):
            if _SRC.search(template.group()):
                for local in sorted(bound):
                    escape(template.start(), local)  # Vue's markup, in another file
        names = "|".join(re.escape(local) for local in sorted(bound))
        for store in re.finditer(rf"(?<![\w$])\$({names})(?![\w$])", code):
            escape(store.start(), store.group(1))
        edges = [0] + [offset for _tag, start, end in regions for offset in (start, end)] + [len(source)]
        for local in sorted(bound):
            hyphenated = "".join(f"(?:{c}|-{c.lower()})" if c.isupper() else re.escape(c) for c in local)
            spelled = re.compile(rf"(?<![A-Za-z0-9_]){hyphenated}(?![A-Za-z0-9_])")
            for begin, end in zip(edges[::2], edges[1::2]):
                for mention in spelled.finditer(source, begin, end):
                    escape(mention.start(), local)
    return [found[k] for k in sorted(found)]


#: An enum name no source can spell. Judged as that enum's file, a module has
#: every ``ns.<export>`` taken for another export's and left alone, so what
#: the enum escape scan still finds is the module object itself handed on.
_NO_ENUM = "\x00"


def _whole_modules(modules: Iterable[str]) -> tuple[xk.KeyDeclaration, ...]:
    return tuple(xk.KeyDeclaration(m, _NO_ENUM, _NO_ENUM, m) for m in sorted(modules))


def module_pattern(modules: Iterable[str], repo_dir: str) -> re.Pattern[str]:
    """What a file's text holds when it can reach one of ``modules`` (see
    :func:`enum_module_pattern`)."""
    return enum_module_pattern(_whole_modules(modules), repo_dir)


def module_escapes(
    path: str, source: str, modules: Iterable[str], repo_dir: str, pattern: re.Pattern[str] | None = None
) -> list[xk.Refusal]:
    """Every use in ``path`` that hands one of ``modules`` on as an object,
    which can then reach any of its exports by a name computed at run time.

    Judged as :func:`enum_escapes` judges an enum's file: a namespace import
    or ``require`` of it used as anything but ``ns.<export>`` or a type, a
    re-export of it in any form, a dynamic ``import()``, ``import m =
    require(...)``, and every loader that can load it. A Vue, Svelte or
    Astro file is judged by :func:`component_escapes`, and its markup by
    :func:`component_match` against ``pattern`` (built when not given)."""
    entries = _whole_modules(modules)
    if Path(path).suffix not in source_index.COMPONENT_SUFFIXES:
        return enum_escapes(path, source, entries, repo_dir)
    found = component_escapes(path, source, entries, repo_dir)
    hit = component_match(path, source, (pattern or module_pattern(modules, repo_dir),))
    if hit is not None:
        line = source.count("\n", 0, hit) + 1
        found.append(xk.Refusal(path, line, xk.UNSUPPORTED_FORM, source.split("\n")[line - 1].strip()))
    return found


def constant_module_escapes(path: str, repo_dir: str) -> list[xk.Refusal]:
    """Each use that hands the module declaring a key constant at ``path`` on
    as an object, ``unsupported-form``: a namespace import or ``require`` of
    it used as anything but ``ns.<export>`` or a type, any re-export of it but
    a barrel's ``export * from`` (followed instead), a dynamic ``import()``,
    ``import m = require(...)``, and every loader that can load it, found
    through any specifier that can name it. Judged as a function wrapper's
    module is (see :func:`flag_cleanup.wrapper_ts.handed_on`)."""
    from flag_cleanup import wrapper_ts

    return [
        xk.Refusal(where, line, xk.UNSUPPORTED_FORM, text)
        for where, line, text in wrapper_ts.handed_on(repo_dir, path)
    ]


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """Empty: a TS/JS key constant has no owner, so no owner can compete."""
    return set()


_PROFILE = xk.Profile(
    language_family=frozenset({"ts", "tsx", "js"}),
    declarations=declarations,
    references=references,
    declared_names=declared_names,
    declared_classifiers=declared_classifiers,
    entry_findings=entry_findings,
    enum_escapes=enum_escapes,
    enum_module_pattern=enum_module_pattern,
    enum_unparsed_suffixes=source_index.COMPONENT_SUFFIXES,
    enum_unparsed_match=component_match,
    enum_unparsed_escapes=component_escapes,
    module_escapes=constant_module_escapes,
)
for _language_name in ("ts", "tsx", "js"):
    xk.register(_language_name, _PROFILE)
