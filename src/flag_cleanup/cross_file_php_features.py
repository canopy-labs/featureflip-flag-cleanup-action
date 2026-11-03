"""Laravel Pennant class-based features, as a declaration kind of the PHP profile.

Pennant has two styles of feature. A string one is named by the string, and
the seed rules read it like any key. A class one is passed as its class,
``Feature::active(Voucher::class)``, and Pennant names it the way its
``resolveFeatureName`` does: the ``#[Name('...')]`` attribute when the class
has one, else ``$instance->name ?? $class``, the ``$name`` property of an
instance made by the container, else the fully qualified class name. Only the
first two can be a key (a key holds no backslash), and a class named by one of
them is the flag's declaration in another file, read as ``Owner::class``: a
third kind beside a constant and an enum case (:data:`xk.FEATURE_CLASS`).

A class is proven to be named by the key when it is a top-level, concrete
``class`` in a file that parses whole, declares ``resolve`` or ``__invoke``
itself (without one Pennant does not resolve the class to its name), and
either carries Pennant's ``Name`` attribute holding exactly the key, or has
no such attribute and a public, non-static ``$name`` whose default is exactly
the key and which nothing in the class can change: no parent class, no trait,
no promoted ``$name`` and no ``->name`` anywhere in its body. A class that
holds the key in either place and fails the proof is reported where it is
declared (``feature-unproven``) and offered as nothing.

A mention in another file is proven only as ``Owner::class``, the whole first
argument of a call to a configured accessor (``Feature::active``,
``feature``, ``Feature::for($u)->active``). The SDK's own ``boolVariation``
would be handed the class's name, not the key, so it is not a read of it.
Every other place that names the class is refused and keeps it: ``new``,
``extends``, a type, an attribute, a static call, ``Feature::define`` or
``Feature::activate`` taking it, ``Owner::class`` held in a variable, and
text spelling its qualified name, ``Owner::`` or its file's name. A ``use``
import is not a mention: the fold strands it, and an import of a class that
no longer exists is never loaded.

The class is the whole point of its file, so once every mention is a proven
read the run deletes the file (:func:`deletable_alone`), never just the class.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import cross_file_php as cp

#: Reported at a class that holds the key as its feature name and cannot be
#: proven to be named by it.
FEATURE_UNPROVEN = "feature-unproven"
#: ``bindings`` kind of a feature class whose file the run deleted, with
#: ``Name::class`` as its text.
REMOVED_FEATURE_CLASS = "removed feature class"

#: Pennant's ``#[Name]`` attribute, as :func:`cross_file_php._resolve_class`
#: spells it.
_NAME_ATTRIBUTE = "laravel\\pennant\\attributes\\name"
#: Methods Pennant calls on a feature class to resolve it.
_RESOLVERS = frozenset({"resolve", "__invoke"})
#: What can stand in a file beside the class it is deleted for.
_FILE_FURNITURE = frozenset(
    {"php_tag", "namespace_definition", "namespace_use_declaration", "declare_statement", "comment", "text_interpolation"}
)
_ACCESS = frozenset(
    {"member_access_expression", "nullsafe_member_access_expression", "member_call_expression", "nullsafe_member_call_expression"}
)


def _attribute_names(cls: Node, sections, imports) -> list[tuple[Node, str | None]]:
    """``(attribute, resolved name)`` for every attribute on ``cls``."""
    found: list[tuple[Node, str | None]] = []
    for group_list in (c for c in cls.named_children if c.type == "attribute_list"):
        for attribute in (n for n in xk.walk(group_list) if n.type == "attribute"):
            written = next((c for c in attribute.named_children if c.type in cp._CLASS_NAMES), None)
            found.append((attribute, cp._resolve_class(sections, imports, written) if written is not None else None))
    return found


def _name_property(body: Node) -> tuple[Node, Node] | None:
    """``(property_declaration, property_element)`` of the class's own ``$name``."""
    for declaration in (c for c in body.named_children if c.type == "property_declaration"):
        for element in (c for c in declaration.named_children if c.type == "property_element"):
            variable = next((c for c in element.named_children if c.type == "variable_name"), None)
            name = next((c for c in variable.named_children if c.type == "name"), None) if variable else None
            if name is not None and cp._text(name) == "name":
                return declaration, element
    return None


def _default(element: Node) -> Node | None:
    """The default value of a ``property_element``: what follows its ``=``."""
    seen = False
    for child in element.children:
        if seen and child.is_named:
            return child
        if child.type == "=":
            seen = True
    return None


def _public_instance(declaration: Node) -> bool:
    """Whether a property declaration is public and per instance: what
    ``$instance->name`` reads from outside the class."""
    visibility = [c for c in declaration.children if c.type == "visibility_modifier"]
    if any(c.type in ("static_modifier", "readonly_modifier") for c in declaration.children):
        return False
    if not visibility:
        return any(c.type == "var_modifier" for c in declaration.children)
    return all(cp._text(v).lower() == "public" for v in visibility)


def _methods(body: Node) -> list[Node]:
    return [c for c in body.named_children if c.type == "method_declaration"]


def _method_name(method: Node) -> str:
    name = method.child_by_field_name("name")
    return cp._text(name).lower() if name is not None else ""


def _changes_name(body: Node) -> bool:
    """Whether anything in the class body could give ``$name`` another value:
    ``$this->name`` read or written, a dynamic ``$this->{...}``, ``unset`` or a
    promoted ``$name`` parameter. Over-counting only reports the class
    unproven."""
    for node in xk.walk(body):
        if node.type in _ACCESS:
            target = node.child_by_field_name("object")
            if target is None or cp._text(target) != "$this":
                continue
            name = node.child_by_field_name("name")
            if name is None or name.type != "name" or cp._text(name) == "name":
                return True
        elif node.type == "property_promotion_parameter":
            variable = node.child_by_field_name("name")
            if variable is not None and cp._text(variable) == "$name":
                return True
        elif node.type == "unset_statement":
            return True
    return False


def _proof(cls: Node, sections, imports, flag_key: str) -> tuple[bool, Node] | None:
    """``(proven, node holding the key)`` for a class that holds ``flag_key``
    as its feature name, or None for one that does not hold it there."""
    body = cls.child_by_field_name("body")
    if body is None:
        return None
    held = xk.key_token(flag_key)
    attributes = _attribute_names(cls, sections, imports)
    pennant = [a for a, fqn in attributes if fqn == _NAME_ATTRIBUTE]
    unresolved = [a for a, fqn in attributes if fqn is None]
    if pennant or unresolved:
        holding = [a for a in pennant + unresolved if held.search(cp._text(a))]
        if not holding:
            return None
        node = holding[0]
        values = cp._arguments(node)
        literal = cp._literal(values[0]) if len(values) == 1 else None
        named = node.child_by_field_name("parameters")
        if literal is None and named is not None:
            # `#[Name(name: 'k')]`: the one named argument.
            arguments = [a for a in named.named_children if a.type == "argument"]
            if len(arguments) == 1:
                value = arguments[0].named_children[-1] if arguments[0].named_children else None
                literal = cp._literal(value)
        proven = len(pennant) == 1 and not unresolved and literal == flag_key
        return proven and _resolves(body), node
    found = _name_property(body)
    if found is None:
        promoted = next(
            (
                n
                for n in xk.walk(body)
                if n.type == "property_promotion_parameter"
                and n.child_by_field_name("name") is not None
                and cp._text(n.child_by_field_name("name")) == "$name"
                and held.search(cp._text(n))
            ),
            None,
        )
        return (False, promoted) if promoted is not None else None
    declaration, element = found
    value = _default(element)
    if value is None or not held.search(cp._text(value)):
        return None
    proven = (
        cp._literal(value) == flag_key
        and _public_instance(declaration)
        and not _changes_name(body)
        and _resolves(body)
    )
    return proven, declaration


def _resolves(body: Node) -> bool:
    return any(_method_name(m) in _RESOLVERS for m in _methods(body))


def _concrete(cls: Node) -> bool:
    """A class Pennant can make: not abstract, extending nothing and using no
    trait (a parent's or a trait's constructor could set ``$name``)."""
    if any(c.type == "abstract_modifier" for c in cls.children):
        return False
    if any(c.type == "base_clause" for c in cls.named_children):
        return False
    body = cls.child_by_field_name("body")
    return body is not None and not any(c.type == "use_declaration" for c in body.named_children)


def classes(
    path: str, root: Node, source: str, flag_key: str
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """The feature classes ``flag_key`` names, and a refusal at each class
    that holds it as its feature name but cannot be proven to be named by it."""
    sections = cp._sections(root)
    imports = cp._imports(root)
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for cls in (n for n in xk.walk(root) if n.type == "class_declaration"):
        name = cls.child_by_field_name("name")
        if name is None:
            continue
        verdict = _proof(cls, sections, imports, flag_key)
        if verdict is None:
            continue
        proven, at = verdict
        section = cp._section_at(cp._sections(root), cls.start_byte)
        if (
            not proven
            or root.has_error
            or section is None
            or not cp._top_level(cls)
            or not _concrete(cls)
        ):
            refused.append(xk.refusal(path, at, FEATURE_UNPROVEN, source))
            continue
        short = cp._text(name)
        found.append(
            xk.KeyDeclaration(path, short, short.lower(), section.namespace, kind=xk.FEATURE_CLASS)
        )
    return found, refused


def deletable_alone(source: str, decl: xk.KeyDeclaration) -> bool:
    """Whether the class is all its file holds: beside it only the opening
    tag, the namespace, ``use`` imports, ``declare`` and comments."""
    root = cp.syntax._parse(source, "php")
    if root.has_error:
        return False
    classes_here = 0
    for child in root.named_children:
        if child.type == "namespace_definition" and child.child_by_field_name("body") is not None:
            members = child.child_by_field_name("body").named_children
        else:
            members = [child]
        for member in members:
            if member.type == "class_declaration":
                name = member.child_by_field_name("name")
                if name is None or cp._text(name) != decl.name:
                    return False
                classes_here += 1
            elif member.type not in _FILE_FURNITURE:
                return False
            elif member.type == "text_interpolation" and any(
                c.type == "text" for c in member.named_children
            ):
                return False  # markup the file outputs
    return classes_here == 1


# --- mentions ---------------------------------------------------------------------


def _spellings(reader: cp._Reader, decl: xk.KeyDeclaration) -> set[str]:
    """The short names, lowercased, through which ``reader`` can name the
    class: its own, and every alias a ``use`` of it binds."""
    fqn = cp._fqn(decl)
    names = {decl.owner or ""}
    for imported in reader.imports:
        if imported.kind == "class" and imported.target.lower().lstrip("\\") == fqn:
            names.add(imported.alias.lower())
    return names


def _class_node(name: Node) -> Node | None:
    """The class name ``name`` ends, or None when it is a namespace segment."""
    parent = name.parent
    if parent is None:
        return name
    if parent.type == "namespace_name":
        return None
    if parent.type in ("qualified_name", "relative_name"):
        last = parent.named_children[-1] if parent.named_children else None
        return parent if last is not None and last.id == name.id else None
    return name


def _in_import(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type in ("namespace_use_clause", "namespace_use_declaration", "namespace_definition"):
            return True
        if current.type not in ("qualified_name", "relative_name", "namespace_name", "namespace_use_group"):
            return False
        current = current.parent
    return False


def _accessor_read(member: Node, accessors: frozenset[str]) -> bool:
    """Whether ``Owner::class`` (``member`` is its ``class``) is the whole first
    argument of a configured accessor's call: Pennant resolves the class to
    its feature name, and the SDK would be handed the class's name instead."""
    if not cp._read_call(member, accessors):
        return False
    call = member.parent.parent.parent.parent  # access -> argument -> arguments -> call
    callee = call.child_by_field_name("name") or call.child_by_field_name("function")
    return callee is not None and cp._text(callee) in accessors


def _judge(
    reader: cp._Reader, node: Node, decl: xk.KeyDeclaration, accessors: frozenset[str]
) -> tuple[xk.KeyReference | str | None, Node | None]:
    """A proven read, a refusal reason, or None for a name that is not the
    class; with the class name as written when it stands where a read would."""
    owner = _class_node(node)
    if owner is None or _in_import(owner):
        return None, None
    parent = owner.parent
    if parent is None:
        return None, None
    field = cp._field(parent, owner)
    if parent.type == "class_constant_access_expression":
        children = parent.named_children
        if children[0].id != owner.id:
            return None, None  # a constant that happens to share the name
        member = children[-1]
        fqn = reader.resolve(owner)
        ours = fqn == cp._fqn(decl)
        reading = cp._text(member).lower() == "class" and _accessor_read(member, accessors)
        if fqn is None:
            return xk.IMPORT_UNRESOLVED, None
        if not ours:
            return None, (owner if reading else None)
        if not reading:
            return xk.NOT_A_READ, None
        return xk.KeyReference("", cp._text(owner), "class"), owner
    if field == "name" or parent.type in ("variable_name", "named_label_statement", "goto_statement"):
        return None, None  # a member, method, function, property or label of that name
    if parent.type == "function_call_expression" and field == "function":
        return None, None
    if cp._position(owner) == "constant":
        return None, None  # a bare constant: another symbol table
    fqn = reader.resolve(owner)
    if fqn is None:
        return xk.IMPORT_UNRESOLVED, None
    return (xk.UNSUPPORTED_FORM if fqn == cp._fqn(decl) else None), None


def _text_patterns(decl: xk.KeyDeclaration, spellings: set[str]) -> re.Pattern[str]:
    """Text that names the class: its qualified name with one or two
    backslashes between segments, ``Name::`` for any spelling, or its file."""
    parts = cp._fqn(decl).split("\\")
    qualified = r"\\{1,2}".join(re.escape(p) for p in parts)
    fetch = "|".join(re.escape(s) for s in sorted(spellings))
    basename = re.escape(Path(decl.path).name)
    return re.compile(
        rf"(?<![\w\\])\\{{0,2}}{qualified}(?![\w])|(?<![\w\\$])(?:{fetch})\s*::|(?<![\w.-]){basename}(?![\w])",
        re.IGNORECASE,
    )


def references(
    reader: cp._Reader,
    path: str,
    source: str,
    decl: xk.KeyDeclaration,
    accessors: frozenset[str],
    *,
    vendored: bool,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    """Every mention of the feature class ``decl`` in one reader file."""
    spellings = _spellings(reader, decl)
    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []
    proven: set[str] = set()
    skipped: list[tuple[Node, str]] = []
    for text, nodes in reader.names.items():
        if text.lower() not in spellings:
            continue
        for node in nodes:
            verdict, at = _judge(reader, node, decl, accessors)
            if verdict is None:
                if at is not None:
                    skipped.append((at, cp._text(at)))
                continue
            if isinstance(verdict, xk.KeyReference):
                if vendored:
                    refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source))
                    continue
                reads.append(xk.KeyReference(path, verdict.owner, verdict.member))
                proven.add(verdict.owner or "")
            else:
                refused.append(xk.refusal(path, node, verdict, source))
    # The reader rules match the owner by TEXT across the whole file: another
    # class spelled like a proven read would be folded with it.
    for node, spelled in skipped:
        if spelled in proven:
            refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source))
    pattern = _text_patterns(decl, spellings)
    lines = source.splitlines()
    # A double-quoted string splits its text around each escape, so the
    # qualified name `"App\\Features\\Voucher"` is matched on the whole.
    quoted = [n for n in xk.walk(reader.root) if n.type in ("encapsed_string", "heredoc")]
    for node in [*reader.texts, *quoted]:
        text = cp._text(node)
        hit = pattern.search(text)
        if hit is not None:
            line = node.start_point[0] + text.count("\n", 0, hit.start())
            quoted = lines[line].strip() if line < len(lines) else ""
            refused.append(xk.Refusal(path, line + 1, xk.UNSUPPORTED_FORM, quoted))
    return reads, refused


def guest_mentions(path: str, source: str, decl: xk.KeyDeclaration) -> list[xk.Refusal]:
    """A file outside PHP that names the class by its qualified name (a
    Symfony service id, a YAML or Twig string) keeps it: never a read."""
    parts = cp._fqn(decl).split("\\")
    qualified = re.compile(
        r"(?<![\w\\])\\{0,2}" + r"\\{1,2}".join(re.escape(p) for p in parts) + r"(?![\w])", re.IGNORECASE
    )
    hit = qualified.search(source)
    if hit is None:
        return []
    line = source.count("\n", 0, hit.start()) + 1
    lines = source.splitlines()
    return [xk.Refusal(path, line, xk.UNSUPPORTED_FORM, lines[line - 1].strip() if line <= len(lines) else "")]
