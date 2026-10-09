"""Text-level plumbing shared by the multi-way guard folds.

A subject-less multi-way guard — Go's tagless ``switch``, Ruby's subject-less
``case``, PHP's and JavaScript's ``switch (true)``, Kotlin's ``when { … }`` —
is an ``if``/``else if`` chain by another spelling, and the engine's built-ins
fold the ``if`` family only. Each language gets its own module because the
DESIGNS genuinely differ: whether an arm can fall into the next one, whether
``break`` binds to the construct, and whether the construct is an expression
that must stay exhaustive decide which rewrites are correct, and no table of
flags would make those arguments readable. What does NOT differ is the driver
and the line arithmetic, which is what lives here.

``go_fold``, ``ruby_fold`` and ``php_fold`` predate this module and carry their
own copies; their cut helpers differ from these in small, fixture-pinned ways
(which side of an arm the newline is taken from), so they were left alone
rather than migrated for the sake of it.
"""

from __future__ import annotations

from collections.abc import Callable

#: Bound on the rewrite loop. Each round removes or rewrites one arm, so a file
#: cannot need more rounds than it has arms; the cap guards against a rewrite
#: that fails to shrink the tree, never a limit reached in practice.
MAX_ROUNDS = 100


def fold_to_fixpoint(
    before: str,
    after: str,
    *,
    literal_arms: Callable[[str], int],
    fold_once: Callable[[str], str | None],
) -> str:
    """Apply ``fold_once`` until it declines, one rewrite per re-parse.

    Nothing happens if ``before`` already had a literal arm: ``case true:`` in
    a multi-way guard is real code somebody may have written — a permanently
    enabled toggle, a branch pinned by hand — and once the file contains one,
    a fold cannot tell the customer's from the engine's. Rewriting theirs would
    be an edit the flag removal never justified, so the file is left to Gate 1.

    One rewrite at a time with a re-parse between, rather than collecting edits
    and splicing them together: dropping an arm or collapsing a construct moves
    every offset after it, so a batch computed against one tree would be stale
    for every edit after the first.
    """
    if after == before or literal_arms(before):
        return after
    for _ in range(MAX_ROUNDS):
        rewritten = fold_once(after)
        if rewritten is None:
            return after
        after = rewritten
    return after


def line_indent(source: str, position: int) -> str:
    """The whitespace opening the line ``position`` sits on."""
    start = source.rfind("\n", 0, position) + 1
    prefix = source[start:position]
    return prefix[: len(prefix) - len(prefix.lstrip())]


def own_lines(
    source: str, start: int, end: int, *, comment: str = "//"
) -> tuple[int, int] | None:
    """``[start, end)`` widened to the whole lines it occupies, or ``None``.

    ``None`` when anything other than indentation precedes ``start`` on its
    line, or anything other than whitespace or a trailing ``comment`` follows
    ``end`` on its line — i.e. when deleting those lines would take code that
    is not part of the span with it. A caller declines rather than guessing
    how to cut a span that shares its line, and the residue gate turns that
    into a refusal.
    """
    line_start = source.rfind("\n", 0, start) + 1
    if source[line_start:start].strip():
        return None
    newline = source.find("\n", end)
    line_end = len(source) if newline == -1 else newline
    rest = source[end:line_end].strip()
    if rest and not rest.startswith(comment):
        return None
    return line_start, line_end + 1 if newline != -1 else line_end


def replace(source: str, start: int, end: int, text: str) -> str:
    return source[:start] + text + source[end:]


def dedented(source: str, start: int, end: int, base: str) -> str:
    """``source[start:end]`` re-indented so its first line sits at ``base``.

    ``start`` is the first byte of the first statement (past its indentation),
    so the first line is returned bare and every later line loses whatever
    indentation the first line had beyond ``base``. A line that does not carry
    that prefix — a continuation inside a multi-line string, say — is left
    exactly as written rather than corrupted.
    """
    inner = line_indent(source, start)
    extra = inner[len(base) :] if inner.startswith(base) else ""
    lines = source[start:end].splitlines()
    out = lines[:1]
    for line in lines[1:]:
        prefix = base + extra
        out.append(base + line[len(prefix) :] if line.startswith(prefix) else line)
    return "\n".join(out).rstrip()
