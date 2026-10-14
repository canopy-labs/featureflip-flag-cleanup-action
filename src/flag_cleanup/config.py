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
from flag_cleanup.ts_gates import Gate, parse_gate
from flag_cleanup.urls import redact_userinfo

# The Featureflip public API origin. Overridable (``FEATUREFLIP_API_URL``) so
# the Action can be pointed at a staging/self-hosted instance.
DEFAULT_API_URL = "https://api.featureflip.io"
DEFAULT_STALENESS = "dead"

#: What this invocation is for. ``remove`` is everything the Action did before
#: archive-on-merge existed, and stays the default so an existing workflow that
#: never sets ``mode`` keeps behaving identically.
#:
#: Each is a separate invocation on its own trigger, never two phases of one
#: run: ``remove`` runs on a schedule against a checkout, ``archive-on-merge``
#: runs on a merged pull request and touches no source at all, ``archive-sweep``
#: runs on a schedule and touches no source either. Splitting them on an
#: explicit input rather than sniffing ``GITHUB_EVENT_NAME`` is deliberate —
#: both archive modes need a token that can WRITE to the customer's flags, and a
#: behaviour that turns itself on because somebody added a trigger for an
#: unrelated reason is the wrong way to reach for that.
DEFAULT_MODE = "remove"
MODE_REMOVE = "remove"
MODE_ARCHIVE_ON_MERGE = "archive-on-merge"
#: Re-attempt every archive that deferred. Scheduled and project-wide rather
#: than pull-request-scoped. Needs ``GITHUB_TOKEN`` with ``pull-requests:
#: read`` — as does ``archive-on-deploy`` below, which shares its lookup;
#: ``archive-on-merge`` is the one archive mode that needs nothing from it. See
#: :mod:`flag_cleanup.backlog`.
MODE_ARCHIVE_SWEEP = "archive-sweep"
#: Archive the outstanding flags a successful deployment actually shipped.
#: Deployment-scoped rather than pull-request-scoped: it fires on the deploy
#: rather than on the merge, so it can tell whether the removal is running —
#: see :mod:`flag_cleanup.deploy`, and note it narrows the sweep's backlog
#: rather than replacing the sweep. Needs ``GITHUB_TOKEN`` for the same reason
#: the sweep does, plus ``contents: read`` to compare commits.
MODE_ARCHIVE_ON_DEPLOY = "archive-on-deploy"
#: Re-run the transform for the flag whose removal pull request this comment
#: was left on. Human-initiated and pull-request-scoped: the branch names the
#: flag, so there is no key to pass and no way to aim it at another one.
MODE_PR_COMMAND = "pr-command"
MODES = (
    MODE_REMOVE,
    MODE_ARCHIVE_ON_MERGE,
    MODE_ARCHIVE_SWEEP,
    MODE_ARCHIVE_ON_DEPLOY,
    MODE_PR_COMMAND,
)

#: Which GitHub deployment environment ``archive-on-deploy`` acts on. A
#: deployment to anything else is reported and archives nothing, so the default
#: is the environment whose name almost every pipeline already uses — and a
#: wrong one is visible on the very first run rather than silently inert.
DEFAULT_DEPLOYMENT_ENVIRONMENT = "production"

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
    #: See :data:`MODES`. Everything below this line is ``remove``-only EXCEPT
    #: ``api_url``, ``dry_run``, ``deployment_environment`` and
    #: ``deployed_refs``, each of which names the modes that read it; no
    #: archive mode touches a checkout, so none of them reads a transform
    #: input.
    mode: str = DEFAULT_MODE
    #: ``archive-on-deploy`` only: the deployment environment whose successful
    #: deployments authorize an archive. Ignored by every other mode.
    deployment_environment: str = DEFAULT_DEPLOYMENT_ENVIRONMENT
    #: ``archive-sweep`` only: git refs (tags, branches or commit shas) that
    #: stand for what is deployed. When set, a flag is archived only once its
    #: removal's merge commit is contained in EVERY one of them. Empty is the
    #: ungated sweep, which archives on the merge alone. See
    #: :func:`flag_cleanup.backlog.partition_by_containment`.
    deployed_refs: tuple[str, ...] = ()
    staleness: str = DEFAULT_STALENESS
    directories: tuple[str, ...] = (".",)
    languages: tuple[str, ...] = supported_languages()
    #: Extra function names to treat as flag reads, on top of each language's
    #: own SDK names. Empty is the historical behaviour. See
    #: :data:`_ACCESSOR_RE` for why these are held to a stricter shape than
    #: flag keys are.
    accessors: tuple[str, ...] = ()
    #: JSX components that render their children only while a flag is on,
    #: with the prop carrying the key — ``FlagGate[flag]`` entries in the same
    #: ``accessors`` input, split out here because nothing that reads
    #: ``accessors`` can use one. See :mod:`flag_cleanup.ts_gates`.
    gates: tuple[Gate, ...] = ()
    ignore: frozenset[str] = frozenset()
    #: Flag keys this run may propose, as an ALLOWlist. Empty — the default —
    #: means no filtering, so an existing workflow is unaffected.
    #:
    #: Not a way around `already_handled`: a flag named here is still skipped
    #: if it has already been proposed. This scopes a sweep (and, with
    #: `dry_run`, previews exactly one flag without touching GitHub at all);
    #: re-proposing a flag whose pull request was closed is `mode:
    #: pr-command`'s job, where a human asks for it on the pull request itself.
    #:
    #: `ignore` WINS when a key is in both. Deny beating allow is the safe
    #: direction, and `ignore` is documented as the way to silence a flag this
    #: tool refuses to process — an allowlist must not resurrect it.
    flags: frozenset[str] = frozenset()
    #: Empty means "resolve the repository's default branch at run time".
    base_branch: str = DEFAULT_BASE_BRANCH
    pr_labels: tuple[str, ...] = ()
    dry_run: bool = False
    #: Inline keyless wrappers at their proven call sites (Kotlin, Java); see
    #: :mod:`flag_cleanup.wrapper_inline`. ``False`` folds the read inside a
    #: wrapper and stops there.
    inline_wrappers: bool = True
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

        accessors, gates = _parse_accessors(_get(env, "FEATUREFLIP_ACCESSORS", ""))

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
            # NOT lower-cased, unlike `mode` and `staleness`: those are this
            # tool's own enums, while this names an environment the customer
            # created in GitHub and may legitimately have capitalised. The
            # comparison in `deploy._skip_reason` is case-insensitive instead,
            # so the value is carried through as written and still matches.
            deployment_environment=_get(
                env,
                "FEATUREFLIP_DEPLOYMENT_ENVIRONMENT",
                DEFAULT_DEPLOYMENT_ENVIRONMENT,
            ),
            deployed_refs=_parse_deployed_refs(_get(env, "FEATUREFLIP_DEPLOYED_REFS", "")),
            staleness=staleness,
            directories=_split_csv(_get(env, "FEATUREFLIP_DIRECTORIES", "."), default=(".",)),
            languages=languages,
            accessors=accessors,
            gates=gates,
            ignore=frozenset(_split_csv(_get(env, "FEATUREFLIP_IGNORE", ""), default=())),
            flags=frozenset(_split_csv(_get(env, "FEATUREFLIP_FLAGS", ""), default=())),
            base_branch=_get(env, "FEATUREFLIP_BASE_BRANCH", DEFAULT_BASE_BRANCH),
            pr_labels=_split_csv(_get(env, "FEATUREFLIP_PR_LABELS", ""), default=()),
            dry_run=_parse_bool(
                "FEATUREFLIP_DRY_RUN", _get(env, "FEATUREFLIP_DRY_RUN", "")
            ),
            inline_wrappers=_parse_bool(
                "FEATUREFLIP_INLINE_WRAPPERS",
                _get(env, "FEATUREFLIP_INLINE_WRAPPERS", "true"),
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


def _parse_accessors(value: str) -> tuple[tuple[str, ...], tuple[Gate, ...]]:
    """Split, validate and de-duplicate ``FEATUREFLIP_ACCESSORS``.

    Returns the function names and, separately, the gate components
    (``Component[prop]``). A gate never reaches the rules: it is matched in
    Python, against the parsed tree, so it is held to its own shape rather than
    to the function-name one.

    De-duplication is not tidiness. Each name becomes one generated rule, and
    two rules sharing a name is a hard engine error — so a repeated value would
    turn a harmless typo into a run that cannot start.
    """
    names = _split_csv(value, default=())
    gates = {gate: None for name in names if (gate := parse_gate(name)) is not None}
    names = tuple(name for name in names if parse_gate(name) is None)
    invalid = [name for name in names if not _ACCESSOR_RE.match(name)]
    if invalid:
        raise ConfigError(
            f"unsupported FEATUREFLIP_ACCESSORS value(s): "
            f"{', '.join(repr(name) for name in invalid)}; each must be a function "
            "name in your source, matching [A-Za-z_][A-Za-z0-9_]*, or a gate "
            "component and its key prop, like FlagGate[flag]. Function names are "
            "substituted into the rewrite rules as text, so anything else either "
            "fails the engine or silently changes which calls are matched"
        )
    seen: dict[str, None] = {}
    for name in names:
        seen[name] = None
    return tuple(seen), tuple(gates)


#: A deployed ref is pasted into a compare URL's PATH, as the first half of
#: ``{ref}...{merge}``. Held to the characters a plain git ref name uses, so
#: none of the ways a value could change the question gets through: a ``...``
#: or ``..`` that re-splits the compare, a ``?`` or ``#`` that ends the path
#: early, whitespace, or the reflog syntax ``@{``. Narrower than git itself
#: allows, deliberately — a tag that needs more is rare, and one that slipped
#: through would compare against something nobody configured.
_DEPLOYED_REF_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._/+-]*\Z")


def _parse_deployed_refs(value: str) -> tuple[str, ...]:
    """Split, validate and de-duplicate ``FEATUREFLIP_DEPLOYED_REFS``.

    Newlines AND commas separate, because a YAML block scalar is the natural
    way to list refs in a workflow and every other list input here takes
    commas. De-duplicated keeping first position: each ref costs one compare
    per outstanding flag, and a repeat only pays for the same answer twice.
    """
    refs = tuple(item.strip() for item in re.split(r"[,\n]", value) if item.strip())
    invalid = [ref for ref in refs if not _is_plain_ref(ref)]
    if invalid:
        raise ConfigError(
            f"unsupported FEATUREFLIP_DEPLOYED_REFS value(s): "
            f"{', '.join(repr(ref) for ref in invalid)}; each must be a tag, branch "
            "or commit sha made of letters, digits and . _ / + -, starting with "
            "a letter, digit or _, with no '..', no '//', no '/'-separated part "
            "starting with '.', and not ending in '/', '.' or '.lock'. Each is "
            "compared against your removals' merge commits in a GitHub API path, "
            "so anything else would compare against a ref you did not name"
        )
    return tuple(dict.fromkeys(refs))


def _is_plain_ref(ref: str) -> bool:
    return (
        _DEPLOYED_REF_RE.match(ref) is not None
        and ".." not in ref
        and "//" not in ref
        and not ref.endswith(("/", ".", ".lock"))
        # git forbids a component starting with `.`, and a `.` segment is one
        # a URL normaliser may silently drop, comparing a ref nobody named.
        and not any(part.startswith(".") for part in ref.split("/"))
    )


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

    Deliberately strict, because for ``dry-run``, the boolean it was written
    for, a lenient parse fails toward the destructive side: a typo like ``ture`` or a
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
