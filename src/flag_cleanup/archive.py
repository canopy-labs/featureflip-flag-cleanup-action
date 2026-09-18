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
closed without merging, and a pull request from a branch this Action did not
create, are both ordinary — the customer's workflow fires on every closed pull
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
  ever look again.
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
from dataclasses import dataclass

from flag_cleanup.client import FeatureflipClient, FlagRecentlyEvaluatedError
from flag_cleanup.config import Config
from flag_cleanup.github_ops import flag_key_from_branch
from flag_cleanup.pr_event import MergedPullRequest, merged_pull_request

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


def run_archive(
    config: Config,
    env: Mapping[str, str] | None = None,
    *,
    client: FeatureflipClient | None = None,
) -> ArchiveResult:
    """Archive the flag whose removal pull request just merged.

    ``client`` is injectable so tests never open a socket; when omitted, one is
    built from ``config`` and closed before returning.

    Raises :class:`~flag_cleanup.pr_event.PullRequestEventError` when the
    workflow is wired wrongly, and
    :class:`~flag_cleanup.client.FeatureflipApiError` when the API refuses.
    Both carry a message that names the remedy; neither is caught here, because
    a partial result is not a thing this mode has — it archives one flag or it
    does not.

    The lone exception is :class:`~flag_cleanup.client.FlagRecentlyEvaluatedError`,
    which is returned as ``deferred`` rather than raised — see the module
    docstring for why that one refusal, and only that one, is soft.
    """
    pull_request = merged_pull_request(env)
    if pull_request is None:
        return ArchiveResult(
            action_taken="not-merged",
            detail="the pull request closed without merging, so nothing is archived",
        )

    return _archive_for(config, pull_request, client=client)


def _archive_for(
    config: Config,
    pull_request: MergedPullRequest,
    *,
    client: FeatureflipClient | None,
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
