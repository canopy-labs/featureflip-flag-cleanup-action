"""A factory's members tested generically: ``describe.each([...names])``.

A wrapper's own tests are sometimes written once for every member::

    describe.each(["isA", "isB"])("%s", (method) => {
      it("forwards", async () => {
        const flags = buildFlags({ isEnabled: vi.fn().mockResolvedValue(true) });
        expect(await flags[method]("u")).toBe(true);
      });
    });

The computed member ``[method]`` is not a call this run can prove, and the
member names are strings, so every member named there used to stay. When the
statement is exactly the shape below, the row naming a removed member is cut
(the whole statement, when it was the last row) and the rows left are about
other members.

The rule is a WHITELIST of positions. Anything this module does not recognise
is not a row, and the name stays a mention that keeps the wrapper:

* a top-level ``describe.each(rows)(title, cb)`` (or ``it.each`` / ``test.each``)
  with no modifier, so no ``.only`` / ``.skip``;
* ``rows`` a literal array of plain strings, no comment, no spread, no
  repeats, so no table is read through a variable or a template;
* ``cb`` a function with ONE plain identifier parameter, no default, no
  destructuring;
* every use of that parameter in the callback is ``<object>[param](...)``, a
  plain call, with ``<object>`` either a call of one identifier (the
  factory), or a ``const`` of the callback that is bound once to such a
  call;
* every call in the callback is the factory's, one of those subscript calls,
  an ``expect(...)`` chain, a ``vi.fn()`` / ``jest.fn()`` chain of the
  stubbing members, or a nested ``it`` / ``test`` call, so nothing else
  could be about the member;
* the file keeps a test once the statement is gone (or is deleted whole).
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import cross_file_keys, ts_syntax

#: The receiver a proven row is recorded under in a wrapper's sibling pairs.
EACH = "[each]"
#: What a scope is prefixed with, so no ordinary test's title can equal one.
SCOPE = "each\x00"

_CALLEES = frozenset({"describe", "it", "test"})
_CALLBACKS = frozenset({"arrow_function", "function_expression", "function"})
#: The matchers an assertion about a forwarded value may use. A snapshot matcher
#: is not among them: cutting its test would orphan the stored snapshot.
_MATCHERS = frozenset(
    {
        "not", "resolves", "rejects", "toBe", "toEqual", "toStrictEqual", "toBeTruthy", "toBeFalsy",
        "toHaveBeenCalled", "toHaveBeenCalledTimes", "toHaveBeenCalledWith", "toHaveBeenLastCalledWith",
        "toHaveBeenNthCalledWith",
    }
)
_STUBBING = frozenset({"fn", "mockResolvedValue", "mockReturnValue", "mockResolvedValueOnce", "mockReturnValueOnce"})
#: A name in a binding position: the parameter may not be shadowed or reused.
_BINDERS = frozenset(
    {
        "variable_declarator", "required_parameter", "optional_parameter", "formal_parameters", "catch_clause",
        "array_pattern", "object_pattern", "assignment_pattern", "rest_pattern", "pair_pattern",
        "function_declaration", "class_declaration", "for_in_statement", "import_specifier", "namespace_import",
        "import_clause",
    }
)


@dataclass(frozen=True)
class Each:
    statement: Node
    #: :data:`SCOPE` and the title.
    scope: str
    #: ``(name, node)`` of each row.
    rows: tuple[tuple[str, Node], ...]
    #: The identifiers the factory is called by.
    factories: frozenset[str]
    #: ``(start, end)`` bytes of each ``<object>[param]`` of the callback.
    subscripts: frozenset[tuple[int, int]] = frozenset()


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _plain_string(node: Node) -> str | None:
    """The value of a string with nothing but plain text in it."""
    if node.type != "string":
        return None
    parts = node.named_children
    if len(parts) != 1 or parts[0].type != "string_fragment":
        return None
    value = _text(parts[0])
    return value if "\\" not in value else None


def _title(node: Node) -> str | None:
    if node.type in ("string", "template_string") and all(c.type == "string_fragment" for c in node.named_children):
        return "".join(_text(c) for c in node.named_children)
    return None


def _root_name(call: Node) -> tuple[str | None, list[str]]:
    """The identifier a call's callee chain starts at, and the member names on the way."""
    members: list[str] = []
    node: Node | None = call.child_by_field_name("function")
    while node is not None:
        if node.type == "identifier":
            return _text(node), members[::-1]
        if node.type == "member_expression":
            members.append(_text(node.child_by_field_name("property")))
            node = node.child_by_field_name("object")
        elif node.type == "call_expression":
            node = node.child_by_field_name("function")
        else:
            return None, members
    return None, members


def _parameter(callback: Node) -> str | None:
    single = callback.child_by_field_name("parameter")
    if single is not None:
        return _text(single) if single.type == "identifier" else None
    params = callback.child_by_field_name("parameters")
    if params is None or params.type != "formal_parameters":
        return None
    named = [c for c in params.named_children if c.type != "comment"]
    if len(named) != 1 or named[0].type != "required_parameter":
        return None
    pattern = named[0].child_by_field_name("pattern")
    if pattern is None or pattern.type != "identifier" or named[0].child_by_field_name("value") is not None:
        return None
    return _text(pattern)


def _binds(name: str, body: Node) -> list[Node]:
    """The identifiers of ``body`` that bind ``name``."""
    found = []
    for n in cross_file_keys.walk(body):
        if n.type == "shorthand_property_identifier_pattern" and _text(n) == name:
            found.append(n)
            continue
        if n.type != "identifier" or _text(n) != name or n.parent is None or n.parent.type not in _BINDERS:
            continue
        if n.parent.type == "variable_declarator" and n.parent.child_by_field_name("name") != n:
            continue
        found.append(n)
    return found


def _subscript_calls(body: Node, param: str) -> list[Node] | None:
    """The subscripts ``<object>[param]`` of the callback, each the function of a
    plain call, or ``None`` when the parameter is used in any other position."""
    found: list[Node] = []
    for n in cross_file_keys.walk(body):
        if n.type == "shorthand_property_identifier" and _text(n) == param:
            return None
        if n.type != "identifier" or _text(n) != param:
            continue
        sub = n.parent
        if sub is None or sub.type != "subscript_expression" or sub.child_by_field_name("index") != n:
            return None
        if any(c.type == "?." for c in sub.children):
            return None
        call = sub.parent
        if call is None or call.type != "call_expression" or call.child_by_field_name("function") != sub:
            return None
        if any(c.type == "?." for c in call.children) or call.child_by_field_name("type_arguments") is not None:
            return None
        found.append(sub)
    return found or None


def _factory_of(obj: Node, body: Node) -> str | None:
    """The factory a subscript's object is: ``F(...)`` or a ``const x = F(...)``."""
    if obj.type == "call_expression":
        callee = obj.child_by_field_name("function")
        return _text(callee) if callee is not None and callee.type == "identifier" else None
    if obj.type != "identifier":
        return None
    bound = _binds(_text(obj), body)
    if len(bound) != 1 or bound[0].parent is None or bound[0].parent.type != "variable_declarator":
        return None
    declarator = bound[0].parent
    statement = declarator.parent
    if statement is None or statement.type != "lexical_declaration" or not any(c.type == "const" for c in statement.children):
        return None
    value = declarator.child_by_field_name("value")
    return _factory_of(value, body) if value is not None and value.type == "call_expression" else None


def _takes_no_parameters(call: Node) -> bool:
    """Whether every function handed to ``call`` declares no parameter."""
    arguments = call.child_by_field_name("arguments")
    for arg in [] if arguments is None else arguments.named_children:
        if arg.type not in _CALLBACKS:
            continue
        if arg.child_by_field_name("parameter") is not None:
            return False
        params = arg.child_by_field_name("parameters")
        if params is not None and params.named_children:
            return False
    return True


def _calls_allowed(body: Node, subscripts: list[Node], factories: frozenset[str]) -> bool:
    ours = {s.id for s in subscripts}
    for n in cross_file_keys.walk(body):
        if n.type == "new_expression":
            return False
        if n.type != "call_expression":
            continue
        function = n.child_by_field_name("function")
        if function is not None and function.type == "subscript_expression":
            if function.id not in ours:
                return False
            continue
        root, members = _root_name(n)
        if root is None:
            return False
        if root == "expect" and all(m in _MATCHERS for m in members):
            continue
        if root in factories and not members:
            continue
        if root in ("it", "test") and not members and _takes_no_parameters(n):
            continue  # a parameter (`ctx`, `done`) can read the row: `ctx.task.name`
        if root in ("vi", "jest") and all(m in _STUBBING for m in members):
            continue
        return False
    return True


# NOTE: deliberately conservative. A file holding a two-row block, a one-row
# block and an export refuses every one of them, because the joint check below
# imagines ALL whitelisted statements deleted, whatever names are removed.
def _after_ok(found: list[Each], source: str, language: str) -> bool:
    """Whether the file is still sound with EVERY such statement gone: it keeps
    a test, or holds only residue and exports nothing (the run then deletes
    it). Judged over all of them together, whatever names are removed, so a
    statement is never cut while another that goes with it stays."""
    from flag_cleanup import ts_emptied_tests, unused

    after = source
    for each in sorted(found, key=lambda e: e.statement.start_byte, reverse=True):
        st = each.statement
        after = unused._delete_method(
            after, unused.Candidate("each", st.start_byte, st.end_byte, "method", _text(st))
        )
    if ts_emptied_tests._any_test(after, language):
        return True
    root = ts_syntax._parse(after, language)
    return ts_emptied_tests._only_residue(root.named_children, b"", True) and not ts_emptied_tests._exports(after, language)


def analyse(statement: Node, source: str, language: str) -> Each | None:
    """The :class:`Each` of a statement of exactly the whitelisted shape, else ``None``."""
    if statement.type != "expression_statement" or statement.parent is None or statement.parent.type != "program":
        return None
    named = [c for c in statement.named_children if c.type != "comment"]
    if len(named) != 1 or named[0].type != "call_expression":
        return None
    call = named[0]
    each = call.child_by_field_name("function")
    arguments = call.child_by_field_name("arguments")
    if each is None or each.type != "call_expression" or arguments is None or arguments.type != "arguments":
        return None
    if call.child_by_field_name("type_arguments") is not None or each.child_by_field_name("type_arguments") is not None:
        return None
    member = each.child_by_field_name("function")
    rows_args = each.child_by_field_name("arguments")
    if member is None or member.type != "member_expression" or rows_args is None or rows_args.type != "arguments":
        return None
    prop, root = member.child_by_field_name("property"), member.child_by_field_name("object")
    if prop is None or _text(prop) != "each" or root is None or root.type != "identifier" or _text(root) not in _CALLEES:
        return None
    if any(c.type == "?." for c in member.children):
        return None
    if any(c.type == "comment" for c in arguments.children) or any(c.type == "comment" for c in rows_args.children):
        return None
    args = arguments.named_children
    row_args = rows_args.named_children
    if len(args) != 2 or len(row_args) != 1 or row_args[0].type != "array" or args[1].type not in _CALLBACKS:
        return None
    title = _title(args[0])
    callback = args[1]
    array = row_args[0]
    if title is None or any(c.type == "comment" for c in array.children):
        return None
    rows: list[tuple[str, Node]] = []
    for element in array.named_children:
        value = _plain_string(element)
        if value is None:
            return None
        rows.append((value, element))
    if not rows or len({v for v, _ in rows}) != len(rows):
        return None
    param = _parameter(callback)
    body = callback.child_by_field_name("body")
    if param is None or body is None or body.type != "statement_block" or _binds(param, body):
        return None
    if any(n.type == "identifier" and _text(n) == "arguments" for n in cross_file_keys.walk(body)):
        return None  # `arguments[0]` reaches the row without naming the parameter
    subscripts = _subscript_calls(body, param)
    if subscripts is None:
        return None
    factories = set()
    for sub in subscripts:
        obj = sub.child_by_field_name("object")
        found = None if obj is None else _factory_of(obj, body)
        if found is None:
            return None
        factories.add(found)
    frozen = frozenset(factories)
    if not _calls_allowed(body, subscripts, frozen):
        return None
    return Each(statement, SCOPE + title, tuple(rows), frozen, frozenset((s.start_byte, s.end_byte) for s in subscripts))


def statements(source: str, language: str) -> list[Each]:
    root = ts_syntax._parse(source, language)
    found = [e for e in (analyse(n, source, language) for n in root.named_children) if e is not None]
    return found if found and _after_ok(found, source, language) else []


def row_of(node: Node, source: str, language: str) -> tuple[Each, str] | None:
    """``(statement, name)`` when ``node`` (a string, or its fragment) is a row
    of a whitelisted statement whose scope is the file's only one of that title."""
    literal = node.parent if node.type == "string_fragment" else node
    if literal is None or literal.type != "string":
        return None
    found = statements(source, language)
    for each in found:
        for name, row in each.rows:
            if row.start_byte == literal.start_byte and row.end_byte == literal.end_byte:
                if sum(1 for other in found if other.scope == each.scope) != 1:
                    return None
                return each, name
    return None


def cut(source: str, language: str, names: frozenset[str], scopes: frozenset[str]) -> tuple[str, list[tuple[str, str]]]:
    """``source`` with the rows naming one of ``names`` cut from the statements
    whose scope is in ``scopes``, and what was cut: ``(title, name)`` for a
    row, ``(title, "")`` for a statement left with none."""
    from flag_cleanup import mock_stubs, unused

    removed: list[tuple[str, str]] = []
    while True:
        found = statements(source, language)
        counts: dict[str, int] = {}
        for each in found:
            counts[each.scope] = counts.get(each.scope, 0) + 1
        target = next(
            (e for e in found if e.scope in scopes and counts[e.scope] == 1 and any(n in names for n, _ in e.rows)), None
        )
        if target is None:
            return source, removed
        title = target.scope[len(SCOPE):]
        data = source.encode("utf-8")
        if all(n in names for n, _ in target.rows):
            start = mock_stubs._with_leading_comments(target.statement, data)
            source = unused._delete_method(
                source, unused.Candidate(title, start, target.statement.end_byte, "method", _text(target.statement))
            )
            removed += [(title, n) for n, _ in target.rows]
            removed.append((title, ""))
            continue
        index = next(i for i, (n, _) in enumerate(target.rows) if n in names)
        name, row = target.rows[index]
        if index + 1 < len(target.rows):
            start, end = row.start_byte, target.rows[index + 1][1].start_byte
        else:
            start, end = target.rows[index - 1][1].end_byte, row.end_byte
        source = (data[:start] + data[end:]).decode("utf-8")
        removed.append((title, name))


def row_subscript(node: Node, source: str, language: str) -> bool:
    """Whether ``node`` is the object of one of a whitelisted statement's
    ``<object>[param]`` subscripts: the factory call itself, or the ``const``
    that holds it."""
    sub = node.parent
    if sub is None or sub.type != "subscript_expression" or sub.child_by_field_name("object") != node:
        return False
    top = sub
    while top.parent is not None and top.parent.type != "program":
        top = top.parent
    if top.parent is None:
        return False
    return any(
        e.statement.start_byte == top.start_byte and (sub.start_byte, sub.end_byte) in e.subscripts
        for e in statements(source, language)
    )
