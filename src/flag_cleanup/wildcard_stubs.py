"""Find test doubles that stub a removed read with a WILDCARD key. Report only.

The test-double pass (:mod:`flag_cleanup.mock_stubs`) decides a stub by its
key: ``when(client.isEnabled("k")).thenReturn(false)`` is about flag ``k``,
so its literal says which branch the test exercises. A stub whose key is a
matcher instead says nothing about which flag it serves:

    every { unleash.isEnabled(any()) } returns false                  // MockK
    when(unleash.isEnabled(anyString())).thenReturn(false);           // Mockito
    fm.Setup(m => m.IsEnabledAsync(It.IsAny<string>())).ReturnsAsync(true); // Moq

It answers for EVERY flag read through that call, the removed one among them.
Once the removal folds the read, a test that relied on the wildcard to reach
the removed branch ("skips when the toggle is off") fails, and nothing else in
the run mentions it: the line names no key, so neither the residual scan nor
the test-double pass sees it.

Nothing here edits. Which flag a wildcard stub was written for is a question
about the test's intent, and the same line usually serves every other flag
the code under test still reads, so deleting the stub or the test would be a
guess. The pull request lists each line instead, and only for a read callee
the run's own diff removed a call of: a wildcard stub of a call the removal
did not touch cannot have stubbed the removed flag.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from tree_sitter import Node

from flag_cleanup import mock_stubs, rule_synthesis

#: The suffixes scanned, and the test-double profile each is parsed with.
_LANGUAGE_BY_SUFFIX = {
    ".java": "java",
    ".kt": "kt",
    ".kts": "kt",
    ".cs": "csharp",
    ".dart": "dart",
    ".go": "go",
    **{suffix: "ts" for suffix in (".ts", ".mts", ".cts")},
    ".tsx": "tsx",
    **{suffix: "js" for suffix in (".js", ".jsx", ".mjs", ".cjs")},
}
SUFFIXES = frozenset(_LANGUAGE_BY_SUFFIX)

#: Argument matchers that accept any key, as the FIRST argument of a read:
#: Mockito / mockito-kotlin / MockK (``any()``, ``anyString()``,
#: ``any<String>()``, ``any(String.class)``, ``anyOrNull()``, ``isA(…)``,
#: ``notNull()``, ``ofType<String>()``, ``allAny()``), Moq
#: (``It.IsAny<string>()``, ``It.IsNotNull<string>()``), NSubstitute
#: (``Arg.Any<string>()``), gomock (``gomock.Any()``), ts-mockito
#: (``anything()``, ``anyString()``), Dart mockito / mocktail (``any``,
#: ``any()``, ``anyNamed('k')``, ``captureAny``), FakeItEasy
#: (``A<string>._``, ``A<string>.Ignored``). A qualifier chain may precede
#: the call (``ArgumentMatchers.anyString()``, ``Mockito.any()``).
_WILDCARD = (
    r"(?:"
    r"(?:[A-Za-z_$][\w$]*\s*\.\s*)*"
    r"(?:any|anyString|anyObject|anyOrNull|anything|isA|notNull|allAny|ofType|anyNamed"
    r"|Any|IsAny|IsNotNull|captureAny)"
    r"(?:\s*<[^<>()]*>)?\s*\((?:[^()]|\([^()]*\))*\)"
    r"|A\s*<[^<>()]*>\s*\.\s*(?:_|Ignored)(?![\w$])"
    r"|(?:any|captureAny)(?=\s*[,)])"
    r")"
)


def removed_reads(diff: str, accessors: tuple[str, ...]) -> frozenset[str]:
    """The read callees ``diff`` removed a call of: more calls on its deleted
    lines than on its added ones. Asked of every language's SDK reads and the
    configured accessors, since the diff may span several languages."""
    names = set(accessors)
    for language in _LANGUAGE_BY_SUFFIX.values():
        names |= mock_stubs._PROFILES[language].reads
    removed: set[str] = set()
    deleted = [line[1:] for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
    added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    for name in names:
        call = re.compile(rf"(?<![\w$]){re.escape(name)}\s*\(")
        if sum(len(call.findall(line)) for line in deleted) > sum(len(call.findall(line)) for line in added):
            removed.add(name)
    return frozenset(removed)


def _pattern(names: Iterable[str]) -> re.Pattern[str]:
    alternation = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<![\w$])(?:{alternation})\s*\(\s*{_WILDCARD}")


def _describes(text: str, pattern: re.Pattern[str]) -> bool:
    return bool(mock_stubs._DSL.search(text)) and bool(pattern.search(text))


def lines_in(source: str, language: str, names: Iterable[str]) -> list[str]:
    """Every statement of ``source`` that stubs or verifies one of ``names``
    with a wildcard key, whitespace-collapsed. The innermost statement only:
    a test or a spec block holding such a line is not one itself."""
    names = tuple(rule_synthesis.accessors_for(language, tuple(names)))
    if not names:
        return []
    pattern = _pattern(names)
    if not pattern.search(source):
        return []
    profile = mock_stubs._PROFILES[language]
    data = source.encode("utf-8")
    root = profile.parse(source, language)
    statements = mock_stubs._statements(root, data, profile, lambda text: _describes(text, pattern))
    found: list[str] = []
    for node in statements:
        if any(other.id != node.id and _inside(other, node) for other in statements):
            continue
        # `fm\n    .Setup(…)` reads as `fm.Setup(…)`.
        found.append(re.sub(r"\s+\.", ".", " ".join(mock_stubs._text(node, data).split())))
    return found


def _inside(inner: Node, outer: Node) -> bool:
    return outer.start_byte <= inner.start_byte and inner.end_byte <= outer.end_byte


def find(paths: Iterable[Path], names: frozenset[str]) -> tuple[tuple[str, str], ...]:
    """``(path, statement)`` for each wildcard stub of ``names`` in ``paths``."""
    if not names:
        return ()
    found: list[tuple[str, str]] = []
    for path in paths:
        language = _LANGUAGE_BY_SUFFIX.get(Path(path).suffix)
        if language is None:
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.extend((str(path), text) for text in lines_in(source, language, names))
    return tuple(found)
