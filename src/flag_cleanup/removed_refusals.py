"""Find code that still handles a refusal the removal deleted. Report only.

A gate's check often ends in a refusal: ``throw Failure("CODE: the feature
is not enabled.")``, ``raise Forbidden("…")``, ``return "…"``. Once the check
goes, the server can never send that response, yet its consumers stay: a
client that maps the code to friendlier wording, a test that feeds it the
server's message, a tool description that promises it. None of them names
the flag key, so the key-based scans never see them.

The coupling is the message text, so that is what is searched. The run's own
diff gives the messages: a string literal in a deleted statement that throws
or returns it (not a log line, not one piece of a concatenation), long enough
to be a sentence, and not written again on an added line. Every file of the
checkout, in any language, swept or not, is then searched for it as exact
text.

A refusal's CODE (``FEATURE_NOT_ENABLED``) is deliberately not searched on its
own: a code is shared by every gate, so it would light up for each of them.
A hit that is itself a refusal with the message is new code that still sends
it, so it is left out. Nothing is edited; whether a consumer is now dead is
the reviewer's call.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: A statement that refuses or answers with a message. Class names are matched
#: case-sensitively (``Error``, ``Failure``, ``BadRequest`` …), so prose and
#: ordinary identifiers (``failure_count``, ``errored``) do not count.
_REFUSES = re.compile(
    r"\b(?:throw|raise|return|abort|reject|panic)\b"
    r"|\b[A-Z]\w*(?:Error|Exception|Failure)\b|\b(?:Error|Failure|Exception)\b"
    r"|\b(?:BadRequest|Forbid|Forbidden|Problem|Conflict|Unauthorized|NotFound|UnprocessableEntity)\b"
    r"|\berrors?\s*:|\berrors?\.\w+\(|\bErrorf\b"
)
#: A statement that only reports: its string is a log line, not a response.
_LOGS = re.compile(r"\blog\w*\.|\blogger\w*\.|\bconsole\.|\blogging\.|\bLog\w*\(", re.IGNORECASE)
#: A quoted string with its escapes, in the quote styles of the supported
#: languages: C# verbatim (``@"a ""b"""``), interpolated, single, backtick.
_LITERAL = re.compile(
    r"""\$?@"(?:[^"]|"")*"|\$?"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`"""
)
#: An error code that prefixes a message: ``FEATURE_NOT_ENABLED: …``.
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,}:\s*")
#: A placeholder inside a message: ``{name}``, ``${name}``, ``%s``, ``%d``.
_PLACEHOLDER = re.compile(r"\$?\{[^{}]*\}|%[-+# 0-9.]*[sdvqfx]")
#: How many statements back from the string the refusing keyword may stand.
_CONTEXT_LINES = 6
_MIN_CHARS = 15
_MIN_WORDS = 3
_MAX_MESSAGES = 20
_MAX_LINES = 500
_GREP_SECONDS = 30
_RECORD = re.compile(rb"([^\0]+)\0(\d+)\0([^\n]*)\n")


def removed_messages(diff: str) -> tuple[str, ...]:
    """The refusal messages ``diff`` deleted and did not write again, in the
    order they were deleted. At most :data:`_MAX_MESSAGES`, none holding a
    line break."""
    runs: list[list[str]] = []
    added: list[str] = []
    previous_removed = False
    for line in diff.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            previous_removed = False
            continue
        if line.startswith("-"):
            if not previous_removed:
                runs.append([])
            runs[-1].append(line[1:])
            previous_removed = True
        else:
            previous_removed = False
            if line.startswith("+"):
                added.append(line[1:])
    written = "\n".join(added)
    found: dict[str, None] = {}
    for run in runs:
        for index, line in enumerate(run):
            # The whole deleted statement: the lines above it in the same
            # deleted run, so a keyword on the first line of a wrapped call counts.
            context = "\n".join(run[max(0, index - _CONTEXT_LINES) : index + 1])
            for match in _LITERAL.finditer(line):
                if _REFUSES.search(context) is None or _logs(run, index):
                    continue
                if _concatenated(line, match, run[:index]):
                    continue
                for message in _messages(match.group(0)):
                    if message not in written:
                        found.setdefault(message)
    return tuple(found)[:_MAX_MESSAGES]


def _logs(run: list[str], index: int) -> bool:
    """Whether the string sits in a logging call: its own line, or the line
    above when that one opens the call."""
    own = run[index]
    above = run[index - 1] if index and run[index - 1].rstrip().endswith("(") else ""
    return _LOGS.search(own) is not None or _LOGS.search(above) is not None


def _concatenated(line: str, match: re.Match[str], above: list[str]) -> bool:
    """Whether the literal is one piece of a longer string built with ``+`` or
    ``..``, on this line or continued from the previous non-blank one: its
    text alone is not the message."""
    before = line[: match.start()].rstrip()
    after = line[match.end() :].lstrip()
    if before.endswith(("+", "..")) or after.startswith(("+", "..")):
        return True
    if before:
        return False
    previous = next((text.rstrip() for text in reversed(above) if text.strip()), "")
    return previous.endswith(("+", ".."))


def _unquote(literal: str) -> str:
    literal = literal.lstrip("$")
    if literal.startswith('@"'):
        return literal[2:-1].replace('""', '"')
    return literal[1:-1].replace('\\"', '"').replace("\\'", "'")


def _messages(literal: str) -> list[str]:
    """The fixed sentences of one string literal: its text less the code
    prefix, split where a placeholder stands, each part long enough to name
    a message and not a word, and none spanning lines."""
    body = _CODE.sub("", _unquote(literal))
    parts = [part.strip() for part in _PLACEHOLDER.split(body)]
    return [
        p
        for p in parts
        if len(p) >= _MIN_CHARS
        and len(p.split()) >= _MIN_WORDS
        and any(c.isalpha() for c in p)
        and "\n" not in p
        and "\r" not in p
        and "\\n" not in p
    ]


#: A path part, or a file stem, that marks a test: a test may mock the very
#: refusal the run removed, which is what the report exists to find.
_TEST_SEGMENT = re.compile(r"^(?:tests?|specs?|__tests__|__mocks__)$", re.IGNORECASE)
_TEST_STEM = re.compile(r"(?:^test|test$|tests$|spec$|_test$|\.test$|\.spec$)", re.IGNORECASE)
_REFUSAL_STATEMENT = re.compile(r"\s*(?:[{}]\s*)*(?:else\s+)?(?:throw|raise|return)\b")


def _test_path(path: str) -> bool:
    parts = Path(path).parts
    stem = Path(parts[-1]).stem if parts else ""
    return any(_TEST_SEGMENT.match(part) for part in parts[:-1]) or _TEST_STEM.search(stem) is not None


def _is_live_refusal(text: str, message: str, path: str) -> bool:
    """Whether a line is itself a ``throw``, ``raise`` or ``return`` of
    ``message`` in a file that is not a test: new code that still sends it,
    not a consumer of the response the run removed. Everything else stays,
    test mocks (``mockRejectedValue(new Error(…))``, ``side_effect = …``, a
    mocked HTTP response) above all."""
    return (
        not _test_path(path)
        and _REFUSAL_STATEMENT.match(text) is not None
        and any(message in _unquote(m.group(0)) for m in _LITERAL.finditer(text))
    )


def find(root: Path, messages: tuple[str, ...], skip_dirs: frozenset[str]) -> tuple[tuple[str, int, str], ...]:
    """``(path, line, text)`` for each line of the checkout at ``root`` that
    spells one of ``messages`` exactly, paths relative to ``root``, at most
    :data:`_MAX_LINES`. A line that is itself a refusal with the message is
    left out."""
    found: list[tuple[str, int, str]] = []
    exclude = [f":(glob,exclude)**/{name}/**" for name in sorted(skip_dirs)]
    for message in messages[:_MAX_MESSAGES]:
        if "\n" in message or "\r" in message:
            continue
        try:
            result = subprocess.run(
                ["git", "grep", "-n", "-I", "-z", "--untracked", "-F", "-e", message, "--", ".", *exclude],
                cwd=root,
                capture_output=True,
                check=False,
                timeout=_GREP_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode not in (0, 1):
            continue
        # With ``-z`` each record is ``path NUL line NUL text LF``. A path can
        # hold a line feed; the text of a matching line cannot.
        for found_path, number, text in _RECORD.findall(result.stdout):
            relative = found_path.decode("utf-8", "surrogateescape")
            if any(part in skip_dirs for part in Path(relative).parts[:-1]):
                continue
            line = text.decode("utf-8", "replace").strip()
            if _is_live_refusal(line, message, relative):
                continue
            found.append((relative, int(number), line[:160]))
        if len(found) >= _MAX_LINES:
            break
    return tuple(dict.fromkeys(found))[:_MAX_LINES]
