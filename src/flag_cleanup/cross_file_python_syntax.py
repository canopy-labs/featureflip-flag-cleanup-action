"""What every part of the Python profile for cross-file key constants reads
source with: the parse, Python's names, a module as an import spells it, and
the parts of an import statement."""

from __future__ import annotations

import ast
import importlib.util
import tokenize
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import syntax


_walk = xk.walk


def _nfkc(text: str) -> str:
    """``text`` as Python reads its names: identifiers are NFKC-normalized
    (PEP 3131), so ``ｋｅｙｓ`` IS ``keys``. Only names and token searches use
    this; a file is always PARSED as written."""
    return text if text.isascii() else unicodedata.normalize("NFKC", text)


def _text(node: Node) -> str:
    return _nfkc(node.text.decode("utf-8", "replace"))


def _read_python(path: Path) -> str | None:
    """The text Python runs for the file at ``path``, decoded by its coding
    cookie as the interpreter does, or None when Python could not decode it."""
    try:
        return importlib.util.decode_source(path.read_bytes())
    except (OSError, SyntaxError, LookupError, UnicodeError):
        return None


def _decoded_otherwise(source: str) -> bool:
    """Whether a coding cookie makes Python run other text than ``source``.

    ``raw_unicode_escape`` turns ``\\u000a`` inside a comment into a line
    break, so code the parse sees as a comment runs. An ASCII file under a
    ``latin-1`` cookie decodes to itself and is fine.
    """
    data = source.encode("utf-8")
    lines = iter(data.splitlines(keepends=True))
    try:
        encoding, _ = tokenize.detect_encoding(lambda: next(lines, b""))
        return encoding not in ("utf-8", "utf-8-sig") and data.decode(encoding) != source
    except (SyntaxError, LookupError, UnicodeError):
        return True


def _parse(source: str) -> Node:
    return syntax._parse(source, "python")


def _line(source: str, number: int) -> str:
    lines = source.splitlines()
    return lines[number - 1].strip() if 0 < number <= len(lines) else ""


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every module-level name ``path`` assigns, keyed by the file itself.

    The file is a declaration's module here, so no other file can declare the
    same qualified name: which file an import means is :func:`_resolve`'s
    question, and it refuses when the answer could be two.
    """
    found: set[xk.QualifiedName] = set()
    for statement in _parse(source).named_children:
        if statement.type != "expression_statement":
            continue
        for assignment in statement.named_children:
            if assignment.type != "assignment":
                continue
            left = assignment.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                found.add((path, None, _text(left)))
    return found


@dataclass(frozen=True)
class _Module:
    """A module as an import spells it: leading dots, then dotted parts."""

    level: int
    parts: tuple[str, ...]

    @property
    def spelled(self) -> str:
        return "." * self.level + ".".join(self.parts)


def _module_of(node: Node) -> _Module:
    """The module a ``dotted_name`` or ``relative_import`` names."""
    if node.type == "relative_import":
        prefix = next((c for c in node.children if c.type == "import_prefix"), None)
        dotted = next((c for c in node.named_children if c.type == "dotted_name"), None)
        level = len(_text(prefix)) if prefix is not None else 0
        parts = tuple(_text(i) for i in dotted.named_children) if dotted is not None else ()
        return _Module(level, parts)
    return _Module(0, tuple(_text(i) for i in node.named_children))


_IMPORT_TYPES = ("import_statement", "import_from_statement", "future_import_statement")


def _in_import(node: Node) -> Node | None:
    current = node.parent
    while current is not None:
        if current.type in _IMPORT_TYPES:
            return current
        current = current.parent
    return None


def _import_bound_names(statement: Node) -> list[tuple[Node, str]]:
    """``(binding node, name it binds)`` for every name an import binds.

    A ``__future__`` import binds its features too, under an ``as`` alias when
    it has one: ``from __future__ import annotations as keys`` binds ``keys``.
    """
    found: list[tuple[Node, str]] = []
    if statement.type == "import_statement":
        for child in statement.named_children:
            if child.type == "aliased_import":
                alias = child.child_by_field_name("alias")
                found.append((child, _text(alias)))
            elif child.type == "dotted_name":
                found.append((child, _text(child.named_children[0])))
    elif statement.type in ("import_from_statement", "future_import_statement"):
        for index in range(statement.child_count):
            if statement.field_name_for_child(index) != "name":
                continue
            child = statement.child(index)
            if child.type == "aliased_import":
                found.append((child, _text(child.child_by_field_name("alias"))))
            else:
                found.append((child, _text(child.named_children[-1])))
    return found


def _python_parse(text: str, root: Node) -> ast.Module | None:
    """Python's own parse of ``text``, when its top-level statements start
    exactly where tree-sitter's do; None otherwise, or when Python cannot
    parse it. A decorated definition starts at its ``def`` or ``class`` in
    Python's parse and at its first ``@`` in tree-sitter's, so its
    definition's start is the one compared."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    ours = []
    for statement in root.named_children:
        if statement.type in ("comment", "line_continuation"):
            continue
        if statement.type == "decorated_definition":
            statement = statement.child_by_field_name("definition") or statement
        ours.append((statement.start_point[0] + 1, statement.start_point[1]))
    if ours != [(s.lineno, s.col_offset) for s in tree.body]:
        return None
    return tree


def _ast_bound(node: ast.AST) -> list[str]:
    """Every name ``node`` binds or deletes, and every attribute it stores or
    deletes, other than through an import."""
    if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id if isinstance(node, ast.Name) else node.attr]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) or type(node).__name__ in (
        "TypeVar", "ParamSpec", "TypeVarTuple"
    ):
        name = getattr(node, "name", None)
        return [name] if name else []
    return []


def _imported_name(name_node: Node) -> str:
    """The name an import entry imports, before any ``as``."""
    dotted = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
    return _text(dotted)


def _not_a_mention(node: Node) -> bool:
    """An identifier that names an attribute or a keyword, never a variable."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "attribute":
        attribute = parent.child_by_field_name("attribute")
        return attribute is not None and attribute.id == node.id
    if parent.type == "keyword_argument":
        name = parent.child_by_field_name("name")
        return name is not None and name.id == node.id
    return False


def _alias(module: _Module) -> str:
    """The private name a sole-name rewrite binds the module to: ``_keys``.

    Private, so the rewrite publishes nothing: ``import a.b.keys as _keys``
    binds no ``a``, which a package ``__init__.py`` reader would otherwise
    set over a sibling submodule ``a``, and ``from m import *`` skips an
    underscore name when ``m`` has no ``__all__``.
    """
    return "_" + module.parts[-1]


def _line_end(data: bytes, offset: int) -> int:
    """Where the line holding ``offset`` ends, before any ``\\r\\n``."""
    line_end = data.find(b"\n", offset)
    line_end = len(data) if line_end == -1 else line_end
    if data[line_end - 1 : line_end] == b"\r":
        line_end -= 1
    return line_end
