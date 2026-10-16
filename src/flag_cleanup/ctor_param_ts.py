"""Prove a TypeScript constructor parameter property can be removed.

``constructor(private client: Client)`` declares a property the fold may leave
unread. Removing it changes the constructor, so every way the class can be
constructed has to be accounted for first. TypeScript's types are structural,
so the proof is by NAME, as for keyless wrappers: every spelling of the class
name in the repository's TypeScript and JavaScript must be one of a short list
of shapes, and anything else refuses.

Allowed: ``new Svc(…)`` (no spread; the argument at the parameter's position
absent or inert), ``extends Svc`` from a class with its own constructor whose
one ``super(…)`` meets the same rule, a type position, an import or export
specifier without an alias, and the right side of ``instanceof``. Strings and
comments are not references: they cannot construct anything.

Refused on the declaration side: a decorator on the class, the constructor or
any of its parameters (dependency injection resolves those by position or
token), a default export (it can be imported under any name), an overloaded
constructor, a constructor that still reads the parameter by its bare name,
and ``new this(…)`` inside the class.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_ts, source_index, ts_scope
from flag_cleanup.ctor_param_model import IDENT, Refusal, Removal, Site
from flag_cleanup.unused import _parse

_CLASSES = frozenset({"class_declaration", "abstract_class_declaration"})
_PARAMETERS = frozenset({"required_parameter", "optional_parameter"})
_SIGNATURES = frozenset({"method_definition", "method_signature", "abstract_method_signature"})
_NAMES = frozenset(
    {
        "identifier",
        "type_identifier",
        "property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
    }
)
_INERT_LEAVES = frozenset(
    {"string", "number", "true", "false", "null", "undefined", "identifier", "this"}
)
_INERT_WRAPPERS = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"}
)


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _extensions() -> tuple[str, ...]:
    from flag_cleanup.piranha_runner import _LANGUAGES

    return tuple(e for g in ("ts", "tsx", "js") for e in _LANGUAGES[g].extensions)


def _inert(node: Node) -> bool:
    """Whether evaluating ``node`` as an argument can do nothing but read."""
    if node.type in _INERT_LEAVES:
        return True
    if node.type == "template_string":
        return not any(c.type == "template_substitution" for c in node.named_children)
    if node.type in _INERT_WRAPPERS:
        return bool(node.named_children) and _inert(node.named_children[0])
    if node.type == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        return (
            obj is not None
            and prop is not None
            and prop.type == "property_identifier"
            and _inert(obj)
        )
    return False


def _site(path: str, arguments: Node | None, index: int, sites: list[Site]) -> str | None:
    """Record the argument at ``index`` of a call, or say why it can't be."""
    if arguments is None:
        return None
    items = [c for c in arguments.named_children if c.type != "comment"]
    if any(c.type == "spread_element" for c in items):
        return "a call spreads its arguments"
    if len(items) <= index:
        return None
    argument = items[index]
    if not _inert(argument):
        return f"an argument that may do work ({' '.join(_text(argument).split())[:60]})"
    sites.append(Site(path, argument.start_byte, argument.end_byte))
    return None


def _constructors(body: Node) -> list[Node]:
    return [
        m
        for m in body.named_children
        if m.type in _SIGNATURES
        and (n := m.child_by_field_name("name")) is not None
        and _text(n) == "constructor"
    ]


def _decorated(cls: Node) -> bool:
    parent = cls.parent
    return any(c.type == "decorator" for c in cls.children) or (
        parent is not None
        and parent.type == "export_statement"
        and any(c.type == "decorator" for c in parent.children)
    )


def _parameter(ctor: Node, name: str) -> tuple[int, Node] | None:
    """``(position, node)`` of ``ctor``'s ``private`` parameter property ``name``."""
    parameters = ctor.child_by_field_name("parameters")
    items = [c for c in parameters.named_children if c.type != "comment"] if parameters else []
    for index, item in enumerate(items):
        pattern = item.child_by_field_name("pattern")
        if (
            item.type in _PARAMETERS
            and pattern is not None
            and pattern.type == "identifier"
            and _text(pattern) == name
            and any(
                c.type == "accessibility_modifier" and _text(c) == "private" for c in item.children
            )
        ):
            return index, item
    return None


def _classes(root: Node, name: str) -> list[Node]:
    return [
        n
        for n in _walk(root)
        if n.type in _CLASSES
        and (cn := n.child_by_field_name("name")) is not None
        and _text(cn) == name
    ]


def has_parameter(source: str, grammar: str, class_name: str, param_name: str) -> bool:
    """Whether ``class_name`` in ``source`` still declares the property."""
    for cls in _classes(_parse(source, grammar), class_name):
        body = cls.child_by_field_name("body")
        for ctor in _constructors(body) if body is not None else []:
            if _parameter(ctor, param_name) is not None:
                return True
    return False


def _subclass(extends: Node, path: str, index: int, sites: list[Site]) -> str | None:
    heritage = extends.parent
    cls = heritage.parent if heritage is not None else None
    body = cls.child_by_field_name("body") if cls is not None else None
    ctors = (
        [c for c in _constructors(body) if c.type == "method_definition"] if body is not None else []
    )
    if len(ctors) != 1:
        return "a subclass with no constructor of its own"
    supers = [
        n
        for n in _walk(ctors[0])
        if n.type == "call_expression"
        and (f := n.child_by_field_name("function")) is not None
        and f.type == "super"
    ]
    if len(supers) != 1:
        return "a subclass constructor without exactly one super(…)"
    return _site(path, supers[0].child_by_field_name("arguments"), index, sites)


def _specifier_source(specifier: Node) -> str | None:
    """The module an import or export specifier names, or ``None`` for a
    local ``export { Svc }``."""
    statement = specifier.parent
    while statement is not None and statement.type not in ("import_statement", "export_statement"):
        statement = statement.parent
    source = statement.child_by_field_name("source") if statement is not None else None
    if source is None:
        return None
    return "".join(_text(c) for c in source.named_children if c.type == "string_fragment")


def _classify(
    node: Node,
    path: str,
    decl_start: int | None,
    index: int,
    sites: list[Site],
    resolves_to_decl=lambda spec: False,
) -> str | None:
    """``None`` when this spelling of the class name is accounted for (and
    records its argument, if it is a call), else why it is not.

    ``resolves_to_decl(specifier)`` says whether a module specifier in this
    file names the declaring file."""
    parent = node.parent
    if parent is None:
        return "an unexpected position"
    named = parent.child_by_field_name("name") == node
    if node.type == "type_identifier":
        if named and parent.type in _CLASSES:
            return None if parent.start_byte == decl_start else "another class of the same name"
        if named and parent.type != "nested_type_identifier":
            return f"another declaration of the same name ({parent.type})"
        return None  # a type position
    if node.type != "identifier":
        return f"a member or key of the same name ({parent.type})"
    if parent.type == "new_expression" and parent.child_by_field_name("constructor") == node:
        return _site(path, parent.child_by_field_name("arguments"), index, sites)
    if parent.type in ("import_specifier", "export_specifier"):
        if parent.child_by_field_name("alias") is not None:
            return "imported or exported under another name"
        spec = _specifier_source(parent)
        if spec is None:
            # `export { Svc }`: a binding of this file, so only the declaring one.
            return None if decl_start is not None else "exported by a file that does not declare it"
        if not resolves_to_decl(spec):
            return f"imported from {spec!r}, which is not its declaring file"
        return None
    if (
        parent.type == "binary_expression"
        and parent.child_by_field_name("right") == node
        and any(c.type == "instanceof" for c in parent.children)
    ):
        return None
    if parent.type == "extends_clause" and parent.child_by_field_name("value") == node:
        return _subclass(parent, path, index, sites)
    # A JSX tag is a `name` field too, and constructs the class: a value use.
    if named and not parent.type.startswith("jsx_"):
        return f"another declaration of the same name ({parent.type})"
    return f"used as a value ({parent.type})"


def _references(
    name: str, decl_path: str, decl_start: int, index: int, repo_dir: str
) -> tuple[list[Site], str | None]:
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])".encode())
    wanted = _extensions()
    decl = str(Path(decl_path).resolve())
    sites: list[Site] = []
    for file in source_index.files(repo_dir, with_ignored=True):
        if not str(file.path).endswith(wanted):
            continue
        try:
            data = file.path.read_bytes()
        except OSError:
            return sites, f"{file.path} could not be read"
        if not word.search(data):
            continue
        if file.ignored:
            return sites, f"{file.path} mentions it and is ignored (not the repository's source)"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return sites, f"{file.path} is not UTF-8"
        root = _parse(text, ts_scope.grammar(str(file.path)))
        if root.has_error:
            return sites, f"{file.path} does not parse"
        start = decl_start if file.resolved == decl else None
        reader = str(file.path)

        def resolves_to_decl(spec: str, reader: str = reader) -> bool:
            target = cross_file_ts.resolve_specifier(reader, spec, repo_dir)
            return target is not None and str(Path(target).resolve()) == decl

        mentions = [n for n in _walk(root) if n.type in _NAMES and n.text == name.encode()]
        # Specifiers first: a value use in another file needs one of them to
        # have imported the class from its declaring file.
        mentions.sort(key=lambda n: n.parent is None or n.parent.type not in ("import_specifier", "export_specifier"))
        imported = False
        for node in mentions:
            reason = _classify(node, reader, start, index, sites, resolves_to_decl)
            if (
                reason is None
                and node.type == "identifier"
                and start is None
                and not imported
                and node.parent is not None
                and node.parent.type in ("new_expression", "extends_clause", "binary_expression")
            ):
                reason = "not imported from its declaring file"
            if reason is not None:
                return sites, f"{file.path}:{node.start_point[0] + 1}: {reason}"
            if node.parent is not None and node.parent.type == "import_specifier":
                imported = True
    # Single-file components hold script no parse here reaches.
    for file in source_index.files(repo_dir, source_index.COMPONENT_SUFFIXES, with_ignored=True):
        try:
            if word.search(file.path.read_bytes()):
                return sites, f"{file.path} mentions it (a .vue, .svelte or .astro component)"
        except OSError:
            return sites, f"{file.path} could not be read"
    return sites, None


def _exported(root: Node, cls: Node, name: str) -> bool:
    parent = cls.parent
    if parent is not None and parent.type == "export_statement":
        return True
    return any(
        n.type == "export_specifier"
        and (spec := n.child_by_field_name("name")) is not None
        and _text(spec) == name
        for n in _walk(root)
    )


def plan(path: str, source: str, label: str, repo_dir: str) -> Removal | Refusal:
    """Prove the parameter property ``label`` (``Class.member``) of the file at
    ``path``, whose current text is ``source``, can be removed with every
    caller's argument."""
    class_name, _, member = label.partition(".")
    if member.startswith("#"):
        return Refusal(label, "not a constructor parameter property", quiet=True)
    if IDENT.fullmatch(class_name) is None or IDENT.fullmatch(member) is None:
        return Refusal(label, "a name the deletion rules cannot spell")
    root = _parse(source, ts_scope.grammar(path))
    if root.has_error:
        return Refusal(label, "the declaring file does not parse")
    classes = _classes(root, class_name)
    if len(classes) != 1:
        return Refusal(label, "the class is declared more than once in its file")
    cls = classes[0]
    body = cls.child_by_field_name("body")
    ctors = _constructors(body) if body is not None else []
    found = [p for c in ctors if (p := _parameter(c, member)) is not None]
    if not found:
        return Refusal(label, "not a constructor parameter property", quiet=True)
    if len(ctors) != 1 or ctors[0].type != "method_definition":
        return Refusal(label, "an overloaded constructor")
    ctor = ctors[0]
    index, parameter = found[0]
    if (
        _decorated(cls)
        or any(c.type == "decorator" for c in ctor.children)
        or any(c.type == "decorator" for c in parameter.children)
    ):
        return Refusal(label, "a decorated class or parameter (dependency injection)")
    parent = cls.parent
    if (
        parent is not None
        and parent.type == "export_statement"
        and any(c.type == "default" for c in parent.children)
    ):
        return Refusal(label, "a default export, which callers can import under any name")
    pattern = parameter.child_by_field_name("pattern")
    for node in _walk(ctor):
        if (
            node.type in ("identifier", "shorthand_property_identifier")
            and _text(node) == member
            and node != pattern
        ):
            return Refusal(label, "the constructor still reads the parameter itself")
    for node in _walk(body):
        if (
            node.type == "new_expression"
            and (c := node.child_by_field_name("constructor")) is not None
            and c.type == "this"
        ):
            return Refusal(label, "the class constructs itself through new this(…)")
    sites, reason = _references(class_name, path, cls.start_byte, index, repo_dir)
    if reason is not None:
        return Refusal(label, reason)
    return Removal(path, class_name, member, label, index, tuple(sites), _exported(root, cls, class_name))
