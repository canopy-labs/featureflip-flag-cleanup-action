"""TypeScript and JavaScript profile for keyless wrappers (see
:mod:`flag_cleanup.wrapper_inline`).

A wrapper is one of these, with a whole body that is one read ``rules/ts.toml``
folds in this run (``boolVariation`` or ``useFeatureFlag``, or a named
accessor, with the key first, as a literal or a same-file or proven cross-file
constant):

* a top-level ``function f(...) { return <read>; }``;
* a top-level ``const f = (...) => <read>`` (or ``=> { return <read>; }``, or
  ``= function (...) { return <read>; }``);
* a non-``async`` method of a non-generic class declaration, static or not,
  or a field of one holding such a function (``m = (...) => <read>``);
* a method of the object literal a top-level ``const`` holds, or a key of it
  holding such a function. The object is the member's owner, and is called
  through like a class with a static method.

A JavaScript wrapper's own declaration file (``flags.d.ts`` beside
``flags.js``) describes it rather than competing with it: its callers are
inlined, and the declarations describing it go with the wrapper. When the
run may not rewrite that file, or one of them shares its statement with
another declaration, the wrapper stays with them (``declaration-file``).

``getBooleanValue`` is never a wrapper body: the form the rules fold is
awaited, which a wrapper would return as a promise, and the synchronous form
is folded only behind an import gate this profile does not repeat.

TypeScript's class types are structural, so a written type proves nothing on
its own: anything with the same members can stand in for the class. The proof
leans instead on the name. If no other member, property, object key,
interface signature or module-level binding of that name exists in the
repository's TS and JS, nothing else can be called at a call that
type-checks. File-level facts (bindings, imports, receiver classes) are in
:mod:`flag_cleanup.ts_scope`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, cross_file_ts, key_const, mock_stubs, ts_async_reads, ts_emptied_tests, ts_scope, ts_tests, ts_workspace
from flag_cleanup import wrapper_inline as wi
from flag_cleanup.ts_scope import text as _text

logger = logging.getLogger(__name__)

#: ``f`` in a call or a binding is an ``identifier``; ``m`` in ``x.m`` and a
#: class member's or object key's name is a ``property_identifier``; and
#: ``const { m } = x`` takes a method off its object with a
#: ``shorthand_property_identifier_pattern``.
MENTION_TYPES = frozenset(
    {"identifier", "property_identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"}
)
#: The text of a string or of a template literal outside its ``${}`` holes:
#: a mention there is reflection, a spy's member name, or a route.
_STRINGS = frozenset({"string_fragment"})
_READ_CALLEES = frozenset({"boolVariation", "useFeatureFlag", "useBooleanFlagValue"})
_FUNCTION_VALUES = ("arrow_function", "function_expression", "function")


def _read(path: str) -> str | None:
    """The file's text with its line endings as they are, like every reader
    on the wrapper path, so a span taken from it is an offset into the file
    the sentinel pre-pass marks."""
    from flag_cleanup import mock_stubs

    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _has(node: Node, *tokens: str) -> bool:
    return any(c.type in tokens for c in node.children)


def _callee_name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "member_expression":
        if function.child_by_field_name("optional_chain") is not None:
            return None
        function = function.child_by_field_name("property")
    if function is None or function.type not in ("identifier", "property_identifier"):
        return None
    return _text(function)


def _only_module_binding(root: Node, name: str) -> bool:
    """Whether the file binds ``name`` exactly once, as a module-level
    declarator: a parameter, an inner ``const`` or a second binding shadows it."""
    found = ts_scope.bindings(root, name)
    if len(found) != 1:
        return False
    declarator = found[0].parent
    declaration = declarator.parent if declarator is not None else None
    return (
        declarator is not None
        and declarator.type == "variable_declarator"
        and declaration is not None
        and declaration.type == "lexical_declaration"
        and ts_scope.top_level(declaration)
    )


def read_test(
    path: str,
    source: str,
    flag_key: str,
    accessors: tuple[str, ...],
    const_ok: bool,
    external: Iterable[str],
    *,
    loose: bool = False,
    promise_reads: frozenset[tuple[str, int, int]] = frozenset(),
) -> Callable[[Node], bool]:
    """Whether a ``call_expression`` is a read of ``flag_key`` this run folds.

    ``external`` holds the texts the cross-file resolution proved name the key
    in this file (``Keys.X``, ``X``). ``loose`` accepts any identifier or
    member expression as the key, a superset for widening the undo snapshot.

    The test carries a second one, ``promise_read``: whether a call is one of
    ``promise_reads`` (``(resolved path, start, end)``), a read the run folds
    to ``Promise.resolve(<literal>)``. Only a factory's members take it as a
    body (:func:`_factory_wrappers`)."""
    root = ts_scope.parse(source, path)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), ts_scope.grammar(path), flag_key)}
        if const_ok
        else set()
    )
    unshadowed = {n for n in consts if _only_module_binding(root, n)}
    names = _READ_CALLEES | set(accessors)
    texts = set(external)

    def is_read(node: Node) -> bool:
        if node.type != "call_expression" or node.child_by_field_name("type_arguments") is not None:
            return False
        if _has(node, "?."):
            return False
        if _callee_name(node) not in names:
            return False
        arguments = node.child_by_field_name("arguments")
        first = next(
            (c for c in ([] if arguments is None else arguments.named_children) if c.type != "comment"), None
        )
        if first is None:
            return False
        if cross_file_ts._string_value(first) == flag_key:
            return True
        if first.type == "identifier":
            return loose or _text(first) in unshadowed or _text(first) in texts
        if first.type == "member_expression":
            return loose or _text(first) in texts
        return False

    resolved = str(Path(path).resolve())
    promised = {(start, end) for where, start, end in promise_reads if where == resolved}

    def promise_read(node: Node) -> bool:
        return node.type == "call_expression" and (node.start_byte, node.end_byte) in promised

    is_read.promise_read = promise_read  # type: ignore[attr-defined]
    return is_read


def _returns_boolean(declaration: Node) -> bool:
    written = declaration.child_by_field_name("return_type")
    if written is None:
        return True
    inner = next((c for c in written.named_children if c.type != "comment"), None)
    return inner is not None and inner.type == "predefined_type" and _text(inner) == "boolean"


def _single_read(body: Node | None, is_read: Callable[[Node], bool]) -> bool:
    """Whether ``body`` is exactly ``{ return <read>; }``, or ``<read>`` itself
    for an arrow's expression body."""
    if body is None:
        return False
    if body.type != "statement_block":
        return is_read(body)
    statements = [c for c in body.named_children if c.type != "comment"]
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    returned = [c for c in statements[0].named_children if c.type != "comment"]
    return len(returned) == 1 and is_read(returned[0])


def _plain_function(node: Node) -> bool:
    """No ``async``, no ``*`` and no type parameters: a function whose call
    is the read's value."""
    return not _has(node, "async", "*") and node.child_by_field_name("type_parameters") is None


def _function_wrapper(node: Node, is_read: Callable[[Node], bool]) -> Node | None:
    """The name node of a top-level function wrapper ``node`` declares."""
    if node.type == "function_declaration":
        if _plain_function(node) and _returns_boolean(node) and _single_read(node.child_by_field_name("body"), is_read):
            return node.child_by_field_name("name")
        return None
    if node.type != "lexical_declaration" or not _has(node, "const"):
        return None
    declarators = [c for c in node.named_children if c.type == "variable_declarator"]
    if len(declarators) != 1:
        return None
    name = declarators[0].child_by_field_name("name")
    value = declarators[0].child_by_field_name("value")
    if name is None or name.type != "identifier" or declarators[0].child_by_field_name("type") is not None:
        return None
    if value is None or value.type not in _FUNCTION_VALUES or not _plain_function(value) or not _returns_boolean(value):
        return None
    return name if _single_read(value.child_by_field_name("body"), is_read) else None


def _decorator_name(decorator: Node) -> str:
    """``Get`` for ``@Get``, ``@Get(...)``, ``@http.Get`` or ``@http.Get(...)``."""
    value = next((c for c in decorator.named_children if c.type != "comment"), None)
    if value is not None and value.type == "call_expression":
        value = value.child_by_field_name("function")
    if value is not None and value.type == "member_expression":
        value = value.child_by_field_name("property")
    return _text(value if value is not None else decorator).lstrip("@")


def decorators(member: Node) -> tuple[str, ...]:
    """The names of the decorators on a class member. The grammar makes a
    method's each a sibling of the method in the class body, the ``decorator``
    nodes right before it, and a field's each a child of the field."""
    names: list[str] = []
    sibling = member.prev_named_sibling
    while sibling is not None and sibling.type in ("decorator", "comment"):
        if sibling.type == "decorator":
            names.insert(0, _decorator_name(sibling))
        sibling = sibling.prev_named_sibling
    return tuple(names) + tuple(_decorator_name(c) for c in member.named_children if c.type == "decorator")


def _plain_method(member: Node, is_read: Callable[[Node], bool]) -> bool:
    """A ``method_definition`` named by a plain identifier, not a constructor,
    accessor, ``async`` or generator method, whose body is the read."""
    name = member.child_by_field_name("name")
    if name is None or name.type != "property_identifier" or _text(name) == "constructor":
        return False
    if _has(member, "get", "set", "async", "*") or member.child_by_field_name("type_parameters") is not None:
        return False
    return _returns_boolean(member) and _single_read(member.child_by_field_name("body"), is_read)


def _function_value(value: Node | None, is_read: Callable[[Node], bool]) -> bool:
    """An arrow or function expression whose call is the read's value."""
    return (
        value is not None
        and value.type in _FUNCTION_VALUES
        and _plain_function(value)
        and _returns_boolean(value)
        and _single_read(value.child_by_field_name("body"), is_read)
    )


def _field_wrapper(member: Node, is_read: Callable[[Node], bool]) -> bool:
    """A class field ``m = (...) => <read>``: an own property of each instance
    (or of the class, ``static``), called like a method. An optional or
    definite (``?``/``!``) field, a typed one and a private name are not."""
    name = member.child_by_field_name("name")
    if name is None or name.type != "property_identifier" or _has(member, "?", "!", "declare", "abstract"):
        return False
    return member.child_by_field_name("type") is None and _function_value(member.child_by_field_name("value"), is_read)


def _method_wrappers(cls: Node, is_read: Callable[[Node], bool]) -> list[Node]:
    if cls.type != "class_declaration" or cls.child_by_field_name("type_parameters") is not None:
        return []
    body = cls.child_by_field_name("body")
    found = []
    for member in [] if body is None else body.named_children:
        if member.type == "method_definition" and _plain_method(member, is_read):
            found.append(member)
        elif member.type == "public_field_definition" and _field_wrapper(member, is_read):
            found.append(member)
    return found


def object_literal(declaration: Node) -> tuple[Node, Node] | None:
    """``(name, object)`` of a ``const`` declaring one untyped name with an
    object literal: ``const flags = { ... }``."""
    if declaration.type != "lexical_declaration":
        return None
    declarators = [c for c in declaration.named_children if c.type == "variable_declarator"]
    if len(declarators) != 1 or not ts_scope.holds_object_literal(declarators[0]):
        return None
    name = declarators[0].child_by_field_name("name")
    if name is None or name.type != "identifier":
        return None
    return name, declarators[0].child_by_field_name("value")


def _member_name(member: Node) -> Node | None:
    return member.child_by_field_name("key" if member.type == "pair" else "name")


def _may_replace(member: Node) -> bool:
    """A spread or a computed key: either may put a value under any name."""
    if member.type == "spread_element":
        return True
    key = _member_name(member)
    return key is not None and key.type == "computed_property_name"


def _object_wrappers(declaration: Node, is_read: Callable[[Node], bool]) -> list[Node]:
    """The members of a top-level ``const`` object literal whose whole body is
    the read: ``m(...) { return <read>; }`` or ``m: (...) => <read>``.

    A spread or a computed key anywhere in the literal may put another value
    under the member's name, so it holds none."""
    held = object_literal(declaration)
    if held is None:
        return []
    return _literal_wrappers(held[1], is_read)


def _literal_wrappers(literal: Node, is_read: Callable[[Node], bool]) -> list[Node]:
    members = [c for c in literal.named_children if c.type != "comment"]
    if any(_may_replace(m) for m in members):
        return []
    found = []
    for member in members:
        if member.type == "method_definition" and _plain_method(member, is_read):
            found.append(member)
        elif member.type == "pair":
            key = _member_name(member)
            if key is not None and key.type == "property_identifier" and _function_value(member.child_by_field_name("value"), is_read):
                found.append(member)
    return found


def _body_read(member: Node) -> Node | None:
    """The read a wrapper member's body returns."""
    function = member.child_by_field_name("value") if member.type == "pair" else member
    body = None if function is None else function.child_by_field_name("body")
    if body is not None and body.type == "statement_block":
        statements = [c for c in body.named_children if c.type != "comment"]
        body = next((c for c in statements[0].named_children if c.type != "comment"), None) if statements else None
    return body


def _factory_wrappers(function: Node, is_read: Callable[[Node], bool]) -> list[tuple[Node, bool]]:
    """``(member, promise)`` for each member of the object literal a factory
    function returns (:func:`ts_scope.factory_object`) whose whole body is the
    read, ``promise`` when the read is one the run keeps a Promise.

    Such a member is its own wrapper on every object the factory makes: each
    call builds the same members afresh, and once the read is folded none of
    them depends on what the factory was handed."""
    literal = ts_scope.factory_object(function)
    if literal is None:
        return []
    promise_read = getattr(is_read, "promise_read", None)
    found = [(m, False) for m in _literal_wrappers(literal, is_read)]
    if promise_read is not None:
        sync = {m.start_byte for m, _ in found}
        for member in _literal_wrappers(literal, lambda n: is_read(n) or promise_read(n)):
            read = _body_read(member)
            if member.start_byte in sync or read is None or not promise_read(read):
                continue
            found.append((member, True))
    return sorted(found, key=lambda pair: pair[0].start_byte)


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = ts_scope.parse(source, path)
    module = Path(path).name.split(".")[0]
    found: list[wi.Wrapper] = []
    for statement in root.named_children:
        if statement.type == "export_statement" and _has(statement, "default"):
            # Its callers import it under a name of their own (``import nc``,
            # ``ns.default``), which neither the proof nor the close-out sees.
            continue
        node = statement.child_by_field_name("declaration") if statement.type == "export_statement" else statement
        if node is None:
            continue
        name = _function_wrapper(node, is_read)
        if name is not None:
            found.append(
                wi.Wrapper(
                    path=path, language=ts_scope.grammar(path), name=_text(name), owner=module, package=path,
                    line=wi.line_of(statement), start=statement.start_byte, end=statement.end_byte, kind="function",
                )
            )
            continue
        cls_name = node.child_by_field_name("name") if node.type == "class_declaration" else None
        for member in _method_wrappers(node, is_read):
            found.append(
                wi.Wrapper(
                    path=path, language=ts_scope.grammar(path), name=_text(member.child_by_field_name("name")),
                    owner=_text(cls_name), package=path, line=wi.line_of(member), start=member.start_byte,
                    end=member.end_byte, static=_has(member, "static"), kind="method",
                    # A framework may call a decorated method (a route, a handler),
                    # and deleting it would leave the decorator with nothing to
                    # decorate, so it is kept: ``annotated``.
                    annotations=decorators(member),
                )
            )
        for member, promise in _factory_wrappers(node, is_read) if node.type == "function_declaration" else ():
            # Called through an object the factory made: an instance of it.
            found.append(
                wi.Wrapper(
                    path=path, language=ts_scope.grammar(path), name=_text(_member_name(member)),
                    owner=ts_scope.factory_owner(_text(node.child_by_field_name("name"))), package=path,
                    line=wi.line_of(member), start=member.start_byte, end=member.end_byte, kind="method",
                    promise=promise,
                )
            )
        held = object_literal(node)
        for member in _object_wrappers(node, is_read):
            # Called through the object itself, like a static method through its class.
            found.append(
                wi.Wrapper(
                    path=path, language=ts_scope.grammar(path), name=_text(_member_name(member)),
                    owner=_text(held[0]), package=path, line=wi.line_of(member), start=member.start_byte,
                    end=member.end_byte, static=True, kind="method",
                )
            )
    declarations = cross_file_ts.declaration_file(path)
    if found and declarations is not None and Path(declarations).is_file():
        resolved = ts_scope._resolved(declarations)
        found = [replace(w, declaration_file=resolved) for w in found]
    return found


# --- The repository index -----------------------------------------------------

#: A path a JS test runner picks up as a test file, whatever its content.
_TEST_PATH = re.compile(r"(^|[/\\])__tests__[/\\]|\.(test|spec)\.[cm]?[jt]sx?$")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


#: Helpers, fixtures and stories that stub without holding a titled test.
_SUPPORT_PATH = re.compile(
    r"(setupTests|jest\.setup|vitest\.setup|\.stories|\.cy|\.e2e)\."
    r"|(^|[/\\])(__mocks__|cypress|e2e|test|tests)[/\\]"
)
#: A call into a mocking library: a file spelling one stubs, whatever it is named.
_MOCKING_DSL = re.compile(r"\b(jest\.|vi\.(mock|spyOn|fn)\b|sinon\.|cy\.stub\b)")


#: A call replacing a whole module, or handing a test an automock of one:
#: Jest's and Vitest's automock, with or without a factory, their
#: ``createMockFromModule``/``requireMock``/``importMock``, and the
#: ``mock.module`` of Node's and Bun's test runners. It never names the
#: wrapper, only its module.
_MODULE_MOCK = re.compile(
    r"(?<![\w$.])(jest\.(mock|doMock|unstable_mockModule|setMock|createMockFromModule|requireMock)"
    r"|vi\.(mock|doMock|importMock))\s*\("
    r"|(?<![\w$])mock\s*\.\s*module\s*\("
)
_MODULE_MOCK_CALLEES = frozenset(
    {
        "jest.mock", "jest.doMock", "jest.unstable_mockModule", "jest.setMock", "jest.createMockFromModule",
        "jest.requireMock", "vi.mock", "vi.doMock", "vi.importMock", "mock.module",
    }
)


def _is_module_mock(function: Node) -> bool:
    callee = "".join(_text(function).split())
    return callee in _MODULE_MOCK_CALLEES or callee.endswith(".mock.module")


def _only_argument(call: Node) -> Node | None:
    arguments = call.child_by_field_name("arguments")
    found = [c for c in ([] if arguments is None else arguments.named_children) if c.type != "comment"]
    return found[0] if len(found) == 1 else None


def _mock_specifier(first: Node | None) -> str | None:
    """The module a mock call names: a plain string, or one wrapped in
    ``import("...")`` (Vitest's typed form) or ``require.resolve("...")``."""
    spec = cross_file_ts._string_value(first)
    if spec is not None or first is None or first.type != "call_expression":
        return spec
    function = first.child_by_field_name("function")
    if function is None or not (function.type == "import" or "".join(_text(function).split()) == "require.resolve"):
        return None
    return cross_file_ts._string_value(_only_argument(first))


def _mocks_module(
    root: Node, path: str, repo_dir: str, wrapper: wi.Wrapper, packages: Callable[[], frozenset[str]] = frozenset
) -> list[Node]:
    """Every ``jest.mock("...")``-like call in a file that may hand its tests
    something else for the wrapper, without spelling its name.

    A call whose target resolves to the wrapper's own module does, unless it
    is a ``jest.mock``/``vi.mock`` factory the test-double pass can read and
    no entry of it may be the wrapper (:func:`_factory_hides`). The other
    callees (``setMock``, ``createMockFromModule``, ``requireMock``,
    ``importMock``, ``mock.module``) always do. For a method, so does a mock
    of the module holding the instance it is called on, which is any module
    spelling the class, or that cannot be read.

    Unresolved is refused, not skipped. A template, a variable, a relative
    path that names no file and an alias only a bundler or test config
    defines all reach some module this run cannot rule out, and a stub there
    turns a test pinned to the removed branch red. The one unresolved target
    that cannot be the wrapper's module is a package: a Node built-in, or a
    name ``packages()`` (every dependency a ``package.json`` in the checkout
    declares) holds, unless it ends in the wrapper file's own name: a
    ``moduleNameMapper`` or ``baseUrl`` alias can shadow a package."""
    owner = _word(ts_scope.factory_name(wrapper.owner) or wrapper.owner) if wrapper.kind == "method" else None
    found = []
    for node in cross_file_keys.walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or not _is_module_mock(function):
            continue
        first = next(iter(mock_stubs.ts_arguments(node)), None)
        spec = _mock_specifier(first) if first is not None and first.type != "spread_element" else None
        # Only these take a factory the test-double pass resolves.
        factory = mock_stubs.ts_callee(node) in mock_stubs.TS_MODULE_MOCKS
        target = None if spec is None else ts_scope.resolve(path, spec, repo_dir)
        if spec is None:
            hides = True
        elif target is None:
            hides = not _is_package(spec, packages) or _names_wrapper_file(spec, wrapper.path)
        elif _same_file(target, wrapper.path):
            hides = not factory or _factory_hides(node, wrapper.name if wrapper.kind == "function" else None)
        else:
            held = _read(target) if owner is not None else None
            hides = (
                owner is not None
                and (held is None or bool(owner.search(held)))
                and (not factory or _factory_hides(node, None, wrapper.name if ts_scope.factory_name(wrapper.owner) else None))
            )
        if hides:
            found.append(node)
    return found


#: Node's built-in modules, which a bare specifier names before any package
#: or alias does.
_NODE_BUILTINS = frozenset(
    {
        "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto",
        "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https",
        "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode", "querystring",
        "readline", "repl", "stream", "string_decoder", "sys", "test", "timers", "tls", "trace_events",
        "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
    }
)
_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def _package_name(spec: str) -> str | None:
    """``lodash`` for ``lodash/fp``, ``@scope/name`` for ``@scope/name/sub``."""
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 and len(parts[0]) > 1 and parts[1] else None
    return parts[0] or None


def _is_package(spec: str, packages: Callable[[], frozenset[str]]) -> bool:
    """Whether an unresolved specifier names a Node built-in or a declared
    dependency. Anything else bare (``flags``, ``~/flags``, ``#flags``) may
    be an alias of the wrapper's module."""
    if spec.startswith("node:"):
        return spec[len("node:"):].split("/")[0] in _NODE_BUILTINS
    name = _package_name(spec)
    return name is not None and (name in _NODE_BUILTINS or name in packages())


#: Lockfiles: a package one lists may be installed, hoisted too, where a
#: runtime looks before any ``baseUrl`` directory.
_LOCKFILES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb"}
)
#: A package's name in any of them: an npm ``node_modules/<name>`` key, or a
#: ``<name>@<version>`` entry (Yarn, pnpm, Bun). Over-matching only refuses.
_LOCKED_NAME = re.compile(
    r"node_modules/((?:@[^/\"'\s]+/)?[^/\"'\s@]+)|(?:^|[\s\"'/,])((?:@[\w.-]+/)?[\w.-]+)@"
    # pnpm's lockfile v5 keys: `/name/1.0.0:`.
    r"|^\s+/((?:@[\w.-]+/)?[\w.-]+)/\d",
    re.MULTILINE,
)
#: Checkout root -> :func:`installable_packages`, read once per checkout.
_INSTALLABLE: dict[str, frozenset[str]] = {}


def installable_packages(repo_dir: str) -> frozenset[str]:
    """Every package a ``package.json`` in the checkout declares or one of its
    lockfiles lists, so possibly installed whether declared or not."""
    from flag_cleanup.piranha_runner import _SKIP_DIRS, checkout_root, source_files

    root = str(Path(checkout_root(repo_dir)).resolve())
    if root not in _INSTALLABLE:
        names = set(declared_packages(repo_dir)) | _own_names(repo_dir)
        for path in source_files(Path(root), (".json", ".lock", ".yaml", ".lockb"), _SKIP_DIRS):
            if path.name not in _LOCKFILES:
                continue
            try:
                # Bun's binary lockfile spells each name as plain bytes.
                text = path.read_bytes().decode("utf-8" if path.suffix != ".lockb" else "latin-1")
            except (OSError, UnicodeDecodeError):
                continue
            if path.suffix == ".lockb":
                names.update(re.findall(r"(?:@[\w.-]+/)?[\w.-]+", text))
                continue
            names.update(a or b or c for a, b, c in _LOCKED_NAME.findall(text))
        _INSTALLABLE[root] = frozenset(names)
    return _INSTALLABLE[root]


def _own_names(repo_dir: str) -> set[str]:
    """The ``name`` of every ``package.json`` in the checkout: Node resolves a
    package's import of its own name to itself."""
    import json

    names: set[str] = set()
    for path in config_files(repo_dir):
        if path.name != "package.json":
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
            names.add(manifest["name"])
    return names


def may_be_package(reader_path: str, spec: str, repo_dir: str) -> bool:
    """Whether a runtime may read a bare specifier as a package although the
    compiler finds it under ``baseUrl``. Jest's ``moduleDirectories``, and a
    bundler listing ``node_modules`` first, look there before the alias does:
    a Node built-in, a package the checkout declares or locks, or a
    ``node_modules/<name>`` above the reader."""
    from flag_cleanup.piranha_runner import checkout_root

    if spec.startswith("node:"):
        return True
    name = _package_name(spec)
    if name is None or name in _NODE_BUILTINS or name in installable_packages(repo_dir):
        return True
    root = Path(checkout_root(repo_dir)).resolve()
    directory = Path(reader_path).resolve().parent
    while True:
        if (directory / "node_modules" / name).exists():
            return True
        if directory == root or directory.parent == directory:
            return False
        directory = directory.parent


def declared_packages(repo_dir: str, configs: list[Path] | None = None) -> frozenset[str]:
    """Every dependency any ``package.json`` in the checkout declares, of any
    kind. One that cannot be read declares nothing, which only refuses more."""
    import json

    names: set[str] = set()
    for path in config_files(repo_dir) if configs is None else configs:
        if path.name != "package.json":
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        for field_name in _DEPENDENCY_FIELDS:
            section = manifest.get(field_name)
            if isinstance(section, dict):
                names.update(k for k in section if isinstance(k, str))
    return frozenset(names)


#: Libraries that stub a whole class from its name, never spelling a member:
#: sinon's ``createStubInstance(C)``, jest-mock-extended's ``mock<C>()``,
#: ts-mockito's ``mock(C)``, ``createMock<C>()``, ts-sinon's
#: ``stubInterface<C>()``, ``Substitute.for<C>()`` and testdouble's
#: ``td.object(C)``.
_CLASS_STUB_CALLEES = frozenset(
    {
        "createStubInstance", "stubInterface", "stubConstructor", "stubObject", "mock", "mockDeep",
        "createMock", "for", "object", "constructor", "imitate", "instance",
    }
)
#: The text that makes a file worth parsing for them. The four generic names
#: are looked for only on their usual receiver, or every class (``constructor(``)
#: and every loop (``for (``) in the repository would be parsed.
_CLASS_STUB = re.compile(
    r"(?<![\w$])(createStubInstance|stubInterface|stubConstructor|stubObject|mock|mockDeep|createMock)\s*[<(]"
    r"|(?<![\w$])(Substitute|td)\s*\.\s*(for|object|constructor|imitate|instance)\s*[<(]"
)


def _class_stubs(file: ts_scope.File, wrapper: wi.Wrapper, reach: _Reach) -> list[Node]:
    """Every class-stub call in a file whose type or value arguments name
    the wrapper's class: directly, through an import of it, or as a member
    of the wrapper's module (``ns.Flags``, ``require("./flags").Flags``)."""
    found = []
    for node in cross_file_keys.walk(file.root):
        if node.type != "call_expression" or _callee_name(node) not in _CLASS_STUB_CALLEES:
            continue
        parts = [node.child_by_field_name("type_arguments"), node.child_by_field_name("arguments")]
        for part in (p for p in parts if p is not None):
            if any(_names_class(n, file, wrapper.owner, reach) for n in cross_file_keys.walk(part)):
                found.append(node)
                break
    return found


def _names_class(node: Node, file: ts_scope.File, owner: str, reach: _Reach | None = None) -> bool:
    if node.type in ("member_expression", "nested_type_identifier"):
        name = node.child_by_field_name("property" if node.type == "member_expression" else "name")
        module = node.child_by_field_name("object" if node.type == "member_expression" else "module")
        return (
            reach is not None and name is not None and module is not None and _text(name) == owner
            and reach(module) == _WRAPPER
        )
    if node.type not in ("identifier", "type_identifier"):
        return False
    if _text(node) == owner:
        return True
    imported = file.import_of(_text(node))
    return imported is not None and imported.imported == owner


# --- What an expression in a test can reach -----------------------------------------------

_WRAPPER, _OTHER, _UNKNOWN = "wrapper", "other", "unknown"
_Reach = Callable[[Node], str]

#: Globals a test spies on routinely, none of which can be a module's exports.
_GLOBALS = frozenset(
    {
        "console", "window", "globalThis", "global", "self", "document", "navigator", "location", "history",
        "process", "Math", "Date", "JSON", "Object", "Array", "Promise", "Reflect", "Intl", "localStorage",
        "sessionStorage", "performance", "crypto",
    }
)
_TRANSPARENT = frozenset({"parenthesized_expression", "as_expression", "non_null_expression", "satisfies_expression"})


def _reach(path: str, file: ts_scope.File, wrapper: wi.Wrapper, repo_dir: str, packages: Callable[[], frozenset[str]]) -> _Reach:
    """Whether an expression can be the wrapper's module, its class, its
    prototype or an instance of it (``wrapper``), provably cannot
    (``other``), or cannot be told (``unknown``)."""
    module = str(Path(wrapper.path).resolve())

    def of_specifier(node: Node | None) -> str:
        spec = cross_file_ts._string_value(node)
        if spec is None:
            return _UNKNOWN
        target = ts_scope.resolve(path, spec, repo_dir)
        if target is None:
            return _OTHER if _is_package(spec, packages) else _UNKNOWN
        return _WRAPPER if str(Path(target).resolve()) == module else _OTHER

    def reach(node: Node) -> str:
        while node.type in _TRANSPARENT and node.named_children:
            node = node.named_children[0]
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            return _UNKNOWN if obj is None or node.child_by_field_name("optional_chain") is not None else (
                _OTHER if reach(obj) == _OTHER else reach(obj)
            )
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function is not None and function.type == "identifier" and _text(function) == "require":
                return of_specifier(_only_argument(node))
            return _UNKNOWN
        if node.type != "identifier":
            return _UNKNOWN
        name = _text(node)
        imported = file.import_of(name)
        if imported is not None:
            if imported.module is None:
                return _UNKNOWN
            return _WRAPPER if str(Path(imported.module).resolve()) == module else _OTHER
        if name in (wrapper.owner, wrapper.name) and file.path == wrapper.path:
            return _WRAPPER
        if file.binding(name) is None and name in _GLOBALS:
            return _OTHER
        return _UNKNOWN

    return reach


#: ``sinon.stub(obj)`` (one argument) stubs every method on ``obj``;
#: ``stub``/``spyOn`` with a computed member name stub whatever it names.
_OBJECT_STUB = re.compile(r"(?<![\w$])(stub|spyOn)\s*\(")
#: Dependency-injection test beds that replace every dependency of the unit
#: under test with a mock: NestJS ``.useMocker(createMock)`` and
#: ``@suites/unit``'s ``TestBed.solitary``/``sociable``.
_AUTO_MOCKER = re.compile(r"(?<![\w$])(useMocker|solitary|sociable)\s*\(")
_AUTO_MOCKER_CALLEES = frozenset({"useMocker", "solitary", "sociable"})


def _object_stubs(file: ts_scope.File, reach: _Reach) -> list[Node]:
    found = []
    for node in cross_file_keys.walk(file.root):
        if node.type != "call_expression" or _callee_name(node) not in ("stub", "spyOn"):
            continue
        arguments = node.child_by_field_name("arguments")
        args = [c for c in ([] if arguments is None else arguments.named_children) if c.type != "comment"]
        if not args or any(a.type == "spread_element" for a in args[:2]):
            if args:
                found.append(node)
            continue
        if len(args) == 1 and _callee_name(node) == "spyOn":
            continue
        # A literal member name is a mention the name scan judges.
        if len(args) >= 2 and cross_file_ts._string_value(args[1]) is not None:
            continue
        if reach(args[0]) != _OTHER:
            found.append(node)
    return found


def _auto_mockers(file: ts_scope.File) -> list[Node]:
    return [
        n for n in cross_file_keys.walk(file.root)
        if n.type == "call_expression" and _callee_name(n) in _AUTO_MOCKER_CALLEES
    ]


# --- Test-runner configuration -------------------------------------------------------------

_CONFIG_NAME = re.compile(r"^(jest|vite|vitest)\.(config|workspace)\.[cm]?[jt]s(on)?$|^package\.json$")
_CONFIG_SUFFIXES = (".json", ".js", ".cjs", ".mjs", ".ts", ".cts", ".mts")
_MOCK_DIR = re.compile(r"(^|[/\\])(__mocks__|mocks)([/\\]|$)")


def config_files(repo_dir: str) -> list[Path]:
    """Every ``package.json`` and Jest/Vite/Vitest config in the checkout."""
    from flag_cleanup.piranha_runner import _SKIP_DIRS, checkout_root, source_files

    return [
        p for p in source_files(Path(checkout_root(repo_dir)), _CONFIG_SUFFIXES, _SKIP_DIRS)
        if _CONFIG_NAME.match(p.name)
    ]


def _alias_may_stub(spec: str, config: Path, wrapper: wi.Wrapper, repo_dir: str) -> bool:
    """Whether one key or value of a ``moduleNameMapper``/``alias`` may
    map the wrapper's module to a stand-in: it points into a mocks
    directory, ends in the module's own name, or resolves to its file."""
    if _MOCK_DIR.search(spec):
        return True
    module = Path(wrapper.path)
    stem = module.name.split(".")[0]
    if stem == "index":
        stem = module.parent.name
    last = re.split(r"[/\\]", spec)[-1]
    if re.sub(r"[\^$()*+?\[\]{}|]", "", last.split(".")[0]) == stem:
        return True
    target = ts_scope.resolve(str(config), spec, repo_dir)
    return target is not None and str(Path(target).resolve()) == str(module.resolve())


def _json_config_doubles(config: Path, text: str, wrapper: wi.Wrapper, repo_dir: str) -> bool:
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    settings = data.get("jest") if config.name == "package.json" else data
    if not isinstance(settings, dict):
        return False
    if settings.get("automock") is True:
        return True
    mapper = settings.get("moduleNameMapper")
    if not isinstance(mapper, dict):
        return False
    specs = [k for k in mapper] + [
        s for v in mapper.values() for s in (v if isinstance(v, list) else [v]) if isinstance(s, str)
    ]
    return any(_alias_may_stub(s, config, wrapper, repo_dir) for s in specs)


def _script_config_doubles(config: Path, text: str, wrapper: wi.Wrapper, repo_dir: str) -> bool:
    root = ts_scope.parse(text, str(config))
    for pair in cross_file_keys.walk(root):
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        value = pair.child_by_field_name("value")
        if key is None or value is None:
            continue
        name = cross_file_ts._string_value(key) if key.type == "string" else _text(key)
        if name == "automock" and value.type == "true":
            return True
        if name not in ("moduleNameMapper", "alias"):
            continue
        for node in cross_file_keys.walk(value):
            spec = cross_file_ts._string_value(node) if node.type == "string" else (
                _text(node) if node.type == "property_identifier" else None
            )
            if spec is not None and _alias_may_stub(spec, config, wrapper, repo_dir):
                return True
    return False


def runner_config_doubles(repo_dir: str, wrapper: wi.Wrapper, configs: list[Path]) -> list[tuple[str, int, str]]:
    """Each test-runner config that can hand a test something other than
    the wrapper: Jest's ``automock: true``, or a ``moduleNameMapper`` /
    ``alias`` that may map its module to a stand-in."""
    found = []
    for config in configs:
        text = _read(str(config))
        if text is None:
            found.append((str(config), 1, "unreadable"))
            continue
        if config.suffix == ".json":
            hit = _json_config_doubles(config, text, wrapper, repo_dir)
        else:
            hit = _script_config_doubles(config, text, wrapper, repo_dir)
        if hit:
            found.append((str(config), 1, config.name))
    return found


# --- The wrapper's module handed on ------------------------------------------------


def _barrels(
    path: str, text: str, modules: set[str], repo_dir: str, named: bool = False
) -> list[tuple[int, int]]:
    """The byte span of each ``export * from`` one of ``modules`` in a file:
    a barrel, which re-exports the wrapper but never hands on a module object
    itself. ``export * as ns``, ``export type *`` and a file that does not
    parse are left to the escape scan.

    With ``named``, a plain ``export { a, b as c } from`` is one too: it hands
    on those exports and never the module. A key constant's scan asks for it,
    since the barrel's own line for a constant is judged where it is
    (:func:`flag_cleanup.cross_file_ts.references`); a function wrapper's does
    not, as its re-export has always been refused."""
    root = ts_scope.parse(text, path)
    if root.has_error:
        return []
    found = []
    for node in root.named_children:
        if node.type != "export_statement":
            continue
        starred = any(c.type == "*" for c in node.children)
        clause = any(c.type == "export_clause" for c in node.named_children)
        if not (starred or (named and clause)):
            continue
        if any(c.type in ("namespace_export", "type") for c in node.children):
            continue
        spec = cross_file_ts._string_value(node.child_by_field_name("source"))
        target = None if spec is None else cross_file_ts.resolve_specifier(path, spec, repo_dir, workspace=True)
        package = ts_workspace.reach(spec, repo_dir) if target is None and spec is not None else None
        if package is not None:
            # Whoever imports it: its proven entry, or else any file of the
            # package, which is followed like a barrel of each.
            if any(package.reaches(m) for m in modules):
                found.append((node.start_byte, node.end_byte))
        elif target is not None and str(Path(target).resolve()) in modules:
            found.append((node.start_byte, node.end_byte))
    return found


def _blanked(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with each byte span turned to spaces, every line kept."""
    raw = text.encode("utf-8")
    for start, end in spans:
        raw = raw[:start] + re.sub(rb"[^\n]", b" ", raw[start:end]) + raw[end:]
    return raw.decode("utf-8")


def module_escapes(repo_dir: str, wrapper: wi.Wrapper) -> list[tuple[str, int, str]]:
    """``(path, line, text)`` of each use that hands the wrapper's module on as
    an object (:func:`handed_on`): ``table[gate](u)`` after ``const table =
    flags as any`` calls the wrapper by a name no scan sees."""
    return handed_on(repo_dir, wrapper.path)


def handed_on(repo_dir: str, module_path: str, named: bool = False) -> list[tuple[str, int, str]]:
    """``(path, line, text)`` of each use that hands the module at
    ``module_path`` on as an object (:func:`cross_file_ts.module_escapes`),
    which can then reach any of its exports by a name computed at run time.

    A barrel's ``export * from`` the module is followed rather than refused:
    the barrel joins the modules judged, so a namespace of it handed on
    escapes in turn. A file that cannot be read escapes at its first line."""
    from flag_cleanup import source_index

    module = str(Path(module_path).resolve())
    modules: set[str] = set()
    frontier = {module}
    texts: dict[str, str | None] = {}
    followed: dict[str, list[tuple[int, int]]] = {}
    kinds = (*ts_scope.FAMILY_EXTENSIONS, *source_index.COMPONENT_SUFFIXES)
    while frontier:
        modules |= frontier
        found: set[str] = set()
        for path in cross_file_keys.mention_files(
            repo_dir,
            (),
            exclude=(),
            also=cross_file_ts.module_pattern(frontier, repo_dir),
            extra_suffixes=source_index.COMPONENT_SUFFIXES,
        ):
            if not path.endswith(kinds):
                continue
            if path not in texts:
                texts[path] = _read(path)
            text = texts[path]
            if text is None or not path.endswith(ts_scope.FAMILY_EXTENSIONS):
                continue
            spans = _barrels(path, text, frontier, repo_dir, named)
            if spans:
                followed.setdefault(path, []).extend(spans)
                found.add(str(Path(path).resolve()))
        frontier = found - modules
    pattern = cross_file_ts.module_pattern(modules, repo_dir)
    escapes: list[tuple[str, int, str]] = []
    for path, text in sorted(texts.items()):
        if text is None:
            escapes.append((path, 1, ""))
            continue
        lines = text.split("\n")
        judged = _blanked(text, followed.get(path, []))
        for refusal in cross_file_ts.module_escapes(path, judged, modules, repo_dir, pattern):
            escapes.append((path, refusal.line, lines[refusal.line - 1].strip()))
    return sorted(set(escapes))


def _stubs(path: str, text: str) -> bool:
    """Whether a file may stub: a test by name or content, a support file, or
    any file that calls a mocking library."""
    return (
        is_test_path(path)
        or bool(_SUPPORT_PATH.search(path))
        or bool(_MOCKING_DSL.search(text))
        or ts_tests.is_test_file(text, ts_scope.grammar(path))
    )


@dataclass
class Index:
    repo_dir: str = ""
    #: ``(path, start byte)`` of every declaration of the wrapper's name that
    #: another object could answer a call with: a function, a method (abstract
    #: too), a class field or parameter property, an object key or method, a
    #: module-level binding, an enum member. Locals are the binding rule's.
    declarations: list[tuple[str, int]] = field(default_factory=list)
    #: Every property or method signature of the name, in an interface or a
    #: type literal.
    interface_members: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, line, text)`` of each mention in a test file that is not a
    #: call: a spy's member name, a mock factory's key, a mock's property.
    test_doubles: list[tuple[str, int, str]] = field(default_factory=list)
    #: A file mentioning the name could not be read, or holds it inside a
    #: region it could not parse.
    unreadable: bool = False
    #: Every TS/JS file mentioning the name, resolved: where call sites are.
    parsed: list[str] = field(default_factory=list)
    #: Nothing here is another language this tool rewrites around TS.
    foreign: list[str] = field(default_factory=list)
    #: Files of any other kind that spell the name (components, templates).
    unparsed: list[str] = field(default_factory=list)
    #: Unused: TS has no second spelling of a name.
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` of each use handing a function wrapper's module
    #: on as an object (:func:`module_escapes`), which may call it unseen.
    escapes: list[tuple[str, int, str]] = field(default_factory=list)
    #: The JavaScript wrapper's own declaration file, resolved, when it has
    #: one: its declarations of the wrapper describe it rather than compete.
    declaration_file: str | None = None
    #: ``(path, line, text)`` of each use of an object-literal owner other
    #: than as a member's receiver (:func:`owner_escapes`).
    owner_escapes: list[tuple[str, int, str]] = field(default_factory=list)
    #: A factory member's escapes (:func:`factory_escapes`), computed only
    #: when :func:`qualify` gets that far: they are repository-wide scans,
    #: and a wrapper a test double already refuses never needs them.
    factory_escapes: Callable[[], list[tuple[str, int, str]]] | None = None
    #: Parsed files, shared by every proof of this wrapper.
    files: ts_scope.Files | None = None
    _packages: frozenset[str] | None = None
    _configs: list[Path] | None = None

    def configs(self) -> list[Path]:
        if self._configs is None:
            self._configs = config_files(self.repo_dir)
        return self._configs

    def packages(self) -> frozenset[str]:
        """The checkout's declared dependencies, read on first need."""
        if self._packages is None:
            self._packages = declared_packages(self.repo_dir, self.configs())
        return self._packages

    @classmethod
    def build(cls, repo_dir: str, wrapper: wi.Wrapper, settled: Mapping[str, Settled] | None = None) -> Index:
        """``settled``: the class mocks a test-double round settled
        (:func:`settled_mocks`), passed over when this indexes the result."""
        from flag_cleanup.piranha_runner import checkout_root

        index = cls(repo_dir=repo_dir, files=ts_scope.Files(checkout_root(repo_dir)))
        declarations = cross_file_ts.declaration_file(wrapper.path)
        if declarations is not None and Path(declarations).is_file():
            index.declaration_file = ts_scope._resolved(declarations)
        word = _word(wrapper.name)
        # As wide as the scan's own: a file found only by ``also`` is not a mention.
        mentions = cross_file_keys.mention_pattern([wrapper.name])
        from flag_cleanup import source_index

        # A class stub never spells the method, only its class, and an
        # auto-mocking test bed spells neither.
        # Neither reaches the object a factory makes, which is no class's
        # instance: a mock of its type is another object, and every call on
        # one is through a receiver the proof refuses.
        classy = wrapper.kind == "method" and ts_scope.factory_name(wrapper.owner) is None
        class_stub = _CLASS_STUB if classy else None
        auto_mocker = _AUTO_MOCKER if classy else None
        patterns = [p for p in (_MODULE_MOCK, _OBJECT_STUB, class_stub, auto_mocker) if p is not None]
        also = re.compile("|".join(f"(?:{p.pattern})" for p in patterns))
        index.test_doubles += runner_config_doubles(repo_dir, wrapper, index.configs())
        if wrapper.kind == "function":
            # A method is reached through an instance, never off the module.
            index.escapes = module_escapes(repo_dir, wrapper)
        if wrapper.kind == "method" and wrapper.static:
            index.owner_escapes = owner_escapes(repo_dir, wrapper, index.files)
        elif wrapper.kind == "method" and ts_scope.factory_name(wrapper.owner) is not None:
            index.factory_escapes = lambda: factory_escapes(repo_dir, wrapper, index.files)
        for path in cross_file_keys.mention_files(
            repo_dir,
            [wrapper.name],
            exclude=(),
            also=also,
            extra_suffixes=source_index.COMPONENT_SUFFIXES,
        ):
            text = _read(path)
            if text is None:
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            if path.endswith(ts_scope.FAMILY_EXTENSIONS):
                if _MODULE_MOCK.search(text):
                    index._add_module_mocks(path, text, wrapper)
                if class_stub is not None and class_stub.search(text):
                    index._add_class_stubs(path, text, wrapper, (settled or {}).get(str(Path(path).resolve())))
                if _OBJECT_STUB.search(text):
                    index._add_calls(path, _object_stubs(index.files.file(path, text), index._reach(path, text, wrapper)))
                if auto_mocker is not None and auto_mocker.search(text):
                    index._add_calls(path, _auto_mockers(index.files.file(path, text)))
                if mentions.search(text):
                    index.parsed.append(path)
                    index._add(path, text, wrapper)
            elif word.search(text):
                index.unparsed.append(path)
        return index

    def _add_module_mocks(self, path: str, text: str, wrapper: wi.Wrapper) -> None:
        """A ``jest.mock(...)`` can replace the wrapper in every test of a file
        without spelling its name: :func:`_mocks_module` says which do."""
        file = self.files.file(path, text)
        self._add_calls(path, _mocks_module(file.root, path, self.repo_dir, wrapper, self.packages))

    def _add_class_stubs(self, path: str, text: str, wrapper: wi.Wrapper, settled: Settled | None = None) -> None:
        """A ``createStubInstance(Flags)``-like call stubs every method of the
        class, the wrapper among them, without spelling its name. One a
        round settled, whose stub it deleted, is not counted."""
        calls = _class_stubs(self.files.file(path, text), wrapper, self._reach(path, text, wrapper))
        self._add_calls(path, [c for c in calls if settled is None or not _still_settled(c, text, settled)])

    def _reach(self, path: str, text: str, wrapper: wi.Wrapper) -> _Reach:
        return _reach(path, self.files.file(path, text), wrapper, self.repo_dir, self.packages)

    def _add_calls(self, path: str, calls: list[Node]) -> None:
        for call in calls:
            self.test_doubles.append((path, wi.line_of(call), wi.text_of(call)))

    def _add(self, path: str, text: str, wrapper: wi.Wrapper) -> None:
        file = self.files.file(path, text)
        name = wrapper.name
        word = _word(name)
        if file.root.has_error and any(
            n.type == "ERROR" and word.search(_text(n)) for n in cross_file_keys.walk(file.root)
        ):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        test = _stubs(path, text)
        described = self.declaration_file is not None and _same_file(path, self.declaration_file)
        for node in cross_file_keys.walk(file.root):
            if described and describes(node, wrapper):
                continue
            kind = None if test and _test_title(node) else _declares(node, name, file, wrapper)
            if kind == "interface":
                self.interface_members.append((path, node.start_byte))
            elif kind == "member":
                self.declarations.append((path, node.start_byte))
            if test and (_stub_mention(node, name, word, wrapper.kind, file) or _mockito_call(node, name)):
                self.test_doubles.append((path, wi.line_of(node), wi.text_of(node.parent or node)))


_SIGNATURES = ("property_signature", "method_signature")
_MEMBERS = (
    "method_definition",
    "abstract_method_signature",
    "public_field_definition",
    "pair",
    "function_declaration",
    "function_signature",
    "class_declaration",
    "abstract_class_declaration",
)


_FUNCTION_LIKE = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
        "generator_function",
        "class_static_block",
    }
)


#: What wraps a top-level declaration in a declaration file:
#: ``export declare function f(): boolean;``.
_DECLARATION_WRAPPERS = ("export_statement", "ambient_declaration")


def _declared_at_top(node: Node) -> bool:
    parent = node.parent
    while parent is not None and parent.type in _DECLARATION_WRAPPERS:
        parent = parent.parent
    return parent is not None and parent.type == "program"


def describes(node: Node, wrapper: wi.Wrapper) -> bool:
    """Whether a declaration in the wrapper's own declaration file is the
    wrapper's: a top-level ``function f(...): boolean;`` or ``const f: ...``
    for a function; for a member, a member of the name in the top-level
    ``class Owner { ... }`` or in the type ``const owner: { ... }`` is written
    with. Its name only names the wrapper, and it goes with the wrapper
    (:func:`without_descriptions`)."""
    declared = _member_name(node)
    if declared is None or not _is_name(declared, wrapper.name):
        return False
    if wrapper.kind == "function":
        if node.type == "function_signature":
            return _declared_at_top(node)
        return node.type == "variable_declarator" and node.parent is not None and _declared_at_top(node.parent)
    if node.type not in ("method_signature", "property_signature", "public_field_definition"):
        return False
    holder = node.parent
    owner = None if holder is None else holder.parent
    if holder is not None and holder.type == "class_body":
        name = None if owner is None or owner.type != "class_declaration" else owner.child_by_field_name("name")
        return name is not None and _is_name(name, wrapper.owner) and _declared_at_top(owner)
    if holder is None or holder.type != "object_type" or owner is None or owner.type != "type_annotation":
        return False
    declarator = owner.parent
    name = None if declarator is None or declarator.type != "variable_declarator" else declarator.child_by_field_name("name")
    return (
        name is not None
        and _is_name(name, wrapper.owner)
        and declarator.child_by_field_name("type") == owner
        and declarator.parent is not None
        and _declared_at_top(declarator.parent)
    )


def describing_name(node: Node, wrapper: wi.Wrapper) -> bool:
    """Whether ``node`` is the name of a declaration that :func:`describes`
    the wrapper."""
    parent = node.parent
    return parent is not None and _member_name(parent) == node and describes(parent, wrapper)


def descriptions(root: Node, wrapper: wi.Wrapper) -> list[Node] | None:
    """What deleting the declarations that :func:`describes` the wrapper
    deletes: the whole top-level statement of a function signature (every
    overload) or a ``declare const``, else the member. ``None`` when a
    ``declare const`` declares something else as well, which would go with it."""
    found: list[Node] = []
    for node in cross_file_keys.walk(root):
        if not describes(node, wrapper):
            continue
        if node.type == "variable_declarator":
            # `lexical_declaration` for `const`/`let`, `variable_declaration` for `var`.
            node = node.parent
            if sum(c.type == "variable_declarator" for c in node.named_children) != 1:
                return None
        if node.type in ("function_signature", "lexical_declaration", "variable_declaration"):
            while node.parent is not None and node.parent.type in _DECLARATION_WRAPPERS:
                node = node.parent
        found.append(node)
    return found


#: A comment that speaks for the file rather than for the declaration under
#: it: a triple-slash directive (``/// <reference types="node" />``), a
#: ``@ts-`` pragma or a lint directive.
_FILE_COMMENT = re.compile(r"^(?:///|(?://|/\*+)\s*(?:@ts-|eslint|tslint|prettier-ignore))")


def _description_start(node: Node, data: bytes) -> int:
    """Where deleting ``node`` starts: back over the comments touching it,
    stopping at one that speaks for the file (:data:`_FILE_COMMENT`)."""
    start = node.start_byte
    previous = node.prev_sibling
    while (
        previous is not None
        and previous.type == "comment"
        and data[previous.end_byte : start].count(b"\n") == 1
        and not data[previous.end_byte : start].strip()
        and not _FILE_COMMENT.match(_text(previous))
    ):
        start = previous.start_byte
        previous = previous.prev_sibling
    return start


def _description_end(node: Node) -> int:
    """Where deleting ``node`` ends: :func:`member_end`, or past a comment
    after a statement on its line, which would otherwise be left on a line of
    its own."""
    end = member_end(node)
    after = node.next_sibling
    if (
        end == node.end_byte
        and after is not None
        and after.type == "comment"
        and after.start_point[0] == node.end_point[0]
    ):
        end = after.end_byte
    return end


def without_descriptions(wrapper: wi.Wrapper) -> tuple[str, str] | None:
    """``(before, after)`` for the wrapper's own declaration file with the
    declarations that :func:`describes` it deleted, each with its own
    comments (:func:`_description_start`, :func:`_description_end`); ``None``
    when one cannot be deleted alone (:func:`descriptions`).

    A file left with no top-level ``import`` or ``export``, having had one,
    would turn into a global script, and every ``import "./flags"`` would stop
    type-checking. The first statement deleted becomes ``export {};`` instead,
    as the stranded-import pass does for a file's last import."""
    from flag_cleanup import unused

    path = wrapper.declaration_file
    source = None if path is None else _read(path)
    if source is None:
        return None
    root = ts_scope.parse(source, path)
    nodes = descriptions(root, wrapper)
    if nodes is None:
        return None
    data = source.encode("utf-8")
    spans = sorted(
        ((_description_start(n, data), _description_end(n), n) for n in nodes),
        key=lambda span: span[0],
    )

    def delete(marker: tuple[int, int, Node] | None = None) -> str:
        # Last first, so every offset still to be used is where it was.
        text = source
        for span in reversed(spans):
            start, end, _ = span
            if span is marker:
                raw = text.encode("utf-8")
                text = (raw[:start] + unused.MODULE_MARKER.encode("utf-8") + raw[end:]).decode("utf-8")
                continue
            body = data[start:end].decode("utf-8", "replace")
            text = unused._delete_method(text, unused.Candidate(wrapper.name, start, end, "method", body))
        return text

    def module(text: str) -> bool:
        return any(c.type in unused._MODULE_EVIDENCE for c in ts_scope.parse(text, path).children)

    after = delete()
    if not module(source) or module(after):
        return source, after
    # Only a top-level statement's deletion can take the last marker away;
    # if none was one, something unforeseen did, and the wrapper stays.
    first = next((s for s in spans if s[2].parent is not None and s[2].parent.type == "program"), None)
    return None if first is None else (source, delete(first))


def _local(node: Node) -> bool:
    """Whether a declaration sits inside a function body: the binding rule's.
    Anything else (a namespace, a block, the module) is visible to others."""
    parent = node.parent
    while parent is not None:
        if parent.type in _FUNCTION_LIKE:
            return True
        parent = parent.parent
    return False


def _is_name(node: Node, name: str) -> bool:
    return _text(node) == name


def _alias_of_wrapper(node: Node, file: ts_scope.File, wrapper: wi.Wrapper) -> bool:
    """Whether a shorthand property names the wrapper itself: the file binds
    the name once, as the wrapper's declaration or an import of it."""
    bound = file.binding(_text(node))
    if bound is None:
        return False
    if file.path == wrapper.path:
        return ts_scope.top_level(bound.parent) or (
            bound.parent.type == "variable_declarator" and ts_scope.top_level(bound.parent.parent)
        )
    imported = file.import_of(_text(node))
    return imported is not None and imported.module == wrapper.path and imported.imported == wrapper.name


def _declares(node: Node, name: str, file: ts_scope.File, wrapper: wi.Wrapper) -> str | None:
    """``interface`` for a signature of ``name``, ``member`` for any other
    declaration another object could answer a call with, else ``None``."""
    if node.type == "string":
        return "member" if cross_file_ts._string_value(node) == name else None
    if node.type in ("assignment_expression", "augmented_assignment_expression"):
        left = node.child_by_field_name("left")
        prop = left.child_by_field_name("property") if left is not None and left.type == "member_expression" else None
        return "member" if prop is not None and _is_name(prop, name) else None
    if node.type == "export_specifier":
        alias = node.child_by_field_name("alias")
        local = node.child_by_field_name("name")
        # Only a specifier that gives something else the name; re-exporting the
        # wrapper, aliased or not, is not a second implementation.
        if alias is None or local is None or not _is_name(alias, name) or _is_name(local, name):
            return None
        return "member"
    if node.type in _SIGNATURES:
        declared = node.child_by_field_name("name")
        if declared is None or not _is_name(declared, name):
            return None
        # A signature in an ambient class declares a member, not a structural type.
        return "member" if node.parent is not None and node.parent.type == "class_body" else "interface"
    if node.type == "pair":
        key = node.child_by_field_name("key")
        return "member" if key is not None and key.type == "property_identifier" and _is_name(key, name) else None
    if node.type in _MEMBERS:
        declared = node.child_by_field_name("name")
        if declared is None or not _is_name(declared, name):
            return None
        if node.type == "function_declaration" and _local(node):
            return None  # a local function: the binding rule's
        return "member"
    if node.type in ("required_parameter", "optional_parameter"):
        pattern = node.child_by_field_name("pattern")
        if pattern is not None and _is_name(pattern, name) and _has(node, *ts_scope._PARAMETER_PROPERTY):
            return "member"
        return None
    if node.type == "shorthand_property_identifier":
        return "member" if _is_name(node, name) and not _alias_of_wrapper(node, file, wrapper) else None
    if node.type == "variable_declarator":
        declared = node.child_by_field_name("name")
        statement = node.parent
        if declared is None or declared.type != "identifier" or not _is_name(declared, name) or statement is None:
            return None
        return None if _local(node) else "member"
    if node.type == "enum_body":
        for member in node.named_children:
            key = member.child_by_field_name("name") if member.type == "enum_assignment" else member
            if key is not None and key.type == "property_identifier" and _is_name(key, name):
                return "member"
    return None


def _callee(node: Node) -> bool:
    """Whether a mention is the function being called: ``f`` in ``f(...)``,
    or ``m`` in ``x.m(...)``."""
    parent = node.parent
    if parent is not None and parent.type == "member_expression" and parent.child_by_field_name("property") == node:
        node, parent = parent, parent.parent
    return parent is not None and parent.type == "call_expression" and parent.child_by_field_name("function") == node


#: The calls whose first argument is a test's title, with the focused and
#: skipped aliases Jest and Jasmine give them.
_TITLED = frozenset({"describe", "it", "test", "fit", "xit", "xtest", "fdescribe", "xdescribe"})
#: Their members that take a title the same way: ``it.only("...")``.
_TITLED_MODIFIERS = frozenset({"only", "skip", "concurrent", "todo", "failing"})


def _takes_title(callee: Node) -> bool:
    """Whether a call through ``callee`` takes a test's title first: ``it``,
    ``it.only``, or the call ``it.each(table)`` returns."""
    if callee.type == "identifier":
        return _text(callee) in _TITLED
    if callee.type == "member_expression":
        target = callee.child_by_field_name("object")
        member = callee.child_by_field_name("property")
        return (
            target is not None and target.type == "identifier" and _text(target) in _TITLED
            and member is not None and _text(member) in _TITLED_MODIFIERS
        )
    if callee.type == "call_expression":
        each = callee.child_by_field_name("function")
        if each is None or each.type != "member_expression":
            return False
        target = each.child_by_field_name("object")
        member = each.child_by_field_name("property")
        return (
            member is not None and _text(member) == "each"
            and target is not None and target.type != "call_expression" and _takes_title(target)
        )
    return False


def _test_title(node: Node) -> bool:
    """Whether a string, or a fragment of one, is a test's title: the literal
    first argument of :func:`_takes_title`'s calls, handed a callback second
    (:func:`flag_cleanup.ts_tests.hands_callback`). The runner consumes a
    title, so it is never a reflective use of a name: no rule that reads
    strings counts one, in a file that may stub (:func:`_stubs`). Without the
    callback the call may be a function of the codebase's own that happens
    to share the runner's name, handed the name to look up, so the string
    still counts. A template's substitutions are nodes of their own, judged
    as any other mention."""
    literal = node.parent if node.type in _STRINGS else node
    if literal is None or literal.type not in ("string", "template_string"):
        return False
    arguments = literal.parent
    if arguments is None or arguments.type != "arguments":
        return False
    if not ts_tests.hands_callback(arguments) or arguments.named_children[0].id != literal.id:
        return False
    call = arguments.parent
    callee = None if call is None or call.type != "call_expression" else call.child_by_field_name("function")
    return callee is not None and _takes_title(callee)


def _stub_mention(
    node: Node, name: str, word: re.Pattern[str], kind: str = "function", file: ts_scope.File | None = None
) -> bool:
    """A test file's mention of the name that is not a call, not an import and
    not a test's title."""
    if node.type in _STRINGS:
        return bool(word.search(_text(node))) and not _test_title(node)
    if node.type not in MENTION_TYPES or not _is_name(node, name) or names_only(node, kind):
        return False
    if file is not None and _binds_import(node, file, kind):
        return False
    parent = node.parent
    if parent is not None and parent.type in ("import_specifier", "export_specifier"):
        return False
    return not _callee(node)


def _known_type(name_node: Node, file: ts_scope.File, files: ts_scope.Files) -> bool:
    """Whether a supertype names a class or interface the repository declares:
    in this file, or through a named import of a resolved module."""
    kinds = ("class_declaration", "abstract_class_declaration", "interface_declaration")
    name = _text(name_node)
    if file.binding(name) is None:
        return False
    if len(ts_scope.top_declarations(file.root, kinds, name)) == 1:
        return True
    imported = file.import_of(name)
    if imported is None or imported.module is None or imported.imported in (None, "default", "="):
        return False
    module = files.file(imported.module)
    return module is not None and len(ts_scope.top_declarations(module.root, kinds, imported.imported)) == 1


def _supertypes_known(wrapper: wi.Wrapper, files: ts_scope.Files) -> bool:
    file = files.file(wrapper.path)
    if file is not None and ts_scope.holds_object_literal(ts_scope.top_value(file.root, wrapper.owner)):
        # An object literal extends nothing but ``Object``, which has no
        # member of a wrapper's name; a spread in it refused discovery.
        return True
    if file is not None and _factory_function(wrapper, files) is not None:
        return True  # the same, for the literal a factory returns
    classes = [] if file is None else ts_scope.top_declarations(file.root, ("class_declaration",), wrapper.owner)
    if len(classes) != 1:
        return False
    heritage = next((c for c in classes[0].named_children if c.type == "class_heritage"), None)
    for clause in [] if heritage is None else heritage.named_children:
        names = (
            [clause.child_by_field_name("value")]
            if clause.type == "extends_clause"
            else list(clause.named_children)
        )
        for name_node in names:
            if name_node is None or name_node.type not in ("identifier", "type_identifier"):
                return False
            if clause.type == "extends_clause" and clause.child_by_field_name("type_arguments") is not None:
                return False
            if not _known_type(name_node, file, files):
                return False
    return True


#: Where a reference to an object-literal owner only names it.
_NAMING_PARENTS = frozenset(
    {"import_specifier", "namespace_import", "import_clause", "shorthand_property_identifier_pattern", "type_query"}
)
#: A re-export of another module, which hands its objects to importers unseen.
_REEXPORT = re.compile(r"(?<![\w$])export\s*(\*|\{[^}]*\}\s*from\b)")


def _factory_function(wrapper: wi.Wrapper, files: ts_scope.Files) -> Node | None:
    """The factory function a factory member's owner names, at the top of
    the wrapper's file."""
    name = ts_scope.factory_name(wrapper.owner)
    file = None if name is None else files.file(wrapper.path)
    if file is None:
        return None
    found = ts_scope.top_declarations(file.root, ("function_declaration",), name)
    return found[0] if len(found) == 1 and ts_scope.factory_object(found[0]) is not None else None


def _owner_literal(wrapper: wi.Wrapper, files: ts_scope.Files) -> Node | None:
    file = files.file(wrapper.path)
    declarator = None if file is None else ts_scope.top_value(file.root, wrapper.owner)
    return declarator.child_by_field_name("value") if ts_scope.holds_object_literal(declarator) else None


def _receiver_only(node: Node) -> bool:
    """Whether a reference to the object is the object of a ``.name`` access:
    reading, calling or assigning a member never hands the object itself on."""
    parent = node.parent
    if parent is not None and parent.type == "non_null_expression":
        node, parent = parent, parent.parent
    return (
        parent is not None
        and parent.type == "member_expression"
        and parent.child_by_field_name("object") == node
        and (prop := parent.child_by_field_name("property")) is not None
        and prop.type == "property_identifier"
    )


def owner_escapes(repo_dir: str, wrapper: wi.Wrapper, files: ts_scope.Files) -> list[tuple[str, int, str]]:
    """Each use of a member's object-literal owner other than as a receiver.

    A class's methods sit on its prototype, out of sight of ``Object.keys``.
    An object literal's members are its own enumerable keys, so code that
    takes the object whole (``Object.keys(flags)``, ``{ ...flags }``,
    ``for (k in flags)``, ``flags[k]``, passing or storing it, a re-export
    under another name) sees the deleted member go, and one that merges into
    it (``Object.assign(flags, o)``) may replace the member without spelling
    its name. Every reference must therefore be ``flags.<name>``: the
    declaration's own name, an import of it from the wrapper's module (or
    ``ns.flags`` off a namespace import), a ``require`` of it, a type query,
    or ``this`` inside the literal. A re-export of the module, the module
    object itself handed on (:func:`module_escapes`), a component or template
    naming the object, and a file that cannot be parsed refuse. Another
    binding of the name in another file is another object."""
    from flag_cleanup import source_index

    literal = _owner_literal(wrapper, files)
    if literal is None:
        return []
    owner = wrapper.owner
    found: list[tuple[str, int, str]] = []
    for node in cross_file_keys.walk(literal):
        if node.type == "this" and not _receiver_only(node):
            found.append((wrapper.path, wi.line_of(node), wi.text_of(node.parent or node)))
    word = _word(owner)
    for path in cross_file_keys.mention_files(
        repo_dir, [owner], exclude=(), also=_REEXPORT, extra_suffixes=source_index.COMPONENT_SUFFIXES
    ):
        text = _read(path)
        if text is None:
            found.append((path, 1, ""))
            continue
        if not path.endswith(ts_scope.FAMILY_EXTENSIONS):
            if word.search(text):
                found.append((path, 1, owner))
            continue
        file = files.file(path, text)
        if file is None or file.root.has_error:
            found.append((path, 1, ""))
            continue
        home = _same_file(path, wrapper.path)
        locals_ = {owner} if home else {
            i.local for i in file.imports if i.imported == owner and i.module == wrapper.path
        }
        namespaces = set() if home else {
            i.local for i in file.imports if i.imported is None and i.module == wrapper.path
        }
        for node in cross_file_keys.walk(file.root):
            if node.type == "export_statement" and node.child_by_field_name("source") is not None:
                target = ts_scope.resolve(path, cross_file_ts._string_value(node.child_by_field_name("source")) or "", repo_dir)
                if target is None or _same_file(target, wrapper.path):
                    if target is not None or word.search(_text(node)):
                        found.append((path, wi.line_of(node), wi.text_of(node)))
                continue
            if node.type == "shorthand_property_identifier" and _text(node) in locals_:
                found.append((path, wi.line_of(node), wi.text_of(node.parent or node)))  # `{ flags }` stores it
                continue
            if node.type == "identifier" and _text(node) in locals_:
                reference = node
            elif (
                node.type == "member_expression"
                and (obj := node.child_by_field_name("object")) is not None
                and obj.type == "identifier"
                and _text(obj) in namespaces
                and (prop := node.child_by_field_name("property")) is not None
                and _is_name(prop, owner)
            ):
                reference = node
            elif node.type == "identifier" and _text(node) == owner and file.binding(owner) is None:
                reference = node  # unbound here: judged as if it were the object
            else:
                continue
            parent = reference.parent
            if parent is not None and (parent.type in _NAMING_PARENTS or _member_name(parent) == reference):
                continue  # an import, a type query, or the declaration's own name
            if parent is not None and parent.type == "export_specifier" and home:
                alias = parent.child_by_field_name("alias")
                if alias is None or _is_name(alias, owner):
                    continue  # `export { flags }`: the object under its own name
            if not _receiver_only(reference):
                found.append((path, wi.line_of(reference), wi.text_of(reference.parent or reference)))
    return found + module_escapes(repo_dir, wrapper)


#: The roles a name plays in a factory member's proof.
_FACTORY, _INSTANCE, _GETTER = "factory", "instance", "getter"


def _call_of(reference: Node) -> Node | None:
    """The call ``reference`` is the function of."""
    parent = reference.parent
    if parent is not None and parent.type == "call_expression" and parent.child_by_field_name("function") == reference:
        return parent
    return None


def _no_arguments(call: Node) -> bool:
    arguments = call.child_by_field_name("arguments")
    return arguments is not None and arguments.type == "arguments" and not [
        c for c in arguments.named_children if c.type != "comment"
    ]


def _const_holding(call: Node) -> Node | None:
    """The name a ``const <name> = <call>`` binds the call's result to."""
    declarator = call.parent
    if declarator is None or declarator.type != "variable_declarator" or declarator.child_by_field_name("value") != call:
        return None
    declaration = declarator.parent
    name = declarator.child_by_field_name("name")
    if declaration is None or declaration.type != "lexical_declaration" or not _has(declaration, "const"):
        return None
    return name if name is not None and name.type == "identifier" else None


def _getter_returning(reference: Node) -> Node | None:
    """The name of the top-level function of no parameters whose whole body
    is ``return <reference>;``."""
    statement = reference.parent
    if statement is not None and statement.type == "binary_expression" and statement.child_by_field_name("right") == reference:
        # `return x ?? reference;`: the fallback of a reassigned `let` (the
        # `let` itself is proven by `ts_scope.Files.held_let`).
        left = statement.child_by_field_name("left")
        operator = statement.child_by_field_name("operator")
        if left is None or left.type != "identifier" or operator is None or _text(operator) not in ("??", "||"):
            return None
        reference, statement = statement, statement.parent
    if statement is None or statement.type != "return_statement":
        return None
    block = statement.parent
    function = block.parent if block is not None else None
    if function is None or ts_scope._returned(function) != reference or not ts_scope.top_level(function):
        return None
    parameters = function.child_by_field_name("parameters")
    if parameters is None or [c for c in parameters.named_children if c.type != "comment"]:
        return None
    return function.child_by_field_name("name")


def _spied_member(call: Node) -> bool:
    """Whether ``call`` is the object of ``jest.spyOn(<call>, "<name>")`` or
    ``vi.spyOn``: a spy replaces the one member it names, and the name scan
    judges that name."""
    holder = call.parent
    spy = None if holder is None or holder.type != "arguments" else holder.parent
    if spy is None or spy.type != "call_expression" or mock_stubs.ts_callee(spy) not in ("jest.spyOn", "vi.spyOn"):
        return False
    args = mock_stubs.ts_arguments(spy)
    return len(args) == 2 and args[0] == call and cross_file_ts._literal(args[1]) is not None


def factory_escapes(repo_dir: str, wrapper: wi.Wrapper, files: ts_scope.Files) -> list[tuple[str, int, str]]:
    """Each use of the objects a factory member's factory makes other than as
    a member's receiver, the way :func:`owner_escapes` asks it of a
    module-level object literal: their members are their own enumerable keys,
    so code that takes one whole (``Object.keys``, a spread, passing or
    storing it, ``obj[k]``) sees the deleted member go, and code that merges
    into it may replace the member unseen.

    ONE INSTANCE. The factory must be called exactly once in the whole
    checkout, tests and helpers included, as the initialiser of a top-level
    ``const`` in a file that is not a test, a test-support file or a file
    calling a mocking library. Every proven receiver then reaches that one
    production object. Any other call (a test's ``buildFlags(fakeClient)``, a
    helper's fake instance, a second module-level instance, a receiver
    ``buildFlags(c).m()``) would be an object whose read the caller chose, and
    whose inlined call would assert against a literal, so each is reported and
    the members stay.

    Three kinds of name are followed, each wherever it is spelled: the
    factory, that one instance, and each top-level function of no parameters
    in the instance's module whose whole body returns it (a getter). Every
    spelling must be a declaration, an import, a type query, that one call,
    an instance or a getter call that is a member's receiver, a getter's
    ``return``, or a getter call that is a spy's object
    (``vi.spyOn(getFlags(), "m")``). A re-export of one, its module object
    handed on, a component naming one, and a file that cannot be parsed
    refuse too."""
    from flag_cleanup import source_index

    function = _factory_function(wrapper, files)
    if function is None:
        return [(wrapper.path, wrapper.line, wrapper.owner)]
    found: list[tuple[str, int, str]] = []
    for node in cross_file_keys.walk(ts_scope.factory_object(function)):
        if node.type == "this" and not _receiver_only(node):
            found.append((wrapper.path, wi.line_of(node), wi.text_of(node.parent or node)))
    pending = [(ts_scope._resolved(wrapper.path), ts_scope.factory_name(wrapper.owner), _FACTORY)]
    seen: set[tuple[str, str]] = set()
    modules: set[str] = set()
    calls: list[tuple[str, Node, Node | None, bool]] = []  # the factory's: (path, call, const name, production file)
    writes: set[int] = set()  # ids of the calls assigned to a proven private `let`
    while pending:
        module, name, role = pending.pop()
        if (module, name) in seen:
            continue
        seen.add((module, name))
        modules.add(module)
        word = _word(name)
        for path in cross_file_keys.mention_files(
            repo_dir, [name], exclude=(), also=_REEXPORT, extra_suffixes=source_index.COMPONENT_SUFFIXES
        ):
            text = _read(path)
            if text is None:
                found.append((path, 1, ""))
                continue
            if not path.endswith(ts_scope.FAMILY_EXTENSIONS):
                if word.search(text):
                    found.append((path, 1, name))
                continue
            file = files.file(path, text)
            if file is not None and file.root.has_error and not word.search(text):
                # Syntax this parser cannot read (`export type * from`), in a
                # file that never spells the name: it matters only if it
                # re-exports the module, which its `from` clauses say.
                if any(
                    (target := ts_scope.resolve(path, spec, repo_dir)) is None or _same_file(target, module)
                    for spec in _FROM_CLAUSES.findall(text)
                ):
                    found.append((path, 1, ""))
                continue
            if file is None or file.root.has_error:
                found.append((path, 1, ""))
                continue
            home = _same_file(path, module)
            locals_ = {name} if home else {i.local for i in file.imports if i.imported == name and i.module == module}
            namespaces = set() if home else {i.local for i in file.imports if i.imported is None and i.module == module}
            for node in cross_file_keys.walk(file.root):
                if node.type == "export_statement" and _type_only_export(node):
                    continue  # `export type { Flags } from "./flags"`: no value at run time
                if node.type == "export_statement" and node.child_by_field_name("source") is not None:
                    target = ts_scope.resolve(path, cross_file_ts._string_value(node.child_by_field_name("source")) or "", repo_dir)
                    if (target is None and word.search(_text(node))) or (target is not None and _same_file(target, module)):
                        found.append((path, wi.line_of(node), wi.text_of(node)))
                    continue
                if _stands_in(node, name, locals_):
                    found.append((path, wi.line_of(node), wi.text_of(node.parent or node)))
                    continue
                if node.type == "identifier" and _text(node) in locals_:
                    reference = node
                elif (
                    node.type == "member_expression"
                    and (obj := node.child_by_field_name("object")) is not None
                    and obj.type == "identifier"
                    and _text(obj) in namespaces
                    and (prop := node.child_by_field_name("property")) is not None
                    and _is_name(prop, name)
                ):
                    reference = node
                elif node.type == "identifier" and _text(node) == name and file.binding(name) is None:
                    reference = node  # unbound here: judged as if it were the object
                else:
                    continue
                parent = reference.parent
                if parent is not None and (parent.type in _NAMING_PARENTS or _member_name(parent) == reference):
                    continue  # an import, a type query, or the declaration's own name
                if parent is not None and parent.type == "export_specifier" and home:
                    alias = parent.child_by_field_name("alias")
                    if alias is None or _is_name(alias, name):
                        continue
                if role == _INSTANCE:
                    if _receiver_only(reference):
                        continue
                    getter = _getter_returning(reference) if home else None
                    if getter is not None:
                        pending.append((file.path, _text(getter), _GETTER))
                        continue
                elif role == _FACTORY:
                    call = _call_of(reference)
                    if call is not None and _test_local_instance(call, file):
                        continue  # a test's own object: its test is judged at the member's call
                    if call is not None:
                        held = _const_holding(call)
                        top = held is not None and ts_scope.top_level(held.parent.parent)
                        calls.append((file.path, call, held if top else None, not _stubs(path, text)))
                        if held is None and _let_write(call, file, files):
                            writes.add(id(call))
                        continue
                else:
                    call = _call_of(reference)
                    if call is not None and _receiver_only(call):
                        continue
                    if call is not None and _no_arguments(call) and _spied_member(call):
                        continue
                found.append((path, wi.line_of(reference), wi.text_of(reference.parent or reference)))
        if role == _FACTORY:
            consts = [c for c in calls if c[2] is not None]
            others = [c for c in calls if c[2] is None]
            # The `let` form: one fallback const, and every other call an
            # assignment to a private `let` that only a getter's
            # `x ?? fallback` reads (see ``ts_scope.Files.held_let``).
            let_form = bool(others) and all(id(c[1]) in writes for c in others)
            if len(consts) == 1 and consts[0][3] and (not others or let_form) and all(c[3] for c in others):
                pending.append((consts[0][0], _text(consts[0][2]), _INSTANCE))
                found += _test_reachable_writers(repo_dir, others, files)
            else:
                found += [(path, wi.line_of(call), wi.text_of(call)) for path, call, _, _ in calls]
    # A module mock of one of these modules is the index's to find: every
    # module spelling the factory counts for a factory member
    # (:func:`_mocks_module`), and each of them spells it.
    if found:
        return found  # refused already; the scans below are the costly ones
    # A module object handed on can reach what the module exports by a
    # computed name. One that exports none of these names hands on none.
    for module in sorted(modules):
        names = {name for where, name in seen if where == module}
        home = files.file(module)
        if home is None or any(_exports(home.root, name) for name in names):
            found += handed_on(repo_dir, module)
    return found


def _test_local_instance(call: Node, file: ts_scope.File) -> bool:
    """Whether a factory call is ``const x = F(<fake>)`` directly in the body of
    an ``it(...)`` of a test file, and every other use of ``x`` in that body
    is a member's receiver: an object only that test sees, none of whose uses
    hands it on. (Whether the test is about the member alone is judged at each
    call of the member, :func:`_owned_test`.)"""
    if not _stubs(file.path, file.source):
        return False
    held = _const_holding(call)
    if held is None:
        return False
    data = file.source.encode("utf-8")
    found = _test_body(call, data)
    if found is None or held.parent.parent.parent != found[1]:
        return False
    name = _text(held)
    if file.binding_of(held) != held:
        return False
    if not _owned_arguments(call):
        return False
    for node in cross_file_keys.walk(found[1]):
        if node.type in ("identifier", "shorthand_property_identifier") and _text(node) == name and node != held:
            if node.type != "identifier" or not _receiver_only(node):
                return False
    return True


def _owned_arguments(call: Node) -> bool:
    arguments = call.child_by_field_name("arguments")
    return arguments is not None and all(
        a.type in ("object", "arrow_function", "identifier", "call_expression", "as_expression") or a.type in _PLAIN_VALUES
        for a in arguments.named_children
        if a.type != "comment"
    )


def _let_write(call: Node, file: ts_scope.File, files: ts_scope.Files) -> bool:
    """Whether ``call`` is a factory call assigned to a module-level ``let``
    that :meth:`ts_scope.Files.held_let` proves holds only that factory's
    objects."""
    assignment = call.parent
    if assignment is None or assignment.type != "assignment_expression" or assignment.child_by_field_name("right") != call:
        return False
    left = assignment.child_by_field_name("left")
    if left is None or left.type != "identifier":
        return False
    held = files.held_let(file, _text(left))
    return held is not None and any(c == call for c in held[1])


#: How many calls up from a writer of a private `let` are followed.
_CALLER_DEPTH = 4
_NO_FUNCTION = object()


def _top_owner(node: Node) -> str | object | None:
    """The top-level function a node sits in, by name: the outermost function
    around it, when that is a top-level declaration or the arrow/function of a
    top-level ``const``. ``_NO_FUNCTION`` for a node run at module load, and
    ``None`` for a method, a class field or anything else unnameable."""
    outer = None
    current = node.parent
    while current is not None:
        if current.type in _FUNCTION_LIKE:
            outer = current
        current = current.parent
    if outer is None:
        return _NO_FUNCTION
    if outer.type == "function_declaration" and ts_scope.top_level(outer):
        if outer.parent is not None and outer.parent.type == "export_statement" and _has(outer.parent, "default"):
            return None  # importers name a default export as they like
        name = outer.child_by_field_name("name")
        return None if name is None else _text(name)
    holder = outer.parent
    if (
        outer.type in ("arrow_function", "function_expression", "function")
        and holder is not None
        and holder.type == "variable_declarator"
        and holder.parent is not None
        and ts_scope.top_level(holder.parent)
        and (name := holder.child_by_field_name("name")) is not None
        and name.type == "identifier"
    ):
        return _text(name)
    return None


def _writer_callers(
    repo_dir: str, files: ts_scope.Files, home: str, name: str, found: list[tuple[str, int, str]]
) -> list[tuple[str, str]]:
    """The top-level functions calling ``name`` (declared in ``home``), by name,
    and in ``found`` every use that may let a test choose the argument: any
    mention in a test or stubbing file, a use that is not a call (an alias, a
    re-export, a stored or passed function), a method calling it, a file that
    cannot be read, and a method or class field that holds the call."""
    from flag_cleanup import source_index

    word = _word(name)
    callers: list[tuple[str, str]] = []
    for other in cross_file_keys.mention_files(
        repo_dir, [name], exclude=(), also=_REEXPORT, extra_suffixes=source_index.COMPONENT_SUFFIXES
    ):
        if not other.endswith(ts_scope.FAMILY_EXTENSIONS):
            continue  # prose and data name things without calling them
        text = _read(other)
        if text is None:
            found.append((other, 1, name))
            continue
        if not word.search(text):
            continue
        local = _same_file(other, home)
        if not local and _unrelated_name(other, text, name, home, files):
            continue
        file = files.file(other, text)
        if file is None or file.root.has_error:
            found.append((other, 1, name))
            continue
        stub = _stubs(other, text)
        namespaces = {i.local for i in file.imports if i.imported is None and i.module is not None and _same_file(i.module, home)}
        for node in cross_file_keys.walk(file.root):
            if node.type == "property_identifier" and _text(node) == name and namespaces:
                member = node.parent
                holder = None if member is None or member.type != "member_expression" else member.child_by_field_name("object")
                if holder is None or holder.type != "identifier" or _text(holder) not in namespaces:
                    continue  # another object's member of the same name
                if stub or _call_of(member) is None:
                    found.append((other, wi.line_of(node), wi.text_of(member)))
                    continue
                owner = _top_owner(node)
                if owner is None:
                    found.append((other, wi.line_of(node), wi.text_of(member)))
                elif owner is not _NO_FUNCTION:
                    callers.append((other, str(owner)))
                continue
            if node.type not in ("identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern", "export_specifier"):
                continue
            if node.type == "export_specifier":
                if word.search(_text(node)):
                    found.append((other, wi.line_of(node), wi.text_of(node)))
                continue
            if _text(node) != name:
                continue
            parent = node.parent
            if parent is not None and (parent.type in ("import_specifier", "import_clause", "namespace_import") or _member_name(parent) == node):
                continue
            if local and parent is not None and parent.type == "function_declaration" and parent.child_by_field_name("name") == node:
                continue
            if stub:
                found.append((other, wi.line_of(node), wi.text_of(parent or node)))
                continue
            if node.type != "identifier" or _call_of(node) is None:
                found.append((other, wi.line_of(node), wi.text_of(parent or node)))
                continue
            owner = _top_owner(node)
            if owner is None:
                found.append((other, wi.line_of(node), wi.text_of(parent or node)))
            elif owner is not _NO_FUNCTION:
                callers.append((other, str(owner)))
    return callers


def _test_reachable_writers(
    repo_dir: str, writes: list[tuple[str, Node, Node | None, bool]], files: ts_scope.Files
) -> list[tuple[str, int, str]]:
    """The writes of a private `let` that a test may reach with a fake: the
    write is not inside a top-level function, or that function, or any
    function calling it (up to ``_CALLER_DEPTH`` calls up, by name), is
    mentioned in a test or stubbing file; or a link of the chain is aliased,
    re-exported, stored, called from a method, or not followed far enough.
    A test installing an instance of its own choosing would see the folded
    member answer from the literal."""
    found: list[tuple[str, int, str]] = []
    frontier: list[tuple[str, str]] = []
    for path, call, _, _ in writes:
        owner = _top_owner(call)
        if owner is None or owner is _NO_FUNCTION:
            found.append((path, wi.line_of(call), wi.text_of(call.parent or call)))
        else:
            frontier.append((path, str(owner)))
    seen: set[tuple[str, str]] = set()
    depth = 0
    while frontier:
        if depth >= _CALLER_DEPTH:
            found += [(path, 1, name) for path, name in frontier if (path, name) not in seen]
            break
        following: list[tuple[str, str]] = []
        for home, name in frontier:
            if (home, name) in seen:
                continue
            seen.add((home, name))
            following += _writer_callers(repo_dir, files, home, name, found)
        frontier = following
        depth += 1
    return found


def _unrelated_name(path: str, text: str, name: str, home: str, files: ts_scope.Files) -> bool:
    """Whether a file's mentions of ``name`` are plainly some other function: a
    string, a member of another object, a declaration of its own, or an import
    from a module that is not ``home`` and re-exports nothing (a generic name
    such as ``initialize`` is common)."""
    file = files.file(path, text)
    if file is None or file.root.has_error:
        return False
    if any(i.imported is None and i.module is not None and _same_file(i.module, home) for i in file.imports):
        return False  # `ns.initialize()` through a namespace import of the module
    spelled = [
        n for n in cross_file_keys.walk(file.root)
        if n.type in ("identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern")
        and _text(n) == name
    ]
    if not spelled:
        return True
    imports = [i for i in file.imports if i.local == name]
    if not imports:
        return file.binding(name) is not None
    for imported in imports:
        if imported.imported is None or (imported.module is not None and _same_file(imported.module, home)):
            return False
        if imported.module is None:
            if imported.specifier is None or not _is_package(imported.specifier, lambda: installable_packages(files.repo_dir)):
                return False
            continue
        target = _read(imported.module)
        if target is None or _REEXPORT.search(target):
            return False
    return True


#: The module specifiers an unparsed file re-exports from or imports.
_FROM_CLAUSES = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)['"]([^'"]+)['"]""")


def _type_only_export(node: Node) -> bool:
    """``export type { T } from "./m"`` / ``export type * from``: erased, so it
    hands on no value. (A specifier-level ``type`` is not judged.)"""
    if node.child_by_field_name("source") is None:
        return False
    for child in node.children:
        if child.type == "type":
            return True
        if child.type in ("export_clause", "*", "namespace_export", "string"):
            return False
    return False


def _exports(root: Node, name: str) -> bool:
    """Whether a module exports anything spelled ``name`` in any form: an
    ``export`` statement naming it, or a CommonJS export assignment."""
    word = _word(name)
    for node in root.named_children:
        if node.type == "export_statement" and word.search(_text(node)):
            return True
        if node.type == "expression_statement" and re.search(r"(?<![\w$])(module\.)?exports\b", _text(node)) and word.search(_text(node)):
            return True
    return False


def _stands_in(node: Node, name: str, locals_: set[str]) -> bool:
    """Whether a spelling of ``name`` that is not a plain reference may stand
    for it: ``{ getFlags }`` storing it, a spy or stub naming it as a string
    (``vi.spyOn(mod, "getFlags")``), or a destructuring that binds it from a
    module load no import resolves (``const { getFlags } = await
    import("./index")``). Another object's member or key of the same name,
    and a destructuring of anything else, are not."""
    if _text(node) != name:
        return False
    if node.type == "shorthand_property_identifier":
        return name in locals_
    if node.type == "string_fragment":
        string = node.parent
        holder = None if string is None else string.parent
        call = None if holder is None or holder.type != "arguments" else holder.parent
        return call is not None and call.type == "call_expression" and mock_stubs.ts_callee(call) in _SPIES
    if node.type == "shorthand_property_identifier_pattern":
        current = node.parent
        while current is not None and current.type != "variable_declarator":
            current = current.parent
        value = None if current is None else current.child_by_field_name("value")
        if value is not None and value.type == "await_expression":
            value = next((c for c in value.named_children if c.type != "comment"), None)
        function = value.child_by_field_name("function") if value is not None and value.type == "call_expression" else None
        return function is not None and (function.type == "import" or _text(function) == "require")
    return False


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | wi.Refused | None]:
    """The wrapper, or why it does not qualify.

    A signature of the name anywhere refuses first: TS interfaces and type
    literals are structural, so neither their implementations nor the calls
    made through them can be counted. A stub refuses next, before any caller
    is folded. Then the name must be declared exactly once, and a method's
    supertypes must all be declared in the repository."""
    if index.interface_members:
        return None, wi.INTERFACE_STRUCTURAL
    if index.test_doubles:
        return None, wi.TEST_DOUBLE
    if index.unreadable or len(index.declarations) != 1:
        return None, wi.NAME_NOT_UNIQUE
    if wrapper.kind == "method" and not _supertypes_known(wrapper, index.files):
        return None, wi.OVERRIDABLE
    if index.factory_escapes is not None:
        index.owner_escapes, index.factory_escapes = index.factory_escapes(), None
        if index.owner_escapes:
            # Reported where it is: the extra factory call, or the use that
            # hands an object on, is what the reviewer has to look at.
            for path, line, text in index.owner_escapes:
                logger.info("%s:%d keeps %s: %s", path, line, wrapper.label, text)
            path, line, text = index.owner_escapes[0]
            return None, wi.Refused(wi.OVERRIDABLE, path, line, text)
    if index.owner_escapes:
        for path, line, text in index.owner_escapes:
            logger.info("%s:%d hands on the object %s, so its member %s is kept: %s", path, line, wrapper.owner, wrapper.name, text)
        return None, wi.OVERRIDABLE
    return wrapper, None


# --- Stub targets -----------------------------------------------------------------

#: Calls that stub a member by its name: ``jest.spyOn(target, "m")``.
_SPIES = frozenset({"jest.spyOn", "vi.spyOn", "sinon.stub"})
#: ``vi.mocked(x)``: ``x`` itself, typed as its mock.
_MOCKED = frozenset({"vi.mocked", "jest.mocked"})
#: ts-mockito's calls that take a call of the mock and describe it.
_MOCKITO = frozenset({"when", "verify"})
#: The packages whose ``mock`` makes an instance of the class it is given.
_MOCK_LIBRARIES = frozenset({"jest-mock-extended", "vitest-mock-extended", "ts-mockito", "@typestrong/ts-mockito"})
_RETURNS = frozenset({"mockReturnValue", "mockReturnValueOnce", "mockResolvedValue", "mockResolvedValueOnce"})


def _same_file(a: str, b: str) -> bool:
    return ts_scope._resolved(a) == ts_scope._resolved(b)


def _collapsed(node: Node) -> str:
    return "".join(_text(node).split())


def _module_mock_calls(root: Node) -> list[Node]:
    """Every ``jest.mock(...)``-like call in a file."""
    return [
        n for n in cross_file_keys.walk(root)
        if n.type == "call_expression" and mock_stubs.ts_callee(n) in mock_stubs.TS_MODULE_MOCKS
    ]


_SOURCE_EXTENSION = re.compile(r"\.(?:[cm]?[jt]s|[jt]sx)$")


def _names_wrapper_file(specifier: str, wrapper_path: str) -> bool:
    """Whether an unresolved specifier ends in the wrapper file's name. The
    resolver cannot read every alias (a Jest ``moduleNameMapper``, a
    ``baseUrl``), so ``@/flags`` may still be the wrapper's own module."""
    last = _SOURCE_EXTENSION.sub("", specifier.rstrip("/").rsplit("/", 1)[-1])
    path = Path(wrapper_path)
    stem = _SOURCE_EXTENSION.sub("", path.name)
    return last == stem or (stem == "index" and last == path.parent.name)


def _object_without(entry: Node, name: str) -> bool:
    """Whether an entry is a function returning an object literal that has no
    member ``name`` and no spread or computed key: a getter handed such an
    object has no such member to answer with."""
    function = entry.child_by_field_name("value") if entry.type == "pair" else entry
    literal = None if function is None else mock_stubs.ts_function_object(function)
    if literal is None:
        return False
    for member in literal.named_children:
        if member.type == "comment":
            continue
        key = mock_stubs.ts_entry_name(member)
        if key is None or key == name:
            return False
    return True


def _factory_hides(call: Node, name: str | None, member: str | None = None) -> bool:
    """Whether a module mock may hand its importers something else for the
    wrapper. An automock does, and so does a factory this cannot read, or one
    spreading anything but the real module. With ``name`` (a function
    wrapper's own module), only an entry of that name, or one whose key cannot
    be read, replaces it; without (a module holding a method's receiver), any
    entry may."""
    found = mock_stubs.ts_factory_object(call)
    if found is None:
        return True
    specifier = mock_stubs.ts_module_specifier(next(iter(mock_stubs.ts_arguments(call)), None))
    for entry in [c for c in found.named_children if c.type != "comment"]:
        if mock_stubs.ts_actual_spread(entry, specifier):
            continue
        if member is not None and mock_stubs.ts_entry_name(entry) is not None and _object_without(entry, member):
            continue  # a factory member's getter, handed an object lacking it
        if entry.type == "spread_element" or name is None or mock_stubs.ts_entry_name(entry) in (None, name):
            return True
    return False


def _mockito_call(node: Node, name: str) -> bool:
    """``x.name(...)`` handed to ts-mockito's ``when`` or ``verify``: the call
    describes a stub rather than making one."""
    if node.type not in MENTION_TYPES or not _is_name(node, name) or not _callee(node):
        return False
    callee = node.parent if node.parent is not None and node.parent.type == "member_expression" else node
    call = callee.parent
    holder = None if call is None else call.parent
    outer = None if holder is None or holder.type != "arguments" else holder.parent
    function = outer.child_by_field_name("function") if outer is not None and outer.type == "call_expression" else None
    return function is not None and function.type == "identifier" and _text(function) in _MOCKITO


def _stub_receivers(root: Node, name: str) -> list[Node]:
    """The receiver of every stub-shaped mention of ``name``: a spy's first
    argument (its member a string or a plain template, as the stub pass reads
    both), the object of ``x.name.mockReturnValue(...)``, and the mock in
    ts-mockito's ``when(x.name(...))``/``verify(x.name(...))``."""
    found: list[Node] = []
    for node in cross_file_keys.walk(root):
        if node.type != "call_expression":
            continue
        callee = mock_stubs.ts_callee(node)
        args = mock_stubs.ts_arguments(node)
        if callee in _SPIES and len(args) == 2 and cross_file_ts._literal(args[1]) == name:
            found.append(args[0])
            continue
        if callee in _MOCKITO and len(args) == 1 and args[0].type == "call_expression":
            inner = args[0].child_by_field_name("function")
            prop = inner.child_by_field_name("property") if inner is not None and inner.type == "member_expression" else None
            if prop is not None and _is_name(prop, name):
                found.append(inner.child_by_field_name("object"))
            continue
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            continue
        returns = function.child_by_field_name("property")
        stubbed = function.child_by_field_name("object")
        if returns is None or _text(returns) not in _RETURNS or stubbed is None:
            continue
        if stubbed.type == "call_expression" and mock_stubs.ts_callee(stubbed) in _MOCKED:
            # `vi.mocked(x.name).mockResolvedValue(v)`
            inner = next(iter(mock_stubs.ts_arguments(stubbed)), None) if len(mock_stubs.ts_arguments(stubbed)) == 1 else None
            if inner is not None and inner.type == "member_expression":
                stubbed = inner
        if stubbed.type != "member_expression":
            continue
        prop = stubbed.child_by_field_name("property")
        if prop is not None and _is_name(prop, name):
            found.append(stubbed.child_by_field_name("object"))
    return [n for n in found if n is not None]


def _mock_of(node: Node, file: ts_scope.File, files: ts_scope.Files) -> ts_scope.TypeRef | None:
    """The class ``const m = mock<C>()`` (jest-mock-extended) or ``const m =
    mock(C)`` (ts-mockito) makes a mock of, when ``mock`` is imported from one
    of those libraries. ``node`` is resolved by scope, so a test may declare
    its own ``m`` beside another test's."""
    bound = file.binding_of(node)
    declarator = None if bound is None else bound.parent
    if declarator is None or declarator.type != "variable_declarator" or declarator.child_by_field_name("name") != bound:
        return None
    value = declarator.child_by_field_name("value")
    function = value.child_by_field_name("function") if value is not None and value.type == "call_expression" else None
    if function is None or function.type != "identifier":
        return None
    imported = file.import_of(_text(function))
    if imported is None or imported.imported != "mock" or imported.specifier not in _MOCK_LIBRARIES:
        return None
    args = mock_stubs.ts_arguments(value)
    type_arguments = value.child_by_field_name("type_arguments")
    if type_arguments is not None:
        written = [c for c in type_arguments.named_children if c.type != "comment"]
        named = written[0] if len(written) == 1 and written[0].type == "type_identifier" and not args else None
    else:
        named = args[0] if len(args) == 1 and args[0].type == "identifier" else None
    return None if named is None else files.class_named(_text(named), file)


def _declaration_of(wrapper: wi.Wrapper, files: ts_scope.Files) -> Node | None:
    file = files.file(wrapper.path)
    if file is None:
        return None
    return next(
        (n for n in cross_file_keys.walk(file.root) if (n.start_byte, n.end_byte) == (wrapper.start, wrapper.end)),
        None,
    )


def _is_field(wrapper: wi.Wrapper, files: ts_scope.Files) -> bool:
    declaration = _declaration_of(wrapper, files)
    return declaration is not None and declaration.type == "public_field_definition"


def _proven_receiver(node: Node, wrapper: wi.Wrapper, file: ts_scope.File, files: ts_scope.Files) -> bool:
    """Whether a stub's receiver is one the call-site proof accepts: a namespace
    import of a function wrapper's module; for a method, an instance of its
    class (written type, ``new``, the exported instance), ``C.prototype``, the
    class itself for a static method, or a library mock of the class; and
    ``vi.mocked(...)`` of any of those."""
    if node.type == "call_expression" and mock_stubs.ts_callee(node) in _MOCKED:
        args = mock_stubs.ts_arguments(node)
        return len(args) == 1 and _proven_receiver(args[0], wrapper, file, files)
    if node.type == "call_expression" and ts_scope.factory_name(wrapper.owner) is None:
        return False
    if wrapper.kind == "function":
        module = files._namespace(node, file)
        return (
            module is not None
            and _same_file(module.path, wrapper.path)
            and _function_reason(wrapper.name, node, wrapper, file, files) is None
        )
    prop = node.child_by_field_name("property") if node.type == "member_expression" else None
    if prop is not None and node.child_by_field_name("optional_chain") is None and _text(prop) == "prototype":
        owner = node.child_by_field_name("object")
        # A field is an own property of each instance, never on the prototype.
        return (
            not wrapper.static
            and not _is_field(wrapper, files)
            and owner is not None
            and files.static_type(owner, file) == ts_scope.TypeRef(wrapper.path, wrapper.owner, True)
        )
    if files.static_type(node, file) == ts_scope.TypeRef(wrapper.path, wrapper.owner, wrapper.static):
        return True
    return (
        not wrapper.static
        and node.type == "identifier"
        and _mock_of(node, file, files) == ts_scope.TypeRef(wrapper.path, wrapper.owner)
    )


def stub_targets(wrapper: wi.Wrapper, index: Index) -> dict[str, mock_stubs.StubTargets]:
    """resolved path -> where a stub of ``wrapper`` may sit in that file, for the
    test-double pass. Only files that may stub (:func:`_stubs`) are looked at,
    and only receivers the call-site proof would accept are kept. Module-mock
    specifiers are kept for a function wrapper's own module: a method is never
    a module's export."""
    files = index.files if index.files is not None else ts_scope.Files(index.repo_dir)
    found: dict[str, mock_stubs.StubTargets] = {}
    for path in index.parsed:
        text = _read(path)
        if text is None or not _stubs(path, text):
            continue
        file = files.file(path, text)
        if file is None:
            continue
        # The stub pass matches a receiver by its spelling, so a spelling is
        # kept only when every stub receiver written that way is proven: one
        # test's mock `m` must not resolve a stub on another test's `m`.
        proven: dict[str, bool] = {}
        for node in _stub_receivers(file.root, wrapper.name):
            spelling = _collapsed(node)
            proven[spelling] = proven.get(spelling, True) and _proven_receiver(node, wrapper, file, files)
        receivers = frozenset(spelling for spelling, ok in proven.items() if ok)
        modules: set[str] = set()
        if wrapper.kind == "function" and _home_reason(wrapper, files) is None:
            for call in _module_mock_calls(file.root):
                specifier = mock_stubs.ts_module_specifier(next(iter(mock_stubs.ts_arguments(call)), None))
                target = None if specifier is None else ts_scope.resolve(path, specifier, index.repo_dir)
                if target is not None and _same_file(target, wrapper.path):
                    modules.add(specifier)
        getters = _getter_mocks(file, path, wrapper, index) if ts_scope.factory_name(wrapper.owner) is not None else frozenset()
        if receivers or modules or getters:
            found[file.path] = mock_stubs.StubTargets(
                receivers, frozenset(modules), resolved=wrapper.promise, getters=getters
            )
    return found


def _getter_mocks(file: ts_scope.File, path: str, wrapper: wi.Wrapper, index: Index) -> frozenset[tuple[str, str]]:
    """``(specifier, getter)`` of each entry of a module mock's factory in
    ``file`` that replaces a getter proven to return the wrapper's factory
    object: an exported top-level function of the mocked module for which
    :meth:`ts_scope.Files.getter_type` names that factory."""
    files = index.files if index.files is not None else ts_scope.Files(index.repo_dir)
    owner = ts_scope.TypeRef(wrapper.path, wrapper.owner)
    found: set[tuple[str, str]] = set()
    for call in _module_mock_calls(file.root):
        specifier = mock_stubs.ts_module_specifier(next(iter(mock_stubs.ts_arguments(call)), None))
        target = None if specifier is None else ts_scope.resolve(path, specifier, index.repo_dir)
        module = None if target is None else files.file(target)
        factory = mock_stubs.ts_factory_object(call)
        if specifier is None or module is None or factory is None:
            continue
        for entry in [c for c in factory.named_children if c.type != "comment"]:
            key = mock_stubs.ts_entry_name(entry)
            if key is not None and files.getter_type(module, key) == owner:
                found.add((specifier, key))
    return frozenset(found)


# --- Class mocks a round settles --------------------------------------------------

#: jest-mock-extended's spelling of a stub, and ts-mockito's.
_MOCK_EXTENDED = frozenset({"jest-mock-extended", "vitest-mock-extended"})
_TS_MOCKITO = frozenset({"ts-mockito", "@typestrong/ts-mockito"})
#: Hooks whose callback runs before each test, after a runner's own resets.
_EACH_HOOKS = frozenset({"beforeEach"})
#: Calls that wipe every mock's stubs at once.
_RESETS_ALL = re.compile(r"(?<![\w$])(?:resetAllMocks|restoreAllMocks)(?![\w$])")
#: Declarations that run wherever their scope starts, before any statement.
_HOISTED = frozenset({"function_declaration", "generator_function_declaration"})
#: Nodes that hand on the value they wrap.
_TRANSPARENT = frozenset({"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"})


@dataclass(frozen=True)
class Settled:
    """The class mocks of one file a test-double round may settle: each is
    stubbed to the kept value before anything else uses it.

    ``text`` is the file before the round, ``calls`` the ``(row, column)``
    of each such ``mock<C>()``/``mock(C)`` call in it. The round only
    deletes lines, so :func:`_still_settled` finds them again after it."""

    text: str
    calls: frozenset[tuple[int, int]]


def _unit_body(block: Node, data: bytes) -> bool:
    """Whether ``block`` is the callback of a test or of ``beforeEach``. Those
    run after a runner's ``resetMocks``; a stub made while the file is
    collected (top level, ``describe``) is wiped before each test by it."""
    function = block.parent
    holder = None if function is None else function.parent
    call = None if holder is None or holder.type != "arguments" else holder.parent
    statement = None if call is None or call.type != "call_expression" else call.parent
    if function is None or function.type not in ("arrow_function", "function_expression") or statement is None:
        return False
    if statement.type != "expression_statement" or statement.named_children[0] != call:
        return False
    return mock_stubs._ts_test(statement, data) is not None or _callee_name(call) in _EACH_HOOKS


def _library_of(name: str, file: ts_scope.File) -> str | None:
    imported = file.import_of(name)
    return None if imported is None else imported.specifier


def _required_parameters(wrapper: wi.Wrapper, files: ts_scope.Files) -> int | None:
    """How many parameters the method takes, when every one is required:
    ts-mockito answers a stub only for a call with as many arguments."""
    text = _read(wrapper.path)
    file = None if text is None else files.file(wrapper.path, text)
    member = None if file is None else file.root.descendant_for_byte_range(wrapper.start, wrapper.end)
    parameters = None if member is None else member.child_by_field_name("parameters")
    if parameters is None:
        return None
    written = [c for c in parameters.named_children if c.type != "comment"]
    for parameter in written:
        pattern = parameter.child_by_field_name("pattern")
        if (
            parameter.type != "required_parameter"
            or parameter.child_by_field_name("value") is not None
            or pattern is None
            or pattern.type != "identifier"
        ):
            return None
    return len(written)


def _kept_stub(
    statement: Node, name: str, library: str, wrapper: wi.Wrapper, treatment: bool, file: ts_scope.File, arity: int | None
) -> bool:
    """``m.<wrapper>.mockReturnValue(<kept>)`` on a jest-mock-extended mock, or
    ``when(m.<wrapper>(anything(), ...)).thenReturn(<kept>)`` on a ts-mockito
    one: a stub every later call of the wrapper on ``m`` answers."""
    call = statement.named_children[0] if statement.type == "expression_statement" and statement.named_children else None
    function = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
    if function is None or function.type != "member_expression" or function.child_by_field_name("optional_chain"):
        return False
    args = mock_stubs.ts_arguments(call)
    if len(args) != 1 or args[0].type != str(treatment).lower():
        return False
    method = _text(function.child_by_field_name("property"))
    target = function.child_by_field_name("object")
    if library in _MOCK_EXTENDED and method == "mockReturnValue":
        return target is not None and _member_of(target, name, wrapper.name)
    if library not in _TS_MOCKITO or method != "thenReturn" or target is None or target.type != "call_expression":
        return False
    when = target.child_by_field_name("function")
    if when is None or when.type != "identifier" or _library_of(_text(when), file) not in _TS_MOCKITO:
        return False
    described = mock_stubs.ts_arguments(target)
    if len(described) != 1 or described[0].type != "call_expression":
        return False
    matchers = mock_stubs.ts_arguments(described[0])
    return (
        _member_of(described[0].child_by_field_name("function"), name, wrapper.name)
        and arity is not None
        and len(matchers) == arity
        and all(_any_matcher(m, file) for m in matchers)
    )


def _member_of(node: Node | None, name: str, member: str) -> bool:
    """Whether ``node`` is ``name.member``, spelled plainly."""
    if node is None or node.type != "member_expression" or node.child_by_field_name("optional_chain"):
        return False
    owner = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    return (
        owner is not None and owner.type == "identifier" and _text(owner) == name
        and prop is not None and prop.type == "property_identifier" and _text(prop) == member
    )


def _any_matcher(node: Node, file: ts_scope.File) -> bool:
    function = node.child_by_field_name("function") if node.type == "call_expression" else None
    return (
        function is not None and function.type == "identifier" and _text(function) == "anything"
        and not mock_stubs.ts_arguments(node) and _library_of("anything", file) in _TS_MOCKITO
    )


def _accounted(use: Node, file: ts_scope.File, repo_dir: str) -> bool:
    """Whether a use of the mock leaves its stubs as they are: one of its
    members, ``instance(m)`` (ts-mockito), or an argument of ``new`` of a
    class imported from a file that is not test code. Anything else hands the
    mock to code that may reset it under another name (a helper, an array's
    ``forEach(mockReset)``, an object read later), so it is not accounted. A
    use inside a function declaration is not either: the declaration is
    hoisted, and may run before the stub."""
    if any(a.type in _HOISTED for a in _ancestors(use)):
        return False
    node = use
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        node = node.parent
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "member_expression":
        # A computed `m[k]` is a `subscript_expression`, and so not accounted.
        return parent.child_by_field_name("object") == node and parent.child_by_field_name("optional_chain") is None
    call = parent.parent if parent.type == "arguments" else None
    if call is None:
        return False
    if call.type == "call_expression":
        function = call.child_by_field_name("function")
        return (
            function is not None and function.type == "identifier" and _text(function) == "instance"
            and _library_of("instance", file) in _TS_MOCKITO and len(mock_stubs.ts_arguments(call)) == 1
        )
    constructor = call.child_by_field_name("constructor") if call.type == "new_expression" else None
    imported = None if constructor is None or constructor.type != "identifier" else file.import_of(_text(constructor))
    target = (
        None
        if imported is None
        else cross_file_ts.resolve_specifier(file.path, imported.specifier, repo_dir, workspace=True)
    )
    text = None if target is None else _read(target)
    return text is not None and not _stubs(target, text)


def _ancestors(node: Node) -> Iterable[Node]:
    while node.parent is not None:
        node = node.parent
        yield node


def _settles(
    call: Node,
    file: ts_scope.File,
    files: ts_scope.Files,
    wrapper: wi.Wrapper,
    treatment: bool,
    arity: int | None,
    repo_dir: str,
) -> bool:
    """Whether a class-mock call makes a mock every use of which sees the
    wrapper stubbed to the kept value: a ``const`` in a test or ``beforeEach``,
    whose first use is such a stub, sitting directly in that callback, and
    whose every other use is accounted for (:func:`_accounted`)."""
    declarator = call.parent
    if declarator is None or declarator.type != "variable_declarator" or declarator.child_by_field_name("value") != call:
        return False
    bound = declarator.child_by_field_name("name")
    declaration = declarator.parent
    block = None if declaration is None else declaration.parent
    if (
        bound is None
        or bound.type != "identifier"
        or declaration.type != "lexical_declaration"
        or _text(declaration.children[0]) != "const"
        or block is None
        or block.type != "statement_block"
        or not _unit_body(block, file.root.text)
        or _mock_of(bound, file, files) != ts_scope.TypeRef(wrapper.path, wrapper.owner)
    ):
        return False
    function = call.child_by_field_name("function")
    library = _library_of(_text(function), file) if function is not None and function.type == "identifier" else None
    name = _text(bound)
    stubs = [
        s for s in block.named_children
        if s.start_byte > declaration.start_byte and _kept_stub(s, name, library or "", wrapper, treatment, file, arity)
    ]
    # `File.binding_of` (through `_mock_of`) proved this the one binding of
    # the name inside its block, and a `const` is seen nowhere else, so every
    # identifier there spelling it is a use of this mock.
    uses = sorted(
        (
            n for n in cross_file_keys.walk(block)
            if n.type in ("identifier", "shorthand_property_identifier") and _text(n) == name and n != bound
        ),
        key=lambda n: n.start_byte,
    )

    def in_stub(node: Node) -> bool:
        return any(s.start_byte <= node.start_byte < s.end_byte for s in stubs)

    return bool(stubs) and bool(uses) and in_stub(uses[0]) and all(in_stub(u) or _accounted(u, file, repo_dir) for u in uses)


def settled_mocks(wrapper: wi.Wrapper, index: Index, treatment: bool) -> dict[str, Settled]:
    """resolved path -> the class mocks of a method wrapper a test-double
    round may settle there, judged before it runs.

    An unstubbed class mock answers ``undefined``/``null`` for the wrapper, so
    the index counts every one as a double, and goes on counting it once the
    round has deleted its stub. A mock found here is stubbed to the kept value
    before any use, and deleting that stub is what the round does, so after
    the round the index passes it over (:class:`Index` with ``settled``). A
    file whose runner resets every mock (``resetAllMocks``) settles none."""
    if wrapper.kind != "method" or wrapper.static:
        return {}
    files = index.files if index.files is not None else ts_scope.Files(index.repo_dir)
    arity = _required_parameters(wrapper, files)
    found: dict[str, Settled] = {}
    for path in sorted({p for p, _, _ in index.test_doubles}):
        text = _read(path)
        if text is None or not _stubs(path, text) or _RESETS_ALL.search(text):
            continue
        file = files.file(path, text)
        if file is None:
            continue
        calls = frozenset(
            (c.start_point[0], c.start_point[1])
            for c in _class_stubs(file, wrapper, _reach(path, file, wrapper, index.repo_dir, index.packages))
            if _settles(c, file, files, wrapper, treatment, arity, index.repo_dir)
        )
        if calls:
            found[str(Path(path).resolve())] = Settled(text, calls)
    return found


def _still_settled(call: Node, text: str, settled: Settled) -> bool:
    """Whether a class-mock call in ``text`` (the file after a round) is one
    of ``settled.calls``. The round only deletes whole lines, so the file's
    lines are a subsequence of the old ones. Every old line the call's line
    could be, under any such match, must hold a settled call at its column:
    a line the match cannot pin down is judged by every reading of it."""
    old = settled.text.split("\n")
    new = text.split("\n")
    earliest: list[int] = []
    i = 0
    for line in new:
        while i < len(old) and old[i] != line:
            i += 1
        if i == len(old):
            return False  # not a deletion of whole lines
        earliest.append(i)
        i += 1
    latest = [0] * len(new)
    i = len(old) - 1
    for j in range(len(new) - 1, -1, -1):
        while old[i] != new[j]:
            i -= 1
        latest[j] = i
        i -= 1
    row, column = call.start_point
    candidates = [k for k in range(earliest[row], latest[row] + 1) if old[k] == new[row]]
    return all((k, column) in settled.calls for k in candidates)


# --- Call sites -----------------------------------------------------------------


def _in_with(node: Node) -> bool:
    """Inside a ``with`` body, where a bare name may be a property of the object."""
    parent = node.parent
    while parent is not None:
        if parent.type == "with_statement":
            return True
        parent = parent.parent
    return False


def names_only(node: Node, kind: str = "method") -> bool:
    """Whether a mention only names something.

    The wrapper's own declaration does (uniqueness leaves it the only one),
    and so does any bare identifier when the wrapper is a method: a method is
    reached only through a receiver, so an identifier of that name is a local.
    An import or export specifier does NOT: one left standing after the fold
    keeps the wrapper, or deleting it would break the import."""
    if kind == "method" and node.type == "identifier":
        return not _in_with(node)
    parent = node.parent
    if parent is None or _member_name(parent) != node:
        return False
    if parent.type == "function_declaration":
        return ts_scope.top_level(parent)
    if parent.type in ("method_definition", "public_field_definition", "pair"):
        return _top_member(parent)
    if parent.type == "variable_declarator":
        return parent.parent is not None and ts_scope.top_level(parent.parent)
    return False


def _top_member(member: Node) -> bool:
    """A member of a class body (a method or a field), or of the object
    literal a top-level ``const`` holds (a method or a key)."""
    holder = member.parent
    if holder is None:
        return False
    if holder.type == "class_body":
        return member.type != "pair"
    return (
        holder.type == "object"
        and member.type != "public_field_definition"
        and (ts_scope.holds_object_literal(holder.parent) or _factory_literal(holder))
    )


def _factory_literal(literal: Node) -> bool:
    """Whether an object literal is the one a top-level factory function returns."""
    function = literal.parent
    while function is not None and function.type in ("parenthesized_expression", "return_statement", "statement_block"):
        function = function.parent
    return function is not None and ts_scope.factory_object(function) == literal


def members(root: Node, name: str) -> list[Node]:
    """Every declaration of ``name`` a close-out may delete: a top-level
    function or single-declarator ``const`` (with its ``export``), a method or
    field of a top-level class, or a method or key of a top-level ``const``
    object literal."""
    found: list[Node] = []
    for statement in root.named_children:
        node = statement.child_by_field_name("declaration") if statement.type == "export_statement" else statement
        if node is None:
            continue
        if node.type == "function_declaration":
            declared = node.child_by_field_name("name")
            if declared is not None and _is_name(declared, name):
                found.append(statement)
        elif node.type == "lexical_declaration":
            declarators = [c for c in node.named_children if c.type == "variable_declarator"]
            declared = declarators[0].child_by_field_name("name") if len(declarators) == 1 else None
            if declared is not None and _is_name(declared, name):
                found.append(statement)
            held = object_literal(node)
            for member in [] if held is None else held[1].named_children:
                declared = _member_name(member) if member.type in ("method_definition", "pair") else None
                if declared is not None and declared.type == "property_identifier" and _is_name(declared, name):
                    found.append(member)
        if node.type == "function_declaration":
            literal = ts_scope.factory_object(node)
            for member in [] if literal is None else literal.named_children:
                declared = _member_name(member) if member.type in ("method_definition", "pair") else None
                if declared is not None and declared.type == "property_identifier" and _is_name(declared, name):
                    found.append(member)
        elif node.type == "class_declaration":
            body = node.child_by_field_name("body")
            for member in [] if body is None else body.named_children:
                declared = member.child_by_field_name("name")
                if (
                    member.type in ("method_definition", "public_field_definition")
                    and declared is not None
                    and _is_name(declared, name)
                ):
                    found.append(member)
    return found


def member_end(member: Node) -> int:
    """Where deleting ``member`` ends: past the ``;`` after a class field, or
    the ``,`` after an object literal's member, which the grammar keeps as the
    member's next sibling rather than inside it, and past a comment before
    that separator or after it on the same line. A separator left behind
    would start the next member's line with ``,``."""
    after = member.next_sibling
    while after is not None and after.type == "comment":
        after = after.next_sibling
    if after is None or after.type not in (";", ","):
        return member.end_byte
    end = after.end_byte
    trailing = after.next_sibling
    if trailing is not None and trailing.type == "comment" and trailing.start_point[0] == after.end_point[0]:
        end = trailing.end_byte
    return end


#: Argument nodes that do work inlining the call would drop.
_DOES_WORK = frozenset(
    {
        "call_expression",
        "new_expression",
        "assignment_expression",
        "augmented_assignment_expression",
        "update_expression",
        "await_expression",
        "yield_expression",
        "spread_element",
    }
)


def _argument_has_call(call: Node) -> bool:
    arguments = call.child_by_field_name("arguments")
    for node in [] if arguments is None else cross_file_keys.walk(arguments):
        if node.type in _DOES_WORK:
            return True
        if node.type == "unary_expression" and _has(node, "delete"):
            return True
    return False


_PARAMETER_DEFAULTS = {
    "required_parameter": "value",
    "optional_parameter": "value",
    "assignment_pattern": "right",
    "object_assignment_pattern": "right",
}


def _default_does_work(wrapper: wi.Wrapper, files: ts_scope.Files) -> bool:
    """Whether a default anywhere in the wrapper's parameters, a destructuring
    one included, may run code (``u = currentUser()``).

    Asked of the declaration, not of the call: an argument the call does pass
    can still be ``undefined`` at run time, or lack the property a pattern
    defaults, and either one runs the default. Inlining the call would drop
    it. "Inert" is the stranded-binding pass's whitelist."""
    from flag_cleanup.unused import _subtree_is_inert

    declaration = _declaration_of(wrapper, files)
    function = None if declaration is None else next(
        (n for n in cross_file_keys.walk(declaration) if n.type in _FUNCTION_LIKE), None
    )
    if function is None:
        return True
    parameters = function.child_by_field_name("parameters")
    for node in [] if parameters is None else cross_file_keys.walk(parameters):
        field_name = _PARAMETER_DEFAULTS.get(node.type)
        default = None if field_name is None else node.child_by_field_name(field_name)
        if default is not None and not _subtree_is_inert(default, wrapper.language):
            return True
    return False


def _plain_call(callee: Node, awaited: bool = False) -> Node | None:
    """The call ``callee`` is the function of, when it is a plain one: no
    optional call, no type arguments, no tagged template, not awaited unless
    ``awaited`` allows it."""
    call = callee.parent
    if call is None or call.type != "call_expression" or call.child_by_field_name("function") != callee:
        return None
    arguments = call.child_by_field_name("arguments")
    if arguments is None or arguments.type != "arguments":
        return None
    if call.child_by_field_name("type_arguments") is not None or _has(call, "?."):
        return None
    if not awaited and call.parent is not None and call.parent.type == "await_expression":
        return None
    return call


def _promise_span(call: Node, callee: Node) -> tuple[tuple[int, int], str]:
    """Where a proven call of a Promise wrapper is marked, and the suffix.

    Awaited, or a statement of its own, it is marked like any other call: the
    engine folds ``await <literal>`` to the literal and deletes the discarded
    statement. Anywhere else its value is the Promise, so the whole call
    becomes ``Promise.resolve(<literal>)`` and keeps its type."""
    parent = call.parent
    if parent is not None and parent.type in ("await_expression", "expression_statement"):
        return (callee.start_byte, callee.end_byte), ""
    return (call.start_byte, call.end_byte), f"({ts_async_reads.PROMISE_TAG})"


def _plain_chain(node: Node) -> bool:
    """``this``, an identifier, or ``.field`` steps on one: nothing to evaluate."""
    if node.type in ("this", "identifier"):
        return True
    if node.type != "member_expression" or node.child_by_field_name("optional_chain") is not None:
        return False
    prop = node.child_by_field_name("property")
    obj = node.child_by_field_name("object")
    return prop is not None and prop.type == "property_identifier" and obj is not None and _plain_chain(obj)


def _import_reason(imported: ts_scope.Import | None, wrapper: wi.Wrapper, namespace: bool) -> str | None:
    if imported is None:
        return wi.RECEIVER_UNPROVEN
    wanted = None if namespace else wrapper.name
    if imported.module != wrapper.path or imported.imported != wanted or imported.type_only or not imported.top:
        return wi.IMPORT_UNRESOLVED
    return None


def _home_reason(wrapper: wi.Wrapper, files: ts_scope.Files) -> str | None:
    """Whether the wrapper's own file lets an importer reach the declaration."""
    # The wrapper's own file must still bind the name once, as its declaration:
    # an assignment there makes an importer's call reach something else.
    home = files.file(wrapper.path)
    declared = None if home is None else home.binding(wrapper.name)
    if declared is None or not names_only(declared, "function"):
        return wi.RECEIVER_UNPROVEN
    # And export that declaration itself: an ``export *``, an ``export { f }
    # from`` or a ``module.exports = ...`` there can hand importers of the
    # name something else, whatever the file declares.
    if not ts_scope._exports_directly(home.root, wrapper.name):
        return wi.IMPORT_UNRESOLVED
    return None


def _function_reason(
    local: str, receiver: Node | None, wrapper: wi.Wrapper, file: ts_scope.File, files: ts_scope.Files
) -> str | None:
    if file.path != wrapper.path:
        home = _home_reason(wrapper, files)
        if home is not None:
            return home
    if receiver is None:
        if file.path == wrapper.path:
            bound = file.binding(local)
            return None if bound is not None and names_only(bound, "function") else wi.RECEIVER_UNPROVEN
        if file.binding(local) is None:
            return wi.RECEIVER_UNPROVEN
        return _import_reason(file.import_of(local), wrapper, namespace=False)
    if receiver.type != "identifier" or file.binding(_text(receiver)) is None:
        return wi.RECEIVER_UNPROVEN
    return _import_reason(file.import_of(_text(receiver)), wrapper, namespace=True)


def _method_reason(receiver: Node | None, wrapper: wi.Wrapper, file: ts_scope.File, files: ts_scope.Files) -> str | None:
    # A getter call evaluates to its one object and does nothing else, which
    # ``static_type`` proves before it names that object's owner.
    if receiver is None or not (_plain_chain(receiver) or receiver.type == "call_expression"):
        return wi.RECEIVER_UNPROVEN
    expected = ts_scope.TypeRef(wrapper.path, wrapper.owner, wrapper.static)
    return None if files.static_type(receiver, file) == expected else wi.RECEIVER_UNPROVEN


def _judge(path: str, node: Node, wrapper: wi.Wrapper, file: ts_scope.File, files: ts_scope.Files) -> wi.Occurrence:
    parent = node.parent
    if _in_with(node):
        return wi.Occurrence(path, wi.line_of(node), wi.text_of(parent or node), None, wi.RECEIVER_UNPROVEN)
    if parent is not None and parent.type == "export_specifier":
        return wi.Occurrence(path, wi.line_of(node), wi.text_of(parent), None, wi.IMPORT_UNRESOLVED)
    callee, receiver = node, None
    if node.type == "property_identifier":
        if (
            parent is None
            or parent.type != "member_expression"
            or parent.child_by_field_name("property") != node
            or parent.child_by_field_name("optional_chain") is not None
        ):
            return wi.Occurrence(path, wi.line_of(node), wi.text_of(parent or node), None, wi.RECEIVER_UNPROVEN)
        callee, receiver = parent, parent.child_by_field_name("object")
    elif node.type != "identifier":
        return wi.Occurrence(path, wi.line_of(node), wi.text_of(parent or node), None, wi.RECEIVER_UNPROVEN)
    call = _plain_call(callee, awaited=wrapper.promise)
    if call is None:
        return wi.Occurrence(path, wi.line_of(node), wi.text_of(callee.parent or callee), None, wi.RECEIVER_UNPROVEN)
    if wrapper.kind == "function":
        reason = _function_reason(_text(node), receiver, wrapper, file, files)
    else:
        reason = _method_reason(receiver, wrapper, file, files)
        if reason is None and not wrapper.static and _built_by_the_test(receiver, file, wrapper):
            # The wrapper's own unit test goes with the member when the member
            # is all it is about; any other test of an object it built keeps it.
            title = _owned_test(call, receiver, wrapper, file)
            if title is not None:
                return wi.Occurrence(
                    path, wi.line_of(node), wi.text_of(call), None, wi.SIBLING_RECEIVER, _text(receiver), True, scope=title
                )
            reason = wi.TEST_DOUBLE
    if reason is None and (_argument_has_call(call) or _default_does_work(wrapper, files)):
        reason = wi.ARGUMENT_HAS_CALL
    if reason is not None:
        return wi.Occurrence(path, wi.line_of(node), wi.text_of(call), None, reason)
    if wrapper.promise:
        span, suffix = _promise_span(call, callee)
        return wi.Occurrence(path, wi.line_of(node), wi.text_of(call), span, None, suffix=suffix)
    return wi.Occurrence(path, wi.line_of(node), wi.text_of(call), (callee.start_byte, callee.end_byte), None)


def _built_by_the_test(receiver: Node | None, file: ts_scope.File, wrapper: wi.Wrapper | None = None) -> bool:
    """Whether a test reaches a CLASS's instance member through an object it
    may have built itself: ``new Flags(fakeClient)`` in the test, a typed
    parameter it is handed, a field of its own.

    Such an object reads the flag through whatever the test injected, so the
    test is about that read (``expect(flags.isX()).toBe(true)``), and
    inlining the call writes ``expect(false).toBe(true)``. In a test file only
    an instance imported from code under test (``import { flags }``) is
    accepted; any other receiver keeps the member, reported ``test-double``.

    A heuristic, unlike a factory member's one-instance rule
    (:func:`factory_escapes`): an instance a helper module builds with a fake
    and a test imports still passes it."""
    if receiver is None or not _stubs(file.path, file.source):
        return False
    if wrapper is not None and ts_scope.factory_name(wrapper.owner) is not None:
        # A factory's object is the test's own when the test binds it to a
        # local `const x = buildFlags(...)`; the instance a test imports or is
        # handed is the one-instance rule's (:func:`factory_escapes`).
        return receiver.type == "identifier" and _local_factory_call(receiver, file) is not None
    root = receiver.child_by_field_name("function") if receiver.type == "call_expression" else receiver
    return root is None or root.type != "identifier" or file.import_of(_text(root)) is None


def _local_factory_call(receiver: Node, file: ts_scope.File) -> Node | None:
    """The call ``F(...)`` in the ``const x = F(...)`` that a receiver ``x``
    is bound by."""
    bound = file.binding_of(receiver)
    declarator = None if bound is None else bound.parent
    if declarator is None or declarator.type != "variable_declarator" or declarator.child_by_field_name("name") != bound:
        return None
    value = declarator.child_by_field_name("value")
    declaration = declarator.parent
    if value is None or value.type != "call_expression" or declaration is None or not _has(declaration, "const"):
        return None
    return value


#: What a test may chain after `vi.fn()`/`jest.fn()` to script a fake.
_FAKE_CHAIN = frozenset(
    {"mockResolvedValue", "mockResolvedValueOnce", "mockReturnValue", "mockReturnValueOnce", "mockImplementation", "mockImplementationOnce"}
)
_FAKE_FUNCTIONS = frozenset({"vi.fn", "jest.fn"})
_PLAIN_VALUES = frozenset({"true", "false", "null", "undefined", "number", "string", "template_string", "regex"})
_WRAPPING = frozenset({"as_expression", "satisfies_expression", "non_null_expression", "parenthesized_expression"})


def _inert_value(node: Node | None, known: set[str]) -> bool:
    """Whether an expression only builds a fake or states a literal: a literal,
    a function, `vi.fn()` scripted with `mock*` calls, an object of such, or a
    name of a local already declared that way (``known``). Nothing is called
    but the mocking library's own functions."""
    if node is None:
        return False
    if node.type in _PLAIN_VALUES or node.type in ("arrow_function", "function_expression", "function"):
        return node.type != "template_string" or not any(c.type == "template_substitution" for c in node.named_children)
    if node.type in _WRAPPING:
        inner = [c for c in node.named_children if c.type not in ("comment", "type_annotation") and c.is_named]
        return bool(inner) and _inert_value(inner[0], known)
    if node.type == "identifier":
        return _text(node) in known or _text(node) == "undefined"
    if node.type == "array":
        return all(_inert_value(c, known) for c in node.named_children if c.type != "comment")
    if node.type == "object":
        for member in node.named_children:
            if member.type == "comment":
                continue
            if member.type == "method_definition":
                continue
            if member.type == "shorthand_property_identifier":
                if _text(member) not in known:
                    return False
                continue
            if member.type != "pair":
                return False
            key = member.child_by_field_name("key")
            if key is None or key.type == "computed_property_name" or not _inert_value(member.child_by_field_name("value"), known):
                return False
        return True
    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        if function is None:
            return False
        if mock_stubs.ts_callee(node) in _FAKE_FUNCTIONS:
            return all(_inert_value(a, known) for a in mock_stubs.ts_arguments(node))
        if function.type == "member_expression" and (prop := function.child_by_field_name("property")) is not None:
            if _text(prop) in _FAKE_CHAIN:
                return _inert_value(function.child_by_field_name("object"), known) and all(
                    _inert_value(a, known) for a in mock_stubs.ts_arguments(node)
                )
    return False


def _test_body(node: Node, data: bytes) -> tuple[Node, Node, str] | None:
    """``(statement, body, title)`` of the ``it("title", () => { ... })``
    around ``node``, when ``node`` is in that callback's own body."""
    current = node.parent
    while current is not None:
        if current.type == "statement_block" and current.parent is not None and current.parent.type in (
            "arrow_function", "function_expression", "function"
        ):
            call = current.parent.parent.parent if current.parent.parent is not None else None
            statement = call.parent if call is not None else None
            if statement is not None and statement.type == "expression_statement":
                title = mock_stubs._ts_test(statement, data)
                if title is not None:
                    return statement, current, title
            # A nested function: not the test's own body.
            return None
        current = current.parent
    return None


def _owned_test(call: Node, receiver: Node | None, wrapper: wi.Wrapper, file: ts_scope.File) -> str | None:
    """The title of the test whose ONLY subject is this wrapper's member, called
    on an instance the test builds itself with an injected fake; else ``None``.

    The test builds the object (``const flags = new Flags(fake)`` or
    ``buildFlags(fake)``) from literals and `vi.fn()` fakes, and every other
    statement is an assertion about a call of this member on it, or about the
    fake it was given. A call of anything else (production code, another
    member), a reference to the instance that is not a call of this member, or
    a hook, a helper or a ``describe``-level instance keeps the test: it
    asserts more than the member forwards. The title must be the file's only
    one of that name."""
    if receiver is None or receiver.type != "identifier":
        return None
    data = file.source.encode("utf-8")
    found = _test_body(call, data)
    if found is None:
        return None
    statement, body, title = found
    if ".only" in "".join(statement.named_children[0].child_by_field_name("function").text.decode().split()):
        return None  # a focused test: deleting it would let the rest of the file run
    bound = file.binding_of(receiver)
    declarator = None if bound is None else bound.parent
    if declarator is None or declarator.type != "variable_declarator" or declarator.parent is None:
        return None
    if declarator.parent.parent is None or declarator.parent.parent != body:
        return None
    titles = [
        mock_stubs._ts_test(n, data)
        for n in cross_file_keys.walk(file.root)
        if n.type == "expression_statement"
    ]
    if titles.count(title) != 1:
        return None
    if not ts_emptied_tests.survives(file.source, ts_scope.grammar(file.path), wrapper.name):
        return None  # deleting it would leave a test file with no test that cannot go
    instance = _text(receiver)
    known: set[str] = set()
    subjects: set[str] = set()  # locals holding a result of the member
    fakes: set[str] = set()
    for item in [c for c in body.named_children if c.type != "comment"]:
        if item.type == "lexical_declaration" and _has(item, "const"):
            for declared in [c for c in item.named_children if c.type == "variable_declarator"]:
                name, value = declared.child_by_field_name("name"), declared.child_by_field_name("value")
                if name is None or name.type != "identifier" or value is None:
                    return None
                label = _text(name)
                if label == instance:
                    construction = value
                    while construction.type in _WRAPPING:
                        inner = [c for c in construction.named_children if c.is_named and c.type != "comment"]
                        construction = inner[0] if inner else construction
                        if not inner:
                            break
                    if construction.type == "new_expression":
                        arguments = construction.child_by_field_name("arguments")
                    elif construction.type == "call_expression":
                        arguments = construction.child_by_field_name("arguments")
                    else:
                        return None
                    if arguments is None or not all(_inert_value(a, known) for a in arguments.named_children if a.type != "comment"):
                        return None
                    known.add(label)
                elif _inert_value(value, known):
                    known.add(label)
                    fakes.add(label)
                elif _member_result(value, instance, wrapper.name):
                    subjects.add(label)
                else:
                    return None
            continue
        if item.type != "expression_statement" or not _subject_assertion(item, instance, wrapper.name, subjects, fakes, known):
            return None
    return title if instance in known else None


def _member_result(value: Node, instance: str, member: str) -> bool:
    """``[await] instance.member(<plain arguments>)``."""
    if value.type == "await_expression":
        inner = [c for c in value.named_children if c.type != "comment"]
        value = inner[0] if len(inner) == 1 else value
    if value.type != "call_expression" or value.child_by_field_name("type_arguments") is not None:
        return False
    function = value.child_by_field_name("function")
    if function is None or function.type != "member_expression" or function.child_by_field_name("optional_chain") is not None:
        return False
    obj, prop = function.child_by_field_name("object"), function.child_by_field_name("property")
    if obj is None or prop is None or obj.type != "identifier" or _text(obj) != instance or _text(prop) != member:
        return False
    return all(_plain_argument(a) for a in mock_stubs.ts_arguments(value))


def _plain_argument(node: Node) -> bool:
    return node.type in _PLAIN_VALUES or (
        node.type == "object" and all(_plain_argument(c.child_by_field_name("value") or c) for c in node.named_children if c.type == "pair")
    )


def _subject_assertion(
    statement: Node, instance: str, member: str, subjects: set[str], fakes: set[str], known: set[str]
) -> bool:
    """``expect(<subject>).matcher(<literals>)`` (``await``/``.resolves`` and
    ``.not`` allowed), the subject being a call of the member on the
    instance, a local holding one's result, or an injected fake."""
    chain = statement.named_children[0] if statement.named_children else None
    if chain is not None and chain.type == "await_expression":
        inner = [c for c in chain.named_children if c.type != "comment"]
        chain = inner[0] if len(inner) == 1 else None
    expect = None
    node = chain
    # Walk down `.a.b(...)` to the `expect(...)` at the base.
    while node is not None:
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function is not None and function.type == "identifier" and _text(function) == "expect":
                expect = node
                break
            for argument in mock_stubs.ts_arguments(node):
                if not _plain_matcher_argument(argument):
                    return False
            node = function
        elif node.type == "member_expression":
            node = node.child_by_field_name("object")
        else:
            return False
    if expect is None:
        return False
    arguments = mock_stubs.ts_arguments(expect)
    if len(arguments) != 1:
        return False
    subject = arguments[0]
    if subject.type == "identifier":
        return _text(subject) in subjects or _text(subject) in fakes
    held = subject
    while held.type == "member_expression" and held.child_by_field_name("optional_chain") is None:
        held = held.child_by_field_name("object")
        if held is None:
            return False
    if held.type == "identifier" and held != subject:
        return _text(held) in fakes  # `client.isEnabled`: a member of the injected fake
    return _member_result(subject, instance, member)


def _plain_matcher_argument(node: Node) -> bool:
    """A matcher's argument: a literal, or an object, array or ``expect.<m>()``
    of those."""
    if node.type in _PLAIN_VALUES:
        return node.type != "template_string" or not any(c.type == "template_substitution" for c in node.named_children)
    if node.type == "object":
        return all(
            c.type == "pair" and _plain_matcher_argument(c.child_by_field_name("value") or c)
            for c in node.named_children
            if c.type != "comment"
        )
    if node.type == "array":
        return all(_plain_matcher_argument(c) for c in node.named_children if c.type != "comment")
    if node.type == "call_expression":
        return mock_stubs.ts_callee(node).startswith("expect.") and all(
            _plain_matcher_argument(a) for a in mock_stubs.ts_arguments(node)
        )
    return False


def _is_import_binding(node: Node, file: ts_scope.File) -> bool:
    """Whether ``node`` is the identifier of the name that an import or a
    ``require`` binds (``import { f }``, ``import f``, ``const { f } = require(...)``),
    which the stranded-import pass owns."""
    return file.binding(_text(node)) == node and file.import_of(_text(node)) is not None


def _binds_import(node: Node, file: ts_scope.File, kind: str) -> bool:
    """Whether a mention of a function wrapper is only an import binding."""
    return kind == "function" and node.type != "property_identifier" and _is_import_binding(node, file)


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    """Every mention of the wrapper in one file, proven or refused.

    An import specifier is not judged: the stranded-import pass removes it
    once its last use folds, and the close-out's re-scan keeps the wrapper
    if one survives. A function imported under an alias is looked for by
    the alias."""
    files = index.files if index.files is not None else ts_scope.Files(index.repo_dir)
    file = files.file(path, source)
    if file is None:
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    described = index.declaration_file is not None and _same_file(path, index.declaration_file)
    names = {wrapper.name}
    if wrapper.kind == "function":
        names |= {
            i.local for i in file.imports if i.imported == wrapper.name and i.module == wrapper.path
        }
    found: list[wi.Occurrence] = []
    for node in cross_file_keys.walk(file.root):
        if node.type not in MENTION_TYPES or _text(node) not in names:
            continue
        if node.type != "identifier" and _text(node) != wrapper.name:
            continue  # an alias is only ever an identifier
        parent = node.parent
        if described and describing_name(node, wrapper):
            # Not a refusal on its own: the proof lets it go with the wrapper
            # when the run may rewrite the file.
            found.append(wi.Occurrence(path, wi.line_of(node), wi.text_of(parent), None, wi.DECLARATION_FILE))
            continue
        if parent is not None and parent.type == "import_specifier":
            continue
        if _binds_import(node, file, wrapper.kind):
            continue  # ``const { f } = require(...)``: the binding, not a use
        if names_only(node, wrapper.kind):
            continue
        found.append(_judge(path, node, wrapper, file, files))
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


# --- Registration -----------------------------------------------------------------


def _index(repo_dir: str, wrapper: wi.Wrapper, settled: Mapping[str, Settled] | None = None) -> Index:
    return Index.build(repo_dir, wrapper, settled)


def _parser(language: str) -> Callable[[str], Node]:
    from flag_cleanup import ts_syntax

    return lambda source: ts_syntax._parse(source, language)


_FAMILY = (
    ("ts", ts_scope.TS_EXTENSIONS),
    ("tsx", ts_scope.TSX_EXTENSIONS),
    ("js", ts_scope.JS_EXTENSIONS),
)

for _language, _extensions in _FAMILY:
    wi.register(
        wi.Profile(
            language=_language,
            extensions=_extensions,
            parse=_parser(_language),
            read_test=read_test,
            discover=discover,
            occurrences=occurrences,
            index=_index,
            qualify=qualify,
            strings=_STRINGS,
            foreign_extensions=(),
            names_only=names_only,
            members=members,
            member_end=member_end,
            mention_types=MENTION_TYPES,
            siblings=tuple(other for other, _ in _FAMILY if other != _language),
            inert_annotations=frozenset(),
            stub_targets=stub_targets,
            settled_mocks=settled_mocks,
            test_title=lambda path, source, node: _stubs(path, source) and _test_title(node),
            describes=describing_name,
            without_descriptions=without_descriptions,
        )
    )
