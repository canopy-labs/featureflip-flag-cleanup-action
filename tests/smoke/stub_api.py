#!/usr/bin/env python3
"""A fixed removal-candidates payload, served over HTTP for the smoke jobs.

Not a convenience: the real endpoint deliberately omits `CreatedUnused` and
`ZeroTraffic` flags, so a flag created for a smoke run and never evaluated is
never returned. A genuinely `Dead` flag needs real traffic history that then
went one way — not something a CI job can seed, and not stable between runs.

Stdlib only, because it also runs on a bare runner with no checkout installed.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

#: The token the smoke passes as `api-token`. Fake, and never a real one — the
#: whole point of the stub is that no real credential is involved.
SMOKE_TOKEN = "smoke-token"

CANDIDATE = {
    "key": "smoke-flag",
    # The real endpoint returns a bare reason code, not prose. Matching that
    # keeps the smoke's output the same shape as production's.
    "reason": "StuckRolledOut",
    "treatment": True,
}


class _Handler(BaseHTTPRequestHandler):
    record_path: Path | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)

        if self.record_path is not None:
            with self.record_path.open("a", encoding="utf-8") as handle:
                # The token itself is never recorded, only whether it arrived.
                # A workflow log is world-readable on a public repository, and
                # "log the secret to prove it was passed" is a habit worth not
                # forming even when the secret is fake.
                handle.write(
                    json.dumps(
                        {
                            "path": parts.path,
                            "query": query,
                            "authorization_ok": self.headers.get("Authorization")
                            == f"Bearer {SMOKE_TOKEN}",
                        }
                    )
                    + "\n"
                )

        if parts.path == "/healthz":
            self._respond(200, {"ok": True})
            return

        if not parts.path.endswith("/flags/removal-candidates"):
            self._respond(404, {"error": f"no stub for {parts.path}"})
            return

        # Echo the requested tier back as the status, so a `staleness` input
        # that failed to bridge shows up as the wrong word in the output rather
        # than as an identical-looking pass.
        tier = (query.get("staleness") or ["dead"])[0]
        status = "Stale" if tier == "stale" else "Dead"
        self._respond(200, {"items": [{**CANDIDATE, "status": status}], "next_cursor": None})

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the per-request stderr line; `--record` is the audit trail."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--record", type=Path, default=None, help="append one JSON line per request"
    )
    args = parser.parse_args()

    if args.record is not None:
        args.record.write_text("", encoding="utf-8")
        _Handler.record_path = args.record

    # 0.0.0.0, not localhost: the client is a container reaching the host across
    # the docker bridge, so a loopback-only bind is unreachable from it.
    ThreadingHTTPServer(("0.0.0.0", args.port), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
