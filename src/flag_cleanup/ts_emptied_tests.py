"""Delete the suites and test files a removal left with no test.

A removal deletes a test that pinned the removed value, or that tested only a
removed wrapper member. When that was a ``describe``'s last test, what stays is
``describe("x", () => {})`` (with, perhaps, its hooks), which Vitest and Jest
both report as a failure ("No test found in suite"). When it was a file's last
test, what stays is imports and mocks: "No test suite found in file".

Only a suite that held a test before this run goes, and only one whose
remaining statements cannot matter once it holds no test: hooks, comments and
declarations. Any other statement (a ``vi.mock``, which applies to the whole
file wherever it is written) keeps it. Only a plain ``describe``/``suite`` is
removed; a ``describe.each`` is left alone. A file goes when no test is left
in it and it exports nothing, so nothing else can depend on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import file_deletion, mock_stubs, ts_scope, ts_syntax, ts_tests, unused

LANGUAGES = frozenset({"ts", "tsx", "js"})
#: Kind of the report row.
EMPTIED = mock_stubs.EMPTIED_SUITE
_PLAIN_SUITE = re.compile(r"^(?:describe|suite)(?:\.(?:skip|only|concurrent|sequential))*$")
_ANY_SUITE = re.compile(r"^(?:describe|suite)\b")
_HOOKS = frozenset({"beforeEach", "afterEach", "beforeAll", "afterAll"})
#: Names that register a test or a suite, in any member form (`it.each(x)(...)`,
#: `describe.skip`, `test.todo`).
_REGISTERS = frozenset(
    {"it", "test", "describe", "suite", "fit", "xit", "fdescribe", "xdescribe", "ftest", "xtest", "bench", "specify", "context"}
)
_MOCKS = frozenset({"vi.mock", "vi.hoisted", "jest.mock", "vi.doMock", "jest.doMock"})


def _kind(callee: str) -> str:
    text = "".join(callee.split())
    if _PLAIN_SUITE.match(text):
        return "suite"
    if _ANY_SUITE.match(text):
        return "other-suite"
    return "test"


def _entries(source: str, language: str) -> list[tuple[Node, str, str]]:
    data = source.encode("utf-8")
    root = ts_syntax._parse(source, language)
    return [(n, _kind(c), t) for n, c, t in ts_tests._titled_tests(root, data, parametrised=True)]


def _chain(node: Node, byid: dict[int, str]) -> tuple[str, ...]:
    """The titles of the suites around ``node`` and its own, outermost first."""
    titles = []
    current: Node | None = node
    while current is not None:
        if current.id in byid:
            titles.append(byid[current.id])
        current = current.parent
    return tuple(reversed(titles))


def _body(call: Node) -> Node | None:
    arguments = call.child_by_field_name("arguments")
    if arguments is None or len(arguments.named_children) < 2:
        return None
    callback = arguments.named_children[1]
    if callback.type not in ("arrow_function", "function_expression", "function"):
        return None
    body = callback.child_by_field_name("body")
    return body if body is not None and body.type == "statement_block" else None


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _root_name(callee: Node | None) -> str | None:
    """The identifier a callee chain starts from: ``it`` for
    ``it.each(rows)("t", cb)``."""
    while callee is not None:
        if callee.type == "identifier":
            return callee.text.decode("utf-8", "replace")
        if callee.type == "member_expression":
            callee = callee.child_by_field_name("object")
        elif callee.type == "call_expression":
            callee = callee.child_by_field_name("function")
        elif callee.type in ("parenthesized_expression", "non_null_expression", "as_expression"):
            callee = callee.named_children[0] if callee.named_children else None
        else:
            return None
    return None


def _registers(node: Node) -> bool:
    """Whether anything under ``node`` may register a test or a suite: a call
    whose chain starts from ``it``, ``test``, ``describe``..., in any form."""
    return any(
        n.type == "call_expression" and _root_name(n.child_by_field_name("function")) in _REGISTERS for n in _walk(node)
    )


def _callee_text(call: Node) -> str:
    function = call.child_by_field_name("function")
    return "" if function is None else "".join(function.text.decode("utf-8", "replace").split())


def _residue(statement: Node, top: bool = False) -> bool:
    """Whether a statement is one a file or suite may keep once no test is left
    in it, and nothing else: an import, a module mock, a hook that registers
    no test, a declaration whose initializer calls and constructs nothing, a
    comment or an empty statement. Anything else (any other call, a loop, a
    function, a test or suite in any form) may be, or build, a test."""
    if statement.type in ("import_statement", "comment", "empty_statement"):
        return True
    if statement.type in ("lexical_declaration", "variable_declaration"):
        hoisted = [] if not top else [
            d.child_by_field_name("value")
            for d in statement.named_children
            if d.type == "variable_declarator"
            and d.child_by_field_name("value") is not None
            and d.child_by_field_name("value").type == "call_expression"
            and _callee_text(d.child_by_field_name("value")) in ("vi.hoisted", "jest.hoisted")
        ]
        if hoisted and len(hoisted) == len(statement.named_children):
            return not any(_registers(h) for h in hoisted)  # `const mocks = vi.hoisted(...)`
        return not any(
            n.type in ("call_expression", "new_expression", "tagged_template_expression") for n in _walk(statement)
        )
    call = statement.named_children[0] if statement.type == "expression_statement" and statement.named_children else None
    if call is None or call.type != "call_expression":
        return False
    function = call.child_by_field_name("function")
    callee = "".join(function.text.decode("utf-8", "replace").split()) if function is not None else ""
    # A module mock applies to the whole file wherever it is written, so inside
    # a suite it is not residue: cutting the suite would unmock its siblings.
    if (top and callee in _MOCKS) or (function is not None and function.type == "identifier" and callee in _HOOKS):
        return not _registers(call)
    return False


def _only_residue(statements, data: bytes, top: bool = False) -> bool:
    return all(_residue(s, top) for s in statements)


def _only_dead_weight(body: Node, data: bytes) -> bool:
    """Whether a suite body holds nothing but residue."""
    return _only_residue(body.named_children, data)


def _held_tests(source: str, language: str) -> dict[tuple[str, ...], int]:
    """Chain of each suite that holds a test (at any depth) -> how many suites
    of that chain there are."""
    entries = _entries(source, language)
    byid = {n.id: t for n, k, t in entries if k != "test"}
    counts: dict[tuple[str, ...], int] = {}
    for node, kind, _ in entries:
        if kind == "test":
            continue
        counts[_chain(node, byid)] = counts.get(_chain(node, byid), 0) + 1
    holding: dict[tuple[str, ...], int] = {}
    for node, kind, _ in entries:
        if kind != "test":
            continue
        chain = _chain(node, byid)
        for size in range(1, len(chain) + 1):
            holding[chain[:size]] = counts.get(chain[:size], 0)
    return holding


def _any_test(source: str, language: str) -> bool:
    """Whether a test is declared in any form this module reads."""
    return any(kind == "test" for _, kind, _ in _entries(source, language))


def _without(source: str, language: str, before: dict[tuple[str, ...], int]) -> tuple[str, list[str]]:
    """``source`` with every emptied plain suite cut, innermost first."""
    emptied: list[str] = []
    while True:
        data = source.encode("utf-8")
        entries = _entries(source, language)
        byid = {n.id: t for n, k, t in entries if k != "test"}
        children: dict[int, int] = {}
        for node, kind, _ in entries:
            parent = node.parent
            while parent is not None and parent.id not in byid:
                parent = parent.parent
            if parent is not None:
                children[parent.id] = children.get(parent.id, 0) + 1
        found = None
        for node, kind, title in entries:
            if kind != "suite" or children.get(node.id, 0):
                continue
            chain = _chain(node, byid)
            if before.get(chain) != 1:
                continue  # it held no test before, or its chain is not unique
            body = _body(node)
            if body is None or not _only_dead_weight(body, data):
                continue
            statement = node.parent
            if statement is None or statement.type != "expression_statement":
                continue
            found = (statement, title)
            break
        if found is None:
            return source, emptied
        statement, title = found
        start = mock_stubs._with_leading_comments(statement, data)
        source = unused._delete_method(source, unused.Candidate(title, start, statement.end_byte, "method", source[start : statement.end_byte]))
        emptied.append(title)


def _exports(source: str, language: str) -> bool:
    root = ts_syntax._parse(source, language)
    return any(
        c.type == "export_statement"
        or (c.type == "expression_statement" and re.match(r"(?:module\.)?exports\b", c.text.decode("utf-8", "replace")))
        for c in root.named_children
    )


def remove(
    summaries: list, language: str, repo_dir: str, rewrite: Callable
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """``summaries`` with each suite the run emptied cut, each file it left with
    no test deleted, and a row for each."""
    if language not in LANGUAGES:
        return summaries, ()
    result: list = []
    rows: list[tuple[str, str, str]] = []
    gone: list[str] = []
    for summary in summaries:
        if (
            file_deletion.deleted(summary)
            or summary.original_content == summary.content
            or not summary.path.endswith(ts_scope.FAMILY_EXTENSIONS)
        ):
            result.append(summary)
            continue
        grammar = ts_scope.grammar(summary.path)
        before_tests = _any_test(summary.original_content, grammar)
        if not before_tests:
            result.append(summary)
            continue
        resolved = Path(summary.path).resolve()
        relative = str(resolved.relative_to(Path(repo_dir).resolve()))
        text, emptied = _without(summary.content, grammar, _held_tests(summary.original_content, grammar))
        if _only_residue(ts_syntax._parse(text, grammar).named_children, b"", True) and not _exports(text, grammar):
            rows.append((relative, EMPTIED, "left with no test, so the file was deleted"))
            gone.append(summary.path)
            result.append(summary)
            continue
        if not emptied:
            result.append(summary)
            continue
        rows += [(relative, EMPTIED, f"{title}, left with no test") for title in emptied]
        Path(summary.path).write_text(text, encoding="utf-8")
        result.append(rewrite(summary.path, summary.original_content, text))
    return file_deletion.delete(result, gone, rewrite), tuple(rows)


def survives(source: str, language: str, member: str) -> bool:
    """Whether a test file stays valid once every test that mentions ``member``
    is deleted, by the rule :func:`remove` applies:

    * every suite holding such a test either keeps another test that
      registers, or is left with residue only (so it is cut whole); a suite
      that would be left with no test and something else (``const s = setup()``,
      a ``vi.mock``) stays empty, which Vitest and Jest fail;
    * the file keeps a test that registers, or is all residue (and exports
      nothing), so it is deleted whole.

    A test registers when its call is an expression statement of the file's
    top level or of the body of a suite that registers; one in a hook, a
    function or an uncalled declaration may never run, so it does not count."""
    data = source.encode("utf-8")
    root = ts_syntax._parse(source, language)
    entries = {n.id: (n, _kind(c)) for n, c, _ in ts_tests._titled_tests(root, data, parametrised=True)}
    word = re.compile(rf"(?<![\w$]){re.escape(member)}(?![\w$])")
    candidates = {i for i, (n, k) in entries.items() if k == "test" and word.search(n.text.decode("utf-8", "replace"))}

    def suite_of(call: Node):
        """``"top"``, the enclosing suite call, or ``None`` when the call is
        not a direct statement of a top level or a suite body."""
        statement = call.parent
        if statement is None or statement.type != "expression_statement":
            return None
        block = statement.parent
        if block is not None and block.type == "program":
            return "top"
        function = None if block is None or block.type != "statement_block" else block.parent
        arguments = None if function is None else function.parent
        owner = None if arguments is None or arguments.type != "arguments" else arguments.parent
        if owner is not None and owner.id in entries and entries[owner.id][1] != "test" and _body(owner) == block:
            return owner
        return None

    def registering(call: Node) -> bool:
        where = suite_of(call)
        while where is not None and where != "top":
            call, where = where, suite_of(where)
        return where == "top"

    def survivors(statements) -> bool:
        for statement in statements:
            call = statement.named_children[0] if statement.type == "expression_statement" and statement.named_children else None
            if call is None or call.id not in entries:
                continue
            kind = entries[call.id][1]
            if kind == "test" and call.id not in candidates:
                return True
            body = _body(call) if kind != "test" else None
            if body is not None and survivors(body.named_children):
                return True
        return False

    def deletable(statements, top: bool = False) -> bool:
        for statement in statements:
            if _residue(statement, top):
                continue
            call = statement.named_children[0] if statement.type == "expression_statement" and statement.named_children else None
            if call is None or call.type != "call_expression" or call.id not in entries:
                return False
            kind = entries[call.id][1]
            if kind == "test" and call.id in candidates:
                continue
            body = _body(call) if kind == "suite" else None
            if body is None or not deletable(body.named_children):
                return False
        return True

    # Every suite holding a candidate.
    holding: dict[int, Node] = {}
    for i in candidates:
        where = suite_of(entries[i][0])
        while where is not None and where != "top":
            holding[where.id] = where
            where = suite_of(where)
    for suite in holding.values():
        body = _body(suite)
        if body is None or not (survivors(body.named_children) or (entries[suite.id][1] == "suite" and deletable(body.named_children))):
            return False
    # The file.
    if any(registering(n) and i not in candidates for i, (n, k) in entries.items() if k == "test"):
        return True
    if _exports(source, language):
        return False
    return deletable(root.named_children, True)
