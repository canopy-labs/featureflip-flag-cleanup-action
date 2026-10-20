"""Prove a TypeScript or JavaScript private field can be removed.

:mod:`flag_cleanup.ts_unread_members` names a ``private`` or ``#`` field the
fold left unread. Deleting it is safe when nothing the deletion skips can do
work, and the proof needs only the class body:

- **an inert initializer, or none**: a literal, a name, a property access off
  a name, or an array or object literal built of those (``#enabled = false``,
  ``private items: Item[] = []``). Anything else (a call, ``new``, ``await``,
  a template with a substitution, anything reading ``this``) may do work,
  and the field stays reported.
- **written only by the constructor**: every other mention of the field is a
  plain ``=`` assignment (the field is unread, so there is no other kind),
  and there may be at most one: ``this.flags = flags;``, a statement of the
  constructor's own body, whose right side is a constructor parameter or
  inert. That statement goes with the field.

When the assignment was the parameter's last read, the parameter goes too,
with every caller's argument, through the same proof as a parameter property
(:func:`flag_cleanup.ctor_param_ts.constructor_callers`). If that proof fails
the field stays reported: deleting it would only move the warning to a
parameter nothing reads, which ``noUnusedParameters`` and ESLint's
``no-unused-vars`` report and this tool would not.

A ``private`` field is private to ``tsc`` only: ``svc['flags']`` reads it from
anywhere, which tests do to inspect one. So a ``private`` field also refuses
when any TypeScript or JavaScript file in the repository indexes something
with a string spelling its name. A ``#`` field needs no such search.

Refused as well: a decorated field (a decorator may register it), a
``declare`` field (it has no runtime existence to remove, and says something
to the type checker), an ``accessor`` field, a class of that name declared
twice in the file, and a parameter with a default value or a rest parameter.

The field is marked as a method of the sentinel's name, its own ``;`` with it,
and the assignment as the sentinel and a ``;``. The engine deletes both by
identity (see :mod:`flag_cleanup.ctor_param_model`). A comment on the field's
own line goes with it, and so does a ``/** … */`` comment on the line above,
which documents the field; a ``//`` comment above may head a group, and stays.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from flag_cleanup import ctor_param_ts, source_index, ts_scope, ts_unread_members
from flag_cleanup.ctor_param_model import IDENT, Refusal, Removal, Site
from flag_cleanup.unused import _parse

_PARAMETERS = frozenset({"required_parameter", "optional_parameter"})
#: Any of these makes a constructor parameter a property (`readonly flags`).
_PROPERTY_MODIFIERS = frozenset({"accessibility_modifier", "readonly", "override_modifier"})
#: A comment that applies to the next line: left behind, it would apply to the
#: member below instead, and an unused `@ts-expect-error` is an error itself.
_DIRECTIVE = re.compile(rb"^(//|/\*)\s*(@ts-expect-error|@ts-ignore|eslint-disable-next-line)\b")
_INERT_LEAVES = frozenset({"string", "number", "true", "false", "null", "undefined", "identifier"})
_WRAPPERS = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"}
)
#: Modifiers that make deleting the field more than deleting a value.
_REFUSED_MODIFIERS = {
    "decorator": "a decorated field",
    "declare": "a `declare` field",
    "accessor": "an `accessor` field",
}


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _inert(node: Node) -> bool:
    """Whether evaluating ``node`` can do nothing but read a name."""
    if node.type in _INERT_LEAVES:
        return True
    if node.type == "template_string":
        return not any(c.type == "template_substitution" for c in node.named_children)
    if node.type in _WRAPPERS:
        return bool(node.named_children) and _inert(node.named_children[0])
    if node.type == "unary_expression":
        argument = node.child_by_field_name("argument")
        operator = node.child_by_field_name("operator")
        return (
            operator is not None
            and operator.type == "-"
            and argument is not None
            and argument.type == "number"
        )
    if node.type == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        return (
            obj is not None
            and prop is not None
            and prop.type == "property_identifier"
            and _inert(obj)
        )
    items = [c for c in node.named_children if c.type != "comment"]
    if node.type == "array":
        return all(_inert(c) for c in items)
    if node.type == "object":
        for item in items:
            if item.type == "shorthand_property_identifier":
                continue
            key = item.child_by_field_name("key") if item.type == "pair" else None
            value = item.child_by_field_name("value") if item.type == "pair" else None
            if key is None or value is None or key.type not in ("property_identifier", "string", "number"):
                return False
            if not _inert(value):
                return False
        return True
    return False


def _row(node: Node) -> int:
    return node.start_point[0]


def _span(owner: Node, siblings: list[Node], *, with_doc: bool) -> tuple[int, int]:
    """``owner``'s bytes, its own ``;``, a comment on its last line, and the
    comments directly above it that apply to it: a ``@ts-expect-error``,
    ``@ts-ignore`` or ``eslint-disable-next-line`` directive, and with
    ``with_doc`` a ``/** … */`` comment."""
    position = siblings.index(owner)
    start, end = owner.start_byte, owner.end_byte
    last_row = owner.end_point[0]
    following = siblings[position + 1 :]
    if following and following[0].type == ";":
        end = following[0].end_byte
        last_row = following[0].end_point[0]
        following = following[1:]
    if following and following[0].type == "comment" and _row(following[0]) == last_row:
        end = following[0].end_byte
    top_row = _row(owner)
    while position > 0:
        before = siblings[position - 1]
        if (
            before.type != "comment"
            or before.end_point[0] != top_row - 1
            or not (_DIRECTIVE.match(before.text) or (with_doc and before.text.startswith(b"/**")))
            or (position >= 2 and siblings[position - 2].end_point[0] >= _row(before))
        ):
            break
        start, top_row, position = before.start_byte, _row(before), position - 1
    return start, end


#: How a member can begin that continues a field above it with no `;`.
_CONTINUES = (b"[", b"(", b"*", b"`", b"+", b"-", b"/")


def _asi_hazard(container: Node, deleted: Node) -> bool:
    """Whether deleting ``deleted`` leaves something with no ``;`` directly
    above a member or statement that would continue it: a field ``a = b``
    then ``['k']() {}`` reads as ``a = b['k']() {}``, and a statement
    ``init()`` then ``(register)(1)`` as ``init()(register)(1)``."""
    children = [c for c in container.children if c.type != "comment"]
    position = children.index(deleted)
    after = children[position + 1 :]
    if after and after[0].type == ";":
        after = after[1:]
    before = children[position - 1] if position > 0 else None
    if before is None or not after or after[0].text[:1] not in _CONTINUES:
        return False
    if container.type == "class_body":
        return before.type == "public_field_definition"
    return before.type not in ("{", ";") and not before.text.rstrip().endswith((b";", b"}"))


def has_field(source: str, grammar: str, class_name: str, field: str) -> bool:
    """Whether ``class_name`` in ``source`` still declares the field."""
    for cls in ctor_param_ts._classes(_parse(source, grammar), class_name):
        body = cls.child_by_field_name("body")
        if body is not None and _fields(body, field):
            return True
    return False


def _fields(body: Node, field: str) -> list[Node]:
    return [
        m
        for m in body.named_children
        if m.type == "public_field_definition"
        and (n := m.child_by_field_name("name")) is not None
        and _text(n) == field
    ]


def _indexed_by_string(field: str, repo_dir: str) -> str | None:
    """Where a TypeScript or JavaScript file, or a component's or HTML page's
    script, indexes something with the string ``field`` (``svc['flags']``),
    or ``None``."""
    word = re.compile(rf"""['"`]{re.escape(field)}['"`]""".encode())
    wanted = ctor_param_ts._extensions()
    for file in source_index.files(repo_dir):
        if not str(file.path).endswith(wanted):
            continue
        try:
            data = file.path.read_bytes()
        except OSError:
            return f"{file.path} could not be read"
        if not word.search(data):
            continue
        root = _parse(data.decode("utf-8", "replace"), ts_scope.grammar(str(file.path)))
        for node in _walk(root):
            index = node.child_by_field_name("index") if node.type == "subscript_expression" else None
            if index is not None and ctor_param_ts._names_string(index, field):
                return f"{file.path}:{_row(node) + 1}: indexed by its name"
    # Script inside markup is not parsed, so its index is read off the text.
    # A quoted attribute value (`class="enabled"`) is no index.
    index = re.compile(rf"""\[[\s(]*(['"`]){re.escape(field)}\1[\s)]*\]""".encode())
    for file in ctor_param_ts.markup_files(repo_dir):
        try:
            if index.search(file.path.read_bytes()):
                return f"{file.path} indexes something with it ({ctor_param_ts.markup_kind(file.path)})"
        except OSError:
            return f"{file.path} could not be read"
    return None


def _parameter(ctor: Node, name: str) -> tuple[int, Node] | None:
    """``(position, node)`` of ``ctor``'s parameter named ``name``."""
    parameters = ctor.child_by_field_name("parameters")
    items = [c for c in parameters.named_children if c.type != "comment"] if parameters else []
    for index, item in enumerate(items):
        pattern = item.child_by_field_name("pattern")
        if pattern is not None and pattern.type == "identifier" and _text(pattern) == name:
            return index, item
    return None


def plan(path: str, source: str, label: str, repo_dir: str) -> Removal | Refusal:
    """Prove the private field ``label`` (``Class.field``) of the file at
    ``path``, whose current text is ``source``, can be removed."""
    class_name, _, field = label.partition(".")
    if IDENT.fullmatch(class_name) is None:
        return Refusal(label, "a name the deletion rules cannot spell")
    grammar = ts_scope.grammar(path)
    root = _parse(source, grammar)
    if root.has_error:
        return Refusal(label, "the declaring file does not parse")
    classes = ctor_param_ts._classes(root, class_name)
    if len(classes) != 1:
        return Refusal(label, "the class is declared more than once in its file")
    cls = classes[0]
    body = cls.child_by_field_name("body")
    found = _fields(body, field) if body is not None else []
    if len(found) != 1:
        return Refusal(label, "not a field", quiet=True)
    declaration = found[0]
    for child in declaration.children:
        if child.type in _REFUSED_MODIFIERS:
            return Refusal(label, _REFUSED_MODIFIERS[child.type])
    value = declaration.child_by_field_name("value")
    if value is not None and not _inert(value):
        return Refusal(label, f"an initializer that may do work ({' '.join(_text(value).split())[:60]})")

    if _asi_hazard(body, declaration):
        return Refusal(label, "deleting it would join the members either side (no `;`)")
    name = declaration.child_by_field_name("name")
    mentions = [
        n
        for n in _walk(body)
        if n.type in ts_unread_members._NAMES and n.text == name.text and n != name
    ]
    writes = [n for n in mentions if ts_unread_members._assigned(n)]
    if len(writes) != len(mentions):
        return Refusal(label, "its name is still spelled as a read in the class")
    if len(writes) > 1:
        return Refusal(label, "assigned more than once")
    sites = [Site(path, *_span(declaration, list(body.children), with_doc=True), "member")]
    removal = Removal(path, class_name, None, label, -1, (), False, field)
    if writes:
        outcome = _assignment(path, root, cls, writes[0], label, repo_dir)
        if isinstance(outcome, Refusal):
            return outcome
        sites += outcome.sites
        removal = outcome
    if not field.startswith("#"):
        reason = _indexed_by_string(field, repo_dir)
        if reason is not None:
            return Refusal(label, reason)
    return Removal(
        path,
        class_name,
        removal.param_name,
        label,
        removal.index,
        tuple(sites),
        removal.exported,
        field,
        removal.heirs,
    )


def _assignment(
    path: str, root: Node, cls: Node, write: Node, label: str, repo_dir: str
) -> Removal | Refusal:
    """The field's one write, ``this.<field> = <right>;``: the statement's
    site, and the parameter it reads if that was the parameter's last read."""
    access = write.parent
    assignment = access.parent if access is not None else None
    statement = assignment.parent if assignment is not None else None
    block = statement.parent if statement is not None else None
    ctor = block.parent if block is not None else None
    obj = access.child_by_field_name("object") if access is not None else None
    if (
        obj is None
        or obj.type != "this"
        or statement.type != "expression_statement"
        or block.type != "statement_block"
        or ctor is None
        or ctor.type != "method_definition"
        or ctor.parent != cls.child_by_field_name("body")
        or _text(ctor.child_by_field_name("name")) != "constructor"
    ):
        return Refusal(label, "assigned somewhere other than a statement of the constructor")
    if _asi_hazard(block, statement):
        return Refusal(label, "deleting the assignment would join the statements either side (no `;`)")
    site = Site(path, *_span(statement, list(block.children), with_doc=False), "statement")
    right = assignment.child_by_field_name("right")
    kept = Removal(path, _text(cls.child_by_field_name("name")), None, label, -1, (site,), False)
    parameter = _parameter(ctor, _text(right)) if right.type == "identifier" else None
    if parameter is None:
        # A literal reads nothing, so deleting it strands nothing. A name or a
        # property access off one might be a local only this statement read.
        if _inert(right) and not any(
            n.type in ("identifier", "shorthand_property_identifier") for n in _walk(right)
        ):
            return kept
        return Refusal(label, "assigned from something other than a constructor parameter or a literal")
    index, item = parameter
    if any(c.type in _PROPERTY_MODIFIERS for c in item.children):
        return kept  # a parameter property: a bare name is not its read
    others = [
        n
        for n in _walk(ctor)
        if n.type in ("identifier", "shorthand_property_identifier")
        and n.text == right.text
        and n != right
        and n != item.child_by_field_name("pattern")
    ]
    if others:
        for other in others:
            outer = other.parent
            if (
                outer is not None
                and outer.type == "assignment_expression"
                and outer.child_by_field_name("right") == other
                and (left := outer.child_by_field_name("left")) is not None
                and left.type == "member_expression"
            ):
                return Refusal(label, "the parameter also feeds another field")
        return kept
    if item.child_by_field_name("value") is not None:
        return Refusal(label, "the parameter has a default value")
    body = cls.child_by_field_name("body")
    ctors = ctor_param_ts._constructors(body)
    if len(ctors) != 1:
        return Refusal(label, "an overloaded constructor")
    callers = ctor_param_ts.constructor_callers(path, root, cls, ctor, index, label, repo_dir)
    if isinstance(callers, Refusal):
        return callers
    arguments, exported, heirs = callers
    return Removal(
        path, kept.class_name, _text(right), label, index, (site, *arguments), exported, heirs=heirs
    )
