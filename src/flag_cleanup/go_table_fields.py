"""Go: the table field a removed test setter's guard was the last reader of.

``if tt.enable { featureflag.Set(…) }`` goes with its setter
(:mod:`flag_cleanup.go_defaults`). The ``enable bool`` field of the table's
anonymous struct, and every row's ``enable: true``, then read nothing. Go does
not reject an unused field, but the table keeps a column nobody reads.

:func:`drop` removes it, and only when it can show that nothing reads it and
that the result is what ``gofmt`` would write. The Action has no ``gofmt`` at
run time, so the layout is a model, and the model is checked against the file
before it is trusted:

* the field is declared once, ``bool``, untagged, on a line of its own, in a
  ``[]struct{…}{…}`` literal the function ranges over (a literal, or a local
  declared once and mentioned only in the ``range``);
* no selector anywhere in the function spells the field, no identifier does
  except as a row key, and the loop variable (or a ``tt := tt`` copy of it)
  appears only as the operand of a selector, so it is never handed on whole;
* every row is keyed, and the field's value in each is ``true`` or ``false``;
* no comment sits in the struct or the rows except on a line that is deleted;
* ``gofmt`` aligns the types of consecutive field lines and the values of
  consecutive ``key: value`` lines to one column (a blank line, a comment or a
  multi-line element ends the run; keys over 40 characters change the rule, so
  they refuse). The column of every run the removal touches is computed from
  the lines that remain, and the edit is made only where the file already had
  the column the model predicts for the lines as they were: a file that was not
  ``gofmt``-formatted, or a shape the model gets wrong, keeps its bytes.

A refusal leaves the field exactly as it was. Nothing here is reported as a
removal: the table the pull request already names is the evidence.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable

from tree_sitter import Node

from flag_cleanup import go_defaults
from flag_cleanup.syntax import _parse, _walk

_KEY_LIMIT = 40  # gofmt's smallSize: past it the alignment rule is a ratio test


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


class _Lines:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.starts = [0] + [i + 1 for i, b in enumerate(data) if b == 10]

    def of(self, byte: int) -> int:
        return bisect.bisect_right(self.starts, byte) - 1

    def start(self, line: int) -> int:
        return self.starts[line]

    def end(self, line: int) -> int:
        """The byte of the newline ending ``line`` (or the end of the data)."""
        return self.starts[line + 1] - 1 if line + 1 < len(self.starts) else len(self.data)

    def text(self, line: int) -> bytes:
        return self.data[self.start(line) : self.end(line)]

    def blank(self, line: int) -> bool:
        return not self.text(line).strip()


def drop(source: str, candidates: Iterable[tuple[str, str]]) -> str:
    """``source`` without the table fields named by ``candidates``
    (``(function name, field)``) that are provably unread, ``source`` itself
    when none is."""
    if "\r" in source:
        return source
    result = source
    candidates = sorted(set(candidates))
    for function_name, field in candidates:
        edited = _drop_one(result, function_name, field)
        if edited is not None:
            result = edited
    # After the fields: the loop variable the deleted guard last read. Doing
    # it first would hide the loop from the field proof.
    for function_name in sorted({name for name, _ in candidates}):
        edited = _drop_unread_loop_variables(result, function_name)
        if edited is not None:
            result = edited
    return result


def _drop_unread_loop_variables(source: str, function_name: str) -> str | None:
    """``for _, tc := range tests {`` where the deleted guard was the last
    reader of ``tc`` does not compile (``declared and not used``). Rewrite it
    to ``for range tests {`` (``for i := range`` when the index is read)."""
    root = _parse(source, "go")
    if root.has_error:
        return None
    data = source.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    for function in _walk(root):
        if function.type not in ("function_declaration", "method_declaration"):
            continue
        name = function.child_by_field_name("name")
        if name is None or _text(name) != function_name:
            continue
        for loop in _walk(function):
            if loop.type != "for_statement":
                continue
            clause = next((c for c in loop.named_children if c.type == "range_clause"), None)
            body = loop.child_by_field_name("body")
            if clause is None or body is None:
                continue
            left, right = clause.child_by_field_name("left"), clause.child_by_field_name("right")
            if left is None or right is None or left.type != "expression_list" or len(left.named_children) != 2:
                continue
            if b":=" not in data[left.end_byte : right.start_byte]:
                continue
            index, value = left.named_children
            if value.type != "identifier" or _text(value) == "_" or index.type != "identifier":
                continue
            mentioned = {_text(n) for n in _walk(body) if n.type == "identifier"}
            if _text(value) in mentioned:
                continue
            if _text(index) != "_" and _text(index) in mentioned:
                text = f"{_text(index)} := range ".encode()
            elif _text(index) == "_":
                text = b"range "
            else:
                continue  # an unused index was never legal either: not this run's doing
            edits.append((left.start_byte, right.start_byte, text))
    if not edits:
        return None
    out = data
    for start, end, text in sorted(edits, reverse=True):
        out = out[:start] + text + out[end:]
    edited = out.decode("utf-8")
    return None if _parse(edited, "go").has_error else edited


def _drop_one(source: str, function_name: str, field: str) -> str | None:
    root = _parse(source, "go")
    if root.has_error:
        return None
    data = source.encode("utf-8")
    lines = _Lines(data)
    edits: list[tuple[int, int, bytes]] = []
    for function in _walk(root):
        if function.type not in ("function_declaration", "method_declaration"):
            continue
        name = function.child_by_field_name("name")
        if name is None or _text(name) != function_name:
            continue
        found = _function_edits(function, field, data, lines)
        if found:
            edits.extend(found)
    if not edits:
        return None
    edits.sort(key=lambda e: e[0])
    for (_, end, _), (start, _, _) in zip(edits, edits[1:]):
        if start < end:
            return None
    out = data
    for start, end, text in reversed(edits):
        out = out[:start] + text + out[end:]
    edited = out.decode("utf-8")
    after = _parse(edited, "go")
    if after.has_error:
        return None
    return edited


def _is_key(node: Node) -> bool:
    parent = node.parent
    if parent is None or parent.type != "literal_element":
        return False
    pair = parent.parent
    return pair is not None and pair.type == "keyed_element" and pair.named_children[:1] == [parent]


def _function_edits(function: Node, field: str, data: bytes, lines: _Lines) -> list[tuple[int, int, bytes]] | None:
    for node in _walk(function):
        if node.type == "selector_expression":
            member = node.child_by_field_name("field")
            if member is not None and _text(member) == field:
                return None
        if node.type == "identifier" and _text(node) == field and not _is_key(node):
            return None
    edits: list[tuple[int, int, bytes]] = []
    seen: set[int] = set()
    for loop in _walk(function):
        if loop.type != "for_statement":
            continue
        clause = next((c for c in loop.named_children if c.type == "range_clause"), None)
        left = clause.child_by_field_name("left") if clause is not None else None
        if left is None:
            continue
        variable = _text(left.named_children[-1]) if left.type == "expression_list" else _text(left)
        table = go_defaults._table(loop, variable)
        if table is None:
            continue
        declared = go_defaults._fields(table)
        if declared is None or declared.get(field) != "bool":
            continue
        if not _loop_only_selects(loop, clause, variable):
            return None
        if table.id in seen:
            continue
        seen.add(table.id)
        found = _table_edits(table, field, data, lines)
        if found is None:
            return None
        edits.extend(found)
    # A table the function declares but no loop proved is left alone, and so
    # is a table whose loop was refused above.
    return edits


def _loop_only_selects(loop: Node, clause: Node, variable: str) -> bool:
    """Whether the loop variable (and a ``tt := tt`` copy of it) is only ever
    the operand of a selector, so no row is handed on whole."""
    names = {variable}
    body = loop.child_by_field_name("body")
    if body is None:
        return False
    changed = True
    while changed:
        changed = False
        for node in _walk(body):
            if node.type != "short_var_declaration":
                continue
            lhs, rhs = node.child_by_field_name("left"), node.child_by_field_name("right")
            if lhs is None or rhs is None or len(lhs.named_children) != 1 or len(rhs.named_children) != 1:
                continue
            if rhs.named_children[0].type == "identifier" and _text(rhs.named_children[0]) in names:
                new = _text(lhs.named_children[0])
                if new not in names:
                    names.add(new)
                    changed = True
    for node in _walk(body):
        if node.type != "identifier" or _text(node) not in names:
            continue
        parent = node.parent
        if parent is not None and parent.type == "selector_expression" and parent.child_by_field_name("operand") == node:
            continue
        if parent is not None and parent.type == "expression_list" and parent.parent is not None:
            declaration = parent.parent
            if declaration.type == "short_var_declaration" and declaration.child_by_field_name("left") == parent:
                continue
        if parent is not None and parent.type == "expression_list" and parent.parent is not None:
            declaration = parent.parent
            if declaration.type == "short_var_declaration" and declaration.child_by_field_name("right") == parent:
                continue
        return False
    return True


def _table_edits(table: Node, field: str, data: bytes, lines: _Lines) -> list[tuple[int, int, bytes]] | None:
    kind = table.child_by_field_name("type")
    element = kind.child_by_field_name("element") if kind is not None else None
    literal = next((c for c in table.named_children if c.type == "literal_value"), None)
    if element is None or literal is None:
        return None
    rows = list(literal.named_children)
    deleted_lines: set[int] = set()
    edits: list[tuple[int, int, bytes]] = []

    # --- the struct type ---
    declaration_list = next((c for c in element.named_children if c.type == "field_declaration_list"), None)
    if declaration_list is None:
        return None
    members = list(declaration_list.named_children)
    if any(m.type not in ("field_declaration", "comment") for m in members):
        return None
    fields = [m for m in members if m.type == "field_declaration"]
    target = None
    infos: list[tuple[Node, str, Node]] = []
    for member in fields:
        names = member.children_by_field_name("name")
        type_ = member.child_by_field_name("type")
        if len(names) != 1 or type_ is None or member.child_by_field_name("tag") is not None:
            return None
        if lines.of(member.start_byte) != lines.of(member.end_byte):
            return None
        infos.append((member, _text(names[0]), type_))
        if _text(names[0]) == field:
            target = member
    if target is None or len(fields) < 2:
        return None
    target_line = lines.of(target.start_byte)
    deleted_lines.add(target_line)
    struct_lines = [lines.of(m.start_byte) for m in fields]
    remaining_struct = [(m, n, t) for m, n, t in infos if m != target]
    pattern_for = lambda name, type_: re.compile(  # noqa: E731
        rb"^([ \t]*)" + re.escape(name.encode()) + rb"( +)" + re.escape(type_.text) + rb"([ \t]*//.*)?$"
    )
    gaps: dict[int, tuple[int, int]] = {}  # line -> (gap start, gap end)
    for member, name, type_ in infos:
        line = lines.of(member.start_byte)
        match = pattern_for(name, type_).match(lines.text(line))
        if match is None:
            return None
        if match.group(3) is not None and member != target:
            return None
        gaps[line] = (lines.start(line) + match.start(2), lines.start(line) + match.end(2))
    if any(c.type == "comment" and lines.of(c.start_byte) != target_line for c in members):
        return None
    for run in _runs([(lines.of(m.start_byte), n) for m, n, _ in infos]):
        width = max(len(n) for _, n in run)
        for line, name in run:
            start, end = gaps[line]
            if end - start != width - len(name) + 1:
                return None  # the file does not already have gofmt's columns
    if not _removable(lines, target_line, declaration_list):
        return None
    for run in _runs([(lines.of(m.start_byte), n) for m, n, _ in infos]):
        run = [(line, name) for line, name in run if line != target_line]
        if not run:
            continue
        width = max(len(n) for _, n in run)
        for line, name in run:
            want = 1 if len(remaining_struct) == 1 else width - len(name) + 1
            start, end = gaps[line]
            if end - start != want:
                edits.append((start, end, b" " * want))
    edits.append((lines.start(target_line), lines.start(target_line + 1) if target_line + 1 < len(lines.starts) else len(data), b""))
    del struct_lines

    # --- the rows ---
    any_row = False
    for row in rows:
        values = go_defaults._row_values(row)
        if values is None:
            return None
        if field not in values:
            if _row_needs_check(row, lines):
                pass
            continue
        if values[field].type not in ("true", "false"):
            return None
        any_row = True
        found = _row_edits(row, field, data, lines, deleted_lines)
        if found is None:
            return None
        edits.extend(found)
    del any_row
    for comment in (n for n in _walk(literal) if n.type == "comment"):
        if lines.of(comment.start_byte) not in deleted_lines:
            return None
    return edits


def _row_needs_check(row: Node, lines: _Lines) -> bool:
    return lines.of(row.start_byte) != lines.of(row.end_byte)


def _runs(items: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Consecutive-line runs of ``(line, key)``."""
    runs: list[list[tuple[int, str]]] = []
    for item in items:
        if runs and runs[-1][-1][0] + 1 == item[0]:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _removable(lines: _Lines, line: int, container: Node) -> bool:
    """Deleting ``line`` leaves neither an empty body nor a stray blank line."""
    before = lines.text(line - 1).strip()
    after = lines.text(line + 1).strip()
    opens = before == b"" or before.endswith(b"{")
    closes = after == b"" or after.startswith(b"}")
    return not (opens and closes)


def _row_edits(row: Node, field: str, data: bytes, lines: _Lines, deleted: set[int]) -> list[tuple[int, int, bytes]] | None:
    literal = row.named_children[0]
    elements = list(literal.named_children)
    if any(e.type not in ("keyed_element", "comment") for e in elements):
        return None
    pairs = [e for e in elements if e.type == "keyed_element"]
    target = next((p for p in pairs if _text(p.named_children[0]) == field), None)
    if target is None or len(pairs) != len(elements):
        return None
    first, last = lines.of(literal.start_byte), lines.of(literal.end_byte - 1)
    if first == last:
        index = pairs.index(target)
        if len(pairs) == 1:
            return [(target.start_byte, target.end_byte, b"")]
        if index < len(pairs) - 1:
            return [(target.start_byte, pairs[index + 1].start_byte, b"")]
        return [(pairs[index - 1].end_byte, target.end_byte, b"")]
    # multi-line row: `{` ends its line, one element per line, `}` alone
    if lines.data[literal.start_byte + 1 : lines.end(first)].strip():
        return None
    if lines.text(last)[: literal.end_byte - 1 - lines.start(last)].strip():
        return None
    entries: list[tuple[int, str, Node]] = []
    for pair in pairs:
        line = lines.of(pair.start_byte)
        if lines.data[lines.start(line) : pair.start_byte].strip():
            return None
        end_line = lines.of(pair.end_byte - 1)
        if lines.data[pair.end_byte : lines.end(end_line)].strip() != b",":
            return None
        key = pair.named_children[0]
        if key.type != "literal_element" or len(_text(key)) > _KEY_LIMIT:
            return None
        entries.append((line, _text(key), pair))
    target_line = lines.of(target.start_byte)
    if lines.of(target.end_byte - 1) != target_line:
        return None
    deleted.add(target_line)
    prefix = {}
    for line, key, pair in entries:
        match = re.match(rb"^([ \t]*)" + re.escape(key.encode()) + rb":( +)", lines.text(line))
        if match is None:
            return None
        prefix[line] = (lines.start(line) + match.start(2), lines.start(line) + match.end(2))
    single = [(line, key) for line, key, pair in entries if lines.of(pair.end_byte - 1) == line]
    breaks = {line for line, _, pair in entries if lines.of(pair.end_byte - 1) != line}
    sections = [[(line, key) for line, key in run if line not in breaks] for run in _runs([(l, k) for l, k in single])]
    sections = [s for s in sections if s]

    def expected(section: list[tuple[int, str]], line: int, key: str) -> int:
        return max(len(k) for _, k in section) - len(key) + 1

    for section in sections:
        for line, key in section:
            start, end = prefix[line]
            if end - start != expected(section, line, key):
                return None
    for line, key, pair in entries:
        if line in breaks:
            start, end = prefix[line]
            if end - start != 1:
                return None
    if not _removable(lines, target_line, literal):
        return None
    edits: list[tuple[int, int, bytes]] = [(lines.start(target_line), lines.start(target_line + 1), b"")]
    for section in sections:
        remaining = [(line, key) for line, key in section if line != target_line]
        for line, key in remaining:
            want = expected(remaining, line, key)
            start, end = prefix[line]
            if end - start != want:
                edits.append((start, end, b" " * want))
    return edits
