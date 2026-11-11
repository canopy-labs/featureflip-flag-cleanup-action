"""Which un-awaited accessor reads may become a bare literal, in TypeScript and JavaScript.

An accessor the project names (``accessors: isEnabled``) is cloned from the
SDK's synchronous seed, so an un-awaited call folded to a bare boolean. When
the accessor really returns a Promise that changes the type of the expression
it sits in: ``return client.isEnabled(...)`` became ``return true`` and every
caller that awaits it, chains ``.then`` or asserts with ``.resolves`` broke.

The name alone cannot say which kind of accessor it is, so each READ is decided
on its own, around two invariants.

**INV-1.** ``Promise.resolve(<lit>)`` is written only on LOCAL, certain
evidence: the enclosing function's explicit ``Promise``/``PromiseLike`` return
annotation, or a receiver resolved soundly to a member with an explicit
Promise return type. A scan of the name's declarations across the repository
can never produce it.

**INV-2.** A bare literal is never written when there is ANY Promise evidence:
at the use site (the result is the object of a member access, ``as``/
``satisfies Promise<..>``, an argument of ``Promise.all`` and friends, the
subject of ``expect(..).resolves``), at the receiver's declaration, or in a
declaration of the name anywhere in the repository that is, or could be, async.
Such a read is left, and the residual scan reports its file.

Order, first match wins, after parentheses, ``!`` (non-null), ``as`` and
``satisfies`` around the read are looked through:

0. Use-site evidence: leave.
1. The read is what an ``async`` function returns: the literal (the function
   wraps it).
2. The read is what a function with an explicit return type returns:
   ``Promise<..>`` gives ``Promise.resolve``, exactly ``boolean`` gives the
   literal, anything else leaves. A read that is only an arm of a ternary or an
   operand of ``&&``/``||``/``??`` in return position folds only inside an
   ``async`` function or one annotated exactly ``boolean``, and is left
   otherwise.
3. The receiver is a parameter, variable or ``this.field`` whose NEAREST
   binding carries a type annotation, resolved through the file's own
   declarations or relative imports to one declaring file, whose members of
   that name agree: ``Promise`` gives ``Promise.resolve`` where the read is
   returned (left elsewhere), ``boolean`` gives the literal, a mix leaves.
   Anything unresolvable (a package type, a destructured binding, a deeper
   ``this`` chain) falls through.
4. By name: any declaration in the repository that is async, Promise-typed,
   documented as returning a Promise, or could be (an explicit return type that
   is not a primitive or a union of primitives, no return type on a signature,
   a body that returns a call other than one of an audited package export
   (``SYNC_PACKAGE_EXPORTS``), a const initialised from a call, a ``.then``
   chain) leaves the read; none folds it. Test files are not read, and in test
   support directories (``tests/``, ``__mocks__/``, ...) a fake whose returns
   call nothing does not count. A name declared nowhere folds, as it always did.

An accessor with at least one read that does not fold to the literal is
"managed": its un-awaited reads are marked per site (see ``piranha_runner``)
instead of matched by the bare-call clone. Awaited reads fold as before; an
awaited read through a key constant is not folded for a managed accessor.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import kotlin_sentinel, ts_syntax

_EXTENSIONS = {".ts": "ts", ".tsx": "tsx", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".mts": "ts", ".cts": "ts"}

#: The argument of a ``sentinel(<tag>)`` call, which the promise rule in
#: ``rules/ts.toml`` turns into ``Promise.resolve(<literal>)``.
PROMISE_TAG = "FF_CLEANUP_PROMISE_READ"

LITERAL, PROMISE, LEAVE = "literal", "promise", "leave"

_PROMISE_TYPE = re.compile(r"\A\s*:?\s*(?:Promise|PromiseLike)\s*<")
_PROMISEISH = re.compile(
    r"Promise|Awaitable|Thenable|\.then\s*\(|\.catch\s*\(|\.finally\s*\(|\bfetch\s*\(|Resolved|Rejected|\basync\b"
)
_JSDOC_PROMISE = re.compile(r"@returns?\s*\{[^}]*Promise", re.DOTALL)

_FUNCTION_NODES = frozenset(
    {"function_declaration", "function_expression", "function", "arrow_function", "method_definition", "generator_function", "generator_function_declaration"}
)
_SIGNATURE_KINDS = frozenset({"method_signature", "function_signature", "abstract_method_signature"})
_CALLABLE_KINDS = _SIGNATURE_KINDS | {"method_definition", "function_declaration"}
_VALUE_KINDS = frozenset({"property_signature", "public_field_definition", "variable_declarator", "pair"})
#: The declarations that bind a name a bare ``name(...)`` call can reach. A
#: member (a property, field, method or signature) is reached only through
#: its object.
_BINDING_KINDS = frozenset({"variable_declarator", "function_declaration", "function_signature"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_TYPE_DECLARATIONS = frozenset(
    {"interface_declaration", "class_declaration", "abstract_class_declaration", "class", "type_alias_declaration"}
)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _has_async_token(node: Node) -> bool:
    return any(child.type == "async" for child in node.children)


def return_kind(type_node: Node | None) -> str:
    """``promise`` / ``sync`` / ``other`` for an explicit return type annotation."""
    if type_node is None:
        return "none"
    text = _text(type_node).strip().lstrip(":").strip()
    if _PROMISE_TYPE.match(": " + text) and _whole_generic(text):
        return "promise"
    if text == "boolean":
        return "sync"
    return "other"


def _whole_generic(text: str) -> bool:
    """True when ``text`` is ONE ``Name<…>`` type and nothing more.

    The `<` that opens the type arguments must close at the very last
    character: `Promise<boolean> | boolean`, `Promise<boolean>[]` and
    `Promise<boolean> & X` are unions, arrays and intersections, not a
    Promise, so they are not written as ``Promise.resolve``.
    """
    start = text.find("<")
    if start < 0 or not text.endswith(">"):
        return False
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "<":
            depth += 1
        elif char == ">" and text[index - 1] != "=":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


def _function_type(node: Node | None) -> Node | None:
    if node is None:
        return None
    for child in ts_syntax._walk(node):
        if child.type == "function_type":
            return child
    return None


def _name_of(node: Node) -> str | None:
    name = node.child_by_field_name("key" if node.type == "pair" else "name")
    return _text(name).strip("'\"") if name is not None else None


def _jsdoc_promise(node: Node) -> bool:
    current: Node | None = node
    for _ in range(4):
        if current is None:
            return False
        previous = current.prev_sibling
        if previous is not None and previous.type == "comment":
            return _JSDOC_PROMISE.search(_text(previous)) is not None
        current = current.parent
    return False


def _member_kind(node: Node) -> str | None:
    """``promise`` / ``sync`` / ``other`` for one declared member, None when it says nothing."""
    if node.type in _CALLABLE_KINDS:
        if _has_async_token(node):
            return "promise"
        kind = return_kind(node.child_by_field_name("return_type"))
        return "other" if kind == "none" else kind
    if node.type in _VALUE_KINDS:
        value = node.child_by_field_name("value")
        if value is not None and value.type in _FUNCTION_VALUES:
            if _has_async_token(value):
                return "promise"
            kind = return_kind(value.child_by_field_name("return_type"))
            return "other" if kind == "none" else kind
        ftype = _function_type(node.child_by_field_name("type"))
        if ftype is not None:
            kind = return_kind(ftype.child_by_field_name("return_type"))
            return "other" if kind == "none" else kind
        return "other"
    return None


_LOGICAL = frozenset({"&&", "||", "??"})
#: Expressions whose value is always a primitive, never an object, so never a Promise.
#: Not ``undefined``: it is a name, and a parameter can shadow it.
_PRIMITIVE_NODES = frozenset({"true", "false", "number", "string", "template_string", "null"})


def _certainly_boolean(expr: Node | None) -> bool:
    """A returned expression that cannot be a Promise: a literal (boolean,
    number, string, template, ``null``), any unary or binary
    operation except the logical ones (comparisons, type tests, ``!``,
    ``typeof``, arithmetic all yield primitives), or a ternary or
    ``&&``/``||``/``??`` whose every possible result is one of those."""
    while expr is not None and expr.type == "parenthesized_expression" and expr.named_children:
        expr = expr.named_children[0]
    if expr is None:
        return False
    if expr.type in _PRIMITIVE_NODES:
        return True
    if expr.type == "binary_expression":
        operator = expr.child_by_field_name("operator")
        if operator is None:
            return False
        if _text(operator) in _LOGICAL:
            return _certainly_boolean(expr.child_by_field_name("left")) and _certainly_boolean(
                expr.child_by_field_name("right")
            )
        return True
    if expr.type == "ternary_expression":
        return _certainly_boolean(expr.child_by_field_name("consequence")) and _certainly_boolean(
            expr.child_by_field_name("alternative")
        )
    if expr.type == "unary_expression":
        operator = expr.child_by_field_name("operator")
        return operator is not None and _text(operator) != "delete"
    return False


#: Types a Promise can never be assigned to. Not ``void``: a function typed
#: ``() => void`` may be an async function.
_PRIMITIVE_TYPES = re.compile(
    r"""\A(?:boolean|true|false|undefined|null|string|number|bigint|-?\d[\w.]*|'[^'|]*'|"[^"|]*")\Z"""
)


def _primitive_return(type_node: Node | None) -> bool:
    """An explicit return type that is a primitive keyword or literal type, or a
    union of them (``boolean | undefined``)."""
    if type_node is None:
        return False
    text = _text(type_node).strip().lstrip(":").strip().lstrip("|").strip()
    parts = [part.strip() for part in text.split("|")]
    return bool(text) and all(_PRIMITIVE_TYPES.match(part) for part in parts)


def _returned_expressions(subject: Node) -> list[Node | None]:
    """What a function returns: its expression body, or each ``return``'s
    expression (None for a bare ``return``), nested functions excluded."""
    body = subject.child_by_field_name("body")
    if body is None:
        return []
    if body.type != "statement_block":
        return [body]
    returns: list[Node | None] = []
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type in _FUNCTION_NODES:
            continue
        if node.type == "return_statement":
            returns.append(node.named_children[0] if node.named_children else None)
        stack.extend(node.children)
    return returns


def _returns_only_booleans(subject: Node, sync_call: Callable[[Node], bool] | None = None) -> bool:
    """Every returned expression is certainly a primitive, or, given
    ``sync_call``, a call it proves returns no Promise."""
    returns = _returned_expressions(subject)
    return bool(returns) and all(
        _certainly_boolean(r) or (sync_call is not None and r is not None and sync_call(r)) for r in returns
    )


#: Exports of published packages whose result IS the flag's value, never a
#: Promise, keyed by package, each with the index of the argument the export
#: may hand back (None: it hands back none of them). Read off the type
#: declarations of every release of the package published up to the audit
#: (2026-11), and the runtime source of the latest: GrowthBook's React SDK
#: declares ``useFeatureIsOn(...): boolean`` and ``useFeatureValue<T extends
#: JSONValue>(id, fallback: T): WidenPrimitives<T>``, which is the flag's JSON
#: value or ``fallback``. TypeScript can only infer a JSON type for that
#: ``fallback``, so the argument matters only in JavaScript, where nothing
#: checks it (:func:`_audited_sync_call`).
#:
#: Named one by one, never matched by a ``use`` prefix: a hook is a plain
#: function and may return anything. Not a Promise is not enough: GrowthBook's
#: ``useFeature`` returns a result object, always truthy, so a wrapper of it
#: does not return the flag's value and is left out, as are the package's
#: other exports (``useGrowthBook``, ``useGrowthBookSSR``, ...) and SDKs whose
#: value hook returns its default unchecked in TypeScript too
#: (``useFeatureFlag<T>(key, defaultValue: T): T``).
SYNC_PACKAGE_EXPORTS: dict[str, dict[str, int | None]] = {
    "@growthbook/growthbook-react": {"useFeatureIsOn": None, "useFeatureValue": 1},
}


def _audited_sync_call(expr: Node, tree: Node, path: str, repo_dir: str) -> bool:
    """Whether ``expr`` is a call of an export of :data:`SYNC_PACKAGE_EXPORTS`.

    The callee is a plain name bound once in the file, by a value import of
    that export from its package (aliased or not), and no compiler config can
    read the package's name as a file of the checkout
    (:func:`cross_file_ts.names_package`). In a JavaScript file an argument
    the export may hand back must be a primitive literal."""
    from flag_cleanup import cross_file_ts, inert_calls, ts_scope

    while expr.type == "parenthesized_expression" and expr.named_children:
        expr = expr.named_children[0]
    if expr.type != "call_expression" or any(c.type == "?." for c in expr.children):
        return False
    callee = expr.child_by_field_name("function")
    if callee is None or callee.type != "identifier":
        return False
    binders = ts_scope.bindings(tree, _text(callee))
    if len(binders) != 1:
        return False
    imported = inert_calls._imported(binders[0])
    if imported is None:
        return False
    specifier, name = imported
    exports = SYNC_PACKAGE_EXPORTS.get(specifier)
    if exports is None or name not in exports:
        return False
    handed_back = exports[name]
    if handed_back is not None and _EXTENSIONS.get(Path(path).suffix) == "js":
        args = expr.child_by_field_name("arguments")
        items = [c for c in args.named_children if c.type != "comment"] if args is not None else []
        if len(items) <= handed_back or items[handed_back].type not in _PRIMITIVE_NODES - {"template_string"}:
            return False
    return cross_file_ts.names_package(path, specifier, repo_dir)


def _only_reads(expr: Node | None) -> bool:
    """A returned expression that calls and constructs nothing: a primitive, a
    name, a property or index read, or a ternary or ``&&``/``||``/``??`` whose
    every possible result is one of those."""
    while expr is not None and expr.type == "parenthesized_expression" and expr.named_children:
        expr = expr.named_children[0]
    if expr is None:
        return False
    if _certainly_boolean(expr):
        return True
    if expr.type in {"identifier", "member_expression", "subscript_expression"}:
        return not any(
            n.type in {"call_expression", "new_expression", "await_expression"} or n.type in _FUNCTION_NODES
            for n in ts_syntax._walk(expr)
        )
    if expr.type == "binary_expression":
        operator = expr.child_by_field_name("operator")
        return (
            operator is not None
            and _text(operator) in _LOGICAL
            and _only_reads(expr.child_by_field_name("left"))
            and _only_reads(expr.child_by_field_name("right"))
        )
    if expr.type == "ternary_expression":
        return _only_reads(expr.child_by_field_name("consequence")) and _only_reads(
            expr.child_by_field_name("alternative")
        )
    return False


_TEST_DOUBLE_METHODS = frozenset({"stub", "fn", "spyOn", "createStubInstance"})
_TEST_FILE = re.compile(r"(\.(test|spec)\.[cm]?[jt]sx?$)|(/__tests__/)")
#: A directory of test support code, matched on the path from the repository
#: root. A declaration there that only reads is a fake (see ``_a_fake``).
_TEST_DIRECTORY = re.compile(r"(?:\A|/)(?:tests?|__mocks__|fixtures|__fixtures__)/")


#: Packages whose ``fn``/``spyOn``, imported by name, make the same spy as
#: ``vi.fn``/``jest.fn``: Storybook re-exports Vitest's.
_TEST_DOUBLE_PACKAGES = frozenset({"storybook/test", "@storybook/test", "@vitest/spy", "jest-mock"})


def _is_test_double(value: Node | None, imported_double: Callable[[Node], bool] | None = None) -> bool:
    """``sinon.stub(..)``, ``jest.fn()``, ``vi.fn().mockResolvedValue(..)``,
    ``x.spyOn(..)``, and, given ``imported_double``, a call of a name it
    proves is a test library's (``fn(..).mockName(..)``, see
    :func:`_imported_test_double`)."""
    while value is not None and value.type == "call_expression":
        callee = value.child_by_field_name("function")
        if callee is not None and callee.type == "identifier" and imported_double is not None:
            return imported_double(callee)
        if callee is None or callee.type != "member_expression":
            return False
        prop = callee.child_by_field_name("property")
        if prop is not None and _text(prop) in _TEST_DOUBLE_METHODS:
            return True
        value = callee.child_by_field_name("object")
    return False


def _imported_test_double(callee: Node, tree: Node, path: str, repo_dir: str) -> bool:
    """Whether ``callee`` names ``fn`` or ``spyOn`` of :data:`_TEST_DOUBLE_PACKAGES`,
    bound once in the file by a value import of it from the package."""
    from flag_cleanup import cross_file_ts, inert_calls, ts_scope

    binders = ts_scope.bindings(tree, _text(callee))
    imported = inert_calls._imported(binders[0]) if len(binders) == 1 else None
    return (
        imported is not None
        and imported[0] in _TEST_DOUBLE_PACKAGES
        and imported[1] in ("fn", "spyOn")
        and cross_file_ts.names_package(path, imported[0], repo_dir)
    )


#: Calls of a global that always return a primitive, when the file binds
#: no other value to the name.
_PRIMITIVE_GLOBALS = frozenset({"Boolean", "String", "Number"})


def _not_callable(expr: Node | None, tree: Node, depth: int = 0) -> bool:
    """Whether ``expr`` can never evaluate to a function.

    A primitive (:func:`_certainly_boolean`), an object or array literal,
    ``Boolean(x)``/``String(x)``/``Number(x)`` when the file binds none of
    those names, ``a && b`` when ``b`` cannot (a function is truthy, so
    ``a`` is the result only when it is not one), ``a || b``, ``a ?? b`` or
    a ternary when no operand that can be the result can, and a name bound
    once in the file by a ``const`` whose value cannot, or whose declared type
    is a primitive. Nothing else: a ``let``, a parameter or a property can be
    given a function later."""
    while expr is not None and expr.type in ("parenthesized_expression", "non_null_expression") and expr.named_children:
        expr = expr.named_children[0]
    if expr is None or depth > 8:
        return False
    if _certainly_boolean(expr) or expr.type in ("object", "array"):
        return True
    if expr.type == "binary_expression":
        operator = _text(expr.child_by_field_name("operator"))
        right = _not_callable(expr.child_by_field_name("right"), tree, depth + 1)
        if operator == "&&":
            return right
        return operator in ("||", "??") and right and _not_callable(expr.child_by_field_name("left"), tree, depth + 1)
    if expr.type == "ternary_expression":
        return _not_callable(expr.child_by_field_name("consequence"), tree, depth + 1) and _not_callable(
            expr.child_by_field_name("alternative"), tree, depth + 1
        )
    if expr.type == "call_expression":
        callee = expr.child_by_field_name("function")
        return (
            callee is not None
            and callee.type == "identifier"
            and _text(callee) in _PRIMITIVE_GLOBALS
            and not _bound_anywhere(tree, _text(callee))
        )
    if expr.type == "identifier":
        from flag_cleanup import ts_scope

        binders = ts_scope.bindings(tree, _text(expr))
        if len(binders) != 1:
            return False
        holder = binders[0].parent
        if holder is None:
            return False
        annotation = holder.child_by_field_name("type")
        if holder.type != "variable_declarator" or holder.child_by_field_name("name") != binders[0]:
            return False
        if not _const_declarator(holder):
            return False
        if annotation is not None:
            return _primitive_return(annotation)
        return _not_callable(holder.child_by_field_name("value"), tree, depth + 1)
    return False


def _const_declarator(node: Node) -> bool:
    """Whether ``node`` is a declarator of a ``const``: the one declaration
    whose value nothing can replace. A ``let``, a parameter, a property, a
    field (``readonly`` or not) and an interface member can all be given a
    function by a write this scan may not see."""
    lexical = node.parent if node.type == "variable_declarator" else None
    return lexical is not None and lexical.type == "lexical_declaration" and any(
        c.type == "const" for c in lexical.children
    )


#: Calls that define a property from a descriptor: ``(target, key, desc)``.
_DEFINE_PROPERTY = frozenset({"Object.defineProperty", "Reflect.defineProperty"})
_ASSIGNMENTS = frozenset({"assignment_expression", "augmented_assignment_expression"})


def _callee_text(call: Node) -> str:
    callee = call.child_by_field_name("function")
    return _text(callee).replace(" ", "") if callee is not None else ""


def _descriptor_possibly_async(descriptor: Node | None, not_callable: Callable[[Node], bool] | None) -> bool | None:
    """A property descriptor may define a function unless it is an object
    literal of plain ``value``/``writable``/``enumerable``/``configurable``
    keys whose ``value``, if any, cannot be a function. A getter can return
    anything."""
    if descriptor is None or descriptor.type != "object":
        return True
    for prop in descriptor.named_children:
        if prop.type == "comment":
            continue
        key = prop.child_by_field_name("key") if prop.type == "pair" else None
        if key is None or _text(key).strip("'\"") not in ("value", "writable", "enumerable", "configurable"):
            return True
        if _text(key).strip("'\"") == "value" and not (not_callable is not None and not_callable(prop.child_by_field_name("value"))):
            return True
    return None


def _written_value_possibly_async(
    value: Node | None,
    sync_call: Callable[[Node], bool] | None,
    imported_double: Callable[[Node], bool] | None,
    not_callable: Callable[[Node], bool] | None,
) -> bool | None:
    """What a write gives the name (``o.enabled = <value>``), judged as a
    declarator's value is: a function by its ``async`` token, return type or
    returns, a test double or a value that cannot be a function by nothing,
    anything else as possibly async."""
    if value is None:
        return True
    if value.type in _FUNCTION_VALUES:
        if _has_async_token(value):
            return True
        rtype = value.child_by_field_name("return_type")
        if rtype is not None:
            return not _primitive_return(rtype)
        return not _returns_only_booleans(value, sync_call)
    if _is_test_double(value, imported_double):
        return None
    if not_callable is not None and not_callable(value):
        return None
    return True


def _write_of(node: Node, name: str) -> tuple[str, Node | None] | None:
    """How ``node`` writes ``name``, when it does: ``("binding", value)`` for
    ``name = value`` (``||=`` and friends too), ``("member", value)`` for
    ``x.name = value``, ``x["name"] = value``,
    ``Object.defineProperty(x, "name", desc)`` (the value is then the
    descriptor). A write whose key cannot be read (``x[k] = v``) adds no
    declaration, and no declaration it could replace is exempt
    (:func:`_const_declarator`)."""
    if node.type in _ASSIGNMENTS:
        left = node.child_by_field_name("left")
        while left is not None and left.type in ("parenthesized_expression", "non_null_expression") and left.named_children:
            left = left.named_children[0]
        value = node.child_by_field_name("right")
        if left is None:
            return None
        if left.type == "identifier":
            return ("binding", value) if _text(left) == name else None
        if left.type == "member_expression":
            prop = left.child_by_field_name("property")
            return ("member", value) if prop is not None and _text(prop) == name else None
        if left.type == "subscript_expression":
            index = left.child_by_field_name("index")
            literal = index is not None and index.type == "string"
            if not literal:
                return None
            return ("member", value) if _text(index)[1:-1] == name else None
        return None
    if node.type == "call_expression" and _callee_text(node) in _DEFINE_PROPERTY:
        args = node.child_by_field_name("arguments")
        items = [c for c in args.named_children if c.type != "comment"] if args is not None else []
        if len(items) < 2 or items[1].type != "string":
            return None
        if _text(items[1])[1:-1] != name:
            return None
        return ("member", items[2] if len(items) > 2 else None)
    return None


def _declared_scope(declaration: Node) -> Node | None:
    """The node a declaration's name is certainly visible throughout: a
    ``let``/``const``'s enclosing block or statement, a ``var``'s enclosing
    function (or the file), a ``function`` declaration's enclosing block.
    An ``export`` around one is looked through."""
    if declaration.type == "variable_declarator":
        holder = declaration.parent
        if holder is None:
            return None
        if holder.type == "variable_declaration":  # `var`
            current = holder.parent
            while current is not None and current.type not in _FUNCTION_NODES and current.type != "program":
                current = current.parent
            return current
        scope = holder.parent
    else:
        scope = declaration.parent
    if scope is not None and scope.type == "export_statement":
        scope = scope.parent
    return scope


def _bound_anywhere(tree: Node, name: str) -> bool:
    from flag_cleanup import ts_scope

    return bool(ts_scope.bindings(tree, name))


def _possibly_async(
    node: Node,
    sync_call: Callable[[Node], bool] | None = None,
    imported_double: Callable[[Node], bool] | None = None,
    not_callable: Callable[[Node], bool] | None = None,
) -> bool | None:
    """Whether a declaration of the name is, or could be, async. None: says nothing.

    Possibly async UNLESS it has an explicit return type that is ``boolean`` or
    another primitive (or a union of them), or is a non-``async`` function
    whose every returned expression is certainly a primitive or, given
    ``sync_call``, a call it proves returns no Promise
    (:func:`_audited_sync_call`). A value that is a reference, any other call,
    an alias or a returned local, or a ternary or ``&&`` with such an arm, all
    count as possibly async.

    A ``const`` whose value cannot be a function says nothing, since no read
    calls it and nothing can replace it: one ``not_callable`` proves
    (:func:`_not_callable`), or one whose declared type is a primitive
    (``const enabled: boolean = …``). Every other declaration of the name
    counts: a property, field or interface member whatever its value.
    """
    if node.type in _VALUE_KINDS:
        value = node.child_by_field_name("value")
        annotation = node.child_by_field_name("type")
        if _is_test_double(value, imported_double):
            return None
        ftype = _function_type(annotation)
        if value is not None and value.type in _FUNCTION_VALUES:
            subject = value
            if ftype is not None and not (_has_async_token(value) or _jsdoc_promise(node)):
                # `const isEnabled: (k: string) => R = (k) => …`: the declared type decides.
                return not _primitive_return(ftype.child_by_field_name("return_type"))
        else:
            if ftype is not None:
                return _jsdoc_promise(node) or not _primitive_return(ftype.child_by_field_name("return_type"))
            if annotation is None and value is None:
                return None
            if _const_declarator(node):
                if annotation is not None and _primitive_return(annotation):
                    return None  # `const enabled: boolean = …`: never called
                if annotation is None and not_callable is not None and not_callable(value):
                    return None  # `const enabled = opts.enabled !== false`: never called
            return True
    else:
        subject = node
    if _has_async_token(subject) or _jsdoc_promise(node):
        return True
    rtype = subject.child_by_field_name("return_type")
    if rtype is not None:
        return not _primitive_return(rtype)
    if node.type in _SIGNATURE_KINDS:
        return True
    return not _returns_only_booleans(subject, sync_call)


#: Text that can make or name a Promise, a thenable or a getter (whose read runs code).
_PROMISE_TOKENS = re.compile(_PROMISEISH.pattern + r"|\bawait\b|\bthen\b|(?i:thenable)|\bget\s+[\w$'\"\[]")
_RUNS_CODE = frozenset(
    {"call_expression", "new_expression", "await_expression", "yield_expression", "method_definition", "class",
     "spread_element", "tagged_template_expression"}
) | _FUNCTION_NODES
_WRITES = frozenset({"assignment_expression", "augmented_assignment_expression"})


def _a_fake(node: Node, tree: Node) -> bool:
    """A declaration in test support code that reads like a fake and provably
    returns no Promise of its own making.

    It is a function that is not ``async`` and carries no return type, declared
    type or ``@returns``; its text holds no Promise-ish token; every returned
    expression only reads (``(key) => key === flag && enabled``,
    ``(key) => flags[key] ?? false``); and every name those returns read
    resolves (``_resolved``) to a parameter or to a binding of this file that
    itself only reads. What a PARAMETER carries is whatever a caller passes,
    and is not decided here."""
    subject = node
    if node.type in _VALUE_KINDS:
        if node.child_by_field_name("type") is not None:
            return False
        subject = node.child_by_field_name("value")
        if subject is None or subject.type not in _FUNCTION_VALUES:
            return False
    elif node.type in _SIGNATURE_KINDS:
        return False
    if _has_async_token(subject) or _jsdoc_promise(node) or subject.child_by_field_name("return_type") is not None:
        return False
    if _PROMISE_TOKENS.search(_text(subject)):
        return False
    returns = _returned_expressions(subject)
    if not returns or not all(_only_reads(r) for r in returns):
        return False
    return all(r is not None and _resolved(r, tree, set(), subject) for r in returns)


def _resolved(expr: Node, tree: Node, seen: set[int], fake: Node) -> bool:
    """Every name ``expr`` reads is a parameter of an enclosing function, or a
    ``const``/``let`` of this file bound once, whose initializer is a primitive
    or an object/array literal that runs no code and whose names resolve too,
    and which nothing in the file writes to (``x = ``, ``x.a = ``, ``x[k]++``,
    ``delete x.a``) or hands on (``_escapes``). ``this`` never resolves."""
    for node in [expr, *ts_syntax._walk(expr)]:
        if node.type in {"this", "super"}:
            return False
        if node.type in _RUNS_CODE:
            return False
        if node.type in {"identifier", "shorthand_property_identifier", "undefined"}:
            if not _name_resolves(node, _text(node), tree, seen, fake):
                return False
    return True


def _params(function: Node) -> list[Node]:
    params = function.child_by_field_name("parameters")
    nodes = list(params.named_children) if params is not None else []
    single = function.child_by_field_name("parameter")
    if single is not None:
        nodes.append(single)
    return nodes


def _param_names(function: Node) -> set[str]:
    names: set[str] = set()
    for param in _params(function):
        names |= _names_bound_by(param)
    return names


def _names_bound_by(param: Node) -> set[str]:
    """The names one parameter binds; its default values bind nothing."""
    names: set[str] = set()
    stack = [param]
    while stack:
        current = stack.pop()
        if current.type in {"identifier", "shorthand_property_identifier_pattern"}:
            names.add(_text(current))
            continue
        if current.type in {"assignment_pattern", "object_assignment_pattern"}:
            left = current.child_by_field_name("left")
            if left is not None:
                stack.append(left)
            continue  # a default value binds nothing
        if current.type in {"required_parameter", "optional_parameter"}:
            pattern = current.child_by_field_name("pattern")
            if pattern is not None:
                stack.append(pattern)
            continue
        if current.type == "pair_pattern":
            value = current.child_by_field_name("value")
            if value is not None:
                stack.append(value)
            continue
        stack.extend(current.named_children)
    return names


def _bindings_of(tree: Node, name: str) -> list[Node]:
    """Every node of the file that binds ``name``."""
    found = []
    for node in ts_syntax._walk(tree):
        kind = node.type
        if kind == "variable_declarator" and _binds(node.child_by_field_name("name"), name):
            found.append(node)
        elif kind in _FUNCTION_NODES and name in _param_names(node):
            found.append(node)
        elif kind in {"function_declaration", "generator_function_declaration", "class_declaration", "class", "function_expression"}:
            own = node.child_by_field_name("name")
            if own is not None and _text(own) == name:
                found.append(node)
        elif kind == "import_statement" and _binds(node, name):
            found.append(node)
        elif kind == "catch_clause" and _binds(node.child_by_field_name("parameter"), name):
            found.append(node)
        elif kind == "for_in_statement" and _binds(node.child_by_field_name("left"), name):
            found.append(node)
    return found


def _defaults_read_only(function: Node, name: str, tree: Node, seen: set[int], fake: Node) -> bool:
    """Every default value in the parameter that binds ``name`` runs no code, and
    is a primitive or resolves (``(key, v = load(key)) => v`` is a call's
    result, not a parameter's; ``enabled = def.mode === 'on'`` is a boolean)."""
    for param in _params(function):
        if name not in _names_bound_by(param):
            continue
        if -1 - param.start_byte in seen:
            continue  # a default naming its own parameter list is checked once
        seen.add(-1 - param.start_byte)
        for node in [param, *ts_syntax._walk(param)]:
            if node.type in {"assignment_pattern", "object_assignment_pattern"}:
                default = node.child_by_field_name("right")
            elif node.type in {"required_parameter", "optional_parameter"}:
                default = node.child_by_field_name("value")  # `(key, v = load(key))`
            else:
                continue
            if default is None:
                continue
            if any(n.type in _RUNS_CODE for n in [default, *ts_syntax._walk(default)]):
                return False
            # A comparison or other operator yields a primitive whatever it reads.
            if not _certainly_boolean(default) and not _resolved(default, tree, seen, fake):
                return False
    return True


def _name_resolves(use: Node, name: str, tree: Node, seen: set[int], fake: Node) -> bool:
    current = use.parent
    while current is not None:
        if current.type in _FUNCTION_NODES and name in _param_names(current):
            body = current.child_by_field_name("body")
            if body is not None and (_written(body, name) or _bindings_of(body, name)):
                return False  # written, or shadowed by a binding inside (`let`, `catch`, `for … of`)
            return _defaults_read_only(current, name, tree, seen, fake)
        current = current.parent
    bindings = _bindings_of(tree, name)
    if not bindings:
        return name == "undefined"
    if len(bindings) != 1 or bindings[0].type != "variable_declarator":
        return False
    declarator = bindings[0]
    if declarator.start_byte in seen:
        return True  # a cycle adds nothing a first visit does not check
    seen.add(declarator.start_byte)
    holder = declarator.parent
    if holder is None or holder.type != "lexical_declaration":
        return False  # `var` hoists; only `const`/`let`
    if declarator.child_by_field_name("name").type != "identifier":
        return False
    scope = holder.parent
    if scope is None or not _is_ancestor(scope, use):
        return False
    value = declarator.child_by_field_name("value")
    if value is None or _PROMISE_TOKENS.search(_text(value)):
        return False
    if not _resolved(value, tree, seen, fake):
        return False
    if _certainly_boolean(value):
        return not _written(tree, name, declarator)  # a primitive cannot be changed through an alias
    return not _escapes(tree, name, declarator, fake)


def _is_parameter(use: Node, name: str) -> bool:
    current = use.parent
    while current is not None:
        if current.type in _FUNCTION_NODES and name in _param_names(current):
            return True
        current = current.parent
    return False


def _escapes(tree: Node, name: str, declarator: Node, fake: Node) -> bool:
    """Whether any use of an object-valued binding could change what it holds.

    Every use must only read it: through property or index reads into an
    operator that consumes the value, a condition, or the fake's own return.
    Anything else (a write, a method call on it, an alias, an argument) may
    replace a property with a Promise. The one exception is an argument, bare
    or inside an object or array literal, of a call to a PARAMETER: that
    callback is the caller's, the same as what a parameter carries."""
    own = declarator.child_by_field_name("name")
    for node in ts_syntax._walk(tree):
        if node.type not in {"identifier", "shorthand_property_identifier"} or _text(node) != name or node == own:
            continue
        if not _read_use(node, fake):
            return True
    return False


def _read_use(node: Node, fake: Node) -> bool:
    top = node
    while True:
        parent = top.parent
        if parent is None:
            return False
        kind = parent.type
        if kind in {"member_expression", "subscript_expression"} and parent.child_by_field_name("object") == top:
            top = parent
        elif kind in {"parenthesized_expression", "non_null_expression"}:
            top = parent
        elif kind == "binary_expression":
            if _text(parent.child_by_field_name("operator")) not in _LOGICAL:
                return True  # a comparison or arithmetic consumes the value
            top = parent
        elif kind == "ternary_expression":
            if parent.child_by_field_name("condition") == top:
                return True
            top = parent
        elif kind == "unary_expression":
            return _text(parent.child_by_field_name("operator")) != "delete"
        elif kind == "subscript_expression":
            return True  # used as an index
        elif kind in {"template_substitution", "if_statement", "while_statement", "do_statement", "expression_statement"}:
            return True
        elif kind == "return_statement" or (kind == "arrow_function" and parent.child_by_field_name("body") == top):
            return _is_ancestor(fake, top) or top.parent == fake
        elif kind in {"pair", "object", "array", "arguments"} or top.type == "shorthand_property_identifier":
            container = top
            while container.parent is not None and container.parent.type in {"pair", "object", "array"}:
                if container.parent.type == "pair" and container.parent.child_by_field_name("value") != container:
                    return False
                container = container.parent
            if container.parent is None or container.parent.type != "arguments" and container.type != "arguments":
                return False
            arguments = container if container.type == "arguments" else container.parent
            call = arguments.parent
            callee = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
            return callee is not None and callee.type == "identifier" and _is_parameter(callee, _text(callee))
        else:
            return False


def _is_ancestor(scope: Node, node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current == scope:
            return True
        current = current.parent
    return False


def _written(scope: Node, name: str, declarator: Node | None = None) -> bool:
    """Whether anything in ``scope`` writes ``name`` or through it."""
    for node in ts_syntax._walk(scope):
        if node.type != "identifier" or _text(node) != name:
            continue
        if declarator is not None and node == declarator.child_by_field_name("name"):
            continue
        top = node
        while top.parent is not None and top.parent.type in {"member_expression", "subscript_expression", "parenthesized_expression", "non_null_expression"} and top.parent.child_by_field_name("index") != top:
            top = top.parent
        parent = top.parent
        if parent is None:
            continue
        if parent.type in _WRITES and parent.child_by_field_name("left") == top:
            return True
        if parent.type == "update_expression":
            return True
        if parent.type == "unary_expression" and _text(parent.child_by_field_name("operator")) == "delete":
            return True
    return False


@dataclass(frozen=True)
class Site:
    path: str
    accessor: str
    callee: tuple[int, int]
    call: tuple[int, int]
    decision: str
    #: The first argument is the flag's key as a string literal. Any other first
    #: argument (a constant, a variable) may still name the key through a
    #: constant, and the decision matters for it too: a read that is left must
    #: keep its constant's declaration. Only keyed reads are marked.
    keyed: bool = True
    #: Why a LEAVE decision was made.
    why: str = ""


class _Repo:
    """Repo-wide facts, built once per plan: declarations by name, parsed files."""

    def __init__(self, repo_dir: str, accessors: frozenset[str]) -> None:
        from flag_cleanup.piranha_runner import _SKIP_DIRS, source_files

        self.accessors = accessors
        #: Every declaration of the name that may be async, whatever it is,
        #: and where the first one is: what a member read may call.
        self.possibly_async: dict[str, bool] = defaultdict(bool)
        self.async_at: dict[str, tuple[str, int]] = {}
        #: The same, of bindings only: all a bare ``name(...)`` read can call.
        self.binding_async: dict[str, tuple[str, int]] = {}
        self.trees: dict[str, Node] = {}
        root = Path(repo_dir)
        files = [root] if root.is_file() else source_files(root, _EXTENSIONS, _SKIP_DIRS)
        sources = []
        for path in files:
            tree = self.tree(str(path))
            if tree is None:
                continue
            if _TEST_FILE.search(str(path).replace("\\", "/")):
                continue  # a test file's fakes say nothing about the production accessor
            sources.append((path, tree))
        for path, tree in sources:
            support = root.is_dir() and _TEST_DIRECTORY.search(path.relative_to(root).as_posix()) is not None
            shown = str(path.relative_to(root)) if root.is_dir() else path.name

            def note(name: str, node: Node, binding: bool) -> None:
                at = (shown, node.start_point[0] + 1)
                self.possibly_async[name] = True
                self.async_at[name] = min(self.async_at.get(name, at), at)
                if binding:
                    self.binding_async[name] = min(self.binding_async.get(name, at), at)

            for node in ts_syntax._walk(tree):
                if node.type in _CALLABLE_KINDS or node.type in _VALUE_KINDS:
                    name = _name_of(node)
                    if name in accessors:
                        verdict = _possibly_async(
                            node,
                            lambda call, tree=tree, path=path: _audited_sync_call(call, tree, str(path), repo_dir),
                            lambda callee, tree=tree, path=path: _imported_test_double(callee, tree, str(path), repo_dir),
                            lambda value, tree=tree: _not_callable(value, tree),
                        )
                        if verdict and support and _a_fake(node, tree):
                            verdict = None  # a fake in test support code that only reads
                        if verdict:
                            note(name, node, node.type in _BINDING_KINDS)
                if node.type in _ASSIGNMENTS or node.type == "call_expression":
                    # A write is a declaration too (`o.enabled = async …`,
                    # `exports.enabled = …`, `enabled = asyncFn`), judged on
                    # what it writes.
                    for name in accessors:
                        write = _write_of(node, name)
                        if write is None:
                            continue
                        kind, value = write
                        if node.type == "call_expression":
                            verdict = _descriptor_possibly_async(value, lambda v, tree=tree: _not_callable(v, tree))
                        else:
                            verdict = _written_value_possibly_async(
                                value,
                                lambda call, tree=tree, path=path: _audited_sync_call(call, tree, str(path), repo_dir),
                                lambda callee, tree=tree, path=path: _imported_test_double(callee, tree, str(path), repo_dir),
                                lambda v, tree=tree: _not_callable(v, tree),
                            )
                        if verdict:
                            note(name, node, kind == "binding")
                elif node.type == "shorthand_property_identifier_pattern" and _text(node) in accessors:
                    note(_text(node), node, True)  # `const { isEnabled } = api`
                elif node.type == "export_specifier":
                    alias = node.child_by_field_name("alias")
                    if alias is not None and _text(alias) in accessors:
                        note(_text(alias), node, True)

    def locally_declared(self, path: str, use: Node) -> bool:
        """Whether ``use``, a bare name, means a ``const``, ``let``, ``var``
        or ``function`` declaration of its own file: every binding of the
        name there is one (no import, parameter or assignment, which can
        hand it anything), and ``use`` sits inside the scope of one of them
        (:func:`_declared_scope`). Otherwise it may mean a global, which
        any module can set."""
        from flag_cleanup import ts_scope

        tree = self.tree(path)
        if tree is None:
            return False
        binders = ts_scope.bindings(tree, _text(use))
        if not binders or not all(
            b.parent is not None
            and b.parent.type in ("variable_declarator", "function_declaration")
            and b.parent.child_by_field_name("name") == b
            for b in binders
        ):
            return False
        return any(
            (scope := _declared_scope(b.parent)) is not None
            and scope.start_byte <= use.start_byte
            and use.end_byte <= scope.end_byte
            for b in binders
        )

    def has_with(self, path: str) -> bool:
        tree = self.tree(path)
        return tree is None or any(n.type == "with_statement" for n in ts_syntax._walk(tree))

    def tree(self, path: str) -> Node | None:
        key = str(Path(path).resolve())
        if key in self.trees:
            return self.trees[key]
        grammar = _EXTENSIONS.get(Path(path).suffix)
        tree = None
        if grammar is not None:
            try:
                tree = ts_syntax._parse(Path(path).read_text(encoding="utf-8"), grammar)
            except (OSError, UnicodeDecodeError):
                tree = None
        self.trees[key] = tree
        return tree

    # -- type resolution ---------------------------------------------------------

    def declarations_in(self, tree: Node, type_name: str) -> list[Node]:
        found = []
        for child in tree.named_children:
            node = child
            if node.type == "export_statement":
                node = node.child_by_field_name("declaration") or node
            if _name_of(node) != type_name:
                continue
            if node.type == "interface_declaration":
                found.append(node)
            elif node.type == "type_alias_declaration":
                value = node.child_by_field_name("value")
                if value is not None and value.type == "object_type":
                    found.append(node)
        return found

    def resolve_type(self, path: str, type_name: str) -> list[Node] | None:
        """The declarations of ``type_name`` as ``path`` itself sees it: a
        declaration of the file, or a named import from one relative file."""
        tree = self.tree(path)
        if tree is None:
            return None
        local = self.declarations_in(tree, type_name)
        if local:
            return local
        for child in tree.named_children:
            if child.type != "import_statement":
                continue
            for spec in ts_syntax._walk(child):
                if spec.type != "import_specifier":
                    continue
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                if alias is not None:
                    if _text(alias) == type_name:
                        return None  # a renamed import is not followed
                    continue
                if name is None or _text(name) != type_name:
                    continue
                source = child.child_by_field_name("source")
                target = self._relative_file(path, _text(source).strip("'\"")) if source is not None else None
                if target is None:
                    return None
                target_tree = self.tree(str(target))
                if target_tree is None:
                    return None
                found = self.declarations_in(target_tree, _text(name))
                return found or None
        return None

    @staticmethod
    def _relative_file(path: str, specifier: str) -> Path | None:
        """TypeScript's order: the file with .ts, .tsx, .d.ts, then a directory index."""
        if not specifier.startswith("."):
            return None
        base = (Path(path).parent / specifier).resolve()
        stem = Path(str(base)[: -len(base.suffix)]) if base.suffix in {".js", ".jsx", ".mjs", ".cjs"} else base
        candidates = [Path(str(stem) + suffix) for suffix in (".ts", ".tsx", ".d.ts")]
        candidates += [stem / f"index{suffix}" for suffix in (".ts", ".tsx", ".d.ts")]
        if base.suffix in {".ts", ".tsx", ".js", ".jsx"}:
            candidates.append(base)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _members(decl: Node, accessor: str) -> list[Node]:
        body = decl.child_by_field_name("body") or decl.child_by_field_name("value")
        if body is None:
            return []
        return [
            member
            for member in ts_syntax._walk(body)
            if (member.type in _CALLABLE_KINDS or member.type in _VALUE_KINDS)
            and _name_of(member) == accessor
            and _enclosing_body(member) == body
        ]

    def member_kind(self, declarations: list[Node], accessor: str) -> str | None:
        """What every member ``accessor`` of ``declarations`` returns, None if none declares it."""
        kinds: set[str] = set()
        for decl in declarations:
            mine = self._members(decl, accessor)
            signatures = [m for m in mine if m.type in _SIGNATURE_KINDS]
            for member in mine:
                if signatures and member.type == "method_definition":
                    continue  # the implementation of an overload set
                kind = _member_kind(member)
                if kind is not None:
                    kinds.add(kind)
        if not kinds:
            return None
        return kinds.pop() if len(kinds) == 1 else "other"

    def receiver_kind(self, path: str, call: Node, callee: Node, accessor: str) -> str | None:
        """``promise`` / ``sync`` / ``other``, or None when the receiver is not soundly resolved.

        Resolved only for a plain identifier that is a typed PARAMETER of an
        enclosing function, rebound nowhere inside that function, annotated with
        a plain reference to an interface or object type that is not a type
        parameter."""
        if callee.type != "member_expression":
            return None
        receiver = callee.child_by_field_name("object")
        if receiver is None or receiver.type != "identifier":
            return None
        type_name = _parameter_type(call, _text(receiver))
        if type_name is None:
            return None
        declarations = self.resolve_type(path, type_name)
        if not declarations:
            return None
        return self.member_kind(declarations, accessor)


def _enclosing_body(member: Node) -> Node | None:
    current = member.parent
    while current is not None:
        if current.type in {"class_body", "object_type", "interface_body"}:
            return current
        current = current.parent
    return None


def _enclosing_function(node: Node) -> Node | None:
    current = node.parent
    while current is not None:
        if current.type in _FUNCTION_NODES:
            return current
        current = current.parent
    return None


_TRANSPARENT = frozenset({"parenthesized_expression", "non_null_expression", "as_expression", "satisfies_expression", "type_assertion"})
_PROMISE_NAMES = re.compile(r"Promise|Awaitable|Thenable")


def _unwrap(call: Node) -> tuple[Node, bool]:
    """The outermost node that is still just the read, and whether a cast says Promise."""
    node, cast = call, False
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        node = node.parent
        if node.type in {"as_expression", "satisfies_expression"} and node.named_children:
            cast = cast or _text(node.named_children[-1]).strip() != "boolean"
        elif node.type == "type_assertion" and node.named_children:
            cast = cast or _text(node.named_children[0]).replace(" ", "") != "<boolean>"
    return node, cast


#: Members a Promise has. A property read off the result that is none of these is
#: not Promise evidence, it is an OBJECT use: a hook returning `{ status }`.
_PROMISE_MEMBERS = frozenset({"then", "catch", "finally"})

PROMISE_USE = "promise"
OBJECT_USE = "object"

#: A refusal reason that starts with this is about an object-shaped result, not a
#: Promise; :func:`refusal` words its headline from that.
OBJECT_RESULT = "its result is used as an object"


def _use_site_kind(top: Node) -> str | None:
    """How the result is used when that rules a boolean fold out: ``"promise"`` (a
    Promise member, a ``Promise.all`` argument, ``expect(..).resolves``), ``"object"``
    (any other property or index read off it), else ``None``."""
    parent = top.parent
    if parent is None:
        return None
    if parent.type == "member_expression" and parent.child_by_field_name("object") == top:
        prop = parent.child_by_field_name("property")
        return PROMISE_USE if prop is not None and _text(prop) in _PROMISE_MEMBERS else OBJECT_USE
    if parent.type == "subscript_expression" and parent.child_by_field_name("object") == top:
        return OBJECT_USE
    holder = parent.parent if parent.type == "array" else parent
    if holder is not None and holder.type == "arguments" and holder.parent is not None:
        call = holder.parent
        fn = call.child_by_field_name("function")
        if fn is not None:
            text = _text(fn)
            if re.fullmatch(r"Promise\s*\.\s*(all|allSettled|race|any)", text):
                return PROMISE_USE
            outer = call.parent
            if text == "expect" and outer is not None and outer.type == "member_expression":
                prop = outer.child_by_field_name("property")
                if prop is not None and _text(prop) in {"resolves", "rejects"}:
                    return PROMISE_USE
    return None


def _use_site_promise(top: Node) -> bool:
    """The result is used as a Promise or as an object (see :func:`_use_site_kind`)."""
    return _use_site_kind(top) is not None


def _boolean_consumed(node: Node) -> bool:
    """Whether the read's value is only tested: a condition, a ternary condition, an
    operand of ``!``, or an operand of ``&&``/``||``/``??`` that is itself tested.
    A Promise there is always truthy, so there is no type to preserve."""
    while True:
        parent = node.parent
        if parent is None:
            return False
        kind = parent.type
        if kind == "parenthesized_expression":
            node = parent
        elif kind in {"if_statement", "while_statement", "do_statement", "for_statement"}:
            return parent.child_by_field_name("condition") == node
        elif kind == "ternary_expression":
            return parent.child_by_field_name("condition") == node
        elif kind == "unary_expression":
            operator = parent.child_by_field_name("operator")
            return operator is not None and _text(operator) == "!"
        elif kind == "binary_expression" and _text(parent.child_by_field_name("operator")) in {"&&", "||", "??"}:
            node = parent
        else:
            return False


def _bound_read_kind(top: Node) -> str | None:
    """The read is bound (``const p = <read>``) and ``p`` is later used in the same
    scope in a way no boolean survives: ``"promise"`` for ``await``, a Promise member,
    a ``Promise.*`` argument or a return from a function annotated Promise,
    ``"object"`` for a property read, a destructuring, or an index. A Promise use
    wins when both occur."""
    parent = top.parent
    if parent is None or parent.type != "variable_declarator" or parent.child_by_field_name("value") != top:
        return None
    target = parent.child_by_field_name("name")
    if target is None or target.type != "identifier":
        return OBJECT_USE  # a destructured binding of the result
    name = _text(target)
    scope = parent.parent.parent if parent.parent is not None else None
    if scope is None:
        return None
    found = None
    for node in ts_syntax._walk(scope):
        if node.type != "identifier" or _text(node) != name or node == target:
            continue
        kind = _use_site_kind(node)
        if node.parent is not None and node.parent.type == "await_expression":
            kind = PROMISE_USE
        holder = node.parent
        if holder is not None and holder.type == "return_statement":
            function = _enclosing_function(holder)
            if function is not None and return_kind(function.child_by_field_name("return_type")) == "promise":
                kind = PROMISE_USE
        if kind == PROMISE_USE:
            return PROMISE_USE
        found = found or kind
    return found


def _returned(top: Node) -> tuple[Node | None, bool]:
    """``(function, composite)``: the function whose result this expression is,
    and whether it is only an arm or operand of what is returned."""
    node, composite = top, False
    while True:
        parent = node.parent
        if parent is None:
            return None, False
        if parent.type in _TRANSPARENT:
            node = parent
        elif parent.type == "ternary_expression" and parent.child_by_field_name("condition") != node:
            composite, node = True, parent
        elif parent.type == "binary_expression" and _text(parent.child_by_field_name("operator")) in {"&&", "||", "??"}:
            composite, node = True, parent
        elif parent.type == "return_statement":
            return _enclosing_function(parent), composite
        elif parent.type == "arrow_function" and parent.child_by_field_name("body") == node:
            return parent, composite
        else:
            return None, False


def _annotation_type_name(annotation: Node | None) -> str | None:
    if annotation is None:
        return None
    named = list(annotation.named_children)
    if len(named) != 1:
        return None
    node = named[0]
    if node.type == "type_identifier":
        return _text(node)
    if node.type == "generic_type":
        name = node.child_by_field_name("name")
        return _text(name) if name is not None and name.type == "type_identifier" else None
    return None


def _binds(pattern: Node | None, name: str) -> bool:
    return pattern is not None and any(
        n.type in {"identifier", "shorthand_property_identifier_pattern"} and _text(n) == name
        for n in ts_syntax._walk(pattern)
    )


def _rebinds(body: Node, name: str) -> bool:
    """Whether ``name`` is bound, assigned or declared anywhere inside ``body``."""
    for node in ts_syntax._walk(body):
        kind = node.type
        if kind == "variable_declarator" and _binds(node.child_by_field_name("name"), name):
            return True
        if kind in {"required_parameter", "optional_parameter", "rest_parameter"} and _binds(node.child_by_field_name("pattern"), name):
            return True
        if kind == "arrow_function":
            single = node.child_by_field_name("parameter")
            if single is not None and _binds(single, name):
                return True
        if kind == "for_in_statement" and _binds(node.child_by_field_name("left"), name):
            return True
        if kind == "catch_clause" and _binds(node.child_by_field_name("parameter"), name):
            return True
        if kind in {"function_declaration", "function_expression", "function", "class_declaration", "class", "generator_function_declaration"}:
            own = node.child_by_field_name("name")
            if own is not None and _text(own) == name:
                return True
        if kind in {"assignment_expression", "augmented_assignment_expression"} and _binds(node.child_by_field_name("left"), name):
            return True
        if kind == "import_statement" and _binds(node, name):
            return True
    return False


def _type_parameters(node: Node) -> set[str]:
    declared = node.child_by_field_name("type_parameters")
    if declared is None:
        return set()
    return {_text(n) for n in ts_syntax._walk(declared) if n.type == "type_identifier"}


def _parameter_type(call: Node, name: str) -> str | None:
    """The plain type name annotating ``name`` when it is a typed parameter of an
    enclosing function and nothing inside that function rebinds it."""
    generics: set[str] = set()
    node = call.parent
    while node is not None:
        generics |= _type_parameters(node)
        if node.type in _FUNCTION_NODES:
            params = node.child_by_field_name("parameters")
            single = node.child_by_field_name("parameter")
            candidates = list(params.named_children) if params is not None else []
            if single is not None:
                candidates.append(single)
            for param in candidates:
                pattern = param.child_by_field_name("pattern") if param.type in {"required_parameter", "optional_parameter"} else param
                if pattern is None or not _binds(pattern, name):
                    continue
                if pattern.type != "identifier" or param.type != "required_parameter":
                    return None
                annotation = param.child_by_field_name("type")
                body = node.child_by_field_name("body")
                if annotation is None or (body is not None and _rebinds(body, name)):
                    return None
                named = list(annotation.named_children)
                if len(named) != 1 or named[0].type != "type_identifier":
                    return None
                type_name = _text(named[0])
                outer = node.parent
                while outer is not None:
                    generics |= _type_parameters(outer)
                    outer = outer.parent
                return None if type_name in generics else type_name
        node = node.parent
    return None


def _first_argument(call: Node) -> tuple[str | None, bool]:
    """``(the key literal, whether the argument could name some key)``."""
    args = call.child_by_field_name("arguments")
    if args is None or not args.named_children:
        return None, False
    first = args.named_children[0]
    if first.type == "string":
        if first.child_count == 3 and first.children[1].type == "string_fragment":
            return _text(first.children[1]), False
        return None, False
    return None, first.type in {"identifier", "member_expression", "template_string", "subscript_expression"}


def _is_optional(call: Node, callee: Node) -> bool:
    if any(c.type == "optional_chain" for c in callee.children):
        return True
    return any(c.type == "?." for c in call.children)


_OBJECT_ADVICE = (
    ": an accessor or hook that returns an object, such as a tri-state `{ status }`, is not folded "
    "to a boolean because nothing says which of its states the removed flag means"
)


def decide(repo: _Repo, path: str, call: Node, callee: Node, accessor: str) -> tuple[str, str]:
    """``(decision, why)``; ``why`` says what stopped a LEAVE."""
    top, promise_cast = _unwrap(call)
    if _boolean_consumed(top):
        return LITERAL, ""
    if promise_cast:
        return LEAVE, "it is cast to a type other than boolean"
    use = _use_site_kind(top)
    if use == PROMISE_USE:
        return LEAVE, "its result is used as a Promise"
    if use == OBJECT_USE:
        return LEAVE, f"{OBJECT_RESULT} (a property is read from it), not as a boolean{_OBJECT_ADVICE}"
    bound = _bound_read_kind(top)
    if bound == PROMISE_USE:
        return LEAVE, "its result is bound to a name that is later used as a Promise"
    if bound == OBJECT_USE:
        return LEAVE, (
            f"{OBJECT_RESULT} once bound to a name (a property is read from it, or it is destructured), "
            f"not as a boolean{_OBJECT_ADVICE}"
        )
    function, composite = _returned(top)
    if function is not None:
        if _has_async_token(function):
            return LITERAL, ""
        annotated = return_kind(function.child_by_field_name("return_type"))
        if composite:
            if annotated == "sync":
                return LITERAL, ""
            return LEAVE, "it is one arm of a returned expression in a function that is neither async nor annotated boolean"
        if annotated == "promise":
            return PROMISE, ""
        if annotated == "sync":
            return LITERAL, ""
        if annotated == "other":
            return LEAVE, "it is returned from a function whose return type is neither Promise nor boolean"
    kind = repo.receiver_kind(path, call, callee, accessor)
    if kind == "promise":
        if function is not None:
            return PROMISE, ""
        return LEAVE, "the receiver's declared type makes it return a Promise and it is not returned"
    if kind == "sync":
        return LITERAL, ""
    if kind == "other":
        return LEAVE, "the receiver's declared type gives it no explicit boolean or Promise return type"
    # A bare call of a name the file itself declares reaches only a binding
    # of the name, unless a `with` makes any object's member one. An
    # imported name is whatever the module exports, members of an exported
    # object (`module.exports = { enabled }`) included.
    bare = callee.type == "identifier" and not repo.has_with(path) and repo.locally_declared(path, callee)
    at = repo.binding_async.get(accessor) if bare else repo.async_at.get(accessor)
    if at is not None:
        return LEAVE, f"a declaration of {accessor} in the repository is, or could be, async ({at[0]}:{at[1]})"
    return LITERAL, ""


def refusal(sites: list[Site]) -> tuple[str, list[str]] | None:
    """The reason to refuse the whole flag, and the files, if any read must be left.

    A read that cannot be folded safely cannot stay while the others go: removal
    is flag-wide, so the flag is refused instead.
    """
    left = [site for site in sites if site.decision == LEAVE]
    if not left:
        return None
    first = left[0]
    count = f"({len(left)} such read{'s' if len(left) != 1 else ''})"
    if first.why.startswith(OBJECT_RESULT):
        reason = f"a read of {first.accessor} whose result is an object, not a boolean: {first.why} {count}"
    else:
        reason = f"an un-awaited read of {first.accessor} whose result may be a Promise: {first.why} {count}"
    return reason, sorted({site.path for site in left})


def plan(repo_dir: str, paths: list[str], flag_key: str, accessors: tuple[str, ...], language: str) -> list[Site]:
    """Every un-awaited accessor read in ``paths`` that is, or may be, of ``flag_key``, with its decision."""
    if language not in ("ts", "tsx", "js") or not accessors:
        return []
    names = frozenset(accessors)
    repo = _Repo(repo_dir, names)
    sites: list[Site] = []
    for path in dict.fromkeys(paths):
        if _EXTENSIONS.get(Path(path).suffix) is None:
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(n in source for n in names):
            continue
        tree = repo.tree(path)
        if tree is None:
            continue
        for node in ts_syntax._walk(tree):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            if callee.type == "member_expression":
                prop = callee.child_by_field_name("property")
                name = _text(prop) if prop is not None else None
            elif callee.type == "identifier":
                name = _text(callee)
            else:
                continue
            if name not in names or _is_optional(node, callee):
                continue
            literal, could_name_key = _first_argument(node)
            if literal != flag_key and not could_name_key:
                continue
            top, _cast = _unwrap(node)
            if top.parent is not None and top.parent.type == "await_expression":
                continue
            decision, why = decide(repo, path, node, callee, name)
            sites.append(
                Site(path, name, (callee.start_byte, callee.end_byte), (node.start_byte, node.end_byte),
                     decision, literal == flag_key, why)
            )
    return sites


def managed_accessors(sites: list[Site]) -> frozenset[str]:
    """Accessors with at least one read that is not a plain literal fold."""
    return frozenset(site.accessor for site in sites if site.decision != LITERAL)


def marks(sites: list[Site], managed: frozenset[str], path: str) -> list[kotlin_sentinel.Mark]:
    """The sentinel marks for ``path``: literal reads by callee, promise reads whole."""
    data = Path(path).read_bytes()
    out = []
    for site in sites:
        if site.path != path or site.accessor not in managed or not site.keyed:
            continue
        if site.decision == LITERAL:
            out.append(kotlin_sentinel.mark(data, *site.callee))
        elif site.decision == PROMISE:
            out.append(kotlin_sentinel.mark(data, *site.call, f"({PROMISE_TAG})"))
    return out
