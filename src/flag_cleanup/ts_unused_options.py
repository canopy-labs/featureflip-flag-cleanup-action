"""Remove an option no call sets any more from the function that declares it.

:mod:`flag_cleanup.ts_default_arguments` leaves out ``{ enabled: true }``
when the fold wrote that ``true`` and the callee destructures ``enabled``
with the same default::

    export function useRole(
      id: string,
      { enabled = true }: { enabled?: boolean } = {}
    ) {
      return useMembers(id, { enabled });
    }

When that call was the last one setting the option, nothing can set it any
more. This module removes it from the declaration and writes its default where
the function reads it, so ``{ enabled }`` becomes ``{ enabled: true }``. It
stops there: the value now passed on is not followed into ``useMembers``.

**Only an option this run's fold set is a candidate.** The input is the list
:class:`~flag_cleanup.ts_default_arguments.DroppedOption` the drop produced,
which holds only a boolean the run wrote. An option no call sets because the
customer never set it is left alone, as is one a call still sets to the same
value by hand.

**The proof is all or nothing per option**, and anything outside it refuses:

* **The function** is a top-level ``function`` or a ``const`` bound to an
  arrow or function expression, in a module, exported by ``export`` on the
  declaration or by one unaliased ``export { name }``, or not exported at
  all. A default export, a wrapper such as ``memo(…)``, type parameters, a
  ``this`` parameter, ``arguments`` and ``eval`` all refuse.
* **Every spelling of its name** in the checkout's TypeScript and JavaScript,
  ignored files included, is checked as
  :mod:`flag_cleanup.ts_exported_props` checks a component's, with a call in
  place of a JSX element: in the declaring file, the declaration, its
  ``export { name }`` and the callee of a call; in any other file, one
  unaliased named import resolving to the declaring file and the callee of a
  call. A re-export, a namespace import, an alias, the function handed on as
  a value, a string spelling the name, or a mention in a markup file refuses,
  and so does a module that reaches the declaring file unspelled (``import *
  as``, ``import()``, a barrel's ``export *`` to one of those).
* **Every call** passes no spread argument, and at the parameter's position
  passes nothing or an object literal that does not set the option and holds
  only plain keys (:func:`flag_cleanup.ts_option_params.passed`).
* **The declaration** binds the option as
  :func:`flag_cleanup.ts_option_params.option` reads it, with a ``true`` or
  ``false`` default. When it is the only option, the parameter must be the
  last, and goes whole (with any ``{}`` a call passes for it).
* **Each read** of the binding in the function is a shorthand property of an
  object literal or a property's value, where the literal needs no further
  work and its type widens to ``boolean`` as the read's did: the object
  (through enclosing objects, arrays and parentheses) initializes a
  variable, is what the function returns, or is passed to a function
  declared without type parameters. A read in a condition or an operand
  would need the engine's fold, which has already run, so it refuses; so
  does a bare call argument or an object passed to a callee that is generic
  or cannot be resolved (either can infer the literal type), an object under
  ``as const``, ``satisfies`` or a type assertion, and the name bound a
  second time anywhere in the function.
* **Every file edited** is one this run changed, or one no other run of the
  same removal takes: it does not spell the flag key, nor (as the caller
  decides) a key constant or candidate wrapper another language of the
  family declares.

This runs on the engine's output, right after the drop that made the option
unused, so it judges the bytes that stay on disk; Gate 1 judges its edits and
the all-or-nothing rollback covers them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import ts_list_join, ts_option_params, ts_scope
from flag_cleanup.ts_bound_overrides import _one_line
from flag_cleanup.ts_constant_props import _is_module
from flag_cleanup.ts_default_arguments import DroppedOption, _parameters, _Resolver
from flag_cleanup.ts_exported_props import _exported_function, _File, _read, _users
from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})

#: The leaves a read or a binding of the option's name can be.
_NAME_LEAVES = frozenset(
    {"identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"}
)


@dataclass(frozen=True)
class Removed:
    #: ``path -> (previous text, rewritten text)`` for each file edited.
    files: dict[str, tuple[str, str]]
    #: ``(declaring path, label)`` for each option removed: the parameter
    #: when it went whole, else the pattern member.
    labels: tuple[tuple[str, str], ...]


def remove_unused_options(
    options: Iterable[DroppedOption],
    files: Mapping[str, tuple[str, str]],
    flag_key: str,
    repo_dir: str,
    taken: Callable[[str], bool] | None = None,
) -> Removed:
    """Each of ``options`` no call sets any more, removed; see the module
    docstring for the proof.

    ``files`` maps each file of the run to ``(customer's text, current
    text)``; a file outside it is read from disk. ``taken(text)`` says
    whether another run of the same removal takes a file outside ``files``
    with that text; without it, one spelling the flag key is.
    """
    if taken is None:
        taken = lambda text: flag_key in text  # noqa: E731
    state = dict(files)
    rewritten: dict[str, tuple[str, str]] = {}
    labels: list[tuple[str, str]] = []
    for option in dict.fromkeys(options):
        by_resolved = {str(Path(p).resolve()): p for p in state}
        planned = _plan(option, state, by_resolved, files, taken, repo_dir)
        if planned is None:
            continue
        edits, declaring, label = planned
        for file, (deletions, replacements) in edits.items():
            width = ts_list_join.line_limit(file.path, file.original, repo_dir)
            text = _apply(file.current, deletions, replacements, ts_scope.grammar(file.path), width)
            state[file.path] = (file.original, text)
            previous = rewritten.get(file.path, (file.current, ""))[0]
            rewritten[file.path] = (previous, text)
        labels.append((declaring, label))
    return Removed(rewritten, tuple(labels))


def _plan(option, state, by_resolved, run_files, taken, repo_dir):
    """``({file: (deletions, replacements)}, declaring path, label)``, or None."""
    found = _read(option.path, state, by_resolved)
    if found is None:
        return None
    spelled, original, current = found
    root = _parse(current, ts_scope.grammar(spelled))
    if root.has_error or not _is_module(root):
        return None
    data = current.encode("utf-8")
    declared = _exported_function(root, data, option.function, private=True)
    if declared is None:
        return None
    function, allowed = declared
    if not _plain(function) or function.child_by_field_name("type_parameters") is not None:
        return None
    parameters = [c for c in _parameters(function) if c.type != "comment"]
    if option.position >= len(parameters) or any(
        (pattern := p.child_by_field_name("pattern")) is not None and pattern.type == "this"
        for p in parameters
    ):
        return None
    found_option = ts_option_params.option(parameters[option.position], option.option, data)
    if found_option is None or found_option.default.type not in ("true", "false"):
        return None
    if found_option.sole and option.position != len(parameters) - 1:
        return None
    decl = _File(spelled, original, current, root, data)
    users = _users(
        option.function,
        decl,
        allowed,
        state,
        by_resolved,
        repo_dir,
        in_run=lambda _path: True,
        use=_callee,
        # As the drop resolved the call that made the option unused.
        solution=True,
    )
    if users is None:
        return None
    deletions: dict[_File, list[tuple[int, int]]] = {}
    for file in users:
        for node in _walk(file.root):
            if not _callee(node) or node.text.decode("utf-8") != option.function:
                continue
            spans = _call_spans(node.parent, found_option, option.position, file.data)
            if spans is None:
                return None
            if spans:
                deletions.setdefault(file, []).extend(spans)
    spans = ts_option_params.declaration_spans(found_option, data)
    if spans is None:
        return None
    deletions.setdefault(decl, []).extend(spans)
    resolver = _Resolver(root, data, spelled, repo_dir, ts_scope.grammar(spelled))
    replacements = _reads(function, found_option, data, resolver)
    if replacements is None:
        return None
    edits = {file: (spans, replacements if file is decl else []) for file, spans in deletions.items()}
    run = {str(Path(p).resolve()) for p in run_files}
    for file in edits:
        if str(Path(file.path).resolve()) not in run and taken(file.current):
            # Another run of this removal takes the file too: editing it here
            # as well would propose its hunks twice.
            return None
    if any(_overlap(spans, replacements if file is decl else []) for file, (spans, replacements) in edits.items()):
        return None
    node = found_option.parameter if found_option.sole else found_option.member
    return edits, spelled, _one_line(node.text.decode("utf-8"))


def _plain(function: Node) -> bool:
    """A ``function`` declaration, or a function a ``const`` is bound to directly."""
    if function.type == "function_declaration":
        return True
    holder = function.parent
    return (
        function.type in _FUNCTION_VALUES
        and holder is not None
        and holder.type == "variable_declarator"
        and holder.child_by_field_name("value") == function
    )


def _callee(node: Node) -> bool:
    """Whether ``node`` is the identifier a plain call is made through."""
    parent = node.parent
    return (
        node.type == "identifier"
        and parent is not None
        and parent.type == "call_expression"
        and parent.child_by_field_name("function") == node
    )


def _call_spans(call: Node, found, position: int, data: bytes) -> list[tuple[int, int]] | None:
    """The bytes removing the option from ``call`` takes, or None to refuse."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None or arguments.type != "arguments":
        # A tagged template.
        return None
    passed = ts_option_params.elements(arguments)
    if any(a.type == "spread_element" for a in passed):
        return None
    argument = passed[position] if position < len(passed) else None
    what = ts_option_params.passed(argument, found.name, data)
    if what is None or what.pair is not None:
        return None
    return ts_option_params.argument_spans(found, what)


def _reads(
    function: Node, found, data: bytes, resolver: _Resolver
) -> list[tuple[int, int, bytes]] | None:
    """The replacements writing the default at each read of the option in
    ``function``, or None when a read or a second binding refuses."""
    name, binder = found.name, found.binder
    if any(
        n.id != binder.id and n.text.decode("utf-8") == name for n in binding_nodes(function)
    ):
        return None
    default = found.default.text
    replacements: list[tuple[int, int, bytes]] = []
    for leaf in _walk(function):
        if leaf.type not in _NAME_LEAVES or leaf.id == binder.id or leaf.text.decode("utf-8") != name:
            continue
        parent = leaf.parent
        if (
            leaf.type == "shorthand_property_identifier"
            and parent.type == "object"
            and _widened(parent, function, resolver)
        ):
            replacements.append((leaf.start_byte, leaf.end_byte, leaf.text + b": " + default))
        elif (
            leaf.type == "identifier"
            and parent.type == "pair"
            and parent.child_by_field_name("value") == leaf
            and _widened(parent.parent, function, resolver)
        ):
            replacements.append((leaf.start_byte, leaf.end_byte, default))
        else:
            # A bare argument too: a generic callee (`<T>(v: T) => T`) infers
            # `true` from the literal where it inferred `boolean` from the
            # read, so a later `=== false` stops compiling.
            return None
    return replacements


#: The expressions that keep an object literal's property types narrow.
_NARROWING = frozenset({"as_expression", "satisfies_expression", "type_assertion"})

#: The nodes an object literal's type flows out through unchanged.
_ENCLOSING = frozenset({"parenthesized_expression", "object", "array", "pair"})


def _widened(literal: Node, function: Node, resolver: _Resolver) -> bool:
    """Whether a property of the object ``literal`` has its type widened, so a
    ``true`` there types as ``boolean`` just as the read did.

    Through any enclosing objects, arrays and parentheses, the outermost
    literal must be a ``const``/``let`` initializer, the value ``function``
    itself returns, or an argument of a call to a function the resolver
    proves has no type parameters. Anywhere else the literal may be inferred
    into a type parameter, which keeps ``true`` where the read gave
    ``boolean`` (``<T extends { on: boolean }>(o: T) => T``, a callback's
    return, ``<const T>``), and ``as const``, ``satisfies`` or a type
    assertion keep it narrow outright.
    """
    node = literal
    while node.parent is not None and node.parent.type in _ENCLOSING:
        node = node.parent
    holder = node.parent
    if holder is None or holder.type in _NARROWING:
        return False
    if holder.type == "variable_declarator":
        return holder.child_by_field_name("value") == node
    if holder.type == "return_statement":
        return _enclosing_function(holder) == function
    if holder == function:
        # An arrow function's expression body.
        return holder.child_by_field_name("body") == node
    if holder.type == "arguments" and holder.parent is not None and holder.parent.type == "call_expression":
        callee = holder.parent.child_by_field_name("function")
        # Not `resolver.function`: a mock of the callee's module changes what
        # runs, not the declared parameter types this asks about.
        return (
            callee is not None
            and callee.type == "identifier"
            and resolver._lookup(callee, component=False) is not None
        )
    return False


_FUNCTION_NODES = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function",
        "generator_function_declaration",
    }
)


def _enclosing_function(node: Node) -> Node | None:
    node = node.parent
    while node is not None and node.type not in _FUNCTION_NODES:
        node = node.parent
    return node


def _overlap(deletions, replacements) -> bool:
    spans = sorted([(s, e) for s, e in deletions] + [(s, e) for s, e, _ in replacements])
    return any(start < end for (_, end), (start, _) in zip(spans, spans[1:]))


def _apply(text, deletions, replacements, grammar: str, width: int | None) -> str:
    """``text`` with ``replacements`` written, then ``deletions`` spliced out
    (shifted past the replacements), joining any list left with one element."""
    data = text.encode("utf-8")
    shifted = list(deletions)
    for start, end, new in sorted(replacements, reverse=True):
        data = data[:start] + new + data[end:]
        delta = len(new) - (end - start)
        shifted = [
            (s + delta if s >= end else s, e + delta if e >= end else e) for s, e in shifted
        ]
    return ts_list_join.splice(data.decode("utf-8"), shifted, grammar, width)
