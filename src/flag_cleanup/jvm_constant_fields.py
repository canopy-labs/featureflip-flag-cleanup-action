"""A private field the fold left assigned one literal in the constructor.

    private final boolean allowScottish;                       // Java
    Gateway(Client client) { this.allowScottish = true; }      // after the fold

    private val allowScottish: Boolean                         // Kotlin
    init { allowScottish = true }                              // after the fold

The engine already inlines a private field whose INITIALIZER is the read
(``private final boolean f = <read>;``, ``private val f = <read>``) into every
reader and deletes it. This pass turns the constructor-assigned form into that
one: the literal moves into the declaration, written as the marker call
``inlined<marker>(<literal>)``, and the assignment goes. The marker round hands
the file back to the engine, whose marker rule creates the literal inside the
cascade, so the field inline and the folds behind it run as they do for the
initializer form.

Moving a literal from the constructor body to the declaration changes only
WHEN the field holds it: from the assignment on, instead of from the start of
construction. So it is proven that nothing can read the field before the
assignment runs:

* **The field** is a single ``private final boolean`` (Java) or
  ``private val …: Boolean`` (Kotlin) declarator with no initializer,
  annotation, getter or delegate, in a class with no superclass to call back
  into it (a Java ``extends``, a Kotlin supertype constructor call, or a
  secondary constructor calling ``super``). Each interface it implements must
  be declared in the checkout, in a file that does not serialize, and extend
  only such interfaces: one declared elsewhere may extend ``Serializable``.
  Java enums and records refuse; so does a Kotlin class with a member
  extension, ``operator`` or ``infix`` function, any of which reaches the
  instance with no call the check below can see.
* **The one write** is ``f = <literal>`` or ``this.f = <literal>``, a statement
  directly in the body of the class's only Java constructor (one that does not
  delegate with ``this(…)``/``super(…)``), or of a Kotlin ``init`` block. The
  customer's code wrote something else there: a literal they wrote is theirs.
* **Everything that runs before it** (every instance field initializer and
  instance initializer block in Java, since they run before the constructor
  body; in Kotlin the property initializers and ``init`` blocks above it; and
  the statements before it in its own body) holds no ``this``, no read of the
  field, no call of a method of the class by its bare name (in Kotlin, no
  bare call at all outside a few standard-library builders, since an
  extension of the class declared anywhere could call back in), no read of a
  Kotlin property that can run code (a getter, a delegate, a function value),
  no construction of a class this file declares and no anonymous object.
* **Nothing names the field by its spelling elsewhere**: no other
  declaration of the name in the file, no string in it, and no string,
  Groovy or Scala file or unparsed file of the checkout naming it.
* **Generated code cannot read it**: the file mentions no Lombok, Java or
  kotlinx serialization, JPA or document mapping, Jackson field detection or
  Gson. A library that builds the object without running its constructor
  (a mock, an objenesis instance) is not detected; the initializer form the
  engine already inlines shares that exposure.

A refusal leaves ``this.f = true;`` standing, which is what the run produced
before this pass existed.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from flag_cleanup.private_constant_params import (
    _LITERALS,
    PARAM_PREFIX,
    _apply,
    _field,
    _jvm_mentioned_elsewhere,
    _text,
    _walk,
    _word,
)
from flag_cleanup.ts_constant_props import _own_lines

#: Bare Kotlin calls known not to reach the instance under construction.
_KT_INERT_CALLS = frozenset(
    {
        "listOf", "mutableListOf", "arrayListOf", "emptyList", "listOfNotNull",
        "setOf", "mutableSetOf", "hashSetOf", "linkedSetOf", "emptySet",
        "mapOf", "mutableMapOf", "hashMapOf", "linkedMapOf", "emptyMap",
        "arrayOf", "emptyArray", "intArrayOf", "lazy", "println", "print",
        "require", "requireNotNull", "check", "checkNotNull", "error",
    }
)
#: Code that reads or writes fields by enumeration rather than by name: Lombok,
#: Java and kotlinx serialization, JPA and document mappers, Jackson field
#: detection, Gson. Removing a field changes what they see.
_GENERATED = re.compile(
    r"\blombok\b|\bSerializable\b|\bExternalizable\b|@Entity\b|@Embeddable\b|@MappedSuperclass\b"
    r"|@Document\b|@Table\b|@JsonAutoDetect\b|\bGson\b|@Serializable\b|@Parcelize\b"
)


def inline_across(files, repo_dir: str, language: str, marker: str, in_run) -> dict[str, tuple[str, str]]:
    finder = {"java": _java, "kt": _kt}.get(language)
    if finder is None:
        return {}
    for path, (before, after) in sorted(files.items()):
        if before == after or marker in after or not in_run(path) or path.endswith(".kts"):
            continue
        if _GENERATED.search(after):
            continue
        rewritten = finder(path, before, after, repo_dir, marker)
        if rewritten is not None:
            return {path: (after, rewritten)}
    return {}


def _assignment_literal(statement: Node, name: str, data: bytes, java: bool) -> str | None:
    """The literal ``statement`` assigns to the field ``name``, or None."""
    if java:
        if statement.type != "expression_statement" or len(statement.named_children) != 1:
            return None
        assignment = statement.named_children[0]
        if assignment.type != "assignment_expression":
            return None
        operator = assignment.child_by_field_name("operator")
        if operator is None or operator.type != "=":
            return None
    else:
        assignment = statement
        if assignment.type != "assignment" or not any(c.type == "=" for c in assignment.children):
            return None
    left = assignment.child_by_field_name("left")
    right = assignment.child_by_field_name("right")
    if left is None or right is None or not _is_field(left, name, data, java):
        return None
    text = _text(right, data)
    return text if text in _LITERALS else None


def _is_field(node: Node, name: str, data: bytes, java: bool) -> bool:
    """``name`` or ``this.name``."""
    if node.type == "identifier":
        return _text(node, data) == name
    if java and node.type == "field_access":
        target = node.child_by_field_name("object")
        field = node.child_by_field_name("field")
        return target is not None and target.type == "this" and field is not None and _text(field, data) == name
    if not java and node.type == "navigation_expression":
        named = node.named_children
        return (
            len(named) == 2
            and named[0].type == "this_expression"
            and _text(named[0], data) == "this"
            and named[1].type == "identifier"
            and _text(named[1], data) == name
        )
    return False


def _inert(nodes: list[Node], data: bytes, members: set[str], local_types: set[str], java: bool,
           allowed: set[int]) -> bool:
    """Whether code that runs before the assignment can reach the field."""
    for root in nodes:
        for node in _walk(root):
            if node.id in allowed:
                continue
            kind = node.type
            if kind in ("this", "super", "this_expression", "super_expression", "explicit_constructor_invocation"):
                return False
            if kind == "object_literal":
                return False  # an anonymous object may call back into the class
            if java and kind == "object_creation_expression":
                if any(c.type == "class_body" for c in node.children):
                    return False
                created = node.child_by_field_name("type")
                if created is not None and _text(created, data).split("<")[0].split(".")[-1] in local_types:
                    return False
            if java and kind == "method_invocation":
                target = node.child_by_field_name("object")
                if target is None:
                    return False
            if not java and kind == "call_expression":
                callee = node.named_children[0] if node.named_children else None
                if callee is not None and callee.type == "identifier":
                    called = _text(callee, data)
                    if called in members or called in local_types:
                        return False
                    # A bare call may be an extension of this class, declared
                    # anywhere, that calls back in. Only a constructor and the
                    # standard library's builders are known not to.
                    if not called[:1].isupper() and called not in _KT_INERT_CALLS:
                        return False
    return True


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


def _java(path, before, after, repo_dir, marker):
    from flag_cleanup import wrapper_java

    data = after.encode("utf-8")
    root = wrapper_java.parse(after)
    if root.has_error:
        return None
    old_data = before.encode("utf-8")
    old_root = wrapper_java.parse(before)
    local_types = {
        _text(n.child_by_field_name("name"), data)
        for n in _walk(root)
        if n.type in ("class_declaration", "record_declaration", "enum_declaration", "interface_declaration")
        and n.child_by_field_name("name") is not None
    }
    for owner in _walk(root):
        if owner.type != "class_declaration" or owner.child_by_field_name("superclass") is not None:
            continue
        body = owner.child_by_field_name("body")
        if body is None:
            continue
        interfaces = owner.child_by_field_name("interfaces")
        names = [_text(t, data) for t in _walk(interfaces) if t.type == "type_identifier"] if interfaces else []
        if not _supertypes_plain(repo_dir, names):
            continue
        for declaration in body.named_children:
            planned = _java_field(declaration, owner, body, root, data, old_root, old_data, local_types, marker)
            if planned is None:
                continue
            name, edits = planned
            if _jvm_mentioned_elsewhere(repo_dir, path, name):
                continue
            return _apply(data, edits)
    return None


def _java_field(declaration, owner, body, root, data, old_root, old_data, local_types, marker):
    if declaration.type != "field_declaration":
        return None
    modifiers = next((c for c in declaration.children if c.type == "modifiers"), None)
    words = [c.type for c in modifiers.children] if modifiers is not None else []
    if sorted(words) != ["final", "private"]:
        return None
    kind = declaration.child_by_field_name("type")
    declarators = [c for c in declaration.named_children if c.type == "variable_declarator"]
    if kind is None or kind.type != "boolean_type" or len(declarators) != 1:
        return None
    declarator = declarators[0]
    name_node = declarator.child_by_field_name("name")
    if name_node is None or declarator.child_by_field_name("value") is not None:
        return None
    if any(c.type == "dimensions" for c in declarator.children):
        return None
    name = _text(name_node, data)
    constructors = [c for c in body.named_children if c.type == "constructor_declaration"]
    if len(constructors) != 1:
        return None
    constructor = constructors[0]
    ctor_body = constructor.child_by_field_name("body")
    if ctor_body is None:
        return None
    statements = [c for c in ctor_body.named_children if c.type not in ("line_comment", "block_comment")]
    if any(s.type == "explicit_constructor_invocation" for s in statements):
        return None
    writes = [(i, s) for i, s in enumerate(statements) if _assignment_literal(s, name, data, True)]
    if len(writes) != 1:
        return None
    index, statement = writes[0]
    literal = _assignment_literal(statement, name, data, True)
    if not _customer_wrote_other(old_root, old_data, name, literal, True):
        return None
    # Every other spelling of the name in the file is a read of this field.
    target = statement.named_children[0].child_by_field_name("left")
    for node in _walk(root):
        if node.type != "identifier" or _text(node, data) != name:
            continue
        if node.id == name_node.id or (target is not None and target.start_byte <= node.start_byte < target.end_byte):
            continue
        if not _java_read(node, data):
            return None
    if _string_names(root, data, name, ("string_literal", "text_block", "character_literal")):
        return None
    if any(c.type == "block" for c in body.named_children):
        return None  # an instance initializer block runs before the constructor body
    # Every instance field initializer runs before the constructor body, then
    # the statements above the assignment.
    before_nodes: list[Node] = [
        value
        for d in body.named_children
        if d.type == "field_declaration" and not _is_static(d)
        for c in d.named_children
        if c.type == "variable_declarator" and (value := c.child_by_field_name("value")) is not None
    ]
    before_nodes += statements[:index]
    methods = {
        _text(m.child_by_field_name("name"), data)
        for m in body.named_children
        if m.type == "method_declaration" and m.child_by_field_name("name") is not None
    }
    if not _inert(before_nodes, data, methods, local_types, True, set()):
        return None
    if any(n.type == "identifier" and _text(n, data) == name for e in before_nodes for n in _walk(e)):
        return None  # a read before the assignment (javac rejects most; a lambda body it does not)
    line = _own_lines(statement, data)
    if line is None:
        return None
    replacement = f" = {PARAM_PREFIX}{marker}({literal})".encode("utf-8")
    return name, [(line[0], line[1], b""), (name_node.end_byte, name_node.end_byte, replacement)]


def _is_static(declaration: Node) -> bool:
    modifiers = next((c for c in declaration.children if c.type == "modifiers"), None)
    return modifiers is not None and any(c.type == "static" for c in modifiers.children)


def _java_read(node: Node, data: bytes) -> bool:
    """A bare read, or ``this.name``; never a declaration, a label, a member
    of something else or a write."""
    parent = node.parent
    if parent is None:
        return False
    field = _field(node)
    if parent.type == "field_access":
        if field != "field":
            return False
        target = parent.child_by_field_name("object")
        if target is None or target.type != "this":
            return False
        return _java_value_position(parent)
    if parent.type in (
        "variable_declarator", "formal_parameter", "catch_formal_parameter", "lambda_expression",
        "inferred_parameters", "labeled_statement", "break_statement", "continue_statement",
        "method_invocation", "method_declaration", "enhanced_for_statement", "resource",
        "spread_parameter", "class_declaration", "element_value_pair", "method_reference",
        "scoped_identifier", "enum_constant",
    ):
        if parent.type == "method_invocation" and field == "object":
            return False  # a primitive has no members
        if parent.type == "variable_declarator" and field == "value":
            return True
        return False
    return _java_value_position(node)


def _java_value_position(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "assignment_expression" and _field(node) == "left":
        return False
    if parent.type in ("update_expression",):
        return False
    return True


def _string_names(root: Node, data: bytes, name: str, kinds: tuple[str, ...]) -> bool:
    word = _word(name)
    return any(n.type in kinds and word.search(_text(n, data)) for n in _walk(root))


def _customer_wrote_other(old_root: Node, old_data: bytes, name: str, literal: str, java: bool) -> bool:
    """Whether the customer's code assigned the field something other than
    ``literal``: the transform made it one."""
    for node in _walk(old_root):
        if java and node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
        elif not java and node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
        else:
            continue
        if left is not None and right is not None and _is_field(left, name, old_data, java):
            return _text(right, old_data) != literal
    return False


# ---------------------------------------------------------------------------
# Kotlin
# ---------------------------------------------------------------------------


def _kt(path, before, after, repo_dir, marker):
    from flag_cleanup import wrapper_kotlin

    data = after.encode("utf-8")
    root = wrapper_kotlin.parse(after)
    if root.has_error:
        return None
    old_data = before.encode("utf-8")
    old_root = wrapper_kotlin.parse(before)
    local_types = {
        _text(n.child_by_field_name("name"), data)
        for n in _walk(root)
        if n.type in ("class_declaration", "object_declaration") and n.child_by_field_name("name") is not None
    }
    for owner in _walk(root):
        if owner.type != "class_declaration":
            continue
        if any(c.type in ("enum", "data", "annotation") for c in owner.children) or _kt_enum(owner, data):
            continue
        specifiers = next((c for c in owner.children if c.type == "delegation_specifiers"), None)
        if specifiers is not None and any(n.type == "constructor_invocation" for n in _walk(specifiers)):
            continue
        body = next((c for c in owner.children if c.type == "class_body"), None)
        if body is None:
            continue
        if not _kt_plain_class(body, data):
            continue
        names = [
            _text(t.named_children[0], data)
            for t in (specifiers.named_children if specifiers is not None else [])
            if t.type == "delegation_specifier" and t.named_children and t.named_children[0].type == "user_type"
        ]
        if not _supertypes_plain(repo_dir, [n.split("<")[0].split(".")[-1] for n in names]):
            continue
        for declaration in body.named_children:
            planned = _kt_field(declaration, owner, body, root, data, old_root, old_data, local_types, marker)
            if planned is None:
                continue
            name, edits = planned
            if _jvm_mentioned_elsewhere(repo_dir, path, name):
                continue
            return _apply(data, edits)
    return None


def _kt_plain_class(body: Node, data: bytes) -> bool:
    """No member can be reached through the implicit ``this`` without a call
    the inertness check sees: no secondary constructor that calls a super
    constructor (which may call back into an override), no member extension
    function or property (``"x".peek()``), and no ``operator`` or ``infix``
    member (``!"x"``, ``a peek b``)."""
    for member in body.named_children:
        if member.type == "secondary_constructor" and re.search(r":\s*super\b", _text(member, data)):
            return False
        if member.type in ("function_declaration", "property_declaration"):
            if any(c.type == "." for c in member.children):
                return False
            modifiers = next((c for c in member.children if c.type == "modifiers"), None)
            if modifiers is not None and re.search(r"\b(operator|infix)\b", _text(modifiers, data)):
                return False
    return True


def _supertypes_plain(repo_dir: str, names: list[str], depth: int = 0) -> bool:
    """Whether every interface in ``names`` is declared in the checkout, in a
    file that says nothing of serialization, and extends only such interfaces.
    One declared elsewhere may extend ``Serializable``."""
    from flag_cleanup import cross_file_keys, wrapper_jvm

    if depth > 5:
        return False
    for name in names:
        header = re.compile(rf"\b(?:interface|class)\s+{re.escape(name)}\b([^{{]*)\{{")
        found = None
        for path in cross_file_keys.mention_files(repo_dir, {name}, exclude=()):
            if not path.endswith(wrapper_jvm.PARSED_EXTENSIONS):
                continue
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                return False
            match = header.search(text)
            if match is None:
                continue
            if found is not None or _GENERATED.search(text):
                return False  # declared twice, or in a file that serializes
            found = match.group(1)
        if found is None:
            return False
        parents = re.sub(r"<[^<>]*>", "", found)
        parents = re.sub(r"\b(extends|implements)\b|:", ",", parents)
        more = [p.strip().split("(")[0].split(".")[-1] for p in parents.split(",") if p.strip()]
        more = [p for p in more if re.fullmatch(r"[A-Za-z_]\w*", p)]
        if not _supertypes_plain(repo_dir, more, depth + 1):
            return False
    return True


def _kt_enum(owner: Node, data: bytes) -> bool:
    modifiers = next((c for c in owner.children if c.type == "modifiers"), None)
    return modifiers is not None and re.search(r"\b(enum|data|value|inline|annotation|sealed|open|abstract)\b",
                                               _text(modifiers, data)) is not None


def _kt_field(declaration, owner, body, root, data, old_root, old_data, local_types, marker):
    if declaration.type != "property_declaration":
        return None
    modifiers = next((c for c in declaration.children if c.type == "modifiers"), None)
    if modifiers is None or _text(modifiers, data).split() != ["private"]:
        return None
    if not any(c.type == "val" for c in declaration.children):
        return None
    if any(c.type in ("=", "property_delegate", "getter", "setter") for c in declaration.children):
        return None
    variable = next((c for c in declaration.named_children if c.type == "variable_declaration"), None)
    if variable is None:
        return None
    named = variable.named_children
    if len(named) != 2 or named[0].type != "identifier" or named[1].type != "user_type":
        return None
    if _text(named[1], data) != "Boolean":
        return None
    name_node = named[0]
    name = _text(name_node, data)
    members = list(body.named_children)
    position = next(i for i, m in enumerate(members) if m.id == declaration.id)
    writes = []
    for i, member in enumerate(members):
        if member.type != "anonymous_initializer":
            continue
        block = next((c for c in member.named_children if c.type == "block"), None)
        if block is None:
            return None
        statements = [c for c in block.named_children if c.type not in ("line_comment", "block_comment")]
        for j, statement in enumerate(statements):
            if _assignment_literal(statement, name, data, False):
                writes.append((i, member, j, statements, statement))
    if len(writes) != 1:
        return None
    index, initializer, j, statements, statement = writes[0]
    if index < position:
        return None
    literal = _assignment_literal(statement, name, data, False)
    if not _customer_wrote_other(old_root, old_data, name, literal, False):
        return None
    target = statement.child_by_field_name("left")
    template = re.compile(rf"\${re.escape(name)}(?!\w)")
    for node in _walk(root):
        if node.type in ("string_content", "string_literal") and (
            template.search(_text(node, data)) or (node.type == "string_literal" and _word(name).search(_text(node, data)))
        ):
            return None
        if node.type != "identifier" or _text(node, data) != name:
            continue
        if node.id == name_node.id or (target is not None and target.start_byte <= node.start_byte < target.end_byte):
            continue
        if not _kt_read(node, data):
            return None
    methods = {
        _text(m.child_by_field_name("name"), data)
        for m in members
        if m.type == "function_declaration" and m.child_by_field_name("name") is not None
    } | {
        _text(v.named_children[0], data)
        for m in members
        if m.type == "property_declaration"
        for v in m.named_children
        if v.type == "variable_declaration" and v.named_children
    }
    earlier: list[Node] = []
    for member in members[:index]:
        if member.type == "property_declaration":
            earlier.extend(c for c in member.named_children if c.type not in ("modifiers", "variable_declaration"))
        elif member.type == "anonymous_initializer":
            earlier.append(member)
    earlier.extend(statements[:j])
    if not _inert(earlier, data, methods, local_types, False, set()):
        return None
    # A member property read early may run code that reads the field: a
    # custom getter, a delegate (`by lazy`), or a value that is a function.
    risky = _kt_risky_properties(members, data) | {
        _text(m.child_by_field_name("name"), data)
        for m in members
        if m.type == "function_declaration" and m.child_by_field_name("name") is not None
    }
    if any(n.type == "identifier" and _text(n, data) in risky | {name} for e in earlier for n in _walk(e)):
        return None
    if len(statements) == 1:
        span = _own_lines(initializer, data)
    else:
        span = _own_lines(statement, data)
    if span is None:
        return None
    replacement = f" = {PARAM_PREFIX}{marker}({literal})".encode("utf-8")
    return name, [(span[0], span[1], b""), (named[1].end_byte, named[1].end_byte, replacement)]


def _kt_risky_properties(members: list[Node], data: bytes) -> set[str]:
    found = set()
    for member in members:
        if member.type != "property_declaration":
            continue
        variable = next((c for c in member.named_children if c.type == "variable_declaration"), None)
        if variable is None or not variable.named_children:
            continue
        name = _text(variable.named_children[0], data)
        if any(c.type in ("getter", "setter", "property_delegate") for c in member.children):
            found.add(name)
            continue
        kinds = {n.type for n in _walk(member)}
        if kinds & {"lambda_literal", "anonymous_function", "callable_reference", "object_literal", "function_type"}:
            found.add(name)
    return found


def _kt_read(node: Node, data: bytes) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "navigation_expression":
        named = parent.named_children
        if named[-1].id != node.id:
            return True  # `allowScottish.not()`: a read, as a receiver
        receiver = named[0]
        # `!this.f` parses as `(!this).f` in this grammar.
        while receiver.type == "unary_expression" and receiver.children and receiver.children[0].type == "!":
            receiver = receiver.named_children[-1]
        return len(named) == 2 and receiver.type == "this_expression" and _kt_value(parent)
    if parent.type in (
        "variable_declaration", "parameter", "class_parameter", "lambda_parameters",
        "function_declaration", "class_declaration", "import", "label", "user_type",
        "type_alias", "object_declaration", "property_declaration",
    ):
        return False
    if parent.type == "value_argument" and any(c.type == "=" for c in parent.children) and parent.named_children[0].id == node.id:
        return False
    if parent.type == "call_expression" and parent.named_children[0].id == node.id:
        return False
    return _kt_value(node)


def _kt_value(node: Node) -> bool:
    """Not the target of an assignment (a `val` has none but this one)."""
    parent = node.parent
    if parent is None:
        return False
    return not (parent.type == "assignment" and _field(node) == "left")
