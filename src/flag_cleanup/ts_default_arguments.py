"""Leave out a literal this removal made equal to the callee's own default.

Two kinds of literal are left behind by a removal, and both can end up passing
exactly what the callee would have used anyway:

* an override the entry deletions emptied, ``<Provider flags={{}}>`` or
  ``createWrapper(['/a'], {})``, where the callee declares ``flags = {}``;
* a value the fold wrote, ``useRole(id, { enabled: true })`` where the read
  ``{ enabled: onboardingFlag }`` folded to ``true`` and the hook declares
  ``{ enabled = true }: { enabled?: boolean } = {}``.

Each such literal is removed when the callee's declaration PROVES the edit
changes nothing, and is otherwise left as it was (an emptied one is still
reported, by :func:`~flag_cleanup.ts_bound_overrides.emptied_overrides`):

* a JSX attribute goes when the component destructures that prop with a
  default equal to the value;
* an object-literal argument goes when it is the LAST argument, the parameter
  defaults to ``{}``, and each of its properties equals the default its
  parameter destructures it with; otherwise just those properties go.

**Only literals this run wrote are candidates.** An emptied override is known
by the line it sits on (``_emptied_lines``, certain lines only, one empty
object on the line). A folded value is a literal on a line the run changed,
held by a property or attribute whose text the old side of the same hunk did
not hold: ``f(isOn('k') ? 1 : 2, { enabled: true })`` keeps the customer's
``enabled: true``. Code the customer already wrote this way is never touched.

**The callee is resolved, never guessed.** Its name must be bound once in the
caller's file, by a top-level ``function``, a ``const`` or an unexported
``let`` (at the top level, or in a block that encloses the call, as a test
helper declared inside ``describe`` is), or by a named import, and every other mention of it there
must be a call to it or a JSX tag naming it, so it is never re-bound,
shadowed or handed on. An import is followed with
:func:`~flag_cleanup.cross_file_ts.resolve_specifier`, through ``export { … }``
and ``export { … } from`` to the declaration; a package, a specifier that
resolves to nothing or to a declaration file, a file outside the checkout, a
file that does not parse, ``export *`` and a default export all refuse. In
the declaring file the same rule holds for the declaration's own name, which
also refuses an overload signature, a reassignment and ``X.defaultProps = …``.
No other binding of the name may exist anywhere in either file, a nested
``class`` included.

**What else refuses**, each because it can tell the two calls apart:

* the function names ``arguments``, has type parameters (a passed literal can
  infer them, a default cannot), or takes a ``this`` parameter;
* the parameter is a rest parameter, or destructures a ``...rest``;
* the member is not destructured by name, or its type is not optional where
  the parameter is annotated (an inline object type, or an ``interface`` or
  object ``type`` declared once in the declaring file); a JavaScript callee
  counts as typed when a JSDoc tag sits on it, since ``allowJs`` checks calls
  against it;
* a component is wrapped in anything but React's ``memo`` (with no compare
  function) or ``forwardRef``, either given type arguments, or ``memo`` is
  handed a ``{}`` or ``[]`` it would compare by identity; or a
  ``defaultProps`` property sits in any file that names it, since React
  fills an omitted prop from it;
* anything in the checkout can read an element's props from outside the
  component: ``cloneElement``, ``Children``, ``x.props`` (not ``this.props``,
  and not a holder the same file assigns ``.props`` to, which a frozen
  element cannot be), ``.prop(…)``, or a shallow renderer;
* a test mocks a module on the import chain (``vi.mock``/``jest.mock``) or
  spies on the callee by name, since it may assert on the argument;
* the element spreads attributes, names the prop twice, or the prop is
  ``key`` or ``ref``, which React keeps out of the props;
* a comment sits beside the attribute or in the bytes the edit takes.

**"Equal" is structural, and narrow**: ``{}`` and ``[]`` (a default is
evaluated afresh on every call, so leaving the argument out builds the same
fresh value), ``true``/``false``/``null``, a number and a string spelling the
same value (never a legacy octal, which a sloppy-mode script reads in base
8), and ``undefined``, which destructuring treats as absent.

This runs on the engine's output, after every other TypeScript-family text
pass, so it judges the bytes that stay on disk; Gate 1 judges its edits and
the all-or-nothing rollback covers them.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_ts, source_index, ts_list_join
from flag_cleanup.ts_bound_overrides import _emptied_lines, _one_line
from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

_PARAMETERS = frozenset({"required_parameter", "optional_parameter"})

_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})

#: The leaf types that spell a value named by an identifier.
_NAME_LEAVES = frozenset(
    {"identifier", "shorthand_property_identifier_pattern", "shorthand_property_identifier"}
)

_JSX_ELEMENTS = frozenset({"jsx_opening_element", "jsx_self_closing_element"})

#: The JSX tags whose ``name`` field names a component.
_JSX_TAGS = _JSX_ELEMENTS | {"jsx_closing_element"}

#: Literal values this pass compares. The grammar gives ``undefined`` a node of
#: its own, though it is a name a file can re-bind.
_LITERALS = frozenset({"true", "false", "null", "undefined", "number", "string", "object", "array"})

#: React's wrappers that hand a component's props through unchanged.
_REACT_WRAPPERS = frozenset({"memo", "forwardRef"})

_DECLARATION_FILES = (".d.ts", ".d.mts", ".d.cts")

#: How many ``export … from`` hops a lookup follows before giving up.
_MAX_HOPS = 16


@dataclass(frozen=True)
class Dropped:
    content: str
    #: The call or JSX element each literal was left out of, on one line, as
    #: it read before.
    sites: tuple[str, ...]
    #: The line :func:`~flag_cleanup.ts_bound_overrides.emptied_overrides`
    #: gives each emptied override this removed, for the caller to take out
    #: of that report.
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _Callee:
    function: Node
    root: Node
    data: bytes
    #: Whether the declaring file is JavaScript with no JSDoc types on it.
    untyped: bool
    #: Whether React's ``memo`` wraps it, which compares props by identity.
    memoized: bool = False
    #: Every module a mock could replace it through: each file on the import
    #: chain from the caller to the declaration.
    modules: frozenset[str] = frozenset()
    #: Every name it goes by along that chain, for ``spyOn(module, 'name')``.
    names: frozenset[str] = frozenset()
    #: The block a ``const`` or ``let`` below the top level binds it in, which
    #: a call must be inside to name it; ``None`` at the top level.
    scope: Node | None = None


def drop_default_arguments(
    before: str,
    after: str,
    language: str,
    flag_key: str,
    path: str | None = None,
    repo_dir: str | None = None,
    width: int | None = None,
) -> Dropped:
    """``after`` without each literal this removal left equal to the
    callee's default; see the module docstring for the proof.

    ``path`` and ``repo_dir`` locate the file, for a callee imported from
    another; without them only a callee declared in this file is followed.
    An argument list the drop leaves with one element is put back on one
    line when it fits ``width``; see :mod:`flag_cleanup.ts_list_join`.
    """
    unchanged = Dropped(after, (), ())
    if before == after:
        return unchanged
    data = after.encode("utf-8")
    root = _parse(after, language)
    after_lines = after.splitlines()
    emptied = _emptied_objects(before, after, language, flag_key, root)
    produced = emptied | _folded_values(before, after, language, root)
    if not produced:
        return unchanged
    resolver = _Resolver(root, data, path, repo_dir, language)
    # Each edit's bytes, the site to report it under (one per call or element
    # it shortens, so a second span of the same object has none), and the
    # emptied-override lines it accounts for.
    edits: list[tuple[int, int, str | None, tuple[str, ...]]] = []
    objects: dict[int, tuple[Node, list[Node]]] = {}

    def report(literal: Node) -> tuple[str, ...]:
        """The emptied-override line ``literal`` accounts for, if it is one."""
        if (literal.start_byte, literal.type) not in emptied:
            return ()
        return (after_lines[literal.start_point[0]].strip(),)

    for node in _walk(root):
        if (node.start_byte, node.type) not in produced:
            continue
        holder = node.parent
        if holder.type == "jsx_expression" and holder.parent.type == "jsx_attribute":
            span = _attribute_span(holder.parent, node, resolver, data)
            if span is not None:
                edits.append((*span, _one_line(_text(holder.parent.parent, data)), report(node)))
        elif holder.type == "pair" and _same(holder.child_by_field_name("value"), node):
            objects.setdefault(holder.parent.id, (holder.parent, []))[1].append(holder)
        elif node.type == "object":
            span = _argument_span(node, resolver)
            if span is not None:
                edits.append((*span, _one_line(_text(holder.parent, data)), report(node)))
    for literal, pairs in objects.values():
        for n, (start, end) in enumerate(_property_spans(literal, pairs, resolver, data)):
            taken = [p.child_by_field_name("value") for p in pairs if start <= p.start_byte < end]
            site = _one_line(_text(literal.parent.parent, data)) if n == 0 else None
            edits.append((start, end, site, tuple(line for v in taken for line in report(v))))
    kept = _non_overlapping(edits)
    if not kept:
        return unchanged
    content = ts_list_join.splice(after, [(s, e) for s, e, _site, _lines in kept], language, width)
    return Dropped(
        content,
        tuple(site for _s, _e, site, _l in kept if site is not None),
        tuple(line for _s, _e, _site, lines in kept for line in lines),
    )


def _non_overlapping(
    edits: list[tuple[int, int, str | None, tuple[str, ...]]],
) -> list[tuple[int, int, str | None, tuple[str, ...]]]:
    """The edits, by position, less any whose bytes an earlier one takes."""
    kept: list[tuple[int, int, str | None, tuple[str, ...]]] = []
    for edit in sorted(edits, key=lambda e: (e[0], e[1])):
        if kept and edit[0] < kept[-1][1]:
            continue
        kept.append(edit)
    return kept


# ---------------------------------------------------------------------------
# Which literals this run wrote
# ---------------------------------------------------------------------------


def _emptied_objects(
    before: str, after: str, language: str, flag_key: str, root: Node
) -> set[tuple[int, str]]:
    """``(start byte, "object")`` for each empty object literal in ``after``
    the entry deletions left, on a CERTAIN line holding no other empty object."""
    rows = {n for n, certain in _emptied_lines(before, after, language, flag_key) if certain}
    if not rows:
        return set()
    empty = [n for n in _walk(root) if n.type == "object" and not n.named_children]
    per_row = Counter(n.start_point[0] for n in empty)
    return {
        (n.start_byte, n.type)
        for n in empty
        if n.start_point[0] in rows and per_row[n.start_point[0]] == 1
    }


def _folded_values(before: str, after: str, language: str, root: Node) -> set[tuple[int, str]]:
    """``(start byte, type)`` for each literal value in ``after`` the run
    wrote into a property or attribute: on a changed line, in a hunk whose old
    side held no property or attribute of the same text."""
    old_root = _parse(before, language)
    old_data, new_data = before.encode("utf-8"), after.encode("utf-8")
    old_holders = _holders(old_root, old_data)
    new_holders = _holders(root, new_data)
    found: set[tuple[int, str]] = set()
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines(), autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("replace", "insert"):
            continue
        old = Counter(text for line, text, _value in old_holders if i1 <= line < i2)
        for line, text, value in new_holders:
            if j1 <= line < j2 and old[text] == 0 and _is_literal(value):
                found.add((value.start_byte, value.type))
    return found


def _holders(root: Node, data: bytes) -> list[tuple[int, str, Node]]:
    """The value's line, the holder's text on one line, and the value, of
    every property and JSX attribute."""
    found = []
    for node in _walk(root):
        if node.type == "pair":
            value = node.child_by_field_name("value")
        elif node.type == "jsx_attribute":
            expression = node.named_children[-1] if len(node.named_children) == 2 else None
            value = (
                expression.named_children[0]
                if expression is not None
                and expression.type == "jsx_expression"
                and len(expression.named_children) == 1
                else None
            )
        else:
            continue
        if value is not None:
            found.append((value.start_point[0], _one_line(_text(node, data)), value))
    return found


def _is_literal(node: Node) -> bool:
    if node.type in ("object", "array"):
        return not node.named_children
    return node.type in _LITERALS


# ---------------------------------------------------------------------------
# The three edits
# ---------------------------------------------------------------------------


def _attribute_span(
    attribute: Node, value: Node, resolver: _Resolver, data: bytes
) -> tuple[int, int] | None:
    """The bytes leaving out JSX ``attribute`` takes, or ``None``."""
    name = attribute.named_children[0]
    element = attribute.parent
    if name.type != "property_identifier" or element is None or element.type not in _JSX_ELEMENTS:
        return None
    prop = _text(name, data)
    if prop in ("key", "ref") or element.child_by_field_name("type_arguments") is not None:
        return None
    attributes = element.children_by_field_name("attribute")
    if any(a.type != "jsx_attribute" for a in attributes):
        # A spread attribute: whichever of it and the prop comes last wins.
        return None
    if sum(1 for a in attributes if _text(a.named_children[0], data) == prop) != 1:
        return None
    tag = element.child_by_field_name("name")
    if tag is None or tag.type != "identifier" or not _text(tag, data)[:1].isupper():
        return None
    callee = resolver.component(tag)
    if callee is None or callee.memoized and value.type in ("object", "array"):
        # `memo` compares props by identity: a fresh `{}` re-renders on every
        # parent render, the default does not.
        return None
    members = [c for c in _parameters(callee.function) if c.type != "comment"]
    if not members or not _destructured_equal(members[0], prop, value, callee, resolver, data):
        return None
    previous = attribute.prev_sibling
    if previous is None or previous.type == "comment" or any(
        c.type == "comment" for c in value.parent.named_children
    ):
        # Deleting up to a `// comment` would put the tag's `>` inside it.
        return None
    return previous.end_byte, attribute.end_byte


def _argument_span(literal: Node, resolver: _Resolver) -> tuple[int, int] | None:
    """The bytes leaving out the emptied argument ``literal`` takes, or ``None``."""
    found = _call_parameter(literal, resolver)
    if found is None or not found[2]:
        return None
    parameter = found[0]
    return _whole_argument(literal) if _defaults_to_empty_object(parameter) else None


def _property_spans(
    literal: Node, pairs: list[Node], resolver: _Resolver, data: bytes
) -> list[tuple[int, int]]:
    """The bytes taken by leaving out each of ``pairs`` that equals its
    destructured default, or the whole argument when every property does,
    it is the last one passed, and the parameter defaults to ``{}``."""
    members = literal.named_children
    if any(m.type != "pair" for m in members):
        # A comment, a spread, a shorthand or a method.
        return []
    keys = [_property_name(m.child_by_field_name("key"), data) for m in members]
    if None in keys or len(set(keys)) != len(keys):
        return []
    found = _call_parameter(literal, resolver)
    if found is None:
        return []
    parameter, callee, last = found
    removable = {
        pair.id
        for pair in pairs
        if _destructured_equal(
            parameter,
            _property_name(pair.child_by_field_name("key"), data),
            pair.child_by_field_name("value"),
            callee,
            resolver,
            data,
        )
    }
    if not removable:
        return []
    if len(removable) == len(members):
        if last and _defaults_to_empty_object(parameter):
            span = _whole_argument(literal)
            if span is not None:
                return [span]
        return [(literal.start_byte + 1, literal.end_byte - 1)]
    kept = [i for i, m in enumerate(members) if m.id not in removable]
    spans = []
    for i, member in enumerate(members):
        if member.id not in removable:
            continue
        if i < kept[-1]:
            spans.append((member.start_byte, members[i + 1].start_byte))
        elif i == kept[-1] + 1:
            # The trailing run goes from the last kept property on, so its
            # comma goes and a trailing comma after it stays.
            spans.append((members[kept[-1]].end_byte, members[-1].end_byte))
    return spans


def _whole_argument(literal: Node) -> tuple[int, int] | None:
    """The bytes leaving out ``literal``, the last argument, takes; ``None``
    when a comment sits among the arguments."""
    arguments = literal.parent
    passed = arguments.named_children
    if any(c.type == "comment" for c in passed):
        return None
    if len(passed) == 1:
        # Everything between the parentheses, so a trailing `,` goes too.
        return arguments.start_byte + 1, arguments.end_byte - 1
    return passed[-2].end_byte, literal.end_byte


def _defaults_to_empty_object(parameter: Node) -> bool:
    default = parameter.child_by_field_name("value")
    return default is not None and default.type == "object" and not default.named_children


def _call_parameter(literal: Node, resolver: _Resolver) -> tuple[Node, _Callee, bool] | None:
    """The parameter argument ``literal`` of a call to a resolved function is
    passed to, the function, and whether ``literal`` is the last argument."""
    arguments = literal.parent
    if arguments is None or arguments.type != "arguments":
        return None
    passed = [c for c in arguments.named_children if c.type != "comment"]
    position = next(i for i, c in enumerate(passed) if c.id == literal.id)
    if any(c.type == "spread_element" for c in passed[: position + 1]):
        return None
    call = arguments.parent
    name = call.child_by_field_name("function") if call.type == "call_expression" else None
    if name is None or name.type != "identifier":
        return None
    callee = resolver.function(name)
    if callee is None:
        return None
    members = [c for c in _parameters(callee.function) if c.type != "comment"]
    if position >= len(members):
        return None
    parameter = members[position]
    pattern = parameter.child_by_field_name("pattern")
    if parameter.type not in _PARAMETERS or pattern is None or pattern.type == "rest_pattern":
        return None
    return parameter, callee, position == len(passed) - 1


# ---------------------------------------------------------------------------
# The callee's side: destructured defaults and their types
# ---------------------------------------------------------------------------


def _destructured_equal(
    parameter: Node, name: str | None, value: Node, callee: _Callee, resolver: _Resolver, data: bytes
) -> bool:
    """Whether ``parameter`` destructures member ``name``, optional in its
    type, with a default ``value`` structurally equals."""
    pattern = parameter.child_by_field_name("pattern")
    if name is None or parameter.type not in _PARAMETERS or pattern is None or pattern.type != "object_pattern":
        return False
    members = _pattern_members(pattern, callee.data)
    if members is None or name not in members:
        return False
    if value.type == "undefined" and resolver.binds_undefined():
        # `undefined` is a name, and this file binds it to something else.
        return False
    if not _equal(value, data, members[name], callee.data):
        return False
    return callee.untyped or _optional_in_type(parameter, name, callee)


def _pattern_members(pattern: Node, data: bytes) -> dict[str, Node | None] | None:
    """Each member ``pattern`` destructures by name, with its default; ``None``
    when it holds a rest, a computed key or a name twice."""
    found: dict[str, Node | None] = {}
    for member in pattern.named_children:
        if member.type == "comment":
            continue
        if member.type == "object_assignment_pattern":
            left = member.child_by_field_name("left")
            if left is None or left.type != "shorthand_property_identifier_pattern":
                return None
            name, default = _text(left, data), member.child_by_field_name("right")
        elif member.type == "shorthand_property_identifier_pattern":
            name, default = _text(member, data), None
        elif member.type == "pair_pattern":
            name = _property_name(member.child_by_field_name("key"), data)
            target = member.child_by_field_name("value")
            if name is None or target is None:
                return None
            default = target.child_by_field_name("right") if target.type == "assignment_pattern" else None
        else:
            # `...rest`, or anything this pass does not model.
            return None
        if name in found:
            return None
        found[name] = default
    return found


def _optional_in_type(parameter: Node, name: str, callee: _Callee) -> bool:
    """Whether the parameter's annotation, if any, makes member ``name`` optional."""
    annotation = parameter.child_by_field_name("type")
    if annotation is None:
        return True
    types = [c for c in annotation.named_children if c.type != "comment"]
    if len(types) != 1:
        return False
    body = types[0]
    if body.type == "type_identifier":
        body = _local_object_type(_text(body, callee.data), callee.root, callee.data)
        if body is None:
            return False
    elif body.type != "object_type":
        return False
    members = [
        m
        for m in body.named_children
        if m.type == "property_signature"
        and _property_name(m.child_by_field_name("name"), callee.data) == name
    ]
    return len(members) == 1 and any(c.type == "?" for c in members[0].children)


#: Declarations that bind a type name.
_TYPE_DECLARATIONS = frozenset(
    {
        "interface_declaration",
        "type_alias_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "enum_declaration",
        "internal_module",
        "module",
    }
)


def _local_object_type(name: str, root: Node, data: bytes) -> Node | None:
    """The body of the one ``interface`` or object ``type`` called ``name``
    in the declaring file, when nothing else there declares or imports it."""
    found = [
        node
        for node in _walk(root)
        if node.type in _TYPE_DECLARATIONS
        and node.child_by_field_name("name") is not None
        and _text(node.child_by_field_name("name"), data) == name
    ]
    imported = any(
        leaf.type in ("identifier", "type_identifier") and _text(leaf, data) == name
        for statement in root.named_children
        if statement.type == "import_statement"
        for leaf in _walk(statement)
    )
    if imported or len(found) != 1:
        return None
    declaration = found[0]
    if declaration.child_by_field_name("type_parameters") is not None:
        return None
    if declaration.type == "interface_declaration":
        return declaration.child_by_field_name("body")
    if declaration.type == "type_alias_declaration":
        value = declaration.child_by_field_name("value")
        return value if value is not None and value.type == "object_type" else None
    return None


def _equal(value: Node, data: bytes, default: Node | None, default_data: bytes) -> bool:
    """Whether passing ``value`` gives the destructured member what leaving
    it out would (``default``, or ``undefined`` when there is none)."""
    if value.type == "undefined":
        # Destructuring treats an explicit `undefined` as absent.
        return True
    if default is None or value.type != default.type:
        return False
    if value.type in ("object", "array"):
        return not value.named_children and not default.named_children
    if value.type in ("true", "false", "null"):
        return True
    if value.type == "number":
        a, b = _number(_text(value, data)), _number(_text(default, default_data))
        return a is not None and a == b
    if value.type == "string":
        a, b = _text(value, data), _text(default, default_data)
        return "\\" not in a and "\\" not in b and a[1:-1] == b[1:-1]
    return False


def _number(text: str) -> float | None:
    if re.fullmatch(r"0[0-9]+", text):
        # `010` is 8 in a sloppy-mode script.
        return None
    text = text.replace("_", "")
    try:
        if text[:2].lower() in ("0x", "0o", "0b"):
            return float(int(text, 0))
        if text.endswith("n"):
            return None
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Resolving the callee
# ---------------------------------------------------------------------------


class _Resolver:
    """Finds the function a name in the caller's file calls, once per name."""

    def __init__(self, root: Node, data: bytes, path: str | None, repo_dir: str | None, language: str):
        self.root, self.data, self.path, self.repo_dir = root, data, path, repo_dir
        self.language = language
        self._functions: dict[tuple[str, bool], _Callee | None] = {}
        self._undefined: bool | None = None
        self._scanned: list[tuple[str, str, str]] | None = None
        self._props_readable: bool | None = None

    def binds_undefined(self) -> bool:
        if self._undefined is None:
            self._undefined = any(n.text == b"undefined" for n in binding_nodes(self.root))
        return self._undefined

    def function(self, name: Node) -> _Callee | None:
        """The function a call to ``name`` runs, when it is a plain function
        nothing in the checkout mocks."""
        callee = self._lookup(name, component=False)
        if callee is None or self._mocked(callee):
            return None
        return callee

    def component(self, tag: Node) -> _Callee | None:
        """The function component JSX ``tag`` renders, unwrapped from React's
        ``memo``/``forwardRef``, when no ``defaultProps`` can reach it, no
        mock replaces it, and nothing in the checkout reads an element's props."""
        callee = self._lookup(tag, component=True)
        if (
            callee is None
            or self._default_props_near(callee, _text(tag, self.data))
            or self._mocked(callee)
            or self._element_props_readable()
        ):
            return None
        return callee

    def _lookup(self, name: Node, component: bool) -> _Callee | None:
        key = (_text(name, self.data), component)
        if key not in self._functions:
            try:
                self._functions[key] = self._resolve(key[0], component)
            except (OSError, UnicodeDecodeError, RecursionError):
                self._functions[key] = None
        callee = self._functions[key]
        if callee is not None and callee.scope is not None and not _within(name, callee.scope):
            # Outside the block that binds it, the name is some other binding.
            return None
        return callee

    def _resolve(self, name: str, component: bool) -> _Callee | None:
        binder = _sole_binder(name, self.root, self.data)
        if binder is None:
            return None
        if binder.parent.type == "import_specifier":
            return self._imported(binder.parent, component)
        callee = _declared_callee(binder, self.root, self.data, self.path or "", component)
        if callee is None:
            return None
        scope = _binding_scope(binder)
        return replace(
            callee, names=frozenset({name}), scope=None if scope.type == "program" else scope
        )

    def _imported(self, specifier: Node, component: bool) -> _Callee | None:
        if self.path is None or self.repo_dir is None:
            return None
        statement = specifier
        while statement is not None and statement.type != "import_statement":
            statement = statement.parent
        if statement is None or _type_only(statement) or _type_only(specifier):
            return None
        imported = specifier.child_by_field_name("name")
        source = statement.child_by_field_name("source")
        if imported is None or imported.type != "identifier" or source is None:
            return None
        specifier_text = cross_file_ts._string_value(source)
        if specifier_text is None:
            return None
        local = specifier.child_by_field_name("alias") or imported
        return self._exported(
            self.path, specifier_text, _text(imported, self.data), component, (), {_text(local, self.data)}
        )

    def _exported(
        self,
        reader: str,
        specifier: str,
        name: str,
        component: bool,
        modules: tuple[str, ...],
        names: set[str],
    ) -> _Callee | None:
        """The function exported as ``name`` by the module ``specifier``
        names from ``reader``, followed through re-exports. ``modules`` and
        ``names`` are the chain so far."""
        if len(modules) > _MAX_HOPS:
            return None
        target = cross_file_ts.resolve_specifier(reader, specifier, self.repo_dir, solution=True)
        if target is None or target.endswith(_DECLARATION_FILES):
            return None
        resolved = Path(target).resolve()
        if not resolved.is_relative_to(Path(source_index.checkout_root(self.repo_dir)).resolve()):
            return None
        source = resolved.read_text(encoding="utf-8")
        root = _parse(source, _grammar(str(resolved)))
        if root.has_error:
            return None
        data = source.encode("utf-8")
        found: list[tuple[str, str | None]] = []
        for statement in root.named_children:
            if statement.type != "export_statement" or any(c.type == "default" for c in statement.children):
                # `export default function name() {}` exports `default`, not `name`.
                continue
            declaration = statement.child_by_field_name("declaration")
            if declaration is not None:
                if name in _declared_names(declaration, data):
                    found.append((name, None))
                continue
            clause = next((c for c in statement.named_children if c.type == "export_clause"), None)
            if clause is None:
                # `export * from`, which a name the module exports itself
                # shadows, and `export = …`.
                continue
            from_source = statement.child_by_field_name("source")
            for exported in clause.named_children:
                if exported.type != "export_specifier" or _type_only(exported):
                    continue
                local = exported.child_by_field_name("name")
                alias = exported.child_by_field_name("alias") or local
                if local is None or _text(alias, data) != name:
                    continue
                found.append(
                    (_text(local, data), cross_file_ts._string_value(from_source) if from_source else None)
                )
        if len(found) != 1:
            return None
        local, onward = found[0]
        modules, names = (*modules, str(resolved)), names | {name, local}
        if onward is not None:
            return self._exported(str(resolved), onward, local, component, modules, names)
        binder = _sole_binder(local, root, data)
        if binder is None or binder.parent.type == "import_specifier" or _binding_scope(binder).type != "program":
            # A module exports only what its top level binds.
            return None
        callee = _declared_callee(binder, root, data, str(resolved), component)
        if callee is None:
            return None
        return replace(callee, modules=frozenset(modules), names=frozenset(names))

    def _default_props_near(self, callee: _Callee, local_name: str) -> bool:
        """Whether any file in the checkout that spells the component's name,
        the caller's or the declaration's, also names a ``defaultProps``
        PROPERTY. A local called ``defaultProps``, a common test fixture
        name, sets nothing on a component, so it does not count."""
        names = {local_name, _declared_name(callee.function, callee.data)} - {None}
        files = [("", self.data.decode("utf-8"), self.language)] + self._scan()
        return any(
            any(n in text for n in names) and _names_default_props(text, grammar)
            for _path, text, grammar in files
        )

    def _element_props_readable(self) -> bool:
        """Whether anything in the checkout can read the props a JSX element
        carries, which the component's own destructuring does not govern:
        ``cloneElement``, ``Children``, ``x.props`` on anything but ``this``,
        ``.prop(…)``/``.props()``, or a shallow renderer."""
        if self._props_readable is None:
            files = [("", self.data.decode("utf-8"), self.language)] + self._scan()
            self._props_readable = any(
                _reads_element_props(text, grammar) for _path, text, grammar in files
            )
        return self._props_readable

    def _mocked(self, callee: _Callee) -> bool:
        """Whether a file in the checkout mocks a module on the callee's
        import chain, or spies on it by one of its names: a test that then
        asserts on the call or the props would see the argument go."""
        for path, text, grammar in self._scan():
            if "mock" not in text.lower() and "spyOn" not in text:
                continue
            for call in _walk(_parse(text, grammar)):
                if call.type != "call_expression":
                    continue
                verb = _called_name(call.child_by_field_name("function"))
                arguments = call.child_by_field_name("arguments")
                passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
                if verb in _MOCKERS and passed and callee.modules:
                    specifier = _module_specifier(passed[0])
                    target = (
                        cross_file_ts.resolve_specifier(path, specifier, self.repo_dir, solution=True)
                        if specifier is not None
                        else None
                    )
                    if target is not None and str(Path(target).resolve()) in callee.modules:
                        return True
                if verb == "spyOn" and len(passed) > 1:
                    if cross_file_ts._string_value(passed[1]) in callee.names:
                        return True
        return False

    def _scan(self) -> list[tuple[str, str, str]]:
        """The path, text and grammar of every TS/JS file in the checkout
        that spells anything the refusals above look for, read once per pass."""
        if self._scanned is None:
            self._scanned = []
            if self.repo_dir is not None:
                for listed in source_index.files(
                    self.repo_dir, (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
                ):
                    try:
                        text = listed.path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if any(needle in text for needle in _SCANNED_FOR):
                        path = str(listed.path)
                        self._scanned.append((path, text, _grammar(path)))
        return self._scanned


#: What a file must spell before :meth:`_Resolver._scan` keeps it.
_SCANNED_FOR = (
    "defaultProps",
    "cloneElement",
    "Children",
    ".props",
    ".prop(",
    "shallow",
    "mock",
    "Mock",
    "spyOn",
)

#: The calls that replace a module: jest's and vitest's.
_MOCKERS = frozenset({"mock", "doMock", "unstable_mockModule", "setMock"})

#: Packages whose renderer exposes the props of the elements it is handed.
_SHALLOW_RENDERERS = frozenset({"enzyme", "react-test-renderer/shallow", "react-shallow-renderer"})


def _called_name(callee: Node | None) -> str | None:
    """``mock`` for ``mock(…)``, ``vi.mock(…)`` or ``jest.mock(…)``."""
    if callee is None:
        return None
    if callee.type == "identifier":
        return callee.text.decode("utf-8")
    if callee.type == "member_expression":
        member = callee.child_by_field_name("property")
        return member.text.decode("utf-8") if member is not None else None
    return None


def _module_specifier(argument: Node) -> str | None:
    """The specifier ``'./m'`` or ``import('./m')`` names."""
    if argument.type == "call_expression":
        callee = argument.child_by_field_name("function")
        arguments = argument.child_by_field_name("arguments")
        if callee is None or callee.type != "import" or arguments is None or not arguments.named_children:
            return None
        argument = arguments.named_children[0]
    return cross_file_ts._string_value(argument)


def _reads_element_props(text: str, grammar: str) -> bool:
    """Whether ``text`` can read a JSX element's props; see
    :meth:`_Resolver._element_props_readable`."""
    if not any(n in text for n in ("cloneElement", "Children", ".props", ".prop(", "shallow")):
        return False
    nodes = list(_walk(_parse(text, grammar)))
    # A test's mock often keeps what it was rendered with in a holder of its
    # own (`captured.props = props`). React freezes an element, so nothing
    # whose `.props` the file assigns is one.
    holders = {
        _one_line(target.text.decode("utf-8"))
        for node in nodes
        if node.type in ("assignment_expression", "augmented_assignment_expression")
        and (left := node.child_by_field_name("left")) is not None
        and left.type == "member_expression"
        and (target := left.child_by_field_name("object")) is not None
        and (member := left.child_by_field_name("property")) is not None
        and member.text == b"props"
    }
    for node in nodes:
        if node.type in ("identifier", "property_identifier") and node.text in (b"cloneElement", b"Children"):
            return True
        if node.type == "member_expression":
            member = node.child_by_field_name("property")
            target = node.child_by_field_name("object")
            if (
                member is not None
                and member.text == b"props"
                and target is not None
                and target.type != "this"
                and _one_line(target.text.decode("utf-8")) not in holders
            ):
                return True
        if node.type == "call_expression" and _called_name(node.child_by_field_name("function")) in (
            "prop",
            "props",
        ) and node.child_by_field_name("function").type == "member_expression":
            return True
        if node.type == "import_statement":
            source = cross_file_ts._string_value(node.child_by_field_name("source"))
            if source in _SHALLOW_RENDERERS:
                return True
    return False


def _names_default_props(text: str, grammar: str) -> bool:
    """Whether ``text`` names ``defaultProps`` as a property: ``X.defaultProps``,
    a ``defaultProps`` key or shorthand, or the string ``'defaultProps'``."""
    if "defaultProps" not in text:
        return False
    for node in _walk(_parse(text, grammar)):
        if node.type in ("property_identifier", "shorthand_property_identifier"):
            if node.text == b"defaultProps":
                return True
        elif node.type == "string_fragment" and node.text == b"defaultProps":
            return True
    return False


def _grammar(path: str) -> str:
    return "ts" if path.endswith((".ts", ".mts", ".cts")) else "tsx"


def _sole_binder(name: str, root: Node, data: bytes) -> Node | None:
    """The one leaf binding ``name`` in this file, a declaration
    :func:`_binding_scope` accepts or a named import, when every other
    mention of the name is a call to it, a JSX tag naming it or an
    ``export { … }`` of it."""
    found = None
    seen: set[int] = set()
    for leaf in _walk(root):
        if leaf.type not in _NAME_LEAVES or _text(leaf, data) != name:
            continue
        seen.add(leaf.id)
        holder = leaf.parent
        if holder is None:
            return None
        if _binding_scope(leaf) is not None:
            if found is not None:
                return None
            found = leaf
            continue
        if holder.type == "call_expression" and _same(holder.child_by_field_name("function"), leaf):
            continue
        if holder.type in _JSX_TAGS and _same(holder.child_by_field_name("name"), leaf):
            continue
        if holder.type == "export_specifier":
            # `export { name }`, or the outward name of `export { a as name }`.
            continue
        if (
            holder.type == "import_specifier"
            and holder.child_by_field_name("alias") is not None
            and _same(holder.child_by_field_name("name"), leaf)
        ):
            # `import { name as other }`: the other module's name, not a
            # binding here.
            continue
        return None
    if found is None or any(
        n.id not in seen and _text(n, data) == name and not _within_import(n)
        for n in binding_nodes(root)
    ):
        # A binding the walk above does not see: a nested `class name`, whose
        # name is a type identifier.
        return None
    return found


def _within_import(node: Node) -> bool:
    while node is not None:
        if node.type == "import_statement":
            return True
        node = node.parent
    return False


def _binding_scope(leaf: Node) -> Node | None:
    """The scope ``leaf`` binds its name in when it names a top-level
    ``function``, a ``const`` or ``let`` at the top level or in a block, or
    a named import's local binding; ``None`` for anything else."""
    holder = leaf.parent
    if holder.type == "import_specifier":
        local = holder.child_by_field_name("alias") or holder.child_by_field_name("name")
        return _program(leaf) if _same(local, leaf) else None
    if not _same(holder.child_by_field_name("name"), leaf):
        return None
    lexical = False
    if holder.type == "function_declaration":
        statement = holder
    elif holder.type == "variable_declarator":
        statement = holder.parent
        if statement is None or statement.type != "lexical_declaration" or not statement.text.startswith(
            (b"const", b"let")
        ):
            return None
        lexical = True
    else:
        return None
    scope = statement.parent
    if scope is not None and scope.type == "export_statement":
        if statement.text.startswith(b"let"):
            # An exported `let` is a live binding its module may re-assign.
            return None
        scope = scope.parent
    if scope is None:
        return None
    if scope.type == "program" or lexical and scope.type == "statement_block":
        # A `function` in a block is hoisted out of it in a sloppy-mode
        # script, so only a lexical binding is scoped by its block.
        return scope
    return None


def _program(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


def _within(node: Node, scope: Node) -> bool:
    return scope.start_byte <= node.start_byte and node.end_byte <= scope.end_byte


def _declared_callee(binder: Node, root: Node, data: bytes, path: str, component: bool) -> _Callee | None:
    """The function ``binder`` declares, unwrapped from React's wrappers for
    a component, when nothing about it can tell a call without the argument
    from a call with it."""
    holder = binder.parent
    memoized = False
    if holder.type == "function_declaration":
        function = statement = holder
    else:
        if holder.child_by_field_name("type") is not None:
            # `const C: FC<Props> = …` types the parameter from outside it.
            return None
        function = holder.child_by_field_name("value")
        statement = holder.parent
        if component:
            memoized = _wrapped_in(function, root, data, "memo")
            function = _unwrapped(function, root, data)
        if function is None or function.type not in _FUNCTION_VALUES:
            return None
    if function.child_by_field_name("type_parameters") is not None:
        return None
    if any(
        (pattern := m.child_by_field_name("pattern")) is not None and pattern.type == "this"
        for m in _parameters(function)
    ):
        return None
    if any(n.type == "identifier" and n.text == b"arguments" for n in _walk(function)):
        return None
    untyped = path.endswith((".js", ".jsx", ".mjs", ".cjs"))
    if untyped and _jsdoc_typed(function, statement):
        # With `allowJs`, TypeScript checks calls against JSDoc types.
        return None
    return _Callee(function, root, data, untyped, memoized)


#: The JSDoc tags that type a function or its parameters.
_JSDOC_TYPES = re.compile(r"@(param|arg|argument|type|typedef|callback|template|satisfies)\b")


def _jsdoc_typed(function: Node, statement: Node) -> bool:
    """Whether a JSDoc tag in a comment right above the declaration, or among
    the parameters, can type the call."""
    if statement.parent is not None and statement.parent.type == "export_statement":
        statement = statement.parent
    comments = [c for c in _walk(function.child_by_field_name("parameters") or function) if c.type == "comment"]
    previous = statement.prev_named_sibling
    while previous is not None and previous.type == "comment":
        comments.append(previous)
        previous = previous.prev_named_sibling
    return any(_JSDOC_TYPES.search(c.text.decode("utf-8")) for c in comments)


def _wrapped_in(value: Node | None, root: Node, data: bytes, wrapper: str) -> bool:
    """Whether React's ``wrapper`` is one of the calls around ``value``."""
    while value is not None and value.type == "call_expression":
        if _react_wrapper(value.child_by_field_name("function"), root, data) == wrapper:
            return True
        arguments = value.child_by_field_name("arguments")
        value = arguments.named_children[0] if arguments is not None and arguments.named_children else None
    return False


def _unwrapped(value: Node | None, root: Node, data: bytes) -> Node | None:
    """``value`` with every ``memo(…)``/``forwardRef(…)`` from React peeled off."""
    while value is not None and value.type == "call_expression":
        arguments = value.child_by_field_name("arguments")
        passed = [c for c in arguments.named_children if c.type != "comment"] if arguments else []
        if (
            len(passed) != 1
            or value.child_by_field_name("type_arguments") is not None
            or _react_wrapper(value.child_by_field_name("function"), root, data) is None
        ):
            # `forwardRef<El, Props>(…)` types the props from outside the parameter.
            return None
        value = passed[0]
    return value


def _react_wrapper(callee: Node | None, root: Node, data: bytes) -> str | None:
    """``memo`` or ``forwardRef`` when ``callee`` is React's, else ``None``."""
    if callee is None:
        return None
    if callee.type == "identifier":
        name = _text(callee, data)
        if name not in _REACT_WRAPPERS:
            return None
        binder = _import_from_react(name, root, data, named=True)
        return name if binder == name else None
    if callee.type == "member_expression":
        target = callee.child_by_field_name("object")
        member = callee.child_by_field_name("property")
        if target is None or target.type != "identifier" or member is None:
            return None
        name = _text(member, data)
        if name not in _REACT_WRAPPERS:
            return None
        return name if _import_from_react(_text(target, data), root, data, named=False) else None
    return None


def _import_from_react(local: str, root: Node, data: bytes, named: bool) -> str | None:
    """The name ``local`` imports from ``react`` (the default or namespace
    when not ``named``), when the file binds ``local`` nowhere else."""
    if sum(1 for n in binding_nodes(root) if _text(n, data) == local) > 1:
        return None
    for statement in root.named_children:
        if statement.type != "import_statement" or _type_only(statement):
            continue
        if cross_file_ts._string_value(statement.child_by_field_name("source")) != "react":
            continue
        for node in _walk(statement):
            if named and node.type == "import_specifier" and not _type_only(node):
                bound = node.child_by_field_name("alias") or node.child_by_field_name("name")
                if bound is not None and _text(bound, data) == local:
                    return _text(node.child_by_field_name("name"), data)
            if not named and node.type == "namespace_import":
                if any(_text(c, data) == local for c in node.named_children):
                    return local
            if not named and node.type == "import_clause":
                default = next((c for c in node.named_children if c.type == "identifier"), None)
                if default is not None and _text(default, data) == local:
                    return local
    return None


def _declared_names(declaration: Node, data: bytes) -> list[str]:
    if declaration.type == "function_declaration":
        name = declaration.child_by_field_name("name")
        return [_text(name, data)] if name is not None else []
    if declaration.type == "lexical_declaration":
        return [
            _text(d.child_by_field_name("name"), data)
            for d in declaration.named_children
            if d.type == "variable_declarator" and d.child_by_field_name("name") is not None
        ]
    return []


def _declared_name(function: Node, data: bytes) -> str | None:
    """The name ``function`` is declared under, for a declaration or a ``const``."""
    if function.type == "function_declaration":
        name = function.child_by_field_name("name")
        return _text(name, data) if name is not None else None
    holder = function.parent
    while holder is not None and holder.type != "variable_declarator":
        holder = holder.parent
    name = holder.child_by_field_name("name") if holder is not None else None
    return _text(name, data) if name is not None else None


def _type_only(node: Node) -> bool:
    """Whether an import statement or specifier is ``type``-only."""
    return any(c.type == "type" for c in node.children)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _parameters(function: Node) -> list[Node]:
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        # `x => …`: a single unparenthesised parameter.
        parameter = function.child_by_field_name("parameter")
        return [] if parameter is None else [parameter]
    return list(parameters.named_children)


def _property_name(key: Node | None, data: bytes) -> str | None:
    if key is None:
        return None
    if key.type in ("property_identifier", "shorthand_property_identifier_pattern"):
        return _text(key, data)
    if key.type == "string":
        text = _text(key, data)
        return None if "\\" in text else text[1:-1]
    return None


def _same(a: Node | None, b: Node | None) -> bool:
    return a is not None and b is not None and a.id == b.id


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")
