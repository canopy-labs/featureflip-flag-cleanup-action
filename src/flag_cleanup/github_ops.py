"""Talk to the GitHub REST API: branch naming, idempotency, PR creation.

Runs in the CUSTOMER's CI against THEIR repository with the runner-provided
``GITHUB_TOKEN``. Raw REST over ``httpx`` (already a dependency for the
Featureflip client) rather than PyGithub — the surface used here is three
endpoints, and keeping it behind this module means the choice never leaks into
the orchestrator.

Two things in here are load-bearing for not being a spammer or a vandal:

* :func:`removal_branch` — a Featureflip flag key is NOT a git ref. It may
  contain characters that are illegal (``~^:?*[\\``, control chars, spaces) or
  merely dangerous (``..``, a trailing ``/`` or ``.``, ``.lock``, ``@{``) in a
  ref name. The key is escaped, never "cleaned up", so two distinct keys can
  never collide onto one branch — a collision would mean two flags fighting
  over one PR.
* :func:`already_handled` — a weekly schedule must not re-open a PR it already
  proposed, so this asks the API (not the local ref store, which in a CI
  checkout knows nothing about branches pushed by last week's run) about BOTH
  the branch and any prior PR for it, in **any** state. A closed PR is a
  customer saying no; re-opening it every week is worse than not shipping the
  feature at all.

Neither an HTTP error nor a missing permission is ever read as "not handled" —
that would silently duplicate PRs. Failures raise :class:`GitHubApiError` so
the orchestrator can fail that one flag and move on; a 401/403 raises the
:class:`GitHubAuthError` subclass instead, because a token that cannot do this
cannot do it for ANY flag and repeating the identical failure per candidate
helps nobody.

:func:`default_branch` and :func:`ensure_head_on_base` cover the two ways the
*base* of a pull request can be wrong: targeting a branch that does not exist
(the ``main``-vs-``master`` assumption, which used to 422 every PR), and
cutting the removal branch from a HEAD that carries commits the base does not.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_SERVER_URL = "https://github.com"

#: Frozen by the plan — one PR per flag on ``featureflip/remove-flag/<key>``.
BRANCH_PREFIX = "featureflip/remove-flag/"

# Characters kept verbatim in a branch name. Deliberately excludes ``.``: with
# no dot anywhere in the encoded key, git's dot rules (no leading ``.``, no
# ``..``, no trailing ``.``, no ``.lock`` suffix) are satisfied by construction
# rather than by a pile of special cases that would each be a chance to be
# subtly wrong.
_SAFE_BRANCH_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
_ESCAPE = "="

# Uppercase only: `_encode_char` emits `{byte:02X}`, so accepting lowercase
# here would decode a branch this tool could not have written.
_UPPER_HEX = frozenset("0123456789ABCDEF")

# The bound is on the ENCODED segment because that is what becomes a path
# component: a loose ref is stored at `.git/refs/heads/<...>/<encoded key>`,
# and git writes `<that>.lock` beside it while updating. Most filesystems cap a
# component at 255 bytes, so 240 leaves room for the 5-byte suffix. Counting
# raw characters instead would let a non-ASCII key (6 encoded characters per
# emoji) sail past the real limit and fail inside git.
_MAX_BRANCH_SEGMENT = 240

_API_VERSION = "2022-11-28"

# Statuses of GitHub's compare endpoint that mean HEAD introduces nothing the
# base branch does not already contain. See :func:`ensure_head_on_base`.
_HEAD_CONTAINED_IN_BASE = frozenset({"identical", "behind"})


class GitHubConfigError(ValueError):
    """A runner-provided GitHub variable is missing or malformed."""


class UnsafeFlagKeyError(ValueError):
    """The flag key cannot be represented as a git branch name.

    Raised rather than guessed at: silently mangling a key risks two flags
    landing on one branch, and pushing a ref git will not accept fails deep in
    a subprocess with a far less obvious message.
    """


class GitHubApiError(RuntimeError):
    """A GitHub API call failed. Never carries the token in its message."""


class GitHubAuthError(GitHubApiError):
    """A GitHub call was refused with 401/403 — a run-level fact, not a bad flag.

    Distinct from its parent so the orchestrator can stop instead of repeating
    the identical failure once per candidate. A token missing ``contents`` or
    ``pull-requests`` permission, or a repository with *"Allow GitHub Actions
    to create and approve pull requests"* turned off, refuses every flag
    equally; one clear message beats N copies of it, and (combined with the
    orphan-branch cleanup) leaves nothing behind to clean up by hand.
    """


class BaseRefError(RuntimeError):
    """The checkout cannot safely be the source of a removal branch.

    Raised before any candidate is processed, so nothing has been modified.
    """


@dataclass(frozen=True, slots=True)
class GitHubEnv:
    """The runner-provided GitHub context.

    Deliberately NOT part of :class:`~flag_cleanup.config.Config`: these come
    from the Actions runner (``GITHUB_*``), not from the Action's own inputs,
    and only this module needs them.

    ``token`` is kept out of ``__repr__`` for the same reason
    ``Config.api_token`` is: this object is exactly the sort of thing that ends
    up inside a debug line, an exception message, or a ``functools.partial``
    whose repr shows its bound kwargs — any of which lands in a workflow log
    that is world-readable on a public repository.
    """

    token: str = field(repr=False)
    repository: str
    api_url: str = DEFAULT_API_URL
    server_url: str = DEFAULT_SERVER_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubEnv:
        """Read ``GITHUB_TOKEN``/``GITHUB_REPOSITORY`` (+ GHES overrides).

        ``env`` is injectable so tests never depend on real process state.
        """
        env = os.environ if env is None else env
        missing = [
            name for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY") if not env.get(name)
        ]
        if missing:
            raise GitHubConfigError(
                f"missing required environment variable(s): {', '.join(missing)} "
                "(provided by the Actions runner; pass GITHUB_TOKEN through in "
                "the workflow's `env:`)"
            )

        repository = env["GITHUB_REPOSITORY"]
        if repository.count("/") != 1 or not all(repository.split("/")):
            raise GitHubConfigError(
                f"GITHUB_REPOSITORY must be 'owner/name', got {repository!r}"
            )

        return cls(
            token=env["GITHUB_TOKEN"],
            repository=repository,
            api_url=env.get("GITHUB_API_URL") or DEFAULT_API_URL,
            server_url=env.get("GITHUB_SERVER_URL") or DEFAULT_SERVER_URL,
        )

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def push_url(self) -> str:
        """The push remote. Contains NO credential — see ``git_ops.push_branch``."""
        return f"{self.server_url.rstrip('/')}/{self.repository}.git"


class GitHubApi:
    """Minimal authenticated transport for the three endpoints this tool uses.

    Pass ``transport`` (e.g. ``httpx.MockTransport``) in tests — no request
    here is ever made against a hardcoded real client.

    Returns raw responses rather than raising on 4xx: ``already_handled``
    needs to tell a 404 (genuinely absent) from a 403 (we cannot tell), and
    conflating them is exactly the bug that would duplicate PRs.
    """

    def __init__(
        self,
        token: str,
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        return self._client.get(path, params=params)

    def post(self, path: str, json: dict) -> httpx.Response:
        return self._client.post(path, json=json)


def removal_branch(key: str) -> str:
    """Return ``featureflip/remove-flag/<escaped key>``.

    The key is escaped, not sanitized: every character outside
    ``[A-Za-z0-9_-]`` (including the escape character itself) becomes
    ``=<UPPERCASE HEX>`` per UTF-8 byte. That is a bijection on the encoded
    alphabet, so distinct keys always produce distinct branches — a "strip the
    bad characters" scheme would map ``a/b`` and ``a-b`` (or ``a.b``) onto one
    branch and let two flags overwrite each other's PR.

    Ordinary kebab/snake-case keys pass through untouched, so the common case
    stays readable (``old-checkout`` -> ``featureflip/remove-flag/old-checkout``).

    Note that the key is NOT stripped first: ``"a "`` and ``"a"`` are different
    keys and must not share a branch, so the trailing space is escaped like any
    other character.

    Raises :class:`UnsafeFlagKeyError` when the key is blank or would produce a
    ref segment too long to store — skip such a flag loudly rather than guess.
    The limit is on the ENCODED length (see ``_MAX_BRANCH_SEGMENT``), so the
    message reports both numbers: a key well under the limit as written can
    still exceed it once escaped.
    """
    if not key.strip():
        raise UnsafeFlagKeyError(f"flag key {key!r} is blank")
    encoded = "".join(_encode_char(char) for char in key)
    if len(encoded) > _MAX_BRANCH_SEGMENT:
        raise UnsafeFlagKeyError(
            f"flag key {key!r} is {len(key)} character(s) long but escapes to "
            f"{len(encoded)} characters for use as a branch name, over the "
            f"{_MAX_BRANCH_SEGMENT}-character limit. Characters outside "
            "[A-Za-z0-9_-] cost 3 characters per byte once escaped"
        )
    return f"{BRANCH_PREFIX}{encoded}"


def _encode_char(char: str) -> str:
    if char in _SAFE_BRANCH_CHARS:
        return char
    return "".join(f"{_ESCAPE}{byte:02X}" for byte in char.encode("utf-8"))


def flag_key_from_branch(branch: str) -> str | None:
    """The flag key ``branch`` encodes, or ``None`` if it is not one of ours.

    The exact inverse of :func:`removal_branch`, which matters more than it
    looks: that function is a documented BIJECTION on the encoded alphabet, so
    the branch name a removal pull request already carries IS a lossless,
    machine-readable record of which flag it removes. Archive-on-merge
    therefore needs no marker written into the pull-request body at open time,
    and works on every pull request this Action has ever opened rather than
    only on ones opened after the feature shipped.

    ``None`` rather than an exception for the overwhelmingly common case: the
    customer's workflow fires on *every* closed pull request in the repository,
    and almost none of them are ours. A branch that is not a removal branch is
    an ordinary no-op, not an error.

    The scan rejects the malformed shapes — lowercase hex, a truncated escape,
    a character :func:`removal_branch` would have escaped. The re-encode check
    at the end exists for the one thing the scan cannot see: a **non-canonical**
    encoding of a perfectly valid key. ``=6F=6C=64`` decodes to ``old`` under
    any lenient reader, but this Action writes ``old``, so a branch spelled that
    way was made by something else. Without that check a hand-crafted branch
    under this prefix could name any flag it liked, and the value flows straight
    into a call that mutates the customer's flag state. It also catches an
    over-long segment, which :func:`removal_branch` refuses to write.

    Stated positively: this can only ever return a key that WOULD produce
    exactly this branch.
    """
    if not branch.startswith(BRANCH_PREFIX):
        return None
    encoded = branch[len(BRANCH_PREFIX) :]
    if not encoded:
        return None

    decoded = bytearray()
    index = 0
    while index < len(encoded):
        char = encoded[index]
        if char == _ESCAPE:
            pair = encoded[index + 1 : index + 3]
            if len(pair) != 2 or any(digit not in _UPPER_HEX for digit in pair):
                return None
            decoded.append(int(pair, 16))
            index += 3
        elif char in _SAFE_BRANCH_CHARS:
            decoded.append(ord(char))
            index += 1
        else:
            return None

    try:
        key = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None

    # The one check that makes this exact. `removal_branch` refuses a blank or
    # over-long key, and either refusal means the input was never something it
    # could have produced.
    try:
        if removal_branch(key) != branch:
            return None
    except UnsafeFlagKeyError:
        return None
    return key


def already_handled(gh: GitHubApi, repo: str, branch: str) -> bool:
    """Has this flag's removal already been proposed?

    True when the branch exists on the remote OR any pull request has ever
    been opened from it — ``state=all``, so a PR the customer closed counts.
    Asked of the API rather than of local refs, because a scheduled run works
    from a fresh shallow checkout that has never seen last week's branch.
    """
    ref_response = gh.get(f"/repos/{repo}/git/ref/heads/{branch}")
    if ref_response.status_code == 200:
        if _body_names_ref(ref_response, f"refs/heads/{branch}"):
            logger.info(
                "branch %s already exists on %s — nothing to propose", branch, repo
            )
            return True
        # A 200 that does NOT name this exact ref is a prefix match on other
        # branches (GitHub's ref endpoints have historically answered that
        # way). Reading it as "exists" would permanently skip a flag whose key
        # is a prefix of another flag's — silently, on every future run.
        logger.debug(
            "ref lookup for %s returned 200 without an exact match — treating "
            "the branch as absent",
            branch,
        )
    elif ref_response.status_code != 404:
        _raise_api_error(
            ref_response,
            f"could not check whether branch {branch} exists; refusing to "
            "assume it does not",
        )

    return existing_pull_request(gh, repo, branch) is not None


def existing_pull_request(gh: GitHubApi, repo: str, branch: str) -> str | None:
    """The URL of a pull request already opened from ``branch``, or ``None``.

    ``state=all``, so a PR the customer closed counts: re-proposing a removal
    they turned down is worse than proposing nothing.

    ``None`` means *provably none exists*. An empty string means one exists but
    the response did not name its URL — the two must not be conflated, because
    the caller in ``orchestrate`` uses this to decide whether a pushed branch
    can be deleted, and deleting the head branch of a pull request CLOSES that
    pull request. Raises rather than guessing when the API will not answer.
    """
    owner = repo.split("/", 1)[0]
    response = gh.get(
        f"/repos/{repo}/pulls",
        params={"state": "all", "head": f"{owner}:{branch}", "per_page": "1"},
    )
    if response.status_code != 200:
        _raise_api_error(
            response,
            f"could not list pull requests for {branch}; refusing to assume "
            "there are none",
        )

    pulls = response.json()
    if not isinstance(pulls, list) or not pulls:
        return None
    first = pulls[0] if isinstance(pulls[0], dict) else {}
    logger.info(
        "pull request #%s was already opened from %s (state=%s) — not proposing it again",
        first.get("number"),
        branch,
        first.get("state"),
    )
    return str(first.get("html_url") or "")


def open_pr(
    gh: GitHubApi,
    repo: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    labels: Sequence[str],
    draft: bool,
) -> str:
    """Open a pull request from ``branch`` into ``base``; return its URL.

    ``draft`` is the Stale-vs-Dead distinction: a stale flag's removal is a
    suggestion, so it lands as a draft.

    Raises when **this call** did not create one — which is NOT the same as
    "none exists". GitHub answers 422 "A pull request already exists for
    owner:branch" when a concurrent run got there first (two overlapping runs,
    or a re-run: the second push is a no-op because the tree and the commit are
    byte-identical, and only the POST fails). Reading a raise as "no PR exists"
    and deleting the pushed branch would close the pull request the other run
    just opened, and — because ``already_handled`` counts closed PRs — retire
    that flag forever. The caller therefore asks
    :func:`existing_pull_request` before deleting anything.

    An empty return means "created, but GitHub did not tell us the URL" — not
    "not created". Nothing after the 201 may raise, for the same reason.
    """
    response = gh.post(
        f"/repos/{repo}/pulls",
        json={
            "title": title,
            "head": branch,
            "base": base,
            "body": body,
            "draft": draft,
        },
    )
    if response.status_code != 201:
        _raise_api_error(response, f"could not open a pull request for {branch}")

    # Past this line the pull request EXISTS, so nothing below may raise:
    # the caller treats "open_pr raised" as "no PR was created" and deletes the
    # pushed branch — which, for a branch that does have a PR, closes it.
    # Every read of the response body is therefore tolerant.
    try:
        pull = response.json()
    except ValueError:
        pull = {}
    if not isinstance(pull, dict):
        pull = {}

    url = str(pull.get("html_url") or "")
    if url:
        logger.info("opened %s%s", url, " (draft)" if draft else "")
    else:
        logger.warning(
            "opened a pull request for %s but the response carried no html_url; "
            "the PR exists — check %s on GitHub",
            branch,
            repo,
        )

    number = pull.get("number")
    if labels and number is None:
        # The `html_url` branch above logs when the response is degraded; this
        # one used to fall through in silence, so a customer routing these PRs
        # by label (a review queue, a required-reviewer rule) got an unlabelled
        # PR, exit 0, and nothing anywhere saying why.
        logger.warning(
            "opened a pull request for %s but the response carried no number, "
            "so label(s) %s were not applied — add them by hand if something "
            "downstream depends on them",
            branch,
            ", ".join(labels),
        )
    elif labels:
        # Labels are cosmetic and the PR already exists — failing the whole
        # flag here would be worse than a PR without labels. Logged, not
        # swallowed.
        label_response = gh.post(
            f"/repos/{repo}/issues/{number}/labels", json={"labels": list(labels)}
        )
        if label_response.status_code >= 400:
            logger.warning(
                "opened %s but could not apply label(s) %s (HTTP %d): %s",
                url,
                ", ".join(labels),
                label_response.status_code,
                _api_message(label_response),
            )

    return url


def default_branch(gh: GitHubApi, repo: str) -> str:
    """The repository's own default branch.

    Used when ``base-branch`` is not configured. Hardcoding ``main`` instead
    means every pull request in a ``master`` repository is rejected — which,
    before the branch is cleaned up on failure, used to push one orphan branch
    per flag and open nothing.
    """
    response = gh.get(f"/repos/{repo}")
    if response.status_code != 200:
        _raise_api_error(
            response,
            f"could not read {repo} to find its default branch; set the "
            "`base-branch` input explicitly to skip this lookup",
        )
    branch = response.json().get("default_branch")
    if not branch:
        raise GitHubApiError(
            f"{repo} reported no default branch; set the `base-branch` input "
            "explicitly"
        )
    logger.info("resolved the base branch for %s to %s", repo, branch)
    return branch


def ensure_head_on_base(gh: GitHubApi, repo: str, base: str, head_sha: str) -> None:
    """Raise unless ``head_sha`` introduces nothing ``base`` does not have.

    Removal branches are cut from the CURRENT HEAD, because that is what the
    Piranha diff was computed against. That is only sound while HEAD is
    contained in the base branch. It is not on a ``pull_request`` trigger,
    where ``actions/checkout`` hands over a merge commit — a branch cut there
    would drag that pull request's unrelated commits into every removal PR.

    ``GET /compare/{base}...{head}`` answers exactly this: ``identical`` or
    ``behind`` means every commit reachable from HEAD is already in ``base``
    (``behind`` simply means the base moved on since the checkout, which is
    harmless). ``ahead``/``diverged`` means HEAD carries commits of its own.

    Asked of the API rather than of local refs because a shallow CI checkout
    frequently has no local copy of the base branch to compare against.
    """
    response = gh.get(f"/repos/{repo}/compare/{base}...{head_sha}")
    if response.status_code in (401, 403):
        # A refusal is a permissions fact, not a fact about the base branch —
        # saying "check that base-branch exists" here would send the customer
        # looking in the wrong place entirely.
        _raise_api_error(response, f"could not compare {head_sha[:12]} against {base}")
    if response.status_code != 200:
        raise BaseRefError(
            f"could not compare the checkout ({head_sha[:12]}) against base "
            f"branch {base!r} in {repo} (HTTP {response.status_code}): "
            f"{_api_message(response)}. Check that `base-branch` names a "
            "branch that exists and that this commit has been pushed"
        )

    status = response.json().get("status")
    if status in _HEAD_CONTAINED_IN_BASE:
        return
    raise BaseRefError(
        f"the checkout ({head_sha[:12]}) is {status!r} relative to base branch "
        f"{base!r}: it carries commits {base!r} does not, so every removal "
        "pull request cut from it would also contain them. Run this Action "
        "from a checkout of the base branch (e.g. `on: schedule` or `on: push` "
        "to that branch), not from a `pull_request` merge commit, or set "
        "`base-branch` to the branch this checkout is on"
    )


def _body_names_ref(response: httpx.Response, ref: str) -> bool:
    """Does the ref-lookup body name ``ref`` exactly?

    Handles both shapes GitHub's ref endpoints have used: a single object for
    an exact match, and a list when the request was treated as a prefix.
    """
    try:
        payload = response.json()
    except ValueError:
        return False
    if isinstance(payload, dict):
        return payload.get("ref") == ref
    if isinstance(payload, list):
        return any(
            isinstance(item, dict) and item.get("ref") == ref for item in payload
        )
    return False


def _raise_api_error(response: httpx.Response, message: str) -> None:
    """Raise :class:`GitHubAuthError` for 401/403, else :class:`GitHubApiError`.

    The split exists so the orchestrator can tell "this one call failed" from
    "this token cannot do this at all", which is true of every flag and should
    stop the run rather than repeat itself N times.
    """
    detail = f"{message} (HTTP {response.status_code}): {_api_message(response)}"
    if response.status_code in (401, 403):
        raise GitHubAuthError(detail)
    raise GitHubApiError(detail)


def _api_message(response: httpx.Response) -> str:
    """GitHub's own error text, truncated. Never includes request headers."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("message", payload))[:200]
    return str(payload)[:200]
