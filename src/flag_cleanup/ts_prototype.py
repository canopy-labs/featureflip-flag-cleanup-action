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

Not seen: code assembled from strings for a loader other than ``eval`` and
``Function``; and, in a file that neither imports nor spells the class, a
member read with a computed key (``x[k]``, which may be ``x["constructor"]``
or ``x["prototype"]``) or a module object enumerated (``Object.values(ns)``)
before its ``prototype`` is taken.
"""

from __future__ import annotations

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
    for path, text in texts:
        if not (_UNNAMED_TOKENS.search(text) or (spelled is not None and spelled.search(text))):
            continue
        site = file_site(path, text, names)
        if site is not None:
            return site
    return None


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
        renamed = re.compile(rf"(?<![\w$]){token}\s+as\s+([A-Za-z_$][\w$]*)")
        default = re.compile(rf"\bexport\s+default\s+(?:class\s+)?[(\s]*{token}(?![\w$])")
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


def file_site(path: str, text: str, names: frozenset[str]) -> Site | None:
    """Where the file at ``path``, holding ``text``, reaches the prototype of
    the class ``names`` spell (:func:`aliases`), or None: the first line
    where either the token check (:func:`_token_line`) or the parsed judge
    (:class:`_FileJudge`) finds it."""
    root = ts_syntax._parse(text, _language(path))
    if root.has_error:
        return _text_site(path, text, names)
    lines = [_token_line(root, names)]
    node = _FileJudge(root, names).first()
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

    def __init__(self, root: Node, names: frozenset[str]) -> None:
        self.root = root
        self.names = names
        self.code_names = frozenset(n for n in names if n != "default")
        # name -> binder kinds: "class" (may be the class) or "else"
        self.binders: dict[str, list[tuple[str, Node]]] = {}
        self.binder_ids: set[int] = set()
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
            self.binder_ids.add(node.id)
            self.binders.setdefault(node.text.decode("utf-8"), []).append((kind, node))
        self.local = frozenset(
            name
            for name, kinds in self.binders.items()
            if any(kind == "class" for kind, _node in kinds)
        ) | frozenset(n for n in self.code_names if n not in self.binders)

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
            if text in self.code_names:
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
            return prop is not None and prop.text.decode("utf-8") in self.code_names
        if node.type == "subscript_expression":
            return _string_value(node.child_by_field_name("index")) in self.code_names
        return False

    # --- the walk ---------------------------------------------------------

    def first(self) -> Node | None:
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
        if kind in ("pair_pattern", "shorthand_property_identifier_pattern"):
            # `const { constructor: C } = x`, `{ __proto__: p }`, `{ [k]: v }`:
            # destructuring takes out what a member read would.
            key = node if kind == "shorthand_property_identifier_pattern" else node.child_by_field_name("key")
            if key is None or key.type == "computed_property_name":
                return True
            return _plain_key(key) in _CLASS_MEMBERS
        if kind in ("member_expression", "subscript_expression"):
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
            if kind == "member_expression" and self._reflection_value(node) is not None:
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
            if function.type == "identifier" and function.text in (b"eval", b"Function"):
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
        if expr.type == "member_expression":
            prop = expr.child_by_field_name("property")
            method = prop.text.decode("utf-8") if prop is not None else ""
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
            )
        return False

    @staticmethod
    def _member_key(node: Node) -> str | None:
        """The member a member or subscript expression names, or None for a
        computed subscript (``x[k]``), which is not followed."""
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            return prop.text.decode("utf-8") if prop is not None else None
        index = node.child_by_field_name("index")
        value = _string_value(index)
        return value

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
