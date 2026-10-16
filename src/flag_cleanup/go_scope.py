"""Go's lexical scopes and static types, as far as a keyless-wrapper proof
needs them (see :mod:`flag_cleanup.wrapper_go`).

Two questions, both answered from what the source spells, with no type
inference:

* :func:`local_binding` -- does a block, a signature or a statement header
  between a use and the file bind this name? Go resolves a name to the
  innermost such scope, so a name none of them binds is the package's (or,
  for a qualifier, an import's).
* :meth:`Packages.static_type` -- the named type of a receiver expression,
  when the file spells it: a parameter's written type, ``var x T``,
  ``x := &T{}``, ``T{}`` or ``new(T)``, a package-level variable of one of
  those forms, or a struct field's declared type, one ``.field`` at a time.

Anything else answers "unknown", and the caller refuses. A name bound twice
in one scope is unknown too: Go's ``:=`` may redeclare, and which
declaration a use sees is not guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, go_module, syntax

_FUNCTIONS = ("function_declaration", "method_declaration", "func_literal")
#: Statements whose header can declare names visible in their body.
_HEADERS = ("if_statement", "for_statement", "expression_switch_statement", "type_switch_statement")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _within(node: Node, outer: Node) -> bool:
    return outer.start_byte <= node.start_byte and node.end_byte <= outer.end_byte


@dataclass(frozen=True, eq=False)
class Binding:
    """What a declaration says about a name: its written type, or the value
    it was initialized with. Neither: bound, but not readable here."""

    type: Node | None = None
    value: Node | None = None

    @property
    def opaque(self) -> bool:
        return self.type is None and self.value is None


OPAQUE = Binding()


def _specs(declaration: Node, kind: str) -> list[Node]:
    """The ``kind`` specs of a declaration, parenthesised or not."""
    found: list[Node] = []
    for child in declaration.named_children:
        if child.type == kind:
            found.append(child)
        elif child.type.endswith("_spec_list"):
            found += [c for c in child.named_children if c.type == kind]
    return found


def _identifiers(node: Node | None) -> list[Node]:
    return [] if node is None else [c for c in node.named_children if c.type == "identifier"]


def _paired(names: list[Node], values: Node | None) -> list[tuple[Node, Binding]]:
    """Each name with its own value; every name opaque when the counts differ
    (``x, err := f()``)."""
    items = [] if values is None else [c for c in values.named_children if c.type != "comment"]
    if len(items) != len(names):
        return [(n, OPAQUE) for n in names]
    return [(n, Binding(value=v)) for n, v in zip(names, items)]


def var_spec_sites(spec: Node) -> list[tuple[Node, Binding]]:
    names = [n for n in spec.children_by_field_name("name") if n.type == "identifier"]
    written = spec.child_by_field_name("type")
    if written is not None:
        return [(n, Binding(type=written)) for n in names]
    return _paired(names, spec.child_by_field_name("value"))


def _statement_sites(statement: Node) -> list[tuple[Node, Binding]]:
    """The names a statement declares for the statements after it."""
    kind = statement.type
    if kind == "labeled_statement":
        inner = [c for c in statement.named_children if c.type != "label_name"]
        return _statement_sites(inner[0]) if inner else []
    if kind == "short_var_declaration":
        return _paired(_identifiers(statement.child_by_field_name("left")), statement.child_by_field_name("right"))
    if kind == "var_declaration":
        return [site for spec in _specs(statement, "var_spec") for site in var_spec_sites(spec)]
    if kind == "const_declaration":
        return [(n, OPAQUE) for spec in _specs(statement, "const_spec") for n in spec.children_by_field_name("name")]
    if kind == "type_declaration":
        specs = _specs(statement, "type_spec") + _specs(statement, "type_alias")
        return [(n, OPAQUE) for spec in specs for n in spec.children_by_field_name("name")]
    if kind == "receive_statement" and any(c.type == ":=" for c in statement.children):
        return [(n, OPAQUE) for n in _identifiers(statement.child_by_field_name("left"))]
    return []


def _type_parameter_sites(function: Node) -> list[tuple[Node, Binding]]:
    """A generic function's type parameters, and the ones a method names in
    its receiver's type arguments: each shadows a function of its name."""
    found: list[tuple[Node, Binding]] = []
    declared = function.child_by_field_name("type_parameters")
    if declared is not None:
        for parameter in declared.named_children:
            found += [(n, OPAQUE) for n in parameter.children_by_field_name("name")]
    receiver = function.child_by_field_name("receiver")
    for node in [] if receiver is None else cross_file_keys.walk(receiver):
        arguments = node.child_by_field_name("type_arguments") if node.type == "generic_type" else None
        for element in [] if arguments is None else arguments.named_children:
            found += [(n, OPAQUE) for n in element.named_children if n.type == "type_identifier"]
    return found


def _parameter_sites(function: Node) -> list[tuple[Node, Binding]]:
    """Receiver, parameters, named results and type parameters: visible in
    the body only."""
    found = _type_parameter_sites(function)
    for field_name in ("receiver", "parameters", "result"):
        holder = function.child_by_field_name(field_name)
        if holder is None or holder.type != "parameter_list":
            continue
        for parameter in holder.named_children:
            if parameter.type == "parameter_declaration":
                binding = Binding(type=parameter.child_by_field_name("type"))
            elif parameter.type == "variadic_parameter_declaration":
                binding = OPAQUE
            else:
                continue
            found += [(n, binding) for n in parameter.children_by_field_name("name")]
    return found


def _sites_in(scope: Node, child: Node, use: Node) -> list[tuple[Node, Binding]]:
    """What ``scope`` binds for a use reached through its child ``child``."""
    if scope.type in _FUNCTIONS:
        return _parameter_sites(scope) if child == scope.child_by_field_name("body") else []
    if scope.type == "statement_list":
        # A declaration is visible after it ENDS: in `x := x` the right side
        # is the outer `x`.
        return [
            site
            for statement in scope.named_children
            if statement.end_byte <= use.start_byte
            for site in _statement_sites(statement)
        ]
    if scope.type == "communication_case":
        communication = scope.child_by_field_name("communication")
        return _statement_sites(communication) if communication is not None and child != communication else []
    if scope.type in _HEADERS:
        found: list[tuple[Node, Binding]] = []
        initializer = scope.child_by_field_name("initializer")
        if initializer is not None and child != initializer:
            found += _statement_sites(initializer)
        if scope.type == "type_switch_statement" and child.type in ("type_case", "default_case"):
            found += [(n, OPAQUE) for n in _identifiers(scope.child_by_field_name("alias"))]
        if scope.type == "for_statement":
            for clause in scope.named_children:
                if clause.type == "for_clause":
                    inner = clause.child_by_field_name("initializer")
                    if inner is not None and not _within(use, inner):
                        found += _statement_sites(inner)
                elif clause.type == "range_clause" and child != clause and any(c.type == ":=" for c in clause.children):
                    found += [(n, OPAQUE) for n in _identifiers(clause.child_by_field_name("left"))]
        return found
    return []


def local_binders(use: Node, name: str) -> list[Node]:
    """The name tokens of the innermost local declarations of ``name``
    visible at ``use``: one for a name bound once, none when no scope between
    ``use`` and the file binds it."""
    child, scope = use, use.parent
    while scope is not None and scope.type != "source_file":
        hits = [node for node, _ in _sites_in(scope, child, use) if _text(node) == name]
        if hits:
            return hits
        child, scope = scope, scope.parent
    return []


def local_binding(use: Node, name: str) -> Binding | None:
    """The innermost local binding of ``name`` visible at ``use``.

    ``None`` when no scope between ``use`` and the file binds it, so the name
    is the package's or an import's. Two bindings in the scope that binds it
    are :data:`OPAQUE`.
    """
    child, scope = use, use.parent
    while scope is not None and scope.type != "source_file":
        hits = [binding for node, binding in _sites_in(scope, child, use) if _text(node) == name]
        if hits:
            return hits[0] if len(hits) == 1 else OPAQUE
        child, scope = scope, scope.parent
    return None


# --- Packages, imports and static types -----------------------------------------


@dataclass(frozen=True)
class TypeRef:
    """A named type: the package that declares it, and its name."""

    directory: Path
    clause: str
    name: str


@dataclass(frozen=True)
class Import:
    path: str
    #: The name written before the path; ``None`` when there is none.
    alias: str | None
    directory: Path
    clause: str


@dataclass
class File:
    path: Path
    root: Node
    directory: Path
    clause: str | None
    module: go_module.Module | None
    #: The source ``root`` was parsed from, for byte offsets into it.
    source: str = ""


def import_specs(root: Node) -> list[Node]:
    return [
        spec
        for declaration in root.named_children
        if declaration.type == "import_declaration"
        for spec in _specs(declaration, "import_spec")
    ]


def _embedded_name(written: Node) -> str | None:
    """The field name an embedded type gives: ``Flags`` for ``Flags``,
    ``*Flags`` or ``*ff.Flags``."""
    if written.type == "type_identifier":
        return _text(written)
    if written.type == "qualified_type":
        name = written.child_by_field_name("name")
        return None if name is None else _text(name)
    return None


class Packages:
    """Parsed Go files, by path and by package, for one wrapper's proof.

    ``stop`` is the checkout root: no ``go.mod`` above it is read.
    """

    def __init__(self, stop: Path) -> None:
        self.stop = stop.resolve()
        self._files: dict[Path, File | None] = {}

    def file(self, path: Path, source: str | None = None) -> File | None:
        path = path.resolve()
        if path not in self._files:
            if source is None:
                from flag_cleanup import mock_stubs

                try:
                    # Byte-exact, as the wrapper path's other readers are.
                    source = mock_stubs._read_exact(str(path))
                except (OSError, UnicodeDecodeError):
                    source = None
            self._files[path] = None if source is None else self._parse(path, source)
        return self._files[path]

    def _parse(self, path: Path, source: str) -> File:
        root = syntax._parse(source, "go")
        return File(
            path, root, path.parent, go_module.package_clause(root), go_module.module_of(path.parent, self.stop), source
        )

    def package(self, directory: Path, clause: str) -> list[File]:
        """Every file of the package: ``directory``'s Go files with ``clause``,
        test files of the same package included."""
        found: list[File] = []
        for path in sorted(directory.glob("*.go")):
            file = self.file(path)
            if file is not None and file.clause == clause:
                found.append(file)
        return found

    def _clause_of(self, directory: Path) -> str | None:
        """The clause an import of ``directory`` binds: its non-test files'."""
        clauses = {
            file.clause
            for path in sorted(directory.glob("*.go"))
            if not path.name.endswith("_test.go") and (file := self.file(path)) is not None
        }
        return next(iter(clauses)) if len(clauses) == 1 and None not in clauses else None

    def imported(self, file: File, name: str) -> Import | None:
        """The import that binds ``name`` in ``file``, when it is a package of
        ``file``'s own module.

        An unaliased import binds its package clause. A compiling file has one
        import per bound name, so a match here is the only one. An import of
        another module never matches, even when it binds ``name``.
        """
        if file.module is None:
            return None
        for spec in import_specs(file.root):
            path_node = spec.child_by_field_name("path")
            if path_node is None:
                continue
            alias_node = spec.child_by_field_name("name")
            alias = None if alias_node is None else _text(alias_node)
            if alias in (".", "_"):
                continue
            path = _text(path_node).strip('"`')
            directory = file.module.directory(path)
            if directory is None or not directory.is_dir():
                continue
            owner = go_module.module_of(directory, self.stop)
            if owner is None or owner.go_mod != file.module.go_mod:
                continue
            clause = self._clause_of(directory.resolve())
            if clause is not None and (alias or clause) == name:
                return Import(path, alias, directory.resolve(), clause)
        return None

    def resolve_type(self, node: Node, file: File) -> TypeRef | None:
        """The named type a written type means: ``T``, ``*T``, ``pkg.T`` or
        ``*pkg.T``. A local ``type T`` in scope, or ``pkg`` bound locally,
        means something else."""
        if node.type == "pointer_type":
            inner = node.named_children
            if len(inner) != 1:
                return None
            node = inner[0]
        if node.type in ("type_identifier", "identifier"):
            name = _text(node)
            if file.clause is None or local_binding(node, name) is not None:
                return None
            return TypeRef(file.directory, file.clause, name)
        if node.type in ("qualified_type", "selector_expression"):
            package = node.child_by_field_name("package") or node.child_by_field_name("operand")
            name = node.child_by_field_name("name") or node.child_by_field_name("field")
            if package is None or name is None or package.type not in ("package_identifier", "identifier"):
                return None
            if local_binding(node, _text(package)) is not None:
                return None
            imported = self.imported(file, _text(package))
            return None if imported is None else TypeRef(imported.directory, imported.clause, _text(name))
        return None

    def static_type(self, expression: Node, file: File, depth: int = 0) -> TypeRef | None:
        """The named type of an identifier or a ``.field`` chain on one."""
        if depth > 8:
            return None
        if expression.type == "identifier":
            name = _text(expression)
            bound = local_binding(expression, name)
            if bound is None:
                return None if file.clause is None else self.package_var(file.directory, file.clause, name)
            return self._binding_type(bound, file)
        if expression.type != "selector_expression":
            return None
        operand = expression.child_by_field_name("operand")
        field_node = expression.child_by_field_name("field")
        if operand is None or field_node is None or field_node.type != "field_identifier":
            return None
        if operand.type == "identifier" and local_binding(operand, _text(operand)) is None:
            imported = self.imported(file, _text(operand))
            if imported is not None:
                return self.package_var(imported.directory, imported.clause, _text(field_node))
        inner = self.static_type(operand, file, depth + 1)
        return None if inner is None else self.field_type(inner, _text(field_node))

    def _binding_type(self, binding: Binding, file: File) -> TypeRef | None:
        if binding.type is not None:
            return self.resolve_type(binding.type, file)
        if binding.value is not None:
            return self._value_type(binding.value, file)
        return None

    def _value_type(self, value: Node, file: File) -> TypeRef | None:
        """``T{...}``, ``&T{...}`` or ``new(T)``; nothing else is read."""
        if value.type == "unary_expression":
            operator = value.child_by_field_name("operator")
            operand = value.child_by_field_name("operand")
            if operator is None or _text(operator) != "&" or operand is None or operand.type != "composite_literal":
                return None
            value = operand
        if value.type == "composite_literal":
            written = value.child_by_field_name("type")
            return None if written is None else self.resolve_type(written, file)
        if value.type == "call_expression":
            function = value.child_by_field_name("function")
            arguments = value.child_by_field_name("arguments")
            if function is None or function.type != "identifier" or _text(function) != "new" or arguments is None:
                return None
            if local_binding(function, "new") is not None or (
                file.clause is not None and self._declares(file.directory, file.clause, "new")
            ):
                return None
            items = [c for c in arguments.named_children if c.type != "comment"]
            return self.resolve_type(items[0], file) if len(items) == 1 else None
        return None

    def declares(self, directory: Path, clause: str, name: str) -> bool:
        """Whether the package declares ``name`` as a function, ``var``,
        ``const`` or type at package level."""
        return self._declares(directory, clause, name)

    def _declares(self, directory: Path, clause: str, name: str) -> bool:
        """Whether the package declares ``name`` at package level."""
        for file in self.package(directory, clause):
            for declaration in file.root.named_children:
                if declaration.type == "function_declaration":
                    found = declaration.child_by_field_name("name")
                    if found is not None and _text(found) == name:
                        return True
                elif declaration.type in ("var_declaration", "const_declaration", "type_declaration"):
                    if any(_text(n) == name for n, _ in _statement_sites(declaration)):
                        return True
        return False

    def package_var(self, directory: Path, clause: str, name: str) -> TypeRef | None:
        """The type of a package-level ``var`` declared exactly once."""
        site = self.package_var_binding(directory, clause, name)
        return None if site is None else self._binding_type(site[1], site[0])

    def package_var_binding(self, directory: Path, clause: str, name: str) -> tuple[File, Binding] | None:
        """What the one declaration of a package-level ``var`` says, and its
        file; ``None`` unless declared exactly once, with a type or value."""
        sites: list[tuple[File, Binding]] = []
        for file in self.package(directory, clause):
            for declaration in file.root.named_children:
                if declaration.type != "var_declaration":
                    continue
                for spec in _specs(declaration, "var_spec"):
                    sites += [(file, b) for n, b in var_spec_sites(spec) if _text(n) == name]
        if len(sites) != 1 or sites[0][1].opaque:
            return None
        return sites[0]

    def _struct(self, owner: TypeRef) -> tuple[File, Node] | None:
        """The one non-generic ``struct`` type declaration of ``owner``, and
        its file; ``None`` when there is not exactly one, or it is not one."""
        specs = [
            (file, spec)
            for file in self.package(owner.directory, owner.clause)
            for declaration in file.root.named_children
            if declaration.type == "type_declaration"
            for spec in _specs(declaration, "type_spec")
            if (n := spec.child_by_field_name("name")) is not None and _text(n) == owner.name
        ]
        if len(specs) != 1:
            return None
        file, spec = specs[0]
        body = spec.child_by_field_name("type")
        if spec.child_by_field_name("type_parameters") is not None or body is None or body.type != "struct_type":
            return None
        return file, body

    def is_struct(self, owner: TypeRef) -> bool:
        """Whether ``owner`` is declared, once, as a non-generic struct."""
        return self._struct(owner) is not None

    def field_type(self, owner: TypeRef, name: str) -> TypeRef | None:
        """The type of field ``name`` declared directly in struct ``owner``
        (an embedded field by its type name); never a promoted one."""
        written = self.field_written(owner, name)
        return None if written is None else self.resolve_type(written[1], written[0])

    def field_written(self, owner: TypeRef, name: str) -> tuple[File, Node] | None:
        """The written type of field ``name`` of struct ``owner``, as
        :meth:`field_type` finds it, and the struct's file."""
        found = self._struct(owner)
        if found is None:
            return None
        file, body = found
        fields = next((c for c in body.named_children if c.type == "field_declaration_list"), None)
        hits: list[Node | None] = []
        for declaration in [] if fields is None else fields.named_children:
            if declaration.type != "field_declaration":
                continue
            written = declaration.child_by_field_name("type")
            names = declaration.children_by_field_name("name")
            if names:
                if any(_text(n) == name for n in names):
                    hits.append(written)
            elif written is not None and _embedded_name(written) == name:
                hits.append(written)
        if len(hits) != 1 or hits[0] is None:
            return None
        return file, hits[0]
