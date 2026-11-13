"""Which of Go's predeclared names a file can trust to mean what Go predeclares.

The engine deletes a stranded local whose value only calls a builtin that does
no work (``n := len(items)``) or converts to a predeclared type
(``name := string(id)``), instead of keeping it as ``_ = …``. That is sound only
while ``len`` is Go's ``len`` and ``string`` is Go's ``string``: a package may
declare its own ``max`` (common before Go 1.21), and a function or a local may
bind any of these names, and then the call can do anything.

The engine sees one file, so the answer comes from here, as a per-file fact:
``{"go_unshadowed": "builtins"}`` when the file spells none of :data:`BUILTINS`
anywhere but as a call's callee, and no package-level declaration in another
file of its package declares one, and ``{"go_unshadowed": "types"}`` the same
for :data:`TYPES`, a type position counting as theirs too. Without the fact the
engine keeps the value, as it always did. Doubt withholds the fact: a file or a
sibling that does not parse cleanly, or whose package clause cannot be read,
gives neither.
"""

from __future__ import annotations

import os
import re

from tree_sitter import Node

from flag_cleanup.syntax import _parse, _walk

#: Builtins whose call does no work and cannot panic.
BUILTINS = frozenset({"len", "cap", "min", "max", "real", "imag", "complex", "new"})

#: Predeclared types a conversion to which does no work and cannot panic.
TYPES = frozenset({
    "bool", "byte", "complex64", "complex128", "error", "float32", "float64",
    "int", "int8", "int16", "int32", "int64", "rune", "string",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr", "any",
})

_NAMES = BUILTINS | TYPES

#: A sibling file that never spells one of the names cannot declare it; the
#: parse decides the rest.
_HINT = re.compile(rb"\b(?:" + b"|".join(sorted(n.encode() for n in _NAMES)) + rb")\b")

def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text is not None else ""


def _foreign(root: Node) -> set[str]:
    """The guarded names ``root`` spells anywhere but where only the predeclared
    meaning is used: the callee of a call (``len(xs)``, ``string(b)``) and, for a
    type name, a type position.

    Not a list of binding sites, which is how a type switch's alias once slipped
    through: a declaration must spell its name somewhere, and every place Go
    binds a name is an ``identifier`` or ``package_identifier`` that is no
    callee, the ``type_identifier`` naming a ``type`` spec, or a
    ``type_identifier`` in a method receiver's type arguments, which binds the
    method's type parameters (``func (l L[string]) m()``), so any spelling in a
    receiver counts. Those are the only ``type_identifier`` binding sites: a
    type parameter of a ``type`` or ``func`` declaration is an ``identifier``,
    and an embedded field's type name names a field. Fields, methods and labels
    (``field_identifier``, ``label_name``) bind nothing a call resolves to. So whatever binds one of the names shows up here, and so does a
    use of something that does (``f := len``, ``var x len``); a name used in some
    other harmless way (a struct literal key ``T{len: 1}``) withholds too, which
    is the safe side."""
    found: set[str] = set()
    for node in _walk(root):
        if node.type not in ("identifier", "package_identifier", "type_identifier"):
            continue
        name = _text(node)
        if name not in _NAMES:
            continue
        parent = node.parent
        if node.type == "identifier" and parent is not None and parent.type == "call_expression":
            callee = parent.child_by_field_name("function")
            if callee is not None and callee.id == node.id:
                continue
        if (
            node.type == "type_identifier"
            and name in TYPES
            and not _names_a_type_spec(node)
            and not _in_receiver(node)
        ):
            continue
        found.add(name)
    return found


def _in_receiver(node: Node) -> bool:
    """Whether ``node`` sits in a method's receiver, where a type argument of the
    receiver's type (``func (l L[string]) m()``) BINDS that name as a type
    parameter of the method, spelled as a ``type_identifier``."""
    child, parent = node, node.parent
    while parent is not None:
        if parent.type == "method_declaration":
            receiver = parent.child_by_field_name("receiver")
            return receiver is not None and receiver.id == child.id
        child, parent = parent, parent.parent
    return False


def _names_a_type_spec(node: Node) -> bool:
    parent = node.parent
    return (
        parent is not None
        and parent.type in ("type_spec", "type_alias")
        and any(n.id == node.id for n in parent.children_by_field_name("name"))
    )


def _package_clause(root: Node) -> str | None:
    clause = next((c for c in root.named_children if c.type == "package_clause"), None)
    if clause is None:
        return None
    name = next((c for c in clause.named_children if c.type == "package_identifier"), None)
    return _text(name) if name is not None else None


def _package_level(root: Node) -> set[str]:
    """The names ``root``'s package-level declarations bind (functions, not methods)."""
    found: set[str] = set()
    for declaration in root.named_children:
        if declaration.type == "function_declaration":
            found.update(_text(n) for n in declaration.children_by_field_name("name"))
        elif declaration.type in ("var_declaration", "const_declaration", "type_declaration"):
            for spec in _walk(declaration):
                if spec.type in ("var_spec", "const_spec", "type_spec", "type_alias"):
                    found.update(_text(n) for n in spec.children_by_field_name("name"))
    return found


def _siblings_declare(path: str, package: str) -> set[str] | None:
    """The names declared at package level by the other ``.go`` files of
    ``path``'s directory whose package clause is ``package``: the names of its
    function, ``var``, ``const`` and ``type`` declarations, which are all a Go
    file can declare at package level (methods and imports bind none there).
    ``None`` when the directory cannot be read, or a sibling that spells one of
    the names cannot be read or does not parse cleanly: its declarations are
    unknown."""
    directory = os.path.dirname(os.path.abspath(path))
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return None
    found: set[str] = set()
    for entry in entries:
        sibling = os.path.join(directory, entry)
        if not entry.endswith(".go") or sibling == os.path.abspath(path) or not os.path.isfile(sibling):
            continue
        try:
            data = open(sibling, "rb").read()
        except OSError:
            return None
        if not _HINT.search(data):
            continue
        try:
            root = _parse(data.decode("utf-8"), "go")
        except UnicodeDecodeError:
            return None
        if root.has_error:
            return None
        clause = _package_clause(root)
        if clause is None:
            return None
        if clause != package:
            continue
        found |= _package_level(root)
    return found


def unshadowed_facts(path: str, source: str) -> list[dict[str, str]]:
    """The ``go_unshadowed`` facts ``path`` (holding ``source``) earns."""
    root = _parse(source, "go")
    if root.has_error:
        return []
    package = _package_clause(root)
    if package is None:
        return []
    siblings = _siblings_declare(path, package)
    if siblings is None:
        return []
    declared = (_foreign(root) | siblings) & _NAMES
    facts = []
    if not declared & BUILTINS:
        facts.append({"go_unshadowed": "builtins"})
    if not declared & TYPES:
        facts.append({"go_unshadowed": "types"})
    return facts
