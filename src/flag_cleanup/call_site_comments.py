"""Report the comments at the call sites of a function that lost a flag read.

A hook or helper that read the flag and combined it with something else::

    export function useCanEdit(orgId: string) {
      return useFlag('new-edit') && usePlan(orgId).isPro;
    }

keeps its name and its callers when the flag is removed::

    export function useCanEdit(orgId: string) {
      return usePlan(orgId).isPro;
    }

    const canEdit = useCanEdit(orgId); // release flag + Pro plan

and the comment at the call is now wrong. It names no key and the call did not
change, so neither the comments that name the key nor the ones beside a changed
line see it. This pass finds it: when the body of a named function lost a read
of the key, every comment on the same line as a call to it, or directly above
the statement holding the call, that contains the word ``flag`` or a form of
``gate``, is reported.

Known misses: a function loaded with ``require()``, a comment inside a JSX
expression container (``{/* gate */}``, which has code before it on its line),
a default import or an alias of an alias, and a hook in one configured
directory with a caller in another (each directory is its own pass).

This module only finds them and edits nothing: what such a comment should say
instead is for a person to decide, except for the one plain shape
:mod:`flag_cleanup.comment_conjuncts` cuts (``release flag + Pro plan``) when
the flag served ``true``. TypeScript and JavaScript only, since the engine's
per-language rules decide what a "read" looks like elsewhere and this needs
only the key as the read's argument.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, replace

from tree_sitter import Node

from flag_cleanup import unused

#: The row kind, with ``"<line>: <comment>"`` as its text.
CALL_SITE_KIND = "call-site comment"

#: A comment worth listing: the word ``flag`` or a form of ``gate``. Whole
#: words, so ``navigate`` and ``aggregate`` do not count.
_MENTIONS = re.compile(r"\b(?:flags?|gat(?:e|es|ed|ing))\b", re.IGNORECASE)

#: Nodes the climb from a call to its statement stops under: a statement is
#: what sits directly in one of these, so a comment far above the enclosing
#: function, class or file is never taken for the statement's own.
_BOUNDARIES = frozenset(
    {
        "statement_block",
        "program",
        "class_body",
        "switch_body",
        "switch_case",
        "switch_default",
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "class_declaration",
        "jsx_element",
        "jsx_fragment",
    }
)


@dataclass(frozen=True)
class CallSiteComment:
    path: str
    line: int
    text: str
    function: str
    #: Whether the function's body, as it stands, reads no flag at all (see
    #: :func:`reads_no_flag`): the only case a comment may be edited.
    clean: bool = False
    #: Whether the call hands the function no value that could be a flag.
    args_ok: bool = True


def _strip_bom(source: str) -> str:
    return source[1:] if source.startswith("\ufeff") else source


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _code_leaves(node: Node):
    """The leaves of ``node`` outside every comment."""
    stack = [node]
    while stack:
        current = stack.pop()
        if "comment" in current.type:
            continue
        if current.child_count == 0:
            yield current
        else:
            stack.extend(reversed(current.children))


def _reads(node: Node, names: re.Pattern[bytes]) -> bool:
    """Whether ``node``'s CODE names the key: a comment saying so is not a read."""
    return any(names.search(leaf.text) for leaf in _code_leaves(node))


def _plain_functions(root: Node) -> list[tuple[str, Node]]:
    """Functions called by their bare name: declarations and ``const f = () =>``.
    Methods are left out, since a call to one goes through a receiver this
    cannot resolve."""
    found: list[tuple[str, Node]] = []
    for node in _walk(root):
        name = None
        if node.type in ("function_declaration", "generator_function_declaration"):
            name = node.child_by_field_name("name")
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                name = node.child_by_field_name("name")
        if name is not None and name.type == "identifier":
            found.append((name.text.decode("utf-8", "replace"), node))
    return found


def functions_that_lost_the_read(before: str, after: str, grammar: str, names: re.Pattern[bytes]) -> set[str]:
    """The names of the functions whose body read the key in ``before`` and no
    longer does in ``after``: the innermost named function holding the read,
    still declared by that name."""
    before, after = _strip_bom(before), _strip_bom(after)
    if not names.search(before.encode("utf-8")):
        return set()
    old = _plain_functions(unused._parse(before, grammar))
    now = {name: node for name, node in _plain_functions(unused._parse(after, grammar))}
    holding = [(name, node) for name, node in old if _reads(node, names)]
    lost: set[str] = set()
    for name, node in holding:
        inner = any(
            other is not node
            and other.start_byte >= node.start_byte
            and other.end_byte <= node.end_byte
            for _, other in holding
        )
        if not inner and name in now and not _reads(now[name], names):
            lost.add(name)
    return lost


#: Callee names that read a flag: the SDK hooks and accessors of every
#: Featureflip, OpenFeature and common vendor client.
_READER = re.compile(
    r"variation|flagvalue|featureflag|usefeature|usegate|isenabled|isfeatureenabled|getflag|useflag"
    r"|(?:boolean|string|number|object|int|double|float)(?:value|details)|treatment|experiment|toggle",
    re.IGNORECASE,
)
_FLAG_KEY = re.compile(r"""^['"`][a-z0-9]+(?:[-_.][a-z0-9]+)+['"`]$""")
_FLAG_MODULES = re.compile(
    r"featureflip|openfeature|launchdarkly|flagsmith|unleash|growthbook|statsig|optimizely|posthog|split|flags?(?:/|$)",
    re.IGNORECASE,
)


def _flag_import_names(root: Node) -> set[str]:
    """The local names a file imports from a flag-client module."""
    names: set[str] = set()
    for statement in root.children:
        if statement.type != "import_statement":
            continue
        source = statement.child_by_field_name("source")
        if source is not None and _FLAG_MODULES.search(source.text.decode("utf-8", "replace")):
            names |= {n.text.decode("utf-8", "replace") for n in _walk(statement) if n.type == "identifier"}
    return names


#: Parameter types that cannot carry a boolean flag: primitives other than
#: ``boolean``, in a union with ``undefined`` or ``null``, or a literal.
_SAFE_TYPE = re.compile(
    r"^(?:string|number|bigint|undefined|null|'[^']*'|\"[^\"]*\"|-?\d+)(?:\s*\|\s*(?:string|number|bigint|undefined|null|'[^']*'|\"[^\"]*\"|-?\d+))*$"
)


def params_cannot_carry_a_flag(function: Node) -> bool:
    """Whether every parameter of ``function`` is typed as a primitive that is
    not ``boolean`` (``string``, ``number``, a union with ``undefined`` or
    ``null``, a literal). An untyped, boolean, ``any``, ``unknown``, object or
    destructured parameter can be handed a flag value by the caller, which would
    make a comment about "the flag" true of the caller's flag, not the removed
    one."""
    if function.type == "variable_declarator":
        function = function.child_by_field_name("value") or function
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return function.child_by_field_name("parameter") is None
    for parameter in parameters.named_children:
        if parameter.type not in ("required_parameter", "optional_parameter"):
            return False
        pattern = parameter.child_by_field_name("pattern")
        if pattern is None or pattern.type != "identifier":
            return False
        if parameter.child_by_field_name("value") is not None:
            return False  # a default value
        annotation = next((c for c in parameter.named_children if c.type == "type_annotation"), None)
        if annotation is None:
            return False
        text = annotation.text.decode("utf-8", "replace").lstrip(":").strip()
        if not _SAFE_TYPE.match(text):
            return False
    return True


def _holds_flag_call(node: Node, flag_names: set[str]) -> bool:
    for call in _walk(node):
        if call.type != "call_expression":
            continue
        callee = call.child_by_field_name("function")
        if callee is not None and callee.type == "member_expression":
            callee = callee.child_by_field_name("property")
        text = callee.text.decode("utf-8", "replace") if callee is not None else ""
        if text in flag_names or _READER.search(text):
            return True
    return False


def flag_bindings(root: Node) -> tuple[set[str], set[str]]:
    """``(flag client names, names the file binds to a flag read)``, read once
    per file."""
    flag_names = _flag_import_names(root)
    bound: set[str] = set()
    for node in _walk(root):
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            name = node.child_by_field_name("name")
            if value is not None and name is not None and _holds_flag_call(value, flag_names):
                bound |= {n.text.decode("utf-8", "replace") for n in _walk(name) if "identifier" in n.type}
    return flag_names, bound


def arguments_hand_over_no_flag(call: Node, facts: tuple[set[str], set[str]]) -> bool:
    """Whether ``call`` passes no boolean literal, no flag read, and no
    identifier the file binds to a flag read (``facts`` from
    :func:`flag_bindings`)."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return True
    flag_names, bound = facts
    for argument in arguments.named_children:
        if argument.type in ("true", "false"):
            return False
        if _holds_flag_call(argument, flag_names):
            return False
        if any(
            n.type == "identifier" and n.text.decode("utf-8", "replace") in bound for n in _walk(argument)
        ):
            return False
    return True


def _loads_no_flag_client(path: str, source: str, repo_dir: str) -> bool:
    """Whether the file, as it stands after the run, loads no flag-client module
    through its whole import closure (see
    :func:`flag_cleanup.ts_test_providers.loads_no_flag_client`)."""
    from flag_cleanup import ts_test_providers

    return ts_test_providers.loads_no_flag_client(path, _strip_bom(source), repo_dir)


def _in_type(leaf: Node, stop: Node) -> bool:
    node = leaf.parent
    while node is not None and node is not stop:
        if node.type in ("type_annotation", "type_alias_declaration", "interface_declaration"):
            return True
        node = node.parent
    return False


def reads_no_flag(function: Node) -> bool:
    """Whether the CODE of ``function`` holds no flag read at all: no call of
    a hook or accessor that reads a flag, no identifier bound by an import of
    a flag client, and no string that is shaped like a flag key. A function
    that still reads ANOTHER flag is never one a comment about "the flag" can
    be cut for. Deliberately wider than any single SDK."""
    root = function
    while root.parent is not None:
        root = root.parent
    flag_names = _flag_import_names(root)
    imported: set[str] = set()
    for statement in root.children:
        if statement.type == "import_statement":
            imported |= {n.text.decode("utf-8", "replace") for n in _walk(statement) if n.type == "identifier"}
    # A context value can carry a flag from elsewhere.
    for call in _walk(function):
        if call.type != "call_expression":
            continue
        callee = call.child_by_field_name("function")
        if callee is not None and callee.type == "member_expression":
            callee = callee.child_by_field_name("property")
        if callee is None or callee.text.decode("utf-8", "replace") not in ("useContext", "use"):
            continue
        arguments = call.child_by_field_name("arguments")
        if arguments is not None and any(
            n.type == "identifier" and n.text.decode("utf-8", "replace") in imported for n in _walk(arguments)
        ):
            return False
    declared = function.child_by_field_name("name")
    for leaf in _code_leaves(function):
        if declared is not None and leaf.start_byte == declared.start_byte:
            continue  # the function's own name says nothing about its body
        if _in_type(leaf, function):
            continue  # a type declares a read, it does not make one
        text = leaf.text.decode("utf-8", "replace")
        if leaf.type == "identifier" and (text in flag_names or _READER.search(text)):
            return False
        if leaf.type in ("property_identifier",) and _READER.search(text):
            return False
        if leaf.type == "string_fragment":
            quoted = leaf.parent.text.decode("utf-8", "replace") if leaf.parent is not None else text
            if _FLAG_KEY.match(quoted):
                return False
    return True


def _statement(call: Node) -> Node:
    """The statement holding ``call``: the node directly under a block, a
    class body or the file."""
    node = call
    while node.parent is not None and node.parent.type not in _BOUNDARIES:
        node = node.parent
    return node


def _comment_text(node: Node) -> str:
    text = node.text.decode("utf-8", "replace")
    text = re.sub(r"^\s*(?://+|/\*+)\s*|\s*\*+/\s*$", "", text)
    return re.sub(r"\s*\n\s*(?:\*\s*)?", " ", text).strip()


_EXTENSIONS = (".d.ts", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".mts", ".cts")


def _stem(path: str) -> str:
    """``path`` without its extension and without a trailing ``/index``."""
    for suffix in _EXTENSIONS:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return path[: -len("/index")] if path.endswith("/index") else path


def _is_module(specifier: str, importer: str, defined: Collection[str], roots: Collection[str]) -> bool:
    """Whether ``specifier``, imported by the file ``importer``, names one of
    the ``defined`` files. A relative specifier is resolved against the
    importer. An ``@/x`` or ``~/x`` alias is the project's own source root, so
    it must equal ``<root>/x`` for a ``root`` in ``roots`` (the directory being
    processed, and its ``src``). Any other specifier is a package, which names
    no file of the project."""
    stems = {_stem(d) for d in defined}
    if specifier.startswith("."):
        return _stem(os.path.normpath(os.path.join(os.path.dirname(importer), specifier))) in stems
    if not specifier.startswith(("@/", "~/")):
        return False
    tail = specifier[2:].lstrip("/")
    return any(_stem(os.path.normpath(os.path.join(root, tail))) in stems for root in roots)


def _imports(
    root: Node, importer: str, defined: Collection[str], roots: Collection[str] = ()
) -> tuple[dict[str, str], set[str]]:
    """``(names, namespaces)`` a file takes from the defining modules:
    ``local name -> original name`` for each value import (a type-only one
    does not count), and the local names that stand for the whole module
    (``import * as hooks``)."""
    local: dict[str, str] = {}
    spaces: set[str] = set()
    for node in root.children:
        if node.type != "import_statement":
            continue
        source = node.child_by_field_name("source")
        if source is None:
            continue
        specifier = source.text.decode("utf-8", "replace").strip("'\"`")
        if not _is_module(specifier, importer, defined, roots):
            continue
        if re.match(rb"import\s+type\b", node.text):
            continue
        for part in _walk(node):
            if part.type == "import_specifier":
                if re.match(rb"type\s", part.text):
                    continue
                name = part.child_by_field_name("name")
                alias = part.child_by_field_name("alias")
                if name is not None:
                    original = name.text.decode("utf-8", "replace")
                    local[(alias or name).text.decode("utf-8", "replace")] = original
            elif part.type == "namespace_import":
                for child in part.children:
                    if child.type == "identifier":
                        spaces.add(child.text.decode("utf-8", "replace"))
    return local, spaces


def _pattern_names(node: Node | None) -> set[str]:
    """The names a binding pattern (a parameter, a destructuring) declares."""
    if node is None:
        return set()
    return {
        n.text.decode("utf-8", "replace")
        for n in _walk(node)
        if n.type in ("identifier", "shorthand_property_identifier_pattern")
    }


def _declares(scope: Node, name: str) -> bool:
    """Whether ``scope`` (a function or a block) declares ``name`` itself: a
    parameter, or a declaration directly in the block."""
    if scope.type in ("statement_block", "program"):
        for child in scope.children:
            if child.type in ("lexical_declaration", "variable_declaration"):
                for declarator in child.children:
                    if declarator.type == "variable_declarator" and name in _pattern_names(
                        declarator.child_by_field_name("name")
                    ):
                        return True
            elif child.type in ("function_declaration", "class_declaration"):
                declared = child.child_by_field_name("name")
                if declared is not None and declared.text.decode() == name:
                    return True
        return False
    parameters = scope.child_by_field_name("parameters") or scope.child_by_field_name("parameter")
    return name in _pattern_names(parameters)


def _shadowed(call: Node, name: str, *, stop_at_program: bool) -> bool:
    """Whether a parameter or a local between ``call`` and the file's top
    level declares ``name``, so the call is not to the imported function."""
    node = call.parent
    while node is not None:
        if node.type == "program" and stop_at_program:
            return False
        if node.type in _BOUNDARIES or node.type == "program":
            if _declares(node, name):
                return True
        node = node.parent
    return False


def comments_at_calls(
    source: str,
    grammar: str,
    function: str | Collection[str],
    path: str = "",
    *,
    defined: Collection[str] | None = None,
    importer: str | None = None,
    roots: Collection[str] = (),
) -> list[CallSiteComment]:
    """The reportable comments at the calls of ``function`` (one name or
    several) in ``source``, which is parsed once. With ``defined`` (the paths of
    the files that declare the function) the source is a file of another module:
    a call counts only where it imports the function from one of them, by its
    own name or an alias, or reads it off a namespace import, and not where a
    parameter or a local of the same name stands in for it. Without it the
    source is the defining file itself."""
    wanted = {function} if isinstance(function, str) else set(function)
    source = _strip_bom(source)
    wanted = {name for name in wanted if name in source}
    if not wanted:
        return []
    data = source.encode("utf-8")
    root = unused._parse(source, grammar)
    lines = data.split(b"\n")
    imported = defined is not None
    local, spaces = _imports(root, importer or path, defined, roots) if defined is not None else ({}, set())
    if imported and not local and not spaces:
        return []
    by_start: dict[int, list[Node]] = {}
    by_end: dict[int, list[Node]] = {}
    for node in _walk(root):
        if "comment" in node.type:
            by_start.setdefault(node.start_point[0], []).append(node)
            by_end.setdefault(node.end_point[0], []).append(node)

    def own_line(comment: Node) -> bool:
        return not lines[comment.start_point[0]][: comment.start_point[1]].strip()

    found: list[CallSiteComment] = []
    seen: set[int] = set()
    facts = flag_bindings(root)
    for call in _walk(root):
        if call.type != "call_expression":
            continue
        callee = call.child_by_field_name("function")
        if callee is None:
            continue
        if callee.type == "member_expression":
            holder = callee.child_by_field_name("object")
            callee = callee.child_by_field_name("property")
            if holder is None or holder.type != "identifier" or holder.text.decode() not in spaces:
                continue  # a method call: the receiver is not known to be the module
            name = callee.text.decode() if callee is not None else ""
        elif callee.type == "identifier":
            spelled = callee.text.decode()
            name = local.get(spelled, spelled) if imported else spelled
            if imported and spelled not in local:
                continue
            if _shadowed(call, spelled, stop_at_program=not imported):
                continue
        else:
            continue
        if name not in wanted:
            continue
        statement = _statement(call)
        end_row = call.end_point[0]
        picked = [
            c for c in by_start.get(end_row, ())
            if c.start_byte >= call.end_byte and not own_line(c)
        ]
        # The own-line comment directly above the statement, and the run of
        # them stacked above that.
        row = statement.start_point[0]
        while True:
            above = [c for c in by_end.get(row - 1, ()) if own_line(c)]
            if not above:
                break
            picked.append(above[0])
            row = above[0].start_point[0]
        text = " ".join(_comment_text(c) for c in sorted(picked, key=lambda c: c.start_byte))
        key = min((c.start_point[0] for c in picked), default=-1)
        if picked and _MENTIONS.search(text) and key not in seen:
            seen.add(key)
            found.append(CallSiteComment(path, key + 1, text, name, args_ok=arguments_hand_over_no_flag(call, facts)))
    return found


def report(
    changed: Iterable[tuple[str, str, str, str]],
    files: Callable[[], Iterable[tuple[str, str, str]]],
    names: re.Pattern[bytes],
    repo_dir: str | None = None,
) -> list[CallSiteComment]:
    """The reportable call-site comments for every function a changed file lost
    the read in. ``changed`` is ``(path, before, after, grammar)`` for each
    changed file; ``files`` yields ``(path, text, grammar)`` for every file that
    may call them, read as it stands after the run, one at a time."""
    roots = (
        (str(Path(repo_dir).resolve()), str(Path(repo_dir).resolve() / "src")) if repo_dir is not None else ()
    )
    lost: dict[str, set[str]] = {}
    clean: dict[str, bool] = {}
    for path, before, after, grammar in changed:
        lost_here = functions_that_lost_the_read(before, after, grammar, names)
        if not lost_here:
            continue
        bodies = dict(_plain_functions(unused._parse(_strip_bom(after), grammar)))
        loads_nothing = repo_dir is not None and _loads_no_flag_client(path, after, repo_dir)
        for name in lost_here:
            lost.setdefault(name, set()).add(str(Path(path).resolve()))
            clean[name] = (
                clean.get(name, True)
                and loads_nothing
                and name in bodies
                and reads_no_flag(bodies[name])
                and params_cannot_carry_a_flag(bodies[name])
            )
    if not lost:
        return []
    found: list[CallSiteComment] = []
    for path, text, grammar in files():
        resolved = str(Path(path).resolve())
        here = {name for name, defined in lost.items() if resolved in defined}
        elsewhere = {name: defined for name, defined in lost.items() if resolved not in defined}
        if here:
            found += comments_at_calls(text, grammar, here, path)
        for name, defined in elsewhere.items():
            found += comments_at_calls(text, grammar, name, path, defined=defined, importer=resolved, roots=roots)
    return [replace(c, clean=clean.get(c.function, False)) for c in found]


__all__ = [
    "CALL_SITE_KIND",
    "CallSiteComment",
    "comments_at_calls",
    "functions_that_lost_the_read",
    "reads_no_flag",
    "report",
]
