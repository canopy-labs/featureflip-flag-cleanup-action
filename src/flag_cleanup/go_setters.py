"""Go test setters: ``featureflag.Set(orgID, Flag, true)``.

An in-house flag catalog usually lets a test pin a flag with a function of its
own instead of mocking the SDK read. Once the flag is removed, a setter that
pins the value that STAYS is dead setup and a setter that pins the value that
goes describes a branch the code no longer has: the same two cases as a mocked
read (:mod:`flag_cleanup.mock_stubs`), and decided the same way. This module
only says WHICH calls are setters; ``mock_stubs`` decides what goes.

The customer names the setter (the ``test-setters`` input), and a name alone
is never enough. A call is recognised only when

* it is a statement of its own (not ``go``, ``defer``, a value or an argument);
* the callee is ``Name(...)`` or ``pkg.Name(...)`` where ``Name`` is a
  package-level FUNCTION of this repository: the same package for the bare
  form, for the qualified one a package that ``pkg`` is bound to by an import
  :func:`flag_cleanup.go_module.resolve` proves (the same module, or another
  module of the checkout every build reads from here), with exactly one such
  declaration. A method call
  (``store.Set(...)``) cannot be resolved without types and is not one;
* it has exactly as many arguments as the function has parameters (no
  variadic), exactly one of them spells the flag's key, and exactly one is the
  literal ``true`` or ``false``, in a parameter declared ``bool``;
* every other argument is a name, a member read or a literal, so deleting the
  statement drops no call and no receive.

Anything else that names a setter beside the key stays where it is, and its
test is left alone (:func:`mock_stubs.resolve` marks it unprovable). The
constant it names is then kept by the cross-file resolution: see
:func:`flag_cleanup.cross_file_go.references`.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import go_module, go_scope, source_index

_DECLARED: ContextVar[frozenset[str]] = ContextVar("go_test_setters", default=frozenset())
_SUSPENDED: ContextVar[bool] = ContextVar("go_test_setters_suspended", default=False)


@contextlib.contextmanager
def declaring(names: Iterable[str]) -> Iterator[None]:
    """Treat calls to any of ``names`` as candidate test setters."""
    token = _DECLARED.set(frozenset(names))
    try:
        yield
    finally:
        _DECLARED.reset(token)


_TREATMENT: ContextVar[bool | None] = ContextVar("go_test_setters_treatment", default=None)


_INERT: ContextVar[list[tuple[str, str]] | None] = ContextVar("go_test_setters_inert", default=None)


@contextlib.contextmanager
def treating(treatment: bool) -> Iterator[None]:
    """The value the run keeps, which decides a setter OUTSIDE a test: there
    only the kept value can be deleted (:func:`treatment`)."""
    token = _TREATMENT.set(treatment)
    found = _INERT.set([])
    try:
        yield
    finally:
        _INERT.reset(found)
        _TREATMENT.reset(token)


def note_inert(path: str, statement: Node) -> None:
    """Record a proven setter of the REMOVED value that stays outside a test:
    the read it steered is folded, so it has no effect any more."""
    found = _INERT.get()
    row = (path, " ".join(_text(statement).split()))
    if found is not None and row not in found:
        found.append(row)


def inert() -> list[tuple[str, str]]:
    """``(path, statement)`` of each :func:`note_inert` row of this run."""
    return list(_INERT.get() or ())


def treatment() -> bool | None:
    return _TREATMENT.get()


@contextlib.contextmanager
def suspended() -> Iterator[None]:
    """Recognise no setter: a check of what a run left behind must count a
    surviving setter as the mention it is."""
    token = _SUSPENDED.set(True)
    try:
        yield
    finally:
        _SUSPENDED.reset(token)


def declared() -> frozenset[str]:
    return frozenset() if _SUSPENDED.get() else _DECLARED.get()


def call_pattern(names: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted(names, key=len, reverse=True)
    return re.compile(r"(?<![\w$])(?:" + "|".join(re.escape(n) for n in ordered) + r")\s*\(")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _compact(node: Node) -> str:
    return "".join(_text(node).split())


@dataclass(frozen=True)
class Site:
    statement: Node
    #: The argument spelling the key.
    key: Node
    value: bool


def _parameter_types(function: Node) -> list[str] | None:
    """The type text of each parameter, one per name; ``None`` for a variadic
    one or a parameter list this cannot read."""
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return None
    found: list[str] = []
    for parameter in parameters.named_children:
        if parameter.type != "parameter_declaration":
            return None  # `...T` is a variadic_parameter_declaration
        kind = parameter.child_by_field_name("type")
        if kind is None:
            return None
        names = [c for c in parameter.children_by_field_name("name")]
        found.extend([_compact(kind)] * max(1, len(names)))
    return found


def _functions(files: list[go_scope.File], name: str) -> list[Node]:
    return [
        declaration
        for file in files
        for declaration in file.root.named_children
        if declaration.type == "function_declaration"
        and (n := declaration.child_by_field_name("name")) is not None
        and _text(n) == name
    ]


def _plain_argument(node: Node) -> bool:
    if node.type in ("identifier", "true", "false", "nil", "interpreted_string_literal", "raw_string_literal", "int_literal"):
        return True
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        return operand is not None and _plain_argument(operand)
    return False


def _in_deferred_closure(statement: Node) -> bool:
    """Whether a function literal other than a subtest body (`t.Run("n",
    func(t *testing.T) {…})`) sits between the statement and its function: a
    `t.Cleanup`, `defer func(){…}()` or `go` closure restores or runs later,
    so what it sets says nothing of the value the test runs with."""
    node = statement.parent
    while node is not None and node.type not in ("function_declaration", "method_declaration"):
        if node.type == "func_literal":
            arguments = node.parent
            call = arguments.parent if arguments is not None and arguments.type == "argument_list" else None
            function = call.child_by_field_name("function") if call is not None else None
            field = function.child_by_field_name("field") if function is not None and function.type == "selector_expression" else None
            if field is None or _text(field) != "Run":
                return True
        node = node.parent
    return False


_LD_TESTDATA = re.compile(r"^github\.com/launchdarkly/[\w./-]*/ldtestdata$")


def _imports_ldtestdata(file: go_scope.File, name: str) -> bool:
    """Whether ``file`` binds ``name`` to LaunchDarkly's ``ldtestdata``."""
    for spec in go_scope.import_specs(file.root):
        path = spec.child_by_field_name("path")
        alias = spec.child_by_field_name("name")
        if path is None:
            continue
        written = _text(path).strip('"`')
        if _LD_TESTDATA.match(written) and (_text(alias) if alias is not None else "ldtestdata") == name:
            return True
    return False


def _is_data_source(value: Node | None, file: go_scope.File) -> bool:
    """``ldtestdata.DataSource()`` where ``ldtestdata`` is that import."""
    if value is None or value.type != "call_expression" or value.child_by_field_name("arguments") is None:
        return False
    if value.child_by_field_name("arguments").named_children:
        return False
    function = value.child_by_field_name("function")
    if function is None or function.type != "selector_expression":
        return False
    operand, field = function.child_by_field_name("operand"), function.child_by_field_name("field")
    return (
        operand is not None
        and field is not None
        and operand.type == "identifier"
        and _text(field) == "DataSource"
        and not go_scope.local_binders(operand, _text(operand))
        and _imports_ldtestdata(file, _text(operand))
    )


def _test_data_source(receiver: Node, file: go_scope.File, packages: go_scope.Packages, here: Path) -> bool:
    """Whether ``receiver`` is a bare name proven to hold a
    ``ldtestdata.DataSource()``: a local bound once, or a package-level
    ``var`` declared once in this package. ``Update`` and ``Flag`` of that
    type only write to an in-memory source."""
    if receiver.type != "identifier" or file.clause is None:
        return False
    name = _text(receiver)
    binding = go_scope.local_binding(receiver, name)
    if binding is not None:
        return not binding.opaque and _is_data_source(binding.value, file)
    site = packages.package_var_binding(here.parent, file.clause, name)
    return site is not None and _is_data_source(site[1].value, site[0])


def _test_data_site(
    node: Node, spellings: frozenset[str], file: go_scope.File, packages: go_scope.Packages, here: Path
) -> Site | None:
    """``td.Update(td.Flag(<key>).VariationForAll(<bool>))`` on a proven
    LaunchDarkly test data source, where ``<key>`` is a spelling of the flag's
    key or ``<handle>.Key()`` of a spelling of its handle."""
    call = node.named_children[0]
    function, arguments = call.child_by_field_name("function"), call.child_by_field_name("arguments")
    if function is None or arguments is None or function.type != "selector_expression" or len(arguments.named_children) != 1:
        return None
    receiver, field = function.child_by_field_name("operand"), function.child_by_field_name("field")
    if receiver is None or field is None or _text(field) != "Update":
        return None
    builder = arguments.named_children[0]
    shape = _chain(builder, "VariationForAll")
    if shape is None or shape[1].type not in ("true", "false"):
        return None
    flag_call = _chain(shape[0], "Flag")
    if flag_call is None or _compact(flag_call[0]) != _compact(receiver):
        return None
    if not _test_data_source(receiver, file, packages, here):
        return None
    key = flag_call[1]
    if _compact(key) in spellings:
        return Site(node, key, shape[1].type == "true")
    if key.type == "call_expression" and not key.child_by_field_name("arguments").named_children:
        accessor = key.child_by_field_name("function")
        if accessor is not None and accessor.type == "selector_expression":
            handle, method = accessor.child_by_field_name("operand"), accessor.child_by_field_name("field")
            if handle is not None and method is not None and _text(method) == "Key" and _compact(handle) in spellings:
                return Site(node, handle, shape[1].type == "true")
    return None


def _chain(node: Node, method: str) -> tuple[Node, Node] | None:
    """``(operand, only argument)`` of ``<operand>.<method>(<argument>)``."""
    if node.type != "call_expression":
        return None
    function, arguments = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    if function is None or arguments is None or function.type != "selector_expression" or len(arguments.named_children) != 1:
        return None
    operand, field = function.child_by_field_name("operand"), function.child_by_field_name("field")
    if operand is None or field is None or _text(field) != method:
        return None
    return operand, arguments.named_children[0]


def sites(
    path: str,
    root: Node,
    spellings: frozenset[str],
    repo_dir: str,
    names: frozenset[str] | None = None,
) -> list[Site]:
    """The statements of ``root`` that are proven setters of a key spelled as
    one of ``spellings`` (compact texts)."""
    names = declared() if names is None else names
    if not names or not spellings:
        return []
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    packages = go_scope.Packages(stop)
    here = Path(path).resolve()
    file = packages.file(here)
    if file is None or file.clause is None:
        return []
    found: list[Site] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "expression_statement" or len(node.named_children) != 1:
            continue
        if _in_deferred_closure(node):
            continue
        call = node.named_children[0]
        if call.type != "call_expression" or call.child_by_field_name("type_arguments") is not None:
            continue
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        if "Update" in names and function.type == "selector_expression":
            found_site = _test_data_site(node, spellings, file, packages, here)
            if found_site is not None:
                found.append(found_site)
                continue
        if function.type == "identifier":
            callee, qualifier = _text(function), None
        elif function.type == "selector_expression":
            operand, field = function.child_by_field_name("operand"), function.child_by_field_name("field")
            if operand is None or field is None or operand.type != "identifier":
                continue
            callee, qualifier = _text(field), _text(operand)
        else:
            continue
        if callee not in names:
            continue
        if qualifier is None:
            candidates = _functions(packages.package(here.parent, file.clause), callee)
        else:
            if go_scope.local_binders(function, qualifier):
                continue
            imported = packages.imported(file, qualifier)
            if imported is None or not callee[:1].isupper():
                continue
            candidates = _functions(packages.package(imported.directory, imported.clause), callee)
        if len(candidates) != 1:
            continue
        types = _parameter_types(candidates[0])
        args = arguments.named_children
        if types is None or len(types) != len(args) or any(a.type == "variadic_argument" for a in args):
            continue
        keys = [i for i, a in enumerate(args) if _compact(a) in spellings]
        values = [i for i, a in enumerate(args) if a.type in ("true", "false")]
        if len(keys) != 1 or len(values) != 1 or types[values[0]] != "bool":
            continue
        if any(not _plain_argument(a) for i, a in enumerate(args) if i != keys[0] and i != values[0]):
            continue
        if not _plain_argument(args[keys[0]]) and args[keys[0]].type != "selector_expression":
            continue
        found.append(Site(node, args[keys[0]], args[values[0]].type == "true"))
    return found


def _descendants(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.named_children)


def remaining(root: Node, names: frozenset[str], spellings: frozenset[str]) -> list[Node]:
    """Every call to one of ``names`` that has an argument spelling the key,
    proven or not."""
    out: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        if function.type == "selector_expression":
            function = function.child_by_field_name("field")
        if function is None or _text(function) not in names:
            continue
        # An argument that holds the spelling anywhere counts: a test-data
        # setter nests it (`td.Update(td.Flag(H.Key()).VariationForAll(true))`).
        if any(
            _compact(inner) in spellings
            for a in arguments.named_children
            for inner in _descendants(a)
        ):
            out.append(node)
    return out
