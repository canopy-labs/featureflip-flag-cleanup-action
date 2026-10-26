"""Put a parameter or argument list a deletion shrank to one element back on one line.

The TypeScript-family passes that remove a parameter or an argument splice
bytes out of a list. When the list was laid out one element per line, the one
that is left keeps that layout::

    function renderChecklist(
      steps: number
    ) {

which a formatter, or a reviewer, writes as
``function renderChecklist(steps: number) {``. :func:`splice` makes the
deletions and then joins each such list, so the edit that shrank the list is
the edit that tidies it, and nothing the run did not shrink is touched.

A list is joined only when all of this holds, and is left exactly as the
deletion left it otherwise:

* it is a function's **parameters** or a call's **arguments** (object and
  array literals, type parameters and JSX attributes are left alone: a
  formatter keeps an object it finds expanded, and the rest are not edited
  by these passes);
* it held two or more elements before the deletions and holds exactly one
  after, so a list the customer wrote with one element is never rewritten;
* the remaining element is on one line of its own, the opening bracket ends
  its line and the closing bracket starts its line, with only whitespace and
  at most one trailing comma between them and the element;
* no comment sits anywhere inside the brackets;
* the joined line fits the width :func:`line_limit` reads for the file.

The trailing comma goes with the line breaks: ``f(a)`` rather than ``f(a,)``,
as a formatter writes a list that fits on one line.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk

#: The lists this joins: a function's parameters and a call's arguments.
_LISTS = frozenset({"formal_parameters", "arguments"})

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
    for open_end, element_start, element_end, close_start in sorted(
        (tuple(_moved(offset, ordered) for offset in join) for join in joins), reverse=True
    ):
        joined = _joined(data, open_end, element_start, element_end, close_start, width)
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


def _shrunk_lists(
    source: str, spans: list[tuple[int, int]], language: str
) -> list[tuple[int, int, int, int]]:
    """``(open bracket end, element start, element end, close bracket start)``,
    in ``source``'s offsets, for each list ``spans`` shrink to one element."""
    if not spans:
        return []
    root = _parse(source, language)
    found: list[tuple[int, int, int, int]] = []
    for container in _walk(root):
        if container.type not in _LISTS or len(container.children) < 2:
            continue
        if not any(container.start_byte < end and start < container.end_byte for start, end in spans):
            continue
        if any(node.type == "comment" for node in _walk(container)):
            continue
        elements = list(container.named_children)
        if len(elements) < 2:
            continue
        kept = [e for e in elements if not any(s <= e.start_byte and e.end_byte <= t for s, t in spans)]
        if len(kept) != 1 or _cuts(kept[0], spans) or _cuts_bracket(container, spans):
            continue
        found.append(
            (
                container.children[0].end_byte,
                kept[0].start_byte,
                kept[0].end_byte,
                container.children[-1].start_byte,
            )
        )
    return found


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


def _joined(
    data: bytes,
    open_end: int,
    element_start: int,
    element_end: int,
    close_start: int,
    width: int,
) -> bytes | None:
    """``data`` with the list between the brackets on one line, or ``None``."""
    before = data[open_end:element_start]
    element = data[element_start:element_end]
    after = data[element_end:close_start]
    if before.strip() or b"\n" not in before or b"\n" in element:
        return None
    if not _TRAILING.fullmatch(after) or b"\n" not in after:
        return None
    # Bracket ends its line, bracket starts its line: whitespace on both
    # sides of the element, line breaks included, and nothing else.
    line_start = data.rfind(b"\n", 0, open_end) + 1
    line_end = data.find(b"\n", close_start)
    line_end = len(data) if line_end == -1 else line_end
    line = data[line_start:open_end] + element + data[close_start:line_end]
    if _width(line.rstrip(b"\r").decode("utf-8")) > width:
        return None
    return data[:open_end] + element + data[close_start:]


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
