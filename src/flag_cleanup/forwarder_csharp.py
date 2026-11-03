"""Inline a C# static method this run reduced to forwarding one call.

A helper that made a decision about a gate is left, once the read is folded
and the gate parameter removed, doing nothing but calling a method on one of
its parameters::

    /// <summary>Protected once the gate is on, raw until then.</summary>
    public static string ForStorage(string secret, IProtector protector) =>
        protector.Protect(secret);

Every caller then reads ``SecretFormat.ForStorage(secret, _protector)``
where ``_protector.Protect(secret)`` would do, and the doc comment describes
the decision the run removed. When every call is proven, each becomes the
call the method forwarded and the method goes with its doc comment. When
not, the method is reported, so a reviewer gets a pointer.

**Only what this run made.** A method is a candidate only when the run's
text holds it as a forwarder and the original text did not
(:func:`forwarders`): a thin named helper someone wrote on purpose is never
touched, and never reported.

**What a forwarder is** (:func:`_shape`): a ``static`` method whose body is
one call ``p.M(a, b, …)``, as an expression body, a lone ``return``, or a
lone statement of a ``void`` method, where ``p`` is one of its parameters,
``M`` a plain name, and the arguments are its other parameters, each exactly
once, positionally and with no modifier.

**The proof** (:func:`plan`), each step a reason to report instead:

* The method is the only member of its name in a top-level ``static`` class
  that is neither ``partial`` nor generic; it is not ``extern``, ``partial``,
  ``abstract``, ``virtual``, ``override``, ``new``, ``async`` or ``unsafe``,
  has no attribute and no type parameter, and no parameter has a modifier
  (``this``, ``ref``, ``in``, ``out``, ``params``, ``scoped``), an attribute
  or a default value. So nothing derives from it or extends a value through
  it, and every call binds to it.
* Who can call it: a ``public`` method of a project that may be packed
  refuses; any other non-private one refuses when the project grants
  ``InternalsVisibleTo`` to an assembly the checkout does not build.
* Reflection and other spellings: the class is reached by its name only
  (one type of the name, no ``using static`` or alias, no string, Razor or
  project file spelling it), no ``typeof`` of it is taken, no C# string
  spells the method's name, and no ``cref`` names it (a dangling one is a
  warning the documentation build turns into an error).
* The callee: the receiver parameter's type is a class or interface the
  checkout declares once, not ``partial`` or generic and with no base list,
  so its members are its own. It declares exactly one member named ``M``,
  a ``public`` instance method with no attribute or type parameter whose
  parameters carry no modifier or default, whose parameter types are written
  as the forwarder's parameters it is passed, and whose return type is
  written as the forwarder's. So ``value.M(…)`` binds to the same method with
  the same conversions, and the call's type does not change.
* No file holding a call uses ``dynamic`` (a call bound at run time), or
  fails to parse.
* The same nullable context everywhere: the receiver's type is declared in
  the forwarder's project, and no ``#nullable`` directive sits in the
  forwarder's file, that type's file or a calling file. Otherwise the
  forwarded call could warn (CS86xx) where the old one did not.
* The owner's simple name means only the owner wherever it is spelled
  (:func:`flag_cleanup.cs_shared_pins._named_only_as_owner`): a member or
  local named like it would take ``Owner.M(…)``, and ``x.Owner.M(…)`` is
  another value's method.
* Every mention of the method is a call: ``Owner.M(…)`` anywhere, or a bare
  ``M(…)`` directly in the class (:func:`flag_cleanup.cs_shared_pins._calls`).
  A method group or ``nameof`` refuses. At least one call exists.
* Each call is outside every lambda, anonymous method and query (an
  expression tree records the call it holds), passes one positional
  argument per parameter with no modifier and no comment in its list, and
  each argument is a literal, a name or a chain of member reads (property
  getters are assumed to do nothing, as elsewhere). The forwarded form
  evaluates the receiver first, so these reads may change order; nothing
  that can do work does.
* The receiver argument's static type is known, and is the receiver
  parameter's type itself: a value of a class implementing an interface
  could bind ``M`` to another member.

**Placement: split.** This module proves (repository facts, R3) and decides
what to report (R1); the engine makes both edits through marks (R4): each
proven call's callee is replaced by a marked name and rewritten by a rule
rendered per method (the ``forward`` block of ``csharp_ctor_param.toml``),
and the method, with its doc comment, is marked as a member and deleted by
``delete_marked_member``. A postcondition re-reads every file; on any
failure every byte goes back and the method is reported.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cs_shared_pins, ctor_param_csharp, git_ops, method_param_csharp, syntax
from flag_cleanup.ctor_param_model import IDENT, blocks, choose_sentinel, forms_for, mark, tidy_lines
from flag_cleanup.ctor_param_model import render_rules as _marked_rules

logger = logging.getLogger(__name__)

#: ``bindings`` kinds: a method inlined at every call and deleted, and one
#: kept for the reason given.
INLINED = "inlined forwarder"
KEPT = "kept forwarder"

_OPAQUE = frozenset({"extern", "partial", "abstract", "virtual", "override", "new", "async", "unsafe"})
#: What may sit around a call that an expression tree would record.
_TREES = frozenset({"lambda_expression", "anonymous_method_expression", "query_expression"})
#: Modifiers a callee may carry: an interface member's, or a class's public instance method's.
_CALLEE_MODIFIERS = frozenset({"public", "virtual", "abstract", "async"})
_LITERALS = frozenset(
    {
        "string_literal", "verbatim_string_literal", "raw_string_literal", "character_literal",
        "integer_literal", "real_literal", "boolean_literal", "null_literal",
    }
)
#: The two spellings of ``InternalsVisibleTo`` whose friend can be read: the
#: attribute with one plain string literal, and the self-closing project item.
#: Any other spelling (a named argument, a constant, ``<AssemblyAttribute>``
#: with ``_Parameter1``) names a friend this cannot read.
_IVT = re.compile(
    r"""InternalsVisibleTo(?:Attribute)?\s*\(\s*@?"([^"]*)"\s*\)"""
    r"""|<InternalsVisibleTo\s+Include\s*=\s*"([^"]*)"\s*/>""",
)
_IVT_WORD = re.compile(r"InternalsVisibleTo")
#: A directive that sets a nullable context for part of a file.
#: A directive that sets a nullable context for part of a file (after a byte
#: order mark on the first line, its usual place).
_NULLABLE = re.compile(rb"^(?:\xef\xbb\xbf)?[ \t]*#[ \t]*nullable\b", re.MULTILINE)
_ASSEMBLY_NAME = re.compile(r"<AssemblyName>\s*([^<]*?)\s*</AssemblyName>")
_BOM = "\ufeff"


@dataclass(frozen=True)
class Shape:
    """A forwarding body: the call is made on parameter ``receiver``, to
    ``member``, passing parameter ``order[i]`` as its ``i``-th argument."""

    receiver: int
    member: str
    order: tuple[int, ...]
    #: The call as written, on one line: ``protector.Protect(secret)``.
    call: str


@dataclass(frozen=True)
class Plan:
    path: str
    owner: str
    name: str
    shape: Shape
    #: The method with its doc comment, in the declaring file's bytes.
    member: tuple[int, int]
    #: ``(path, callee start, callee end, call start, call end, forwarded text)`` per call.
    calls: tuple[tuple[str, int, int, int, int, str], ...]
    arity: int


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _parse(source: str) -> Node:
    return syntax._parse(source.removeprefix(_BOM), "csharp")


def _body_call(method: Node) -> Node | None:
    """The one call ``method``'s body consists of, or ``None``."""
    body = method.child_by_field_name("body") or next(
        (c for c in method.children if c.type == "arrow_expression_clause"), None
    )
    if body is None:
        return None
    if body.type == "arrow_expression_clause":
        parts = body.named_children
        return parts[0] if len(parts) == 1 and parts[0].type == "invocation_expression" else None
    if body.type != "block" or len(body.named_children) != 1:
        return None
    statement = body.named_children[0]
    if statement.type == "return_statement":
        parts = statement.named_children
        return parts[0] if len(parts) == 1 and parts[0].type == "invocation_expression" else None
    returns = method.child_by_field_name("returns")
    if statement.type == "expression_statement" and returns is not None and _text(returns) == "void":
        parts = statement.named_children
        return parts[0] if len(parts) == 1 and parts[0].type == "invocation_expression" else None
    return None


def _shape(method: Node) -> Shape | None:
    """The forwarding call ``method`` is, or ``None``; see the module docstring."""
    if method.type != "method_declaration" or "static" not in ctor_param_csharp._modifiers(method):
        return None
    listed = method.child_by_field_name("parameters")
    if listed is None or any(c.type != "parameter" for c in listed.named_children):
        return None
    names = [_text(p.child_by_field_name("name")).removeprefix("@") for p in listed.named_children if p.child_by_field_name("name") is not None]
    if len(names) != len(listed.named_children):
        return None
    call = _body_call(method)
    function = call.child_by_field_name("function") if call is not None else None
    if function is None or function.type != "member_access_expression":
        return None
    receiver = function.child_by_field_name("expression")
    member = function.child_by_field_name("name")
    if receiver is None or member is None or receiver.type != "identifier" or member.type != "identifier":
        return None
    if _text(function).replace(" ", "") != f"{_text(receiver)}.{_text(member)}":
        return None  # `p?.M`, a comment inside, or another operator
    spelled = _text(receiver).removeprefix("@")
    if spelled not in names:
        return None
    index = names.index(spelled)
    arguments = call.child_by_field_name("arguments")
    if arguments is None or any(c.type != "argument" for c in arguments.named_children):
        return None
    order: list[int] = []
    for argument in arguments.named_children:
        if argument.child_by_field_name("name") is not None:
            return None
        value = ctor_param_csharp._expression(argument)
        if value is None or value.type != "identifier":
            return None
        name = _text(value).removeprefix("@")
        if name not in names or names.index(name) == index:
            return None
        order.append(names.index(name))
    if sorted(order) != [i for i in range(len(names)) if i != index]:
        return None
    return Shape(index, _text(member).removeprefix("@"), tuple(order), _flat(_text(call)))


def _static_methods(root: Node):
    """``(owner name, method)`` for every static method of a class."""
    for method in _walk(root):
        if method.type != "method_declaration":
            continue
        owner = method.parent.parent if method.parent is not None else None
        name = method.child_by_field_name("name")
        if owner is None or owner.type != "class_declaration" or name is None:
            continue
        owner_name = owner.child_by_field_name("name")
        if owner_name is not None:
            yield _text(owner_name), method


def forwarders(original: str, content: str) -> list[tuple[str, str]]:
    """``(owner, method)`` of each static method ``content`` holds as a
    forwarder that ``original`` held, once and by the same name, as
    something else: the forwarders this run made."""
    if original == content:
        return []
    before: dict[tuple[str, str], list[Node]] = {}
    for owner, method in _static_methods(_parse(original)):
        before.setdefault((owner, _text(method.child_by_field_name("name"))), []).append(method)
    found: list[tuple[str, str]] = []
    for owner, method in _static_methods(_parse(content)):
        key = (owner, _text(method.child_by_field_name("name")))
        if _shape(method) is None:
            continue
        olds = before.get(key, [])
        if len(olds) == 1 and _shape(olds[0]) is None:
            found.append(key)
    return list(dict.fromkeys(found))


# --- the proof ------------------------------------------------------------------


def _assembly_names(repo_dir: str) -> frozenset[str] | None:
    """The assembly name of every project in the checkout, or ``None`` when
    a project file cannot be read."""
    from flag_cleanup import source_index

    names: set[str] = set()
    for f in source_index.files(repo_dir, {".csproj"}, with_ignored=True):
        if not f.path.name.endswith(".csproj"):
            continue
        try:
            text = f.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        names.add(f.path.name.removesuffix(".csproj"))
        names.update(m.group(1) for m in _ASSEMBLY_NAME.finditer(text))
    return frozenset(names)


def _grants_outside(path: str, repo_dir: str) -> bool:
    """Whether the project holding ``path`` may let an assembly the checkout
    does not build see its internals: an ``InternalsVisibleTo`` in its C#
    files, its project file or any MSBuild file of the checkout naming one,
    or spelled in any form whose friend cannot be read (see :data:`_IVT`)."""
    from flag_cleanup import cross_file_csharp as xcs
    from flag_cleanup import source_index

    project = xcs._project_of(path, xcs._project_dirs(repo_dir))
    known = _assembly_names(repo_dir)
    if project is None or known is None:
        return True
    for f in source_index.files(repo_dir, {".cs", ".csproj", ".props", ".targets"}, with_ignored=True):
        name = f.path.name
        if name.endswith(".cs") and project not in Path(f.resolved).parents:
            continue
        if name.endswith(".csproj") and Path(f.resolved).parent != project:
            continue
        try:
            text = f.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        if "InternalsVisibleTo" not in text:
            continue
        # Every spelling must be one of the two readable forms, naming a
        # project of the checkout; anything else (a comment included) is
        # taken to grant an outside friend.
        readable = {m.start(): m for m in _IVT.finditer(text)}
        for word in _IVT_WORD.finditer(text):
            match = readable.get(word.start()) or readable.get(word.start() - 1)
            if match is None:
                return True
            granted = (match.group(1) or match.group(2) or "").split(",", 1)[0].strip()
            if not granted or "$(" in granted or granted not in known:
                return True
    return False


def _cref_names(repo_dir: str, owner: str, name: str) -> bool:
    """Whether a C# comment's ``cref`` may name the method."""
    from flag_cleanup import source_index
    from flag_cleanup import wrapper_csharp as wc

    pattern = re.compile(rf"""cref\s*=\s*["'][^"']*\b{re.escape(name)}\b""")
    for f in source_index.files(repo_dir, {".cs"}, with_ignored=True):
        text = wc._read(f.resolved)
        if text is None:
            return True
        if name in text and pattern.search(text):
            return True
    return False


def _spells_dynamic(root: Node) -> bool:
    """Whether a parsed C# file spells ``dynamic`` as a name or a type."""
    return b"dynamic" in root.text and any(
        n.type in ("identifier", "predefined_type") and _text(n) == "dynamic" for n in _walk(root)
    )


def _plain(node: Node) -> bool:
    """A literal, a name, ``this``, or a chain of member reads of one."""
    if node.type in _LITERALS or node.type in ("identifier", "this", "this_expression"):
        return True
    if node.type == "postfix_unary_expression" and _text(node).rstrip().endswith("!"):
        return bool(node.named_children) and _plain(node.named_children[0])
    if node.type == "member_access_expression":
        expression = node.child_by_field_name("expression")
        name = node.child_by_field_name("name")
        return (
            expression is not None
            and name is not None
            and name.type == "identifier"
            and (expression.type == "predefined_type" or _plain(expression))
        )
    return False


def _aligned(data: bytes, root: Node) -> bool:
    """Whether ``root`` was parsed from ``data`` as it is on disk, so that its
    offsets index ``data`` directly. Those offsets count a byte order mark,
    which ``root.text`` leaves out."""
    return data[root.start_byte : root.end_byte] == root.text and root.end_byte <= len(data)


def _same(root: Node, node: Node) -> Node | None:
    """The node of ``root``'s tree with ``node``'s type and span."""
    found = root.descendant_for_byte_range(node.start_byte, node.end_byte)
    while found is not None and not (
        found.type == node.type and found.start_byte == node.start_byte and found.end_byte == node.end_byte
    ):
        found = found.parent
    return found


def _parameter_types(method: Node) -> list[str]:
    listed = method.child_by_field_name("parameters")
    out = []
    for parameter in listed.named_children if listed is not None else ():
        type_node = parameter.child_by_field_name("type") if parameter.type == "parameter" else None
        out.append(_flat(_text(type_node)) if type_node is not None else "")
    return out


def _plain_parameters(method: Node) -> bool:
    """No ``params`` array, and no parameter with a modifier, an attribute
    or a default value."""
    listed = method.child_by_field_name("parameters")
    for parameter in listed.named_children if listed is not None else ():
        if parameter.type == "comment":
            continue
        if parameter.type != "parameter":
            return False
        kept = (parameter.child_by_field_name("type"), parameter.child_by_field_name("name"))
        if any(c not in kept for c in parameter.named_children) or any(c.type == "modifier" for c in parameter.children):
            return False
    return True


def _callee(receiver_type: Node, readers, path: str, shape: Shape, method: Node) -> tuple[Node, str] | str:
    """The declaration of the receiver parameter's type, with its file, or
    why the forwarded call might bind differently."""
    from flag_cleanup import wrapper_csharp as wc

    reader = readers.get(path)
    if reader is None:
        return "its file cannot be read"
    found = wc._checkout_type(wc._Typed(receiver_type, reader), readers)
    if found is None:
        return f"{_flat(_text(receiver_type))} is not a type this repository declares once"
    declaration, declaring = found
    if declaration.type not in ("interface_declaration", "class_declaration"):
        return f"{_flat(_text(receiver_type))} is not a class or an interface"
    if wc._base_nodes(declaration) or any(c.type == "type_parameter_list" for c in declaration.children):
        return f"{_flat(_text(receiver_type))} has a base list or type parameters"
    if "partial" in ctor_param_csharp._modifiers(declaration):
        return f"{_flat(_text(receiver_type))} is partial"
    body = declaration.child_by_field_name("body")
    named = [
        m for m in (body.named_children if body is not None else ())
        if (n := m.child_by_field_name("name")) is not None and _text(n).removeprefix("@") == shape.member
    ]
    if len(named) != 1 or named[0].type != "method_declaration":
        return f"{shape.member} is overloaded or not a method"
    callee = named[0]
    modifiers = ctor_param_csharp._modifiers(callee)
    interface = declaration.type == "interface_declaration"
    if modifiers - _CALLEE_MODIFIERS or (not interface and "public" not in modifiers):
        return f"{shape.member} is not a public instance method"
    if any(c.type in ("attribute_list", "type_parameter_list") for c in callee.children):
        return f"{shape.member} has an attribute or type parameters"
    if not _plain_parameters(callee):
        return f"{shape.member} has a parameter with a modifier or a default value"
    theirs = _parameter_types(callee)
    ours = _parameter_types(method)
    if len(theirs) != len(shape.order) or any(theirs[i] != ours[p] for i, p in enumerate(shape.order)):
        return f"{shape.member}'s parameter types differ from the ones passed to it"
    returns = callee.child_by_field_name("returns")
    own = method.child_by_field_name("returns")
    if returns is None or own is None or _flat(_text(returns)) != _flat(_text(own)):
        return f"{shape.member} returns another type"
    return declaration, declaring


def plan(path: str, content: str, owner_name: str, method_name: str, repo_dir: str) -> Plan | str:
    """Every call of the forwarder ``owner_name.method_name`` in ``path`` and
    what each becomes, or why not; see the module docstring."""
    from flag_cleanup import wrapper_csharp as wc

    resolved = str(Path(path).resolve())
    readers = wc._Readers(repo_dir)
    reader = readers.get(resolved)
    if reader is None:
        return "its file cannot be read"
    root = reader.root
    owners = [
        n for n in _walk(root)
        if n.type == "class_declaration" and (c := n.child_by_field_name("name")) is not None and _text(c) == owner_name
    ]
    if len(owners) != 1 or not method_param_csharp._static_top_level(owners[0]):
        return f"{owner_name} is not one top-level static class that is neither partial nor generic"
    owner = owners[0]
    body = owner.child_by_field_name("body")
    members = [
        m for m in (body.named_children if body is not None else ())
        if (n := m.child_by_field_name("name")) is not None and _text(n).removeprefix("@") == method_name
    ]
    if len(members) != 1 or members[0].type != "method_declaration":
        return f"another member of {owner_name} is named {method_name}"
    method = members[0]
    shape = _shape(method)
    if shape is None:
        return "it no longer only forwards"
    if IDENT.fullmatch(shape.member) is None:
        return f"{shape.member} cannot be spelled in a rule"
    modifiers = ctor_param_csharp._modifiers(method)
    if modifiers & _OPAQUE:
        return f"{method_name} is not a plain static method"
    if any(c.type in ("attribute_list", "type_parameter_list") for c in method.children):
        return f"{method_name} has an attribute or type parameters"
    if not _plain_parameters(method):
        return "a parameter has a modifier, an attribute or a default value"
    if "public" in modifiers and wc.published(resolved, repo_dir):
        return "a public method of a project that may be packed: code outside the repository may call it"
    if modifiers & {"public", "internal", "protected"} and "public" not in modifiers and _grants_outside(resolved, repo_dir):
        return "its project lets an assembly outside the repository see its internals"
    if not cs_shared_pins._bound_to_owner(repo_dir, owner_name, {}):
        return f"{owner_name} may be reached by another name (alias, using static, string or Razor)"
    if method_param_csharp._typeof_spelled(repo_dir, owner_name):
        return f"typeof({owner_name}) is taken (reflection may call {method_name})"
    if method_param_csharp._string_spells(repo_dir, method_name):
        return f"a string spells {method_name} (reflection may call it)"
    if _cref_names(repo_dir, owner_name, method_name):
        return f"a doc comment's cref names {method_name}"
    listed = method.child_by_field_name("parameters")
    parameters = [p for p in listed.named_children if p.type == "parameter"]
    receiver_type = parameters[shape.receiver].child_by_field_name("type")
    callee = _callee(receiver_type, readers, resolved, shape, method)
    if isinstance(callee, str):
        return callee
    declaration, declaring = callee
    from flag_cleanup import cross_file_csharp as xcs

    projects = xcs._project_dirs(repo_dir)
    if xcs._project_of(declaring, projects) != xcs._project_of(resolved, projects):
        return f"{_flat(_text(receiver_type))} is declared in another project, whose nullable context may differ"
    if any(_NULLABLE.search(Path(p).read_bytes()) for p in {resolved, declaring}):
        return "a #nullable directive may give the call another nullable context"
    if not cs_shared_pins._named_only_as_owner(repo_dir, owner_name, resolved, {}):
        return f"{owner_name} also names another value or member (x.{owner_name}, a member named {owner_name})"
    calls = cs_shared_pins._calls(
        repo_dir, owner_name, method_name, resolved, (owner.start_byte, owner.end_byte), {}, static=True
    )
    if calls is None:
        return f"a mention of {method_name} is not a call (a method group, nameof, or the like)"
    if not any(calls.values()):
        return "nothing in the repository calls it"
    sites: list[tuple[str, int, int, int, int, str]] = []
    for call_path, nodes in sorted(calls.items()):
        call_reader = readers.get(call_path)
        if call_reader is None:
            return f"{call_path}: cannot be read"
        if not _aligned(Path(call_path).read_bytes(), call_reader.root):
            return f"{call_path}: its text changed while it was read"
        if call_reader.root.has_error or _spells_dynamic(call_reader.root):
            return f"{call_path}: does not parse, or uses dynamic (a call bound at run time)"
        if _NULLABLE.search(call_reader.root.text):
            return f"{call_path}: a #nullable directive may give the call another nullable context"
        for found in nodes:
            where = f"{call_path}:{found.start_point[0] + 1}"
            call = _same(call_reader.root, found)
            if call is None:
                return f"{where}: a call this cannot find again"
            ancestor = call.parent
            while ancestor is not None and ancestor.type not in _TREES:
                ancestor = ancestor.parent
            if ancestor is not None:
                return f"{where}: a call inside a lambda or query (an expression tree records it)"
            arguments = call.child_by_field_name("arguments")
            items = list(arguments.named_children) if arguments is not None else []
            if any(c.type != "argument" for c in items) or len(items) != len(parameters):
                return f"{where}: a call with a comment or another number of arguments"
            values: list[Node] = []
            for item in items:
                value = ctor_param_csharp._expression(item)
                if item.child_by_field_name("name") is not None or value is None:
                    return f"{where}: a named argument, or one with a modifier"
                if not _plain(value):
                    return f"{where}: an argument that may do work ({_flat(_text(value))[:60]})"
                values.append(value)
            typed = wc._static_type(values[shape.receiver], readers, call_path)
            found_type = wc._checkout_type(typed, readers) if typed is not None and not typed.task else None
            if found_type is None or found_type[1] != declaring or found_type[0].start_byte != declaration.start_byte:
                return (
                    f"{where}: {_flat(_text(values[shape.receiver]))} is not known to be exactly a "
                    f"{_flat(_text(receiver_type))} (a class implementing it may bind {shape.member} elsewhere)"
                )
            function = call.child_by_field_name("function")
            forwarded = f"{_flat(_text(values[shape.receiver]))}.{shape.member}(" + ", ".join(
                _flat(_text(values[i])) for i in shape.order
            ) + ")"
            sites.append((
                call_path, function.start_byte, function.end_byte, call.start_byte, call.end_byte, forwarded,
            ))
    data = Path(resolved).read_bytes()
    if not _aligned(data, root):
        return "its text changed while it was read"
    # A node's offsets count a byte order mark, which `root.text` leaves out:
    # every span here is taken against the file's own bytes.
    return Plan(
        resolved, owner_name, method_name, shape, ctor_param_csharp.declaration_span(method, data), tuple(sites), len(parameters)
    )


# --- the edit ---------------------------------------------------------------------


def render_rules(plan_: Plan, sentinel: str) -> str:
    """The rules that rewrite every marked call of ``plan_``'s method and
    delete the marked method: the C# template's ``argument`` blocks and its
    ``forward`` block, cloned for this method's arity and argument order."""
    if IDENT.fullmatch(sentinel) is None or IDENT.fullmatch(plan_.shape.member) is None:
        raise ValueError("cannot spell the sentinel or the member in a rule")
    captures = [f"@fwd_{i}_" for i in range(plan_.arity)]
    arguments = ". " + " . ".join(f"(argument) {c}" for c in captures) + " ."
    forwarded = f"{captures[plan_.shape.receiver]}.{plan_.shape.member}(" + ", ".join(
        captures[i] for i in plan_.shape.order
    ) + ")"
    out = [_marked_rules("csharp", [], sentinel)]
    for block in blocks("csharp", "forward"):
        out.append(
            block.replace("__FWD_ARGUMENTS__", arguments)
            .replace("__FWD_SENTINEL__", f"{sentinel}_fwd")
            .replace("__FWD_CALL__", forwarded)
            .strip()
            + "\n"
        )
    return "\n".join(out)


class _Failed(Exception):
    pass


def _declares(source: bytes, owner: str, name: str) -> bool:
    root = _parse(source.decode("utf-8"))
    for owner_name, method in _static_methods(root):
        if owner_name == owner and _text(method.child_by_field_name("name")).removeprefix("@") == name:
            return True
    return False


def apply(plan_: Plan, engine: Callable[[list[str], str], object]) -> list[str]:
    """Write ``plan_`` through the engine; returns the paths it changed. On
    any failure every byte goes back and :class:`_Failed` says why."""
    paths = sorted({plan_.path, *(site[0] for site in plan_.calls)})
    git_ops.before_writing(paths)
    before = {path: Path(path).read_bytes() for path in paths}
    sentinel = choose_sentinel(before.values())
    if sentinel is None:
        raise _Failed("no free sentinel name")
    forms = {**forms_for("csharp"), "forward": "{}_fwd"}
    spans: dict[str, list[tuple[int, int, str]]] = {plan_.path: [(*plan_.member, "member")]}
    for path, start, end, *_ in plan_.calls:
        spans.setdefault(path, []).append((start, end, "forward"))
    marked: dict[str, bytes] = {}
    failure: str | None = None
    try:
        for path in paths:
            marked[path] = mark(before[path], spans.get(path, []), sentinel, forms=forms)
            Path(path).write_bytes(marked[path])
        engine(paths, render_rules(plan_, sentinel))
        for path in paths:
            out = Path(path).read_bytes()
            if path == plan_.path:
                if b"\r" in out:
                    bom = out.startswith(b"\xef\xbb\xbf")
                    fixed = syntax.collapse_introduced_carriage_returns(
                        marked[path].decode("utf-8").removeprefix(_BOM),
                        out.decode("utf-8").removeprefix(_BOM),
                        "csharp",
                    ).encode("utf-8")
                    out = (b"\xef\xbb\xbf" if bom else b"") + fixed
                out = tidy_lines(marked[path], out)
                Path(path).write_bytes(out)
        failure = _postcondition(plan_, paths, before, sentinel)
    except Exception as exc:  # noqa: BLE001 - any surprise: put the bytes back
        failure = f"{type(exc).__name__}: {exc}"
    if failure is not None:
        for path, data in before.items():
            Path(path).write_bytes(data)
        raise _Failed(f"postcondition: {failure}")
    return [path for path in paths if Path(path).read_bytes() != before[path]]


def _postcondition(plan_: Plan, paths: list[str], before: dict[str, bytes], sentinel: str) -> str | None:
    """Every mark consumed, every file parsing, and every file holding
    exactly its old bytes with each call forwarded and the method (with its
    doc comment) cut out. A calling file must match byte for byte; the
    declaring file outside whitespace, since the engine takes the line break
    before the method and the blank line left is tidied."""
    needle = sentinel.encode("ascii")
    for path in paths:
        after = Path(path).read_bytes()
        if needle in after:
            return f"{path}: a mark was not consumed"
        if _parse(after.decode("utf-8")).has_error:
            return f"{path}: no longer parses"
        edits = [(start, end, forwarded.encode("utf-8")) for p, _, _, start, end, forwarded in plan_.calls if p == path]
        if path == plan_.path:
            edits.append((*plan_.member, b""))
        expected = before[path]
        for start, end, text in sorted(edits, reverse=True):
            expected = expected[:start] + text + expected[end:]
        if path != plan_.path:
            if after != expected:
                return f"{path}: the calls were not rewritten as proven"
            continue
        if after.startswith(b"\xef\xbb\xbf") != expected.startswith(b"\xef\xbb\xbf"):
            return f"{path}: its byte order mark changed"
        if _WHITESPACE.sub(b"", after) != _WHITESPACE.sub(b"", expected):
            return f"{path}: the method and its calls were not edited as proven"
        if _declares(after, plan_.owner, plan_.name):
            return f"{path}: {plan_.owner}.{plan_.name} is still declared"
    return None


_WHITESPACE = re.compile(rb"\s+")


def inline(
    summaries: list,
    repo_dir: str,
    rewrite: Callable,
    engine: Callable[[list[str], str], object],
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """Inline every forwarder this run made in a C# file of ``summaries``, or
    report it. Returns the summaries and the rows: ``(path, INLINED, text)``
    for each method inlined and deleted, ``(path, KEPT, text)`` for each kept,
    with its reason."""
    candidates: list[tuple[str, str, str]] = []
    for summary in summaries:
        if not summary.path.endswith(".cs") or summary.original_content == summary.content:
            continue
        for owner, name in forwarders(summary.original_content, summary.content):
            candidates.append((str(Path(summary.path).resolve()), owner, name))
    if not candidates:
        return summaries, ()
    rows: list[tuple[str, str, str]] = []
    summaries = list(summaries)
    for path, owner, name in candidates:
        content = Path(path).read_text(encoding="utf-8")
        relative = os.path.relpath(path, repo_dir)
        shape = next(
            (_shape(m) for o, m in _static_methods(_parse(content)) if o == owner and _text(m.child_by_field_name("name")) == name),
            None,
        )
        described = f"{owner}.{name}, which only calls {shape.call}" if shape is not None else f"{owner}.{name}"
        try:
            planned = plan(path, content, owner, name, repo_dir)
            if isinstance(planned, str):
                raise _Failed(planned)
            originals = {p: Path(p).read_bytes() for p in {planned.path, *(s[0] for s in planned.calls)}}
            changed = apply(planned, engine)
        except _Failed as kept:
            logger.info("keeping %s: %s", described, kept)
            rows.append((relative, KEPT, f"{described}: kept, because {_tidy(str(kept), repo_dir)}"))
            continue
        except Exception as exc:  # noqa: BLE001 - a forwarder never refuses the flag
            logger.warning("keeping %s: %s: %s", described, type(exc).__name__, exc)
            rows.append((relative, KEPT, f"{described}: kept, because it could not be judged"))
            continue
        logger.info("inlined %s at %d call(s)", described, len(planned.calls))
        count = len(planned.calls)
        rows.append((relative, INLINED, f"{described}: inlined at {count} call{'' if count == 1 else 's'}"))
        by_key = {str(Path(s.path).resolve()): i for i, s in enumerate(summaries)}
        for changed_path in changed:
            text = Path(changed_path).read_text(encoding="utf-8")
            at = by_key.get(changed_path)
            if at is None:
                summaries.append(rewrite(changed_path, originals[changed_path].decode("utf-8"), text))
            else:
                old = summaries[at]
                summaries[at] = rewrite(old.path, old.original_content, text)
    return summaries, tuple(rows)


def _tidy(reason: str, repo_dir: str) -> str:
    """A reason with every absolute path spelled from the repository."""
    root = str(Path(repo_dir).resolve()) + os.sep
    return reason.replace(root, "")
