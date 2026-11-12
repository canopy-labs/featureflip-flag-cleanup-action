"""What a folded Go read evaluated besides the flag, kept as ``_ = <expr>``.

The fold replaces a whole read with one literal: ``gc().IsFlagEnabled(ctx, "KEY",
org())`` becomes ``true``, and ``gc()`` and ``org()`` no longer run. Go evaluates a
method call's receiver, then its arguments, before the call, so each of them is
an effect the customer's program had whatever the flag said. This pass runs
BEFORE the engine, on the bytes it will see, and writes every receiver and
argument that can do work (holds a call or a channel receive outside a function
literal) as a statement of its own, on the lines before the statement holding
the read, in that order::

    if gc().IsFlagEnabled(ctx, "KEY", org()) {      _ = gc()
        …                                       ->  _ = org()
    }                                               if gc().IsFlagEnabled(ctx, "KEY", org()) {

The read is left whole; the engine folds it as before, and the kept statements
run where the read's evaluation started. A receiver or argument that does no
work (a name, a field chain, a literal, an operator on those, a builtin that can
be constant such as ``len`` unless its package declares that name) is left to the
fold unchanged, so ``s.c.IsFlagEnabled(…)`` folds exactly as it did, and
``1<<len(arr)`` is not kept as ``_ = 1 << len(arr)``, which would give the
constant the default type. Each kept expression is written as gofmt writes it
alone on the right of ``_ =`` (:func:`kept_text`: go/printer spaces binary
operators by depth, so ``"org-"+org()`` as an argument is ``"org-" + org()``
there), with the file's own line endings.

Moving work to before the statement is only the same program when nothing in
that statement runs before the read, the read always runs, and every name the
kept text reads means the same variable there. So the read must be reached from
the statement through positions evaluated first and unconditionally: the
initializer of an ``if``/``switch``, or its condition when it has no
initializer (an initializer can bind or set a name the condition reads), and
that ``if``/``switch`` a statement of a block (not an ``else if``), the value of a ``:=``,
``=``, ``var`` (one spec, inside a function), ``return`` or ``ch <-``, an
argument or an element, the left operand of ``&&``/``||``, an operand of ``!``
or of another operator, parentheses. Everything evaluated to the read's left on
the way must do no work. The statement must start its line, and each kept
expression must fit on one line. Any other read that does work is a refusal
with the reason (:func:`refusal_reason`, asked of the engine's output, so a read
this pass did not rewrite cannot be folded with its work dropped).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from tree_sitter import Node

from flag_cleanup import go_handle_reads, mock_stubs
from flag_cleanup.syntax import _parse, _walk

KIND = "kept receiver or argument"

#: Where a statement of a function body sits.
_BODIES = frozenset({"statement_list", "block"})
#: Statements whose evaluation can be preceded by a statement of its own.
_STATEMENTS = frozenset(
    {
        "expression_statement", "short_var_declaration", "assignment_statement", "var_declaration",
        "return_statement", "if_statement", "expression_switch_statement", "send_statement",
    }
)
#: Containers where every child left of the read is evaluated before it.
_ORDERED = frozenset(
    {
        "argument_list", "expression_list", "literal_value", "keyed_element", "literal_element",
        "parenthesized_expression", "composite_literal",
    }
)


def _text(node: Node) -> str:
    return node.text.decode("utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class _Hoist:
    statement: Node
    #: The receiver and arguments that do work, in evaluation order, as gofmt
    #: writes each on the right of ``_ =``.
    expressions: tuple[str, ...]
    call: Node


@dataclass(frozen=True)
class _Declined:
    call: Node
    reason: str


#: Builtins that do no work and can be constant: ``1 << len(arr)`` is a constant,
#: and as ``_ = 1 << len(arr)`` it takes the default type and can overflow.
_CONSTANT_BUILTINS = frozenset({"len", "cap", "min", "max", "real", "imag", "complex"})


def _declared_names(root: Node) -> frozenset[str]:
    """Every name ``root`` declares anywhere: a builtin it shadows is a call like any other."""
    found: set[str] = set()
    for node in _walk(root):
        if node.type in ("function_declaration", "var_spec", "const_spec", "type_spec",
                         "parameter_declaration", "variadic_parameter_declaration", "type_parameter_declaration"):
            found.update(_text(n) for n in node.children_by_field_name("name"))
        elif node.type in ("short_var_declaration", "range_clause", "receive_statement"):
            left = node.child_by_field_name("left")
            if left is not None:
                found.update(_text(n) for n in left.named_children if n.type == "identifier")
        elif node.type == "import_spec":
            name = node.child_by_field_name("name")
            if name is not None:
                found.add(_text(name))
    return frozenset(found)


#: A file that never spells one of the names cannot declare it; the parse decides
#: the rest (a grouped `var (` puts the keyword and the name on different lines).
_SHADOW_HINT = re.compile(rb"\b(?:len|cap|min|max|real|imag|complex)\b")


def _package_clause(root: Node) -> str | None:
    clause = next((c for c in root.named_children if c.type == "package_clause"), None)
    if clause is None:
        return None
    name = next((c for c in clause.named_children if c.type == "package_identifier"), None)
    return _text(name) if name is not None else None


def package_shadows(path: str | None, source: str) -> frozenset[str]:
    """The builtins that can be constant (``len``, ``max``, …) which a package-level
    declaration in another file of ``path``'s package redeclares: every ``.go``
    file in its directory with the same package clause (a test file in the
    package's own clause counts, an external ``_test`` package does not).
    ``unsafe`` is an import, so file-scoped, and is not asked about here."""
    if path is None:
        return frozenset()
    package = _package_clause(_parse(source, "go"))
    directory = os.path.dirname(os.path.abspath(path))
    found: set[str] = set()
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return frozenset()
    for entry in entries:
        sibling = os.path.join(directory, entry)
        if not entry.endswith(".go") or sibling == os.path.abspath(path) or not os.path.isfile(sibling):
            continue
        try:
            data = open(sibling, "rb").read()
        except OSError:
            continue
        if not _SHADOW_HINT.search(data):
            continue
        try:
            root = _parse(data.decode("utf-8"), "go")
        except UnicodeDecodeError:
            continue
        if _package_clause(root) != package:
            continue
        for declaration in root.named_children:
            specs: list[Node] = []
            if declaration.type == "function_declaration":
                specs = [declaration]
            elif declaration.type in ("var_declaration", "const_declaration", "type_declaration"):
                specs = [n for n in _walk(declaration) if n.type in ("var_spec", "const_spec", "type_spec", "type_alias")]
            for spec in specs:
                found.update(_text(n) for n in spec.children_by_field_name("name"))
    return frozenset(found) & _CONSTANT_BUILTINS


def _is_constant_builtin(call: Node, shadowed: frozenset[str]) -> bool:
    function = call.child_by_field_name("function")
    if function is None:
        return False
    if function.type == "identifier":
        return _text(function) in _CONSTANT_BUILTINS and _text(function) not in shadowed
    if function.type == "selector_expression":
        operand = function.child_by_field_name("operand")
        return (
            operand is not None and operand.type == "identifier" and _text(operand) == "unsafe"
            and "unsafe" not in shadowed
        )
    return False


def does_work(node: Node, shadowed: frozenset[str] = frozenset()) -> bool:
    """Whether evaluating ``node`` calls something or receives from a channel.
    A function literal's body is not evaluated there; calling one is. A builtin
    that can be constant (``len``, ``unsafe.Sizeof``, …) does no work of its own,
    unless its file or package declares the name; its arguments still count. A generic
    type's "conversion" (``f[int](x)``) may be a call of a generic function."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "call_expression" and not _is_constant_builtin(current, shadowed):
            return True
        if current.type == "type_conversion_expression":
            kind = current.child_by_field_name("type")
            if kind is not None and kind.type == "generic_type":
                return True
        if current.type == "unary_expression" and _text(current).lstrip().startswith("<-"):
            return True
        if current.type == "func_literal":
            continue
        stack.extend(current.children)
    return False


_does_work = does_work

#: Go's binary operator precedences, as go/token gives them.
_PRECEDENCE = {
    "*": 5, "/": 5, "%": 5, "<<": 5, ">>": 5, "&": 5, "&^": 5,
    "+": 4, "-": 4, "|": 4, "^": 4,
    "==": 3, "!=": 3, "<": 3, "<=": 3, ">": 3, ">=": 3,
    "&&": 2, "||": 1,
}


class _Unformattable(Exception):
    """The kept text would not be written the way gofmt writes it."""


def _operator(node: Node) -> str:
    operator = node.child_by_field_name("operator")
    return _text(operator) if operator is not None else ""


def _walk_binary(e: Node) -> tuple[bool, bool, int]:
    """go/printer's ``walkBinary``. In a tree-sitter tree a binary operand of lower
    precedence is always parenthesized, so the precedence checks there never skip."""
    precedence = _PRECEDENCE[_operator(e)]
    has4, has5, problem = precedence == 4, precedence == 5, 0
    left, right = e.child_by_field_name("left"), e.child_by_field_name("right")
    if left is not None and left.type == "binary_expression":
        h4, h5, p = _walk_binary(left)
        has4, has5, problem = has4 or h4, has5 or h5, max(problem, p)
    if right is not None and right.type == "binary_expression":
        h4, h5, p = _walk_binary(right)
        has4, has5, problem = has4 or h4, has5 or h5, max(problem, p)
    elif right is not None and right.type == "unary_expression":
        unary = _operator(right)
        if unary == "*":
            if _operator(e) == "/":
                problem = 5
        elif _operator(e) + unary in ("/*", "&&", "&^"):
            problem = 5
        elif _operator(e) + unary in ("++", "--"):
            problem = max(problem, 4)
    return has4, has5, problem


def _cutoff(e: Node, depth: int) -> int:
    has4, has5, problem = _walk_binary(e)
    if problem > 0:
        return problem + 1
    if has4 and has5:
        return 5 if depth == 1 else 4
    return 6 if depth == 1 else 4


def _named(node: Node) -> list[Node]:
    return [c for c in node.named_children if c.type != "comment"]


def _holds_binary(node: Node) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "binary_expression":
            return True
        if current.type != "func_literal":
            stack.extend(current.children)
    return False


def _layout(node: Node, depth: int, edits: list[tuple[int, int, str]], source: bytes) -> None:
    """Collect the blanks gofmt writes around ``node``'s binary operators and slice
    colons when ``node`` is printed at ``depth`` (go/printer's ``expr1``)."""

    def gap(before: Node, after: Node, text: str) -> None:
        if source[before.end_byte:after.start_byte].strip(b" \t"):
            raise _Unformattable
        edits.append((before.end_byte, after.start_byte, text))

    kind = node.type
    if kind == "binary_expression":
        precedence = _PRECEDENCE.get(_operator(node))
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        operator = node.child_by_field_name("operator")
        if precedence is None or left is None or right is None or operator is None:
            raise _Unformattable
        blank = " " if precedence < _cutoff(node, depth) else ""
        gap(left, operator, blank)
        gap(operator, right, blank)
        same = left.type == "binary_expression" and _PRECEDENCE.get(_operator(left)) == precedence
        _layout(left, depth if same else depth + 1, edits, source)
        _layout(right, depth + 1, edits, source)
    elif kind == "parenthesized_expression":
        inner = _named(node)
        if len(inner) != 1 or inner[0].type == "parenthesized_expression":
            raise _Unformattable
        _layout(inner[0], max(depth - 1, 1), edits, source)
    elif kind == "unary_expression":
        operand = node.child_by_field_name("operand")
        if operand is not None:
            _layout(operand, 1 if _operator(node) == "*" else depth, edits, source)
    elif kind in ("selector_expression", "type_assertion_expression"):
        operand = node.child_by_field_name("operand")
        if operand is not None:
            _layout(operand, depth, edits, source)
        kind_node = node.child_by_field_name("type")
        if kind_node is not None and _holds_binary(kind_node):
            raise _Unformattable
    elif kind == "index_expression":
        operand, index = node.child_by_field_name("operand"), node.child_by_field_name("index")
        if operand is not None:
            _layout(operand, 1, edits, source)
        if index is not None:
            _layout(index, depth + 1, edits, source)
    elif kind == "slice_expression":
        operand = node.child_by_field_name("operand")
        if operand is not None:
            _layout(operand, 1, edits, source)
        indices = [node.child_by_field_name(f) for f in ("start", "end", "capacity")]
        present = [i for i in indices if i is not None]
        blanks = depth <= 1 and len(present) > 1 and any(i.type == "binary_expression" for i in present)
        children = [c for c in node.children if c.type != "comment"]
        for at, child in enumerate(children):
            if child.type != ":":
                continue
            before, after = children[at - 1], children[at + 1]
            gap(before, child, " " if blanks and before.type not in ("[", ":") else "")
            gap(child, after, " " if blanks and after.type not in ("]", ":") else "")
        for index in present:
            _layout(index, depth + 1, edits, source)
    elif kind == "call_expression":
        arguments = node.child_by_field_name("arguments")
        values = _named(arguments) if arguments is not None else []
        inner = depth + 1 if len(values) > 1 else depth
        function = node.child_by_field_name("function")
        if function is not None:
            _layout(function, inner, edits, source)
        for value in values:
            _layout(value, inner, edits, source)
    elif kind == "variadic_argument":
        for value in _named(node):
            _layout(value, depth, edits, source)
    elif kind == "type_conversion_expression":
        kind_node = node.child_by_field_name("type")
        if kind_node is not None and _holds_binary(kind_node):
            raise _Unformattable
        operand = node.child_by_field_name("operand")
        if operand is not None:
            _layout(operand, depth, edits, source)
    elif kind == "composite_literal":
        kind_node = node.child_by_field_name("type")
        if kind_node is not None and _holds_binary(kind_node):
            raise _Unformattable
        body = node.child_by_field_name("body")
        if body is not None:
            _layout(body, 1, edits, source)
    elif kind in ("literal_value", "keyed_element", "literal_element"):
        for child in _named(node):
            _layout(child, 1, edits, source)
    elif kind == "func_literal":
        return
    elif _holds_binary(node):
        raise _Unformattable


def kept_text(node: Node, source: bytes) -> str | None:
    """``node``'s text as gofmt writes it alone on the right of ``_ =``, or ``None``
    when that cannot be worked out. Only the blanks around binary operators and
    slice colons depend on where an expression sits (go/printer's depth), so only
    those are rewritten."""
    edits: list[tuple[int, int, str]] = []
    try:
        _layout(node, 1, edits, source)
    except _Unformattable:
        return None
    data = source[node.start_byte:node.end_byte]
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start - node.start_byte] + text.encode("utf-8") + data[end - node.start_byte:]
    return data.decode("utf-8")


def _is_read(call: Node, flag_key: str, readers: dict[int, frozenset[str]], spellings: frozenset[str]) -> bool:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "selector_expression":
        function = function.child_by_field_name("field")
    arguments = call.child_by_field_name("arguments")
    if function is None or arguments is None:
        return False
    name = _text(function)
    values = [c for c in arguments.named_children if c.type != "comment"]
    for position, names in readers.items():
        if name not in names or position >= len(values):
            continue
        value = values[position]
        if value.type == "interpreted_string_literal" and _text(value)[1:-1] == flag_key:
            return True
        if value.type in ("identifier", "selector_expression") and _text(value) in spellings:
            return True
    return False


def _working_parts(call: Node, shadowed: frozenset[str]) -> list[Node]:
    """The receiver and arguments of ``call`` that do work, in evaluation order."""
    parts: list[Node] = []
    function = call.child_by_field_name("function")
    if function is not None and function.type == "selector_expression":
        operand = function.child_by_field_name("operand")
        if operand is not None and does_work(operand, shadowed):
            parts.append(operand)
    arguments = call.child_by_field_name("arguments")
    for argument in (c for c in (arguments.named_children if arguments is not None else []) if c.type != "comment"):
        if does_work(argument, shadowed):
            parts.append(argument)
    return parts


def _left_of(parent: Node, child: Node) -> list[Node]:
    return [c for c in parent.children if c.end_byte <= child.start_byte]


def _statement(call: Node, shadowed: frozenset[str]) -> Node | str:
    """The statement the kept expressions go in front of, or why there is none."""

    def does_work(node: Node | None) -> bool:
        return node is not None and _does_work(node, shadowed)

    child, parent = call, call.parent
    while parent is not None:
        kind = parent.type
        if kind in _BODIES:
            if child.type not in _STATEMENTS:
                return "it is not in a statement the kept calls can be placed in front of"
            return child
        if kind == "source_file":
            return "it is at package level, where a call cannot be kept as a statement"
        if kind in ("expression_case", "communication_case"):
            return "it is a `case` value, which only runs when the earlier cases do not match"
        if kind in ("func_literal", "for_statement", "for_clause", "range_clause", "go_statement",
                    "defer_statement", "labeled_statement", "select_statement", "type_switch_statement"):
            return f"it is evaluated inside a `{kind.replace('_', ' ')}`, where the kept calls cannot run first"
        if kind == "if_statement":
            condition = parent.child_by_field_name("condition")
            initializer = parent.child_by_field_name("initializer")
            if child.id not in {n.id for n in (condition, initializer) if n is not None}:
                return "it is not in a statement the kept calls can be placed in front of"
            if parent.parent is not None and parent.parent.type == "if_statement":
                return "it is in an `else if`, which only runs when the earlier conditions are false"
            # The initializer can bind a name the condition reads (`if v := …; f(v)`
            # or `if v = …; f(v)`): in front of the `if`, that name means another
            # variable, or nothing.
            if initializer is not None and initializer.id != child.id:
                return "the `if` has an initializer, which runs first and can set a name the kept calls read"
        elif kind == "expression_switch_statement":
            value = parent.child_by_field_name("value")
            initializer = parent.child_by_field_name("initializer")
            if child.id not in {n.id for n in (value, initializer) if n is not None}:
                return "it is a `case` value, which only runs when the earlier cases do not match"
            if initializer is not None and initializer.id != child.id:
                return "the `switch` has an initializer, which runs first and can set a name the kept calls read"
        elif kind == "binary_expression":
            operator = parent.child_by_field_name("operator")
            left = parent.child_by_field_name("left")
            if left is None or left.id != child.id:
                if operator is not None and _text(operator) in ("&&", "||"):
                    return f"it is on the right of `{_text(operator)}`, so it does not always run"
                if does_work(left):
                    return "something to its left in the expression does work first"
        elif kind == "unary_expression":
            pass
        elif kind in _ORDERED or kind == "call_expression":
            if any(does_work(n) for n in _left_of(parent, child)):
                return "something to its left in the statement does work first"
        elif kind in ("short_var_declaration", "assignment_statement", "send_statement", "var_spec",
                      "return_statement", "expression_statement"):
            if any(does_work(n) for n in _left_of(parent, child)):
                return "something to its left in the statement does work first"
        elif kind == "var_declaration":
            if len([c for c in parent.named_children if c.type != "comment"]) != 1:
                return "it is one of several specs of a `var` declaration"
        else:
            return f"it sits inside a `{kind.replace('_', ' ')}`, where the kept calls cannot run first"
        child, parent = parent, parent.parent
    return "it is not in a statement the kept calls can be placed in front of"


def _starts_its_line(data: bytes, node: Node) -> bool:
    line = data.rfind(b"\n", 0, node.start_byte) + 1
    return data[line:node.start_byte].strip(b" \t") == b""


def _newline_of(data: bytes, node: Node) -> str:
    """The line ending of ``node``'s first line: the file's own, CRLF or LF."""
    end = data.find(b"\n", node.start_byte)
    return "\r\n" if end > 0 and data[end - 1:end] == b"\r" else "\n"


def _indent_of(data: bytes, node: Node) -> str:
    line = data.rfind(b"\n", 0, node.start_byte) + 1
    return data[line:node.start_byte].decode("utf-8")


def _mentions(source: str, flag_key: str, spellings: frozenset[str]) -> bool:
    return flag_key in source or any(s in source for s in spellings)


def scan(
    source: str, flag_key: str, accessors: Iterable[str], spellings: Iterable[str] = (),
    package: frozenset[str] = frozenset(),
) -> tuple[list[_Hoist], list[_Declined]]:
    """The reads of ``source`` whose work this pass keeps, and the ones it cannot.
    ``package`` names the builtins another file of the package redeclares
    (:func:`package_shadows`)."""
    spellings = frozenset(spellings)
    if flag_key not in source and not any(s in source for s in spellings):
        return [], []
    readers = go_handle_reads.readers_of(accessors)
    data = source.encode("utf-8")
    root = _parse(source, "go")
    shadowed = (_declared_names(root) & (_CONSTANT_BUILTINS | {"unsafe"})) | package
    hoists: list[_Hoist] = []
    declines: list[_Declined] = []
    for node in _walk(root):
        if node.type != "call_expression" or not _is_read(node, flag_key, readers, spellings):
            continue
        parts = _working_parts(node, shadowed)
        if not parts:
            continue
        statement = _statement(node, shadowed)
        texts = [kept_text(p, data) for p in parts]
        if isinstance(statement, str):
            declines.append(_Declined(node, statement))
        elif not _starts_its_line(data, statement):
            declines.append(_Declined(node, "the statement holding it shares its line with another"))
        elif any(b"\n" in p.text for p in parts):
            declines.append(_Declined(node, "a receiver or argument that does work spans several lines"))
        elif any(t is None for t in texts):
            declines.append(_Declined(node, "a receiver or argument that does work could not be laid out as gofmt would"))
        else:
            hoists.append(_Hoist(statement, tuple(t for t in texts if t is not None), node))
    # Two reads that do work in one statement: the second one's work would
    # move ahead of the first read's.
    by_statement: dict[int, list[_Hoist]] = {}
    for hoist in hoists:
        by_statement.setdefault(hoist.statement.id, []).append(hoist)
    kept = []
    for hoist in hoists:
        if len(by_statement[hoist.statement.id]) > 1:
            declines.append(_Declined(hoist.call, "another read in the same statement does work too"))
        else:
            kept.append(hoist)
    return kept, declines


def _reason(call: Node, why: str) -> str:
    return (
        f"the read `{_flat(_text(call))}` evaluates a receiver or argument that does work, "
        f"which the fold would drop, and the work cannot be kept: {why}"
    )


def rewrite(
    source: str, flag_key: str, accessors: Iterable[str], spellings: Iterable[str] = (),
    package: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """``(rewritten source, the texts of the reads whose work was kept)``."""
    hoists, _ = scan(source, flag_key, accessors, spellings, package)
    if not hoists:
        return source, []
    data = source.encode("utf-8")
    edits: list[tuple[int, bytes]] = []
    for hoist in hoists:
        indent = _indent_of(data, hoist.statement)
        newline = _newline_of(data, hoist.statement)
        lines = "".join(f"_ = {p}{newline}{indent}" for p in hoist.expressions)
        edits.append((hoist.statement.start_byte, lines.encode("utf-8")))
    for start, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[start:]
    return data.decode("utf-8"), [_flat(_text(h.call)) for h in hoists]


@dataclass
class Prepass(mock_stubs.Prepass):
    """Keeps each read's working receiver and arguments before the engine folds it."""

    #: The bare names this flag's key is declared under in the candidate files.
    names: frozenset[str] = field(default_factory=frozenset)

    def spellings(self, path: str) -> frozenset[str]:
        return self.names | self.key_texts.get(path, frozenset())

    def apply(self, paths: list[str]) -> None:
        for path in paths:
            try:
                source = mock_stubs._read_exact(path)
            except (OSError, UnicodeDecodeError):
                continue
            key = os.path.abspath(path)
            if key in self.originals:
                continue
            spellings = self.spellings(path)
            if not _mentions(source, self.flag_key, spellings):
                continue
            resolved, calls = rewrite(
                source, self.flag_key, self.accessors, spellings, package_shadows(path, source)
            )
            if not calls:
                continue
            self.spelled[key] = path
            self.originals[key] = source
            self.removals[key] = [mock_stubs.Removal(KIND, c) for c in calls]
            self.edited[key] = resolved
            mock_stubs._write_exact(path, resolved)

    def report(self, repo_dir: str) -> tuple[tuple[str, str, str], ...]:
        return ()


def refusal_reason(
    passes: Iterable[mock_stubs.Prepass],
    path: str,
    baseline: str,
    content: str,
    flag_key: str,
    accessors: Iterable[str],
) -> str | None:
    """Why ``content`` (``path``'s result, from ``baseline``, what the engine
    saw) is refused: a read that does work was folded without its work kept,
    or a read whose work was kept was not folded, so it would run twice."""
    key = os.path.abspath(path)
    mine = [p for p in passes if isinstance(p, Prepass)]
    kept = [r.text for p in mine for r in p.removals.get(key, ()) if r.kind == KIND]
    spellings: frozenset[str] = frozenset()
    for p in mine:
        spellings |= p.spellings(p.spelled.get(key, path))
    flat_before, flat_after = _flat(baseline), _flat(content)
    for text in kept:
        if text in flat_after:
            return (
                f"the read `{text}` was not folded after its receiver and arguments that do work "
                "were kept, so they would run twice"
            )
    package = package_shadows(path, baseline) if _mentions(baseline, flag_key, spellings) else frozenset()
    hoists, declines = scan(baseline, flag_key, accessors, spellings, package)
    found = [(h.call, "it was not rewritten before the fold") for h in hoists]
    found += [(d.call, d.reason) for d in declines]
    unclaimed = list(kept)
    for call, why in sorted(found, key=lambda f: f[0].start_byte):
        text = _flat(_text(call))
        if text in unclaimed:
            unclaimed.remove(text)
            continue
        if flat_after.count(text) < flat_before.count(text):
            return _reason(call, why)
    return None
