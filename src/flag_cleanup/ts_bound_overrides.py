"""Delete an override entry whose value is a NAME provably bound to a boolean.

The entry rules delete ``{ 'k': true }`` because a boolean value is evidence
the literal is a flag override. A test helper often passes the value through a
parameter instead::

    function renderChecklist({ flag = true }: { flag?: boolean } = {}) {
      return render(<Provider flags={{ 'k': flag }}>…</Provider>);
    }

No rule can see that ``flag`` is a boolean: the evidence is a binding
somewhere above the entry, and a tree-sitter query cannot follow a name to its
binding without enumerating every way the name could be re-bound in between,
which is the enumeration this tool keeps out of its rules. So the entry used to
survive, and while it named the key, the registry entry the key is typed
against had to survive with it.

This pass makes the proof in Python, and makes it narrow on purpose:

* **The binding.** Walking up from the entry, the first function whose
  parameters, or the first block whose ``const``/``let`` declarations, bind
  the name decide it. It counts as boolean when it is a plain parameter or a
  declaration annotated ``boolean`` or defaulted/initialised to ``true`` or
  ``false``, or a shorthand property of an object-pattern parameter that is
  defaulted to a boolean literal or typed ``boolean`` by the parameter's own
  object type. Anything else declines.
* **Nothing else uses the name.** Inside that function or block, every
  occurrence of the name has to be the binding itself or the value of another
  entry keyed by this flag. That one check rules out a re-binding in between
  (an inner parameter, a ``catch``, a ``for`` variable, a nested function's
  own name), a reassignment, and any other read — it rejects a lot that would
  have been safe, and needs no list of binding forms to stay sound.

Because the name is then read by nothing but the entries this pass deletes, the
binding is left unread, which an unused-variable lint rejects. A ``const`` is
then removed by the stranded-binding pass like any other local. A parameter is
removed by :func:`drop_unread_parameters` only where every call is in sight and
none passes it; anywhere else it is REPORTED, because removing a parameter
changes every call that passes it. :func:`emptied_overrides` reports the
literals the deletions left as ``{}``.

The pass runs on the ENGINE's output, in the window every text pass shares, so
Gate 1 judges its edits and the all-or-nothing rollback covers them.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk

#: Nodes whose parameters can bind a name.
_FUNCTIONS = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
        "generator_function",
    }
)

#: Nodes whose direct ``const``/``let`` declarations bind a name.
_BLOCKS = frozenset({"statement_block", "program"})

#: The leaf types that spell a VALUE named by an identifier: a read, a binding,
#: or a shorthand property (``{ flag }``) that is both. A member name
#: (``property_identifier``) and a type name are not the variable.
_NAME_LEAVES = frozenset(
    {"identifier", "shorthand_property_identifier_pattern", "shorthand_property_identifier"}
)

_PARAMETERS = frozenset({"required_parameter", "optional_parameter"})


@dataclass(frozen=True)
class Result:
    """What :func:`delete_bound_entries` did to one file."""

    content: str
    #: The text of each deleted entry, e.g. ``'k': flag``.
    entries: tuple[str, ...] = ()
    #: Each binding left unread by those deletions, on one line: the whole
    #: parameter or declaration, or just the pattern member when the
    #: parameter binds other names too.
    unread: tuple[str, ...] = ()
    #: ``(function name, parameter)`` for each unread binding that is a whole
    #: parameter of a named function — what :func:`drop_unread_parameters`
    #: takes. The text alone is no identity: two helpers can share it.
    parameters: tuple[tuple[str, str], ...] = ()


def delete_bound_entries(source: str, language: str, flag_key: str) -> Result:
    """``source`` without each entry keyed by ``flag_key`` whose value is a
    name provably bound to a boolean, and what it deleted."""
    data = source.encode("utf-8")
    root = _parse(source, language)
    # binder node id -> (binding, the entries reading it)
    by_binder: dict[int, tuple[_Binding, list[Node]]] = {}
    for pair in _walk(root):
        if not _keyed_by(pair, data, flag_key):
            continue
        value = pair.child_by_field_name("value")
        if value is None or value.type != "identifier":
            continue
        binding = _binding_of(value, _text(value, data), data)
        if binding is None:
            continue
        entry = by_binder.setdefault(binding.binder.id, (binding, []))
        entry[1].append(pair)

    spans: list[tuple[int, int]] = []
    entries: list[tuple[int, str]] = []
    unread: list[tuple[int, str]] = []
    parameters: list[tuple[str, str]] = []
    for binding, pairs in by_binder.values():
        if not _only_these_use(binding, pairs, data, flag_key):
            continue
        taken = 0
        for pair in pairs:
            span = _deletion_span(pair)
            if span is None:
                continue
            spans.append(span)
            entries.append((pair.start_byte, _text(pair, data)))
            taken += 1
        if taken == len(pairs) and not binding.exported:
            label = binding.label(data)
            unread.append((binding.declaration.start_byte, label))
            if binding.whole_parameter(data):
                name = _function_name(binding.scope, data)
                if name is not None:
                    parameters.append((name, label))

    if not spans:
        return Result(source)
    for start, end in sorted(spans, reverse=True):
        data = data[:start] + data[end:]
    return Result(
        data.decode("utf-8"),
        tuple(text for _, text in sorted(entries)),
        tuple(text for _, text in sorted(unread)),
        tuple(parameters),
    )


@dataclass(frozen=True)
class _Binding:
    name: str
    #: The leaf that binds the name.
    binder: Node
    #: The parameter or declaration that holds it, for the report.
    declaration: Node
    #: The function or block within which the name means this binding.
    scope: Node
    exported: bool = False

    def whole_parameter(self, data: bytes) -> bool:
        """Whether the binding is a parameter binding no other name."""
        return self.declaration.type in _PARAMETERS and not any(
            leaf.id != self.binder.id
            for leaf in _walk(self.declaration.child_by_field_name("pattern"))
            if leaf.type in _NAME_LEAVES
        )

    def label(self, data: bytes) -> str:
        if self.declaration.type in _PARAMETERS and not self.whole_parameter(data):
            member = self.binder.parent
            node = member if member is not None and member.type == "object_assignment_pattern" else self.binder
            return _one_line(_text(node, data))
        return _one_line(_text(self.declaration, data))


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _keyed_by(pair: Node, data: bytes, flag_key: str) -> bool:
    """Whether ``pair`` sits in an object literal and its key is exactly ``flag_key``.

    Double-anchored like the entry rules: the key's ONLY named child is a
    fragment equal to the key, so an escape splitting it never matches.
    """
    if pair.type != "pair" or pair.parent is None or pair.parent.type != "object":
        return False
    key = pair.child_by_field_name("key")
    if key is None or key.type != "string":
        return False
    fragments = key.named_children
    return (
        len(fragments) == 1
        and fragments[0].type == "string_fragment"
        and _text(fragments[0], data) == flag_key
    )


def _binding_of(use: Node, name: str, data: bytes) -> _Binding | None:
    """The boolean binding ``use`` reads, or ``None`` when it is not provably one.

    The FIRST scope on the way up that binds the name decides: an unprovable
    binding there is a refusal, never a reason to look further out.
    """
    scope = use.parent
    while scope is not None:
        if scope.type in _FUNCTIONS:
            found = _parameter_binding(scope, name, data)
            if found is not None:
                return found or None
        elif scope.type in _BLOCKS:
            found = _declared_binding(scope, name, data)
            if found is not None:
                return found or None
        scope = scope.parent
    return None


def _parameter_binding(function: Node, name: str, data: bytes) -> _Binding | bool | None:
    """``None`` when ``function``'s parameters do not mention ``name``; the
    binding when they bind it provably as a boolean; ``False`` otherwise."""
    single = function.child_by_field_name("parameter")
    if single is not None:
        return False if _text(single, data) == name else None
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return None
    leaves = [
        leaf
        for leaf in _walk(parameters)
        if leaf.type in _NAME_LEAVES and _text(leaf, data) == name
    ]
    if not leaves:
        return None
    if len(leaves) != 1:
        return False
    binder = leaves[0]
    parameter = binder.parent
    while parameter is not None and parameter.type not in _PARAMETERS:
        parameter = parameter.parent
    if parameter is None:
        return False
    pattern = parameter.child_by_field_name("pattern")
    proven = False
    if pattern is not None and pattern.id == binder.id:
        proven = _boolean_annotation(parameter.child_by_field_name("type")) or _boolean_literal(
            parameter.child_by_field_name("value")
        )
    elif pattern is not None and pattern.type == "object_pattern" and binder.parent is not None:
        holder = binder.parent
        if holder.id == pattern.id and binder.type == "shorthand_property_identifier_pattern":
            proven = _typed_boolean_member(parameter, name, data)
        elif (
            holder.type == "object_assignment_pattern"
            and holder.parent is not None
            and holder.parent.id == pattern.id
            and holder.child_by_field_name("left") is not None
            and holder.child_by_field_name("left").id == binder.id
        ):
            proven = _boolean_literal(
                holder.child_by_field_name("right")
            ) or _typed_boolean_member(parameter, name, data)
    if not proven:
        return False
    return _Binding(name, binder, parameter, function)


def _declared_binding(block: Node, name: str, data: bytes) -> _Binding | bool | None:
    """The block's own ``const``/``let`` binding of ``name``, as for parameters."""
    for statement in block.named_children:
        exported = False
        declaration = statement
        if statement.type == "export_statement":
            inner = statement.child_by_field_name("declaration")
            if inner is None:
                continue
            declaration, exported = inner, True
        if declaration.type != "lexical_declaration":
            continue
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            target = declarator.child_by_field_name("name")
            if target is None or target.type != "identifier" or _text(target, data) != name:
                continue
            proven = _boolean_annotation(
                declarator.child_by_field_name("type")
            ) or _boolean_literal(declarator.child_by_field_name("value"))
            if not proven:
                return False
            return _Binding(name, target, statement, block, exported)
    return None


def _only_these_use(binding: _Binding, pairs: list[Node], data: bytes, flag_key: str) -> bool:
    """Whether every occurrence of the name in the binding's scope is the
    binding itself or the value of an entry keyed by this flag (``pairs``)."""
    values = {
        value.id
        for pair in pairs
        if (value := pair.child_by_field_name("value")) is not None
    }
    for leaf in _walk(binding.scope):
        if leaf.type not in _NAME_LEAVES or _text(leaf, data) != binding.name:
            continue
        if leaf.id != binding.binder.id and leaf.id not in values:
            return False
    return True


def _deletion_span(pair: Node) -> tuple[int, int] | None:
    """The bytes deleting ``pair`` removes, separator included; ``None`` when a
    comment sits in them (it would be deleted, or stranded beside a comma)."""
    obj = pair.parent
    members = [c for c in obj.named_children if c.type != "comment"]
    comments = [c for c in obj.named_children if c.type == "comment"]
    index = next(i for i, m in enumerate(members) if m.id == pair.id)
    if len(members) == 1:
        # The sole entry: `{ 'k': flag }` -> `{}`, the shape the stub rule leaves.
        start, end = obj.start_byte + 1, obj.end_byte - 1
    elif index + 1 < len(members):
        start, end = pair.start_byte, members[index + 1].start_byte
    else:
        start, end = members[index - 1].end_byte, pair.end_byte
    if any(start < c.end_byte and c.start_byte < end for c in comments):
        return None
    return start, end


def _boolean_annotation(annotation: Node | None) -> bool:
    if annotation is None or annotation.type != "type_annotation":
        return False
    types = annotation.named_children
    return len(types) == 1 and types[0].type == "predefined_type" and types[0].text == b"boolean"


def _boolean_literal(node: Node | None) -> bool:
    return node is not None and node.type in ("true", "false")


def _typed_boolean_member(parameter: Node, name: str, data: bytes) -> bool:
    """Whether the parameter's own object type declares ``name`` as ``boolean``."""
    annotation = parameter.child_by_field_name("type")
    if annotation is None or len(annotation.named_children) != 1:
        return False
    object_type = annotation.named_children[0]
    if object_type.type != "object_type":
        return False
    for member in object_type.named_children:
        if member.type != "property_signature":
            continue
        member_name = member.child_by_field_name("name")
        if member_name is not None and _text(member_name, data) == name:
            return _boolean_annotation(member.child_by_field_name("type"))
    return False


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def emptied_overrides(before: str, after: str, language: str, flag_key: str) -> tuple[str, ...]:
    """The line of each object literal this removal emptied, e.g.
    ``<Provider flags={{}}>``, for the reviewer to judge.

    Taking the entry is right; what is left may not be. An empty override
    literal is harmless, but the prop or argument carrying it may now be
    pointless, and whether it can go depends on a type declared elsewhere — so
    it is reported, never edited. Read off the two texts, hunk by hunk: a hunk
    whose old side held N objects with nothing but entries keyed by the flag
    reports up to N empty object literals on its CHANGED new lines. Counting
    the old side's sole-keyed objects rather than the change in empty ones
    keeps a ``= {}`` default deleted in the same hunk from cancelling one out.
    """
    if before == after:
        return ()
    old_lines, old_keyed, _old_empty = _object_lines(before, language, flag_key)
    new_lines, _new_keyed, new_empty = _object_lines(after, language, flag_key)
    found: list[str] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("replace", "insert"):
            continue
        emptied = sum(1 for n in old_keyed if i1 <= n < i2)
        unchanged = set(old_lines[i1:i2])
        changed = [n for n in new_empty if j1 <= n < j2 and new_lines[n] not in unchanged]
        found.extend(new_lines[n].strip() for n in changed[:emptied])
    return tuple(found)


def _object_lines(
    source: str, language: str, flag_key: str
) -> tuple[list[str], list[int], list[int]]:
    """The source's lines, the line of each object literal holding nothing but
    entries keyed by ``flag_key``, and the line of each empty object literal."""
    data = source.encode("utf-8")
    keyed: list[int] = []
    empty: list[int] = []
    for node in _walk(_parse(source, language)):
        if node.type != "object":
            continue
        members = [c for c in node.named_children if c.type != "comment"]
        if not node.named_children:
            empty.append(node.start_point[0])
        elif members and all(_keyed_by(m, data, flag_key) for m in members):
            keyed.append(node.start_point[0])
    return source.splitlines(), sorted(keyed), sorted(empty)


def drop_unread_parameters(
    source: str, language: str, keys: frozenset[tuple[str, str]]
) -> tuple[str, tuple[str, ...]]:
    """``source`` without each parameter named in ``labels`` that no call can
    still pass, and the label of each one it removed.

    ``keys`` are the ``(function name, parameter)`` pairs
    :func:`delete_bound_entries` reported unread (``Result.parameters``), so
    this never touches a parameter the customer had already left unused. Asked
    AFTER the tests chosen for deletion are gone, because the test about the
    removed branch is usually the one caller that passed the argument
    (``renderChecklist(x, { flag: false })``).

    A parameter goes only when all of this holds, and is reported unread
    otherwise:

    * it is the LAST parameter, so no later argument shifts into its place,
      and there is no ``this`` parameter, which callers do not pass;
    * its function is a ``function`` declaration or a ``const`` bound to a
      function, NOT exported, so every caller is in this file;
    * every other occurrence of the function's name is the callee of a call
      passing fewer arguments than the parameter's position — a spread, a
      tagged template, or the function handed on as a value, refuses;
    * nothing in the function still names the binding, and no comment sits in
      the bytes the deletion takes.
    """
    if not keys:
        return source, ()
    data = source.encode("utf-8")
    root = _parse(source, language)
    spans: list[tuple[int, int]] = []
    dropped: list[tuple[int, str]] = []
    for parameter in _walk(root):
        if parameter.type not in _PARAMETERS:
            continue
        label = _one_line(_text(parameter, data))
        function = parameter.parent.parent if parameter.parent is not None else None
        name = _function_name(function, data) if function is not None else None
        if (name, label) not in keys:
            continue
        span = _droppable(parameter, root, data)
        if span is not None:
            spans.append(span)
            dropped.append((parameter.start_byte, label))
    if not spans:
        return source, ()
    for start, end in sorted(spans, reverse=True):
        data = data[:start] + data[end:]
    return data.decode("utf-8"), tuple(label for _, label in sorted(dropped))


def _droppable(parameter: Node, root: Node, data: bytes) -> tuple[int, int] | None:
    """The bytes removing ``parameter`` takes, or ``None`` when it may not go."""
    parameters = parameter.parent
    function = parameters.parent if parameters is not None else None
    if parameters is None or function is None:
        return None
    members = [c for c in parameters.named_children if c.type != "comment"]
    if members[-1].id != parameter.id:
        return None
    if any(
        (pattern := m.child_by_field_name("pattern")) is not None and pattern.type == "this"
        for m in members
    ):
        return None
    position = len(members) - 1
    name = _function_name(function, data)
    if name is None:
        return None
    bound = [
        leaf
        for leaf in _walk(parameter)
        if leaf.type in _NAME_LEAVES
    ]
    for leaf in bound:
        uses = [
            other
            for other in _walk(function)
            if other.type in _NAME_LEAVES and _text(other, data) == _text(leaf, data)
        ]
        if len(uses) != 1:
            return None
    for leaf in _walk(root):
        if leaf.type not in _NAME_LEAVES or _text(leaf, data) != name or _declares(leaf, function):
            continue
        call = leaf.parent
        if (
            call is None
            or call.type != "call_expression"
            or call.child_by_field_name("function") is None
            or call.child_by_field_name("function").id != leaf.id
        ):
            return None
        arguments = call.child_by_field_name("arguments")
        if arguments is None or arguments.type != "arguments":
            return None
        passed = [c for c in arguments.named_children if c.type != "comment"]
        if any(c.type == "spread_element" for c in passed) or len(passed) > position:
            return None
    if position == 0:
        # Everything between the parentheses, so a trailing `,` goes too.
        start, end = parameters.start_byte + 1, parameters.end_byte - 1
    else:
        start, end = members[position - 1].end_byte, parameter.end_byte
    if any(
        c.type == "comment" and start < c.end_byte and c.start_byte < end
        for c in parameters.named_children
    ):
        return None
    return start, end


def _function_name(function: Node, data: bytes) -> str | None:
    """The name every caller uses, for a function only this file can call."""
    if function.type == "function_declaration":
        holder = function
        name = function.child_by_field_name("name")
    elif function.type in ("arrow_function", "function_expression", "function"):
        holder = function.parent
        if holder is None or holder.type != "variable_declarator":
            return None
        if holder.child_by_field_name("value") is None or holder.child_by_field_name("value").id != function.id:
            return None
        name = holder.child_by_field_name("name")
        declaration = holder.parent
        if declaration is None or declaration.type != "lexical_declaration" or not declaration.text.startswith(b"const"):
            return None
        holder = declaration
    else:
        return None
    if name is None or name.type != "identifier":
        return None
    if holder.parent is not None and holder.parent.type == "export_statement":
        return None
    return _text(name, data)


def _declares(leaf: Node, function: Node) -> bool:
    """Whether ``leaf`` is the name the function is declared under."""
    if function.type == "function_declaration":
        name = function.child_by_field_name("name")
    else:
        name = function.parent.child_by_field_name("name")
    return name is not None and name.id == leaf.id
