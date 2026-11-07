"""What a C# gate-family removal leaves describing the gate in the tests it keeps.

A complete gate family takes the tests that ran its removed branch with it,
and cuts the parts and ``[InlineData]`` rows that did. What it keeps can still
describe the branch that is gone:

* **A ``[Theory]`` left with one row.** Once the removed value's rows go,
  ``[Theory] [InlineData(false)] M(bool other)`` runs once, with a parameter
  that is a constant. It becomes ``[Fact] M()``, and each parameter's value is
  written where the parameter was read (:func:`collapse`).
* **A name that states the gate's state.** ``EnsureCanSet_GateOn_Passes``
  names a condition that no longer exists. A name whose part is nothing but
  the gate's KEPT state is renamed without that part, when nothing else in the
  repository names the method; any other is reported (:func:`review_names`).
* **A test the run cut down to what a sibling does** (``_duplicates``), and a
  name that speaks of several gates once one is gone (``_several_gates``):
  both reported, never edited.
* **A test a phase cut shortened** (``TEST_PHASE``) whose name says what
  happened while the gate held the removed value
  (``Job_DoesNothing_UntilTheGateIsOn``): the name is now false.
  When what is left arranges and acts, token for token, as exactly one
  sibling ``[Fact]`` of the same class does, and both then only assert, it is
  folded into that sibling (:func:`fold`): the assertions the sibling does
  not already make go at its end, and the test goes. Otherwise it is
  reported beside the cut, never edited.

Which gate a name means is decided per token (:func:`_attributed`): a word
before the gate's noun that belongs to the family's name (``EncryptionGate``)
names it; a word that belongs only to another gate type of the file names
that gate. A bare ``GateOn`` in a test whose body names no other gate is the
family's when the body names a family type or a field of one, or this run
deleted a family test of the file whose name holds the opposite state and
shares another part with it (``GateOn_PastExpiry_…`` beside a deleted
``GateOff_PastExpiry_…``). When only the class ties it to the family (it
names no other gate type, but one can be reached through a type declared
elsewhere), the name is reported, never renamed.

A ``nameof`` argument or a ``///`` ``cref`` naming the method, in its own file
(bare inside its type, or qualified by the type), is renamed with it. A rename
is refused when anything else in the checkout spells the old name (prose and
strings included; the row names the spellings, never the declaration), a file
is named for it (a snapshot library's ``Class.Method.verified.txt``), or the
test's type, another part of it or a type it derives from already declares the
new one. A theory with a file
named for it keeps its row for the same reason. A ``string``
value is written only where it cannot join a constant expression (an
argument, an assignment's or initializer's value, a ``return``, an
interpolation); an ``int`` value only where its type is also fixed (not an
argument, an initializer's element or a lambda's result, where the constant
can pick another overload). A read under ``nameof`` refuses for every type.

The theory rewrite is the engine's: the run marks the ``[Theory]``, the row,
each parameter and each read with sentinel names, and rules rendered here
replace or delete them (the attribute, the attribute list, the parameter, a
literal). A ``bool`` literal is written through the group the flag fold uses,
so the engine folds what it exposes (``if (false) …``) the way it folds a flag
read. Deciding which theories qualify, and every name, is this module's.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import file_deletion, git_ops, syntax
from flag_cleanup.ctor_param_model import choose_sentinel, tidy, tidy_lines
from flag_cleanup.mock_stubs import (
    DUPLICATE_TEST,
    FALSE_TITLE,
    FOLDED_TEST,
    GATE_NAMED,
    RENAMED_METHOD,
    TEST,
    TEST_PHASE,
    THEORY_FACT,
)

logger = logging.getLogger(__name__)

_BOM = "﻿"


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _parse(source: str) -> Node:
    return syntax._parse(source.removeprefix(_BOM), "csharp")


def _errors(root: Node) -> int:
    return sum(1 for n in _walk(root) if n.type == "ERROR" or n.is_missing)


def _attribute_name(attribute: Node) -> str:
    name = attribute.child_by_field_name("name")
    spelled = _text(name) if name is not None else ""
    return re.split(r"[.:]", spelled)[-1].strip().removesuffix("Attribute")


def _attributes(method: Node) -> list[tuple[Node, Node]]:
    """``(attribute list, attribute)`` for each attribute on ``method``."""
    return [
        (attribute_list, attribute)
        for attribute_list in method.children
        if attribute_list.type == "attribute_list"
        for attribute in attribute_list.named_children
        if attribute.type == "attribute"
    ]


def _type_chain(node: Node) -> tuple[str, ...]:
    chain: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type in ("class_declaration", "struct_declaration", "record_declaration"):
            name = parent.child_by_field_name("name")
            chain.append(_text(name) if name is not None else "")
        parent = parent.parent
    return tuple(reversed(chain))


def _methods(root: Node) -> dict[tuple[tuple[str, ...], str], Node]:
    """Each method by ``(enclosing types, name)``, when the name is declared
    once in its type."""
    found: dict[tuple[tuple[str, ...], str], list[Node]] = {}
    for node in _walk(root):
        if node.type != "method_declaration":
            continue
        name = node.child_by_field_name("name")
        if name is not None:
            found.setdefault((_type_chain(node), _text(name)), []).append(node)
    return {key: nodes[0] for key, nodes in found.items() if len(nodes) == 1}


# --- A [Theory] left with one row ---------------------------------------------------

#: The parameter types whose row value is written where the parameter was
#: read, each with the literal it takes. A string's text is held to what a
#: rule's replacement can spell as it is (no ``@``, which the engine reads as
#: a capture, no escape, no quote).
_VALUES = {
    "bool": re.compile(r"true|false"),
    "int": re.compile(r"[0-9]{1,9}"),
    "string": re.compile(r'"[A-Za-z0-9 _.,:;/+*#!?()\[\]=<>-]*"'),
}
#: Attributes a theory may carry beside ``[Theory]`` and its one row: none
#: that can add a row.
_HARMLESS_ATTRIBUTES = frozenset({"Trait"})
#: Where a ``bool`` parameter's name is a read the literal can take the place
#: of, by parent node type, and the field it must be in (``None``: any). What
#: a ``true``/``false`` makes constant here, the engine folds as it folds a
#: flag read.
_READ_PARENTS: dict[str, frozenset[str] | None] = {
    "if_statement": frozenset({"condition"}),
    "conditional_expression": None,
    "binary_expression": None,
    "prefix_unary_expression": None,
    "postfix_unary_expression": None,
    "parenthesized_expression": None,
    "assignment_expression": frozenset({"right"}),
    "argument": None,
    "lambda_expression": frozenset({"body"}),
    "arrow_expression_clause": None,
    "interpolation": None,
    "initializer_expression": None,
    "member_access_expression": frozenset({"expression"}),
    "equals_value_clause": None,
    "variable_declarator": None,
    "return_statement": None,
    "is_pattern_expression": frozenset({"expression"}),
    "cast_expression": frozenset({"value"}),
}
#: The same for an ``int`` or ``string``: only where the literal cannot join
#: a constant expression. ``10 / d`` with ``0`` is CS0020, ``100000 * n``
#: CS0220, ``(byte)n`` CS0221, and ``if (d == 0)`` or ``switch (n)`` leaves
#: a branch the compiler calls unreachable (CS0162), none of which the
#: engine folds away.
#: Where an ``int`` constant may pick another overload than the ``int``
#: variable did: it converts implicitly to ``byte``, ``short``, ``uint`` and
#: the rest when it fits, so ``F(2)`` calls ``F(byte)`` where ``F(n)`` called
#: ``F(long)``. A call's, constructor's or indexer's argument, a collection
#: initializer's element (an ``Add`` overload), an object initializer's value
#: (a setter, and through it nothing fixed here) and a lambda's body (its
#: delegate type is chosen by overloads, and so is a ``return`` inside one)
#: are all such places. An assignment, a local's initializer, a method's
#: ``return`` and an interpolation take a fixed type.
_OVERLOAD_PARENTS = frozenset({"argument", "initializer_expression"})
_VALUE_PARENTS: dict[str, frozenset[str] | None] = {
    "argument": None,
    "assignment_expression": frozenset({"right"}),
    "equals_value_clause": None,
    "variable_declarator": None,
    "initializer_expression": None,
    "return_statement": None,
    "interpolation": None,
    "arrow_expression_clause": None,
    "lambda_expression": frozenset({"body"}),
}


@dataclass
class _Theory:
    method: Node
    name: str
    #: The ``Theory`` identifier, which becomes ``Fact``.
    attribute: Node
    #: The one ``[InlineData(…)]`` list left.
    row: Node
    #: ``(parameter, name, literal)`` for each parameter.
    parameters: list[tuple[Node, str, str]]
    #: ``(identifier, index into parameters)`` for each read.
    reads: list[tuple[Node, int]] = field(default_factory=list)


def _row_count(method: Node) -> int:
    return sum(1 for _, a in _attributes(method) if _attribute_name(a) == "InlineData")


def theories(original: str, content: str) -> tuple[list[_Theory], list[tuple[str, str]]]:
    """The theories of ``content`` the run left with one ``[InlineData]`` row
    that can become a ``[Fact]``, and ``(name, reason)`` for each one that
    cannot. A theory is the run's when ``original`` declared the same method,
    in the same types, with two rows or more."""
    before = _methods(_parse(original))
    plans: list[_Theory] = []
    refused: list[tuple[str, str]] = []
    for key, method in _methods(_parse(content)).items():
        old = before.get(key)
        if old is None or _row_count(old) < 2 or _row_count(method) != 1:
            continue
        plan = _plan(method)
        if isinstance(plan, str):
            refused.append((key[1], plan))
        else:
            plans.append(plan)
    return plans, refused


def _plan(method: Node) -> _Theory | str:
    """The rewrite of one theory, or why it is left as it is."""
    name = _text(method.child_by_field_name("name"))
    theory = row = None
    for attribute_list, attribute in _attributes(method):
        kind = _attribute_name(attribute)
        if len([a for a in attribute_list.named_children if a.type == "attribute"]) != 1:
            return "an attribute list holds more than one attribute"
        arguments = next((c for c in attribute.named_children if c.type == "attribute_argument_list"), None)
        if kind == "Theory":
            spelled = attribute.child_by_field_name("name")
            if arguments is not None or spelled is None or spelled.type != "identifier":
                return "its [Theory] is qualified or has arguments"
            theory = spelled
        elif kind == "InlineData":
            row = attribute_list
        elif kind not in _HARMLESS_ATTRIBUTES:
            return f"it carries [{kind}], which may add rows"
    if theory is None or row is None:
        return "it is not a plain [Theory]"
    if method.child_by_field_name("type_parameters") is not None or any(
        c.type == "type_parameter_list" for c in method.children
    ):
        return "it is generic"
    listed = method.child_by_field_name("parameters")
    parameters = [c for c in listed.named_children if c.type == "parameter"] if listed is not None else []
    if listed is None or any(c.type not in ("parameter", "comment") for c in listed.named_children):
        return "its parameter list holds more than plain parameters"
    attribute = next(a for _, a in _attributes(method) if _attribute_name(a) == "InlineData")
    arguments = next((c for c in attribute.named_children if c.type == "attribute_argument_list"), None)
    items = [a for a in arguments.named_children if a.type == "attribute_argument"] if arguments is not None else []
    if len(items) != len(parameters) or any(c.type in ("=", ":") for item in items for c in item.children):
        return "its row does not pass one positional value per parameter"
    planned: list[tuple[Node, str, str]] = []
    for parameter, item in zip(parameters, items):
        spelled = parameter.child_by_field_name("name")
        type_node = parameter.child_by_field_name("type")
        if spelled is None or type_node is None:
            return "a parameter has no name or type"
        pname = _text(spelled).removeprefix("@")
        if any(c.type in ("modifier", "attribute_list", "equals_value_clause", "=") for c in parameter.children):
            return f"parameter `{pname}` has a modifier, an attribute or a default"
        pattern = _VALUES.get(_text(type_node))
        value = item.named_children[-1] if item.named_children else None
        if pattern is None:
            return f"parameter `{pname}` is a `{_text(type_node)}`; only bool, int and string values are written in"
        if value is None or not pattern.fullmatch(_text(value)):
            return f"the row's value for `{pname}` is not a plain literal of its type"
        if _doc_names(method, pname):
            return f"a doc comment names parameter `{pname}`"
        planned.append((parameter, pname, _text(value)))
    plan = _Theory(method, name, theory, row, planned)
    names = {pname: i for i, (_, pname, _) in enumerate(planned)}
    if len(names) != len(planned):
        return "two parameters share a name"
    body = method.child_by_field_name("body")
    scopes = [c for c in method.children if c.type in ("block", "arrow_expression_clause")]
    if body is not None and body not in scopes:
        scopes.append(body)
    for scope in scopes:
        for node in _walk(scope):
            if node.type not in ("identifier", "implicit_parameter"):
                continue
            spelled = _text(node).removeprefix("@")
            if spelled not in names:
                continue
            literal = planned[names[spelled]][2]
            verdict = _read(node, literal in ("true", "false"), literal.isdigit())
            if verdict is None:
                continue
            if verdict is not True:
                return f"parameter `{spelled}` is {verdict}"
            plan.reads.append((node, names[spelled]))
    return plan


def _read(node: Node, boolean: bool = True, integer: bool = False) -> bool | str | None:
    """``True`` when ``node``, an identifier spelling a parameter, is a read a
    literal can take the place of; ``None`` when it is another thing's name
    (a member, a named argument's label); why not, otherwise. ``boolean``
    says the literal is ``true``/``false`` (see :data:`_VALUE_PARENTS`), and
    ``integer`` that it is an ``int`` (see :data:`_OVERLOAD_PARENTS`)."""
    ancestor = node.parent
    while ancestor is not None and ancestor.type not in ("method_declaration", "local_function_statement"):
        if ancestor.type == "invocation_expression":
            function = ancestor.child_by_field_name("function")
            if function is not None and _text(function) == "nameof":
                # `nameof(s.Length)` names a member of the parameter's type.
                return "read with nameof"
        ancestor = ancestor.parent
    parent = node.parent
    if parent is None:
        return "read where nothing can replace it"
    if node.type == "implicit_parameter":
        return "declared again inside the method"
    field_name = None
    for i, child in enumerate(parent.children):
        if child.id == node.id:
            field_name = parent.field_name_for_child(i)
    if parent.type == "member_access_expression" and field_name == "name":
        return None
    if parent.type in ("name_colon", "name_equals"):
        return None
    if parent.type == "assignment_expression" and field_name == "left":
        initializer = parent.parent
        if (
            initializer is not None
            and initializer.type == "initializer_expression"
            and initializer.parent is not None
            and initializer.parent.type in ("object_creation_expression", "implicit_object_creation_expression")
        ):
            return None  # `new T { Name = … }` sets T's member
        return "assigned"
    if parent.type in ("prefix_unary_expression", "postfix_unary_expression") and any(
        not c.is_named and _text(c) in ("++", "--") for c in parent.children
    ):
        return "assigned"
    if parent.type == "argument":
        if any(not c.is_named and _text(c) in ("ref", "out", "in") for c in parent.children):
            return "passed by reference"
        call = parent.parent.parent if parent.parent is not None else None
        if parent.parent is not None and parent.parent.type == "tuple_expression":
            return "a tuple element, whose name it gives"
        if call is not None and call.type == "invocation_expression":
            function = call.child_by_field_name("function")
            if function is not None and _text(function) == "nameof":
                return "read with nameof"
    if parent.type in ("variable_declarator", "parameter") and field_name == "name":
        return "declared again inside the method"
    if integer and (
        parent.type in _OVERLOAD_PARENTS
        or (parent.type == "lambda_expression" and field_name == "body")
        or (parent.type == "return_statement" and _in_lambda(parent))
        or (
            parent.type == "assignment_expression"
            and parent.parent is not None
            and parent.parent.type == "initializer_expression"
        )
    ):
        return "an int read where a constant could choose another overload"
    allowed = _READ_PARENTS if boolean else _VALUE_PARENTS
    if not boolean and parent.type not in _VALUE_PARENTS:
        return "read where its value would join a constant expression"
    if parent.type not in allowed:
        return f"named inside a `{parent.type}`, which this does not rewrite"
    fields = allowed[parent.type]
    if fields is not None and field_name not in fields:
        return f"named inside a `{parent.type}`, which this does not rewrite"
    return True


def _in_lambda(node: Node) -> bool:
    """Whether the function ``node`` returns from is a lambda or an anonymous
    method, whose return type overloads may choose."""
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in ("lambda_expression", "anonymous_method_expression"):
            return True
        if ancestor.type in ("method_declaration", "local_function_statement"):
            return False
        ancestor = ancestor.parent
    return False


def _doc_names(method: Node, parameter: str) -> bool:
    """Whether a comment directly above the method names the parameter
    (a dangling ``<param>`` is CS1572)."""
    wanted = re.compile(rf"""name\s*=\s*["']{re.escape(parameter)}["']""")
    sibling = method.prev_named_sibling
    while sibling is not None and sibling.type == "comment":
        if wanted.search(_text(sibling)):
            return True
        sibling = sibling.prev_named_sibling
    return False


def render_rules(sentinel: str, values: list[str]) -> str:
    """The rules that finish every marked theory: ``[<s>_fact]`` becomes
    ``[Fact]``, ``[<s>_row]`` goes with its line, a parameter named
    ``<s>_param`` goes, and ``<s>_v<i>()`` becomes ``values[i]``."""
    rules = [
        _rule("theory_to_fact", "(attribute name: (identifier) @name)", f"{sentinel}_fact", "name", "Fact"),
        _rule("theory_row", "(attribute_list (attribute name: (identifier) @name)) @list", f"{sentinel}_row", "list", ""),
        _rule(
            "theory_parameter",
            "(parameter_list (parameter name: (identifier) @name) @param)",
            f"{sentinel}_param",
            "param",
            "",
        ),
    ]
    for index, value in enumerate(values):
        rules.append(
            _rule(
                f"theory_value_{index}",
                "(invocation_expression function: (identifier) @name arguments: (argument_list)) @call",
                f"{sentinel}_v{index}",
                "call",
                value,
                # A boolean reaches the cascade the flag fold starts, so what it
                # exposes (`if (false)`, `!true`) is folded the same way.
                group=value in ("true", "false"),
            )
        )
    return "\n".join(rules)


def _rule(name: str, pattern: str, sentinel: str, node: str, replace: str, group: bool = False) -> str:
    lines = [
        "[[rules]]",
        f'name = "{name}"',
        f'query = """(\n  {pattern}\n  (#eq? @name "{sentinel}")\n)"""',
        f'replace_node = "{node}"',
        f"replace = {json.dumps(replace)}",
        "is_seed_rule = true",
    ]
    if group:
        lines.append('groups = ["replace_expression_with_boolean_literal"]')
    return "\n".join(lines) + "\n"


def collapse(
    summaries: list,
    repo_dir: str,
    rewrite: Callable,
    engine: Callable[[list[str], str], object],
) -> tuple[list, list[tuple[str, str, str]]]:
    """Rewrite every ``[Theory]`` the run left with one row as a ``[Fact]``
    (:func:`theories`), through the engine. A file whose result does not
    hold is put back as it was. Returns the summaries and the rows."""
    candidates: dict[str, tuple[int, object, list[_Theory]]] = {}
    for index, summary in enumerate(summaries):
        if file_deletion.deleted(summary) or not str(summary.path).endswith(".cs"):
            continue
        if summary.original_content == summary.content:
            continue
        try:
            plans, refused = theories(summary.original_content, summary.content)
        except Exception as exc:  # noqa: BLE001 - never refuses the flag
            logger.info("theories of %s not judged: %s: %s", summary.path, type(exc).__name__, exc)
            continue
        for name, reason in refused:
            logger.info("%s keeps [Theory] %s with one row: %s", summary.path, name, reason)
        if plans:
            candidates[str(Path(summary.path).resolve())] = (index, summary, plans)
    if not candidates:
        return summaries, []
    # A snapshot library names its file for the test and, for a theory, its
    # arguments (`Class.M_on=True.verified.txt`): a fact would not find it.
    files = _files_named({p.name for _, _, plans in candidates.values() for p in plans}, repo_dir)
    blocked = set(files)
    for name in sorted(blocked):
        logger.info("[Theory] %s keeps its one row: a file is named for it: %s", name, files[name])
    candidates = {
        path: (index, summary, [p for p in plans if p.name not in blocked])
        for path, (index, summary, plans) in candidates.items()
        if any(p.name not in blocked for p in plans)
    }
    if not candidates:
        return summaries, []
    paths = sorted(candidates)
    git_ops.before_writing(paths)
    before = {path: Path(path).read_bytes() for path in paths}
    sentinel = choose_sentinel(before.values())
    if sentinel is None:
        return summaries, []
    values: list[str] = []
    marked: dict[str, bytes] = {}
    described: dict[str, list[str]] = {}
    for path in paths:
        data = before[path]
        text = data.decode("utf-8")
        offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        _, summary, _ = candidates[path]
        # Planned on the bytes on disk, which the summary's content mirrors.
        plans, _ = theories(summary.original_content, text.removeprefix(_BOM))
        plans = [p for p in plans if p.name not in blocked]
        edits: list[tuple[int, int, bytes]] = []
        for plan in plans:
            edits.append((plan.attribute.start_byte, plan.attribute.end_byte, f"{sentinel}_fact".encode()))
            edits.append((plan.row.start_byte, plan.row.end_byte, f"[{sentinel}_row]".encode()))
            first = len(values)
            values.extend(literal for _, _, literal in plan.parameters)
            edits += [(p.start_byte, p.end_byte, f"object {sentinel}_param".encode()) for p, _, _ in plan.parameters]
            edits += [(n.start_byte, n.end_byte, f"{sentinel}_v{first + i}()".encode()) for n, i in plan.reads]
            described.setdefault(path, []).append(
                f"{plan.name}: " + ", ".join(f"`{p}` = `{v}`" for _, p, v in plan.parameters)
            )
        out = data
        for start, end, replacement in sorted(edits, reverse=True):
            out = out[: start + offset] + replacement + out[end + offset :]
        marked[path] = out
    rows: list[tuple[str, str, str]] = []
    summaries = list(summaries)
    try:
        for path in paths:
            Path(path).write_bytes(marked[path])
        engine(paths, render_rules(sentinel, values))
    except Exception as exc:  # noqa: BLE001 - put every byte back
        logger.warning("theories left as they were: %s: %s", type(exc).__name__, exc)
        for path in paths:
            Path(path).write_bytes(before[path])
        return summaries, []
    original_summaries = list(summaries)
    try:
        for path in paths:
            out = tidy(marked[path], Path(path).read_bytes())
            if b"\r" in out:
                bom = out.startswith(b"\xef\xbb\xbf")
                fixed = syntax.collapse_introduced_carriage_returns(
                    marked[path].decode("utf-8").removeprefix(_BOM), out.decode("utf-8").removeprefix(_BOM), "csharp"
                ).encode("utf-8")
                out = (b"\xef\xbb\xbf" if bom else b"") + fixed
            out = tidy_lines(marked[path], out)
            text = out.decode("utf-8")
            if sentinel in text or _errors(_parse(text)) > _errors(_parse(before[path].decode("utf-8"))):
                logger.warning("theories of %s left as they were: the rewrite did not hold", path)
                Path(path).write_bytes(before[path])
                continue
            Path(path).write_bytes(out)
            index, summary, _ = candidates[path]
            summaries[index] = rewrite(summary.path, summary.original_content, text)
            rows += [(os.path.relpath(path, repo_dir), THEORY_FACT, row) for row in described[path]]
    except Exception as exc:  # noqa: BLE001 - put every byte back
        logger.warning("theories left as they were: %s: %s", type(exc).__name__, exc)
        for path in paths:
            Path(path).write_bytes(before[path])
        return original_summaries, []
    return summaries, rows


# --- Names that state the gate's state ----------------------------------------------

_ON, _OFF = frozenset({"On", "Enabled"}), frozenset({"Off", "Disabled"})
#: Words between the gate's noun and its state: ``GateIsOn``, ``GateSwitchedOff``.
_VERBS = frozenset({"Is", "Was", "Switched", "Turned", "Set", "Stays", "Goes", "Flipped"})
#: Words that make a state a change over time: the test ran both.
_TRANSITIONS = frozenset({"Until", "Unless", "Before", "After", "Once", "Only", "Then", "Switched", "Turned", "Flipped"})
#: Words that make a name speak of several gates at once: ``EitherGateOff``.
_QUANTIFIERS = frozenset({"Either", "Both", "Any", "All", "Neither", "Each", "Every"})
#: Words that may come with the state in a part of a name that goes whole.
_FILLERS = frozenset({"With", "When", "While", "If", "The"})
#: Prefixes a test double's type name adds to the gate it fakes.
_DOUBLE_PREFIXES = frozenset({"I", "Stub", "Fake", "Test", "Mock", "Always", "Never", "Fixed", "Null", "Noop", "On", "Off"})
#: Attributes that make a method a test.
_TEST_ATTRIBUTES = frozenset({"Fact", "Theory", "Test", "TestCase", "TestMethod", "DataTestMethod", "SkippableFact", "SkippableTheory"})


def _words(name: str) -> list[str]:
    return re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", name)


@dataclass(frozen=True)
class Family:
    """A complete gate family's type names, and the words its gate is named by."""

    names: frozenset[str]
    #: The last word of the interface's name: ``Gate``.
    noun: str
    #: The interface's other words: ``Flag``, ``Expiration``.
    qualifiers: frozenset[str]


def family(interface: str, names: Iterable[str]) -> Family | None:
    stem = interface[1:] if len(interface) > 1 and interface[0] == "I" and interface[1].isupper() else interface
    words = _words(stem)
    if not words:
        return None
    return Family(frozenset(names) | {interface}, words[-1], frozenset(words[:-1]))


@dataclass(frozen=True)
class _Token:
    segment: int
    #: The words of its segment, and the noun's position in them.
    words: tuple[str, ...]
    at: int
    state: str
    verb: str | None

    @property
    def before(self) -> str | None:
        return self.words[self.at - 1] if self.at > 0 else None


def _tokens(name: str, noun: str, plural: bool = False) -> list[_Token]:
    """The gate-state tokens of ``name``. ``plural`` also reads ``Gates`` right
    after a quantifier (``BothGatesOn``), which only :func:`_several_gates`
    asks for."""
    found: list[_Token] = []
    for index, segment in enumerate(name.split("_")):
        words = _words(segment)
        for at, word in enumerate(words):
            if word != noun and not (plural and word == noun + "s" and at > 0 and words[at - 1] in _QUANTIFIERS):
                continue
            rest = words[at + 1 :]
            verb = rest[0] if rest and rest[0] in _VERBS else None
            state = rest[1 if verb else 0] if len(rest) > (1 if verb else 0) else None
            if state in _ON | _OFF:
                found.append(_Token(index, tuple(words), at, state, verb))
    return found


@dataclass
class _File:
    family: Family
    #: Gate type names of the file that are not the family's, and their words.
    others: frozenset[str]
    other_words: frozenset[str]
    #: Fields and properties typed by a family type, or by another gate.
    family_receivers: frozenset[str]
    other_receivers: frozenset[str]
    #: Methods of the original text, by name.
    methods: dict[str, Node]
    #: ``(segment set, state)`` of each family test this run deleted.
    deleted: list[tuple[frozenset[str], str]]


def _scan(original: str, fam: Family, deleted_names: Iterable[str]) -> _File | None:
    """What :func:`_attributed` reads of one file, or ``None`` when the
    file's own text names no family type."""
    if not any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", original) for n in fam.names):
        return None
    others: set[str] = set()
    other_words: set[str] = set()
    for spelled in set(re.findall(rf"\b[A-Z]\w*{re.escape(fam.noun)}\b", original)) - fam.names:
        words = [w for w in _words(spelled)[:-1] if w not in _DOUBLE_PREFIXES]
        if set(words) <= fam.qualifiers:
            continue  # another double of the same gate
        others.add(spelled)
        other_words |= set(words) - fam.qualifiers
    root = _parse(original)
    family_receivers: set[str] = set()
    other_receivers: set[str] = set()
    for node in _walk(root):
        if node.type not in ("field_declaration", "property_declaration"):
            continue
        declaration = next((c for c in node.named_children if c.type == "variable_declaration"), node)
        type_node = declaration.child_by_field_name("type")
        if type_node is None:
            continue
        simple = re.split(r"[.:<]", _text(type_node))[-1].strip().rstrip("?")
        if node.type == "property_declaration":
            named = [node.child_by_field_name("name")]
        else:
            named = [d.child_by_field_name("name") for d in declaration.named_children if d.type == "variable_declarator"]
        spelled = {_text(n) for n in named if n is not None}
        if simple in fam.names:
            family_receivers |= spelled
        elif simple in others:
            other_receivers |= spelled
    methods: dict[str, Node] = {}
    for (_, name), method in _methods(root).items():
        methods.setdefault(name, method)
    deleted = []
    for name in deleted_names:
        for token in _tokens(name, fam.noun):
            parts = frozenset(p for i, p in enumerate(name.split("_")) if i != token.segment)
            deleted.append((parts, token.state))
    return _File(fam, frozenset(others), frozenset(other_words), frozenset(family_receivers), frozenset(other_receivers), methods, deleted)


def _mentions(text: str, names: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![\w@]){re.escape(n)}(?!\w)", text) for n in names)


#: How :func:`_attributed` ties a token to the family: by the test itself
#: (its name's qualifier, its body, a deleted opposite), or only by its class.
_STRONG, _WEAK = "strong", "weak"


def _attributed(name: str, token: _Token, scan: _File) -> str | None:
    """How ``token`` of test ``name`` names the family's gate: :data:`_STRONG`
    when the test itself says so, :data:`_WEAK` when only its class does
    (another gate may be reached through a type declared elsewhere, so it is
    reported, never renamed), ``None`` when it does not."""
    before = token.before
    if before in scan.family.qualifiers and before not in scan.other_words:
        return _STRONG
    if before in scan.other_words:
        return None
    method = scan.methods.get(name)
    body = _text(method) if method is not None else ""
    if _mentions(body, scan.others | scan.other_receivers):
        return None
    if _mentions(body, scan.family.names | scan.family_receivers):
        return _STRONG
    parts = frozenset(p for i, p in enumerate(name.split("_")) if i != token.segment)
    if any(parts & deleted for deleted, state in scan.deleted if _opposite(state, token.state)):
        return _STRONG
    return _WEAK if not scan.others else None


def _opposite(a: str, b: str) -> bool:
    return (a in _ON) != (b in _ON)


def _is_test(method: Node) -> bool:
    return any(_attribute_name(a) in _TEST_ATTRIBUTES for _, a in _attributes(method))


def _false_title(tokens: list[_Token], kept: frozenset[str]) -> bool:
    """Whether a test that ran both values, and now runs only the kept one,
    is named for what happened at the removed value."""
    for token in tokens:
        if token.state not in kept:
            return True
        if token.verb in _TRANSITIONS or _TRANSITIONS & set(token.words[: token.at]):
            return True
    return False


def _renamed(name: str, tokens: list[_Token], fam: Family) -> str | None:
    """``name`` without each part that is nothing but a kept-state token, or
    ``None`` when a token shares its part with other words."""
    drop = set()
    for token in tokens:
        words = token.words
        tail = words[token.at + 1 :]
        if tail != ((token.verb,) if token.verb else ()) + (token.state,):
            return None
        head = list(words[: token.at])
        while head and head[-1] in fam.qualifiers:
            head.pop()
        if any(w not in _FILLERS for w in head):
            return None
        drop.add(token.segment)
    parts = [p for i, p in enumerate(name.split("_")) if i not in drop]
    new = "_".join(parts)
    if not new or not re.fullmatch(r"[A-Za-z_]\w*", new) or new == name:
        return None
    return new


def _several_gates(name: str, method: Node, scans: list[_File]) -> str | None:
    """Why ``name`` is now false, when it speaks of several gates
    (``EitherGateOff``) in a test the run edited, and the test named a gate of
    the family it removed. A rename would have to invent the surviving gate's
    name, so it is reported."""
    for scan in scans:
        original = scan.methods.get(name)
        if original is None or _norm(original) == _norm(method):
            continue
        for token in _tokens(name, scan.family.noun, plural=True):
            before = token.before
            if before in _QUANTIFIERS and _mentions(_text(original), scan.family.names | scan.family_receivers):
                return (
                    f"it says `{before}` gate, and this run removed one of the gates the test used, "
                    "so the name no longer says which one is left"
                )
    return None


def _norm(node: Node) -> str:
    """``node``'s text as its tokens alone: whitespace and comments gone."""
    return " ".join(_text(n) for n in _walk(node) if n.child_count == 0 and n.type != "comment")


def _statements(method: Node) -> list[str] | None:
    body = method.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    return [_norm(c) for c in body.named_children if c.type != "comment"]


#: Fewest statements two tests must share before the first assertion for the
#: shorter one to be reported: a lone ``var x = Service();`` is every test's.
_SHARED_STATEMENTS = 3


def _signature(method: Node) -> tuple[str, ...]:
    """A test's attributes and parameters, token for token: two tests that
    differ in either (``[InlineData]`` rows, a ``[Trait]``) are not one test."""
    parameters = method.child_by_field_name("parameters")
    return (*(_norm(a) for _, a in _attributes(method)), _norm(parameters) if parameters is not None else "")


_SIMPLE_STATEMENTS = ("expression_statement", "local_declaration_statement")


def _kinds(method: Node) -> list[str]:
    body = method.child_by_field_name("body")
    return [c.type for c in body.named_children if c.type != "comment"] if body is not None else []


def _duplicates(relative: str, root: Node, original: Node) -> list[tuple[str, str, str]]:
    """A test the run cut down (fewer statements than before) whose statements
    before its first assertion are, token for token, the first ones of a
    sibling test of the same type with the same attributes and parameters, and
    which began with a different arrangement before the run. A simple
    statement holding an assertion may hold the act (``var e =
    Assert.Throws(() => svc.Run());``), so it and the simple assertions after
    it must match the sibling's too.
    It may now say what the sibling says: that is reported once per pair, and
    nothing is deleted."""
    before = _methods(original)
    tests = {key: method for key, method in _methods(root).items() if _is_test(method)}
    rows: list[tuple[str, str, str]] = []
    pairs: set[frozenset[str]] = set()
    for (chain, name), method in tests.items():
        old = before.get((chain, name))
        mine, was = _statements(method), _statements(old) if old is not None else None
        if mine is None or was is None or len(mine) >= len(was):
            continue
        asserting = next((i for i, s in enumerate(mine) if "Assert" in s), len(mine))
        if asserting < _SHARED_STATEMENTS:
            continue
        if asserting == len(mine):
            continue
        # A simple statement holding an assertion may hold the act too
        # (`var e = Assert.Throws(() => svc.Run());`), so it and the simple
        # assertions after it are compared. A compound one (`using (...) {
        # Assert... }`) is not: what it asserts is what the row asks to compare.
        upto = asserting
        kinds = _kinds(method)
        while upto < len(mine) and kinds[upto] in _SIMPLE_STATEMENTS and "Assert" in mine[upto]:
            upto += 1
        for (other_chain, other), sibling in sorted(tests.items(), key=lambda kv: kv[0][1]):
            pair = frozenset((name, other))
            if other_chain != chain or other == name or pair in pairs:
                continue
            theirs = _statements(sibling)
            theirs_was = _statements(before[(chain, other)]) if (chain, other) in before else None
            if theirs is None or theirs_was is None or theirs[:upto] != mine[:upto]:
                continue
            if _signature(sibling) != _signature(method) or theirs_was[:asserting] == was[:asserting]:
                continue
            pairs.add(pair)
            rows.append((relative, DUPLICATE_TEST, f"{name}: it now begins exactly as `{other}` does, up to its first assertions; compare what each asserts"))
            break
    return rows


# --- A cut-down test folded into its sibling ----------------------------------------


def _is_false_title(name: str, method: Node, scans: list[_File], phases: set[str], kept: frozenset[str]) -> bool:
    """Whether :func:`review_names` would report ``name`` as a false title:
    a test a phase cut shortened whose name says what happened at the
    removed value. The same questions, asked in the same order."""
    if name not in phases or _several_gates(name, method, scans) is not None:
        return False
    tokens = [t for scan in scans for t in _tokens(name, scan.family.noun) if _attributed(name, t, scan)]
    return bool(tokens) and _false_title(tokens, kept)


#: What an assertion's arguments, or the resource of a ``using`` around
#: assertions, may not hold: a write, a name it declares (an ``out var`` or a
#: pattern variable is in scope to the end of the enclosing block), or code
#: deferred to a delegate (``Assert.Throws(() => svc.Run())`` holds the act).
_NOT_INERT = frozenset({
    "assignment_expression", "lambda_expression", "anonymous_method_expression",
    "declaration_expression", "declaration_pattern", "var_pattern", "local_function_statement",
})


def _inert(node: Node) -> bool:
    for n in _walk(node):
        if n.type in _NOT_INERT:
            return False
        if n.type in ("prefix_unary_expression", "postfix_unary_expression") and any(
            not c.is_named and _text(c) in ("++", "--") for c in n.children
        ):
            return False
        if n.type == "argument" and any(not c.is_named and _text(c) in ("ref", "out", "in") for c in n.children):
            return False
        if n.type == "recursive_pattern" and n.child_by_field_name("name") is not None:
            return False
    return True


def _assertion_call(expression: Node | None) -> bool:
    """``Assert.X(…)`` (or ``Xunit.Assert.X(…)``), not awaited, whose
    arguments are inert."""
    if expression is None or expression.type != "invocation_expression":
        return False
    function = expression.child_by_field_name("function")
    if function is None or function.type != "member_access_expression":
        return False
    receiver = function.child_by_field_name("expression")
    if receiver is None or _text(receiver) not in ("Assert", "Xunit.Assert"):
        return False
    arguments = expression.child_by_field_name("arguments")
    return arguments is not None and _inert(arguments)


def _is_assertion(statement: Node) -> bool:
    """Whether ``statement`` only asserts: ``Assert.X(…);``, or a plain
    ``using (expression)`` whose body is one or a block of those."""
    if statement.type == "expression_statement":
        return len(statement.named_children) == 1 and _assertion_call(statement.named_children[0])
    if statement.type != "using_statement" or any(not c.is_named and _text(c) == "await" for c in statement.children):
        return False
    body = statement.child_by_field_name("body")
    resources = [c for c in statement.named_children if body is None or (c.id != body.id and c.type != "comment")]
    if body is None or len(resources) != 1 or resources[0].type == "variable_declaration" or not _inert(resources[0]):
        return False
    if body.type == "expression_statement":
        return _is_assertion(body)
    if body.type != "block":
        return False
    inner = [c for c in body.named_children if c.type != "comment"]
    return bool(inner) and all(c.type == "expression_statement" and _is_assertion(c) for c in inner)


def _body_statements(method: Node) -> list[Node] | None:
    body = method.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    return [c for c in body.named_children if c.type != "comment"]


def _bare_fact(method: Node) -> bool:
    """One ``[Fact]``, bare (no ``Skip``, no ``Trait``, no other attribute),
    and no parameters."""
    attributes = _attributes(method)
    lists = [c for c in method.children if c.type == "attribute_list"]
    if len(attributes) != 1 or len(lists) != 1:
        return False
    _, attribute = attributes[0]
    spelled = attribute.child_by_field_name("name")
    if spelled is None or spelled.type != "identifier" or _text(spelled) != "Fact":
        return False
    if any(c.type == "attribute_argument_list" for c in attribute.named_children):
        return False
    parameters = method.child_by_field_name("parameters")
    return parameters is not None and not [c for c in parameters.named_children if c.type != "comment"]


def _shape(method: Node) -> tuple[str, ...]:
    """Modifiers, return type and parameters, token for token."""
    name = method.child_by_field_name("name")
    return tuple(
        _norm(c) for c in method.children
        if c.type not in ("attribute_list", "block", "comment", "arrow_expression_clause") and not (name is not None and c.id == name.id)
    )


@dataclass
class _Fold:
    name: str
    sibling: str
    method: Node
    #: The sibling's last statement, after which the assertions go.
    anchor: Node
    #: The cut-down test's assertion statements the sibling does not already make.
    appended: list[Node]
    #: The ``Assert`` calls in ``appended`` the sibling does not make, and
    #: those it does (a moved ``using`` block keeps both).
    new: int = 0
    repeated: int = 0


def _fold_plan(method: Node, chain: tuple[str, ...], tests: dict[tuple[tuple[str, ...], str], Node]) -> _Fold | str:
    """How the cut-down test ``method`` folds into its one sibling, or why it
    does not."""
    name = _text(method.child_by_field_name("name"))
    if not _bare_fact(method):
        return "it is not a bare [Fact] with no parameters"
    mine = _body_statements(method)
    if not mine:
        return "it has no statements"
    first = next((i for i, s in enumerate(mine) if _is_assertion(s)), None)
    if first is None:
        return "it holds no assertion"
    if first == 0:
        return "it arranges nothing before its first assertion"
    if not all(_is_assertion(s) for s in mine[first:]):
        return "it does more than assert after its first assertion"
    prefix = [_norm(s) for s in mine[:first]]
    matches = []
    for (other_chain, other), sibling in tests.items():
        if other_chain != chain or other == name:
            continue
        theirs = _body_statements(sibling)
        if theirs is not None and len(theirs) >= first and [_norm(s) for s in theirs[:first]] == prefix:
            matches.append((other, sibling, theirs))
    if not matches:
        return "no sibling arranges and acts as it does"
    if len(matches) > 1:
        return "more than one sibling arranges and acts as it does: " + ", ".join(sorted(m[0] for m in matches))
    other, sibling, theirs = matches[0]
    if not _bare_fact(sibling):
        return f"its sibling `{other}` is not a bare [Fact] with no parameters"
    if _shape(sibling) != _shape(method):
        return f"its sibling `{other}` is declared differently (modifiers or return type)"
    if not all(_is_assertion(s) for s in theirs[first:]):
        return f"its sibling `{other}` does more than assert after the statements they share"
    if mine[first].start_point[1] != theirs[0].start_point[1] or mine[0].start_point[1] != theirs[0].start_point[1]:
        return f"its sibling `{other}` is indented differently"
    apart = _preprocessor_apart(method, sibling)
    if apart:
        return apart
    made = {_norm(s) for s in theirs[first:]}
    appended = [s for s in mine[first:] if _norm(s) not in made]
    # The moved assertions now run after the sibling's own: those may not
    # call, await or construct anything the moved ones do not themselves.
    resources = {
        _norm(c) for s in appended if s.type == "using_statement"
        for c in s.named_children if c.id != (s.child_by_field_name("body") or s).id
    }
    if appended and not all(_quiet(s, resources) for s in theirs[first:]):
        return (
            f"its sibling `{other}` calls, awaits or builds something in its assertions that is not "
            "a plain read, and the moved ones would run after it"
        )
    shared = _shared_receivers(theirs[first:], resources)
    if appended and _reads_receiver(appended, shared, resources):
        return (
            f"its sibling `{other}` opens the same scope first, and the moved assertions read a "
            "member of what opens it, which may now count it twice"
        )
    asserted = {_norm(c) for s in theirs[first:] for c in _assert_calls(s)}
    calls = [_norm(c) for s in appended for c in _assert_calls(s)]
    new_calls = sum(1 for c in calls if c not in asserted)
    return _Fold(name, other, method, theirs[-1], appended, new_calls, len(calls) - new_calls)


#: Nodes that run code of the test's own: a call, an ``await``, a
#: construction, an indexer.
_EFFECTS = frozenset({
    "invocation_expression", "await_expression", "object_creation_expression",
    "implicit_object_creation_expression", "element_access_expression",
})


def _assert_calls(statement: Node) -> list[Node]:
    """The statement-level ``Assert.X(…)`` calls of an assertion statement."""
    if statement.type == "expression_statement":
        return [c for c in statement.named_children if c.type == "invocation_expression"]
    body = statement.child_by_field_name("body")
    if body is None:
        return []
    if body.type == "expression_statement":
        return _assert_calls(body)
    return [call for c in body.named_children if c.type == "expression_statement" for call in _assert_calls(c)]


#: Calls a sibling's assertions may make before the moved ones run: reads
#: that leave what they read as it was. ``Drain``, ``Dequeue`` or a channel's
#: ``ReadAsync`` would take what the moved assertions then look for.
_PURE_READS = frozenset({
    "ToString", "Contains", "Count", "Any", "All", "First", "FirstOrDefault", "Single",
    "SingleOrDefault", "Equals", "GetHashCode",
})
#: Names that ask rather than take: ``IsValid``, ``HasRows``, ``CanRead``,
#: ``ContainsKey``, and a ``Try…`` lookup, except the ones that remove or add
#: what they find.
_PURE_PREFIX = re.compile(r"(?:Is|Has|Can|Contains|Try)(?=[A-Z]|$)")
_TAKING_TRIES = frozenset({
    "TryDequeue", "TryRead", "TryTake", "TryPop", "TryReceive", "TryAdd", "TryRemove",
    "TryUpdate", "TryGetNext",
})


def _pure_read(name: str) -> bool:
    return name in _PURE_READS or (bool(_PURE_PREFIX.match(name)) and name not in _TAKING_TRIES)


def _shared_receivers(statements: list[Node], resources: set[str]) -> set[str]:
    """The receivers (``svc``, ``factory.TenantContext``) of each ``using``
    resource in ``statements`` that the moved statements open too."""
    found: set[str] = set()
    for statement in statements:
        if statement.type != "using_statement":
            continue
        body = statement.child_by_field_name("body")
        for child in statement.named_children:
            if (body is None or child.id != body.id) and _norm(child) in resources and child.type == "invocation_expression":
                _, receiver = _method_name(child)
                if receiver is not None:
                    found.add(_norm(receiver))
    return found


def _reads_receiver(statements: list[Node], receivers: set[str], resources: set[str]) -> bool:
    """Whether the moved statements read a member of a shared scope's
    receiver (``Assert.Single(svc.OpenedScopes)`` after a second
    ``using (svc.Scope("v"))``), beside the shared resource itself."""
    if not receivers:
        return False
    stack = list(statements)
    while stack:
        node = stack.pop()
        if node.parent is not None and node.parent.type == "using_statement" and _norm(node) in resources:
            continue  # the resource the sibling opens too
        if node.type == "member_access_expression":
            expression = node.child_by_field_name("expression")
            if expression is not None and _norm(expression) in receivers:
                return True
        stack.extend(node.children)
    return False
#: Of those, the ones that read a sequence, which must not be a call's
#: result: ``svc.Drain().First()`` drains.
_ON_PLAIN_RECEIVER = frozenset({"First", "FirstOrDefault", "Single", "SingleOrDefault", "Any", "All", "Count", "Contains"})


def _method_name(call: Node) -> tuple[str, Node | None]:
    function = call.child_by_field_name("function")
    receiver = None
    if function is not None and function.type == "member_access_expression":
        receiver = function.child_by_field_name("expression")
        function = function.child_by_field_name("name")
    if function is not None and function.type == "generic_name":
        function = next((c for c in function.named_children if c.type == "identifier"), None)
    return (_text(function) if function is not None and function.type == "identifier" else ""), receiver


def _awaited_lookup(node: Node) -> Node | None:
    """The call in ``(await M(…))[x]``: a value fetched once and indexed."""
    if node.type != "element_access_expression":
        return None
    inner = node.child_by_field_name("expression")
    if inner is None or inner.type != "parenthesized_expression" or len(inner.named_children) != 1:
        return None
    awaited = inner.named_children[0]
    if awaited.type != "await_expression" or len(awaited.named_children) != 1:
        return None
    call = awaited.named_children[0]
    return call if call.type == "invocation_expression" else None


def _quiet(statement: Node, resources: set[str]) -> bool:
    """Whether an assertion statement of the sibling, now run before the
    moved ones, can leave alone what they read: beside its ``Assert`` calls
    it holds names, literals and member reads, calls on the
    :data:`_PURE_READS` list or :func:`_pure_read`'s prefixes, awaited lookups ``(await M(…))[x]``, and a
    ``using`` resource the moved statements open too. Anything else (a
    ``Drain()``, ``Dequeue()``, ``await reader.ReadAsync()``, a construction)
    refuses."""
    own = {c.id for c in _assert_calls(statement)}
    allowed: set[int] = set()
    if statement.type == "using_statement":
        body = statement.child_by_field_name("body")
        allowed = {
            c.id for c in statement.named_children
            if (body is None or c.id != body.id) and _norm(c) in resources
        }
    stack = [statement]
    while stack:
        node = stack.pop()
        if node.id in allowed:
            continue
        if node.id in own:
            stack.extend(node.children)
            continue
        lookup = _awaited_lookup(node)
        if lookup is not None:
            stack.extend(lookup.children)
            subscript = node.child_by_field_name("subscript")
            if subscript is not None:
                stack.append(subscript)
            continue
        if node.type == "invocation_expression":
            name, receiver = _method_name(node)
            if not _pure_read(name):
                return False
            if name in _ON_PLAIN_RECEIVER and receiver is not None and receiver.type in _EFFECTS:
                return False
            stack.extend(node.children)
            continue
        if node.type in _EFFECTS:
            return False
        stack.extend(node.children)
    return True


def _preprocessor_apart(method: Node, sibling: Node) -> str | None:
    """Why the two tests are not under the same preprocessor conditions: a
    different enclosing directive, or a directive inside either or between
    them (``#if`` around one of them, ``#region``, ``#pragma``)."""
    def enclosing(node: Node) -> tuple[int, ...]:
        found = []
        parent = node.parent
        while parent is not None:
            if parent.type.startswith("preproc"):
                found.append(parent.id)
            parent = parent.parent
        return tuple(found)

    if enclosing(method) != enclosing(sibling):
        return "it and its sibling sit under different preprocessor directives"
    root = method
    while root.parent is not None:
        root = root.parent
    lo = min(method.start_byte, sibling.start_byte)
    hi = max(method.end_byte, sibling.end_byte)
    for node in _walk(root):
        if node.end_byte <= lo or node.start_byte >= hi:
            continue
        if node.type.startswith("preproc") and lo <= node.start_byte:
            return "a preprocessor directive lies inside or between it and its sibling"
    return None


#: Files whose spelling of a test's name is prose, which a fold may leave
#: stale. Any other file (a CI workflow, a ``.runsettings`` filter, a script)
#: may select the test by name, and keeps it.
_PROSE = (".md", ".mdx", ".txt", ".rst")


def _line_start(data: bytes, at: int) -> int:
    return data.rfind(b"\n", 0, at) + 1


def _line_end(data: bytes, at: int) -> int:
    """The byte after the newline ending the line ``at`` is on."""
    end = data.find(b"\n", at)
    return len(data) if end < 0 else end + 1


def _blank(data: bytes, start: int, end: int) -> bool:
    return not data[start:end].strip()


def _fold_edits(data: bytes, plan: _Fold, newline: bytes) -> list[tuple[int, int, bytes]] | str:
    """The byte edits of one fold over ``data`` (the parsed text, no BOM)."""
    method = plan.method
    start = method.start_byte
    # Its leading comments: each on the line directly above the next, none
    # trailing the member before it.
    previous = method.prev_named_sibling
    while previous is not None and previous.type == "comment" and previous.end_point[0] == _row(data, start) - 1:
        before = previous.prev_named_sibling
        if before is not None and before.end_point[0] == previous.start_point[0]:
            break
        start = previous.start_byte
        previous = before
    start = _line_start(data, start)
    end = _line_end(data, method.end_byte)
    if not _blank(data, _line_start(data, method.start_byte), method.start_byte):
        return "its declaration shares a line with other code"
    if not _blank(data, method.end_byte, end):
        return "its closing brace shares a line with other code"
    # One blank line goes with it, so none is left doubled.
    above = _line_start(data, max(start - 1, 0))
    if start > 0 and _blank(data, above, start):
        start = above
    elif end < len(data) and _blank(data, end, _line_end(data, end)):
        end = _line_end(data, end)
    edits = [(start, end, b"")]
    if plan.appended:
        chunks = [data[_line_start(data, s.start_byte) : s.end_byte] for s in plan.appended]
        if any(not _blank(data, _line_start(data, s.start_byte), s.start_byte) for s in plan.appended):
            return "an assertion it keeps shares a line with other code"
        # After the sibling's last statement, or after a comment trailing it
        # on its line, which stays with it.
        at = plan.anchor.end_byte
        line_end = _line_end(data, at)
        rest = data[at:line_end].rstrip(b"\r\n")
        if not rest.strip() or rest.strip().startswith(b"//"):
            at += len(rest)
        edits.append((at, at, newline + newline + newline.join(chunks)))
    return edits


def _row(data: bytes, at: int) -> int:
    return data.count(b"\n", 0, at)


def fold(
    summaries: list,
    families: list[Family],
    test_doubles: Iterable[tuple[str, str, str]],
    treatment: bool,
    repo_dir: str,
    rewrite: Callable,
) -> tuple[list, list[tuple[str, str, str]]]:
    """Fold each test a phase cut left with a false name into the one sibling
    that arranges and acts token for token as it now does, when both end in
    nothing but assertions: its assertions the sibling does not already make
    go at the end of the sibling, and the test goes with its attributes and
    leading comment. Anything else is left for :func:`review_names` to
    report. Returns the summaries and the rows."""
    if not families:
        return summaries, []
    kept = _ON if treatment else _OFF
    deleted_by_path: dict[str, list[str]] = {}
    phases_by_path: dict[str, set[str]] = {}
    for path, kind, text in test_doubles:
        if kind == TEST:
            deleted_by_path.setdefault(path, []).append(text)
        elif kind == TEST_PHASE:
            phases_by_path.setdefault(path, set()).add(text.split(":", 1)[0])
    planned: list[tuple[int, str, str, list[_Fold]]] = []
    for index, summary in enumerate(summaries):
        if file_deletion.deleted(summary) or not str(summary.path).endswith(".cs"):
            continue
        if summary.original_content == summary.content:
            continue
        resolved = str(Path(summary.path).resolve())
        relative = os.path.relpath(resolved, repo_dir)
        phases = phases_by_path.get(relative, set())
        if not phases:
            continue
        try:
            root = _parse(summary.content.removeprefix(_BOM))
            original = summary.original_content.removeprefix(_BOM)
            scans = [s for f in families if (s := _scan(original, f, deleted_by_path.get(relative, ()))) is not None]
            tests = {key: m for key, m in _methods(root).items() if _is_test(m)}
            folds: list[_Fold] = []
            for (chain, name), method in tests.items():
                if not _is_false_title(name, method, scans, phases, kept):
                    continue
                plan = _fold_plan(method, chain, tests)
                if isinstance(plan, str):
                    logger.info("%s: %s keeps its false name, not folded: %s", relative, name, plan)
                    continue
                folds.append(plan)
        except Exception as exc:  # noqa: BLE001 - never refuses the flag
            logger.info("cut-down tests of %s not folded: %s: %s", summary.path, type(exc).__name__, exc)
            continue
        # A test folded into is never itself folded, and two folds never
        # share a sibling.
        targets = [f.sibling for f in folds]
        folds = [
            f for f in folds
            if f.name not in targets and targets.count(f.sibling) == 1
        ]
        if folds:
            planned.append((index, resolved, relative, folds))
    if not planned:
        return summaries, []
    names = {f.name for _, _, _, folds in planned for f in folds}
    spelled = _spellings(names, repo_dir)
    named_files = _files_named(names, repo_dir)
    rows: list[tuple[str, str, str]] = []
    summaries = list(summaries)
    for index, resolved, relative, folds in planned:
        summary = summaries[index]
        go: list[_Fold] = []
        for plan in folds:
            declaration = plan.method.child_by_field_name("name")
            others = [
                f"{where}:{line}" for where, line, path, at, _ in spelled.get(plan.name, [])
                if not where.lower().endswith(_PROSE) and not (path == resolved and at - (3 if summary.content.startswith(_BOM) else 0) in (declaration.start_byte, declaration.start_byte + 1))
            ]
            if others:
                logger.info("%s: %s not folded: a file that is not prose names it at %s", relative, plan.name, ", ".join(others[:3]))
            elif plan.name in named_files:
                logger.info("%s: %s not folded: a file is named for it: %s", relative, plan.name, named_files[plan.name])
            else:
                go.append(plan)
        if not go:
            continue
        try:
            raw = Path(resolved).read_bytes()
        except OSError as exc:
            logger.info("%s: cut-down tests not folded: %s", relative, exc)
            continue
        try:
            done = _apply_folds(resolved, raw, summary, go, relative)
        except Exception as exc:  # noqa: BLE001 - put the file back
            logger.warning("cut-down tests of %s not folded: %s: %s", relative, type(exc).__name__, exc)
            Path(resolved).write_bytes(raw)
            continue
        if done is None:
            continue
        text, done_plans = done
        summaries[index] = rewrite(summary.path, summary.original_content, text)
        rows += [(relative, FOLDED_TEST, f"{p.name}\n{p.sibling}\n{len(p.appended)}\n{p.new}\n{p.repeated}") for p in done_plans]
    return summaries, rows


def _apply_folds(resolved: str, raw: bytes, summary, go: list[_Fold], relative: str) -> tuple[str, list[_Fold]] | None:
    """Write the folds ``go`` into ``resolved``, whose bytes are ``raw``:
    the new text (byte-order mark kept) and the folds made, or ``None`` when
    the file is left as it was."""
    bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    data = raw[bom:]
    if data.decode("utf-8") != summary.content.removeprefix(_BOM):
        return None  # not what was judged
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    edits: list[tuple[int, int, bytes]] = []
    done: list[_Fold] = []
    for plan in go:
        made = _fold_edits(data, plan, newline)
        if isinstance(made, str):
            logger.info("%s: %s not folded: %s", relative, plan.name, made)
            continue
        edits += made
        done.append(plan)
    if not done:
        return None
    spans = sorted((s, e) for s, e, _ in edits if e > s)
    if any(a_end > b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:])):
        return None
    out = data
    for start, end, replacement in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:start] + replacement + out[end:]
    text = out.decode("utf-8")
    if _errors(_parse(text)) > _errors(_parse(data.decode("utf-8"))):
        logger.warning("cut-down tests of %s not folded: the result does not parse", relative)
        return None
    git_ops.before_writing([resolved])
    Path(resolved).write_bytes(raw[:bom] + out)
    return (_BOM if bom else "") + text, done


def review_names(
    summaries: list,
    families: list[Family],
    test_doubles: Iterable[tuple[str, str, str]],
    treatment: bool,
    repo_dir: str,
    rewrite: Callable,
) -> tuple[list, list[tuple[str, str, str]]]:
    """Rename or report each kept test whose name states a removed gate's
    state (see the module docstring). Returns the summaries and the rows."""
    if not families:
        return summaries, []
    kept = _ON if treatment else _OFF
    deleted_by_path: dict[str, list[str]] = {}
    phases_by_path: dict[str, set[str]] = {}
    for path, kind, text in test_doubles:
        if kind == TEST:
            deleted_by_path.setdefault(path, []).append(text)
        elif kind == TEST_PHASE:
            phases_by_path.setdefault(path, set()).add(text.split(":", 1)[0])
    rows: list[tuple[str, str, str]] = []
    #: ``path -> [(identifier start, end, old, new)]``, and every old name.
    renames: dict[str, list[tuple[int, int, str, str]]] = {}
    planned: list[tuple[str, int, str, str]] = []
    #: ``(path, identifier start) -> (type name, type start, type end)``.
    owners: dict[tuple[str, int], tuple[str, int, int]] = {}
    for index, summary in enumerate(summaries):
        if file_deletion.deleted(summary) or not str(summary.path).endswith(".cs"):
            continue
        if summary.original_content == summary.content:
            continue
        resolved = str(Path(summary.path).resolve())
        relative = os.path.relpath(resolved, repo_dir)
        content = summary.content.removeprefix(_BOM)
        try:
            root = _parse(content)
            scans = [s for f in families if (s := _scan(summary.original_content.removeprefix(_BOM), f, deleted_by_path.get(relative, ()))) is not None]
        except Exception as exc:  # noqa: BLE001 - reporting never refuses the flag
            logger.info("names in %s not judged: %s: %s", summary.path, type(exc).__name__, exc)
            continue
        if not scans:
            continue
        members = _member_names(root)
        try:
            rows += _duplicates(relative, root, _parse(summary.original_content.removeprefix(_BOM)))
        except Exception as exc:  # noqa: BLE001 - reporting never refuses the flag
            logger.info("duplicates in %s not judged: %s: %s", summary.path, type(exc).__name__, exc)
        for (chain, name), method in _methods(root).items():
            if not _is_test(method):
                continue
            several = _several_gates(name, method, scans)
            if several is not None:
                rows.append((relative, GATE_NAMED, f"{name}: {several}"))
                continue
            judged = [(t, how) for scan in scans for t in _tokens(name, scan.family.noun) if (how := _attributed(name, t, scan))]
            tokens = [t for t, _ in judged]
            if not tokens:
                continue
            if name in phases_by_path.get(relative, set()) and _false_title(tokens, kept):
                rows.append((relative, FALSE_TITLE, name))
                continue
            if any(t.state not in kept for t in tokens):
                rows.append((relative, GATE_NAMED, f"{name}: it names the state this pull request deletes"))
                continue
            if any(how != _STRONG for _, how in judged):
                rows.append((relative, GATE_NAMED, f"{name}: nothing in the test ties the name to the removed gate"))
                continue
            new = _renamed(name, tokens, scans[0].family)
            if new is None:
                rows.append((relative, GATE_NAMED, f"{name}: the state is part of a longer word"))
                continue
            if new in members.get(chain, set()) or chain[-1:] == (new,) or any(
                p == resolved and n == new for p, _, _, n in planned
            ):
                rows.append((relative, GATE_NAMED, f"{name}: `{new}` is already taken"))
                continue
            identifier = method.child_by_field_name("name")
            owner = method.parent.parent if method.parent is not None else None
            if owner is not None and owner.type in _TYPE_DECLARATIONS and owner.child_by_field_name("name") is not None:
                owners[(resolved, identifier.start_byte)] = (_text(owner.child_by_field_name("name")), owner.start_byte, owner.end_byte)
            planned.append((resolved, index, name, new))
            renames.setdefault(resolved, []).append((identifier.start_byte, identifier.end_byte, name, new))
    if not planned:
        return summaries, rows
    olds = {old for _, _, old, _ in planned}
    spelled = _spellings(olds, repo_dir)
    named_files = _files_named(olds, repo_dir)
    summaries = list(summaries)
    for resolved, edits in renames.items():
        index = next(i for p, i, _, _ in planned if p == resolved)
        summary = summaries[index]
        relative = os.path.relpath(resolved, repo_dir)
        try:
            raw = Path(resolved).read_bytes()
            bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
            tree = _parse(raw[bom:].decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            # Judged before anything is written, so a file that cannot be read
            # as it was judged leaves every file as it was.
            logger.info("references to renamed tests in %s not judged: %s: %s", relative, type(exc).__name__, exc)
            continue
        go: list[tuple[int, int, str, str]] = []
        #: The method's own references (``nameof``, ``cref``) that move with it.
        along: list[tuple[int, int, str, str]] = []
        for start, end, old, new in edits:
            owner, owner_start, owner_end = owners.get((resolved, start), ("", -1, -1))
            blockers: list[str] = []
            moving: list[tuple[int, int, str, str]] = []
            for where, line, spelled_path, at, to in spelled.get(old, []):
                if spelled_path == resolved and at == start + bom:
                    continue  # the declaration being renamed
                if spelled_path == resolved and owner and _own_reference(tree, at - bom, to - bom, owner, owner_start + bom <= at < owner_end + bom):
                    moving.append((at - bom, to - bom, old, new))
                else:
                    blockers.append(f"{where}:{line}")
            taken = _declared_in_hierarchy(resolved, start, new, repo_dir)
            if blockers:
                more = f", and {len(blockers) - 3} more" if len(blockers) > 3 else ""
                rows.append((relative, GATE_NAMED, f"{old}: the name is also spelled at {', '.join(blockers[:3])}{more}"))
            elif old in named_files:
                # A snapshot library finds its file by the test's name.
                rows.append((relative, GATE_NAMED, f"{old}: a file is named for it: {named_files[old]}"))
            elif taken is not None:
                # Another part of a partial class, or a base class, may declare it.
                rows.append((relative, GATE_NAMED, f"{old}: `{new}` is already declared at {taken}"))
            else:
                go.append((start, end, old, new))
                along += moving
        if not go:
            continue
        git_ops.before_writing([resolved])
        data = Path(resolved).read_bytes()
        offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        if data[offset:].decode("utf-8") != summary.content.removeprefix(_BOM):
            continue  # not what was judged
        for start, end, old, new in sorted(go + along, reverse=True):
            data = data[: start + offset] + new.encode() + data[end + offset :]
        Path(resolved).write_bytes(data)
        summaries[index] = rewrite(summary.path, summary.original_content, data.decode("utf-8"))
        rows += [(relative, RENAMED_METHOD, f"{old}\n{new}") for _, _, old, new in sorted(go)]
    return summaries, rows


def _member_names(root: Node) -> dict[tuple[str, ...], set[str]]:
    """Each type's member names, by its chain of type names."""
    found: dict[tuple[str, ...], set[str]] = {}
    for node in _walk(root):
        if node.type not in ("class_declaration", "struct_declaration", "record_declaration"):
            continue
        chain = _type_chain(node) + (_text(node.child_by_field_name("name")),)
        names = found.setdefault(chain, set())
        body = node.child_by_field_name("body")
        for member in body.named_children if body is not None else ():
            spelled = member.child_by_field_name("name")
            if spelled is not None:
                names.add(_text(spelled))
            for declaration in (c for c in member.named_children if c.type == "variable_declaration"):
                for declarator in (d for d in declaration.named_children if d.type == "variable_declarator"):
                    n = declarator.child_by_field_name("name")
                    if n is not None:
                        names.add(_text(n))
    return found


_TYPE_DECLARATIONS = ("class_declaration", "struct_declaration", "record_declaration", "interface_declaration")


def _declared_in_hierarchy(path: str, at: int, name: str, repo_dir: str) -> str | None:
    """``"path:line"`` where a type the method named at byte ``at`` of
    ``path`` (byte-order mark not counted)
    belongs to declares a member called ``name``: its own type, another part
    of it (``partial``, by name, in any file) or a type it derives from that
    the checkout declares (CS0111, or CS0108 under warnings-as-errors)."""
    root = _parse(Path(path).read_bytes().decode("utf-8"))
    method = next(
        (n for n in _walk(root) if n.type == "method_declaration" and (m := n.child_by_field_name("name")) is not None
         and m.start_byte == at),
        None,
    )
    owner = method.parent.parent if method is not None and method.parent is not None else None
    if owner is None or owner.type not in _TYPE_DECLARATIONS:
        return "an unknown type"
    wanted = [_text(owner.child_by_field_name("name"))]
    seen: set[str] = set()
    from flag_cleanup import source_index

    files = [f.resolved for f in source_index.files(repo_dir, {".cs"}, with_ignored=True)]
    checkout = source_index.checkout_root(repo_dir)
    while wanted:
        type_name = wanted.pop()
        if type_name in seen:
            continue
        seen.add(type_name)
        spelled = re.compile(rf"\b(?:class|struct|record|interface)\s+{re.escape(type_name)}\b".encode())
        for file in files:
            try:
                text = Path(file).read_bytes()
            except OSError:
                continue
            if not spelled.search(text):
                continue
            tree = _parse(text.decode("utf-8", "replace"))
            for node in _walk(tree):
                if node.type not in _TYPE_DECLARATIONS:
                    continue
                type_spelled = node.child_by_field_name("name")
                if type_spelled is None or _text(type_spelled) != type_name:
                    continue
                for member, line in _members(node):
                    if member == name:
                        return f"{os.path.relpath(file, checkout)}:{line}"
                bases = next((c for c in node.children if c.type == "base_list"), None)
                for base in bases.named_children if bases is not None else ():
                    simple = re.split(r"[.:]", re.sub(r"<.*", "", _text(base)))[-1].strip()
                    if simple:
                        wanted.append(simple)
    return None


def _members(declaration: Node) -> list[tuple[str, int]]:
    """``(name, line)`` of each member a type declaration declares."""
    found: list[tuple[str, int]] = []
    body = declaration.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        spelled = member.child_by_field_name("name")
        if spelled is not None:
            found.append((_text(spelled), spelled.start_point[0] + 1))
        for variables in (c for c in member.named_children if c.type == "variable_declaration"):
            for declarator in (d for d in variables.named_children if d.type == "variable_declarator"):
                n = declarator.child_by_field_name("name")
                if n is not None:
                    found.append((_text(n), n.start_point[0] + 1))
    return found


def _spellings(names: set[str], repo_dir: str) -> dict[str, list[tuple[str, int, str, int, int]]]:
    """``name -> [(path, line, resolved path, start, end), …]`` for each
    spelling of each name in the checkout's text files, paths from the
    checkout's root and offsets in the file's bytes."""
    from flag_cleanup import source_index
    from flag_cleanup.csharp_residue import _text_files

    root = source_index.checkout_root(repo_dir)
    pattern = re.compile(rb"(?<!\w)@?(" + b"|".join(re.escape(n.encode()) for n in sorted(names)) + rb")(?!\w)")
    seen: dict[str, list[tuple[str, int, str, int, int]]] = {}
    for path in _text_files(repo_dir):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        for match in pattern.finditer(data):
            line = data.count(b"\n", 0, match.start()) + 1
            seen.setdefault(match.group(1).decode(), []).append(
                (os.path.relpath(path, root), line, str(path.resolve()), match.start(1), match.end(1))
            )
    return seen


_CREF_BEFORE = re.compile(r'cref\s*=\s*"((?:@?[A-Za-z_]\w*\.)*)@?$')
_CREF_AFTER = re.compile(r'(?:\([^()"]*\))?"')


def _own_reference(root: Node, at: int, to: int, owner: str, inside: bool) -> bool:
    """Whether the spelling at bytes ``[at, to)`` of ``root``'s source is a
    reference the compiler checks that moves with the method: the argument of
    a ``nameof`` (``nameof(Name)``, written bare it must lie ``inside`` the
    owner's own span; ``nameof(Owner.Name)``, qualified by the owner) or the
    ``cref`` of a ``///`` comment. Everything else (a string, a raw string, an
    interpolated string's text, any other comment) is prose and blocks."""
    node = root.descendant_for_byte_range(at, to)
    if node is None:
        return False
    if node.type == "comment":
        # Offsets are bytes, so slice bytes: a multi-byte letter earlier in the
        # line moves the match in characters but not in bytes.
        text = node.text or b""
        if not text.startswith(b"///"):
            return False
        offset = at - node.start_byte
        found = _CREF_BEFORE.search(text[:offset].decode("utf-8", "ignore"))
        if found is None or _CREF_AFTER.match(text[offset + (to - at) :].decode("utf-8", "ignore")) is None:
            return False
        qualifier = [q.lstrip("@") for q in found.group(1).split(".") if q]
        return inside if not qualifier else qualifier[-1] == owner
    if node.type != "identifier" or node.start_byte not in (at, at - 1) or node.end_byte != to:
        return False
    parent = node.parent
    qualifier: list[str] = []
    if parent is not None and parent.type == "member_access_expression" and parent.child_by_field_name("name") == node:
        qualifier = [q.strip().lstrip("@") for q in _text(parent.child_by_field_name("expression")).split(".")]
        parent = parent.parent
    if parent is None or parent.type != "argument":
        return False
    arguments = parent.parent
    call = arguments.parent if arguments is not None and arguments.type == "argument_list" else None
    if call is None or call.type != "invocation_expression" or len(arguments.named_children) != 1:
        return False
    function = call.child_by_field_name("function")
    if function is None or _text(function) != "nameof":
        return False
    return inside if not qualifier else qualifier[-1] == owner


def _files_named(names: set[str], repo_dir: str) -> dict[str, str]:
    """``name -> path`` of a file in the checkout whose name spells each name
    as a word of its own (``_``, ``.`` and ``-`` divide words): a snapshot
    library's ``Class.Method.verified.txt``, ``.approved.txt`` or
    ``__snapshots__/Class.Method.snap``, or a theory's
    ``Class.Method_arg=1.verified.txt``."""
    from flag_cleanup import source_index
    from flag_cleanup.csharp_residue import _text_files

    if not names:
        return {}
    root = source_index.checkout_root(repo_dir)
    pattern = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in sorted(names)) + r")(?![A-Za-z0-9])")
    found: dict[str, str] = {}
    for path in _text_files(repo_dir):
        for match in pattern.finditer(path.name):
            found.setdefault(match.group(1), os.path.relpath(path, root))
    return found


def review(
    plan,
    statuses: list,
    summaries: list,
    test_doubles: Iterable[tuple[str, str, str]],
    treatment: bool,
    repo_dir: str,
    rewrite: Callable,
    engine: Callable[[list[str], str], object],
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """The theory rewrite, then the names, for a C# run. Never refuses: a
    failure is logged and leaves the files as they were."""
    from flag_cleanup import wrapper_inline as wi

    rows: list[tuple[str, str, str]] = []
    try:
        summaries, rows = collapse(summaries, repo_dir, rewrite, engine)
    except Exception as exc:  # noqa: BLE001
        logger.warning("theories left as they were: %s: %s", type(exc).__name__, exc)
    complete = {f.wrapper for f in statuses if f.reason == wi.COMPLETE}
    families = [
        f
        for w in getattr(plan, "wrappers", ())
        if w.language == "csharp" and w.interface is not None and w.label in complete
        and (f := family(w.interface.name, [w.owner, *(s.owner for s in w.siblings)])) is not None
    ]
    try:
        summaries, folded = fold(summaries, families, test_doubles, treatment, repo_dir, rewrite)
        rows += folded
    except Exception as exc:  # noqa: BLE001
        logger.warning("cut-down tests left as they were: %s: %s", type(exc).__name__, exc)
    try:
        summaries, named = review_names(summaries, families, test_doubles, treatment, repo_dir, rewrite)
        rows += named
    except Exception as exc:  # noqa: BLE001
        logger.warning("test names left as they were: %s: %s", type(exc).__name__, exc)
    return summaries, tuple(rows)
