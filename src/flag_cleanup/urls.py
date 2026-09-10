"""Keep basic-auth credentials out of anything a human or a log will see.

``api-url`` explicitly supports self-hosted instances, so a URL carrying
``user:password@`` userinfo is a normal thing for a customer to configure. That
value then has three ways out of this tool, and each one leaked at some point:

* the pull-request body (world-readable on a public repository);
* httpx's INFO request log, which prints the full URL — silenced in ``__main__``;
* an ``httpx.HTTPStatusError``, whose message is built from ``str(request.url)``
  — and ``httpx.URL.__str__`` does NOT redact the password, only ``__repr__``
  does.

Three instances of one class, so the stripper lives here rather than next to
any single caller. Anything that renders a URL into text a person can read
goes through :func:`without_userinfo` first.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def without_userinfo(url: str) -> str:
    """Strip any ``user:password@`` from a URL, keeping the rest intact.

    The host is deliberately kept: a redacted URL still has to say which
    instance the request went to, or the message stops being diagnostic.

    ``urlsplit`` itself is extremely permissive, but ``SplitResult`` parses
    LAZILY: ``.hostname`` and ``.port`` split the authority on attribute
    access, and ``.port`` raises ``ValueError`` for anything non-numeric or
    outside 0-65535. Guarding only the ``urlsplit`` call therefore guarded
    nothing — ``https://user:pw@host:notaport/`` raised straight out of the one
    function whose job is to make an error message safe to print, from inside
    the handler building that message. Both attribute reads are inside the
    ``try`` for that reason, and the fallback REDACTS rather than passing the
    input through: an unparseable authority is exactly when the credential
    must not survive.
    """
    trimmed = url.rstrip("/")
    try:
        parts = urlsplit(trimmed)
        hostname, netloc, port = parts.hostname, parts.netloc, parts.port
    except ValueError:
        return redact_userinfo(trimmed)
    if not hostname or "@" not in netloc:
        return trimmed
    return urlunsplit(
        (
            parts.scheme,
            hostname + (f":{port}" if port else ""),
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def redact_userinfo(url: str) -> str:
    """``url`` with anything before the last ``@`` replaced, however malformed.

    The crude counterpart to :func:`without_userinfo`, for the inputs that one
    cannot parse: a value with no scheme (``user:pw@api.internal`` — urlsplit
    files the whole thing under ``path``, so the userinfo is invisible to it)
    or with an authority it chokes on. Cutting at the last ``@`` loses the
    scheme, which is a fine trade in a message that is already saying the URL
    is unusable.
    """
    if "@" not in url:
        return url
    return f"***@{url.rpartition('@')[2]}"
