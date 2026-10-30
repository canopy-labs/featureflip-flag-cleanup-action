"""Find an assertion the fold turned into one that can only fail.

A flag read inside an assertion folds to a literal. When that literal makes the
assertion pass whatever happens (``Assert.True(true)``), the rules delete the
statement: they are reached only from the fold's own literal, see
``rules/<language>_assertions.toml``. When it makes the
assertion FAIL (``Assert.True(false)``, ``expect(false).toBe(true)``), the test
was about the branch this removal deletes, and nothing here edits it: the
pull request names it, so the reviewer deletes or rewrites the test.

Which calls count as assertions is the same list of test-framework APIs the
rules use. :func:`failing` asks it by text, as a differential: a failing
assertion the customer's file already held is theirs, and is not reported.
"""

from __future__ import annotations

import re
from collections import Counter

_T, _F = r"true", r"false"


def _call(callee: str, *args: str) -> str:
    inner = r"\s*,\s*".join(args)
    return rf"{callee}\s*\(\s*{inner}\s*\)"


def _either(*pairs: tuple[str, str]) -> list[str]:
    return [r"\s*,\s*".join(pair) for pair in pairs]


#: The literal pairs an equality assertion fails on.
_UNEQUAL = ((_T, _F), (_F, _T))

_CSHARP = [
    _call(r"\bAssert\s*\.\s*(?:True|IsTrue|That)", _F),
    _call(r"\bAssert\s*\.\s*(?:False|IsFalse)", _T),
    *[_call(r"\bAssert\s*\.\s*(?:Equal|AreEqual)", pair) for pair in _either(*_UNEQUAL)],
]

_JVM_PREFIX = r"(?<![\w.])(?:(?:Assert|Assertions)\s*\.\s*)?"
_JVM = [
    _call(_JVM_PREFIX + "assertTrue", _F),
    _call(_JVM_PREFIX + "assertFalse", _T),
    *[_call(_JVM_PREFIX + "assertEquals", pair) for pair in _either(*_UNEQUAL)],
]

_TS = [
    _call(r"(?<![\w.$])assert", _F),
    _call(r"(?<![\w.$])assert\s*\.\s*ok", _F),
    *[
        _call(r"(?<![\w.$])assert\s*\.\s*(?:equal|strictEqual|deepEqual|deepStrictEqual)", pair)
        for pair in _either(*_UNEQUAL)
    ],
    *[
        _call(r"(?<![\w.$])expect", actual) + r"\s*\.\s*" + _call(r"(?:toBe|toEqual|toStrictEqual)", expected)
        for actual, expected in _UNEQUAL
    ],
    _call(r"(?<![\w.$])expect", _F) + r"\s*\.\s*" + _call("toBeTruthy"),
    _call(r"(?<![\w.$])expect", _T) + r"\s*\.\s*" + _call("toBeFalsy"),
]

_PY_UNEQUAL = (("True", "False"), ("False", "True"))
_PYTHON = [
    r"^[ \t]*assert[ \t]+False\b",
    _call(r"\bself\s*\.\s*assertTrue", "False"),
    _call(r"\bself\s*\.\s*assertFalse", "True"),
    *[_call(r"\bself\s*\.\s*(?:assertEqual|assertIs)", pair) for pair in _either(*_PY_UNEQUAL)],
]

#: Per language, the failing forms of the assertions the rules delete in
#: their passing forms.
_PATTERNS = {
    "csharp": _CSHARP,
    "java": _JVM,
    "kt": _JVM,
    "ts": _TS,
    "tsx": _TS,
    "js": _TS,
    "python": _PYTHON,
}

_COMPILED = {
    language: re.compile("|".join(f"(?:{p})" for p in patterns), re.MULTILINE)
    for language, patterns in _PATTERNS.items()
}


def _found(text: str, language: str) -> Counter[str]:
    pattern = _COMPILED.get(language)
    if pattern is None:
        return Counter()
    return Counter(" ".join(m.group(0).split()) for m in pattern.finditer(text))


def failing(before: str, after: str, language: str) -> list[str]:
    """Each failing-literal assertion ``after`` holds more of than ``before``,
    whitespace-collapsed, once per new occurrence."""
    if before == after:
        return []
    old = _found(before, language)
    new = _found(after, language)
    return [text for text, count in new.items() for _ in range(count - old.get(text, 0))]
