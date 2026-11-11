"""Reads of a flag enum's entry off an object that spreads the enum.

``features: { ...Features, enabled }`` puts every member of the enum
``Features`` on the object, so ``ax.features.FollowSort`` reads
``Features.FollowSort`` without spelling the enum. Such a read is not folded:
proving that nothing writes to the object or replaces a member before it is
read takes a whole-repository proof this tool cannot close (any function the
object holds can hand it out as ``this``, and the files that import it are not
all found by their specifiers). The spread itself stays a use of the enum that
hands it on, so the entry is reported as left.

What this module adds is that the read is NAMED where it is, rather than
skipped: :class:`SpreadRoots` tells whether an expression's value may be got
from an object spreading a flag enum, so that ``<expr>.<Entry>`` and
``<expr>.<prop>.<Entry>`` are refused at their line. An unrelated object's
member of the same name (``this.props.DarkMode``) is not refused.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from . import source_index, ts_syntax


def _x():
    # cross_file_ts imports this module; its helpers are reached lazily.
    from . import cross_file_ts

    return cross_file_ts


def _import_locals(statement: Node) -> list[Node]:
    """The local name nodes an ``import`` statement binds."""
    found: list[Node] = []
    for clause in statement.named_children:
        if clause.type != "import_clause":
            continue
        for part in clause.named_children:
            if part.type == "identifier":
                found.append(part)  # a default import
            elif part.type == "namespace_import":
                found.extend(c for c in part.named_children if c.type == "identifier")
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type == "import_specifier":
                        local = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                        if local is not None:
                            found.append(local)
    for clause in statement.named_children:
        if clause.type == "import_require_clause":
            found.extend(c for c in clause.named_children[:1] if c.type == "identifier")
    return found


def _root_of(node: Node | None) -> Node | None:
    """The name an expression's value is got from: ``a`` of ``a.b.c``,
    ``useA`` of ``useA().b``, through wrappers and ``await``."""
    x = _x()
    for _ in range(64):
        node = x._unwrap(node)
        if node is None:
            return None
        if node.type == "member_expression":
            node = node.child_by_field_name("object")
        elif node.type == "call_expression":
            node = node.child_by_field_name("function")
        elif node.type == "await_expression":
            node = next((c for c in node.named_children if c.type != "comment"), None)
        elif node.type == "identifier":
            return node
        else:
            return None
    return None


def _spreads_an_enum(root: Node, owners: frozenset[str]) -> bool:
    """Whether the file spreads one of ``owners``, by name or through an import
    of that name under another."""
    x = _x()
    names = set(owners)
    for node in x._walk(root):
        if node.type == "import_specifier":
            name = node.child_by_field_name("name")
            alias = node.child_by_field_name("alias")
            if name is not None and alias is not None and x._text(name) in owners:
                names.add(x._text(alias))
    return any(
        node.type == "spread_element"
        and node.named_children
        and node.named_children[0].type == "identifier"
        and x._text(node.named_children[0]) in names
        for node in x._walk(root)
    )


class SpreadRoots:
    """Tells whether a read off ``<root>...`` may be a read off an object that
    spreads an enum: ``root`` is imported from a module that spreads one, or
    bound to a value got from such an import, or to a value in this file that
    spreads one. Only such a read is refused by name; any other chain
    (``this.props.X``) is another object's member."""

    def __init__(self, root: Node, path: str, owners: frozenset[str], repo_dir: str) -> None:
        self._root = root
        self._path = path
        self._owners = owners
        self._repo_dir = repo_dir
        self._modules: dict[str, bool] = {}
        self._names: dict[str, bool] = {}
        self._imports: dict[str, str] | None = None

    def _module_spreads(self, module: str) -> bool:
        x = _x()
        if module not in self._modules:
            try:
                text = Path(module).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            tree = ts_syntax._parse(text, x._language(module)) if "..." in text else None
            self._modules[module] = tree is not None and _spreads_an_enum(tree, self._owners)
        return self._modules[module]

    def _tail_matches(self, spec: str) -> list[str]:
        """The checkout's script files a bare ``spec`` may name: those whose
        path ends in its segments past the first (``#/a/b`` -> ``a/b``), as a
        file or as a directory's index."""
        x = _x()
        segments = [p for p in spec.split("/") if p]
        tail = segments[1:] if len(segments) > 1 else segments
        if not tail:
            return []
        found: list[str] = []
        suffixes = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"} & set(source_index.all_suffixes())
        for file in source_index.files(self._repo_dir, suffixes):
            parts = Path(file.resolved).parts
            stem = x._stem(file.resolved)
            named = [*parts[:-1], stem] if stem != "index" else list(parts[:-1])
            if named[-len(tail):] == tail:
                found.append(str(file.resolved))
        return found

    def _imported(self) -> dict[str, str]:
        """``{local: specifier}`` for every value import in the file."""
        x = _x()
        if self._imports is None:
            self._imports = {}
            for node in x._walk(self._root):
                if node.type != "import_statement" or x._type_only(node):
                    continue
                spec = x._string_value(node.child_by_field_name("source"))
                if spec is None:
                    continue
                for local in _import_locals(node):
                    self._imports[x._text(local)] = spec
        return self._imports

    def reaches(self, node: Node | None, depth: int = 0) -> bool:
        """Whether the expression ``node`` may be got from an object spreading an enum."""
        x = _x()
        name = _root_of(node)
        if name is None or depth > 4:
            return False
        local = x._text(name)
        if local in self._names:
            return self._names[local]
        self._names[local] = False  # a cycle through the bindings proves nothing
        spec = self._imported().get(local)
        found = False
        if spec is not None:
            module = x._resolve(self._path, spec, x._stop_for(self._repo_dir), strict=False)
            if module is not None:
                found = self._module_spreads(module)
            elif not spec.startswith((".", "/")):
                # An alias whose config cannot be read (it extends a package
                # that is not installed): any file the specifier's tail can
                # name. Only ever adds a refusal.
                found = any(self._module_spreads(m) for m in self._tail_matches(spec))
        if not found:
            for binder in ts_syntax.binding_nodes(self._root):
                if x._text(binder) != local:
                    continue
                declarator = binder.parent
                while declarator is not None and declarator.type in x._PATTERN_PARTS | {"object_pattern", "array_pattern"}:
                    declarator = declarator.parent
                if declarator is None or declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                if value is None:
                    continue
                if _spreads_an_enum(value, self._owners) or self.reaches(value, depth + 1):
                    found = True
                    break
        self._names[local] = found
        return found


__all__ = ["SpreadRoots"]
