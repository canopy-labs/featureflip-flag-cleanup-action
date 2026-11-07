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
    after = _cut(data, root, removals, grammar)
    if after is None:
        return source, []
    text = after.decode("utf-8")
    return ("\ufeff" + text if bom else text), removals


def _cut(data: bytes, root: Node, removals: list[Removal], grammar: str) -> bytes | None:
    """``data`` without ``removals`` (and the blank lines they would double),
    or ``None`` when the result would change a code token."""
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
        return None
    return after


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
    if not hits and any(
        needle in r.text
        or (r.text and r.text in needle)
        # A reword names each original line, so a row for one of them is
        # found even though the comment's joined text is not on any line.
        or any(needle in raw for raw in getattr(r, "raw", ()))
        for r in removals
    ):
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


#: "The flag" in prose, the subject of a comment about the flag's role
#: ("the flag only decides whether…") rather than its state. Reported only,
#: and only near the run's changes, like :data:`FLAG_STATE`.
THE_FLAG = re.compile(r"\bthe\s+(?:feature\s+)?flag\b(?!\s+(?:client|service|provider|sdk)\b)", re.IGNORECASE)

#: Words that vanish from a file when a branch goes without naming anything
#: a comment could be about.
_KEYWORDS = frozenset(
    """
    if else elif elsif when then end do return true false null nil none
    var let const val def fun func function new this self await async
    """.split()
)
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def removed_names(before: str, after: str, root: Node) -> frozenset[str]:
    """Identifiers on the lines the run changed that appear nowhere in the
    code of ``after`` (parsed as ``root``; its comments do not count) and at
    least twice in ``before``: a local bound to the read (``seatCapFlagOn``),
    read and then inlined away. A comment naming one describes what the flag
    decided."""
    matcher = difflib.SequenceMatcher(
        None, before.split("\n"), after.split("\n"), autojunk=False
    )
    lines = before.split("\n")
    changed = {
        name
        for tag, i1, i2, _j1, _j2 in matcher.get_opcodes()
        if tag != "equal"
        for line in lines[i1:i2]
        for name in _IDENTIFIER.findall(line)
    }
    left = {
        name
        for _type, text in _code_tokens(root)
        for name in _IDENTIFIER.findall(text.decode("utf-8", "replace"))
    }
    return frozenset(
        name
        for name in changed - left - _KEYWORDS
        if len(re.findall(rf"\b{re.escape(name)}\b", before)) >= 2
    )


def _only_the_flag(text: str) -> bool:
    """Whether a trigger match is nothing but "the flag" (no state phrase)."""
    return THE_FLAG.fullmatch(text) is not None and FLAG_STATE.search(text) is None


def _trigger(names: frozenset[str], *, the_flag: bool = True) -> re.Pattern[str]:
    """What makes a comment near the run's changes worth reporting: a state
    phrase, "the flag", or a name the run removed."""
    parts = [FLAG_STATE.pattern, *([THE_FLAG.pattern] if the_flag else [])]
    if names:
        parts.append(r"(?-i:\b(?:" + "|".join(sorted(map(re.escape, names))) + r")\b)")
    return re.compile("|".join(f"(?:{part})" for part in parts), re.IGNORECASE)


def _indent(line: bytes) -> int:
    return len(line) - len(line.lstrip(b" \t"))


def _reaches_change(
    data: bytes, starts: list[int], ends: list[int], first: int, last: int, written: set[int], below: set[int]
) -> bool:
    """Whether the code below a comment reaches a line the run changed
    without a blank line and without leaving the comment's block.

    Leaving the block is read off indentation: a line indented less than the
    comment closes the block the comment is in. A comment at the left margin
    is never widened this way: there every top-level declaration below it
    shares its block."""
    indent = _indent(data[starts[first] : ends[first]])
    if indent == 0:
        return False
    row = last + 1
    while row < len(starts):
        line = data[starts[row] : ends[row]]
        if not line.strip() or _indent(line) < indent:
            return False
        if row in written or row in below:
            return True
        row += 1
    return False


#: Nodes that declare a function, method or class, in every grammar: the
#: declarations a doc comment sits above.
_DECLARATIONS = frozenset({
    "function_declaration", "generator_function_declaration", "method_definition",
    "class_declaration", "abstract_class_declaration", "function_definition",
    "method_declaration", "constructor_declaration", "local_function_statement",
    "class_definition", "method", "singleton_method",
})

#: A function value that is a declaration only when a name is bound to it
#: (``const Gate = () => …``).
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})

#: What a declaration can sit inside and still own the comment above it.
_DECLARATION_WRAPPERS = frozenset({
    "export_statement", "variable_declarator", "lexical_declaration",
    "variable_declaration", "decorated_definition",
})


def _enclosing_declaration_rows(
    data: bytes, root: Node, starts: list[int], ends: list[int], rows: set[int]
) -> set[int]:
    """The 0-based first rows of every function, method or class (with what
    exports or binds it) that encloses one of ``rows``: the row a doc comment
    on it ends directly above."""
    found: set[int] = set()
    row_of = _row_finder(starts)
    for row in rows:
        if row >= len(starts):
            continue
        line = data[starts[row] : ends[row]]
        offset = starts[row] + (len(line) - len(line.lstrip()) if line.strip() else 0)
        node = root.descendant_for_byte_range(offset, offset)
        while node is not None:
            if node.type in _DECLARATIONS or node.type in _FUNCTION_VALUES:
                statement, named = node, node.type in _DECLARATIONS
                while statement.parent is not None and statement.parent.type in _DECLARATION_WRAPPERS:
                    statement = statement.parent
                    named = named or statement.type == "variable_declarator"
                if named:
                    found.add(row_of(statement.start_byte))
            node = node.parent
    return found


def _comment_groups(data: bytes, root: Node, row_of) -> list[list[Node]]:
    """Adjacent own-line comments, grouped as :func:`plan` groups them; a
    comment after code is a group of its own."""
    starts = _line_starts(data)
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
    return groups


def _row_finder(starts: list[int]):
    def row_of(offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo

    return row_of


def _group_texts(data: bytes, starts: list[int], ends: list[int], group: list[Node], first: int, last: int):
    """Each row's comment text (from the comment's start on its first row),
    and the rows' contents joined, with each row's offset into the join."""
    texts: list[str] = []
    for row in range(first, last + 1):
        begin = max(starts[row], group[0].start_byte) if row == first else starts[row]
        texts.append(data[begin : ends[row]].decode("utf-8", "replace").strip())
    contents = [_content(t.encode("utf-8")).decode("utf-8", "replace") for t in texts]
    joined = ""
    offsets: list[int] = []
    for content in contents:
        offsets.append(len(joined))
        joined += content + " "
    return texts, joined, offsets


def flag_state_comments(
    before: str,
    after: str,
    grammar: str,
    flag_key: str,
    treatment: bool,
    skip: frozenset[int] = frozenset(),
) -> list[StateComment]:
    """The comments of ``after`` near a line the run changed that describe a
    flag's state, or what it decided, without naming ``flag_key``.

    Near is on the changed line, directly above it, above it in the same
    block with no blank line between (the comment of the statement before a
    folded ``if``), or directly above a function, method or class that holds
    a changed line: its doc comment, which describes what the body did,
    however far down the fold sits. Describes is a state phrase (:data:`FLAG_STATE`), "the
    flag" (:data:`THE_FLAG`), or a local the run removed (see
    :func:`removed_names`).

    Such a comment sat beside the read the run folded, so "while the flag is
    off" is likely about THIS flag even though it never says which. That is a
    guess from the wording, which is why each is only reported: a comment
    line naming the key is decided by :func:`rewrite` and the kept-comment
    report instead, and is left out here, though another line of the same
    comment is not. A run of adjacent comments is one comment, so a phrase
    wrapped onto its next line is still found. A comment holding a 1-based
    line in ``skip`` was reported already, and is left out.
    """
    if before == after:
        return []
    data = after.encode("utf-8")
    try:
        root = _parse(data, grammar)
    except Exception:  # noqa: BLE001 - report only; an unparsable file reports nothing
        return []
    removed = removed_names(before, after, root)
    if grammar == "csharp":
        # A name counts only when it is an identifier of the CODE that went: the
        # words of a deleted comment ("the", "summary") name nothing, and "the
        # flag" alone is a role, not a state.
        in_code = {
            word
            for _type, text in _code_tokens(_parse(before.encode("utf-8"), grammar))
            for word in _IDENTIFIER.findall(text.decode("utf-8", "replace"))
        }
        removed = frozenset(name for name in removed if name in in_code)
    trigger = _trigger(removed)
    if not trigger.search(after):
        return []
    names = names_pattern(flag_key)
    written, below_deletion = _changed_rows(before, after)
    starts = _line_starts(data)
    ends = [*(s for s in starts[1:]), len(data)]
    row_of = _row_finder(starts)

    # The first row of each function, method or class around a changed line:
    # a doc comment directly above one describes the code the run changed,
    # however far down the change sits.
    declarations = _enclosing_declaration_rows(data, root, starts, ends, written | below_deletion)

    found: list[StateComment] = []
    for group in _comment_groups(data, root, row_of):
        first, last = row_of(group[0].start_byte), row_of(group[-1].end_byte)
        rows = range(first, last + 1)
        if any(r + 1 in skip for r in rows):
            continue
        near = any(r in written for r in rows) or last + 1 in written or last + 1 in below_deletion
        own_line = not data[starts[first] : group[0].start_byte].strip()
        documents = own_line and last + 1 in declarations
        if not near and not documents and not (
            own_line and _reaches_change(data, starts, ends, first, last, written, below_deletion)
        ):
            continue
        texts, joined, offsets = _group_texts(data, starts, ends, group, first, last)
        attached = any(r in written for r in rows) or last + 1 in written
        # The first phrase on a line that does not name the key: a line naming
        # it is the kept-comment report's, but the group's other lines are not.
        hits = [
            (row, match)
            for match in trigger.finditer(joined)
            for row in [max(i for i, offset in enumerate(offsets) if offset <= match.start())]
            if not names.search(texts[row].encode("utf-8"))
            # C#: "the flag" alone is a role, so it counts only for a comment
            # attached to a changed line (trailing it, or directly above it).
            and (grammar != "csharp" or attached or not _only_the_flag(match.group(0)))
        ]
        if not hits:
            continue
        at, match = hits[0]
        if documents and not near:
            # A doc comment covers the whole declaration, so the sentence
            # that describes a branch is the one to name, wherever it sits.
            at, match = next(
                (
                    (row, m)
                    for row, m in hits
                    if describes_removed(_sentence(joined, m.start()), flag_key, treatment)
                    or describes_kept(_sentence(joined, m.start()), flag_key, treatment)
                ),
                (at, match),
            )
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


# --- The leading comment of a statement the run deleted outright ----------


@dataclass(frozen=True)
class LeadingComment:
    """A comment directly above a statement or entry the run deleted outright."""

    #: The whole comment as a removal of its lines in ``after``.
    removal: Removal
    #: 1-based lines of ``after`` the comment holds.
    rows: frozenset[int]
    #: Its first line's text, for the pull request.
    text: str
    #: Whether it describes a flag's state (:data:`FLAG_STATE`): such a
    #: comment explained only the statement that went, and goes with it.
    describes_state: bool
    #: Whether what went was the flag's own: its read, a binding of it, or
    #: its entry in a registry. Such a comment has lost its subject, whatever
    #: it says, and goes with it.
    own: bool = False

    @property
    def goes(self) -> bool:
        """Whether the comment is deleted with what it sat above."""
        return self.describes_state or self.own


#: A comment that instructs a tool rather than describing code: never the
#: deleted statement's prose.
_DIRECTIVE = re.compile(
    r"\s*(?:eslint|@ts-|prettier-|istanbul|c8\b|noqa|pylint|type:|nolint|rubocop|swiftlint|#?pragma|#?region"
    r"|#?endregion|nosonar|biome-ignore|tslint:|deno-lint-ignore|deno-fmt-ignore|@flow\b|fmt:|pyright:|mypy:"
    r"|frozen_string_literal|ignore:|ignore_for_file|lint:ignore|swift-format-ignore)",
    re.IGNORECASE,
)
#: More directive SHAPES, for the pass that rewrites a comment's words. Each is
#: the exact spelling a tool reads, never a bare word: ``Licensed seats get
#: it.`` or ``Webpack bundles it lazily.`` is prose, and ``_DIRECTIVE`` above
#: (which decides what a deleted statement's comment may be taken with) stays
#: as it was.
_SHAPED_DIRECTIVE = re.compile(
    r"\s*(?:\+build\b|-\*-|<reference\b|<amd-|@phpstan|@psalm|noinspection\s+(?-i:[A-Z])|jshint\s+[\w-]+"
    r"|spdx-license-identifier:|copyright\s*(?:\(c\)|\u00a9|\d{4})|all rights reserved"
    r"|(?-i:webpack[A-Z])\w*\s*:|licen[sc]e\b[^.\n]*\b(?:header|notice)\b|(?:licensed|released)\s+under\b)",
    re.IGNORECASE,
)
#: A directive written with no space after the marker: Go's ``//go:embed``,
#: ``//go:generate``, ``//nolint:x``, cgo's ``//export Name`` and ``//line``.
#: Any ``//word:`` spelling is one, since prose is written with a space.
_BARE_DIRECTIVE = re.compile(rb"[ \t]*(?:/{2,}|#+)(?:\w[\w.-]*:\S|export\s|line\s|\+build)|[ \t]*#!")


def is_directive_line(raw: bytes, content: str) -> bool:
    """Whether one comment line (``raw``, its text with the marker and
    ``content``, the words after it) is a tool directive. Shared by the
    passes that rewrite a comment's words: one such line makes the whole
    comment off limits, since a directive's neighbours are often its
    arguments or its subject."""
    return bool(_DIRECTIVE.match(content) or _SHAPED_DIRECTIVE.match(content) or _BARE_DIRECTIVE.match(raw))


def _closes(line: bytes) -> bool:
    """Whether ``line`` (stripped of its indent) closes the block it sits in:
    a bracket, or ``end`` as a whole word, not ``endpoint``."""
    return line.startswith((b"}", b")", b"]")) or re.match(rb"end(?![A-Za-z0-9_])", line) is not None

#: The target of a declaration or assignment of one plain name, up to its
#: ``=``: keywords and a type before the name (``const``, ``final bool``,
#: ``var``) or after it (``: boolean``, Go's ``var on bool``), and ``=`` or
#: ``:=``. A member (``this.on``, ``settings.on``) is not one. Go's form is
#: its own alternative, tried first: a type after the name without a ``:``
#: would otherwise read ``const on =`` as a ``const`` of type ``on``.
_TARGET = (
    r"(?:var\s+(?P<go>[A-Za-z_]\w*)\s+[\w.\[\]*]+\s*=(?![=>])"
    r"|(?:[\w$.<>\[\]?,]+\s+)*?(?P<name>\$?[A-Za-z_]\w*)\s*(?::\s*[\w$.<>\[\]?]+\s*)?:?=(?![=>]))"
)
#: What may stand before the read's callee for the read to be the statement's
#: whole value: nothing, or the target, then ``await``/``try`` and a paren.
_BEFORE_READ = re.compile(r"(?:" + _TARGET + r")?\s*(?:await\s+|try[?!]?\s+)?\(?\s*", re.DOTALL)
#: A callee, read backwards from the argument list: a chain of names
#: (``client.boolVariation``, ``$this->client->read``, ``Client::read``).
_CALLEE = re.compile(r"[\w$.?!:<>-]+$")
#: What may follow the read's argument list.
_AFTER_READ = re.compile(r"\s*\)?\s*;?\s*")
#: A declaration of one name and its value.
_DECLARATION = re.compile(_TARGET + r"\s*(?P<value>.+?)\s*;?\s*", re.DOTALL)
#: A value that is already a boolean literal: the engine inlines such a local
#: too, but it was never a read.
_LITERAL = re.compile(r"\(?\s*(?:true|false)\s*\)?", re.IGNORECASE)


def _normalised(text: str) -> str:
    """An entry's text with its whitespace collapsed and its separator dropped."""
    return " ".join(text.split()).strip(",; ")


def leading_comments(
    before: str,
    after: str,
    grammar: str,
    flag_key: str,
    bound: Iterable[str] = (),
    entries: Iterable[str] = (),
) -> list[LeadingComment]:
    """Each own-line comment of ``before`` that sat directly above statements
    or entries the run deleted whole, as it stands in ``after``.

    Directly above is no blank line between. Whole is a run of complete
    statements of ``before`` (a declaration the run inlined, a guard of the
    removed branch), never part of one: a deleted ``if`` header whose body
    stayed is not a deleted statement. A doc comment documents something
    that survives, and a comment naming the key is :func:`rewrite`'s; both
    are left out.

    A statement's comment is found only when nothing else claims it: the
    deleted statements were followed by a blank line, the end of their block
    or the end of the file; otherwise the comment may describe the next
    statement too. It is the flag's own (:attr:`LeadingComment.own`) when
    every deleted statement is the flag's read or a binding of one (see
    :func:`_own_statement`; ``bound`` holds the locals the engine inlined as
    the flag's value): the read is gone, so the comment explained nothing
    that is left.

    An entry is one of ``entries``, the flag's entries the run deleted from
    a literal (``'k': { default: false },``). Entries sit on adjacent lines,
    so its comment is found whatever follows; it is the entry's own when the
    deleted lines are followed by a blank line, a closer, the end of the
    file or the next entry's own comment. Followed by another entry, the
    comment may be about both, and is reported rather than deleted.
    """
    if before == after:
        return []
    old = before.encode("utf-8")
    data = after.encode("utf-8")
    try:
        old_root = _parse(old, grammar)
        _parse(data, grammar)
    except Exception:  # noqa: BLE001 - an unparsable file has no leading comments
        return []
    names = names_pattern(flag_key)
    bound = frozenset(bound)
    read = _key_first(flag_key)
    own_entries = {_normalised(text) for text in entries}
    old_lines = before.split("\n")
    new_lines = after.split("\n")
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    old_starts = _line_starts(old)
    old_ends = [*(s for s in old_starts[1:]), len(old)]
    old_row_of = _row_finder(old_starts)
    starts = _line_starts(data)
    ends = [*(s for s in starts[1:]), len(data)]
    # Read off ``before``: once an entry between two comments goes, the
    # comments are adjacent in ``after`` and would read as one.
    by_last = {}
    for group in _comment_groups(old, old_root, old_row_of):
        first, last = old_row_of(group[0].start_byte), old_row_of(group[-1].end_byte)
        if not old[old_starts[first] : group[0].start_byte].strip():
            by_last[last] = (group, first)
    comment_rows = {first for _group, first in by_last.values()}
    found: list[LeadingComment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "delete" or j1 != j2 or i1 == 0 or j1 == 0:
            continue
        # Only the paragraph directly below the comment: a deletion that runs
        # on past a blank line (a binding and the guard the fold removed)
        # holds a paragraph break, and what follows it is not the comment's.
        i2 = next((i for i in range(i1, i2) if not old_lines[i].strip()), i2)
        if i2 == i1:
            continue
        following = old_lines[i2].lstrip().encode("utf-8") if i2 < len(old_lines) else b""
        closed = not following or _closes(following)
        entry = _normalised("\n".join(old_lines[i1:i2])) in own_entries
        if entry:
            shared = not closed and i2 not in comment_rows
            own = not shared
        else:
            if not closed:
                continue
            covered = _whole_statements(old_root, old, old_starts, i1, i2)
            if covered is None:
                continue
            shared = False
            own = all(_own_statement(c, old, read, bound) for c in covered)
        candidate = by_last.get(i1 - 1)
        if candidate is None:
            continue
        group, first = candidate
        top = j1 - (i1 - first)
        # The comment must stand in ``after`` as it did, line for line.
        if top < 0 or old_lines[first:i1] != new_lines[top:j1]:
            continue
        texts, joined, _offsets = _group_texts(old, old_starts, old_ends, group, first, i1 - 1)
        raw = old[old_starts[first] : old_ends[i1 - 1]]
        if (
            any(t.encode("utf-8").startswith(_DOC_MARKERS) for t in texts)
            or names.search(raw)
            or _DIRECTIVE.match(joined)
        ):
            continue
        found.append(
            LeadingComment(
                Removal(starts[top], ends[j1 - 1], top + 1, tuple(range(top, j1)), " ".join(texts)),
                frozenset(range(top + 1, j1 + 1)),
                texts[0],
                not shared and bool(FLAG_STATE.search(joined)),
                own,
            )
        )
    return found


def _own_statement(node: Node, data: bytes, read: re.Pattern[bytes], bound: frozenset[str]) -> bool:
    """Whether a deleted statement was the flag's read or a binding of one.

    Judged by shape, around the argument list the parse finds:

    * the read itself, a call whose first argument is the key
      (``client.boolVariation('k', false);``), alone or as the whole value of
      a declaration or assignment of one plain name (``const on =
      useFlag('k');``, ``on := await read("k")``);
    * or the declaration of a local in ``bound``, one the engine inlined as
      the flag's value (``var on = _gate.IsEnabled;``), unless its value was
      already a literal.

    So not a statement that holds the read inside something else (an
    assertion, an argument, an array or object, an operand), a member
    (``this.on = …``), an assignment of a bound name, or a compound statement
    (a dead ``if`` assigning it)."""
    text = data[node.start_byte : node.end_byte]
    for arguments in _descendants(node):
        if "argument" not in arguments.type or not read.match(data[arguments.start_byte : arguments.end_byte]):
            continue
        head = data[node.start_byte : arguments.start_byte].decode("utf-8", "replace")
        callee = _CALLEE.search(head)
        if (
            callee is not None
            and _BEFORE_READ.fullmatch(head[: callee.start()])
            and _AFTER_READ.fullmatch(data[arguments.end_byte : node.end_byte].decode("utf-8", "replace"))
        ):
            return True
    if not bound or "declaration" not in node.type:
        return False
    declared = _DECLARATION.fullmatch(text.decode("utf-8", "replace").strip())
    return (
        declared is not None
        and (declared.group("go") or declared.group("name")) in bound
        and not _LITERAL.fullmatch(declared.group("value"))
    )


def _descendants(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _key_first(flag_key: str) -> re.Pattern[bytes]:
    """An argument list whose first argument is ``flag_key``, quoted."""
    return re.compile(rb"\(\s*(['\"`])" + re.escape(flag_key.encode()) + rb"\1\s*[,)]")


def _whole_statements(root: Node, data: bytes, starts: list[int], first: int, end: int) -> list[Node] | None:
    """The statements lines ``first`` to ``end`` (exclusive) of ``data`` hold,
    when they hold exactly one or more complete statements and nothing else."""
    ends = [*(s - 1 for s in starts[1:]), len(data)]
    begin = starts[first] + (len(data[starts[first] : ends[first]]) - len(data[starts[first] : ends[first]].lstrip()))
    finish = ends[end - 1]
    while finish > begin and data[finish - 1 : finish] in (b" ", b"\t", b"\r"):
        finish -= 1
    node = root
    while True:
        inner = next(
            (c for c in node.children if c.start_byte <= begin and c.end_byte >= finish),
            None,
        )
        if inner is None:
            break
        node = inner
    if node.start_byte == begin and node.end_byte == finish:
        covered = [node]
    else:
        covered = [c for c in node.named_children if c.start_byte >= begin and c.end_byte <= finish]
        if not covered or covered[0].start_byte != begin or covered[-1].end_byte != finish:
            return None
    return covered if all(_is_statement(c) for c in covered) else None


def _is_statement(node: Node) -> bool:
    return any(word in node.type for word in ("statement", "declaration", "definition"))


def remove_leading(after: str, grammar: str, comments: list[LeadingComment]) -> str | None:
    """``after`` without ``comments``, or ``None`` when that would change a
    code token."""
    if not comments:
        return after
    data = after.encode("utf-8")
    return (
        cut.decode("utf-8")
        if (cut := _cut(data, _parse(data, grammar), [c.removal for c in comments], grammar)) is not None
        else None
    )
