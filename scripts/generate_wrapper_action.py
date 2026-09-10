#!/usr/bin/env python3
"""Generate the public wrapper repository's `action.yml`.

The wrapper repository is what customers write `uses:` against. This file is
the only thing in it that differs from what lives beside this script: the
version here builds the image from source (`image: 'Dockerfile'`), which no
customer can do, because the source is not public. The published copy names an
exact image digest instead.

Exactly one line changes. The inputs, their documentation and the `runs.env`
bridge are copied verbatim, because a hand-maintained second copy drifts —
and an input that loses its `FEATUREFLIP_*` entry is silently ignored by the
container, which presents as the Action misbehaving rather than as a
packaging bug.

Stdlib only, and deliberately outside `src/`: this is release tooling the
customer's image has no use for, and importing `flag_cleanup` executes its
`__init__`, which imports polyglot-piranha to rewrite one line of YAML.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: The published image. It shares a name with the wrapper repository, but they
#: are different things — a GHCR package and a GitHub repository.
IMAGE = "ghcr.io/canopy-labs/featureflip-flag-cleanup-action"

#: The line the source `action.yml` carries, verbatim including its indent.
SOURCE_IMAGE_LINE = "  image: 'Dockerfile'"

#: A digest, not a tag. `v1` in a customer's workflow is expected to move; the
#: commit it resolves to must still determine exactly one image.
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class GenerationError(RuntimeError):
    """The wrapper file could not be generated, and none was written."""


def generate(source: str, digest: str, image: str = IMAGE) -> str:
    """Return `source` with its image reference replaced by `image@digest`."""
    if not _DIGEST.fullmatch(digest):
        raise GenerationError(
            f"refusing to generate: {digest!r} is not a sha256 digest. A tag can "
            f"be moved, which would give up the one property this indirection "
            f"buys — that the commit `v1` resolves to determines exactly one image."
        )

    lines = source.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.rstrip("\n") == SOURCE_IMAGE_LINE]
    if len(hits) != 1:
        raise GenerationError(
            f"expected exactly one {SOURCE_IMAGE_LINE!r} line in the source "
            f"action.yml, found {len(hits)}. Generation is a one-line "
            f"substitution; a source that no longer has that shape needs a human "
            f"to look at it, not a guess."
        )

    index = hits[0]
    newline = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"  image: 'docker://{image}@{digest}'{newline}"
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", required=True, help="sha256:… of the pushed manifest")
    parser.add_argument(
        "--source", type=Path, required=True, help="path to this repository's action.yml"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="where to write the generated file"
    )
    parser.add_argument("--image", default=IMAGE, help=f"image name (default {IMAGE})")
    args = parser.parse_args(argv)

    try:
        generated = generate(
            args.source.read_text(encoding="utf-8"), args.digest, args.image
        )
    except GenerationError as exc:
        # Nothing is written on this path: a release that cannot produce a
        # correct wrapper must fail before it publishes an incorrect one.
        print(f"generate_wrapper_action: {exc}", file=sys.stderr)
        return 1

    args.output.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
