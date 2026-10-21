"""Python profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

A wrapper is a module-level function, or a method of a module-level class
(an instance method, a ``@staticmethod``, a ``@classmethod`` or a
``@property``), whose whole body is ``return <read>``, after an optional
docstring. The read is one ``rules/python.toml`` folds in this run:
``variation(key, ctx, <bool literal>)`` (or ``default=<bool literal>``),
``get_boolean_value(key, ...)`` or a named accessor, with the key as a
literal, a same-file constant where Gate 2 let the const rules run, or a
constant of another file the cross-file resolution proved. Any other
decorator, ``async def`` and a parameter default that calls something make
it no wrapper: a decorator can change what the function returns.

**The mark carries the value.** A proven call (``flags.new_checkout(u)``, or a
property read ``self.enabled``) is replaced whole by
``<sentinel>(True)`` -- the sentinel is the marker of
:mod:`flag_cleanup.python_fold`, and ``python.toml``'s marker rule folds it
inside the cascade. Its arguments go with it, so a call whose arguments do
work is refused.

Python has no static types, so a call is proven only through a receiver the
file itself ties to the wrapper, and only where every token spelling that
receiver is one this module models (the same completeness rule the bound-read
inliner relies on):

* a function called bare in its own module, or in a module that imports it
  with ``from <its module> import name`` at the top level, or called as
  ``mod.name(...)`` through a top-level ``import <its module> [as mod]`` or
  ``from <package> import mod``, resolved like the cross-file key constants
  (:func:`flag_cleanup.cross_file_python_proofs._bindings`);
* a method called as ``self.name(...)`` (or ``cls.name(...)`` for a static or
  class method) inside a method of the owner class itself, or as
  ``Owner.name(...)`` for a static or class method, with ``Owner`` the class
  or a top-level ``from <its module> import Owner``.

A file that names ``globals``, ``vars``, ``locals``, ``exec``, ``eval``,
``__dict__`` or ``__getattribute__`` proves nothing. A test double --
``patch("pkg.mod.name")``, ``patch.object(obj, "name")``,
``MagicMock(name=...)``, ``m.name.return_value = ...``, or a mock of the
owner class for a method -- refuses the whole wrapper (``test-double``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, key_const, syntax
from flag_cleanup import wrapper_inline as wi

logger = logging.getLogger(__name__)

EXTENSIONS = (".py",)
#: Literal text only: an f-string's interpolation holds reads, not a name.
_STRINGS = frozenset({"string_content"})
_VARIATION = "variation"
_GET_BOOLEAN = "get_boolean_value"
#: Decorators that leave a method one this module can prove calls of.
_KINDS = {"staticmethod": "method", "classmethod": "method", "property": "property"}
_INERT = frozenset(_KINDS)
#: Names through which a file reaches a namespace without spelling a name.
_OPAQUE = re.compile(r"\b(?:globals|vars|locals|exec|eval|__dict__|__getattribute__|__builtins__)\b")
#: Callees whose arguments describe a double rather than call anything.
_MOCK_CALLEES = frozenset(
    {
        "patch", "object", "multiple", "dict", "spy", "create_autospec",
        "Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock", "PropertyMock",
    }
)
#: Calls that set or delete an attribute named by a string: a test's double
#: in a file with mock words, another declaration anywhere else.
_REBINDERS = frozenset({"setattr", "delattr", "__setattr__", "__delattr__"})
#: Class decorators that leave every method where the class put it.
_INERT_CLASS_DECORATORS = re.compile(
    r"(?:dataclasses\.)?dataclass(?:\(.*\))?|(?:typing\.)?final|(?:functools\.)?total_ordering", re.DOTALL
)
#: An attribute of a mock that configures or inspects it.
_MOCK_ATTRIBUTES = re.compile(
    r"return_value|side_effect|called|call_count|call_args|call_args_list|mock_calls|assert_\w+|reset_mock"
)
#: Positions where an attribute is written or deleted, not read.
_TARGETS = ("assignment", "augmented_assignment")
_PATTERNS = ("pattern_list", "tuple_pattern", "list_pattern", "as_pattern_target", "delete_statement")


def parse(source: str) -> Node:
    return syntax._parse(source, "python")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _read(path: str) -> str | None:
    from flag_cleanup import mock_stubs

    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")


def _named(node: Node | None) -> list[Node]:
    return [] if node is None else [c for c in node.named_children if c.type != "comment"]


def _plain_string(node: Node) -> str | None:
    """The text of a string with no interpolation, as the seed rules read it."""
    if node.type != "string":
        return None
    parts = [c for c in node.named_children if c.type not in ("string_start", "string_end")]
    return _text(parts[0]) if len(parts) == 1 and parts[0].type == "string_content" else None


def _callee_name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "attribute":
        function = function.child_by_field_name("attribute")
    return _text(function) if function is not None and function.type == "identifier" else None


# --- Discovery ------------------------------------------------------------------


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
    """Whether a ``call`` is a read of ``flag_key`` this run folds, in the
    shapes ``python.toml`` and its accessor and constant rules match.
    ``loose`` accepts any name as the key, for widening the undo snapshot."""
    root = parse(source)
    consts = (
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "python", flag_key)}
        if const_ok
        else set()
    )
    texts = {" ".join(t.split()) for t in external}

    def is_key(node: Node) -> bool:
        if _plain_string(node) == flag_key:
            return True
        if node.type not in ("identifier", "attribute"):
            return False
        return loose or (node.type == "identifier" and _text(node) in consts) or " ".join(_text(node).split()) in texts

    def is_read(node: Node) -> bool:
        if node.type != "call":
            return False
        args = _named(node.child_by_field_name("arguments"))
        name = _callee_name(node)
        if not args or args[0].type == "keyword_argument" or not is_key(args[0]):
            return False
        if name == _VARIATION:
            if len(args) >= 3 and args[1].type != "keyword_argument" and args[2].type in ("true", "false"):
                return True
            return any(
                a.type == "keyword_argument"
                and _text(a.child_by_field_name("name")) == "default"
                and a.child_by_field_name("value").type in ("true", "false")
                for a in args[1:]
            )
        return name == _GET_BOOLEAN or name in accessors

    return is_read


def _body_read(definition: Node, is_read: Callable[[Node], bool]) -> bool:
    """Whether the body is ``return <read>``, after an optional docstring."""
    statements = _named(definition.child_by_field_name("body"))
    if statements and statements[0].type == "expression_statement" and len(statements) == 2:
        only = _named(statements[0])
        if len(only) == 1 and only[0].type == "string":
            statements = statements[1:]
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    returned = _named(statements[0])
    return len(returned) == 1 and is_read(returned[0])


def _defaults_do_work(definition: Node) -> bool:
    """A parameter default holding a call, which runs where the function is
    defined; deleting the function would drop it."""
    for parameter in _named(definition.child_by_field_name("parameters")):
        value = parameter.child_by_field_name("value")
        if value is not None and any(n.type in ("call", "await") for n in cross_file_keys.walk(value)):
            return True
    return False


def _decorators(node: Node) -> list[str]:
    if node.type != "decorated_definition":
        return []
    return [_text(d).lstrip("@").strip() for d in node.named_children if d.type == "decorator"]


def first_parameter(definition: Node) -> Node | None:
    """The identifier of a method's first parameter (``self`` / ``cls``)."""
    params = _named(definition.child_by_field_name("parameters"))
    if not params:
        return None
    first = params[0]
    if first.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
        first = first.child_by_field_name("name") or next(iter(_named(first)), None)
    return first if first is not None and first.type == "identifier" else None


def _candidate(node: Node) -> tuple[Node, Node, list[str]] | None:
    """``(statement, function_definition, decorators)`` for a top-level or
    class-body statement that defines a function."""
    definition = node.child_by_field_name("definition") if node.type == "decorated_definition" else node
    if definition is None or definition.type != "function_definition":
        return None
    if any(c.type == "async" for c in definition.children):
        return None
    return node, definition, _decorators(node)


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    module = Path(path).stem
    found: list[wi.Wrapper] = []

    def add(statement: Node, definition: Node, owner: str, kind: str, static: bool, decorators: list[str]) -> None:
        if not _body_read(definition, is_read) or _defaults_do_work(definition):
            return
        found.append(
            wi.Wrapper(
                path=path,
                language="python",
                name=_text(definition.child_by_field_name("name")),
                owner=owner,
                package=module,
                line=wi.line_of(statement),
                start=statement.start_byte,
                end=statement.end_byte,
                static=static,
                annotations=tuple(decorators),
                kind=kind,
            )
        )

    for statement in root.named_children:
        candidate = _candidate(statement)
        if candidate is not None:
            if not candidate[2]:
                add(statement, candidate[1], module, "function", True, [])
            continue
        cls = statement.child_by_field_name("definition") if statement.type == "decorated_definition" else statement
        if cls is None or cls.type != "class_definition":
            continue
        if not all(_INERT_CLASS_DECORATORS.fullmatch(d) for d in _decorators(statement)):
            # A class decorator can hand back something else under the name.
            continue
        owner = _text(cls.child_by_field_name("name"))
        for member in _named(cls.child_by_field_name("body")):
            candidate = _candidate(member)
            if candidate is None:
                continue
            _, definition, decorators = candidate
            if len(decorators) > 1 or (decorators and decorators[0] not in _KINDS):
                continue
            kind = _KINDS.get(decorators[0], "method") if decorators else "method"
            static = bool(decorators) and decorators[0] != "property"
            if not static and first_parameter(definition) is None:
                continue
            add(member, definition, owner, kind, static, decorators)
    return found


# --- The repository index -----------------------------------------------------


def _store(attribute: Node) -> bool:
    """Whether an attribute (or identifier) is written or deleted."""
    node, parent = attribute, attribute.parent
    while parent is not None and parent.type in _PATTERNS[:3]:
        node, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type in _TARGETS or parent.type in ("for_statement", "for_in_clause"):
        return parent.child_by_field_name("left") == node
    return parent.type in _PATTERNS[3:]


def _mock_callee(node: Node) -> str | None:
    """The callee of the nearest call building a double (or setting an
    attribute by name) that holds ``node`` in its arguments."""
    current = node.parent
    while current is not None:
        if current.type == "call" and _callee_name(current) in _MOCK_CALLEES | _REBINDERS:
            arguments = current.child_by_field_name("arguments")
            if arguments is not None and arguments.start_byte <= node.start_byte < arguments.end_byte:
                return _callee_name(current)
        current = current.parent
    return None


def _in_mock_call(node: Node) -> bool:
    """Inside the arguments (or as a keyword) of a call building a double."""
    return _mock_callee(node) in _MOCK_CALLEES


def _scope(node: Node) -> Node:
    """The function, lambda, class or module a name token belongs to; a
    definition's own name belongs to the scope around it."""
    current = node.parent
    if current is not None and current.type in ("function_definition", "class_definition"):
        if current.child_by_field_name("name") == node:
            current = current.parent
    while current is not None and current.type not in ("function_definition", "lambda", "class_definition", "module"):
        current = current.parent
    return current if current is not None else node


def _docstring(content: Node) -> bool:
    """A string that is a whole statement: it runs nothing and names nothing,
    unless it holds a doctest (``>>>``), which a test runner executes."""
    if b">>>" in content.text:
        return False
    string = content.parent
    statement = string.parent if string is not None else None
    return (
        string is not None
        and string.type == "string"
        and statement is not None
        and statement.type == "expression_statement"
        and len(statement.named_children) == 1
    )


def _binds(node: Node) -> bool:
    """Whether an identifier spelling a name declares one: a definition, a
    parameter, an assignment or loop target, an alias or a ``global``."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("function_definition", "class_definition"):
        return parent.child_by_field_name("name") == node
    if parent.type in ("parameters", "lambda_parameters", "list_splat_pattern", "dictionary_splat_pattern"):
        return True
    if parent.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
        return parent.child_by_field_name("value") != node and parent.type != "type"
    if parent.type in ("global_statement", "nonlocal_statement", "named_expression", "keyword_pattern"):
        return parent.type != "named_expression" or parent.child_by_field_name("name") == node
    if parent.type == "aliased_import":
        return parent.child_by_field_name("alias") == node
    return _store(node)


@dataclass
class Index:
    repo_dir: str = ""
    #: ``(path, start byte)`` of every declaration of the wrapper's name: a
    #: definition, a parameter, a target (an attribute written included).
    declarations: list[tuple[str, int]] = field(default_factory=list)
    #: ``(path, line, text)`` of every test double of the wrapper.
    doubles: list[tuple[str, int, str]] = field(default_factory=list)
    #: A file mentioning the name could not be read or parsed whole there.
    unreadable: bool = False
    parsed: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    synthetic: list[tuple[str, int, str]] = field(default_factory=list)
    #: A method's owner: its ``class`` statement and every other top-level
    #: binding of the name in its module, or attribute write of it anywhere.
    owners: list[tuple[str, int]] = field(default_factory=list)
    #: The value the flag keeps, set by the shared loop: the mark carries it.
    treatment: bool | None = None

    @classmethod
    def build(cls, repo_dir: str, wrapper: wi.Wrapper) -> Index:
        index = cls(repo_dir=repo_dir)
        word = _word(wrapper.name)
        names = [wrapper.name] + ([wrapper.owner] if wrapper.kind != "function" else [])
        for path in cross_file_keys.mention_files(repo_dir, names, exclude=()):
            text = _read(path)
            if text is None:
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            if path.endswith(EXTENSIONS):
                if word.search(text):
                    index.parsed.append(path)
                index._add(path, text, wrapper)
            elif word.search(text):
                index.unparsed.append(path)
        return index

    def _add(self, path: str, text: str, wrapper: wi.Wrapper) -> None:
        root = parse(text)
        name = wrapper.name
        word = _word(name)
        mocks = _MOCK_WORDS.search(text) is not None
        opaque = _OPAQUE.search(text) is not None or "__setattr__" in text
        home = str(Path(path).resolve()) == wrapper.path
        if root.has_error and any(n.type == "ERROR" and word.search(_text(n)) for n in cross_file_keys.walk(root)):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        for node in cross_file_keys.walk(root):
            if node.type == "string_content":
                spelled = _text(node)
                callee = _mock_callee(node)
                if word.search(spelled) and callee in _REBINDERS and not mocks:
                    # `setattr(obj, "name", ...)`: another value under the name.
                    self.declarations.append((path, node.start_byte))
                elif word.search(spelled) and callee is not None:
                    self.doubles.append((path, wi.line_of(node), wi.text_of(node.parent)))
                elif word.search(spelled) and opaque and not _docstring(node):
                    # A namespace written by a computed name can rebind it.
                    self.declarations.append((path, node.start_byte))
                elif (
                    wrapper.kind != "function"
                    and _word(wrapper.owner).search(spelled)
                    and callee is not None
                ):
                    # A patched class answers a mock for every member.
                    self.doubles.append((path, wi.line_of(node), wi.text_of(node.parent)))
                continue
            if node.type != "identifier":
                continue
            spelled = _text(node)
            if spelled == wrapper.owner and wrapper.kind != "function":
                if _in_mock_call(node) and not _binds(node):
                    self.doubles.append((path, wi.line_of(node), wi.text_of(node.parent or node)))
                elif (home and _binds(node) and _scope(node).type == "module") or (
                    _attribute_name(node) and _store(node.parent)
                ):
                    # The class, or anything rebinding its name after it.
                    self.owners.append((path, node.start_byte))
                continue
            if spelled != name:
                continue
            parent = node.parent
            label = parent is not None and parent.type == "keyword_argument" and parent.child_by_field_name("name") == node
            if label and _in_mock_call(node):
                self.doubles.append((path, wi.line_of(node), wi.text_of(parent)))
            elif parent is not None and parent.type == "attribute" and parent.child_by_field_name("attribute") == node:
                above = parent.parent
                configures = (
                    above is not None
                    and above.type == "attribute"
                    and above.child_by_field_name("object") == parent
                    and _MOCK_ATTRIBUTES.fullmatch(_text(above.child_by_field_name("attribute")))
                )
                if mocks and (configures or _store(parent)):
                    self.doubles.append((path, wi.line_of(node), wi.text_of(above or parent)))
                elif _store(parent):
                    self.declarations.append((path, node.start_byte))
            elif _binds(node) or (wrapper.kind != "function" and _scope(node).type == "class_definition"):
                # For a method, any name in a class body may be a member of
                # that name: an import, a match capture, a `type` alias.
                self.declarations.append((path, node.start_byte))


#: Words of a file that builds doubles.
_MOCK_WORDS = re.compile(r"\b(?:mock|Mock|MagicMock|AsyncMock|patch|mocker|monkeypatch|create_autospec)\b")


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | wi.Refused | None]:
    """The wrapper, or why it does not qualify. A double refuses first: its
    strings and keywords would otherwise read as declarations."""
    if index.doubles:
        path, line, text = index.doubles[0]
        return None, wi.Refused(wi.TEST_DOUBLE, path, line, text)
    if index.unreadable or len(index.declarations) != 1:
        return None, wi.NAME_NOT_UNIQUE
    if wrapper.kind != "function" and len(index.owners) != 1:
        return None, wi.NAME_NOT_UNIQUE
    return wrapper, None


# --- Call sites -----------------------------------------------------------------


def names_only(node: Node, kind: str = "method") -> bool:
    """A definition's own name, or a keyword argument's label."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("function_definition", "class_definition"):
        return parent.child_by_field_name("name") == node
    return parent.type == "keyword_argument" and parent.child_by_field_name("name") == node


def members(root: Node, name: str) -> list[Node]:
    """The top-level function or class-body method of ``name``, with its
    decorators. A method that is its class's only statement is left out:
    deleting it would leave a class with an empty body."""
    found: list[Node] = []
    for statement in root.named_children:
        candidate = _candidate(statement)
        if candidate is not None:
            if _text(candidate[1].child_by_field_name("name")) == name:
                found.append(statement)
            continue
        cls = statement.child_by_field_name("definition") if statement.type == "decorated_definition" else statement
        if cls is None or cls.type != "class_definition":
            continue
        body = _named(cls.child_by_field_name("body"))
        for member in body:
            candidate = _candidate(member)
            if candidate is not None and _text(candidate[1].child_by_field_name("name")) == name and len(body) > 1:
                found.append(member)
    return found


def member_end(member: Node) -> int:
    """Where a deleted member's span ends: through the blank lines after it
    when a sibling at its own indentation follows, so the sibling keeps the
    separation the member had (two blank lines at the top level, one in a
    class). Before a dedent (the last method of a class) the member's own
    end, which leaves the larger gap the dedented statement had."""
    root = member
    while root.parent is not None:
        root = root.parent
    data, base = root.text, root.start_byte
    end = data.find(b"\n", member.end_byte - base)
    if end == -1:
        return member.end_byte
    column = member.start_point[1]
    position = end + 1
    while position < len(data):
        line_end = data.find(b"\n", position)
        line = data[position : len(data) if line_end == -1 else line_end]
        if line.strip():
            indent = len(line) - len(line.lstrip(b" \t"))
            return base + position - 1 if indent == column else member.end_byte
        if line_end == -1:
            break
        position = line_end + 1
    return member.end_byte


def _argument_does_work(call: Node) -> bool:
    """An argument that calls, awaits, yields, assigns, unpacks or iterates:
    inlining the call would drop it."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None or arguments.type != "argument_list":
        return True
    return any(
        n.type
        in (
            "call", "await", "yield", "named_expression", "list_splat", "dictionary_splat",
            "list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression",
        )
        for n in cross_file_keys.walk(arguments)
    )


def _tokens(scope: Node, name: str) -> list[Node]:
    return [n for n in cross_file_keys.walk(scope) if n.type == "identifier" and _text(n) == name]


def _object_of(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "attribute" and parent.child_by_field_name("object") == node


def _label(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "keyword_argument" and parent.child_by_field_name("name") == node


def _called(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "call" and parent.child_by_field_name("function") == node


def _attribute_name(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "attribute" and parent.child_by_field_name("attribute") == node


def _passed(node: Node) -> bool:
    """A plain load passed on or returned: it rebinds nothing."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("argument_list", "return_statement"):
        return True
    return parent.type == "keyword_argument" and parent.child_by_field_name("value") == node


def _top_level(statement: Node) -> bool:
    return statement.parent is not None and statement.parent.type == "module"


@dataclass
class _File:
    path: str
    source: str
    root: Node
    #: ``_bindings`` of the wrapper's module (for a function) or of its
    #: owner class (for a method), top-level and unambiguous only.
    bindings: list
    opaque: bool


def _file(path: str, source: str, wrapper: wi.Wrapper, repo_dir: str) -> _File:
    from flag_cleanup import cross_file_python_proofs as proofs

    root = parse(source)
    decl = _declaration(wrapper)
    same = str(Path(path).resolve()) == wrapper.path
    bindings = [] if same else proofs._bindings(root, path, repo_dir, decl)
    return _File(path, source, root, bindings, _OPAQUE.search(source) is not None)


def _sole_binder(file: _File, name: str, home: bool, allowed: Callable[[Node], bool]) -> Node | None:
    """The one node binding module-level ``name`` in the file -- the
    definition in its home file, else the one top-level import -- when every
    other token spelling it is one ``allowed`` accepts; ``None`` otherwise."""
    star = any(c.type == "wildcard_import" for c in cross_file_keys.walk(file.root))
    if star or file.opaque:
        return None
    binders: list[Node] = []
    for token in _tokens(file.root, name):
        parent = token.parent
        if home and parent is not None and parent.type in ("function_definition", "class_definition"):
            statement = parent.parent if parent.parent is not None and parent.parent.type == "decorated_definition" else parent
            if parent.child_by_field_name("name") == token and _top_level(statement):
                binders.append(token)
                continue
        if not home and _in_import(token):
            binders.append(token)
            continue
        if not allowed(token):
            return None
    return binders[0] if len(binders) == 1 else None


def _in_import(token: Node) -> Node | None:
    current = token.parent
    while current is not None:
        if current.type in ("import_statement", "import_from_statement"):
            return current
        current = current.parent
    return None


def _function_reason(node: Node, call: Node, file: _File, wrapper: wi.Wrapper, home: bool) -> str | None:
    callee = call.child_by_field_name("function")
    if callee == node:
        # Bare: bound once at module level, by the definition or one import.
        if home:
            binder = _sole_binder(file, wrapper.name, True, lambda t: _called(t) or _attribute_name(t) or _label(t))
            return None if binder is not None else wi.RECEIVER_UNPROVEN
        bare = [b for b in file.bindings if b.kind == "bare" and b.local == wrapper.name and _top_level(b.statement)]
        if len(bare) != 1 or bare[0].ambiguous:
            return wi.IMPORT_UNRESOLVED
        binder = _sole_binder(file, wrapper.name, False, lambda t: _called(t) or _attribute_name(t) or _label(t))
        return None if binder is not None else wi.RECEIVER_UNPROVEN
    if home:
        return wi.RECEIVER_UNPROVEN
    receiver = callee.child_by_field_name("object")
    spelled = " ".join(_text(receiver).split())
    qualified = [b for b in file.bindings if b.kind == "qualified" and b.owner == spelled]
    if len(qualified) != 1 or qualified[0].ambiguous or not _top_level(qualified[0].statement):
        return wi.IMPORT_UNRESOLVED
    return _module_head_reason(file, qualified[0])


def _module_head_reason(file: _File, binding) -> str | None:
    """Every token spelling the import's local name is the import itself, a
    top-level ``import`` of the same package head, or the object of an
    attribute."""
    if file.opaque or any(c.type == "wildcard_import" for c in cross_file_keys.walk(file.root)):
        return wi.RECEIVER_UNPROVEN
    for token in _tokens(file.root, binding.local):
        statement = _in_import(token)
        if statement is not None:
            if statement != binding.statement and not _same_head(token, statement, binding):
                return wi.RECEIVER_UNPROVEN
            continue
        if not _object_of(token):
            return wi.RECEIVER_UNPROVEN
    return None


def _same_head(token: Node, statement: Node, binding) -> bool:
    """``import app.other`` beside ``import app.flags``: both bind ``app``
    to the same package, unaliased and at the top level."""
    dotted = token.parent
    return (
        binding.statement.type == "import_statement"
        and binding.name_node.type == "dotted_name"
        and statement.type == "import_statement"
        and _top_level(statement)
        and dotted is not None
        and dotted.type == "dotted_name"
        and dotted.parent == statement
        and dotted.named_children[0] == token
    )


def _enclosing_definition(node: Node) -> Node | None:
    current = node.parent
    while current is not None and current.type != "function_definition":
        current = current.parent
    return current


def _class_of(definition: Node) -> tuple[Node, list[str]] | None:
    """``(class_definition, decorators)`` for a method directly in a class body."""
    statement = definition.parent if definition.parent is not None and definition.parent.type == "decorated_definition" else definition
    block = statement.parent
    cls = block.parent if block is not None and block.type == "block" else None
    if cls is None or cls.type != "class_definition":
        return None
    return cls, _decorators(statement)


def _method_reason(node: Node, file: _File, wrapper: wi.Wrapper, home: bool, receiver: Node) -> str | None:
    if receiver.type != "identifier":
        return wi.RECEIVER_UNPROVEN
    spelled = _text(receiver)
    method = _enclosing_definition(node)
    owned = _class_of(method) if method is not None else None
    if (
        home
        and owned is not None
        and _text(owned[0].child_by_field_name("name")) == wrapper.owner
        and _top_level(owned[0].parent if owned[0].parent.type == "decorated_definition" else owned[0])
        and (first := first_parameter(method)) is not None
        and _text(first) == spelled
        and "staticmethod" not in owned[1]
    ):
        # `self.m()` in a method of the owner, or `cls.m()` in a classmethod.
        if "classmethod" in owned[1] and not wrapper.static:
            return wi.RECEIVER_UNPROVEN
        if file.opaque or not all(t == first or _object_of(t) or _called(t) or _passed(t) for t in _tokens(method, spelled)):
            return wi.RECEIVER_UNPROVEN
        return None
    if spelled != wrapper.owner and not any(b.local == spelled for b in file.bindings):
        return wi.RECEIVER_UNPROVEN
    if not wrapper.static:
        return wi.RECEIVER_UNPROVEN
    # `Owner.m()`: the class itself, or one top-level import of it.
    if not home:
        bare = [b for b in file.bindings if b.kind == "bare" and b.local == spelled and _top_level(b.statement)]
        if len(bare) != 1 or bare[0].ambiguous:
            return wi.IMPORT_UNRESOLVED
    binder = _sole_binder(file, spelled, home, lambda t: _object_of(t) or _called(t) or _passed(t) or _in_type(t))
    return None if binder is not None else wi.RECEIVER_UNPROVEN


def _in_type(node: Node) -> bool:
    current = node.parent
    while current is not None and current.type not in ("type", "block", "module"):
        current = current.parent
    return current is not None and current.type == "type"


def _self_documenting(node: Node) -> bool:
    """Inside an f-string field with ``=`` (``f"{on(u)=}"``), which prints
    its own source text: the mark would change what it prints."""
    current = node.parent
    while current is not None and current.type != "interpolation":
        current = current.parent
    return current is not None and any(c.type == "=" for c in current.children)


def _judge(path: str, node: Node, wrapper: wi.Wrapper, file: _File, home: bool, treatment: str) -> wi.Occurrence:
    parent = node.parent
    line = wi.line_of(node)
    if _self_documenting(node):
        return wi.Occurrence(path, line, wi.text_of(parent or node), None, wi.UNPARSED_MENTION)
    if parent is not None and parent.type == "attribute" and parent.child_by_field_name("attribute") == node:
        target, receiver = parent, parent.child_by_field_name("object")
    else:
        target, receiver = node, None
    if wrapper.kind == "property":
        if receiver is None or _called(target) or _store(target):
            return wi.Occurrence(path, line, wi.text_of(target), None, wi.RECEIVER_UNPROVEN)
        reason = _method_reason(node, file, wrapper, home, receiver)
        if reason is not None or wrapper.static:
            return wi.Occurrence(path, line, wi.text_of(target), None, reason or wi.RECEIVER_UNPROVEN)
        return wi.Occurrence(path, line, wi.text_of(target), (target.start_byte, target.end_byte), None, suffix=f"({treatment})")
    call = target.parent
    if call is None or call.type != "call" or call.child_by_field_name("function") != target:
        return wi.Occurrence(path, line, wi.text_of(target), None, wi.RECEIVER_UNPROVEN)
    if wrapper.kind == "function":
        reason = _function_reason(node, call, file, wrapper, home)
    else:
        reason = wi.RECEIVER_UNPROVEN if receiver is None else _method_reason(node, file, wrapper, home, receiver)
    if reason is None and _argument_does_work(call):
        reason = wi.ARGUMENT_HAS_CALL
    span = (call.start_byte, call.end_byte) if reason is None else None
    return wi.Occurrence(path, line, wi.text_of(call), span, reason, suffix=f"({treatment})" if span else "")


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    file = _file(path, source, wrapper, index.repo_dir)
    home = str(Path(path).resolve()) == wrapper.path
    treatment = "False" if index.treatment is False else "True"
    found: list[wi.Occurrence] = []
    for node in cross_file_keys.walk(file.root):
        if node.type != "identifier" or _text(node) != wrapper.name or names_only(node):
            continue
        statement = _in_import(node)
        if statement is not None:
            # `from <its module> import name` is not a call; it goes once its
            # calls fold, or it is a leftover. Any other import is a mention.
            if any(b.kind == "bare" and b.local == wrapper.name and b.statement == statement for b in file.bindings):
                continue
            found.append(wi.Occurrence(path, wi.line_of(node), wi.text_of(statement), None, wi.RECEIVER_UNPROVEN))
            continue
        found.append(_judge(path, node, wrapper, file, home, treatment))
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def string_mentions(index: Index, path: str, wrapper: wi.Wrapper) -> list[tuple[int, str]]:
    """String literals spelling the name, docstrings aside."""
    source = _read(path)
    if source is None:
        return []
    word = _word(wrapper.name)
    return [
        (wi.line_of(n), wi.text_of(n.parent))
        for n in cross_file_keys.walk(parse(source))
        if n.type == "string_content" and word.search(_text(n)) and not _docstring(n)
    ]


def _declaration(wrapper: wi.Wrapper) -> cross_file_keys.KeyDeclaration:
    """The wrapper as the cross-file key machinery spells a module member:
    a function is bound by its own name, a method by its class's."""
    return cross_file_keys.KeyDeclaration(
        path=wrapper.path,
        name=wrapper.name,
        owner=None if wrapper.kind == "function" else wrapper.owner,
        module=wrapper.package,
        kind=cross_file_keys.CONSTANT if wrapper.kind == "function" else cross_file_keys.ENUM_ENTRY,
    )


def imports(repo_dir: str, path: str, wrapper: wi.Wrapper) -> tuple[str, str] | None:
    """``(before, after)`` with each ``from <its module> import name`` nothing
    uses any more rewritten so the module still loads: the name is dropped
    from the list, or a statement left with no name becomes an import of the
    module itself (``import app.flags as _flags  # noqa: F401``), exactly as
    a deleted key constant's import is."""
    from flag_cleanup import cross_file_python_proofs as proofs
    from flag_cleanup import cross_file_python_rewrite

    source = _read(path)
    if source is None or wrapper.kind != "function":
        return None
    root = parse(source)
    decl = _declaration(wrapper)
    for binding in proofs._bindings(root, path, repo_dir, decl):
        if binding.kind == "bare" and _top_level(binding.statement):
            if proofs._rewrite_refusal(root, binding, path, repo_dir, (decl,)) is not None:
                # The private alias the rewrite would bind is taken, or the
                # line cannot carry its mark: the import stays, and keeps it.
                return None
    after = cross_file_python_rewrite.rewrite_stranded_imports(path, source, (decl,), repo_dir)
    return None if after == source else (source, after)


def leftovers(
    repo_dir: str, wrapper: wi.Wrapper, removable: frozenset[str] = frozenset()
) -> list[tuple[str, int, str]]:
    """Every mention of the name left after the fold: an identifier that does
    not only name something, a string that is not a docstring, or any word
    match in a file this tool does not parse. A file the run rewrote is read
    as :func:`imports` will leave it, so an import it rewrites does not count
    and one it cannot does."""
    found: list[tuple[str, int, str]] = []
    word = _word(wrapper.name)
    for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=()):
        source = _read(path)
        if source is None:
            found.append((path, 1, ""))
            continue
        if not path.endswith(EXTENSIONS):
            found += [
                (path, n, " ".join(line.split()))
                for n, line in enumerate(source.splitlines(), start=1)
                if word.search(line)
            ]
            continue
        resolved = str(Path(path).resolve())
        if resolved in removable and resolved != wrapper.path:
            rewritten = imports(repo_dir, path, wrapper)
            source = rewritten[1] if rewritten is not None else source
        root = parse(source)
        found += [
            (path, wi.line_of(n), wi.text_of(n.parent))
            for n in cross_file_keys.walk(root)
            if n.type == "string_content" and word.search(_text(n)) and not _docstring(n)
        ]
        for node in cross_file_keys.walk(root):
            if node.type == "identifier" and _text(node) == wrapper.name and not names_only(node):
                found.append((path, wi.line_of(node), wi.text_of(node.parent or node)))
    return found


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


wi.register(
    wi.Profile(
        language="python",
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
        inert_annotations=_INERT,
        string_mentions=string_mentions,
        leftovers=leftovers,
        imports=imports,
    )
)
