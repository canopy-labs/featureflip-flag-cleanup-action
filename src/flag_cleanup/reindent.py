"""Restore the indentation a fold moved, in every language at once.

Folding an ``if`` splices its body up one nesting level, and the engine does
not re-indent what it splices. The artefact it leaves is not one shape but
THREE, which is the first thing to know here — a fix derived from any one of
them alone is wrong for the other two:

* **Go** keeps every line after the first at its ORIGINAL column, so the body
  arrives over-indented by exactly one level.
* **Java, TypeScript, C#, Kotlin, Dart and PHP** flatten the block's direct
  children after the first to column ZERO while leaving the lines nested inside
  those at their old column — so the output holds neither the right structure
  nor a uniform offset.
* **Ruby** moves nothing at all: the ``if`` header becomes a blank line and the
  whole body stays exactly where it was.

In the first two the FIRST spliced line is placed correctly, at the column the
``if`` itself occupied — which is the same column the removed header sat at, so
nothing here has to read the output to learn where the body belongs. Ruby moves
not even that line, and reading the header is what covers it too.

Cosmetic in most of these; **blocking in Go**, where ``gofmt -l`` in CI is
close to universal and a pull request that fails it is one the customer has to
fix by hand — on a tool whose pitch is that the cleanup arrives ready to
review (#2694).

Running each language's formatter over the result was the obvious fix and is
the wrong one twice over: the image ships none of the nine toolchains, and a
formatter rewrites code this removal never touched, turning an ugly diff into
an unreviewable one. So nothing here formats anything.

**Indentation is RECOVERED from the input, never invented.** Every line the
splice moved is textually unchanged — only its leading whitespace differs — so
the input still holds the indentation it should have, one level deeper. Read it
back and re-apply the shift, and the body's internal structure comes back
exactly, including the column-zero case where the output no longer contains it.
That is why this can restore a nested ``if`` in Java, where no rule computed
from the output alone could: the information is not in the output.

The one line that has no counterpart to read back is the one the transform
REWROTE — a second read of the same flag inside its own branch — and its own
leading whitespace stands in, but only while it still carries the body's
indentation, which is precisely what proves the engine left it where the input
had it. See :func:`_shift_rewritten_body`; it is Ruby that needs this, because
Ruby moves nothing.

The shift is applied as a PREFIX SUBSTITUTION rather than a column count:

    new indent = <first spliced line's new indent> + <this line's old indent
                 with the first line's old indent stripped off the front>

which is exact whatever the file indents with — tabs, spaces, or the mixture a
continuation line often carries — and needs no notion of a tab's width. The
same prefix is what says where the spliced region ENDS: a line whose old
indentation does not start with the region's own is a line outside the block
that was folded, and it is left alone.

A line's leading whitespace falls into THREE kinds, not two, and the third is
the one that took a second pass to name. A blank line carries no indentation
worth moving. A line beginning INSIDE a token usually carries no indentation at
all — its leading whitespace is the value of a Go raw string, a Java text block
or a template literal, and shifting it changes what the program prints rather
than how it reads. But a JSX child also begins inside a token, and there the
whitespace is layout the language throws away: refusing it left an element with
its tag re-indented and its contents where they were. So a line inside a token
gets one further question, :func:`_whitespace_elastic`, which asks the GRAMMAR
whether whitespace survives in that position rather than consulting a list of
node types. Both tests are structural, the first the same one
:mod:`flag_cleanup.python_fold` and
:func:`flag_cleanup.syntax.strip_introduced_trailing_whitespace` make, so
neither costs a list of node names to keep current.

There is a FOURTH kind, and it is the one the first three all quietly assumed
away: a line the grammar describes NOTHING at. Every test above asks which
token owns a byte, and each is sound only where a token owns every byte that is
not whitespace — which stopped being true at Dart (#2744), whose grammar emits
a multi-line string's two ``'''`` delimiters and no node whatever for what lies
between them. Its interior is therefore inside no token, so it read as ordinary
indentation and was re-indented, changing the value the program prints; and it
was in neither token stream, so :func:`_verified` compared the corrupted file
with the original and found them equal. :func:`_unmodelled` is the fourth
question and :func:`_unowned_content_survives` the matching whole-file check —
both, like the three above, properties asked of the tree rather than lists of
languages or node names.

Python is not excluded and needs no exclusion: ``python.toml`` folds only what
needs no re-indenting and :mod:`flag_cleanup.python_fold` re-indents the rest
itself, so by the time this runs there is almost nothing left to find. Arrived
at by measurement rather than by a language check, which is one less list to
rot.

**"Almost" is the honest word, and the reason matters more than the
exception.** What makes this safe is not that the pass declines to run on
Python but that ``python_fold`` has already applied the SAME prefix
substitution to every statement in the body, so the shift computed here equals
the column the line is already at and the write changes nothing. The lines
where the two disagree are bracketed continuations — where Python's
indentation is not significant, so a wrong answer is untidy rather than
unparseable. That is a property of how ``python_fold`` re-indents, not
something enforced here: were it ever to indent by column arithmetic instead of
by prefix substitution, this would start moving Python blocks with no gate able
to see it, because :func:`_verified` cannot judge Python at all (zero-width
``INDENT``/``DEDENT``). ``test_python_folds_arrive_already_indented`` walks the
Python fixtures and ``test_a_python_bracketed_continuation_comes_back_aligned``
pins the one shape that does move.
"""

from __future__ import annotations

import difflib
from functools import lru_cache

from flag_cleanup.syntax import (
    _construct_at,
    _construct_spans,
    _leaf_spans,
    _root_node,
    _walk,
    count_syntax_errors,
    supported,
)


def reindent_spliced_lines(before: str, after: str, language: str) -> str:
    """Re-indent the lines a fold moved, and nothing else.

    A file whose transform folded nothing is returned unchanged, so this is a
    no-op for a language whose rules splice nothing.
    """
    if after == before or not supported(language):
        return after
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    # Compared by CONTENT, so a line the splice only re-indented reads as the
    # same line. Comparing whole lines instead splits the run in two: Go's
    # untouched-column lines match exactly while Java's flattened ones do not,
    # and the shape this has to find spans both.
    opcodes = difflib.SequenceMatcher(
        None, [line.strip() for line in old], [line.strip() for line in new],
        autojunk=False,
    ).get_opcodes()
    folds = _folds(before, after, language, opcodes)
    if not folds:
        return after
    paired = _reanchored(_paired(opcodes, folds), folds, old, new)
    starts = _line_starts(after)
    leaves = _leaf_spans(after, language)
    out = list(new)
    for source, target in paired.items():
        line = old[source]
        if not line.strip():
            continue
        code = _code_start(new[target], starts[target])
        if code is None or _immovable(starts[target], code, leaves, after, language):
            continue
        wanted = _reindented(_indent(line), _indent(new[target]), source, folds)
        if wanted is not None:
            out[target] = wanted + new[target].lstrip()
    _shift_rewritten_body(out, new, starts, leaves, folds, after, language)
    return _verified("".join(out), after, language)


def _paired(
    opcodes: list[tuple[str, int, int, int, int]],
    folds: list[tuple[int, int, str, str, tuple[int, int]]],
) -> dict[int, int]:
    """Each input line mapped to the output line it became, where that is KNOWN.

    Two kinds of hunk answer it, and the second was missing until #3030:

    * **``equal`` — the line survived.** Matched on stripped content, so a line
      the splice only re-indented still reads as the same line; that is the
      whole point of diffing stripped lines rather than whole ones.
    * **``replace`` of N lines by N lines — the lines were rewritten in place.**
      A second read of the same flag inside its own branch, and still body
      lines: ``{social && <GoogleButton />}`` becomes ``<GoogleButton />`` and
      has to come up a level with everything around it.
      :func:`_shift_rewritten_body` covers only the rewrite sharing the FOLD's
      own hunk, so a nested gate whose whole body is ONE line was moved by
      nothing and kept the column it had while nested deeper — two columns off
      its siblings in JSX, and at column ZERO for a statement, which is where
      the C family's flattening leaves it (#3030).

    **Equal counts, and the restriction is the point rather than caution.** A
    ``replace`` of N by M has no correspondence to read at all, while equal
    counts means the k-th rewritten line answers the k-th original: the engine
    rewrites in place and never reorders, so a differing count is the only way
    a line can go missing from the middle of the run. Nothing needs the wider
    guess anyway — a multi-line rewrite keeps its inner lines verbatim
    (``{social && (`` … ``)}`` around an untouched element), so ``difflib`` puts
    those in ``equal`` hunks and the first kind above already carries them. That
    is exactly why no fixture caught this: ``tsx/jsx_gate_disjunction`` has
    single-line inner gates but its outer gate does not dedent them, and every
    other nested gate in the corpus is multi-line.

    **Take the run whole rather than a line at a time.** Two rewritten body
    lines that happen to be adjacent are ONE ``replace`` hunk, not two — the
    reported shape and the ternary beside it merge the moment they are written
    next to each other — so a rule admitting only the one-for-one case fixes the
    bug as reported and leaves it standing in the fixture written to lock it.

    A pair is a candidate, never a decision: :func:`_reindented` still has to
    find a fold whose body contains the input line and whose base its
    indentation extends, and a line failing either is left exactly as the
    engine wrote it.

    **What keeps the pairing honest is ``difflib``'s anchoring, NOT
    :func:`_reindented`'s ``placed`` check, and it is worth being clear about
    that because the reverse is the tempting thing to write.** ``placed``
    admits a line sitting at its own old column, at column zero, or at the
    fold's target — and between them those are very nearly every column a
    spliced line can legally occupy, since column zero is exactly where the C
    family flattens a body and the target is exactly where the first spliced
    line lands. So it is a good test of "did the engine put SOMETHING here" and
    a poor one of "is this THAT line". What actually holds is upstream: the
    engine rewrites in place and never reorders, so equal counts really do mean
    the k-th answers the k-th.

    The residue is the hazard :func:`_reindented` already names for closing
    delimiters — stripped content that repeats inside one fold body AT
    DIFFERING DEPTHS, where ``difflib`` can anchor a line against a namesake
    several lines away. Three `alpha(true);` at two depths mis-columns all
    three. That shape mis-columns identically WITHOUT this change (measured:
    the ``equal`` path carries the same exposure, and the pre-#3030 code gets
    the same file equally wrong), so this widens a door that was already open
    rather than opening one. Narrowing ``placed`` enough to separate two body
    lines of the same fold is a change to the ``equal`` path first and belongs
    with that defect, not here.

    ``consumed`` is what keeps this off :func:`_shift_rewritten_body`'s ground.
    A fold's own hunk is usually a different opcode, whose output range is
    disjoint from these by construction — but "usually" is not an invariant once
    a ``replace`` can be matched here, so its lines are dropped outright. That
    makes "a fold's own hunk belongs to :func:`_shift_rewritten_body` alone" a
    property of this function rather than something resting on
    :func:`_still_stands` happening to refuse every equal-count shape.
    """
    consumed = {
        index for _, _, _, _, (j1, j2) in folds for index in range(j1, j2)
    }
    paired = {}
    for tag, i1, i2, j1, j2 in opcodes:
        # One test for both kinds: an `equal` hunk is equal-length by
        # definition, so what is being asked of a `replace` is only that it be
        # the same shape — as many lines out as in, mapped in order.
        if tag not in ("equal", "replace") or i2 - i1 != j2 - j1:
            continue
        for step in range(i2 - i1):
            if j1 + step not in consumed:
                paired[i1 + step] = j1 + step
    return paired


def _reanchored(
    paired: dict[int, int],
    folds: list[tuple[int, int, str, str, tuple[int, int]]],
    old: list[str],
    new: list[str],
) -> dict[int, int]:
    """``paired``, with any fold body ``difflib`` anchored off by one re-laid.

    The hazard :func:`_paired` names and :func:`_reindented` was asked to catch:
    stripped content that repeats inside ONE fold body at differing depths.
    Three ``alpha(true);`` a level apart give ``difflib`` three namesakes to
    anchor the survivor against, it takes the first, and every pair after that
    anchor is off by one — so the body's second line is re-indented against the
    third's base, and the FIRST line, which the engine had already placed
    correctly at the header's own column, is moved a level deeper (#3040).

    **This cannot be fixed where it was first looked for.** ``placed`` admits a
    line at its own old column, at column zero and at the fold's target, and
    those are not three of the columns a spliced line can occupy — between them
    they are very nearly all of them, since column zero is exactly where the C
    family flattens a body and the target is exactly where its first line
    lands. A mis-paired body line therefore sits at a perfectly legal column
    and no tightening of that check can say otherwise. Neither can the content:
    the whole point of diffing stripped lines is that the namesakes are equal,
    and the line ``difflib`` skipped was skipped because the transform REWROTE
    it, so it does not match its own output either.

    What does settle it is the same premise :func:`_paired` and :func:`_folds`
    already rest on — **the engine rewrites in place and never reorders** — plus
    one fact about a fold that is read off the INPUT: its body is a contiguous
    run of lines, and it stays one. So where the body's own arithmetic is
    FORCED, the k-th surviving body line answers the k-th output line and the
    diff's opinion is not needed. Forced means all of:

    * the fold's hunk produced nothing (``j1 == j2``), so the body's output
      begins at ``j1`` — everything before the fold maps before it, and every
      line of the hunk at or before the header was deleted. A hunk that DID
      produce something is :func:`_shift_rewritten_body`'s ground and is left
      to it, which is why ``consumed`` is re-derived here rather than assumed;
    * no other fold lies inside this one. A nested fold deletes a header and a
      closing delimiter from the middle of this body, so the counts stop
      matching and positions stop answering positions;
    * the run ``new[j1 : j1 + len(body)]`` exists and is all non-blank — a
      blank line inside the run means the engine kept one where the body has
      none, and the counts have already diverged;
    * every output line in that run is currently claimed by a line of this fold
      or by its closing delimiter, and by nothing else. That is the guard that
      keeps a body which COLLAPSED — a multi-line rewrite coming back as one
      line — from claiming the statement after the fold: the run would reach a
      line already answering a source below ``end``, and the repair declines;
    * and at least one of those claims belongs to the BODY rather than to the
      closing delimiter. A fold's body does not always survive the fold, and
      nothing above notices: the arm a ``treated`` removal DELETES is a header
      with a body of its own, so :func:`_folds` records it like any other and
      its ``j1`` is wherever the deletion landed — which is the line AFTER the
      whole construct. Without this the C family re-indented an enclosing
      class's closing brace to the dead arm's column, on eight fixture cases
      across four languages. A body
      the transform rewrote in full leaves no paired line either and is
      declined the same way — conservative, and no loss: there is nothing there
      to re-anchor against.

    And it only fires at all when ``difflib`` disagrees, i.e. when the body's
    first line did not come back at ``j1``. Every fixture in the corpus pairs
    that line the ordinary way, so this is inert for all of them — which is the
    point: the shape it exists for is one the corpus had nowhere, and a repair
    that re-laid every fold would be a rewrite of the pass rather than a fix to
    it.

    A re-laid pair is still only a candidate. :func:`_reindented` applies the
    same ``placed`` test to it as to any other, so a body this misjudges is
    left exactly where the engine wrote it rather than moved somewhere new.
    """
    consumed = {
        index for _, _, _, _, (j1, j2) in folds for index in range(j1, j2)
    }
    for start, end, _, _, (j1, j2) in folds:
        if j1 != j2:
            continue
        if any(other[0] > start and other[1] <= end for other in folds):
            continue
        body = [index for index in range(start, end) if old[index].strip()]
        if not body or paired.get(body[0]) == j1:
            continue
        run = range(j1, j1 + len(body))
        if run.stop > len(new) or any(
            index in consumed or not new[index].strip() for index in run
        ):
            continue
        claimed = {source for source, target in paired.items() if target in run}
        if any(not start <= source <= end for source in claimed):
            continue
        if claimed.isdisjoint(body):
            continue
        for source in claimed:
            del paired[source]
        for offset, source in enumerate(body):
            paired[source] = j1 + offset
    return paired


def _folds(
    before: str,
    after: str,
    language: str,
    opcodes: list[tuple[str, int, int, int, int]],
) -> list[tuple[int, int, str, str, tuple[int, int]]]:
    """Every fold, as ``(body start, body end, base, target, rewritten region)``.

    A fold is a header line the transform DELETED, whose body survives it. Both
    halves are load-bearing and each closes a defect the diff-driven version
    shipped:

    * **The line must be a header (:func:`_opens`), and that header must be
      gone (:func:`_still_stands`).** A header the transform REWROTE is still a
      header and still owns its body — Python's ``if <read> == other:`` becomes
      ``if True == other:``, one line replaced by another — and moving that
      block up a level is not a formatting change there but a file CPython will
      not parse. Both questions used to be approximated by "did the hunk leave
      nothing but whitespace behind?", which answered them together and got each
      wrong in its own way once a rewrite shared the hunk; see those two for
      what replaced it and what each one costs when it is missing.
    * **The body's extent is read from the INPUT, not from the diff.** The
      body is the run of lines whose indentation strictly extends the header's,
      which is what "the body of that header" means and is a fact about the
      input alone. Taking it from the diff instead meant a blank line inside the
      branch — which the engine drops, so it has no counterpart to match — cut
      the run in two, and the second half then took its column from a line the
      engine had already flattened to zero. That did not merely fail to fix the
      file: it re-columned lines the engine never touched, against a base of
      zero. It also meant the run could continue PAST the end of the folded
      block, so a following statement indented deeper than the ``if`` — legal,
      just untidy — was dedented for no reason. Both are gone: a line outside
      the header's body is not in any fold's range, so nothing can reach it.

    The target is the header's own column, and it is the only source. Splicing
    a body up one level means exactly "the body now starts where its header
    did", and ``test_reindent.py`` measures that against what the engine itself
    does with the first spliced line, on every language that moves it — the
    nine that do agree, and Ruby, which moves nothing, is why reading the
    header is what this does rather than reading the output.

    **A hunk holds as many headers as it holds header lines, and asking only
    about the first got three ordinary shapes wrong (#3037).** ``difflib``
    merges adjacent deletions into ONE opcode, so "which line is this hunk's
    header?" has no single answer:

    * **A binding directly above the header it feeds** — the way this code is
      most often written. The ``const`` and the ``if`` are adjacent deletions,
      ``old[i1]`` is the binding, and a binding opens nothing, so the hunk was
      skipped and no fold was found AT ALL. Not a fold placed badly: the pass
      returned the engine's output untouched and the C family's column-zero
      flattening shipped as written, which in Go is the ``gofmt -l`` failure
      this module exists to prevent.
    * **Two ``if``s on one flag nested directly inside each other** — both
      headers deleted, one opcode, only the outer one read. :func:`_reindented`
      composes nested folds correctly and always did; there was simply one fold
      where there should have been two, so a body owing two levels came up one.
    * **Allman brace style**, where the ``{`` is on its own line. Here
      ``old[i1]`` DOES open something, so this looked nothing like the other
      two and failed one step later: the ``if`` line's body scan meets the
      ``{`` at the SAME column, so it finds no body and records no fold, and
      the ``{`` — which does have one — was never reached. It is the default
      brace style of every style guide C# ships with.

    In all three the tell is that ADJACENCY is the only ingredient: put one
    unrelated statement between the two lines, the deletions land in separate
    opcodes, and the same file comes back correct. Same flag, same rules, same
    language, one line either side of a ``difflib`` boundary.

    So every line the hunk DELETED is asked, and each header that did not
    survive contributes its own fold. The three guards below make that safe
    rather than merely more generous — a blank line is not a header, a line
    that opens nothing is not a header, a header still standing is refused —
    each now asked per LINE where they were asked once per hunk.

    **Which lines the hunk deleted is the load-bearing half, and the corpus is
    what settled it.** :func:`_still_stands` reads the hunk's after-side as a
    WHOLE, so it cannot say which input line a given output line was made from
    — and asking it about a line that was rewritten rather than removed
    produces a fold that dedents a block still standing. Two fixtures found
    exactly that: C#'s ``else if (<read>)`` + ``{`` becomes ``else {``, where
    the brace survives by moving onto the line above and the type test cannot
    see it; and Ruby's ``elsif premium?`` promoted to ``if premium?``, where
    the type changes outright. Both dedented a live branch's body.

    The rule that separates them takes the same premise :func:`_paired` rests
    on — the engine rewrites in place and never reorders — so the ``kept``
    non-blank lines a hunk produced answer its LAST ``kept`` input lines, and
    those are not asked. ``old[i1]`` is asked always, so a one-for-one hunk
    still reaches :func:`_still_stands`' first clause rather than being skipped
    by arithmetic that happens to agree with it. That clause counts lines in
    the hunk, so it fires only when the hunk IS one line, and then the header
    is ``old[i1]`` by construction — it never sees a header found part-way in,
    which is correct, since its reasoning is that a one-line hunk covers the
    header and nothing else.
    """
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    found = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal" or i2 == i1:
            continue
        kept = sum(1 for index in range(j1, min(j2, len(new))) if new[index].strip())
        # Every input line the hunk DELETED, not just the first: `difflib`
        # merges adjacent deletions into one opcode, so such a hunk holds as
        # many headers as it holds header lines. The `kept` lines it produced
        # answer its LAST `kept` input lines, which is the same premise
        # :func:`_paired` rests on — the engine rewrites in place and never
        # reorders — so those are the lines whose survival cannot be read off
        # the after-side and they are not asked. `old[i1]` is always asked, so
        # a one-for-one hunk still reaches :func:`_still_stands`' first clause
        # rather than being skipped by arithmetic that happens to agree with
        # it. See the docstring (#3037).
        for index in range(i1, max(i1 + 1, i2 - kept)):
            header = old[index]
            # A blank line is not a header. The engine drops blank lines from a
            # spliced body, which arrives here as its own deletion — and its
            # empty indentation would otherwise name column zero as every
            # following line's target.
            if not header.strip():
                continue
            target = _indent(header)
            end, base = index + 1, None
            while end < len(old):
                if not old[end].strip():
                    end += 1  # a blank line neither ends the body nor sets its base
                    continue
                line_indent = _indent(old[end])
                if len(line_indent) <= len(target) or not line_indent.startswith(target):
                    break
                if base is None:
                    base = line_indent
                end += 1
            # Nothing indented below the line means nothing to fold, and that
            # settles it without parsing — so the two questions below, which do
            # parse, are asked only of a line that could be a header. The order
            # matters because `_opens` is now asked once per DELETED LINE
            # rather than once per hunk: a `treated=false` removal, where the
            # whole guarded block is one contiguous deletion, would otherwise
            # pay a tree descent per statement in it — 0.146s against 0.013s on
            # a 4,800-line file with an 800-line block, where the whole of
            # `_folds` used to cost 0.011s.
            if base is None:
                continue
            opened = _opens(before, language, index)
            # A line that opens nothing is not a header, whatever the diff did
            # to it. A closing delimiter is the case that matters: `        }`
            # begins no node — the block it ends began pages earlier — so
            # reading it as a header invents a fold whose "body" is whatever
            # happens to be indented more deeply BELOW the real one, and dedents
            # it. The old guard hid this by refusing every non-blank hunk;
            # nothing else would catch it, because such a line has a perfectly
            # good indentation to be a target.
            if opened is None:
                continue
            if _still_stands(after, language, opened, i1, i2, j1, j2):
                continue
            found.append((index + 1, end, base, target, (j1, j2)))
    return found


def _shift_rewritten_body(
    out: list[str],
    new: list[str],
    starts: tuple[int, ...],
    leaves: list[tuple[int, int]] | None,
    folds: list[tuple[int, int, str, str, tuple[int, int]]],
    source: str,
    language: str,
) -> None:
    """Shift the body lines the rewrite CONSUMED, which have no survivor to follow.

    The loop above can only move a line :func:`_paired` matched, and a line
    inside a fold's OWN hunk is never one: that hunk's output range is exactly
    what :func:`_paired` drops, so whatever the rewrite consumed there is this
    function's alone to move. In ten languages it still costs nothing, because
    the line the rewrite consumed is the FIRST spliced line and the engine has
    already placed that one at the header's column. Ruby is the exception it
    always is: it moves nothing at all, so that line is still at the body's own
    depth and stays there — leaving `alpha(true)` at the old column while the
    line below it came back at the header's, a body split across two columns.
    Consistent and misplaced beats inconsistent, so leaving this out would have
    made Ruby worse than the engine had, which is the one outcome this module
    refuses.

    ``indent.startswith(base)`` is what makes reading the column off the OUTPUT
    sound here, and it is not the same read the module's docstring rules out. A
    line still carrying the body's own indentation is a line the engine did not
    move, so that indentation IS the input's — recovered, not invented. A line
    the engine did move is at the fold's target or at column zero, and ``base``
    strictly extends ``target``, so neither can start with it: already-placed
    lines cannot be shifted twice, whichever language produced them.

    Every after-line in one of these regions is body content, which is what the
    survival guard buys: the header is gone, so nothing else is left to be. No
    line is written twice: none of these regions can overlap a target
    :func:`_paired` produced, because ``consumed`` drops exactly these output
    ranges, and a line matched by more than one fold is COMPOSED rather than
    overwritten.

    **INNERMOST FIRST, and the composition is not decoration** (#3037). Two
    folds can now share one output region — two ``if``s on the same flag nested
    directly inside one another are adjacent deletions, so ``difflib`` merges
    them into a single opcode and both headers yield a fold against the same
    ``(j1, j2)``. A line inside both owes BOTH shifts. Applied independently
    against the engine's own indentation each one computes a single level, the
    second silently replacing the first, and the result is the split body this
    function exists to prevent: Ruby's ``alpha(true)`` came back one level above
    the ``beta`` beside it, which :func:`_reindented` had composed correctly.
    Running the shifts in the same order, each over the previous one's result,
    makes the two paths agree by construction.
    """
    shifted: dict[int, str] = {}
    for _, _, base, target, (j1, j2) in sorted(folds, key=lambda fold: -len(fold[2])):
        for index in range(j1, min(j2, len(new))):
            line = new[index]
            if not line.strip():
                continue
            indent = shifted.get(index, _indent(line))
            if not indent.startswith(base):
                continue
            shifted[index] = target + indent[len(base) :]
    for index, indent in shifted.items():
        code = _code_start(new[index], starts[index])
        if code is None or _immovable(starts[index], code, leaves, source, language):
            continue
        out[index] = indent + new[index].lstrip()


def _opens(source: str, language: str, line: int) -> str | None:
    """The construct line ``line`` opens, or ``None`` if it opens nothing.

    A thin read of :func:`~flag_cleanup.syntax._construct_at` in line terms:
    what begins at this line's first code character and is still open when the
    line ends. That is what makes something a header, and asking it of the input
    costs one parse and names no node type.
    """
    starts = _line_starts(source)
    opening = _code_start(_lines(source)[line], starts[line])
    if opening is None:
        return None
    return _construct_at(source, language, opening, _line_end(source, starts, line + 1))


def _still_stands(
    after: str,
    language: str,
    opened: str,
    i1: int,
    i2: int,
    j1: int,
    j2: int,
) -> bool:
    """Whether the header the transform touched is STILL A HEADER afterwards.

    :func:`_folds` has to know whether the transform DELETED the header or
    merely rewrote it, and until #2719 it approximated that with "did anything
    survive in its place?" — a hunk whose after-side held nothing but whitespace
    was a fold, and every other hunk was refused. The two differ exactly when a
    deletion and an unrelated rewrite land in ONE ``difflib`` opcode, which is
    what a second read of the SAME flag inside its own branch produces: the
    header is deleted and the inner read is rewritten in the same breath, so the
    after-side is non-blank and the fold was refused in all eleven languages. A
    read of a DIFFERENT flag in that position folded perfectly, which is the
    tell.

    Two clauses, and BOTH are needed. Each was derived from the corpus rather
    than reasoned about, and each refuses a family the other admits.

    **One line replaced by another — the header survived.** Measured across
    every rewritten-header case the fixtures hold, that family is `before=1,
    after=1` WITHOUT EXCEPTION: `} else if (<read>) {` becomes `} else {` in six
    languages, `case <read>:` becomes `default:` in Go and PHP, Ruby's `when`
    and `elsif` become `else`, and Python's `if <read> == other:` becomes
    `if True == other:`. This is not a heuristic but a reading of the opcode: a
    hunk covering exactly one input line covers the header and nothing else, so
    whatever stands in the after-side was made FROM the header. A fold, by
    contrast, always takes body lines with it — the eleven #2719 shapes are 3→1,
    3→2 and 2→1. Refusing here is what keeps this family at the column it
    already had, and getting it wrong dedents an `else` branch out of its own
    block in fifteen fixtures.

    **The construct is still there, still owning what follows.** The first
    clause cannot see a header rewritten in the same hunk as one of its body
    lines (`before=3, after=2`), so ask the tree as well. ``opened`` is the type
    :func:`_opens` read off the INPUT, and the header survived exactly when a
    construct of that same type begins at some line's first code character
    inside the rewritten region and still extends BEYOND it — still owning the
    lines this pass is about to move. Nothing here names a node type; the type
    comes from the input and is only ever compared with itself.

    Three details of that second clause:

    * **Beyond the region, not merely past the header.** The lines at risk are
      the paired lines BELOW the hunk, and a construct that opens and closes
      inside the region owns none of them. It used to own nothing at all, since
      no line inside a ``replace`` hunk was paired; since #3030 one can be, but
      only ever by an ENCLOSING fold whose body genuinely contains it — a
      rewritten header sitting inside an ``if`` that folded belongs one level up
      along with everything around it. That is the right answer, not the one
      this clause exists to refuse, which is a header dragging its OWN block up
      a level.
    * **At a line's first code character.** A construct that starts mid-line is
      part of an expression, not a header. The same rule in :func:`_opens` means
      `} else if (…) {` never reaches here at all — its line begins with the
      brace closing the block above it, so it opens nothing this can compare
      against, and that whole family is caught by the first clause.
    * **A region running to the end of the file cannot hold a survivor**, so
      nothing can escape it and the answer is trivially "gone" — correct rather
      than lucky: with no lines below the hunk there is nothing to protect.

    What this deliberately does NOT do is compare the header with the after-side
    by SIMILARITY, which is the shape the issue first proposed. It was measured
    and it does not separate: Python's rewritten header scores 0.432 against its
    replacement while the eleven true folds score at most 0.222 — a gap, but one
    only a threshold invented here could exploit, and 0.432 is itself below
    ``difflib``'s own 0.75 cutoff for calling a line modified rather than
    replaced, so the library's constant would refuse Python outright. Go's
    `case <read>:` → `default:` scores lower still than several true folds. A
    number in that gap would be load-bearing, arbitrary, and silent when it
    drifted.
    """
    new = after.splitlines(keepends=True)
    if i2 - i1 == 1 and any(new[index].strip() for index in range(j1, j2)):
        return True
    new_starts = _line_starts(after)
    heads = {
        start
        for index in range(j1, min(j2, len(new)))
        if (start := _code_start(new[index], new_starts[index])) is not None
    }
    if not heads:
        return False
    end = _line_end(after, new_starts, j2)
    return any(
        begin in heads and finish > end
        for begin, finish in _construct_spans(after, language, opened)
    )


def _code_start(line: str, start: int) -> int | None:
    """Byte offset of ``line``'s first non-whitespace character; ``None`` if blank.

    Byte, not column, for the reason :func:`_line_starts` gives: these offsets
    are compared against tree-sitter's own.
    """
    if not line.strip():
        return None
    return start + len(_indent(line).encode("utf-8"))


def _line_end(source: str, starts: tuple[int, ...], index: int) -> int:
    """Byte offset where line ``index`` begins, or the file's end past the last."""
    return starts[index] if index < len(starts) else len(source.encode("utf-8"))


def _reindented(
    indent: str,
    placed: str,
    source: int,
    folds: list[tuple[int, int, str, str, tuple[int, int]]],
) -> str | None:
    """``indent`` after every fold containing line ``source``; ``None`` if none do.

    INNERMOST FIRST, which is what makes nested folds compose. Two ``if``s on
    the same flag, one inside the other, both fold, and the inner body has to
    come up two levels rather than one: applied inner-then-outer each step sees
    an indentation the previous step produced, and the two shifts add. The other
    order applies the outer shift to a line the inner one has not moved yet, and
    the inner base no longer matches.

    A line whose indentation does not extend the body's base is left where it
    is rather than guessed at — it is inside the block but does not share its
    column, so there is no prefix to substitute.

    ``placed`` is where the ENGINE left this line, and checking it is what makes
    the line-matching trustworthy. The match comes from a diff over stripped
    lines, and a file's closing delimiters are all the same stripped line — so
    a nested double fold, where two ``}`` are deleted and two survive, really
    does align a deleted one with a survivor that is nowhere near it. Left
    unchecked that re-indented an enclosing method's brace to the inner block's
    column: worse than what the engine shipped, on a line the fold never
    touched.

    The check is that a spliced line can only be in one of three places, all of
    which were measured: where it started (Go, Ruby, and every line nested
    inside a flattened child), at column zero (the C-family's flattening), or
    already at a fold's target (the first spliced line, everywhere). A line
    anywhere else is not a line this fold moved, whatever the diff paired it
    with.
    """
    allowed = {indent, ""}
    applied = False
    for start, end, base, target, _ in sorted(folds, key=lambda f: -len(f[2])):
        if not start <= source < end or not indent.startswith(base):
            continue
        allowed.add(target)
        indent = target + indent[len(base) :]
        applied = True
    if not applied or placed not in allowed | {indent}:
        return None
    return indent


def _verified(result: str, after: str, language: str) -> str:
    '''``result``, or the engine's own output if a TOKEN moved rather than a line.

    The belt to the token check's braces, and the shape the design notes in
    ``CLAUDE.md`` asked for before any of this was written: a whitespace-only
    re-indent is correct exactly when the token stream is unchanged, because the
    only bytes it is allowed to touch are the ones no token owns. So the one way
    this pass can corrupt rather than merely misformat — moving a line whose
    leading whitespace is the value of a raw string or a text block — cannot
    reach a pull request even if the per-line guard above were to miss it.

    **That premise has an unstated half, and Dart is where it came due
    (#2744).** "The bytes no token owns" is only the same set as "whitespace"
    while the grammar has a token for every byte that is not whitespace. Dart's
    has not: a multi-line string's contents belong to no node, so the interior
    was in NEITHER token stream, moving it changed nothing either stream could
    see, and this returned a corrupted file having compared it and found it
    equal. The token comparison is therefore no longer reached first —
    :func:`_unowned_content_survives` runs ahead of it and asks the question
    tokens cannot: did any byte the grammar does not describe change? Ordering
    matters as much as the check, since the early ``produced == original``
    return below is exactly what a Dart string interior slips through.

    Compared by TEXT rather than by position: every span after an edit has
    moved, so comparing offsets would report every re-indent as a corruption.

    It does NOT cover Python, and cannot: tree-sitter's ``INDENT``/``DEDENT``
    are zero-width, so a Python block moved to the wrong column has the same
    token stream and passes here (measured, not assumed). Python's protection is
    the survivor guard in :func:`_folds`, which refuses to move a block whose
    header the transform kept, plus Gate 1 downstream. Layered that way
    round because Python is the one language where a wrong column parses as a
    different program rather than as ugly code.

    Returning the engine's output rather than raising is the conservative
    direction and the only sensible one. This pass is cosmetic, so a file it
    cannot safely tidy is a file that ships exactly as it shipped before — not
    a flag abandoned over indentation. Nothing here can be the reason a removal
    fails.

    **One token may move, and only in the one way the grammar says costs
    nothing.** Re-indenting a JSX child rewrites the text of the token holding
    it, so identity alone would throw away the WHOLE file's re-indent to undo a
    change that is by construction invisible — which is what pinned #2719 shape
    2 for two releases. Three conditions together license it, and dropping any
    one puts a corrupting edit back in reach: the two streams hold the same
    NUMBER of tokens, so nothing was lexed into being or out of it; a token that
    differs differs only in whitespace, its non-space runs identical and in
    order; and that token is one :func:`_whitespace_elastic` has asked the
    grammar about. A string's content fails the third and a file touching one
    still ships exactly as the engine left it.

    Position, not text, is what pairs the two streams — :func:`_tokens` and
    :func:`~flag_cleanup.syntax._leaf_spans` are built from the same walk in the
    same order, so index ``i`` names one token in both, and the equal count
    checked first is what makes that alignment mean anything.

    **A token that changed without CONTAINING a line start changed for some
    other reason, and that is refused whatever the grammar says about its
    whitespace.** This pass edits one thing — the run of whitespace that opens a
    line — so the only token whose text it can legitimately rewrite is one a
    line begins strictly inside. PHP is why the rule is written down rather than
    assumed: a heredoc there is not one token but one per LINE, each starting
    exactly at its own first byte, so no line begins strictly inside any of them
    and :func:`_covering_token` has never protected a heredoc body — only token
    identity ever did. Worse, blanking such a token leaves a whitespace-only
    heredoc line, which that grammar tokenises as nothing at all, so
    :func:`_whitespace_elastic` answers a confident and wrong "elastic". This
    check is what keeps the heredoc safe, and it is sound for the reason the
    probe is not: it is a fact about what this module does, not about a grammar.
    '''
    if result == after:
        return after
    if not _unowned_content_survives(result, after, language):
        return after
    produced, original = _tokens(result, language), _tokens(after, language)
    if produced == original:
        return result
    if len(produced) != len(original):
        return after
    spans = _leaf_spans(after, language)
    starts = _line_starts(after)
    for span, fresh, was in zip(spans, produced, original):
        if fresh == was:
            continue
        if fresh.split() != was.split():
            return after
        if not any(span[0] < start < span[1] for start in starts):
            return after
        if not _whitespace_elastic(after, language, span):
            return after
    return result


def _unowned_content_survives(result: str, after: str, language: str) -> bool:
    """Whether every byte NO token describes came through the re-indent intact.

    The whole-file counterpart to :func:`_unmodelled`, and the half of
    :func:`_verified` that can see a Dart string interior move. Between one
    token and the next lies a run of bytes the grammar accounts for only by
    omission. In ten languages that run holds nothing but whitespace, which is
    this pass's to rearrange. In Dart it can hold the contents of a multi-line
    string, which is not.

    **The run is split at its LAST NEWLINE, and everything before that point
    must be identical.** This pass edits exactly one thing — the whitespace that
    OPENS a line — so within any run only the tail after the final newline can
    legitimately be its own, and even that only while the tail really is nothing
    but whitespace (see :func:`_run_survives`). Everything earlier belongs to
    the construct the run follows.

    That split is what makes the check usable rather than merely strict, and
    Dart is where both halves are needed at once. Freezing a run wholesale
    looks right and quietly disables the pass: a `//` comment there has UNOWNED
    TEXT, and the run carrying it runs on past the newline to the next token —
    so re-indenting the line after any comment rewrites that run's tail, and a
    whole-run comparison reads an ordinary commented fold as a corruption and
    ships the file unindented. Measured, not feared: it turned a three-statement
    Dart fold with one comment in it into a complete no-op. Ruby's backslash
    line-continuation is the same shape in a second language.

    Comparing the runs modulo whitespace instead would fix that and give up the
    thing being protected — the whitespace INSIDE a string's interior IS its
    value, and a Dart interior line that moves changes bytes before the run's
    last newline, which is precisely what the head comparison catches.

    Runs are paired by POSITION and every one is emitted, empty runs included,
    so there is one more run than there are tokens on each side and index ``i``
    names the same gap in both. A differing count means the files are not
    comparable and the answer is no, which is the direction this module always
    fails in.
    """
    produced = _unowned_runs(result, _leaf_spans(result, language))
    original = _unowned_runs(after, _leaf_spans(after, language))
    if len(produced) != len(original):
        return False
    return all(
        _run_survives(fresh, was) for fresh, was in zip(produced, original)
    )


def _run_survives(fresh: bytes, was: bytes) -> bool:
    """Whether one unowned run changed only in ways this pass is allowed to.

    Split at the last newline, then TWO conditions, because the tail is only
    *usually* line-leading whitespace. The head must be identical outright. The
    tail must be identical too, unless it is blank on both sides — which is what
    says it really was nothing but the indentation of the line the next token
    opens.

    The second condition is not redundant, and dropping it re-opens the defect
    in a shape the first cannot see: a run whose LAST line carries content and
    then the closing delimiter — Dart's `    body''';` — puts that content in
    the tail, where a head-only comparison would wave a changed value through.
    """
    head, tail = _run_parts(fresh)
    was_head, was_tail = _run_parts(was)
    if head != was_head:
        return False
    return tail == was_tail or not (tail.strip() or was_tail.strip())


def _run_parts(run: bytes) -> tuple[bytes, bytes]:
    """``run`` split after its last newline.

    A run holding no newline becomes ALL TAIL, not all head — ``rfind`` returns
    -1 and the cut lands at 0. Stated because the intuitive reading is the
    opposite one, and "restoring" it would be a behaviour change: such a run is
    then protected only while it carries content, and a blank one may move. That
    is the correct direction for the case that actually occurs. A leaf whose own
    text ends in a newline exists (measured: 14 in ts, 2 in csharp across 2,606
    real files), and the run following one IS a line's whole indentation — so
    freezing every newline-free run would turn each of those files into the
    whole-file no-op this iteration exists to remove.
    """
    cut = run.rfind(b"\n") + 1
    return run[:cut], run[cut:]


def _unowned_runs(source: str, spans: list[tuple[int, int]]) -> list[bytes]:
    """The byte runs between consecutive tokens, in document order.

    Sorted here because :func:`~flag_cleanup.syntax._leaf_spans` reports reverse
    document order (see :func:`_tokens`), and a gap is only meaningful against
    its neighbours. Leaves do not nest — they are childless by construction — so
    the sorted spans are disjoint and the complement is well defined.

    The empty run rather than a skipped one is for zero-width tokens: Python's
    ``INDENT``/``DEDENT`` carry no bytes, and a token that ends where it begins
    must not drop a gap from the list the caller pairs by index. ``max`` on the
    running position is a different guard and is deliberately NOT the same one —
    a zero-width span leaves ``position`` unchanged either way, so what ``max``
    protects against is a NESTED or overlapping span rewinding the scan. The
    line above says leaves cannot nest and that is measured (zero overlapping
    pairs across 687 fixture files), which makes this unreachable defence rather
    than dead code: it costs nothing and it is the assumption's backstop.
    """
    data = source.encode("utf-8")
    runs: list[bytes] = []
    position = 0
    for begin, end in sorted(spans):
        runs.append(data[position:begin] if begin > position else b"")
        position = max(position, end)
    runs.append(data[position:])
    return runs


def _tokens(source: str, language: str) -> list[bytes]:
    '''The text of every childless node — the file's token stream, reversed.

    Reversed because :func:`~flag_cleanup.syntax._walk` is a stack, so it visits
    last child first. Harmless and left alone: both sides of the comparison in
    :func:`_verified` are built the same way, and reverse document order is a
    bijection with document order, so the equality means exactly what it would
    forwards. Named here so the next reader does not have to re-derive it.
    '''
    data = source.encode("utf-8")
    return [data[begin:end] for begin, end in _leaf_spans(source, language)]


def _indent(line: str) -> str:
    body = line.rstrip("\r\n")
    return body[: len(body) - len(body.lstrip())]


@lru_cache(maxsize=4)
def _lines(source: str) -> tuple[str, ...]:
    """``source`` split keeping line endings, held across the calls one file makes.

    :func:`_opens` wants ONE line and used to re-split the whole file to get
    it. Cached for the reason :func:`_line_starts` beside it is, and bounded
    the same way.
    """
    return tuple(source.splitlines(keepends=True))


@lru_cache(maxsize=4)
def _line_starts(source: str) -> tuple[int, ...]:
    """Byte offset at which each line of ``source`` begins.

    Cached, and a TUPLE so the cached value cannot be edited by one caller
    under another's feet. Four entries for the reason :func:`_encoded` holds
    four: one pass has a ``before`` and an ``after`` in play at once. Without
    the cache this is O(file) on every call, and since #3037 :func:`_opens` is
    asked once per DELETED LINE rather than once per hunk — measured at 0.011s
    to 0.603s on a 4,800-line file whose guarded block is one 800-line
    deletion, which is an ordinary `treated=false` removal, not a pathological
    input.

    Byte, not character: :func:`~flag_cleanup.syntax._leaf_spans` reports
    tree-sitter's own offsets, and a non-ASCII line above would put every span
    after it out of reach of a character index.

    ``splitlines`` here, unlike :mod:`flag_cleanup.python_fold`, which scans for
    ``\n`` alone because it must agree with CPython's tokeniser about what a
    line is. Nothing here has to: every list this walks comes from the SAME
    ``splitlines``, so a form feed inside a string simply makes one more
    fragment on both sides, and the offset of that fragment stays exact — which
    is all :func:`_covering_token` needs to keep its hands off it.
    """
    offsets: list[int] = []
    position = 0
    for line in source.splitlines(keepends=True):
        offsets.append(position)
        position += len(line.encode("utf-8"))
    return tuple(offsets)


def _covering_token(
    start: int, leaves: list[tuple[int, int]] | None
) -> tuple[int, int] | None:
    """The token a line beginning at ``start`` opens inside, or ``None``.

    Strictly inside: ``begin < start``, not ``begin <= start``. A line the
    engine flattened to column zero begins exactly where its first token does,
    and the inclusive test reads that as "inside a token" and protects the one
    line most in need of moving — which is precisely the shape the C-family
    languages produce. A token that STARTS a line is the line's code; only a
    token that started on an EARLIER line owns the whitespace in front of it.

    Returns the span rather than a boolean because being inside a token is only
    half the question: :func:`_whitespace_elastic` then asks whether THAT token
    keeps its whitespace, and it needs the span to ask.
    """
    return next((span for span in leaves or () if span[0] < start < span[1]), None)


def _immovable(
    start: int,
    code: int,
    leaves: list[tuple[int, int]] | None,
    source: str,
    language: str,
) -> bool:
    """Whether the whitespace opening this line is DATA rather than layout.

    THREE questions now, in the order that makes the cheap ones first: a line
    inside a token moves only if the grammar throws that token's whitespace
    away; a line whose first byte no token owns is ordinary indentation and
    moves — but only once the grammar is shown to model what FOLLOWS that
    whitespace, which is :func:`_unmodelled` and is the third.

    That third question exists because "no token owns the line's first byte" and
    "this is ordinary indentation" are not the same statement, and Dart is where
    they came apart (#2744). Its grammar emits the two ``'''`` delimiters of a
    multi-line string and NOTHING between them, so the interior belongs to no
    node at all: `_covering_token` finds no token to protect, and the line reads
    as ordinary indentation while its leading whitespace is the value the
    program prints. Re-indenting it changed what the string contained, which is
    the one outcome this module exists to make impossible.
    """
    span = _covering_token(start, leaves)
    if span is not None:
        return not _whitespace_elastic(source, language, span)
    return _unmodelled(code, leaves) or _opaque_interior(source, language, start)


def _unmodelled(code: int, leaves: list[tuple[int, int]] | None) -> bool:
    """Whether the byte at ``code`` — a line's first CODE byte — is in no token.

    The property the Dart gap needs, and deliberately a property rather than a
    list of node types or of languages: this package has had such enumerations
    leak four times, and "dart, plus whatever the next grammar hides" would be
    the fifth. Nothing here knows what a string is. It asks the only question
    that can be asked of a grammar that describes nothing — *does the grammar
    describe anything here at all?* — and treats "no" as a reason to keep its
    hands off, because a pass that cannot see what it is moving cannot know
    whether the whitespace in front of it is layout or data.

    Note this reads the first CODE byte, where :func:`_covering_token` reads the
    line's first byte. They are different questions and both are needed. A line
    inside a Go raw string begins strictly inside that string's content token,
    so the first byte answers it. A line inside a Dart string begins inside no
    token — and so does a line of perfectly ordinary code that merely follows a
    blank run — so only the first byte with something ON it separates the two.

    **Measured, not argued**, because a rule that refuses too much would quietly
    turn this whole pass off: across 723 fixture files and 3,181 real source
    files in the monorepo, exactly three families answer yes. A Dart string's
    interior, which is the defect. A Python docstring split by an escape
    sequence, where ``string_content`` gains a child and stops being a leaf —
    the same shape, in a language this pass is already a no-op for. And the
    byte-order mark on a C# file's first line, which is a byte no token owns
    because it is not part of the program. Ordinary code never answers yes: the
    grammar has a token for all of it.

    Fails CLOSED, like every other question here — a file with no tokens at all
    is one this cannot reason about, and the cost of a wrong "yes" is a line
    left ugly against a wrong "no" that rewrites a string.

    **It is not sufficient on its own**, because a line can hold nothing but the
    string's CLOSING DELIMITER — `    ''';` — whose first code byte is the
    `'''` token and is therefore owned, while the whitespace in front of it is
    still inside the string. :func:`_opaque_interior` is the question that
    covers that line, and the two are asked together.
    """
    return not any(begin <= code < end for begin, end in leaves or ())


@lru_cache(maxsize=4)
def _encoded(source: str) -> bytes:
    """``source`` as UTF-8, held across the calls one file makes.

    :func:`_opaque_interior` runs once per line and needs the file's bytes to
    read a node's inter-child gaps, so encoding on each call would be O(file)
    per line. Four entries for the same reason
    :func:`~flag_cleanup.syntax._root_node` holds four: one pass has a ``before``
    and an ``after`` in play at once.
    """
    return source.encode("utf-8")


def _opaque_interior(source: str, language: str, position: int) -> bool:
    """Whether ``position`` sits inside a node the grammar leaves CONTENT in.

    The companion to :func:`_unmodelled`, and the one that reaches the line a
    multi-line string closes on. `_unmodelled` reads the line's first CODE byte,
    which works while that byte is part of the unmodelled region — every
    interior line of a Dart string. It stops working for `    ''';`, where the
    first code byte is the delimiter TOKEN: owned, so the line reads as movable,
    while the whitespace before it is still the string's own. Eight of the nine
    languages with a multi-line literal are safe there only because their
    content token runs right up to the delimiter, so the line begins strictly
    inside it and :func:`_covering_token` answers first; Dart, having no content
    token, has nothing to begin inside.

    So this asks about the ENCLOSING NODE instead, in two steps. Descend to the
    innermost node strictly containing the byte and collect the gaps between THAT
    node's own children which hold anything but whitespace — a literal whose
    interior the grammar does not model has content sitting there, a structural
    container has only the whitespace the grammar skips. Content alone is not the
    answer, though, because elided syntax lands in those gaps too: the decision
    belongs to :func:`_gaps_hold_data`, which asks the grammar whether that
    content is data or syntax. Read that function before changing this one; the
    filter here is cheap and the decision there is the load-bearing half.

    **Scoping to the node's OWN children rather than to all its descendants is
    what keeps this from refusing everything**, and Dart is again where it
    shows: a `//` comment there is a node whose TEXT is unowned too, so a rule
    reading descendants would find unmodelled content inside every block holding
    a comment and freeze the file. The comment's text lives inside the comment
    node, never in its parent's inter-child gaps, so one level is the right
    level.

    **It is only ever asked AFTER :func:`_covering_token`, and that order is
    load-bearing rather than incidental.** Put this question first and it undoes
    #2737: asked in isolation it answers "opaque" for a JSX child and for a
    Javadoc block — 12 lines of the fixture corpus — because both sit in a node
    whose gaps carry their own text. Both are lines that BEGIN inside a token,
    so `_covering_token` finds one and :func:`_whitespace_elastic` rightly calls
    it elastic before this is reached. Measured along the real path
    (:func:`_immovable`, not this function alone): **zero** of the fixture
    corpus's 5,443 indented lines across eleven languages are refused, Java's
    multi-line call arguments and Dart's own nested blocks included — both of
    which a coarser "are this node's children all leaves" test does refuse. That
    is a FIXTURE number and reads as one; real source is not zero, and the delta
    that matters is at the foot of this docstring.

    **One residue, and it is a refusal this does not make.** A multi-line string
    with no non-whitespace TEXT anywhere in it — whatever interpolations it
    carries, and carrying none is the same case — leaves no content in any gap,
    so its closing line stays movable and moving it changes the string's value.
    Degenerate, and the alternative (reading descendants) costs every commented
    block in the language.

    **The change as a whole was measured as a DELTA against master**: across
    325,525 indented lines in eleven languages — the fixture corpus plus every
    `packages/`, `apps/` and `tests/` source file — this newly refuses **22**
    and newly permits nothing anywhere. A guard that only ever adds refusals is
    the safe direction, and that property is worth re-measuring rather than
    assumed if any of this is touched again.

    Thirteen of the 22 are Ruby backslash line-continuations. The other **nine
    are Dart's ADJACENT-STRING concatenation**, and they are the accepted cost of
    the correctness property rather than a defect: `'one '` and `'two'` on
    consecutive lines are ONE `string_literal` node with four quote children, so
    blanking their text is inert and this rightly calls them data. The
    whitespace opening the second line is layout, but the grammar hands this the
    same node it hands a multi-line interior and genuinely cannot tell them
    apart. Leaving a wrapped log message at its old column is the price of not
    corrupting a string, and it is the direction this module errs in everywhere.

    **Quote a delta against the iteration it was measured on.** Three existed
    here — no narrowing, the crossed-line narrowing, and this — refusing 26, 13
    and 22 lines respectively, and an earlier draft of this docstring reported
    13 alongside a "4 released" figure taken from a different pair. Both numbers
    were individually true and neither described what shipped.
    """
    node = _root_node(source, language).descendant_for_byte_range(position, position)
    if node is None or not node.start_byte < position < node.end_byte:
        return False
    data = _encoded(source)
    gaps, at = [], node.start_byte
    for child in node.children:
        if data[at : child.start_byte].strip():
            gaps.append((at, child.start_byte))
        at = child.end_byte
    if data[at : node.end_byte].strip():
        gaps.append((at, node.end_byte))
    return bool(gaps) and _gaps_hold_data(source, language, tuple(gaps))


@lru_cache(maxsize=64)
def _gaps_hold_data(
    source: str, language: str, gaps: tuple[tuple[int, int], ...]
) -> bool:
    """Whether a node's content-bearing gaps are DATA rather than elided SYNTAX.

    Having content in the gaps between its children is not by itself enough to
    call a node a literal, and the case that proves it is a `;` joining two
    statements on one line: tree-sitter keeps no node for the separator in Swift
    or Kotlin, so it lands in the enclosing block's gap exactly as a string's
    text lands in its literal's. One `alpha(); beta()` would otherwise make every
    line of that block immovable — and Kotlin freezes the block's own brace with
    them, so the fold emits a HALF-indented block, which reads as a bug in the
    tool where the whole-file fallback at least ships something consistent.

    So the two are separated by asking the GRAMMAR, in the idiom
    :func:`_whitespace_elastic` already uses for the neighbouring question: blank
    the bytes and re-parse. Elided syntax is load-bearing, so removing it parses
    worse — `alpha()  beta()` is not two Kotlin statements. A string's text is
    inert to the grammar, so removing it changes nothing. Blanking preserves
    LENGTH and NEWLINES, which keeps every offset outside the gap where it was
    and keeps a multi-byte character valid UTF-8, for the same reasons that
    function gives.

    **The direction is deliberately the opposite of `_whitespace_elastic`'s
    fail-closed one**, and it is not an inconsistency: there, a probe that parses
    worse is no evidence about whitespace and the answer is "do not move". Here,
    parsing worse IS the evidence — it is what says the content was syntax, so
    the node is a container and its inter-child whitespace is layout.

    An earlier cut of this asked instead whether a gap CROSSED A LINE, on the
    reasoning that content confined to one line cannot be any line's opening
    whitespace. That is true of the gap holding the position and false of the
    scan, which reads every gap of the node on purpose — so a Dart string whose
    text never crosses a line break, `'''Total: $ctx` with its closing `'''`
    below, had both its gaps discarded and was corrupted again. The
    justification and the use were asking different questions, which is the
    thing to check when a narrowing looks free.
    """
    data = bytearray(_encoded(source))
    for begin, end in gaps:
        for index in range(begin, end):
            if data[index] != 0x0A:
                data[index] = 0x20
    probe = bytes(data).decode("utf-8")
    return count_syntax_errors(probe, language) <= _syntax_errors(source, language)


@lru_cache(maxsize=4)
def _syntax_errors(source: str, language: str) -> int:
    """The file's own error count, which every probe compares against.

    Cached because it is invariant per file while the probe's own count is not:
    each blanked probe is a different string and has to be parsed, but the
    baseline is the same parse every time. Recomputing it once per candidate
    node doubled the cost of this path — measured at 4.24s against 2.39s on a
    synthetic 29KB Dart file holding 400 nodes with content-bearing gaps.

    The rest of that cost is inherent: a probe is a fresh string, so it cannot
    be cached, and :func:`_gaps_hold_data` is therefore the expensive path here.
    It runs only for a node that HAS content in its gaps, which in ten of the
    eleven languages is nothing at all.
    """
    return count_syntax_errors(source, language)


@lru_cache(maxsize=64)
def _whitespace_elastic(source: str, language: str, span: tuple[int, int]) -> bool:
    """Whether the GRAMMAR discards whitespace inside the token at ``span``.

    The third category of line, and the one that took two releases to name.
    :func:`_covering_token` alone splits lines in two — indentation, which
    moves, and the interior of a token, which never does — and that is right for
    a Go raw string, a Java text block and a template literal, where the leading
    whitespace is the value the program prints. It is wrong for JSX: a
    ``<div>``'s children are one token running from the opening tag's ``>`` to
    the closing tag's ``<``, so every line between them — the line holding
    ``</div>`` included — begins inside it, and the whole element came back with
    its tag re-indented and its contents at the old column (#2719 shape 2).

    **Asked of the grammar, not assumed, and not answered from a list of node
    types** — this package has had node-type enumerations leak three times, and
    ``jsx_text`` on a list here would be the fourth. The question a grammar can
    actually answer is whether whitespace at this position is an EXTRA, and the
    way to ask is to put nothing but whitespace there: blank the token's own
    bytes and re-parse. Between two JSX children the parser then yields NO node
    at all — the run is skipped, exactly as it is between two statements —
    while inside every string-ish token blanking leaves the content token
    covering the same bytes, because a string's extent is fixed by its
    delimiters and not by what it holds. That difference is the whole test, and
    it was measured across all eleven languages rather than reasoned about.

    It agrees with what JSX itself does downstream: the transform trims leading
    and trailing whitespace off each line of text and joins the rest with a
    single space, so a child's column is presentation and not content. Block
    comments come out elastic too, which is correct for the same reason and is
    a small bonus rather than a separate feature — a comment left behind at the
    old column is as wrong as a text node left there.

    **Blanking preserves LENGTH and NEWLINES**, so every byte offset outside the
    token — and every line boundary inside it — is exactly where it was, and the
    spans the re-parse reports can be compared against the originals directly.
    Non-ASCII is safe for the same reason: a three-byte character becomes three
    spaces, so the probe is still valid UTF-8 and still the same size.

    **Blanking alone is NOT sufficient, and PHP is the counterexample that
    proves it.** A token can vanish from the blanked parse for a second reason
    that has nothing to do with whitespace being skipped: the literal's own body
    rule may simply produce no fragment for a line that holds only spaces. A PHP
    nowdoc body is a run of sibling ``nowdoc_string`` fragments, one per line,
    and blanking one leaves a whitespace-only line the grammar keeps no fragment
    for — so the probe answers "elastic" about text PHP prints verbatim. Left at
    one condition this shipped a nowdoc dedented from column 8 to column 4, a
    changed value rather than an untidy file. C# ``@"…"`` is a second instance.

    So the token must ALSO sit among structured siblings. Whitespace is skipped
    between the CHILDREN of a construct, which is a place where children are
    things — a ``jsx_text`` node's siblings are the opening and closing elements,
    each a node with children of its own. A literal's body has no such
    structure: every sibling of a heredoc fragment is another raw fragment, and
    every sibling of a Go raw string's content is a delimiter. "At least one
    sibling of this token is not a leaf" is that distinction, and it is asked of
    the tree rather than of a list of node names.

    The two conditions are load-bearing in OPPOSITE directions, which is why
    neither can be dropped as redundant: a nowdoc fragment and a C# verbatim
    string pass the blank probe and are caught by the siblings; a template
    literal holding a substitution has a composite sibling and is caught by the
    blank probe. Measured across all eleven languages, not argued.

    Fails CLOSED in every direction that matters. A probe that parses worse than
    the source is not evidence about whitespace, a token the blanked parse still
    covers is data, and a span this cannot resolve to a node is a question it
    cannot answer; all three mean "do not move it". That is the right direction
    for a cosmetic pass: the cost of a wrong "no" is a line left ugly, and of a
    wrong "yes" a corrupted string.
    """
    node = next(
        (
            leaf
            for leaf in _walk(_root_node(source, language))
            if leaf.child_count == 0 and (leaf.start_byte, leaf.end_byte) == span
        ),
        None,
    )
    if node is None or node.parent is None:
        return False
    # `node` is childless, so it cannot be the sibling this finds.
    if not any(sibling.child_count for sibling in node.parent.children):
        return False
    data = source.encode("utf-8")
    begin, end = span
    blanked = bytes(byte if byte == 0x0A else 0x20 for byte in data[begin:end])
    probe = (data[:begin] + blanked + data[end:]).decode("utf-8")
    if count_syntax_errors(probe, language) > count_syntax_errors(source, language):
        return False
    return not any(
        other < end and beyond > begin for other, beyond in _leaf_spans(probe, language)
    )
