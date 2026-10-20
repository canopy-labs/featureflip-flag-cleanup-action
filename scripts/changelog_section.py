#!/usr/bin/env python3
"""Print the `CHANGELOG.md` section for one version, the body of its Release.

    python3 scripts/changelog_section.py 1.7.0

A version's section runs from its `## 1.7.0` heading to the next `## `
heading. Its text, without the heading, is printed to stdout. Unreleased
changes collect under `## Unreleased`, and cutting a release renames that
heading to the version in the same change that bumps `pyproject.toml`.

A release whose version has no section, or an empty one, is refused: exit 1,
with the reason on stderr. The release job runs this before it builds
anything, so a missing section stops the release instead of publishing one
whose notes say nothing about what changed.

Stdlib only, like the other scripts here: the release job runs it on a bare
runner before installing anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(text: str, version: str) -> str | None:
    """The body under `## <version>`, stripped; None when there is no such heading.

    Only a level-2 heading starts or ends a section, so the `###` subheadings
    inside one belong to it.
    """
    lines = text.splitlines()
    heading = f"## {version}"
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == heading)
    except StopIteration:
        return None
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: changelog_section.py <version>", file=sys.stderr)
        return 2
    version = argv[1]
    body = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if body is None:
        print(
            f"CHANGELOG.md has no '## {version}' section. Rename '## Unreleased' "
            f"to '## {version}' in the change that bumps the version.",
            file=sys.stderr,
        )
        return 1
    if not body:
        print(f"CHANGELOG.md's '## {version}' section is empty.", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
