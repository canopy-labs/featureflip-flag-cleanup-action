"""One iterative tree walk, for passes that must survive a deeply nested file.

A recursive generator over a syntax tree needs a stack frame per level, and a
generated file can nest a thousand levels deep (a long string concatenation
parses to a left-deep tree with one level per operator). Python's recursion
limit then ends the run. Passes that visit every node of a file use this
instead.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node


def preorder(node: Node) -> Iterator[Node]:
    """``node`` and every node below it: parents first, siblings in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))
