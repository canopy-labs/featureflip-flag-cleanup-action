"""A view of newer Python source that this interpreter's ``ast`` can read.

The passes that prove a Python rewrite sound (the bound-read inliner first)
ask CPython itself, through ``ast`` and ``symtable``, and this Action runs on
3.12. A file written for 3.14 can hold syntax 3.12 rejects in a line that has
nothing to do with the flag, and one such line used to cost the whole file its
analysis:

* PEP 758: ``except ValueError, TypeError:`` without parentheses, which ruff
  writes for any project targeting 3.14;
* PEP 750: template strings, ``t"…"``.

:func:`view` rewrites exactly those, byte for byte the same length, so every
offset, line and column ``ast`` reports holds in the customer's source and an
edit computed against the view applies to the original unchanged:

* the second and later exceptions of an unparenthesised handler are blanked
  (``except ValueError         :``). Their names drop out of the analysis, so
  a name spelled only there is one the passes no longer see; each of them
  counts NAME tokens in the ORIGINAL and refuses when a count and the analysis
  disagree, so that is a refusal, never a wrong rewrite;
* a template string's ``t`` prefix becomes ``f``: the two share their
  grammar, and an f-string evaluates the same names a template does.

tree-sitter finds them, since its grammar is newer than this interpreter.
Anything else 3.12 cannot parse is left alone, and :func:`view` returns the
source unchanged, so the caller sees the parse error it always did.
"""

from __future__ import annotations

import ast

from tree_sitter import Node

from flag_cleanup import syntax


def view(source: str) -> str:
    """``source`` if this interpreter parses it, else the same-length view of it
    described in the module docstring when that parses, else ``source``."""
    try:
        ast.parse(source)
        return source
    except (SyntaxError, ValueError):
        pass
    data = bytearray(source.encode("utf-8"))
    for node in _walk(syntax._parse(source, "python")):
        if node.type == "except_clause":
            values = node.children_by_field_name("value")
            if len(values) > 1 and not any(child.type == "(" for child in node.children):
                _blank(data, values[0].end_byte, values[-1].end_byte)
        elif node.type == "string_start":
            prefix = bytes(data[node.start_byte : node.end_byte]).rstrip(b"'\"")
            index = prefix.lower().find(b"t")
            if index != -1:
                position = node.start_byte + index
                data[position : position + 1] = b"f" if prefix[index : index + 1] == b"t" else b"F"
    rewritten = data.decode("utf-8")
    try:
        ast.parse(rewritten)
    except (SyntaxError, ValueError):
        return source
    return rewritten


def _blank(data: bytearray, start: int, end: int) -> None:
    """Spaces over ``data[start:end]``, keeping every line break and the
    backslash continuing a line onto it."""
    for position in range(start, end):
        byte = data[position : position + 1]
        if byte in (b"\n", b"\r"):
            continue
        if byte == b"\\" and data[position + 1 : position + 2] in (b"\n", b"\r"):
            continue
        data[position : position + 1] = b" "


def _walk(root: Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)
