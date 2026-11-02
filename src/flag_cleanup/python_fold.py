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

from flag_cleanup import python_view


#: Bound purely as a runaway guard. Each rewrite strictly removes a binding,
#: and there are finitely many, so this terminates on its own.
_MAX_ROUNDS = 200

#: Shared verbatim with :data:`flag_cleanup.kotlin_sentinel._SENTINEL_PREFIX`,
#: and it has to be here: a Python wrapper call is replaced whole by
#: ``<sentinel>(True)``, which the marker rule folds to that argument. A language
#: whose sentinel keeps the call's own arguments must not share this identifier:
#: see :func:`flag_cleanup.ts_fold.marker_for`.
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

    * **``symtable`` — CPython's own binding analysis — must show the name as
      a plain local of the function that binds it, which no scope nested in
      that function mentions.** This is what stops the rewrite that runs and
      is wrong: two functions can each say ``use_legacy``, one binding a
      local and the other reading a module global, and every occurrence in
      the file balances. Only a scope analysis can tell those apart, and it
      also says why the OTHER function never matters: a local is invisible
      outside the function that binds it and the scopes nested in it, so
      everything here is judged within that one function, its own reads
      rewritten and every other function's left alone. Requiring a FUNCTION
      scope is the same call
      ``ts_const.toml`` makes for an exported const — a module-level or
      class-body binding is public API, its importers do not contain the flag
      key so they are not even in the candidate set, and deleting it would
      break a file this run cannot see.
    * **every NAME token spelling the name within that function's lines must
      be one this analysis models** — the binding itself, a load in its body,
      an attribute, or a keyword-argument name.
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


def _literal_binding_names(source: str) -> set[tuple[tuple[str, ...], str]] | None:
    """``(scope path, name)`` for each name ``source`` already binds to a
    boolean literal; ``None`` if unparseable.

    Keyed by the enclosing ``def``/``class`` names, not by the name alone:
    ``res = False`` in one function says nothing about the ``res`` another
    function binds, and keying by name would refuse a flag read in every
    function of a module where any one of them already held a literal.
    """
    try:
        tree = ast.parse(python_view.view(source))
    except (SyntaxError, ValueError):
        return None
    return {
        (path, binding[0])
        for node, path, _owner in _scoped_statements(tree)
        if (binding := _literal_binding(node)) is not None
    }


#: The ``ast`` nodes that open a scope a plain assignment can sit in directly.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _scoped_statements(
    tree: ast.AST,
) -> list[tuple[ast.AST, tuple[str, ...], ast.AST]]:
    """Every STATEMENT with the ``def``/``class`` names enclosing it and its
    innermost enclosing scope node (the module itself at top level).

    Statements only, reached through the fields that hold blocks: a binding
    this module rewrites is a statement, and no expression can contain one
    (a lambda's body is an expression), so walking expressions would only
    cost time. For the same reason a lambda or a comprehension is never an
    owner: a statement's scope is the nearest ``def``/``class``.
    """
    found: list[tuple[ast.AST, tuple[str, ...], ast.AST]] = []
    stack: list[tuple[ast.AST, tuple[str, ...], ast.AST]] = [(tree, (), tree)]
    while stack:
        node, path, owner = stack.pop()
        if node is not tree:
            found.append((node, path, owner))
        if isinstance(node, _SCOPES):
            path, owner = path + (node.name,), node
        for block in _blocks(node):
            stack.extend((statement, path, owner) for statement in block)
    return found


def _blocks(node: ast.AST) -> list[list[ast.AST]]:
    """The statement lists directly inside ``node``."""
    blocks = [
        block
        for field in ("body", "orelse", "finalbody")
        if isinstance(block := getattr(node, field, None), list)
    ]
    for holder in [*getattr(node, "handlers", ()), *getattr(node, "cases", ())]:
        blocks.append(holder.body)
    return blocks


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
    source: str, preexisting: set[tuple[tuple[str, ...], str]], marker: str | None = None
) -> str | None:
    """Inline the first eligible binding; ``None`` if there is none.

    One edit per call with a re-parse between: a deletion moves every offset
    after it, so a batch of edits computed against the original text would be
    stale for every edit after the first.
    """
    analysis = _Analysis.of(source)
    if analysis is None:  # pragma: no cover - Gate 1 owns unparseable input
        return None
    for node, name, value, owner in analysis.candidates(preexisting):
        if analysis.refusal(name, node, owner) is not None:
            continue
        edited = analysis.edit(name, value, node, owner, marker)
        if edited is not None:
            return edited
    return None


def unresolved_bindings(before: str, after: str) -> list[tuple[str, int, str]]:
    """``(name, line, why)`` for each name the transform newly bound to a
    literal that :func:`inline_literal_bindings` would leave standing.

    The residue gate refuses such a file whatever this says; this only
    explains the refusal in words, so a reader is told which name stopped
    the removal and why rather than "still bound to a name".
    """
    preexisting = _literal_binding_names(before)
    analysis = _Analysis.of(after)
    if preexisting is None or analysis is None:
        return []
    found = []
    for node, name, _value, owner in analysis.candidates(preexisting):
        why = analysis.refusal(name, node, owner)
        if why is None and _whole_lines(analysis.data, analysis.lines, node) is None:
            why = "it shares its line with another statement"
        if why is not None:
            found.append((name, node.lineno, why))
    return found


class _Analysis:
    """One parse of a file: its ``ast``, its symbol table and its NAME tokens."""

    def __init__(
        self,
        source: str,
        tree: ast.AST,
        table: symtable.SymbolTable,
        tokens: list[tuple[str, int]],
    ) -> None:
        self.tree = tree
        self.table = table
        self.tokens = tokens
        self.data = source.encode("utf-8")
        self.lines = _line_offsets(self.data)
        self.statements = _scoped_statements(tree)

    @classmethod
    def of(cls, source: str) -> _Analysis | None:
        # Newer syntax elsewhere in the file is read through a same-length view,
        # so every offset holds in `source` (see `python_view`). Tokens still
        # come from `source`, so a name only the view hides is still counted.
        analysed = python_view.view(source)
        try:
            tree = ast.parse(analysed)
            table = symtable.symtable(analysed, "<inline>", "exec")
        except (SyntaxError, ValueError):
            return None
        tokens = _name_tokens(source)
        if tokens is None:  # pragma: no cover - unparseable is caught above
            return None
        return cls(source, tree, table, tokens)

    def candidates(
        self, preexisting: set[tuple[tuple[str, ...], str]]
    ) -> list[tuple[ast.AST, str, bool, ast.AST]]:
        """Each new literal binding, in source order (so a file with several
        is rewritten the same way every run), with its owner scope node."""
        found = [
            (node, binding[0], binding[1], owner)
            for node, path, owner in self.statements
            if (binding := _literal_binding(node)) is not None
            and (path, binding[0]) not in preexisting
        ]
        found.sort(key=lambda item: (item[0].lineno, item[0].col_offset))
        return found

    def refusal(self, name: str, node: ast.AST, owner: ast.AST) -> str | None:
        """Why ``name`` cannot be inlined; ``None`` when both checks hold."""
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            where = "a class body" if isinstance(owner, ast.ClassDef) else "module level"
            return f"it is bound at {where}, where other modules may read it"
        why = _confined_to_its_function(name, owner, self.table)
        if why is not None:
            return why
        first, last = _span_lines(owner)
        counted = sum(1 for spelled, row in self.tokens if spelled == name and first <= row <= last)
        if counted != len(self.loads(name, owner)) + _unrelated_uses(owner, name) + 1:
            return (
                "the function spells the name somewhere other than a plain read "
                "(another binding, a `del`, a `global`/`nonlocal`, or a default)"
            )
        return None

    @staticmethod
    def loads(name: str, owner: ast.AST) -> list[ast.Name]:
        """Every read of ``name`` in the function's BODY.

        The body only: decorators, defaults and annotations are evaluated in
        the enclosing scope, so a ``name`` there is a different variable. It
        still counts as a token, which is what refuses the function.
        """
        return [
            found
            for statement in owner.body
            for found in ast.walk(statement)
            if isinstance(found, ast.Name)
            and found.id == name
            and isinstance(found.ctx, ast.Load)
        ]

    def edit(
        self,
        name: str,
        value: bool,
        node: ast.AST,
        owner: ast.AST,
        marker: str | None,
    ) -> str | None:
        """The edit itself, once :meth:`refusal` has passed."""
        data, lines = self.data, self.lines
        span = _whole_lines(data, lines, node)
        if span is None:
            return None
        start, end = span
        replacements = [
            (
                _offset(lines, found.lineno, found.col_offset),
                _offset(lines, found.end_lineno, found.end_col_offset),
            )
            for found in self.loads(name, owner)
        ]
        if any(start <= begin < end for begin, _ in replacements):
            # Unreachable as things stand, and kept because what makes it so
            # is a guard above rather than anything about this edit: a
            # reference sharing the binding's line would be deleted WITH the
            # line and its replacement spliced into text that no longer exists.
            # Removing both is what `test_a_binding_sharing_its_line_is_left_alone`
            # measures.
            return None  # pragma: no cover
        literal = "True" if value else "False"
        # A CALL rather than a bare literal when the caller is going to re-run
        # the engine. Substituting a call for a name can never change
        # precedence -- a call binds tightest -- and a call is legal in every
        # position a load of the name was.
        substitute = (
            f"{marker}({literal})" if marker is not None else literal
        ).encode("utf-8")
        edits = [(begin, finish, substitute) for begin, finish in replacements]
        edits.append((start, end, _binding_filler(self.tree, node, data, start)))
        text = data
        for begin, finish, replacement in sorted(edits, reverse=True):
            text = text[:begin] + replacement + text[finish:]
        return text.decode("utf-8")


def _confined_to_its_function(
    name: str, owner: ast.AST, table: symtable.SymbolTable
) -> str | None:
    """Why ``name`` is not one plain local of ``owner`` that only ``owner``
    reads; ``None`` when it is.

    Python resolves a name per FUNCTION: a local of one function is invisible
    to every scope that is not nested inside it, so another function binding
    the same name, a module global of that name, or a ``global`` declaration
    elsewhere are all different variables, and none of them blocks this one.
    What CAN reach it is a scope nested inside the function -- a nested
    ``def``, a lambda, a generator expression, or a class body, which reads an
    enclosing function's local as a free variable (``nonlocal`` too) -- so any
    nested scope mentioning the name at all is refused. That refuses the
    closure case, which would be sound to inline, for an invariant that is one
    line to check. A list, set or dict comprehension has no symbol table of
    its own from Python 3.12 (they are inlined), so its reads are the
    function's own and are rewritten with them; a comprehension that binds
    the name is a token the count below does not model, so it refuses.
    """
    matches = [
        scope
        for scope in _scopes(table)
        if scope.get_type() == "function"
        and scope.get_name() == owner.name
        and scope.get_lineno() == owner.lineno
    ]
    if len(matches) != 1:  # pragma: no cover - a def opens its own line
        return "its function could not be told apart from another"
    scope = matches[0]
    try:
        symbol = scope.lookup(name)
    except KeyError:  # pragma: no cover - the binding is in this scope
        return "its function does not bind it"
    if not (
        symbol.is_local()
        and symbol.is_assigned()
        and not symbol.is_parameter()
        and not symbol.is_global()
        and not symbol.is_free()
        and not symbol.is_nonlocal()
        and not symbol.is_imported()
        # Bound to a `def`/`class` of the same name somewhere in the scope.
        and not symbol.is_namespace()
    ):
        return "it is a parameter, an import, a def, or declared global or nonlocal"
    for nested in _scopes(scope)[1:]:
        if name in nested.get_identifiers():
            return (
                "a nested function, lambda, generator expression or class "
                "inside the same function also uses the name"
            )
    return None


def _span_lines(owner: ast.AST) -> tuple[int, int]:
    """The physical lines ``owner`` occupies, its decorators included.

    Lines are exact here: a ``def`` always opens its own logical line, and
    nothing but a comment can follow its last statement on the last one.
    """
    first = min([owner.lineno] + [d.lineno for d in owner.decorator_list])
    return first, owner.end_lineno


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


def _name_tokens(source: str) -> list[tuple[str, int]] | None:
    """``(spelling, line)`` of every NAME token; ``None`` if untokenisable."""
    found: list[tuple[str, int]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME:
                found.append((token.string, token.start[0]))
    except (tokenize.TokenError, SyntaxError):  # pragma: no cover - see above
        return None
    return found


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
