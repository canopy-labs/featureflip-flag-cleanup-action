"""A project's own Blade directives, and the text each one compiles to.

Blade compiles ``@money($x, 'GBP')`` by calling the closure registered with
``Blade::directive('money', ...)`` on the text between the parentheses, and
splicing what it returns into the compiled view. A directive the transform does
not know may compile to anything, an ``if (...):`` that a plain ``@endif``
closes included, so a view with one inside a conditional the run folds is left
as it is. That stays true here; what changes is that a registration this module
can READ is known:

* the name is a string literal, registered with ``Blade::directive`` (the
  facade, in any case) or ``->directive(...)`` on any receiver
  (``$blade->directive``, ``app('blade.compiler')->directive``), in a file
  outside the project's test directories;
* the handler is a closure or arrow function whose whole body returns a text
  built only from string literals and its own parameter (``"<?php echo
  money({$expression}); ?>"``, ``'<b>' . $e . '</b>'``).

Its text, with :data:`ARGUMENT` where the argument goes, is handed to the engine
as each view's input facts, and the gate (:mod:`flag_cleanup.blade_code`) checks
every use of it independently. Neither trusts the text further than "this is
what Blade splices in": a use inside a folded conditional is kept only when that
PHP, with the use's own argument, opens and closes no block.

Nothing is known when the project registers a directive or a ``Blade::if``
under a name that is not a literal: any name in a view could be that one. A
name registered twice differently, or registered with a handler this cannot
read, or touched by ``Blade::if`` (which registers ``name``, ``elsename``,
``endname`` and ``unlessname``), stays unknown. Only the project's own files
are read: a package's registrations under an ignored ``vendor/`` are not, and
a project's own registration of the same name runs after them and wins.

A few packages' directives are known instead, from the package's own source
(:data:`_PACKAGES`), when the checkout's ``composer.lock`` installs a release
inside the range that source was read at. ``composer.json`` alone, or any other
release, says nothing: a later release may compile differently. They join the
project's registrations, so a project registration of the same name that
differs, or cannot be read, makes the name unknown again.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index, syntax

#: Where Blade puts a directive's argument in the compiled text.
ARGUMENT = "\0"
#: The engine's input-fact keys (`blade_view::DIRECTIVE_FACT`, `COMPILES_TO_FACT`).
FACT = "blade_directive"
COMPILES_TO = "blade_compiles_to"

#: PHP's double-quoted escapes this reads; any other refuses the handler.
_ESCAPES = {
    "\\n": "\n", "\\t": "\t", "\\r": "\r", "\\v": "\v", "\\f": "\f", "\\e": "\x1b",
    "\\\\": "\\", "\\$": "$", '\\"': '"',
}


#: Directives packages register, read from each package's provider at every
#: release tag in the audited range (inclusive). Every release of
#: akaunting/laravel-money from 1.0.0 to 6.1.0 registers these two with
#: `return "<?php echo money($expression); ?>";` and its `currency` twin.
_PACKAGES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], dict[str, str]]] = {
    "akaunting/laravel-money": (
        (1, 0, 0),
        (6, 1, 0),
        {
            "money": f"<?php echo money({ARGUMENT}); ?>",
            "currency": f"<?php echo currency({ARGUMENT}); ?>",
        },
    ),
}
_RELEASE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class _Unknowable(Exception):
    """A registration whose name is not a literal: nothing can be known."""


def registrations(repo_dir: str) -> dict[str, str | None]:
    """Each directive name this checkout's own non-test PHP registers, with the
    text it compiles to, or ``None`` when that cannot be read. Empty when any
    registration's name is not a literal."""
    found: dict[str, set[str | None]] = {}
    for package, version in _installed_packages(repo_dir):
        audited = _PACKAGES.get(package)
        release = _RELEASE.fullmatch(version)
        if audited is None or release is None:
            continue
        low, high, directives = audited
        if low <= tuple(int(part) for part in release.groups()) <= high:
            for name, compiled in directives.items():
                found.setdefault(name, set()).add(compiled)
    memo = source_index.run_memo("blade_directives")
    root = Path(source_index.checkout_root(repo_dir))
    try:
        for source in source_index.files(repo_dir, (".php",)):
            path = source.path
            if path.name.endswith(".blade.php") or _is_test(path, root):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            folded = text.lower()
            if "directive" not in folded and "if" not in folded:
                continue
            key = str(path)
            cached = memo.get(key) if memo is not None else None
            if cached is not None and cached[0] == text:
                entries = cached[1]
            else:
                entries = tuple(_registrations_in(text))
                if memo is not None:
                    memo[key] = (text, entries)
            for name, compiled in entries:
                found.setdefault(name, set()).add(compiled)
    except _Unknowable:
        return {}
    return {
        name: next(iter(texts)) if len(texts) == 1 else None
        for name, texts in sorted(found.items())
    }


#: Directories whose PHP never runs in production; a registration there (a
#: test's own `Blade::directive`) says nothing about the views.
_TEST_DIRECTORIES = frozenset({"tests", "test", "spec"})


def _is_test(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    # PHPUnit's own suffix, case-sensitive: `Latest.php` is not a test.
    return (
        any(part.lower() in _TEST_DIRECTORIES for part in parts[:-1])
        or path.name.endswith("Test.php")
    )


def _installed_packages(repo_dir: str) -> set[tuple[str, str]]:
    """``(name, version)`` for each package the checkout's ``composer.lock``
    installs (the root's and ``repo_dir``'s), names lower-cased."""
    root = Path(source_index.checkout_root(repo_dir))
    found: set[tuple[str, str]] = set()
    for directory in {root, Path(repo_dir)}:
        try:
            data = json.loads((directory / "composer.lock").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for section in ("packages", "packages-dev"):
            for package in data.get(section) or ():
                if not isinstance(package, dict):
                    continue
                name, version = package.get("name"), package.get("version")
                if isinstance(name, str) and isinstance(version, str):
                    found.add((name.lower(), version))
    return found


def facts(
    paths: list[str], directives: dict[str, str | None]
) -> dict[str, list[dict[str, str]]]:
    """The engine's input facts for each Blade view in ``paths``: a name alone
    for a directive registered in a way that could not be read."""
    if not directives:
        return {}
    described = [
        {FACT: name} if compiled is None else {FACT: name, COMPILES_TO: compiled}
        for name, compiled in directives.items()
    ]
    return {path: list(described) for path in paths if path.endswith(".blade.php")}


#: The facade every Laravel service provider imports.
_FACADE = "illuminate\\support\\facades\\blade"
def _registrations_in(text: str) -> Iterator[tuple[str, str | None]]:
    """``(name, compiled text or None)`` for each registration in ``text``.

    Only a DIRECT call is a recognised registration: ``directive``/``if``
    named as a method (in any case, PHP names are case-blind) on the Blade
    facade or on any receiver. Everything else that could register one fails
    closed, raising :class:`_Unknowable`: a registration whose name is not a
    literal, a string ``'directive'`` or ``'if'`` anywhere (a callable array, a
    method name held in a variable, ``Closure::fromCallable``,
    ``call_user_func``), and a dynamic method name on the facade or on a
    receiver that names Blade (``Blade::$m(...)``, ``Blade::{'x'}(...)``). A
    direct call on a class this cannot resolve to the facade makes its name
    unknown."""
    data = text.encode("utf-8")
    tree = syntax._parser("php").parse(data)
    uses, namespaced = _imports(tree.root_node)
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type in ("string", "encapsed_string", "heredoc", "nowdoc"):
            if _VERB_STRING.fullmatch(_string_body(node)):
                raise _Unknowable
            continue
        if node.type not in (
            "scoped_call_expression", "member_call_expression", "nullsafe_member_call_expression"
        ):
            continue
        method = node.child_by_field_name("name")
        scoped = node.type == "scoped_call_expression"
        if method is None or method.type != "name":
            receiver = node.child_by_field_name("scope" if scoped else "object")
            if receiver is None or "blade" in _text(receiver).lower() or (
                scoped and _is_blade(receiver, uses, namespaced)
            ):
                raise _Unknowable
            continue
        verb = _text(method).lower()
        if verb not in ("directive", "if"):
            continue
        arguments = _arguments(node)
        name = _literal(arguments[0]) if arguments else None
        if name is None:
            raise _Unknowable
        blade = not scoped or _is_blade(node.child_by_field_name("scope"), uses, namespaced)
        if verb == "if" or not blade:
            yield from _names_of(verb, name)
            continue
        yield name, _compiled(arguments[1]) if len(arguments) == 2 else None


#: A string naming the registering method, as a callable or a method name:
#: `'directive'`, `'IF'`, `'Blade::directive'`, `'\\Illuminate\\…\\Blade::if'`.
_VERB_STRING = re.compile(r"\s*(?:[\w\\]*::)?(?:directive|if)\s*", re.IGNORECASE)


def _string_body(node: Node) -> str:
    """A string literal's text between its delimiters, escapes left as written."""
    raw = _text(node)
    if node.type in ("string", "encapsed_string") and len(raw) >= 2:
        return raw[1:-1]
    if node.type in ("heredoc", "nowdoc"):
        lines = raw.split("\n")
        return "\n".join(lines[1:-1])
    return raw


def _names_of(verb: str, name: str) -> Iterator[tuple[str, None]]:
    """The names a registration this cannot read makes unknown: a
    ``Blade::if`` registers four."""
    if verb == "if":
        for spelled in (name, f"else{name}", f"end{name}", f"unless{name}"):
            yield spelled, None
    else:
        yield name, None


def _imports(root: Node) -> tuple[dict[str, str], bool]:
    """The file's class imports (alias -> lower-cased name) and whether it
    declares a namespace."""
    uses: dict[str, str] = {}
    namespaced = False
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "namespace_definition":
            namespaced = True
        if node.type == "namespace_use_clause":
            names = [c for c in node.named_children if c.type in ("qualified_name", "name")]
            if names:
                full = _text(names[0]).lstrip("\\").lower()
                alias = _text(names[-1]).lower() if len(names) > 1 else full.rsplit("\\", 1)[-1]
                uses[alias] = full
            continue
        stack.extend(node.children)
    return uses, namespaced


def _is_blade(scope: Node | None, uses: dict[str, str], namespaced: bool) -> bool:
    """Whether the class ``scope`` names is the Blade facade: imported as
    itself, spelled in full from the root, or the root alias ``Blade`` in a
    file with no namespace."""
    if scope is None:
        return False
    spelled = _text(scope).lower()
    if scope.type == "name":
        if spelled in uses:
            return uses[spelled] == _FACADE
        return spelled == "blade" and not namespaced
    if scope.type == "qualified_name":
        if spelled.startswith("\\"):
            return spelled[1:] in (_FACADE, "blade")
        return spelled == _FACADE and not namespaced
    return False


def _arguments(call: Node) -> list[Node]:
    """The call's argument expressions; a spread or named argument makes the
    list unreadable, and then none is returned."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    out = []
    for argument in args.named_children:
        if argument.type == "comment":
            continue
        if argument.type != "argument" or argument.child_by_field_name("name") is not None:
            return []
        values = [c for c in argument.named_children if c.type != "comment"]
        if len(values) != 1 or any(c.type == "..." for c in argument.children):
            return []
        out.append(values[0])
    return out


def _compiled(handler: Node) -> str | None:
    """The text a closure handler returns, :data:`ARGUMENT` for its parameter;
    ``None`` for any handler this cannot read."""
    params = handler.child_by_field_name("parameters")
    if handler.type not in ("anonymous_function", "arrow_function") or params is None:
        return None
    declared = [c for c in params.named_children if c.type != "comment"]
    if len(declared) > 1 or any(c.type != "simple_parameter" for c in declared):
        return None
    param = None
    if declared:
        variable = declared[0].child_by_field_name("name")
        if variable is None or any(c.type in ("&", "...") for c in declared[0].children):
            return None
        param = _text(variable)
    body = handler.child_by_field_name("body")
    if body is None:
        return None
    if handler.type == "anonymous_function":
        statements = [c for c in body.named_children if c.type != "comment"]
        if len(statements) != 1 or statements[0].type != "return_statement":
            return None
        values = [c for c in statements[0].named_children if c.type != "comment"]
        if len(values) != 1:
            return None
        body = values[0]
    return _template(body, param)


def _template(node: Node, param: str | None) -> str | None:
    if node.type == "parenthesized_expression":
        inner = [c for c in node.named_children if c.type != "comment"]
        return _template(inner[0], param) if len(inner) == 1 else None
    if node.type == "variable_name":
        return ARGUMENT if param is not None and _text(node) == param else None
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if operator is None or _text(operator) != "." or left is None or right is None:
            return None
        a, b = _template(left, param), _template(right, param)
        return None if a is None or b is None else a + b
    if node.type in ("string", "encapsed_string"):
        return _string(node, param)
    return None


def _literal(node: Node) -> str | None:
    """A string literal's value with no interpolation, else ``None``."""
    if node.type not in ("string", "encapsed_string"):
        return None
    value = _string(node, None)
    return value if value is not None and ARGUMENT not in value else None


def _string(node: Node, param: str | None) -> str | None:
    raw = _text(node)
    if node.type == "string":
        # Single-quoted: only `\\` and `\'` are escapes.
        if not (raw.startswith("'") and raw.endswith("'")) or len(raw) < 2:
            return None
        value = raw[1:-1].replace("\\\\", "\0\0").replace("\\'", "'").replace("\0\0", "\\")
        return None if ARGUMENT in value else value
    if not (raw.startswith('"') and raw.endswith('"')):
        return None
    parts: list[str] = []
    children = node.children[1:-1]
    i = 0
    while i < len(children):
        child = children[i]
        if child.type == "string_content":
            if "\\" in _text(child) or ARGUMENT in _text(child):
                return None
            parts.append(_text(child))
        elif child.type == "escape_sequence":
            decoded = _ESCAPES.get(_text(child))
            if decoded is None:
                return None
            parts.append(decoded)
        elif child.type == "variable_name" and param is not None and _text(child) == param:
            parts.append(ARGUMENT)
        elif (
            child.type == "{"
            and i + 2 < len(children)
            and children[i + 1].type == "variable_name"
            and param is not None
            and _text(children[i + 1]) == param
            and children[i + 2].type == "}"
        ):
            parts.append(ARGUMENT)
            i += 2
        else:
            return None
        i += 1
    return "".join(parts)


def _text(node: Node) -> str:
    return node.text.decode("utf-8") if node.text is not None else ""


__all__ = ["ARGUMENT", "COMPILES_TO", "FACT", "facts", "registrations"]
