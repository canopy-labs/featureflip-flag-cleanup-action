"""Ruby profile for cross-file key constants.

A declaration is a constant assigned exactly the key (a plain string, or one
with ``.freeze``) at the top level of a file, or directly in the body of a
``class`` or ``module`` nested only in other classes and modules
(:func:`flag_cleanup.key_const._ruby_string_content`). Its owner is the full
lexical path of the class or module holding it (``App::Flags``, stored dotted,
``App.Flags``), and a top-level constant has none. Ruby has one namespace, so
the module of every declaration is empty.

Ruby resolves a constant at run time, through the reader's lexical scopes
(``Module.nesting``), then the ancestors of the innermost one, then ``Object``.
So a read is proven only when no class, module or constant anywhere in the
checkout could stand for the name it starts with (:func:`ruby_index`), and in
two shapes, each the whole first argument of a ``bool_variation`` call (or of
an accessor you named) called with ``.`` or with no receiver:

* ``Owner::NAME``: the owner spelled as its full path (``App::Flags``, or
  ``::App::Flags``), or as the tail of it inside a scope that holds the rest
  (``Flags::NAME`` inside ``module App`` for ``App::Flags``). For a relative
  spelling, every class, module, constant or ``autoload`` of the checkout
  whose name is the spelling's first segment has to be the one the owner path
  means: ``Flags::NAME`` inside ``module Admin`` reads ``Admin::Flags`` if one
  exists, and inside a class including a module that declares ``Flags``, that
  one.
* ``NAME``: inside the owner reopened in another file (the nearest enclosing
  ``class``/``module`` is the owner's full path, with no ``class << self`` in
  between), or anywhere for a top-level constant, and nothing else in the
  checkout declares a constant of that name.

Every other spelling of the name in a file holding Ruby is refused and keeps
the declaration: ``include Flags`` then a bare ``NAME``, ``Sub::NAME``
through a subclass, the name in a string or a symbol (``const_get(:NAME)``),
an assignment of it (``Flags::NAME = ...``), and any mention in a view, a
project file or a script this tool does not parse.

A Ruby constant is not constant: another file can assign it, or set it by a
computed name. So a kept declaration keeps every read of it
(:attr:`~flag_cleanup.cross_file_keys.Profile.readers_need_deletable`), and
the owner reached whole anywhere (``Flags.constants``, ``Flags.const_get(n)``,
``register(Flags)``, a reopened ``Flags`` listing its own ``constants``) keeps
every key constant it owns: that half is
:mod:`flag_cleanup.ruby_owner_reach`'s, asked of every segment of the owner's
path (:func:`module_escapes`).

A constant declared in a gem (a directory holding a ``.gemspec``) is
published: code outside the repository may read it. It is kept whenever any
other file in the checkout spells it, and then its reads stay too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const, ruby_owner_reach, source_index, syntax

#: The suffixes the runner treats as Ruby and ERB. Spelled out rather than
#: imported from the runner, which imports the shared module at load.
RUBY_EXTENSIONS: tuple[str, ...] = (".rb", ".rake")
ERB_EXTENSIONS: tuple[str, ...] = (".erb",)

#: Files that can hold Ruby but that no profile parses: views, project files,
#: and (with no suffix) ``Rakefile`` and shebang scripts. One that spells a
#: key constant's name keeps its declaration.
UNPARSED_SUFFIXES = frozenset(source_index.RUBY_SUFFIXES | source_index.NO_SUFFIX | {".yml", ".yaml"})

#: Configuration that Rails, Sidekiq and others run through ERB before
#: reading it: Ruby inside ``<% %>`` tags. One holding a tag is judged as an
#: unparsed template.
_ERB_CONFIG_SUFFIXES = (".yml", ".yaml")

_SCOPES = frozenset({"class", "module"})
#: What may stand between a declaration and the file's top: a class or module
#: body and nothing else (not a method, a block, a condition or ``class <<``).
_DECLARATION_PATH = frozenset({"program", "body_statement", "class", "module"})

_READ_CALLEES = frozenset({"bool_variation"})


def _text(node: Node | None) -> str:
    return "" if node is None else (node.text or b"").decode("utf-8", "replace")


def _field(node: Node) -> str | None:
    parent = node.parent
    if parent is None:
        return None
    for index, child in enumerate(parent.children):
        if child.id == node.id:
            return parent.field_name_for_child(index)
    return None


def _code(path: str, source: str) -> str:
    """The Ruby of ``path``: an ERB template's code view, else the text."""
    return syntax._code_view(source, "erb") if path.endswith(ERB_EXTENSIONS) else source


def _parse(path: str, source: str) -> tuple[Node, str]:
    code = _code(path, source)
    return syntax._parse(code, "ruby"), code


def _spelled(node: Node | None) -> tuple[bool, tuple[str, ...]] | None:
    """``(absolute, segments)`` for a constant path (``A``, ``A::B``, ``::A``),
    or None when a segment is not a constant (``obj::B``, ``foo()::B``)."""
    if node is None:
        return None
    if node.type == "constant":
        return False, (_text(node),)
    if node.type != "scope_resolution":
        return None
    name = node.child_by_field_name("name")
    scope = node.child_by_field_name("scope")
    if name is None or name.type != "constant":
        return None
    if scope is None:
        return True, (_text(name),)
    inner = _spelled(scope)
    if inner is None:
        return None
    return inner[0], (*inner[1], _text(name))


def _scope_path(node: Node, cache: dict[int, tuple[str, ...] | None]) -> tuple[str, ...] | None:
    """The full path a ``class``/``module`` node declares, or None when its
    name or an enclosing one is not a plain constant path."""
    if node.id in cache:
        return cache[node.id]
    spelled = _spelled(node.child_by_field_name("name"))
    result: tuple[str, ...] | None
    if spelled is None:
        result = None
    else:
        absolute, segments = spelled
        outer = _enclosing_scope(node)
        if absolute or outer is None:
            result = segments
        else:
            prefix = _scope_path(outer, cache)
            result = None if prefix is None else prefix + segments
    cache[node.id] = result
    return result


def _enclosing_scope(node: Node) -> Node | None:
    current = node.parent
    while current is not None:
        if current.type in _SCOPES:
            return current
        current = current.parent
    return None


def _crefs(node: Node, cache: dict[int, tuple[str, ...] | None]) -> list[tuple[str, ...]] | None:
    """The lexical scopes at ``node``, innermost first (``Module.nesting``):
    each enclosing class or module's full path. ``class A::B`` contributes
    ``A::B`` alone, as in Ruby. None when one cannot be named or a
    ``class << x`` stands in between."""
    found: list[tuple[str, ...]] = []
    current = node.parent
    while current is not None:
        if current.type == "singleton_class":
            return None
        if current.type in _SCOPES:
            path = _scope_path(current, cache)
            if path is None:
                return None
            found.append(path)
        current = current.parent
    return found


def _declared_at(assignment: Node) -> bool:
    """Whether a constant assignment sits at the top of the file or directly
    in a class or module body, with nothing else around it."""
    current = assignment.parent
    while current is not None:
        if current.type not in _DECLARATION_PATH:
            return False
        current = current.parent
    return True


def _owner(path: tuple[str, ...] | None) -> str | None:
    return ".".join(path) if path else None


def _owner_path(decl: xk.KeyDeclaration) -> tuple[str, ...]:
    return tuple(decl.owner.split(".")) if decl.owner else ()


#: Per declaring file, by resolved path, the names :func:`module_escapes`
#: asks :mod:`~flag_cleanup.ruby_owner_reach` about: every class or module
#: around any constant holding the key there, offered or not, and None for
#: one at the top level. Kept here because that hook is handed only the path.
_OWNERS: dict[str, set[str | None]] = {}
#: Per declaring file, the key constants it holds that :func:`declarations`
#: does not offer (inside ``class << self``, a block or a condition), with
#: their lines.
_UNOFFERED: dict[str, list[tuple[str, int]]] = {}
#: Per declaring file, every other mention there of an offered constant's
#: name, with its line: anything but its own declaration and a bare read
#: the in-file rules remove (``bool_variation(NAME, ...)``). A second
#: assignment, ``const_set(:NAME, ...)``, ``private_constant :NAME``, a
#: mutation (``NAME.replace(...)``) or a qualified read the in-file rules
#: leave all make the declared value, or the deletion, unsafe to trust from
#: another file.
_HOME_MENTIONS: dict[str, list[int]] = {}
#: :func:`ruby_owner_reach.family` per ``(repo_dir, owner)``, for one
#: resolution: cleared whenever a resolution starts reading declarations.
_FAMILIES: dict[tuple[str, str], frozenset[str]] = {}


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    if not path.endswith(RUBY_EXTENSIONS):
        return []  # a template's constants are its own
    root, code = _parse(path, source)
    if root.has_error:
        return []
    raw = code.encode("utf-8")
    _FAMILIES.clear()
    cache: dict[int, tuple[str, ...] | None] = {}
    found: list[xk.KeyDeclaration] = []
    asked: set[str | None] = set()
    unoffered: list[tuple[str, int]] = []
    declared: set[int] = set()
    for node in xk.walk(root):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "constant":
            continue
        if key_const._ruby_string_content(right, raw) != flag_key:
            continue
        around = _around(node)
        asked |= set(around) if around else {None}
        scope = _enclosing_scope(node)
        owner = _scope_path(scope, cache) if scope is not None else ()
        if owner is None or not _declared_at(node):
            unoffered.append((_text(left), node.start_point[0] + 1))
            continue
        found.append(xk.KeyDeclaration(path, _text(left), _owner(owner), ""))
        declared.add(left.start_byte)
    _OWNERS[path] = asked
    _UNOFFERED[path] = unoffered
    _HOME_MENTIONS[path] = _home_mentions(root, raw, found, declared)
    return found


def _home_mentions(
    root: Node, raw: bytes, offered: list[xk.KeyDeclaration], declared: set[int]
) -> list[int]:
    if not offered:
        return []
    comments = _comment_spans(root)
    allowed: set[int] = set(declared)
    names = {d.name for d in offered}
    for node in xk.walk(root):
        if node.type != "constant" or _text(node) not in names or node.start_byte in declared:
            continue
        if node.parent is not None and node.parent.type == "scope_resolution":
            continue  # `Flags::NAME` in its own file: the in-file rules leave it
        call = _read_call(node, frozenset())
        if call is not None and _dot_call(call):
            allowed.add(node.start_byte)
    lines: list[int] = []
    for decl in offered:
        token = re.compile(xk.mention_pattern([decl.name]).pattern.encode())
        for hit in token.finditer(raw):
            at = hit.start()
            if at in allowed or any(start <= at < end for start, end in comments):
                continue
            lines.append(raw.count(b"\n", 0, at) + 1)
    return sorted(set(lines))


def _around(node: Node) -> list[str]:
    """Every name on the paths of the classes and modules around ``node``,
    and the constant a ``Struct.new do`` block is assigned to: what reaching
    any of them whole can reach a constant declared at ``node``."""
    names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in _SCOPES:
            spelled = _spelled(current.child_by_field_name("name"))
            if spelled is not None:
                names.extend(spelled[1])
        elif current.type == "singleton_class":
            spelled = _spelled(current.child_by_field_name("value"))
            if spelled is not None:
                names.extend(spelled[1])
        elif current.type == "assignment":
            spelled = _spelled(current.child_by_field_name("left"))
            if spelled is not None:
                names.extend(spelled[1])
        current = current.parent
    return names


def _assigned(node: Node, cache) -> tuple[str, ...] | None:
    """The full path a constant assignment (or ``autoload :Name``) binds, or
    None when it binds nothing nameable here."""
    if node.type == "assignment" or node.type == "operator_assignment":
        left = node.child_by_field_name("left")
        spelled = _spelled(left)
        if spelled is None:
            return None
        absolute, segments = spelled
        if absolute or len(segments) > 1:
            # `Flags::NAME = ...` or `::NAME = ...`: the spelling, from the top.
            # A relative one may also mean a nested scope: counted twice.
            return segments
        scope = _enclosing_scope(node)
        prefix = _scope_path(scope, cache) if scope is not None else ()
        return None if prefix is None else prefix + segments
    if node.type == "call" and _text(node.child_by_field_name("method")) == "autoload":
        arguments = node.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
        if first is None or first.type != "simple_symbol":
            return None
        scope = _enclosing_scope(node)
        prefix = _scope_path(scope, cache) if scope is not None else ()
        return None if prefix is None else prefix + (_text(first).lstrip(":"),)
    return None


def _bound_paths(root: Node) -> list[tuple[str, ...]]:
    """Every constant path the file binds: classes and modules, constant
    assignments and ``autoload``s. A relative ``A::B = x`` inside a scope is
    recorded from the top and from the scope."""
    cache: dict[int, tuple[str, ...] | None] = {}
    found: list[tuple[str, ...]] = []
    for node in xk.walk(root):
        if node.type in _SCOPES:
            path = _scope_path(node, cache)
            if path is None:
                # `class foo::Bar`: its last segment is bound somewhere.
                name = node.child_by_field_name("name")
                last = name.child_by_field_name("name") if name is not None and name.type == "scope_resolution" else name
                if last is not None and last.type == "constant":
                    found.append(("?", _text(last)))
            else:
                # `class Admin::Flags::Beta` binds `Admin::Flags` too: Zeitwerk
                # makes a namespace of a directory with no file declaring it.
                found.extend(path[:end] for end in range(1, len(path) + 1))
            continue
        if node.type in _CODE_TEXT:
            # Ruby in a string a run can evaluate (`module_eval <<~RUBY`):
            # a class, module or constant it binds is not read, so its name
            # stands for an unknown path.
            found.extend(("?", name) for name in _bound_in_text(_text(node)))
            continue
        path = _assigned(node, cache)
        if path is None:
            continue
        found.append(path)
        if node.type != "call":
            spelled = _spelled(node.child_by_field_name("left"))
            scope = _enclosing_scope(node)
            if spelled is not None and not spelled[0] and len(spelled[1]) > 1 and scope is not None:
                prefix = _scope_path(scope, cache)
                if prefix:
                    found.append(prefix + spelled[1])
    return found


#: String-like nodes whose text may be Ruby handed to an ``eval``.
_CODE_TEXT = frozenset({"string_content", "heredoc_content"})

_BINDING_TEXT = re.compile(
    r"(?:\b(?:class|module)\s+(?:::)?(?:[A-Z]\w*::)*([A-Z]\w*))"
    r"|(?:(?<![\w:.])(?:[A-Z]\w*::)*([A-Z]\w*)\s*(?:\|\||&&)?=(?![=~>]))"
)


def _bound_in_text(text: str) -> set[str]:
    """The names a string of Ruby would bind if evaluated: ``class X``,
    ``module X``, ``X = ...``."""
    return {m.group(1) or m.group(2) for m in _BINDING_TEXT.finditer(text)}


def _declared(path: str, source: str) -> list[tuple[str, ...]]:
    root, _code_text = _parse(path, source)
    if root.has_error:
        raise xk.DeclarationsUnreadable(path)
    return _bound_paths(root)


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every constant the file assigns, as ``("", owner, name)``. Class and
    module names are not constants here: a reopened module is the same one."""
    root, _code_text = _parse(path, source)
    if root.has_error:
        raise xk.DeclarationsUnreadable(path)
    cache: dict[int, tuple[str, ...] | None] = {}
    names: set[xk.QualifiedName] = set()
    for node in xk.walk(root):
        if node.type not in ("assignment", "operator_assignment"):
            continue
        bound = _assigned(node, cache)
        if bound is None:
            continue
        names.add(("", _owner(bound[:-1]), bound[-1]))
        # A relative `Flags::NAME = x` inside `module App` may bind either.
        spelled = _spelled(node.child_by_field_name("left"))
        scope = _enclosing_scope(node)
        if spelled is not None and not spelled[0] and len(spelled[1]) > 1 and scope is not None:
            prefix = _scope_path(scope, cache)
            if prefix:
                full = prefix + spelled[1]
                names.add(("", _owner(full[:-1]), full[-1]))
    return names


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """A constant ASSIGNED where an owner's path is (``Flags = PaidFlags``,
    ``App = Other``), which can make ``Flags::NAME`` read another module.
    A reopened ``class``/``module`` is not one: it is the same module, and
    a write or reflection inside it is refused on its own."""
    root, _code_text = _parse(path, source)
    if root.has_error:
        raise xk.DeclarationsUnreadable(path)
    cache: dict[int, tuple[str, ...] | None] = {}
    found: set[tuple[str, str]] = set()
    for node in xk.walk(root):
        if node.type in ("assignment", "operator_assignment"):
            bound = _assigned(node, cache)
            if bound is not None:
                found.add(("", ".".join(bound)))
    return found


# --- the checkout's constant paths ---------------------------------------------


@dataclass(frozen=True)
class _Share:
    paths: frozenset[tuple[str, ...]]
    #: Every capitalised word of a file that does not parse whole: it might
    #: bind any of them.
    words: frozenset[str]


@dataclass(frozen=True)
class RubyIndex:
    by_last: dict[str, frozenset[tuple[str, ...]]]
    unparsed_words: frozenset[str]
    unreadable: bool

    def only(self, name: str, path: tuple[str, ...]) -> bool:
        """Whether everything the checkout binds as ``name`` is ``path``."""
        if self.unreadable or name in self.unparsed_words:
            return False
        return self.by_last.get(name, frozenset({path})) == frozenset({path})


_WORDS = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9_]*")


def _share(code: str) -> tuple[_Share, bool]:
    root = syntax._parse(code, "ruby")
    if root.has_error:
        return _Share(frozenset(), frozenset(_WORDS.findall(code))), False
    return _Share(frozenset(_bound_paths(root)), frozenset()), True


def _combine(shares: list[tuple[str, _Share]], _incomplete: bool, unreadable: bool) -> RubyIndex:
    by_last: dict[str, set[tuple[str, ...]]] = {}
    words: set[str] = set()
    for _path, share in shares:
        words |= share.words
        for path in share.paths:
            by_last.setdefault(path[-1], set()).add(path)
    return RubyIndex({k: frozenset(v) for k, v in by_last.items()}, frozenset(words), unreadable)


_ruby_index = xk.CheckoutIndex("ruby", RUBY_EXTENSIONS, _share, _combine)
_erb_index = xk.CheckoutIndex(
    "erb", ERB_EXTENSIONS, lambda text: _share(syntax._code_view(text, "erb")), _combine
)


def ruby_index(repo_dir: str) -> RubyIndex:
    """Every constant path the checkout's Ruby and ERB files bind, by last
    segment. A file that does not parse binds every capitalised word in it."""
    first, second = _ruby_index(repo_dir), _erb_index(repo_dir)
    by_last = dict(first.by_last)
    for name, paths in second.by_last.items():
        by_last[name] = by_last.get(name, frozenset()) | paths
    return RubyIndex(
        by_last,
        first.unparsed_words | second.unparsed_words,
        first.unreadable or second.unreadable,
    )


# --- reads -------------------------------------------------------------------


def _read_call(value: Node, accessors: frozenset[str]) -> Node | None:
    """The flag read ``value`` is the whole first argument of, or None."""
    arguments = value.parent
    if arguments is None or arguments.type != "argument_list" or not arguments.named_children:
        return None
    if arguments.named_children[0].id != value.id:
        return None
    call = arguments.parent
    if call is None or call.type != "call" or _field(arguments) != "arguments":
        return None
    method = _text(call.child_by_field_name("method"))
    if method not in _READ_CALLEES and method not in accessors:
        return None
    return call


def _dot_call(call: Node) -> bool:
    """A call the reader rules match: no receiver, or one taken with ``.``
    (never ``&.``, whose nil the fold would replace)."""
    if call.child_by_field_name("receiver") is None:
        return True
    return _text(call.child_by_field_name("operator")) == "."


def _judge(
    constant: Node,
    decl: xk.KeyDeclaration,
    accessors: frozenset[str],
    index,
    cache: dict[int, tuple[str, ...] | None],
) -> xk.KeyReference | str:
    """A proven read of ``decl`` at ``constant`` (a ``constant`` node spelling
    its name), or the reason it is not one."""
    value = constant
    parent = constant.parent
    if parent is not None and parent.type == "scope_resolution":
        if _field(constant) != "name":
            return xk.NOT_A_READ  # `NAME::Inner`
        value = parent
    call = _read_call(value, accessors)
    if call is None:
        return xk.NOT_A_READ
    if not _dot_call(call):
        return xk.UNSUPPORTED_FORM
    owner = _owner_path(decl)
    if value.type == "scope_resolution":
        spelled = _spelled(value.child_by_field_name("scope")) if value.child_by_field_name("scope") else None
        if spelled is None or not owner:
            return xk.UNSUPPORTED_FORM  # `obj::NAME`, `::NAME`, `Object::NAME`
        absolute, segments = spelled
        if absolute:
            if segments != owner:
                return xk.UNSUPPORTED_FORM
        else:
            k = len(segments)
            if k > len(owner) or owner[len(owner) - k:] != segments:
                return xk.UNSUPPORTED_FORM
            if k < len(owner):
                crefs = _crefs(constant, cache)
                if crefs is None or owner[: len(owner) - k] not in crefs:
                    return xk.UNSUPPORTED_FORM
            # The first segment resolves through the reader's scopes and
            # their ancestors before the top: nothing else may bind it.
            if not index().only(segments[0], owner[: len(owner) - k + 1]):
                return xk.NAME_NOT_UNIQUE
        return xk.KeyReference("", _text(value.child_by_field_name("scope")), decl.name)
    # A bare `NAME`.
    crefs = _crefs(constant, cache)
    if crefs is None:
        return xk.UNSUPPORTED_FORM
    if owner and (not crefs or crefs[0] != owner):
        return xk.UNSUPPORTED_FORM  # outside the reopened owner: `include`, a subclass
    if not index().only(decl.name, owner + (decl.name,)):
        return xk.NAME_NOT_UNIQUE
    return xk.KeyReference("", None, decl.name)


def _comment_spans(root: Node) -> list[tuple[int, int]]:
    return [(n.start_byte, n.end_byte) for n in xk.walk(root) if n.type == "comment"]


def _line_at(code: bytes, offset: int) -> int:
    return code.count(b"\n", 0, offset)


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    root, code = _parse(path, source)
    erb = path.endswith(ERB_EXTENSIONS)
    lines = source.splitlines()

    def refusal_at(offset: int | None, name: str, reason: str) -> xk.Refusal:
        if erb or offset is None:
            # The code view's lines are not the template's.
            line, text = xk._line_of(path, name)
            return xk.Refusal(path, line, reason, text)
        line = _line_at(raw, offset)
        return xk.Refusal(path, line + 1, reason, lines[line].strip() if line < len(lines) else "")

    raw = code.encode("utf-8")
    if root.has_error:
        refused = [
            refusal_at(None, d.name, xk.UNSUPPORTED_FORM)
            for d in declarations
            if xk.mention_pattern([d.name]).search(code)
        ]
        return [], refused[:1]
    built: list = []

    def index():
        if not built:
            built.append(ruby_index(repo_dir))
        return built[0]

    comments = _comment_spans(root)
    cache: dict[int, tuple[str, ...] | None] = {}
    constants = [n for n in xk.walk(root) if n.type == "constant"]
    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []
    for decl in declarations:
        judged: set[int] = set()
        for node in constants:
            if _text(node) != decl.name:
                continue
            judged.add(node.start_byte)
            verdict = _judge(node, decl, accessors, index, cache)
            if isinstance(verdict, xk.KeyReference):
                reads.append(xk.KeyReference(path, verdict.owner, verdict.member))
            else:
                refused.append(refusal_at(node.start_byte, decl.name, verdict))
        # The name anywhere else: a string or symbol a lookup completes
        # (`const_get(:NAME)`, `"Flags::NAME"`), a heredoc, a method name.
        token = re.compile(xk.mention_pattern([decl.name]).pattern.encode())
        for hit in token.finditer(raw):
            at = hit.start()
            if at in judged or any(start <= at < end for start, end in comments):
                continue
            refused.append(refusal_at(at, decl.name, xk.UNSUPPORTED_FORM))
    if refused:
        return [], refused
    return reads, []


# --- the owner reached whole, and gems ------------------------------------------


def module_escapes(path: str, repo_dir: str) -> list[xk.Refusal]:
    """A refusal at each place in the checkout that reaches an owner of a key
    constant ``path`` declares, or any class or module on that owner's path,
    as a whole (or, for a top-level constant, a lookup by a computed name):
    :func:`flag_cleanup.ruby_owner_reach.reaching`, the declaring file
    included. Each keeps every key constant ``path`` declares."""
    from flag_cleanup import piranha_runner

    asked = _OWNERS.get(path)
    if not asked:
        return []
    index = piranha_runner._source_index(repo_dir, "ruby")
    families: dict[str, frozenset[str]] = {}
    found: list[xk.Refusal] = []
    for name in sorted(asked, key=lambda o: o or ""):
        for where, line, _form in ruby_owner_reach.reaching(index, name, families):
            text = index.get(where, "")
            quoted = text.splitlines()[line - 1].strip() if 0 < line <= text.count("\n") + 1 else ""
            refusal = xk.Refusal(str(where), line, xk.UNSUPPORTED_FORM, quoted)
            if refusal not in found:
                found.append(refusal)
    return found


def gem_root(path: str, repo_dir: str) -> Path | None:
    """The nearest directory above ``path``, inside the checkout, holding a
    ``.gemspec``: the gem that publishes it."""
    root = Path(source_index.checkout_root(repo_dir)).resolve()
    current = Path(path).resolve().parent
    while True:
        if any(current.glob("*.gemspec")):
            return current
        if current == root or root not in current.parents:
            return None
        current = current.parent


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """A finding at each key constant a gem declares (code outside the
    repository may read it), and at each one :func:`declarations` does not
    offer: no read of it is proven, so another file spelling it is one this
    run cannot see past. Either keeps its file when another file spells one
    of its key constants; otherwise it is set aside
    (:func:`home_constant_names`), and the in-file rules treat the file as
    they did before."""
    found: list[xk.Refusal] = []
    lines = source.splitlines()
    offered = declarations(path, source, flag_key)
    if offered and gem_root(path, str(Path(path).parent)) is not None:
        for decl in offered:
            line, text = xk._line_of(path, decl.name)
            found.append(xk.Refusal(path, line, xk.UNSUPPORTED_FORM, text))
    for line in _HOME_MENTIONS.get(path, ()):
        found.append(
            xk.Refusal(path, line, xk.UNSUPPORTED_FORM, lines[line - 1].strip() if line <= len(lines) else "")
        )
    for _name, line in _UNOFFERED.get(path, ()):
        found.append(
            xk.Refusal(path, line, xk.UNSUPPORTED_FORM, lines[line - 1].strip() if line <= len(lines) else "")
        )
    return found


def home_constant_names(path: str, source: str, flag_key: str) -> set[str]:
    return {d.name for d in declarations(path, source, flag_key)} | {
        name for name, _line in _UNOFFERED.get(path, ())
    }


def unparsed_constant_mentions(
    path: str, source: str, constants: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """The refusals a file holding Ruby that no profile here parses gives
    ``constants``, judged as the in-file gate judges another file
    (``_referenced_elsewhere``): a constant's name spelled together with its
    owner or a class inheriting it, or alone for a top-level constant or in a
    HAML or Slim template, whose Ruby is never parsed. A suffix-less file
    that is not Ruby (a shell script, a ``Makefile``) gives none."""
    erb_config = path.endswith(_ERB_CONFIG_SUFFIXES)
    if erb_config and "<%" not in source:
        return []
    if not erb_config and not ruby_owner_reach.is_ruby(path, source):
        return []
    from flag_cleanup import piranha_runner

    found: list[xk.Refusal] = []
    lines = source.splitlines()
    for decl in constants:
        hit = xk.mention_pattern([decl.name]).search(source)
        if hit is None:
            continue
        if decl.owner is not None and not erb_config and not ruby_owner_reach.unparsed(path):
            owner = _owner_path(decl)
            key = (str(source_index.checkout_root(repo_dir)), owner[-1])
            if key not in _FAMILIES:
                _FAMILIES[key] = ruby_owner_reach.family(
                    piranha_runner._source_index(repo_dir, "ruby"), owner[-1]
                )
            if not xk.mention_pattern(set(owner) | _FAMILIES[key]).search(source):
                continue
        line = source.count("\n", 0, hit.start())
        found.append(
            xk.Refusal(path, line + 1, xk.UNSUPPORTED_FORM, lines[line].strip() if line < len(lines) else "")
        )
    return found


_PROFILE = xk.Profile(
    language_family=frozenset({"ruby", "erb"}),
    declarations=declarations,
    references=references,
    declared_names=declared_names,
    declared_classifiers=declared_classifiers,
    entry_findings=entry_findings,
    home_constant_names=home_constant_names,
    # Views, project files and scripts: a name spelled there keeps it.
    enum_unparsed_suffixes=UNPARSED_SUFFIXES,
    unparsed_constant_mentions=unparsed_constant_mentions,
    # The owner, or a class inheriting it, taken whole.
    module_escapes=module_escapes,
    # Any file can assign a constant, or set one by a computed name, and no
    # rule tells such a write from a harmless mention: a kept declaration
    # keeps every read.
    readers_need_deletable=True,
)

xk.register("ruby", _PROFILE)
xk.register("erb", _PROFILE)
