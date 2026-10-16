"""Shared types and rule rendering for removing an unread constructor parameter.

A removal deletes a constructor parameter and the argument each caller passes
for it. Which callers exist is proven per language (``ctor_param_ts``,
``ctor_param_csharp``); the deletion itself is the engine's, through rules
rendered here.
"""

from __future__ import annotations

import difflib
import re
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"

#: Names a rendered rule can spell inside a query string without escaping.
IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

SENTINEL_PREFIX = "__ff_drop_arg_"

_TEMPLATES = {"ts": "ts_ctor_param.toml", "csharp": "csharp_ctor_param.toml"}
_BLOCK = re.compile(r"^# ctor: (parameter|argument)\n", re.MULTILINE)


@dataclass(frozen=True)
class Site:
    """One argument to delete: a byte span in ``path``."""

    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Removal:
    declaring_path: str
    class_name: str
    param_name: str
    #: The unread row's label: ``Svc.client`` (TS) or ``Svc(IClient client)`` (C#).
    label: str
    #: The parameter's position among the constructor's parameters, 0-based.
    index: int
    sites: tuple[Site, ...]
    #: Exported (TS) or ``public`` (C#): code outside the repository can call it.
    exported: bool


@dataclass(frozen=True)
class Refusal:
    label: str
    reason: str
    #: Not a candidate at all (a field, a method): not worth an INFO line.
    quiet: bool = False


def choose_sentinel(
    texts: Iterable[bytes], token: Callable[[int], str] = secrets.token_hex
) -> str | None:
    """A sentinel identifier that occurs in none of ``texts``, or ``None``.

    Three draws: a collision with 48 random bits is already a sign something
    else is writing these names, and a fourth draw would not be evidence of
    anything.
    """
    texts = list(texts)
    for _ in range(3):
        candidate = SENTINEL_PREFIX + token(6)
        needle = candidate.encode("ascii")
        if not any(needle in text for text in texts):
            return candidate
    return None


def mark(data: bytes, spans: Iterable[tuple[int, int]], sentinel: str) -> bytes:
    """``data`` with every ``(start, end)`` byte span replaced by ``sentinel``."""
    ordered = sorted(spans)
    for (_, end), (start, _) in zip(ordered, ordered[1:]):
        if start < end:
            raise ValueError("overlapping spans")
    if ordered and (ordered[0][0] < 0 or ordered[-1][1] > len(data)):
        raise ValueError("span outside the data")
    replacement = sentinel.encode("ascii")
    for start, end in reversed(ordered):
        data = data[:start] + replacement + data[end:]
    return data


_SPACE = b" \t"
_OPENERS = b"(<["
_CLOSERS = b")>],"


def _deletion_gaps(before: bytes, after: bytes) -> list[int]:
    """Offsets in ``after`` where bytes of ``before`` were deleted.

    Lines first, then characters only inside the lines that differ: a
    character diff of a whole file is quadratic, and minutes on a large one.
    """
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    starts = [0]
    for line in new:
        starts.append(starts[-1] + len(line))
    gaps: list[int] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag == "equal":
            continue
        chunk_old, chunk_new = b"".join(old[i1:i2]), b"".join(new[j1:j2])
        gaps.extend(
            starts[j1] + k1
            for t, _a1, _a2, k1, _k2 in difflib.SequenceMatcher(
                None, chunk_old, chunk_new, autojunk=False
            ).get_opcodes()
            if t == "delete"
        )
    return gaps


def tidy(before: bytes, after: bytes) -> bytes:
    """``after`` with the whitespace each deletion left on its line closed up.

    The engine deletes an argument or parameter with one comma and leaves the
    spaces around the gap: ``f( b)``, ``f(a,  c)``, ``f(a, )``. ``before`` is
    the text the engine was given, and only gaps where ``after`` lost bytes
    are touched, so the customer's own spacing elsewhere stays. A gap that
    meets a line break (a multi-line list) is left alone: the engine already
    leaves those clean, and indentation is not this function's to change.
    """
    out = after
    gaps = _deletion_gaps(before, after)
    for gap in sorted(set(gaps), reverse=True):
        left = gap
        while left > 0 and out[left - 1] in _SPACE:
            left -= 1
        right = gap
        while right < len(out) and out[right] in _SPACE:
            right += 1
        if left == right:
            continue
        prev = out[left - 1 : left]
        nxt = out[right : right + 1]
        if prev in (b"", b"\n", b"\r") or nxt in (b"", b"\n", b"\r"):
            continue
        fill = b"" if prev in _OPENERS or nxt in _CLOSERS else b" "
        out = out[:left] + fill + out[right:]
    return out


def _blocks(template: str) -> list[tuple[str, str]]:
    """``(kind, text)`` for each ``# ctor: <kind>`` block, header excluded."""
    marks = list(_BLOCK.finditer(template))
    return [
        (m.group(1), template[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(template)])
        for i, m in enumerate(marks)
    ]


def render_rules(language: str, removals: Sequence[Removal], sentinel: str) -> str:
    """The rules that delete every removal's parameter and every marked argument."""
    for removal in removals:
        for name in (removal.class_name, removal.param_name):
            if IDENT.fullmatch(name) is None:
                raise ValueError(f"cannot spell {name!r} in a rule")
    if IDENT.fullmatch(sentinel) is None:
        raise ValueError(f"cannot spell {sentinel!r} in a rule")
    template = (RULES_DIR / _TEMPLATES[language]).read_text(encoding="utf-8")
    out: list[str] = []
    for kind, block in _blocks(template):
        if kind == "argument":
            out.append(block.replace("__CTOR_SENTINEL__", sentinel))
            continue
        for index, removal in enumerate(removals):
            clone = block.replace("__CTOR_CLASS__", removal.class_name)
            clone = clone.replace("__CTOR_PARAM__", removal.param_name)
            clone = re.sub(
                r'^name = "(.*)"$', rf'name = "\1__{index}"', clone, count=1, flags=re.MULTILINE
            )
            out.append(clone)
    return "\n".join(b.strip() + "\n" for b in out)
