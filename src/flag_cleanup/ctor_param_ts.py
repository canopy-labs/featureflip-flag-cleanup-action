"""Prove a TypeScript constructor parameter property can be removed.

``constructor(private client: Client)`` declares a property the fold may leave
unread. Removing it changes the constructor, so every way the class can be
constructed has to be accounted for first. TypeScript's types are structural,
so the proof is by NAME, as for keyless wrappers: every spelling of the class
name in the repository's TypeScript and JavaScript must be one of a short list
of shapes, and anything else refuses.

Allowed: ``new Svc(…)`` (no spread; the argument at the parameter's position
absent or inert), ``extends Svc`` from a class with its own constructor whose
one ``super(…)`` meets the same rule, or from a named class with none, which
inherits Svc's: every spelling of ITS name is proven the same way, recursively,
and its ``new Child(…)`` calls are Svc's callers. A type position, an import or export
specifier without an alias, and the right side of ``instanceof``. Strings and
comments are not references: they cannot construct anything, with one
exception. ``new m['Svc'](…)`` constructs the class from a string, so a file
holding the string ``'Svc'`` and a computed construction (``new`` of anything
but a name or a property access, or ``Reflect.construct``) refuses. A key
computed at run time is never spelled, and cannot be seen.

Refused on the declaration side: a decorator on the class, the constructor or
any of its parameters (dependency injection resolves those by position or
token), a default export (it can be imported under any name), an overloaded
constructor, a constructor that still reads the parameter by its bare name or
reads ``arguments``, and ``new this(…)`` inside the class.

:func:`constructor_callers` is the caller half on its own, for a plain
parameter whose last read was a field assignment :mod:`flag_cleanup.ts_field_removal`
deletes. :func:`plan_parameter` proves a plain parameter the run left unused
some other way, usually a subclass's ``super(c, l)`` losing its ``c``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_ts, source_index, ts_scope
from flag_cleanup.ctor_param_model import IDENT, Refusal, Removal, Site
from flag_cleanup.unused import _parse

_CLASSES = frozenset({"class_declaration", "abstract_class_declaration"})
_PARAMETERS = frozenset({"required_parameter", "optional_parameter"})
#: Any of these makes a constructor parameter a property (`readonly flags`).
_PROPERTY_MODIFIERS = frozenset({"accessibility_modifier", "readonly", "override_modifier"})
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
#: What ``new`` may construct without computing the class: ``new Svc``, ``new m.Svc``.
_PLAIN_CONSTRUCTORS = frozenset({"identifier", "member_expression", "this"})
_INERT_WRAPPERS = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"}
)
#: How many constructor-less subclasses deep a chain is followed. A class
#: cannot extend itself, so this bounds a chain rather than breaking a cycle.
_MAX_INHERITANCE = 8


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


#: A JavaScript escape in an identifier or a string: ``\u0063``, ``\u{63}``, ``\x63``, ``\c``.
_ESCAPE = re.compile(r"\\u\{([0-9a-fA-F]+)\}|\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|\\(.)", re.DOTALL)
#: Class members that ``static`` makes static, ``this`` then being the class.
_MEMBERS = frozenset({"method_definition", "public_field_definition", "class_static_block"})


def _unescaped(text: str) -> str:
    return _ESCAPE.sub(
        lambda m: chr(int(h, 16)) if (h := m.group(1) or m.group(2) or m.group(3)) else m.group(4),
        text,
    )


def _member_name(node: Node) -> str | None:
    """The name a method's name node spells, escapes decoded: ``constructor``,
    ``"constructor"`` and ``\\u0063onstructor`` all name the constructor. A
    computed name (``['constructor']``) is an ordinary method."""
    if node.type == "string":
        return _unescaped("".join(_text(c) for c in node.named_children))
    if node.type == "property_identifier":
        return _unescaped(_text(node))
    return None


def _static(member: Node) -> bool:
    return any(c.type == "static" for c in member.children)


def _constructors(body: Node) -> list[Node]:
    return [
        m
        for m in body.named_children
        if m.type in _SIGNATURES
        and not _static(m)
        and (n := m.child_by_field_name("name")) is not None
        and _member_name(n) == "constructor"
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
    """Whether ``class_name`` in ``source`` still declares a constructor
    parameter ``param_name``, a property or not."""
    return bool(parameter_spans(source, grammar, class_name, param_name))


def parameter_spans(
    source: str, grammar: str, class_name: str, param_name: str
) -> list[tuple[int, int]]:
    """The byte span of each constructor parameter ``param_name`` of
    ``class_name`` in ``source``, a property or not."""
    spans = []
    for cls in _classes(_parse(source, grammar), class_name):
        body = cls.child_by_field_name("body")
        for ctor in _constructors(body) if body is not None else []:
            parameters = ctor.child_by_field_name("parameters")
            for item in parameters.named_children if parameters else ():
                pattern = item.child_by_field_name("pattern")
                if item.type in _PARAMETERS and pattern is not None and _text(pattern) == param_name:
                    spans.append((item.start_byte, item.end_byte))
    return spans


def _root(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


def _class_refusal(cls: Node) -> str | None:
    """Why a class's constructor can be reached without its name being
    spelled at a call, if it can."""
    if _decorated(cls):
        return "a decorated class (dependency injection)"
    parent = cls.parent
    if (
        parent is not None
        and parent.type == "export_statement"
        and any(c.type == "default" for c in parent.children)
    ):
        return "a default export, which callers can import under any name"
    body = cls.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        for node in _walk(member):
            if (
                node.type == "new_expression"
                and (c := node.child_by_field_name("constructor")) is not None
                and c.type == "this"
            ):
                return "the class constructs itself through new this(…)"
            if (
                node.type == "member_expression"
                and (o := node.child_by_field_name("object")) is not None
                and o.type == "this"
                and (prop := node.child_by_field_name("property")) is not None
                and _member_name(prop) == "constructor"
            ):
                return "the class reaches its constructor through this.constructor"
            if node.type == "this" and member.type in _MEMBERS and _static(member):
                # `const C = this; new C(…)`, `this.create(…)` reaching an
                # inherited `new this(…)`, `Reflect.construct(this, …)`.
                return "a static member uses this, which is the class and can construct it"
    return None


def _subclass(
    extends: Node,
    path: str,
    index: int,
    sites: list[Site],
    inherit: Callable[[Node | None], str | None],
) -> str | None:
    heritage = extends.parent
    cls = heritage.parent if heritage is not None else None
    body = cls.child_by_field_name("body") if cls is not None else None
    declared = _constructors(body) if body is not None else []
    if not declared:
        return inherit(cls)
    ctors = [c for c in declared if c.type == "method_definition"]
    if len(ctors) != 1:
        return "a subclass constructor without a single body"
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
    inherit: Callable[[Node | None], str | None] = lambda cls: "a subclass with no constructor of its own",
) -> str | None:
    """``None`` when this spelling of the class name is accounted for (and
    records its argument, if it is a call), else why it is not.

    ``resolves_to_decl(specifier)`` says whether a module specifier in this
    file names the declaring file; ``inherit(cls)`` proves a subclass with no
    constructor of its own."""
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
        return _subclass(parent, path, index, sites, inherit)
    # A JSX tag is a `name` field too, and constructs the class: a value use.
    if named and not parent.type.startswith("jsx_"):
        return f"another declaration of the same name ({parent.type})"
    return f"used as a value ({parent.type})"


def _names_string(node: Node, name: str) -> bool:
    """Whether ``node`` is a string literal holding exactly ``name``."""
    if node.type not in ("string", "template_string"):
        return False
    parts = [c for c in node.named_children if c.type != "escape_sequence"]
    return len(parts) == 1 and parts[0].type == "string_fragment" and _text(parts[0]) == name


def _computed_construction(node: Node) -> bool:
    """``new m['Svc'](…)``, ``new (lookup())(…)``, ``Reflect.construct(…)``."""
    if node.type == "new_expression":
        constructor = node.child_by_field_name("constructor")
        return constructor is not None and constructor.type not in _PLAIN_CONSTRUCTORS
    function = node.child_by_field_name("function") if node.type == "call_expression" else None
    return function is not None and _text(function) == "Reflect.construct"


def _references(
    name: str,
    decl_path: str,
    decl_start: int,
    index: int,
    repo_dir: str,
    heirs: list[tuple[str, str]],
    depth: int = 0,
) -> tuple[list[Site], str | None]:
    """Every caller's argument at ``index``, or why one cannot be proven.
    ``heirs`` collects each exported subclass that inherits the constructor."""
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

        def inherit(cls: Node | None, reader: str = reader) -> str | None:
            # `new Child(…)` runs Svc's constructor with the same arguments.
            name_node = cls.child_by_field_name("name") if cls is not None and cls.type in _CLASSES else None
            if cls is None or name_node is None:
                return "a class expression with no constructor of its own"
            child = _text(name_node)
            if depth >= _MAX_INHERITANCE:
                return f"subclass {child}: subclasses without a constructor nested too deep"
            reason = _class_refusal(cls)
            if reason is None:
                found, reason = _references(
                    child, reader, cls.start_byte, index, repo_dir, heirs, depth + 1
                )
                sites.extend(found)
            if reason is not None:
                return f"subclass {child}, which inherits the constructor: {reason}"
            if _exported(_root(cls), cls, child):
                heirs.append((reader, child))
            return None

        nodes = list(_walk(root))
        named_at = next((n for n in nodes if _names_string(n, name)), None)
        if named_at is not None and any(_computed_construction(n) for n in nodes):
            return sites, (
                f"{file.path}:{named_at.start_point[0] + 1}: "
                "a string naming the class, in a file that constructs through a computed expression"
            )
        mentions = [n for n in nodes if n.type in _NAMES and n.text == name.encode()]
        # Specifiers first: a value use in another file needs one of them to
        # have imported the class from its declaring file.
        mentions.sort(key=lambda n: n.parent is None or n.parent.type not in ("import_specifier", "export_specifier"))
        imported = False
        for node in mentions:
            reason = _classify(node, reader, start, index, sites, resolves_to_decl, inherit)
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
    # Single-file components and HTML pages hold script no parse here reaches.
    for file in markup_files(repo_dir):
        try:
            if word.search(file.path.read_bytes()):
                return sites, f"{file.path} mentions it ({markup_kind(file.path)})"
        except OSError:
            return sites, f"{file.path} could not be read"
    return sites, None


_MARKUP_SUFFIXES = source_index.COMPONENT_SUFFIXES | source_index.PAGE_SUFFIXES


def markup_files(repo_dir: str) -> list[source_index.SourceFile]:
    """The checkout's component and HTML files, ignored ones included."""
    return source_index.files(repo_dir, _MARKUP_SUFFIXES, with_ignored=True)


def markup_kind(path: Path) -> str:
    """What a file of :func:`markup_files` is, for a refusal's reason."""
    if path.suffix in source_index.PAGE_SUFFIXES:
        return "an HTML page"
    return "a .vue, .svelte or .astro component or an .mdx document"


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
    pattern = parameter.child_by_field_name("pattern")
    for node in _walk(ctor):
        if (
            node.type in ("identifier", "shorthand_property_identifier")
            and _text(node) == member
            and node != pattern
        ):
            return Refusal(label, "the constructor still reads the parameter itself")
    callers = constructor_callers(path, root, cls, ctor, index, label, repo_dir)
    if isinstance(callers, Refusal):
        return callers
    sites, exported, heirs = callers
    return Removal(path, class_name, member, label, index, sites, exported, heirs=heirs)


def plan_parameter(path: str, source: str, label: str, repo_dir: str) -> Removal | Refusal:
    """Prove the plain constructor parameter ``label`` (``Class(param)``) of
    the file at ``path``, whose current text is ``source``, can be removed
    with every caller's argument."""
    class_name, _, rest = label.partition("(")
    member = rest.removesuffix(")")
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
    if len(ctors) != 1 or ctors[0].type != "method_definition":
        return Refusal(label, "an overloaded constructor")
    ctor = ctors[0]
    parameters = ctor.child_by_field_name("parameters")
    items = [c for c in parameters.named_children if c.type != "comment"] if parameters else []
    found = [
        (index, item)
        for index, item in enumerate(items)
        if item.type in _PARAMETERS
        and (pattern := item.child_by_field_name("pattern")) is not None
        and pattern.type == "identifier"
        and _text(pattern) == member
    ]
    if len(found) != 1 or any(c.type in _PROPERTY_MODIFIERS for c in found[0][1].children):
        return Refusal(label, "not a plain constructor parameter")
    index, parameter = found[0]
    if parameter.child_by_field_name("value") is not None:
        # A caller that omits the argument runs the default; removing it would not.
        return Refusal(label, "the parameter has a default value")
    pattern = parameter.child_by_field_name("pattern")
    for node in _walk(ctor):
        if node.type == "identifier" and _text(node) == "eval":
            return Refusal(label, "the constructor calls eval, which can read the parameter by name")
        if (
            node.type in ("identifier", "shorthand_property_identifier")
            and _text(node) == member
            and node != pattern
        ):
            return Refusal(label, "the constructor still reads the parameter")
    callers = constructor_callers(path, root, cls, ctor, index, label, repo_dir)
    if isinstance(callers, Refusal):
        return callers
    sites, exported, heirs = callers
    return Removal(path, class_name, member, label, index, sites, exported, heirs=heirs)


def constructor_callers(
    path: str, root: Node, cls: Node, ctor: Node, index: int, label: str, repo_dir: str
) -> tuple[tuple[Site, ...], bool, tuple[tuple[str, str], ...]] | Refusal:
    """The argument every caller passes at ``index`` to ``cls``'s one
    constructor ``ctor``, whether the class is exported, and ``(path,
    class)`` for each exported subclass inheriting the constructor; or why
    the callers cannot all be proven. ``root`` is the declaring file's tree."""
    class_name = _text(cls.child_by_field_name("name"))
    parameters = ctor.child_by_field_name("parameters")
    items = [c for c in parameters.named_children if c.type != "comment"] if parameters else []
    if (
        _decorated(cls)
        or any(c.type == "decorator" for c in ctor.children)
        or any(c.type == "decorator" for c in items[index].children)
    ):
        return Refusal(label, "a decorated class or parameter (dependency injection)")
    if any(n.type == "identifier" and n.text == b"arguments" for n in _walk(ctor)):
        return Refusal(label, "the constructor reads `arguments`, which counts its parameters")
    reason = _class_refusal(cls)
    if reason is not None:
        return Refusal(label, reason)
    heirs: list[tuple[str, str]] = []
    sites, reason = _references(class_name, path, cls.start_byte, index, repo_dir, heirs)
    if reason is not None:
        return Refusal(label, reason)
    return tuple(sites), _exported(root, cls, class_name), tuple(dict.fromkeys(heirs))
