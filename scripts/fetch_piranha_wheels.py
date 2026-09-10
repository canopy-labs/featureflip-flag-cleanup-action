#!/usr/bin/env python3
"""Fetch the forked wheels this tool pins into wheels/.

TWO packages here exist only as GitHub-release wheels, and for the SAME
underlying reason -- each is a canopy-labs fork whose PyPI name belongs to
upstream:

* **polyglot-piranha**, the transform engine. PyPI's package of that name is
  upstream's, which tops out at 0.4.8 and has no C# support.
* **tree-sitter-typescript**, the grammar backing the safety gates in
  `flag_cleanup.ts_syntax`. PyPI has stock 0.23.2; the engine bundles 0.23.2
  plus one external-scanner patch, so a bare `&` in JSX text
  (`<p>Terms & Conditions</p>`, which `tsc` accepts) parses for the engine and
  ERRORS for the gate. Gate 1 must see the same node shapes the engine
  rewrites against -- when it does not, tree-sitter's error recovery
  re-partitions across an edit and the stranded-keyword and ASI-fusion checks
  read the shift as damage, refusing a correct rewrite. A Gate 1 refusal is
  flag-wide, so one such file abandons the whole flag on every run.

Every `pip install` of this tool therefore needs `--find-links wheels`, and
every Docker build bind-mounts the same directory; this script is the one
thing that fills it.

Each release tag is derived from pyproject.toml's own pin, so bumping a pin IS
bumping what gets fetched -- there is no second version literal to keep in
sync. The grammar's pin carries a PEP 440 LOCAL version (`0.23.2+canopy.3`),
which is what makes it unambiguous: PyPI forbids local versions, so that pin
can never be satisfied silently by upstream's identically-numbered 0.23.2.

Auth: PIRANHA_WHEEL_TOKEN (in CI: a repo-read fine-grained PAT on the fork,
stored as BOTH an Actions secret and a Dependabot secret -- a
Dependabot-triggered run reads only the Dependabot store), falling back to
`gh auth token` for local runs. The piranha fork is private, so a token is
required even though the grammar fork is public. stdlib-only on purpose: this
has to run before any pip install can.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

TOOL_ROOT = Path(__file__).resolve().parent.parent


class _WheelSource(NamedTuple):
    """One forked distribution, and how to find its wheels on a release.

    A ``NamedTuple`` rather than a dataclass on purpose: ``tests/`` loads this
    file with ``spec_from_file_location`` WITHOUT registering it in
    ``sys.modules``, and ``@dataclass`` looks its own module up there at class
    creation time -- so a dataclass here fails at import with a bare
    ``AttributeError`` on ``NoneType``, from a test that only wanted to parse a
    version.
    """

    #: The `name==version` key in pyproject.toml's dependencies. This is the
    #: ONLY place the version lives; `tag` derives the release from it.
    distribution: str
    repo: str
    #: Matched against each release asset name, with `{version}` substituted
    #: (regex-escaped) from the pin.
    pattern: str
    #: How many assets must match. Both sources ship exactly one wheel per
    #: architecture -- x86_64 for local development, aarch64 for the ARM CI
    #: runner and the ARM production image -- so a count that is not 2 means
    #: the release is malformed or the pattern has rotted, and installing a
    #: partial set would fail on one architecture only.
    expected_assets: int = 2

    def tag(self, version: str) -> str:
        """The release tag carrying this version's wheels.

        A PEP 440 local segment (`+canopy.1`) is spelled with a `-` in the tag:
        `+` is legal in a git ref but has to be percent-encoded in every API
        path that names it, which is friction for no benefit.
        """
        return "v" + version.replace("+", "-")


_WHEEL_SOURCES = (
    #: One python tag on purpose: the image is python:3.12-slim and
    #: requires-python caps at <3.13, so only cp312 wheels are ever installable
    #: here. The fork release ships cp39-cp314 -- widen this, the base image and
    #: requires-python TOGETHER if that ever changes.
    _WheelSource(
        distribution="polyglot-piranha",
        repo="canopy-labs/piranha",
        pattern=r"^polyglot_piranha-{version}-cp312-cp312-manylinux_.*\.whl$",
    ),
    #: abi3, so ONE wheel per architecture covers every interpreter this tool
    #: could run on (3.9+) -- unlike the engine above, this one does not have to
    #: move when the base image's Python does.
    _WheelSource(
        distribution="tree-sitter-typescript",
        repo="canopy-labs/tree-sitter-typescript",
        #: `manylinux.*`, not `manylinux_.*`: auditwheel tags a wheel with EVERY
        #: standard it satisfies, so these arrive as
        #: `manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64` and
        #: `manylinux2014_aarch64.manylinux_2_17_aarch64...` -- neither of which
        #: starts `manylinux_`. The engine's wheels above are maturin-built and
        #: really do start `manylinux_2_28`, which is why the two differ.
        pattern=r"^tree_sitter_typescript-{version}-cp39-abi3-manylinux.*\.whl$",
    ),
)


def pinned_version(pyproject: Path, distribution: str = "polyglot-piranha") -> str:
    """The exact version ``distribution`` is pinned to in ``pyproject``."""
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    pattern = re.compile(rf"{re.escape(distribution)}==(.+)")
    pins = [m.group(1) for dep in deps if (m := pattern.fullmatch(dep))]
    if len(pins) != 1:
        sys.exit(
            f"expected exactly one exact {distribution} pin in {pyproject}, found {pins!r}"
        )
    return pins[0]


def _token() -> str:
    token = os.environ.get("PIRANHA_WHEEL_TOKEN", "").strip()
    if token:
        return token
    try:
        token = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        token = ""
    if token:
        return token
    sys.exit(
        "no PIRANHA_WHEEL_TOKEN in the environment and `gh auth token` produced "
        "nothing. In CI that secret is a repo-read PAT on the piranha fork; "
        "locally, `gh auth login` is enough."
    )


def _request(url: str, token: str, *, accept: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


class _StopRedirect(urllib.request.HTTPRedirectHandler):
    """Surface the redirect instead of following it.

    GitHub answers an asset download with a redirect to short-lived storage
    whose URL is ALREADY signed. urllib re-sends every header on a redirect,
    Authorization included, and the storage backend rejects a request that
    carries two credentials. So: catch the Location, then fetch it bare.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _download(asset: dict, token: str, dest: Path) -> None:
    opener = urllib.request.build_opener(_StopRedirect())
    request = _request(asset["url"], token, accept="application/octet-stream")
    try:
        response = opener.open(request, timeout=60)
    except urllib.error.HTTPError as err:
        if err.code not in (301, 302, 303, 307, 308):
            raise
        response = urllib.request.urlopen(err.headers["Location"], timeout=300)
    with response, open(dest, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)


def _fetch_source(source: _WheelSource, dest_dir: Path, token: str) -> None:
    """Fetch every wheel ``source`` pins, into ``dest_dir``."""
    version = pinned_version(TOOL_ROOT / "pyproject.toml", source.distribution)
    tag = source.tag(version)

    try:
        with urllib.request.urlopen(
            _request(
                f"https://api.github.com/repos/{source.repo}/releases/tags/{tag}",
                token,
                accept="application/vnd.github+json",
            ),
            timeout=30,
        ) as response:
            release = json.load(response)
    except urllib.error.HTTPError as err:
        sys.exit(
            f"GET release {tag} of {source.repo}: HTTP {err.code}. A 404 usually "
            f"means the token cannot see the repo (or the tag was never released)."
        )

    pattern = re.compile(source.pattern.format(version=re.escape(version)))
    assets = [a for a in release["assets"] if pattern.match(a["name"])]
    if len(assets) != source.expected_assets:
        sys.exit(
            f"expected exactly {source.expected_assets} wheel(s) for "
            f"{source.distribution} on {tag}, matched {[a['name'] for a in assets]!r} "
            f"-- check the release and this source's pattern"
        )

    # A wheel left over from a previous pin would sit here forever otherwise
    # (the exact `==` pin means pip would never pick it, but the Docker build
    # context and the confusion both grow). Scoped to THIS distribution's
    # files: the directory now holds more than one project's wheels, and a
    # broader glob would delete the other one on every run.
    stem = source.distribution.replace("-", "_")
    for stale in dest_dir.glob(f"{stem}-*.whl"):
        if not pattern.match(stale.name):
            print(f"removing stale {stale.name}")
            stale.unlink()

    for asset in assets:
        dest = dest_dir / asset["name"]
        if dest.is_file() and dest.stat().st_size == asset["size"]:
            print(f"already present: {asset['name']}")
            continue
        print(f"fetching {asset['name']} ({asset['size'] / 1e6:.1f} MB)")
        _download(asset, token, dest)
        got = dest.stat().st_size
        if got != asset["size"]:
            sys.exit(f"{asset['name']}: downloaded {got} bytes, release says {asset['size']}")


def main() -> None:
    dest_dir = TOOL_ROOT / "wheels"
    dest_dir.mkdir(exist_ok=True)

    token = _token()
    for source in _WHEEL_SOURCES:
        _fetch_source(source, dest_dir, token)

    print(f"wheels ready in {dest_dir}")


if __name__ == "__main__":
    main()
