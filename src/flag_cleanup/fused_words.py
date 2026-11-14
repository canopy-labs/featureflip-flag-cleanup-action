"""Refuse a rewrite that wrote a name the source never had.

A rewrite that replaces punctuation with a word can land the word directly
against another one: ``return(<read>) ? 1 : 2`` folded to ``return1;``,
``throw(<read>) || x`` to ``throwtrue``, Ruby's ``x if(<read>) && a?`` to
``x ifa?``, Python's ``return(<read>) or ñ`` to ``returnñ``. The fused text is
an ordinary name, so the file still parses, and no parse-based check can tell
it from code the customer wrote.

It is caught by construction, from how a join happens rather than from what
it spells. A name in the rewritten file (a leaf of an identifier kind, as the
grammar decides, whatever its characters) is refused when it does not line
up with the same token of the file before the rewrite (a surviving token) and
it splits, end to end, into two or more PIECES: tokens of the run's source (any file of the run, named or not,
keywords and punctuation included; strings and comments aside), literal
words, and the words of this tool's ``replace`` templates. ``returnñ`` is
``return`` + ``ñ``, ``ifa?`` is ``if`` + ``a?``, ``ok?`` is ``ok`` + ``?``.

Why that lets no join through. A join is two adjacent pieces of text read as
one token. The piece outside the rewritten node is a token of this file's
source, left where it was. The piece the rewrite wrote is the edge of what it
wrote, and the engine writes only text captured from the source (whole tokens
at its edges; an inlined helper's body is another file of the run) and the
words of its rule templates. So both sides of every join are pieces, and
every joined name splits into them: the split is a property every join has,
and asking for it only spares names that are no join. Only a surviving
token of this file excuses a join: a same-spelled name in another file of
the run, in a string, or only in code the rewrite deleted, is no evidence
that this file meant it (``def ifready?`` elsewhere, or a deleted
``ifready = 1``, does not make ``x ifready`` a call the customer wrote).
Widening the pieces to the whole run only adds refusals.

A join whose seam is where a read of the flag was is refused before any
excuse is asked: every fusion is made by deleting the text between two
tokens, and for a fold that text holds the read, so a name ``A`` + ``B``
where the original has ``A``, then text holding the flag's key or a call of
one of the run's accessors, then ``B``,
is a join even when a same-text token elsewhere would line up with it, if
the smallest node of the original holding that text and ``B`` starts where
the text does (or the smallest holding ``A`` and the text ends where it
does): the node a fold replaces when it writes its edge against the
neighbour. What reads the flag is its key and aliases, the key constants
the run resolved for it, its keyless wrappers, the run's accessors and the
SDK read methods the language's seed rules match; more markers only add
refusals here. The check is not tied to the join's position: a seam
elsewhere in the file refuses a same-spelled name the customer wrote (a
known gap, rare). The
residual: a read first inlined through a variable leaves the variable, not
the read, in the deleted text. The transform engine's own ``word_separated``
guard, which withholds a fold that would join two words, is the primary
defence; this gate is the second.

The argument's scope is what the engine and this tool's rule files write. A
pass of this tool that computed a name of its own (an import alias such as
``_keys``, a renamed test, ``[Fact]``) and wrote it against a word would fall
outside it; none does, since those passes write whole statements, and their
names, being no join, are never refused. Words the engine's built-in rules
write are not read from its rule files (PHP's ``(bool)``); today they are
always written inside punctuation. Never asked about: ``_``, Go's blank
identifier (and no piece, else ``_`` + ``keys``), and the literal words,
which some grammars read as a name in some positions. A template (ERB, Blade)
is asked through the code it compiles to.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from flag_cleanup import syntax
from flag_cleanup.ts_syntax import _parse as _ts_parse
from flag_cleanup.ts_syntax import _walk as _ts_walk

_TS_GRAMMARS = frozenset({"ts", "tsx", "js"})

#: Leaf kinds that hold a name without "identifier" in their kind.
_NAME_KINDS = frozenset({"constant", "name"})

#: Never a joined word: the blank identifier and the literal words.
_NEVER_FUSED = frozenset({"_", "true", "false", "True", "False", "null", "nil", "None", "undefined"})

_RULES_DIR = Path(__file__).parent / "rules"
_CAPTURE = re.compile(r"@[A-Za-z_][A-Za-z0-9_.]*")
_TEMPLATE_WORD = re.compile(r"[^\W\d]\w*")


@lru_cache(maxsize=1)
def _rule_writes() -> frozenset[str]:
    """Every word of every ``replace`` template in this tool's rule files,
    captures aside: the names a rule can write."""
    words: set[str] = set()
    for path in sorted(_RULES_DIR.glob("*.toml")):
        try:
            rules = tomllib.loads(path.read_text(encoding="utf-8")).get("rules", [])
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for rule in rules:
            replace = rule.get("replace")
            if isinstance(replace, str):
                words.update(_TEMPLATE_WORD.findall(_CAPTURE.sub(" ", replace)))
    return frozenset(words)


def _is_name(node) -> bool:
    if node.child_count or not node.is_named:
        return False
    return "identifier" in node.type or node.type in _NAME_KINDS


def _is_string_content(node) -> bool:
    return not node.child_count and any(k in node.type for k in ("string", "content", "fragment"))


def _view(source: str, grammar: str) -> tuple[str, str]:
    view = syntax.host_code_view(source, grammar)
    return view if view is not None else (source, grammar)


def _tree(source: str, grammar: str):
    if grammar in _TS_GRAMMARS:
        return _ts_walk(_ts_parse(source, grammar))
    if grammar in syntax._PROFILES:
        return syntax._walk(syntax._parse(source, grammar))
    return ()


def names(source: str, grammar: str) -> frozenset[str]:
    """Every name token ``source`` spells, as ``grammar`` parses it."""
    source, grammar = _view(source, grammar)
    return frozenset(
        node.text.decode("utf-8", "replace") for node in _tree(source, grammar) if _is_name(node)
    )


def _leaves(source: str, grammar: str) -> list[tuple[str, bool]]:
    """Every code leaf of ``source`` in order, named or not (keywords and
    punctuation included), strings and comments aside, as ``(text, is a
    name)``."""
    source, grammar = _view(source, grammar)
    found = []
    for node in _tree(source, grammar):
        if node.child_count or "comment" in node.type or _is_string_content(node):
            continue
        text = node.text.decode("utf-8", "replace")
        if text and not text.isspace():
            found.append((text, _is_name(node)))
    return found


def tokens(source: str, grammar: str) -> frozenset[str]:
    """The text of every code leaf of ``source`` (see :func:`_leaves`)."""
    return frozenset(text for text, _ in _leaves(source, grammar))


def _joins(name: str, pieces: frozenset[str]) -> bool:
    """Whether ``name`` is two or more of ``pieces`` end to end."""
    ends = {0: 0}  # position reached -> fewest pieces used to reach it
    for start in range(len(name)):
        if start not in ends:
            continue
        for end in range(start + 1, len(name) + 1):
            if name[start:end] in pieces:
                count = ends[start] + 1
                if end not in ends or count > ends[end]:
                    ends[end] = count
    return ends.get(len(name), 0) >= 2


def _root(source: str, grammar: str):
    if grammar in _TS_GRAMMARS:
        return _ts_parse(source, grammar)
    return syntax._parse(source, grammar)


def _smallest(root, start: int, end: int):
    """The smallest node of ``root`` that spans bytes ``start``..``end``."""
    return root.descendant_for_byte_range(start, max(start, end - 1)) if end > start else None


def _starting(root, at: int, within) -> list:
    """The nodes inside ``within`` that start at byte ``at``, innermost first."""
    node = root.descendant_for_byte_range(at, at)
    found = []
    while node is not None and node.start_byte == at and node.end_byte <= within.end_byte:
        found.append(node)
        node = node.parent
    return found


def _ending(root, at: int, within) -> list:
    """The nodes inside ``within`` that end at byte ``at``, innermost first."""
    node = root.descendant_for_byte_range(max(0, at - 1), max(0, at - 1))
    found = []
    while node is not None and node.end_byte == at and node.start_byte >= within.start_byte:
        found.append(node)
        node = node.parent
    return found


class _Context:
    """Where a fold's node stood, read back from the rewritten file.

    ``after(text, end)``: the rewritten file holds ``text`` at a token's
    start, followed by what followed the node (ending at byte ``end`` of the
    original) on its line, up to the first byte the rewrite changed on that
    line (as ``difflib`` sees the line against what the rewrite made of it).
    A line the rewrite left whole, or changed only before the node, keeps all
    of it, which is what keeps a name the customer wrote apart from a join
    (``renderAccept`` beside ``render(<Provider flags={{ <key> }}><Accept
    …>)`` is not followed by the call's ``;``); a line the rewrite changed
    past the node (a second fold on it) keeps only what it left as it was,
    possibly nothing.
    ``before(text, start)`` is the mirror.
    """

    def __init__(self, code: bytes, written: bytes) -> None:
        self.code = code
        self.written = written
        self._lines = code.splitlines(keepends=True)
        self._written_lines = written.splitlines(keepends=True)
        self._map: dict[int, bytes | None] | None = None

    def _line(self, at: int) -> tuple[int, int]:
        """The index and starting byte of the original's line holding ``at``."""
        start = self.code.rfind(b"\n", 0, at) + 1
        return self.code.count(b"\n", 0, start), start

    def _counterpart(self, index: int) -> bytes | None:
        """What the rewrite made of the original's line ``index``: the same
        line when the rewrite left it whole, else the most alike line of the
        block that replaced it, or ``None`` when it deleted it."""
        if self._map is None:
            self._map = {}
            matcher = difflib.SequenceMatcher(None, self._lines, self._written_lines, autojunk=False)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                for i in range(i1, i2):
                    if tag == "equal":
                        self._map[i] = self._written_lines[j1 + i - i1]
                    elif tag == "replace":
                        self._map[i] = max(
                            self._written_lines[j1:j2],
                            key=lambda w: difflib.SequenceMatcher(None, self._lines[i], w, autojunk=False).ratio(),
                        )
        return self._map.get(index)

    def _occurrences(self, text: bytes, *, start: bool):
        pattern = rb"(?<![A-Za-z0-9_$])" + re.escape(text) if start else re.escape(text) + rb"(?![A-Za-z0-9_$])"
        return re.finditer(pattern, self.written)

    def after(self, text: bytes, end: int) -> bool:
        index, line_start = self._line(end)
        line = self._lines[index] if index < len(self._lines) else b""
        column = end - line_start
        follows = line[column:]
        counterpart = self._counterpart(index)
        follows = follows[: _first_change(line, counterpart or b"", column, forward=True) - column]
        return any(
            self.written[found.end() : found.end() + len(follows)] == follows
            for found in self._occurrences(text, start=True)
        )

    def before(self, text: bytes, start: int) -> bool:
        index, line_start = self._line(start)
        line = self._lines[index] if index < len(self._lines) else b""
        column = start - line_start
        counterpart = self._counterpart(index)
        leads = line[_first_change(line, counterpart or b"", column, forward=False) : column]
        return any(
            self.written[max(0, found.start() - len(leads)) : found.start()] == leads
            for found in self._occurrences(text, start=False)
        )


def _first_change(line: bytes, written_line: bytes, column: int, *, forward: bool) -> int:
    """The first byte of ``line`` from ``column`` on (or, backward, the end of
    the last change before it) that the rewrite changed, as ``difflib`` sees
    ``line`` against ``written_line``."""
    matcher = difflib.SequenceMatcher(None, line, written_line, autojunk=False)
    changes = [(i1, i2) for tag, i1, i2, _, _ in matcher.get_opcodes() if tag != "equal"]
    if forward:
        return min([max(i1, column) for i1, i2 in changes if i2 > column or (i1 >= column)] or [len(line)])
    return max([min(i2, column) for i1, i2 in changes if i1 < column] or [0])


def _seams(
    name: str, original: str, rewritten: str, grammar: str, read_markers: tuple[str, ...]
) -> bool:
    """Whether ``name`` (``A`` + ``B``) is what a fold of a read wrote: the
    original has a token ending ``A``, then a seam, then a token starting
    ``B``, and either

    * the smallest node holding the seam and ``B`` starts where the seam
      does, reads the flag, and the rewritten file has ``A``, then the text of
      a node of it starting at ``B``, then what followed that node on its
      line up to the first byte the rewrite changed there (a fold replaced
      the node with that part of it: ``ready.now``, ``ready(u)``,
      ``ready && go``, or ``ready`` from ``(ready && <read>)``; a second fold
      on the line, ``… && ready && <read>``, leaves nothing to compare); or
    * the mirror: the smallest node holding ``A`` and the seam ends where the
      seam does, reads the flag, and the rewritten file has what came before
      that node on its line (back to the last byte the rewrite changed
      there), the text of a node of it ending at ``A``, then ``B``.

    Tying the check to the text around the node is what keeps a name the
    customer wrote out of it (``renderAccept`` beside ``render(<Provider
    flags={{ <key> }}><Accept``): there no part of the node stands where the
    node stood."""
    if not read_markers:
        return False
    from flag_cleanup.syntax import reads_flag

    source, grammar = _view(original, grammar)
    written = _view(rewritten, grammar)[0].encode("utf-8")
    code = source.encode("utf-8")
    context = _Context(code, written)
    root = None
    encoded_upto = lambda i: len(source[:i].encode("utf-8"))  # noqa: E731
    for cut in range(1, len(name)):
        a_text, b_text = name[:cut].encode("utf-8"), name[cut:].encode("utf-8")
        head, tail = re.escape(name[:cut]), re.escape(name[cut:])
        # `A` and `B` are whole tokens' edges: no name character runs on from
        # `A` into the seam, or from the seam into `B`.
        pattern = re.compile(
            r"(?<![A-Za-z0-9_$])" + head + r"(?![A-Za-z0-9_$])(.{1,2000}?)(?<![A-Za-z0-9_$])" + tail
            + r"(?![A-Za-z0-9_$])",
            re.S,
        )
        # Every place `A` starts, overlapping: the shortest seam from each.
        for start in re.finditer(r"(?<![A-Za-z0-9_$])" + head, source):
            match = pattern.match(source, start.start())
            if match is None:
                continue
            if root is None:
                root = _root(source, grammar)
            a_start, a_end = encoded_upto(match.start()), encoded_upto(match.start(1))
            b_start, b_end = encoded_upto(match.end(1)), encoded_upto(match.end())
            node = _smallest(root, a_end, b_end)
            if node is not None and node.start_byte == a_end and reads_flag(
                code[node.start_byte : node.end_byte].decode("utf-8", "replace"), read_markers
            ):
                if any(context.after(a_text + code[d.start_byte : d.end_byte], node.end_byte) for d in _starting(root, b_start, node)):
                    return True
            node = _smallest(root, a_start, b_start)
            if node is not None and node.end_byte == b_start and reads_flag(
                code[node.start_byte : node.end_byte].decode("utf-8", "replace"), read_markers
            ):
                if any(context.before(code[d.start_byte : d.end_byte] + b_text, node.start_byte) for d in _ending(root, a_end, node)):
                    return True
    return False


def fused_names(
    original: str,
    rewritten: str,
    grammar: str,
    run_tokens: Iterable[str] = (),
    read_markers: tuple[str, ...] = (),
) -> list[str]:
    """The names ``rewritten`` spells that are pieces joined end to end
    (tokens of ``original`` or of ``run_tokens``, the tokens of the run's
    other files, literal words, template words) at a place that is not a
    surviving token of ``original``. Sorted; empty when the texts are the
    same.

    "Surviving": the rewritten file's tokens are aligned with the original's
    (``difflib``), and a name is excused only where it lines up with the same
    token of the original, so a same-spelled name elsewhere, or one only
    deleted code spelled, excuses nothing. The alignment can only narrow the
    excuse: a name it fails to line up is asked about, never let through.
    """
    if original == rewritten:
        return []
    after = _leaves(rewritten, grammar)
    candidates = {
        text for text, is_name in after if is_name and text not in _NEVER_FUSED and text not in _rule_writes()
    }
    if not candidates:
        return []
    before = _leaves(original, grammar)
    # `_` is no piece: no fold writes against it, and the tool writes it
    # itself as a prefix (a private import alias, `_keys`).
    pieces = (frozenset(t for t, _ in before) | frozenset(run_tokens) | _NEVER_FUSED | _rule_writes()) - {"_"}
    joins = {name for name in candidates if _joins(name, pieces)}
    if not joins:
        return []
    # Checked first, and never excused: a join whose seam is where a read of
    # the flag was. A token that merely lines up with a same-text token of the
    # original (one in deleted code, say) cannot vouch for it.
    seamed = {name for name in joins if _seams(name, original, rewritten, grammar, read_markers)}
    matcher = difflib.SequenceMatcher(None, [t for t, _ in before], [t for t, _ in after], autojunk=False)
    survived = {j for block in matcher.get_matching_blocks() for j in range(block.b, block.b + block.size)}
    return sorted(
        seamed
        | {text for j, (text, is_name) in enumerate(after) if is_name and text in joins and j not in survived}
    )
