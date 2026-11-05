"""Delete the sentence of a comment that names a removed flag, and keep the rest.

:mod:`flag_cleanup.stale_comments` deletes a comment that says nothing but
what the run removed. A comment that also says something else used to stay,
listed for a person to reword::

    // Gated on the `new-checkout` flag. Retries twice.

This pass cuts that comment down to what is still true::

    // Retries twice.

It is deliberately narrower than a person would be, because a wrong cut
deletes prose a reader needed and nothing checks prose. A sentence is deleted
only when it names a removed name (the key, or a type the run deleted) and
says nothing else: every word left once the names are taken out is in
:data:`flag_cleanup.stale_comments.VOCABULARY`, the test the whole-comment
deletion uses. A fact, a condition, a warning or another symbol in the
sentence keeps it, whatever its shape, and a sentence naming the key that
cannot go keeps the WHOLE comment as it is, for the pull request to list. Even
then the comment is reworded only when ALL of these hold:

* it is on lines of its own (a comment after code is never touched) and no line
  of it is a tool directive (``eslint-disable``, ``//go:embed``, ``noqa``, …);
* a sentence stays in every unit that held one: a ``<summary>``, a ``<param>``
  or a JSDoc tag is never left empty. Only a plain comment's own paragraph or
  list item may go whole, with the blank line beside it;
* the next sentence that stays does not lean on the cut one (``This``, ``It``,
  ``However``, … even across a paragraph), the one before it does not end with
  ``,``, nor with ``:`` unless the cut sentence has a unit of its own and
  something after it stays;
* where a sentence ends is certain: ``e.g.``, ``i.e.``, decimals, initials and
  ``etc.`` are not ends, and a full stop followed by a lowercase word is
  something this cannot tell, so the comment stays;
* brackets and quotes pair up in the cut text and in what is left.

Sentences end at ``.``, ``!`` or ``?`` followed by whitespace. They never cross
an XML block tag (``<summary>``, ``<param>``, …), a blank comment line, a list
marker or ``word:`` lead-in at the start of a line, a JSDoc ``@tag`` or a line
whose indent differs from the one above. An inline tag (``<c>``, ``<see>``,
``{@link}``) belongs to the sentence around it.

Every rewrite is checked before it is kept, like the comment deletion: the file
is parsed again and its code tokens must be exactly what they were.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import stale_comments
from flag_cleanup.stale_comments import (
    _code_tokens,
    _comments,
    _line_starts,
    _parse,
    _plain,
    _vocabulary_only,
    _words,
    is_directive_line,
)

#: The comment text before and after is shown in the pull request; a longer one
#: is cut so a pasted essay does not fill the body. The window is placed around
#: the first difference (see :func:`preview`), never at the start of the text.
_SHOWN = 240
#: Characters of unchanged text kept in front of the first difference.
_CONTEXT = 60

#: The ``bindings`` kind of a reworded comment; its text is
#: ``"<line>: <json [before, after]>"`` (see :func:`encode`).
REWORDED_KIND = "reworded comment"


@dataclass(frozen=True)
class Reword:
    """One comment cut down to the sentences that still hold."""

    #: 1-based line of the comment's first line in the file as it stands after
    #: every reword.
    line: int
    #: The comment's text on one line, before and after.
    before: str
    after: str
    #: Each original line of the comment, stripped, for
    #: :func:`flag_cleanup.stale_comments.follow`.
    raw: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """The original text, for :func:`flag_cleanup.stale_comments.follow`."""
        return self.before


def encode(line: int, before: str, after: str) -> str:
    """A reword as the text of a ``bindings`` row. JSON, so a comment that
    holds any separator at all still reads back as it was written."""
    return f"{line}: {json.dumps([before, after], ensure_ascii=False)}"


def preview(before: str, after: str) -> tuple[str, str]:
    """``before`` and ``after`` cut to about :data:`_SHOWN` characters around
    the first place they differ, with an ellipsis on each side that was cut.
    Both are windowed at the same offset, so two texts that differ never read
    the same, whatever their length."""
    if len(before) <= _SHOWN and len(after) <= _SHOWN:
        return before, after
    first = 0
    while first < min(len(before), len(after)) and before[first] == after[first]:
        first += 1
    start = max(0, first - _CONTEXT)
    if start:
        # Begin on a word, not in the middle of one.
        space = before.find(" ", start, first)
        if space != -1 and after.find(" ", start, first) == space:
            start = space + 1

    def window(text: str) -> str:
        end = start + _SHOWN
        piece = text[start:end].strip()
        return ("…" if start else "") + piece + ("…" if end < len(text) else "")

    return window(before), window(after)


def decode(text: str) -> tuple[str, str, str]:
    """``(line, before, after)`` of a row written by :func:`encode`."""
    line, _, rest = text.partition(": ")
    try:
        before, after = json.loads(rest)
    except (ValueError, TypeError):
        before, after = rest, ""
    return line, str(before), str(after)


# --- the comment's lines ----------------------------------------------------

_LINE_MARKER = re.compile(rb"[ \t]*(?:/{2,}!?|#+!?)[ \t]*")
_BLOCK_OPEN = re.compile(rb"[ \t]*/\*+!?[ \t]*")
_BLOCK_STAR = re.compile(rb"[ \t]*\*+(?!/)[ \t]*")
_INDENT = re.compile(rb"[ \t]*")
_DOC_OPEN = (b"///", b"//!", b"/**", b"/*!")


@dataclass
class _Line:
    row: int
    #: Absolute byte offsets: the line, its content and (when it ends the
    #: block) where ``*/`` starts. ``end`` excludes the line terminator.
    start: int
    end: int
    cs: int
    ce: int
    eol: bytes
    #: ``line`` for a single-line comment, ``first`` / ``middle`` / ``last`` /
    #: ``only`` for a line of a block comment.
    kind: str
    closer: int | None = None
    #: Spaces between the marker and the first word.
    lead: int = 0


def _lines(group: list[Node], data: bytes, starts: list[int], line_of) -> list[_Line] | None:
    """The lines of a comment group with the span of each line's own words, or
    ``None`` for a group this pass does not read (a mix of markers, a comment
    that is not ``//``, ``#`` or ``/* */``)."""
    first, last = line_of(group[0].start_byte), line_of(group[-1].end_byte)
    block = len(group) == 1 and group[0].text.startswith(b"/*")
    if not block and not all(n.text.startswith((b"//", b"#")) for n in group):
        return None
    if block and not group[0].text.rstrip().endswith(b"*/"):
        return None
    lines: list[_Line] = []
    for row in range(first, last + 1):
        start = starts[row]
        end = starts[row + 1] if row + 1 < len(starts) else len(data)
        eol = b""
        if data[start:end].endswith(b"\n"):
            end -= 1
            eol = b"\n"
            if data[start:end].endswith(b"\r"):
                end -= 1
                eol = b"\r\n"
        text = data[start:end]
        closer = None
        lead = 0
        if not block:
            marker = _LINE_MARKER.match(text)
            if marker is None:
                return None
            kind, cs, ce = "line", start + marker.end(), end
            lead = len(marker.group(0)) - len(marker.group(0).rstrip(b" \t"))
        else:
            only = first == last
            kind = "only" if only else "first" if row == first else "last" if row == last else "middle"
            if kind in ("only", "first"):
                opening = _BLOCK_OPEN.match(text)
                if opening is None:
                    return None
                cs = start + opening.end()
                lead = -1  # the space after `/*` says nothing about the text's indent
            else:
                star = _BLOCK_STAR.match(text)
                cs = start + (star.end() if star else _INDENT.match(text).end())
                lead = (len(star.group(0)) - len(star.group(0).rstrip(b" \t"))) if star else 0
            ce = end
            if kind in ("only", "last"):
                at = text.rfind(b"*/")
                if at == -1:
                    return None
                closer = start + at
                ce = closer
            if cs > ce:
                cs = ce
        while ce > cs and data[ce - 1 : ce] in (b" ", b"\t"):
            ce -= 1
        lines.append(_Line(row, start, end, cs, ce, eol, kind, closer, lead))
    return lines


# --- regions and sentences --------------------------------------------------

#: An XML documentation tag that holds text of its own: what is inside one is
#: a unit, and the tag itself is never part of a sentence. ``code`` and
#: ``example`` are here only so the group is skipped (see :func:`_group_edit`).
_BLOCK_TAG = re.compile(
    rb"</?(?:summary|remarks|returns|param|typeparam|exception|value|para|list|listheader|item|term|description"
    rb"|seealso|inheritdoc|include|permission|code|example)(?![\w-])[^<>]*>"
)
_LIST_ITEM = re.compile(rb"(?:[-*+\xe2\x80\xa2]|\d+[.)]|[A-Za-z][.)])[ \t]+\S")
#: A list marker at the start of an item: group 2 is set for an ORDERED one.
_MARKER = re.compile(rb"(?:([-*+\xe2\x80\xa2])|(\d+[.)]|[A-Za-z][.)]))[ \t]+(?=\S)")
_JSDOC_TAG = re.compile(rb"@\w+")
#: Words that end in a full stop without ending the sentence.
_ABBREVIATIONS = frozenset(
    "e.g i.e eg ie vs cf approx incl viz mr mrs ms dr st no fig etc al resp esp".split()
)
_OPENS_SENTENCE = re.compile(rb"""[A-Z0-9`<(\["'@$_*#{~\-]""")
#: A sentence after a deleted one that opens with one of these leans on it.
#: (Besides the connectors: scope and default fragments, which only make sense
#: next to what they qualify.)
_CONNECTORS = frozenset(
    """
    this that these those it its they them their which however otherwise also and but so then
    thus therefore instead both such there here hence because since meanwhile additionally
    likewise similarly conversely either neither nor or yet still else
    only just off on for by in with without when whenever if unless until once while after before
    during except per to at from as where behind outside inside default defaults always never
    currently now temporarily optionally optional disabled enabled
    """.split()
)
#: A sentence after a deleted one that holds one of these ANYWHERE qualifies
#: or restates the gate ("Retries twice unless rolled back."), so it is not
#: shown to stand alone. Nothing proves the sentences that remain are true
#: without the cut one, so this errs towards leaving the comment for a human.
_GATE_CUES = frozenset(
    """
    unless except until whenever rollout ramp ramped staged beta canary temporarily temporary
    currently legacy rolled experiment experiments experimental
    """.split()
)
#: Phrases with the same job. Bare `when`, `if`, `while`, `once`, `default` are
#: NOT here: "Returns the total when paid." and "Uses the default timeout."
#: stand alone. They count only as the first word (see ``_CONNECTORS``).
_GATE_PHRASES = re.compile(
    r"\bonly (?:for|in|to)\b|\bby default\b|\brolls? out\b|(?<![\w/.-])a/b(?![\w/])"
    r"|\bafter (?:the )?(?:launch|rollout|experiment|release|migration)\b"
    r"|\b(?:remove|delete|drop|clean ?up|retire)\b[^.]*\bafter\b",
    re.IGNORECASE,
)


@dataclass
class _Region:
    start: int
    end: int
    sentences: list[tuple[int, int]]
    ambiguous: bool
    #: The list marker that opens the unit (it is no part of a sentence, and
    #: goes with the item when the whole item does), and whether it numbers.
    marker: tuple[int, int] | None = None
    ordered: bool = False
    #: Indexes of the first and last line of the unit.
    first_line: int = 0
    last_line: int = 0


def _closers(text: bytes, at: int) -> int:
    """The offset after the closing brackets, quotes and tags that follow a
    sentence's last full stop."""
    while at < len(text):
        tag = re.match(rb"</[A-Za-z][^<>]*>", text[at:])
        if tag:
            at += tag.end()
        elif text[at : at + 1] in (b")", b"]", b'"', b"'", b"`", b"*", b"_"):
            at += 1
        else:
            break
    return at


def _split(text: bytes, base: int) -> tuple[list[tuple[int, int]], bool]:
    """The sentences of one region, as spans of the joined text (offset
    ``base``), and whether a full stop in it could not be classified."""
    ends: list[int] = []
    ambiguous = False
    i = 0
    while i < len(text):
        if text[i : i + 1] not in (b".", b"!", b"?"):
            i += 1
            continue
        j = i + 1
        while j < len(text) and text[j : j + 1] in (b".", b"!", b"?"):
            j += 1
        after = _closers(text, j)
        if after >= len(text):
            ends.append(len(text))
            break
        if text[after : after + 1] not in (b" ", b"\t", b"\n"):
            i = j  # `1.5`, `foo.bar`, `e.g.,` : inside a token
            continue
        nxt = after
        while nxt < len(text) and text[nxt : nxt + 1] in (b" ", b"\t", b"\n"):
            nxt += 1
        word = re.search(rb"([\w.]+)$", text[:i])
        token = word.group(1).decode("utf-8", "replace") if word else ""
        lowered = token.lower()
        if text[i : i + 1] == b"." and j == i + 1 and (lowered in _ABBREVIATIONS or lowered.rstrip(".") in _ABBREVIATIONS):
            if lowered == "etc" and _OPENS_SENTENCE.match(text[nxt : nxt + 1]):
                ends.append(after)
            i = j
            continue
        if text[i : i + 1] == b"." and len(token) == 1 and token.isalpha():
            ambiguous = True  # an initial, or `Plan A.`
            i = j
            continue
        if _OPENS_SENTENCE.match(text[nxt : nxt + 1]) or not text[nxt : nxt + 1].isalpha():
            ends.append(after)
        else:
            ambiguous = True  # a full stop and a lowercase word
        i = j
    spans: list[tuple[int, int]] = []
    at = 0
    for end in [*ends, len(text)]:
        piece = text[at:end]
        lead = len(piece) - len(piece.lstrip())
        trimmed = piece.strip()
        if trimmed:
            spans.append((base + at + lead, base + at + lead + len(trimmed)))
        at = max(at, end)
    return spans, ambiguous


_LEAD_IN = re.compile(rb"[\w.-]+:(?:\s|$)")
#: A line that closes a sentence or a unit: ends in a full stop, `!`, `?` or
#: `:` (after brackets and quotes), or in a block tag.
_ENDS_UNIT = re.compile(rb"(?:[.!?:][)\]\"'`*_]*|" + _BLOCK_TAG.pattern + rb")\s*$")


def _regions(
    joined: bytes, contents: list[bytes], line_start: list[int], leads: list[int]
) -> list[_Region]:
    """The units of text a sentence may not leave: the text between XML block
    tags, blank comment lines, list markers, JSDoc tags, a ``word:`` lead-in
    at the start of a line, and a line whose indent differs from the one
    above it (a nested list, a wrapped argument description)."""
    cuts: list[tuple[int, int]] = [(m.start(), m.end()) for m in _BLOCK_TAG.finditer(joined)]
    breaks: set[int] = set()
    unclear = False
    for index, (content, start) in enumerate(zip(contents, line_start)):
        if not content.strip():
            breaks.add(start)
            continue
        change = (
            _LIST_ITEM.match(content) or _JSDOC_TAG.match(content) or _LEAD_IN.match(content)
            or (index > 0 and contents[index - 1].strip() and -1 not in (leads[index], leads[index - 1])
                and leads[index] != leads[index - 1])
        )
        if not change or index == 0:
            continue
        previous = contents[index - 1]
        if not previous.strip() or _ENDS_UNIT.search(previous):
            breaks.add(start)
        else:
            # A list marker, an indent or a `word:` after a line that is
            # still mid-sentence: a wrapped sentence or a new unit, and
            # nothing here can tell which.
            unclear = True
    points = sorted({0, len(joined), *breaks, *(p for c in cuts for p in c)})
    inside = lambda a, b: any(s <= a and b <= e for s, e in cuts)  # noqa: E731
    regions: list[_Region] = []
    for a, b in zip(points, points[1:]):
        if inside(a, b):
            continue
        chunk = joined[a:b]
        trimmed = chunk.strip()
        if not trimmed:
            continue
        lead = len(chunk) - len(chunk.lstrip())
        begin = a + lead
        marker = None
        ordered = False
        found = _MARKER.match(trimmed)
        if found is not None and begin in line_start:
            marker = (begin, begin + found.end())
            ordered = found.group(2) is not None
            body = trimmed[found.end() :]
            spans, ambiguous = _split(body, begin + found.end())
        else:
            spans, ambiguous = _split(trimmed, begin)
        regions.append(
            _Region(
                begin,
                begin + len(trimmed),
                spans,
                ambiguous,
                marker,
                ordered,
                bisect_right(line_start, begin) - 1,
                bisect_right(line_start, begin + len(trimmed) - 1) - 1,
            )
        )
    if unclear:
        for region in regions:
            region.ambiguous = True
    return regions


# --- what a sentence may be cut with ----------------------------------------


#: Vocabulary words that carry meaning of their own, so a sentence holding one
#: is not about the flag alone.
_MEANING = frozenset("no not never still false true check checked checks".split())
_STATES = frozenset("enabled disabled on off".split())
_SYMBOLISH = re.compile(rb"[/\\@#$%&=*+|~^<>]|\w\.\w|::|->")


def _deletable(sentence: bytes, names: re.Pattern[bytes]) -> bool:
    """Whether ``sentence`` names a removed name and says nothing else: every
    word left once the names are taken out is in the vocabulary the whole-
    comment deletion uses. This is the one test that decides a cut, so a
    sentence that holds a fact, a condition, a warning or a symbol that stays
    is never cut, whatever its shape."""
    if not names.search(sentence) or not _vocabulary_only(sentence, names):
        return False
    # Words the whole-comment test lets through that CHANGE what a sentence
    # says: "No check when k is on", "Is false when k is true".
    words = [w.lower() for w in _words(sentence, names)]
    if set(words) & _MEANING:
        return False
    if sum(1 for w in words if w in _STATES) >= 2:
        return False  # "Disabled when k is enabled": a rule, not a label
    # Punctuation the word test throws away but that makes a name: a path, a
    # dotted or `::` name, an arrow, an operator, a tag that is not markup.
    return _SYMBOLISH.search(names.sub(b" ", _plain(sentence))) is None


def _balanced(text: bytes) -> bool:
    """Whether brackets and quotes in ``text`` pair up: a sentence cut out of
    the middle of ``(see below. Gated on k.)`` would leave half of each."""
    return (
        all(text.count(o) == text.count(c) for o, c in ((b"(", b")"), (b"[", b"]"), (b"{", b"}")))
        and text.count(b'"') % 2 == 0
        and text.count(b"`") % 2 == 0
    )


def _leans_on_previous(sentence: bytes) -> bool:
    """Whether ``sentence``, which now follows a cut one, may only make sense
    next to it: it opens with a connector or a scope word, or it carries a
    condition or default of its own."""
    first = re.match(rb"[\W_]*([A-Za-z']+)", sentence)
    if first is not None and first.group(1).decode().lower().rstrip("'") in _CONNECTORS:
        return True
    text = sentence.decode("utf-8", "replace")
    if _GATE_PHRASES.search(text):
        return True
    return any(w.lower() in _GATE_CUES for w in re.findall(r"[A-Za-z']+", text))


# --- the comment ------------------------------------------------------------


def _same_list(regions, lines, region, after, flat) -> bool:
    """Whether the unit being cut and the next unit that stays are both items
    of one list: the only case a lead-in (``Why:``) can lose an item and still
    introduce what follows."""
    if after is None or regions[region_index(regions, region)].marker is None:
        return False
    other = regions[flat[after][2]]
    mine = regions[region_index(regions, region)]
    return (
        other.marker is not None
        and not other.ordered
        and lines[other.first_line].lead == lines[mine.first_line].lead
    )


def region_index(regions, region) -> int:
    return region if isinstance(region, int) else regions.index(region)


def _plan_group(
    group: list[Node], data: bytes, starts: list[int], line_of, names: re.Pattern[bytes]
) -> tuple[int, int, bytes, str, str, int, tuple[str, ...]] | None:
    """``(start, end, new bytes, before, after, rows removed)`` for the span of
    a comment group, or ``None`` when it is not reworded."""
    lines = _lines(group, data, starts, line_of)
    if lines is None:
        return None
    contents = [data[ln.cs : ln.ce] for ln in lines]
    joined = b"\n".join(contents)
    if not names.search(joined):
        return None
    # One directive line anywhere makes the whole comment off limits.
    for ln, content in zip(lines, contents):
        if is_directive_line(data[ln.start : ln.end], content.decode("utf-8", "replace")):
            return None
    if re.search(rb"</?(?:code|example)\b", joined) or re.search(rb"^\s*```", joined, re.MULTILINE):
        return None
    line_start: list[int] = []
    at = 0
    for c in contents:
        line_start.append(at)
        at += len(c) + 1
    regions = _regions(joined, contents, line_start, [ln.lead for ln in lines])
    doc_like = bool(_BLOCK_TAG.search(joined)) or group[0].text.startswith(_DOC_OPEN) or any(
        _JSDOC_TAG.match(c) for c in contents
    )
    # Every sentence, in order, with the region it is in.
    flat: list[tuple[int, int, int]] = [
        (s, e, index) for index, region in enumerate(regions) for s, e in region.sentences
    ]
    hit = [bool(names.search(joined[s:e])) for s, e, _ in flat]
    if not any(hit):
        return None
    go = [h and _deletable(joined[s:e], names) for h, (s, e, _) in zip(hit, flat)]
    if any(h and not g for h, g in zip(hit, go)):
        return None  # a sentence naming the key also says something else
    for index, region in enumerate(regions):
        member = [i for i, (_s, _e, r) in enumerate(flat) if r == index]
        if any(go[i] for i in member):
            if region.ambiguous:
                return None
            if region.ordered:
                return None  # the numbering would skip
            if region.marker is not None:
                nxt = region.last_line + 1
                if (
                    nxt < len(lines)
                    and contents[nxt].strip()
                    and -1 not in (lines[nxt].lead, lines[region.first_line].lead)
                    and lines[nxt].lead > lines[region.first_line].lead
                ):
                    return None  # the item has children, indented under it
            if doc_like and all(go[i] for i in member):
                return None  # a summary, a parameter or a description left empty
    if all(go):
        return None
    for i, (s, e, region) in enumerate(flat):
        if not go[i]:
            continue
        if not _balanced(joined[s:e]):
            return None
        following = flat[i + 1] if i + 1 < len(flat) else None
        if following is not None and not go[i + 1]:
            head = joined[following[0] : following[1]].decode("utf-8", "replace")
            opens_item = following[2] != region and regions[following[2]].marker is not None
            if head[:1].islower() and not opens_item:
                return None  # what stays would now open a comment mid-sentence
        after = next((j for j in range(i + 1, len(flat)) if not go[j]), None)
        before = next((j for j in range(i - 1, -1, -1) if not go[j]), None)
        if after is not None and _leans_on_previous(joined[flat[after][0] : flat[after][1]]):
            return None
        if before is not None:
            tail = joined[flat[before][0] : flat[before][1]].rstrip()
            if tail.endswith(b","):
                return None
            # A lead-in ("Why it is slow:") needs what it introduces: the
            # cut sentence in its own unit, or nothing after it.
            if tail.endswith(b":") and not _same_list(regions, lines, region, after, flat):
                return None
    deleted = []
    for go_, (s, e, _) in zip(go, flat):
        if go_:
            # A closing tag that ends the sentence's line (`</p>`) closes
            # something that stays; it is not part of the sentence.
            while True:
                tag = re.search(rb"\s*</([A-Za-z][\w:.-]*)>\s*$", joined[s:e])
                if tag is None or tag.start() == 0:
                    break
                # Only a tag whose opener is NOT in the cut text: a pair
                # inside the sentence goes together.
                name = re.escape(tag.group(1))
                cut = joined[s:e]
                if len(re.findall(rb"</" + name + rb">", cut)) <= len(
                    re.findall(rb"<" + name + rb"(?:[\s>/])", cut)
                ):
                    break
                e = s + tag.start()
            deleted.append((s, e))
    for index, region in enumerate(regions):
        member = [i for i, (_s, _e, r) in enumerate(flat) if r == index]
        if region.marker is not None and member and all(go[i] for i in member):
            deleted.append(region.marker)  # the marker goes with its item
    survivors = sum(1 for g in go if not g)
    if not survivors:
        return None

    # Per line: the pieces of the deleted sentences on it, widened over one
    # run of spaces so no double space or trailing space is left.
    new_contents: list[bytes] = []
    emptied: list[bool] = []
    for ln, content, base in zip(lines, contents, line_start):
        pieces: list[tuple[int, int]] = []
        for s, e in deleted:
            a, b = max(s, base) - base, min(e, base + len(content)) - base
            if a >= b:
                continue
            if b >= len(content):
                while a > 0 and content[a - 1 : a] in (b" ", b"\t"):
                    a -= 1
            else:
                nb = b
                while nb < len(content) and content[nb : nb + 1] in (b" ", b"\t"):
                    nb += 1
                if re.fullmatch(rb"(?:</[A-Za-z][\w:.-]*>\s*)*", content[nb:]):
                    # Nothing but closing tags follow (`...</summary>`): take
                    # the space in front too, or it is left before the tag.
                    while a > 0 and content[a - 1 : a] in (b" ", b"\t"):
                        a -= 1
                b = nb
            pieces.append((a, b))
        if not pieces:
            new_contents.append(content)
            emptied.append(False)
            continue
        out, cursor = bytearray(), 0
        for a, b in sorted(pieces):
            if a > cursor:
                out += content[cursor:a]
            cursor = max(cursor, b)
        out += content[cursor:]
        new_contents.append(bytes(out))
        emptied.append(not out.strip())

    # Whole lines go, but a block comment keeps its opener and closer.
    removed = [
        gone and ln.kind in ("line", "middle")
        for ln, gone in zip(lines, emptied)
    ]
    # A blank comment line left beside a gap is dropped when it would lead,
    # trail or double.
    blank = [not c.strip() for c in contents]
    changed = True
    while changed:
        changed = False
        for i, is_blank in enumerate(blank):
            if not is_blank or removed[i] or lines[i].kind not in ("line", "middle"):
                continue
            above = next((j for j in range(i - 1, -1, -1) if not removed[j]), None)
            below = next((j for j in range(i + 1, len(lines)) if not removed[j]), None)
            touches = (i > 0 and removed[i - 1]) or (i + 1 < len(lines) and removed[i + 1])
            if touches and (above is None or below is None or blank[above] or blank[below]):
                removed[i] = True
                changed = True
    if all(removed):
        return None

    out = bytearray()
    for i, (ln, content, new, gone) in enumerate(zip(lines, contents, new_contents, removed)):
        if gone:
            continue
        if new == content:
            out += data[ln.start : ln.end] + ln.eol
            continue
        if ln.kind in ("first", "only") or not new.strip():
            head = data[ln.start : ln.cs]
            if not new.strip():
                head = head.rstrip()
        else:
            head = data[ln.start : ln.cs]
        if ln.closer is not None:
            tail = data[ln.closer : ln.end]
            if not new.strip() and ln.kind == "last":
                previous = bytes(out)
                previous = previous[: -(2 if previous.endswith(b"\r\n") else 1)] if previous.endswith(b"\n") else previous
                last_line = previous[previous.rfind(b"\n") + 1 :].strip()
                if (
                    last_line
                    and last_line.strip(b"*") != b""
                    and last_line not in (b"/*", b"/**", b"/*!")
                ):
                    # Join the closer to the text above instead of leaving a
                    # line that reads as an empty comment line.
                    del out[len(previous) :]
                    out += b" " + tail + ln.eol
                    continue
                head = data[ln.start : ln.start + len(_INDENT.match(data[ln.start : ln.end]).group(0))]
                out += head + tail + ln.eol
                continue
            glue = b" " if new.strip() else b""
            out += head + new + glue + tail + ln.eol
        else:
            out += head + new + ln.eol
    if not lines[-1].eol and out.endswith((b"\n",)):
        # The last line of the file had no terminator and went.
        out = out[: -len(b"\r\n" if out.endswith(b"\r\n") else b"\n")]
    start, end = lines[0].start, lines[-1].end + len(lines[-1].eol)
    result = bytes(out)
    kept = [c for c, gone in zip(new_contents, removed) if not gone]
    if names.search(result) or not _balanced(b"\n".join(kept)):
        return None
    raw = tuple(data[ln.start : ln.end].decode("utf-8", "replace").strip() for ln in lines)
    return start, end, result, _one_line(contents), _one_line(kept), sum(removed), raw


def _one_line(contents: list[bytes]) -> str:
    text = " ".join(c.decode("utf-8", "replace").strip() for c in contents if c.strip())
    text = re.sub(r"\s+", " ", text)
    return text


def _groups(root: Node, data: bytes, starts: list[int], line_of) -> list[list[Node]]:
    """The own-line comments of a file, adjacent single-line ones together
    (whatever their markers, as in :func:`stale_comments.plan`)."""
    ends = [*starts[1:], len(data)]
    groups: list[list[Node]] = []
    for node in _comments(root):
        first, last = line_of(node.start_byte), line_of(node.end_byte)
        if data[starts[first] : node.start_byte].strip() or data[node.end_byte : ends[last]].strip():
            continue
        previous = groups[-1][-1] if groups else None
        if (
            previous is not None
            and first == last
            and line_of(previous.start_byte) == line_of(previous.end_byte)
            and line_of(previous.end_byte) + 1 == first
            and not data[previous.end_byte : node.start_byte].strip()
        ):
            groups[-1].append(node)
        else:
            groups.append([node])
    return groups


def reword(source: str, grammar: str, names: re.Pattern[bytes]) -> tuple[str, list[Reword]]:
    """``source`` with the sentences naming a removed name cut out of the
    comments that also say something else, and what changed. ``source`` itself,
    and nothing, when no comment qualifies or the result would change a code
    token."""
    bom = source.startswith("﻿")
    data = source[1:].encode("utf-8") if bom else source.encode("utf-8")
    if not names.search(data):
        return source, []
    root = _parse(data, grammar)
    starts = _line_starts(data)

    def line_of(offset: int) -> int:
        return bisect_right(starts, offset) - 1

    edits: list[tuple[int, int, bytes, str, str, int, tuple[str, ...], int]] = []
    for group in _groups(root, data, starts, line_of):
        planned = _plan_group(group, data, starts, line_of, names)
        if planned is not None:
            edits.append((*planned, line_of(planned[0])))
    if not edits:
        return source, []
    out, at = bytearray(), 0
    for start, end, replacement, *_ in edits:
        out += data[at:start] + replacement
        at = end
    out += data[at:]
    after = bytes(out)
    try:
        if _code_tokens(_parse(after, grammar)) != _code_tokens(root):
            return source, []
    except UnicodeDecodeError:
        return source, []
    rewords: list[Reword] = []
    shift = 0
    for _start, _end, _new, before, now, removed, raw, row in edits:
        rewords.append(Reword(row + 1 - shift, before, now, raw))
        shift += removed
    text = after.decode("utf-8")
    return ("﻿" + text if bom else text), rewords


__all__ = ["Reword", "reword", "stale_comments"]
