"""Collapse a Python function the fold left returning one literal on every path.

When a removal makes every ``return`` of a function the same boolean, the
function keeps the statements that only fed the result::

    def guest_access_enabled() -> bool:
        presentation = os.getenv("NEXT_PUBLIC_GUEST")
        if presentation is None:
            return False
        return False

A reviewer has to reduce that to ``return False`` by hand. This pass does it,
but only when it can PROVE that every statement it deletes does nothing else.
That is a whitelist, not a blacklist: a statement is deletable only if it is
one of the shapes below, and anything else keeps the function exactly as the
fold left it.

* an assignment (or annotated assignment) of a plain name from a pure
  expression;
* an ``if`` whose test is pure and whose branches hold only ``return
  <literal>``, ``pass`` or another such ``if``;
* the ``return <bool literal>`` statements themselves, and a docstring (kept).

A pure expression is built from literals; names that are parameters, locals
assigned earlier in the function, or bound exactly once at module level to a
literal; ``os.getenv`` / ``os.environ.get`` with a literal key (``os`` being
the module, imported once and never rebound); a small set of ``str`` methods
on a value known to be a ``str``; ``not``/``and``/``or``; ``is``, ``is not``,
``==``, ``!=``, ``in``, ``not in`` between builtin values; and ``|`` between
two literal sets.

"Builtin value" is tracked, not assumed. A parameter has an unknown type, so
the only thing allowed to touch it is ``is`` / ``is not``, which cannot run
user code. ``x.strip()`` needs ``x`` known to be a ``str`` (not possibly
``None``): the check follows ``if x is None: return`` narrowing, and
``x is None or x.strip()`` short-circuiting. Any call that is not on the list,
any attribute, subscript, comprehension, lambda, ``await``, ``yield``,
f-string, loop, ``try`` or ``with`` keeps the function as it is.

Scoped to the diff: a function is collapsed only if the transform changed it
and it did not already return one literal before. The whole function must end
in a ``return`` on every path.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

#: The ``bindings`` kind of the pull request callout (see ``pr_content``).
COLLAPSED_FUNCTION = "collapsed function"

_SCALARS = frozenset({"str", "opt_str", "bool", "int", "float", "none"})
_HASHABLE = _SCALARS
_CONTAINERS = frozenset({"set", "fset", "seq", "dict"})
_BUILTIN = _SCALARS | _CONTAINERS | {"mixed"}
_UNKNOWN = "unknown"

#: ``str`` methods taking an optional string of characters.
_STR_STRIPS = frozenset({"strip", "lstrip", "rstrip"})
#: ``str`` methods taking no argument at all.
_STR_TRANSFORMS = frozenset({"lower", "upper", "casefold", "title", "capitalize", "swapcase"})
_STR_PREDICATES = frozenset({"startswith", "endswith"})
_STR_FLAGS = frozenset(
    {"isdigit", "isalpha", "isalnum", "isnumeric", "isspace", "isupper", "islower", "isdecimal"}
)
_DECORATORS = frozenset({"staticmethod", "classmethod", "property"})


class _Refuse(Exception):
    """The function holds something this pass cannot prove inert."""


def collapse(before: str, after: str) -> str:
    """``after`` with each function the transform made constant reduced to its return."""
    return collapse_reporting(before, after)[0]


def collapse_reporting(before: str, after: str) -> tuple[str, list[tuple[str, int, bool]]]:
    """:func:`collapse`, and ``(function, statements removed, literal)`` for each one reduced."""
    if before == after:
        return after, []
    try:
        old = ast.parse(before)
        tree = ast.parse(after)
        comments = _comment_rows(after)
    except (SyntaxError, ValueError, RecursionError, tokenize.TokenError):
        return after, []
    # A name defined twice (a property's getter and setter, a conditional or
    # plain redefinition) cannot be matched to its old self: leave both.
    old_all = list(_functions(old))
    new_all = list(_functions(tree))
    old_names = [n for n, _ in old_all]
    new_names = [n for n, _ in new_all]
    repeated = {n for n in old_names if old_names.count(n) > 1} | {
        n for n in new_names if new_names.count(n) > 1
    }
    old_functions = dict(old_all)
    module = _Module(tree)
    edits: list[tuple[int, int, str]] = []
    report: list[tuple[str, int, bool]] = []
    for qualname, fn in new_all:
        if qualname in repeated:
            continue
        previous = old_functions.get(qualname)
        if previous is None or ast.dump(previous) == ast.dump(fn):
            continue
        if _constant(previous) is not None:
            continue  # already constant before the run: the customer's
        planned = _plan(fn, module, after, comments)
        if planned is not None:
            first, last, text, removed, literal = planned
            edits.append((first, last, text))
            report.append((qualname.replace(".<locals>.", "."), removed, literal))
    if not edits:
        return after, []
    lines = io.StringIO(after, newline="").readlines()
    for first, last, text in sorted(edits, reverse=True):
        # The last replaced line's own ending, so a file that ends without a
        # newline still does.
        newline = _newline(lines[last - 1])
        lines[first - 1 : last] = [text + newline]
    result = "".join(lines)
    try:
        ast.parse(result)
    except (SyntaxError, ValueError, RecursionError):  # pragma: no cover - defence
        return after, []
    return result, report


def _comment_rows(source: str) -> set[int]:
    """The (1-based) lines holding a comment."""
    rows = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            rows.add(token.start[0])
    return rows


def _newline(line: str) -> str:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return ending
    return ""


def _functions(tree: ast.AST):
    """``(qualified name, node)`` for every function, by nesting."""

    def walk(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                yield name, child
                yield from walk(child, name + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
            else:
                yield from walk(child, prefix)

    return walk(tree, "")


def _constant(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool | None:
    """The one boolean every ``return`` of ``fn`` gives, ``None`` if it varies."""
    values = set()
    for node in _own_nodes(fn):
        if isinstance(node, ast.Return):
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool)):
                return None
            values.add(node.value.value)
    return values.pop() if len(values) == 1 else None


def _own_nodes(fn: ast.AST):
    """Nodes of ``fn``'s own body, not those of a nested function or class."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


class _Module:
    """What the module binds at its top level, for names a function may read."""

    def __init__(self, tree: ast.Module) -> None:
        counts: dict[str, int] = {}
        self.star = False
        self.globals: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                counts[node.id] = counts.get(node.id, 0) + 1
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "*":
                        self.star = True
                    bound = alias.asname or alias.name.split(".")[0]
                    counts[bound] = counts.get(bound, 0) + 1
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                counts[node.name] = counts.get(node.name, 0) + 1
            elif isinstance(node, ast.arg):
                counts[node.arg] = counts.get(node.arg, 0) + 1
            elif isinstance(node, ast.ExceptHandler) and node.name:
                counts[node.name] = counts.get(node.name, 0) + 1
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                self.globals.update(node.names)
        self._counts = counts
        self._parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        self._tree = tree
        self.types: dict[str, str] = {}
        self.os_name: str | None = None
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os" and alias.asname is None and self._once("os"):
                        self.os_name = "os"
                continue
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name) or not self._once(target.id):
                continue
            kind = self._constant_type(value)
            if kind is None:
                continue
            mutable = isinstance(value, (ast.Set, ast.List, ast.Dict)) or kind == "set"
            if mutable and not (target.id.startswith("_") and self._plain_reads(target.id, node)):
                continue
            self.types[target.id] = kind

    def _once(self, name: str) -> bool:
        return not self.star and name not in self.globals and self._counts.get(name, 0) == 1

    def _constant_type(self, node: ast.expr) -> str | None:
        """The type of a module-level value this pass may trust, ``None`` otherwise.

        Immutable literals always. A set, list or dict may hold an object with
        a custom ``__eq__`` once something adds one, so it is trusted only when
        it is private to the module and the module reads it in nothing but
        membership tests and ``|`` (the callers in other files cannot reach a
        ``_name`` through anything this pass would call a plain read).
        """
        scalar = _scalar(node)
        if scalar is not None:
            return scalar
        if isinstance(node, ast.Tuple) and all(_scalar(e) is not None for e in node.elts):
            return "seq"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "frozenset"
            and self._counts.get("frozenset", 0) == 0
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], (ast.Set, ast.Tuple, ast.List))
            and all(_scalar(e) is not None for e in node.args[0].elts)
        ):
            return "fset"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left = self._operand_type(node.left)
            right = self._operand_type(node.right)
            if left == right == "fset":
                return "fset"
            if left in ("set", "fset") and right in ("set", "fset"):
                return "set"
            return None
        mutable = _literal_type(node)
        if mutable in _CONTAINERS:
            return mutable
        return None

    def _operand_type(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.types.get(node.id)
        return self._constant_type(node)

    def _plain_reads(self, name: str, definition: ast.stmt) -> bool:
        """Whether, outside ``definition``, ``name`` is only the right side of
        ``in`` / ``not in`` or an operand of ``|``: nothing that can add to it."""
        inside = {id(n) for n in ast.walk(definition)}
        for node in ast.walk(self._tree):
            if not (isinstance(node, ast.Name) and node.id == name) or id(node) in inside:
                continue
            parent = self._parents.get(node)
            if not isinstance(node.ctx, ast.Load):
                return False
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.BitOr):
                continue
            if isinstance(parent, ast.Compare) and any(node is c for c in parent.comparators):
                index = next(i for i, c in enumerate(parent.comparators) if c is node)
                if isinstance(parent.ops[index], (ast.In, ast.NotIn)):
                    continue
            return False
        return True


def _scalar(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None:
            return "none"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, str):
            return "str"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
    return None


def _literal_type(node: ast.expr) -> str | None:
    """The container type of a literal of constants, the scalar type of a constant."""
    scalar = _scalar(node)
    if scalar is not None:
        return scalar
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        if all(_scalar(e) is not None for e in node.elts):
            return "set" if isinstance(node, ast.Set) else "seq"
        return None
    if isinstance(node, ast.Dict):
        if all(k is not None and _scalar(k) is not None and _scalar(v) is not None for k, v in zip(node.keys, node.values)):
            return "dict"
    return None


class _Function:
    def __init__(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, module: _Module) -> None:
        self.module = module
        self.env: dict[str, str] = {}
        args = fn.args
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None:
                self.env[a.arg] = _UNKNOWN
        self.params = set(self.env)
        self.assigned = {
            n.id
            for n in _own_nodes(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
        }

    # -- expressions -----------------------------------------------------

    def expr(self, node: ast.expr, env: dict[str, str]) -> str:
        scalar = _scalar(node)
        if scalar is not None:
            return scalar
        if isinstance(node, (ast.Set, ast.Tuple, ast.List, ast.Dict)):
            kind = _literal_type(node)
            if kind is None:
                raise _Refuse
            return kind
        if isinstance(node, ast.Name):
            return self.name(node.id, env)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self.builtin(self.expr(node.operand, env))
            return "bool"
        if isinstance(node, ast.BoolOp):
            return self.boolop(node, env)
        if isinstance(node, ast.Compare):
            return self.compare(node, env)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left, right = self.expr(node.left, env), self.expr(node.right, env)
            if left == right and left in ("set", "fset"):
                return left
            raise _Refuse
        if isinstance(node, ast.Call):
            return self.call(node, env)
        raise _Refuse

    def name(self, name: str, env: dict[str, str]) -> str:
        if name in env:
            return env[name]
        if name in self.assigned:
            raise _Refuse  # read before the assignment that binds it
        kind = self.module.types.get(name)
        if kind is None:
            raise _Refuse
        return kind

    @staticmethod
    def builtin(kind: str) -> str:
        if kind not in _BUILTIN:
            raise _Refuse
        return kind

    def boolop(self, node: ast.BoolOp, env: dict[str, str]) -> str:
        scope = dict(env)
        kinds = []
        for value in node.values:
            kinds.append(self.builtin(self.expr(value, scope)))
            narrowed = _non_null_when_true(value, scope) if isinstance(node.op, ast.And) else _non_null_when_false(value, scope)
            scope = _narrow(scope, narrowed)
        if len(set(kinds)) == 1:
            return kinds[0]
        return "opt_str" if set(kinds) <= {"str", "opt_str", "none"} else "mixed"

    def compare(self, node: ast.Compare, env: dict[str, str]) -> str:
        left = node.left
        left_kind = self.expr(left, env)
        for op, right in zip(node.ops, node.comparators):
            right_kind = self.expr(right, env)
            if isinstance(op, (ast.Is, ast.IsNot)):
                pass
            elif isinstance(op, (ast.Eq, ast.NotEq)):
                if left_kind not in _SCALARS or right_kind not in _SCALARS:
                    raise _Refuse
            elif isinstance(op, (ast.In, ast.NotIn)):
                if right_kind in ("set", "fset", "dict"):
                    if left_kind not in _HASHABLE:
                        raise _Refuse
                elif right_kind == "seq":
                    if left_kind not in _SCALARS:
                        raise _Refuse
                elif right_kind == "str":
                    if left_kind != "str":
                        raise _Refuse
                else:
                    raise _Refuse
            else:
                raise _Refuse
            left_kind = right_kind
        return "bool"

    def call(self, node: ast.Call, env: dict[str, str]) -> str:
        func = node.func
        if not isinstance(func, ast.Attribute):
            raise _Refuse
        os_name = self.module.os_name
        if os_name is not None and not self._shadowed(os_name):
            if _is_path(func, (os_name, "getenv")):
                return self.getenv(node, allow_keyword=True)
            if _is_path(func, (os_name, "environ", "get")):
                return self.getenv(node, allow_keyword=False)
        receiver = self.expr(func.value, env)
        if receiver != "str" or node.keywords:
            raise _Refuse
        constants = all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args)
        if func.attr in _STR_STRIPS and len(node.args) <= 1 and constants:
            return "str"
        if func.attr in _STR_TRANSFORMS and not node.args:
            return "str"
        if func.attr in _STR_PREDICATES and len(node.args) == 1 and constants:
            return "bool"
        if func.attr in _STR_FLAGS and not node.args:
            return "bool"
        raise _Refuse

    def _shadowed(self, name: str) -> bool:
        return name in self.params or name in self.assigned

    @staticmethod
    def getenv(node: ast.Call, *, allow_keyword: bool) -> str:
        args = list(node.args)
        for keyword in node.keywords:
            if not allow_keyword or keyword.arg != "default":
                raise _Refuse
            args.append(keyword.value)
        if not 1 <= len(args) <= 2 or len(node.args) < 1:
            raise _Refuse
        key = args[0]
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            raise _Refuse
        if len(args) == 1:
            return "opt_str"
        default = args[1]
        if isinstance(default, ast.Constant) and default.value is None:
            return "opt_str"
        if isinstance(default, ast.Constant) and isinstance(default.value, str):
            return "str"
        raise _Refuse

    # -- statements ------------------------------------------------------

    def block(self, body: list[ast.stmt], env: dict[str, str], values: set[bool], *, nested: bool) -> bool:
        """Check ``body``; whether it always returns. Fills ``values`` with its returns."""
        terminal = False
        for stmt in body:
            if isinstance(stmt, ast.Return):
                value = stmt.value
                if not (isinstance(value, ast.Constant) and isinstance(value.value, bool)):
                    raise _Refuse
                values.add(value.value)
                terminal = True
            elif isinstance(stmt, ast.Pass):
                continue
            elif isinstance(stmt, ast.If):
                test = self.builtin(self.expr(stmt.test, env))
                del test
                yes = _narrow(env, _non_null_when_true(stmt.test, env))
                no = _narrow(env, _non_null_when_false(stmt.test, env))
                body_terminal = self.block(stmt.body, dict(yes), values, nested=True)
                else_terminal = self.block(stmt.orelse, dict(no), values, nested=True)
                if body_terminal and else_terminal:
                    terminal = True
                elif body_terminal:
                    env.update(no)
                elif else_terminal:
                    env.update(yes)
            elif not nested and isinstance(stmt, ast.Assign):
                if not all(isinstance(t, ast.Name) for t in stmt.targets):
                    raise _Refuse
                kind = self.expr(stmt.value, env)
                for t in stmt.targets:
                    env[t.id] = kind  # type: ignore[union-attr]
            elif (
                not nested
                and isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                env[stmt.target.id] = self.expr(stmt.value, env)
            else:
                raise _Refuse
        return terminal


def _is_path(node: ast.expr, path: tuple[str, ...]) -> bool:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return False
    parts.append(node.id)
    return tuple(reversed(parts)) == path


def _narrow(env: dict[str, str], names: set[str]) -> dict[str, str]:
    narrowed = dict(env)
    for name in names:
        if narrowed.get(name) == "opt_str":
            narrowed[name] = "str"
    return narrowed


def _non_null_when_false(test: ast.expr, env: dict[str, str]) -> set[str]:
    """Names known not to be ``None`` once ``test`` is false."""
    if (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        return {test.left.id}
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _non_null_when_true(test.operand, env)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return set().union(*(_non_null_when_false(v, env) for v in test.values))
    return set()


def _non_null_when_true(test: ast.expr, env: dict[str, str]) -> set[str]:
    """Names known not to be ``None`` once ``test`` is true."""
    if (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        return {test.left.id}
    if isinstance(test, ast.Name) and env.get(test.id) == "opt_str":
        return {test.id}
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _non_null_when_false(test.operand, env)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return set().union(*(_non_null_when_true(v, env) for v in test.values))
    return set()


def _plan(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, module: _Module, source: str, comments: set[int]
) -> tuple[int, int, str, int, bool] | None:
    """``(first line, last line, replacement)`` collapsing ``fn``, ``None`` to keep it."""
    for decorator in fn.decorator_list:
        if not (isinstance(decorator, ast.Name) and decorator.id in _DECORATORS):
            return None
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) < 2:
        return None  # a lone `return <literal>` has nothing to delete
    values: set[bool] = set()
    try:
        terminal = _Function(fn, module).block(body, {}, values, nested=False)
    except _Refuse:
        return None
    if not terminal or len(values) != 1:
        return None
    first, last = body[0], body[-1]
    lines = io.StringIO(source, newline="").readlines()
    head = lines[first.lineno - 1]
    indent = head[: len(head) - len(head.lstrip(" \t"))]
    if not indent or head[len(indent) :].startswith("#"):
        return None
    if head.encode("utf-8")[: first.col_offset].strip(b" \t"):
        return None  # shares its line with the header or another statement
    tail = lines[last.end_lineno - 1].encode("utf-8")[last.end_col_offset :].decode("utf-8")  # type: ignore[index]
    if tail.strip():
        return None
    if fn.body[0] is not body[0]:
        start = fn.body[0].end_lineno + 1  # type: ignore[operator]
    else:
        ends = [fn.lineno, *(a.end_lineno or 0 for a in ast.walk(fn.args) if hasattr(a, "end_lineno"))]
        if fn.returns is not None:
            ends.append(fn.returns.end_lineno or 0)
        start = max(ends) + 1
    if any(row in comments for row in range(min(start, first.lineno), (last.end_lineno or last.lineno) + 1)):
        return None  # a comment of the customer's sits in the range
    removed = len(body) - 1
    # A statement may not share a line with a neighbour.
    seen: set[int] = set()
    for stmt in body:
        if stmt.lineno in seen or stmt.lineno <= fn.lineno:
            return None
        seen.update(range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))
    literal = values.pop()
    return first.lineno, last.end_lineno, f"{indent}return {literal}", removed, literal  # type: ignore[return-value]
