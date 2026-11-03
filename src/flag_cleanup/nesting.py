"""Files too deeply nested to analyse, found after the interpreter ran out of stack.

A generated file can hold an expression nested a thousand levels deep: a long
string concatenation parses to a left-deep tree with one level per operator,
and a generator that writes a whole document as one constant produces exactly
that. Most passes here walk trees with an explicit stack, but a few recurse,
and Python's recursion limit is a hard ceiling for them.

Rather than rely on every pass staying iterative, :mod:`orchestrate` turns a
:class:`RecursionError` inside one flag's analysis into a refusal of that flag
alone. This module supplies what the refusal needs to be useful: the files that
are the likely cause, measured rather than guessed, each with its depth.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import piranha_runner, syntax, ts_syntax

logger = logging.getLogger(__name__)

#: A tree this deep is not hand-written code. Real source rarely passes sixty.
DEEP = 100

_TS_FAMILY = frozenset({"ts", "tsx", "js"})


def tree_depth(root: Node) -> int:
    """The depth of the deepest node under ``root``, counting ``root`` as 1.

    Walked with a cursor, never by recursion: the point is to measure a tree
    that recursion cannot survive.
    """
    cursor = root.walk()
    depth = deepest = 1
    while True:
        if cursor.goto_first_child():
            depth += 1
            deepest = max(deepest, depth)
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return deepest
            depth -= 1


def deep_files(
    directories: Iterable[str], languages: Iterable[str], flag_key: str
) -> list[tuple[str, int]]:
    """``(path, depth)`` for the files naming ``flag_key`` that are unusually deep.

    Deepest first. When none passes :data:`DEEP`, the single deepest file is
    returned instead, because a refusal that names nothing is the one outcome
    that cannot be acted on. Empty only when no file naming the key parsed.
    """
    measured: dict[str, int] = {}
    for directory in directories:
        for language in languages:
            try:
                spec = piranha_runner._language_spec(language)
            except ValueError:
                continue
            for path in piranha_runner._candidate_files(
                Path(directory), spec.extensions, flag_key, spec.excludes
            ):
                if path in measured:
                    continue
                grammar = piranha_runner._grammar_of(path, language)
                try:
                    source = Path(path).read_bytes()
                    parser = (
                        ts_syntax._parser(grammar)
                        if grammar in _TS_FAMILY
                        else syntax._parser(grammar)
                    )
                    measured[path] = tree_depth(parser.parse(source).root_node)
                except Exception:  # noqa: BLE001 - a file that will not parse cannot be measured
                    logger.debug("could not measure the nesting of %s", path, exc_info=True)
    ranked = sorted(measured.items(), key=lambda item: (-item[1], item[0]))
    deep = [item for item in ranked if item[1] >= DEEP]
    return deep or ranked[:1]
