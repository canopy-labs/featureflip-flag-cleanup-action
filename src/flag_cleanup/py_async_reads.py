"""Which Python accessor reads may become a bare literal: not an un-awaited ``async def``.

A project's accessor (``accessors: is_on``) is cloned from the synchronous seed, so a read
folds to ``True``/``False``. When the accessor is an ``async def`` an un-awaited call is a
coroutine, which is always truthy: ``if is_on(ctx, "k"):`` took the true branch whatever
the flag served, and the fold changed that. A read directly awaited folds as before (the
engine rewrites the whole ``await``). Any other read of an accessor that is, or may be, an
``async def`` refuses the flag, before anything is written: a partial removal is forbidden.

The callee is resolved without running anything: a name bound by a ``def`` in the file or
by ``from m import n [as z]``, a module attribute (``sdk.is_on`` after ``import sdk`` /
``import a.sdk as sdk`` / ``from a import sdk``), a method through ``self``/``cls`` or a
class of the repo. A callee that resolves only to synchronous definitions folds; one that
resolves to an ``async def`` refuses. One that cannot be resolved refuses when the file
defines or imports an ``async def`` of that name, and folds otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import wrapper_python

_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "site-packages", "dist", "build"})


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    accessor: str
    why: str


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


class _Repo:
    """Parsed Python files of the checkout, read on demand."""

    def __init__(self, repo_dir: str, names: frozenset[str]) -> None:
        self.root = Path(repo_dir)
        self.names = names
        self._trees: dict[str, tuple[bytes, Node] | None] = {}
        self._files: list[str] | None = None

    def files(self) -> list[str]:
        if self._files is None:
            found: list[str] = []
            for base, dirs, names in os.walk(self.root):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                found += [os.path.join(base, n) for n in names if n.endswith(".py")]
            self._files = sorted(found)
        return self._files

    def tree(self, path: str) -> Node | None:
        if path not in self._trees:
            source = wrapper_python._read(path)
            self._trees[path] = None if source is None else (source.encode(), wrapper_python.parse(source))
        entry = self._trees[path]
        return None if entry is None else entry[1]

    def module_files(self, dotted: str, near: str, level: int) -> list[str]:
        """The repo files a ``dotted`` module name (relative ``level`` dots) may be."""
        parts = [p for p in dotted.split(".") if p]
        out: list[str] = []
        if level:
            base = Path(near).parent
            for _ in range(level - 1):
                base = base.parent
            for cand in _module_paths(base, parts):
                if cand.is_file():
                    out.append(str(cand))
            return out
        suffix = parts
        for path in self.files():
            rel = Path(path).relative_to(self.root).with_suffix("")
            pieces = list(rel.parts[:-1]) if rel.name == "__init__" else list(rel.parts)
            if suffix and len(pieces) >= len(suffix) and pieces[-len(suffix):] == suffix:
                out.append(path)
        return out


def _module_paths(base: Path, parts: list[str]):
    target = base.joinpath(*parts) if parts else base
    yield target.with_suffix(".py") if parts else base / "__init__.py"
    yield target / "__init__.py"


def _definitions(root: Node, name: str) -> list[Node]:
    """Every ``def``/``async def`` of ``name`` in a tree, at any depth."""
    return [
        n for n in _walk(root)
        if n.type == "function_definition"
        and (nm := n.child_by_field_name("name")) is not None
        and _text(nm) == name
    ]


def _is_async(definition: Node) -> bool:
    return any(c.type == "async" for c in definition.children)


def _top_level(root: Node, name: str) -> list[Node]:
    out = []
    for stmt in root.named_children:
        node = stmt.child_by_field_name("definition") if stmt.type == "decorated_definition" else stmt
        if node is not None and node.type == "function_definition":
            nm = node.child_by_field_name("name")
            if nm is not None and _text(nm) == name:
                out.append(node)
    return out


def _class_methods(root: Node, cls: str, name: str) -> list[Node] | None:
    """The methods ``name`` of the class ``cls`` in ``root``, or ``None`` when it has none named so or no such class."""
    found: list[Node] = []
    seen_class = False
    for node in _walk(root):
        if node.type == "class_definition" and (nm := node.child_by_field_name("name")) is not None and _text(nm) == cls:
            seen_class = True
            body = node.child_by_field_name("body")
            for stmt in body.named_children if body is not None else []:
                fn = stmt.child_by_field_name("definition") if stmt.type == "decorated_definition" else stmt
                if fn is not None and fn.type == "function_definition":
                    fname = fn.child_by_field_name("name")
                    if fname is not None and _text(fname) == name:
                        found.append(fn)
    return found if seen_class else None


@dataclass
class _Imports:
    #: local name -> (module, level, original name)
    names: dict[str, tuple[str, int, str]]
    #: local name -> (module, level) for ``import a.b [as c]`` / ``from a import b`` of a module
    modules: dict[str, list[tuple[str, int, str | None]]]


def _imports(root: Node) -> _Imports:
    names: dict[str, tuple[str, int, str]] = {}
    modules: dict[str, list[tuple[str, int, str | None]]] = {}
    for node in _walk(root):
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "aliased_import":
                    mod = child.child_by_field_name("name")
                    alias = child.child_by_field_name("alias")
                    if mod is not None and alias is not None:
                        modules.setdefault(_text(alias), []).append((_text(mod), 0, None))
                elif child.type == "dotted_name":
                    dotted = _text(child)
                    modules.setdefault(dotted.split(".")[0], []).append((dotted.split(".")[0], 0, None))
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is None:
                continue
            text = _text(module)
            level = len(text) - len(text.lstrip("."))
            dotted = text.lstrip(".")
            for child in node.named_children:
                if child == module:
                    continue
                if child.type == "aliased_import":
                    orig = child.child_by_field_name("name")
                    alias = child.child_by_field_name("alias")
                    if orig is not None and alias is not None:
                        names[_text(alias)] = (dotted, level, _text(orig))
                        modules.setdefault(_text(alias), []).append((dotted, level, _text(orig)))
                elif child.type == "dotted_name":
                    local = _text(child)
                    names[local] = (dotted, level, local)
                    modules.setdefault(local, []).append((dotted, level, local))
    return _Imports(names, modules)


def _enclosing_class(node: Node) -> str | None:
    cur = node.parent
    while cur is not None:
        if cur.type == "class_definition":
            nm = cur.child_by_field_name("name")
            return _text(nm) if nm is not None else None
        cur = cur.parent
    return None


def _directly_awaited(call: Node) -> bool:
    cur = call.parent
    while cur is not None and cur.type == "parenthesized_expression":
        cur = cur.parent
    return cur is not None and cur.type == "await"


def _key_argument(call: Node, index: int) -> Node | None | bool:
    """The argument at ``index`` (``False`` when a keyword or splat may hold it, ``None`` when absent)."""
    args = call.child_by_field_name("arguments")
    if args is None or args.type != "argument_list":
        return False
    positional = [a for a in args.named_children if a.type not in ("keyword_argument", "comment")]
    if any(a.type in ("list_splat", "dictionary_splat") for a in positional):
        return False
    return positional[index] if index < len(positional) else None


def _string_value(node: Node) -> str | None:
    if node.type != "string":
        return None
    content = [c for c in node.named_children if c.type == "string_content"]
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    return "".join(_text(c) for c in content)


def _is_read_of(call: Node, flag_key: str, index: int) -> bool:
    arg = _key_argument(call, index)
    if arg is None:
        return True
    if arg is False:
        return True
    value = _string_value(arg)
    if value is None:
        # A name, an f-string, a call: the key may be this flag's.
        return arg.type != "string"
    return value == flag_key


def _kinds(defs: list[Node]) -> tuple[bool, bool]:
    return any(_is_async(d) for d in defs), any(not _is_async(d) for d in defs)


def _decide(repo: _Repo, path: str, root: Node, imports: _Imports, call: Node, name: str) -> str | None:
    """Why this un-awaited call may be a coroutine, or ``None`` when it provably is not."""
    callee = call.child_by_field_name("function")
    if callee is None:
        return None
    own_async = any(_is_async(d) for d in _definitions(root, name))

    def imported_async() -> str | None:
        for local, (module, level, orig) in imports.names.items():
            if orig == name or local == name:
                for file in repo.module_files(module, path, level):
                    tree = repo.tree(file)
                    if tree is not None and any(_is_async(d) for d in _definitions(tree, orig)):
                        return f"{name} is imported from {Path(file).name}, where it is an async def"
        for local, entries in imports.modules.items():
            for module, level, orig in entries:
                for file in repo.module_files(module if orig is None else module, path, level):
                    tree = repo.tree(file)
                    if tree is not None and any(_is_async(d) for d in _definitions(tree, name)):
                        return f"{Path(file).name}, which this file imports, defines {name} as an async def"
        return None

    if callee.type == "identifier":
        local = _text(callee)
        defs = _top_level(root, local) or _definitions(root, local)
        if defs:
            is_async, _ = _kinds(defs)
            return f"{local} is an async def in this file" if is_async else None
        if local in imports.names:
            module, level, orig = imports.names[local]
            files = repo.module_files(module, path, level)
            targets = [d for f in files if (t := repo.tree(f)) is not None for d in _top_level(t, orig)]
            if targets:
                is_async, _ = _kinds(targets)
                return f"{orig} is an async def in {module or '.'}" if is_async else None
            if files:
                # The module is the repo's and does not define it at the top level: a re-export.
                for f in files:
                    t = repo.tree(f)
                    if t is not None and any(_is_async(d) for d in _definitions(t, orig)):
                        return f"{orig} is an async def in {module or '.'}"
            return None
        return None

    if callee.type == "attribute":
        receiver = callee.child_by_field_name("object")
        member = callee.child_by_field_name("attribute")
        if receiver is None or member is None:
            return None
        member_name = _text(member)
        if member_name != name:
            return None
        if receiver.type == "identifier":
            rname = _text(receiver)
            if rname in ("self", "cls"):
                cls = _enclosing_class(call)
                if cls is not None:
                    methods = _class_methods(root, cls, name)
                    if methods:
                        is_async, _ = _kinds(methods)
                        return f"{cls}.{name} is an async method" if is_async else None
            elif methods := _class_methods(root, rname, name):
                is_async, _ = _kinds(methods)
                return f"{rname}.{name} is an async method" if is_async else None
            for module, level, orig in imports.modules.get(rname, []):
                dotted = module if orig is None else (f"{module}.{orig}" if module else orig)
                files = repo.module_files(dotted, path, level)
                for f in files:
                    t = repo.tree(f)
                    if t is not None and (found := _top_level(t, name)):
                        if _kinds(found)[0]:
                            return f"{rname}.{name} is an async def in {Path(f).name}"
                        return None
                if orig is not None:
                    # `from m import Cls`: a class of that module
                    for f in repo.module_files(module, path, level):
                        t = repo.tree(f)
                        if t is not None and (methods := _class_methods(t, orig, name)):
                            return f"{orig}.{name} is an async method in {Path(f).name}" if _kinds(methods)[0] else None
                if files:
                    return None
        # Unresolved receiver: the module's own declarations decide.
        if own_async:
            return f"this file defines {name} as an async def and the receiver cannot be resolved"
        if (why := imported_async()) is not None:
            return f"{why}, and the receiver cannot be resolved"
        return None
    return None


def plan(
    repo_dir: str,
    paths: list[str],
    flag_key: str,
    accessors: tuple[str, ...],
    key_positions: dict[str, int | str] | None = None,
) -> list[Site]:
    """Every un-awaited read of the flag through an accessor that is, or may be, an ``async def``."""
    positions = {k: v for k, v in (key_positions or {}).items() if isinstance(v, int)}
    names = frozenset(a for a in accessors)
    if not names:
        return []
    repo = _Repo(repo_dir, names)
    sites: list[Site] = []
    for path in dict.fromkeys(paths):
        if not path.endswith(".py"):
            continue
        source = wrapper_python._read(path)
        if source is None or not any(n in source for n in names):
            continue
        root = repo.tree(path)
        if root is None:
            continue
        imports = _imports(root)
        for node in _walk(root):
            if node.type != "call":
                continue
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            tail = callee if callee.type == "identifier" else callee.child_by_field_name("attribute")
            if tail is None or tail.type != "identifier" or _text(tail) not in names:
                continue
            name = _text(tail)
            if not _is_read_of(node, flag_key, positions.get(name, 0)) or _directly_awaited(node):
                continue
            why = _decide(repo, path, root, imports, node, name)
            if why is not None:
                sites.append(Site(path, node.start_point[0] + 1, name, why))
    return sites


def refusal(sites: list[Site]) -> tuple[str, list[str]] | None:
    """The reason to refuse the whole flag, and the files, if any read must be left."""
    if not sites:
        return None
    first = sites[0]
    count = f"({len(sites)} such read{'s' if len(sites) != 1 else ''})"
    reason = (
        f"an un-awaited read of {first.accessor} (line {first.line} of {Path(first.path).name}) "
        f"whose result may be a coroutine, which is always truthy: {first.why}; "
        f"it is an async def, so only an awaited read can be removed {count}"
    )
    return reason, sorted({s.path for s in sites})
