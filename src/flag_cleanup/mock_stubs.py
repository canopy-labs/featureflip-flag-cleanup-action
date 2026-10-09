"""Resolve the test doubles that stub or verify the flag read being removed.

A test that mocks the SDK says, in its stub, which branch it exercises:

    when(client.boolVariation("k", ctx, false)).thenReturn(false)    // Mockito
    client.Setup(c => c.BoolVariation("k", ctx, false)).Returns(false); // Moq
    m.EXPECT().BoolVariation("k", gomock.Any(), false).Return(false)  // gomock

Once the read is folded to the kept value, every such line is about a call
that no longer happens, and what is left of the test is decided by the
stub's own literal:

* **It returns the KEPT value.** The test exercises the branch that stays. The
  stub is dead (and under a strict mock it is an error), so the STUB goes and
  the test keeps running against the kept branch.
* **It returns the REMOVED value.** The test exercises the branch this removal
  deletes, so its assertions describe behaviour the code can no longer
  produce. The whole TEST goes — the same thing the fold just did to the
  branch it tested.
* **A verification of the read** (``verify(client).boolVariation(...)``,
  ``client.Verify(c => c.BoolVariation(...), ...)``, a gomock expectation)
  asserts a call that no longer exists, so it goes whatever the test is about.

Anything not provable from those literals is left alone: a stub returning a
variable or a ``[Theory]`` parameter, a test stubbing BOTH values, a stub in a
setup method or constructor that returns the removed value (no single test to
delete), a DSL spelling no profile here recognises. In every language but C#,
what is left then reaches the existing refusals — the fold turns a leftover
``when(client.boolVariation(…))`` into ``when(true)``, and
``syntax._mock_stubbed_literals`` / Go's ``literal_residues`` refuse the flag
— so an unprovable stub still costs the customer the flag's diff and never a
test that quietly stopped stubbing. C#'s Moq is untouched by the fold (see
``rules/csharp.toml``'s expression-tree guard) and reaches the pull request's
stub caveat instead.

**This runs BEFORE the engine, on the customer's bytes**, and that is not a
preference. Mockito, MockK, Dart mockito, ts-mockito and gomock take a REAL
call (mocktail takes one as the body of a lambda), so the engine folds the
read inside the stub along with every other read, and afterwards there is
nothing left to recognise. Guarding those reads
in the rules instead was rejected when the refusals were written: a
``not_enclosing_node`` per DSL name per language, each able to stop guarding
silently. Reading the stub first needs no rule at all.

**Only when this run folded a real read**, which the runner asks after the
engine (:func:`folded_a_read`); otherwise it undoes the whole transform and
runs it again without this pass. A stub sits in
a test while the read it stubs sits in production code, so a repository whose
read the rules cannot match — a key held in a variable — would otherwise get a
pull request that deletes its tests and nothing else.

**The decision is structural, the spelling is per library.** Each profile below
says which nodes hold statements, which are tests, and how each library spells
a stub and a verification. The recognisers are anchored regular expressions
over ONE statement's text: the grammars disagree underneath (Kotlin's
booleans are identifiers, Dart hangs arguments off selectors), and a statement
is already the unit being deleted, so a whole-statement match leaves nothing
partial behind. A statement that mentions the read inside a mocking DSL and
matches no recogniser marks its test as unprovable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from tree_sitter import Node

from flag_cleanup import key_const, syntax, ts_syntax, unused
from flag_cleanup.key_const import (
    _CSHARP_FLAG_READ_CALLEES,
    _DART_FLAG_READ_CALLEES,
    _GO_FLAG_READ_CALLEES,
    _JAVA_FLAG_READ_CALLEES,
    _KOTLIN_FLAG_READ_CALLEES,
    _TS_FLAG_READ_CALLEES,
)

#: What a removal is reported as. ``test`` carries the test's name.
STUB, VERIFICATION, TEST = "stub", "verification", "test"

#: An argument list's tail after the key: balanced to three levels, which
#: covers matchers such as ``any(Context.class)`` and ``It.IsAny<C>()``.
_BALANCED = r"(?:[^()]|\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))*"
_TAIL = rf"(?:\s*,{_BALANCED})?\)"
#: A receiver chain ending in a dot: ``client.``, ``this.client.``, ``m.EXPECT().``.
_CHAIN = r"(?:[\w$!?]+(?:\(\s*\))?\s*\.\s*)+"
#: A bare mock expression, as handed to ``verify(...)`` or ``when(...)``.
_MOCK = r"[\w$.!?]+"
_BOOL = r"(?P<value>true|false)"

#: The words a statement must hold to be a test-double line at all. A read
#: with none of them is a real read, and the engine folds it.
_DSL = re.compile(
    r"\b(?:when|`when`|whenever|given|every|coEvery|verify|coVerify|verifyNever|"
    r"Verify|Setup|EXPECT|doReturn|Returns|ReturnsAsync|Received|DidNotReceive|"
    r"thenReturn|thenResolve|willReturn)\b"
)


@dataclass(frozen=True)
class Removal:
    """One thing this pass deleted, for the pull request body."""

    kind: str
    #: A deleted line's whitespace-collapsed text, or a deleted test's name.
    text: str


@dataclass(frozen=True)
class _Profile:
    #: Node types whose named children are statements.
    blocks: frozenset[str]
    #: ``(kind, pattern)``; a stub pattern captures ``value``. Built per run
    #: from the read names and key spellings.
    patterns: Callable[[str, str], list[tuple[str, str]]]
    #: The test's name if ``node`` is a single test this pass may delete whole.
    test_name: Callable[[Node, bytes], str | None]
    parse: Callable[[str, str], Node]
    reads: frozenset[str]


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _walk(node: Node) -> Iterable[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _simple_name(text: str) -> str:
    """``@org.junit.Test(timeout = 1)`` -> ``Test``."""
    text = text.lstrip("@[ ").split("(", 1)[0].strip()
    return text.rsplit(".", 1)[-1].removesuffix("Attribute")


#: Marks a single test. Any PARAMETERISED marker beside it disqualifies the
#: method: its stub's value may be one row of a table, not the whole test.
_TEST_MARKERS = frozenset({"Test", "Fact", "TestMethod"})
_PARAMETERISED_MARKERS = frozenset(
    {
        "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate",
        "Theory", "TestCase", "TestCaseSource", "DataRow", "DataTestMethod",
        "InlineData", "MemberData", "ClassData", "Values", "Combinatorial",
        "Sequential", "Range", "Random", "AutoData", "InlineAutoData",
    }
)


def _marked_test(markers: list[str], node: Node, data: bytes) -> str | None:
    names = {_simple_name(m) for m in markers}
    if not names & _TEST_MARKERS or names & _PARAMETERISED_MARKERS:
        return None
    # TestNG parameterises through the marker itself.
    if any("dataProvider" in m for m in markers):
        return None
    name = node.child_by_field_name("name")
    return _text(name, data) if name is not None else None


def _java_test(node: Node, data: bytes) -> str | None:
    if node.type != "method_declaration":
        return None
    modifiers = next((c for c in node.children if c.type == "modifiers"), None)
    if modifiers is None:
        return None
    markers = [
        _text(c, data) for c in modifiers.children if c.type in ("marker_annotation", "annotation")
    ]
    return _marked_test(markers, node, data)


def _kotlin_test(node: Node, data: bytes) -> str | None:
    if node.type != "function_declaration":
        return None
    modifiers = next((c for c in node.children if c.type == "modifiers"), None)
    if modifiers is None:
        return None
    markers = [_text(c, data) for c in modifiers.children if c.type == "annotation"]
    names = {_simple_name(m) for m in markers}
    if not names & _TEST_MARKERS or names & _PARAMETERISED_MARKERS:
        return None
    name = next((c for c in node.children if c.type in ("identifier", "simple_identifier")), None)
    return _text(name, data) if name is not None else None


def _csharp_test(node: Node, data: bytes) -> str | None:
    if node.type != "method_declaration":
        return None
    markers = [
        _text(attribute, data)
        for attribute_list in node.children
        if attribute_list.type == "attribute_list"
        for attribute in attribute_list.named_children
        if attribute.type == "attribute"
    ]
    return _marked_test(markers, node, data)


def _go_test(node: Node, data: bytes) -> str | None:
    """``func TestX(t *testing.T)``, or one ``t.Run("name", func(…) {…})`` subtest."""
    if node.type == "function_declaration":
        name = node.child_by_field_name("name")
        parameters = node.child_by_field_name("parameters")
        if (
            name is not None
            and re.fullmatch(r"Test[A-Z_0-9]\w*|Test", _text(name, data))
            and parameters is not None
            and "*testing.T" in _text(parameters, data)
        ):
            return _text(name, data)
        return None
    if node.type != "expression_statement" or not node.named_children:
        return None
    call = node.named_children[0]
    if call.type != "call_expression":
        return None
    function = call.child_by_field_name("function")
    arguments = call.child_by_field_name("arguments")
    if (
        function is None
        or function.type != "selector_expression"
        or arguments is None
        or len(arguments.named_children) != 2
    ):
        return None
    field_node = function.child_by_field_name("field")
    title, body = arguments.named_children
    if (
        field_node is None
        or _text(field_node, data) != "Run"
        or title.type != "interpreted_string_literal"
        or body.type != "func_literal"
    ):
        return None
    return _text(title, data)[1:-1]


def _dart_test(node: Node, data: bytes) -> str | None:
    """``test('title', () { … });`` and ``testWidgets(…)``."""
    if node.type != "expression_statement" or len(node.named_children) < 2:
        return None
    callee, selector = node.named_children[0], node.named_children[1]
    if callee.type != "identifier" or _text(callee, data) not in ("test", "testWidgets"):
        return None
    match = re.match(r"\(\s*(['\"])(.*?)\1\s*,", _text(selector, data), re.DOTALL)
    return match.group(2) if match else None


#: ``it``/``test`` and their ``.only``/``.skip``/``x``/``f`` spellings — a
#: single test, never a ``describe`` group.
_TS_TEST_CALLEES = frozenset({"it", "test", "fit", "xit", "ftest", "xtest"})


def _ts_test(node: Node, data: bytes) -> str | None:
    if node.type != "expression_statement" or not node.named_children:
        return None
    call = node.named_children[0]
    if call.type != "call_expression":
        return None
    callee = call.child_by_field_name("function")
    arguments = call.child_by_field_name("arguments")
    if callee is None or arguments is None or not arguments.named_children:
        return None
    if callee.type == "member_expression":
        callee = callee.child_by_field_name("object")
    if callee is None or callee.type != "identifier" or _text(callee, data) not in _TS_TEST_CALLEES:
        return None
    title = arguments.named_children[0]
    if title.type != "string":
        return None
    return "".join(_text(c, data) for c in title.named_children)


def _mockito_patterns(read_call: str, read: str, key: str) -> list[tuple[str, str]]:
    """Mockito and BDDMockito, and the Kotlin and Dart ports of them."""
    when = r"(?:(?:Mockito|BDDMockito)\.)?(?:when|`when`|whenever|given)"
    times = rf"(?:\s*,\s*(?:Mockito\.)?\w+\({_BALANCED}\))?"
    return [
        (STUB, rf"{when}\(\s*{read_call}\s*\)\s*\.\s*(?:thenReturn|willReturn)\(\s*{_BOOL}\s*\)"),
        (
            STUB,
            rf"(?:Mockito\.)?doReturn\(\s*{_BOOL}\s*\)\s*\.\s*when\(\s*{_MOCK}\s*\)"
            rf"\s*\.\s*(?:{read})\(\s*{key}{_TAIL}",
        ),
        (VERIFICATION, rf"(?:Mockito\.)?verify\(\s*{_MOCK}{times}\s*\)\s*\.\s*(?:{read})\(\s*{key}{_TAIL}"),
    ]


def _java_patterns(read: str, key: str) -> list[tuple[str, str]]:
    return _mockito_patterns(rf"{_CHAIN}(?:{read})\(\s*{key}{_TAIL}", read, key)


def _kotlin_patterns(read: str, key: str) -> list[tuple[str, str]]:
    read_call = rf"{_CHAIN}(?:{read})\(\s*{key}{_TAIL}"
    return [
        *_mockito_patterns(read_call, read, key),
        (STUB, rf"(?:co)?every\s*\{{\s*{read_call}\s*\}}\s*returns\s+{_BOOL}"),
        (VERIFICATION, rf"(?:co)?verify\s*(?:\({_BALANCED}\))?\s*\{{\s*{read_call}\s*\}}"),
    ]


def _dart_patterns(read: str, key: str) -> list[tuple[str, str]]:
    """``package:mockito``, which takes the call, and ``package:mocktail``,
    which takes it as the body of a no-argument lambda: ``when(() => <read>)``.
    Either library may answer with a function instead of a value; one that
    returns a literal decides the test the same way."""
    read_call = rf"(?:\(\s*\)\s*(?:async\s*)?=>\s*)?{_CHAIN}(?:{read})\(\s*{key}{_TAIL}"
    when = rf"when\(\s*{read_call}\s*\)\s*\.\s*"
    return [
        (STUB, rf"{when}thenReturn\(\s*{_BOOL}\s*\)"),
        (STUB, rf"{when}thenAnswer\(\s*\(\s*\w+\s*\)\s*(?:async\s*)?=>\s*{_BOOL}\s*\)"),
        (VERIFICATION, rf"verify(?:Never)?\(\s*{read_call}\s*\)(?:\s*\.\s*called\(\s*\d+\s*\))?"),
    ]


def _ts_patterns(read: str, key: str) -> list[tuple[str, str]]:
    read_call = rf"{_CHAIN}(?:{read})\(\s*{key}{_TAIL}"
    return [
        (
            STUB,
            rf"when\(\s*{read_call}\s*\)\s*\.\s*(?:thenReturn|thenResolve)\(\s*{_BOOL}\s*\)",
        ),
        (VERIFICATION, rf"verify\(\s*{read_call}\s*\)\s*\.\s*\w+\(\s*\d*\s*\)"),
    ]


def _go_patterns(read: str, key: str) -> list[tuple[str, str]]:
    calls = r"(?:\s*\.\s*(?:Times|MinTimes|MaxTimes|AnyTimes)\(\s*\d*\s*\))*"
    return [
        (
            STUB,
            rf"{_MOCK}\.EXPECT\(\s*\)\s*\.\s*(?:{read})\(\s*{key}{_TAIL}"
            rf"{calls}\s*\.\s*Return\(\s*{_BOOL}\s*\){calls}",
        ),
    ]


def _csharp_patterns(read: str, key: str) -> list[tuple[str, str]]:
    # Moq: the read's receiver must be the lambda's own parameter, as in the
    # seed rules' expression-tree guard. Checked by `_same_parameter` below.
    lam = (
        r"(?:\(\s*(?:[\w<>.?\[\], ]+\s+)?(?P<parameter>\w+)\s*\)|(?P<bare>\w+))\s*=>\s*"
        rf"(?P<receiver>\w+)\s*\.\s*(?:{read})\(\s*{key}{_TAIL}"
    )
    return [
        (STUB, rf"{_MOCK}\.Setup\(\s*{lam}\s*\)\s*\.\s*Returns(?:Async)?\(\s*{_BOOL}\s*\)"),
        (VERIFICATION, rf"{_MOCK}\.Verify\(\s*{lam}\s*(?:,{_BALANCED})?\)"),
        # NSubstitute, which takes the real call.
        (
            STUB,
            rf"{_CHAIN}(?:{read})\(\s*{key}{_TAIL}\s*\.\s*Returns(?:ForAnyArgs)?\(\s*{_BOOL}\s*\)",
        ),
        (
            VERIFICATION,
            rf"{_MOCK}\.(?:Received\(\s*\d*\s*\)|DidNotReceive\(\s*\))\s*\.\s*(?:{read})\(\s*{key}{_TAIL}",
        ),
    ]


def _same_parameter(match: re.Match[str]) -> bool:
    if "receiver" not in match.re.groupindex:
        return True
    parameter = match.group("parameter") or match.group("bare")
    return parameter == match.group("receiver")


_TS = _Profile(
    blocks=frozenset({"statement_block"}),
    patterns=_ts_patterns,
    test_name=_ts_test,
    parse=ts_syntax._parse,
    reads=_TS_FLAG_READ_CALLEES,
)

_PROFILES: dict[str, _Profile] = {
    "java": _Profile(
        frozenset({"block"}), _java_patterns, _java_test, syntax._parse, _JAVA_FLAG_READ_CALLEES
    ),
    "kt": _Profile(
        frozenset({"block", "statements"}),
        _kotlin_patterns,
        _kotlin_test,
        syntax._parse,
        _KOTLIN_FLAG_READ_CALLEES,
    ),
    "dart": _Profile(
        frozenset({"block"}), _dart_patterns, _dart_test, syntax._parse, _DART_FLAG_READ_CALLEES
    ),
    "go": _Profile(
        frozenset({"statement_list", "block"}),
        _go_patterns,
        _go_test,
        syntax._parse,
        _GO_FLAG_READ_CALLEES,
    ),
    "csharp": _Profile(
        frozenset({"block"}),
        _csharp_patterns,
        _csharp_test,
        syntax._parse,
        # Only the synchronous read: the awaited one is the only form folded,
        # and an expression tree cannot hold an `await`, so a stub of it cannot
        # tell which form production uses.
        _CSHARP_FLAG_READ_CALLEES - {"GetBooleanValueAsync"},
    ),
    "ts": _TS,
    "tsx": _TS,
    "js": _TS,
}


def covers(language: str) -> bool:
    return language in _PROFILES


def _reads(language: str, accessors: tuple[str, ...]) -> str:
    names = sorted(_PROFILES[language].reads | set(accessors), key=len, reverse=True)
    return "|".join(re.escape(name) for name in names)


def _key(flag_key: str, consts: Iterable[str]) -> str:
    """The key as a stub can spell it: quoted, a hoisted constant, or inside an
    equality matcher (``eq("k")``, ``gomock.Eq("k")``)."""
    spellings = [re.escape(f'"{flag_key}"'), re.escape(f"'{flag_key}'"), re.escape(f"`{flag_key}`")]
    spellings += [rf"\b{re.escape(name)}\b" for name in consts]
    literal = "|".join(spellings)
    return rf"(?:(?:[\w$]+\.)*(?:eq|Eq)\(\s*(?:{literal})\s*\)|(?:{literal}))"


@dataclass
class _Unit:
    """The statements describing the read inside one test, or one statement
    outside any test."""

    test: Node | None
    name: str | None
    stubs: list[tuple[Node, bool]] = field(default_factory=list)
    verifications: list[Node] = field(default_factory=list)
    unprovable: bool = False


def resolve(
    source: str,
    language: str,
    flag_key: str,
    treatment: bool,
    accessors: tuple[str, ...] = (),
) -> tuple[str, list[Removal]]:
    """``source`` with every decidable test-double line resolved; what went.

    Pure: no file is read or written. See the module docstring for the policy.
    """
    profile = _PROFILES.get(language)
    if profile is None:
        return source, []
    reads = _reads(language, accessors)
    if not re.search(rf"\b(?:{reads})\b", source):
        return source, []
    data = source.encode("utf-8")
    root = profile.parse(source, language)
    consts = [k.name for k in key_const.key_consts(root, data, language, flag_key)]
    key = _key(flag_key, consts)
    mention = re.compile(rf"\b(?:{reads})\s*\(\s*{key}")
    patterns = [(kind, re.compile(p)) for kind, p in profile.patterns(reads, key)]

    statements = [
        node
        for node in _walk(root)
        if node.is_named
        and "comment" not in node.type
        and node.parent is not None
        and node.parent.type in profile.blocks
        and _describes_read(_text(node, data), mention)
    ]
    # A statement holding another such statement — `it(…, () => { when(…) })`
    # — is a container, not a line of the double. Its inner lines are judged.
    inner = {s.id for s in statements}
    containers = set()
    for statement in statements:
        ancestor = statement.parent
        while ancestor is not None:
            if ancestor.id in inner:
                containers.add(ancestor.id)
            ancestor = ancestor.parent

    units: dict[int, _Unit] = {}
    for statement in statements:
        if statement.id in containers:
            continue
        test, name = _enclosing_test(statement, profile, data)
        unit_id = test.id if test is not None else statement.id
        unit = units.setdefault(unit_id, _Unit(test, name))
        kind, value = _recognise(_text(statement, data), patterns)
        if kind == STUB:
            unit.stubs.append((statement, value == "true"))
        elif kind == VERIFICATION:
            unit.verifications.append(statement)
        else:
            unit.unprovable = True

    lines: list[tuple[Node, str]] = []
    tests: list[tuple[Node, str]] = []
    for unit in units.values():
        values = {value for _, value in unit.stubs}
        if (
            unit.test is not None
            and not unit.unprovable
            and values == {not treatment}
        ):
            tests.append((unit.test, unit.name or ""))
            continue
        lines += [(v, VERIFICATION) for v in unit.verifications]
        if not unit.unprovable and values == {treatment}:
            lines += [(s, STUB) for s, _ in unit.stubs]

    # A test inside another deleted test (a `t.Run` subtest of a deleted
    # `TestX`) goes with its parent. As a span of its own it would overlap the
    # parent's, and cutting one shifts the other's bytes onto its neighbours.
    tests = [
        (test, name)
        for test, name in tests
        if not any(
            other.id != test.id
            and other.start_byte <= test.start_byte
            and test.end_byte <= other.end_byte
            for other, _ in tests
        )
    ]
    if not lines and not tests:
        return source, []
    spans: list[tuple[int, int, Removal]] = []
    for test, name in tests:
        spans.append((_with_leading_comments(test, data), test.end_byte, Removal(TEST, name)))
    for line, kind in lines:
        if any(t.start_byte <= line.start_byte and line.end_byte <= t.end_byte for t, _ in tests):
            continue
        text = " ".join(_text(line, data).split())
        spans.append((line.start_byte, line.end_byte, Removal(kind, text)))
    spans.sort(key=lambda span: span[0])
    result = source
    # Last first, so every earlier span's offsets still hold. A line and a test
    # take the same treatment: whole lines, plus the blank line that would be
    # left directly inside a brace or doubled up.
    for start, end, removal in reversed(spans):
        result = unused._delete_method(
            result, unused.Candidate(removal.text, start, end, removal.kind, removal.text)
        )
    # This pass's own guard. Gate 1 judges the engine's rewrite of these files
    # from THIS output, not from the customer's text — its differential checks
    # read a deliberately deleted test as a fold that lost something (Go's `if`
    # initializer count, say) — so the one thing it can no longer see, whether
    # the deletion itself still parses, is asked here.
    if _parse_errors(profile.parse(result, language)) > _parse_errors(root):
        return source, []
    return result, [removal for _, _, removal in spans]


def _parse_errors(root: Node) -> int:
    """ERROR and MISSING nodes. Read off the s-expression, because a MISSING
    node tree-sitter inserts at a token boundary is not always among any
    node's ``children`` — measured: a deletion that cut a Go `func` header in
    half parsed with ``has_error`` set and not one such node reachable by a
    walk."""
    sexp = str(root)
    return sexp.count("(ERROR") + sexp.count("(MISSING")


def _describes_read(text: str, mention: re.Pattern[str]) -> bool:
    return bool(mention.search(text)) and bool(_DSL.search(text))


def _recognise(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> tuple[str | None, str | None]:
    body = " ".join(text.split()).rstrip()
    body = body[:-1].rstrip() if body.endswith(";") else body
    for kind, pattern in patterns:
        match = pattern.fullmatch(body)
        if match is not None and _same_parameter(match):
            return kind, match.groupdict().get("value")
    return None, None


def _enclosing_test(statement: Node, profile: _Profile, data: bytes) -> tuple[Node | None, str | None]:
    ancestor = statement.parent
    while ancestor is not None:
        name = profile.test_name(ancestor, data)
        if name is not None:
            return ancestor, name
        ancestor = ancestor.parent
    return None, None


def _with_leading_comments(node: Node, data: bytes) -> int:
    """Reach back over comments that touch the test: a doc comment left above
    the next member would describe the wrong test."""
    start = node.start_byte
    previous = node.prev_sibling
    while (
        previous is not None
        and "comment" in previous.type
        and data[previous.end_byte : start].count(b"\n") == 1
        and not data[previous.end_byte : start].strip()
    ):
        start = previous.start_byte
        previous = previous.prev_sibling
    return start


def _read_count(source: str, language: str, accessors: tuple[str, ...]) -> int:
    """Reads ``source`` PERFORMS: every call of a read, less those inside a
    statement of a mocking DSL, which describe a read rather than make one."""
    profile = _PROFILES[language]
    call = re.compile(rf"\b(?:{_reads(language, accessors)})\s*\(")
    total = len(call.findall(source))
    if not total:
        return 0
    data = source.encode("utf-8")
    described = [
        node
        for node in _walk(profile.parse(source, language))
        if node.is_named
        and node.parent is not None
        and node.parent.type in profile.blocks
        and _DSL.search(_text(node, data))
        and call.search(_text(node, data))
    ]
    ids = {node.id for node in described}
    outer = set()
    for node in described:
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.id in ids:
                outer.add(ancestor.id)
            ancestor = ancestor.parent
    return total - sum(
        len(call.findall(_text(node, data))) for node in described if node.id not in outer
    )


def folded_a_read(before: str, after: str, language: str, accessors: tuple[str, ...]) -> bool:
    """Whether the engine's rewrite ``before`` -> ``after`` removed a read.

    Asked of the engine's own summaries, whose "before" already has this
    pass's edits in it, so a deleted stub is not mistaken for a fold. And a
    stub the pass could NOT resolve, which the engine then folds
    (``sub.BoolVariation("k", …).Returns(v)`` becomes ``true.Returns(v)``), is
    not a fold either: :func:`_read_count` leaves out every read inside a
    mocking DSL's statement. Without that, a repository whose production read
    the rules cannot match had its tests resolved on the strength of the
    engine rewriting one of its other stubs. Counted as text otherwise, which
    the differential makes safe: a mention in a string or a comment is in
    both and cancels.
    """
    if language not in _PROFILES:
        return False
    return _read_count(after, language, accessors) < _read_count(before, language, accessors)


@dataclass
class Prepass:
    """Applies :func:`resolve` to the candidate files and owns the undo."""

    language: str
    flag_key: str
    treatment: bool
    accessors: tuple[str, ...] = ()
    #: absolute path -> the customer's text before this pass touched it.
    originals: dict[str, str] = field(default_factory=dict)
    #: absolute path -> what was deleted there.
    removals: dict[str, list[Removal]] = field(default_factory=dict)
    #: absolute path -> the text this pass left, which is what the engine saw.
    edited: dict[str, str] = field(default_factory=dict)

    def apply(self, paths: list[str]) -> None:
        for path in paths:
            try:
                source = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            resolved, removed = resolve(
                source, self.language, self.flag_key, self.treatment, self.accessors
            )
            if not removed:
                continue
            key = os.path.abspath(path)
            self.originals[key] = source
            self.removals[key] = removed
            self.edited[key] = resolved
            Path(path).write_text(resolved, encoding="utf-8")

    def restore_all(self) -> None:
        for path, source in self.originals.items():
            Path(path).write_text(source, encoding="utf-8")

    def withdraw(self, paths: Iterable[str]) -> None:
        """Undo this pass in ``paths`` and forget it there: a file the engine
        quarantined is reported as left untouched, so it must be."""
        for path in [os.path.abspath(p) for p in paths]:
            if path in self.originals:
                Path(path).write_text(self.originals.pop(path), encoding="utf-8")
                self.removals.pop(path, None)
                self.edited.pop(path, None)

    def rebase(self, summaries, rewrite: Callable) -> list:
        """Summaries whose "before" is the customer's text, plus a summary for
        every file this pass edited and the engine left alone."""
        rebased = [
            rewrite(
                s.path,
                self.originals.get(os.path.abspath(s.path), s.original_content),
                s.content,
            )
            for s in summaries
        ]
        seen = {os.path.abspath(r.path) for r in rebased}
        for path, source in self.originals.items():
            if path not in seen:
                rebased.append(rewrite(path, source, Path(path).read_text(encoding="utf-8")))
        return rebased

    def gate_baseline(self, path: str, original: str) -> str:
        """What Gate 1 should compare ``path``'s result against: this pass's
        output where it edited the file, else ``original``."""
        return self.edited.get(os.path.abspath(path), original)

    def report(self, repo_dir: str) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (os.path.relpath(path, repo_dir), removal.kind, removal.text)
            for path in sorted(self.removals)
            for removal in self.removals[path]
        )


def mock_descriptions(source: str, flag_key: str, accessors: tuple[str, ...]) -> list[str]:
    """Every C# statement still DESCRIBING a read of ``flag_key`` to a mock —
    a ``Setup`` or ``Verify`` this pass could not resolve — whitespace-collapsed.

    For the pull request's caveat, after the removal: these are the test lines
    whose subject is a read that no longer happens. C# only, because it is the
    one language whose leftover stubs survive the fold; everywhere else a
    leftover stub is folded into a literal and the flag is refused.
    """
    profile = _PROFILES["csharp"]
    data = source.encode("utf-8")
    root = profile.parse(source, "csharp")
    consts = [k.name for k in key_const.key_consts(root, data, "csharp", flag_key)]
    mention = re.compile(rf"\b(?:{_reads('csharp', accessors)})\s*\(\s*{_key(flag_key, consts)}")
    found: list[str] = []
    for node in _walk(root):
        if not node.type.endswith("statement") or node.type == "block":
            continue
        if not _describes_read(_text(node, data), mention):
            continue
        if any(
            child.type.endswith("statement") and _describes_read(_text(child, data), mention)
            for child in _walk(node)
            if child.id != node.id
        ):
            continue
        found.append(" ".join(_text(node, data).split()))
    return sorted(set(found), key=found.index)
