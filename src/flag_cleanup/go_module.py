"""Go modules: which import path names a directory, and back.

A Go import path is the ``module`` directive of the nearest ``go.mod`` at or
above a package's directory, joined with the directory's path relative to
that ``go.mod``. That is all :func:`module_of` reads.

An import of ANOTHER module is resolved by :func:`resolve`, which reads the
importing module's ``go.mod`` (``require``, ``replace``, ``exclude``) and
every ``go.work`` in the checkout that uses the module. The same source can be
built two ways, and both are asked: without a workspace (``GOWORK=off``, or a
container build that copies one module), where the ``go.mod`` decides, and in
each workspace that uses the module, where the workspace's modules come first.
An import is resolved only when every way that builds it builds the SAME
directory of this checkout. A module required at a version nothing replaces
with a directory here may be a published copy, and any doubt (a ``vendor``
directory, a second replacement or a versioned one this module may not
select, a replacement outside the checkout, a module path two directories
declare, two modules that could provide the package, a file this reader
cannot parse) leaves it unresolved, which every caller treats as a refusal.

:func:`may_read` is the other direction: every directory an import MIGHT
mean, for deciding which reads must be judged at all. A read it could mean is
refused unless :func:`resolve` proves it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

#: ``module example.com/app`` or ``module "example.com/app"``, with an
#: optional trailing comment. The parenthesised form is legal and unheard of;
#: it reads as no module, which refuses.
_MODULE = re.compile(r'^[ \t]*module[ \t]+("?)([^\s"]+)\1[ \t]*(?://.*)?$', re.MULTILINE)


@dataclass(frozen=True)
class Module:
    go_mod: Path
    #: The ``module`` directive.
    path: str

    @property
    def root(self) -> Path:
        return self.go_mod.parent

    def import_path(self, directory: Path) -> str | None:
        try:
            rel = directory.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None
        return self.path if rel == "." else f"{self.path}/{rel}"

    def directory(self, import_path: str) -> Path | None:
        """The directory ``import_path`` names inside this module, existing or
        not; ``None`` when the path is not this module's."""
        if import_path == self.path:
            return self.root
        if not import_path.startswith(self.path + "/"):
            return None
        return self.root / import_path[len(self.path) + 1 :]


def module_of(directory: Path, stop: Path) -> Module | None:
    """The module whose nearest ``go.mod`` holds ``directory``, looking no
    higher than ``stop``. An unreadable ``go.mod`` still ends the search: it
    is the module boundary whether or not its path can be read."""
    current = directory.resolve()
    stop = stop.resolve()
    while True:
        candidate = current / "go.mod"
        if candidate.is_file():
            path = _module_path(candidate)
            return None if path is None else Module(candidate, path)
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _module_path(go_mod: Path) -> str | None:
    try:
        text = go_mod.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = [match.group(2) for match in _MODULE.finditer(text)]
    return found[0] if len(found) == 1 else None


def package_clause(root: Node) -> str | None:
    """The name in the file's ``package`` clause."""
    for child in root.named_children:
        if child.type == "package_clause":
            name = next((c for c in child.named_children if c.type == "package_identifier"), None)
            return None if name is None else name.text.decode("utf-8", "replace")
    return None


# --- another module: go.mod, go.work ------------------------------------------

#: One token of a ``go.mod`` or ``go.work`` line: a quoted or raw string, a
#: comment (the rest of the line), a parenthesis, the replace arrow or a word.
_TOKEN = re.compile(r'(?P<comment>//.*)|(?P<quoted>"(?:[^"\\\n]|\\.)*")|(?P<raw>`[^`\n]*`)|(?P<word>[()]|=>|(?:[^\s()"`/=]|/(?!/)|=(?!>))+)|(?P<other>\S)')

#: Directives a ``go.mod`` may hold; any other is not read, so it refuses.
_MOD_VERBS = frozenset({"module", "go", "toolchain", "godebug", "require", "replace", "exclude", "retract", "tool", "ignore"})
_WORK_VERBS = frozenset({"go", "toolchain", "godebug", "use", "replace"})


@dataclass(frozen=True)
class _Replace:
    old: str
    #: ``None`` replaces every version.
    old_version: str | None
    new: str
    new_version: str | None

    @property
    def local(self) -> bool:
        """A directory on the right, which is what Go calls a path that is
        absolute or starts ``./`` or ``../``."""
        return self.new_version is None and (
            self.new in (".", "..") or self.new.startswith(("./", "../", "/"))
        )


@dataclass
class _File:
    """A parsed ``go.mod`` or ``go.work``."""

    path: Path
    module: str | None = None
    go: tuple[int, ...] | None = None
    requires: dict[str, str] = field(default_factory=dict)
    replaces: list[_Replace] = field(default_factory=list)
    excludes: set[str] = field(default_factory=set)
    uses: list[str] = field(default_factory=list)


def _lines(text: str) -> list[list[str]] | None:
    found: list[list[str]] = []
    for line in text.splitlines():
        tokens: list[str] = []
        for match in _TOKEN.finditer(line):
            kind, token = match.lastgroup, match.group(0)
            if kind == "comment":
                break
            if kind == "other":
                return None  # a character this reader does not know
            if kind == "quoted":
                if "\\" in token:
                    return None
                token = token[1:-1]
            elif kind == "raw":
                token = token[1:-1]
            tokens.append(token)
        if tokens:
            found.append(tokens)
    return found


def _directives(text: str, verbs: frozenset[str]) -> list[tuple[str, list[str]]] | None:
    """``(verb, arguments)`` for each directive, a block's lines one by one."""
    lines = _lines(text)
    if lines is None:
        return None
    out: list[tuple[str, list[str]]] = []
    block: str | None = None
    for tokens in lines:
        if block is not None:
            if tokens == [")"]:
                block = None
            elif "(" in tokens or ")" in tokens:
                return None
            else:
                out.append((block, tokens))
            continue
        verb, rest = tokens[0], tokens[1:]
        if verb not in verbs:
            return None
        if rest == ["("]:
            block = verb
        elif rest == ["(", ")"]:
            continue
        elif "(" in rest or ")" in rest:
            return None
        else:
            out.append((verb, rest))
    return None if block is not None else out


def _version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\S*)?", text)
    return None if match is None else tuple(int(g) for g in match.groups() if g is not None)


def _replace(args: list[str]) -> _Replace | None:
    if "=>" not in args:
        return None
    at = args.index("=>")
    left, right = args[:at], args[at + 1 :]
    if len(left) not in (1, 2) or len(right) not in (1, 2):
        return None
    return _Replace(left[0], left[1] if len(left) == 2 else None, right[0], right[1] if len(right) == 2 else None)


def _parse(path: Path, verbs: frozenset[str]) -> _File | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    directives = _directives(text, verbs)
    if directives is None:
        return None
    parsed = _File(path)
    for verb, args in directives:
        if verb == "module":
            if len(args) != 1 or parsed.module is not None:
                return None
            parsed.module = args[0]
        elif verb == "go":
            if len(args) != 1 or (version := _version(args[0])) is None:
                return None
            parsed.go = version
        elif verb == "require":
            if len(args) != 2 or parsed.requires.get(args[0], args[1]) != args[1]:
                return None
            parsed.requires[args[0]] = args[1]
        elif verb == "replace":
            replace = _replace(args)
            if replace is None:
                return None
            parsed.replaces.append(replace)
        elif verb == "exclude":
            if len(args) != 2:
                return None
            parsed.excludes.add(args[0])
        elif verb == "use":
            if len(args) != 1:
                return None
            parsed.uses.append(args[0])
    return parsed


def _memo() -> dict:
    from flag_cleanup import source_index

    memo = source_index.run_memo("go_modules")
    return {} if memo is None else memo


def _go_mod(path: Path) -> _File | None:
    memo = _memo()
    key = ("mod", path)
    if key not in memo:
        memo[key] = _parse(path, _MOD_VERBS)
    return memo[key]


def _go_work(path: Path) -> _File | None:
    memo = _memo()
    key = ("work", path)
    if key not in memo:
        memo[key] = _parse(path, _WORK_VERBS)
    return memo[key]


def _workspace_files(stop: Path) -> list[Path]:
    """Every workspace file in the checkout: Go reads the ``go.work`` nearest
    the directory a build runs in, and ``GOWORK`` can name any other file. Any
    ``*.work``, and any other file that looks like one by name (``go.work.ci``,
    ``ci.work.local``) and holds a ``go`` or ``use`` directive. A file named
    otherwise (``GOWORK=ci.txt``) is not seen."""
    return _build_files(stop)[1]


def _opaque_files(stop: Path) -> list[Path]:
    """Files that may be a ``-modfile`` or a workspace file by name, but that
    this reader cannot read at all: what they would change is unknown."""
    return _build_files(stop)[2]


#: A line that starts a ``go.mod`` ``module`` directive, or a workspace
#: file's ``go`` or ``use`` one.
_MODULE_LINE = re.compile(r"^[ \t]*module\b", re.MULTILINE)
_WORK_LINE = re.compile(r"^[ \t]*(?:go|use)\b", re.MULTILINE)


#: ``work`` as a whole dot-separated part of a name (``ci.work.local``), not
#: the start of a longer word: ``foo.worker.js`` and ``ci.workflow.yml`` name
#: no workspace file.
_WORK_PART = re.compile(r"\.work(?:\.|$)")

#: A web or script source file is never a workspace file, however it is named.
_NOT_A_WORKFILE_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".map", ".json", ".css", ".html", ".vue", ".svelte"}
)


def _loose_workfile_name(name: str) -> bool:
    """Whether a name that does not end in ``.work`` may still be a workspace
    file (``GOWORK`` takes any name): ``go.work.ci``, ``ci.work.local``."""
    if name == "go.work.sum" or os.path.splitext(name)[1].lower() in _NOT_A_WORKFILE_SUFFIXES:
        return False
    return name.startswith("go.work") or _WORK_PART.search(name) is not None


def _build_files(stop: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """``(modfiles, workfiles, opaque)`` of the checkout, walked once per run.

    Modfiles: each ``go.mod``, and every other ``*.mod`` holding a ``module``
    directive anywhere (Go takes directives in any order, and ``-modfile``
    requires the ``.mod`` suffix, so the suffix scan is complete). Workfiles:
    see :func:`_workspace_files`; ``go.work.sum`` is a checksum file. Opaque: a
    candidate of either kind that cannot be decoded, or that holds no such
    directive and does not tokenize."""
    from flag_cleanup.piranha_runner import _SKIP_DIRS, _is_go_vendor_tree

    memo = _memo()
    key = ("build files", stop)
    if key in memo:
        return memo[key]
    mods: list[Path] = []
    works: list[Path] = []
    opaque: list[Path] = []
    for directory, dirnames, filenames in os.walk(stop):
        here = Path(directory)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not _is_go_vendor_tree(here / d))
        for name in sorted(filenames):
            path = here / name
            if name == "go.mod":
                mods.append(path)
                continue
            is_mod = name.endswith(".mod")
            is_work = name.endswith(".work")
            loose = not is_work and _loose_workfile_name(name)
            if not (is_mod or is_work or loose):
                continue
            if is_work:
                works.append(path)
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                opaque.append(path)
                continue
            if is_mod and _MODULE_LINE.search(text):
                mods.append(path)
            elif loose and _WORK_LINE.search(text):
                works.append(path)
            elif _lines(text) is None:
                opaque.append(path)
    memo[key] = (mods, works, opaque)
    return memo[key]


#: What resolving a path can raise: a missing or unreadable directory, and a
#: symlink loop (``RuntimeError`` on Python 3.12).
_PATH_ERRORS = (OSError, RuntimeError)

#: An import nothing in this checkout resolves the same way under every build.
_UNRESOLVED = "unresolved"
#: An import a build cannot resolve at all: no module of the build provides
#: it. That build fails, which is no evidence either way.
_MISSING = "missing"


def _under(path: Path, stop: Path) -> bool:
    try:
        path.relative_to(stop)
    except ValueError:
        return False
    return True


def _covers(module_path: str, import_path: str) -> bool:
    return import_path == module_path or import_path.startswith(module_path + "/")


def _provided(root: Path, module_path: str, import_path: str, stop: Path) -> Path | None:
    """The directory of ``import_path`` in the module at ``root``, when that
    module holds it: it exists, and no nested ``go.mod`` takes it."""
    rest = import_path[len(module_path) + 1 :] if import_path != module_path else ""
    directory = root / rest if rest else root
    try:
        if not directory.is_dir():
            return None
        directory = directory.resolve()
    except _PATH_ERRORS:
        return None
    owner = module_of(directory, stop)
    return directory if owner is not None and owner.go_mod == (root / "go.mod").resolve() else None


def _local_root(base: Path, replace: _Replace, module_path: str, stop: Path) -> Path | str:
    """The checkout directory a local replacement names, or why it is none."""
    try:
        root = (base / replace.new).resolve()
    except _PATH_ERRORS:
        return _UNRESOLVED
    if not _under(root, stop):
        return _UNRESOLVED
    declared = _module_path(root / "go.mod") if (root / "go.mod").is_file() else None
    return root if declared == module_path else _UNRESOLVED


def _choose(results: list[Path | str]) -> Path | str:
    if _UNRESOLVED in results:
        return _UNRESOLVED
    found = {r for r in results if isinstance(r, Path)}
    if len(found) > 1:
        return _UNRESOLVED  # two modules provide it: Go calls the import ambiguous
    return found.pop() if found else _MISSING


def _only_replacement(mod: _File, path: str, stop: Path, versioned: bool = True) -> _Replace | None:
    """The one replacement of ``path`` in ``mod`` a build is sure to apply, to
    a directory: a single versionless one, or a single versioned one of
    exactly the version ``mod`` requires when ``mod`` says ``go 1.17`` or
    later (a pruned module graph lists the version selected) and no other
    ``go.mod`` of the checkout requires the path at another version (local
    evidence the selected version can rise past it). Two replacements of the
    path, or a versioned one of another version, are in doubt.

    ``versioned=False`` takes no versioned one: a workspace checks no module's
    ``go.mod`` against the workspace's module graph, so a dependency another
    module of it requires can raise the version past the replaced one with
    every ``go.mod`` tidy and the default ``-mod=readonly``.

    What a versioned one leaves open: a committed ``go.mod`` that already
    fails the default ``-mod=readonly`` build (a requirement outside the
    checkout selects a higher version), built anyway with ``-mod=mod`` or after
    ``go mod tidy``, with this module path published at that version. Even
    then the build reads the published copy, which still declares what the
    removal deletes here, so the result compiles."""
    found = [r for r in mod.replaces if r.old == path]
    if len(found) != 1 or not found[0].local:
        return None
    replace = found[0]
    if replace.old_version is None:
        return replace
    if not versioned or replace.old_version != mod.requires.get(path) or mod.go is None or mod.go < (1, 17):
        return None
    others = _required_versions(stop)
    if others is None or others.get(path, set()) - {replace.old_version}:
        return None
    return replace


def _required_versions(stop: Path) -> dict[str, set[str]] | None:
    """Every version any ``go.mod`` (or ``-modfile`` alternate) of the
    checkout requires each path at;
    ``None`` when one of them cannot be read."""
    found: dict[str, set[str]] = {}
    for path in _mod_files(stop):
        mod = _go_mod(path)
        if mod is None:
            return None
        for required, version in mod.requires.items():
            found.setdefault(required, set()).add(version)
    return found


def _checkout_go_mods(stop: Path) -> list[Path]:
    """Every ``go.mod`` in the checkout."""
    return [p for p in _mod_files(stop) if p.name == "go.mod"]


def _mod_files(stop: Path) -> list[Path]:
    """Every file a build may read as a module's ``go.mod``: each ``go.mod``,
    and any other ``*.mod`` with a ``module`` directive, which ``-modfile``
    can name in its place (see :func:`_build_files`)."""
    return _build_files(stop)[0]


def _module_mode(import_path: str, module: Module, stop: Path) -> Path | str:
    """What a build without a workspace makes of ``import_path``."""
    mod = _go_mod(module.go_mod)
    if mod is None or (module.root / "vendor").exists():
        return _UNRESOLVED
    results: list[Path | str] = []
    for path in mod.requires:
        if path == module.path or not _covers(path, import_path):
            continue
        if path in mod.excludes:
            results.append(_UNRESOLVED)
            continue
        replaced = _only_replacement(mod, path, stop)
        if replaced is None:
            results.append(_UNRESOLVED)  # a published copy, or one elsewhere
            continue
        root = _local_root(module.root, replaced, path, stop)
        results.append(root if isinstance(root, str) else (_provided(root, path, import_path, stop) or _MISSING))
    return _choose(results)


def _workspace_mode(import_path: str, work: _File, stop: Path) -> Path | str:
    """What the workspace ``work`` makes of ``import_path``."""
    base = work.path.parent
    if (base / "vendor").exists():
        return _UNRESOLVED
    members: dict[str, Path] = {}
    for use in work.uses:
        try:
            root = (base / use).resolve()
        except _PATH_ERRORS:
            return _UNRESOLVED
        path = _module_path(root / "go.mod") if (root / "go.mod").is_file() else None
        if path is None or not _under(root, stop) or members.get(path, root) != root:
            return _UNRESOLVED
        members[path] = root
    results: list[Path | str] = []
    replaced: set[str] = set()
    for replace in work.replaces:
        if not _covers(replace.old, import_path):
            continue
        replaced.add(replace.old)
        if replace.old_version is not None or not replace.local or replace.old in members:
            results.append(_UNRESOLVED)
            continue
        root = _local_root(base, replace, replace.old, stop)
        results.append(root if isinstance(root, str) else (_provided(root, replace.old, import_path, stop) or _MISSING))
    for path, root in members.items():
        if _covers(path, import_path):
            results.append(_provided(root, path, import_path, stop) or _MISSING)
    # A requirement of any workspace module that is not one itself may
    # provide the package too, from wherever the modules' ``go.mod`` files
    # replace it: the workspace applies them, and they must agree.
    mods: list[tuple[Path, _File]] = []
    for root in members.values():
        mod = _go_mod(root / "go.mod")
        if mod is None:
            return _UNRESOLVED
        mods.append((root, mod))
    required = {path for _, mod in mods for path in mod.requires}
    for path in sorted(required):
        if path in members or path in replaced or not _covers(path, import_path):
            continue
        roots: set[Path | str] = set()
        for root, mod in mods:
            if any(r.old == path for r in mod.replaces):
                chosen = _only_replacement(mod, path, stop, versioned=False)
                roots.add(_UNRESOLVED if chosen is None else _local_root(root, chosen, path, stop))
        if len(roots) != 1 or _UNRESOLVED in roots:
            results.append(_UNRESOLVED)  # published, or replaced in doubt
            continue
        local = roots.pop()
        assert isinstance(local, Path)
        results.append(_provided(local, path, import_path, stop) or _MISSING)
    return _choose(results)


def resolve(import_path: str, module: Module, stop: Path) -> Path | None:
    """The directory of this checkout that an import of ``import_path`` in a
    package of ``module`` builds, resolved; ``None`` when that is not proven.

    A package of the importing module itself is that module's, as
    :meth:`Module.directory` spells it. Anything else is asked of the module's
    ``go.mod`` and of every ``go.work`` in the checkout that uses the module
    (see the module docstring), and resolved only when none of them is in
    doubt and every one that provides the package provides the same directory.
    """
    stop = stop.resolve()
    own = module.directory(import_path)
    if own is not None:
        found = _provided(module.root, module.path, import_path, stop)
        if found is not None:
            return found
    memo = _memo()
    key = ("resolve", import_path, module.go_mod, stop)
    if key not in memo:
        memo[key] = _elsewhere(import_path, module, stop)
    return memo[key]


def _declared_paths(stop: Path) -> dict[str, int]:
    """How many ``go.mod`` files in the checkout declare each module path."""
    memo = _memo()
    key = ("declared", stop)
    if key not in memo:
        counts: dict[str, int] = {}
        for path in _checkout_go_mods(stop):
            if (declared := _module_path(path)) is not None:
                counts[declared] = counts.get(declared, 0) + 1
        memo[key] = counts
    return memo[key]


def _elsewhere(import_path: str, module: Module, stop: Path) -> Path | None:
    # Two directories declaring a module path the import may come from: a
    # build that reads another workspace file could pick the other copy.
    if any(n > 1 and _covers(path, import_path) for path, n in _declared_paths(stop).items()):
        return None
    # A ``-modfile`` alternate stands in for some module's go.mod, and which
    # one is not written down: one that names a covering path at all
    # (``require`` or ``replace``), or that cannot be read, is doubt.
    for path in _mod_files(stop):
        if path.name == "go.mod":
            continue
        alternate = _go_mod(path)
        if alternate is None or any(_covers(p, import_path) for p in alternate.requires) or any(
            _covers(r.old, import_path) for r in alternate.replaces
        ):
            return None
    results = [_module_mode(import_path, module, stop)]
    root = module.root
    for path in _workspace_files(stop):
        work = _go_work(path)
        if work is None:
            results.append(_UNRESOLVED)
            continue
        try:
            used = {(path.parent / use).resolve() for use in work.uses}
        except _PATH_ERRORS:
            results.append(_UNRESOLVED)
            continue
        if root in used:
            results.append(_workspace_mode(import_path, work, stop))
    found = _choose(results)
    return found if isinstance(found, Path) else None


def may_read(import_path: str, module: Module, stop: Path) -> set[Path] | None:
    """Every checkout directory some build of ``module`` might read for
    ``import_path``, resolved: an over-approximation for deciding whether an
    import MAY name a package, where :func:`resolve` decides whether it surely
    does. The module's own spelling of it (a symlink included), each local
    ``replace`` that covers it in the module's ``go.mod``, in any ``-modfile``
    alternate, in any workspace file that uses the module or in that
    workspace's modules, and each such workspace's modules that cover it,
    whatever the replacement's own ``go.mod`` says.

    ``None`` means unknown: a file it consults cannot be read, or a path
    cannot be resolved. A caller must then treat the import as possibly any
    package, since dropping what it cannot read would skip a read."""
    stop = stop.resolve()
    found: set[Path] = set()
    unknown = False

    def add(root: Path, module_path: str) -> None:
        nonlocal unknown
        rest = import_path[len(module_path) + 1 :] if import_path != module_path else ""
        try:
            found.add((root / rest).resolve() if rest else root.resolve())
        except _PATH_ERRORS:
            unknown = True

    def replaced(bases: tuple[Path, ...], replaces: list[_Replace]) -> None:
        for replace in replaces:
            if replace.local and _covers(replace.old, import_path):
                for base in bases:
                    add(base / replace.new, replace.old)

    if _covers(module.path, import_path):
        add(module.root, module.path)
    mod = _go_mod(module.go_mod)
    if mod is None or _opaque_files(stop):
        return None
    replaced((module.root,), mod.replaces)
    for path in _mod_files(stop):
        if path.name == "go.mod":
            continue
        alternate = _go_mod(path)
        if alternate is None:
            return None
        replaced((path.parent, module.root), alternate.replaces)
    for path in _workspace_files(stop):
        work = _go_work(path)
        if work is None:
            return None
        roots = []
        for use in work.uses:
            try:
                roots.append((path.parent / use).resolve())
            except _PATH_ERRORS:
                return None
        if module.root not in roots:
            continue
        replaced((path.parent,), work.replaces)
        for root in roots:
            member = _go_mod(root / "go.mod")
            if member is None:
                return None
            if member.module is not None and _covers(member.module, import_path):
                add(root, member.module)
            replaced((root,), member.replaces)
    return None if unknown else found
