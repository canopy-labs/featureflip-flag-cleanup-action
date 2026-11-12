"""Bare specifiers that name a workspace package of the checkout.

A monorepo's packages import each other by name (``import { getFlags } from
"@acme/flags"``), and the package's manifest points at a build output that is
not committed (``"main": "./dist/index.js"``). The source is in the checkout;
this module finds it, or says it cannot. :func:`resolve` answers only when
every reading below is certain, so a caller that refused every package before
refuses exactly what is left.

The package is found by name among the members of the workspace the reader
belongs to: the nearest directory above the reader whose ``package.json`` has
``workspaces``, or that holds a ``pnpm-workspace.yaml`` or a ``lerna.json``
with ``packages``, its globs matched against every directory holding a
``package.json``. It must be the ONLY ``package.json`` of the checkout with
that name, and the reader must not be inside it.

Which copy runs is the package manager's to say, so it must say this one:

* a ``package.json`` from the reader's directory up to the workspace root
  declares the dependency, and every one that does declares it as
  ``workspace:`` or as a ``file:``/``link:``/``portal:`` path to the package.
  A version range means a copy from the registry may be installed instead;
* no ``overrides``, ``resolutions`` or ``pnpm.overrides`` of the checkout, and
  no ``pnpm-workspace.yaml``, names the package;
* every ``node_modules/<name>`` from the reader's directory up to the checkout
  root is a link to the package's directory;
* nothing a MODULE RESOLVER reads as its config (a bundler, a test runner,
  TypeScript, Node's loader, Babel or SWC, Deno, Bun, the package manager)
  names it. Formats no resolver reads that way may name it freely: JSON,
  YAML, TOML, docs, lockfiles, a formatter's, linter's, editor's, CI's or
  Nx's config, ``package.json`` ``scripts``. What is checked:

  - a closed list of resolver configs (:data:`_RESOLVER_CONFIGS`: every
    ``tsconfig``/``jsconfig``, ``deno.json``, an import map, ``.babelrc``,
    ``babel.config.json``, ``.swcrc``, ``jest.config.json``,
    ``bunfig.toml``, ``.yarnrc.yml``) refuses on the name, its escaped
    regex spelling (``@acme\\/flags``), its scope followed by a pattern
    (``^@acme/(.*)$``) or a name-free mapper (Babel's ``module-resolver``,
    Jest's ``moduleNameMapper``, ``modulePaths``, ``moduleDirectories``);
  - so do a ``package.json``'s resolution keys (``overrides``,
    ``resolutions``, ``jest``, ``_moduleAliases``, ``browser``, ``pnpm.*``
    but its build and release-age settings, ...) and a
    ``pnpm-workspace.yaml``'s settings other than ``packages`` and those;
  - a script a resolver can run (:mod:`flag_cleanup.ts_resolver_files`:
    capability flows from the resolver engines it, or a script loading it,
    imports, from a tool loading it by name, and from a path naming it in a
    capable script or on a Node command line) refuses on the same; the name
    may stand there only as a module specifier or in a comment. Any other
    script may name it anywhere. An alias map such a script computes at build
    time is not inspected (an accepted residual: see the README).

And the compiler must read the specifier only as a package: no
``tsconfig*.json``/``jsconfig*.json`` from the reader's directory up to the
checkout root has a ``paths`` pattern that fits it or a ``baseUrl`` file that
answers it, and none of them, nor anything they extend, is unreadable. A
config's ``references`` are read past here: they say which projects to build,
not how a name resolves.

The entry is the file every field a tool may read leads to: the ``exports``
entry for the subpath (every condition), and for the package itself
``main``, ``module``, ``types``, ``typings``, ``source``, ``browser`` and
``react-native``. A field that names a source file of the checkout leads to
it. One that names a file that is not there leads to the source the
package's own ``tsconfig*.json`` compiles it from: under its ``outDir`` (or
``declarationDir`` for a declaration file), mapped back under its explicit
``rootDir``. Every config of the package that emits there must agree, and a
config that emits without a ``rootDir`` proves nothing. All the fields must
lead to the same file; a subpath must be listed in ``exports`` exactly, and
a resolver that ignores ``exports`` must find no other file at the subpath
(or, for the package itself without ``main``, at its ``index``). Anything
else -- a pattern export, a ``null`` target, a field that is an object, a
committed build output, a package config that cannot be read -- is not
resolved. The manifest itself is a WHITELIST too: a top-level field outside
:data:`_INERT_FIELDS` may name another entry some tool reads (``svelte``,
``jsnext:main``, ``typesVersions``, a tool's own block), so it refuses. And
the ``outDir`` mapping is only trusted when ``tsc`` is the package's build: a
bundler config in the package's directory (``vite.config.ts``,
``tsup.config.ts``, ``webpack.common.js``, ...) or a bundler named in its
``scripts`` or Nx ``project.json`` may build ``dist/index.js`` from any
source, so then only a field naming a source file itself counts.

:func:`reach` is the other direction, for the checks that may only
over-attribute a specifier to a file: a specifier naming a package of the
checkout reaches its proven entry, or, when that cannot be proven (a deep
import, a pattern export, an unreadable manifest), every file under the
package's directory. :func:`holding` is what the candidate scans look for:
the name of every package whose directory holds a module.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

#: Whether the run follows workspace packages (the ``workspace-packages``
#: input is ``follow``); see :func:`following`.
_follow: ContextVar[bool] = ContextVar("ts_workspace_follow", default=False)


@contextmanager
def following(enabled: bool) -> Iterator[None]:
    """Follow workspace packages (:func:`resolve`) for the scope, or not.

    Off (the default) :func:`resolve` answers nothing, so every bare specifier
    naming a workspace package stays ``import-unresolved`` as it always was.
    The checks that may only over-attribute a specifier to a file
    (:func:`reach`, :func:`holding`) work either way: they only add escapes."""
    token = _follow.set(enabled)
    try:
        yield
    finally:
        _follow.reset(token)

#: The dependency fields a package manager installs from.
_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
#: The manifest fields some tool reads as the package's entry.
_ENTRY_FIELDS = ("main", "module", "types", "typings", "source", "browser", "react-native")
#: The manifest fields known not to name an entry or change resolution.
#: Any other top-level field refuses the package's entry (see the module
#: docstring).
_INERT_FIELDS = frozenset(
    {
        "name", "version", "private", "description", "license", "licenses", "author", "contributors",
        "maintainers", "repository", "homepage", "bugs", "funding", "keywords", "scripts",
        "dependencies", "devDependencies", "peerDependencies", "peerDependenciesMeta",
        "optionalDependencies", "bundledDependencies", "bundleDependencies", "dependenciesMeta",
        "engines", "os", "cpu", "files", "sideEffects", "publishConfig", "workspaces",
        "packageManager", "type", "exports", "nx", "eslintConfig", "prettier", "browserslist", "lint-staged",
        "$schema", "config", "husky", "commitlint", "stylelint", "jscpd", "c8", "nyc", "volta", "typeCoverage",
        "gitHead", "readme", "directories", "man", *_ENTRY_FIELDS,
    }
)
#: The directories no package of the checkout lives in.
_SKIP_DIRS = frozenset({"node_modules", ".git"})
#: A bundler's config file in a package's directory, by name.
_BUNDLER_CONFIG = re.compile(
    r"^(?:(?:vite|tsup|rollup|rolldown|webpack|esbuild|parcel|microbundle|unbuild|build|rspack|rsbuild|tsdown"
    r"|snowpack|wmr|bunfig)(?:\.[\w-]+)*\.(?:[cm]?[jt]sx?|json|toml)|\.parcelrc)$",
    re.IGNORECASE,
)
#: Files that mention a package without being able to map it.
_LOCKFILES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"}
)
_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc")
#: A module mapper that can redirect a name without spelling it: Babel's
#: module resolver, Jest's mapper and search paths.
_MAPPERS = re.compile(rb"module[-_]resolver|moduleNameMapper|modulePaths|moduleDirectories")
#: Member calls whose first argument is a module specifier (test runners).
_LOADER_MEMBERS = frozenset(
    {
        "resolve", "mock", "doMock", "unmock", "dontMock", "setMock", "requireActual", "requireMock",
        "importActual", "importMock", "createMockFromModule",
    }
)
_ANY_CONFIG = re.compile(r"^[tj]sconfig.*\.jsonc?$", re.IGNORECASE)
_SCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_DECLARATION_SUFFIXES = {".d.ts": ".js", ".d.mts": ".mjs", ".d.cts": ".cjs"}


# Something this module would need to read cannot be read for certain: the
# resolver's own exception, so the helpers it lends raise the same one.
from flag_cleanup.cross_file_ts import _Unknown, probable_path  # noqa: E402


@dataclass
class _Checkout:
    """Every manifest and tool config of a checkout, read once per run."""

    #: Directory -> parsed ``package.json``; None when it does not parse.
    manifests: dict[Path, object] = field(default_factory=dict)
    #: Directory -> raw ``package.json`` text.
    texts: dict[Path, str] = field(default_factory=dict)
    #: Workspace root -> its member globs as (include, exclude) regexes; None
    #: when they cannot be read.
    roots: dict[Path, tuple[list[re.Pattern[str]], list[re.Pattern[str]]] | None] = field(default_factory=dict)
    #: Text of every ``pnpm-workspace.yaml``.
    pnpm: list[str] = field(default_factory=list)
    #: Every file of the checkout outside :data:`_SKIP_DIRS`.
    files: list[Path] = field(default_factory=list)
    #: Package name -> the first file that mentions it outside the whitelist
    #: (``""`` when none does), worked out on first need.
    mentioned: dict[str, str] = field(default_factory=dict)
    #: The checkout root.
    stop: Path | None = None
    #: Name -> package directories a dependency installs under that name
    #: other than its own (``"flags": "file:../flags"``,
    #: ``"legacy": "workspace:@acme/flags@*"``, ``npm:@acme/flags@1``).
    aliases: dict[str, set[Path]] = field(default_factory=dict)
    #: The resolver-capable scripts (:mod:`flag_cleanup.ts_resolver_files`).
    capable: frozenset[Path] | None = None
    #: Those capable on their own (rules 1, 2, 4, 5), not only as imported.
    primary: set[Path] = field(default_factory=set)
    #: Primary script naming a mapper -> the keys of its literal Jest
    #: ``moduleNameMapper`` maps, compiled, or None when they cannot be read
    #: (:func:`_mapper_keys`); worked out on first need.
    mapper_keys: dict[Path, tuple[re.Pattern[str], ...] | None] | None = None
    #: The first resolver-capable script that could redirect a deep subpath
    #: without naming the package (:func:`_deep_redirect`); ``""`` for none,
    #: None until worked out.
    deep_redirect: str | None = None


def _memo() -> dict | None:
    """Manifests, configs and file texts: the run writes none of them but
    source files, and deleting code can only drop a mention the whitelist
    refused, never add one, so a run-long answer only over-refuses."""
    from flag_cleanup import source_index

    return source_index.run_memo("ts_workspace")


def _existence_memo() -> dict | None:
    """What depends on which files exist (an entry's mapping): dropped when the
    transform deletes a file (:func:`flag_cleanup.source_index.transform_memo`)."""
    from flag_cleanup import source_index

    return source_index.transform_memo("ts_workspace_entries")


def _checkout(stop: Path) -> _Checkout:
    memo = _memo()
    if memo is not None and stop in memo:
        return memo[stop]
    found = _Checkout(stop=stop)
    for directory, dirnames, filenames in os.walk(stop):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        here = Path(directory)
        names = set(filenames)
        found.files.extend(here / name for name in sorted(filenames))
        if "package.json" in names:
            text = _read(here / "package.json")
            found.texts[here] = text or ""
            try:
                found.manifests[here] = json.loads(text) if text is not None else None
            except ValueError:
                found.manifests[here] = None
        globs: list[str] | None = []
        is_root = False
        manifest = found.manifests.get(here)
        if isinstance(manifest, dict) and "workspaces" in manifest:
            is_root = True
            globs = _listed(manifest["workspaces"], globs)
        if "pnpm-workspace.yaml" in names:
            is_root = True
            text = _read(here / "pnpm-workspace.yaml")
            found.pnpm.append(text or "")
            globs = _pnpm_globs(text, globs)
        if "lerna.json" in names:
            text = _read(here / "lerna.json")
            try:
                lerna = json.loads(text) if text is not None else None
            except ValueError:
                lerna = None
            if not isinstance(lerna, dict):
                is_root, globs = True, None
            elif "packages" in lerna:
                is_root = True
                globs = _listed(lerna["packages"], globs)
        if is_root:
            found.roots[here] = _matchers(globs)
    _alias_dependencies(found)
    if memo is not None:
        memo[stop] = found
    return found


def _alias_dependencies(checkout: _Checkout) -> None:
    """Fill :attr:`_Checkout.aliases`: every dependency key whose range
    installs a package of the checkout (by path, or by ``workspace:`` or
    ``npm:`` naming it), under a key other than the package's name."""
    by_name: dict[str, list[Path]] = {}
    for directory, manifest in checkout.manifests.items():
        if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
            by_name.setdefault(manifest["name"], []).append(directory)
    for directory, manifest in checkout.manifests.items():
        if not isinstance(manifest, dict):
            continue
        pnpm = manifest.get("pnpm") if isinstance(manifest.get("pnpm"), dict) else {}
        for block in (
            *(manifest.get(f) for f in _DEPENDENCY_FIELDS),
            manifest.get("overrides"),
            manifest.get("resolutions"),
            pnpm.get("overrides"),
        ):
            if not isinstance(block, dict):
                continue
            for key, value in block.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                key = key.rsplit(">", 1)[-1].lstrip("*/")  # `a>b`, `**/b`
                targets: list[Path] = []
                for protocol in ("file:", "link:", "portal:", "workspace:"):
                    if value.startswith(protocol):
                        rest = value[len(protocol) :]
                        if rest.startswith((".", "/", "~")):
                            target = Path(os.path.normpath(directory / rest))
                            targets += [d for d in checkout.manifests if _same(target, d)]
                        elif protocol == "workspace:" and re.match(r"@?[^@\s]+@", rest):
                            targets += by_name.get(_without_range(rest), [])
                if value.startswith("npm:"):
                    targets += by_name.get(_without_range(value[4:]), [])
                for target in targets:
                    own = checkout.manifests.get(target)
                    if not (isinstance(own, dict) and own.get("name") == key):
                        checkout.aliases.setdefault(key, set()).add(target)


def _without_range(spec: str) -> str:
    """``@a/b`` of ``@a/b@^1``, ``x`` of ``x@1`` and of ``x``."""
    at = spec.find("@", 1)
    return spec if at < 0 else spec[:at]


def _names_of(directory: Path, checkout: _Checkout) -> set[str]:
    """Every name a bare specifier can load ``directory``'s package by."""
    names: set[str] = set()
    manifest = checkout.manifests.get(directory)
    if isinstance(manifest, dict):
        if isinstance(manifest.get("name"), str):
            names.add(manifest["name"])
    else:
        names.update(re.findall(r'"name"\s*:\s*"([^"\n]+)"', checkout.texts.get(directory, "")))
    names.update(key for key, targets in checkout.aliases.items() if directory in targets)
    return names


def _reachable(checkout: _Checkout, name: str) -> list[Path]:
    """The package directories a bare specifier of package part ``name`` may
    load: the ones of that name, and the ones a dependency aliases to it."""
    return sorted(set(_named(checkout, name)) | checkout.aliases.get(name, set()))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _listed(value: object, globs: list[str] | None) -> list[str] | None:
    """``globs`` with a manifest's ``workspaces`` (a list, or an object's
    ``packages`` list) added; None when either cannot be read."""
    if globs is None:
        return None
    if isinstance(value, dict):
        value = value.get("packages", [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return globs + value


def _pnpm_globs(text: str | None, globs: list[str] | None) -> list[str] | None:
    """``globs`` with a ``pnpm-workspace.yaml``'s ``packages`` added."""
    if text is None or globs is None:
        return None
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if data is None:
        return globs
    if not isinstance(data, dict):
        return None
    return _listed(data.get("packages", []), globs)


def _matchers(globs: list[str] | None) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]] | None:
    if globs is None:
        return None
    include: list[re.Pattern[str]] = []
    exclude: list[re.Pattern[str]] = []
    for glob in globs:
        negated = glob.startswith("!")
        pattern = _glob(glob[1:] if negated else glob)
        if pattern is None:
            return None
        (exclude if negated else include).append(pattern)
    return include, exclude


def _glob(glob: str) -> re.Pattern[str] | None:
    """A workspace glob as a regex over a ``/``-separated relative path; None
    for one with syntax read here no further than ``*``, ``**`` and ``?``."""
    glob = glob.strip()
    while glob.startswith("./"):
        glob = glob[2:]
    glob = glob.rstrip("/")
    if not glob or glob.startswith("/") or any(ch in glob for ch in "{}[]()!+@\\"):
        return None
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _package_name(specifier: str) -> tuple[str, str] | None:
    """``(name, subpath)`` of a bare specifier: ``("@a/b", "./c")`` for
    ``@a/b/c``, ``("x", ".")`` for ``x``; None for anything else."""
    if not specifier or "\n" in specifier or "\\" in specifier or specifier.startswith((".", "/", "#")):
        return None
    if ":" in specifier:
        return None  # `node:fs`, a URL, a loader prefix
    parts = specifier.split("/")
    size = 2 if specifier.startswith("@") else 1
    if len(parts) < size or not all(parts[:size]) or (size == 2 and len(parts[0]) < 2):
        return None
    rest = parts[size:]
    if any(p in ("", ".", "..") for p in rest):
        return None
    return "/".join(parts[:size]), "/".join([".", *rest]) if rest else "."


def holding(module: str, repo_dir: str) -> set[str]:
    """The name of every ``package.json`` of the checkout whose directory
    holds ``module``: a bare specifier that may reach it starts with one
    (:func:`reach`). A manifest that does not parse counts by every quoted
    ``"name"`` value in its text."""
    from flag_cleanup import source_index

    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    checkout = _checkout(stop)
    target = Path(module).resolve()
    names: set[str] = set()
    for directory in checkout.manifests:
        if not target.is_relative_to(directory.resolve()) or _unreachable_root(directory, checkout):
            continue
        names |= _names_of(directory, checkout)
    return names


@dataclass(frozen=True)
class Reach:
    """Where a bare specifier naming a package of the checkout can lead."""

    #: The one file it is proven to load (:func:`entry`); None when that
    #: cannot be proven, and then it may load any file under :attr:`directories`.
    entry: str | None
    #: The directory of every ``package.json`` named for its package.
    directories: tuple[Path, ...]
    #: For a subpath into packages with no ``exports`` (nor a ``browser``
    #: map), the files the path itself names (``pkg/sub`` → ``sub.js``,
    #: ``sub.ts``); None when that cannot be listed, and then every file.
    files: tuple[str, ...] | None = None

    def reaches(self, module: str) -> bool:
        """Whether the specifier may load ``module``."""
        if self.entry is not None:
            return str(Path(module).resolve()) == self.entry
        if self.files is not None:
            return str(Path(module).resolve()) in self.files
        target = Path(module).resolve()
        return any(target.is_relative_to(d.resolve()) for d in self.directories)


def package_part(text: str) -> str | None:
    """The package name a string starting a bare specifier names, read
    loosely: ``@a/b`` of ``@a/b/../c?raw``, of ``@a/b/`` and of the text
    before a template's first substitution. None when it cannot be one."""
    if not text or text.startswith((".", "/", "#")) or "\n" in text:
        return None
    head = re.split(r"[?#]", text, maxsplit=1)[0]
    if ":" in head:
        return None
    parts = head.split("/")
    if head.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 and len(parts[0]) > 1 and parts[1] else None
    return parts[0] or None


def reach(text: str, repo_dir: str) -> Reach | None:
    """Where ``text``, a specifier or the start of one, can lead when it names
    a package of the checkout; None when it names none. For the checks that
    may only over-attribute a specifier to a file."""
    from flag_cleanup import source_index

    name = package_part(text)
    if name is None:
        return None
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    checkout = _checkout(stop)
    directories = tuple(d for d in _reachable(checkout, name) if not _unreachable_root(d, checkout))
    if not directories:
        return None
    # An aliased name is never proven: only the package's own name is. A
    # query or fragment (`?raw`, `#x`) loads the file the rest names.
    head = re.split(r"[?#]", text, maxsplit=1)[0]
    proven = entry(head, repo_dir) if _package_name(head) is not None and _named(checkout, name) else None
    return Reach(proven, directories, None if proven is not None else _legacy_files(head, name, directories, checkout))


def _legacy_files(head: str, name: str, directories: tuple[Path, ...], checkout: _Checkout) -> tuple[str, ...] | None:
    """The files a subpath names in packages that leave it to the path
    (no ``exports``, no ``browser`` map): what Node, TypeScript and a bundler
    all look up then. None when a package does map subpaths, when the text
    is not a plain subpath, or when no such file exists (a build output)."""
    from flag_cleanup import ts_resolver_files

    rest = head[len(name) :]
    if not rest.startswith("/") or "*" in rest or len(rest) < 2 or ".." in rest.split("/"):
        return None
    found: list[str] = []
    for directory in directories:
        manifest = checkout.manifests.get(directory)
        if not isinstance(manifest, dict) or "exports" in manifest or isinstance(manifest.get("browser"), dict):
            return None
        hits = ts_resolver_files._files_named(Path(os.path.normpath(directory / rest.lstrip("/"))))
        if not hits:
            return None
        found += [str(h.resolve()) for h in hits]
    return tuple(found) or None


def _unreachable_root(directory: Path, checkout: _Checkout) -> bool:
    """Whether ``directory`` is a workspace root no bare specifier can load:
    a package manager never links the root under its own name unless a
    manifest depends on it, and Node's self-reference needs ``exports``.
    (A root's name is often a common word, ``root`` or ``monorepo``, and
    every string spelling it would otherwise reach every file.)"""
    manifest = checkout.manifests.get(directory)
    if directory not in checkout.roots or not isinstance(manifest, dict):
        return False
    for outer, globs in checkout.roots.items():
        # A nested root is an outer workspace's member: linked by its name.
        if outer != directory and directory.is_relative_to(outer) and (globs is None or _member(directory, outer, globs)):
            return False
    name = manifest.get("name")
    if "exports" in manifest or not isinstance(name, str):
        return False
    if any(directory in targets for targets in checkout.aliases.values()):
        return False
    for other in checkout.manifests.values():
        if not isinstance(other, dict):
            return False
        for field_name in _DEPENDENCY_FIELDS:
            wanted = other.get(field_name)
            if not isinstance(wanted, dict) or name in wanted:
                if wanted is not None:
                    return False
    return True


def bundled(specifier: str, repo_dir: str) -> bool:
    """Whether bare ``specifier`` names a package of the checkout that a
    bundler may build (:func:`_bundled`, unless nothing builds it at all:
    :func:`_unbuilt`), or whose manifest cannot be read: what it loads at
    run time may then do more than its source shows."""
    from flag_cleanup import source_index

    name = package_part(specifier) if probable_path(specifier) else None
    if name is None:
        return False
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    checkout = _checkout(stop)
    for directory in _named(checkout, name):
        manifest = checkout.manifests.get(directory)
        try:
            if not isinstance(manifest, dict):
                return True
            # With no build at all, the TypeScript source the specifier names
            # is what loads: no bundler writes `.ts`, so it cannot be one's
            # output. A JavaScript entry may be, whatever build it is that
            # this reading cannot see (a root script, a task runner's file).
            if _bundled(directory, manifest, stop) and not (
                _unbuilt(directory, manifest, stop) and _typescript_source(entry(specifier, repo_dir))
            ):
                return True
        except OSError:
            return True
    return False


def named(specifier: str, repo_dir: str) -> list[Path]:
    """The directories of the checkout's ``package.json`` files named for
    bare ``specifier``'s package, whatever else holds."""
    from flag_cleanup import source_index

    parsed = _package_name(specifier)
    if parsed is None:
        return []
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    checkout = _checkout(stop)
    return _named(checkout, parsed[0])


def _named(checkout: _Checkout, name: str) -> list[Path]:
    """Every directory whose ``package.json`` is (or may be) named ``name``."""
    return sorted(
        d
        for d, m in checkout.manifests.items()
        if (isinstance(m, dict) and m.get("name") == name) or (m is None and f'"{name}"' in checkout.texts.get(d, ""))
    )


def resolve(reader_path: str, specifier: str, stop: Path) -> str | None:
    """The source file bare ``specifier`` reaches from ``reader_path`` through
    a workspace package of the checkout rooted at ``stop``; None unless every
    reading the module docstring lists is certain."""
    if not _follow.get() or not probable_path(specifier):
        return None  # `workspace-packages: off`, or a name no file has
    parsed = _package_name(specifier)
    if parsed is None:
        return None
    memo = _existence_memo()
    # Every check reads from the reader's directory up, so the directory is the key.
    key = ("resolve", str(Path(reader_path).parent), specifier, stop)
    if memo is not None and key in memo:
        return memo[key]
    found = _resolve(reader_path, parsed, specifier, stop)
    if memo is not None:
        memo[key] = found
    return found


def _resolve(reader_path: str, parsed: tuple[str, str], specifier: str, stop: Path) -> str | None:
    name, subpath = parsed
    try:
        if not _named(_checkout(stop), name):
            return None  # the common case: a package from the registry
        package = _package(Path(reader_path).resolve(), name, stop)
        if package is None:
            return None
        directory, manifest = package
        if _mapped(specifier, _checkout(stop)):
            return None  # a test runner sends it elsewhere
        if subpath != "." and _deep_redirect(_checkout(stop)):
            return None  # a pattern may match the subpath alone
        if not _compiler_reads_package(Path(reader_path), specifier, stop):
            return None
        return _entry(directory, manifest, subpath, stop)
    except (_Unknown, OSError, UnicodeDecodeError, ValueError, RuntimeError):
        # RuntimeError: a symlink loop, from `Path.resolve`.
        return None


def entry(specifier: str, repo_dir: str) -> str | None:
    """The entry :func:`resolve` would find for bare ``specifier``, when a
    workspace package of that name exists, without asking whether a given
    reader reaches it. For the checks that may only over-attribute a
    specifier to a file."""
    from flag_cleanup import source_index

    parsed = _package_name(specifier)
    if parsed is None:
        return None
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    memo = _existence_memo()
    key = ("entry", specifier, stop)
    if memo is not None and key in memo:
        return memo[key]
    checkout = _checkout(stop)
    found = _named(checkout, parsed[0])
    result = None
    if len(found) == 1 and found[0] not in checkout.roots and isinstance(checkout.manifests.get(found[0]), dict):
        try:
            result = _entry(found[0], checkout.manifests[found[0]], parsed[1], stop)
        except (_Unknown, OSError, UnicodeDecodeError, ValueError, RuntimeError):
            result = None
    if memo is not None:
        memo[key] = result
    return result


def _package(reader: Path, name: str, stop: Path) -> tuple[Path, dict] | None:
    """The one workspace package named ``name`` the package manager links for
    ``reader``, with its manifest; None when that is not certain."""
    checkout = _checkout(stop)
    found = _named(checkout, name)
    if len(found) != 1 or not isinstance(checkout.manifests[found[0]], dict) or found[0] in checkout.roots:
        return None
    directory = found[0]
    if reader.is_relative_to(directory.resolve()):
        return None  # a self-reference goes through `exports` alone
    # The workspace the reader is in: the nearest root above it.
    root = None
    current = reader.parent
    while True:
        if current in checkout.roots:
            root = current
            break
        if current == stop or current.parent == current:
            break
        current = current.parent
    if root is None or checkout.roots[root] is None or not _member(directory, root, checkout.roots[root]):
        return None
    if not _declared_here(reader, name, directory, root, checkout):
        return None
    if (
        _overridden(name, checkout)
        or any(_mentioned(alias, checkout) for alias in sorted(_names_of(directory, checkout)))
        or not _linked(reader, name, directory, stop)
    ):
        return None
    return directory, checkout.manifests[directory]


def _member(directory: Path, root: Path, globs) -> bool:
    if directory == root or not directory.is_relative_to(root):
        return False
    relative = directory.relative_to(root).as_posix()
    include, exclude = globs
    return any(p.match(relative) for p in include) and not any(p.match(relative) for p in exclude)


def _declared_here(reader: Path, name: str, directory: Path, root: Path, checkout: _Checkout) -> bool:
    """Whether a manifest from the reader's directory up to ``root`` declares
    ``name``, and every one that does names the workspace copy."""
    declared = False
    current = reader.parent
    while True:
        if current in checkout.manifests:
            manifest = checkout.manifests[current]
            if not isinstance(manifest, dict):
                return False
            for field_name in _DEPENDENCY_FIELDS:
                wanted = manifest.get(field_name)
                if wanted is None:
                    continue
                if not isinstance(wanted, dict):
                    return False
                if name in wanted:
                    if not _workspace_range(wanted[name], current, directory):
                        return False
                    declared = True
        if current == root or current.parent == current:
            return declared
        current = current.parent


def _workspace_range(value: object, manifest_dir: Path, directory: Path) -> bool:
    """Whether a dependency's range can only be the workspace copy."""
    if not isinstance(value, str):
        return False
    if value.startswith("workspace:"):
        rest = value[len("workspace:") :]
        if re.fullmatch(r"[*^~]|[\^~]?\d[\w.+-]*", rest):
            return True
        return rest.startswith((".", "/")) and _same(manifest_dir / rest, directory)
    for protocol in ("file:", "link:", "portal:"):
        if value.startswith(protocol):
            return _same(manifest_dir / value[len(protocol) :], directory)
    return False


def _same(path: Path, directory: Path) -> bool:
    try:
        return Path(os.path.normpath(path)).resolve() == directory.resolve()
    except (OSError, RuntimeError):
        return False


def _overridden(name: str, checkout: _Checkout) -> bool:
    """Whether an override of the checkout, or a pnpm workspace file, names
    ``name``: either can make the package manager install another copy."""

    def mentions(node: object) -> bool:
        if isinstance(node, dict):
            # Any key spelling the name: `name`, `name@1`, `**/name`, `a>name`.
            return any(name in key or mentions(value) for key, value in node.items())
        if isinstance(node, list):
            return any(mentions(item) for item in node)
        return False

    for manifest in checkout.manifests.values():
        if not isinstance(manifest, dict):
            continue
        pnpm = manifest.get("pnpm")
        for node in (
            manifest.get("overrides"),
            manifest.get("resolutions"),
            pnpm.get("overrides") if isinstance(pnpm, dict) else pnpm,
        ):
            if mentions(node):
                return True
    return False  # `pnpm-workspace.yaml` settings are read by `_pnpm_redirects`


def _mentioned(name: str, checkout: _Checkout) -> str:
    """The first file of the checkout where a module resolver could read
    ``name`` as resolution config (see the module docstring), or ``""``."""
    if name in checkout.mentioned:
        return checkout.mentioned[name]
    # The name, or a regex spelling of it with its `/` escaped once or, inside
    # a string handed to `new RegExp`, twice (`@acme\\/flags`).
    slash = r"(?:/|\\{1,2}/)"
    spelled = re.compile(
        rf"(?<![\w@/.\\-]){slash.join(re.escape(part) for part in name.split('/'))}(?![\w-])".encode("utf-8")
    )
    # A pattern over the scope (`^@acme/(.*)$`, `@acme/*`, `@acme\/.+`) can
    # match the name without spelling it.
    scope = name.split("/", 1)[0] if name.startswith("@") else None
    patterned = (
        re.compile(rf"(?<![\w@/.\\-]){re.escape(scope)}{slash}[(\[.*^$?+{{|\\]".encode("utf-8"))
        if scope
        else None
    )
    # The scope alone as a string (`"@acme/"`, `"@acme"`): an inline plugin's
    # `id.startsWith("@acme/")` or `id.split("/")[0] === "@acme"`. Judged only
    # in a resolver's own config, where any such string may be its test.
    bare = re.compile(rf"(?<=[\"'`]){re.escape(scope)}(?:{slash})?(?=[\"'`])".encode("utf-8")) if scope else None

    def redirects(data: bytes) -> bool:
        """Spelled, or matched by a pattern or a name-free mapper."""
        return bool(
            spelled.search(data) or _MAPPERS.search(data) or (patterned is not None and patterned.search(data))
        )

    found = ""
    for path in checkout.files:
        kind = _resolver_kind(path)
        if kind is None or not path.is_file():
            continue  # no resolver reads it; or a dangling link, which holds nothing
        try:
            data = path.read_bytes()
        except OSError:
            found = str(path)
            break
        if kind == "manifest":
            blocked = _manifest_redirects(data, redirects)
        elif kind == "pnpm":
            blocked = _pnpm_redirects(data, redirects)
        elif kind == "config":
            blocked = redirects(data)
        else:  # a script: resolver config only when a resolver can run it
            if path.resolve() not in _capable(checkout):
                continue
            hits = [m.span() for m in spelled.finditer(data)]
            patterns = [m.span() for m in patterned.finditer(data)] if patterned is not None else []
            mappers = [m.span() for m in _MAPPERS.finditer(data)]
            # Comments never run, so nothing in one can redirect a specifier.
            if path.resolve() in checkout.primary:
                if bare is not None:
                    patterns += [m.span() for m in bare.finditer(data)]
                mentioned = bool(hits) and not _specifiers_only(path, data, hits)
                mentioned = mentioned or (bool(patterns) and _outside_comments(path, data, patterns))
            else:
                # Capable only as something a resolver's script imports: there
                # the name, or a pattern over its scope, redirects only where
                # it stands like a redirect.
                mentioned = bool(hits + patterns) and _redirect_shaped(path, data, hits + patterns)
            if mappers and _mapper_keys(checkout).get(path) is not None:
                mappers = []  # a literal Jest map: judged per specifier (`_mapped`)
            blocked = mentioned or (bool(mappers) and _outside_comments(path, data, mappers))
            # An alias map computed at build time (a variable, a spread of an
            # incoming config, `Object.fromEntries`) is not inspected: see the
            # README's "Workspace packages" for the residual this accepts.
        if blocked:
            found = str(path)
            break
    checkout.mentioned[name] = found
    return found


#: The non-executable files a module resolver reads as its config.
_RESOLVER_CONFIGS = re.compile(
    r"^(?:[tj]sconfig.*\.jsonc?|deno\.jsonc?|import_map\.json|.*\.importmap.*|\.babelrc(?:\.json)?"
    r"|babel\.config\.json|\.swcrc|jest\.config\.json|bunfig\.toml|\.yarnrc\.yml|metro\.config\.json"
    r"|snowpack\.config\.json|\.parcelrc)$",
    re.IGNORECASE,
)
_SCRIPTS = _SCRIPT_SUFFIXES + (".vue", ".svelte", ".astro")


def _resolver_kind(path: Path) -> str | None:
    """How a module resolver may read ``path``: ``"manifest"``, ``"pnpm"``,
    ``"config"`` (a closed list of resolver configs), ``"script"`` (code,
    which counts when a resolver can run it: :func:`_capable`), or None for a file none reads
    as resolution config (JSON, YAML, TOML, docs, lockfiles, a formatter's,
    linter's, editor's, CI's or Nx's config)."""
    name = path.name
    if name in _LOCKFILES or name.lower().endswith(_DOC_SUFFIXES):
        return None
    if name == "package.json":
        return "manifest"
    if name == "pnpm-workspace.yaml":
        return "pnpm"
    if _RESOLVER_CONFIGS.match(name):
        return "config"
    if name.endswith(_SCRIPTS):
        return "script"
    return None


def _capable(checkout: _Checkout) -> frozenset[Path]:
    """The checkout's resolver-capable scripts, worked out once per run."""
    from flag_cleanup import ts_resolver_files

    if checkout.capable is None:
        # A tsconfig's strings name what tsc compiles (`include`, `rootDir`),
        # not code a resolver runs; only a compiler plugin there is one.
        configs = [
            f
            for f in checkout.files
            if _RESOLVER_CONFIGS.match(f.name) and (not _ANY_CONFIG.match(f.name) or b'"plugins"' in _bytes(f))
        ]
        checkout.capable = ts_resolver_files.capable(checkout.files, checkout.stop, configs, primaries=checkout.primary)
    return checkout.capable


def _bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b'"plugins"'  # unreadable: assume the worst


#: ``package.json`` keys a resolver or package manager reads to redirect a name.
_REDIRECTING_FIELDS = (
    "overrides", "resolutions", "patchedDependencies", "packageExtensions", "catalog", "catalogs",
    "jest", "babel", "_moduleAliases", "alias", "browser", "react-native", "imports", "metro", "snowpack",
)
#: pnpm settings that do not change what a name resolves to.
_INERT_PNPM = frozenset(
    {
        "packages", "minimumReleaseAge", "minimumReleaseAgeExclude", "minimumReleaseAgeStrict",
        "onlyBuiltDependencies", "neverBuiltDependencies", "ignoredBuiltDependencies",
    }
)


def _manifest_redirects(data: bytes, redirects) -> bool:
    """Whether a ``package.json``'s resolution-affecting keys (``overrides``,
    ``jest``, ``_moduleAliases``, ``pnpm.*`` but the build and release-age
    settings, ...) name the package, or it cannot be read."""
    try:
        manifest = json.loads(data)
    except ValueError:
        return True
    if not isinstance(manifest, dict):
        return True
    picked = {k: manifest[k] for k in _REDIRECTING_FIELDS if k in manifest}
    pnpm = manifest.get("pnpm")
    if pnpm is not None:
        picked["pnpm"] = (
            {k: v for k, v in pnpm.items() if k not in _INERT_PNPM} if isinstance(pnpm, dict) else pnpm
        )
    return bool(picked) and redirects(json.dumps(picked).encode("utf-8"))


def _pnpm_redirects(data: bytes, redirects) -> bool:
    """Whether a ``pnpm-workspace.yaml`` names the package in a setting that
    changes resolution (anything but :data:`_INERT_PNPM`), or cannot be read."""
    import yaml

    try:
        settings = yaml.safe_load(data)
    except yaml.YAMLError:
        return True
    if settings is None:
        return False
    if not isinstance(settings, dict):
        return True
    picked = {str(k): v for k, v in settings.items() if k not in _INERT_PNPM}
    return bool(picked) and redirects(json.dumps(picked, default=str).encode("utf-8"))


def _specifiers_only(path: Path, data: bytes, hits: list[tuple[int, int]]) -> bool:
    """Whether every mention at ``hits`` in a script is inside a comment or a
    plain string that is a module specifier (see the module docstring)."""
    from flag_cleanup import cross_file_ts as ts
    from flag_cleanup import ts_syntax

    # A `.d.ts` is read the same way: `declare module "<name>"` is no
    # specifier position, so it refuses.
    try:
        root = ts_syntax._parse(data.decode("utf-8"), ts._language(str(path)))
    except UnicodeDecodeError:
        return False
    if root.has_error:
        return False
    for start, end in hits:
        node = root.descendant_for_byte_range(start, end)
        while node is not None and node.type not in ("comment", "string", "template_string", "regex", "program"):
            node = node.parent
        if node is None or node.type in ("template_string", "regex", "program"):
            return False
        if node.type != "comment" and not _specifier(node) and not _compile_only(path, node):
            return False
    return True


#: Options whose value lists package names a tool only COMPILES or leaves to
#: Node, by the config file that reads them: they say how the package is
#: built, never where its name leads. A whitelist: an entry here lets a
#: mention through, so a key that can redirect (webpack's ``externals``,
#: Rollup's ``external`` with ``output.globals``: a global another script
#: provides) must never join it. Each is a key path from the exported config
#: object, which :func:`_proven_config` must prove nothing else reads.
_COMPILE_ONLY = (
    ("next", re.compile(r"^next\.config\.[cm]?[jt]s$"), (("transpilePackages",), ("serverExternalPackages",))),
    (
        "vite",
        re.compile(r"^vite(?:st)?\.config(?:\.[\w-]+)?\.[cm]?[jt]s$"),
        (("optimizeDeps", "include"), ("optimizeDeps", "exclude")),
    ),
)
#: Calls an exported config may be wrapped in, by tool, as (import source,
#: imported name): looked through to their FIRST argument, one level. Each
#: Next plugin was read at the version noted in the internal notes and never
#: reads ``transpilePackages`` or ``serverExternalPackages`` into an alias or
#: resolution (``@serwist/turbopack`` and ``@sentry/nextjs`` append to
#: ``serverExternalPackages``). ``"default"`` is a default import or a
#: ``require`` of the module.
_CONFIG_WRAPPERS = {
    "next": frozenset({("@serwist/turbopack", "withSerwist"), ("@sentry/nextjs", "withSentryConfig")}),
    "vite": frozenset({("vite", "defineConfig"), ("vitest/config", "defineConfig")}),
}
#: Plugin factories: a default export called with options returns the wrapper
#: (``const withBundleAnalyzer = bundleAnalyzer({ enabled })``).
_CONFIG_WRAPPER_FACTORIES = {
    "next": frozenset({"@next/bundle-analyzer", "@serwist/next"}),
    "vite": frozenset(),
}
#: Next hooks that receive the resolved config (``webpack(config, { config })``).
_CONFIG_HOOKS = frozenset({"webpack"})


def compile_only(path: str | Path, string) -> bool:
    """Whether ``string``, a parsed string node of the script at ``path``, is
    a plain element of the literal list under one of :data:`_COMPILE_ONLY`'s
    options, on a config object no code but the tool can read: no load of the
    package it names, and no redirect of it."""
    return _compile_only(Path(path), string)


def _compile_only(path: Path, string) -> bool:
    if string.type != "string":
        return False
    tool = next(((t, keys) for t, pattern, keys in _COMPILE_ONLY if pattern.match(path.name)), None)
    array = string.parent
    if tool is None or array is None or array.type != "array":
        return False
    # The key path from the list up to the outermost object literal.
    names: list[str] = []
    node, holder = array, None
    while True:
        while node.parent is not None and node.parent.type in _SEE_THROUGH:
            node = node.parent
        pair = node.parent
        if pair is None or pair.type != "pair" or pair.child_by_field_name("value") != node:
            break
        key = pair.child_by_field_name("key")
        if key is None or key.type not in ("property_identifier", "string"):
            return False
        names.insert(0, key.text.decode("utf-8", "replace").strip("'\""))
        holder = pair.parent
        if holder is None or holder.type != "object":
            return False
        node = holder
    if tuple(names) not in tool[1] or holder is None:
        return False
    root = holder
    while root.parent is not None:
        root = root.parent
    proven = _proven_config(root, tool[0], path)
    return proven is not None and (proven.start_byte, proven.end_byte) == (holder.start_byte, holder.end_byte)


def _proven_config(root, tool: str, path: Path):
    """The object literal a config script exports when no code but the tool
    (and a whitelisted wrapper) can read it; None otherwise. Sound by
    construction, by the ways an object reaches code:

    * the export: the file's one ``export default`` or ``module.exports =``
      (no other ``module.exports``/``exports`` in the file), whose value is
      the object, a top-level ``const`` bound to it, or a call of a
      :data:`_CONFIG_WRAPPERS` wrapper whose FIRST argument is either;
    * a binding: the ``const`` name appears in the file only where it is
      declared and once in the export;
    * ``this``: none anywhere inside the object;
    * a spread into it: only of a whitelisted wrapper's call;
    * a hook the tool hands the resolved config to: every Next ``webpack``
      in the file is a function literal whose options parameter is absent
      or destructured by plain names other than ``config``, and that never
      names ``arguments``, nor reads ``module`` or ``options`` off its
      first parameter (the loader rules carry the config too); Vite hands
      it to every plugin, so a Vite config with a ``plugins`` key anywhere
      is refused;
    * the module system: no primitive that reaches the file's own exports
      or its loader (:func:`_reaches_itself`).

    Wrappers count only as an ``import`` binding or a ``const`` bound to a
    ``require`` of their package, never declared or assigned again, and
    only while no package of the checkout has their package's name."""
    if _reaches_itself(root, path):
        return None
    bindings: dict[bytes, list] = {}
    imports: dict[bytes, tuple[str, str]] = {}
    imported: set[bytes] = set()  # bound by an `import` statement, not a `const`
    for child in root.named_children:
        declaration = child.child_by_field_name("declaration") if child.type == "export_statement" else child
        if child.type == "import_statement":
            source = child.child_by_field_name("source")
            spec = _string_value(source) if source is not None else None
            clause = next((c for c in child.named_children if c.type == "import_clause"), None)
            if spec is None or clause is None:
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    imports[part.text] = (spec, "default")
                    imported.add(part.text)
                elif part.type == "named_imports":
                    for item in part.named_children:
                        if item.type == "import_specifier":
                            name, alias = item.child_by_field_name("name"), item.child_by_field_name("alias")
                            if name is not None:
                                imports[(alias or name).text] = (spec, name.text.decode("utf-8"))
                                imported.add((alias or name).text)
        elif declaration is not None and declaration.type in ("lexical_declaration", "variable_declaration"):
            constant = declaration.type == "lexical_declaration" and declaration.children[0].type == "const"
            for declarator in declaration.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name, value = declarator.child_by_field_name("name"), declarator.child_by_field_name("value")
                required = _required(value) if constant else None
                if name is not None and name.type == "identifier":
                    bindings.setdefault(name.text, []).append(value if constant else None)
                    if required is not None:
                        imports[name.text] = (required, "default")
                elif name is not None and name.type == "object_pattern" and required is not None:
                    for item in name.named_children:
                        if item.type == "shorthand_property_identifier_pattern":
                            imports[item.text] = (required, item.text.decode("utf-8"))
                        elif item.type == "pair_pattern":
                            key, local = item.child_by_field_name("key"), item.child_by_field_name("value")
                            if key is not None and local is not None and local.type == "identifier":
                                imports[local.text] = (required, key.text.decode("utf-8"))

    def bound(name: bytes):
        values = bindings.get(name, [])
        return values[0] if len(values) == 1 else None

    def wrapper(callee) -> bool:
        if callee is None or callee.type != "identifier":
            return False
        source = imports.get(callee.text)
        if source in _CONFIG_WRAPPERS[tool]:
            return not _rebound(root, callee.text, 0 if callee.text in imported else 1) and not _in_checkout(path, source[0])
        return not _rebound(root, callee.text, 1) and _factory_product(callee)

    def _factory_product(callee) -> bool:
        value = bound(callee.text)
        if value is None or value.type != "call_expression":
            return False
        made = value.child_by_field_name("function")
        if made is not None and made.type == "identifier":
            source = imports.get(made.text)
            package = (
                source[0]
                if source is not None and source[1] == "default" and not _rebound(root, made.text, 0 if made.text in imported else 1)
                else None
            )
        else:
            package = _required(made) if made is not None else None
        return package in _CONFIG_WRAPPER_FACTORIES[tool] and not _in_checkout(path, package)

    exports = []
    for node in _walk(root):
        if node.type == "export_statement" and any(c.type == "default" for c in node.children):
            exports.append(node.child_by_field_name("value"))
        elif node.type == "identifier" and node.text == b"exports":
            exports.append(None)  # `exports.webpack = ...` changes what is exported
        elif node.type == "member_expression" and node.text.replace(b" ", b"") == b"module.exports":
            parent = node.parent
            if (
                parent is not None
                and parent.type == "assignment_expression"
                and parent.child_by_field_name("left") == node
                and parent.parent is not None
                and parent.parent.type == "expression_statement"
                and parent.parent.parent == root
            ):
                exports.append(parent.child_by_field_name("right"))
            else:
                exports.append(None)  # any other use of the CommonJS export
    if len(exports) != 1 or exports[0] is None:
        return None
    value = _unwrapped(exports[0])
    binding = None
    if value is not None and value.type == "call_expression":
        if not wrapper(value.child_by_field_name("function")):
            return None
        arguments = value.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
        value = _unwrapped(first)
    if value is not None and value.type == "identifier":
        binding = value.text
        value = _unwrapped(bound(binding))
    if value is None or value.type != "object":
        return None
    if binding is not None and sum(1 for n in _walk(root) if n.type in ("identifier", "shorthand_property_identifier") and n.text == binding) != 2:
        return None  # read, written or handed on somewhere else
    for item in value.named_children:
        if item.type == "spread_element":
            spread = _unwrapped(item.named_children[0] if item.named_children else None)
            if spread is None or spread.type != "call_expression" or not wrapper(spread.child_by_field_name("function")):
                return None
    for node in _walk(value):
        # A getter or setter runs code on a read; a computed key hides which
        # option (`['web' + 'pack']`) it sets.
        if node.type == "computed_property_name":
            return None
        if node.type == "method_definition" and any(c.type in ("get", "set") for c in node.children):
            return None
    # The option names may be spelled only as the list's own keys.
    own_keys = {
        (key.start_byte, key.end_byte)
        for item in value.named_children
        if item.type == "pair" and (key := item.child_by_field_name("key")) is not None
    }
    for node in _walk(root):
        if node.type in ("identifier", "property_identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern", "string", "template_string"):
            text = node.text.decode("utf-8", "replace")
            if any(name in text for name in _COMPILE_OPTION_NAMES):
                if node.type in ("property_identifier", "string") and (node.start_byte, node.end_byte) in own_keys:
                    continue
                if node.type == "string" and node.parent is not None and node.parent.type == "string":
                    continue
                return None
        elif node.type == "string_fragment" and any(name in node.text.decode("utf-8", "replace") for name in _COMPILE_OPTION_NAMES):
            parent = node.parent
            if parent is not None and (parent.start_byte, parent.end_byte) in own_keys:
                continue
            return None
    for node in _walk(root):
        if tool == "vite" and node.type in ("pair", "shorthand_property_identifier") and _key_text(node) == "plugins":
            return None
        if tool == "next" and not _hook_blind(node):
            return None
    return value


#: Names through which code reaches its own module, the loader, or code
#: built at run time; or (``defaultLoaders``) the config Next hands its
#: hooks. Any one, anywhere in a compile-only config, refuses it.
_SELF_REACHING = frozenset(
    {
        b"exports", b"createRequire", b"__filename", b"__dirname", b"arguments", b"eval", b"Function",
        b"globalThis", b"global", b"Reflect", b"Proxy", b"defaultLoaders",
    }
)
#: Node's built-in modules, as ``require('module').builtinModules`` lists
#: them (Node 22), plus any ``node:`` specifier: a config may load only
#: :data:`_ALLOWED_BUILTINS` of them. The rest reach the loader
#: (``module._cache``), the process, or files and code at run time.
_NODE_BUILTINS = frozenset(
    """_http_agent _http_client _http_common _http_incoming _http_outgoing _http_server _stream_duplex
    _stream_passthrough _stream_readable _stream_transform _stream_wrap _stream_writable _tls_common _tls_wrap
    assert assert/strict async_hooks buffer child_process cluster console constants crypto dgram
    diagnostics_channel dns dns/promises domain events fs fs/promises http http2 https inspector
    inspector/promises module net os path path/posix path/win32 perf_hooks process punycode querystring
    readline readline/promises repl stream stream/consumers stream/promises stream/web string_decoder sys
    timers timers/promises tls trace_events tty url util util/types v8 vm wasi worker_threads zlib""".split()
)
_ALLOWED_BUILTINS = frozenset({"path", "node:path", "url", "node:url"})
#: Property names that reach the same: ``process.mainModule``, Next's
#: ``defaultLoaders``.
_SELF_REACHING_PROPERTIES = frozenset({b"mainModule", b"defaultLoaders"})


def _reaches_itself(root, path: Path) -> bool:
    """Whether a config script can reach its own exports, its loader or the
    config a tool hands its hooks, by any primitive JavaScript has for it:

    * ``module``, except as the one top-level ``module.exports =`` target
      (``module['exports']``, ``module.children``, ``module.require``);
    * ``exports``; ``this`` anywhere (``module.exports`` at the top of a
      CommonJS file, the config itself in a method);
    * ``require`` other than called with one plain string naming a package
      or a relative path that is not this file (so no ``require.cache``,
      ``require.main``, ``require.resolve``, ``require(__filename)``, no
      ``const r = require``); ``import(...)`` likewise; ``import.meta``;
      ``createRequire``;
    * ``__filename``, ``__dirname``, ``arguments`` (the CommonJS wrapper's
      ``module`` is ``arguments[2]``), ``eval``, ``Function``,
      ``globalThis``, ``global``, ``Reflect``, ``Proxy``, ``with``,
      ``process.mainModule``;
    * ``defaultLoaders`` (Next's Babel loader options carry the config);
    * ``process`` (``process.getBuiltinModule('module')._cache``) but as a
      read of one environment variable (:func:`_env_read`: strings, which
      reach nothing), and any Node built-in but ``path`` and ``url``, by
      ``import``, ``require`` or ``import()``
      (``require('node:module')._cache``), but ``readFileSync`` imported by
      name from ``fs`` and only ever called on a literal ``.json`` path
      inside ``JSON.parse`` (:func:`_json_read_only`);
    * an enumeration (:data:`_ENUMERATORS`, ``Object.assign`` from anything
      but object literals, ``for...in``) or ``JSON``, except ``JSON.parse``
      of a string literal or of ``readFileSync`` of a literal ``.json``
      path; and (judged by :func:`_proven_config`, which knows the list's
      own key) any other spelling of the compile options' names.

    The internal notes give why this set is closed."""
    for node in _walk(root):
        kind = node.type
        if kind in ("this", "with_statement", "meta_property"):
            return True
        if kind == "identifier":
            text = node.text
            if text in _SELF_REACHING:
                return True
            if text == b"module" and not _the_export_target(node, root):
                return True
            if text == b"require" and not _plain_load(node, path, "function"):
                return True
            if text == b"process" and not _env_read(node):
                return True
            if text == b"readFileSync" and not _json_read_only(node):
                return True
        elif kind in ("property_identifier", "shorthand_property_identifier_pattern", "shorthand_property_identifier"):
            if node.text in _SELF_REACHING_PROPERTIES:
                return True
        elif kind == "string" and (_string_value(node) or "").encode("utf-8") in _SELF_REACHING_PROPERTIES:
            return True
        elif kind == "call_expression":
            function = node.child_by_field_name("function")
            if function is not None and function.type == "import" and not _plain_load(function, path, "function"):
                return True
        elif kind == "import_statement" and not any(c.type == "type" for c in node.children):
            source = node.child_by_field_name("source")
            if source is None or (_builtin(_string_value(source)) and not _imports_read_file_sync(node)):
                return True
        elif kind == "for_in_statement" and any(c.type == "in" for c in node.children):
            return True
        elif kind == "subscript_expression":
            index = node.child_by_field_name("index")
            if index is None or index.type not in ("string", "number"):
                return True  # `x['constr' + 'uctor']` names what no check can see
        if kind in ("property_identifier", "shorthand_property_identifier_pattern") and node.text in _PROTOTYPE_NAMES:
            return True
        if kind == "string" and (_string_value(node) or "").encode("utf-8") in _PROTOTYPE_NAMES:
            return True
        if kind == "member_expression":
            obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
            if obj is not None and prop is not None and obj.text in (b"Object", b"JSON"):
                if obj.text == b"Object" and prop.text in _ENUMERATORS:
                    return True
                if obj.text == b"Object" and prop.text == b"assign" and not _assigns_literals(node):
                    return True
                if obj.text == b"JSON" and not _parses_a_literal(node):
                    return True
        if kind == "string" and node.parent is not None and node.parent.type == "arguments":
            call = node.parent.parent
            function = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
            if function is not None and function.type in ("identifier", "import") and function.text in (b"require", b"import"):
                if _builtin(_string_value(node)) and not _requires_read_file_sync(call):
                    return True
    return False


def _builtin(spec: str | None) -> bool:
    """Whether ``spec`` names a Node built-in a config may not load."""
    if spec is None:
        return True
    if spec in _ALLOWED_BUILTINS:
        return False
    return spec.startswith("node:") or spec in _NODE_BUILTINS


#: Properties that reach a value's constructor or prototype: every function's
#: ``constructor`` is ``Function`` (``[].map.constructor('code')``), which
#: runs a string no check reads.
_PROTOTYPE_NAMES = frozenset({b"constructor", b"__proto__", b"prototype"})
#: ``Object.prototype``'s members: names an environment read may not take,
#: since ``process.env`` inherits them.
_OBJECT_PROTOTYPE_MEMBERS = frozenset(
    {
        b"constructor", b"__proto__", b"hasOwnProperty", b"isPrototypeOf", b"propertyIsEnumerable",
        b"toLocaleString", b"toString", b"valueOf", b"__defineGetter__", b"__defineSetter__",
        b"__lookupGetter__", b"__lookupSetter__",
    }
)
#: An environment variable's name, as such names are written.
_ENV_NAME = re.compile(rb"[A-Z_][A-Z0-9_]*\Z")

#: The modules the one ``fs`` binding a config may take comes from.
_FS_MODULES = frozenset({"fs", "node:fs"})


def _env_read(process) -> bool:
    """Whether ``process`` is read only as one environment variable by name
    (a property, or a string-literal index): a name written like one
    (``API_URL``) or any identifier that is not an ``Object.prototype``
    member, whose value is a string used as one: never assigned, deleted,
    used whole, called, indexed or read into (``.constructor``)."""
    env = process.parent
    if env is None or env.type != "member_expression" or env.child_by_field_name("object") != process:
        return False
    prop = env.child_by_field_name("property")
    read = env.parent
    if prop is None or prop.text != b"env" or read is None:
        return False
    if read.type == "member_expression":
        named = read.child_by_field_name("property")
        if read.child_by_field_name("object") != env or named is None or named.type != "property_identifier":
            return False
        variable = named.text
    elif read.type == "subscript_expression":
        index = read.child_by_field_name("index")
        if read.child_by_field_name("object") != env or index is None or index.type != "string":
            return False
        variable = (_string_value(index) or "").encode("utf-8")
    else:
        return False
    if not (_ENV_NAME.match(variable) or (variable and variable not in _OBJECT_PROTOTYPE_MEMBERS)):
        return False
    if _ENV_NAME.match(variable) is None and not re.fullmatch(rb"[A-Za-z_$][\w$]*", variable):
        return False
    outer = read.parent
    if outer is None:
        return True
    # The value is a string: used as one, never called, indexed or read into.
    if outer.type == "call_expression" and outer.child_by_field_name("function") == read:
        return False
    if outer.type in ("member_expression", "subscript_expression") and outer.child_by_field_name("object") == read:
        return False
    if outer.type in ("assignment_expression", "augmented_assignment_expression") and outer.child_by_field_name("left") == read:
        return False
    if outer.type == "update_expression" or (outer.type == "unary_expression" and outer.children[0].type == "delete"):
        return False
    return True


def _imports_read_file_sync(statement) -> bool:
    """Whether an ``import`` takes exactly ``readFileSync``, by name and
    unrenamed, from ``fs`` or ``node:fs``."""
    source = statement.child_by_field_name("source")
    if source is None or _string_value(source) not in _FS_MODULES:
        return False
    clause = next((c for c in statement.named_children if c.type == "import_clause"), None)
    parts = clause.named_children if clause is not None else []
    if len(parts) != 1 or parts[0].type != "named_imports":
        return False  # a default or namespace import hands on the whole module
    specifiers = [c for c in parts[0].named_children if c.type == "import_specifier"]
    return (
        len(specifiers) == 1
        and specifiers[0].child_by_field_name("alias") is None
        and specifiers[0].child_by_field_name("name").text == b"readFileSync"
    )


def _requires_read_file_sync(call) -> bool:
    """Whether a ``require('fs')`` is ``const { readFileSync } = require('fs')``."""
    declarator = call.parent
    if declarator is None or declarator.type != "variable_declarator" or declarator.child_by_field_name("value") != call:
        return False
    declaration = declarator.parent
    pattern = declarator.child_by_field_name("name")
    return (
        _required(call) in _FS_MODULES
        and declaration is not None
        and declaration.type == "lexical_declaration"
        and declaration.children[0].type == "const"
        and pattern is not None
        and pattern.type == "object_pattern"
        and [c.type for c in pattern.named_children] == ["shorthand_property_identifier_pattern"]
        and pattern.named_children[0].text == b"readFileSync"
    )


def _json_read_only(name) -> bool:
    """Whether an identifier ``readFileSync`` is its own binding (by
    :func:`_imports_read_file_sync` or :func:`_requires_read_file_sync`),
    or a call on a literal ``.json`` path that is ``JSON.parse``'s one
    argument (:func:`_parses_a_literal`)."""
    parent = name.parent
    if parent is not None and parent.type == "import_specifier":
        return True
    call = parent
    if call is None or call.type != "call_expression" or call.child_by_field_name("function") != name:
        return False
    arguments = call.parent
    parse = arguments.parent if arguments is not None and arguments.type == "arguments" else None
    member = parse.child_by_field_name("function") if parse is not None and parse.type == "call_expression" else None
    return (
        member is not None
        and member.type == "member_expression"
        and member.text == b"JSON.parse"
        and _parses_a_literal(member)
    )


def _parses_a_literal(member) -> bool:
    """Whether ``JSON.<x>`` is ``JSON.parse`` of a string literal, or of
    ``readFileSync('<literal>.json', ...)``: data, never the config."""
    prop = member.child_by_field_name("property")
    call = member.parent
    if prop is None or prop.text != b"parse" or call is None or call.type != "call_expression":
        return False
    arguments = call.child_by_field_name("arguments")
    listed = [a for a in arguments.named_children if a.type != "comment"] if arguments is not None else []
    if len(listed) != 1:
        return False
    first = listed[0]
    if first.type == "string":
        return True
    if first.type != "call_expression":
        return False
    read = first.child_by_field_name("function")
    inner = first.child_by_field_name("arguments")
    read_args = [a for a in inner.named_children if a.type != "comment"] if inner is not None else []
    return (
        read is not None
        and read.type == "identifier"
        and read.text == b"readFileSync"
        and bool(read_args)
        and read_args[0].type == "string"
        and (_string_value(read_args[0]) or "").endswith(".json")
    )


def _the_export_target(module, root) -> bool:
    """Whether ``module`` is the object of the one top-level ``module.exports
    = ...`` the file exports with."""
    member = module.parent
    if member is None or member.type != "member_expression" or member.child_by_field_name("object") != module:
        return False
    prop = member.child_by_field_name("property")
    assignment = member.parent
    return (
        prop is not None
        and prop.text == b"exports"
        and assignment is not None
        and assignment.type == "assignment_expression"
        and assignment.child_by_field_name("left") == member
        and assignment.parent is not None
        and assignment.parent.type == "expression_statement"
        and assignment.parent.parent == root
    )


def _plain_load(callee, path: Path, field: str) -> bool:
    """Whether ``callee`` (``require`` or ``import``) is called with one plain
    string naming a package, or a relative path to another file."""
    call = callee.parent
    if call is None or call.type != "call_expression" or call.child_by_field_name(field) != callee:
        return False
    arguments = call.child_by_field_name("arguments")
    listed = [a for a in arguments.named_children if a.type != "comment"] if arguments is not None else []
    if len(listed) != 1 or listed[0].type != "string":
        return False
    spec = _string_value(listed[0])
    if not spec:
        return False
    if spec.startswith((".", "/")):
        target = Path(os.path.normpath(path.parent / spec))
        stem = path.name.split(".")[0]
        own = {path, path.with_suffix(""), path.parent / stem, path.parent / ".".join(path.name.split(".")[:-1])}
        if target in own or target == path.parent:
            return False  # this file, or its directory's index
    return True


def _rebound(root, name: bytes, allowed: int) -> bool:
    """Whether ``name`` is declared or assigned anywhere beyond its own
    binding (``allowed`` declarators: 1 for a ``const``, 0 for an
    ``import``): another ``let``/``var``/``const`` or parameter of the name
    (a shadow, over-refused), a ``function`` or ``class`` of it, an
    assignment or an update."""
    declared = 0
    for node in _walk(root):
        kind = node.type
        if kind in ("assignment_expression", "augmented_assignment_expression", "update_expression"):
            target = node.child_by_field_name("left") if kind != "update_expression" else node
            if target is not None and any(n.type == "identifier" and n.text == name for n in _walk(target)):
                return True
        elif kind in ("function_declaration", "generator_function_declaration", "class_declaration"):
            declared_name = node.child_by_field_name("name")
            if declared_name is not None and declared_name.text == name:
                return True
        else:
            if kind == "variable_declarator":
                target = node.child_by_field_name("name")
            elif kind == "formal_parameters":
                target = node
            elif kind in ("arrow_function", "catch_clause"):
                target = node.child_by_field_name("parameter")
            else:
                continue
            if target is not None and any(
                n.type in ("identifier", "shorthand_property_identifier_pattern") and n.text == name for n in _walk(target)
            ):
                declared += 1
    return declared > allowed


def _in_checkout(path: Path, package: str) -> bool:
    """Whether a ``package.json`` of the checkout is named ``package``: the
    name may then resolve to it rather than to the published wrapper."""
    from flag_cleanup import source_index

    try:
        stop = Path(source_index.checkout_root(str(path.parent))).resolve()
        return bool(_named(_checkout(stop), package))
    except (OSError, RuntimeError, ValueError):
        return True


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _unwrapped(node):
    while node is not None and node.type in _SEE_THROUGH:
        node = node.named_children[0] if node.named_children else None
    return node


def _key_text(node) -> str | None:
    """The plain key of a pair, method or shorthand property."""
    if node.type == "shorthand_property_identifier":
        return node.text.decode("utf-8", "replace")
    key = node.child_by_field_name("key") if node.type == "pair" else node.child_by_field_name("name")
    if key is None or key.type not in ("property_identifier", "string"):
        return None
    return key.text.decode("utf-8", "replace").strip("'\"`")


def _required(value) -> str | None:
    """``m`` of a ``require("m")`` call."""
    if value is None or value.type != "call_expression":
        return None
    function = value.child_by_field_name("function")
    arguments = value.child_by_field_name("arguments")
    if function is None or function.text != b"require" or arguments is None or len(arguments.named_children) != 1:
        return None
    first = arguments.named_children[0]
    return _string_value(first) if first.type == "string" else None


#: Property names a Next ``webpack`` hook may not READ: the loader options
#: (``rule.use[].options``, ``rule.options``, a ``oneOf`` branch's) carry
#: Next's config, ``transpilePackages`` included. Writing them as keys of a
#: rule the hook adds (``use: [{ loader, options }]``) is fine.
_LOADER_READS = frozenset({b"options", b"use", b"loader", b"oneOf"})
#: ``Object`` methods that enumerate an object's properties, or copy them.
_ENUMERATORS = frozenset(
    {b"values", b"entries", b"keys", b"getOwnPropertyDescriptors", b"getOwnPropertyDescriptor", b"getOwnPropertyNames"}
)
#: The option names a hook may not spell at all.
_COMPILE_OPTION_NAMES = frozenset({"transpilePackages", "serverExternalPackages"})


def _loader_rules_unread(function, first) -> bool:
    """Whether a Next ``webpack`` hook cannot dig the config out of the loader
    rules it is handed. Its first parameter (the webpack config) is used only
    through a property (``config.module.rules.find(...)``,
    ``config.resolve.alias[...] = ...``, ``config.plugins.push(...)``) or
    returned; never destructured, aliased or handed to other code. And
    nowhere in the hook: a read of :data:`_LOADER_READS` (by member, by a
    computed index, or by destructuring), a computed index that is not a
    literal (``rule['opt' + 'ions']``), an ``Object`` enumeration
    (:data:`_ENUMERATORS`) or ``Object.assign`` from anything but an object
    literal, ``for...in``, ``JSON``, or any spelling of the compile options'
    names. What remains, an accepted residual (README): spreading or
    rest-destructuring a found rule object and using the result as an
    alias, and a rule object handed to a helper outside the hook."""
    if first.type in ("required_parameter", "optional_parameter"):
        first = first.child_by_field_name("pattern")
    if first is None or first.type != "identifier":
        return False
    name = first.text
    for node in _walk(function):
        kind = node.type
        if kind == "identifier" and node.text == name and node != first:
            parent = node.parent
            if parent is not None and parent.type == "member_expression" and parent.child_by_field_name("object") == node:
                continue
            if parent is not None and (
                parent.type == "return_statement"
                or (parent.type == "arrow_function" and parent.child_by_field_name("body") == node)
            ):
                continue
            return False
        if kind == "member_expression":
            prop = node.child_by_field_name("property")
            obj = node.child_by_field_name("object")
            if prop is not None and prop.text in _LOADER_READS:
                return False
            if obj is not None and obj.text in (b"Object", b"JSON") and prop is not None:
                if obj.text == b"JSON" or prop.text in _ENUMERATORS:
                    return False
                if prop.text == b"assign" and not _assigns_literals(node):
                    return False
        elif kind == "subscript_expression":
            index = node.child_by_field_name("index")
            if index is None or index.type not in ("string", "number"):
                return False
            if index.type == "string" and (_string_value(index) or "").encode("utf-8") in _LOADER_READS:
                return False
        elif kind in ("shorthand_property_identifier_pattern",) and node.text in _LOADER_READS:
            return False
        elif kind == "pair_pattern":
            key = node.child_by_field_name("key")
            if key is None or key.type not in ("property_identifier", "string") or key.text.strip(b"'\"") in _LOADER_READS:
                return False
        elif kind == "for_in_statement" and any(c.type == "in" for c in node.children):
            return False
        elif kind in ("identifier", "property_identifier", "shorthand_property_identifier") and node.text.decode(
            "utf-8", "replace"
        ) in _COMPILE_OPTION_NAMES:
            return False
        elif kind in ("string", "template_string") and any(n in node.text.decode("utf-8", "replace") for n in _COMPILE_OPTION_NAMES):
            return False
    return True


def _assigns_literals(member) -> bool:
    """Whether an ``Object.assign`` call copies only object literals in."""
    call = member.parent
    if call is None or call.type != "call_expression" or call.child_by_field_name("function") != member:
        return False
    arguments = call.child_by_field_name("arguments")
    listed = [a for a in arguments.named_children if a.type != "comment"] if arguments is not None else []
    return all(_unwrapped(a) is not None and _unwrapped(a).type == "object" for a in listed[1:])


def _hook_blind(node) -> bool:
    """Whether ``node`` is not a Next hook, or is one that cannot read the
    resolved config it is handed: see :func:`_proven_config`."""
    if node.type == "assignment_expression":
        left = node.child_by_field_name("left")
        prop = left.child_by_field_name("property") if left is not None and left.type == "member_expression" else None
        return prop is None or prop.text.decode("utf-8", "replace") not in _CONFIG_HOOKS
    if node.type == "shorthand_property_identifier":
        return node.text.decode("utf-8", "replace") not in _CONFIG_HOOKS
    if node.type not in ("pair", "method_definition") or _key_text(node) not in _CONFIG_HOOKS:
        return True
    function = _unwrapped(node.child_by_field_name("value")) if node.type == "pair" else node
    if function is None or function.type not in ("arrow_function", "function_expression", "function", "method_definition"):
        return False
    if any(n.type == "identifier" and n.text == b"arguments" for n in _walk(function)):
        return False
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return function.type == "arrow_function"  # `config => ...`: one parameter
    listed = [p for p in parameters.named_children if p.type != "comment"]
    if len(listed) > 2:
        return False
    if listed and not _loader_rules_unread(function, listed[0]):
        return False
    if len(listed) < 2:
        return True
    options = listed[1]
    if options.type in ("required_parameter", "optional_parameter"):
        options = options.child_by_field_name("pattern")
    if options is None or options.type != "object_pattern":
        return False
    for item in options.named_children:
        if item.type == "object_assignment_pattern":
            item = item.child_by_field_name("left")
        if item is None:
            return False
        if item.type == "shorthand_property_identifier_pattern":
            name = item.text
        elif item.type == "pair_pattern":
            key = item.child_by_field_name("key")
            if key is None or key.type not in ("property_identifier", "string"):
                return False
            name = key.text.strip(b"'\"")
        elif item.type == "comment":
            continue
        else:
            return False  # a rest element, a computed key
        if name == b"config":
            return False
    return True


#: Wrappers seen through when asking where a string stands.
_SEE_THROUGH = ("parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression")
#: Operators whose operand can be a redirect's test of a specifier.
_COMPARISONS = frozenset({"===", "!==", "==", "!=", "in"})
#: Methods that test (or rewrite) a specifier with a string argument.
_TEST_METHODS = frozenset({"startsWith", "endsWith", "includes", "match", "test", "replace", "indexOf"})
#: Methods whose receiver, an array literal, is a list of names a specifier is
#: looked up in (`["@acme/flags"].includes(id)`).
_LOOKUP_METHODS = frozenset({"includes", "has", "get", "indexOf"})
#: Constructors whose argument is a lookup table (`new Map([["@acme/flags", m]])`).
_TABLES = frozenset({b"Map", b"Set"})
#: Option keys whose value is a redirect's pattern.
_PATTERN_KEYS = frozenset({"find", "filter", "test"})


#: Escapes a mapper key may hold that a JavaScript and a Python pattern read
#: alike on an ASCII specifier: a quoted metacharacter, a digit, a word char.
_MAPPER_ESCAPES = frozenset(r"./-\^$*+?()[]|dw")
#: How a JavaScript string spells a character by escape, for the ones a key
#: may use; any other escape is not read.
_STRING_ESCAPES = {"\\\\": "\\", "\\'": "'", '\\"': '"', "\\/": "/"}


def _literal_mapper(path: Path, data: bytes, hits: list[tuple[int, int]]) -> tuple[re.Pattern[str], ...] | None:
    """The keys of every Jest ``moduleNameMapper`` at ``hits`` in a script,
    compiled, when each hit outside a comment is the key of an option whose
    value is an object literal of plain keys, every one a pattern JavaScript
    and Python match alike (:func:`_mapper_pattern`); None otherwise (a
    spread, a computed key, a variable, an assignment, another mapper option,
    a pattern this cannot read the same way), and then the script refuses."""
    from flag_cleanup import cross_file_ts as ts
    from flag_cleanup import ts_syntax

    try:
        root = ts_syntax._parse(data.decode("utf-8"), ts._language(str(path)))
    except UnicodeDecodeError:
        return None
    if root.has_error:
        return None
    keys: list[re.Pattern[str]] = []
    for start, end in hits:
        if data[start:end] != b"moduleNameMapper":
            return None
        node = root.descendant_for_byte_range(start, end)
        while node is not None and node.type not in ("comment", "property_identifier", "string", "program"):
            node = node.parent
        if node is None or node.type == "program":
            return None
        if node.type == "comment":
            continue
        if node.type == "string" and _string_value(node) != "moduleNameMapper":
            return None
        pair = node.parent
        if pair is None or pair.type != "pair" or pair.child_by_field_name("key") != node:
            return None
        value = pair.child_by_field_name("value")
        if value is None or value.type != "object":
            return None
        for item in value.named_children:
            if item.type == "comment":
                continue
            key = item.child_by_field_name("key") if item.type == "pair" else None
            if key is None:
                return None  # a spread, a method, a shorthand property
            if key.type == "property_identifier":
                text = key.text.decode("utf-8")
            elif key.type == "string":
                text = _string_value(key)
            else:
                return None
            pattern = _mapper_pattern(text) if text is not None else None
            if pattern is None:
                return None
            keys.append(pattern)
    return tuple(keys)


def _string_value(string) -> str | None:
    """The value of a plain string literal, when every escape in it is one of
    :data:`_STRING_ESCAPES`; None otherwise."""
    parts: list[str] = []
    for child in string.named_children:
        text = child.text.decode("utf-8", "replace")
        if child.type == "string_fragment":
            parts.append(text)
        elif child.type == "escape_sequence" and text in _STRING_ESCAPES:
            parts.append(_STRING_ESCAPES[text])
        else:
            return None
    return "".join(parts)


def _mapper_pattern(source: str) -> re.Pattern[str] | None:
    """``source``, a Jest mapper key (a JavaScript pattern, no flags), as a
    Python pattern that matches an ASCII specifier exactly where it does; None
    when it holds syntax the two may read apart. Sound by construction, not by
    a list of known differences: only printable ASCII, no braces, no group but
    ``(?:``, ``(?=``, ``(?!``, escapes only of punctuation, ``\\d`` and ``\\w``,
    and a character class only as :func:`_class_end` reads it."""
    if not source or any(not (" " <= c <= "~") for c in source) or "{" in source or "}" in source:
        return None
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "\\":
            if i + 1 >= n or source[i + 1] not in _MAPPER_ESCAPES:
                return None
            i += 2
            continue
        if c == "[":
            end = _class_end(source, i)
            if end is None:
                return None
            i = end
            continue
        if c == "(" and source.startswith("(?", i) and not source.startswith(("(?:", "(?=", "(?!"), i):
            return None
        i += 1
    try:
        return re.compile(source)
    except re.error:
        return None


def _class_end(source: str, start: int) -> int | None:
    """The index past the character class opening at ``start``, when it is one
    JavaScript and Python read alike: an optional ``^``, then at least one
    member before the closing ``]`` (so neither ``[]``, which JavaScript reads
    as matching nothing, nor ``[^]``, which it reads as matching anything,
    while Python reads a ``]`` there as a member), each member a literal
    character other than ``[``, ``]`` and ``\\``, an escape of punctuation,
    ``\\d``, ``\\w``, or a range between two letters or two digits of one
    kind. Doubled ``-``, ``&``, ``~`` or ``|`` (a set operation Python may
    adopt) and anything else is None."""
    n = len(source)
    j = start + 1
    if j < n and source[j] == "^":
        j += 1
    members = 0
    while j < n:
        c = source[j]
        if c == "]":
            return j + 1 if members else None
        if c == "[":
            return None
        if c in "-&~|" and j + 1 < n and source[j + 1] == c:
            return None
        if c == "\\":
            if j + 1 >= n or source[j + 1] not in _MAPPER_ESCAPES:
                return None
            j += 2
            if j + 1 < n and source[j] == "-" and source[j + 1] != "]":
                return None  # a range from an escape
            members += 1
            continue
        j += 1
        if j + 1 < n and source[j] == "-" and source[j + 1] != "]":
            low, high = c, source[j + 1]
            if not any(low in k and high in k for k in _RANGE_KINDS) or low > high:
                return None
            j += 2
        members += 1
    return None  # unterminated


#: The character kinds a class range may span.
_RANGE_KINDS = ("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789")


def _mapper_keys(checkout: _Checkout) -> dict[Path, tuple[re.Pattern[str], ...] | None]:
    """Every primary resolver script of the checkout that names a mapper,
    with its literal Jest mapper keys (:func:`_literal_mapper`), worked out
    once per run. Only a primary script's map is read this way: anywhere
    else a mapper refuses on sight."""
    if checkout.mapper_keys is None:
        found: dict[Path, tuple[re.Pattern[str], ...] | None] = {}
        capable = _capable(checkout)
        for path in checkout.files:
            if _resolver_kind(path) != "script" or path.resolve() not in capable:
                continue
            if path.resolve() not in checkout.primary:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue  # `_mentioned` refuses an unreadable file itself
            hits = [m.span() for m in _MAPPERS.finditer(data)]
            if hits:
                found[path] = _literal_mapper(path, data, hits)
        checkout.mapper_keys = found
    return checkout.mapper_keys


#: Text in a resolver's config that can redirect a subpath without naming
#: the package: webpack's replacement plugin (its pattern is often a string).
_DEEP_REDIRECT_TEXT = re.compile(rb"NormalModuleReplacementPlugin")


def _deep_redirect(checkout: _Checkout) -> str:
    """The first resolver-capable script of the checkout (one a tool loads as
    its config, one that loads a resolver's API, or anything such a script
    imports or names: an alias table kept in a helper) holding a regex literal, a
    ``RegExp(...)``, ``NormalModuleReplacementPlugin`` or a ``resolver``
    property, or that does not parse; ``""`` for none. Any of them may send
    ``@acme/flags/src/keys`` elsewhere by matching ``/src/keys`` alone (a
    Vite alias ``find: /\\/src\\/keys$/``, a Jest ``resolver``), which the
    checks on the package's name cannot see. Refuses more than it must, on
    purpose: a deep subpath is only followed where nothing like it exists."""
    if checkout.deep_redirect is None:
        from flag_cleanup import cross_file_ts as ts
        from flag_cleanup import ts_syntax

        found = ""
        capable = _capable(checkout)
        for path in checkout.files:
            resolved = path.resolve()
            if _resolver_kind(path) != "script" or resolved not in capable:
                continue
            try:
                data = path.read_bytes()
                root = ts_syntax._parse(data.decode("utf-8"), ts._language(str(path)))
            except (OSError, UnicodeDecodeError):
                found = str(path)
                break
            if root.has_error or _DEEP_REDIRECT_TEXT.search(data) or _redirects_by_pattern(root):
                found = str(path)
                break
        checkout.deep_redirect = found
    return checkout.deep_redirect


def _redirects_by_pattern(root) -> bool:
    """Whether a parsed script holds a regex literal, a ``RegExp`` call or
    construction, or a ``resolver`` property (a key, or ``x.resolver``)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "regex":
            return True
        if node.type in ("new_expression", "call_expression"):
            callee = node.child_by_field_name("constructor" if node.type == "new_expression" else "function")
            if callee is not None and callee.text in (b"RegExp", b"globalThis.RegExp"):
                return True
        if node.type == "pair":
            key = node.child_by_field_name("key")
            if key is not None and key.text.strip(b"'\"`") == b"resolver":
                return True
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            if prop is not None and prop.text == b"resolver":
                return True
        if node.type in ("shorthand_property_identifier", "method_definition") and (
            node.text == b"resolver" or node.child_by_field_name("name") is not None and node.child_by_field_name("name").text == b"resolver"
        ):
            return True
        stack.extend(node.children)
    return False


def _mapped(specifier: str, checkout: _Checkout) -> bool:
    """Whether a Jest mapper of the checkout may send ``specifier``
    elsewhere: a literal key that matches it, any key when it is not plain
    ASCII, or a map that cannot be read."""
    keys = _mapper_keys(checkout)
    if not keys:
        return False
    if any(k is None for k in keys.values()) or not specifier.isascii():
        return True
    return any(p.search(specifier) for patterns in keys.values() for p in patterns)


def _outside_comments(path: Path, data: bytes, hits: list[tuple[int, int]]) -> bool:
    """Whether any of ``hits`` in a script stands outside a comment; a script
    that does not parse is taken to have one."""
    from flag_cleanup import cross_file_ts as ts
    from flag_cleanup import ts_syntax

    try:
        root = ts_syntax._parse(data.decode("utf-8"), ts._language(str(path)))
    except UnicodeDecodeError:
        return True
    if root.has_error:
        return True
    for start, end in hits:
        node = root.descendant_for_byte_range(start, end)
        while node is not None and node.type not in ("comment", "program"):
            node = node.parent
        if node is None or node.type != "comment":
            return True
    return False


def _redirect_shaped(path: Path, data: bytes, hits: list[tuple[int, int]]) -> bool:
    """Whether a mention at ``hits`` in a script stands where it can redirect a
    specifier: an object KEY (computed from a string too), the value of a
    ``find``/``filter``/``test`` option, a regex (literal or ``new
    RegExp(...)``), an operand of ``===``/``!==``/``==``/``!=``/``in``, of a
    ``switch`` case or of ``.startsWith``/``.endsWith``/``.includes``/
    ``.match``/``.test``/``.replace``/``.indexOf``, an element of an array
    literal that ``.includes``/``.has``/``.get``/``.indexOf`` is called on, or
    anything inside the argument of ``new Map(...)``/``new Set(...)``. A plain
    value (a pair's value, an array element, an argument, a return, a
    template's text) is not; a specifier neither. A script that does not
    parse is taken to redirect."""
    from flag_cleanup import cross_file_ts as ts
    from flag_cleanup import ts_syntax

    try:
        root = ts_syntax._parse(data.decode("utf-8"), ts._language(str(path)))
    except UnicodeDecodeError:
        return True
    if root.has_error:
        return True
    for start, end in hits:
        node = root.descendant_for_byte_range(start, end)
        while node is not None and node.type not in ("comment", "string", "template_string", "regex", "program"):
            node = node.parent
        if node is None or node.type == "program" or node.type == "regex":
            return True
        if node.type == "comment" or (node.type == "string" and _specifier(node)):
            continue
        if _stands_like_a_redirect(node):
            return True
    return False


def _stands_like_a_redirect(string) -> bool:
    if _in_a_table(string):
        return True
    child, parent = string, string.parent
    while parent is not None and parent.type in _SEE_THROUGH:
        child, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type == "array":
        return _looked_up_in(parent)
    if parent.type == "pair":
        if parent.child_by_field_name("key") == child:
            return True
        key = parent.child_by_field_name("key")
        return key is not None and key.text.decode("utf-8", "replace").strip("'\"`") in _PATTERN_KEYS
    if parent.type in ("computed_property_name", "switch_case"):
        return True
    if parent.type == "binary_expression":
        operator = parent.child_by_field_name("operator")
        return operator is not None and operator.type in _COMPARISONS
    if parent.type == "arguments":
        call = parent.parent
        if call is not None and call.type == "new_expression":
            constructor = call.child_by_field_name("constructor")
            return constructor is not None and constructor.text == b"RegExp"
        function = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
        if function is not None and function.type == "member_expression":
            prop = function.child_by_field_name("property")
            if prop is not None and prop.text.decode("utf-8", "replace") in _TEST_METHODS:
                return True
            obj = function.child_by_field_name("object")
            return obj is not None and obj.type == "identifier" and obj.text == b"RegExp"  # RegExp(...) as a call
        return function is not None and function.type == "identifier" and function.text == b"RegExp"
    return False


def _looked_up_in(array) -> bool:
    """Whether ``array`` is the receiver of a lookup call (``[...].includes(id)``)."""
    child, parent = array, array.parent
    while parent is not None and parent.type in _SEE_THROUGH:
        child, parent = parent, parent.parent
    if parent is None or parent.type != "member_expression" or parent.child_by_field_name("object") != child:
        return False
    prop = parent.child_by_field_name("property")
    call = parent.parent
    return (
        prop is not None
        and prop.text.decode("utf-8", "replace") in _LOOKUP_METHODS
        and call is not None
        and call.type == "call_expression"
        and call.child_by_field_name("function") == parent
    )


def _in_a_table(string) -> bool:
    """Whether ``string`` stands, at any depth, inside the argument of ``new
    Map(...)`` or ``new Set(...)``."""
    node = string.parent
    while node is not None:
        if node.type == "arguments" and node.parent is not None and node.parent.type == "new_expression":
            constructor = node.parent.child_by_field_name("constructor")
            if constructor is not None and constructor.text in _TABLES:
                return True
        node = node.parent
    return False


def _specifier(string) -> bool:
    """Whether ``string`` is a module specifier: an import or export source,
    an ``import x = require()`` target, or the first argument of ``require``,
    ``import()`` or a loader member (``require.resolve``, ``vi.mock``)."""
    parent = string.parent
    if parent is None:
        return False
    if parent.type in ("import_statement", "export_statement"):
        return parent.child_by_field_name("source") == string
    if parent.type == "import_require_clause":
        return True
    if parent.type != "arguments" or not parent.named_children or parent.named_children[0] != string:
        return False
    call = parent.parent
    function = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
    if function is None:
        return False
    if function.type == "import" or (function.type == "identifier" and function.text == b"require"):
        return True
    if function.type == "member_expression":
        prop = function.child_by_field_name("property")
        return prop is not None and prop.text.decode("utf-8") in _LOADER_MEMBERS
    return False


def _linked(reader: Path, name: str, directory: Path, stop: Path) -> bool:
    """Whether every ``node_modules/<name>`` above the reader links to the
    workspace copy (none at all is the usual checkout)."""
    current = reader.parent
    while True:
        installed = current / "node_modules" / name
        if os.path.lexists(installed) and not _same(installed, directory):
            return False
        if current == stop or current.parent == current:
            return True
        current = current.parent


def _compiler_reads_package(reader: Path, specifier: str, stop: Path) -> bool:
    """Whether no compiler config from the reader's directory up to ``stop``
    can read ``specifier`` as a file, and no file answers it the way classic
    resolution looks (see the module docstring)."""
    from flag_cleanup import cross_file_ts as ts

    current = reader.parent
    while True:
        if ts._candidates(os.path.normpath(current / specifier)):
            return False
        for config in sorted(current.iterdir()):
            if not (_ANY_CONFIG.match(config.name) and config.is_file()):
                continue
            chain = _chain(config, stop)
            suffixes, _ = ts._in_force(chain, "moduleSuffixes", [""])
            if suffixes != [""]:
                return False
            options = ts._options(config, stop, strict=False)
            if options.targets(specifier) is not None:
                return False
            if options.base_url is not None and ts._candidates(os.path.normpath(options.base_url / specifier)):
                return False
        if current == stop or current.parent == current:
            return True
        current = current.parent


def _chain(config: Path, stop: Path) -> list[tuple[Path, dict]]:
    """``config``'s ``extends`` chain, read past ``references`` only: each
    config in it must still parse strictly (no repeated key, no U+2028)."""
    from flag_cleanup import cross_file_ts as ts

    try:
        chain = ts._inheriting(config, stop, (), strict=False)
    except ts._Unknown as error:
        raise _Unknown(str(config)) from error
    for path, _options in chain:
        text = path.read_text(encoding="utf-8")
        if " " in text or " " in text:
            raise _Unknown(str(path))
        try:
            json.loads(ts._strip_jsonc(text), object_pairs_hook=ts._no_repeats)
        except (ValueError, ts._Unknown) as error:
            raise _Unknown(str(path)) from error
    return chain


# --- the entry ------------------------------------------------------------------


@dataclass(frozen=True)
class _Emit:
    """Where one of the package's configs writes its output."""

    out: Path
    declarations: Path
    #: The explicit ``rootDir``; None when the config does not set one.
    root: Path | None


def _emits(directory: Path, stop: Path) -> list[_Emit]:
    """Where every ``tsconfig*.json``/``jsconfig*.json`` in the package's
    directory that emits writes its output."""
    from flag_cleanup import cross_file_ts as ts

    found: list[_Emit] = []
    for config in sorted(directory.iterdir()):
        if not (_ANY_CONFIG.match(config.name) and config.is_file()):
            continue
        chain = _chain(config, stop)
        try:
            no_emit, _ = ts._in_force(chain, "noEmit", False)
            out, out_in = ts._in_force(chain, "outDir", None)
            root, root_in = ts._in_force(chain, "rootDir", None)
            declarations, declarations_in = ts._in_force(chain, "declarationDir", None)
        except ts._Unknown as error:
            raise _Unknown(str(config)) from error
        if no_emit is True:
            continue
        if not isinstance(no_emit, bool) or out_in is None:
            if not isinstance(no_emit, bool):
                raise _Unknown(str(config))
            continue  # emits beside each source: no build directory to map
        values = [(out, out_in), (root, root_in), (declarations, declarations_in)]
        if any(declared is not None and not isinstance(value, str) for value, declared in values):
            raise _Unknown(str(config))

        def path(value: object, declared: Path | None) -> Path | None:
            if declared is None:
                return None
            try:
                return Path(os.path.normpath(ts._option_path(value, declared.parent, config, declared))).resolve()
            except ts._Unknown as error:
                raise _Unknown(str(config)) from error

        out_dir = path(out, out_in)
        found.append(_Emit(out_dir, path(declarations, declarations_in) or out_dir, path(root, root_in)))
    return found


def _entry(directory: Path, manifest: dict, subpath: str, stop: Path) -> str:
    """The one source file every entry field for ``subpath`` leads to."""
    from flag_cleanup import cross_file_ts as ts

    unknown = sorted(set(manifest) - _INERT_FIELDS)
    if unknown:
        raise _Unknown(f"{directory}: {unknown[0]}")
    leaves: list[str] = []
    exports = manifest.get("exports")
    if exports is not None:
        leaves += _leaves(_exported(exports, subpath))
    if subpath == ".":
        for field_name in _ENTRY_FIELDS:
            value = manifest.get(field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise _Unknown(field_name)
            leaves.append(value)
    elif exports is None:
        return _named_file(directory, manifest, subpath, stop)
    if not leaves:
        raise _Unknown(str(directory))
    emits = _emits(directory, stop)
    bundled = _bundled(directory, manifest, stop)
    sources: set[str] = set()
    for leaf in leaves:
        sources.add(_source_of(directory, leaf, emits, bundled))
    if subpath != ".":
        # A resolver that ignores `exports` looks the subpath up as a path.
        legacy = ts._candidates(os.path.normpath(directory / subpath))
    elif "main" not in manifest:
        # ...and the package itself up as its `index`.
        legacy = ts._candidates(os.path.normpath(directory / "index"))
    else:
        legacy = []
    if legacy and {str(Path(h).resolve()) for h in legacy} != sources:
        raise _Unknown(subpath)
    if len(sources) != 1:
        raise _Unknown(str(directory))
    return sources.pop()


#: Scripts that build or publish the package, with their npm hooks.
_BUILD_SCRIPT = re.compile(
    r"^(?:pre|post)?(?:build|compile|prepare|prepublish|prepublishOnly|prepack)(?:[:_\-].*)?$", re.IGNORECASE
)
#: Commands a tsc build may run around ``tsc`` that write no build output.
_BESIDE_TSC = frozenset(
    {
        "rimraf", "rm", "del", "del-cli", "shx", "mkdir", "mkdirp", "echo", "true", "madge", "eslint", "biome",
        "prettier", "tsc-alias", "tsc", "tspc",
    }
)
#: An Nx executor of a bundler (``@nx/esbuild:esbuild``, ``@nx/vite:build``).
_BUNDLER_EXECUTOR = re.compile(
    r"vite|webpack|esbuild|rollup|rspack|rsbuild|next|parcel|tsup|swc|babel|expo|metro|react-native|angular|remix",
    re.IGNORECASE,
)
#: Nx executors that build with ``tsc``, or run the package's own scripts.
_TSC_EXECUTORS = frozenset({"@nx/js:tsc", "@nrwl/js:tsc", "nx:run-script", "@nrwl/workspace:run-script"})


def _bundled(directory: Path, manifest: dict, stop: Path) -> bool:
    """Whether something other than ``tsc`` may build the package, so its
    output says nothing about which source it was built from. An ALLOWLIST:
    at least one build script must run ``tsc`` (or an Nx build target use a
    ``tsc`` executor), every build script of the package (``build*``,
    ``compile*``, ``prepare``, ``prepublish*``, ``prepack`` and their
    ``pre``/``post`` hooks) must be a plain ``tsc`` run, with only :data:`_BESIDE_TSC` around it and other such
    scripts by ``npm run``; every Nx build target (the manifest's ``nx``,
    ``project.json``, and ``nx.json`` ``targetDefaults``) a ``tsc`` executor;
    and no bundler config may sit in the package's directory or at the root
    of a workspace holding it, except a ``vite.config`` that only configures
    Vitest (it imports ``vitest/config`` and nothing runs ``vite``)."""
    for entry in directory.iterdir():
        if _BUNDLER_CONFIG.match(entry.name) or entry.name.lower().startswith("webpack."):
            return True
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict) or any(not isinstance(v, str) for v in scripts.values()):
        return True
    built = False  # positive evidence that tsc is the build
    for name in scripts:
        if _BUILD_SCRIPT.match(name):
            if not _plain_tsc(name, scripts, set()):
                return True
            built = built or _runs_tsc(name, scripts, set())
    targets = [manifest.get("nx")]
    project = directory / "project.json"
    if project.exists():
        try:
            targets.append(json.loads(_read(project) or ""))
        except ValueError:
            return True
    own = len(targets)
    nx = stop / "nx.json"
    if nx.exists():
        try:
            data = json.loads(_read(nx) or "")
        except ValueError:
            return True
        targets.append({"targets": data.get("targetDefaults", {})} if isinstance(data, dict) else None)
    for block in targets:
        if block is None:
            continue
        if not isinstance(block, dict) or not _tsc_targets(block.get("targets", {})):
            return True
    # The package's own targets (not `targetDefaults`) can say tsc builds it.
    built = built or any(isinstance(b, dict) and _tsc_executor(b.get("targets", {})) for b in targets[:own])
    if not built:
        return True  # no build of the package says it is tsc's
    return _root_bundler(directory, stop)


def _root_bundler(directory: Path, stop: Path) -> bool:
    """Whether a bundler config at the root of a workspace holding the
    package (or of the checkout) may build it: any but a Vitest-only one."""
    roots = [r for r in _checkout(stop).roots if directory.is_relative_to(r)] + [stop]
    for root in roots:
        for entry in root.iterdir() if root.is_dir() else ():
            if not (_BUNDLER_CONFIG.match(entry.name) or entry.name.lower().startswith("webpack.")):
                continue
            if not _vitest_only(entry, root):
                return True
    return False


#: TypeScript source, which no bundler emits.
_TYPESCRIPT_SOURCE = (".ts", ".tsx", ".mts", ".cts")


def _typescript_source(path: str | None) -> bool:
    return path is not None and path.endswith(_TYPESCRIPT_SOURCE) and not path.endswith(tuple(_DECLARATION_SUFFIXES))


def _unbuilt(directory: Path, manifest: dict, stop: Path) -> bool:
    """Whether nothing builds the package at all: no build script (by
    :data:`_BUILD_SCRIPT`'s names), no Nx project or workspace, no bundler
    config in its directory or at a root holding it. Then what a bare
    specifier loads is what its manifest names, and when that is source no
    bundle stands between."""
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict) or any(_BUILD_SCRIPT.match(str(name)) for name in scripts):
        return False
    if "nx" in manifest or (directory / "project.json").exists() or (stop / "nx.json").exists():
        return False
    for entry in directory.iterdir():
        if _BUNDLER_CONFIG.match(entry.name) or entry.name.lower().startswith("webpack."):
            return False
    return not _root_bundler(directory, stop)


def _runs_tsc(name: str, scripts: dict, seen: set[str]) -> bool:
    """Whether script ``name`` (through ``npm run`` chains) invokes ``tsc``."""
    if name in seen or not isinstance(scripts.get(name), str):
        return False
    seen.add(name)
    for segment in re.split(r"&&|\|\||;|\|", scripts[name]):
        words = [w.rsplit("/", 1)[-1] for w in segment.split()]
        if "tsc" in words or "tspc" in words:
            return True
        if words and words[0] in ("npm", "pnpm", "yarn", "bun") and len(words) > 1:
            target = words[2] if words[1] == "run" and len(words) > 2 else words[1]
            if _runs_tsc(target, scripts, seen):
                return True
    return False


def _tsc_executor(targets: object) -> bool:
    """Whether a build target of ``targets`` uses a ``tsc`` executor."""
    return isinstance(targets, dict) and any(
        isinstance(t, dict) and t.get("executor") in ("@nx/js:tsc", "@nrwl/js:tsc") and _BUILD_SCRIPT.match(str(n))
        for n, t in targets.items()
    )


def _plain_tsc(name: str, scripts: dict, seen: set[str]) -> bool:
    """Whether script ``name`` only runs ``tsc`` and :data:`_BESIDE_TSC`."""
    if name in seen:
        return True
    seen.add(name)
    command = scripts.get(name)
    if not isinstance(command, str):
        return False
    for segment in re.split(r"&&|\|\||;|\|", command):
        words = segment.split()
        while words and (re.fullmatch(r"[A-Za-z_][\w]*=\S*", words[0]) or words[0] in ("cross-env", "npx", "exec")):
            words = words[1:]
        if words[:2] in (["pnpm", "exec"], ["yarn", "exec"]):
            words = words[2:]
        if not words:
            continue
        program = words[0].rsplit("/", 1)[-1]
        if program in ("npm", "pnpm", "yarn", "bun") and len(words) > 1:
            target = words[2] if words[1] == "run" and len(words) > 2 else words[1]
            if target not in scripts or not _plain_tsc(target, scripts, seen):
                return False
            continue
        if program not in _BESIDE_TSC:
            return False
    return True


def _tsc_targets(targets: object) -> bool:
    """Whether every Nx build target of ``targets`` builds with ``tsc``."""
    if not isinstance(targets, dict):
        return False
    for name, target in targets.items():
        if ":" in str(name):
            # `targetDefaults` keyed by executor: a bundler's, wherever it runs.
            if name not in _TSC_EXECUTORS and _BUNDLER_EXECUTOR.search(str(name)):
                return False
            continue
        if not _BUILD_SCRIPT.match(str(name)):
            continue
        if not isinstance(target, dict):
            return False
        executor = target.get("executor")
        if executor is None or executor in _TSC_EXECUTORS:
            continue
        if executor in ("nx:run-commands", "@nrwl/workspace:run-commands"):
            options = target.get("options", {})
            commands = options.get("commands", []) if isinstance(options, dict) else None
            single = options.get("command") if isinstance(options, dict) else None
            listed = [single] if single is not None else commands
            if not isinstance(listed, list) or not all(
                isinstance(c, str) and _plain_tsc("c", {"c": c}, set()) for c in listed
            ):
                return False
            continue
        return False
    return True


def _vitest_only(config: Path, root: Path) -> bool:
    """Whether a root ``vite.*config`` only configures Vitest: it imports
    ``vitest/config`` and not ``vite``, and no root script runs ``vite``."""
    if not config.name.lower().startswith("vite."):
        return False
    text = _read(config) or ""
    if not re.search(r"""["']vitest/config["']""", text) or re.search(r"""["']vite["']""", text):
        return False
    try:
        manifest = json.loads(_read(root / "package.json") or "{}")
    except ValueError:
        return False
    scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
    return isinstance(scripts, dict) and not any(
        isinstance(v, str) and re.search(r"(?:^|[\s;&|(/])vite(?=\s|$)", v) for v in scripts.values()
    )


def _named_file(directory: Path, manifest: dict, subpath: str, stop: Path) -> str:
    """The source file a subpath names in a package without ``exports``: with
    nothing to map it, Node, TypeScript and a bundler all look it up as a
    path in the package. A ``browser`` or ``react-native`` map may send it
    elsewhere for one bundler, so either one refuses; so does a path naming
    no file, or more than one (``keys.ts`` beside ``keys.js``, a
    ``keys.json`` a bundler may try first), a declaration file, or a build
    output of the package's own configs."""
    from flag_cleanup import cross_file_ts as ts

    if any(not isinstance(manifest.get(f), (str, type(None))) for f in _ENTRY_FIELDS):
        raise _Unknown(subpath)
    try:
        hits = ts._candidates(os.path.normpath(directory / subpath))
    except ts._Unknown as error:
        raise _Unknown(subpath) from error
    if len(hits) != 1 or not hits[0].endswith(_SCRIPT_SUFFIXES) or hits[0].endswith(tuple(_DECLARATION_SUFFIXES)):
        raise _Unknown(subpath)
    hit = Path(hits[0]).resolve()
    written = Path(os.path.normpath(directory / subpath))
    if written.is_file() and written.resolve() != hit:
        raise _Unknown(subpath)  # Node and bundlers try the path as written first
    if not hit.is_relative_to(directory.resolve()) or any(
        hit.is_relative_to(e.out) or hit.is_relative_to(e.declarations) for e in _emits(directory, stop)
    ):
        raise _Unknown(subpath)
    if not _typescript_source(str(hit)) and _bundled(directory, manifest, stop):
        raise _Unknown(subpath)  # a committed file some bundler may rewrite
    return str(hit)


def _exported(exports: object, subpath: str) -> object:
    """The ``exports`` target for ``subpath``: a subpath map's exact key, or
    the whole value for ``.`` when it is a string, a list or a conditions
    object. A pattern key that could match raises, and so does a missing one."""
    if isinstance(exports, dict) and exports and all(isinstance(k, str) and k.startswith(".") for k in exports):
        if subpath in exports:
            return exports[subpath]
        if any("*" in key for key in exports):
            raise _Unknown(subpath)
        raise _Unknown(subpath)
    if isinstance(exports, dict) and any(isinstance(k, str) and k.startswith(".") for k in exports):
        raise _Unknown(subpath)  # keys of both kinds: invalid
    if subpath != ".":
        raise _Unknown(subpath)
    return exports


def _leaves(target: object) -> list[str]:
    """Every path a conditions tree can choose; any ``null`` or other value
    raises (it blocks the subpath for some condition)."""
    if isinstance(target, str):
        return [target]
    if isinstance(target, list) and target:
        return [leaf for item in target for leaf in _leaves(item)]
    if isinstance(target, dict) and target:
        if any(not isinstance(k, str) or k.startswith(".") for k in target):
            raise _Unknown("nested subpath")
        return [leaf for value in target.values() for leaf in _leaves(value)]
    raise _Unknown(repr(target))


def _source_of(directory: Path, leaf: str, emits: list[_Emit], bundled: bool = False) -> str:
    """The source file ``leaf`` (a path in the package) is, or is built from.
    ``bundled``: a bundler may build the package, so only a leaf that is a
    source file itself counts."""
    from flag_cleanup import cross_file_ts as ts

    written = leaf[2:] if leaf.startswith("./") else leaf
    if not written or written.startswith("/") or "*" in written or "\\" in written:
        raise _Unknown(leaf)
    path = Path(os.path.normpath(directory / written))
    if not path.is_relative_to(directory) or path == directory:
        raise _Unknown(leaf)
    declaration = next((s for s in _DECLARATION_SUFFIXES if path.name.endswith(s)), None)
    resolved = path.resolve()
    under = [e for e in emits if resolved.is_relative_to(e.declarations if declaration else e.out)]
    if path.exists():
        if under or not path.is_file():
            raise _Unknown(leaf)  # a committed build output, or a directory
        if declaration is not None:
            runs = Path(str(path)[: -len(declaration)] + _DECLARATION_SUFFIXES[declaration])
            if not runs.is_file():
                raise _Unknown(leaf)
            return str(runs.resolve())
        if not path.name.endswith(_SCRIPT_SUFFIXES):
            raise _Unknown(leaf)
        return str(resolved)
    if not under or bundled:
        raise _Unknown(leaf)
    sources: set[str] = set()
    for emit in under:
        if emit.root is None:
            raise _Unknown(leaf)  # the compiler's computed root is not read here
        relative = resolved.relative_to(emit.declarations if declaration else emit.out).as_posix()
        if declaration is not None:
            relative = relative[: -len(declaration)] + _DECLARATION_SUFFIXES[declaration]
        if not relative.endswith((".js", ".jsx", ".mjs", ".cjs")):
            raise _Unknown(leaf)
        try:
            hits = ts._candidates(os.path.normpath(emit.root / relative))
        except ts._Unknown as error:
            raise _Unknown(leaf) from error
        if len(hits) != 1 or hits[0].endswith((".d.ts", ".d.mts", ".d.cts")):
            raise _Unknown(leaf)
        hit = Path(hits[0]).resolve()
        if (
            not hit.is_relative_to(emit.root)
            or not hit.is_relative_to(directory.resolve())  # what `holding` scans for
            or any(hit.is_relative_to(e.out) for e in emits)
        ):
            raise _Unknown(leaf)
        sources.add(str(hit))
    if len(sources) != 1:
        raise _Unknown(leaf)
    return sources.pop()
