"""Ruby's local-versus-method resolution, and the refusal built on it.

Ruby decides, TEXTUALLY, whether a bare identifier is a local variable or a
method call: it is a local iff an assignment (or another binding) to that name
appears EARLIER in the source within the same local scope. The assignment need
not run: ``y = 1 if false`` still declares ``y`` (holding ``nil``) for every
later read. So a fold that deletes the dead assignment can change what a
SURVIVING read means: ``puts y.inspect`` printed ``nil`` and now raises
``NameError`` (or calls some method ``y``, silently, when one exists).

The gate resolves every bare identifier in the file before and after the
rewrite, pairs each surviving read with the one it came from, and refuses when
a read's resolution flipped. Sound by construction rather than by shape: it
asks the question Ruby's parser asks, of every read, whatever the rewrite
deleted or moved. A read the rewrite deleted is not asked about.

Scopes. ``def``, ``class``, ``module``, ``class <<`` and the file start a new
scope that sees nothing outside. A block or lambda sees its enclosing locals,
and a name first bound inside it is its own. ``for``, ``if`` and loops open
no scope. Bindings: ``=``, op-assigns (``||=``, ``+=``, ...), multiple and
nested assignment (splat too), ``rescue => e``, ``for x in``, method, block
and lambda parameters (and block-locals), pattern matching (``in x``,
``in {y:}``, ``=> y``, ``in Integer => y``, splats), and the named groups of a
regex literal on the LEFT of ``=~``. A binding is in force from its own text
on, and a left-hand side precedes the right (``x = x`` reads a local).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

#: Kinds that start a scope seeing no enclosing locals.
_HARD = frozenset({"program", "method", "singleton_method", "class", "singleton_class", "module"})
#: Kinds that open a scope inheriting the enclosing locals.
_SOFT = frozenset({"block", "do_block", "lambda"})
_BINDING_LISTS = frozenset({
    "left_assignment_list", "destructured_left_assignment", "rest_assignment", "splat_parameter",
    "mlhs", "destructured_parameter",
})
_PARAMETER_HOLDERS = frozenset({"method_parameters", "block_parameters", "lambda_parameters"})
_NAMED_PARAMETERS = frozenset({
    "optional_parameter", "keyword_parameter", "splat_parameter", "hash_splat_parameter",
    "block_parameter",
})
_NAMED_GROUP = re.compile(r"\(\?<([a-z_][A-Za-z0-9_]*)>")


@dataclass(frozen=True)
class Read:
    """One bare identifier read: where, what and whether Ruby takes it as a local."""

    line: int
    index: int
    name: str
    local: bool


class _Scopes:
    def __init__(self) -> None:
        self.stack: list[tuple[bool, set[str]]] = [(True, set())]

    def push(self, hard: bool) -> None:
        self.stack.append((hard, set()))

    def pop(self) -> None:
        self.stack.pop()

    def visible(self, name: str) -> bool:
        for hard, names in reversed(self.stack):
            if name in names:
                return True
            if hard:
                return False
        return False

    def declare(self, name: str) -> None:
        if not self.visible(name):
            self.stack[-1][1].add(name)


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


class _Resolver:
    def __init__(self) -> None:
        self.scopes = _Scopes()
        self.reads: list[Read] = []
        self._per_line: dict[int, int] = {}

    def _read(self, node, name: str) -> None:
        line = node.start_point[0]
        index = self._per_line.get(line, 0)
        self._per_line[line] = index + 1
        self.reads.append(Read(line, index, name, self.scopes.visible(name)))

    # -- bindings -----------------------------------------------------------------

    def _bind(self, node) -> None:
        """``node`` is where a name is being bound."""
        if node.type == "identifier":
            self.scopes.declare(_text(node))
        elif node.type in _BINDING_LISTS:
            for child in node.children:
                if child.is_named:
                    self._bind_part(child)
        else:
            self._visit(node)

    def _bind_part(self, node) -> None:
        if node.type in _NAMED_PARAMETERS and node.child_by_field_name("name") is not None:
            self._bind(node.child_by_field_name("name"))
            value = node.child_by_field_name("value")
            if value is not None:
                self._visit(value)
        else:
            self._bind(node)

    def _pattern(self, node) -> None:
        """A pattern: every bare name in it binds, ``^x`` and constants read."""
        kind = node.type
        if kind == "identifier":
            self.scopes.declare(_text(node))
        elif kind == "variable_reference_pattern":
            name = node.child_by_field_name("name")
            if name is not None:
                self._read(name, _text(name))
        elif kind == "keyword_pattern":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            if value is None and key is not None:
                self.scopes.declare(_text(key))
            elif value is not None:
                self._pattern(value)
        elif kind in ("as_pattern", "splat_parameter"):
            for child in node.children:
                if child.is_named:
                    self._pattern(child)
        elif kind in (
            "array_pattern", "find_pattern", "hash_pattern", "alternative_pattern",
            "parenthesized_pattern", "pair", "hash_splat_parameter",
        ):
            for child in node.children:
                if child.is_named:
                    self._pattern(child)
        else:
            self._visit(node)

    # -- traversal ----------------------------------------------------------------

    def _children(self, node) -> None:
        for child in node.children:
            if child.is_named:
                self._visit(child)

    def _parameters(self, node) -> None:
        for child in node.children:
            if child.is_named:
                self._bind_part(child)
        for local in node.children_by_field_name("locals"):
            self._bind(local)

    def _visit(self, node) -> None:
        kind = node.type
        if kind == "identifier":
            self._read(node, _text(node))
        elif kind == "hash_key_symbol":
            parent = node.parent
            if parent is not None and parent.type == "pair" and parent.child_by_field_name("value") is None:
                self._read(node, _text(node))
        elif kind in ("assignment", "operator_assignment"):
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None:
                self._bind(left)
            if right is not None:
                self._visit(right)
        elif kind == "for":
            pattern = node.child_by_field_name("pattern")
            if pattern is not None:
                self._bind(pattern)
            for child in node.children:
                if child.is_named and child != pattern:
                    self._visit(child)
        elif kind in ("exception_variable",):
            for child in node.children:
                if child.is_named:
                    self._bind(child)
        elif kind in ("in_clause", "match_pattern"):
            for child in node.children:
                if not child.is_named:
                    continue
                if child == node.child_by_field_name("pattern"):
                    self._pattern(child)
                else:
                    self._visit(child)
        elif kind == "call":
            for child in node.children:
                if child.is_named and child != node.child_by_field_name("method"):
                    self._visit(child)
        elif kind in ("method", "singleton_method"):
            for child in node.children:  # `def self.x` reads its receiver in the outer scope
                if child.is_named and child == node.child_by_field_name("object"):
                    self._visit(child)
            self.scopes.push(True)
            for child in node.children:
                if not child.is_named or child == node.child_by_field_name("name"):
                    continue
                if child == node.child_by_field_name("object"):
                    continue
                self._visit(child)
            self.scopes.pop()
        elif kind in ("class", "module", "singleton_class"):
            self.scopes.push(True)
            self._children(node)
            self.scopes.pop()
        elif kind in _SOFT:
            self.scopes.push(False)
            self._children(node)
            self.scopes.pop()
        elif kind in _PARAMETER_HOLDERS:
            self._parameters(node)
        elif kind == "binary" and node.child_by_field_name("operator") is not None and _text(
            node.child_by_field_name("operator")
        ) == "=~":
            left = node.child_by_field_name("left")
            self._children(node)
            if left is not None and left.type == "regex" and not any(
                c.type == "interpolation" for c in left.children
            ):
                for name in _NAMED_GROUP.findall(_text(left)):
                    self.scopes.declare(name)
        else:
            self._children(node)


def reads(source_root) -> list[Read]:
    """Every bare-identifier read under ``source_root``, in source order."""
    resolver = _Resolver()
    resolver._visit(source_root)
    return resolver.reads


def _name_lists(found: list[Read]) -> dict[str, list[Read]]:
    by_name: dict[str, list[Read]] = {}
    for read in found:
        by_name.setdefault(read.name, []).append(read)
    return by_name


def flipped_read(before: str, after: str, parse) -> tuple[str, int, bool] | None:
    """A surviving read whose resolution changed: ``(name, line, was_local)``.

    ``parse`` turns Ruby source into a tree-sitter root. Reads are paired through
    a line diff of the whitespace-stripped lines: a line both files hold pairs
    its reads one to one, and within a rewritten hunk the reads of one name pair
    in order. A read the rewrite deleted has no partner and is not asked about.
    """
    before_reads = reads(parse(before))
    after_reads = reads(parse(after))
    before_lines = [line.strip() for line in before.splitlines()]
    after_lines = [line.strip() for line in after.splitlines()]
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old = [r for r in before_reads if i1 <= r.line < i2]
        new = [r for r in after_reads if j1 <= r.line < j2]
        if tag == "equal":
            pairs = list(zip(old, new))
        else:
            old_by, new_by = _name_lists(old), _name_lists(new)
            pairs = [p for name, rs in new_by.items() for p in zip(old_by.get(name, []), rs)]
        for was, now in pairs:
            if was.name == now.name and was.local != now.local:
                return now.name, now.line + 1, was.local
    return None
