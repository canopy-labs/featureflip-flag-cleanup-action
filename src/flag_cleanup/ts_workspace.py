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
from flag_cleanup.cross_file_ts import _Unknown  # noqa: E402


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
    bundler may build (:func:`_bundled`), or whose manifest cannot be read:
    what it loads at run time may then do more than its source shows."""
    from flag_cleanup import source_index

    name = package_part(specifier)
    if name is None:
        return False
    stop = Path(source_index.checkout_root(repo_dir)).resolve()
    checkout = _checkout(stop)
    for directory in _named(checkout, name):
        manifest = checkout.manifests.get(directory)
        try:
            if not isinstance(manifest, dict) or _bundled(directory, manifest, stop):
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
    if not _follow.get():
        return None  # `workspace-packages: off`
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
        if node.type != "comment" and not _specifier(node):
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
        raise _Unknown(subpath)
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
    roots = [r for r in _checkout(stop).roots if directory.is_relative_to(r)] + [stop]
    for root in roots:
        for entry in root.iterdir() if root.is_dir() else ():
            if not (_BUNDLER_CONFIG.match(entry.name) or entry.name.lower().startswith("webpack.")):
                continue
            if not _vitest_only(entry, root):
                return True
    return False


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
