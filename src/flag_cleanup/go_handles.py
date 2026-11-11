"""Whether a Go typed flag handle's constructor carries the key.

``var HeaderV5WriteFlag = NewBoolFlag("header-v5-write", false)`` is a handle
(a third kind of key constant, :func:`key_const._go_var_holders`) only when
``NewBoolFlag`` provably puts its first argument, unchanged, into the value it
returns. Then every read of the handle is a read of the key, and the
declaration can go once those reads fold.

What the constructor may do besides is not limited: a real flag package
registers each flag in a store or a fallback map first, keeps the struct in a
local while it does, or logs. The proof is a WHITELIST of how the key may
travel to the returned value, so a shape nobody listed refuses:

* the constructor is one function declared in the repository (the file's own
  package, or ``pkg.Name`` through an import of a package of the checkout), with no type
  parameters, an unnamed result, and a first parameter of type ``string``,
  which is only ever read (never assigned, rebound or addressed);
* if it has other parameters, one of them is a ``bool``: a constructor whose
  other arguments are all something else (an int, a string, a JSON value)
  builds a handle of another type;
* the body's last statement is its only ``return`` (a function literal's own
  returns aside), of ``T{…}``/``&T{…}``, or of a local ``f`` declared once at
  the body's top as ``f := T{…}`` or ``var f = T{…}``;
* ``T`` is a struct type declared in the repository (a named slice or map, an
  alias, a generic type or a struct with an embedded field refuses), the key
  parameter is the value of exactly one field, and every other field value is
  a parameter declared ``bool`` or a non-string basic literal (any other type,
  a string or rune literal, or a composite could be the field the reads use),
  and the literal sets every field (one left at its zero value could be too);
* after its declaration ``f`` appears only as ``f.<field>`` of a field ``T``
  declares, read as a value (not written, addressed, called or reached
  through), and in the final ``return f``.

So a method value (``f.set``), the local handed on whole, a field computed
from package state (``bit := len(all)``) and a key that may sit in another
field all refuse, as ``constructor-unproven``.

The handle's own call is judged too: deleting ``var X = NewBoolFlag("k",
Default())`` stops ``Default()`` running, so an argument that calls anything
but a conversion to a basic type, or a function named in
``side-effect-free-calls``, refuses as :data:`ARGUMENT_CALL`.

Deleting the declaration then drops the rest of the body FOR THIS FLAG ONLY:
what it registered under the key, and whatever it counted. Anything reading
the store by this key spells the key or the handle, and both are mentions this
run already sees. What it cannot see is code that lists or counts every flag
(a test asserting the registered set, an endpoint listing it), which sees one
fewer, exactly as when the flag is removed by hand; so a deleted handle whose
constructor does more than build it is named in the pull request
(:data:`SIDE_EFFECTS`).

The one case where more than this flag's share goes is the LAST call: with no
other call left, nothing runs the body again, and a statement that does not
depend on the flag (a ``sync.Once`` that initialises the store, say) stops
running at all. So a constructor that does more than build the handle must
keep another call that certainly runs: a package-level ``var`` initializer of
the declaring package whose whole value is the call, in a file that is
neither a test, behind a build constraint nor cgo, for another key. Otherwise
the handle is refused (:data:`LAST_CALL`).

And anything left in the checkout that still spells the key may read what the
constructor did for it (an ``init`` that panics when the store lacks it), so
when such a handle is deleted and any mention of the key survives the
rewrite, the whole removal is refused (:func:`residue_refusals`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup.tree_walk import preorder

#: The constructor is not one function declared in the repository.
UNRESOLVED = "constructor-unresolved"
#: The constructor is declared, and the key is not proven to reach what it
#: returns unchanged.
UNPROVEN = "constructor-unproven"
#: The constructor does more than build the handle, and this is its last call
#: that certainly runs.
LAST_CALL = "constructor-last-call"
#: An argument of the handle's constructor call calls something, which
#: deleting the declaration would stop running.
ARGUMENT_CALL = "handle-argument-call"
#: ``X.Fallback()``: the constructor's default, a read of the handle that is
#: not the flag's value under either treatment. Folding it to the constructor's
#: literal would need the handle type's ``Fallback`` proven to return the field
#: the constructor set from that argument, and nothing assigned to it since;
#: none of that is proven, so the call is named and the handle kept.
FALLBACK_READ = "handle-fallback"
#: ``bindings`` kind: a handle deleted although its constructor does more
#: than build it. ``text`` is the declaration.
SIDE_EFFECTS = "handle constructor"


@dataclass(frozen=True)
class Verdict:
    #: ``None`` when the constructor is proven; otherwise one of
    #: :data:`UNRESOLVED`, :data:`UNPROVEN`, :data:`LAST_CALL`.
    refusal: str | None
    #: The body is the construction and nothing else.
    pure: bool = True


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk_outside_literals(node: Node):
    """``node`` and everything under it, a function literal's body excluded."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if current.type != "func_literal":
            stack.extend(reversed(current.named_children))


def _is_field_key(identifier: Node) -> bool:
    """``name`` in ``T{name: v}``: the field, not the variable."""
    parent = identifier.parent
    if parent is not None and parent.type == "literal_element":
        holder = parent.parent
        return holder is not None and holder.type == "keyed_element" and holder.child_by_field_name("key") == parent
    return False


#: Statements whose ``left`` expression list is written.
_WRITERS = ("assignment_statement", "short_var_declaration", "range_clause")
#: Nodes whose ``name`` binds a name.
_BINDERS = ("var_spec", "const_spec", "parameter_declaration", "variadic_parameter_declaration", "type_parameter_declaration")


def _use(identifier: Node) -> tuple[str, bool]:
    """How ``identifier`` is used: ``(kind, chained)``. ``kind`` is
    ``bind``, ``write``, ``address``, ``call`` or ``read``; ``chained`` says
    whether it is reached through a field or index (``v.f``, ``v[i]``)."""
    parent = identifier.parent
    if parent is not None and parent.type in _BINDERS:
        return "bind", False
    top = identifier
    chained = False
    while top.parent is not None and (
        (top.parent.type in ("selector_expression", "index_expression") and top.parent.child_by_field_name("operand") == top)
        or top.parent.type == "parenthesized_expression"
    ):
        chained = chained or top.parent.type != "parenthesized_expression"
        top = top.parent
    holder = top.parent
    if holder is None:
        return "read", chained
    if holder.type == "expression_list" and holder.parent is not None and holder.parent.type in _WRITERS:
        if holder.parent.child_by_field_name("left") == holder:
            return "write", chained
    if holder.type in ("inc_statement", "dec_statement"):
        return "write", chained
    if holder.type == "unary_expression" and holder.children and holder.children[0].type == "&":
        return "address", chained
    if holder.type == "call_expression" and holder.child_by_field_name("function") == top:
        return "call", chained
    return "read", chained


def _mentions(body: Node, name: bytes) -> list[Node]:
    return [
        n for n in preorder(body) if n.type == "identifier" and n.text == name and not _is_field_key(n)
    ]


#: Literals a field other than the key's may be given: never a string or a
#: rune, which could be the key the reads use.
_NON_STRING_LITERALS = frozenset({"int_literal", "float_literal", "imaginary_literal", "true", "false", "nil"})


def _literal(expression: Node) -> tuple[Node | None, bool]:
    """``(composite literal, through &)`` for ``T{…}`` or ``&T{…}``."""
    pointer = False
    if expression.type == "unary_expression":
        if not expression.children or expression.children[0].type != "&":
            return None, False
        expression = expression.child_by_field_name("operand")
        pointer = True
    if expression is None or expression.type != "composite_literal":
        return None, False
    return expression, pointer


def _parameters(constructor: Node) -> list[tuple[bytes, bytes]]:
    """``(name, type text)`` per parameter name, in order; a variadic
    parameter's type is spelled ``...T``."""
    listed = constructor.child_by_field_name("parameters")
    found: list[tuple[bytes, bytes]] = []
    for declaration in listed.named_children if listed is not None else ():
        kind = declaration.child_by_field_name("type")
        spelled = b"" if kind is None else kind.text
        if declaration.type == "variadic_parameter_declaration":
            spelled = b"..." + spelled
        names = declaration.children_by_field_name("name")
        found.extend((n.text, spelled) for n in names) if names else found.append((b"_", spelled))
    return found


def _values(literal: Node, fields: tuple[bytes, ...]) -> list[tuple[bytes, Node]] | None:
    """``(field, value)`` per element of a struct literal whose type declares
    ``fields`` in order, or ``None`` when an element is not ``field: value``
    or a positional value (a positional literal must list every field)."""
    body = literal.child_by_field_name("body")
    if body is None:
        return None
    elements = [e for e in body.named_children if e.type != "comment"]
    keyed = [e for e in elements if e.type == "keyed_element"]
    found: list[tuple[bytes, Node]] = []
    if keyed and len(keyed) != len(elements):
        return None
    if keyed:
        for element in keyed:
            key = element.child_by_field_name("key")
            value = element.child_by_field_name("value")
            while key is not None and key.type == "literal_element" and len(key.named_children) == 1:
                key = key.named_children[0]
            if key is None or key.type not in ("identifier", "field_identifier") or value is None:
                return None
            found.append((key.text, value))
    else:
        if len(elements) != len(fields):
            return None
        found = list(zip(fields, elements))
    unwrapped: list[tuple[bytes, Node]] = []
    for field, value in found:
        while value.type == "literal_element" and len(value.named_children) == 1:
            value = value.named_children[0]
        if field not in fields:
            return None
        unwrapped.append((field, value))
    return unwrapped


def _single(expression: Node | None) -> Node | None:
    """The one expression of an ``expression_list`` of one, or the node."""
    if expression is None:
        return None
    if expression.type == "expression_list":
        return expression.named_children[0] if len(expression.named_children) == 1 else None
    return expression


def _local(statement: Node) -> tuple[Node, Node] | None:
    """``(name, value)`` of ``v := X`` or ``var v = X`` declaring one name."""
    if statement.type == "short_var_declaration":
        left = statement.child_by_field_name("left")
        name = _single(left)
        value = _single(statement.child_by_field_name("right"))
        if name is not None and name.type == "identifier" and value is not None:
            return name, value
        return None
    if statement.type == "var_declaration":
        specs = [c for c in statement.named_children if c.type == "var_spec"]
        if len(specs) != 1 or specs[0].child_by_field_name("type") is not None:
            return None
        names = [c for c in specs[0].children_by_field_name("name") if c.type == "identifier"]
        value = _single(specs[0].child_by_field_name("value"))
        if len(names) == 1 and value is not None:
            return names[0], value
    return None


#: Where ``v.f`` may NOT stand when ``f`` is read as a value: written,
#: addressed, called, or reached through (``v.f.g``, ``v.f[i]``, ``v.f[a:b]``).
def _field_read(selector: Node) -> bool:
    parent = selector.parent
    if parent is None:
        return False
    if parent.type in ("inc_statement", "dec_statement", "parenthesized_expression"):
        return False
    if parent.type == "expression_list" and parent.parent is not None and parent.parent.type in _WRITERS:
        if parent.parent.child_by_field_name("left") == parent:
            return False
    if parent.type == "unary_expression" and parent.children and parent.children[0].type == "&":
        return False
    if parent.type == "call_expression" and parent.child_by_field_name("function") == selector:
        return False
    if parent.type in ("selector_expression", "index_expression", "slice_expression", "type_assertion_expression"):
        if parent.child_by_field_name("operand") == selector:
            return False
    return True


def carried(constructor: Node, struct_fields) -> tuple[bool, bool]:
    """``(proven, pure)`` for a constructor declaration: whether its first
    parameter provably reaches the value it returns unchanged, and whether
    the body does nothing else. ``struct_fields`` maps the literal's written
    type to the field names of the struct it declares, or ``None``.

    A WHITELIST, so a shape nobody thought of refuses (see the module
    docstring for the rules)."""
    if constructor.child_by_field_name("type_parameters") is not None:
        return False, False
    result = constructor.child_by_field_name("result")
    if result is not None and result.type == "parameter_list":
        # A named result can be changed by a deferred call after the
        # `return`; `(T)` is the one parenthesised form kept.
        declared = result.named_children
        if len(declared) != 1 or declared[0].children_by_field_name("name"):
            return False, False
    elif result is None:
        return False, False
    body = constructor.child_by_field_name("body")
    parameters = _parameters(constructor)
    if body is None or not parameters:
        return False, False
    parameter, kind = parameters[0]
    if kind != b"string" or parameter == b"_":
        return False, False
    others = [p for p in parameters[1:]]
    if others and not any(t == b"bool" for _, t in others):
        # Every other argument is something else (an int, a string, a JSON
        # value): not a boolean flag's constructor.
        return False, False
    booleans = {name for name, t in parameters[1:] if t == b"bool"} - {parameter, b"_"}
    listed = next((c for c in body.named_children if c.type == "statement_list"), None)
    statements = [c for c in (listed.named_children if listed is not None else []) if c.type != "comment"]
    if not statements or statements[-1].type != "return_statement":
        return False, False
    final = statements[-1]
    if any(n.type == "return_statement" and n.id != final.id for n in _walk_outside_literals(body)):
        return False, False
    # The parameter is only ever read.
    if any(_use(n)[0] != "read" for n in _mentions(body, parameter)):
        return False, False
    returned = _single(next(iter(final.named_children), None))
    if returned is None:
        return False, False

    def holds_key(literal: Node) -> tuple[bytes, ...] | None:
        """The struct's fields when ``literal`` is ``T{…}`` of a repository
        struct (no embedded field) holding the key parameter in exactly one
        field, and in every other field only a parameter declared ``bool``
        or a non-string basic literal. An exact whitelist: a field of any
        other type could be the one the reads use (a named string type, a
        ``[]byte``, a struct whose field is promoted, a string literal)."""
        kind = literal.child_by_field_name("type")
        fields = None if kind is None else struct_fields(kind)
        if not fields:
            return None
        values = _values(literal, fields)
        if values is None or sorted(f for f, _ in values) != sorted(fields):
            # A field left out holds its zero value, and may be the one the
            # reads use.
            return None
        keyed = 0
        for _, value in values:
            if value.type in _NON_STRING_LITERALS:
                continue
            if value.type != "identifier":
                return None
            if value.text == parameter:
                keyed += 1
            elif value.text not in booleans:
                return None
        return fields if keyed == 1 else None

    literal, _ = _literal(returned)
    if literal is not None:
        if holds_key(literal) is None:
            return False, False
        return True, len(statements) == 1
    # `return f`, `f` a local declared once at the top as `f := T{…}`.
    if returned.type != "identifier":
        return False, False
    local = returned.text
    declarations = [(s, found) for s in statements[:-1] if (found := _local(s)) is not None and found[0].text == local]
    if len(declarations) != 1:
        return False, False
    _, (name, value) = declarations[0]
    if value.type != "composite_literal":
        return False, False
    fields = holds_key(value)
    if fields is None:
        return False, False
    for mention in _mentions(body, local):
        if mention.id in (name.id, returned.id):
            continue
        # Only `f.<field>` of a declared field, read as a value. A method value
        # (`f.set`), the local handed on whole, a write, `&` all refuse.
        selector = mention.parent
        if selector is None or selector.type != "selector_expression" or selector.child_by_field_name("operand") != mention:
            return False, False
        field = selector.child_by_field_name("field")
        if field is None or field.text not in fields or not _field_read(selector):
            return False, False
    return True, len(statements) == 2


#: GOOS and GOARCH values a file name can end in (`_linux.go`,
#: `_linux_amd64.go`), which build the file only there.
_GOOS = frozenset(
    "aix android darwin dragonfly freebsd hurd illumos ios js linux nacl netbsd openbsd plan9 solaris wasip1 windows zos".split()
)
_GOARCH = frozenset(
    "386 amd64 arm arm64 loong64 mips mipsle mips64 mips64le ppc64 ppc64le riscv64 s390x wasm".split()
)


def _always_built(file) -> bool:
    """A non-test file with no build constraint, in its name or its text."""
    name = file.path.name
    if name.endswith("_test.go"):
        return False
    parts = name[: -len(".go")].split("_")[1:]
    if parts and (parts[-1] in _GOOS or parts[-1] in _GOARCH):
        return False
    head = file.source.split("\npackage ", 1)[0]
    if any(line.strip().startswith(("//go:build", "// +build")) for line in head.splitlines()):
        return False
    # A cgo file is built only with cgo enabled.
    return not any(
        (path := spec.child_by_field_name("path")) is not None and _text(path) == '"C"'
        for declaration in file.root.named_children
        if declaration.type == "import_declaration"
        for spec in preorder(declaration)
        if spec.type == "import_spec"
    )


def _another_call(packages, here: Path, call: Node, target: tuple[Path, str, bytes]) -> bool:
    """Whether a package-level ``var`` of ``here``'s package, in a file that is
    always built, is initialised by a call of the constructor ``target`` for
    another first argument than ``call``'s."""
    from flag_cleanup import go_scope

    file = packages.file(here)
    if file is None or file.clause is None:
        return False
    arguments = call.child_by_field_name("arguments")
    key = _text(arguments.named_children[0]) if arguments is not None and arguments.named_children else None
    directory, clause, name = target
    for member in packages.package(here.parent, file.clause):
        if not _always_built(member):
            continue
        for declaration in member.root.named_children:
            if declaration.type != "var_declaration":
                continue
            # Only a spec's whole value, `var X = Ctor(…)`: a call under `&&`,
            # `||` or a function literal may never run.
            values = [
                value.named_children[0]
                for spec in preorder(declaration)
                if spec.type == "var_spec"
                and (value := spec.child_by_field_name("value")) is not None
                and len(value.named_children) == 1
            ]
            for node in values:
                if node.type != "call_expression":
                    continue
                function = node.child_by_field_name("function")
                if function is None:
                    continue
                if function.type == "identifier":
                    if function.text != name or (member.directory, member.clause) != (directory, clause):
                        continue
                elif function.type == "selector_expression":
                    operand = function.child_by_field_name("operand")
                    field = function.child_by_field_name("field")
                    if operand is None or field is None or operand.type != "identifier" or field.text != name:
                        continue
                    imported = packages.imported(member, _text(operand))
                    if imported is None or (imported.directory, imported.clause) != (directory, clause):
                        continue
                    if go_scope.local_binders(function, _text(operand)):
                        continue
                else:
                    continue
                listed = node.child_by_field_name("arguments")
                first = listed.named_children[0] if listed is not None and listed.named_children else None
                if first is None or _text(first) == key:
                    continue
                return True
    return False


def _struct_fields(packages, file, written: Node) -> tuple[bytes, ...] | None:
    """The field names, in order, of the struct type ``written`` names in
    ``file``: ``T`` declared once in ``file``'s package, or ``pkg.T`` through
    an import :meth:`go_scope.Packages.imported` resolves. Anything else (a named slice or map, an
    alias, a generic type, a type from a dependency, a struct with an
    embedded field) is ``None``."""
    if written.type == "type_identifier":
        name, directory, clause = written.text, file.directory, file.clause
    elif written.type == "qualified_type":
        package = written.child_by_field_name("package")
        named = written.child_by_field_name("name")
        if package is None or named is None:
            return None
        imported = packages.imported(file, _text(package))
        if imported is None:
            return None
        name, directory, clause = named.text, imported.directory, imported.clause
    else:
        return None
    specs = [
        spec
        for member in packages.package(directory, clause)
        for declaration in member.root.named_children
        if declaration.type == "type_declaration"
        for spec in declaration.named_children
        if spec.type in ("type_spec", "type_alias")
        and spec.child_by_field_name("name") is not None
        and spec.child_by_field_name("name").text == name
    ]
    if len(specs) != 1 or specs[0].type != "type_spec" or specs[0].child_by_field_name("type_parameters") is not None:
        return None
    struct = specs[0].child_by_field_name("type")
    if struct is None or struct.type != "struct_type":
        return None
    listed = next((c for c in struct.named_children if c.type == "field_declaration_list"), None)
    fields: list[bytes] = []
    for declaration in listed.named_children if listed is not None else ():
        if declaration.type != "field_declaration":
            continue
        names = declaration.children_by_field_name("name")
        if not names:
            # An embedded field promotes its own fields, so a name read on the
            # handle need not be one this literal sets.
            return None
        fields.extend(n.text for n in names)
    return tuple(fields)


def _constructor(call: Node, path: str | None):
    """``(packages, here, declaration, (directory, clause, name), fields)`` for the
    ``Ctor`` of a handle's ``Ctor("key", …)`` at ``path``, or ``None`` when it
    is not exactly one function declared in the repository: ``Name`` of the
    file's own package or ``pkg.Name`` through an import of a package of the checkout.
    Any other callee (the standard library, a dependency) is not resolved,
    and nor is one asked about without the file's path. ``fields`` resolves
    a type written in the constructor's file (:func:`_struct_fields`)."""
    if path is None:
        return None
    from flag_cleanup import go_scope, source_index

    function = call.child_by_field_name("function")
    if function is None:
        return None
    packages = go_scope.Packages(Path(source_index.checkout_root(str(Path(path).resolve().parent))))
    here = Path(path).resolve()
    file = packages.file(here)
    if file is None or file.clause is None:
        return None
    if function.type == "identifier":
        name = function.text
        directory, clause = here.parent, file.clause
    elif function.type == "selector_expression":
        operand = function.child_by_field_name("operand")
        field = function.child_by_field_name("field")
        if operand is None or field is None or operand.type != "identifier":
            return None
        if go_scope.local_binders(function, _text(operand)):
            return None
        imported = packages.imported(file, _text(operand))
        if imported is None:
            return None
        name, directory, clause = field.text, imported.directory, imported.clause
    else:
        return None
    found = [
        (member, declaration)
        for member in packages.package(directory, clause)
        for declaration in member.root.named_children
        if declaration.type == "function_declaration"
        and declaration.child_by_field_name("name") is not None
        and declaration.child_by_field_name("name").text == name
    ]
    if len(found) != 1:
        return None
    member, declaration = found[0]
    return (
        packages,
        here,
        declaration,
        (directory.resolve(), clause, name),
        lambda written: _struct_fields(packages, member, written),
    )


#: Predeclared types a call may convert an argument to without running code.
_BASIC_TYPES = frozenset(
    b"bool string byte rune int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 uintptr "
    b"float32 float64 complex64 complex128".split()
)


def _argument_calls(call: Node) -> list[Node]:
    """The calls among a handle's arguments after the key that deleting the
    declaration would stop running: any call but a conversion to a basic
    type (``bool(x)``) or one the customer names in ``side-effect-free-calls``,
    and any channel receive (``<-ready``)."""
    from flag_cleanup import inert_calls

    declared = inert_calls.declared()
    arguments = call.child_by_field_name("arguments")
    found: list[Node] = []
    for argument in (arguments.named_children[1:] if arguments is not None else ()):
        for node in preorder(argument):
            if node.type == "unary_expression" and node.children and node.children[0].type == "<-":
                # A channel receive blocks and consumes a value, like a call.
                found.append(node)
                continue
            if node.type != "call_expression":
                continue
            function = node.child_by_field_name("function")
            listed = node.child_by_field_name("arguments")
            if (
                function is not None
                and function.type == "identifier"
                and function.text in _BASIC_TYPES
                and listed is not None
                and len(listed.named_children) == 1
            ):
                continue
            name = function
            if name is not None and name.type == "selector_expression":
                name = name.child_by_field_name("field")
            if name is not None and _text(name) in declared:
                continue
            found.append(node)
    return found


def verdict(call: Node, path: str | None) -> Verdict:
    """Whether the constructor of a handle's ``Ctor("key", …)`` at ``path``
    is proven (see the module docstring)."""
    resolved = _constructor(call, path)
    if resolved is None:
        return Verdict(UNRESOLVED, False)
    packages, here, declaration, target, fields = resolved
    directory, clause, _ = target
    if any(packages.declares(directory, clause, name) for name in ("bool", "string")):
        # The proof reads the parameter types `bool` and `string` as written,
        # which a package-level declaration of either name would redefine.
        return Verdict(UNPROVEN, False)
    proven, pure = carried(declaration, fields)
    if not proven:
        return Verdict(UNPROVEN, False)
    if _argument_calls(call):
        return Verdict(ARGUMENT_CALL, False)
    if not pure and not _another_call(packages, here, call, target):
        return Verdict(LAST_CALL, False)
    return Verdict(None, pure)


def deleted_with_effects(summaries, flag_key: str, repo_dir: str) -> list[tuple[str, str, str]]:
    """``(path, SIDE_EFFECTS, declaration)`` for each handle of ``flag_key``
    this run deleted although its constructor does more than build it, so
    the pull request can say the rest of that body no longer runs for this
    flag. Judged on each file's text before the run (``original_content``);
    the constructor is read from disk, and the run never edits one it proved."""
    import os

    from flag_cleanup import key_const, syntax

    rows: list[tuple[str, str, str]] = []
    for summary in summaries:
        if not summary.path.endswith(".go") or summary.original_content == summary.content:
            continue
        before = summary.original_content
        if flag_key not in before:
            continue
        root = syntax._parse(before, "go")
        data = before.encode("utf-8")
        after = syntax._parse(summary.content, "go")
        kept = {c.name for c in key_const._go_key_consts(after, summary.content.encode("utf-8"), flag_key)}
        for const in key_const._go_key_consts(root, data, flag_key):
            if not key_const.go_is_var_holder(const) or const.name in kept:
                continue
            call = key_const._go_holder_call(const)
            resolved = None if call is None else _constructor(call, summary.path)
            if resolved is None or carried(resolved[2], resolved[4]) != (True, False):
                continue
            spec = const.name_nodes[0].parent
            shown = " ".join(_text(spec if spec is not None else const.declaration).split())
            rows.append((os.path.relpath(summary.path, repo_dir), SIDE_EFFECTS, shown))
    return rows


def residue_refusals(rows, flag_key: str, repo_dir: str) -> dict[str, str]:
    """A refusal for each declaring file of ``rows`` (:func:`deleted_with_effects`)
    when anything in the checkout still spells ``flag_key`` once the rewrite
    is on disk. What the constructor did for the flag (registering it in a
    store) is gone, and code that still names the key may read exactly that:
    an ``init`` that panics when the store lacks it, say. A clean removal
    leaves no such mention; any other run leaves the handle in place.

    The mentions are the ones the run's caveat lists, found the same way
    (``orchestrate._files_matching``)."""
    if not rows:
        return {}
    import os

    from flag_cleanup import orchestrate

    left = orchestrate._files_matching(Path(repo_dir), orchestrate._RESIDUE_SUFFIXES, flag_key)
    if not left:
        return {}
    named = sorted(os.path.relpath(p, repo_dir) for p in left)
    shown = ", ".join(named[:3]) + (f" and {len(named) - 3} more" if len(named) > 3 else "")
    reason = (
        "a flag handle whose constructor does more than build it would be deleted, "
        f"and {shown} still mention{'s' if len(named) == 1 else ''} the key, which may "
        "depend on what the constructor did for it"
    )
    return {os.path.abspath(os.path.join(repo_dir, path)): reason for path, _, _ in rows}

