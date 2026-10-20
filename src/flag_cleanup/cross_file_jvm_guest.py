"""Groovy and Scala: JVM languages that can read a Java or Kotlin constant.

A Spock spec or a Scala object can name ``CheckoutGate.FLAG_KEY`` as easily as
a Java file can, and the build breaks if the declaration is deleted under it.
This tool transforms neither language and has no parser for them, so this
judge never proves a read. It only decides whether a mention could be one of
ours, and it decides by tokens, failing closed:

- an owner-less constant (a Kotlin top-level one, reached through a facade
  class whose name this cannot be sure of) is reached by any file spelling it;
- a file spelling the owner, or any repository type that may extend it, may
  reach it -- through an import, a static import, an alias or inheritance;
- otherwise every occurrence of the name has to be ``T.NAME`` where ``T`` is
  surely an unrelated repository class that this file can see by that name
  (imported, or in its own package), and the file never uses ``T`` as a
  variable. A bare ``NAME`` may be inherited or statically imported, and any
  other qualifier may be a variable or a dynamic property holding the owner.

"May extend" is followed through Java, Kotlin, Groovy and Scala together: a
Groovy class extending the owner and a Java class extending that one both
count. A type declared in a Groovy or Scala file is taken to extend whatever
that file spells, which over-counts and only keeps a declaration.

Every mention this cannot rule out is refused, so the declaration it names is
kept and the pull request names the file. That is the whole cost of a wrong
guess here: one constant left in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from flag_cleanup import cross_file_keys as xk

#: The suffixes judged here. ``.gradle`` is a Groovy build script (a Kotlin
#: one, ``.gradle.kts``, is already the Kotlin profile's) and ``.gsp`` a Grails
#: view. ``.scala.html``, ``.scala.txt`` and ``.scala.xml`` are Play templates,
#: compiled to Scala, so a template naming the constant breaks the build too.
GUEST_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "groovy": (".groovy", ".gvy", ".gy", ".gsh", ".gradle", ".gsp"),
    "scala": (".scala", ".sc", ".scala.html", ".scala.txt", ".scala.xml"),
}

_ALL_EXTENSIONS = tuple(ext for exts in GUEST_EXTENSIONS.values() for ext in exts)


def walk_suffixes(extensions: tuple[str, ...]) -> set[str]:
    """The single suffixes a directory walk must keep to find ``extensions``.

    A walk compares ``Path.suffix``, which is ``.html`` for ``x.scala.html``,
    so a two-part extension is found by its last part and filtered after.
    """
    return {"." + ext.rsplit(".", 1)[1] for ext in extensions}


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TYPE_DECL = re.compile(
    r"(?<![A-Za-z0-9_$])(?:class|interface|trait|enum|object|record)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_PACKAGE = re.compile(r"^[ \t]*package[ \t]+(?!object\b)([A-Za-z_][\w.]*)[ \t]*(;|\{)?", re.M)
_IMPORT_LINE = re.compile(r"^[ \t]*import\b.*$", re.M)
#: A comment, or a string literal of any quoting both languages share. What is
#: left after removing these is where a declaration can be.
_NOT_CODE = re.compile(
    r'//[^\n]*|/\*.*?\*/|""".*?"""|\'\'\'.*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'',
    re.S,
)

#: Words that may stand right before a class name without making it a
#: variable: ``return SearchGate.KEY``, ``new SearchGate()``, ``class X
#: extends SearchGate``. Any other word there (``def``, ``val``, a type) is
#: declaring something of that name.
_TYPE_CONTEXT = frozenset({
    "return", "new", "extends", "implements", "with", "instanceof", "as", "in",
    "case", "throw", "throws", "import", "assert", "yield", "else", "if",
    "class", "interface", "trait", "enum", "object", "record", "then", "do",
    "expect", "and", "not", "is", "match",
})


def _token(name: str) -> re.Pattern[str]:
    # The resolve loop's own token rule, so `"$NAME"` (a string template
    # reading NAME) is a hit here exactly as it was there.
    return xk.mention_pattern([name])


def _code(source: str) -> str:
    return _NOT_CODE.sub(" ", source)


class _NoPackage(Exception):
    """Scala's block form ``package a { ... }`` can put types in several."""


def _package(code: str) -> str:
    """The package of ``code`` (comments and strings removed).

    Scala's chained ``package a`` / ``package b`` clauses make one package
    ``a.b``.
    """
    parts: list[str] = []
    for match in _PACKAGE.finditer(code):
        if match.group(2) == "{":
            raise _NoPackage
        parts.append(match.group(1))
    return ".".join(parts)


@dataclass(frozen=True)
class _GuestFile:
    tokens: frozenset[str]
    types: frozenset[str]
    #: None for a file whose package cannot be told.
    package: str | None


def _guest_file(text: str) -> tuple[_GuestFile, bool]:
    """One Groovy or Scala file, as tokens and declared types."""
    code = _code(text)
    try:
        package: str | None = _package(code)
    except _NoPackage:
        package = None
    guest = _GuestFile(
        frozenset(_IDENT.findall(text)), frozenset(_TYPE_DECL.findall(code)), package
    )
    return guest, True


def _by_path(
    shares: list[tuple[str, _GuestFile]], incomplete: bool, _unreadable: bool
) -> tuple[dict[str, _GuestFile], bool]:
    return dict(shares), incomplete


#: Every Groovy and Scala file in a checkout, by resolved path, and whether
#: one of them could not be read.
_guest_index = xk.CheckoutIndex("Groovy or Scala", _ALL_EXTENSIONS, _guest_file, _by_path)


@dataclass(frozen=True)
class _Reach:
    #: The owner and every type that may be it or extend it.
    reaching: frozenset[str]
    #: Kotlin top-level properties and typealiases, which may stand for a type.
    values: frozenset[str]
    guests: dict[str, _GuestFile]


def _reach(repo_dir: str, owner: str) -> _Reach | None:
    """What may stand for ``owner``, or None when an index is incomplete."""
    from flag_cleanup import cross_file_java

    _supers, values, incomplete = cross_file_java._JvmTypes(repo_dir)._get()
    guests, guests_incomplete = _guest_index(repo_dir)
    if incomplete or guests_incomplete:
        return None
    return _Reach(heirs(repo_dir, owner), frozenset(values), guests)


def heirs(repo_dir: str, owner: str) -> frozenset[str]:
    """``owner`` and the simple name of every repository type that may extend
    it, followed through Java, Kotlin, Groovy and Scala together. What an
    index could not read is left out: a caller that must fail closed asks
    whether one was incomplete."""
    from flag_cleanup import cross_file_java

    supers, _values, _incomplete = cross_file_java._JvmTypes(repo_dir)._get()
    guests, _guests_incomplete = _guest_index(repo_dir)
    reaching = {owner}
    changed = True
    while changed:
        changed = False
        for name, chains in supers.items():
            if name not in reaching and any(reaching.intersection(c) for c in chains):
                reaching.add(name)
                changed = True
        for guest in guests.values():
            if guest.tokens & reaching and not guest.types <= reaching:
                reaching |= guest.types
                changed = True
    return frozenset(reaching)


def _used_as_variable(source: str, name: str) -> bool:
    """Whether ``source`` may bind or read ``name`` as something other than a class.

    Errs towards yes: a declaration (``def SearchGate``, ``Foo SearchGate``),
    an assignment, a closure, lambda or Scala parameter, a destructuring, a
    property of something else (``obj.SearchGate``) and a bare argument all
    count. Import lines are the caller's business.
    """
    for match in _token(name).finditer(source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        if re.match(r"[ \t]*import\b", source[line_start:]):
            continue
        before = source[: match.start()].rstrip(" \t")
        after = source[match.end() :].lstrip(" \t")
        if before.endswith((".", "@")):
            return True
        if after.startswith(("->", "=>", "<-", ",", ")")):
            return True
        if after.startswith(":") and not after.startswith("::"):
            return True
        if after.startswith("=") and not after.startswith(("==", "=~")):
            return True
        if after.startswith("in ") or after.startswith("in\t"):
            return True
        if before and (before[-1].isalnum() or before[-1] in "_$>]"):
            word = re.search(r"[A-Za-z0-9_$]*$", before).group(0)
            if word not in _TYPE_CONTEXT:
                return True
    return False


def _visible_class(name: str, path: str, source: str, reach: _Reach, repo_dir: str) -> bool:
    """Whether ``name`` in this file surely means a repository class of that name.

    It does when an import line here spells it, or a Java, Kotlin, Groovy or
    Scala file declares it in this file's own package. Anything else may be
    resolved dynamically -- a Groovy closure's delegate, a script binding --
    and stand for anything.
    """
    from flag_cleanup import cross_file_java

    token = _token(name)
    if any(token.search(line) for line in _IMPORT_LINE.findall(source)):
        return True
    try:
        package = _package(_code(source))
    except _NoPackage:
        return False
    if cross_file_java._JvmTypes(repo_dir).top_level_type(name, package):
        return True
    return any(
        guest.package == package and name in guest.types for guest in reach.guests.values()
    )


def _first_line(source: str, offset: int) -> tuple[int, str]:
    line = source.count("\n", 0, offset) + 1
    return line, source.splitlines()[line - 1].strip()


def _refusal_in(
    path: str, source: str, decl: xk.KeyDeclaration, reach: _Reach | None, repo_dir: str
) -> int | None:
    """The offset of a mention of ``decl`` that may read it, if there is one."""
    occurrences = [m.start() for m in _token(decl.name).finditer(source)]
    if not occurrences:
        return None
    if decl.owner is None or reach is None:
        return occurrences[0]
    if any(_token(name).search(source) for name in reach.reaching):
        return occurrences[0]
    for start in occurrences:
        qualifier = re.search(
            r"([A-Za-z_][A-Za-z0-9_$]*)[ \t]*\.[ \t]*$", source[max(0, start - 200) : start]
        )
        if qualifier is None:
            return start
        name = qualifier.group(1)
        if (
            not name[0].isupper()
            or name in reach.reaching
            or name in reach.values
            or _used_as_variable(source, name)
            or not _visible_class(name, path, source, reach, repo_dir)
        ):
            return start
    return None


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    """Never a read: a refusal for the first mention that may reach a declaration."""
    reaches: dict[str, _Reach | None] = {}
    for decl in declarations:
        if decl.owner is not None and decl.owner not in reaches:
            reaches[decl.owner] = _reach(repo_dir, decl.owner)
        reach = reaches.get(decl.owner) if decl.owner is not None else None
        offset = _refusal_in(path, source, decl, reach, repo_dir)
        if offset is not None:
            line, text = _first_line(source, offset)
            return [], [xk.Refusal(path, line, xk.OTHER_LANGUAGE, text)]
    return [], []


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    """None: this tool never removes a Groovy or Scala constant."""
    return []


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """None that a Java or Kotlin reader could resolve to.

    Neither language has a top-level field a Java or Kotlin import can name
    (a Scala 3 top-level ``val`` lives in a synthetic ``package`` object), so
    only a type declared here can compete with ours.
    """
    return set()


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """``(package, name)`` of every type-like declaration here, nested ones too.

    Read with comments and strings removed, so a spec's prose (``def "class
    CheckoutGate keeps its key"()``) declares nothing. Otherwise over-reports
    on purpose (a nested type counts): a competing owner only voids a
    declaration. A Scala package block makes the file fail closed.
    """
    code = _code(source)
    try:
        package = _package(code)
    except _NoPackage:
        raise xk.DeclarationsUnreadable(path) from None
    return {(package, name) for name in _TYPE_DECL.findall(code)}


#: A string or a comment, whichever starts first: a ``//`` or ``/*`` inside a
#: string (``'http://x'``, ``"lib/*"``) is the string's.
_STRING_OR_COMMENT = re.compile(
    r'(?P<string>"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')|//[^\n]*|/\*.*?\*/', re.S
)
_WORD_BEFORE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*$")
_MEMBER_AFTER = re.compile(r"[ \t]*\??\.[ \t]*([A-Za-z_][A-Za-z0-9_]*)")
#: ``Mock(`` (or ScalaMock's ``mock[``) right before a class, and ``)``, ``]``
#: or ``,`` (``.class`` allowed) after it.
_CALL_BEFORE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*[(\[][ \t]*$")
#: A type position: ``Keys gate = Mock()``, ``go(Keys gate)``, Scala ``x: Keys``.
#: The class is named, not taken, and a variable of it holds an instance. The
#: ``:`` form is Scala's only: in Groovy ``[gate: Keys]`` hands the class on.
_DECLARES_AFTER = re.compile(r"[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*[=,);:]")
_TYPE_BEFORE = re.compile(r"[A-Za-z0-9_)\]][ \t]*:[ \t]*$")
_ARGUMENT_AFTER = re.compile(r"[ \t]*(?:\.[ \t]*class[ \t]*)?[,)\]]")
#: Words before a type's name that only declare the type, name it as a
#: supertype (whose subtype is then judged in turn), or construct one.
_NOT_TAKEN = frozenset({
    "class", "interface", "trait", "enum", "object", "record", "extends", "implements", "with", "new",
})


def _members(repo_dir: str, name: str) -> frozenset[str]:
    """What ``name.<member>`` can read from a Java or Kotlin type of that
    name without reaching its other fields: a field, a nested type, a method
    or a Kotlin member. Empty for a Groovy or Scala type, which is not read."""
    from flag_cleanup import cross_file_java, cross_file_kotlin

    members: set[str] = set()
    for info in cross_file_java._type_index(repo_dir).by_name.get(name, ()):
        members |= info.fields | info.member_types | info.methods
    for kotlin_info in cross_file_kotlin._kotlin_index(repo_dir).by_name.get(name, ()):
        members |= kotlin_info.names
    return frozenset(members)


def constant_escapes(
    path: str, source: str, constants: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[tuple[str, xk.Refusal]]:
    """``(declaring file, refusal)`` at each place in ``path`` that may take a
    Java or Kotlin key constant's owner whole, read by tokens as every guest
    mention is: the owner's name, or that of any type that may extend it,
    anywhere outside a comment or an import (``Keys.class``, ``Keys.fields``,
    ``classOf[Keys]``, ``Keys`` handed on, a GString reading it) unless it is
    declared, extended or constructed there, sits in a type position
    (``Keys gate = Mock()``, ``x: Keys``), is the first argument of a test
    double or a logger (``Mock(Keys)``, see
    :data:`~flag_cleanup.cross_file_java.NOT_REFLECTING`), or is followed by
    ``.<member>`` of a Java or Kotlin type of that name; and a string holding the class's
    binary name (``Class.forName("app.Keys")``). Fails closed: a member list
    an index could not read whole is taken to be empty.
    """
    from flag_cleanup import cross_file_java

    reach = cross_file_java.owner_reach(constants, repo_dir)
    if not reach:
        return []
    names = {d.name for d in constants}
    scala = path.endswith(GUEST_EXTENSIONS["scala"])
    # Comments blanked in place, so offsets still give lines; strings kept,
    # since a GString can read the class.
    code = _STRING_OR_COMMENT.sub(
        lambda m: m.group(0) if m.group("string") else re.sub(r"[^\n]", " ", m.group(0)), source
    )
    found: set[tuple[str, xk.Refusal]] = set()

    def add(holders, offset: int) -> None:
        line, text = _first_line(source, offset)
        for holder in holders:
            found.add((holder, xk.Refusal(path, line, xk.UNSUPPORTED_FORM, text)))

    for match in _STRING_OR_COMMENT.finditer(code):
        if match.group("string"):
            add(reach.by_string(match.group(0)[1:-1]), match.start())
    for name, holders in reach.names().items():
        members = _members(repo_dir, name) - names
        for match in _token(name).finditer(code):
            line_start = code.rfind("\n", 0, match.start()) + 1
            if re.match(r"[ \t]*(?:import|package)\b", code[line_start:]):
                continue
            before = _WORD_BEFORE.search(code, line_start, match.start())
            if before is not None and before.group(1) in _NOT_TAKEN:
                continue
            call = _CALL_BEFORE.search(code, line_start, match.start())
            if (
                call is not None
                and call.group(1) in cross_file_java.NOT_REFLECTING | cross_file_java.NOT_REFLECTING_ANNOTATIONS
                and _ARGUMENT_AFTER.match(code, match.end())
            ):
                continue  # `Mock(Keys)`, `mock(Keys.class)`, `@Import(Keys)`
            if _DECLARES_AFTER.match(code, match.end()) or (
                scala and _TYPE_BEFORE.search(code, line_start, match.start())
            ):
                continue  # a type position
            after = _MEMBER_AFTER.match(code, match.end())
            if after is not None and (after.group(1) in members or after.group(1) in names):
                continue  # a member read, or a mention `references` judges
            add(holders, match.start())
    return sorted(found, key=lambda hit: (hit[0], hit[1].line, hit[1].text))


for _language in GUEST_EXTENSIONS:
    xk.register(
        _language,
        xk.Profile(
            language_family=frozenset({_language}),
            declarations=declarations,
            references=references,
            declared_names=declared_names,
            declared_classifiers=declared_classifiers,
            constant_escapes=constant_escapes,
        ),
    )
