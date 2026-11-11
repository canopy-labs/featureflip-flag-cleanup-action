"""Go: a flag whose registry default is the REMOVED branch.

An in-house catalog (``"host-port": {Default: false}``) says what a test that
never sets the flag gets. When that is the branch a removal deletes, three
things follow in the tests, and this module is the Go half of what the
TypeScript family does in :mod:`flag_cleanup.ts_tests`:

* the catalog entry is read (:func:`registry_defaults`) so the pull request can
  say every test that never set the flag ran the removed branch;
* deleting a setter (:mod:`flag_cleanup.go_setters`) can empty the ``if`` that
  guarded it (``if tt.enable { featureflag.Set(…) }``). An ``if`` whose whole
  body is deleted, whose condition is a name or member read (optionally
  negated), and that has neither an ``else`` nor an initializer, goes with it
  (:func:`settle`), up through any enclosing ``if`` that empties in turn;
* a table row that the guard leaves unset (its field is ``false`` or absent)
  ran with the default. When its title names the flag in the removed state, by
  the TypeScript family's rules, it is deleted; any other unset row is
  reported by name as a test that relied on the default.

Nothing here is a proof that a row's ``want`` describes the removed branch: the
title is what decides, exactly as for TypeScript, and the pull request says so.
The now-unused table field is left, because removing it means rewriting every
row and the struct's alignment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import ts_tests
from flag_cleanup.syntax import _parse, _walk

#: The row fields whose string says what the row is about.
_TITLE_FIELDS = ("name", "desc", "description", "title", "scenario", "testName", "label")

#: What a row deleted for its title is reported as (``Test/row``).
DEFAULT_ROW = "default row"
#: What an unset row that stayed is reported as.
RELIED_ON_DEFAULT = "relied on default"


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _string_value(node: Node) -> str | None:
    if node.type != "interpreted_string_literal":
        return None
    content = [c for c in node.children if c.type == "interpreted_string_literal_content"]
    if len(content) == 1 and all(c.type in ("\"", "interpreted_string_literal_content") for c in node.children):
        return _text(content[0])
    return None


def registry_defaults(source: str, flag_key: str) -> list[ts_tests.RegistryEntry]:
    """Each entry of a Go map literal keyed by ``flag_key`` whose value is a
    struct literal with a boolean ``Default``: ``"k": {Default: false}``.

    Test files are not asked (their copies are fixture data)."""
    root = _parse(source, "go")
    found = []
    for pair in _walk(root):
        if pair.type != "keyed_element" or len(pair.named_children) != 2:
            continue
        key, value = pair.named_children
        if key.type != "literal_element" or value.type != "literal_element" or len(key.named_children) != 1:
            continue
        if _string_value(key.named_children[0]) != flag_key or len(value.named_children) != 1:
            continue
        body = value.named_children[0]
        if body.type == "composite_literal":
            body = body.child_by_field_name("body") or next((c for c in body.named_children if c.type == "literal_value"), body)
        if body.type != "literal_value":
            continue
        for member in body.named_children:
            if member.type != "keyed_element" or len(member.named_children) != 2:
                continue
            name, literal = member.named_children
            inner = literal.named_children[0] if len(literal.named_children) == 1 else None
            if (
                name.named_children
                and _text(name.named_children[0]) == "Default"
                and inner is not None
                and inner.type in ("true", "false")
            ):
                holder = pair.parent
                siblings = frozenset(
                    v
                    for other in (holder.named_children if holder is not None else ())
                    if other.type == "keyed_element" and other.id != pair.id and other.named_children
                    and other.named_children[0].named_children
                    and (v := _string_value(other.named_children[0].named_children[0])) is not None
                )
                found.append(ts_tests.RegistryEntry(inner.type, " ".join(_text(pair).split()), siblings))
    return found


def _simple_condition(node: Node | None, *, negated_ok: bool = True) -> Node | None:
    """The name or member read ``node`` is, or ``None``. ``!x`` is one too when
    ``negated_ok``."""
    if node is None:
        return None
    if node.type == "parenthesized_expression" and len(node.named_children) == 1:
        return _simple_condition(node.named_children[0], negated_ok=negated_ok)
    if node.type == "unary_expression" and negated_ok and _text(node)[:1] == "!" and node.named_children:
        return _simple_condition(node.named_children[-1], negated_ok=False)
    if node.type == "identifier":
        return node
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        if operand is not None and _simple_condition(operand, negated_ok=False) is not None:
            return node
    return None


def _statements(block: Node | None) -> list[Node] | None:
    if block is None or block.type != "block":
        return None
    out: list[Node] = []
    for child in block.named_children:
        if child.type == "statement_list":
            out.extend(child.named_children)
        else:
            out.append(child)
    return out


def _bare_if(node: Node) -> bool:
    """An ``if`` that may go once its body is gone: a statement of its own
    (never the ``alternative`` of another ``if``, where deleting it would leave
    ``else`` to adopt the next statement), with no ``else`` or initializer, and
    a MEMBER read for a condition. A bare local (``if on {``) stays: deleting
    its only reader would strand the local."""
    condition = _simple_condition(node.child_by_field_name("condition"))
    return (
        node.type == "if_statement"
        and node.parent is not None
        and node.parent.type in ("statement_list", "block")
        and node.child_by_field_name("alternative") is None
        and node.child_by_field_name("initializer") is None
        and condition is not None
        and condition.type == "selector_expression"
    )


def _enclosing_function(node: Node) -> Node | None:
    while node is not None and node.type not in ("function_declaration", "method_declaration"):
        node = node.parent
    return node


@dataclass(frozen=True)
class Settled:
    #: ``(node, kind)`` lines to delete, the emptied ``if``s standing for what they held.
    lines: list[tuple[Node, str]]
    #: ``(row, "Test/title")`` to delete.
    rows: list[tuple[Node, str]]
    #: ``"Test/title"`` of each unset row that stays.
    relied: list[str]


def settle(
    root: Node,
    lines: list[tuple[Node, str]],
    setters: dict[tuple[int, int], bool],
    treatment: bool,
    flag_key: str,
    removed_default: ts_tests.FlagDefault | None,
) -> Settled:
    """Fold the ``if``s the deleted ``lines`` empty into them, and read the
    table rows the guards leave unset. ``removed_default`` is the run's
    registry default when it is the removed branch, else ``None``."""
    gone = {node.id for node, _ in lines}
    candidates: dict[int, Node] = {}
    for node, _ in lines:
        parent = node.parent
        while parent is not None:
            if _bare_if(parent):
                candidates[parent.id] = parent
            parent = parent.parent
    changed = True
    emptied: dict[int, Node] = {}
    while changed:
        changed = False
        for candidate in candidates.values():
            if candidate.id in emptied:
                continue
            body = _statements(candidate.child_by_field_name("consequence"))
            if body and all(s.id in gone for s in body):
                emptied[candidate.id] = candidate
                gone.add(candidate.id)
                changed = True
    outermost = [
        n for n in emptied.values()
        if not any(o.id != n.id and o.start_byte <= n.start_byte and n.end_byte <= o.end_byte for o in emptied.values())
    ]
    kept: list[tuple[Node, str]] = []
    for node, kind in lines:
        if any(o.start_byte <= node.start_byte and node.end_byte <= o.end_byte for o in outermost):
            continue
        kept.append((node, kind))
    for candidate in sorted(outermost, key=lambda n: n.start_byte):
        first = next(kind for node, kind in lines if candidate.start_byte <= node.start_byte < candidate.end_byte)
        kept.append((candidate, first))

    rows: list[tuple[Node, str]] = []
    relied: list[str] = []
    if removed_default is not None:
        rows, relied = _rows(root, list(emptied.values()), setters, treatment, flag_key, removed_default)
    return Settled(kept, rows, relied)


def _table(loop: Node, variable: str) -> Node | None:
    """The ``[]struct{…}{…}`` literal the range loop walks, when it is one
    literal (directly, or a local declared once and never touched again)."""
    clause = next((c for c in loop.named_children if c.type == "range_clause"), None)
    if clause is None:
        return None
    left = clause.child_by_field_name("left")
    right = clause.child_by_field_name("right")
    if left is None or right is None:
        return None
    names = [_text(c) for c in left.named_children] if left.type == "expression_list" else [_text(left)]
    if names[-1:] != [variable]:
        return None
    if right.type == "composite_literal":
        return right
    if right.type != "identifier":
        return None
    function = _enclosing_function(loop)
    if function is None:
        return None
    name = _text(right)
    found = None
    for node in _walk(function):
        if node.type == "identifier" and _text(node) == name and node.id != right.id:
            parent = node.parent
            declaration = parent.parent if parent is not None and parent.type == "expression_list" else None
            if (
                declaration is not None
                and declaration.type == "short_var_declaration"
                and declaration.child_by_field_name("left") is parent
                and len(parent.named_children) == 1
                and found is None
            ):
                value = declaration.child_by_field_name("right")
                if value is not None and len(value.named_children) == 1 and value.named_children[0].type == "composite_literal":
                    found = value.named_children[0]
                    continue
            return None  # any other mention: appended to, reassigned, passed on
    return found


def _fields(table: Node) -> dict[str, str] | None:
    kind = table.child_by_field_name("type")
    if kind is None or kind.type != "slice_type":
        return None
    element = kind.child_by_field_name("element")
    if element is None or element.type != "struct_type":
        return None
    found: dict[str, str] = {}
    for declaration in _walk(element):
        if declaration.type != "field_declaration":
            continue
        names = [_text(c) for c in declaration.children_by_field_name("name")]
        type_ = declaration.child_by_field_name("type")
        if type_ is None:
            return None
        for name in names:
            if name in found:
                return None
            found[name] = _text(type_)
    return found


def _row_values(row: Node) -> dict[str, Node] | None:
    if row.type != "literal_element" or len(row.named_children) != 1 or row.named_children[0].type != "literal_value":
        return None
    values: dict[str, Node] = {}
    for element in row.named_children[0].named_children:
        if element.type != "keyed_element" or len(element.named_children) != 2:
            return None
        key, value = element.named_children
        if key.type != "literal_element" or key.named_children[0].type != "identifier" or len(value.named_children) != 1:
            return None
        values[_text(key.named_children[0])] = value.named_children[0]
    return values


def _own_lines(row: Node) -> bool:
    """Whether ``row`` sits on lines of its own: nothing but its trailing
    comma and whitespace shares them."""
    source = _source_of(row)
    start = source.rfind(b"\n", 0, row.start_byte) + 1
    end = source.find(b"\n", row.end_byte)
    end = len(source) if end == -1 else end
    return not source[start : row.start_byte].strip() and source[row.end_byte : end].strip() in (b"", b",")


def _source_of(node: Node) -> bytes:
    root = node
    while root.parent is not None:
        root = root.parent
    return root.text


def _rows(
    root: Node,
    emptied: list[Node],
    setters: dict[tuple[int, int], bool],
    treatment: bool,
    flag_key: str,
    default: ts_tests.FlagDefault,
) -> tuple[list[tuple[Node, str]], list[str]]:
    untreated = "false" if treatment else "true"
    # The title must contain the flag's LITERAL key beside the removed-state
    # word (case and `-`/`_`/space aside). Nothing weaker deletes a row: the
    # key constant's name ("Proxy", "Enabled") or a single word of the key
    # ("port") is common English, so such a row is only listed as relying on
    # the default.
    keyed = _key_pattern(flag_key, untreated)
    guards: dict[int, tuple[Node, str, set[str]]] = {}
    for candidate in emptied:
        condition = candidate.child_by_field_name("condition")
        if condition is None or condition.type != "selector_expression":
            continue
        body = _statements(candidate.child_by_field_name("consequence")) or []
        if not all(setters.get((s.start_byte, s.end_byte)) is treatment for s in body):
            continue
        operand = condition.child_by_field_name("operand")
        field = condition.child_by_field_name("field")
        if operand is None or field is None or operand.type != "identifier":
            continue
        loop = candidate.parent
        while loop is not None and not (loop.type == "for_statement" and _table(loop, _text(operand)) is not None):
            loop = loop.parent
        if loop is None:
            continue
        guards.setdefault(loop.id, (loop, _text(operand), set()))[2].add(_text(field))
    rows: list[tuple[Node, str]] = []
    relied: list[str] = []
    for loop, variable, fields in guards.values():
        table = _table(loop, variable)
        declared = _fields(table) if table is not None else None
        if table is None or declared is None or any(declared.get(f) != "bool" for f in fields):
            continue
        literal = next((c for c in table.named_children if c.type == "literal_value"), None)
        function = _enclosing_function(loop)
        function_name = _text(function.child_by_field_name("name")) if function is not None else "?"
        elements = list(literal.named_children) if literal is not None else []
        unset: list[tuple[Node, str]] = []
        doomed: list[tuple[Node, str]] = []
        for index, element in enumerate(elements):
            values = _row_values(element)
            if values is None or any(f in values and values[f].type != "false" for f in fields):
                continue
            title = next(
                (t for f in _TITLE_FIELDS if f in values and (t := _string_value(values[f])) is not None), None
            )
            label = f"{function_name}/{title if title is not None else f'row {index + 1}'}"
            if (
                title is not None
                and keyed.search(title)
                and not ts_tests._HEDGES.search(title)
                and _own_lines(element)
            ):
                doomed.append((element, label))
            else:
                unset.append((element, label))
        if doomed and len(doomed) >= len(elements):
            # Every row would go: leave the table for the reviewer.
            unset, doomed = unset + doomed, []
        rows.extend(doomed)
        relied.extend(label for _row, label in unset)
    return rows, relied


def _key_pattern(flag_key: str, untreated: str) -> re.Pattern[str]:
    """A title naming the flag's literal key in the removed state: the key
    with any run of ``-``, ``_`` and space standing for each of its
    separators, any case, beside the state word (the TypeScript family's
    wording)."""
    words = [w for w in re.split(r"[-_\s]+", flag_key) if w]
    key = r"(?<![A-Za-z0-9_-])" + r"[-_\s]+".join(re.escape(w) for w in words) + r"(?![A-Za-z0-9_-])"
    state = rf"(?:{ts_tests._STATES[untreated]})"
    return re.compile(rf"{key}(?:\W+\w+){{0,2}}?\W+{state}\b|\b{state}\W+{key}", re.IGNORECASE)
