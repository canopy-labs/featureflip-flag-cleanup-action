"""Delete the Java and Kotlin test classes a removal left with no test.

A removal deletes a test that stubs the removed value. When that was a class's
last test, what stays is its mock fields and its setup, which test nothing, and
a ``@Nested`` class with nothing in it at all. Such a class goes, and with it
its file when nothing else is declared there.

Only a class that held a test before this run goes, so nothing a customer left
empty on purpose is touched, and only one that nothing can depend on:

* it extends nothing and implements nothing, since a base class or an interface
  may bring tests of its own, and it is not ``abstract``;
* nothing outside its own text spells its name, in this file or any other.

A class holding a test in any form this does not read (TestNG's ``@Test`` on
the class, say) never reads as having held one, so it stays.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys, file_deletion, mock_stubs, syntax, unused

LANGUAGES = frozenset({"java", "kt"})
_SUFFIXES = {"java": ".java", "kt": ".kt"}
#: What may stand at the top of a file beside its types without keeping it.
_FILE_FURNITURE = frozenset(
    {
        "package_declaration", "import_declaration", "package_header", "import", "import_list",
        "file_annotation", "line_comment", "block_comment", "multiline_comment",
    }
)
_METHODS = frozenset({"method_declaration", "function_declaration"})


@dataclass(frozen=True)
class _Class:
    node: Node
    #: Its enclosing classes' names and its own, outermost first.
    path: tuple[str, ...]


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _name(declaration: Node) -> str:
    name = declaration.child_by_field_name("name")
    if name is None:
        name = next((c for c in declaration.named_children if c.type == "identifier"), None)
    return _text(name)


def _classes(root: Node) -> list[_Class]:
    """Every named class, innermost last; a local or an anonymous one is not a
    member of anything."""
    found: list[_Class] = []
    stack: list[tuple[Node, tuple[str, ...]]] = [(root, ())]
    while stack:
        node, path = stack.pop()
        for child in node.named_children:
            if child.type == "class_declaration":
                inner = (*path, _name(child))
                found.append(_Class(child, inner))
                body = next((c for c in child.named_children if c.type == "class_body"), None)
                if body is not None:
                    stack.append((body, inner))
    return found


def _modifiers(declaration: Node) -> list[Node]:
    held = next((c for c in declaration.children if c.type == "modifiers"), None)
    return [] if held is None else list(held.children)


def _nested(declaration: Node) -> bool:
    return any(
        mock_stubs._simple_name(_text(m)) == "Nested" for m in _modifiers(declaration) if "annotation" in m.type
    )


def _holds_a_test(declaration: Node, data: bytes, language: str) -> bool:
    profile = mock_stubs._PROFILES[language]
    body = next((c for c in declaration.children if c.type == "class_body"), None)
    return body is not None and any(
        (node.type in _METHODS and profile.test_name(node, data) is not None)
        or (node.type == "class_declaration" and _nested(node))
        for node in mock_stubs._walk(body)
    )


def _inherits(declaration: Node) -> bool:
    return any(
        c.type in ("superclass", "super_interfaces", "delegation_specifiers") for c in declaration.named_children
    ) or any(_text(m) == "abstract" for m in _modifiers(declaration))


def _named_elsewhere(name: str, path: str, source: str, span: tuple[int, int], repo_dir: str) -> bool:
    data = source.encode("utf-8")
    pattern = cross_file_keys.mention_pattern([name])
    rest = (data[: span[0]] + b"\n" + data[span[1] :]).decode("utf-8", "replace")
    return pattern.search(rest) is not None or bool(
        cross_file_keys.mention_files(repo_dir, [name], exclude=[path])
    )


def _emptied_class(
    source: str, before: frozenset[tuple[str, ...]], path: str, language: str, repo_dir: str
) -> _Class | None:
    """One class of ``source`` this pass deletes, innermost first."""
    data = source.encode("utf-8")
    for found in reversed(_classes(syntax._parse(source, language))):
        declaration = found.node
        if (
            found.path in before
            and not _holds_a_test(declaration, data, language)
            and not _inherits(declaration)
            and not _named_elsewhere(
                found.path[-1], path, source, (declaration.start_byte, declaration.end_byte), repo_dir
            )
        ):
            return found
    return None


def _tested(source: str, language: str) -> frozenset[tuple[str, ...]]:
    data = source.encode("utf-8")
    return frozenset(
        found.path for found in _classes(syntax._parse(source, language))
        if _holds_a_test(found.node, data, language)
    )


def _only_furniture(source: str, language: str) -> bool:
    return all(c.type in _FILE_FURNITURE for c in syntax._parse(source, language).named_children)


def _without(source: str, found: _Class) -> str:
    data = source.encode("utf-8")
    start = mock_stubs._with_leading_comments(found.node, data)
    end = found.node.end_byte
    text = data[start:end].decode("utf-8", "replace")
    return unused._delete_method(source, unused.Candidate(found.path[-1], start, end, "method", text))


def remove(
    summaries: list, language: str, repo_dir: str, rewrite: Callable
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """``summaries`` with each class the run emptied deleted, and a row for
    each, as a deleted test is listed."""
    if language not in LANGUAGES:
        return summaries, ()
    result: list = []
    rows: list[tuple[str, str, str]] = []
    gone: list[str] = []
    for summary in summaries:
        if (
            file_deletion.deleted(summary)
            or summary.original_content == summary.content
            or not summary.path.endswith(_SUFFIXES[language])
        ):
            result.append(summary)
            continue
        resolved = str(Path(summary.path).resolve())
        before = _tested(summary.original_content, language)
        text, emptied = summary.content, []
        while before:
            found = _emptied_class(text, before, resolved, language, repo_dir)
            if found is None:
                break
            text = _without(text, found)
            emptied.append(found.path[-1])
        if not emptied:
            result.append(summary)
            continue
        relative = str(Path(resolved).relative_to(Path(repo_dir).resolve()))
        rows += [(relative, mock_stubs.TEST, f"{name}, left with no test") for name in emptied]
        if _only_furniture(text, language):
            result.append(summary)
            gone.append(summary.path)
            continue
        Path(summary.path).write_text(text, encoding="utf-8")
        result.append(rewrite(summary.path, summary.original_content, text))
    return file_deletion.delete(result, gone, rewrite), tuple(rows)
