"""Timeouts and a bounded retry for the reads both API clients make.

httpx's default is a 5-second timeout on every phase and no retry, so a single
request that stalled anywhere between the runner and the API — a proxy, the
CDN, a dropped connection — ended the whole run, though the API itself was
answering in milliseconds.

Only reads are retried. A write that timed out may still have landed, and
repeating it is not this module's call to make: the caller of a write already
decides what an unanswered one means.

Retrying happens around the call rather than inside a custom transport on
purpose. Passing ``transport=`` to ``httpx.Client`` switches off its reading of
``HTTPS_PROXY`` and friends, which a self-hosted runner behind a proxy relies
on; the clients therefore hand httpx a transport only when a test supplies one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

#: Long enough for a slow page of results, short enough that a dead
#: connection costs a run seconds rather than the job's whole budget.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Attempts per read, the first one included.
ATTEMPTS = 3

#: Seconds to wait before the second and third attempts.
BACKOFF_SECONDS = (1.0, 4.0)

#: Failures that say nothing about the request itself: it never reached the
#: server, or its answer never came back. An ``UnsupportedProtocol`` or a
#: ``LocalProtocolError`` would fail the same way every time, and an HTTP error
#: status is an answer, so none of those is retried.
TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

Sleep = Callable[[float], None]


def get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    sleep: Sleep = time.sleep,
) -> httpx.Response:
    """``client.get(url, params=params)``, retried on a transient failure.

    The last failure is re-raised unchanged, so a caller that already handles
    ``httpx.HTTPError`` sees exactly what it saw before, only later.

    ``url`` is logged, so it must be a path relative to the client's base URL:
    the base can carry basic-auth credentials, and ``str(httpx.URL)`` does not
    redact them.
    """
    for attempt in range(ATTEMPTS):
        try:
            return client.get(url, params=params)
        except TRANSIENT as exc:
            if attempt == ATTEMPTS - 1:
                raise
            delay = BACKOFF_SECONDS[attempt]
            logger.warning(
                "GET %s failed (%s), retrying in %gs (attempt %d of %d)",
                url,
                type(exc).__name__,
                delay,
                attempt + 2,
                ATTEMPTS,
            )
            sleep(delay)
    raise AssertionError("unreachable: the last attempt returns or raises")
