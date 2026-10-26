"""Prove that a Dart field a constructor sets can go with what sets it.

:mod:`flag_cleanup.dart_field_removal` finds a private field the fold left
unread. When a constructor of its class sets it, as a ``this._client``
parameter or as an initializer-list entry ``: _client = client``, deleting the
field means editing every constructor that sets it, and when a parameter goes,
the argument every caller passes for it. This module proves both.

**The constructors.** Every write of the field must be one of:

- a ``this._client`` positional parameter (a named one needs an experimental
  language feature, and refuses). It goes, with ``[...]`` around it when it
  was the group's only member;
- an initializer-list entry. It goes, with the list's ``:`` when it was the
  only entry. When its right side is a plain parameter of the same
  constructor that nothing else in the constructor spells, that parameter
  goes too, with its ``required`` and its default value, or the whole
  ``{...}``/``[...]`` group when it was its only member. A right side that
  is a literal or a ``this.`` parameter goes alone; anything else refuses,
  since it may do work.

**The callers,** for each constructor that loses a parameter. Dart resolves a
class by name, so the proof is by name, as for TypeScript: every spelling of
the class in the files that can see it must be one of a short list of shapes,
and anything else refuses. A library-private class (``_Svc``) can be seen by
its own file only. Any other class by every Dart file in the checkout that
imports its library, directly or through a library of the checkout that
exports it (a file that does not names some other class of that name), and it
refuses when its package may be published (a public class in ``lib/`` of a
``pubspec.yaml`` without ``publish_to: none``), since a package depending on
this one may construct it.

Allowed: a call of the constructor, ``Svc(…)``, ``Svc.named(…)``,
``Svc.new(…)``, ``new``/``const`` and an import prefix included; an access of
a static member (``Svc.instance``); a type position; ``implements`` and
``with``; a ``show``/``hide`` combinator; and ``extends Svc`` from a class
whose every constructor reaches this one through ``super(…)``/``super.named(…)``
or not at all. A redirection ``: this(…)`` inside the class is a caller too.
Refused: a tear-off (``Svc.new`` or ``Svc.named`` not called), the class as a
value (a ``Type`` literal), a ``typedef`` naming it, a part file spelling it,
an import whose library cannot be told, a redirecting factory, a mixin application
(``class B = Svc with M;``), a subclass with a ``super.`` parameter, any file
importing ``dart:mirrors``, an annotation on the class, a constructor that
changes, or a parameter that goes (a code generator or an injector may
construct it), a class name declared twice, and a file that does not parse.

Each argument that goes must do no work when skipped, and strand nothing: a
literal built of literals, a ``const`` object, a call of a constructor that
builds the object and does nothing else (:func:`const_construction`), a bare
name that is a parameter of a function around the call or a variable of the
file whose initializer is inert (:func:`_inert_name`), or ``widget.f`` in a
Flutter ``State`` where ``f`` is provably a plain field of the widget
(:func:`_widget_field`). Any other property may be a getter, a lazily
initialised variable with an initializer that may do work runs it on its
first read, and a local left unread is ``unused_local_variable``. Every name
it spells must be spelled elsewhere in its file too. A
positional argument is found by its index among the positional ones, a named
one by its label. A call that passes fewer positional arguments than the index
passes nothing to delete.

Everything here reads the gate's grammar (``tree-sitter-dart`` 0.1.0), in which
a call is a name followed by ``selector`` siblings. The deletions are marked
(see :data:`flag_cleanup.ctor_param_model.FORMS`) and made by the engine.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index
from flag_cleanup.ctor_param_model import Refusal, Site
from flag_cleanup.unused import _parse

_CONSTRUCTORS = frozenset(
    {
        "constructor_signature",
        "constant_constructor_signature",
        "factory_constructor_signature",
        "redirecting_factory_constructor_signature",
    }
)
_GENERATIVE = frozenset({"constructor_signature", "constant_constructor_signature"})
_TYPE_DECLARATIONS = frozenset(
    {
        "class_definition",
        "enum_declaration",
        "mixin_declaration",
        "extension_declaration",
        "extension_type_declaration",
        "type_alias",
        "mixin_application_class",
    }
)
#: Literal leaves that name nothing and do nothing.
_LITERALS = frozenset(
    {
        "decimal_integer_literal",
        "decimal_floating_point_literal",
        "hex_integer_literal",
        "true",
        "false",
        "null_literal",
    }
)
_COLLECTIONS = frozenset({"list_literal", "set_or_map_literal"})
#: Annotations that say nothing about who constructs a class.
_INERT_ANNOTATIONS = frozenset({"deprecated", "Deprecated", "immutable"})
_PUBLISH_NONE = re.compile(r"^publish_to:\s*['\"]?none['\"]?\s*$", re.MULTILINE)


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _where(path: str, node: Node) -> str:
    return f"{path}:{node.start_point[0] + 1}"


@dataclass(frozen=True)
class Edits:
    """What the constructors lose: the sites in the declaring file, and for
    each constructor (``""`` for the unnamed one) the parameter its callers
    stop passing, as ``("positional", index)`` or ``("named", name)``."""

    sites: tuple[Site, ...]
    dropped: dict[str, tuple[str, int | str]]
    #: A removed parameter's name, for the pull request.
    parameter: str | None


# --- the constructors -------------------------------------------------------


def _constructor_name(signature: Node) -> str:
    """``""`` for ``Svc(…)`` and ``Svc.new(…)``, ``named`` for ``Svc.named(…)``."""
    names = [c for c in signature.children if c.type == "identifier"]
    name = _text(names[1]) if len(names) > 1 else ""
    return "" if name == "new" else name


def constructors(body: Node) -> list[tuple[Node, Node]]:
    """``(signature, member)`` for each constructor of a class body: the
    member is the ``declaration`` or ``method_signature`` holding it."""
    found = []
    for member in body.named_children:
        if member.type not in ("declaration", "method_signature"):
            continue
        signature = next((c for c in member.named_children if c.type in _CONSTRUCTORS), None)
        if signature is not None:
            found.append((signature, member))
    return found


def _extent(member: Node) -> list[Node]:
    """The member and, for one with a ``{ … }`` body, the body beside it."""
    nodes = [member]
    following = member.next_named_sibling
    if member.type == "method_signature" and following is not None and following.type == "function_body":
        nodes.append(following)
    return nodes


def _meaningful(annotation: Node) -> bool:
    name = annotation.child_by_field_name("name")
    return name is None or _text(name) not in _INERT_ANNOTATIONS


def _annotated(node: Node) -> bool:
    """Whether ``node`` carries an annotation that may say how it is
    constructed: one directly before it among its siblings (a member), or
    among its own children (a class)."""
    if any(c.type == "annotation" and _meaningful(c) for c in node.children):
        return True
    previous = node.prev_named_sibling
    while previous is not None and previous.type in ("comment", "documentation_comment"):
        previous = previous.prev_named_sibling
    return previous is not None and previous.type == "annotation" and _meaningful(previous)


def _parameters(signature: Node) -> list[tuple[Node, Node | None]]:
    """``(formal_parameter, group)`` in order; ``group`` is the ``[...]`` or
    ``{...}`` holding it."""
    plist = next((c for c in signature.named_children if c.type == "formal_parameter_list"), None)
    found: list[tuple[Node, Node | None]] = []
    for child in plist.named_children if plist is not None else ():
        if child.type == "formal_parameter":
            found.append((child, None))
        elif child.type == "optional_formal_parameters":
            found += [(c, child) for c in child.named_children if c.type == "formal_parameter"]
    return found


def _parameter_name(parameter: Node) -> str | None:
    """A plain parameter's name; ``None`` for ``this.x`` and ``super.x``."""
    if any(c.type in ("constructor_param", "super_formal_parameter") for c in parameter.named_children):
        return None
    names = [c for c in parameter.named_children if c.type == "identifier"]
    return _text(names[-1]) if names else None


def _parameter_span(parameter: Node, group: Node | None) -> tuple[int, int]:
    """The bytes a parameter takes: its ``required`` and default value, or
    its whole group when it is the group's only member."""
    if group is not None:
        members = [c for c in group.named_children if c.type == "formal_parameter"]
        if members == [parameter]:
            return group.start_byte, group.end_byte
    start, end = parameter.start_byte, parameter.end_byte
    previous = parameter.prev_sibling
    if previous is not None and previous.type == "required":
        start = previous.start_byte
    following = parameter.next_sibling
    if following is not None and following.type == "=":
        value = following.next_sibling
        while value is not None and value.type not in (",", "]", "}"):
            end = value.end_byte
            value = value.next_sibling
    return start, end


def _dropped(parameters: list[tuple[Node, Node | None]], parameter: Node, group: Node | None):
    """How a caller passes ``parameter``: by position, or by name."""
    if group is not None and group.children and group.children[0].type == "{":
        return "named", _parameter_name(parameter) or ""
    positional = [p for p, g in parameters if g is None or (g.children and g.children[0].type == "[")]
    return "positional", positional.index(parameter)


def _literal(nodes: list[Node]) -> bool:
    """Whether ``nodes`` (an expression, as a run of siblings) is a literal
    built of literals: it names nothing and does nothing."""
    if len(nodes) != 1:
        return False
    node = nodes[0]
    if node.type in _LITERALS:
        return True
    if node.type == "string_literal":
        return not any(c.type == "template_substitution" for c in node.named_children)
    if node.type == "unary_expression":
        named = node.named_children
        return len(named) == 2 and _text(named[0]) == "-" and named[1].type in _LITERALS
    if node.type in _COLLECTIONS:
        for item in node.named_children:
            if item.type in ("comment", "type_arguments", "const_builtin"):
                continue
            parts = item.named_children if item.type == "pair" else [item]
            if not all(_literal([p]) for p in parts if p.type != "comment"):
                return False
        return not any(c.type == "type_arguments" for c in node.named_children)
    return False


def plan_constructors(
    path: str, cls: Node, field: str, writes: list[tuple[str, Node]]
) -> Edits | str:
    """The edits that take the field ``field`` out of every constructor of
    ``cls`` that sets it, or why they cannot be made. ``writes`` is every
    spelling of the field outside its declaration, each a ``"parameter"``
    (``this._x``) or an ``"initializer"`` entry."""
    body = next((c for c in cls.named_children if c.type == "class_body"), None)
    ctors = constructors(body) if body is not None else []
    sites: list[Site] = []
    dropped: dict[str, tuple[str, int | str]] = {}
    parameter_name: str | None = None
    for kind, node in writes:
        owner = next(
            (
                (signature, member)
                for signature, member in ctors
                if any(n.start_byte <= node.start_byte < n.end_byte for n in _extent(member))
            ),
            None,
        )
        if owner is None:
            return "set somewhere other than one of its class's constructors"
        signature, member = owner
        if signature.type not in _GENERATIVE:
            return "set by a factory constructor"
        name = _constructor_name(signature)
        if _annotated(member):
            return "set by an annotated constructor"
        parameters = _parameters(signature)
        if kind == "parameter":
            parameter = node.parent.parent  # constructor_param -> formal_parameter
            group = next((g for p, g in parameters if p == parameter), None)
            if parameter.type != "formal_parameter" or (
                group is not None and group.children and group.children[0].type == "{"
            ):
                return "set by a named `this.` parameter"
            if any(c.type == "annotation" for c in _walk(parameter)):
                return "set by an annotated parameter"
            if name in dropped:
                return "a constructor sets it twice"
            sites.append(Site(path, *_parameter_span(parameter, group), "parameter"))
            dropped[name] = _dropped(parameters, parameter, group)
            parameter_name = field
            continue
        entry = node.parent.parent  # field_initializer -> initializer_list_entry
        if entry is None or entry.type != "initializer_list_entry":
            return "set somewhere other than one of its class's constructors"
        sites.append(Site(path, entry.start_byte, entry.end_byte, "initializer"))
        after = [c.type for c in node.parent.children].index("=") + 1
        value = [c for c in node.parent.children[after:] if c.is_named and c.type != "comment"]
        if _literal(value):
            continue
        if len(value) != 1 or value[0].type != "identifier":
            return "set in an initializer list from something other than a parameter or a literal"
        wanted = _text(value[0])
        found = next(((p, g) for p, g in parameters if _parameter_name(p) == wanted), None)
        if found is None:
            fields = [
                p for p, _ in parameters
                if any(c.type == "constructor_param" and _text(c.named_children[-1]) == wanted
                       for c in p.named_children)
            ]
            if fields:
                continue  # `this.x`, which still sets its own field
            return "set in an initializer list from something other than a parameter or a literal"
        parameter, group = found
        others = [
            n
            for extent in _extent(member)
            for n in _walk(extent)
            if n.type in ("identifier", "identifier_dollar_escaped")
            and _text(n).lstrip("$") == wanted
            and n != value[0]
            and not (parameter.start_byte <= n.start_byte < parameter.end_byte)
        ]
        if others:
            continue  # the parameter is still read: only the entry goes
        if any(c.type == "annotation" for c in _walk(parameter)):
            return "its parameter is annotated"
        if name in dropped:
            return "a constructor sets it twice"
        sites.append(Site(path, *_parameter_span(parameter, group), "parameter"))
        dropped[name] = _dropped(parameters, parameter, group)
        parameter_name = wanted
    return Edits(tuple(sites), dropped, parameter_name)


# --- the callers -------------------------------------------------------------


def published(path: str) -> bool:
    """Whether a public declaration in ``path`` may be used by a package
    outside the repository: it is in ``lib/`` of a package that does not say
    ``publish_to: none``, or no ``pubspec.yaml`` encloses it."""
    file = Path(path).resolve()
    home = next((d for d in file.parents if (d / "pubspec.yaml").is_file()), None)
    if home is None:
        return True
    if not file.is_relative_to(home / "lib"):
        return False
    try:
        return _PUBLISH_NONE.search((home / "pubspec.yaml").read_text(encoding="utf-8")) is None
    except (OSError, UnicodeDecodeError):
        return True


def _member_names(body: Node) -> set[str]:
    """Every name a non-constructor member of a class body declares."""
    names: set[str] = set()
    for member in body.named_children:
        for node in _walk(member):
            if node.type in ("function_signature", "getter_signature", "setter_signature"):
                names |= {_text(c) for c in node.named_children[:3] if c.type == "identifier"}
            elif node.type in ("initialized_identifier", "static_final_declaration"):
                if node.named_children:
                    names.add(_text(node.named_children[0]))
    return names


def _chain(node: Node) -> list[Node]:
    """The ``selector`` siblings after ``node`` in its postfix chain."""
    out = []
    following = node.next_sibling
    while following is not None and following.type == "selector":
        out.append(following)
        following = following.next_sibling
    return out


def _call_arguments(selector: Node | None) -> Node | None:
    if selector is None:
        return None
    part = next((c for c in selector.named_children if c.type == "argument_part"), None)
    return next((c for c in part.named_children if c.type == "arguments"), None) if part else None


def _selected(selector: Node) -> str | None:
    """``x`` for a ``.x`` selector, else ``None``."""
    access = next((c for c in selector.named_children if c.type == "unconditional_assignable_selector"), None)
    name = next((c for c in access.named_children if c.type == "identifier"), None) if access else None
    return _text(name) if name is not None else None


@dataclass
class _Scan:
    class_name: str
    ctor_names: set[str]
    members: set[str]
    #: ``(path, constructor name, arguments node, root)``.
    calls: list[tuple[str, str, Node]]
    #: ``(path, class_definition)`` of each class extending this one.
    subclasses: list[tuple[str, Node]]
    #: Whether the file being read sees the class under its own name, and
    #: the import prefixes that bring it in.
    bare: bool = True
    prefixes: set[str] = frozenset()


_CONSTRUCTIONS = frozenset({"new_expression", "const_object_expression", "constructor_invocation"})


def _next_identifier(node: Node) -> str:
    """The constructor name an ``identifier`` after ``node`` spells, or ``""``."""
    following = node.next_named_sibling
    while following is not None and following.type == "type_arguments":
        following = following.next_named_sibling
    name = _text(following) if following is not None and following.type == "identifier" else ""
    return "" if name == "new" else name


def _classify(path: str, node: Node, scan: _Scan) -> str | None:
    """Record what the spelling ``node`` of the class does, or say why it is
    not one of the allowed shapes. A spelling this file cannot resolve to the
    class (a bare one with no import of it, or through a prefix that does not
    import it) names something else, and is passed over."""
    parent = node.parent
    where = _where(path, node)
    if parent is None:
        return f"{where}: an unexpected spelling"
    if node.type == "identifier_dollar_escaped":
        return f"{where}: used as a value" if scan.bare else None
    if any(a.type == "type_alias" for a in _ancestors(node)):
        return f"{where}: named by a type alias" if scan.bare else None
    before = node.prev_named_sibling
    prefixed = before is not None and before.type == "type_identifier"
    if parent.type in _CONSTRUCTIONS or parent.type in ("superclass", "constructor_tearoff"):
        if node.type == "identifier" and not (prefixed and _text(before) in scan.prefixes):
            # `const Other.Svc()`: a constructor of another class named alike.
            return None if parent.type != "superclass" else f"{where}: an unexpected spelling"
        if prefixed and _text(before) not in scan.prefixes:
            return None
        if not prefixed and not scan.bare:
            return None
    if parent.type == "constructor_tearoff":
        return f"{where}: a constructor torn off"
    if parent.type in _CONSTRUCTIONS:
        arguments = next((c for c in parent.named_children if c.type == "arguments"), None)
        if arguments is None:
            return f"{where}: an unexpected construction"
        scan.calls.append((path, _next_identifier(node), arguments))
        return None
    if parent.type == "superclass":
        owner = parent.parent
        if owner is None or owner.type != "class_definition":
            return f"{where}: extended by something other than a class"
        scan.subclasses.append((path, owner))
        return None
    if parent.type in ("redirecting_factory_constructor_signature", "mixin_application"):
        return f"{where}: a redirecting factory or a mixin application" if scan.bare or prefixed else None
    if node.type == "type_identifier":
        return None  # a type position, `implements`, `with`
    if parent.type in ("combinator", "show_combinator", "hide_combinator"):
        return None
    head = node
    if parent.type == "unconditional_assignable_selector":
        # `prefix.Svc(…)`: the selector holding the name follows the prefix.
        selector = parent.parent
        prefix = selector.prev_sibling if selector is not None else None
        if (
            selector is None
            or selector.type != "selector"
            or prefix is None
            or prefix.type != "identifier"
            or (prefix.prev_sibling is not None and prefix.prev_sibling.type == "selector")
        ):
            return None  # `x.y.Svc`: a member of something, not this class
        if _text(prefix) not in scan.prefixes:
            return None
        head = selector
    elif node.type != "identifier":
        return f"{where}: an unexpected spelling"
    elif not scan.bare:
        return None
    chain = _chain(head)
    if not chain:
        return f"{where}: used as a value"
    arguments = _call_arguments(chain[0])
    if arguments is not None:
        scan.calls.append((path, "", arguments))
        return None
    selected = _selected(chain[0])
    if selected is None:
        return f"{where}: an unexpected access"
    if selected == "new" or selected in scan.ctor_names:
        arguments = _call_arguments(chain[1]) if len(chain) > 1 else None
        if arguments is None:
            return f"{where}: a constructor torn off"
        scan.calls.append((path, "" if selected == "new" else selected, arguments))
        return None
    if selected in scan.members:
        return None  # a static member
    return f"{where}: `{scan.class_name}.{selected}` is neither a constructor nor a member"


def _ancestors(node: Node) -> Iterator[Node]:
    while node.parent is not None:
        node = node.parent
        yield node


def _visibility(file: str, tree: Node, declaring: str, class_name: str, repo_dir: str):
    """``(bare, prefixes)``: whether ``tree`` sees the class under its own
    name, and the import prefixes that bring it in; or why that cannot be
    told. Seen through an import of the declaring library, or of a library
    of the checkout whose names (exports followed) include it."""
    from flag_cleanup import dart_namespace

    if str(Path(file).resolve()) == str(Path(declaring).resolve()):
        return True, set()
    if any(c.type == "part_of_directive" for c in tree.named_children):
        return f"{file}: a part file spells {class_name}"
    root = Path(repo_dir).resolve()
    resolve = dart_namespace.resolver(file, str(root))
    target = Path(declaring).resolve()
    bare = False
    prefixes: set[str] = set()
    for spec in (n for n in _walk(tree) if n.type in ("import_specification", "library_export")):
        uri = dart_namespace.uri_of(spec)
        if uri is None:
            if spec.type == "import_specification":
                return f"{_where(file, spec)}: an import whose library cannot be told"
            continue
        located = dart_namespace.locate(uri, Path(file).resolve(), root)
        if located is None:
            continue  # `dart:` or another package: it cannot reach this file
        if located != target:
            namespace = resolve(uri)
            if namespace is None:
                return f"{_where(file, spec)}: an import whose names cannot be told"
            if class_name not in namespace.names:
                continue
        shown: set[str] | None = None
        hidden: set[str] = set()
        for combinator in (c for c in spec.named_children if c.type == "combinator"):
            names = {_text(c) for c in combinator.named_children if c.type == "identifier"}
            if combinator.children and combinator.children[0].type == "show":
                shown = names if shown is None else shown & names
            else:
                hidden |= names
        if class_name in hidden or (shown is not None and class_name not in shown):
            continue
        prefix = next(
            (c for c in spec.named_children if c.type == "identifier" and c.prev_sibling is not None and c.prev_sibling.type == "as"),
            None,
        )
        if prefix is not None:
            prefixes.add(_text(prefix))
        elif spec.type == "import_specification":
            bare = True
    return bare, prefixes


def _files(path: str, class_name: str, repo_dir: str) -> list[str] | str:
    if class_name.startswith("_"):
        return [path]
    if published(path):
        return "its package may be published, and code outside the repository may construct it"
    return [str(f.path) for f in source_index.files(repo_dir, (".dart",), with_ignored=True)]


def _super_calls(path: str, subclass: Node, scan: _Scan) -> str | None:
    """Record each ``super(…)`` of a subclass's constructors as a caller."""
    body = next((c for c in subclass.named_children if c.type == "class_body"), None)
    for signature, member in constructors(body) if body is not None else []:
        if any(n.type == "super_formal_parameter" for n in _walk(signature)):
            return f"{_where(path, signature)}: a subclass passes a `super.` parameter"
        for n in _walk(member):
            if n.type == "initializer_list_entry" and n.children and n.children[0].type == "super":
                names = [c for c in n.named_children if c.type == "identifier"]
                arguments = next((c for c in n.named_children if c.type == "arguments"), None)
                if arguments is None:
                    return f"{_where(path, n)}: an unexpected `super` call"
                scan.calls.append((path, _text(names[0]) if names else "", arguments))
    return None


def _redirections(path: str, body: Node, scan: _Scan) -> None:
    for member in body.named_children:
        for n in _walk(member):
            if n.type == "redirection":
                names = [c for c in n.named_children if c.type == "identifier"]
                arguments = next((c for c in n.named_children if c.type == "arguments"), None)
                if arguments is not None:
                    scan.calls.append((path, _text(names[0]) if names else "", arguments))


#: Where a name is bound other than as a parameter. Any ``*_pattern``
#: ancestor binds as well (``case Foo c``, ``var (a, b) = …``).
_BINDERS = frozenset(
    {
        "initialized_variable_definition",
        "initialized_identifier",
        "static_final_declaration",
        "for_loop_parts",
        "catch_parameters",
        "function_signature",
    }
)
#: The binders above that declare one variable, its name first.
_DECLARATORS = frozenset({"initialized_variable_definition", "initialized_identifier", "static_final_declaration"})
#: Holders of a parameter list whose names are in scope below them.
_FUNCTIONS = frozenset(
    {"function_expression", "declaration", "method_signature", "function_signature", "lambda_expression"}
)


def _own_parameters(holder: Node) -> set[str]:
    """The parameter names ``holder`` declares itself, not a nested
    closure's."""
    names: set[str] = set()
    stack = list(holder.named_children)
    while stack:
        current = stack.pop()
        if current.type in ("function_body", "function_expression_body", "function_expression", "block"):
            continue
        if current.type == "formal_parameter":
            name = _parameter_name(current)
            if name is not None:
                names.add(name)
            continue
        stack.extend(current.named_children)
    return names


def _member(node: Node) -> Node:
    """The top-level declaration or type member enclosing ``node``."""
    while node.parent is not None and node.parent.type not in ("program", "class_body", "enum_body", "extension_body"):
        node = node.parent
    return node


def _bound_in_member(argument: Node, name: str) -> bool:
    """Whether the member around ``argument`` binds ``name`` other than as
    the enclosing function's own name or a parameter: the argument may then
    name that binding instead."""
    member = _member(argument)
    siblings = [member]
    following = member.next_named_sibling
    if member.type in ("method_signature", "function_signature") and following is not None and following.type == "function_body":
        siblings.append(following)
    for top in siblings:
        for n in _walk(top):
            if n.type != "identifier" or _text(n) != name:
                continue
            if n.parent is not None and (
                n.parent.type in _BINDERS or any(a.type.endswith("_pattern") for a in _ancestors(n))
            ):
                if n.parent is member:
                    continue  # the enclosing function's own name
                if n.parent.type in _DECLARATORS and n != next(
                    (c for c in n.parent.named_children if c.type == "identifier"), None
                ):
                    continue  # read in an initializer, not the name it declares
                return True
    return False


def _enclosing_parameters(argument: Node) -> set[str]:
    """The parameter names of every function around ``argument``."""
    scope: set[str] = set()
    current = argument
    while current.parent is not None:
        if current.parent.type in _FUNCTIONS:
            scope |= _own_parameters(current.parent)
        if current.type == "function_body" and current.prev_named_sibling is not None:
            scope |= _own_parameters(current.prev_named_sibling)
        current = current.parent
    return scope


#: The body of each kind of type declaration.
_BODIES = {
    "class_definition": "class_body",
    "mixin_declaration": "class_body",
    "enum_declaration": "enum_body",
    "extension_declaration": "extension_body",
    "extension_type_declaration": "class_body",
}
#: What every object has, inherited from ``Object`` by a type with no
#: supertype clause.
_OBJECT_MEMBERS = frozenset({"hashCode", "runtimeType", "toString", "noSuchMethod"})
#: What every enum has besides.
_ENUM_MEMBERS = frozenset({"values", "index", "name"})
#: What may come before a variable's declarator list among its siblings.
_VARIABLE_PREFIX = frozenset(
    {
        "late",
        "external",
        "static",
        "covariant",
        "final_builtin",
        "const_builtin",
        "inferred_type",
        "type_identifier",
        "type_arguments",
        "nullable_type",
        "function_type",
        "record_type",
        "annotation",
        "comment",
        "documentation_comment",
    }
)
_TYPES = frozenset({"type_identifier", "type_arguments", "nullable_type", "function_type", "record_type"})


def _enclosing_type(node: Node) -> Node | None:
    """The class, mixin, enum or extension whose body holds ``node``."""
    for ancestor in _ancestors(node):
        if ancestor.parent is not None and _BODIES.get(ancestor.parent.type) == ancestor.type:
            return ancestor.parent
    return None


def _body(declaration: Node) -> Node | None:
    return next((c for c in declaration.named_children if c.type == _BODIES.get(declaration.type)), None)


def _declared_members(body: Node) -> dict[str, list[Node]]:
    """Each name a member of ``body`` declares (a field, a getter, a setter,
    a method, an enum constant), with the member declaring it."""
    found: dict[str, list[Node]] = {}
    for member in body.named_children:
        names: list[Node] = []
        if member.type == "enum_constant":
            names = [c for c in member.named_children[:1] if c.type == "identifier"]
        elif member.type == "declaration":
            for child in member.named_children:
                if child.type in ("initialized_identifier_list", "static_final_declaration_list"):
                    names += [d.named_children[0] for d in child.named_children if d.named_children]
                elif child.type == "identifier_list":
                    names += [c for c in child.named_children if c.type == "identifier"]
                elif child.type == "identifier":
                    names.append(child)  # `external FeatureClient client;`
                elif child.type in ("function_signature", "getter_signature", "setter_signature"):
                    names += [c for c in child.named_children[:3] if c.type == "identifier"][:1]
        elif member.type == "method_signature":
            for child in member.named_children:
                if child.type in ("function_signature", "getter_signature", "setter_signature"):
                    names += [c for c in child.named_children[:3] if c.type == "identifier"][:1]
        for name in names:
            found.setdefault(_text(name), []).append(member)
    return found


def _has_supertype_clause(declaration: Node) -> bool:
    """Whether ``declaration`` names a supertype it inherits members from:
    ``extends``/``with`` (a class's ``superclass`` holds both), an enum's
    ``with``, or a mixin's ``on``."""
    if any(c.type in ("superclass", "mixins") for c in declaration.named_children):
        return True
    return declaration.type == "mixin_declaration" and any(c.type == "on" for c in declaration.children)


def _variable_prefix(listing: Node) -> list[Node]:
    """The modifiers, type and annotations before ``listing``, a declarator
    list, among its siblings."""
    found: list[Node] = []
    previous = listing.prev_sibling
    while previous is not None and previous.type in _VARIABLE_PREFIX:
        found.append(previous)
        previous = previous.prev_sibling
    return found[::-1]


def _initializer_of(declarator: Node) -> list[Node]:
    nodes: list[Node] = []
    after = False
    for part in declarator.children:
        if after and part.is_named and "comment" not in part.type:
            nodes.append(part)
        after = after or part.type == "="
    return nodes


def _inert_declaration(path: str, root: Node, declarator: Node, prefix: list[Node], repo_dir: str) -> str | None:
    """Why reading the variable ``declarator`` declares may do work, or
    ``None`` when it cannot. Dart initializes a top-level or static variable
    on its first read, so that read runs the initializer and nothing else:
    the initializer must be inert. ``prefix`` is its modifiers and type."""
    kinds = {c.type for c in prefix}
    if "external" in kinds:
        return "an `external` variable, which may be a getter"
    value = _initializer_of(declarator)
    if not value:
        return "a variable with no initializer"  # a `late` one throws when read unset
    # Passed where a typed parameter is wanted, a `dynamic` value is cast,
    # and the cast may throw. An inferred type is the initializer's, which
    # is `dynamic` only for `null`; a declared one may be `dynamic` through
    # a typedef, so it must be the class the initializer constructs.
    types = [c for c in prefix if c.type in _TYPES]
    if not types and len(value) == 1 and value[0].type == "null_literal":
        return "a `dynamic` variable (`null` infers it), which the call would cast"
    if types:
        declared = "".join(_text(t) for t in types)
        declared = declared[:-1] if declared.endswith("?") else declared
        if declared != _constructed_class(value):
            return "a declared type other than the class its initializer builds, which may be `dynamic`"
    from flag_cleanup.dart_field_removal import _inert

    if "const_builtin" in kinds and _constructed_class(value) is not None:
        return None  # a constant construction runs no code
    if _inert(value) or const_construction(path, root, value, repo_dir, names=False):
        return None
    return "a variable whose initializer may do work"


def _constructed_class(value: list[Node]) -> str | None:
    """The class an initializer constructs (``const C()``, ``new C()``,
    ``C()``, ``C.named()``), as spelled; ``None`` for anything else."""
    if len(value) == 1 and value[0].type in ("const_object_expression", "new_expression"):
        labels = [c for c in value[0].named_children if c.type == "type_identifier"]
        return _text(labels[0]) if len(labels) == 1 else None
    if value and value[0].type == "identifier" and len(value) in (2, 3) and all(n.type == "selector" for n in value[1:]):
        return _text(value[0])
    return None


def _inert_static(path: str, root: Node, members: list[Node], repo_dir: str) -> str | None:
    if len(members) != 1 or members[0].type != "declaration":
        return "a member of the enclosing type that is not one static field"
    member = members[0]
    prefix = [c for c in member.children if c.type in _VARIABLE_PREFIX]
    if "static" not in {c.type for c in prefix}:
        return "an instance member of the enclosing type"
    listing = next((c for c in member.named_children if c.type in ("initialized_identifier_list", "static_final_declaration_list")), None)
    if listing is None or len(listing.named_children) != 1:
        return "a static field with no initializer, or declared beside others"
    return _inert_declaration(path, root, listing.named_children[0], prefix, repo_dir)


def _inert_top_level(path: str, root: Node, name: str, repo_dir: str) -> str | None:
    from flag_cleanup.dart_field_removal import _TYPE_DECLARATIONS as declarations
    from flag_cleanup.dart_field_removal import _callable_names

    if name in _callable_names(root):
        return "a getter, setter or function of that name is declared in the file"
    if any(
        n.type in declarations
        and (label := next((c for c in n.named_children if c.type in ("identifier", "type_identifier")), None)) is not None
        and _text(label) == name
        for n in root.named_children
    ):
        return "a type of that name is declared in the file"
    found: list[tuple[Node, Node]] = []
    for listing in root.named_children:
        if listing.type in ("initialized_identifier_list", "static_final_declaration_list"):
            found += [(listing, d) for d in listing.named_children if d.named_children and _text(d.named_children[0]) == name]
        elif listing.type == "identifier_list":
            found += [(listing, c) for c in listing.named_children if c.type == "identifier" and _text(c) == name]
    if not found:
        return "not a variable declared in the file (an import's, or a local's)"
    if len(found) != 1:
        return "declared more than once in the file"
    listing, declarator = found[0]
    if declarator.type == "identifier":
        return "a variable with no initializer"
    return _inert_declaration(path, root, declarator, _variable_prefix(listing), repo_dir)


def _inert_name(
    path: str, argument: Node, name: str, root: Node, repo_dir: str, *, parameters: bool = True
) -> str | None:
    """Why the bare name ``name`` passed in ``argument`` may do work when
    read, or strand a local; ``None`` when it cannot. Accepted:

    - a parameter of a function enclosing the call (an unused parameter is
      no warning);
    - a variable of the file whose initializer is inert (see
      :func:`_inert_declaration`): a top-level one, or a ``static`` field of
      the class, mixin or enum around the call.

    Refused when the member around the call binds the name any other way;
    for a file with part files (another part may declare it); inside an
    extension; and, for a top-level variable read inside a type, when the
    type declares a member of the name or names a supertype, since the
    name may then be that member. A getter, a local, an import's name or a
    variable with no initializer is never a candidate."""
    if _bound_in_member(argument, name):
        return "bound again in the member around the call"
    if name in _enclosing_parameters(argument):
        # A constructor's argument is never a parameter: the parameter's
        # type is not known, and a `dynamic` one would be cast there.
        return None if parameters else "a parameter, passed on to a constructor"
    if any(c.type in ("part_directive", "part_of_directive") for c in root.named_children):
        return "its library has part files"
    owner = _enclosing_type(argument)
    if owner is not None:
        if owner.type in ("extension_declaration", "extension_type_declaration"):
            return "read inside an extension"
        body = _body(owner)
        members = _declared_members(body) if body is not None else {}
        if name in members:
            from flag_cleanup.dart_field_removal import _callable_names

            if name in _callable_names(root):
                return "a getter, setter or function of that name is declared in the file"
            return _inert_static(path, root, members[name], repo_dir)
        if _has_supertype_clause(owner):
            return "read inside a type with an `extends`, `with` or `on` clause, which may inherit it"
        if name in _OBJECT_MEMBERS or (owner.type == "enum_declaration" and name in _ENUM_MEMBERS):
            return "a member every object of the enclosing type has"
    return _inert_top_level(path, root, name, repo_dir)


# --- `widget.f` in a Flutter `State` -------------------------------------------

#: The public libraries of ``package:flutter``. None declares or exports a
#: top-level ``widget``, and ``State`` is declared once, in the widgets
#: library, which ``material`` and ``cupertino`` export.
_FLUTTER_LIBRARIES = frozenset(
    f"package:flutter/{name}.dart"
    for name in (
        "animation", "cupertino", "foundation", "gestures", "material", "painting",
        "physics", "rendering", "scheduler", "semantics", "services", "widget_previews",
        "widgets",
    )
)
_STATE_LIBRARIES = frozenset(f"package:flutter/{name}.dart" for name in ("widgets", "material", "cupertino"))


def _brings(spec: Node, name: str) -> bool:
    """Whether the import ``spec``'s combinators let ``name`` through."""
    shown: set[str] | None = None
    hidden: set[str] = set()
    for combinator in (c for c in spec.named_children if c.type == "combinator"):
        names = {_text(c) for c in combinator.named_children if c.type == "identifier"}
        if combinator.children and combinator.children[0].type == "show":
            shown = names if shown is None else shown & names
        else:
            hidden |= names
    return name not in hidden and (shown is None or name in shown)


def _flutter_state(path: str, root: Node, repo_dir: str) -> str | None:
    """Why ``State`` and a bare ``widget`` in ``root`` may not be Flutter's
    ``State`` and its getter, or ``None``. A top-level ``widget`` in scope
    would win over the inherited getter, so no import may bring one."""
    from flag_cleanup import dart_namespace

    declared = dart_namespace.declared_names(root)
    if declared is None:
        return "the file declares names that cannot be read"
    for name in ("State", "widget"):
        if name in declared.names:
            return f"the file declares `{name}`"
    resolve = dart_namespace.resolver(path, repo_dir)
    flutter = False
    for spec in (n for n in _walk(root) if n.type == "import_specification"):
        if any(c.type == "as" for c in spec.children):
            continue  # a prefix brings no bare name
        uri = dart_namespace.uri_of(spec)
        if uri is None:
            return f"{_where(path, spec)}: an import whose library cannot be told"
        if uri in _FLUTTER_LIBRARIES:
            flutter = flutter or (uri in _STATE_LIBRARIES and _brings(spec, "State"))
            continue
        if uri.startswith("dart:") or not (_brings(spec, "State") or _brings(spec, "widget")):
            continue
        namespace = resolve(uri)
        if namespace is None:
            return f"{_where(path, spec)}: an import whose names cannot be read may bring a `widget` or a `State`"
        if {"State", "widget"} & set(namespace.names):
            return f"{_where(path, spec)}: an import brings a `widget` or a `State`"
    if not flutter:
        return "`State` is not imported from `package:flutter/widgets.dart`, `material.dart` or `cupertino.dart`"
    return None


def _state_widget(owner: Node | None) -> str | Node:
    """The type argument ``W`` of ``owner``'s ``extends State<W>``, or why
    ``owner`` is not such a class."""
    if owner is None or owner.type != "class_definition":
        return "not inside a class"
    superclass = next((c for c in owner.named_children if c.type == "superclass"), None)
    if superclass is None or [c.type for c in superclass.named_children] != ["type_identifier", "type_arguments"]:
        return "not inside a class that extends `State<…>`, and nothing else"
    state, arguments = superclass.named_children
    if _text(state) != "State":
        return "not inside a class that extends `State<…>`, and nothing else"
    if [c.type for c in arguments.named_children] != ["type_identifier"]:
        return "the `State`'s widget type is not a plain class name"
    widget = arguments.named_children[0]
    parameters = next((c for c in owner.named_children if c.type == "type_parameters"), None)
    if parameters is not None and any(
        c.type == "type_identifier" and _text(c) == _text(widget) for p in parameters.named_children for c in p.named_children[:1]
    ):
        return "the `State`'s widget type is a type parameter"
    body = _body(owner)
    if body is None or "widget" in _declared_members(body):
        return "the class declares a `widget` member"
    return widget


_SUPERTYPE_CLAUSES = frozenset({"superclass", "interfaces", "mixins", "mixin_application"})


def _is_supertype(node: Node) -> bool:
    """Whether the type name ``node`` is in a supertype clause (and not a
    type argument there)."""
    for ancestor in _ancestors(node):
        if ancestor.type in ("type_arguments", "type_parameters", "type_bound"):
            return False
        if ancestor.type in _SUPERTYPE_CLAUSES:
            return True
        if ancestor.type == "mixin_declaration":
            return True  # its `on` clause
        if ancestor.type in _TYPE_DECLARATIONS or ancestor.type in ("class_body", "enum_body", "extension_body"):
            return False
    return False


def _widget_field_declaration(cls: Node, field: str) -> str | None:
    """Why ``field`` may not be a plain field of the class ``cls``."""
    from flag_cleanup.dart_field_removal import _fields

    if field.startswith("_"):
        return f"`{field}` is private, and this may be its last read"
    body = _body(cls)
    members = _declared_members(body).get(field, []) if body is not None else []
    if len(members) != 1 or members[0].type != "declaration":
        return f"`{field}` is not declared once, as a field, in the widget class"
    tokens = {c.type for c in members[0].children}
    for modifier in ("static", "late", "external", "abstract"):
        if modifier in tokens:
            return f"`{field}` is a `{modifier}` field"
    if not any(f == members[0] for f, _ in _fields(body)):
        return f"`{field}` is not a field of the widget class"
    types = [c for c in members[0].named_children if c.type in _TYPES]
    if not types or any(_text(t) == "dynamic" for t in types):
        return f"`{field}` is `dynamic`, which the call would cast"
    return None


def _field_type(declaring: str, cls: Node, field: str, repo_dir: str) -> str | None:
    """Why the declared type of the widget field ``field`` may be
    ``dynamic``, which the call would cast, or ``None``. A typedef may name
    ``dynamic``, so the type must be one plain name, optionally ``?``, that
    the widget's file sees as a class, enum, mixin or extension type
    declared once in the repository, or as a name of an audited Featureflip
    SDK library it imports."""
    from flag_cleanup import dart_namespace

    body = _body(cls)
    member = _declared_members(body)[field][0]
    types = [c for c in member.named_children if c.type in _TYPES]
    if [t.type for t in types] not in (["type_identifier"], ["type_identifier", "nullable_type"]):
        return f"`{field}`'s type is not a plain class name"
    type_name = _text(types[0])
    root = _root(cls)
    found: list[tuple[str, Node]] = []
    for source in source_index.files(repo_dir, (".dart",), with_ignored=True):
        file = str(source.path)
        try:
            data = source.path.read_bytes()
        except OSError:
            return f"{file} could not be read"
        if type_name.encode() not in data:
            continue
        tree = _parse(data.decode("utf-8", "replace"), "dart")
        if tree.has_error:
            return f"{file} does not parse"
        for node in _walk(tree):
            if node.type in _TYPE_DECLARATIONS:
                label = next((c for c in node.named_children if c.type in ("identifier", "type_identifier")), None)
                if label is not None and _text(label) == type_name:
                    found.append((file, node))
    if found:
        if len(found) != 1 or found[0][1].type not in (
            "class_definition", "enum_declaration", "mixin_declaration", "extension_type_declaration"
        ):
            return f"`{type_name}` is not declared once in the repository, as a class"
        visibility = _visibility(declaring, root, found[0][0], type_name, repo_dir)
        if isinstance(visibility, str):
            return visibility
        if not visibility[0]:
            return f"the `{type_name}` declared in the repository is not the one the widget's file sees"
        return None
    resolve = dart_namespace.resolver(declaring, repo_dir)
    for spec in (n for n in _walk(root) if n.type == "import_specification"):
        uri = dart_namespace.uri_of(spec)
        if (
            uri is not None
            and uri.startswith(f"package:{dart_namespace.AUDITED_SDK_PACKAGE}/")
            and not any(c.type == "as" for c in spec.children)
            and _brings(spec, type_name)
            and (namespace := resolve(uri)) is not None
            and type_name in namespace.names
        ):
            return None
    return f"`{type_name}` is not a class of the repository or of the audited Featureflip SDK, so it may be `dynamic`"


def _widget_field(path: str, argument: Node, value: list[Node], root: Node, repo_dir: str) -> str | None:
    """Why the argument ``widget.f`` may do work, or ``None`` when it reads
    a plain field of the mounted widget. ``widget`` is the getter
    ``State.widget``, which returns the widget and does nothing else (it
    throws only before mounting). So the read is pure when:

    - the call is inside ``class S extends State<W>`` (nothing more in that
      clause), with Flutter's ``State`` (see :func:`_flutter_state`), and
      ``S`` declares no ``widget``;
    - ``W`` is declared exactly once in the repository, visible to the call,
      in a package that is not published, and no class of the repository
      names it as a supertype, aliases it or names it in an annotation (a
      generated mock would implement it);
    - ``f`` is a field of ``W``'s body, declared once, not ``static``,
      ``late``, ``external`` or ``abstract``, with a type that is not
      ``dynamic``;
    - no getter named ``f`` and no ``noSuchMethod`` is declared anywhere in
      the repository."""
    field = _selected(value[1])
    if field is None or len(value[1].named_children) != 1:
        return "not a plain `widget.<field>` read"
    if _bound_in_member(argument, "widget") or "widget" in _enclosing_parameters(argument):
        return "`widget` is bound in the member around the call"
    if any(c.type in ("part_directive", "part_of_directive") for c in root.named_children):
        return "its library has part files"
    widget = _state_widget(_enclosing_type(argument))
    if isinstance(widget, str):
        return widget
    reason = _flutter_state(path, root, repo_dir)
    if reason is not None:
        return reason
    name = _text(widget)
    state = next(c for c in _enclosing_type(argument).named_children if c.type == "identifier")
    state_name = _text(state)
    getters = {field, "widget"}
    declarations: list[tuple[str, Node]] = []
    for source in source_index.files(repo_dir, (".dart",), with_ignored=True):
        file = str(source.path)
        try:
            data = source.path.read_bytes()
        except OSError:
            return f"{file} could not be read"
        if not any(needle.encode() in data for needle in (name, state_name, field, "widget", "noSuchMethod")):
            continue
        tree = root if str(Path(file).resolve()) == str(Path(path).resolve()) else _parse(data.decode("utf-8", "replace"), "dart")
        if tree.has_error:
            return f"{file} does not parse"
        for node in _walk(tree):
            if node.type in _TYPE_DECLARATIONS:
                label = next((c for c in node.named_children if c.type in ("identifier", "type_identifier")), None)
                if label is not None and _text(label) == name:
                    declarations.append((file, node))
                if node.type == "type_alias" and any(
                    c.type == "type_identifier" and _text(c) == name for c in _walk(node)
                ):
                    return f"{_where(file, node)}: a type alias names {name}"
            elif node.type == "getter_signature" and any(
                c.type == "identifier" and _text(c) in getters for c in node.named_children
            ):
                label = next(_text(c) for c in node.named_children if c.type == "identifier" and _text(c) in getters)
                return f"{_where(file, node)}: a getter named `{label}` is declared"
            elif node.type == "function_signature" and any(
                c.type == "identifier" and _text(c) == "noSuchMethod" for c in node.named_children[:3]
            ):
                return f"{_where(file, node)}: `noSuchMethod` is declared"
            elif node.type == "type_identifier" and _text(node) == state_name and _is_supertype(node):
                return f"{_where(file, node)}: a class names {state_name} as a supertype"
            elif node.type == "type_identifier" and _text(node) == name:
                if _is_supertype(node):
                    return f"{_where(file, node)}: a class names {name} as a supertype"
                if any(a.type == "annotation" for a in _ancestors(node)):
                    return f"{_where(file, node)}: an annotation names {name}"
            elif node.type == "identifier" and _text(node) == name and any(a.type == "annotation" for a in _ancestors(node)):
                return f"{_where(file, node)}: an annotation names {name}"
    if len(declarations) != 1 or declarations[0][1].type != "class_definition":
        return f"{name} is not declared once in the repository, as a class"
    declaring, cls = declarations[0]
    visibility = _visibility(path, root, declaring, name, repo_dir)
    if isinstance(visibility, str):
        return visibility
    if not visibility[0]:
        return f"the {name} declared in the repository is not the one the file sees"
    if published(declaring):
        return f"{name}'s package may be published, and a subclass outside the repository may override `{field}`"
    if any(c.type == "type_parameters" for c in cls.named_children):
        return f"{name} is generic, so a field's type may be a type parameter, `dynamic` in `State<{name}>`"
    reason = _widget_field_declaration(cls, field)
    if reason is not None:
        return reason
    return _field_type(declaring, cls, field, repo_dir)


def _argument_site(path: str, arguments: Node, how: tuple[str, int | str], repo_dir: str) -> Site | None | str:
    """The site of the argument a call passes for a dropped parameter,
    ``None`` when it passes none, or why it cannot go. A literal, a constant,
    a parameter, an inert variable (see :func:`_inert_name`), a plain field
    of a Flutter ``State``'s widget (see :func:`_widget_field`), or a call
    of a constructor that builds the object and does nothing else (see
    :func:`const_construction`) can go; anything else may do work the call
    would no longer do."""
    kind, key = how
    items = [c for c in arguments.named_children if c.type in ("argument", "named_argument")]
    if kind == "positional":
        positional = [c for c in items if c.type == "argument"]
        if len(positional) <= key:
            return None
        item = positional[key]
        value = item.named_children
    else:
        item = next(
            (
                c
                for c in items
                if c.type == "named_argument"
                and (label := next((x for x in c.named_children if x.type == "label"), None)) is not None
                and label.named_children
                and _text(label.named_children[0]) == key
            ),
            None,
        )
        if item is None:
            return None
        value = [c for c in item.named_children if c.type != "label"]
    value = [c for c in value if c.type != "comment"]
    where = _where(path, item)
    root = _root(item)
    if _literal(value) or (len(value) == 1 and value[0].type == "const_object_expression"):
        return Site(path, item.start_byte, item.end_byte, "argument")
    if len(value) == 2 and value[0].type == "identifier" and _text(value[0]) == "widget" and value[1].type == "selector":
        reason = _widget_field(path, item, value, root, repo_dir)
        if reason is not None:
            return f"{where}: an argument that may do work ({' '.join(_text(item).split())[:60]}: {reason})"
        return Site(path, item.start_byte, item.end_byte, "argument")
    if const_construction(path, root, value, repo_dir):
        return Site(path, item.start_byte, item.end_byte, "argument")
    if len(value) != 1 or value[0].type != "identifier":
        return f"{where}: an argument that may do work ({' '.join(_text(item).split())[:60]})"
    reason = _inert_name(path, item, _text(value[0]), root, repo_dir)
    if reason is not None:
        return f"{where}: an argument that is neither a parameter nor an inert variable ({_text(value[0])}: {reason})"
    return Site(path, item.start_byte, item.end_byte, "argument")


def inert_initializer_name(path: str, node: Node, root: Node, repo_dir: str) -> str | None:
    """Why the bare name ``node``, a whole field initializer, may do work;
    ``None`` when it reads an inert variable (see :func:`_inert_name`)."""
    return _inert_name(path, node, _text(node), root, repo_dir)


def plan_callers(
    path: str, root: Node, cls: Node, dropped: dict[str, tuple[str, int | str]], repo_dir: str
) -> list[Site] | str:
    """Every caller's argument for each dropped parameter, or why the callers
    cannot be proven."""
    name_node = next((c for c in cls.named_children if c.type == "identifier"), None)
    body = next((c for c in cls.named_children if c.type == "class_body"), None)
    if name_node is None or body is None:
        return "an unexpected class"
    class_name = _text(name_node)
    if _annotated(cls):
        return "an annotated class"
    ctors = constructors(body)
    if any(s.type == "redirecting_factory_constructor_signature" for s, _ in ctors):
        return "a redirecting factory constructor"
    files = _files(path, class_name, repo_dir)
    if isinstance(files, str):
        return files
    scan = _Scan(class_name, {_constructor_name(s) for s, _ in ctors} - {""}, _member_names(body), [], [])
    declaration = {(path, name_node.start_byte)}
    declaration |= {
        (path, c.start_byte)
        for s, _ in ctors
        for c in s.children[:3]
        if c.type == "identifier" and _text(c) == class_name
    }
    resolved = str(Path(path).resolve())
    for file in files:
        try:
            if str(Path(file).resolve()) == resolved:
                data, tree = None, root
            else:
                data = Path(file).read_bytes()
                if class_name.encode() not in data and b"dart:mirrors" not in data:
                    continue
                tree = _parse(data.decode("utf-8", "replace"), "dart")
        except OSError:
            return f"{file} could not be read"
        if tree.has_error:
            return f"{file} does not parse"
        visibility = _visibility(file, tree, path, class_name, repo_dir)
        if isinstance(visibility, str):
            if class_name.encode() in (data if data is not None else b""):
                return visibility
            visibility = (False, set())
        scan.bare, scan.prefixes = visibility
        for node in _walk(tree):
            if node.type == "uri" and "dart:mirrors" in _text(node):
                return f"{file} imports dart:mirrors"
            if node.type in _TYPE_DECLARATIONS and node is not cls:
                label = next((c for c in node.named_children if c.type in ("identifier", "type_identifier")), None)
                if label is not None and _text(label) == class_name:
                    return f"{_where(file, node)}: another declaration of {class_name}"
            if node.type not in ("identifier", "type_identifier", "identifier_dollar_escaped"):
                continue
            if _text(node).lstrip("$") != class_name:
                continue
            key = (path, node.start_byte) if str(Path(file).resolve()) == resolved else None
            if key in declaration:
                continue
            reason = _classify(file if key is None else path, node, scan)
            if reason is not None:
                return reason
    _redirections(path, body, scan)
    for sub_path, subclass in scan.subclasses:
        reason = _super_calls(sub_path, subclass, scan)
        if reason is not None:
            return reason
    sites: list[Site] = []
    trees: dict[str, Node] = {}
    for call_path, ctor, arguments in scan.calls:
        if ctor not in dropped:
            continue
        site = _argument_site(call_path, arguments, dropped[ctor], repo_dir)
        if isinstance(site, str):
            return site
        if site is not None:
            sites.append(site)
            trees.setdefault(call_path, _root(arguments))
    for call_path, tree in trees.items():
        reason = _stranded_by_arguments(call_path, tree, [s for s in sites if s.path == call_path])
        if reason is not None:
            return reason
    return sites


def _root(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


def _stranded_by_arguments(path: str, root: Node, sites: list[Site]) -> str | None:
    """A name the arguments ``sites`` of one file spell that nothing else in
    the file does (an import, a private declaration), or a private variable
    nothing else reads (see :func:`private_variable_unread`): deleting them
    would leave it unused. A ``.member`` names no
    declaration of the file, and neither does the ``widget`` of an argument
    ``widget.f`` (see :func:`_widget_field`)."""
    from flag_cleanup.dart_field_removal import _CORE, _own_declarations

    spans = [(s.start, s.end) for s in sites]

    def inside(node: Node, ranges: list[tuple[int, int]]) -> bool:
        return any(start <= node.start_byte < end for start, end in ranges)

    for node in _walk(root):
        if node.type not in ("identifier", "type_identifier") or not inside(node, spans):
            continue
        if node.parent is not None and node.parent.type in (
            "label",
            "unconditional_assignable_selector",
            "conditional_assignable_selector",
            "cascade_selector",
        ):
            continue
        if widget_head(node, spans):
            continue
        name = _text(node)
        if name.startswith("_"):
            unread = private_variable_unread(root, name, spans)
            if unread:
                return f"{_where(path, node)}: deleting the argument would leave `{name}` unused"
            if unread is not None:
                continue
        skipped = spans + (_own_declarations(root, name) if name.startswith("_") else [])
        if name not in _CORE and not any(
            n.type in ("identifier", "type_identifier", "identifier_dollar_escaped")
            and _text(n).lstrip("$") == name
            and not inside(n, skipped)
            for n in _walk(root)
        ):
            return f"{_where(path, node)}: deleting the argument would leave `{name}` unused"
    return None


def widget_head(node: Node, spans: list[tuple[int, int]]) -> bool:
    """Whether ``node`` is the ``widget`` of an argument ``widget.f`` that
    is one of ``spans``: the inherited getter, which no declaration of the
    file provides (see :func:`_widget_field`)."""
    if _text(node) != "widget" or node.parent is None or (node.parent.start_byte, node.parent.end_byte) not in spans:
        return False
    values = [c for c in node.parent.named_children if c.type not in ("label", "comment")]
    return bool(values) and values[0] == node


def _declares_variable(node: Node) -> bool:
    """Whether the identifier ``node`` is the name a variable, field or
    parameter declaration declares."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _DECLARATORS:
        return node == next((c for c in parent.named_children if c.type == "identifier"), None)
    if parent.type == "identifier_list":
        return True
    if parent.type == "declaration":
        return True  # `external FeatureClient _client;`
    if parent.type == "formal_parameter":
        return _parameter_name(parent) == _text(node)
    return False


def private_variable_unread(root: Node, name: str, skipped: list[tuple[int, int]]) -> bool | None:
    """For a private ``name`` that a variable, field or parameter of the
    file declares: whether nothing outside ``skipped`` reads it, which
    ``dart analyze`` reports once ``skipped`` is deleted. Names are matched
    by spelling, so a name declared twice in the file counts as unread (a
    read may be the other declaration's), and so does one only written
    (``_c = …``, ``this._c``). ``None`` when no variable declares it."""
    from flag_cleanup.dart_field_removal import _write_kind

    def inside(node: Node) -> bool:
        return any(start <= node.start_byte < end for start, end in skipped)

    spellings = [
        n for n in _walk(root)
        if n.type in ("identifier", "identifier_dollar_escaped") and _text(n).lstrip("$") == name
    ]
    declared = [n for n in spellings if n.type == "identifier" and _declares_variable(n)]
    if not declared:
        return None
    if len(declared) > 1:
        return True
    return not any(n not in declared and not inside(n) and _write_kind(n) is None for n in spellings)


# --- a construction that does nothing but build the object --------------------


def _const_arguments(path: str, root: Node, arguments: Node, repo_dir: str, names: bool) -> bool:
    """Whether every argument is a literal, a ``const`` object or, with
    ``names``, a bare name reading an inert variable (see
    :func:`_inert_name`). A variable's own initializer is judged without
    ``names``, so no chain of variables is followed."""
    for item in arguments.named_children:
        if item.type == "comment":
            continue
        if item.type not in ("argument", "named_argument"):
            return False
        value = [c for c in item.named_children if c.type not in ("label", "comment")]
        if _literal(value) or (len(value) == 1 and value[0].type == "const_object_expression"):
            continue
        if not (
            names
            and len(value) == 1
            and value[0].type == "identifier"
            and _inert_name(path, item, _text(value[0]), root, repo_dir, parameters=False) is None
        ):
            return False
    return True


def _does_nothing(signature: Node, member: Node) -> bool:
    """Whether the generative constructor ``signature`` runs no code of its
    own: a ``const`` one with no ``assert`` (which runs when it is called
    without ``const``), or one with no initializer list or redirection, no
    ``super.`` parameter, not ``external``, and a body that is ``;`` or
    ``{}``."""
    if signature.type == "constant_constructor_signature":
        # Constant otherwise, but called without `const` its asserts run.
        return not any(n.type == "assertion" for n in _walk(member))
    if signature.type != "constructor_signature":
        return False
    if [c for c in member.named_children if "comment" not in c.type] != [signature]:
        return False  # an initializer list, a redirection
    if any(c.type == "external" for c in member.children):
        return False
    if any(c.type == "super_formal_parameter" for p, _ in _parameters(signature) for c in p.named_children):
        return False
    if member.type == "method_signature":
        body = member.next_named_sibling
        block = body.named_children[0] if body is not None and body.type == "function_body" and body.named_children else None
        return block is not None and block.type == "block" and not [
            c for c in block.named_children if "comment" not in c.type
        ]
    return True


def _fields_do_nothing(body: Node) -> bool:
    """Whether building an instance runs no field initializer that may do
    work. A ``late`` field's runs on first read and a ``static`` one's on
    first access, neither at construction."""
    from flag_cleanup.dart_field_removal import _DECLARATOR_LISTS, _DECLARATORS, _inert

    for member in body.named_children:
        if member.type != "declaration" or {"late", "static"} & {c.type for c in member.children}:
            continue
        for listing in (c for c in member.named_children if c.type in _DECLARATOR_LISTS):
            for declarator in (d for d in listing.named_children if d.type in _DECLARATORS):
                value: list[Node] = []
                after = False
                for part in declarator.children:
                    if after and part.is_named and "comment" not in part.type:
                        value.append(part)
                    after = after or part.type == "="
                if value and not _inert(value):
                    return False
    return True


def _has_const_constructor(root: Node, class_name: str, name: str) -> bool | None:
    """Whether ``root`` declares a concrete class ``class_name`` whose
    constructor ``name`` builds the object and does nothing else: a
    ``const`` generative one with no ``assert``, or one that runs no code of its own in a class
    with no superclass or mixin (whose constructors would run too) and no
    field initializer that may do work. ``None`` when it declares no such
    class."""
    for cls in (n for n in root.named_children if n.type == "class_definition"):
        label = next((c for c in cls.named_children if c.type == "identifier"), None)
        if label is None or _text(label) != class_name:
            continue
        body = next((c for c in cls.named_children if c.type == "class_body"), None)
        if body is None or any(c.type == "abstract" for c in cls.children):
            return False
        found = [(s, m) for s, m in constructors(body) if _constructor_name(s) == name]
        if len(found) == 1 and found[0][0].type == "constant_constructor_signature":
            return _does_nothing(*found[0])
        return (
            len(found) == 1
            and _does_nothing(*found[0])
            and not any(c.type == "superclass" for c in cls.named_children)
            and _fields_do_nothing(body)
        )
    return None


def _imported(path: str, root: Node, class_name: str, repo_dir: str) -> Node | None:
    """The tree of the one checkout file an unprefixed import of ``root``
    brings ``class_name`` in from, declared there directly; ``None`` when no
    import provably does."""
    from flag_cleanup import dart_namespace

    found: list[Node] = []
    for spec in (n for n in _walk(root) if n.type == "import_specification"):
        if any(c.type == "as" for c in spec.children):
            continue
        shown: set[str] | None = None
        hidden: set[str] = set()
        for combinator in (c for c in spec.named_children if c.type == "combinator"):
            names = {_text(c) for c in combinator.named_children if c.type == "identifier"}
            if combinator.children and combinator.children[0].type == "show":
                shown = names if shown is None else shown & names
            else:
                hidden |= names
        if class_name in hidden or (shown is not None and class_name not in shown):
            continue
        uri = dart_namespace.uri_of(spec)
        target = (
            None
            if uri is None
            else dart_namespace.locate(uri, Path(path).resolve(), Path(repo_dir).resolve())
        )
        tree = dart_namespace.read(target) if target is not None else None
        if tree is not None and _has_const_constructor(tree, class_name, "") is not None:
            found.append(tree)
    return found[0] if len(found) == 1 else None


def const_construction(path: str, root: Node, nodes: list[Node], repo_dir: str, *, names: bool = True) -> bool:
    """Whether the expression ``nodes`` (a postfix chain) calls, with
    constant arguments (or, with ``names``, bare names of inert variables,
    see :func:`_const_arguments`), a constructor that builds a value and
    does nothing else (see :func:`_has_const_constructor`): ``FeatureClient()`` where
    ``FeatureClient`` has a ``const FeatureClient();`` or a plain
    ``FeatureClient();``. The class must be declared in the file, or in the
    one checkout file an import brings it in from, and nothing else in the
    file may be named like it (a local or a member would be called
    instead)."""
    if len(nodes) == 1 and nodes[0].type == "new_expression":
        node = nodes[0]
        label = next((c for c in node.named_children if c.type == "type_identifier"), None)
        idents = [c for c in node.named_children if c.type == "identifier"]
        arguments = next((c for c in node.named_children if c.type == "arguments"), None)
        if label is None or arguments is None:
            return False
        class_name, name = _text(label), _text(idents[0]) if idents else ""
    elif nodes and nodes[0].type == "identifier" and all(n.type == "selector" for n in nodes[1:]):
        class_name = _text(nodes[0])
        if len(nodes) == 2:
            name, call = "", nodes[1]
        elif len(nodes) == 3 and _selected(nodes[1]) is not None:
            name, call = _selected(nodes[1]), nodes[2]
        else:
            return False
        arguments = _call_arguments(call)
        if arguments is None or any(c.type == "type_arguments" for c in call.named_children[0].named_children):
            return False
    else:
        return False
    name = "" if name == "new" else name
    if name.startswith("_"):
        return False  # a private constructor whose last call this may be
    if not _const_arguments(path, root, arguments, repo_dir, names):
        return False
    from flag_cleanup.wrapper_dart import binds

    if any(
        n.type == "identifier"
        and _text(n) == class_name
        and n.parent is not None
        and n.parent.type not in ("class_definition", *_CONSTRUCTORS)
        and binds(n)
        for n in _walk(root)
    ):
        return False
    local = _has_const_constructor(root, class_name, name)
    if local is not None:
        return local
    if any(
        n.type in _TYPE_DECLARATIONS
        and (label := next((c for c in n.named_children if c.type in ("identifier", "type_identifier")), None))
        is not None
        and _text(label) == class_name
        for n in root.named_children
    ):
        return False
    tree = _imported(path, root, class_name, repo_dir)
    return tree is not None and bool(_has_const_constructor(tree, class_name, name))
