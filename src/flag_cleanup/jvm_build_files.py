"""Java and Kotlin modules, and whether a module's own build files publish it.

:mod:`flag_cleanup.jvm_publishing` adds what the checkout's scripts deploy and
what ships inside a published module. Everything here errs toward published:
a file that cannot be read, or a form this does not recognise, is a module
that may publish.
"""

from __future__ import annotations

import functools
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")
GRADLE_SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")


@dataclass(frozen=True)
class Module:
    #: ``gradle`` or ``maven``.
    system: str
    #: The project's directory, resolved.
    directory: Path
    #: Gradle: the build's settings directory. Maven: the module's own directory.
    root: Path
    #: Gradle: the project path (``:a:b``, ``:`` for the root project).
    #: Maven: ``groupId:artifactId``.
    name: str


@dataclass
class Layout:
    """Every module in a checkout, and the Gradle builds whose projects could
    not be listed."""

    modules: list[Module] = field(default_factory=list)
    unresolved: set[Path] = field(default_factory=set)

    def modules_of(self, path: Path) -> list[Module] | None:
        """The innermost project of every Gradle build holding ``path``
        (resolved), and the innermost Maven module, or ``None`` when it is in
        a Gradle build whose projects cannot be listed, or inside a build yet
        in none of its projects. Empty: no build encloses it.

        Every build counts, not just the innermost: a directory can be a
        project of an outer build and the root of its own nested one."""
        found: list[Module] = []
        if any(b == path or b in path.parents for b in self.unresolved):
            return None
        holding: dict[Path, list[Module]] = {}
        for m in self.modules:
            if m.system == "gradle" and (m.directory == path or m.directory in path.parents):
                holding.setdefault(m.root, []).append(m)
        for build in {m.root for m in self.modules if m.system == "gradle"}:
            if (build == path or build in path.parents) and build not in holding:
                return None  # its root project was moved away
        for projects in holding.values():
            found.append(max(projects, key=lambda m: len(m.directory.parts)))
        maven = [m for m in self.modules if m.system == "maven" and m.directory in path.parents]
        if maven:
            found.append(max(maven, key=lambda m: len(m.directory.parts)))
        return found


def layout(root: Path, files: list[Path]) -> Layout:
    """Every module under ``root``, read from the build files among ``files``."""
    result = Layout()
    settings = sorted({f.parent.resolve() for f in files if f.name in GRADLE_SETTINGS_FILES})
    for directory in settings:
        projects = _gradle_projects(directory)
        if projects is None:
            result.unresolved.add(directory)
            continue
        result.modules.extend(Module("gradle", d, directory, name) for name, d in projects.items())
    for file in files:
        directory = file.parent.resolve()
        if file.name in GRADLE_BUILD_FILES and not any(s == directory or s in directory.parents for s in settings):
            # A build with no settings file is a single project.
            if not any(m.system == "gradle" and m.root == directory for m in result.modules):
                result.modules.append(Module("gradle", directory, directory, ":"))
        elif file.name == "pom.xml" and "META-INF" not in file.parts:
            result.modules.append(Module("maven", directory, directory, _maven_name(file)))
    return result


_STRING = r"""(?:"[^"\\\n$]*"|'[^'\\\n$]*')"""
_LITERAL = re.compile(_STRING)
_INCLUDE = re.compile(r"\binclude(?P<flat>Flat)?\b(?P<paren>\s*\()?")
_GROOVY_ARGS = re.compile(rf"\s+{_STRING}(?:\s*,\s*{_STRING})*")
_PROJECT_DIR = re.compile(r"\bprojectDir\b")
#: A project whose build file is renamed: which file configures it is not read.
_BUILD_FILE_NAME = re.compile(r"\bbuildFileName\b")
_MOVED = re.compile(
    r"""project\(\s*["'](?P<name>:[^"'$]*)["']\s*\)\s*\.\s*projectDir\s*=\s*file\(\s*["'](?P<dir>[^"'$]+)["']\s*\)"""
)
_COMMENT = re.compile(r"/\*.*?\*/|^\s*//[^\n]*", re.DOTALL | re.MULTILINE)


def _gradle_projects(directory: Path) -> dict[str, Path] | None:
    """Project path -> directory for the build whose settings live in
    ``directory``, or ``None`` when its projects cannot be listed."""
    projects: dict[str, Path] = {":": directory}
    for name in GRADLE_SETTINGS_FILES:
        if not (directory / name).is_file():
            continue
        text = _read(directory / name)
        if text is None:
            return None
        text = _strip_comments(text)
        paths = _includes(text)
        moved = _moved(text)
        if paths is None or moved is None or _BUILD_FILE_NAME.search(text):
            return None
        for path in paths:
            segments = [s for s in path.split(":") if s]
            for end in range(1, len(segments) + 1):
                projects.setdefault(":" + ":".join(segments[:end]), directory.joinpath(*segments[:end]).resolve())
        if any(other.startswith(m + ":") for m in moved for other in projects):
            return None  # a child of a moved project moves with it
        for project, target in moved.items():
            projects[project] = (directory / target).resolve()
    return projects


def _includes(text: str) -> list[str] | None:
    found: list[str] = []
    for match in _INCLUDE.finditer(text):
        if match.group("flat"):
            return None
        if match.group("paren"):
            end = text.find(")", match.end())
            if end == -1:
                return None
            args = text[match.end():end]
        else:
            groovy = _GROOVY_ARGS.match(text, match.end())
            if groovy is None:
                return None
            args = groovy.group(0)
        literals = _LITERAL.findall(args)
        if not literals or re.sub(r"[\s,]", "", _LITERAL.sub("", args)):
            return None
        found += [s[1:-1] for s in literals]
    return found


def _moved(text: str) -> dict[str, str] | None:
    moved = {m.group("name"): m.group("dir") for m in _MOVED.finditer(text)}
    if len(_PROJECT_DIR.findall(text)) != len(list(_MOVED.finditer(text))):
        return None
    return moved


def _maven_name(pom: Path) -> str:
    document = _parse(pom)
    if document is None:
        return ":"
    group = _text(document, "groupId") or _text(_child(document, "parent"), "groupId") or ""
    return f"{group}:{_text(document, 'artifactId') or ''}"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _strip_comments(text: str) -> str:
    """Block comments and whole-line ``//`` comments. A ``//`` after code is
    kept, since it may be inside a URL."""
    return _COMMENT.sub("", text)


def _parse(path: Path) -> ElementTree.Element | None:
    text = _read(path)
    # No build file needs a DTD, and refusing one rules out entity expansion
    # whatever the XML library would do with it.
    if text is None or "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None
    try:
        return ElementTree.fromstring(text)
    except (ElementTree.ParseError, ValueError):
        return None


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if element is None:
        return None
    return next((c for c in element if _local(c.tag) == name), None)


def _text(element: ElementTree.Element | None, name: str) -> str | None:
    child = _child(element, name)
    return None if child is None else (child.text or "").strip()


#: Gradle plugins that publish nothing by themselves. A module can still
#: publish through ``maven-publish``, which is not here.
GRADLE_PLUGINS = frozenset({
    # Gradle core
    "java", "java-library", "application", "war", "ear", "base", "idea", "eclipse", "jacoco",
    "checkstyle", "pmd", "groovy", "scala", "antlr", "distribution", "java-test-fixtures",
    "jvm-test-suite", "test-report-aggregation", "jacoco-report-aggregation",
    # Kotlin
    "org.jetbrains.kotlin.jvm", "org.jetbrains.kotlin.android", "org.jetbrains.kotlin.kapt",
    "org.jetbrains.kotlin.plugin.spring", "org.jetbrains.kotlin.plugin.jpa",
    "org.jetbrains.kotlin.plugin.allopen", "org.jetbrains.kotlin.plugin.noarg",
    "org.jetbrains.kotlin.plugin.serialization", "org.jetbrains.kotlin.plugin.lombok",
    "com.google.devtools.ksp",
    # Applications and their packaging
    "org.springframework.boot", "io.spring.dependency-management", "com.google.cloud.tools.jib",
    "com.github.johnrengelman.shadow", "com.gradleup.shadow", "org.graalvm.buildtools.native",
    "com.android.application", "io.freefair.lombok", "org.flywaydb.flyway",
    # Linters and formatters
    "com.diffplug.spotless", "org.jlleitschuh.gradle.ktlint", "io.gitlab.arturbosch.detekt",
    "com.github.ben-manes.versions", "org.sonarqube",
    # Settings plugins
    "com.gradle.develocity", "com.gradle.enterprise",
    "org.gradle.toolchains.foojay-resolver", "org.gradle.toolchains.foojay-resolver-convention",
})

#: A word only a publishing configuration spells, matched inside longer
#: names (``nexusPublishing``, ``publishToMavenLocal``). Resolve-mirror
#: names (Nexus, Artifactory) are not words: publishing through one needs
#: this word or a plugin the list above leaves out.
_PUBLISHING = re.compile(r"publish|uploadArchives|\bsigning\s*\{|jreleaser", re.IGNORECASE)
#: Build-scan configuration, which spells ``publishing`` without publishing
#: anything (``buildScan { publishing.onlyIf { … } }``).
_BUILD_SCAN = re.compile(r"\b(?:develocity|gradleEnterprise|buildScan)\s*\{")
#: Configuration one project's script applies to another: a sibling can
#: configure the project without being its ancestor.
_CROSS_CONFIGURATION = re.compile(
    r"""\brootProject\s*(?:\{|\.\s*(?:allprojects|subprojects|project\s*\(|configure|apply|plugins|pluginManager|afterEvaluate))
       |\bconfigure\s*\(|\bfindProject\s*\(|\bgradle\s*\.\s*(?:allprojects|beforeProject|afterProject)
       |\bproject\s*\(\s*["'][^"']*["']\s*\)\s*(?:\{|\.\s*(?:apply|plugins|pluginManager|afterEvaluate|extensions|configure))""",
    re.VERBOSE,
)
_APPLIES = re.compile(r"\b(?:plugin|from)\b")
_PLUGINS_OPEN = re.compile(r"\bplugins\s*\{")
_PLUGIN_SPEC = re.compile(
    r"""(?:id\s*\(?\s*["'](?P<id>[^"'$]+)["']\s*\)?
        |kotlin\s*\(\s*["'](?P<kotlin>[^"'$]+)["']\s*\)
        |alias\s*\(\s*(?P<alias>[\w.]+)\s*\)
        |`(?P<tick>[\w-]+)`
        |(?P<bare>[A-Za-z]\w*))
       (?:\s*\.?\s*version\s*\(?\s*(?:"[^"\n]*"|'[^'\n]*'|[\w.]+(?:\(\))?)\s*\)?)?
       (?:\s*\.?\s*apply\s*\(?\s*(?:true|false)\s*\)?)?""",
    re.VERBOSE,
)
_APPLY = re.compile(
    r"""(?P<false>\bapply\s*\(?\s*false\b)
       |(?P<scope>\.\s*apply\s*\{)
       |\b(?:project\.)?(?:plugins|pluginManager)\s*\.\s*apply\s*\(\s*["'](?P<call>[^"'$]+)["']\s*\)
       |\bapply\s*\(?\s*plugin\s*[:=]\s*["'](?P<id>[^"'$]+)["']\s*\)?
       |(?P<other>\bapply\b)""",
    re.VERBOSE,
)
_INCLUDE_BUILD = re.compile(r"""\bincludeBuild\b\s*\(?\s*(?P<path>"[^"$\n]*"|'[^'$\n]*')?""")
_REGISTERED = re.compile(
    r"""\bid\s*=\s*["'](?:""" + "|".join(re.escape(p) for p in sorted(GRADLE_PLUGINS)) + r""")["']"""
)


def publishes(module: Module, checkout_root: Path, layout: Layout) -> bool:
    """Whether ``module``'s own build files may publish it."""
    if module.system == "gradle":
        return _gradle_publishes(module, layout)
    return _maven_publishes(module, checkout_root)


def reset_cache() -> None:
    _shadowed.cache_clear()


def _gradle_files(module: Module, layout: Layout) -> list[Path]:
    """Everything that can configure a project: the build's settings files,
    the build files of the project and of each ancestor in the PROJECT tree
    (``:a:b``, ``:a``, ``:``; a moved project's directory says nothing about
    its parent), and any other project's build file that configures projects
    other than its own subtree."""
    projects = {m.name: m for m in layout.modules if m.system == "gradle" and m.root == module.root}
    found = [module.root / n for n in GRADLE_SETTINGS_FILES if (module.root / n).is_file()]
    segments = [s for s in module.name.split(":") if s]
    for end in range(len(segments), -1, -1):
        ancestor = projects.get(":" + ":".join(segments[:end]))
        if ancestor is not None:
            found += [ancestor.directory / n for n in GRADLE_BUILD_FILES if (ancestor.directory / n).is_file()]
    for other in projects.values():
        for name in GRADLE_BUILD_FILES:
            file = other.directory / name
            if file in found or not file.is_file():
                continue
            text = _read(file)
            if text is None or _CROSS_CONFIGURATION.search(_strip_comments(text)):
                found.append(file)
    return found


def _gradle_publishes(module: Module, layout: Layout) -> bool:
    catalog: dict[str, str] | None = None
    for file in _gradle_files(module, layout):
        text = _read(file)
        if text is None:
            return True
        text = _strip_comments(text)
        if _PUBLISHING.search(_without_blocks(text, _BUILD_SCAN)):
            return True
        plugins = _gradle_plugins(text)
        if plugins is None:
            return True
        ids, aliases = plugins
        if aliases:
            catalog = catalog if catalog is not None else _catalog(module.root)
            if catalog is None or any(a not in catalog for a in aliases):
                return True
            ids = ids + [catalog[a] for a in aliases]
        if any(i not in GRADLE_PLUGINS for i in ids):
            return True
    return _shadowed(module.root)


def _gradle_plugins(text: str) -> tuple[list[str], list[str]] | None:
    """Plugin ids and catalog aliases a build script applies, or ``None``
    when it applies one in a form not read here."""
    ids: list[str] = []
    aliases: list[str] = []
    bodies = _blocks(text, _PLUGINS_OPEN)
    if bodies is None:
        return None
    for body in bodies:
        for statement in re.split(r"[\n;]", body):
            statement = re.sub(r"\s//.*$", "", statement).strip()
            if not statement:
                continue
            spec = _PLUGIN_SPEC.fullmatch(statement)
            if spec is None:
                return None
            if spec["id"]:
                ids.append(spec["id"])
            elif spec["kotlin"]:
                ids.append("org.jetbrains.kotlin." + spec["kotlin"])
            elif spec["alias"]:
                aliases.append(spec["alias"])
            else:
                ids.append(spec["tick"] or spec["bare"])
    for match in _APPLY.finditer(text):
        if match["other"]:
            return None
        if match["scope"]:
            # Kotlin's `apply {}` is the scope function, unless the block
            # applies a plugin or a script (`Project.apply(Action)`).
            body = _block_at(text, match.end())
            if body is None or _APPLIES.search(body):
                return None
        if match["call"] or match["id"]:
            ids.append(match["call"] or match["id"])
    return ids, aliases


def _blocks(text: str, opener: re.Pattern[str]) -> list[str] | None:
    bodies: list[str] = []
    for match in opener.finditer(text):
        body = _block_at(text, match.end())
        if body is None:
            return None
        bodies.append(body)
    return bodies


def _block_end(text: str, start: int) -> int | None:
    """The index just past the ``}`` closing a block whose ``{`` ends at ``start``."""
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return None if depth else i


def _block_at(text: str, start: int) -> str | None:
    end = _block_end(text, start)
    return None if end is None else text[start:end - 1]


def _without_blocks(text: str, opener: re.Pattern[str]) -> str:
    """``text`` with every block ``opener`` starts cut out, outermost first.
    An unclosed block is left in, which only makes it read as more."""
    kept: list[str] = []
    position = 0
    for match in opener.finditer(text):
        if match.start() < position:
            continue
        end = _block_end(text, match.end())
        if end is None:
            continue
        kept.append(text[position:match.start()])
        position = end
    kept.append(text[position:])
    return "".join(kept)


def _catalog(build: Path) -> dict[str, str] | None:
    """``libs.plugins.<accessor>`` -> plugin id, from the build's default
    version catalog."""
    path = build / "gradle" / "libs.versions.toml"
    text = _read(path) if path.is_file() else None
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        return None
    found: dict[str, str] = {}
    for key, value in plugins.items():
        plugin = value.split(":", 1)[0] if isinstance(value, str) else value.get("id") if isinstance(value, dict) else None
        if isinstance(plugin, str):
            found["libs.plugins." + re.sub(r"[-_]", ".", key)] = plugin
    return found


@functools.cache
def _shadowed(build: Path) -> bool:
    """Whether ``buildSrc`` or an included build registers a plugin under an
    id the list above trusts, which would replace it."""
    directories = [build / "buildSrc"]
    for name in GRADLE_SETTINGS_FILES:
        if not (build / name).is_file():
            continue
        text = _read(build / name)
        if text is None:
            return True
        for match in _INCLUDE_BUILD.finditer(_strip_comments(text)):
            if match["path"] is None:
                return True
            directories.append(build / match["path"][1:-1])
    names = {f"{p}{s}" for p in GRADLE_PLUGINS for s in (".gradle", ".gradle.kts", ".properties")}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if path.name in names:
                return True
            if path.suffix in (".gradle", ".kts"):
                text = _read(path)
                if text is None or _REGISTERED.search(text):
                    return True
    return False


#: Maven plugins, by artifactId, that deploy nothing. ``exec`` and
#: ``antrun`` are left out: they run whatever they are given.
MAVEN_PLUGINS = frozenset({
    "maven-compiler-plugin", "maven-surefire-plugin", "maven-failsafe-plugin",
    "maven-surefire-report-plugin", "maven-resources-plugin", "maven-clean-plugin",
    "maven-install-plugin", "maven-jar-plugin", "maven-war-plugin", "maven-shade-plugin",
    "maven-assembly-plugin", "maven-dependency-plugin", "maven-enforcer-plugin",
    "build-helper-maven-plugin", "spring-boot-maven-plugin", "jacoco-maven-plugin",
    "kotlin-maven-plugin", "jib-maven-plugin", "native-maven-plugin", "flyway-maven-plugin",
    "git-commit-id-plugin", "git-commit-id-maven-plugin", "maven-checkstyle-plugin",
    "maven-pmd-plugin", "spotbugs-maven-plugin", "spotless-maven-plugin",
})
#: Parents outside the repository known to configure no deployment.
MAVEN_PARENTS = frozenset({("org.springframework.boot", "spring-boot-starter-parent")})


def _maven_chain(module: Module, checkout_root: Path) -> tuple[list[Path], bool]:
    """The module's ``pom.xml`` and each in-repository parent, in order, and
    whether the chain is trusted: every POM parses, and it ends without a
    parent or at a known external one."""
    chain: list[Path] = []
    pom = module.directory / "pom.xml"
    while True:
        document = _parse(pom)
        if document is None:
            return chain, False
        chain.append(pom)
        parent = _child(document, "parent")
        if parent is None:
            return chain, True
        group, artifact = _text(parent, "groupId"), _text(parent, "artifactId")
        relative = _child(parent, "relativePath")
        target: Path | None
        if relative is None:
            target = pom.parent.parent / "pom.xml"
        elif (relative.text or "").strip():
            target = pom.parent / relative.text.strip()
        else:
            target = None  # `<relativePath/>`: looked up outside the repository
        if target is not None:
            target = target.resolve()
            if target.is_dir():
                target = target / "pom.xml"
        if (
            target is not None
            and target.is_file()
            and checkout_root in target.parents
            and target not in chain
            and _maven_name(target) == f"{group}:{artifact}"
            and _same_version(_text(parent, "version"), target)
        ):
            pom = target
            continue
        return chain, (group, artifact) in MAVEN_PARENTS


def _same_version(wanted: str | None, pom: Path) -> bool:
    """Whether the parent a ``<parent>`` asks for is the in-repository one:
    at another version, Maven resolves it from a repository instead. A
    version this cannot compare (missing, or a property) is taken as the
    same, as the name already matched."""
    document = _parse(pom)
    actual = _text(document, "version") or _text(_child(document, "parent"), "version")
    if not wanted or not actual or "${" in wanted or "${" in actual:
        return True
    return wanted == actual


def _maven_publishes(module: Module, checkout_root: Path) -> bool:
    chain, trusted = _maven_chain(module, checkout_root)
    if not trusted:
        return True
    for pom in chain:
        text = _read(pom)
        document = _parse(pom)
        if text is None or document is None or "altDeploymentRepository" in text:
            return True
        for element in document.iter():
            tag = _local(element.tag)
            if tag == "distributionManagement":
                return True
            # A build extension (a wagon, a deployer). A plugin's own
            # `<extensions>true</extensions>` is judged by the plugin.
            if tag == "build" and _child(element, "extensions") is not None:
                return True
            # Only a `<plugins>` entry is a plugin: Kotlin's
            # `<compilerPlugins><plugin>spring</plugin>` is configuration.
            if tag == "plugins" and any(
                _local(p.tag) == "plugin" and (_text(p, "artifactId") or "") not in MAVEN_PLUGINS for p in element
            ):
                return True
    return _mvn_directory_publishes(module.directory, checkout_root)


def _mvn_directory_publishes(directory: Path, checkout_root: Path) -> bool:
    """Every ``.mvn`` directory from the module up to the checkout root:
    which one Maven reads depends on where the build starts. A build
    extension or a deployment property in any of them publishes."""
    for current in (directory, *directory.parents):
        mvn = current / ".mvn"
        if mvn.is_dir():
            extensions = mvn / "extensions.xml"
            if extensions.is_file():
                document = _parse(extensions)
                if document is None or any(_local(e.tag) == "extension" for e in document.iter()):
                    return True
            config = mvn / "maven.config"
            if config.is_file():
                text = _read(config)
                if text is None or "altDeploymentRepository" in text:
                    return True
        if current == checkout_root:
            break
    return False


#: A source-set directory line (`srcDir("…")`, `srcDirs = ['…']`): sources it
#: names from outside the project are compiled into the project.
_SOURCE_DIR = re.compile(r"\b(?:srcDirs?|setSrcDirs)\b[^\n]*")
_PROJECT_REF = re.compile(r"""\bproject\s*\(\s*(?:path\s*[:=]\s*)?["'](?P<path>:[^"'$]*)["']""")
_PROJECT_CALL = re.compile(r"\bproject\s*\(")
_ACCESSOR = re.compile(r"\bprojects\.(?P<chain>[A-Za-z_][\w.]*)")


def dependencies(module: Module, layout: Layout, checkout_root: Path) -> set[Module] | None:
    """The in-repository modules ``module`` depends on, or ``None`` when a
    reference cannot be resolved. Gradle: every ``project(":x")`` and
    ``projects.x`` in its build file or an ancestor's (a root
    ``subprojects {}`` block names dependencies of every project). Maven:
    every dependency artifactId, in its POM or an in-repository parent, that
    an in-repository module declares. Over-counting is safe: it only
    publishes more."""
    found: set[Module] = set()
    if module.system == "gradle":
        siblings = {m.name: m for m in layout.modules if m.system == "gradle" and m.root == module.root}
        accessors = {_accessor(name): m for name, m in siblings.items() if name != ":"}
        for file in _gradle_files(module, layout):
            if file.name not in GRADLE_BUILD_FILES:
                continue
            text = _read(file)
            if text is None:
                return None
            text = _strip_comments(text)
            for line in (m.group(0) for m in _SOURCE_DIR.finditer(text)):
                literals = [s[1:-1] for s in _LITERAL.findall(line)]
                if not literals or not all(_inside(module.directory, d) for d in literals):
                    return None
            refs = list(_PROJECT_REF.finditer(text))
            if len(refs) != len(_PROJECT_CALL.findall(text)):
                return None
            for ref in refs:
                if ref["path"] not in siblings:
                    return None
                found.add(siblings[ref["path"]])
            for ref in _ACCESSOR.finditer(text):
                parts = ref["chain"].split(".")
                hit = next(
                    (accessors[".".join(parts[:n])] for n in range(len(parts), 0, -1) if ".".join(parts[:n]) in accessors),
                    None,
                )
                if hit is None:
                    return None
                found.add(hit)
    else:
        chain, _ = _maven_chain(module, checkout_root)
        if not chain:
            return None
        by_artifact: dict[str, list[Module]] = {}
        for other in layout.modules:
            if other.system == "maven":
                by_artifact.setdefault(other.name.split(":", 1)[1], []).append(other)
        for pom in chain:
            document = _parse(pom)
            if document is None:
                return None
            for element in document.iter():
                tag = _local(element.tag)
                if tag in ("source", "sourceDirectory"):
                    # Sources added from elsewhere (build-helper's `add-source`)
                    # ship inside this module. `<source>17</source>` is a
                    # compiler release, and stays inside.
                    value = (element.text or "").strip()
                    value = re.sub(r"\$\{(?:project\.)?basedir\}", ".", value)
                    if value and ("${" in value or not _inside(module.directory, value)):
                        return None
                    continue
                if tag not in ("dependency", "artifactItem"):
                    continue
                artifact = _text(element, "artifactId") or ""
                if "${" in artifact:
                    return None
                found.update(by_artifact.get(artifact, ()))
    found.discard(module)
    return found


def _inside(directory: Path, relative: str) -> bool:
    try:
        target = (directory / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return target == directory or directory in target.parents


def _accessor(path: str) -> str:
    """The type-safe accessor Gradle generates for a project path:
    ``:flag-core:api`` is ``flagCore.api``."""
    return ".".join(re.sub(r"[-_](\w)", lambda m: m.group(1).upper(), s) for s in path.split(":") if s)
