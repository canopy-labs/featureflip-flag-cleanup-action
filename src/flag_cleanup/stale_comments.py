"""Delete the comments a removal leaves describing nothing but what it removed.

Once a flag is gone its key can survive in prose: ``// Gated by the
new-checkout flag.`` above code that no longer branches, or a trailing
``// new-checkout`` on a line the fold kept. Nothing reads a comment, so no
rule matches one, and every such line used to be a hand edit in the pull
request. For a C# gate family the same is true of a comment naming a type the
run deleted.

A comment goes only when it says nothing else. That is decided by WORDS, not
by guessing at meaning: take the comment's text, drop every spelling of a
removed name (the key, a deleted type, ``Type.Member`` on one), drop markup
and punctuation, and what is left must be words from :data:`VOCABULARY` —
articles, prepositions and the handful of words people use to describe a flag
(``gated``, ``behind``, ``enabled``, ``remove once rolled out``). One word
from outside it — a ticket number, a person, another type, ``copyright`` —
keeps the comment, and the pull request reports it instead. That is the
whole safety argument, and it is why a licence header, a ``TODO`` naming
other work or a sentence that also explains something else are never
touched: each of them carries such a word.

The unit is a comment, or one line of one:

* a run of single-line comments on consecutive lines, whatever their markers
  (or one block comment), goes whole when one line names a removed name and
  every line is the vocabulary and the names;
* otherwise a line goes alone when it is about them only AND is a sentence of
  its own: it starts one (the line before it in the comment ends one, there
  is none, or it opens with a capitalised word of the vocabulary) and it ends
  one (or nothing follows it). A line in the middle of a sentence stays, with
  the sentence;
* a doc comment (``///``, ``/**``) is never removed whole, because it documents
  a symbol that survives and some builds require one; only its lines go, and
  only while another line with words remains;
* a comment after code on the same line goes with the spaces before it. One
  with code after it on its line is never touched.

Comments inside strings are not comments to the parser, so they are never
seen. A docstring is a string.

Every rewrite is checked before it is kept: the file is parsed again and its
code tokens (every leaf outside a comment) must be exactly what they were. A
file that fails that is left as it was.

Separately, and only to REPORT: :func:`flag_state_comments` finds comments on
or directly above a changed line that describe a flag's state without naming
the key ("while the flag is off"), and :func:`describes_removed` and
:func:`describes_kept` say whether a comment describes the branch the run
deleted or the one it kept. None of them edits anything.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import syntax, unused
from flag_cleanup.ts_tests import _HEDGES, _removed_state_pattern

#: Words a comment may hold beside the removed names and still be ABOUT them
#: only. Function words, the vocabulary of flags, and the words of a removal
#: note. Deliberately small: a word missing here keeps a comment that could
#: have gone, which costs one reported line; a word wrongly here deletes
#: prose a reader needed.
VOCABULARY = frozenset(
    """
    a an the this that these those it its is are was were be been being
    by on off of for to in at with behind under via from and or but not no
    when while if unless until once whether only now still
    flag flags flagged feature features gate gates gated gating toggle toggles
    toggled switch kill rollout rolled roll out experiment experimental
    enabled enable enables disabled disable disables true false
    check checks checked guard guards guarded control controls controlled
    todo fixme remove removed delete deleted cleanup clean up after fully
    released see
    """.split()
)

#: Comment markers to strip from the front of a line, and the end of a block
#: comment from its back.
_LEADING_MARKER = re.compile(rb"^\s*(?:/{2,}!?|#+!?|/\*+!?|\*+(?!/))")
_TRAILING_MARKER = re.compile(rb"\*+/\s*$")
#: Doc-comment openers: such a comment documents a declaration that survives.
_DOC_MARKERS = (b"///", b"/**", b"//!")
#: An XML or HTML tag: its attribute values are kept as text (a ``cref`` names
#: a symbol), the rest of the tag is markup.
_TAG = re.compile(rb"</?[A-Za-z][^<>]*?/?>")
_ATTRIBUTE_VALUE = re.compile(rb"""=\s*(?:"([^"]*)"|'([^']*)')""")
#: A word in any script: a comment written in Russian or Japanese has words
#: outside the vocabulary too, and must not read as empty.
_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)?")
#: What may close a sentence after its last word: closing brackets, quotes,
#: emphasis and a closing tag.
_CLOSERS = re.compile(rb"(?:</[A-Za-z][^<>]*>|[\s)\]\"'`*_])+$")


@dataclass(frozen=True)
class Removal:
    """One comment, or one line of one, to delete: a byte span of the source."""

    start: int
    end: int
    #: 1-based line of the comment's first removed line.
    line: int
    #: The 0-based lines the span removes whole (none for a comment after
    #: code, which leaves its line).
    lines: tuple[int, ...]
    #: The removed comment on one line, for the pull request.
    text: str


def names_pattern(flag_key: str, type_names: Iterable[str] = ()) -> re.Pattern[bytes]:
    """Every spelling of a removed name: the key as a whole token, and each
    deleted type, qualified or not, with any member read off it."""
    key = re.escape(flag_key.encode())
    alternatives = [rb"(?<![\w.-])" + key + rb"(?![\w-])(?!\.\w)"]
    for name in sorted(type_names):
        alternatives.append(
            rb"(?<![\w.])(?:[A-Za-z_]\w*\.)*" + re.escape(name.encode()) + rb"(?!\w)(?:\.\w+)*"
        )
    return re.compile(b"|".join(alternatives))


def _content(line: bytes) -> bytes:
    """A comment line without its markers."""
    return _TRAILING_MARKER.sub(b"", _LEADING_MARKER.sub(b"", line.rstrip(b"\r\n"))).strip()


def _plain(content: bytes) -> bytes:
    """``content`` with each tag replaced by its attribute values."""

    def values(match: re.Match[bytes]) -> bytes:
        found = [a or b for a, b in _ATTRIBUTE_VALUE.findall(match.group(0))]
        return b" " + b" ".join(found) + b" "

    return _TAG.sub(values, content)


def _words(content: bytes, names: re.Pattern[bytes]) -> list[str]:
    """The words of ``content`` that are not a removed name."""
    rest = names.sub(b" ", _plain(content))
    return _WORD.findall(rest.decode("utf-8", "replace"))


def _vocabulary_only(content: bytes, names: re.Pattern[bytes]) -> bool:
    return all(word.lower() in VOCABULARY for word in _words(content, names))


def about_only(content: bytes, names: re.Pattern[bytes]) -> bool:
    """Whether ``content`` names a removed name and says nothing else."""
    return bool(names.search(_plain(content))) and _vocabulary_only(content, names)


def _neutral(content: bytes, names: re.Pattern[bytes]) -> bool:
    """Whether ``content`` holds no word and no name: a blank comment line,
    a lone ``*``, a ``<summary>`` tag."""
    return not _words(content, names) and not names.search(_plain(content))


#: Capitalised, these open a sentence about a flag. Function words (``The``,
#: ``It``, ``Only``) are left out: one can be capitalised in the middle of a
#: wrapped sentence too.
_OPENERS = frozenset(
    "flag feature gate gated toggle toggled behind enabled disabled controlled "
    "guarded checked todo fixme remove see".split()
)


def _opens_sentence(content: bytes, names: re.Pattern[bytes]) -> bool:
    """Whether ``content`` starts with a capitalised flag word (``Gated``,
    ``Behind``, ``TODO``): a sentence opening, which a line in the middle of
    one does not have. A removed type name is capitalised too, and proves
    nothing."""
    words = _WORD.findall(_plain(content).decode("utf-8", "replace"))
    if not words or names.match(_plain(content).lstrip(b" `*_\"'(")):
        return False
    return words[0][0].isupper() and words[0].lower() in _OPENERS


def _balanced_tags(content: bytes) -> bool:
    """Whether every tag ``content`` opens it also closes: a doc-comment line
    holding half of ``<summary>…</summary>`` cannot go alone."""
    opened = len(re.findall(rb"<[A-Za-z][^<>]*?(?<!/)>", content))
    closed = len(re.findall(rb"</[A-Za-z][^<>]*>", content))
    return opened == closed


def _ends_sentence(content: bytes, colon: bool = False) -> bool:
    body = _CLOSERS.sub(b"", content)
    return body.endswith((b".", b"!", b"?") + ((b":",) if colon else ()))


def _comments(root: Node) -> list[Node]:
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            found.append(node)
            continue
        stack.extend(reversed(node.children))
    return sorted(found, key=lambda n: n.start_byte)


def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    at = data.find(b"\n")
    while at != -1:
        starts.append(at + 1)
        at = data.find(b"\n", at + 1)
    return starts


def plan(data: bytes, root: Node, names: re.Pattern[bytes]) -> list[Removal]:
    """The removals for ``data`` (parsed as ``root``), in source order."""
    starts = _line_starts(data)
    ends = [*(s for s in starts[1:]), len(data)]

    def line_of(offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo

    removals: list[Removal] = []
    groups: list[list[Node]] = []
    for node in _comments(root):
        first, last = line_of(node.start_byte), line_of(node.end_byte)
        before = data[starts[first] : node.start_byte]
        after = data[node.end_byte : ends[last]]
        if after.strip():
            continue
        if before.strip():
            # After code on its line: a single-line comment about the names
            # only goes, with the spaces in front of it.
            if first != last or node.text.startswith(_DOC_MARKERS):
                continue
            if about_only(_content(node.text), names):
                # A line comment's node can hold the `\r` of a CRLF line.
                end = node.end_byte - (1 if data[node.end_byte - 1 : node.end_byte] == b"\r" else 0)
                start = node.start_byte
                while start > starts[first] and data[start - 1 : start] in (b" ", b"\t"):
                    start -= 1
                removals.append(
                    Removal(start, end, first + 1, (), node.text.decode("utf-8", "replace").strip())
                )
            continue
        # A run of single-line comments on consecutive lines is one comment,
        # whatever their markers: a `//` line between two `///` lines does not
        # end the doc comment for this purpose, and treating the run as one
        # only ever makes a removal harder.
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

    for group in groups:
        removals += _plan_group(group, data, starts, ends, line_of, names)
    return sorted(removals, key=lambda r: r.start)


def _plan_group(group, data, starts, ends, line_of, names) -> list[Removal]:
    first = line_of(group[0].start_byte)
    last = line_of(group[-1].end_byte)
    rows = list(range(first, last + 1))
    contents = [_content(data[starts[row] : ends[row]]) for row in rows]
    mentions = [i for i, c in enumerate(contents) if names.search(_plain(c))]
    if not mentions:
        return []
    doc = any(node.text.lstrip().startswith(_DOC_MARKERS) for node in group)
    worded = [i for i, c in enumerate(contents) if not _neutral(c, names)]
    # Whole: every line is the vocabulary and the names, and one names them
    # (`// Gated by the X flag.` above `// Remove once rolled out.`).
    if all(_vocabulary_only(contents[i], names) for i in worded):
        if doc:
            return []
        return [_whole_lines(rows, data, starts, ends)]
    # Line by line. A block comment's first and last lines carry its
    # delimiters, so only its inner lines can go.
    block = len(group) == 1 and first != last
    removals: list[Removal] = []
    for i in mentions:
        if block and i in (0, len(rows) - 1):
            continue
        if not about_only(contents[i], names) or not _balanced_tags(contents[i]):
            continue
        before = next((contents[j] for j in range(i - 1, -1, -1) if not _neutral(contents[j], names)), None)
        after = next((contents[j] for j in range(i + 1, len(rows)) if not _neutral(contents[j], names)), None)
        neighbour_above = contents[i - 1] if i > 0 else None
        starts_sentence = (
            before is None
            or _ends_sentence(before)
            # A header (`Rollout plan:`) keeps a line under it while it has
            # another: removing its only one would leave it heading nothing.
            or (_ends_sentence(before, colon=True) and after is not None)
            or (neighbour_above is not None and _neutral(neighbour_above, names))
            or _opens_sentence(contents[i], names)
        )
        ends_sentence = _ends_sentence(contents[i]) or after is None
        if starts_sentence and ends_sentence:
            removals.append(_whole_lines([rows[i]], data, starts, ends))
    return removals


def _whole_lines(rows: list[int], data: bytes, starts: list[int], ends: list[int]) -> Removal:
    text = " ".join(data[starts[r] : ends[r]].decode("utf-8", "replace").strip() for r in rows)
    return Removal(starts[rows[0]], ends[rows[-1]], rows[0] + 1, tuple(rows), text)


def _blank(line: bytes) -> bool:
    return not line.strip()


def _tidy(data: bytes, removals: list[Removal]) -> list[tuple[int, int]]:
    """The byte spans to cut: each removal, with the blank line it would
    leave doubled (or stranded under an opening brace or above a closing one)."""
    starts = _line_starts(data)
    ends = [*(s for s in starts[1:]), len(data)]
    lines = [data[s:e] for s, e in zip(starts, ends)]
    gone = {row for r in removals for row in r.lines}
    spans = [(r.start, r.end) for r in removals if not r.lines]
    row = 0
    while row < len(lines):
        if row not in gone:
            row += 1
            continue
        top = row
        while row in gone:
            row += 1
        above = top - 1
        below = row
        has_above = above >= 0
        has_below = below < len(lines) and lines[below] != b""
        cut_start, cut_end = starts[top], ends[row - 1]
        if has_below and _blank(lines[below]):
            opens = not has_above or _blank(lines[above]) or lines[above].rstrip().endswith((b"{", b"(", b"["))
            if opens:
                cut_end = ends[below]
                row += 1
        elif has_above and _blank(lines[above]) and (
            not has_below or lines[below].lstrip().startswith((b"}", b")", b"]"))
        ):
            cut_start = starts[above]
        spans.append((cut_start, cut_end))
    return sorted(spans)


def _code_tokens(root: Node) -> list[tuple[str, bytes]]:
    tokens: list[tuple[str, bytes]] = []
    # The root is never a token: a file that was only comments parses to a
    # root with no children once they go.
    stack = list(reversed(root.children))
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            continue
        if node.child_count == 0:
            tokens.append((node.type, node.text))
            continue
        stack.extend(reversed(node.children))
    return tokens


def _parse(data: bytes, grammar: str) -> Node:
    text = data.decode("utf-8")
    if grammar in ("ts", "tsx", "js"):
        return unused._parse(text, grammar)
    return syntax._parse(text, grammar)


def rewrite(source: str, grammar: str, names: re.Pattern[bytes]) -> tuple[str, list[Removal]]:
    """``source`` without the comments about the removed names only, and what
    went. ``source`` itself, and nothing, when there is nothing to remove or
    the result would change a code token."""
    bom = source.startswith("\ufeff")
    data = source[1:].encode("utf-8") if bom else source.encode("utf-8")
    if not names.search(data):
        return source, []
    root = _parse(data, grammar)
    removals = plan(data, root, names)
    if not removals:
        return source, []
    out = bytearray()
    at = 0
    for start, end in _tidy(data, removals):
        if start < at:
            start = at
        out += data[at:start]
        at = max(at, end)
    out += data[at:]
    after = bytes(out)
    if _code_tokens(_parse(after, grammar)) != _code_tokens(root):
        return source, []
    text = after.decode("utf-8")
    return ("\ufeff" + text if bom else text), removals


def follow(row_text: str, source: str, removals: list[Removal]) -> int | None:
    """Where a prose row about this file now points, once ``removals`` went:
    ``None`` when its comment was removed, its new line when the text is on
    exactly one line, ``0`` when that cannot be told (the row is kept as it
    was). ``row_text`` is the line it names, or ``<tag> is now <text>`` for a
    rewritten doc reference, which is found by its new text."""
    needle = row_text.split(" is now ", 1)[1] if " is now " in row_text else row_text
    exact = " is now " not in row_text
    lines = source.split("\n")
    hits = [
        i for i, line in enumerate(lines)
        if (line.strip() == needle if exact else needle in line)
    ]
    if not hits and any(needle in r.text or (r.text and r.text in needle) for r in removals):
        return None
    return hits[0] + 1 if len(hits) == 1 else 0


# --- Comments that describe the flag's state without naming it ------------
#
# Report only: nothing below edits a byte.

#: The fixed phrases that describe a flag's state in prose: "the flag is
#: off", "flag is enabled", "while the flag …", "when the checkout flag …".
#: Deliberately narrow and anchored on the word ``flag`` (or ``toggle``): a
#: comment matching one is only REPORTED, but a report that fires on every
#: "when the user is enabled" would bury the ones that matter.
FLAG_STATE = re.compile(
    r"\b(?:flag|toggle)s?\s+(?:is|are|was|were)\s+(?:(?:turned|switched)\s+)?"
    r"(?:on|off|enabled|disabled)\b"
    r"|\b(?:while|when|whenever|if|unless)\s+the\s+"
    r"(?:[^\s]+\s+){0,2}?(?:flag|toggle)s?\b",
    re.IGNORECASE,
)

#: The comment kinds a run reports, as ``bindings`` rows.
REMOVED_BRANCH_KIND = "removed-branch comment"
KEPT_BRANCH_KIND = "kept-branch comment"
FLAG_STATE_KIND = "flag-state comment"


@dataclass(frozen=True)
class StateComment:
    """A comment near the run's changes that describes the flag's state."""

    #: 1-based line holding the start of the phrase.
    line: int
    #: That line's comment text.
    text: str
    #: Whether the comment describes the branch the run deleted, which makes
    #: it false rather than just stale.
    removed: bool
    #: Whether it describes the branch the run kept: that state is now the
    #: only one, so a qualifier like "only runs while" no longer holds.
    kept: bool = False


def describes_removed(text: str, flag_key: str, treatment: bool) -> bool:
    """Whether ``text`` describes the flag in the state whose branch a removal
    serving ``treatment`` deletes ("the flag is off" when ``true`` is served).

    The same words, and the same hedges ("even when the flag is off" holds in
    both states), that decide which tests are about the removed branch.
    """
    untreated = "false" if treatment else "true"
    return bool(_removed_state_pattern(flag_key, untreated).search(text)) and not _HEDGES.search(text)


def describes_kept(text: str, flag_key: str, treatment: bool) -> bool:
    """Whether ``text`` describes the flag in the state a removal serving
    ``treatment`` keeps ("the flag is on" when ``true`` is served), by the
    same words and hedges as :func:`describes_removed`."""
    treated = "true" if treatment else "false"
    return bool(_removed_state_pattern(flag_key, treated).search(text)) and not _HEDGES.search(text)


def _changed_rows(before: str, after: str) -> tuple[set[int], set[int]]:
    """The 0-based lines of ``after`` the run wrote, and the lines of
    ``after`` that lines the run deleted sat directly above."""
    matcher = difflib.SequenceMatcher(
        None, before.split("\n"), after.split("\n"), autojunk=False
    )
    written: set[int] = set()
    below_deletion: set[int] = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if j1 == j2:
            below_deletion.add(j1)
        written.update(range(j1, j2))
    return written, below_deletion


#: Where a sentence ends inside a comment's joined text.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _sentence(text: str, at: int) -> str:
    """The sentence of ``text`` holding offset ``at``."""
    start = max((m.end() for m in _SENTENCE_END.finditer(text, 0, at)), default=0)
    end = _SENTENCE_END.search(text, at)
    return text[start : end.end() if end else len(text)]


def flag_state_comments(
    before: str, after: str, grammar: str, flag_key: str, treatment: bool
) -> list[StateComment]:
    """The comments of ``after`` that sit on, or directly above, a line the
    run changed and describe a flag's state without naming ``flag_key``.

    Such a comment sat beside the read the run folded, so "while the flag is
    off" is likely about THIS flag even though it never says which. That is a
    guess from the wording, which is why each is only reported: a comment
    line naming the key is decided by :func:`rewrite` and the kept-comment
    report instead, and is left out here, though another line of the same
    comment is not. A run of adjacent comments is one comment, so a phrase
    wrapped onto its next line is still found.
    """
    if before == after or not FLAG_STATE.search(after):
        return []
    data = after.encode("utf-8")
    try:
        root = _parse(data, grammar)
    except Exception:  # noqa: BLE001 - report only; an unparsable file reports nothing
        return []
    names = names_pattern(flag_key)
    written, below_deletion = _changed_rows(before, after)
    starts = _line_starts(data)
    ends = [*(s for s in starts[1:]), len(data)]

    def row_of(offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo

    # Group adjacent own-line comments, as :func:`plan` does; a comment after
    # code is a group of its own.
    groups: list[list[Node]] = []
    for node in _comments(root):
        first = row_of(node.start_byte)
        own_line = not data[starts[first] : node.start_byte].strip()
        previous = groups[-1][-1] if groups else None
        if (
            previous is not None
            and own_line
            and row_of(previous.end_byte) + 1 == first
            and not data[previous.end_byte : node.start_byte].strip()
            and not data[starts[row_of(previous.start_byte)] : previous.start_byte].strip()
        ):
            groups[-1].append(node)
        else:
            groups.append([node])

    found: list[StateComment] = []
    for group in groups:
        first, last = row_of(group[0].start_byte), row_of(group[-1].end_byte)
        rows = range(first, last + 1)
        near = any(r in written for r in rows) or last + 1 in written or last + 1 in below_deletion
        if not near:
            continue
        # Each row's comment text: from the comment's start on its first row.
        texts: list[str] = []
        for row in rows:
            begin = max(starts[row], group[0].start_byte) if row == first else starts[row]
            texts.append(data[begin : ends[row]].decode("utf-8", "replace").strip())
        contents = [_content(t.encode("utf-8")).decode("utf-8", "replace") for t in texts]
        joined = ""
        offsets: list[int] = []
        for content in contents:
            offsets.append(len(joined))
            joined += content + " "
        # The first phrase on a line that does not name the key: a line naming
        # it is the kept-comment report's, but the group's other lines are not.
        hit = next(
            (
                (row, match)
                for match in FLAG_STATE.finditer(joined)
                for row in [max(i for i, offset in enumerate(offsets) if offset <= match.start())]
                if not names.search(texts[row].encode("utf-8"))
            ),
            None,
        )
        if hit is None:
            continue
        at, match = hit
        # Which branch it describes is read off its own sentence, so a hedge
        # in another sentence of the same comment ("whether any key exists")
        # does not cancel it.
        sentence = _sentence(joined, match.start())
        removed = describes_removed(sentence, flag_key, treatment)
        found.append(
            StateComment(
                first + at + 1,
                texts[at],
                removed,
                not removed and describes_kept(sentence, flag_key, treatment),
            )
        )
    return found
