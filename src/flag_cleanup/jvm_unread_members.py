"""Find the Java and Kotlin private members a fold left unread.

``private final FeatureClient flags;``, assigned in the constructor, and
Kotlin's ``class Svc(private val flags: FeatureClient)`` are the usual shape: a
service that is handed a flags client only to call it. When every read of the
member was a flag read the run folded, the member stays with nothing reading
it. Neither compiler rejects that, but the linters a JVM build commonly gates
on do: detekt's ``UnusedPrivateProperty`` (active by default, and detekt fails
on any finding by default), Error Prone's ``UnusedVariable``, PMD's
``UnusedPrivateField``, Sonar's ``S1068``. The pull request names it.

The same differential as :mod:`flag_cleanup.ts_unread_members` decides it:
**a member is reported only if it was read BEFORE the fold and is read nowhere
AFTER it**, so a member the customer had already left unread is not blamed on
this run. Only ``private`` members qualify, since anything else may be read
from another file. Reads are counted across the whole file (a nested or
enclosing class can read a private member too), and the count errs towards
"still read": any spelling of the name counts, a method or a member of another
object of the same name included, except

- the declaration itself, and the target of a plain ``=``
  (``this.flags = flags``), which writes without reading;
- a bare name that a parameter (of a method, constructor, lambda or
  ``catch``), a loop variable or an earlier local of an enclosing block
  declares again: ``this.flags = flags`` reads the PARAMETER.

A Kotlin string template's ``$flags`` counts. A mistake costs a missed note.

Skipped, so never reported: a class name declared twice in one file, a member
name declared twice in one file, a file that does not parse, and a member that
code the build generates may read. That is a member whose class carries an
annotation that generates readers of every field (Lombok's ``@Data``,
``@Value``, ``@Getter``, ``@ToString``, ``@EqualsAndHashCode``; a persistence
or serialization annotation such as ``@Entity`` or kotlinx's
``@Serializable``), whose class is ``Serializable`` or ``Parcelable``, or that
carries any annotation itself other than the injection and nullability ones
(``@Autowired``, ``@Inject``, ``@Value``, ``@Nullable``, ...), which only
write it.

:func:`unread_constructor_parameters` asks the same differential of a PLAIN
Kotlin primary-constructor parameter (no ``val``/``var``), which is visible
only to the class's property initializers, ``init`` blocks, superclass
arguments and later parameters' defaults. The fold strands one when its only
read was a flag read in an ``init`` block or an initializer.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from flag_cleanup.unused import _parse

#: Class annotations under which generated code reads every field.
CLASS_READERS = frozenset(
    {
        # Lombok
        "Data", "Value", "Getter", "ToString", "EqualsAndHashCode",
        # persistence and serialization
        "Entity", "Embeddable", "MappedSuperclass", "Document", "Table",
        "JsonAutoDetect", "Serializable", "Parcelize", "AutoValue",
    }
)
#: Supertypes whose serialization reads every non-transient field.
SERIALIZED = frozenset({"Serializable", "Externalizable", "Parcelable"})
#: Member annotations that only write the member (or say nothing about reads).
WRITERS = frozenset(
    {
        "Autowired", "Inject", "Value", "Resource", "Qualifier", "Named",
        "Nullable", "NonNull", "NotNull", "Nonnull", "Deprecated",
        "SuppressWarnings", "Suppress",
    }
)

#: Parents of an identifier that declares a name rather than reading one.
_KT_BINDERS = frozenset(
    {"variable_declaration", "parameter", "catch_block", "parameter_with_optional_type"}
)
_JAVA_CLASSES = frozenset({"class_declaration", "enum_declaration"})
_KT_CLASSES = frozenset({"class_declaration", "object_declaration", "companion_object"})


def walk(node: Node) -> Iterator[Node]:
    """Pre-order, in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


# --- annotations --------------------------------------------------------------


def _last_segment(node: Node) -> str:
    return text(node).rsplit(".", 1)[-1].strip()


def java_annotations(modifiers: Node | None) -> list[str]:
    """The simple names of the annotations in a Java ``modifiers`` node."""
    if modifiers is None:
        return []
    names: list[str] = []
    for child in modifiers.named_children:
        if child.type in ("marker_annotation", "annotation"):
            name = child.child_by_field_name("name")
            if name is not None:
                names.append(_last_segment(name))
    return names


def kotlin_annotations(modifiers: Node | None) -> list[str]:
    """The simple names of the annotations in a Kotlin ``modifiers`` node."""
    if modifiers is None:
        return []
    names: list[str] = []
    for child in modifiers.named_children:
        if child.type != "annotation":
            continue
        # ``@A``, ``@A(x)`` and the grouped ``@[A B(x)]`` all hold their names as a direct
        # ``user_type`` child or one under a ``constructor_invocation`` child; arguments are skipped.
        for part in child.named_children:
            if part.type == "constructor_invocation":
                part = child_of_type(part, "user_type")
            if part is not None and part.type == "user_type":
                names.append(_last_segment(part))
    return names


def child_of_type(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def java_modifiers(node: Node) -> Node | None:
    return child_of_type(node, "modifiers")


def java_has_modifier(node: Node, keyword: str) -> bool:
    modifiers = java_modifiers(node)
    return modifiers is not None and any(c.type == keyword for c in modifiers.children)


def kotlin_modifier_words(node: Node) -> set[str]:
    """Every keyword in ``node``'s ``modifiers`` (``private``, ``open``, ...)."""
    modifiers = child_of_type(node, "modifiers")
    if modifiers is None:
        return set()
    return {
        text(leaf)
        for part in modifiers.named_children
        if part.type != "annotation"
        for leaf in walk(part)
        if leaf.child_count == 0
    }


def _java_class_reads_every_field(declaration: Node) -> bool:
    if set(java_annotations(java_modifiers(declaration))) & CLASS_READERS:
        return True
    interfaces = declaration.child_by_field_name("interfaces")
    return interfaces is not None and any(
        n.type == "type_identifier" and text(n) in SERIALIZED for n in walk(interfaces)
    )


def _kotlin_class_reads_every_field(declaration: Node) -> bool:
    if set(kotlin_annotations(child_of_type(declaration, "modifiers"))) & CLASS_READERS:
        return True
    supertypes = child_of_type(declaration, "delegation_specifiers")
    return supertypes is not None and any(
        n.type == "identifier" and text(n) in SERIALIZED for n in walk(supertypes)
    )


# --- shadowing ----------------------------------------------------------------


def _java_binders(scope: Node) -> list[Node]:
    """The name nodes a Java method, constructor, lambda, ``catch`` or loop
    declares for its whole extent."""
    kind = scope.type
    if kind in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
        parameters = scope.child_by_field_name("parameters")
        return [
            p.child_by_field_name("name") or child_of_type(p, "variable_declarator")
            for p in (parameters.named_children if parameters is not None else ())
            if p.type in ("formal_parameter", "spread_parameter", "receiver_parameter")
        ]
    if kind == "lambda_expression":
        parameters = scope.child_by_field_name("parameters")
        if parameters is None:
            return []
        if parameters.type == "identifier":
            return [parameters]
        found = []
        for p in parameters.named_children:
            if p.type == "identifier":
                found.append(p)
            elif p.type in ("formal_parameter", "spread_parameter"):
                found.append(p.child_by_field_name("name"))
        return found
    if kind == "catch_clause":
        parameter = child_of_type(scope, "catch_formal_parameter")
        return [parameter.child_by_field_name("name")] if parameter is not None else []
    if kind == "enhanced_for_statement":
        return [scope.child_by_field_name("name")]
    if kind == "for_statement":
        init = scope.child_by_field_name("init")
        if init is not None and init.type == "local_variable_declaration":
            return [d.child_by_field_name("name") for d in init.named_children if d.type == "variable_declarator"]
    if kind == "try_with_resources_statement":
        resources = scope.child_by_field_name("resources")
        return [
            r.child_by_field_name("name")
            for r in (resources.named_children if resources is not None else ())
            if r.type == "resource"
        ]
    return []


def _java_locals(statement: Node) -> list[Node]:
    if statement.type != "local_variable_declaration":
        return []
    return [d.child_by_field_name("name") for d in statement.named_children if d.type == "variable_declarator"]


def _kotlin_binders(scope: Node) -> list[Node]:
    kind = scope.type
    if kind in ("function_declaration", "secondary_constructor", "anonymous_function"):
        parameters = child_of_type(scope, "function_value_parameters")
        return [
            child_of_type(p, "identifier")
            for p in (parameters.named_children if parameters is not None else ())
            if p.type == "parameter"
        ]
    if kind == "lambda_literal":
        parameters = child_of_type(scope, "lambda_parameters")
        if parameters is None:
            return []
        return [
            n
            for n in walk(parameters)
            if n.type == "identifier" and n.parent is not None and n.parent.type == "variable_declaration"
        ]
    if kind == "for_statement":
        return [
            n
            for c in scope.named_children
            if c.type in ("variable_declaration", "multi_variable_declaration")
            for n in walk(c)
            if n.type == "identifier" and n.parent is not None and n.parent.type == "variable_declaration"
        ]
    if kind == "catch_block":
        name = child_of_type(scope, "identifier")
        return [name] if name is not None else []
    if kind == "setter":
        parameter = child_of_type(scope, "parameter_with_optional_type") or child_of_type(scope, "parameter")
        name = child_of_type(parameter, "identifier") if parameter is not None else None
        return [name] if name is not None else []
    return []


def _kotlin_locals(statement: Node) -> list[Node]:
    if statement.type != "property_declaration" or statement.parent is None or statement.parent.type == "class_body":
        return []
    return [
        n
        for c in statement.named_children
        if c.type in ("variable_declaration", "multi_variable_declaration")
        for n in walk(c)
        if n.type == "identifier" and n.parent is not None and n.parent.type == "variable_declaration"
    ]


_BLOCKS = {
    "java": frozenset({"block", "constructor_body", "switch_block_statement_group"}),
    "kt": frozenset({"block", "statements", "control_structure_body", "lambda_literal", "function_body"}),
}


def _covers(scope: Node, child: Node, grammar: str) -> bool:
    """Whether what ``scope`` binds for its extent is in scope in its
    ``child``: a loop variable is not in its own iterable, a resource not in
    the ``catch`` or ``finally`` of its ``try``, a parameter not in the
    parameter list (a Kotlin default value reads the member)."""
    kind = scope.type
    if grammar == "java":
        if kind in ("enhanced_for_statement", "try_with_resources_statement", "lambda_expression",
                    "method_declaration", "constructor_declaration", "compact_constructor_declaration"):
            return child == scope.child_by_field_name("body")
        if kind == "catch_clause":
            return child.type == "block"
        if kind == "for_statement":
            return child != scope.child_by_field_name("init")
        return True
    if kind in ("function_declaration", "anonymous_function", "setter"):
        return child.type == "function_body"
    if kind == "secondary_constructor":
        return child.type != "function_value_parameters"
    if kind == "for_statement":
        return bool(scope.named_children) and child == scope.named_children[-1]
    if kind == "catch_block":
        return child.type == "block"
    return True


def shadowed(node: Node, grammar: str) -> bool:
    """Whether the bare name ``node`` names a parameter, loop variable or
    local declared in an enclosing scope rather than a member."""
    name = node.text
    binders = _java_binders if grammar == "java" else _kotlin_binders
    local = _java_locals if grammar == "java" else _kotlin_locals
    blocks = _BLOCKS[grammar]
    child = node
    scope = node.parent
    while scope is not None:
        if scope.type in ("class_body", "enum_body", "enum_body_declarations"):
            return False  # a member scope: what is above is the class's
        if _covers(scope, child, grammar) and any(
            b is not None and b.text == name for b in binders(scope)
        ):
            return True
        if scope.type in blocks:
            for sibling in scope.named_children:
                if sibling.start_byte >= child.start_byte:
                    break
                if any(b is not None and b.text == name for b in local(sibling)):
                    return True
        child, scope = scope, scope.parent
    return False


# --- reads ----------------------------------------------------------------------


def _java_bare(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return True
    if parent.type == "field_access" and parent.child_by_field_name("field") == node:
        return False
    if parent.type == "method_invocation" and parent.child_by_field_name("name") == node:
        return False
    return True


def _java_written(node: Node) -> bool:
    """Whether ``node`` is the whole target of a plain ``=``: ``f = v`` or
    ``this.f = v`` (or ``Outer.this.f = v``)."""
    target = node
    parent = node.parent
    if parent is not None and parent.type == "field_access" and parent.child_by_field_name("field") == node:
        obj = parent.child_by_field_name("object")
        if obj is None or obj.type not in ("this",) and not (
            obj.type == "field_access" and text(obj).endswith(".this")
        ):
            return False
        target = parent
    assignment = target.parent
    if assignment is None or assignment.type != "assignment_expression":
        return False
    operator = assignment.child_by_field_name("operator")
    return (
        assignment.child_by_field_name("left") == target
        and operator is not None
        and operator.type == "="
    )


def _kotlin_bare(node: Node) -> bool:
    parent = node.parent
    return not (
        parent is not None
        and parent.type == "navigation_expression"
        and parent.named_children
        and parent.named_children[-1] == node
        and parent.named_children[0] != node
    )


def _kotlin_written(node: Node) -> bool:
    target = node
    parent = node.parent
    if parent is not None and parent.type == "navigation_expression" and not _kotlin_bare(node):
        if parent.named_children[0].type != "this_expression":
            return False
        target = parent
    assignment = target.parent
    if assignment is None or assignment.type != "assignment":
        return False
    operator = assignment.child_by_field_name("operator")
    return (
        assignment.child_by_field_name("left") == target
        and operator is not None
        and operator.type == "="
    )


def _template_reads(root: Node, names: set[str]) -> dict[str, int]:
    """``$name`` spellings in Kotlin string literals (``${name}`` is parsed)."""
    counts = dict.fromkeys(names, 0)
    if not names:
        return counts
    # An escaped `\$` would count too: erring towards "still read" costs a
    # missed note, while telling `"\$x"` from `"\\$x"` wrongly costs a build.
    pattern = re.compile(r"\$(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_])")
    for node in walk(root):
        if node.type in ("string_literal", "multiline_string_literal"):
            for match in pattern.finditer(text(node)):
                counts[match.group(1)] += 1
    return counts


def _declares(node: Node, grammar: str) -> bool:
    """Whether ``node`` is the name a local, parameter or loop variable
    declares, which reads nothing."""
    parent = node.parent
    if parent is None:
        return False
    if grammar == "kt":
        return parent.type in _KT_BINDERS or parent.type == "class_parameter"
    if parent.type == "lambda_expression":
        return parent.child_by_field_name("parameters") == node
    if parent.type in ("variable_declarator", "formal_parameter", "spread_parameter",
                       "catch_formal_parameter", "enhanced_for_statement", "resource"):
        return parent.child_by_field_name("name") == node
    return parent.type == "inferred_parameters"


#: Directives whose names are packages and types, never a member's read.
_DIRECTIVES = frozenset(
    {"package_declaration", "import_declaration", "package_header", "import", "import_header", "import_list"}
)


def _walk_code(root: Node) -> Iterator[Node]:
    """:func:`walk` without the package and import directives."""
    stack = [root]
    while stack:
        current = stack.pop()
        if current.type in _DIRECTIVES:
            continue
        yield current
        stack.extend(reversed(current.children))


def reads(root: Node, grammar: str, names: set[str], declared: set[tuple[int, int]]) -> dict[str, int]:
    """For each of ``names``, how many spellings in ``root`` may read a
    member of that name (see the module docstring)."""
    counts = dict.fromkeys(names, 0)
    bare = _java_bare if grammar == "java" else _kotlin_bare
    written = _java_written if grammar == "java" else _kotlin_written
    for node in _walk_code(root):
        if node.type != "identifier":
            continue
        name = text(node)
        if name not in counts or (node.start_byte, node.end_byte) in declared:
            continue
        if written(node) or _declares(node, grammar):
            continue
        if bare(node) and shadowed(node, grammar):
            continue
        counts[name] += 1
    if grammar == "kt":
        for name, count in _template_reads(root, names).items():
            counts[name] += count
    return counts


# --- members --------------------------------------------------------------------


def _java_body(declaration: Node) -> list[Node]:
    body = declaration.child_by_field_name("body")
    if body is None:
        return []
    if body.type == "enum_body":
        extra = child_of_type(body, "enum_body_declarations")
        return list(extra.named_children) if extra is not None else []
    return list(body.named_children)


def java_fields(declaration: Node) -> list[tuple[Node, Node]]:
    """``(field_declaration, name node)`` for each field of a Java class."""
    return [
        (member, d.child_by_field_name("name"))
        for member in _java_body(declaration)
        if member.type == "field_declaration"
        for d in member.named_children
        if d.type == "variable_declarator" and d.child_by_field_name("name") is not None
    ]


def kotlin_properties(declaration: Node) -> list[tuple[Node, Node]]:
    """``(declaration, name node)`` for each member property of a Kotlin
    class: a ``property_declaration`` of its body, or a ``val``/``var``
    primary-constructor parameter."""
    found: list[tuple[Node, Node]] = []
    constructor = child_of_type(declaration, "primary_constructor")
    parameters = child_of_type(constructor, "class_parameters") if constructor is not None else None
    for parameter in parameters.named_children if parameters is not None else ():
        if parameter.type == "class_parameter" and any(c.type in ("val", "var") for c in parameter.children):
            name = child_of_type(parameter, "identifier")
            if name is not None:
                found.append((parameter, name))
    body = child_of_type(declaration, "class_body")
    for member in body.named_children if body is not None else ():
        if member.type != "property_declaration":
            continue
        variable = child_of_type(member, "variable_declaration")
        name = child_of_type(variable, "identifier") if variable is not None else None
        if name is not None:
            found.append((member, name))
    return found


def class_name(declaration: Node, grammar: str) -> str | None:
    if declaration.type == "companion_object":
        name = child_of_type(declaration, "identifier")
        return text(name) if name is not None else "Companion"
    name = declaration.child_by_field_name("name")
    if name is None and grammar == "kt":
        name = child_of_type(declaration, "identifier")
    return text(name) if name is not None else None


def is_kotlin_interface(declaration: Node) -> bool:
    return any(c.type == "interface" for c in declaration.children)


def _private(member: Node, grammar: str) -> bool:
    if grammar == "java":
        return java_has_modifier(member, "private")
    return "private" in kotlin_modifier_words(member)


def _annotations(member: Node, grammar: str) -> list[str]:
    if grammar == "java":
        return java_annotations(java_modifiers(member))
    return kotlin_annotations(child_of_type(member, "modifiers"))


def classes(root: Node, grammar: str) -> list[Node]:
    kinds = _JAVA_CLASSES if grammar == "java" else _KT_CLASSES
    return [
        n for n in walk(root) if n.type in kinds and not (grammar == "kt" and is_kotlin_interface(n))
    ]


def members(source: str, grammar: str, eligibility: object = None) -> dict[tuple[str, str], int] | None:
    """``(class, member) -> reads`` for each private member of a named class in
    ``source`` that the module docstring does not skip. ``None`` when the file
    does not parse. A class is considered only when it is eligible
    (:mod:`flag_cleanup.jvm_class_eligibility`)."""
    from flag_cleanup import jvm_class_eligibility

    eligible = eligibility if eligibility is not None else jvm_class_eligibility.Eligibility(None)
    root = _parse(source, grammar)
    if root.has_error:
        return None
    owners: dict[str, str] = {}
    seen_classes: set[str] = set()
    duplicated_classes: set[str] = set()
    names: dict[str, int] = {}
    declared: set[tuple[int, int]] = set()
    candidates: list[tuple[str, str]] = []
    for declaration in classes(root, grammar):
        name = class_name(declaration, grammar)
        if name is None:
            continue
        if name in seen_classes:
            duplicated_classes.add(name)
        seen_classes.add(name)
        everything_read = (
            _java_class_reads_every_field(declaration)
            if grammar == "java"
            else _kotlin_class_reads_every_field(declaration)
        ) or not eligible.class_ok(declaration, grammar, source)
        listed = java_fields(declaration) if grammar == "java" else kotlin_properties(declaration)
        for member, name_node in listed:
            member_name = text(name_node)
            names[member_name] = names.get(member_name, 0) + 1
            declared.add((name_node.start_byte, name_node.end_byte))
            if everything_read or not _private(member, grammar):
                continue
            if not set(_annotations(member, grammar)) <= WRITERS:
                continue
            candidates.append((name, member_name))
            owners[member_name] = name
    wanted = {
        (cls, member)
        for cls, member in candidates
        if cls not in duplicated_classes and names.get(member) == 1
    }
    counts = reads(root, grammar, {member for _, member in wanted}, declared)
    return {(cls, member): counts[member] for cls, member in candidates if (cls, member) in wanted}


def unread_members(before: str, after: str, grammar: str, eligibility: object = None) -> list[str]:
    """Each private member read in ``before`` and not in ``after``.

    Returned as ``Class.member`` (``Svc.flags``) in the order they appear in
    ``after``. ``grammar`` is ``"java"`` or ``"kt"``.
    """
    old = members(before, grammar, eligibility)
    new = members(after, grammar, eligibility)
    if old is None or new is None:
        return []
    return [
        f"{cls}.{name}"
        for (cls, name), count in new.items()
        if count == 0 and old.get((cls, name), 0) > 0
    ]


# --- Kotlin plain constructor parameters ------------------------------------------


def initializer_regions(declaration: Node) -> list[Node]:
    """The parts of a Kotlin class where a plain primary-constructor
    parameter is in scope: property initializers and delegates (not
    accessors), ``init`` blocks, the superclass arguments, and the
    parameters' own default values."""
    regions: list[Node] = []
    constructor = child_of_type(declaration, "primary_constructor")
    parameters = child_of_type(constructor, "class_parameters") if constructor is not None else None
    for parameter in parameters.named_children if parameters is not None else ():
        after_equals = False
        for part in parameter.children:
            if after_equals and part.is_named:
                regions.append(part)
            after_equals = after_equals or part.type == "="
    supertypes = child_of_type(declaration, "delegation_specifiers")
    if supertypes is not None:
        regions.append(supertypes)
    body = child_of_type(declaration, "class_body")
    for member in body.named_children if body is not None else ():
        if member.type == "anonymous_initializer":
            regions.append(member)
        elif member.type == "property_declaration":
            after_equals = False
            for part in member.children:
                if part.type == "property_delegate":
                    regions.append(part)
                if after_equals and part.is_named and part.type not in ("getter", "setter"):
                    regions.append(part)
                after_equals = (after_equals or part.type == "=") and part.type not in ("getter", "setter")
    return regions


def plain_parameters(declaration: Node) -> list[Node]:
    """The name nodes of a Kotlin class's plain primary-constructor parameters."""
    constructor = child_of_type(declaration, "primary_constructor")
    parameters = child_of_type(constructor, "class_parameters") if constructor is not None else None
    return [
        name
        for p in (parameters.named_children if parameters is not None else ())
        if p.type == "class_parameter" and not any(c.type in ("val", "var") for c in p.children)
        and (name := child_of_type(p, "identifier")) is not None
    ]


def parameter_reads(declaration: Node, name: bytes) -> int | None:
    """Reads of the plain parameter ``name`` in ``declaration``'s initializer
    regions, or ``None`` when one of them binds the name again (a lambda
    parameter, a local): which spellings are the parameter's is then a scope
    question this does not ask."""
    regions = initializer_regions(declaration)
    count = 0
    for region in regions:
        for node in walk(region):
            if node.type != "identifier" or node.text != name:
                continue
            parent = node.parent
            if parent is not None and parent.type in _KT_BINDERS:
                return None  # bound again
            if _kotlin_bare(node):
                count += 1
    decoded = name.decode("utf-8", "replace")
    for region in regions:
        count += _template_reads(region, {decoded})[decoded]
    return count


def _parameters(source: str) -> dict[tuple[str, str], int] | None:
    root = _parse(source, "kt")
    if root.has_error:
        return None
    found: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    duplicated: set[str] = set()
    for declaration in classes(root, "kt"):
        if declaration.type != "class_declaration":
            continue
        name = class_name(declaration, "kt")
        if name is None:
            continue
        if name in seen:
            duplicated.add(name)
        seen.add(name)
        for parameter in plain_parameters(declaration):
            count = parameter_reads(declaration, parameter.text)
            if count is not None:
                found[(name, text(parameter))] = count
    return {key: value for key, value in found.items() if key[0] not in duplicated}


def unread_constructor_parameters(before: str, after: str) -> list[str]:
    """Each plain Kotlin primary-constructor parameter read in ``before`` and
    not in ``after``, as ``Class(parameter)``."""
    old = _parameters(before)
    new = _parameters(after)
    if old is None or new is None:
        return []
    return [
        f"{cls}({name})"
        for (cls, name), count in new.items()
        if count == 0 and old.get((cls, name), 0) > 0
    ]
