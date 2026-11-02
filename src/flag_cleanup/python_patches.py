"""Resolve the ``unittest.mock`` patches of Python keyless wrappers.

A Python stub is not a statement but a decorator, or a ``with`` around one, so
it cannot be read the way :mod:`flag_cleanup.mock_stubs` reads a line. What is
decided is the same: a patch returning the kept value goes and its test stays,
a test patching the removed value goes whole.

    @patch("app.svc.is_purge_enabled", return_value=True)
    def test_purges(self, mock_flag): ...

**The hazard is the parameter.** A decorator hands its mock over by position,
bottom-up, after ``self``, so deleting one without its parameter is a
``TypeError`` no gate sees. The parameter is found by counting, and a decorator
this module cannot read may be injecting too, so it leaves the count unproven.
A mock read only by its own assertions (``mock_flag.assert_called_once()``)
takes them with it; read any other way, the patch stays.

A patch is left, and the wrapper with it, when its target is not one the
wrapper pass proved, when ``return_value`` is not a literal or ``side_effect``
is set, when the test patches both values, and outside a test. A test holding
a patch of the wrapper that cannot be read is left whole.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from tree_sitter import Node

from flag_cleanup import mock_stubs, syntax, unused
from flag_cleanup.mock_stubs import STUB, TEST, VERIFICATION, Removal

#: The calls that patch by a dotted string.
_PATCHES = frozenset({"patch", "mock.patch", "unittest.mock.patch"})
_OBJECT_PATCHES = frozenset({"patch.object", "mock.patch.object", "unittest.mock.patch.object"})
_DICT_PATCHES = frozenset({"patch.dict", "mock.patch.dict", "unittest.mock.patch.dict"})
_MOCKER_PATCHES = frozenset({"mocker.patch"})
#: Decorators that inject nothing.
_INERT_DECORATOR = re.compile(
    r"pytest\.mark\.\w+|(?:unittest\.)?skip\w*|override_settings|freeze_time|(?:unittest\.)?expectedFailure"
)
_LITERALS = {"true": True, "false": False, "none": False}
#: What a patch may say beside its answer without changing it.
_QUIET_KEYWORDS = frozenset({"return_value", "autospec"})


@dataclass
class _Patch:
    """A patch of a wrapper in one test."""

    #: The decorator, the ``with`` item, or the ``mocker.patch`` statement.
    node: Node
    #: The value it answers with; ``None`` when that cannot be read.
    value: bool | None


@dataclass
class _Test:
    node: Node
    name: str
    #: The test's whole statement, decorators included.
    outer: Node
    patches: list[_Patch] = field(default_factory=list)


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _callee(call: Node) -> str:
    return "".join(_text(call.child_by_field_name("function")).split())


def _string(node: Node | None) -> str | None:
    """A plain string literal's text."""
    if node is None or node.type != "string":
        return None
    parts = [c for c in node.named_children if c.type == "string_content"]
    return _text(parts[0]) if len(parts) == 1 else None


def _arguments(call: Node) -> tuple[list[Node], dict[str, Node]]:
    arguments = call.child_by_field_name("arguments")
    children = [] if arguments is None else [c for c in arguments.named_children if c.type != "comment"]
    keywords = {
        _text(c.child_by_field_name("name")): c.child_by_field_name("value")
        for c in children if c.type == "keyword_argument"
    }
    return [c for c in children if c.type != "keyword_argument"], keywords


def _value(call: Node) -> bool | None:
    """What a patch answers, when its call says so as a literal and nothing
    else can change it."""
    positional, keywords = _arguments(call)
    answer = keywords.get("return_value")
    if len(positional) != 1 or answer is None or answer.type not in _LITERALS or set(keywords) - _QUIET_KEYWORDS:
        return None
    return _LITERALS[answer.type]


def _test_of(node: Node, name: str) -> _Test:
    outer = node.parent if node.parent is not None and node.parent.type == "decorated_definition" else node
    return _Test(node, name, outer)


def _tests(root: Node) -> list[_Test]:
    return [
        _test_of(node, _text(node.child_by_field_name("name")))
        for node in mock_stubs._walk(root)
        if node.type == "function_definition" and _text(node.child_by_field_name("name")).startswith("test")
    ]


def _decorators(test: _Test) -> list[Node]:
    return [c for c in test.outer.children if c.type == "decorator"]


def _decorator_call(decorator: Node) -> Node | None:
    inner = [c for c in decorator.named_children if c.type != "comment"]
    return inner[0] if len(inner) == 1 and inner[0].type == "call" else None


def _targets_wrapper(call: Node | None, names: frozenset[str]) -> bool:
    """A call patching a dotted string that ends in a wrapper's name."""
    if call is None or call.type != "call" or _callee(call) not in _PATCHES | _MOCKER_PATCHES:
        return False
    positional, _ = _arguments(call)
    target = _string(positional[0]) if positional else None
    return target is not None and target.rsplit(".", 1)[-1] in names


def _proven(call: Node, targets: frozenset[str]) -> bool:
    positional, _ = _arguments(call)
    return _string(positional[0]) in targets


def _collect(test: _Test, names: frozenset[str], targets: frozenset[str]) -> None:
    """Every patch of a wrapper in ``test``. One this cannot read, or on a
    target no proof reached, has no value, which leaves the test."""
    seen: set[int] = set()

    def add(node: Node, call: Node) -> None:
        seen.add(call.id)
        test.patches.append(_Patch(node, _value(call) if _proven(call, targets) else None))

    for decorator in _decorators(test):
        call = _decorator_call(decorator)
        if call is not None and _targets_wrapper(call, names):
            add(decorator, call)
    for node in mock_stubs._walk(test.node):
        call = None
        if node.type == "with_item":
            call = node.child_by_field_name("value")
            if call is not None and call.type == "as_pattern":
                call = call.named_children[0] if call.named_children else None
        elif node.type == "expression_statement" and node.named_children:
            call = node.named_children[0]
            call = call if call.type == "call" and _callee(call) in _MOCKER_PATCHES else None
        if call is not None and _targets_wrapper(call, names):
            add(node, call)
    for node in mock_stubs._walk(test.node):
        if node.type == "call" and node.id not in seen and _targets_wrapper(node, names):
            test.patches.append(_Patch(node, None))


def _injects(call: Node) -> bool:
    """Whether a decorating patch hands the test a mock."""
    positional, keywords = _arguments(call)
    name = _callee(call)
    if name in _OBJECT_PATCHES:
        return len(positional) == 2 and "new" not in keywords
    return len(positional) == 1 and "new" not in keywords


def _parameter(test: _Test, decorator: Node) -> Node | None:
    """The parameter ``decorator`` injects: decorators inject bottom-up, so one
    is counted past those below it. ``None`` when one of those cannot be read
    or the list is no plain run of names."""
    position = 0
    for other in reversed(_decorators(test)):
        if other.id == decorator.id:
            break
        call = _decorator_call(other)
        name = _callee(call) if call is not None else ""
        if name in _DICT_PATCHES:
            continue
        if name in _PATCHES | _OBJECT_PATCHES:
            position += _injects(call)
        elif _INERT_DECORATOR.fullmatch(_text(other).lstrip("@").split("(", 1)[0].strip()) is None:
            return None
    parameters = test.node.child_by_field_name("parameters")
    names = [] if parameters is None else [c for c in parameters.named_children if c.type != "comment"]
    owner = test.outer.parent.parent if test.outer.parent is not None else None
    if owner is not None and owner.type == "class_definition":
        names = names[1:]
    if position >= len(names) or any(c.type != "identifier" for c in names[: position + 1]):
        return None
    return names[position]


def _reads(test: _Test, parameter: str) -> list[Node]:
    body = test.node.child_by_field_name("body")
    return [] if body is None else [
        n for n in mock_stubs._walk(body) if n.type == "identifier" and _text(n) == parameter
    ]


def _verification(read: Node) -> Node | None:
    """The statement a mock's read is the receiver of an assertion in, alone:
    ``mock_flag.assert_called_once()``."""
    attribute = read.parent
    if attribute is None or attribute.type != "attribute" or attribute.child_by_field_name("object") != read:
        return None
    if not _text(attribute.child_by_field_name("attribute")).startswith("assert_"):
        return None
    call = attribute.parent
    statement = call.parent if call is not None and call.type == "call" else None
    return statement if statement is not None and statement.type == "expression_statement" else None


def _line_after(data: bytes, end: int) -> int:
    newline = data.find(b"\n", end)
    return len(data) if newline == -1 else newline + 1


def _line_start(data: bytes, start: int) -> int:
    return data.rfind(b"\n", 0, start) + 1


def _parameter_span(parameter: Node) -> tuple[int, int]:
    """The parameter with the comma that parts it from a neighbour."""
    following = parameter.next_named_sibling
    if following is not None:
        return parameter.start_byte, following.start_byte
    previous = parameter.prev_named_sibling
    return (previous.end_byte if previous is not None else parameter.start_byte), parameter.end_byte


def _statement_span(statement: Node, data: bytes) -> tuple[int, int, bytes]:
    """A deleted statement's whole lines, or ``pass`` when it was its block's
    only statement and Python needs one."""
    block = statement.parent
    siblings = [] if block is None else [c for c in block.named_children if c.type != "comment"]
    start, end = _line_start(data, statement.start_byte), _line_after(data, statement.end_byte)
    if len(siblings) == 1:
        indent = data[start : statement.start_byte]
        return start, end, indent + b"pass\n"
    return start, end, b""


def _with_edit(item: Node, data: bytes) -> tuple[int, int, bytes] | None:
    """The edit that takes one patch out of its ``with``; ``None`` when the body
    cannot be moved: a string over several lines would change."""
    clause = item.parent
    statement = clause.parent if clause is not None else None
    if statement is None or statement.type != "with_statement":
        return None
    items = [c for c in clause.named_children if c.type == "with_item"]
    if len(items) > 1:
        following, previous = item.next_named_sibling, item.prev_named_sibling
        if following is not None:
            return item.start_byte, following.start_byte, b""
        return previous.end_byte, item.end_byte, b""
    body = statement.child_by_field_name("body")
    colon = next((c for c in statement.children if c.type == ":"), None)
    if (
        body is None
        or colon is None
        or body.start_point[0] == colon.start_point[0]
        or any(n.type == "string" and n.start_point[0] != n.end_point[0] for n in mock_stubs._walk(body))
    ):
        return None
    width = body.start_point[1] - statement.start_point[1]
    lines = data[_line_after(data, colon.end_byte) : body.end_byte].split(b"\n")
    moved = b"\n".join(line[width:] if not line[:width].strip() else line for line in lines)
    return _line_start(data, statement.start_byte), body.end_byte, moved


def _kept_edits(test: _Test, data: bytes) -> tuple[list[tuple[int, int, bytes]], list[Removal]] | None:
    edits: list[tuple[int, int, bytes]] = []
    removals: list[Removal] = []
    for patch in test.patches:
        node = patch.node
        text = " ".join(_text(node).split())
        if node.type == "decorator":
            parameter = _parameter(test, node)
            call = _decorator_call(node)
            if parameter is None or call is None or not _injects(call):
                return None
            reads = _reads(test, _text(parameter))
            assertions = [_verification(read) for read in reads]
            if any(a is None for a in assertions):
                return None
            for assertion in assertions:
                edits.append(_statement_span(assertion, data))
                removals.append(Removal(VERIFICATION, " ".join(_text(assertion).split())))
            edits.append((_line_start(data, node.start_byte), _line_after(data, node.end_byte), b""))
            edits.append((*_parameter_span(parameter), b""))
        elif node.type == "with_item":
            alias = node.child_by_field_name("value")
            if alias is not None and alias.type == "as_pattern":
                return None
            edit = _with_edit(node, data)
            if edit is None:
                return None
            edits.append(edit)
        else:
            edits.append(_statement_span(node, data))
        removals.append(Removal(STUB, text))
    return edits, removals


def _overlap(edits: Iterable[tuple[int, int, bytes]]) -> bool:
    ordered = sorted(edits)
    return any(a[1] > b[0] for a, b in zip(ordered, ordered[1:]))


def resolve(
    source: str, treatment: bool, names: Iterable[str], targets: Mapping[str, mock_stubs.StubTargets]
) -> tuple[str, list[Removal]]:
    """``source`` with every decidable patch of the wrappers ``names`` resolved
    on its proven ``targets``; what went."""
    proven = frozenset(t for name in names if name in targets for t in targets[name].receivers)
    wrappers = frozenset(names)
    if not proven:
        return source, []
    data = source.encode("utf-8")
    root = syntax._parse(source, "python")
    edits: list[tuple[int, int, bytes]] = []
    deleted: list[_Test] = []
    removals: list[Removal] = []
    for test in _tests(root):
        _collect(test, wrappers, proven)
        values = {patch.value for patch in test.patches}
        if len(values) != 1 or None in values:
            continue
        if values == {not treatment}:
            deleted.append(test)
            removals.append(Removal(TEST, test.name))
            continue
        kept = _kept_edits(test, data)
        if kept is not None and not _overlap(kept[0]):
            edits += kept[0]
            removals += kept[1]
    if not removals:
        return source, []
    result = source
    # Last first, so every earlier span's offsets still hold.
    spans: list[tuple[int, int, bytes | _Test]] = [
        *edits, *((mock_stubs._with_leading_comments(t.outer, data), t.outer.end_byte, t) for t in deleted)
    ]
    for start, end, payload in sorted(spans, key=lambda span: span[0], reverse=True):
        if isinstance(payload, _Test):
            text = data[start:end].decode("utf-8", "replace")
            result = unused._delete_method(result, unused.Candidate(payload.name, start, end, "method", text))
        else:
            encoded = result.encode("utf-8")
            result = (encoded[:start] + payload + encoded[end:]).decode("utf-8")
    if mock_stubs._parse_errors(syntax._parse(result, "python")) > mock_stubs._parse_errors(root):
        return source, []
    return result, removals
