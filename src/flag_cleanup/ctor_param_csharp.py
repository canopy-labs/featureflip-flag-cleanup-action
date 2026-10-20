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
whose argument count the constructor could accept refuses too.
"""

from __future__ import annotations

import re
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
        return (
            expression is not None
            and name is not None
            and name.type == "identifier"
            and _inert(expression)
        )
    return False


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
) -> str | None:
    """``None`` when this spelling of the type name is accounted for (and
    records its argument, if it is a call), else why it is not.

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
        return "typeof (reflection can construct it positionally)"
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
) -> tuple[list[Site], str | None]:
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
    for file in source_index.files(repo_dir, with_ignored=True):
        spelled = str(file.path)
        is_cs = spelled.endswith(".cs")
        if not is_cs and not spelled.endswith(guests):
            continue
        try:
            data = file.path.read_bytes()
        except OSError:
            return sites, f"{file.path} could not be read"
        mentions = word.search(data) is not None
        if not is_cs:
            if mentions:
                return sites, f"{file.path} mentions it (razor, cshtml or another .NET guest file)"
            continue
        if not mentions and not _NEW_CALL.search(data):
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
                if arity[0] <= count <= arity[1]:
                    return sites, (
                        f"{file.path}:{node.start_point[0] + 1}: "
                        "a target-typed new(…) could construct it"
                    )
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
            reason = _classify(node, spelled, start, index, names, local, namespace, visible, inert)
            if reason is not None:
                return sites, f"{file.path}:{node.start_point[0] + 1}: {reason}"
        if named_at is not None and reflects:
            return sites, (
                f"{file.path}:{named_at.start_point[0] + 1}: "
                "a string naming the type, in a file that constructs by reflection"
            )
        # Offsets into the file's bytes, BOM included.
        sites.extend(Site(s.path, s.start + offset, s.end + offset) for s in local)
    return sites, None


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
