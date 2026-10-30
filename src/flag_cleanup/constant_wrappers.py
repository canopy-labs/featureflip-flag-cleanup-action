"""Members the fold reduced to a constant, and the callers it cannot reach.

A team that wraps its flag reads behind a gate — an interface with an
``IsEnabled`` method, implemented once against the SDK and once against local
config — gets a removal that stops at the wrapper. The rules rewrite the read
inside the implementation, so ``return client.BoolVariation("key", ...)``
becomes ``return true``, and there the rewrite ends: every caller still
branches on ``gate.IsEnabled()``, and none of them names the key, so nothing
can match them. The pull request is not wrong, it is half a removal, and the
half it leaves is the half a reviewer needs to hear about.

This module finds that case and reports it. It does NOT inline the constant
into the callers, and that is a decision rather than a gap:

* **The call carries no key.** The only thing tying ``gate.IsEnabled()`` to the
  flag is the receiver's TYPE, which tree-sitter does not know. Matching by
  member name instead folds every gate in a codebase where they all expose the
  same ``IsEnabled`` — which is what a gate family looks like.
* **A sibling implementation is a behaviour the transform would override.** The
  config-backed gate beside the SDK one keeps returning whatever its setting
  says. Inlining ``true`` at a call through the interface changes what that
  path does, and no amount of parsing can prove that the customer wants it.

So the report names everything a reviewer needs to finish the job by hand —
the member, the constant it returns, the types it was declared against, the
other implementations of those types and the files that call it — and changes
nothing.

**Scoped to the diff.** A member is reported only when the diff changed a line
inside it and it now returns one boolean literal on every path. A predicate
that already returned a constant before this run, and that the transform
never touched, is not this flag's business and is not reported.

**C# and Java only.** Those are the languages where the shape is idiomatic and
where a declaration names the types it implements, which is what makes the
siblings and callers findable. A free function in the other languages has no
declared type to search by, and reporting it with no callers would read as
"nothing calls this", which would be false.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import report_paths, syntax
from flag_cleanup.piranha_runner import _SKIP_DIRS, source_files

# Pruned from the sibling and caller scan for the reason the residual scan
# prunes it: a build directory holds a copy of the source, and naming it as a
# caller sends the reviewer to a file their build overwrites.
from flag_cleanup.pr_content import _GENERATED_DIRS

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class _Dialect:
    language: str
    extensions: tuple[str, ...]
    #: Declarations whose body is a type's member list.
    types: frozenset[str]
    #: Nodes that start a new function scope. A ``return`` inside one belongs
    #: to that function, not to the member being examined.
    scopes: frozenset[str]
    #: Node types that are the boolean literals ``true``/``false``.
    literals: frozenset[str]


_CSHARP = _Dialect(
    language="csharp",
    extensions=(".cs",),
    types=frozenset(
        {"class_declaration", "struct_declaration", "record_declaration", "interface_declaration"}
    ),
    scopes=frozenset(
        {"lambda_expression", "anonymous_method_expression", "local_function_statement"}
    ),
    literals=frozenset({"boolean_literal"}),
)

_JAVA = _Dialect(
    language="java",
    extensions=(".java",),
    types=frozenset(
        {"class_declaration", "record_declaration", "enum_declaration", "interface_declaration"}
    ),
    scopes=frozenset(
        {"lambda_expression", "class_body", "class_declaration", "record_declaration"}
    ),
    literals=frozenset({"true", "false"}),
)

_DIALECTS = {ext: dialect for dialect in (_CSHARP, _JAVA) for ext in dialect.extensions}


@dataclass(frozen=True)
class ConstantWrapper:
    """One member the fold reduced to a constant, and what still depends on it."""

    path: str
    #: ``Type.Member``, as a reviewer would search for it.
    member: str
    value: bool
    #: The types the enclosing declaration names as its bases or interfaces.
    types: tuple[str, ...] = ()
    #: ``(Type, path)`` for every other declaration of one of ``types`` that
    #: declares a member of the same name. Untouched by this run.
    siblings: tuple[tuple[str, str], ...] = ()
    #: Files that call the member through ``types`` or the enclosing type.
    callers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Found:
    type_name: str
    member: str
    value: bool
    types: tuple[str, ...]


def find_constant_wrappers(
    diff: str, directories: Iterable[str]
) -> tuple[ConstantWrapper, ...]:
    """Every member ``diff`` reduced to a constant, with its siblings and callers.

    Reads the REWRITTEN files, so both callers run it inside the transform
    window, next to the residual scan and for the same reason: once the window
    closes the edits are rolled back and every member reads as it did before.
    """
    directories = tuple(directories)
    found: list[tuple[str, _Dialect, _Found]] = []
    for rel, changed in _changed_lines(diff).items():
        dialect = _DIALECTS.get(os.path.splitext(rel)[1])
        if dialect is None:
            continue
        path = _resolve(rel, directories)
        if path is None:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for member in _constant_members(source, dialect, changed):
            found.append((str(path), dialect, member))

    if not found:
        return ()

    corpus = _Corpus(directories)
    wrappers = []
    for path, dialect, member in found:
        wrappers.append(
            ConstantWrapper(
                path=path,
                member=f"{member.type_name}.{member.member}",
                value=member.value,
                types=member.types,
                siblings=_siblings(corpus, dialect, path, member),
                callers=_callers(corpus, dialect, path, member),
            )
        )
    return tuple(wrappers)


def note_constant_wrappers(wrappers: tuple[ConstantWrapper, ...]) -> None:
    """Log what the pull-request body says about ``wrappers``.

    For ``--dry-run``, which opens no pull request: without this the one mode
    meant to preview a real run is silent about the residue a real run reports.
    WARNING, like the residual scan's: the flag still decides what callers do.
    """
    for wrapper in wrappers:
        logger.warning(
            "%s in %s now always returns %s, but its callers still branch on "
            "it: remove them and the member by hand%s",
            wrapper.member,
            wrapper.path,
            "true" if wrapper.value else "false",
            f" (called from {', '.join(wrapper.callers)})" if wrapper.callers else "",
        )


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class _Changed:
    #: New-file line numbers (1-based) of every added line.
    added: set[int]
    #: For every deleted run, the new-file line it sat in front of.
    deleted: set[int]


def _changed_lines(diff: str) -> dict[str, _Changed]:
    """Which lines of each rewritten file the diff touched, by NEW line number.

    Context lines are skipped on purpose: a hunk carries three of them either
    side, so a member that merely sits next to a fold would otherwise read as
    changed by it.

    Hunk bodies are consumed by the counts in their ``@@`` header, never by
    sniffing for ``---``/``+++``: a deleted line that itself began ``-- `` is
    written ``--- `` in the diff, and reading it as a file header would
    attribute every later line to a file that does not exist.
    """
    files: dict[str, _Changed] = {}
    current: _Changed | None = None
    line = old_left = new_left = 0
    for text in diff.splitlines():
        if old_left or new_left:
            marker = text[:1]
            if marker == "+":
                current.added.add(line)
                line += 1
                new_left -= 1
            elif marker == "-":
                current.deleted.add(line)
                old_left -= 1
            elif marker == " ":
                line += 1
                old_left -= 1
                new_left -= 1
            # `\ No newline at end of file` belongs to the hunk and counts
            # against neither side.
            continue
        if text.startswith("+++ "):
            name = text[4:].strip()
            name = name[2:] if name.startswith("b/") else name
            current = files.setdefault(name, _Changed(set(), set()))
            continue
        match = _HUNK.match(text)
        if match and current is not None:
            old_left = int(match.group(1) or 1)
            line = int(match.group(2))
            new_left = int(match.group(3) or 1)
    return files


def _resolve(rel: str, directories: tuple[str, ...]) -> Path | None:
    """The file a diff path names, under whichever configured directory holds it.

    Diff paths are relative to the repository root, and the file is returned
    spelled under the first configured directory holding it, the way the
    corpus spells every other file. Outside a checkout they are relative to
    the directory the transform ran in (its parent, for a directory entry
    naming a single file), so with several directories configured the first
    one holding the file is the one that produced it.
    """
    root = report_paths.repository_root(directories[0]) if directories else None
    if root is not None:
        target = os.path.join(root, rel)
        if not os.path.isfile(target):
            return None
        for directory in directories:
            base = Path(directory)
            base = base.parent if base.is_file() else base
            inside = report_paths.shown(target, os.path.abspath(base))
            if not os.path.isabs(inside):
                return Path(os.path.normpath(os.path.join(base, inside)))
        return None
    for directory in directories:
        root = Path(directory)
        root = root.parent if root.is_file() else root
        candidate = root / rel
        if candidate.is_file():
            return Path(os.path.normpath(candidate))
    return None


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def _constant_members(
    source: str, dialect: _Dialect, changed: _Changed
) -> Iterator[_Found]:
    root = syntax._parse(source, dialect.language)
    # `_walk` pops siblings off a stack, so it visits them last-first; sorted,
    # the body lists members in the order the reviewer reads the file.
    for node in sorted(syntax._walk(root), key=lambda n: n.start_byte):
        body = _predicate_body(node, dialect)
        if body is None or not _touched(node, changed):
            continue
        value = _constant_value(body, dialect)
        if value is None:
            continue
        owner = _enclosing_type(node, dialect)
        if owner is None:
            continue
        name = node.child_by_field_name("name")
        types = _base_types(owner, dialect.language)
        explicit = _explicit_interface(node)
        if explicit and explicit not in types:
            types = (*types, explicit)
        yield _Found(
            type_name=_text(owner.child_by_field_name("name")),
            member=_text(name),
            value=value,
            types=types,
        )


def _predicate_body(node: Node, dialect: _Dialect) -> Node | None:
    """The body of a boolean-returning member, or ``None`` if ``node`` is not one."""
    if dialect is _CSHARP:
        if node.type == "method_declaration":
            if _text(node.child_by_field_name("returns")) != "bool":
                return None
            return node.child_by_field_name("body")
        if node.type == "property_declaration":
            if _text(node.child_by_field_name("type")) != "bool":
                return None
            value = node.child_by_field_name("value")
            if value is not None and value.type == "arrow_expression_clause":
                return value
            accessors = node.child_by_field_name("accessors")
            for accessor in accessors.named_children if accessors else ():
                if accessor.type == "accessor_declaration" and any(
                    child.type == "get" for child in accessor.children
                ):
                    return accessor.child_by_field_name("body") or next(
                        (c for c in accessor.named_children if c.type == "arrow_expression_clause"),
                        None,
                    )
        return None
    if node.type == "method_declaration":
        if _text(node.child_by_field_name("type")) != "boolean":
            return None
        return node.child_by_field_name("body")
    return None


def _touched(member: Node, changed: _Changed) -> bool:
    """Did the diff add or delete a line INSIDE ``member``?

    A deleted run is placed in front of the new line it preceded, so a
    deletion recorded at the member's FIRST line sat above it — the member
    before was removed, not this one — and is not counted.
    """
    first = member.start_point[0] + 1
    last = member.end_point[0] + 1
    return any(first <= line <= last for line in changed.added) or any(
        first < line <= last for line in changed.deleted
    )


def _constant_value(body: Node, dialect: _Dialect) -> bool | None:
    """The one boolean literal ``body`` returns on every path, if there is one."""
    if body.type == "arrow_expression_clause":
        return _literal(_only_named_child(body), dialect)
    values = {
        _literal(_only_named_child(ret), dialect)
        for ret in _returns(body, dialect)
    }
    if len(values) != 1:
        return None
    return values.pop()


def _returns(node: Node, dialect: _Dialect) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in dialect.scopes:
            continue
        if child.type == "return_statement":
            yield child
        else:
            yield from _returns(child, dialect)


def _only_named_child(node: Node) -> Node | None:
    children = [c for c in node.named_children if c.type != "comment"]
    return children[0] if len(children) == 1 else None


def _literal(node: Node | None, dialect: _Dialect) -> bool | None:
    if node is None:
        return None
    while node.type == "parenthesized_expression" and len(node.named_children) == 1:
        node = node.named_children[0]
    if node.type not in dialect.literals:
        return None
    return _text(node) == "true"


def _enclosing_type(node: Node, dialect: _Dialect) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type in dialect.types:
            return parent
        parent = parent.parent
    return None


def _base_types(declaration: Node, language: str) -> tuple[str, ...]:
    """The simple names of the types ``declaration`` derives from or implements."""
    if language == "csharp":
        lists = [c for c in declaration.named_children if c.type == "base_list"]
    else:
        lists = [
            c
            for c in declaration.named_children
            if c.type in {"superclass", "super_interfaces", "extends_interfaces"}
        ]
    names: list[str] = []
    for base_list in lists:
        for entry in _type_entries(base_list):
            name = _simple_name(entry)
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _type_entries(node: Node) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in {"type_list", "argument_list"}:
            yield from _type_entries(child)
        elif child.type == "primary_constructor_base_type":
            yield from child.named_children[:1]
        else:
            yield child


def _simple_name(node: Node) -> str:
    """``Gate`` for ``Gate``, ``Gate<T>``, ``Ns.Gate`` and ``Ns.Gate<T>``."""
    if node.type in {"identifier", "type_identifier"}:
        return _text(node)
    if node.type in {"generic_name", "generic_type"}:
        return _simple_name(node.named_children[0])
    if node.type in {"qualified_name", "scoped_type_identifier"}:
        return _simple_name(node.named_children[-1])
    return ""


def _explicit_interface(member: Node) -> str:
    """``IGate`` for a C# explicit implementation ``bool IGate.IsEnabled()``."""
    for child in member.named_children:
        if child.type == "explicit_interface_specifier":
            return _simple_name(child.named_children[0])
    return ""


def _text(node: Node | None) -> str:
    return node.text.decode() if node is not None and node.text is not None else ""


# ---------------------------------------------------------------------------
# Siblings and callers
# ---------------------------------------------------------------------------


class _Corpus:
    """The configured source tree, read once per run and only if needed."""

    def __init__(self, directories: tuple[str, ...]) -> None:
        self._directories = directories
        self._texts: dict[tuple[str, ...], dict[str, str]] = {}

    def texts(self, dialect: _Dialect) -> dict[str, str]:
        cached = self._texts.get(dialect.extensions)
        if cached is not None:
            return cached
        texts: dict[str, str] = {}
        for directory in self._directories:
            for path in source_files(
                Path(directory), dialect.extensions, _SKIP_DIRS | _GENERATED_DIRS
            ):
                key = os.path.normpath(path)
                if key in texts:
                    continue
                try:
                    texts[key] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
        self._texts[dialect.extensions] = texts
        return texts


def _token(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _siblings(
    corpus: _Corpus, dialect: _Dialect, path: str, found: _Found
) -> tuple[tuple[str, str], ...]:
    """Other declarations of ``found.types`` that declare the same member."""
    if not found.types:
        return ()
    member = _token(found.member)
    bases = [_token(t) for t in found.types]
    siblings: list[tuple[str, str]] = []
    for other, text in sorted(corpus.texts(dialect).items()):
        if not member.search(text) or not any(b.search(text) for b in bases):
            continue
        root = syntax._parse(text, dialect.language)
        for node in syntax._walk(root):
            if node.type not in dialect.types:
                continue
            name = _text(node.child_by_field_name("name"))
            if other == os.path.normpath(path) and name == found.type_name:
                continue
            if set(_base_types(node, dialect.language)).isdisjoint(found.types):
                continue
            if _declares(node, found.member, dialect):
                siblings.append((name, other))
    return tuple(siblings)


def _declares(declaration: Node, member: str, dialect: _Dialect) -> bool:
    body = declaration.child_by_field_name("body")
    for child in body.named_children if body else ():
        if child.type in {"method_declaration", "property_declaration"} and _text(
            child.child_by_field_name("name")
        ) == member:
            return True
    return False


def _callers(
    corpus: _Corpus, dialect: _Dialect, path: str, found: _Found
) -> tuple[str, ...]:
    """Files that name one of the member's types and access ``.Member`` on something.

    Textual on purpose, and phrased in the body as what it is. Proving the
    receiver's type needs the type checker this tool does not have; the two
    tokens together are a reviewer's own grep, done for them.
    """
    access = re.compile(rf"\.\s*{re.escape(found.member)}(?![A-Za-z0-9_])")
    names = [_token(n) for n in (found.type_name, *found.types)]
    own = os.path.normpath(path)
    return tuple(
        other
        for other, text in sorted(corpus.texts(dialect).items())
        if other != own and access.search(text) and any(n.search(text) for n in names)
    )
