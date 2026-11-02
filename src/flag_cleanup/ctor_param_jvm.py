"""Prove a Java or Kotlin private member, or Kotlin constructor parameter, the
fold left unread can be removed.

:mod:`flag_cleanup.jvm_unread_members` names three things: a Java ``private``
field, a Kotlin ``private`` property (of the class body, or a ``private val``
of the primary constructor), and a plain Kotlin primary-constructor parameter.
:func:`plan` proves one can go and marks every span to delete; the engine
deletes them (``rules/java_ctor_param.toml``, ``rules/kt_ctor_param.toml``).

**A member that leaves every constructor as it is** needs only its own file:

- Java: a field whose initializer is absent or inert (a literal, a name, a
  field access of a name) and that nothing writes, or that only statements of
  the class's one constructor write, each ``this.f = <parameter or literal>;``
  directly in its body. The statement goes with the field.
- Kotlin: a body property with an inert initializer (a literal, or a plain
  constructor parameter) that nothing writes. Not ``lateinit``, not
  delegated (``by``), no accessor.

Neither may carry an annotation, and the name may not be spelled as a string
anywhere in the repository's Java and Kotlin (``getDeclaredField("flags")``,
``ReflectionTestUtils.setField(svc, "flags", …)``). A file that imports Lombok
refuses: its annotations generate constructors and accessors this cannot see.

**A member that takes a constructor parameter with it** (a Java field whose
assignment was its parameter's last read, a Kotlin ``private val`` parameter,
a plain Kotlin parameter) changes the constructor every caller calls, so every
construction of the class must be proven inside the repository. Java and
Kotlin types are nominal, but the proof is still by NAME, as in TypeScript:

- at least one construction exists: a class that takes arguments and that
  nothing here builds is built by something else (a deserializer, a
  container, reflection);
- every call is in the class's own package, or in a file importing it, and
  in no file importing another class of that name (``java.util.Timer``);
- the class name is declared once, as this class, across every Java and
  Kotlin file of the checkout (generated and ignored ones included), and no
  other file of the checkout spells it as a word (an XML bean definition, a
  ``META-INF/services`` entry, a Groovy or Scala caller);
- every spelling of it in Java and Kotlin is the declaration, an import, a
  plain type position, a static member access, or a call: Java
  ``new Svc(…)`` without a class body, Kotlin ``Svc(…)`` without a trailing
  lambda or spread, neither inside a function carrying an annotation (a
  Spring ``@Bean``, a Dagger ``@Provides``). Refused: a method or callable
  reference (``Svc::new``, ``::Svc``), a class literal (``Svc.class``,
  ``Svc::class``), a supertype, a type argument of a call (``get<Svc>()``,
  ``readValue<Svc>(…)``), the type of an annotated declaration
  (``@InjectMocks Svc svc``), a string spelling the name, and anything else;
- each call's argument for the parameter is inert. A Kotlin name must be a
  local or parameter: any other may be a getter or an ``object``.

Refused on the declaring side: any class annotation (a Spring stereotype, a
Lombok one, ``@Serializable``, ``@JsonCreator``...) besides ``@Deprecated``
and ``@Suppress``/``@SuppressWarnings``; an annotated constructor or
parameter (``@Inject``, ``@JvmOverloads``, ``@Autowired``); a Java class that
is abstract, an enum, a record, or has more than one constructor; a Kotlin
class that is ``open``, ``abstract``, ``sealed``, ``data`` (its ``componentN``
functions and ``copy`` follow the parameter list), ``enum``, ``value``,
``inline``, ``annotation``, ``expect``/``actual`` or has a secondary
constructor; any ``vararg`` parameter (positions after it bind differently); a
default value that may do work; a documentation comment naming the
parameter (``@param flags``), which ``javadoc`` rejects once it is gone, or a
Java field (``{@link #RETRIES}``).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import jvm_unread_members as jvm
from flag_cleanup import source_index
from flag_cleanup.ctor_param_model import IDENT, Refusal, Removal, Site
from flag_cleanup.unused import _parse

_SOURCE_SUFFIXES = (".java", ".kt", ".kts")
#: Prose a class name may appear in without constructing anything.
_DOC_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc", ".html", ".htm"})
#: The biggest non-source file read for a spelling of the class name.
_MAX_TEXT = 2_000_000
#: Class annotations that say nothing about how the class is built.
_HARMLESS_ANNOTATIONS = frozenset({"Deprecated", "Suppress", "SuppressWarnings"})
_REFUSED_KOTLIN_CLASS_MODIFIERS = frozenset(
    {
        "open", "abstract", "sealed", "data", "enum", "value", "inline",
        "annotation", "expect", "actual", "external",
    }
)
_JAVA_LITERALS = frozenset(
    {
        "decimal_integer_literal", "hex_integer_literal", "octal_integer_literal",
        "binary_integer_literal", "decimal_floating_point_literal",
        "hex_floating_point_literal", "string_literal", "character_literal",
        "true", "false", "null_literal",
    }
)
_KOTLIN_LITERALS = frozenset(
    {
        "number_literal", "integer_literal", "long_literal", "hex_literal",
        "bin_literal", "unsigned_literal", "real_literal", "float_literal",
        "boolean_literal", "character_literal", "null", "null_literal",
    }
)
#: The nodes holding a string literal's own text, in both grammars.
_STRING_TEXT = frozenset({"string_fragment", "multiline_string_fragment", "string_content", "text_block"})
_TYPE_DECLARATIONS = frozenset(
    {
        # Java
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "annotation_type_declaration",
        # Kotlin
        "object_declaration", "type_alias",
    }
)


def _walk(node: Node) -> Iterator[Node]:
    return jvm.walk(node)


def _text(node: Node) -> str:
    return jvm.text(node)


def grammar_of(path: str) -> str:
    return "java" if path.endswith(".java") else "kt"


# --- inert expressions ------------------------------------------------------------


def _java_inert(node: Node) -> bool:
    if node.type in _JAVA_LITERALS or node.type in ("identifier", "this"):
        return True
    if node.type == "parenthesized_expression":
        return len(node.named_children) == 1 and _java_inert(node.named_children[0])
    if node.type == "field_access":
        obj = node.child_by_field_name("object")
        return obj is not None and _java_inert(obj)
    if node.type == "unary_expression":
        operand = node.child_by_field_name("operand")
        return _text(node).startswith("-") and operand is not None and operand.type in _JAVA_LITERALS
    return False


def _kotlin_inert(node: Node, local_name_ok: bool = True) -> bool:
    """A literal, a string without a template, or (``local_name_ok``) a name
    a parameter or local of an enclosing function declares."""
    if node.type in _KOTLIN_LITERALS:
        return True
    if node.type == "string_literal":
        return not any(c.type == "interpolation" for c in node.named_children) and "$" not in _text(node)
    if node.type == "parenthesized_expression":
        return len(node.named_children) == 1 and _kotlin_inert(node.named_children[0], local_name_ok)
    if node.type == "prefix_expression":
        return _text(node).startswith("-") and len(node.named_children) == 1 and (
            node.named_children[0].type in _KOTLIN_LITERALS
        )
    if node.type == "identifier":
        return local_name_ok and jvm.shadowed(node, "kt")
    return False


# --- the class --------------------------------------------------------------------


def _find_class(root: Node, grammar: str, class_name: str) -> list[Node]:
    return [c for c in jvm.classes(root, grammar) if jvm.class_name(c, grammar) == class_name]


def _class_annotations(declaration: Node, grammar: str) -> list[str]:
    if grammar == "java":
        return jvm.java_annotations(jvm.java_modifiers(declaration))
    return jvm.kotlin_annotations(jvm.child_of_type(declaration, "modifiers"))


def _imports_lombok(root: Node) -> bool:
    return any(
        n.type in ("import_declaration", "import") and _text(n).replace(" ", "").startswith(("importlombok", "importstaticlombok"))
        for n in root.named_children
    )


def _string_spells(repo_dir: str, name: str) -> str | None:
    """A Java or Kotlin file of the checkout with a string literal spelling
    ``name`` exactly, or ``None``."""
    quoted = (f'"{name}"').encode()
    for source in source_index.files(repo_dir, _SOURCE_SUFFIXES, with_ignored=True):
        try:
            if quoted in source.path.read_bytes():
                return str(source.path)
        except OSError:
            return str(source.path)
    return None


# --- every construction ---------------------------------------------------------------


def _annotated(node: Node, grammar: str) -> bool:
    """Whether the declaration ``node`` carries an annotation."""
    modifiers = jvm.java_modifiers(node) if grammar == "java" else jvm.child_of_type(node, "modifiers")
    if modifiers is None:
        return False
    if grammar == "java":
        return bool(jvm.java_annotations(modifiers))
    return bool(jvm.kotlin_annotations(modifiers))


_FUNCTIONS = frozenset(
    {"method_declaration", "constructor_declaration", "function_declaration", "secondary_constructor"}
)
_ANNOTATABLE = frozenset(
    {
        # Java
        "field_declaration", "local_variable_declaration", "formal_parameter",
        "method_declaration", "constructor_declaration",
        # Kotlin
        "property_declaration", "parameter", "class_parameter", "function_declaration",
    }
)


def _inside_annotated_function(node: Node, grammar: str) -> bool:
    scope = node.parent
    while scope is not None:
        if scope.type in _FUNCTIONS and _annotated(scope, grammar):
            return True
        scope = scope.parent
    return False


def _in_annotated_declaration(node: Node, grammar: str) -> bool:
    """Whether ``node`` sits in the type of an annotated declaration: the
    nearest declaration above it carries an annotation."""
    scope = node.parent
    while scope is not None:
        if scope.type in _ANNOTATABLE:
            if scope.type in ("method_declaration", "function_declaration"):
                # Only the return type belongs to the declaration itself.
                body = scope.child_by_field_name("body") or jvm.child_of_type(scope, "function_body")
                if body is not None and body.start_byte <= node.start_byte:
                    return False
            return _annotated(scope, grammar)
        if scope.type in ("block", "class_body", "constructor_body", "statements", "function_body"):
            return False
        scope = scope.parent
    return False


def _java_type_position(node: Node) -> bool:
    """Whether the ``type_identifier`` ``node`` is a plain type, not a
    supertype, a class literal or a construction."""
    parent = node.parent
    while parent is not None and parent.type in (
        "generic_type", "scoped_type_identifier", "array_type", "type_arguments", "wildcard",
        "type_bound", "annotated_type",
    ):
        if parent.type == "type_arguments" and parent.parent is not None and parent.parent.type in (
            "method_invocation", "explicit_constructor_invocation", "object_creation_expression"
        ):
            return False  # `<Svc>get()`, a type argument of a call
        node, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type in ("superclass", "super_interfaces", "type_list", "extends_interfaces", "permits"):
        return False
    if parent.type in ("class_literal", "method_reference", "object_creation_expression"):
        return False
    return parent.type in (
        "local_variable_declaration", "field_declaration", "formal_parameter", "spread_parameter",
        "method_declaration", "cast_expression", "instanceof_expression", "catch_type",
        "record_pattern", "type_pattern", "array_creation_expression", "throws",
        "enhanced_for_statement", "resource", "constant_declaration", "annotation_type_element_declaration",
        "lambda_expression", "inferred_parameters",
    )


def _java_construction(node: Node) -> Node | None:
    """The ``new Svc(…)`` whose class is the ``type_identifier`` ``node``."""
    target = node
    if target.parent is not None and target.parent.type == "generic_type":
        target = target.parent
    creation = target.parent
    if (
        creation is not None
        and creation.type == "object_creation_expression"
        and creation.child_by_field_name("type") == target
    ):
        return creation
    return None


def _kotlin_construction(node: Node) -> Node | None:
    """The ``Svc(…)`` whose callee is the ``identifier`` ``node``."""
    call = node.parent
    if call is not None and call.type == "call_expression" and call.named_children and call.named_children[0] == node:
        return call
    return None


def _kotlin_type_position(node: Node) -> bool:
    """Whether the ``identifier`` ``node`` is the only part of a plain
    ``user_type`` that is not a supertype, a receiver or a call's type argument."""
    user_type = node.parent
    if user_type is None or user_type.type != "user_type":
        return False
    if [c for c in user_type.named_children if c.type == "identifier"] != [node]:
        return False
    child, parent = user_type, user_type.parent
    while parent is not None and parent.type in (
        "nullable_type", "type_projection", "type_arguments", "parenthesized_type", "non_nullable_type",
        "type_projection_modifiers",
    ):
        if parent.type == "type_arguments" and parent.parent is not None and parent.parent.type in (
            "call_expression", "call_suffix", "navigation_expression", "constructor_invocation",
        ):
            return False  # `get<Svc>()`
        child, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type in ("delegation_specifier", "constructor_invocation", "delegation_specifiers"):
        return False
    if parent.type == "function_declaration" and parent.child_by_field_name("name") is not None:
        # `fun Svc.ext()`: a receiver. A return type is allowed.
        name = parent.child_by_field_name("name")
        if child.end_byte <= name.start_byte:
            return False
    return parent.type in (
        "property_declaration", "variable_declaration", "parameter", "class_parameter",
        "function_declaration", "as_expression", "is_expression", "type_test",
        "binary_expression", "catch_block", "function_type", "function_type_parameters",
        "parameter_with_optional_type", "getter", "setter", "user_type",
        "check_expression", "lambda_parameters",
    )


def _import_of(node: Node) -> Node:
    scope = node
    while scope.parent is not None and scope.type not in ("import", "package_header"):
        scope = scope.parent
    return scope


def _kotlin_import(node: Node) -> bool:
    scope = node.parent
    while scope is not None:
        if scope.type in ("import", "package_header"):
            return True
        scope = scope.parent
    return False


def _java_import(node: Node) -> bool:
    scope = node.parent
    while scope is not None:
        if scope.type in ("import_declaration", "package_declaration"):
            return True
        scope = scope.parent
    return False


def _java_static_access(node: Node) -> bool:
    """``Svc.CONSTANT`` or ``Svc.helper()``: the identifier is the object."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "field_access" and parent.child_by_field_name("object") == node:
        field = parent.child_by_field_name("field")
        return field is not None and _text(field) != "class"
    if parent.type == "method_invocation" and parent.child_by_field_name("object") == node:
        return True
    return parent.type == "scoped_identifier"  # `Svc.Inner` in an expression


def _kotlin_static_access(node: Node) -> bool:
    """``Svc.CONSTANT``: the identifier heads a navigation that is not ``::``."""
    parent = node.parent
    return (
        parent is not None
        and parent.type == "navigation_expression"
        and parent.named_children
        and parent.named_children[0] == node
        and "::" not in _text(parent)[len(_text(node)) : len(_text(node)) + 3]
    )


def _declared_names(root: Node, grammar: str) -> Iterator[Node]:
    """The name nodes of every type, function, property or alias ``root``
    declares (Kotlin top-level and member functions included)."""
    for node in _walk(root):
        if node.type in _TYPE_DECLARATIONS or (grammar == "kt" and node.type == "class_declaration"):
            name = node.child_by_field_name("name") or jvm.child_of_type(node, "identifier")
            if name is not None:
                yield name
        elif grammar == "java" and node.type == "class_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                yield name
        elif grammar == "kt" and node.type == "function_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                yield name


def _root_of(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


def _names_in_scope(root: Node, grammar: str) -> tuple[str, dict[str, str], set[str]]:
    """``(package, {simple name: imported fully qualified name}, wildcard
    packages)`` of a file. Static imports are left out: they bring members."""
    package = ""
    explicit: dict[str, str] = {}
    wildcards: set[str] = set()
    for node in root.named_children:
        if node.type in ("package_declaration", "package_header"):
            path = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier", "qualified_identifier")), None)
            package = _text(path) if path is not None else ""
        elif node.type in ("import_declaration", "import"):
            if any(c.type == "static" for c in node.children):
                continue
            path = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier", "qualified_identifier")), None)
            if path is None:
                continue
            dotted = "".join(_text(path).split())
            if any(c.type in ("asterisk", "*") for c in node.children) or _text(node).rstrip("; \n").endswith("*"):
                wildcards.add(dotted)
            else:
                explicit[dotted.rsplit(".", 1)[-1]] = dotted
    return package, explicit, wildcards


def _qualified_name(root: Node, declaration: Node, grammar: str) -> tuple[str, bool]:
    """``(fully qualified name, top level)`` of ``declaration``."""
    package, _, _ = _names_in_scope(root, grammar)
    parts = [jvm.class_name(declaration, grammar) or ""]
    scope = declaration.parent
    top = True
    while scope is not None:
        if scope.type in ("class_declaration", "object_declaration", "enum_declaration",
                          "interface_declaration", "record_declaration", "companion_object"):
            top = False
            parts.append(jvm.class_name(scope, grammar) or "")
        scope = scope.parent
    return ".".join(([package] if package else []) + list(reversed(parts))), top


def _resolves_here(root: Node, grammar: str, class_name: str, fqn: str, top: bool) -> bool:
    """Whether a bare ``class_name`` in the file ``root`` names the class
    ``fqn``: imported by that name, or (a top-level class) in its package or
    imported with its package's wildcard, and not imported from elsewhere."""
    package, explicit, wildcards = _names_in_scope(root, grammar)
    if class_name in explicit:
        return explicit[class_name] == fqn
    if not top:
        return False
    owner = fqn.rsplit(".", 1)[0] if "." in fqn else ""
    return package == owner or owner in wildcards


def constructions(
    declaring_path: str, declaration: Node, class_name: str, repo_dir: str
) -> list[tuple[str, str, Node, int]] | str:
    """Every ``(path, grammar, call)`` constructing ``class_name`` in the
    checkout, or why not every construction can be proven."""
    here = str(Path(declaring_path).resolve())
    fqn, top = _qualified_name(_root_of(declaration), declaration, grammar_of(declaring_path))
    declared = declaration.child_by_field_name("name")
    own_span = (declared.start_byte, declared.end_byte) if declared is not None else None
    spelled = class_name.encode()
    word = re.compile(rb"(?<![A-Za-z0-9_$])" + re.escape(spelled) + rb"(?![A-Za-z0-9_])")
    sites: list[tuple[str, str, Node, int]] = []
    sources = source_index.files(repo_dir, _SOURCE_SUFFIXES, with_ignored=True)
    for source in sources:
        path = str(source.path)
        try:
            data = source.path.read_bytes()
        except OSError:
            return f"{path} could not be read"
        # The plain search first: the pattern's lookbehind costs ten times
        # as much, on every Java and Kotlin file of the checkout.
        if spelled not in data or not word.search(data):
            continue
        grammar = grammar_of(path)
        offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        text = data[offset:]
        root = _parse(text.decode("utf-8", "replace"), grammar)
        if root.has_error:
            return f"{path} does not parse"
        own = str(source.path.resolve()) == here
        for name in _declared_names(root, grammar):
            if _text(name) == class_name and not (own and (name.start_byte, name.end_byte) == own_span):
                return f"{path} declares another `{class_name}`"
        for node in _walk(root):
            if node.type in _STRING_TEXT:
                if word.search(node.text):
                    return f"{path} spells `{class_name}` in a string"
                continue
            if node.type not in ("identifier", "type_identifier") or _text(node) != class_name:
                continue
            reason = _classify(node, grammar, own_span if own else None)
            if isinstance(reason, str):
                return f"{path}: {reason}"
            if reason is not None:
                if not own and not _resolves_here(root, grammar, class_name, fqn, top):
                    return f"{path} constructs a `{class_name}` that may be another class"
                if source.ignored:
                    return f"{path} constructs it, and is ignored by git (generated)"
                if grammar != grammar_of(declaring_path):
                    return f"{path} constructs it from another language"
                sites.append((path, grammar, reason, offset))
    other = _other_files(repo_dir, class_name, word)
    if other is not None:
        return other
    if not sites:
        # Nothing here builds it, yet it takes arguments: something outside
        # the code does (a deserializer, a container, reflection).
        return "nothing in the repository constructs it"
    return sites


def _classify(node: Node, grammar: str, own_span: tuple[int, int] | None) -> Node | str | None:
    """A call constructing the class at ``node``, ``None`` for a spelling
    that cannot construct it, or why it may. ``own_span`` is the class's own
    name in this file, if it is declared here."""
    if own_span is not None and (node.start_byte, node.end_byte) == own_span:
        return None
    if grammar == "java":
        if _java_import(node):
            return None
        parent = node.parent
        if parent is not None and parent.type == "constructor_declaration" and parent.child_by_field_name("name") == node:
            return None
        creation = _java_construction(node) if node.type == "type_identifier" else None
        if creation is not None:
            if jvm.child_of_type(creation, "class_body") is not None:
                return "an anonymous subclass"
            if _inside_annotated_function(creation, grammar):
                return "a construction inside an annotated method (a framework factory)"
            return creation
        if node.type == "type_identifier" and _java_type_position(node):
            if _in_annotated_declaration(node, grammar):
                return "the type of an annotated declaration"
            return None
        if node.type == "identifier" and _java_static_access(node):
            return None
        return f"`{_text(node.parent or node)[:60]}` may construct it"
    if _kotlin_import(node):
        if any(c.type == "as" for c in _import_of(node).children):
            return "an import under another name"
        return None
    call = _kotlin_construction(node)
    if call is not None:
        if _inside_annotated_function(call, grammar):
            return "a construction inside an annotated function (a framework factory)"
        return call
    if _kotlin_type_position(node):
        if _in_annotated_declaration(node, grammar):
            return "the type of an annotated declaration"
        return None
    if _kotlin_static_access(node):
        return None
    return f"`{' '.join(_text(node.parent or node).split())[:60]}` may construct it"


def _other_files(repo_dir: str, class_name: str, word: re.Pattern[bytes]) -> str | None:
    """Why a file of the checkout that is neither Java, Kotlin nor prose may
    spell the class name as a word, or ``None`` when none does.

    Read: every file git tracks or leaves untracked without ignoring it. A
    file nothing can read refuses: it cannot be shown not to spell the name.
    A tracked file gone from the working tree spells nothing, as does a
    symlink, a directory (a submodule), a file over :data:`_MAX_TEXT` and a
    binary one.

    On a large checkout most of those files never mention the class, so one
    ``git grep`` for the bare name picks the files worth reading first
    (:func:`_grep_hits`). It only narrows: each file it names is still held
    to the same checks, and when git cannot vouch for its answer every listed
    file is read.
    """
    root = source_index.checkout_root(repo_dir)
    try:
        tracked = _tagged_files(root)
        untracked = source_index._ls_files(root, "--others", "--exclude-standard")
    except Exception:  # noqa: BLE001 - not listable: cannot prove the absence
        return f"{root} could not be listed"
    listed = dict.fromkeys([*tracked, *untracked])
    hits = _grep_hits(root, class_name)
    if hits is not None:
        # Git searches a file it does not read from the working tree (skip-
        # worktree, assume-unchanged, unmerged) in the index, or not at all,
        # depending on its version: those are read here whatever it said.
        listed = {rel: None for rel in listed if rel in hits or tracked.get(rel, "H") != "H"}
    for rel in listed:
        path = Path(root) / rel
        suffix = path.suffix.lower()
        if suffix in _SOURCE_SUFFIXES or suffix in _DOC_SUFFIXES:
            continue
        try:
            mode = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            continue  # tracked, but no longer in the working tree
        except OSError:
            return f"{rel} could not be read"
        if not stat.S_ISREG(mode.st_mode) or mode.st_size > _MAX_TEXT:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            return f"{rel} could not be read"
        if b"\0" in data[:4096]:
            continue  # binary
        if word.search(data):
            return f"{rel} spells `{class_name}`"
    return None


def _tagged_files(root: str) -> dict[str, str]:
    """Every file git tracks under ``root``, with its ``ls-files -v`` tag:
    ``H`` for a file git reads from the working tree, anything else for one
    it may not."""
    tagged: dict[str, str] = {}
    for entry in source_index._ls_files(root, "--cached", "-v"):
        tag, _, rel = entry.partition(" ")
        if tagged.get(rel, "H") == "H":
            tagged[rel] = tag
    return tagged


def _grep_hits(root: str, class_name: str) -> frozenset[str] | None:
    """The files under ``root`` that ``git grep`` finds ``class_name`` in, as
    plain bytes, among those git tracks or leaves unignored; ``None`` when its
    answer cannot be trusted.

    ``git grep`` reports a file it cannot read on its stderr and still exits
    0 or 1, so anything on stderr is distrusted along with any other status.
    """
    try:
        result = subprocess.run(
            ["git", "grep", "--untracked", "--no-color", "--no-textconv", "-l", "-z", "-F", "-e", class_name],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode not in (0, 1) or result.stderr.strip():
        return None
    return frozenset(os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry)


# --- the constructor ------------------------------------------------------------------


def _documents(declaration: Node, name: str) -> bool:
    """Whether a documentation comment just above ``declaration`` names
    ``name`` with ``@param`` or ``@property``."""
    sibling = declaration.prev_named_sibling
    pattern = re.compile(r"@(?:param|property)\s+" + re.escape(name) + r"(?![A-Za-z0-9_])")
    while sibling is not None and sibling.type in ("block_comment", "line_comment", "multiline_comment", "comment"):
        if pattern.search(_text(sibling)):
            return True
        sibling = sibling.prev_named_sibling
    return False


def _java_argument_sites(calls: list[tuple[str, str, Node, int]], index: int, label: str) -> list[Site] | str:
    sites: list[Site] = []
    for path, grammar, call, offset in calls:
        if grammar == "java":
            arguments = call.child_by_field_name("arguments")
            items = [c for c in arguments.named_children if c.type not in ("line_comment", "block_comment")] if arguments else []
            if index >= len(items):
                return f"a call in {path} passes {len(items)} argument(s)"
            argument = items[index]
            if not _java_inert(argument):
                return f"an argument that may do work ({' '.join(_text(argument).split())[:60]})"
            sites.append(Site(path, argument.start_byte + offset, argument.end_byte + offset))
        else:
            site = _kotlin_site(path, call, index, None, offset)
            if isinstance(site, str):
                return site
            if site is not None:
                sites.append(site)
    return sites


def _kotlin_site(path: str, call: Node, index: int, name: str | None, offset: int = 0) -> Site | str | None:
    """The argument of the Kotlin call ``call`` bound to parameter ``index``
    (named ``name`` when Kotlin can bind it by name), ``None`` when the call
    leaves it to its default, or why it cannot be marked."""
    arguments = jvm.child_of_type(call, "value_arguments")
    if any(c.type == "annotated_lambda" for c in call.named_children):
        return f"a call in {path} passes a trailing lambda"
    items = [c for c in arguments.named_children if c.type == "value_argument"] if arguments else []
    named_seen = False
    found: Site | None = None
    for position, item in enumerate(items):
        if any(c.type == "*" for c in item.children) or _text(item).lstrip().startswith("*"):
            return f"a call in {path} spreads its arguments"
        label = item.named_children[0] if len(item.named_children) >= 2 and any(c.type == "=" for c in item.children) else None
        if label is not None:
            named_seen = True
            if name is None:
                return f"a call in {path} names an argument of a Java constructor"
            if _text(label) != name:
                continue
            value = item.named_children[-1]
        else:
            if named_seen:
                return f"a call in {path} passes a positional argument after a named one"
            if position != index:
                continue
            value = item.named_children[-1] if item.named_children else None
        if value is None:
            return f"a call in {path} has an empty argument"
        if not _kotlin_inert(value):
            return f"an argument that may do work ({' '.join(_text(value).split())[:60]})"
        if found is not None:
            return f"a call in {path} passes the parameter twice"
        found = Site(path, item.start_byte + offset, item.end_byte + offset)
    return found


def _kotlin_argument_sites(
    calls: list[tuple[str, str, Node, int]], index: int, name: str
) -> list[Site] | str:
    sites: list[Site] = []
    for path, grammar, call, offset in calls:
        if grammar == "java":
            arguments = call.child_by_field_name("arguments")
            items = [c for c in arguments.named_children if c.type not in ("line_comment", "block_comment")] if arguments else []
            if index >= len(items):
                return f"a call in {path} relies on a default value Java cannot see"
            argument = items[index]
            if not _java_inert(argument):
                return f"an argument that may do work ({' '.join(_text(argument).split())[:60]})"
            sites.append(Site(path, argument.start_byte + offset, argument.end_byte + offset))
            continue
        site = _kotlin_site(path, call, index, name, offset)
        if isinstance(site, str):
            return site
        if site is not None:
            sites.append(site)
    return sites


def _exported_java(declaration: Node, constructor: Node | None) -> bool:
    if not jvm.java_has_modifier(declaration, "public"):
        return False
    return constructor is None or jvm.java_has_modifier(constructor, "public") or jvm.java_has_modifier(
        constructor, "protected"
    )


def _exported_kotlin(declaration: Node) -> bool:
    words = jvm.kotlin_modifier_words(declaration)
    if words & {"private", "internal"}:
        return False
    constructor = jvm.child_of_type(declaration, "primary_constructor")
    if constructor is not None and jvm.kotlin_modifier_words(constructor) & {"private", "internal"}:
        return False
    return True


def _class_refusal(root: Node, declaration: Node, grammar: str) -> str | None:
    """Why the class's constructor cannot change, its callers aside."""
    if set(_class_annotations(declaration, grammar)) - _HARMLESS_ANNOTATIONS:
        return "an annotated class (a framework may construct it)"
    if _imports_lombok(root):
        return "its file uses Lombok"
    if grammar == "java":
        if declaration.type != "class_declaration":
            return "an enum"
        if jvm.java_has_modifier(declaration, "abstract"):
            return "an abstract class"
        return None
    if declaration.type != "class_declaration" or jvm.is_kotlin_interface(declaration):
        return "not a class"
    refused = jvm.kotlin_modifier_words(declaration) & _REFUSED_KOTLIN_CLASS_MODIFIERS
    if refused:
        return f"a `{sorted(refused)[0]}` class"
    if any(c.type == "enum_class_body" for c in declaration.named_children):
        return "an enum"
    body = jvm.child_of_type(declaration, "class_body")
    if body is not None and any(m.type == "secondary_constructor" for m in body.named_children):
        return "a class with a secondary constructor"
    constructor = jvm.child_of_type(declaration, "primary_constructor")
    if constructor is not None and _annotated(constructor, "kt"):
        return "an annotated constructor"
    if any(
        n.type == "function_declaration" and _text(n.child_by_field_name("name") or n) == "invoke"
        for n in _walk(declaration)
    ):
        return "an `invoke` operator a call of the class name may reach"
    return None


# --- Java ---------------------------------------------------------------------------------


def _java_constructors(declaration: Node) -> list[Node]:
    body = declaration.child_by_field_name("body")
    return [m for m in body.named_children if m.type == "constructor_declaration"] if body is not None else []


def _member_span(member: Node, data: bytes) -> tuple[int, int]:
    """``member``'s bytes with a documentation comment directly above it."""
    start = member.start_byte
    previous = member.prev_sibling
    if (
        previous is not None
        and previous.type in ("block_comment", "multiline_comment")
        and _text(previous).startswith("/**")
        and previous.end_point[0] == member.start_point[0] - 1
        and (previous.prev_sibling is None or previous.prev_sibling.end_point[0] < previous.start_point[0])
    ):
        start = previous.start_byte
    return start, member.end_byte


def _java_writes(root: Node, field: str, declared: Node) -> list[Node] | str:
    """Every node writing the field; a reason when one may read it."""
    writes: list[Node] = []
    for node in _walk(root):
        if node.type != "identifier" or _text(node) != field or node == declared:
            continue
        if jvm._declares(node, "java"):
            continue
        bare = jvm._java_bare(node)
        if bare and jvm.shadowed(node, "java"):
            continue
        if jvm._java_written(node):
            writes.append(node)
            continue
        if _java_import(node):
            continue
        return "its name is still spelled where it may be read"
    return writes


def _plan_java_field(path: str, root: Node, data: bytes, label: str, repo_dir: str) -> Removal | Refusal:
    class_name, _, field = label.partition(".")
    declarations = _find_class(root, "java", class_name)
    if len(declarations) != 1:
        return Refusal(label, "the class is declared more than once in its file")
    declaration = declarations[0]
    found = [(m, n) for m, n in jvm.java_fields(declaration) if _text(n) == field]
    if len(found) != 1:
        return Refusal(label, "declared more than once")
    member, name = found[0]
    declarators = [d for d in member.named_children if d.type == "variable_declarator"]
    if len(declarators) != 1:
        return Refusal(label, "declared beside other fields in one declaration")
    if jvm.java_annotations(jvm.java_modifiers(member)):
        return Refusal(label, "an annotated field")
    if _imports_lombok(root):
        return Refusal(label, "its file uses Lombok")
    if declaration.type != "class_declaration" and declaration.type != "enum_declaration":
        return Refusal(label, "not a class")
    value = declarators[0].child_by_field_name("value")
    if value is not None and not _java_inert(value):
        return Refusal(label, f"an initializer that may do work ({' '.join(_text(value).split())[:60]})")
    writes = _java_writes(root, field, name)
    if isinstance(writes, str):
        return Refusal(label, writes)
    spelled = _string_spells(repo_dir, field)
    if spelled is not None:
        return Refusal(label, f"{spelled} spells its name in a string (reflection may read it)")
    if re.search(rb"#" + re.escape(field.encode()) + rb"(?![A-Za-z0-9_$])", data):
        return Refusal(label, "a documentation comment links to it (`#" + field + "`)")
    sites = [Site(path, *_member_span(member, data), "member")]
    if not writes:
        if value is None and jvm.java_has_modifier(member, "final"):
            return Refusal(label, "a final field nothing visible assigns")
        return Removal(path, class_name, None, label, -1, tuple(sites), False, field)
    constructors = _java_constructors(declaration)
    if len(writes) != 1:
        return Refusal(label, "assigned more than once")
    write = writes[0]
    target = write if write.parent.type != "field_access" else write.parent
    assignment = target.parent
    statement = assignment.parent
    constructor = statement.parent.parent if statement.parent is not None else None
    if (
        statement.type != "expression_statement"
        or statement.parent is None
        or statement.parent.type != "constructor_body"
        or constructor is None
        or constructor not in constructors
    ):
        return Refusal(label, "assigned outside a constructor's own statements")
    right = assignment.child_by_field_name("right")
    sites.append(Site(path, statement.start_byte, statement.end_byte, "statement"))
    if right is not None and right.type in _JAVA_LITERALS:
        return Removal(path, class_name, None, label, -1, tuple(sites), False, field)
    parameters = constructor.child_by_field_name("parameters")
    formal = [p for p in parameters.named_children if p.type in ("formal_parameter", "spread_parameter", "receiver_parameter")]
    names = [p.child_by_field_name("name") for p in formal]
    if right is None or right.type != "identifier" or not any(n is not None and n.text == right.text for n in names):
        return Refusal(label, f"assigned from something that may do work ({' '.join(_text(right or assignment).split())[:60]})")
    index = next(i for i, n in enumerate(names) if n is not None and n.text == right.text)
    parameter = formal[index]
    still = [
        n
        for n in _walk(constructor)
        if n.type == "identifier"
        and n.text == right.text
        and n != names[index]
        and not (statement.start_byte <= n.start_byte < statement.end_byte)
        and jvm._java_bare(n)
    ]
    if still:
        return Removal(path, class_name, None, label, -1, tuple(sites), False, field)
    reason = _java_constructor_refusal(root, declaration, constructors, parameter, _text(right))
    if reason is not None:
        return Refusal(label, f"its parameter `{_text(right)}` cannot go: {reason}")
    calls = constructions(path, declaration, class_name, repo_dir)
    if isinstance(calls, str):
        return Refusal(label, f"its parameter `{_text(right)}` cannot go: {calls}")
    arguments = _java_argument_sites(calls, index, label)
    if isinstance(arguments, str):
        return Refusal(label, f"its parameter `{_text(right)}` cannot go: {arguments}")
    sites.append(Site(path, parameter.start_byte, parameter.end_byte, "parameter"))
    sites += arguments
    return Removal(
        path, class_name, _text(right), label, index, tuple(sites),
        _exported_java(declaration, constructor), field,
    )


def _java_constructor_refusal(
    root: Node, declaration: Node, constructors: list[Node], parameter: Node, name: str
) -> str | None:
    reason = _class_refusal(root, declaration, "java")
    if reason is not None:
        return reason
    if len(constructors) != 1:
        return "the class has more than one constructor"
    constructor = constructors[0]
    if _annotated(constructor, "java") or any(
        _annotated(p, "java") for p in constructor.child_by_field_name("parameters").named_children
    ):
        return "an annotated constructor or parameter (dependency injection)"
    if parameter.type != "formal_parameter":
        return "a variable-arity parameter"
    if _documents(constructor, name):
        return "its documentation comment names it"
    return None


# --- Kotlin ---------------------------------------------------------------------------------


def _kotlin_parameters(declaration: Node) -> list[Node]:
    constructor = jvm.child_of_type(declaration, "primary_constructor")
    parameters = jvm.child_of_type(constructor, "class_parameters") if constructor is not None else None
    return [p for p in parameters.named_children if p.type == "class_parameter"] if parameters is not None else []


def _plan_kotlin_parameter(
    path: str, root: Node, declaration: Node, class_name: str, parameter: Node, label: str, repo_dir: str
) -> Removal | Refusal:
    name_node = jvm.child_of_type(parameter, "identifier")
    name = _text(name_node)
    if _annotated(parameter, "kt"):
        return Refusal(label, "an annotated parameter")
    parameters = _kotlin_parameters(declaration)
    if any("vararg" in jvm.kotlin_modifier_words(p) for p in parameters):
        return Refusal(label, "a constructor with a variable-arity parameter")
    default = _kotlin_initializer(parameter)
    if default is not None and not _kotlin_inert(default, local_name_ok=False):
        return Refusal(label, f"a default value that may do work ({' '.join(_text(default).split())[:60]})")
    reason = _class_refusal(root, declaration, "kt")
    if reason is not None:
        return Refusal(label, reason)
    if _documents(declaration, name):
        return Refusal(label, "its documentation comment names it")
    if any(c.type in ("val", "var") for c in parameter.children):
        spelled = _string_spells(repo_dir, name)
        if spelled is not None:
            return Refusal(label, f"{spelled} spells its name in a string (reflection may read it)")
    index = parameters.index(parameter)
    calls = constructions(path, declaration, class_name, repo_dir)
    if isinstance(calls, str):
        return Refusal(label, calls)
    arguments = _kotlin_argument_sites(calls, index, name)
    if isinstance(arguments, str):
        return Refusal(label, arguments)
    sites = [Site(path, parameter.start_byte, parameter.end_byte, "parameter"), *arguments]
    return Removal(path, class_name, name, label, index, tuple(sites), _exported_kotlin(declaration))


def _plan_kotlin_member(path: str, root: Node, data: bytes, label: str, repo_dir: str) -> Removal | Refusal:
    class_name, _, member_name = label.partition(".")
    declarations = _find_class(root, "kt", class_name)
    if len(declarations) != 1:
        return Refusal(label, "the class is declared more than once in its file")
    declaration = declarations[0]
    found = [(m, n) for m, n in jvm.kotlin_properties(declaration) if _text(n) == member_name]
    if len(found) != 1:
        return Refusal(label, "declared more than once")
    member, name = found[0]
    if member.type == "class_parameter":
        return _plan_kotlin_parameter(path, root, declaration, class_name, member, label, repo_dir)
    if _annotated(member, "kt"):
        return Refusal(label, "an annotated property")
    if _imports_lombok(root):
        return Refusal(label, "its file uses Lombok")
    words = jvm.kotlin_modifier_words(member)
    if words & {"lateinit", "override", "abstract", "open", "expect", "actual", "external"}:
        return Refusal(label, f"a `{sorted(words & {'lateinit', 'override', 'abstract', 'open', 'expect', 'actual', 'external'})[0]}` property")
    if any(c.type in ("property_delegate", "getter", "setter") for c in member.named_children):
        return Refusal(label, "a delegated property or one with an accessor")
    value = _kotlin_initializer(member)
    plain = {_text(p) for p in jvm.plain_parameters(declaration)}
    if value is None:
        return Refusal(label, "a property without an initializer")
    if not (_kotlin_inert(value, local_name_ok=False) or (value.type == "identifier" and _text(value) in plain)):
        return Refusal(label, f"an initializer that may do work ({' '.join(_text(value).split())[:60]})")
    for node in _walk(root):
        if node.type == "identifier" and _text(node) == member_name and node != name and jvm._kotlin_written(node):
            if not (jvm._kotlin_bare(node) and jvm.shadowed(node, "kt")):
                return Refusal(label, "still assigned")
    spelled = _string_spells(repo_dir, member_name)
    if spelled is not None:
        return Refusal(label, f"{spelled} spells its name in a string (reflection may read it)")
    sites = (Site(path, *_member_span(member, data), "member"),)
    return Removal(path, class_name, None, label, -1, sites, False, member_name)


def _kotlin_initializer(member: Node) -> Node | None:
    after = False
    for part in member.children:
        if after and part.is_named and part.type not in ("comment", "line_comment", "multiline_comment"):
            return part
        after = after or part.type == "="
    return None


def _plan_kotlin_plain(path: str, root: Node, label: str, repo_dir: str) -> Removal | Refusal:
    class_name, _, rest = label.partition("(")
    name = rest.rstrip(")")
    declarations = [d for d in _find_class(root, "kt", class_name) if d.type == "class_declaration"]
    if len(declarations) != 1:
        return Refusal(label, "the class is declared more than once in its file")
    declaration = declarations[0]
    found = [p for p in _kotlin_parameters(declaration) if _text(jvm.child_of_type(p, "identifier") or p) == name]
    if len(found) != 1 or any(c.type in ("val", "var") for c in found[0].children):
        return Refusal(label, "not a plain constructor parameter", quiet=True)
    if jvm.parameter_reads(declaration, name.encode()) != 0:
        return Refusal(label, "still read")
    return _plan_kotlin_parameter(path, root, declaration, class_name, found[0], label, repo_dir)


# --- entry points ---------------------------------------------------------------------------


def plan(path: str, source: str, kind: str, label: str, repo_dir: str) -> Removal | Refusal:
    """Prove the unread member or parameter ``label`` of the Java or Kotlin
    file at ``path``, whose current text is ``source``, can be removed."""
    grammar = grammar_of(path)
    names = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", label)
    if not names or any(IDENT.fullmatch(n) is None for n in names):
        return Refusal(label, "a name the deletion rules cannot spell")
    data = source.encode("utf-8")
    root = _parse(source, grammar)
    if root.has_error:
        return Refusal(label, "the declaring file does not parse")
    if grammar == "java":
        if kind != "unread member":
            return Refusal(label, "not a field", quiet=True)
        return _plan_java_field(path, root, data, label, repo_dir)
    if kind == "unread constructor parameter":
        return _plan_kotlin_plain(path, root, label, repo_dir)
    return _plan_kotlin_member(path, root, data, label, repo_dir)


def has_member(source: str, path: str, class_name: str, name: str) -> bool:
    """Whether ``class_name`` in ``source`` still declares the member or
    primary-constructor parameter ``name``."""
    grammar = grammar_of(path)
    root = _parse(source, grammar)
    for declaration in _find_class(root, grammar, class_name):
        listed = jvm.java_fields(declaration) if grammar == "java" else jvm.kotlin_properties(declaration)
        if any(_text(n) == name for _, n in listed):
            return True
        if grammar == "kt" and any(_text(n) == name for n in jvm.plain_parameters(declaration)):
            return True
    return False
