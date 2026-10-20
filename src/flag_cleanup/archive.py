"""Archive-on-merge: close the loop when a removal pull request merges.

The ``remove`` mode opens a pull request deleting a dead flag's code. Merging
it leaves the flag itself live in Featureflip, so somebody has to remember to
archive it by hand — which means the tool completes half the job it advertises.
This module is the other half.

It is a different shape from ``orchestrate`` in every way that matters: no
checkout, no transform engine, no git, no GitHub writes, and exactly one flag
per run rather than up to ``max-prs``. What it needs is the branch the merged
pull request came from, and one ``POST``.

**Which flag, without a marker.** ``github_ops.removal_branch`` is a documented
bijection, so the branch name a removal pull request already carries encodes
its flag key losslessly. Decoding it beats writing a machine-readable marker
into the pull-request body at open time on three counts: there is nothing to
write, nothing to parse out of prose a human may have edited, and it works on
every pull request this Action has already opened rather than only on ones
opened after this shipped.

**Two quiet no-ops, one soft outcome, one loud failure.** A pull request that
closed without merging (or merged into some branch other than the base, which
the removal has therefore not reached), and a pull request from a branch this
Action did not create, are both ordinary — the customer's workflow fires on every closed pull
request in the repository and almost none of them are ours. Neither is worth a
red build. Almost everything else is: a token that cannot archive, a project
slug that does not match, or a flag the domain refuses to archive all mean this
mode is not working, and finding that out weeks later from a stale flag list is
exactly what it exists to prevent.

``FLAG_RECENTLY_EVALUATED`` is the single exception, and it is a deliberate one
rather than a softening of that rule. It is worth stating plainly because the
whole invariant this tool is built on is that a refusal must never be spelled
the same way as nothing having matched:

* ``FLAG_HAS_DEPENDENTS`` and ``FLAG_HAS_PENDING_SCHEDULES`` stay LOUD. Nothing
  changes until a person removes a prerequisite or cancels a schedule, so a run
  that exits 0 is telling them the loop closed when it did not, and nobody will
  ever look again. The one carve-out is a dependents refusal where EVERY
  dependent named has a merged removal of its own: then this flag waits on the
  same deploy its dependents do, nobody has anything to do, and it is
  ``deferred`` like the refusal below. Showing that takes a GitHub lookup per
  dependent, so this mode makes it only when ``GITHUB_TOKEN`` is passed
  through, and otherwise stays loud.
* ``FLAG_RECENTLY_EVALUATED`` is ``deferred`` and exits 0. Nobody has anything
  to do: the code merged, the deploy has not landed yet, and the refusal lifts
  by itself once traffic drains. ``archive-on-merge`` fires on the MERGE, so on
  a repository whose deploys are manual — or that ships to clients it cannot
  update — this refusal is the ordinary case and would redden the build on
  every removal merge. A red build nobody can act on trains people to ignore
  the runs that matter, which is the same failure the two quiet no-ops above
  exist to avoid.

The deferral is only safe because something drains it: ``mode: archive-sweep``
(:mod:`flag_cleanup.backlog`) re-attempts every outstanding archive on a
schedule. A deferred archive nothing retries is just a leak, and the flag stays
live forever — which is the half-finished state this mode was written to end.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx
from dataclasses import dataclass

from flag_cleanup.client import (
    FeatureflipClient,
    FlagHasDependentsError,
    FlagRecentlyEvaluatedError,
)
from flag_cleanup.config import Config
from flag_cleanup.github_ops import (
    GitHubApi,
    GitHubApiError,
    GitHubConfigError,
    GitHubEnv,
    UnsafeFlagKeyError,
    flag_key_from_branch,
    merged_removal,
    removal_branch,
)
from flag_cleanup.pr_event import (
    MergedPullRequest,
    PullRequestEventError,
    merged_pull_request,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """What one archive-mode run did, for a single reportable line.

    Shared with ``mode: archive-sweep`` (:mod:`flag_cleanup.backlog`), which
    reports a LIST of these — same line format, same outcome vocabulary, so the
    two modes read alike in a workflow log.
    """

    #: ``archived``, ``deferred``, ``not-merged`` or ``not-a-removal-branch``
    #: here; the sweep adds ``would-archive`` and ``failed``.
    action_taken: str
    detail: str
    key: str | None = None
    pull_request: int | None = None


def deferred_detail(exc: FlagRecentlyEvaluatedError) -> str:
    """The one-line report for a deferred archive.

    Phrased for both shapes of :attr:`FlagRecentlyEvaluatedError.environments`
    — the API names them, but an empty tuple is a documented possibility and
    "still being evaluated in " reads as a bug.
    """
    where = f" in {', '.join(exc.environments)}" if exc.environments else ""
    return (
        f"not archived yet: live traffic is still evaluating this flag{where}. "
        "The removal merged but has not finished deploying — nothing to do, and "
        "`mode: archive-sweep` re-attempts it once traffic drains"
    )


def dependents_deferred_detail(exc: FlagHasDependentsError) -> str:
    """The one-line report for an archive deferred behind its own dependents.

    Only ever printed once the caller has shown that EVERY dependent the
    refusal names has a merged removal of its own — see
    :func:`flag_cleanup.backlog.archive_outstanding`. The flag is then waiting
    on the same deploy its dependents are, and nobody has anything to do.
    """
    names = ", ".join(exc.dependents)
    noun = "flag" if len(exc.dependents) == 1 else "flags"
    return (
        f"not archived yet: dependent {noun} {names} still list it as a "
        "prerequisite, and their removal has merged too but they are not "
        "archived yet — nothing to do, and `mode: archive-sweep` archives this "
        "flag once they are"
    )


def run_archive(
    config: Config,
    env: Mapping[str, str] | None = None,
    *,
    client: FeatureflipClient | None = None,
    gh: GitHubApi | None = None,
) -> ArchiveResult:
    """Archive the flag whose removal pull request just merged.

    ``client`` and ``gh`` are injectable so tests never open a socket; when
    omitted, each is built when needed and closed before returning.

    Raises :class:`~flag_cleanup.pr_event.PullRequestEventError` when the
    workflow is wired wrongly, and
    :class:`~flag_cleanup.client.FeatureflipApiError` when the API refuses.
    Both carry a message that names the remedy; neither is caught here, because
    a partial result is not a thing this mode has — it archives one flag or it
    does not.

    The exceptions are :class:`~flag_cleanup.client.FlagRecentlyEvaluatedError`,
    which is returned as ``deferred`` rather than raised — see the module
    docstring — and a :class:`~flag_cleanup.client.FlagHasDependentsError` whose
    every dependent has a merged removal of its own, which can only be shown
    when ``GITHUB_TOKEN`` is available (:func:`_dependents_all_removed`).
    """
    pull_request = merged_pull_request(env)
    if pull_request is None:
        return ArchiveResult(
            action_taken="not-merged",
            detail="the pull request closed without merging, so nothing is archived",
        )

    return _archive_for(config, pull_request, client=client, env=env, gh=gh)


def _archive_for(
    config: Config,
    pull_request: MergedPullRequest,
    *,
    client: FeatureflipClient | None,
    env: Mapping[str, str] | None = None,
    gh: GitHubApi | None = None,
) -> ArchiveResult:
    key = flag_key_from_branch(pull_request.head_ref)
    if key is None:
        return ArchiveResult(
            action_taken="not-a-removal-branch",
            detail=(
                f"{pull_request.head_ref} was not opened by this Action, so there "
                "is no flag to archive"
            ),
            pull_request=pull_request.number or None,
        )

    if not _same_repository(pull_request):
        # A fork's branch name is whatever its author typed, so a removal-shaped
        # one names a flag nobody here asked to remove — and under
        # `pull_request_target` this run holds a token that can archive it.
        # The canonical encoding is no defence: anyone can produce it.
        return ArchiveResult(
            action_taken="not-a-removal-branch",
            detail=(
                f"{pull_request.head_ref} comes from "
                f"{pull_request.head_repository or 'a repository that no longer exists'}"
                f", not {pull_request.repository or 'this repository'}, so this "
                "Action did not open it and there is no flag to archive"
            ),
            pull_request=pull_request.number or None,
        )

    base = config.base_branch.strip() or pull_request.default_branch
    if not base:
        raise PullRequestEventError(
            "the event payload names neither the repository's default branch nor "
            "the branch the pull request merged into, so there is no telling "
            "whether the removal reached it; set the `base-branch` input"
        )
    if pull_request.base_ref != base:
        # Merged, but not where the removal has to land: a pull request
        # retargeted to a spike branch and merged there leaves the flag's code
        # on the base branch exactly as it was.
        return ArchiveResult(
            action_taken="not-merged",
            detail=(
                f"the pull request merged into "
                f"{pull_request.base_ref or 'a branch the event did not name'}, not "
                f"the base branch {base}, so the removal has not reached it and "
                "nothing is archived"
            ),
            key=key,
            pull_request=pull_request.number or None,
        )

    owned = client is None
    active = client or FeatureflipClient(config.api_url, config.api_token)
    try:
        active.archive_flag(config.org, config.project, key)
    except FlagRecentlyEvaluatedError as exc:
        # Caught NARROWLY, by type, rather than by inspecting a 400's text:
        # the sibling refusals travel as the same status code through the same
        # envelope, and they must keep failing. See the module docstring.
        logger.info(
            "archive of flag %s deferred: still evaluated in %s",
            key,
            ", ".join(exc.environments) or "an environment the API did not name",
        )
        return ArchiveResult(
            action_taken="deferred",
            detail=deferred_detail(exc),
            key=key,
            pull_request=pull_request.number or None,
        )
    except FlagHasDependentsError as exc:
        if not _dependents_all_removed(exc.dependents, base=base, env=env, gh=gh):
            raise
        logger.info(
            "archive of flag %s deferred: dependents %s have merged removals",
            key,
            ", ".join(exc.dependents),
        )
        return ArchiveResult(
            action_taken="deferred",
            detail=dependents_deferred_detail(exc),
            key=key,
            pull_request=pull_request.number or None,
        )
    finally:
        if owned:
            active.close()

    logger.info("archived flag %s after pull request #%s merged", key, pull_request.number)
    return ArchiveResult(
        action_taken="archived",
        detail=f"archived after pull request #{pull_request.number} merged",
        key=key,
        pull_request=pull_request.number or None,
    )


def _same_repository(pull_request: MergedPullRequest) -> bool:
    """Whether the pull request's branch lives in this repository, not a fork.

    Compared case-insensitively, as GitHub resolves repository names; an empty
    side — a deleted fork, a payload that omits it — is never a match.
    """
    head = pull_request.head_repository.casefold()
    return bool(head) and head == pull_request.repository.casefold()


def _dependents_all_removed(
    dependents: tuple[str, ...],
    *,
    base: str,
    env: Mapping[str, str] | None,
    gh: GitHubApi | None,
) -> bool:
    """Whether every dependent has a merged removal pull request of its own.

    This mode needs nothing from ``GITHUB_TOKEN`` and does not start needing
    it: when the workflow does not pass one through, the answer is ``False``
    and the refusal stays the loud failure it always was. A workflow that DOES
    pass one (with ``pull-requests: read``) gets the sweep's judgement — the
    same :func:`~flag_cleanup.github_ops.merged_removal` evidence, one lookup
    per dependent.

    Any lookup that cannot answer is ``False`` too. "I could not tell" must
    never quiet a refusal a person may need to act on.
    """
    try:
        github = GitHubEnv.from_env(env)
    except GitHubConfigError:
        return False
    active = gh if gh is not None else GitHubApi(github.token, api_url=github.api_url)
    try:
        for dependent in dependents:
            branch = removal_branch(dependent)
            if merged_removal(active, github.repository, branch, base=base) is None:
                return False
        return True
    except (UnsafeFlagKeyError, GitHubApiError, httpx.HTTPError) as exc:
        logger.warning("could not check the removals of %s: %s", ", ".join(dependents), exc)
        return False
    finally:
        if gh is None:
            active.close()
