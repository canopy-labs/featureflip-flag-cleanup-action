"""Ruby profile for keyless wrappers (see :mod:`flag_cleanup.wrapper_inline`).

A wrapper is a method of a ``class`` or ``module`` -- an instance method, a
``def self.`` class method, or a module function (a ``def`` after a bare
``module_function``) -- whose whole body is one read ``rules/ruby.toml``
folds in this run::

    class Flags
      def new_checkout? = client.bool_variation("old-checkout", ctx, false)

      def self.on?
        CLIENT.bool_variation("old-checkout", {}, false)
      end
    end

The read is ``bool_variation`` (or a named accessor) with the key first, as a
literal or a same-file constant where Gate 2 let the const rules run, or
OpenFeature's ``fetch_boolean_value(flag_key: ...)``. Parameters are plain
ones and optional ones with a literal default, so a call's arity is known.

Ruby is dynamic, so the proof asks for little and refuses much:

* the method's name must be defined exactly once across the repository's
  Ruby, and its class or module's name declared exactly once (a class
  reopened anywhere is refused);
* a name spelled as a symbol or a string anywhere (``:new_checkout?``,
  ``new_checkout?:``, ``send(...)``, ``define_method``, ``attr_reader``,
  ``delegate``, ``helper_method``, an RSpec or Minitest stub) refuses the
  wrapper: any of them can define it, call it or stub it by name;
* a class or module that declares or inherits ``method_missing``, prepends a
  module, defines methods from computed names, is reopened through
  ``class_eval``, ``class << Owner`` or a refinement, or that a double stands
  in for (``instance_double(Flags)``, ``stub_const("Flags", ...)``) is refused;
* an instance method of a class nothing in the repository subclasses is
  called bare (``new_checkout?``) or through ``self`` inside an instance
  method of that class, never inside a block or a lambda, and a bare name that a
  local or a parameter of the enclosing method also binds is never a call;
* a class method or a module function is called through its class or
  module's name (``Flags.on?``, ``Admin::Flags.on?``), which the file's
  nesting resolves to it, or bare or through ``self`` inside one of the
  owner's own ``def self.`` methods (a class: when nothing subclasses it).

A wrapper of a gem (a ``.gemspec`` beside or above it) may be overridden by a
subclass in another repository, so its instance methods are refused
(``interface-published``) and only its class methods' calls through the
class's name are inlined; a public one is never deleted (``published``).
A call through a computed name (``send(name)``, ``public_send(*args)``) is
not seen, as Python's ``getattr`` with a computed string is not.

An ERB template calls a Rails helper bare (``<% if new_checkout? %>``), and
those calls are inlined too, under a narrow rule, because a view resolves a
bare name through every helper module Rails mixes into it:

* the wrapper is an instance method of a module in ``app/helpers/`` of a Rails
  application (a ``config/application.rb`` beside that ``app/``), whose name
  is the one Rails derives from its file (``admin/flags_helper.rb`` holds
  ``Admin::FlagsHelper``);
* nothing in the repository spells ``include_all_helpers``,
  ``clear_helpers`` or ``helpers_path``, which change which helpers a view
  includes;
* the template is under that application's ``app/views/``. A template
  anywhere else (a view component, a mailer engine, a gem) is not a caller
  this proves, and keeps the wrapper as before;
* the call is ``m?``, ``m!``, ``m(...)`` or ``self.m?``, outside every block
  (a block may be evaluated against another object) and every ``def``,
  inside one tag, where its value is used: a condition, an operand, an
  argument, a value, or the whole of an output tag (``<%= m? %>``). A bare
  ``m`` is never a call there: ``render`` may pass a local of that name.

The template's code is judged as the Ruby it is, through the tags' code
joined in source order, and each proven call is mapped back to its bytes in
the file. A symbol or a string spelling the name in that code refuses the
wrapper, as in a Ruby file; a mention in the markup or in a comment tag keeps
it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, key_const, mock_stubs, ruby_owner_reach, source_index, syntax
from flag_cleanup import wrapper_inline as wi

logger = logging.getLogger(__name__)

EXTENSIONS = (".rb", ".rake")
#: Files holding Ruby that the listing keeps only on request: views, project
#: files and scripts, and files with no suffix (``Rakefile``, ``bin/setup``).
_EXTRA_SUFFIXES = source_index.RUBY_SUFFIXES | source_index.NO_SUFFIX
#: Read callees the seed rules fold.
_READ_CALLEES = frozenset({"bool_variation"})
_OPENFEATURE = "fetch_boolean_value"
_DEFS = ("method", "singleton_method")
#: Methods a class or module can be given whose name it never spells, or
#: through which it answers a call of a name it does not define.
_MAGIC = frozenset({"method_missing", "respond_to_missing?"})
#: Hooks through which a class or module reaches the classes that inherit or
#: include it, and can give them methods.
_HOOKS = frozenset({"inherited", "included", "extended", "prepended", "method_added"})
#: Calls that define or rename methods from their arguments.
_DEFINERS = frozenset({
    "define_method", "define_singleton_method", "alias_method", "attr", "attr_reader", "attr_accessor",
    "attr_writer", "delegate", "def_delegator", "def_delegators", "def_instance_delegator",
    "def_instance_delegators", "delegate_missing_to", "instance_variable_get",
})
#: Methods called ON a class or module that reopen it, change what its
#: methods are, or call one by a name nothing here can read.
_OPENING = frozenset({
    "class_eval", "class_exec", "module_eval", "module_exec", "instance_eval", "instance_exec",
    "define_method", "define_singleton_method", "prepend", "alias_method", "remove_method",
    "undef_method", "send", "__send__", "public_send", "singleton_class", "instance_method",
    "method", "public_method", "singleton_method", "include", "extend", "prepend_features",
    "attr_accessor", "attr_reader", "attr_writer", "private", "protected", "public",
    "private_class_method", "public_class_method", "module_function",
})
#: RSpec, Minitest and Mocha calls that create a double or stub a method.
_DOUBLES = frozenset({
    "receive", "receive_messages", "receive_message_chain", "have_received", "allow", "expect",
    "allow_any_instance_of", "expect_any_instance_of", "instance_double", "class_double",
    "object_double", "instance_spy", "class_spy", "object_spy", "double", "spy", "stub_const",
    "stub", "stubs", "expects", "any_instance", "mock", "stub_any_instance", "unstub",
})
#: Where an identifier names a local or a parameter rather than reads one.
_BINDERS = frozenset({
    "method_parameters", "block_parameters", "lambda_parameters", "optional_parameter",
    "keyword_parameter", "splat_parameter", "hash_splat_parameter", "block_parameter",
    "destructured_parameter", "left_assignment_list", "rest_assignment",
    "destructured_left_assignment", "exception_variable", "for", "block_local_variables",
})
#: Statement lists whose last statement is a body's value.
_VALUE_BODIES = {"body_statement": _DEFS + ("do_block",), "block_body": ("block",)}
#: Values an argument can be without the call doing work that inlining would drop.
_INERT_ARGUMENTS = frozenset({
    "integer", "float", "rational", "complex", "true", "false", "nil", "self", "simple_symbol",
    "instance_variable", "class_variable", "global_variable", "constant", "scope_resolution",
})
#: Settings through which an application changes which helper modules its
#: views include.
_HELPER_SETTINGS = ("include_all_helpers", "clear_helpers", "helpers_path")
#: Nodes of a template's code between which a call may run on another
#: object, or not at all.
_TEMPLATE_BREAKS = frozenset({"block", "do_block", "lambda", *_DEFS, "class", "module", "singleton_class"})


def parse(source: str) -> Node:
    return syntax._parse(source, "ruby")


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _read(path: str) -> str | None:
    try:
        return mock_stubs._read_exact(path)
    except (OSError, UnicodeDecodeError):
        return None


def _walk(node: Node) -> Iterable[Node]:
    return cross_file_keys.walk(node)


def _named(node: Node | None) -> list[Node]:
    return [] if node is None else [c for c in node.named_children if c.type != "comment"]


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w@$:]){re.escape(name)}(?![\w?!=])")


# --- Reads and discovery ----------------------------------------------------------


def _plain_string(node: Node) -> str | None:
    """The text of a string literal with no interpolation."""
    if node.type != "string":
        return None
    contents = _named(node)
    if not contents:
        return ""
    return _text(contents[0]) if len(contents) == 1 and contents[0].type == "string_content" else None


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
        {c.name for c in key_const.key_consts(root, source.encode("utf-8"), "ruby", flag_key)}
        if const_ok
        else set()
    )
    callees = _READ_CALLEES | set(accessors)

    def key_matches(value: Node | None) -> bool:
        if value is None:
            return False
        if value.type == "string":
            return _plain_string(value) == flag_key
        return value.type == "constant" and (loose or _text(value) in consts)

    def is_read(expression: Node) -> bool:
        if expression.type != "call":
            return False
        receiver = expression.child_by_field_name("receiver")
        if receiver is not None and _text(expression.child_by_field_name("operator")) != ".":
            return False  # `&.` reads nil when the receiver is
        method = _text(expression.child_by_field_name("method"))
        arguments = _named(expression.child_by_field_name("arguments"))
        if expression.child_by_field_name("block") is not None or not arguments:
            return False
        if method in callees:
            return key_matches(arguments[0])
        if method == _OPENFEATURE:
            return any(
                a.type == "pair"
                and _text(a.child_by_field_name("key")) == "flag_key"
                and a.child_by_field_name("key").type == "hash_key_symbol"
                and key_matches(a.child_by_field_name("value"))
                for a in arguments
            )
        return False

    return is_read


def _single_read(method: Node, is_read: Callable[[Node], bool]) -> bool:
    """Whether the method's body is ``read``, ``return read`` or an endless
    ``= read``, with no ``rescue`` or ``ensure``."""
    body = method.child_by_field_name("body")
    if body is None:
        return False
    if body.type != "body_statement":
        return is_read(body)
    statements = _named(body)
    if len(statements) != 1:
        return False
    only = statements[0]
    if only.type == "return":
        values = [v for c in _named(only) for v in (_named(c) if c.type == "argument_list" else [c])]
        return len(values) == 1 and is_read(values[0])
    return is_read(only)


_LITERAL_DEFAULTS = frozenset({"integer", "float", "true", "false", "nil", "simple_symbol", "string"})


def _arity(method: Node) -> tuple[int, int | None] | None:
    """``(required, max)`` for plain parameters and optional ones with a
    literal default; ``None`` for anything else (a splat, a keyword, a
    block), which this profile does not count."""
    parameters = method.child_by_field_name("parameters")
    required = optional = 0
    for parameter in _named(parameters):
        if parameter.type == "identifier" and not optional:
            required += 1
        elif parameter.type == "optional_parameter":
            value = parameter.child_by_field_name("value")
            if value is None or value.type not in _LITERAL_DEFAULTS or (
                value.type == "string" and _plain_string(value) is None
            ):
                return None
            optional += 1
        else:
            return None
    return required, required + optional


def _sorbet_sig(method: Node) -> tuple[str, ...]:
    """``("sig",)`` when a Sorbet ``sig`` stands above the method: it
    describes the next method, so the two go together or not at all."""
    previous = method.prev_named_sibling
    while previous is not None and previous.type == "comment":
        previous = previous.prev_named_sibling
    if previous is not None and previous.type in ("call", "identifier"):
        head = previous.child_by_field_name("method") if previous.type == "call" else previous
        if _text(head) == "sig":
            return ("sig",)
    return ()


def _scope_names(node: Node) -> list[str] | None:
    """The full names of the classes and modules lexically enclosing
    ``node``, innermost first; ``None`` when one of them sits in a block or a
    method (``Class.new do``, a class defined at run time)."""
    names: list[str] = []
    chain: list[Node] = []
    parent = node.parent
    while parent is not None:
        if parent.type in ("class", "module"):
            chain.append(parent)
        elif parent.type in ("block", "do_block", "lambda", *_DEFS, "singleton_class") and chain:
            return None
        parent = parent.parent
    full = ""
    for scope in reversed(chain):
        written = "".join(_text(scope.child_by_field_name("name")).split())
        if written.startswith("::"):
            full = written[2:]  # `class ::Flags` is top-level wherever it is written
        else:
            full = f"{full}::{written}" if full else written
        names.append(full)
    return list(reversed(names))


def _owner_full(declaration: Node) -> str | None:
    """``Admin::Flags`` for a class or module, or ``None`` when it is defined
    where nothing names it statically."""
    scopes = _scope_names(declaration.child_by_field_name("name") or declaration)
    return scopes[0] if scopes else None


def discover(path: str, source: str, is_read: Callable[[Node], bool]) -> list[wi.Wrapper]:
    root = parse(source)
    found: list[wi.Wrapper] = []
    for owner in _walk(root):
        if owner.type not in ("class", "module"):
            continue
        full = _owner_full(owner)
        body = owner.child_by_field_name("body")
        if full is None or body is None or owner.child_by_field_name("name").type not in (
            "constant", "scope_resolution"
        ):
            continue
        simple = full.rsplit("::", 1)[-1]
        private = module_function = False
        for member in body.named_children:
            if member.type == "identifier":
                word = _text(member)
                if word in ("private", "protected", "public"):
                    # A bare visibility keyword also ends module-function mode.
                    private = word != "public"
                    module_function = False
                elif word == "module_function" and owner.type == "module":
                    module_function = True
                continue
            if member.type not in _DEFS:
                continue
            if member.type == "singleton_method" and _text(member.child_by_field_name("object")) != "self":
                continue
            name = member.child_by_field_name("name")
            arity = _arity(member)
            if name is None or name.type != "identifier" or arity is None or not _single_read(member, is_read):
                continue
            static = member.type == "singleton_method" or module_function
            found.append(
                wi.Wrapper(
                    path=path,
                    language="ruby",
                    name=_text(name),
                    owner=simple,
                    package=full,
                    line=wi.line_of(member),
                    start=member.start_byte,
                    end=member.end_byte,
                    static=static,
                    annotations=_sorbet_sig(member),
                    kind="singleton" if member.type == "singleton_method" else "method",
                    arity=arity,
                    # A module function's instance copy is private; what
                    # matters here is whether ``Owner.m`` is callable.
                    private=private and member.type == "method" and not module_function,
                )
            )
    return found


def names_only(node: Node, kind: str = "method") -> bool:
    """A method's own name in its ``def``, or a local or a parameter being
    bound, which lives apart from methods. A READ of such a local is still a
    mention: it is spelled like a call."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _DEFS:
        return parent.child_by_field_name("name") == node
    return _declares(node)


def members(root: Node, name: str) -> list[Node]:
    return [
        n for n in _walk(root)
        if n.type in _DEFS and _text(n.child_by_field_name("name")) == name
    ]


# --- ERB templates --------------------------------------------------------------


@dataclass(frozen=True)
class _View:
    """A template's Ruby: the code of its tags joined with newlines in source
    order, parsed, with the way back from each of its bytes into the file.

    Its offsets are its own, so nothing reads one as a file's: every span and
    line handed out goes through :meth:`span` and :meth:`line`."""

    root: Node
    #: ``(view start, file start, length, printed)`` per tag, in order;
    #: ``printed`` for an output tag (``<%=``, ``<%==``).
    segments: tuple[tuple[int, int, int, bool], ...]
    data: bytes

    def _segment(self, node: Node) -> tuple[int, int, int, bool] | None:
        for segment in self.segments:
            start, _, length, _ = segment
            if start <= node.start_byte and node.end_byte <= start + length:
                return segment
        return None

    def span(self, node: Node) -> tuple[int, int] | None:
        """The node's bytes in the file, or ``None`` when it spans two tags."""
        segment = self._segment(node)
        if segment is None:
            return None
        start, file_start, _, _ = segment
        return file_start + node.start_byte - start, file_start + node.end_byte - start

    def line(self, node: Node) -> int:
        segment = self._segment(node) or next(
            (s for s in reversed(self.segments) if s[0] <= node.start_byte), None
        )
        if segment is None:
            return 1
        offset = segment[1] + max(0, min(node.start_byte - segment[0], segment[2]))
        return self.data[:offset].count(b"\n") + 1

    def printed(self, statement: Node) -> bool:
        """Whether a top-level statement is the whole code of an output tag,
        whose value the template prints."""
        segment = self._segment(statement)
        if segment is None or not segment[3]:
            return False
        start, _, length, _ = segment
        return not [
            other for other in _named(self.root)
            if other != statement and other.start_byte < start + length and other.end_byte > start
        ]


def _view(source: str) -> _View:
    template = syntax._parse(source, "erb")
    data = source.encode("utf-8")
    codes = sorted((n for n in syntax._walk(template) if n.type == "code"), key=lambda n: n.start_byte)
    parts: list[bytes] = []
    segments: list[tuple[int, int, int, bool]] = []
    offset = 0
    for code in codes:
        if parts:
            offset += 1  # the newline joining two tags
        opening = code.parent.children[0] if code.parent is not None and code.parent.children else None
        printed = (
            code.parent is not None
            and code.parent.type == "output_directive"
            # The grammar calls `<%-` an output directive too; it prints nothing.
            and opening is not None
            and _text(opening).startswith("<%=")
        )
        segments.append((offset, code.start_byte, code.end_byte - code.start_byte, printed))
        parts.append(data[code.start_byte : code.end_byte])
        offset += code.end_byte - code.start_byte
    return _View(parse(b"\n".join(parts).decode("utf-8")), tuple(segments), data)


def _markup_lines(source: str, name: str) -> list[tuple[int, str]]:
    """Lines where the name is spelled outside every tag's code: in the markup
    or a comment tag, where nothing here can tell what reads it."""
    template = syntax._parse(source, "erb")
    data = bytearray(source.encode("utf-8"))
    for code in (n for n in syntax._walk(template) if n.type == "code"):
        data[code.start_byte : code.end_byte] = bytes(
            b if b == 0x0A else 0x20 for b in data[code.start_byte : code.end_byte]
        )
    word = _word(name)
    return [
        (number, " ".join(line.split()))
        for number, line in enumerate(data.decode("utf-8").splitlines(), start=1)
        if word.search(line)
    ]


def _views_dir(wrapper: wi.Wrapper, owner: Node | None) -> Path | None:
    """The ``app/views/`` whose templates call the wrapper as a Rails helper,
    or ``None`` when it is not one: an instance method of a module in
    ``app/helpers/`` of a Rails application, named as Rails derives the name
    from its file, which is how Rails includes it in every view."""
    if wrapper.kind != "method" or wrapper.static or owner is None or owner.type != "module":
        return None
    path = Path(wrapper.path)
    for helpers in path.parents:
        if helpers.name != "helpers" or helpers.parent.name != "app":
            continue
        root = helpers.parent.parent
        if not (root / "config" / "application.rb").is_file():
            return None
        parts = path.relative_to(helpers).with_suffix("").parts
        expected = "::".join("".join(w[:1].upper() + w[1:] for w in part.split("_")) for part in parts)
        return root / "app" / "views" if expected == wrapper.package else None
    return None


def _template_judge(path: str, node: Node, wrapper: wi.Wrapper, view: _View) -> wi.Occurrence:
    def occurrence(shown: Node, reason: str | None, span=None) -> wi.Occurrence:
        return wi.Occurrence(path, view.line(node), wi.text_of(shown), span, reason, suffix="()" if span else "")

    parent = node.parent
    if parent is None or parent.type != "call" or parent.child_by_field_name("method") != node:
        # A bare `m` may be a local that `render` passed the template.
        return occurrence(parent or node, wi.RECEIVER_UNPROVEN)
    call = parent
    receiver = call.child_by_field_name("receiver")
    if receiver is not None and (receiver.type != "self" or _text(call.child_by_field_name("operator")) != "."):
        return occurrence(call, wi.RECEIVER_UNPROVEN)
    ancestors = list(_ancestors(call))
    if any(a.type in ("interpolation", "heredoc_body") for a in ancestors):
        return occurrence(call, wi.UNPARSED_MENTION)
    if any(a.type in _TEMPLATE_BREAKS for a in ancestors):
        return occurrence(call, wi.RECEIVER_UNPROVEN)
    span = view.span(call)
    if span is None or not _value_used(call, view.printed):
        return occurrence(call, wi.RECEIVER_UNPROVEN)
    reason = _arguments_reason(call, wrapper)
    if reason is not None:
        return occurrence(call, reason)
    return occurrence(call, None, span)


def _template_occurrences(path: str, source: str, wrapper: wi.Wrapper) -> list[wi.Occurrence]:
    view = _view(source)
    if view.root.has_error:
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    found = [
        _template_judge(path, node, wrapper, view)
        for node in _walk(view.root)
        if node.type == "identifier" and _text(node) == wrapper.name and not names_only(node)
    ]
    found += [
        wi.Occurrence(path, line, text, None, wi.UNPARSED_MENTION)
        for line, text in _markup_lines(source, wrapper.name)
    ]
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


# --- The repository index -----------------------------------------------------


def _is_rb(path: str) -> bool:
    return path.endswith(EXTENSIONS)


def _ruby_text(path: str, text: str) -> bool:
    """A file of Ruby this profile parses for declarations: ``.rb``, ``.rake``
    and every other Ruby file but a template."""
    if _is_rb(path):
        return True
    return (
        not path.endswith(".erb")
        and not ruby_owner_reach.unparsed(path)
        and ruby_owner_reach.is_ruby(path, text)
    )


def _symbol_text(node: Node) -> str | None:
    """The name a symbol spells, for every way Ruby writes one."""
    if node.type == "simple_symbol":
        return _text(node).removeprefix(":")
    if node.type in ("hash_key_symbol", "bare_symbol"):
        return _text(node)
    if node.type == "delimited_symbol":
        contents = _named(node)
        return _text(contents[0]) if len(contents) == 1 and contents[0].type == "string_content" else None
    return None


def _call_method(node: Node) -> str:
    return _text(node.child_by_field_name("method")) if node.type == "call" else ""


def _in_double(node: Node) -> bool:
    """Inside the arguments, or the receiver chain, of a stub or a double."""
    parent = node.parent
    while parent is not None and parent.type not in ("body_statement", "block_body", "program", *_DEFS):
        if parent.type == "call" and _call_method(parent) in _DOUBLES:
            return True
        parent = parent.parent
    return False


def _last_segment(node: Node | None) -> str:
    if node is None:
        return ""
    if node.type == "scope_resolution":
        return _text(node.child_by_field_name("name"))
    return _text(node) if node.type == "constant" else ""


@dataclass
class Index:
    repo_dir: str = ""
    #: ``(path, line, text)`` of every definition of the wrapper's name.
    declarations: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` of every declaration of the owner's name.
    owners: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` for each symbol or string spelling the name.
    named: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` for each double of the owner or stub of the name.
    doubles: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` for each use that reopens or rewires the owner.
    opened: list[tuple[str, int, str]] = field(default_factory=list)
    #: The full names of the owner's in-repository subclasses.
    subclasses: list[Node] = field(default_factory=list)
    #: ``(path, node)`` of every class or module declaration seen, by simple name.
    classes: dict[str, list[tuple[str, Node]]] = field(default_factory=dict)
    unreadable: bool = False
    gem: bool = False
    #: Something in the repository subclasses the owner.
    subclassed: bool = False
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
        index = cls(repo_dir=repo_dir, gem=_gem(wrapper.path))
        word = _word(wrapper.name)
        views = index._views_dir(wrapper)
        for path in cross_file_keys.mention_files(
            repo_dir, [wrapper.name, wrapper.owner], exclude=(), extra_suffixes=_EXTRA_SUFFIXES
        ):
            text = _read(path)
            if text is None:
                logger.info("cannot read %s, so no keyless wrapper qualifies", path)
                index.unreadable = True
                continue
            if path.endswith(".erb"):
                index._template(path, text, wrapper, views)
                continue
            if not _ruby_text(path, text):
                if not Path(path).suffix and not ruby_owner_reach.is_ruby(path, text):
                    if word.search(text):
                        index.unparsed.append(path)
                    continue
                if word.search(text):
                    (index.foreign if path.endswith(".erb") else index.unparsed).append(path)
                continue
            root = index.file(path, text)[1]
            if word.search(text):
                (index.parsed if _is_rb(path) else index.unparsed).append(path)
            index._scan(path, root, wrapper)
        home = index.file(wrapper.path)
        owner = _owner_declaration(home[1], wrapper) if home is not None else None
        if owner is None:
            index.unreadable = True
            return index
        index._scan_owner(wrapper.path, owner, wrapper)
        for path, subclass in index.subclasses_of(wrapper):
            index.subclassed = True
            index._scan_owner(path, subclass, wrapper)
        for path, ancestor in index.superclasses_of(owner):
            index._scan_owner(path, ancestor, wrapper, magic_only=True)
        return index

    def _views_dir(self, wrapper: wi.Wrapper) -> Path | None:
        """Where templates may call the wrapper as a helper (see
        :func:`_views_dir`), unless something in the repository changes which
        helpers a view includes."""
        home = self.file(wrapper.path)
        views = _views_dir(wrapper, _owner_declaration(home[1], wrapper) if home is not None else None)
        if views is None:
            return None
        settings = cross_file_keys.mention_files(
            self.repo_dir, list(_HELPER_SETTINGS), exclude=(), extra_suffixes=_EXTRA_SUFFIXES
        )
        for path in settings:
            text = _read(path)
            if text is None or any(re.search(rf"\b{s}\b", text) for s in _HELPER_SETTINGS):
                logger.info("%s may change which helpers a view includes, so no template call is proven", path)
                return None
        return views

    def _template(self, path: str, text: str, wrapper: wi.Wrapper, views: Path | None) -> None:
        """A template calling the wrapper as a helper is parsed through its
        code and scanned like a Ruby file; any other one mentioning the name
        keeps the wrapper (``other-language``)."""
        view = _view(text) if views is not None and Path(path).resolve().is_relative_to(views) else None
        if view is None or view.root.has_error:
            if wi._foreign_lines(path, wrapper.name):
                self.foreign.append(path)
            return
        self.parsed.append(path)
        self._scan(path, view.root, wrapper, view.line)

    def _scan(
        self, path: str, root: Node, wrapper: wi.Wrapper, line_of: Callable[[Node], int] = wi.line_of
    ) -> None:
        name, owner = wrapper.name, wrapper.owner
        word = _word(name)
        if root.has_error and any(
            n.type in ("ERROR", "MISSING") or n.is_missing for n in _walk(root) if word.search(_text(n))
        ):
            logger.info("cannot parse the declarations of %s, so no keyless wrapper qualifies", path)
            self.unreadable = True
        for node in _walk(root):
            kind = node.type
            if kind in _DEFS and _text(node.child_by_field_name("name")) == name:
                self.declarations.append((path, line_of(node), _text(node).split("\n", 1)[0].strip()))
            elif kind in ("alias", "undef") and any(
                _text(c) == name or _symbol_text(c) == name for c in node.named_children
            ):
                self.declarations.append((path, line_of(node), wi.text_of(node)))
            elif _symbol_text(node) in (name, owner):
                into = self.doubles if _in_double(node) else self.named
                into.append((path, line_of(node), wi.text_of(node.parent or node)))
            elif kind in ("string_content", "heredoc_content") and (
                word.search(_text(node)) or _owner_string(_text(node), wrapper)
            ):
                into = self.doubles if _in_double(node) else self.named
                into.append((path, line_of(node), wi.text_of(node.parent or node)))
            elif kind in ("class", "module"):
                written = node.child_by_field_name("name")
                if _last_segment(written) == owner:
                    self.owners.append((path, line_of(node), wi.text_of(written)))
                self.classes.setdefault(_last_segment(written), []).append((path, node))
            elif kind in ("assignment", "operator_assignment") and _last_segment(
                node.child_by_field_name("left")
            ) == owner:
                self.owners.append((path, line_of(node), wi.text_of(node)))
            elif kind == "singleton_class" and _last_segment(node.child_by_field_name("value")) == owner:
                self.opened.append((path, line_of(node), wi.text_of(node).split("\n", 1)[0]))
            elif kind == "call":
                method = _call_method(node)
                receiver = node.child_by_field_name("receiver")
                arguments = _named(node.child_by_field_name("arguments"))
                if _last_segment(receiver) == owner and method in _OPENING:
                    self.opened.append((path, line_of(node), wi.text_of(node).split("\n", 1)[0]))
                elif method == "refine" and arguments and _last_segment(arguments[0]) == owner:
                    self.opened.append((path, line_of(node), wi.text_of(node).split("\n", 1)[0]))
                elif method in _DOUBLES and (
                    _last_segment(receiver) == owner or any(_last_segment(a) == owner for a in arguments)
                ):
                    self.doubles.append((path, line_of(node), wi.text_of(node).split("\n", 1)[0]))

    def subclasses_of(self, wrapper: wi.Wrapper) -> list[tuple[str, Node]]:
        found: list[tuple[str, Node]] = []
        for declarations in self.classes.values():
            for path, node in declarations:
                superclass = node.child_by_field_name("superclass")
                if superclass is not None and any(
                    _last_segment(n) == wrapper.owner for n in _walk(superclass)
                ):
                    found.append((path, node))
        return found

    def superclasses_of(self, owner: Node) -> list[tuple[str, Node]]:
        """The in-repository classes the owner inherits from, as far as their
        simple names find exactly one declaration each."""
        found: list[tuple[str, Node]] = []
        current = owner
        for _ in range(10):
            superclass = current.child_by_field_name("superclass")
            written = _named(superclass)
            name = _last_segment(written[0]) if written else ""
            declarations = self.classes.get(name, []) if name else []
            if len(declarations) != 1:
                if name and not declarations:
                    declarations = _declarations_named(self, name)
                if len(declarations) != 1:
                    return found
            found.append(declarations[0])
            current = declarations[0][1]
        return found

    def _scan_owner(self, path: str, owner: Node, wrapper: wi.Wrapper, magic_only: bool = False) -> None:
        """What in the owner's own body, a subclass's or an ancestor's can give
        an instance a method the index cannot see."""
        body = owner.child_by_field_name("body")
        for node in _walk(body) if body is not None else ():
            if node.type in _DEFS and _text(node.child_by_field_name("name")) in _MAGIC | _HOOKS:
                self.opened.append((path, wi.line_of(node), _text(node).split("\n", 1)[0].strip()))
            if magic_only or node.type not in ("call", "identifier"):
                continue
            # Every identifier is read as a method name, so a call's own
            # name is judged whatever its receiver
            # (`singleton_class.prepend(X)`, `k.define_method(...)`).
            method = _call_method(node) if node.type == "call" else _text(node)
            arguments = _named(node.child_by_field_name("arguments")) if node.type == "call" else []
            literal = bool(arguments) and _symbol_text(arguments[0]) is not None or (
                bool(arguments) and _plain_string(arguments[0]) is not None
            )
            if method == "prepend" or (method in _DEFINERS and not literal) or method in (
                "class_eval", "class_exec", "module_eval", "instance_eval", "method_added",
            ):
                self.opened.append((path, wi.line_of(node), wi.text_of(node).split("\n", 1)[0]))


def _declarations_named(index: Index, name: str) -> list[tuple[str, Node]]:
    """Every class declaration of ``name`` in the repository's Ruby, for an
    ancestor the wrapper's own scan did not reach."""
    found: list[tuple[str, Node]] = []
    for path in cross_file_keys.mention_files(index.repo_dir, [name], exclude=(), extra_suffixes=_EXTRA_SUFFIXES):
        text = _read(path)
        if text is None or not _ruby_text(path, text):
            continue
        root = index.file(path, text)[1]
        found += [
            (path, n) for n in _walk(root)
            if n.type in ("class", "module") and _last_segment(n.child_by_field_name("name")) == name
        ]
    return found


def _owner_string(text: str, wrapper: wi.Wrapper) -> bool:
    """A string that names the owner (``stub_const("Flags", ...)``,
    ``"Admin::Flags".constantize``)."""
    stripped = text.strip().removeprefix("::")
    return stripped == wrapper.owner or stripped.endswith("::" + wrapper.owner)


def _owner_declaration(root: Node, wrapper: wi.Wrapper) -> Node | None:
    for node in _walk(root):
        if node.type in ("class", "module") and _owner_full(node) == wrapper.package:
            return node
    return None


def _gem(path: str) -> bool:
    """Whether ``path`` belongs to a gem: a ``.gemspec`` beside it or in a
    directory above it, up to the checkout's root."""
    for directory in Path(path).parents:
        if any(directory.glob("*.gemspec")):
            return True
        if (directory / ".git").exists():
            break
    return False


def qualify(wrapper: wi.Wrapper, index: Index) -> tuple[wi.Wrapper | None, str | wi.Refused | None]:
    """The wrapper, or why it does not qualify."""
    if index.unreadable:
        return None, wi.NAME_NOT_UNIQUE
    if index.doubles:
        return None, wi.Refused(wi.TEST_DOUBLE, *index.doubles[0])
    if index.named:
        return None, wi.Refused(wi.UNPARSED_MENTION, *index.named[0])
    if index.opened:
        return None, wi.Refused(wi.OVERRIDABLE, *index.opened[0])
    if len(index.owners) != 1:
        return None, wi.Refused(wi.NAME_NOT_UNIQUE, *index.owners[-1]) if index.owners else wi.NAME_NOT_UNIQUE
    if len(index.declarations) != 1:
        others = [d for d in index.declarations if (d[0], d[1]) != (wrapper.path, wrapper.line)]
        return None, wi.Refused(wi.NAME_NOT_UNIQUE, *others[0]) if others else wi.NAME_NOT_UNIQUE
    if index.gem and wrapper.kind != "singleton":
        # Another repository may subclass the owner, or include the module,
        # and define its own method of the name.
        return None, wi.INTERFACE_PUBLISHED
    return wrapper, None


def keep(wrapper: wi.Wrapper) -> str | None:
    """A public wrapper of a gem may be called by another repository."""
    return wi.PUBLISHED if not wrapper.private and _gem(wrapper.path) else None


# --- Call sites ---------------------------------------------------------------


def _member_of(definition: Node, owner: Node) -> bool:
    """Whether a ``def`` is written directly in the owner's body."""
    parent = definition.parent
    return parent is not None and parent.type == "body_statement" and parent.parent == owner


def _self_is_owner(call: Node, wrapper: wi.Wrapper, path: str, index: Index) -> bool:
    """Whether ``self`` at ``call`` is the owner: an instance of it inside
    one of its instance methods, the owner itself inside one of its class
    methods, as the wrapper needs, with no block between."""
    if path != wrapper.path or index.gem or index.subclassed:
        # ``self`` may be an instance of a subclass, or the subclass itself,
        # and what it mixes in comes before the owner.
        return False
    cached = index.file(path)
    owner = _owner_declaration(cached[1], wrapper) if cached is not None else None
    if owner is None:
        return False
    singleton = wrapper.kind == "singleton"
    parent = call.parent
    while parent is not None:
        if parent.type in ("block", "do_block", "lambda"):
            # Whatever receives the block may `instance_exec` it, even a
            # `select` or an `each` (Sequel's virtual rows do).
            return False
        elif parent.type == "method":
            # Never in a module: its instance methods run on whatever includes
            # it, which may define the name itself.
            return not singleton and owner.type == "class" and _member_of(parent, owner)
        elif parent.type == "singleton_method":
            return (
                wrapper.static
                and _text(parent.child_by_field_name("object")) == "self"
                and _member_of(parent, owner)
            )
        elif parent.type in ("class", "module", "singleton_class", "program"):
            return False
        parent = parent.parent
    return False


def _constant_reaches(receiver: Node, wrapper: wi.Wrapper) -> bool:
    """Whether a written constant (``Flags``, ``Admin::Flags``, ``::Flags``)
    resolves to the owner where it is written: through the lexical nesting
    there, and nothing else in the repository declaring the owner's name."""
    written = "".join(_text(receiver).split())
    if written.startswith("::"):
        return written[2:] == wrapper.package
    nesting = _scope_names(receiver)
    if nesting is None:
        nesting = []
    return wrapper.package in [f"{scope}::{written}" for scope in nesting] + [written]


def _scope_of(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type in (*_DEFS, "class", "module", "program", "singleton_class"):
            return parent
        parent = parent.parent
    return None


def _declares(node: Node) -> bool:
    """Whether an identifier is the name a parameter or an assignment binds:
    never a parameter's default, a pattern, a guard or a value, where the same
    spelling may be a call."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("method_parameters", "block_parameters", "lambda_parameters", "left_assignment_list",
                       "destructured_left_assignment", "block_local_variables"):
        return True
    if parent.type in ("optional_parameter", "keyword_parameter", "splat_parameter", "hash_splat_parameter",
                       "block_parameter", "rest_assignment"):
        return parent.child_by_field_name("name") == node or (
            parent.type == "rest_assignment" and parent.named_children[:1] == [node]
        )
    if parent.type in ("assignment", "operator_assignment"):
        return parent.child_by_field_name("left") == node
    return parent.type == "exception_variable"


def _binding(node: Node) -> bool:
    """Whether an identifier MAY bind a local or a parameter: wide, since it
    only ever refuses a bare call."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _BINDERS or _declares(node):
        return True
    ancestor = parent
    while ancestor is not None and ancestor.type not in (*_DEFS, "class", "module", "program"):
        if "pattern" in ancestor.type or ancestor.type == "in_clause":
            return True
        ancestor = ancestor.parent
    return False


def _binds(scope: Node | None, name: str) -> bool:
    """Whether anything in ``scope`` binds ``name`` as a local or a parameter,
    which makes a bare ``name`` a read of it rather than a call."""
    return scope is not None and any(
        node.type == "identifier" and _text(node) == name and _binding(node) for node in _walk(scope)
    )


def _local(node: Node, scope: Node | None) -> bool:
    """An identifier argument the enclosing method binds as a local or a
    parameter BEFORE it, as Ruby's parser decides: a read, not a call."""
    name = _text(node)
    return scope is not None and any(
        n.type == "identifier"
        and _text(n) == name
        and n.end_byte <= node.start_byte
        and _declares(n)
        and _block_reaches(n, node, scope)
        for n in _walk(scope)
    )


def _block_reaches(declaration: Node, node: Node, scope: Node) -> bool:
    """Whether every block or lambda between ``declaration`` and ``scope``
    holds ``node`` as well: a block's parameters and locals end with it."""
    parent = declaration.parent
    while parent is not None and parent != scope:
        if parent.type in ("block", "do_block", "lambda") and not (
            parent.start_byte <= node.start_byte and node.end_byte <= parent.end_byte
        ):
            return False
        parent = parent.parent
    return True


def _value_used(expression: Node, printed: Callable[[Node], bool] | None = None) -> bool:
    """Whether the expression's value is used where a literal can stand: a
    condition, an operand, a value, an argument, or the last statement of a
    method's or a block's body. In a template's code, ``printed`` says
    whether a top-level statement is the whole of an output tag, whose value
    is printed."""
    parent = expression.parent
    if parent is None:
        return False
    kind = parent.type
    if kind == "program":
        return printed is not None and printed(expression)
    if kind in ("if", "unless", "while", "until", "elsif", "if_modifier", "unless_modifier",
                "while_modifier", "until_modifier"):
        return parent.child_by_field_name("condition") == expression
    if kind == "unary":
        return _text(parent.child_by_field_name("operator")) in ("!", "not") and _value_used(parent, printed)
    if kind == "parenthesized_statements":
        return len(_named(parent)) == 1 and _value_used(parent, printed)
    if kind == "conditional":
        return parent.child_by_field_name("condition") == expression or _value_used(parent, printed)
    if kind == "binary":
        return _value_used(parent, printed)
    if kind in ("argument_list", "pair", "array", "return", "next", "break"):
        return kind != "pair" or parent.child_by_field_name("value") == expression
    if kind in ("assignment", "operator_assignment"):
        return parent.child_by_field_name("right") == expression
    if kind in _DEFS:
        return parent.child_by_field_name("body") == expression
    holders = _VALUE_BODIES.get(kind)
    if holders is not None and parent.parent is not None and parent.parent.type in holders:
        statements = _named(parent)
        return bool(statements) and statements[-1] == expression
    return False


def _occurrence(path: str, node: Node, shown: Node, reason: str | None, span=None) -> wi.Occurrence:
    return wi.Occurrence(path, wi.line_of(node), wi.text_of(shown), span, reason, suffix="()" if span else "")


def _judge(path: str, node: Node, wrapper: wi.Wrapper, index: Index) -> wi.Occurrence:
    parent = node.parent
    if parent is not None and parent.type == "call" and parent.child_by_field_name("method") == node:
        call, bare = parent, False
    elif parent is not None and parent.type != "call":
        call, bare = node, True
    else:
        return _occurrence(path, node, parent or node, wi.RECEIVER_UNPROVEN)
    if any(a.type in ("interpolation", "heredoc_body") for a in _ancestors(call)):
        return _occurrence(path, node, call, wi.UNPARSED_MENTION)
    if not _value_used(call):
        return _occurrence(path, node, call, wi.RECEIVER_UNPROVEN)
    receiver = call.child_by_field_name("receiver") if not bare else None
    if receiver is not None and _text(call.child_by_field_name("operator")) != ".":
        return _occurrence(path, node, call, wi.RECEIVER_UNPROVEN)
    if receiver is None or receiver.type == "self":
        proven = _self_is_owner(call, wrapper, path, index)
        if proven and bare and _binds(_scope_of(call), wrapper.name):
            proven = False
        reason = None if proven else wi.RECEIVER_UNPROVEN
    elif receiver.type in ("constant", "scope_resolution"):
        if not wrapper.static or wrapper.private:
            reason = wi.RECEIVER_UNPROVEN
        else:
            reason = None if _constant_reaches(receiver, wrapper) else wi.IMPORT_UNRESOLVED
    else:
        reason = wi.RECEIVER_UNPROVEN
    if reason is None:
        reason = _arguments_reason(call, wrapper)
    if reason is not None:
        return _occurrence(path, node, call, reason)
    return _occurrence(path, node, call, None, (call.start_byte, call.end_byte))


def _arguments_reason(call: Node, wrapper: wi.Wrapper) -> str | None:
    """Why the call's arguments keep it, or ``None``: an argument that does
    work (a call, an assignment, a block) would be dropped with it, and a
    count the method does not take raises where the literal would not."""
    if call.type == "call" and call.child_by_field_name("block") is not None:
        return wi.ARGUMENT_HAS_CALL
    arguments = _named(call.child_by_field_name("arguments")) if call.type == "call" else []
    scope = _scope_of(call)
    for argument in arguments:
        if argument.type == "string" and _plain_string(argument) is not None:
            continue
        if argument.type in _INERT_ARGUMENTS or (argument.type == "identifier" and _local(argument, scope)):
            continue
        return wi.ARGUMENT_HAS_CALL
    required, most = wrapper.arity or (0, 0)
    if not required <= len(arguments) <= (most if most is not None else len(arguments)):
        return wi.RECEIVER_UNPROVEN
    return None


def _ancestors(node: Node) -> Iterable[Node]:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def occurrences(path: str, source: str, wrapper: wi.Wrapper, index: Index) -> list[wi.Occurrence]:
    if path.endswith(".erb"):
        # Only a template the index admitted as a helper's caller is here.
        return _template_occurrences(path, source, wrapper)
    cached = index.file(path, source)
    if cached is None:
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    root = cached[1]
    if root.has_error:
        # The engine will not rewrite a file it cannot parse, so a mark there
        # would survive.
        return [wi.Occurrence(path, 1, "", None, wi.RECEIVER_UNPROVEN)]
    found = [
        _judge(path, node, wrapper, index)
        for node in _walk(root)
        if node.type == "identifier" and _text(node) == wrapper.name and not names_only(node)
    ]
    return sorted(found, key=lambda o: (o.line, o.span or (0, 0), o.text))


def string_mentions(index: Index, path: str, wrapper: wi.Wrapper) -> list[tuple[int, str]]:
    """None: a string or a symbol spelling the name refuses the wrapper at
    qualification."""
    return []


def leftovers(
    repo_dir: str, wrapper: wi.Wrapper, removable: frozenset[str] = frozenset()
) -> list[tuple[str, int, str]]:
    """Every mention of the wrapper's name still in the repository, read
    fresh: any word match in a file that is not Ruby this profile parses, and
    in one that is, each identifier that is not a ``def``'s own name and
    each symbol or string spelling it."""
    found: list[tuple[str, int, str]] = []
    word = _word(wrapper.name)
    for path in cross_file_keys.mention_files(repo_dir, [wrapper.name], exclude=(), extra_suffixes=_EXTRA_SUFFIXES):
        source = _read(path)
        if source is None:
            found.append((path, 1, ""))
            continue
        if not _ruby_text(path, source):
            found += [
                (path, number, " ".join(line.split()))
                for number, line in enumerate(source.splitlines(), start=1)
                if word.search(line)
            ]
            continue
        root = parse(source)
        for node in _walk(root):
            if (
                (node.type == "identifier" and _text(node) == wrapper.name and not names_only(node))
                or _symbol_text(node) == wrapper.name
                or (node.type in ("string_content", "heredoc_content") and word.search(_text(node)))
            ):
                found.append((path, wi.line_of(node), wi.text_of(node.parent or node)))
    return found


def _index(repo_dir: str, wrapper: wi.Wrapper) -> Index:
    return Index.build(repo_dir, wrapper)


wi.register(
    wi.Profile(
        language="ruby",
        extensions=EXTENSIONS,
        parse=parse,
        read_test=read_test,
        discover=discover,
        occurrences=occurrences,
        index=_index,
        qualify=qualify,
        strings=frozenset({"string", "heredoc_body"}),
        foreign_extensions=(),
        # A template calling a helper is rewritten by this run, under the ERB
        # grammar; see `_template_occurrences`.
        siblings=("erb",),
        names_only=names_only,
        members=members,
        keep=keep,
        inert_annotations=frozenset(),
        string_mentions=string_mentions,
        leftovers=leftovers,
    )
)
