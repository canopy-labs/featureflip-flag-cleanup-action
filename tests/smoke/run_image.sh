#!/usr/bin/env bash
# Run a locally-built image the way the runner runs it, and check the output.
#
# This exists for one trap: `pip install -e '.[dev]'` — what the suite and every
# developer use — never builds a wheel, so a packaging error that keeps
# `rules/*.toml` out of the IMAGE is invisible to every test in the suite. Such
# an image starts fine and fails on the first flag, which is why the assertions
# below are on a real transform rather than on the container merely exiting 0.
set -euo pipefail

IMAGE="${1:?usage: run_image.sh <image-ref>}"
PORT="${SMOKE_PORT:-8787}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Under `$RUNNER_TEMP`, not a bare `mktemp -d`, because of the `-v` below.
# On our self-hosted (ARC) runners `dockerd` lives in a SIDECAR container with
# its own mount namespace, and only `/home/runner/_work`, `/certs` and
# `/home/runner/externals` are shared with the runner container — never `/tmp`.
# A `/tmp/tmp.XXXX` path therefore names one directory on the runner and a
# DIFFERENT, empty one that dockerd creates for itself; the container gets the
# empty one, the preflight refuses ("not inside a git work tree") and the run
# exits 2. `$RUNNER_TEMP` is `/home/runner/_work/_temp`, mounted identically on
# both sides, so the bind resolves to the same bytes. The `/tmp` fallback keeps
# this working for local runs outside Actions.
WORK="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flag-cleanup-smoke.XXXXXX")"
trap 'kill "${STUB_PID:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

"$HERE/make_repo.sh" "$WORK/repo" >/dev/null

# Redirected, not inherited: an un-redirected background process keeps the
# step's stdout pipe open and the caller never returns.
python3 "$HERE/stub_api.py" --port "$PORT" >"$WORK/stub.log" 2>&1 &
STUB_PID=$!

for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null; then break; fi
  sleep 1
done

# Loud, not just non-zero: `-s` suppresses curl's own message, and the trap
# deletes stub.log on the way out — this is the only chance to show why the
# stub never came up (port in use, python3 crashed, bind failure) before the
# evidence is gone.
if ! curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
  echo "smoke: the stub API never became healthy on port ${PORT}" >&2
  echo "smoke: stub log follows" >&2
  cat "$WORK/stub.log" >&2
  exit 1
fi

# `--add-host` rather than a hardcoded 172.17.0.1: `host-gateway` resolves to
# whatever the bridge gateway actually is. Inside the container 127.0.0.1 is
# the container.
set +e
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -v "$WORK/repo:/github/workspace" \
  -w /github/workspace \
  -e FEATUREFLIP_API_TOKEN=smoke-token \
  -e FEATUREFLIP_ORG=smoke-org \
  -e FEATUREFLIP_PROJECT=smoke-project \
  -e "FEATUREFLIP_API_URL=http://host.docker.internal:${PORT}" \
  -e FEATUREFLIP_LANGUAGES=ts \
  -e FEATUREFLIP_DRY_RUN=true \
  "$IMAGE" >"$WORK/out.txt" 2>&1
STATUS=$?
set -e

cat "$WORK/out.txt"

if [ "$STATUS" -ne 0 ]; then
  echo "smoke: the image exited ${STATUS}, expected 0" >&2
  exit 1
fi

# A dry run needs no GITHUB_TOKEN and makes no GitHub request, so exit 0 here
# means the fetch, the transform and the diff all ran.
if ! grep -qF '[dry-run] smoke-flag (status=Dead, treatment=True)' "$WORK/out.txt"; then
  echo "smoke: the candidate was not reported as a dry-run proposal" >&2
  exit 1
fi

# The transform itself, not just the run: without the rule files Piranha errors
# and the line above would read [piranha-error].
if ! grep -q '^-.*boolVariation' "$WORK/out.txt"; then
  echo "smoke: no diff removing the boolVariation call — are rules/*.toml in the image?" >&2
  exit 1
fi

echo "smoke: OK"
