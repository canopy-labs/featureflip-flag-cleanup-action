"""A public C# test helper whose ``bool`` parameter builds a test fake, called
from other files.

The shared-fixture form of :mod:`flag_cleanup.cs_param_pins`'s construction
pin::

    public static class TestExpiry
    {
        public static Expiry Create(Ctx c, bool expiration = true) =>
            new(c, new StubExpirationGate(expiration));
        public static Expiry Off(Ctx c) => Create(c, expiration: false);
    }

A private helper's calls are all in its file, so each test calling it with
the removed value can be deleted. A public one is called from anywhere, often
as a neutral baseline by tests about something else, so nothing is deleted:
the construction takes the kept literal, the parameter goes, and every call
loses its argument. Each member that passed the removed value now runs with
the kept one, and the pull request lists it for review.

Everything that has to hold, each checked rather than assumed:

* The construction passes a plain ``bool`` parameter of its method (no
  ``ref``/``in``/``out``/``params``, no attribute), with a ``true`` or
  ``false`` default or none, and read nowhere else in the method. A comment
  above it may name it only as ``<paramref name="p"/>``, which becomes
  ``<c>p</c>``: a dangling ``<paramref>`` is CS1734, a dangling ``<param>``
  CS1572, and any other mention is prose this cannot rewrite.
* The method is ``public`` or ``internal``, has no attribute, is not generic,
  ``extern`` or ``partial``. Its class is a top-level class that is not
  ``partial`` or generic and that nothing derives from: ``static`` (the
  method is then static), or ``sealed`` (an instance method, or a static
  one). A bare call of the name outside it is then another method.
* An instance method's calls are ``recv.M(…)``, so each receiver in a file
  that may hold a value of the class (:func:`_holders`: the class's own
  taint, as proof 2 reads it) must be typed: by a declaration spelling its
  type, a ``new``, a ``var`` from either, or a static method of a checkout
  type whose declared return type it reads. The class's name means its own
  call; any other type, another method. A receiver this cannot type
  refuses.
* The class's simple name is declared once in the checkout, no ``using
  static`` or alias names it, and no string or Razor file spells it (by
  name, reflection reaches anything).
* Every mention of the method's name in the checkout is a call ``M(…)``
  inside the class, ``Owner.M(…)``, or something else entirely (another
  receiver's ``M``). ``Owner.M`` that is not called, a method group, refuses.
* Each call passes the parameter as a ``true``/``false`` literal (by name, or
  positionally as the last argument) or relies on its default.
* No span the edit deletes holds a comment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from flag_cleanup import cs_param_pins, unused

_OPAQUE = frozenset({"partial", "extern", "abstract", "virtual", "override", "private", "protected"})
_RAZOR = (".razor", ".cshtml")
_STRINGS = cs_param_pins._STRINGS


@dataclass
class Plan:
    """What the shared pins of one run edit."""

    #: absolute path -> ``(start, end, replacement)`` edits.
    edits: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    #: absolute path -> ``(kind, text)`` report rows.
    rows: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Pin:
    path: str
    owner: str
    pin: cs_param_pins.CsPin
    construction: Node
    passed: Node
    #: Byte span of the owner class in its file.
    owner_span: tuple[int, int]
    #: The doc comment's ``<paramref>`` rewrites (:func:`_pin_of`).
    docs: tuple[tuple[int, int, str], ...] = ()


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _method_of(node: Node) -> Node | None:
    while node is not None and node.type != "method_declaration":
        node = node.parent
    return node


def _top_level_class(method: Node, data: bytes) -> Node | None:
    """The static or sealed, non-partial, non-generic top-level class
    declaring ``method``, or ``None``."""
    owner = method.parent.parent if method.parent is not None else None
    if owner is None or owner.type != "class_declaration":
        return None
    words = cs_param_pins._modifiers(owner, data)
    if not words & {"static", "sealed"} or "partial" in words:
        return None
    if any(c.type == "type_parameter_list" for c in owner.children):
        return None
    parent = owner.parent
    while parent is not None and parent.type == "declaration_list":
        parent = parent.parent
    if parent is None or parent.type not in ("compilation_unit", "namespace_declaration", "file_scoped_namespace_declaration"):
        return None
    return owner


#: A doc comment's reference to the parameter that can be rewritten.
_PARAMREF = r'<paramref\s+name\s*=\s*"{name}"\s*/>'


def _pin_of(
    root: Node, data: bytes, construction: Node, passed: Node
) -> tuple[str, cs_param_pins.CsPin, list[tuple[int, int, str]]] | None:
    """``(owner, pin, doc edits)`` for a construction in a public helper
    passing its parameter ``passed``, when the helper's own shape allows
    one; the edits turn each ``<paramref>`` to it into ``<c>…</c>``."""
    method = _method_of(construction)
    if method is None:
        return None
    words = cs_param_pins._modifiers(method, data)
    if not words & {"public", "internal"} or words & (_OPAQUE | {"new"}):
        return None
    if any(c.type in ("type_parameter_list", "attribute_list") for c in method.children):
        return None
    owner = _top_level_class(method, data)
    name_node = method.child_by_field_name("name")
    owner_name = owner.child_by_field_name("name") if owner is not None else None
    body = method.child_by_field_name("body")
    if owner_name is None or name_node is None or body is None:
        return None
    parameter = _text(passed, data).removeprefix("@")
    parameters = cs_param_pins._parameters(method)
    index = next(
        (i for i, p in enumerate(parameters) if (n := p.child_by_field_name("name")) is not None and _text(n, data) == parameter),
        None,
    )
    if index is None:
        return None
    declared = parameters[index]
    type_node = declared.child_by_field_name("type")
    if type_node is None or _text(type_node, data) != "bool":
        return None
    if any(c.type in ("modifier", "attribute_list") for c in declared.children):
        return None
    listed = method.child_by_field_name("parameters")
    if any(c.type not in ("parameter", "comment") for c in listed.named_children):
        return None
    ok, default = cs_param_pins._default(declared, data)
    if not ok:
        return None
    for node in cs_param_pins._walk(body):
        if node.type == "identifier" and _text(node, data).removeprefix("@") == parameter and node.id != passed.id:
            return None
    named = re.compile(rf"(?<![\w$@]){re.escape(parameter)}(?![\w$])")
    reference = re.compile(_PARAMREF.format(name=re.escape(parameter)))
    docs: list[tuple[int, int, str]] = []
    previous = method.prev_sibling
    while previous is not None and previous.type == "comment":
        comment = _text(previous, data)
        if named.search(reference.sub("", comment)):
            return None
        base = previous.start_byte
        for match in reference.finditer(comment):
            start = base + len(comment[: match.start()].encode("utf-8"))
            end = base + len(comment[: match.end()].encode("utf-8"))
            docs.append((start, end, f"<c>{parameter}</c>"))
        previous = previous.prev_sibling
    pin = cs_param_pins.CsPin(
        _text(name_node, data), parameter, index, default, method.start_byte, "static" in words
    )
    return _text(owner_name, data), pin, docs


def _bound_to_owner(repo_dir: str, owner: str, sources: dict[str, str]) -> bool:
    """Whether nothing but its own name can reach the class: one type of that
    name in the checkout, no ``using static`` or alias naming it (in C# or a
    project file), and no string or Razor file spelling it."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import source_index
    from flag_cleanup import wrapper_csharp as wc

    if len(xcs.csharp_index(repo_dir).by_last.get(owner, ())) != 1:
        return False
    word = re.compile(rf"(?<![\w@])@?{re.escape(owner)}(?![\w])")
    using = re.compile(rf"\busing\s+(?:static\s+[\w.:]*\b{re.escape(owner)}\b|\w+\s*=\s*[\w.:]*\b{re.escape(owner)}\b)")
    for f in source_index.files(repo_dir, {".cs", *_RAZOR, ".csproj", ".props", ".targets"}, with_ignored=True):
        name = f.path.name.lower()
        text = sources.get(f.resolved)
        if text is None:
            text = wc._read(f.resolved)
        if text is None:
            return False
        if word.search(text) is None:
            continue
        if not name.endswith(".cs"):
            return False  # markup, or a project item (`<Using Include=… Static="true">`)
        if using.search(text):
            return False
        root = wc.parse(text)
        data = text.encode("utf-8")
        if root.has_error:
            return False
        if any(n.type in _STRINGS and word.search(_text(n, data)) for n in cs_param_pins._walk(root)):
            return False
    return True


def _holders(repo_dir: str, owner: str):
    """The family whose proof 2 says which files may hold a value of the
    class ``owner``: one spelling a name that leads to it."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import csharp_family

    index = xcs.csharp_index(repo_dir)
    taint = csharp_family._taint(repo_dir, frozenset({owner}), frozenset(index.by_last), frozenset(index.by_full))
    return csharp_family.Family(
        full_names=frozenset(),
        simple_names=taint.family,
        members=frozenset(),
        settable=False,
        projects=frozenset(),
        returning=taint.returning,
        tainting=taint.tainting,
        opaque=taint.opaque,
        guest_named=taint.guest_named,
    )


_TYPE_DECLARATIONS = ("class_declaration", "struct_declaration", "record_declaration", "interface_declaration")


def _directly_in(node: Node, owner_span: tuple[int, int]) -> bool:
    """Whether the type declaration nearest around ``node`` is the owner
    (spanning ``owner_span``), not a type nested in it."""
    current = node.parent
    while current is not None and current.type not in _TYPE_DECLARATIONS:
        current = current.parent
    return current is not None and (current.start_byte, current.end_byte) == owner_span


#: Library interfaces whose members are known, none a test helper's name.
_KNOWN_BASES = {"IDisposable": frozenset({"Dispose"}), "IAsyncDisposable": frozenset({"DisposeAsync"})}


def _may_implement(repo_dir: str, owner: Node, method: str, data: bytes) -> bool:
    """Whether the method ``method`` of the class ``owner`` may implement or
    override a member of a base: any base the checkout does not declare
    (but :data:`_KNOWN_BASES`), or one up whose chain a type declares the
    name."""
    from flag_cleanup import cross_file_csharp as xcs

    index = xcs.csharp_index(repo_dir)
    bases = list(xcs._supertypes(owner))
    for base in bases:
        if base in _KNOWN_BASES and not index.by_last.get(base):
            if method in _KNOWN_BASES[base]:
                return True
            continue
        if not index.by_last.get(base) or index.spelled_where_unparsed(base):
            return True
        if method in index.inherited_members([base]):
            return True
    return False


def _static_return(repo_dir: str, type_name: str, method: str, sources: dict[str, str]) -> str | None:
    """The simple name of the type a static method ``method`` of the
    checkout's one type ``type_name`` declares it returns; ``None`` when
    that cannot be told (no such type or method, several, an unparsed file)."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import wrapper_csharp as wc

    index = xcs.csharp_index(repo_dir)
    infos = index.by_last.get(type_name, ())
    if len(infos) != 1 or index.spelled_where_unparsed(type_name):
        return None
    info = infos[0]
    text = sources.get(info.path)
    if text is None:
        text = wc._read(info.path)
    if text is None:
        return None
    root = wc.parse(text)
    if root.has_error:
        return None
    data = text.encode("utf-8")
    declared = wc._find_type_in(root, info.namespace, info.chain)
    body = wc._child(declared, "declaration_list") if declared is not None else None
    found = {
        wc._type_word(m.child_by_field_name("returns"))
        for m in (body.named_children if body is not None else ())
        if m.type == "method_declaration"
        and (n := m.child_by_field_name("name")) is not None
        and _text(n, data).removeprefix("@") == method
        and "static" in cs_param_pins._modifiers(m, data)
    }
    return found.pop() if len(found) == 1 else None


#: What :func:`_receiver_word` gives a receiver that names a type or a
#: namespace, never a value: through it, no instance method is called.
_NOT_A_VALUE = "<not a value>"


def _receiver_word(
    receiver: Node | None,
    readers,
    path: str,
    repo_dir: str,
    sources: dict[str, str],
    depth: int = 0,
    returning: frozenset[str] = frozenset(),
) -> str | None:
    """The simple name of the type a call's receiver certainly has: a
    declaration spelling it, a ``new``, a ``var`` or ``foreach`` variable
    from one of those, or a static method of a checkout type whose declared
    return type says it. :data:`_NOT_A_VALUE` for a name nothing in the
    file binds and no base supplies, which is a type or a namespace, unless
    it is one of the ``returning`` member names (a ``using static`` may
    bring one in as a value). ``None`` when that cannot be told."""
    from flag_cleanup import wrapper_csharp as wc

    node = wc._unwrap(receiver)
    while node is not None and node.type == "parenthesized_expression" and node.named_children:
        node = wc._unwrap(node.named_children[0])
    if node is None or depth > 8:
        return None
    if node.type == "object_creation_expression":
        return wc._type_word(node.child_by_field_name("type"))
    if node.type == "identifier":
        name = wc._text(node).removeprefix("@")
        if wc._pattern_binds(node, name):
            return None
        value = wc._implicit_value(node, name)
        if value is True:
            return None  # a lambda's parameter
        if isinstance(value, Node):
            return _receiver_word(value, readers, path, repo_dir, sources, depth + 1, returning)
        bound = wc._binding(node, name, readers, path)
        if bound is None and depth == 0:
            return None if name in returning else _NOT_A_VALUE
        return None if bound is None or isinstance(bound, str) else wc._type_word(bound)
    if node.type == "invocation_expression":
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_access_expression":
            return None
        holder = function.child_by_field_name("expression")
        called = function.child_by_field_name("name")
        if holder is None or holder.type != "identifier" or called is None or called.type != "identifier":
            return None
        # A local or member of the name is a value: its method, not a type's.
        if wc._binding(holder, wc._text(holder), readers, path) is not None:
            return None
        return _static_return(repo_dir, wc._text(holder).removeprefix("@"), wc._text(called).removeprefix("@"), sources)
    return None


def _calls(
    repo_dir: str,
    owner: str,
    name: str,
    owner_path: str,
    owner_span: tuple[int, int],
    sources: dict[str, str],
    static: bool = True,
) -> dict[str, list[Node]] | None:
    """absolute path -> the calls of ``owner``'s method ``name`` there:
    ``Owner.M(…)`` for a static one, a typed ``recv.M(…)`` for an instance
    one (see the module docstring); ``None`` when a mention is not a call,
    a receiver cannot be typed, or a file naming it cannot be read."""
    from flag_cleanup import csharp_family
    from flag_cleanup import source_index
    from flag_cleanup import wrapper_csharp as wc

    method = re.compile(rf"(?<![\w@])@?{re.escape(name)}(?![\w])")
    from flag_cleanup import cross_file_csharp as xcs

    holders = None if static else _holders(repo_dir, owner)
    readers = None if static else wc._Readers(repo_dir)

    def xcs_reader(root: Node, path: str):
        return xcs._Reader(root, path, repo_dir)

    found: dict[str, list[Node]] = {}
    for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
        if not f.path.name.endswith(".cs"):
            continue
        text = sources.get(f.resolved)
        if text is None:
            text = wc._read(f.resolved)
        if text is None:
            return None
        if method.search(text) is None:
            continue
        root = wc.parse(text)
        data = text.encode("utf-8")
        own = f.resolved == owner_path
        for node in cs_param_pins._walk(root):
            if node.type != "identifier" or _text(node, data).removeprefix("@") != name:
                continue
            parent = node.parent
            if parent is None:
                continue
            if parent.type == "method_declaration" and parent.child_by_field_name("name") == node:
                continue
            if parent.type == "member_binding_expression":
                return None  # `h?.M(…)`: a receiver this does not type
            if parent.type == "member_access_expression" and parent.child_by_field_name("name") == node:
                receiver = parent.child_by_field_name("expression")
                if static:
                    last = _text(receiver, data).rsplit(".", 1)[-1].rsplit("::", 1)[-1] if receiver is not None else ""
                    if last.removeprefix("@") != owner:
                        continue  # another type's member of the name
                elif receiver is not None and receiver.type in ("this", "base"):
                    if not (own and owner_span[0] <= node.start_byte < owner_span[1]):
                        continue  # another type's own member
                    if not _directly_in(node, owner_span):
                        return None  # a nested type's `this` may have a method of the name
                else:
                    if csharp_family.family_free(text, root, holders):
                        continue  # no value of the class can be here
                    reader = xcs_reader(root, f.resolved)
                    readers.add(reader)
                    word = _receiver_word(receiver, readers, f.resolved, repo_dir, sources, returning=holders.returning)
                    if word is None or word == "dynamic":
                        return None
                    if word != owner:
                        continue  # another type's member of the name
                call = parent.parent
                if call is None or call.type != "invocation_expression" or call.child_by_field_name("function") != parent:
                    return None  # a method group, `nameof`, or the like
                found.setdefault(f.resolved, []).append(call)
                continue
            inside = own and owner_span[0] <= node.start_byte < owner_span[1]
            if not inside:
                continue  # a bare name outside the class is another method
            if parent.type != "invocation_expression" or parent.child_by_field_name("function") != node:
                return None
            if not _directly_in(node, owner_span):
                return None  # a nested type may declare a method of the name
            found.setdefault(f.resolved, []).append(parent)
    return found


def _rebuilt(
    repo_dir: str, pin: _Pin, path: str, call: Node, data: bytes, sources: dict[str, str]
) -> list[tuple[str, str]]:
    """``(path, where)`` of each member that now runs with the kept value
    because of ``call``: the member around it, or, when that is another
    public static method of the helper's class (``Off() => Create(…, false)``),
    each member calling that one."""
    member = _enclosing_method(call)
    if (
        member is not None
        and path == pin.path
        and pin.owner_span[0] <= member.start_byte < pin.owner_span[1]
        and "static" in cs_param_pins._modifiers(member, data)
        and cs_param_pins._modifiers(member, data) & {"public", "internal"}
        and (name := member.child_by_field_name("name")) is not None
    ):
        callers = _calls(repo_dir, pin.owner, _text(name, data), pin.path, pin.owner_span, sources) or {}
        found = []
        for caller_path, nodes in callers.items():
            caller_text = sources.get(caller_path)
            if caller_text is None:
                from flag_cleanup import wrapper_csharp as wc

                caller_text = wc._read(caller_path) or ""
            caller_data = caller_text.encode("utf-8")
            for node in nodes:
                found.append((caller_path, f"{_enclosing_member(node, caller_data)} (line {node.start_point[0] + 1})"))
        if found:
            return found
    return [(path, f"{_enclosing_member(call, data)} (line {call.start_point[0] + 1})")]


def _enclosing_method(node: Node) -> Node | None:
    while node is not None and node.type != "method_declaration":
        node = node.parent
    return node


def _enclosing_member(node: Node, data: bytes) -> str:
    while node is not None and node.type not in ("method_declaration", "local_function_statement", "constructor_declaration", "property_declaration", "field_declaration"):
        node = node.parent
    if node is None:
        return "?"
    name = node.child_by_field_name("name")
    if name is None and node.type == "field_declaration":
        declarator = next((d for d in cs_param_pins._walk(node) if d.type == "variable_declarator"), None)
        name = declarator.child_by_field_name("name") if declarator is not None else None
    return _text(name, data) if name is not None else "?"


def _pins(
    repo_dir: str, helpers: dict[str, str], owners: dict[str, dict[str, tuple[str, bool]]]
) -> list[_Pin]:
    """Each public helper in ``helpers`` whose parameter builds a fake proven
    there (``owners``), as far as its own file and class say: its calls are
    judged by :func:`plan`."""
    from flag_cleanup import mock_stubs
    from flag_cleanup import wrapper_csharp as wc

    pins: list[_Pin] = []
    for path, text in helpers.items():
        known = owners.get(path) or {}
        if not known:
            continue
        root = wc.parse(text)
        data = text.encode("utf-8")
        if root.has_error:
            continue
        for node in cs_param_pins._walk(root):
            if node.type != "object_creation_expression":
                continue
            if mock_stubs._construction_value(node, known, data) is not mock_stubs._UNREADABLE:
                continue
            passed = mock_stubs._construction_parameter(node, known, data)
            if passed is None:
                continue
            found = _pin_of(root, data, node, passed)
            if found is not None:
                owner = _top_level_class(_method_of(node), data)
                body = owner.child_by_field_name("body")
                overloads = [
                    m for m in (body.named_children if body is not None else ())
                    if m.type == "method_declaration"
                    and (n := m.child_by_field_name("name")) is not None
                    and _text(n, data).removeprefix("@") == found[1].method
                ]
                if len(overloads) != 1 or _may_implement(repo_dir, owner, found[1].method, data):
                    continue  # an overload or a base member may take the parameter's place
                pins.append(
                    _Pin(path, found[0], found[1], node, passed, (owner.start_byte, owner.end_byte), tuple(found[2]))
                )
    return pins


def forwards(
    repo_dir: str,
    helpers: dict[str, str],
    owners: dict[str, dict[str, tuple[str, bool]]],
    sources: dict[str, str],
) -> frozenset[tuple[str, str, str, int]]:
    """``(owner, method, parameter, index)`` of each static helper of
    :func:`_pins` whose class's simple name is proven to mean it: a private
    helper's pin may pass its own parameter on to one
    (:func:`flag_cleanup.cs_param_pins.find`). Its argument then takes the
    kept literal, which :func:`plan` cuts like any other; should the plan
    refuse, the literal stays, and builds the fake every test left runs."""
    sources = {**sources, **helpers}
    return frozenset(
        (pin.owner, pin.pin.method, pin.pin.parameter, pin.pin.index)
        for pin in _pins(repo_dir, helpers, owners)
        if pin.pin.static and _bound_to_owner(repo_dir, pin.owner, sources)
    )


def plan(
    repo_dir: str,
    helpers: dict[str, str],
    owners: dict[str, dict[str, tuple[str, bool]]],
    kept: str,
    sources: dict[str, str],
) -> Plan | None:
    """The shared pins in ``helpers`` (absolute path -> text), with the fakes
    proven in each (``owners``); ``None`` when there are none. ``sources``
    holds the text this run has already given a file, which wins over disk."""
    from flag_cleanup import mock_stubs
    from flag_cleanup import wrapper_csharp as wc

    removed = "false" if kept == "true" else "true"
    pins = _pins(repo_dir, helpers, owners)
    result = Plan()
    sources = {**sources, **helpers}
    for pin in pins:
        data = helpers[pin.path].encode("utf-8")
        if not _bound_to_owner(repo_dir, pin.owner, sources):
            continue
        calls = _calls(repo_dir, pin.owner, pin.pin.method, pin.path, pin.owner_span, sources, pin.pin.static)
        if calls is None:
            continue
        edits: dict[str, list[tuple[int, int, str]]] = {}
        review: list[tuple[str, str]] = []
        cut = 0
        refused = False
        #: path -> the tests that pass the removed value explicitly, which go.
        deleted: dict[str, list[tuple[int, int, str]]] = {}
        for path, nodes in calls.items():
            file_text = sources.get(path)
            if file_text is None:
                file_text = wc._read(path) or ""
            file_data = file_text.encode("utf-8")
            file_root = wc.parse(file_text)
            for call in nodes:
                argument, deletable = cs_param_pins._argument_at(call, pin.pin, file_data)
                if argument is None:
                    if not deletable or pin.pin.default is None:
                        refused = True
                        break
                    value = pin.pin.default
                    if value == removed:
                        # The call never chose the removed value, yet runs with
                        # it: whether its test still holds with the kept one
                        # cannot be told, so the pin is not made.
                        refused = True
                        break
                else:
                    literal = argument.named_children[-1] if argument.named_children else None
                    if not deletable or literal is None or literal.type != "boolean_literal":
                        refused = True
                        break
                    value = _text(literal, file_data)
                    test = _enclosing_test(call, file_data)
                    if value == removed and test is not None:
                        # A test about the removed value goes, as a private
                        # helper's does.
                        start, end = unused._expand_to_whole_lines(
                            file_data, mock_stubs._with_leading_comments(test, file_data), test.end_byte
                        )
                        # The type's last member: the blank line above it goes too.
                        if file_data[:start].endswith(b"\n\n") and file_data[end:].lstrip(b" \t").startswith(b"}"):
                            start -= 1
                        span = (start, end, _text(test.child_by_field_name("name"), file_data))
                        if span not in deleted.setdefault(path, []):
                            deleted[path].append(span)
                        continue
                    start, end = cs_param_pins._list_span(argument)
                    if cs_param_pins._holds_comment(file_root, start, end):
                        refused = True
                        break
                    edits.setdefault(path, []).append((start, end, ""))
                    cut += 1
                if value == removed:
                    review += _rebuilt(repo_dir, pin, path, call, file_data, sources)
            if refused:
                break
        if refused:
            continue
        # An argument cut inside a test that goes is no edit of its own.
        for path, spans in deleted.items():
            kept_edits = [
                e for e in edits.get(path, []) if not any(s <= e[0] and e[1] <= t for s, t, _ in spans)
            ]
            cut -= len(edits.get(path, [])) - len(kept_edits)
            edits[path] = kept_edits + [(s, t, "") for s, t, _ in spans]
        review = [
            (path, where)
            for path, where in review
            if not any(where.startswith(f"{name} (") for _, _, name in deleted.get(path, []))
        ]
        method = _method_of(pin.construction)
        root = pin.construction
        while root.parent is not None:
            root = root.parent
        parameter = cs_param_pins._parameters(method)[pin.pin.index]
        start, end = cs_param_pins._list_span(parameter)
        if cs_param_pins._holds_comment(root, start, end):
            continue
        listed = method.child_by_field_name("parameters")
        signature = " ".join(f"{pin.owner}.{pin.pin.method}{_text(listed, data)}".split())
        for path, spans in edits.items():
            result.edits.setdefault(path, []).extend(spans)
        result.edits.setdefault(pin.path, []).extend(
            [(start, end, ""), (pin.passed.start_byte, pin.passed.end_byte, kept), *pin.docs]
        )
        said = f"removed from {cut} call{'' if cut == 1 else 's'}" if cut else "removed; no call passed it"
        result.rows.setdefault(pin.path, []).append(
            (
                mock_stubs.PINNED_PARAMETER,
                f"{mock_stubs.code_span(signature)}: parameter `{pin.pin.parameter}` {said}, "
                f"the fake built with `{kept}`",
            )
        )
        for path, where in review:
            result.rows.setdefault(path, []).append((mock_stubs.REBUILT, where))
        for path, spans in deleted.items():
            result.rows.setdefault(path, []).extend((mock_stubs.TEST, name) for _, _, name in spans)
    return result if result.edits else None


def _enclosing_test(node: Node, data: bytes) -> Node | None:
    """The test method around ``node``, if it is in one."""
    from flag_cleanup import mock_stubs

    method = _enclosing_method(node)
    return method if method is not None and mock_stubs._csharp_any_test(method, data) else None


def apply(text: str, edits: list[tuple[int, int, str]]) -> str | None:
    """``text`` with ``edits`` made, or ``None`` when two overlap."""
    data = text.encode("utf-8")
    ordered = sorted(set(edits))
    for (s1, e1, _), (s2, _e2, _) in zip(ordered, ordered[1:]):
        if s2 < e1:
            return None
    for start, end, replacement in reversed(ordered):
        data = data[:start] + replacement.encode("utf-8") + data[end:]
    return data.decode("utf-8")
