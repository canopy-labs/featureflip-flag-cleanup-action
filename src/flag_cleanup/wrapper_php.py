"""PHP profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

A wrapper is a top-level function, or a method of a ``class``, static or
not, declared to return ``bool`` (or with no return type), whose whole body
is ``{ return <read>; }``. The read is one ``rules/php.toml`` folds in this
run: ``boolVariation`` (or a named accessor) or OpenFeature's
``getBooleanValue`` with the key first, as a literal, a same-file constant
where Gate 2 let the const rules run, or a text the cross-file resolution
proved names the key.

PHP binds a name at compile time, by its namespace, and never lexically: a
local cannot hide a function or a method. So:

* the wrapper's name must be declared once across the repository's PHP files,
  as a function or a method of any class-like, compared without case as PHP
  compares it;
* a function is called bare where the file's namespace, a ``use function``
  import or the global fallback resolves the name to it, or qualified
  (``\\App\\Flags\\newCheckout()``, ``Flags\\newCheckout()``) where the name
  resolves to it;
* a static method is called through its class's name, resolved through the
  file's namespace and imports, or as ``self::`` inside its class;
* an instance method is called as ``$this->m()`` inside its class, through a
  parameter of the enclosing function or method typed as the class and never
  written, or as ``$this->prop->m()`` where ``prop`` is a ``private``
  property of the enclosing class typed as the class.

A class anything in the repository extends, that is abstract, that has an
``extends`` or ``implements`` clause, uses a trait, or declares ``__call`` /
``__callStatic`` is refused; a PHPUnit, Mockery or Prophecy double of the
class or of the method refuses it as ``test-double``. A string naming the
wrapper (a callable, ``[$obj, 'm']``, ``call_user_func('f')``) keeps it.
A public wrapper of a Composer package that is not a ``project`` is never
deleted (``published``), since code outside the repository may call it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, cross_file_php, key_const, mock_stubs, syntax
from flag_cleanup import wrapper_inline as wi

logger = logging.getLogger(__name__)

EXTENSIONS = cross_file_php.PHP_EXTENSIONS
_STRINGS = frozenset({"string", "encapsed_string", "heredoc", "nowdoc"})
#: A function or method name is a ``name`` node wherever it is spelled.
MENTION_TYPES = frozenset({"name"})
#: Read callees the seed rules fold, and whether a bare call of one counts.
_READ_CALLEES = {"boolVariation": True, "getBooleanValue": False}
#: Attributes that say nothing about who calls a member.
_INERT = frozenset({"Deprecated", "ReturnTypeWillChange", "Pure", "SensitiveParameter"})
#: Magic methods through which a class answers a call of a name it does not
#: declare, so that deleting a member changes what a call does.
_MAGIC = frozenset({"__call", "__callstatic"})
#: Calls of the PHPUnit, Mockery and Prophecy APIs (and php-mock's function
#: mocks) that create a double of a class or stub one of its methods.
_MOCK_CALLS = frozenset({
    "createmock", "createstub", "createpartialmock", "createconfiguredmock", "createconfiguredstub",
    "createtestproxy", "getmockbuilder", "getmockforabstractclass", "getmockfortrait", "mock", "spy",
    "partialmock", "namedmock", "instancemock", "prophesize", "getfunctionmock", "method",
    "shouldreceive", "shouldnotreceive", "shouldhavereceived", "shouldnothavereceived", "allows",
    "expects", "onlymethods", "addmethods", "setmethods", "setmethodsexcept",
})
#: Calls into which a value bound to a parameter can leave the scope or
#: be rebound by name.
_SCOPE_READERS = frozenset({
    "compact", "extract", "get_defined_vars", "eval", "parse_str", "assert", "mb_ereg_replace",
    "mb_eregi_replace",
})
#: Nodes that rebind ``$this`` or ``self`` (``Closure::bind``) or open an
#: anonymous class: a proof through either stops there.
_SCOPE_BREAKS = frozenset({"anonymous_function", "arrow_function", "object_creation_expression"})
_FUNCTIONS = ("function_definition", "method_declaration")
#: Work in an argument that inlining the call would drop.
_WORK = frozenset({
    "function_call_expression", "member_call_expression", "nullsafe_member_call_expression",
    "scoped_call_expression", "object_creation_expression", "assignment_expression",
    "augmented_assignment_expression", "reference_assignment_expression", "update_expression",
    "yield_expression", "include_expression", "include_once_expression", "require_expression",
    "require_once_expression", "throw_expression", "print_intrinsic", "exit_statement",
    "shell_command_expression", "variadic_unpacking", "clone_expression", "variadic_placeholder",
})
#: Where a call is an expression the seed rules fold to a literal. Not an
#: argument: a literal cannot be passed by reference, and the callee's
#: signature is not read here.
_EXPRESSION_PARENTS = frozenset({
    "parenthesized_expression", "binary_expression", "unary_op_expression", "conditional_expression",
    "return_statement", "expression_statement", "array_element_initializer", "echo_statement",
    "match_conditional_expression", "match_default_expression",
})


#: Statement lists, where a statement can be deleted on its own.
_BLOCKS = frozenset({"compound_statement", "program", "colon_block", "case_statement", "default_statement"})


def parse(source: str) -> Node:
    return syntax._parse(source, "php")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def _read(path: str) -> str | None:
    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def _word(name: str, fold: bool = True) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$\\]){re.escape(name)}(?![\w])", re.IGNORECASE if fold else 0)


def _walk(node: Node) -> Iterable[Node]:
    return cross_file_keys.walk(node)


# --- Reads and discovery ----------------------------------------------------------


def _written(node: Node) -> str:
    """A key argument as the cross-file resolution spells it: ``Keys.NAME``."""
    return " ".join(_text(node).split()).replace("::", ".").replace("->", ".")


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
    folds. ``loose`` accepts any constant as the key."""
    root = parse(source)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "php", flag_key)}
        if const_ok
        else set()
    )
    texts = frozenset(external)
    callees = {**_READ_CALLEES, **{a: True for a in accessors}}

    def key_matches(value: Node) -> bool:
        if value.type in ("string", "encapsed_string"):
            contents = list(value.named_children)
            return len(contents) == 1 and contents[0].type == "string_content" and _text(contents[0]) == flag_key
        if value.type == "name":
            return loose or _text(value) in consts or _written(value) in texts
        if value.type == "class_constant_access_expression":
            scope, member = value.named_children[0], value.named_children[-1]
            if scope.type == "relative_scope" and _text(scope) in ("self", "static") and member.type == "name":
                return loose or _text(member) in consts or _written(value) in texts
            return loose or _written(value) in texts
        return _written(value) in texts

    def is_read(expression: Node) -> bool:
        if expression.type == "member_call_expression":
            callee = expression.child_by_field_name("name")
            bare_ok = True
        elif expression.type == "function_call_expression":
            callee = expression.child_by_field_name("function")
            bare_ok = False
        else:
            return False
        if callee is None or callee.type != "name" or _text(callee) not in callees:
            return False
        if not bare_ok and not callees[_text(callee)]:
            return False
        arguments = expression.child_by_field_name("arguments")
        listed = [c for c in arguments.named_children if c.type != "comment"] if arguments is not None else []
        if not listed or listed[0].type != "argument":
            return False
        values = [c for c in listed[0].named_children if c.type != "comment"]
        return bool(values) and key_matches(values[-1])

    return is_read


def _single_read(body: Node | None, is_read: Callable[[Node], bool]) -> bool:
    """Whether ``body`` is ``{ return read; }``."""
    if body is None or body.type != "compound_statement":
        return False
    statements = [c for c in body.named_children if c.type != "comment"]
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    values = [c for c in statements[0].named_children if c.type != "comment"]
    return len(values) == 1 and is_read(values[0])


def _returns_bool(declaration: Node) -> bool:
    """Declared ``bool``, or no return type at all (the read returns one)."""
    written = declaration.child_by_field_name("return_type")
    return written is None or (written.type == "primitive_type" and _text(written).lower() == "bool")


def _does_work(node: Node | None) -> bool:
    return node is not None and any(n.type in _WORK for n in _walk(node))


def _plain_parameters(declaration: Node) -> bool:
    """No by-reference parameter (a call creates the variable it names) and
    no default whose evaluation does work. A by-reference return is refused
    too."""
    if any(c.type == "reference_modifier" for c in declaration.children):
        return False
    parameters = declaration.child_by_field_name("parameters")
    for parameter in parameters.named_children if parameters is not None else ():
        if parameter.type == "comment":
            continue
        if parameter.type not in ("simple_parameter", "variadic_parameter"):
            return False
        if parameter.child_by_field_name("reference_modifier") is not None or _does_work(
            parameter.child_by_field_name("default_value")
        ):
            return False
    return True


_DOC_ANNOTATION = re.compile(r"(?<![\w@])@([A-Z][\w\\]*)")


def _annotations(declaration: Node) -> tuple[str, ...]:
    """Attribute names on the declaration, and docblock annotations above it
    (``@Route(...)``), which a framework reads at run time."""
    found = [
        _text(n).rsplit("\\", 1)[-1]
        for c in declaration.named_children
        if c.type == "attribute_list"
        for n in _walk(c)
        if n.type == "attribute" and n.named_children
        for n in [n.named_children[0]]
    ]
    previous = declaration.prev_named_sibling
    if previous is not None and previous.type == "comment" and _text(previous).startswith("/**"):
        found += [m.group(1).rsplit("\\", 1)[-1] for m in _DOC_ANNOTATION.finditer(_text(previous))]
    return tuple(found)


def _namespace_at(root: Node, byte: int) -> str | None:
    section = cross_file_php._section_at(cross_file_php._sections(root), byte)
    return None if section is None else section.namespace


def _modifier(declaration: Node, kind: str) -> bool:
    return any(c.type == kind for c in declaration.named_children)


def _visibility(declaration: Node) -> str:
    written = next((c for c in declaration.named_children if c.type == "visibility_modifier"), None)
    return _text(written).lower() if written is not None else "public"


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    if cross_file_php.vendored(Path(path)):
        # A Composer dependency is never rewritten.
        return []
    root = parse(source)
    found: list[wi.Wrapper] = []

    def add(declaration: Node, owner: str, method: bool) -> None:
        name = declaration.child_by_field_name("name")
        namespace = _namespace_at(root, declaration.start_byte)
        if (
            name is None
            or namespace is None
            or _modifier(declaration, "abstract_modifier")
            or not _returns_bool(declaration)
            or not _plain_parameters(declaration)
            or not _single_read(declaration.child_by_field_name("body"), is_read)
        ):
            return
        found.append(
            wi.Wrapper(
                path=path,
                language="php",
                name=_text(name),
                owner=owner,
                package=namespace,
                line=wi.line_of(declaration),
                start=declaration.start_byte,
                end=declaration.end_byte,
                static=method and _modifier(declaration, "static_modifier"),
                annotations=_annotations(declaration),
                kind="method" if method else "function",
                private=method and _visibility(declaration) == "private",
            )
        )

    stem = Path(path).name
    for node in _walk(root):
        if not cross_file_php._top_level(node):
            continue
        if node.type == "function_definition":
            add(node, stem, False)
        elif node.type == "class_declaration":
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is None or body is None:
                continue
            for member in body.named_children:
                if member.type == "method_declaration":
                    add(member, _text(name), True)
    return found


def top_level(wrapper: wi.Wrapper) -> bool:
    return wrapper.kind == "function"


def _fqn(namespace: str, name: str) -> str:
    return f"{namespace}\\{name}".lower() if namespace else name.lower()


def _declared_name(node: Node) -> bool:
    """Whether the ``name`` node is a function's or a method's own name."""
    parent = node.parent
    return parent is not None and parent.type in _FUNCTIONS and parent.child_by_field_name("name") == node


def _label(node: Node) -> bool:
    """``name:`` of a named argument, which names a parameter."""
    parent = node.parent
    return parent is not None and parent.type == "argument" and parent.child_by_field_name("name") == node


def names_only(node: Node, kind: str = "method") -> bool:
    """A declaration's own name (uniqueness leaves the wrapper's as the only
    one), a named argument's label, or a variable's name (``$newCheckout``),
    which lives apart from functions and methods."""
    parent = node.parent
    return _declared_name(node) or _label(node) or (parent is not None and parent.type == "variable_name")


def members(root: Node, name: str) -> list[Node]:
    """Every top-level function, and every method of a top-level class, of
    ``name``."""
    found: list[Node] = []
    for node in _walk(root):
        if not cross_file_php._top_level(node):
            continue
        if node.type == "function_definition":
            candidates = [node]
        elif node.type == "class_declaration" and node.child_by_field_name("body") is not None:
            candidates = [m for m in node.child_by_field_name("body").named_children if m.type == "method_declaration"]
        else:
            continue
        for candidate in candidates:
            written = candidate.child_by_field_name("name")
            if written is not None and _same(_text(written), name):
                found.append(candidate)
    return found


# --- The repository index -----------------------------------------------------


@dataclass
class Index:
    repo_dir: str = ""
    #: ``(path, start byte)`` of every function or method of the wrapper's name.
    declarations: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, start byte)`` of every class-like of the owner's name.
    owners: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, line, text)`` for each class that extends the owner.
    subtypes: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` for each double of the owner or the wrapper.
    doubles: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` for each ``class_alias`` naming the owner.
    aliases: list[tuple[str, int, str]] = field(default_factory=list)
    #: The owner is abstract, extends or implements anything, uses a trait or
    #: declares ``__call``/``__callStatic``.
    open_owner: bool = False
    #: The owner is ``final``.
    owner_closed: bool = False
    unreadable: bool = False
    parsed: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` for each line of a file that is not PHP (a
    #: template, a config file) spelling the name, judged as PHP judges it.
    bean_reads: list[tuple[str, int, str]] = field(default_factory=list)
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
        names = [wrapper.name] + ([] if top_level(wrapper) else [wrapper.owner])
        for path in cross_file_keys.mention_files(repo_dir, names, exclude=(), also=_spellings(wrapper.name)):
            if cross_file_php.vendored(Path(path)):
                continue
            text = _read(path)
            if text is None:
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            if not path.endswith(EXTENSIONS):
                index.bean_reads += _template_lines(path, text, wrapper.name)
                continue
            root = index.file(path, text)[1]
            if _word(wrapper.name).search(text):
                index.parsed.append(path)
            index._scan(path, root, wrapper)
        if top_level(wrapper):
            return index
        home = index.file(wrapper.path)
        if home is None:
            index.unreadable = True
            return index
        owner = _owner_declaration(home[1], wrapper)
        if owner is None:
            index.unreadable = True
            return index
        body = owner.child_by_field_name("body")
        index.open_owner = (
            _modifier(owner, "abstract_modifier")
            or any(c.type in ("base_clause", "class_interface_clause") for c in owner.named_children)
            or any(c.type == "use_declaration" for c in body.named_children)
            or any(
                m.type == "method_declaration"
                and (n := m.child_by_field_name("name")) is not None
                and _text(n).lower() in _MAGIC
                for m in body.named_children
            )
        )
        index.owner_closed = _modifier(owner, "final_modifier")
        return index

    def _scan(self, path: str, root: Node, wrapper: wi.Wrapper) -> None:
        word = _word(wrapper.name)
        if root.has_error and any(n.type == "ERROR" and word.search(_text(n)) for n in _walk(root)):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        method = not top_level(wrapper)
        for node in _walk(root):
            if node.type == "name" and _declared_name(node) and _same(_text(node), wrapper.name):
                self.declarations.append((path, node.start_byte))
            if not method:
                continue
            if node.type in ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"):
                name = node.child_by_field_name("name")
                if name is not None and _same(_text(name), wrapper.owner):
                    self.owners.append((path, node.start_byte))
            if node.type == "base_clause" and any(
                n.type == "name" and _same(_text(n), wrapper.owner) for n in _walk(node)
            ):
                holder = node.parent
                self.subtypes.append((path, wi.line_of(holder), wi.text_of(holder).split("{")[0].strip()))
            if node.type in ("function_call_expression", "member_call_expression", "scoped_call_expression"):
                callee = node.child_by_field_name("function" if node.type == "function_call_expression" else "name")
                if callee is None or _text(callee).rsplit("\\", 1)[-1].lower() not in ("class_alias",) + tuple(_MOCK_CALLS):
                    continue
                arguments = node.child_by_field_name("arguments")
                if arguments is None or not _names_owner(arguments, wrapper, root):
                    continue
                into = self.aliases if _text(callee).rsplit("\\", 1)[-1].lower() == "class_alias" else self.doubles
                into.append((path, wi.line_of(node), wi.text_of(node)))
        for node in _walk(root):
            # A stub of the method itself (`->method('banner')`), or a mock of
            # a function (`getFunctionMock(__NAMESPACE__, 'newCheckout')`).
            if node.type not in _STRINGS or not _same(_string_value(node) or "", wrapper.name):
                continue
            call = _enclosing_call(node)
            callee = _callee(call) if call is not None else None
            if callee is not None and _text(callee).rsplit("\\", 1)[-1].lower() in _MOCK_CALLS:
                self.doubles.append((path, wi.line_of(call), wi.text_of(call)))


_GETTER = re.compile(r"(?:is|get|has)([A-Z]\w*)")


def _spellings(name: str) -> re.Pattern[str]:
    """How a file that is not PHP can name the member, without case as PHP
    resolves it: its name, and for ``isX``/``getX``/``hasX`` the property
    a template engine reads it as (Twig's ``flags.x`` tries ``isX()``)."""
    names = [name]
    getter = _GETTER.fullmatch(name)
    if getter is not None:
        names.append(getter.group(1))
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?<![\w$\\])(?:{alternation})(?![\w])", re.IGNORECASE)


def _template_lines(path: str, text: str, name: str) -> list[tuple[str, int, str]]:
    spelled = _spellings(name)
    return [
        (path, number, " ".join(line.split()))
        for number, line in enumerate(text.splitlines(), start=1)
        if spelled.search(line)
    ]


def _string_value(node: Node) -> str | None:
    """The text of a literal with no interpolation."""
    contents = [c for c in node.named_children if c.type not in ("heredoc_start", "heredoc_end")]
    if node.type in ("string", "encapsed_string"):
        if not contents:
            return ""
        return _text(contents[0]) if len(contents) == 1 and contents[0].type == "string_content" else None
    body = next((c for c in contents if c.type in ("heredoc_body", "nowdoc_body")), None)
    if body is None or any(c.type not in ("string_content", "nowdoc_string") for c in body.named_children):
        return None
    return _text(body).strip()


def _callee(call: Node) -> Node | None:
    return call.child_by_field_name("function" if call.type == "function_call_expression" else "name")


def _enclosing_call(node: Node) -> Node | None:
    """The call whose arguments hold ``node``."""
    parent = node.parent
    while parent is not None and parent.type not in ("expression_statement", "compound_statement", "program"):
        if parent.type == "arguments" and parent.parent is not None and parent.parent.type in (
            "function_call_expression", "member_call_expression", "scoped_call_expression",
            "nullsafe_member_call_expression",
        ):
            return parent.parent
        parent = parent.parent
    return None


def _names_owner(arguments: Node, wrapper: wi.Wrapper, root: Node) -> bool:
    """``Owner::class`` (under any alias) or a string naming the class, in a
    call's arguments."""
    owner = wrapper.owner
    for node in _walk(arguments):
        if node.type == "class_constant_access_expression":
            scope, member = node.named_children[0], node.named_children[-1]
            if not _same(_text(member), "class"):
                continue
            if _same(_text(scope).rsplit("\\", 1)[-1], owner):
                return True
            resolved = cross_file_php._resolve_class(
                cross_file_php._sections(root), cross_file_php._imports(root), scope
            )
            if resolved is not None and resolved.rsplit("\\", 1)[-1] == owner.lower():
                return True
        elif node.type in _STRINGS:
            value = _string_value(node)
            if value is not None and _same(value.rsplit("\\", 1)[-1], owner):
                return True
    return False


def _owner_declaration(root: Node, wrapper: wi.Wrapper) -> Node | None:
    for node in _walk(root):
        if node.type == "class_declaration" and cross_file_php._top_level(node):
            name = node.child_by_field_name("name")
            if name is not None and _text(name) == wrapper.owner and node.child_by_field_name("body") is not None:
                return node
    return None


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | wi.Refused | None]:
    """The wrapper, or why it does not qualify."""
    if index.unreadable:
        return None, wi.NAME_NOT_UNIQUE
    if "Override" in wrapper.annotations:
        return None, wi.OVERRIDABLE
    if index.doubles:
        path, line, text = index.doubles[0]
        return None, wi.Refused(wi.TEST_DOUBLE, path, line, text)
    if not top_level(wrapper):
        if index.subtypes:
            path, line, text = index.subtypes[0]
            return None, wi.Refused(wi.OVERRIDABLE, path, line, text)
        if index.open_owner:
            return None, wi.OVERRIDABLE
        if index.aliases:
            path, line, text = index.aliases[0]
            return None, wi.Refused(wi.NAME_NOT_UNIQUE, path, line, text)
        if len(index.owners) != 1:
            return None, wi.NAME_NOT_UNIQUE
        if not wrapper.static and not wrapper.private and not index.owner_closed and keep(wrapper) == wi.PUBLISHED:
            # A package depending on this one may extend the class and hand
            # its own member in.
            return None, wi.INTERFACE_PUBLISHED
    if len(index.declarations) != 1:
        return None, wi.NAME_NOT_UNIQUE
    return wrapper, None


def keep(wrapper: wi.Wrapper) -> str | None:
    """Why a wrapper whose callers all folded is still not deleted: a public
    or protected one of a Composer package that is not a ``project`` (a
    library, Composer's default type) may be called by another package."""
    if wrapper.private:
        return None
    path = Path(wrapper.path)
    for directory in path.parents:
        manifest = directory / "composer.json"
        if manifest.is_file():
            try:
                kind = json.loads(manifest.read_text(encoding="utf-8")).get("type", "library")
            except (OSError, UnicodeDecodeError, ValueError, AttributeError):
                return wi.PUBLISHED
            return None if kind == "project" else wi.PUBLISHED
        if _wordpress_package(directory):
            return wi.PUBLISHED
        if (directory / ".git").exists():
            break
    return None


_WORDPRESS_HEADER = re.compile(r"^[ \t/*#@]*(?:Plugin|Theme) Name:", re.MULTILINE)


def _wordpress_package(directory: Path) -> bool:
    """A WordPress plugin or theme, whose functions other plugins and themes
    call: a ``Plugin Name:`` header in a PHP file, or a ``Theme Name:`` one in
    ``style.css``, at the top of ``directory``."""
    candidates = [directory / "style.css", *sorted(directory.glob("*.php"))]
    for candidate in candidates:
        try:
            with candidate.open(encoding="utf-8", errors="replace") as handle:
                head = handle.read(8192)
        except OSError:
            continue
        if _WORDPRESS_HEADER.search(head):
            return True
    return False


# --- Call sites ---------------------------------------------------------------


def _enclosing(node: Node, kinds: tuple[str, ...] | frozenset[str]) -> Node | None:
    """The nearest enclosing node of ``kinds``, or ``None`` when a scope
    break (a closure, an arrow function, an anonymous class) comes first."""
    parent = node.parent
    while parent is not None:
        if parent.type in kinds:
            return parent
        if parent.type in _SCOPE_BREAKS:
            return None
        parent = parent.parent
    return None


def _is_owner(definition: Node | None, wrapper: wi.Wrapper, path: str) -> bool:
    if definition is None or definition.type != "class_declaration" or path != wrapper.path:
        return False
    name = definition.child_by_field_name("name")
    return name is not None and _text(name) == wrapper.owner


def _resolves_to_owner(type_node: Node | None, root: Node, wrapper: wi.Wrapper) -> bool:
    """Whether a written class name (not nullable, not a union) means the
    wrapper's class here."""
    if type_node is None or type_node.type != "named_type" or type_node.named_child_count != 1:
        return False
    written = type_node.named_children[0]
    resolved = cross_file_php._resolve_class(
        cross_file_php._sections(root), cross_file_php._imports(root), written
    )
    return resolved == _fqn(wrapper.package, wrapper.owner)


def _scope_reads_by_name(scope: Node) -> bool:
    """Whether the function rebinds or reads its variables by name: ``$$x``,
    ``compact``/``extract``/``eval`` and friends, or an ``include``."""
    for node in _walk(scope):
        if node.type == "dynamic_variable_name" or node.type.startswith(("include", "require")):
            return True
        if node.type == "name" and _text(node).lower() in _SCOPE_READERS:
            return True
    return False


def _parameter_typed(receiver: Node, root: Node, wrapper: wi.Wrapper) -> bool:
    """Whether ``$f`` is a parameter of the enclosing function or method
    typed as the class (no default, not by reference, not variadic), read
    only as an object (``$f->x``) in that function, so nothing rebinds it."""
    function = _enclosing(receiver, _FUNCTIONS)
    if function is None:
        return False
    name = _text(receiver)
    parameters = function.child_by_field_name("parameters")
    declared = [
        p
        for p in (parameters.named_children if parameters is not None else ())
        if (n := p.child_by_field_name("name")) is not None and _text(n) == name
    ]
    if len(declared) != 1:
        return False
    parameter = declared[0]
    if (
        parameter.type != "simple_parameter"
        or parameter.child_by_field_name("reference_modifier") is not None
        or parameter.child_by_field_name("default_value") is not None
        or not _resolves_to_owner(parameter.child_by_field_name("type"), root, wrapper)
    ):
        return False
    body = function.child_by_field_name("body")
    if body is None or _scope_reads_by_name(body):
        return False
    for node in _walk(body):
        if node.type != "variable_name" or _text(node) != name:
            continue
        parent = node.parent
        if parent is not None and parent.type in (
            "member_call_expression", "member_access_expression",
            "nullsafe_member_call_expression", "nullsafe_member_access_expression",
        ) and parent.child_by_field_name("object") == node:
            continue
        if parent is not None and parent.type == "anonymous_function_use_clause" and not any(
            c.type == "by_ref" or _text(c) == "&" for c in parent.children
        ):
            continue
        return False
    return True


def _property_typed(access: Node, root: Node, wrapper: wi.Wrapper) -> bool:
    """Whether ``$this->prop`` names a ``private`` property of the enclosing
    class typed as the wrapper's class (declared, or promoted by the
    constructor), never unset in that class."""
    holder = access.child_by_field_name("object")
    prop = access.child_by_field_name("name")
    if holder is None or _text(holder) != "$this" or prop is None or prop.type != "name":
        return False
    definition = _enclosing(access, ("class_declaration", "trait_declaration", "enum_declaration", "interface_declaration"))
    if definition is None or definition.type != "class_declaration":
        return False
    body = definition.child_by_field_name("body")
    wanted = "$" + _text(prop)
    typed: list[Node] = []
    for member in body.named_children if body is not None else ():
        if member.type == "property_declaration":
            elements = [c for c in member.named_children if c.type == "property_element"]
            if any(_text(e.child_by_field_name("name")) == wanted for e in elements if e.child_by_field_name("name")):
                if len(elements) != 1 or _visibility(member) != "private" or _modifier(member, "static_modifier"):
                    return False
                typed.append(member.child_by_field_name("type"))
        elif member.type == "method_declaration" and _same(_text(member.child_by_field_name("name")), "__construct"):
            parameters = member.child_by_field_name("parameters")
            for parameter in parameters.named_children if parameters is not None else ():
                name = parameter.child_by_field_name("name")
                if parameter.type == "property_promotion_parameter" and name is not None and _text(name) == wanted:
                    visibility = parameter.child_by_field_name("visibility")
                    if visibility is None or _text(visibility).lower() != "private":
                        return False
                    typed.append(parameter.child_by_field_name("type"))
    if len(typed) != 1 or not _resolves_to_owner(typed[0], root, wrapper):
        return False
    for node in _walk(definition):
        if node.type == "unset_statement" and any(
            n.type == "name" and _text(n) == _text(prop) for n in _walk(node)
        ):
            return False
    return True


def _function_reaches(callee: Node, path: str, root: Node, wrapper: wi.Wrapper) -> bool:
    """Whether a called function name resolves to the wrapper: through a
    ``use function`` import, the file's namespace, or the global fallback
    (which a unique name leaves nothing to intercept)."""
    target = _fqn(wrapper.package, wrapper.name)
    sections = cross_file_php._sections(root)
    if callee.type in ("qualified_name", "relative_name"):
        return cross_file_php._resolve_class(sections, cross_file_php._imports(root), callee) == target
    section = cross_file_php._section_at(sections, callee.start_byte)
    if section is None:
        return False
    before, later = cross_file_php._bound(
        cross_file_php._imports(root), section, "function", _text(callee), callee.start_byte, fold=True
    )
    if later or len(before) > 1:
        return False
    if before:
        return before[0].target.lower().lstrip("\\") == target
    return _fqn(section.namespace, _text(callee)) == target or not wrapper.package


def _class_reaches(scope: Node, path: str, root: Node, wrapper: wi.Wrapper) -> bool:
    if scope.type == "relative_scope":
        word = _text(scope).lower()
        if word == "self" or (word == "static" and _static_is_owner(wrapper)):
            return _is_owner(_enclosing(scope, ("class_declaration", "trait_declaration", "enum_declaration")), wrapper, path)
        return False
    if scope.type not in ("name", "qualified_name", "relative_name"):
        return False
    return cross_file_php._resolve_class(
        cross_file_php._sections(root), cross_file_php._imports(root), scope
    ) == _fqn(wrapper.package, wrapper.owner)


def _static_is_owner(wrapper: wi.Wrapper) -> bool:
    """``static::`` means the class itself only when nothing outside the
    repository can extend it (nothing inside does, by qualification)."""
    return wrapper.private or keep(wrapper) is None


def _occurrence(path: str, node: Node, shown: Node, reason: str | None, span=None) -> wi.Occurrence:
    return wi.Occurrence(path, wi.line_of(node), wi.text_of(shown), span, reason)


def _import_clause(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.type in ("qualified_name", "namespace_name"):
        parent = parent.parent
    return parent if parent is not None and parent.type == "namespace_use_clause" else None


def _judge(path: str, node: Node, root: Node, wrapper: wi.Wrapper) -> wi.Occurrence | None:
    clause = _import_clause(node)
    if clause is not None:
        imported = next(
            (i for i in cross_file_php._imports(root) if i.node == clause or i.node == clause.parent), None
        )
        if (
            top_level(wrapper)
            and imported is not None
            and imported.kind == "function"
            and imported.target.lower() == _fqn(wrapper.package, wrapper.name)
            and _same(imported.alias, wrapper.name)
        ):
            return None  # stranded by the fold and removed, or a leftover at close-out
        return _occurrence(path, node, clause, wi.IMPORT_UNRESOLVED)
    parent = node.parent
    callee = node
    if parent is not None and parent.type in ("qualified_name", "relative_name") and parent.named_children[-1] == node:
        callee, parent = parent, parent.parent
    call = parent
    if call is None:
        return _occurrence(path, node, node, wi.RECEIVER_UNPROVEN)
    if any(n.type in ("encapsed_string", "heredoc") for n in _ancestors(call)):
        return _occurrence(path, node, call, wi.UNPARSED_MENTION)
    if call.type == "function_call_expression" and call.child_by_field_name("function") == callee:
        kind = "function"
    elif call.type in ("member_call_expression", "scoped_call_expression") and call.child_by_field_name("name") == node:
        kind = call.type
    else:
        return _occurrence(path, node, call, wi.RECEIVER_UNPROVEN)
    arguments = call.child_by_field_name("arguments")
    if arguments is None or any(c.type == "variadic_placeholder" for c in arguments.named_children):
        # A first-class callable (`f(...)`) hands the member on.
        return _occurrence(path, node, call, wi.RECEIVER_UNPROVEN)
    outer = call.parent
    if outer is None or outer.type not in _EXPRESSION_PARENTS and not (
        outer.type in ("assignment_expression", "augmented_assignment_expression")
        and outer.child_by_field_name("right") == call
    ):
        return _occurrence(path, node, call, wi.RECEIVER_UNPROVEN)
    if outer.type == "expression_statement" and (outer.parent is None or outer.parent.type not in _BLOCKS):
        # A braceless body (`if ($x) $f->m();`): deleting the statement would
        # hand the body to the next one.
        return _occurrence(path, node, call, wi.RECEIVER_UNPROVEN)
    top = call
    while top.parent is not None and top.parent.type == "parenthesized_expression":
        top = top.parent
    if top.parent is not None and top.parent.type == "argument":
        # PHP drops the parentheses: `f((g()))` still passes a call result.
        return _occurrence(path, node, call, wi.RECEIVER_UNPROVEN)
    reason = _receiver_reason(path, node, callee, call, kind, root, wrapper)
    if reason is None and _does_work(arguments):
        reason = wi.ARGUMENT_HAS_CALL
    if reason is not None:
        return _occurrence(path, node, call, reason)
    head = callee if kind == "function" else call
    return _occurrence(path, node, call, None, (head.start_byte, node.end_byte))


def _ancestors(node: Node) -> Iterable[Node]:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _receiver_reason(path, node, callee, call, kind, root, wrapper) -> str | None:
    if top_level(wrapper):
        if kind != "function":
            return wi.RECEIVER_UNPROVEN
        return None if _function_reaches(callee, path, root, wrapper) else wi.IMPORT_UNRESOLVED
    if kind == "function":
        return wi.RECEIVER_UNPROVEN
    if kind == "scoped_call_expression":
        if not wrapper.static:
            return wi.RECEIVER_UNPROVEN
        scope = call.child_by_field_name("scope")
        if scope is None:
            return wi.RECEIVER_UNPROVEN
        if scope.type == "relative_scope" or scope.type in ("name", "qualified_name", "relative_name"):
            if _class_reaches(scope, path, root, wrapper):
                return None
            return wi.RECEIVER_UNPROVEN if scope.type == "relative_scope" else wi.IMPORT_UNRESOLVED
        return wi.RECEIVER_UNPROVEN
    if wrapper.static:
        return wi.RECEIVER_UNPROVEN
    receiver = call.child_by_field_name("object")
    if receiver is None:
        return wi.RECEIVER_UNPROVEN
    if receiver.type == "variable_name" and _text(receiver) == "$this":
        inside = _enclosing(call, ("class_declaration", "trait_declaration", "enum_declaration", "interface_declaration"))
        return None if _is_owner(inside, wrapper, path) else wi.RECEIVER_UNPROVEN
    if receiver.type == "variable_name":
        return None if _parameter_typed(receiver, root, wrapper) else wi.RECEIVER_UNPROVEN
    if receiver.type == "member_access_expression":
        return None if _property_typed(receiver, root, wrapper) else wi.RECEIVER_UNPROVEN
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
        if node.type not in MENTION_TYPES or not _same(_text(node), wrapper.name) or names_only(node):
            continue
        judged = _judge(path, node, root, wrapper)
        if judged is not None:
            found.append(judged)
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def _named_strings(root: Node, name: str) -> list[tuple[int, str]]:
    """Literals whose whole text names the wrapper -- ``'newCheckout'``,
    ``'App\\newCheckout'``, ``'Flags::check'`` -- which PHP can call by that
    name, and inline markup (a template) spelling it as a word."""
    found: list[tuple[int, str]] = []
    word = _word(name)
    for node in _walk(root):
        if node.type in _STRINGS:
            value = _string_value(node)
            if value is None:
                continue
            tail = re.split(r"::|@|\\", value.strip().lstrip("\\"))[-1]
            if _same(tail, name):
                found.append((wi.line_of(node), wi.text_of(node)))
        elif node.type == "text" and word.search(_text(node)):
            data = _text(node)
            for match in word.finditer(data):
                line = wi.line_of(node) + data.count("\n", 0, match.start())
                found.append((line, data.splitlines()[data.count("\n", 0, match.start())].strip()))
    return found


def string_mentions(index: Index, path: str, wrapper: wi.Wrapper) -> list[tuple[int, str]]:
    cached = index.file(path)
    return [] if cached is None else _named_strings(cached[1], wrapper.name)


def leftovers(
    repo_dir: str, wrapper: wi.Wrapper, removable: frozenset[str] = frozenset()
) -> list[tuple[str, int, str]]:
    """Every mention of the wrapper's name still in the repository, read
    fresh: any word match in a file that is not PHP, and in a PHP file each
    name spelling it that does not only name something, each string naming
    it and each word of markup. An import left behind counts."""
    found: list[tuple[str, int, str]] = []
    for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=(), also=_spellings(wrapper.name)):
        if cross_file_php.vendored(Path(path)):
            continue
        source = _read(path)
        if source is None:
            found.append((path, 1, ""))
            continue
        if not path.endswith(EXTENSIONS):
            found += _template_lines(path, source, wrapper.name)
            continue
        root = parse(source)
        found += [(path, line, text) for line, text in _named_strings(root, wrapper.name)]
        found += [
            (path, wi.line_of(n), wi.text_of(n.parent or n))
            for n in _walk(root)
            if n.type in MENTION_TYPES and _same(_text(n), wrapper.name) and not names_only(n)
        ]
    return found


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


wi.register(
    wi.Profile(
        language="php",
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
        inert_annotations=_INERT,
        string_mentions=string_mentions,
        leftovers=leftovers,
    )
)
