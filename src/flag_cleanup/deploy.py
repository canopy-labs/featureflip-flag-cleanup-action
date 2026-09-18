"""``mode: archive-on-deploy`` — archive on the event that actually closes the loop.

``archive-on-merge`` fires on the merge. The flag becomes safe to archive when
the code that no longer reads it is **live**, and those are the same moment in
some pipelines and days apart in others — so on every pipeline where they
differ, that mode is proposing an archive against a commit nobody is running
yet. This module fires on the deploy instead, and archives only the flags whose
removal that deployment actually contains.

**It does NOT make the deferral go away, and reading it that way is the one
mistake that costs a customer something.** The platform refuses an archive
while it can still see the flag being evaluated, and a deploy fires seconds
after the build being replaced was serving that flag — so for any flag with
live traffic the ordinary outcome HERE is ``deferred`` too, and the archive
lands on a later run once the traffic has drained. ``archive-sweep`` alongside
this one is therefore required rather than advisable; without it a repository
that deploys rarely defers every removal and never drains.

**What it adds is the half that refusal cannot do.** That refusal reads
telemetry, so it rules only on environments it has seen traffic from and lets a
flag nothing has evaluated through — which is right when the flag is genuinely
dead and wrong when the code reading it is deployed but rarely reached. This
mode asks git instead, and git cannot be quiet about a commit: either the
deployment contains the removal or it does not. The two guards fail in opposite
directions, which is the whole argument for running both.

**It inverts the lookup, and that is the whole design.** ``archive-on-merge``
reads *this pull request, therefore this flag* straight out of the event
payload. This mode asks *this deployed commit — which merged removals does it
contain, and which of their flags are still outstanding?* The second half of
that question is :func:`~flag_cleanup.backlog.outstanding_archives` exactly, so
it is called rather than re-derived, and the first half is one
``GET /compare`` per candidate. Both halves therefore agree with the sweep by
construction: a flag this mode leaves outstanding is one the sweep will find.

**It costs what the sweep costs, and for the same reason.**
``archive-on-merge`` needs nothing at all from ``GITHUB_TOKEN`` — it reads its
pull request out of the payload and touches no source. This mode needs
``pull-requests: read`` (which merged removals exist) and ``contents: read``
(does the deployed commit contain them). That is a real trade against the
property that made the other mode unusually cheap, and it is documented in the
README rather than quietly widened. Customers who cannot pay it keep
``archive-on-merge`` plus ``archive-sweep``, unchanged.

**Two things it does not do**, both stated here because a mode named after
deploys invites the assumption that it covers them:

* It helps only repositories that create GitHub **Deployments**. Plenty of
  pipelines do (Vercel, Netlify, ``actions/deploy-pages``, Argo via webhooks);
  plenty do not, and for those there is no event to hang this on.
* It does nothing for clients you cannot update — mobile, desktop, embedded.
  No git event corresponds to *the last old app version stopped calling us*,
  so the platform's own refusal is what protects those, exactly as before.

**Every outcome that is not an archive is still a line.** A deployment to
another environment, a status that is not ``success``, and a merged removal the
deployed commit does not yet contain are all reported and all exit ``0`` —
because each is the ordinary result of a workflow that fires on every status of
every deployment, and a red build nobody can act on is the failure the
deferral rule was written to avoid. What none of them may be is SILENT: a flag
skipped without a line is indistinguishable from a flag that was never
outstanding, which is the state this mode exists to end.

The one carve-out is the sweep's, inherited with
:func:`~flag_cleanup.backlog.archive_outstanding`: a token that cannot archive
stops the loop, because that is provably not about any one flag, so the
candidates after it get no line. It is named there rather than silently, and it
is the only way a flag leaves this mode unreported.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from flag_cleanup.archive import ArchiveResult
from flag_cleanup.backlog import OutstandingArchive, archive_outstanding, outstanding_archives
from flag_cleanup.client import FeatureflipClient
from flag_cleanup.config import Config
from flag_cleanup.github_ops import (
    GitHubApi,
    GitHubApiError,
    GitHubEnv,
    commit_is_contained,
)
from flag_cleanup.pr_event import DEPLOYMENT_SUCCESS, DeploymentEvent, deployment_event

logger = logging.getLogger(__name__)

#: Outcomes that describe the DELIVERY rather than a flag, and therefore stand
#: alone: when one is returned it is the whole result. ``__main__`` keys off
#: this to skip the per-flag summary, which would otherwise announce "0
#: outstanding flags" about a run that never looked.
SKIPPED_ACTIONS = frozenset({"not-a-successful-deployment", "other-environment"})


def run_archive_on_deploy(
    config: Config,
    env: Mapping[str, str] | None = None,
    *,
    client: FeatureflipClient | None = None,
    gh: GitHubApi | None = None,
) -> list[ArchiveResult]:
    """Archive every outstanding flag whose removal this deployment shipped.

    Returns one result per outstanding flag — the ones this deployment does not
    contain first, then the ones it does — or a single result whose
    ``action_taken`` is in :data:`SKIPPED_ACTIONS` when the delivery was not one
    this mode acts on at all. Each group keeps
    :func:`~flag_cleanup.backlog.outstanding_archives` order, so two runs over
    an unchanged project report identically and diff cleanly.

    One flag per result holds in every case but one:
    :func:`~flag_cleanup.backlog.archive_outstanding` STOPS on a token that
    cannot archive, so the candidates behind it get no result at all. That is
    the sweep's documented exception, shared rather than re-decided here — the
    failure is reported on the flag it stopped at, and it is about the token
    rather than about any flag.

    Both clients are injectable so tests never open a socket; each is closed
    only when this function built it.

    Raises :class:`~flag_cleanup.pr_event.PullRequestEventError` when the
    workflow is wired to the wrong trigger, and
    :class:`~flag_cleanup.github_ops.GitHubConfigError` when ``GITHUB_TOKEN``
    was not passed through — this mode genuinely needs it. ``__main__`` renders
    both as exit 2 ("the run could not start"), which is accurate: both are
    raised before the first request, so nothing has been archived.
    """
    deployment = deployment_event(env)

    # BEFORE the environment gate, deliberately. A workflow that forgot to pass
    # GITHUB_TOKEN through is broken for every delivery, and checking it after
    # the gate would hide that behind a green `other-environment` line on every
    # staging deploy — surfacing it only on the first production one, possibly
    # weeks later and in the run that mattered. It reads environment variables
    # and opens no connection, so paying for it on a delivery this mode ignores
    # costs nothing.
    github = GitHubEnv.from_env(env)

    skipped = _skip_reason(deployment, config.deployment_environment)
    if skipped is not None:
        return [skipped]

    owned_gh = gh is None
    owned_client = client is None
    active_gh = gh if gh is not None else GitHubApi(github.token, api_url=github.api_url)
    active = (
        client if client is not None else FeatureflipClient(config.api_url, config.api_token)
    )
    try:
        candidates = outstanding_archives(config, active_gh, github.repository, client=active)
        deployed, pending = _partition_by_containment(
            active_gh, github.repository, candidates, deployment.sha
        )
        return pending + archive_outstanding(config, deployed, active)
    finally:
        if owned_gh:
            active_gh.close()
        if owned_client:
            active.close()


def _skip_reason(deployment: DeploymentEvent, environment: str) -> ArchiveResult | None:
    """The one line this delivery earns, or ``None`` to go on and archive.

    The state is checked before the environment on purpose: a failed production
    deploy is a more useful thing to see named than the environment it failed
    in, and it is also the shape most likely to be misread as "this ran".
    """
    if deployment.state != DEPLOYMENT_SUCCESS:
        return ArchiveResult(
            action_taken="not-a-successful-deployment",
            detail=(
                f"the deployment to {deployment.environment or 'an unnamed environment'} "
                f"reported state {deployment.state or '(none)'!r}, so whatever it "
                f"would have shipped is not running — nothing is archived"
            ),
        )

    # Case-insensitively, because the alternative fails SILENTLY and forever:
    # a workflow configured `deployment-environment: Production` against an
    # environment GitHub names `production` would report this line on every
    # deploy it was written to act on, and read exactly like a repository with
    # nothing outstanding. Two environments differing only by case is the far
    # rarer hazard, and it is one a customer can see in their own settings.
    if deployment.environment.casefold() != environment.casefold():
        return ArchiveResult(
            action_taken="other-environment",
            detail=(
                f"this deployment was to {deployment.environment or '(no environment)'!r}, "
                f"not {environment!r} — nothing is archived. Set the "
                f"`deployment-environment` input if that is the wrong environment "
                f"to archive on"
            ),
        )
    return None


def _partition_by_containment(
    gh: GitHubApi,
    repo: str,
    candidates: list[OutstandingArchive],
    deployed_sha: str,
) -> tuple[list[OutstandingArchive], list[ArchiveResult]]:
    """Split the backlog into *this deploy shipped it* and *not yet*.

    Returns the candidates to archive, and a ready-made report line for each
    one held back. Held back is never permanent: nothing is written to record
    it, the flag's own unarchived state is what puts it back in the backlog,
    and the next deploy that contains the merge picks it up — as does the
    sweep, if one is also configured.

    **Isolated per flag**, like :func:`~flag_cleanup.backlog.archive_outstanding`
    and for the same reason. A compare GitHub will not answer is a fact about
    ONE candidate — a merge commit orphaned by a history rewrite, two commits
    with no common ancestor, a 500 on a comparison too large to compute — and
    letting it out of this loop would abandon the whole run before anything was
    archived, discarding the lines already computed for every other flag. That
    turns one unanswerable candidate into a mode that is inert on every
    subsequent deployment, which is the permanent silent no-op this whole
    module exists to end.
    """
    deployed: list[OutstandingArchive] = []
    pending: list[ArchiveResult] = []

    for candidate in candidates:
        if not candidate.merge_commit_sha:
            # Degraded rather than absent evidence: the merge is proven (that
            # is what put this flag in the backlog), but the commit it produced
            # was not named, so there is nothing to compare. Reported on its own
            # line rather than quietly grouped with the not-yet-deployed, since
            # the remedy differs — this one will not clear on the next deploy.
            pending.append(
                ArchiveResult(
                    action_taken="pending-deploy",
                    detail=(
                        "the merged pull request did not name the commit it "
                        "merged, so there is no way to tell whether this "
                        "deployment contains the removal. Left outstanding; "
                        "`mode: archive-sweep` archives it on the merge alone"
                    ),
                    key=candidate.key,
                    pull_request=candidate.pull_request,
                )
            )
            continue

        try:
            contained = commit_is_contained(
                gh, repo, candidate.merge_commit_sha, within=deployed_sha
            )
        except GitHubApiError as exc:
            # Reported as `failed` rather than `pending-deploy`, deliberately:
            # the run goes red and this flag is named, because "I could not
            # tell" is not the same as "not yet" and will not clear by itself.
            # The flag key leads the line — the exception names only the two
            # shas, which is not something a customer can act on.
            logger.warning("could not place flag %s against this deploy: %s", candidate.key, exc)
            pending.append(
                ArchiveResult(
                    action_taken="failed",
                    detail=(
                        f"could not tell whether this deployment contains the removal: "
                        f"{exc} The flag is left outstanding; `mode: archive-sweep` "
                        f"archives it on the merge alone"
                    ),
                    key=candidate.key,
                    pull_request=candidate.pull_request,
                )
            )
            continue

        if contained:
            deployed.append(candidate)
            continue

        logger.info(
            "flag %s stays outstanding: %s is not contained in the deployed commit %s",
            candidate.key,
            candidate.merge_commit_sha[:12],
            deployed_sha[:12],
        )
        pending.append(
            ArchiveResult(
                action_taken="pending-deploy",
                detail=(
                    f"the removal merged as {candidate.merge_commit_sha[:12]}, which "
                    f"this deployment ({deployed_sha[:12]}) does not contain — so "
                    f"the code that reads this flag is still running. Left "
                    f"outstanding for the deploy that ships it"
                ),
                key=candidate.key,
                pull_request=candidate.pull_request,
            )
        )

    return deployed, pending
