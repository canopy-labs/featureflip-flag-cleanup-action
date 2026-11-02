#!/usr/bin/env python3
"""Gather the unreleased changelog notes into a version's `CHANGELOG.md` section.

    python3 scripts/changelog_release.py 2.2.0

Every change that has not been released yet is a note of its own in
`changelog.d/`, one file per pull request, never an edit to `CHANGELOG.md`. A
shared `## Unreleased` section made every pending pull request conflict with
each one that merged before it. A note is Markdown under one or more of the
headings in `CATEGORIES`:

    ### Fixed

    - **Go:** ...

This writes `## <version>` above the newest section, with each category's
notes in `CATEGORIES` order and, within one, in file-name order, then deletes
the notes. The release pull request runs it in the change that bumps
`pyproject.toml`. It refuses, changing nothing, when there are no notes, when
a note is malformed, or when the version already has a section.

Stdlib only, like the other scripts here.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = TOOL_ROOT / "CHANGELOG.md"
NOTES = TOOL_ROOT / "changelog.d"

#: The headings a note may use, in the order a section lists them.
CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")


class NoteError(ValueError):
    """A note `changelog.d/` cannot be gathered from."""


def notes(directory: Path = NOTES) -> list[Path]:
    """The notes waiting for a release, in file-name order."""
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.md") if path.name != "README.md")


def parse(text: str, name: str) -> dict[str, str]:
    """A note's body per category, stripped.

    Raises :class:`NoteError` for text before the first heading, a heading
    that is not one of `CATEGORIES`, a category with nothing under it, or a
    note with no category at all.
    """
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if not line.startswith("### ") or heading not in CATEGORIES:
                raise NoteError(
                    f"{name}: {line!r} is not one of "
                    + ", ".join(f"'### {c}'" for c in CATEGORIES)
                )
            current = found.setdefault(heading, [])
        elif current is None:
            if line.strip():
                raise NoteError(f"{name}: text before the first '### ' heading")
        else:
            current.append(line)
    if not found:
        raise NoteError(f"{name}: no '### ' heading")
    bodies = {category: "\n".join(lines).strip() for category, lines in found.items()}
    for category, body in bodies.items():
        if not body:
            raise NoteError(f"{name}: '### {category}' is empty")
    return bodies


def section(paths: list[Path]) -> str:
    """The body of a version's section, gathered from `paths`."""
    gathered: dict[str, list[str]] = {}
    for path in paths:
        for category, body in parse(path.read_text(encoding="utf-8"), path.name).items():
            gathered.setdefault(category, []).append(body)
    return "\n\n".join(
        f"### {category}\n\n" + "\n".join(gathered[category])
        for category in CATEGORIES
        if category in gathered
    )


def release(version: str, changelog: Path = CHANGELOG, directory: Path = NOTES) -> None:
    """Write `## <version>` from the notes into `changelog` and delete the notes."""
    paths = notes(directory)
    if not paths:
        raise NoteError(f"no notes in {directory.name}/ to release")
    text = changelog.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if any(line.rstrip() == f"## {version}" for line in lines):
        raise NoteError(f"{changelog.name} already has a '## {version}' section")
    body = section(paths)
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    head = "".join(lines[:first]).rstrip("\n") + "\n\n"
    rest = "".join(lines[first:])
    changelog.write_text(
        head + f"## {version}\n\n{body}\n" + ("\n" + rest if rest else ""), encoding="utf-8"
    )
    for path in paths:
        path.unlink()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: changelog_release.py <version>", file=sys.stderr)
        return 2
    try:
        release(argv[1])
    except NoteError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
