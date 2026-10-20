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

The question is asked of the files on disk at the moment the pass runs. The
run only ever removes code from them, which can make a module more inert but
never less, so a module judged inert stays inert in the tree the pull request
proposes.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
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
})

#: The manifest fields where naming a package only says which published
#: version to install, as long as the version spells a registry range.
_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")

#: The packages behind :data:`SIDE_EFFECT_FREE_PACKAGES`. They load each
#: other (`node` loads `js`, `react` loads `browser`), so a substitute for any
#: one of them reaches a load of another.
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
    target = cross_file_ts.resolve_specifier(reader, specifier, repo_dir)
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
