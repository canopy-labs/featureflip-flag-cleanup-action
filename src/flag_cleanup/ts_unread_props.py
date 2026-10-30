"""Remove a JSX prop the run left unread inside its component.

A fold can delete the last read of a prop without touching the prop itself::

    const expired = showExpiry && isExpired(expiresAt);   // before
    // (gone: `showExpiry` was `false` everywhere, so the line folded away)

``expiresAt`` is still destructured from the props, still declared in the
props type and still passed by every element, and nothing reads it. A
reviewer drops it from all three, as :mod:`flag_cleanup.ts_constant_props`
drops a prop that became one literal. :func:`drop_unread_props` makes that
edit. It writes no marker: there is nothing left to fold, only declarations
and arguments to delete.

The component must qualify exactly as it does for
:mod:`flag_cleanup.ts_constant_props` (module-private, a top-level function or
a ``const`` bound to one, spelled only at its declaration and as JSX element
names, one destructured parameter, its props type its own). On top of that:

* **The run made it unread.** The component read the prop in the customer's
  file and reads it nowhere in the result. A prop the customer already passed
  and never read is theirs, and stays.
* **The name is bound once in the component, before and after.** A callback
  parameter or local of the same name would count as a read that is not one,
  and turn a prop that was already unread into "newly unread".
* **No element spreads, and each passes the prop at most once, as a value
  that can only read** (a literal, a name, a property access off a name). A
  call or a ``new`` in the attribute might do work that removing it would stop.
* **The destructuring keeps at least one entry.**

A refusal leaves the prop where it is, which is what the run produced before
this pass existed.
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup.ctor_param_ts import _inert
from flag_cleanup.ts_constant_props import (
    _attributes,
    _component_function,
    _elements,
    _is_module,
    _item_span,
    _member,
    _names,
    _own_lines,
    _text,
    _with_doc_comment,
)
from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

_READS = frozenset({"identifier", "shorthand_property_identifier"})

Edit = tuple[int, int, bytes]


def drop_unread_props(before: str, after: str, language: str) -> str:
    """Remove every prop this run left unread in a module-private component.

    ``before`` is the customer's file and ``after`` the current result.
    Returns ``after`` unchanged when nothing qualifies (see the module
    docstring).
    """
    if after == before or language == "ts":
        return after
    root = _parse(after, language)
    if root.has_error or not _is_module(root):
        return after
    old_root = _parse(before, language)
    if old_root.has_error:
        return after
    data = after.encode("utf-8")
    old_data = before.encode("utf-8")
    old_elements = _elements(old_root, old_data)
    edits: list[Edit] = []
    for component, elements in _elements(root, data).items():
        if any(e.spreads for e in elements):
            continue
        function = _component_function(root, data, component, elements)
        old_function = _component_function(
            old_root, old_data, component, old_elements.get(component, [])
        )
        if function is None or old_function is None:
            continue
        for planned in _plans(root, data, function, old_function, old_data, elements):
            if not _overlaps(edits, planned):
                edits.extend(planned)
    if not edits:
        return after
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8")


def _overlaps(edits: list[Edit], planned: list[Edit]) -> bool:
    return any(s < e2 and s2 < e for s, e, _ in edits for s2, e2, _ in planned)


def _entries(function: Node) -> tuple[Node, list[Node]] | None:
    """The parameter and its object pattern's entries, or None."""
    parameters = function.child_by_field_name("parameters")
    if parameters is None or len(parameters.named_children) != 1:
        return None
    parameter = parameters.named_children[0]
    if parameter.type != "required_parameter":
        return None
    pattern = parameter.child_by_field_name("pattern")
    if pattern is None or pattern.type != "object_pattern":
        return None
    return parameter, [c for c in pattern.named_children if c.type != "comment"]


def _reads(function: Node, data: bytes, prop: str) -> int:
    """How many times the component's body spells ``prop`` as a value."""
    body = function.child_by_field_name("body")
    if body is None:
        return -1
    return sum(1 for n in _walk(body) if n.type in _READS and _text(n, data) == prop)


def _bound_once(function: Node, data: bytes, prop: str) -> bool:
    return sum(1 for n in binding_nodes(function) if _text(n, data) == prop) == 1


def _plans(root, data, function, old_function, old_data, elements) -> list[list[Edit]]:
    """One edit list per prop that qualifies, in source order."""
    found = _entries(function)
    if found is None:
        return []
    parameter, entries = found
    plans: list[list[Edit]] = []
    for entry in entries:
        if entry.type != "shorthand_property_identifier_pattern":
            continue
        prop = _text(entry, data)
        if len(plans) + 1 >= len(entries):
            # The destructuring keeps one entry.
            break
        if any(
            c.type in {"object_assignment_pattern", "pair_pattern"} and _names(c, data, prop)
            for c in entries
        ):
            continue
        if _reads(function, data, prop) != 0 or _reads(old_function, old_data, prop) < 1:
            continue
        if not (_bound_once(function, data, prop) and _bound_once(old_function, old_data, prop)):
            continue
        planned = _plan(root, data, parameter, entry, prop, elements)
        if planned is not None:
            plans.append(planned)
    return plans


def _plan(root, data, parameter, entry, prop, elements) -> list[Edit] | None:
    member = _member(root, data, parameter.child_by_field_name("type"), prop)
    if member is False:
        return None
    edits: list[Edit] = []
    for element in elements:
        attributes = _attributes(element, prop, data)
        if not attributes:
            continue
        if len(attributes) != 1 or not _inert_value(attributes[0]):
            return None
        span = _item_span(attributes[0], data, separator=None)
        if span is None:
            return None
        edits.append((*span, b""))
    span = _item_span(entry, data, separator=b",")
    if span is None:
        return None
    edits.append((*span, b""))
    if member is not None:
        span = _own_lines(member, data)
        if span is None:
            return None
        edits.append((_with_doc_comment(member, data, span[0]), span[1], b""))
    return edits


def _inert_value(attribute: Node) -> bool:
    """Whether the attribute's value can only read: none, a string, or an
    expression container holding one inert expression."""
    values = attribute.named_children[1:]
    if not values:
        return True
    if len(values) != 1:
        return False
    value = values[0]
    if value.type == "string":
        return True
    if value.type != "jsx_expression":
        return False
    inner = [c for c in value.named_children if c.type != "comment"]
    return len(inner) == 1 and len(value.named_children) == 1 and _inert(inner[0])
