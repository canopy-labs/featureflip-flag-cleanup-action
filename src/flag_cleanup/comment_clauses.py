"""Cut a removed flag's clause out of a C# comment, and keep the rest.

When a gate is removed, the prose around its code still describes it::

    /// Protects webhook signing secrets stored raw (#3307), once <c>IKeyGate</c>
    /// is on. A scheduled job rather than a startup step so that turning the flag on
    /// takes effect without a redeploy, and so rows an older pod wrote are picked up.

:mod:`flag_cleanup.comment_sentences` cuts a whole sentence that says nothing but
the flag. This pass cuts the one CLAUSE of a sentence that also says something
true (``Protects webhook signing secrets stored raw (#3307).``), for C# ``//`` and
``///`` comments, XML documentation included.

A cut is a WHITELIST of exact clause shapes, never a free-form search. Each one:

* names its subject by the key, a type this run deleted (``<see cref>``, ``<c>``
  or bare), or ``that gate`` / ``that flag`` / ``the flag`` when such a name comes
  EARLIER in the same comment and no other gate or flag type is named in it;
* says the kept state (``is on`` for a flag that served ``true``): nothing is
  cut for ``false``, and nothing for a clause whose state word differs;
* is one of the shapes of :data:`_SHAPES`: a trailing ``, once <flag> is on``,
  ``only once <flag> is on``, the purpose clause ``so that turning the flag on
  takes effect without a redeploy, and so <rest>`` (leaving ``so that <rest>``),
  a conjunct ``once <flag> is on and the <noun> has run`` (leaving ``once the
  <noun> has run``), and ``, gated on <flag>, so flipping that flag takes effect
  without a redeploy``.

Only the text of the comment is edited, and the paragraph an edit lands in is
filled again to the width its longest line had, so no line grows. The XML
documentation must stay well-formed, the file's code tokens must come out
exactly as they went in, and a comment that holds a tool directive is never
touched. Anything outside the shapes stays and is reported as before.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from bisect import bisect_right
from itertools import groupby

from flag_cleanup import stale_comments
from flag_cleanup.comment_sentences import (
    _BLOCK_TAG,
    Reword,
    _groups,
    _lines,
    _one_line,
)
from flag_cleanup.stale_comments import _code_tokens, _line_starts, _parse, is_directive_line

#: A line that is nothing but block tags starts or ends a paragraph.
_TAG_ONLY = re.compile(r"^(?:</?(?:summary|remarks|returns|param|typeparam|exception|value|para|list|listheader|item|term|description|example|code|seealso|inheritdoc|include|permission)\b[^<>]*>\s*)+$")
_TAG_START = re.compile(r"^</?(?:summary|remarks|returns|param|typeparam|exception|value|para|list|listheader|item|term|description|example|code|seealso|inheritdoc|include|permission)\b")
_KEPT = {True: "on|enabled", False: "off|disabled"}


def _subject(key: str, family: frozenset[str]) -> str:
    """The spellings of a name that is certainly this flag: the key (bare, in
    backticks or in ``<c>``) or a deleted type (bare, in ``<c>`` or in a
    ``<see cref>``), optionally preceded by ``the`` and followed by ``gate`` or
    ``flag``."""
    types = "|".join(sorted(map(re.escape, family))) or r"(?!)"
    quoted_key = re.escape(key)
    return (
        r"(?:the\s+)?(?:"
        rf'<see\s+cref="(?:\w+\.)*(?:{types})"\s*/>'
        rf"|<c>(?:{types}|{quoted_key})</c>"
        rf"|`{quoted_key}`"
        rf"|(?<![\w-]){quoted_key}(?![\w-])"
        rf"|(?<![\w.<-])(?:{types})(?![\w>])"
        r")(?:\s+(?:gate|flag|toggle))?"
    )


_ANAPHOR = r"(?:that|the)\s+(?:gate|flag)"
_EFFECT = r"takes\s+effect\s+without\s+a\s+(?:redeploy|restart)"
_TURNING = r"(?:turning|flipping|switching|toggling)"


def _shapes(subject: str, state: str) -> list[tuple[str, str, str, bool]]:
    """``(pattern, replacement, name, anaphoric)`` of each whitelisted clause.
    ``anaphoric`` says the pattern may rest on "that gate" / "the flag", which
    needs a name earlier in the comment."""
    flag = rf"(?:{subject}|{_ANAPHOR})"
    return [
        # `…(#3307), once <gate> is on.`
        (rf",\s+(?:once|when|while|whenever|if)\s+{flag}\s+is\s+(?:{state})(?=\s*[.;])", "", "trailing", True),
        # `Run by the job only once that gate is on.`
        (rf"\s+only\s+(?:once|when|if|while)\s+{flag}\s+is\s+(?:{state})(?=\s*[.;])", "", "only-once", True),
        # `…so that turning the flag on takes effect without a redeploy, and so rows…`
        (
            rf"(?<=so that )(?:{_TURNING}\s+{flag}(?:\s+(?:on|off))?\s+{_EFFECT},\s+and\s+so\s+)",
            "",
            "purpose",
            True,
        ),
        # `once <gate> is on and the backfill has run` -> `once the backfill has run`
        (
            rf"(?P<lead>\b(?:once|after))\s+{flag}\s+is\s+(?:{state})\s+and\s+"
            r"(?=the\s+\w+(?:\s+\w+){0,2}\s+(?:has|have)\s+(?:run|completed|finished)\b)",
            r"\g<lead> ",
            "conjunct",
            True,
        ),
        # `, gated inside Execute on <key>, so flipping that flag takes effect without a redeploy.`
        (
            rf",\s+gated\s+(?:inside\s+\w+\s+)?on\s+{flag},\s+so\s+{_TURNING}\s+{_ANAPHOR}(?:\s+(?:on|off))?\s+{_EFFECT}(?=\s*[.;])",
            "",
            "gated",
            True,
        ),
    ]


def _regions(contents: list[str]) -> list[tuple[int, int]]:
    """Inclusive line ranges of the paragraphs of a comment: runs of text lines
    between blank lines and lines that are only block tags."""
    regions: list[tuple[int, int]] = []
    start: int | None = None
    for index, text in enumerate(contents):
        stripped = text.strip()
        boundary = not stripped or bool(_TAG_ONLY.match(stripped))
        opens = bool(_TAG_START.match(stripped)) and not boundary
        if boundary or (opens and start is not None):
            if start is not None:
                regions.append((start, index - 1))
                start = None
        if boundary:
            continue
        if start is None:
            start = index
    if start is not None:
        regions.append((start, len(contents) - 1))
    return regions


def _well_formed(contents: list[str]) -> bool:
    try:
        ET.fromstring("<r>" + "\n".join(contents) + "</r>")
    except ET.ParseError:
        return False
    return True


def _wrap(text: str, width: int) -> list[str]:
    words = re.sub(r"<[^<>]*>", lambda m: m.group(0).replace(" ", "\x00"), text).split()
    lines: list[str] = []
    current = ""
    for word in (w.replace("\x00", " ") for w in words):
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines


#: Comment words that make a tool, not a reader, the audience: the whole
#: comment is left alone.
_TOOLING = re.compile(
    r"\b(?:resharper|nolint|noqa|eslint|pragma|pylint|stylecop|suppress\w*|nosonar|sonar\w*|codeql|cs\d{4}|ide\d{4}|ca\d{4}|sa\d{4})\b",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"^(?:[-*+\u2022]|\d+[.)])\s")
_LAYOUT = re.compile(r"<(?:code|list|example|para|item|pre|table|br)\b|```", re.IGNORECASE)


def _marker(node) -> bytes:
    """The slash run (and ``!``) that opens a line comment: ``//`` and ``///`` are
    different comments, whatever lines they sit beside."""
    match = re.match(rb"/+!?", node.text)
    return match.group(0) if match else b""


def _plain_prose(lines, a: int, b: int, contents: list[str], text: str) -> bool:
    """Whether the paragraph on lines ``a..b`` may be refilled: every line is
    indented the same, none opens a list item, and no layout tag or fence is
    inside it."""
    if any(ln.lead != lines[a].lead for ln in lines[a : b + 1]):
        return False
    if any(_LIST_ITEM.match(c.strip()) for c in contents[a : b + 1]):
        return False
    return _LAYOUT.search(text) is None


_GATEISH = re.compile(
    r"\b(?:gate|flag|toggle|switch|feature|killswitch|gated|gating)(?:s|es)?\b", re.IGNORECASE
)


def _gateish_words(text: str) -> bool:
    """Whether ``text`` holds a gate-like word, whole or as a camelCase,
    kebab-case or snake_case segment of a longer name."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    spaced = re.sub(r"[-_.]", " ", spaced)
    return _GATEISH.search(spaced) is not None


_KEBAB_QUOTED = re.compile(r"(?:`|<c>)([a-z0-9]+(?:-[a-z0-9]+)+)(?:`|</c>)")


def _other_flag_named(segment: str, key: str, known: frozenset[str]) -> bool:
    """Whether the segment names a flag that is not this one: a kebab-case token
    in backticks or ``<c>`` (the shape of a key), or any other key of the
    project. Type names in ``<c>`` or a cref are not flag keys and do not count."""
    if any(m.group(1) != key for m in _KEBAB_QUOTED.finditer(segment)):
        return True
    return any(
        other != key and re.search(rf"(?<![\w-]){re.escape(other)}(?![\w-])", segment) for other in known
    )


def _only_ours(
    segment: str, at: int, end: int, linked: re.Pattern[str], key: str = "", known: frozenset[str] = frozenset()
) -> bool:
    """The rule for a back-reference ("that gate", "the flag") at ``segment[at:end]``:
    this flag must be named earlier in the segment (the paragraph inside one XML
    element), and EVERY gate-like word of the segment must be the back-reference
    itself or part of this flag's key or a deleted type's name. Any other
    (``the billing gate``, ``the v2 kill switch``, ``outboundWebhooks``) means
    "that gate" could be it."""
    if linked.search(segment[:at]) is None or _other_flag_named(segment, key, known):
        return False
    rest = linked.sub(" ", segment[:at] + " " + segment[end:])
    return not _gateish_words(rest)


def _segment(text: str, at: int) -> tuple[str, int]:
    """The part of ``text`` between the block tags around offset ``at``, and the
    offset in it: an XML element ends at a ``</summary>`` or starts at a
    ``<remarks>`` wherever they sit in a line."""
    start, stop = 0, len(text)
    for tag in _BLOCK_TAG_TEXT.finditer(text):
        if tag.end() <= at:
            start = tag.end()
        elif tag.start() >= at:
            stop = tag.start()
            break
    return text[start:stop], at - start


_BLOCK_TAG_TEXT = re.compile(_BLOCK_TAG.pattern.decode() if isinstance(_BLOCK_TAG.pattern, bytes) else _BLOCK_TAG.pattern)


def _plan(group, data, starts, line_of, names, key, family, shapes, linked, known):
    lines = _lines(group, data, starts, line_of)
    if lines is None or any(ln.kind != "line" for ln in lines):
        return None
    contents = [data[ln.cs : ln.ce].decode("utf-8") for ln in lines]
    if any(
        is_directive_line(data[ln.start : ln.end], c) or _TOOLING.search(c)
        for ln, c in zip(lines, contents)
    ):
        return None
    joined = "\n".join(contents)
    if not names.search(joined.encode("utf-8")):
        return None
    if re.search(r"<(?:code|list|example|pre|table)\b|```", joined, re.IGNORECASE):
        return None  # a comment with a code sample or a list keeps its layout
    regions = _regions(contents)
    texts = [" ".join(c.strip() for c in contents[a : b + 1]) for a, b in regions]
    group_edits: list[tuple[int, int, int, str, str]] = []  # region, start, end, replacement, shape
    for index, text in enumerate(texts):
        for pattern, replacement, name, anaphoric in shapes:
            for match in pattern.finditer(text):
                if linked.search(match.group(0)) is None:
                    # Rests on "that gate" / "the flag": every gate-like word in its
                    # XML element must be this flag's (see `_only_ours`).
                    if not anaphoric:
                        continue
                    segment, at = _segment(text, match.start())
                    if not _only_ours(segment, at, at + len(match.group(0)), linked, key, known):
                        continue
                group_edits.append((index, match.start(), match.end(), match.expand(replacement), name))
    if not group_edits:
        return None
    spans = sorted((i, s, e) for i, s, e, _r, _n in group_edits)
    if any(x[0] == y[0] and x[2] > y[1] for x, y in zip(spans, spans[1:])):
        return None  # overlapping matches: ambiguous
    touched = sorted({g[0] for g in group_edits})
    rebuilt: dict[int, list[str]] = {}
    new_texts = list(texts)
    for index in touched:
        a, b = regions[index]
        if not _plain_prose(lines, a, b, contents, texts[index]):
            return None
        text = texts[index]
        for _i, s0, e0, replacement, _n in sorted((g for g in group_edits if g[0] == index), reverse=True):
            text = text[:s0] + replacement + text[e0:]
        new_texts[index] = re.sub(r"\s+", " ", text).strip()
        width = max(len(c.rstrip()) for c in contents[a : b + 1])
        filled = _wrap(new_texts[index], max(width, 20))
        if not filled:
            return None
        rebuilt[index] = filled
    new_contents = list(contents)
    for index in sorted(rebuilt, reverse=True):
        a, b = regions[index]
        new_contents[a : b + 1] = rebuilt[index]
    if _well_formed(contents) and not _well_formed(new_contents):
        return None
    out = bytearray()
    cursor = 0
    delta = 0
    for index in touched:
        a, b = regions[index]
        for ln in lines[cursor:a]:
            out += data[ln.start : ln.end] + ln.eol
        prefix = data[lines[a].start : lines[a].cs]
        mid = lines[a].eol or lines[b].eol or b"\n"
        for n, content in enumerate(rebuilt[index]):
            out += prefix + content.encode("utf-8") + (mid if n < len(rebuilt[index]) - 1 else lines[b].eol)
        delta += len(rebuilt[index]) - (b - a + 1)
        cursor = b + 1
    for ln in lines[cursor:]:
        out += data[ln.start : ln.end] + ln.eol
    first_row = line_of(lines[0].start)
    rewords = []
    shift = 0
    for index in touched:
        a, b = regions[index]
        rewords.append(
            Reword(
                first_row + a + 1 + shift,
                texts[index],
                new_texts[index],
                tuple(data[ln.start : ln.end].decode("utf-8", "replace").strip() for ln in lines[a : b + 1]),
            )
        )
        shift += len(rebuilt[index]) - (b - a + 1)
    return lines[0].start, lines[-1].end + len(lines[-1].eol), bytes(out), rewords, delta


def cut(
    source: str,
    grammar: str,
    key: str,
    family_names: frozenset[str] | set[str],
    *,
    treatment: bool,
    known_keys: frozenset[str] = frozenset(),
) -> tuple[str, list[Reword]]:
    """``source`` with each whitelisted clause cut out of the C# comments that
    hold it, and what changed. ``source`` itself when nothing qualifies or a
    code token, or the XML of a doc comment, would change."""
    if not treatment:
        return source, []
    bom = source.startswith("﻿")
    data = source[1:].encode("utf-8") if bom else source.encode("utf-8")
    family = frozenset(family_names)
    names = stale_comments.names_pattern(key, family)
    if not names.search(data):
        return source, []
    subject = _subject(key, family)
    shapes = [(re.compile(p, re.IGNORECASE), r, n, a) for p, r, n, a in _shapes(subject, _KEPT[treatment])]
    linked = re.compile(subject, re.IGNORECASE)
    root = _parse(data, grammar)
    starts = _line_starts(data)

    def line_of(offset: int) -> int:
        return bisect_right(starts, offset) - 1

    edits: list[tuple[int, int, bytes, list[Reword], int]] = []
    for whole_group in _groups(root, data, starts, line_of):
        # `//` and `///` are different comments: each run of one marker alone.
        for _marker_bytes, run in groupby(whole_group, key=_marker):
            group = list(run)
            planned = _plan(group, data, starts, line_of, names, key, family, shapes, linked, known_keys)
            if planned is not None:
                edits.append(planned)
    if not edits:
        return source, []
    out_bytes, at = bytearray(), 0
    for start, end, replacement, _rewords, _delta in edits:
        out_bytes += data[at:start] + replacement
        at = end
    out_bytes += data[at:]
    after = bytes(out_bytes)
    try:
        if _code_tokens(_parse(after, grammar)) != _code_tokens(root):
            return source, []
    except UnicodeDecodeError:
        return source, []
    # Line numbers are those of the file as it stands after every cut.
    rewords_all: list[Reword] = []
    carried = 0
    for _s, _e, _r, rewords, delta in edits:
        for r in rewords:
            rewords_all.append(Reword(r.line + carried, r.before, r.after, r.raw))
        carried += delta
    text = after.decode("utf-8")
    return ("﻿" + text if bom else text), rewords_all


__all__ = ["cut"]
