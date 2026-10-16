"""Prove a C# primary-constructor parameter can be removed with its arguments.

The C# counterpart of :mod:`flag_cleanup.ctor_param_ts`. A primary-constructor
parameter the fold left unread is CS9113. Removing it changes the constructor,
so every spelling of the type name in the repository's C# must be one of:
``new Svc(…)`` / ``new Ns.Svc(…)`` (no named arguments; the argument at the
parameter's position absent or inert), a derived type's ``: Svc(…)`` or
``: base(…)``, the type's own ``: this(…)``, a type position, ``nameof``, or a
generic type argument (``AddScoped<Svc>()``: Microsoft's container resolves
constructor parameters by type, not position). ``typeof``,
``ActivatorUtilities``, a ``using`` alias and a mention in Razor or another
.NET guest file refuse.

A target-typed ``new(…)`` never names the type, so any one in the repository
whose argument count the constructor could accept refuses too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
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


def _site(path: str, argument_list: Node | None, index: int, sites: list[Site]) -> str | None:
    if argument_list is None:
        return None
    items = _items(argument_list)
    if any(a.child_by_field_name("name") is not None for a in items):
        return "a call with a named argument"
    if len(items) <= index:
        return None
    expression = _expression(items[index])
    if expression is None:
        return "an argument with a modifier"
    if not _inert(expression):
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


def has_parameter(source: str, class_name: str, param_name: str) -> bool:
    """Whether ``class_name`` in ``source`` still declares the parameter."""
    for declaration in _declarations(_parse(source, "csharp"), class_name):
        parameters = _primary(declaration)
        for item in _parameters(parameters) if parameters is not None else []:
            name = item.child_by_field_name("name")
            if name is not None and _text(name) == param_name:
                return True
    return False


def _argument_list(node: Node) -> Node | None:
    return next((c for c in node.children if c.type == "argument_list"), None)


def _derived(base_list: Node, path: str, index: int, sites: list[Site]) -> str | None:
    """A derived type naming the class with no arguments in its base list:
    every one of its constructors must chain to ``base(…)`` or ``this(…)``."""
    derived = base_list.parent
    body = derived.child_by_field_name("body") if derived is not None else None
    ctors = [
        m for m in (body.named_children if body is not None else []) if m.type == "constructor_declaration"
    ]
    if not ctors:
        return "a derived type with no constructor of its own"
    for ctor in ctors:
        initializer = next((c for c in ctor.children if c.type == "constructor_initializer"), None)
        if initializer is None:
            return "a derived constructor without : base(…)"
        if any(c.type == "this" for c in initializer.children):
            continue
        reason = _site(path, _argument_list(initializer), index, sites)
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
    sites: list[Site],
    namespace: str = "",
    visible: bool = True,
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
        return _site(path, _argument_list(initializer), index, sites)
    if named:
        return f"another declaration of the same name ({kind})"
    if kind == "object_creation_expression" and parent.child_by_field_name("type") == current:
        return _site(path, parent.child_by_field_name("arguments"), index, sites)
    if kind == "primary_constructor_base_type":
        return _site(path, _argument_list(parent), index, sites)
    if kind == "base_list":
        # `class D(...) : Svc(a, b)`: this grammar puts the arguments in an
        # `argument_list` right after the base type, inside the base list.
        following = current.next_named_sibling
        if following is not None and following.type == "argument_list":
            return _site(path, following, index, sites)
        return _derived(parent, path, index, sites)
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


def _arity(parameter_list: Node) -> tuple[int, float]:
    """The fewest and most arguments a call can pass. A ``params`` array is
    not a ``parameter`` node in this grammar; it makes the most unbounded."""
    parameters = _parameters(parameter_list)
    unbounded = any(c.type != "parameter" for c in parameter_list.named_children if c.type != "comment")
    required = 0
    for item in parameters:
        fixed = {item.child_by_field_name("type"), item.child_by_field_name("name")}
        has_default = any(
            c.is_named and c not in fixed and c.type not in ("attribute_list", "modifier")
            for c in item.children
        )
        required += 0 if has_default else 1
    return required, float("inf") if unbounded else float(len(parameters))


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
    arity: tuple[int, float],
    repo_dir: str,
    namespace: str = "",
) -> tuple[list[Site], str | None]:
    word = re.compile(rf"(?<![\w@]){re.escape(name)}(?!\w)".encode())
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
        for node in _walk(root):
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
            reason = _classify(node, spelled, start, index, local, namespace, visible)
            if reason is not None:
                return sites, f"{file.path}:{node.start_point[0] + 1}: {reason}"
        # Offsets into the file's bytes, BOM included.
        sites.extend(Site(s.path, s.start + offset, s.end + offset) for s in local)
    return sites, None
