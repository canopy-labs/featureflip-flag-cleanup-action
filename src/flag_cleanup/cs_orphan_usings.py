"""Delete a C# ``using`` directive that this run's own type deletions orphaned.

A plain ``using N;`` binds no name of its own, so whether it is still needed is
the question "does any name in this file resolve through ``N``?", which in
general needs the compiler's view of every assembly the project references.
This pass answers a much narrower question, and only where its answer can be
read off the checkout:

* the run deleted a type declared directly in ``N`` (the type is gone from
  every file of the checkout), and the file, before the run, spelled that
  type's name: the run took away a use the directive was there for;
* a ``using`` directive imports the types declared directly in a namespace
  and, through its static classes, their extension members. Nothing else:
  not a nested namespace, not a nested type. So when every type the checkout
  still declares in ``N`` is known, none of them declares an extension
  member, and the file spells none of their names (in code, in a ``cref``,
  in a comment, as an attribute without its ``Attribute`` suffix), nothing
  in the file can bind through the directive, and deleting it changes
  nothing the compiler sees. (Deleting a ``using`` can never make a name
  ambiguous, or bind it elsewhere: two directives that both supplied it
  would already be an ambiguity error.)

What each condition rules out, and what is refused instead:

* a type of ``N`` the index could not see: a file that does not parse,
  cannot be read, or declares a type inside ``#if``, and spells the
  namespace's last segment keeps every directive of ``N``;
* an extension method, or a C# 14 extension block, both only declarable in a
  static class: a static class in ``N`` with a parameter marked ``this`` or
  an ``extension`` block keeps the directive, whether the file calls
  anything or not, since a call never spells the class's name;
* a type a source generator adds to ``N``: generators key on a ``partial``
  declaration or an attribute, so any ``partial`` or attributed type in ``N``
  keeps the directive;
* the name spelled by an escape sequence (``\\u0041``): the file's words cannot
  be read, and the directive stays;
* a namespace that is not the repository's own: only one that is, or sits
  under, a project's root namespace (``<RootNamespace>``, else
  ``<AssemblyName>``, else the project file's name) is judged, and never
  ``System`` or ``Microsoft`` or one under them, where packages declare;
* a type a Razor component or page compiles to: no ``.cs`` file declares
  it, so a checkout with a ``.razor`` or ``.cshtml`` file is left alone;
* ``global using``, ``using static``, an alias, a directive inside a namespace
  declaration or a ``#region``, and any file with a conditional-compilation
  directive: never touched. A directive directly under a comment goes only
  when a directive that stays follows it.

What it cannot see is a type that a compiled assembly the project references
(a package), or a source generator keyed on something outside ``N``, declares
inside the checkout's own namespace. The removal of a namespace the run
empties has the same limit; the README states it.

Placement: Action (R3). The membership proof is the checkout-wide C# type index
(:mod:`flag_cleanup.cross_file_csharp`), which the engine does not have, and
the deletion is a whole line this pass writes itself.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_csharp as xcs
from flag_cleanup import file_deletion, git_ops, source_index
from flag_cleanup.unused import _parse

#: The binding kind of each row: a deleted import, reported with the others.
KIND = "import"

_CONDITIONAL = re.compile(rb"^[ \t]*#[ \t]*(?:if|elif|else|endif|define|undef)\b", re.MULTILINE)
_NAME = re.compile(r"\w+")
_BOM = "﻿"
_ATTRIBUTE = "Attribute"
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
#: Namespaces never judged the repository's own, whatever a project's root
#: namespace says.
_PLATFORM = ("System", "Microsoft")
_REGION = re.compile(rb"^[ \t]*#[ \t]*region\b", re.MULTILINE)
_ENDREGION = re.compile(rb"^[ \t]*#[ \t]*endregion\b", re.MULTILINE)
#: Razor components and pages: each compiles to a type no ``.cs`` file spells.
_RAZOR = (".razor", ".cshtml")


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _plain_name(name: str) -> str | None:
    """``name`` as the file's words spell it: no verbatim ``@``, no formatting
    characters; ``None`` when it is anything but word characters (an escape)."""
    name = name.removeprefix("@")
    if not name.isascii():
        name = "".join(c for c in name if unicodedata.category(c) != "Cf")
    return name if _NAME.fullmatch(name) else None


def _top_level_types(source: str) -> set[tuple[str, str]] | None:
    """``(namespace, name)`` for every type declared outside any other type;
    ``None`` when the file does not parse whole."""
    root = _parse(source.removeprefix(_BOM), "csharp")
    if root.has_error:
        return None
    return {(ns, chain) for ns, chain, _ in xcs._types(root) if "." not in chain}


@dataclass(frozen=True)
class _Checkout:
    """What every file's judgement reads from the checkout, read once."""

    index: object
    #: The root namespace of each project in the checkout: a namespace that
    #: is one, or sits under one, is the repository's own.
    roots: frozenset[str]


def _project_roots(repo_dir: str) -> frozenset[str]:
    """Each ``.csproj``'s ``<RootNamespace>``, else its ``<AssemblyName>``,
    else its file name without the suffix. A value spelled with an MSBuild
    property (``$(…)``) cannot be read, and that project contributes none."""
    roots: set[str] = set()
    for source in source_index.files(repo_dir, {".csproj"}, with_ignored=True):
        if not source.path.name.endswith(".csproj"):
            continue
        try:
            text = source.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for tag in ("RootNamespace", "AssemblyName"):
            values = [v.strip() for v in re.findall(rf"<{tag}\b[^>]*>([^<]*)</{tag}\s*>", text)]
            if values:
                roots.update(v for v in values if _DOTTED.fullmatch(v))
                break
        else:
            stem = source.path.name.removesuffix(".csproj")
            if _DOTTED.fullmatch(stem):
                roots.add(stem)
    return frozenset(roots)


def _owned(namespace: str, roots: frozenset[str]) -> bool:
    """Whether ``namespace`` is the repository's own: a project's root
    namespace or one under it, and never the platform's (``System``,
    ``Microsoft``), into which packages declare whatever they like."""
    if any(namespace == r or namespace.startswith(r + ".") for r in _PLATFORM):
        return False
    return any(namespace == r or namespace.startswith(r + ".") for r in roots)


def _deleted_types(summaries: list, deleted: list, index) -> set[tuple[str, str]] | None:
    """``(namespace, name)`` of every top-level type a file of this run
    declared before it and no file of the checkout declares now; ``None`` when
    a file of the run did not parse before it."""
    gone: set[tuple[str, str]] = set()
    for summary in [*summaries, *deleted]:
        if not summary.path.endswith(".cs") or summary.original_content == summary.content:
            continue
        before = _top_level_types(summary.original_content)
        if before is None:
            return None
        for namespace, name in before:
            full = f"{namespace}.{name}" if namespace else name
            if not index.by_full.get(full):
                gone.add((namespace, name))
    return gone


def _members(namespace: str, index) -> frozenset[str] | None:
    """The simple names of the types the checkout declares directly in
    ``namespace``; ``None`` when one may declare an extension member, a source
    generator may add to it, or a file the index did not read whole (one
    that does not parse, or declares a type inside ``#if``) may declare into
    it."""
    if index.spelled_where_unparsed(namespace.rsplit(".", 1)[-1]):
        return None
    names: set[str] = set()
    for (declared_in, chain), infos in index.by_qualified.items():
        if declared_in != namespace:
            continue
        for info in infos:
            if info.extensions or info.partial or info.attributed:
                return None
        name = _plain_name(chain.split(".", 1)[0])
        if name is None:
            return None
        names.add(name)
    return frozenset(names)


def _target(directive: Node) -> str | None:
    """The namespace a plain file-level ``using N;`` imports, else None."""
    if directive.child_by_field_name("name") is not None:
        return None
    if any(c.type in ("global", "static") for c in directive.children):
        return None
    names = [c for c in directive.named_children if c.type != "comment"]
    if len(names) != 1:
        return None
    return xcs._dotted(names[0])


def _spelled(data: bytes, start: int, end: int) -> frozenset[str] | None:
    """Every word of the file outside ``[start, end)``; ``None`` when an
    escape sequence may spell any."""
    text = (data[:start] + b" " + data[end:]).decode("utf-8")
    return xcs._spelled_words(text)


def _spells_one(words: frozenset[str], names: frozenset[str]) -> bool:
    return any(
        name in words
        or (name.endswith(_ATTRIBUTE) and len(name) > len(_ATTRIBUTE) and name[: -len(_ATTRIBUTE)] in words)
        for name in names
    )


def _line_span(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """The whole line(s) the directive stands on, line break included; None
    when anything else shares them."""
    line_start = data.rfind(b"\n", 0, start) + 1
    if data[line_start:start].strip():
        return None
    newline = data.find(b"\n", end)
    line_end = len(data) if newline == -1 else newline + 1
    if data[end:line_end].strip():
        return None
    return line_start, line_end


def _under_comment(data: bytes, line_start: int) -> bool:
    """Whether the line above ``line_start`` is a comment: it may be about
    the directive below it."""
    if not line_start:
        return False
    above = data[data.rfind(b"\n", 0, line_start - 1) + 1 : line_start].strip()
    return above.startswith((b"//", b"/*", b"*")) or above.endswith(b"*/")


def _in_region(data: bytes, offset: int) -> bool:
    """Whether ``offset`` lies inside a ``#region``, which deleting the
    directive could leave empty."""
    head = data[:offset]
    return len(_REGION.findall(head)) > len(_ENDREGION.findall(head))


def _spelling(directive: Node) -> str:
    """The directive as one line: whitespace collapsed, none around ``.``."""
    return re.sub(r"\s*(\.|::)\s*", r"\1", " ".join(_text(directive).split()))


def _cut(data: bytes, line_start: int, line_end: int) -> bytes:
    """``data`` without the line, and without the blank line after it when
    that would be left first in the file or after another blank line."""
    data = data[:line_start] + data[line_end:]
    newline = data.find(b"\n", line_start)
    following = data[line_start : len(data) if newline == -1 else newline + 1]
    if newline == -1 or following.strip():
        return data
    previous_blank = line_start == 0 or not data[data.rfind(b"\n", 0, line_start - 1) + 1 : line_start].strip()
    return data[:line_start] + data[newline + 1 :] if previous_blank else data


def _orphaned(
    original: str, current: str, gone: set[tuple[str, str]], checkout: _Checkout
) -> tuple[bytes, list[str]] | None:
    """The file's new bytes (after its byte-order mark) and the directives
    deleted, or None when there is nothing to delete."""
    data = current.removeprefix(_BOM).encode("utf-8")
    if _CONDITIONAL.search(data):
        return None
    root = _parse(data.decode("utf-8"), "csharp")
    if root.has_error:
        return None
    before = xcs._spelled_words(original.removeprefix(_BOM))
    if before is None:
        return None
    #: Line start -> line end of every file-level directive.
    lines: dict[int, int] = {}
    candidates: list[tuple[int, int, str]] = []
    for directive in root.named_children:
        if directive.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
            break
        if directive.type != "using_directive":
            continue
        span = _line_span(data, directive.start_byte, directive.end_byte)
        if span is not None:
            lines[span[0]] = span[1]
        namespace = _target(directive)
        if namespace is None or span is None or not _owned(namespace, checkout.roots):
            continue
        orphans = {name for ns, name in gone if ns == namespace and _plain_name(name) in before}
        if not orphans or _in_region(data, directive.start_byte):
            continue
        members = _members(namespace, checkout.index)
        if members is None:
            continue
        words = _spelled(data, directive.start_byte, directive.end_byte)
        if words is None or _spells_one(words, members):
            continue
        candidates.append((*span, _spelling(directive)))
    # A directive under a comment goes only when a directive that stays
    # follows it, making the comment the block's. Decided from the bottom
    # up, so the line below is already known to stay or go.
    cuts: list[tuple[int, int, str]] = []
    going: set[int] = set()
    for line_start, line_end, text in sorted(candidates, reverse=True):
        if _under_comment(data, line_start) and (line_end not in lines or line_end in going):
            continue
        cuts.append((line_start, line_end, text))
        going.add(line_start)
    if not cuts:
        return None
    for line_start, line_end, _ in cuts:
        data = _cut(data, line_start, line_end)
    after = _parse(data.decode("utf-8"), "csharp")
    if after.has_error:
        return None
    return data, [text for _, _, text in sorted(cuts)]


def remove(
    summaries: list, deleted: list, repo_dir: str, rewrite: Callable
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """Delete each ``using`` directive the run's type deletions orphaned in
    the files it changed. ``deleted`` holds the summaries of the files the
    run deleted, whose declarations count among what it took away. Returns
    the summaries and a ``(path, "import", directive)`` row for each."""
    if not any(s.path.endswith(".cs") and s.original_content != s.content for s in [*summaries, *deleted]):
        return summaries, ()
    index = xcs.csharp_index(repo_dir)
    gone = _deleted_types(summaries, deleted, index)
    if not gone:
        return summaries, ()
    # A Razor component or page compiles to a type in a namespace that no
    # `.cs` file declares it in (the folder's, or an `@namespace`).
    if any(f.path.name.endswith(_RAZOR) for f in source_index.files(repo_dir, set(_RAZOR), with_ignored=True)):
        return summaries, ()
    checkout = _Checkout(index, _project_roots(repo_dir))
    out: list = []
    rows: list[tuple[str, str, str]] = []
    for summary in summaries:
        if (
            not summary.path.endswith(".cs")
            or file_deletion.deleted(summary)
            or summary.original_content == summary.content
        ):
            out.append(summary)
            continue
        on_disk = Path(summary.path).read_bytes()
        if on_disk != summary.content.encode("utf-8"):
            out.append(summary)
            continue
        found = _orphaned(summary.original_content, summary.content, gone, checkout)
        if found is None:
            out.append(summary)
            continue
        data, directives = found
        bom = _BOM if summary.content.startswith(_BOM) else ""
        text = bom + data.decode("utf-8")
        git_ops.before_writing([summary.path])
        Path(summary.path).write_bytes(text.encode("utf-8"))
        out.append(rewrite(summary.path, summary.original_content, text))
        relative = os.path.relpath(summary.path, repo_dir)
        rows.extend((relative, KIND, directive) for directive in directives)
    return out, tuple(rows)
