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

**C# test code says it two more ways**, for a keyless wrapper. A helper whose
``bool`` parameter feeds a stub (``.Returns(on)``) lets each call choose: the
pin (:mod:`flag_cleanup.cs_param_pins`) deletes the stub, the parameter and
every argument passed to it, and each calling test is judged by its
argument, or the default. A test fake -- an inert sibling -- is chosen by
its knob: a write of a literal (``stub.Enabled = false``) is a stub of that
value, and a fake built with one (``new StubGate { Enabled = false }``,
``new FixedGate(false)``) decides its test the same way but is never deleted.
Both are made only in a test project's files, and only for a wrapper that
completes; what they cannot resolve is reported by the wrapper pass and
keeps the wrapper.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping

from tree_sitter import Node

from flag_cleanup import cs_param_pins, key_const, syntax, ts_syntax, unused
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
#: A C# test that read a keyless wrapper's member through a receiver typed as
#: a sibling implementation; carries the test's name.
SIBLING_TEST = "sibling test"
#: A C# test helper's parameter removed by a pin (:mod:`flag_cleanup.cs_param_pins`),
#: one per helper; carries its signature and how many calls lost the argument.
PINNED_PARAMETER = "pinned parameter"
#: The statements of a C# test that set a fake's knob to the removed value
#: and back (:func:`_phases`); carries the test's name and the first write.
TEST_PHASE = "test phase"
#: A ``[Theory]`` that lost the rows passing the removed value to a helper.
TEST_ROWS = "test rows"
#: A member that called a public test helper (:mod:`flag_cleanup.cs_shared_pins`)
#: with the removed value, or a private one relying on a default that is the
#: removed value, and now runs with the kept one; carries the member and its
#: line, for review.
REBUILT = "rebuilt with the kept value"

#: An argument list's tail after the key: balanced to three levels, which
#: covers matchers such as ``any(Context.class)`` and ``It.IsAny<C>()``.
_BALANCED = r"(?:[^()]|\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))*"
_TAIL = rf"(?:\s*,{_BALANCED})?\)"
#: A receiver chain ending in a dot: ``client.``, ``this.client.``, ``m.EXPECT().``.
_CHAIN = r"(?:[\w$!?]+(?:\(\s*\))?\s*\.\s*)+"
#: A bare mock expression, as handed to ``verify(...)`` or ``when(...)``.
_MOCK = r"[\w$.!?]+"
_BOOL = r"(?P<value>true|false)"
#: A C# wrapper stub's value: a literal, or a name a helper's parameter may
#: hold (:mod:`flag_cleanup.cs_param_pins`).
_VALUE = r"(?:(?P<value>true|false)|(?P<param>[A-Za-z_]\w*))"

#: The words a statement must hold to be a test-double line at all. A read
#: with none of them is a real read, and the engine folds it.
_DSL = re.compile(
    r"\b(?:when|`when`|whenever|given|every|coEvery|verify|coVerify|verifyNever|"
    r"Verify|VerifyGet|Setup|SetupGet|EXPECT|doReturn|Returns|ReturnsAsync|ReturnsForAnyArgs|"
    r"Received|DidNotReceive|thenReturn|thenResolve|willReturn)\b"
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


@dataclass(frozen=True)
class WrapperStub:
    """Resolve the doubles of keyless wrappers instead of the SDK read.

    A wrapper takes no key, so a stub of it is recognised by the wrapper's
    name on a receiver the wrapper pass proved is the wrapper (see
    :mod:`flag_cleanup.wrapper_inline`), with any arguments: matchers such as
    ``any()`` are calls, and a stub is deleted, never inlined, so they drop
    nothing.
    """

    names: tuple[str, ...]
    #: C#: ``(test method, receiver)`` of one file, each receiver proven typed
    #: as a sibling implementation in that test. The test, reading a member
    #: through it, goes whole, whatever its rows -- but only for a wrapper
    #: that completes, whose sibling member is deleted with it (see ``kept``).
    siblings: frozenset[tuple[str, str]] = frozenset()
    #: Names of the wrappers that do NOT complete. A statement naming one is
    #: left with its whole test; for C#, their sibling members stay, and a
    #: test mentioning one through a sibling receiver stays, whatever else
    #: it reads.
    kept: frozenset[str] = frozenset()
    #: C#: the settable members of test fakes (inert siblings) that choose
    #: what the fake's member returns, written by tests as
    #: ``fake.Knob = <literal>``. A write is resolved like a stub of its value.
    knobs: frozenset[str] = frozenset()
    #: C#: ``(owner, knob, primary)`` for each test fake whose construction
    #: sets its knob, written ``new Owner { Knob = <literal> }`` or, for a
    #: ``primary`` constructor parameter, ``new Owner(<literal>)``. One built
    #: with the removed value decides its test; one built with the kept value
    #: is left as written.
    sibling_owners: frozenset[tuple[str, str, bool]] = frozenset()
    #: C#: ``(owner, default)`` for each fake of :attr:`sibling_owners` set by
    #: a member knob, with the value that knob holds until a test sets it. A
    #: test that builds one with no initializer and never writes the knob
    #: is decided by that value.
    owner_defaults: frozenset[tuple[str, bool]] = frozenset()
    #: C#: the fakes of :attr:`sibling_owners` built through a constructor that
    #: writes a SETTABLE knob (``Stub(bool on) => Enabled = on;``). Unlike a
    #: primary constructor's parameter, the value it is built with can be
    #: overwritten later, by a helper or a lambda no receiver proof sees, so a
    #: construction decides its test only when the fake is handed straight to
    #: another construction, bound to no name anything could reach it by.
    settable_owners: frozenset[str] = frozenset()
    #: C#: the fakes whose knob some test code writes from outside a test
    #: body, or inside a lambda or local function (a helper's
    #: ``g.Enabled = true``, ``s => s.Enabled = true``): such a write can set a
    #: fake back after a test chose its value, unseen by the test. While any
    #: is listed, a test is decided by a knob write or a settable fake's
    #: construction only when the fake cannot reach such code
    #: (:func:`_escapes`). Empty, every write is in a test body, judged there.
    flippable_owners: frozenset[str] = frozenset()
    #: C#: the simple names of the types test projects declare. A fake handed
    #: to one of them being constructed (``new Flipper(g)``) may be stored
    #: where something later sets its knob; one handed to anything else
    #: built (the code under test) cannot be.
    test_types: frozenset[str] = frozenset()
    #: C#: whether the file is test code this pass may edit for the pins,
    #: knobs and constructions above (its project is a test project). Off
    #: unless the caller says so.
    test_code: bool = False
    #: C#: ``(owner, method, parameter, index)`` of each public static test
    #: helper whose parameter builds one of these fakes, which a private
    #: helper's pin may pass its own parameter on to
    #: (:func:`flag_cleanup.cs_shared_pins.forwards`).
    forwards: frozenset[tuple[str, str, str, int]] = frozenset()


@dataclass(frozen=True)
class StubTargets:
    """Where a TypeScript or JavaScript test may stub one keyless wrapper in one
    file, as the wrapper pass proved it (see :mod:`flag_cleanup.wrapper_ts`).

    ``receivers`` are the objects a stub may name, as written with the
    whitespace taken out (``m``, ``flags``, ``Flags.prototype``,
    ``vi.mocked(flags)``). ``modules`` are the specifiers of the
    ``jest.mock``/``vi.mock`` calls there that replace the wrapper's own module.
    ``modules`` names every module mock proven to target the wrapper's own
    module; whether a factory there is resolvable is decided by the test-double
    pass. An automock never is.
    """

    receivers: frozenset[str] = frozenset()
    modules: frozenset[str] = frozenset()


#: The calls that replace a whole module, with a factory or without one.
TS_MODULE_MOCKS = frozenset(
    {"jest.mock", "jest.doMock", "jest.unstable_mockModule", "vi.mock", "vi.doMock"}
)


def ts_callee(call: Node) -> str:
    """A call's function as written, whitespace removed: ``jest.mock``."""
    function = call.child_by_field_name("function")
    return "" if function is None else "".join(function.text.decode("utf-8", "replace").split())


def ts_arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return [] if arguments is None else [c for c in arguments.named_children if c.type != "comment"]


def _plain_string(node: Node | None) -> str | None:
    """A string literal's text when it has no escapes; ``None`` otherwise."""
    if node is None or node.type != "string":
        return None
    parts = node.named_children
    if len(parts) != 1 or parts[0].type != "string_fragment":
        return None
    return parts[0].text.decode("utf-8", "replace")


def ts_module_specifier(node: Node | None) -> str | None:
    """The module a ``jest.mock``/``vi.mock`` first argument names: a plain
    string, or Vitest's ``import("<string>")``. ``None`` for anything computed."""
    if node is not None and node.type == "call_expression":
        function = node.child_by_field_name("function")
        inner = ts_arguments(node)
        if function is None or function.type != "import" or len(inner) != 1:
            return None
        node = inner[0]
    return _plain_string(node)


def ts_factory_object(call: Node) -> Node | None:
    """The object literal a module mock's factory returns, when the factory is
    written out as one: ``() => ({...})`` or ``() => { return {...}; }``.
    ``None`` for an automock, an ``async`` factory, a factory held in a
    variable, or anything else."""
    args = ts_arguments(call)
    if len(args) != 2 or args[1].type not in ("arrow_function", "function_expression", "function"):
        return None
    factory = args[1]
    if any(c.type == "async" for c in factory.children):
        return None
    body = factory.child_by_field_name("body")
    if body is not None and body.type == "statement_block":
        statements = [c for c in body.named_children if c.type != "comment"]
        if len(statements) != 1 or statements[0].type != "return_statement":
            return None
        returned = [c for c in statements[0].named_children if c.type != "comment"]
        body = returned[0] if len(returned) == 1 else None
    while body is not None and body.type == "parenthesized_expression":
        inner = [c for c in body.named_children if c.type != "comment"]
        body = inner[0] if len(inner) == 1 else None
    return body if body is not None and body.type == "object" else None


def ts_entry_name(entry: Node) -> str | None:
    """The key an object literal entry sets (``k: v``, ``"k": v``, ``k``,
    ``k() {}``); ``None`` for a spread or a computed key."""
    if entry.type == "shorthand_property_identifier":
        return entry.text.decode("utf-8", "replace")
    if entry.type == "pair":
        key = entry.child_by_field_name("key")
    elif entry.type == "method_definition":
        key = entry.child_by_field_name("name")
    else:
        return None
    if key is None:
        return None
    if key.type == "property_identifier":
        return key.text.decode("utf-8", "replace")
    return _plain_string(key)


def ts_actual_spread(entry: Node, specifier: str | None) -> bool:
    """``...jest.requireActual("<specifier>")``: the real module, spread in."""
    if entry.type != "spread_element" or specifier is None:
        return False
    inner = [c for c in entry.named_children if c.type != "comment"]
    if len(inner) != 1 or inner[0].type != "call_expression":
        return False
    args = ts_arguments(inner[0])
    return ts_callee(inner[0]) == "jest.requireActual" and len(args) == 1 and _plain_string(args[0]) == specifier


def _wrapper_patterns(language: str, names: str, receivers: str) -> list[tuple[str, str]]:
    if language == "csharp":
        return _csharp_wrapper_patterns(names, receivers)
    receiver = rf"(?:this\s*\.\s*)?(?:{receivers})"
    read_call = rf"{receiver}\s*\.\s*(?:{names})\(\s*{_BALANCED}\)"
    when = r"(?:(?:Mockito|BDDMockito)\.)?(?:when|`when`|whenever|given)"
    times = rf"(?:\s*,\s*(?:Mockito\.)?\w+\({_BALANCED}\))?"
    patterns = [
        (STUB, rf"{when}\(\s*{read_call}\s*\)\s*\.\s*(?:thenReturn|willReturn)\(\s*{_BOOL}\s*\)"),
        (
            STUB,
            rf"(?:Mockito\.)?doReturn\(\s*{_BOOL}\s*\)\s*\.\s*when\(\s*{receiver}\s*\)"
            rf"\s*\.\s*(?:{names})\(\s*{_BALANCED}\)",
        ),
        (
            VERIFICATION,
            rf"(?:Mockito\.)?verify\(\s*{receiver}{times}\s*\)\s*\.\s*(?:{names})\(\s*{_BALANCED}\)",
        ),
    ]
    if language == "kt":
        patterns += [
            (STUB, rf"(?:co)?every\s*\{{\s*{read_call}\s*\}}\s*returns\s+{_BOOL}"),
            (VERIFICATION, rf"(?:co)?verify\s*(?:\({_BALANCED}\))?\s*\{{\s*{read_call}\s*\}}"),
        ]
    return patterns


def _csharp_wrapper_patterns(names: str, receivers: str) -> list[tuple[str, str]]:
    """Moq and NSubstitute, on a property (no call) or a method (any
    arguments). A Moq lambda must read the member off its own parameter,
    which :func:`_same_parameter` checks. A stub's value may name a helper's
    parameter (``param``), which only a pin proves."""
    member = rf"(?:{names})(?:\(\s*{_BALANCED}\))?"
    lam = (
        r"(?:\(\s*(?:[\w<>.?\[\], ]+\s+)?(?P<parameter>\w+)\s*\)|(?P<bare>\w+))\s*=>\s*"
        rf"(?P<receiver>\w+)\s*\.\s*{member}"
    )
    mock = rf"(?:this\s*\.\s*)?(?:{receivers})"
    return [
        (STUB, rf"{mock}\s*\.\s*Setup(?:Get)?\(\s*{lam}\s*\)\s*\.\s*Returns(?:Async)?\(\s*{_VALUE}\s*\)"),
        (VERIFICATION, rf"{mock}\s*\.\s*Verify(?:Get)?\(\s*{lam}\s*(?:,{_BALANCED})?\)"),
        (STUB, rf"{mock}\s*\.\s*{member}\s*\.\s*Returns(?:ForAnyArgs)?\(\s*{_VALUE}\s*\)"),
        (
            VERIFICATION,
            rf"(?:_\s*=\s*)?{mock}\s*\.\s*(?:Received\(\s*\d*\s*\)|DidNotReceive\(\s*\))\s*\.\s*{member}",
        ),
    ]


def csharp_receiver_read(receivers: Iterable[str], members: Iterable[str], *, reads_only: bool) -> re.Pattern[str]:
    """``<receiver>.<member>`` and ``this.<receiver>.<member>``, for
    :func:`reads_through`. With ``reads_only``, an assignment target
    (``fake.IsXEnabled = true``) is not a read."""
    alternation = "|".join(re.escape(r) for r in sorted(receivers, key=len, reverse=True))
    names = "|".join(re.escape(m) for m in sorted(members, key=len, reverse=True))
    write = r"(?!\s*=(?!=))" if reads_only else ""
    return re.compile(rf"(?<![\w$])(?:this\s*\.\s*)?(?:{alternation})\s*\.\s*(?:{names})\b{write}")


def csharp_mock_mention(receiver: str, member: str) -> re.Pattern[str]:
    """Any statement through ``receiver`` that names ``member`` before it
    ends -- ``m.Setup(x => x.M)``, ``m.M.Returns(…)``, ``m.Received().M`` --
    for :func:`reads_through`. Deliberately loose: it only ever keeps a stub."""
    return re.compile(
        rf"(?<![\w$])(?:this\s*\.\s*)?{re.escape(receiver)}\s*\.[^;{{}}]*?\b{re.escape(member)}\b"
    )


def reads_through(text: str, pattern: re.Pattern[str]) -> bool:
    """Whether ``text`` reads a member through an UNQUALIFIED receiver (or
    ``this.``): ``h.gate.M``, ``h?.gate.M``, ``p->gate.M`` and ``base.gate.M`` name a member
    of another object, whatever ``gate`` is bound to here, so they never
    count -- as the wrapper pass never counts them as reads through ``gate``."""
    for match in pattern.finditer(text):
        before = match.start() - 1
        while before >= 0 and text[before].isspace():
            before -= 1
        if before >= 1 and text[before - 1 : before + 1] == "->":
            continue
        if before < 0 or text[before] not in ".?":
            return True
    return False


def _csharp_any_test(node: Node, data: bytes) -> bool:
    """Whether ``node`` is a C# test method of any kind, parameterised
    included: a read through a sibling deletes the test whatever its rows."""
    if node.type != "method_declaration":
        return False
    names = {
        _simple_name(_text(attribute, data))
        for attribute_list in node.children
        if attribute_list.type == "attribute_list"
        for attribute in attribute_list.named_children
        if attribute.type == "attribute"
    }
    return bool(names & (_TEST_MARKERS | _PARAMETERISED_MARKERS))


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


_TS_LANGUAGES = frozenset({"ts", "tsx", "js"})
#: A TS wrapper's stub may sit at the top of a test file as well as in a block.
_TS_WRAPPER_BLOCKS = frozenset({"statement_block", "program"})
#: Words that make a statement mentioning a wrapper a line of a double, even
#: when it spells the wrapper as a call (ts-mockito's ``when(f.m())``).
_TS_DOUBLE = re.compile(
    r"\b(?:spyOn|stub|when|verify|mocked|mock|doMock|mockReturnValue|mockReturnValueOnce|"
    r"mockImplementation|mockImplementationOnce|returns|thenReturn)\b"
)


def _alternation(names: Iterable[str]) -> str:
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


def _spelled(receiver: str) -> str:
    """A receiver as written, with any whitespace allowed between its tokens."""
    return r"\s*".join(re.escape(token) for token in re.findall(r"[\w$]+|\S", receiver))


def _ts_wrapper_patterns(name: str, receivers: str) -> list[tuple[str, str]]:
    """Jest and Vitest spies, Sinon stubs, a mock's member (jest-mock-extended,
    ``vi.mocked``), and ts-mockito, each on a receiver the wrapper pass
    proved. ``name`` is already escaped."""
    target = rf"(?:{receivers})"
    member = rf"(?P<quote>['\"`]){name}(?P=quote)"
    returns = rf"\s*\.\s*mockReturnValue(?:Once)?\(\s*{_BOOL}\s*\)"
    spy = rf"(?:jest|vi)\s*\.\s*spyOn\(\s*{target}\s*,\s*{member}\s*\)"
    return [
        (STUB, spy + returns),
        (STUB, spy + rf"\s*\.\s*mockImplementation\(\s*\(\s*\)\s*=>\s*{_BOOL}\s*\)"),
        (STUB, rf"sinon\s*\.\s*stub\(\s*{target}\s*,\s*{member}\s*\)\s*\.\s*returns\(\s*{_BOOL}\s*\)"),
        (STUB, rf"{target}\s*\.\s*{name}{returns}"),
        (
            STUB,
            rf"when\(\s*{target}\s*\.\s*{name}\(\s*{_BALANCED}\)\s*\)\s*\.\s*thenReturn\(\s*{_BOOL}\s*\)",
        ),
        (
            VERIFICATION,
            rf"verify\(\s*{target}\s*\.\s*{name}\(\s*{_BALANCED}\)\s*\)\s*\.\s*\w+\(\s*\d*\s*\)",
        ),
    ]


def _bool_function(node: Node | None) -> str | None:
    """``"true"``/``"false"`` for ``() => <bool>``, ``jest.fn(() => <bool>)`` or
    ``vi.fn(() => <bool>)``; ``None`` for anything else."""
    if node is not None and node.type == "call_expression":
        inner = ts_arguments(node)
        if ts_callee(node) not in ("jest.fn", "vi.fn") or len(inner) != 1:
            return None
        node = inner[0]
    if node is None or node.type != "arrow_function" or any(c.type == "async" for c in node.children):
        return None
    parameters = node.child_by_field_name("parameters")
    if parameters is None or [c for c in parameters.named_children if c.type != "comment"]:
        return None
    body = node.child_by_field_name("body")
    return body.type if body is not None and body.type in ("true", "false") else None


def _run_span(first: Node, last: Node, data: bytes) -> tuple[int, int] | None:
    """A run of adjacent object entries with the comma that parts it from a
    neighbour: the one after it, plus the spaces before the next token on the
    same line; or, with none after, the one before it. ``None`` when a comment
    sits between the run and that comma, which a cut would strand."""
    after = last.next_sibling
    while after is not None and after.type == "comment":
        after = after.next_sibling
    if after is not None and after.type == ",":
        if last.next_sibling.id != after.id:
            return None
        end = after.end_byte
        while data[end : end + 1] in (b" ", b"\t"):
            end += 1
        return first.start_byte, end
    before = first.prev_sibling
    while before is not None and before.type == "comment":
        before = before.prev_sibling
    if before is not None and before.type == ",":
        if first.prev_sibling.id != before.id:
            return None
        return before.start_byte, last.end_byte
    return first.start_byte, last.end_byte


def _ts_factory(
    statement: Node, data: bytes, modules: Mapping[str, frozenset[str]], treatment: bool
) -> list[tuple[int, int, Removal]] | None:
    """A top-level module mock of a wrapper's own module, decided.

    ``None`` when the statement is not one (or names no wrapper). Otherwise
    the spans to delete: the whole call when, with the wrapper's entries gone,
    nothing but ``...jest.requireActual(<same module>)`` would be left; else
    just those entries. An empty list leaves the statement alone. That happens
    when the factory cannot be read, or an entry is not ``() => <bool>``, or it
    returns the removed value: the stub then stays, and the wrapper is refused.
    """
    call = None
    if statement.type == "expression_statement" and statement.named_children:
        call = statement.named_children[0]
    if call is None or call.type != "call_expression" or ts_callee(call) not in TS_MODULE_MOCKS:
        return None
    args = ts_arguments(call)
    specifier = ts_module_specifier(args[0]) if args else None
    names = {name for name, specifiers in modules.items() if specifier in specifiers}
    if not names:
        return None
    found = ts_factory_object(call)
    if found is None:
        return []
    entries = [c for c in found.named_children if c.type != "comment"]
    ours = [e for e in entries if ts_entry_name(e) in names]
    if not ours:
        return None
    values = {_bool_function(e.child_by_field_name("value")) if e.type == "pair" else None for e in ours}
    if values != {"true" if treatment else "false"}:
        return []
    ids = {e.id for e in ours}
    if all(ts_actual_spread(e, specifier) for e in entries if e.id not in ids):
        return [(statement.start_byte, statement.end_byte, Removal(STUB, " ".join(_text(statement, data).split())))]
    runs: list[list[Node]] = []
    for index, entry in enumerate(entries):
        if entry.id not in ids:
            continue
        if runs and index > 0 and entries[index - 1].id == runs[-1][-1].id:
            runs[-1].append(entry)
        else:
            runs.append([entry])
    spans = []
    for run in runs:
        span = _run_span(run[0], run[-1], data)
        if span is None:
            return []
        text = data[run[0].start_byte : run[-1].end_byte].decode("utf-8")
        spans.append((*span, Removal(STUB, " ".join(text.split()))))
    return spans


def _resolve_ts_wrappers(
    source: str, language: str, treatment: bool, wrapper: WrapperStub, targets: Mapping[str, StubTargets]
) -> tuple[str, list[Removal]]:
    """:func:`resolve` for TypeScript and JavaScript keyless wrappers.

    A statement is a line of a double when it mentions a wrapper and either
    uses a mocking word or spells the wrapper other than as a call (a spy's
    string, a mock's member). A real call in a test, ``expect(f(u))``, is
    left to the fold. The decisions are :func:`_apply`'s, as for the SDK.
    """
    names = [n for n in wrapper.names if n in targets]
    if not names:
        return source, []
    words = _alternation(names)
    word = re.compile(rf"(?<![\w$])(?:{words})(?![\w$])")
    if not word.search(source):
        return source, []
    call = re.compile(rf"(?<![\w$])(?:{words})\s*\(")

    def describes(text: str) -> bool:
        mentions = len(word.findall(text))
        return mentions > 0 and (bool(_TS_DOUBLE.search(text)) or mentions > len(call.findall(text)))

    profile = _PROFILES[language]
    data = source.encode("utf-8")
    root = profile.parse(source, language)
    statements = _statements(root, data, _TS_WRAPPER_BLOCKS, describes)
    patterns = [
        (kind, re.compile(pattern))
        for name in names
        if targets[name].receivers
        for kind, pattern in _ts_wrapper_patterns(
            re.escape(name), "|".join(_spelled(r) for r in sorted(targets[name].receivers, key=len, reverse=True))
        )
    ]
    modules = {name: targets[name].modules for name in names if targets[name].modules}
    factories: list[tuple[int, int, Removal]] = []
    claimed: list[Node] = []
    for statement in statements:
        if statement.parent is None or statement.parent.type != "program":
            continue
        decided = _ts_factory(statement, data, modules, treatment)
        if decided is not None:
            claimed.append(statement)
            factories += decided
    # A factory's own lines (a `return {...}` in its body) are the factory's.
    statements = [
        s for s in statements
        if not any(c.start_byte <= s.start_byte and s.end_byte <= c.end_byte for c in claimed)
    ]
    return _apply(source, language, profile, root, data, statements, patterns, treatment, factories)


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
    #: Names a wrapper that stays (:attr:`WrapperStub.kept`): left whole.
    kept: bool = False
    #: C#: the values the test's fakes are built with (:attr:`WrapperStub.sibling_owners`).
    constructions: list[bool] = field(default_factory=list)
    #: C#: each of those constructions, with its value.
    built: list[tuple[Node, bool]] = field(default_factory=list)
    #: C#: the stubs among :attr:`stubs` that write a fake's knob.
    knob_writes: list[Node] = field(default_factory=list)
    #: C#: a fake whose value decides the test can be reached by code that may
    #: set its knob unseen (:func:`_escapes`), so the test cannot be decided
    #: by the removed value: neither deleted nor cut into phases.
    escapes: bool = False


#: A fake built in a shape nothing here reads.
_UNREADABLE = object()
#: A construction of anything but a test fake of this run.
_NOT_A_FAKE = object()


#: Where a name is captured: code that runs at a time the test does not choose.
_CAPTURING = frozenset({"lambda_expression", "anonymous_method_expression", "local_function_statement"})


def _knob_receivers_written(statement: Node, data: bytes, knobs: frozenset[str]) -> list[tuple[str, bool]]:
    """``(receiver, through this)`` for each knob ``statement`` writes: ``r``
    in ``r.Knob = …`` (``False``) or ``this.r.Knob = …`` (``True``); a longer
    chain (``host.Fake.Knob``) as written."""
    found: list[tuple[str, bool]] = []
    for node in _walk(statement):
        if node.type != "assignment_expression":
            continue
        left = node.child_by_field_name("left")
        if left is None or left.type != "member_access_expression":
            continue
        member = left.child_by_field_name("name")
        if member is None or _text(member, data) not in knobs:
            continue
        receiver = left.child_by_field_name("expression")
        via_this = False
        if receiver is not None and receiver.type == "member_access_expression":
            inner = receiver.child_by_field_name("expression")
            if inner is not None and inner.type in ("this", "this_expression"):
                receiver, via_this = receiver.child_by_field_name("name"), True
        found.append((_text(receiver, data) if receiver is not None else "", via_this))
    return found


def _built_type(creation: Node | None, data: bytes) -> str | None:
    """The simple name of the type ``creation`` builds: written, or for a
    target-typed ``new(…)`` the type its context declares (an arrow body or
    ``return`` of a method that is not ``async``, an explicitly typed
    declaration). ``None`` for anything else, and for a name a ``using``
    alias of the file defines, which may stand for any type."""
    if creation is None:
        return None
    if creation.type == "object_creation_expression":
        built = creation.child_by_field_name("type")
        name = re.split(r"[.:<]", _text(built, data))[-1].strip() if built is not None else None
    elif creation.type == "implicit_object_creation_expression":
        from flag_cleanup import ctor_param_csharp

        parent = creation.parent
        if parent is None or parent.type == "initializer_expression":
            return None  # a collection element: the repository may declare the collection
        name = ctor_param_csharp._target_name(creation, "")
    else:
        return None
    if name is None:
        return None
    root = creation
    while root.parent is not None:
        root = root.parent
    for node in _walk(root):
        if node.type == "using_directive":
            alias = node.child_by_field_name("name")
            if alias is not None and _text(alias, data).removeprefix("@") == name:
                return None
    return name


def _built_escapes(
    node: Node, test: Node, data: bytes, knobs: frozenset[str], profile: _Profile, test_types: frozenset[str]
) -> bool:
    """Whether a fake constructed at ``node`` inside ``test`` can be reached by
    code that may set its knob: bound to a name that :func:`_escapes`, or
    handed to anything but an object of a type no test project declares."""
    holder = node.parent
    if holder is not None and holder.type == "variable_declarator":
        name = holder.child_by_field_name("name")
        return name is None or _escapes(_text(name, data), False, node, test, data, knobs, profile, test_types)
    if holder is not None and holder.type == "assignment_expression" and _same(holder.child_by_field_name("right"), node):
        left = holder.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            return _escapes(_text(left, data), False, node, test, data, knobs, profile, test_types)
    if holder is not None and holder.type == "argument":
        return not _into_code_under_test(holder, data, test_types)
    return True


def _into_code_under_test(argument: Node, data: bytes, test_types: frozenset[str]) -> bool:
    """Whether ``argument`` is passed, by value, to the construction of a type
    no test project declares: the code under test, which can only see the
    fake through its interface."""
    if any(not c.is_named and _text(c, data) in ("ref", "out", "in") for c in argument.children):
        return False
    creation = argument.parent.parent if argument.parent is not None else None
    built = _built_type(creation, data)
    return built is not None and built not in test_types


def _same(a: Node | None, b: Node) -> bool:
    return a is not None and a.start_byte == b.start_byte and a.end_byte == b.end_byte


def _escapes(
    name: str,
    via_this: bool,
    at: Node,
    test: Node,
    data: bytes,
    knobs: frozenset[str],
    profile: _Profile,
    test_types: frozenset[str],
) -> bool:
    """Whether the fake bound to ``name`` -- a local of ``test``, or a private
    field of its class (always, when reached ``via_this``) -- may have its
    knob set by code the test-double pass does not judge.

    A use is harmless when it writes the knob inside the test (each write is
    judged there) or in the class's instance constructor (it runs before the
    test body), reads the knob, or hands the fake by value to the
    construction of a type no test project declares (the code under test
    sees it only through its interface). Anything else escapes: any other
    member (a method, an extension method, a property that may return the
    fake), an argument to a call or to a test type's constructor, a capture
    by a lambda or local function, being assigned, aliased or returned. For
    a field, so does handing ``this`` anywhere, which reaches every field. A
    name bound any other way (a parameter, a property, a field that is not
    private, a static field, a field of a partial class) escapes too.

    Another test of the class is not looked at unless this one calls it: it
    never runs inside this one (a fresh instance each under xUnit, before or
    after it under a runner that shares one)."""
    scope = _binding_scope(name, via_this, test, data)
    if scope is None:
        return True
    root, declared = scope
    field = root.id != test.id
    called = {
        _text(fn, data)
        for node in _walk(test)
        if node.type == "invocation_expression"
        and (fn := node.child_by_field_name("function")) is not None
        and fn.type == "identifier"
    }
    for node in _walk(root):
        if field and node.type in ("this", "this_expression") and node.is_named:
            parent = node.parent
            if parent is None or parent.type != "member_access_expression" or not _same(
                parent.child_by_field_name("expression"), node
            ):
                return True
        if node.type != "identifier" or node.id == declared or _text(node, data) != name:
            continue
        inside = test.start_byte <= node.start_byte < test.end_byte
        if not inside:
            other, _ = _enclosing_test(node, profile, data)
            if other is not None and other.id != test.id:
                other_name = other.child_by_field_name("name")
                if other_name is None or _text(other_name, data) not in called:
                    continue
        if not _harmless_use(node, root, data, knobs, inside, test_types):
            return True
    return False


def _binding_scope(name: str, via_this: bool, test: Node, data: bytes) -> tuple[Node, int] | None:
    """``(the scope to search, the declaring identifier's id)`` for ``name``:
    the test, for one of its locals (never when reached through ``this``);
    its class, for a private, non-static field of a non-partial class;
    ``None`` otherwise."""
    if not via_this:
        for node in _walk(test):
            if node.type == "variable_declarator":
                declared = node.child_by_field_name("name")
                if declared is not None and _text(declared, data) == name:
                    return test, declared.id
    owner = test.parent
    while owner is not None and owner.type not in ("class_declaration", "struct_declaration", "record_declaration"):
        owner = owner.parent
    if owner is None or any(_text(c, data) == "partial" for c in owner.children if c.type == "modifier"):
        return None
    body = owner.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        if member.type != "field_declaration":
            continue
        words = {_text(c, data) for c in member.children if c.type == "modifier"}
        variable = next((c for c in member.named_children if c.type == "variable_declaration"), None)
        for declarator in variable.named_children if variable is not None else ():
            declared = declarator.child_by_field_name("name") if declarator.type == "variable_declarator" else None
            if declared is not None and _text(declared, data) == name:
                if words & {"public", "protected", "internal", "static"}:
                    return None
                return owner, declared.id
    return None


def _harmless_use(
    node: Node, root: Node, data: bytes, knobs: frozenset[str], inside: bool, test_types: frozenset[str]
) -> bool:
    """Whether this mention of a fake's name cannot set its knob unseen; see
    :func:`_escapes`."""
    value = node
    parent = node.parent
    if parent is not None and parent.type == "member_access_expression" and _same(parent.child_by_field_name("name"), node):
        receiver = parent.child_by_field_name("expression")
        if receiver is None or receiver.type not in ("this", "this_expression"):
            return True  # `other.name`: another object's member, not this binding
        value, parent = parent, parent.parent
    ancestor = value.parent
    in_constructor = False
    while ancestor is not None and ancestor.id != root.id:
        if ancestor.type in _CAPTURING:
            return False
        if ancestor.type == "constructor_declaration" and not any(
            _text(c, data) == "static" for c in ancestor.children if c.type == "modifier"
        ):
            in_constructor = True
        ancestor = ancestor.parent
    if parent is None:
        return False
    if parent.type == "member_access_expression" and _same(parent.child_by_field_name("expression"), value):
        member = parent.child_by_field_name("name")
        if member is None or _text(member, data) not in knobs:
            # A method, an extension method, or a member that may hand the
            # fake on (`Self => this`).
            return False
        outer = parent.parent
        written = (
            outer is not None
            and outer.type in ("assignment_expression", "prefix_unary_expression", "postfix_unary_expression")
            and (outer.type != "assignment_expression" or _same(outer.child_by_field_name("left"), parent))
        ) or (outer is not None and outer.type == "argument")
        if not written:
            return True  # a read of the knob
        # Judged as a stub in the test, or set before it by the constructor.
        return (inside or in_constructor) and (outer is None or outer.type != "argument")
    if parent.type == "argument":
        return _into_code_under_test(parent, data, test_types)
    return False


def _passed_straight_into_a_construction(node: Node) -> bool:
    """``new Svc(new Fake())``: the fake is an argument of an object
    creation and bound to no name the test could reach it by."""
    argument = node.parent
    holder = argument.parent.parent if argument is not None and argument.type == "argument" and argument.parent is not None else None
    return holder is not None and holder.type in ("object_creation_expression", "implicit_object_creation_expression")


def _construction_parameter(node: Node, owners: dict[str, tuple[str, bool]], data: bytes) -> Node | None:
    """The identifier a construction of a positional-knob fake passes as its
    one argument (``new Fake(on)``, ``new Fake(enabled: on)``), when that is
    all it passes: a helper's parameter a construction pin may make."""
    type_node = node.child_by_field_name("type")
    if type_node is None or node.child_by_field_name("initializer") is not None:
        return None
    simple = _text(type_node, data)
    if simple not in owners or not owners[simple][1]:
        return None
    arguments = node.child_by_field_name("arguments")
    passed = [a for a in arguments.named_children if a.type != "comment"] if arguments is not None else []
    if len(passed) != 1 or passed[0].type != "argument":
        return None
    label = passed[0].child_by_field_name("name")
    value = passed[0].named_children[-1] if passed[0].named_children else None
    if (label is not None and _text(label, data) != owners[simple][0]) or value is None or value.type != "identifier":
        return None
    if any(not c.is_named and _text(c, data) in ("ref", "out", "in") for c in passed[0].children):
        return None
    return value


def _construction_value(node: Node, owners: dict[str, tuple[str, bool]], data: bytes):
    """What a construction of a test fake sets its knob to: ``True``/``False``,
    ``None`` when it sets nothing (the fake keeps its default, which
    qualification proved is the kept value), :data:`_UNREADABLE` for any
    other shape; or :data:`_NOT_A_FAKE`."""
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return _NOT_A_FAKE
    spelled = _text(type_node, data)
    simple = re.split(r"[.:]", spelled)[-1].split("<", 1)[0].strip()
    if simple not in owners:
        return _NOT_A_FAKE
    if spelled != simple:
        return _UNREADABLE  # qualified or generic: not the proven spelling
    knob, primary = owners[simple]
    arguments = node.child_by_field_name("arguments")
    passed = [a for a in arguments.named_children if a.type != "comment"] if arguments is not None else []
    initializer = node.child_by_field_name("initializer")
    if primary:
        if initializer is not None:
            return _UNREADABLE
        if not passed:
            return None
        if len(passed) != 1 or passed[0].type != "argument":
            return _UNREADABLE
        label = passed[0].child_by_field_name("name")
        value = passed[0].named_children[-1] if passed[0].named_children else None
        if (label is not None and _text(label, data) != knob) or value is None or value.type != "boolean_literal":
            return _UNREADABLE
        if any(not c.is_named and _text(c, data) in ("ref", "out", "in") for c in passed[0].children):
            return _UNREADABLE
        return _text(value, data) == "true"
    if passed:
        return _UNREADABLE
    if initializer is None:
        return None
    members = [c for c in initializer.named_children if c.type != "comment"]
    if len(members) != 1 or members[0].type != "assignment_expression":
        return _UNREADABLE
    left = members[0].child_by_field_name("left")
    right = members[0].child_by_field_name("right")
    operator = members[0].child_by_field_name("operator")
    if (
        left is None
        or right is None
        or left.type != "identifier"
        or _text(left, data) != knob
        or (operator is not None and _text(operator, data) != "=")
        or right.type != "boolean_literal"
    ):
        return _UNREADABLE
    return _text(right, data) == "true"


def resolve(
    source: str,
    language: str,
    flag_key: str,
    treatment: bool,
    accessors: tuple[str, ...] = (),
    *,
    wrapper: WrapperStub | None = None,
    receivers: frozenset[str] = frozenset(),
    knob_receivers: frozenset[str] | None = None,
    targets: Mapping[str, StubTargets] | None = None,
    checkout: tuple[str, str] | None = None,
) -> tuple[str, list[Removal]]:
    """``source`` with every decidable test-double line resolved; what went.

    With ``wrapper``, the doubles resolved are those of the named keyless
    wrappers instead of the SDK read's: on one of ``receivers`` in Java,
    Kotlin and C#, and on each name's ``targets`` in TypeScript and
    JavaScript. C# knob writes are matched on ``knob_receivers`` only, never
    on a mock's name.

    Pure: no file is read or written. See the module docstring for the policy.
    """
    profile = _PROFILES.get(language)
    if profile is None:
        return source, []
    if wrapper is not None and language in _TS_LANGUAGES:
        return _resolve_ts_wrappers(source, language, treatment, wrapper, targets or {})
    siblings = wrapper.siblings if wrapper is not None and language == "csharp" else frozenset()
    # C# test code: helper pins, fake knobs and fake constructions.
    fakes = wrapper is not None and language == "csharp" and wrapper.test_code
    knob_names = wrapper.knobs if fakes else frozenset()
    knob_holders = (knob_receivers or frozenset()) if knob_names else frozenset()
    owners = {o: (k, p) for o, k, p in wrapper.sibling_owners} if fakes else {}
    defaults = dict(wrapper.owner_defaults) if fakes else {}
    if wrapper is not None:
        if (
            language not in ("java", "kt", "csharp")
            or not (receivers or siblings or knob_holders or owners)
            or not wrapper.names
        ):
            return source, []
        reads = "|".join(re.escape(n) for n in sorted(wrapper.names, key=len, reverse=True))
        words = [*wrapper.names, *(knob_names if knob_holders else ()), *owners]
        if not re.search(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", source):
            return source, []
        data = source.encode("utf-8")
        root = profile.parse(source, language)
        # A C# property is read with no call.
        mention = re.compile(rf"\b(?:{reads})\b" if language == "csharp" else rf"\b(?:{reads})\s*\(")
        # With no receiver proven a wrapper here, no stub can be recognised:
        # every DSL line mentioning the member stays unprovable.
        alternation = "|".join(re.escape(r) for r in sorted(receivers, key=len, reverse=True))
        patterns = (
            [(kind, re.compile(p)) for kind, p in _wrapper_patterns(language, reads, alternation)]
            if receivers
            else []
        )
    else:
        reads = _reads(language, accessors)
        if not re.search(rf"\b(?:{reads})\b", source):
            return source, []
        data = source.encode("utf-8")
        root = profile.parse(source, language)
        consts = [k.name for k in key_const.key_consts(root, data, language, flag_key)]
        key = _key(flag_key, consts)
        mention = re.compile(rf"\b(?:{reads})\s*\(\s*{key}")
        patterns = [(kind, re.compile(p)) for kind, p in profile.patterns(reads, key)]
    # C#: a statement naming a fake's knob through a proven receiver is a
    # line of the double too: a write of a literal is resolved like a stub,
    # one of a `[Theory]`'s own parameter by its rows, and anything else (a
    # read, a computed value) leaves its test unprovable.
    knob_mention: re.Pattern[str] | None = None
    if knob_holders:
        holders = "|".join(re.escape(r) for r in sorted(knob_holders, key=len, reverse=True))
        knobs = "|".join(re.escape(k) for k in sorted(knob_names, key=len, reverse=True))
        holder = rf"(?:this\s*\.\s*)?(?:{holders})"
        knob_mention = re.compile(rf"(?<![\w$]){holder}\s*\.\s*(?:{knobs})\b")
        patterns.append((STUB, re.compile(rf"{holder}\s*\.\s*(?:{knobs})\s*=\s*{_VALUE}")))

    def describes(text: str) -> bool:
        return _describes_read(text, mention) or (knob_mention is not None and bool(knob_mention.search(text)))

    statements = _statements(root, data, profile.blocks, describes)
    if language == "csharp" and wrapper is not None:
        statements += [n for n in _constructor_bodies(root, data) if describes(_text(n, data))]
    return _apply(
        source, language, profile, root, data, statements, patterns, treatment,
        wrapper=wrapper, siblings=siblings, fakes=bool(fakes), owners=owners,
        knob_writes=patterns[-1][1] if knob_holders else None, defaults=defaults, checkout=checkout,
    )


def _statements(root: Node, data: bytes, blocks: frozenset[str], describes: Callable[[str], bool]) -> list[Node]:
    """The statements of ``blocks`` that describe the read, by their text."""
    return [
        node
        for node in _walk(root)
        if node.is_named
        and "comment" not in node.type
        and node.parent is not None
        and node.parent.type in blocks
        and describes(_text(node, data))
    ]


def _apply(
    source: str,
    language: str,
    profile: _Profile,
    root: Node,
    data: bytes,
    statements: list[Node],
    patterns: list[tuple[str, re.Pattern[str]]],
    treatment: bool,
    extra: Iterable[tuple[int, int, Removal]] = (),
    *,
    wrapper: WrapperStub | None = None,
    siblings: frozenset[tuple[str, str]] = frozenset(),
    fakes: bool = False,
    owners: dict[str, tuple[str, bool]] | None = None,
    knob_writes: re.Pattern[str] | None = None,
    defaults: dict[str, bool] | None = None,
    checkout: tuple[str, str] | None = None,
) -> tuple[str, list[Removal]]:
    """Decide each test or statement from its stubs and delete what goes, with
    the ``extra`` spans another recogniser already decided. ``siblings``,
    ``fakes``, ``owners`` and ``knob_writes`` (what a fake's knob write
    looks like) are C#'s (see :func:`resolve`)."""
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

    # A C# test reading the member through a sibling-typed receiver: the
    # sibling's member goes with the wrapper, so the test goes, whatever it
    # stubs. A write (`fake.IsXEnabled = true`) is not a read.
    sibling_tests: list[tuple[Node, str, str]] = []
    by_test: dict[str, set[str]] = {}
    for method, receiver in siblings:
        by_test.setdefault(method, set()).add(receiver)
    for node in _walk(root) if by_test else ():
        name = node.child_by_field_name("name")
        names = by_test.get(_text(name, data)) if name is not None else None
        if not names or not _csharp_any_test(node, data):
            continue
        text = _text(node, data)
        if not reads_through(text, csharp_receiver_read(names, wrapper.names, reads_only=True)):
            continue
        # Deciding what STAYS: any spelling counts, qualified or not.
        if wrapper.kept and csharp_receiver_read(names, wrapper.kept, reads_only=False).search(text):
            continue
        sibling_tests.append((node, _text(name, data), SIBLING_TEST))
    # A statement naming a wrapper that stays describes a read that still
    # happens, so its whole unit -- stub, verification, test -- is left as is.
    kept_word = (
        re.compile(r"\b(?:" + "|".join(re.escape(k) for k in sorted(wrapper.kept)) + r")\b")
        if wrapper is not None and wrapper.kept
        else None
    )

    units: dict[int, _Unit] = {}
    #: A stub returning a helper's parameter, with the pin proving it.
    pins: list[tuple[Node, cs_param_pins.CsPin, _Unit]] = []
    #: A pinned construction's id -> the parameter it passes, which takes the
    #: kept literal instead of being deleted with a stub's line.
    constructed: dict[int, Node] = {}
    #: A fake's knob written with a ``[Theory]``'s own parameter, whose rows
    #: say the value (:func:`cs_param_pins.theory_write`).
    theory_writes: list[tuple[Node, cs_param_pins.TheoryRows]] = []
    for statement in statements:
        if statement.id in containers:
            continue
        test, name = _enclosing_test(statement, profile, data)
        unit_id = test.id if test is not None else statement.id
        unit = units.setdefault(unit_id, _Unit(test, name))
        if kept_word is not None and kept_word.search(_text(statement, data)):
            unit.kept = True
        kind, value, groups = _recognise(_text(statement, data), patterns)
        if kind == STUB and groups.get("param") is not None:
            # A knob written from a parameter is decided by a theory's rows
            # alone: a helper's knob write is not a stub pin.
            knob = knob_writes is not None and knob_writes.search(_text(statement, data)) is not None
            pin = (
                cs_param_pins.find(root, data, statement, groups["param"], forwards=wrapper.forwards)
                if fakes and not unit.kept and not knob
                else None
            )
            theory = (
                cs_param_pins.theory_write(statement, groups["param"], data)
                if knob and fakes and not unit.kept
                else None
            )
            if theory is not None:
                theory_writes.append((statement, theory))
            elif pin is None:
                unit.unprovable = True
            else:
                pins.append((statement, pin, unit))
        elif kind == STUB:
            unit.stubs.append((statement, value == "true"))
            if knob_writes is not None and knob_writes.search(_text(statement, data)):
                unit.knob_writes.append(statement)
                if test is not None and wrapper.flippable_owners and any(
                    "." not in receiver
                    and _escapes(receiver, via_this, statement, test, data, wrapper.knobs, profile, wrapper.test_types)
                    for receiver, via_this in _knob_receivers_written(statement, data, wrapper.knobs)
                ):
                    # Something other than this test may set the knob back.
                    unit.escapes = True
        elif kind == VERIFICATION:
            unit.verifications.append(statement)
        else:
            unit.unprovable = True
    if owners:
        for node in _walk(root):
            if node.type != "object_creation_expression":
                continue
            built = _construction_value(node, owners, data)
            if built is _NOT_A_FAKE:
                continue
            test, name = _enclosing_test(node, profile, data)
            if test is None:
                # A helper's parameter may choose it, as one chooses a stub.
                passed = _construction_parameter(node, owners, data) if built is _UNREADABLE else None
                pin = (
                    cs_param_pins.find(root, data, node, _text(passed, data), forwards=wrapper.forwards)
                    if passed is not None and fakes
                    else None
                )
                if pin is not None:
                    pins.append((node, pin, units.setdefault(node.id, _Unit(None, None))))
                    constructed[node.id] = passed
                continue  # no one test to decide; prove reports what is left
            unit = units.setdefault(test.id, _Unit(test, name))
            simple = re.split(r"[.:]", _text(node.child_by_field_name("type"), data))[-1].strip()
            if built is _UNREADABLE:
                unit.unprovable = True
            elif built is not None:
                unit.constructions.append(built)
                unit.built.append((node, built))
                if (
                    simple in wrapper.flippable_owners
                    and (simple in wrapper.settable_owners or not owners[simple][1])
                    and _built_escapes(node, test, data, wrapper.knobs, profile, wrapper.test_types)
                ):
                    # Built into a settable knob, then reachable by code that
                    # may set it back.
                    unit.escapes = True
            elif (
                defaults
                and _passed_straight_into_a_construction(node)
                and not (wrapper.flippable_owners and _built_type(node.parent.parent.parent, data) in wrapper.test_types)
            ):
                # Built as it is, at a default that is the removed value, and
                # handed straight to what the test builds, so nothing can set
                # it: the test runs the removed branch and goes, unless it
                # writes the knob anywhere, however spelled.
                simple = re.split(r"[.:]", _text(node.child_by_field_name("type"), data))[-1].strip()
                knob, primary = owners[simple]
                if not primary and defaults.get(simple, treatment) != treatment and not re.search(
                    rf"(?<![\w$@]){re.escape(knob)}\s*=(?!=)", _text(test, data)
                ):
                    unit.constructions.append(defaults[simple])

    sibling_ids = {test.id for test, _, _ in sibling_tests}
    doubles = [s for s in statements if s.id not in containers]
    #: ``(start, end, removal)`` of each test phase the last decision made.
    phases: list[tuple[int, int, Removal]] = []

    #: ``(call, test name)`` of each pinned call rebuilt with the kept value.
    rebuilt: list[tuple[Node, str | None]] = []
    #: The pins (by method start) one of whose calls passes the removed value
    #: only through the parameter's default: such a test never chose the
    #: value it runs with, so whether it holds with the kept one cannot be
    #: told, and the pin is not made.
    defaulted_removed: set[int] = set()

    def decide(live: list[tuple[Node, cs_param_pins.CsPin, _Unit]]):
        """``(tests, lines, pinned calls)`` with the pins in ``live`` made."""
        phases.clear()
        rebuilt.clear()
        defaulted_removed.clear()
        pinned: dict[int, list[bool]] = {}
        calls: list[tuple[cs_param_pins.CsPin, Node, bool | None, Node | None]] = []
        emptied: list[tuple[Node, str, str]] = []
        removed = "false" if treatment else "true"
        for _, pin, _ in live:
            defaulted: set[int] = set()
            for call, value in cs_param_pins.calls(root, data, pin, defaulted=defaulted) or ():
                if value == removed and call.id in defaulted:
                    defaulted_removed.add(pin.method_start)
                if value == cs_param_pins.ROWS:
                    # The theory's rows say the value: it goes when none
                    # passes the kept one, and is cut otherwise.
                    theory = cs_param_pins.theory_at(call, pin, data)
                    if theory is not None and not any(v == ("true" if treatment else "false") for _, _, v in theory.rows):
                        emptied.append((theory.method, theory.name, TEST))
                    calls.append((pin, call, None, theory.method if theory is not None else None))
                    continue
                test, name = _enclosing_test(call, profile, data)
                if test is not None:
                    units.setdefault(test.id, _Unit(test, name))
                    pinned.setdefault(test.id, []).append(value == "true")
                calls.append((pin, call, value == "true", test))
        tests: list[tuple[Node, str, str]] = list(sibling_tests) + emptied
        lines: list[tuple[Node, str]] = []
        for unit in units.values():
            values = {v for _, v in unit.stubs} | set(unit.constructions)
            if unit.test is not None:
                values |= set(pinned.get(unit.test.id, ()))
            if unit.kept or (unit.test is not None and unit.test.id in sibling_ids):
                continue
            if unit.escapes and (not treatment) in values:
                # The removed value may not be the one the test runs with.
                unit.unprovable = True
            if unit.test is not None and not unit.unprovable and values == {not treatment}:
                tests.append((unit.test, unit.name or "", TEST))
                continue
            if (
                unit.test is not None
                and not unit.unprovable
                and values == {True, False}
                and all(v == treatment or _construction_start(n, data) is not None for n, v in unit.built)
                and len(unit.built) == len(unit.constructions)
                and unit.test.id not in pinned
            ):
                found = _phases(unit, treatment, doubles, data, checkout)
                if found is not None:
                    phases.extend(found)
                    lines += [(v, VERIFICATION) for v in unit.verifications]
                    lines += [(s, STUB) for s, value in unit.stubs if value == treatment]
                    continue
            lines += [(v, VERIFICATION) for v in unit.verifications]
            if not unit.unprovable and values == {treatment}:
                lines += [(s, STUB) for s, _ in unit.stubs]
        lines += [(statement, STUB) for statement, _, _ in live if statement.id not in constructed]
        # A theory writing its parameter to a knob: rows of the removed value
        # go, and the write with them, since every row left writes the kept one.
        for statement, theory in theory_writes:
            if any(v == ("true" if treatment else "false") for _, _, v in theory.rows):
                lines.append((statement, STUB))
            else:
                emptied_write = (theory.method, theory.name, TEST)
                if emptied_write not in tests:
                    tests.append(emptied_write)
        return tests, lines, calls

    # A pin is made whole or not at all: every call's test must go, or keep
    # its call with the kept value, the parameter gone. One that cannot
    # leaves the stub, which leaves the wrapper.
    live = list(pins)
    while True:
        tests, lines, calls = decide(live)
        deleted = [test for test, _, _ in tests]
        refused = set(defaulted_removed)
        for pin, call, value, test in calls:
            if any(t.start_byte <= call.start_byte and call.end_byte <= t.end_byte for t in deleted):
                continue
            if value is None:
                # A theory cut by its rows holds no double of its own.
                if test is None or any(test.start_byte <= s.start_byte < test.end_byte for s in doubles):
                    refused.add(pin.method_start)
                continue
            # A call passing the kept value only loses its argument, wherever
            # it is: outside any test (a `[Theory]`, a field) as well. One
            # passing the removed value needs a test to delete, and a test
            # left whole keeps its call as written.
            unit = units.get(test.id) if test is not None else None
            if value != treatment or (unit is not None and unit.kept) or (test is not None and unit is None):
                refused.add(pin.method_start)
        if not refused:
            raw = _pin_spans(root, data, live, deleted, treatment, theory_writes)
            forwarded = [span for _, pin, _ in live for span in pin.forwards]
            if raw is not None and not _overlaps(raw + forwarded, tests, lines, data):
                break
            refused = {pin.method_start for _, pin, _ in live}
        for _, pin, unit in live:
            if pin.method_start in refused:
                unit.unprovable = True
        live = [entry for entry in live if entry[1].method_start not in refused]

    # A test inside another deleted test (a `t.Run` subtest of a deleted
    # `TestX`) goes with its parent. As a span of its own it would overlap the
    # parent's, and cutting one shifts the other's bytes onto its neighbours.
    tests = [
        (test, name, kind)
        for test, name, kind in tests
        if not any(
            other.id != test.id
            and other.start_byte <= test.start_byte
            and test.end_byte <= other.end_byte
            for other, _, _ in tests
        )
    ]
    extra = list(extra)
    # A pin whose every call keeps its test still edits: the parameter, the
    # arguments and a construction's literal.
    if not lines and not tests and not extra and not phases and not raw and not live:
        return source, []
    spans: list[tuple[int, int, Removal | None]] = []
    for test, name, kind in tests:
        spans.append((_with_leading_comments(test, data), test.end_byte, Removal(kind, name)))
    spans += [p for p in phases if not any(t.start_byte <= p[0] and p[1] <= t.end_byte for t, _, _ in tests)]
    for line, kind in lines:
        if any(t.start_byte <= line.start_byte and line.end_byte <= t.end_byte for t, _, _ in tests):
            continue
        if any(start <= line.start_byte and line.end_byte <= end for start, end, _ in phases):
            continue
        text = " ".join(_text(line, data).split())
        spans.append((*_line_span(line, data), Removal(kind, text)))
    spans += [(start, end, None) for start, end in raw]
    kept_literal = "true" if treatment else "false"
    spans += [
        (constructed[statement.id].start_byte, constructed[statement.id].end_byte, kept_literal)
        for statement, _, _ in live
        if statement.id in constructed
    ]
    # A shared helper the parameter was passed on to builds the same fake.
    spans += [(start, end, kept_literal) for _, pin, _ in live for start, end in pin.forwards]
    spans += extra
    spans.sort(key=lambda span: span[0])
    result = source
    # Last first, so every earlier span's offsets still hold. A line and a test
    # take the same treatment: whole lines, plus the blank line that would be
    # left directly inside a brace or doubled up. A pin's parameter and
    # arguments are cut exactly.
    for start, end, removal in reversed(spans):
        if removal is None or isinstance(removal, str):
            # A cut, or a pinned construction's parameter given the kept literal.
            encoded = result.encode("utf-8")
            result = (encoded[:start] + (removal or "").encode("utf-8") + encoded[end:]).decode("utf-8")
            continue
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
    # One phase cut around a statement it keeps is two spans sharing ONE row,
    # so collapse by identity only: two tests deleting the same line are two
    # equal rows, and stay two.
    removals: list[Removal] = []
    for _, _, removal in spans:
        if isinstance(removal, Removal) and not any(removal is listed for listed in removals):
            removals.append(removal)
    return result, removals + [_pin_removal(root, data, pin, deleted) for pin in _with_chained(root, data, live)] + [
        Removal(
            TEST_ROWS,
            f"{theory.name}: {sum(v != kept for _, _, v in theory.rows)} of {len(theory.rows)} rows removed, "
            f"and parameter `{_text(theory.parameter.child_by_field_name('name'), data)}`",
        )
        for theory, kept in _cut_theories(root, data, live, deleted, treatment, theory_writes)
    ] + [
        Removal(REBUILT, f"{name or 'line'} (line {call.start_point[0] + 1})")
        for call, name in rebuilt
        if not any(t.start_byte <= call.start_byte < t.end_byte for t in deleted)
    ]


#: What a test phase may not hold: a jump out of it, a label into it, or a
#: local function the rest of its block may call.
_PHASE_JUMPS = frozenset(
    {"return_statement", "yield_statement", "goto_statement", "labeled_statement", "local_function_statement"}
)
#: Scopes whose declarations stay inside them.
_PHASE_SCOPES = frozenset(
    {"block", "lambda_expression", "anonymous_method_expression", "using_statement", "for_statement",
     "foreach_statement", "catch_clause", "switch_section", "local_function_statement"}
)


def _knob_key(statement: Node, data: bytes) -> str:
    """``host.Fake.Knob`` of a knob write, whitespace dropped."""
    return "".join(_text(statement, data).split()).rsplit("=", 1)[0]


def _declared_in_block_scope(statement: Node, data: bytes) -> set[str]:
    """The names ``statement``, a statement of a block, declares into that
    block: a local's, or a pattern or ``out`` variable's outside any nested
    scope (C# gives those the enclosing block)."""
    if statement.type == "local_declaration_statement":
        return {
            _text(n, data)
            for n in _walk(statement)
            if n.type == "identifier"
            and n.parent is not None
            and (
                (n.parent.type == "variable_declarator" and n.parent.child_by_field_name("name") == n)
                or n.parent.type == "tuple_pattern"
            )
        }
    found: set[str] = set()
    stack = list(statement.children)
    while stack:
        node = stack.pop()
        if node.type in _PHASE_SCOPES:
            continue
        if node.type == "identifier" and node.parent is not None and (
            node.parent.type.endswith("_designation") or node.parent.type == "declaration_expression"
        ):
            found.add(_text(node, data))
        stack.extend(node.children)
    return found


#: Where an initializer's ``Name = value`` names a member of the object being
#: built. An array's initializer (``new[] { x = 1 }``, ``int[] a = { … }``)
#: assigns the local ``x`` instead.
_OBJECT_CREATIONS = frozenset({"object_creation_expression", "implicit_object_creation_expression"})


def _written(node: Node, data: bytes) -> set[str]:
    """Names ``node`` writes: an assignment's target, an increment's
    operand, a ``ref``/``out`` argument. An object initializer's ``Member =
    …`` (``new T { Member = … }``) sets the new object's member, which is no
    name in scope."""
    found: set[str] = set()
    for n in _walk(node):
        target = None
        if (
            n.type == "assignment_expression"
            and n.parent is not None
            and n.parent.type == "initializer_expression"
            and n.parent.parent is not None
            and n.parent.parent.type in _OBJECT_CREATIONS
        ):
            continue
        if n.type == "assignment_expression":
            target = n.child_by_field_name("left")
        elif n.type in ("postfix_unary_expression", "prefix_unary_expression") and any(
            _text(c, data) in ("++", "--") for c in n.children
        ):
            target = n.named_children[0] if n.named_children else None
        elif n.type == "argument" and any(not c.is_named and _text(c, data) in ("ref", "out") for c in n.children):
            target = n.named_children[-1] if n.named_children else None
        if target is not None and target.type == "identifier":
            found.add(_text(target, data))
    return found


def _phase_independent(test: Node, phase: list[Node], data: bytes) -> bool:
    """Whether deleting ``phase`` leaves the rest of ``test`` compiling as it
    did: no name the phase writes is read outside it (CS0165, or a changed
    value), no local declared before it is used only inside it (CS0168,
    CS0219), and an ``await`` in it is not the test's last (CS1998)."""
    start, end = phase[0].start_byte, phase[-1].end_byte

    def outside(n: Node) -> bool:
        return not (start <= n.start_byte < end)

    words = [n for n in _walk(test) if n.type == "identifier"]
    written = set().union(*(_written(c, data) for c in phase))
    if any(outside(n) and _text(n, data) in written for n in words):
        return False
    for n in words:
        parent = n.parent
        if (
            parent is not None
            and parent.type == "variable_declarator"
            and parent.child_by_field_name("name") == n
            and n.end_byte <= start
        ):
            name = _text(n, data)
            uses = [w for w in words if w is not n and _text(w, data) == name]
            if uses and all(not outside(w) for w in uses):
                return False
    awaits = [n for n in _walk(test) if n.type == "await_expression"]
    return not awaits or any(outside(n) for n in awaits)


def _phases(
    unit: _Unit, treatment: bool, doubles: list[Node], data: bytes, checkout: tuple[str, str] | None = None
) -> list[tuple[int, int, Removal]] | None:
    """The spans of a C# test that run with a fake's knob at the removed
    value, when the test writes it there part-way through: each
    removed-value write is followed, in the same block and with no other
    double between, by a write of the kept value to the same
    ``host.Fake.Knob`` -- or by nothing, when it is a statement of the
    test's own body. The statements from the removed-value write up to the
    kept-value one (or the body's end) are the test's removed-value phase,
    and go; a kept-value write goes as a stub. A phase that holds a jump or
    a label, or declares a name a later statement of its block reads, is
    not deleted: ``None``, and the test stays whole."""
    if any(s.id not in {w.id for w in unit.knob_writes} for s, _ in unit.stubs):
        return None
    found: list[tuple[int, int, Removal]] = []
    order = sorted(doubles, key=lambda n: n.start_byte)
    for construction, value in unit.built:
        if value == treatment:
            continue
        cut = _construction_phase(unit, construction, treatment, order, data, checkout)
        if cut is None:
            return None
        found += cut
    for write, value in unit.stubs:
        if value == treatment:
            continue
        later = [n for n in order if n.start_byte >= write.end_byte]
        back = later[0] if later else None
        body = unit.test.child_by_field_name("body") if unit.test is not None else None
        if write.parent is None:
            return None
        if back is None or not (unit.test.start_byte <= back.start_byte < unit.test.end_byte):
            # Nothing sets it back: the rest of the test's own body runs at
            # the removed value.
            if body is None or write.parent.id != body.id:
                return None
            end = body.end_byte
        elif (
            back.parent is None
            or back.parent.id != write.parent.id
            or not any(s.id == back.id and v == treatment for s, v in unit.stubs)
            or _knob_key(back, data) != _knob_key(write, data)
        ):
            return None
        else:
            end = back.start_byte
        siblings = [c for c in write.parent.named_children if "comment" not in c.type]
        phase = [c for c in siblings if write.start_byte <= c.start_byte and c.end_byte <= end]
        after = [c for c in siblings if c.start_byte >= end]
        if any(n.type in _PHASE_JUMPS for c in phase for n in _walk(c)):
            return None
        declared = set().union(*(_declared_in_block_scope(c, data) for c in phase))
        if any(
            n.type == "identifier" and _text(n, data) in declared for c in after for n in _walk(c)
        ):
            return None
        if not _phase_independent(unit.test, phase, data):
            return None
        text = " ".join(_text(write, data).split())
        found.append((_with_leading_comments(write, data), phase[-1].end_byte, Removal(TEST_PHASE, f"{unit.name}: {text}")))
    return found or None


def _construction_start(construction: Node, data: bytes) -> tuple[Node, str, Node] | None:
    """``(statement, local, literal)`` when ``construction`` is the whole
    initializer of ``var local = new Fake(…)``, a statement of its test's
    own body, built with a ``bool`` literal: such a construction may start
    a removed-value phase (:func:`_construction_phase`)."""
    declarator = construction.parent
    while declarator is not None and declarator.type == "equals_value_clause":
        declarator = declarator.parent
    declaration = declarator.parent if declarator is not None and declarator.type == "variable_declarator" else None
    statement = declaration.parent if declaration is not None and declaration.type == "variable_declaration" else None
    if statement is None or statement.type != "local_declaration_statement" or len(
        [d for d in declaration.named_children if d.type == "variable_declarator"]
    ) != 1:
        return None
    if any(c.type == "modifier" or (not c.is_named and _text(c, data) in ("using", "await")) for c in statement.children):
        return None
    name = declarator.child_by_field_name("name")
    literal = next((n for n in _walk(construction) if n.type == "boolean_literal"), None)
    if name is None or literal is None or statement.parent is None or statement.parent.type != "block":
        return None
    method = statement.parent.parent
    if method is None or method.type != "method_declaration":
        return None
    return statement, _text(name, data), literal


def _local_value(statement: Node) -> Node | None:
    """The initializer of a one-declarator, non-``using`` local declaration."""
    declaration = next((c for c in statement.named_children if c.type == "variable_declaration"), None)
    declarators = [d for d in declaration.named_children if d.type == "variable_declarator"] if declaration else []
    if len(declarators) != 1 or any(not c.is_named and c.type in ("using", "await") for c in statement.children):
        return None
    name = declarators[0].child_by_field_name("name")
    values = [c for c in declarators[0].named_children if name is None or c.id != name.id]
    while values and values[-1].type == "equals_value_clause":
        values = list(values[-1].named_children)
    return values[-1] if values else None


#: Constant values a kept local may be built of.
_KEPT_CONSTANTS = frozenset(
    {"boolean_literal", "integer_literal", "real_literal", "character_literal", "string_literal",
     "verbatim_string_literal", "raw_string_literal", "null_literal"}
)


def _kept_building(value: Node, repo_dir: str, path: str) -> bool:
    """Whether evaluating ``value``, kept inside a cut removed-value phase,
    runs nothing that could observe the fake: a name, a constant, a static
    member of a type (``NullLogger<T>.Instance``), a value's field or
    auto-property, or a construction of a class whose constructors only
    assign (:func:`ctor_param_csharp._plain_construction`) from such
    arguments. Anything else may compute from the removed value, and then the
    rest of the test, run at the kept one, would assert something else."""
    from flag_cleanup import ctor_param_csharp
    from flag_cleanup import wrapper_csharp as wc

    kind = value.type
    if kind in _KEPT_CONSTANTS or kind == "identifier":
        return True
    if kind == "object_creation_expression":
        arguments = value.child_by_field_name("arguments")
        items = [a for a in arguments.named_children if a.type == "argument"] if arguments is not None else []
        return ctor_param_csharp._plain_construction(value, repo_dir, path) and all(
            (e := ctor_param_csharp._expression(a)) is not None and _kept_building(e, repo_dir, path) for a in items
        )
    if kind != "member_access_expression":
        return False
    receiver = value.child_by_field_name("expression")
    member = value.child_by_field_name("name")
    if receiver is None or member is None or member.type != "identifier":
        return False
    readers = wc._Readers(repo_dir)
    if receiver.type in ("generic_name", "predefined_type") or (
        receiver.type == "identifier" and wc._binding(receiver, receiver.text.decode("utf-8", "replace"), readers, path) is None
    ):
        return True  # a static member of a type: nothing reaches a fake just built plainly
    typed = wc._static_type(receiver, readers, path)
    found = wc._checkout_type(typed, readers) if typed is not None else None
    if found is None or not _kept_building(receiver, repo_dir, path):
        return False
    declaration, _ = found
    body = declaration.child_by_field_name("body")
    name = member.text.decode("utf-8", "replace")
    members = list(body.named_children) if body is not None else []
    fields = {f for m in members if m.type == "field_declaration" for f in ctor_param_csharp._field_names(m)}
    for m in members:
        if m.type == "field_declaration" and name in fields:
            return True
        if m.type == "property_declaration" and wc._name_of(m) == name:
            return ctor_param_csharp._auto_property(m) or _stored_getter(m, fields)
    return False


def _stored_getter(property_node: Node, fields: set[str]) -> bool:
    """Whether reading ``property_node`` runs nothing but a read of its own
    type's fields: its getter is one expression of those fields, constants,
    ``??`` and ``throw`` (``public T X => _x ?? throw new …;``). A throw
    yields no value, so what it builds cannot reach the kept local."""
    expression = None
    value = property_node.child_by_field_name("value")
    if value is not None and value.type == "arrow_expression_clause":
        expression = value.named_children[0] if value.named_children else None
    else:
        accessors = next((c for c in property_node.named_children if c.type == "accessor_list"), None)
        getters = [
            a for a in (accessors.named_children if accessors is not None else ())
            if a.type == "accessor_declaration" and any(not c.is_named and c.type == "get" for c in a.children)
        ]
        if len(getters) != 1:
            return False
        arrow = next((c for c in getters[0].named_children if c.type == "arrow_expression_clause"), None)
        block = next((c for c in getters[0].named_children if c.type == "block"), None)
        if arrow is not None:
            expression = arrow.named_children[0] if arrow.named_children else None
        elif block is not None:
            statements = [c for c in block.named_children if "comment" not in c.type]
            if len(statements) == 1 and statements[0].type == "return_statement":
                expression = statements[0].named_children[0] if statements[0].named_children else None
    return expression is not None and _stored_read(expression, fields)


def _stored_read(node: Node, fields: set[str]) -> bool:
    kind = node.type
    if kind in _KEPT_CONSTANTS or kind == "throw_expression":
        return True
    if kind == "identifier":
        return node.text.decode("utf-8", "replace") in fields
    if kind == "member_access_expression":
        receiver = node.child_by_field_name("expression")
        name = node.child_by_field_name("name")
        return (
            receiver is not None and receiver.type == "this_expression"
            and name is not None and name.text.decode("utf-8", "replace") in fields
        )
    if kind in ("parenthesized_expression", "postfix_unary_expression"):
        return bool(node.named_children) and _stored_read(node.named_children[0], fields)
    if kind == "binary_expression":
        operator = node.child_by_field_name("operator")
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        return (
            operator is not None and operator.type == "??"
            and left is not None and right is not None
            and _stored_read(left, fields) and _stored_read(right, fields)
        )
    return False


def _construction_phase(
    unit: _Unit,
    construction: Node,
    treatment: bool,
    order: list[Node],
    data: bytes,
    checkout: tuple[str, str] | None = None,
) -> list[tuple[int, int, Removal | str]] | None:
    """The edits that cut the removed-value phase a construction starts:
    ``var gate = new Fake(enabled: false); …; gate.Enabled = true;``. The
    construction takes the kept literal; the statements up to the kept-value
    write of the same local's knob go, but for a local declaration read after
    them whose initializer only builds objects (:data:`_KEPT_BUILDING`); the
    write goes as a stub. ``None`` when the shape is anything else, or what
    goes is not independent of the rest (:func:`_phase_independent`)."""
    from flag_cleanup import ctor_param_csharp

    start = _construction_start(construction, data)
    if start is None or unit.test is None or checkout is None:
        return None
    repo_dir, path = checkout
    # A fake whose construction runs code may have handed itself on already.
    if not ctor_param_csharp._plain_construction(construction, repo_dir, path):
        return None
    statement, local, literal = start
    later = [n for n in order if n.start_byte >= statement.end_byte]
    back = later[0] if later else None
    if (
        back is None
        or back.parent is None
        or back.parent.id != statement.parent.id
        or not any(s.id == back.id and v == treatment for s, v in unit.stubs)
        or not _knob_key(back, data).startswith(local + ".")
    ):
        return None
    siblings = [c for c in statement.parent.named_children if "comment" not in c.type]
    between = [c for c in siblings if statement.end_byte <= c.start_byte and c.end_byte <= back.start_byte]
    after = [c for c in siblings if c.start_byte >= back.end_byte]
    read_after = {_text(n, data) for c in after for n in _walk(c) if n.type == "identifier"}
    kept = [
        c for c in between
        if c.type == "local_declaration_statement"
        and _declared_in_block_scope(c, data) & read_after
        and (value := _local_value(c)) is not None
        and _kept_building(value, repo_dir, path)
    ]
    cut = [c for c in between if c not in kept]
    if any(n.type in _PHASE_JUMPS for c in cut for n in _walk(c)):
        return None
    declared = set().union(*(_declared_in_block_scope(c, data) for c in cut)) if cut else set()
    if any(n.type == "identifier" and _text(n, data) in declared for c in (*kept, *after) for n in _walk(c)):
        return None
    if cut and not _phase_independent(unit.test, cut, data):
        return None
    kept_literal = "true" if treatment else "false"
    text = " ".join(_text(statement, data).split())
    removal = Removal(TEST_PHASE, f"{unit.name}: {text}")
    edits: list[tuple[int, int, Removal | str]] = [(literal.start_byte, literal.end_byte, kept_literal)]
    runs: list[list[Node]] = []
    for c in cut:
        if runs and siblings.index(c) == siblings.index(runs[-1][-1]) + 1:
            runs[-1].append(c)
        else:
            runs.append([c])
    edits += [(_with_leading_comments(run[0], data), run[-1].end_byte, removal) for run in runs]
    return edits


def _with_chained(
    root: Node, data: bytes, live: list[tuple[Node, cs_param_pins.CsPin, _Unit]]
) -> list[cs_param_pins.CsPin]:
    """Each pin in ``live``, and after it each helper its calls pass the
    parameter on to (:func:`cs_param_pins.chained`), once."""
    found: list[cs_param_pins.CsPin] = []
    for _, pin, _ in live:
        for each in (pin, *cs_param_pins.chained(root, data, pin)):
            if all((p.method_start, p.parameter) != (each.method_start, each.parameter) for p in found):
                found.append(each)
    return found


def _pin_removal(root: Node, data: bytes, pin: cs_param_pins.CsPin, deleted: list[Node]) -> Removal:
    """The pull request's row for one pin: the helper as declared, and the
    calls that lost the argument. A call in a deleted test went with it, and
    one passing nothing kept its text, so neither is counted."""
    method = next(n for n in _walk(root) if n.type == "method_declaration" and n.start_byte == pin.method_start)
    listed = method.child_by_field_name("parameters")
    signature = " ".join(f"{pin.method}{_text(listed, data)}".split())
    _, *arguments = cs_param_pins.edits(root, data, pin) or [None]
    cut = sum(
        1 for start, end in arguments if not any(t.start_byte <= start and end <= t.end_byte for t in deleted)
    )
    said = (
        f"removed from {cut} call{'' if cut == 1 else 's'}" if cut else "removed; no remaining call passed it"
    )
    return Removal(PINNED_PARAMETER, f"{code_span(signature)}: parameter `{pin.parameter}` {said}")


def code_span(text: str) -> str:
    """``text`` as a markdown code span its own backticks cannot break out
    of, for a report row that carries its own markup (a pin's signature, a
    sibling's returned expression)."""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _cut_theories(
    root: Node,
    data: bytes,
    live: list[tuple[Node, cs_param_pins.CsPin, _Unit]],
    deleted: list[Node],
    treatment: bool,
    theory_writes: list[tuple[Node, cs_param_pins.TheoryRows]] = (),
) -> list[tuple[cs_param_pins.TheoryRows, str]]:
    """``(theory, kept literal)`` for each ``[Theory]`` a pin in ``live``, or
    a knob write of its own parameter, cuts by its rows rather than deleting
    it."""
    kept = "true" if treatment else "false"
    found = [
        (theory, kept)
        for _, theory in theory_writes
        if not any(t.start_byte <= theory.method.start_byte < t.end_byte for t in deleted)
    ]
    for _, pin, _ in live:
        for call, value in cs_param_pins.calls(root, data, pin) or ():
            if value != cs_param_pins.ROWS or any(t.start_byte <= call.start_byte < t.end_byte for t in deleted):
                continue
            theory = cs_param_pins.theory_at(call, pin, data)
            if theory is not None:
                found.append((theory, kept))
    return found


def _pin_spans(
    root: Node,
    data: bytes,
    live: list[tuple[Node, cs_param_pins.CsPin, _Unit]],
    deleted: list[Node],
    treatment: bool,
    theory_writes: list[tuple[Node, cs_param_pins.TheoryRows]] = (),
) -> list[tuple[int, int]] | None:
    """Every byte span the pins in ``live`` cut: each parameter, each
    argument outside a deleted test, and a cut theory's rows, column and
    parameter."""
    spans: list[tuple[int, int]] = []
    for theory, kept in _cut_theories(root, data, live, deleted, treatment, theory_writes):
        cut = cs_param_pins.theory_spans(theory, data, kept)
        if not cut:
            return None
        spans += cut
    for pin in _with_chained(root, data, live):
        edits = cs_param_pins.edits(root, data, pin)
        if edits is None:
            return None
        parameter, *arguments = edits
        spans.append(parameter)
        spans += [
            (start, end)
            for start, end in arguments
            if not any(t.start_byte <= start and end <= t.end_byte for t in deleted)
        ]
    return spans


def _overlaps(raw: list[tuple[int, int]], tests, lines, data: bytes) -> bool:
    """Whether a cut shares a byte -- or a deleted line's whole line -- with
    any other edit."""
    whole = [unused._expand_to_whole_lines(data, _with_leading_comments(t, data), t.end_byte) for t, _, _ in tests]
    whole += [unused._expand_to_whole_lines(data, *_line_span(n, data)) for n, _ in lines]
    ordered = sorted(raw)
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
        return True
    return any(start < e and s < end for start, end in raw for s, e in whole)


def _parse_errors(root: Node) -> int:
    """ERROR and MISSING nodes. Read off the s-expression, because a MISSING
    node tree-sitter inserts at a token boundary is not always among any
    node's ``children`` — measured: a deletion that cut a Go `func` header in
    half parsed with ``has_error`` set and not one such node reachable by a
    walk."""
    sexp = str(root)
    return sexp.count("(ERROR") + sexp.count("(MISSING")


def in_test_double(node: Node, data: bytes, language: str) -> bool:
    """Whether ``node`` sits in a statement of a mocking DSL.

    The statement is the nearest ancestor that is a direct child of one of the
    language's statement blocks. For Kotlin a bare ``when`` is the language's
    own keyword, never Mockito's (which Kotlin spells `` `when` `` or
    ``whenever``), so it does not count there.
    """
    profile = _PROFILES.get(language)
    if profile is None:
        return False
    statement = node
    while statement.parent is not None and statement.parent.type not in profile.blocks:
        statement = statement.parent
    text = _text(statement, data)
    for match in _DSL.finditer(text):
        if language == "kt" and match.group(0) == "when":
            if text[max(0, match.start() - 1) : match.start()] != "`":
                continue
        return True
    return False


def _describes_read(text: str, mention: re.Pattern[str]) -> bool:
    return bool(mention.search(text)) and bool(_DSL.search(text))


def _recognise(
    text: str, patterns: list[tuple[str, re.Pattern[str]]]
) -> tuple[str | None, str | None, dict[str, str | None]]:
    body = " ".join(text.split()).rstrip()
    body = body[:-1].rstrip() if body.endswith(";") else body
    for kind, pattern in patterns:
        match = pattern.fullmatch(body)
        if match is not None and _same_parameter(match):
            groups = match.groupdict()
            return kind, groups.get("value"), groups
    return None, None, {}


def _enclosing_test(statement: Node, profile: _Profile, data: bytes) -> tuple[Node | None, str | None]:
    ancestor = statement.parent
    while ancestor is not None:
        name = profile.test_name(ancestor, data)
        if name is not None:
            return ancestor, name
        ancestor = ancestor.parent
    return None, None


def _constructor_bodies(root: Node, data: bytes) -> list[Node]:
    """The expression body of each C# constructor that can be deleted with
    it (``public Tests() => _gate.Setup(…).Returns(true);``): a class's one
    instance constructor, with no parameters, initializer, attribute or
    comment, in a class that is not ``partial`` and has no primary
    constructor. The class's implicit constructor then does the same, less
    that statement. :func:`_line_span` deletes the whole constructor."""
    found = []
    for ctor in _walk(root):
        if ctor.type != "constructor_declaration":
            continue
        body = ctor.child_by_field_name("body")
        if body is None or body.type != "arrow_expression_clause":
            continue
        words = {_text(c, data) for c in ctor.children if c.type == "modifier"}
        parameters = ctor.child_by_field_name("parameters")
        owner = ctor.parent.parent if ctor.parent is not None else None
        if (
            words & {"static", "extern", "unsafe"}
            or parameters is None
            or parameters.named_children
            or any(c.type in ("attribute_list", "constructor_initializer") for c in ctor.children)
            or any("comment" in n.type for n in _walk(ctor))
            or owner is None
            or owner.type != "class_declaration"
            or any(c.type == "parameter_list" for c in owner.children)
            or "partial" in {_text(c, data) for c in owner.children if c.type == "modifier"}
        ):
            continue
        others = [
            m
            for m in ctor.parent.named_children
            if m.type == "constructor_declaration"
            and m.id != ctor.id
            and "static" not in {_text(c, data) for c in m.children if c.type == "modifier"}
        ]
        expressions = [c for c in body.named_children if "comment" not in c.type]
        if not others and len(expressions) == 1:
            found.append(expressions[0])
    return found


def _line_span(line: Node, data: bytes) -> tuple[int, int]:
    """What deleting the statement ``line`` deletes: the statement, or the
    whole constructor whose expression body it is (:func:`_constructor_bodies`)."""
    if line.parent is not None and line.parent.type == "arrow_expression_clause":
        ctor = line.parent.parent
        if ctor is not None and ctor.type == "constructor_declaration":
            return _with_leading_comments(ctor, data), ctor.end_byte
    return line.start_byte, line.end_byte


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


def _read_exact(path: str) -> str:
    """The file's text with its line endings as they are: a CRLF file read
    through universal newlines and written back would come back LF."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_exact(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


@dataclass
class Prepass:
    """Applies :func:`resolve` to the candidate files and owns the undo."""

    language: str
    flag_key: str
    treatment: bool
    accessors: tuple[str, ...] = ()
    #: Resolve keyless wrappers' doubles instead of the SDK read's.
    wrapper: WrapperStub | None = None
    #: path (as handed to :meth:`apply`) -> the receivers proven to be a wrapper.
    receivers: dict[str, frozenset[str]] = field(default_factory=dict)
    #: path (as handed to :meth:`apply`) -> ``(test method, receiver)`` proven
    #: typed as a sibling there (:attr:`WrapperStub.siblings`). Per file and
    #: test: a name proven in one test says nothing about it in another.
    siblings: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict)
    #: C#, path (as handed to :meth:`apply`) -> the receivers proven typed as
    #: a test fake there, whose knob writes are resolved.
    knob_receivers: dict[str, frozenset[str]] = field(default_factory=dict)
    #: C#, path -> the fakes (:attr:`WrapperStub.sibling_owners`) whose simple
    #: name is proven to mean them there.
    owners: dict[str, frozenset[tuple[str, str, bool]]] = field(default_factory=dict)
    #: C#: the paths whose project is a test project, where the pins, knobs
    #: and constructions may be resolved (:attr:`WrapperStub.test_code`).
    test_code: frozenset[str] = frozenset()
    #: path (as handed to :meth:`apply`) -> wrapper name -> where a TypeScript
    #: test may stub it there.
    targets: dict[str, dict[str, StubTargets]] = field(default_factory=dict)
    #: absolute path -> the customer's text before this pass touched it.
    originals: dict[str, str] = field(default_factory=dict)
    #: absolute path -> what was deleted there.
    removals: dict[str, list[Removal]] = field(default_factory=dict)
    #: absolute path -> the text this pass left, which is what the engine saw.
    edited: dict[str, str] = field(default_factory=dict)
    #: absolute path -> the spelling :meth:`apply` was handed it by.
    spelled: dict[str, str] = field(default_factory=dict)
    #: C#: the checkout, for a public test helper's calls in any file
    #: (:mod:`flag_cleanup.cs_shared_pins`).
    repo_dir: str | None = None

    def apply(self, paths: list[str]) -> None:
        forwards = self._forwards(paths)
        for path in paths:
            try:
                source = _read_exact(path)
            except (OSError, UnicodeDecodeError):
                continue
            wrapper = self.wrapper
            knob_receivers = None
            if wrapper is not None:
                wrapper = replace(
                    wrapper,
                    siblings=self.siblings.get(path, frozenset()),
                    sibling_owners=frozenset(
                        o for o in self.owners.get(path, ()) if o in wrapper.sibling_owners
                    ),
                    owner_defaults=wrapper.owner_defaults,
                    test_code=path in self.test_code,
                    forwards=forwards,
                )
                knob_receivers = self.knob_receivers.get(path, frozenset())
            resolved, removed = resolve(
                source, self.language, self.flag_key, self.treatment, self.accessors,
                wrapper=wrapper,
                receivers=self.receivers.get(path, frozenset()),
                knob_receivers=knob_receivers,
                targets=self.targets.get(path),
                checkout=(self.repo_dir, os.path.abspath(path)) if self.repo_dir is not None else None,
            )
            if not removed:
                continue
            key = os.path.abspath(path)
            self.spelled[key] = path
            self.originals[key] = source
            self.removals[key] = removed
            self.edited[key] = resolved
            _write_exact(path, resolved)
        self._apply_shared(paths)

    def _shared_helpers(
        self, paths: list[str]
    ) -> tuple[dict[str, str], dict[str, dict[str, tuple[str, bool]]]] | None:
        """``(helpers, owners)`` for :mod:`flag_cleanup.cs_shared_pins`: the
        text of each test file in ``paths`` holding a proven fake, and those
        fakes; ``None`` when there are none."""
        wrapper = self.wrapper
        if self.language != "csharp" or wrapper is None or self.repo_dir is None or not wrapper.sibling_owners:
            return None
        helpers: dict[str, str] = {}
        owners: dict[str, dict[str, tuple[str, bool]]] = {}
        for path in paths:
            if path not in self.test_code:
                continue
            proven = {o: (k, p) for o, k, p in self.owners.get(path, ()) if (o, k, p) in wrapper.sibling_owners}
            if not proven:
                continue
            key = os.path.abspath(path)
            try:
                helpers[key] = self.edited.get(key) or _read_exact(path)
            except (OSError, UnicodeDecodeError):
                continue
            owners[key] = proven
        return (helpers, owners) if helpers else None

    def _forwards(self, paths: list[str]) -> frozenset[tuple[str, str, str, int]]:
        """The public helpers a private helper's pin may pass its parameter
        on to (:func:`flag_cleanup.cs_shared_pins.forwards`)."""
        from flag_cleanup import cs_shared_pins

        shared = self._shared_helpers(paths)
        if shared is None:
            return frozenset()
        return cs_shared_pins.forwards(self.repo_dir, *shared, dict(self.edited))

    def _apply_shared(self, paths: list[str]) -> None:
        """A public test helper's pins (:mod:`flag_cleanup.cs_shared_pins`):
        its calls may be in any file of the checkout, so they are made after
        every file's own pass, and all or none."""
        from flag_cleanup import cs_shared_pins

        shared = self._shared_helpers(paths)
        if shared is None:
            return
        helpers, owners = shared
        kept = "true" if self.treatment else "false"
        plan = cs_shared_pins.plan(self.repo_dir, helpers, owners, kept, dict(self.edited))
        if plan is None:
            return
        written: dict[str, tuple[str, str]] = {}
        for key, edits in plan.edits.items():
            try:
                base = self.edited.get(key) or _read_exact(key)
            except (OSError, UnicodeDecodeError):
                return
            new = cs_shared_pins.apply(base, edits)
            if new is None:
                return
            written[key] = (base, new)
        for key, (base, new) in written.items():
            self.spelled.setdefault(key, key)
            self.originals.setdefault(key, base)
            self.edited[key] = new
            _write_exact(key, new)
        for key, rows in plan.rows.items():
            self.spelled.setdefault(key, key)
            self.removals.setdefault(key, []).extend(Removal(kind, text) for kind, text in rows)

    def reapply(self, wrapper: WrapperStub) -> None:
        """Resolve every file this pass edited again, from the text it found
        there, with ``wrapper`` instead."""
        self.wrapper = wrapper
        paths = [self.spelled.get(path, path) for path in self.originals]
        for path in list(self.originals):
            _write_exact(path, self.originals.pop(path))
            self.removals.pop(path, None)
            self.edited.pop(path, None)
        self.apply(paths)

    def restore_all(self) -> None:
        for path, source in self.originals.items():
            _write_exact(path, source)

    def withdraw(self, paths: Iterable[str]) -> None:
        """Undo this pass in ``paths`` and forget it there: a file the engine
        quarantined is reported as left untouched, so it must be."""
        for path in [os.path.abspath(p) for p in paths]:
            if path in self.originals:
                _write_exact(path, self.originals.pop(path))
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
                rebased.append(rewrite(path, source, _read_exact(path)))
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
