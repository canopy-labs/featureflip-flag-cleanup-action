"""Drop a removed flag's condition from a Java or Kotlin test's title.

The titles a person writes: ``@DisplayName("…")``, a Kotlin function named in
backticks, and a Kotest, Spek or minutest ``test("…") { }``. What may be dropped,
and the bar a title must pass, are :func:`flag_cleanup.ts_tests.reworded_title`'s.
A Java method name is left: renaming it is an edit a build can see.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from flag_cleanup import mock_stubs, syntax, ts_tests

_LANGUAGES = frozenset({"java", "kt"})
_STRING_PARTS = frozenset({"string_fragment", "string_content"})


def covers(language: str) -> bool:
    return language in _LANGUAGES


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _plain_string(node: Node, data: bytes) -> tuple[int, int] | None:
    """The bytes between the quotes of the first string under ``node``, when
    it is one run of text: not an interpolation, and not two parts of one."""
    for found in mock_stubs._walk(node):
        if found.type in ("string_literal", "line_string_literal"):
            parts = [c for c in found.named_children if "comment" not in c.type]
            if len(parts) == 1 and parts[0].type in _STRING_PARTS:
                return parts[0].start_byte, parts[0].end_byte
            return None
    return None


def _annotation_name(node: Node, data: bytes) -> str:
    return mock_stubs._simple_name(_text(node, data).split("(", 1)[0])


def _titles(root: Node, data: bytes, language: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for node in mock_stubs._walk(root):
        span = None
        if node.type in ("annotation", "marker_annotation") and _annotation_name(node, data) == "DisplayName":
            span = _plain_string(node, data)
        elif language == "kt" and node.type == "call_expression" and mock_stubs._kotlin_call_test(node, data):
            span = _plain_string(node.named_children[0], data)
        elif language == "kt" and node.type == "function_declaration" and mock_stubs._kotlin_test(node, data):
            name = next((c for c in node.children if c.type in ("identifier", "simple_identifier")), None)
            if name is not None and _text(name, data).startswith("`"):
                span = (name.start_byte + 1, name.end_byte - 1)
        if span is not None:
            found.append(span)
    return found


def rename_kept_titles(
    source: str, language: str, flag_key: str, kept: str, has_stored_snapshot: object = False
) -> tuple[str, list[tuple[str, str]]]:
    """``source`` with the condition dropped from each title naming the KEPT
    branch, and ``(old, new)`` for each it rewrote."""
    if flag_key not in source:
        return source, []
    data = source.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    renamed: list[tuple[str, str]] = []
    produced: set[str] = set()
    spans = _titles(syntax._parse(source, language), data, language)
    # Kotest registers a second test of one name in a container as a duplicate
    # and fails the spec, so a title another test already carries is not a
    # name to rename into. Anywhere in the file: a sibling is the case that
    # matters, and telling containers apart is not worth a wrong rename.
    taken = {data[a:b].decode("utf-8", "replace") for a, b in spans} if language == "kt" else set()
    for start, end in spans:
        title = data[start:end].decode("utf-8", "replace")
        new = ts_tests.reworded_title(title, flag_key, kept)
        # A function's name must stay its own, and a title with `$` is a template.
        if (
            new is None
            or "$" in title
            or f"`{new}`" in source
            or new in produced
            or new in taken
            or re.search(rf"\bfun\s+{re.escape(new)}\s*\(", source)
        ):
            continue
        produced.add(new)
        edits.append((start, end, new.encode("utf-8")))
        renamed.append((title, new))
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8"), renamed
