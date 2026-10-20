"""Resolve a Python flag read the transform left bound to a name.

``use_legacy = client.variation(…)`` becomes ``use_legacy = True`` in the
engine, and stops there. Finishing it means deleting the binding AND rewriting
every reference to it, two edits in different places, which needs the whole
file in hand and a scope analysis no query rule has. So it happens here, after
the engine has run:

* :func:`inline_literal_bindings` replaces the name with the literal, or with a
  call to :func:`marker_for`'s marker so that the engine can create the literal
  inside its own cascade, and deletes the binding.

Folding the literal conditions that result, multi-statement branches and
``elif True:`` included, is the engine's job: it re-indents what it splices.

Everything here still answers to Gate 1: this runs before it, so an edit that
produces something CPython will not parse is caught and the whole transform
rolled back, exactly as an engine-produced one would be.
"""

from __future__ import annotations

import ast
import hashlib
import io
import symtable
import tokenize


#: Bound purely as a runaway guard. Each rewrite strictly removes a binding,
#: and there are finitely many, so this terminates on its own.
_MAX_ROUNDS = 200

#: Shared verbatim with :data:`flag_cleanup.kotlin_sentinel._SENTINEL_PREFIX`,
#: and deliberately not imported from it: the two mark different things at
#: different times (that one rewrites a CALLEE before the engine runs, this one
#: stands in for a NAME after it) and only the "no real codebase contains this"
#: property is common. They can never meet — one language each.
_MARKER_PREFIX = "__ff_cleanup_"


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


def marker_for(flag_key: str) -> str:
    """The call this pass stands in for an inlined read of ``flag_key``.

    A valid Python identifier whatever the key looks like (hex only), and
    derived from the key rather than random so a run is reproducible and a
    failure can be reasoned about from the flag alone — both for
    :func:`~flag_cleanup.kotlin_sentinel.sentinel_for`'s reasons.
    """
    digest = hashlib.sha256(flag_key.encode("utf-8")).hexdigest()[:16]
    return f"{_MARKER_PREFIX}{digest}"


def inline_literal_bindings(
    before: str, after: str, marker: str | None = None
) -> str:
    """Inline a local the transform bound to a boolean literal, and drop it.

    ``use_legacy = client.variation(…)`` becomes ``use_legacy = True`` and
    stops there: Python has no built-in ``variable_inline_cleanup`` and no
    rules file of ours can supply one, because the useful cases need the
    binding DELETED and its references rewritten in the same breath — two
    edits in different places, which is a whole-file text job rather than a
    query rewrite. So this ran into the residue gate and the file was refused,
    making Python the only language of the eleven where an ordinary shape
    failed the run outright rather than producing a pull request.

    Inlining turns ``if use_legacy:`` into ``if True:``, which the engine then
    folds. Neither step finishes the job alone.

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

    Differential per NAME rather than per file. ``DEBUG = False`` sits at the
    top of a great many modules, so bailing on any file that already binds a
    literal would mean this pass almost never fires. A
    name already bound to a literal BEFORE the transform is the customer's and
    is left alone; only a name the transform newly bound to one is inlined.

    ``marker``, when given, is substituted as ``marker(True)`` in place of the
    bare literal so the caller can run the engine over the result and have the
    literal created INSIDE its cascade — see
    ``rules/python.toml``'s marker rule and
    :func:`flag_cleanup.piranha_runner._inline_bound_reads` for why a literal
    written here reaches no edge and stops where it lands (#3029). Passing
    ``None`` substitutes the literal directly, which is what the later,
    engine-less call still does as the fallback for a file the marker pass
    declined.
    """
    if after == before:
        return after
    if marker is not None and marker in after:
        # Astronomically unlikely — a SHA-256 prefix behind a reserved-looking
        # name — and refused rather than worked around, exactly as
        # `kotlin_sentinel` refuses its own: once the name is already in the
        # file, "did a marker survive the engine?" cannot tell ours from
        # theirs, and that question is the only thing standing between a
        # broken identifier and a pull request. Returning `after` unchanged
        # leaves the binding standing, which the residue gate then refuses
        # loudly.
        return after
    preexisting = _literal_binding_names(before)
    if preexisting is None:  # pragma: no cover - Gate 1 owns unparseable input
        return after
    for _ in range(_MAX_ROUNDS):
        rewritten = _inline_once(after, preexisting, marker)
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


def _inline_once(
    source: str, preexisting: set[str], marker: str | None = None
) -> str | None:
    """Inline the first eligible binding; ``None`` if there is none.

    One edit per call with a re-parse between: a deletion moves every offset
    after it, so a batch of edits computed against the original text would be
    stale for every edit after the first.
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
            name, value, node, tree, table, occurrences, data, lines, marker
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
    marker: str | None = None,
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
    literal = "True" if value else "False"
    # A CALL rather than a bare literal when the caller is going to re-run the
    # engine. Substituting a call for a name can never change precedence — a
    # call binds tightest — and a call is legal in every position a load of the
    # name was.
    substitute = (
        f"{marker}({literal})" if marker is not None else literal
    ).encode("utf-8")
    edits = [(begin, finish, substitute) for begin, finish in replacements]
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
