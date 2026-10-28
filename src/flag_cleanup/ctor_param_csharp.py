"""Prove a C# primary-constructor parameter can be removed with its arguments.

The C# counterpart of :mod:`flag_cleanup.ctor_param_ts`. A primary-constructor
parameter the fold left unread is CS9113. Removing it changes the constructor,
so every spelling of the type name in the repository's C# must be one of:
``new Svc(…)`` / ``new Ns.Svc(…)`` (the argument bound to the parameter, by
position or by name, absent or inert), a derived type's ``: Svc(…)`` or
``: base(…)`` (or none: an implicit ``base()`` passes nothing), the type's own
``: this(…)``, a type position, ``nameof``, or a
generic type argument (``AddScoped<Svc>()``: Microsoft's container resolves
constructor parameters by type, not position). ``typeof``,
``ActivatorUtilities``, a ``using`` alias and a mention in Razor or another
.NET guest file refuse.

Three ways of constructing it never spell the name where that walk looks, so
each is looked for on its own: an attribute class is applied as ``[Foo(…)]``
without its ``Attribute`` suffix, an XML-doc ``cref`` to the constructor sits
in a comment (CS1574 once the signature changes), and reflection constructs it
from a string. The last cannot be proven in general; a file holding a string
that names the type and a reflection call (``Activator.CreateInstance``,
``GetType("…")``) refuses.

A target-typed ``new(…)`` never names the type, so any one in the repository
whose argument count the constructor could accept refuses too. A caller that
can tell which files see the type (``sees``) narrows that: a file whose
project does not reach the type's cannot be given it as a target, and a
``new(…)`` whose context declares its type by another name
(:func:`_target_name`) creates that type.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index
from flag_cleanup.cross_file_dotnet_guest import DOTNET_GUEST_EXTENSIONS
from flag_cleanup.ctor_param_model import IDENT, Refusal, Removal, Site
from flag_cleanup.unused import _parse

_TYPES = frozenset({"class_declaration", "struct_declaration"})
_ACTIVATORS = frozenset({"CreateInstance", "CreateFactory", "GetServiceOrCreateInstance"})
_INERT_LEAVES = frozenset(
    {
        "string_literal", "verbatim_string_literal", "raw_string_literal", "character_literal",
        "integer_literal", "real_literal", "boolean_literal", "null_literal",
        "identifier", "this_expression", "this", "default_expression",
    }
)
#: Parents in which the type name is a type, never a construction.
_TYPE_POSITIONS = frozenset(
    {
        "variable_declaration", "parameter", "property_declaration", "method_declaration",
        "nullable_type", "array_type", "cast_expression", "type_parameter_constraint",
        "is_pattern_expression", "declaration_pattern", "type_pattern", "recursive_pattern",
        "as_expression", "local_function_statement", "delegate_declaration",
        "indexer_declaration", "operator_declaration", "conversion_operator_declaration",
        "event_field_declaration", "ref_type", "tuple_element", "catch_declaration",
        "foreach_statement", "local_declaration_statement", "field_declaration",
    }
)
#: Tokens in an ``argument`` that are not its expression.
_ARGUMENT_TOKENS = frozenset({":", "ref", "out", "in"})
_NEW_CALL = re.compile(rb"\bnew\s*\(")
_STRINGS = frozenset({"string_literal", "verbatim_string_literal", "raw_string_literal"})
_SUFFIX = "Attribute"


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _inert(node: Node) -> bool:
    """Whether evaluating ``node`` as an argument can do nothing but read."""
    if node.type in _INERT_LEAVES:
        return True
    if node.type in ("parenthesized_expression", "postfix_unary_expression"):
        return bool(node.named_children) and _inert(node.named_children[0])
    if node.type == "cast_expression":
        value = node.child_by_field_name("value")
        return value is not None and _inert(value)
    if node.type == "member_access_expression":
        expression = node.child_by_field_name("expression")
        name = node.child_by_field_name("name")
        # A type's static member (`NullLogger<T>.Instance`, `string.Empty`)
        # is a read like a value's member.
        return (
            expression is not None
            and name is not None
            and name.type == "identifier"
            and (expression.type in ("generic_name", "predefined_type") or _inert(expression))
        )
    return False


#: Literals that convert to no interface the checkout declares: ``int``,
#: ``string`` and the rest implement only library interfaces, and an
#: interface declares no conversion.
_LITERALS = frozenset(
    {
        "integer_literal", "real_literal", "boolean_literal", "character_literal", "string_literal",
        "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression",
    }
)


def _literal_for_an_interface(arguments: Node, interfaces: frozenset[int]) -> bool:
    """Whether a target-typed ``new(…)``'s positional arguments pass a
    literal (:data:`_LITERALS`, a negated number among them) where the
    constructor takes one of the checkout's interfaces (``interfaces``, by
    position): then it cannot be calling that constructor. Never with a named
    argument, whose position this does not follow."""
    items = _items(arguments)
    if not interfaces or any(i.child_by_field_name("name") is not None for i in items):
        return False
    for position in interfaces:
        value = _expression(items[position]) if position < len(items) else None
        if value is not None and value.type == "prefix_unary_expression" and _text(value).startswith("-") and value.named_children:
            value = value.named_children[-1]
        if value is not None and (value.type in _LITERALS or _builtin_constant(value)):
            return True
    return False


#: Types a ``const`` can have, none of which converts to an interface the
#: checkout declares.
_CONST_TYPES = frozenset(
    {"string", "bool", "char", "byte", "sbyte", "short", "ushort", "int", "uint", "long", "ulong", "float",
     "double", "decimal", "nint", "nuint"}
)


def _builtin_constant(name: Node) -> bool:
    """Whether the identifier ``name`` can only be a ``const`` field of a
    predefined type (``private const string ExceptionType = "…";``) that the
    innermost type enclosing it declares: that type has no primary-constructor
    parameter of the name, and the member holding ``name`` binds the name
    nowhere (:func:`_binds`), so no local, parameter, pattern, query or loop
    variable can be what it reads. A const of an outer type is never taken:
    the inner type's own, inherited and primary-constructor names win."""
    if name.type != "identifier":
        return False
    spelled = _text(name).removeprefix("@")
    member, owner = _member_and_owner(name)
    if owner is None or member is None or owner.type not in _CONST_OWNERS or _binds(member, spelled):
        return False
    header = next((c for c in owner.children if c.type == "parameter_list"), None)
    if header is not None and _binds(header, spelled):
        return False
    body = owner.child_by_field_name("body")
    for declared in body.named_children if body is not None else ():
        if declared.type != "field_declaration" or "const" not in _modifiers(declared):
            continue
        declaration = next((c for c in declared.named_children if c.type == "variable_declaration"), None)
        if declaration is None or spelled not in _declared_by(declaration):
            continue
        type_node = declaration.child_by_field_name("type")
        return type_node is not None and type_node.type == "predefined_type" and _text(type_node) in _CONST_TYPES
    return False


#: Type declarations, any of which bounds the members a name is looked up in.
_TYPE_DECLARATIONS = frozenset(
    {"class_declaration", "struct_declaration", "record_declaration", "record_struct_declaration",
     "interface_declaration", "enum_declaration", "extension_declaration"}
)


def _member_and_owner(at: Node) -> tuple[Node | None, Node | None]:
    """The member declaration holding ``at`` and the innermost type that
    declares it; for a top-level statement, the compilation unit and None."""
    node = at
    while node.parent is not None:
        parent = node.parent
        if parent.type == "declaration_list" and parent.parent is not None and parent.parent.type in _TYPE_DECLARATIONS:
            return node, parent.parent
        if parent.type in _TYPE_DECLARATIONS:
            return node, parent
        node = parent
    return node, None


#: Parents whose identifier child binds a name whatever field holds it.
_BINDING_PARENTS = frozenset({"tuple_pattern", "let_clause", "join_clause", "join_into_clause", "query_continuation"})


def _bindings(scope: Node, spelled: str) -> int:
    """How many places in ``scope`` may bind the name ``spelled``: an
    identifier in a ``name`` field (declarators, parameters, patterns,
    ``out var``, deconstructions, ``from``, ``catch``, local functions; and,
    over-counting on purpose, member names and named arguments), a
    ``foreach`` variable, a range variable of the other query clauses, a
    deconstruction element and an implicit lambda parameter."""
    count = 0
    for n in _walk(scope):
        if n.type == "implicit_parameter":
            count += _text(n).removeprefix("@") == spelled
            continue
        if n.type != "identifier" or _text(n).removeprefix("@") != spelled or n.parent is None:
            continue
        parent = n.parent
        index = next(i for i, c in enumerate(parent.children) if c.id == n.id)
        field = parent.field_name_for_child(index)
        if field == "name" or (field == "left" and parent.type == "foreach_statement") or parent.type in _BINDING_PARENTS:
            count += 1
    return count


def _binds(scope: Node, spelled: str) -> bool:
    return _bindings(scope, spelled) > 0


def _declared_by(node: Node, header_only: bool = False) -> set[str]:
    """The names ``node`` declares: its variable declarators (in its header
    only, with ``header_only``), parameters, implicit lambda parameter,
    ``foreach`` variable and ``catch`` variable."""
    found: set[str] = set()
    parameters = node.child_by_field_name("parameters")
    if parameters is not None:
        if parameters.type in ("identifier", "implicit_parameter"):
            found.add(_text(parameters))
        for p in parameters.named_children:
            label = p.child_by_field_name("name") if p.type == "parameter" else (p if p.type == "implicit_parameter" else None)
            if label is not None:
                found.add(_text(label))
    left = node.child_by_field_name("left") if node.type == "foreach_statement" else None
    if left is not None and left.type == "identifier":
        found.add(_text(left))
    scope = [c for c in node.children if c.type in ("variable_declaration", "catch_declaration")] if header_only else [node]
    for part in scope:
        for n in _walk(part):
            if n.type == "variable_declarator" and (d := n.child_by_field_name("name")) is not None:
                found.add(_text(d))
            elif n.type == "catch_declaration" and (d := n.child_by_field_name("name")) is not None:
                found.add(_text(d))
    return found


#: Constructs that bind names of their own around the code inside them.
_BINDERS = frozenset(
    {"method_declaration", "constructor_declaration", "local_function_statement", "lambda_expression",
     "anonymous_method_expression", "foreach_statement", "for_statement", "using_statement", "catch_clause",
     "accessor_declaration", "operator_declaration", "conversion_operator_declaration", "indexer_declaration"}
)
#: Types whose ``const`` fields an unqualified name inside them may read.
_CONST_OWNERS = frozenset(
    {"class_declaration", "struct_declaration", "record_declaration", "record_struct_declaration", "interface_declaration"}
)


def _interface_positions(parameter_list: Node | None, root: Node, path: str, repo_dir: str) -> frozenset[int]:
    """The positions of the parameters typed as an interface the checkout
    declares, resolved by C#'s lookup from the declaring file."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import wrapper_csharp as wc

    if parameter_list is None:
        return frozenset()
    reader = xcs._Reader(root, path, repo_dir)
    readers = wc._Readers(repo_dir)
    checkout = xcs.csharp_index(repo_dir)
    found = set()
    for position, parameter in enumerate(_parameters(parameter_list)):
        type_node = parameter.child_by_field_name("type")
        if type_node is not None and type_node.type == "nullable_type" and type_node.named_children:
            type_node = type_node.named_children[0]
        full = wc._type_full(type_node, reader) if type_node is not None else None
        infos = [i for i in checkout.by_full.get(full or "", ()) if not i.generic]
        if len(infos) != 1:
            continue
        declaring = readers.get(infos[0].path)
        declaration = wc._find_type(declaring.root, infos[0].chain) if declaring is not None else None
        if declaration is not None and declaration.type == "interface_declaration":
            found.add(position)
    return frozenset(found)


def _items(argument_list: Node) -> list[Node]:
    return [c for c in argument_list.named_children if c.type == "argument"]


def _expression(argument: Node) -> Node | None:
    """The argument's value. ``this`` is an anonymous token here, so it is
    looked for among all children, not the named ones."""
    name = argument.child_by_field_name("name")
    parts = [c for c in argument.children if c != name and c.type not in _ARGUMENT_TOKENS]
    if len(parts) != 1 or any(c.type in ("ref", "out", "in") for c in argument.children):
        return None
    return parts[0]


def _bound(items: list[Node], index: int, names: tuple[str, ...]) -> Node | str | None:
    """The argument bound to parameter ``index``, ``None`` when the call
    passes none (the parameter has a default), or why it cannot be told.

    A named argument binds by name. A positional one binds by position, which
    C# allows after a named argument only while every named argument so far
    sits in its own position, so a positional argument after one that does
    not is refused rather than guessed at. Deleting the bound argument keeps
    every in-position named argument in position, because the parameter
    before it goes with it."""
    bound: Node | None = None
    displaced = False
    for position, item in enumerate(items):
        name = item.child_by_field_name("name")
        if name is None:
            if displaced:
                return "a positional argument after an out-of-position named argument"
            slot = position
        else:
            spelled = _text(name).removeprefix("@")
            if spelled not in names:
                return f"a named argument for no parameter of the primary constructor ({spelled})"
            slot = names.index(spelled)
            displaced = displaced or slot != position
        if slot == index:
            if bound is not None:
                return "two arguments for the parameter"
            bound = item
    return bound


def _site(
    path: str,
    argument_list: Node | None,
    index: int,
    names: tuple[str, ...],
    sites: list[Site],
    inert: Callable[[Node], bool] = _inert,
) -> str | None:
    if argument_list is None:
        return None
    argument = _bound(_items(argument_list), index, names)
    if argument is None or isinstance(argument, str):
        return argument
    expression = _expression(argument)
    if expression is None:
        return "an argument with a modifier"
    if not inert(expression):
        return f"an argument that may do work ({' '.join(_text(expression).split())[:60]})"
    sites.append(Site(path, expression.start_byte, expression.end_byte))
    return None


def _primary(declaration: Node) -> Node | None:
    return next((c for c in declaration.children if c.type == "parameter_list"), None)


def _declarations(root: Node, name: str) -> list[Node]:
    return [
        n
        for n in _walk(root)
        if n.type in _TYPES and (cn := n.child_by_field_name("name")) is not None and _text(cn) == name
    ]


def _parameters(parameter_list: Node) -> list[Node]:
    return [c for c in parameter_list.named_children if c.type == "parameter"]


def has_parameter(source: str, class_name: str, param_name: str, classic: bool = False) -> bool:
    """Whether ``class_name`` in ``source`` still declares the parameter."""
    return bool(parameter_spans(source, class_name, param_name, classic))


def parameter_spans(
    source: str, class_name: str, param_name: str, classic: bool = False
) -> list[tuple[int, int]]:
    """The byte span of each declaration of the parameter in ``source``: in
    the primary constructor, or with ``classic`` in an explicit one."""
    spans = []
    for declaration in _declarations(_parse(source, "csharp"), class_name):
        lists = (
            [c.child_by_field_name("parameters") for c in _constructors(declaration)]
            if classic
            else [_primary(declaration)]
        )
        for parameters in lists:
            for item in _parameters(parameters) if parameters is not None else []:
                name = item.child_by_field_name("name")
                if name is not None and _text(name) == param_name:
                    spans.append((item.start_byte, item.end_byte))
    return spans


def _constructors(declaration: Node) -> list[Node]:
    """The explicit instance constructors ``declaration`` declares."""
    body = declaration.child_by_field_name("body")
    return [
        member
        for member in (body.named_children if body is not None else [])
        if member.type == "constructor_declaration"
        and not any(c.type == "modifier" and _text(c) == "static" for c in member.children)
    ]


def _argument_list(node: Node) -> Node | None:
    return next((c for c in node.children if c.type == "argument_list"), None)


def _derived(
    base_list: Node,
    path: str,
    index: int,
    names: tuple[str, ...],
    sites: list[Site],
    inert: Callable[[Node], bool] = _inert,
) -> str | None:
    """A derived type naming the class with no arguments in its base list:
    each ``base(…)`` its constructors pass is a site.

    A constructor without an initializer, and the one C# supplies to a type
    that declares none, calls ``base()`` with no arguments: nothing to delete,
    and ``_overloads`` keeps it binding where it did. C# constructors are not
    inherited, so ``new Derived(…)`` never reaches this one. A partial type may
    declare a constructor in a part this does not read."""
    derived = base_list.parent
    if derived is None:
        return "an unexpected position"
    if any(c.type == "modifier" and _text(c) == "partial" for c in derived.children):
        return "a partial derived type, whose constructors may be in another part"
    body = derived.child_by_field_name("body")
    ctors = [
        m for m in (body.named_children if body is not None else []) if m.type == "constructor_declaration"
    ]
    for ctor in ctors:
        initializer = next((c for c in ctor.children if c.type == "constructor_initializer"), None)
        if initializer is None or any(c.type == "this" for c in initializer.children):
            continue
        reason = _site(path, _argument_list(initializer), index, names, sites, inert)
        if reason is not None:
            return reason
    return None


def _inside(node: Node, decl_start: int) -> bool:
    current = node.parent
    while current is not None:
        if current.type in _TYPES and current.start_byte == decl_start:
            return True
        current = current.parent
    return False


_GLOBAL_USING = re.compile(rb"^[ \t]*global[ \t]+using[ \t]+(?!static\b)([\w.]+)[ \t]*;", re.MULTILINE)
_MSBUILD_USING = re.compile(rb"<Using\s+Include=\"([\w.]+)\"(?![^>]*\b(?:Alias|Static)=)")
#: Arguments a generic call may pass without constructing the type itself.
_FACTORIES = frozenset({"lambda_expression", "anonymous_method_expression"})


def _namespace_of(node: Node) -> str:
    """The namespace ``node`` sits in, ``""`` for the global one."""
    parts: list[str] = []
    current = node.parent
    root = node
    while current is not None:
        if current.type == "namespace_declaration":
            name = current.child_by_field_name("name")
            if name is not None:
                parts.append(_text(name))
        root = current
        current = current.parent
    scoped = next(
        (c for c in root.named_children if c.type == "file_scoped_namespace_declaration"), None
    )
    name = scoped.child_by_field_name("name") if scoped is not None else None
    return ".".join(([_text(name)] if name is not None else []) + list(reversed(parts)))


def _usings(root: Node) -> set[str]:
    """The namespaces a file imports with a plain ``using`` (not an alias,
    not ``using static``), wherever in the file it sits."""
    found: set[str] = set()
    for node in _walk(root):
        if node.type != "using_directive" or node.child_by_field_name("name") is not None:
            continue
        if any(c.type == "static" for c in node.children) or not node.named_children:
            continue
        found.add(_text(node.named_children[-1]))
    return found


def _global_usings(repo_dir: str) -> set[str]:
    """``global using`` directives and MSBuild ``<Using Include>`` items: a
    namespace every C# file of the project sees."""
    found: set[str] = set()
    for file in source_index.files(repo_dir):
        suffix = file.path.suffix
        if suffix not in (".cs", ".csproj", ".props", ".targets"):
            continue
        try:
            data = file.path.read_bytes()
        except OSError:
            continue
        pattern = _GLOBAL_USING if suffix == ".cs" else _MSBUILD_USING
        found.update(m.group(1).decode() for m in pattern.finditer(data))
    return found


def _sees(qualifier: str, namespace: str) -> bool:
    """Whether ``qualifier`` (``App.Services``, ``Services`` from inside
    ``App``, ``global::App.Services``) names ``namespace``."""
    qualifier = qualifier.removeprefix("global::")
    return qualifier == namespace or namespace.endswith("." + qualifier)


def _generic_call_arguments(generic: Node | None) -> Node | None:
    """The argument list of the call or creation a generic name is the
    callee or type of: ``new Mock<Svc>(…)``, ``Substitute.ForPartsOf<Svc>(…)``."""
    holder = generic.parent if generic is not None else None
    if holder is None:
        return None
    if holder.type == "object_creation_expression" and holder.child_by_field_name("type") == generic:
        return holder.child_by_field_name("arguments")
    if holder.type == "invocation_expression" and holder.child_by_field_name("function") == generic:
        return holder.child_by_field_name("arguments")
    if (
        holder.type == "member_access_expression"
        and holder.child_by_field_name("name") == generic
        and holder.parent is not None
        and holder.parent.type == "invocation_expression"
    ):
        return holder.parent.child_by_field_name("arguments")
    return None


def _classify(
    node: Node,
    path: str,
    decl_start: int | None,
    index: int,
    names: tuple[str, ...],
    sites: list[Site],
    namespace: str = "",
    visible: bool = True,
    inert: Callable[[Node], bool] = _inert,
    typeof: Callable[[Node], str | None] | None = None,
) -> str | None:
    """``None`` when this spelling of the type name is accounted for (and
    records its argument, if it is a call), else why it is not. A caller
    with ``typeof`` judges a ``typeof(…)`` of it instead of refusing it.

    ``namespace`` is the declaring namespace; ``visible`` says whether an
    unqualified spelling here can resolve to it (the file sits in it, or
    imports it)."""
    current = node
    qualifier: str | None = None
    while current.parent is not None and current.parent.type in ("qualified_name", "alias_qualified_name"):
        if current.parent.child_by_field_name("name") != current:
            return None  # `Svc.Nested`: the type as a qualifier
        if qualifier is None:
            left = current.parent.child_by_field_name(
                "qualifier" if current.parent.type == "qualified_name" else "alias"
            )
            qualifier = _text(left) if left is not None else ""
            if current.parent.type == "alias_qualified_name":
                qualifier += "::"
        current = current.parent
    parent = current.parent
    if parent is None:
        return "an unexpected position"
    kind = parent.type
    named = parent.child_by_field_name("name") == current
    if kind == "using_directive":
        return "a using alias"
    if qualifier is not None and not named:
        if not _sees(qualifier, namespace) and qualifier.removesuffix("::") != "global":
            return f"qualified with another namespace ({qualifier})"
        if qualifier.removesuffix("::") == "global" and namespace:
            return f"qualified with another namespace ({qualifier})"
    elif not visible and not named:
        return f"a file that cannot see {namespace or 'the global namespace'}"
    if named and kind in _TYPES:
        return None if parent.start_byte == decl_start else "another type of the same name"
    if kind == "constructor_declaration" and named:
        if decl_start is None or not _inside(parent, decl_start):
            return "a constructor of another type of the same name"
        initializer = next((c for c in parent.children if c.type == "constructor_initializer"), None)
        if initializer is None or not any(c.type == "this" for c in initializer.children):
            return None
        return _site(path, _argument_list(initializer), index, names, sites, inert)
    if kind == "attribute" and named:
        return "used as an attribute, which constructs it with positional arguments"
    if named:
        return f"another declaration of the same name ({kind})"
    if kind == "object_creation_expression" and parent.child_by_field_name("type") == current:
        return _site(path, parent.child_by_field_name("arguments"), index, names, sites, inert)
    if kind == "primary_constructor_base_type":
        return _site(path, _argument_list(parent), index, names, sites, inert)
    if kind == "base_list":
        # `class D(...) : Svc(a, b)`: this grammar puts the arguments in an
        # `argument_list` right after the base type, inside the base list.
        following = current.next_named_sibling
        if following is not None and following.type == "argument_list":
            return _site(path, following, index, names, sites, inert)
        return _derived(parent, path, index, names, sites, inert)
    if kind == "typeof_expression":
        reason = typeof(parent) if typeof is not None else None
        return None if typeof is not None and reason is None else (
            "typeof (reflection can construct it positionally)" + (f": {reason}" if reason else "")
        )
    if kind == "type_argument_list":
        generic = parent.parent
        callee = generic.named_children[0] if generic is not None and generic.named_children else None
        if callee is not None and _text(callee) in _ACTIVATORS:
            return "ActivatorUtilities constructs it positionally"
        arguments = _generic_call_arguments(generic)
        if arguments is not None and any(
            (e := _expression(a)) is None or e.type not in _FACTORIES for a in _items(arguments)
        ):
            return "a generic call passing arguments, which a mock or proxy can use to construct it positionally"
        return None
    if kind == "argument":
        invocation = parent.parent.parent if parent.parent is not None else None
        function = invocation.child_by_field_name("function") if invocation is not None else None
        if function is not None and _text(function) == "nameof":
            return None
    if kind == "member_access_expression":
        if parent.child_by_field_name("expression") == current:
            return None  # static member access
        return "a member of the same name"
    if kind in _TYPE_POSITIONS:
        return None
    return f"used as {kind}"


def _attribute(node: Node) -> bool:
    """Whether ``node`` names an attribute, qualified or not: ``[Foo(…)]``."""
    current = node
    while current.parent is not None and current.parent.type in ("qualified_name", "alias_qualified_name"):
        if current.parent.child_by_field_name("name") != current:
            return False
        current = current.parent
    parent = current.parent
    return parent is not None and parent.type == "attribute" and parent.child_by_field_name("name") == current


def _literal(node: Node) -> str:
    """The text a string literal holds, escapes left as written."""
    if node.type == "verbatim_string_literal":
        return _text(node)[2:-1]
    content = ("string_literal_content", "raw_string_content", "escape_sequence")
    return "".join(_text(c) for c in node.children if c.type in content)


def _reflects(node: Node) -> bool:
    """A call that constructs or looks up a type by name: ``CreateInstance…``,
    or a ``GetType`` given an argument (``obj.GetType()`` is not one)."""
    if node.type != "invocation_expression":
        return False
    function = node.child_by_field_name("function")
    if function is not None and function.type == "member_access_expression":
        function = function.child_by_field_name("name")
    if function is None or function.type not in ("identifier", "generic_name"):
        return False
    callee = _text(function.named_children[0] if function.type == "generic_name" else function)
    arguments = node.child_by_field_name("arguments")
    return callee.startswith("CreateInstance") or (
        callee == "GetType" and arguments is not None and bool(_items(arguments))
    )


def _declared(parameter_list: Node | None) -> bool:
    """Whether a parameter list declares anything. A ``params`` array is not
    a ``parameter`` node, so any named child counts."""
    return parameter_list is not None and any(c.type != "comment" for c in parameter_list.named_children)


def _overloads(declaration: Node, primary: Node, removed: Node) -> str | None:
    """Why another constructor makes the removal unprovable, if one does.

    Every call is bound to the primary constructor, which holds only while no
    explicit constructor takes arguments: one that does could be the call's
    target (overload resolution is by type, which this does not see), and the
    removal could make it collide with the primary one or win a call the
    primary one used to. A parameterless one cannot take a call with
    arguments, but once every parameter left is optional a call can be left
    with none: ``new Svc(c)`` becomes ``new Svc()``, which binds to the
    explicit one (C# prefers a candidate needing no defaults), and
    ``this(c)`` inside it becomes ``this()``, a call to itself. A struct
    always has a parameterless ``new S()``, which is the zero value unless the
    primary constructor declares no parameters at all, so a call left with
    none can stop running the primary constructor (and every field
    initialiser), or one already passing none can start."""
    body = declaration.child_by_field_name("body")
    explicit = [
        member
        for member in (body.named_children if body is not None else [])
        if member.type == "constructor_declaration"
        and not any(c.type == "modifier" and _text(c) == "static" for c in member.children)
    ]
    if any(_declared(member.child_by_field_name("parameters")) for member in explicit):
        return "an explicit constructor with parameters (a call may bind to it instead)"
    if any(not _optional(item) for item in _parameters(primary) if item != removed):
        return None
    if explicit:
        return "an explicit parameterless constructor a call left with no arguments would bind to"
    if declaration.type == "struct_declaration":
        return "a struct whose primary constructor would need no arguments (new S() could stop or start running it)"
    return None


def _names(parameter_list: Node) -> tuple[str, ...]:
    """Each parameter's name as a named argument spells it, by position."""
    return tuple(
        _text(name).removeprefix("@") if (name := item.child_by_field_name("name")) is not None else ""
        for item in _parameters(parameter_list)
    )


def _arity(parameter_list: Node) -> tuple[int, float]:
    """The fewest and most arguments a call can pass. A ``params`` array is
    not a ``parameter`` node in this grammar; it makes the most unbounded."""
    parameters = _parameters(parameter_list)
    unbounded = any(c.type != "parameter" for c in parameter_list.named_children if c.type != "comment")
    required = sum(not _optional(item) for item in parameters)
    return required, float("inf") if unbounded else float(len(parameters))


def _optional(parameter: Node) -> bool:
    """Whether a call may leave the parameter out: it has a default value (a
    bare expression child), or an attribute, which may be ``[Optional]`` under
    any alias and is not resolved here."""
    fixed = {parameter.child_by_field_name("type"), parameter.child_by_field_name("name")}
    return any(
        c.type == "attribute_list" or (c.is_named and c not in fixed and c.type != "modifier")
        for c in parameter.children
    )


def plan(path: str, source: str, label: str, repo_dir: str) -> Removal | Refusal:
    """Prove the primary-constructor parameter ``label`` (``Svc(IClient
    client)``) of the file at ``path``, whose current text is ``source``, can
    be removed with every caller's argument."""
    class_name = label.partition("(")[0]
    # `_references` parses each file without its byte-order mark, so the
    # declaration's offset must be measured the same way to recognise it.
    root = _parse(source.removeprefix("\ufeff"), "csharp")
    if root.has_error:
        return Refusal(label, "the declaring file does not parse")
    declarations = _declarations(root, class_name)
    if len(declarations) != 1:
        return Refusal(label, "the type is declared more than once in its file")
    declaration = declarations[0]
    parameters = _primary(declaration)
    found = None
    for index, item in enumerate(_parameters(parameters) if parameters is not None else []):
        if f"{class_name}({_text(item)})" == label:
            found = (index, item)
    if found is None or parameters is None:
        return Refusal(label, "no longer a primary-constructor parameter", quiet=True)
    if any(c.type == "type_parameter_list" for c in declaration.children):
        return Refusal(label, "a generic type")
    index, parameter = found
    reason = _overloads(declaration, parameters, parameter)
    if reason is not None:
        return Refusal(label, reason)
    name_node = parameter.child_by_field_name("name")
    param_name = _text(name_node) if name_node is not None else ""
    if IDENT.fullmatch(class_name) is None or IDENT.fullmatch(param_name) is None:
        return Refusal(label, "a name the deletion rules cannot spell")
    if any(c.type == "attribute_list" for c in parameter.children):
        return Refusal(label, "an attribute on the parameter (keyed or explicit injection)")
    if any(c.type == "modifier" for c in parameter.children):
        return Refusal(label, "a parameter modifier (ref, out, in, this, scoped)")
    from flag_cleanup import csharp_family

    exported = any(c.type == "modifier" and _text(c) == "public" for c in declaration.children)
    sites, reason = _references(
        class_name,
        path,
        declaration.start_byte,
        index,
        _names(parameters),
        _arity(parameters),
        repo_dir,
        _namespace_of(declaration),
        # A project that cannot reference the type's cannot construct it.
        sees=lambda caller: csharp_family.sees(repo_dir, caller, path),
        interfaces=_interface_positions(parameters, root, path, repo_dir),
        inert_in=lambda node, caller: _plain_construction(node, repo_dir, caller),
    )
    if reason is not None:
        return Refusal(label, reason)
    return Removal(path, class_name, param_name, label, index, tuple(sites), exported)


def _references(
    name: str,
    decl_path: str,
    decl_start: int,
    index: int,
    names: tuple[str, ...],
    arity: tuple[int, float],
    repo_dir: str,
    namespace: str = "",
    inert: Callable[[Node], bool] = _inert,
    sees: Callable[[str], bool] | None = None,
    interfaces: frozenset[int] = frozenset(),
    inert_in: Callable[[Node, str], bool] | None = None,
) -> tuple[list[Site], str | None]:
    """Every site that spells the type ``name``, or a reason it cannot be
    proven: see the module docstring."""
    # `[Foo(…)]` applies `FooAttribute`: both spellings name it.
    short = name.removesuffix(_SUFFIX) if name.endswith(_SUFFIX) and name != _SUFFIX else None
    spellings = "|".join(re.escape(n) for n in (name, short) if n)
    word = re.compile(rf"(?<![\w@])(?:{spellings})(?!\w)".encode())
    cref = re.compile(
        rf"""cref\s*=\s*["'][^"']*?(?<![\w@]){re.escape(name)}(?:\s*\(|\.#ctor\b)""".encode()
    )
    naming = re.compile(rf"(?:global::)?(?:[\w.]*[.+])?{re.escape(name)}(?:\s*,.*)?", re.DOTALL)
    guests = DOTNET_GUEST_EXTENSIONS["dotnet_guest"]
    decl = str(Path(decl_path).resolve())
    sites: list[Site] = []
    everywhere = _global_usings(repo_dir) if namespace else set()
    #: Target-typed ``new(…)``s whose context names another type: the type
    #: name, and where.
    targeted: list[tuple[str, str]] = []
    aliases: set[str] = set()
    base_inert = inert
    for file in source_index.files(repo_dir, with_ignored=True):
        spelled = str(file.path)
        # An argument may also be judged by what the file it is in names.
        inert = (
            (lambda node, path=file.resolved: base_inert(node) or inert_in(node, path))
            if inert_in is not None
            else base_inert
        )
        is_cs = spelled.endswith(".cs")
        if not is_cs and not spelled.endswith(guests):
            continue
        try:
            data = file.path.read_bytes()
        except OSError:
            return sites, f"{file.path} could not be read"
        mentions = word.search(data) is not None
        if is_cs and b"using" in data:
            # Every word between a `using` and its `=`: `unsafe`, a comment
            # or an escape between them only adds names.
            for alias in _ALIAS.finditer(_decoded(data.decode("utf-8", "replace"))):
                aliases.update(_WORD.findall(alias.group(1)))
        if not is_cs:
            if mentions:
                return sites, f"{file.path} mentions it (razor, cshtml or another .NET guest file)"
            continue
        if not mentions and not _NEW_CALL.search(data):
            continue
        if not mentions and sees is not None and not sees(file.resolved):
            # Naming nothing, its `new(…)`s build no type its project cannot see.
            continue
        if file.ignored and mentions:
            return sites, f"{file.path} mentions it and is ignored (not the repository's source)"
        offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        try:
            text = data[offset:].decode("utf-8")
        except UnicodeDecodeError:
            return sites, f"{file.path} is not UTF-8"
        root = _parse(text, "csharp")
        if root.has_error:
            return sites, f"{file.path} does not parse"
        start = decl_start if file.resolved == decl else None
        imports = (_usings(root) | everywhere) if namespace else set()
        local: list[Site] = []
        named_at: Node | None = None
        reflects = False
        for node in _walk(root):
            if node.type == "comment":
                if cref.search(node.text):
                    return sites, (
                        f"{file.path}:{node.start_point[0] + 1}: "
                        "an XML-doc cref names its constructor (CS1574 once the signature changes)"
                    )
                continue
            if node.type in _STRINGS:
                if named_at is None and naming.fullmatch(_literal(node).strip()):
                    named_at = node
                continue
            reflects = reflects or _reflects(node)
            if short is not None and node.type == "identifier" and _text(node) == short and _attribute(node):
                return sites, (
                    f"{file.path}:{node.start_point[0] + 1}: "
                    "used as an attribute, which constructs it with positional arguments"
                )
            if node.type == "implicit_object_creation_expression":
                args = _argument_list(node)
                count = len(_items(args)) if args is not None else 0
                if (
                    arity[0] <= count <= arity[1]
                    and (sees is None or sees(file.resolved))
                    and not (args is not None and _literal_for_an_interface(args, interfaces))
                ):
                    where = f"{file.path}:{node.start_point[0] + 1}"
                    target = _target_name(node, repo_dir, file.resolved) if sees is not None else None
                    if target is None:
                        return sites, f"{where}: a target-typed new(…) could construct it"
                    if target == _decoded(name):
                        # Its context declares this very type: a construction
                        # like `new Svc(…)`, its argument edited the same way.
                        # The declared type's own spelling is judged by this
                        # walk like any other, which compares it as written:
                        # a file spelling a name with an escape is refused.
                        if b"\\u" in data or b"\\U" in data:
                            return sites, f"{where}: a target-typed new(…) could construct it"
                        reason = _site(spelled, args, index, names, local, inert)
                        if reason is not None:
                            return sites, f"{where}: {reason}"
                    targeted.append((target, where))
                continue
            if node.type != "identifier" or _text(node) != name:
                continue
            here = _namespace_of(node)
            visible = (
                not namespace
                or start is not None
                or namespace in imports
                or here == namespace
                or here.startswith(namespace + ".")
            )
            reason = _classify(
                node, spelled, start, index, names, local, namespace, visible, inert,
                (lambda t, root=root: _compared_type(t, root, repo_dir))
                if sees is not None else None,
            )
            if reason is not None:
                return sites, f"{file.path}:{node.start_point[0] + 1}: {reason}"
        if named_at is not None and reflects:
            return sites, (
                f"{file.path}:{named_at.start_point[0] + 1}: "
                "a string naming the type, in a file that constructs by reflection"
            )
        # Offsets into the file's bytes, BOM included.
        sites.extend(Site(s.path, s.start + offset, s.end + offset) for s in local)
    if targeted:
        items, readable = _xcs().msbuild_usings(repo_dir)
        aliases |= {_decoded(i.alias) for i in items if i.alias}
        for target, where in targeted:
            if not readable or target in aliases:
                return sites, f"{where}: a target-typed new(…) could construct it (its type may be an alias)"
    return sites, None


# --- a typeof kept by an attribute only to be compared ---------------------
#
# `[Covers(typeof(Job))]` hands the attribute a `Type`, which reflection
# could construct positionally. It cannot when the attribute is the
# checkout's own, keeps the value only in a get-only property, and every
# read of that property compares it; when nothing that can see the
# attribute reads attribute data raw, takes an attribute but as a named
# type, or reads a property's value by reflection; and when no string there
# names the property.

#: Reads of attribute data that bypass the attribute's own members, and
#: reflective reads of a member's value. Matched on decoded text, ``Get…``
#: prefixes included (``GetConstructorArguments``).
_RAW_ATTRIBUTES = re.compile(
    r"CustomAttributeData|ConstructorArguments|GetCustomAttributesData"
    r"|\b(?:CustomAttributes|GetAttributes|GetValue|TypeDescriptor|PropertyDescriptor|GetGetMethod|GetAccessors|CreateDelegate|InvokeMember"
    r"|MakeMemberAccess|dynamic)\b"
    r"|\.\s*GetMethod\b(?!\s*[(<])"
    r"|\bExpression\s*\.\s*(?:Property|PropertyOrField)\b"
)
#: Method lookups whose result a file could invoke on an attribute.
_METHOD_LOOKUPS = frozenset(
    {"GetMethod", "GetMethods", "GetRuntimeMethod", "GetRuntimeMethods", "GetDeclaredMethod",
     "GetDeclaredMethods", "DeclaredMethods", "GetMember", "GetMembers", "FindMembers"}
)
_METHOD_LOOKUP = re.compile(r"\b(?:" + "|".join(sorted(_METHOD_LOOKUPS)) + r")\b")
_INVOKE = re.compile(r"\bInvoke\b")
#: What reaches an assembly's types without a project reference: loading
#: it at run time, a type by name, or a source generator.
_LOADS = re.compile(
    r"\b(?:Assembly\s*\.\s*(?:Load\w*|UnsafeLoadFrom)|LoadFrom\w*|LoadFile|AssemblyLoadContext|MetadataLoadContext"
    r"|ExecuteAssembly\w*|CreateInstanceFrom\w*|AppDomain|ReflectionOnly\w*)\b"
    r"|\bGetType\s*\(\s*[^)\s]"
)
#: A source generator, which may write code constructing what an
#: attribute's data names. An analyzer only reports.
_GENERATOR = re.compile(r"\b(?:ISourceGenerator|IIncrementalGenerator)\b|\[\s*Generator\b")
_RETRIEVALS = frozenset({"GetCustomAttribute", "GetCustomAttributes"})
#: Type arguments that take every attribute.
_ANY_ATTRIBUTE = frozenset({"Attribute", "object", "Object", "dynamic"})


def _compared_type(node: Node, root: Node, repo_dir: str) -> str | None:
    """``None`` when the ``typeof(…)`` ``node`` (in a file parsed as
    ``root``) is a positional argument of an attribute that keeps it only to
    be compared (see above); else why not."""
    from flag_cleanup import csharp_family

    argument = node.parent
    arguments = argument.parent if argument is not None and argument.type == "attribute_argument" else None
    attribute = arguments.parent if arguments is not None and arguments.type == "attribute_argument_list" else None
    if attribute is None or attribute.type != "attribute" or [c.id for c in argument.named_children] != [node.id]:
        return "not an attribute's positional argument"
    positional = [c for c in arguments.named_children if c.type == "attribute_argument"]
    if any(c.child_by_field_name("name") is not None for c in positional):
        return "the attribute names an argument"
    # Unqualified only: a qualifier may name another assembly's attribute.
    name = attribute.child_by_field_name("name")
    spelled = _simple(name) if name is not None and name.type == "identifier" else None
    if spelled is None:
        return "the attribute's name is not a plain one"
    index = _xcs().csharp_index(repo_dir)
    candidates = {spelled, spelled + _SUFFIX}
    infos = [i for n in candidates for i in index.by_last.get(n, ())]
    if (
        len(infos) != 1
        or index.unreadable
        or any(words is None or words & candidates for words in index.unparsed_types)
        or "." in infos[0].chain
    ):
        return "the attribute is not one type of the checkout"
    info = infos[0]
    here = _namespace_of(attribute)
    if info.namespace and here != info.namespace and (
        not (here.startswith(info.namespace + ".") or info.namespace in _usings(root) | _global_usings(repo_dir))
        or _nested_using(root)
    ):
        # Outside the attribute's own namespace a nearer `using` may bring in
        # another assembly's attribute of the name.
        return "the file may not see the checkout's attribute"
    property_name = _kept_property(info, arguments, argument)
    if property_name is None:
        return f"{info.chain} keeps the value some other way"
    # A value of the attribute's type reaches only a file that names it or
    # something leading to it (the gate family's own taint argument).
    taint = csharp_family._taint(repo_dir, frozenset({info.chain}), frozenset(index.by_last), frozenset(index.by_full))
    holders = csharp_family.Family(
        full_names=frozenset(),
        simple_names=taint.family,
        members=frozenset(),
        settable=False,
        projects=frozenset(),
        returning=taint.returning,
        tainting=taint.tainting,
        opaque=taint.opaque,
        guest_named=taint.guest_named,
    )
    word = re.compile(rf"(?<!\w){re.escape(property_name)}(?!\w)")
    names = {info.chain, info.chain.removesuffix(_SUFFIX)}
    # A file reads the attribute's data only from a project that references
    # it, or by loading its assembly at run time (or as a source generator
    # reading its syntax): those files are read for what reads attributes or
    # values by reflection; only a file that may hold the attribute is held
    # to reading the property by ==.
    files: list[tuple[str, str, str]] = []
    projects = _xcs()._project_dirs(repo_dir)
    generators: set = set()
    for file in source_index.files(repo_dir, {".cs"}, with_ignored=True):
        if not str(file.path).endswith(".cs"):
            continue
        try:
            text = file.path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return f"{file.path} could not be read"
        plain = _decoded(text)
        if _GENERATOR.search(plain):
            homes = csharp_family._enclosing(file.resolved, projects)
            if not homes:
                return f"{file.path} is a source generator outside every project"
            generators.update(homes)
        files.append((str(file.path), file.resolved, text))
    for spelled, resolved, text in files:
        plain = _decoded(text)
        sees = csharp_family.sees(repo_dir, resolved, info.path)
        if (
            not sees
            and not _LOADS.search(plain)
            and not generators.intersection(csharp_family._enclosing(resolved, projects))
        ):
            continue
        if _RAW_ATTRIBUTES.search(plain):
            return f"{spelled} reads attribute data or a member's value by reflection"
        invokes = _INVOKE.search(plain) is not None and _METHOD_LOOKUP.search(plain) is not None
        if not invokes and "GetCustomAttribute" not in plain and not word.search(plain):
            continue
        root = _parse(text.removeprefix("\ufeff"), "csharp")
        if invokes and not _lookups_bound(root, names):
            return f"{spelled} invokes a method it looks up on a type that may be the attribute's"
        holds = sees and not csharp_family.family_free(text, root, holders)
        reason = _reads_compare(root, property_name, info, resolved, holds)
        if reason is not None:
            return f"{spelled}: {reason}"
    return None


def _nested_using(root: Node) -> bool:
    """Whether a ``using`` directive sits inside a namespace, where it is
    searched before the namespaces around it."""
    scoped = next((c for c in root.named_children if c.type == "file_scoped_namespace_declaration"), None)
    for node in _walk(root):
        if node.type != "using_directive":
            continue
        if scoped is not None and node.start_byte > scoped.start_byte:
            return True
        parent = node.parent
        while parent is not None:
            if parent.type == "namespace_declaration":
                return True
            parent = parent.parent
    return False


def _lookups_bound(root: Node, names: set[str]) -> bool:
    """Whether every method lookup of a parsed file is a call on
    ``typeof(X)`` for an ``X`` that is not the attribute."""
    from flag_cleanup import csharp_family

    for node in _walk(root):
        if node.type != "identifier" or _decoded(_text(node)) not in _METHOD_LOOKUPS:
            continue
        access = node.parent
        call = access.parent if access is not None and access.type == "member_access_expression" else None
        if call is None or call.type != "invocation_expression" or access.child_by_field_name("name") != node:
            return False
        bound = csharp_family._typeof_type(access.child_by_field_name("expression"))
        if bound is None or _decoded(bound.rsplit(".", 1)[-1]) in names:
            return False
    return True


def _kept_property(info, arguments: Node, argument: Node) -> str | None:
    """The get-only ``Type`` property the attribute class ``info`` keeps its
    parameter at ``argument``'s place in, when that is all it does with it:
    a sealed, non-partial class deriving from ``Attribute`` alone, with a
    primary constructor taking exactly the attribute's positional
    arguments and no other constructor."""
    try:
        source = Path(info.path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if _decoded(source) != source.replace("@", ""):
        return None  # an escape or a formatting character may hide a use
    root = _parse(source.removeprefix("\ufeff"), "csharp")
    if root.has_error:
        return None
    found = [
        n for n in _walk(root)
        if n.type == "class_declaration"
        and _simple(n.child_by_field_name("name")) == info.chain
        and _namespace_of(n) == info.namespace
    ]
    if len(found) != 1:
        return None
    declaration = found[0]
    bases = next((c for c in declaration.named_children if c.type == "base_list"), None)
    parameters = next((c for c in declaration.named_children if c.type == "parameter_list"), None)
    body = declaration.child_by_field_name("body")
    modifiers = _modifiers(declaration)
    if (
        "sealed" not in modifiers
        or "partial" in modifiers
        or bases is None
        or [_text(b) for b in bases.named_children] not in (["Attribute"], ["System.Attribute"])
        or parameters is None
        or body is None
        or any(c.type == "constructor_declaration" for c in body.named_children)
    ):
        return None
    declared = [c for c in parameters.named_children if c.type == "parameter"]
    passed = [c for c in arguments.named_children if c.type == "attribute_argument"]
    position = next(i for i, c in enumerate(passed) if c.id == argument.id)
    if len(declared) != len(passed) or any(
        c.type == "modifier" or _text(c) == "params" for d in declared for c in d.children
    ):
        return None
    parameter = declared[position]
    kind = parameter.child_by_field_name("type")
    name = _simple(parameter.child_by_field_name("name"))
    if name is None or kind is None or _text(kind) not in ("Type", "System.Type"):
        return None
    uses = [n for n in _walk(body) if n.type == "identifier" and _simple(n) == name]
    holder = uses[0].parent if len(uses) == 1 else None
    value = holder.child_by_field_name("value") if holder is not None else None
    if holder is None or holder.type != "property_declaration" or value is None or value.id != uses[0].id:
        return None
    accessors = holder.child_by_field_name("accessors")
    getters = [c for c in accessors.named_children if c.type == "accessor_declaration"] if accessors else []
    property_type = holder.child_by_field_name("type")
    if (
        len(getters) != 1
        or [_text(c) for c in getters[0].children if not c.is_named] != ["get", ";"]
        or getters[0].named_children
        or property_type is None
        or _text(property_type) not in ("Type", "System.Type")
        or any(c.type == "attribute_list" for c in holder.children)
        or _modifiers(holder) - {"public", "internal"}
    ):
        return None
    return _simple(holder.child_by_field_name("name"))


def _reads_compare(root: Node, property_name: str, info, path: str, holds: bool) -> str | None:
    """``None`` when every attribute retrieval in a parsed file names one
    attribute type that is no type parameter and, where the file may hold
    the attribute (``holds``), every mention of ``property_name`` is the
    attribute's own declaration or a member access compared with ``==`` or
    ``!=`` and no string spells it; else why not."""
    if root.has_error:
        return "does not parse"
    spellings = {property_name, f"get_{property_name}", f"<{property_name}>k__BackingField"}
    for node in _walk(root):
        if holds and node.type in _STRINGS and _decoded(_literal(node)) in spellings:
            return f"a string names {property_name}"
        if node.type == "generic_name" and _decoded(_simple(node) or "") in _RETRIEVALS:
            arguments = next((c for c in node.named_children if c.type == "type_argument_list"), None)
            taken = arguments.named_children if arguments is not None else []
            last = taken[0].child_by_field_name("name") if len(taken) == 1 and taken[0].type == "qualified_name" else (
                taken[0] if len(taken) == 1 else None
            )
            simple = _simple(last) if last is not None else None
            if simple is None or _decoded(simple) in _ANY_ATTRIBUTE or _type_parameter(node, simple):
                return "retrieves attributes other than as one named type"
            continue
        if node.type == "identifier" and _decoded(_text(node)) in _RETRIEVALS:
            parent = node.parent
            if parent is None or parent.type != "generic_name":
                return "retrieves attributes other than as one named type"
            continue
        if not holds or node.type != "identifier" or _decoded(_text(node)) != property_name:
            continue
        parent = node.parent
        if parent is not None and parent.type == "property_declaration" and path == info.path:
            continue
        compared = parent.parent if parent is not None and parent.type == "member_access_expression" else None
        member = parent.child_by_field_name("name") if compared is not None else None
        if (
            compared is None
            or member is None
            or member.id != node.id
            or compared.type != "binary_expression"
            or not any(_text(c) in ("==", "!=") for c in compared.children if not c.is_named)
        ):
            return f"reads {property_name} other than to compare it"
    return None


def _type_parameter(node: Node, simple: str) -> bool:
    """Whether ``simple`` is a type parameter in scope at ``node``."""
    scope = node.parent
    while scope is not None:
        parameters = scope.child_by_field_name("type_parameters") or next(
            (c for c in scope.children if c.type == "type_parameter_list"), None
        )
        if parameters is not None and any(
            (n := t.child_by_field_name("name")) is not None and _text(n).removeprefix("@") == simple
            for t in parameters.named_children
        ):
            return True
        scope = scope.parent
    return False


#: The text from a ``using`` to the ``=`` of an alias, global or not; an
#: over-match only refuses.
_ALIAS = re.compile(r"\busing\b([^;{}()=]*)=")
_WORD = re.compile(r"\w+")
_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def _decoded(text: str) -> str:
    """``text`` as the compiler reads an identifier: escapes decoded,
    formatting characters dropped, ``@`` dropped."""

    def char(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1) or match.group(2), 16))
        except (ValueError, OverflowError):
            return "\ufffd"

    text = _ESCAPE.sub(char, text)
    if not text.isascii():
        text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return text.replace("@", "")
#: Where a ``return`` takes its target type from the enclosing member.
_RETURNING = frozenset({"method_declaration", "local_function_statement"})
#: Operators declare what they return in ``type``: a conversion its target.
_OPERATORS = ("operator_declaration", "conversion_operator_declaration")
#: Where a ``return`` belongs to something else, whose type this does not read.
_FUNCTIONS = frozenset(
    {
        "method_declaration", "local_function_statement", "lambda_expression", "anonymous_method_expression",
        "accessor_declaration", "constructor_declaration", "destructor_declaration", "operator_declaration",
        "conversion_operator_declaration", "indexer_declaration", "property_declaration",
    }
)
#: Collections whose initializer passes each element to ``Add(T)``.
_COLLECTIONS = frozenset({"List", "HashSet"})
#: Library lists whose indexer's element type is their one type argument.
_INDEXED_LISTS = frozenset({"List", "IList"})
#: Library dictionaries whose indexer's element type is their second type argument.
_INDEXED_MAPS = frozenset({"Dictionary", "SortedDictionary", "SortedList"})


def _indexed_element(initializer: Node | None, repo_dir: str, from_path: str | None) -> Node | None:
    """The element type an index initializer (``[k] = …``) of ``initializer``
    assigns: the creation's own list or dictionary type, when that is a
    library type (no checkout type of its name)."""
    if initializer is None or initializer.type != "initializer_expression":
        return None
    creation = initializer.parent
    if creation is None or creation.type != "object_creation_expression":
        return None
    created = creation.child_by_field_name("type")
    generic = created.child_by_field_name("name") if created is not None and created.type == "qualified_name" else created
    if generic is None or generic.type != "generic_name":
        return None
    arguments = next((c for c in generic.named_children if c.type == "type_argument_list"), None)
    simple = _simple(generic) or ""
    if arguments is None or _may_declare_type(repo_dir, simple, from_path):
        return None
    count = len(arguments.named_children)
    if simple in _INDEXED_LISTS and count == 1:
        return arguments.named_children[0]
    if simple in _INDEXED_MAPS and count == 2:
        return arguments.named_children[1]
    return None


def _explicit_local_type(at: Node, name: str) -> Node | None:
    """The declared type of the local or parameter ``name`` that ``at``
    reads, when the nearest binding of the name is one with a type written
    out; ``None`` for ``var``, a pattern variable, a member, or anything this
    cannot place, and whenever the member holding ``at`` binds the name more
    than once (a lambda's, a pattern's or another switch section's)."""
    member, _ = _member_and_owner(at)
    if member is None or _bindings(member, name) != 1:
        return None
    node = at
    while node.parent is not None:
        parent = node.parent
        if parent.type in ("block", "switch_section"):
            for statement in reversed([c for c in parent.named_children if c.end_byte <= node.start_byte]):
                if statement.type != "local_declaration_statement":
                    if name in _declared_by(statement):
                        return None
                    continue
                declaration = next((c for c in statement.named_children if c.type == "variable_declaration"), None)
                if declaration is not None and name in _declared_by(declaration):
                    type_node = declaration.child_by_field_name("type")
                    return None if type_node is None or type_node.type == "implicit_type" else type_node
        elif parent.type in _BINDERS:
            parameters = parent.child_by_field_name("parameters")
            for p in parameters.named_children if parameters is not None else ():
                if p.type == "parameter" and _text(p.child_by_field_name("name") or p) == name:
                    return p.child_by_field_name("type")
            if name in _declared_by(parent, header_only=True):
                return None
        if parent.type in _CONST_OWNERS:
            return None
        node = parent
    return None


def _xcs():
    from flag_cleanup import cross_file_csharp

    return cross_file_csharp


#: repo_dir -> ``(resolved path, type names it may declare)`` of each C#
#: file that cannot be read (``None``: any) or does not parse whole.
_UNPARSED: dict[str, list[tuple[str, frozenset[str] | None]]] = {}


def _unparsed_files(repo_dir: str) -> list[tuple[str, frozenset[str] | None]]:
    if repo_dir not in _UNPARSED:
        from flag_cleanup import source_index

        found: list[tuple[str, frozenset[str] | None]] = []
        for file in source_index.files(repo_dir, {".cs"}, with_ignored=True):
            if not str(file.path).endswith(".cs"):
                continue
            try:
                text = file.path.read_bytes().decode("utf-8-sig")
            except (OSError, UnicodeDecodeError):
                found.append((file.resolved, None))
                continue
            if _parse(text, "csharp").has_error:
                found.append((file.resolved, _xcs()._declarable_words(text)))
        _UNPARSED[repo_dir] = found
    return _UNPARSED[repo_dir]


def _may_declare_type(repo_dir: str, name: str, from_path: str | None) -> bool:
    """Whether a type of this simple name may be declared where ``from_path``
    (or, with ``None``, any file) can see it: the index holds one in a project
    that file can see, or a file this cannot read or parse whole may declare
    one there."""
    from flag_cleanup import csharp_family

    index = _xcs().csharp_index(repo_dir)
    if from_path is None:
        return index.may_declare_type(name)
    if index.unreadable or any(csharp_family.sees(repo_dir, from_path, info.path) for info in index.by_last.get(name, ())):
        return True
    return any(
        (words is None or name in words) and csharp_family.sees(repo_dir, from_path, path)
        for path, words in _unparsed_files(repo_dir)
    )


def _target_name(node: Node, repo_dir: str, from_path: str | None = None) -> str | None:
    """The simple name of the type a target-typed ``new(…)`` creates, when
    its context declares it: the arrow body of a method, local function,
    operator, conversion operator or property, or a ``return`` in a method,
    local function or operator (neither ``async``), the value of an explicitly typed declaration, a branch of a
    conditional that is one of these and whose other branch has no type of
    its own (``null``, ``default``, another ``new(…)``), or an element
    of a ``List<T>``/``HashSet<T>`` collection initializer (no such type in
    the checkout). None otherwise, and for a type parameter in scope. The
    caller still refuses a name some ``using`` aliases."""
    parent = node.parent
    declared: Node | None = None
    if parent is None:
        return None
    if parent.type == "conditional_expression" and node.id != (parent.child_by_field_name("condition") or node).id:
        # `c ? null : new(…)`: with no natural type on the other branch the
        # conditional is target-typed, so the `new` creates what it is
        # converted to. One that has a type (`c ? foo : new(…)`) types the
        # `new` itself, which its context may only convert.
        other = parent.child_by_field_name(
            "alternative" if node.id == (parent.child_by_field_name("consequence") or node).id else "consequence"
        )
        untyped = other is not None and (
            other.type in ("null_literal", "implicit_object_creation_expression")
            or (other.type == "default_expression" and not other.named_children)
        )
        return _target_name(parent, repo_dir, from_path) if untyped else None
    if (
        parent.type == "assignment_expression"
        and parent.child_by_field_name("right") is not None
        and parent.child_by_field_name("right").id == node.id
        and any(not c.is_named and _text(c) == "=" for c in parent.children)
    ):
        # `x = new(…)` and `x[i] = new(…)` for a local or parameter `x` of a
        # declared type: that type, or the element type of a library list or
        # array (no checkout type of the list's name).
        left = parent.child_by_field_name("left")
        if left is not None and left.type == "element_binding_expression":
            # `new Dictionary<K, V> { [k] = new(…) }`: the value type of a
            # library dictionary, or the element type of a library list.
            declared = _indexed_element(parent.parent, repo_dir, from_path)
        elif left is not None and left.type == "identifier":
            declared = _explicit_local_type(left, _text(left))
        elif left is not None and left.type == "element_access_expression":
            target = left.child_by_field_name("expression")
            collection = _explicit_local_type(target, _text(target)) if target is not None and target.type == "identifier" else None
            if collection is not None and collection.type == "nullable_type" and collection.named_children:
                collection = collection.named_children[0]
            if collection is not None and collection.type == "array_type":
                declared = collection.child_by_field_name("type")
            elif collection is not None and collection.type == "generic_name":
                arguments = next((c for c in collection.named_children if c.type == "type_argument_list"), None)
                simple = _simple(collection) or ""
                if (
                    arguments is not None
                    and len(arguments.named_children) == 1
                    and simple in _INDEXED_LISTS
                    and not _may_declare_type(repo_dir, simple, from_path)
                ):
                    declared = arguments.named_children[0]
        if declared is None:
            return None
    elif parent.type == "arrow_expression_clause" or parent.type == "return_statement":
        owner = parent.parent
        if parent.type == "return_statement":
            while owner is not None and owner.type not in _FUNCTIONS:
                owner = owner.parent
        if owner is None or owner.type not in (*_RETURNING, *_OPERATORS, "property_declaration") or "async" in _modifiers(owner):
            return None
        declared = owner.child_by_field_name("returns") or owner.child_by_field_name("type")
    elif parent.type == "variable_declarator":
        declaration = parent.parent
        if declaration is None or declaration.type != "variable_declaration":
            return None
        if parent.child_by_field_name("name") is None or node.id == parent.child_by_field_name("name").id:
            return None
        declared = declaration.child_by_field_name("type")
    elif parent.type == "initializer_expression" and parent.parent is not None and (
        parent.parent.type == "object_creation_expression"
    ):
        created = parent.parent.child_by_field_name("type")
        generic = created.child_by_field_name("name") if created is not None and created.type == "qualified_name" else created
        arguments = next((c for c in generic.named_children if c.type == "type_argument_list"), None) if generic else None
        if (
            generic is None
            or generic.type != "generic_name"
            or _simple(generic) not in _COLLECTIONS
            or arguments is None
            or len(arguments.named_children) != 1
            or _may_declare_type(repo_dir, _simple(generic) or "", from_path)
        ):
            return None
        declared = arguments.named_children[0]
    if declared is not None and declared.type == "nullable_type" and declared.named_children:
        declared = declared.named_children[0]
    if declared is None:
        return None
    if declared.type == "qualified_name":
        last = declared.child_by_field_name("name")
        simple = _simple(last) if last is not None else None
        return _decoded(simple) if simple is not None else None
    if declared.type not in ("identifier", "generic_name"):
        return None
    simple = _simple(declared)
    if simple is None or _type_parameter(node, simple):
        return None
    return _decoded(simple)


def _simple(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "identifier":
        return _text(node).removeprefix("@")
    if node.type == "generic_name":
        first = node.named_children[0] if node.named_children else None
        return _text(first).removeprefix("@") if first is not None and first.type == "identifier" else None
    return None


# --- a classic constructor's parameter of a type a run deletes ------------------
#
# The primary-constructor proof above removes a parameter the fold left
# unread. This one is narrower in what it starts from and wider in what it
# deletes: a parameter of an explicit constructor whose TYPE is one this run
# deletes (``family``), which therefore cannot stay. It goes with the one
# statement that stored it (``_f = p;``), the private field that statement
# wrote, and every caller's argument. Everything else about the class must be
# plain enough that nothing can see the change but the callers the walk above
# finds: no other constructor (overload resolution, and DI's choice of the
# longest satisfiable one, are by type), nothing that reads its layout or
# synthesises members from its fields, and no other use of the parameter or
# the field.

#: Class attributes through which something reads the type's fields or layout.
_LAYOUT_ATTRIBUTES = frozenset({"Serializable", "DataContract", "StructLayout"})
_DOC_COMMENT = re.compile(rb"\A(?:///|/\*\*)")
_LINE_COMMENT = re.compile(rb"[ \t]*//[^\r\n]*")


def _simple_type(node: Node | None) -> str | None:
    """The simple name a type spells: ``I``, ``Ns.I``, ``global::Ns.I``,
    ``I?``. ``None`` for anything else (generic, array, tuple, predefined)."""
    while node is not None and node.type == "nullable_type":
        node = node.named_children[0] if node.named_children else None
    if node is None:
        return None
    if node.type == "identifier":
        return _text(node)
    if node.type in ("qualified_name", "alias_qualified_name"):
        name = node.child_by_field_name("name")
        return _simple_type(name) if name is not None else None
    return None


def _attribute_names(node: Node) -> set[str]:
    names: set[str] = set()
    for attribute_list in (c for c in node.children if c.type == "attribute_list"):
        for attribute in (c for c in attribute_list.named_children if c.type == "attribute"):
            name = _simple_type(attribute.child_by_field_name("name"))
            if name is not None:
                names.add(name.removesuffix(_SUFFIX) if name != _SUFFIX else name)
    return names


def _modifiers(node: Node) -> set[str]:
    return {_text(c) for c in node.children if c.type == "modifier"}


def declaration_span(node: Node, data: bytes) -> tuple[int, int]:
    """``node``'s byte span widened to the doc comment directly above it and
    a line comment after it on its own last line: what deleting it must take
    with it, or the comment describes whatever comes next.

    A plain ``//`` comment above is left: it may head a group, and a pass
    reports those rather than editing them."""
    start = node.start_byte
    previous = node.prev_sibling
    while (
        previous is not None
        and previous.type == "comment"
        and _DOC_COMMENT.match(previous.text)
        and not data[previous.end_byte : start].strip()
        and data[previous.end_byte : start].count(b"\n") <= 1
    ):
        start = previous.start_byte
        previous = previous.prev_sibling
    end = node.end_byte
    trailing = _LINE_COMMENT.match(data, end)
    if trailing is not None:
        end = trailing.end()
    return start, end


def _family_object(node: Node, family: frozenset[str]) -> bool:
    """Whether ``node`` builds a test double or a member of the family with
    nothing that can do work: ``Mock.Of<F>()``, ``Substitute.For<F>()``, or
    ``new F(<inert>…)``. A family type's constructors are proven inert by the
    caller, and a double's construction runs no code of the repository's."""
    if node.type == "invocation_expression":
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or function.type != "member_access_expression":
            return False
        receiver = function.child_by_field_name("expression")
        generic = function.child_by_field_name("name")
        if receiver is None or generic is None or generic.type != "generic_name":
            return False
        callee = (_text(receiver), _text(generic.named_children[0]))
        type_arguments = next((c for c in generic.named_children if c.type == "type_argument_list"), None)
        types = [c for c in type_arguments.named_children] if type_arguments is not None else []
        return (
            callee in (("Mock", "Of"), ("Substitute", "For"))
            and len(types) == 1
            and _simple_type(types[0]) in family
            and arguments is not None
            and not _items(arguments)
        )
    if node.type == "object_creation_expression":
        if _simple_type(node.child_by_field_name("type")) not in family:
            return False
        if node.child_by_field_name("initializer") is not None:
            return False
        arguments = node.child_by_field_name("arguments")
        return all(
            (e := _expression(a)) is not None and _inert(e)
            for a in (_items(arguments) if arguments is not None else [])
        )
    return False


def _plain_construction(node: Node, repo_dir: str, path: str, depth: int = 0) -> bool:
    """Whether ``node`` builds a checkout class whose construction runs
    nothing but assignments: ``new Stub(enabled: true)`` for ``class Stub {
    public Stub(bool enabled) => Enabled = enabled; public bool Enabled { get;
    set; } }``. The class (resolved from ``path``) is not generic or partial,
    has no base class (interfaces only), no static constructor, every field
    and property initializer is a literal or a primary-constructor
    parameter, and every constructor only assigns a parameter or a literal
    to one of its own members, chaining at most to ``this(…)`` of the same.
    Its arguments are inert themselves."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import csharp_family
    from flag_cleanup import wrapper_csharp as wc

    if node.type != "object_creation_expression" or depth > 2 or node.child_by_field_name("initializer") is not None:
        return False
    arguments = node.child_by_field_name("arguments")
    if not all(
        (e := _expression(a)) is not None and (_inert(e) or _plain_construction(e, repo_dir, path, depth + 1))
        for a in (_items(arguments) if arguments is not None else [])
    ):
        return False
    readers = wc._Readers(repo_dir)
    reader = readers.get(path)
    type_node = node.child_by_field_name("type")
    full = wc._type_full(type_node, reader) if reader is not None and type_node is not None else None
    infos = [i for i in xcs.csharp_index(repo_dir).by_full.get(full or "", ()) if not i.generic and not i.partial]
    if len(infos) != 1:
        return False
    declaring = readers.get(infos[0].path)
    declaration = wc._find_type(declaring.root, infos[0].chain) if declaring is not None else None
    if declaration is None or declaration.type != "class_declaration":
        return False
    # A user-defined conversion runs code where a value is passed or stored:
    # refused when one the checkout declares could serve a slot of the class.
    targets = _conversion_targets(repo_dir)
    if targets is None or any(_may_convert_into(t, declaring, readers, targets) for t in _stored_types(declaration)):
        return False
    for base in wc._base_nodes(declaration):
        simple = xcs._simple_name(base) or ""
        found = wc._checkout_type(wc._Typed(base, declaring), readers)
        if found is not None:
            if found[0].type != "interface_declaration":
                return False
        elif csharp_family._INTERFACE_NAME.match(simple) is None or simple.endswith(("Exception", "Attribute")):
            return False
    primary = {wc._name_of(p) for p in (wc._child(declaration, "parameter_list").named_children if wc._child(declaration, "parameter_list") is not None else ()) if p.type == "parameter"}
    body = declaration.child_by_field_name("body")
    # Assigning a property runs its setter: only fields and auto-properties.
    members = {
        n
        for m in (body.named_children if body is not None else ())
        if m.type == "field_declaration" or (m.type == "property_declaration" and _auto_property(m))
        for n in (wc._member_names(m) if m.type == "property_declaration" else _field_names(m))
    }
    for member in body.named_children if body is not None else ():
        if member.type == "constructor_declaration":
            if "static" in xcs._modifiers(member) or not _assigning_constructor(member, members):
                return False
        elif member.type in ("field_declaration", "event_field_declaration", "property_declaration"):
            for initial in _initializers(member):
                if not (initial.type in _CONSTANT_VALUES or (
                    initial.type == "identifier" and wc._text(initial) in primary
                )):
                    return False
        elif member.type in ("destructor_declaration", "operator_declaration", "conversion_operator_declaration"):
            continue
    return True


#: Values that run nothing: literals, but no interpolated string, whose holes do.
_CONSTANT_VALUES = (_LITERALS - {"interpolated_string_expression"}) | {"null_literal"}
#: Predefined numeric types: a conversion to one serves a wider one too.
_NUMERIC = frozenset(
    {"sbyte", "byte", "short", "ushort", "int", "uint", "long", "ulong", "nint", "nuint", "float", "double", "decimal", "char"}
)
#: repo_dir -> :func:`_conversion_targets`.
_CONVERSIONS: dict[str, tuple[frozenset[str], bool] | None] = {}


def _conversion_targets(repo_dir: str) -> tuple[frozenset[str], bool] | None:
    """What a value may become through a user-defined ``implicit`` or
    ``explicit`` conversion the checkout declares: the simple names of each
    operator's target type and of every type a checkout target derives from
    or implements (a conversion to ``T`` serves a slot of any of those), all
    numeric types when a target is numeric; and whether some target is a
    library or generic type, whose supertypes are not known here. ``None``
    when a file cannot be read or parsed."""
    if repo_dir not in _CONVERSIONS:
        _CONVERSIONS[repo_dir] = _find_conversion_targets(repo_dir)
    return _CONVERSIONS[repo_dir]


def _find_conversion_targets(repo_dir: str) -> tuple[frozenset[str], bool] | None:
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import wrapper_csharp as wc

    pattern = re.compile(r"\b(?:implicit|explicit)\s+operator\b")
    names: set[str] = set()
    library = False
    readers = wc._Readers(repo_dir)
    for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
        text = wc._read(f.resolved)
        if text is None:
            return None
        if pattern.search(text) is None:
            continue
        reader = readers.get(f.resolved)
        if reader is None or reader.root.has_error:
            return None
        for node in _walk(reader.root):
            if node.type != "conversion_operator_declaration":
                continue
            target = node.child_by_field_name("type")
            if target is None:
                return None
            pending = [(target, reader)]
            seen: set[str] = set()
            while pending:
                type_node, in_reader = pending.pop()
                if type_node.type == "nullable_type" and type_node.named_children:
                    type_node = type_node.named_children[0]
                if type_node.type == "predefined_type":
                    simple = wc._text(type_node)
                    names.add(simple)
                    if simple in _NUMERIC:
                        names.update(_NUMERIC)
                    continue
                simple = xcs._simple_name(type_node)
                if simple is None:
                    return None
                if simple in seen:
                    continue
                seen.add(simple)
                names.add(simple)
                # Every part of a partial type, and a generic one, has bases.
                full = wc._type_full(type_node, in_reader)
                infos = xcs.csharp_index(repo_dir).by_full.get(full or "", ()) if full is not None else ()
                if not infos:
                    library = True
                    continue
                for info in infos:
                    declaring = readers.get(info.path)
                    declaration = (
                        wc._find_type_in(declaring.root, info.namespace, info.chain) if declaring is not None else None
                    )
                    if declaration is None:
                        return None
                    pending.extend((base, declaring) for base in wc._base_nodes(declaration))
    return frozenset(names), library


def _stored_types(declaration: Node) -> list[Node | None]:
    """The declared types a construction of ``declaration`` passes values
    into or stores: its primary-constructor and constructor parameters, and
    its fields, events and properties (``None`` for one it cannot read)."""
    from flag_cleanup import wrapper_csharp as wc

    body = declaration.child_by_field_name("body")
    members = list(body.named_children) if body is not None else []
    found: list[Node | None] = []
    lists = [wc._child(declaration, "parameter_list")]
    lists += [wc._child(m, "parameter_list") for m in members if m.type == "constructor_declaration"]
    for listed in lists:
        for parameter in listed.named_children if listed is not None else ():
            if parameter.type == "parameter":
                found.append(parameter.child_by_field_name("type"))
    for member in members:
        if member.type in ("property_declaration", "event_declaration"):
            found.append(member.child_by_field_name("type"))
        elif member.type in ("field_declaration", "event_field_declaration"):
            variables = wc._child(member, "variable_declaration")
            found.append(variables.child_by_field_name("type") if variables is not None else None)
    return found


def _may_convert_into(
    type_node: Node | None, reader: object, readers: object, targets: tuple[frozenset[str], bool]
) -> bool:
    """Whether a user-defined conversion of :func:`_conversion_targets` could
    serve a slot of type ``type_node``: never ``object`` or ``dynamic``,
    which take every value by a standard conversion."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import wrapper_csharp as wc

    names, library = targets
    while type_node is not None and type_node.type in ("nullable_type", "array_type"):
        type_node = type_node.named_children[0] if type_node.named_children else None
    if type_node is None:
        return True
    if type_node.type == "predefined_type":
        simple = wc._text(type_node)
        return simple != "object" and simple in names
    simple = xcs._simple_name(type_node)
    if simple is None:
        return True
    if simple == "dynamic":
        return False
    if simple in names:
        return True
    # A library target serves a library-typed slot; a checkout type's
    # subtypes are all in ``names``.
    full = wc._type_full(type_node, reader)
    return library and not (full is not None and xcs.csharp_index(readers.repo_dir).by_full.get(full))


def _auto_property(property_node: Node) -> bool:
    """Whether every accessor of a property has no body (``{ get; set; }``)."""
    from flag_cleanup import wrapper_csharp as wc

    accessors = wc._child(property_node, "accessor_list")
    if accessors is None:
        return False
    listed = [a for a in accessors.named_children if a.type == "accessor_declaration"]
    return bool(listed) and all(a.child_by_field_name("body") is None for a in listed)


def _field_names(field_node: Node) -> list[str]:
    from flag_cleanup import wrapper_csharp as wc

    declaration = wc._child(field_node, "variable_declaration")
    return [
        n
        for d in (declaration.named_children if declaration is not None else ())
        if d.type == "variable_declarator" and (n := wc._name_of(d))
    ]


def _initializers(member: Node) -> list[Node]:
    """The initializer values of a field's declarators or a property."""
    from flag_cleanup import wrapper_csharp as wc

    found = []
    if member.type == "property_declaration":
        value = member.child_by_field_name("value")
        # `=> expr` is the getter's body, run when read, not an initializer.
        if value is not None and value.type != "arrow_expression_clause":
            found.append(value)
        return found
    declaration = wc._child(member, "variable_declaration")
    for declarator in declaration.named_children if declaration is not None else ():
        if declarator.type != "variable_declarator":
            continue
        values = [c for c in wc._named(declarator) if c.id != (declarator.child_by_field_name("name") or c).id]
        found += values[-1:]
    return found


def _assigning_constructor(constructor: Node, members: set[str]) -> bool:
    """Whether ``constructor`` only assigns its parameters or literals to the
    class's own ``members`` (``M = p``, ``this.M = p``), chaining at most to
    ``this(…)`` with such arguments."""
    from flag_cleanup import wrapper_csharp as wc

    listed = constructor.child_by_field_name("parameters")
    names = {wc._name_of(p) for p in (listed.named_children if listed is not None else ()) if p.type == "parameter"}
    if listed is not None and any(
        c.type == "modifier" for p in listed.named_children if p.type == "parameter" for c in p.children
    ):
        return False

    def value_ok(value: Node | None) -> bool:
        return value is not None and (
            value.type in _CONSTANT_VALUES or (value.type == "identifier" and wc._text(value) in names)
        )

    initializer = wc._child(constructor, "constructor_initializer")
    if initializer is not None:
        if not wc._text(initializer).lstrip(": \t\n").startswith("this"):
            return False
        chained = _argument_list(initializer)
        if chained is not None and not all(value_ok(_expression(a)) for a in _items(chained)):
            return False
    body = constructor.child_by_field_name("body")
    if body is None:
        return False
    if body.type == "arrow_expression_clause":
        statements = [c for c in body.named_children if c.type != "comment"]
    elif body.type == "block":
        statements = []
        for statement in body.named_children:
            if statement.type == "comment":
                continue
            if statement.type != "expression_statement" or len(statement.named_children) != 1:
                return False
            statements.append(statement.named_children[0])
    else:
        return False
    for expression in statements:
        if expression.type != "assignment_expression":
            return False
        operator = next((c for c in expression.children if not c.is_named), None)
        left = expression.child_by_field_name("left")
        if operator is None or wc._text(operator) != "=" or left is None:
            return False
        if left.type == "member_access_expression":
            receiver = left.child_by_field_name("expression")
            if receiver is None or receiver.type != "this":
                return False
            left = left.child_by_field_name("name")
        if left is None or left.type != "identifier" or wc._text(left) not in members:
            return False
        if not value_ok(expression.child_by_field_name("right")):
            return False
    return True


def _family_inert(family: frozenset[str]) -> Callable[[Node], bool]:
    return lambda node: _inert(node) or _family_object(node, family)


def _assignment(statement: Node, param_name: str) -> str | None:
    """The field ``statement`` stores the parameter in, when it is exactly
    ``_f = p;`` or ``this._f = p;``."""
    if statement.type != "expression_statement" or len(statement.named_children) != 1:
        return None
    return _assignment_expression(statement.named_children[0], param_name)


def _assignment_expression(expression: Node, param_name: str) -> str | None:
    """The field ``expression`` stores the parameter in: ``_f = p`` or
    ``this._f = p``."""
    if expression.type != "assignment_expression":
        return None
    operator = next((c for c in expression.children if not c.is_named), None)
    left = expression.child_by_field_name("left")
    right = expression.child_by_field_name("right")
    if operator is None or _text(operator) != "=" or right is None or left is None:
        return None
    if right.type != "identifier" or _text(right) != param_name:
        return None
    if left.type == "member_access_expression":
        receiver = left.child_by_field_name("expression")
        if receiver is None or receiver.type not in ("this_expression", "this"):
            return None
        left = left.child_by_field_name("name")
    if left is None or left.type != "identifier":
        return None
    return _text(left)


def _field(body: Node, name: str) -> Node | None:
    """The field declaration in ``body`` whose one declarator is ``name``."""
    for member in body.named_children:
        if member.type != "field_declaration":
            continue
        declaration = next((c for c in member.named_children if c.type == "variable_declaration"), None)
        declarators = [
            c for c in (declaration.named_children if declaration is not None else []) if c.type == "variable_declarator"
        ]
        if any(_declarator_name(d) == name for d in declarators):
            return member if len(declarators) == 1 else None
    return None


def _declarator_name(declarator: Node) -> str | None:
    name = declarator.child_by_field_name("name")
    if name is None:
        name = next((c for c in declarator.named_children if c.type == "identifier"), None)
    return _text(name) if name is not None else None


def _field_type(field: Node) -> Node | None:
    declaration = next((c for c in field.named_children if c.type == "variable_declaration"), None)
    return declaration.child_by_field_name("type") if declaration is not None else None


def _named_in_strings(name: str, repo_dir: str) -> str | None:
    """A C# file holding a string literal that is exactly ``name``:
    reflection (``GetField("_f")``, ``[UnsafeAccessor(Name = "_f")]``) can
    reach a private field through one, so the field cannot be proven unread."""
    needle = f'"{name}"'.encode()
    for file in source_index.files(repo_dir, (".cs",), with_ignored=True):
        try:
            data = file.path.read_bytes()
        except OSError:
            return f"{file.path} could not be read"
        if needle in data:
            return f"{file.path}: a string names the field {name}"
    return None


def plan_classic(
    path: str,
    source: str,
    class_name: str,
    param_name: str,
    repo_dir: str,
    family: frozenset[str],
) -> Removal | Refusal:
    """Prove that the explicit constructor parameter ``param_name`` of
    ``class_name`` (in the file at ``path``, whose text is ``source``), whose
    type is one of ``family``'s simple names, can go with the field it was
    stored in and every caller's argument.

    A caller's argument may be anything :func:`_inert` accepts, or a test
    double or member of the family built from inert arguments
    (:func:`_family_object`). A use of the parameter as a whole argument of
    the constructor's own ``base(…)`` is returned as ``forwarded``: it can go
    only if the base type's matching parameter goes in the same run, which
    is the caller's to check."""
    from flag_cleanup import csharp_family

    label = f"{class_name}({param_name})"
    root = _parse(source.removeprefix("﻿"), "csharp")
    data = source.removeprefix("﻿").encode("utf-8")
    if root.has_error:
        return Refusal(label, "the declaring file does not parse")
    named = [
        n
        for n in _walk(root)
        if n.type.endswith("_declaration")
        and n.type not in ("constructor_declaration", "method_declaration", "namespace_declaration")
        and (cn := n.child_by_field_name("name")) is not None
        and _text(cn) == class_name
    ]
    if len(named) != 1:
        return Refusal(label, "the type is declared more than once in its file")
    declaration = named[0]
    if declaration.type != "class_declaration":
        return Refusal(label, f"a {declaration.type.removesuffix('_declaration').replace('_', ' ')}, not a class")
    if "partial" in _modifiers(declaration):
        return Refusal(label, "a partial class, whose other parts may read the field")
    if any(c.type == "type_parameter_list" for c in declaration.children):
        return Refusal(label, "a generic type")
    if _primary(declaration) is not None:
        return Refusal(label, "a class with a primary constructor")
    layout = _attribute_names(declaration) & _LAYOUT_ATTRIBUTES
    if layout:
        return Refusal(label, f"[{sorted(layout)[0]}] reads the type's fields")
    constructors = _constructors(declaration)
    if len(constructors) != 1:
        return Refusal(
            label,
            f"{len(constructors)} explicit constructors (a call is bound by type, which this does not see)",
        )
    constructor = constructors[0]
    parameters = constructor.child_by_field_name("parameters")
    found = [
        (i, item)
        for i, item in enumerate(_parameters(parameters) if parameters is not None else [])
        if (n := item.child_by_field_name("name")) is not None and _text(n) == param_name
    ]
    if parameters is None or len(found) != 1:
        return Refusal(label, "no longer a parameter of the constructor", quiet=True)
    index, parameter = found[0]
    type_name = _simple_type(parameter.child_by_field_name("type"))
    if type_name not in family:
        return Refusal(label, "the parameter's type is not one this run deletes", quiet=True)
    label = f"{class_name}({_text(parameter)})"
    if IDENT.fullmatch(class_name) is None or IDENT.fullmatch(param_name) is None:
        return Refusal(label, "a name the deletion rules cannot spell")
    if any(c.type == "attribute_list" for c in parameter.children):
        return Refusal(label, "an attribute on the parameter (keyed or explicit injection)")
    if any(c.type == "modifier" for c in parameter.children):
        return Refusal(label, "a parameter modifier (ref, out, in, this, scoped)")
    doc_start, _ = declaration_span(constructor, data)
    if re.search(rf"""name\s*=\s*["']{re.escape(param_name)}["']""".encode(), data[doc_start : constructor.start_byte]):
        return Refusal(label, "a doc comment names the parameter (CS1572 once it is gone)")

    name_node = parameter.child_by_field_name("name")
    uses = [
        n
        for n in _walk(constructor)
        if n.type == "identifier" and _text(n) == param_name and n != name_node
    ]
    body = constructor.child_by_field_name("body")
    sites: list[Site] = []
    forwarded: list[Site] = []
    field_name: str | None = None
    # `public Svc(IGate gate) => _gate = gate;` cannot lose its store and
    # keep a body, so when the parameter is the only one the whole
    # constructor goes: with no other explicit constructor, C# then supplies
    # the parameterless one it stands for.
    whole = (
        body is not None
        and body.type == "arrow_expression_clause"
        and len(_parameters(parameters)) == 1
        and not any(c.type in ("constructor_initializer", "attribute_list") for c in constructor.children)
        and _modifiers(constructor) == {"public"}
    )
    for use in uses:
        statement = use.parent.parent if use.parent is not None else None
        stored = _assignment(statement, param_name) if statement is not None and field_name is None else None
        if body is not None and body.type == "block" and statement is not None and statement.parent == body and stored:
            field_name = stored
            start, end = declaration_span(statement, data)
            sites.append(Site(path, start, end, "statement"))
            continue
        if (
            whole
            and stored is None
            and use.parent is not None
            and use.parent.parent == body
            and use.parent.type == "assignment_expression"
            and (stored := _assignment_expression(use.parent, param_name)) is not None
            and field_name is None
        ):
            field_name = stored
            continue
        argument = use.parent
        initializer = argument.parent.parent if argument is not None and argument.parent is not None else None
        if (
            argument is not None
            and argument.type == "argument"
            and _expression(argument) == use
            and initializer is not None
            and initializer.type == "constructor_initializer"
            and any(c.type == "base" for c in initializer.children)
        ):
            forwarded.append(Site(path, use.start_byte, use.end_byte))
            continue
        return Refusal(
            label,
            f"line {use.start_point[0] + 1}: the parameter is used other than stored in one field",
        )
    if whole:
        start, end = declaration_span(constructor, data)
        sites.append(Site(path, start, end, "member"))

    class_body = declaration.child_by_field_name("body")
    if field_name is not None:
        field = _field(class_body, field_name) if class_body is not None else None
        if field is None:
            return Refusal(label, f"{field_name} is not one field declared alone in the class")
        modifiers = _modifiers(field)
        if modifiers & {"public", "protected", "internal", "static", "const", "volatile"}:
            return Refusal(label, f"{field_name} is not a private instance field")
        if any(c.type == "attribute_list" for c in field.children):
            return Refusal(label, f"an attribute on {field_name}")
        if _simple_type(_field_type(field)) not in family:
            return Refusal(label, f"{field_name}'s type is not one this run deletes")
        # A private field is visible only inside its class, nested types
        # included, and `partial` is refused, so the class is its reach.
        mentions = [
            n for n in _walk(declaration) if n.type == "identifier" and _text(n) == field_name
        ]
        if len(mentions) != 2:
            line = next(
                (m.start_point[0] + 1 for m in mentions if not (field.start_byte <= m.start_byte < field.end_byte)
                 and not any(s.start <= m.start_byte < s.end for s in sites)),
                field.start_point[0] + 1,
            )
            return Refusal(label, f"line {line}: {field_name} is still read")
        reason = _named_in_strings(field_name, repo_dir)
        if reason is not None:
            return Refusal(label, reason)
        start, end = declaration_span(field, data)
        sites.append(Site(path, start, end, "member"))

    callers, reason = _references(
        class_name,
        path,
        declaration.start_byte,
        index,
        _names(parameters),
        _arity(parameters),
        repo_dir,
        _namespace_of(declaration),
        inert=_family_inert(family),
        sees=lambda caller: csharp_family.sees(repo_dir, caller, path),
        interfaces=_interface_positions(parameters, root, path, repo_dir),
        inert_in=lambda node, caller: _plain_construction(node, repo_dir, caller),
    )
    if reason is not None:
        return Refusal(label, reason)
    # The declaring file's own sites were measured without its byte-order
    # mark; `_references` already adds it for the callers.
    offset = 3 if source.startswith("﻿") else 0
    own = [Site(s.path, s.start + offset, s.end + offset, s.form) for s in sites]
    exported = "public" in _modifiers(declaration) and "public" in _modifiers(constructor)
    return Removal(
        path,
        class_name,
        param_name,
        label,
        index,
        tuple(callers) + tuple(own),
        exported,
        field=field_name,
        classic=True,
        forwarded=tuple(Site(s.path, s.start + offset, s.end + offset) for s in forwarded),
    )
