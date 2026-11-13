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
   call nothing does not count. A read that is not returned folds.
5. A read returned from a function nothing annotates folds only on evidence
   that it is synchronous: an audited SDK accessor imported from its package
   (``SYNC_AUDITS``), a call reaching a declaration annotated ``boolean``, or
   a wrapper that declares a primitive return type. Without it, only a
   module-private wrapper whose every caller, all in its own file, only tests
   the result folds (``_private_only_tested``); anything else leaves, with the
   advice that unblocks it.

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


def _module_candidates(importer: str, specifier: str) -> list[Path]:
    base = (Path(importer).parent / specifier).resolve()
    stems = [base]
    if base.suffix in {".js", ".mjs", ".cjs", ".jsx"}:
        stems += [base.with_suffix(suffix) for suffix in _RESOLVE_SUFFIXES]
    options = [*stems, *(Path(f"{base}{suffix}") for suffix in _RESOLVE_SUFFIXES)]
    options += [base / f"index{suffix}" for suffix in _RESOLVE_SUFFIXES]
    return options


def _string_value(node: Node | None) -> str | None:
    if node is None or node.type != "string" or node.child_count != 3:
        return None
    return _text(node.children[1])


_FOREIGN_SUFFIXES = (".vue", ".svelte", ".astro", ".mdx")
_MAX_DEPTH = 4
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


def _module_source(node: Node) -> str | None:
    """The module specifier of the import or export statement ``node`` sits in."""
    current: Node | None = node
    while current is not None and current.type not in {"import_statement", "export_statement"}:
        current = current.parent
    if current is None:
        return None
    return _string_value(current.child_by_field_name("source"))


#: Parents under which an identifier is where a name is bound, not a use of it.
_BINDING_PARENTS = frozenset(
    {
        "variable_declarator", "required_parameter", "optional_parameter", "formal_parameters", "import_specifier",
        "import_clause", "namespace_import", "import_require_clause", "function_declaration",
        "generator_function_declaration", "function_expression", "class_declaration", "catch_clause",
        "pair_pattern", "object_pattern", "array_pattern", "rest_pattern", "assignment_pattern",
    }
)


def _is_binding(node: Node) -> bool:
    """Whether ``node`` is where a name is bound (a declarator, a parameter, an
    import), not a use of it."""
    parent = node.parent
    if parent is None or parent.type not in _BINDING_PARENTS:
        return False
    if parent.type == "variable_declarator":
        return parent.child_by_field_name("name") == node
    if parent.type in {"function_declaration", "generator_function_declaration", "function_expression", "class_declaration"}:
        return parent.child_by_field_name("name") == node
    if parent.type == "pair_pattern":
        return parent.child_by_field_name("value") == node
    if parent.type == "assignment_pattern":
        return parent.child_by_field_name("left") == node
    if parent.type in {"required_parameter", "optional_parameter"}:
        return parent.child_by_field_name("pattern") == node
    return True


def _unwrap_once(node: Node) -> Node | None:
    values = [c for c in node.named_children if c.type != "comment"]
    if not values:
        return None
    return values[-1] if node.type == "type_assertion" else values[0]


#: Wrappers that hand their operand on unchanged.
_SEE_THROUGH = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression", "type_assertion"}
)


def _in_type(node: Node) -> bool:
    from flag_cleanup import cross_file_ts

    return cross_file_ts._in_type(node)


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
    if Path(path).name in _html_script_names(repo):
        return "its file is loaded by an HTML `<script src>`, which makes its top-level functions globals"
    if any(child.type in {"import_statement", "export_statement"} for child in tree.children):
        return None
    if Path(path).suffix in {".cjs", ".mjs", ".cts", ".mts"}:
        return None  # a module by its extension alone
    text = tree.text.decode("utf-8", "replace")
    if not re.search(r"\btypeof\s+(?:module|exports|define|require)\b", text) and any(
        _commonjs_statement(child) for child in tree.children
    ):
        return None
    return "it is a top-level function of a classic script (or a UMD one), which is a global reachable by a computed key"


def _commonjs_statement(statement: Node) -> bool:
    """Whether a top-level statement makes its file a CommonJS module whatever
    runs it: an unconditional ``module.exports``/``exports.x`` assignment, or a
    ``require(...)`` call. One under a ``typeof module`` test (UMD) does not
    count, and neither does the file as a whole when it has such a test."""
    for node in ts_syntax._walk(statement):
        if node.type in _FUNCTION_NODES or node.type in {"if_statement", "ternary_expression"}:
            return False
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is not None and re.match(r"(?:module\.exports|exports)\b", _text(left)):
                return True
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type == "identifier" and _text(callee) == "require":
                return True
    return False


def _html_script_names(repo: _Repo) -> frozenset[str]:
    """The file names every HTML file of the checkout loads with ``<script src>``."""
    cached = getattr(repo, "_html_script_names", None)
    if cached is not None:
        return cached
    names: set[str] = set()
    root = getattr(repo, "root", None)
    if root is not None and Path(root).is_dir():
        for current, directories, files in os.walk(root):
            directories[:] = [d for d in directories if d not in {"node_modules", ".git"}]
            for file in files:
                if not file.endswith((".html", ".htm")):
                    continue
                try:
                    text = (Path(current) / file).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for src in re.findall(r"""<script\b[^>]*\bsrc\s*=\s*["']?([^"'\s>]+)""", text, re.IGNORECASE):
                    names.add(src.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1])
    facts = frozenset(names)
    repo._html_script_names = facts  # type: ignore[attr-defined]
    return facts


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


# --- sync evidence and module-private wrappers --------------------------------------------------
#
# A read returned from a wrapper function folds to a bare literal only when
# the READ is known to be synchronous (an audited SDK accessor, or a declared
# non-Promise type), or when the wrapper is module-private and every caller,
# all of them in its own file, only tests the result. Anything else is left,
# with the advice that unblocks it.


@dataclass(frozen=True)
class _Audit:
    """A published flag SDK whose reads are synchronous, as its own type
    declarations state them. ``calls`` are exports that read a flag when
    called (``useFlag("k")``); ``instances`` are exports that are a client
    (``"default"`` for the default export); ``factories`` are exports that
    return one (``useLDClient()``); ``members`` are the client's reading
    methods. ``minimum`` and ``majors`` bound the releases audited: a
    project's declared or installed version must fall inside them."""

    package: str
    minimum: tuple[int, int, int]
    majors: frozenset[int]
    calls: frozenset[str] = frozenset()
    instances: frozenset[str] = frozenset()
    factories: frozenset[str] = frozenset()
    members: frozenset[str] = frozenset()


#: Synchronous flag accessors of published SDKs. Every entry was read off the
#: package's published ``.d.ts`` (tarballs fetched with ``npm pack``, never
#: run) at the first and last release of each listed major, in 2026-11; each
#: comment gives the declared return types. A Promise-returning lookalike is
#: never listed: LaunchDarkly's server SDK (``@launchdarkly/node-server-sdk``,
#: ``variation``/``boolVariation`` return ``Promise<…>``), ConfigCat's
#: ``getValueAsync`` (``Promise<SettingTypeOf<T>>``), OpenFeature's server
#: client. ``@unleash/react`` is not a published package.
SYNC_AUDITS: tuple[_Audit, ...] = (
    # useFeatureIsOn(id): boolean; useFeatureValue(id, fallback: T): WidenPrimitives<T>
    # (0.17.0 … 2.0.0; 0.10.0 had neither). useGrowthBook(): GrowthBook (| undefined
    # before 1.0), whose isOn/isOff(key): boolean and getFeatureValue(key, d):
    # WidenPrimitives<V> (@growthbook/growthbook 0.17.0 … 2.0.0).
    _Audit(
        "@growthbook/growthbook-react", (0, 17, 0), frozenset({0, 1, 2}),
        calls=frozenset({"useFeatureIsOn", "useFeatureValue"}),
        factories=frozenset({"useGrowthBook"}), members=frozenset({"isOn", "isOff", "getFeatureValue"}),
    ),
    # useFeatureFlagEnabled(flag): boolean | undefined (a `(flag, defaultValue): boolean`
    # overload since 1.4xx); useFeatureFlagVariantKey(flag): string | boolean |
    # undefined; useFeatureFlagPayload(flag): JsonType (1.50.0 … 1.447.15; 1.0.0
    # shipped no types).
    _Audit(
        "posthog-js/react", (1, 50, 0), frozenset({1}),
        calls=frozenset({"useFeatureFlagEnabled", "useFeatureFlagVariantKey", "useFeatureFlagPayload"}),
    ),
    # The `posthog` instance (default and named export): isFeatureEnabled(key, o?):
    # boolean (| undefined since 1.100); getFeatureFlag(key, o?): boolean | string |
    # undefined; getFeatureFlagPayload(key): JsonType (1.50.0 … 1.447.15).
    _Audit(
        "posthog-js", (1, 50, 0), frozenset({1}),
        instances=frozenset({"default", "posthog"}),
        members=frozenset({"isFeatureEnabled", "getFeatureFlag", "getFeatureFlagPayload"}),
    ),
    # useFlag(name): boolean; useVariant(name): any (1.x-3.0) / Partial<IVariant>
    # (3.6 … 6.0.1).
    _Audit(
        "@unleash/proxy-client-react", (1, 0, 0), frozenset({1, 2, 3, 4, 5, 6}),
        calls=frozenset({"useFlag", "useVariant"}),
    ),
    # useFlags<T extends LDFlagSet>(): T (2.29.4 … 3.9.4); useLDClient(): LDClient |
    # undefined, whose variation(key, d?): LDFlagValue and variationDetail(key, d?):
    # LDEvaluationDetail (launchdarkly-js-sdk-common 3.0.0 … 5.8.3, which the
    # client SDK re-exports). The types do not settle `variation`: LDFlagValue is
    # declared `any`. The entry rests on the 5.8.3 SOURCE (src/index.js):
    # variation returns `variationDetailInternal(...).value`, read synchronously
    # from the flag store or the default, never a Promise.
    _Audit(
        "launchdarkly-react-client-sdk", (2, 29, 4), frozenset({2, 3}),
        calls=frozenset({"useFlags"}),
        factories=frozenset({"useLDClient"}), members=frozenset({"variation", "variationDetail"}),
    ),
    # initialize(envKey, context, options?): LDClient (2.10.3 … 3.9.6), whose
    # variation/variationDetail are as above (the source, not the `any` types).
    _Audit(
        "launchdarkly-js-client-sdk", (2, 10, 3), frozenset({2, 3}),
        factories=frozenset({"initialize"}), members=frozenset({"variation", "variationDetail"}),
    ),
    # The default `flagsmith` instance: hasFeature(key, o?): boolean; getValue(key, o?):
    # string | number | boolean (1.7.4-3.0.0) / IFlagsmithValue<T> (3.24.0 … 10.0.0).
    _Audit(
        "flagsmith", (1, 7, 4), frozenset(range(1, 11)),
        instances=frozenset({"default"}), members=frozenset({"hasFeature", "getValue"}),
    ),
    # useFeatureFlag<T>(key, defaultValue: T): T; useFeatureflipClient():
    # FeatureflipClient, whose boolVariation(key, d): boolean, stringVariation: string,
    # numberVariation: number (@featureflip/react and @featureflip/browser 2.19.1,
    # this repository's own SDKs).
    _Audit(
        "@featureflip/react", (2, 19, 1), frozenset({2}),
        calls=frozenset({"useFeatureFlag"}),
        factories=frozenset({"useFeatureflipClient"}),
        members=frozenset({"boolVariation", "stringVariation", "numberVariation"}),
    ),
)
_AUDITS = {audit.package: audit for audit in SYNC_AUDITS}


def _import_of(tree: Node, name: Node) -> tuple[str, str] | None:
    """``(package, imported name)`` when the identifier ``name`` is bound exactly
    once in the file, by a value import (``"default"`` for a default import)."""
    from flag_cleanup import inert_calls, ts_scope

    binders = ts_scope.bindings(tree, _text(name))
    if len(binders) != 1:
        return None
    return inert_calls._imported(binders[0])


def _const_value(tree: Node, name: Node) -> Node | None:
    """The initializer of the one ``const`` binding of the identifier ``name``."""
    from flag_cleanup import ts_scope

    binders = ts_scope.bindings(tree, _text(name))
    if len(binders) != 1:
        return None
    holder = binders[0].parent
    if holder is None or holder.type != "variable_declarator" or holder.child_by_field_name("name") != binders[0]:
        return None
    declaration = holder.parent
    if declaration is None or declaration.type != "lexical_declaration" or _text(declaration.children[0]) != "const":
        return None
    value = holder.child_by_field_name("value")
    while value is not None and value.type in _SEE_THROUGH:
        value = _unwrap_once(value)
    return value


def _factory_package(tree: Node, call: Node | None) -> tuple[_Audit, str] | None:
    """``(audit, export)`` when ``call`` calls one of an audit's ``factories``:
    ``useLDClient()``, or ``LD.initialize(…)`` off a namespace import."""
    if call is None or call.type != "call_expression":
        return None
    callee = call.child_by_field_name("function")
    if callee is not None and callee.type == "identifier":
        imported = _import_of(tree, callee)
        if imported is not None and imported[0] in _AUDITS and imported[1] in _AUDITS[imported[0]].factories:
            return _AUDITS[imported[0]], imported[1]
    if callee is not None and callee.type == "member_expression":
        owner, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
        if owner is not None and owner.type == "identifier" and prop is not None:
            imported = _import_of(tree, owner)
            if imported is not None and imported[1] == "*" and imported[0] in _AUDITS and _text(prop) in _AUDITS[imported[0]].factories:
                return _AUDITS[imported[0]], _text(prop)
    return None


def _audited_package(tree: Node, call: Node, callee: Node) -> _Audit | None:
    """The audit that makes this read synchronous, if any: an imported
    ``calls`` export called by its local name, or an audited client's
    ``members`` called on an imported instance, on a factory's result, or on
    a ``const`` bound to that result."""
    if callee.type == "identifier":
        imported = _import_of(tree, callee)
        if imported is not None and imported[0] in _AUDITS and imported[1] in _AUDITS[imported[0]].calls:
            return _AUDITS[imported[0]]
        return None
    if callee.type != "member_expression":
        return None
    prop = callee.child_by_field_name("property")
    receiver = callee.child_by_field_name("object")
    while receiver is not None and receiver.type in _SEE_THROUGH:
        receiver = _unwrap_once(receiver)
    if prop is None or receiver is None:
        return None
    member = _text(prop)
    if receiver.type == "identifier":
        imported = _import_of(tree, receiver)
        if imported is not None:
            audit = _AUDITS.get(imported[0])
            return audit if audit is not None and imported[1] in audit.instances and member in audit.members else None
        receiver = _const_value(tree, receiver)
    made = _factory_package(tree, receiver)
    return made[0] if made is not None and member in made[0].members else None


_SPEC_VERSION = re.compile(r"\A[\^~]?v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?(?:[-+][\w.+-]*)?\Z")


def _spec_floor(spec: str) -> tuple[int, int, int] | None:
    """The lowest release a dependency spec allows, when it is a plain version,
    a ``^``/``~`` range or an ``x`` range (each of which stays within its
    major); None for anything else (``*``, ``>=``, a tag, a path or URL)."""
    match = _SPEC_VERSION.match(spec.strip())
    if match is None:
        return None
    parts = [int(p) if p is not None and p.isdigit() else 0 for p in match.groups()]
    return parts[0], parts[1], parts[2]


#: ``package.json`` fields that can put another module under a package's name.
_REPLACING_FIELDS = ("overrides", "resolutions", "browser")


def _names_package(value: object, package: str) -> bool:
    """Whether a manifest field's keys, at any depth, name ``package`` (an npm
    override nests by parent; a yarn resolution may be ``**/pkg`` or
    ``pkg@1``; a pnpm one ``parent>pkg``). Over-matching only withholds."""
    if isinstance(value, dict):
        return any(package in str(key) or _names_package(v, package) for key, v in value.items())
    if isinstance(value, list):
        return any(_names_package(v, package) for v in value)
    return False


def _version_audited(repo: _Repo, audit: _Audit) -> bool:
    """Whether the checkout resolves ``audit``'s package to a release the audit
    covers, as far as its ``package.json`` files can tell. Read once per run.

    Withheld when any manifest can put another module under the package's
    name: a package NAMED like it (a workspace package, or the root with an
    ``exports`` self-reference), an ``overrides``, ``resolutions`` or
    ``pnpm.overrides`` entry naming it, or a ``browser`` field mapping it; and
    when a manifest does not parse. Otherwise every installed copy, or else
    every declared spec, must fall inside the audited releases; a spec that
    does not bound one (``*``, ``>=2``, a tag, a path, an ``npm:`` alias) is
    not trusted. A package neither installed nor declared is trusted only when
    the checkout has no ``package.json`` at all (a fixture): otherwise a bare
    import may resolve to a hoisted dependency, a workspace package or a
    parent's root."""
    cache: dict[str, bool] = repo.__dict__.setdefault("_audited_versions", {})
    if audit.package in cache:
        return cache[audit.package]
    package = audit.package.split("/react")[0] if audit.package == "posthog-js/react" else audit.package
    installed: list[tuple[int, int, int] | None] = []
    declared: list[tuple[int, int, int] | None] = []
    manifests = 0
    replaced = False
    root = getattr(repo, "root", None)
    if root is not None and Path(root).is_dir():
        for current, directories, files in os.walk(root):
            directories[:] = [d for d in directories if d not in {"node_modules", ".git"}]
            if "pnpm-workspace.yaml" in files:
                try:
                    replaced |= package in (Path(current) / "pnpm-workspace.yaml").read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    replaced = True
            if "package.json" not in files:
                continue
            manifests += 1
            try:
                manifest = json.loads((Path(current) / "package.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                manifest = None
            if not isinstance(manifest, dict):
                replaced = True
                continue
            if manifest.get("name") == package:
                replaced = True
            pnpm = manifest.get("pnpm")
            if any(_names_package(manifest.get(field), package) for field in _REPLACING_FIELDS) or (
                isinstance(pnpm, dict) and _names_package(pnpm.get("overrides"), package)
            ):
                replaced = True
            for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                deps = manifest.get(field)
                if isinstance(deps, dict) and package in deps:
                    spec = deps[package]
                    declared.append(_spec_floor(spec) if isinstance(spec, str) else None)
            copy = Path(current) / "node_modules" / package / "package.json"
            if copy.is_file():
                try:
                    version = json.loads(copy.read_text(encoding="utf-8")).get("version")
                except (OSError, UnicodeDecodeError, ValueError, AttributeError):
                    version = None
                installed.append(_spec_floor(version) if isinstance(version, str) else None)
    versions = installed or declared
    if replaced or (not versions and manifests):
        answer = False
    else:
        answer = all(v is not None and v >= audit.minimum and v[0] in audit.majors for v in versions)
    cache[audit.package] = answer
    return answer


def _fits(pattern: str, specifier: str) -> bool:
    """Whether a ``paths`` key matches ``specifier`` as the compiler matches one."""
    if "*" not in pattern:
        return pattern == specifier
    prefix, _star, suffix = pattern.partition("*")
    return specifier.startswith(prefix) and specifier.endswith(suffix) and len(specifier) >= len(prefix) + len(suffix)


def _compiler_redirects(repo: _Repo, path: str, package: str) -> bool:
    """Whether the compiler may read ``package`` as a file of the checkout: a
    ``paths`` key in any JSON config of the checkout that fits it (every
    ``.json`` file holding ``compilerOptions`` is read, so a config another
    extends counts wherever it lives; one that does not parse counts when it
    spells the package), a file or directory of that name under any
    ``baseUrl``, or one beside the reader or above it (classic resolution).
    A config inside an installed package only maps names into that package."""
    from flag_cleanup import cross_file_ts

    configs = repo.__dict__.get("_compiler_configs")
    if configs is None:
        configs = []
        root = getattr(repo, "root", None)
        if root is not None and Path(root).is_dir():
            for current, directories, files in os.walk(root):
                directories[:] = [d for d in directories if d not in {"node_modules", ".git"}]
                for file in files:
                    if not file.endswith(".json"):
                        continue
                    try:
                        text = (Path(current) / file).read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if "compilerOptions" not in text:
                        continue
                    try:
                        data = json.loads(cross_file_ts._strip_jsonc(text))
                    except ValueError:
                        data = None
                    configs.append((Path(current), text, data))
        repo._compiler_configs = configs  # type: ignore[attr-defined]
    for directory, text, data in configs:
        options = data.get("compilerOptions") if isinstance(data, dict) else None
        if not isinstance(options, dict):
            if package in text:
                return True
            continue
        paths = options.get("paths")
        if isinstance(paths, dict) and any(_fits(key, package) for key in paths):
            return True
        if paths is not None and not isinstance(paths, dict) and package in text:
            return True
        base_url = options.get("baseUrl")
        if isinstance(base_url, str) and _names_a_file(directory / base_url / package):
            return True
    reader = Path(path).resolve().parent
    root = Path(getattr(repo, "root", reader)).resolve()
    while True:
        if _names_a_file(reader / package):
            return True
        if reader == root or reader.parent == reader:
            return False
        reader = reader.parent


def _names_a_file(base: Path) -> bool:
    try:
        return base.exists() or any(Path(f"{base}{suffix}").exists() for suffix in (*_RESOLVE_SUFFIXES, ".d.ts"))
    except OSError:
        return True


def _package_redirected(repo: _Repo, path: str, package: str) -> bool:
    """Whether a compiler config (:func:`_compiler_redirects`), a bundler, Jest
    or Babel alias in a config file, or a module directory a config adds
    (:func:`_extra_search_paths`) may send the package's name to another module.

    Configs are read from every JavaScript or TypeScript file named like a
    config, ``.babelrc*``, every JSON file named like a Jest config, and the
    ``"jest"`` object of every ``package.json``. A config's own location says
    nothing reliable about the files it governs (``rootDir: '..'``), so an
    added module directory counts wherever it is: it withholds the audit when
    any directory of the checkout whose path ends in it holds a file or
    directory named after the package."""
    from flag_cleanup.ts_constant_params import _alias_keys, _redirects

    if _compiler_redirects(repo, path, package):
        return True
    facts = repo.__dict__.get("_alias_facts")
    if facts is None:
        patterns: list[tuple[str | None, bool]] = []
        entries: list[str | None] = []
        directories_seen: list[Path] = []
        root = getattr(repo, "root", None)
        if root is not None and Path(root).is_dir():
            for current, directories, files in os.walk(root):
                directories[:] = [d for d in directories if d not in {"node_modules", ".git"}]
                directories_seen.append(Path(current))
                for file in files:
                    if file == "package.json" or (file.endswith(".json") and "jest" in file):
                        _json_config(Path(current) / file, file == "package.json", patterns, entries)
                        continue
                    if "config" not in file and not file.startswith(".babelrc"):
                        continue
                    if Path(file).suffix not in _EXTENSIONS and not file.startswith(".babelrc"):
                        continue
                    try:
                        data = (Path(current) / file).read_bytes()
                    except OSError:
                        patterns.append((None, True))
                        continue
                    if file.startswith(".babelrc") and not file.endswith((".js", ".cjs", ".mjs")):
                        data = b"(" + data + b")"
                    grammar = _EXTENSIONS.get(Path(file).suffix, "js")
                    tree = ts_syntax._parse(data.decode("utf-8", "replace"), grammar)
                    patterns.extend(_alias_keys(tree, data))
                    entries.extend(_extra_search_paths(tree))
        facts = (patterns, entries, directories_seen)
        repo._alias_facts = facts  # type: ignore[attr-defined]
    patterns, entries, directories_seen = facts
    if _redirects(patterns, package):
        return True
    for entry in entries:
        if entry is None:
            return True  # computed: may be any directory
        parts = tuple(p for p in entry.replace("<rootDir>", "").split("/") if p not in {"", ".", ".."})
        for directory in directories_seen:
            if (not parts or directory.parts[-len(parts):] == parts) and _names_a_file(directory / package):
                return True
    return False


def _json_config(file: Path, manifest: bool, patterns: list[tuple[str | None, bool]], entries: list[str | None]) -> None:
    """Read a JSON Jest config (``jest.config.json``, or a ``package.json``'s
    ``"jest"`` object) into ``patterns`` (``moduleNameMapper`` keys, as regular
    expressions) and ``entries`` (``moduleDirectories``/``modulePaths``). One
    that does not parse counts as redirecting everything; a manifest that does
    not parse is already withheld (:func:`_version_audited`)."""
    from flag_cleanup import cross_file_ts

    try:
        data = json.loads(cross_file_ts._strip_jsonc(file.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        if not manifest:
            patterns.append((None, True))
        return
    config = data.get("jest") if manifest and isinstance(data, dict) else data
    if not isinstance(config, dict):
        if config is not None:
            patterns.append((None, True))
        return
    mapper = config.get("moduleNameMapper")
    if isinstance(mapper, dict):
        patterns.extend((str(key), True) for key in mapper)
    elif mapper is not None:
        patterns.append((None, True))
    for key in ("moduleDirectories", "modulePaths"):
        value = config.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            entries.append(None)
            continue
        entries.extend(v if isinstance(v, str) else None for v in value if v != "node_modules")


def _extra_search_paths(tree: Node) -> list[str | None]:
    """The directories besides ``node_modules`` a config adds to where bare
    names are looked up: Jest's ``moduleDirectories``/``modulePaths``, or
    webpack's ``resolve.modules``. Each is a plain string, or None for one
    that is not (a computed ``path.resolve(...)``), which may be any directory."""
    found: list[str | None] = []
    for node in ts_syntax._walk(tree):
        if node.type != "pair":
            continue
        key = node.child_by_field_name("key")
        name = _text(key).strip("'\"`") if key is not None else ""
        if name == "modules":
            owner = node.parent.parent if node.parent is not None else None
            owner_key = owner.child_by_field_name("key") if owner is not None and owner.type == "pair" else None
            if owner_key is None or _text(owner_key).strip("'\"`") != "resolve":
                continue
        elif name not in {"moduleDirectories", "modulePaths"}:
            continue
        value = node.child_by_field_name("value")
        items = [c for c in value.named_children if c.type != "comment"] if value is not None and value.type == "array" else None
        if items is None:
            found.append(None)
            continue
        for item in items:
            text = _string_value(item)
            if text != "node_modules":
                found.append(text)
    return found


def _audited_sync_read(repo: _Repo, path: str, call: Node, callee: Node) -> bool:
    """Whether the read is a call of an audited synchronous SDK accessor
    (:data:`SYNC_AUDITS`) imported from its package, at a version inside the
    audit, with nothing redirecting the package's name."""
    tree = repo.tree(path)
    if tree is None:
        return False
    audit = _audited_package(tree, call, callee)
    if audit is None:
        return False
    return _version_audited(repo, audit) and not _package_redirected(repo, path, audit.package)


def _declared_sync(function: Node) -> bool:
    """Whether the wrapper's own declared return type is a primitive or a union of
    primitive and literal types (``boolean``, ``boolean | undefined``,
    ``'on' | 'off'``): never ``any``, ``unknown``, a Promise or an object. Its
    own annotation, or the function type its ``const`` is declared with."""
    if _primitive_return(function.child_by_field_name("return_type")):
        return True
    holder = function.parent
    if holder is not None and holder.type == "variable_declarator" and holder.child_by_field_name("value") == function:
        annotation = holder.child_by_field_name("type")
        kind = next((n for n in ts_syntax._walk(annotation) if n.type == "function_type"), None) if annotation else None
        if kind is not None and _primitive_return(kind.child_by_field_name("return_type")):
            return True
    return False


#: What lets code in a file reach a binding of that file by name without
#: spelling a reference to it.
_FILE_REFLECTION = frozenset({"eval", "Function"})
_GLOBAL_RECEIVERS = frozenset({"this", "globalThis", "window", "self", "global"})
#: The rewire API and package: they read any top-level binding of a module from another file.
_REWIRE = re.compile(rb"__(?:RewireAPI|Rewire|ResetDependency|GetDependency|get|set|with)__|\brewire\b")


def _file_reflects(tree: Node) -> str | None:
    """A reason when code in the file may reach its bindings by a computed name:
    ``eval``, ``Function``, a ``with`` statement, or a computed member of
    ``this`` or the global object."""
    for node in ts_syntax._walk(tree):
        if node.type == "with_statement":
            return f"line {node.start_point[0] + 1} has a `with` statement"
        if node.type == "identifier" and _text(node) in _FILE_REFLECTION:
            return f"line {node.start_point[0] + 1} uses `{_text(node)}`"
        if node.type == "subscript_expression":
            owner = node.child_by_field_name("object")
            while owner is not None and owner.type in _SEE_THROUGH:
                owner = _unwrap_once(owner)
            if owner is not None and (owner.type == "this" or (owner.type == "identifier" and _text(owner) in _GLOBAL_RECEIVERS)):
                return f"line {node.start_point[0] + 1} reads a computed member of `{_text(owner)}`"
    return None


def _module_scope_reflection(repo: _Repo) -> bool:
    """Whether anything in the checkout may reach a module's top-level binding
    from another file: the rewire API or package in a source, or ``rewire`` in
    a ``package.json``. Read once per run; an unreadable file answers yes."""
    cached = getattr(repo, "_module_scope_reflection", None)
    if cached is not None:
        return cached
    found = False
    root = getattr(repo, "root", None)
    if root is not None and Path(root).is_dir():
        for current, directories, files in os.walk(root):
            directories[:] = [d for d in directories if d not in {"node_modules", ".git"}]
            for file in files:
                if file != "package.json" and Path(file).suffix not in _EXTENSIONS and not file.endswith(_FOREIGN_SUFFIXES):
                    continue
                try:
                    if _REWIRE.search((Path(current) / file).read_bytes()):
                        found = True
                except OSError:
                    found = True
                if found:
                    break
            if found:
                break
    repo._module_scope_reflection = found  # type: ignore[attr-defined]
    return found


def _exported(function: Node) -> bool:
    """Whether ``function``'s declaration sits in an ``export`` statement."""
    current: Node | None = function
    while current is not None and current.type not in {"program", "statement_block"}:
        if current.type == "export_statement":
            return True
        current = current.parent
    return False


def _private_only_tested(repo: _Repo, path: str, function: Node, depth: int = 0, seen: frozenset[int] = frozenset()) -> str | None:
    """Why a read returned from ``function`` cannot fold without sync evidence,
    or ``None`` when it can: ``function`` is a plain named function private to
    its module (no export of any form, the file a module, not a classic
    script, and nothing that reaches a binding by a computed name in the file
    or by name from another one), and every mention of its name in the file
    is a call whose result is only tested, or returned from another such
    function (to ``_MAX_DEPTH`` levels)."""
    if function.id in seen:
        return None
    if depth > _MAX_DEPTH:
        return "the chain of functions returning the read is too deep to check"
    name_node = _function_name(function)
    if name_node is None:
        return "it is returned from a function with no name"
    plain = _plain_function(function)
    if plain is not None:
        return plain
    if _exported(function):
        return "it is returned from an exported function, whose callers in other files can't all be followed"
    script = _classic_script(repo, path, function)
    if script is not None:
        return script
    tree = repo.tree(path)
    if tree is None:
        return "its file could not be parsed"
    reflects = _file_reflects(tree)
    if reflects is not None:
        return f"its file may reach the function by a computed name ({reflects})"
    if _module_scope_reflection(repo):
        return "the checkout uses rewire, which reaches a module's private functions from other files"
    name = _text(name_node)
    shown = Path(path).name
    for node in ts_syntax._walk(tree):
        if node.type not in {"identifier", "shorthand_property_identifier"} or _text(node) != name or node == name_node:
            continue
        where = f"{shown}:{node.start_point[0] + 1}"
        if node.type == "shorthand_property_identifier":
            return f"it is stored in an object at {where}"
        parent = node.parent
        if parent is None or _in_type(node):
            continue
        if parent.type == "export_specifier":
            return f"it is exported at {where}"
        if _is_binding(node):
            continue  # another binding of the name (a parameter, a nested local)
        call = parent if parent.type == "call_expression" and parent.child_by_field_name("function") == node else None
        if call is None:
            return f"it is used as a value at {where}"
        top, cast = _unwrap(call)
        if cast:
            return f"its result is cast at {where}"
        if _boolean_only(top) or _bound_boolean_only(top):
            continue
        returned = _returned_function(top)
        if returned is not None and _function_name(returned) is not None:
            if _declared_sync(returned) or return_kind(returned.child_by_field_name("return_type")) == "sync":
                continue
            reason = _private_only_tested(repo, path, returned, depth + 1, seen | {function.id})
            if reason is not None:
                return reason
            continue
        return f"its result is used other than as a test at {where}"
    return None


#: The end of every reason a returned read is left for, saying how to unblock it.
_UNBLOCK = (
    "; declare the wrapper's return type (`: boolean`), since the accessor is not a known "
    "synchronous SDK accessor"
)


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
    # A declaration of the accessor that is, or could be, async is evidence a
    # declared `boolean` cannot outweigh: the accessor may be typed `any`.
    # A bare call of a name the file itself declares reaches only a binding
    # of the name, unless a `with` makes any object's member one. An
    # imported name is whatever the module exports, members of an exported
    # object (`module.exports = { enabled }`) included.
    bare = callee.type == "identifier" and not repo.has_with(path) and repo.locally_declared(path, callee)
    at = repo.binding_async.get(accessor) if bare else repo.async_at.get(accessor)
    async_why = None if at is None else f"a declaration of {accessor} in the repository is, or could be, async ({at[0]}:{at[1]})"
    function, composite = _returned(top)
    if function is not None:
        if _has_async_token(function):
            return LITERAL, ""
        annotated = return_kind(function.child_by_field_name("return_type"))
        if composite:
            if annotated == "sync":
                return (LEAVE, async_why) if async_why else (LITERAL, "")
            return LEAVE, "it is one arm of a returned expression in a function that is neither async nor annotated boolean"
        if annotated == "promise":
            return PROMISE, ""
        if annotated == "sync":
            return (LEAVE, async_why) if async_why else (LITERAL, "")
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
    if async_why is not None:
        return LEAVE, async_why
    # Declared nowhere in the repository as anything that may be async, and
    # returned from a function nothing annotates. The literal needs evidence
    # that the read is synchronous: the call reaches a declaration annotated
    # `boolean`, it is an audited SDK accessor, or the wrapper declares a
    # primitive return type. Without it, only a module-private wrapper whose
    # every caller in its own file only tests the result folds.
    owner = _returned_function(top)
    if owner is None:
        return LITERAL, ""
    if (
        _declared_boolean(repo, path, call, callee, accessor)
        or _audited_sync_read(repo, path, call, callee)
        or _declared_sync(owner)
    ):
        return LITERAL, ""
    why = _private_only_tested(repo, path, owner)
    if why is not None:
        return LEAVE, f"{why}, and nothing says the read is synchronous{_UNBLOCK}"
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
