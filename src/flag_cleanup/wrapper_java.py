"""Java profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

Parsed with the wheel grammar Gate 1 uses. A wrapper is discovered only when
its body is a read the rules fold in this run: a literal key, a same-file key
constant where Gate 2 let the const rules run, or a constant of another file
by a text the cross-file resolution proved.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from tree_sitter import Node

from flag_cleanup import cross_file_keys, key_const, mock_stubs, syntax, wrapper_jvm
from flag_cleanup import wrapper_inline as wi

EXTENSIONS = (".java",)
_COMMENTS = frozenset({"line_comment", "block_comment"})


def parse(source: str) -> Node:
    return syntax._parse(source, "java")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _named(node: Node) -> list[Node]:
    return [c for c in node.named_children if c.type not in _COMMENTS]


def _child(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def package_of(root: Node) -> str:
    declaration = _child(root, "package_declaration")
    if declaration is None or not declaration.named_children:
        return ""
    return "".join(_text(declaration.named_children[0]).split())


def modifier_words(node: Node) -> set[str]:
    """Modifier keywords (``public``, ``static``) and annotation names (``Override``)."""
    modifiers = _child(node, "modifiers")
    if modifiers is None:
        return set()
    words: set[str] = set()
    for child in modifiers.children:
        if child.type in ("marker_annotation", "annotation"):
            name = child.child_by_field_name("name")
            if name is not None:
                words.add("@" + _text(name).rsplit(".", 1)[-1])
        elif not child.is_named:
            words.add(child.type)
    return words


def _plain_string(node: Node) -> str | None:
    if node.type != "string_literal":
        return None
    parts = _named(node)
    if len(parts) != 1 or parts[0].type != "string_fragment":
        return None
    return _text(parts[0])


def _name_text(node: Node) -> str | None:
    """``NAME`` or ``Owner.NAME`` as one text, or ``None`` for anything else."""
    if node.type == "identifier":
        return _text(node)
    if node.type == "field_access":
        owner = node.child_by_field_name("object")
        field = node.child_by_field_name("field")
        if owner is not None and field is not None and owner.type == "identifier":
            return f"{_text(owner)}.{_text(field)}"
    return None


def read_test(
    path: str,
    source: str,
    flag_key: str,
    accessors: tuple[str, ...],
    const_ok: bool,
    external: Iterable[str],
    *,
    loose: bool = False,
) -> Callable[[Node], bool]:
    """Whether a ``method_invocation`` is a read of ``flag_key`` this run folds.

    ``loose`` also accepts any name or ``Owner.NAME`` as the first argument --
    a superset, for widening the undo snapshot before anything is resolved."""
    root = parse(source)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "java", flag_key)}
        if const_ok
        else set()
    )
    proven = set(external)
    names = key_const._JAVA_FLAG_READ_CALLEES | set(accessors)

    def is_read(node: Node) -> bool:
        if node.type != "method_invocation":
            return False
        name = node.child_by_field_name("name")
        arguments = node.child_by_field_name("arguments")
        if name is None or _text(name) not in names or arguments is None:
            return False
        args = _named(arguments)
        if not args:
            return False
        first = args[0]
        if _plain_string(first) == flag_key:
            return True
        text = _name_text(first)
        return text is not None and (
            loose or text in proven or (first.type == "identifier" and text in consts)
        )

    return is_read


def _body_read(method: Node, is_read: Callable[[Node], bool]) -> bool:
    """Whether the method's body is exactly ``{ return <read>; }``."""
    body = method.child_by_field_name("body")
    if body is None:
        return False
    statements = _named(body)
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    returned = _named(statements[0])
    return len(returned) == 1 and is_read(returned[0])


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    package = package_of(root)
    found: list[wi.Wrapper] = []
    for top in root.named_children:
        if top.type != "class_declaration":
            continue
        owner = top.child_by_field_name("name")
        body = top.child_by_field_name("body")
        if owner is None or body is None:
            continue
        for member in body.named_children:
            if member.type != "method_declaration":
                continue
            words = modifier_words(member)
            if words & {"abstract", "native"}:
                continue
            name = member.child_by_field_name("name")
            if name is None or not _body_read(member, is_read):
                continue
            found.append(
                wi.Wrapper(
                    path=path,
                    language="java",
                    name=_text(name),
                    owner=_text(owner),
                    package=package,
                    line=wi.line_of(member),
                    start=member.start_byte,
                    end=member.end_byte,
                    static="static" in words,
                    annotations=tuple(sorted(w[1:] for w in words if w.startswith("@"))),
                )
            )
    return found


# --- Call sites ----------------------------------------------------------------

_STOP = "stop"
_UNTYPED = "untyped"
_MOCK_FACTORIES = frozenset({"mock", "spy"})
_SIDE_EFFECTS = frozenset(
    {
        "method_invocation",
        "object_creation_expression",
        "assignment_expression",
        "update_expression",
        "lambda_expression",
        "method_reference",
        "array_creation_expression",
    }
)
_CLASS_SCOPES = frozenset({"class_declaration", "enum_declaration", "record_declaration"})


def imports_of(root: Node) -> wrapper_jvm.Imports:
    single: dict[str, str] = {}
    wildcards: set[str] = set()
    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        if any(c.type == "static" for c in node.children):
            continue  # binds members, never a type name this proof reads
        target = next(
            (c for c in node.named_children if c.type in ("scoped_identifier", "identifier")),
            None,
        )
        if target is None:
            continue
        fqn = "".join(_text(target).split())
        if any(c.type == "asterisk" for c in node.named_children):
            wildcards.add(fqn)
        else:
            single[fqn.rsplit(".", 1)[-1]] = fqn
    return wrapper_jvm.Imports(single, wildcards)


def _type_text(node: Node | None) -> str | None:
    """``C`` or ``a.b.C`` for a plain class type, else ``None``."""
    if node is None:
        return None
    if node.type == "type_identifier":
        return _text(node)
    if node.type == "scoped_type_identifier":
        return "".join(_text(node).split())
    return None


def _initializer_type(value: Node | None) -> str:
    """The type a ``var`` initializer creates: ``new C()`` or ``mock(C.class)``."""
    if value is None:
        return _UNTYPED
    if value.type == "object_creation_expression":
        if any(c.type == "class_body" for c in value.named_children):
            return _UNTYPED  # an anonymous subclass
        return _type_text(value.child_by_field_name("type")) or _UNTYPED
    if value.type == "method_invocation":
        name = value.child_by_field_name("name")
        arguments = value.child_by_field_name("arguments")
        receiver = value.child_by_field_name("object")
        if (
            name is not None
            and _text(name) in _MOCK_FACTORIES
            and (receiver is None or _text(receiver) == "Mockito")
            and arguments is not None
        ):
            args = _named(arguments)
            if len(args) == 1 and args[0].type == "class_literal":
                return _type_text(args[0].named_children[0]) or _UNTYPED
    return _UNTYPED


def _declarator_type(declaration: Node, name: str) -> str | None:
    """The type a field or local declaration binds ``name`` to, if it binds it."""
    type_node = declaration.child_by_field_name("type")
    for declarator in declaration.named_children:
        if declarator.type != "variable_declarator":
            continue
        declared = declarator.child_by_field_name("name")
        if declared is None or _text(declared) != name:
            continue
        if declarator.child_by_field_name("dimensions") is not None:
            return _UNTYPED
        if type_node is not None and _text(type_node) == "var":
            return _initializer_type(declarator.child_by_field_name("value"))
        return _type_text(type_node) or _UNTYPED
    return None


def _parameter_type(parameters: Node | None, name: str) -> str | None:
    for parameter in parameters.named_children if parameters is not None else ():
        if parameter.type not in ("formal_parameter", "spread_parameter", "catch_formal_parameter"):
            continue
        declared = parameter.child_by_field_name("name") or next(
            (c for c in parameter.named_children if c.type == "identifier"), None
        )
        if declared is not None and _text(declared) == name:
            if parameter.type == "spread_parameter":
                return _UNTYPED
            if parameter.type == "catch_formal_parameter":
                return _UNTYPED  # an exception, never the wrapper
            return _type_text(parameter.child_by_field_name("type")) or _UNTYPED
    return None


def _class_binding(scope: Node, name: str) -> str | None:
    body = scope.child_by_field_name("body") if scope.type != "object_creation_expression" else _child(scope, "class_body")
    if scope.type == "record_declaration":
        found = _parameter_type(scope.child_by_field_name("parameters"), name)
        if found is not None:
            return found
    for member in body.named_children if body is not None else ():
        if member.type in ("field_declaration", "constant_declaration"):
            found = _declarator_type(member, name)
            if found is not None:
                return found
    return None


_PATTERNS = frozenset({"instanceof_expression", "type_pattern", "record_pattern_component"})
def _pattern_binds(call: Node, name: str) -> bool:
    """Whether a pattern variable named ``name`` is declared in the body around ``call``.

    ``o instanceof T name`` and record patterns bind a name whose scope follows
    flow, not blocks, so any such binding in the enclosing member makes the
    name unprovable rather than being placed exactly.
    """
    scope = call.parent
    body = None
    while scope is not None:
        if scope.type in ("method_declaration", "constructor_declaration", "static_initializer"):
            body = scope
            break
        if scope.type in _CLASS_SCOPES:
            break
        scope = scope.parent
    for node in cross_file_keys.walk(body) if body is not None else ():
        if node.type in _PATTERNS and any(
            c.type == "identifier" and _text(c) == name for c in node.named_children
        ):
            return True
    return False


def _binding(call: Node, name: str, class_only: bool) -> str | None:
    """The type ``name`` is bound to at ``call``: a type text, ``_UNTYPED``,
    ``_STOP``, or ``None`` when nothing in the file binds it."""
    if not class_only and _pattern_binds(call, name):
        return _UNTYPED
    scope = call.parent
    while scope is not None:
        found: str | None = None
        if not class_only:
            if scope.type in ("block", "switch_block_statement_group", "constructor_body"):
                for statement in scope.named_children:
                    if statement.start_byte >= call.start_byte:
                        break
                    if statement.type == "local_variable_declaration":
                        found = _declarator_type(statement, name) or found
            elif scope.type in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
                found = _parameter_type(scope.child_by_field_name("parameters"), name)
            elif scope.type == "lambda_expression":
                params = scope.child_by_field_name("parameters")
                if params is not None and params.type == "identifier":
                    found = _UNTYPED if _text(params) == name else None
                elif params is not None and params.type == "inferred_parameters":
                    found = _UNTYPED if name in _text(params).strip("()").replace(",", " ").split() else None
                else:
                    found = _parameter_type(params, name)
            elif scope.type == "enhanced_for_statement":
                declared = scope.child_by_field_name("name")
                if declared is not None and _text(declared) == name:
                    found = _type_text(scope.child_by_field_name("type")) or _UNTYPED
            elif scope.type in ("for_statement", "try_with_resources_statement"):
                for part in cross_file_keys.walk(scope):
                    if part.type in ("local_variable_declaration", "resource") and part.start_byte < call.start_byte:
                        if part.type == "resource":
                            declared = part.child_by_field_name("name")
                            if declared is not None and _text(declared) == name:
                                found = _type_text(part.child_by_field_name("type")) or _UNTYPED
                        else:
                            found = _declarator_type(part, name) or found
            elif scope.type == "catch_clause":
                formal = _child(scope, "catch_formal_parameter")
                declared = formal.child_by_field_name("name") if formal is not None else None
                if declared is not None and _text(declared) == name:
                    found = _UNTYPED
        if scope.type in _CLASS_SCOPES:
            found = _class_binding(scope, name)
            if found is None and (
                scope.child_by_field_name("superclass") is not None
                or scope.child_by_field_name("interfaces") is not None
            ):
                return _STOP
            # A local class sees the locals around it; a member class does not.
            if scope.parent is None or scope.parent.type in ("class_body", "program"):
                class_only = True
        elif scope.type == "object_creation_expression" and _child(scope, "class_body") is not None:
            found = _class_binding(scope, name)
            if found is None:
                return _STOP
            class_only = True
        if found is not None:
            return found
        scope = scope.parent
    return None


def _in_owner(call: Node, wrapper: wi.Wrapper, path: str) -> bool:
    scope = call.parent
    while scope is not None:
        if scope.type == "object_creation_expression" and _child(scope, "class_body") is not None:
            return False
        if scope.type in _CLASS_SCOPES or scope.type == "interface_declaration":
            if (
                path == wrapper.path
                and scope.parent is not None
                and scope.parent.type == "program"
                and scope.start_byte <= wrapper.start < scope.end_byte
            ):
                return True
            if (
                scope.child_by_field_name("superclass") is not None
                or scope.child_by_field_name("interfaces") is not None
            ):
                return False
        scope = scope.parent
    return False


def _argument_has_call(call: Node) -> bool:
    arguments = call.child_by_field_name("arguments")
    return any(
        node.type in _SIDE_EFFECTS
        for node in (cross_file_keys.walk(arguments) if arguments is not None else ())
        if node.id != arguments.id
    )


def occurrences(
    path: str, source: str, wrapper: wi.Wrapper, index: wrapper_jvm.Index
) -> list[wi.Occurrence]:
    root = parse(source)
    data = source.encode("utf-8")
    package = package_of(root)
    imports = imports_of(root)
    targets = [(wrapper.package, wrapper.owner)]
    if wrapper.interface is not None:
        targets.append((wrapper.interface.package, wrapper.interface.name))
    skip = set()
    if path == wrapper.path:
        skip.add(wrapper.start)
    if wrapper.interface is not None and path == wrapper.interface.path:
        skip.add(wrapper.interface.start)

    def resolves(type_text: str) -> str | None:
        if any(
            wrapper_jvm.type_resolves(type_text, package, imports, pkg, name)
            for pkg, name in targets
        ):
            return None
        simple = type_text.rsplit(".", 1)[-1]
        if simple in {name for _, name in targets}:
            return wi.IMPORT_UNRESOLVED
        return wi.RECEIVER_UNPROVEN

    found: list[wi.Occurrence] = []
    for node in cross_file_keys.walk(root):
        if node.type != "identifier" or _text(node) != wrapper.name:
            continue
        parent = node.parent
        if parent is None or parent.type in ("import_declaration", "scoped_identifier"):
            continue
        if parent.type == "method_declaration" and parent.child_by_field_name("name") == node:
            continue  # a declaration: the wrapper, its interface member, or (refused) another
        line = wi.line_of(node)
        if parent.type != "method_invocation" or parent.child_by_field_name("name") != node:
            found.append(wi.Occurrence(path, line, wi.text_of(parent), None, wi.RECEIVER_UNPROVEN))
            continue
        call = parent
        receiver = call.child_by_field_name("object")
        double = mock_stubs.in_test_double(call, data, "java")
        reason: str | None = None
        name: str | None = None
        if receiver is None or receiver.type == "this":
            if not _in_owner(call, wrapper, path):
                reason = wi.RECEIVER_UNPROVEN
        elif receiver.type == "identifier":
            name = _text(receiver)
            bound = _binding(call, name, class_only=False)
            if bound is None:
                reason = None if wrapper.static and resolves(name) is None else wi.RECEIVER_UNPROVEN
            elif bound in (_STOP, _UNTYPED):
                reason = wi.RECEIVER_UNPROVEN
            else:
                reason = resolves(bound)
        elif (
            receiver.type == "field_access"
            and receiver.child_by_field_name("object") is not None
            and receiver.child_by_field_name("object").type == "this"
        ):
            name = _text(receiver.child_by_field_name("field"))
            bound = _binding(call, name, class_only=True)
            reason = wi.RECEIVER_UNPROVEN if bound in (None, _STOP, _UNTYPED) else resolves(bound)
        else:
            reason = wi.RECEIVER_UNPROVEN
        if reason is None and _argument_has_call(call):
            reason = wi.ARGUMENT_HAS_CALL
        span = (call.start_byte, node.end_byte) if reason is None else None
        found.append(wi.Occurrence(path, line, wi.text_of(call), span, reason, name, double))
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


wi.register(
    wi.Profile(
        language="java",
        extensions=EXTENSIONS,
        parse=parse,
        read_test=read_test,
        discover=discover,
        occurrences=occurrences,
        peers=("kt",),
    )
)
