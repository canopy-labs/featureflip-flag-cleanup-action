"""A Go function the fold emptied, and the calls to it.

``func offerSessionEvents(w http.ResponseWriter, org string) {}`` is what a
removal leaves when the flag served false and the function's whole body was the
gated branch. Every call to it is a statement that does nothing, so the
function goes and so do its calls, when that can be PROVEN to change no
behaviour and to leave the package building:

* the function was emptied by this run (its body held something before);
* it is a package-level function with an UNEXPORTED name (no other package can
  call it), no result, and no type parameters, and not ``main`` or ``init``;
* the body is empty, comments included;
* at least one call exists, and every mention of the name in its package (the same directory and package
  clause, test files included) is the callee of a call that is a statement of
  its own, whose arguments are names, member reads and literals: no ``go``,
  no ``defer``, no value use (``f := offer``), no call or receive among the
  arguments whose effect would go with the statement, and no local that shadows
  the name;
* every file of the package parses.

Anything else leaves the function and its calls exactly as the fold left them.
An exported function is left alone: its callers outside the repository cannot
be seen. What the deleted calls strand (a local, an import) is offered to the
stranded-binding pass again by the caller.
"""

from __future__ import annotations

import os
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import go_module, go_scope, unused
from flag_cleanup.syntax import _parse, _walk

FUNCTION = "emptied function"
CALL = "call of an emptied function"

_NEVER = frozenset({"main", "init", "_"})
_PLAIN_LITERALS = frozenset(
    {"true", "false", "nil", "interpreted_string_literal", "raw_string_literal", "int_literal", "float_literal", "rune_literal", "iota"}
)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _plain(node: Node) -> bool:
    if node.type == "identifier" or node.type in _PLAIN_LITERALS:
        return True
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        return operand is not None and _plain(operand)
    if node.type == "parenthesized_expression":
        return all(_plain(c) for c in node.named_children)
    return False


def _functions(root: Node) -> dict[str, Node]:
    return {
        _text(name): node
        for node in root.named_children
        if node.type == "function_declaration" and (name := node.child_by_field_name("name")) is not None
    }


def _empty(function: Node) -> bool:
    body = function.child_by_field_name("body")
    return body is not None and not body.named_children


def _candidate(function: Node) -> bool:
    name = _text(function.child_by_field_name("name"))
    return (
        name not in _NEVER
        and name[:1].islower()
        and function.child_by_field_name("result") is None
        and function.child_by_field_name("type_parameters") is None
        and _empty(function)
    )


def _call_statement(identifier: Node) -> Node | None:
    """The expression statement ``identifier(…)`` is the whole of, when it is
    a plain call that can be deleted without dropping an effect."""
    call = identifier.parent
    if call is None or call.type != "call_expression":
        return None
    function = call.child_by_field_name("function")
    if function is None or function.id != identifier.id or call.child_by_field_name("type_arguments") is not None:
        return None
    statement = call.parent
    if statement is None or statement.type != "expression_statement" or len(statement.named_children) != 1:
        return None
    if statement.parent is None or statement.parent.type not in ("statement_list", "block"):
        return None
    arguments = call.child_by_field_name("arguments")
    if arguments is None or not all(_plain(a) for a in arguments.named_children):
        return None
    return statement


def _package_roots(directory: Path, clause: str, contents: dict[Path, str]) -> dict[Path, tuple[str, Node]] | None:
    """Parsed files of the package, ``None`` when one does not parse."""
    found: dict[Path, tuple[str, Node]] = {}
    for path in sorted(directory.glob("*.go")):
        resolved = path.resolve()
        try:
            source = contents[resolved] if resolved in contents else path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        root = _parse(source, "go")
        file_clause = go_module.package_clause(root)
        if root.has_error:
            # An unreadable file of another package cannot name this one's
            # unexported function; one of this package could.
            if file_clause is None or file_clause == clause:
                return None
            continue
        if file_clause == clause:
            found[resolved] = (source, root)
    return found


def remove(summaries, repo_dir: str, rewrite) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """Delete each function this run emptied, with its calls; report what went.

    ``summaries`` are the run's per-file results; a caller in a file the run
    did not otherwise change gets a summary of its own."""
    live = {
        Path(s.path).resolve(): s
        for s in summaries
        if str(s.path).endswith(".go") and not _deleted(s)
    }
    contents: dict[Path, str] = {p: s.content for p, s in live.items()}
    originals: dict[Path, str] = {p: s.original_content for p, s in live.items()}
    rows: list[tuple[str, str, str]] = []
    edited: dict[Path, str] = {}
    seen: set[tuple[Path, str]] = set()
    for path, summary in list(live.items()):
        if summary.original_content == summary.content:
            continue
        after = _parse(summary.content, "go")
        before = _parse(summary.original_content, "go")
        clause = go_module.package_clause(after)
        if after.has_error or before.has_error or clause is None:
            continue
        earlier = _functions(before)
        for name, function in _functions(after).items():
            if not _candidate(function) or (path.parent, name) in seen:
                continue
            old = earlier.get(name)
            if old is None or _empty(old):
                continue
            seen.add((path.parent, name))
            plan = _plan(path.parent, clause, name, contents)
            if plan is None:
                continue
            for target, spans in plan.items():
                text = contents[target] if target in contents else target.read_text(encoding="utf-8")
                for start, end, kind, shown in sorted(spans, key=lambda s: -s[0]):
                    text = unused._delete_method(text, unused.Candidate(shown, start, end, kind, shown))
                contents[target] = text
                edited[target] = text
                relative = os.path.relpath(target, repo_dir)
                rows.extend((relative, kind, shown) for _, _, kind, shown in sorted(spans))
    if not edited:
        return list(summaries), ()
    result = []
    done: set[Path] = set()
    for s in summaries:
        key = Path(s.path).resolve()
        if key in edited:
            Path(s.path).write_text(edited[key], encoding="utf-8")
            result.append(rewrite(s.path, s.original_content, edited[key]))
            done.add(key)
        else:
            result.append(s)
    for key, text in edited.items():
        if key in done:
            continue
        before = originals.get(key)
        if before is None:
            before = key.read_text(encoding="utf-8")
        key.write_text(text, encoding="utf-8")
        result.append(rewrite(str(key), before, text))
    return result, tuple(rows)


def _deleted(summary) -> bool:
    from flag_cleanup import file_deletion

    return file_deletion.deleted(summary)


def _plan(
    directory: Path, clause: str, name: str, contents: dict[Path, str]
) -> dict[Path, list[tuple[int, int, str, str]]] | None:
    """Per file, the ``(start, end, kind, text)`` spans to delete for ``name``,
    or ``None`` when anything about it is not proven."""
    package = _package_roots(directory, clause, contents)
    if package is None:
        return None
    plan: dict[Path, list[tuple[int, int, str, str]]] = {}
    declarations = 0
    for path, (source, root) in package.items():
        data = source.encode("utf-8")
        for node in _walk(root):
            if node.type != "identifier" or _text(node) != name:
                continue
            parent = node.parent
            if parent is not None and parent.type == "function_declaration" and parent.child_by_field_name("name") is not None and parent.child_by_field_name("name").id == node.id:
                declarations += 1
                comment_start = parent.start_byte
                previous = parent.prev_sibling
                while (
                    previous is not None
                    and "comment" in previous.type
                    and data[previous.end_byte : comment_start].count(b"\n") == 1
                    and not data[previous.end_byte : comment_start].strip()
                ):
                    comment_start = previous.start_byte
                    previous = previous.prev_sibling
                plan.setdefault(path, []).append(
                    (comment_start, parent.end_byte, FUNCTION, " ".join(_text(parent).split()))
                )
                continue
            if go_scope.local_binders(node, name):
                return None
            statement = _call_statement(node)
            if statement is None:
                return None
            plan.setdefault(path, []).append(
                (statement.start_byte, statement.end_byte, CALL, " ".join(_text(statement).split()))
            )
    if declarations != 1:
        return None
    # A function nothing calls is not this pass's: it may be an entry point,
    # a fixture or a test helper, and deleting it is not what the fold did.
    if not any(kind == CALL for spans in plan.values() for _, _, kind, _ in spans):
        return None
    return plan
