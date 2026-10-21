"""Remove a constructor parameter or private field the fold left unread.

The report passes (:mod:`flag_cleanup.ts_unread_members`,
:mod:`flag_cleanup.primary_constructor`) name a parameter nothing reads any
more: a TypeScript parameter property, a plain TypeScript or JavaScript
constructor parameter, or a C# primary-constructor parameter. When every caller can be proven (:mod:`flag_cleanup.ctor_param_ts`,
:mod:`flag_cleanup.ctor_param_csharp`), each caller's argument is replaced by a
sentinel identifier and the engine deletes the sentinels and the parameter.
A TypeScript or JavaScript private field (:mod:`flag_cleanup.ts_field_removal`)
is marked the same way, with the constructor statement that assigns it, and
takes the constructor parameter it was assigned from when nothing else reads
that parameter. A Dart private field (:mod:`flag_cleanup.dart_field_removal`)
is marked the same way, on its own.
Deleting by a marked identity rather than by position matters: the engine
applies a rule until it stops matching, so a positional rule would also delete
the argument that moves into the emptied slot.

A postcondition then re-reads every file. If a sentinel survived, a parameter
survived, a file stopped parsing, or a file lost a different number of
arguments than it had marks, every file this pass touched gets its bytes back
and the parameters are reported unread, exactly as they were before this pass
existed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import (
    ctor_param_csharp,
    ctor_param_ts,
    dart_field_removal,
    git_ops,
    syntax,
    ts_field_removal,
    ts_scope,
)
from flag_cleanup.ctor_param_model import (
    IDENT,
    Refusal,
    Removal,
    choose_sentinel,
    forms_for,
    mark,
    render_rules,
    tidy,
)
from flag_cleanup.kotlin_sentinel import Rewrite
from flag_cleanup.unused import _parse

logger = logging.getLogger(__name__)

_ARGUMENT_LISTS = frozenset({"arguments", "argument_list"})
#: The unread-row kinds each language's report passes write.
_TS_KINDS = frozenset({"unread member", "unread constructor parameter"})
_KINDS = {
    "ts": _TS_KINDS,
    "tsx": _TS_KINDS,
    "js": _TS_KINDS,
    "csharp": frozenset({"unread parameter"}),
    "dart": frozenset({"unread member"}),
}

Row = tuple[str, str, str]


@dataclass(frozen=True)
class Result:
    summaries: list
    #: The unread rows still unread: refused, or rolled back.
    unread: tuple[Row, ...]
    #: ``(path, "removed parameter", label)``, ``(path, "removed member", label)``
    #: and ``(path, "changed constructor", class)``.
    rows: tuple[Row, ...]
    #: The summary paths this pass rewrote, spelled as in ``summaries``.
    rewritten: frozenset[str]
    #: ``(path, name)`` for each deleted argument that was a bare name: the
    #: local it named may now be read by nothing (see :func:`kept_argument_locals`).
    argument_names: tuple[tuple[str, str], ...] = ()


def _key(path: str) -> str:
    return str(Path(path).resolve())


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _grammar(path: str, language: str) -> str:
    return language if language in ("csharp", "dart") else ts_scope.grammar(path)


def _decode(data: bytes) -> str:
    return data[3:].decode("utf-8") if data.startswith(b"\xef\xbb\xbf") else data.decode("utf-8")


def _arguments(data: bytes, grammar: str) -> int:
    """Every call argument in the file, comments aside."""
    return sum(
        len([c for c in node.named_children if c.type != "comment"])
        for node in _walk(_parse(_decode(data), grammar))
        if node.type in _ARGUMENT_LISTS
    )


def _inside(removal: Removal, repo_dir: str) -> Removal | Refusal:
    """``removal``, or a refusal when a caller lies outside ``repo_dir``.

    A run owns one directory: it is what gets restored after a dry run and
    staged for the pull request. A caller elsewhere would be rewritten and
    then left out of the commit, still passing the argument.
    """
    scope = Path(repo_dir).resolve()
    for site in removal.sites:
        path = Path(_key(site.path))
        if path != scope and scope not in path.parents:
            return Refusal(removal.label, f"{site.path} calls it from outside {repo_dir}")
    return removal


def _plan(summary, kind: str, label: str, language: str, repo_dir: str) -> Removal | Refusal:
    try:
        if language == "csharp":
            return ctor_param_csharp.plan(summary.path, summary.content, label, repo_dir)
        if language == "dart":
            return dart_field_removal.plan(summary.path, summary.content, label, repo_dir)
        if kind == "unread constructor parameter":
            return ctor_param_ts.plan_parameter(summary.path, summary.content, label, repo_dir)
        outcome = ctor_param_ts.plan(summary.path, summary.content, label, repo_dir)
        if isinstance(outcome, Refusal) and outcome.quiet:
            # Not a parameter property: a field, or a method or accessor.
            return ts_field_removal.plan(summary.path, summary.content, label, repo_dir)
        return outcome
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Refusal(label, f"could not be checked: {exc}")


def remove(
    summaries,
    language: str,
    unread: tuple[Row, ...],
    repo_dir: str,
    *,
    engine: Callable[[list[str], str], list],
) -> Result:
    """Remove every unread constructor parameter in ``unread`` whose callers
    can all be proven, and every unread TypeScript, JavaScript or Dart field that
    can be, through ``engine(paths, rules_text)``."""
    unchanged = Result(summaries, tuple(unread), (), frozenset())
    kinds = _KINDS.get(language, frozenset())
    by_path = {os.path.relpath(s.path, repo_dir): s for s in summaries}
    removals: list[tuple[Row, Removal]] = []
    for row in unread:
        path, row_kind, label = row
        summary = by_path.get(path)
        if row_kind not in kinds or summary is None:
            continue
        outcome = _plan(summary, row_kind, label, language, repo_dir)
        if isinstance(outcome, Removal):
            outcome = _inside(outcome, repo_dir)
        if isinstance(outcome, Refusal):
            log = logger.debug if outcome.quiet else logger.info
            log("keeping %s unread in %s: %s", label, path, outcome.reason)
            continue
        removals.append((row, outcome))
    if not removals:
        return unchanged

    # One file can be spelled two ways (a summary's path, and the source
    # listing's for the same file as a caller), so every map is keyed by the
    # resolved path and each file is read, marked and restored exactly once.
    spelling: dict[str, str] = {}
    for _, removal in removals:
        spelling.setdefault(_key(removal.declaring_path), removal.declaring_path)
        for site in removal.sites:
            spelling.setdefault(_key(site.path), site.path)
    for summary in summaries:
        if _key(summary.path) in spelling:
            spelling[_key(summary.path)] = summary.path
    files = sorted(spelling)
    before = {key: Path(key).read_bytes() for key in files}
    sentinel = choose_sentinel(before.values())
    if sentinel is None:
        logger.warning(
            "keeping %d unread constructor parameter(s) or field(s): no free sentinel name",
            len(removals),
        )
        return unchanged

    marks: dict[str, list[tuple[int, int, str]]] = {}
    parameters: dict[str, list[tuple[int, int]]] = {}
    for _, removal in removals:
        for site in removal.sites:
            marks.setdefault(_key(site.path), []).append((site.start, site.end, site.form))
        if removal.param_name is not None:
            key = _key(removal.declaring_path)
            parameters.setdefault(key, []).extend(_parameter_spans(before[key], removal, language))
    rules = render_rules(
        language if language in ("csharp", "dart") else "ts", [r for _, r in removals], sentinel
    )
    # The candidate scan never listed a caller: bring every file this pass
    # writes under the running transform's undo first.
    git_ops.before_writing([spelling[key] for key in files])
    try:
        marked = {
            key: mark(
                before[key], marks.get(key, ()), sentinel, parameters.get(key, ()),
                forms=forms_for(language),
            )
            for key in files
        }
        for key in files:
            if marked[key] != before[key]:
                Path(key).write_bytes(marked[key])
        engine([spelling[key] for key in files], rules)
        # The engine leaves the spaces around each gap on a single-line list.
        # A deleted line of a multi-line list leaves its `\r` behind in a CRLF
        # file (`a,\r\r\n`): the same repair every other deletion gets.
        for key in files:
            engine_out = Path(key).read_bytes()
            tidied = tidy(marked[key], engine_out)
            if b"\r" in tidied:
                tidied = syntax.collapse_introduced_carriage_returns(
                    _decode(marked[key]), _decode(tidied), _grammar(key, language)
                ).encode("utf-8")
                if engine_out.startswith(b"\xef\xbb\xbf"):
                    tidied = b"\xef\xbb\xbf" + tidied
            if tidied != engine_out:
                Path(key).write_bytes(tidied)
        failure = _postcondition(files, marked, marks, removals, sentinel, language)
    except Exception as exc:  # noqa: BLE001 - any surprise means: put the bytes back
        for key, data in before.items():
            Path(key).write_bytes(data)
        if type(exc).__name__ == "PiranhaTransformError":
            # The engine aborting on our own rules is a defect to surface,
            # not degrade: the flag is refused like any other engine abort.
            raise
        failure = f"{type(exc).__name__}: {exc}"
    if failure is not None:
        for key, data in before.items():
            Path(key).write_bytes(data)
        logger.warning(
            "keeping %d unread constructor parameter(s) or field(s): %s", len(removals), failure
        )
        return unchanged

    after = {key: Path(key).read_bytes() for key in files}
    changed = {key for key in files if after[key] != before[key]}
    result = []
    rewritten: set[str] = set()
    for summary in summaries:
        key = _key(summary.path)
        if key in changed:
            summary = Rewrite(summary.path, summary.original_content, after[key].decode("utf-8"))
            changed.discard(key)
            rewritten.add(summary.path)
        result.append(summary)
    for key in sorted(changed):  # a caller the fold never touched
        result.append(
            Rewrite(spelling[key], before[key].decode("utf-8"), after[key].decode("utf-8"))
        )
        rewritten.add(spelling[key])
    names: list[tuple[str, str]] = []
    for key, spans in marks.items():
        for start, end, form in spans:
            text = before[key][start:end].decode("utf-8", "replace")
            if form == "argument" and IDENT.fullmatch(text):
                names.append((spelling[key], text))
    done = {row for row, _ in removals}
    rows: list[Row] = []
    for row, removal in removals:
        if removal.field is None:
            rows.append((row[0], "removed parameter", removal.label))
        else:
            rows.append((row[0], "removed member", removal.label))
            if removal.param_name is not None:
                rows.append(
                    (row[0], "removed parameter", f"{removal.class_name}({removal.param_name})")
                )
        if removal.exported:
            rows.append((row[0], "changed constructor", removal.class_name))
        for path, heir in removal.heirs:
            rows.append((os.path.relpath(path, repo_dir), "changed constructor", heir))
    return Result(
        result,
        tuple(r for r in unread if r not in done),
        tuple(dict.fromkeys(rows)),
        frozenset(rewritten),
        tuple(dict.fromkeys(names)),
    )


def kept_argument_locals(
    summaries, names: tuple[tuple[str, str], ...], language: str, repo_dir: str
) -> tuple[Row, ...]:
    """``(path, "kept argument local", declarator)`` for each local that a
    deleted argument named and that nothing reads any more.

    Asked after the stranded-binding pass, which deletes such a local when
    its initializer cannot do work. One it kept (``const m = mock<Flags>()``:
    a call may) is unread now, and the pull request names it.
    """
    by_path = {s.path: s for s in summaries}
    rows: list[Row] = []
    for path, name in names:
        summary = by_path.get(path)
        if summary is None:
            continue
        root = _parse(summary.content.removeprefix("\ufeff"), _grammar(path, language))
        wanted = name.encode()
        declarators = [
            n
            for n in _walk(root)
            if n.type == "variable_declarator"
            and (bound := n.child_by_field_name("name")) is not None
            and bound.text == wanted
        ]
        if len(declarators) != 1:
            continue
        bound = declarators[0].child_by_field_name("name")
        if any(
            n.type in ("identifier", "shorthand_property_identifier") and n.text == wanted and n != bound
            for n in _walk(root)
        ):
            continue
        text = " ".join(declarators[0].text.decode("utf-8", "replace").split())
        rows.append((os.path.relpath(path, repo_dir), "kept argument local", text))
    return tuple(rows)


def _parameter_spans(data: bytes, removal: Removal, language: str) -> list[tuple[int, int]]:
    """Byte spans of ``removal``'s parameter in ``data``, byte-order mark included."""
    offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    source = _decode(data)
    if language == "csharp":
        spans = ctor_param_csharp.parameter_spans(source, removal.class_name, removal.param_name)
    else:
        spans = ctor_param_ts.parameter_spans(
            source, ts_scope.grammar(removal.declaring_path), removal.class_name, removal.param_name
        )
    return [(start + offset, end + offset) for start, end in spans]


def _postcondition(
    files: list[str],
    marked: dict[str, bytes],
    marks: dict[str, list[tuple[int, int, str]]],
    removals: list[tuple[Row, Removal]],
    sentinel: str,
    language: str,
) -> str | None:
    needle = sentinel.encode("ascii")
    for key in files:
        after = Path(key).read_bytes()
        if needle in after:
            return f"{key}: a marked argument was not deleted"
        grammar = _grammar(key, language)
        if _parse(_decode(after), grammar).has_error:
            return f"{key}: no longer parses"
        # A parameter is not an argument, so only argument marks change the
        # count. Counted on the marked text: a member mark already took the
        # arguments inside it (a Dart field's `const Duration(seconds: 3)`).
        arguments = [m for m in marks.get(key, ()) if m[2] == "argument"]
        expected = _arguments(marked[key], grammar) - len(arguments)
        if _arguments(after, grammar) != expected:
            return f"{key}: lost a different number of arguments than it had marks"
    for _, removal in removals:
        source = _decode(Path(removal.declaring_path).read_bytes())
        grammar = _grammar(removal.declaring_path, language)
        if removal.field is not None and (
            dart_field_removal.has_field(source, removal.class_name, removal.field)
            if language == "dart"
            else ts_field_removal.has_field(source, grammar, removal.class_name, removal.field)
        ):
            return f"{removal.declaring_path}: the field {removal.field} is still declared"
        if removal.param_name is None:
            continue
        if language == "csharp":
            present = ctor_param_csharp.has_parameter(
                source, removal.class_name, removal.param_name
            )
        else:
            present = ctor_param_ts.has_parameter(
                source, grammar, removal.class_name, removal.param_name
            )
        if present:
            return f"{removal.declaring_path}: the parameter {removal.param_name} is still declared"
    return None
