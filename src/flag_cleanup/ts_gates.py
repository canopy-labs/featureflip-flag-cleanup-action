"""Unwrap a flag-gating JSX component: ``<FlagGate flag="k">…</FlagGate>``.

A React codebase often gates a whole route or panel behind a component rather
than a hook call:

.. code-block:: tsx

    <FlagGate flag="new-billing" redirectTo="/settings">
      <BillingPage />
    </FlagGate>

The key is a JSX PROP, not a call argument, so no accessor rule can see it,
and the engine leaves the gate standing. Worse, once the registry entry the
gate's prop is typed against goes, the gate stops compiling. So the customer
names the component and its key prop in the ``accessors`` input —
``FlagGate[flag]`` — and this pass removes it.

What a gate does when its flag is ON is the definition of a gate: it renders
its children. So on the ``true`` treatment the element is replaced with them —
the single child element itself when there is exactly one, otherwise the
children wrapped in a fragment, which is what the gate rendered. What it does
when the flag is OFF is the component's own business (a redirect, a fallback,
nothing at all), and nothing in the call site says which. The ``false``
treatment is therefore REFUSED, never guessed at; see :func:`refusal_reason`.

Three shapes are refused on ``true`` as well, for the same reason — the call
site does not say what the gate would do:

* a self-closing gate, which has no children to keep;
* a spread attribute AFTER the key prop, which may override the key;
* a key prop whose value is not a plain string literal (a variable, a
  template with substitutions). That one is not a refusal so much as a shape
  this pass does not recognise: it is not matched at all, and the residual
  scan reports the file.

Gates inside a TEST file are deliberately left alone. A test that writes
``<FlagGate flag="k">`` is almost always testing the gate itself, with the key
as fixture data, and unwrapping it would delete the subject under test. The
transform puts such a file back whole; see :func:`flag_cleanup.ts_tests.data_mention`.

The TSX grammar is required, so this only runs for languages that parse JSX
(``tsx`` and ``js``). A ``.ts`` file cannot contain JSX at all.

A second kind of gate takes a boolean rather than a key, and says at the call
site what it renders either way:

.. code-block:: tsx

    <ConditionallyRender condition={show} show={<Chart />} elseShow={<Empty />} />

The customer declares it as ``ConditionallyRender[condition?show:elseShow]``.
Nothing about it is known before the engine runs: the fold is what settles it,
by leaving ``condition={true}``. So :func:`unwrap_boolean_gates` runs after the
engine, and replaces the element with the branch it renders. Which elements
qualify, and why each condition is there:

* **The transform made the literal.** A file that already passed one to this
  gate is left as it was: that literal is the customer's, and nothing tells it
  apart from the fold's.
* **Every attribute is one the gate declared, once.** A spread can set any of
  them, a ``key`` would be lost with the element, and any other prop may change
  what the component does.
* **The chosen branch can be read off the call site**: JSX, a string, ``null``,
  or a function of no arguments that returns one expression. A plain name is
  not one of them, since it may hold a function the component calls.

An element that does not qualify keeps its literal condition, which is what the
run produced before this pass existed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"

#: ``Component[prop]`` in the ``accessors`` input, or for a gate on a boolean
#: ``Component[prop?show:otherwise]``, the ``otherwise`` being optional. The
#: component may be dotted (``Flags.Gate``), the props may not: they are JSX
#: attribute names, or ``children``.
GATE_RE = re.compile(
    rf"\A(?P<component>{_NAME}(?:\.{_NAME})*)"
    rf"\[(?P<prop>{_NAME})(?:\?(?P<show>{_NAME})(?::(?P<otherwise>{_NAME}))?)?\]\Z"
)

#: The languages whose grammar has JSX: both parse with the TSX grammar, the
#: same split the engine draws when it loads its TSX-only JSX cleanup. Plain
#: ``ts`` shares their rule base and not their grammar.
_LANGUAGES = frozenset({"tsx", "js"})

_ELEMENTS = frozenset({"jsx_element", "jsx_self_closing_element"})

#: What a boolean gate's prop may be replaced with. See the module docstring.
_RENDERED = _ELEMENTS | {"string", "null"}

_CHILDREN = "children"


@dataclass(frozen=True)
class Gate:
    """A component that renders its children only while a flag is on.

    With ``show`` set, one that takes a boolean in ``prop`` instead of a key,
    and renders ``show`` or ``otherwise``: props, or ``children``.
    """

    component: str
    prop: str
    show: str | None = None
    otherwise: str | None = None

    def __str__(self) -> str:
        if self.show is None:
            return f"{self.component}[{self.prop}]"
        otherwise = "" if self.otherwise is None else f":{self.otherwise}"
        return f"{self.component}[{self.prop}?{self.show}{otherwise}]"


def parse_gate(value: str) -> Gate | None:
    """``Gate`` for a ``Component[prop]`` spelling, ``None`` for anything else."""
    match = GATE_RE.match(value)
    if match is None:
        return None
    return Gate(match["component"], match["prop"], match["show"], match["otherwise"])


def covers(language: str) -> bool:
    """Whether ``language`` can hold a JSX gate at all."""
    return language in _LANGUAGES


@dataclass(frozen=True)
class _Found:
    node: Node
    gate: Gate
    #: Why this gate cannot be unwrapped on ``true``, or ``None`` if it can.
    problem: str | None


def find_gates(
    source: str, language: str, flag_key: str, gates: tuple[Gate, ...]
) -> list[_Found]:
    """Every element of a configured gate whose key prop is ``flag_key``.

    Outermost first, and never one nested inside another found one: unwrapping
    the outer keeps the inner in its children, and the next pass finds it.
    """
    gates = tuple(gate for gate in gates if gate.show is None)
    if not gates or not covers(language):
        return []
    data = source.encode("utf-8")
    found: list[_Found] = []
    for node in _walk(_parse(source, language)):
        if node.type not in _ELEMENTS:
            continue
        tag = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
        if tag is None:
            continue
        name = tag.child_by_field_name("name")
        if name is None:
            continue
        for gate in gates:
            if _text(name, data) != gate.component:
                continue
            problem = _key_problem(tag, data, gate.prop, flag_key)
            if problem is _NOT_THIS_FLAG:
                continue
            if problem is None and node.type == "jsx_self_closing_element":
                problem = "it has no children to keep"
            found.append(_Found(node, gate, problem))
            break
    outermost = [
        f
        for f in found
        if not any(o is not f and _contains(o.node, f.node) for o in found)
    ]
    return sorted(outermost, key=lambda f: f.node.start_byte)


def refusal_reason(
    source: str, language: str, flag_key: str, gates: tuple[Gate, ...], treatment: bool
) -> str | None:
    """Why this file's gates cannot be removed for ``treatment``, or ``None``.

    Asked of the CUSTOMER's bytes, before the engine runs, so a refusal costs
    nothing to roll back.
    """
    found = find_gates(source, language, flag_key, gates)
    if not found:
        return None
    first = found[0]
    line = source.count("\n", 0, first.node.start_byte) + 1
    where = f"<{first.gate.component} {first.gate.prop}=…> on line {line}"
    if not treatment:
        return (
            f"{where} gates on this flag, and what a gate renders when its flag "
            "is off (a redirect, a fallback, nothing) is the component's own "
            "behaviour, which the call site does not show"
        )
    for f in found:
        if f.problem is not None:
            line = source.count("\n", 0, f.node.start_byte) + 1
            return f"<{f.gate.component} {f.gate.prop}=…> on line {line}: {f.problem}"
    return None


def unwrap_gates(
    source: str, language: str, flag_key: str, gates: tuple[Gate, ...]
) -> tuple[str, int]:
    """``source`` with every gate on ``flag_key`` replaced by its children.

    For the ``true`` treatment only, and only after :func:`refusal_reason` has
    passed the file. A gate that is nonetheless not unwrappable is left
    standing rather than guessed at. Returns the number unwrapped.
    """
    count = 0
    # One gate per parse: replacing it moves every byte after it, and a gate
    # nested in its children is only outermost once it is gone.
    for _ in range(1000):
        found = [f for f in find_gates(source, language, flag_key, gates) if f.problem is None]
        if not found:
            break
        data = source.encode("utf-8")
        node = found[0].node
        replacement = _children_text(node, data)
        data = data[: node.start_byte] + replacement + data[node.end_byte :]
        source = data.decode("utf-8")
        count += 1
    return source, count


def unwrap_boolean_gates(before: str, after: str, language: str, gates: tuple[Gate, ...]) -> str:
    """``after`` with each boolean gate the transform settled replaced by its branch.

    ``before`` is the file as the customer wrote it. See the module docstring
    for which elements qualify; the rest are left as they are.
    """
    gates = tuple(
        gate
        for gate in gates
        if gate.show is not None and covers(language) and after != before
        and not _settled(before, language, (gate,))
    )
    if not gates:
        return after
    # One gate per parse, as in `unwrap_gates`. Each round removes a settled
    # element, so a refused one cannot keep this going.
    while True:
        data = after.encode("utf-8")
        for node, gate, literal in _settled(after, language, gates):
            edit = _branch_edit(node, gate, literal, data)
            if edit is not None:
                start, end, replacement = edit
                after = (data[:start] + replacement + data[end:]).decode("utf-8")
                break
        else:
            return after


def _settled(source: str, language: str, gates: tuple[Gate, ...]) -> list[tuple[Node, Gate, str]]:
    """Each element of a boolean gate whose condition is a literal, in source order."""
    data = source.encode("utf-8")
    found = []
    for node in _walk(_parse(source, language)):
        if node.type not in _ELEMENTS:
            continue
        tag = _open_tag(node)
        name = tag.child_by_field_name("name") if tag is not None else None
        if name is None:
            continue
        for gate in gates:
            literal = _condition(tag, gate.prop, data)
            if _text(name, data) == gate.component and literal is not None:
                found.append((node, gate, literal))
                break
    return sorted(found, key=lambda item: item[0].start_byte)


def _open_tag(element: Node) -> Node | None:
    return element.child_by_field_name("open_tag") if element.type == "jsx_element" else element


def _attribute_name(attribute: Node, data: bytes) -> str | None:
    """A plain attribute's name, ``None`` for a spread or a namespaced name."""
    if attribute.type != "jsx_attribute" or not attribute.named_children:
        return None
    name = attribute.named_children[0]
    return _text(name, data) if name.type == "property_identifier" else None


def _attribute_value(attribute: Node) -> Node | None:
    return attribute.named_children[1] if len(attribute.named_children) > 1 else None


def _condition(tag: Node, prop: str, data: bytes) -> str | None:
    """``true`` or ``false`` if ``prop`` is that literal, in braces."""
    for attribute in tag.children_by_field_name("attribute"):
        if _attribute_name(attribute, data) != prop:
            continue
        value = _attribute_value(attribute)
        if value is not None and value.type == "jsx_expression" and len(value.named_children) == 1:
            inner = value.named_children[0]
            if inner.type in ("true", "false"):
                return inner.type
    return None


def _branch_edit(
    node: Node, gate: Gate, literal: str, data: bytes
) -> tuple[int, int, bytes] | None:
    """The ``(start, end, replacement)`` that settles ``node``, ``None`` to leave it."""
    tag = _open_tag(node)
    assert tag is not None
    props = {}
    for attribute in tag.children_by_field_name("attribute"):
        name = _attribute_name(attribute, data)
        if name is None or name in props or name not in (gate.prop, gate.show, gate.otherwise):
            return None
        props[name] = attribute
    children = _children(node, data) if node.type == "jsx_element" else []
    if children and _CHILDREN not in (gate.show, gate.otherwise):
        return None
    chosen = gate.show if literal == "true" else gate.otherwise
    in_jsx = node.parent is not None and node.parent.type == "jsx_element"
    if chosen == _CHILDREN and children:
        return node.start_byte, node.end_byte, _children_text(node, data)
    attribute = props.get(chosen) if chosen != _CHILDREN else None
    if attribute is None:
        return _nothing_rendered(node, in_jsx, data)
    value = _attribute_value(attribute)
    branch = _rendered(value) if value is not None else None
    if branch is None:
        return None
    if branch.type == "null":
        return _nothing_rendered(node, in_jsx, data)
    text = _dedented(branch, node, data)
    if text is None:
        return None
    if in_jsx and branch.type == "string":
        text = b"{" + text + b"}"
    return node.start_byte, node.end_byte, text


def _rendered(value: Node) -> Node | None:
    """What an attribute value renders, if the call site says."""
    if value.type == "jsx_expression":
        if len(value.named_children) != 1:
            return None
        value = _unparenthesized(value.named_children[0])
    if value.type == "arrow_function":
        parameters = value.child_by_field_name("parameters")
        body = value.child_by_field_name("body")
        if (
            parameters is None
            or parameters.named_children
            or body is None
            or value.children[0].type == "async"
        ):
            return None
        value = _unparenthesized(body)
    return value if value.type in _RENDERED else None


def _unparenthesized(node: Node) -> Node:
    while node.type == "parenthesized_expression" and len(node.named_children) == 1:
        node = node.named_children[0]
    return node


def _nothing_rendered(node: Node, in_jsx: bool, data: bytes) -> tuple[int, int, bytes]:
    """The edit for a gate that renders nothing: no child, or a ``null`` value."""
    if not in_jsx:
        outer = node.parent if node.parent.type == "parenthesized_expression" else node
        return outer.start_byte, outer.end_byte, b"null"
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    end = data.find(b"\n", node.end_byte)
    end = len(data) if end < 0 else end + 1
    if data[start : node.start_byte].strip() or data[node.end_byte : end].strip():
        return node.start_byte, node.end_byte, b""
    return start, end, b""


_NOT_THIS_FLAG = "not this flag"


def _key_problem(tag: Node, data: bytes, prop: str, flag_key: str) -> str | None:
    """``None`` if ``tag`` gates on ``flag_key`` and can be unwrapped.

    :data:`_NOT_THIS_FLAG` if it does not name ``flag_key`` in ``prop`` at all,
    and otherwise why it cannot be unwrapped.
    """
    attributes = tag.children_by_field_name("attribute")
    key_at = None
    for index, attribute in enumerate(attributes):
        if attribute.type != "jsx_attribute" or not attribute.named_children:
            continue
        name = attribute.named_children[0]
        if name.type != "property_identifier" or _text(name, data) != prop:
            continue
        value = attribute.named_children[1] if len(attribute.named_children) > 1 else None
        if value is None or _literal(value, data) != flag_key:
            return _NOT_THIS_FLAG
        key_at = index
    if key_at is None:
        return _NOT_THIS_FLAG
    if any(_is_spread(a) for a in attributes[key_at + 1 :]):
        return "a spread attribute after the key prop may override it"
    return None


def _literal(value: Node, data: bytes) -> str | None:
    """The text of ``"k"``, ``{'k'}`` or ``{`k`}``; ``None`` for anything else."""
    if value.type == "jsx_expression":
        inner = value.named_children
        if len(inner) != 1:
            return None
        value = inner[0]
    if value.type == "string" or (
        value.type == "template_string"
        and all(c.type == "string_fragment" for c in value.named_children)
    ):
        fragments = value.named_children
        if len(fragments) == 1 and fragments[0].type == "string_fragment":
            return _text(fragments[0], data)
    return None


def _is_spread(attribute: Node) -> bool:
    return attribute.type == "jsx_expression" and any(
        c.type == "spread_element" for c in attribute.named_children
    )


def _children_text(element: Node, data: bytes) -> bytes:
    """What the gate rendered: its sole child element, or its children in a fragment."""
    open_tag = element.child_by_field_name("open_tag")
    close_tag = element.child_by_field_name("close_tag")
    assert open_tag is not None and close_tag is not None
    children = _children(element, data)
    if len(children) == 1 and children[0].type in _ELEMENTS:
        moved = _dedented(children[0], element, data)
        if moved is not None:
            return moved
    inner = data[open_tag.end_byte : close_tag.start_byte]
    return b"<>" + inner + b"</>"


def _children(element: Node, data: bytes) -> list[Node]:
    """``element``'s children, without the whitespace around them."""
    open_tag = element.child_by_field_name("open_tag")
    close_tag = element.child_by_field_name("close_tag")
    assert open_tag is not None and close_tag is not None
    return [
        c
        for c in element.named_children
        if c.id not in (open_tag.id, close_tag.id)
        and not (c.type == "jsx_text" and not _text(c, data).strip())
    ]


def _dedented(child: Node, element: Node, data: bytes) -> bytes | None:
    """``child``'s text, moved out to ``element``'s column.

    Its first line takes the gate's place, so every later line has to lose the
    difference between the two columns, or the element is left indented one
    level deeper than what now encloses it. ``None`` when that cannot be done
    exactly — a line with less indentation than the shift, or a multi-line
    string whose content would change — and the caller falls back to a
    fragment, which needs no re-indenting at all. JSX text is not such a
    string: its lines are trimmed before they are joined.
    """
    text = data[child.start_byte : child.end_byte]
    if b"\n" not in text:
        return text
    child_column = _column(child, data)
    element_column = _column(element, data)
    if child_column is None or element_column is None or child_column < element_column:
        return None
    shift = child_column - element_column
    if any(
        n.type in ("string", "template_string")
        and n.start_point[0] != n.end_point[0]
        for n in _walk(child)
    ):
        return None
    first, *rest = text.split(b"\n")
    moved = [first]
    for line in rest:
        if not line.strip():
            moved.append(line.strip(b" \t"))
            continue
        if line[:shift].strip(b" ") or len(line) < shift:
            return None
        moved.append(line[shift:])
    return b"\n".join(moved)


def _column(node: Node, data: bytes) -> int | None:
    """``node``'s column if only spaces precede it on its line, else ``None``."""
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    prefix = data[start : node.start_byte]
    if prefix.strip(b" "):
        # Something precedes it on the line — `element={<Gate …>` — so the
        # column the element visually belongs to is the line's indentation.
        return len(prefix) - len(prefix.lstrip(b" "))
    return len(prefix)


def _contains(outer: Node, inner: Node) -> bool:
    return outer.start_byte <= inner.start_byte and inner.end_byte <= outer.end_byte


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")
