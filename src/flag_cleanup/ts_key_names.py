"""Which spellings of a code key in a TypeScript-family file are NAMES, not values.

A ``key-aliases`` spelling can be the name of the constant or enum member that
holds it (``export enum Keys { IS_X = 'IS_X' }``). Every reader in another file
then spells the key too, but as a name: ``Keys.IS_X``, ``IS_X``,
``import { IS_X }``. Such a file does not hold the key; it reads it through the
declaration, like a reader of a constant whose value its name does not spell.

:func:`without_names` blanks exactly those positions, and nothing else:

* an ``identifier`` (a binding or a reference to one, an import or export
  specifier's name) and a ``type_identifier``;
* the property of a member access whose object is, through parentheses and
  ``!``, a name this file IMPORTS (``Keys.IS_X``, ``ns.IS_X``), or a member
  chain rooted at one (``ns.Keys.IS_X``); the same for the parts of a dotted
  name (``Keys.IS_X`` in a type or a JSX tag).

Every other position still counts, and each is a place the key can be a
value: a string or template text (``'IS_X'``, ``flag="IS_X"``), a property of
anything else (``flags.IS_X``, ``props.IS_X``, ``useFlags().IS_X``,
``this.IS_X``: a flag map keyed by the spelling), an object or type member's
key (``{ IS_X: true }`` is ``{ "IS_X": true }``), a shorthand property or
destructured name (``{ IS_X }``, ``const { IS_X } = flags``), a class member's
name, a JSX attribute's name, a comment, JSX text. A file that does not parse
is returned unchanged, so it counts wherever the text spells the key.

Only a spelling the run's ``key-aliases`` maps a flag to is read this way
(:func:`declaring`, :func:`aliased`); any other key keeps the plain text
search.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from tree_sitter import Node

from flag_cleanup import ts_syntax

#: Leaves that are always a name, wherever they sit.
_NAMES = frozenset({"identifier", "type_identifier"})
#: Containers whose property is a name when their root is an imported name.
_DOTTED = frozenset({"member_expression", "nested_identifier", "nested_type_identifier"})
#: What may wrap a member access's object without changing what it is.
_TRANSPARENT = frozenset({"parenthesized_expression", "non_null_expression"})

_declared: frozenset[str] = frozenset()


@contextmanager
def declaring(code_keys: Iterable[str]) -> Iterator[None]:
    """Treat each of ``code_keys`` (the ``key-aliases`` spellings) as one a
    name can spell, for the scope. Set once per run, like
    :func:`flag_cleanup.inert_load.declaring`; nested scopes add to it."""
    global _declared
    previous = _declared
    _declared = previous | frozenset(code_keys)
    try:
        yield
    finally:
        _declared = previous


def aliased(flag_key: str) -> bool:
    """Whether ``flag_key`` is a ``key-aliases`` spelling of this run."""
    return flag_key in _declared


def _language(path: str) -> str:
    if path.endswith(".tsx"):
        return "tsx"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "js"
    return "ts"


def _imported(root: Node) -> frozenset[str]:
    """The names this file's top-level imports bind: default, named (under
    its local name), namespace, and ``import x = require(...)``."""
    names: set[str] = set()
    for statement in root.named_children:
        if statement.type != "import_statement":
            continue
        for part in statement.named_children:
            if part.type == "import_require_clause":
                names.update(c.text.decode() for c in part.named_children if c.type == "identifier")
            if part.type != "import_clause":
                continue
            for item in part.named_children:
                if item.type == "identifier":
                    names.add(item.text.decode())
                elif item.type == "namespace_import":
                    names.update(c.text.decode() for c in item.named_children if c.type == "identifier")
                elif item.type == "named_imports":
                    for spec in item.named_children:
                        if spec.type != "import_specifier":
                            continue
                        local = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                        if local is not None and local.type == "identifier":
                            names.add(local.text.decode())
    return frozenset(names)


def _rooted(node: Node | None, imported: frozenset[str]) -> bool:
    """Whether ``node`` is an imported name, or a member chain rooted at one."""
    while node is not None:
        if node.type in _TRANSPARENT:
            node = next((c for c in node.named_children if c.type != "comment"), None)
        elif node.type in _DOTTED:
            node = node.child_by_field_name("object") or node.child_by_field_name("module")
        else:
            return node.type == "identifier" and node.text.decode() in imported
    return False


def _is_name(node: Node, imported: frozenset[str]) -> bool:
    if node.type in _NAMES:
        return True
    if node.type != "property_identifier":
        return False
    parent = node.parent
    if parent is None or parent.type not in _DOTTED:
        return False
    if parent.child_by_field_name("property") != node:
        return False
    return _rooted(parent.child_by_field_name("object"), imported)


def without_names(text: str, path: str) -> str:
    """``text`` of ``path`` with every name position (see the module
    docstring) blanked to spaces, byte for byte. Unchanged when it does not
    parse."""
    root = ts_syntax._parse(text, _language(path))
    if root.has_error:
        return text
    imported = _imported(root)
    raw = bytearray(text.encode("utf-8"))
    stack = [root]
    while stack:
        node = stack.pop()
        if _is_name(node, imported):
            raw[node.start_byte : node.end_byte] = b" " * (node.end_byte - node.start_byte)
            continue
        stack.extend(node.children)
    return raw.decode("utf-8")
