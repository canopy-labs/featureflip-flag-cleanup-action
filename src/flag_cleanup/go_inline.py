"""A Go local bound to the flag read, inlined where another binding shares its name.

``on := client.BoolVariation(…)`` folds to ``on := true``, and the engine then
deletes the declaration and writes ``true`` for every identifier spelled ``on``
in the function. Written for every identifier, that would rewrite a ``range``
variable, an ``if`` initializer, a function literal's parameter or a later
``n, on := parse()`` spelled the same as well. Because ``true`` and ``false``
are predeclared identifiers in Go rather than keywords, some of those results
would still build and quietly behave differently: ``for _, true := range prefs``
declares a variable named ``true``, and every ``if on`` in the loop body would
already have been folded as if it were the flag.

The engine now withholds the inline when the name is bound, assigned, used as a
struct literal key or has its address taken anywhere else in the function, and
leaves ``on := true`` standing with its reads. What it cannot see is a read
ABOVE the declaration that resolves to an outer variable of the same name
(``var on = false`` at package level, ``use(on)`` before ``on := <read>``): its
binding count looks only inside the function, and nothing in its filters says
"before". So Gate 1 still refuses the file here (see :func:`refusal_reason`):
every identifier spelled like an inlined local, in the function the engine
rewrote, must be a plain read that Go resolves to that local's own declaration.
For every other shape this is defence in depth. The check reads the customer's
source, where the names are still there to resolve.
"""

from __future__ import annotations

from collections.abc import Iterable

from tree_sitter import Node

from flag_cleanup import go_scope
from flag_cleanup.syntax import _parse, _walk
from flag_cleanup.wrapper_go import _is_read

#: The scope the engine rewrites an inlined local's identifiers in. A function
#: literal is not one: its identifiers are rewritten with its enclosing
#: function's.
_FUNCTIONS = frozenset({"function_declaration", "method_declaration"})


def refusal_reason(source: str, names: Iterable[str]) -> str | None:
    """Why inlining the locals ``names`` in ``source`` would rewrite something else.

    ``names`` are the locals the engine inlined in this file, read off its
    rule matches. ``None`` when every identifier it rewrote was a read of the
    local it inlined.
    """
    names = frozenset(names)
    if not names:
        return None
    for node in sorted(_walk(_parse(source, "go")), key=lambda n: n.start_byte):
        if node.type != "short_var_declaration":
            continue
        left = node.child_by_field_name("left")
        for binder in [] if left is None else left.named_children:
            name = binder.text.decode("utf-8", "replace")
            if binder.type != "identifier" or name not in names:
                continue
            function = node.parent
            while function is not None and function.type not in _FUNCTIONS:
                function = function.parent
            if function is None:
                continue
            other = _other_mention(function, binder, name)
            if other is not None:
                return (
                    f"the local `{name}` on line {binder.start_point[0] + 1} is "
                    "inlined, and the same function also spells "
                    f"`{name}` on line {other.start_point[0] + 1} for something "
                    "other than a read of it, which inlining would rewrite too"
                )
    return None


def _other_mention(function: Node, binder: Node, name: str) -> Node | None:
    """The first identifier ``name`` in ``function`` that is not a read of ``binder``."""
    key = [(binder.start_byte, binder.end_byte)]
    for leaf in sorted(_walk(function), key=lambda n: n.start_byte):
        if leaf.type != "identifier" or leaf == binder or leaf.text.decode("utf-8", "replace") != name:
            continue
        if (
            not _is_read(leaf)
            or _address_taken(leaf)
            or [(b.start_byte, b.end_byte) for b in go_scope.local_binders(leaf, name)] != key
        ):
            return leaf
    return None


def _address_taken(leaf: Node) -> bool:
    """``&on``: a literal has no address, so ``&true`` does not build."""
    parent = leaf.parent
    return (
        parent is not None
        and parent.type == "unary_expression"
        and parent.child_by_field_name("operand") == leaf
        and any(c.type == "&" for c in parent.children)
    )
