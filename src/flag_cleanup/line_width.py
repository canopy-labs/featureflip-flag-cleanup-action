"""The line width a Python or Dart file is formatted to, read off the repository.

The engine puts a layout its own edit made unnecessary back on one line: a
Python ``(``/``)`` pair whose one element now fits, a Dart initializer that
fits after its ``=``. It never works out a width itself, because the width is
a fact about the repository (a formatter's configuration), not about the
file, so the run hands it in per file as the ``print_width`` fact. A file
given no width has nothing joined.

**Python.** The nearest configuration above the file, up to the repository
root, decides: ``ruff.toml``/``.ruff.toml`` (``line-length``), a
``pyproject.toml`` with a ``[tool.ruff]`` or ``[tool.black]`` table
(``line-length``, 88 when the table names none; the narrower when both do),
then a ``[flake8]``/``[pycodestyle]`` section's ``max-line-length`` in
``setup.cfg``, ``tox.ini`` or ``.flake8``. With none of them, black's 88 or
the longest line the file already has, whichever is narrower, so a join never
writes a line longer than the customer's own. A configuration this cannot
read gives no width.

**Dart.** ``dart format`` reads ``formatter: page_width`` from the
``analysis_options.yaml`` nearest the file (following a relative
``include:``), and uses 80 without one. An ``include:`` of a package's
options is not followed: lint packages set no width, and one that did could
only make the width this reads too wide by its difference from 80.
"""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

import yaml

#: black's and ruff's default line length.
PYTHON_DEFAULT_WIDTH = 88

#: ``dart format``'s default page width.
DART_DEFAULT_WIDTH = 80

_TAB_WIDTH = 4


def python_line_limit(path: str, original: str, repo_dir: str) -> int | None:
    """The widest line a join may write in the Python file ``path`` (whose
    text is ``original``), or ``None`` to join nothing."""
    for directory in _directories(path, repo_dir):
        found = _python_config_width(directory)
        if found is _UNREADABLE:
            return None
        if isinstance(found, int):
            return found
    longest = max((len(line.expandtabs(_TAB_WIDTH)) for line in original.splitlines()), default=0)
    return min(PYTHON_DEFAULT_WIDTH, longest) or None


def dart_page_width(path: str, repo_dir: str) -> int | None:
    """The page width ``dart format`` uses for ``path``, or ``None`` when the
    configuration that sets it cannot be read."""
    for directory in _directories(path, repo_dir):
        options = directory / "analysis_options.yaml"
        if options.is_file():
            return _dart_options_width(options, repo_dir, seen=set())
    return DART_DEFAULT_WIDTH


class _Unreadable:
    pass


_UNREADABLE = _Unreadable()


def _python_config_width(directory: Path) -> int | _Unreadable | None:
    """The width the formatter configuration in ``directory`` sets, ``None``
    when the directory holds none."""
    for name in (".ruff.toml", "ruff.toml"):
        candidate = directory / name
        if candidate.is_file():
            data = _toml(candidate)
            if data is None:
                return _UNREADABLE
            return _positive(data.get("line-length", PYTHON_DEFAULT_WIDTH))
    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        data = _toml(pyproject)
        if data is None:
            return _UNREADABLE
        tool = data.get("tool", {})
        widths = [
            _positive(tool[name].get("line-length", PYTHON_DEFAULT_WIDTH))
            for name in ("ruff", "black")
            if isinstance(tool.get(name), dict)
        ]
        if widths:
            if any(not isinstance(width, int) for width in widths):
                return _UNREADABLE
            return min(widths)
    for name in ("setup.cfg", "tox.ini", ".flake8"):
        candidate = directory / name
        if not candidate.is_file():
            continue
        parser = configparser.RawConfigParser()
        try:
            parser.read(candidate, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            return _UNREADABLE
        for section in ("flake8", "pycodestyle"):
            if parser.has_option(section, "max-line-length"):
                return _positive(parser.get(section, "max-line-length").strip())
            if parser.has_option(section, "max_line_length"):
                return _positive(parser.get(section, "max_line_length").strip())
    return None


def _dart_options_width(options: Path, repo_dir: str, seen: set[Path]) -> int | None:
    resolved = options.resolve()
    if resolved in seen:
        return None
    seen.add(resolved)
    try:
        data = yaml.safe_load(options.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if data is None:
        return DART_DEFAULT_WIDTH
    if not isinstance(data, dict):
        return None
    formatter = data.get("formatter")
    if isinstance(formatter, dict) and "page_width" in formatter:
        width = _positive(formatter["page_width"])
        return width if isinstance(width, int) else None
    include = data.get("include")
    if isinstance(include, str) and not include.startswith("package:"):
        target = (options.parent / include).resolve()
        root = Path(repo_dir).resolve()
        if target.is_file() and target.is_relative_to(root):
            return _dart_options_width(target, repo_dir, seen)
        return None
    return DART_DEFAULT_WIDTH


def _toml(path: Path) -> dict | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _positive(value: object) -> int | _Unreadable:
    if isinstance(value, bool):
        return _UNREADABLE
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int) and value > 0:
        return value
    return _UNREADABLE


def _directories(path: str, repo_dir: str) -> list[Path]:
    """``path``'s directory and each one above it, up to ``repo_dir`` and never past it."""
    root = Path(repo_dir).resolve()
    directory = Path(path).resolve().parent
    found = []
    while True:
        found.append(directory)
        if directory == root or root not in directory.parents:
            break
        directory = directory.parent
    return found
