"""Which Swift module each file of a checkout is compiled into.

A Swift file never names its own module, and every top-level name of a module
is visible in all of its files without an import, so a cross-file proof needs
each file's module before it can say what a name means there. That comes from
the build files: a SwiftPM ``Package.swift`` and an Xcode ``project.pbxproj``.

Both are read conservatively. A manifest is read only when its targets are
plain literals; one that computes them, edits them after the fact, branches on
``#if``, lists ``sources:`` or ``exclude:``, or has a versioned sibling
(``Package@swift-5.9.swift``) is OPAQUE, and every file under it is in an
unknown module. A file in an unknown module is never offered and never
proven. The names an opaque build file mentions are kept, so an ``import`` of
one of its modules is recognised as one this reader cannot follow.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index, syntax
from flag_cleanup.cross_file_keys import within

__all__ = ["Layout", "Module", "layout", "literal", "parse", "within"]


@dataclass(frozen=True)
class Module:
    #: Unique in the checkout: the build file, then the target.
    id: str
    #: What an ``import`` spells; None when the build settings do not say.
    name: str | None
    #: The SwiftPM package root, for ``package`` access; None for Xcode.
    package: str | None = None


@dataclass(frozen=True, eq=False)
class Layout:
    by_file: dict[str, frozenset[Module]]
    #: Files a build file covers but cannot be fully read for.
    unknown: frozenset[str]
    modules: frozenset[Module]
    #: Names of modules whose files cannot all be placed.
    opaque_names: frozenset[str]
    #: Some module's name cannot be read, so any import this reader does not
    #: recognise may be it.
    unnamed: bool

    def modules_of(self, path: str) -> frozenset[Module] | None:
        """The modules ``path`` is compiled into; None when unknown."""
        resolved = str(Path(path).resolve())
        if resolved in self.unknown:
            return None
        return self.by_file.get(resolved) or None

    def named(self, name: str) -> frozenset[Module]:
        return frozenset(m for m in self.modules if m.name == name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(m.name for m in self.modules if m.name is not None) | self.opaque_names


def parse(text: str) -> Node:
    return syntax._parser("swift").parse(text.encode("utf-8")).root_node


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.named_children)


def literal(node: Node | None) -> str | None:
    """A plain string literal's text; None for anything else (interpolated,
    escaped, multi-line, or not a string)."""
    if node is None or node.type != "line_string_literal":
        return None
    parts = node.named_children
    if not parts:
        return ""
    if len(parts) != 1 or parts[0].type != "line_str_text":
        return None
    return _text(parts[0])


# --- SwiftPM ------------------------------------------------------------------

_SOURCES = ("Sources", "Source", "src", "srcs")
#: Where SwiftPM looks for a target's sources when it names no ``path:``, per
#: target kind; None for a kind with no Swift sources of its own.
_TARGET_DIRS: dict[str, tuple[str, ...] | None] = {
    "target": _SOURCES,
    "executableTarget": _SOURCES,
    "macro": _SOURCES,
    "testTarget": ("Tests", *_SOURCES),
    "plugin": ("Plugins",),
    "binaryTarget": None,
    "systemLibrary": None,
}
#: Arguments that move files in or out of a target, which this reader does
#: not follow.
_MOVES_FILES = frozenset({"sources", "exclude"})
_NAME = re.compile(r'name:\s*"([^"\\]+)"')
_OPAQUE = object()
#: A ``name:`` argument whose value is not a string literal.
_COMPUTED_NAME = re.compile(r'name:\s*[^"\s]')


def _c99(name: str) -> str:
    """Xcode's ``c99extidentifier`` of ``name``, which is also how SwiftPM
    spells a target as a module: what an ``import`` names."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return "_" + cleaned if cleaned[:1].isdigit() else cleaned


@dataclass(frozen=True)
class _Package:
    root: Path
    #: ``(directory, module)`` per target with sources; None when opaque.
    targets: tuple[tuple[Path, Module], ...] | None
    #: Every literal ``name:`` in the manifest, kept for an opaque one.
    names: frozenset[str]
    #: Opaque, and a target is named by something other than a literal.
    unnamed: bool = False


def _arguments(call: Node) -> dict[str | None, Node] | None:
    """A call's arguments by label (None for an unlabelled one); None when a
    label repeats or the call has no argument list."""
    suffix = next((c for c in call.named_children if c.type == "call_suffix"), None)
    values = None if suffix is None else next(
        (c for c in suffix.named_children if c.type == "value_arguments"), None
    )
    if values is None:
        return None
    found: dict[str | None, Node] = {}
    for argument in values.named_children:
        if argument.type != "value_argument":
            continue
        label = argument.child_by_field_name("name")
        value = argument.child_by_field_name("value")
        key = None if label is None else _text(label)
        if key in found or value is None:
            return None
        found[key] = value
    return found


def _target(element: Node, root: Path, manifest: Path):
    """``(directory, module)`` for one ``.target(...)`` element; None for a
    target without Swift sources; ``_OPAQUE`` when it cannot be read."""
    if element.type != "call_expression" or not element.named_children:
        return _OPAQUE
    callee = element.named_children[0]
    if callee.type != "prefix_expression":
        return _OPAQUE
    operation = callee.child_by_field_name("operation")
    kind_node = callee.child_by_field_name("target")
    if _text(operation) != "." or kind_node is None or kind_node.type != "simple_identifier":
        return _OPAQUE
    kind = _text(kind_node)
    if kind not in _TARGET_DIRS:
        return _OPAQUE
    arguments = _arguments(element)
    if arguments is None or _MOVES_FILES & arguments.keys():
        return _OPAQUE
    name = literal(arguments.get("name"))
    if not name:
        return _OPAQUE
    candidates = _TARGET_DIRS[kind]
    if candidates is None:
        return None
    if "path" in arguments:
        path = literal(arguments["path"])
        if path is None:
            return _OPAQUE
        directory = root / path
    else:
        directory = next((root / d / name for d in candidates if (root / d / name).is_dir()), None)
        if directory is None:
            return None
    return directory.resolve(), Module(f"{manifest}#{name}", _c99(name), str(root))


def _package(manifest: Path) -> _Package:
    root = manifest.parent
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _Package(root, None, frozenset(), True)  # nothing about its names is known
    names = frozenset(_c99(n) for n in _NAME.findall(text))
    opaque = _Package(root, None, names, bool(_COMPUTED_NAME.search(text)))
    if any(root.glob("Package@swift*.swift")):
        return opaque  # which manifest applies depends on the toolchain
    program = parse(text)
    if program.has_error:
        return opaque
    calls: list[Node] = []
    for node in _walk(program):
        if node.type == "directive":
            return opaque
        if node.type == "navigation_suffix" and _text(node.child_by_field_name("suffix")) == "targets":
            return opaque  # `package.targets += [...]`
        if (
            node.type == "call_expression"
            and node.named_children
            and node.named_children[0].type == "simple_identifier"
            and _text(node.named_children[0]) == "Package"
        ):
            calls.append(node)
    if len(calls) != 1:
        return opaque
    arguments = _arguments(calls[0])
    if arguments is None:
        return opaque
    listing = arguments.get("targets")
    if listing is None:
        return _Package(root, (), names)
    if listing.type != "array_literal":
        return opaque
    targets: list[tuple[Path, Module]] = []
    for element in listing.named_children:
        if element.type in ("comment", "multiline_comment"):
            continue
        target = _target(element, root, manifest)
        if target is _OPAQUE:
            return opaque
        if target is not None:
            targets.append(target)
    return _Package(root, tuple(targets), names)


# --- Xcode --------------------------------------------------------------------

_PLIST_TOKEN = re.compile(
    r"""(?P<skip>\s+|//[^\n]*|/\*.*?\*/)
      | (?P<string>"(?:[^"\\]|\\.)*")
      | (?P<word>(?:[^\s{}();=,"/]|/(?![/*]))+)
      | (?P<punct>[{}();=,])""",
    re.S | re.X,
)


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), text, flags=re.S)


def _plist(text: str):
    """An old-style property list (what ``project.pbxproj`` is) as dicts,
    lists and strings. Raises ``ValueError`` on anything it cannot read."""
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        match = _PLIST_TOKEN.match(text, position)
        if match is None:
            raise ValueError(f"unreadable property list at offset {position}")
        position = match.end()
        if match.lastgroup == "string":
            tokens.append(("s", _unescape(match.group("string")[1:-1])))
        elif match.lastgroup == "word":
            tokens.append(("s", match.group("word")))
        elif match.lastgroup == "punct":
            tokens.append(("p", match.group("punct")))
    try:
        value, end = _plist_value(tokens, 0)
    except IndexError as error:
        raise ValueError("truncated property list") from error
    except RecursionError as error:
        raise ValueError("property list nested too deeply") from error
    if end != len(tokens):
        raise ValueError("trailing tokens after the property list")
    return value


def _plist_value(tokens: list[tuple[str, str]], index: int):
    kind, text = tokens[index]
    if kind == "s":
        return text, index + 1
    if text == "{":
        found: dict[str, object] = {}
        index += 1
        while tokens[index] != ("p", "}"):
            key, index = _plist_value(tokens, index)
            if not isinstance(key, str) or tokens[index] != ("p", "="):
                raise ValueError("expected `key =`")
            found[key], index = _plist_value(tokens, index + 1)
            if tokens[index] != ("p", ";"):
                raise ValueError("expected `;`")
            index += 1
        return found, index + 1
    if text == "(":
        items: list[object] = []
        index += 1
        while tokens[index] != ("p", ")"):
            item, index = _plist_value(tokens, index)
            items.append(item)
            if tokens[index] == ("p", ","):
                index += 1
            elif tokens[index] != ("p", ")"):
                raise ValueError("expected `,` or `)`")
        return items, index + 1
    raise ValueError(f"unexpected {text!r}")


#: Per checkout root: the run's listing (by identity) and the project files
#: found while it was current.
_PROJECTS: dict[str, tuple[object, list[Path]]] = {}


def _projects(root: Path) -> list[Path]:
    """Every Xcode project file the checkout holds, tracked or not.

    Asked of git once per run (:func:`source_index.run_scope`): the run's
    listing is one object for the whole run, so the answer is kept for as long
    as that object is current. Outside a run every call asks again."""
    listing = source_index.run_listing(str(root))
    cached = _PROJECTS.get(str(root))
    if listing is not None and cached is not None and cached[0] is listing:
        return cached[1]
    found = _list_projects(root)
    if listing is not None:
        _PROJECTS[str(root)] = (listing, found)
    return found


def _list_projects(root: Path) -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", ":(glob)**/*.xcodeproj/project.pbxproj"],
            capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace").split("\0")
        return sorted({(root / p).resolve() for p in listed if p})
    except (OSError, subprocess.CalledProcessError):
        return sorted(p.resolve() for p in root.rglob("project.pbxproj") if p.parent.suffix == ".xcodeproj")


_TARGET_NAME = frozenset({"$(TARGET_NAME)", "${TARGET_NAME}", "$(TARGET_NAME:c99extidentifier)"})


def _name_from(settings: dict, target: str) -> str | None:
    module = settings.get("PRODUCT_MODULE_NAME")
    if module is not None and module != "$(PRODUCT_NAME:c99extidentifier)":
        return _c99(module) if isinstance(module, str) and "$" not in module else None
    product = settings.get("PRODUCT_NAME", "$(TARGET_NAME)")
    if not isinstance(product, str):
        return None
    if product in _TARGET_NAME:
        product = target
    return None if "$" in product else _c99(product)


def _configurations(list_id, objects: dict) -> list[dict]:
    listing = objects.get(list_id) if isinstance(list_id, str) else None
    if not isinstance(listing, dict):
        return []
    found = [objects.get(c) for c in listing.get("buildConfigurations", [])]
    return [c for c in found if isinstance(c, dict)]


def _module_name(target: dict, project: dict, objects: dict) -> str | None:
    """The module a target builds, when every configuration agrees on a name
    the project file spells out; None otherwise. An ``.xcconfig`` (a base
    configuration) can set any of it, so one anywhere makes it unreadable."""
    title = target.get("name")
    if not isinstance(title, str):
        return None
    inherited = {c.get("name"): c for c in _configurations(project.get("buildConfigurationList"), objects)}
    configs = _configurations(target.get("buildConfigurationList"), objects)
    if not configs:
        return None
    names: set[str | None] = set()
    for config in configs:
        base = inherited.get(config.get("name"), {})
        if "baseConfigurationReference" in config or "baseConfigurationReference" in base:
            return None
        settings = {**(base.get("buildSettings") or {}), **(config.get("buildSettings") or {})}
        if any(isinstance(k, str) and k.startswith(("PRODUCT_NAME[", "PRODUCT_MODULE_NAME[")) for k in settings):
            return None  # a conditional setting: which applies depends on the build
        names.add(_name_from(settings, title))
    return names.pop() if len(names) == 1 else None


class _Groups:
    """Where each group, folder and file reference of a project lives."""

    def __init__(self, objects: dict, main_group: object, base: Path) -> None:
        self._objects = objects
        self._main = main_group
        self._base = base
        self._parents: dict[str, str] = {}
        for oid, obj in objects.items():
            if isinstance(obj, dict) and obj.get("isa") in ("PBXGroup", "PBXVariantGroup", "XCVersionGroup"):
                for child in obj.get("children", []):
                    if isinstance(child, str):
                        self._parents[child] = oid

    def location(self, oid: object, depth: int = 0) -> Path | None:
        obj = self._objects.get(oid) if isinstance(oid, str) else None
        if not isinstance(obj, dict) or depth > 64:
            return None
        tree = obj.get("sourceTree", "<group>")
        path = obj.get("path", "")
        if not isinstance(path, str):
            return None
        if tree == "SOURCE_ROOT":
            return (self._base / path).resolve()
        if tree != "<group>":
            return None
        if oid == self._main:
            return (self._base / path).resolve()
        parent = self._parents.get(oid)
        base = None if parent is None else self.location(parent, depth + 1)
        return None if base is None else (base / path).resolve()


@dataclass(frozen=True)
class _Project:
    #: Where the project's files live; the Swift files under any of these
    #: are unknown when the project is opaque.
    roots: tuple[Path, ...]
    members: dict[Path, set[Module]]
    modules: tuple[Module, ...]
    opaque: bool
    #: Every target's name and product name, kept for an opaque project.
    names: frozenset[str]
    unnamed: bool


_EXCEPTION_SET = "PBXFileSystemSynchronizedBuildFileExceptionSet"


def _synchronized(objects: dict) -> dict[str, list[tuple[object, object, list]]]:
    """Per synchronized root group: ``(target, isa, membershipExceptions)``
    for each exception set it carries."""
    found: dict[str, list[tuple[object, object, list]]] = {}
    for oid, obj in objects.items():
        if not isinstance(obj, dict) or obj.get("isa") != "PBXFileSystemSynchronizedRootGroup":
            continue
        entries = found.setdefault(oid, [])
        for exception_id in obj.get("exceptions", []):
            exception = objects.get(exception_id)
            if not isinstance(exception, dict):
                raise ValueError("an exception set this reader cannot find")
            listed = exception.get("membershipExceptions", [])
            entries.append((exception.get("target"), exception.get("isa"), listed if isinstance(listed, list) else []))
    return found


def _target_files(target_id: str, target: dict, objects: dict, groups: _Groups,
                  swift: list[Path], synchronized: dict) -> set[Path] | None:
    """The Swift files ``target`` compiles; None when one cannot be placed.

    ``swift`` is every Swift file of the checkout this project may claim. A
    claimed file outside it (never written, moved without the project being
    updated, or under a package root) cannot be placed either: its top-level
    names would be invisible to every lookup in the target's module."""
    files: set[Path] = set()
    in_checkout = frozenset(swift)
    for phase_id in target.get("buildPhases", []):
        phase = objects.get(phase_id)
        if not isinstance(phase, dict) or phase.get("isa") != "PBXSourcesBuildPhase":
            continue
        for build_id in phase.get("files", []):
            build = objects.get(build_id)
            if not isinstance(build, dict):
                return None
            if "fileRef" not in build:
                if "productRef" in build:
                    continue  # a package product, compiled from its package
                return None
            reference = objects.get(build["fileRef"]) if isinstance(build["fileRef"], str) else None
            if not isinstance(reference, dict):
                return None
            if reference.get("isa") in ("XCVersionGroup", "PBXVariantGroup"):
                continue  # a Core Data model or localized resource: no checkout Swift
            if reference.get("isa") != "PBXFileReference":
                return None
            location = groups.location(build["fileRef"])
            spelled = str(reference.get("path", ""))
            if location is None:
                if spelled.endswith(".swift"):
                    return None  # generated or SDK-relative Swift this reader cannot place
                continue
            if location.suffix == ".swift":
                if location not in in_checkout:
                    return None
                files.add(location)
    members = target.get("fileSystemSynchronizedGroups", [])
    for group_id in members:
        if group_id not in synchronized:
            return None
    for group_id, entries in synchronized.items():
        mine = [entry for entry in entries if entry[0] == target_id]
        if any(isa != _EXCEPTION_SET for _, isa, _ in mine):
            return None
        exceptions = {str(p).rstrip("/") for _, _, paths in mine for p in paths}
        member = group_id in members
        if not member and not exceptions:
            continue
        folder = groups.location(group_id)
        if folder is None:
            return None
        for path in swift:
            if not within(path, folder):
                continue
            relative = path.relative_to(folder).as_posix()
            excepted = any(relative == p or relative.startswith(p + "/") for p in exceptions)
            if member != excepted:
                files.add(path)
    return files


_NAME_SETTING = re.compile(r'\b(?:PRODUCT_NAME|PRODUCT_MODULE_NAME)\s*=\s*"?([^";\s$]+)"?\s*;')


def _opaque_project(roots: tuple[Path, ...], text: str) -> _Project:
    """A project nothing reliable can be read from: every name it spells is
    kept so an import of one is recognised, and no name can be trusted."""
    spelled = re.findall(r'\bname = "?([^";\n]+?)"?;', text) + _NAME_SETTING.findall(text)
    return _Project(roots, {}, (), True, frozenset(_c99(n) for n in spelled), True)


def _project(pbxproj: Path, swift: list[Path]) -> _Project:
    root = pbxproj.parent.parent
    try:
        text = pbxproj.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _opaque_project((root,), "")
    try:
        plist = _plist(text)
        objects = plist["objects"]
        project = objects[plist["rootObject"]]
        if not isinstance(objects, dict) or not isinstance(project, dict):
            raise ValueError("not a project")
    except (ValueError, KeyError, TypeError):
        return _opaque_project((root,), text)
    directory = project.get("projectDirPath", "")
    if not isinstance(directory, str):
        return _opaque_project((root,), text)
    base = (root / directory).resolve()
    try:
        return _read_project(pbxproj, base, objects, project, swift)
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return _opaque_project((root, base), text)


def _read_project(pbxproj: Path, base: Path, objects: dict, project: dict, swift: list[Path]) -> _Project:
    root = pbxproj.parent.parent
    groups = _Groups(objects, project.get("mainGroup"), base)
    synchronized = _synchronized(objects)
    members: dict[Path, set[Module]] = {}
    modules: list[Module] = []
    names: set[str] = set()
    opaque = unnamed = False
    for target_id in project.get("targets", []):
        target = objects.get(target_id)
        if not isinstance(target, dict) or target.get("isa") != "PBXNativeTarget":
            continue
        title = target.get("name")
        if isinstance(title, str):
            names.add(_c99(title))
        name = _module_name(target, project, objects)
        if name is not None:
            names.add(name)
        unnamed |= name is None
        module = Module(f"{pbxproj}#{target_id}", name)
        modules.append(module)
        files = _target_files(target_id, target, objects, groups, swift, synchronized)
        if files is None:
            opaque = True
            continue
        for path in files:
            members.setdefault(path, set()).add(module)
    return _Project((root, base), members, tuple(modules), opaque, frozenset(names), unnamed)


# --- the layout ---------------------------------------------------------------

_CACHE: dict[str, tuple[tuple, Layout]] = {}


def _stamp(path: Path) -> tuple:
    try:
        info = path.stat()
    except OSError:
        return (str(path), None)
    return (str(path), info.st_mtime_ns, info.st_size)


def layout(root: str) -> Layout:
    """The module of every Swift file of the checkout at ``root``.

    Memoised on the Swift file list (a synchronized Xcode folder and a
    SwiftPM default directory are both decided by which files exist) and on
    every build file's stamp.
    """
    base = Path(root).resolve()
    swift = sorted(
        {Path(f.resolved) for f in source_index.files(str(base), {".swift"}, with_ignored=True)}
    )
    manifests = [p for p in swift if p.name == "Package.swift"]
    projects = _projects(base)
    key = (tuple(swift), tuple(_stamp(p) for p in (*manifests, *projects)))
    cached = _CACHE.get(str(base))
    if cached is not None and cached[0] == key:
        return cached[1]
    built = _build(swift, manifests, projects)
    _CACHE[str(base)] = (key, built)
    return built


def _build(swift: list[Path], manifests: list[Path], projects: list[Path]) -> Layout:
    by_file: dict[str, set[Module]] = {}
    unknown: set[str] = set()
    modules: set[Module] = set()
    opaque_names: set[str] = set()
    unnamed = False
    # Deepest first: a nested package claims its own subtree.
    packages = sorted((_package(m) for m in manifests), key=lambda p: len(p.root.parts), reverse=True)
    for package in packages:
        if package.targets is None:
            opaque_names |= package.names
            unnamed |= package.unnamed
        else:
            modules |= {module for _, module in package.targets}
    outside: list[Path] = []
    for path in swift:
        home = next((p for p in packages if within(path, p.root)), None)
        if home is None:
            outside.append(path)
        elif home.targets is None:
            unknown.add(str(path))
        else:
            claims = {module for directory, module in home.targets if within(path, directory)}
            if claims:
                by_file[str(path)] = claims
    for pbxproj in projects:
        if any(within(pbxproj, p.root) for p in packages):
            continue
        project = _project(pbxproj, outside)
        unnamed |= project.unnamed
        if project.opaque:
            opaque_names |= project.names
            unknown |= {str(p) for p in outside if any(within(p, r) for r in project.roots)}
            continue
        modules |= set(project.modules)
        for path, claims in project.members.items():
            by_file.setdefault(str(path), set()).update(claims)
    return Layout(
        {path: frozenset(claims) for path, claims in by_file.items()},
        frozenset(unknown),
        frozenset(modules),
        frozenset(opaque_names),
        unnamed,
    )
