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
  interface has exactly one implementation and cannot be implemented outside
  the repository.

Anything unproven is left exactly as it is and reported, with its reason.

The shared loop lives here; what a declaration, a receiver and an import look
like lives in one profile per language (:mod:`flag_cleanup.wrapper_kotlin`,
:mod:`flag_cleanup.wrapper_java`, :mod:`flag_cleanup.wrapper_csharp`), with
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node

    from flag_cleanup import kotlin_sentinel, wrapper_jvm

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
#: Never produced for the JVM languages, whose interfaces are nominal.
INTERFACE_STRUCTURAL = "interface-structural"
#: Nothing in the file ties the call's receiver to the wrapper.
RECEIVER_UNPROVEN = "receiver-unproven"
#: The receiver's declared type does not resolve to the wrapper's class.
IMPORT_UNRESOLVED = "import-unresolved"
#: An argument holds a call or an assignment, which inlining would drop.
ARGUMENT_HAS_CALL = "argument-has-call"
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
#: The read inside the wrapper was not folded in this run (its file could not
#: be processed), so the wrapper stays as it was.
NOT_FOLDED = "not-folded"

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


@dataclass(frozen=True)
class InterfaceMember:
    """The interface member a wrapper implements, deleted along with it."""

    path: str
    name: str
    package: str
    start: int
    end: int


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
    #: Inside a statement of a mocking DSL.
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
    #: it does not qualify; ``None`` for the JVM rules.
    qualify: Callable[[Wrapper, wrapper_jvm.Index], tuple[Wrapper | None, str | None]] | None = None
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


_PROFILES: dict[str, Profile] = {}


def register(profile: Profile) -> None:
    _PROFILES[profile.language] = profile


def profile(language: str) -> Profile | None:
    """The language's profile, or ``None`` for a language not covered yet."""
    from flag_cleanup import wrapper_csharp, wrapper_java, wrapper_kotlin  # noqa: F401 - registration

    return _PROFILES.get(language)


def _profile_for(path: str) -> Profile | None:
    """The profile that parses ``path``, by its suffix."""
    profile("java")  # registration
    return next((p for p in _PROFILES.values() if path.endswith(p.extensions)), None)


def line_of(node: Node) -> int:
    return node.start_point[0] + 1


def text_of(node: Node) -> str:
    return " ".join(node.text.decode("utf-8", "replace").split())


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
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


def qualify_all(
    repo_dir: str, wrappers: list[Wrapper]
) -> tuple[list[tuple[Wrapper, wrapper_jvm.Index]], list[Finding]]:
    """The wrappers that qualify, each with the index it was judged on, and a
    declaration-site finding for each one that does not."""
    from flag_cleanup import wrapper_jvm

    qualified: list[tuple[Wrapper, wrapper_jvm.Index]] = []
    findings: list[Finding] = []
    for wrapper in wrappers:
        lang = profile(wrapper.language)
        build = lang.index if lang is not None and lang.index is not None else wrapper_jvm.Index.build
        judge = lang.qualify if lang is not None and lang.qualify is not None else wrapper_jvm.qualify
        index = build(repo_dir, wrapper)
        member, reason = judge(wrapper, index)
        if member is None:
            findings.append(
                Finding(wrapper.path, wrapper.line, reason or NAME_NOT_UNIQUE, wrapper.label, wrapper.label)
            )
            continue
        qualified.append((member, index))
    return qualified, findings


def _occurrences_in(
    language: str, path: str, wrapper: Wrapper, index: wrapper_jvm.Index
) -> tuple[bool, list[Occurrence]]:
    """``(same language, occurrences)`` for one parsed file of the index."""
    same = profile(language)
    lang = next(
        (
            p
            for p in (same, *(profile(peer) for peer in same.peers))
            if p is not None and path.endswith(p.extensions)
        ),
        same,
    )
    source = _read(path)
    if source is None:
        return lang is same, [Occurrence(path, 1, "", None, RECEIVER_UNPROVEN)]
    return lang is same, lang.occurrences(path, source, wrapper, index)


def stub_receivers(
    language: str, qualified: list[tuple[Wrapper, wrapper_jvm.Index]]
) -> dict[str, frozenset[str]]:
    """path -> receivers of test-double statements whose binding was proven.

    Collected before the test-double pass, which may only resolve a stub
    whose receiver is the wrapper itself.
    """
    found: dict[str, set[str]] = {}
    for wrapper, index in qualified:
        for path in index.parsed:
            same, occurrences = _occurrences_in(language, path, wrapper, index)
            if not same:
                continue
            for o in occurrences:
                if o.in_test_double and o.receiver and o.reason in (None, ARGUMENT_HAS_CALL):
                    found.setdefault(path, set()).add(o.receiver)
    return {path: frozenset(names) for path, names in found.items()}


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
    strings = _profile_for(path).strings
    prefix = r"\$?" if templates else ""
    word = re.compile(rf"(?<![\w$]){prefix}{re.escape(name)}(?![\w$])")
    return [
        (line_of(node), text_of(node))
        for node in cross_file_keys.walk(root)
        if node.type in strings and word.search(node.text.decode("utf-8", "replace"))
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
            refused += [
                Finding(path, line, UNPARSED_MENTION, text, wrapper.label)
                for line, text in _foreign_lines(path, wrapper.name)
            ]
        refused += [
            Finding(path, line, OTHER_LANGUAGE, text, wrapper.label)
            for path, line, text in index.synthetic
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
        framework = [a for a in wrapper.annotations if a not in _INERT_ANNOTATIONS]
        if framework:
            refused.insert(
                0, Finding(wrapper.path, wrapper.line, ANNOTATED, wrapper.label, wrapper.label)
            )
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
    """
    from flag_cleanup import cross_file_keys, wrapper_jvm

    lang = profile(wrapper.language)
    foreign = (
        lang.foreign_extensions if lang.foreign_extensions is not None else wrapper_jvm.FOREIGN_EXTENSIONS
    )
    parsed = tuple(
        ext for p in (lang, *(profile(peer) for peer in lang.peers)) if p is not None for ext in p.extensions
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
                node.type == "identifier"
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
        text = data[start : member.end_byte].decode("utf-8", "replace")
        after = unused._delete_method(after, unused.Candidate(name, start, member.end_byte, "method", text))
    return source, after


def close_out(
    plan: WrapperPlan,
    repo_dir: str,
    summaries: list,
    unprocessable: Iterable[str],
    rewrite: Callable,
) -> tuple[list, list[Finding]]:
    """Delete each wrapper nothing calls any more; report every wrapper.

    A wrapper goes only when it is ``complete`` -- every mention was a proven
    call, so every one was marked and folded -- its own read was folded (its
    file was rewritten, and not left unprocessed), and a fresh re-scan of the
    repository finds its name nowhere else. Its interface member goes with it,
    since an implementation cannot be removed from under one. Anything short
    of that keeps both, and the wrapper is ``partial``.

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
                    extra.append(
                        Finding(wrapper.path, wrapper.line, NAME_NOT_UNIQUE, wrapper.label, wrapper.label)
                    )
                else:
                    for path, (before, after) in edits:
                        Path(path).write_text(after, encoding="utf-8")
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
        statuses.append(Finding(wrapper.path, wrapper.line, status, wrapper.label, wrapper.label))
    return summaries, statuses + list(plan.findings) + extra
