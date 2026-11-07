"""Cut a removed flag's conjunct, or its ``key=value`` list item, out of a comment.

:mod:`flag_cleanup.comment_sentences` cuts a sentence that says nothing but the
flag. Two smaller shapes sit INSIDE a sentence that also says something true,
and used to be reported for a person to reword::

    const canEdit = useCanEdit(orgId); // release flag + Pro plan
    /** Whether to show the editor: the `new-edit` release flag AND a Pro plan. */
    // Run with FORCE_FLAGS=new-edit=true,other=true

The first two are conjunctions whose flag half the removal made always true, so
the comment is cut down to the other half (``// Pro plan``, ``Whether to show
the editor: a Pro plan.``). The third is a list item (``FORCE_FLAGS=other=true``
afterwards). Both are deliberately narrow, because a wrong cut deletes prose a
reader needed and nothing checks prose. A comment is only touched when ALL of
these hold:

* it is LINKED to the removed flag: it names the key, or (the call-site form)
  the run reported it at a call of a function it removed the flag's only read
  from, and then it is one comment whose whole text is the conjunction;
* the conjunction is binary, ``<flag phrase> (+|&|&&|AND|and) <words>`` or the
  words first, and runs to the end of the sentence (a clause start, or after a
  ``:`` lead-in with no negation in it). A flag phrase is ``[the] [<key>]
  [release|feature] (flag|gate|toggle)`` with the key optionally backticked, and
  names no key but the removed one; the other side is plain words with no
  ``or``, ``not``, ``unless``, a second operator or any symbol;
* the flag was served ``true``: removing a ``true`` conjunct leaves the other
  half, while a ``false`` one would leave the whole thing false, so nothing is
  cut then;
* a list item is ``<key>=<value>`` (or ``:``) in a comma-separated run of
  other such items, written without spaces, with at most an ``UPPER_CASE=``
  assignment in front of the first. A list left empty is left alone;
* the line is no tool directive, and the file's code tokens come out exactly as
  they went in.

Anything else stays as it was and is reported as before.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from flag_cleanup import stale_comments
from flag_cleanup.comment_sentences import Reword
from flag_cleanup.stale_comments import _code_tokens, _comments, _line_starts, _parse, is_directive_line

_NEGATIONS = re.compile(
    r"\b(?:not|never|no|unless|except|without|off|disabled|false|only|or|nor|but|if|when|whenever|until)\b",
    re.IGNORECASE,
)
#: Words of the other half: plain, and never a second operator, a condition
#: or a verb (a verb after the conjunction makes it a clause that goes on).
_WORD = re.compile(r"[A-Za-z][\w'-]*")
_STOP = frozenset(
    (
        "and or nor not no unless except if when whenever until while but only without with also then else off on"
        " is are was were be been being am has have had does do did can could will would shall should must may might"
        " its it their his her our your this that these those which who whose"
    ).split()
)
_OPERATOR = r"\s+(?:\+|&&?|AND|and)\s+"
#: The ONLY lead-ins a conjunction may follow: a condition or a requirement.
#: Anything else ("Deprecated:", "Cache key:", "Returns true:") changes what
#: the words after it mean, so the comment stays.
_LEAD = (
    r"(?:Whether to (?:show|display|enable|render|allow|offer|use)\b[^:.]{0,40}:"
    r"|(?:Requires?|Required|Gated (?:on|by)|(?:Shown|Visible|Enabled|Available|Rendered|Active)(?: only)? (?:when|if|for)"
    r"|Only (?:when|if|for)):?)\s+"
)
#: A marker in front of a comment line's words, and the closer behind them.
_OPENER = re.compile(r"^(?:/{2,}!?|/\*+!?|\s*\*+(?!/))[ \t]*")
_CLOSER = re.compile(r"[ \t]*\*+/\s*$")


def _flag_phrase(key: str) -> str:
    """``[the] <key> [release|feature] flag|gate|toggle`` or ``[the] release
    flag|toggle``. A bare "the flag", "feature flag" and "release gate" are not
    one: each is as often a field, a domain object or a deploy window."""
    quoted = re.escape(key)
    return (
        r"(?:the\s+)?(?:(?:`" + quoted + r"`|" + quoted + r")\s+(?:(?:release|feature)\s+)?(?:flag|gate|toggle)"
        r"|release\s+(?:flag|toggle))\b"
    )


def _plain_words(text: str) -> bool:
    words = text.split()
    if not words or len(words) > 6:
        return False
    return all(_WORD.fullmatch(w) and w.lower() not in _STOP for w in words)


def conjunct(content: str, key: str, *, linked: bool) -> str | None:
    """``content`` (a comment line without its markers) with the flag half of
    its conjunction cut, or ``None`` when it has none of the shape.

    The shape is a WHITELIST. The line is either exactly ``<flag phrase> OP
    <words>`` (or the reverse), allowed only for ``linked`` call-site comments,
    which the run tied to the flag by a call; or the same after one of the
    condition lead-ins of :data:`_LEAD`. The words are plain nouns, no verb, no
    second operator, condition or symbol, and the line ends with them."""
    text = content.rstrip()
    flag = _flag_phrase(key)
    leads = rf"(?P<head>{_LEAD})" if not linked else rf"(?P<head>{_LEAD}|)"
    for pattern in (
        rf"^{leads}(?P<flag>{flag}){_OPERATOR}(?P<rest>[^.:;,()`\[\]{{}}]+?)(?P<dot>\.?)$",
        rf"^{leads}(?P<rest>[^.:;,()`\[\]{{}}]+?){_OPERATOR}(?P<flag>{flag})(?P<dot>\.?)$",
    ):
        found = re.match(pattern, text, re.IGNORECASE)
        if found is None:
            continue
        if key not in found.group("flag") and not linked:
            continue
        rest = found.group("rest").strip()
        if not _plain_words(rest):
            continue
        # A key in the other half, or a second flag phrase, is a different
        # condition altogether.
        if key in rest or re.search(r"\b(?:flag|gate|toggle)s?\b", rest, re.IGNORECASE):
            continue
        out = found.group("head") + rest + found.group("dot")
        if key in out:
            continue
        return out + content[len(text) :]
    return None


_ITEM = r"[\w.-]+[=:][\w.-]+"


def list_item(content: str, key: str, *, kept: bool = True) -> str | None:
    """``content`` with the ``<key>=<value>`` item of a comma-separated list
    removed with one comma, or ``None``. Only an item whose value is the
    treatment the run KEPT (``true`` when ``kept``, else ``false``) goes: any
    other value says something the removal did not settle."""
    value = "true" if kept else "false"
    pattern = re.compile(
        r"(?<![^\s,`'\"(])(?P<assign>[A-Z][A-Z0-9_]*=)?(?P<item>" + re.escape(key) + r"[=:]" + value + r")(?![\w.=:-])"
    )
    found = pattern.search(content)
    if found is None:
        return None
    start, end = found.start("item"), found.end("item")
    if content[end : end + 1] == "," and re.match(_ITEM, content[end + 1 :]):
        out = content[:start] + content[end + 1 :]
    elif found.group("assign") is None and start > 0 and content[start - 1] == ",":
        before = content[: start - 1]
        if not re.search(_ITEM + "$", before):
            return None
        out = before + content[end:]
    else:
        return None
    return None if key in out else out


def _lines_of(node: Node) -> list[tuple[int, int, str, str, str]]:
    """``(start, end, opener, content, closer)`` per line of a comment node, as
    byte offsets into the node's text."""
    data = node.text
    out = []
    at = 0
    for raw in data.split(b"\n"):
        line = raw.decode("utf-8", "replace")
        trailing_cr = line.endswith("\r")
        body = line[:-1] if trailing_cr else line
        opener = _OPENER.match(body)
        opening = opener.group(0) if opener else ""
        rest = body[len(opening) :]
        closer = _CLOSER.search(rest) if node.text.startswith(b"/*") else None
        closing = closer.group(0) if closer else ""
        content = rest[: len(rest) - len(closing)] if closing else rest
        out.append((at, at + len(raw), opening, content, closing))
        at += len(raw) + 1
    return out


def is_cut_line(line: str, texts: set[str]) -> bool:
    """Whether ``line`` is a comment line (or ends in a comment) whose words are
    exactly one of ``texts``, each as the marker leaves them."""
    return any(
        re.search(
            r"(?:^|\s)(?://+!?|/\*+!?|\*+(?!/)|#)[ \t]*" + re.escape(t) + r"[ \t]*(?:\*+/)?[ \t]*\r?$", line
        )
        for t in texts
        if t
    )


def _comment_text(node: Node) -> str:
    text = node.text.decode("utf-8", "replace")
    text = re.sub(r"^\s*(?://+|/\*+)\s*|\s*\*+/\s*$", "", text)
    return re.sub(r"\s*\n\s*(?:\*\s*)?", " ", text).strip()


def cut(
    source: str,
    grammar: str,
    key: str,
    *,
    treatment: bool,
    linked: dict[int, str] | None = None,
) -> tuple[str, list[Reword]]:
    """``source`` with the conjunct or list item cut out of each qualifying
    comment, and what changed. Without ``linked`` the comments tried are the
    ones naming ``key``. With it (``{1-based line: comment text}`` from the
    call-site report) they are the single comments starting on those lines
    whose whole text is the reported one, and only a conjunct is cut. ``source``
    itself when nothing qualifies or a code token would change."""
    bom = source.startswith("﻿")
    data = source[1:].encode("utf-8") if bom else source.encode("utf-8")
    if linked is None and key.encode() not in data:
        return source, []
    if linked is not None and not treatment:
        return source, []
    root = _parse(data, grammar)
    starts = _line_starts(data)
    names = stale_comments.names_pattern(key)
    source_lines = data.split(b"\n")

    def comment_only(row: int) -> bool:
        return 0 <= row < len(source_lines) and source_lines[row].lstrip().startswith((b"//", b"/*", b"*", b"#"))

    edits: list[tuple[int, int, bytes, Reword]] = []
    for node in _comments(root):
        row = node.start_point[0]
        if linked is not None:
            if linked.get(row + 1) != _comment_text(node):
                continue
        elif not names.search(node.text):
            continue
        lines = _lines_of(node)
        trailing = bool(source_lines[row][: node.start_point[1]].strip())
        for index, (a, b, opening, content, closing) in enumerate(lines):
            if not content.strip():
                continue
            line_bytes = source_lines[row + index] if row + index < len(source_lines) else b""
            if is_directive_line(line_bytes, content):
                continue
            new = None
            if treatment and (linked is not None or names.search(content.encode())):
                new = conjunct(content, key, linked=linked is not None)
                if new is not None and not _stands_alone(
                    node, lines, index, content, trailing, comment_only, row,
                    source_lines[row + 1].decode("utf-8", "replace") if row + 1 < len(source_lines) else "",
                ):
                    new = None
            if new is None and linked is None and names.search(content.encode()):
                new = list_item(content, key, kept=treatment)
            if new is None or new == content or not new.strip():
                continue
            start = node.start_byte + a + len(opening.encode())
            end = node.start_byte + a + len(opening.encode()) + len(content.encode())
            raw = tuple(
                bytes(source_lines[r]).decode("utf-8", "replace").strip()
                for r in range(row, row + len(lines))
            )
            edits.append((start, end, new.encode(), Reword(row + index + 1, content.strip(), new.strip(), raw)))
    if not edits:
        return source, []
    out, at = bytearray(), 0
    for start, end, replacement, _ in sorted(edits, key=lambda e: e[0]):
        out += data[at:start] + replacement
        at = end
    out += data[at:]
    after = bytes(out)
    try:
        if _code_tokens(_parse(after, grammar)) != _code_tokens(root):
            return source, []
    except UnicodeDecodeError:
        return source, []
    text = after.decode("utf-8")
    return ("﻿" + text if bom else text), [e[3] for e in sorted(edits, key=lambda e: e[0])]


def _continues(next_line: str) -> bool:
    """Whether the own-line comment right under a trailing one may be the rest
    of its sentence: any that opens with a letter, a digit or an operator, in
    any case. One opening with a quote or a bracket is a separate remark."""
    if not next_line.strip().startswith(("//", "/*", "*", "#")):
        return False
    words = _OPENER.sub("", next_line.strip(), count=1)
    return bool(words) and (words[0].isalnum() or words[0] in "+&|-,;)")


def _stands_alone(node, lines, index, content, trailing, comment_only, row, source_lines_next: str = "") -> bool:
    """Whether the sentence the conjunction ends is this line's own: it ends in
    a full stop or is the comment's last words, and it does not continue a
    sentence from the line above."""
    ends_sentence = content.rstrip().endswith((".", "!", "?"))
    last = all(not c.strip() for _a, _b, _o, c, _cl in lines[index + 1 :])
    starts_lead = True
    if index > 0:
        above = next((c for _a, _b, _o, c, _cl in reversed(lines[:index]) if c.strip()), None)
        starts_lead = above is None or above.rstrip().endswith((".", "!", "?", ":"))
    if node.text.startswith(b"/*"):
        return (ends_sentence or last) and starts_lead
    if trailing:
        # A sentence that wraps onto the next own-line comment is not whole.
        return not _continues(source_lines_next)
    below = comment_only(row + 1)
    above_line = comment_only(row - 1)
    return not above_line and not below


__all__ = ["conjunct", "cut", "list_item"]
