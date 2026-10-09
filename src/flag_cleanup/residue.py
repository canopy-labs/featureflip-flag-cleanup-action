"""Sort the files that still mention a flag once its removal is on disk.

``orchestrate.all_unprocessed`` answers "does this pull request remove every
mention of the flag?" with a list of files. That list mixes things a reviewer
should treat very differently, and the pull request used to print it as one
caveat:

* a TEST THAT STUBS THE READ this pull request removed. Its setup is now dead,
  and an assertion beside it about what the flag returned can start failing —
  the most urgent entry, because it can turn the pull request red;
* a file that names the flag ONLY IN COMMENTS — prose to update, nothing that
  can break;
* a file whose only mention of the key is an ENUM MEMBER's value (or, in
  TypeScript, a property of an ``as const`` object, which the run reads as
  one): resolved for some languages and forms, so the pull request says which;
  otherwise it would sit in the generic caveat with no explanation;
* everything else, which keeps the original, generic caveat.

Both verdicts are POSITIVE ones, and a file this module cannot parse, or whose
extension it does not know, is simply left in the generic group. That is the
safe direction: the generic caveat already asks the reviewer to look.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import cross_file_ts, mock_stubs, syntax, unused

#: Extension to the grammar this module parses it with. ERB is absent: its
#: Ruby lives inside a template, and a comment verdict there would need the
#: template's own code view. It stays in the generic group.
_GRAMMAR_BY_EXTENSION = {
    **{ext: "ts" for ext in (".ts", ".mts", ".cts")},
    ".tsx": "tsx",
    **{ext: "js" for ext in (".js", ".jsx", ".mjs", ".cjs")},
    ".java": "java",
    ".go": "go",
    ".py": "python",
    ".kt": "kt",
    ".kts": "kt",
    ".cs": "csharp",
    **{ext: "php" for ext in (".php", ".phtml", ".inc", ".module")},
    ".rb": "ruby",
    ".rake": "ruby",
    ".dart": "dart",
    ".swift": "swift",
}


@dataclass(frozen=True)
class Residue:
    #: ``(path, statement)`` for every C# statement still describing a read
    #: of the flag to a mock. See :func:`flag_cleanup.mock_stubs.mock_descriptions`.
    stubbed: tuple[tuple[str, str], ...] = ()
    #: Files every mention of the flag in which sits inside a comment.
    comment_only: tuple[str, ...] = ()
    #: ``(path, member)`` for every file whose every live mention of the flag
    #: is an enum member's value: the member's first line. Resolved for some
    #: languages and some forms only, so the pull request says which.
    enum_held: tuple[tuple[str, str], ...] = ()


def _mention(flag_key: str) -> re.Pattern[str]:
    """The key as a whole token: not inside a longer key or identifier."""
    return re.compile(rf"(?<![\w.-]){re.escape(flag_key)}(?![\w.-])")


def _comment_only(root: Node, mention: re.Pattern[str]) -> bool:
    in_comments = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            in_comments += len(mention.findall(node.text.decode("utf-8", "replace")))
            continue
        if node.child_count == 0:
            if mention.search(node.text.decode("utf-8", "replace")):
                return False
            continue
        stack.extend(node.children)
    return in_comments > 0


#: The grammar node of one enum member, per language (measured, not guessed).
#: Python has no enum syntax: an assignment in a class whose bases name an
#: ``...Enum`` is the member there.
_ENUM_MEMBERS = {
    "java": "enum_constant",
    "dart": "enum_constant",
    "kt": "enum_entry",
    "swift": "enum_entry",
    "ts": "enum_assignment",
    "tsx": "enum_assignment",
    "js": "enum_assignment",
    "csharp": "enum_member_declaration",
    "php": "enum_case",
}
_PYTHON_ENUM_BASE = re.compile(r"\w*Enum\b")


#: Substrings of a node type that mark a callable or a body. A mention below
#: one of these, between it and the member, is code that runs (a method of an
#: enum constant, a lambda in its arguments), not the member's own value.
_BODY_MARKERS = ("block", "body", "function", "method", "lambda", "constructor", "closure", "statements")


def _python_member(node: Node) -> Node | None:
    """The assignment that is a DIRECT statement of an Enum class's body."""
    current: Node | None = node
    while current is not None and current.type != "assignment":
        if any(m in current.type for m in _BODY_MARKERS):
            return None
        current = current.parent
    if current is None:
        return None
    statement = current.parent
    body = statement.parent if statement is not None else None
    owner = body.parent if body is not None else None
    if (
        statement is None
        or statement.type != "expression_statement"
        or body is None
        or body.type != "block"
        or owner is None
        or owner.type != "class_definition"
    ):
        return None
    bases = owner.child_by_field_name("superclasses")
    text = bases.text.decode("utf-8", "replace") if bases is not None else ""
    return current if _PYTHON_ENUM_BASE.search(text) else None


def _enum_member(node: Node, language: str) -> Node | None:
    """The enum member whose OWN value ``node`` is, or None when ``node`` sits
    in a body or callable on the way up (a live read, not the stored key)."""
    if language == "python":
        return _python_member(node)
    kind = _ENUM_MEMBERS.get(language)
    current: Node | None = node
    while current is not None:
        if kind is not None and current.type == kind:
            return current
        if kind == "enum_assignment" and current.type == "pair" and _as_const_property(current, node):
            return current
        if any(m in current.type for m in _BODY_MARKERS):
            return None
        current = current.parent
    return None


def _as_const_property(pair: Node, mention: Node) -> bool:
    """Whether ``mention`` is the plain string value of ``pair``, a property of
    an ``as const`` object literal. Not its name (a registry keyed by the
    flag), and not something nested in its value: no entry holds those."""
    value = pair.child_by_field_name("value")
    if value is None or value.type != "string" or not (value.start_byte <= mention.start_byte < value.end_byte):
        return False
    obj = pair.parent
    outer = obj
    while outer is not None and outer.parent is not None and outer.parent.type == "parenthesized_expression":
        outer = outer.parent
    return (
        obj is not None
        and obj.type == "object"
        and outer is not None
        and outer.parent is not None
        and cross_file_ts.as_const_object(outer.parent) is not None
    )


def _enum_held(root: Node, mention: re.Pattern[str], language: str) -> str | None:
    """The first enum member holding the key, when every live mention sits in one.

    Each mention is judged at the SMALLEST node whose text contains it, so a
    grammar whose string content is not a leaf (Dart's) is judged the same way.
    """
    first: Node | None = None
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            continue
        if not mention.search(node.text.decode("utf-8", "replace")):
            continue
        inner = [c for c in node.children if mention.search(c.text.decode("utf-8", "replace"))]
        if inner:
            stack.extend(inner)
            continue
        member = _enum_member(node, language)
        if member is None:
            return None
        if first is None or member.start_byte < first.start_byte:
            first = member
    if first is None:
        return None
    return first.text.decode("utf-8", "replace").splitlines()[0].strip()


def _parse(source: str, language: str) -> Node:
    if language in ("ts", "tsx", "js"):
        return unused._parse(source, language)
    return syntax._parse(source, language)


def classify(
    paths: tuple[str, ...], flag_key: str, accessors: tuple[str, ...] = ()
) -> Residue:
    """Sort ``paths`` — the still-referencing files — into the two groups a
    pull request can say something specific about. Must be asked while the
    rewrite is on disk, like the scan that produced ``paths``."""
    mention = _mention(flag_key)
    stubbed: list[tuple[str, str]] = []
    comment_only: list[str] = []
    enum_held: list[tuple[str, str]] = []
    for path in paths:
        language = _GRAMMAR_BY_EXTENSION.get(os.path.splitext(path)[1])
        if language is None:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        if language == "csharp":
            described = mock_stubs.mock_descriptions(source, flag_key, accessors)
            if described:
                stubbed.extend((path, text) for text in described)
                continue
        root = _parse(source, language)
        if _comment_only(root, mention):
            comment_only.append(path)
            continue
        held = _enum_held(root, mention, language)
        if held is not None:
            enum_held.append((path, held))
    return Residue(tuple(stubbed), tuple(comment_only), tuple(enum_held))


def live_references(paths: tuple[str, ...], flag_key: str) -> tuple[str, ...]:
    """``paths`` without the files that name the flag only in comments.

    The NEGATIVE of the comment verdict, so it errs the other way from
    :func:`classify`: a file this module cannot parse, or whose extension it
    does not know, counts as a live reference. The caller keeps a registry
    entry while any survives, and an entry kept for nothing costs one line.
    """
    mention = _mention(flag_key)
    live: list[str] = []
    for path in paths:
        language = _GRAMMAR_BY_EXTENSION.get(os.path.splitext(path)[1])
        try:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        except (OSError, UnicodeDecodeError):
            live.append(path)
            continue
        if language is None or not _comment_only(_parse(source, language), mention):
            live.append(path)
    return tuple(live)
