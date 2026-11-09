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
   documented as returning a Promise, or could be (an explicit return type other
   than ``boolean``, no return type on a signature, a body that returns a call,
   a const initialised from a call, a ``.then`` chain) leaves the read; none
   folds it. A name declared nowhere folds, as it always did.

An accessor with at least one read that does not fold to the literal is
"managed": its un-awaited reads are marked per site (see ``piranha_runner``)
instead of matched by the bare-call clone. Awaited reads fold as before; an
awaited read through a key constant is not folded for a managed accessor.
"""

from __future__ import annotations

import re
from collections import defaultdict
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


_COMPARISONS = frozenset({"==", "===", "!=", "!==", "<", ">", "<=", ">=", "instanceof", "in"})


def _certainly_boolean(expr: Node | None) -> bool:
    """A returned expression that cannot be a Promise: a boolean literal, a
    comparison or type test, or a negation."""
    while expr is not None and expr.type == "parenthesized_expression" and expr.named_children:
        expr = expr.named_children[0]
    if expr is None:
        return False
    if expr.type in {"true", "false"}:
        return True
    if expr.type == "binary_expression":
        operator = expr.child_by_field_name("operator")
        return operator is not None and _text(operator) in _COMPARISONS
    if expr.type == "unary_expression":
        operator = expr.child_by_field_name("operator")
        return operator is not None and _text(operator) == "!"
    return False


def _returns_only_booleans(subject: Node) -> bool:
    body = subject.child_by_field_name("body")
    if body is None:
        return False
    if body.type != "statement_block":
        return _certainly_boolean(body)
    returns = []
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type in _FUNCTION_NODES:
            continue
        if node.type == "return_statement":
            returns.append(node.named_children[0] if node.named_children else None)
        stack.extend(node.children)
    return bool(returns) and all(_certainly_boolean(r) for r in returns)


_TEST_DOUBLE_METHODS = frozenset({"stub", "fn", "spyOn", "createStubInstance"})
_TEST_FILE = re.compile(r"(\.(test|spec)\.[cm]?[jt]sx?$)|(/__tests__/)")


def _is_test_double(value: Node | None) -> bool:
    """``sinon.stub(..)``, ``jest.fn()``, ``vi.fn().mockResolvedValue(..)``, ``x.spyOn(..)``."""
    while value is not None and value.type == "call_expression":
        callee = value.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            return False
        prop = callee.child_by_field_name("property")
        if prop is not None and _text(prop) in _TEST_DOUBLE_METHODS:
            return True
        value = callee.child_by_field_name("object")
    return False


def _possibly_async(node: Node) -> bool | None:
    """Whether a declaration of the name is, or could be, async. None: says nothing.

    Possibly async UNLESS it has an explicit return annotation of exactly
    ``boolean``, or is a function whose every returned expression is certainly
    a boolean. A value that is a reference, a call, an alias, a ternary or a
    returned local all count as possibly async.
    """
    if node.type in _VALUE_KINDS:
        value = node.child_by_field_name("value")
        annotation = node.child_by_field_name("type")
        if _is_test_double(value):
            return None
        if value is not None and value.type in _FUNCTION_VALUES:
            subject = value
        else:
            ftype = _function_type(annotation)
            if ftype is not None:
                return _jsdoc_promise(node) or return_kind(ftype.child_by_field_name("return_type")) != "sync"
            if annotation is None and value is None:
                return None
            return True
    else:
        subject = node
    if _has_async_token(subject) or _jsdoc_promise(node):
        return True
    rtype = subject.child_by_field_name("return_type")
    if rtype is not None:
        return return_kind(rtype) != "sync"
    if node.type in _SIGNATURE_KINDS:
        return True
    return not _returns_only_booleans(subject)


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
        self.possibly_async: dict[str, bool] = defaultdict(bool)
        self.trees: dict[str, Node] = {}
        root = Path(repo_dir)
        files = [root] if root.is_file() else source_files(root, _EXTENSIONS, _SKIP_DIRS)
        for path in files:
            tree = self.tree(str(path))
            if tree is None:
                continue
            if _TEST_FILE.search(str(path).replace("\\", "/")):
                continue  # a test file's fakes say nothing about the production accessor
            for node in ts_syntax._walk(tree):
                if node.type in _CALLABLE_KINDS or node.type in _VALUE_KINDS:
                    name = _name_of(node)
                    if name in accessors:
                        verdict = _possibly_async(node)
                        if verdict is not None:
                            self.possibly_async[name] = self.possibly_async[name] or verdict
                elif node.type == "shorthand_property_identifier_pattern" and _text(node) in accessors:
                    self.possibly_async[_text(node)] = True  # `const { isEnabled } = api`
                elif node.type == "export_specifier":
                    alias = node.child_by_field_name("alias")
                    if alias is not None and _text(alias) in accessors:
                        self.possibly_async[_text(alias)] = True

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


def _use_site_promise(top: Node) -> bool:
    """The result is used as a Promise: a member access on it, a ``Promise.all`` argument, ``expect(..).resolves``."""
    parent = top.parent
    if parent is None:
        return False
    if parent.type == "member_expression" and parent.child_by_field_name("object") == top:
        return True
    if parent.type == "subscript_expression" and parent.child_by_field_name("object") == top:
        return True
    holder = parent.parent if parent.type == "array" else parent
    if holder is not None and holder.type == "arguments" and holder.parent is not None:
        call = holder.parent
        fn = call.child_by_field_name("function")
        if fn is not None:
            text = _text(fn)
            if re.fullmatch(r"Promise\s*\.\s*(all|allSettled|race|any)", text):
                return True
            outer = call.parent
            if text == "expect" and outer is not None and outer.type == "member_expression":
                prop = outer.child_by_field_name("property")
                if prop is not None and _text(prop) in {"resolves", "rejects"}:
                    return True
    return False


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


def _bound_read_promise(top: Node) -> bool:
    """The read is bound (``const p = <read>``) and ``p`` is later used as a Promise
    in the same scope: member access, ``await``, a ``Promise.*`` argument, or
    returned from a function annotated Promise."""
    parent = top.parent
    if parent is None or parent.type != "variable_declarator" or parent.child_by_field_name("value") != top:
        return False
    target = parent.child_by_field_name("name")
    if target is None or target.type != "identifier":
        return True  # a destructured binding of the result: not understood
    name = _text(target)
    scope = parent.parent.parent if parent.parent is not None else None
    if scope is None:
        return False
    for node in ts_syntax._walk(scope):
        if node.type != "identifier" or _text(node) != name or node == target:
            continue
        if _use_site_promise(node) or (node.parent is not None and node.parent.type == "await_expression"):
            return True
        holder = node.parent
        if holder is not None and holder.type == "return_statement":
            function = _enclosing_function(holder)
            if function is not None and return_kind(function.child_by_field_name("return_type")) == "promise":
                return True
    return False


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


def decide(repo: _Repo, path: str, call: Node, callee: Node, accessor: str) -> tuple[str, str]:
    """``(decision, why)``; ``why`` says what stopped a LEAVE."""
    top, promise_cast = _unwrap(call)
    if _boolean_consumed(top):
        return LITERAL, ""
    if promise_cast:
        return LEAVE, "it is cast to a type other than boolean"
    if _use_site_promise(top):
        return LEAVE, "its result is used as a Promise or as an object"
    if _bound_read_promise(top):
        return LEAVE, "its result is bound to a name that is later used as a Promise"
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
    if repo.possibly_async.get(accessor):
        return LEAVE, f"a declaration of {accessor} in the repository is, or could be, async"
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
    reason = (
        f"an un-awaited read of {first.accessor} whose result may be a Promise: {first.why}"
        f" ({len(left)} such read{'s' if len(left) != 1 else ''})"
    )
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
