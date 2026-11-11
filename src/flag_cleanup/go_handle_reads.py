"""A flag handle handed to a local closure that only forwards it to an accessor.

``boolFlag := func(f featureflags.BoolFlag) bool { return flags.BoolFlag(ctx, f) }``
followed by ``boolFlag(featureflags.X)`` reads ``X`` through the accessor, but
the call names the closure, not the accessor, so the reader rules cannot fold
it and the handle's declaration is kept. This pass runs BEFORE the engine, on
the customer's bytes, and writes the read the closure stands for in its place::

    return boolFlag(featureflags.X)     ->     return flags.BoolFlag(ctx, featureflags.X)

That is a pure inline of a function with no other effect, so it is correct
whether or not the flag is then folded. The closure is deleted when no call is
left (Go rejects an unused local), and kept while another handle or key still
goes through it. A closure is a forwarder only when ALL of these hold:

* it is declared as a statement, ``name := func(...)`` or ``var name = func(...)``,
  with one name, one plain parameter and a result;
* its body is exactly ``return <read>``, a configured accessor (or a built-in
  reader) called with the parameter, bare, as its key, the other arguments and
  the receiver being names, selectors or literals (nothing that calls), the
  whole read on one line, no comment in the literal;
* every other mention of ``name`` in the function is a direct call with one
  argument, resolved to this closure (not a reassignment, a value handed on, a
  ``go``/``defer`` call or a field name);
* each name the body reads, resolved at the call, is the binding it resolved to
  inside the closure (a ``ctx`` redeclared between the two is not the same).

Only a call whose argument names a declaration of this flag's key is inlined
(``names``): the rest keep their closure, as before.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import go_scope, key_const, mock_stubs
from flag_cleanup.syntax import _parse, _walk

_INERT = frozenset(
    {
        "identifier", "selector_expression", "interpreted_string_literal", "raw_string_literal",
        "int_literal", "float_literal", "true", "false", "nil", "rune_literal", "imaginary_literal",
    }
)
_FUNCTIONS = ("function_declaration", "method_declaration", "func_literal")
KIND = "closure forwarding a handle"


def _text(node: Node) -> str:
    return node.text.decode("utf-8")


@dataclass(frozen=True)
class _Forwarder:
    statement: Node
    name: Node
    #: The call the closure's body makes, and the identifier in it that is the key.
    read: Node
    key: Node


def _chain(node: Node) -> bool:
    """``a`` or ``a.b.c``: names only."""
    while node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        if operand is None:
            return False
        node = operand
    return node.type == "identifier"


def _forwarder(statement: Node, readers: dict[int, frozenset[str]]) -> _Forwarder | None:
    if statement.type == "short_var_declaration":
        left, right = statement.child_by_field_name("left"), statement.child_by_field_name("right")
        names = [] if left is None else [c for c in left.named_children if c.type != "comment"]
        values = [] if right is None else [c for c in right.named_children if c.type != "comment"]
    elif statement.type == "var_declaration":
        specs = [c for c in statement.named_children if c.type == "var_spec"]
        if len(specs) != 1:
            return None
        names = specs[0].children_by_field_name("name")
        value = specs[0].child_by_field_name("value")
        values = [] if value is None else [c for c in value.named_children if c.type != "comment"]
        if specs[0].child_by_field_name("type") is not None:
            return None
    else:
        return None
    if len(names) != 1 or len(values) != 1 or names[0].type != "identifier" or values[0].type != "func_literal":
        return None
    literal = values[0]
    if any(n.type == "comment" for n in _walk(literal)):
        return None
    parameters, result, body = (literal.child_by_field_name(k) for k in ("parameters", "result", "body"))
    if parameters is None or result is None or body is None:
        return None
    declared = [c for c in parameters.named_children if c.type != "comment"]
    if len(declared) != 1 or declared[0].type != "parameter_declaration":
        return None
    parameter = declared[0].children_by_field_name("name")
    if len(parameter) != 1:
        return None
    statements = [c for c in body.named_children if c.type != "comment"]
    if len(statements) != 1:
        return None
    only = statements[0]
    if only.type == "statement_list":
        inner = [c for c in only.named_children if c.type != "comment"]
        if len(inner) != 1:
            return None
        only = inner[0]
    if only.type != "return_statement":
        return None
    returned = [c for c in only.named_children if c.type != "comment"]
    if len(returned) != 1 or returned[0].type == "expression_list" and len(returned[0].named_children) != 1:
        return None
    call = returned[0]
    if call.type == "expression_list":
        call = call.named_children[0]
    if call.type != "call_expression" or "\n" in _text(call) or call.child_by_field_name("type_arguments") is not None:
        return None
    function = call.child_by_field_name("function")
    arguments = call.child_by_field_name("arguments")
    if function is None or arguments is None:
        return None
    callee = function.child_by_field_name("field") if function.type == "selector_expression" else function
    if callee is None or callee.type not in ("identifier", "field_identifier"):
        return None
    if function.type == "selector_expression":
        operand = function.child_by_field_name("operand")
        if operand is None or not _chain(operand):
            return None
    elif function.type != "identifier":
        return None
    args = [c for c in arguments.named_children if c.type != "comment"]
    if any(a.type not in _INERT or (a.type == "selector_expression" and not _chain(a)) for a in args):
        return None
    if any(n.type == "call_expression" and n.id != call.id for n in _walk(call)):
        return None
    if any(n.type == "variadic_argument" for n in _walk(arguments)):
        return None
    keys = [i for i, a in enumerate(args) if a.type == "identifier" and _text(a) == _text(parameter[0])]
    uses = [n for n in _walk(literal) if n.type == "identifier" and _text(n) == _text(parameter[0]) and n.id != parameter[0].id]
    if len(keys) != 1 or len(uses) != 1 or keys[0] not in (0, 1) or _text(callee) not in readers.get(keys[0], frozenset()):
        return None
    return _Forwarder(statement, names[0], call, args[keys[0]])


def _outermost(node: Node) -> Node | None:
    found = None
    current = node.parent
    while current is not None:
        if current.type in _FUNCTIONS:
            found = current
        current = current.parent
    return found


def _handle_argument(arg: Node, names: frozenset[str]) -> bool:
    """``X`` or ``pkg.X`` with ``X`` a declaration of this flag's key."""
    if arg.type == "identifier":
        return _text(arg) in names
    if arg.type == "selector_expression":
        operand, field_ = arg.child_by_field_name("operand"), arg.child_by_field_name("field")
        return operand is not None and operand.type == "identifier" and field_ is not None and _text(field_) in names
    return False


def _same_free_names(forwarder: _Forwarder, call: Node) -> bool:
    """Whether every name the closure's read mentions means the same thing at ``call``."""
    callee = call.child_by_field_name("function")
    if callee is None:
        return False
    for node in _walk(forwarder.read):
        if node.type != "identifier" or node.id == forwarder.key.id:
            continue
        name = _text(node)
        inside = {b.id for b in go_scope.local_binders(node, name)}
        outside = {b.id for b in go_scope.local_binders(callee, name)}
        if inside != outside:
            return False
    return True


def _span_of_line(data: bytes, node: Node) -> tuple[int, int]:
    """The whole lines ``node`` occupies, with the blank line left beside them."""
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    if data[start:node.start_byte].strip():
        return node.start_byte, node.end_byte
    end = data.find(b"\n", node.end_byte)
    end = len(data) if end == -1 else end + 1
    before = data[:start]
    if (before.rstrip(b" \t").endswith(b"{\n") or before.endswith(b"\n\n")) and data[end:end + 1] == b"\n":
        end += 1
    return start, end


def _other_mentions(root: Node, names: frozenset[str]) -> bool:
    """Whether the file mentions a declaration of the key other than as an
    argument of a call (``X.Fallback()``, a registry, the declaration itself).
    The handle is kept for such a mention, so its reads would not fold; the
    file is then left as the customer wrote it rather than half rewritten."""
    for node in _walk(root):
        if node.type not in ("identifier", "field_identifier") or _text(node) not in names:
            continue
        expr = node
        parent = node.parent
        if parent is not None and parent.type == "selector_expression":
            operand = parent.child_by_field_name("operand")
            if operand is not None and operand.id == node.id:
                return True  # `X.Method`: a use of the handle's value
            expr = parent
        holder = expr.parent
        if holder is None or holder.type != "argument_list":
            return True
    return False


def rewrite(
    source: str,
    names: frozenset[str],
    readers: dict[int, frozenset[str]],
) -> tuple[str, list[str]]:
    """``(rewritten source, the calls inlined)``."""
    if not any(n in source for n in names):
        return source, []
    root = _parse(source, "go")
    if root.has_error:
        return source, []
    if _other_mentions(root, names):
        return source, []
    data = source.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    inlined: list[str] = []
    for statement in _walk(root):
        if statement.type not in ("short_var_declaration", "var_declaration"):
            continue
        parent = statement.parent
        if parent is None or parent.type not in ("statement_list", "block"):
            continue
        forwarder = _forwarder(statement, readers)
        function = None if forwarder is None else _outermost(statement)
        if forwarder is None or function is None:
            continue
        name = _text(forwarder.name)
        calls: list[Node] = []
        eligible = True
        for node in _walk(function):
            if node.type not in ("identifier", "field_identifier", "type_identifier", "label_name") or _text(node) != name:
                continue
            if node.id == forwarder.name.id:
                continue
            call = node.parent
            if (
                node.type != "identifier" or call is None or call.type != "call_expression"
                or call.child_by_field_name("function") is None or call.child_by_field_name("function").id != node.id
                or call.child_by_field_name("type_arguments") is not None
                or call.parent is None or call.parent.type in ("go_statement", "defer_statement")
                or [b.id for b in go_scope.local_binders(node, name)] != [forwarder.name.id]
            ):
                eligible = False
                break
            arguments = [c for c in call.child_by_field_name("arguments").named_children if c.type != "comment"]
            if len(arguments) != 1 or any(n.type == "variadic_argument" for n in _walk(call.child_by_field_name("arguments"))):
                eligible = False
                break
            calls.append(call)
        if not eligible:
            continue
        # Not inside the closure's own text (it has no such mention), and in
        # source order with the declaration first.
        chosen = [
            c for c in calls
            if _handle_argument(c.child_by_field_name("arguments").named_children[0], names) and _same_free_names(forwarder, c)
        ]
        if not chosen:
            continue
        for call in chosen:
            argument = call.child_by_field_name("arguments").named_children[0]
            body = (
                data[forwarder.read.start_byte:forwarder.key.start_byte]
                + data[argument.start_byte:argument.end_byte]
                + data[forwarder.key.end_byte:forwarder.read.end_byte]
            )
            edits.append((call.start_byte, call.end_byte, body))
            inlined.append(" ".join(_text(call).split()))
        if len(chosen) == len(calls):
            start, end = _span_of_line(data, statement)
            edits.append((start, end, b""))
    if not inlined:
        return source, []
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8"), inlined


def readers_of(accessors: Iterable[str]) -> dict[int, frozenset[str]]:
    """The callee names a key at argument 0 or 1 is read through."""
    configured = frozenset(a.split("[", 1)[0] for a in accessors)
    return {
        0: frozenset(key_const._GO_FLAG_READ_CALLEES) | configured,
        1: frozenset(key_const._GO_SECOND_ARGUMENT_READ_CALLEES) | configured,
    }


@dataclass
class Prepass(mock_stubs.Prepass):
    """Inlines each forwarding closure's calls of this flag's handles before the engine."""

    #: The names this flag's key is declared under (handles and constants).
    names: frozenset[str] = field(default_factory=frozenset)

    def apply(self, paths: list[str]) -> None:
        readers = readers_of(self.accessors)
        for path in paths:
            try:
                source = mock_stubs._read_exact(path)
            except (OSError, UnicodeDecodeError):
                continue
            key = os.path.abspath(path)
            if key in self.originals:
                continue
            resolved, calls = rewrite(source, self.names, readers)
            if not calls:
                continue
            self.spelled[key] = path
            self.originals[key] = source
            self.removals[key] = [mock_stubs.Removal(KIND, c) for c in calls]
            self.edited[key] = resolved
            mock_stubs._write_exact(path, resolved)

    def report(self, repo_dir: str) -> tuple[tuple[str, str, str], ...]:
        return ()


def declared_names(paths: Iterable[str], flag_key: str) -> frozenset[str]:
    """Every package-level name the files declare for ``flag_key`` (a handle or a constant)."""
    from flag_cleanup import cross_file_go

    found: set[str] = set()
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if flag_key not in source:
            continue
        found.update(d.name for d in cross_file_go.declarations(path, source, flag_key))
    return frozenset(found)
