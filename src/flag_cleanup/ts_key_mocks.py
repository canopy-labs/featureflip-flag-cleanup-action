"""A test that replaces the module declaring a TypeScript/JavaScript key constant.

``vi.mock("./keys", () => ({ K: "other" }))`` hands every read of ``K`` under
that test another value, so a read the run folds as this flag's is not the
read the test exercises: after the removal the test runs another path,
silently. Such a flag is refused, naming the call.

A module mock counts when it can change the module's exports: a factory or
object (``vi.mock``/``vi.doMock``/``jest.mock``/``jest.doMock``/
``jest.unstable_mockModule`` with a second argument, ``jest.setMock``), or a
mock with none that loads a manual mock (a ``__mocks__`` file beside the
module). An automock keeps a string export's value. The module is the one
declaring a key constant of the run, or any module re-exporting from one
(``export … from``, ``export *``), to a fixed point; a specifier the resolver
cannot read counts when its last segment names one of them, and one that is
not a literal always counts.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, cross_file_ts, mock_stubs, ts_scope, ts_syntax

#: The calls that replace a module, by how they are spelled.
MOCK_CALLS = frozenset(
    {"vi.mock", "vi.doMock", "jest.mock", "jest.doMock", "jest.unstable_mockModule", "jest.setMock"}
)
_SPELLED = re.compile(r"\b(?:mock|doMock|unstable_mockModule|setMock)\s*\(")
_PLAIN_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _stem(module: str) -> str:
    path = Path(module)
    stem = re.sub(r"(?:\.d)?\.[cm]?[jt]sx?$", "", path.name)
    return path.parent.name if stem == "index" else stem


def _reexporters(modules: set[str], repo_dir: str) -> set[str]:
    """Every module that re-exports from one of ``modules`` or from another
    such module, by any ``export … from``. Over-approximate by design."""
    stop = cross_file_ts._stop_for(repo_dir)
    candidates = [
        p for p in cross_file_keys.mention_files(repo_dir, (), exclude=(), also=re.compile(r"\bexport\b[^;]*\bfrom\b"))
        if p.endswith(_PLAIN_SUFFIXES)
    ]
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for candidate in candidates:
            resolved = str(Path(candidate).resolve())
            if resolved in modules or resolved in found:
                continue
            try:
                text = Path(candidate).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            root = ts_syntax._parse(text, cross_file_ts._language(candidate))
            for statement in root.named_children:
                if statement.type != "export_statement":
                    continue
                spec = cross_file_ts._string_value(statement.child_by_field_name("source"))
                target = cross_file_ts._resolve(candidate, spec, stop, workspace=True) if spec is not None else None
                if target is not None and str(Path(target).resolve()) in modules | found:
                    found.add(resolved)
                    changed = True
                    break
    return found


def refusals(repo_dir: str, declaring: set[str]) -> dict[str, str]:
    """``{path: reason}`` for each file holding a module mock that can replace
    one of ``declaring`` (resolved paths) or a module re-exporting from it."""
    if not declaring:
        return {}
    modules = set(declaring) | _reexporters(set(declaring), repo_dir)
    stems = {_stem(m) for m in modules}
    found: dict[str, str] = {}
    for path in cross_file_keys.mention_files(repo_dir, (), exclude=(), also=_SPELLED):
        if not path.endswith(ts_scope.FAMILY_EXTENSIONS):
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        root = ts_syntax._parse(source, cross_file_ts._language(path))
        for node in cross_file_keys.walk(root):
            if node.type != "call_expression" or mock_stubs.ts_callee(node) not in MOCK_CALLS:
                continue
            why = _replaces(node, path, repo_dir, modules, stems)
            if why is not None:
                line = node.start_point[0] + 1
                found.setdefault(
                    path,
                    f"the module mock on line {line} ({source.splitlines()[line - 1].strip()}) {why}, so the "
                    "reads this removal folds are not the reads that test exercises",
                )
    return found


def _replaces(call: Node, path: str, repo_dir: str, modules: set[str], stems: set[str]) -> str | None:
    arguments = mock_stubs.ts_arguments(call)
    if not arguments:
        return None
    first = arguments[0]
    if first.type == "call_expression" and (first.child_by_field_name("function") or first).type == "import":
        inner = mock_stubs.ts_arguments(first)
        first = inner[0] if inner else first
    spec = cross_file_ts._string_value(first) if first.type in ("string", "template_string") else None
    if spec is None:
        return "names a module this cannot read, which may declare the flag's key constant"
    target = cross_file_ts.resolve_specifier(path, spec, repo_dir)
    if target is None:
        segment = re.sub(r"\.[cm]?[jt]sx?$", "", spec.rstrip("/").rsplit("/", 1)[-1])
        if segment not in stems:
            return None
        module = None
    else:
        module = str(Path(target).resolve())
        if module not in modules:
            return None
    supplies = len(arguments) >= 2 or mock_stubs.ts_callee(call) == "jest.setMock"
    if not supplies:
        if module is None or not _manual_mock(module):
            return None
        return "loads a manual mock of a module declaring the flag's key constant"
    return "replaces the exports of a module declaring the flag's key constant (or re-exporting it)"


def _manual_mock(module: str) -> bool:
    """Whether a ``__mocks__`` file beside ``module`` mocks it."""
    path = Path(module)
    stem = re.sub(r"(?:\.d)?\.[cm]?[jt]sx?$", "", path.name)
    folder = path.parent / "__mocks__"
    return folder.is_dir() and any(re.sub(r"\.[cm]?[jt]sx?$", "", p.name) == stem for p in folder.iterdir())
