"""A TS/JS accessor that returns an OBJECT is not a boolean read, wherever it is read.

The structural check in :mod:`flag_cleanup.syntax` sees a folded literal only
where it is consumed in the same expression (``const { value } = true``). It
cannot see a read whose member access happens later, as in::

    const { result } = renderHook(() => useBooleanFlag('k'));
    expect(result.current.value).toBe(true);

which folds to ``renderHook(() => false)`` and fails when the test runs. This
module decides the question from the other end, BEFORE the engine runs:

* an accessor whose in-repo declaration PROVABLY returns a non-boolean (an
  explicit object, array or tuple return type, or a body that returns only
  object/array literals or a call of such a hook) is refused at every read of
  the flag's key. Nothing is guessed: no declaration found (a package), a
  type alias, an ``async`` function, or declarations that disagree all mean
  "unknown", which keeps today's behaviour and relies on the structural check;
* a read returned from a ``renderHook`` callback is refused whenever the same
  file reads a member of ``.current``, whatever the declaration says.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index, ts_syntax

_SUFFIXES = (".ts", ".mts", ".cts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_WRAPPERS = frozenset(
    {"parenthesized_expression", "as_expression", "non_null_expression", "satisfies_expression", "type_assertion"}
)
_FUNCTIONS = frozenset(
    {"function_declaration", "function_expression", "arrow_function", "function_signature", "method_definition", "generator_function_declaration"}
)
_OBJECT_TYPES = frozenset({"object_type", "array_type", "tuple_type"})
_NOTHING_TYPES = frozenset({"undefined", "null"})
_MAX_DEPTH = 3


def language_of(path: str) -> str:
    suffix = Path(path).suffix
    return "tsx" if suffix == ".tsx" else "ts" if suffix in (".ts", ".mts", ".cts") else "js"


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _unwrap(node: Node) -> Node:
    while node.type in _WRAPPERS and node.named_children:
        node = node.named_children[0]
    return node


def _callee_name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    if function is None:
        return None
    function = _unwrap(function)
    if function.type == "identifier":
        return _text(function)
    if function.type == "member_expression":
        prop = function.child_by_field_name("property")
        return _text(prop) if prop is not None else None
    return None


def _reads_key(call: Node, flag_key: str) -> bool:
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return False
    for argument in arguments.named_children:
        argument = _unwrap(argument)
        if argument.type == "string" and _text(argument)[1:-1] == flag_key:
            return True
        if argument.type == "template_string" and _text(argument)[1:-1] == flag_key:
            return True
    return False


def _returned(function: Node) -> list[Node] | None:
    """The expressions ``function`` returns; ``None`` when it cannot be said."""
    body = function.child_by_field_name("body")
    if body is None:
        return None
    if body.type != "statement_block":
        return [body]
    found: list[Node] = []
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type in _FUNCTIONS or node.type in ("class_declaration", "class"):
            continue
        if node.type == "return_statement":
            if not node.named_children:
                return None
            found.append(node.named_children[0])
            continue
        stack.extend(node.children)
    return found or None


def _type_verdict(annotation: Node) -> bool | None:
    """``True`` object-like, ``False`` known otherwise, ``None`` unknown."""
    inner = [c for c in annotation.named_children]
    if len(inner) != 1:
        return None
    node = inner[0]
    if node.type == "union_type":
        members = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "union_type":
                stack.extend(current.named_children)
            else:
                members.append(current)
        kinds = [m for m in members if _text(m) not in _NOTHING_TYPES]
        return True if kinds and all(m.type in _OBJECT_TYPES for m in kinds) else None
    if node.type in _OBJECT_TYPES:
        return True
    if node.type == "generic_type":
        name = node.child_by_field_name("name")
        if name is not None and _text(name) in ("Array", "ReadonlyArray"):
            return True
    return None


class _Index:
    """The in-repo declarations of accessor names, scanned once per name."""

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir
        self._found: dict[str, list[tuple[str, Node]]] = {}
        self._trees: list[Node] = []  # keeps the parsed trees alive for the nodes held

    def declarations(self, name: str) -> list[tuple[str, Node]]:
        if name in self._found:
            return self._found[name]
        found: list[tuple[str, Node]] = self._found.setdefault(name, [])
        for listed in source_index.files(self.repo_dir, _SUFFIXES):
            location = listed.path if listed.path.is_absolute() else Path(self.repo_dir) / listed.path
            try:
                source = location.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if name not in source:
                continue
            root = ts_syntax._parse(source, language_of(str(listed.path)))
            self._trees.append(root)
            for node in ts_syntax._walk(root):
                declared = _declared(node)
                if declared is not None and declared[0] == name:
                    found.append((str(listed.path), declared[1]))
        return found


def _declared(node: Node) -> tuple[str, Node] | None:
    """``(name, function node)`` when ``node`` declares a named function."""
    if node.type in ("function_declaration", "function_signature", "generator_function_declaration"):
        name = node.child_by_field_name("name")
        return (_text(name), node) if name is not None else None
    if node.type == "method_definition":
        name = node.child_by_field_name("name")
        return (_text(name), node) if name is not None else None
    if node.type == "variable_declarator":
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if name is not None and value is not None and name.type == "identifier":
            value = _unwrap(value)
            if value.type in ("arrow_function", "function_expression"):
                return _text(name), value
    return None


def _is_async(function: Node) -> bool:
    return any(c.type == "async" for c in function.children)


def _function_is_object(function: Node, index: _Index, depth: int) -> bool:
    if _is_async(function):
        return False
    annotation = function.child_by_field_name("return_type")
    if annotation is not None:
        return _type_verdict(annotation) is True
    returned = _returned(function)
    if not returned:
        return False
    for value in returned:
        value = _unwrap(value)
        if value.type in ("object", "array"):
            continue
        if value.type == "call_expression" and depth < _MAX_DEPTH:
            callee = _callee_name(value)
            if callee is not None and _name_is_object(callee, index, depth + 1):
                continue
        return False
    return True


def _name_is_object(name: str, index: _Index, depth: int = 0) -> bool:
    declarations = index.declarations(name)
    return bool(declarations) and all(
        _function_is_object(function, index, depth) for _, function in declarations
    )


def _current_member_reads(root: Node) -> bool:
    """Whether the file reads a member of, or destructures, ``<x>.current``."""
    for node in ts_syntax._walk(root):
        if node.type != "member_expression":
            continue
        prop = node.child_by_field_name("property")
        if prop is None or _text(prop) != "current":
            continue
        parent = node.parent
        while parent is not None and parent.type in _WRAPPERS:
            node, parent = parent, parent.parent
        if parent is None:
            continue
        if parent.type in ("member_expression", "subscript_expression"):
            held = parent.child_by_field_name("object")
            if held is not None and held.id == node.id:
                return True
        if parent.type == "variable_declarator":
            pattern = parent.child_by_field_name("name")
            if pattern is not None and pattern.type in ("object_pattern", "array_pattern"):
                return True
    return False


def _hook_callback_reads(root: Node, flag_key: str, accessors: frozenset[str]) -> bool:
    for node in ts_syntax._walk(root):
        if node.type != "call_expression" or _callee_name(node) != "renderHook":
            continue
        arguments = node.child_by_field_name("arguments")
        callbacks = [a for a in (arguments.named_children if arguments else []) if _unwrap(a).type in ("arrow_function", "function_expression")]
        if not callbacks:
            continue
        returned = _returned(_unwrap(callbacks[0])) or []
        for value in returned:
            value = _unwrap(value)
            if value.type == "call_expression" and _callee_name(value) in accessors and _reads_key(value, flag_key):
                return True
    return False


def refusals(
    paths: list[str], flag_key: str, accessors: tuple[str, ...], repo_dir: str
) -> dict[str, str]:
    """``{path: reason}`` for every file holding a read that is an object read."""
    names = frozenset(a.split("[", 1)[0] for a in accessors)
    if not names:
        return {}
    index = _Index(repo_dir)
    verdicts: dict[str, bool] = {}
    declared_in: dict[str, str] = {}
    found: dict[str, str] = {}
    for path in paths:
        if not path.endswith(_SUFFIXES):
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if flag_key not in source or not any(n in source for n in names):
            continue
        root = ts_syntax._parse(source, language_of(path))
        if _hook_callback_reads(root, flag_key, names) and _current_member_reads(root):
            found[path] = (
                "the read is the return value of a renderHook callback and the test reads a "
                "member of `.current`, so the hook returns an object, not a boolean"
            )
            continue
        for node in ts_syntax._walk(root):
            if node.type != "call_expression":
                continue
            name = _callee_name(node)
            if name not in names or not _reads_key(node, flag_key):
                continue
            if name not in verdicts:
                verdicts[name] = _name_is_object(name, index)
                declared_in[name] = ", ".join(sorted({p for p, _ in index.declarations(name)}))
            if verdicts[name]:
                found[path] = (
                    f"`{name}` is declared in {declared_in[name]} as returning an object or "
                    "array, not a boolean, so its read is not a boolean read"
                )
                break
    return found
