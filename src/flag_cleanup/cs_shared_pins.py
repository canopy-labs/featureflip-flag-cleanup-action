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
  ``false`` default or none, read nowhere else in the method, and named by
  no comment above it (a dangling ``<paramref>`` is CS1734).
* The method is ``public`` or ``internal`` and ``static``, has no attribute,
  is not generic, ``extern`` or ``partial``. Its class is a top-level
  ``static`` class that is not ``partial`` or generic: nothing derives from
  it, so a bare call of the name outside it is another method.
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

from flag_cleanup import cs_param_pins

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


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _method_of(node: Node) -> Node | None:
    while node is not None and node.type != "method_declaration":
        node = node.parent
    return node


def _top_level_class(method: Node, data: bytes) -> Node | None:
    """The static, non-partial, non-generic top-level class declaring
    ``method``, or ``None``."""
    owner = method.parent.parent if method.parent is not None else None
    if owner is None or owner.type != "class_declaration":
        return None
    words = cs_param_pins._modifiers(owner, data)
    if "static" not in words or "partial" in words:
        return None
    if any(c.type == "type_parameter_list" for c in owner.children):
        return None
    parent = owner.parent
    while parent is not None and parent.type == "declaration_list":
        parent = parent.parent
    if parent is None or parent.type not in ("compilation_unit", "namespace_declaration", "file_scoped_namespace_declaration"):
        return None
    return owner


def _pin_of(root: Node, data: bytes, construction: Node, passed: Node) -> tuple[str, cs_param_pins.CsPin] | None:
    """``(owner, pin)`` for a construction in a public static helper passing
    its parameter ``passed``, when the helper's own shape allows one."""
    method = _method_of(construction)
    if method is None:
        return None
    words = cs_param_pins._modifiers(method, data)
    # Its class is static (:func:`_top_level_class`), so it is too.
    if not words & {"public", "internal"} or words & _OPAQUE:
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
    previous = method.prev_sibling
    while previous is not None and previous.type == "comment":
        if named.search(_text(previous, data)):
            return None
        previous = previous.prev_sibling
    return _text(owner_name, data), cs_param_pins.CsPin(_text(name_node, data), parameter, index, default, method.start_byte)


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


def _calls(
    repo_dir: str, owner: str, name: str, owner_path: str, owner_span: tuple[int, int], sources: dict[str, str]
) -> dict[str, list[Node]] | None:
    """absolute path -> the calls of ``owner``'s static method ``name``
    there; ``None`` when a mention of ``Owner.M`` is not a call, or a file
    naming it cannot be read."""
    from flag_cleanup import source_index
    from flag_cleanup import wrapper_csharp as wc

    method = re.compile(rf"(?<![\w@])@?{re.escape(name)}(?![\w])")
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
            if parent.type == "member_access_expression" and parent.child_by_field_name("name") == node:
                receiver = parent.child_by_field_name("expression")
                last = _text(receiver, data).rsplit(".", 1)[-1].rsplit("::", 1)[-1] if receiver is not None else ""
                if last.removeprefix("@") != owner:
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
                pins.append(_Pin(path, found[0], found[1], node, passed, (owner.start_byte, owner.end_byte)))
    result = Plan()
    sources = {**sources, **helpers}
    for pin in pins:
        data = helpers[pin.path].encode("utf-8")
        if not _bound_to_owner(repo_dir, pin.owner, sources):
            continue
        calls = _calls(repo_dir, pin.owner, pin.pin.method, pin.path, pin.owner_span, sources)
        if calls is None:
            continue
        edits: dict[str, list[tuple[int, int, str]]] = {}
        review: list[tuple[str, str]] = []
        cut = 0
        refused = False
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
                else:
                    literal = argument.named_children[-1] if argument.named_children else None
                    if not deletable or literal is None or literal.type != "boolean_literal":
                        refused = True
                        break
                    value = _text(literal, file_data)
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
            [(start, end, ""), (pin.passed.start_byte, pin.passed.end_byte, kept)]
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
    return result if result.edits else None


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
