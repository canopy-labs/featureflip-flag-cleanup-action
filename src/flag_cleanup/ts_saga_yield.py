"""Drop the ``yield`` a folded redux-saga accessor read leaves behind.

``const on = yield call(isEnabled, KEY)`` folds to ``const on = yield true``.
redux-saga's middleware resumes a generator with the yielded value itself when
it is not an effect, so ``yield true`` evaluates to ``true``, and the ``yield``
only stops the constant fold from carrying the value into the code that used
it (``if (on)``, ``return on``). This pass writes the bare literal instead,
and the fold's local-literal inliner takes it from there.

That equivalence holds only while the saga middleware drives the generator.
Anything else that iterates it (``yield* gen()``, ``all([...gen()])`` read as a
list of steps, ``for…of``, a spread, ``.next()``, ``Object.values(sagas)``, a
test helper) sees one value fewer once the ``yield`` is gone. Every importer
that could do so cannot be found (a ``baseUrl`` or bundler alias, a backtick
``require``, ``require.context``, a Vite glob, a Vue or Svelte file), so the
rule is narrow enough to be decided inside ONE module:

* **A generator defined and driven in the same module, and exported in no
  way**: a ``function*`` declaration, or a ``const`` bound to a ``function*``,
  with no ``export``, no ``export { gen }``, no ``module.exports``, and every
  reference to its name in the file the generator argument of a redux-saga
  ``fork``, ``spawn``, ``call``, ``takeEvery``, ``takeLatest``,
  ``takeLeading``, ``throttle``, ``debounce``, ``retry``, ``runSaga``, or of
  ``<x>.run(...)`` where ``x`` is ``const x = createSagaMiddleware(...)``.
  At least one such reference must exist.
* **An anonymous generator that is itself such an argument**
  (``takeEvery('A', function* () { … })``).
* The callee is judged by its binding: ``fork`` and the rest count only when
  nothing else in the file binds that name (``(fork) => fork(init)`` does not).
  The yielded ``call``/``select`` is judged the same way, and must be imported
  from redux-saga (from ``typed-redux-saga`` for ``yield*``).
* **No direct ``eval``** in the file (it reads every binding of the module).
* **Not async**, and the engine wrote the literal: found by aligning the
  customer's lines with the output's, never by a literal the customer wrote.

An exported saga keeps ``yield <literal>``. A bare ``yield true;`` statement is
deleted with its line, but only as a statement of a block or the program: the
body of an unbraced ``if`` would make the next statement conditional.

:func:`delegates_to_literal` is the gate under all of it. ``yield* true``
throws when run (``true is not iterable``), so a ``yield*`` of a boolean, however
cast or parenthesised, never reaches disk: the caller refuses the file.
"""

from __future__ import annotations

import difflib

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

_SAGA_SOURCES = frozenset(
    {
        "redux-saga",
        "redux-saga/effects",
        "@redux-saga/core",
        "@redux-saga/core/effects",
        "typed-redux-saga",
        "typed-redux-saga/macro",
    }
)
_TYPED_SOURCES = frozenset({"typed-redux-saga", "typed-redux-saga/macro"})
#: Modules whose default export is ``createSagaMiddleware``.
_MIDDLEWARE_SOURCES = frozenset({"redux-saga", "@redux-saga/core"})
#: Effect creators that hand a generator to the saga runtime, and which
#: argument holds it.
_DRIVER_ARGUMENT = {
    "fork": 0,
    "spawn": 0,
    "call": 0,
    "takeEvery": 1,
    "takeLatest": 1,
    "takeLeading": 1,
    "throttle": 2,
    "debounce": 2,
    "retry": 2,
    "runSaga": 1,
}
_YIELDED = frozenset({"call", "select"})
_LITERALS = frozenset({"true", "false"})
#: Wrappers that leave the value as it was (a cast, a parenthesis, a ``!``).
_TRANSPARENT = frozenset(
    {
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
        "non_null_expression",
        "type_assertion",
    }
)
_FUNCTIONS = frozenset(
    {
        "generator_function_declaration",
        "generator_function",
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
    }
)


def _text(node: Node) -> str:
    return node.text.decode("utf-8")


def _peel(node: Node | None) -> Node | None:
    """``node`` with casts, parentheses and ``!`` stripped (recursively)."""
    while node is not None and node.type in _TRANSPARENT:
        inner = [c for c in node.named_children if c.type != "comment"]
        if node.type in ("as_expression", "satisfies_expression", "type_assertion"):
            # `x as T`, `x satisfies T`, `<T>x`: the value is the expression.
            inner = inner[:1] if node.type != "type_assertion" else inner[-1:]
        node = inner[0] if inner else None
    return node


def _yield_argument(node: Node) -> Node | None:
    arguments = [c for c in node.named_children if c.type != "comment"]
    return _peel(arguments[0]) if len(arguments) == 1 else None


def _delegates(node: Node) -> bool:
    return any(child.type == "*" for child in node.children)


def _literal_yield(node: Node, delegating_ok: bool) -> Node | None:
    """The ``true``/``false`` of a ``yield_expression``, or ``None``."""
    if node.type != "yield_expression" or (_delegates(node) and not delegating_ok):
        return None
    argument = _yield_argument(node)
    return argument if argument is not None and argument.type in _LITERALS else None


def delegates_to_literal(before: str, after: str, language: str) -> bool:
    """Whether ``after`` holds more ``yield* <boolean>`` than ``before``.

    The engine writes the literal in place of a ``call(accessor, KEY)`` whatever
    wraps it, so ``yield* call(...)`` becomes ``yield* true``, which throws
    "true is not iterable" when the saga runs. Casts, parentheses and ``!``
    around the literal count. Run on the final content, so it also covers a
    ``yield*`` that :func:`unwrap_yielded_literals` declined to rewrite.
    """
    if "yield" not in after or "*" not in after:
        return False

    def count(text: str) -> int:
        return sum(
            1
            for node in _walk(_parse(text, language))
            if node.type == "yield_expression"
            and _delegates(node)
            and _literal_yield(node, True) is not None
        )

    return count(after) > count(before)


# ---------------------------------------------------------------------------
# What redux-saga a file imports


class _Imports:
    """The names a file imports from redux-saga: ``names`` maps a local name
    to ``(imported name, source)``; ``namespaces`` maps a local namespace."""

    def __init__(self, root: Node) -> None:
        self.root = root
        self.names: dict[str, tuple[str, str]] = {}
        self.namespaces: dict[str, str] = {}
        #: Local names of the default import of ``redux-saga`` (``createSagaMiddleware``).
        self.factories: set[str] = set()
        from flag_cleanup import cross_file_ts

        #: Names bound anywhere in the file by something other than an import:
        #: a parameter, a variable, a function. An imported name that is also
        #: bound that way might be the other binding at a use (`(fork) => fork(g)`),
        #: so it is not trusted.
        self.rebound = {
            _text(n)
            for n in binding_nodes(root)
            if n.parent is None or n.parent.type not in ("import_specifier", "import_clause", "namespace_import")
        }

        for statement in root.named_children:
            if statement.type != "import_statement":
                continue
            source = cross_file_ts._string_value(statement.child_by_field_name("source"))
            if source not in _SAGA_SOURCES:
                continue
            for node in _walk(statement):
                if node.type == "import_specifier":
                    name = node.child_by_field_name("name")
                    alias = node.child_by_field_name("alias")
                    if name is not None:
                        self.names[_text(alias if alias is not None else name)] = (_text(name), source)
                elif node.type == "namespace_import":
                    local = next((c for c in node.named_children if c.type == "identifier"), None)
                    if local is not None:
                        self.namespaces[_text(local)] = source
                elif (
                    node.type == "import_clause"
                    and source in _MIDDLEWARE_SOURCES
                    and not any(c.type in ("type", "typeof") for c in statement.children)
                ):
                    for child in node.named_children:
                        if child.type == "identifier":
                            self.factories.add(_text(child))

    def is_middleware(self, receiver: Node | None) -> bool:
        """Whether ``receiver`` is an identifier bound exactly once, by
        ``const x = createSagaMiddleware(...)``, with ``createSagaMiddleware``
        the default import of ``redux-saga`` / ``@redux-saga/core``."""
        if receiver is None or receiver.type != "identifier" or not self.factories:
            return False
        name = _text(receiver)
        binders = [n for n in binding_nodes(self.root) if _text(n) == name]
        imported = [
            n for n in _walk(self.root)
            if n.type == "identifier" and _text(n) == name and n.parent is not None
            and n.parent.type in ("import_specifier", "import_clause", "namespace_import", "import_require_clause")
        ]
        if len(binders) != 1 or imported:
            return False
        declarator = binders[0].parent
        if declarator.type != "variable_declarator":
            return False
        declaration = declarator.parent
        if declaration is None or declaration.type != "lexical_declaration" or not any(
            c.type == "const" for c in declaration.children
        ):
            return False
        value = _peel(declarator.child_by_field_name("value"))
        if value is None or value.type != "call_expression":
            return False
        callee = value.child_by_field_name("function")
        return (
            callee is not None
            and callee.type == "identifier"
            and _text(callee) in self.factories
            and _text(callee) not in self.rebound
        )

    def effect(self, callee: Node | None) -> tuple[str, str] | None:
        """``(effect name, source)`` when ``callee`` is a redux-saga effect creator."""
        if callee is None:
            return None
        if callee.type == "identifier":
            return None if _text(callee) in self.rebound else self.names.get(_text(callee))
        if callee.type == "member_expression":
            obj, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
            if obj is not None and prop is not None and obj.type == "identifier":
                source = None if _text(obj) in self.rebound else self.namespaces.get(_text(obj))
                if source is not None:
                    return _text(prop), source
        return None


def _yields_a_saga_effect(node: Node, imports: _Imports) -> bool:
    """Whether this ``yield`` yields a ``call``/``select`` imported from redux-saga
    (from typed-redux-saga when it delegates)."""
    argument = _yield_argument(node)
    if argument is None or argument.type != "call_expression":
        return False
    found = imports.effect(argument.child_by_field_name("function"))
    if found is None or found[0] not in _YIELDED:
        return False
    return found[1] in _TYPED_SOURCES if _delegates(node) else True


# ---------------------------------------------------------------------------
# The rewrite


def unwrap_yielded_literals(
    before: str,
    after: str,
    language: str,
    marker: str,
    path: str | None = None,
    repo_dir: str | None = None,
) -> str:
    """``after`` with each qualifying ``yield <literal>`` unwrapped (module docstring).

    The literal is written as a call of ``marker`` (see :mod:`flag_cleanup.ts_fold`):
    the engine turns it back into the literal inside its cascade, so an ``if``
    or ``&&`` around it folds. A bare literal written here would reach no edge.
    """
    if (
        after == before
        or marker in after
        or "yield" not in after
    ):
        return after
    old_root, root = _parse(before, language), _parse(after, language)
    if root.has_error or old_root.has_error:
        return after
    imports = _Imports(old_root)
    if not imports.names and not imports.namespaces:
        return after
    if _direct_eval(root, imports):
        return after  # `eval('[...init()]')` sees every binding of the module
    typed = any(source in _TYPED_SOURCES for _name, source in imports.names.values()) or any(
        source in _TYPED_SOURCES for source in imports.namespaces.values()
    )
    candidates = _engine_written(old_root, root, before, after, imports, typed)
    if not candidates:
        return after

    data = after.encode("utf-8")
    judged: dict[int, bool] = {}
    edits: list[tuple[int, int, bytes]] = []
    saga_imports = _Imports(root)
    for node in candidates:
        function = _enclosing_function(node)
        if function is None:
            continue
        if function.id not in judged:
            judged[function.id] = _driven_by_the_saga_runtime(function, root, path, repo_dir, saga_imports)
        if not judged[function.id]:
            continue
        literal = _literal_yield(node, typed)
        parent = node.parent
        if parent is not None and parent.type == "expression_statement":
            if parent.parent is None or parent.parent.type not in ("statement_block", "program"):
                continue
            span = _own_lines(parent, data)
            if span is None:
                continue
            edits.append((span[0], span[1], b""))
        else:
            text = data[literal.start_byte : literal.end_byte]
            edits.append((node.start_byte, node.end_byte, marker.encode() + b"(" + text + b")"))
    if not edits:
        return after
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8")


def _direct_eval(root: Node, imports: _Imports) -> bool:
    """Whether the file calls the bare identifier ``eval`` (a direct eval reads
    the module's bindings), unless the file binds the name itself."""
    if "eval" in imports.rebound:
        return False
    for node in _walk(root):
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type == "identifier" and _text(callee) == "eval":
                return True
    return False


def _engine_written(
    old_root: Node, root: Node, before: str, after: str, imports: _Imports, typed: bool
) -> list[Node]:
    """The ``yield <literal>`` nodes of ``after`` that replaced a yielded
    ``call``/``select`` of the customer's file.

    The two files are aligned on ``\\n`` lines only (tree-sitter rows count
    nothing else, and ``str.splitlines`` splits on form feeds and U+2028).
    A ``yield <literal>`` counts when its lines sit inside one rewritten block
    holding no ``yield <literal>`` of the customer's own, and at least as many
    yielded saga effects as the block now holds literals.
    """
    old_lines, new_lines = before.split("\n"), after.split("\n")
    effect_rows = [
        n.start_point[0] for n in _walk(old_root)
        if n.type == "yield_expression" and _yields_a_saga_effect(n, imports)
    ]
    old_literal_rows = [
        n.start_point[0] for n in _walk(old_root) if _literal_yield(n, True) is not None
    ]
    blocks = [
        (a0, a1, b0, b1)
        for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False
        ).get_opcodes()
        if tag == "replace"
    ]
    found = [n for n in _walk(root) if _literal_yield(n, typed) is not None]
    per_block: dict[int, list[Node]] = {}
    for node in found:
        for index, (_a0, _a1, b0, b1) in enumerate(blocks):
            if b0 <= node.start_point[0] and node.end_point[0] < b1:
                per_block.setdefault(index, []).append(node)
                break
    result: list[Node] = []
    for index, nodes in per_block.items():
        a0, a1, _b0, _b1 = blocks[index]
        if any(a0 <= row < a1 for row in old_literal_rows):
            continue
        if sum(1 for row in effect_rows if a0 <= row < a1) >= len(nodes):
            result.extend(nodes)
    return result


def _enclosing_function(node: Node) -> Node | None:
    current = node.parent
    while current is not None:
        if current.type in _FUNCTIONS:
            return current
        current = current.parent
    return None


def _own_lines(statement: Node, data: bytes) -> tuple[int, int] | None:
    start = data.rfind(b"\n", 0, statement.start_byte) + 1
    newline = data.find(b"\n", statement.end_byte)
    end = len(data) if newline == -1 else newline + 1
    if data[start : statement.start_byte].strip() or data[statement.end_byte : end].strip():
        return None
    return start, end


# ---------------------------------------------------------------------------
# Who drives the generator


def _driven_by_the_saga_runtime(
    function: Node, root: Node, path: str | None, repo_dir: str | None, imports: _Imports
) -> bool:
    """Whether this generator is defined and driven in this module alone.

    Only two shapes qualify, because only a module can be read whole:

    * a generator that is not exported in any way, every reference to which in
      the file is a driver argument (and at least one is);
    * an anonymous generator that is itself a driver argument.
    """
    if function.type not in ("generator_function_declaration", "generator_function"):
        return False
    if any(child.type == "async" for child in function.children):
        return False
    own = function.child_by_field_name("name")
    parent = function.parent
    if function.type == "generator_function_declaration":
        if own is None or (parent is not None and parent.type == "export_statement"):
            return False
        name_node = own
    else:
        if parent is not None and parent.type == "arguments":
            return own is None and _is_driver_argument(function, imports)
        if parent is None or parent.type != "variable_declarator":
            return False
        declaration = parent.parent
        if declaration is None or declaration.type != "lexical_declaration" or not any(
            c.type == "const" for c in declaration.children
        ):
            return False
        if declaration.parent is not None and declaration.parent.type == "export_statement":
            return False
        name_node = parent.child_by_field_name("name")
        if name_node is None or name_node.type != "identifier":
            return False
        if own is not None and _text(own) != _text(name_node):
            return False  # `const init = function* initSaga()`
    drivers = _driver_references(root, _text(name_node), name_node, imports)
    # A generator nothing hands to the saga runtime may be run by other code.
    return drivers is not None and drivers > 0


def _is_driver_argument(reference: Node, imports: _Imports) -> bool:
    """Whether ``reference`` is the generator argument of a redux-saga driver."""
    arguments = reference.parent
    if arguments is None or arguments.type != "arguments":
        return False
    call = arguments.parent
    if call is None or call.type != "call_expression":
        return False
    position = [c for c in arguments.named_children if c.type != "comment"].index(reference) if reference in [
        c for c in arguments.named_children if c.type != "comment"
    ] else -1
    callee = call.child_by_field_name("function")
    found = imports.effect(callee)
    if found is not None:
        return _DRIVER_ARGUMENT.get(found[0]) == position
    if callee is not None and callee.type == "member_expression":
        prop = callee.child_by_field_name("property")
        return (
            prop is not None
            and _text(prop) == "run"
            and position == 0
            and imports.is_middleware(callee.child_by_field_name("object"))
        )
    return False


_IDENTIFIERS = frozenset(
    {
        "identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "property_identifier",
        "type_identifier",
    }
)


def _driver_references(root: Node, name: str, declaration: Node, imports: _Imports) -> int | None:
    """How many references to ``name`` are driver positions, or ``None`` when
    one is not (a call, a spread, an export of any kind, an alias, a variable,
    an object member: anything but the argument of a redux-saga driver)."""
    drivers = 0
    for node in _walk(root):
        if node.type not in _IDENTIFIERS or _text(node) != name or node == declaration:
            continue
        if _is_driver_argument(node, imports):
            drivers += 1
            continue
        return None
    return drivers
