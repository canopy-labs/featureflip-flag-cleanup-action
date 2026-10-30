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
* a JavaScript-family file that names it only in TEST TITLES (and comments):
  prose as well — ``it('renders the page when the flag is on')`` is a string
  no code reads as a key — but prose in front of every run of the suite, so
  the pull request lists it apart from the comments;
* a file that names the key only as a URL PATH SEGMENT in a string
  (``/environments/${id}/scheduled-changes``): an endpoint named after the
  feature, which is common and is no flag read, so it is listed apart;
* a TypeScript-family file that names it only as a STRING VALUE IN A
  LITERAL, an element of an array that also holds something computed
  (``['scheduled-changes', orgId] as const``, a query cache key): a value
  named after the feature, listed apart for the same reason;
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

from flag_cleanup import cross_file_ts, mock_stubs, syntax, ts_tests, unused

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
    #: TypeScript-family files every mention of the flag in which sits in a
    #: test's title or a comment, with at least one in a title.
    title_only: tuple[str, ...] = ()
    #: ``(path, line, text)`` for each comment line naming the flag in a
    #: ``comment_only`` file: the prose the run kept because it says
    #: something besides the flag (see :mod:`flag_cleanup.stale_comments`).
    comment_lines: tuple[tuple[str, int, str], ...] = ()
    #: Files every live mention of the flag in which is a URL path segment in
    #: a string (and the rest comments or test titles); see :func:`_path_only`.
    path_only: tuple[str, ...] = ()
    #: Files outside the run's ``directories`` that also name the flag,
    #: relative to the checkout root. Never examined and never edited; see
    #: :func:`flag_cleanup.orchestrate.outside_mentions`.
    outside: tuple[str, ...] = ()
    #: TypeScript-family files every live mention of the flag in which is a
    #: whole string element of an array literal beside a non-literal element
    #: (a query cache key), and the rest comments or test titles; see
    #: :func:`_literal_only`.
    literal_only: tuple[str, ...] = ()


def _mention(flag_key: str) -> re.Pattern[str]:
    """The key as a whole token: not inside a longer key or identifier. A
    full stop after it ends a sentence; one with a word after it is a
    member read."""
    return re.compile(rf"(?<![\w.-]){re.escape(flag_key)}(?![\w-])(?!\.\w)")


def _comment_only(
    root: Node, mention: re.Pattern[str], inert: frozenset[int] = frozenset()
) -> bool:
    """Whether every mention sits in a comment, or in a node ``inert`` names.

    At least one has to be found there: a file whose mention matched no node
    at all is not proven anything.
    """
    in_comments = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type or node.id in inert:
            in_comments += len(mention.findall(node.text.decode("utf-8", "replace")))
            continue
        if node.child_count == 0:
            if mention.search(node.text.decode("utf-8", "replace")):
                return False
            continue
        stack.extend(node.children)
    return in_comments > 0


def _comment_lines(root: Node, source: str, mention: re.Pattern[str]) -> list[tuple[int, str]]:
    """``(line, text)`` for each source line where a comment names the key."""
    lines = source.split("\n")
    found: dict[int, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            text = node.text.decode("utf-8", "replace")
            for match in mention.finditer(text):
                row = node.start_point[0] + text.count("\n", 0, match.start())
                found.setdefault(row + 1, lines[row].strip() if row < len(lines) else "")
            continue
        stack.extend(node.children)
    return sorted(found.items())


#: The grammars whose tests carry a title :mod:`flag_cleanup.ts_tests` can read.
_TITLED = frozenset({"ts", "tsx", "js"})


def _title_only(
    root: Node, source: str, mention: re.Pattern[str], language: str
) -> bool:
    """Whether every mention sits in a test title or a comment, one in a title."""
    if language not in _TITLED:
        return False
    titles = ts_tests.title_nodes(root, source.encode("utf-8"))
    in_titles = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            continue
        if node.id in titles:
            in_titles += len(mention.findall(node.text.decode("utf-8", "replace")))
            continue
        if node.child_count == 0:
            if mention.search(node.text.decode("utf-8", "replace")):
                return False
            continue
        stack.extend(node.children)
    return in_titles > 0


#: What may follow a key that is a whole path segment: another segment, a
#: query or fragment, or the string's closing quote.
_PATH_END = frozenset(b"/?#\"'`")


def _path_only(root: Node, source: str, mention: re.Pattern[str], language: str) -> bool:
    """Whether every mention outside comments and test titles is a whole
    path segment of a URL in a string: ``/`` in front of it, and ``/``,
    ``?``, ``#`` or the closing quote after it, with one such mention found.

    Read off the bytes around each mention rather than a grammar's string
    node, so every language answers the same way; the leaf must still be a
    string's (its type, or its parent's, names one), or ``a/b`` in code
    would count."""
    data = source.encode("utf-8")
    titles = ts_tests.title_nodes(root, data) if language in _TITLED else frozenset()
    in_paths = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type or node.id in titles:
            continue
        if node.child_count:
            stack.extend(node.children)
            continue
        text = node.text.decode("utf-8", "replace")
        matches = list(mention.finditer(text))
        if not matches:
            continue
        if "str" not in node.type and (node.parent is None or "str" not in node.parent.type):
            return False
        for match in matches:
            start = node.start_byte + len(text[: match.start()].encode("utf-8"))
            end = start + len(match.group().encode("utf-8"))
            if data[start - 1 : start] != b"/" or (end < len(data) and data[end] not in _PATH_END):
                return False
            in_paths += 1
    return in_paths > 0


def _literal_only(root: Node, source: str, mention: re.Pattern[str], language: str) -> bool:
    """Whether every mention outside comments and test titles is a string
    element of an array literal, the WHOLE string being the key, with one
    such mention found. TypeScript family only.

    Three things keep a list of flag keys out, since that is a flag read in
    all but name: the array must hold at least one element that is not a
    string literal (``['key', orgId]``; ``['key', 'other']`` stays in the
    generic caveat), it must not be handed straight to a call (the callee may
    read flags by key), and the string must be a plain one (a template can
    splice anything in)."""
    if language not in _TITLED:
        return False
    data = source.encode("utf-8")
    titles = ts_tests.title_nodes(root, data)
    found = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type or node.id in titles:
            continue
        if node.child_count:
            stack.extend(node.children)
            continue
        if not mention.search(node.text.decode("utf-8", "replace")):
            continue
        string = node.parent
        if (
            node.type != "string_fragment"
            or string is None
            or string.type != "string"
            or not mention.fullmatch(node.text.decode("utf-8", "replace"))
            or len(string.named_children) != 1
            or not _array_value(string)
        ):
            return False
        found += 1
    return found > 0


def _array_value(string: Node) -> bool:
    """Whether ``string`` is an element of an array literal that also holds a
    non-literal element and is not an argument of a call."""
    array = string.parent
    if array is None or array.type != "array":
        return False
    if array.parent is not None and array.parent.type == "arguments":
        return False
    return any(
        element.type not in ("string", "comment") for element in array.named_children
    )


#: The grammar node of one enum member, per language (measured, not guessed).
#: Python has no enum syntax: an assignment in a class whose bases name an
#: ``...Enum`` or a Django ``...Choices`` is the member there.
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
_PYTHON_ENUM_BASE = re.compile(r"\w*(?:Enum|Choices)\b")


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
    title_only: list[str] = []
    enum_held: list[tuple[str, str]] = []
    comment_lines: list[tuple[str, int, str]] = []
    path_only: list[str] = []
    literal_only: list[str] = []
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
            comment_lines.extend((path, line, text) for line, text in _comment_lines(root, source, mention))
            continue
        if _title_only(root, source, mention, language):
            title_only.append(path)
            continue
        if _path_only(root, source, mention, language):
            path_only.append(path)
            continue
        if _literal_only(root, source, mention, language):
            literal_only.append(path)
            continue
        held = _enum_held(root, mention, language)
        if held is not None:
            enum_held.append((path, held))
    return Residue(
        tuple(stubbed),
        tuple(comment_only),
        tuple(enum_held),
        tuple(title_only),
        tuple(comment_lines),
        tuple(path_only),
        literal_only=tuple(literal_only),
    )


def live_mention(path: str, source: str, flag_key: str) -> bool:
    """Whether ``source`` (the text of ``path``) names ``flag_key`` other than
    in a comment or, in the TypeScript family, a test title. One file's
    answer to :func:`live_references`, for text that is not on disk yet."""
    mention = _mention(flag_key)
    if not mention.search(source):
        return False
    language = _GRAMMAR_BY_EXTENSION.get(os.path.splitext(path)[1])
    if language is None:
        return True
    root = _parse(source, language)
    inert = (
        ts_tests.title_nodes(root, source.encode("utf-8"))
        if ts_tests.covers(language)
        else frozenset()
    )
    return not _comment_only(root, mention, inert)


def live_references(paths: tuple[str, ...], flag_key: str) -> tuple[str, ...]:
    """``paths`` without the files that name the flag only in comments and,
    in the TypeScript family, test titles.

    The NEGATIVE of the comment verdict, so it errs the other way from
    :func:`classify`: a file this module cannot parse, or whose extension it
    does not know, counts as a live reference. The caller keeps a registry
    entry while any survives, and an entry kept for nothing costs one line.

    A test title is the one string let through, because it is the one that is
    inert by construction: nothing is typed against it and nothing looks it
    up, so deleting the entry cannot break it. Any other string stays live
    even in a test file, where fixture data borrowing the key may still reach
    a lookup by key — see :func:`flag_cleanup.ts_tests.title_nodes`.
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
        if language is None:
            live.append(path)
            continue
        root = _parse(source, language)
        inert = (
            ts_tests.title_nodes(root, source.encode("utf-8"))
            if ts_tests.covers(language)
            else frozenset()
        )
        if not _comment_only(root, mention, inert):
            live.append(path)
    return tuple(live)
