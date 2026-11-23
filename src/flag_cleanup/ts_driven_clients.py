"""A test that drives the in-repo client, or the key, a TS/JS read goes through.

The fold replaces a read with the flag's value, whatever the client would have
answered. A test that chooses that answer itself, through the client's own
module, then asserts what the read used to return and goes red:

* ``vi.spyOn(client, "isEnabled").mockReturnValue(false)``, ``Object.assign(client, …)``;
* ``holder.c = fake`` under a getter that returns ``holder.c``;
* ``client.override("key", true)``, ``setClient(fake)``, ``const F = Flags; F.impl = fake``;
* ``vi.mock("./client", () => ({ client: fake }))``, with or without a factory.

So the rule is by MODULE, not by shape. For each read of the flag (production
code and keyless wrappers alike), every in-repo module the receiver chain
touches is on its route: the module declaring the receiver, each barrel it is
imported through, and every module a getter, a returned function or an
initialiser on the way reads (:func:`wrapper_ts.module_receiver`, recording).
Any module re-exporting one of those joins them. Then, in every test, support
or setup file (``wrapper_ts._stubs``, a runner's setup files, a module only
test code loads):

* a module mock of a route module refuses the flag (the mock API is resolved
  by what it is, through ``import { vi as v }``, ``const { mock } = vi`` and
  ``vitest.mock``, and a runner config that maps a route module to another
  file, or aliases a specifier to one, counts too);
* a load of one (``import``, ``require``, ``import()``, ``requireActual``)
  binds names, and ANY use of them other than calling a read on them
  (``client.isEnabled(...)``), calling a declaration that holds the read
  (``handler()``), or ``typeof`` refuses it: a write, an alias, a cast, a
  spread, an argument, a spy, a call of any other function the module exports.

The module declaring the flag's KEY constant (and every barrel forwarding it)
is on the route too: a module mock of it, a spy on it, a write through it or
the module object handed on refuses the flag. Reading a constant from it does
not, whatever the test does with the string.

Two more routes have no module to load:

* a global the route reads (an ``env`` member of the process object behind a
  ternary, ``globalThis.__flags``): a test or setup file writing it (an
  assignment, ``delete``, ``Object.assign``, a spy, ``vi.stubEnv``/``vi.stubGlobal``);
* an npm package the route ends in, which a SETUP file drives for every test
  (or a module only test code loads, reached from one): a module mock of it or
  any use but a read. A single test file's use of a package is the keyless
  wrapper path's (``wrapper_ts._package_driven``), as before.

Over-approximate by design: a test that imports a helper from the client's
module refuses the flag even when the helper cannot touch the client.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, cross_file_ts, mock_stubs, rule_synthesis, ts_key_mocks, ts_mock_config, ts_scope
from flag_cleanup import wrapper_ts as wt
from flag_cleanup.ts_scope import text as _text

#: Reads the base rules fold whatever ``accessors`` says.
_BUILT_IN_READS = frozenset({*wt._READ_CALLEES, "getBooleanValue"})

_FUNCTIONS = frozenset(
    {
        "function_declaration", "arrow_function", "function_expression", "function", "method_definition",
        "generator_function_declaration", "generator_function",
    }
)

#: Spelled calls that load a module and hand back its exports.
_LOADERS = frozenset({"require", "jest.requireActual", "vi.importActual", "jest.requireMock", "vi.importMock"})

#: What the test runners' mock objects (``vi``, ``jest``, ``vitest``, Node's
#: and Bun's ``mock``) do, by method name.
_MODULE_MOCKS = frozenset(
    {"mock", "doMock", "unstable_mockModule", "setMock", "createMockFromModule", "module", "enableAutomock"}
)
_MOCK_LOADERS = frozenset({"requireActual", "importActual", "requireMock", "importMock"})
_SPIES = frozenset({"spyOn", "replaceProperty", "stubGlobal", "stubEnv", "method", "getter", "setter"})
#: Where the mock objects come from.
_RUNNER_MODULES = {
    "vitest": frozenset({"vi", "vitest"}),
    "@jest/globals": frozenset({"jest"}),
    "node:test": frozenset({"mock"}),
    "bun:test": frozenset({"mock", "jest", "vi"}),
}
_RUNNER_GLOBALS = frozenset({"vi", "jest", "vitest"})
#: Spelled calls that write members of the object handed to them.
_WRITERS = wt._MEMBER_STUBS - {"vi.mocked", "jest.mocked"}


def _read(path: str) -> str | None:
    """A regular file's text; ``None`` for anything else (a FIFO would block)."""
    try:
        if not os.path.isfile(path):
            return None
    except OSError:
        return None
    return wt._read(path)


#: Files outside the script listing whose text the pass's answers read: the
#: runner configs, every ``package.json``, compiler configs (``paths``,
#: ``baseUrl``, ``references``), Babel configs, lockfiles, and the patches and
#: config-named JSON files :func:`cross_file_ts.package_outside` scans.
_CONFIG_LISTING = (*wt._CONFIG_SUFFIXES, ".yml", ".yaml", ".jsonc", ".toml", ".patch", ".lock", ".lockb", "")
#: Run memos of other passes that are derived from those files: emptied when
#: one of them changed, since they are kept for the run on the promise that
#: the run writes none.
_CONFIG_MEMOS = ("ts_manifests", "ts_aliases", "ts_workspace", "mock_config")

#: ``(size, mtime_ns, inode)`` of a regular file, ``None`` for anything else.
_Stamp = tuple[int, int, int] | None


def _stamp(path: str) -> _Stamp:
    import stat as stat_mode

    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat_mode.S_ISREG(info.st_mode):
        return None  # a FIFO would block
    return (info.st_size, info.st_mtime_ns, info.st_ino)


class _Checkout:
    """What the pass reads of one checkout that does not depend on the flag,
    kept for a whole run (``source_index.run_memo``) so only the route walk
    and the judging are repeated per flag: parsed files, file texts, the
    loaders of each module, what each file loads, specifier resolution,
    :func:`cross_file_ts.package_outside`'s answers, the runner configs, their
    aliases, the setup files and whether test files are isolated.

    All of it is derived from the text of the WATCHED files (every script of
    the listing, every config file, every file the parsed set holds), so
    :meth:`refresh` stats them on every call: when a stamp differs it re-reads
    that file, and when any text differs, or a file came or went, every
    derived answer is dropped and rebuilt. Only parse trees survive, keyed by
    text. A file whose stamp moved and whose text did not (the run's undo
    writes the same bytes back) changes nothing. Outside a run (a single
    transform, a test) each call builds its own."""

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir
        #: Parse trees by ``(path, text)``: the one thing kept across a rebuild.
        self.trees: dict[tuple[str, str], Node] = {}
        #: Every watched file's stamp and text when the derived state was built.
        self.stamps: dict[str, _Stamp] = {}
        self.watched: dict[str, str | None] = {}
        self.listed: frozenset[str] = frozenset()
        self.built = False
        self._drop()

    def _drop(self) -> None:
        """Forget every derived answer."""
        self.files = ts_scope.Files(self.repo_dir, self.trees)
        #: Script texts of the listing, by resolved path.
        self.texts: dict[str, str] = {}
        self.listing: list[str] = []
        self.loaders: dict[str, set[str]] = {}
        self.loads: dict[str, tuple[str, tuple[str | None, ...]]] = {}
        self.reexports: dict[str, tuple[str, frozenset[str]]] = {}
        self.resolved: dict[tuple[str, str], str | None] = {}
        #: :func:`cross_file_ts.package_outside`'s answers (see :func:`refusals`).
        self.packages: dict = {}
        self.configs: list[Path] | None = None
        self.inputs: list[str] = []
        self.aliases: dict[str, tuple[list["_Alias"], bool] | None] = {}
        self.setups: frozenset[str] | None | bool = False  # False: not read yet
        self.isolated: bool | None = None

    def configs_list(self) -> list[Path]:
        """Every ``package.json`` and runner config of the checkout (listed once per build)."""
        if self.configs is None:
            self._list_configs()
        assert self.configs is not None
        return self.configs

    def _list_configs(self) -> None:
        from flag_cleanup.piranha_runner import _SKIP_DIRS, checkout_root, source_files

        configs: list[Path] = []
        inputs: list[str] = []
        for p in source_files(Path(checkout_root(self.repo_dir)), _CONFIG_LISTING, _SKIP_DIRS):
            name = p.name
            lowered = name.lower()
            if (
                name in ("package.json", "bunfig.toml") or wt._CONFIG_NAME.match(name)
                or ts_mock_config._CONFIG_NAME.match(name) or ts_mock_config._VITE_NAME.match(name)
                or wt._MOCHARC.match(name)
            ) and p.suffix in (*wt._CONFIG_SUFFIXES, ".yml", ".yaml", ".jsonc", ".toml", ""):
                configs.append(p)
                inputs.append(str(p))
            elif (
                cross_file_ts._ANY_CONFIG.match(name) or name in wt._LOCKFILES
                or lowered.startswith((".babelrc", "babel.config")) or p.suffix == ".patch"
                or (p.suffix in (".json", "") and ("config" in lowered or "workspace" in lowered))
            ):
                inputs.append(str(p))
        self.configs = sorted(configs)
        self.inputs = sorted(set(inputs))

    def refresh(self) -> None:
        """Make the derived state the one the files on disk give (see the class)."""
        from flag_cleanup import source_index

        listing = [src.resolved for src in source_index.files(self.repo_dir, frozenset(_LOADER_SUFFIXES))]
        if self.built and not self._changed(listing):
            self._per_flag()
            return
        was_built, old_inputs, old_watched = self.built, self.inputs, self.watched
        self._drop()
        self._list_configs()
        stamps: dict[str, _Stamp] = {}
        watched: dict[str, str | None] = {}
        for path in dict.fromkeys([*listing, *self.inputs]):
            stamp = stamps[path] = _stamp(path)
            text = wt._read(path) if stamp is not None else None
            watched[path] = text
            if text is not None:
                self.texts[path] = text
        self.texts = {p: self.texts[p] for p in listing if p in self.texts}
        self.listing = list(self.texts)
        # Trees whose text is no file's now are never asked for again.
        self.trees = {k: v for k, v in self.trees.items() if watched.get(k[0]) == k[1]}
        self.files = ts_scope.Files(self.repo_dir, self.trees)
        self.stamps, self.watched, self.listed, self.built = stamps, watched, frozenset(listing), True
        if was_built and (
            set(self.inputs) != set(old_inputs) or any(watched[p] != old_watched.get(p) for p in self.inputs)
        ):
            self._forget_config_memos()
        self._per_flag()

    def _changed(self, listing: list[str]) -> bool:
        """Whether a file came or went, or any watched file's text differs
        from the build's. A moved stamp over the same text is the new stamp."""
        if frozenset(listing) != self.listed:
            return True
        for path, kept in self.stamps.items():
            stamp = _stamp(path)
            if stamp == kept:
                continue
            text = wt._read(path) if stamp is not None else None
            if text != self.watched.get(path):
                return True
            self.stamps[path] = stamp
        # A file the walk parsed from outside the listing (an ignored module
        # a route reaches) is watched by the text it was parsed from.
        for key, file in list(self.files._cache.items()):
            if key not in self.stamps and wt._read(key) != file.source:
                return True
        return False

    def _forget_config_memos(self) -> None:
        from flag_cleanup import source_index
        from flag_cleanup.piranha_runner import checkout_root

        for name in _CONFIG_MEMOS:
            memo = source_index.run_memo(name)
            if memo is not None:
                memo.clear()
        entries = source_index.transform_memo("ts_workspace_entries")
        if entries is not None:
            entries.clear()
        wt._INSTALLABLE.pop(str(Path(checkout_root(self.repo_dir)).resolve()), None)

    def _per_flag(self) -> None:
        """Forget what the walk keeps per flag on the parsed files."""
        wt.recording(self.files, None)
        for name in ("_test_only", "_members_written", "_package_driven", "_test_support", "_loader_index"):
            self.files.__dict__.pop(name, None)


#: The checkouts this process is asking about right now, by ``repo_dir``.
_CURRENT: dict[str, _Checkout] = {}


def _checkout(repo_dir: str) -> _Checkout:
    """The run's :class:`_Checkout` for ``repo_dir`` (a new one outside a run)."""
    from flag_cleanup import source_index

    memo = source_index.run_memo("ts_driven_checkout")
    found = memo.get(repo_dir) if memo is not None else None
    if found is None:
        found = _Checkout(repo_dir)
        if memo is not None:
            memo[repo_dir] = found
    _CURRENT[repo_dir] = found
    return found


def reset_cache() -> None:
    """Forget every checkout (tests that rewrite a tree inside one run)."""
    from flag_cleanup import source_index

    memo = source_index.run_memo("ts_driven_checkout")
    if memo is not None:
        memo.clear()
    _CURRENT.clear()


def _state(repo_dir: str) -> _Checkout:
    """The checkout being asked about (made on demand, for a direct caller)."""
    found = _CURRENT.get(repo_dir)
    if found is None:
        found = _CURRENT[repo_dir] = _Checkout(repo_dir)
    return found


def _files(repo_dir: str) -> ts_scope.Files:
    """The parsed files of :func:`_state` for ``repo_dir``."""
    return _state(repo_dir).files


@dataclass
class _Site:
    path: str
    line: int


@dataclass
class Plan:
    """The route of every read of the flag, and where one was read."""

    route: wt.Route = field(default_factory=wt.Route)
    #: Unresolved in-repo specifiers on the route, by last path segment.
    #: In-repo specifiers on the route no resolver reads, and the kind of the
    #: route they are on (``client`` or ``hook``).
    stems: dict[str, str] = field(default_factory=dict)
    site: _Site | None = None
    #: ``(module, name)`` of each top-level declaration holding a read: calling
    #: it runs the read, and cannot choose what the client answers.
    readers: set[tuple[str, str]] = field(default_factory=set)
    #: The files holding a read: production code under test, not test support.
    read_files: set[str] = field(default_factory=set)
    #: Route modules that only re-export.
    barrels: set[str] = field(default_factory=set)
    #: Modules a read reaches only through a React hook (``useFlag(...)``,
    #: ``useFlags().isEnabled(...)``): the provider a test renders chooses
    #: what the hook returns, and the key it spells is the pin passes'. A mock
    #: of one, a spy on it or a write through it still refuses.
    hooks: set[str] = field(default_factory=set)
    #: Modules declaring a key constant of the flag, and barrels forwarding one,
    #: with the names each declares.
    keys: dict[str, set[str]] = field(default_factory=dict)


def _top_declaration(node: Node) -> str | None:
    """The name of the top-level declaration holding ``node``: a function,
    a class, or a one-declarator ``const``/``let``."""
    current = node
    while current.parent is not None and current.parent.type != "program":
        current = current.parent
    if current.type == "export_statement":
        current = next((c for c in current.named_children if c.type not in ("comment", "decorator")), current)
    if current.type in (
        "function_declaration", "class_declaration", "generator_function_declaration", "abstract_class_declaration"
    ):
        name = current.child_by_field_name("name")
        return _text(name) if name is not None else None
    if current.type in ("lexical_declaration", "variable_declaration"):
        declarators = [c for c in current.named_children if c.type == "variable_declarator"]
        name = declarators[0].child_by_field_name("name") if len(declarators) == 1 else None
        return _text(name) if name is not None and name.type == "identifier" else None
    return None


def _key_names(path: str, file: ts_scope.File, flag_key: str, resolution: cross_file_keys.Resolution) -> set[str]:
    """The names a read's key argument may spell to mean ``flag_key``: a
    constant the cross-file resolution proved here, or one this file binds
    with ``const`` to the key's literal."""
    names = {r.member for r in resolution.readers.get(str(Path(path).resolve()), ())}
    for node in cross_file_keys.walk(file.root):
        if node.type != "variable_declarator":
            continue
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if name is not None and name.type == "identifier" and value is not None:
            if ts_mock_config.plain_string(value) == flag_key:
                names.add(_text(name))
    return names


def _names_key(node: Node | None, flag_key: str, names: set[str]) -> bool:
    if node is None:
        return False
    if ts_mock_config.plain_string(node) == flag_key:
        return True
    if node.type == "identifier":
        return _text(node) in names
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return prop is not None and _text(prop) in names
    return False


def _key_argument(call: Node, position: int | str) -> Node | None:
    arguments = mock_stubs.ts_arguments(call)
    if isinstance(position, int):
        return arguments[position] if position < len(arguments) else None
    first = arguments[0] if arguments else None
    if first is None or first.type != "object":
        return None
    for pair in first.named_children:
        key = pair.child_by_field_name("key") if pair.type == "pair" else None
        if key is not None and _text(key) == position:
            return pair.child_by_field_name("value")
    return None


def _scope(call: Node) -> tuple[set[str], dict[str, Node]]:
    """The parameters and body ``const``/``let`` values of every function
    around ``call``: names a caller (or the body) chooses, not the module."""
    params: set[str] = set()
    locals_: dict[str, Node] = {}
    node = call.parent
    while node is not None:
        if node.type in _FUNCTIONS:
            params |= wt._parameter_names(node)
            body = node.child_by_field_name("body")
            for d in cross_file_keys.walk(body) if body is not None else ():
                if d.type != "variable_declarator":
                    continue
                name, value = d.child_by_field_name("name"), d.child_by_field_name("value")
                if name is not None and name.type == "identifier" and value is not None:
                    locals_.setdefault(_text(name), value)
        node = node.parent
    return params, locals_


def plan(
    repo_dir: str,
    paths: list[str],
    flag_key: str,
    accessors: tuple[str, ...],
    resolution: cross_file_keys.Resolution,
) -> Plan:
    """Walk the receiver of every read of ``flag_key`` in ``paths``."""
    positions: dict[str, int | str] = {
        rule_synthesis.plain_name(a): 0 for a in accessors
    } | rule_synthesis.positions_of(accessors)
    for name in _BUILT_IN_READS:
        positions.setdefault(name, 0)
    plain = tuple(sorted(positions))
    state = _state(repo_dir)
    if not state.built:
        state.refresh()
    files = state.files
    found = Plan()
    wt.recording(files, found.route)
    try:
        for path in dict.fromkeys(paths):
            if not path.endswith(ts_scope.FAMILY_EXTENSIONS):
                continue
            file = files.file(path, state.texts.get(str(Path(path).resolve())))
            if file is None or not any(n in file.source for n in plain):
                continue
            names = _key_names(path, file, flag_key, resolution)
            for node in cross_file_keys.walk(file.root):
                if node.type != "call_expression":
                    continue
                callee = node.child_by_field_name("function")
                if callee is None:
                    continue
                last = callee.child_by_field_name("property") if callee.type == "member_expression" else callee
                if last is None or _text(last) not in positions:
                    continue
                if not _names_key(_key_argument(node, positions[_text(last)]), flag_key, names):
                    continue
                params, locals_ = _scope(node)
                route = wt.Route()
                wt.recording(files, route)
                wt.module_receiver(callee, file, params, locals_, files, plain, flag_key)
                _walk_unresolved(route, files, plain, flag_key)
                modules = {m for m in route.modules if not m.startswith("?")}
                for marker in (m for m in route.modules if m.startswith("?")):
                    _reader, spec, name = marker[1:].split("\0")
                    if name and name != "=":
                        found.route.names.add(name)  # what a factory mock of it must leave alone
                    if found.stems.get(spec) != "client":
                        found.stems[spec] = "hook" if _through_hook(callee) else "client"
                if _through_hook(callee):
                    found.hooks |= modules
                else:
                    found.route.modules |= modules
                found.route.globals |= route.globals
                found.route.packages |= route.packages
                found.route.names |= route.names
                if found.site is None and (route.modules or route.globals or route.packages):
                    found.site = _Site(path, node.start_point[0] + 1)
                found.read_files.add(file.path)
                holder = _top_declaration(node)
                if holder is not None:
                    found.readers.add((file.path, holder))
    finally:
        wt.recording(files, None)
    found.barrels = _reexporters(found.route.modules, repo_dir, files)
    found.route.modules |= found.barrels
    found.hooks -= found.route.modules
    found.hooks |= _reexporters(found.hooks, repo_dir, files) - found.route.modules
    declared: dict[str, set[str]] = {}
    for d in resolution.declarations:
        names = declared.setdefault(str(Path(d.module or d.path).resolve()), set())
        if d.owner is None and d.kind == cross_file_keys.CONSTANT:
            names.add(d.name)  # a string constant: handing it on hands on a string
    for barrel in _reexporters(set(declared), repo_dir, files):
        declared.setdefault(barrel, set()).update(n for names in declared.values() for n in names)
    found.keys = declared
    return found


def _is_package(spec: str, reader: str, repo_dir: str) -> bool:
    """Whether a bare specifier no resolver read names an npm package that
    cannot be a file of the checkout (:func:`cross_file_ts.package_outside`:
    no ``paths``/``baseUrl`` reads it, no workspace package or link is it, no
    bundler, Babel or Jest config aliases it, and it is declared by a version
    or installed). The runner's own aliases were tried first (:func:`_apply`).
    Kept per reader and specifier for the run by :func:`refusals`."""
    if spec.startswith((".", "/")):
        return False
    return cross_file_ts.package_outside(reader, spec, repo_dir)


def _resolve(reader: str, spec: str, repo_dir: str) -> str | None:
    """:func:`ts_scope.resolve`, and through a solution-style ``tsconfig.json``
    whose ``references`` hold the ``paths`` (a Vite app's ``@/*``). An answer
    depends on the reader's directory only, so it is kept per directory."""
    cache = _state(repo_dir).resolved
    asked = (os.path.dirname(reader), spec)
    if asked in cache:
        return cache[asked]
    found = ts_scope.resolve(reader, spec, repo_dir)
    if found is None and not spec.startswith("."):
        found = cross_file_ts.resolve_specifier(
            reader, spec, repo_dir, beside_declarations=True, solution=True, workspace=True
        )
    cache[asked] = found
    return found


#: The files whose loads can load a TypeScript or JavaScript module.
_LOADER_SUFFIXES = (*ts_scope.FAMILY_EXTENSIONS, *sorted(wt.source_index_suffixes()))


def _through_hook(callee: Node) -> bool:
    """Whether a read's callee is a hook or reaches the client through a
    hook call (``useFlag``, ``useFlags().isEnabled``)."""
    return wt._calls_a_hook(callee) or any(
        n.type == "call_expression" and wt._calls_a_hook(n.child_by_field_name("function"))
        for n in cross_file_keys.walk(callee)
    )


def _walk_unresolved(route: wt.Route, files: ts_scope.Files, accessors: tuple[str, ...], key: str) -> None:
    """Resolve each specifier the walk could not (``?<reader>\\0<spec>\\0<name>``)
    through :func:`_resolve`, and walk on from what it names, to a fixed point."""
    done: set[str] = set()
    while True:
        pending = [m for m in route.modules if m.startswith("?") and m not in done]
        if not pending:
            return
        for marker in pending:
            done.add(marker)
            reader, spec, name = marker[1:].split("\0")
            target = _resolve(reader, spec, files.repo_dir)
            if target is None:
                continue
            route.modules.discard(marker)
            route.modules.add(str(Path(target).resolve()))
            if name and name not in ("default", "="):
                wt._stable_export(str(Path(target).resolve()), name, files, 1, accessors, key=key)

def _segment(spec: str) -> str:
    """A specifier's or module's last path segment, its directory's for an ``index``."""
    parts = spec.replace("\\", "/").rstrip("/").split("/")
    stem = re.sub(r"(?:\.d)?\.[cm]?[jt]sx?$", "", parts[-1])
    return parts[-2] if stem == "index" and len(parts) > 1 else stem


#: ``export … from "<spec>"``, ``export * from``, ``export * as ns from``.
_REEXPORT = re.compile(r"""\bexport\b(?:\s+type\b)?[^;'"]*?\bfrom\s*(['"])([^'"\n]+)\1""")


def _reexporters(modules: set[str], repo_dir: str, files: ts_scope.Files) -> set[str]:
    """Every module that re-exports from one of ``modules`` or from another
    such module (``export … from``, renamed or ``*``, a CommonJS barrel), to
    a fixed point."""
    if not modules:
        return set()
    state = _state(repo_dir)
    candidates = []
    for path, text in state.texts.items():
        if not path.endswith(ts_scope.FAMILY_EXTENSIONS) or not (
            "module.exports" in text or ("export" in text and "from" in text)
        ):
            continue
        kept = state.reexports.get(path)
        if kept is None or kept[0] != text:
            targets: set[str] = set()
            # By text (a re-export spelled in a comment only adds a barrel);
            # the CommonJS forms need the parse.
            specs: list[str | None] = [m.group(2) for m in _REEXPORT.finditer(text)]
            if "module.exports" in text:
                file = files.file(path, text)
                specs += wt._commonjs_barrel_sources(file) if file is not None else []
            for spec in specs:
                target = _resolve(path, spec, repo_dir) if spec is not None else None
                if target is not None:
                    targets.add(str(Path(target).resolve()))
            kept = state.reexports[path] = (text, frozenset(targets))
        if kept[1]:
            candidates.append((path, kept[1]))
    found: set[str] = set()
    grew = True
    while grew:
        grew = False
        for path, targets in candidates:
            if path not in modules and path not in found and targets & (modules | found):
                found.add(path)
                grew = True
    return found


# --- runner configuration: aliases and module maps --------------------------------


@dataclass
class _Alias:
    """One ``moduleNameMapper`` or ``resolve.alias`` entry."""

    config: Path
    key: str
    #: The key is a regular expression (a mapper's, or a vite ``find`` regex).
    regex: bool
    #: The target as written, ``None`` when it is code this cannot read.
    value: str | None
    #: The config's directory, resolved: the project the alias is for.
    home: str = ""

    def __post_init__(self) -> None:
        if not self.home:
            self.home = str(self.config.parent.resolve())


_ALIAS_KEYS = frozenset({"moduleNameMapper", "alias"})
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".d.ts")


def _runner_configs(repo_dir: str) -> list[Path]:
    found = {
        p for p in _state(repo_dir).configs_list()
        if p.suffix in wt._CONFIG_SUFFIXES and (
            p.name == "package.json" or wt._CONFIG_NAME.match(p.name)
            or ts_mock_config._CONFIG_NAME.match(p.name) or ts_mock_config._VITE_NAME.match(p.name)
        )
    }
    for manifest in [p for p in found if p.name == "package.json"]:
        named = wt._script_setups(manifest)
        if named is not None:
            found |= {p for p, runner in named[0] if runner != "mocha" and p.is_file()}
    return sorted(found)


def _path_value(node: Node | None) -> str | None:
    """A config path as written: a string, or ``path.resolve``/``join`` (and
    ``fileURLToPath(new URL(...))``) of strings, ``__dirname`` and
    ``import.meta.url`` dropped (the config's own directory). ``None`` for
    anything else."""
    if node is None:
        return None
    plain = ts_mock_config.plain_string(node)
    if plain is not None:
        return plain
    if node.type in ("call_expression", "new_expression"):
        parts: list[str] = []
        arguments = node.child_by_field_name("arguments")
        for argument in arguments.named_children if arguments is not None else ():
            if argument.type == "comment":
                continue
            text = _text(argument)
            if text in ("__dirname", "import.meta.url", "process.cwd()", "import.meta.dirname"):
                continue
            inner = _path_value(argument)
            if inner is None:
                return None
            parts.append(inner)
        return "/".join(p.strip("/") if i else p.rstrip("/") for i, p in enumerate(parts)) if parts else "."
    return None


def _aliases_in(config: Path) -> tuple[list[_Alias], bool] | None:
    """``(the alias entries, whether it turns automock on)`` of one runner
    config, or ``None`` when it cannot be read."""
    text = _read(str(config))
    if text is None:
        return None
    found: list[_Alias] = []
    if config.suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return [], False  # no runner reads a config that is not JSON, so it maps nothing
        automock = False
        pending = [data]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key == "automock" and value is True:
                        automock = True
                    if key in _ALIAS_KEYS and isinstance(value, dict):
                        found += [
                            _Alias(config, k, key == "moduleNameMapper", v if isinstance(v, str) else None)
                            for k, v in value.items()
                        ]
                    else:
                        pending.append(value)
            elif isinstance(item, list):
                pending += item
        return found, automock
    if not re.search(r"\b(?:moduleNameMapper|alias|automock)\b", text):
        return [], False
    root = ts_scope.parse(text, str(config))
    automock = False
    for node in cross_file_keys.walk(root):
        if node.type != "pair":
            continue
        key = node.child_by_field_name("key")
        name = ts_mock_config.plain_string(key) if key is not None and key.type == "string" else (
            _text(key) if key is not None else ""
        )
        value = node.child_by_field_name("value")
        if name == "automock" and value is not None and value.type == "true":
            automock = True
        if name not in _ALIAS_KEYS or value is None:
            continue
        mapper = name == "moduleNameMapper"
        if value.type == "object":
            for entry in value.named_children:
                if entry.type != "pair":
                    if entry.type != "comment":
                        found.append(_Alias(config, "", True, None))  # a spread or computed key: anything
                    continue
                label = entry.child_by_field_name("key")
                spelled = ts_mock_config.plain_string(label) if label is not None and label.type == "string" else (
                    _text(label) if label is not None and label.type == "property_identifier" else None
                )
                if spelled is None:
                    found.append(_Alias(config, "", True, None))
                    continue
                found.append(_Alias(config, spelled, mapper, _path_value(entry.child_by_field_name("value"))))
        elif value.type == "array":
            for entry in value.named_children:
                if entry.type != "object":
                    continue
                pairs = {
                    _text(p.child_by_field_name("key")): p.child_by_field_name("value")
                    for p in entry.named_children
                    if p.type == "pair" and p.child_by_field_name("key") is not None
                }
                find = pairs.get("find")
                if find is None:
                    continue
                if find.type == "regex":
                    pattern = find.child_by_field_name("pattern")
                    found.append(_Alias(config, _text(pattern) if pattern is not None else "", True,
                                        _path_value(pairs.get("replacement"))))
                else:
                    spelled = ts_mock_config.plain_string(find)
                    found.append(_Alias(config, spelled or "", spelled is None,
                                        _path_value(pairs.get("replacement")) if spelled is not None else None))
        else:
            found.append(_Alias(config, "", True, None))  # an alias table this cannot read
    return found, automock


def _target_file(base: Path, value: str) -> str | None:
    """The source file a config path names, by the resolver's extension rules."""
    rooted = "<rootDir>" in value
    value = value.replace("<rootDir>", str(base))
    # `/src/x` is root-relative to a bundler and absolute to Node: both are tried.
    bases = [Path(value)] if rooted else [
        *([Path(value)] if Path(value).is_absolute() else []), base / value.lstrip("/")
    ]
    for path in bases:
        for candidate in (path, *(Path(str(path) + s) for s in _SOURCE_SUFFIXES), *(path / f"index{s}" for s in _SOURCE_SUFFIXES)):
            if candidate.is_file():
                return str(candidate.resolve())
    return None


def _apply(alias: _Alias, spec: str) -> str | None | bool:
    """What ``spec`` becomes under ``alias``: the target file, ``None`` when
    the alias does not match it, ``True`` when it matches and the target
    cannot be read or found."""
    if alias.regex:
        try:
            match = re.search(alias.key, spec) if alias.key else None
        except re.error:
            match = None
        if not alias.key:
            return True if alias.value is None else None
        if match is None:
            return None
        if alias.value is None:
            return True
        value = re.sub(r"\$(\d+)", lambda m: match.group(int(m.group(1))) or "", alias.value)
        value = re.sub(r"\\(\d+)", lambda m: match.group(int(m.group(1))) or "", value)
    else:
        if spec == alias.key:
            rest = ""
        elif spec.startswith(alias.key.rstrip("/") + "/"):
            rest = spec[len(alias.key.rstrip("/")) + 1:]
        else:
            return None
        if alias.value is None:
            return True
        value = alias.value.rstrip("/") + ("/" + rest if rest else "")
    target = _target_file(alias.config.parent, value)
    return target if target is not None else True


def _aliases_for(aliases: list[_Alias], reader: str) -> list[_Alias]:
    """The aliases of the configs whose directory holds ``reader``, nearest
    config first: a runner reads its own project's aliases only."""
    held = [a for a in aliases if reader == a.home or reader.startswith(a.home.rstrip(os.sep) + os.sep)]
    return sorted(held, key=lambda a: len(a.home), reverse=True)


def _literal_segment(alias: _Alias) -> str | None:
    """The last path segment an alias key spells literally (``keys`` for
    ``^./keys$``, ``@flags`` for ``@flags``), or ``None`` (``^@/(.*)$``)."""
    key = alias.key
    if alias.regex:
        key = re.sub(r"\\(.)", r"\1", key).strip("^$")
        if re.search(r"[()*+?\[\]{}|]", key.rsplit("/", 1)[-1]):
            return None
    segment = _segment(key) if key else ""
    return segment or None


# --- the test side ---------------------------------------------------------------


@dataclass
class _Context:
    repo_dir: str
    #: Client route modules (receivers, holders, getters, their barrels).
    modules: set[str]
    stems: dict[str, str]
    #: Key modules, with the constant names each declares.
    keys: dict[str, set[str]]
    aliases: list[_Alias]
    accessors: frozenset[str]
    readers: set[tuple[str, str]]
    barrels: set[str]
    hooks: set[str] = field(default_factory=set)
    #: Every name the route takes from a module of the repository.
    names: set[str] = field(default_factory=set)
    #: The literal the removal keeps (``true``/``false``), when known.
    kept: str | None = None

    def kind(self, module: str) -> str:
        """``client`` (any use but a read drives it), ``hook`` or ``key`` (only
        a mock, a spy or a write through it does)."""
        if module.startswith("?hook:"):
            return "hook"
        if module in self.modules or module.startswith("?"):
            return "client"
        return "hook" if module in self.hooks else "key"

    def stem(self, spec: str, resolved: str | None = None) -> str | None:
        """The marker of a route specifier no resolver read that ``spec`` may
        name: the same bare specifier (or a subpath of it), or for a relative
        one, the same last segment."""
        for stem, kind in self.stems.items():
            if stem.startswith("."):
                hit = _segment(resolved or spec) == _segment(stem)
            else:
                hit = spec == stem or spec.startswith(stem.rstrip("/") + "/")
            if hit:
                return f"?{'hook:' if kind == 'hook' else ''}{spec}"
        return None

    def on_route(self, module: str) -> bool:
        return module in self.modules or module in self.keys or module in self.hooks

    def segments(self) -> set[str]:
        return {_segment(m) for m in (*self.modules, *self.keys, *self.hooks)}

    def module(self, spec: str | None, path: str) -> str | None:
        """The route module ``spec`` loads from ``path``, else None. An alias
        a runner config maps it through counts; a specifier this cannot
        resolve counts when it is no package and its last segment is a route
        module's; a computed one always counts."""
        if spec is None:
            return "?a module this cannot read"
        target = _resolve(path, spec, self.repo_dir)
        if target is not None:
            resolved = str(Path(target).resolve())
            if self.on_route(resolved):
                return resolved
            return self.stem(spec, resolved)
        for alias in _aliases_for(self.aliases, path):
            applied = _apply(alias, spec)
            if isinstance(applied, str) and self.on_route(applied):
                return applied
        marker = self.stem(spec)
        if marker is not None:
            return marker
        if _is_package(spec, path, self.repo_dir):
            return None
        # A path-like specifier no resolver or alias reads (`@/lib/flags`,
        # `~/flags`) may be a bundler alias of the route module it is named
        # after. A single bare word is a package name.
        if "/" in spec and _segment(spec) in self.segments():
            return f"?{spec}"
        return None


def _line(node: Node, source: str) -> tuple[int, str]:
    line = node.start_point[0] + 1
    lines = source.splitlines()
    return line, (lines[line - 1].strip() if line - 1 < len(lines) else "")


class _MockApi:
    """The test runners' mock objects as ``file`` binds them: ``vi``,
    ``jest`` and ``vitest`` as globals or imports (``import { vi as v }``),
    Node's and Bun's ``mock``, an alias (``const v = vi``) and a method taken
    off one (``const { mock, spyOn: spy } = vi``)."""

    def __init__(self, file: ts_scope.File) -> None:
        self.objects: set[str] = {n for n in _RUNNER_GLOBALS if not ts_scope.bindings(file.root, n)}
        self.functions: dict[str, str] = {}
        for imported in file.imports:
            runner = _RUNNER_MODULES.get(imported.specifier or "")
            if runner is None:
                continue
            if imported.imported in runner or imported.imported in (None, "default"):
                self.objects.add(imported.local)
            elif imported.imported:
                self.functions[imported.local] = imported.imported
        grew = True
        declarators = [n for n in cross_file_keys.walk(file.root) if n.type == "variable_declarator"]
        while grew:
            grew = False
            for d in declarators:
                name, value = d.child_by_field_name("name"), d.child_by_field_name("value")
                if name is None or value is None:
                    continue
                while value.type in wt._TRANSPARENT:
                    value = wt._operand(value) or value
                    if value.type not in wt._TRANSPARENT:
                        break
                source = _text(value) if value.type == "identifier" else None
                if source not in self.objects:
                    continue
                if name.type == "identifier" and _text(name) not in self.objects:
                    self.objects.add(_text(name))
                    grew = True
                elif name.type == "object_pattern":
                    for entry in name.named_children:
                        if entry.type == "shorthand_property_identifier_pattern":
                            local, method = _text(entry), _text(entry)
                        elif entry.type == "pair_pattern":
                            label, bound = entry.child_by_field_name("key"), entry.child_by_field_name("value")
                            if label is None or bound is None or bound.type != "identifier":
                                continue
                            local, method = _text(bound), _text(label)
                        else:
                            continue
                        if self.functions.get(local) != method:
                            self.functions[local] = method
                            grew = True

    def method(self, call: Node) -> str | None:
        """The mock API method ``call`` calls (``mock`` for ``v.mock(...)``), else None."""
        return self.method_of(call.child_by_field_name("function"))

    def method_of(self, function: Node | None) -> str | None:
        """:meth:`method` of a call's function."""
        while function is not None and function.type in wt._TRANSPARENT:
            function = wt._operand(function)
        if function is None:
            return None
        if function.type == "identifier":
            return self.functions.get(_text(function))
        if function.type != "member_expression":
            return None
        prop = function.child_by_field_name("property")
        receiver = function.child_by_field_name("object")
        while receiver is not None and receiver.type == "member_expression":
            tail = receiver.child_by_field_name("property")
            if tail is not None and _text(tail) == "mock":  # `t.mock.module(...)`
                return _text(prop) if prop is not None else None
            receiver = None
        if receiver is None or receiver.type != "identifier" or prop is None:
            return None
        return _text(prop) if _text(receiver) in self.objects else None


def _accessor_root(node: Node, accessors: frozenset[str]) -> bool:
    """Whether a use of a bound name is only the root of a read
    (``client.isEnabled(...)``, ``Flags.client.isEnabled(...)``,
    ``getClient().variation(...)``) or ``typeof``: nothing else it may be
    handed to can choose what a read answers."""
    parent = node.parent
    if parent is not None and parent.type == "unary_expression" and any(c.type == "typeof" for c in parent.children):
        return True
    if parent is not None and parent.type == "type_query":
        return True
    chain = node
    seen = 0
    while seen < 64:
        seen += 1
        up = chain.parent
        if up is None:
            return False
        if up.type in wt._TRANSPARENT:
            chain = up
        elif up.type == "member_expression" and up.child_by_field_name("object") == chain:
            chain = up
        elif up.type == "subscript_expression" and up.child_by_field_name("object") == chain:
            index = up.child_by_field_name("index")
            if index is None or not wt._literal_operand(index):
                return False
            chain = up
        elif up.type == "call_expression" and up.child_by_field_name("function") == chain:
            last = chain.child_by_field_name("property") if chain.type == "member_expression" else chain
            if last is not None and _text(last) in accessors:
                return True
            if not all(wt._literal_operand(a) for a in mock_stubs.ts_arguments(up)):
                return False
            chain = up  # a call on the way to a read: `getClient().isEnabled(...)`
        else:
            return False
    return False


def _runs_a_reader(node: Node, imported: str | None, module: str, ctx: _Context) -> bool:
    """Whether a use calls (or constructs) a top-level declaration holding a
    read (``handler()``, ``ns.handler()``, ``new Service()``): that runs the
    read, it does not choose what the client answers."""
    names = {n for m, n in ctx.readers if m == module or module in ctx.barrels or module.startswith("?")}
    target = node
    name = imported
    if imported is None:  # a namespace: `ns.handler()`
        parent = node.parent
        if parent is None or parent.type != "member_expression" or parent.child_by_field_name("object") != node:
            return False
        prop = parent.child_by_field_name("property")
        name = _text(prop) if prop is not None else None
        target = parent
    if name not in names:
        return False
    parent = target.parent
    return parent is not None and (
        (parent.type == "call_expression" and parent.child_by_field_name("function") == target)
        or (parent.type == "new_expression" and parent.child_by_field_name("constructor") == target)
    )


def _declarator_of(node: Node) -> Node | None:
    """The declarator whose name (or pattern) ``node`` is in, else None."""
    current = node
    while current.parent is not None and current.parent.type in (
        "object_pattern", "array_pattern", "pair_pattern", "assignment_pattern", "rest_pattern",
    ):
        current = current.parent
    declarator = current.parent
    if declarator is not None and declarator.type == "variable_declarator" and (
        declarator.child_by_field_name("name") == current
    ):
        return declarator
    return None


def _is_binding_site(node: Node) -> bool:
    """Whether ``node`` is where its name is bound (an import or export
    specifier, a declarator's name or pattern), not a use of it."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in wt._NOT_A_USE or parent.type in ("import_require_clause", "export_specifier"):
        return True
    return _declarator_of(node) is not None


def _unwrapped(value: Node | None) -> Node | None:
    while value is not None and (value.type == "await_expression" or value.type in wt._TRANSPARENT):
        if value.type == "await_expression":
            value = next((c for c in value.named_children if c.type != "comment"), None)
        else:
            value = wt._operand(value)
    return value


def _load_call(call: Node, api: _MockApi) -> bool:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "import":
        return True
    return mock_stubs.ts_callee(call) in _LOADERS or api.method(call) in _MOCK_LOADERS


def _from_load(binding: Node, api: _MockApi) -> bool:
    """Whether a binding node is made by a load: an import, or a declarator
    whose value is a ``require``/``import()``/``requireActual`` call."""
    parent = binding.parent
    while parent is not None and parent.type not in ("program", "statement_block"):
        if parent.type in ("import_statement", "import_require_clause"):
            return True
        parent = parent.parent
    declarator = _declarator_of(binding)
    value = _unwrapped(declarator.child_by_field_name("value") if declarator is not None else None)
    return value is not None and value.type == "call_expression" and _load_call(value, api)


@dataclass
class _Bound:
    module: str
    #: The exported name; ``None`` for the module object (namespace, ``require``).
    imported: str | None


def _bound_from_loads(
    file: ts_scope.File, ctx: _Context, api: _MockApi
) -> tuple[dict[str, _Bound], list[tuple[Node, str]]]:
    """``({local name: what it holds}, [(load node, route module)])``: the
    names ``file`` binds from route modules, and each load it makes of one
    whose value no declaration binds whole (``(await import("./c")).client``)."""
    bound: dict[str, _Bound] = {}
    for imported in file.imports:
        if imported.type_only:
            continue
        if imported.module is not None:
            module = str(Path(imported.module).resolve())
            hit = module if ctx.on_route(module) else None
            if hit is None and imported.specifier:
                hit = ctx.module(imported.specifier, file.path) if not imported.specifier.startswith(".") else None
        else:
            hit = ctx.module(imported.specifier, file.path) if imported.specifier else None
        if hit is not None:
            bound[imported.local] = _Bound(hit, imported.imported if imported.imported not in ("=",) else None)
    loose: list[tuple[Node, str]] = []
    for node in cross_file_keys.walk(file.root):
        if node.type != "call_expression" or not _load_call(node, api):
            continue
        if node.parent is not None and node.parent.type == "unary_expression" and _text(node.parent).startswith("typeof"):
            continue  # `typeof import("./x")`, a type the grammar reads as a call
        arguments = mock_stubs.ts_arguments(node)
        if not arguments:
            continue
        spec = ts_mock_config.plain_string(arguments[0])
        if spec is None:
            continue  # a computed load (`import(pathToFileURL(p).href)`): not followed
        module = ctx.module(spec, file.path)
        if module is None:
            continue
        holder = node.parent
        while holder is not None and (holder.type == "await_expression" or holder.type in wt._TRANSPARENT):
            holder = holder.parent
        target = holder.child_by_field_name("name") if holder is not None and holder.type == "variable_declarator" else None
        if target is None:
            loose.append((node, module))
            continue
        if target.type == "identifier":
            bound[_text(target)] = _Bound(module, None)
            continue
        for n in cross_file_keys.walk(target):
            if n.type == "shorthand_property_identifier_pattern":
                bound[_text(n)] = _Bound(module, _text(n))
            elif n.type == "pair_pattern":
                label, value = n.child_by_field_name("key"), n.child_by_field_name("value")
                if value is not None and value.type == "identifier":
                    bound[_text(value)] = _Bound(module, _text(label) if label is not None else None)
            elif n.type == "identifier" and _text(n) not in bound:
                bound[_text(n)] = _Bound(module, None)
    return bound, loose


def _written_through(node: Node) -> bool:
    """Whether ``node`` is the root of what an assignment, update or
    ``delete`` writes (``keys.K = "x"``, ``ns.flags.client = fake``)."""
    chain = node
    while chain.parent is not None and (
        chain.parent.type in wt._TRANSPARENT
        or (chain.parent.type in ("member_expression", "subscript_expression")
            and chain.parent.child_by_field_name("object") == chain)
    ):
        chain = chain.parent
        target = wt._write_target(chain.parent) if chain.parent is not None else None
        if target is not None and target == chain:
            return True
    target = wt._write_target(chain.parent) if chain.parent is not None else None
    return target is not None and target == chain


def _handed_whole(node: Node) -> Node | None:
    """The call ``node`` itself (not a member of it) is an argument of, seen
    through :data:`wrapper_ts._TRANSPARENT`, else None."""
    current = node
    while current.parent is not None and current.parent.type in wt._TRANSPARENT:
        current = current.parent
    arguments = current.parent
    if arguments is None or arguments.type != "arguments":
        return None
    return arguments.parent


def _spied(node: Node, api: _MockApi) -> bool:
    """Whether ``node`` itself is handed to a spy or a writer
    (``vi.spyOn(require("./keys"), "K")``, ``Object.assign(...)``)."""
    call = _handed_whole(node)
    return call is not None and (api.method(call) in _SPIES or mock_stubs.ts_callee(call) in _WRITERS)

def _names_route(node: Node, bound: _Bound, ctx: _Context, api: _MockApi) -> bool:
    """Whether a use of a key's or a hook's module touches a name the route
    takes (``ctx.names``: the key constants, the hook and what it was followed
    through): the binding's own name, the member a namespace use names
    (``keys.K``), or the property a spy on the namespace names. A namespace
    handed on whole, or a member this cannot name, may touch any."""
    if bound.imported is not None:
        return bound.imported in ctx.names
    parent = node.parent
    if parent is not None and parent.type in ("member_expression", "subscript_expression") and (
        parent.child_by_field_name("object") == node
    ):
        member = parent.child_by_field_name("property") if parent.type == "member_expression" else None
        if member is None:
            index = parent.child_by_field_name("index")
            spelled = ts_mock_config.plain_string(index) if index is not None else None
            return spelled is None or spelled in ctx.names
        return _text(member) in ctx.names
    call = _handed_whole(node)
    if call is not None and (api.method(call) in _SPIES or mock_stubs.ts_callee(call) in _WRITERS):
        arguments = mock_stubs.ts_arguments(call)
        if len(arguments) > 1 and arguments[0] == node or (
            len(arguments) > 1 and _unwrapped(arguments[0]) == node
        ):
            spelled = ts_mock_config.plain_string(arguments[1])
            return spelled is None or spelled in ctx.names
    return True

def _key_use_drives(node: Node, bound: _Bound, ctx: _Context, api: _MockApi, leaf: bool = False) -> bool:
    """Whether a use of a name taken from a key module may change the key a
    read sees: a write through it, or the module object (or an object holding
    keys) handed on, spied on, aliased or spread. Reading a constant, or a
    member of one, does not."""
    if _written_through(node):
        return True
    leaf = leaf or (bound.imported is not None and bound.imported in ctx.keys.get(bound.module, set()))
    parent = node.parent
    member_read = parent is not None and parent.type in ("member_expression", "subscript_expression") and (
        parent.child_by_field_name("object") == node
    )
    if member_read:
        return False
    call = _handed_whole(node)
    if call is not None:
        method = api.method(call)
        callee = mock_stubs.ts_callee(call)
        if method in _SPIES or callee in _WRITERS:
            return True
        return not leaf
    if parent is not None and parent.type in ("spread_element", "variable_declarator", "assignment_expression", "pair"):
        return not leaf
    return False


def _module_uses(file: ts_scope.File, bound: dict[str, _Bound], ctx: _Context, api: _MockApi) -> list[tuple[Node, str]]:
    """Each use ``file`` makes of a name bound from a route module that may
    drive it, with that module."""
    found: list[tuple[Node, str]] = []
    if not bound:
        return found
    shadowed: dict[str, bool] = {}
    for node in cross_file_keys.walk(file.root):
        if node.type not in ("identifier", "shorthand_property_identifier") or _text(node) not in bound:
            continue
        if _is_binding_site(node) or wt._in_type(node):
            continue
        name = _text(node)
        if name not in shadowed:
            shadowed[name] = len(ts_scope.bindings(file.root, name)) > 1
        binding = file.binding_of(node) if shadowed[name] else None
        if binding is not None and not _from_load(binding, api):
            continue  # another binding of the same name: a local of the test's own
        held = bound[_text(node)]
        if ctx.kind(held.module) != "client":
            if _names_route(node, held, ctx, api) and _key_use_drives(
                node, held, ctx, api, leaf=ctx.kind(held.module) == "hook"
            ):
                found.append((node, held.module))
            continue
        if node.type == "identifier" and (
            _accessor_root(node, ctx.accessors) or _runs_a_reader(node, held.imported, held.module, ctx)
        ):
            continue
        found.append((node, held.module))
    return found


def _module_mocks(file: ts_scope.File, ctx: _Context, api: _MockApi) -> list[tuple[Node, str]]:
    """Each module mock (by what the call IS) of a route module, and each
    ``enableAutomock()``."""
    found: list[tuple[Node, str]] = []
    for node in cross_file_keys.walk(file.root):
        generic = _misparsed_generic_call(node)
        if generic is not None:
            function, arguments = generic
            method = api.method_of(function)
            spelled = "".join(_text(function).split()) in wt._PACKAGE_MOCKS
        elif node.type == "call_expression":
            method = api.method(node)
            spelled = mock_stubs.ts_callee(node) in wt._PACKAGE_MOCKS
            arguments = mock_stubs.ts_arguments(node)
        else:
            continue
        if method not in _MODULE_MOCKS and not spelled:
            continue
        if method == "enableAutomock":
            found.append((node, "every module"))
            continue
        spec = wt._mock_specifier(arguments[0]) if arguments else None
        module = ctx.module(spec, file.path)
        if module is None:
            continue
        if (method in _FACTORY_MOCKS or spelled) and _factory_spares(arguments, ctx.names, api, ctx.accessors, ctx.kept):
            continue  # a partial mock that leaves the route's names alone
        if ctx.kind(module) == "key" and len(arguments) < 2 and method != "setMock" and not (
            module.startswith("?") or ts_key_mocks._manual_mock(module)
        ):
            continue  # an automock keeps a string export's value (`ts_key_mocks`)
        found.append((node, module))
    return found


def _misparsed_generic_call(node: Node) -> tuple[Node, list[Node]] | None:
    """``(function, arguments)`` of a generic call the grammar reads as two
    comparisons: ``vi.mock<typeof import("./x")>("./x", f)`` parses as
    ``(vi.mock < typeof import("./x")) > ("./x", f)``."""
    if node.type != "binary_expression" or _text(node.child_by_field_name("operator") or node) != ">":
        return None
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None or left.type != "binary_expression" or right.type != "parenthesized_expression":
        return None
    if _text(left.child_by_field_name("operator") or left) != "<":
        return None
    function = left.child_by_field_name("left")
    if function is None or function.type not in ("member_expression", "identifier"):
        return None
    inner = next((c for c in right.named_children if c.type != "comment"), None)
    if inner is None:
        return None
    arguments = [c for c in inner.named_children if c.type != "comment"] if inner.type == "sequence_expression" else [inner]
    return function, arguments

#: The module mocks that take a factory as their second argument.
_FACTORY_MOCKS = frozenset({"mock", "doMock", "unstable_mockModule", "module"})
#: Calls that hand a factory the real module.
_ORIGINALS = frozenset({"vi.importActual", "jest.requireActual"})


def _factory_spares(
    arguments: list[Node], names: set[str], api: _MockApi, accessors: frozenset[str] = frozenset(), kept: str | None = None
) -> bool:
    """Whether a module mock's factory leaves every name the route takes
    alone: an object literal (returned by the factory, through ``async`` and a
    ``const`` holding the original) whose entries are spreads of the real
    module (``...(await importOriginal())``, ``...actual``,
    ``...jest.requireActual(...)``) or plain keys no route name spells. An
    accessor answering the literal the removal keeps (``useFlag: () =>
    true`` for ``treatment: true``) answers what the fold does, so it is
    spared too. A mock with no factory, or one this cannot read, replaces
    them."""
    if len(arguments) < 2:
        return False
    factory = arguments[1]
    while factory.type in wt._TRANSPARENT:
        factory = wt._operand(factory) or factory
        if factory.type not in wt._TRANSPARENT:
            break
    if factory.type not in ("arrow_function", "function_expression", "function"):
        return False
    originals = {n for n in wt._parameter_names(factory)}
    body = factory.child_by_field_name("body")
    value: Node | None = body
    if body is not None and body.type == "statement_block":
        statements = [c for c in body.named_children if c.type != "comment"]
        if not statements or statements[-1].type != "return_statement":
            return False
        for statement in statements[:-1]:
            declarators = [c for c in statement.named_children if c.type == "variable_declarator"]
            if statement.type != "lexical_declaration" or len(declarators) != 1:
                return False
            name = declarators[0].child_by_field_name("name")
            if name is None or name.type != "identifier" or not _is_original(
                declarators[0].child_by_field_name("value"), originals, api
            ):
                return False
            originals.add(_text(name))
        value = next((c for c in statements[-1].named_children if c.type != "comment"), None)
    value = _unwrapped(value)
    if value is None or value.type != "object":
        return False
    for entry in value.named_children:
        if entry.type == "comment":
            continue
        if entry.type == "spread_element":
            inner = next((c for c in entry.named_children if c.type != "comment"), None)
            if not _is_original(inner, originals, api):
                return False
            continue
        if entry.type == "shorthand_property_identifier":
            label = _text(entry)
        elif entry.type in ("pair", "method_definition"):
            key = entry.child_by_field_name("key") if entry.type == "pair" else entry.child_by_field_name("name")
            if key is None or key.type == "computed_property_name":
                return False
            label = ts_mock_config.plain_string(key) if key.type == "string" else _text(key)
        else:
            return False
        if label is None or label == "default":
            return False
        if label in accessors and kept is not None and _answers(entry, kept):
            continue
        if label in names:
            return False
    return True


def _answers(entry: Node, kept: str) -> bool:
    """Whether a factory entry is a function answering the literal ``kept``
    whatever it is asked: ``() => true``, ``function () { return true }``,
    ``vi.fn(() => true)``, ``vi.fn().mockReturnValue(true)``."""
    value = entry.child_by_field_name("value") if entry.type == "pair" else entry
    value = _unwrapped(value)
    if value is None:
        return False
    if value.type == "call_expression":
        callee = mock_stubs.ts_callee(value)
        arguments = mock_stubs.ts_arguments(value)
        if callee in ("vi.fn", "jest.fn") and len(arguments) == 1:
            value = _unwrapped(arguments[0])
        elif callee.endswith((".mockReturnValue", ".mockImplementation")) and callee.split(".")[0] in ("vi", "jest") and len(arguments) == 1:
            first = _unwrapped(arguments[0])
            if callee.endswith(".mockReturnValue"):
                return first is not None and first.type == kept
            value = first
        else:
            return False
    if value is None or value.type not in ("arrow_function", "function_expression", "function", "method_definition"):
        return False
    body = value.child_by_field_name("body")
    if body is not None and body.type == "statement_block":
        statements = [c for c in body.named_children if c.type != "comment"]
        if len(statements) != 1 or statements[0].type != "return_statement":
            return False
        body = next((c for c in statements[0].named_children if c.type != "comment"), None)
    body = _unwrapped(body)
    return body is not None and body.type == kept


def _is_original(node: Node | None, originals: set[str], api: _MockApi) -> bool:
    """Whether ``node`` is the real module: ``importOriginal()`` (the
    factory's parameter), a name holding it, ``vi.importActual(...)`` or
    ``jest.requireActual(...)``, through ``await`` and parentheses."""
    node = _unwrapped(node)
    if node is None:
        return False
    if node.type == "identifier":
        return _text(node) in originals
    if node.type != "call_expression":
        return False
    # `await orig<T>()` parses as a call whose function is `await orig`.
    function = _unwrapped(node.child_by_field_name("function"))
    if function is None:
        return False
    if function.type == "identifier" and _text(function) in originals:
        return True
    return "".join(_text(function).split()) in _ORIGINALS or api.method_of(function) in ("importActual", "requireActual")


def _chain(node: Node | None) -> tuple[str, str | None] | None:
    """``(root, first member)`` of a member chain rooted at a plain name
    (``("globalThis", "posthog")`` for ``window.posthog.x``), through
    :data:`wrapper_ts._TRANSPARENT`; the member is ``None`` for the bare
    name, and ``"?"`` for a computed one."""
    steps: list[str] = []
    seen = 0
    while node is not None and seen < 64:
        seen += 1
        if node.type in wt._TRANSPARENT:
            node = wt._operand(node)
        elif node.type == "member_expression":
            prop = node.child_by_field_name("property")
            steps.append(_text(prop) if prop is not None else "?")
            node = node.child_by_field_name("object")
        elif node.type == "subscript_expression":
            index = node.child_by_field_name("index")
            steps.append((ts_mock_config.plain_string(index) or "?") if index is not None else "?")
            node = node.child_by_field_name("object")
        elif node.type == "identifier":
            return wt.global_root(_text(node)), (steps[-1] if steps else None)
        else:
            return None
    return None


def _matches(chain: tuple[str, str | None] | None, globals_: set[tuple[str, str | None]], second: str | None) -> bool:
    if chain is None:
        return False
    root, member = chain
    member = member if member is not None else second
    for g_root, g_member in globals_:
        if root != g_root:
            continue
        if g_member is None or member is None or member == "?" or member == g_member:
            return True
    return False


def _global_writes(file: ts_scope.File, globals_: set[tuple[str, str | None]], api: _MockApi) -> list[Node]:
    """Each write ``file`` makes to a global the route reads: an assignment,
    update or ``delete`` through it, a stub or ``Object.assign`` of it, or
    ``stubEnv``/``stubGlobal``. A name the file binds itself is not the global."""
    found: list[Node] = []
    local = {r for r, _ in globals_ if not wt._global_name(file, r)}
    live = {g for g in globals_ if g[0] not in local}
    if not live:
        return found
    for node in cross_file_keys.walk(file.root):
        target = wt._write_target(node)
        if target is not None:
            chain = _chain(target)
            if chain is not None and chain[1] is not None and _matches(chain, live, None):
                found.append(node)
            continue
        if node.type != "call_expression":
            continue
        method = api.method(node)
        arguments = mock_stubs.ts_arguments(node)
        if method == "stubEnv":
            if any(g[0] == "process" and g[1] in (None, "env") for g in live):
                found.append(node)
        elif method == "stubGlobal":
            name = ts_mock_config.plain_string(arguments[0]) if arguments else None
            if any(g[0] == "globalThis" and (g[1] is None or name is None or g[1] == name) for g in live):
                found.append(node)
        elif (method in _SPIES or mock_stubs.ts_callee(node) in _WRITERS) and arguments:
            second = ts_mock_config.plain_string(arguments[1]) if len(arguments) > 1 else None
            if len(arguments) > 1 and second is None:
                second = "?"
            if _matches(_chain(arguments[0]), live, second):
                found.append(node)
    return found


# --- loaders that replace a module's imports or variables ------------------------

#: Packages whose call loads a module with some of its imports replaced
#: (``proxyquire("./a", {"./keys": stub})``), or with its top-level variables
#: writable (``rewire("./a").__set__("K", v)``).
_PROXYQUIRE = frozenset({"proxyquire"})
_REWIRE = frozenset({"rewire"})
#: What ``rewire`` and ``babel-plugin-rewire`` add to a module to replace its variables.
_REWIRE_API = frozenset(
    {"__set__", "__get__", "__with__", "__Rewire__", "__ResetDependency__", "__RewireAPI__", "__GetDependency__"}
)


def _chain_root(node: Node | None) -> Node | None:
    """The leftmost operand of a chain of members and argument-less calls
    (``pq`` of ``pq.noCallThru().load``): the loader, configured. A call
    with arguments (``rewire("./a")``) is a loaded module, and ends it."""
    while node is not None:
        if node.type in wt._TRANSPARENT:
            node = wt._operand(node)
        elif node.type == "member_expression":
            node = node.child_by_field_name("object")
        elif node.type == "call_expression" and not _require_of(node):
            if mock_stubs.ts_arguments(node):
                return None
            node = node.child_by_field_name("function")
        else:
            return node
    return None


def _require_of(call: Node) -> str | None:
    """The specifier of ``require("x")``, else None."""
    function = call.child_by_field_name("function")
    if function is None or function.type != "identifier" or _text(function) != "require":
        return None
    arguments = mock_stubs.ts_arguments(call)
    return cross_file_ts._string_value(arguments[0]) if arguments else None


def _loader_names(file: ts_scope.File, packages: frozenset[str]) -> set[str]:
    """The names ``file`` binds to one of ``packages`` (an import, a
    ``require``, an alias, a configured copy: ``const pq = proxyquire.noCallThru()``),
    to a fixed point."""
    names = {i.local for i in file.imports if (i.specifier or "") in packages and not i.type_only}
    declarators = [n for n in cross_file_keys.walk(file.root) if n.type == "variable_declarator"]
    grew = True
    while grew:
        grew = False
        for d in declarators:
            name, value = d.child_by_field_name("name"), d.child_by_field_name("value")
            if name is None or name.type != "identifier" or value is None or _text(name) in names:
                continue
            if _loads_from(value, names, packages):
                names.add(_text(name))
                grew = True
    return names


def _loads_from(node: Node | None, names: set[str], packages: frozenset[str]) -> bool:
    root = _chain_root(node)
    if root is None:
        return False
    if root.type == "identifier":
        return _text(root) in names
    return root.type == "call_expression" and _require_of(root) in packages


def _substitutions(
    file: ts_scope.File, ctx: _Context, read_files: set[str], reach: "_Reach"
) -> list[tuple[Node, str]]:
    """Each load that replaces a route module or a variable of the code under
    test: a ``proxyquire`` stub keyed by a specifier that names a route module
    (read from the loaded module), a stub map this cannot read for a module
    that may reach the read, and ``rewire``'s ``__set__``/``__with__`` (or
    ``babel-plugin-rewire``'s ``__Rewire__``) on a file holding a read or a
    route module."""
    found: list[tuple[Node, str]] = []
    proxy = _loader_names(file, _PROXYQUIRE)
    rewire = _loader_names(file, _REWIRE)
    rewires = any(name in file.source for name in _REWIRE_API)
    for node in cross_file_keys.walk(file.root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = mock_stubs.ts_arguments(node)
        spec = cross_file_ts._string_value(arguments[0]) if arguments else None
        if proxy and len(arguments) >= 2 and _loads_from(function, proxy, _PROXYQUIRE):
            if spec is None:
                found.append((node, "?a module this cannot read"))
                continue
            target = _resolve(file.path, spec, ctx.repo_dir)
            base = str(Path(target).resolve()) if target is not None else file.path
            stubs = _unwrapped(arguments[1])
            if stubs is None or stubs.type != "object":
                if target is None or base in read_files or reach.reaches(base):
                    found.append((node, f"?{spec}"))
                continue
            for entry in stubs.named_children:
                if entry.type == "comment":
                    continue
                key = entry.child_by_field_name("key") if entry.type == "pair" else None
                named = cross_file_ts._string_value(key) if key is not None and key.type == "string" else None
                if named is None and key is not None and key.type == "property_identifier":
                    named = _text(key)
                module = ctx.module(named, base) if named is not None or entry.type != "pair" else None
                if named is None and (target is None or base in read_files or reach.reaches(base)):
                    module = module or f"?{spec}"
                if module is not None:
                    found.append((node, module))
                    break
        elif rewire and rewires and arguments and _loads_from(function, rewire, _REWIRE):
            target = _resolve(file.path, spec, ctx.repo_dir) if spec is not None else None
            resolved = str(Path(target).resolve()) if target is not None else None
            if spec is None or resolved is None or resolved in read_files or ctx.on_route(resolved):
                found.append((node, resolved or f"?{spec}"))
    # babel-plugin-rewire: the API is an export of the module itself.
    if rewires:
        for imported in file.imports:
            module = str(Path(imported.module).resolve()) if imported.module is not None else None
            if module is None or not (module in read_files or ctx.on_route(module)):
                continue
            uses = [
                n for n in cross_file_keys.walk(file.root)
                if n.type in ("identifier", "property_identifier") and _text(n) in _REWIRE_API
                and (imported.imported in _REWIRE_API and _text(n) == imported.local or n.type == "property_identifier"
                     and n.parent is not None and n.parent.type == "member_expression"
                     and _text(n.parent.child_by_field_name("object") or n) == imported.local)
            ]
            if uses:
                found.append((uses[0], module))
    return found


def _test_only(path: str, files: ts_scope.Files, setups: frozenset[str] | None) -> bool:
    """Whether only test code loads the module at ``path``: something loads
    it, and no production file does, where a production file is a non-test
    file that nothing loads (an entry point) or that a production file loads.
    So a cycle of production modules an entry point reaches is production
    code (``wrapper_ts._test_support`` caches a cycle as test code, which is
    right for its narrower question only), and a module nothing loads is an
    entry point. Loaders are found by the module's last path segment in the
    text of the checkout's files and resolved by :func:`_resolve`; a load that
    does not resolve is not a loader (it only makes more modules test code)."""
    memo: dict[str, bool] = files.__dict__.setdefault("_test_only", {})
    return _loaded(path, files, setups, memo) and not _production(path, files, setups, memo)


def _texts(files: ts_scope.Files) -> dict[str, str]:
    """Every file of the checkout whose loads can load a script, by resolved path."""
    return _state(files.repo_dir).texts


def _loaders_of(path: str, files: ts_scope.Files) -> set[str]:
    cache = _state(files.repo_dir).loaders
    if path in cache:
        return cache[path]
    segment = _segment(path)
    found: set[str] = set()
    for loader, text in _texts(files).items():
        if loader == path or segment not in text:
            continue
        for match in wt._LOADS.finditer(text):
            spec = match.group(2)
            if _segment(spec) != segment and not spec.rstrip("/").endswith(segment):
                continue
            target = _resolve(loader, spec, files.repo_dir)
            if target is not None and str(Path(target).resolve()) == path:
                found.add(loader)
                break
    cache[path] = found
    return found


def _is_test_code(path: str, setups: frozenset[str] | None) -> bool:
    if not path.endswith(ts_scope.FAMILY_EXTENSIONS):
        return False  # a component file: production code
    return wt._stubs(path, _read(path) or "") or setups is None or wt._setup_file(path, setups)


def _loaded(path: str, files: ts_scope.Files, setups: frozenset[str] | None, memo: dict) -> bool:
    return bool(_loaders_of(path, files))


def _production(path: str, files: ts_scope.Files, setups: frozenset[str] | None, memo: dict) -> bool:
    """Whether a production file reaches ``path`` (an entry point, through
    non-test files)."""
    return _reached(path, files, setups, memo, set())[0]


def _reached(
    path: str, files: ts_scope.Files, setups: frozenset[str] | None, memo: dict, walk: set[str]
) -> tuple[bool, bool]:
    """``(reached, final)``: ``final`` is False when the answer leaned on a
    module still on ``walk`` (a cycle), which is then not cached, so a module
    is never left marked test code because it was asked about mid-cycle."""
    if path in memo:
        return memo[path], True
    if path in walk:
        return False, False
    walk.add(path)
    loaders = _loaders_of(path, files)
    answer, final = (not loaders and not _is_test_code(path, setups)), True
    for loader in sorted(loaders):
        if answer:
            break
        if _is_test_code(loader, setups):
            continue
        reached, settled = _reached(loader, files, setups, memo, walk)
        answer, final = reached, final and settled
    walk.discard(path)
    if answer or final:
        memo[path] = answer
    return answer, answer or final

def _setup_closure(
    every: list[str], files: ts_scope.Files, setups: frozenset[str] | None, repo_dir: str
) -> set[str]:
    """The setup files among ``every`` (every test file when the setup list
    cannot be read), and every module only test code loads that one of them
    loads, to a fixed point."""
    found: set[str] = set()
    pending: list[str] = []
    for path in every:
        if not os.path.isfile(path):
            continue
        if setups is None:
            if wt._stubs(path, _read(path) or ""):
                pending.append(str(Path(path).resolve()))
        elif wt._setup_file(path, setups):
            pending.append(str(Path(path).resolve()))
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.add(path)
        file = files.file(path, _state(repo_dir).texts.get(path))
        if file is None:
            continue
        for spec in wt._loaded_specifiers(file.root):
            target = _resolve(path, spec, repo_dir)
            if target is None:
                continue
            resolved = str(Path(target).resolve())
            if resolved not in found and _test_only(resolved, files, setups):
                pending.append(resolved)
    return found


def _package_uses(
    file: ts_scope.File, packages: set[str], accessors: frozenset[str], api: _MockApi
) -> list[tuple[Node, str]]:
    found: list[tuple[Node, str]] = []
    for name in sorted(packages):
        for node in cross_file_keys.walk(file.root):
            if node.type == "call_expression" and (
                api.method(node) in _MODULE_MOCKS or mock_stubs.ts_callee(node) in wt._PACKAGE_MOCKS
            ):
                arguments = mock_stubs.ts_arguments(node)
                spec = wt._mock_specifier(arguments[0]) if arguments else None
                if spec is None or wt._names_package(spec, name):
                    found.append((node, name))
        bound = wt._package_bindings(file, name)
        if bound and wt._mentions_beyond_reads(file, bound, accessors):
            node = next(
                (n for n in cross_file_keys.walk(file.root)
                 if n.type == "identifier" and _text(n) in bound and not _is_binding_site(n)),
                file.root,
            )
            found.append((node, name))
        elif wt._loads_package(file, name, bound):
            found.append((file.root, name))
    return found


def _config_refusals(ctx: _Context, configs: list[Path]) -> tuple[dict[str, str], list[_Alias]]:
    """``({config: reason}, every alias read)``: a config that turns automock
    on, or maps a specifier naming a route module to another file (a
    ``moduleNameMapper`` stub), refuses; one that cannot be read too."""
    out: dict[str, str] = {}
    aliases: list[_Alias] = []
    segments = ctx.segments()
    repo = ctx.repo_dir
    state = _state(ctx.repo_dir)
    for config in configs:
        key = str(config)
        if key not in state.aliases:
            state.aliases[key] = _aliases_in(config)
        read = state.aliases[key]
        rel = os.path.relpath(config, repo)
        if read is None:
            out[str(config)] = f"{rel} cannot be read, so it may map a module the flag's read goes through"
            continue
        entries, automock = read
        aliases += entries
        if automock:
            out[str(config)] = f"{rel} turns on automock, which replaces the modules the flag's read goes through"
            continue
        for alias in entries:
            segment = _literal_segment(alias)
            if alias.key == "" and alias.value is None:
                out.setdefault(str(config), f"{rel} has a module map this cannot read, which may replace a module the flag's read goes through")
                continue
            if segment is None or segment not in segments:
                continue
            target = _apply(alias, alias.key) if not alias.regex else (
                _target_file(alias.config.parent, alias.value) if alias.value is not None and "$" not in alias.value else True
            )
            if isinstance(target, str) and ctx.on_route(target):
                continue  # an alias for the module itself
            out.setdefault(
                str(config),
                f"{rel} maps `{alias.key}` to {alias.value or 'code this cannot read'}, so a test may load "
                "another file in place of a module the flag's read goes through",
            )
    return out, aliases


#: What a test-runner configuration spells when it runs test files in one shared module registry.
_SHARED_REGISTRY = re.compile(r"\bisolate\b[\"']?\s*:\s*false\b|--no-isolate\b|\bbun\s+test\b")


def _isolated(repo_dir: str) -> bool:
    """Whether every test file runs in a module registry of its own (Jest,
    and Vitest unless ``isolate: false``), so a mock or a spy in one cannot
    reach another's reads. Mocha and Bun's runner share one, and so does a
    configuration this cannot read."""
    state = _state(repo_dir)
    if state.isolated is not None:
        return state.isolated
    state.isolated = _reads_isolated(state.configs_list())
    return state.isolated


def _reads_isolated(configs: list[Path]) -> bool:
    for path in configs:
        if wt._MOCHARC.match(path.name) or path.name == "bunfig.toml":
            return False
        if path.name != "package.json" and not (
            wt._CONFIG_NAME.match(path.name) or ts_mock_config._CONFIG_NAME.match(path.name)
            or ts_mock_config._VITE_NAME.match(path.name)
        ):
            continue
        text = _read(str(path))
        if text is None or _SHARED_REGISTRY.search(text):
            return False
        if path.name == "package.json" and re.search(r'"mocha"\s*:|\bmocha\b', text):
            return False
    return True


#: Loads of more than one module, or of one this cannot name.
_GLOB_LOADS = re.compile(r"import\.meta\.glob|require\.context")
_COMPUTED_LOAD = re.compile(r"""(?<![\w$.])(?:import|require)\s*\(\s*(?![\s'"]|`[^`$]*`)""")


class _Reach:
    """Whether a file's imports may reach a file holding the read, through
    files of the repository: a load this cannot resolve (that is not an npm
    package), a computed one, ``import.meta.glob`` and ``require.context``
    count as reaching. Answers are shared across the test files of a run."""

    def __init__(self, files: ts_scope.Files, repo_dir: str, targets: set[str]) -> None:
        self.files, self.repo_dir, self.targets = files, repo_dir, targets
        self.aliases: list[_Alias] = []
        self.known: dict[str, bool] = {}

    def reaches(self, start: str) -> bool:
        if start in self.known:
            return self.known[start]
        seen = {start}
        pending = [start]
        answer = False
        while pending and not answer:
            path = pending.pop()
            if path in self.targets or self.known.get(path):
                answer = True
                break
            if len(seen) > 4000:
                answer = True  # too far to follow: assume it does
                break
            for target in self._loads(path):
                if target is None:
                    answer = True
                    break
                if target not in seen and not self.known.get(target) is False:
                    seen.add(target)
                    pending.append(target)
        for path in seen if not answer else (start,):
            self.known[path] = answer
        return answer

    def _loads(self, path: str) -> list[str | None]:
        """What ``path`` loads, resolved (``None``: may load anything), kept
        per text for the run."""
        state = _state(self.repo_dir)
        text = state.texts.get(path)
        if text is None:
            text = _read(path)
        if text is None:
            return []
        kept = state.loads.get(path)
        if kept is not None and kept[0] == text:
            return list(kept[1])
        found = self._read_loads(path, text)
        state.loads[path] = (text, tuple(found))
        return found

    def _read_loads(self, path: str, text: str) -> list[str | None]:
        if _GLOB_LOADS.search(text) or _COMPUTED_LOAD.search(text):
            return [None]  # by text: one spelled in a comment only reaches more
        file = self.files.file(path, text) if path.endswith(ts_scope.FAMILY_EXTENSIONS) else None
        if file is not None:
            specs: list[str | None] = list(wt._loaded_specifiers(file.root))  # prose saying `from "x"` loads nothing
        else:
            specs = [m.group(2) for m in wt._LOADS.finditer(text) if not re.search(r"\s", m.group(2))]
        found: list[str | None] = []
        for spec in specs:
            if spec is None:
                found.append(None)
                continue
            target = _resolve(path, spec, self.repo_dir)
            if target is None:
                # A runner's alias (`"@": path.resolve(__dirname, "./src")`) reads it as a file.
                aliased = next((a for a in (_apply(alias, spec) for alias in _aliases_for(self.aliases, path)) if a is not None), None)
                if aliased is True:
                    found.append(None)
                    continue
                target = aliased
            if target is not None:
                found.append(str(Path(target).resolve()))
            elif not (
                spec.startswith("node:") or _is_package(spec, path, self.repo_dir)
                # `fs`, `path`: Node's own, unless a compiler config reads the name as a file.
                or (spec in wt._NODE_BUILTINS and cross_file_ts.names_package(path, spec, self.repo_dir))
            ):
                found.append(None)
        return found

def refusals(
    repo_dir: str,
    paths: list[str],
    flag_key: str,
    accessors: tuple[str, ...],
    resolution: cross_file_keys.Resolution,
    wrappers: frozenset[str] = frozenset(),
    treatment: bool | None = None,
) -> dict[str, str]:
    """``{path: reason}`` for each test, support, setup or runner config file
    that drives a client (or the key) a read of ``flag_key`` goes through
    (see the module docstring). ``wrappers`` is kept for callers; a wrapper is
    a declaration holding a read, and calling one is never a drive."""
    state = _checkout(repo_dir)
    state.refresh()
    # Every package question this asks, the route walk's included, is kept for
    # the run: the answers depend on files `refresh` watches, and it drops
    # them with the rest when one changes.
    with cross_file_ts.keeping_package_answers(state.packages):
        return _refusals(state, repo_dir, paths, flag_key, accessors, resolution, treatment)


def _refusals(
    state: _Checkout,
    repo_dir: str,
    paths: list[str],
    flag_key: str,
    accessors: tuple[str, ...],
    resolution: cross_file_keys.Resolution,
    treatment: bool | None,
) -> dict[str, str]:
    found = plan(repo_dir, paths, flag_key, accessors, resolution)
    route = found.route
    if not (route.modules or found.stems or route.globals or route.packages or found.keys or found.hooks):
        return {}
    read = (
        f"{os.path.relpath(found.site.path, repo_dir)}:{found.site.line}" if found.site is not None else "the flag's read"
    )
    ctx = _Context(
        repo_dir, route.modules, found.stems, found.keys, [],
        frozenset({rule_synthesis.plain_name(a) for a in accessors} | set(_BUILT_IN_READS)),
        found.readers, found.barrels, found.hooks,
        route.names | {n for names in found.keys.values() for n in names},
        None if treatment is None else ("true" if treatment else "false"),
    )
    out: dict[str, str] = {}
    if route.modules or found.stems or found.keys or found.hooks:
        configs, ctx.aliases = _config_refusals(ctx, _runner_configs(repo_dir))
        out.update(configs)
    if state.setups is False:
        state.setups = wt.setup_files(repo_dir)
    setups = state.setups
    files = state.files
    from flag_cleanup import source_index

    # The checkout's listing (ignored files left out, as every other scan here).
    every = [p for p in state.listing if p.endswith(ts_scope.FAMILY_EXTENSIONS)]
    closure = _setup_closure(every, files, setups, repo_dir) if route.packages else set()
    # A module only test code loads is test support; one holding a read is the code under test.
    # Every static load of a route module spells its last segment (or an
    # alias key) at the end of a string, and every global write its root: a
    # file spelling none of them is not read further, test or not. A setup
    # file of the closure is.
    specs = ctx.segments() | {_segment(s) for s in found.stems} | {s for s in found.stems if not s.startswith(".")}
    specs |= {a.key for a in ctx.aliases if a.key and not a.regex}
    words = {g for g, _ in route.globals}
    if any(g == "globalThis" for g, _ in route.globals):
        words |= wt._GLOBAL_OBJECTS | {"stubGlobal"}
    if any(g == "process" for g, _ in route.globals):
        words.add("stubEnv")
    if found.keys or route.modules or found.hooks:
        words.add("enableAutomock")
        words |= _REWIRE_API  # `rewire("./route")` spells the file holding the read, not a route module
    if any(a.regex for a in ctx.aliases):
        words |= {_segment(m) for m in (*route.modules, *found.keys, *found.hooks)}  # a mapper may hide the path
    patterns = []
    if specs:
        names = "|".join(re.escape(t) for t in sorted(specs, key=len, reverse=True))
        patterns.append(rf"""[/'"`](?:{names})(?:\.[cm]?[jt]sx?)?(?:/index(?:\.[cm]?[jt]sx?)?)?/?['"`]""")
    if words:
        patterns.append(r"(?<![\w$])(?:" + "|".join(re.escape(t) for t in sorted(words)) + r")(?![\w$])")
    mentions = re.compile("|".join(patterns)) if patterns else None

    isolated = setups is not None and _isolated(repo_dir)
    reach = _Reach(files, repo_dir, found.read_files)
    reach.aliases = ctx.aliases

    def rel(module: str) -> str:
        if module.startswith("?"):
            return module[1:].removeprefix("hook:")
        return os.path.relpath(module, repo_dir) if os.path.isabs(module) else module

    for path in every:
        source = state.texts.get(path)
        if source is None:
            continue
        resolved = path
        if resolved not in closure and (mentions is None or not mentions.search(source)):
            continue
        test = wt._stubs(path, source) or setups is None or wt._setup_file(path, setups) or resolved in closure
        if not test and (resolved in found.read_files or not _test_only(resolved, files, setups)):
            continue
        file = files.file(path, source)
        if file is None:
            continue
        api = _MockApi(file)
        reasons: list[tuple[int, str]] = []
        # A module mock, a spy or a binding use in a test file acts in that
        # file's own module registry. When the runner isolates test files, it
        # matters only to a file whose imports may reach the read.
        scoped = (
            isolated and wt._stubs(path, source) and not wt._setup_file(path, setups or frozenset())
            and resolved not in closure and not _test_only(resolved, files, setups)
        )
        if (route.modules or found.stems or found.keys or found.hooks) and not (
            scoped and not reach.reaches(resolved)
        ):
            for node, module in _module_mocks(file, ctx, api):
                line, text = _line(node, source)
                reasons.append((line, f"line {line} replaces {rel(module)} (`{text}`), a module the read at {read} goes through"))
            bound, loose = _bound_from_loads(file, ctx, api)
            for node, module in loose:
                if ctx.kind(module) != "client" and not _spied(node, api):
                    continue  # a key's or a hook's module, held or read: only a spy or a write changes it
                line, text = _line(node, source)
                reasons.append((
                    line,
                    f"line {line} loads {rel(module)} (`{text}`), a module the read at {read} goes through, "
                    "other than into a declaration",
                ))
            for node, module in _module_uses(file, bound, ctx, api):
                line, text = _line(node, source)
                what = "the flag's key" if ctx.kind(module) == "key" else "what it answers"
                reasons.append((
                    line,
                    f"line {line} uses `{_text(node)}` from {rel(module)} other than by calling a read (`{text}`); "
                    f"the read at {read} goes through that module, so the test may choose {what}",
                ))
        if route.modules or found.stems or found.keys or found.hooks:
            # A load that substitutes acts on the module it loads, wherever the test's imports go.
            for node, module in _substitutions(file, ctx, found.read_files, reach):
                line, text = _line(node, source)
                reasons.append((
                    line,
                    f"line {line} loads code under test with {rel(module)} or its variables replaced (`{text}`), "
                    f"and the read at {read} goes through it",
                ))
        if route.globals:
            for node in _global_writes(file, route.globals, api):
                line, text = _line(node, source)
                reasons.append((line, f"line {line} writes a global the read at {read} goes through (`{text}`)"))
        if resolved in closure:
            for node, package in _package_uses(file, route.packages, ctx.accessors, api):
                line, text = _line(node, source)
                reasons.append((
                    line, f"line {line} drives {package} for every test (`{text}`), and the read at {read} goes through it"
                ))
        if reasons:
            reasons.sort()
            out[path] = reasons[0][1] + (f" (and {len(reasons) - 1} more)" if len(reasons) > 1 else "")
    return out
