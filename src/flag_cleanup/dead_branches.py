"""The branch a flag read's fold deletes, found before the fold runs.

A guard often names the flag it checks inside the branch it guards:

    if (!features.IsEnabled(FlagKeys.NewCheckout))
    {
        log.Info("skipped: {Flag} is disabled", FlagKeys.NewCheckout);
        return;
    }

The log line is no flag read, so the key-constant checks used to count it as
a mention that keeps the declaration, and a file with such a mention keeps
every read in it. But under the treatment that makes the condition false, the
fold deletes that block, and the mention goes with it. This module finds those
blocks, so the checks can leave out what is inside them.

It only PREDICTS what the engine will delete. Whoever relies on a prediction
must check the rewritten file afterwards and refuse the flag when a mention it
left out is still there; that check, not this one, is what makes leaving a
mention out safe. A wrong prediction then costs the flag, never the build.

Go's ``if`` reads the same way (``call_expression``, ``unary_expression``);
one with an initializer is left alone. So is TypeScript's and JavaScript's.

Only the plainest shape is predicted: an ``if`` whose whole condition is the
read, perhaps negated and parenthesised, and whose deleted branch is a braced
block. A read under ``&&``, ``||``, ``await`` or a comparison, an ``else if``
alternative, or an unbraced body is left alone: what is in it still counts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tree_sitter import Node


@dataclass(frozen=True)
class _Spec:
    #: The node a call is in this grammar.
    call: str
    #: The node a logical negation is, whose first child is the ``!`` token.
    negation: str
    #: The node a braced body is.
    block: str = "block"
    #: A node wrapping the alternative (``else`` and its statement), if the grammar has one.
    else_clause: str | None = None
    #: The token that negates, the first child of a ``negation`` node.
    not_token: str = "!"
    #: An indented body (Python) has no braces to keep: the whole block is blank.
    indented: bool = False


#: Measured on the bundled grammars: both put the condition, the consequence
#: and the alternative under those field names of ``if_statement``, with the
#: alternative a ``block`` or another ``if_statement``.
_SPECS = {
    "csharp": _Spec(call="invocation_expression", negation="prefix_unary_expression"),
    "java": _Spec(call="method_invocation", negation="unary_expression"),
    "go": _Spec(call="call_expression", negation="unary_expression"),
}
#: TypeScript and JavaScript: the same `if_statement` and `!` as Java's, a
#: `statement_block` body, and an `else_clause` around the alternative.
for _ts in ("ts", "tsx", "js"):
    _SPECS[_ts] = _Spec(
        call="call_expression", negation="unary_expression", block="statement_block", else_clause="else_clause"
    )


#: Python: an `if` with a `not_operator`, an indented `block`, and an
#: `else_clause` around the `else` body. `elif` is another alternative and is
#: left alone, as `else if` is elsewhere.
_SPECS["python"] = _Spec(
    call="call", negation="not_operator", else_clause="else_clause", not_token="not", indented=True
)


def supported(language: str) -> bool:
    return language in _SPECS


def _walk(node: Node) -> Iterable[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _condition_call(condition: Node, spec: _Spec) -> tuple[Node, bool] | None:
    """The call that is the whole condition, and whether it is negated an odd
    number of times. None for any other condition."""
    negated = False
    current: Node | None = condition
    while current is not None:
        if current.type == "parenthesized_expression" and current.named_child_count == 1:
            current = current.named_children[0]
        elif (
            current.type == spec.negation
            and current.child_count == 2
            and current.children[0].type == spec.not_token
        ):
            negated = not negated
            current = current.children[1]
        elif current.type == spec.call:
            return current, negated
        else:
            return None
    return None


def nearest_call(node: Node, language: str) -> Node | None:
    """The innermost call ``node`` sits in, ``node`` itself included."""
    spec = _SPECS[language]
    current: Node | None = node
    while current is not None and current.type != spec.call:
        current = current.parent
    return current


def dead_blocks(
    root: Node,
    language: str,
    treatment: bool,
    is_read: Callable[[Node], bool],
) -> list[tuple[int, int]]:
    """``(start_byte, end_byte)`` of each braced block the fold deletes.

    ``is_read`` is asked of the call that is a condition's whole value and
    says whether the run folds it to ``treatment``. The block is the
    consequence when the condition then folds to false, and the alternative
    when it folds to true, if that alternative is a block.
    """
    spec = _SPECS.get(language)
    if spec is None:
        return []
    found: list[tuple[int, int]] = []
    for node in _walk(root):
        if node.type != "if_statement":
            continue
        condition = node.child_by_field_name("condition")
        # Go's `if v := read(); cond {`: what the fold leaves of the header is
        # not predicted.
        if condition is None or node.child_by_field_name("initializer") is not None:
            continue
        hit = _condition_call(condition, spec)
        if hit is None or not is_read(hit[0]):
            continue
        holds = treatment != hit[1]
        dead = node.child_by_field_name("alternative" if holds else "consequence")
        if dead is not None and dead.type == spec.else_clause and dead.named_child_count == 1:
            dead = dead.named_children[0]
        if dead is not None and dead.type == spec.block:
            # `blank` keeps the first and last byte of a braced block; an
            # indented one has none to keep.
            widen = 1 if spec.indented else 0
            found.append((dead.start_byte - widen, dead.end_byte + widen))
    return found


def inside(node: Node, blocks: Iterable[tuple[int, int]]) -> bool:
    return any(start < node.start_byte and node.end_byte < end for start, end in blocks)


def blank(source: str, blocks: Iterable[tuple[int, int]]) -> str:
    """``source`` with the inside of each block replaced by spaces, its line
    breaks kept, so every line keeps its number and nothing in a block can be
    read as a mention."""
    data = bytearray(source.encode("utf-8"))
    for start, end in blocks:
        for i in range(start + 1, end - 1):
            if data[i] not in (0x0A, 0x0D):
                data[i] = 0x20
    return data.decode("utf-8")
