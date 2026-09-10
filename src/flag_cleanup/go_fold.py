"""Fold the literal `case` arms Piranha leaves behind in a Go tagless switch.

Go is the third language to need a post-pass, and — like PHP's and Python's —
the reason is structural rather than a missing rule.

A tagless ``switch { case <read>: … }`` is Go's idiomatic multi-way guard: it
is what you write instead of an ``if``/``else if`` chain, and the language has
no ternary to fall back on. The engine's built-ins fold the ``if`` family only,
so the read becomes ``case false:`` and nothing else moves — a dead arm left
standing with the flag key gone from the file, which no later run can ever
revisit. `syntax.py`'s ``literal_residues`` entry refused the whole flag rather
than ship that, which is the honest half of "no diff or a complete diff" but
still costs the customer every cleanup in the file (#2692).

**Why a Piranha rule cannot finish it.** The rewrite needs to say "the arms
AFTER the flag's arm", and a sibling relationship is what these queries cannot
express. Measured against the bundled grammar, a child-pattern SEQUENCE is
implicitly anchored to the parent's first named child, with no backtracking:

    switch arms          query                       matches
    [case, default]      (expression_case) . (default_case)      1
    [case, true, default] (expression_case) . (default_case)     0
    [case, true, default] (true-arm) . (default_case)            0
    [true, default]      (true-arm) . (default_case)             1

So a rule can see "the arm after the flag's arm" only when the flag's arm is
the FIRST one. That covers the common shape and nothing else, which is why the
whole fold lives here instead of being split across two mechanisms.

**The three rewrites, and why each is safe.** Verified by executing both
versions under every combination of the surviving conditions, not by reading
them (`go vet` clean, ``gofmt -l`` silent on the output):

* **false, any position** — delete the arm. An arm that can never match
  contributes nothing, and every later arm keeps its order and its semantics.
* **true, first arm** — the switch always takes it, so the whole statement
  collapses to that arm's body, re-indented to the switch's own column. Every
  other arm was already unreachable and goes with it.
* **true, not first** — the arms ABOVE still take precedence, so splicing here
  would be WRONG rather than untidy: with ``case isAdmin`` above it, splicing
  runs the legacy path for admins, who took the admin arm before. Measured:

      isAdmin=true   original=[admin]   splice=[legacy]  WRONG
      isAdmin=true   original=[admin]   promote=[admin]  MATCH

  The arm becomes the terminal ``default:`` and every arm below it goes, since
  an arm below one that always matches is unreachable.

**What is refused rather than folded**, each because no local edit is correct:

* a ``fallthrough`` ANYWHERE in the switch. It runs one arm's body into the
  next REGARDLESS of that arm's condition, so arm boundaries stop being local
  and neither deleting nor promoting an arm is a self-contained edit. Measured
  on the arm ABOVE the flag's — which the issue did not name — deleting the
  dead arm changed ``[A B]`` to ``[A C]``.
* a ``break`` in a body about to be spliced OUT of its switch: ``break`` binds
  to the nearest enclosing switch or loop, so lifting it changes which one, and
  it may become a loop break instead. This one compiles, which is why it is
  guarded rather than left to Gate 1.
* an initializer (``switch v := f(); { … }``), whose side effect and bindings
  the rest of the function can see. Same hazard `syntax.py`'s
  ``preserved_fields`` exists for, and there is no rewrite that both drops the
  statement and keeps the binding.
* a ``default:`` sitting BEFORE the flag's true arm. Promoting would emit a
  second ``default``, which does not compile.

A tagged ``switch enabled { case true: … }`` is ordinary customer code this
tool never creates, and is matched by nothing here: every query below requires
the switch to have no ``value`` field, which is exactly what "tagless" means.
"""

from __future__ import annotations

import tree_sitter_go
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_go.language())
_PARSER = Parser(_LANGUAGE)

#: Bound on the rewrite loop. Each round removes one arm or one switch, so a
#: file cannot need more rounds than it has arms; the cap is a guard against a
#: rewrite that fails to shrink the tree, never a limit reached in practice.
_MAX_ROUNDS = 100


def fold_literal_cases(before: str, after: str) -> str:
    """Fold every tagless-switch arm the transform left with a literal value.

    Nothing happens if ``before`` already had one, exactly as in ``php_fold``
    and ``python_fold``: ``case true:`` in a tagless switch is real code — a
    permanently-enabled toggle, a branch somebody pinned by hand — and once the
    file contains one this cannot tell the customer's from the fold's.
    Rewriting theirs would be an edit the flag removal never justified, so the
    whole file is left to Gate 1 instead.

    Applied one rewrite at a time with a re-parse between, rather than
    collecting every edit and splicing them together: dropping a chain's tail
    or collapsing a switch changes the shape of the tree around it, so offsets
    computed against the original would be stale for every edit after the first.
    """
    if after == before or _literal_cases(before):
        return after
    for _ in range(_MAX_ROUNDS):
        rewritten = _fold_once(after)
        if rewritten is None:
            return after
        after = rewritten
    return after


def _literal_cases(source: str) -> int:
    """How many tagless-switch arms in ``source`` carry a boolean literal."""
    root = _PARSER.parse(source.encode("utf-8")).root_node
    return sum(
        1
        for switch in _iter(root)
        if _is_tagless(switch)
        for arm in switch.named_children
        if _literal(arm) is not None
    )


def _iter(node: Node):
    yield node
    for child in node.children:
        yield from _iter(child)


def _is_tagless(node: Node) -> bool:
    """A ``switch { … }`` with no subject expression.

    The ``value`` field is the subject, so its ABSENCE is the whole definition
    of tagless — and it is what keeps a tagged ``switch enabled { case true: }``
    out of every rewrite here. ``initializer`` is a separate field and is
    checked where it matters, not here: a tagless switch may still carry one.
    """
    return (
        node.type == "expression_switch_statement"
        and node.child_by_field_name("value") is None
    )


def _literal(arm: Node) -> bool | None:
    """``True``/``False`` if this arm's value is a boolean literal, else ``None``.

    ``case a, b:`` is a genuine list of values rather than one condition, so a
    literal among several is deliberately not resolved — the same reading
    `syntax.py`'s ``transparent_nodes`` gives ``expression_list``.
    """
    if arm.type != "expression_case":
        return None
    value = arm.child_by_field_name("value")
    if value is None or value.type != "expression_list":
        return None
    if len(value.named_children) != 1:
        return None
    kind = value.named_children[0].type
    return True if kind == "true" else False if kind == "false" else None


def _contains(node: Node, *types: str) -> bool:
    return any(child.type in types for child in _iter(node))


def _line_indent(data: bytes, position: int) -> str:
    """The whitespace opening the line ``position`` sits on."""
    start = data.rfind(b"\n", 0, position) + 1
    prefix = data[start:position]
    return prefix[: len(prefix) - len(prefix.lstrip())].decode("utf-8")


def _fold_once(source: str) -> str | None:
    """Apply the first foldable literal arm found; ``None`` if there is none."""
    data = source.encode("utf-8")
    root = _PARSER.parse(data).root_node
    for switch in _iter(root):
        if not _is_tagless(switch):
            continue
        # `fallthrough` makes an arm's body run into the next one regardless of
        # that arm's condition, so no arm in this switch can be deleted or
        # promoted as a local edit. Checked once for the whole statement.
        if _contains(switch, "fallthrough_statement"):
            continue
        # An initializer binds names the ARMS use -- `switch v := load(); {`.
        # Removing an arm removes those uses, and Go rejects a binding nothing
        # reads (`v declared and not used`), so a fold here can produce a file
        # that does not compile. Refused wholesale rather than reasoned about
        # per arm: the remaining arms are not guaranteed to reference it either.
        # Same hazard `syntax.py`'s `preserved_fields` exists for.
        if switch.child_by_field_name("initializer") is not None:
            continue
        arms = switch.named_children
        for index, arm in enumerate(arms):
            verdict = _literal(arm)
            if verdict is None:
                continue
            rewritten = (
                _delete_arm(data, switch, arm, arms)
                if verdict is False
                else _fold_true_arm(data, switch, arm, arms, index)
            )
            if rewritten is not None:
                return rewritten
    return None


def _delete_arm(data: bytes, switch: Node, arm: Node, arms: list[Node]) -> str | None:
    """Drop an arm that can never match.

    When it was the switch's only arm the statement itself goes, because an
    empty ``switch { }`` is a statement that does nothing and reads as debris
    rather than as code somebody wrote. A switch carrying an initializer never
    reaches here -- see `_fold_once`.
    """
    if len(arms) == 1:
        return _cut_statement(data, switch)
    return _cut(data, arm.start_byte, arm.end_byte)


def _fold_true_arm(
    data: bytes, switch: Node, arm: Node, arms: list[Node], index: int
) -> str | None:
    """An arm that always matches: collapse to it, or make it the ``default``."""
    body = next((c for c in arm.named_children if c.type == "statement_list"), None)
    if body is None:
        return None
    if index == 0:
        # The switch can only ever take this arm, so it IS this arm's body.
        if _contains(body, "break_statement"):
            return None
        return _splice(data, switch, body)
    # Arms above still take precedence, so the arm becomes the terminal
    # `default` instead — promoting it any further would change which branch
    # runs for an input the arms above already claimed.
    if any(other.type == "default_case" for other in arms[:index]):
        return None
    return _promote_to_default(data, switch, arm, arms, index)


def _splice(data: bytes, switch: Node, body: Node) -> str:
    """Replace the whole switch with ``body``, re-indented to the switch's column."""
    base = _line_indent(data, switch.start_byte)
    inner = _line_indent(data, body.start_byte)
    extra = inner[len(base) :] if inner.startswith(base) else ""
    text = data[body.start_byte : body.end_byte].decode("utf-8")
    lines = text.splitlines()
    out = [lines[0]] if lines else []
    for line in lines[1:]:
        out.append(base + line[len(base) + len(extra) :] if line.startswith(base + extra) else line)
    return _replace(data, switch.start_byte, switch.end_byte, "\n".join(out).rstrip())


def _promote_to_default(
    data: bytes, switch: Node, arm: Node, arms: list[Node], index: int
) -> str:
    """Make ``arm`` the terminal ``default:`` and drop every arm below it.

    The header is replaced up to and including its ``:``, so the body's own
    indentation is never touched — the one edit here that needs no re-indenting.
    """
    colon = next((c for c in arm.children if not c.is_named and c.text == b":"), None)
    if colon is None:  # pragma: no cover - `case <value> :` always carries one
        return None
    source = data.decode("utf-8")
    promoted = source[: arm.start_byte] + "default:"
    if index == len(arms) - 1:
        return promoted + source[colon.end_byte :]
    # Everything below an arm that always matches is unreachable, so the tail
    # goes in the same edit. Resuming after the LAST arm keeps the closing
    # brace and its indentation, which sit outside every arm node.
    body = source[colon.end_byte : arm.end_byte]
    return promoted + body + source[arms[-1].end_byte :]


def _cut(data: bytes, start: int, end: int) -> str:
    """Remove ``[start, end)`` along with the line the arm opened on.

    Taken back to the line start rather than to ``start`` itself: an arm node
    begins at its ``case`` keyword, so cutting from there would leave the
    indentation behind as a whitespace-only line. An arm node already ends past
    its own newline, so nothing more is needed on that side.

    A Piranha rule doing the same edit had to replace with ``"\n"`` instead,
    because deleting the span outright fused the previous arm's last statement
    onto the next ``case`` keyword and Go needs a statement separator there.
    Working on the whole file in Python, the line is simply removed — which
    keeps the result `gofmt`-clean and the diff free of a blank line nobody
    would have written.
    """
    return _replace(data, data.rfind(b"\n", 0, start) + 1, end, "")


def _cut_statement(data: bytes, node: Node) -> str:
    """Remove a whole statement along with the line it sat on."""
    source = data.decode("utf-8")
    start = source.rfind("\n", 0, node.start_byte) + 1
    end = node.end_byte
    if source[end : end + 1] == "\n":
        end += 1
    return source[:start] + source[end:]


def _replace(data: bytes, start: int, end: int, text: str) -> str:
    source = data.decode("utf-8")
    return source[:start] + text + source[end:]
