"""Parse Action inputs (environment variables) into a typed :class:`Config`.

This runs inside the CUSTOMER's CI. ``FEATUREFLIP_API_TOKEN`` is THEIR
Featureflip API token — never a Featureflip-internal secret (see the package
docstring in ``flag_cleanup/__init__.py``). ``GITHUB_TOKEN``/
``GITHUB_REPOSITORY`` are runner-provided and belong to ``github_ops``,
not this module.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from flag_cleanup.piranha_runner import supported_languages
from flag_cleanup.urls import redact_userinfo

# The Featureflip public API origin. Overridable (``FEATUREFLIP_API_URL``) so
# the Action can be pointed at a staging/self-hosted instance.
DEFAULT_API_URL = "https://api.featureflip.io"
DEFAULT_STALENESS = "dead"

#: What this invocation is for. ``remove`` is everything the Action did before
#: archive-on-merge existed, and stays the default so an existing workflow that
#: never sets ``mode`` keeps behaving identically.
#:
#: The two are separate invocations on separate triggers, never two phases of
#: one run: ``remove`` runs on a schedule against a checkout, ``archive-on-merge``
#: runs on a merged pull request and touches no source at all. Splitting them on
#: an explicit input rather than sniffing ``GITHUB_EVENT_NAME`` is deliberate —
#: archive mode needs a token that can WRITE to the customer's flags, and a
#: behaviour that turns itself on because somebody added a trigger for an
#: unrelated reason is the wrong way to reach for that.
DEFAULT_MODE = "remove"
MODE_REMOVE = "remove"
MODE_ARCHIVE_ON_MERGE = "archive-on-merge"
MODES = (MODE_REMOVE, MODE_ARCHIVE_ON_MERGE)

#: The endpoint's ``?staleness=`` enum, verbatim. Validated here rather than
#: sent blind: a typo would otherwise reach the API, come back 4xx, and surface
#: as a raw ``httpx.HTTPStatusError`` traceback — while every other malformed
#: input gets a clean message and exit 2.
STALENESS_VALUES = ("dead", "stale")

# Empty on purpose: unset means "ask the repository what its default branch
# is" (``orchestrate._resolve_base``). Guessing ``main`` here was wrong for
# every ``master`` repository, where it made GitHub reject every pull request
# — a guaranteed first-run failure, not an edge case.
DEFAULT_BASE_BRANCH = ""

#: Most pull requests one run may propose before it stops. Deliberately small.
#:
#: Every other safety argument in this tool bounds how wrong ONE pull request
#: can be; none of them bounded how MANY. A project with 200 dead flags would
#: have had 200 branches pushed and 200 pull requests opened against a
#: customer's repository on the first run — and if anything about that run was
#: wrong, all 200 were wrong. A limit converts that into a mistake somebody
#: notices at 10 and can stop.
#:
#: The remainder is not lost: the next scheduled run picks up where this one
#: stopped, because `already_handled` skips what has been proposed. Set
#: `max-prs: 0` to lift the limit once a repository has been through a few
#: rounds and the diffs are trusted.
DEFAULT_MAX_PRS = 10


class ConfigError(ValueError):
    """A required environment variable was missing or malformed."""


@dataclass(frozen=True, slots=True)
class Config:
    """Everything the orchestrator needs, resolved once at startup.

    ``api_token`` is kept OUT of ``__repr__``. This is the one secret-bearing
    object in the tool, and a ``Config`` is exactly the kind of thing that ends
    up inside a debug log line or an exception message ("failed with
    config=...") — at which point it is in the customer's workflow log, which
    is world-readable on a public repository.
    """

    api_token: str = field(repr=False)
    org: str
    project: str
    api_url: str = DEFAULT_API_URL
    #: See :data:`MODES`. Everything below this line except ``api_url`` is
    #: ``remove``-only; archive mode reads a merged pull request and makes one
    #: API call, so it needs no checkout and none of the transform inputs.
    mode: str = DEFAULT_MODE
    staleness: str = DEFAULT_STALENESS
    directories: tuple[str, ...] = (".",)
    languages: tuple[str, ...] = supported_languages()
    #: Extra function names to treat as flag reads, on top of each language's
    #: own SDK names. Empty is the historical behaviour. See
    #: :data:`_ACCESSOR_RE` for why these are held to a stricter shape than
    #: flag keys are.
    accessors: tuple[str, ...] = ()
    ignore: frozenset[str] = frozenset()
    #: Empty means "resolve the repository's default branch at run time".
    base_branch: str = DEFAULT_BASE_BRANCH
    pr_labels: tuple[str, ...] = ()
    dry_run: bool = False
    #: Most pull requests one run may propose; ``0`` means no limit. A bound on
    #: blast radius rather than on correctness — see :data:`DEFAULT_MAX_PRS`.
    max_prs: int = DEFAULT_MAX_PRS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a :class:`Config` from the process environment.

        Pass ``env`` explicitly in tests to avoid depending on real process
        state. Raises :class:`ConfigError` when a required variable
        (``FEATUREFLIP_API_TOKEN``/``_ORG``/``_PROJECT``) is missing or blank,
        or when ``FEATUREFLIP_LANGUAGES`` names something unsupported.

        Every value is stripped, and a **set-but-empty** variable falls back to
        its default rather than overriding it with ``""``. Both matter because
        of how Actions inputs reach here: an unset workflow input arrives as
        the empty string (not as an absent key), so ``.get(k, default)`` alone
        would hand ``api_url=""`` straight to httpx and fail at request time
        with ``ValueError: unknown url type``. A secret piped through a shell
        similarly arrives with a trailing newline, which would go into the
        ``Authorization`` header verbatim; an org with surrounding spaces would
        be percent-encoded into the request path as ``%20``.
        """
        env = os.environ if env is None else env
        required = ("FEATUREFLIP_API_TOKEN", "FEATUREFLIP_ORG", "FEATUREFLIP_PROJECT")
        # Whitespace-only counts as missing: it passes a bare truthiness check
        # but produces a malformed header or an unusable path.
        missing = [name for name in required if not _get(env, name, "")]
        if missing:
            raise ConfigError(f"missing required environment variable(s): {', '.join(missing)}")

        languages = _split_csv(
            _get(env, "FEATUREFLIP_LANGUAGES", ""), default=supported_languages()
        )
        unknown = [name for name in languages if name not in supported_languages()]
        if unknown:
            # Caught here rather than deep inside run_piranha, where it would
            # surface as a raw ValueError traceback part-way through a run.
            raise ConfigError(
                f"unsupported FEATUREFLIP_LANGUAGES value(s): {', '.join(unknown)}; "
                f"expected one or more of {', '.join(supported_languages())}"
            )

        accessors = _parse_accessors(_get(env, "FEATUREFLIP_ACCESSORS", ""))

        # Lower-cased before the check: the wire value is lowercase, but the
        # API's own `status` field is PascalCase, so "Dead" is an easy thing
        # for a human to write in a workflow.
        staleness = _get(env, "FEATUREFLIP_STALENESS", DEFAULT_STALENESS).lower()
        if staleness not in STALENESS_VALUES:
            raise ConfigError(
                f"unsupported FEATUREFLIP_STALENESS value: "
                f"{_get(env, 'FEATUREFLIP_STALENESS', DEFAULT_STALENESS)!r}; "
                f"expected one of {', '.join(STALENESS_VALUES)}"
            )

        # Lower-cased like `staleness`, and for the same reason: the value is
        # hand-written in a workflow file, where `Archive-On-Merge` is an easy
        # and harmless-looking thing to type.
        mode = _get(env, "FEATUREFLIP_MODE", DEFAULT_MODE).lower()
        if mode not in MODES:
            raise ConfigError(
                f"unsupported FEATUREFLIP_MODE value: "
                f"{_get(env, 'FEATUREFLIP_MODE', DEFAULT_MODE)!r}; "
                f"expected one of {', '.join(MODES)}"
            )

        api_url = _get(env, "FEATUREFLIP_API_URL", DEFAULT_API_URL)
        _ensure_absolute_url(api_url)

        return cls(
            api_token=_get(env, "FEATUREFLIP_API_TOKEN", ""),
            org=_get(env, "FEATUREFLIP_ORG", ""),
            project=_get(env, "FEATUREFLIP_PROJECT", ""),
            api_url=api_url,
            mode=mode,
            staleness=staleness,
            directories=_split_csv(_get(env, "FEATUREFLIP_DIRECTORIES", "."), default=(".",)),
            languages=languages,
            accessors=accessors,
            ignore=frozenset(_split_csv(_get(env, "FEATUREFLIP_IGNORE", ""), default=())),
            base_branch=_get(env, "FEATUREFLIP_BASE_BRANCH", DEFAULT_BASE_BRANCH),
            pr_labels=_split_csv(_get(env, "FEATUREFLIP_PR_LABELS", ""), default=()),
            dry_run=_parse_bool(
                "FEATUREFLIP_DRY_RUN", _get(env, "FEATUREFLIP_DRY_RUN", "")
            ),
            max_prs=_parse_non_negative_int(
                "FEATUREFLIP_MAX_PRS",
                _get(env, "FEATUREFLIP_MAX_PRS", str(DEFAULT_MAX_PRS)),
            ),
        )


#: An accessor name is pasted into the rewrite rules as TEXT, inside a
#: tree-sitter predicate string: `(#eq? @sdk_fn "NAME")`. A name containing a
#: quote therefore closes that string and appends whatever follows to the
#: query, which is arbitrary control over what the engine rewrites in the
#: customer's repository. Flag keys get a laxer allow-list because a key
#: legitimately contains `-`; an accessor is a function name in the target
#: language, so it can be held to the identifier shape every one of those
#: languages shares — and is, deliberately, at the narrowest point.
_ACCESSOR_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _parse_accessors(value: str) -> tuple[str, ...]:
    """Split, validate and de-duplicate ``FEATUREFLIP_ACCESSORS``.

    De-duplication is not tidiness. Each name becomes one generated rule, and
    two rules sharing a name is a hard engine error — so a repeated value would
    turn a harmless typo into a run that cannot start.
    """
    names = _split_csv(value, default=())
    invalid = [name for name in names if not _ACCESSOR_RE.match(name)]
    if invalid:
        raise ConfigError(
            f"unsupported FEATUREFLIP_ACCESSORS value(s): "
            f"{', '.join(repr(name) for name in invalid)}; each must be a function "
            "name in your source, matching [A-Za-z_][A-Za-z0-9_]*. These are "
            "substituted into the rewrite rules as text, so anything else either "
            "fails the engine or silently changes which calls are matched"
        )
    seen: dict[str, None] = {}
    for name in names:
        seen[name] = None
    return tuple(seen)


def _get(env: Mapping[str, str], name: str, default: str) -> str:
    """``env[name]`` stripped, falling back to ``default`` when blank OR absent.

    The "blank" half is the point: ``.get(name, default)`` only fires on
    absence, so a set-but-empty variable silently defeats every default.
    """
    value = env.get(name)
    if value is None:
        return default
    return value.strip() or default


def _ensure_absolute_url(value: str) -> None:
    """Reject an ``api-url`` httpx cannot build a request from.

    Every other malformed input in this file aborts with a one-liner and exit
    2 before anything is touched. A scheme-less value (``api.featureflip.io``,
    the obvious thing to write) did not: it survived here, survived the
    preflight, and died on the first request as
    ``httpx.UnsupportedProtocol: Request URL is missing an 'http://' or
    'https://' protocol`` — a type ``__main__`` does not recognise, so the
    customer got a raw traceback and exit 1 where the README promises exit 2
    and "nothing was modified". The value is a workflow input, so a typo in it
    is configuration, not a bug.

    The host is not resolved and nothing is requested: this asks only whether
    the string is a URL at all. The value is echoed back through
    :func:`~flag_cleanup.urls.redact_userinfo` — not ``without_userinfo``,
    which parses the URL to keep the host and port intact, and everything
    reaching these two messages is by definition something that did not parse
    — because a self-hosted ``api-url`` may carry credentials and a workflow
    log is world-readable on a public repository.
    """
    try:
        parts = urlsplit(value)
        # `.port` is read for its PARSE, not its value: `SplitResult` splits
        # the authority lazily, so this is where an unbracketed IPv6 literal, a
        # non-numeric port or one outside 0-65535 actually raises. Left
        # unasked, those reached httpx instead — `httpx.InvalidURL`, a type
        # `__main__` does not recognise, so a typo'd port printed a traceback
        # and exited 1 rather than the documented exit 2.
        scheme, netloc, _port = parts.scheme.lower(), parts.netloc, parts.port
    except ValueError as exc:
        raise ConfigError(
            f"FEATUREFLIP_API_URL is not a valid URL: {redact_userinfo(value)!r} ({exc})"
        ) from exc
    if scheme not in ("http", "https") or not netloc:
        raise ConfigError(
            f"FEATUREFLIP_API_URL must be an absolute http(s) URL, got "
            f"{redact_userinfo(value)!r} — include the scheme, e.g. {DEFAULT_API_URL}"
        )


def _parse_non_negative_int(name: str, value: str) -> int:
    """Parse a count, refusing anything that is not clearly one.

    Strict for the same reason :func:`_parse_bool` is: this value bounds how
    much a single run may do to the customer's repository, and a lenient parse
    fails toward the destructive side. ``int("10 ")`` works, ``int("ten")``
    raises a ``ValueError`` nobody catches, and a silent fallback to the
    default would turn ``max-prs: 1000`` typed as ``max-prs: 1OOO`` into a
    number the workflow never asked for — in either direction.
    """
    try:
        parsed = int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from None
    if parsed < 0:
        raise ConfigError(f"{name} cannot be negative, got {parsed} (use 0 for no limit)")
    return parsed


def _split_csv(value: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    """Split a comma-separated env value, trimming whitespace and blanks."""
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or default


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def _parse_bool(name: str, value: str) -> bool:
    """Parse a boolean env value, rejecting anything that isn't clearly one.

    Deliberately strict, because the only boolean here is ``dry-run`` and a
    lenient parse fails toward the destructive side: a typo like ``ture`` or a
    quoted ``"true"`` would silently read as False and open real pull requests
    against the customer's repository. Every other malformed input in this file
    aborts with a clear message before anything is touched (exit 2); this one
    must too, rather than being the one place a typo means "go ahead".
    """
    token = value.strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    raise ConfigError(
        f"{name} must be one of true/false (also accepted: "
        f"1/0, yes/no, on/off) — got {value!r}. Refusing to guess, because "
        f"guessing wrong here opens real pull requests."
    )
