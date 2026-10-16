"""Keyless wrappers: inline a flag read hidden behind a member that takes no key.

A codebase that wraps its reads behind a method gets a removal that stops at
the wrapper::

    class FeatureFlags(private val client: Client) {
      fun shouldEnableX(userId: String): Boolean =
        client.getBoolean(ENABLE_X, withContext(userId), false)
    }
    // elsewhere
    if (featureFlags.shouldEnableX(userId)) newPath() else oldPath()

The rules fold the read inside ``shouldEnableX`` to a literal, and there they
stop: no caller names a key, so nothing can match them. This module finds such
a member, proves which of its call sites the file itself ties to it, and marks
exactly those for the engine to fold. Once nothing references the member any
more, it is deleted.

**Python proves, the engine edits.** Each proven call's callee is replaced by
the run's sentinel identifier -- the same mark the Kotlin pre-pass gives a read
(see :mod:`flag_cleanup.kotlin_sentinel`) -- and the language's seed rule folds
it with the whole cleanup cascade. Which calls are this flag's is decided here,
call by call, and a mark the engine does not consume is a refusal.

**A member name alone never qualifies a call.** The two reasons a member like
this used to be only reported (:mod:`flag_cleanup.constant_wrappers`) are both
answered structurally:

* A family of gates sharing ``isEnabled`` would be folded by name matching. So
  the name must be declared once in the repository, and every call must be
  bound to the wrapper through the receiver's declared type, which the file
  spells.
* A sibling implementation of the same interface would be overridden. So a
  member that implements an interface member qualifies only when that
  interface cannot be implemented outside the repository and every other
  implementation is inert: its member only returns a literal or its own
  stored state (C#; the JVM profiles still require exactly one
  implementation). An inert sibling's member is deleted with the wrapper.

Anything unproven is left exactly as it is and reported, with its reason.

The shared loop lives here; what a declaration, a receiver and an import look
like lives in one profile per language (:mod:`flag_cleanup.wrapper_kotlin`,
:mod:`flag_cleanup.wrapper_java`, :mod:`flag_cleanup.wrapper_csharp`,
:mod:`flag_cleanup.wrapper_go`, :mod:`flag_cleanup.wrapper_ts`), with
the facts both JVM languages share in :mod:`flag_cleanup.wrapper_jvm`. A
profile that brings its own repository index and qualification says so; one
that does not is judged by the JVM ones.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tree_sitter import Node

    from flag_cleanup import kotlin_sentinel, mock_stubs, wrapper_jvm

logger = logging.getLogger(__name__)

#: The member name, its class, or the interface member it implements is
#: declared more than once in the repository.
NAME_NOT_UNIQUE = "name-not-unique"
#: The member is open, abstract or an override, or its class is subclassed.
OVERRIDABLE = "overridable"
#: The member implements an interface member, and that interface can be (or
#: is) implemented by something else.
INTERFACE_PUBLISHED = "interface-published"
#: The interface is structural, so its implementations cannot be counted.
#: Never produced for the JVM languages, whose interfaces are nominal. Go's
#: are structural, and an exported Go method is kept for the same reason.
INTERFACE_STRUCTURAL = "interface-structural"
#: Nothing in the file ties the call's receiver to the wrapper. Also a use
#: handing a TypeScript or JavaScript function wrapper's module on as an
#: object, through which a computed name can call it.
RECEIVER_UNPROVEN = "receiver-unproven"
#: The receiver's declared type does not resolve to the wrapper's class.
IMPORT_UNRESOLVED = "import-unresolved"
#: An argument holds a call or an assignment, which inlining would drop.
ARGUMENT_HAS_CALL = "argument-has-call"
#: Folding the call would leave a local unused that the stranded-binding
#: cleanup cannot remove. Only Go produces it: there an unused local does not
#: build.
UNREMOVABLE_LOCAL = "unremovable-local"
#: The call goes through a pointer that may be nil to a method with a value
#: receiver, so it panics there, and folding it would remove the panic. Only
#: Go produces it.
NIL_RECEIVER = "nil-receiver"
#: A mention in a file of another language, which this run does not rewrite.
OTHER_LANGUAGE = "other-language"
#: A test double of the wrapper that could not be resolved.
TEST_DOUBLE = "test-double"
#: The member carries an annotation through which a framework may call it
#: (a route, a bean, a serializer), so it is kept even with no caller left.
ANNOTATED = "annotated"

#: The call, or a file the wrapper depends on, is outside the directories
#: this run rewrites.
OUTSIDE_RUN = "outside-run"
#: The name appears where nothing here can parse a call: a string (reflection,
#: an expression language), a template, a layout, a config file.
UNPARSED_MENTION = "unparsed-mention"
#: A JavaScript wrapper's own declaration file (``flags.d.ts`` beside
#: ``flags.js``) declares it. Its callers are inlined, but the declaration is
#: hand-written and outside what this run rewrites, so the wrapper stays
#: with it.
DECLARATION_FILE = "declaration-file"
#: The read inside the wrapper was not folded in this run (its file could not
#: be processed), so the wrapper stays as it was.
NOT_FOLDED = "not-folded"
#: Another implementation of the wrapper's interface does more than return
#: a literal or its own stored state, or cannot be proven not to: it may be
#: extended, based on a type outside the repository, shared with another
#: interface declaring the name, or read from outside the repository. Also
#: a sibling fixed at the value being removed, which whatever builds it
#: relies on, and a sibling member this run's own edits left where it cannot
#: be found.
SIBLING_NOT_INERT = "sibling-not-inert"
#: The member is read through a receiver typed as an inert sibling, whose
#: member goes with the wrapper.
SIBLING_RECEIVER = "sibling-receiver"

#: Annotations that say nothing about who calls a member. Not ``JvmName``:
#: Java calls the member by the name it gives, which no scan here looks for.
#: The C# ones are attribute names with any ``Attribute`` suffix dropped.
_INERT_ANNOTATIONS = frozenset(
    {"Override", "Suppress", "SuppressWarnings", "JvmStatic", "Deprecated"}
    | {"Obsolete", "SuppressMessage", "ExcludeFromCodeCoverage", "DebuggerStepThrough",
       "DebuggerHidden", "MethodImpl"}
)

#: String literal node types, in either JVM grammar.
_STRINGS = frozenset({"string_literal", "text_block", "multiline_string_literal"})

#: Not refusals: what became of a wrapper, reported at its declaration.
COMPLETE = "complete"
PARTIAL = "partial"
#: An inert sibling whose member went with a complete wrapper: what it
#: returned, and the value its callers now see.
SIBLING = "sibling"


@dataclass(frozen=True)
class InterfaceMember:
    """The interface member a wrapper implements, deleted along with it."""

    path: str
    name: str
    package: str
    start: int
    end: int


@dataclass(frozen=True)
class Sibling:
    """Another implementation of the wrapper's interface, whose member is
    inert and goes with the wrapper (see :mod:`flag_cleanup.csharp_family`)."""

    path: str
    owner: str
    namespace: str
    line: int
    #: Byte span of the sibling's member declaration.
    start: int
    end: int
    #: What the member returned, as written (``_options.Enabled``, ``true``).
    returns: str
    #: The settable member a test fake's member reads, written by tests to
    #: choose a value; ``None`` for a production sibling.
    knob: str | None = None
    #: Declared in a test project, by its project file.
    test: bool = False
    #: What a knob other than the member itself holds until something sets
    #: it: ``"true"``/``"false"``, or ``""`` for a primary-constructor
    #: parameter every construction passes.
    knob_default: str | None = None
    #: The knob is the primary constructor's parameter, set as
    #: ``new Owner(<value>)``; otherwise a member, set as ``x.Knob = <value>``
    #: or ``new Owner { Knob = <value> }``.
    knob_primary: bool = False


@dataclass(frozen=True)
class Wrapper:
    """A member whose whole body is one read of the flag being removed."""

    #: Resolved absolute path of the declaring file.
    path: str
    language: str
    name: str
    #: Simple name of the declaring top-level class or object.
    owner: str
    package: str
    #: 1-based line of the declaration.
    line: int
    #: Byte span of the whole member declaration, modifiers included.
    start: int
    end: int
    #: A Java ``static`` method, or a member of a Kotlin ``object`` or
    #: companion object: callable through the type's name.
    static: bool = False
    interface: InterfaceMember | None = None
    siblings: tuple[Sibling, ...] = ()
    #: Simple names of the annotations on the declaration.
    annotations: tuple[str, ...] = ()
    #: ``method``, ``property`` (a Kotlin getter, read with no call) or
    #: ``function`` (a Kotlin top-level function; ``owner`` is its file facade).
    kind: str = "method"
    #: A Kotlin top-level function's ``(required, max)`` argument counts, ``max``
    #: being ``None`` for a ``vararg``; ``None`` when not a function.
    arity: tuple[int, int | None] | None = None
    #: A ``private`` Kotlin top-level function, visible in its own file only.
    private: bool = False

    @property
    def label(self) -> str:
        return f"{self.owner}.{self.name}"


@dataclass(frozen=True)
class Occurrence:
    """One mention of a wrapper's name, proven or refused."""

    path: str
    line: int
    #: Whitespace-collapsed text of the call, for the report.
    text: str
    #: The bytes to replace with the sentinel; ``None`` when refused.
    span: tuple[int, int] | None
    #: ``None`` when proven.
    reason: str | None
    #: The receiver identifier, for the test-double pass.
    receiver: str | None = None
    #: Inside a statement of a mocking DSL; for a ``sibling-receiver`` read,
    #: inside a test method of a test project, which the test-double pass may
    #: delete whole.
    in_test_double: bool = False
    #: Written after the sentinel: ``"()"`` for a property read, which has
    #: no call of its own for the seed rules to match.
    suffix: str = ""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str
    text: str
    #: :attr:`Wrapper.label`.
    wrapper: str


@dataclass(frozen=True)
class Refused:
    """A qualification refusal caused by something other than the wrapper's
    own declaration (a sibling implementation), reported where it is."""

    reason: str
    path: str
    line: int
    text: str


@dataclass
class WrapperPlan:
    #: The wrappers that qualified.
    wrappers: tuple[Wrapper, ...] = ()
    #: path -> sentinel spans to mark there.
    spans: dict[str, list[tuple[int, int] | tuple[int, int, str]]] = field(default_factory=dict)
    #: path -> receivers proven bound to a wrapper there.
    receivers: dict[str, frozenset[str]] = field(default_factory=dict)
    #: Every refusal: qualification and per call.
    findings: list[Finding] = field(default_factory=list)
    #: Labels of the qualified wrappers with no refusal at all.
    complete: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Profile:
    language: str
    extensions: tuple[str, ...]
    parse: Callable[[str], Node]
    #: ``(path, source, flag_key, accessors, const_ok, external)`` -> whether a
    #: call node is a read of this flag that this run folds.
    read_test: Callable[..., Callable[[Node], bool]]
    #: ``(path, source, is_read)`` -> the wrappers the file declares.
    discover: Callable[[str, str, Callable[[Node], bool]], list[Wrapper]]
    #: ``(path, source, wrapper, index)`` -> every mention of the wrapper.
    occurrences: Callable[[str, str, Wrapper, wrapper_jvm.Index], list[Occurrence]] | None = None
    #: Languages whose files this one's index parses alongside its own, and
    #: whose callers are found with their own profile and reported
    #: ``other-language`` (Java for Kotlin, and the reverse).
    peers: tuple[str, ...] = ()
    #: ``(repo_dir, wrapper)`` -> the repository index; ``None`` for the JVM one.
    index: Callable[[str, Wrapper], wrapper_jvm.Index] | None = None
    #: ``(wrapper, index)`` -> the wrapper with its interface member, or why
    #: it does not qualify (a :class:`Refused` when the cause lies elsewhere);
    #: ``None`` for the JVM rules.
    qualify: Callable[[Wrapper, wrapper_jvm.Index], tuple[Wrapper | None, str | Refused | None]] | None = None
    #: String literal node types, where a mention is reflection or an
    #: expression language rather than a call.
    strings: frozenset[str] = _STRINGS
    #: Suffixes of the languages that can call the wrapper but that this tool
    #: never rewrites; ``None`` for the JVM guests.
    foreign_extensions: tuple[str, ...] | None = None
    #: ``(identifier, kind)`` -> whether it only names something (its own
    #: declaration, a named-argument label); ``None`` for the JVM rules.
    names_only: Callable[[Node, str], bool] | None = None
    #: ``(root, name)`` -> the member declarations of ``name`` a close-out may
    #: delete; ``None`` for the JVM ones.
    members: Callable[[Node, str], list[Node]] | None = None
    #: ``(member)`` -> the byte its deletion ends at, when the grammar keeps a
    #: separator that goes with it outside the node (a TypeScript class field's
    #: ``;``, an object literal member's ``,``); ``None``: the node's own end.
    member_end: Callable[[Node], int] | None = None
    #: Node types that spell the wrapper's name where it is used, for the
    #: close-out's leftover scan. Go spells ``pkg.F`` and ``s.M`` with a
    #: ``field_identifier``.
    mention_types: frozenset[str] = frozenset({"identifier"})
    #: ``(wrapper)`` -> why a wrapper is kept even when every caller folds, or
    #: ``None``. A Go exported method may satisfy an interface declared
    #: outside the repository.
    keep: Callable[[Wrapper], str | None] | None = None
    #: Languages of the same family whose files this language's run rewrites
    #: itself when a caller there is admitted (``.tsx`` callers of a ``.ts``
    #: wrapper): their calls are found with their own profile, and count as
    #: this language's rather than ``other-language``.
    siblings: tuple[str, ...] = ()
    #: Annotation names that say nothing about who calls a member. Empty for
    #: TypeScript and JavaScript, where a decorator is a function of the
    #: codebase's own: ``@Override`` there can register the method anywhere.
    inert_annotations: frozenset[str] = _INERT_ANNOTATIONS
    #: ``(index, path, kind)`` -> whether a mention of ``kind`` (``"string"``,
    #: a string literal of a parsed file; ``"unparsed"``, a file nothing here
    #: parses) can be the wrapper's member; ``None``: every one can.
    mention_counts: Callable[[wrapper_jvm.Index, str, str], bool] | None = None
    #: ``(repo_dir, wrapper)`` -> every remaining mention after the fold, the
    #: profile's own judgement; ``None`` for the shared scan.
    leftovers: Callable[[str, Wrapper], list[tuple[str, int, str]]] | None = None
    #: ``(wrapper, index)`` -> resolved path -> where a test may stub the
    #: wrapper there, for the test-double pass that runs before the proof;
    #: ``None`` for languages whose doubles are found per call
    #: (:attr:`Occurrence.in_test_double`).
    stub_targets: Callable[[Wrapper, Any], dict[str, mock_stubs.StubTargets]] | None = None
    #: ``(wrapper, index, treatment)`` -> what the test-double pass is about to
    #: settle (class mocks whose stub of the wrapper it deletes), handed back
    #: to ``index`` as its third argument when the round indexes its result;
    #: ``None`` where nothing is.
    settled_mocks: Callable[[Wrapper, Any, bool], Any] | None = None
    #: ``(path, source, string node)`` -> whether the string is a test's
    #: title, which a test runner consumes and so never names a member;
    #: ``None`` where no string is exempt.
    test_title: Callable[[str, str, Node], bool] | None = None


_PROFILES: dict[str, Profile] = {}


def register(profile: Profile) -> None:
    _PROFILES[profile.language] = profile


def profile(language: str) -> Profile | None:
    """The language's profile, or ``None`` for a language not covered yet."""
    from flag_cleanup import wrapper_csharp, wrapper_go, wrapper_java, wrapper_kotlin, wrapper_ts  # noqa: F401 - registration

    return _PROFILES.get(language)


def _profile_for(path: str) -> Profile | None:
    """The profile that parses ``path``, by its suffix."""
    profile("java")  # registration
    return next((p for p in _PROFILES.values() if path.endswith(p.extensions)), None)


def language_for(path: str) -> str | None:
    """The language whose profile parses ``path``, by its suffix."""
    lang = _profile_for(path)
    return None if lang is None else lang.language


def line_of(node: Node) -> int:
    return node.start_point[0] + 1


def text_of(node: Node) -> str:
    return " ".join(node.text.decode("utf-8", "replace").split())


def _read(path: str) -> str | None:
    """The file's text with its line endings as they are, so every byte
    offset taken from it is one into the file, and a file written back from
    it keeps CRLF."""
    from flag_cleanup import mock_stubs

    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def discover_all(
    language: str,
    flag_key: str,
    accessors: tuple[str, ...],
    folded_files: dict[str, tuple[bool, frozenset[str]]],
) -> list[Wrapper]:
    """Every wrapper declared in a file whose reads this run folds.

    ``folded_files`` maps each such file to ``(const_ok, external)``: whether
    Gate 2 let the same-file const rules run there, and the texts the
    cross-file resolution proved name the key there. A read the run does not
    fold never makes a wrapper, so a caller is never folded while its wrapper
    keeps reading the flag.
    """
    found: list[Wrapper] = []
    lang = profile(language)
    if lang is None:
        return found
    for path, (const_ok, external) in sorted(folded_files.items()):
        source = _read(path)
        if source is None:
            continue
        is_read = lang.read_test(path, source, flag_key, accessors, const_ok, external)
        found.extend(lang.discover(str(Path(path).resolve()), source, is_read))
    return found


def _sibling_note(sibling: Sibling, member: str, value: str) -> str:
    """What a deleted sibling's member returned, and what its callers get now.

    An auto-property returns no expression to quote: it is named, with the
    ``false`` it holds until something sets it."""
    if sibling.returns == "auto-property":
        return f"{sibling.owner} returned its auto-property `{member}` (default `false`); now `{value}`"
    from flag_cleanup import mock_stubs

    return f"{sibling.owner} returned {mock_stubs.code_span(sibling.returns)}; now `{value}`"


def _fixed_at_removed_value(sibling: Sibling, treatment: bool) -> bool:
    """Whether ``sibling`` is fixed at the value being removed: a literal, or
    an auto-property left at its ``false`` default. Whatever builds it --
    production code, or a test through any alias, factory, fixture or
    language -- relies on the branch being removed, and nothing here can see
    all of what builds it, so such a sibling never goes. A test fake whose
    knob DEFAULTS to the removed value is the same: a test that builds it and
    never sets the knob runs the removed branch."""
    fixed = {"true": True, "false": False, "auto-property": False}.get(sibling.returns)
    if fixed is None and sibling.knob is not None:
        fixed = {"true": True, "false": False}.get(sibling.knob_default or "")
    return fixed is not None and fixed != treatment


def qualify_all(
    repo_dir: str, wrappers: list[Wrapper], treatment: bool | None = None
) -> tuple[list[tuple[Wrapper, wrapper_jvm.Index]], list[Finding]]:
    """The wrappers that qualify, each with the index it was judged on, and a
    declaration-site finding for each one that does not.

    With the run's ``treatment``, a wrapper with a sibling fixed at the
    removed value (:func:`_fixed_at_removed_value`) does not qualify either,
    reported ``sibling-not-inert`` at the sibling."""
    from flag_cleanup import wrapper_jvm

    qualified: list[tuple[Wrapper, wrapper_jvm.Index]] = []
    findings: list[Finding] = []
    for wrapper in wrappers:
        lang = profile(wrapper.language)
        build = lang.index if lang is not None and lang.index is not None else wrapper_jvm.Index.build
        judge = lang.qualify if lang is not None and lang.qualify is not None else wrapper_jvm.qualify
        index = build(repo_dir, wrapper)
        if treatment is not None and hasattr(index, "treatment"):
            # A profile that reports what its test doubles leave behind needs
            # to know which value stays.
            index.treatment = treatment
        member, reason = judge(wrapper, index)
        if member is None:
            if isinstance(reason, Refused):
                findings.append(Finding(reason.path, reason.line, reason.reason, reason.text, wrapper.label))
            else:
                findings.append(
                    Finding(wrapper.path, wrapper.line, reason or NAME_NOT_UNIQUE, wrapper.label, wrapper.label)
                )
            continue
        fixed = (
            [s for s in member.siblings if _fixed_at_removed_value(s, treatment)] if treatment is not None else []
        )
        if fixed:
            findings += [
                Finding(s.path, s.line, SIBLING_NOT_INERT, f"{s.owner} returns `{s.returns}`", wrapper.label)
                for s in fixed
            ]
            continue
        qualified.append((member, index))
    return qualified, findings


def _occurrences_in(
    language: str, path: str, wrapper: Wrapper, index: wrapper_jvm.Index
) -> tuple[bool, list[Occurrence]]:
    """``(this run's, occurrences)`` for one parsed file of the index: a file
    of this language or of a sibling is this run's, a peer's is not."""
    same = profile(language)
    lang = next(
        (
            p
            for p in (
                same,
                *(profile(s) for s in same.siblings),
                *(profile(peer) for peer in same.peers),
            )
            if p is not None and path.endswith(p.extensions)
        ),
        same,
    )
    source = _read(path)
    ours = lang is same or lang.language in same.siblings
    if source is None:
        return ours, [Occurrence(path, 1, "", None, RECEIVER_UNPROVEN)]
    return ours, lang.occurrences(path, source, wrapper, index)


def stub_receivers(
    language: str, qualified: list[tuple[Wrapper, wrapper_jvm.Index]]
) -> dict[str, frozenset[str]]:
    """path -> receivers of test-double statements whose binding was proven.

    Collected before the test-double pass, which may only resolve a stub
    whose receiver is the wrapper itself.

    C#: that pass matches a stub of ANY of the run's wrappers on the name, so
    a name stays only where every wrapper stubbed through it in the file
    proved it there too.
    """
    found: dict[str, set[str]] = {}
    proven: dict[tuple[str, str], set[str]] = {}
    for wrapper, index in qualified:
        for path in index.parsed:
            same, occurrences = _occurrences_in(language, path, wrapper, index)
            if not same:
                continue
            for o in occurrences:
                if o.in_test_double and o.receiver and o.reason in (None, ARGUMENT_HAS_CALL):
                    found.setdefault(path, set()).add(o.receiver)
                    proven.setdefault((wrapper.label, path), set()).add(o.receiver)
    if language != "csharp":
        return {path: frozenset(names) for path, names in found.items()}
    from flag_cleanup import mock_stubs

    result: dict[str, frozenset[str]] = {}
    for path, names in found.items():
        source = _read(path) or ""
        kept = frozenset(
            name
            for name in names
            if all(
                name in proven.get((wrapper.label, path), ())
                # Deciding what STAYS: any spelling counts, qualified or not.
                or not mock_stubs.csharp_mock_mention(name, wrapper.name).search(source)
                for wrapper, _ in qualified
            )
        )
        if kept:
            result[path] = kept
    return result


def sibling_receivers(
    language: str, qualified: list[tuple[Wrapper, wrapper_jvm.Index]]
) -> dict[str, frozenset[str]]:
    """path -> receivers typed as a sibling there, read inside a test method.

    For the test-double pass, which deletes a test reading the member through
    one: the sibling's member goes with the wrapper. Keyed by file, since a
    name proves nothing outside the file whose binding was proven.

    That pass matches a read of ANY of the run's wrappers through the name,
    so a name stays only where every wrapper read through it in the file
    proved it too (the profile drops a name it cannot prove for the whole
    file).
    """
    found: dict[str, set[str]] = {}
    proven: dict[tuple[str, str], set[str]] = {}
    for wrapper, index in qualified:
        for path in index.parsed:
            same, occurrences = _occurrences_in(language, path, wrapper, index)
            if not same:
                continue
            for o in occurrences:
                if o.reason == SIBLING_RECEIVER and o.in_test_double and o.receiver:
                    found.setdefault(path, set()).add(o.receiver)
                    proven.setdefault((wrapper.label, path), set()).add(o.receiver)
    from flag_cleanup import mock_stubs

    result: dict[str, frozenset[str]] = {}
    for path, names in found.items():
        source = _read(path) or ""
        kept = frozenset(
            name
            for name in names
            if all(
                name in proven.get((wrapper.label, path), ())
                # Deciding what STAYS: any spelling counts, qualified or not.
                or not mock_stubs.csharp_receiver_read({name}, {wrapper.name}, reads_only=False).search(source)
                for wrapper, _ in qualified
            )
        )
        if kept:
            result[path] = kept
    return result


def fake_settings(
    language: str, qualified: list[tuple[Wrapper, wrapper_jvm.Index]]
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[tuple[str, str, bool]]], frozenset[str]]:
    """C#: ``(path -> knob receivers, path -> fake owners, test paths)`` for
    the test-double pass: the receivers proven typed as a test fake and
    written through its knob, the fakes whose simple name is proven to mean
    them where they are built, and the files of a test project, where those
    and helper pins may be resolved. Judged against every qualified
    wrapper's fakes at once, since the pass matches every knob name of the
    run on every receiver it is given."""
    if language != "csharp":
        return {}, {}, frozenset()
    from flag_cleanup import wrapper_csharp

    fakes = wrapper_csharp.fakes_of(w for w, _ in qualified)
    paths = sorted({path for _, index in qualified for path in index.parsed})
    receivers: dict[str, frozenset[str]] = {}
    owners: dict[str, frozenset[tuple[str, str, bool]]] = {}
    tests: set[str] = set()
    repo_dir = qualified[0][1].repo_dir if qualified else ""
    for path in paths:
        if wrapper_csharp._test_project(path, repo_dir) is not True:
            continue
        tests.add(path)
        source = _read(path)
        if source is None or not fakes:
            continue
        names = wrapper_csharp.knob_receivers(path, source, fakes, repo_dir)
        if names:
            receivers[path] = names
        built = wrapper_csharp.knob_owners(path, source, fakes, repo_dir)
        if built:
            owners[path] = built
    return receivers, owners, frozenset(tests)


def _foreign_lines(path: str, name: str) -> list[tuple[int, str]]:
    """Every line of ``path`` spelling ``name`` as a word, in any case where
    the file's language ignores it (VB), since that is how it was found."""
    from flag_cleanup import cross_file_keys

    source = _read(path)
    if source is None:
        return [(1, "")]
    flags = re.IGNORECASE if cross_file_keys._case_insensitive(path) else 0
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])", flags)
    return [
        (number, " ".join(line.split()))
        for number, line in enumerate(source.splitlines(), start=1)
        if word.search(line)
    ]


def _line_at(path: str, offset: int) -> int:
    source = _read(path)
    return 1 if source is None else source.encode("utf-8")[:offset].count(b"\n") + 1


def _string_mentions(path: str, name: str, templates: bool = False) -> list[tuple[int, str]]:
    """String literals in a parsed file that spell ``name`` as a word.

    With ``templates`` a brace-less ``$name`` interpolation counts too.
    """
    from flag_cleanup import cross_file_keys

    source = _read(path)
    root = _parse_for(path, source) if source is not None else None
    if root is None:
        return []
    lang = _profile_for(path)
    prefix = r"\$?" if templates else ""
    word = re.compile(rf"(?<![\w$]){prefix}{re.escape(name)}(?![\w$])")
    return [
        (line_of(node), text_of(node))
        for node in cross_file_keys.walk(root)
        if node.type in lang.strings
        and word.search(node.text.decode("utf-8", "replace"))
        and not (lang.test_title is not None and lang.test_title(path, source, node))
    ]


def prove_all(
    language: str,
    qualified: list[tuple[Wrapper, wrapper_jvm.Index]],
    findings: list[Finding],
    rewritable: Callable[[str], bool],
) -> WrapperPlan:
    """Prove every call site of every qualified wrapper.

    Asked after the test-double pass, so a statement of a mocking DSL still
    mentioning a wrapper is one that pass could not resolve. ``rewritable``
    says whether this run may rewrite a file: a proven call it may not is
    left, and reported, like any other.
    """
    plan = WrapperPlan(findings=list(findings))
    lang = profile(language)
    counts = lang.mention_counts if lang is not None else None
    kept: list[Wrapper] = []
    incomplete: set[str] = set()
    receivers: dict[str, set[str]] = {}
    for wrapper, index in qualified:
        refused: list[Finding] = []
        for path in index.foreign:
            refused += [
                Finding(path, line, OTHER_LANGUAGE, text, wrapper.label)
                for line, text in _foreign_lines(path, wrapper.name)
            ]
        for path in index.unparsed:
            if counts is None or counts(index, path, "unparsed"):
                refused += [
                    Finding(path, line, UNPARSED_MENTION, text, wrapper.label)
                    for line, text in _foreign_lines(path, wrapper.name)
                ]
        refused += [
            Finding(path, line, OTHER_LANGUAGE, text, wrapper.label)
            for path, line, text in index.synthetic
        ]
        # A module object handed on can call the wrapper by a computed name
        # (TypeScript and JavaScript only).
        refused += [
            Finding(path, line, RECEIVER_UNPROVEN, text, wrapper.label)
            for path, line, text in getattr(index, "escapes", ())
        ]
        if wrapper.interface is not None and not rewritable(wrapper.interface.path):
            refused.append(
                Finding(
                    wrapper.interface.path,
                    _line_at(wrapper.interface.path, wrapper.interface.start),
                    OUTSIDE_RUN,
                    f"{wrapper.interface.name}.{wrapper.name}",
                    wrapper.label,
                )
            )
        marks: list[tuple[str, kotlin_sentinel.Span, str | None]] = []
        for path in index.parsed:
            if counts is None or counts(index, path, "string"):
                refused += [
                    Finding(path, line, UNPARSED_MENTION, text, wrapper.label)
                    for line, text in _string_mentions(path, wrapper.name, wrapper.kind == "property")
                ]
            same, occurrences = _occurrences_in(language, path, wrapper, index)
            for o in occurrences:
                if not same:
                    reason = OTHER_LANGUAGE
                elif o.in_test_double:
                    reason = TEST_DOUBLE
                elif o.reason is not None:
                    reason = o.reason
                elif not rewritable(path):
                    reason = OUTSIDE_RUN
                else:
                    span = (*o.span, o.suffix) if o.suffix else o.span
                    marks.append((path, span, o.receiver))
                    continue
                refused.append(Finding(path, o.line, reason, o.text, wrapper.label))
        if not marks and not refused:
            # Nothing mentions it at all: there is no caller to inline, and a
            # member with no caller in the repository may still be called from
            # outside it. The fold inside it is the whole removal, as before.
            continue
        inert = lang.inert_annotations if lang is not None else _INERT_ANNOTATIONS
        framework = [a for a in wrapper.annotations if a not in inert]
        if framework:
            refused.insert(
                0, Finding(wrapper.path, wrapper.line, ANNOTATED, wrapper.label, wrapper.label)
            )
        reason_kept = lang.keep(wrapper) if lang is not None and lang.keep is not None else None
        if reason_kept:
            refused.insert(0, Finding(wrapper.path, wrapper.line, reason_kept, wrapper.label, wrapper.label))
        kept.append(wrapper)
        for path, span, receiver in marks:
            plan.spans.setdefault(path, []).append(span)
            if receiver:
                receivers.setdefault(path, set()).add(receiver)
        if refused:
            incomplete.add(wrapper.label)
        plan.findings += refused
    plan.wrappers = tuple(kept)
    plan.receivers = {path: frozenset(names) for path, names in receivers.items()}
    plan.complete = frozenset(w.label for w in plan.wrappers) - incomplete
    return plan


def candidate_names(
    paths: Iterable[str], language: str, flag_key: str, accessors: tuple[str, ...]
) -> set[str]:
    """Names of members that may be wrappers of this flag, for the undo snapshot.

    Judged loosely -- a read whose first argument is any name counts -- since
    the cross-file resolution has not run yet. A superset only widens the
    snapshot; which wrappers are real is decided during the transform.
    """
    lang = profile(language)
    if lang is None:
        return set()
    names: set[str] = set()
    for path in paths:
        if not path.endswith(lang.extensions):
            continue
        source = _read(path)
        if source is None:
            continue
        is_read = lang.read_test(path, source, flag_key, accessors, True, (), loose=True)
        names.update(w.name for w in lang.discover(path, source, is_read))
    return names


# --- Close-out ------------------------------------------------------------------


def _parse_for(path: str, source: str) -> Node | None:
    lang = _profile_for(path)
    return lang.parse(source) if lang is not None else None


_DECLARATIONS = ("function_declaration", "method_declaration", "property_declaration")


def _declaration_name(node: Node, kind: str = "method", path: str = "") -> bool:
    """Whether ``node`` only names something rather than referring to it.

    A method wrapper's own declaration is the only name-only mention. A getter
    or a function wrapper also ignores the declared name of a property, local
    or parameter and a named-argument label (``wrapper_kotlin.names_only``).
    A profile with its own rule (``Profile.names_only``) answers for its files.
    """
    from flag_cleanup import wrapper_kotlin

    lang = _profile_for(path) if path else None
    if lang is not None and lang.names_only is not None:
        return lang.names_only(node, kind)
    parent = node.parent
    if parent is None:
        return False
    if kind != "method" and wrapper_kotlin.names_only(node):
        return True
    if parent.type not in _DECLARATIONS or (kind == "method" and parent.type == "property_declaration"):
        return False
    name = wrapper_kotlin.declared_name(parent)
    return name is not None and name.id == node.id


#: What may follow an import on its line for the line to go with it: a ``;``
#: and a comment that ends there.
_IMPORT_TAIL = re.compile(rb"[ \t]*;?[ \t]*(?://[^\n]*|/\*(?:(?!\*/)[^\n])*\*/[ \t]*)?\r?")


def _import_line(node: Node, data: bytes) -> tuple[int, int] | None:
    """Byte span of the whole line(s) an ``import`` stands on, or ``None``
    when they hold anything else (another import, a declaration), which
    deleting them would delete too."""
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    end = data.find(b"\n", node.end_byte)
    end = len(data) if end < 0 else end
    if data[start : node.start_byte].strip() or not _IMPORT_TAIL.fullmatch(data, node.end_byte, end):
        return None
    return start, min(end + 1, len(data))


def _is_function_import(node: Node, wrapper: Wrapper) -> bool:
    """Whether ``node`` is ``import <package>.<name>``: an explicit, unaliased
    import of a top-level function wrapper."""
    from flag_cleanup import wrapper_kotlin

    parts = wrapper_kotlin.import_parts(node)
    return (
        parts is not None
        and parts[1] is None
        and not parts[2]
        and parts[0] == wrapper_kotlin.wrapper_fqn(wrapper)
    )


def _function_imports(path: str, wrapper: Wrapper) -> list[tuple[int, int]]:
    """Byte spans (whole lines) of the wrapper's imports in ``path`` that
    stand on lines of their own; :func:`_leftovers` keeps the wrapper while
    any other one remains."""
    from flag_cleanup import wrapper_kotlin

    source = _read(path)
    if source is None or not path.endswith(wrapper_kotlin.EXTENSIONS):
        return []
    root = wrapper_kotlin.parse(source)
    data = source.encode("utf-8")
    spans: list[tuple[int, int]] = []
    for node in root.named_children:
        if node.type == "import" and _is_function_import(node, wrapper):
            span = _import_line(node, data)
            if span is not None:
                spans.append(span)
    return spans


def _deleted_import(node: Node, data: bytes, wrapper: Wrapper) -> bool:
    """Whether an identifier lies in an import the close-out deletes, or in
    one that does not import the wrapper at all."""
    statement = node.parent
    while statement is not None and statement.type != "import":
        statement = statement.parent
    if statement is None:
        return False
    return not _is_function_import(statement, wrapper) or _import_line(statement, data) is not None


def _leftovers(
    repo_dir: str, wrapper: Wrapper, removable: frozenset[str] = frozenset()
) -> list[tuple[str, int, str]]:
    """Every mention of the wrapper's name still in the repository, read fresh.

    Parsed where this tool can parse the file, so a comment does not count,
    and declarations of the name do not count either (the wrapper's own and
    its interface member's are the only ones, by qualification). A string
    literal spelling it does count (reflection, an expression language), and
    so does any word-boundary match in a file this tool cannot parse. For a
    top-level function, an import in a ``removable`` file does not count: the
    close-out deletes it. A getter is also read from Java as ``getX()``.
    A profile with its own judgement (``Profile.leftovers``) answers instead.
    """
    from flag_cleanup import cross_file_keys, wrapper_jvm

    lang = profile(wrapper.language)
    if lang.leftovers is not None:
        return lang.leftovers(repo_dir, wrapper)
    foreign = (
        lang.foreign_extensions if lang.foreign_extensions is not None else wrapper_jvm.FOREIGN_EXTENSIONS
    )
    parsed = tuple(
        ext
        for p in (lang, *(profile(s) for s in lang.siblings), *(profile(peer) for peer in lang.peers))
        if p is not None
        for ext in p.extensions
    )
    found: list[tuple[str, int, str]] = []
    for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=()):
        if path.endswith(foreign) or not path.endswith(parsed):
            found += [(path, line, text) for line, text in _foreign_lines(path, wrapper.name)]
            continue
        source = _read(path)
        root = _parse_for(path, source) if source is not None else None
        if root is None:
            found.append((path, 1, ""))
            continue
        found += [
            (path, line, text)
            for line, text in _string_mentions(path, wrapper.name, wrapper.kind == "property")
        ]
        data = source.encode("utf-8")
        for node in cross_file_keys.walk(root):
            if (
                node.type in lang.mention_types
                # C#'s verbatim `@Name` is the same name.
                and node.text.decode("utf-8", "replace").removeprefix("@") == wrapper.name
                and not _declaration_name(node, wrapper.kind, path)
                and not (
                    wrapper.kind == "function"
                    and path in removable
                    and _deleted_import(node, data, wrapper)
                )
            ):
                found.append((path, line_of(node), text_of(node.parent or node)))
    if wrapper.kind == "property":
        found += wrapper_jvm.jvm_getter_calls(repo_dir, wrapper.name)
    return found


def _without_member(path: str, name: str, expected: int = 1) -> tuple[str, str] | None:
    """``(before, after)`` for ``path`` with its declarations of ``name``
    deleted, whole lines and their leading comments with them; ``None``
    unless exactly ``expected`` are found (two when the wrapper and the
    interface member it implements share the file)."""
    from flag_cleanup import cross_file_keys, mock_stubs, unused, wrapper_kotlin

    source = _read(path)
    root = _parse_for(path, source) if source is not None else None
    if root is None:
        return None
    lang = _profile_for(path)
    if lang is not None and lang.members is not None:
        members = lang.members(root, name)
    else:
        members = [
            n
            for n in cross_file_keys.walk(root)
            if n.type in _DECLARATIONS
            and (declared := wrapper_kotlin.declared_name(n)) is not None
            and declared.text.decode("utf-8", "replace") == name
            and (
                n.type != "property_declaration"
                or (n.parent is not None and n.parent.type in wrapper_kotlin.MEMBER_SCOPES)
            )
        ]
    if len(members) != expected:
        return None
    data = source.encode("utf-8")
    after = source
    for member in sorted(members, key=lambda n: n.start_byte, reverse=True):
        start = mock_stubs._with_leading_comments(member, data)
        end = _member_end(lang, member)
        text = data[start:end].decode("utf-8", "replace")
        after = unused._delete_method(after, unused.Candidate(name, start, end, "method", text))
    return source, after


def _member_end(lang: Profile | None, member: Node) -> int:
    return lang.member_end(member) if lang is not None and lang.member_end is not None else member.end_byte


def _without_spans(path: str, spans: list[tuple[int, int]], name: str) -> tuple[str, str] | None:
    """``(before, after)`` for ``path`` with exactly these declarations of
    ``name`` deleted, whole lines and their leading comments with them;
    ``None`` when a span no longer starts and ends one there."""
    from flag_cleanup import mock_stubs, unused

    source = _read(path)
    root = _parse_for(path, source) if source is not None else None
    lang = _profile_for(path)
    if root is None or lang is None or lang.members is None:
        return None
    nodes = {n.start_byte: n for n in lang.members(root, name)}
    data = source.encode("utf-8")
    after = source
    for start, end in sorted(set(spans), reverse=True):
        member = nodes.get(start)
        if member is None or member.end_byte != end:
            return None
        begin = mock_stubs._with_leading_comments(member, data)
        finish = _member_end(lang, member)
        text = data[begin:finish].decode("utf-8", "replace")
        after = unused._delete_method(after, unused.Candidate(name, begin, finish, "method", text))
    return source, after


def _current_span(sibling: Sibling, summaries: list) -> tuple[int, int] | None:
    """The sibling member's span in its file as it is now. A file this run
    rewrote has moved: the member is found again by its own bytes, which
    must still occur exactly once; ``None`` otherwise."""
    summary = next(
        (s for s in summaries if str(Path(s.path).resolve()) == sibling.path and s.original_content != s.content),
        None,
    )
    if summary is None:
        return sibling.start, sibling.end
    member = summary.original_content.encode("utf-8")[sibling.start : sibling.end]
    current = summary.content.encode("utf-8")
    if not member or current.count(member) != 1:
        return None
    start = current.index(member)
    return start, start + len(member)


def close_out(
    plan: WrapperPlan,
    repo_dir: str,
    summaries: list,
    unprocessable: Iterable[str],
    rewrite: Callable,
    treatment: bool = True,
) -> tuple[list, list[Finding]]:
    """Delete each wrapper nothing calls any more; report every wrapper.

    A wrapper goes only when it is ``complete`` -- every mention was a proven
    call, so every one was marked and folded -- its own read was folded (its
    file was rewritten, and not left unprocessed), and a fresh re-scan of the
    repository finds its name nowhere else. Its interface member goes with it,
    since an implementation cannot be removed from under one, and so does
    every inert sibling's member, each reported as a ``sibling`` row with
    what it returned and the ``treatment`` value its callers now see. Anything
    short of that keeps them all, and the wrapper is ``partial``; so does a
    sibling sharing the wrapper's or the interface's file, which one edit
    there already holds, and one whose file this run rewrote so that its
    member cannot be found again by its own bytes (``sibling-not-inert``).

    Returns the summaries with the deletions in them, written to disk, and
    every finding: one ``complete``/``partial`` row per wrapper first, then
    each refusal.
    """
    if not plan.wrappers:
        return summaries, list(plan.findings)
    summaries = list(summaries)
    unprocessed = {str(Path(p).resolve()) for p in unprocessable}
    statuses: list[Finding] = []
    extra: list[Finding] = []
    for wrapper in plan.wrappers:
        status = PARTIAL
        index = next(
            (i for i, s in enumerate(summaries) if str(Path(s.path).resolve()) == wrapper.path),
            None,
        )
        folded = (
            index is not None
            and summaries[index].original_content != summaries[index].content
            and wrapper.path not in unprocessed
        )
        if wrapper.label in plan.complete and not folded:
            # Every caller was proven and folded, but the wrapper's own read
            # was not: its file could not be processed.
            extra.append(Finding(wrapper.path, wrapper.line, NOT_FOLDED, wrapper.label, wrapper.label))
        if wrapper.label in plan.complete and folded:
            rewritten = frozenset(
                str(Path(s.path).resolve()) for s in summaries if s.original_content != s.content
            )
            removable = rewritten if wrapper.kind == "function" else frozenset()
            leftovers = _leftovers(repo_dir, wrapper, removable)
            if leftovers:
                logger.warning(
                    "keyless wrapper %s still has %d mention(s) after its callers were "
                    "folded, so it is kept", wrapper.label, len(leftovers),
                )
                extra += [
                    Finding(path, line, RECEIVER_UNPROVEN, text, wrapper.label)
                    for path, line, text in leftovers
                ]
            else:
                together = wrapper.interface is not None and wrapper.interface.path == wrapper.path
                edits = [(wrapper.path, _without_member(wrapper.path, wrapper.name, 2 if together else 1))]
                if wrapper.interface is not None and not together:
                    edits.append(
                        (wrapper.interface.path, _without_member(wrapper.interface.path, wrapper.name))
                    )
                by_path: dict[str, list[tuple[int, int] | None]] = {}
                for sibling in wrapper.siblings:
                    by_path.setdefault(sibling.path, []).append(_current_span(sibling, summaries))
                held = {wrapper.path} | ({wrapper.interface.path} if wrapper.interface is not None else set())
                moved: list[Finding] = []
                for sibling_path, spans in sorted(by_path.items()):
                    # A sibling in the wrapper's or the interface's file:
                    # that file's edit counts the declarations of the name,
                    # so refuse rather than edit it twice. Qualification
                    # already refuses one; this stays as the belt.
                    if sibling_path in held or None in spans:
                        edit = None
                    else:
                        edit = _without_spans(sibling_path, [s for s in spans if s is not None], wrapper.name)
                    if edit is None:
                        moved += [
                            Finding(s.path, s.line, SIBLING_NOT_INERT, f"{s.owner}.{wrapper.name}", wrapper.label)
                            for s in wrapper.siblings
                            if s.path == sibling_path
                        ]
                    edits.append((sibling_path, edit))
                if wrapper.kind == "function":
                    for path in sorted(removable - {wrapper.path}):
                        spans = _function_imports(path, wrapper)
                        if spans:
                            before = _read(path)
                            raw = before.encode("utf-8")
                            for start, end in sorted(spans, reverse=True):
                                raw = raw[:start] + raw[end:]
                            edits.append((path, (before, raw.decode("utf-8"))))
                if not all(edit is not None for _, edit in edits):
                    # A sibling member this run moved out of reach, or one
                    # in a file another edit holds, is named at the sibling;
                    # anything else is a second declaration of the name
                    # where one was expected.
                    others = all(e is not None for p, e in edits if p not in by_path)
                    if moved and others:
                        extra += moved
                    else:
                        extra.append(
                            Finding(wrapper.path, wrapper.line, NAME_NOT_UNIQUE, wrapper.label, wrapper.label)
                        )
                else:
                    from flag_cleanup import mock_stubs

                    for path, (before, after) in edits:
                        mock_stubs._write_exact(path, after)
                        at = next(
                            (i for i, s in enumerate(summaries) if str(Path(s.path).resolve()) == path),
                            None,
                        )
                        if at is None:
                            summaries.append(rewrite(path, before, after))
                        else:
                            old = summaries[at]
                            summaries[at] = rewrite(old.path, old.original_content, after)
                    status = COMPLETE
                    value = "true" if treatment else "false"
                    extra += [
                        Finding(s.path, s.line, SIBLING, _sibling_note(s, wrapper.name, value), wrapper.label)
                        for s in wrapper.siblings
                    ]
        statuses.append(Finding(wrapper.path, wrapper.line, status, wrapper.label, wrapper.label))
    return summaries, statuses + list(plan.findings) + extra
