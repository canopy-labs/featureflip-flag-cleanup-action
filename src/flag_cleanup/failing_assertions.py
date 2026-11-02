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


#: One string literal as a message, as the rules allow: anything else could be
#: the last reader of a local.
_STRING = r"""(?:"[^"\n]*"|'[^'\n]*')"""
_MESSAGE = rf"(?:\s*,\s*{_STRING})?"
#: JUnit 4 and a Kotlin call of it put the message first.
_LEADING_MESSAGE = rf"(?:{_STRING}\s*,\s*)?"
#: The literal pairs an equality assertion fails on.
_UNEQUAL = ((_T, _F), (_F, _T))

_CSHARP = [
    _call(r"\bAssert\s*\.\s*(?:True|IsTrue|That)", _F + _MESSAGE),
    _call(r"\bAssert\s*\.\s*(?:False|IsFalse)", _T + _MESSAGE),
    *[_call(r"\bAssert\s*\.\s*(?:Equal|AreEqual)", pair) for pair in _either(*_UNEQUAL)],
]

_JVM_PREFIX = r"(?<![\w.])(?:(?:Assert|Assertions)\s*\.\s*)?"
_JVM = [
    _call(_JVM_PREFIX + "assertTrue", _LEADING_MESSAGE + _F + _MESSAGE),
    _call(_JVM_PREFIX + "assertFalse", _LEADING_MESSAGE + _T + _MESSAGE),
    *[
        _call(_JVM_PREFIX + "assertEquals", _LEADING_MESSAGE + pair + _MESSAGE)
        for pair in _either(*_UNEQUAL)
    ],
]

_TS = [
    _call(r"(?<![\w.$])assert", _F + _MESSAGE),
    _call(r"(?<![\w.$])assert\s*\.\s*ok", _F + _MESSAGE),
    *[
        _call(r"(?<![\w.$])assert\s*\.\s*(?:equal|strictEqual|deepEqual|deepStrictEqual)", pair + _MESSAGE)
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
_PY_MESSAGE = rf"(?:\s*,\s*(?:msg\s*=\s*)?{_STRING})?"
_PYTHON = [
    r"^[ \t]*assert[ \t]+False\b",
    _call(r"\bself\s*\.\s*assertTrue", "False" + _PY_MESSAGE),
    _call(r"\bself\s*\.\s*assertFalse", "True" + _PY_MESSAGE),
    *[
        _call(r"\bself\s*\.\s*(?:assertEqual|assertIs)", pair + _PY_MESSAGE)
        for pair in _either(*_PY_UNEQUAL)
    ],
]

#: testify, on `assert` or `require`, test handle first; an optional single
#: string-literal message, as in the rules.
_GO_PREFIX = r"(?<![\w.])(?:assert|require)\s*\.\s*"
_GO_MESSAGE = r'(?:\s*,\s*(?:"(?:[^"\\\n]|\\.)*"|`[^`]*`))?'
_GO = [
    _call(_GO_PREFIX + "Truef?", r"\w+", _F + _GO_MESSAGE),
    _call(_GO_PREFIX + "Falsef?", r"\w+", _T + _GO_MESSAGE),
    *[_call(_GO_PREFIX + "Equalf?", r"\w+", pair + _GO_MESSAGE) for pair in _either(*_UNEQUAL)],
]

#: RSpec `expect(<literal>).to be|eq(<literal>)`, parenthesised or not, and the
#: truthy/falsey matchers; minitest's receiverless `assert`/`refute`/`assert_not`/
#: `assert_equal`. One string literal as a message, as in the rules.
_RB_BARE = r"(?<![\w.:@$])"
_RB_END = r"(?=[ \t]*(?:\)|;|#|$))"


def _rb_call(callee: str, *args: str) -> str:
    inner = r"\s*,\s*".join(args) + _MESSAGE
    return rf"{_RB_BARE}{callee}(?:\s*\(\s*{inner}\s*\)|[ \t]+{inner}{_RB_END})"


def _rb_expect(actual: str, matcher: str) -> str:
    return (
        rf"{_RB_BARE}expect\s*\(\s*{actual}\s*\)\s*\.\s*to"
        rf"(?:\s*\(\s*{matcher}\s*\)|[ \t]+{matcher}{_RB_END})"
    )


_RUBY = [
    *[
        _rb_expect(actual, rf"(?:be|eq)(?:\s*\(\s*{expected}\s*\)|[ \t]+{expected}\b)")
        for actual, expected in _UNEQUAL
    ],
    _rb_expect(_F, r"be_truthy\b"),
    _rb_expect(_T, r"be_falsey\b"),
    _rb_call("assert", _F),
    _rb_call("refute", _T),
    _rb_call("assert_not", _T),
    *[_rb_call("assert_equal", pair) for pair in _either(*_UNEQUAL)],
]

#: PHPUnit on `$this->`, `self::`, `static::` or `Assert::`, with at most a
#: string message; PHP's literals are case-insensitive.
_PHP_PREFIX = r"(?:\$this\s*->|(?<![\w$\\])(?:self|static|Assert)\s*::)\s*"
_PHP_T, _PHP_F = r"(?i:true)", r"(?i:false)"
_PHP = [
    _call(_PHP_PREFIX + "assertTrue", _PHP_F + _MESSAGE),
    _call(_PHP_PREFIX + "assertFalse", _PHP_T + _MESSAGE),
    *[
        _call(_PHP_PREFIX + "(?:assertSame|assertEquals)", pair + _MESSAGE)
        for pair in _either((_PHP_T, _PHP_F), (_PHP_F, _PHP_T))
    ],
]

#: XCTest, called bare, and Swift Testing's `#expect`; at most a string message.
_SWIFT = [
    _call(r"(?<![\w.])XCTAssert(?:True)?", _F + _MESSAGE),
    _call(r"(?<![\w.])XCTAssertFalse", _T + _MESSAGE),
    *[_call(r"(?<![\w.])XCTAssertEqual", pair + _MESSAGE) for pair in _either(*_UNEQUAL)],
    _call(r"#expect", _F + _MESSAGE),
]

#: package:test / flutter_test `expect`, called bare, at most with a `reason:`.
_DART_EXPECT = r"(?<![\w.$])expect"
_DART_REASON = rf"(?:\s*,\s*reason\s*:\s*{_STRING})?"
_DART = [
    _call(_DART_EXPECT, _F, "isTrue" + _DART_REASON),
    _call(_DART_EXPECT, _T, "isFalse" + _DART_REASON),
    *[_call(_DART_EXPECT, pair) for pair in _either(*_UNEQUAL)],
    *[_call(_DART_EXPECT, actual, _call("equals", expected)) for actual, expected in _UNEQUAL],
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
    "go": _GO,
    "ruby": _RUBY,
    "php": _PHP,
    "swift": _SWIFT,
    "dart": _DART,
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
