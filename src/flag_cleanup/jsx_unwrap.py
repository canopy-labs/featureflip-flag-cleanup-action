"""Replace JSX elements by their children, through the engine.

A pass that has proven an element renders nothing but its children (a gate on
a removed flag, an emptied test provider) does not write the replacement
itself. It marks the element's two tag names and the engine's marked-element
rules (``rules/tsx_ctor_param.toml``) make the edit:

* one child element, which the engine puts in the element's place and
  re-indents (``unwrap_ctor_marked_element``);
* anything else (several children, text, an ``{expression}``): the opening
  tag becomes ``<>`` and the closing tag ``</>``, so the children stay where
  they are (``unwrap_ctor_marked_fragment_*``).

A lone child that would not move out exactly (a multi-line string inside it,
a line indented less than the shift) takes the fragment too, as the text
splice did, since a fragment needs no re-indenting.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable

from tree_sitter import Node

from flag_cleanup.ctor_param_model import choose_sentinel, mark, render_rules
from flag_cleanup.ts_syntax import _parse

#: ``engine(paths, rules_text)``: runs the rendered rules over ``paths`` in place.
Engine = Callable[[list[str], str], object]

_ELEMENTS = ("jsx_element", "jsx_self_closing_element")


def _children(element: Node, data: bytes) -> list[Node]:
    open_tag = element.child_by_field_name("open_tag")
    close_tag = element.child_by_field_name("close_tag")
    return [
        c
        for c in element.named_children
        if c.id not in (open_tag.id, close_tag.id)
        and not (c.type == "jsx_text" and not data[c.start_byte : c.end_byte].strip())
    ]


def has_key(tag: Node, data: bytes) -> bool:
    """Whether the opening tag carries a ``key`` prop."""
    for attribute in tag.children_by_field_name("attribute"):
        if attribute.type == "jsx_attribute" and attribute.named_children:
            name = attribute.named_children[0]
            if data[name.start_byte : name.end_byte] == b"key":
                return True
    return False


def _bare_cr(text: bytes) -> bool:
    """Whether ``text`` has a carriage return that is not part of a CRLF."""
    return re.search(rb"\r(?!\n)", text) is not None


def marks(element: Node, data: bytes) -> list[tuple[int, int, str]] | None:
    """The two tag-name spans that unwrap ``element``, each with its mark form;
    ``None`` for anything but a ``jsx_element`` with a name on both tags, and
    for one with a ``key`` prop: the key tells React which list item this is,
    and the element that replaces it would not carry it."""
    if element.type != "jsx_element":
        return None
    open_tag = element.child_by_field_name("open_tag")
    close_tag = element.child_by_field_name("close_tag")
    open_name = open_tag.child_by_field_name("name") if open_tag is not None else None
    close_name = close_tag.child_by_field_name("name") if close_tag is not None else None
    if open_name is None or close_name is None or has_key(open_tag, data):
        return None
    # Imported late: `ts_gates` imports this module.
    from flag_cleanup.ts_gates import _dedented

    children = _children(element, data)
    lone = len(children) == 1 and children[0].type in _ELEMENTS
    if lone and _bare_cr(data[children[0].start_byte : children[0].end_byte]):
        # The engine re-indents on "\n" only; a fragment needs no re-indenting.
        lone = False
    form = "argument" if lone and _dedented(children[0], element, data) is not None else "fragment"
    return [
        (open_name.start_byte, open_name.end_byte, form),
        (close_name.start_byte, close_name.end_byte, form),
    ]


def unwrap(
    path: str, source: str, spans: Iterable[tuple[int, int]], grammar: str, engine: Engine
) -> str | None:
    """``source`` with each element spanning one of ``spans`` replaced by its
    children, or ``None`` when an element cannot be marked or the engine's
    result is not clean (a mark left, a parse error). ``path`` holds
    ``source`` on return either way; the caller writes the result."""
    data = source.encode("utf-8")
    wanted = set(spans)
    found: list[tuple[int, int, str]] = []
    stack = [_parse(source, grammar)]
    while stack:
        node = stack.pop()
        if (node.start_byte, node.end_byte) in wanted:
            spans_of = marks(node, data)
            if spans_of is None:
                return None
            found += spans_of
            wanted.discard((node.start_byte, node.end_byte))
        stack.extend(node.children)
    if wanted or not found:
        return None
    sentinel = choose_sentinel([data])
    if sentinel is None:
        return None
    file = Path(path)
    try:
        file.write_bytes(mark(data, found, sentinel))
        engine([path], render_rules("tsx", [], sentinel))
        # Bytes, not `read_text`: universal newlines would turn every CRLF
        # (or lone CR) of the customer's file into LF.
        after = file.read_bytes().decode("utf-8")
    except Exception:  # noqa: BLE001 - any surprise, the engine's refusal included, means: no unwrap
        file.write_bytes(data)
        return None
    file.write_bytes(data)
    if sentinel in after or _parse(after, grammar).has_error:
        return None
    return after
