#!/usr/bin/env bash
# Build the git repository the smoke runs against.
#
# A repository, not a bare directory: the tool's preflight refuses a directory
# that is not inside a git work tree, and refuses one with uncommitted changes
# to tracked files (it undoes its own edits between flags and cannot tell those
# apart from yours). Both are satisfied by a normal `actions/checkout`; here
# they have to be arranged.
set -euo pipefail

DEST="${1:?usage: make_repo.sh <destination>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Checked, not assumed: `git` is not guaranteed on a self-hosted runner image,
# and a green `actions/checkout` does not prove it is there — that action falls
# back to downloading a REST tarball when git is absent. Without this the run
# dies on line 18 with a bare "git: command not found" that reads like a bug in
# the fixture rather than a missing dependency on the runner.
if ! command -v git >/dev/null 2>&1; then
  echo "make_repo.sh: git is not on PATH. The smoke fixture must be a git" >&2
  echo "repository (the tool's preflight refuses anything else), so install" >&2
  echo "git on this runner or run the smoke somewhere that has it." >&2
  exit 1
fi

rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$HERE/repo/." "$DEST/"

git -C "$DEST" init -q -b master
git -C "$DEST" config user.name "smoke"
git -C "$DEST" config user.email "smoke@example.invalid"
git -C "$DEST" add -A
git -C "$DEST" commit -q -m "smoke fixture"

echo "$DEST"
