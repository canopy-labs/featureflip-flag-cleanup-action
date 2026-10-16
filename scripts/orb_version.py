#!/usr/bin/env python3
"""Decide whether `orb/orb.yml` needs publishing as a new CircleCI orb version.

`orb/VERSION` is the orb's version, set by hand for the same reason
`pyproject.toml`'s is: whether a parameter change is breaking is a judgment,
not something a script can read off a diff. The orb's version is independent
of the Action's. Its default image floats on the Action's major tag, so an
Action release changes nothing an orb user runs, and only an edit to
`orb.yml` itself needs a new orb version.

Published orb versions are immutable, and the registry hands back each
version's source byte for byte, without authentication. That is enough to
tell the three states apart:

- `orb/VERSION` is not in the registry: publish it.
- It is, with the same source: nothing to do. Every push that does not touch
  the orb lands here, as does a re-run of a release that already published.
- It is, with DIFFERENT source: `orb.yml` changed and nobody bumped
  `orb/VERSION`. That is an error, raised before anything is published,
  because the registry would refuse the publish anyway and a quiet skip would
  leave the change unreleased with nothing to say so.

A version that is new but not greater than every published one is refused
too, so a typo cannot publish a `1.0.1` after a `1.1.0`.

Stdlib only, like the other scripts here: the release job runs it on a bare
runner before installing anything.

Exit status is 0 when the orb is either up to date or ready to publish, 1 on
any refusal or registry error. With `--expect-published` the "ready to
publish" state is a failure too, which is how the release checks that its
publish actually landed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ORB = "featureflip/flag-cleanup"

#: The registry's GraphQL endpoint. The CircleCI CLI reads orb sources from it,
#: and orb reads need no token, which is what lets a pull request check this
#: with no credential at all.
REGISTRY = "https://circleci.com/graphql-unstable"

#: The registry returns versions newest first. A page this size covers every
#: version this orb will plausibly ever have; `fetch_published` refuses rather
#: than guesses if it is ever full.
PAGE = 200

TOOL_ROOT = Path(__file__).resolve().parent.parent
ORB_FILE = TOOL_ROOT / "orb" / "orb.yml"
VERSION_FILE = TOOL_ROOT / "orb" / "VERSION"

_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


class OrbVersionError(RuntimeError):
    """The orb cannot be published as it stands, and nothing was."""


def parse(version: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise OrbVersionError(f"{version!r} is not a plain X.Y.Z version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    parse(version)
    return version


def fetch_published(orb: str = ORB) -> dict[str, str]:
    """Return every published version of `orb`, mapped to its source."""
    body = json.dumps(
        {
            "query": "query($name: String!, $count: Int!) "
            "{ orb(name: $name) { versions(count: $count) { version source } } }",
            "variables": {"name": orb, "count": PAGE},
        }
    ).encode()
    request = urllib.request.Request(
        REGISTRY,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)

    if payload.get("errors"):
        raise OrbVersionError(f"the registry returned errors: {payload['errors']}")
    node = (payload.get("data") or {}).get("orb")
    if node is None:
        # Not "no versions yet": creating the orb is a one-time manual step,
        # and a missing orb means the name here is wrong or it was deleted.
        raise OrbVersionError(f"the registry has no orb named {orb}")
    versions = node["versions"]
    if len(versions) >= PAGE:
        raise OrbVersionError(f"{orb} has {PAGE}+ versions; raise PAGE before trusting this check")
    return {entry["version"]: entry["source"] for entry in versions}


def decide(version: str, source: str, published: dict[str, str]) -> bool:
    """True when `version` should be published, False when it already is.

    Raises when publishing would be wrong: the version exists with other
    source, or it would not be the newest version.
    """
    if version in published:
        if published[version] == source:
            return False
        raise OrbVersionError(
            f"orb/orb.yml has changed since {ORB}@{version} was published, and "
            f"published versions are immutable. Bump orb/VERSION: patch for a "
            f"fix, minor for a new optional parameter, major for anything an "
            f"existing pipeline could break on."
        )

    released = [parse(v) for v in published if _SEMVER.fullmatch(v)]
    if released and parse(version) <= max(released):
        newest = ".".join(str(part) for part in max(released))
        raise OrbVersionError(
            f"orb/VERSION is {version}, which is not newer than the published {newest}"
        )
    return True


def _output(**values: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--expect-published",
        action="store_true",
        help="fail unless orb/VERSION is already published with this source",
    )
    args = parser.parse_args(argv)

    try:
        version = read_version()
        # Bytes, decoded, rather than `read_text`, which translates line
        # endings. The registry stores exactly what was uploaded, so a
        # translation here would report a change that is not there.
        source = ORB_FILE.read_bytes().decode("utf-8")
        publish = decide(version, source, fetch_published())
    except (OrbVersionError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    ref = f"{ORB}@{version}"
    if publish and args.expect_published:
        print(f"error: {ref} is not in the registry", file=sys.stderr)
        return 1

    print(f"{ref} is new; publish it" if publish else f"{ref} is published and up to date")
    _output(publish="true" if publish else "false", version=version, ref=ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
