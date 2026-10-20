"""Flag keys hoisted to a constant in ANOTHER file.

The seed rules anchor on the key, and the key-const rules resolve a hoisted
name only inside the file that declares it. A reader that imports the
constant from elsewhere names no key at all, so no rule reaches it, and the
declaring file's delete guard cannot see it either.

This module answers, per flag, three questions the engine cannot: which
declarations hold the key, which other files mention their names, and for
each such file whether every mention is a read the reader rules will remove.
It edits nothing. The runner turns its answer into a third engine invocation
for the proven readers and into Gate 2's verdict on the declaring files.

The shared loop lives here once. What differs per language -- what counts as
an exported constant, how an import or package makes a name visible, which
call shapes are reads -- lives in a small profile, and a profile never
decides whether a declaration may be deleted. That decision is this module's,
and it is the same everywhere: only when every mention in the repository that
can reach the declaration is a proven read. Which mentions can reach it is the
profiles' question -- the declaring language's, or, for a file in another
language that can name its constants, that language's own: Java and Kotlin
judge each other, and Groovy and Scala, which this tool never transforms, are
judged by a profile that proves nothing and only refuses; so are Razor, XAML,
VB and F# for C#. A file in any other language is never asked.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from tree_sitter import Node

    from flag_cleanup.source_index import SourceFile

NAME_NOT_UNIQUE = "name-not-unique"
IMPORT_UNRESOLVED = "import-unresolved"
NOT_A_READ = "not-a-read"
REBOUND = "rebound"
OTHER_LANGUAGE = "other-language"
UNSUPPORTED_FORM = "unsupported-form"
#: A proven reader left unfolded because a declaration it reads is kept by
#: another refusal (:attr:`Profile.readers_need_deletable`). Its own form is
#: fine; the refusal that keeps the declaration is listed beside it.
DECLARATION_KEPT = "declaration-kept"

#: What a declaration is. A constant can be deleted once every mention of it is
#: a proven read; an enum entry never is (see :func:`enum_findings`).
CONSTANT = "constant"
ENUM_ENTRY = "enum-entry"

#: An enum entry holds the key, but its accessor cannot be proven to return it.
ENUM_UNPROVEN = "enum-unproven"
#: A use of the whole enum (iteration, a name lookup, a class literal) that can
#: reach an entry without spelling it. Reported, never a reason to refuse.
ENUM_DYNAMIC = "enum-dynamic"
#: Not a refusal: an entry whose every read was folded, left in place.
KEPT = "kept"
#: Not a refusal either: an entry some refusal names, reported at its own
#: declaration. It stays, and so do the reads the refusals list.
LEFT = "left"
#: Like ``left``, for an entry whose only refusals are ``name-not-unique``:
#: another declaration can stand in for it, so no read through it was judged
#: and none is listed.
LEFT_COMPETING = "left-competing"


@dataclass(frozen=True)
class KeyDeclaration:
    path: str
    name: str
    owner: str | None
    module: str
    #: The name of the Kotlin companion object holding the constant
    #: (``Companion`` when it has none of its own), whose class is ``owner``.
    #: A member import has to spell it; ``Owner.NAME`` does not.
    companion: str | None = None
    kind: str = CONSTANT
    #: For an enum entry, every accessor spelling proven to return the key:
    #: ``"key()"`` for a method, ``"key"`` for a field or Kotlin property, and
    #: ``""`` for a TypeScript member, which is the key itself.
    accessors: frozenset[str] = frozenset()
    #: How an enum entry is written in its own language, for reports, when
    #: ``Owner.NAME`` would not be: a PHP owner is stored lowercased (class
    #: names fold case there), and PHP spells a case ``Owner::NAME``. Empty
    #: for every other language.
    entry_text: str = ""
    #: Declared in third-party code the run never rewrites (PHP's Composer
    #: ``vendor/``). Its reads still fold, but the pull request never lists it
    #: as safe to delete: it is not the reviewer's code.
    third_party: bool = False

    @property
    def entry(self) -> str:
        """``Owner.NAME`` for an enum entry, the name a finding is reported under."""
        if self.kind != ENUM_ENTRY:
            return ""
        return self.entry_text or f"{self.owner}.{self.name}"


@dataclass(frozen=True)
class KeyReference:
    path: str
    owner: str | None
    member: str
    #: The accessor spelling used at an enum read (``"key()"``, ``"key"``), or
    #: ``""``. Part of what a reader rule has to match.
    accessor: str = ""
    #: The enum entry the read was proven against; None for a constant.
    declaration: KeyDeclaration | None = None


@dataclass(frozen=True)
class Refusal:
    path: str
    line: int
    reason: str
    text: str
    #: ``Owner.NAME`` when the refusal is about an enum entry. Reported apart
    #: from constant refusals, whose wording ("the declaration is kept") is
    #: false for an entry, which is always kept.
    entry: str = ""


@dataclass(frozen=True)
class Resolution:
    readers: dict[str, tuple[KeyReference, ...]]
    refusals: tuple[Refusal, ...]
    deletable: frozenset[str]
    #: The unique declarations the readers were proven against, kept so the
    #: rewritten readers can be checked against the same proof afterwards.
    declarations: tuple[KeyDeclaration, ...] = ()
    #: Declaring files some refusal stops from losing a declaration. The
    #: in-file gate cannot see why (a mention elsewhere it does not recognise
    #: as one -- a subclass reading the constant bare, say), so the runner
    #: withholds the const rules from each of these whatever that gate says.
    #: Every refusal is against a declaration in one of them, which is what
    #: makes "kept, because of this line" true of every refusal reported.
    blocked: frozenset[str] = frozenset()
    #: ``enum-dynamic`` findings. They never drop a reader: a dynamic use is
    #: about the whole enum, not this entry.
    notes: tuple[Refusal, ...] = ()
    #: Every enum entry the key files declare, the voided and duplicated ones
    #: included: each is reported at its own declaration (see
    #: :func:`enum_findings`), whatever happened to its reads.
    entries: tuple[KeyDeclaration, ...] = ()
    #: :attr:`Profile.readers_need_deletable` of the profile that resolved
    #: this, so :func:`report_uncovered` applies it to what it blocks.
    readers_need_deletable: bool = False
    #: Constant findings of :attr:`Profile.entry_findings` in a declaring
    #: file no other file names a constant of, and that only stop other files
    #: trusting its value (:attr:`Profile.home_constant_names`). They keep
    #: nothing: with no reader elsewhere the file is the in-file gate's alone,
    #: folded exactly as it would be with no cross-file pass. The runner reports them only where that gate withholds
    #: the file, as the reason it stays.
    home_findings: tuple[Refusal, ...] = ()


#: ``(module, owner, name)`` -- what a reader's import or qualifier names.
QualifiedName = tuple[str, "str | None", str]


@dataclass(frozen=True)
class Profile:
    language_family: frozenset[str]
    declarations: Callable[[str, str, str], list[KeyDeclaration]]
    references: Callable[
        [str, str, tuple[KeyDeclaration, ...], frozenset[str], str],
        tuple[list[KeyReference], list[Refusal]],
    ]
    #: Every constant a file declares, as ``(module, owner, name)``, WHATEVER
    #: its value. ``declarations`` answers only for the key; this is how a
    #: second declaration of the same qualified name holding another value
    #: (one per build variant or source set, say) is seen at all. Over-reporting
    #: is safe: it only keeps a declaration and its reads.
    declared_names: Callable[[str, str], set[QualifiedName]]
    #: Every top-level type-like name a file declares, as ``(module, name)``:
    #: a class, interface, object, enum, record or type alias. A second
    #: ``Owner`` of the same qualified name can supply ``Owner.NAME`` through
    #: inheritance or an alias without ever spelling ``NAME``, so for an
    #: owner-bearing declaration the OWNER is what has to be unique.
    #:
    #: Either callable raises :class:`DeclarationsUnreadable` for a file whose
    #: parse has errors, where a declaration may sit in the part the parser
    #: could not read. A profile whose modules are its files (TS/JS) never
    #: needs to: no other file can declare a name in its module.
    declared_classifiers: Callable[[str, str], set[tuple[str, str]]]
    #: Languages outside the family whose files can still name this
    #: language's constants: the other JVM languages. A file in any other
    #: language cannot mention the declaration at all, whatever it spells, and
    #: is skipped. One of these is judged by its OWN profile: only a mention
    #: that profile would read or refuse is one.
    reaches: frozenset[str] = frozenset()
    #: Refusals about the enum entries a declaring file holds the key in and
    #: cannot prove (``enum-unproven``), or reads it cannot hand to the reader
    #: invocation (a read inside the declaring file, a nested enum). None for a
    #: language with no enum support.
    entry_findings: Callable[[str, str, str], list[Refusal]] | None = None
    #: ``(path, source, entries, repo_dir)``: the ``enum-dynamic`` notes for
    #: one file. None for a language with no enum support.
    enum_dynamic_uses: (
        Callable[[str, str, tuple[KeyDeclaration, ...], str], list[Refusal]] | None
    ) = None
    #: ``(path, source, entries, repo_dir)``: for a language whose enum is a
    #: value a file can hand on (TypeScript's), a refusal of every entry at
    #: each use of the enum's file or of the enum in ``path`` that the run
    #: cannot follow. Asked of every file of the family
    #: :attr:`enum_module_pattern` or an entry's or enum's name finds, and a
    #: profile with it has a file judged by ``references`` when it spells
    #: only an enum's name, not just an entry's.
    enum_escapes: (
        Callable[[str, str, tuple[KeyDeclaration, ...], str], list[Refusal]] | None
    ) = None
    #: ``(entries, repo_dir)``: what a file's text holds when it can name the
    #: file declaring one of ``entries``, however it spells the specifier.
    enum_module_pattern: (
        Callable[[tuple[KeyDeclaration, ...], str], re.Pattern[str]] | None
    ) = None
    #: ``(path, source, constants, repo_dir)``: for a language whose module or
    #: owner is a value a file can hand on or reflect over (Python's module,
    #: a PHP, Java or Kotlin class), ``(declaring file, refusal)`` at each
    #: place in ``path`` that reaches the file declaring one of ``constants``
    #: as a whole, where a constant can be read without its name being
    #: spelled. Each keeps every declaration of that file. Asked of every file
    #: of the family the scan finds, including one that spells no constant's
    #: name, which is never asked :attr:`references` about it, and of every
    #: file a :attr:`reaches` language's profile judges, through that
    #: profile's own hook (a Kotlin file reflecting over a Java owner). The
    #: declaring file is asked too, about its own constants as well: an owner
    #: reflecting over itself (``self::class`` in the owner,
    #: ``Keys.class.getFields()`` inside ``Keys``) spells none of them.
    constant_escapes: (
        Callable[[str, str, tuple[KeyDeclaration, ...], str], list[tuple[str, Refusal]]] | None
    ) = None
    #: ``(constants, repo_dir)``: what a file's text holds when it can reach
    #: the file declaring one of ``constants`` as a whole, however it spells
    #: the import. Set with :attr:`constant_escapes`.
    constant_module_pattern: (
        Callable[[tuple[KeyDeclaration, ...], str], re.Pattern[str]] | None
    ) = None
    #: Suffixes of files that can use an enum's file or a constant but that
    #: no profile reads as a whole (TypeScript's: Vue, Svelte and Astro
    #: components and MDX documents), from
    #: :data:`~flag_cleanup.source_index.COMPONENT_SUFFIXES`. Listed for the
    #: escape scan alone and never transformed. Such a file whose text names
    #: an enum, or that :attr:`enum_module_pattern` finds for the enum's
    #: file, refuses every entry of that enum, unread, wherever
    #: :attr:`enum_unparsed_match` counts the text, and wherever
    #: :attr:`enum_unparsed_escapes` finds an escape in the code it can read.
    #: One whose text spells a constant's name anywhere keeps that
    #: constant's declaration, ``unsupported-form`` at the first such line.
    enum_unparsed_suffixes: frozenset[str] = frozenset()
    #: ``(path, source, patterns)``: the offset of the first place in such a
    #: file where one of ``patterns`` can mean a use, or None. Unset, any
    #: match anywhere in its text is one.
    enum_unparsed_match: (
        Callable[[str, str, tuple[re.Pattern[str], ...]], int | None] | None
    ) = None
    #: ``(path, source, entries, repo_dir)``: the escapes in the parts of
    #: such a file that :attr:`enum_unparsed_match` leaves to it, a
    #: component's scripts, with a read of an entry counted as one: the file
    #: is never rewritten.
    enum_unparsed_escapes: (
        Callable[[str, str, tuple[KeyDeclaration, ...], str], list[Refusal]] | None
    ) = None
    #: Whether a proven read folds only when every declaration its file
    #: spells is deletable. For a language whose constants are not constant
    #: (Python's: any module can assign one), a refusal that keeps a
    #: declaration may be a write of another value, which a fold would
    #: replace with the declared one; telling such a write from a harmless
    #: mention would take a list of hazards. So any refusal keeping a
    #: declaration voids every reader that spells it, each listed as
    #: :data:`DECLARATION_KEPT` at its first mention, and a voided reader keeps
    #: every other declaration it spells, which can void more (see
    #: :func:`_void_readers_of_kept`). A constant finding of
    #: :attr:`entry_findings` (one with no ``entry``) keeps its file too,
    #: unless no other file spells a constant of that file (see
    #: :attr:`home_constant_names`).
    #: Off, a kept declaration leaves its proven reads folding.
    readers_need_deletable: bool = False
    #: Whether an escape (:attr:`enum_escapes`) can change what an entry's
    #: accessor returns, not only hand the enum on. Python's can (a write of a
    #: member's ``_value_``, the class replaced in its module), so no read of
    #: an escaped entry folds: every reader of one is voided like a reader of
    #: a kept declaration, each listed as :data:`DECLARATION_KEPT`. Off, an
    #: escape leaves only its own file (TypeScript's).
    escapes_void_readers: bool = False
    #: ``(path, source, flag_key)``: for a profile with
    #: :attr:`readers_need_deletable`, the name of every constant holding the
    #: key in the file when nothing there can give one another value, so its
    #: :attr:`entry_findings` only stop other files trusting it; empty
    #: otherwise. A file no other file spells one of these in has those
    #: findings set aside as :attr:`Resolution.home_findings` instead of
    #: keeping the file.
    home_constant_names: Callable[[str, str, str], set[str]] | None = None
    #: ``(path, repo_dir)``: for a language whose module is a value a file
    #: can hand on whole (TypeScript's), a refusal at each use, anywhere,
    #: that hands the module declaring a constant at ``path`` on as an
    #: object. That object reaches the constant by a name no scan sees
    #: (``Object.values(K)``, ``K[name]``), so each keeps every constant the
    #: file declares. Its readers still fold. None for a language whose
    #: constants are reached only by name. Unlike :attr:`constant_escapes`,
    #: which judges one file at a time, this walks the checkout itself, so it
    #: can follow a module through others that re-export it (a TS barrel).
    module_escapes: Callable[[str, str], list[Refusal]] | None = None


class DeclarationsUnreadable(Exception):
    """A file's declarations cannot all be read, so it might declare anything."""


_PROFILES: dict[str, Profile] = {}

# Filled lazily from the runner's language table, so this module does not
# import the runner at import time (the runner imports this module).
_EXTENSIONS_BY_LANGUAGE: dict[str, tuple[str, ...]] = {}


logger = logging.getLogger(__name__)

#: Files already reported by :func:`note_incomplete_index` in this process.
_REPORTED_INCOMPLETE: set[str] = set()


def note_incomplete_index(path: str, language: str) -> None:
    """Say, once per file, that a type index could not see all of ``path``.

    The index is how a profile tells whether a type's members, a supertype or
    an implicit receiver can claim a constant's name. A file it cannot read or
    fully parse might declare such a member, so every read that question
    applies to is refused rather than proven, and the constant's declaration
    is kept. A Kotlin file that does parse, only not whole, narrows that to
    the reads it could affect: those whose supertypes it names or whose name
    it spells. Logged because nothing else would tell the reader why.
    """
    if path in _REPORTED_INCOMPLETE:
        return
    _REPORTED_INCOMPLETE.add(path)
    logger.info(
        "%s could not be read or fully parsed as %s, so a key constant read "
        "inside a type body or a receiver scope (a lambda, an extension, a "
        "subclass) that this file could affect is not resolved across files "
        "while it stays that way; those reads and their declarations are left "
        "in place",
        path,
        language,
    )


_Contribution = TypeVar("_Contribution")
_Index = TypeVar("_Index")


@dataclass(frozen=True)
class _Parsed(Generic[_Contribution]):
    stamp: tuple[int, int]
    digest: bytes
    contribution: _Contribution
    #: False when the parse had errors: the file might declare anything.
    complete: bool


class CheckoutIndex(Generic[_Contribution, _Index]):
    """One language's index over the whole checkout, for a profile to consult.

    The Java, Kotlin and Groovy/Scala profiles each need one -- which types
    the checkout declares, what they inherit, which names stand for an
    object -- and each used to carry its own copy of the same walk, stamp,
    cache and fail-closed loop. What differs is two functions: ``parse``
    turns one file's text into its share of the index (and whether it parsed
    cleanly), ``combine`` folds the shares, in path order, into the index.

    A file is re-read only when its ``(mtime, size)`` moved, and re-parsed
    only when the text read back DIFFERS from what was parsed before. The
    post-flag restore rewrites every file a flag touched with the content it
    already had: a stamp alone calls that a change, and re-parsed the
    checkout's touched files for every flag of the run.

    A file that cannot be read, or whose parse has errors, makes the index
    incomplete, since it might declare anything; each is reported once (see
    :func:`note_incomplete_index`). ``combine`` is told both, and separately
    whether any file could not be READ: one that parsed with errors still
    contributes a share, from which a profile can narrow what it may declare.
    """

    def __init__(
        self,
        language: str,
        extensions: tuple[str, ...],
        parse: Callable[[str], tuple[_Contribution, bool]],
        combine: Callable[[list[tuple[str, _Contribution]], bool, bool], _Index],
        exclude: Callable[[Path], bool] | None = None,
    ) -> None:
        self._language = language
        self._extensions = extensions
        self._parse = parse
        self._combine = combine
        #: Files the index never reads, by resolved path: a language's
        #: third-party tree (PHP's Composer ``vendor/``) that cannot declare
        #: the checkout's own types and would dominate the cost.
        self._exclude = exclude
        self._files: dict[str, _Parsed[_Contribution]] = {}
        self._built: dict[str, tuple[tuple, _Index]] = {}

    def __call__(self, repo_dir: str) -> _Index:
        from flag_cleanup import source_index
        from flag_cleanup.cross_file_jvm_guest import walk_suffixes

        key: list[tuple[str, bytes | None]] = []
        shares: list[tuple[str, _Contribution]] = []
        incomplete = unreadable = False
        # Ignored files included: a type only generated code declares can still
        # be the path from a tracked reader to the owner.
        for source in source_index.files(
            repo_dir, walk_suffixes(self._extensions), with_ignored=True
        ):
            if not source.path.name.endswith(self._extensions):
                continue
            if self._exclude is not None and self._exclude(Path(source.resolved)):
                continue
            parsed = self._parsed(source)
            if parsed is None or not parsed.complete:
                incomplete = True
                note_incomplete_index(str(source.path), self._language)
            if parsed is None:
                unreadable = True
                key.append((source.resolved, None))
                continue
            key.append((source.resolved, parsed.digest))
            shares.append((source.resolved, parsed.contribution))
        root = source_index.checkout_root(repo_dir)
        built = self._built.get(root)
        if built is not None and built[0] == tuple(key):
            return built[1]
        index = self._combine(shares, incomplete, unreadable)
        self._built[root] = (tuple(key), index)
        return index

    def _parsed(self, source: SourceFile) -> _Parsed[_Contribution] | None:
        try:
            info = source.path.stat()
        except OSError:
            return None
        stamp = (info.st_mtime_ns, info.st_size)
        cached = self._files.get(source.resolved)
        if cached is not None and cached.stamp == stamp:
            return cached
        try:
            text = source.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
        if cached is not None and cached.digest == digest:
            cached = replace(cached, stamp=stamp)
        else:
            contribution, complete = self._parse(text)
            cached = _Parsed(stamp, digest, contribution, complete)
        self._files[source.resolved] = cached
        return cached


def walk(node: Node) -> Iterator[Node]:
    """``node`` and every named node below it."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.named_children)


def refusal(path: str, node: Node, reason: str, source: str, entry: str = "") -> Refusal:
    """A refusal of ``path`` at ``node``'s line, quoting that line."""
    line = node.start_point[0]
    lines = source.splitlines()
    return Refusal(
        path, line + 1, reason, lines[line].strip() if line < len(lines) else "", entry
    )


def register(language: str, profile: Profile) -> None:
    _PROFILES[language] = profile


def supported(language: str) -> bool:
    _load_profiles()
    return language in _PROFILES


def family(language: str) -> frozenset[str]:
    """The languages whose files ``language``'s constants are proven across.

    Just ``language`` for one without a profile, and for every profile but the
    TypeScript family's, whose members (``ts``, ``tsx``, ``js``) import each
    other's modules freely.
    """
    _load_profiles()
    profile = _PROFILES.get(language)
    return profile.language_family if profile is not None else frozenset({language})


def _load_profiles() -> None:
    # Imported for their registration side effect. Kept here rather than at
    # module top so a profile can import this module's dataclasses freely.
    from flag_cleanup import (  # noqa: F401
        cross_file_csharp,
        cross_file_dart,
        cross_file_dotnet_guest,
        cross_file_java,
        cross_file_jvm_guest,
        cross_file_kotlin,
        cross_file_php,
        cross_file_php_guest,
        cross_file_python,
        cross_file_swift,
        cross_file_ts,
    )


def _extensions(language: str) -> tuple[str, ...]:
    if not _EXTENSIONS_BY_LANGUAGE:
        from flag_cleanup.cross_file_jvm_guest import GUEST_EXTENSIONS
        from flag_cleanup.piranha_runner import _LANGUAGES

        _EXTENSIONS_BY_LANGUAGE.update(
            {name: spec.extensions for name, spec in _LANGUAGES.items()}
        )
        # Languages this tool never transforms but whose files can read a
        # constant it may delete, so the mention scan has to see them.
        _EXTENSIONS_BY_LANGUAGE.update(GUEST_EXTENSIONS)
        from flag_cleanup.cross_file_dotnet_guest import DOTNET_GUEST_EXTENSIONS

        # Razor, XAML, VB and F# can read a C# constant; MSBuild files hold
        # project-wide `<Using>` items. Both have to be in the listing.
        _EXTENSIONS_BY_LANGUAGE.update(DOTNET_GUEST_EXTENSIONS)
        from flag_cleanup.cross_file_php_guest import PHP_GUEST_EXTENSIONS

        # Twig, Latte and Smarty templates and Symfony configuration can
        # read a PHP constant.
        _EXTENSIONS_BY_LANGUAGE.update(PHP_GUEST_EXTENSIONS)
    return _EXTENSIONS_BY_LANGUAGE.get(language, ())


def mention_pattern(names: Iterable[str]) -> re.Pattern[str]:
    """A token match for any of ``names``, as wide as a mention scan needs.

    Only a letter, digit or underscore on either side makes a hit part of a
    longer identifier. A ``$`` does NOT: in Kotlin, Dart and Groovy
    ``"flag=$NAME"`` is a string template reading ``NAME``, and ``"$NAME$x"``
    reads it too, so ``$`` must not hide a hit on either side. The cost is a
    JavaScript identifier such as ``$NAME`` or ``NAME$`` counting as a mention
    of ``NAME``, and an over-counted mention only keeps a declaration. Every
    scan asking "could this file name the constant?" builds its pattern here.

    No names gives a pattern that never matches: an empty alternation would
    match everywhere.
    """
    alternatives = sorted(set(names))
    if not alternatives:
        return re.compile(r"(?!)")
    return re.compile(
        r"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])" % "|".join(map(re.escape, alternatives))
    )


def key_token(flag_key: str) -> re.Pattern[str]:
    """``flag_key`` as a whole token in source text, as the residue scan reads it.

    Whether an enum entry's arguments HOLD the key: ``"old-checkout-v2"``
    is another flag's, while ``PREFIX + "old-checkout"`` or a template
    around the exact key still holds it (and is refused as unproven).
    """
    return re.compile(rf"(?<![\w.-]){re.escape(flag_key)}(?![\w.-])")


def mention_files(
    repo_dir: str,
    names: Iterable[str],
    exclude: Iterable[str],
    *,
    with_ignored: bool = False,
    also: re.Pattern[str] | None = None,
    extra_suffixes: frozenset[str] = frozenset(),
) -> list[str]:
    """Every source file (any language) that mentions one of ``names`` as a token.

    Or whose text ``also`` finds, when given: a file that can name a
    declaration's module without spelling the declaration.

    Searched across ``repo_dir``'s whole git checkout, not just ``repo_dir``:
    the configured directory can be narrower than the repository, and a reader
    outside it still reads the constant. Which files that is, and why ignored
    build output is left out unless ``with_ignored``, is
    :mod:`~flag_cleanup.source_index`'s answer. Every suffix it lists is
    scanned: a ``.html`` file with no profile is skipped by the caller, which
    is how ``x.scala.html`` is found at all. ``extra_suffixes`` adds ones the
    listing keeps only on request.
    """
    from flag_cleanup import source_index

    names = sorted(set(names))
    if not names and also is None:
        return []
    pattern = mention_pattern(names)
    folded = re.compile(pattern.pattern, re.IGNORECASE)
    excluded = {str(Path(p).resolve()) for p in exclude}
    found: list[str] = []
    suffixes = source_index.all_suffixes() | extra_suffixes if extra_suffixes else None
    for source in source_index.files(repo_dir, suffixes, with_ignored=with_ignored):
        if source.resolved in excluded:
            continue
        try:
            text = source.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Cannot tell whether it mentions a name, so it counts as a mention
            # and the caller refuses it rather than letting it vanish.
            found.append(source.resolved)
            continue
        spells = (folded if _case_insensitive(source.resolved) else pattern).search(text)
        if spells or (also is not None and also.search(text)):
            found.append(source.resolved)
    return found


def _case_insensitive(path: str) -> bool:
    """Whether ``path`` is in a language whose names ignore case: VB, and
    PHP for class and namespace names."""
    from flag_cleanup.cross_file_dotnet_guest import CASE_INSENSITIVE_SUFFIXES
    from flag_cleanup.cross_file_php import PHP_EXTENSIONS

    return path.lower().endswith(CASE_INSENSITIVE_SUFFIXES + PHP_EXTENSIONS)


def within(path: Path, scope: Path) -> bool:
    """Whether ``path`` is ``scope`` or lies under it (both already resolved)."""
    return path == scope or scope in path.parents


def _line_of(path: str, needle: str) -> tuple[int, str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 1, ""
    # A case-insensitive language's declarations are recorded lowercased
    # (PHP's owners), so `keys` has to find `class Keys`.
    fold = _case_insensitive(path)
    wanted = needle.lower() if fold else needle
    for number, line in enumerate(lines, start=1):
        if wanted in (line.lower() if fold else line):
            return number, line.strip()
    return 1, ""


def resolve(
    repo_dir: str,
    language: str,
    flag_key: str,
    key_files: list[str],
    accessors: Iterable[str],
) -> Resolution:
    _load_profiles()
    profile = _PROFILES.get(language)
    if profile is None:
        return Resolution({}, (), frozenset())
    accessor_set = frozenset(accessors)

    declarations: list[KeyDeclaration] = []
    entry_refusals: list[Refusal] = []
    # Per file, the names of its constants, when it has a constant finding.
    findings_in: dict[str, set[str]] = {}
    for path in key_files:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        resolved = str(Path(path).resolve())
        declarations.extend(profile.declarations(resolved, source, flag_key))
        if profile.entry_findings is not None:
            found = profile.entry_findings(resolved, source, flag_key)
            entry_refusals.extend(found)
            if profile.home_constant_names is not None and any(not r.entry for r in found):
                findings_in[resolved] = profile.home_constant_names(resolved, source, flag_key)
    home = _unnamed_elsewhere(repo_dir, findings_in, profile)
    home_findings = tuple(r for r in entry_refusals if r.path in home and not r.entry)
    entry_refusals = [r for r in entry_refusals if r not in home_findings]
    # A constant finding at its own declaration keeps that file, when kept
    # declarations decide what folds.
    kept_at_home = (
        {r.path for r in entry_refusals if not r.entry}
        if profile.readers_need_deletable
        else set()
    )
    if not declarations:
        return Resolution(
            {}, tuple(entry_refusals), frozenset(), blocked=frozenset(kept_at_home),
            readers_need_deletable=profile.readers_need_deletable,
            home_findings=home_findings,
        )

    refusals: list[Refusal] = list(entry_refusals)
    entries_found = tuple(d for d in declarations if d.kind == ENUM_ENTRY)
    by_qualified: dict[tuple[str, str | None, str], list[KeyDeclaration]] = defaultdict(list)
    for decl in declarations:
        by_qualified[(decl.module, decl.owner, decl.name)].append(decl)
    unique = [group[0] for group in by_qualified.values() if len(group) == 1]
    duplicated: set[str] = set()
    for group in by_qualified.values():
        if len(group) > 1:
            for decl in group:
                if decl.kind == CONSTANT:
                    duplicated.add(decl.path)
                line, text = _line_of(decl.path, decl.name)
                refusals.append(Refusal(decl.path, line, NAME_NOT_UNIQUE, text, decl.entry))
    if not unique:
        return Resolution(
            {}, tuple(refusals), frozenset(), blocked=frozenset(duplicated | kept_at_home),
            entries=entries_found, readers_need_deletable=profile.readers_need_deletable,
            home_findings=home_findings,
        )

    declaring = {decl.path for decl in unique if decl.kind == CONSTANT}

    def spelled_in(source: str, path: str) -> set[str]:
        """The declaring files whose constant ``source`` spells by name.

        A refusal in a file keeps exactly these: the file's mentions of any
        other declaration's name do not exist, and its reads of these are
        dropped with the rest of the file's. A VB file spells a name in any
        case.
        """
        flags = re.IGNORECASE if _case_insensitive(path) else 0
        return {
            d.path
            for d in unique
            if d.kind == CONSTANT
            and re.compile(mention_pattern([d.name]).pattern, flags).search(source)
        }

    family_extensions = tuple(
        ext for lang in profile.language_family for ext in _extensions(lang)
    )
    readers: dict[str, tuple[KeyReference, ...]] = {}
    # declaring files some mention stops from being deleted
    blocked: set[str] = set(kept_at_home)
    names = {decl.name for decl in unique}
    # Every segment of a nested owner (`Outer.Inner`): a file declaring only
    # `Outer` can still supply it, and has to be read to be seen competing.
    owners = {
        part for decl in unique if decl.owner is not None for part in decl.owner.split(".")
    }
    scope = Path(repo_dir).resolve()
    sources: dict[str, str | None] = {}
    # A file can use an enum's module without spelling a name: `use(m)` after
    # `import * as m from "./flags"`, or `export * from "./flags"`.
    escapes_of = tuple(d for d in unique if d.kind == ENUM_ENTRY) if profile.enum_escapes else ()
    also = (
        profile.enum_module_pattern(escapes_of, repo_dir)
        if escapes_of and profile.enum_module_pattern is not None
        else None
    )
    # Or a constant's module, without spelling the constant: `use(keys)`.
    constants_of = tuple(d for d in unique if d.kind == CONSTANT) if profile.constant_escapes else ()
    if constants_of and profile.constant_module_pattern is not None:
        whole = profile.constant_module_pattern(constants_of, repo_dir)
        also = whole if also is None else re.compile(f"(?:{also.pattern})|(?:{whole.pattern})")
    unparsed = profile.enum_unparsed_suffixes
    # Ignored build output included: a competing declaration there still
    # decides which constant a tracked reader gets. Its own mentions are
    # skipped below.
    for path in mention_files(
        repo_dir, names | owners, exclude=(), with_ignored=True, also=also, extra_suffixes=unparsed
    ):
        try:
            sources[path] = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            sources[path] = None
    ignored = _ignored(repo_dir, unparsed)

    voided = _competing(unique, sources, refusals)
    blocked |= {decl.path for decl in voided if decl.kind == CONSTANT}
    live = [decl for decl in unique if decl not in voided]

    # Only a file that spells a constant's NAME can read it. One that names
    # just an owner was read above for the competing-declaration question,
    # unless the owner is an enum a file can take as a value: then a file
    # spelling only the enum's name can still hand it on.
    judged_owners = (
        {d.owner for d in live if d.kind == ENUM_ENTRY and d.owner is not None}
        if profile.enum_escapes is not None
        else set()
    )
    spells_a_name = mention_pattern(names | judged_owners)
    spells_a_name_folded = re.compile(spells_a_name.pattern, re.IGNORECASE)
    live_constants = [d for d in live if d.kind == CONSTANT]
    spells_a_constant = mention_pattern({d.name for d in live_constants})
    for path, source in sources.items():
        if unparsed and Path(path).suffix in unparsed:
            # Never rewritten, and never read by `references`: a constant it
            # spells, in its code or its markup (an auto-import binds one
            # unwritten), is a read no fold reaches. The escape scan below
            # refuses an enum's.
            if live_constants and path not in ignored:
                hit = spells_a_constant.search(source) if source is not None else None
                if source is None:
                    refusals.append(Refusal(path, 1, UNSUPPORTED_FORM, ""))
                    blocked |= {d.path for d in live_constants}
                elif hit is not None:
                    line = source.count("\n", 0, hit.start()) + 1
                    refusals.append(Refusal(path, line, UNSUPPORTED_FORM, source.split("\n")[line - 1].strip()))
                    blocked |= spelled_in(source, path)
            continue
        spells = spells_a_name_folded if _case_insensitive(path) else spells_a_name
        if source is not None and not spells.search(source):
            continue
        if path in ignored:
            continue  # build output: the next build rewrites it, a commit never carries it
        in_family = path.endswith(family_extensions)
        judge = profile if in_family else _reaching_profile(profile, path)
        if judge is None:
            # A language that cannot name this one's constants: whatever it
            # spells is its own (a frontend's `FLAG_KEY`, a script's).
            continue
        if source is None:
            entries = sorted({d.entry for d in live if d.kind == ENUM_ENTRY})
            kinds = ([""] if any(d.kind == CONSTANT for d in live) else []) + entries
            refusals.extend(Refusal(path, 1, UNSUPPORTED_FORM, "", e) for e in kinds)
            blocked |= declaring
            continue
        in_scope = within(Path(path), scope)
        if not (in_family and in_scope):
            # Never rewritten by this run: outside the directory it
            # transforms, or in another JVM language. It keeps every
            # declaration whose name it spells alive -- but only if a mention
            # in it can reach one, as its own language's profile judges.
            visible = tuple(d for d in live if d.path != path)
            reads, refused = judge.references(path, source, visible, accessor_set, repo_dir)
            if not (reads or refused):
                continue
            # One refusal per entry it reads or refuses (and one for the
            # constants), at its first refusal or read: an entry named by
            # none would be reported kept while its read stays here.
            sites: dict[str, tuple[int, str]] = {}
            for refusal in refused:
                sites.setdefault(refusal.entry, (refusal.line, refusal.text))
            for ref in reads:
                entry = ref.declaration.entry if ref.declaration is not None else ""
                if entry not in sites:
                    sites[entry] = _line_of(path, ref.member)
            reason = UNSUPPORTED_FORM if not in_scope else OTHER_LANGUAGE
            refusals.extend(Refusal(path, line, reason, text, entry) for entry, (line, text) in sites.items())
            blocked |= spelled_in(source, path)
            continue
        # A declaring file is still a reader of the OTHER declarations; its
        # mentions of its own are the in-file check's business.
        visible = tuple(d for d in live if d.path != path)
        if not visible:
            continue
        reads, refused = profile.references(path, source, visible, accessor_set, repo_dir)
        if reads:
            readers[path] = tuple(reads)
        if refused:
            refusals.extend(refused)
            blocked |= spelled_in(source, path)
    constants = tuple(d for d in live if d.kind == CONSTANT)
    if constants and profile.constant_escapes is not None:
        for path, source in sorted(sources.items()):
            escapes = _escape_hook(profile, path, family_extensions)
            if source is None or path in ignored or escapes is None:
                continue
            # The declaring file included: an owner that reflects over itself
            # (`self::class`, `Keys.class.getFields()`) spells no constant's
            # name, so the in-file check never sees it.
            for holder, escape in escapes(path, source, constants, repo_dir):
                blocked.add(holder)
                if escape not in refusals:
                    refusals.append(escape)
    if constants and profile.module_escapes is not None:
        # A refusal a reader already gave on the same line is this escape.
        refused_at = {(r.path, r.line) for r in refusals if not r.entry}
        for path in sorted({d.path for d in constants}):
            for escape in profile.module_escapes(path, repo_dir):
                blocked.add(path)
                if (escape.path, escape.line) not in refused_at:
                    refused_at.add((escape.path, escape.line))
                    refusals.append(escape)
    notes: list[Refusal] = []
    entries = tuple(d for d in live if d.kind == ENUM_ENTRY)
    if entries and profile.enum_escapes is not None:
        # An escape on a line a reader's refusal already names for the same
        # entry is that refusal: its reason is the constant's for the spelling.
        refused_at = {(r.path, r.line, r.entry) for r in refusals if r.entry}
        escaped: set[str] = set()
        unread = _UnparsedUses(profile, entries, repo_dir)
        for path, source in sorted(sources.items()):
            if path in ignored:
                continue
            if unparsed and Path(path).suffix in unparsed:
                escapes = unread(path, source)
                if source is not None and profile.enum_unparsed_escapes is not None:
                    escapes += profile.enum_unparsed_escapes(path, source, entries, repo_dir)
            elif source is None or not path.endswith(family_extensions):
                continue
            else:
                escapes = profile.enum_escapes(path, source, entries, repo_dir)
            for escape in escapes:
                escaped.add(escape.entry)
                if (escape.path, escape.line, escape.entry) not in refused_at:
                    refused_at.add((escape.path, escape.line, escape.entry))
                    refusals.append(escape)
        if profile.escapes_void_readers and escaped:
            # The entry's accessor may not return its key: no read of it folds.
            for path in sorted(readers):
                hit = [
                    ref for ref in readers[path]
                    if ref.declaration is not None and ref.declaration.entry in escaped
                ]
                if not hit:
                    continue
                del readers[path]
                blocked |= spelled_in(sources.get(path) or "", path)
                line, text = _line_of(path, hit[0].member)
                for entry in sorted({ref.declaration.entry for ref in hit}):
                    refusals.append(Refusal(path, line, DECLARATION_KEPT, text, entry))
    if entries and profile.enum_dynamic_uses is not None:
        for path, source in sorted(sources.items()):
            if source is None or path in ignored or not path.endswith(family_extensions):
                continue
            notes.extend(profile.enum_dynamic_uses(path, source, entries, repo_dir))
    if profile.readers_need_deletable:
        readers, refusals, blocked = _void_readers_of_kept(
            readers, refusals, blocked | duplicated, unique, lambda path: sources.get(path)
        )
    return Resolution(
        readers,
        tuple(refusals),
        frozenset(declaring - blocked),
        tuple(live),
        frozenset(blocked | duplicated),
        tuple(notes),
        entries_found,
        readers_need_deletable=profile.readers_need_deletable,
        home_findings=home_findings,
    )


def _unnamed_elsewhere(
    repo_dir: str, names_by_file: dict[str, set[str]], profile: Profile | None = None
) -> set[str]:
    """The files of ``names_by_file`` whose names no other file spells, and
    that no other file reaches whole (:attr:`Profile.constant_escapes`).

    Any other file in the checkout counts, in any language, ignored ones
    included, and so does one that cannot be read: a file counted here when
    it reads nothing only keeps a declaration, as the check it skips would.
    """
    if not names_by_file:
        return set()
    every = set().union(*names_by_file.values())
    spelled: dict[str, str | None] = {}
    for hit in mention_files(repo_dir, every, exclude=(), with_ignored=True):
        try:
            spelled[hit] = Path(hit).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            spelled[hit] = None
    home: set[str] = set()
    for path, names in names_by_file.items():
        token = mention_pattern(names)
        if names and not any(
            hit != path and (text is None or token.search(text)) for hit, text in spelled.items()
        ):
            home.add(path)
    if home and profile is not None and profile.constant_escapes is not None:
        # `use(keys)` reads every constant of `keys` spelling none. A profile
        # with constant escapes keys a module by its file.
        held = tuple(
            KeyDeclaration(path, name, None, path) for path in sorted(home) for name in sorted(names_by_file[path])
        )
        family = tuple(ext for lang in profile.language_family for ext in _extensions(lang))
        whole = profile.constant_module_pattern(held, repo_dir) if profile.constant_module_pattern else None
        for hit in mention_files(repo_dir, (), exclude=(), with_ignored=True, also=whole) if whole else ():
            visible = tuple(d for d in held if d.path != hit and d.path in home)
            escapes = _escape_hook(profile, hit, family)
            if escapes is None or not visible:
                continue
            text = spelled.get(hit)
            if text is None:
                try:
                    text = Path(hit).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue  # unreadable: the name scan above already found it
            home -= {holder for holder, _ in escapes(hit, text, visible, repo_dir)}
    return home


def _void_readers_of_kept(
    readers: dict[str, tuple[KeyReference, ...]],
    refusals: Iterable[Refusal],
    blocked: Iterable[str],
    declarations: Iterable[KeyDeclaration],
    source_of: Callable[[str], str | None],
) -> tuple[dict[str, tuple[KeyReference, ...]], list[Refusal], set[str]]:
    """``readers`` without each one that spells a kept declaration, for
    :attr:`Profile.readers_need_deletable`.

    A voided reader keeps all its reads, so every declaration it spells is
    kept as well, which can void another reader in turn: repeated until
    nothing changes. Each voided reader is listed at the first line that
    spells a kept declaration, as :data:`DECLARATION_KEPT`.
    """
    readers = dict(readers)
    refusals = list(refusals)
    blocked = set(blocked)
    constants = [d for d in declarations if d.kind == CONSTANT]
    changed = True
    while changed:
        changed = False
        for path in sorted(readers):
            source = source_of(path)
            spelled = [
                d for d in constants if source is None or mention_pattern([d.name]).search(source)
            ]
            kept = [d for d in spelled if d.path in blocked]
            if not kept:
                continue
            # Its reads of enum entries stay too: each entry is left, never
            # reported as safe to delete.
            entry_reads = [ref for ref in readers[path] if ref.declaration is not None]
            del readers[path]
            blocked |= {d.path for d in spelled}
            changed = True
            line, text = 1, ""
            if source is not None:
                token = mention_pattern(sorted({d.name for d in kept}))
                lines = source.splitlines()
                number = next((i for i, each in enumerate(lines) if token.search(each)), 0)
                line, text = number + 1, lines[number].strip() if lines else ""
            refusals.append(
                Refusal(path, line, DECLARATION_KEPT, text)
            )
            if entry_reads:
                at, quoted = _line_of(path, entry_reads[0].member)
                refusals.extend(
                    Refusal(path, at, DECLARATION_KEPT, quoted, entry)
                    for entry in sorted({ref.declaration.entry for ref in entry_reads})
                )
    return readers, refusals, blocked


class _UnparsedUses:
    """The escapes of a file no profile parses: a component file.

    It is never read as code, so whatever it does with an enum cannot be
    followed. If its text names the enum, or holds what
    :attr:`Profile.enum_module_pattern` finds for the enum's file, every entry
    of that enum is refused at the first line that does; an unreadable one
    refuses every entry. :attr:`Profile.enum_unparsed_match` says which parts
    of the text count. Each enum's pattern is built once per resolution.
    """

    def __init__(self, profile: Profile, entries: tuple[KeyDeclaration, ...], repo_dir: str) -> None:
        self._profile = profile
        self._repo_dir = repo_dir
        self._by_module: dict[str, list[KeyDeclaration]] = {}
        for decl in entries:
            self._by_module.setdefault(decl.module, []).append(decl)
        self._patterns: dict[str, tuple[re.Pattern[str], ...]] = {}

    def _patterns_of(self, module: str) -> tuple[re.Pattern[str], ...]:
        if module not in self._patterns:
            decls = self._by_module[module]
            found = [mention_pattern({d.owner for d in decls if d.owner is not None})]
            if self._profile.enum_module_pattern is not None:
                found.append(self._profile.enum_module_pattern(tuple(decls), self._repo_dir))
            self._patterns[module] = tuple(found)
        return self._patterns[module]

    def _match(self, path: str, source: str, patterns: tuple[re.Pattern[str], ...]) -> int | None:
        if self._profile.enum_unparsed_match is not None:
            return self._profile.enum_unparsed_match(path, source, patterns)
        return min((m.start() for p in patterns if (m := p.search(source))), default=None)

    def __call__(self, path: str, source: str | None) -> list[Refusal]:
        refusals: list[Refusal] = []
        for module, decls in sorted(self._by_module.items()):
            line, text = 1, ""
            if source is not None:
                hit = self._match(path, source, self._patterns_of(module))
                if hit is None:
                    continue
                line = source.count("\n", 0, hit) + 1
                text = source.split("\n")[line - 1].strip()
            refusals.extend(Refusal(path, line, UNSUPPORTED_FORM, text, d.entry) for d in decls)
        return refusals


def _ignored(repo_dir: str, extra_suffixes: frozenset[str] = frozenset()) -> frozenset[str]:
    """The resolved paths that ONLY ignored source files lead to.

    Resolved, because that is how ``resolve`` keys a file, so a path some
    entry that is not ignored also resolves to is never in it: a link in an
    ignored symlink tree points at the tracked file whose mentions still count.
    ``extra_suffixes`` as for :func:`mention_files`.
    """
    from flag_cleanup import source_index

    suffixes = source_index.all_suffixes() | extra_suffixes if extra_suffixes else None
    listing = source_index.files(repo_dir, suffixes, with_ignored=True)
    kept = {f.resolved for f in listing if not f.ignored}
    return frozenset(f.resolved for f in listing if f.ignored) - kept


def report_uncovered(
    resolution: Resolution, covered: Iterable[str], extensions: tuple[str, ...]
) -> Resolution:
    """``resolution`` with a refusal for every proven reader the run will not rewrite.

    A reader is proven across the whole language family, and a run rewrites
    the readers it was handed, a sibling file type's included. One it was not
    handed -- a ``.tsx`` component when ``tsx`` is not a configured language,
    or one that another run of the family also rewrites -- keeps its read, so
    the declarations whose names it spells stay too, and the pull request has
    to say which file is why. A reader of the run's own language that was not
    handed to it is ``unsupported-form``; one of another file type in the
    family is ``other-language``.
    """
    rewritten = {str(Path(p).resolve()) for p in covered}
    refusals: list[Refusal] = []
    blocked = set(resolution.blocked)
    for path, refs in sorted(resolution.readers.items()):
        if path in rewritten:
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            source = ""
        token = mention_pattern(sorted({ref.member for ref in refs}))
        lines = source.splitlines()
        number = next((i for i, line in enumerate(lines) if token.search(line)), 0)
        text = lines[number].strip() if lines else ""
        reason = UNSUPPORTED_FORM if path.endswith(extensions) else OTHER_LANGUAGE
        entries = sorted({r.declaration.entry for r in refs if r.declaration is not None})
        kinds = ([""] if any(r.declaration is None for r in refs) else []) + entries
        refusals.extend(Refusal(path, number + 1, reason, text, e) for e in kinds)
        blocked |= {
            d.path
            for d in resolution.declarations
            if d.kind == CONSTANT
            and (not source or mention_pattern([d.name]).search(source))
        }
    if not refusals:
        return resolution
    readers = resolution.readers
    refusals = list(resolution.refusals) + refusals
    if resolution.readers_need_deletable:
        # Each uncovered reader is listed above; the rest fold only if
        # what they read is still deletable.
        readers = {path: refs for path, refs in readers.items() if path in rewritten}

        def source_of(path: str) -> str | None:
            try:
                return Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None

        readers, refusals, blocked = _void_readers_of_kept(
            readers, refusals, blocked, resolution.declarations, source_of
        )
    return replace(
        resolution,
        readers=readers,
        refusals=tuple(refusals),
        deletable=resolution.deletable - blocked,
        blocked=frozenset(blocked),
    )


def _escape_hook(profile: Profile, path: str, family: tuple[str, ...]):
    """The :attr:`Profile.constant_escapes` that judges ``path`` for
    ``profile``'s constants: its own for a file of its family, else that of the
    guest profile reaching ``path`` (a Razor view or a VB file for C#, a Twig
    template for PHP), if any."""
    if path.endswith(family):
        return profile.constant_escapes
    guest = _reaching_profile(profile, path)
    return guest.constant_escapes if guest is not None else None


def _reaching_profile(profile: Profile, path: str) -> Profile | None:
    """The profile that judges ``path`` for ``profile``'s constants, if any can."""
    _extensions("")  # populate the language table unless already set
    for language in profile.reaches:
        if path.endswith(_EXTENSIONS_BY_LANGUAGE.get(language, ())) and language in _PROFILES:
            return _PROFILES[language]
    return None


def _profile_for(path: str) -> Profile | None:
    """The registered profile of the language ``path`` is written in, if any."""
    _extensions("")  # populate the language table unless already set
    for language, extensions in _EXTENSIONS_BY_LANGUAGE.items():
        if path.endswith(extensions) and language in _PROFILES:
            return _PROFILES[language]
    return None


def _competing(
    unique: list[KeyDeclaration],
    sources: dict[str, str | None],
    refusals: list[Refusal],
) -> set[KeyDeclaration]:
    """The declarations another file can stand in for, refusals recorded.

    A second declaration of the same qualified name in another file -- an
    Android flavor, a per-platform source set, a second module -- may hold a
    different value, and which one a reader gets is decided by the build, not
    by anything in the source. For an owner-bearing declaration a second OWNER
    of the same qualified name is enough: it can supply ``Owner.NAME`` by
    inheritance or as an alias without spelling ``NAME`` at all. A nested
    owner's enclosing types count as owners of it too. Such a
    declaration is VOID: none of its reads is proven, and its file is never
    deletable.

    Every file is asked in its OWN language's terms, so a Java variant of a
    Kotlin owner (or the reverse) counts -- the two share one namespace on the
    JVM. Any file counts, inside the configured directory or not. A language
    with no profile cannot be asked, and its mentions are refused elsewhere.

    An owner can also be a top-level PROPERTY of that name (a Kotlin
    ``val RuntimeKeys = PaidKeys`` makes ``RuntimeKeys.NAME`` read
    ``PaidKeys``), so an owner-less declared name counts against owners too.
    A file whose declarations cannot all be read fails closed: every
    declaration whose name or owner it spells is void.
    """
    by_name = {(d.module, d.owner, d.name): d for d in unique}
    by_owner: dict[tuple[str, str], list[KeyDeclaration]] = defaultdict(list)
    # A nested owner (`Outer.Inner`) is also supplied by any type of one of
    # its enclosing names: a second `Outer` can hand out `Outer.Inner`
    # through a base class's nested type without declaring `Inner` at all.
    by_owner_prefix: dict[tuple[str, str], list[KeyDeclaration]] = defaultdict(list)
    for decl in unique:
        if decl.owner is not None:
            by_owner[(decl.module, decl.owner)].append(decl)
            parts = decl.owner.split(".")
            for end in range(1, len(parts)):
                by_owner_prefix[(decl.module, ".".join(parts[:end]))].append(decl)
    voided: set[KeyDeclaration] = set()
    for path, source in sources.items():
        if source is None:
            continue
        profile = _profile_for(path)
        if profile is None:
            continue
        found: list[tuple[KeyDeclaration, str, str]] = []
        try:
            names = profile.declared_names(path, source)
            classifiers = profile.declared_classifiers(path, source)
        except DeclarationsUnreadable:
            for decl in unique:
                spelled = [w for w in (decl.name, decl.owner) if w is not None]
                flags = re.IGNORECASE if _case_insensitive(path) else 0
                needle = next(
                    (
                        w
                        for w in spelled
                        if re.compile(mention_pattern([w]).pattern, flags).search(source)
                    ),
                    None,
                )
                if needle is not None:
                    found.append((decl, needle, UNSUPPORTED_FORM))
        else:
            for qualified in names:
                decl = by_name.get(qualified)
                if decl is not None:
                    found.append((decl, decl.name, NAME_NOT_UNIQUE))
                module, owner, name = qualified
                if owner is None:
                    found.extend(
                        (d, name, NAME_NOT_UNIQUE) for d in by_owner.get((module, name), ())
                    )
            for classifier in classifiers:
                found.extend(
                    (d, classifier[1], NAME_NOT_UNIQUE)
                    for d in (*by_owner.get(classifier, ()), *by_owner_prefix.get(classifier, ()))
                )
        here: set[KeyDeclaration] = set()  # one refusal per declaration per file
        for decl, needle, reason in found:
            if decl.path == path or decl in here:
                continue
            here.add(decl)
            voided.add(decl)
            line, text = _line_of(path, needle)
            refusals.append(Refusal(path, line, reason, text, decl.entry))
    return voided


def still_mentions(
    path: str,
    source: str,
    language: str,
    resolution: Resolution,
    accessors: Iterable[str],
    repo_dir: str,
) -> bool:
    """Whether a rewritten reader still names one of the resolved declarations.

    Asked of every proven reader after the engine ran. A proven read the reader
    rules did not remove -- or an import they left naming the constant --
    would point at a declaration the same run may delete, and only this
    profile can tell such a mention from an unrelated word.
    """
    _load_profiles()
    profile = _PROFILES.get(language)
    if profile is None:
        return False
    resolved = str(Path(path).resolve())
    visible = tuple(d for d in resolution.declarations if d.path != resolved)
    if not visible:
        return False
    reads, refused = profile.references(
        resolved, source, visible, frozenset(accessors), repo_dir
    )
    return bool(reads or refused)


def enum_findings(resolution: Resolution) -> tuple[Refusal, ...]:
    """Everything a pull request says about enum entries, in report order.

    The refusals about an entry, then the dynamic uses of its enum, then one
    finding at each entry's own declaration: ``left`` for an entry some
    refusal names (its unresolved reads are the refusals),
    ``left-competing`` for one only a competing declaration names (its reads
    were never judged, so none is listed), and ``kept`` for one a proven read
    was folded through and nothing refused. Either way the entry stays,
    because deleting an enum entry can change stored values (ordinals,
    backing values) and break lookups by name or value, and no build catches
    either.
    """
    refused = tuple(r for r in resolution.refusals if r.entry)
    against = {r.entry for r in refused}
    unresolved = {r.entry for r in refused if r.reason != NAME_NOT_UNIQUE}
    folded = {
        ref.declaration
        for refs in resolution.readers.values()
        for ref in refs
        if ref.declaration is not None
    }
    left: list[Refusal] = []
    kept: list[Refusal] = []
    reported = set(resolution.entries) | folded
    for decl in sorted(reported, key=lambda d: (d.path, d.owner or "", d.name)):
        if decl.entry in unresolved:
            reason, found = LEFT, left
        elif decl.entry in against:
            reason, found = LEFT_COMPETING, left
        elif decl in folded and not decl.third_party:
            reason, found = KEPT, kept
        else:
            continue
        line, _text = _line_of(decl.path, decl.name)
        found.append(Refusal(decl.path, line, reason, decl.entry, decl.entry))
    return refused + resolution.notes + tuple(left) + tuple(kept)


def entry_names(path: str, source: str, language: str, flag_key: str) -> set[str]:
    """The names of the enum entries ``path`` holds ``flag_key`` in, for the mention scan."""
    _load_profiles()
    profile = _PROFILES.get(language)
    if profile is None:
        return set()
    resolved = str(Path(path).resolve())
    return {
        d.name
        for d in profile.declarations(resolved, source, flag_key)
        if d.kind == ENUM_ENTRY
    }
