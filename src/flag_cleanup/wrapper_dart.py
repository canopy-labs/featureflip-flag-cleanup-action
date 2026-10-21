"""Dart profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

A wrapper is a top-level function or getter, or a method or getter of a
``class``, static or not, declared to return ``bool`` and whose whole body is
``=> <read>;`` or ``{ return <read>; }`` with no ``async``/``sync*``
modifier. The read is one ``rules/dart.toml`` folds in this run:
``boolVariation`` (or a named accessor) with the key first, as a literal, a
same-file constant where Gate 2 let the const rules run, or a text the
cross-file resolution proved names the key.

Everything here reads the gate's grammar, PyPI's ``tree-sitter-dart`` 0.1.0,
in which a call is not a node: ``flags.newCheckout(u)`` is an ``identifier``
followed by a ``selector`` holding ``.newCheckout`` and a ``selector``
holding the argument list, all siblings. So a call is judged as that run of
siblings, and the span handed to the sentinel pre-pass runs from the head to
the end of the member's selector. The engine parses the marked text with its
own grammar, where ``__ff_cleanup_<hash>(u)`` is an ordinary
``call_expression`` the seed rules match.

What makes a call provable in Dart is its LEXICAL LOOKUP: an unqualified name
resolves to the nearest enclosing declaration -- a local, a parameter, a member
the enclosing class itself declares -- and then to the library's own
top-level names and its imports. Inherited members are consulted only when that
finds nothing, so an imported top-level name beats an inherited member of the
same name. So:

* the wrapper's name must be declared exactly once across the repository's
  Dart files, counting every binding form (locals, parameters, fields,
  pattern variables, catch and loop variables), which leaves nothing in the
  repository that could shadow it;
* a top-level wrapper is called bare in its own file or through an import of
  a library in the checkout that exports it, or through a prefix of one;
* a static member is called through its class's name, resolved the same way,
  or bare inside its class;
* an instance member is called bare or through ``this`` inside its class, or
  through a name every declaration of which in the file is written with the
  class as its type (or initialised by calling its unnamed constructor), one
  of whose scopes encloses the call.

A class that anything in the repository extends, implements or mixes in,
an abstract, sealed, interface or mixin class, and a member marked
``@override`` are refused; a mock or fake implementing the class refuses its
wrappers as ``test-double``. A library with ``part`` files, and a part, is
left alone. Members of an extension, mixin, enum or extension type are not
wrappers here: their reads are folded where they are, as before. A public
wrapper of a package that may be published is never deleted (``published``),
since code outside the repository may call it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, dart_namespace, key_const, mock_stubs, syntax
from flag_cleanup import wrapper_inline as wi

logger = logging.getLogger(__name__)

EXTENSIONS = (".dart",)
#: The grammar exposes no content node, so the whole literal is a string;
#: an interpolation inside it is part of its text.
_STRINGS = frozenset({"string_literal"})
#: ``'$name'`` puts the name in an ``identifier_dollar_escaped`` leaf.
MENTION_TYPES = frozenset({"identifier", "identifier_dollar_escaped"})
_READ_CALLEES = frozenset({"boolVariation"})
#: Annotations that say nothing about who calls a member. ``@pragma`` can
#: say native code does (``vm:entry-point``), so it is not here.
_INERT = frozenset({"deprecated", "Deprecated"})
#: Class modifiers that invite another implementation.
_OPEN_CLASS = frozenset({"abstract", "sealed", "interface", "mixin"})
#: Supertypes of a test double.
_DOUBLES = frozenset({"Mock", "Fake", "SmartFake"})
#: Test runner calls whose first argument is a description, not code.
_TEST_CALLS = frozenset({"test", "testWidgets", "group", "setUp", "tearDown"})
_SIGNATURES = ("function_signature", "getter_signature")
#: Parents whose ``name`` field (or first identifier) is a declaration.
_NAMED_DECLARATIONS = frozenset({
    "function_signature", "getter_signature", "setter_signature", "class_definition",
    "enum_declaration", "extension_declaration", "extension_type_declaration",
    "enum_constant", "initialized_variable_definition", "for_loop_parts",
})
_CONSTRUCTORS = frozenset({
    "constructor_signature", "factory_constructor_signature",
    "redirecting_factory_constructor_signature", "constant_constructor_signature",
})


def parse(source: str) -> Node:
    return syntax._parse(source, "dart")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _read(path: str) -> str | None:
    """The file's text with its line endings as they are, so a span taken
    from it is an offset into the file the sentinel pre-pass marks."""
    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _expression(container: Node, skip: tuple[str, ...]) -> list[Node]:
    """The children of ``container`` that make up its expression, less the
    tokens ``skip`` names and any comment."""
    return [c for c in container.children if c.type not in skip and "comment" not in c.type]


def _member(selector: Node) -> Node | None:
    """The name of ``.name`` in a selector; ``None`` for any other selector."""
    if selector.type != "selector" or selector.named_child_count != 1:
        return None
    inner = selector.named_children[0]
    if inner.type != "unconditional_assignable_selector":
        return None
    names = [c for c in inner.named_children if c.type == "identifier"]
    return names[0] if len(names) == 1 else None


def _arguments(selector: Node) -> Node | None:
    """The ``arguments`` of a selector that is a plain call's argument list
    (no type arguments); ``None`` for anything else."""
    if selector.type != "selector" or selector.named_child_count != 1:
        return None
    part = selector.named_children[0]
    if part.type != "argument_part" or part.named_child_count != 1:
        return None
    arguments = part.named_children[0]
    return arguments if arguments.type == "arguments" else None


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
    """Whether a body's expression (an arrow ``function_body`` or a
    ``return_statement``) is exactly one read of ``flag_key`` this run folds.

    ``loose`` accepts any identifier as the key, a superset for widening the
    undo snapshot."""
    root = parse(source)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "dart", flag_key)}
        if const_ok
        else set()
    )
    texts = frozenset(external)
    names = _READ_CALLEES | set(accessors)

    def key_matches(argument: Node) -> bool:
        value = [c for c in argument.named_children if "comment" not in c.type]
        if len(value) == 1 and value[0].type == "string_literal":
            quoted = key_const._DART_QUOTED.match(_text(value[0]))
            return quoted is not None and quoted.group(2) == flag_key
        written = " ".join(_text(argument).split())
        if len(value) == 1 and value[0].type == "identifier":
            return loose or written in consts or written in texts
        return written in texts

    def is_read(container: Node) -> bool:
        parts = _expression(container, ("=>", "return", ";"))
        if len(parts) < 2 or parts[0].type not in ("identifier", "this"):
            return False
        arguments = _arguments(parts[-1])
        if arguments is None:
            return False
        if len(parts) == 2:
            callee = parts[0] if parts[0].type == "identifier" else None
        else:
            if any(_member(s) is None for s in parts[1:-1]):
                return False
            callee = _member(parts[-2])
        if callee is None or _text(callee) not in names:
            return False
        # The seed rules match the key first, or after ONE leading comment.
        listed = list(arguments.named_children)
        if listed and "comment" in listed[0].type:
            listed = listed[1:]
        return bool(listed) and listed[0].type == "argument" and key_matches(listed[0])

    return is_read


def _single_read(body: Node | None, is_read: Callable[[Node], bool]) -> bool:
    """Whether ``body`` is ``=> read;`` or ``{ return read; }``."""
    if body is None or body.type != "function_body" or not body.children:
        return False
    first = body.children[0]
    if first.type == "=>":
        return is_read(body)
    if first.type != "block":
        return False  # `async`, `sync*`, `async*`
    statements = [c for c in first.named_children if "comment" not in c.type]
    return len(statements) == 1 and statements[0].type == "return_statement" and is_read(statements[0])


def _returns_bool(signature: Node) -> bool:
    """Declared ``bool``: not ``bool?``, not inferred, no type parameters."""
    name = signature.child_by_field_name("name")
    if name is None:
        return False
    before = [c for c in signature.named_children if c.start_byte < name.start_byte]
    return (
        len(before) == 1
        and before[0].type == "type_identifier"
        and _text(before[0]) == "bool"
        and not any(c.type == "type_parameters" for c in signature.named_children)
    )


def _annotations(node: Node) -> tuple[str, ...]:
    """Names of the annotations directly above ``node`` (comments between
    are skipped)."""
    found: list[str] = []
    previous = node.prev_named_sibling
    while previous is not None and (previous.type in ("annotation", "marker_annotation") or "comment" in previous.type):
        if "comment" not in previous.type:
            name = previous.child_by_field_name("name")
            if name is None:
                name = next((c for c in previous.named_children if c.type == "identifier"), None)
            found.append(_text(name) if name is not None else "?")
        previous = previous.prev_named_sibling
    return tuple(reversed(found))


def _pairs(scope: Node) -> Iterable[tuple[Node, Node, Node]]:
    """``(declaration, signature, body)`` for each member of ``scope`` that
    has a body: a top-level signature or a class body's ``method_signature``
    and the ``function_body`` beside it."""
    children = scope.named_children
    for index, child in enumerate(children[:-1]):
        body = children[index + 1]
        if body.type != "function_body":
            continue
        if child.type in _SIGNATURES:
            yield child, child, body
        elif child.type == "method_signature":
            inner = [c for c in child.named_children if c.type != "static"]
            if len(inner) == 1 and inner[0].type in _SIGNATURES:
                yield child, inner[0], body


def _has_parts(root: Node) -> bool:
    return any(c.type in ("part_directive", "part_of_directive") for c in root.named_children)


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    found: list[wi.Wrapper] = []

    def add(declaration: Node, signature: Node, body: Node, owner: str, top: bool) -> None:
        if not _returns_bool(signature) or not _single_read(body, is_read):
            return
        name = _text(signature.child_by_field_name("name"))
        getter = signature.type == "getter_signature"
        found.append(
            wi.Wrapper(
                path=path,
                language="dart",
                name=name,
                owner=owner,
                package="",
                line=wi.line_of(declaration),
                start=declaration.start_byte,
                end=body.end_byte,
                static=not top and any(c.type == "static" for c in declaration.children),
                annotations=_annotations(declaration),
                kind="property" if getter else ("function" if top else "method"),
                private=name.startswith("_"),
            )
        )

    stem = Path(path).name
    for declaration, signature, body in _pairs(root):
        add(declaration, signature, body, stem, True)
    for definition in root.named_children:
        if definition.type != "class_definition":
            continue
        name = definition.child_by_field_name("name")
        class_body = definition.child_by_field_name("body")
        if name is None or class_body is None:
            continue
        for declaration, signature, body in _pairs(class_body):
            add(declaration, signature, body, _text(name), False)
    return found


def top_level(wrapper: wi.Wrapper) -> bool:
    """A top-level function or getter: its owner is its file's name."""
    return wrapper.owner.endswith(EXTENSIONS)


# --- Bindings -----------------------------------------------------------------


def _in_pattern(node: Node) -> bool:
    """Whether ``node`` sits in a pattern, where an identifier may bind (the
    gate grammar reads ``var (a, b)`` as two constant patterns)."""
    parent = node.parent
    while parent is not None and parent.type not in _STATEMENT_LEVEL:
        if parent.type.endswith("_pattern") or parent.type in ("pattern_variable_declaration", "pattern_assignment"):
            return True
        parent = parent.parent
    return False


#: Where the search for an enclosing pattern stops.
_STATEMENT_LEVEL = frozenset({
    "block", "function_body", "program", "class_body", "switch_block", "arguments", "selector",
})


def binds(node: Node) -> bool:
    """Whether the identifier ``node`` is declared here, in any binding form:
    a function, getter, setter, class or other type, a constructor, a
    variable or field, a parameter, a loop, catch or pattern variable, an
    enum value or an import prefix. Erring towards yes only refuses more."""
    parent = node.parent
    if parent is None or node.type != "identifier":
        return False
    kind = parent.type
    if kind in _NAMED_DECLARATIONS:
        name = parent.child_by_field_name("name")
        if name is not None:
            return name == node
        return kind == "enum_constant"
    if kind in ("initialized_identifier", "static_final_declaration"):
        return parent.named_children[0] == node
    if kind == "formal_parameter":
        return True
    if kind in (
        "catch_parameters", "mixin_declaration", "type_alias", "import_specification",
        "representation_declaration", "identifier_list", "super_formal_parameter", "typed_identifier",
    ):
        return True
    if kind in _CONSTRUCTORS or kind == "constructor_param":
        # A constructor's own name; `this.x` refers to the field.
        return kind != "constructor_param"
    return _in_pattern(node)


def _label(node: Node) -> bool:
    """``name:`` of a named argument, which names a parameter."""
    parent = node.parent
    return parent is not None and parent.type == "label" and parent.parent is not None and parent.parent.type == "named_argument"


def names_only(node: Node, kind: str = "method") -> bool:
    """Whether an identifier only names something: a declaration's own name
    (uniqueness leaves the wrapper's as the only one) or a named argument's
    label. Not a combinator's ``show`` entry: one left behind would be a
    leftover the close-out must see."""
    return binds(node) or _label(node)


def members(root: Node, name: str) -> list[Node]:
    """Every top-level function or getter, and every class member, of
    ``name``: its signature, whose body is the sibling after it."""
    found = [d for d, s, _ in _pairs(root) if _text(s.child_by_field_name("name")) == name]
    for definition in root.named_children:
        body = definition.child_by_field_name("body") if definition.type == "class_definition" else None
        if body is not None:
            found += [d for d, s, _ in _pairs(body) if _text(s.child_by_field_name("name")) == name]
    return found


def member_end(member: Node) -> int:
    """A member ends with its body, the sibling after its signature."""
    body = member.next_named_sibling
    return body.end_byte if body is not None and body.type == "function_body" else member.end_byte


# --- The repository index -----------------------------------------------------


@dataclass
class Index:
    repo_dir: str = ""
    root: Path = field(default_factory=Path)
    #: ``(path, start byte)`` of every declaration of the wrapper's name.
    declarations: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, start byte)`` of every declaration of the owner's name.
    owners: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, line, text, double)`` for each class or mixin that extends,
    #: implements or mixes in the owner, or a mixin constrained to it.
    subtypes: list[tuple[str, int, str, bool]] = field(default_factory=list)
    #: The owner is abstract, sealed, an interface or a mixin class.
    open_owner: bool = False
    #: The owner names a supertype (``extends``, ``implements``, ``with``):
    #: its members may override one without saying so, ``@override`` being
    #: optional.
    owner_supertypes: bool = False
    #: The owner is ``final`` or ``base``, so nothing outside its library can
    #: implement it.
    owner_closed: bool = False
    #: The wrapper's library has parts, or is one.
    parts: bool = False
    #: A file mentioning a name could not be read, or holds it inside a
    #: region it could not parse: nothing about uniqueness can be trusted.
    unreadable: bool = False
    #: Every Dart file mentioning the name, resolved: where call sites are.
    parsed: list[str] = field(default_factory=list)
    #: Dart has no guest language this tool rewrites around it.
    foreign: list[str] = field(default_factory=list)
    #: Source files of any other kind that spell the name.
    unparsed: list[str] = field(default_factory=list)
    #: Unused: Dart has no second spelling of a name.
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)
    #: path -> (source, root), shared by every proof of this wrapper.
    files: dict[str, tuple[str, Node]] = field(default_factory=dict)

    def file(self, path: str, source: str | None = None) -> tuple[str, Node] | None:
        cached = self.files.get(path)
        if cached is not None and (source is None or cached[0] == source):
            return cached
        text = _read(path) if source is None else source
        if text is None:
            return None
        self.files[path] = (text, parse(text))
        return self.files[path]

    @classmethod
    def build(cls, repo_dir: str, wrapper: wi.Wrapper) -> Index:
        from flag_cleanup.piranha_runner import checkout_root

        index = cls(repo_dir=repo_dir, root=Path(checkout_root(repo_dir)).resolve())
        word = _word(wrapper.name)
        for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=()):
            text = _read(path)
            if text is None:
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            if path.endswith(EXTENSIONS):
                index.parsed.append(path)
                index._declarations(path, text, wrapper.name, index.declarations)
            elif word.search(text):
                index.unparsed.append(path)
        home = index.file(wrapper.path)
        if home is None:
            index.unreadable = True
            return index
        index.parts = _has_parts(home[1])
        if top_level(wrapper):
            return index
        for definition in home[1].named_children:
            name = definition.child_by_field_name("name") if definition.type == "class_definition" else None
            if name is not None and _text(name) == wrapper.owner:
                index.open_owner = any(c.type in _OPEN_CLASS for c in definition.children)
                index.owner_supertypes = any(
                    c.type in ("superclass", "interfaces", "mixins") for c in definition.named_children
                )
                keyword = next((c for c in definition.children if c.type == "class"), None)
                index.owner_closed = keyword is not None and any(
                    _text(c) in ("final", "base") for c in definition.children if c.end_byte <= keyword.start_byte
                )
        for path in cross_file_keys.mention_files(repo_dir, [wrapper.owner], exclude=()):
            if not path.endswith(EXTENSIONS):
                continue
            text = _read(path)
            if text is None:
                index.unreadable = True
                continue
            index._declarations(path, text, wrapper.owner, index.owners, constructors=False)
            index._subtypes(path, wrapper.owner)
        return index

    def _declarations(
        self, path: str, text: str, name: str, into: list[tuple[str, int]], constructors: bool = True
    ) -> None:
        """Every declaration of ``name`` in the file, into ``into``. A class's
        constructors spell its name and are not counted for the class."""
        cached = self.file(path, text)
        root = cached[1]
        word = _word(name)
        if root.has_error and any(n.type == "ERROR" and word.search(_text(n)) for n in cross_file_keys.walk(root)):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        for node in cross_file_keys.walk(root):
            if node.type == "identifier" and _text(node) == name and binds(node):
                if constructors or node.parent.type not in _CONSTRUCTORS:
                    into.append((path, node.start_byte))

    def _subtypes(self, path: str, owner: str) -> None:
        root = self.file(path)[1]
        for node in cross_file_keys.walk(root):
            if node.type != "type_identifier" or _text(node) != owner:
                continue
            clause = node.parent
            while clause is not None and clause.type not in ("superclass", "interfaces", "mixins", "mixin_declaration"):
                if clause.type in ("class_body", "program", "block", "function_body"):
                    clause = None
                    break
                clause = clause.parent
            if clause is None:
                continue
            definition = clause if clause.type == "mixin_declaration" else clause.parent
            while definition is not None and definition.type not in ("class_definition", "mixin_declaration"):
                definition = definition.parent
            header = [
                _text(t)
                for c in (definition.named_children if definition is not None else [])
                if c.type in ("superclass", "interfaces", "mixins")
                for t in cross_file_keys.walk(c)
                if t.type == "type_identifier"
            ]
            double = bool(_DOUBLES & set(header)) or path.endswith(".mocks.dart")
            line_node = definition or node
            self.subtypes.append((path, wi.line_of(line_node), wi.text_of(line_node).split("{")[0].strip(), double))


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | wi.Refused | None]:
    """The wrapper, or why it does not qualify.

    A library with parts shares its scope with files this proof does not
    read. ``@override`` names a supertype's member. A class anything in the
    repository extends, implements or mixes in may answer a call with
    another member, and a test double implementing it stubs the wrapper.
    Then the name, and a member's class name, must each be declared once."""
    if index.unreadable:
        return None, wi.NAME_NOT_UNIQUE
    if index.parts:
        return None, wi.IMPORT_UNRESOLVED
    if "override" in wrapper.annotations:
        return None, wi.OVERRIDABLE
    if not top_level(wrapper):
        if index.subtypes:
            path, line, text, double = index.subtypes[0]
            if all(s[3] for s in index.subtypes):
                return None, wi.Refused(wi.TEST_DOUBLE, path, line, text)
            path, line, text, _ = next(s for s in index.subtypes if not s[3])
            return None, wi.Refused(wi.OVERRIDABLE, path, line, text)
        if index.open_owner or index.owner_supertypes:
            return None, wi.OVERRIDABLE
        if len(index.owners) != 1:
            return None, wi.NAME_NOT_UNIQUE
        if not wrapper.static and not index.owner_closed and keep(wrapper) == wi.PUBLISHED:
            # Every class is an interface: a package depending on this one
            # may implement it and hand its own member in.
            return None, wi.INTERFACE_PUBLISHED
    if len(index.declarations) != 1:
        return None, wi.NAME_NOT_UNIQUE
    return wrapper, None


_PUBLISH_NONE = re.compile(r"^publish_to:\s*['\"]?none['\"]?\s*$", re.MULTILINE)


def keep(wrapper: wi.Wrapper) -> str | None:
    """Why a wrapper whose callers all folded is still not deleted.

    A public wrapper in a package's ``lib/`` may be called by another package
    that depends on this one, unless the package says it is never published
    (``publish_to: none``, the default for an app)."""
    if wrapper.name.startswith("_") or (not top_level(wrapper) and wrapper.owner.startswith("_")):
        return None
    path = Path(wrapper.path)
    home = None
    for directory in path.parents:
        if (directory / "pubspec.yaml").is_file():
            home = directory
            break
    if home is None:
        return wi.PUBLISHED
    if not path.is_relative_to(home / "lib"):
        return None
    try:
        pubspec = (home / "pubspec.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return wi.PUBLISHED
    return None if _PUBLISH_NONE.search(pubspec) else wi.PUBLISHED


# --- Call sites ---------------------------------------------------------------


def _enclosing_class(node: Node) -> Node | None:
    """The nearest enclosing class-like declaration, or ``None`` at the top
    level."""
    parent = node.parent
    while parent is not None:
        if parent.type in (
            "class_definition", "mixin_declaration", "extension_declaration",
            "extension_type_declaration", "enum_declaration",
        ):
            return parent
        parent = parent.parent
    return None


def _is_owner(definition: Node | None, wrapper: wi.Wrapper, path: str) -> bool:
    if definition is None or definition.type != "class_definition" or path != wrapper.path:
        return False
    name = definition.child_by_field_name("name")
    return name is not None and _text(name) == wrapper.owner


def _imports(root: Node) -> list[Node]:
    return [n for n in cross_file_keys.walk(root) if n.type == "import_specification"]


def _prefix(spec: Node) -> str | None:
    children = spec.children
    for at, child in enumerate(children[:-1]):
        if child.type == "as" and children[at + 1].type == "identifier":
            return _text(children[at + 1])
    return None


def _combinators_allow(spec: Node, name: str) -> bool:
    for combinator in (c for c in spec.children if c.type == "combinator"):
        listed = {_text(c) for c in combinator.named_children if c.type == "identifier"}
        keyword = combinator.children[0].type if combinator.children else ""
        if (keyword == "show" and name not in listed) or (keyword == "hide" and name in listed):
            return False
    return True


def _provides(spec: Node, path: str, name: str, home: str, root: Path) -> bool:
    """Whether the import ``spec`` in ``path`` brings ``name`` declared in
    ``home``: its URI resolves in the checkout to ``home``, or to a library
    exporting it, and no combinator drops it. The name is declared once in
    the repository, so any library in the checkout exporting it exports
    ``home``'s."""
    if any(c.type == "deferred" for c in spec.children):
        return False
    uri = dart_namespace.uri_of(spec)
    target = None if uri is None else dart_namespace.locate(uri, Path(path).resolve(), root)
    if target is None or not _combinators_allow(spec, name):
        return False
    if target == Path(home).resolve():
        return not name.startswith("_")
    namespace = dart_namespace.library_names(target, root)
    return namespace is not None and name in namespace.names


def _reaches(name: str, path: str, root: Node, home: str, index: Index) -> bool:
    """Whether an unqualified ``name`` in ``path`` reaches the top-level
    declaration of it in ``home``: the file is ``home``, or an unprefixed
    import provides it. Uniqueness has ruled out anything closer."""
    if path == home:
        return True
    return any(_prefix(s) is None and _provides(s, path, name, home, index.root) for s in _imports(root))


def _prefix_reaches(prefix: str, name: str, path: str, root: Node, home: str, index: Index) -> bool:
    """Whether ``prefix.name`` reaches the declaration in ``home``: every
    binding of ``prefix`` in the file is an import prefix, and each such
    import provides it. A local or a member of that name would hide the
    prefix."""
    specs = [s for s in _imports(root) if _prefix(s) == prefix]
    others = [
        n for n in cross_file_keys.walk(root)
        if n.type == "identifier" and _text(n) == prefix and binds(n) and n.parent.type != "import_specification"
    ]
    return bool(specs) and not others and all(_provides(s, path, name, home, index.root) for s in specs)


def _owner_reaches(path: str, root: Node, wrapper: wi.Wrapper, index: Index) -> bool:
    """Whether the class name, unqualified, means the wrapper's class here:
    nothing in the file binds it but, in its own file, the class itself; and
    it reaches the declaration."""
    binders = [
        n
        for n in cross_file_keys.walk(root)
        if _text(n) == wrapper.owner
        and (
            (n.type == "identifier" and binds(n) and n.parent.type not in _CONSTRUCTORS)
            # A type parameter of that name hides the class in its scope.
            or (n.type == "type_identifier" and n.parent is not None and n.parent.type == "type_parameter")
        )
    ]
    if path == wrapper.path:
        return len(binders) == 1 and binders[0].parent.type == "class_definition"
    return not binders and _reaches(wrapper.owner, path, root, wrapper.path, index)


def _scope(binder: Node) -> Node | None:
    """Where the declaration whose name is ``binder`` is visible, or ``None``
    for a form not read here."""
    parent = binder.parent
    if parent is None:
        return None
    if parent.type == "formal_parameter":
        listing = parent.parent
        while listing is not None and listing.type == "optional_formal_parameters":
            listing = listing.parent
        if listing is None or listing.type != "formal_parameter_list":
            return None
        owner = listing.parent
        if owner is None:
            return None
        if owner.type == "function_expression":
            return owner
        if owner.type != "function_signature":
            return None
        holder = owner.parent
        if holder is not None and holder.type == "method_signature":
            body = holder.next_named_sibling
        elif holder is not None and holder.type == "lambda_expression":
            body = owner.next_named_sibling
        else:
            body = owner.next_named_sibling
        return body if body is not None and body.type == "function_body" else None
    if parent.type == "initialized_variable_definition":
        declaration = parent.parent
        holder = declaration.parent if declaration is not None else None
        if declaration is None or declaration.type != "local_variable_declaration" or holder is None:
            return None
        return holder.parent if holder.type == "for_loop_parts" else holder
    if parent.type in ("initialized_identifier", "static_final_declaration"):
        listing = parent.parent
        holder = listing.parent if listing is not None else None
        if holder is not None and holder.type == "declaration":
            return holder.parent  # a field: the class body
        if holder is not None and holder.type == "program":
            return holder
    return None


def _declared_type(binder: Node, data: bytes) -> tuple[str, list[Node]] | None:
    """``(written type, value nodes)`` of the declaration named ``binder``,
    with ``""`` when no type is written; ``None`` for a ``late`` one or a
    form not read here. The written type is the source text between the
    modifiers and the name."""
    parent = binder.parent
    if parent.type == "formal_parameter":
        text = " ".join(data[parent.start_byte : binder.start_byte].decode("utf-8", "replace").split())
        return text.removeprefix("final ").strip(), []
    if parent.type == "initialized_variable_definition":
        if any(c.type == "late" for c in parent.children):
            return None
        start = parent.start_byte
        modifiers = [c for c in parent.named_children if c.type in ("final_builtin", "const_builtin", "inferred_type")]
        if modifiers:
            start = modifiers[-1].end_byte
        written = " ".join(data[start : binder.start_byte].decode("utf-8", "replace").split())
        values = parent.children_by_field_name("value")
        return written, values
    if parent.type in ("initialized_identifier", "static_final_declaration"):
        listing = parent.parent
        first = listing
        previous = listing.prev_sibling
        while previous is not None and previous.type in (
            "final_builtin", "const_builtin", "type_identifier", "nullable_type", "type_arguments",
            "inferred_type", "late", "static", "covariant", "external",
        ):
            first = previous
            previous = previous.prev_sibling
        head = [c for c in (listing.parent.children if listing.parent is not None else []) if first.start_byte <= c.start_byte < listing.start_byte]
        if any(c.type in ("late", "external", "covariant") for c in head):
            return None
        typed = [c for c in head if c.type not in ("final_builtin", "const_builtin", "inferred_type", "static")]
        written = " ".join(
            data[typed[0].start_byte : listing.start_byte].decode("utf-8", "replace").split()
        ) if typed else ""
        values = [c for c in parent.named_children if c != binder]
        return written, values
    return None


def _constructs(values: list[Node], owner: str) -> bool:
    """``Owner(...)``, ``const Owner(...)`` or ``new Owner(...)``: a call of
    the class's unnamed constructor."""
    if len(values) == 2 and values[0].type == "identifier" and _text(values[0]) == owner:
        return _arguments(values[1]) is not None
    if len(values) == 1 and values[0].type in ("const_object_expression", "new_expression"):
        types = [c for c in values[0].named_children if c.type == "type_identifier"]
        rest = [c for c in values[0].named_children if c.type not in ("const_builtin", "new", "type_identifier", "arguments")]
        return len(types) == 1 and _text(types[0]) == owner and not rest
    return False


def _receiver_typed(receiver: Node, path: str, root: Node, data: bytes, wrapper: wi.Wrapper, index: Index) -> bool:
    """Whether ``receiver`` names a variable of the wrapper's class.

    Every declaration of the name in the file must be written with the class
    as its type, or declared without one and initialised by calling the
    class's unnamed constructor; and one of them must enclose the call. The
    nearest enclosing declaration is the one a call reads, and every one of
    them is of the class, so which it is does not matter. With none
    enclosing, the name is an import's or an inherited member's: refused."""
    name = _text(receiver)
    binders = [n for n in cross_file_keys.walk(root) if n.type == "identifier" and _text(n) == name and binds(n)]
    if not binders:
        return False
    enclosing = False
    for binder in binders:
        declared = _declared_type(binder, data)
        scope = _scope(binder)
        if declared is None or scope is None:
            return False
        written, values = declared
        if written:
            if written != wrapper.owner:
                return False
        elif not _constructs(values, wrapper.owner):
            return False
        if scope.start_byte <= receiver.start_byte and receiver.end_byte <= scope.end_byte:
            enclosing = True
    return enclosing and _owner_reaches(path, root, wrapper, index)


def _argument_does_work(arguments: Node | None) -> bool:
    """A call, an assignment, an increment, an ``await`` or a ``throw``
    anywhere in the arguments: work that inlining the call would drop."""
    for node in [] if arguments is None else cross_file_keys.walk(arguments):
        if node.type in (
            "argument_part", "assignment_expression", "postfix_expression", "await_expression",
            "throw_expression", "new_expression", "cascade_section",
        ) or node.type in ("increment_operator",):
            return True
        if node.type == "unary_expression" and any(c.type in ("++", "--") for c in node.children):
            return True
    return False


def _inside(node: Node, kinds: tuple[str, ...]) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in kinds:
            return True
        parent = parent.parent
    return False


#: Where a bare name heads an expression the seed rules can fold: a
#: condition, an operand, an argument, a value, a returned or arrow body, an
#: element. Anything else (a record field, a symbol, a label, an annotation,
#: a named constructor, a parameter of a function type) is not read as an
#: expression, and a mark there would not fold.
_EXPRESSION_PARENTS = frozenset({
    "if_statement", "while_statement", "do_statement", "parenthesized_expression",
    "logical_and_expression", "logical_or_expression", "unary_expression", "conditional_expression",
    "equality_expression", "argument", "return_statement", "expression_statement",
    "function_body", "function_expression_body", "list_literal", "set_or_map_literal", "if_element",
    "assert_statement", "assertion", "if_null_expression", "spread_element",
})
#: Containers whose ``value`` (not ``name``) is an expression.
_VALUE_PARENTS = frozenset({
    "initialized_variable_definition", "static_final_declaration", "initialized_identifier",
    "named_argument", "assignment_expression",
})


def _expression_head(node: Node) -> bool:
    """Whether ``node`` starts an expression, judged by an allow-list of
    parents (a mark anywhere else would not fold, and would refuse the flag)."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _EXPRESSION_PARENTS:
        return True
    if parent.type in _VALUE_PARENTS:
        if parent.type == "named_argument":
            return not _label(node)
        if parent.type == "assignment_expression":
            return parent.child_by_field_name("right") == node
        if parent.type == "initialized_variable_definition":
            return node in parent.children_by_field_name("value")
        return parent.named_children[0] != node
    return False


def _occurrence(path: str, node: Node, text_node: Node, reason: str | None, span=None, suffix: str = "", data=b"") -> wi.Occurrence:
    return wi.Occurrence(
        path,
        wi.line_of(node),
        wi.text_of(text_node),
        span,
        reason,
        in_test_double=bool(data) and reason is None and mock_stubs.in_test_double(node, data, "dart"),
        suffix=suffix,
    )


def _judge(path: str, node: Node, root: Node, data: bytes, wrapper: wi.Wrapper, index: Index) -> wi.Occurrence:
    if node.type == "identifier_dollar_escaped" or _inside(node, ("string_literal",)):
        return _occurrence(path, node, node.parent or node, wi.UNPARSED_MENTION)
    parent = node.parent
    head: Node = node
    member_selector: Node | None = None
    if parent is not None and parent.type == "unconditional_assignable_selector":
        member_selector = parent.parent
        if member_selector is None or member_selector.type != "selector":
            return _occurrence(path, node, parent, wi.RECEIVER_UNPROVEN)
        head = member_selector.prev_sibling
        if head is None or head.type == "selector" or head.type not in ("identifier", "this"):
            # A longer chain (`widget.flags.x`, `p.Owner.x`) or a call result.
            return _occurrence(path, node, member_selector.parent or node, wi.RECEIVER_UNPROVEN)
    elif parent is not None and parent.type == "combinator":
        if parent.children and parent.children[0].type == "show":
            return None  # deleted with the call it served, or a leftover at close-out
        return _occurrence(path, node, parent, wi.IMPORT_UNRESOLVED)
    elif not _expression_head(node):
        return _occurrence(path, node, parent or node, wi.RECEIVER_UNPROVEN)
    last = member_selector if member_selector is not None else node
    after = last.next_sibling
    arguments = _arguments(after) if after is not None else None
    container = last.parent
    shown = container if container is not None else node
    called = after is not None and after.type == "selector" and any(c.type == "argument_part" for c in after.children)
    if (wrapper.kind == "property" and called) or (wrapper.kind != "property" and arguments is None):
        # A getter's value called, a method torn off, or type arguments.
        return _occurrence(path, node, shown, wi.RECEIVER_UNPROVEN)
    if _has_parts(root):
        # A part shares its library's imports, and the stranded-import
        # cleanup does not touch a library with parts.
        return _occurrence(path, node, shown, wi.IMPORT_UNRESOLVED)
    reason = _receiver_reason(path, node, head, member_selector, root, data, wrapper, index)
    if reason is None and _argument_does_work(arguments):
        reason = wi.ARGUMENT_HAS_CALL
    if reason is not None:
        return _occurrence(path, node, shown, reason)
    end = last.end_byte
    span = (head.start_byte, end)
    suffix = "()" if wrapper.kind == "property" else ""
    return _occurrence(path, node, shown, None, span, suffix, data)


def _receiver_reason(path, node, head, member_selector, root, data, wrapper, index) -> str | None:
    bare = member_selector is None
    if top_level(wrapper):
        if bare:
            return None if _reaches(wrapper.name, path, root, wrapper.path, index) else wi.IMPORT_UNRESOLVED
        if head.type != "identifier":
            return wi.RECEIVER_UNPROVEN
        return None if _prefix_reaches(_text(head), wrapper.name, path, root, wrapper.path, index) else wi.IMPORT_UNRESOLVED
    inside = _is_owner(_enclosing_class(node), wrapper, path)
    if bare or head.type == "this":
        # Inside its class the member is the nearest declaration of the name
        # (an instance one read from a static context does not compile).
        return None if inside and not (head.type == "this" and wrapper.static) else wi.RECEIVER_UNPROVEN
    if wrapper.static:
        if _text(head) != wrapper.owner:
            return wi.RECEIVER_UNPROVEN
        return None if _owner_reaches(path, root, wrapper, index) else wi.IMPORT_UNRESOLVED
    return None if _receiver_typed(head, path, root, data, wrapper, index) else wi.RECEIVER_UNPROVEN


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    cached = index.file(path, source)
    if cached is None:
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    root = cached[1]
    if syntax.source_is_unreadable(source, "dart"):
        # The engine is never handed this file, so a mark would survive.
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    data = source.encode("utf-8")
    found: list[wi.Occurrence] = []
    for node in cross_file_keys.walk(root):
        if node.type not in MENTION_TYPES or _text(node) != wrapper.name or names_only(node):
            continue
        judged = _judge(path, node, root, data, wrapper, index)
        if judged is not None:
            found.append(judged)
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def test_title(path: str, source: str, string: Node) -> bool:
    """Whether ``string`` is the description of a ``test``/``group`` call,
    which the test runner consumes and so never names a member."""
    argument = string.parent
    arguments = argument.parent if argument is not None and argument.type == "argument" else None
    if arguments is None or arguments.type != "arguments":
        return False
    first = next((c for c in arguments.named_children if "comment" not in c.type), None)
    selector = arguments.parent.parent if arguments.parent is not None else None
    callee = selector.prev_sibling if selector is not None and selector.type == "selector" else None
    return (
        first == argument
        and callee is not None
        and callee.type == "identifier"
        and _text(callee) in _TEST_CALLS
    )


def _named_strings(path: str, source: str, root: Node, name: str) -> list[tuple[int, str]]:
    """String literals whose whole content is ``name``: a lookup key, which
    can reach the member through a table or a platform channel.

    A string that only mentions the name in its prose (``'newCheckout is
    on'``) names nothing; an interpolation of it is an identifier, which
    the call-site scan judges. A test's description is exempt."""
    found: list[tuple[int, str]] = []
    for node in cross_file_keys.walk(root):
        if node.type not in _STRINGS or test_title(path, source, node):
            continue
        quoted = key_const._DART_QUOTED.match(_text(node).lstrip("rR"))
        if quoted is not None and quoted.group(2) == name:
            found.append((wi.line_of(node), wi.text_of(node)))
    return found + _doc_references(root, name)


def _doc_references(root: Node, name: str) -> list[tuple[int, str]]:
    """Doc comments referring to the member (``[newCheckout]``,
    ``[Flags.banner]``), which the analyzer resolves: a reference left
    dangling, and the import it kept in use, would both be reported."""
    reference = re.compile(rf"\[(?:\w+\.)*{re.escape(name)}\]")
    return [
        (wi.line_of(node), wi.text_of(node))
        for node in cross_file_keys.walk(root)
        if "comment" in node.type and reference.search(_text(node))
    ]


def string_mentions(index: Index, path: str, wrapper: wi.Wrapper) -> list[tuple[int, str]]:
    cached = index.file(path)
    return [] if cached is None else _named_strings(path, cached[0], cached[1], wrapper.name)


def leftovers(
    repo_dir: str, wrapper: wi.Wrapper, removable: frozenset[str] = frozenset()
) -> list[tuple[str, int, str]]:
    """Every mention of the wrapper's name still in the repository, read
    fresh: any word match in a file that is not Dart, and in a Dart file each
    identifier spelling it that does not only name something (a ``show``
    entry does count), and each string that is the name. ``removable`` is
    not needed: the stranded-import cleanup has already run."""
    found: list[tuple[str, int, str]] = []
    for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=()):
        source = _read(path)
        if not path.endswith(EXTENSIONS) or source is None:
            found += [(path, line, text) for line, text in wi._foreign_lines(path, wrapper.name)]
            continue
        root = parse(source)
        found += [(path, line, text) for line, text in _named_strings(path, source, root, wrapper.name)]
        found += [
            (path, wi.line_of(n), wi.text_of(n.parent or n))
            for n in cross_file_keys.walk(root)
            if n.type in MENTION_TYPES and _text(n) == wrapper.name and not names_only(n)
        ]
    return found


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


wi.register(
    wi.Profile(
        language="dart",
        extensions=EXTENSIONS,
        parse=parse,
        read_test=read_test,
        discover=discover,
        occurrences=occurrences,
        index=_index,
        qualify=qualify,
        strings=_STRINGS,
        foreign_extensions=(),
        names_only=names_only,
        members=members,
        member_end=member_end,
        mention_types=MENTION_TYPES,
        keep=keep,
        inert_annotations=_INERT,
        test_title=test_title,
        string_mentions=string_mentions,
        leftovers=leftovers,
    )
)
