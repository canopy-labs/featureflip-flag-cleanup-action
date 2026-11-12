"""Whether a TypeScript or JavaScript file can change what an instance of a
class reads: the question a key holder (``new Feature("key", false)``) needs
answered before its key can be trusted.

The holder's class is proven to store the key in an own field
(:func:`flag_cleanup.key_const._ts_class_keeps_first_argument`). Only a change
to the class's prototype chain can then change what an instance reads: a
setter defined for the key field on ``Feature.prototype`` (or on
``Object.prototype``) runs instead of the constructor's assignment, and a
getter there can stand in for it. This module finds every place in the
checkout that could make that change, from the parsed code: strings, comments
and JSX text never count.

Two checks, and a file reaches the prototype when either does; the line
named is the first either finds:

* A floor (:func:`_token_line`): the file's CODE -- identifiers and property
  names, never a string, comment, template text or JSX text -- spells the
  class (or an alias) and one of ``prototype``, ``defineProperty``,
  ``defineProperties``, ``setPrototypeOf``, ``__proto__``, ``getPrototypeOf``,
  ``Reflect``, ``constructor`` (not a class's own constructor), ``assign``,
  ``eval`` or ``Function`` (not as a type). This is the text check this
  module replaced, asked of code alone, so the judge below can only ever add
  to what it found.
* The parsed judge (:class:`_FileJudge`), which also sees what a file that
  never spells the class can do.

The judge's model, and why it is closed:

* The prototype is reached by name only through the class's own value.
  Within a file that value is spelled through a binding: the class's
  declaration, an import of it (under any alias, through a re-export that
  renames it, which the repository's text names, through a default export
  and every ``default as M`` of the repository, or ``import F = M.Feature``),
  a destructuring of a module
  object (``const { Feature } = require(...)``), a member named like it
  (``ns.Feature``, ``window.Feature``), or a global of its name. Every use of
  that value is judged: constructing it, reading a static member, ``instanceof``,
  a comparison, ``typeof``, ``extends`` and a re-export are harmless; its
  ``prototype`` (or ``__proto__``), a computed member, a write to a member,
  and anything that hands the value on (an argument, an assignment, a return,
  an element of an object or array) reach it. Once handed on, the value can
  go anywhere, so the place it was handed on is the place reported.
* The prototype is reached without the name through an instance:
  ``Object.getPrototypeOf(x)``, ``Reflect.getPrototypeOf(x)`` and
  ``x.__proto__`` hand out its prototype, and ``x.constructor`` its class.
  Each reaches it unless the value is only looked at: compared, tested, a
  member read off it, handed to a function that only reads
  (``Object.getOwnPropertyNames``), or stored in a name every use of which is
  one of those.
* ``Object.prototype`` is in every instance's chain: it is judged the same
  way, so ``Object.prototype.hasOwnProperty.call(o, k)`` reaches nothing.
* Reflection is recognised however it is spelled: ``Object.getPrototypeOf``,
  ``globalThis.Object...``, ``const O = Object``, ``const { defineProperty }
  = Object``, ``const gpo = Object.getPrototypeOf``, ``fn.call(null, x)``;
  one of those functions handed on as a value reaches it.
* Destructuring that takes out ``constructor``, ``prototype``, ``__proto__``
  or a computed key reaches it, as the member read would.
* ``Object.defineProperty``, ``defineProperties``, ``setPrototypeOf``,
  ``Object.assign``, ``Reflect.defineProperty``, ``Reflect.setPrototypeOf`` and
  ``Reflect.set`` reach it when their first argument is the class, its
  prototype, or, in a file that imports, declares or spells the class,
  anything not provably something else (a fresh object, another class or
  import of the file, a member named otherwise, a global such as
  ``HTMLElement``). ``x.prototype`` follows the same rule. Elsewhere an
  unknown value can only be the class or its prototype if a place above
  handed it out, and that place is reported.
* ``eval`` and ``Function`` run code no parse can see.

A file that does not parse falls back to the text: it reaches the prototype
when it spells the class's name (or an alias) and ``prototype``,
``defineProperty``, ``defineProperties`` or ``setPrototypeOf``, or spells
``__proto__``, ``getPrototypeOf`` or ``Object.prototype`` anywhere but before
a method borrowed with ``.call``/``.apply``/``.bind``.

Judged by what a single file cannot prove, in two tiers. Any file that
matches a narrow prefilter (:data:`_SCOPED_TOKENS`) is asked a small set of
rules that need no mention of the class (:meth:`_FileJudge._scoped`): ``eval`` in
any spelling and ``Function``, an exported alias of a reflection function, a
module used whole (``Object.values(ns)``, ``export { ns }``, ``require("./x")``
handed on, an ``export * as ns`` re-exported), and ``import.meta.glob`` /
``require.context``. The ``vm`` module and a timer given a string are asked only
of files an instance can reach (:class:`_Context`: the import-connected part of
the checkout that holds the class). Only a file that spells the class or a
prototype is asked the rest.

Not seen, and so not withheld (named residuals): a computed member read whose key
the file does not fix (``x[k]`` with ``k: string``, which may be ``x["constructor"]``;
measured on real repositories it withheld every holder and was a plain lookup
table nine times in ten); a holder instance changed through another name
(``useFeature(f)`` doing ``f.id = 'x'``), which is not a prototype change and
needs the instance's aliases; a setup, polyfill or config-loaded file that no
import connects to the class (jest ``setupFiles``, a webpack entry); code
assembled from strings held in a parameter; and an instance handed to a file
outside the import-connected part (dependency injection, a global registry).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import ts_syntax

#: Every TypeScript and JavaScript suffix a file of the checkout can have.
EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

#: A file whose text holds none of these, nor a name of the class, cannot
#: reach the prototype (see the module docstring), so it is never parsed.
_UNNAMED_TOKENS = re.compile(
    r"prototype|__proto__|getPrototypeOf|\??\.\s*constructor\b|\[\s*[\"'`]constructor"
)

#: The text fallback for a file that does not parse.
_PATCH_TEXT = re.compile(r"\bprototype\b|\b(?:defineProperty|defineProperties|setPrototypeOf)\b")
_UNNAMED_TEXT = re.compile(
    r"__proto__|getPrototypeOf|\bObject\s*\.\s*prototype\b"
    r"(?!\s*\.\s*[\w$]+\s*\.\s*(?:call|apply|bind)\b)"
)

#: ``(object, method)`` calls that change their first argument's properties
#: or prototype.
_PATCHERS = frozenset(
    {
        ("Object", "defineProperty"),
        ("Object", "defineProperties"),
        ("Object", "setPrototypeOf"),
        ("Object", "assign"),
        ("Reflect", "defineProperty"),
        ("Reflect", "setPrototypeOf"),
        ("Reflect", "set"),
    }
)

#: Names that mean the reflection function whatever owns them.
_UNAMBIGUOUS = frozenset({"defineProperty", "defineProperties", "setPrototypeOf", "getPrototypeOf"})

#: The globals whose methods are reflection.
_REFLECTING_GLOBALS = frozenset({"Object", "Reflect"})

#: Wrappers that hand their operand through unchanged.
_TRANSPARENT = frozenset(
    {"parenthesized_expression", "non_null_expression", "as_expression", "satisfies_expression"}
)

#: Values that are a new object, never an existing class or prototype.
_FRESH = frozenset(
    {
        "new_expression",
        "object",
        "array",
        "function_expression",
        "function",
        "arrow_function",
        "class",
        "string",
        "template_string",
        "number",
        "regex",
        "true",
        "false",
        "null",
        "undefined",
    }
)

#: The ``Object.prototype`` methods that define a property on their receiver.
_DEFINERS = frozenset({"__defineGetter__", "__defineSetter__"})

#: ``(object, method)`` calls that only read their argument.
_INSPECTORS = frozenset(
    {
        ("Object", "getOwnPropertyNames"),
        ("Object", "getOwnPropertyDescriptor"),
        ("Object", "getOwnPropertyDescriptors"),
        ("Object", "getOwnPropertySymbols"),
        ("Object", "keys"),
        ("Object", "isFrozen"),
        ("Object", "isSealed"),
        ("Object", "isExtensible"),
        ("Object", "is"),
        ("Reflect", "ownKeys"),
        ("Reflect", "has"),
        ("Reflect", "getOwnPropertyDescriptor"),
        ("Reflect", "isExtensible"),
        # A new object whose prototype it is: the prototype is not changed.
        ("Object", "create"),
        ("Error", "captureStackTrace"),
    }
)

#: The reflection functions whose every use is judged: a patcher, or
#: ``getPrototypeOf``, which hands out a prototype.
_REFLECTION = _PATCHERS | {("Object", "getPrototypeOf"), ("Reflect", "getPrototypeOf")}

#: Functions called by name that only read their argument: a test's
#: assertion subject (``expect(Object.getPrototypeOf(x)).toBe(...)``).
_INSPECTING_FUNCTIONS = frozenset({"expect"})

#: Members that hand out a class or a prototype from any value.
_CLASS_MEMBERS = frozenset({"constructor", "prototype", "__proto__", "Object"})

#: The node types the scoped rules look at.
_SCOPED_TYPES = frozenset(
    {
        "identifier",
        "string",
        "call_expression",
        "new_expression",
        "member_expression",
        "subscript_expression",
        "pair_pattern",
        "shorthand_property_identifier_pattern",
        "export_specifier",
        "shorthand_property_identifier",
    }
)

#: The members that hand out a class or a prototype.
_CLASS_MEMBERS_DANGEROUS = frozenset({"constructor", "prototype", "__proto__"})

#: The global object's names: a member of one is a global.
_GLOBAL_OBJECTS = frozenset({"window", "globalThis", "global", "self"})

_COMPARISONS = frozenset({"===", "!==", "==", "!="})
_PROTOTYPE_KEYS = frozenset({"prototype", "__proto__"})


@dataclass(frozen=True)
class Site:
    """Where a file reaches a class's prototype: the file's resolved path, the
    1-based line, and that line's text."""

    path: str
    line: int
    text: str


def reached(name: str, repo_root: str) -> Site | None:
    """The first place in the checkout at ``repo_root`` that can change what an
    instance of the class ``name`` reads, or None."""
    from flag_cleanup import source_index

    texts: list[tuple[str, str]] = []
    for listed in source_index.files(repo_root, EXTENSIONS):
        try:
            texts.append((listed.resolved, Path(listed.resolved).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    names = aliases(name, [text for _path, text in texts])
    spelled = _spelling(names)
    context = _Context(repo_root, texts, spelled, names)
    for path, text in texts:
        wide = bool(_UNNAMED_TOKENS.search(text) or (spelled is not None and spelled.search(text)))
        if not wide and not _scoped_match(text):
            continue
        site = file_site(path, text, names, context, scoped_only=not wide)
        if site is not None:
            return site
    return None


#: Text that makes a file worth parsing for the rules that need no mention of
#: the class (see :meth:`_FileJudge._scoped`), and only for those: a module
#: bound whole or enumerated, ``import.meta.glob``/``require.context``, the
#: ``vm`` module, a timer given a string, ``eval`` / ``Function``, or an alias
#: of a reflection function exported by name. Deliberately narrow: a file that
#: only matches this is not judged by the rest of the rules.
_SCOPED_TOKENS = re.compile(
    r"\bimport\s*\*|\bexport\s*\*\s*as\b|\bimport\s+\w+\s*=\s*require\b"
    r"|\bimport\.meta\.glob|\brequire\.context|\bvm\b"
    r"|\bset(?:Timeout|Interval)\s*\(\s*[\"'`]"
    r"|\b(?:require|import)\s*\(\s*[\"'`](?:[.~#]|@/)"
    r"|\beval\b|\bnew\s+Function\b|\bFunction\s*\("
    r"|\bexport\s+(?:const|let|var)\s+(?:\{[^}]*\}|[\w$]+)\s*(?::[^=]+)?=\s*(?:\(\s*)?(?:(?:globalThis|window|self|global)\s*\.\s*)?(?:Object|Reflect)\b"
    r"|\bexports?\s*\.\s*[\w$]+\s*=\s*(?:Object|Reflect)\b"
    r"|[\"'`]\s*(?:constructor|prototype|__proto__)\s*[\"'`]|\bReflect\s*(?:\.\s*get\b|\[)"
)


#: A reflection function named in the text: with ``export`` it may be handed on
#: under another name (``const def = Object.defineProperty; export { def }``).
_REFLECTION_MENTION = re.compile(r"\b(?:Object|Reflect)\s*\.\s*(?:defineProperty|defineProperties|setPrototypeOf|assign|set)\b")


def _scoped_match(text: str) -> bool:
    return bool(_SCOPED_TOKENS.search(text) or ("export" in text and _REFLECTION_MENTION.search(text)))


class _Context:
    """What a single file cannot know about the checkout: whether it can be
    handed an instance of the class (it lies in the import-connected part of
    the repository that holds the class), and whether a module it imports
    does. Instances flow along imports in both directions: to a callee, and
    back from a factory. Built on first use, so a checkout none of whose files
    needs the answer pays nothing."""

    _SPECIFIER = re.compile(
        r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)(['"])([^'"\n]+)\1"""
    )
    _NAMESPACE_EXPORT = re.compile(r"\bexport\s*\*\s*as\s+([A-Za-z_$][\w$]*)\s+from")

    def __init__(
        self, repo_root: str, texts: list[tuple[str, str]], spelled: re.Pattern[str] | None, names: frozenset[str] = frozenset()
    ) -> None:
        self.repo_root = repo_root
        self._names = names
        self._texts = texts
        self._spelled = spelled
        self.paths = [path for path, _text in texts]
        self._pathset = frozenset(self.paths)
        self._text_of = dict(texts)
        self._exports: dict[str, bool] = {}
        self._targets: dict[tuple[str, str], frozenset[str]] = {}
        self._tails: dict[str, list[str]] | None = None
        self._component: set[str] | None = None
        #: Names given to a whole module by ``export * as ns from "./x"``.
        self.namespace_exports = frozenset(
            name for _path, text in texts if "export" in text for name in self._NAMESPACE_EXPORT.findall(text)
        )

    @property
    def component(self) -> set[str]:
        if self._component is None:
            adjacency: dict[str, set[str]] = {path: set() for path in self.paths}
            for path, text in self._texts:
                for _quote, specifier in self._SPECIFIER.findall(text):
                    for target in self.resolve(path, specifier):
                        adjacency.setdefault(target, set()).add(path)
                        adjacency[path].add(target)
            seeds = [p for p, text in self._texts if self._spelled is not None and self._spelled.search(text)]
            found: set[str] = set(seeds)
            pending = list(seeds)
            while pending:
                for neighbour in adjacency.get(pending.pop(), ()):
                    if neighbour not in found:
                        found.add(neighbour)
                        pending.append(neighbour)
            self._component = found
        return self._component

    def _tail_index(self) -> dict[str, list[str]]:
        """Every file under each of its path-suffixes (``a/b/c`` -> ``b/c``, ``c``),
        extension and ``/index`` removed: the answer for a specifier no resolver
        settles (a ``baseUrl`` or bundler alias)."""
        if self._tails is None:
            index: dict[str, list[str]] = {}
            for path in self.paths:
                bare = re.sub(r"\.(?:[cm]?[jt]sx?)$", "", path)
                if bare.endswith("/index"):
                    bare = bare[: -len("/index")]
                parts = bare.split("/")
                for start in range(len(parts)):
                    index.setdefault("/".join(parts[start:]), []).append(path)
            self._tails = index
        return self._tails

    def resolve(self, reader: str, specifier: str) -> frozenset[str]:
        """The checkout files ``specifier`` may name. A relative specifier is
        joined and probed against the checkout's own file list (no configuration
        is read: one resolver call per specifier cost seconds on a large tree).
        A specifier that names no file but could (relative, or under an alias or
        ``baseUrl``) names every file whose path ends with it: more files only
        makes more places count. A bare name goes to the resolver once, so that
        a workspace package or a ``paths`` entry is followed; one it cannot
        settle names none."""
        bare = not self.looks_local(specifier)
        key = (specifier,) if bare else (reader, specifier)
        if key in self._targets:
            return self._targets[key]
        found: set[str] = set()
        if specifier.startswith("."):
            base = os.path.normpath(os.path.join(os.path.dirname(reader), specifier))
            stem = re.sub(r"\.(?:[cm]?[jt]sx?)$", "", base)
            for candidate in (
                base,
                *(base + ext for ext in EXTENSIONS),
                *(stem + ext for ext in EXTENSIONS),
                *(os.path.join(base, "index" + ext) for ext in EXTENSIONS),
            ):
                if candidate in self._pathset:
                    found.add(candidate)
                    break
        elif bare:
            from flag_cleanup import cross_file_ts

            try:
                target = cross_file_ts.resolve_specifier(reader, specifier, self.repo_root, solution=True, workspace=True)
            except Exception:  # noqa: BLE001 - an unreadable config only widens the answer
                target = None
            if target is not None:
                found.add(str(Path(target).resolve()))
        if not found and (not bare or "/" in specifier):
            tail = re.sub(r"\.(?:[cm]?[jt]sx?)$", "", specifier.lstrip("./~#@")).rstrip("/")
            if tail:
                found.update(self._tail_index().get(tail, ()))
        result = frozenset(found)
        self._targets[key] = result
        return result

    @staticmethod
    def looks_local(specifier: str) -> bool:
        """Whether ``specifier`` could name a file of the checkout rather than a package."""
        return specifier.startswith((".", "/", "~", "#", "@/"))

    def reachable(self, path: str) -> bool:
        return path in self.component

    _REEXPORT = re.compile(
        r"""\bexport\s*(?:\*(?:\s*as\s+[A-Za-z_$][\w$]*)?|\{[^}]*\})\s*from\s*(['"])([^'"\n]+)\1"""
    )

    def exports_class(self, path: str, _seen: set[str] | None = None) -> bool:
        """Whether the module at ``path`` may export the class: its text spells
        the class (or an alias) and exports something, or it re-exports from a
        module that does (``export *``, ``export { … } from``), followed
        transitively and cycle-safe."""
        cached = self._exports.get(path)
        if cached is not None:
            return cached
        seen = _seen if _seen is not None else set()
        if path in seen:
            return False
        seen.add(path)
        text = self._text_of.get(path, "")
        found = bool(
            self._spelled is not None
            and "export" in text
            and self._spelled.search(text)
            and _binds_class_value(path, text, self._names)
        )
        if not found and "export" in text:
            for _quote, specifier in self._REEXPORT.findall(text):
                targets = self.resolve(path, specifier)
                if targets:
                    if any(self.exports_class(target, seen) for target in targets):
                        found = True
                        break
                elif self.looks_local(specifier) and not self.is_data(specifier):
                    found = True  # fail closed, as `module_holds_class` does
                    break
        if _seen is None:  # a verdict reached inside a cycle is not final
            self._exports[path] = found
        return found

    #: Suffixes of files that load as data or assets: no class in them.
    _DATA_SUFFIXES = frozenset(
        {
            ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp", ".json", ".css", ".scss",
            ".sass", ".less", ".styl", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".ogg",
            ".webm", ".md", ".txt", ".html", ".htm", ".yaml", ".yml", ".graphql", ".gql", ".csv", ".pdf", ".wasm",
        }
    )

    @classmethod
    def is_data(cls, specifier: str) -> bool:
        """Whether ``specifier`` names an asset or data file (``./a.png``, ``./x.json?raw``)."""
        last = specifier.rstrip("/").rsplit("/", 1)[-1]
        return "." in last and "." + last.rsplit(".", 1)[-1].split("?")[0].lower() in cls._DATA_SUFFIXES

    def module_holds_class(self, reader: str, specifier: str) -> bool:
        """Whether the module ``specifier`` names may export the class: it is
        a file of the checkout that exports it (see :meth:`exports_class`), or
        looks like one but cannot be resolved (taken to hold it). A specifier
        with a data or asset suffix holds none."""
        if self.is_data(specifier):
            return False
        targets = self.resolve(reader, specifier)
        if targets:
            return any(self.exports_class(target) for target in targets)
        return self.looks_local(specifier)

    def glob_may_match(self, reader: str, patterns: list[str] | None) -> bool:
        """Whether an ``import.meta.glob`` / ``require.context`` pattern could
        match a file of the part of the checkout that holds the class. A pattern
        the file does not fix (``None``) matches."""
        if patterns is None:
            return True
        directory = str(Path(reader).parent)
        for pattern in patterns:
            if pattern.startswith("!"):
                continue
            if pattern.startswith("."):
                base = str(Path(directory, pattern).resolve()) if "*" not in pattern else directory + "/" + pattern
            elif pattern.startswith("/"):
                base = self.repo_root.rstrip("/") + pattern
            else:
                base = "*/" + pattern.lstrip("@~#/")
            expression = _glob_regex(base)
            if any(expression.fullmatch(path) or expression.match(path + "/") for path in self.component):
                return True
        return False


def _binds_class_value(path: str, text: str, names: frozenset[str]) -> bool:
    """Whether the file holds a VALUE binding of the class: a ``class``
    declaration or ``const`` of its name, a non-``type`` import of it (or the
    default import, when the class is a default export), or a destructured
    ``require``. A type-only mention (``import type { Feature }``, a parameter
    type) can export nothing the class's prototype hangs on. A file that does
    not parse is taken to bind it."""
    root = ts_syntax._parse(text, _language(path))
    if root.has_error:
        return True
    code_names = {n for n in names if n != "default"}
    for node in _preorder(root):
        kind = node.type
        if kind in ("class_declaration", "abstract_class_declaration", "class"):
            name = node.child_by_field_name("name")
            if name is not None and name.text.decode("utf-8") in code_names:
                return True
        elif kind == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "identifier" and name.text.decode("utf-8") in code_names:
                return True
            if name is not None and name.type == "object_pattern":
                for child in name.named_children:
                    key = child if child.type == "shorthand_property_identifier_pattern" else child.child_by_field_name("key")
                    if key is not None and key.text.decode("utf-8") in code_names:
                        return True
        elif kind == "import_specifier":
            imported = node.child_by_field_name("name")
            if (
                imported is not None
                and imported.text.decode("utf-8") in code_names
                and not any(c.type == "type" for c in node.children)
                and not _FileJudge._type_only(node)
            ):
                return True
        elif kind == "import_clause" and "default" in names and not _FileJudge._type_only(node):
            if any(c.type == "identifier" for c in node.named_children):
                return True
    return False


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    pattern = re.sub(r"/\./", "/", pattern)
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "{":
            end = pattern.find("}", i)
            if end == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("(?:" + "|".join(re.escape(x) for x in pattern[i + 1 : end].split(",")) + ")")
                i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out))


#: Whitespace and comments between the words of a declaration: a comment in
#: ``export default /* c */ Feature`` must not hide the alias.
_GAP = r"(?:\s++|/\*(?s:.*?)\*/|//[^\n]*+(?:\n|$))++"


def aliases(name: str, texts: list[str]) -> frozenset[str]:
    """``name`` and every name the repository's text gives it: ``N as M`` in
    an import or export (``export { Feature as Flag }``), followed through
    each new name, and ``default`` when one is exported as the default
    (``export default Feature``, ``export default (Feature)``,
    ``export { Feature as default }``), followed in turn through every
    ``default as M`` of the repository.

    Text, so a type assertion (``x as Feature``), a comment, or a default
    re-export of another module can only add names, which only makes more
    places count."""
    found = {name}
    pending = [name]
    while pending:
        current = pending.pop()
        token = re.escape(current)
        renamed = re.compile(rf"(?<![\w$]){token}{_GAP}as{_GAP}([A-Za-z_$][\w$]*)")
        default = re.compile(rf"\bexport{_GAP}default{_GAP}(?:class{_GAP})?(?:\(|{_GAP})*+{token}(?![\w$])")
        for text in texts:
            if current not in text:
                continue
            for match in renamed.finditer(text):
                if match.group(1) not in found:
                    found.add(match.group(1))
                    pending.append(match.group(1))
            if current != "default" and "default" not in found and default.search(text):
                found.add("default")
                pending.append("default")
    return frozenset(found)


def _spelling(names: frozenset[str]) -> re.Pattern[str] | None:
    words = sorted(n for n in names if n != "default")
    if not words:
        return None
    return re.compile(r"(?<![\w$])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w$])")


def _language(path: str) -> str:
    if path.endswith(".tsx"):
        return "tsx"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "js"
    return "ts"


def file_site(
    path: str, text: str, names: frozenset[str], context: _Context | None = None, *, scoped_only: bool = False
) -> Site | None:
    """Where the file at ``path``, holding ``text``, reaches the prototype of
    the class ``names`` spell (:func:`aliases`), or None: the first line
    where either the token check (:func:`_token_line`) or the parsed judge
    (:class:`_FileJudge`) finds it."""
    root = ts_syntax._parse(text, _language(path))
    if root.has_error:
        return _scoped_text_site(path, text) if scoped_only else _text_site(path, text, names)
    lines = [None if scoped_only else _token_line(root, names)]
    node = _FileJudge(root, names, path, context).first(scoped_only=scoped_only)
    if node is not None:
        lines.append(node.start_point[0] + 1)
    found = [line for line in lines if line is not None]
    return _site(path, text, min(found)) if found else None


#: Code tokens: what is left of a file once strings, comments, template text
#: and JSX text are taken out.
_CODE_TOKENS = frozenset(
    {
        "identifier",
        "property_identifier",
        "type_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "private_property_identifier",
    }
)

#: Words that, in the code of a file that also spells the class, may reach
#: its prototype: the text check this module replaced, and more.
_TRIGGERS = frozenset(
    {
        "prototype",
        "defineProperty",
        "defineProperties",
        "setPrototypeOf",
        "__proto__",
        "getPrototypeOf",
        "Reflect",
        "constructor",
        "assign",
        "eval",
        "Function",
    }
)


def _token_line(root: Node, names: frozenset[str]) -> int | None:
    """The floor under the parsed judge: when the file's CODE spells the class
    (or an alias) and one of :data:`_TRIGGERS`, the line of the first trigger.

    This is the text check the parsed judge replaced, asked of code tokens
    alone, so a string, a comment or JSX text never counts and the judge can
    never answer less than it did. A class's own ``constructor`` method and a
    ``Function`` type are not triggers: neither runs anything."""
    spelled = False
    first: int | None = None
    for node in _walk_all(root):
        if node.type not in _CODE_TOKENS:
            continue
        word = node.text.decode("utf-8")
        if word in names and word != "default":
            spelled = True
        if word in _TRIGGERS and first is None and not _inert_trigger(node, word):
            first = node.start_point[0] + 1
        if spelled and first is not None:
            return first
    return None


def _inert_trigger(node: Node, word: str) -> bool:
    parent = node.parent
    if word == "constructor" and parent is not None:
        return parent.type in ("method_definition", "method_signature", "abstract_method_signature") and _is_field(
            parent, "name", node
        )
    return word == "Function" and node.type == "type_identifier"


def _walk_all(node: Node):
    """Every node under ``node``, in source order, type queries included."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _site(path: str, text: str, line: int) -> Site:
    lines = text.split("\n")  # the parser's rows: `splitlines` also splits at U+2028
    return Site(path, line, lines[line - 1].strip() if 0 < line <= len(lines) else "")


_SCOPED_TEXT = re.compile(r"\beval\b|\bnew\s+Function\b|\bFunction\s*\(|[\"'`](?:node:)?vm[\"'`]")


def _scoped_text_site(path: str, text: str) -> Site | None:
    """The fallback for an unparseable file that matched only the narrow
    prefilter: ``eval``, ``Function`` and ``vm`` anywhere in its text, taken to
    run code (fail closed)."""
    match = _SCOPED_TEXT.search(text)
    return _site(path, text, text.count("\n", 0, match.start()) + 1) if match is not None else None


def _text_site(path: str, text: str, names: frozenset[str]) -> Site | None:
    """The fallback for a file that does not parse: its first matching line."""
    spelled = _spelling(names)
    match = _UNNAMED_TEXT.search(text)
    if match is None and spelled is not None and spelled.search(text):
        match = _PATCH_TEXT.search(text)
    if match is None:
        return None
    return _site(path, text, text.count("\n", 0, match.start()) + 1)


def _unwrap(node: Node | None) -> Node | None:
    while node is not None and node.type in _TRANSPARENT:
        values = [c for c in node.named_children if c.type != "comment"]
        node = values[0] if values else None
    return node


def _outer(node: Node) -> Node:
    """``node`` with every transparent wrapper around it, outermost."""
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        values = [c for c in node.parent.named_children if c.type != "comment"]
        if not values or values[0].id != node.id:
            break
        node = node.parent
    return node


def _is_field(parent: Node, field: str, node: Node) -> bool:
    child = parent.child_by_field_name(field)
    return child is not None and child.id == node.id


def _string_value(node: Node | None) -> str | None:
    """The value of a plain string literal, or None."""
    if node is None or node.type != "string":
        return None
    parts = [c for c in node.named_children]
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].type == "string_fragment":
        return parts[0].text.decode("utf-8")
    return None


def _written(member: Node) -> bool:
    """Whether ``member`` (a member or subscript expression) is assigned,
    updated, deleted or destructured into."""
    member = _outer(member)
    parent = member.parent
    if parent is None:
        return False
    if parent.type in ("assignment_expression", "augmented_assignment_expression", "for_in_statement"):
        return _is_field(parent, "left", member)
    if parent.type == "update_expression":
        return True
    if parent.type == "unary_expression":
        operator = parent.child_by_field_name("operator")
        return operator is not None and operator.type == "delete"
    return parent.type in (
        "array_pattern",
        "object_pattern",
        "pair_pattern",
        "assignment_pattern",
        "rest_pattern",
    )


class _FileJudge:
    """One parsed file, judged against one class."""

    def __init__(self, root: Node, names: frozenset[str], path: str = "", context: _Context | None = None) -> None:
        self.root = root
        self.names = names
        self.path = path
        self.context = context
        self.code_names = frozenset(n for n in names if n != "default")
        self._bound = False

    def _bind(self) -> None:
        """Collect the file's bindings; asked for on first use, since the scoped
        rules of a file that matched only the narrow prefilter seldom need them."""
        self._bound = True
        root = self.root
        # name -> binder kinds: "class" (may be the class) or "else"
        self._binders: dict[str, list[tuple[str, Node]]] = {}
        self._binder_ids: set[int] = set()
        bound = ts_syntax.binding_nodes(root) + [
            # `binding_nodes` follows `name` fields only, so the local name
            # of `import { N as M }` is added here.
            alias
            for node in _preorder(root)
            if node.type == "import_specifier"
            and (alias := node.child_by_field_name("alias")) is not None
        ] + [
            # `import F = M.Feature` names its binding with no field at all.
            node.named_children[0]
            for node in _preorder(root)
            if node.type == "import_alias"
            and node.named_children
            and node.named_children[0].type == "identifier"
        ]
        seen: set[int] = set()
        for node in bound:
            if node.id in seen:
                continue
            seen.add(node.id)
            kind = self._binder_kind(node)
            if kind is None:
                continue
            self._binder_ids.add(node.id)
            self._binders.setdefault(node.text.decode("utf-8"), []).append((kind, node))
        self._local = frozenset(
            name
            for name, kinds in self._binders.items()
            if any(kind == "class" for kind, _node in kinds)
        ) | frozenset(n for n in self.code_names if n not in self._binders)

    @property
    def binders(self) -> dict[str, list[tuple[str, Node]]]:
        if not self._bound:
            self._bind()
        return self._binders

    @property
    def binder_ids(self) -> set[int]:
        if not self._bound:
            self._bind()
        return self._binder_ids

    @property
    def local(self) -> frozenset[str]:
        if not self._bound:
            self._bind()
        return self._local

    @property
    def reachable(self) -> bool:
        """Whether this file can be handed an instance of the class (see
        :class:`_Context`). Without a context, every file can."""
        return self.context is None or self.context.reachable(self.path)

    # --- bindings ---------------------------------------------------------

    def _binder_kind(self, node: Node) -> str | None:
        """``"class"`` for a binding that may hold the class, ``"else"`` for
        one that holds something else or is unknown, None for no value
        binding at all (a type, a re-export, an import's imported name)."""
        parent = node.parent
        if parent is None:
            return "else"
        if parent.type == "export_specifier":
            return None
        if parent.type == "import_specifier":
            alias = parent.child_by_field_name("alias")
            if alias is not None and alias.id != node.id:
                return None  # the imported name of `N as M`
            if self._type_only(parent):
                return None
            imported = parent.child_by_field_name("name")
            text = imported.text.decode("utf-8") if imported is not None else ""
            return "class" if text in self.names else "import"
        if parent.type == "import_alias":
            spelled = {
                n.text.decode("utf-8")
                for n in _walk_all(parent)
                if n.id != node.id and n.type in ("identifier", "property_identifier")
            }
            return "class" if spelled & self.names else "import"
        if parent.type == "import_clause":
            return "class" if "default" in self.names and not self._type_only(parent) else "import"
        if parent.type == "namespace_import":
            return "import"
        if node.type == "type_identifier":
            if parent.type in ("class_declaration", "abstract_class_declaration"):
                return "class" if node.text.decode("utf-8") in self.names else "declared"
            return None  # a type alias or an interface: no value
        if node.type == "shorthand_property_identifier_pattern":
            return "class" if node.text.decode("utf-8") in self.names else "else"
        if parent.type == "pair_pattern":
            key = parent.child_by_field_name("key")
            if key is not None and key.text.decode("utf-8") in self.names:
                return "class"
            return "else"
        if parent.type in ("function_declaration", "generator_function_declaration", "enum_declaration"):
            return "declared"
        if parent.type == "variable_declarator" and _is_field(parent, "name", node):
            declaration = parent.parent
            value = _unwrap(parent.child_by_field_name("value"))
            if (
                declaration is not None
                and declaration.type == "lexical_declaration"
                and any(c.type == "const" for c in declaration.children)
                and value is not None
                and node.type == "identifier"
            ):
                return "const"
        return "else"

    @staticmethod
    def _type_only(node: Node) -> bool:
        while node is not None and node.type != "import_statement":
            if any(c.type in ("type", "typeof") for c in node.children):
                return True
            node = node.parent
        return node is not None and any(c.type in ("type", "typeof") for c in node.children)

    def _maybe_class(self, expr: Node | None, *, names_class: bool, depth: int = 0) -> bool:
        """Whether ``expr`` may hold the class or its prototype. With
        ``names_class`` False, an unknown value is taken not to (see the
        module docstring)."""
        expr = _unwrap(expr)
        if expr is None or depth > 8:
            return True
        if expr.type in _FRESH:
            return False
        if expr.type == "identifier":
            name = expr.text.decode("utf-8")
            if name in self.local:
                return True
            kinds = self.binders.get(name)
            if not kinds:
                return False  # a global of another name: `HTMLElement`, `Object`
            for kind, binder in kinds:
                if kind in ("import", "declared"):
                    continue
                if kind == "const":
                    value = binder.parent.child_by_field_name("value") if binder.parent else None
                    if self._maybe_class(value, names_class=names_class, depth=depth + 1):
                        return True
                    continue
                return names_class
            return False
        if expr.type == "member_expression":
            # `View.prototype`, `ns.Other.prototype`, `eeBilling.Repo`: a
            # member named otherwise holds the class only where the class was
            # handed on, which is reported where that happens.
            prop = expr.child_by_field_name("property")
            text = prop.text.decode("utf-8") if prop is not None else ""
            if text in self.code_names or (text == "default" and "default" in self.names):
                return True
            if text == "prototype":
                # `HTMLElement.prototype` is the class's only when
                # `HTMLElement` may be the class.
                obj = expr.child_by_field_name("object")
                return self._maybe_class(obj, names_class=names_class, depth=depth + 1)
            return names_class if text in _CLASS_MEMBERS else False
        if self._names_class_member(expr):
            return True
        return names_class

    def _names_class_member(self, node: Node) -> bool:
        """``ns.Feature``, ``window["Feature"]``: a member named like the class."""
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            text = prop.text.decode("utf-8") if prop is not None else ""
            # `m.default`, `require("./feature").default`: the module's default export.
            return text in self.code_names or (text == "default" and "default" in self.names)
        if node.type == "subscript_expression":
            value = _string_value(node.child_by_field_name("index"))
            return value in self.code_names or (value == "default" and "default" in self.names)
        return False

    # --- the walk ---------------------------------------------------------

    def first(self, *, scoped_only: bool = False) -> Node | None:
        """The first node that reaches the prototype. ``scoped_only`` asks only
        the rules that need no mention of the class (:meth:`_scoped`): a file
        that matched nothing but their narrow prefilter is judged by those alone."""
        if scoped_only:
            for node in _preorder(self.root):
                if self._scoped(node):
                    return node
            return None
        names_class = self._names_class()
        for node in _preorder(self.root):
            if self._reaches(node, names_class):
                return node
        return None

    def _names_class(self) -> bool:
        """Whether the file binds the class's value (an import of it, its
        declaration) or spells it anywhere."""
        if any(kind == "class" for kinds in self.binders.values() for kind, _node in kinds):
            return True
        for node in _preorder(self.root):
            if self._class_reference(node):
                return True
        return False

    def _class_reference(self, node: Node) -> bool:
        """Whether ``node`` is a use (not a binding) of the class's value."""
        if node.type == "identifier":
            if node.id in self.binder_ids or node.text.decode("utf-8") not in self.local:
                return False
            parent = node.parent
            if parent is not None and parent.type in ("import_specifier", "export_specifier", "namespace_import"):
                return False
            if parent is not None and parent.type in ("member_expression",) and _is_field(parent, "property", node):
                return False
            return True
        if node.type == "shorthand_property_identifier":
            return node.text.decode("utf-8") in self.local
        return self._names_class_member(node)

    def _reaches(self, node: Node, names_class: bool) -> bool:
        if self._class_reference(node) and not self._harmless_use(node):
            return True
        kind = node.type
        if self._scoped(node):
            return True
        if kind in ("pair_pattern", "shorthand_property_identifier_pattern"):
            # `const { constructor: C } = x`, `{ __proto__: p }`, `{ [k]: v }`:
            # destructuring takes out what a member read would.
            key = node if kind == "shorthand_property_identifier_pattern" else node.child_by_field_name("key")
            if key is None or key.type == "computed_property_name":
                return True
            return _plain_key(key) in _CLASS_MEMBERS
        if kind in ("member_expression", "subscript_expression"):
            verdict = self._dangerous_member(node, names_class)
            if verdict is not None:
                return verdict
            if self._reflection_value(node) is not None:
                return not self._called(node)
        if kind == "identifier" and node.id not in self.binder_ids and self._reflection_value(node) is not None:
            parent = node.parent
            if parent is not None and parent.type == "member_expression" and _is_field(parent, "property", node):
                return False
            return not self._called(node)
        if kind == "call_expression":
            function = _unwrap(node.child_by_field_name("function"))
            if function is None:
                return False
            if function.type == "identifier" and function.text in (b"eval", b"Function") and (
                function.text.decode("utf-8") not in self.binders
            ):
                return True
            if self._reflective_read(node, names_class):
                return True
            resolved = self._reflection_call(node)
            if resolved is None:
                return False
            (owner, method), first = resolved
            if method == "getPrototypeOf":
                return not self._inspected(node)
            if (owner, method) in _PATCHERS or (owner is None and method in _UNAMBIGUOUS):
                if first is None or first.type == "spread_element":
                    return True
                return self._maybe_class(first, names_class=names_class)
        if kind == "new_expression":
            constructor = _unwrap(node.child_by_field_name("constructor"))
            return constructor is not None and constructor.type == "identifier" and constructor.text == b"Function"
        return False

    def _dangerous_member(self, node: Node, names_class: bool) -> bool | None:
        """Whether a read of ``prototype`` / ``__proto__`` / ``constructor``
        (spelled, or fixed by the file) reaches the prototype; None when the
        member is none of them."""
        key = self._member_key(node)
        obj = node.child_by_field_name("object")
        if key == "prototype":
            inner = _unwrap(obj)
            if self._global_named(inner) == "Object":
                return not self._inspected(node)
            if not self._maybe_class(obj, names_class=names_class):
                return False
            if inner is not None and self._class_reference(inner):
                return True  # `Feature.prototype`, whatever follows
            return not self._inspected(node)
        if key in ("__proto__", "constructor"):
            # An instance hands out its prototype, and its class.
            return not self._inspected(node)
        return None

    # --- what escapes a per-file reading ----------------------------------

    def _scoped(self, node: Node) -> bool:
        """The rules that need no mention of the class: code run from a string,
        a module used whole, a glob over modules, an exported alias of a
        reflection function. They are what a file matching only the narrow
        prefilter is judged by."""
        if node.type not in _SCOPED_TYPES:
            return False
        return (
            self._runs_code(node)
            or self._evals(node)
            or self._function_constructor(node)
            or self._enumerates_module(node)
            or self._globs_modules(node)
            or self._exports_reflection(node)
            or self._exported_reflection_value(node)
            or self._fixed_key_read(node)
        )

    def _exported_reflection_value(self, node: Node) -> bool:
        """``export const def = Object.defineProperty`` and ``const def =
        Object.defineProperty; export { def }``: importers use the name as
        reflection, where this file cannot see it."""
        if node.type == "export_specifier":
            name = node.child_by_field_name("name")
            return name is not None and self._reflection_value(name) is not None
        if node.type not in ("member_expression", "subscript_expression") or self._reflection_value(node) is None:
            return False
        outer = _outer(node)
        declarator = outer.parent
        if declarator is None or declarator.type != "variable_declarator" or not _is_field(declarator, "value", outer):
            return False
        declaration = declarator.parent
        return declaration is not None and declaration.parent is not None and declaration.parent.type == "export_statement"

    @property
    def _names_class_cached(self) -> bool:
        if getattr(self, "_nc", None) is None:
            self._nc = self._names_class()
        return self._nc

    def _fixed_key_read(self, node: Node) -> bool:
        """A subscript whose key the file fixes to ``constructor``, ``prototype``
        or ``__proto__`` (``featureX["constructor"]``, ``const k = "constructor";
        x[k]``), and ``Reflect.get(x, "constructor")``: judged as the member read,
        in a file that need not spell the class."""
        if node.type == "subscript_expression":
            keys = self._index_keys(node.child_by_field_name("index"))
            return bool(keys and keys & _CLASS_MEMBERS_DANGEROUS) and bool(
                self._dangerous_member(node, self._names_class_cached)
            )
        if node.type == "call_expression":
            return self._reflective_read(node, self._names_class_cached)
        return False

    def _function_constructor(self, node: Node) -> bool:
        """``Function(s)`` and ``new Function(s)``: code from a string, in the global scope."""
        if node.type == "call_expression":
            function = _unwrap(node.child_by_field_name("function"))
        elif node.type == "new_expression":
            function = _unwrap(node.child_by_field_name("constructor"))
        else:
            return False
        return (
            function is not None
            and function.type == "identifier"
            and function.text == b"Function"
            and "Function" not in self.binders
        )

    def _evals(self, node: Node) -> bool:
        """``eval`` in any spelling that can reach the module's bindings or the
        global one: called, ``(0, eval)(s)``, ``window.eval``, ``const e = eval``,
        ``Reflect.apply(eval, …)``. Any use of the global name counts, called or not."""
        if node.type != "identifier" or node.text != b"eval" or "eval" in self.binders or node.id in self.binder_ids:
            return False
        parent = node.parent
        if parent is not None and parent.type == "member_expression" and _is_field(parent, "property", node):
            return False  # `obj.eval`: handled at the member
        return True

    def _runs_code(self, node: Node) -> bool:
        """Code assembled from a string for a loader other than ``eval`` and
        ``Function``: the ``vm`` module (``runInThisContext`` runs in this
        realm and can patch any prototype in it) and a timer given a string
        (``setTimeout("…")`` evaluates it as global code); and ``eval`` reached
        as a member of the global object (``window.eval``). The first two are
        asked of the files an instance can reach."""
        if node.type == "subscript_expression":  # `globalThis["eval"]`
            obj = _unwrap(node.child_by_field_name("object"))
            return (
                _string_value(node.child_by_field_name("index")) == "eval"
                and obj is not None
                and obj.type == "identifier"
                and obj.text.decode("utf-8") in _GLOBAL_OBJECTS
                and obj.text.decode("utf-8") not in self.binders
            )
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            obj = _unwrap(node.child_by_field_name("object"))
            return (
                prop is not None
                and prop.text == b"eval"
                and obj is not None
                and obj.type == "identifier"
                and obj.text.decode("utf-8") in _GLOBAL_OBJECTS
                and obj.text.decode("utf-8") not in self.binders
            )
        if node.type == "string" and node.parent is not None:
            value = _string_value(node)
            if value in ("vm", "node:vm") and self.reachable:
                parent = node.parent
                if parent.type == "import_statement" or (
                    parent.type == "arguments"
                    and parent.parent is not None
                    and (parent.parent.child_by_field_name("function") or parent).text in (b"require", b"import")
                ):
                    return True
            return False
        if node.type != "call_expression":
            return False
        function = _unwrap(node.child_by_field_name("function"))
        if function is None:
            return False
        if function.type == "member_expression":
            obj = _unwrap(function.child_by_field_name("object"))
            prop = function.child_by_field_name("property")
            if obj is None or prop is None or obj.type != "identifier" or obj.text.decode("utf-8") not in _GLOBAL_OBJECTS:
                return False
            name = prop.text.decode("utf-8")
        elif function.type == "identifier":
            name = function.text.decode("utf-8")
        else:
            return False
        if name not in ("setTimeout", "setInterval") or (function.type == "identifier" and name in self.binders):
            return False
        arguments = node.child_by_field_name("arguments")
        first = next((c for c in arguments.named_children if c.type != "comment"), None) if arguments is not None else None
        return self._stringish(first) and self.reachable

    def _stringish(self, expr: Node | None, depth: int = 0) -> bool:
        expr = _unwrap(expr)
        if expr is None or depth > 6:
            return False
        if expr.type in ("string", "template_string"):
            return True
        if expr.type == "binary_expression":
            return self._stringish(expr.child_by_field_name("left"), depth + 1) or self._stringish(
                expr.child_by_field_name("right"), depth + 1
            )
        if expr.type == "identifier":
            kinds = self.binders.get(expr.text.decode("utf-8"), [])
            return any(
                kind == "const" and self._stringish(binder.parent.child_by_field_name("value"), depth + 1)
                for kind, binder in kinds
            )
        return False

    def _module_source(self, call: Node) -> str | None:
        """``"./x"`` for ``require("./x")`` / ``import("./x")`` (a plain string or
        template), else None."""
        callee = _unwrap(call.child_by_field_name("function"))
        arguments = call.child_by_field_name("arguments")
        if callee is None or arguments is None or callee.text not in (b"require", b"import"):
            return None
        first = next((c for c in arguments.named_children if c.type != "comment"), None)
        if first is None:
            return None
        if first.type == "template_string" and not any(c.type == "template_substitution" for c in first.named_children):
            return first.text.decode("utf-8").strip("`")
        return _string_value(first)

    def _namespaces(self) -> dict[str, str]:
        """Local names bound to a whole module: ``import * as ns from "x"``,
        ``import ns = require("x")``, ``const ns = require("x")``, ``const ns =
        await import("x")``, the parameter of ``import("x").then(ns => …)``,
        and a named import of a name that some file defines as ``export * as
        ns from "…"``."""
        if getattr(self, "_namespace_cache", None) is not None:
            return self._namespace_cache
        found: dict[str, str] = {}
        exported = self.context.namespace_exports if self.context is not None else frozenset()
        for node in _preorder(self.root):
            if node.type == "namespace_import":
                local = next((c for c in node.named_children if c.type == "identifier"), None)
                statement = node.parent
                while statement is not None and statement.type != "import_statement":
                    statement = statement.parent
                source = _string_value(statement.child_by_field_name("source")) if statement is not None else None
                if local is not None and source is not None:
                    found[local.text.decode("utf-8")] = source
            elif node.type == "import_require_clause":
                local = next((c for c in node.named_children if c.type == "identifier"), None)
                source = _string_value(next((c for c in node.named_children if c.type == "string"), None))
                if local is not None and source is not None:
                    found[local.text.decode("utf-8")] = source
            elif node.type == "import_specifier" and exported:
                name, alias = node.child_by_field_name("name"), node.child_by_field_name("alias")
                if name is not None and name.text.decode("utf-8") in exported:
                    statement = node.parent
                    while statement is not None and statement.type != "import_statement":
                        statement = statement.parent
                    source = _string_value(statement.child_by_field_name("source")) if statement is not None else None
                    if source is not None:
                        found[(alias or name).text.decode("utf-8")] = source
            elif node.type == "variable_declarator":
                name = node.child_by_field_name("name")
                value = _unwrap(node.child_by_field_name("value"))
                while value is not None and value.type == "await_expression":
                    value = _unwrap(next(iter(value.named_children), None))
                if name is None or name.type != "identifier" or value is None or value.type != "call_expression":
                    continue
                source = self._module_source(value)
                if source is not None:
                    found[name.text.decode("utf-8")] = source
            elif node.type == "call_expression":
                # `import("x").then((ns) => …)`
                source = self._module_source(node)
                parent = node.parent
                while source is not None and parent is not None and parent.type in ("await_expression", "parenthesized_expression"):
                    parent = parent.parent
                if source is None or parent is None or parent.type != "member_expression":
                    continue
                prop = parent.child_by_field_name("property")
                caller = parent.parent
                if prop is None or prop.text != b"then" or caller is None or caller.type != "call_expression":
                    continue
                arguments = caller.child_by_field_name("arguments")
                callback = next((c for c in arguments.named_children), None) if arguments is not None else None
                params = callback.child_by_field_name("parameters") if callback is not None else None
                first = None
                if params is not None:
                    first = next((c for c in _preorder(params) if c.type in ("identifier",)), None)
                elif callback is not None:
                    first = callback.child_by_field_name("parameter")
                if first is not None and first.type == "identifier":
                    found[first.text.decode("utf-8")] = source
        self._namespace_cache = found
        return found

    def _holds(self, source: str) -> bool:
        if self.context is None:
            return _Context.looks_local(source) and not _Context.is_data(source)
        return self.context.module_holds_class(self.path, source)

    def _enumerates_module(self, node: Node) -> bool:
        """A module object used as a value (``Object.values(ns)``, ``for (k in
        ns)``, ``{ ...ns }``, ``ns[k]``, ``export { ns }``, or the call
        ``require("x")`` / ``import("x")`` itself, handed on) when the module
        can hold the class: its members are the module's exports, and the class
        may be one. A target the checkout cannot resolve but that looks like a
        file of it is taken to hold the class."""
        if node.type == "call_expression":
            source = self._module_source(node)
            if source is None:
                return False
            parent = node.parent
            while parent is not None and parent.type in ("await_expression", "parenthesized_expression", "as_expression", "non_null_expression"):
                node, parent = parent, parent.parent
            if parent is None:
                return False
            if parent.type == "variable_declarator" and _is_field(parent, "value", node):
                return False  # a binding: judged by its uses
            if parent.type == "member_expression" and _is_field(parent, "object", node):
                return False  # `import("x").then(ns => …)`: the parameter is judged (`_namespaces`)
            if parent.type == "expression_statement":
                return False  # `require("./x");` for its effects
            return self._holds(source)
        if node.type == "shorthand_property_identifier":  # `{ ns }`: the module goes into an object
            source = self._namespaces().get(node.text.decode("utf-8"))
            return source is not None and self._holds(source)
        if node.type != "identifier":
            return False
        name = node.text.decode("utf-8")
        source = self._namespaces().get(name)
        if source is None:
            return False
        if node.id in self.binder_ids:
            # `export const ns = await import("x")`: importers get the module whole.
            declarator = node.parent
            declaration = declarator.parent if declarator is not None else None
            return (
                declarator is not None
                and declarator.type == "variable_declarator"
                and _is_field(declarator, "name", node)
                and declaration is not None
                and declaration.parent is not None
                and declaration.parent.type == "export_statement"
                and self._holds(source)
            )
        outer = _outer(node)  # `ns as unknown as T` is `ns`
        parent = outer.parent
        if parent is None:
            return False
        if parent.type in (
            "namespace_import", "import_specifier", "nested_identifier",
            "nested_type_identifier", "import_alias", "type_query", "import_require_clause",
        ):
            return False  # a name, a type or a qualified alias; the class's own name is judged on its own
        if parent.type == "export_specifier":
            return self._holds(source)  # the module object is handed to every importer
        if parent.type == "member_expression" and _is_field(parent, "property", outer):
            return False
        if parent.type == "variable_declarator" and _is_field(parent, "name", outer):
            return False
        if parent.type == "variable_declarator" and _is_field(parent, "value", outer):
            pattern = parent.child_by_field_name("name")
            if pattern is not None and pattern.type == "object_pattern":
                # `const { a, b } = ns` reads named members, as `ns.a` does; a
                # rest element or a computed key takes the module whole.
                whole = any(
                    c.type == "rest_pattern"
                    or (c.type == "pair_pattern" and (c.child_by_field_name("key") or c).type == "computed_property_name")
                    for c in pattern.named_children
                )
                return whole and self._holds(source)
        if parent.type == "member_expression" and _is_field(parent, "object", outer):
            return False  # `ns.x`: a named member; the class's own name is judged on its own
        if parent.type == "subscript_expression" and _is_field(parent, "object", outer):
            keys = self._index_keys(parent.child_by_field_name("index"))
            if keys is not None:
                return False
        return self._holds(source)

    def _globs_modules(self, node: Node) -> bool:
        """``import.meta.glob(patterns)`` and ``require.context(dir)`` load every
        module a pattern matches: one that could be a module holding the class
        is a way to it."""
        if node.type != "call_expression":
            return False
        function = _unwrap(node.child_by_field_name("function"))
        if function is None or function.type != "member_expression":
            return False
        obj, prop = _unwrap(function.child_by_field_name("object")), function.child_by_field_name("property")
        if obj is None or prop is None:
            return False
        glob = obj.type == "meta_property" and prop.text == b"glob"
        context = obj.type == "identifier" and obj.text == b"require" and prop.text == b"context"
        if not (glob or context):
            return False
        arguments = node.child_by_field_name("arguments")
        first = next((c for c in arguments.named_children if c.type != "comment"), None) if arguments is not None else None
        patterns: list[str] | None = None
        if first is not None:
            value = _string_value(first)
            if value is not None:
                patterns = [value + "/**"] if context else [value]
            elif first.type == "array":
                items = [_string_value(c) for c in first.named_children if c.type != "comment"]
                if all(i is not None for i in items):
                    patterns = [i for i in items if i is not None]
        if self.context is None:
            return True
        return self.context.glob_may_match(self.path, patterns)

    def _reflective_read(self, call: Node, names_class: bool) -> bool:
        """``Reflect.get(x, "constructor")``, ``Reflect.get(Object, "prototype")``,
        ``Object.getOwnPropertyDescriptor(Object, "prototype")``: a read of the
        class or the prototype spelled as a call, whose result is not followed.
        A key the file does not fix is not asked (named residual)."""
        function = _unwrap(call.child_by_field_name("function"))
        if function is None or function.type not in ("member_expression", "subscript_expression"):
            return False
        if function.type == "member_expression":
            prop = function.child_by_field_name("property")
            method = prop.text.decode("utf-8") if prop is not None else ""
        else:
            method = _string_value(function.child_by_field_name("index")) or ""
        owner = self._global_named(function.child_by_field_name("object"))
        if (owner, method) not in (
            ("Reflect", "get"),
            ("Reflect", "getOwnPropertyDescriptor"),
            ("Object", "getOwnPropertyDescriptor"),
        ):
            return False
        arguments = call.child_by_field_name("arguments")
        listed = [c for c in arguments.named_children if c.type != "comment"] if arguments is not None else []
        if len(listed) < 2:
            return False
        keys = self._index_keys(listed[1])
        if keys is None:
            return False
        first = listed[0]
        if "prototype" in keys:
            return self._global_named(first) is not None or self._maybe_class(first, names_class=names_class)
        if keys & {"constructor", "__proto__"}:
            return _unwrap(first) is None or _unwrap(first).type not in _FRESH
        return False

    def _exports_reflection(self, node: Node) -> bool:
        """``export const { defineProperty } = Object`` (and the pair form):
        importers use the name as reflection, where this file cannot see it."""
        if node.type not in ("shorthand_property_identifier_pattern", "pair_pattern"):
            return False
        pattern = node.parent
        if pattern is None or pattern.type != "object_pattern":
            return False
        declarator = pattern.parent
        declaration = declarator.parent if declarator is not None else None
        if (
            declarator is None
            or declarator.type != "variable_declarator"
            or declaration is None
            or declaration.parent is None
            or declaration.parent.type != "export_statement"
        ):
            return False
        key = node.text.decode("utf-8") if node.type == "shorthand_property_identifier_pattern" else _plain_key(node.child_by_field_name("key"))
        owner = self._global_named(declarator.child_by_field_name("value"))
        return (owner, key) in _REFLECTION if owner is not None else key in _UNAMBIGUOUS

    # --- reflection, however it is spelled ---------------------------------

    def _global_named(self, expr: Node | None, depth: int = 0) -> str | None:
        """``"Object"`` or ``"Reflect"`` when ``expr`` is that global:
        ``Object``, ``globalThis.Object``, or a ``const`` bound to one."""
        expr = _unwrap(expr)
        if expr is None or depth > 8:
            return None
        if expr.type == "identifier":
            name = expr.text.decode("utf-8")
            kinds = self.binders.get(name)
            if not kinds:
                return name if name in _REFLECTING_GLOBALS else None
            if len(kinds) == 1 and kinds[0][0] == "const":
                value = kinds[0][1].parent.child_by_field_name("value")
                return self._global_named(value, depth + 1)
            return None
        if expr.type == "member_expression":
            obj = _unwrap(expr.child_by_field_name("object"))
            prop = expr.child_by_field_name("property")
            text = prop.text.decode("utf-8") if prop is not None else ""
            if (
                text in _REFLECTING_GLOBALS
                and obj is not None
                and obj.type == "identifier"
                and obj.text.decode("utf-8") in _GLOBAL_OBJECTS
                and obj.text.decode("utf-8") not in self.binders
            ):
                return text
        return None

    def _reflection_value(self, expr: Node | None, depth: int = 0) -> tuple[str | None, str] | None:
        """``(owner, method)`` when ``expr`` is one of the reflection functions
        this module judges, however it is reached: ``Object.defineProperty``,
        ``const d = Object.defineProperty``, ``const { defineProperty } =
        Object``. An unresolved name spelled like an unambiguous one
        (``defineProperty``, ``getPrototypeOf``) counts as it, owner None."""
        expr = _unwrap(expr)
        if expr is None or depth > 8:
            return None
        if expr.type in ("member_expression", "subscript_expression"):
            if expr.type == "member_expression":
                prop = expr.child_by_field_name("property")
                method = prop.text.decode("utf-8") if prop is not None else ""
            else:  # `Object["defineProperty"]`
                method = _string_value(expr.child_by_field_name("index")) or ""
            owner = self._global_named(expr.child_by_field_name("object"))
            if owner is not None and (owner, method) in _REFLECTION:
                return owner, method
            return None
        if expr.type != "identifier":
            return None
        name = expr.text.decode("utf-8")
        kinds = self.binders.get(name)
        if not kinds:
            return None
        for kind, binder in kinds:
            if kind in ("import", "declared", "class"):
                continue
            if kind == "const":
                found = self._reflection_value(binder.parent.child_by_field_name("value"), depth + 1)
                if found is not None:
                    return found
                continue
            source = _destructured_from(binder)
            if source is not None:
                key, value = source
                owner = self._global_named(value)
                if owner is not None and (owner, key) in _REFLECTION:
                    return owner, key
                if owner is None and key in _UNAMBIGUOUS:
                    return None, key
                continue
            if name in _UNAMBIGUOUS:
                return None, name
        return None

    def _reflection_call(self, call: Node) -> tuple[tuple[str | None, str], Node | None] | None:
        """``((owner, method), first argument)`` for a call of a reflection
        function: directly, through an alias, or through ``.call`` (whose
        first argument is the second) and ``.apply`` (unknown: None)."""
        function = _unwrap(call.child_by_field_name("function"))
        arguments = call.child_by_field_name("arguments")
        listed = [c for c in arguments.named_children if c.type != "comment"] if arguments is not None else []
        found = self._reflection_value(function)
        if found is not None:
            return found, listed[0] if listed else None
        if function is not None and function.type == "member_expression":
            prop = function.child_by_field_name("property")
            via = prop.text.decode("utf-8") if prop is not None else ""
            if via in ("call", "apply"):
                found = self._reflection_value(function.child_by_field_name("object"))
                if found is not None:
                    first = listed[1] if via == "call" and len(listed) > 1 else None
                    return found, first
        return None

    def _called(self, node: Node) -> bool:
        """Whether a reflection function ``node`` evaluates to is only called
        here (directly or through ``.call``/``.apply``), or stored in a name
        whose uses are judged on their own (``const d = Object.defineProperty``)."""
        outer = _outer(node)
        parent = outer.parent
        if parent is None:
            return False
        if parent.type == "call_expression" and _is_field(parent, "function", outer):
            return True
        if parent.type == "member_expression" and _is_field(parent, "object", outer):
            prop = parent.child_by_field_name("property")
            if prop is not None and prop.text in (b"call", b"apply"):
                member = _outer(parent)
                caller = member.parent
                return caller is not None and caller.type == "call_expression" and _is_field(caller, "function", member)
            return False
        if parent.type == "variable_declarator" and _is_field(parent, "value", outer):
            name = parent.child_by_field_name("name")
            declaration = parent.parent
            return (
                name is not None
                and name.type == "identifier"
                and declaration is not None
                and any(c.type == "const" for c in declaration.children)
                # An exported alias is used by importers this file cannot see.
                and not (declaration.parent is not None and declaration.parent.type == "export_statement")
            )
        return False

    def _member_key(self, node: Node) -> str | None:
        """The member a member or subscript expression names, or None for a
        computed subscript the file does not settle (``x[k]``).

        A subscript is settled when its index can only be a number, or a string
        the file fixes: a literal, a ``const`` bound to one, or a name typed as
        a union of string literals. A key set holding ``prototype``,
        ``__proto__``, ``constructor`` or a definer (``__defineGetter__``) answers
        as that member; a single other key answers as itself (so a read of
        ``o["__defineSetter__"]`` is judged as the call it is); several, or
        numbers only, answer ``""``, a member that reaches nothing."""
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            return prop.text.decode("utf-8") if prop is not None else None
        index = node.child_by_field_name("index")
        keys = self._index_keys(index)
        if keys is None:
            return None
        for danger in ("prototype", "__proto__", "constructor", *sorted(_DEFINERS)):
            if danger in keys:
                return danger
        return next(iter(keys)) if len(keys) == 1 else ""

    def _index_keys(self, index: Node | None, depth: int = 0) -> frozenset[str] | None:
        """Every string ``index`` can be (empty for a number), or None."""
        index = _unwrap(index)
        if index is None or depth > 6:
            return None
        if index.type == "number":
            return frozenset()
        value = _string_value(index)
        if value is not None:
            return frozenset({value})
        if index.type == "template_string":
            # `constructor` written `${a}` with a fixed `a`, or in pieces.
            pieces: list[frozenset[str]] = []
            for child in index.named_children:
                if child.type == "template_substitution":
                    inner = next((c for c in child.named_children if c.type != "comment"), None)
                    pieces.append(self._index_keys(inner, depth + 1) if inner is not None else None)
                elif child.type in ("string_fragment", "escape_sequence"):
                    pieces.append(frozenset({child.text.decode("utf-8")}))
                if pieces and pieces[-1] is None:
                    return None
            if all(p is not None and len(p) == 1 for p in pieces):
                return frozenset({"".join(next(iter(p)) for p in pieces)})
            return None
        if index.type == "binary_expression":
            operator = index.child_by_field_name("operator")
            left = self._index_keys(index.child_by_field_name("left"), depth + 1)
            right = self._index_keys(index.child_by_field_name("right"), depth + 1)
            if operator is None or left is None or right is None:
                return None
            if operator.type == "+" and len(left) == 1 and len(right) == 1:
                return frozenset({next(iter(left)) + next(iter(right))})  # "con" + "structor"
            if operator.type in ("-", "*", "/", "%", "**", "+") and left == frozenset() and right == frozenset():
                return frozenset()  # arithmetic on numbers
            return None
        if index.type != "identifier":
            return None
        name = index.text.decode("utf-8")
        binders = self.binders.get(name)
        if not binders or len(binders) != 1:
            return None
        kind, binder = binders[0]
        keys = self._annotated_keys(binder)
        if keys is not None:
            return keys
        parent = binder.parent
        if kind == "const" and parent is not None and parent.type == "variable_declarator":
            return self._index_keys(parent.child_by_field_name("value"), depth + 1)
        if (
            parent is not None
            and parent.type == "variable_declarator"
            and _is_field(parent, "name", binder)
            and _unwrap(parent.child_by_field_name("value")) is not None
            and _unwrap(parent.child_by_field_name("value")).type == "number"
            and not self._assigned_other_than_updates(name)
        ):
            return frozenset()  # `for (let i = 0; …; i++)`: only ever a number
        return None

    def _assigned_other_than_updates(self, name: str) -> bool:
        for node in _preorder(self.root):
            if node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "identifier" and left.text.decode("utf-8") == name:
                    return True
            elif node.type == "augmented_assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "identifier" and left.text.decode("utf-8") == name:
                    operator = node.child_by_field_name("operator")
                    if operator is None or operator.type not in ("+=", "-=", "*=", "/=", "%=", "**="):
                        return True
                    right = node.child_by_field_name("right")
                    if self._index_keys(right) != frozenset():
                        return True
        return False

    @staticmethod
    def _annotated_keys(binder: Node) -> frozenset[str] | None:
        """The keys a name typed ``number`` or as a union of string literals can
        hold (``k: "a" | "b"``), or None."""
        parent = binder.parent
        if parent is None:
            return None
        annotation = next((c for c in parent.children if c.type == "type_annotation"), None)
        if annotation is None:
            return None
        found: set[str] = set()

        def collect(node: Node) -> bool:
            if node.type == "predefined_type":
                return node.text == b"number"
            if node.type == "literal_type":
                inner = node.named_children[0] if node.named_children else None
                if inner is not None and inner.type == "number":
                    return True
                value = _string_value(inner)
                if value is None:
                    return False
                found.add(value)
                return True
            if node.type in ("union_type", "parenthesized_type"):
                parts = [c for c in node.named_children if c.type != "comment"]
                return bool(parts) and all(collect(c) for c in parts)
            return False

        inner = [c for c in annotation.named_children if c.type != "comment"]
        if len(inner) != 1 or not collect(inner[0]):
            return None
        return frozenset(found)

    def _inspected(self, node: Node, seen: set[str] | None = None) -> bool:
        """Whether a prototype ``node`` evaluates to is only looked at:
        compared, tested, a member read off it (``proto.hasOwnProperty(k)``,
        ``Object.prototype.toString.call(v)``), or handed to a function that
        only reads (``Object.getOwnPropertyNames``). A name it is stored in
        counts every use of that name in the file. Anything else -- a write, a
        call of ``__defineGetter__``/``__defineSetter__``, ``constructor``, an
        argument of any other call, a return -- may change it."""
        seen = set() if seen is None else seen
        outer = _outer(node)
        parent = outer.parent
        if parent is None:
            return False
        kind = parent.type
        if kind == "binary_expression":
            operator = parent.child_by_field_name("operator")
            op = operator.type if operator is not None else ""
            if op in _COMPARISONS or op in ("instanceof", "in"):
                return True
            if op in ("&&", "||", "??"):
                return self._inspected(parent, seen)
            return False
        if kind == "unary_expression":
            operator = parent.child_by_field_name("operator")
            return operator is not None and operator.type in ("!", "typeof", "void")
        if kind in ("if_statement", "while_statement", "do_statement", "for_statement"):
            return _is_field(parent, "condition", outer)
        if kind == "new_expression":
            return _is_field(parent, "constructor", outer)
        if kind == "ternary_expression":
            if _is_field(parent, "condition", outer):
                return True
            return self._inspected(parent, seen)
        if kind in ("member_expression", "subscript_expression") and _is_field(parent, "object", outer):
            if _written(parent):
                return False
            key = self._member_key(parent)
            if key is None:
                return False
            if key in _PROTOTYPE_KEYS or key == "constructor":
                return self._inspected(parent, seen)
            member = _outer(parent)
            caller = member.parent
            if caller is not None and caller.type == "call_expression" and _is_field(caller, "function", member):
                return key not in _DEFINERS
            return True
        if kind == "arguments":
            call = parent.parent
            function = _unwrap(call.child_by_field_name("function")) if call is not None else None
            if function is not None and function.type == "identifier" and self._reflection_value(function) is None:
                return function.text.decode("utf-8") in _INSPECTING_FUNCTIONS
            resolved = self._reflection_call(call) if call is not None else None
            if resolved is None or resolved[1] is None or resolved[1].id != outer.id:
                if function is None or function.type != "member_expression":
                    return False
                obj = self._global_named(function.child_by_field_name("object"))
                prop = function.child_by_field_name("property")
                return (obj, prop.text.decode("utf-8") if prop is not None else "") in _INSPECTORS
            if resolved[0][1] == "getPrototypeOf":
                return self._inspected(call, seen)
            return resolved[0] in _INSPECTORS
        if kind == "variable_declarator" and _is_field(parent, "value", outer):
            name = parent.child_by_field_name("name")
            if name is not None and name.type == "object_pattern":
                return self._pattern_reads(name)
            return name is not None and name.type == "identifier" and self._uses_inspected(name, seen)
        if kind == "assignment_expression" and _is_field(parent, "right", outer):
            left = parent.child_by_field_name("left")
            return left is not None and left.type == "identifier" and self._uses_inspected(left, seen)
        return False

    @staticmethod
    def _pattern_reads(pattern: Node) -> bool:
        """Whether ``const { name } = x.constructor`` only reads members: no
        rest, and none that is the prototype or the class again."""
        for child in pattern.named_children:
            if child.type == "comment":
                continue
            if child.type == "shorthand_property_identifier_pattern":
                key = child.text.decode("utf-8")
            elif child.type in ("pair_pattern", "object_assignment_pattern"):
                first = child.child_by_field_name("key") or child.child_by_field_name("left")
                if first is None or first.type not in (
                    "property_identifier",
                    "shorthand_property_identifier_pattern",
                ):
                    return False
                key = first.text.decode("utf-8")
            else:
                return False
            if key in _CLASS_MEMBERS:
                return False
        return True

    def _uses_inspected(self, name_node: Node, seen: set[str]) -> bool:
        """Whether every use of the name ``name_node`` binds is only looked
        at (:meth:`_inspected`); by name, so a shadowing name counts too."""
        name = name_node.text.decode("utf-8")
        if name in seen:
            return True
        seen.add(name)
        for node in _preorder(self.root):
            if node.type == "shorthand_property_identifier" and node.text.decode("utf-8") == name:
                return False  # `patch({ p })` hands it on
            if node.type != "identifier" or node.text.decode("utf-8") != name or node.id in self.binder_ids:
                continue
            parent = node.parent
            if parent is not None and parent.type == "assignment_expression" and _is_field(parent, "left", node):
                continue  # a new value; an obtainer there is judged on its own
            if parent is not None and parent.type == "member_expression" and _is_field(parent, "property", node):
                continue
            if not self._inspected(node, seen):
                return False
        return True

    def _harmless_use(self, node: Node) -> bool:
        """Whether a use of the class's value leaves its prototype alone."""
        if node.type == "shorthand_property_identifier":
            return False  # `{ Feature }` hands it on
        outer = _outer(node)
        parent = outer.parent
        if parent is None:
            return False
        kind = parent.type
        if kind == "new_expression":
            return _is_field(parent, "constructor", outer)
        if kind in ("member_expression", "subscript_expression") and _is_field(parent, "object", outer):
            if kind == "subscript_expression" and _string_value(parent.child_by_field_name("index")) is None:
                return False  # `Feature[key]` may be its prototype
            key = self._member_key(parent)
            return key not in _PROTOTYPE_KEYS and not _written(parent)
        if kind == "binary_expression":
            operator = parent.child_by_field_name("operator")
            if operator is None:
                return False
            if operator.type == "instanceof":
                return _is_field(parent, "right", outer)
            return operator.type in _COMPARISONS
        if kind == "unary_expression":
            operator = parent.child_by_field_name("operator")
            return operator is not None and operator.type == "typeof"
        if kind == "extends_clause":
            return _is_field(parent, "value", outer)
        if kind in ("type_query", "export_specifier"):
            return True
        if kind == "export_statement":
            return _is_field(parent, "value", outer)  # `export default Feature`: see `aliases`
        if kind in ("jsx_opening_element", "jsx_closing_element", "jsx_self_closing_element"):
            return True  # React constructs it and reads its prototype, never writes it
        return False


def _plain_key(key: Node | None) -> str | None:
    """The name a pattern's or an object's key spells, or None if computed."""
    if key is None:
        return None
    if key.type in ("property_identifier", "shorthand_property_identifier_pattern", "number"):
        return key.text.decode("utf-8")
    return _string_value(key)


def _destructured_from(binder: Node) -> tuple[str | None, Node | None] | None:
    """``(key, value)`` for a name bound by ``const { key } = value`` or
    ``const { key: name } = value``, else None."""
    parent = binder.parent
    if binder.type == "shorthand_property_identifier_pattern":
        key: str | None = binder.text.decode("utf-8")
        pattern = parent
    elif parent is not None and parent.type == "pair_pattern" and _is_field(parent, "value", binder):
        key = _plain_key(parent.child_by_field_name("key"))
        pattern = parent.parent
    else:
        return None
    if pattern is None or pattern.type != "object_pattern":
        return None
    declarator = pattern.parent
    if declarator is None or declarator.type != "variable_declarator" or not _is_field(declarator, "name", pattern):
        return None
    return key, declarator.child_by_field_name("value")


def _preorder(node: Node):
    """Every node under ``node``, parents first, in source order, past every
    type query (``typeof Repo.prototype.save`` in a type runs nothing)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if current.type != "type_query":
            stack.extend(reversed(current.children))
