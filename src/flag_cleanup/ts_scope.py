"""File-level facts for the TypeScript and JavaScript keyless-wrapper proof.

TS scopes are not modelled. A proof rests instead on a name being bound
exactly once in its file (:func:`bindings`), the rule the TS key-constant
resolution already uses: a second binding anywhere in the file, whatever its
scope, refuses. That is sound without a scope walk, and costs only calls a
scope walk would have proven.

Types are not inferred either. A receiver's class is read off what is
written: a parameter's or field's annotation naming a class, a ``new C()``
initializer, an import of a module-level ``const x = new C()``, or the class
name itself for a static call. Anything else (a union, a generic, ``any``,
an interface, a call's result) has no class here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, cross_file_ts, ts_syntax

TS_EXTENSIONS = (".ts", ".mts", ".cts")
TSX_EXTENSIONS = (".tsx",)
JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")
FAMILY_EXTENSIONS = TS_EXTENSIONS + TSX_EXTENSIONS + JS_EXTENSIONS


def grammar(path: str) -> str:
    """``ts``, ``tsx`` or ``js``: the grammar the engine and Gate 1 parse
    ``path`` with (``js`` is the TSX grammar)."""
    if path.endswith(TSX_EXTENSIONS):
        return "tsx"
    if path.endswith(JS_EXTENSIONS):
        return "js"
    return "ts"


def parse(source: str, path: str) -> Node:
    return ts_syntax._parse(source, grammar(path))


def text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _resolved(path: str) -> str:
    return str(Path(path).resolve())


# --- Bindings --------------------------------------------------------------------

#: Import forms whose bound identifier ``ts_syntax.binding_nodes`` does not
#: see: a namespace import, a default import and ``import x = require()``.
_IMPORT_HOLDERS = ("namespace_import", "import_clause", "import_require_clause")


_TARGET_WRAPPERS = ("parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression", "type_assertion")


def _assigned_identifier(left: Node | None) -> Node | None:
    """The identifier ``(x as any) = …`` and its kin assign to, else ``None``."""
    while left is not None and left.type in _TARGET_WRAPPERS:
        named = [c for c in left.named_children if c.type != "comment"]
        left = named[-1] if left.type == "type_assertion" and named else (named[0] if named else None)
    return left if left is not None and left.type == "identifier" else None


def bindings(root: Node, name: str) -> list[Node]:
    """Every node binding ``name`` anywhere in the file.

    Declarations, parameters and assignment targets come from
    :func:`ts_syntax.binding_nodes`; import aliases and the three import forms
    it does not see are added. In ``import { a as b }`` only ``b`` is bound,
    and ``export { x }`` binds nothing."""
    found: list[Node] = []
    for node in ts_syntax.binding_nodes(root):
        if text(node) != name:
            continue
        parent = node.parent
        if parent is not None and parent.type == "export_specifier":
            continue
        if (
            parent is not None
            and parent.type == "import_specifier"
            and parent.child_by_field_name("alias") is not None
            and parent.child_by_field_name("name") == node
        ):
            continue
        found.append(node)
    for node in cross_file_keys.walk(root):
        if node.type == "import_specifier":
            alias = node.child_by_field_name("alias")
            if alias is not None and text(alias) == name:
                found.append(alias)
        elif node.type in _IMPORT_HOLDERS:
            found += [c for c in node.named_children if c.type == "identifier" and text(c) == name]
        elif node.type in ("assignment_expression", "augmented_assignment_expression"):
            target = _assigned_identifier(node.child_by_field_name("left"))
            if target is not None and text(target) == name and target not in found:
                found.append(target)
    return found


# --- Imports ---------------------------------------------------------------------


@dataclass(frozen=True)
class Import:
    """One name a file binds from another module."""

    local: str
    #: The exported name; ``None`` for a namespace (``import * as ns``,
    #: ``const ns = require(...)``), ``"default"`` for a default import, and
    #: ``"="`` for ``import x = require(...)``.
    imported: str | None
    #: The file the specifier resolves to, absolute; ``None`` when it does not
    #: resolve (a package, an ambiguous path, a config this cannot read).
    module: str | None
    type_only: bool = False
    #: At the top of the file, where a proof needs it.
    top: bool = True


def _imports_of(root: Node, path: str, repo_dir: str) -> list[Import]:
    def resolve(node: Node | None) -> str | None:
        spec = cross_file_ts._string_value(node)
        if spec is None:
            return None
        found = cross_file_ts.resolve_specifier(path, spec, repo_dir)
        return _resolved(found) if found is not None else None

    found: list[Import] = []
    require_global = not bindings(root, "require")
    for node in cross_file_keys.walk(root):
        if node.type == "import_statement":
            top = node.parent is not None and node.parent.type == "program"
            module = resolve(node.child_by_field_name("source"))
            whole_type = any(c.type == "type" for c in node.children)
            for part in node.named_children:
                if part.type == "import_require_clause":
                    name = next((c for c in part.named_children if c.type == "identifier"), None)
                    if name is not None:
                        found.append(Import(text(name), "=", resolve(part.child_by_field_name("source")), whole_type, top))
                    continue
                if part.type != "import_clause":
                    continue
                for clause in part.named_children:
                    if clause.type == "identifier":
                        found.append(Import(text(clause), "default", module, whole_type, top))
                    elif clause.type == "namespace_import":
                        name = next((c for c in clause.named_children if c.type == "identifier"), None)
                        if name is not None:
                            found.append(Import(text(name), None, module, whole_type, top))
                    elif clause.type == "named_imports":
                        for spec in clause.named_children:
                            if spec.type != "import_specifier":
                                continue
                            imported = spec.child_by_field_name("name")
                            alias = spec.child_by_field_name("alias")
                            local = alias if alias is not None else imported
                            if imported is None or local is None:
                                continue
                            type_only = whole_type or any(c.type == "type" for c in spec.children)
                            found.append(Import(text(local), text(imported), module, type_only, top))
        elif node.type == "variable_declarator" and require_global:
            value = node.child_by_field_name("value")
            target = node.child_by_field_name("name")
            if value is None or target is None or value.type != "call_expression":
                continue
            function = value.child_by_field_name("function")
            if function is None or function.type != "identifier" or text(function) != "require":
                continue
            args = value.child_by_field_name("arguments")
            specs = [] if args is None else [c for c in args.named_children if c.type != "comment"]
            if len(specs) != 1:
                continue
            module = resolve(specs[0])
            lexical = node.parent
            top = (
                lexical is not None
                and lexical.type == "lexical_declaration"
                and any(c.type == "const" for c in lexical.children)
                and lexical.parent is not None
                and lexical.parent.type == "program"
            )
            if target.type == "identifier":
                found.append(Import(text(target), None, module, False, top))
            elif target.type == "object_pattern":
                # Only the shorthand `{ f }`: a renaming `{ f: g }` is not
                # needed by the spec's call shapes, and refusing it is safe.
                found += [
                    Import(text(p), text(p), module, False, top)
                    for p in target.named_children
                    if p.type == "shorthand_property_identifier_pattern"
                ]
    return found


def _in_import(node: Node) -> bool:
    """Whether a binding node is made by an import or a require."""
    current = node.parent
    while current is not None and current.type not in ("program", "statement_block"):
        if current.type == "import_statement":
            return True
        if current.type == "variable_declarator":
            value = current.child_by_field_name("value")
            function = value.child_by_field_name("function") if value is not None and value.type == "call_expression" else None
            return function is not None and function.type == "identifier" and text(function) == "require"
        current = current.parent
    return False


# --- Static types ----------------------------------------------------------------


@dataclass(frozen=True)
class TypeRef:
    """A class declared at the top of a file."""

    #: Resolved absolute path of the declaring file.
    path: str
    name: str
    #: The class itself (a static call's receiver), not an instance of it.
    static: bool = False


_CLASSES = ("class_declaration",)
#: What `this` cannot be seen through: each gives `this` a value of its own.
_THIS_BARRIERS = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "generator_function_declaration",
        "generator_function",
        "class",
        "class_declaration",
        "abstract_class_declaration",
        "class_static_block",
        "program",
    }
)
#: The modifiers that make a constructor parameter a field as well.
_PARAMETER_PROPERTY = ("accessibility_modifier", "readonly", "override_modifier")


def top_level(node: Node) -> bool:
    parent = node.parent
    if parent is not None and parent.type == "export_statement":
        parent = parent.parent
    return parent is not None and parent.type == "program"


def _top_nodes(root: Node) -> list[Node]:
    return [
        c.child_by_field_name("declaration") if c.type == "export_statement" else c
        for c in root.named_children
    ]


def top_declarations(root: Node, kinds: tuple[str, ...], name: str) -> list[Node]:
    """The declarations of ``name`` of one of ``kinds`` at the top of a file."""
    found = []
    for node in _top_nodes(root):
        if node is None or node.type not in kinds:
            continue
        declared = node.child_by_field_name("name")
        if declared is not None and text(declared) == name:
            found.append(node)
    return found


def top_value(root: Node, name: str) -> Node | None:
    """The one module-level ``const`` declarator of ``name``."""
    hits = []
    for node in _top_nodes(root):
        if node is None or node.type != "lexical_declaration" or not any(c.type == "const" for c in node.children):
            continue
        hits += [
            d
            for d in node.named_children
            if d.type == "variable_declarator"
            and (n := d.child_by_field_name("name")) is not None
            and n.type == "identifier"
            and text(n) == name
        ]
    return hits[0] if len(hits) == 1 else None


def _is_this_member(node: Node, name: str) -> bool:
    """``this.<name>`` or ``this["<name>"]``."""
    if node.type == "member_expression":
        obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
        return obj is not None and obj.type == "this" and prop is not None and text(prop) == name
    if node.type == "subscript_expression":
        obj, index = node.child_by_field_name("object"), node.child_by_field_name("index")
        if obj is None or obj.type != "this" or index is None:
            return False
        return text(index)[1:-1] == name if index.type == "string" else True
    return False


def _assigns_this(left: Node, name: str) -> bool:
    """Whether an assignment target is, or destructures into, ``this.<name>``."""
    return any(_is_this_member(n, name) for n in cross_file_keys.walk(left))


def _this_assignments(body: Node, name: str) -> list[Node]:
    """Every assignment to ``this.<name>`` anywhere in a class body, plain or
    destructuring, by member or subscript. A computed subscript counts, as it
    may be the name."""
    return [
        n
        for n in cross_file_keys.walk(body)
        if n.type in ("assignment_expression", "augmented_assignment_expression")
        and (left := n.child_by_field_name("left")) is not None
        and _assigns_this(left, name)
    ]


def _exports_directly(root: Node, name: str) -> bool:
    """Whether a module exports its top-level declaration ``name`` under
    that name and no other export could bind it: ``export class X``,
    ``export const x = ...`` or ``export { X }``. An ``export { Y as X }``,
    an ``export *``, a CommonJS export assignment, or a default export of the
    declaration makes it unprovable."""
    direct = 0
    for node in cross_file_keys.walk(root):
        if node.type in ("assignment_expression", "augmented_assignment_expression"):
            left = node.child_by_field_name("left")
            written = text(left) if left is not None else ""
            if written == "module.exports" or written.startswith(("module.exports.", "module.exports[", "exports.", "exports[")):
                return False
        elif node.type == "export_statement":
            if any(c.type == "*" for c in node.children):
                return False
            if node.child_by_field_name("source") is not None:
                alias = next((c for c in node.named_children if c.type == "namespace_export"), None)
                if alias is not None and name in text(alias):
                    return False
            declaration = node.child_by_field_name("declaration")
            if declaration is None:
                continue
            if any(c.type == "default" for c in node.children):
                continue
            if declaration.type == "lexical_declaration":
                declared = [d.child_by_field_name("name") for d in declaration.named_children]
            else:
                declared = [declaration.child_by_field_name("name")]
            direct += sum(1 for d in declared if d is not None and text(d) == name) if top_level(declaration) else 0
        elif node.type == "export_specifier":
            written = node.child_by_field_name("name")
            alias = node.child_by_field_name("alias")
            exported = alias if alias is not None else written
            if exported is None or text(exported) != name:
                continue
            clause_parent = node.parent.parent if node.parent is not None else None
            if alias is not None or (clause_parent is not None and clause_parent.child_by_field_name("source") is not None):
                return False
            direct += 1
    return direct == 1


def _via_require(node: Node | None) -> bool:
    """Whether a binding node is made by ``require`` rather than ``import``."""
    current = None if node is None else node.parent
    while current is not None and current.type != "program":
        if current.type == "variable_declarator":
            return True
        current = current.parent
    return False


def _constructor_assignment(constructor: Node, name: str) -> Node | None:
    """The one ``this.<name> = ...`` statement directly in a constructor's body."""
    body = constructor.child_by_field_name("body")
    hits = []
    for statement in [] if body is None else body.named_children:
        if statement.type != "expression_statement":
            continue
        expression = next(iter(statement.named_children), None)
        if expression is not None and expression in _this_assignments(statement, name):
            hits.append(expression)
    return hits[0] if len(hits) == 1 else None


# --- Files -----------------------------------------------------------------------


@dataclass
class File:
    #: Resolved absolute path.
    path: str
    source: str
    root: Node
    imports: list[Import] = field(default_factory=list)

    def binding(self, name: str) -> Node | None:
        """The file's one binding of ``name``; ``None`` for none or several."""
        found = bindings(self.root, name)
        return found[0] if len(found) == 1 else None

    def import_of(self, name: str) -> Import | None:
        """The import binding ``name``, when it is the file's only binding of it."""
        node = self.binding(name)
        if node is None or not _in_import(node):
            return None
        hits = [i for i in self.imports if i.local == name]
        return hits[0] if len(hits) == 1 else None


class Files:
    """Parsed files, shared by every proof of one wrapper.

    Keyed by path AND content: a file another pass has edited since it was
    cached is parsed again, so a span is never computed on stale bytes."""

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir
        self._cache: dict[str, File] = {}

    def file(self, path: str, source: str | None = None) -> File | None:
        key = _resolved(path)
        cached = self._cache.get(key)
        if source is None:
            if cached is not None:
                return cached
            try:
                source = Path(key).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
        if cached is not None and cached.source == source:
            return cached
        root = parse(source, key)
        made = File(key, source, root, _imports_of(root, key, self.repo_dir))
        self._cache[key] = made
        return made

    def class_named(self, name: str, file: File) -> TypeRef | None:
        """The class ``name`` means in ``file``: one declared at its top, or a
        named import of one declared at the top of the module it resolves to."""
        if file.binding(name) is None:
            return None
        if len(top_declarations(file.root, _CLASSES, name)) == 1:
            return TypeRef(file.path, name)
        imported = file.import_of(name)
        if imported is None or imported.module is None or imported.imported in (None, "default", "="):
            return None
        if _via_require(file.binding(name)):
            return None
        module = self.file(imported.module)
        if (
            module is None
            or len(top_declarations(module.root, _CLASSES, imported.imported)) != 1
            or not _exports_directly(module.root, imported.imported)
        ):
            return None
        return TypeRef(module.path, imported.imported)

    def _namespace(self, node: Node | None, file: File) -> File | None:
        """The module a namespace import or ``const ns = require()`` binds."""
        if node is None or node.type != "identifier":
            return None
        imported = file.import_of(text(node))
        if imported is None or imported.imported is not None or imported.module is None or not imported.top:
            return None
        if _via_require(file.binding(text(node))):
            return None
        return self.file(imported.module)

    def _class_in(self, module: File | None, name: str, static: bool = False) -> TypeRef | None:
        if module is None or len(top_declarations(module.root, _CLASSES, name)) != 1:
            return None
        if not _exports_directly(module.root, name):
            return None
        return TypeRef(module.path, name, static)

    def annotated(self, annotation: Node | None, file: File) -> TypeRef | None:
        """The class a type annotation names exactly: ``C`` or ``ns.C``."""
        if annotation is None or annotation.type != "type_annotation":
            return None
        written = next((c for c in annotation.named_children if c.type != "comment"), None)
        if written is None:
            return None
        if written.type == "type_identifier":
            return self.class_named(text(written), file)
        if written.type == "nested_type_identifier":
            name = written.child_by_field_name("name")
            module = self._namespace(written.child_by_field_name("module"), file)
            return None if name is None else self._class_in(module, text(name))
        return None

    def constructed(self, value: Node | None, file: File) -> TypeRef | None:
        """The class a ``new C(...)`` or ``new ns.C(...)`` makes."""
        if value is None or value.type != "new_expression":
            return None
        if value.child_by_field_name("type_arguments") is not None:
            return None
        made = value.child_by_field_name("constructor")
        if made is None:
            return None
        if made.type == "identifier":
            return self.class_named(text(made), file)
        if made.type == "member_expression":
            name = made.child_by_field_name("property")
            module = self._namespace(made.child_by_field_name("object"), file)
            return None if name is None else self._class_in(module, text(name))
        return None

    def _exported(self, module: File, name: str) -> TypeRef | None:
        """``name`` at the top of ``module``: the class itself, or the instance
        a ``const name = new C()`` holds."""
        if module.binding(name) is None or not _exports_directly(module.root, name):
            return None
        if len(top_declarations(module.root, _CLASSES, name)) == 1:
            return TypeRef(module.path, name, static=True)
        declarator = top_value(module.root, name)
        if declarator is None:
            return None
        made = self.constructed(declarator.child_by_field_name("value"), module)
        written = declarator.child_by_field_name("type")
        if written is not None and self.annotated(written, module) != made:
            return None
        return made

    def _identifier_type(self, node: Node, file: File) -> TypeRef | None:
        name = text(node)
        bound = file.binding(name)
        parent = None if bound is None else bound.parent
        if parent is None:
            return None
        if parent.type == "class_declaration" and parent.child_by_field_name("name") == bound and top_level(parent):
            return TypeRef(file.path, name, static=True)
        if parent.type == "required_parameter" and parent.child_by_field_name("pattern") == bound:
            return self.annotated(parent.child_by_field_name("type"), file)
        if parent.type == "variable_declarator" and parent.child_by_field_name("name") == bound:
            made = self.constructed(parent.child_by_field_name("value"), file)
            written = parent.child_by_field_name("type")
            if written is None:
                return made
            declared = self.annotated(written, file)
            return declared if made is None or made == declared else None
        imported = file.import_of(name)
        if (
            imported is None
            or imported.module is None
            or imported.type_only
            or not imported.top
            or imported.imported in (None, "default", "=")
            or _via_require(bound)
        ):
            return None
        module = self.file(imported.module)
        return None if module is None else self._exported(module, imported.imported)

    def _this_class(self, node: Node, file: File) -> TypeRef | None:
        """The class an instance ``this`` is, seen only through arrows."""
        current = node.parent
        while current is not None:
            if current.type in ("method_definition", "public_field_definition"):
                inside = current.child_by_field_name("body" if current.type == "method_definition" else "value")
                if inside is None or not (inside.start_byte <= node.start_byte and node.end_byte <= inside.end_byte):
                    return None
                body = current.parent
                if body is None or body.type != "class_body" or any(c.type == "static" for c in current.children):
                    return None
                owner = body.parent
                if owner is None or owner.type != "class_declaration" or not top_level(owner):
                    return None
                name = owner.child_by_field_name("name")
                return None if name is None else TypeRef(file.path, text(name))
            if current.type in _THIS_BARRIERS:
                return None
            current = current.parent
        return None

    def static_type(self, node: Node, file: File) -> TypeRef | None:
        """The class a receiver expression holds, read off what is written."""
        if node.type == "this":
            return self._this_class(node, file)
        if node.type == "identifier":
            return self._identifier_type(node, file)
        if node.type != "member_expression" or node.child_by_field_name("optional_chain") is not None:
            return None
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if obj is None or prop is None or prop.type != "property_identifier":
            return None
        module = self._namespace(obj, file)
        if module is not None:
            return self._exported(module, text(prop))
        owner = self.static_type(obj, file)
        if owner is None or owner.static:
            return None
        return self.field_type(owner, text(prop))

    def field_type(self, owner: TypeRef, name: str) -> TypeRef | None:
        """The class of field ``name`` of ``owner``, from the one place it is
        declared: a field (typed, or initialized with ``new``), a constructor
        parameter property, or, in JavaScript only, the constructor's
        ``this.name = new C()``. Any other assignment to ``this.name``, a
        member of the same name, or two declarations, and it has none."""
        module = self.file(owner.path)
        classes = [] if module is None else top_declarations(module.root, _CLASSES, owner.name)
        if len(classes) != 1:
            return None
        body = classes[0].child_by_field_name("body")
        if body is None:
            return None
        sources: list[TypeRef | None] = []
        constructor: Node | None = None
        for member in body.named_children:
            declared = member.child_by_field_name("name")
            if declared is None:
                continue
            if member.type == "public_field_definition" and text(declared) == name:
                if any(c.type == "static" for c in member.children):
                    continue
                if any(c.type == "?" for c in member.children):
                    sources.append(None)
                    continue
                written = member.child_by_field_name("type")
                sources.append(
                    self.annotated(written, module)
                    if written is not None
                    else self.constructed(member.child_by_field_name("value"), module)
                )
            elif member.type == "method_definition" and text(declared) == name:
                return None
            elif member.type == "method_definition" and text(declared) == "constructor":
                constructor = member
                params = member.child_by_field_name("parameters")
                for p in [] if params is None else params.named_children:
                    if p.type not in ("required_parameter", "optional_parameter"):
                        continue
                    if not any(c.type in _PARAMETER_PROPERTY for c in p.children):
                        continue
                    pattern = p.child_by_field_name("pattern")
                    if pattern is not None and text(pattern) == name:
                        sources.append(
                            self.annotated(p.child_by_field_name("type"), module)
                            if p.type == "required_parameter"
                            else None
                        )
        assignments = _this_assignments(body, name)
        if grammar(module.path) == "js" and constructor is not None:
            assigned = _constructor_assignment(constructor, name)
            if assigned is not None:
                assignments = [a for a in assignments if a != assigned]
                sources.append(self.constructed(assigned.child_by_field_name("right"), module))
        if assignments or len(sources) != 1:
            return None
        return sources[0]
