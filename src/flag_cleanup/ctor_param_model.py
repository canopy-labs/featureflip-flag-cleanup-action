"""Shared types and rule rendering for removing an unread constructor parameter.

A removal deletes a constructor parameter and the argument each caller passes
for it, or a TypeScript field (``ts_field_removal``) with the constructor
statement that assigns it, and that constructor's parameter when the
assignment was its last read, or a Dart field (``dart_field_removal``) with
whatever in its constructors sets it. Which callers exist is proven per
language (``ctor_param_ts``, ``ctor_param_csharp``, ``ctor_param_dart``); the
deletion itself is the
engine's, through rules rendered here.
"""

from __future__ import annotations

import bisect
import difflib
import re
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"

#: Names a rendered rule can spell inside a query string without escaping.
IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

SENTINEL_PREFIX = "__ff_drop_arg_"

_TEMPLATES = {
    "ts": "ts_ctor_param.toml",
    "csharp": "csharp_ctor_param.toml",
    "dart": "dart_ctor_param.toml",
}
_BLOCK = re.compile(r"^# ctor: (parameter|classic|argument)\n", re.MULTILINE)

#: What each form of mark writes in place of its span. An argument becomes the
#: sentinel itself. A field becomes a method of that name, because a method
#: has no `;` of its own: the engine deletes a member and the line break before
#: it, but not the `;` after it. A statement becomes the sentinel and a `;`.
#: A Dart constructor parameter becomes the sentinel, a parameter that is a
#: bare name, a Dart initializer-list entry an entry setting it, and a Dart
#: import directive an import with that prefix.
FORMS = {
    "argument": "{}",
    "member": "{}(){{}}",
    "statement": "{};",
    "parameter": "{}",
    "initializer": "{} = 0",
    "import": "import 'dart:core' as {};",
}

#: The same for C#, plus a whole type declaration and a ``using`` directive.
#: A C# method needs a return type, and a bare identifier is not a statement
#: to this grammar (it parses as an error), so a statement becomes a call.
CSHARP_FORMS = {
    "argument": "{}",
    "member": "void {}(){{}}",
    "statement": "{}();",
    "type": "class {}{{}}",
    "using": "using {};",
    "parameter": "object {}",
}


def forms_for(language: str) -> Mapping[str, str]:
    """The :data:`FORMS` table a language's marks are written in."""
    return CSHARP_FORMS if language == "csharp" else FORMS


@dataclass(frozen=True)
class Site:
    """One thing to delete: a byte span in ``path``, and the :data:`FORMS`
    entry it is marked as."""

    path: str
    start: int
    end: int
    form: str = "argument"


@dataclass(frozen=True)
class Removal:
    declaring_path: str
    class_name: str
    #: ``None`` when a field goes and the constructor keeps every parameter.
    param_name: str | None
    #: The unread row's label: ``Svc.client`` (TS) or ``Svc(IClient client)`` (C#).
    label: str
    #: The parameter's position among the constructor's parameters, 0-based.
    index: int
    #: Every caller's argument, and for a field, the field and its assignment.
    sites: tuple[Site, ...]
    #: Exported (TS) or ``public`` (C#): code outside the repository can call it.
    exported: bool
    #: The field this removes (``flags``, ``#enabled``), if it is one.
    field: str | None = None
    #: ``(path, class)`` for each exported subclass that inherits the
    #: constructor (TS): its signature changes with it.
    heirs: tuple[tuple[str, str], ...] = ()
    #: C#: the parameter is a classic (explicit) constructor's, not the
    #: primary constructor's.
    classic: bool = False
    #: C#: a use of a classic parameter as a whole argument of its own
    #: constructor's ``base(…)``. It can go only with the base type's
    #: matching parameter, so the caller must find each among that removal's
    #: sites.
    forwarded: tuple[Site, ...] = ()


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


def mark(
    data: bytes,
    spans: Iterable[tuple[int, int] | tuple[int, int, str]],
    sentinel: str,
    deleted: Iterable[tuple[int, int]] = (),
    forms: Mapping[str, str] = FORMS,
) -> bytes:
    """``data`` with every ``(start, end)`` byte span replaced by ``sentinel``,
    or ``(start, end, form)`` by that form of it in ``forms`` (see
    :data:`FORMS`, :func:`forms_for`).

    ``deleted`` spans are the other items the engine will delete (the
    parameters). For each argument span and each of those, a line comment that
    trails it on a line of its own is dropped: the engine deletes an item with
    the line break before it, so the comment would otherwise end up describing
    the item on the line above. A marked parameter is treated the same way. A
    member or statement span already holds its own trailing comment.
    """
    ordered = sorted((span[0], span[1], span[2] if len(span) > 2 else "argument") for span in spans)
    for (_, end, _), (start, _, _) in zip(ordered, ordered[1:]):
        if start < end:
            raise ValueError("overlapping spans")
    if ordered and (ordered[0][0] < 0 or ordered[-1][1] > len(data)):
        raise ValueError("span outside the data")
    edits = [(start, end, forms[form].format(sentinel).encode("ascii")) for start, end, form in ordered]
    arguments = [(start, end) for start, end, form in ordered if form in ("argument", "parameter")]
    for start, end in [*arguments, *deleted]:
        comment = _trailing_comment(data, start, end)
        if comment is not None:
            edits.append((*comment, b""))
    edits.sort()
    for (_, end, _), (start, _, _) in zip(edits, edits[1:]):
        if start < end:
            raise ValueError("overlapping spans")
    for start, end, text in reversed(edits):
        data = data[:start] + text + data[end:]
    return data


#: After an item: its separator, then a line comment running to the line break.
_TRAILING_COMMENT = re.compile(rb"(?:[ \t]*,)?([ \t]*//[^\r\n]*)(?:\r?\n|\Z)")


def _trailing_comment(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """The span of the line comment after ``data[start:end]``, when nothing but
    indentation precedes the item on its line and nothing but its separator
    and the comment follows it. On a line shared with another item, or after
    a closing bracket, the comment may describe what stays, so it is kept.
    """
    line_start = data.rfind(b"\n", 0, start) + 1
    if data[line_start:start].strip(b" \t"):
        return None
    match = _TRAILING_COMMENT.match(data, end)
    return match.span(1) if match is not None else None


_SPACE = b" \t"
_OPENERS = b"(<["
_CLOSERS = b")>],;"


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


def tidy(before: bytes, after: bytes, *, named_groups: bool = False) -> bytes:
    """``after`` with the whitespace each deletion left on its line closed up.

    The engine deletes an argument or parameter with one comma and leaves the
    spaces around the gap: ``f( b)``, ``f(a,  c)``, ``f(a, )``, and a Dart
    constructor that lost its whole initializer list ``Svc() ;``. ``before`` is
    the text the engine was given, and only gaps where ``after`` lost bytes
    are touched, so the customer's own spacing elsewhere stays. A gap that
    meets a line break (a multi-line list) is left alone: the engine already
    leaves those clean, and indentation is not this function's to change.
    With ``named_groups`` (Dart), a gap right after ``{`` loses its space as
    after ``(``: the brace opens a group of named parameters.
    """
    openers = _OPENERS + b"{" if named_groups else _OPENERS
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
        # The gap keeps one side's whitespace, the left one where it has any:
        # `f(a,\tb,\tc)` stays tab-separated.
        fill = b"" if prev in openers or nxt in _CLOSERS else out[left:gap] or out[gap:right]
        out = out[:left] + fill + out[right:]
    return out


def tidy_lines(before: bytes, after: bytes) -> bytes:
    """``after`` without the blank line a whole-line deletion left just
    inside a brace or at either end of the file.

    The engine deletes a member, a statement, a type or a ``using`` with the
    line break before it, so the first member of a body leaves ``{`` followed
    by the blank line that used to separate it from the next one, the last
    leaves a blank line before ``}``, and the last type of a file a blank
    line at its end. Only a blank line touching a gap where ``after`` lost
    whole lines is dropped: one the customer wrote elsewhere stays.
    """
    lines = after.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    drop: set[int] = set()
    for gap in _deletion_gaps(before, after):
        at_break = after[gap - 1 : gap] in (b"", b"\n") or after[gap : gap + 1] in (b"\n", b"\r")
        if not at_break:
            continue
        line = bisect.bisect_right(starts, gap) - 1
        for candidate in (line - 1, line, line + 1):
            if not 0 <= candidate < len(lines) or lines[candidate].strip():
                continue
            previous = lines[candidate - 1].rstrip() if candidate > 0 else None
            following = lines[candidate + 1].strip() if candidate + 1 < len(lines) else None
            if (
                previous is None
                or following is None
                or previous.endswith(b"{")
                or following.startswith(b"}")
            ):
                drop.add(candidate)
    return b"".join(line for index, line in enumerate(lines) if index not in drop)


def _blocks(template: str) -> list[tuple[str, str]]:
    """``(kind, text)`` for each ``# ctor: <kind>`` block, header excluded."""
    marks = list(_BLOCK.finditer(template))
    return [
        (m.group(1), template[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(template)])
        for i, m in enumerate(marks)
    ]


def render_rules(language: str, removals: Sequence[Removal], sentinel: str) -> str:
    """The rules that delete every removal's parameter and everything marked."""
    removals = [r for r in removals if r.param_name is not None]
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
        # A `parameter` block deletes a primary (or TypeScript) constructor's
        # parameter, a `classic` block an explicit C# constructor's: the
        # proof that a removal is safe is a different one for each.
        for index, removal in enumerate(removals):
            if removal.classic != (kind == "classic"):
                continue
            clone = block.replace("__CTOR_CLASS__", removal.class_name)
            clone = clone.replace("__CTOR_PARAM__", removal.param_name)
            clone = re.sub(
                r'^name = "(.*)"$', rf'name = "\1__{index}"', clone, count=1, flags=re.MULTILINE
            )
            out.append(clone)
    return "\n".join(b.strip() + "\n" for b in out)
