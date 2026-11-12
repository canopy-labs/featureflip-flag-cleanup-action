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

import json
import os
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import kotlin_sentinel, rule_synthesis, ts_syntax

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


def _may_return_promise(node: Node) -> bool:
    """Whether one declared member's annotation allows a Promise: ``async``, or a
    return type that is not a primitive keyword/literal type or a union of them
    (``boolean | Promise<boolean>``, ``MaybePromise<boolean>``, an alias that
    cannot be expanded here). No annotation says nothing."""
    if node.type in _CALLABLE_KINDS:
        if _has_async_token(node):
            return True
        annotation = node.child_by_field_name("return_type")
    elif node.type in _VALUE_KINDS:
        value = node.child_by_field_name("value")
        if value is not None and value.type in _FUNCTION_VALUES:
            if _has_async_token(value):
                return True
            annotation = value.child_by_field_name("return_type")
        else:
            ftype = _function_type(node.child_by_field_name("type"))
            annotation = ftype.child_by_field_name("return_type") if ftype is not None else None
    else:
        return False
    return annotation is not None and not _primitive_return(annotation)


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
    #: The key is a property of an object argument (``getFlag{key}``). The
    #: awaited reads have rules of their own, so these sites are un-awaited ones
    #: and a LITERAL decision is marked too: no bare-call rule exists to fold it.
    prop: bool = False


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
        #: Per accessor name, what each declaration in the repository says it returns.
        self.declared_kinds: dict[str, set[str]] = defaultdict(set)
        #: The same, of plain functions and function-valued bindings only (a bare call reaches no method).
        self.declared_function_kinds: dict[str, set[str]] = defaultdict(set)
        #: The same, per file: (resolved path, name) -> kinds.
        self.function_kinds_in: dict[tuple[str, str], set[str]] = defaultdict(set)
        root = Path(repo_dir)
        self.root = root
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
                        kind = "async" if verdict else (_member_kind(node) or "other")
                        self.declared_kinds[name].add(kind)
                        if node.type in _BINDING_KINDS:
                            self.declared_function_kinds[name].add(kind)
                            self.function_kinds_in[(str(Path(path).resolve()), name)].add(kind)
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

    def receiver_may_return_promise(self, path: str, call: Node, callee: Node, accessor: str) -> bool:
        """Whether any member ``accessor`` of the receiver's declared type, overloads
        and unions included, may return a Promise (see :func:`_may_return_promise`)."""
        if callee.type != "member_expression":
            return False
        receiver = callee.child_by_field_name("object")
        if receiver is None or receiver.type != "identifier":
            return False
        type_name = _parameter_type(call, _text(receiver))
        declarations = self.resolve_type(path, type_name) if type_name is not None else []
        return any(
            _may_return_promise(member) for decl in declarations for member in self._members(decl, accessor)
        )

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


def _property_argument(call: Node, prop: str) -> tuple[str | None, bool]:
    """:func:`_first_argument` for an accessor whose key is the ``prop`` property of its object argument.

    A read whose object the property-key rules decline (a spread, a computed
    name, code that could run) is none of this pass's: it is not a read it can
    fold, so it is left as it always was.
    """
    if ts_syntax.declined_by_object_key_rules(call, prop, awaited=False):
        return None, False
    args = call.child_by_field_name("arguments")
    obj = args.named_children[0] if args is not None and args.named_children else None
    if obj is None or obj.type != "object":
        return None, False
    for pair in obj.named_children:
        key = pair.child_by_field_name("key") if pair.type == "pair" else None
        value = pair.child_by_field_name("value") if pair.type == "pair" else None
        if key is None or value is None or _text(key) != prop:
            continue
        if value.type == "string":
            if value.child_count == 3 and value.children[1].type == "string_fragment":
                return _text(value.children[1]), False
            return None, False
        return None, value.type in {"identifier", "member_expression", "template_string", "subscript_expression"}
    return None, False


def _is_optional(call: Node, callee: Node) -> bool:
    if any(c.type == "optional_chain" for c in callee.children):
        return True
    return any(c.type == "?." for c in call.children)


_OBJECT_ADVICE = (
    ": an accessor or hook that returns an object, such as a tri-state `{ status }`, is not folded "
    "to a boolean because nothing says which of its states the removed flag means"
)


_HOOK_NAME = re.compile(r"\Ause[A-Z]")


def _sync_evidence(repo: _Repo, path: str, call: Node, callee: Node, accessor: str) -> bool:
    """Positive evidence that a property-keyed read returns a boolean: a
    React-hook-shaped callee (``useFlag``), a receiver whose declared type gives
    it an explicit ``boolean`` return, or a read returned from a function
    annotated ``boolean``."""
    name = callee.child_by_field_name("property") if callee.type == "member_expression" else callee
    if name is not None and _HOOK_NAME.match(_text(name)):
        return True
    if repo.receiver_kind(path, call, callee, accessor) == "sync":
        return True
    top, _cast = _unwrap(call)
    function, _composite = _returned(top)
    return function is not None and return_kind(function.child_by_field_name("return_type")) == "sync"


def _function_name(function: Node) -> Node | None:
    """The name node of ``function`` as a caller would spell it, else ``None``."""
    own = function.child_by_field_name("name")
    if own is not None and function.type in {"function_declaration", "generator_function_declaration", "method_definition"}:
        return own
    holder = function.parent
    if holder is not None and holder.type == "variable_declarator" and holder.child_by_field_name("value") == function:
        name = holder.child_by_field_name("name")
        return name if name is not None and name.type == "identifier" else None
    if holder is not None and holder.type == "pair" and holder.child_by_field_name("value") == function:
        key = holder.child_by_field_name("key")
        return key if key is not None and key.type == "property_identifier" else None
    return None


def _returned_function(top: Node) -> Node | None:
    """The function whose result the read is: returned directly, or bound to a
    local that the function returns (``const on = read; return on;``)."""
    function, _composite = _returned(top)
    if function is not None:
        return function
    parent = top.parent
    if parent is None or parent.type != "variable_declarator" or parent.child_by_field_name("value") != top:
        return None
    target = parent.child_by_field_name("name")
    if target is None or target.type != "identifier":
        return None
    function = _enclosing_function(parent)
    if function is None:
        return None
    for node in ts_syntax._walk(function):
        if node.type == "identifier" and _text(node) == _text(target) and node != target:
            returned, _c = _returned(node)
            if returned == function:
                return function
    return None


_RESOLVE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")


def _names_module(importer: str, specifier: str, target: str) -> bool:
    """Whether the relative ``specifier`` of a file at ``importer`` names the module at ``target``."""
    if not specifier.startswith("."):
        return False
    base = (Path(importer).parent / specifier).resolve()
    goal = Path(target).resolve()
    stems = [base]
    if base.suffix in {".js", ".mjs", ".cjs", ".jsx"}:
        stems += [base.with_suffix(suffix) for suffix in _RESOLVE_SUFFIXES]
    options = [*stems, *(Path(f"{base}{suffix}") for suffix in _RESOLVE_SUFFIXES)]
    options += [base / f"index{suffix}" for suffix in _RESOLVE_SUFFIXES]
    return goal in options


def _module_candidates(importer: str, specifier: str) -> list[Path]:
    base = (Path(importer).parent / specifier).resolve()
    stems = [base]
    if base.suffix in {".js", ".mjs", ".cjs", ".jsx"}:
        stems += [base.with_suffix(suffix) for suffix in _RESOLVE_SUFFIXES]
    options = [*stems, *(Path(f"{base}{suffix}") for suffix in _RESOLVE_SUFFIXES)]
    options += [base / f"index{suffix}" for suffix in _RESOLVE_SUFFIXES]
    return options


def _resolves_in_repo(repo: _Repo, importer: str, specifier: str) -> bool:
    """Whether the relative ``specifier`` names a TS/JS source this pass has parsed."""
    return any(str(c) in repo.trees for c in _module_candidates(importer, specifier))


def _workspace(repo: _Repo) -> tuple[dict[str, Path], tuple[str, ...]]:
    """``(workspace package name -> its directory, path-alias prefixes)``, read once."""
    cached = getattr(repo, "_workspace_facts", None)
    if cached is not None:
        return cached
    packages: dict[str, Path] = {}
    prefixes: list[str] = ["@/", "~/", "#"]
    root = getattr(repo, "root", None)
    if root is not None and Path(root).is_dir():
        from flag_cleanup.piranha_runner import _SKIP_DIRS

        for current, directories, files in os.walk(root):
            directories[:] = [d for d in directories if d not in _SKIP_DIRS]
            for file in files:
                if file == "package.json":
                    try:
                        name = json.loads((Path(current) / file).read_text(encoding="utf-8")).get("name")
                    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
                        continue
                    if isinstance(name, str):
                        packages[name] = Path(current).resolve()
                elif file in {"tsconfig.json", "jsconfig.json"} or (file.startswith("tsconfig.") and file.endswith(".json")):
                    try:
                        text = (Path(current) / file).read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    prefixes += [m.rstrip("*") for m in re.findall(r'"([^"]+)"\s*:\s*\[', text) if "*" in m or m.startswith(("@", "~", "#", "$"))]
    facts = (packages, tuple(p for p in prefixes if p))
    repo._workspace_facts = facts  # type: ignore[attr-defined]
    return facts


def _may_name_module(
    repo: _Repo, importer: str, specifier: str, target: str, depth: int = 0, seen: frozenset[str] = frozenset()
) -> bool:
    """Whether ``specifier`` (in ``importer``) may be the module at ``target``:
    it resolves to it, or to a barrel that re-exports it (``export * from``,
    ``export { … } from``, ``export * as``, followed to ``_BARREL_DEPTH`` hops,
    cycle-safe, an unresolvable hop counting as naming it), or it is a relative
    specifier that resolves to nothing parsed, or a bare specifier naming a
    workspace package that holds the target or a configured path alias (neither
    can be resolved here)."""
    if not specifier:
        return False
    if _names_module(importer, specifier, target):
        return True
    if specifier.startswith("."):
        resolved = next((str(c) for c in _module_candidates(importer, specifier) if str(c) in repo.trees), None)
        if resolved is None:
            return True
        tree = repo.trees.get(resolved)
        if tree is None or depth >= _BARREL_DEPTH or resolved in seen:
            return tree is None or depth >= _BARREL_DEPTH
        for node in tree.children:
            if node.type == "export_statement" and node.child_by_field_name("source") is not None:
                hop = _string_value(node.child_by_field_name("source"))
                if hop and _may_name_module(repo, resolved, hop, target, depth + 1, seen | {resolved}):
                    return True
        return False
    packages, prefixes = _workspace(repo)
    if any(specifier.startswith(prefix) for prefix in prefixes):
        return True
    parts = specifier.split("/")
    name = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
    directory = packages.get(name)
    return directory is not None and directory in Path(target).resolve().parents


def _string_value(node: Node | None) -> str | None:
    if node is None or node.type != "string" or node.child_count != 3:
        return None
    return _text(node.children[1])


def _default_names(repo: _Repo, path: str, name: str) -> set[str]:
    """The local names other files give ``name`` when they import it as the
    module's default (``export default isRbac``, ``module.exports = isRbac``)."""
    tree = repo.trees.get(str(Path(path).resolve())) or repo.trees.get(path)
    if tree is None:
        return set()
    default = False
    commonjs = False
    for node in ts_syntax._walk(tree):
        if node.type == "export_statement" and any(c.type == "default" for c in node.children):
            declared = node.child_by_field_name("declaration") or node.child_by_field_name("value")
            if declared is not None and (
                (declared.type == "identifier" and _text(declared) == name)
                or (declared.type in {"function_declaration", "generator_function_declaration"}
                    and declared.child_by_field_name("name") is not None
                    and _text(declared.child_by_field_name("name")) == name)
            ):
                default = True
        elif node.type == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and _text(left) == "module.exports" and right is not None and right.type == "identifier" and _text(right) == name:
                commonjs = True
    if not default and not commonjs:
        return set()
    found: set[str] = set()
    for other_path, other in repo.trees.items():
        if other is None:
            continue
        for node in ts_syntax._walk(other):
            if default and node.type == "import_clause":
                local = next((c for c in node.children if c.type == "identifier"), None)
                source = node.parent.child_by_field_name("source") if node.parent is not None else None
                if local is not None and _names_module(other_path, _string_value(source) or "", path):
                    found.add(_text(local))
            elif node.type == "variable_declarator":
                target, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if target is None or target.type != "identifier" or value is None or value.type != "call_expression":
                    continue
                callee, args = value.child_by_field_name("function"), value.child_by_field_name("arguments")
                if callee is not None and _text(callee) == "require" and args is not None and args.named_children:
                    if _names_module(other_path, _string_value(args.named_children[0]) or "", path):
                        found.add(_text(target))
    return found


def _alias_names(repo: _Repo, path: str, name: str) -> tuple[set[str], str | None]:
    """Every name the function is reached under: itself, a renamed import or
    re-export, a destructured rename, a plain alias, and a default import."""
    names = {name}
    frontier = [name]
    reason: str | None = None
    while frontier:
        current = frontier.pop()
        for other_path, tree in repo.trees.items():
            if tree is None:
                continue
            for node in ts_syntax._walk(tree):
                added = None
                if node.type in {"import_specifier", "export_specifier"}:
                    imported, alias = node.child_by_field_name("name"), node.child_by_field_name("alias")
                    if imported is not None and alias is not None and _text(imported) == current:
                        added = _text(alias)
                        if _text(alias) == "default":
                            reason = reason or "it is reached through a default export, whose importers can't all be found"
                        source = _module_source(node)
                        if source is not None and not _resolves_in_repo(repo, other_path, source):
                            reason = reason or (
                                f"it is re-exported or imported under another name through {source!r}, which can't be resolved"
                            )
                elif node.type == "pair_pattern":
                    key, value = node.child_by_field_name("key"), node.child_by_field_name("value")
                    if key is not None and value is not None and _text(key) == current and value.type == "identifier":
                        added = _text(value)
                elif node.type == "variable_declarator":
                    target, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                    if target is not None and target.type == "identifier" and value is not None:
                        if value.type == "identifier" and _text(value) == current:
                            added = _text(target)
                        elif value.type == "member_expression":
                            prop = value.child_by_field_name("property")
                            if prop is not None and _text(prop) == current:
                                added = _text(target)
                if added is not None and added not in names:
                    names.add(added)
                    frontier.append(added)
    return names, reason


#: Parents under which a mention of a name is a declaration or a binding, not a use.
_NAME_POSITIONS = frozenset(
    {
        "function_declaration", "generator_function_declaration", "method_definition", "method_signature",
        "property_signature", "abstract_method_signature", "function_signature", "public_field_definition",
        "import_specifier", "export_specifier", "import_clause", "namespace_import", "pair_pattern",
        "required_parameter", "optional_parameter", "type_query",
    }
)
_FOREIGN_SUFFIXES = (".vue", ".svelte", ".astro", ".mdx")
_MAX_DEPTH = 4
_BARREL_DEPTH = 5
_COMPARISON = frozenset({"===", "!==", "==", "!="})


def _boolean_only(top: Node) -> bool:
    """Whether the call's value is only tested or discarded: a statement, a
    condition (see :func:`_boolean_consumed`), a comparison with ``true`` or
    ``false``, or the left operand of ``&&`` in a JSX expression."""
    parent = top.parent
    if parent is None:
        return False
    if parent.type == "expression_statement":
        return True
    if _boolean_consumed(top):
        return True
    if parent.type == "binary_expression":
        operator = parent.child_by_field_name("operator")
        op = _text(operator) if operator is not None else ""
        other = parent.child_by_field_name("right") if parent.child_by_field_name("left") == top else parent.child_by_field_name("left")
        if op in _COMPARISON and other is not None and other.type in {"true", "false"}:
            return True
        if op == "&&" and parent.child_by_field_name("left") == top and parent.parent is not None and parent.parent.type == "jsx_expression":
            return True
    return False


def _bound_boolean_only(top: Node) -> bool:
    """``const on = call()`` where every other use of ``on`` in the scope is boolean-only."""
    parent = top.parent
    if parent is None or parent.type != "variable_declarator" or parent.child_by_field_name("value") != top:
        return False
    target = parent.child_by_field_name("name")
    scope = parent.parent.parent if parent.parent is not None else None
    if target is None or target.type != "identifier" or scope is None:
        return False
    for node in ts_syntax._walk(scope):
        if node.type == "identifier" and _text(node) == _text(target) and node != target and not _boolean_only(node):
            return False
    return True


def _name_mentions(repo: _Repo, names: set[str]) -> str | None:
    """A reason when a name is mentioned in a file this pass cannot read."""
    for path, tree in repo.trees.items():
        if tree is None:
            return f"{Path(path).name} could not be parsed, so its uses of the function cannot be checked"
        if tree.has_error and any(name in tree.text.decode("utf-8", "replace") for name in names):
            return f"{Path(path).name} does not parse cleanly and names the function"
    root = getattr(repo, "root", None)
    if root is not None and Path(root).is_dir():
        from flag_cleanup.piranha_runner import _SKIP_DIRS

        for current, directories, files in os.walk(root):
            directories[:] = [d for d in directories if d not in _SKIP_DIRS]
            for file in files:
                if file.endswith(_FOREIGN_SUFFIXES):
                    try:
                        text = (Path(current) / file).read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        return f"{file} could not be read, so its uses of the function cannot be checked"
                    if any(re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text) for name in names):
                        return f"{file} (a template this pass cannot read) names the function"
    return None


def _module_source(node: Node) -> str | None:
    """The module specifier of the import or export statement ``node`` sits in."""
    current: Node | None = node
    while current is not None and current.type not in {"import_statement", "export_statement"}:
        current = current.parent
    if current is None:
        return None
    return _string_value(current.child_by_field_name("source"))


def _whole_module_uses(tree: Node, binding: Node, local: str) -> str | None:
    """A reason when ``local`` (a name bound to a whole module) is used other than
    as ``local.member``."""
    for node in ts_syntax._walk(tree):
        if node.type != "identifier" or _text(node) != local or node == binding:
            continue
        parent = node.parent
        if parent is not None and parent.type in {"import_clause", "namespace_import", "import_require_clause", "namespace_export"}:
            continue
        if parent is not None and parent.type == "member_expression" and parent.child_by_field_name("object") == node:
            continue
        return f"line {node.start_point[0] + 1}"
    return None


def _unseen_importers(repo: _Repo, path: str, names: set[str]) -> str | None:
    """A reason when a use of the function could be out of sight: it is a default
    export in any form, or a file takes the declaring module as a whole (a
    namespace, ``require``, ``import()``, ``export * as``) and does anything but
    call a member of it. A specifier that cannot be resolved and may name the
    module counts as naming it."""
    for other_path, tree in repo.trees.items():
        if tree is None:
            continue
        shown = Path(other_path).name
        for node in ts_syntax._walk(tree):
            kind = node.type
            if kind == "export_statement" and any(c.type == "default" for c in node.children):
                declared = node.child_by_field_name("declaration") or node.child_by_field_name("value")
                named = declared.child_by_field_name("name") if declared is not None else None
                if declared is not None and (
                    (declared.type == "identifier" and _text(declared) in names)
                    or (named is not None and _text(named) in names)
                ):
                    return f"it is reached through a default export ({shown}:{node.start_point[0] + 1}), whose importers can't all be found"
            elif kind == "assignment_expression":
                left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                if left is not None and _text(left) == "module.exports" and right is not None and right.type == "identifier" and _text(right) in names:
                    return f"it is reached through a default export ({shown}:{node.start_point[0] + 1}), whose importers can't all be found"
            elif kind == "export_specifier":
                alias = node.child_by_field_name("alias")
                imported = node.child_by_field_name("name")
                if alias is not None and imported is not None and _text(alias) == "default" and _text(imported) in names:
                    return f"it is reached through a default export ({shown}:{node.start_point[0] + 1}), whose importers can't all be found"
            # a whole-module take
            binding: Node | None = None
            specifier: str | None = None
            if kind == "namespace_import":
                binding = next((c for c in node.children if c.type == "identifier"), None)
                specifier = _module_source(node)
            elif kind == "import_require_clause":
                binding = next((c for c in node.children if c.type == "identifier"), None)
                specifier = _string_value(next((c for c in node.children if c.type == "string"), None))
            elif kind == "namespace_export":
                specifier = _module_source(node)
                if _may_name_module(repo, other_path, specifier or "", path):
                    return f"the module is re-exported as a namespace at {shown}:{node.start_point[0] + 1}"
                continue
            elif kind == "call_expression":
                callee = node.child_by_field_name("function")
                args = node.child_by_field_name("arguments")
                if callee is None or args is None or not args.named_children:
                    continue
                if callee.type == "import" or (callee.type == "identifier" and _text(callee) == "require"):
                    specifier = _string_value(args.named_children[0])
                    if specifier is None and callee.type == "import":
                        return f"a dynamic import with a computed specifier at {shown}:{node.start_point[0] + 1}"
                    # A computed `require(...)` may be the declaring module.
                    if specifier is not None and not _may_name_module(repo, other_path, specifier, path):
                        continue
                    holder = node.parent
                    if callee.type == "import" or holder is not None and holder.type == "await_expression" and callee.type == "import":
                        return f"the module is imported dynamically at {shown}:{node.start_point[0] + 1}"
                    if holder is not None and holder.type == "variable_declarator" and holder.child_by_field_name("value") == node:
                        target = holder.child_by_field_name("name")
                        if target is not None and target.type == "identifier":
                            binding = target
                        elif target is not None and any(c.type == "rest_pattern" for c in target.children):
                            return f"the module is destructured with a rest element at {shown}:{node.start_point[0] + 1}"
                        else:
                            continue
                    elif holder is not None and holder.type == "member_expression" and holder.child_by_field_name("object") == node:
                        continue
                    else:
                        return f"the module is required without a name at {shown}:{node.start_point[0] + 1}"
                    specifier = None
                else:
                    continue
            if binding is None:
                continue
            if specifier is not None and not _may_name_module(repo, other_path, specifier, path):
                continue
            bad = _whole_module_uses(tree, binding, _text(binding))
            if bad is not None:
                return f"the module is bound to {_text(binding)!r} in {shown} and used other than as a member call ({shown}, {bad})"
    return None


def _imported_as(tree: Node, local: str) -> tuple[str, str] | None:
    """``(module specifier, imported name)`` when ``local`` is bound by a named import."""
    for node in ts_syntax._walk(tree):
        if node.type != "import_specifier":
            continue
        imported, alias = node.child_by_field_name("name"), node.child_by_field_name("alias")
        bound = alias or imported
        if bound is not None and imported is not None and _text(bound) == local:
            source = _module_source(node)
            return (source, _text(imported)) if source is not None else None
    return None


def _declared_boolean(repo: _Repo, path: str, call: Node, callee: Node, accessor: str) -> bool:
    """Whether THIS call reaches a declaration annotated ``boolean``. A method call
    needs its receiver to resolve to one (``receiver_kind == "sync"``). A bare call
    needs the callee identifier to resolve to one: a function of that name declared
    in the same file, or a named import through a relative specifier that resolves
    to a file declaring it. A package import, or a name declared elsewhere in the
    repository and not imported, never qualifies."""
    if callee.type == "member_expression":
        return repo.receiver_kind(path, call, callee, accessor) == "sync"
    if callee.type != "identifier":
        return False
    here = str(Path(path).resolve())
    if repo.function_kinds_in.get((here, accessor)) == {"sync"}:
        return True
    tree = repo.trees.get(here)
    imported = _imported_as(tree, accessor) if tree is not None else None
    if imported is None or not imported[0].startswith("."):
        return False
    for candidate in _module_candidates(path, imported[0]):
        if str(candidate) in repo.trees:
            return repo.function_kinds_in.get((str(candidate), imported[1])) == {"sync"}
    return False


def _classic_script(repo: _Repo, path: str, function: Node) -> str | None:
    """A reason when ``function`` is a top-level function of a file with no
    import, export, ``require`` or ``module.exports``: a classic script, whose
    top-level functions are properties of the global object, reachable by a
    computed key."""
    tree = repo.trees.get(str(Path(path).resolve()))
    if tree is None or function.parent is None or function.parent.type not in {"program", "lexical_declaration", "variable_declarator"}:
        return None
    if function.parent.type != "program" and (function.parent.parent is None or function.parent.parent.parent is None or function.parent.parent.parent.type != "program"):
        return None
    if any(child.type in {"import_statement", "export_statement"} for child in tree.children):
        return None
    text = tree.text.decode("utf-8", "replace")
    if re.search(r"\brequire\s*\(|\bmodule\.exports\b|\bexports\.", text):
        return None
    return "it is a top-level function of a classic script, which is a global reachable by a computed key"


def _plain_function(function: Node) -> str | None:
    """A reason when ``function`` is not a plain named function: a declaration, or
    a ``const`` (or never-reassigned ``let``/``var``) bound function or arrow.
    Methods, object property functions and class fields can be reached by a
    computed key or by dispatch."""
    if function.type in {"function_declaration", "generator_function_declaration"}:
        return None
    holder = function.parent
    if function.type in {"arrow_function", "function_expression", "function"} and holder is not None and holder.type == "variable_declarator":
        declaration = holder.parent
        name = holder.child_by_field_name("name")
        if name is not None and name.type == "identifier" and declaration is not None:
            if declaration.type == "lexical_declaration" and _text(declaration.children[0]) == "const":
                return None
            return None if not _reassigned(function, _text(name)) else "it is returned from a function bound to a variable that is reassigned"
    if function.type in {"method_definition"} or (holder is not None and holder.type in {"pair", "public_field_definition"}):
        return "a method can be reached by a computed key or dispatch that cannot be followed"
    return "it is returned from a function with no name a caller can be found by (anonymous, a default export or a class field)"


def _reassigned(function: Node, name: str) -> bool:
    root = function
    while root.parent is not None:
        root = root.parent
    return any(
        node.type in {"assignment_expression", "augmented_assignment_expression"}
        and node.child_by_field_name("left") is not None
        and _text(node.child_by_field_name("left")) == name
        for node in ts_syntax._walk(root)
    )


def _quoted_names(repo: _Repo, names: set[str]) -> str | None:
    """A reason when a name appears in a string literal or template chunk anywhere
    (``obj['isRbac']``, ``this[name]`` dispatch tables)."""
    pattern = re.compile("|".join(rf"(?<![\w$]){re.escape(n)}(?![\w$])" for n in sorted(names)))
    for path, tree in repo.trees.items():
        if tree is None:
            continue
        for node in ts_syntax._walk(tree):
            if node.type == "string_fragment" and pattern.search(_text(node)):
                return f"its name appears in a string at {Path(path).name}:{node.start_point[0] + 1}, so it may be reached by a computed key"
    return None


def _result_only_tested(
    repo: _Repo, path: str, function: Node, depth: int = 0, seen: frozenset[int] = frozenset()
) -> str | None:
    """Why the fold of a read returned from ``function`` is not proven safe, or ``None``.

    A bare literal is proven only when ``function`` has a name and EVERY
    reference to that name (and its aliases) in the repository is a call whose
    result is only tested or discarded, or is returned from a function for
    which the same holds (to ``_MAX_DEPTH`` levels). Anything else (passed on,
    stored, chained on, awaited, an unreadable file, a template) is a reason."""
    if function.id in seen:
        return None
    if depth > _MAX_DEPTH:
        return "the chain of functions returning the read is too deep to check"
    name_node = _function_name(function)
    if name_node is None:
        return "it is returned from a function with no name a caller can be found by (anonymous, a default export or a class field)"
    plain = _plain_function(function)
    if plain is not None:
        return plain
    script = _classic_script(repo, path, function)
    if script is not None:
        return script
    names, hop = _alias_names(repo, path, _text(name_node))
    quoted = _quoted_names(repo, names)
    if quoted is not None:
        return quoted
    if hop is not None:
        return hop
    unreadable = _name_mentions(repo, names)
    if unreadable is not None:
        return unreadable
    unseen = _unseen_importers(repo, path, names)
    if unseen is not None:
        return unseen
    for other_path, tree in repo.trees.items():
        for node in ts_syntax._walk(tree):
            if node.type not in {"identifier", "property_identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"}:
                continue
            if _text(node) not in names or node == name_node:
                continue
            where = f"{Path(other_path).name}:{node.start_point[0] + 1}"
            parent = node.parent
            if parent is None:
                continue
            if node.type == "shorthand_property_identifier_pattern":
                continue  # a destructured import: its calls are found by the name
            if parent.type in _NAME_POSITIONS:
                continue  # a declaration, a parameter or an import/export binding
            if parent.type == "variable_declarator" and parent.child_by_field_name("name") == node:
                continue
            if parent.type == "pair" and parent.child_by_field_name("key") == node:
                continue
            if node.type == "shorthand_property_identifier":
                owner = parent.parent
                if parent.type == "object" and owner is not None and owner.type == "assignment_expression" and _text(owner.child_by_field_name("left")) == "module.exports":
                    continue
                return f"it is stored in an object at {where}"
            if parent.type == "variable_declarator" and parent.child_by_field_name("value") == node:
                continue  # an alias: its own name is followed
            callee = parent if parent.type == "member_expression" and parent.child_by_field_name("property") == node else node
            if callee is not node and callee.parent is not None and callee.parent.type == "variable_declarator" and callee.parent.child_by_field_name("value") == callee:
                continue  # `const r = ns.name`
            call = callee.parent
            if call is None or call.type != "call_expression" or call.child_by_field_name("function") != callee:
                return f"it is used as a value at {where}"
            top, cast = _unwrap(call)
            if cast:
                return f"its result is cast at {where}"
            if _boolean_only(top) or _bound_boolean_only(top):
                continue
            returned = _returned_function(top)
            if returned is not None:
                reason = _result_only_tested(repo, other_path, returned, depth + 1, seen | {function.id})
                if reason is not None:
                    return reason
                continue
            return f"its result is used other than as a test at {where}"
    return None


def _promise_evidence(repo: _Repo, path: str, call: Node, callee: Node, accessor: str) -> str | None:
    """Why the read may be a Promise, or ``None``: the receiver's declared type
    says so, or a declaration of the accessor in the repository is, or could be, async."""
    kind = repo.receiver_kind(path, call, callee, accessor)
    if kind == "promise":
        return "the receiver's declared type makes it return a Promise"
    if kind == "sync":
        return None
    if kind == "other" and repo.receiver_may_return_promise(path, call, callee, accessor):
        return "the receiver's declared type may return a Promise (a union, an overload or an alias)"
    bare = callee.type == "identifier" and not repo.has_with(path) and repo.locally_declared(path, callee)
    at = repo.binding_async.get(accessor) if bare else repo.async_at.get(accessor)
    if at is not None:
        return f"a declaration of {accessor} in the repository is, or could be, async ({at[0]}:{at[1]})"
    return None


def decide(repo: _Repo, path: str, call: Node, callee: Node, accessor: str) -> tuple[str, str]:
    """``(decision, why)``; ``why`` says what stopped a LEAVE."""
    top, promise_cast = _unwrap(call)
    if _boolean_consumed(top):
        # A Promise in a condition is always truthy: folding the read to the
        # flag's value would change which branch the customer's code takes.
        evidence = _promise_evidence(repo, path, call, callee, accessor)
        if evidence is not None:
            return LEAVE, f"a Promise in a condition is always truthy, and {evidence}"
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
    # Declared nowhere in the repository, and returned from a function nothing
    # annotates: the literal is proven only when every use of that function is
    # a call whose result is only tested (see `_result_only_tested`).
    owner = _returned_function(top)
    # The call itself reaches a declaration annotated `boolean`: positive
    # evidence, nothing to prove about the callers.
    if owner is not None and not _declared_boolean(repo, path, call, callee, accessor):
        why = _result_only_tested(repo, path, owner)
        if why is not None:
            return LEAVE, f"{why}, and nothing says the read is synchronous"
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
    props = {n: p for n, p in rule_synthesis.positions_of(accessors).items() if isinstance(p, str)}
    names = frozenset(a for a in accessors if rule_synthesis.plain_name(a) == a)
    repo = _Repo(repo_dir, names | frozenset(props))
    sites: list[Site] = []
    for path in dict.fromkeys(paths):
        if _EXTENSIONS.get(Path(path).suffix) is None:
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(n in source for n in (*names, *props)):
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
            if _is_optional(node, callee):
                continue
            prop = props.get(name) if name not in names else None
            if prop is not None:
                literal, could_name_key = _property_argument(node, prop)
            elif name in names:
                literal, could_name_key = _first_argument(node)
            else:
                continue
            if literal != flag_key and not could_name_key:
                continue
            top, _cast = _unwrap(node)
            if top.parent is not None and top.parent.type == "await_expression":
                continue
            decision, why = decide(repo, path, node, callee, name)
            if prop is not None and decision == LITERAL and not _sync_evidence(repo, path, node, callee, name):
                # Nothing says the read is not a Promise: a result that is
                # returned or passed on may be awaited or `.then`-ed by a
                # caller. Left as it always was, and listed by the residual scan.
                continue
            sites.append(
                Site(path, name, (callee.start_byte, callee.end_byte), (node.start_byte, node.end_byte),
                     decision, literal == flag_key, why, prop is not None)
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
        if site.path != path or not site.keyed or (site.accessor not in managed and not site.prop):
            continue
        if site.decision == LITERAL:
            out.append(kotlin_sentinel.mark(data, *site.callee))
        elif site.decision == PROMISE:
            out.append(kotlin_sentinel.mark(data, *site.call, f"({PROMISE_TAG})"))
    return out
