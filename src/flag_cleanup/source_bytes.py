"""The whole source a tree-sitter tree was parsed from, indexed like its nodes.

``root.text`` is NOT the file: the root node starts after a byte-order mark
and any leading whitespace, so ``root.text[node.start_byte]`` is off by that
many bytes in every grammar. Anything that cuts the source with a node's
``start_byte``/``end_byte`` takes its bytes from here instead. A use of
``root.text`` that takes no offsets (a search, a substring test) is marked
``# root-text-ok`` on its line; a test enforces that every other one goes
through :func:`of`.
"""

from __future__ import annotations

from tree_sitter import Node


def of(node: Node) -> bytes:
    """The source ``node``'s tree was parsed from, up to the end of the root
    node. The bytes before the root (a byte-order mark, leading whitespace)
    are spaces, which keeps every offset the nodes report valid."""
    root = node
    while root.parent is not None:
        root = root.parent
    return b" " * root.start_byte + root.text  # root-text-ok: the one place that pads it
