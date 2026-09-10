"""Fold the literal conditions Piranha leaves behind in Python.

Python is the one language here where the engine cannot finish the job, and the
reason is structural rather than a missing rule. Measured against 0.4.8:
replacing an ``if_statement`` with the statements of its block pastes the first
line at the ``if``'s column and leaves every later line at its ORIGINAL column;
capturing the whole ``(block)`` instead leaves every line over-indented. Both
produce source CPython rejects. **The engine cannot re-indent**, and in Python
indentation is the block structure.

So ``python.toml`` folds only a branch that is a SINGLE SIMPLE STATEMENT — the
one case where nothing needs re-indenting, because only the first line moves —
and everything else was refused: a multi-statement branch, a read bound to a
name, and ``elif True:``. A refusal is loud and actionable, which is much better
than a half-fold, but it is still a flag that can never be cleaned from that
repository without hand edits.

This module does the rest, after the engine has run, where the whole file is in
hand and re-indenting a region is ordinary text work. It has two passes, and
the runner chains them IN THIS ORDER because the first exists to give the
second something to act on:

1. :func:`inline_literal_bindings` — replace a name the transform bound to a
   boolean literal with that literal, and delete the binding. This is what
   turns ``use_legacy = True`` plus ``if use_legacy:`` into ``if True:``.
2. :func:`fold_literal_conditions` — fold the literal conditions, which is
   everything below.

The condition folder has four rewrites, and only the first two move anything:

* ``if True:`` — splice the block's statements in at the ``if``'s column, and
  drop every ``elif``/``else`` after it.
* ``if False:`` with an ``else:`` — splice the else block in the same way. With
  an ``elif`` instead, the first ``elif`` becomes the new ``if`` (a header-line
  edit at the same column, so nothing moves). With neither, the statement goes.
* ``elif True:`` — the header becomes ``else:`` and every clause after it is
  deleted. This is the rewrite ``python.toml`` calls impossible, and it is: the
  ``elif_clause`` node spans its own body, so replacing the node can only emit
  ``else:<body>`` on one line. Editing the HEADER LINE alone has neither
  problem, and the body does not move, because ``else:`` sits at the ``elif``'s
  column.
* ``elif False:`` — the clause is deleted. Already covered by a rule; handled
  here too so a fold that exposes one is not left half-finished.

Re-indentation never touches a line that begins inside a token. Shifting the
continuation lines of a triple-quoted string would change the string's VALUE,
which is the one way a re-indenter can silently corrupt a program rather than
break it loudly. The test is structural — is this byte inside a childless node —
so it costs no list of string node types to keep current.

Everything here still answers to Gate 1: this runs before it, so a fold that
produces something CPython will not parse is caught and the whole transform
rolled back, exactly as an engine-produced one would be.
"""

from __future__ import annotations

import ast
import io
import symtable
import tokenize

import tree_sitter_python
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_python.language())
_PARSER = Parser(_LANGUAGE)

#: Bound purely as a runaway guard. Each rewrite strictly removes a literal
#: condition, and there are finitely many, so this terminates on its own.
_MAX_ROUNDS = 200


def fold_literal_conditions(before: str, after: str) -> str:
    """Fold every ``if``/``elif`` the transform left with a literal condition.

    Nothing happens if ``before`` already had one. ``if True:`` is real code —
    a permanently-enabled toggle, a commented-out branch somebody kept — and
    once the file contains one this cannot tell the customer's from the fold's.
    Rewriting theirs would be an edit the flag removal never justified, so the
    whole file is left to Gate 1 instead. Same rule, same reason, as the Java
    unreachable-statement removal.

    Applied one rewrite at a time with a re-parse between, rather than
    collecting every edit and splicing them together. Folding changes the shape
    of the tree around it — dropping an ``elif`` chain, promoting a clause — so
    a batch of offsets computed against the original tree would be stale for
    every edit after the first.
    """
    if after == before or _literal_conditions(before):
        return after
    for _ in range(_MAX_ROUNDS):
        rewritten = _fold_once(after)
        if rewritten is None:
            return after
        after = rewritten
    return after


def _literal_conditions(source: str) -> int:
    """How many ``if``/``elif`` conditions in ``source`` are a boolean literal."""
    root = _PARSER.parse(source.encode("utf-8")).root_node
    return sum(
        1
        for node in _iter(root)
        if node.type in {"if_statement", "elif_clause"}
        and _literal_condition(node, "condition") is not None
    )


def _fold_once(source: str) -> str | None:
    """Apply the first foldable literal condition found; ``None`` if there is none."""
    data = source.encode("utf-8")
    root = _PARSER.parse(data).root_node
    for node in _iter(root):
        if node.type == "if_statement":
            literal = _literal_condition(node, "condition")
            if literal is not None:
                return _fold_if(data, node, literal)
        elif node.type == "elif_clause":
            literal = _literal_condition(node, "condition")
            if literal is not None:
                return _fold_elif(data, node, literal)
    return None


def _iter(node: Node):
    """Depth-first, in source order, so the OUTERMOST fold happens first.

    Order matters: folding an outer `if True:` can delete an inner one outright,
    and rewriting the inner one first would be work spliced away a moment later
    — or worse, an offset into text the outer rewrite has already moved.
    """
    yield node
    for child in node.children:
        yield from _iter(child)


def _literal_condition(node: Node, field: str) -> bool | None:
    """``True``/``False`` if this node's condition is that literal, else ``None``."""
    condition = node.child_by_field_name(field)
    if condition is None:
        return None
    if condition.type == "true":
        return True
    if condition.type == "false":
        return False
    return None


def _clauses(node: Node) -> list[Node]:
    """The ``elif``/``else`` clauses hanging off an ``if_statement``.

    Python's chain is FLAT — one ``if_statement`` carries a list of
    ``alternative:`` children rather than nesting another ``if`` inside an else —
    which is what makes promoting a clause a local edit here and a disaster in
    the C-family languages.
    """
    return [
        child
        for child in node.children
        if child.type in {"elif_clause", "else_clause"}
    ]


def _fold_if(data: bytes, node: Node, taken: bool) -> str:
    """Rewrite an ``if`` whose condition is a literal."""
    clauses = _clauses(node)
    if taken:
        # The `if` runs: keep its body, drop every alternative.
        return _splice(data, node, node.child_by_field_name("consequence"))
    if not clauses:
        # Nothing runs and there is nothing to promote: the statement goes.
        return _cut(data, node)
    first = clauses[0]
    if first.type == "else_clause":
        return _splice(data, node, _block_of(first))
    # The first `elif` becomes the new `if`. A header-line edit at the same
    # column, so no line in the body moves.
    return _promote_to_if(data, node, first)


def _fold_elif(data: bytes, node: Node, taken: bool) -> str:
    """Rewrite an ``elif`` whose condition is a literal."""
    if not taken:
        return _cut(data, node)
    # This clause is the one that runs, so it IS the else: everything after it
    # is unreachable and goes with the promotion.
    parent = node.parent
    following = []
    if parent is not None:
        clauses = _clauses(parent)
        position = next(
            (i for i, c in enumerate(clauses) if c.id == node.id), None
        )
        if position is not None:
            following = clauses[position + 1 :]
    text = data
    for clause in reversed(following):
        start, end = _statement_span(text, clause)
        text = text[:start] + text[end:]
    header_start, header_end = _header_span(text, node)
    indent = _indent_of(text, header_start)
    return (
        text[:header_start] + indent + b"else:\n" + text[header_end:]
    ).decode("utf-8")


def _promote_to_if(data: bytes, node: Node, clause: Node) -> str:
    """Delete the dead ``if`` header and its body; make the first ``elif`` the ``if``.

    Two edits, applied BACK TO FRONT. The header rewrite sits later in the file
    than the deletion, so doing it first leaves the deletion's offsets — both of
    which are before it — still valid. The other order would splice against text
    that had already moved.
    """
    header_start, header_end = _header_span(data, clause)
    indent = _indent_of(data, header_start)
    condition = clause.child_by_field_name("condition")
    condition_text = data[condition.start_byte : condition.end_byte]
    text = (
        data[:header_start]
        + indent
        + b"if "
        + condition_text
        + b":\n"
        + data[header_end:]
    )
    statement_start = _line_start(data, node.start_byte)
    return (text[:statement_start] + text[header_start:]).decode("utf-8")


def _splice(data: bytes, node: Node, block: Node | None) -> str:
    """Replace ``node`` with ``block``'s statements, re-indented to its column."""
    if block is None:  # pragma: no cover - a clause always has a body
        return _cut(data, node)
    start, end = _statement_span(data, node)
    target = len(_indent_of(data, start))
    body_start = _line_start(data, block.start_byte)
    body = data[body_start : _line_end(data, block.end_byte)]
    shifted = _shift(body, target - len(_indent_of(data, body_start)), data, body_start)
    return (data[:start] + shifted + data[end:]).decode("utf-8")


def _shift(body: bytes, delta: int, whole: bytes, offset: int) -> bytes:
    """Move every line of ``body`` by ``delta`` columns, sparing string interiors.

    ``whole``/``offset`` locate ``body`` inside the file so the token test can be
    made against the real parse tree rather than against a fragment, which would
    not parse on its own once it is dedented past column zero.
    """
    if delta == 0:
        return body
    leaves = _leaf_spans(whole)
    out: list[bytes] = []
    position = offset
    for line in body.splitlines(keepends=True):
        stripped = line.lstrip()
        if not stripped.strip():
            # A blank line carries no indentation worth preserving, and shifting
            # it would leave trailing whitespace behind.
            out.append(line)
        elif any(begin <= position < end for begin, end in leaves):
            # Inside a multi-line string: its leading whitespace is DATA.
            out.append(line)
        elif delta > 0:
            out.append(b" " * delta + line)
        else:
            removable = len(line) - len(stripped)
            out.append(line[min(-delta, removable) :])
        position += len(line)
    return b"".join(out)


def _leaf_spans(data: bytes) -> list[tuple[int, int]]:
    """Byte ranges of every childless node — see ``syntax._leaf_spans``."""
    root = _PARSER.parse(data).root_node
    return [
        (node.start_byte, node.end_byte)
        for node in _iter(root)
        if node.child_count == 0
    ]


def _cut(data: bytes, node: Node) -> str:
    start, end = _statement_span(data, node)
    return (data[:start] + _filler(data, node, start) + data[end:]).decode("utf-8")


def _filler(data: bytes, node: Node, start: int) -> bytes:
    """``pass`` when removing ``node`` would leave its block empty, else nothing.

    Python has no empty suite, so a deletion that takes the only statement out
    of a ``with``/``try``/``if``/``def`` body produces an ``IndentationError``.
    Gate 1 catches that, but catching it costs the WHOLE FLAG — a Gate 1
    refusal is flag-wide, so one such file abandons the removal everywhere.

    ``pass`` rather than refusing, because the enclosing statement usually
    still has to run: emptying `with lock():` is exactly the case, and the lock
    is the point. Comments do not count as statements — a block left holding
    only a comment is still an error — but they are left in place.
    """
    parent = node.parent
    if parent is None or parent.type != "block":
        return b""
    statements = [
        child for child in parent.named_children if child.type != "comment"
    ]
    if len(statements) != 1:
        return b""
    return _indent_of(data, start) + b"pass\n"


def _statement_span(data: bytes, node: Node) -> tuple[int, int]:
    """``node``'s byte range widened to whole lines.

    A statement owns its indentation and its newline; leaving either behind
    turns a deletion into a blank line or, worse, a stray indent that changes
    what block the next statement belongs to.
    """
    return _line_start(data, node.start_byte), _line_end(data, node.end_byte)


def _block_of(clause: Node) -> Node | None:
    """A clause's suite. ``elif`` calls it ``consequence``, ``else`` calls it ``body``.

    Named explicitly rather than taken as "the last child" — the two spellings
    are a grammar detail that has already cost one wrong answer here, and a
    positional guess would go wrong silently rather than loudly.
    """
    return clause.child_by_field_name("consequence") or clause.child_by_field_name(
        "body"
    )


def _header_span(data: bytes, clause: Node) -> tuple[int, int]:
    """The clause's header — ``elif <cond>:`` — up to where its body begins.

    This is what makes promoting a clause possible at all. The node spans its
    body too, so replacing the NODE can only emit ``else:<body>`` on one line;
    replacing the header alone leaves every body line exactly where it is.

    The end is the start of the body's first line rather than the end of the
    clause's first line, so a condition spread over several lines is covered
    too.
    """
    start = _line_start(data, clause.start_byte)
    block = _block_of(clause)
    end = (
        _line_start(data, block.start_byte)
        if block is not None
        else _line_end(data, clause.end_byte)
    )
    return start, end


def _line_start(data: bytes, position: int) -> int:
    return data.rfind(b"\n", 0, position) + 1


def _line_end(data: bytes, position: int) -> int:
    found = data.find(b"\n", position)
    return len(data) if found == -1 else found + 1


def _indent_of(data: bytes, line_start: int) -> bytes:
    line = data[line_start : _line_end(data, line_start)]
    return line[: len(line) - len(line.lstrip())]


# ---------------------------------------------------------------------------
# A read bound to a name (#2672)
# ---------------------------------------------------------------------------


def inline_literal_bindings(before: str, after: str) -> str:
    """Inline a local the transform bound to a boolean literal, and drop it.

    ``use_legacy = client.variation(…)`` becomes ``use_legacy = True`` and
    stops there: Python has no built-in ``variable_inline_cleanup`` and no
    rules file of ours can supply one, because the useful cases need the
    binding DELETED and its references rewritten in the same breath — two
    edits in different places, which is a whole-file text job rather than a
    query rewrite. So this ran into the residue gate and the file was refused,
    making Python the only language of the eleven where an ordinary shape
    failed the run outright rather than producing a pull request.

    Run BEFORE :func:`fold_literal_conditions`, which is the point: inlining
    turns ``if use_legacy:`` into ``if True:``, and the condition folder then
    removes it. Neither pass finishes the job alone.

    The safety argument has two halves, and neither can see what the other
    does. Both must hold, per name:

    * **``symtable`` — CPython's own binding analysis — must place the name in
      exactly ONE scope, and that scope must be a function.** This is what
      stops the rewrite that runs and is wrong: two functions can each say
      ``use_legacy``, one binding a local and the other reading a module
      global, and every occurrence balances. Only a scope analysis can tell
      those apart. Requiring a FUNCTION scope is the same call
      ``ts_const.toml`` makes for an exported const — a module-level or
      class-body binding is public API, its importers do not contain the flag
      key so they are not even in the candidate set, and deleting it would
      break a file this run cannot see.
    * **every NAME token spelling the name must be one this analysis models**
      — the binding itself, a load, an attribute, or a keyword-argument name.
      This is the completeness half, and it is what makes the first half
      trustworthy: ``except E as use_legacy`` is a second binder that symtable
      describes in exactly the same words as the assignment, and that carries
      no ``Name`` node to be found by walking for binding contexts. Counting
      tokens sees it without having to know what it is, which closes the
      binder forms nobody here has enumerated — including any a later Python
      adds. Enumerating re-binding forms is what leaked twice in TypeScript;
      this does not enumerate them.

    Differential per NAME rather than per file, unlike the condition folder
    above. ``if True:`` is rare enough in real source that bailing on the whole
    file costs almost nothing, but ``DEBUG = False`` sits at the top of a great
    many modules — bailing there would mean this pass almost never fires. A
    name already bound to a literal BEFORE the transform is the customer's and
    is left alone; only a name the transform newly bound to one is inlined.
    """
    if after == before:
        return after
    preexisting = _literal_binding_names(before)
    if preexisting is None:  # pragma: no cover - Gate 1 owns unparseable input
        return after
    for _ in range(_MAX_ROUNDS):
        rewritten = _inline_once(after, preexisting)
        if rewritten is None:
            return after
        after = rewritten
    return after  # pragma: no cover - bounded by the number of bindings


def _literal_binding_names(source: str) -> set[str] | None:
    """Names ``source`` already binds to a boolean literal; ``None`` if unparseable."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    return {
        binding[0]
        for node in ast.walk(tree)
        if (binding := _literal_binding(node)) is not None
    }


def _literal_binding(node: ast.AST) -> tuple[str, bool] | None:
    """``(name, value)`` if ``node`` binds one plain name to ``True``/``False``.

    Only a single-target assignment qualifies. ``a = b = <read>`` and
    ``a, b = <read>, x`` are left to the residue gate: both need the statement
    kept for the other binding's sake, which is a different edit from the one
    this module makes.
    """
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1:
            return None
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    else:
        return None
    value = node.value
    if not isinstance(target, ast.Name) or not isinstance(value, ast.Constant):
        return None
    # `is`, not `==`: `1 == True`, and folding `count = 1` would be a rewrite
    # of arithmetic rather than of a flag.
    if value.value is not True and value.value is not False:
        return None
    return target.id, value.value


def _inline_once(source: str, preexisting: set[str]) -> str | None:
    """Inline the first eligible binding; ``None`` if there is none.

    One edit per call with a re-parse between, for the reason
    :func:`fold_literal_conditions` gives: every offset after the first edit
    would otherwise be computed against text that has already moved.
    """
    try:
        tree = ast.parse(source)
        table = symtable.symtable(source, "<inline>", "exec")
    except (SyntaxError, ValueError):  # pragma: no cover - Gate 1 owns this
        return None
    occurrences = _name_token_counts(source)
    if occurrences is None:  # pragma: no cover - unparseable is caught above
        return None
    data = source.encode("utf-8")
    lines = _line_offsets(data)
    candidates = [
        (node, binding)
        for node in ast.walk(tree)
        if (binding := _literal_binding(node)) is not None
        and binding[0] not in preexisting
    ]
    # Source order, so a file with several is rewritten the same way every run.
    candidates.sort(key=lambda item: (item[0].lineno, item[0].col_offset))
    for node, (name, value) in candidates:
        edited = _inline_binding(
            name, value, node, tree, table, occurrences, data, lines
        )
        if edited is not None:
            return edited
    return None


def _inline_binding(
    name: str,
    value: bool,
    node: ast.AST,
    tree: ast.AST,
    table: symtable.SymbolTable,
    occurrences: dict[str, int],
    data: bytes,
    lines: list[int],
) -> str | None:
    """The two-part safety check, then the edit. ``None`` if it does not hold."""
    if not _confined_to_one_function(name, table):
        return None
    loads = [
        found
        for found in ast.walk(tree)
        if isinstance(found, ast.Name)
        and found.id == name
        and isinstance(found.ctx, ast.Load)
    ]
    modelled = len(loads) + _unrelated_uses(tree, name) + 1  # +1: the binding
    if occurrences.get(name, 0) != modelled:
        return None
    span = _whole_lines(data, lines, node)
    if span is None:
        return None
    start, end = span
    replacements = [
        (
            _offset(lines, found.lineno, found.col_offset),
            _offset(lines, found.end_lineno, found.end_col_offset),
        )
        for found in loads
    ]
    if any(start <= begin < end for begin, _ in replacements):
        # Unreachable as things stand, and kept because what makes it so is a
        # guard three lines up rather than anything about this edit: a
        # reference sharing the binding's line would be deleted WITH the line
        # and its replacement spliced into text that no longer exists. Removing
        # both is what `test_a_binding_sharing_its_line_is_left_alone` measures.
        return None  # pragma: no cover
    literal = b"True" if value else b"False"
    edits = [(begin, finish, literal) for begin, finish in replacements]
    edits.append((start, end, _binding_filler(tree, node, data, start)))
    text = data
    for begin, finish, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[finish:]
    return text.decode("utf-8")


def _confined_to_one_function(name: str, table: symtable.SymbolTable) -> bool:
    """Whether ``name`` names one function-local and nothing else in the file.

    Every scope that so much as mentions the name appears in ``symtable`` —
    including a function that only READS it as a global, and a nested function
    that closes over it — so "exactly one scope holds it" is a single check
    that covers shadowing, closures and same-named globals at once. It refuses
    the closure case, which would in fact be sound to inline; that is the price
    of an invariant worth one line and one sentence.
    """
    holders = []
    for scope in _scopes(table):
        try:
            symbol = scope.lookup(name)
        except KeyError:
            continue
        # Carry the SCOPE with its symbol. Reading the loop variable after the
        # loop instead reads whichever scope came last in the file, so the
        # answer moved when an unrelated class was declared below the function
        # rather than above it.
        holders.append((scope, symbol))
        if len(holders) > 1:
            return False
    if not holders:
        return False
    scope, symbol = holders[0]
    if scope.get_type() != "function":
        return False
    return (
        symbol.is_local()
        and symbol.is_assigned()
        and not symbol.is_parameter()
        and not symbol.is_global()
        and not symbol.is_free()
        and not symbol.is_imported()
        # Bound to a `def`/`class` of the same name somewhere in the scope.
        and not symbol.is_namespace()
    )


def _scopes(table: symtable.SymbolTable) -> list[symtable.SymbolTable]:
    """``table`` and every scope nested inside it, depth-first."""
    found = [table]
    for child in table.get_children():
        found.extend(_scopes(child))
    return found


def _binding_filler(tree: ast.AST, node: ast.AST, data: bytes, start: int) -> bytes:
    """:func:`_filler`, for the ``ast`` side. Same hazard, same answer.

    Kept separate rather than shared because the two passes hold the file in
    different parsers, and asking tree-sitter about a node ``ast`` found would
    mean re-locating it by offset — more moving parts than the check is worth.
    """
    for owner in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(owner, field, None)
            if not isinstance(block, list):
                continue
            if not any(statement is node for statement in block):
                continue
            # A module may legitimately end up empty; a suite may not.
            if isinstance(owner, ast.Module) or len(block) > 1:
                return b""
            return _indent_of(data, start) + b"pass\n"
    return b""  # pragma: no cover - every statement is in some block


def _unrelated_uses(tree: ast.AST, name: str) -> int:
    """Occurrences of ``name`` that spell something other than the variable.

    An attribute (``o.use_legacy``) and a keyword-argument name
    (``render(use_legacy=use_legacy)``) are NAME tokens that never refer to a
    local. Both are ordinary Python — the keyword one especially, where the
    argument is conventionally named after the variable passed to it — so they
    are modelled rather than refused, and left exactly as they are.
    """
    return sum(
        1
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr == name)
        or (isinstance(node, ast.keyword) and node.arg == name)
    )


def _name_token_counts(source: str) -> dict[str, int] | None:
    """How many NAME tokens spell each identifier; ``None`` if untokenisable."""
    counts: dict[str, int] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME:
                counts[token.string] = counts.get(token.string, 0) + 1
    except (tokenize.TokenError, SyntaxError):  # pragma: no cover - see above
        return None
    return counts


def _whole_lines(data: bytes, lines: list[int], node: ast.AST) -> tuple[int, int] | None:
    """``node``'s span widened to whole lines, or ``None`` if it shares one.

    Deleting whole lines is what keeps this edit simple, and it is also what
    makes a `;`-joined neighbour collateral damage — so a statement that does
    not have its lines to itself is left to the residue gate. A trailing
    comment goes with it: it annotates the binding being removed.
    """
    start = _offset(lines, node.lineno, node.col_offset)
    end = _offset(lines, node.end_lineno, node.end_col_offset)
    line_start = _line_start(data, start)
    line_end = _line_end(data, end)
    if data[line_start:start].strip():
        return None
    trailing = data[end:line_end].strip()
    if trailing and not trailing.startswith(b"#"):
        return None
    return line_start, line_end


def _line_offsets(data: bytes) -> list[int]:
    """Byte offset of the start of every line, for resolving ``ast`` positions.

    Built by scanning for ``\n`` alone rather than with ``splitlines``, which
    also breaks on form feed and vertical tab — characters CPython's tokeniser
    does NOT count as line breaks, so using it would put every position after
    one on the wrong line.
    """
    offsets = [0]
    position = data.find(b"\n")
    while position != -1:
        offsets.append(position + 1)
        position = data.find(b"\n", position + 1)
    return offsets


def _offset(lines: list[int], lineno: int, column: int) -> int:
    """An ``ast`` position as a byte offset.

    ``col_offset`` is documented as a UTF-8 byte offset within its line, not a
    character index, so this needs no decoding to be exact on non-ASCII source.
    """
    return lines[lineno - 1] + column
