"""Remove a C# method parameter the fold left unread, with every caller's argument.

A gate passed down to a helper is the case this exists for::

    public static class SecretFormat
    {
        public static string ForStorage(string secret, IProtector protector, IGate gate) =>
            gate.IsEnabled ? protector.Protect(secret) : secret;
    }

Once the read is folded, ``gate`` is read by nothing, yet every caller still
passes its own field of the gate type, which keeps that field, its
constructor parameter and the gate's interface alive. The parameter and each
argument passed for it go, so what passed the argument can be judged again.

Only a parameter of one of the run's own gate interfaces is considered (see
:func:`unread_parameters`), and only when every call can be found:

* The method is ``static``, not ``extern``, ``partial``, ``abstract``,
  ``virtual``, ``override`` or ``new``, has no attribute and no type
  parameter, and is the only member of its name in its class. The class is a
  top-level ``static`` class that is not ``partial`` or generic, so nothing
  derives from it, nothing implements an interface through it, and a call
  of the name outside it is another method unless it is ``Owner.M(…)``.
* The parameter has no attribute, modifier (``this``, ``ref``, ``in``,
  ``out``, ``params``, ``scoped``) or default value, and the method's doc
  comment does not name it (a dangling ``<param>`` is CS1572, a
  ``<paramref>`` CS1734).
* The class's simple name is declared once in the checkout, no ``using
  static`` or alias names it, no string, Razor or project file spells it,
  and no ``typeof`` of it is taken (reflection could then call the method
  with the old number of arguments).
* Every mention of the method's name is a call: ``M(…)`` directly inside the
  class or ``Owner.M(…)`` anywhere. A method group, ``nameof`` or a
  conditional access refuses (:func:`flag_cleanup.cs_shared_pins._calls`).
* Each call passes an inert argument for the parameter (a name, a member of
  one, a literal), positionally or by name, which deleting cannot skip any
  work for.
* A ``public`` method of a project that may be packed refuses: code outside
  the repository may call it.

The deletion is the engine's, like a constructor parameter's
(:mod:`flag_cleanup.ctor_param_removal`): the parameter is marked as
``object <sentinel>`` and each argument as the sentinel, and the rules in
``csharp_ctor_param.toml`` delete both by that identity.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cs_shared_pins, ctor_param_csharp, syntax
from flag_cleanup.ctor_param_model import Refusal, Removal, Site

#: What a candidate's label looks like: ``Owner.Method(IGate gate)``.
_LABEL = re.compile(r"(\w+)\.(\w+)\((.+) (\w+)\)")
_OPAQUE = frozenset({"extern", "partial", "abstract", "virtual", "override", "new"})


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _parse(source: str) -> Node:
    return syntax._parse(source.removeprefix("﻿"), "csharp")


def _simple(type_node: Node | None) -> str | None:
    node = type_node
    while node is not None and node.type == "nullable_type" and node.named_children:
        node = node.named_children[0]
    if node is None:
        return None
    if node.type == "qualified_name":
        node = node.child_by_field_name("name")
    return _text(node) if node is not None and node.type == "identifier" else None


def _reads(method: Node, name: str) -> int:
    """How many times the method's body spells ``name`` as something other
    than a member's name after a dot."""
    body = method.child_by_field_name("body") or next(
        (c for c in method.children if c.type == "arrow_expression_clause"), None
    )
    if body is None:
        return 0
    count = 0
    for n in _walk(body):
        if n.type != "identifier" or _text(n).removeprefix("@") != name:
            continue
        parent = n.parent
        if parent is not None and parent.type == "member_access_expression" and parent.child_by_field_name("name") == n:
            continue
        count += 1
    return count


def _methods(root: Node):
    """``(owner name, method, parameter)`` for every parameter of every
    static method of a top-level class."""
    for method in _walk(root):
        if method.type != "method_declaration" or "static" not in ctor_param_csharp._modifiers(method):
            continue
        owner = method.parent.parent if method.parent is not None else None
        if owner is None or owner.type != "class_declaration":
            continue
        owner_name = owner.child_by_field_name("name")
        parameters = method.child_by_field_name("parameters")
        if owner_name is None or parameters is None:
            continue
        for parameter in ctor_param_csharp._parameters(parameters):
            yield _text(owner_name), method, parameter


def _key(owner: str, method: Node, parameter: Node) -> tuple[str, str, str, str] | None:
    name = method.child_by_field_name("name")
    param = parameter.child_by_field_name("name")
    type_name = _simple(parameter.child_by_field_name("type"))
    if name is None or param is None or type_name is None:
        return None
    return owner, _text(name), type_name, _text(param)


def unread_parameters(original: str, content: str, types: frozenset[str]) -> list[str]:
    """The labels (``Owner.M(IGate gate)``) of the static methods' parameters
    typed as one of ``types`` that ``original`` read and ``content`` does
    not."""
    if not types or original == content:
        return []
    before: dict[tuple[str, str, str, str], int] = {}
    for owner, method, parameter in _methods(_parse(original)):
        key = _key(owner, method, parameter)
        if key is not None and key[2] in types:
            before[key] = before.get(key, 0) + _reads(method, key[3])
    found = []
    for owner, method, parameter in _methods(_parse(content)):
        key = _key(owner, method, parameter)
        if key is None or key[2] not in types or not before.get(key):
            continue
        if _reads(method, key[3]) == 0:
            found.append(f"{key[0]}.{key[1]}({key[2]} {key[3]})")
    return list(dict.fromkeys(found))


def _static_top_level(owner: Node) -> bool:
    """Whether ``owner`` is a ``static`` class, not ``partial`` or generic,
    declared at the top level of its file or namespace."""
    modifiers = ctor_param_csharp._modifiers(owner)
    if "static" not in modifiers or "partial" in modifiers:
        return False
    if any(c.type == "type_parameter_list" for c in owner.children):
        return False
    parent = owner.parent
    while parent is not None and parent.type == "declaration_list":
        parent = parent.parent
    return parent is not None and parent.type in (
        "compilation_unit", "namespace_declaration", "file_scoped_namespace_declaration"
    )


def _doc_names(method: Node, param: str) -> bool:
    """Whether a comment directly above the method names the parameter."""
    wanted = re.compile(rf"""name\s*=\s*["']{re.escape(param)}["']""")
    sibling = method.prev_named_sibling
    while sibling is not None and sibling.type == "comment":
        if wanted.search(_text(sibling)):
            return True
        sibling = sibling.prev_named_sibling
    return False


def _typeof_spelled(repo_dir: str, owner: str) -> bool:
    from flag_cleanup import source_index

    pattern = re.compile(rf"\btypeof\s*\(\s*(?:global::)?[\w.]*\b{re.escape(owner)}\s*\)")
    for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
        try:
            text = f.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        if owner in text and pattern.search(text):
            return True
    return False


def plan(path: str, content: str, label: str, repo_dir: str) -> Removal | Refusal:
    """The parameter ``label`` names in ``path`` and every argument passed
    for it, or why not; see the module docstring."""
    from flag_cleanup import wrapper_csharp

    matched = _LABEL.fullmatch(label)
    if matched is None:
        return Refusal(label, "not a method parameter", quiet=True)
    owner_name, method_name, type_name, param_name = matched.groups()
    root = _parse(content)
    data = content.removeprefix("﻿").encode("utf-8")
    owners = [
        n for n in _walk(root)
        if n.type == "class_declaration" and (c := n.child_by_field_name("name")) is not None and _text(c) == owner_name
    ]
    if len(owners) != 1:
        return Refusal(label, f"{len(owners)} classes named {owner_name} here")
    owner = owners[0]
    if not _static_top_level(owner):
        return Refusal(label, f"{owner_name} is not a top-level static class that is neither partial nor generic")
    body = owner.child_by_field_name("body")
    members = [
        m for m in (body.named_children if body is not None else ())
        if (n := m.child_by_field_name("name")) is not None and _text(n) == method_name
    ]
    if len(members) != 1 or members[0].type != "method_declaration":
        return Refusal(label, f"{method_name} is not the only member of its name")
    method = members[0]
    modifiers = ctor_param_csharp._modifiers(method)
    if "static" not in modifiers or modifiers & _OPAQUE:
        return Refusal(label, f"{method_name} is not a plain static method")
    if any(c.type in ("attribute_list", "type_parameter_list") for c in method.children):
        return Refusal(label, f"{method_name} has an attribute or type parameters")
    parameter_list = method.child_by_field_name("parameters")
    parameters = ctor_param_csharp._parameters(parameter_list) if parameter_list is not None else []
    names = ctor_param_csharp._names(parameter_list) if parameter_list is not None else ()
    if any(c.type != "parameter" for c in (parameter_list.named_children if parameter_list is not None else ()) if c.type != "comment"):
        return Refusal(label, "a params array")
    index = next((i for i, n in enumerate(names) if n == param_name), None)
    if index is None:
        return Refusal(label, "the parameter is gone")
    parameter = parameters[index]
    extra = [
        c for c in parameter.named_children
        if c != parameter.child_by_field_name("type") and c != parameter.child_by_field_name("name")
    ]
    if extra or any(c.type == "modifier" for c in parameter.children):
        return Refusal(label, "the parameter has an attribute, a modifier or a default value")
    if _simple(parameter.child_by_field_name("type")) != type_name:
        return Refusal(label, "the parameter's type changed")
    if _reads(method, param_name):
        return Refusal(label, "the parameter is still read")
    if _doc_names(method, param_name):
        return Refusal(label, f"a doc comment names {param_name} (CS1572/CS1734 once it goes)")
    if "public" in modifiers and wrapper_csharp.published(path, repo_dir):
        return Refusal(label, "a public method of a project that may be packed")
    if not cs_shared_pins._bound_to_owner(repo_dir, owner_name, {}):
        return Refusal(label, f"{owner_name} may be reached by another name (alias, using static, string or Razor)")
    if _typeof_spelled(repo_dir, owner_name):
        return Refusal(label, f"typeof({owner_name}) is taken (reflection may call {method_name})")
    resolved = str(Path(path).resolve())
    calls = cs_shared_pins._calls(
        repo_dir, owner_name, method_name, resolved, (owner.start_byte, owner.end_byte), {}, static=True
    )
    if calls is None:
        return Refusal(label, f"a mention of {method_name} is not a call")
    sites: list[Site] = []
    for call_path, nodes in calls.items():
        raw = Path(call_path).read_bytes()
        text = wrapper_csharp._read(call_path)
        shift = len(raw) - len(text.encode("utf-8")) if text is not None else 0
        for call in nodes:
            arguments = call.child_by_field_name("arguments")
            bound = ctor_param_csharp._bound(ctor_param_csharp._items(arguments), index, names) if arguments is not None else None
            if bound is None:
                return Refusal(label, f"{call_path}:{call.start_point[0] + 1}: a call passing no argument for it")
            if isinstance(bound, str):
                return Refusal(label, f"{call_path}:{call.start_point[0] + 1}: {bound}")
            value = ctor_param_csharp._expression(bound)
            if value is None or not ctor_param_csharp._inert(value):
                return Refusal(label, f"{call_path}:{call.start_point[0] + 1}: an argument that may do work")
            sites.append(Site(call_path, value.start_byte + shift, value.end_byte + shift))
    offset = len(content.encode("utf-8")) - len(data)
    sites.append(Site(path, parameter.start_byte + offset, parameter.end_byte + offset, "parameter"))
    return Removal(path, owner_name, None, label, index, tuple(sites), False)


# --- a private method's parameter only registered -----------------------------

#: A registration that infers its service type from what it is handed.
_INFERRING = frozenset({"AddScoped", "AddSingleton", "AddTransient", "TryAddScoped", "TryAddSingleton", "TryAddTransient"})
_ACCESS = frozenset({"public", "protected", "internal", "private"})


def _registration_of(node: Node, param: str, body: Node) -> Node | None:
    """The statement ``services.AddScoped(_ => p);`` (or another of
    :data:`_INFERRING`; ``AddSingleton(p)`` too) when the read ``node`` of
    ``p`` is its whole service: an expression statement directly in the
    method's ``body`` block, a plain receiver, no type arguments, and one
    argument that is ``p`` or a one-parameter lambda returning it."""
    value = node
    lambda_ = node.parent
    if lambda_ is not None and lambda_.type == "lambda_expression" and lambda_.child_by_field_name("body") == node:
        parameters = lambda_.child_by_field_name("parameters")
        if parameters is None or any(c.type == "modifier" for c in lambda_.children):
            return None
        if parameters.type == "implicit_parameter":
            bound = _text(parameters)
        elif parameters.type == "parameter_list":
            listed = [c for c in parameters.named_children if c.type != "comment"]
            if len(listed) != 1 or listed[0].type != "parameter" or listed[0].child_by_field_name("type") is not None:
                return None
            name_node = listed[0].child_by_field_name("name")
            bound = _text(name_node) if name_node is not None else None
        else:
            return None
        if bound is None or bound.removeprefix("@") == param:
            return None
        value = lambda_
    argument = value.parent
    if argument is None or argument.type != "argument" or len(argument.named_children) != 1:
        return None
    arguments = argument.parent
    call = arguments.parent if arguments is not None else None
    if call is None or call.type != "invocation_expression" or len(ctor_param_csharp._items(arguments)) != 1:
        return None
    function = call.child_by_field_name("function")
    if function is None or function.type != "member_access_expression":
        return None
    receiver = function.child_by_field_name("expression")
    name = function.child_by_field_name("name")
    if receiver is None or receiver.type != "identifier" or name is None or name.type != "identifier":
        return None
    if _text(name) not in _INFERRING or (value is node and _text(name) not in ("AddSingleton", "TryAddSingleton")):
        return None
    statement = call.parent
    if statement is None or statement.type != "expression_statement" or statement.parent != body:
        return None
    return statement


def _private_calls(root: Node, owner: Node, name: str) -> list[Node] | None:
    """Every call of the owner's private method ``name``: a mention outside
    the owner cannot reach it, and one inside must be ``M(…)``,
    ``this.M(…)`` or ``Owner.M(…)`` directly in the owner (a nested type may
    declare its own ``M``; any other receiver may be a derived type's). A
    method group, ``nameof`` or a conditional access refuses."""
    span = (owner.start_byte, owner.end_byte)
    owner_name = _text(owner.child_by_field_name("name"))
    calls: list[Node] = []
    for node in _walk(root):
        if node.type != "identifier" or _text(node).removeprefix("@") != name:
            continue
        if not span[0] <= node.start_byte < span[1]:
            continue
        parent = node.parent
        if parent is None:
            return None
        if parent.type == "method_declaration" and parent.child_by_field_name("name") == node:
            continue
        function = node
        if parent.type == "member_access_expression" and parent.child_by_field_name("name") == node:
            receiver = parent.child_by_field_name("expression")
            if receiver is None or not (receiver.type in ("this", "this_expression") or (receiver.type == "identifier" and _text(receiver) == owner_name)):
                return None
            function = parent
        call = function.parent
        if call is None or call.type != "invocation_expression" or call.child_by_field_name("function") != function:
            return None
        if not cs_shared_pins._directly_in(node, span):
            return None
        calls.append(call)
    return calls


def _string_spells(repo_dir: str, word: str) -> bool:
    """Whether a C# string of the checkout, or a file that cannot be read or
    parsed, may spell ``word`` (reflection may then find the method by
    name)."""
    from flag_cleanup import source_index
    from flag_cleanup import wrapper_csharp as wc

    pattern = re.compile(rf"(?<![\w@]){re.escape(word)}(?![\w])")
    for f in source_index.files(repo_dir, {".cs", ".cshtml", ".razor"}, with_ignored=True):
        text = wc._read(f.resolved)
        if text is None:
            return True
        if pattern.search(text) is None:
            continue
        if not f.path.name.endswith(".cs"):
            return True
        root = wc.parse(text)
        if root.has_error:
            return True
        if any(
            n.type in ("string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression")
            and pattern.search(_text(n))
            for n in _walk(root)
        ):
            return True
    return False


def plan_private(
    path: str, content: str, owner_name: str, method_name: str, param_name: str, repo_dir: str, types: frozenset[str]
) -> tuple[Removal, list[tuple[int, int]]] | Refusal:
    """A private method's parameter of one of ``types`` whose only reads are
    registrations that infer their service type from it (``services.AddScoped(_
    => gate);``), with every call's argument and those registrations; or
    why not. The registrations register a type this run deletes, so they go
    with it, and the parameter is then unread.

    * The method is a direct member of a class or struct that is not
      ``partial``, is ``private`` (written or by default), not ``extern``,
      ``abstract``, ``virtual``, ``override`` or ``new``, has no attribute or
      type parameter, and is the only member of its name there. Nothing
      outside the owner's text can call it, and inside it every call is
      found by :func:`_private_calls`.
    * The parameter has no attribute, modifier or default, its type is one
      of ``types``, and the doc comment does not name it.
    * The owner is reached by no other name (``cs_shared_pins._bound_to_owner``),
      no ``typeof`` of it is taken, and no string of the checkout spells the
      method's name: reflection could otherwise call it with the old
      arguments.
    * Each call passes an inert argument for it.

    Returns the removal and the byte spans of the registration statements
    in the file, which the caller deletes."""
    label = f"{owner_name}.{method_name}"
    root = _parse(content)
    data = content.removeprefix("﻿").encode("utf-8")
    offset = len(content.encode("utf-8")) - len(data)
    owners = [
        n for n in _walk(root)
        if n.type in ("class_declaration", "struct_declaration")
        and (c := n.child_by_field_name("name")) is not None and _text(c) == owner_name
    ]
    if len(owners) != 1:
        return Refusal(label, f"{len(owners)} types named {owner_name} here")
    owner = owners[0]
    body = owner.child_by_field_name("body")
    members = [
        m for m in (body.named_children if body is not None else ())
        if (n := m.child_by_field_name("name")) is not None and _text(n) == method_name
    ]
    if len(members) != 1 or members[0].type != "method_declaration":
        return Refusal(label, f"{method_name} is not the only member of its name")
    method = members[0]
    modifiers = ctor_param_csharp._modifiers(method)
    if (modifiers & _ACCESS) - {"private"}:
        # Not this pass's case: the parameter is reported as any other use.
        return Refusal(label, f"{method_name} is not private", quiet=True)
    if modifiers & _OPAQUE:
        return Refusal(label, f"{method_name} is not a plain private method")
    if "partial" in ctor_param_csharp._modifiers(owner):
        return Refusal(label, f"{owner_name} is partial: another part may call {method_name}")
    if any(c.type in ("attribute_list", "type_parameter_list") for c in method.children):
        return Refusal(label, f"{method_name} has an attribute or type parameters")
    parameter_list = method.child_by_field_name("parameters")
    if parameter_list is None or any(c.type not in ("parameter", "comment") for c in parameter_list.named_children):
        return Refusal(label, "a params array")
    parameters = ctor_param_csharp._parameters(parameter_list)
    names = ctor_param_csharp._names(parameter_list)
    index = next((i for i, n in enumerate(names) if n == param_name), None)
    if index is None:
        return Refusal(label, "the parameter is gone")
    parameter = parameters[index]
    label = f"{owner_name}.{method_name}({_text(parameter.child_by_field_name('type'))} {param_name})"
    extra = [
        c for c in parameter.named_children
        if c != parameter.child_by_field_name("type") and c != parameter.child_by_field_name("name")
    ]
    if extra or any(c.type == "modifier" for c in parameter.children):
        return Refusal(label, "the parameter has an attribute, a modifier or a default value")
    if _simple(parameter.child_by_field_name("type")) not in types:
        return Refusal(label, "the parameter's type is not the family's")
    block = method.child_by_field_name("body")
    if block is None or block.type != "block":
        return Refusal(label, f"{method_name} has no block body")
    registrations: list[Node] = []
    for n in _walk(block):
        if n.type != "identifier" or _text(n).removeprefix("@") != param_name:
            continue
        parent = n.parent
        if parent is not None and parent.type == "member_access_expression" and parent.child_by_field_name("name") == n:
            continue
        statement = _registration_of(n, param_name, block)
        if statement is None:
            return Refusal(label, f"line {n.start_point[0] + 1}: a read of {param_name} that is not a registration of it")
        registrations.append(statement)
    if not registrations:
        return Refusal(label, "the parameter is not read", quiet=True)
    if _doc_names(method, param_name):
        return Refusal(label, f"a doc comment names {param_name} (CS1572/CS1734 once it goes)")
    if not cs_shared_pins._bound_to_owner(repo_dir, owner_name, {}):
        return Refusal(label, f"{owner_name} may be reached by another name (alias, using static, string or Razor)")
    if _typeof_spelled(repo_dir, owner_name):
        return Refusal(label, f"typeof({owner_name}) is taken (reflection may call {method_name})")
    if _string_spells(repo_dir, method_name):
        return Refusal(label, f"a string spells {method_name} (reflection may call it)")
    calls = _private_calls(root, owner, method_name)
    if calls is None:
        return Refusal(label, f"a mention of {method_name} is not a call this can follow")
    sites: list[Site] = []
    for call in calls:
        arguments = call.child_by_field_name("arguments")
        bound = ctor_param_csharp._bound(ctor_param_csharp._items(arguments), index, names) if arguments is not None else None
        if bound is None:
            return Refusal(label, f"line {call.start_point[0] + 1}: a call passing no argument for it")
        if isinstance(bound, str):
            return Refusal(label, f"line {call.start_point[0] + 1}: {bound}")
        value = ctor_param_csharp._expression(bound)
        if value is None or not ctor_param_csharp._inert(value):
            return Refusal(label, f"line {call.start_point[0] + 1}: an argument that may do work")
        sites.append(Site(path, value.start_byte + offset, value.end_byte + offset))
    sites.append(Site(path, parameter.start_byte + offset, parameter.end_byte + offset, "parameter"))
    spans = [(r.start_byte + offset, r.end_byte + offset) for r in registrations]
    return Removal(path, owner_name, None, label, index, tuple(sites), False), spans
