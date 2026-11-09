"""Which Java and Kotlin classes may have an unread private field reported.

A class is *eligible* only when nothing that reads its fields reflectively can
be at work, and that is decided by construction, not by naming the frameworks
that do. All three must hold:

* every class-level annotation is on a short whitelist of annotations known
  not to read fields (a stereotype Spring uses to build a bean, a marker such
  as ``@Deprecated``, a Lombok logger annotation);
* no field of the class carries an annotation other than the write-only ones
  (:data:`~flag_cleanup.jvm_unread_members.WRITERS`: injection and
  nullability), since a mapping annotation on a SIBLING field (``@Id``,
  ``@SerializedName``, ``@XmlElement``) marks a class a library reads whole;
* every supertype is proven. Each is first resolved to a package-qualified
  name through the file's single-type imports, its own package and any
  qualification; then it must be a whitelisted JDK type at its JDK location,
  or declared in the repository IN THAT PACKAGE and itself eligible,
  recursively (a bare name matching an unrelated class elsewhere proves
  nothing). A star import that could supply the name leaves it unproven
  unless it is a JDK package. A supertype this cannot see
  (``extends BaseDto``, ``implements Dto`` from a library) may be
  ``Serializable`` or a persistence base, so it makes the class ineligible.

A Kotlin ``data`` or ``value`` class is never eligible: its generated members
read every property.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from flag_cleanup import jvm_unread_members as jvm
from flag_cleanup import source_index
from flag_cleanup.unused import _parse

#: Class annotations that do not read fields. ``Deprecated``, ``Suppress*`` and
#: ``FunctionalInterface`` are markers. ``Generated`` marks tool output.
#: Spring's stereotypes make the container build a bean and inject into it
#: (a write); none of them serializes or maps it.
CLASS_ANNOTATIONS = frozenset(
    {
        "Deprecated", "SuppressWarnings", "Suppress", "FunctionalInterface", "SafeVarargs", "Generated",
        "Service", "Component", "Repository", "Configuration", "RestController", "Controller",
    }
)
#: Lombok annotations that read no field (loggers, try/finally, constructors), counted only in a file importing Lombok.
LOMBOK_INERT = frozenset(
    {
        "Slf4j", "XSlf4j", "Log4j", "Log4j2", "Log", "CommonsLog", "Flogger", "JBossLog", "CustomLog",
        "SneakyThrows", "Synchronized", "Cleanup",
        # Constructor generators only WRITE the fields.
        "RequiredArgsConstructor", "AllArgsConstructor", "NoArgsConstructor",
    }
)
#: Supertypes with no serialization or mapping meaning, by the package that
#: declares them. A name counts only when it resolves to one of these packages.
JDK_SAFE = {
    "java.lang": frozenset({"Object", "Runnable", "AutoCloseable", "Comparable", "Iterable", "Cloneable"}),
    "java.util.function": frozenset({"Supplier", "Consumer", "Function", "Predicate", "BiFunction"}),
    "java.util.concurrent": frozenset({"Callable"}),
    "java.io": frozenset({"Closeable"}),
    "java.util": frozenset({"Comparator"}),
    "kotlin": frozenset({"Any", "Comparable", "Comparator", "Cloneable"}),
    "kotlin.collections": frozenset({"Iterable"}),
}
#: Packages a name resolves into with no import at all.
_DEFAULT_PACKAGES = {"java": ("java.lang",), "kt": ("java.lang", "kotlin", "kotlin.collections")}
#: Star imports that cannot shadow a name with something unproven.
_JDK_STARS = frozenset(JDK_SAFE)
_IMPORT = re.compile(r"import\s+(static\s+)?([\w.]+?)(\.\*)?(?:\s+as\s+(\w+))?\s*;?\s*$")
_PACKAGE = re.compile(r"package\s+([\w.]+)")

_JAVA_TYPES = frozenset({"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"})
_KT_TYPES = frozenset({"class_declaration", "object_declaration"})
_DECLARES = re.compile(r"\b(?:class|interface|enum|object|record|typealias)\s+([A-Za-z_]\w*)")


def _types(root: Node, grammar: str) -> Iterator[Node]:
    kinds = _JAVA_TYPES if grammar == "java" else _KT_TYPES
    return (n for n in jvm.walk(root) if n.type in kinds)


def _dotted(node: Node) -> str:
    """``a.b.Foo`` of ``a.b.Foo<T>``: the base type's dotted name, no arguments."""
    if node.type == "user_type":
        names = [c for c in node.named_children if c.type in ("identifier", "type_identifier")]
        return ".".join(jvm.text(n) for n in names)
    if node.type == "generic_type" and node.named_children:
        return _dotted(node.named_children[0])
    return "".join(jvm.text(node).split("<", 1)[0].split())


def supertypes(declaration: Node, grammar: str) -> list[str]:
    """The dotted name, as written, of every type ``declaration`` extends or implements."""
    found: list[str] = []
    if grammar == "java":
        for part in declaration.named_children:
            if part.type in ("superclass", "super_interfaces", "extends_interfaces"):
                for ref in jvm.walk(part):
                    if (
                        ref.type in ("type_identifier", "scoped_type_identifier", "generic_type")
                        and not (ref.parent is not None and ref.parent.type in ("scoped_type_identifier", "generic_type"))
                        and not any(a.type == "type_arguments" for a in _ancestors(ref, part))
                    ):
                        found.append(_dotted(ref))
        return found
    specifiers = jvm.child_of_type(declaration, "delegation_specifiers")
    for ref in jvm.walk(specifiers) if specifiers is not None else ():
        if ref.type == "user_type" and not (ref.parent is not None and ref.parent.type == "type_arguments"):
            if not any(a.type == "type_arguments" for a in _ancestors(ref, specifiers)):
                found.append(_dotted(ref))
    return found


class _Header:
    """A file's package and imports."""

    def __init__(self, root: Node):
        self.package = ""
        self.single: dict[str, str] = {}
        self.stars: list[str] = []
        for child in root.named_children:
            body = jvm.text(child).strip()
            if child.type in ("package_declaration", "package_header"):
                found = _PACKAGE.match(body)
                self.package = found.group(1) if found else ""
            elif child.type in ("import_declaration", "import"):
                found = _IMPORT.match(body)
                if found is None:
                    self.stars.append("?")  # an import this cannot read could supply anything
                elif found.group(3):
                    self.stars.append(found.group(2))
                else:
                    self.single[found.group(4) or found.group(2).rsplit(".", 1)[-1]] = found.group(2)


def _ancestors(node: Node, stop: Node) -> Iterator[Node]:
    current = node.parent
    while current is not None and current.id != stop.id:
        yield current
        current = current.parent


class Eligibility:
    """Decides :func:`class_ok` for a checkout; ``repo_dir=None`` sees no
    repository, so only the whitelist proves a supertype."""

    def __init__(self, repo_dir: str | None = None):
        self._repo_dir = repo_dir
        self._index: dict[str, list[str]] | None = None
        self._trees: dict[str, tuple[Node, str, str]] = {}
        self._packages: dict[str, str] = {}

    def _names(self) -> dict[str, list[str]]:
        if self._index is None:
            self._index = {}
            if self._repo_dir is not None:
                for source in source_index.files(self._repo_dir, (".java", ".kt"), with_ignored=True):
                    try:
                        body = source.path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    for name in set(_DECLARES.findall(body)):
                        self._index.setdefault(name, []).append(str(source.path))
        return self._index

    def _tree(self, path: str) -> tuple[Node, str, str] | None:
        if path not in self._trees:
            grammar = "kt" if path.endswith(".kt") else "java"
            try:
                body = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                return None
            self._trees[path] = (_parse(body, grammar), grammar, body)
        return self._trees[path]

    def _package_of(self, path: str) -> str | None:
        if path not in self._packages:
            tree = self._tree(path)
            if tree is None:
                return None
            self._packages[path] = _Header(tree[0]).package
        return self._packages[path]

    def _declared_in(self, name: str, package: str) -> list[str]:
        return [p for p in self._names().get(name, []) if self._package_of(p) == package]

    def class_ok(
        self, declaration: Node, grammar: str, source: str, _seen: frozenset[str] = frozenset()
    ) -> bool:
        """Whether ``declaration``, from ``source``, satisfies all three rules."""
        lombok = "import lombok" in source
        modifiers = jvm.java_modifiers(declaration) if grammar == "java" else jvm.child_of_type(declaration, "modifiers")
        names = jvm.java_annotations(modifiers) if grammar == "java" else jvm.kotlin_annotations(modifiers)
        if any(n not in CLASS_ANNOTATIONS and not (lombok and n in LOMBOK_INERT) for n in names):
            return False
        if grammar == "kt" and jvm.kotlin_modifier_words(declaration) & {"data", "value", "inline"}:
            return False
        listed = jvm.java_fields(declaration) if grammar == "java" else jvm.kotlin_properties(declaration)
        if any(not set(jvm._annotations(member, grammar)) <= jvm.WRITERS for member, _ in listed):
            return False
        root = declaration
        while root.parent is not None:
            root = root.parent
        header = _Header(root)
        local: dict[str, list[Node]] = {}
        for node in _types(root, grammar):
            declared = jvm.class_name(node, grammar)
            if declared is not None:
                local.setdefault(declared, []).append(node)
        for node in jvm.walk(root):
            if node.type == "type_alias":  # a name bound to an alias is never followed: unproven
                alias = jvm.child_of_type(node, "identifier")
                if alias is not None:
                    local.setdefault(jvm.text(alias), [])
        own = jvm.class_name(declaration, grammar) or ""
        seen = _seen | {_join(header.package, own)}
        return all(self._supertype_ok(name, local, grammar, header, source, seen) for name in supertypes(declaration, grammar))

    def _resolve(self, dotted: str, local: dict[str, list[Node]], grammar: str, header: _Header) -> tuple[str, str] | None:
        """``(package, name)`` the written ``dotted`` names, or ``None`` when it cannot be proven."""
        first, _, rest = dotted.partition(".")
        known = first in header.single or first in local or bool(self._declared_in(first, header.package))
        if rest:
            if known:
                return None  # a nested type of a known one
            package, _, name = dotted.rpartition(".")
            return package, name
        if first in header.single:
            package, _, name = header.single[first].rpartition(".")
            return package, name
        if first in local or self._declared_in(first, header.package):
            return header.package, first
        if any(star not in _JDK_STARS for star in header.stars):
            return None  # a star import could supply it
        for package in (*(p for p in header.stars if first in JDK_SAFE.get(p, ())), *_DEFAULT_PACKAGES[grammar]):
            if first in JDK_SAFE.get(package, ()):
                return package, first
        return None

    def _supertype_ok(
        self, written: str, local: dict[str, list[Node]], grammar: str, header: _Header, source: str, seen: frozenset[str]
    ) -> bool:
        resolved = self._resolve(written, local, grammar, header)
        if resolved is None:
            return False
        package, name = resolved
        if name in JDK_SAFE.get(package, ()):
            return True
        qualified = _join(package, name)
        if qualified in seen:
            return False  # a cycle
        proven = False
        if package == header.package and name in local:
            for d in local[name]:
                if not self.class_ok(d, grammar, source, seen):
                    return False
            proven = bool(local[name])  # an alias has no declaration to prove
        for path in self._declared_in(name, package):
            tree = self._tree(path)
            if tree is None:
                return False
            root, other, body = tree
            for d in _types(root, other):
                if jvm.class_name(d, other) == name:
                    if not self.class_ok(d, other, body, seen):
                        return False
                    proven = True
        return proven


def _join(package: str, name: str) -> str:
    return f"{package}.{name}" if package else name
