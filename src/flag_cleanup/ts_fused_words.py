"""Refuse a TypeScript-family rewrite that fused a word with its neighbour.

A rewrite that replaces punctuation with a word can land the word directly
against another one: ``return(<read>) ? 1 : 2`` folded to ``return1;``, and
``throw(<read>) || x`` to ``throwtrue``. The fused text is an ordinary name,
so the file still parses, and no parse-based check can tell it from code the
customer wrote. It is caught by what it spells instead: a name in the
rewritten file that the original never spelled anywhere, and that starts with
a keyword or a literal word, or ends with a literal word.

A name the original spelled, anywhere in its text, is never counted, so a
rewrite that only moves or keeps names cannot be refused here. The rewrite
writes new names in a few places on purpose (``Promise.resolve``,
``Record<string, never>``); none starts with a keyword, and one that did
would be refused, which is the safe direction.
"""

from __future__ import annotations

import re

from flag_cleanup.ts_syntax import _parse, _walk

_WORD = re.compile(r"[A-Za-z0-9_$]+")

#: Node kinds that hold a name.
_NAME_KINDS = frozenset(
    {
        "identifier",
        "property_identifier",
        "private_property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "type_identifier",
        "statement_identifier",
    }
)

#: The words a fused name can start with: reserved and contextual keywords
#: that can stand directly before an expression, and the literal words.
_PREFIXES = (
    "as", "async", "await", "case", "default", "delete", "do", "else", "export",
    "extends", "false", "in", "instanceof", "new", "null", "of", "return",
    "satisfies", "throw", "true", "typeof", "undefined", "void", "yield",
)

#: The words a fused name can end with: what a fold writes.
_SUFFIXES = ("true", "false", "null", "undefined")


def _fused(name: str) -> bool:
    return any(name.startswith(p) and len(name) > len(p) for p in _PREFIXES) or any(
        name.endswith(s) and len(name) > len(s) for s in _SUFFIXES
    )


def fused_names(original: str, rewritten: str, grammar: str) -> list[str]:
    """The names ``rewritten`` spells that look fused and that ``original``
    never spelled, sorted; empty when the texts are the same."""
    if original == rewritten:
        return []
    spelled = set(_WORD.findall(original))
    found = {
        node.text.decode("utf-8")
        for node in _walk(_parse(rewritten, grammar))
        if node.type in _NAME_KINDS
    }
    return sorted(name for name in found - spelled if _fused(name))
