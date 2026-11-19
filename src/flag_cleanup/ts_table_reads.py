"""A flag-keyed table entry is deleted only when every use of the table is a literal read.

The entry rules delete ``look: true`` from ``const flags = { look: true, other: false }``
on the strength of the key. That is right only when every read of the table
names a key the run resolves: ``flags[name]`` with a variable ``name`` reads
``flags.look`` at runtime, and the deletion makes it ``undefined`` where it
answered ``true``.

This is a WHITELIST, decided on the customer's bytes before the engine runs.
Keeping an entry is always safe, so anything not proven keeps every entry of
the pass (:func:`holds` returns a reason per file; the caller runs without the
entry rules and reports the entries as kept).

An entry is deletable only when its object

* is made only of plain pairs (an identifier or string key with a literal value:
  a method, getter, spread, computed key or any other value could read the table
  through ``this`` or hand it on; a free-standing override only has to be free of
  members that run code, since its values are expressions the test evaluates),
  and is either
* a free-standing literal in a TEST file (a call argument, a JSX attribute value
  or an array element): an override handed to the code under test; or
* bound directly, ``const NAME = { ... }`` (``Object.freeze(...)``, ``as const``
  and ``satisfies`` around it are allowed), in a module file that holds no
  ``eval``, ``Function`` or ``with``, where EVERY whole-word mention of ``NAME``
  in the file is the declaration, a plain ``export { NAME }``, ``NAME.literal``,
  or ``NAME['literal']`` / ``NAME[CONST]``. ``CONST`` must be a top-level ``const``
  string that nothing else in the file rebinds, and no key may be an
  ``Object.prototype`` member.

A table that is never exported is fully proven by that. An EXPORTED table also
needs the importer proof of :mod:`flag_cleanup.ts_table_importers`, which finds
every module that can load the file and checks how each one reads the table.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index, ts_syntax

_SUFFIXES = (".ts", ".mts", ".cts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TRANSPARENT = frozenset(
    {"parenthesized_expression", "as_expression", "non_null_expression", "satisfies_expression", "type_assertion"}
)
#: Members every plain object answers: reading one hands the table on.
_OBJECT_MEMBERS = frozenset(
    {
        "valueOf", "toString", "toLocaleString", "hasOwnProperty", "isPrototypeOf", "propertyIsEnumerable",
        "constructor", "__proto__", "__defineGetter__", "__defineSetter__", "__lookupGetter__", "__lookupSetter__",
    }
)
_IDENTIFIERS = frozenset({"identifier", "property_identifier", "shorthand_property_identifier", "type_identifier", "shorthand_property_identifier_pattern", "statement_identifier"})


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _language(path: str) -> str:
    suffix = Path(path).suffix
    return "tsx" if suffix in (".tsx", ".jsx") else "ts" if suffix in (".ts", ".mts", ".cts") else "js"


def is_test_path(path: str, source: str) -> bool:
    """A test file, decided by its path alone; a file holding in-source tests is not one."""
    if "import.meta.vitest" in source:
        return False
    parts = Path(path).parts
    name = Path(path).name
    return "__tests__" in parts or bool(re.search(r"\.(test|spec)\.[^.]+$", name))


def _outer(node: Node) -> Node:
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        node = node.parent
    return node


def _string_literal(node: Node | None) -> bool:
    if node is None:
        return False
    while node.type in _TRANSPARENT and node.named_children:
        node = node.named_children[0]
    if node.type == "string":
        return True
    return node.type == "template_string" and not any(c.type == "template_substitution" for c in node.named_children)


class _Checkout:
    """The checkout's TS/JS files, parsed at most once each."""

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir
        self._parsed: dict[str, tuple[str, Node]] | None = None

    def files(self) -> dict[str, tuple[str, Node]]:
        if self._parsed is None:
            parsed: dict[str, tuple[str, Node]] = {}
            for listed in source_index.files(self.repo_dir, _SUFFIXES):
                location = listed.path if listed.path.is_absolute() else Path(self.repo_dir) / listed.path
                try:
                    source = location.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                parsed[str(location)] = (source, ts_syntax._parse(source, _language(str(location))))
            self._parsed = parsed
        return self._parsed


def _word(names: set[str]) -> re.Pattern[str]:
    return re.compile("(?<![\\w$])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + ")(?![\\w$])")


def holds(repo_dir: str, flag_key: str) -> dict[str, str]:
    """``{path: reason}`` for every file whose entry for ``flag_key`` must stay."""
    checkout = _Checkout(repo_dir)
    found: dict[str, str] = {}
    candidates: list[tuple[str, Node, str]] = []
    for path, (source, root) in checkout.files().items():
        if flag_key not in source:
            continue
        test_file = is_test_path(path, source)
        for node in ts_syntax._walk(root):
            if node.type != "pair" or _pair_key(node) != flag_key:
                continue
            host = node.parent
            if host is None or host.type != "object":
                continue
            verdict = _position(host, test_file)
            # A free-standing override only has to be free of members that run code on
            # the object (a method, getter, spread or computed key); its values are
            # expressions the test evaluates, and a bound table's must be literals.
            if not isinstance(verdict, str) and not _plain_table(host, any_value=verdict is None):
                found.setdefault(path, "has a member that is not a plain `key: literal` pair (a method, getter, spread, computed key or non-literal value)")
                continue
            if verdict is None:
                continue
            if isinstance(verdict, str):
                found.setdefault(path, verdict)
            else:
                candidates.append((path, verdict, _text(verdict)))
    for path, binder, name in candidates:
        if path in found:
            continue
        why = _unexplained(checkout, path, binder, name)
        if why is None and _exported(checkout.files()[path][1], binder, name):
            # Readers in other files: every module that can load this one must be
            # resolved and read the table by literals only.
            from flag_cleanup import ts_table_importers

            other = ts_table_importers.hold_reason(repo_dir, checkout.files(), path, {name})
            if other is not None:
                why = f"is exported and {other}"
        if why is not None:
            found[path] = f"the table `{name}` {why}"
    return found


def _exported(root: Node, binder: Node, name: str) -> bool:
    """Whether the module exports the table under its own name."""
    lexical = binder.parent.parent if binder.parent is not None else None
    if lexical is not None and lexical.parent is not None and lexical.parent.type == "export_statement":
        return True
    for node in ts_syntax._walk(root):
        if node.type == "export_specifier" and node.parent is not None:
            inner = node.child_by_field_name("name")
            if inner is not None and _text(inner) == name:
                return True
    return False


def _pair_key(pair: Node) -> str | None:
    key = pair.child_by_field_name("key")
    if key is None:
        return None
    if key.type == "property_identifier":
        return _text(key)
    if key.type == "string":
        fragments = [c for c in key.named_children if c.type == "string_fragment"]
        return _text(fragments[0]) if len(fragments) == 1 and len(key.named_children) == 1 else None
    return None


def _position(host: Node, test_file: bool) -> Node | str | None:
    """Where the object holding the entry sits: ``None`` when its entry may go
    unconditionally, a string (why it must stay), or the ``const`` name node it
    is bound to directly."""
    node = _outer(host)
    while True:
        parent = node.parent
        if parent is None:
            return "a position it cannot follow"
        if parent.type == "arguments" and _is_freeze(parent):
            node = _outer(parent.parent)  # type: ignore[arg-type]
            continue
        break
    if parent.type == "variable_declarator" and parent.child_by_field_name("value") == node:
        name = parent.child_by_field_name("name")
        lexical = parent.parent
        if name is None or name.type != "identifier":
            return "is bound through a destructuring pattern"
        if lexical is None or lexical.type != "lexical_declaration" or not any(c.type == "const" for c in lexical.children):
            return "is bound with `let` or `var`, which can be reassigned"
        return name
    if test_file and parent.type in ("arguments", "jsx_expression", "array"):
        return None  # an override handed to the code under test
    if parent.type == "pair":
        return "is nested in another object"
    if parent.type in ("public_field_definition", "field_definition", "class_body"):
        return "is a class field"
    if parent.type in ("export_statement", "return_statement", "assignment_expression", "augmented_assignment_expression"):
        return "is exported as a default, returned or assigned"
    if parent.type in ("required_parameter", "optional_parameter", "assignment_pattern"):
        return "is a parameter default"
    if parent.type in ("arguments", "jsx_expression", "array", "ternary_expression", "binary_expression", "arrow_function", "spread_element"):
        return "is a value handed on, not a table bound to a name"
    return "sits in a position this does not recognise"


def _is_freeze(arguments: Node) -> bool:
    call = arguments.parent
    function = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
    return function is not None and _text(function) == "Object.freeze"


def _unexplained(checkout: _Checkout, declaring: str, binder: Node, name: str) -> str | None:
    """Why some mention of the table in its own file is not explained, else ``None``."""
    source, root = checkout.files()[declaring]
    if not any(c.type in ("import_statement", "export_statement", "export_assignment") for c in root.children):
        return "is declared in a script file, whose top-level names are global"
    for node in ts_syntax._walk(root):
        if node.type == "with_statement":
            return "is declared in a file that uses `with`"
        if node.type == "identifier" and node.child_count == 0 and _text(node) in ("eval", "Function"):
            return "is declared in a file that uses `eval` or `Function`"
    data = source.encode("utf-8")
    pattern = _word({name})
    matches = {len(source[: m.start()].encode("utf-8")) for m in pattern.finditer(source)}
    explained = _explained(root, data, {name}, binder)
    specifiers = _specifier_ranges(root)
    if {m for m in matches - explained if not any(a <= m < b for a, b in specifiers)}:
        return "is mentioned in a way this cannot follow"
    return None


def _explained(root: Node, data: bytes, names: set[str], binder: Node | None) -> set[int]:
    """Start offsets (in bytes) of the mentions of ``names`` that are acceptable."""
    constants = string_constants(root)
    explained: set[int] = set()
    for node in ts_syntax._walk(root):
        if node.type != "identifier" or node.child_count != 0 or _text(node) not in names:
            continue
        parent = node.parent
        if parent is None:
            continue
        if binder is not None and node.id == binder.id:
            explained.add(node.start_byte)
            continue
        if parent.type == "export_specifier" and parent.child_by_field_name("name") == node and parent.child_by_field_name("alias") is None:
            explained.add(node.start_byte)  # `export { NAME }`: the declaration's own export
            continue
        outer = _outer(node)
        holder = outer.parent
        if holder is None:
            continue
        if holder.type == "member_expression" and holder.child_by_field_name("object") == outer:
            prop = holder.child_by_field_name("property")
            if prop is not None and prop.type == "property_identifier" and _text(prop) not in _OBJECT_MEMBERS:
                explained.add(node.start_byte)
        elif holder.type == "subscript_expression" and holder.child_by_field_name("object") == outer:
            index = holder.child_by_field_name("index")
            key = _key_text(index, constants)
            if key is not None and key not in _OBJECT_MEMBERS:
                explained.add(node.start_byte)
    return explained


def _key_text(index: Node | None, constants: dict[str, str]) -> str | None:
    """The string a subscript key spells: a literal, or a same-file ``const`` string."""
    if index is None:
        return None
    while index.type in _TRANSPARENT and index.named_children:
        index = index.named_children[0]
    if any(c.type == "escape_sequence" for c in index.named_children):
        return None  # `"valu\x65Of"` spells a key the source text does not show
    if index.type == "string":
        return "".join(_text(c) for c in index.named_children if c.type == "string_fragment")
    if index.type == "template_string" and not any(c.type == "template_substitution" for c in index.named_children):
        return _text(index).strip("`")
    if index.type == "identifier":
        return constants.get(_text(index))
    return None


def string_constants(root: Node) -> dict[str, str]:
    """Names usable as a literal key: a TOP-LEVEL ``const`` bound to a plain
    string, whose name is bound nowhere else in the file (no parameter, local,
    catch, loop variable, import, function, class or destructuring target can
    shadow it where it is used)."""
    bindings: dict[str, int] = {}
    for node in ts_syntax.binding_nodes(root):
        bindings[_text(node)] = bindings.get(_text(node), 0) + 1
    for node in ts_syntax._walk(root):
        # Import clause identifiers carry no field: a default or namespace binding.
        if node.type in ("import_clause", "namespace_import", "import_require_clause"):
            for part in node.named_children:
                if part.type == "identifier":
                    bindings[_text(part)] = bindings.get(_text(part), 0) + 1
    found: dict[str, str] = {}
    for node in root.children:
        declaration = node.child_by_field_name("declaration") if node.type == "export_statement" else node
        if declaration is None or declaration.type != "lexical_declaration" or not any(c.type == "const" for c in declaration.children):
            continue
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            name, value = declarator.child_by_field_name("name"), declarator.child_by_field_name("value")
            if name is None or name.type != "identifier" or not _string_literal(value):
                continue
            spelled = _key_text(value, {})
            if spelled is not None and bindings.get(_text(name), 0) == 1:
                found[_text(name)] = spelled
    return found


def _plain_table(host: Node, *, any_value: bool = False) -> bool:
    """Whether every member of the object literal is a plain pair: an identifier
    or string key with a literal value (a boolean, string, number, ``null``, or
    a nested plain object or array of those). A method, getter, setter, function,
    spread, computed key, or any other value could read the table through
    ``this`` or hand it on."""
    if host.type == "array":
        return all(_plain_value(c) for c in host.named_children if c.type != "comment")
    for member in host.named_children:
        if member.type == "comment":
            continue
        if member.type != "pair":
            return False
        key = member.child_by_field_name("key")
        if key is None or key.type not in ("property_identifier", "string", "number"):
            return False
        if key.type == "string" and not _string_literal(key):
            return False
        value = member.child_by_field_name("value")
        if value is None:
            return False
        if any_value:
            # Any expression, except one that is a function with its own `this`:
            # it can read the override through the object it is called on.
            inner = value
            while inner.type in _TRANSPARENT and inner.named_children:
                inner = inner.named_children[0]
            if inner.type in ("function_expression", "generator_function"):
                return False
        elif not _plain_value(value):
            return False
    return True


def _plain_value(value: Node) -> bool:
    while value.type in _TRANSPARENT and value.named_children:
        value = value.named_children[0]
    if value.type in ("true", "false", "null", "number", "string"):
        return True
    if value.type == "template_string":
        return _string_literal(value)
    if value.type == "unary_expression":
        operand = value.child_by_field_name("argument")
        return operand is not None and operand.type == "number"
    if value.type in ("object", "array"):
        return _plain_table(value)
    return False


def _specifier_ranges(root: Node) -> list[tuple[int, int]]:
    """Byte ranges of module specifier strings (``'./flags'``): a path, not a mention."""
    ranges: list[tuple[int, int]] = []
    for node in ts_syntax._walk(root):
        if node.type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
            if source is not None:
                ranges.append((source.start_byte, source.end_byte))
            for child in node.named_children:
                if child.type == "import_require_clause":
                    for part in child.named_children:
                        if part.type == "string":
                            ranges.append((part.start_byte, part.end_byte))
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is not None and (_text(function) == "require" or function.type == "import") and arguments is not None:
                first = arguments.named_children[0] if arguments.named_children else None
                if first is not None and first.type == "string":
                    ranges.append((first.start_byte, first.end_byte))
    return ranges
