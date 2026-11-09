"""Whether a test that mocks a callee's module can see an argument passed to it.

:mod:`flag_cleanup.ts_default_arguments` leaves out an argument equal to the
callee's own default: the real function cannot tell the two calls apart. A
test that replaces the callee's module (``vi.mock``/``jest.mock``) runs a
stand-in instead, and the stand-in can: a ``vi.fn()`` records every call it
gets, for an assertion such as ``toHaveBeenCalledWith`` to read back, and an
implementation reads its parameters. So a drop is only safe when no stand-in
can read the argument and nothing can read a stand-in's record of its calls.

The proof is by name across the whole checkout, built on
:mod:`flag_cleanup.ts_exported_props`' proof for an exported component, with
a call in place of a JSX element: every spelling of the callee's name in the
repository's TypeScript and JavaScript must be one of a short list of
shapes, and every specifier that resolves to the declaring module must be a
plain import of names, a side-effect import, a named re-export or a mock.
Anything else refuses. A re-export, a default import, an alias, the function
handed on as a value or asserted on, and a string spelling its name
(``spyOn(module, 'name')``) all refuse, and so does a namespace import or an
``import()`` unless the module is read only by name (``ns.name``,
``const { name } = await import(…)``), so the only ways left to reach a
stand-in are the shapes below, and each is checked. A specifier that resolves
to no file, is not relative and names no package the importing file can load (a
root-relative path, a bundler-only alias, a ``moduleNameMapper`` key) may still
name the declaring file to the tool that runs the test, so a module importing
through one refuses, as a mock of one does.

In a test file, besides an import of the name and a call to it:

* **a factory key.** ``vi.mock(spec, () => ({ name: <stand-in> }))``, where
  the stand-in is ``vi.fn()``/``jest.fn()``, either given an implementation,
  or an inline function. A function reads only as many arguments as it
  declares parameters, so it can see the argument only when it declares more
  than the argument's position; a rest parameter, ``arguments`` or ``eval``
  can see every one in a file
  that runs in sloppy mode (no ``import``/``export``, no ``"use strict"``) a
  ``function`` can also read them through ``self.arguments`` or a caller's
  ``.caller.arguments``, so only an arrow function is followed there. ``vi`` and
  ``jest`` go by the local name an import from ``vitest`` or ``@jest/globals``
  gives them, and ``x.mock(spec, () => …)`` on any receiver is a runner call;
* **a receiver that only sets what the stand-in does.** ``vi.mocked(name)``,
  ``name`` or ``name as Mock`` followed by one of ``mockReturnValue``,
  ``mockResolvedValue``, ``mockRejectedValue`` (and their ``Once`` forms),
  ``mockReturnThis``, ``mockClear``, ``mockReset``, ``mockRestore``,
  ``mockName``, or ``mockImplementation``/``mockImplementationOnce`` handed an
  inline function, as a statement of its own (the call returns the stand-in);
* **one ``const`` alias** of such a receiver (``const m = name as Mock``,
  ``const m = vi.mocked(name)``), unexported, every use of which is a
  receiver of that kind.

Every mock of the declaring module is checked, whether or not it spells the
name:

* it is in a test file (a ``__tests__`` directory, a ``.test``/``.spec``
  file, or one that declares a titled test), never a setup file or helper;
* a factory is an inline function taking at most the original-module loader,
  returning an object literal, either as its body or after ``const``
  bindings of the module's own original. The object holds plain keys,
  methods not named for the callee, and at most one spread of that original
  (``...(await importOriginal())``, ``...jest.requireActual(spec)`` with the
  same ``spec``), which hands out only the real exports. A computed key, any
  other spread, a getter or method named for the callee, ``this`` and
  ``arguments`` refuse, and so does a factory handed in by name;
* without a factory, the runner fills each export with a recording stand-in,
  unless a ``__mocks__`` file supplies the module: one anywhere in the
  checkout refuses.

Every mock anywhere names its module plainly: a computed specifier refuses,
and so does one that resolves to nothing unless it is a package the mocking
file can load (a Node built-in, a dependency in a ``package.json`` above it,
or one installed in a ``node_modules`` above it). A specifier naming the
declaring file's own package by name refuses, since a workspace link
resolves it there. The callee must be imported straight from its declaring
module, under its own name.

The answer is the number of leading arguments some stand-in can see (``0``
when none can see any), or ``None`` when one can see them all or the proof
refuses. It is computed once per callee and kept in the ``cache`` a caller
hands in for its run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_ts, source_index, ts_scope
from flag_cleanup.ctor_param_ts import _extensions
from flag_cleanup.ts_constant_props import _is_module
from flag_cleanup.ts_exported_props import (
    _SPECIFIER,
    _File,
    _exact_aliases,
    _exported_function,
    _plausible,
    _outward,
    _specifier,
    _users,
    original_call,
)
from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

#: The calls that replace a module and that this proof can follow.
_MOCKS = frozenset({"mock", "doMock"})
#: The calls that put a module back.
_UNMOCKS = frozenset({"unmock", "doUnmock"})
#: The calls that replace a module, or hand out a replacement, in a way this
#: proof does not follow.
_OTHER_MOCKS = frozenset(
    {"setMock", "unstable_mockModule", "createMockFromModule", "requireMock", "importMock"}
)

#: Methods that set what a stand-in returns or forget its calls, and read no
#: argument.
_SETTERS = frozenset(
    {
        "mockReturnValue",
        "mockReturnValueOnce",
        "mockResolvedValue",
        "mockResolvedValueOnce",
        "mockRejectedValue",
        "mockRejectedValueOnce",
        "mockReturnThis",
        "mockClear",
        "mockReset",
        "mockRestore",
        "mockName",
    }
)
#: Methods that give a stand-in an implementation, which reads its parameters.
_IMPLEMENTERS = frozenset({"mockImplementation", "mockImplementationOnce"})

_FUNCTIONS = frozenset({"arrow_function", "function_expression", "function"})

#: Expressions that change a type and nothing else.
_CASTS = frozenset({"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"})

#: Where a test runner reads the mocks: ``vi.mock(``, ``jest.doMock(``, ``x.mock (``.
_MOCK_TEXT = re.compile(
    r"\.\s*(?:mock|doMock|unmock|doUnmock|setMock|unstable_mockModule|createMockFromModule"
    r"|requireMock|importMock)\s*\("
)

#: Mocks this proof does not follow at all: automock, and the ``node:test`` and
#: ``bun:test`` mock APIs.
_UNFOLLOWED = re.compile(r"enableAutomock|\bmock\s*\.\s*(?:module|method|getter|setter)\s*\(")

#: Node's built-in modules, which a bare specifier names without a package.
_BUILTINS = frozenset(
    {
        "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "crypto",
        "dgram", "dns", "events", "fs", "fs/promises", "http", "http2", "https", "module",
        "net", "os", "path", "perf_hooks", "process", "querystring", "readline", "stream",
        "string_decoder", "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads",
        "zlib",
    }
)

_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


@dataclass
class _State:
    """What the mocks seen so far allow."""

    #: How many leading arguments some stand-in can see.
    reach: int = 0
    #: ``(resolved path, start byte)`` of each factory key proven a stand-in.
    keys: set[tuple[str, int]] = field(default_factory=set)
    #: The text each file holding such a key was read with: a key is only
    #: recognized again in the same text.
    texts: dict[str, str] = field(default_factory=dict)
    #: Whether each file, by resolved path, is a test file.
    tests: dict[str, bool] = field(default_factory=dict)
    #: The checkout's exact ``paths`` aliases (None: unreadable), and the
    #: declaring module, for :func:`~flag_cleanup.ts_exported_props._plausible`.
    aliases: frozenset[str] | None = None
    reached: set[str] = field(default_factory=set)
    #: What each ``(directory, specifier)`` resolved to.
    resolved: dict[tuple[str, str], str | None] = field(default_factory=dict)

    #: The callee's name.
    name: str = ""
    #: The package names that resolve to the declaring file (see
    #: :func:`_package_names`).
    owned: frozenset[str] = frozenset()

    def loadable(self, repo_dir: str):
        """For :func:`~flag_cleanup.ts_exported_props._users`: whether a
        specifier resolving to no file names something a reader can load (a
        relative path, or a package it can reach). One that does not, such as
        a root-relative path, a bundler-only alias or a ``moduleNameMapper``
        key, may still name the declaring file to the tool that runs the
        test, so a module importing through it refuses."""
        return lambda reader, specifier: specifier.startswith(".") or _package(
            reader, specifier, repo_dir, self.owned
        )

    def see(self, count: int) -> None:
        self.reach = max(self.reach, count)

    def use_in(self, file: _File, node: Node) -> bool:
        """For :func:`~flag_cleanup.ts_exported_props._users`: whether a
        spelling of the callee is one of the mock shapes this proof follows."""
        return _use_in(file, node, self.name, self)


def reach(decl_path: str, name: str, repo_dir: str, cache: dict | None = None) -> int | None:
    """How many leading arguments a stand-in for ``name``, the function the
    module at ``decl_path`` exports, can see; ``None`` when the proof in the
    module docstring refuses. ``cache`` keeps the answer per callee."""
    key = (str(Path(decl_path).resolve()), name)
    if cache is not None and key in cache:
        return cache[key]
    try:
        # One listing of the checkout serves every scan below.
        with source_index.run_scope():
            found = _reach(decl_path, name, repo_dir)
    except (OSError, UnicodeDecodeError, RecursionError, ValueError):
        found = None
    if cache is not None:
        cache[key] = found
    return found


def _reach(decl_path: str, name: str, repo_dir: str) -> int | None:
    text = Path(decl_path).read_text(encoding="utf-8")
    root = _parse(text, ts_scope.grammar(decl_path))
    if root.has_error or not _is_module(root):
        return None
    data = text.encode("utf-8")
    declared = _exported_function(root, data, name)
    if declared is None:
        return None
    _function, allowed = declared
    decl = _File(decl_path, text, text, root, data)
    state = prepare(decl_path, name, repo_dir)
    if state is None:
        return None
    users = _users(
        name,
        decl,
        allowed,
        {},
        {},
        repo_dir,
        in_run=lambda _path: True,
        use=_called,
        solution=True,
        use_in=state.use_in,
        factories=True,
        loadable=state.loadable(repo_dir),
    )
    if users is None:
        return None
    return state.reach


def prepare(decl_path: str, name: str, repo_dir: str) -> _State | None:
    """The mocks' half of the proof: every mock in the checkout names its
    module plainly, and every mock of the module at ``decl_path`` is one
    followed. Returns what a caller hands
    :func:`~flag_cleanup.ts_exported_props._users` as ``use_in`` (with
    ``factories``), whose ``reach`` is final once that proof holds; ``None``
    to refuse."""
    try:
        with source_index.run_scope():
            return _prepare(decl_path, name, repo_dir)
    except (OSError, UnicodeDecodeError, RecursionError, ValueError):
        return None


def _prepare(decl_path: str, name: str, repo_dir: str) -> _State | None:
    from flag_cleanup import ts_mock_config

    if ts_mock_config.everything_mocked(repo_dir):
        return None
    decl_resolved = str(Path(decl_path).resolve())
    owned = _package_names(decl_resolved, repo_dir)
    listing = [
        listed
        for listed in source_index.files(repo_dir, with_ignored=True)
        if str(listed.path).endswith(_extensions())
    ]
    manual = any("__mocks__" in Path(listed.resolved).parts for listed in listing)
    state = _State(aliases=_exact_aliases(repo_dir), reached={decl_resolved}, name=name, owned=owned)
    for listed in listing:
        source = listed.path.read_text(encoding="utf-8")
        if _UNFOLLOWED.search(source):
            return None
        for match in _SPECIFIER.finditer(source) if any(own in source for own in owned) else ():
            specifier = match.group(2)
            if any(specifier == own or specifier.startswith(own + "/") for own in owned):
                # A workspace link resolves the package's own name to its files.
                return None
        if _MOCK_TEXT.search(source) and not _mocks_allowed(
            str(listed.path), listed.resolved, source, name, decl_resolved, repo_dir, owned, manual, state
        ):
            return None
    return state


# ---------------------------------------------------------------------------
# The mocks
# ---------------------------------------------------------------------------


def _mocks_allowed(path, resolved, source, name, decl_resolved, repo_dir, owned, manual, state) -> bool:
    """Whether every mock in ``path`` names its module plainly and every mock
    of the declaring module is one this proof follows, recording what each
    lets a stand-in see."""
    root = _parse(source, ts_scope.grammar(path))
    runners = _runner_names(root)
    for call in _walk(root):
        if call.type != "call_expression":
            continue
        callee = call.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            continue
        method = _property(callee)
        if method not in _MOCKS | _UNMOCKS | _OTHER_MOCKS:
            continue
        arguments = call.child_by_field_name("arguments")
        passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
        if not passed:
            continue
        target_object = callee.child_by_field_name("object")
        runner = (
            target_object is not None
            and target_object.type == "identifier"
            and target_object.text.decode("utf-8") in runners
        ) or (
            # Whatever it is called, `x.mock(spec, () => …)` installs a factory.
            method in _MOCKS
            and len(passed) >= 2
            and _inward(passed[1]).type in _FUNCTIONS
        )
        specifier = _mocked_specifier(passed[0])
        if specifier is None:
            if runner:
                return False
            # Another library's `mock(…)` (an HTTP mock's URL, say).
            continue
        plausible = _plausible(specifier, state.reached, state.aliases)
        if not plausible and (specifier.startswith((".", "/")) or not runner or _installed(path, specifier, repo_dir, owned)):
            # A specifier whose last segment is not the module's, and no
            # exact alias, cannot name it: a path, whether or not it names a
            # file, or a package the file can load.
            continue
        key = (str(Path(path).parent), specifier)
        if key not in state.resolved:
            state.resolved[key] = cross_file_ts.resolve_specifier(path, specifier, repo_dir, solution=True)
        target = state.resolved[key]
        if target is None:
            if not runner:
                continue
            if not _package(path, specifier, repo_dir, owned):
                return False
            continue
        if str(Path(target).resolve()) != decl_resolved:
            continue
        if method not in _MOCKS | _UNMOCKS or not _is_test(path, resolved, source, state):
            return False
        if method in _UNMOCKS:
            continue
        if len(passed) == 1:
            if manual:
                # A `__mocks__` file may supply the module instead.
                return False
            continue
        if len(passed) != 2 or not _factory(passed[1], name, specifier, resolved, state):
            return False
        state.texts[resolved] = source
    return True


def _mocked_specifier(argument: Node) -> str | None:
    """The specifier ``'./m'`` or ``import('./m')`` names, or None."""
    if argument.type == "call_expression":
        callee = argument.child_by_field_name("function")
        arguments = argument.child_by_field_name("arguments")
        if callee is None or callee.type != "import" or arguments is None:
            return None
        passed = [c for c in arguments.named_children if c.type != "comment"]
        if len(passed) != 1:
            return None
        argument = passed[0]
    return _specifier(argument)


def _factory(function: Node, name: str, specifier: str, resolved: str, state: _State) -> bool:
    """Whether the mock factory ``function`` returns an object whose stand-in
    for ``name``, if it has one, is followed, recording what it can see."""
    if function.type not in _FUNCTIONS:
        return False
    for node in _walk(function):
        if node.type in ("this", "super"):
            return False
        if node.type == "identifier" and node.text in (b"arguments", b"eval"):
            return False
    parameters = _parameters(function)
    if parameters is None or len(parameters) > 1:
        return False
    loader = None
    if parameters:
        loader = _parameter_name(parameters[0])
        if loader is None:
            return False
    loads: list[Node] = []

    def original(node: Node) -> bool:
        """Whether ``node`` loads the module's own original."""
        node = _inward(node)
        if node.type == "await_expression":
            inner = [c for c in node.named_children if c.type != "comment"]
            if len(inner) != 1:
                return False
            node = _inward(inner[0])
        if node.type != "call_expression":
            return False
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
        if loader is not None and callee is not None and callee.type == "identifier" and callee.text == loader:
            if passed:
                return False
            loads.append(callee)
            return True
        return original_call(node) == specifier

    body = function.child_by_field_name("body")
    bound: dict[bytes, Node] = {}
    if body is not None and body.type == "statement_block":
        statements = [c for c in body.named_children if c.type != "comment"]
        if not statements or statements[-1].type != "return_statement":
            return False
        for statement in statements[:-1]:
            declarators = [c for c in statement.named_children if c.type == "variable_declarator"]
            if (
                statement.type != "lexical_declaration"
                or not statement.text.startswith(b"const")
                or len(declarators) != 1
            ):
                return False
            target = declarators[0].child_by_field_name("name")
            value = declarators[0].child_by_field_name("value")
            if target is None or target.type != "identifier" or value is None or not original(value):
                return False
            bound[target.text] = target
        returned = [c for c in statements[-1].named_children if c.type != "comment"]
        if len(returned) != 1:
            return False
        obj = _inward(returned[0])
    elif body is not None:
        obj = _inward(body)
    else:
        return False
    if obj.type != "object":
        return False
    spreads = 0
    for member in obj.named_children:
        if member.type == "comment":
            continue
        if member.type == "spread_element":
            spreads += 1
            argument = _inward(member.named_children[0]) if member.named_children else None
            if argument is None:
                return False
            if not (argument.type == "identifier" and argument.text in bound) and not original(argument):
                return False
        elif member.type == "pair":
            key = member.child_by_field_name("key")
            if key is None or key.type == "computed_property_name":
                return False
            if _key_text(key) != name:
                continue
            if key.type != "property_identifier":
                return False
            count = _stand_in(member.child_by_field_name("value"))
            if count is None:
                return False
            state.see(count)
            state.keys.add((resolved, key.start_byte))
        elif member.type == "method_definition":
            key = member.child_by_field_name("name")
            if key is None or key.type == "computed_property_name" or _key_text(key) == name:
                return False
        elif member.type == "shorthand_property_identifier":
            if member.text.decode("utf-8") == name:
                return False
        else:
            return False
    if spreads > 1:
        return False
    # Each name bound to the original, and the loader, is read only where
    # it was just checked.
    for text, target in bound.items():
        reads = [
            n
            for n in _walk(function)
            if n.type in ("identifier", "shorthand_property_identifier") and n.text == text and n != target
        ]
        if len(reads) != 1 or reads[0].type != "identifier" or _outward(reads[0]).parent.type != "spread_element":
            return False
    if loader is not None:
        reads = [
            n
            for n in _walk(function)
            if n.type in ("identifier", "shorthand_property_identifier") and n.text == loader
        ]
        if len(reads) != 1 + len(loads) or any(n.type != "identifier" for n in reads):
            return False
    return True


def _stand_in(value: Node | None) -> int | None:
    """How many leading arguments the stand-in ``value`` can see: a
    ``vi.fn()``/``jest.fn()``, either given an inline implementation, or an
    inline function; None for anything else."""
    if value is None:
        return None
    value = _uncast(value)
    if value.type in _FUNCTIONS:
        return _arity(value)
    if value.type != "call_expression" or not _runner_call(value, "fn"):
        return None
    arguments = value.child_by_field_name("arguments")
    passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
    if not passed:
        return 0
    return _arity(passed[0]) if len(passed) == 1 else None


def _arity(function: Node) -> int | None:
    """How many arguments the inline ``function`` can read, or None for
    every one (a rest parameter, ``arguments``, ``eval``) or a function that
    is not inline. A ``function`` in a sloppy-mode file can read them all
    through ``self.arguments`` or a caller's ``.caller.arguments``, so only an
    arrow function is followed there."""
    function = _inward(function)
    if function.type not in _FUNCTIONS:
        return None
    if function.type != "arrow_function" and _sloppy(function):
        return None
    if any(n.type == "identifier" and n.text in (b"arguments", b"eval") for n in _walk(function)):
        return None
    parameters = _parameters(function)
    if parameters is None:
        return None
    for parameter in parameters:
        pattern = parameter.child_by_field_name("pattern") if parameter.type != "identifier" else parameter
        if pattern is None or pattern.type == "rest_pattern":
            return None
    return len(parameters)


def _sloppy(node: Node) -> bool:
    """Whether ``node`` is in a file that runs in sloppy mode: no ``import`` or
    ``export``, no ``import.meta``, and no ``"use strict"`` directive in the
    file or in a function around ``node``."""
    root = node
    while True:
        if root.type in _FUNCTIONS and _strict_directive(root.child_by_field_name("body")):
            return False
        if root.parent is None:
            break
        root = root.parent
    if _strict_directive(root):
        return False
    for child in root.named_children:
        if child.type in ("import_statement", "export_statement"):
            return False
    return not any(n.type == "meta_property" for n in _walk(root))


def _strict_directive(body: Node | None) -> bool:
    """Whether ``body`` (a program or a function's block) opens with ``"use strict"``."""
    if body is None or body.type not in ("program", "statement_block"):
        return False
    for child in body.named_children:
        if child.type == "comment":
            continue
        if child.type != "expression_statement":
            return False
        inner = child.named_children[0] if child.named_children else None
        if inner is None or inner.type != "string":
            return False
        if inner.text[1:-1] == b"use strict":
            return True
    return False


# ---------------------------------------------------------------------------
# The spellings
# ---------------------------------------------------------------------------


def _called(node: Node) -> bool:
    """Whether ``node`` is the identifier a plain call is made through."""
    parent = node.parent
    return (
        node.type == "identifier"
        and parent is not None
        and parent.type == "call_expression"
        and parent.child_by_field_name("function") == node
    )


def _use_in(file: _File, node: Node, name: str, state: _State) -> bool:
    """Whether the spelling ``node`` of ``name`` in ``file`` is a factory key
    proven a stand-in, a receiver that only sets what the stand-in does, or
    the value of a ``const`` alias used only as such a receiver."""
    resolved = str(Path(file.path).resolve())
    if not _is_test(file.path, resolved, file.current, state):
        return False
    if node.type == "property_identifier":
        return state.texts.get(resolved) == file.current and (resolved, node.start_byte) in state.keys
    if node.type != "identifier":
        return False
    count = _receiver(node)
    if count is not None:
        state.see(count)
        return True
    alias = _alias(node)
    if alias is None:
        return False
    target, statement = alias
    if statement.parent is not None and statement.parent.type == "export_statement":
        return False
    if sum(1 for n in binding_nodes(file.root) if n.text == target.text) != 1:
        return False
    seen = 0
    for other in _walk(file.root):
        if other == target or other.text != target.text:
            continue
        if other.type == "shorthand_property_identifier":
            return False
        if other.type != "identifier":
            continue
        count = _receiver(other)
        if count is None:
            return False
        seen = max(seen, count)
    state.see(seen)
    return True


def _receiver(node: Node) -> int | None:
    """How many arguments the statement ``node`` is the receiver of lets a
    stand-in see, when it is ``node.<setter>(…)``, ``(node as T).<setter>(…)``
    or ``vi.mocked(node).<setter>(…)`` on its own; None for anything else."""
    expression = _outward_casts(node)
    holder = expression.parent
    if (
        holder is not None
        and holder.type == "arguments"
        and holder.parent is not None
        and _runner_call(holder.parent, "mocked")
    ):
        passed = [c for c in holder.named_children if c.type != "comment"]
        if not passed or passed[0] != expression or len(passed) > 2:
            return None
        expression = _outward_casts(holder.parent)
    member = expression.parent
    if member is None or member.type != "member_expression" or member.child_by_field_name("object") != expression:
        return None
    method = _property(member)
    call = member.parent
    if (
        method not in _SETTERS | _IMPLEMENTERS
        or call is None
        or call.type != "call_expression"
        or call.child_by_field_name("function") != member
        or not _discarded(call)
    ):
        return None
    if method in _SETTERS:
        return 0
    arguments = call.child_by_field_name("arguments")
    passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
    return _arity(passed[0]) if len(passed) == 1 else None


#: Test hooks. A function their callback returns is called with no arguments, as
#: a teardown, so a stand-in returned from one sees none of the call's arguments.
_HOOKS = frozenset({"beforeEach", "afterEach", "beforeAll", "afterAll"})


def _discarded(call: Node) -> bool:
    """Whether nothing keeps what ``call`` returns: it is a statement of its
    own, the body of an arrow handed to a test hook (which calls a returned
    stand-in with no arguments, as a teardown), or the body of an arrow
    helper bound by a ``const`` that is only ever called as a statement."""
    holder = call.parent
    if holder is None:
        return False
    if holder.type == "expression_statement":
        return True
    if holder.type != "arrow_function" or holder.child_by_field_name("body") != call:
        return False
    outer = holder.parent
    if outer is not None and outer.type == "arguments" and outer.parent is not None:
        hook = outer.parent.child_by_field_name("function")
        passed = [c for c in outer.named_children if c.type != "comment"]
        return (
            hook is not None
            and hook.type == "identifier"
            and hook.text.decode("utf-8") in _HOOKS
            and passed == [holder]
        )
    if outer is None or outer.type != "variable_declarator" or outer.child_by_field_name("value") != holder:
        return False
    target = outer.child_by_field_name("name")
    statement = outer.parent
    if (
        target is None
        or target.type != "identifier"
        or statement is None
        or statement.type != "lexical_declaration"
        or not statement.text.startswith(b"const")
        or statement.parent is None
        or statement.parent.type == "export_statement"
    ):
        return False
    root = statement
    while root.parent is not None:
        root = root.parent
    if sum(1 for n in binding_nodes(root) if n.text == target.text) != 1:
        return False
    for other in _walk(root):
        if other == target or other.text != target.text:
            continue
        if other.type == "shorthand_property_identifier":
            return False
        if other.type != "identifier":
            continue
        use = other.parent
        if (
            use is None
            or use.type != "call_expression"
            or use.child_by_field_name("function") != other
            or use.parent is None
            or use.parent.type != "expression_statement"
        ):
            return False
    return True


def _alias(node: Node) -> tuple[Node, Node] | None:
    """``(name, statement)`` of the ``const`` whose value is ``node``, through
    casts or ``vi.mocked(…)``."""
    expression = _outward_casts(node)
    holder = expression.parent
    if (
        holder is not None
        and holder.type == "arguments"
        and holder.parent is not None
        and _runner_call(holder.parent, "mocked")
    ):
        passed = [c for c in holder.named_children if c.type != "comment"]
        if not passed or passed[0] != expression or len(passed) > 2:
            return None
        expression = _outward_casts(holder.parent)
    declarator = expression.parent
    if (
        declarator is None
        or declarator.type != "variable_declarator"
        or declarator.child_by_field_name("value") != expression
    ):
        return None
    target = declarator.child_by_field_name("name")
    statement = declarator.parent
    if (
        target is None
        or target.type != "identifier"
        or statement is None
        or statement.type != "lexical_declaration"
        or not statement.text.startswith(b"const")
    ):
        return None
    return target, statement


# ---------------------------------------------------------------------------
# Files and packages
# ---------------------------------------------------------------------------


def _is_test(path: str, resolved: str, source: str, state: _State) -> bool:
    """Whether a test runner runs ``path`` as a test file."""
    if resolved not in state.tests:
        from flag_cleanup import ts_tests, wrapper_ts

        state.tests[resolved] = wrapper_ts.is_test_path(path) or ts_tests.is_test_file(
            source, ts_scope.grammar(path)
        )
    return state.tests[resolved]


def _package_names(decl_resolved: str, repo_dir: str) -> frozenset[str]:
    """The ``name`` of each ``package.json`` from the declaring file's
    directory up to the checkout root, by which a workspace link or a
    self-reference can resolve to it. A workspace root that declares no
    ``exports`` is neither linked nor self-referenced, so it is left out."""
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    names: set[str] = set()
    directory = Path(decl_resolved).parent
    while True:
        manifest = directory / "package.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and isinstance(data.get("name"), str)
                and data["name"]
                and not ("workspaces" in data and "exports" not in data)
            ):
                names.add(data["name"])
        if directory == stop or directory.parent == directory or not directory.is_relative_to(stop):
            break
        directory = directory.parent
    return frozenset(names)


def _package(path: str, specifier: str, repo_dir: str, owned: frozenset[str]) -> bool:
    """Whether ``specifier``, which resolves to no file, is a package the
    file at ``path`` can load: a Node built-in, or a dependency declared or
    installed above it, never the declaring file's own."""
    if specifier.startswith("node:"):
        return True
    if not specifier or specifier.startswith((".", "/", "#")):
        return False
    if not cross_file_ts.names_package(path, specifier, repo_dir):
        return False
    return _installed(path, specifier, repo_dir, owned)


def _installed(path: str, specifier: str, repo_dir: str, owned: frozenset[str]) -> bool:
    """Whether bare ``specifier`` names a Node built-in, or a package
    declared in a ``package.json`` above ``path`` or installed in a
    ``node_modules`` above it, other than the declaring file's own."""
    if specifier.startswith("node:"):
        return True
    if not specifier or specifier.startswith((".", "/", "#")):
        return False
    parts = specifier.split("/")
    package = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
    if package in owned:
        return False
    if specifier in _BUILTINS or package in _BUILTINS:
        return True
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    directory = Path(path).resolve().parent
    while True:
        if (directory / "node_modules" / package).exists():
            return True
        manifest = directory / "package.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                return False
            if isinstance(data, dict) and any(
                isinstance(data.get(f), dict) and package in data[f] for f in _DEPENDENCY_FIELDS
            ):
                return True
        if directory == stop or directory.parent == directory or not directory.is_relative_to(stop):
            return False
        directory = directory.parent


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


#: The modules that export the runner object, and the name each exports.
_RUNNER_MODULES = {"vitest": "vi", "@jest/globals": "jest"}


def _runner_names(root: Node) -> frozenset[str]:
    """The names ``vi`` and ``jest`` go by in the file: those two, and each
    local name an import from ``vitest`` or ``@jest/globals`` binds to them."""
    names = {"vi", "jest"}
    for statement in root.named_children:
        if statement.type != "import_statement":
            continue
        module = _specifier(statement.child_by_field_name("source"))
        exported = _RUNNER_MODULES.get(module or "")
        if exported is None:
            continue
        for node in _walk(statement):
            if node.type != "import_specifier":
                continue
            name = node.child_by_field_name("name")
            alias = node.child_by_field_name("alias")
            if name is not None and alias is not None and name.text.decode("utf-8") == exported:
                names.add(alias.text.decode("utf-8"))
    return frozenset(names)


def _runner_call(call: Node, method: str) -> bool:
    """Whether ``call`` is ``vi.<method>(…)`` or ``jest.<method>(…)``, under
    whatever local name the file imports the runner as."""
    callee = call.child_by_field_name("function")
    if callee is None or callee.type != "member_expression":
        return False
    target = callee.child_by_field_name("object")
    if target is None or target.type != "identifier" or _property(callee) != method:
        return False
    root = call
    while root.parent is not None:
        root = root.parent
    return target.text.decode("utf-8") in _runner_names(root)


def _property(member: Node) -> str | None:
    node = member.child_by_field_name("property")
    return node.text.decode("utf-8") if node is not None and node.type == "property_identifier" else None


def _key_text(key: Node) -> str | None:
    if key.type in ("property_identifier", "number"):
        return key.text.decode("utf-8")
    if key.type == "string":
        return cross_file_ts._string_value(key)
    return None


def _parameters(function: Node) -> list[Node] | None:
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        parameter = function.child_by_field_name("parameter")
        return [] if parameter is None else [parameter]
    return [c for c in parameters.named_children if c.type != "comment"]


def _parameter_name(parameter: Node) -> bytes | None:
    """The name a plain parameter binds, with no default."""
    if parameter.type == "identifier":
        return parameter.text
    if parameter.type != "required_parameter" or parameter.child_by_field_name("value") is not None:
        return None
    pattern = parameter.child_by_field_name("pattern")
    return pattern.text if pattern is not None and pattern.type == "identifier" else None


def _inward(node: Node) -> Node:
    """``node`` without the parentheses and casts around its value."""
    while node.type in _CASTS:
        inner = [c for c in node.named_children if c.type != "comment"]
        if not inner:
            break
        node = inner[0]
    return node


def _uncast(node: Node) -> Node:
    return _inward(node)


def _outward_casts(node: Node) -> Node:
    """``node`` with the parentheses and casts around it."""
    while node.parent is not None and node.parent.type in _CASTS:
        first = [c for c in node.parent.named_children if c.type != "comment"]
        if not first or first[0] != node:
            break
        node = node.parent
    return node
