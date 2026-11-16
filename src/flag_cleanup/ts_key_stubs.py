"""A test double of a flag accessor that answers by key refuses the flag.

``jest.mock('./flags', () => ({ isEnabled: (name) => mock(name) }))`` followed by
``mock.mockImplementation((n) => n === 'flag')`` names the flag in a predicate.
The fold of the production read leaves that stub and its test alone, so the
tests run a branch that no longer exists and fail, and no other pass sees it.

Resolving such a stub (evaluating the predicate, deleting the tests it
describes, rewriting what stays) needs the order jest runs hooks and tests in,
a model of its config, and every way a test file can spell a test, and each
review round found another shape that left a failing test behind. So this
module does not resolve anything. A test file is a CANDIDATE when it spells the
key and mocks a module the accessor comes from; a candidate that holds any stub
call on a mock, or the key inside a module-mock factory, refuses the flag with
the file and line. The tests need that flag taken out by hand first.

A file that stubs the accessor with ``vi.mocked(f)`` or ``jest.spyOn(m, 'f')``
and spells the key refuses the same way, candidate or not.
"""

from __future__ import annotations

import os

from tree_sitter import Node

from flag_cleanup import mock_stubs

#: Calls that set what a mock answers.
_STUBS = frozenset(
    {
        "mockImplementation",
        "mockImplementationOnce",
        "mockReturnValue",
        "mockReturnValueOnce",
        "mockResolvedValue",
        "mockResolvedValueOnce",
        "mockRejectedValue",
        "mockRejectedValueOnce",
        "mockReturnThis",
    }
)
_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _string(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return mock_stubs._plain_string(node)
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.children):
        return _text(node)[1:-1]
    return None


def _spells(node: Node, key: str) -> bool:
    if node.type == "string":
        return _string(node) == key
    return node.type == "template_string" and key in _text(node)


def _spelled_nodes(root: Node, key: str) -> list[Node]:
    return [n for n in _walk(root) if n.type in ("string", "template_string") and _spells(n, key)]


def _module_mocks(root: Node) -> list[tuple[Node, str | None]]:
    found = []
    for node in _walk(root):
        if node.type == "call_expression" and mock_stubs.ts_callee(node) in mock_stubs.TS_MODULE_MOCKS:
            arguments = mock_stubs.ts_arguments(node)
            found.append((node, mock_stubs.ts_module_specifier(arguments[0]) if arguments else None))
    return found


def _accessor_imports(root: Node, accessors: tuple[str, ...]) -> set[str]:
    """The module specifiers the file imports an accessor from."""
    sources: set[str] = set()
    for node in _walk(root):
        if node.type != "import_statement":
            continue
        named = any(
            c.type == "identifier" and _text(c) in accessors
            for c in _walk(node)
            if c.parent is not None and c.parent.type == "import_specifier"
        )
        if named and (spec := _string(node.child_by_field_name("source"))) is not None:
            sources.add(spec)
    return sources


def _resolves_to_accessor(spec: str, path: str | None, accessors: tuple[str, ...]) -> bool:
    """Whether a relative specifier names a file mentioning an accessor; one
    that cannot be found or read counts, since nothing says it does not."""
    if path is None or not spec.startswith("."):
        return False
    base = os.path.normpath(os.path.join(os.path.dirname(path), spec))
    for candidate in (
        base,
        *(base + suffix for suffix in _SUFFIXES),
        *(os.path.join(base, "index" + suffix) for suffix in _SUFFIXES),
    ):
        if os.path.isfile(candidate):
            try:
                text = open(candidate, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                return True
            return any(name in text for name in accessors)
    return True


def _is_candidate(root: Node, accessors: tuple[str, ...], key: str, path: str | None) -> bool:
    if not _spelled_nodes(root, key):
        return False
    imported = _accessor_imports(root, accessors)
    for _, spec in _module_mocks(root):
        if spec is None:
            if imported:
                return True
        elif spec in imported or _resolves_to_accessor(spec, path, accessors):
            return True
    return False


def _stubs_of_the_accessor(root: Node, accessors: tuple[str, ...], key: str) -> list[str]:
    """`vi.mocked(f)` / `jest.mocked(f)` and `spyOn(m, 'f')` of an accessor in a
    file that spells the key: a stub, whichever module mock stands behind it."""
    if not _spelled_nodes(root, key):
        return []
    problems = []
    for node in _walk(root):
        if node.type not in ("identifier", "property_identifier", "string"):
            continue
        name = _string(node) if node.type == "string" else _text(node)
        if name not in accessors:
            continue
        arguments = node.parent
        call = arguments.parent if arguments is not None and arguments.type == "arguments" else None
        if call is None or call.type != "call_expression":
            continue
        callee = "".join(_text(call.child_by_field_name("function")).split())
        position = [c.id for c in arguments.named_children if c.type != "comment"].index(node.id) if node.id in [
            c.id for c in arguments.named_children
        ] else -1
        if (node.type != "string" and callee in ("vi.mocked", "jest.mocked", "mocked")) or (
            node.type == "string" and callee.endswith("spyOn") and position == 1
        ):
            problems.append(f"line {_line(node)}: `{name}` is stubbed")
    return problems


def refusals(
    source: str, language: str, flag_key: str, accessors: tuple[str, ...], path: str | None = None
) -> list[str]:
    """Where ``source`` answers the accessor by key, as ``line N: <what>``
    strings; empty when the file is not about the flag or stubs nothing."""
    if language not in ("ts", "tsx", "js") or flag_key not in source:
        return []
    root = mock_stubs._PROFILES[language].parse(source, language)
    problems = _stubs_of_the_accessor(root, accessors, flag_key)
    if not _is_candidate(root, accessors, flag_key, path):
        return problems
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is not None and callee.type == "member_expression":
            method = callee.child_by_field_name("property")
            if method is not None and _text(method) in _STUBS:
                problems.append(f"line {_line(node)}: a stub (`{_text(method)}`) in a file that mocks the accessor")
    for call, _ in _module_mocks(root):
        for node in _spelled_nodes(call, flag_key):
            problems.append(f"line {_line(node)}: the key is spelled inside a module mock of the accessor")
    return problems
