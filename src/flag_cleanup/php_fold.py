"""Fold the literal conditions Piranha leaves behind in PHP.

PHP is the second language to need a post-pass, and — like Python — the reason
is structural rather than a missing rule. Two shapes reach Gate 1 unfinished,
and neither can be closed with a Piranha rule:

1. **The alternative syntax, where MARKUP interleaves.** ``if (…): … else: …
   endif;`` is what a ``.phtml`` template is made of, and it is the idiom the
   extension is claimed for. The colon form on its own is fine — measured, the
   engine folds ``if (…): return 'a'; else: return 'b'; endif;`` in a plain
   ``.php`` file unaided, which is what ``phtml_alternative_syntax``'s
   ``Sidebar.php`` pins. What defeats it is an arm made of MARKUP: the read
   folded to ``if (false):`` and both arms stayed (markup-only arms, both
   treatments; a mixed arm folds on true and stands on false).

   A rule cannot finish it because **an arm is not a node**. The clause stops at
   its ``:``, and the markup after it is either a sibling of the clause or a
   ``text_interpolation`` — an ``extra``, so it floats up out of the
   ``colon_block`` that ought to hold it. The region between two clauses is raw
   TEXT, which is what this module edits.

   The same fact is why ``delete_trailing_else_if_false`` had to grow a
   ``body: (compound_statement)`` constraint: deleting the clause NODE in this
   syntax removes the guard and leaves its arm attached to the rung before it.
   That shipped, parsed, and was wrong.

2. **A mid-chain ``elseif`` serving true.** ``promote_trailing_else_if_true``
   may only fire on the LAST clause, because ``else { … } elseif { … }`` does
   not parse. Restructuring the chain instead — the clause becomes the terminal
   ``else`` and every clause after it goes, since they are unreachable — needs a
   rule that deletes the clause's SIBLINGS, and Piranha's ``Parent`` scope only
   ever matches a rule whose ``replace_node`` is the offered ancestor itself.
   Measured: rooted at the ``else_if_clause`` a rule fires and can rewrite that
   clause; rooted at the ``if_statement`` it fires only when it replaces the
   whole ``if_statement``, which a variable-length chain cannot be templated
   from. The engine's Python built-ins make the same rewrite for ``elif True:``.

Both are handled here by one chain walk, because they are the same problem
under two spellings of a body — ``:`` versus ``{`` — and splitting them would
have meant two walks that must agree.

This never re-indents. PHP's blocks are delimited, so
indentation is decorative and a fold that leaves a body at its old column is
cosmetic rather than wrong; #2694 tracks formatting for every language at once.
What this DOES tidy is tag balance: a deletion that would leave ``<?php ?>``
bracketing nothing takes both tags with it, and a deletion left alone on its
line takes the line. Without that a folded template keeps one empty PHP tag per
clause, which is a diff no reviewer would accept.

**A folded template's rendered whitespace moves, and that is deliberate.**
Removing the whole ``<?php … ?>`` line also removes the newline it sat on,
which PHP's ``?>`` had been swallowing anyway — so the surviving markup keeps
its own indentation instead of inheriting the guard's. Verified across 48
renderings of the fixtures under every combination of their conditions: the
markup emitted is identical once insignificant whitespace is collapsed, and
only the insignificant whitespace differs. The alternative is leaving an empty
tag pair or a blank indented line behind, which is a worse diff for a change
no browser can see.

Everything here still answers to Gate 1: this runs before it, so a fold that
produces something the grammar cannot parse is caught and the whole transform
rolled back, exactly as an engine-produced one would be.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_php
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_php.language_php())
_PARSER = Parser(_LANGUAGE)

#: Bound purely as a runaway guard. Each rewrite strictly removes a literal
#: condition, and there are finitely many, so this terminates on its own.
_MAX_ROUNDS = 200

#: The two body spellings this module can finish. A braceless single-statement
#: body (``if ($x) foo();``) is neither, and is deliberately not folded here:
#: that fold is the engine's (a whole statement replaced by a whole statement,
#: so the parent position is never asked to hold anything new), and whatever
#: the engine leaves standing is Gate 1's.
_COLON, _BRACE = "colon_block", "compound_statement"

#: Whitespace a deletion may absorb at its seam. Deliberately not `\r` or `\n`:
#: joining two lines is a restructuring, not a tidy-up.
_HORIZONTAL = (b" ", b"\t")


def fold_literal_conditions(before: str, after: str) -> str:
    """Fold every ``if``/``elseif`` the transform left with a literal condition.

    Nothing happens if ``before`` already had one: ``if (true):`` is real
    code — a permanently-enabled toggle,
    a branch somebody commented out by hand — and once the file contains one
    this cannot tell the customer's from the fold's. Rewriting theirs would be
    an edit the flag removal never justified, so the whole file is left to
    Gate 1 instead.

    Applied one rewrite at a time with a re-parse between, rather than
    collecting every edit and splicing them together. Folding changes the shape
    of the tree around it — dropping a chain's tail, promoting a clause — so a
    batch of offsets computed against the original tree would be stale for
    every edit after the first.
    """
    if after == before or _literal_conditions(before):
        return after
    for _ in range(_MAX_ROUNDS):
        rewritten = _fold_once(after)
        if rewritten is None:
            return after
        after = rewritten
    return after


def _literal_conditions(source: str) -> int:
    """How many ``if``/``elseif`` conditions in ``source`` are a boolean literal."""
    root = _PARSER.parse(source.encode("utf-8")).root_node
    return sum(
        1
        for node in _iter(root)
        if node.type in {"if_statement", "else_if_clause"}
        and _literal(node.child_by_field_name("condition")) is not None
    )


def _fold_once(source: str) -> str | None:
    """Apply the first foldable literal condition found; ``None`` if there is none."""
    data = source.encode("utf-8")
    root = _PARSER.parse(data).root_node
    tags = _PhpTags(root)
    for node in _iter(root):
        if node.type != "if_statement" or _under_dangling_else(node):
            continue
        chain = _chain(node)
        if chain is not None:
            rewritten = _rewrite(data, tags, node, chain)
            if rewritten is not None:
                return rewritten
    return None


#: Brace-less bodies that leave an ``if`` under them open to a later ``else``.
#: A ``do`` closes its body with ``while (…);``, so it ends the chain.
_DANGLING_BODIES = frozenset(
    {"if_statement", "foreach_statement", "for_statement", "while_statement", "declare_statement"}
)


def _under_dangling_else(node: Node) -> bool:
    """Whether ``node`` is, or sits under, an ``if`` whose else may not be its own.

    PHP gives an ``else`` to the innermost open ``if``; the grammar gives it to
    an outer one. In ``if ($a) if (<read>) { … } else …;``, and equally with
    brace-less ``foreach``/``for``/``while``/``declare`` bodies (or more
    ``if``s) between the two at any depth, the inner ``if`` therefore reads as
    alternative-free, and folding it moves the ``else`` onto the outer one: a
    branch that ran when the read was false now runs on the outer condition.
    Measured with php 8.2. Wherever the grammar hangs the ``else``, it hangs it
    on an ``if`` whose body is one of :data:`_DANGLING_BODIES`, so nothing at or
    under such an ``if`` is folded; the engine declines the same set. That also
    declines an ``else`` that really is the outer ``if``'s, which only leaves a
    literal for Gate 1.
    """
    current: Node | None = node
    while current is not None:
        if current.type == "if_statement":
            body = current.child_by_field_name("body")
            if (
                body is not None
                and body.type in _DANGLING_BODIES
                and current.child_by_field_name("alternative") is not None
            ):
                return True
        current = current.parent
    return False


def _iter(node: Node):
    """Depth-first, in source order, so the OUTERMOST fold happens first.

    Order matters: folding an outer ``if (true):`` can delete an inner one
    outright, and rewriting the inner one first would be work spliced away a
    moment later — or worse, an offset into text the outer rewrite has already
    moved.
    """
    yield node
    for child in node.children:
        yield from _iter(child)


def _literal(condition: Node | None) -> bool | None:
    """``True``/``False`` if this condition is that literal, else ``None``.

    ``parenthesized_expression`` is unwrapped because PHP's grammar wraps EVERY
    condition in one — the same fact ``syntax._PROFILES["php"]`` records as a
    ``transparent_node`` — and the literal is spelled by a single ``boolean``
    node for both values rather than by a node type per value. PHP keywords are
    case-insensitive, so ``TRUE`` and ``True`` are the same literal and are
    compared lowercased; the grammar accepts all three spellings.
    """
    inner = condition
    while inner is not None and inner.type == "parenthesized_expression":
        named = inner.named_children
        inner = named[0] if len(named) == 1 else None
    if inner is None or inner.type != "boolean":
        return None
    return inner.text.lower() == b"true"


@dataclass(frozen=True)
class _Clause:
    """One rung of an ``if``/``elseif``/``else`` chain.

    ``node`` is what the rung's header STARTS at, which is not always what its
    condition and body hang off: the ``if_statement`` itself for the leading
    rung, the ``else_if_clause`` for a one-word ``elseif``, and — for a two-word
    ``else if`` — the ``else_clause``, whose condition and body come from the
    ``if_statement`` nested inside it. Keeping the three uniform here is what
    lets every rewrite below say "replace from the rung's start to its
    ``header_end``" without asking which spelling it has.
    """

    node: Node
    condition: Node | None
    #: One past the ``:`` (alternative syntax) or at the ``{`` (braced) — the
    #: first byte of this rung's ARM.
    header_end: int
    #: Where this rung's arm stops: the next rung's first byte, or the chain's
    #: terminator. Not a node boundary in the alternative syntax, which is the
    #: whole reason this module exists.
    arm_end: int
    literal: bool | None


def _chain(node: Node) -> tuple[list[_Clause], str] | None:
    """Every rung of ``node``'s chain, plus which body spelling it uses.

    **The two-word ``else if`` is FLATTENED into the same list as the one-word
    ``elseif``, and that is what makes one set of rewrites serve both.** PHP
    spells the two differently and parses them differently: ``elseif`` is a flat
    ``else_if_clause`` sibling, while ``else if`` is an ``else_clause`` wrapping
    a whole nested ``if_statement`` that owns the REST of the chain — the
    C-family nesting, in the one language that also has the flat form. Read as
    a rung it is neither special nor nested: the clause starts at ``else``, its
    condition and body come from the statement inside it, and that statement's
    own clauses continue the walk. Descending like that is why
    :func:`_promote_to_else` can replace ``else if (true) `` with ``else `` and
    :func:`_cut` can drop a dead ``else if`` rung without either knowing which
    spelling it is looking at.

    ``None`` when the statement is not one this module can finish: a braceless
    body, a chain mixing the two body spellings, or an alternative-syntax chain
    with no ``endif`` (which the grammar only produces inside an error).
    """
    body = _body_of(node)
    if body is None or body.type not in {_COLON, _BRACE}:
        return None
    kind = body.type
    if kind == _COLON:
        end = next(
            (child.start_byte for child in node.children if child.type == "endif"), None
        )
        if end is None:
            return None
    else:
        end = node.end_byte

    rungs: list[tuple[Node, Node | None, Node]] = [
        (node, node.child_by_field_name("condition"), body)
    ]
    current = node
    while current is not None:
        nested = None
        for clause in current.children:
            if clause.type not in {"else_if_clause", "else_clause"}:
                continue
            clause_body = _body_of(clause)
            if clause_body is None:
                return None
            if clause.type == "else_clause" and clause_body.type == "if_statement":
                inner = _body_of(clause_body)
                if inner is None or inner.type != kind:
                    return None
                rungs.append(
                    (clause, clause_body.child_by_field_name("condition"), inner)
                )
                nested = clause_body
                break
            if clause_body.type != kind:
                return None
            rungs.append((clause, clause.child_by_field_name("condition"), clause_body))
        current = nested

    starts = [rung.start_byte for rung, _, _ in rungs[1:]] + [end]
    built = []
    for (rung, condition, rung_body), arm_end in zip(rungs, starts):
        header_end = _header_end(rung_body, kind)
        if header_end is None:
            return None
        built.append(_Clause(rung, condition, header_end, arm_end, _literal(condition)))
    return built, kind


def _body_of(rung: Node) -> Node | None:
    """A rung's body. Both spellings of every clause type call the field ``body``."""
    return rung.child_by_field_name("body")


def _header_end(body: Node, kind: str) -> int | None:
    """The first byte of the arm this body introduces.

    A ``colon_block`` starts with its ``:`` and then — this is the trap — may or
    may not go on to OWN the arm: it swallows the arm's statements when the arm
    has any, and stops dead at the ``:`` when the arm is markup only, because
    ``text_interpolation`` is an ``extra`` and floats up to the enclosing
    ``if_statement``. Measuring the arm from the ``:`` rather than from the
    block's end is what makes both cases one case.
    """
    if kind == _BRACE:
        return body.start_byte
    colon = body.children[0] if body.children else None
    return colon.end_byte if colon is not None and colon.type == ":" else None


def _rewrite(data: bytes, tags: "_PhpTags", node: Node, chain) -> str | None:
    """The one rewrite this chain needs, or ``None`` if it has no literal rung."""
    clauses, kind = chain
    first = clauses[0]
    if first.literal is True:
        return _fold_to_arm(data, tags, node, first)
    if first.literal is False:
        if len(clauses) == 1:
            return _cut(data, tags, node.start_byte, node.end_byte)
        second = clauses[1]
        if second.condition is None:
            return _fold_to_arm(data, tags, node, second)
        return _promote_to_if(data, node, second, kind)
    for index, clause in enumerate(clauses[1:], start=1):
        if clause.literal is True:
            return _promote_to_else(data, tags, clauses, index, kind)
        if clause.literal is False:
            return _cut(data, tags, clause.node.start_byte, clause.arm_end)
    return None


def _fold_to_arm(data: bytes, tags: "_PhpTags", node: Node, clause: _Clause) -> str:
    """Replace the whole statement with the one arm that runs.

    Two deletions — the header and everything before the arm, then everything
    from the arm's end to ``endif;``/the last ``}`` — with the arm left standing
    between them. Nothing is rebuilt from captures, so a comment inside the
    surviving arm survives with it; that is the property Ruby's ``elsif``
    promotion could not have (CLAUDE.md), and the reason to prefer two cuts over
    a splice even where a splice would read more directly.

    The arm is copied VERBATIM, tags and all, and that is what keeps a template
    balanced: the statement begins in PHP mode and ends in PHP mode, and so does
    the arm — it starts one byte past a ``:`` and stops one byte before the next
    clause keyword. An arm that opens with ``?>`` and closes with ``<?php``
    therefore lands in a position that expects exactly that.
    """
    if not data[clause.header_end : clause.arm_end].strip():
        return _cut(data, tags, node.start_byte, node.end_byte)
    tail_start, tail_end = _widen(data, tags, clause.arm_end, node.end_byte)
    head_start, head_end = _widen(data, tags, node.start_byte, clause.header_end)
    if head_end > tail_start:
        # The two widenings met in the middle, so between them they would delete
        # part of the arm — silently, because a Python slice with a reversed
        # range is empty rather than an error. Only reachable for an arm the
        # `.strip()` above did not call blank yet which holds no line of its own;
        # fall back to the exact boundaries, which cannot overlap by
        # construction, and accept the stranded tags.
        head_start, head_end = node.start_byte, clause.header_end
        tail_start, tail_end = clause.arm_end, node.end_byte
    return (
        data[:head_start] + data[head_end:tail_start] + data[tail_end:]
    ).decode("utf-8")


def _promote_to_if(data: bytes, node: Node, second: _Clause, kind: str) -> str:
    """The leading ``if`` is false: the first ``elseif`` becomes the new ``if``.

    One replacement rather than a deletion plus an edit, spanning from the dead
    ``if`` through the promoted clause's own header — so the arm that follows
    does not move at all, and no tag can be left unbalanced because none is
    crossed that was not already inside the replaced text.
    """
    condition = data[second.condition.start_byte : second.condition.end_byte]
    header = b"if " + condition + (b":" if kind == _COLON else b" ")
    return (
        data[: node.start_byte] + header + data[second.header_end :]
    ).decode("utf-8")


def _promote_to_else(
    data: bytes, tags: "_PhpTags", clauses: list[_Clause], index: int, kind: str
) -> str:
    """A mid-chain ``elseif`` is true: it becomes the ``else`` and the tail goes.

    Every clause after this one is unreachable, so dropping them is not a
    tidy-up — it is what makes the promotion legal. ``else`` may only be the
    last rung, which is the anchoring ``promote_trailing_else_if_true`` carries
    and the reason it must decline this shape.

    Back to front: the tail deletion sits later in the file than the header
    replacement, so removing it first leaves the header's offsets — both before
    it — still valid.
    """
    clause = clauses[index]
    tail_start, tail_end = clause.arm_end, clauses[-1].arm_end
    if tail_start < tail_end:
        tail_start, tail_end = _widen(data, tags, tail_start, tail_end)
    header = b"else:" if kind == _COLON else b"else "
    return (
        data[: clause.node.start_byte]
        + header
        + data[clause.header_end : tail_start]
        + data[tail_end:]
    ).decode("utf-8")


def _cut(data: bytes, tags: "_PhpTags", start: int, end: int) -> str:
    """Delete ``[start, end)``, widened so it does not strand its own scaffolding."""
    start, end = _widen(data, tags, start, end)
    return (data[:start] + data[end:]).decode("utf-8")


def _widen(data: bytes, tags: "_PhpTags", start: int, end: int) -> tuple[int, int]:
    """Grow a deletion to take the tags and the line it would otherwise strand.

    Two independent steps, in this order because the first can make the second
    apply:

    * **Tag balance.** If the only thing between the preceding ``<?php`` and
      this deletion is whitespace, and likewise between the deletion and the
      following ``?>``, then removing the deletion leaves a PHP region holding
      nothing. Both tags go with it. Either side alone is NOT enough: a region
      that still holds a statement keeps its tags, which is what stops this from
      merging two unrelated PHP blocks.
    * **Whole lines.** If nothing but whitespace precedes the deletion on its
      first line and follows it on its last, the lines go too — otherwise every
      folded template keeps one blank, indented line per clause removed.

    Both are decided from the parse tree's tag positions rather than by
    searching the text for ``<?php``, so a heredoc or a string that spells a tag
    cannot be mistaken for one.

    Failing both, the deletion takes the horizontal whitespace in front of it —
    but ONLY when whitespace also follows, so the two sides cannot be joined
    into one token. That is the separator of the thing being removed
    (``<?php $n = 1; `` before a guard, ``} `` before a promoted clause), and
    keeping it is how a fold leaves ``;  ?>`` or a line ending in a space. The
    seam is always a clause or statement boundary, never the interior of a
    token, so trimming it cannot change a string's value.
    """
    open_start = tags.open_before(data, start)
    close_end = tags.close_after(data, end)
    if open_start is not None and close_end is not None:
        start, end = open_start, close_end
    line_start = data.rfind(b"\n", 0, start) + 1
    newline = data.find(b"\n", end)
    line_end = len(data) if newline == -1 else newline + 1
    if not data[line_start:start].strip() and not data[end:line_end].strip():
        return line_start, line_end
    if data[end : end + 1].isspace():
        while start > line_start and data[start - 1 : start] in _HORIZONTAL:
            start -= 1
    return start, end


class _PhpTags:
    """Where every ``<?php`` ends and every ``?>`` begins, by byte offset.

    Keyed the way :func:`_widen` asks the question — "is there an open tag
    immediately before me" — so the lookup is a dict hit rather than a scan.
    """

    def __init__(self, root: Node) -> None:
        self._opens: dict[int, int] = {}
        self._closes: dict[int, int] = {}
        for node in _iter(root):
            if node.type == "php_tag":
                self._opens[node.end_byte] = node.start_byte
            elif node.type == "php_end_tag":
                self._closes[node.start_byte] = node.end_byte

    def open_before(self, data: bytes, position: int) -> int | None:
        while position > 0 and data[position - 1 : position].isspace():
            position -= 1
        return self._opens.get(position)

    def close_after(self, data: bytes, position: int) -> int | None:
        while position < len(data) and data[position : position + 1].isspace():
            position += 1
        return self._closes.get(position)
