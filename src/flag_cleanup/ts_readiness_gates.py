"""Remove a wait for the flags to load that guards no flag read any more.

A React component that decides something from a flag often waits for the SDK
first, so that it does not act on the value a flag reads before the client is
ready:

.. code-block:: tsx

    export function BillingGate({ children }: { children: ReactNode }) {
      const enabled = useFlag('new-billing');
      const resolved = useFlagsResolved();
      if (!resolved) {
        return <Spinner />;
      }
      return enabled ? <>{children}</> : <Navigate to="/settings" />;
    }

Removing the flag folds ``enabled`` away and leaves the wait standing: the
component no longer reads a flag, yet still shows a spinner until the SDK
settles. That is latency for nothing. This pass takes it out, in three steps,
each of which stands on its own:

1. **The guard.** In a function from which THIS run removed the last read of
   the flag, an early return on a readiness signal (``if (!resolved) return
   …;``) and the binding it tests are deleted, when nothing left in the
   function can read a flag. A readiness signal is the SDK's own
   ``useFeatureflipStatus()`` (destructured ``isReady``), or a hook of the
   repository's own that resolves to it (below).
2. **The component.** When that leaves a component named for a gate
   (``…Gate``/``…Guard``) whose body only renders its ``children``, every
   element of it in the repository is replaced by its child: the component
   rendered nothing else.
3. **Its files.** The component's file, and a test file named for it, are
   deleted.

What "nothing left can read a flag" means is deliberately narrow: every name
left in the function, whether called, rendered or only handed on as a value
(``createElement(Page)``, ``<Route Component={Page} />``), is bound inside
it (``children`` among them), names an HTML element, or is a hook of React or
React Router or one of a few of their components that render nothing handed
to them (``Navigate``, ``Outlet``, ``Link``, …; never ``createElement``,
``cloneElement`` or ``lazy``). Anything else (the repository's own code,
another flag read, a component of its own) keeps the guard, since the wait
may still protect it.

A hook of the repository's own is a readiness signal when the import it is
reached through resolves, through named re-exports, to a function of no
parameters that calls ``useFeatureflipStatus`` and whose every call is one of
these: the Featureflip React SDK's ``useFeatureflipStatus`` and
``useFeatureflipClient``, React's state and effect hooks, a setter its own
``useState`` returned, the client's ``initialize`` (which joins the
initialization already in flight), ``.then`` on such a call, or a function of
the same file that keeps to the same list. Its only effects are then on its
own state and on the client's initialization, so deleting a call to it loses
nothing but the wait. What it returns must be a wait too: built with ``&&``,
``||`` and parentheses from the status's ``isReady``, its own ``useState``
values (written from nothing in the status) and ``isError`` as an
alternative, with ``isReady`` in it. ``return !isError`` is an error
fallback, not a wait, and is left alone. The same proof is skipped when ``inline-wrappers`` is
off, which leaves the SDK's own hook.

Steps 2 and 3 need every use of the component proven, the same proof
:mod:`flag_cleanup.ts_exported_props` makes for an exported component: every
spelling of its name in the repository's TypeScript and JavaScript is its
declaration, one unaliased import of it, or a JSX element name. Each element
must have no attributes and exactly one child element. Its file must hold
nothing but imports and the component, and nothing may name that file but the
import this pass deletes in each file (a ``vi.mock`` of the path in a file
that renders it keeps the component). A file that loaded a module for its
effects (``import './theme.css';``) keeps its file, unless the module is an
audited SDK or declared ``side-effect-free-modules``. A test file goes when it
is named for the component's file and holds, beside imports, declarations and
``vi``/``jest``/``before*``/``after*`` setup, only ``describe('<Component>',
…)`` groups.

A test file that spells the readiness hook (a mock of it does) and reaches
the guard's file through its imports drives the wait, so the wait stays, on
either path. When step 1's proof holds but the rest cannot follow, the guard
is deleted on its own unless a test file renders the component, since such a
test usually checks the wait itself; that guard stays. Every guard kept for one of
these reasons, and everything deleted, is named in the pull request.

The edits are the engine's: the guard's statements, each element's tags and
the import of a deleted file are marked, and rendered rules delete or unwrap
what is marked (see ``rules/ts_ctor_param.toml`` and
``rules/tsx_ctor_param.toml``). The files go through
:mod:`flag_cleanup.file_deletion`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_ts, file_deletion, git_ops, inert_load, source_index, ts_scope, ts_tests
from flag_cleanup.ctor_param_model import choose_sentinel, mark, render_rules
from flag_cleanup.inert_load import SIDE_EFFECT_FREE_PACKAGES
from flag_cleanup.ts_exported_props import _File, _exported_function, _users
from flag_cleanup.ts_syntax import _parse, _walk

logger = logging.getLogger(__name__)

#: The languages whose grammar holds JSX.
LANGUAGES = frozenset({"tsx", "js"})

#: ``bindings`` kinds this pass writes. Each is reported in the pull request.
REMOVED_GATE = "removed gate component"
REMOVED_GUARD = "removed readiness guard"
KEPT_GUARD = "kept readiness guard"

Row = tuple[str, str, str]

_GATE_NAME = re.compile(r"[A-Z][A-Za-z0-9_$]*(?:Gate|Guard)\Z")
_STATUS = "useFeatureflipStatus"
_CLIENT = "useFeatureflipClient"
_SDK_HOOKS = frozenset({_STATUS, _CLIENT})
#: Client methods a readiness hook may call. ``initialize`` returns the
#: initialization already started, so a second call does no new work.
_CLIENT_METHODS = frozenset({"initialize"})
_REACT_HOOKS = frozenset({"useState", "useEffect", "useLayoutEffect", "useMemo", "useCallback", "useRef"})
#: Packages none of whose exports can read a Featureflip flag.
_FLAGLESS_PACKAGES = frozenset({"react", "react-dom", "react-router", "react-router-dom"})
#: What of theirs a function may still use beside its hooks: components that
#: render nothing handed to them as a value. ``createElement``,
#: ``cloneElement``, ``lazy`` and their kin render a component they are given,
#: which may read a flag, so they are not here.
_FLAGLESS_COMPONENTS = frozenset({"Navigate", "Outlet", "Link", "NavLink", "Fragment", "StrictMode", "Suspense"})
#: Top-level calls a component's own test file may make beside its group.
_TEST_SETUP = frozenset({"vi", "jest", "beforeEach", "afterEach", "beforeAll", "afterAll"})
_ELEMENTS = frozenset({"jsx_element", "jsx_self_closing_element"})
_TAGS = frozenset({"jsx_opening_element", "jsx_self_closing_element"})
_TEST_SUFFIX = re.compile(r"\.(?:test|spec)\Z")
_TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _key(path: str) -> str:
    return str(Path(path).resolve())


@dataclass(eq=False)
class _Guard:
    """One readiness guard this run left guarding nothing."""

    path: str
    name: str
    function: Node
    #: The statements to delete: the guard, and the binding it tests.
    statements: list[Node]
    root: Node
    data: bytes
    summary: object
    #: The names a test would spell to drive the wait: the readiness hook as
    #: called and as exported, and the SDK hook under it.
    signals: frozenset[str] = frozenset()
    #: Why step 2 cannot follow, once asked.
    kept: list[str] = field(default_factory=list)


def remove(
    summaries,
    language: str,
    flag_key: str,
    repo_dir: str,
    rewrite: Callable,
    *,
    engine: Callable[[list[str], str], list],
    in_run: Callable[[str], bool],
    resolve_wrappers: bool = True,
) -> tuple[list, tuple[Row, ...]]:
    """``(summaries, rows)`` after removing every readiness guard this run
    left guarding no flag read, and the gate components that leaves empty.

    ``engine(paths, rules)`` runs rendered rules over ``paths``;
    ``in_run(path)`` says whether this run's engine processes ``path``.
    """
    if language not in LANGUAGES:
        return summaries, ()
    files = ts_scope.Files(repo_dir)
    rows: list[Row] = []
    summaries = list(summaries)
    for summary in list(summaries):
        if (
            file_deletion.deleted(summary)
            or summary.original_content == summary.content
            or not in_run(summary.path)
            or ts_scope.grammar(summary.path) not in LANGUAGES
        ):
            continue
        current = next((s for s in summaries if s.path == summary.path), None)
        if current is None or file_deletion.deleted(current):
            continue
        for name in _names(current):
            guard = _guard(current, name, flag_key, files, repo_dir, resolve_wrappers)
            if guard is None:
                continue
            summaries, done = _remove_gate(guard, summaries, language, repo_dir, rewrite, engine, in_run)
            if done is not None:
                rows += done
                break
            summaries, done = _remove_guard(guard, summaries, language, repo_dir, rewrite, engine)
            rows += done
            break
    return summaries, tuple(dict.fromkeys(rows))


# --- Step 1: the guard -------------------------------------------------------


def _top_functions(root: Node) -> dict[str, Node]:
    """Each top-level function or ``const`` bound to one, by name."""
    found: dict[str, Node] = {}
    for statement in root.named_children:
        declaration = statement
        if statement.type == "export_statement":
            if any(c.type == "default" for c in statement.children):
                continue
            declaration = statement.child_by_field_name("declaration")
            if declaration is None:
                continue
        if declaration.type == "function_declaration":
            name = declaration.child_by_field_name("name")
            if name is not None:
                found.setdefault(_text(name), declaration)
        elif declaration.type == "lexical_declaration":
            for declarator in declaration.named_children:
                if declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                name = declarator.child_by_field_name("name")
                if name is not None and value is not None and value.type in ("arrow_function", "function_expression"):
                    found.setdefault(_text(name), value)
    return found


def _names(summary) -> list[str]:
    root = _parse(summary.content, ts_scope.grammar(summary.path))
    if root.has_error:
        return []
    return sorted(_top_functions(root))


def _mentions_key(function: Node, flag_key: str) -> bool:
    return any(
        n.type == "string_fragment" and _text(n) == flag_key for n in _walk(function)
    )


def _unparenthesized(node: Node | None) -> Node | None:
    while node is not None and node.type == "parenthesized_expression":
        named = [c for c in node.named_children if c.type != "comment"]
        if len(named) != 1:
            return node
        node = named[0]
    return node


def _returns_only(consequence: Node | None) -> bool:
    if consequence is None:
        return False
    if consequence.type == "return_statement":
        return True
    if consequence.type != "statement_block":
        return False
    statements = [c for c in consequence.named_children if c.type != "comment"]
    return len(statements) == 1 and statements[0].type == "return_statement"


def _tested(statement: Node) -> Node | None:
    """``x`` in ``if (!x) return …;`` with no ``else``, else None."""
    if statement.type != "if_statement" or statement.child_by_field_name("alternative") is not None:
        return None
    if not _returns_only(statement.child_by_field_name("consequence")):
        return None
    condition = _unparenthesized(statement.child_by_field_name("condition"))
    if condition is None or condition.type != "unary_expression" or condition.children[0].type != "!":
        return None
    return _unparenthesized(condition.child_by_field_name("argument"))


def _bare_call(node: Node | None) -> Node | None:
    """The callee of a call with no arguments to a plain name, else None."""
    if node is None or node.type != "call_expression":
        return None
    callee = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if callee is None or callee.type != "identifier" or arguments is None:
        return None
    if [c for c in arguments.named_children if c.type != "comment"]:
        return None
    return callee


def _single_const(statement: Node) -> Node | None:
    if statement.type != "lexical_declaration" or not any(c.type == "const" for c in statement.children):
        return None
    declarators = [c for c in statement.named_children if c.type == "variable_declarator"]
    return declarators[0] if len(declarators) == 1 else None


def _guard(summary, name, flag_key, files, repo_dir, resolve_wrappers) -> _Guard | None:
    grammar = ts_scope.grammar(summary.path)
    before = _top_functions(_parse(summary.original_content, grammar)).get(name)
    if before is None or not _mentions_key(before, flag_key):
        return None
    reader = files.file(summary.path, summary.content)
    if reader is None or reader.root.has_error:
        return None
    function = _top_functions(reader.root).get(name)
    if function is None or _mentions_key(function, flag_key):
        return None
    body = function.child_by_field_name("body")
    if body is None or body.type != "statement_block":
        return None
    statements = [c for c in body.named_children if c.type != "comment"]
    for index, statement in enumerate(statements):
        tested = _tested(statement)
        if tested is None:
            continue
        found = _readiness(tested, statements[:index], function, reader, files, repo_dir, resolve_wrappers)
        if found is None:
            continue
        drop = [*found, statement]
        reason = _flag_read_left(function, drop, reader, repo_dir)
        if reason is not None:
            logger.debug("keeping the readiness guard in %s: %s", name, reason)
            return None
        signals = {_STATUS, *(_text(n) for n in _walk(tested) if n.type == "identifier")}
        for statement in found:
            called = _bare_call(_single_const(statement).child_by_field_name("value"))
            signals.add(_text(called))
        for local in list(signals):
            imported = _import_of(reader, local)
            if imported is not None and imported.imported not in (None, "default", "="):
                signals.add(imported.imported)
        return _Guard(
            summary.path, name, function, drop, reader.root, summary.content.encode("utf-8"), summary,
            frozenset(signals),
        )
    return None


def _readiness(tested, earlier, function, reader, files, repo_dir, resolve_wrappers) -> list[Node] | None:
    """The statements binding the readiness signal ``tested`` reads, or
    None when it is not one."""
    callee = _bare_call(tested)
    if callee is not None:
        return [] if _readiness_hook(callee, reader, files, repo_dir, resolve_wrappers) else None
    if tested.type != "identifier":
        return None
    local = _text(tested)
    uses = [
        n
        for n in _walk(function)
        if n.type in ("identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern")
        and _text(n) == local
    ]
    if len(uses) != 2:
        return None
    for statement in earlier:
        declarator = _single_const(statement)
        if declarator is None:
            continue
        target = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        called = _bare_call(value)
        if target is None or called is None or declarator.child_by_field_name("type") is not None:
            continue
        if target.type == "identifier" and _text(target) == local:
            ok = _readiness_hook(called, reader, files, repo_dir, resolve_wrappers)
            return [statement] if ok else None
        if target.type == "object_pattern":
            entries = [c for c in target.named_children if c.type != "comment"]
            if (
                len(entries) == 1
                and entries[0].type == "shorthand_property_identifier_pattern"
                and _text(entries[0]) == local == "isReady"
                and _sdk_import(reader, _text(called), repo_dir) == _STATUS
            ):
                return [statement]
    return None


def _import_of(file: ts_scope.File, local: str) -> ts_scope.Import | None:
    """The import binding ``local``, when nothing else in the file binds it.

    :meth:`ts_scope.File.import_of`, less one quirk: the binding scan counts
    a JSX element's name (``<Navigate …>``) as a binding of it."""
    bound = [
        n
        for n in ts_scope.bindings(file.root, local)
        if n.parent is None or n.parent.type not in (*_TAGS, "jsx_closing_element")
    ]
    hits = [i for i in file.imports if i.local == local]
    if len(bound) != 1 or len(hits) != 1:
        return None
    return hits[0]


def _sdk_import(file: ts_scope.File, local: str, repo_dir: str) -> str | None:
    """The Featureflip SDK export ``local`` is imported as, else None."""
    imported = _import_of(file, local)
    if (
        imported is None
        or imported.type_only
        or imported.specifier not in SIDE_EFFECT_FREE_PACKAGES
        or not cross_file_ts.names_package(file.path, imported.specifier, repo_dir)
    ):
        return None
    return imported.imported


def _package_import(file: ts_scope.File, local: str, packages: frozenset[str], repo_dir: str) -> str | None:
    imported = _import_of(file, local)
    if (
        imported is None
        or imported.type_only
        or imported.specifier not in packages
        or not cross_file_ts.names_package(file.path, imported.specifier, repo_dir)
    ):
        return None
    return imported.imported


def _readiness_hook(callee: Node, reader, files, repo_dir, resolve_wrappers) -> bool:
    """Whether calling ``callee()`` with no arguments returns a readiness
    boolean: a hook of the repository's own that the module docstring's
    audit accepts. (The SDK's own hook returns an object, so it is only a
    readiness signal destructured.)"""
    if not resolve_wrappers:
        return False
    local = _text(callee)
    declared = _declaration_of(reader, local, files, repo_dir)
    if declared is None:
        return False
    file, function = declared
    parameters = function.child_by_field_name("parameters")
    if parameters is None or [c for c in parameters.named_children if c.type != "comment"]:
        return False
    if any(c.type in ("async", "*") for c in function.children):
        return False
    status: list[bool] = []
    return (
        _keeps_to_the_audit(file, function, repo_dir, status, {function.id})
        and bool(status)
        and _returns_readiness(file, function, repo_dir)
    )


def _own_returns(function: Node) -> list[Node]:
    """The ``return`` statements of ``function`` itself, not of a function
    nested in it."""
    found: list[Node] = []
    stack = list(function.children)
    while stack:
        node = stack.pop()
        if node.type in ("function_declaration", "function_expression", "arrow_function", "method_definition"):
            continue
        if node.type == "return_statement":
            found.append(node)
        stack.extend(node.children)
    return found


def _returns_readiness(file: ts_scope.File, function: Node, repo_dir: str) -> bool:
    """Whether every value ``function`` returns is true only once the flags
    are ready (or the load failed): built with ``&&``, ``||`` and parentheses
    from the status's ``isReady``, its ``isError`` as an alternative
    (``… || isError``), and the hook's own ``useState`` values, with
    ``isReady`` in it. A negation, another field of the status, or anything
    else is not a wait: ``return !isError`` is an error fallback."""
    status: set[str] = set()
    state: set[str] = set()
    setters: set[str] = set()
    for node in _walk(function):
        if node.type != "variable_declarator":
            continue
        target = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        called = _bare_call(value)
        if called is not None and _sdk_import(file, _text(called), repo_dir) == _STATUS:
            entries = [c for c in target.named_children if c.type != "comment"] if target is not None else []
            if target is None or target.type != "object_pattern" or any(
                e.type != "shorthand_property_identifier_pattern" for e in entries
            ):
                return False
            status.update(_text(e) for e in entries)
            continue
        callee = value.child_by_field_name("function") if value is not None and value.type == "call_expression" else None
        if (
            target is not None
            and target.type == "array_pattern"
            and callee is not None
            and callee.type == "identifier"
            and _package_import(file, _text(callee), frozenset({"react"}), repo_dir) == "useState"
        ):
            entries = [c for c in target.named_children if c.type != "comment"]
            if not entries or entries[0].type != "identifier":
                return False
            state.add(_text(entries[0]))
            if len(entries) > 1 and entries[1].type == "identifier":
                setters.add(_text(entries[1]))
            if not _writes_no_status(value.child_by_field_name("arguments"), status):
                return False
    for node in _walk(function):
        if node.type == "call_expression" and _text(node.child_by_field_name("function")) in setters:
            if not _writes_no_status(node.child_by_field_name("arguments"), status):
                return False
    returns = _own_returns(function)
    if not returns or "isReady" not in status:
        return False
    for statement in returns:
        values = [c for c in statement.named_children if c.type != "comment"]
        if len(values) != 1 or _positive(values[0], status, state, False) is not True:
            return False
    return True


def _writes_no_status(arguments: Node | None, status: set[str]) -> bool:
    """Whether a state write reads no status field and negates nothing."""
    for node in _walk(arguments) if arguments is not None else ():
        if node.type == "identifier" and _text(node) in status:
            return False
        if node.type == "unary_expression" and node.children[0].type == "!":
            return False
    return True


def _positive(node: Node, status: set[str], state: set[str], alternative: bool) -> bool | None:
    """True when ``node`` holds ``isReady``, False when it is allowed without
    it, None when it is not allowed. ``alternative``: an operand of ``||``."""
    node = _unparenthesized(node)
    if node is None:
        return None
    if node.type == "identifier":
        name = _text(node)
        if name == "isReady" and name in status:
            return True
        if name in state or (alternative and name == "isError" and name in status):
            return False
        return None
    if node.type != "binary_expression":
        return None
    operator = node.child_by_field_name("operator")
    if operator is None or operator.type not in ("&&", "||"):
        return None
    either = operator.type == "||"
    left = _positive(node.child_by_field_name("left"), status, state, either)
    right = _positive(node.child_by_field_name("right"), status, state, either)
    if left is None or right is None:
        return None
    return left or right


def _declaration_of(reader: ts_scope.File, local: str, files, repo_dir) -> tuple[ts_scope.File, Node] | None:
    top = _top_functions(reader.root).get(local)
    if top is not None and top.type == "function_declaration":
        return (reader, top) if reader.binding(local) is not None else None
    imported = _import_of(reader, local)
    if imported is None or imported.type_only or imported.specifier is None or imported.imported in (None, "default", "="):
        return None
    module = _resolve(reader.path, imported.specifier, repo_dir)
    if module is None:
        return None
    return _exported_declaration(module, imported.imported, files, repo_dir, 0)


def _resolve(reader: str, specifier: str, repo_dir: str) -> str | None:
    """The file ``specifier`` names from ``reader``, read through a solution
    config's references too (Vite's default layout)."""
    return cross_file_ts.resolve_specifier(reader, specifier, repo_dir, solution=True)


def _exported_declaration(path: str, name: str, files, repo_dir, depth: int) -> tuple[ts_scope.File, Node] | None:
    """The function ``path`` exports as ``name``: declared there with
    ``export function``, or named in an ``export { … } from`` it makes."""
    if depth > 8:
        return None
    file = files.file(path)
    if file is None or file.root.has_error:
        return None
    for statement in file.root.named_children:
        if statement.type != "export_statement":
            continue
        source = statement.child_by_field_name("source")
        declaration = statement.child_by_field_name("declaration")
        if source is None:
            if (
                declaration is not None
                and declaration.type == "function_declaration"
                and _text(declaration.child_by_field_name("name")) == name
            ):
                return file, declaration
            continue
        specifier = cross_file_ts._string_value(source)
        for exported in _walk(statement):
            if exported.type != "export_specifier":
                continue
            original = exported.child_by_field_name("name")
            alias = exported.child_by_field_name("alias")
            if _text(alias if alias is not None else original) != name or specifier is None:
                continue
            target = _resolve(file.path, specifier, repo_dir)
            if target is None:
                return None
            return _exported_declaration(target, _text(original), files, repo_dir, depth + 1)
    return None


def _inside(node: Node | None, outer: Node) -> bool:
    return node is not None and outer.start_byte <= node.start_byte and node.end_byte <= outer.end_byte


def _declared_inside(file: ts_scope.File, use: Node, function: Node) -> Node | None:
    """The declaration ``use`` names, when it is one inside ``function``."""
    bound = file.binding_of(use)
    if bound is None or not _inside(bound, function):
        return None
    return bound


def _locally_declared(name: str, at: Node, function: Node) -> bool:
    """Whether a ``let``/``const``/``var`` inside ``function`` whose block
    holds ``at`` declares ``name``."""
    for node in _walk(function):
        if node.type != "variable_declarator" or _text(node.child_by_field_name("name")) != name:
            continue
        block = node.parent
        while block is not None and block.type not in ("statement_block", "program"):
            block = block.parent
        if block is not None and _inside(at, block) and _inside(block, function):
            return True
    return False


def _keeps_to_the_audit(file, function, repo_dir, status: list[bool], seen: set[int]) -> bool:
    for node in _walk(function):
        if node.type in ("new_expression", "await_expression", "yield_expression") or node.type.startswith("jsx_"):
            return False
        if node.type == "unary_expression" and node.children[0].type == "delete":
            return False
        if node.type in ("assignment_expression", "augmented_assignment_expression", "update_expression"):
            target = node.child_by_field_name("left") or node.child_by_field_name("argument")
            if target is None or target.type != "identifier" or not _locally_declared(_text(target), node, function):
                return False
        if node.type == "call_expression" and not _audited_call(node, file, function, repo_dir, status, seen):
            return False
    return True


def _audited_call(call, file, function, repo_dir, status, seen) -> bool:
    callee = call.child_by_field_name("function")
    if callee is None:
        return False
    if callee.type == "identifier":
        local = _text(callee)
        sdk = _sdk_import(file, local, repo_dir)
        if sdk in _SDK_HOOKS:
            if sdk == _STATUS:
                status.append(True)
            return True
        if _package_import(file, local, frozenset({"react"}), repo_dir) in _REACT_HOOKS:
            return True
        bound = _declared_inside(file, callee, function)
        if bound is not None:
            return _is_state_setter(bound, file, repo_dir)
        helper = _top_functions(file.root).get(local)
        if helper is not None and helper.type == "function_declaration" and file.binding(local) is not None:
            if helper.id in seen:
                return True
            seen.add(helper.id)
            return _keeps_to_the_audit(file, helper, repo_dir, status, seen)
        return False
    if callee.type != "member_expression":
        return False
    target = callee.child_by_field_name("object")
    member = _text(callee.child_by_field_name("property"))
    if member == "then" and target is not None and target.type == "call_expression":
        return True
    if target is None or target.type != "identifier" or member not in _CLIENT_METHODS:
        return False
    bound = _declared_inside(file, target, function)
    declarator = bound.parent if bound is not None else None
    if declarator is None or declarator.type != "variable_declarator":
        return False
    called = _bare_call(declarator.child_by_field_name("value"))
    return called is not None and _sdk_import(file, _text(called), repo_dir) == _CLIENT


def _is_state_setter(bound: Node, file, repo_dir) -> bool:
    pattern = bound.parent
    if pattern is None or pattern.type != "array_pattern":
        return False
    entries = [c for c in pattern.named_children if c.type != "comment"]
    if len(entries) != 2 or entries[1] != bound:
        return False
    declarator = pattern.parent
    if declarator is None or declarator.type != "variable_declarator":
        return False
    value = declarator.child_by_field_name("value")
    callee = value.child_by_field_name("function") if value is not None and value.type == "call_expression" else None
    return (
        callee is not None
        and callee.type == "identifier"
        and _package_import(file, _text(callee), frozenset({"react"}), repo_dir) == "useState"
    )


def _flag_read_left(function: Node, dropped: list[Node], reader: ts_scope.File, repo_dir: str) -> str | None:
    """Why something left in ``function`` (outside ``dropped``) may still
    read a flag, or None when nothing can.

    Every name left must be accounted for, not only what is called or
    rendered: a component handed on as a value (``createElement(Page)``,
    ``<Route Component={Page} />``) is rendered too. A name may be bound
    inside the function (its parameters, ``children`` among them, and its
    locals, whose values are judged here as well), name an HTML element, or
    be one of :func:`_flagless`'s imports."""
    own = function.child_by_field_name("name")
    for node in _walk(function):
        if any(_inside(node, d) for d in dropped):
            continue
        if node.type == "new_expression":
            return f"it still constructs {_text(node.child_by_field_name('constructor'))}"
        if node.type in _TAGS and node.child_by_field_name("name") is not None:
            if node.child_by_field_name("name").type != "identifier":
                return f"it still renders <{_text(node.child_by_field_name('name'))}>"
            continue
        if node.type != "identifier" or node == own:
            continue
        local = _text(node)
        parent = node.parent
        if parent is not None and parent.type in (*_TAGS, "jsx_closing_element") and parent.child_by_field_name("name") == node:
            if local[:1].islower() or _flagless(reader, local, repo_dir):
                continue
            return f"it still renders <{local}>"
        if _import_of(reader, local) is not None:
            if _flagless(reader, local, repo_dir):
                continue
            return f"it still uses {local}"
        bound = reader.binding_of(node)
        if bound is not None and _inside(bound, function):
            continue
        return f"it still uses {local}"
    return None


def _flagless(reader: ts_scope.File, local: str, repo_dir: str) -> bool:
    """A hook of React or React Router, or one of their components that
    renders nothing it is handed (:data:`_FLAGLESS_COMPONENTS`)."""
    imported = _package_import(reader, local, _FLAGLESS_PACKAGES, repo_dir)
    return imported is not None and (imported.startswith("use") or imported in _FLAGLESS_COMPONENTS)


# --- Steps 2 and 3: the component and its files ----------------------------


def _passes_children_through(function: Node, dropped: list[Node]) -> bool:
    """Whether ``function`` is ``({ children }) => <>{children}</>`` once
    ``dropped`` is gone."""
    parameters = function.child_by_field_name("parameters")
    named = [] if parameters is None else [c for c in parameters.named_children if c.type != "comment"]
    if len(named) != 1 or named[0].type != "required_parameter":
        return False
    pattern = named[0].child_by_field_name("pattern")
    if pattern is None or pattern.type != "object_pattern":
        return False
    entries = [c for c in pattern.named_children if c.type != "comment"]
    if len(entries) != 1 or entries[0].type != "shorthand_property_identifier_pattern" or _text(entries[0]) != "children":
        return False
    body = function.child_by_field_name("body")
    kept = [c for c in body.named_children if c.type != "comment" and c not in dropped]
    if len(kept) != 1 or kept[0].type != "return_statement":
        return False
    value = [c for c in kept[0].named_children if c.type != "comment"]
    if len(value) != 1:
        return False
    rendered = _unparenthesized(value[0])
    if rendered is None or rendered.type != "jsx_element":
        return False
    tag = rendered.child_by_field_name("open_tag")
    if tag is None or tag.child_by_field_name("name") is not None or tag.named_children:
        return False
    inside = [
        c
        for c in rendered.named_children
        if c.type not in ("jsx_opening_element", "jsx_closing_element")
        and not (c.type == "jsx_text" and not _text(c).strip())
    ]
    if len(inside) != 1 or inside[0].type != "jsx_expression":
        return False
    expression = [c for c in inside[0].named_children if c.type != "comment"]
    return len(expression) == 1 and expression[0].type == "identifier" and _text(expression[0]) == "children"


def _extra(guard: _Guard) -> str:
    """`` (it also runs `…`)`` naming the first statement besides the wait
    and the return, or nothing."""
    body = guard.function.child_by_field_name("body")
    for statement in body.named_children if body is not None else ():
        if statement.type in ("comment", "return_statement") or statement in guard.statements:
            continue
        text = " ".join(_text(statement).split())
        return f" (it also runs `{text[:80]}`)"
    return ""


def _loads_for_effect(original: Node) -> bool:
    """Whether the customer's file has a bare ``import './theme.css';``.

    It is there for its load, which deleting the file would stop, unless the
    module is an audited Featureflip SDK or declared
    ``side-effect-free-modules``. Asked of the customer's text: a bare import
    the run left (an import whose names the fold took) loaded nothing the
    customer asked for by itself."""
    for statement in original.named_children:
        if statement.type != "import_statement" or any(
            c.type in ("import_clause", "import_require_clause") for c in statement.named_children
        ):
            continue
        specifier = cross_file_ts._string_value(statement.child_by_field_name("source"))
        if specifier is None or not (specifier in SIDE_EFFECT_FREE_PACKAGES or inert_load.declared(specifier)):
            return True
    return False


def _declares_only(root: Node, function: Node) -> bool:
    """Whether the file holds nothing but imports, comments and ``function``."""
    for statement in root.named_children:
        if statement.type in ("import_statement", "comment"):
            continue
        if _inside(function, statement):
            if statement.type == "lexical_declaration" or (
                statement.type == "export_statement"
                and (statement.child_by_field_name("declaration") or statement).type
                in ("function_declaration", "lexical_declaration")
            ):
                continue
            if statement.type == "function_declaration":
                continue
        return False
    return True


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _source_of(node: Node) -> Node | None:
    statement = node
    while statement is not None and statement.type != "import_statement":
        statement = statement.parent
    return statement


@dataclass
class _Use:
    """A file that renders the component: what to mark in it."""

    file: _File
    #: ``(start, end, form)`` spans for :func:`ctor_param_model.mark`.
    marks: list[tuple[int, int, str]]
    elements: int


def _own_test(file: _File, component: str, declaring: str) -> bool:
    """Whether ``file`` is the component's own test file, deleted with it."""
    path = Path(file.path)
    stem = path.name
    for extension in sorted(_TS_EXTENSIONS, key=len, reverse=True):
        if stem.endswith(extension):
            stem = stem[: -len(extension)]
            break
    match = _TEST_SUFFIX.search(stem)
    if match is None or stem[: match.start()] != Path(declaring).name.split(".")[0]:
        return False
    if not ts_tests.is_test_file(file.current, ts_scope.grammar(file.path)):
        return False
    # Every top-level statement is a declaration, an import, test setup, or
    # the one group named for the component: a test of anything else would be
    # deleted with it.
    groups = 0
    for statement in file.root.named_children:
        if statement.type in (
            "import_statement", "comment", "lexical_declaration", "function_declaration",
            "type_alias_declaration", "interface_declaration",
        ):
            continue
        call = statement.named_children[0] if statement.type == "expression_statement" and statement.named_children else None
        if call is None or call.type != "call_expression":
            return False
        callee = call.child_by_field_name("function")
        head = callee
        while head is not None and head.type in ("member_expression", "call_expression"):
            head = head.child_by_field_name("object" if head.type == "member_expression" else "function")
        if head is None or head.type != "identifier":
            return False
        if _text(head) in _TEST_SETUP:
            continue
        arguments = call.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
        title = "".join(_text(c) for c in first.named_children) if first is not None and first.type == "string" else None
        if callee.type != "identifier" or _text(callee) != "describe" or title != component:
            return False
        groups += 1
    return groups > 0


def _use(file: _File, component: str, stem: str) -> _Use | str:
    """What to mark in ``file``, or why it cannot be rewritten.

    A string or comment naming the component or its file (``stem``) may only
    be the source of the one import this deletes: ``vi.mock('../gates')``
    would outlive the file."""
    word = _word(component)
    file_word = _word(stem)
    named: list[Node] = []
    marks: list[tuple[int, int, str]] = []
    elements = 0
    imports: list[Node] = []
    for node in _walk(file.root):
        if node.type in _ELEMENTS:
            tag = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
            name = tag.child_by_field_name("name") if tag is not None else None
            if name is None or _text(name) != component:
                continue
            if node.type != "jsx_element" or tag.named_children[1:]:
                return f"{Path(file.path).name} renders <{component}> with attributes or no children"
            children = [
                c
                for c in node.named_children
                if c.type not in ("jsx_opening_element", "jsx_closing_element")
                and not (c.type == "jsx_text" and not _text(c).strip())
            ]
            if len(children) != 1 or children[0].type not in _ELEMENTS:
                return f"{Path(file.path).name} renders <{component}> around something other than one element"
            close = node.child_by_field_name("close_tag")
            close_name = close.child_by_field_name("name") if close is not None else None
            if close_name is None:
                return f"{Path(file.path).name} renders <{component}> in a shape this tool cannot unwrap"
            marks += [(name.start_byte, name.end_byte, "argument"), (close_name.start_byte, close_name.end_byte, "argument")]
            elements += 1
        elif node.type == "import_specifier" and _text(node.child_by_field_name("name")) == component:
            imports.append(node)
        elif node.type in ("string_fragment", "template_string", "comment") and (
            word.search(_text(node)) or file_word.search(_text(node))
        ):
            if node.type == "comment":
                return f"{Path(file.path).name} names {component} in a comment"
            named.append(node)
    if len(imports) != 1:
        return f"{Path(file.path).name} imports {component} more than once"
    statement = _source_of(imports[0])
    if any(not _inside(n, statement) for n in named):
        return f"{Path(file.path).name} names {component} or its file outside its import"
    clause = next((c for c in statement.named_children if c.type == "import_clause"), None)
    named = clause.named_children if clause is not None else []
    if (
        statement.parent is None
        or statement.parent.type != "program"
        or len(named) != 1
        or named[0].type != "named_imports"
        or len([s for s in named[0].named_children if s.type == "import_specifier"]) != 1
        or any(c.type == "type" for c in statement.children)
    ):
        return f"{Path(file.path).name} imports more than {component} from its file"
    marks.append((statement.start_byte, statement.end_byte, "statement"))
    return _Use(file, marks, elements)


def _remove_gate(guard: _Guard, summaries, language, repo_dir, rewrite, engine, in_run):
    """Steps 2 and 3, or ``(summaries, None)`` when they cannot be taken
    (with the reason on ``guard.kept``)."""
    name = guard.name
    if not _GATE_NAME.fullmatch(name):
        return summaries, None
    if not _passes_children_through(guard.function, guard.statements):
        guard.kept.append(f"{name} does more than wait and render its children{_extra(guard)}")
        return summaries, None
    if not _declares_only(guard.root, guard.function):
        guard.kept.append(f"its file declares more than {name}")
        return summaries, None
    if _loads_for_effect(_parse(guard.summary.original_content, ts_scope.grammar(guard.path))):
        guard.kept.append("its file loads a module for its effects")
        return summaries, None
    declared = _exported_function(guard.root, guard.data, name)
    if declared is None or declared[0].start_byte != guard.function.start_byte:
        guard.kept.append(f"{name} is not exported in a way this tool can follow")
        return summaries, None
    files = {s.path: (s.original_content, s.content) for s in summaries if not file_deletion.deleted(s)}
    by_resolved = {_key(p): p for p in files}
    decl = _File(guard.path, guard.summary.original_content, guard.summary.content, guard.root, guard.data)
    users = _users(
        name, decl, declared[1], files, by_resolved, repo_dir, in_run,
        exempt=lambda f: _own_test(f, name, guard.path),
        solution=True,
    )
    if users is None:
        guard.kept.append(f"a file uses {name} in a way this tool cannot follow")
        return summaries, None
    deleted = [guard.path]
    uses: list[_Use] = []
    for user in users[1:]:
        if _own_test(user, name, guard.path):
            deleted.append(user.path)
            continue
        # The proof read a file outside the run as text, with its line
        # endings translated; the marks are byte offsets into what is on disk.
        raw = Path(user.path).read_bytes().decode("utf-8")
        root = _parse(raw, ts_scope.grammar(user.path))
        if root.has_error:
            guard.kept.append(f"{Path(user.path).name} does not parse")
            return summaries, None
        user = _File(user.path, user.original, raw, root, raw.encode("utf-8"))
        use = _use(user, name, Path(guard.path).name.split(".")[0])
        if isinstance(use, str):
            guard.kept.append(use)
            return summaries, None
        uses.append(use)
    driving = _wait_tested(guard, deleted, repo_dir)
    if driving is not None:
        guard.kept.append(driving)
        return summaries, None
    if not _rest_of_repo_ignores(guard.path, name, {_key(p) for p in deleted} | {_key(u.file.path) for u in uses}, repo_dir):
        guard.kept.append(f"another file names {name}'s file")
        return summaries, None
    if uses:
        outcome = _apply(
            {u.file.path: u.marks for u in uses},
            summaries,
            language,
            repo_dir,
            rewrite,
            engine,
            check=lambda path, text: not _word(name).search(text),
        )
        if outcome is None:
            guard.kept.append(f"unwrapping <{name}> did not come out clean")
            return summaries, None
        summaries = outcome
    summaries = file_deletion.delete(summaries, deleted, rewrite)
    rel = os.path.relpath(guard.path, repo_dir)
    rows = [(rel, REMOVED_GATE, name)]
    rows += [(os.path.relpath(p, repo_dir), REMOVED_GATE, f"{name}'s test file") for p in deleted[1:]]
    rows += [
        (os.path.relpath(u.file.path, repo_dir), REMOVED_GATE, f"<{name}> replaced by its child ({u.elements})")
        for u in uses
    ]
    return summaries, rows


def _rest_of_repo_ignores(declaring: str, name: str, accounted: set[str], repo_dir: str) -> bool:
    """Whether no file outside ``accounted`` spells the declaring file's
    name, which would be a way to load it this pass cannot see."""
    stem = Path(declaring).name.split(".")[0]
    word = _word(stem)
    for listed in source_index.files(repo_dir, with_ignored=True):
        if listed.resolved in accounted:
            continue
        try:
            text = listed.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if word.search(text):
            return False
    return True


# --- Step 1 on its own --------------------------------------------------------


def _tested_by(name: str, path: str, repo_dir: str) -> str | None:
    """A test file other than ``path`` that names ``name``, if any."""
    word = _word(name)
    for listed in source_index.files(repo_dir, with_ignored=True):
        if not str(listed.path).endswith(_TS_EXTENSIONS) or listed.resolved == _key(path):
            continue
        try:
            text = listed.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if word.search(text) and ts_tests.is_test_file(text, ts_scope.grammar(str(listed.path))):
            return listed.path.name
    return None


def _wait_tested(guard: _Guard, deleted: list[str], repo_dir: str) -> str | None:
    """Why a test file outside ``deleted`` may check the wait, or None.

    One that spells the readiness hook (or mocks it, which spells it too)
    and reaches the guard's file through its imports renders the wait and
    can tell it from no wait. A test of the hook alone reaches no gate."""
    pattern = re.compile("|".join(rf"(?<![\w$]){re.escape(n)}(?![\w$])" for n in sorted(guard.signals)))
    skip = {_key(p) for p in deleted}
    target = _key(guard.path)
    files = ts_scope.Files(repo_dir)
    for listed in source_index.files(repo_dir, with_ignored=True):
        if not str(listed.path).endswith(_TS_EXTENSIONS) or listed.resolved in skip or listed.resolved == target:
            continue
        try:
            text = listed.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not pattern.search(text) or not ts_tests.is_test_file(text, ts_scope.grammar(str(listed.path))):
            continue
        if _reaches(str(listed.path), target, files, repo_dir):
            return f"the test file {listed.path.name} drives the wait through its imports"
    return None


def _reaches(start: str, target: str, files: ts_scope.Files, repo_dir: str, limit: int = 2000) -> bool:
    """Whether ``start`` imports or re-exports ``target``, directly or through
    the repository's own modules. Past ``limit`` files, assumed so."""
    seen = {_key(start)}
    queue = [start]
    while queue:
        path = queue.pop()
        file = files.file(path)
        if file is None:
            continue
        for node in _walk(file.root):
            if node.type not in ("import_statement", "export_statement", "call_expression"):
                continue
            if node.type == "call_expression":
                callee = node.child_by_field_name("function")
                arguments = node.child_by_field_name("arguments")
                if callee is None or callee.type not in ("import", "identifier") or arguments is None:
                    continue
                if callee.type == "identifier" and _text(callee) != "require":
                    continue
                source = next((c for c in arguments.named_children if c.type == "string"), None)
            else:
                source = node.child_by_field_name("source")
            specifier = cross_file_ts._string_value(source) if source is not None else None
            if specifier is None:
                continue
            found = _resolve(path, specifier, repo_dir)
            if found is None:
                continue
            key = _key(found)
            if key == target:
                return True
            if key not in seen:
                seen.add(key)
                if len(seen) > limit:
                    return True
                queue.append(found)
    return False


def _remove_guard(guard: _Guard, summaries, language, repo_dir, rewrite, engine):
    rel = os.path.relpath(guard.path, repo_dir)
    test = _tested_by(guard.name, guard.path, repo_dir)
    reasons = list(guard.kept)
    if test is not None:
        reasons.append(f"the test file {test} renders it, and may test the wait itself")
    else:
        driving = _wait_tested(guard, [], repo_dir)
        if driving is not None and driving not in reasons:
            reasons.append(driving)
    if test is not None or any("drives the wait" in r for r in reasons):
        return summaries, [(rel, KEPT_GUARD, f"{guard.name}: {'; '.join(reasons)}")]
    marks = [(s.start_byte, s.end_byte, "statement") for s in guard.statements]
    outcome = _apply({guard.path: marks}, summaries, language, repo_dir, rewrite, engine)
    if outcome is None:
        return summaries, [(rel, KEPT_GUARD, f"{guard.name}: deleting it did not come out clean")]
    text = guard.name
    if guard.kept:
        text += f"; the component stays: {'; '.join(guard.kept)}"
    return outcome, [(rel, REMOVED_GUARD, text)]


# --- Marking and the engine -----------------------------------------------------


def _apply(marks_by_path, summaries, language, repo_dir, rewrite, engine, check=None):
    """``summaries`` with every marked span deleted or unwrapped by the
    engine, or None (every file put back) when the result is not clean."""
    paths = sorted(marks_by_path)
    before = {p: Path(p).read_bytes() for p in paths}
    sentinel = choose_sentinel(before.values())
    if sentinel is None:
        return None
    git_ops.before_writing(paths)
    try:
        for path in paths:
            Path(path).write_bytes(mark(before[path], marks_by_path[path], sentinel))
        engine(paths, render_rules("ts", [], sentinel) + render_rules("tsx", [], sentinel))
        after = {p: Path(p).read_bytes() for p in paths}
        clean = all(
            sentinel.encode() not in after[p]
            and not _parse(after[p].decode("utf-8"), ts_scope.grammar(p)).has_error
            and (check is None or check(p, after[p].decode("utf-8")))
            for p in paths
        )
    except Exception as exc:  # noqa: BLE001 - any surprise means: put the bytes back
        for path, data in before.items():
            Path(path).write_bytes(data)
        if type(exc).__name__ == "PiranhaTransformError":
            raise
        logger.warning("readiness guard edit failed: %s", exc)
        return None
    if not clean:
        for path, data in before.items():
            Path(path).write_bytes(data)
        return None
    result = list(summaries)
    index = {_key(s.path): i for i, s in enumerate(result)}
    for path in paths:
        text = after[path].decode("utf-8")
        at = index.get(_key(path))
        if at is None:
            result.append(rewrite(path, before[path].decode("utf-8"), text))
        else:
            result[at] = rewrite(result[at].path, result[at].original_content, text)
    return result
