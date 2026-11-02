"""Remove a private helper's parameter the fold made the same literal at every call.

The Java, Kotlin and Go twin of :mod:`flag_cleanup.ts_constant_params`::

    return buildQuery(flags.boolVariation("summary-tab", null, false));   // before
    return buildQuery(true);                                              // after the fold

When every call of a helper nothing outside its scope can call now passes the
same literal at one position, the parameter carries no information.
:func:`inline_across` deletes the argument at every call and the parameter from
the declaration, and writes ``inlined<marker>(<literal>)`` at each read, so the
language's marker rule (``replace_inlined_binding_marker_with_boolean_literal``
in ``java.toml``, ``kt.toml`` and ``go.toml``) creates the literal inside the
engine's cascade, and what the parameter guarded folds as a read written
inline would.

Which helpers qualify, and why:

* **Java: a ``private`` method.** Nothing outside the file can call it, and
  inside it only a plain ``m(…)`` or ``this.m(…)`` whose nearest enclosing
  class body is the declaring one is proven. A call from a nested, local or
  anonymous class could bind to a member that class inherits.
* **Kotlin: a ``private fun``**, a member (proven calls are bare or
  ``this.``, directly in the declaring class's body) or top-level (file
  scope; a call inside a class with supertypes could bind to an inherited
  member instead, so it refuses, and so does a top-level helper whose name any
  other Java or Kotlin file spells: a function of the package it would clash
  with, or an extension of the calling class that would win). No call may sit
  under another implicit receiver: inside an extension function or property,
  or inside a lambda other than one passed to a call known to take a plain
  lambda (``map``, ``forEach``, ``let``…), since a lambda with receiver
  (``with``, ``apply``, a DSL builder) puts its receiver's members first.
  Extension, ``operator`` and ``infix`` functions refuse.
* **Go: an unexported function.** Every file of its package, in its
  directory, is read, and the name must be spelled only by its one declaration
  and as the callee of a plain call (a type of the name refuses: ``f(true)``
  would be a conversion). ``//export`` and ``go:linkname`` lines, and an
  assembly file in the directory naming it, refuse. A grouped declaration
  (``a, b bool``) at or before the parameter refuses, since it holds several
  argument positions.
* **The name is spelled nowhere else.** Every other spelling in the scope
  (a method reference, a local of the same name, a second declaration) refuses.
  For Java and Kotlin, which reflect over names, any string literal or comment
  link in the file naming it refuses, and so does any other file of the
  checkout naming it in a string, in Groovy or Scala, or in a file nothing here
  parses (Spring XML can name a private ``init-method``).
* **The transform made it a literal.** Summed over the calls, the output
  passes the literal at that position more often than the customer's code
  did. A literal the customer already wrote at every call is theirs, and
  stays.
* **The parameter is a plain ``boolean``** (Java ``boolean``, Kotlin
  ``Boolean``, Go ``bool``): no default, not variadic, not annotated, not
  grouped with another Go name. Every parameter before it is positional at
  every call (no Kotlin default or ``vararg``, no named or spread argument).
* **Every read is an ordinary expression** in an allow-listed position, and
  the name is never written, declared again or read inside a nested class
  body. Kotlin's ``$name`` template refuses.
* **A Java or Kotlin class with supertypes keeps a helper that would still
  take arguments.** With a parameter gone, an inherited overload the run cannot
  see could become the better match for a call (``h(Object, boolean)`` losing
  its flag beside an inherited ``h(String)``). A helper left with no
  parameters cannot be out-matched that way: an inherited ``h()`` it would
  clash with is a compile error, not a silent rebinding.

Every rewritten file must still parse: a deletion that would leave a list
malformed (Java has no trailing comma; ``f(,)``) refuses instead.

A refusal leaves the literal argument standing, which is what the run produced
before this pass existed. The edit is all or nothing across the files it
touches; :func:`flag_cleanup.piranha_runner._inline_bound_reads` unwinds every
one of them if the marker survives in any.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup.ts_constant_params import _list_item_span

logger = logging.getLogger(__name__)

Edit = tuple[int, int, bytes]

_LITERALS = frozenset({"true", "false"})
#: Put before the run's marker for the call written at each read. Not the bare
#: marker: that is also the wrapper sentinel, whose rule folds any call of it
#: to the treatment whatever its argument (see the marker rule in ``kt.toml``).
PARAM_PREFIX = "inlined"
#: Members every JVM class has: a helper renamed into one of them by losing a
#: parameter could be out-matched by the inherited one.
_OBJECT_METHODS = frozenset(
    {"equals", "hashCode", "toString", "getClass", "notify", "notifyAll", "wait", "clone", "finalize"}
)
#: Files other languages own: nothing in them can reach a JVM private member.
_OTHER_SOURCES = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rb", ".erb", ".php",
    ".swift", ".dart", ".cs", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".md",
)


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


@dataclass
class _Helper:
    """One candidate: its declaration, the parameters and the proven calls."""

    path: str
    name: str
    params: list[Node]
    body: Node
    #: (path, argument items, argument-list node) for every proven call.
    calls: list[tuple[str, list[Node], Node]]
    #: Whether a class it is a member of has supertypes (Java and Kotlin).
    inherits: bool = False
    #: path -> the bytes of that file the plan edits.
    data: dict[str, bytes] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def inline_across(
    files: dict[str, tuple[str, str]],
    repo_dir: str,
    language: str,
    marker: str,
    in_run: Callable[[str], bool],
) -> dict[str, tuple[str, str]]:
    """One helper's parameter removed, as ``{path: (previous, rewritten)}``.

    ``files`` maps each file the run holds to ``(customer's text, current
    text)``. Only a file the run changed is searched for a helper, and only
    one helper is removed per call; the marker round asks again.
    """
    finder = _FINDERS.get(language)
    if finder is None:
        return {}
    for path, (before, after) in sorted(files.items()):
        if before == after or marker in after or not in_run(path) or path.endswith(".kts"):
            continue
        try:
            planned = finder(path, before, after, files, repo_dir, marker)
        except (OSError, UnicodeDecodeError) as exc:
            logger.info("a private helper in %s is left as it is: %s", path, exc)
            planned = None
        if planned:
            return planned
    return {}


def _current(path: str, files: dict[str, tuple[str, str]]) -> str:
    return files[path][1] if path in files else Path(path).read_text(encoding="utf-8")


def _original(path: str, files: dict[str, tuple[str, str]]) -> str:
    return files[path][0] if path in files else Path(path).read_text(encoding="utf-8")


def _plan(
    helper: _Helper,
    old_calls: list[tuple[str, list[Node]]],
    old_data: dict[str, bytes],
    marker: str,
    positional: Callable[[int], bool],
    plain: Callable[[Node], str | None],
    references: Callable[[Node, str], list[tuple[Node, bool]] | None],
    literal_of: Callable[[Node, bytes], str | None],
) -> dict[str, list[Edit]] | None:
    """The edits removing the first qualifying parameter, or None.

    ``positional(i)`` says whether the parameter at ``i`` still sits at the
    same argument position at every call; ``plain`` returns a qualifying
    parameter's name; ``references`` returns each read of a name in the body
    (node to replace, whether it is a negation to fold into the literal), or
    None when any spelling is not a read.
    """
    for index, parameter in enumerate(helper.params):
        if not positional(index):
            return None
        name = plain(parameter)
        if name is None:
            continue
        literals = {
            literal_of(items[index], helper.data[p]) if len(items) > index else None
            for p, items, _ in helper.calls
        }
        if len(literals) != 1 or None in literals:
            continue
        literal = literals.pop()
        assert literal is not None
        earlier = sum(
            1
            for path, items in old_calls
            if len(items) > index and literal_of(items[index], old_data[path]) == literal
        )
        if len(helper.calls) <= earlier:
            continue
        if helper.inherits and len(helper.params) > 1:
            return None
        reads = references(helper.body, name)
        if reads is None:
            continue
        edits: dict[str, list[Edit]] = {}
        failed = False
        for path, items, _ in helper.calls:
            span = _list_item_span(items[index], items, helper.data[path])
            if span is None:
                failed = True
                break
            edits.setdefault(path, []).append((*span, b""))
        if failed:
            continue
        span = _list_item_span(parameter, helper.params, helper.data[helper.path])
        if span is None:
            continue
        own = edits.setdefault(helper.path, [])
        own.append((*span, b""))
        for node, negations in reads:
            value = literal if negations % 2 == 0 else ("false" if literal == "true" else "true")
            own.append((node.start_byte, node.end_byte, f"{PARAM_PREFIX}{marker}({value})".encode("utf-8")))
        if any(_overlap(e) for e in edits.values()):
            continue
        return edits
    return None


def _overlap(edits: list[Edit]) -> bool:
    ordered = sorted(edits)
    return any(s < e for (_, e, _), (s, _, _) in zip(ordered, ordered[1:]))


def _apply(data: bytes, edits: list[Edit]) -> str:
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8")


def _jvm_mentioned_elsewhere(repo_dir: str, path: str, name: str) -> bool:
    """Whether any other file of the checkout could reach ``name`` by its
    spelling: a string in a Java or Kotlin file (reflection), a Groovy or
    Scala file (which ignore ``private``), or a file nothing here parses."""
    from flag_cleanup import cross_file_keys, wrapper_inline, wrapper_jvm

    for other in cross_file_keys.mention_files(repo_dir, {name}, exclude=[path]):
        if other.endswith(_OTHER_SOURCES):
            continue
        if other.endswith(wrapper_jvm.PARSED_EXTENSIONS):
            if wrapper_inline._string_mentions(other, name, templates=other.endswith((".kt", ".kts"))):
                return True
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

_JAVA_BODIES = frozenset({"class_body", "interface_body", "enum_body", "annotation_type_body"})
_JAVA_COMMENTS = frozenset({"line_comment", "block_comment"})
#: (parent type, field) where an identifier is an ordinary read. ``None`` as
#: the field means any position of a node with no fields there.
_JAVA_READS = frozenset(
    {
        ("binary_expression", "left"),
        ("binary_expression", "right"),
        ("unary_expression", "operand"),
        ("parenthesized_expression", None),
        ("argument_list", None),
        ("return_statement", None),
        ("ternary_expression", "condition"),
        ("ternary_expression", "consequence"),
        ("ternary_expression", "alternative"),
        ("assignment_expression", "right"),
        ("variable_declarator", "value"),
        ("array_initializer", None),
        ("lambda_expression", "body"),
        ("assert_statement", None),
        ("for_statement", "condition"),
        ("cast_expression", "value"),
    }
)


def _field(node: Node) -> str | None:
    parent = node.parent
    if parent is None:
        return None
    for index, child in enumerate(parent.children):
        if child.id == node.id:
            return parent.field_name_for_child(index)
    return None


def _java_body_of(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.type not in _JAVA_BODIES:
        parent = parent.parent
    return parent


def _java_items(arguments: Node) -> list[Node]:
    return [c for c in arguments.named_children if c.type not in _JAVA_COMMENTS]


def _java_literal(node: Node, data: bytes) -> str | None:
    return node.type if node.type in _LITERALS else None


def _java_calls(root: Node, data: bytes, name: str) -> list[Node]:
    """Every ``name(…)`` / ``this.name(…)`` call's argument list."""
    found = []
    for node in _walk(root):
        if node.type != "method_invocation":
            continue
        callee = node.child_by_field_name("name")
        receiver = node.child_by_field_name("object")
        arguments = node.child_by_field_name("arguments")
        if (
            callee is not None
            and _text(callee, data) == name
            and (receiver is None or receiver.type == "this")
            and arguments is not None
        ):
            found.append(arguments)
    return found


def _java_inherits(body: Node) -> bool:
    owner = body.parent
    if owner is None:
        return True
    if owner.type == "class_declaration":
        return (
            owner.child_by_field_name("superclass") is not None
            or owner.child_by_field_name("interfaces") is not None
        )
    if owner.type == "interface_declaration":
        return any(c.type == "extends_interfaces" for c in owner.children)
    # enums, records, anonymous classes, annotation types: implicit supertypes
    return True


def _java(path, before, after, files, repo_dir, marker):
    from flag_cleanup import wrapper_java

    data, old_data = after.encode("utf-8"), before.encode("utf-8")
    root = wrapper_java.parse(after)
    if root.has_error:
        return None
    old_root = wrapper_java.parse(before)
    methods: dict[str, list[Node]] = {}
    for node in _walk(root):
        if node.type in ("method_declaration", "constructor_declaration"):
            name = node.child_by_field_name("name")
            if name is not None:
                methods.setdefault(_text(name, data), []).append(node)
    for name, declared in sorted(methods.items()):
        if len(declared) != 1 or name in _OBJECT_METHODS:
            continue
        method = declared[0]
        if method.type != "method_declaration" or not _java_private(method):
            continue
        body = method.child_by_field_name("body")
        parameters = method.child_by_field_name("parameters")
        owner = _java_body_of(method)
        if body is None or parameters is None or owner is None:
            continue
        calls = _java_proven_calls(root, data, name, method, owner)
        if not calls:
            continue
        params = [c for c in parameters.named_children if c.type not in _JAVA_COMMENTS]
        if any(p.type == "receiver_parameter" for p in params):
            continue
        helper = _Helper(
            path, name, params, body,
            [(path, _java_items(a), a) for a in calls],
            inherits=_java_inherits(owner),
            data={path: data},
        )
        if any(len(items) != len(params) for _, items, _ in helper.calls) and not any(
            p.type == "spread_parameter" for p in params
        ):
            continue
        old = [(path, _java_items(a)) for a in _java_calls(old_root, old_data, name)]

        def positional(index: int, params=params) -> bool:
            return params[index].type == "formal_parameter"

        edits = _plan(
            helper, old, {path: old_data}, marker, positional, _java_plain,
            lambda body, n: _java_reads(body, data, n), _java_literal,
        )
        if edits is None:
            continue
        if _jvm_mentioned_elsewhere(repo_dir, path, name):
            continue
        rewritten = _apply(data, edits[path])
        if wrapper_java.parse(rewritten).has_error:
            continue  # a list the deletion left malformed (a trailing comma)
        return {path: (after, rewritten)}
    return None


def _java_private(method: Node) -> bool:
    modifiers = next((c for c in method.children if c.type == "modifiers"), None)
    return modifiers is not None and any(c.type == "private" for c in modifiers.children)


def _java_proven_calls(root, data, name, method, owner) -> list[Node] | None:
    """Every call's argument list, or None when the name is spelled anywhere
    but the declaration and a call this pass can bind to it."""
    word = _word(name)
    declared = method.child_by_field_name("name")
    calls = []
    for node in _walk(root):
        if node.type in ("string_literal", "text_block", "character_literal"):
            if word.search(_text(node, data)):
                return None
            continue
        if node.type in _JAVA_COMMENTS:
            if re.search(rf"#{re.escape(name)}\b", _text(node, data)):
                return None  # `{@link #name(boolean)}` would point at a signature that went
            continue
        if node.type != "identifier" or _text(node, data) != name:
            continue
        if declared is not None and node.id == declared.id:
            continue
        call = node.parent
        if call is None or call.type != "method_invocation" or _field(node) != "name":
            return None
        receiver = call.child_by_field_name("object")
        arguments = call.child_by_field_name("arguments")
        if receiver is not None and receiver.type != "this":
            return None
        if arguments is None or _java_body_of(call) is None or _java_body_of(call).id != owner.id:
            return None
        calls.append(arguments)
    return calls


def _java_plain(parameter: Node) -> str | None:
    if parameter.type != "formal_parameter":
        return None
    kind = parameter.child_by_field_name("type")
    name = parameter.child_by_field_name("name")
    if kind is None or kind.type != "boolean_type" or name is None or name.type != "identifier":
        return None
    if any(c.type == "dimensions" for c in parameter.children):
        return None
    modifiers = next((c for c in parameter.children if c.type == "modifiers"), None)
    if modifiers is not None and any(c.type != "final" for c in modifiers.children):
        return None
    return name.text.decode("utf-8")


def _java_reads(body: Node, data: bytes, name: str) -> list[tuple[Node, int]] | None:
    reads = []
    for node in _walk(body):
        if node.type != "identifier" or _text(node, data) != name:
            continue
        parent = node.parent
        if parent is None or (parent.type, _field(node)) not in _JAVA_READS:
            return None
        if _java_body_of(node) is not None and body.start_byte <= _java_body_of(node).start_byte:
            return None  # inside a local or anonymous class: it may inherit the name
        reads.append((node, 0))
    return reads


# ---------------------------------------------------------------------------
# Kotlin
# ---------------------------------------------------------------------------

_KT_CLASSES = frozenset(
    {"class_declaration", "object_declaration", "companion_object", "object_literal", "enum_entry"}
)
_KT_READ_PARENTS = frozenset(
    {
        "binary_expression",
        "parenthesized_expression",
        "if_expression",
        "return_expression",
        "when_subject",
        "when_entry",
        "interpolation",
        "statements",
        "lambda_literal",
    }
)
_KT_MODIFIERS = frozenset({"private", "inline", "suspend", "tailrec"})


def _kt_class_of(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.type not in _KT_CLASSES:
        parent = parent.parent
    return parent


def _kt_inherits(owner: Node) -> bool:
    return any(c.type in ("delegation_specifiers", "delegation_specifier") for c in owner.children)


def _kt_literal(node: Node, data: bytes) -> str | None:
    if node.type != "value_argument" or len(node.named_children) != 1:
        return None
    value = node.named_children[0]
    text = _text(value, data)
    return text if value.type == "identifier" and text in _LITERALS else None


def _kt_items(arguments: Node) -> list[Node]:
    return [c for c in arguments.named_children if c.type == "value_argument"]


def _kt_head(node: Node) -> Node:
    """The outermost ``!`` the grammar hangs on a bare callee, or the node."""
    while (
        (parent := node.parent) is not None
        and parent.type == "unary_expression"
        and parent.children
        and parent.children[0].type == "!"
        and parent.named_children[-1].id == node.id
    ):
        node = parent
    return node


def _kt_call_of(node: Node) -> tuple[Node, Node] | None:
    """``(call, value_arguments)`` when ``node`` is the callee name of a bare
    or ``this.`` call, else None."""
    head = node
    parent = node.parent
    if parent is not None and parent.type == "navigation_expression":
        if parent.named_children[-1].id != node.id or len(parent.named_children) != 2:
            return None
        receiver = parent.named_children[0]
        if receiver.type != "this_expression" or _text_of(receiver) != "this":
            return None
        if not any(c.type == "." for c in parent.children):
            return None
        head = parent
    else:
        head = _kt_head(node)
    call = head.parent
    if call is None or call.type != "call_expression" or call.named_children[0].id != head.id:
        return None
    arguments = next((c for c in call.named_children if c.type == "value_arguments"), None)
    if arguments is None or any(c.type == "annotated_lambda" for c in call.named_children):
        return None
    return call, arguments


def _text_of(node: Node) -> str:
    return node.text.decode("utf-8") if node.text is not None else ""


def _kt(path, before, after, files, repo_dir, marker):
    from flag_cleanup import wrapper_kotlin

    data, old_data = after.encode("utf-8"), before.encode("utf-8")
    root = wrapper_kotlin.parse(after)
    if root.has_error:
        return None
    old_root = wrapper_kotlin.parse(before)
    functions: dict[str, list[Node]] = {}
    for node in _walk(root):
        if node.type == "function_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                functions.setdefault(_text(name, data), []).append(node)
    for name, declared in sorted(functions.items()):
        if len(declared) != 1 or name in _OBJECT_METHODS:
            continue
        function = declared[0]
        if not _kt_qualifies(function, data):
            continue
        scope = function.parent
        if scope is None:
            continue
        owner = None
        if scope.type in ("class_body", "enum_class_body"):
            owner = scope.parent
            if owner is None or owner.type not in _KT_CLASSES:
                continue
        elif scope.type != "source_file":
            continue  # a local function
        body = next((c for c in function.children if c.type == "function_body"), None)
        parameters = next((c for c in function.children if c.type == "function_value_parameters"), None)
        if body is None or parameters is None:
            continue
        calls = _kt_proven_calls(root, data, name, function, owner)
        if not calls:
            continue
        params = [c for c in parameters.named_children if c.type == "parameter"]
        helper = _Helper(
            path, name, params, body,
            [(path, _kt_items(a), a) for a in calls],
            inherits=owner is not None and _kt_inherits(owner),
            data={path: data},
        )
        if any(len(items) > len(params) for _, items, _ in helper.calls):
            continue
        old = []
        for node in _walk(old_root):
            if node.type == "identifier" and node.text == name.encode("utf-8"):
                found = _kt_call_of(node)
                if found is not None:
                    old.append((path, _kt_items(found[1])))

        def positional(index: int, params=params) -> bool:
            return _kt_positional(params[index])

        edits = _plan(
            helper, old, {path: old_data}, marker, positional,
            lambda p: _kt_plain(p, data),
            lambda body, n: _kt_reads(body, data, n), _kt_literal,
        )
        if edits is None:
            continue
        if _jvm_mentioned_elsewhere(repo_dir, path, name):
            continue
        if owner is None and _named_in_other_jvm_code(repo_dir, path, name):
            # A top-level function competes with every function of its name in
            # the package (a clash once its parameter goes) and loses to an
            # extension of the calling class declared anywhere.
            continue
        rewritten = _apply(data, edits[path])
        if wrapper_kotlin.parse(rewritten).has_error:
            continue  # a list the deletion left malformed (`f(,)`)
        return {path: (after, rewritten)}
    return None


def _named_in_other_jvm_code(repo_dir: str, path: str, name: str) -> bool:
    from flag_cleanup import cross_file_keys, wrapper_jvm

    return any(
        other.endswith(wrapper_jvm.PARSED_EXTENSIONS)
        for other in cross_file_keys.mention_files(repo_dir, {name}, exclude=[path])
    )


def _kt_qualifies(function: Node, data: bytes) -> bool:
    modifiers = next((c for c in function.children if c.type == "modifiers"), None)
    if modifiers is None:
        return False
    words = set()
    for child in modifiers.named_children:
        if child.type == "annotation":
            return False
        words.add(_text(child, data))
    if "private" not in words or not words <= _KT_MODIFIERS:
        return False
    # An extension function, `fun Receiver.name(…)`, is called on a receiver.
    return not any(c.type == "." for c in function.children)


def _kt_positional(parameter: Node) -> bool:
    """No default after it and no modifier (``vararg``, an annotation) before it."""
    previous = parameter.prev_sibling
    following = parameter.next_sibling
    return (
        previous is not None
        and previous.type in ("(", ",")
        and following is not None
        and following.type in (")", ",")
    )


def _kt_plain(parameter: Node, data: bytes) -> str | None:
    if not _kt_positional(parameter):
        return None
    named = parameter.named_children
    if len(named) != 2 or named[0].type != "identifier" or named[1].type != "user_type":
        return None
    kind = named[1]
    if len(kind.named_children) != 1 or _text(kind, data) != "Boolean":
        return None
    return _text(named[0], data)


def _kt_proven_calls(root, data, name, function, owner) -> list[Node] | None:
    word = _word(name)
    declared = function.child_by_field_name("name")
    calls = []
    for node in _walk(root):
        if node.type in ("string_literal", "multiline_string_literal"):
            if word.search(_text(node, data)):
                return None
            continue
        if node.type in ("line_comment", "block_comment", "multiline_comment"):
            if re.search(rf"\[{re.escape(name)}\]|#{re.escape(name)}\b", _text(node, data)):
                return None
            continue
        if node.type != "identifier" or _text(node, data) != name:
            continue
        if declared is not None and node.id == declared.id:
            continue
        found = _kt_call_of(node)
        if found is None:
            return None
        call, arguments = found
        if any(
            any(c.type == "=" for c in item.children) or any(c.type == "*" for c in item.children)
            for item in _kt_items(arguments)
        ):
            return None  # a named or spread argument
        if not _kt_receiver_free(call, data):
            return None
        enclosing = _kt_class_of(call)
        if owner is not None:
            # A member: the call must sit in the declaring class's own body.
            if enclosing is None or enclosing.id != owner.id or not _inside_body(call, owner):
                return None
        else:
            # Top-level: no class around the call may bring a member of the name.
            walker = enclosing
            while walker is not None:
                if _kt_inherits(walker):
                    return None
                walker = _kt_class_of(walker)
            if node.parent is not None and node.parent.type == "navigation_expression":
                return None  # `this.f()` is not a top-level function
        calls.append(arguments)
    return calls


#: Calls whose lambda argument takes an ordinary parameter (`it`), never a
#: receiver: a bare call inside it binds as it would outside. Any other lambda
#: may be a lambda with receiver (`with`, `apply`, `run`, `buildString`, a DSL),
#: whose receiver's members win over the helper.
_KT_PLAIN_LAMBDA_CALLS = frozenset(
    {
        "let", "also", "takeIf", "takeUnless", "forEach", "forEachIndexed", "onEach",
        "map", "mapIndexed", "mapNotNull", "flatMap", "filter", "filterNot", "filterIndexed",
        "any", "all", "none", "count", "first", "firstOrNull", "last", "lastOrNull",
        "find", "sumOf", "sortedBy", "sortedByDescending", "groupBy", "associate",
        "associateBy", "associateWith", "partition", "fold", "reduce", "maxByOrNull",
        "minByOrNull", "distinctBy", "repeat",
    }
)


def _kt_receiver_free(node: Node, data: bytes) -> bool:
    """No implicit receiver other than the class's own sits between ``node``
    and its enclosing class: no extension function or property body, and no
    lambda other than one passed to a call known to take a plain lambda."""
    walker = node.parent
    while walker is not None and walker.type not in _KT_CLASSES:
        if walker.type in ("function_declaration", "property_declaration", "getter", "setter"):
            if any(c.type == "." for c in walker.children):
                return False  # `fun Receiver.f()`: its receiver's members come first
        if walker.type == "anonymous_function":
            return False
        if walker.type == "lambda_literal":
            holder = walker.parent
            if holder is not None and holder.type in ("annotated_lambda", "value_argument"):
                holder = holder.parent
            if holder is not None and holder.type == "value_arguments":
                holder = holder.parent
            if holder is None or holder.type != "call_expression" or not holder.named_children:
                return False
            callee = holder.named_children[0]
            if callee.type == "navigation_expression":
                callee = callee.named_children[-1]
            if callee.type != "identifier" or _text(callee, data) not in _KT_PLAIN_LAMBDA_CALLS:
                return False
        walker = walker.parent
    return True


def _inside_body(node: Node, owner: Node) -> bool:
    body = next((c for c in owner.children if c.type in ("class_body", "enum_class_body")), None)
    return body is not None and body.start_byte <= node.start_byte and node.end_byte <= body.end_byte


def _kt_reads(body: Node, data: bytes, name: str) -> list[tuple[Node, int]] | None:
    template = re.compile(rf"\${re.escape(name)}(?![\w])")
    reads = []
    for node in _walk(body):
        if node.type == "string_content" and template.search(_text(node, data)):
            return None
        if node.type == "string_literal" and template.search(_text(node, data)):
            return None
        if node.type != "identifier" or _text(node, data) != name:
            continue
        if _kt_class_of(node) is not None and body.start_byte <= _kt_class_of(node).start_byte:
            return None
        if node.parent is not None and node.parent.type == "interpolation" and not _text(
            node.parent, data
        ).startswith("${"):
            return None  # a raw string's `$name`: a call there would not be a template
        target = _kt_head(node)
        negations = 0
        walker = node
        while walker.id != target.id:
            walker = walker.parent
            negations += 1
        parent = target.parent
        if parent is None:
            return None
        if parent.type == "value_argument":
            if len(parent.named_children) != 1 or any(c.type in ("=", "*") for c in parent.children):
                return None
        elif parent.type == "property_declaration":
            if not any(c.type == "=" for c in parent.children) or parent.named_children[-1].id != target.id:
                return None
        elif parent.type == "assignment":
            if _field(target) != "right":
                return None
        elif parent.type == "function_body":
            if not any(c.type == "=" for c in parent.children):
                return None
        elif parent.type not in _KT_READ_PARENTS:
            return None
        reads.append((target, negations))
    return reads


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

_GO_READS = frozenset(
    {
        ("binary_expression", "left"),
        ("binary_expression", "right"),
        ("unary_expression", "operand"),
        ("parenthesized_expression", None),
        ("argument_list", None),
        ("if_statement", "condition"),
        ("for_statement", None),
        ("expression_switch_statement", "value"),
    }
)


def _go_package(root: Node, data: bytes) -> str | None:
    clause = next((c for c in root.named_children if c.type == "package_clause"), None)
    if clause is None:
        return None
    name = next((c for c in clause.named_children if c.type == "package_identifier"), None)
    return _text(name, data) if name is not None else None


def _go_literal(node: Node, data: bytes) -> str | None:
    return node.type if node.type in _LITERALS else None


def _go_items(arguments: Node) -> list[Node]:
    return [c for c in arguments.named_children if c.type != "comment"]


def _go(path, before, after, files, repo_dir, marker):
    from flag_cleanup import wrapper_go

    data = after.encode("utf-8")
    root = wrapper_go.parse(after)
    if root.has_error:
        return None
    package = _go_package(root, data)
    if package is None:
        return None
    candidates = []
    for node in root.named_children:
        if node.type != "function_declaration":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, data)
        if name[:1].isupper() or name in ("init", "main", "_"):
            continue
        if node.child_by_field_name("type_parameters") is not None:
            continue
        candidates.append((name, node))
    if not candidates:
        return None
    package_files = _go_package_files(path, package, files)
    if package_files is None:
        return None
    for name, function in candidates:
        planned = _go_plan(path, name, function, package_files, files, repo_dir, marker)
        if planned:
            return planned
    return None


def _go_package_files(path, package, files) -> dict[str, tuple[Node, bytes, Node | None, bytes]] | None:
    """Each file of the package: (current root, current bytes, customer's root, customer's bytes)."""
    from flag_cleanup import wrapper_go

    directory = os.path.dirname(path)
    found = {}
    for entry in sorted(os.listdir(directory)):
        other = os.path.join(directory, entry)
        if not entry.endswith(".go") or not os.path.isfile(other):
            continue
        text = _current(other, files)
        data = text.encode("utf-8")
        root = wrapper_go.parse(text)
        if _go_package(root, data) != package:
            continue
        if root.has_error:
            return None
        original = _original(other, files)
        old_data = original.encode("utf-8")
        found[other] = (root, data, wrapper_go.parse(original), old_data)
    return found


def _go_plan(path, name, function, package_files, files, repo_dir, marker):
    word = _word(name)
    directory = os.path.dirname(path)
    for entry in os.listdir(directory):
        if entry.endswith((".s", ".S", ".c", ".h")):
            try:
                if word.search(Path(directory, entry).read_text(encoding="utf-8", errors="replace")):
                    return None
            except OSError:
                return None
    if _go_linknamed(repo_dir, name):
        return None
    declared = function.child_by_field_name("name")
    calls: list[tuple[str, list[Node], Node]] = []
    old: list[tuple[str, list[Node]]] = []
    for other, (root, data, old_root, old_data) in package_files.items():
        for node in _walk(root):
            if node.type == "comment":
                text = _text(node, data)
                if re.search(rf"//\s*export\s+{re.escape(name)}\b|go:linkname\s+{re.escape(name)}\b", text):
                    return None
                continue
            if node.type in ("type_identifier", "package_identifier") and _text(node, data) == name:
                return None  # `name(true)` may be a conversion to a type of the name
            if node.type != "identifier" or _text(node, data) != name:
                continue
            if other == path and declared is not None and node.start_byte == declared.start_byte:
                continue
            call = node.parent
            if call is None or call.type != "call_expression" or _field(node) != "function":
                return None
            arguments = call.child_by_field_name("arguments")
            if arguments is None or any(c.type == "..." for c in arguments.children):
                return None
            calls.append((other, _go_items(arguments), arguments))
        for node in _walk(old_root):
            if node.type == "identifier" and _text(node, old_data) == name and _field(node) == "function":
                arguments = node.parent.child_by_field_name("arguments")
                if arguments is not None:
                    old.append((other, _go_items(arguments)))
    if not calls:
        return None
    parameters = function.child_by_field_name("parameters")
    body = function.child_by_field_name("body")
    if parameters is None or body is None:
        return None
    params = [c for c in parameters.named_children if c.type != "comment"]
    helper = _Helper(
        path, name, params, body, calls,
        data={p: package_files[p][1] for p in package_files},
    )
    if any(len(items) != len(params) for _, items, _ in calls) and not any(
        p.type == "variadic_parameter_declaration" for p in params
    ):
        return None
    current_data = package_files[path][1]

    def positional(index: int) -> bool:
        # A grouped declaration (`a, b bool`) holds several argument
        # positions, so every position after it is off by one or more.
        return params[index].type == "parameter_declaration" and _go_names(params[index]) == 1

    def plain(parameter: Node) -> str | None:
        names = [c for i, c in enumerate(parameter.children) if parameter.field_name_for_child(i) == "name"]
        kind = parameter.child_by_field_name("type")
        if parameter.type != "parameter_declaration" or len(names) != 1 or kind is None:
            return None
        if _text(kind, current_data) != "bool":
            return None
        return _text(names[0], current_data)

    edits = _plan(
        helper, old, {p: package_files[p][3] for p in package_files}, marker, positional, plain,
        lambda b, n: _go_reads(b, current_data, n), _go_literal,
    )
    if edits is None:
        return None
    from flag_cleanup import wrapper_go

    planned = {
        p: (package_files[p][1].decode("utf-8"), _apply(package_files[p][1], e))
        for p, e in edits.items()
    }
    if any(wrapper_go.parse(rewritten).has_error for _, rewritten in planned.values()):
        return None  # a list the deletion left malformed (`f(,)`)
    return planned


def _go_names(parameter: Node) -> int:
    return sum(1 for i, _ in enumerate(parameter.children) if parameter.field_name_for_child(i) == "name")


def _go_linknamed(repo_dir: str, name: str) -> bool:
    from flag_cleanup import cross_file_keys

    pattern = re.compile(rf"go:linkname\s+\S+\s+\S*\.{re.escape(name)}\b")
    for other in cross_file_keys.mention_files(repo_dir, {name}, exclude=()):
        if not other.endswith(".go"):
            continue
        try:
            if pattern.search(Path(other).read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            return True
    return False


def _go_reads(body: Node, data: bytes, name: str) -> list[tuple[Node, int]] | None:
    reads = []
    for node in _walk(body):
        if node.type != "identifier" or _text(node, data) != name:
            continue
        parent = node.parent
        if parent is None:
            return None
        position = (parent.type, _field(node))
        if parent.type == "unary_expression":
            operator = parent.child_by_field_name("operator")
            if operator is None or operator.type != "!":
                return None
        elif parent.type == "expression_list":
            holder = parent.parent
            if holder is None:
                return None
            if holder.type == "return_statement":
                pass
            elif holder.type in ("assignment_statement", "short_var_declaration", "var_spec"):
                if _field(parent) not in ("right", "value"):
                    return None
            else:
                return None
        elif parent.type == "literal_element":
            holder = parent.parent
            if holder is None:
                return None
            if holder.type == "keyed_element" and _field(parent) != "value":
                return None
            if holder.type not in ("keyed_element", "literal_value"):
                return None
        elif position not in _GO_READS and (parent.type, None) not in _GO_READS:
            return None
        reads.append((node, 0))
    return reads


_FINDERS = {"java": _java, "kt": _kt, "go": _go}
