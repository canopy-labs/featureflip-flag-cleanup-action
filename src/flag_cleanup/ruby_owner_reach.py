"""Ruby files that reach a key constant's owner whole, never spelling the constant.

A Ruby key constant declared in one file is kept when another file could read
it (:func:`flag_cleanup.piranha_runner._referenced_elsewhere`), and "could read
it" used to mean "spells its name and its owner's". Ruby reaches a constant
without spelling it as a matter of course:

* reflection on the owner: ``Gate.const_get(n)``, ``Gate.constants``,
  ``Gate.send(:const_get, n)``, ``Gate.class_eval(src)``;
* the owner handed on as a value: ``register(Gate)``, ``m = Gate``, ``[Gate]``,
  ``"#{Gate}"``, after which any of the above can run on it anywhere;
* the owner named by a string or symbol that a lookup completes at run time:
  ``Object.const_get("Gate::#{n}")``, ``"Gate::" + n``, ``"Gate::#{n}".constantize``,
  ``Object.const_get(:Gate)``;
* the owner reopened, with a reflective call on its own ``self``:
  ``class Gate; def self.all = constants; end``, and the same call, or any
  form above, in the declaring file itself;
* any of the above on a class that inherits the constant: one that subclasses
  the owner, or includes, extends or prepends it, transitively. ``Sub::NAME``
  in a third file spells the constant but not ``Gate``, so the family also
  widens the owner half of the name check.

"Another file" is any file holding Ruby: ``.rb``, ``.rake`` and ``.erb``, and
the views, project files and scripts the listing holds for these checks alone
(``.jbuilder``, ``.haml``, ``.slim``, ``config.ru``; see
:data:`~flag_cleanup.source_index.RUBY_SUFFIXES`), and a file with no suffix
named like ``Rakefile`` or ``Gemfile`` (:data:`NAMES`) or starting with a Ruby
shebang. HAML and Slim templates are never parsed: one reaches every owner it
names.

A top-level constant has no owner to reach. ``const_get`` with any receiver
finds it (``Object`` is every class's ancestor, and ``Module#const_get`` looks
in ``Object`` for a module too), so a computed ``const_get``, ``constantize`` or
``Object.constants`` anywhere keeps it.

Every answer here only ever KEEPS a declaration, so each list errs wide: an
unrecognised use of the owner counts as handing it on, and a file that does not
parse counts as reaching every owner it names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index, syntax

#: The languages whose key constants this module answers for. ERB holds Ruby
#: inside a template and is read through its code view.
LANGUAGES = frozenset({"ruby", "erb"})

#: Templates holding Ruby that no grammar here reads. One is never parsed: it
#: reaches every owner it names, and reads every constant it names.
UNPARSED_SUFFIXES = (".haml", ".slim", ".rbs")

#: Templates read through ERB's code view.
_ERB_SUFFIXES = (".erb", ".rhtml")

#: Suffixes of the files that can reach a Ruby owner: the ones the ruby and erb
#: languages claim, and the ones the listing holds for these checks alone.
_SUFFIXES = (".rb", ".rake", ".erb", *sorted(source_index.RUBY_SUFFIXES))

#: Ruby files known by their whole name, with no suffix to list them by.
NAMES = frozenset(
    {
        "Rakefile", "Gemfile", "Guardfile", "Capfile", "Thorfile", "Vagrantfile", "Podfile",
        "Fastfile", "Appfile", "Dangerfile", "Brewfile", "Berksfile", "Steepfile",
    }
)

#: A first line that runs the file with Ruby: ``#!/usr/bin/env ruby``,
#: ``#!/usr/bin/ruby -w``, ``#!/usr/bin/env -S jruby``.
_SHEBANG = re.compile(r"#![^\n]*[/ \t](?:j|truffle)?ruby[\d.]*(?:[ \t]|$)", re.MULTILINE)


def is_ruby(path: Path | str, text: str) -> bool:
    """Whether ``path`` holds Ruby: by suffix, or with no suffix, by its whole
    name (``Rakefile``) or a Ruby shebang (``bin/setup``)."""
    path = Path(path)
    if path.suffix:
        return path.name.endswith(_SUFFIXES)
    return path.name in NAMES or _SHEBANG.match(text) is not None


def unparsed(path: Path | str) -> bool:
    """Whether ``path`` is a template whose Ruby is never parsed here."""
    return str(path).endswith(UNPARSED_SUFFIXES)

#: Methods called ON the owner that read a constant without its name, hand the
#: owner on (it is their receiver or return value), or name it in a string a
#: lookup can complete. Core ``Module``/``Kernel`` methods, plus ``extend`` and
#: friends, which give the owner methods that can reflect on it.
_REACHING = frozenset(
    {
        "const_get",
        "const_defined?",
        "const_source_location",
        "const_missing",
        "constants",
        "const_set",
        "remove_const",
        "deprecate_constant",
        "public_constant",
        "send",
        "__send__",
        "public_send",
        "instance_eval",
        "instance_exec",
        "class_eval",
        "class_exec",
        "module_eval",
        "module_exec",
        "method",
        "public_method",
        "singleton_method",
        "singleton_class",
        "tap",
        "then",
        "yield_self",
        "itself",
        "freeze",
        "dup",
        "clone",
        "ancestors",
        "name",
        "to_s",
        "inspect",
        "extend",
        "include",
        "prepend",
    }
)

#: Calls that take the owner as an argument and only compare it or mix it in.
#: Mixing in makes the caller's class a member of the family, which is judged
#: on its own.
_TAKES_WITHOUT_HANDING_ON = frozenset(
    {"include", "extend", "prepend", "is_a?", "kind_of?", "instance_of?", "equal?", "eql?", "=="}
)

_MIXINS = frozenset({"include", "extend", "prepend"})

#: Lookups that name what they read in their first argument: one spelled as a
#: literal reads that and nothing else, and is the name check's to judge.
_LOOKUPS = frozenset(
    {
        "const_get",
        "const_defined?",
        "const_source_location",
        "const_set",
        "remove_const",
        "deprecate_constant",
        "public_constant",
    }
)

#: What an implicit receiver reaches inside a reopened owner. Narrower than
#: :data:`_REACHING`: ``name``, ``method`` or ``then`` there is far more often a
#: local or an attribute than the owner's own method.
_SELF_REFLECTING = frozenset(
    {
        "const_get",
        "const_defined?",
        "const_source_location",
        "constants",
        "class_eval",
        "class_exec",
        "module_eval",
        "module_exec",
        "instance_eval",
        "instance_exec",
        "singleton_class",
        "ancestors",
    }
)

#: Test-framework calls that take the owner as the subject of a spec or a
#: double, which reads none of its constants: `RSpec.describe Gate do`,
#: `allow(Gate).to receive(:x)`, `class_double("Gate")`.
_TEST_SUBJECTS = frozenset(
    {
        "describe",
        "context",
        "allow",
        "expect",
        "class_double",
        "instance_double",
        "object_double",
        "class_spy",
        "instance_spy",
        "object_spy",
    }
)

#: Calls that evaluate a string as Ruby, so a string naming the owner there is
#: code reaching it.
_EVALS = frozenset({"eval", "instance_eval", "class_eval", "module_eval"})

_SENDS = frozenset({"send", "__send__", "public_send"})

_COMPARISONS = frozenset({"==", "!=", "===", "<", "<=", ">", ">=", "<=>"})

#: The receivers whose ``constants`` lists the top-level ones.
_TOP_LEVEL_HOLDERS = frozenset({"Object", "Module", "BasicObject", "Kernel"})

_TOP_LEVEL_TOKENS = re.compile(
    r"const_get|const_defined\?|const_source_location|constantize|\bconstants\b"
)

#: String-like nodes whose content a constant lookup could be handed.
_STRINGS = frozenset({"string", "delimited_symbol", "heredoc_body", "bare_string"})
#: Symbol-like nodes whose whole text names something.
_SYMBOLS = frozenset({"simple_symbol", "hash_key_symbol", "bare_symbol"})

_HOLE = "\0"

#: A constant path, its leading segments possibly interpolated
#: (`"#{ns}::Gate"`). The last segment is the one named.
_CONSTANT_PATH = re.compile(r"(?:::)?(?:(?:[A-Z]\w*|\0)::)*([A-Z]\w*)")


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


def _walk(node: Node):
    """Every NAMED node under ``node``, in source order. Operators and
    punctuation are read through their parents' fields, never visited."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _reopens(node: Node, family: frozenset[str]) -> bool:
    """Whether ``node`` reopens an owner of ``family``: ``class Gate``,
    ``module Gate``, ``class << Gate``."""
    if node.type in ("class", "module"):
        return _last_segment(node.child_by_field_name("name")) in family
    if node.type == "singleton_class":
        return _last_segment(node.child_by_field_name("value")) in family
    return False


def _walk_scoped(root: Node, family: frozenset[str]):
    """``(node, reopened)`` for every named node, ``reopened`` when an
    ancestor reopens an owner of ``family``. One pass: asking each node's
    ancestors instead made the scan quadratic in nesting."""
    stack = [(root, False)]
    while stack:
        current, reopened = stack.pop()
        yield current, reopened
        inner = reopened or _reopens(current, family)
        stack.extend((child, inner) for child in reversed(current.named_children))


@lru_cache(maxsize=256)
def _tree(path: str, text: str) -> Node | None:
    """The Ruby tree of ``text`` (an ERB template's code view), or None when it
    does not parse. Cached for the two walks of one file (:func:`_heirs`,
    :func:`_owner_uses`), whose own answers are cached for longer: a tree holds
    its source, an answer does not. A HAML or Slim template is never parsed:
    one can happen to parse as Ruby and read as something it is not."""
    if unparsed(path):
        return None
    if path.endswith(_ERB_SUFFIXES):
        text = syntax._code_view(text, "erb")
    root = syntax._parse(text, "ruby")
    return None if root.has_error else root


def _last_segment(node: Node | None) -> str | None:
    """``Gate`` for ``Gate``, ``App::Gate`` and ``::Gate``; None otherwise."""
    if node is None:
        return None
    if node.type == "constant":
        return _text(node)
    if node.type == "scope_resolution":
        return _last_segment(node.child_by_field_name("name"))
    return None


def _as_value(constant: Node) -> Node:
    """The expression a mention of a constant stands in: ``App::Gate`` for its
    ``Gate``, the constant itself when it is not the last segment of a path."""
    node = constant
    while node.parent is not None and node.parent.type == "scope_resolution" and _field(node) == "name":
        node = node.parent
    return node


def _call_method(call: Node) -> str:
    return _text(call.child_by_field_name("method"))


def _first_argument(call: Node) -> Node | None:
    arguments = call.child_by_field_name("arguments")
    return arguments.named_children[0] if arguments is not None and arguments.named_children else None


def _literal_name(node: Node | None) -> str | None:
    """The text of a plain symbol or a string with nothing interpolated, or
    None for anything a run could compute."""
    if node is None:
        return None
    if node.type == "simple_symbol":
        return _text(node).lstrip(":")
    if node.type == "string" and all(c.type == "string_content" for c in node.named_children):
        return "".join(_text(c) for c in node.named_children)
    return None


def _literal_arguments(call: Node) -> bool:
    """Whether a lookup's first argument is a literal name."""
    return _literal_name(_first_argument(call)) is not None


def _sends_reflection(call: Node, methods: frozenset[str]) -> bool:
    """Whether ``send(:m, ...)`` (or ``__send__``/``public_send``) names one of
    ``methods`` as a literal."""
    return _literal_name(_first_argument(call)) in methods


def _argument_of(node: Node) -> Node | None:
    """The call ``node`` is a direct argument of, or None."""
    parent = node.parent
    if parent is None or parent.type != "argument_list":
        return None
    call = parent.parent
    return call if call is not None and call.type == "call" else None


def _escape(value: Node, reaching: frozenset[str] = _REACHING) -> str | None:
    """How ``value``, an expression holding the owner, reaches it whole, or
    None when its position only names a member, compares it or declares it.
    ``reaching`` is what a method called on it must be to count."""
    parent = value.parent
    field = _field(value)
    if parent is None:
        return None
    kind = parent.type
    if kind == "scope_resolution" and field == "scope":
        return None  # `Gate::NAME` spells what it reads
    if kind == "call" and field == "receiver":
        method = _call_method(parent)
        if method in _LOOKUPS and _literal_arguments(parent):
            return None  # `Gate.const_get(:OTHER)` reads OTHER
        if method in _SENDS and _literal_name(_first_argument(parent)) is not None:
            # `Gate.send(:enabled?)`: only a reflective method counts.
            return f".{method}" if _sends_reflection(parent, reaching | _LOOKUPS) else None
        if method in ("new", "allocate") and parent.parent is not None \
                and parent.parent.type == "call" and _field(parent) == "receiver" \
                and _call_method(parent.parent) == "class":
            return _escape(parent.parent, reaching)  # `Gate.new.class` is Gate again
        return f".{method}" if method in reaching else None
    if kind in ("class", "module") and field == "name":
        return None  # reopened: its body is judged on its own
    if kind in ("superclass", "singleton_class") or (kind == "singleton_method" and field == "object"):
        return None
    if kind in ("exceptions", "pattern"):
        return None  # `rescue Gate`, `when Gate`
    if kind == "binary" and _text(parent.child_by_field_name("operator")) in _COMPARISONS:
        return None
    if kind == "parenthesized_statements" and parent.parent is not None and parent.parent.type == "unary":
        if _text(parent.parent.child_by_field_name("operator")) == "defined?":
            return None
    call = _argument_of(value)
    if call is not None:
        method = _call_method(call)
        if method in _TEST_SUBJECTS:
            return None
        if method in _TAKES_WITHOUT_HANDING_ON:
            receiver = call.child_by_field_name("receiver")
            if method not in _MIXINS or receiver is None or receiver.type == "self":
                return None
    return "handed on"


def _host_name(node: Node) -> str | None:
    """The class or module whose body ``node`` is in: the nearest ``class`` or
    ``module``, or a constant assigned a call whose block holds it
    (``S = Struct.new(:a) do ... end``, ``K = Class.new do ... end``)."""
    current = node.parent
    while current is not None:
        if current.type in ("class", "module"):
            return _last_segment(current.child_by_field_name("name"))
        if current.type in ("do_block", "block") and current.parent is not None \
                and current.parent.type == "call":
            holder = current.parent.parent
            if holder is not None and holder.type == "assignment":
                left = holder.child_by_field_name("left")
                if left is not None and left.type in ("constant", "scope_resolution"):
                    return _last_segment(left)
        current = current.parent
    return None


def _string_reaches(node: Node, family: frozenset[str]) -> bool:
    """Whether a string-like ``node`` names an owner of ``family`` in a way a
    run-time lookup completes: the owner's whole name (``"Gate"``,
    ``"App::Gate"``, ``"#{ns}::Gate"``), ``"Gate::"`` followed by a hole -- an
    interpolation, a format directive, the string's end when something is
    appended to it, or nothing at all -- or any mention in code handed to
    ``eval``."""
    call = _argument_of(node)
    if call is not None and _call_method(call) in _TEST_SUBJECTS:
        return False  # `class_double("Gate")`
    parent = node.parent
    if parent is not None and parent.type == "pair" \
            and _text(parent.child_by_field_name("key")).strip(":") == "class_name":
        return False  # `belongs_to :gate, class_name: "Gate"` resolves the class only
    pieces: list[str] = []
    for child in node.named_children:
        pieces.append(_HOLE if child.type == "interpolation" else _text(child))
    text = "".join(pieces)
    if call is not None and _call_method(call) in _EVALS and re.search(_names(family), text):
        return True
    if "%" in text:
        text = text.replace("%", _HOLE)  # a format string's directive is a hole
    appended = (
        parent is not None
        and (
            (parent.type == "binary" and _field(node) == "left"
             and _text(parent.child_by_field_name("operator")) in ("+", "<<", "%"))
            or (parent.type == "chained_string" and parent.named_children[-1].id != node.id)
        )
    )
    whole = _CONSTANT_PATH.fullmatch(text)
    if whole is not None and whole.group(1) in family:
        return True
    for name in family:
        for match in re.finditer(r"(?<![\w])%s::(\w*)" % re.escape(name), text):
            rest = text[match.end():]
            if rest.startswith(_HOLE) or not match.group(1):
                return True
            if not rest and appended:
                return True
    return False


def _symbol_reaches(node: Node, family: frozenset[str]) -> bool:
    text = _text(node).lstrip(":").rstrip(":")
    return text in family


def _self_is_instance(node: Node) -> bool:
    """Whether ``self`` at ``node`` is an instance: the nearest scope is an
    instance method rather than a class body, ``def self.x`` or ``class <<``."""
    current = node.parent
    while current is not None:
        if current.type == "method":
            return True
        if current.type in ("singleton_method", "class", "module", "singleton_class"):
            return False
        current = current.parent
    return False


def _not_parsed(path: str) -> str:
    """The form logged for a file with no tree to walk."""
    return "a template that is not parsed" if unparsed(path) else "a file that does not parse"


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def owner_uses(path: str, text: str, family: frozenset[str]) -> list[tuple[int, str]]:
    """``(line, form)`` at each place in ``path`` reaching an owner of
    ``family`` whole. A file that does not parse reaches every owner it
    spells, at line 1."""
    return list(_owner_uses(path, text, family))


@lru_cache(maxsize=4096)
def _owner_uses(path: str, text: str, family: frozenset[str]) -> tuple[tuple[int, str], ...]:
    root = _tree(path, text)
    if root is None:
        return ((1, _not_parsed(path)),) if re.search(_names(family), text) else ()
    found: list[tuple[int, str]] = []
    for node, reopened in _walk_scoped(root, family):
        kind = node.type
        if kind == "constant" and _text(node) in family:
            value = _as_value(node)
            if _last_segment(value) not in family:
                continue  # `Gate::Inner`: the owner is a scope, not the value
            form = _escape(value)
            if form is not None:
                found.append((_line(node), f"`{_text(value)}` {form}"))
        elif kind in _STRINGS and _string_reaches(node, family):
            found.append((_line(node), "a string naming the owner"))
        elif kind in _SYMBOLS and _symbol_reaches(node, family):
            found.append((_line(node), "a symbol naming the owner"))
        elif not reopened:
            continue
        elif kind == "self":
            value = node
            if node.parent is not None and node.parent.type == "call" and _field(node) == "receiver" \
                    and _call_method(node.parent) == "class":
                value = node.parent  # `self.class` in an instance method
            elif _self_is_instance(node):
                continue  # an instance handed on is not its class
            form = _escape(value, _SELF_REFLECTING)
            if form is not None:
                found.append((_line(node), f"`{_text(value)}` {form} in the reopened owner"))
        elif kind == "call" and node.child_by_field_name("receiver") is None:
            method = _call_method(node)
            if (method in _SELF_REFLECTING and not (method in _LOOKUPS and _literal_arguments(node))) \
                    or (method in _SENDS and _sends_reflection(node, _SELF_REFLECTING)):
                found.append((_line(node), f"`{method}` in the reopened owner"))
        elif kind == "identifier" and _text(node) in _SELF_REFLECTING and _field(node) != "method" \
                and node.parent is not None \
                and node.parent.type not in ("method_parameters", "block_parameters", "lambda_parameters") \
                and not (node.parent.type == "assignment" and _field(node) == "left"):
            # A bare `constants` parses as an identifier, not a call, wherever
            # it stands: `constants.map(&:to_s)`, `x = constants`.
            found.append((_line(node), f"`{_text(node)}` in the reopened owner"))
    return tuple(found)


def top_level_uses(path: str, text: str) -> list[tuple[int, str]]:
    """``(line, form)`` at each place in ``path`` that can read a TOP-LEVEL
    constant without its name: a lookup by a computed name from any receiver,
    or ``Object.constants``."""
    if not _TOP_LEVEL_TOKENS.search(text):
        return []
    root = _tree(path, text)
    if root is None:
        return [(1, _not_parsed(path))]
    found: list[tuple[int, str]] = []
    for node in _walk(root):
        if node.type != "call":
            continue
        method = _call_method(node)
        receiver = node.child_by_field_name("receiver")
        if method in _LOOKUPS and not _literal_arguments(node):
            found.append((_line(node), f"`{method}` by a computed name"))
        elif method in ("constantize", "safe_constantize") and _literal_name(receiver) is None:
            found.append((_line(node), f"`{method}` of a computed name"))
        elif method == "constants" and (receiver is None or _text(receiver).lstrip(":") in _TOP_LEVEL_HOLDERS):
            found.append((_line(node), "`constants`"))
        elif method in _SENDS and _sends_reflection(node, _LOOKUPS | {"constants"}):
            found.append((_line(node), f"`{method}` of a lookup"))
    return found


def _names(names: Iterable[str]) -> str:
    alternatives = sorted(set(names))
    if not alternatives:
        return r"(?!)"
    return r"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])" % "|".join(map(re.escape, alternatives))


def _ruby_files(index: Mapping[Path, str]) -> list[tuple[Path, str]]:
    return [(path, text) for path, text in index.items() if is_ruby(path, text)]


@lru_cache(maxsize=4096)
def _heirs(path: str, text: str) -> tuple[tuple[str, str], ...]:
    """``(heir, parent)`` for each class or module of ``text`` that subclasses,
    includes, extends or prepends ``parent``, both by last segment. One walk
    per file, however many owners and declaring files ask."""
    root = _tree(path, text)
    if root is None:
        return ()
    edges: list[tuple[str, str]] = []
    for node in _walk(root):
        if node.type == "class":
            superclass = node.child_by_field_name("superclass")
            name = _last_segment(node.child_by_field_name("name"))
            for child in superclass.named_children if superclass is not None else ():
                parent = _last_segment(child)
                if name is not None and parent is not None:
                    edges.append((name, parent))
        elif node.type == "call" and _call_method(node) in _MIXINS:
            receiver = node.child_by_field_name("receiver")
            arguments = node.child_by_field_name("arguments")
            if (receiver is not None and receiver.type != "self") or arguments is None:
                continue
            host = _host_name(node)
            for argument in arguments.named_children:
                parent = _last_segment(argument)
                if host is not None and parent is not None:
                    edges.append((host, parent))
    return tuple(edges)


def family(index: Mapping[Path, str], owner: str) -> frozenset[str]:
    """``owner`` and every class or module of the checkout that inherits its
    constants: one that subclasses it, or includes, extends or prepends it,
    transitively. Named by last segment, as owners are."""
    edges = [edge for path, text in _ruby_files(index) for edge in _heirs(str(path), text)]
    members = {owner}
    grown = True
    while grown:
        grown = False
        for heir, parent in edges:
            if parent in members and heir not in members:
                members.add(heir)
                grown = True
    return frozenset(members)


def reaching(
    index: Mapping[Path, str], owner: str | None, families: dict[str, frozenset[str]]
) -> list[tuple[Path, int, str]]:
    """``(file, line, form)`` for each Ruby file of ``index`` reaching
    ``owner`` (or, for None, a top-level constant) whole. ``families`` caches
    :func:`family` per owner across a run's declarations.

    The declaring file is asked too: an owner listing its own constants
    (``def self.all = constants``, ``Gate.constants`` after its body) reads
    the key constant without spelling it, which no in-file check sees."""
    found: list[tuple[Path, int, str]] = []
    if owner is not None and owner not in families:
        families[owner] = family(index, owner)
    members = families.get(owner, frozenset()) if owner is not None else frozenset()
    pattern = re.compile(_names(members)) if owner is not None else None
    for path, text in _ruby_files(index):
        if pattern is not None:
            uses = owner_uses(str(path), text, members) if pattern.search(text) else []
        else:
            uses = top_level_uses(str(path), text)
        found.extend((path, line, form) for line, form in uses)
    return found
