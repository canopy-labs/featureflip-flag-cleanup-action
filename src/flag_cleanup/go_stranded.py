"""A Go local the run left declared and no longer used.

Go does not build with an unused local (``declared and not used``), so every
local a fold strands has to go. The engine deletes most of them and the
stranded-binding pass finishes most of the rest, but both withhold what they
cannot judge: several names declared together with values (``x, y := 3,
load()``, ``var low, high = 1, 9``), and a name another binding or a struct
literal key spells the same (``func(depth int)``, a sibling block's own
``limit``, ``Opts{retries: 1}``), because each counts the name's text. What
either one withholds used to ship as it stood, in a pull request that does not
build.

Gate 1 refuses such a file (see :func:`refusal_reason`). It asks Go's own
question of the result: is every local still used? A local counts as used when
an identifier Go treats as a use of it resolves to it through Go's scopes
(:func:`flag_cleanup.go_scope.local_binders`). The answer leans toward
"unused" wherever Go's is uncertain from the source alone, so a doubtful case
refuses rather than ships: a plain assignment is not counted (as
:func:`flag_cleanup.wrapper_go._is_read` judges it), and neither is a
composite literal key unless the literal is a map. ``x++`` and ``x += v`` read
the local to add to it, and Go counts them. The question is asked of the
customer's source too, and only a local unused after the run but not before
is the run's doing: a parse-level snippet may declare a local nothing reads,
and that one is not this run's to answer for. Locals are told apart by their
function, name and declaration, never by name alone: the fold can delete an
unused-looking local of the same name along with the branch, and a count per
name would then see one before and one after.
"""

from __future__ import annotations

from collections import Counter

from tree_sitter import Node

from flag_cleanup import go_scope
from flag_cleanup.syntax import _parse, _walk
from flag_cleanup.wrapper_go import _is_read, _literal_key

_FUNCTIONS = frozenset({"function_declaration", "method_declaration", "func_literal"})
#: Statements whose header declares variables for their body alone.
_HEADER_HOLDERS = frozenset({"range_clause", "receive_statement"})
#: The nodes whose text identifies a declaration. A type switch is identified
#: by its alias and value alone: its clauses are what the fold edits.
_UNITS = frozenset({"short_var_declaration", "var_spec", "range_clause", "receive_statement", "type_switch_statement"})


def refusal_reason(before: str, after: str) -> str | None:
    """Why the run's result ``after`` of ``before`` would not build: a local
    it left declared and unused. ``None`` when it left none."""
    gained = Counter(map(_identity, _unused_locals(after))) - Counter(
        map(_identity, _unused_locals(before))
    )
    for binder in _unused_locals(after):
        name = _text(binder)
        if gained[_identity(binder)] > 0:
            return (
                f"the local `{name}` in {_function_name(binder)} is declared "
                "and no longer used once the flag's branch is gone, "
                "and neither cleanup could remove it; Go does not build with "
                "an unused local"
            )
    return None


def _identity(binder: Node) -> tuple[str, str, str]:
    """What tells a local apart across the rewrite: its function, its name and
    its declaration's text, which the fold leaves alone when it removes code
    around it."""
    unit = binder.parent
    while unit is not None and unit.type not in _UNITS:
        unit = unit.parent
    if unit is not None and unit.type == "type_switch_statement":
        parts = [unit.child_by_field_name("alias"), unit.child_by_field_name("value")]
        declared = " ".join(" ".join(_text(p).split()) for p in parts if p is not None)
    else:
        declared = "" if unit is None else " ".join(_text(unit).split())
    function = binder.parent
    while function is not None and function.type not in ("function_declaration", "method_declaration"):
        function = function.parent
    owner = "" if function is None else " ".join(_text(function).split("{", 1)[0].split())
    return owner, _text(binder), declared


def _function_name(node: Node) -> str:
    """The named function or method around ``node``, as the customer would
    look for it: a line number would be the rewritten file's, which is not
    the one left on disk."""
    parent = node.parent
    while parent is not None and parent.type not in ("function_declaration", "method_declaration"):
        parent = parent.parent
    name = parent.child_by_field_name("name") if parent is not None else None
    return "a function" if name is None else f"`{_text(name)}`"


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _unused_locals(source: str) -> list[Node]:
    """The name tokens of the local variables in ``source`` nothing uses, in
    source order."""
    root = _parse(source, "go")
    binders = [b for b in _local_binders(root) if _text(b) != "_"]
    if not binders:
        return []
    names = {_text(b) for b in binders}
    used: set[tuple[int, int]] = set()
    for leaf in _walk(root):
        if leaf.type != "identifier" or _text(leaf) not in names or not _uses(leaf):
            continue
        used.update((b.start_byte, b.end_byte) for b in go_scope.local_binders(leaf, _text(leaf)))
    unused = [b for b in binders if (b.start_byte, b.end_byte) not in used]
    return sorted(unused, key=lambda b: b.start_byte)


def _uses(leaf: Node) -> bool:
    """Whether Go may count this mention as a use: :func:`_is_read`, plus the
    key of a map literal, which reads the local it names, and the target of
    ``x++`` or ``x += v``, which reads it to add to it."""
    parent = leaf.parent
    if parent is not None and parent.type in ("inc_statement", "dec_statement"):
        return True
    holder = parent.parent if parent is not None and parent.type == "expression_list" else None
    if (
        holder is not None
        and holder.type == "assignment_statement"
        and holder.child_by_field_name("left") == parent
        and any(not c.is_named and c.type not in ("=", ",") for c in holder.children)
    ):
        return True
    if _literal_key(leaf):
        composite = leaf.parent.parent.parent.parent
        written = composite.child_by_field_name("type") if composite is not None else None
        return composite is not None and composite.type == "composite_literal" and (
            written is not None and written.type == "map_type"
        )
    return _is_read(leaf)


def _local_binders(root: Node) -> list[Node]:
    """Every name token that declares a local variable: a ``:=`` name that is
    not a redeclaration, a ``var`` name inside a function, a ``range`` or
    ``select``-receive name declared with ``:=``, and a type-switch alias.
    Parameters, constants and package-level names are never reported unused,
    so they are not offered."""
    found: list[Node] = []
    for node in _walk(root):
        if node.type == "short_var_declaration":
            found += [n for n in _names(node.child_by_field_name("left")) if not _redeclared(node, n)]
        elif node.type == "var_spec" and _in_function(node):
            found += [n for n in node.children_by_field_name("name") if n.type == "identifier"]
        elif node.type in _HEADER_HOLDERS and any(c.type == ":=" for c in node.children):
            found += _names(node.child_by_field_name("left"))
        elif node.type == "type_switch_statement":
            found += _names(node.child_by_field_name("alias"))
    return found


def _names(holder: Node | None) -> list[Node]:
    return [] if holder is None else [c for c in holder.named_children if c.type == "identifier"]


def _in_function(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in _FUNCTIONS:
            return True
        parent = parent.parent
    return False


def _redeclared(declaration: Node, name: Node) -> bool:
    """Whether ``name`` in this ``:=`` assigns a variable already declared in
    the same scope: an earlier statement of its block, or, at a function's top
    level, a parameter or named result. Go then declares nothing by that name
    here, and its uses belong to the earlier declaration."""
    block = declaration.parent
    if block is None or block.type != "statement_list":
        return False
    text = _text(name)
    earlier = [
        node
        for statement in block.named_children
        if statement.end_byte <= declaration.start_byte
        for node, _ in go_scope._statement_sites(statement)
    ]
    body = block.parent
    function = body.parent if body is not None and body.type == "block" else None
    if function is not None and function.type in _FUNCTIONS and function.child_by_field_name("body") == body:
        earlier += [node for node, _ in go_scope._parameter_sites(function)]
    return any(_text(node) == text for node in earlier)
