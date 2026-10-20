"""What Java and Kotlin share for keyless wrappers: the repository's declarations.

A wrapper qualifies only on facts about the whole repository -- its name is
declared once, nothing extends its class, the interface it implements has one
implementation and cannot be implemented outside -- and Java and Kotlin see
each other's declarations, so both are read here, together, into one
:class:`Index`. Groovy and Scala files are never parsed: a mention in one is
recorded so the run can report it, because this tool does not rewrite them.

The index reads only the files that mention the wrapper's name, its class's
name or one of that class's supertypes. A declaration that could break a
proof has to spell one of those, so nothing is missed by the narrowing.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, jvm_publishing, wrapper_java, wrapper_kotlin
from flag_cleanup import wrapper_inline as wi
from flag_cleanup.cross_file_jvm_guest import GUEST_EXTENSIONS

logger = logging.getLogger(__name__)

#: Every suffix a JVM declaration can be read from by this tool: the Java and
#: Kotlin profiles' own (spelled out, since those modules import this one).
PARSED_EXTENSIONS = (".java", ".kt", ".kts")
#: JVM languages that can call the wrapper but are never rewritten here.
FOREIGN_EXTENSIONS = tuple(ext for exts in GUEST_EXTENSIONS.values() for ext in exts)

_JAVA_TYPES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "class",
    "annotation_type_declaration": "other",
}


@dataclass(frozen=True)
class Imports:
    """What a file's imports make visible: ``single`` maps each name a single
    import binds (the alias, where there is one) to its fully qualified name."""

    single: dict[str, str]
    wildcards: set[str]


def type_resolves(
    type_text: str,
    file_package: str,
    imports: Imports,
    target_package: str,
    target_name: str,
) -> bool:
    """Whether a declared type, as a file spells it, can only mean the target.

    Java and Kotlin resolve a simple name the same way: a single import first,
    then the file's own package, then a star import. The target's simple name
    is declared once in the repository (:func:`qualify`), so a type from the
    file's own package can only be it, and two star imports both providing the
    name do not compile.
    """
    fqn = f"{target_package}.{target_name}" if target_package else target_name
    if "." in type_text:
        return type_text == fqn
    bound = imports.single.get(type_text)
    if bound is not None:
        return bound == fqn
    if type_text != target_name:
        return False
    return file_package == target_package or target_package in imports.wildcards


@dataclass
class TypeDecl:
    path: str
    package: str
    name: str
    #: ``class``, ``interface``, ``object``, ``enum`` or ``other``.
    kind: str
    top_level: bool
    public: bool
    #: Abstract, or a Kotlin ``open``/``sealed`` class: something may extend it.
    open: bool
    supertypes: tuple[str, ...]
    #: member name -> byte span of its first declaration in this type.
    members: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: property name -> byte span of its first declaration in this type. Kept
    #: apart from ``members`` so a function never matches a property.
    properties: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: An interface a lambda can implement: one abstract member, or a Kotlin
    #: ``fun interface``. Its implementations cannot be counted.
    functional: bool = False


@dataclass
class Index:
    #: method or function name -> ``(path, start byte)`` of every declaration.
    methods: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    #: property name -> ``(path, start byte)`` of every class-body, top-level
    #: and ``val``/``var`` constructor-parameter declaration of it.
    properties: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    types: dict[str, list[TypeDecl]] = field(default_factory=lambda: defaultdict(list))
    #: Simple names something in the repository extends or implements.
    subclassed: set[str] = field(default_factory=set)
    #: interface simple name -> how many classes, objects and anonymous
    #: classes implement it.
    implementations: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Modifier words on the wrapper's own declaration.
    wrapper_words: set[str] = field(default_factory=set)
    #: A file that mentions a word could not be read, or holds the word inside
    #: a region it could not parse: nothing about uniqueness can be trusted.
    unreadable: bool = False
    #: Names of Kotlin ``typealias`` declarations: ``f()`` may construct one.
    type_aliases: set[str] = field(default_factory=set)
    #: Every parsed file mentioning a word, resolved: where call sites are.
    parsed: list[str] = field(default_factory=list)
    #: Groovy or Scala files mentioning a word.
    foreign: list[str] = field(default_factory=list)
    #: Files of any other kind that mention the wrapper's name: a template,
    #: a layout, a config file. Nothing here can parse them.
    unparsed: list[str] = field(default_factory=list)
    #: ``(path, line, text)``: a Kotlin file reading a Java getter wrapper as a
    #: property (``flags.enabled`` for ``getEnabled()``), which never spells
    #: the method's name.
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)``: a getter method's JavaBeans property (``enableX``
    #: for ``isEnableX()`` or ``getEnableX()``) spelled where a template or an
    #: expression language reads it: a file nothing here parses, or a string
    #: literal (SpEL in ``@PreAuthorize("@flags.enableX")``). Neither spells
    #: the method's name.
    bean_reads: list[tuple[str, int, str]] = field(default_factory=list)
    #: The directory the run was configured with: publication is asked of
    #: the checkout holding it.
    repo_dir: str = ""

    @classmethod
    def build(cls, repo_dir: str, wrapper: wi.Wrapper) -> Index:
        index = cls(repo_dir=repo_dir)
        try:
            source = _exact(wrapper.path)
        except (OSError, UnicodeDecodeError):
            index.unreadable = True
            return index
        own = _types_in(wrapper.path, source)
        owner = next((t for t in own if t.name == wrapper.owner and t.top_level), None)
        words = {wrapper.name, wrapper.owner, *(owner.supertypes if owner else ())}
        index.wrapper_words = _member_words(wrapper, source)
        if wrapper.kind == "property":
            words |= set(getter_names(wrapper.name))
        types = words - {wrapper.name}
        for path in cross_file_keys.mention_files(repo_dir, words, exclude=()):
            if path.endswith(FOREIGN_EXTENSIONS):
                index.foreign.append(path)
                index._add_foreign(path, types)
                continue
            if not path.endswith(PARSED_EXTENSIONS):
                text = _read_text(path)
                if text is None or _word(wrapper.name).search(text):
                    index.unparsed.append(path)
                continue
            try:
                text = _exact(path)
            except (OSError, UnicodeDecodeError):
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            index.parsed.append(path)
            index._add(path, text, words)
        if wrapper.language == "java":
            index.synthetic = _kotlin_property_reads(repo_dir, wrapper.name)
        prop = bean_property(wrapper, source)
        if prop is not None:
            index.bean_reads = _bean_reads(repo_dir, prop)
        if wrapper.language == "kt" and wrapper.kind == "property":
            index.synthetic = jvm_getter_calls(repo_dir, wrapper.name)
        return index

    def _add_foreign(self, path: str, types: set[str]) -> None:
        """Count a Groovy or Scala class, closure coercion or anonymous class
        that extends or implements one of ``types``. Not parsed: a word match
        on the declaration forms, which errs toward counting one too many."""
        text = _read_text(path)
        if text is None:
            self.unreadable = True
            return
        for name in types:
            word = re.escape(name)
            forms = (
                rf"\b(?:extends|implements|with)\s+(?:[\w.]+\s*,\s*)*(?:[\w]+\.)*{word}\b",
                rf"\bas\s+(?:[\w]+\.)*{word}\b",
                rf"\bnew\s+(?:[\w]+\.)*{word}\s*(?:\([^)]*\))?\s*\{{",
            )
            count = sum(len(re.findall(form, text)) for form in forms)
            if count:
                self.subclassed.add(name)
                self.implementations[name] += count

    def _add(self, path: str, text: str, words: set[str]) -> None:
        kotlin = path.endswith((".kt", ".kts"))
        root = wrapper_kotlin.parse(text) if kotlin else wrapper_java.parse(text)
        if root.has_error and _word_in_error(root, words):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        for decl in _types_in(path, text, root):
            self.types[decl.name].append(decl)
            for supertype in decl.supertypes:
                self.subclassed.add(supertype)
                self.implementations[supertype] += 1
        for node in cross_file_keys.walk(root):
            if node.type in ("function_declaration", "method_declaration"):
                name = node.child_by_field_name("name")
                if name is not None:
                    self.methods[_text(name)].append((path, node.start_byte))
            if kotlin and node.type == "type_alias":
                alias = wrapper_kotlin._child(node, "identifier")
                if alias is not None:
                    self.type_aliases.add(_text(alias))
            if kotlin:
                prop_name = _property_name(node)
                if prop_name is not None:
                    self.properties[prop_name].append((path, node.start_byte))
            for supertype in _anonymous_supertypes(node, kotlin):
                self.subclassed.add(supertype)
                self.implementations[supertype] += 1


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | None]:
    """The wrapper with its interface member filled in, or why it does not qualify."""
    if index.unreadable:
        return None, wi.NAME_NOT_UNIQUE
    if wrapper.kind == "function":
        # A top-level function cannot be overridden: its name being declared
        # once -- as a function or as a property, which `x()` can invoke -- is
        # the whole qualification.
        declared = index.methods.get(wrapper.name, []) + index.properties.get(wrapper.name, [])
        if index.types.get(wrapper.name) or wrapper.name in index.type_aliases:
            return None, wi.NAME_NOT_UNIQUE  # `f()` would construct it or call its `invoke`
        return (wrapper, None) if len(declared) == 1 else (None, wi.NAME_NOT_UNIQUE)
    owners = index.types.get(wrapper.owner, [])
    if len(owners) != 1:
        return None, wi.NAME_NOT_UNIQUE
    owner = owners[0]
    member = wrapper
    for supertype in owner.supertypes:
        candidates = index.types.get(supertype, [])
        declared_in = (lambda c: c.properties) if wrapper.kind == "property" else (lambda c: c.members)
        if not any(wrapper.name in declared_in(c) for c in candidates):
            continue
        if len(candidates) != 1:
            return None, wi.NAME_NOT_UNIQUE
        interface = candidates[0]
        if interface.kind != "interface":
            # A superclass in the repository declares the name: an override.
            return None, wi.OVERRIDABLE
        if (
            (interface.public and (not index.repo_dir or jvm_publishing.published(interface.path, index.repo_dir)))
            or interface.functional
            or interface.supertypes
            or index.implementations.get(supertype, 0) != 1
        ):
            # A public interface in a module that may be published can be
            # implemented anywhere. A lambda can implement a functional
            # interface anywhere, and an inherited member can make one
            # functional, so neither is counted.
            return None, wi.INTERFACE_PUBLISHED
        start, end = declared_in(interface)[wrapper.name]
        member = replace(
            wrapper,
            interface=wi.InterfaceMember(
                interface.path, interface.name, interface.package, start, end
            ),
        )
        break
    overrides = bool(index.wrapper_words & {"override", "@Override"})
    if overrides and member.interface is None:
        return None, wi.OVERRIDABLE
    interface_at = None if member.interface is None else (member.interface.path, member.interface.start)
    if wrapper.kind == "property":
        pool = (
            index.properties.get(wrapper.name, [])
            + index.methods.get(wrapper.name, [])
            + [d for n in getter_names(wrapper.name) for d in index.methods.get(n, [])]
        )
    else:
        pool = index.methods.get(wrapper.name, [])
    declared = [d for d in pool if d != interface_at]
    if len(declared) != 1:
        return None, wi.NAME_NOT_UNIQUE
    if (
        owner.open
        or index.wrapper_words & {"open", "abstract"}
        or wrapper.owner in index.subclassed
    ):
        return None, wi.OVERRIDABLE
    return member, None


def getter_names(prop: str) -> tuple[str, ...]:
    """Java method names Kotlin reads as the property ``prop``.

    ``getEnabled()`` is read as ``enabled``; ``isEnabled()`` as ``isEnabled``,
    and so is ``getIsEnabled()``.
    """
    cap = prop[:1].upper() + prop[1:]
    if re.fullmatch(r"is[A-Z]\w*", prop):
        return (prop, f"get{cap}")
    return (f"get{cap}",)


def bean_property(wrapper: wi.Wrapper, source: str) -> str | None:
    """The JavaBeans property a getter method is read as, or None.

    ``isEnableX()`` and ``getEnableX()`` are both read as ``enableX`` by an
    expression language (Thymeleaf, JSP EL, SpEL), and ``getURL()`` as
    ``URL``, as ``java.beans.Introspector`` names it. Only an instance method
    with no parameter is one."""
    match = re.fullmatch(r"(?:get|is)([A-Z][\w$]*)", wrapper.name)
    if wrapper.kind != "method" or wrapper.static or match is None:
        return None
    kotlin = wrapper.language == "kt"
    root = wrapper_kotlin.parse(source) if kotlin else wrapper_java.parse(source)
    for node in cross_file_keys.walk(root):
        if node.start_byte == wrapper.start and node.type in ("function_declaration", "method_declaration"):
            params = (
                wrapper_kotlin._child(node, "function_value_parameters")
                if kotlin
                else node.child_by_field_name("parameters")
            )
            if params is None or any(c.type.endswith("parameter") for c in params.named_children):
                return None
            rest = match.group(1)
            return rest if rest[1:2].isupper() else rest[0].lower() + rest[1:]
    return None


def _bean_reads(repo_dir: str, prop: str) -> list[tuple[str, int, str]]:
    """Every line where a template or an expression language can read the
    property ``prop``: a line spelling it in a file nothing here parses, or a
    string literal spelling it in a Java or Kotlin file."""
    found: list[tuple[str, int, str]] = []
    for path in cross_file_keys.mention_files(repo_dir, [prop], exclude=()):
        if path.endswith(FOREIGN_EXTENSIONS):
            continue
        if path.endswith(PARSED_EXTENSIONS):
            found += [(path, line, text) for line, text in wi._string_mentions(path, prop)]
            continue
        text = _read_text(path)
        if text is None:
            found.append((path, 1, ""))
            continue
        word = _word(prop)
        found += [
            (path, number, " ".join(line.split()))
            for number, line in enumerate(text.splitlines(), start=1)
            if word.search(line)
        ]
    return found


def _property_name(node: Node) -> str | None:
    """The name a Kotlin declaration binds as a member or top-level property:
    a class-body or top-level ``property_declaration``, or a constructor
    parameter declared ``val``/``var``. A local is not one: no ``recv.x``
    can reach it."""
    if node.type == "property_declaration":
        if node.parent is None or node.parent.type not in wrapper_kotlin.MEMBER_SCOPES:
            return None
        name = wrapper_kotlin.declared_name(node)
        return _text(name) if name is not None else None
    if node.type == "class_parameter" and any(c.type in ("val", "var") for c in node.children if not c.is_named):
        name = next((c for c in node.named_children if c.type == "identifier"), None)
        return _text(name) if name is not None else None
    return None


def jvm_getter_calls(repo_dir: str, prop: str) -> list[tuple[str, int, str]]:
    """Every Java, Groovy and Scala line spelling a Kotlin getter's JVM name
    as a word: ``f.getEnabled()`` for ``enabled``, and also a call split
    across lines, a method reference, or Scala's ``f.getEnabled`` with no
    parentheses.

    Any word counts, not just a call: a declaration of that name makes the
    getter's name not unique, so whatever is left can only refer to it."""
    names = getter_names(prop)
    alternatives = "|".join(re.escape(n) for n in names)
    word = re.compile(rf"(?<![\w$])(?:{alternatives})(?![\w$])")
    found: list[tuple[str, int, str]] = []
    for path in cross_file_keys.mention_files(repo_dir, list(names), exclude=()):
        if not path.endswith((".java",) + FOREIGN_EXTENSIONS):
            continue
        text = _read_text(path)
        if text is None:
            found.append((path, 1, ""))
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if word.search(line):
                found.append((path, number, " ".join(line.split())))
    return found


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _exact(path: str) -> str:
    """The file's text with its line endings as they are, like every other
    reader on the wrapper path, so a span taken here (an interface member's)
    is an offset into the file on disk."""
    from flag_cleanup import mock_stubs

    return mock_stubs._read_exact(path)


def _read_text(path: str) -> str | None:
    try:
        return _exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _kotlin_property_reads(repo_dir: str, method: str) -> list[tuple[str, int, str]]:
    """Kotlin reads of a Java getter as a property: ``x.enabled`` for
    ``getEnabled()``, ``x.isEnabled`` for ``isEnabled()``."""
    match = re.fullmatch(r"(get|is)([A-Z]\w*)", method)
    if match is None:
        return []
    prefix, rest = match.groups()
    prop = method if prefix == "is" else rest[0].lower() + rest[1:]
    access = re.compile(rf"\.\s*{re.escape(prop)}(?![\w$])(?!\s*\()")
    found: list[tuple[str, int, str]] = []
    for path in cross_file_keys.mention_files(repo_dir, [prop], exclude=()):
        if not path.endswith((".kt", ".kts")):
            continue
        text = _read_text(path) or ""
        for number, line in enumerate(text.splitlines(), start=1):
            if access.search(line):
                found.append((path, number, " ".join(line.split())))
    return found


def _word_in_error(root: Node, words: set[str]) -> bool:
    for node in cross_file_keys.walk(root):
        if node.type == "ERROR" or node.is_missing:
            text = _text(node)
            if any(word in text for word in words):
                return True
    return False


def _member_words(wrapper: wi.Wrapper, source: str) -> set[str]:
    kotlin = wrapper.language == "kt"
    root = wrapper_kotlin.parse(source) if kotlin else wrapper_java.parse(source)
    for node in cross_file_keys.walk(root):
        if node.start_byte == wrapper.start and node.type in (
            "function_declaration",
            "method_declaration",
            "property_declaration",
        ):
            return (wrapper_kotlin if kotlin else wrapper_java).modifier_words(node)
    return set()


def _last_identifier(node: Node) -> str | None:
    """The simple name a type reference ends in: ``C`` of ``a.b.C<T>``."""
    if node.type in ("type_identifier", "identifier"):
        return _text(node)
    if node.type == "user_type":
        names = [c for c in node.named_children if c.type in ("identifier", "type_identifier")]
        return _text(names[-1]) if names else None
    if node.type == "scoped_type_identifier":
        name = node.named_children[-1] if node.named_children else None
        return _text(name) if name is not None else None
    if node.type == "generic_type" and node.named_children:
        return _last_identifier(node.named_children[0])
    if node.type == "constructor_invocation" and node.named_children:
        return _last_identifier(node.named_children[0])
    return None


def _java_supertypes(node: Node) -> list[str]:
    found: list[str] = []
    for field_name in ("superclass", "interfaces"):
        part = node.child_by_field_name(field_name)
        if part is None:
            continue
        for ref in cross_file_keys.walk(part):
            if ref.parent is not None and ref.parent.type in ("superclass", "type_list"):
                name = _last_identifier(ref)
                if name is not None:
                    found.append(name)
    # An interface extending another lists it under `extends_interfaces`.
    for child in node.named_children:
        if child.type == "extends_interfaces":
            for ref in cross_file_keys.walk(child):
                if ref.parent is not None and ref.parent.type == "type_list":
                    name = _last_identifier(ref)
                    if name is not None:
                        found.append(name)
    return found


def _kotlin_supertypes(node: Node) -> list[str]:
    specifiers = next(
        (c for c in node.named_children if c.type == "delegation_specifiers"), None
    )
    if specifiers is None:
        return []
    found: list[str] = []
    for specifier in specifiers.named_children:
        target = specifier.named_children[0] if specifier.named_children else None
        name = _last_identifier(target) if target is not None else None
        if name is not None:
            found.append(name)
    return found


def _anonymous_supertypes(node: Node, kotlin: bool) -> list[str]:
    if kotlin and node.type == "object_literal":
        return _kotlin_supertypes(node)
    if not kotlin and node.type == "object_creation_expression":
        if any(c.type == "class_body" for c in node.named_children):
            type_node = node.child_by_field_name("type")
            name = _last_identifier(type_node) if type_node is not None else None
            return [name] if name is not None else []
    return []


def _types_in(path: str, text: str, root: Node | None = None) -> list[TypeDecl]:
    kotlin = path.endswith((".kt", ".kts"))
    if root is None:
        root = wrapper_kotlin.parse(text) if kotlin else wrapper_java.parse(text)
    package = wrapper_kotlin.package_of(root) if kotlin else wrapper_java.package_of(root)
    found: list[TypeDecl] = []
    for node in cross_file_keys.walk(root):
        decl = _kotlin_type(node, path, package) if kotlin else _java_type(node, path, package)
        if decl is not None:
            found.append(decl)
    return found


def _java_type(node: Node, path: str, package: str) -> TypeDecl | None:
    kind = _JAVA_TYPES.get(node.type)
    name = node.child_by_field_name("name")
    if kind is None or name is None:
        return None
    words = wrapper_java.modifier_words(node)
    body = node.child_by_field_name("body")
    members: dict[str, tuple[int, int]] = {}
    abstract = 0
    for member in body.named_children if body is not None else ():
        if member.type == "method_declaration":
            member_name = member.child_by_field_name("name")
            if member_name is not None:
                members.setdefault(_text(member_name), (member.start_byte, member.end_byte))
            member_words = wrapper_java.modifier_words(member)
            if member.child_by_field_name("body") is None and not member_words & {"default", "static"}:
                abstract += 1
    return TypeDecl(
        path=path,
        package=package,
        name=_text(name),
        kind=kind,
        functional=kind == "interface" and abstract == 1,
        top_level=node.parent is not None and node.parent.type == "program",
        # A member of an interface is implicitly public, nested types included.
        public=bool(words & {"public", "protected"})
        or (node.parent is not None and node.parent.type == "interface_body"),
        open="abstract" in words,
        supertypes=tuple(_java_supertypes(node)),
        members=members,
    )


def _kotlin_type(node: Node, path: str, package: str) -> TypeDecl | None:
    if node.type not in ("class_declaration", "object_declaration"):
        return None
    name = node.child_by_field_name("name")
    if name is None:
        return None
    words = wrapper_kotlin.modifier_words(node)
    if node.type == "object_declaration":
        kind = "object"
    elif wrapper_kotlin.is_interface(node):
        kind = "interface"
    elif "enum" in words:
        kind = "enum"
    else:
        kind = "class"
    body = next((c for c in node.named_children if c.type in ("class_body", "enum_class_body")), None)
    members: dict[str, tuple[int, int]] = {}
    properties: dict[str, tuple[int, int]] = {}
    abstract = 0
    for member in body.named_children if body is not None else ():
        if member.type == "function_declaration":
            member_name = member.child_by_field_name("name")
            if member_name is not None:
                members.setdefault(_text(member_name), (member.start_byte, member.end_byte))
            if not any(c.type == "function_body" for c in member.named_children):
                abstract += 1
        elif member.type == "property_declaration":
            prop_name = _property_name(member)
            if prop_name is not None:
                properties.setdefault(prop_name, (member.start_byte, member.end_byte))
            if not any(c.type in ("getter", "property_delegate") for c in member.named_children):
                abstract += 1
    fun_interface = any(c.type == "fun" for c in node.children if not c.is_named)
    return TypeDecl(
        path=path,
        package=package,
        name=_text(name),
        kind=kind,
        top_level=node.parent is not None and node.parent.type == "source_file",
        public=not words & {"internal", "private", "protected"},
        open=bool(words & {"open", "abstract", "sealed"}),
        supertypes=tuple(_kotlin_supertypes(node)),
        members=members,
        properties=properties,
        functional=kind == "interface" and (fun_interface or abstract == 1),
    )
