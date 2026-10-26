"""The names a Dart import brings into scope, when they can be known.

``import 'package:app/legacy.dart';`` spells none of the names it binds, so
the stranded-binding pass cannot ask the file whether the import is still
needed. It can when the imported library is a file in the same checkout:
that library's public top-level declarations, plus whatever it exports, ARE
the import's namespace, and the differential then runs over that set — the
import was referenced before the fold if any of its names was, and is
stranded only if none is referenced after.

Even ``as legacy`` and ``show LegacyFlow`` need the library, for one reason:
an EXTENSION. Dart applies every extension an import brings in, through a
prefix and whatever a ``show`` lists, wherever its member is called — and
that call spells the member, never the import's prefix or shown names. So
every import's bindings include the extension members of its library, kept
apart in :attr:`Namespace.members` because no combinator filters them out of
the safe set: a ``show`` or ``hide`` beside an export or an import narrows
the TOP-LEVEL names, and keeping a member it may also have removed is only
the superset again.

Soundness rests on the set being complete, never on it being exact. A name
missing from it is one the pass cannot see being used, which deletes an
import the file still needs. A name the set holds that the library does not
really export only makes the import look used — a missed tidy. So every
choice below errs toward MORE names, and every shape whose names cannot all
be enumerated makes the whole library unresolvable (``None``) rather than
smaller:

* the top level is a closed world. Each node type is either a declaration
  whose names are read, or a piece of syntax known to bind nothing; any
  other type refuses, so a grammar construct nobody listed cannot silently
  drop out of the set;
* an ``extension`` contributes its MEMBER names as well as its own. Code
  that relies on one calls a member on a receiver (``context.l10n``,
  ``'s'.shout()``) and never spells the extension, but it does spell the
  member — and a member spelled in a property position counts as a use of
  an import, since only a variable's count excludes those. Two ways of
  invoking a member spell nothing, an operator (``a + b``) and ``call``
  (``x()``), so an extension declaring either refuses;
* a ``dart:`` library, another package, a conditional URI and anything
  outside the checkout refuse, because the declarations are not in hand.
  The one exception is the Featureflip Dart SDK itself (see
  :func:`audited_sdk`), whose namespace was read off every published
  version and is carried here as a table;
* an ``export`` recurses, honouring ``show`` and ``hide`` for top-level
  names only, and inherits the refusal of anything it cannot resolve.
  ``part`` files are read as part of the library they belong to.

Library-private names (a leading ``_``) are dropped: they cannot be imported.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import tree_sitter_dart
from tree_sitter import Language, Node, Parser

#: Bounds the export chain. Real libraries re-export a level or two; a chain
#: this long is far more likely a cycle the ``seen`` set has not caught, and
#: giving up costs only a missed tidy.
_MAX_DEPTH = 16

#: Top-level node types that bind no importable name: directives, comments,
#: a function's body (its name is on the signature beside it), and the
#: pieces a top-level variable declaration is FLATTENED into in this grammar
#: — ``int top = 1;`` arrives as ``type_identifier``,
#: ``initialized_identifier_list`` and ``;`` side by side under the program.
_BINDS_NOTHING = frozenset({
    "comment", "documentation_comment", "library_name", "part_of_directive",
    "function_body", "annotation", "marker_annotation", "type_identifier",
    "nullable_type", "type_arguments", "function_type", "void_type",
    "inferred_type", "final_builtin", "const_builtin", "late", "external",
    "static", "covariant",
})

#: Declarations whose single name is the node's ``name`` field.
_NAMED_BY_FIELD = frozenset({
    "class_definition", "enum_declaration", "extension_type_declaration",
    "function_signature", "getter_signature", "setter_signature",
})

#: The flattened variable lists, and the child type each name sits in.
_VARIABLE_LISTS = {
    "initialized_identifier_list": "initialized_identifier",
    "static_final_declaration_list": "static_final_declaration",
}

_PUBSPEC_NAME = re.compile(r"^name:\s*['\"]?([A-Za-z_]\w*)['\"]?\s*$", re.MULTILINE)
_QUOTED = re.compile(r"""^(['"])([^'"$\\]*)\1$""")

#: The pub.dev package of the Featureflip Dart SDK.
AUDITED_SDK_PACKAGE = "featureflip"

#: Every public library of that package, mapped to every top-level name it
#: exports in ANY version of :data:`AUDITED_SDK_VERSIONS`. Read off each
#: version's published archive with :func:`library_names`, exports followed:
#: no version exports another package or a ``dart:`` library, and none
#: declares an extension or an extension type anywhere under ``lib/``, so no
#: call can depend on the import without spelling one of these names. A
#: union across versions is a superset of any one version's namespace, which
#: is the safe direction (see the module docstring).
AUDITED_SDK: dict[str, frozenset[str]] = {
    "package:featureflip/featureflip.dart": frozenset({
        "EvaluateResponse", "EvaluationEvent", "EvaluationInspector",
        "FeatureflipClient", "FeatureflipConfig", "FeatureflipProvider",
        "FlagValue", "SdkEvent",
    }),
}

#: The published versions that table was read from. A later release may
#: export a name the table lacks, so the table applies only to a package
#: whose ``pubspec.lock`` resolves the SDK to one of these.
AUDITED_SDK_VERSIONS = frozenset({
    "0.1.0", "1.0.3", "1.0.4", "2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0",
    "2.4.1", "2.5.0", "2.6.0", "2.6.1", "2.6.2", "2.6.3", "2.7.0", "2.8.0",
})

#: The hosts a lock entry may name for the SDK: pub.dev and its old name. Any
#: other server may publish a different package under the same name.
_PUB_DEV = frozenset({"https://pub.dev", "https://pub.dartlang.org"})
_MENTIONS_SDK = re.compile(rf"\b{AUDITED_SDK_PACKAGE}\b")
_LOCK_FIELD = re.compile(r"^\s+(source|version|url):\s*['\"]?([^'\"\s]*)['\"]?\s*$")


@dataclass(frozen=True)
class Namespace:
    """What one library makes available to a file that imports it."""

    #: Public top-level names, after every export's ``show``/``hide``.
    names: frozenset[str]
    #: Every extension member name, which no combinator removes; see the
    #: module docstring.
    members: frozenset[str]

    def __or__(self, other: Namespace) -> Namespace:
        return Namespace(self.names | other.names, self.members | other.members)


_EMPTY = Namespace(frozenset(), frozenset())


@lru_cache(maxsize=None)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_dart.language()))


def parse(source: bytes) -> Node:
    """``source`` parsed with this module's grammar, errors and all."""
    return _parser().parse(source).root_node


def library_names(path: Path, root: Path) -> Namespace | None:
    """Everything the library at ``path`` exports, or ``None`` if any of it
    cannot be known; see the module docstring."""
    return _library_names(path, root, set(), 0)


def uri_text(node: Node) -> str | None:
    """The text of a ``uri`` node's plain quoted string; ``None`` for a raw,
    multi-line or interpolated one."""
    match = _QUOTED.match(node.text.decode("utf-8", "replace"))
    return match.group(2) if match else None


def resolver(importer: str, root: str) -> Callable[[str], Namespace | None]:
    """A memoised ``uri -> Namespace`` lookup for imports written in ``importer``.

    ``root`` bounds every file read: a URI that resolves outside it is
    treated as unresolvable rather than followed, since the checkout is the
    only tree this Action was handed.
    """
    importer_path = Path(importer).resolve()
    root_path = Path(root).resolve()
    cache: dict[str, Namespace | None] = {}

    def resolve(uri: str) -> Namespace | None:
        if uri not in cache:
            target = locate(uri, importer_path, root_path)
            cache[uri] = (
                audited_sdk(uri, importer_path, root_path)
                if target is None
                else _library_names(target, root_path, set(), 0)
            )
        return cache[uri]

    return resolve


def audited_sdk(uri: str, importer: Path, root: Path) -> Namespace | None:
    """What an import of a Featureflip Dart SDK library brings into a file at
    ``importer``, from :data:`AUDITED_SDK`; ``None`` when ``uri`` is not one
    of its libraries, or the name ``featureflip`` may not mean the audited
    package there.

    It does when the importer's package (the nearest ``pubspec.yaml``) is not
    itself named ``featureflip``, overrides nothing of that name (in
    ``dependency_overrides`` or a ``pubspec_overrides.yaml``), is not a pub
    workspace member (whose lock lives elsewhere), and has a
    ``pubspec.lock`` beside it that resolves ``featureflip`` to an audited
    version hosted on pub.dev. A path, git or other-server source, a version
    the table was not read from, or a lock that cannot be read leaves the
    import unknown, as any other package's is.

    The namespace has no extension members: no audited version declares an
    extension.
    """
    names = AUDITED_SDK.get(uri)
    if names is None:
        return None
    home = package_root(importer, root)
    if home is None or package_name(home) == AUDITED_SDK_PACKAGE:
        return None
    pubspec = _read_text(home / "pubspec.yaml", root)
    lock = _read_text(home / "pubspec.lock", root)
    if pubspec is None or lock is None or _substitutes_sdk(pubspec):
        return None
    overrides = home / "pubspec_overrides.yaml"
    if overrides.exists() or overrides.is_symlink():
        text = _read_text(overrides, root)
        if text is None or _MENTIONS_SDK.search(text):
            return None
    if _locked_sdk_version(lock) not in AUDITED_SDK_VERSIONS:
        return None
    return Namespace(names, frozenset())


def _read_text(path: Path, root: Path) -> str | None:
    """``path``'s text, if it is a file inside ``root`` (a symlink out of the
    checkout is not followed)."""
    try:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _substitutes_sdk(pubspec: str) -> bool:
    """Whether ``pubspec`` may resolve ``featureflip`` other than through its
    lock: it is that package however its ``name:`` is written, a
    ``dependency_overrides`` section mentions it, or it is a
    ``resolution: workspace`` member (its lock is the workspace's)."""
    section = None
    for line in pubspec.splitlines():
        if line[:1] not in ("", " ", "\t", "#"):
            section = line
            key = line.split(":", 1)[0].strip(" '\"")
            if key == "resolution" and "workspace" in line:
                return True
            if key == "name" and _MENTIONS_SDK.search(line):
                return True
        if section is not None and "dependency_overrides" in section and _MENTIONS_SDK.search(line):
            return True
    return False


def _locked_sdk_version(lock: str) -> str | None:
    """The version ``lock`` resolves ``featureflip`` to, when it is the one
    entry of that name and is hosted on pub.dev; otherwise ``None``."""
    lines = lock.splitlines()
    if "packages:" not in lines:
        return None
    entries = [i for i, line in enumerate(lines) if line.rstrip() == f"  {AUDITED_SDK_PACKAGE}:"]
    if len(entries) != 1 or entries[0] < lines.index("packages:"):
        return None
    fields: dict[str, str] = {}
    for line in lines[entries[0] + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        match = _LOCK_FIELD.match(line)
        if match is not None:
            if match.group(1) in fields:
                return None
            fields[match.group(1)] = match.group(2)
    if fields.get("source") != "hosted" or fields.get("url") not in _PUB_DEV:
        return None
    return fields.get("version")


def locate(uri: str, importer: Path, root: Path) -> Path | None:
    """The file ``uri`` names, relative to ``importer``, if it is in ``root``.

    A relative URI resolves against the importer's directory. A ``package:``
    URI resolves only for the importer's OWN package — the nearest
    ``pubspec.yaml`` above it, within ``root``, whose ``name`` matches —
    to that package's ``lib/``. Every other scheme, ``dart:`` included,
    is out of reach.
    """
    if uri.startswith("package:"):
        package, _, rest = uri[len("package:"):].partition("/")
        home = package_root(importer, root)
        if home is None or not rest or package_name(home) != package:
            return None
        target = home / "lib" / rest
    elif ":" in uri.split("/", 1)[0]:
        return None
    else:
        target = importer.parent / uri
    target = target.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def package_root(importer: Path, root: Path) -> Path | None:
    """The nearest directory above ``importer`` holding a ``pubspec.yaml``
    that is itself inside ``root`` — resolved, so a symlink out of the
    checkout is not followed."""
    directory = importer.parent
    while directory.is_relative_to(root):
        pubspec = directory / "pubspec.yaml"
        if pubspec.is_file():
            return directory if pubspec.resolve().is_relative_to(root) else None
        if directory == root:
            break
        directory = directory.parent
    return None


def package_name(home: Path) -> str | None:
    try:
        text = (home / "pubspec.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _PUBSPEC_NAME.search(text)
    return match.group(1) if match else None


def uri_of(directive: Node) -> str | None:
    """The plain URI of an import or export directive, or ``None`` when it is
    conditional or not a simple quoted string.

    A ``configurable_uri`` holding anything but its ``uri`` is a conditional
    import (``if (dart.library.io) 'io.dart'``), whose target depends on the
    platform it is compiled for. A ``deferred`` import holds a bare ``uri``
    instead, since it cannot be conditional. This grammar exposes no string content, so
    the URI is read off the literal's text; a raw, multi-line or
    interpolated string is declined rather than unescaped.
    """
    configurable = next(
        (c for c in directive.children if c.type in {"configurable_uri", "uri"}),
        None,
    )
    if configurable is None or configurable.named_child_count != 1:
        return None
    match = _QUOTED.match(configurable.text.decode("utf-8", "replace"))
    return match.group(2) if match else None


def _library_names(
    path: Path, root: Path, seen: set[Path], depth: int
) -> Namespace | None:
    """Everything the library at ``path`` exports; ``None`` if any of it
    cannot be known (see the module docstring)."""
    if depth > _MAX_DEPTH:
        return None
    if path in seen:
        # Already being collected further up the chain; its names arrive
        # there. Only an export cycle reaches this.
        return _EMPTY
    seen = seen | {path}
    program = read(path)
    if program is None:
        return None
    if any(c.type == "part_of_directive" for c in program.named_children):
        # A part cannot be imported; whatever this is, it is not a library.
        return None
    collected = declared_names(program)
    if collected is None:
        return None
    for child in program.named_children:
        if child.type == "part_directive":
            part = _part_names(child, path, root)
            if part is None:
                return None
            collected |= part
            continue
        if child.type != "import_or_export":
            continue
        export = next((c for c in child.named_children if c.type == "library_export"), None)
        if export is None:
            continue
        exported = _exported_names(export, path, root, seen, depth)
        if exported is None:
            return None
        collected |= exported
    return Namespace(
        frozenset(n for n in collected.names if not n.startswith("_")),
        frozenset(n for n in collected.members if not n.startswith("_")),
    )


def read(path: Path) -> Node | None:
    try:
        source = path.read_bytes()
    except OSError:
        return None
    program = parse(source)
    return None if program.has_error else program


def _part_names(directive: Node, library: Path, root: Path) -> Namespace | None:
    uri_node = next((c for c in directive.named_children if c.type == "uri"), None)
    if uri_node is None:
        return None
    uri = uri_text(uri_node)
    if uri is None:
        return None
    target = locate(uri, library, root)
    if target is None:
        return None
    program = read(target)
    return None if program is None else declared_names(program)


def _exported_names(
    export: Node, library: Path, root: Path, seen: set[Path], depth: int
) -> Namespace | None:
    uri = uri_of(export)
    target = None if uri is None else locate(uri, library, root)
    if target is None:
        return None
    namespace = _library_names(target, root, seen, depth + 1)
    if namespace is None:
        return None
    names = namespace.names
    for combinator in (c for c in export.named_children if c.type == "combinator"):
        listed = {
            c.text.decode("utf-8", "replace")
            for c in combinator.named_children
            if c.type == "identifier"
        }
        keyword = combinator.children[0].type if combinator.children else ""
        if keyword == "show":
            names = names & listed
        elif keyword == "hide":
            names = names - listed
        else:
            return None
    return Namespace(names, namespace.members)


#: What an extension body holds besides its members' bodies.
_EXTENSION_BODY_NOISE = frozenset({
    "function_body", "annotation", "marker_annotation", "comment",
    "documentation_comment",
})

#: Where a member's name sits inside a ``method_signature`` or
#: ``declaration``: a signature's ``name`` field, or the first identifier of
#: an entry in a static field list.
_MEMBER_SIGNATURES = frozenset({
    "function_signature", "getter_signature", "setter_signature",
})
_MEMBER_FIELD_ENTRIES = frozenset({"static_final_declaration", "initialized_identifier"})


def _extension_member_names(extension: Node) -> frozenset[str] | None:
    """Every member name an extension declares; ``None`` when one of them
    can be invoked without being spelled, or cannot be read.

    Each member must yield at least one name: a member this function finds
    no name in is one it does not understand, and an unseen member is
    exactly how a use goes uncounted.
    """
    body = extension.child_by_field_name("body")
    if body is None:
        return None
    names: set[str] = set()
    for member in body.named_children:
        if member.type in _EXTENSION_BODY_NOISE:
            continue
        if member.type not in {"method_signature", "declaration"}:
            return None
        found = False
        stack = [member]
        while stack:
            node = stack.pop()
            if node.type == "operator_signature":
                return None
            if node.type in _MEMBER_SIGNATURES:
                name = node.child_by_field_name("name")
                if name is None:
                    return None
                names.add(name.text.decode("utf-8", "replace"))
                found = True
                continue
            if node.type in _MEMBER_FIELD_ENTRIES:
                name = next(
                    (c for c in node.named_children if c.type == "identifier"), None
                )
                if name is None:
                    return None
                names.add(name.text.decode("utf-8", "replace"))
                found = True
                continue
            stack.extend(node.named_children)
        if not found:
            return None
    if "call" in names:
        return None
    return frozenset(names)


def declared_names(program: Node) -> Namespace | None:
    """The names declared directly in ``program``, private ones included;
    ``None`` when the top level holds anything this function cannot read."""
    names: set[str] = set()
    members: set[str] = set()
    for child in program.named_children:
        kind = child.type
        if kind in _BINDS_NOTHING or kind in {"import_or_export", "part_directive"}:
            continue
        if kind == "extension_declaration":
            extension_members = _extension_member_names(child)
            if extension_members is None:
                return None
            members |= extension_members
            name = child.child_by_field_name("name")
            if name is not None:
                names.add(name.text.decode("utf-8", "replace"))
        elif kind in _NAMED_BY_FIELD:
            name = child.child_by_field_name("name")
            if name is None:
                return None
            names.add(name.text.decode("utf-8", "replace"))
        elif kind == "mixin_declaration":
            name = next((c for c in child.named_children if c.type == "identifier"), None)
            if name is None:
                return None
            names.add(name.text.decode("utf-8", "replace"))
        elif kind == "type_alias":
            # Every type name in the alias's head, not just the one being
            # declared: `typedef Foo Bar(int x)` puts the return type beside
            # the name, and an extra name is the safe direction.
            head = [c for c in child.named_children if c.type == "type_identifier"]
            if not head:
                return None
            names.update(c.text.decode("utf-8", "replace") for c in head)
        elif kind in _VARIABLE_LISTS:
            entry = _VARIABLE_LISTS[kind]
            for declaration in child.named_children:
                if declaration.type != entry:
                    return None
                name = next(
                    (c for c in declaration.named_children if c.type == "identifier"),
                    None,
                )
                if name is None:
                    return None
                names.add(name.text.decode("utf-8", "replace"))
        else:
            return None
    return Namespace(frozenset(names), frozenset(members))
