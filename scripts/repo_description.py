#!/usr/bin/env python3
"""Derive the public wrapper repository's GitHub "About" description.

The repository description is the one customer-facing statement of what this
Action supports that lives OUTSIDE any repository, so it appears in no diff and
no review ever sees it. It drifted exactly the way that predicts: it advertised
five languages (TypeScript, Java, Go, Python, Kotlin) while the registry had
twelve, and Ruby, Dart, PHP, TSX, JavaScript, C# and Swift had all shipped
behind it unannounced. `tests/test_packaging.py` already couples every roster
surface INSIDE the repository to `_LANGUAGES`; this closes the one it cannot
reach by making the description generated rather than typed.

Stdlib only, and deliberately outside `src/`, for the same two reasons
`generate_wrapper_action.py` gives: this is release tooling the customer's
image has no use for, and importing `flag_cleanup` executes its `__init__`,
which loads polyglot-piranha to read a dozen string literals.

That is why the roster is recovered with `ast` instead of an import. The parse
is narrow on purpose, and `tests/test_packaging.py` asserts it returns exactly
what the imported registry does, so a registry that changes shape enough to
defeat this walk fails a test rather than silently producing a short list.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: GitHub rejects a longer description outright. Twelve languages spend ~205 of
#: it, so this is headroom rather than a live constraint — but a release that
#: fails on a 400-character API error is a worse way to learn that than this.
MAX_LENGTH = 350

#: The prose around the roster. Deliberately a constant and not derived from
#: README.md: the wording is an editorial choice, and only the roster inside it
#: is a fact about the code. Same split as `action.yml`'s own description, which
#: `test_the_action_description_names_every_supported_language` checks for
#: coverage rather than pinning word for word.
TEMPLATE = (
    "GitHub Action that deterministically removes dead Featureflip feature "
    "flags from {languages} source and opens one pull request per flag."
)

#: The registry `run_piranha` dispatches on. Named here so a rename fails loudly
#: below instead of yielding an empty roster.
REGISTRY = "_LANGUAGES"


class DescriptionError(RuntimeError):
    """The description could not be derived, and none was printed."""


def display_names(runner_source: str) -> list[str]:
    """Return each language's `display_name`, in registry order.

    Keyword-only by construction: `_Language.display_name` is declared
    `field(kw_only=True)`, so every entry must spell it as a keyword and a
    positional walk would be wrong rather than merely fragile.
    """
    try:
        tree = ast.parse(runner_source)
    except SyntaxError as exc:  # pragma: no cover - a broken runner fails earlier
        raise DescriptionError(f"could not parse the runner module: {exc}") from exc

    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
        if target != REGISTRY or not isinstance(node.value, ast.Dict):
            continue

        names: list[str] = []
        for token, call in zip(node.value.keys, node.value.values):
            if not isinstance(call, ast.Call):
                raise DescriptionError(
                    f"{REGISTRY} entry {ast.unparse(token)} is not a call, so its "
                    f"display name cannot be read. The walk here is deliberately "
                    f"narrow; widen it consciously rather than by accident."
                )
            found = [
                kw.value.value
                for kw in call.keywords
                if kw.arg == "display_name" and isinstance(kw.value, ast.Constant)
            ]
            if len(found) != 1:
                raise DescriptionError(
                    f"{REGISTRY} entry {ast.unparse(token)} has {len(found)} literal "
                    f"display_name keywords, expected exactly 1."
                )
            names.append(found[0])

        if not names:
            raise DescriptionError(f"{REGISTRY} parsed as empty.")
        return names

    raise DescriptionError(
        f"no {REGISTRY} dict assignment found. If the registry was renamed, "
        f"update REGISTRY here in the same change."
    )


def join(names: list[str]) -> str:
    """`A, B and C` — an Oxford-comma-free list, matching the README's roster."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def build(runner_source: str) -> str:
    """Return the description text for the current registry."""
    description = TEMPLATE.format(languages=join(display_names(runner_source)))
    if len(description) > MAX_LENGTH:
        raise DescriptionError(
            f"the derived description is {len(description)} characters, over "
            f"GitHub's {MAX_LENGTH} limit. Shorten TEMPLATE rather than dropping "
            f"a language from the roster, which is the one thing it must state."
        )
    return description


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner",
        type=Path,
        required=True,
        help="path to src/flag_cleanup/piranha_runner.py",
    )
    args = parser.parse_args(argv)

    try:
        print(build(args.runner.read_text(encoding="utf-8")))
    except DescriptionError as exc:
        # Nothing is printed on this path, so a caller substituting this into a
        # `gh repo edit` gets an empty argument and a failed step rather than a
        # repository described by an error message.
        print(f"repo_description: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
