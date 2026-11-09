"""Whether ``eq`` in a Java or Kotlin test is the mocking library's matcher.

A stub whose key is ``eq("k")`` is about flag ``k`` only when ``eq`` is the
library's equality matcher. A name a file cannot bind for certain (another
star import, a same-package helper, a base class in another file) is not
trusted, and a stub that is not trusted is left to the run's refusals.

A bare ``eq`` is trusted only when BOTH hold:

* the file imports the library's matcher by name (``import org.mockito.kotlin.eq``,
  ``import static org.mockito.ArgumentMatchers.eq;``, ``Mockito.eq``,
  ``Matchers.eq``) -- a star import alone does not count -- or, for MockK, the
  ``eq`` sits lexically inside an ``every { }`` / ``coEvery { }`` /
  ``verify { }`` / ``coVerify { }`` lambda of a file that imports ``io.mockk``,
  where it is the scope's own member;
* no JVM source file in the repository declares a function, method, field,
  property or parameter named ``eq``. The scan is a token scan that
  over-refuses on purpose.

A qualified ``eq`` is trusted when written fully qualified
(``org.mockito.ArgumentMatchers.eq``), or when its qualifier (``Mockito``,
``ArgumentMatchers``, ``Matchers``) is imported from ``org.mockito`` and no file
of the repository declares a type of that name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: ``import`` lines may end in a comment; a line that starts like an import and
#: is not recognised here makes the whole file untrusted (fail closed).
_IMPORT = re.compile(
    r"^[ \t]*import\s+(static\s+)?([\w.]+?)(\.\*)?(?:\s+as\s+(\w+))?"
    r"[ \t]*;?[ \t]*(?://[^\n]*|/\*.*?\*/)?[ \t\r]*$",
    re.M,
)
_IMPORT_LINE = re.compile(r"^[ \t]*import\b[^\n]*", re.M)

#: Where the library's matcher lives, by import.
_ARGUMENT_MATCHER_CLASSES = ("ArgumentMatchers", "Mockito", "Matchers")
_BARE_EQ_IMPORTS = frozenset(
    {f"org.mockito.{c}.eq" for c in _ARGUMENT_MATCHER_CLASSES} | {"org.mockito.kotlin.eq"}
)
_MOCKK_SCOPES = frozenset({"every", "coEvery", "verify", "coVerify"})

#: Fully-qualified spellings, whitespace-insensitive.
FULLY_QUALIFIED = re.compile(r"org\.mockito\.(?:(?:ArgumentMatchers|Mockito|Matchers)|kotlin)")

# A declaration of ``eq`` anywhere: over-refusing is the point.
_DECLARES_EQ = re.compile(
    r"\b(?:fun|val|var)\b[^=\n(]*?`?\beq\b`?"  # Kotlin function / property, with a receiver or type parameters
    r"|\beq\s*:"  # Kotlin parameter or property type
    r"|\beq\s*->"  # lambda parameter
    r"|(?<![\w$.])(?!(?:return|new|else|throw|case|yield|in|is|as|to|and|or)\b)[\w$>\]?]+\s+`?eq`?\s*[(=;,)]"  # Java
)
_DECLARES_TYPE = re.compile(
    r"\b(?:class|interface|object|enum|typealias|record)\s+(" + "|".join(_ARGUMENT_MATCHER_CLASSES) + r")\b"
)


@dataclass(frozen=True)
class _Facts:
    declares_eq: bool
    types: frozenset[str]


_repo: str | None = None
_cache: dict[str, _Facts] = {}


_SOURCE_SUFFIXES = (".java", ".kt", ".kts", ".groovy", ".scala")
_BUILD_FILES = frozenset({"build.gradle", "build.gradle.kts", "pom.xml"})


def _pruned(directory: str, siblings: list[str]) -> bool:
    """Whether the scan skips ``directory``. ``build`` and ``target`` are output
    directories only beside a build file; a package of that name is source."""
    if directory in (".git", "node_modules", ".gradle"):
        return True
    return directory in ("build", "target") and any(
        name in _BUILD_FILES or name.startswith("settings.gradle") for name in siblings
    )


def use_repo(repo_dir: str | None) -> None:
    """Name the checkout whose JVM files the rule scans (``None``: this file only)."""
    global _repo
    _repo = repo_dir


def forget() -> None:
    _cache.clear()


def _scan(texts) -> _Facts:
    declares = False
    types: set[str] = set()
    for text in texts:
        declares = declares or _DECLARES_EQ.search(text) is not None
        types.update(_DECLARES_TYPE.findall(text))
    return _Facts(declares, frozenset(types))


def _facts(source: str) -> _Facts:
    own = _scan([source])
    if _repo is None:
        # No checkout named for this run: nothing is known about the rest of
        # the repository, so a bare `eq` cannot be trusted.
        return _Facts(True, own.types)
    if _repo not in _cache:

        def texts():
            for root, dirs, files in os.walk(_repo):
                dirs[:] = [d for d in dirs if not _pruned(d, files)]
                for name in files:
                    if name.endswith(_SOURCE_SUFFIXES):
                        try:
                            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as handle:
                                yield handle.read()
                        except OSError:
                            continue

        _cache[_repo] = _scan(texts())
    repo = _cache[_repo]
    return _Facts(repo.declares_eq or own.declares_eq, repo.types | own.types)


#: Whether an import was `import static` (Java), keyed like the tuples below.
_STATIC: dict[tuple[str, bool], bool] = {}

#: A star import of one of these supplies the library's own ``eq``.
_LIBRARY_STARS = frozenset(
    {f"org.mockito.{c}" for c in _ARGUMENT_MATCHER_CLASSES} | {"org.mockito.kotlin"}
)
#: Star imports from these packages are known not to supply a function named
#: ``eq``, so they cannot compete with the library's. Anything else could.
_INERT_STAR_PREFIXES = (
    "org.mockito.", "io.mockk", "org.junit.", "org.hamcrest.", "org.assertj.",
    "java.", "javax.", "jakarta.", "kotlin.", "kotlinx.coroutines",
)


def _star_trusts(imports: list[tuple[str, bool, str | None]], language: str) -> bool:
    """A library star import trusts a bare ``eq`` only when no OTHER star import
    could also supply a function named ``eq``. In Java only `import static … .*`
    imports methods; in Kotlin every star import can."""
    library = False
    for name, star, _alias in imports:
        if not star:
            continue
        if name in _LIBRARY_STARS:
            library = True
            continue
        if language == "java" and not _STATIC.get((name, True), False):
            continue
        if not (name + ".").startswith(_INERT_STAR_PREFIXES) and name not in ("io.mockk",):
            return False
    return library


def _imports(source: str) -> list[tuple[str, bool, str | None]] | None:
    """Every import as ``(name, star, alias)``; ``None`` when a line is not understood."""
    found = []
    _STATIC.clear()
    for line in _IMPORT_LINE.finditer(source):
        match = _IMPORT.fullmatch(line.group(0).rstrip("\r"))
        if match is None:
            return None
        found.append((match.group(2), match.group(3) is not None, match.group(4)))
        _STATIC[(match.group(2), match.group(3) is not None)] = match.group(1) is not None
    return found


def _eq_unshadowed(source: str) -> list[tuple[str, bool, str | None]] | None:
    imports = _imports(source)
    if imports is None or _facts(source).declares_eq:
        return None
    return imports


def bare_eq_is_library(source: str, language: str) -> bool:
    """Whether an unqualified ``eq(...)`` is the library's by an explicit import."""
    imports = _eq_unshadowed(source)
    if imports is None:
        return False
    trusted = False
    for name, star, alias in imports:
        if alias == "eq":
            return False
        if not star and alias is None and name.rsplit(".", 1)[-1] == "eq":
            if name not in _BARE_EQ_IMPORTS:
                return False
            trusted = True
    return trusted or _star_trusts(imports, language)


def mockk_scope_eq_is_library(source: str) -> bool:
    """Whether an ``eq`` inside an ``every { }``-style lambda is MockK's scope member."""
    imports = _eq_unshadowed(source)
    if imports is None:
        return False
    if any(
        alias == "eq" or (not star and alias is None and name.rsplit(".", 1)[-1] == "eq" and name not in _BARE_EQ_IMPORTS)
        for name, star, alias in imports
    ):
        return False
    return any(name == "io.mockk" or name.startswith("io.mockk.") for name, _star, _alias in imports)


def qualifier_is_library(qualifier: str, source: str) -> bool:
    """Whether ``<qualifier>.eq`` names the library's matcher."""
    qualifier = re.sub(r"\s+", "", qualifier)
    if FULLY_QUALIFIED.fullmatch(qualifier):
        return True
    return qualifier in _qualifiers(source)


def _qualifiers(source: str) -> set[str]:
    imports = _imports(source)
    if imports is None:
        return set()
    types = _facts(source).types
    return {
        name.rsplit(".", 1)[-1]
        for name, star, alias in imports
        if not star
        and alias is None
        and name in {f"org.mockito.{c}" for c in _ARGUMENT_MATCHER_CLASSES}
        and name.rsplit(".", 1)[-1] not in types
    }


def inside_mockk_scope(call) -> bool:
    """Whether a Kotlin syntax node sits inside an ``every { }``-style lambda."""
    node = call.parent
    while node is not None:
        if node.type == "call_expression" and node.named_children:
            callee = node.named_children[0]
            if callee.type == "identifier" and callee.text.decode("utf-8", "replace") in _MOCKK_SCOPES:
                return True
        node = node.parent
    return False


def eq_alternation(source: str, language: str, mockk_scope: bool = False) -> str:
    """The regex for the spellings of the equality matcher a stub may wrap a key in.

    ``mockk_scope`` is for the patterns that sit inside ``every { }`` and
    ``verify { }``, where a MockK file's bare ``eq`` is the scope's member.
    """
    names = [r"org\s*\.\s*mockito\s*\.\s*(?:ArgumentMatchers|Mockito|Matchers|kotlin)\s*\.\s*eq"]
    names += [rf"{q}\s*\.\s*eq" for q in sorted(_qualifiers(source))]
    if bare_eq_is_library(source, language) or (mockk_scope and language == "kt" and mockk_scope_eq_is_library(source)):
        names.append("eq")
    return "(?:" + "|".join(names) + ")"
