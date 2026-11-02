"""Delete a call the removal kept only as a discard, when the customer names it inert.

A local whose last reader was the folded flag read goes with the fold. When its
initializer calls something, the call is kept as a statement on its own
(Go's ``_ = getClient(p)``, a bare ``getClient(p);`` elsewhere), because a
call can have side effects and this tool cannot tell a client getter from one
that does real work. The ``side-effect-free-calls`` input is the customer
telling it: a discard that is exactly a call to one of those names, with
nothing but names, literals and member reads as arguments, is deleted instead.

Their word, not a proof, so every deletion is named in the pull request. And it
only ever touches what THIS run wrote: a discard statement whose text the
customer's file did not already hold. One they wrote themselves is theirs.

Each deletion is refused (the statement stays, and is reported as a kept
discard) wherever removing a whole statement could change more than the call:

- the statement does not have its lines to itself;
- its container is not a braced block or a file (a braceless ``if`` body would
  adopt the next statement);
- in Kotlin, Swift and Ruby, it is the last statement of its container, whose
  value it may be;
- in Python, it is the only statement of its block;
- in Go, deleting it would strand a local or an import that the
  stranded-binding cleanup would not remove (Go does not build with either);
- the file has more parse errors afterwards than before.

What a deletion strands elsewhere (the local or import the call was the last to
read) is left to the stranded-binding cleanup, which runs again afterwards.
"""

from __future__ import annotations

import contextlib
import re
from collections import Counter
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import unused

#: The ``bindings`` kind of a discarded call deleted on the customer's word.
DECLARED_CALL = "deleted call"

_DECLARED: ContextVar[frozenset[str]] = ContextVar("side_effect_free_calls", default=frozenset())


@contextlib.contextmanager
def declaring(names: tuple[str, ...] | frozenset[str]) -> Iterator[None]:
    """Treat a call to any of ``names`` as inert for the duration."""
    token = _DECLARED.set(frozenset(names))
    try:
        yield
    finally:
        _DECLARED.reset(token)


def declared() -> frozenset[str]:
    return _DECLARED.get()


#: Containers a statement can be deleted from without anything else moving
#: into its place. A braceless body (`if (c) f();`) is never one of them.
_BLOCKS = {
    "go": frozenset({"statement_list"}),
    "java": frozenset({"block", "constructor_body", "switch_block_statement_group"}),
    "csharp": frozenset({"block"}),
    "ts": frozenset({"statement_block", "program"}),
    "tsx": frozenset({"statement_block", "program"}),
    "js": frozenset({"statement_block", "program"}),
    "php": frozenset({"compound_statement", "program"}),
    "python": frozenset({"block", "module"}),
    "kt": frozenset({"block", "statements"}),
    "swift": frozenset({"statements"}),
    "ruby": frozenset({"body_statement", "program", "then", "else", "do", "begin", "block_body"}),
    "dart": frozenset({"block"}),
}

#: Languages where a container's last statement can be its value: a Kotlin
#: lambda or `if` expression, a Swift single-expression closure, any Ruby body.
_LAST_IS_A_VALUE = frozenset({"kt", "swift", "ruby"})

#: Leaves that read nothing but a name or are a literal.
_INERT_LEAVES = frozenset({
    "identifier", "simple_identifier", "field_identifier", "property_identifier",
    "name", "constant", "this", "self", "super", "type_identifier",
    "true", "false", "nil", "null", "null_literal", "boolean_literal", "boolean",
    "integer", "float", "int_literal", "float_literal", "imaginary_literal", "rune_literal",
    "integer_literal", "real_literal", "decimal_integer_literal", "hex_integer_literal",
    "decimal_floating_point_literal", "character_literal", "number", "number_literal",
    "string_content", "string_fragment", "escape_sequence", "line_str_text",
    "string_start", "string_end", "interpreted_string_literal_content",
    "raw_string_literal_content", "string_literal_content", "optional_chain",
    "predefined_type", "simple_symbol", "hash_key_symbol",
})

#: Nodes that are inert when everything inside them is: argument wrappers,
#: string literals, and member reads (`o.f`, `$o->f`, `Foo::BAR`).
_INERT_CONTAINERS = frozenset({
    "argument", "value_argument", "value_argument_label",
    "string", "string_literal", "interpreted_string_literal", "raw_string_literal",
    "line_string_literal", "template_string", "encapsed_string", "verbatim_string_literal",
    "selector_expression", "member_expression", "field_access", "member_access_expression",
    "navigation_expression", "navigation_suffix", "attribute", "scoped_property_access_expression",
    "class_constant_access_expression", "variable_name", "qualified_name", "namespace_name",
    "unconditional_assignable_selector", "parenthesized_expression",
})


def _inert(node: Node, language: str) -> bool:
    if node.type in _INERT_LEAVES:
        return True
    if language == "ruby" and node.type == "call":
        # `o.f` with no arguments and no block is Ruby's member read.
        return (
            node.child_by_field_name("arguments") is None
            and node.child_by_field_name("block") is None
            and all(_inert(c, language) for c in node.named_children)
        )
    if language == "dart" and node.type == "selector":
        return all(c.type == "unconditional_assignable_selector" and _inert(c, language) for c in node.named_children)
    if node.type in _INERT_CONTAINERS:
        return all(_inert(c, language) for c in node.named_children)
    return False


def _last_name(node: Node | None) -> str | None:
    """The name a callee expression ends in: `f`, `o.f`, `a.b.f`, `Foo::f`."""
    if node is None:
        return None
    if node.type in {"identifier", "simple_identifier", "field_identifier", "property_identifier", "name", "constant"}:
        return node.text.decode("utf-8", "replace")
    if node.type == "generic_name":
        return _last_name(node.named_children[0] if node.named_children else None)
    for field in ("field", "property", "name", "attribute", "suffix", "method"):
        child = node.child_by_field_name(field)
        if child is not None:
            return _last_name(child)
    if node.type in {"navigation_expression", "navigation_suffix", "qualified_name"} and node.named_children:
        return _last_name(node.named_children[-1])
    return None


def _call_of(statement: Node, language: str) -> tuple[str, list[Node]] | None:
    """``(callee name, argument nodes)`` when ``statement`` is a discarded call:
    a statement whose whole expression is one call, and nothing else."""
    call: Node | None
    if language == "go":
        if statement.type == "assignment_statement":
            left = statement.child_by_field_name("left")
            right = statement.child_by_field_name("right")
            operator = statement.child_by_field_name("operator")
            if left is None or right is None or operator is None or operator.type != "=":
                return None
            if [c.text for c in left.named_children] != [b"_"] or len(right.named_children) != 1:
                return None
            call = right.named_children[0]
        elif statement.type == "expression_statement" and len(statement.named_children) == 1:
            call = statement.named_children[0]
        else:
            return None
        if call.type != "call_expression":
            return None
        args = call.child_by_field_name("arguments")
        return _named(_last_name(call.child_by_field_name("function")), args)
    if language == "dart":
        if statement.type != "expression_statement":
            return None
        parts = statement.named_children
        if len(parts) < 2 or parts[0].type != "identifier" or parts[-1].type != "selector":
            return None
        last = parts[-1].named_children
        if len(last) != 1 or last[0].type != "argument_part":
            return None
        if any(p.type != "selector" for p in parts[1:-1]):
            return None
        if len(parts) == 2:
            name = _last_name(parts[0])
        else:
            previous = parts[-2].named_children
            if len(previous) != 1 or previous[0].type != "unconditional_assignable_selector":
                return None
            name = _last_name(previous[0].named_children[0] if previous[0].named_children else None)
        receiver_ok = all(_inert(p, language) for p in parts[1:-2])
        if not receiver_ok:
            return None
        arguments = next((c for c in last[0].named_children if c.type == "arguments"), None)
        return _named(name, arguments)
    if language in {"kt", "swift"}:
        if statement.type != "call_expression":
            return None
        callee = statement.named_children[0] if statement.named_children else None
        if language == "kt":
            rest = statement.named_children[1:]
            if len(rest) != 1 or rest[0].type != "value_arguments":
                return None  # a trailing lambda runs code
            name = _last_name(callee)
            if callee is not None and callee.type == "navigation_expression" and callee.named_children:
                name = _last_name(callee.named_children[-1])
            return _named(name, rest[0])
        suffix = statement.named_children[1:]
        if len(suffix) != 1 or suffix[0].type != "call_suffix":
            return None
        parts = suffix[0].named_children
        if len(parts) != 1 or parts[0].type != "value_arguments":
            return None  # a trailing closure runs code
        return _named(_last_name(callee), parts[0])
    if language == "ruby":
        if statement.type != "call" or statement.child_by_field_name("block") is not None:
            return None
        receiver = statement.child_by_field_name("receiver")
        if receiver is not None and not _inert(receiver, language):
            return None
        return _named(_last_name(statement.child_by_field_name("method")), statement.child_by_field_name("arguments"))
    if statement.type != "expression_statement" or len(statement.named_children) != 1:
        return None
    call = statement.named_children[0]
    if language == "java":
        if call.type != "method_invocation":
            return None
        receiver = call.child_by_field_name("object")
        if receiver is not None and not _inert(receiver, language):
            return None
        return _named(_last_name(call.child_by_field_name("name")), call.child_by_field_name("arguments"))
    if language == "csharp":
        if call.type != "invocation_expression":
            return None
        return _named(_last_name(call.child_by_field_name("function")), call.child_by_field_name("arguments"))
    if language in {"ts", "tsx", "js"}:
        if call.type != "call_expression":
            return None
        return _named(_last_name(call.child_by_field_name("function")), call.child_by_field_name("arguments"))
    if language == "python":
        if call.type != "call":
            return None
        return _named(_last_name(call.child_by_field_name("function")), call.child_by_field_name("arguments"))
    if language == "php":
        if call.type == "function_call_expression":
            return _named(_last_name(call.child_by_field_name("function")), call.child_by_field_name("arguments"))
        if call.type in {"member_call_expression", "nullsafe_member_call_expression", "scoped_call_expression"}:
            receiver = call.child_by_field_name("object") or call.child_by_field_name("scope")
            if receiver is not None and not _inert(receiver, language):
                return None
            return _named(_last_name(call.child_by_field_name("name")), call.child_by_field_name("arguments"))
    return None


def _named(name: str | None, arguments: Node | None) -> tuple[str, list[Node]] | None:
    if name is None:
        return None
    return name, [] if arguments is None else list(arguments.named_children)


_CALL_TYPES = frozenset({
    "call_expression", "method_invocation", "invocation_expression", "call",
    "function_call_expression", "member_call_expression", "scoped_call_expression",
    "nullsafe_member_call_expression", "argument_part", "object_creation_expression",
    "new_expression", "await_expression", "lambda_literal", "annotated_lambda",
    "func_literal", "arrow_function", "function_expression", "lambda", "block",
    "do_block", "closure", "anonymous_function",
})


def _own_call_count(statement: Node, language: str) -> int:
    count = 0
    for node in _walk(statement):
        if node.type in _CALL_TYPES:
            if node is not statement and language == "ruby" and node.type == "call" and _inert(node, language):
                continue  # Ruby's member read
            count += 1
    return count


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.named_children)


def _errors(root: Node) -> int:
    return sum(1 for n in _walk(root) if n.type == "ERROR" or n.is_missing)


def _normalized(node: Node) -> str:
    return " ".join(node.text.decode("utf-8", "replace").split())


def _candidates(root: Node, language: str, names: frozenset[str]) -> list[Node]:
    blocks = _BLOCKS.get(language, frozenset())
    found = []
    for node in _walk(root):
        parent = node.parent
        if parent is None or parent.type not in blocks:
            continue
        call = _call_of(node, language)
        if call is None:
            continue
        name, arguments = call
        if name not in names:
            continue
        if not all(_inert(a, language) for a in arguments):
            continue
        if _own_call_count(node, language) != 1:
            continue
        found.append(node)
    return found


def _statements(parent: Node) -> list[Node]:
    return [c for c in parent.named_children if "comment" not in c.type]


def _line_span(data: bytes, node: Node) -> tuple[int, int] | None:
    """The byte range of the whole lines ``node`` occupies, when it has them
    to itself (a trailing `;` aside)."""
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    if data[start:node.start_byte].strip():
        return None
    end = data.find(b"\n", node.end_byte)
    end = len(data) if end == -1 else end
    if data[node.end_byte:end].strip(b" \t\r;"):
        return None
    return start, min(end + 1, len(data))


def remove(before: str, after: str, language: str, path: str | None = None, root: str | None = None) -> tuple[str, list[str]]:
    """``after`` with each declared discard this run introduced deleted, and
    the deleted statements' text."""
    names = declared()
    if not names or language not in _BLOCKS:
        return after, []
    tree = unused._parse(after, language)
    candidates = _candidates(tree, language, names)
    if not candidates:
        return after, []
    already = Counter(_normalized(n) for n in _candidates(unused._parse(before, language), language, names))
    data = after.encode("utf-8")
    spans: list[tuple[int, int, str]] = []
    for node in candidates:
        text = _normalized(node)
        if already[text]:
            continue  # the customer's own, or indistinguishable from it
        siblings = _statements(node.parent)
        if language in _LAST_IS_A_VALUE and siblings and siblings[-1] == node:
            continue
        if language == "python" and node.parent.type == "block" and len(siblings) == 1:
            continue
        if language == "go" and not _go_strands_are_removable(after, node, path, root):
            continue
        span = _line_span(data, node)
        if span is None:
            continue
        spans.append((span[0], span[1], text))
    if not spans:
        return after, []
    result = data
    for start, end, _text in sorted(spans, reverse=True):
        result = result[:start] + _blank_seam(result, start, end, language)
    text = result.decode("utf-8")
    if _errors(unused._parse(text, language)) > _errors(tree):
        return after, []
    return text, [t for _s, _e, t in sorted(spans)]


def _blank_seam(data: bytes, start: int, end: int, language: str) -> bytes:
    """The rest of ``data`` after deleting ``[start, end)``, without leaving
    two blank lines in a row where the deleted lines sat between two."""
    rest = data[end:]
    above_blank = start >= 2 and data[start - 2 : start] == b"\n\n" or start == 0
    below_blank = re.match(rb"[ \t]*\r?\n", rest) is not None
    if above_blank and below_blank:
        rest = rest[rest.index(b"\n") + 1 :]
    return rest


def _go_strands_are_removable(source: str, statement: Node, path: str | None, root: str | None) -> bool:
    """Whether deleting ``statement`` strands only what the stranded-binding
    cleanup removes. Go builds with neither an unused local nor import."""
    from flag_cleanup import go_scope, wrapper_go

    if path is None:
        return False
    packages = go_scope.Packages(Path(root) if root is not None else Path(path).parent)
    file = packages.file(Path(path), source)
    if file is None:
        return False
    node = file.root.descendant_for_byte_range(statement.start_byte, statement.end_byte)
    while node is not None and (node.start_byte, node.end_byte, node.type) != (
        statement.start_byte, statement.end_byte, statement.type
    ):
        node = node.parent
    if node is None:
        return False
    return wrapper_go._strand_reason([node], file, packages) is None
