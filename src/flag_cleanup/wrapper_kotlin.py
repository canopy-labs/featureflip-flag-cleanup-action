"""Kotlin profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

Parsed with the wheel grammar the sentinel pre-pass uses, because the
pre-pass is what folds a Kotlin read: a wrapper is discovered only when its
body is a read that pass would mark, judged by the pass's own functions.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, kotlin_sentinel, mock_stubs, wrapper_jvm
from flag_cleanup import wrapper_inline as wi

_COMMENTS = frozenset({"line_comment", "block_comment"})
#: Kotlin source suffixes, as the runner's Kotlin language claims them.
EXTENSIONS = (".kt", ".kts")


def parse(source: str) -> Node:
    return kotlin_sentinel._parser().parse(source.encode("utf-8")).root_node


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _named(node: Node) -> list[Node]:
    return [c for c in node.named_children if c.type not in _COMMENTS]


def _child(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def package_of(root: Node) -> str:
    header = _child(root, "package_header")
    if header is None:
        return ""
    name = _child(header, "qualified_identifier") or _child(header, "identifier")
    return "".join(_text(name).split()) if name is not None else ""


def modifier_words(node: Node) -> set[str]:
    """Every modifier keyword on a declaration (``open``, ``override``, ...)."""
    modifiers = _child(node, "modifiers")
    if modifiers is None:
        return set()
    return {
        _text(c)
        for c in modifiers.named_children
        if c.type.endswith("_modifier")
    }


def annotation_names(node: Node) -> tuple[str, ...]:
    """Simple names of the annotations on a declaration (``@Bean`` -> ``Bean``)."""
    modifiers = _child(node, "modifiers")
    names: list[str] = []
    for annotation in modifiers.named_children if modifiers is not None else ():
        if annotation.type != "annotation":
            continue
        target = next(
            (c for c in cross_file_keys.walk(annotation) if c.type == "user_type"), None
        )
        if target is not None:
            names.append(_text(target).split("<", 1)[0].rsplit(".", 1)[-1].strip())
    return tuple(sorted(names))


def is_interface(node: Node) -> bool:
    return node.type == "class_declaration" and any(
        c.type == "interface" for c in node.children if not c.is_named
    )


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
    """Whether a ``call_expression`` is a read of ``flag_key`` this run folds.

    ``loose`` also accepts any name or qualified name as the first argument --
    a superset, for widening the undo snapshot before anything is resolved.

    The sentinel pre-pass's own decision, call for call: the callee names the
    SDK read or an accessor, is not reached through ``?.``, carries no ``!``
    the grammar mis-binds into it, and its first argument resolves to the key
    -- through a same-file ``const val`` only where Gate 2 allowed it
    (``const_ok``), and through a constant of another file only by a text the
    cross-file resolution proved (``external``).
    """
    consts = kotlin_sentinel.const_val_keys(parse(source)) if const_ok else {}
    proven = {text: flag_key for text in external}
    names = {kotlin_sentinel.SDK_FUNCTION_NAME, *accessors}

    def is_read(node: Node) -> bool:
        if node.type != "call_expression" or not node.named_children:
            return False
        callee = node.named_children[0]
        if kotlin_sentinel._method_name(callee) not in names:
            return False
        if kotlin_sentinel._is_optional_call(callee):
            return False
        if kotlin_sentinel._callee_start(callee) != callee.start_byte:
            return False  # `!read`: the call's value is not the read's
        arguments = _child(node, "value_arguments")
        if arguments is None or not arguments.named_children:
            return False
        first = arguments.named_children[0]
        if kotlin_sentinel._resolved_key(first, consts, proven) == flag_key:
            return True
        return loose and kotlin_sentinel._argument_text(first.named_children[-1]) is not None

    return is_read


def _body_read(function: Node, is_read: Callable[[Node], bool]) -> bool:
    """Whether the function's whole body is one read: ``= read`` or ``{ return read }``."""
    body = _child(function, "function_body")
    if body is None:
        return False
    named = _named(body)
    if len(named) != 1:
        return False
    only = named[0]
    if only.type == "block":
        statements = _named(only)
        if len(statements) != 1 or statements[0].type != "return_expression":
            return False
        returned = _named(statements[0])
        if len(returned) != 1:
            return False
        only = returned[0]
    return is_read(only)


def _is_extension(function: Node) -> bool:
    """A receiver type before the name: ``fun Ctx.on()``."""
    name = function.child_by_field_name("name")
    for child in function.named_children:
        if name is not None and child.id == name.id:
            return False
        if child.type not in ("modifiers", "type_parameters") and child.type not in _COMMENTS:
            return True
    return False


def _getter_read(prop: Node, is_read: Callable[[Node], bool]) -> bool:
    """Whether a ``val`` is a getter whose whole body is one read.

    No initializer, delegate or setter, and not an extension property: then
    the getter is the only thing the property does.
    """
    if any(c.type == "var" for c in prop.children if not c.is_named):
        return False
    if modifier_words(prop) & {"expect", "external", "abstract"}:
        return False
    variable = _child(prop, "variable_declaration")
    getter = _child(prop, "getter")
    if variable is None or getter is None:
        return False
    for child in _named(prop):
        if child.type not in ("modifiers", "variable_declaration", "getter"):
            return False  # an initializer, a delegate, a setter, a receiver type
    return _body_read(getter, is_read)


def facade_name(path: str, root: Node) -> str | None:
    """The JVM class a file's top-level functions compile into: the
    ``@file:JvmName`` value, else ``FlagsKt`` for ``flags.kt`` (characters
    that are not valid in an identifier become ``_``, as kotlinc does).

    ``None`` when a ``JvmName`` annotation is present but its argument is not
    a single string literal, so the name cannot be known.
    """
    for annotation in root.named_children:
        if annotation.type != "file_annotation":
            continue
        text = _text(annotation)
        if "JvmName" not in text:
            continue
        match = re.search(r'JvmName\s*\(\s*(?:name\s*=\s*)?"([^"\\$]+)"\s*\)', text)
        return match.group(1) if match else None
    stem = re.sub(r"[^0-9A-Za-z_]", "_", Path(path).stem)
    if stem[:1].isdigit():
        stem = "_" + stem
    return stem[:1].upper() + stem[1:] + "Kt"


_DECLARATIONS = ("function_declaration", "property_declaration")


def _members(top: Node) -> Iterable[tuple[Node, bool]]:
    """``(declaration, static)`` for each member a wrapper can be: a
    ``function_declaration`` or a ``property_declaration``."""
    body = _child(top, "class_body")
    if body is None:
        return
    static = top.type == "object_declaration"
    for member in body.named_children:
        if member.type in _DECLARATIONS:
            yield member, static
        elif member.type == "companion_object":
            inner = _child(member, "class_body")
            for declaration in inner.named_children if inner is not None else ():
                if declaration.type in _DECLARATIONS:
                    yield declaration, True


def _wrapper_kind(declaration: Node, is_read: Callable[[Node], bool]) -> str | None:
    if declaration.type == "property_declaration":
        return "property" if _getter_read(declaration, is_read) else None
    name = declaration.child_by_field_name("name")
    if name is None or _is_extension(declaration):
        return None
    if modifier_words(declaration) & {"expect", "external", "abstract"}:
        return None
    return "method" if _body_read(declaration, is_read) else None


#: Where a ``property_declaration`` declares a member or top-level property
#: rather than a local.
MEMBER_SCOPES = ("class_body", "enum_class_body", "source_file")


def declared_name(declaration: Node) -> Node | None:
    """The name node of a function, method or property declaration."""
    if declaration.type == "property_declaration":
        variable = _child(declaration, "variable_declaration")
        return _child(variable, "identifier") if variable is not None else None
    return declaration.child_by_field_name("name")


def _arity(function: Node) -> tuple[int, int | None]:
    """``(required, max)`` positional arguments a call may pass; ``max`` is
    ``None`` with a ``vararg``.

    Only positional binding counts (named arguments are refused elsewhere), so
    ``required`` is one past the last parameter before any vararg that has no
    default. A parameter after the vararg without a default can only be passed
    by name: then no positional call binds, and ``(1, 0)`` is returned, which
    no argument count satisfies.

    A default value shows as an ``=`` after the parameter, either inside it
    (a number) or beside it (other expressions): this grammar varies.
    """
    params = _child(function, "function_value_parameters")
    declared: list[list[bool]] = []  # [has_default, is_vararg]
    vararg_next = False
    for child in params.children if params is not None else ():
        if child.type == "parameter_modifiers":
            vararg_next = vararg_next or "vararg" in _text(child).split()
        elif child.type == "parameter":
            declared.append([any(c.type == "=" for c in child.children), vararg_next])
            vararg_next = False
        elif child.type == "=" and declared:
            declared[-1][0] = True
    vararg = next((i for i, (_, is_vararg) in enumerate(declared) if is_vararg), None)
    before = declared if vararg is None else declared[:vararg]
    if vararg is not None and any(not has_default for has_default, _ in declared[vararg + 1:]):
        return 1, 0
    required = max((i + 1 for i, (has_default, _) in enumerate(before) if not has_default), default=0)
    return required, None if vararg is not None else len(declared)


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    package = package_of(root)
    found: list[wi.Wrapper] = []

    def add(declaration: Node, owner: str, static: bool, kind: str) -> None:
        found.append(
            wi.Wrapper(
                path=path,
                language="kt",
                name=_text(declared_name(declaration)),
                owner=owner,
                package=package,
                line=wi.line_of(declaration),
                start=declaration.start_byte,
                end=declaration.end_byte,
                static=static,
                annotations=annotation_names(declaration),
                kind=kind,
                arity=_arity(declaration) if kind in ("function", "method") else None,
                private=kind == "function" and "private" in modifier_words(declaration),
            )
        )

    for top in root.named_children:
        if top.type == "function_declaration":
            # A script's functions cannot be imported, so only a `.kt` file's.
            facade = facade_name(path, root) if path.endswith(".kt") else None
            if facade is not None and _wrapper_kind(top, is_read) == "method":
                add(top, facade, True, "function")
            continue
        if top.type == "class_declaration":
            if is_interface(top) or modifier_words(top) & {"enum", "annotation"}:
                continue
        elif top.type != "object_declaration":
            continue
        owner = top.child_by_field_name("name")
        if owner is None:
            continue
        for declaration, static in _members(top):
            kind = _wrapper_kind(declaration, is_read)
            if kind is not None:
                add(declaration, _text(owner), static, kind)
    return found


# --- Call sites ----------------------------------------------------------------

#: Factories whose single type argument is the type of the double they return.
_MOCK_FACTORIES = frozenset({"mockk", "spyk", "mock", "spy"})
#: What the scope walk found for a name.
_STOP = "stop"  # a scope it cannot see through: an inherited member may bind it
_UNTYPED = "untyped"


def import_parts(node: Node) -> tuple[str, str | None, bool] | None:
    """``(fqn, alias, star)`` of an ``import`` node, or ``None`` when unreadable."""
    target = _child(node, "qualified_identifier") or _child(node, "identifier")
    if target is None:
        return None
    fqn = "".join(_text(target).split())
    alias = next(
        (c for c in node.named_children if c.type == "identifier" and c.id != target.id),
        None,
    )
    star = any(c.type == "*" for c in node.children)
    return fqn, (_text(alias) if alias is not None else None), star


def imports_of(root: Node) -> wrapper_jvm.Imports:
    single: dict[str, str] = {}
    wildcards: set[str] = set()
    for node in root.named_children:
        if node.type != "import":
            continue
        parts = import_parts(node)
        if parts is None:
            continue
        fqn, alias, star = parts
        if star:
            wildcards.add(fqn)
            continue
        single[alias if alias is not None else fqn.rsplit(".", 1)[-1]] = fqn
    return wrapper_jvm.Imports(single, wildcards)


def _type_text(node: Node | None) -> str | None:
    """``a.b.C`` for a plain user type; ``None`` for anything else (nullable,
    generic, function types), which the proof does not accept."""
    if node is None or node.type != "user_type":
        return None
    if any(c.type == "type_arguments" for c in node.named_children):
        return None
    names = [c for c in node.named_children if c.type == "identifier"]
    return ".".join(_text(n) for n in names) if names else None


def _declared_type(holder: Node) -> Node | None:
    return next(
        (c for c in holder.named_children if c.type in ("user_type", "nullable_type", "function_type")),
        None,
    )


def _property_type(declaration: Node, constructors: frozenset[str]) -> str:
    """The type a ``property_declaration`` binds, or ``_UNTYPED``."""
    variable = _child(declaration, "variable_declaration")
    if variable is None:
        return _UNTYPED
    declared = _declared_type(variable)
    if declared is not None:
        return _type_text(declared) or _UNTYPED
    initializer = next(
        (
            c
            for c in declaration.named_children
            if c.type not in ("modifiers", "variable_declaration", "property_delegate")
            and c.type not in _COMMENTS
        ),
        None,
    )
    if initializer is None or initializer.type != "call_expression":
        return _UNTYPED
    callee = initializer.named_children[0]
    if callee.type != "identifier":
        return _UNTYPED
    name = _text(callee)
    if name in constructors:
        return name
    arguments = _child(initializer, "type_arguments")
    if name in _MOCK_FACTORIES and arguments is not None:
        projections = [c for c in arguments.named_children if c.type == "type_projection"]
        if len(projections) == 1:
            return _type_text(_child(projections[0], "user_type")) or _UNTYPED
    return _UNTYPED


def _named_by(node: Node, name: str) -> bool:
    return any(c.type == "identifier" and _text(c) == name for c in node.named_children)


def _class_binding(scope: Node, name: str, constructors: frozenset[str]) -> str | None:
    """The type a class, object or companion binds ``name`` to as a member."""
    constructor = _child(scope, "primary_constructor")
    parameters = _child(constructor, "class_parameters") if constructor is not None else None
    for parameter in parameters.named_children if parameters is not None else ():
        if parameter.type == "class_parameter" and _named_by(parameter, name):
            return _type_text(_declared_type(parameter)) or _UNTYPED
    body = _child(scope, "class_body")
    for member in body.named_children if body is not None else ():
        if member.type == "property_declaration":
            variable = _child(member, "variable_declaration")
            if variable is not None and _named_by(variable, name):
                return _property_type(member, constructors)
    return None


def _binding(
    call: Node, name: str, constructors: frozenset[str], class_only: bool, locals_only: bool = False
) -> str | None:
    """The type ``name`` is bound to at ``call``: a type text, ``_UNTYPED``,
    ``_STOP``, or ``None`` when nothing in the file binds it."""
    child = call
    scope = call.parent
    while scope is not None:
        found: str | None = None
        if not class_only:
            if scope.type in ("block", "lambda_literal", "statements"):
                for statement in scope.named_children:
                    if statement.start_byte >= call.start_byte:
                        break
                    if statement.type == "property_declaration":
                        variable = _child(statement, "variable_declaration")
                        multi = _child(statement, "multi_variable_declaration")
                        if variable is not None and _named_by(variable, name):
                            found = _property_type(statement, constructors)
                        elif multi is not None and any(
                            _named_by(v, name) for v in multi.named_children
                        ):
                            found = _UNTYPED
                if found is None and scope.type == "lambda_literal":
                    params = _child(scope, "lambda_parameters")
                    # A word match, not a parse: a destructured `(a, b) ->`
                    # carries no named children in this grammar.
                    if name == "it" or (
                        params is not None
                        and re.search(rf"(?<![\w`]){re.escape(name)}(?![\w`])", _text(params))
                    ):
                        found = _UNTYPED
            elif scope.type == "when_expression":
                subject = _child(scope, "when_subject")
                variable = _child(subject, "variable_declaration") if subject is not None else None
                if variable is not None and _named_by(variable, name):
                    found = _type_text(_declared_type(variable)) or _UNTYPED
            elif scope.type == "for_statement":
                variable = _child(scope, "variable_declaration")
                if variable is not None and _named_by(variable, name) and child.id != variable.id:
                    found = _type_text(_declared_type(variable)) or _UNTYPED
            elif scope.type in (
                "function_declaration",
                "anonymous_function",
                "secondary_constructor",
                "getter",
                "setter",
            ):
                params = _child(scope, "function_value_parameters")
                for parameter in params.named_children if params is not None else ():
                    if parameter.type == "parameter" and _named_by(parameter, name):
                        found = _type_text(_declared_type(parameter)) or _UNTYPED
            elif scope.type == "catch_block" and _named_by(scope, name):
                found = _type_text(_declared_type(scope)) or _UNTYPED
        if scope.type in ("class_declaration", "object_declaration", "companion_object", "object_literal"):
            if locals_only:
                return _STOP if scope.type == "object_literal" else None
            found = _class_binding(scope, name, constructors)
            if found is None and (
                scope.type == "object_literal" or _child(scope, "delegation_specifiers") is not None
            ):
                return _STOP
            # A local class sees the locals of the function around it, so the
            # walk keeps reading them; only a member class leaves them behind.
            if scope.parent is None or scope.parent.type in ("class_body", "source_file"):
                class_only = True
        elif scope.type == "source_file":
            for statement in scope.named_children:
                if statement.type == "property_declaration":
                    variable = _child(statement, "variable_declaration")
                    if variable is not None and _named_by(variable, name):
                        found = _property_type(statement, constructors)
        if found is not None:
            return found
        child = scope
        scope = scope.parent
    return None


_CLASS_SCOPES = ("class_declaration", "object_declaration", "companion_object", "object_literal")


def _in_owner(call: Node, wrapper: wi.Wrapper, path: str, bare: bool = True) -> bool:
    """Whether a receiver-less (``bare``) or ``this.`` method call binds to the
    wrapper's own declaration.

    The NEAREST enclosing class-like scope decides, as for a getter: a nested
    or inner class, an object literal or a companion brings its own receiver,
    and an instance method is not in scope in a companion or a nested class.
    Only that scope's ``class_body`` counts: its header and the arguments of a
    ``this(...)`` delegation run before any instance exists.

    A companion's method is in scope in its own body and, behind the class's
    instance, in the owner's body. There a member the owner inherits would
    win, so the owner must declare no supertypes; and ``this`` there names the
    instance, never the companion.
    """
    child = call
    scope = call.parent
    while scope is not None and scope.type not in _CLASS_SCOPES:
        if scope.type == "constructor_delegation_call":
            return False
        child = scope
        scope = scope.parent
    if (
        scope is None
        or scope.type == "object_literal"
        or path != wrapper.path
        or child.type != "class_body"
        or not scope.start_byte <= wrapper.start < scope.end_byte
    ):
        return False
    if scope.type == "companion_object":
        return True  # a wrapper inside it is one of its own members
    if scope.parent is None or scope.parent.type != "source_file":
        return False
    in_companion = any(
        c.type == "companion_object" and c.start_byte <= wrapper.start < c.end_byte
        for c in child.named_children
    )
    return not in_companion or (bare and _child(scope, "delegation_specifiers") is None)


def _operator(navigation: Node) -> str:
    return "".join(c.type for c in navigation.children if not c.is_named)


_SIDE_EFFECTS = frozenset({"call_expression", "assignment", "annotated_lambda", "lambda_literal",
                           "anonymous_function", "object_literal"})


def _argument_has_call(call: Node) -> bool:
    if _child(call, "annotated_lambda") is not None:
        return True
    parent = call.parent
    if (
        parent is not None
        and parent.type == "call_expression"
        and parent.named_children
        and parent.named_children[0].id == call.id
    ):
        return True  # `m(x) { ... }`: a trailing lambda wraps the call
    arguments = _child(call, "value_arguments")
    for node in cross_file_keys.walk(arguments) if arguments is not None else ():
        if node.type in _SIDE_EFFECTS:
            return True
        if any(c.type in ("++", "--") for c in node.children if not c.is_named):
            return True
    return False


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
    # A call `Name(...)` builds an instance of the wrapper's class only when no
    # function of that name exists anywhere that could be meant instead.
    constructors = frozenset(
        alias
        for alias, fqn in [(wrapper.owner, None), *imports.single.items()]
        if (fqn is None or fqn == f"{wrapper.package}.{wrapper.owner}")
        and not index.methods.get(alias)
    )
    skip = set()
    if path == wrapper.path:
        skip.add(wrapper.start)
    if wrapper.interface is not None and path == wrapper.interface.path:
        skip.add(wrapper.interface.start)

    def resolves(type_text: str) -> str | None:
        """``None`` when the type is the wrapper's class or interface, else why not."""
        if any(
            wrapper_jvm.type_resolves(type_text, package, imports, pkg, name)
            for pkg, name in targets
        ):
            return None
        simple = imports.single.get(type_text, type_text).rsplit(".", 1)[-1]
        if simple in {name for _, name in targets}:
            return wi.IMPORT_UNRESOLVED
        return wi.RECEIVER_UNPROVEN

    # `import lib.f` or `import lib.g as f` beside `import pkg.f`: which wins
    # depends on order, so any other explicit import bringing the name refuses the calls.
    rival_import = wrapper.kind == "function" and any(
        parts is not None and not parts[2]
        and (parts[1] or parts[0].rsplit(".", 1)[-1]) == wrapper.name and parts[0] != wrapper_fqn(wrapper)
        for parts in (import_parts(n) for n in root.named_children if n.type == "import")
    )
    found: list[wi.Occurrence] = []
    for node in cross_file_keys.walk(root):
        if node.type != "identifier" or _text(node) != wrapper.name:
            continue
        parent = node.parent
        if wrapper.kind == "function" and inside(node, "import"):
            refusal = _aliased_import(node, wrapper)
            if refusal is not None:
                found.append(wi.Occurrence(path, wi.line_of(node), refusal, None, wi.IMPORT_UNRESOLVED))
            continue
        if parent is None or parent.type == "import" or inside(node, "import"):
            continue
        if parent.type == "function_declaration" and parent.start_byte in skip:
            continue
        if parent.type == "function_declaration":
            continue  # another declaration: uniqueness already refused it
        if wrapper.kind != "method" and names_only(node):
            continue  # it names, it does not refer
        if wrapper.kind == "property":
            found.append(_property_occurrence(path, node, data, wrapper, constructors, resolves))
            continue
        if wrapper.kind == "function":
            found.append(
                _function_occurrence(path, node, data, wrapper, package, imports, constructors, rival_import)
            )
            continue
        callee: Node = node
        receiver: Node | None = None
        if parent.type == "navigation_expression" and parent.named_children[-1].id == node.id:
            callee = parent
            receiver = _unbang(parent.named_children[0])
        call = callee.parent
        line = wi.line_of(node)
        if (
            call is None
            or call.type != "call_expression"
            or call.named_children[0].id != callee.id
            or _child(call, "value_arguments") is None
        ):
            found.append(wi.Occurrence(path, line, wi.text_of(callee), None, wi.RECEIVER_UNPROVEN))
            continue
        text = wi.text_of(call)
        double = mock_stubs.in_test_double(call, data, "kt")
        reason: str | None = None
        name: str | None = None
        if receiver is not None and (_operator(callee) != "." or _labelled_this(receiver)):
            reason = wi.RECEIVER_UNPROVEN
        elif receiver is None and _binding(call, wrapper.name, constructors, False, locals_only=True) is not None:
            reason = wi.RECEIVER_UNPROVEN  # a local or parameter of a function type shadows it
        elif receiver is None and _constructor_parameter_named(call, wrapper.name):
            reason = wi.RECEIVER_UNPROVEN  # it wins over the member in initializers
        elif not _call_fits(call, wrapper.arity):
            reason = wi.RECEIVER_UNPROVEN  # Kotlin binds it to another candidate
        else:
            bare = receiver is None
            reason, name = _receiver_reason(
                call, receiver, wrapper, path, constructors, resolves,
                in_owner=lambda anchor, w, p: _in_owner(anchor, w, p, bare),
            )
        if reason is None and _argument_has_call(call):
            reason = wi.ARGUMENT_HAS_CALL
        span = (kotlin_sentinel._callee_start(callee), callee.end_byte) if reason is None else None
        found.append(wi.Occurrence(path, line, text, span, reason, name, double))
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def names_only(node: Node) -> bool:
    """Whether an identifier only NAMES something rather than refers to it: the
    declared name of a local, property or parameter (its first identifier --
    a bare default value after ``=`` is a reference), or a named-argument
    label (``f(name = v)``)."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("variable_declaration", "class_parameter", "parameter"):
        first = next((c for c in parent.named_children if c.type == "identifier"), None)
        return first is not None and first.id == node.id
    return (
        parent.type == "value_argument"
        and parent.named_children[0].id == node.id
        and any(c.type == "=" for c in parent.children)
    )


def wrapper_fqn(wrapper: wi.Wrapper) -> str:
    return f"{wrapper.package}.{wrapper.name}" if wrapper.package else wrapper.name


def _aliased_import(node: Node, wrapper: wi.Wrapper) -> str | None:
    """The import's text when it imports the wrapper under another name:
    the calls then never spell it, so none of them can be proven."""
    statement: Node | None = node
    while statement is not None and statement.type != "import":
        statement = statement.parent
    parts = import_parts(statement) if statement is not None else None
    if statement is None or parts is None:
        return None
    fqn, alias, star = parts
    if alias is None or star or fqn != wrapper_fqn(wrapper):
        return None
    return wi.text_of(statement)


def _in_extension(node: Node) -> bool:
    """Whether an extension function or extension property's body encloses ``node``:
    a member of its receiver would then win over any import or owner member."""
    scope = node.parent
    while scope is not None:
        if scope.type == "function_declaration" and _is_extension(scope):
            return True
        if scope.type == "anonymous_function" and any(
            c.type not in ("modifiers", "type_parameters", "annotation") and c.type not in _COMMENTS
            for c in scope.named_children
            if c.start_byte < _first_start(scope, "function_value_parameters")
        ):
            return True  # `fun Base.() = ...`, `fun (Base).() = ...`: anything before the parameters
        if scope.type in ("getter", "setter") and scope.parent is not None:
            prop = scope.parent
            variable = _child(prop, "variable_declaration")
            for child in prop.named_children:
                if variable is not None and child.id == variable.id:
                    break
                if child.type not in ("modifiers", "type_parameters") and child.type not in _COMMENTS:
                    return True
        scope = scope.parent
    return False


def _first_start(node: Node, kind: str) -> int:
    child = _child(node, kind)
    return child.start_byte if child is not None else node.start_byte


def _call_fits(call: Node, arity: tuple[int, int | None] | None) -> bool:
    """Whether the call could be bound to a function with this arity: no type
    arguments, no named or spread argument, a count within the signature."""
    if _child(call, "type_arguments") is not None:
        return False
    arguments = _child(call, "value_arguments")
    given = 0
    for argument in arguments.named_children if arguments is not None else ():
        if argument.type != "value_argument":
            continue
        if any(c.type in ("=", "spread_expression") for c in argument.children):
            return False
        given += 1
    if arity is None:
        return True
    required, most = arity
    return given >= required and (most is None or given <= most)


def _function_occurrence(
    path: str,
    node: Node,
    data: bytes,
    wrapper: wi.Wrapper,
    package: str,
    imports: wrapper_jvm.Imports,
    constructors: frozenset[str],
    rival_import: bool = False,
) -> wi.Occurrence:
    """One mention of a top-level function wrapper: only a bare call is proven.

    The name must resolve to the wrapper through Kotlin's own order (an
    explicit import, then the file's package, then a star import), and no
    closer scope may bind it: not a local or parameter, not an extension
    receiver, and not a class or object with supertypes, whose inherited
    member would win over the import. The call must also fit the signature,
    or Kotlin would bind it to another function.
    """
    line = wi.line_of(node)
    callee: Node = node
    parent = node.parent
    # `!f()` parses as `(!f)()`: the `!` stays in the source, the mark goes on `f`.
    if parent is not None and parent.type == "unary_expression" and parent.children[0].type == "!":
        callee = parent
    call = callee.parent
    if (
        call is None
        or call.type != "call_expression"
        or call.named_children[0].id != callee.id
        or _child(call, "value_arguments") is None
        or (parent is not None and parent.type == "navigation_expression")
    ):
        return wi.Occurrence(path, line, wi.text_of(callee), None, wi.RECEIVER_UNPROVEN)
    text = wi.text_of(call)
    double = mock_stubs.in_test_double(call, data, "kt")
    reason: str | None = None
    if path.endswith(".kts") or _in_extension(call):
        reason = wi.RECEIVER_UNPROVEN  # a script host or an extension receiver may supply the name
    elif wrapper.private and path != wrapper.path:
        reason = wi.RECEIVER_UNPROVEN  # not visible here: the call binds to something else
    elif rival_import or not wrapper_jvm.type_resolves(
        wrapper.name, package, imports, wrapper.package, wrapper.name
    ):
        reason = wi.IMPORT_UNRESOLVED
    elif _binding(call, wrapper.name, constructors, class_only=False) is not None:
        reason = wi.RECEIVER_UNPROVEN
    elif not _call_fits(call, wrapper.arity):
        reason = wi.RECEIVER_UNPROVEN
    elif _argument_has_call(call):
        reason = wi.ARGUMENT_HAS_CALL
    span = (node.start_byte, node.end_byte) if reason is None else None
    return wi.Occurrence(path, line, text, span, reason, None, double)


def _property_occurrence(
    path: str,
    node: Node,
    data: bytes,
    wrapper: wi.Wrapper,
    constructors: frozenset[str],
    resolves: Callable[[str], str | None],
) -> wi.Occurrence:
    """One mention of a getter wrapper: ``recv.x``, ``this.x`` or a bare ``x``.

    A getter is read, never called, so the read must be the whole use: not
    the callee of a call (that invokes what it returns), not assigned to, not
    a callable reference (``recv::x``, which this grammar parses as a
    navigation with a ``::`` operator).
    """
    parent = node.parent
    read: Node = node
    receiver: Node | None = None
    if parent.type == "navigation_expression" and parent.named_children[-1].id == node.id:
        read = parent
        receiver = _unbang(parent.named_children[0])
    line = wi.line_of(node)
    used = read.parent
    called = (
        used is not None
        and used.type == "call_expression"
        and used.named_children
        and used.named_children[0].id == read.id
    )
    assigned = (
        used is not None and used.type == "assignment" and used.named_children[0].id == read.id
    )
    double = mock_stubs.in_test_double(read, data, "kt")
    reason: str | None = None
    name: str | None = None
    labelled_this = receiver is not None and (
        _labelled_this(receiver)
        or (
            receiver.type == "navigation_expression"
            and bool(receiver.named_children)
            and _labelled_this(_unbang(receiver.named_children[0]))
        )
    )
    misplaced = parent.type in ("user_type", "callable_reference") or (
        parent.type == "labeled_expression"
        and any(c.type == "label" and c.end_byte == node.start_byte for c in parent.children)
    )
    if called or assigned or misplaced or labelled_this or (receiver is not None and _operator(read) != "."):
        reason = wi.RECEIVER_UNPROVEN
    elif (receiver is None or receiver.type == "this_expression") and _in_extension(read):
        reason = wi.RECEIVER_UNPROVEN  # `this` is the extension's receiver, not the owner
    elif receiver is None and _binding(
        read, wrapper.name, constructors, False, locals_only=True
    ) is not None:
        reason = wi.RECEIVER_UNPROVEN  # a local or parameter shadows it
    elif receiver is None and _constructor_parameter_named(read, wrapper.name):
        reason = wi.RECEIVER_UNPROVEN  # it wins over the member in initializers
    else:
        reason, name = _receiver_reason(
            read, receiver, wrapper, path, constructors, resolves, in_owner=_in_owner_declaration
        )
    span = (kotlin_sentinel._callee_start(read), read.end_byte) if reason is None else None
    return wi.Occurrence(path, line, wi.text_of(read), span, reason, name, double, "()" if span else "")


def _labelled_this(node: Node) -> bool:
    """``this@X``: a ``this`` that names a scope, which the proof does not follow."""
    return node.type == "this_expression" and bool(node.named_children)


def _in_owner_declaration(read: Node, wrapper: wi.Wrapper, path: str) -> bool:
    """Whether the NEAREST enclosing class-like scope is the wrapper's own declaration.

    A getter is an instance member: a bare read inside a companion, a nested
    class, an inner class or an object literal binds to that scope's receiver,
    not to the owner's instance, so only the owner's own body proves it.

    Only its ``class_body``: the class header (annotations, the primary
    constructor's defaults, the supertype arguments) is evaluated before any
    instance exists, or reads a constructor parameter or an import instead,
    and so are the arguments of a ``this(...)`` delegation.
    """
    child = read
    scope = read.parent
    while scope is not None:
        if scope.type == "constructor_delegation_call":
            return False
        if scope.type in ("class_declaration", "object_declaration", "companion_object", "object_literal"):
            return (
                path == wrapper.path
                and child.type == "class_body"
                and scope.type != "companion_object"
                and scope.parent is not None
                and scope.parent.type == "source_file"
                and scope.start_byte <= wrapper.start < scope.end_byte
            )
        child = scope
        scope = scope.parent
    return False


def _constructor_parameter_named(read: Node, name: str) -> bool:
    """Whether the nearest enclosing class's primary constructor declares a
    parameter ``name``, ``val`` or not. A plain one is what a bare ``name``
    means in the class's initializers, delegates and ``init`` blocks."""
    scope = read.parent
    while scope is not None:
        if scope.type in ("object_declaration", "companion_object", "object_literal"):
            return False
        if scope.type == "class_declaration":
            constructor = _child(scope, "primary_constructor")
            parameters = _child(constructor, "class_parameters") if constructor is not None else None
            # The first identifier is the parameter's name; a bare default
            # value after `=` is another identifier child.
            return any(
                p.type == "class_parameter" and (first := _child(p, "identifier")) is not None
                and _text(first) == name
                for p in (parameters.named_children if parameters is not None else ())
            )
        scope = scope.parent
    return False


def _receiver_reason(
    anchor: Node,
    receiver: Node | None,
    wrapper: wi.Wrapper,
    path: str,
    constructors: frozenset[str],
    resolves: Callable[[str], str | None],
    in_owner: Callable[[Node, wi.Wrapper, str], bool] = _in_owner,
) -> tuple[str | None, str | None]:
    """Why ``receiver`` is not proven to be the wrapper (``None`` when it is), and
    the name it was traced through. ``anchor`` is the node the scope walk starts at."""
    if receiver is None or receiver.type == "this_expression":
        return (None if in_owner(anchor, wrapper, path) else wi.RECEIVER_UNPROVEN), None
    if receiver.type == "identifier":
        name = _text(receiver)
        bound = _binding(anchor, name, constructors, class_only=False)
        if bound is None:
            return (None if wrapper.static and resolves(name) is None else wi.RECEIVER_UNPROVEN), name
        if bound in (_STOP, _UNTYPED):
            return wi.RECEIVER_UNPROVEN, name
        return resolves(bound), name
    if (
        receiver.type == "navigation_expression"
        and _operator(receiver) == "."
        and len(receiver.named_children) == 2
        and _unbang(receiver.named_children[0]).type == "this_expression"
        and receiver.named_children[1].type == "identifier"
    ):
        name = _text(receiver.named_children[1])
        bound = _binding(anchor, name, constructors, class_only=True)
        return (wi.RECEIVER_UNPROVEN if bound in (None, _STOP, _UNTYPED) else resolves(bound)), name
    return wi.RECEIVER_UNPROVEN, None


def _unbang(node: Node) -> Node:
    """The operand under a ``!`` the wheel grammar binds to a receiver.

    ``!recv.m()`` means ``!(recv.m())``, but the tree hangs the ``!`` on
    ``recv`` (see :func:`kotlin_sentinel._callee_start`). The mark starts after
    it, so the negation stays in the source; the receiver is its operand.
    """
    while (
        node.type == "unary_expression"
        and node.children
        and node.children[0].type == "!"
        and node.named_children
    ):
        node = node.named_children[-1]
    return node


def inside(node: Node, kind: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == kind:
            return True
        parent = parent.parent
    return False


wi.register(
    wi.Profile(
        language="kt",
        extensions=EXTENSIONS,
        parse=parse,
        read_test=read_test,
        discover=discover,
        occurrences=occurrences,
        peers=("java",),
    )
)
