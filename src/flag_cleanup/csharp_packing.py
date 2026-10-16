"""Which C# projects a build step packs into a NuGet package.

A project whose package can be published lets code outside the repository
implement its public interfaces, so a keyless wrapper behind one of them
cannot be inlined (see :mod:`flag_cleanup.wrapper_csharp`). A pack step
anywhere used to publish every project at once; a step that names what it
packs publishes only that.

What counts, and how it fails:

* ``dotnet pack``, and any ``dotnet <verb>`` or ``msbuild`` with a
  ``-t:``/``/t:``/``-target:`` switch whose target list includes ``Pack``:
  the first positional argument is what is packed. A project file, a
  solution file, or a directory holding one. A target switch with no
  command this can identify is a step with no argument.
* ``nuget pack``, any ``.nuspec``, and any file this cannot read pack
  something this cannot tie to a project: every project is published.
* A package property set outside a project file -- ``GeneratePackageOnBuild``
  or ``IsPackable`` to anything but ``false``, or any ``PackageId`` -- as a
  ``-p:``/``/p:``/``--property:`` switch (a ``Directory.Build.rsp`` included,
  which MSBuild applies to every command-line build), an environment
  variable (which MSBuild reads as a property) or a build script's property
  call: it packs, or makes packable, projects no step names, so every
  project is published.
* Not seen: a pack or push made only by a build tool's own API (Cake's
  ``DotNetPack(…)``/``DotNetNuGetPush(…)``, a Nuke build class).
* ``nuget push`` packs nothing. Whatever produced the package is a pack step,
  a ``.nuspec`` or ``GeneratePackageOnBuild``, each detected on its own. A
  push in a checkout where none of those is found means something unseen
  packed the package, so every project is published.

Where the step runs from is unknown outside a GitHub Actions workflow, so a
path argument there publishes every project whose path ends with it, and a
step with no argument publishes every project. Both err toward published.

Pack steps live in build scripts, which a run does not edit (only source
files are), so :func:`packed_projects` answers once per checkout; tests that
rewrite a tree call :func:`reset_cache`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class All:
    """Every project: a step this cannot tie to the projects it packs."""

    def __repr__(self) -> str:
        return "ALL"


ALL = All()

#: Build scripts and workflow files a pack step may live in.
SCRIPTS = frozenset(
    {".yml", ".yaml", ".sh", ".ps1", ".psm1", ".cmd", ".bat", ".cake", ".props", ".targets", ".proj",
     ".mk", ".csx", ".groovy", ".rsp"}
)
#: Extensionless build files, matched by name (the walk matches by suffix only).
SCRIPT_NAMES = frozenset({"Makefile", "makefile", "GNUmakefile", "justfile", "Jenkinsfile", "Dockerfile"})

_DOTNET_PACK = re.compile(r"\bdotnet\s+pack\b(?P<rest>[^\n;&|]*)", re.IGNORECASE)
#: A command that takes a target switch: any ``dotnet <verb>`` but ``pack``, or msbuild.
_TARGET_HOST = re.compile(
    r"\b(?:dotnet\s+(?!pack\b)[\w-]+|msbuild(?:\.exe)?)\b(?P<rest>(?:[-/]t(?:arget)?:[^\s\"']*|\"[^\"\n]*\"|'[^'\n]*'|[^\n;&|\"'])*)",
    re.IGNORECASE,
)
_TARGET_SWITCH = re.compile(r"[-/]t(?:arget)?:\s*(?P<targets>\"[^\"]*\"|'[^']*'|[^\s\"']*)", re.IGNORECASE)
_PUSH = re.compile(r"\bnuget(?:\.exe)?\s+push\b", re.IGNORECASE)
_GENERATES_PACKAGE = re.compile(r"<GeneratePackageOnBuild\b[^>]*>\s*(?!false\b)", re.IGNORECASE)
_NUGET_PACK = re.compile(r"\bnuget(?:\.exe)?\s+pack\b", re.IGNORECASE)
#: A package property assigned in a script: ``-p:GeneratePackageOnBuild=true``,
#: ``--property:PackageId=X``, ``GeneratePackageOnBuild: true`` (or quoted) in
#: a workflow's ``env:``, ``export IsPackable=true``, Dockerfile ``ENV
#: GeneratePackageOnBuild true``, and the call forms
#: ``SetEnvironmentVariable("GeneratePackageOnBuild", "true")`` and Cake's
#: ``WithProperty("PackageId", …)``. Matched wherever it is spelled, since
#: MSBuild also takes an environment variable as a property. The separator is
#: an atomic group, so a space before ``false`` cannot be backtracked into a
#: match; the lookbehind requires that one was there.
_SEPARATOR = r"""(?>["']?\s*[:=,]?\s*["']?)(?<=[\s:=,"'])"""
_PACKAGE_PROPERTY = re.compile(
    rf"\b(?:(?:GeneratePackageOnBuild|IsPackable){_SEPARATOR}(?!false\b)|PackageId{_SEPARATOR})",
    re.IGNORECASE,
)
_VARIABLE = re.compile(r"\$|%")
#: An MSBuild-style switch (``/t:Pack``, ``/p:X=Y``), as opposed to an absolute path.
_SLASH_OPTION = re.compile(r"^/\w+:")
#: Options whose value is the next token, so it is not the positional argument.
_VALUED = frozenset(
    {"-c", "--configuration", "-o", "--output", "-f", "--framework", "-r", "--runtime",
     "--version-suffix", "-v", "--verbosity", "-p", "--property", "--source", "-s",
     "-a", "--arch", "--os", "--artifacts-path"}
)

_cache: dict[str, frozenset[Path] | All] = {}


def reset_cache() -> None:
    """Forget every answered checkout."""
    _cache.clear()


@dataclass(frozen=True)
class PackStep:
    #: The first positional argument, ``None`` when there is none.
    argument: str | None
    #: ``dotnet`` (dotnet pack or msbuild -t:Pack) or ``nuget`` (nuget pack).
    kind: str


def _positional(rest: str) -> str | None:
    try:
        tokens = shlex.split(rest.replace("\\", "/"), posix=True)
    except ValueError:
        tokens = rest.split()
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token.startswith("-") or _SLASH_OPTION.match(token):
            if token in _VALUED:
                skip = True
            continue
        return token
    return None


def _targets_pack(targets: str) -> bool:
    return any(t.strip().strip("\"'").strip().lower() == "pack" for t in targets.split(";"))


def pack_steps(text: str) -> list[PackStep]:
    """Every pack step in ``text``, in order. Pushes are not steps; a package
    property set outside a project file is one that packs anything."""
    found: list[tuple[int, PackStep]] = []
    for match in _DOTNET_PACK.finditer(text):
        found.append((match.start(), PackStep(_positional(match.group("rest")), "dotnet")))
    hosted: list[tuple[int, int]] = []
    for match in _TARGET_HOST.finditer(text):
        hosted.append(match.span())
        if any(_targets_pack(m.group("targets")) for m in _TARGET_SWITCH.finditer(match.group("rest"))):
            found.append((match.start(), PackStep(_positional(match.group("rest")), "dotnet")))
    for match in _TARGET_SWITCH.finditer(text):
        if _targets_pack(match.group("targets")) and not any(a <= match.start() < b for a, b in hosted):
            found.append((match.start(), PackStep(None, "dotnet")))
    for match in _NUGET_PACK.finditer(text):
        found.append((match.start(), PackStep(None, "nuget")))
    for match in _PACKAGE_PROPERTY.finditer(text):
        found.append((match.start(), PackStep(None, "nuget")))
    return [step for _, step in sorted(found, key=lambda pair: pair[0])]


def _solution_projects(path: Path) -> frozenset[Path] | All:
    """The project directories a ``.sln``/``.slnx`` lists, or ``ALL``. A path
    that cannot even be looked up (a NUL byte) is ``ALL`` too."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ALL
    if path.suffix.lower() == ".slnx":
        relatives = re.findall(r'<Project\b[^>]*\bPath\s*=\s*"([^"]+)"', text)
    else:
        relatives = re.findall(
            r'^Project\("[^"]*"\)\s*=\s*"[^"]*",\s*"([^"]+\.csproj)"', text, re.MULTILINE | re.IGNORECASE
        )
    found: set[Path] = set()
    for relative in relatives:
        try:
            project = (path.parent / relative.replace("\\", "/")).resolve()
            if not project.exists():
                return ALL
        except (ValueError, OSError, RuntimeError):
            return ALL
        found.add(project.parent)
    return frozenset(found)


def _listed(solutions: Iterable[Path]) -> frozenset[Path] | All:
    """The union of the projects the solutions list, or ``ALL``."""
    union: set[Path] = set()
    for solution in solutions:
        listed = _solution_projects(solution)
        if listed is ALL:
            return ALL
        union |= listed
    return frozenset(union)


def resolve_argument(
    argument: str | None, directory: Path | None, projects: frozenset[Path], root: Path
) -> frozenset[Path] | All:
    """The projects one ``dotnet pack`` argument packs, run from ``directory``
    (``None``: unknown)."""
    if argument is not None and _VARIABLE.search(argument):
        return ALL
    if directory is None:
        if argument is None:
            return ALL
        suffix = argument.replace("\\", "/").strip("/")
        if suffix.lower().endswith((".sln", ".slnx")):
            from flag_cleanup import piranha_runner  # noqa: PLC0415

            matches = [
                s
                for s in piranha_runner.source_files(root, {".sln", ".slnx"}, piranha_runner._SKIP_DIRS)
                if s.name == Path(suffix).name and str(s.resolve()).endswith(suffix)
            ]
            if not matches:
                return ALL
            return _listed(matches)
        if not suffix.lower().endswith(".csproj"):
            # A directory: if a solution sits in one ending the same way, the
            # step may be packing that solution's projects instead.
            from flag_cleanup import piranha_runner  # noqa: PLC0415

            for solution in piranha_runner.source_files(root, {".sln", ".slnx"}, piranha_runner._SKIP_DIRS):
                if str(solution.resolve().parent).endswith(suffix):
                    return ALL
        if suffix.lower().endswith(".csproj"):
            found = frozenset(p for p in projects if any(str(c).endswith(suffix) for c in p.glob("*.csproj")))
        else:
            found = frozenset(p for p in projects if str(p).endswith(suffix))
        return found or ALL
    target = directory if argument is None else (directory / argument.replace("\\", "/")).resolve()
    if target.is_file():
        if target.suffix.lower() == ".csproj":
            return frozenset({target.parent})
        if target.suffix.lower() in (".sln", ".slnx"):
            return _solution_projects(target)
        return ALL
    if target.is_dir():
        solutions = sorted([*target.glob("*.sln"), *target.glob("*.slnx")])
        if solutions:
            return _listed(solutions)
        if any(target.glob("*.csproj")):
            return frozenset({target})
    return ALL


def packed_projects(repo_dir: str) -> frozenset[Path] | All:
    """The project directories some step in the checkout packs, or ``ALL``."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import piranha_runner, source_index

    key = str(Path(source_index.checkout_root(repo_dir)).resolve())
    if key not in _cache:
        _cache[key] = _packed_projects(repo_dir, Path(key), xcs._project_dirs(repo_dir), piranha_runner)
    return _cache[key]


def _packed_projects(repo_dir: str, root: Path, projects: frozenset[Path], piranha_runner) -> frozenset[Path] | All:
    # Local import: csharp_workflows imports this module.
    from flag_cleanup import csharp_workflows  # noqa: PLC0415

    packed: set[Path] = set()
    steps = 0
    pushes = False
    names = SCRIPTS | {".nuspec", ""}
    for path in piranha_runner.source_files(root, names, piranha_runner._SKIP_DIRS):
        if path.suffix == "" and path.name not in SCRIPT_NAMES:
            continue
        if path.suffix.lower() == ".nuspec":
            return ALL
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ALL
        pushes = pushes or bool(_PUSH.search(text))
        try:
            located = csharp_workflows.located_steps(path, text, root)
            if located is None:
                # A workflow that does not parse: any step in it is invisible.
                return ALL
            for step, directory in located:
                steps += 1
                if step.kind == "nuget":
                    return ALL
                result = resolve_argument(step.argument, directory, projects, root)
                if result is ALL:
                    return ALL
                packed |= result
        except (ValueError, OSError, RuntimeError):
            # A path no lookup accepts (a NUL byte) or one that loops: what
            # the step packs cannot be told, so it packs everything.
            return ALL
    if pushes and not steps and not _generates_package(root, piranha_runner):
        # A package is pushed that nothing visible packed.
        return ALL
    return frozenset(packed)


def _generates_package(root: Path, piranha_runner) -> bool:
    for path in piranha_runner.source_files(root, {".csproj", ".props", ".targets"}, piranha_runner._SKIP_DIRS):
        try:
            if _GENERATES_PACKAGE.search(path.read_text(encoding="utf-8")):
                return True
        except (OSError, UnicodeDecodeError):
            # Unreadable: not shown to generate a package, so the push stands unexplained.
            continue
    return False
