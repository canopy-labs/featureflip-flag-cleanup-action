"""Put a list a deletion shrank back on one line.

The TypeScript-family passes that remove a parameter, an argument or a JSX
attribute splice bytes out of a list. When the list was laid out one element
per line, what is left keeps that layout::

    function renderChecklist(
      steps: number
    ) {

    <TestProvider
    >

which a formatter, or a reviewer, writes as
``function renderChecklist(steps: number) {`` and ``<TestProvider>``.
:func:`splice` makes the deletions and then joins each such list, so the edit
that shrank the list is the edit that tidies it, and nothing the run did not
shrink is touched.

A list is joined only when all of this holds, and is left exactly as the
deletion left it otherwise:

* it is a function's **parameters** or a call's **arguments** (object and
  array literals and type parameters are left alone: a formatter keeps an
  object it finds expanded, and the rest are not edited by these passes);
* it held two or more elements before the deletions and holds exactly one
  after, so a list the customer wrote with one element is never rewritten;
* the remaining element is on one line of its own, the opening bracket ends
  its line and the closing bracket starts its line, with only whitespace and
  at most one trailing comma between them and the element;
* no comment sits anywhere inside the brackets;
* the joined line fits the width :func:`line_limit` reads for the file.

The trailing comma goes with the line breaks: ``f(a)`` rather than ``f(a,)``,
as a formatter writes a list that fits on one line.

A JSX opening or self-closing element is the one other list joined, under the
same conditions except two: any number of attributes may be left, and none
at all. They go on the tag's line, one space apart, the ``>`` straight after
them and the ``/>`` a space after: ``<Tag>``, ``<Tag />``, ``<Tag a={1} b>``.
Every attribute left must sit on one line, with only whitespace between it,
the tag name and the bracket, and the tag name itself must be on one line.

Where one join would sit inside another, only the inner one is made.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk

#: The lists this joins to their one remaining element: a function's
#: parameters and a call's arguments.
_LISTS = frozenset({"formal_parameters", "arguments"})

#: The JSX elements whose attributes this joins onto the tag's line.
_ELEMENTS = frozenset({"jsx_opening_element", "jsx_self_closing_element"})

#: How wide a tab counts when a joined line is measured, generously, so a
#: tab-indented file is never judged to have more room than it does.
_TAB_WIDTH = 4

#: The width a formatter assumes when its configuration names none.
_PRETTIER_DEFAULT_WIDTH = 80

#: Prettier's configuration files, in the order it looks for them in one
#: directory. The ones it evaluates as code are listed so that finding one
#: stops the search: its width is unknowable here, and the list is then left.
_PRETTIER_FILES = (
    "package.json",
    "package.yaml",
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yaml",
    ".prettierrc.yml",
    ".prettierrc.json5",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    ".prettierrc.ts",
    ".prettierrc.cts",
    ".prettierrc.mts",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    "prettier.config.ts",
    "prettier.config.cts",
    "prettier.config.mts",
    ".prettierrc.toml",
)

#: Found, but holding no setting this can read: the search stops, and nothing
#: is joined.
_UNKNOWN = object()


def splice(
    source: str,
    spans: Iterable[tuple[int, int]],
    language: str,
    width: int | None = None,
) -> str:
    """``source`` without the byte ``spans``, each list they shrank to one
    element joined onto one line when it fits ``width``.

    ``spans`` are ``(start, end)`` byte offsets into ``source`` and must not
    overlap. With ``width`` ``None`` this only deletes.
    """
    ordered = sorted(spans)
    data = source.encode("utf-8")
    joins = _shrunk_lists(source, ordered, language) if width is not None else []
    for start, end in reversed(ordered):
        data = data[:start] + data[end:]
    moved = [
        _Join(
            _moved(join.start, ordered),
            _moved(join.head_end, ordered),
            tuple((_moved(s, ordered), _moved(e, ordered)) for s, e in join.elements),
            _moved(join.close_start, ordered),
            join.attributes,
        )
        for join in joins
    ]
    for join in sorted(moved, key=lambda j: j.head_end, reverse=True):
        joined = _joined_attributes(data, join, width) if join.attributes else _joined(data, join, width)
        if joined is not None:
            data = joined
    return data.decode("utf-8")


def line_limit(path: str, original: str, repo_dir: str) -> int | None:
    """The widest line a join may write in ``path``, or ``None`` to join nothing.

    A Prettier configuration in the file's directory or above it, up to
    ``repo_dir``, decides first: its ``printWidth``, else the
    ``max_line_length`` EditorConfig gives the file, else Prettier's own
    default. A configuration this cannot read (one written as code, one that
    extends a shared configuration, or one with per-file ``overrides``) joins
    nothing. Without Prettier, EditorConfig's ``max_line_length`` decides,
    and without either, the longest line the file already had: a join never
    writes a line longer than the customer's own.
    """
    prettier = _prettier_width(path, repo_dir)
    if prettier is _UNKNOWN:
        return None
    editorconfig = _editorconfig_width(path, repo_dir)
    if prettier is True:
        # Prettier reads `max_line_length` when it names no width of its own.
        return editorconfig if isinstance(editorconfig, int) else _PRETTIER_DEFAULT_WIDTH
    if isinstance(prettier, int):
        return prettier
    if isinstance(editorconfig, int):
        return editorconfig
    return max((_width(line) for line in original.splitlines()), default=0)


# ---------------------------------------------------------------------------
# Which lists the deletions shrank
# ---------------------------------------------------------------------------


class _Join(NamedTuple):
    """One list to join, by byte offsets."""

    #: Where the list's container starts.
    start: int
    #: Where the bytes before the elements end: the opening bracket, or the
    #: tag name and its type arguments.
    head_end: int
    #: Each element left, ``(start, end)``.
    elements: tuple[tuple[int, int], ...]
    #: Where the closing bracket starts.
    close_start: int
    #: Whether this is a JSX element's attributes rather than a list in brackets.
    attributes: bool


def _shrunk_lists(source: str, spans: list[tuple[int, int]], language: str) -> list[_Join]:
    """Each list ``spans`` shrink, in ``source``'s offsets: a parameter or
    argument list left with one element, or a JSX element left with fewer
    attributes."""
    if not spans:
        return []
    root = _parse(source, language)
    found: list[tuple[Node, _Join]] = []
    for container in _walk(root):
        if container.type in _LISTS:
            join = _shrunk_list(container, spans)
        elif container.type in _ELEMENTS:
            join = _shrunk_element(container, spans)
        else:
            continue
        if join is not None:
            found.append((container, join))
    # A join inside another moves the outer one's bytes, so the outer one is left.
    return [
        join
        for container, join in found
        if not any(
            other is not container
            and container.start_byte <= other.start_byte
            and other.end_byte <= container.end_byte
            for other, _ in found
        )
    ]


def _shrunk_list(container: Node, spans: list[tuple[int, int]]) -> _Join | None:
    if len(container.children) < 2 or not _touched(container, spans) or _has_comment(container):
        return None
    elements = list(container.named_children)
    if len(elements) < 2:
        return None
    kept = [e for e in elements if not _taken(e, spans)]
    if len(kept) != 1 or _cuts(kept[0], spans) or _cuts_bracket(container, spans):
        return None
    return _Join(
        container.start_byte,
        container.children[0].end_byte,
        ((kept[0].start_byte, kept[0].end_byte),),
        container.children[-1].start_byte,
        False,
    )


def _shrunk_element(container: Node, spans: list[tuple[int, int]]) -> _Join | None:
    if not _touched(container, spans) or _has_comment(container):
        return None
    attributes = container.children_by_field_name("attribute")
    kept = [a for a in attributes if not _taken(a, spans)]
    if len(kept) == len(attributes) or any(_cuts(a, spans) for a in kept):
        return None
    ids = {a.id for a in attributes}
    head = [c for c in container.children[:-1] if c.id not in ids]
    close = container.children[-1]
    if any(_cuts(node, spans) for node in (*head, close)):
        return None
    return _Join(
        container.start_byte,
        max(node.end_byte for node in head),
        tuple((a.start_byte, a.end_byte) for a in kept),
        close.start_byte,
        True,
    )


def _touched(container: Node, spans: list[tuple[int, int]]) -> bool:
    """Whether any span takes bytes from inside ``container``."""
    return any(container.start_byte < end and start < container.end_byte for start, end in spans)


def _taken(node: Node, spans: list[tuple[int, int]]) -> bool:
    """Whether one span takes all of ``node``."""
    return any(s <= node.start_byte and node.end_byte <= t for s, t in spans)


def _has_comment(container: Node) -> bool:
    return any(node.type == "comment" for node in _walk(container))


def _cuts(node: Node, spans: list[tuple[int, int]]) -> bool:
    """Whether any span takes bytes from inside ``node``."""
    return any(s < node.end_byte and node.start_byte < t for s, t in spans)


def _cuts_bracket(container: Node, spans: list[tuple[int, int]]) -> bool:
    """Whether any span takes one of ``container``'s brackets."""
    return any(_cuts(bracket, spans) for bracket in (container.children[0], container.children[-1]))


def _moved(offset: int, spans: list[tuple[int, int]]) -> int:
    """Where ``offset``, outside every span, lands once ``spans`` are deleted."""
    return offset - sum(end - start for start, end in spans if end <= offset)


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------

_TRAILING = re.compile(rb"\s*,?\s*")


def _joined(data: bytes, join: _Join, width: int) -> bytes | None:
    """``data`` with the list between the brackets on one line, or ``None``."""
    ((element_start, element_end),) = join.elements
    before = data[join.head_end:element_start]
    element = data[element_start:element_end]
    after = data[element_end:join.close_start]
    if before.strip() or b"\n" not in before or b"\n" in element:
        return None
    if not _TRAILING.fullmatch(after) or b"\n" not in after:
        return None
    # Bracket ends its line, bracket starts its line: whitespace on both
    # sides of the element, line breaks included, and nothing else.
    return _fitted(data, join, element, width)


def _joined_attributes(data: bytes, join: _Join, width: int) -> bytes | None:
    """``data`` with the JSX element's attributes on its tag's line, or ``None``."""
    if b"\n" in data[join.start:join.head_end]:
        return None
    bounds = [join.head_end, *(offset for element in join.elements for offset in element), join.close_start]
    gaps = [data[bounds[i]:bounds[i + 1]] for i in range(0, len(bounds), 2)]
    attributes = [data[start:end] for start, end in join.elements]
    if any(gap.strip() for gap in gaps) or not any(b"\n" in gap for gap in gaps):
        return None
    if any(b"\n" in attribute for attribute in attributes):
        return None
    joined = b"".join(b" " + attribute for attribute in attributes)
    if data.startswith(b"/>", join.close_start):
        joined += b" "
    return _fitted(data, join, joined, width)


def _fitted(data: bytes, join: _Join, joined: bytes, width: int) -> bytes | None:
    """``data`` with ``joined`` in place of everything between the list's
    head and its closing bracket, or ``None`` when that line is wider than ``width``."""
    line_start = data.rfind(b"\n", 0, join.head_end) + 1
    line_end = data.find(b"\n", join.close_start)
    line_end = len(data) if line_end == -1 else line_end
    line = data[line_start:join.head_end] + joined + data[join.close_start:line_end]
    if _width(line.rstrip(b"\r").decode("utf-8")) > width:
        return None
    return data[:join.head_end] + joined + data[join.close_start:]


def _width(line: str) -> int:
    return len(line.expandtabs(_TAB_WIDTH))


# ---------------------------------------------------------------------------
# The width the repository asks for
# ---------------------------------------------------------------------------


def _directories(path: str, repo_dir: str) -> list[Path]:
    """``path``'s directory and each one above it, up to ``repo_dir`` and never past it."""
    root = Path(repo_dir).resolve()
    directory = Path(path).resolve().parent
    found: list[Path] = []
    while True:
        if directory != root and root not in directory.parents:
            return found
        found.append(directory)
        if directory == root:
            return found
        directory = directory.parent


def _prettier_width(path: str, repo_dir: str) -> object:
    """``None`` when no Prettier configuration applies; ``True`` when one does
    without naming a width; the width; or ``_UNKNOWN``."""
    for directory in _directories(path, repo_dir):
        for name in _PRETTIER_FILES:
            candidate = directory / name
            if not candidate.is_file():
                continue
            found = _read_prettier(candidate)
            if found is not None:
                return found
    return None


def _read_prettier(candidate: Path) -> object:
    """What ``candidate`` says, or ``None`` when it is not a Prettier configuration."""
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _UNKNOWN
    name = candidate.name
    if name == "package.json":
        try:
            manifest = json.loads(text)
        except ValueError:
            return None
        if not isinstance(manifest, dict) or "prettier" not in manifest:
            return None
        return _settings(manifest["prettier"])
    if name == "package.yaml":
        return None if not re.search(r"(?m)^prettier\s*:", text) else _UNKNOWN
    if name.endswith(".toml"):
        try:
            return _settings(tomllib.loads(text))
        except tomllib.TOMLDecodeError:
            return _UNKNOWN
    if name in (".prettierrc", ".prettierrc.json", ".prettierrc.json5"):
        try:
            return _settings(json.loads(text))
        except ValueError:
            if name != ".prettierrc":
                return _UNKNOWN
    if name in (".prettierrc", ".prettierrc.yaml", ".prettierrc.yml"):
        return _yaml_settings(text)
    return _UNKNOWN


def _settings(config: object) -> object:
    """The width a parsed Prettier configuration gives, by :func:`_prettier_width`'s values."""
    if not isinstance(config, dict) or "overrides" in config:
        return _UNKNOWN
    if config.get("singleAttributePerLine") not in (None, False, "false"):
        # Prettier keeps two or more JSX attributes one per line, and a join
        # is told only the width.
        return _UNKNOWN
    width = config.get("printWidth")
    if width is None:
        return True
    return width if isinstance(width, int) and not isinstance(width, bool) and width > 0 else _UNKNOWN


def _yaml_settings(text: str) -> object:
    """The same, for a YAML file, read only as flat ``key: value`` lines."""
    config: dict[str, object] = {}
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped or stripped == "---":
            continue
        if line[:1].isspace() or ":" not in stripped:
            # Nested structure: per-file overrides or a value this cannot read.
            return _UNKNOWN
        key, value = (part.strip() for part in stripped.split(":", 1))
        if not value:
            return _UNKNOWN
        config[key] = int(value) if value.isdigit() else value
    return _settings(config)


def _editorconfig_width(path: str, repo_dir: str) -> int | str | None:
    """The ``max_line_length`` EditorConfig gives ``path``: a number, ``"off"``, or ``None``."""
    files: list[Path] = []
    for directory in _directories(path, repo_dir):
        candidate = directory / ".editorconfig"
        if not candidate.is_file():
            continue
        files.append(candidate)
        try:
            if re.search(r"(?mi)^\s*root\s*=\s*true\s*$", candidate.read_text(encoding="utf-8")):
                break
        except (OSError, UnicodeDecodeError):
            return None
    value: str | None = None
    target = Path(path).resolve()
    # The file nearest the source is read last, so it wins.
    for candidate in reversed(files):
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        relative = target.relative_to(candidate.parent.resolve()).as_posix()
        matches = False
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped[0] in "#;":
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                matches = _glob_matches(stripped[1:-1], relative)
                continue
            if matches and "=" in stripped:
                key, setting = (part.strip() for part in stripped.split("=", 1))
                if key.lower() == "max_line_length":
                    value = setting.lower()
    if value is None:
        return None
    if value.isdigit() and int(value) > 0:
        return int(value)
    return "off" if value == "off" else None


def _glob_matches(pattern: str, relative: str) -> bool:
    """Whether an EditorConfig section ``pattern`` covers ``relative``."""
    regex = _glob_regex(pattern)
    if regex is None:
        return False
    if "/" not in pattern:
        regex = "(?:.*/)?" + regex
    elif pattern.startswith("/"):
        regex = regex[1:] if regex.startswith("/") else regex
    return re.fullmatch(regex, relative) is not None


def _glob_regex(pattern: str) -> str | None:
    """The regular expression for an EditorConfig glob; ``None`` for a
    numeric range, which this does not read."""
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : close]
                negate = body.startswith("!")
                body = body[1:] if negate else body
                out.append("[" + ("^" if negate else "") + body.replace("\\", "\\\\") + "]")
                i = close + 1
                continue
        elif c == "{":
            close = pattern.find("}", i + 1)
            if close != -1 and re.fullmatch(r"-?\d+\.\.-?\d+", pattern[i + 1 : close]):
                return None
            out.append("(?:")
            depth += 1
        elif c == "}" and depth:
            out.append(")")
            depth -= 1
        elif c == "," and depth:
            out.append("|")
        elif c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        else:
            out.append(re.escape(c))
        i += 1
    if depth:
        return None
    return "".join(out)
