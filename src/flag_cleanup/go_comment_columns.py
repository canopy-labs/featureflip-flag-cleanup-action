"""Trailing comments on consecutive Go statements, kept in gofmt's column.

``gofmt`` lines up the ``//`` comments that trail consecutive one-line
statements: the column is the longest statement of the run plus one space. A
run is broken by a blank line, a line with no trailing comment, a comment on a
line of its own or a statement of another indentation. Delete a statement from
the run, or put a line without a comment into it, and the rest keep a column
sized for what is no longer there. The file builds and vets, and ``gofmt -l``
lists it.

:func:`realign` takes the file as it was and as the run left it. It rewrites
the gaps of the runs the run touched, and no others, so code the run did not
edit is never reformatted. It does so only where the original already had
gofmt's gaps for the run it touches: a file that was not gofmt-formatted, or a
shape this model gets wrong (the line was part of a struct, a ``const`` group
or a statement that spans lines), keeps its bytes. A gap this model cannot
describe therefore costs a withheld realignment, never a wrong one.

Every edit is made to whitespace between a statement and its comment.
"""

from __future__ import annotations

import difflib
import re

from tree_sitter import Node

from flag_cleanup.syntax import _parse, _walk

_BLOCKS = frozenset({"block", "statement_list"})
#: Declarations lay their parts out in cells of their own (name, type, value,
#: comment), so a run of them is not a run of statements.
_DECLARATIONS = frozenset({"var_declaration", "const_declaration", "type_declaration"})
_EOL = re.compile(r"\r?\n")


class _Line:
    """A statement alone on its line with a ``//`` comment trailing it."""

    __slots__ = ("row", "indent", "code", "gap", "comment")

    def __init__(self, row: int, indent: str, code: str, gap: int, comment: str) -> None:
        self.row, self.indent, self.code, self.gap, self.comment = row, indent, code, gap, comment


def realign(before: str, after: str) -> str:
    """``after``, with the trailing-comment column of each run it touched put
    where ``gofmt`` puts it."""
    if before == after:
        return after
    old_lines = _split(before)
    new_lines = _split(after)
    old_runs = _runs(before, old_lines)
    new_runs = _runs(after, new_lines)
    if not new_runs:
        return after
    old_run_of = {line.row: index for index, run in enumerate(old_runs) for line in run}
    consistent = [_is_gofmt(run) for run in old_runs]
    new_run_of = {line.row: index for index, run in enumerate(new_runs) for line in run}
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    # run index -> every old run the edits around it came from
    touched: dict[int, set[int]] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        news = {new_run_of[row] for row in range(j1 - 1, j2 + 1) if row in new_run_of}
        olds = {old_run_of[row] for row in range(i1 - 1, i2 + 1) if row in old_run_of}
        for index in news:
            touched.setdefault(index, set()).update(olds)
    # The lines kept as they were come from runs of their own.
    mapping = _equal_rows(matcher)
    for index in touched:
        for line in new_runs[index]:
            source = mapping.get(line.row)
            if source is not None and source in old_run_of:
                touched[index].add(old_run_of[source])
    result = list(new_lines)
    for index, sources in touched.items():
        run = new_runs[index]
        if not sources or not all(consistent[source] for source in sources):
            continue
        if any("\t" in line.code for line in run):
            continue
        column = max(_width(line.code) for line in run) + 1
        for line in run:
            text, ending = result[line.row]
            gap = column - _width(line.code)
            result[line.row] = (line.indent + line.code + " " * gap + line.comment, ending)
    return "".join(text + ending for text, ending in result)


def _split(source: str) -> list[tuple[str, str]]:
    """``(text, line ending)`` for each line."""
    lines: list[tuple[str, str]] = []
    position = 0
    for match in _EOL.finditer(source):
        lines.append((source[position : match.start()], match.group()))
        position = match.end()
    if position < len(source):
        lines.append((source[position:], ""))
    return lines


def _equal_rows(matcher: difflib.SequenceMatcher) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            mapping.update((j1 + k, i1 + k) for k in range(i2 - i1))
    return mapping


def _width(code: str) -> int:
    return len(code)


def _is_gofmt(run: list[_Line]) -> bool:
    """The run's gaps are the ones ``gofmt`` writes."""
    if any("\t" in line.code for line in run):
        return False
    column = max(_width(line.code) for line in run) + 1
    return all(line.gap == column - _width(line.code) for line in run)


def _runs(source: str, lines: list[tuple[str, str]]) -> list[list[_Line]]:
    """Maximal groups of consecutive, equally indented :class:`_Line`."""
    found = _trailing_lines(source, lines)
    runs: list[list[_Line]] = []
    for line in found:
        if runs and runs[-1][-1].row + 1 == line.row and runs[-1][-1].indent == line.indent:
            runs[-1].append(line)
        else:
            runs.append([line])
    return runs


def _trailing_lines(source: str, lines: list[tuple[str, str]]) -> list[_Line]:
    data = source.encode("utf-8")
    root = _parse(source, "go")
    found: dict[int, _Line] = {}
    rows_of_comments: dict[int, int] = {}
    for node in _walk(root):
        if node.type == "comment":
            rows_of_comments[node.start_point[0]] = rows_of_comments.get(node.start_point[0], 0) + 1
    for node in _walk(root):
        if node.type not in _BLOCKS:
            continue
        for child in node.children:
            comment = child.next_sibling
            if (
                not child.is_named
                or child.type == "comment"
                or child.type in _DECLARATIONS
                or comment is None
                or comment.type != "comment"
                or not _one_line(child, comment, data)
            ):
                continue
            row = child.start_point[0]
            if rows_of_comments.get(row) != 1 or row >= len(lines):
                continue
            text = lines[row][0]
            start = _column(data, child.start_byte)
            code_end = _column(data, child.end_byte)
            comment_start = _column(data, comment.start_byte)
            if not text.startswith("//", comment_start):
                continue
            indent = text[:start]
            between = text[code_end:comment_start]
            if indent.strip(" \t") or between.strip(" ") or not between:
                continue
            found[row] = _Line(row, indent, text[start:code_end], len(between), text[comment_start:])
    return [found[row] for row in sorted(found)]


def _one_line(statement: Node, comment: Node, data: bytes) -> bool:
    """``statement`` and ``comment`` share a line, with nothing else on it."""
    if statement.start_point[0] != statement.end_point[0]:
        return False
    if comment.start_point[0] != statement.end_point[0] or comment.end_point[0] != comment.start_point[0]:
        return False
    line_start = data.rfind(b"\n", 0, statement.start_byte) + 1
    return not data[line_start : statement.start_byte].strip(b" \t")


def _column(data: bytes, byte: int) -> int:
    """The character offset within its line of the byte offset ``byte``."""
    line_start = data.rfind(b"\n", 0, byte) + 1
    return len(data[line_start:byte].decode("utf-8"))
