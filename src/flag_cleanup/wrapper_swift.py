"""Swift profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

A wrapper is a top-level function, or a method or computed property of a
top-level ``struct``, ``enum`` or ``class``, static or not, declared to return
``Bool``, whose whole body is one read of the flag (``{ read }`` or
``{ return read }``). The read is one ``rules/swift.toml`` folds in this run:
``boolVariation`` (or a named accessor) with the key first, as a literal, a
same-file constant where Gate 2 let the const rules run, or a text the
cross-file resolution proved names the key. A computed property is read with
no call, so its mark gets ``()`` after the sentinel.

Swift resolves a bare name lexically first (locals, parameters, then the
members of the enclosing types, inherited ones included), and only then at
module level; a member through the static type of its receiver. So:

* the wrapper's name must be bound once across the repository's Swift files,
  counting every declaration, parameter, local, closure parameter, pattern
  and optional binding, so nothing can hide it or overload it;
* a function is called bare, in its own module, where no enclosing type has
  an inheritance clause and none is an extension (whose members, or a
  supertype's, would come first);
* a static member is called through its type's name in its own module, or
  bare or through ``Self`` inside its type;
* an instance member is called bare or through ``self`` inside its type,
  through ``self.p`` where ``p`` is a property of the enclosing type written
  with the type as its type or initialised with ``Owner(...)``, or through a
  name whose nearest binding is such a parameter, local or property and whose
  every binding in the file is one.

A type with an inheritance clause (a protocol requirement may be called
through the protocol, and the member is that conformance), an ``open``
class, a class anything in the repository subclasses, and a member that is
``open``, ``class``, ``override``, ``dynamic`` or carries any attribute
(``@objc``, ``@available``, ...) are refused; a subclass or a sibling
conformance in a test file is reported as ``test-double``. A key path, a
``#selector`` or a string naming the member keeps it. A ``public`` wrapper is
never deleted (``published``) unless its ``Package.swift`` declares no
library product.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, key_const, mock_stubs, swift_modules, syntax
from flag_cleanup import cross_file_swift as xs
from flag_cleanup import wrapper_inline as wi

logger = logging.getLogger(__name__)

EXTENSIONS = (".swift",)
_STRINGS = frozenset({"line_string_literal", "multi_line_string_literal", "raw_string_literal"})
MENTION_TYPES = frozenset({"simple_identifier"})
_READ_CALLEES = frozenset({"boolVariation"})
#: The kinds of type a wrapper may be a member of. Not an actor, whose
#: members are ``await``ed from outside it, and not a protocol or an
#: extension, whose members dispatch dynamically or belong to a type declared
#: elsewhere.
_OWNER_KINDS = frozenset({"struct", "class", "enum"})
#: Parents under which a ``simple_identifier`` binds the name, on top of the
#: key-constant gate's set.
_BINDERS = frozenset({
    "enum_entry", "protocol_function_declaration", "capture_list_item", "class_declaration",
    "while_statement", "computed_setter", "willset_clause", "didset_clause",
})
#: What a function wrapper's declaration may hold, and nothing else: no
#: generics, ``async``, ``throws``, default argument (evaluated at the call
#: site, so inlining would drop it) or ``where`` clause.
_FUNCTION_PARTS = frozenset({"modifiers", "func", "simple_identifier", "(", ")", ",", "parameter", "->", "user_type", "function_body"})
#: Modifiers through which a member may be replaced, overridden or reached
#: dynamically, recorded on the wrapper so qualification refuses it.
_OPEN_WORDS = frozenset({"open", "class", "override", "dynamic", "optional", "required"})
#: Nodes an argument may hold for a call to be inlined: names, member
#: accesses and literals. Anything else may do work the inlining would drop.
_PLAIN = frozenset({
    "simple_identifier", "navigation_expression", "navigation_suffix", "self_expression",
    "line_string_literal", "line_str_text", "integer_literal", "real_literal", "boolean_literal",
    "value_argument_label", ".",
})
#: Parents of an expression where a literal can stand in its place. Not a
#: statement: a bare ``true`` is a warning.
_EXPRESSION_PARENTS = frozenset({
    "conjunction_expression", "disjunction_expression", "ternary_expression", "equality_expression",
    "tuple_expression",
})
_CONDITIONS = frozenset({"if_statement", "guard_statement", "while_statement", "repeat_while_statement"})
_TYPES = frozenset({"class_declaration", "protocol_declaration"})
_FUNCTIONS = frozenset({"function_declaration", "init_declaration", "lambda_literal"})
_TEST_PART = re.compile(r"(?:Tests?|Mocks?|Fakes?|Stubs?|Spies|Spy)$")


def parse(source: str) -> Node:
    return xs.parse(source.encode("utf-8"))


def _text(node: Node | None) -> str:
    return xs._text(node)


def _ident(node: Node | None) -> str:
    return xs._ident(node)


def _read(path: str) -> str | None:
    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def _walk(node: Node) -> Iterable[Node]:
    return cross_file_keys.walk(node)


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w])")


def _field(node: Node, name: str) -> Node | None:
    return node.child_by_field_name(name)


def _field_of(node: Node) -> str | None:
    """The field name ``node`` has in its parent."""
    parent = node.parent
    if parent is None:
        return None
    for i, child in enumerate(parent.children):
        if child.id == node.id:
            return parent.field_name_for_child(i)
    return None


# --- Reads and discovery ----------------------------------------------------------


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
    """Whether an expression is exactly one read of ``flag_key`` this run
    folds: never negated (the gate grammar puts a leading ``!`` inside the
    call), never optional-chained, with no trailing closure. ``loose``
    accepts any name as the key."""
    root = parse(source)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "swift", flag_key)}
        if const_ok
        else set()
    )
    texts = frozenset(external)
    callees = _READ_CALLEES | set(accessors)

    def key_matches(value: Node) -> bool:
        if value.type == "line_string_literal":
            parts = [c for c in value.named_children]
            return len(parts) == 1 and parts[0].type == "line_str_text" and _text(parts[0]) == flag_key
        written = " ".join(_text(value).split())
        if value.type == "simple_identifier":
            return loose or _ident(value) in consts or written in texts
        if value.type == "navigation_expression":
            return loose or written in texts
        return False

    def is_read(expression: Node) -> bool:
        if expression.type != "call_expression" or _text(expression).startswith("!"):
            return False
        parts = xs._code(expression)
        if len(parts) != 2 or parts[1].type != "call_suffix":
            return False
        callee, suffix = parts
        if callee.type == "navigation_expression":
            if "?" in _text(callee):
                return False
            member = _field(callee, "suffix")
            name = None if member is None else _field(member, "suffix")
        elif callee.type == "simple_identifier":
            name = callee
        else:
            return False
        if name is None or name.type != "simple_identifier" or _text(name) not in callees:
            return False
        arguments = xs._code(suffix)
        if len(arguments) != 1 or arguments[0].type != "value_arguments":
            return False
        listed = xs._code(arguments[0])
        if not listed or _field(listed[0], "name") is not None:
            return False
        value = _field(listed[0], "value")
        return value is not None and key_matches(value)

    return is_read


def _single_read(statements: Node | None, is_read: Callable[[Node], bool]) -> bool:
    """Whether ``statements`` is ``read`` or ``return read``."""
    if statements is None or statements.type != "statements":
        return False
    parts = xs._code(statements)
    if len(parts) != 1:
        return False
    only = parts[0]
    if only.type == "control_transfer_statement":
        if not _text(only).startswith("return"):
            return False
        only = _field(only, "result")
    return only is not None and is_read(only)


def _is_bool(node: Node | None) -> bool:
    return (
        node is not None
        and node.type == "user_type"
        and len(node.named_children) == 1
        and node.named_children[0].type == "type_identifier"
        and _text(node) == "Bool"
    )


def _modifier_words(declaration: Node) -> tuple[tuple[str, ...], str | None, bool] | None:
    """``(recorded words, access, static)``: each attribute as ``@name`` and
    each modifier through which the member can be overridden or reached
    dynamically; ``None`` for one that makes no wrapper (``mutating``,
    ``nonisolated``, an ownership modifier)."""
    words: list[str] = []
    access: str | None = None
    static = False
    for modifier in xs._modifier_nodes(declaration):
        text = _text(modifier).strip()
        if modifier.type == "attribute":
            name = modifier.named_children[0] if modifier.named_children else None
            words.append("@" + (_text(name) if name is not None else text))
        elif modifier.type == "visibility_modifier":
            if "(" in text:
                return None
            access = text
            if text == "open":
                words.append("open")
        elif modifier.type == "property_modifier" and text == "static":
            static = True
        elif modifier.type == "inheritance_modifier" and text == "final":
            continue
        elif text in _OPEN_WORDS:
            words.append(text)
        else:
            return None
    if any(c.type == "class" for c in declaration.children):
        words.append("class")
    return tuple(words), access, static


def _function_wrapper(declaration: Node, is_read: Callable[[Node], bool]) -> bool:
    parts = [c for c in declaration.children if c.type not in xs._COMMENTS]
    if any(c.type not in _FUNCTION_PARTS and c.type != "class" for c in parts):
        return False
    arrow = next((i for i, c in enumerate(parts) if c.type == "->"), None)
    if arrow is None or arrow + 1 >= len(parts) or not _is_bool(parts[arrow + 1]):
        return False
    body = _field(declaration, "body")
    statements = next((c for c in xs._code(body) if c.type == "statements"), None) if body is not None else None
    return _single_read(statements, is_read)


def _property_wrapper(declaration: Node, is_read: Callable[[Node], bool]) -> Node | None:
    """The bound name of ``var x: Bool { read }``; ``None`` for anything else."""
    binding = next((c for c in declaration.named_children if c.type == "value_binding_pattern"), None)
    patterns = [c for c in declaration.named_children if c.type == "pattern"]
    annotation = next((c for c in declaration.named_children if c.type == "type_annotation"), None)
    computed = _field(declaration, "computed_value")
    if (
        binding is None
        or _text(binding) != "var"
        or len(patterns) != 1
        or annotation is None
        or not _is_bool(_field(annotation, "name"))
        or computed is None
        or _field(declaration, "value") is not None
    ):
        return None
    names = [c for c in patterns[0].named_children if c.type == "simple_identifier"]
    if len(names) != 1 or len(patterns[0].named_children) != 1:
        return None
    others = [c for c in declaration.named_children if c.type not in ("modifiers", "value_binding_pattern", "pattern", "type_annotation", "computed_property") and c.type not in xs._COMMENTS]
    statements = [c for c in xs._code(computed)]
    if others or len(statements) != 1 or not _single_read(statements[0], is_read):
        return None
    return names[0]


def _owner_kind(node: Node) -> str | None:
    kind = xs._kind(node)
    name = _field(node, "name")
    if kind not in _OWNER_KINDS or name is None or name.type != "type_identifier":
        return None
    return kind


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    found: list[wi.Wrapper] = []

    def add(declaration: Node, owner: str, kind: str) -> None:
        if xs._in_conditional(declaration):
            return
        modifiers = _modifier_words(declaration)
        if modifiers is None:
            return
        words, access, static = modifiers
        if kind == "property":
            name = _property_wrapper(declaration, is_read)
            if name is None:
                return
        else:
            name = _field(declaration, "name")
            if name is None or name.type != "simple_identifier" or not _function_wrapper(declaration, is_read):
                return
        if _text(name).startswith("`"):
            return  # the rules and every scan here compare the name as written
        found.append(
            wi.Wrapper(
                path=path,
                language="swift",
                name=_text(name),
                owner=owner,
                package="",
                line=wi.line_of(declaration),
                start=declaration.start_byte,
                end=declaration.end_byte,
                static=static,
                annotations=words,
                kind=kind,
                private=access in ("private", "fileprivate"),
            )
        )

    for node in root.named_children:
        if node.type == "function_declaration":
            add(node, Path(path).name, "function")
        elif node.type == "class_declaration" and _owner_kind(node) is not None:
            body = _field(node, "body")
            for member in xs._code(body) if body is not None else ():
                if member.type == "function_declaration":
                    add(member, _ident(_field(node, "name")), "method")
                elif member.type == "property_declaration":
                    add(member, _ident(_field(node, "name")), "property")
    return found


def top_level(wrapper: wi.Wrapper) -> bool:
    return wrapper.kind == "function"


def _binds(node: Node) -> bool:
    """Whether a ``simple_identifier`` binds its name here."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "parameter" and _field_of(node) == "external_name":
        return False  # an argument label binds nothing in the body
    return key_const._swift_is_binding(node) or parent.type in _BINDERS or _field_of(node) == "bound_identifier"


def names_only(node: Node, kind: str = "method") -> bool:
    """A binding of the name (by uniqueness, the wrapper's own declaration) or
    an argument label, which names a parameter."""
    parent = node.parent
    return _binds(node) or (parent is not None and parent.type in ("value_argument_label", "parameter"))


def members(root: Node, name: str) -> list[Node]:
    """Every top-level function, and every function or property of a
    top-level struct, enum or class, of ``name``."""
    found: list[Node] = []
    for node in root.named_children:
        if node.type == "function_declaration":
            candidates = [node]
        elif node.type == "class_declaration" and _owner_kind(node) is not None and _field(node, "body") is not None:
            candidates = [m for m in xs._code(_field(node, "body")) if m.type in ("function_declaration", "property_declaration")]
        else:
            continue
        for candidate in candidates:
            if candidate.type == "function_declaration":
                written = _field(candidate, "name")
                names = [] if written is None else [_text(written)]
            else:
                names = xs._bound(candidate)
            if names == [name]:
                found.append(candidate)
    return found


# --- The repository index -----------------------------------------------------


def _is_test(path: str) -> bool:
    """A file of a test target, or a double by its name."""
    return any(_TEST_PART.search(part) for part in Path(path).with_suffix("").parts)


def _inherits(node: Node) -> list[str]:
    """The last name of each type in a declaration's inheritance clause."""
    return [
        _ident(t.named_children[-1])
        for spec in node.children
        if spec.type == "inheritance_specifier"
        for t in [_field(spec, "inherits_from")]
        if t is not None and t.named_children
    ]


@dataclass
class Index:
    repo_dir: str = ""
    #: ``(path, start byte)`` of every binding of the wrapper's name.
    declarations: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, start byte)`` of every type (or type alias, generic parameter)
    #: of the owner's name, extensions aside.
    owners: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, line, text)`` for each class that inherits from the owner.
    subtypes: list[tuple[str, int, str]] = field(default_factory=list)
    #: Each name in an inheritance clause of the owner or an extension of it.
    conformances: set[str] = field(default_factory=set)
    #: ``(path, line, text, inherited names)`` of every type declaration with
    #: an inheritance clause in a test file, for sibling conformances.
    test_types: list[tuple[str, int, str, tuple[str, ...]]] = field(default_factory=list)
    #: The owner's kind, ``open``, ``final`` and access, from its declaration.
    owner_kind: str | None = None
    owner_open: bool = False
    owner_final: bool = False
    owner_access: str | None = None
    unreadable: bool = False
    #: The last name of every type any Swift file of the checkout extends:
    #: an extension (or a conformance it adds) can bring a member of the
    #: wrapper's name into a bare lookup inside that type.
    extended: frozenset[str] = frozenset()
    layout: swift_modules.Layout | None = None
    parsed: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)
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
        index = cls(repo_dir=repo_dir)
        index.layout = swift_modules.layout(repo_dir)
        index.extended = _extended(repo_dir)
        names = [wrapper.name] + ([] if top_level(wrapper) else [wrapper.owner])
        word = _word(wrapper.name)
        for path in cross_file_keys.mention_files(repo_dir, names, exclude=()):
            text = _read(path)
            if text is None:
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            if not path.endswith(EXTENSIONS):
                # A file nothing here parses names the member: a selector,
                # a key or prose.
                if word.search(text):
                    index.unparsed.append(path)
                continue
            root = index.file(path, text)[1]
            if word.search(text):
                index.parsed.append(path)
            index._scan(path, root, wrapper)
        return index

    def _scan(self, path: str, root: Node, wrapper: wi.Wrapper) -> None:
        if root.has_error and any(
            n.type == "ERROR" and (_word(wrapper.name).search(_text(n)) or _word(wrapper.owner).search(_text(n)))
            for n in _walk(root)
        ):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        member = not top_level(wrapper)
        for node in _walk(root):
            if node.type == "simple_identifier" and _ident(node) == wrapper.name and _binds(node):
                self.declarations.append((path, node.start_byte))
            if node.type in _TYPES and _is_test(path) and _inherits(node):
                self.test_types.append((path, wi.line_of(node), _header(node), tuple(_inherits(node))))
            if not member:
                continue
            if node.type == "type_identifier" and _ident(node) == wrapper.owner and node.parent is not None and node.parent.type in xs._TYPE_BINDERS and node.parent.type != "class_declaration":
                self.owners.append((path, node.start_byte))
            if node.type not in _TYPES:
                continue
            kind = xs._kind(node)
            named = xs._type_name(node)
            if named == wrapper.owner:
                if kind == "extension":
                    self.conformances.update(_inherits(node))
                    continue
                self.owners.append((path, node.start_byte))
                if path == wrapper.path and _owner_kind(node) is not None and node.parent is not None and node.parent.type == "source_file":
                    self.owner_kind = kind
                    self.conformances.update(_inherits(node))
                    words = [_text(m).strip() for m in xs._modifier_nodes(node)]
                    self.owner_open = "open" in words
                    self.owner_final = "final" in words
                    self.owner_access = xs._written_access(node)
            elif kind != "extension" and wrapper.owner in _inherits(node):
                self.subtypes.append((path, wi.line_of(node), _header(node)))


_EXTENSION = re.compile(r"(?<![\w.])extension\s+([\w.]+)")


def _extended(repo_dir: str) -> frozenset[str]:
    from flag_cleanup import source_index

    names: set[str] = set()
    for source in source_index.files(repo_dir, {".swift"}):
        try:
            text = source.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        names.update(m.group(1).rsplit(".", 1)[-1] for m in _EXTENSION.finditer(text))
    return frozenset(names)


def _header(node: Node) -> str:
    return wi.text_of(node).split("{")[0].strip()


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | wi.Refused | None]:
    """The wrapper, or why it does not qualify."""
    if index.unreadable:
        return None, wi.NAME_NOT_UNIQUE
    if any(w.startswith("@") for w in wrapper.annotations):
        return None, wi.ANNOTATED
    if wrapper.annotations:
        return None, wi.OVERRIDABLE
    if not top_level(wrapper):
        for path, line, text in index.subtypes:
            return None, wi.Refused(wi.TEST_DOUBLE if _is_test(path) else wi.OVERRIDABLE, path, line, text)
        if index.conformances:
            sibling = next((t for t in index.test_types if set(t[3]) & index.conformances), None)
            if sibling is not None:
                return None, wi.Refused(wi.TEST_DOUBLE, *sibling[:3])
            return None, wi.OVERRIDABLE
        if index.owner_open:
            return None, wi.OVERRIDABLE
        if index.owner_kind is None or len(index.owners) != 1:
            return None, wi.NAME_NOT_UNIQUE
    if len(index.declarations) != 1:
        return None, wi.NAME_NOT_UNIQUE
    return wrapper, None


def _public(access: str | None) -> bool:
    return access in ("public", "open")


def keep(wrapper: wi.Wrapper) -> str | None:
    """Why a wrapper whose callers all folded is still not deleted: a
    ``public`` one (of a ``public`` type) may be called by another module,
    unless its ``Package.swift`` declares no library product."""
    source = _read(wrapper.path)
    if source is None:
        return wi.PUBLISHED
    root = parse(source)
    declaration = next((n for n in _walk(root) if n.start_byte == wrapper.start and n.end_byte == wrapper.end), None)
    if declaration is None or not _public(xs._written_access(declaration)):
        return None
    if not top_level(wrapper):
        owner = next(
            (n for n in root.named_children if n.type == "class_declaration" and xs._type_name(n) == wrapper.owner and xs._kind(n) != "extension"),
            None,
        )
        if owner is None or not _public(xs._written_access(owner)):
            return None
    for directory in Path(wrapper.path).parents:
        manifest = directory / "Package.swift"
        if manifest.is_file():
            try:
                text = manifest.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return wi.PUBLISHED
            return wi.PUBLISHED if ".library(" in text else None
        if (directory / ".git").exists():
            break
    # An Xcode framework, a podspec or a Carthage build ships public API with
    # no manifest that says so.
    return wi.PUBLISHED


# --- Call sites ---------------------------------------------------------------


def _same_module(index: Index, path: str, wrapper: wi.Wrapper) -> bool:
    """Whether ``path`` is compiled into the wrapper's module, where its
    top-level names win over every import. A checkout with no build file at
    all is one module."""
    layout = index.layout
    if layout is None:
        return False
    mine, theirs = layout.modules_of(path), layout.modules_of(wrapper.path)
    if mine is None and theirs is None:
        return not layout.modules and not layout.unknown and not layout.opaque_names
    return mine is not None and theirs is not None and bool(mine & theirs)


def _enclosing_type(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type in _TYPES:
            return parent
        parent = parent.parent
    return None


def _in_owner(node: Node, path: str, wrapper: wi.Wrapper) -> bool:
    """Whether the nearest enclosing type is the wrapper's own declaration."""
    holder = _enclosing_type(node)
    return (
        holder is not None
        and path == wrapper.path
        and xs._kind(holder) != "extension"
        and xs._type_name(holder) == wrapper.owner
        and holder.parent is not None
        and holder.parent.type == "source_file"
    )


def _opens_scope(node: Node, index: Index) -> bool:
    """Whether a bare name may reach another type's member before the module:
    an enclosing type with an inheritance clause, any extension, or a type an
    extension anywhere in the checkout extends (which can add a conformance,
    and with it a protocol extension's members)."""
    parent = node.parent
    while parent is not None:
        if key_const._swift_opens_a_supertype(parent):
            return True
        if parent.type in _TYPES and (xs._type_name(parent) or "").rsplit(".", 1)[-1] in index.extended:
            return True
        parent = parent.parent
    return False


def _owner_typed(binding: Node, wrapper: wi.Wrapper) -> bool:
    """Whether a binding of a name is written with the owner as its type, or
    is a property initialised with ``Owner(...)``."""
    parent = binding.parent
    if parent is None:
        return False
    if parent.type in ("parameter", "lambda_parameter"):
        if _field_of(binding) != "name" or _unwrapped(parent):
            return False
        written = [c for c in parent.named_children if c.type not in ("simple_identifier",) and c.type not in xs._COMMENTS]
        return len(written) == 1 and _is_owner_type(written[0], wrapper)
    if parent.type != "pattern" or len(parent.named_children) != 1:
        return False
    declaration = parent.parent
    if declaration is None or declaration.type != "property_declaration":
        return False
    if len([c for c in declaration.named_children if c.type == "pattern"]) != 1:
        return False
    # A stored property or a local, read with no work: not a computed or a
    # `lazy` one, whose getter or initialiser the inlining would drop.
    if _field(declaration, "computed_value") is not None or any(
        _text(m).strip() == "lazy" for m in xs._modifier_nodes(declaration)
    ):
        return False
    annotation = next((c for c in declaration.named_children if c.type == "type_annotation"), None)
    if annotation is not None:
        return not _unwrapped(annotation) and _is_owner_type(_field(annotation, "name"), wrapper)
    value = _field(declaration, "value")
    if value is None or value.type != "call_expression":
        return False
    parts = xs._code(value)
    return (
        len(parts) == 2
        and parts[0].type == "simple_identifier"
        and _ident(parts[0]) == wrapper.owner
        and parts[1].type == "call_suffix"
        and [c.type for c in xs._code(parts[1])] == ["value_arguments"]
    )


def _unwrapped(node: Node) -> bool:
    """Whether a written type is optional or implicitly unwrapped
    (``Flags!`` keeps its ``!`` beside the type, not in it)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("!", "?", "optional_type"):
            return True
        stack.extend(current.children)  # the `!` is an anonymous token
    return False


def _is_owner_type(node: Node | None, wrapper: wi.Wrapper) -> bool:
    return (
        node is not None
        and node.type == "user_type"
        and len(node.named_children) == 1
        and node.named_children[0].type == "type_identifier"
        and _ident(node.named_children[0]) == wrapper.owner
    )


def _property_of(holder: Node, name: str) -> Node | None:
    """The binding of ``name`` by a property declared in ``holder``'s body."""
    body = _field(holder, "body")
    for member in xs._code(body) if body is not None else ():
        if member.type != "property_declaration":
            continue
        for pattern in (c for c in member.named_children if c.type == "pattern"):
            for n in pattern.named_children:
                if n.type == "simple_identifier" and _ident(n) == name:
                    return n
    return None


def _receiver_bound(receiver: Node, root: Node, wrapper: wi.Wrapper) -> bool:
    """Whether a receiver name's nearest binding is typed as the owner, and
    every binding of the name in the file is too, so that no binding form
    read here wrongly can stand between."""
    name = _ident(receiver)
    for node in _walk(root):
        if node.type == "simple_identifier" and _ident(node) == name and _binds(node):
            if not _owner_typed(node, wrapper):
                return False
    current = receiver
    while current.parent is not None:
        scope = current.parent
        if scope.type in _FUNCTIONS:
            if any(
                n.type == "simple_identifier"
                and _ident(n) == name
                and n.parent is not None
                and n.parent.type in ("parameter", "lambda_parameter")
                and _field_of(n) == "name"
                and (function := _nearest_function(n)) is not None
                and function.id == scope.id
                for n in _walk(scope)
            ):
                return True
        elif scope.type in ("statements", "source_file"):
            if any(
                statement.type == "property_declaration" and name in xs._bound(statement)
                for statement in xs._code(scope)
            ):
                return True
        elif scope.type in _TYPES:
            return _property_of(scope, name) is not None
        current = scope
    return False


def _nearest_function(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.type not in _FUNCTIONS:
        parent = parent.parent
    return parent


def _reference(call: Node) -> bool:
    """Whether ``m(_:)`` names a function by its labels rather than calls it."""
    return any(
        argument.type == "value_argument" and _field(argument, "value") is None
        for part in xs._code(xs._code(call)[-1])
        for argument in xs._code(part)
    )


def _plain_arguments(suffix: Node | None) -> bool:
    if suffix is None:
        return False
    parts = xs._code(suffix)
    if len(parts) != 1 or parts[0].type != "value_arguments":
        return False  # a trailing closure
    return all(n.type in _PLAIN for argument in xs._code(parts[0]) for n in _walk(argument) if n.is_named and n.id != argument.id)


def _value_position(node: Node) -> bool:
    """Whether a literal can stand where ``node`` is."""
    parent = node.parent
    if parent is None:
        return False
    place = _field_of(node)
    if parent.type in _EXPRESSION_PARENTS:
        return True
    if parent.type == "prefix_expression":
        return _text(_field(parent, "operation")) == "!" and _value_position(parent)
    if parent.type in _CONDITIONS:
        return place == "condition"
    if parent.type == "control_transfer_statement":
        return place == "result" and _text(parent).startswith("return")
    if parent.type == "property_declaration":
        return place == "value"
    if parent.type == "assignment":
        return place == "result"
    if parent.type == "value_argument":
        return place == "value"
    if parent.type == "statements" and len(xs._code(parent)) == 1:
        # The implicit return of a single-expression body that returns a value.
        holder = parent.parent
        if holder is not None and holder.type == "function_body":
            function = holder.parent
            return function is not None and function.type == "function_declaration" and any(
                c.type == "->" for c in function.children
            )
        return holder is not None and holder.type in ("computed_property", "computed_getter")
    return False


def _unbang(node: Node) -> Node:
    if node.type == "prefix_expression" and _text(_field(node, "operation")) == "!":
        target = _field(node, "target")
        return target if target is not None else node
    return node


def _occurrence(path: str, node: Node, shown: Node, reason: str | None, span=None, suffix: str = "") -> wi.Occurrence:
    return wi.Occurrence(path, wi.line_of(node), wi.text_of(shown), span, reason, suffix=suffix)


def _judge(path: str, node: Node, root: Node, wrapper: wi.Wrapper, index: Index) -> wi.Occurrence:
    parent = node.parent
    receiver: Node | None = None
    if any(a.type in ("selector_expression", "key_path_expression", "key_path_string_expression") for a in _ancestors(node)):
        return _occurrence(path, node, parent or node, wi.UNPARSED_MENTION)
    if parent is not None and parent.type == "navigation_suffix" and _field(parent, "suffix") == node:
        head = parent.parent
        target = _field(head, "target") if head is not None else None
        if head is None or target is None or "?" in _text(head):
            return _occurrence(path, node, head or node, wi.RECEIVER_UNPROVEN)
        if target.type == "key_path_expression":
            return _occurrence(path, node, head, wi.UNPARSED_MENTION)
        receiver = target
    else:
        head = node
    # `!x.m()` parses as `(!x).m()`, `!m()` as `(!m)()`: the bang sits inside
    # the callee, so it is stepped over, and the mark starts after it.
    outer = head.parent
    call: Node | None = None
    if outer is not None and outer.type == "call_expression" and xs._code(outer)[0].id == head.id:
        call = outer
    elif (
        outer is not None
        and outer.type == "prefix_expression"
        and _text(_field(outer, "operation")) == "!"
        and outer.parent is not None
        and outer.parent.type == "call_expression"
        and xs._code(outer.parent)[0].id == outer.id
    ):
        call = outer.parent
    expression = call if call is not None else head
    shown = expression
    if _text(expression).startswith("!!") or _text(head).startswith("!!"):
        return _occurrence(path, node, shown, wi.RECEIVER_UNPROVEN)
    if (call is None) != (wrapper.kind == "property") or (call is not None and _reference(call)):
        # A method named without a call, or by its labels (`Flags.check(_:)`),
        # hands it on; a property is never called.
        return _occurrence(path, node, shown, wi.RECEIVER_UNPROVEN)
    if not _value_position(expression):
        return _occurrence(path, node, shown, wi.RECEIVER_UNPROVEN)
    reason = _receiver_reason(path, node, receiver, root, wrapper, index)
    if reason is None and call is not None and not _plain_arguments(xs._code(call)[-1]):
        reason = wi.ARGUMENT_HAS_CALL
    if reason is not None:
        return _occurrence(path, node, shown, reason)
    start = head.start_byte + (1 if _text(head).startswith("!") else 0)
    return _occurrence(path, node, shown, None, (start, node.end_byte), "" if call is not None else "()")


def _ancestors(node: Node) -> Iterable[Node]:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _receiver_reason(path, node, receiver, root, wrapper, index) -> str | None:
    if receiver is None:
        if top_level(wrapper):
            if not _same_module(index, path, wrapper):
                return wi.IMPORT_UNRESOLVED
            return wi.RECEIVER_UNPROVEN if _opens_scope(node, index) else None
        return None if _in_owner(node, path, wrapper) else wi.RECEIVER_UNPROVEN
    if top_level(wrapper):
        return wi.RECEIVER_UNPROVEN
    target = _unbang(receiver)
    if wrapper.static:
        if target.type == "simple_identifier" and _ident(target) == "Self":
            return None if _in_owner(node, path, wrapper) else wi.RECEIVER_UNPROVEN
        if target.type == "simple_identifier" and _ident(target) == wrapper.owner:
            if not _same_module(index, path, wrapper):
                return wi.IMPORT_UNRESOLVED
            if _opens_scope(node, index) or any(
                n.type == "simple_identifier" and _ident(n) == wrapper.owner and _binds(n) for n in _walk(root)
            ):
                # A supertype or an extension's protocol may declare a type of
                # the same name, which a lookup inside the type finds first.
                return wi.RECEIVER_UNPROVEN
            return None
        return wi.RECEIVER_UNPROVEN
    if target.type == "self_expression":
        return None if _in_owner(node, path, wrapper) else wi.RECEIVER_UNPROVEN
    if not _same_module(index, path, wrapper):
        return wi.IMPORT_UNRESOLVED
    if target.type == "simple_identifier":
        return None if _receiver_bound(target, root, wrapper) else wi.RECEIVER_UNPROVEN
    if target.type == "navigation_expression":
        holder = _unbang(_field(target, "target")) if _field(target, "target") is not None else None
        member = _field(target, "suffix")
        name = _field(member, "suffix") if member is not None else None
        if holder is None or holder.type != "self_expression" or name is None or name.type != "simple_identifier":
            return wi.RECEIVER_UNPROVEN
        enclosing = _enclosing_type(node)
        binding = _property_of(enclosing, _ident(name)) if enclosing is not None else None
        return None if binding is not None and _owner_typed(binding, wrapper) else wi.RECEIVER_UNPROVEN
    return wi.RECEIVER_UNPROVEN


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    cached = index.file(path, source)
    if cached is None:
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    root = cached[1]
    if root.has_error:
        # The engine will not rewrite a file it cannot parse, so a mark there
        # would survive.
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    found: list[wi.Occurrence] = []
    for node in _walk(root):
        if node.type not in MENTION_TYPES or _ident(node) != wrapper.name or names_only(node):
            continue
        if _text(node).startswith("`"):
            # The mark is placed by the name as written.
            found.append(_occurrence(path, node, node.parent or node, wi.RECEIVER_UNPROVEN))
            continue
        found.append(_judge(path, node, root, wrapper, index))
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def _named_strings(root: Node, name: str) -> list[tuple[int, str]]:
    """Literals whose whole text names the member (``"newCheckout"``,
    ``"newCheckout:"``), as a selector, a key-value key or a reflection
    lookup does."""
    whole = re.compile(rf"{re.escape(name)}(?:[:(].*)?", re.DOTALL)
    found: list[tuple[int, str]] = []
    for node in _walk(root):
        if node.type in _STRINGS:
            parts = node.named_children
            text = "".join(_text(p) for p in parts) if all(p.type in ("line_str_text", "multi_line_str_text", "raw_str_part") for p in parts) else None
            if text is not None and whole.fullmatch(text.strip()):
                found.append((wi.line_of(node), wi.text_of(node)))
    return found


def string_mentions(index: Index, path: str, wrapper: wi.Wrapper) -> list[tuple[int, str]]:
    cached = index.file(path)
    return [] if cached is None else _named_strings(cached[1], wrapper.name)


def leftovers(
    repo_dir: str, wrapper: wi.Wrapper, removable: frozenset[str] = frozenset()
) -> list[tuple[str, int, str]]:
    """Every mention of the wrapper's name still in the repository, read
    fresh: any word match in a file that is not Swift, and in a Swift file
    each identifier spelling it that does not only name something, and each
    string naming it."""
    found: list[tuple[str, int, str]] = []
    word = _word(wrapper.name)
    for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=()):
        source = _read(path)
        if source is None:
            found.append((path, 1, ""))
            continue
        if not path.endswith(EXTENSIONS):
            found += [
                (path, number, " ".join(line.split()))
                for number, line in enumerate(source.splitlines(), start=1)
                if word.search(line)
            ]
            continue
        root = parse(source)
        if root.has_error:
            found.append((path, 1, ""))
            continue
        found += [(path, line, text) for line, text in _named_strings(root, wrapper.name)]
        found += [
            (path, wi.line_of(n), wi.text_of(n.parent or n))
            for n in _walk(root)
            if n.type in MENTION_TYPES and _ident(n) == wrapper.name and not names_only(n)
        ]
    return found


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


wi.register(
    wi.Profile(
        language="swift",
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
        mention_types=MENTION_TYPES,
        keep=keep,
        inert_annotations=frozenset(),
        string_mentions=string_mentions,
        leftovers=leftovers,
    )
)
