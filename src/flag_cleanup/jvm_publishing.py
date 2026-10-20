"""Whether code outside the repository can implement a Java or Kotlin
module's public interfaces: whether the module may be published.

A module is published when its own build files configure publishing
(:mod:`flag_cleanup.jvm_build_files`), when a script in the checkout deploys
it, or when a published module depends on it, since its classes then ship
with that module. Everything errs toward published. Two cases are not seen:
JitPack, which builds a public repository with no configuration at all, and
a deploy configured only outside the repository (a runner's Maven
``settings.xml``, an init script in its Gradle user home).

Maven deploy steps are tied to the modules they name: ``-f`` picks the
reactor by path suffix, since the step's working directory is not known, and
``-pl`` picks modules in it. Gradle publish tasks are not read: a task can
publish only a project whose build files configure publishing, and those are
already read project by project. An init script, ``jreleaser``, or a script
that cannot be read publishes every module.

Build files and scripts are not edited by a run, so :func:`published`
answers once per checkout; tests that rewrite a tree call
:func:`reset_cache`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from flag_cleanup import jvm_build_files as jbf
from flag_cleanup.jvm_build_files import Module

_CONTINUATION = re.compile(r"[\\`]\r?\n")
#: An init script, which can add publishing to every project: named by
#: `--init-script` anywhere (a build action's `arguments:`), or by `-I` on a
#: Gradle command, attached or not.
_GRADLE_INIT = re.compile(r"--init-script\b|(?<![\w.-])gradlew?(?:\.bat)?(?![\w-])[^\n;&|]*\s-I")
#: The JFrog CLI and Jenkins Artifactory steps deploy through their own
#: build-info extractor, at `install` and with no POM or plugin to see.
_JFROG = re.compile(r"\bjf\s+(?:mvn|gradle|rt)\b|\bjfrog\s+rt\b|\brt(?:Maven|Gradle)\w*\s*\(")
_ALT_REPOSITORY = "altDeploymentRepository"
_JRELEASER = re.compile(r"\bjreleaser\b", re.IGNORECASE)
#: A Maven command: `mvn`, the wrapper, the daemon, or a variable named for
#: one (`$(MVN)`, `${MVN}`, `"$MVN_CMD"`).
_MAVEN = re.compile(
    r"""(?<![\w.-])(?:mvn[wd]?(?:\.cmd)?|\$[{(]?MVN\w*[})]?)(?![\w.-])(?P<rest>[^\n;&|]*)""",
    re.IGNORECASE,
)
#: Maven options whose value is the next token.
_VALUED = frozenset({
    "-f", "--file", "-pl", "--projects", "-s", "--settings", "-gs", "--global-settings",
    "-P", "--activate-profiles", "-T", "--threads", "-rf", "--resume-from", "-l", "--log-file",
    "-t", "--toolchains", "-gt", "--global-toolchains", "-D", "--define", "-b", "--builder",
})
_DEPLOY_PLUGINS = (
    "maven-deploy-plugin", "nexus-staging-maven-plugin", "central-publishing-maven-plugin", "maven-release-plugin",
)


@dataclass
class _State:
    layout: jbf.Layout
    published: frozenset[Module]
    everything: bool


_cache: dict[str, _State] = {}


def reset_cache() -> None:
    """Forget every answered checkout."""
    _cache.clear()
    jbf.reset_cache()


def published(path: str, repo_dir: str) -> bool:
    """Whether the module holding ``path`` may be published. A file under no
    build, or under a build whose projects cannot be listed, is."""
    state = _state(repo_dir)
    modules = state.layout.modules_of(Path(path).resolve())
    if not modules or state.everything:
        return True
    return any(m in state.published for m in modules)


def _state(repo_dir: str) -> _State:
    from flag_cleanup import piranha_runner, source_index

    root = Path(source_index.checkout_root(repo_dir)).resolve()
    key = str(root)
    if key not in _cache:
        _cache[key] = _compute(root, piranha_runner)
    return _cache[key]


def _compute(root: Path, piranha_runner) -> _State:
    names = jbf.GRADLE_BUILD_FILES + jbf.GRADLE_SETTINGS_FILES + ("pom.xml",)
    files = [
        p for p in piranha_runner.source_files(root, {".gradle", ".kts", ".xml"}, piranha_runner._SKIP_DIRS)
        if p.name in names
    ]
    layout = jbf.layout(root, files)
    deployed = _deployed(root, layout.modules, piranha_runner)
    if deployed is None:
        return _State(layout, frozenset(), True)
    roots = deployed | {m for m in layout.modules if jbf.publishes(m, root, layout)}
    shipped: set[Module] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in shipped:
            continue
        shipped.add(current)
        edges = jbf.dependencies(current, layout, root)
        if edges is None:
            return _State(layout, frozenset(), True)
        pending.extend(edges)
    return _State(layout, frozenset(shipped), False)


def _deployed(root: Path, modules: list[Module], piranha_runner) -> set[Module] | None:
    """The modules a script in the checkout deploys, or ``None`` for every one."""
    from flag_cleanup import csharp_packing

    maven = [m for m in modules if m.system == "maven"]
    selected: set[Module] = set()
    suffixes = csharp_packing.SCRIPTS | {"", ".gradle", ".kts", ".toml", ".json", ".xml"}
    for path in piranha_runner.source_files(root, suffixes, piranha_runner._SKIP_DIRS):
        if path.name.lower().startswith("jreleaser.") or ".jfrog" in path.parts:
            return None
        if path.parent.name == "init.d" and path.suffix in (".gradle", ".kts"):
            return None
        if path.suffix in (".toml", ".json") or (path.suffix == "" and path.name not in csharp_packing.SCRIPT_NAMES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            if path.suffix == ".xml":
                continue  # an XML file is read only for the property below
            return None
        if path.suffix == ".xml":
            # A Maven `settings.xml` (or any other) that sets the deployment
            # repository a `deploy` with no `-pl` would use. A POM is judged
            # by its own module.
            if path.name != "pom.xml" and _ALT_REPOSITORY in text:
                return None
            continue
        raw = text
        text = _CONTINUATION.sub(" ", text)
        if _GRADLE_INIT.search(text) or _JRELEASER.search(text) or _JFROG.search(text) or _ALT_REPOSITORY in text:
            return None
        commands = [text]
        if path.suffix in (".yml", ".yaml") and _MAVEN.search(raw):
            # A folded scalar (`run: >`) joins its lines into one command,
            # so a goal on its own line belongs to the `mvn` above it.
            scalars = _yaml_strings(raw)
            if scalars is None:
                return None
            commands += [_CONTINUATION.sub(" ", s) for s in scalars]
        for command in commands:
            for match in _MAVEN.finditer(command):
                step = _maven_step(match["rest"], maven, root)
                if step is None:
                    return None
                selected |= step
    return selected


def _yaml_strings(text: str) -> list[str] | None:
    """Every string value in a YAML document as its loader reads it, or
    ``None`` when it does not parse."""
    import yaml

    try:
        documents = list(yaml.safe_load_all(text))
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    found: list[str] = []
    pending: list[object] = documents
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return found


def _maven_step(rest: str, maven: list[Module], root: Path) -> set[Module] | None:
    """The modules one Maven command deploys: empty when it deploys nothing,
    ``None`` when what it deploys cannot be told."""
    try:
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        tokens = rest.split()
    goals: list[str] = []
    options: dict[str, str] = {}
    pending: str | None = None
    for token in tokens:
        if pending is not None:
            options[pending] = token
            pending = None
        elif token in _VALUED:
            pending = token
        elif not token.startswith("-"):
            goals.append(token)
    if any(c in goal for goal in goals for c in "$%"):
        return None
    if not any(_deploys(goal) for goal in goals):
        return set()
    if any(goal.endswith("deploy-file") for goal in goals):
        return None
    file = options.get("-f", options.get("--file"))
    projects = options.get("-pl", options.get("--projects"))
    if any(value is not None and any(c in value for c in "$%") for value in (file, projects)):
        return None
    reactor = maven
    if file is not None:
        target = _normalise(file)
        if target.endswith(".xml"):
            target = target.rpartition("/")[0]
        bases = [m.directory for m in maven if _ends_with(_relative(m.directory, root), target)]
        if not bases:
            return None
        reactor = [m for m in maven if any(b == m.directory or b in m.directory.parents for b in bases)]
    if projects is None:
        return set(reactor)
    chosen: set[Module] = set()
    for entry in (e.strip() for e in projects.split(",")):
        if not entry or entry.startswith(("!", "-")):
            continue  # an exclusion is not honoured: it only publishes more
        if ":" in entry:
            group, _, artifact = entry.rpartition(":")
            hits = [
                m for m in reactor
                if m.name.split(":", 1)[1] == artifact and (not group or m.name.split(":", 1)[0] == group)
            ]
        else:
            hits = [m for m in reactor if _ends_with(_relative(m.directory, root), _normalise(entry))]
        if not hits:
            return None
        chosen.update(hits)
    return chosen or set(reactor)


def _deploys(goal: str) -> bool:
    prefix = goal.split(":", 1)[0]
    return (
        goal == "deploy"
        or prefix in ("deploy", "nexus-staging", "central-publishing")
        or goal == "release:perform"
        or any(plugin in goal for plugin in _DEPLOY_PLUGINS)
    )


def _normalise(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/")
    if path == "pom.xml":
        return ""
    return path.removesuffix("/pom.xml")


def _relative(directory: Path, root: Path) -> str:
    relative = directory.relative_to(root).as_posix()
    return "" if relative == "." else relative


def _ends_with(relative: str, target: str) -> bool:
    """Whether a module directory may be the one a path names from an
    unknown working directory. An empty path (``pom.xml``) may be any."""
    return target == "" or relative == target or relative.endswith("/" + target)
