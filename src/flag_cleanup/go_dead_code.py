"""A Go label the fold left with nothing to jump to it.

``outer: for … { … continue outer … }`` whose last ``continue outer`` sat in a
branch the fold removed leaves ``outer:`` naming nothing, and Go rejects that
outright: ``label outer defined and not used`` is a compile error, not a vet
finding. The engine's Go cascade has no rule for it, so the label goes here:
the label and its colon, while the statement it labelled stays.

A Go label is scoped to its whole function body, not to the statement it
labels: ``goto`` may jump to it from anywhere in that body, ``break`` and
``continue`` from inside the statement. So a label counts as used when any
jump in its function names it. A function literal is a function of its own,
with labels of its own, and is not looked into.

DIFFERENTIAL, like every pass here: nothing is touched when the input already
had an unused label, because then the fold's cannot be told from the
customer's. Not listed in the pull request body: a label nothing jumps to is
nothing the customer could want kept.
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup.syntax import _parse, _walk

_FUNCTIONS = frozenset({"function_declaration", "method_declaration", "func_literal"})
_JUMPS = frozenset({"break_statement", "continue_statement", "goto_statement"})


def remove_orphans(before: str, after: str) -> str:
    """Drop each label the fold and the passes before this left unused."""
    if after == before or _unused_labels(before):
        return after
    data = after.encode("utf-8")
    for node in sorted(_unused_labels(after), key=lambda n: -n.start_byte):
        colon = next((c for c in node.children if c.type == ":"), None)
        if colon is None:  # pragma: no cover - `label:` always carries one
            continue
        start, end = node.start_byte, colon.end_byte
        line_start = data.rfind(b"\n", 0, start) + 1
        newline = data.find(b"\n", end)
        line_end = len(data) if newline == -1 else newline + 1
        if not data[line_start:start].strip() and not data[end:line_end].strip():
            # gofmt puts a label on its own line, one level out from the
            # statement it labels, so the whole line goes and the statement
            # keeps its own indentation.
            start, end = line_start, line_end
        else:
            while end < len(data) and data[end : end + 1] in (b" ", b"\t"):
                end += 1
        data = data[:start] + data[end:]
    return data.decode("utf-8")


def _unused_labels(source: str) -> list[Node]:
    """``labeled_statement`` nodes whose label no jump in their function names."""
    unused: list[Node] = []
    for node in _walk(_parse(source, "go")):
        if node.type != "labeled_statement":
            continue
        label = node.child_by_field_name("label")
        function = node.parent
        while function is not None and function.type not in _FUNCTIONS:
            function = function.parent
        if label is None or function is None:
            continue
        if not any(
            any(c.type == "label_name" and c.text == label.text for c in jump.children)
            for jump in _jumps(function)
        ):
            unused.append(node)
    return unused


def _jumps(function: Node):
    """Every jump in ``function``'s own body: not in a function literal inside it."""
    stack = list(function.children)
    while stack:
        node = stack.pop()
        if node.type in _FUNCTIONS:
            continue
        if node.type in _JUMPS:
            yield node
        stack.extend(node.children)
