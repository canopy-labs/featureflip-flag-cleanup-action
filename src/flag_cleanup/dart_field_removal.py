"""Find, and remove where it is safe, a Dart private field the fold left unread.

``final FeatureClient _client;`` is private to its LIBRARY. When every read of
it was a flag read the run folded, the field stays with nothing reading it, and
``dart analyze`` reports ``unused_field``: a warning, and a non-zero exit, so a
project that gates on the analyzer fails the pull request.

:func:`unread_fields` finds the field with the same differential as
:mod:`flag_cleanup.ts_unread_members`: **a field is reported only if it was
read BEFORE the fold and is read nowhere AFTER it**, so a field the customer
had already left unread is not blamed on this run. Reads are counted across
the whole file, since any class or function in the library may read it, and
the count errs towards "still read": every spelling of the name counts,
string interpolation (``'$_client'``) included, except the three that only
write it: the field's own declaration, a ``this._client`` constructor
parameter, and the left side of an initializer-list entry or of a plain
``=``. A mistake costs a missed note.

:func:`plan` proves the field can go. Deleting it is safe when nothing the
deletion skips could do work, and nothing else in the library spells it:

- **no other spelling but a constructor's.** The field is unread, so what is
  left can only write it: a ``this._client`` parameter or an initializer-list
  entry, which go with it when :mod:`flag_cleanup.ctor_param_dart` proves the
  constructor and its callers can change, or an assignment, whose right side
  may do work.
- **an initializer that never runs, or does no work**: a ``late`` or
  ``static`` field's initializer runs on the field's first read, which no
  longer happens. Any other field's runs when the object is built, so it must
  be absent or constant: a literal, a ``const`` expression, a collection
  literal built of those (``final _retries = 3;``, ``final _ids = <int>[];``),
  a call, with constant arguments, of a ``const`` constructor or of one
  that runs no code of its own (``final _client = FeatureClient();``, see
  :func:`flag_cleanup.ctor_param_dart.const_construction`), or a bare name
  reading a top-level or static variable of the file whose own initializer
  is inert, as the initializer or as such a constructor's argument
  (``final _flags = Flags(client);`` under ``final client = Client();``).
  ``make()``, ``Foo.bar`` (a getter, or a static whose initializer runs on
  first access) and ``'$x'`` (a ``toString()``) may do work.
- **nothing else left unused.** Every name the declaration spells (its type,
  a ``const _Config()``, a ``late`` field's ``_make()``) must also be spelled
  outside every private field declaration of the file, or be one of
  ``dart:core``'s common types: otherwise deleting it would leave an import,
  a private class or a private function unused, which ``dart analyze``
  reports in turn. The same holds for what else goes from the file: a
  parameter's type and default, an argument. A name an import brings is the
  exception when the import can go too: one of a library in the checkout,
  or of the Featureflip Dart SDK's audited libraries, is judged by the
  stranded-binding pass that runs next, and an unprefixed
  ``show`` import of any other library goes with the field when every name
  it shows does (see :func:`_import_strand`).
- **one library, one declaration.** A file with a ``part`` or ``part of``
  directive is refused: another part may read the field. So is a class
  declared twice in its file, a field declared beside others
  (``int _a = 1, _b = 2;``), and a name also declared as a getter, setter or
  method.

Refused as well: an annotated field (``@visibleForTesting``, or an annotation
a code generator reads), and an ``external`` or ``abstract`` one.

The field is marked as a method of the sentinel's name, its own ``;`` with it,
and the engine deletes it by identity (see :mod:`flag_cleanup.ctor_param_model`).
A comment on the field's own line goes with it, and so do the ``///`` lines
directly above, which document it, with any ``//`` line between them and the
field; a ``//`` comment above them may head a group, and stays.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import ctor_param_dart, dart_namespace
from flag_cleanup.ctor_param_model import IDENT, Refusal, Removal, Site
from flag_cleanup.unused import _parse

#: Nodes whose text, when it spells a field's name, may be a read of it.
_NAMES = frozenset({"identifier", "identifier_dollar_escaped"})
#: Nodes that spell a name a declaration or import provides.
_SPELLINGS = frozenset({"identifier", "identifier_dollar_escaped", "type_identifier"})
_TYPE_DECLARATIONS = frozenset(
    {
        "class_definition",
        "enum_declaration",
        "mixin_declaration",
        "extension_declaration",
        "extension_type_declaration",
        "type_alias",
    }
)
_TOP_SIGNATURES = frozenset({"function_signature", "getter_signature", "setter_signature"})
_SIGNATURES = frozenset({"function_signature", "getter_signature", "setter_signature"})
_DECLARATORS = frozenset({"initialized_identifier", "static_final_declaration"})
_DECLARATOR_LISTS = frozenset({"initialized_identifier_list", "static_final_declaration_list"})
_INERT_LEAVES = frozenset(
    {
        "decimal_integer_literal",
        "decimal_floating_point_literal",
        "hex_integer_literal",
        "true",
        "false",
        "null_literal",
        "symbol_literal",
    }
)
_COLLECTIONS = frozenset({"list_literal", "set_or_map_literal"})
#: ``dart:core`` types no import provides, so deleting their last spelling
#: strands nothing.
_CORE = frozenset(
    {
        "bool", "int", "double", "num", "String", "Object", "dynamic", "void",
        "Null", "Never", "List", "Map", "Set", "Iterable", "Duration",
        "DateTime", "Future", "Stream", "Function", "Type", "Symbol", "Record",
        "Uri", "RegExp", "BigInt",
    }
)
#: Modifiers that make deleting the field more than deleting a value.
_REFUSED_MODIFIERS = {"external": "an `external` field", "abstract": "an `abstract` field"}


def _walk(node: Node) -> Iterator[Node]:
    """Pre-order, in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _row(node: Node) -> int:
    return node.start_point[0]


def _has_parts(root: Node) -> bool:
    return any(c.type in ("part_directive", "part_of_directive") for c in root.named_children)


def _classes(root: Node) -> list[Node]:
    return [n for n in _walk(root) if n.type == "class_definition"]


def _class_name(cls: Node) -> str | None:
    name = next((c for c in cls.named_children if c.type == "identifier"), None)
    return _text(name) if name is not None else None


def _fields(body: Node) -> list[tuple[Node, list[Node]]]:
    """``(declaration, [name nodes])`` for each field declaration of ``body``."""
    found: list[tuple[Node, list[Node]]] = []
    for member in body.named_children:
        if member.type != "declaration":
            continue
        names: list[Node] = []
        for child in member.named_children:
            if child.type in _DECLARATOR_LISTS:
                names += [
                    d.named_children[0]
                    for d in child.named_children
                    if d.type in _DECLARATORS and d.named_children
                ]
            elif child.type == "identifier_list":
                names += [c for c in child.named_children if c.type == "identifier"]
            elif child.type == "identifier":
                names.append(child)  # `external int _c;`
        if names:
            found.append((member, names))
    return found


def _declared_names(root: Node) -> dict[str, list[Node]]:
    """Every field name declared in a class of ``root``, with its name nodes."""
    declared: dict[str, list[Node]] = {}
    for cls in _classes(root):
        body = next((c for c in cls.named_children if c.type == "class_body"), None)
        for _, names in _fields(body) if body is not None else ():
            for name in names:
                declared.setdefault(_text(name), []).append(name)
    return declared


def _callable_names(root: Node) -> set[str]:
    """Every name a getter, setter, method or function of ``root`` declares."""
    return {
        _text(name)
        for node in _walk(root)
        if node.type in _SIGNATURES
        for name in node.named_children[:2]
        if name.type == "identifier"
    }


def _write_kind(node: Node) -> str | None:
    """How ``node``, a spelling of a field's name, writes the field without
    reading it, or ``None`` when it may read it."""
    parent = node.parent
    if parent is None:
        return None
    if parent.type == "constructor_param":
        return "parameter"
    if parent.type == "field_initializer":
        # `_x = v` and `this._x = v`: the name is what comes right before `=`.
        equals = next((c for c in parent.children if c.type == "="), None)
        if equals is not None and node.next_sibling == equals:
            return "initializer"
        return None
    # `_x = v` and `this._x = v`: the name is the last thing on the left.
    target = parent
    if target.type == "unconditional_assignable_selector":
        target = target.parent
    if target is None or target.type != "assignable_expression" or target.named_children[-1] not in (
        node,
        parent,
    ):
        return None
    assignment = target.parent
    if (
        assignment is not None
        and assignment.type == "assignment_expression"
        and assignment.named_children[0] == target
        and any(c.type == "=" for c in assignment.children)
    ):
        return "assignment"
    return None


def _reads(root: Node, names: set[str], declared: set[tuple[int, int]]) -> dict[str, int]:
    counts = dict.fromkeys(names, 0)
    for node in _walk(root):
        if node.type not in _NAMES:
            continue
        text = _text(node)
        if text not in counts or (node.start_byte, node.end_byte) in declared:
            continue
        if _write_kind(node) is None:
            counts[text] += 1
    return counts


def _members(source: str) -> dict[tuple[str, str], int] | None:
    """``(class, field) -> reads`` for each private field of a named class in
    ``source`` whose name is declared once in the file. ``None`` when the file
    does not parse."""
    root = _parse(source, "dart")
    if root.has_error:
        return None
    callables = _callable_names(root)
    declared = _declared_names(root)
    owners: dict[str, str] = {}
    seen: set[str] = set()
    duplicated: set[str] = set()
    for cls in _classes(root):
        class_name = _class_name(cls)
        body = next((c for c in cls.named_children if c.type == "class_body"), None)
        if class_name is None or body is None:
            continue
        if class_name in seen:
            duplicated.add(class_name)
        seen.add(class_name)
        for _, names in _fields(body):
            for name in names:
                text = _text(name)
                if text.startswith("_") and len(declared[text]) == 1 and text not in callables:
                    owners[text] = class_name
    spans = {(n.start_byte, n.end_byte) for nodes in declared.values() for n in nodes}
    reads = _reads(root, set(owners), spans)
    return {
        (owner, name): reads[name] for name, owner in owners.items() if owner not in duplicated
    }


def unread_fields(before: str, after: str) -> list[str]:
    """Each private field read in ``before`` and not in ``after``.

    Returned as ``Class._field`` (``Svc._client``), in the order they appear
    in ``after``.
    """
    old = _members(before)
    new = _members(after)
    if old is None or new is None:
        return []
    return [
        f"{class_name}.{name}"
        for (class_name, name), reads in new.items()
        if reads == 0 and old.get((class_name, name), 0) > 0
    ]


def _inert(nodes: list[Node]) -> bool:
    """Whether evaluating the initializer ``nodes`` (a postfix chain is a run
    of siblings) can do no work."""
    if len(nodes) != 1:
        return False  # a call, a property access, a cascade
    node = nodes[0]
    if node.type in _INERT_LEAVES:
        return True
    if node.type == "string_literal":
        return not any(c.type == "template_substitution" for c in node.named_children)
    if node.type == "const_object_expression":
        return True  # every argument of a `const` constructor is constant
    if node.type == "unary_expression":
        operator = node.named_children[0] if node.named_children else None
        return (
            operator is not None
            and operator.type == "prefix_operator"
            and _text(operator) == "-"
            and len(node.named_children) == 2
            and node.named_children[1].type in _INERT_LEAVES
        )
    if node.type in _COLLECTIONS:
        items = [c for c in node.named_children if c.type not in ("comment", "type_arguments")]
        if any(c.type == "const_builtin" for c in items):
            return True  # a constant context
        for item in items:
            if item.type == "pair":
                if not all(_inert([c]) for c in item.named_children if c.type != "comment"):
                    return False
            elif not _inert([item]):
                return False  # a spread, a collection `if` or `for`, a name
        return True
    return False


def _initializer(declaration: Node) -> list[Node]:
    """The initializer of ``declaration``'s one declarator: the nodes after its
    ``=``, comments aside."""
    for child in declaration.named_children:
        if child.type in _DECLARATOR_LISTS:
            declarator = child.named_children[0]
            after = False
            nodes: list[Node] = []
            for part in declarator.children:
                if after and part.is_named and part.type != "comment":
                    nodes.append(part)
                after = after or part.type == "="
            return nodes
    return []


def _span(declaration: Node, siblings: list[Node]) -> tuple[int, int]:
    """``declaration``'s bytes, its own ``;``, a comment on its last line, and
    the ``///`` documentation comment directly above it."""
    position = siblings.index(declaration)
    start, end = declaration.start_byte, declaration.end_byte
    last_row = declaration.end_point[0]
    following = siblings[position + 1 :]
    if following and following[0].type == ";":
        end = following[0].end_byte
        last_row = following[0].end_point[0]
        following = following[1:]
    if (
        following
        and following[0].type in ("comment", "documentation_comment")
        and _row(following[0]) == last_row
    ):
        end = following[0].end_byte
    # The run of whole-line comments directly above, up to its topmost `///`
    # line: a `//` line between the documentation and the field goes too,
    # since what it sat between is gone.
    top_row = _row(declaration)
    while position > 0:
        before = siblings[position - 1]
        if (
            before.type not in ("comment", "documentation_comment")
            or before.end_point[0] != top_row - 1
            or (position >= 2 and siblings[position - 2].end_point[0] >= _row(before))
        ):
            break
        top_row, position = _row(before), position - 1
        if before.type == "documentation_comment":
            start = before.start_byte
    return start, end


def _own_declarations(root: Node, name: str) -> list[tuple[int, int]]:
    """The spans of every top-level declaration of ``name`` in ``root``, with
    a function's or getter's body: a private name spelled only there is
    unused."""
    spans: list[tuple[int, int]] = []
    children = root.named_children
    for index, node in enumerate(children):
        named = node.named_children
        if node.type in _TOP_SIGNATURES:
            label = next((c for c in named if c.type == "identifier"), None)
            if label is not None and _text(label) == name:
                end = node.end_byte
                if index + 1 < len(children) and children[index + 1].type == "function_body":
                    end = children[index + 1].end_byte
                spans.append((node.start_byte, end))
        elif node.type in _DECLARATOR_LISTS:  # a top-level variable
            spans += [
                (d.start_byte, d.end_byte)
                for d in named
                if d.type in _DECLARATORS and d.named_children and _text(d.named_children[0]) == name
            ]
        elif node.type in _TYPE_DECLARATIONS:
            label = next((c for c in named if c.type in ("identifier", "type_identifier")), None)
            if label is not None and _text(label) == name:
                spans.append((node.start_byte, node.end_byte))
    return spans


def _import_strand(
    root: Node,
    name: str,
    stranded: dict[str, list[Node]],
    kept: set[str],
    resolve: Callable[[str], object | None],
) -> list[tuple[int, int]] | None:
    """What deleting the last spelling of ``name``, a name an import brings,
    takes with it: ``[]`` when an import of a library in the checkout
    brings it and the stranded-binding pass that runs after this one will
    either delete that import or find it still used; the spans of the
    directives to delete with the field; ``None`` when no import provably
    brings it, or one that does may still bring something else. ``kept`` is
    every name spelled outside what this removal deletes and the file's
    directives, other private fields included.

    That pass offers an import with no combinator, only ``hide`` ones, or one
    ``show``, and keeps one while any extension member of its library is
    spelled, a call of it or not; so an import of the checkout that brings
    nothing else spelled is accepted only when no such member is.

    The resolver also knows the Featureflip Dart SDK's libraries from an
    audited table, when the package's lock pins a release that table was
    read from (see :func:`flag_cleanup.dart_namespace.audited_sdk`), so such
    an import is judged the same way. Any other library of a ``package:`` or
    ``dart:`` import outside the checkout cannot be read, so what it brings
    is known only when the import is unprefixed and has one ``show`` list:
    then it brings those names and nothing else, not even an extension it
    does not show. It goes when every name it shows goes with the field,
    each spelled only as a type there, which an extension never is. Any
    other import of such a library might be what brings the name, or
    something an extension call needs, so it keeps the field unless an
    import above already accounts for the name."""
    if name.startswith("_"):
        return None
    known = False
    deleted: list[tuple[int, int]] = []
    for spec in (n for n in _walk(root) if n.type == "import_specification"):
        directive = spec.parent.parent if spec.parent is not None else None
        if directive is None or directive.type != "import_or_export" or any(c.type == "as" for c in spec.children):
            continue  # a prefix brings no bare name
        combinators = [c for c in spec.children if c.type == "combinator"]
        listed = [{_text(x) for x in c.named_children if x.type == "identifier"} for c in combinators]
        kinds = [c.children[0].type if c.children else "" for c in combinators]
        if any(k == "show" and name not in names for k, names in zip(kinds, listed)) or any(
            k == "hide" and name in names for k, names in zip(kinds, listed)
        ):
            continue
        uri = dart_namespace.uri_of(spec)
        namespace = None if uri is None else resolve(uri)
        if namespace is not None:
            if name not in namespace.names:
                continue
            if kinds not in ([], ["show"]) and set(kinds) != {"hide"}:
                return None  # the stranded-binding pass never offers it
            brought = set(listed[0]) if kinds == ["show"] else set(namespace.names).difference(*listed)
            if not brought & kept and set(namespace.members) & kept:
                return None
            known = True
            continue
        if kinds != ["show"]:
            continue  # cannot be read: may bring the name, or may not
        if not all(
            shown in stranded and shown not in kept and all(n.type == "type_identifier" for n in stranded[shown])
            for shown in listed[0]
        ):
            return None
        known = True
        deleted.append((directive.start_byte, directive.end_byte))
    return deleted if known else None


def _stranded(
    root: Node,
    declaration: Node,
    field: Node,
    spans: list[tuple[int, int]],
    skip: set[str],
    resolve: Callable[[str], object | None],
) -> str | list[tuple[int, int]]:
    """A name that ``declaration``, or what else goes in ``spans``, spells and
    that nothing outside the file's private field declarations, its import
    and export directives and ``spans`` does, unless an import brings it
    and goes with it or is left to the stranded-binding pass (see
    :func:`_import_strand`); else the spans of the import directives to
    delete with it. ``spans`` starts with the declaration's; ``skip`` holds
    the names whose every spelling goes with them (the field, a parameter
    that goes)."""
    fields = [
        (member.start_byte, member.end_byte)
        for cls in _classes(root)
        for body in cls.named_children
        if body.type == "class_body"
        for member, names in _fields(body)
        if any(_text(n).startswith("_") for n in names)
    ]
    # A `show` or `hide` entry names what a library brings; it uses nothing.
    directives = [(n.start_byte, n.end_byte) for n in root.named_children if n.type == "import_or_export"]
    fields += spans + directives

    def elsewhere(name: str) -> bool:
        if name.startswith("_"):
            unread = ctor_param_dart.private_variable_unread(root, name, fields)
            if unread is not None:
                return not unread
        skipped = fields + (_own_declarations(root, name) if name.startswith("_") else [])
        return any(
            node.type in _SPELLINGS
            and _text(node) == name
            and not any(start <= node.start_byte < end for start, end in skipped)
            for node in _walk(root)
        )

    spelled = list(_walk(declaration)) + [
        n for n in _walk(root) if any(start <= n.start_byte < end for start, end in spans[1:])
    ]
    stranded: dict[str, list[Node]] = {}
    for node in spelled:
        if node.type not in _SPELLINGS or node == field or node.parent is None:
            continue
        if ctor_param_dart.widget_head(node, spans[1:]):
            continue  # `widget.f`'s getter is inherited, not declared in the file
        if node.parent.type in (
            "label",
            "symbol_literal",
            "unconditional_assignable_selector",
            "conditional_assignable_selector",
            "cascade_selector",
        ):
            continue  # a named argument's name, `#sym`, `.member`: none names a top-level declaration
        name = _text(node)
        if name not in _CORE and name not in skip and not elsewhere(name):
            stranded.setdefault(name, []).append(node)
    kept = {
        _text(node)
        for node in _walk(root)
        if node.type in _SPELLINGS
        and not any(start <= node.start_byte < end for start, end in spans + directives)
    }
    imports: list[tuple[int, int]] = []
    for name in stranded:
        deleted = _import_strand(root, name, stranded, kept, resolve)
        if deleted is None:
            return name
        imports += deleted
    return sorted(set(imports))


def _find(root: Node, class_name: str) -> list[Node]:
    return [c for c in _classes(root) if _class_name(c) == class_name]


def has_field(source: str, class_name: str, field: str) -> bool:
    """Whether ``class_name`` in ``source`` still declares the field."""
    for cls in _find(_parse(source, "dart"), class_name):
        body = next((c for c in cls.named_children if c.type == "class_body"), None)
        if body is not None and any(
            _text(n) == field for _, names in _fields(body) for n in names
        ):
            return True
    return False


def plan(path: str, source: str, label: str, repo_dir: str) -> Removal | Refusal:
    """Prove the private field ``label`` (``Class._field``) of the Dart file at
    ``path``, whose current text is ``source``, can be removed. ``repo_dir`` is
    where the callers of a constructor that loses a parameter are looked for:
    a private field's library is its file once parts are refused."""
    class_name, _, field = label.partition(".")
    if IDENT.fullmatch(class_name) is None:
        return Refusal(label, "a name the deletion rules cannot spell")
    root = _parse(source, "dart")
    if root.has_error:
        return Refusal(label, "the declaring file does not parse")
    if _has_parts(root):
        return Refusal(label, "its library has part files, which may read it")
    classes = _find(root, class_name)
    if len(classes) != 1:
        return Refusal(label, "the class is declared more than once in its file")
    body = next((c for c in classes[0].named_children if c.type == "class_body"), None)
    found = [
        (declaration, names)
        for declaration, names in (_fields(body) if body is not None else ())
        if any(_text(n) == field for n in names)
    ]
    if len(found) != 1 or len(_declared_names(root).get(field, ())) != 1:
        return Refusal(label, "declared more than once in its file")
    declaration, names = found[0]
    if field in _callable_names(root):
        return Refusal(label, "a getter, setter or method shares its name")
    if len(names) != 1:
        return Refusal(label, "declared beside other fields in one declaration")
    siblings = list(body.children)
    previous = [c for c in siblings[: siblings.index(declaration)] if c.type not in ("comment", "documentation_comment")]
    if previous and previous[-1].type == "annotation":
        return Refusal(label, "an annotated field")
    tokens = {c.type for c in declaration.children}
    for modifier, reason in _REFUSED_MODIFIERS.items():
        if modifier in tokens:
            return Refusal(label, reason)
    writes: list[tuple[str, Node]] = []
    for node in _walk(root):
        if node.type in _NAMES and _text(node) == field and node != names[0]:
            kind = _write_kind(node)
            if kind is None:
                return Refusal(label, "its name is still spelled as a read in its library")
            if kind == "assignment":
                return Refusal(label, "still assigned in its library")
            writes.append((kind, node))
    if not ({"late", "static"} & tokens):
        value = _initializer(declaration)
        if value and not _inert(value) and not ctor_param_dart.const_construction(path, root, value, repo_dir):
            text = " ".join(source[value[0].start_byte : value[-1].end_byte].split())[:60]
            if len(value) != 1 or value[0].type != "identifier":
                return Refusal(label, f"an initializer that may do work ({text})")
            reason = ctor_param_dart.inert_initializer_name(path, value[0], root, repo_dir)
            if reason is not None:
                return Refusal(label, f"an initializer that may do work ({text}: {reason})")
    span = _span(declaration, siblings)
    sites = [Site(path, *span, "member")]
    parameter: str | None = None
    if writes:
        edits = ctor_param_dart.plan_constructors(path, classes[0], field, writes)
        if isinstance(edits, str):
            return Refusal(label, edits)
        sites += edits.sites
        parameter = edits.parameter
    if writes and edits.dropped:
        callers = ctor_param_dart.plan_callers(path, root, classes[0], edits.dropped, repo_dir)
        if isinstance(callers, str):
            return Refusal(label, callers)
        sites += callers
    here = str(Path(path).resolve())
    stranded = _stranded(
        root,
        declaration,
        names[0],
        [(s.start, s.end) for s in sites if str(Path(s.path).resolve()) == here],
        {field} | ({parameter} if parameter else set()),
        dart_namespace.resolver(path, repo_dir),
    )
    if isinstance(stranded, str):
        return Refusal(label, f"deleting it would leave `{stranded}` unused")
    sites += [Site(path, start, end, "import") for start, end in stranded]
    return Removal(path, class_name, parameter, label, -1, tuple(sites), False, field)
