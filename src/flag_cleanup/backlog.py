"""``mode: archive-sweep`` — re-attempt the archives that deferred.

``archive-on-merge`` fires once, on the merge, and a flag live traffic is still
evaluating comes back ``deferred`` rather than archived (see
:mod:`flag_cleanup.archive`). That is only safe because something retries it. A
deferred archive nothing comes back for is just a leak: the flag stays live
forever and the loop is half-done again, which is the state ``archive-on-merge``
was written to end. This module is the thing that comes back.

**The candidate set needs no new state, and that is the design.** A flag whose
removal pull request merged but whose archive never landed is, by definition,
still unarchived — the flag's own ``isArchived`` bit IS the pending marker. And
``github_ops.removal_branch`` is a documented bijection, so a flag key maps to
the exact branch its removal pull request would have carried. So *"merged
removal pull requests whose flag is still outstanding"* is computable from what
already exists, with no marker file, no pull-request-body parsing, and nothing
written at deferral time.

**Computed from the Featureflip end, not the GitHub end.** The set is the same
either way; the cost is not. Walking the repository's closed pull requests looking
for removal branches is bounded by the repository's entire history and cannot be
narrowed server-side (GitHub's ``head`` filter is an exact branch name, not a
prefix), so a monorepo would pay tens of thousands of reads and a cap on them
would silently drop the oldest outstanding flag. Walking the project's live flags
is bounded by the project's flag count, which is smaller by orders of magnitude
and shrinks as the sweep does its job.

**The cost this mode does NOT get to keep quiet about.** ``archive-on-merge``
reads its pull request out of the event payload, touches no source, and needs
nothing at all from ``GITHUB_TOKEN``. This mode asks GitHub once per live flag
whether that flag's removal branch ever produced a merged pull request, so it
needs a token with ``pull-requests: read``. That is a real trade against the
property that made the other mode unusually cheap, and it is documented in the
README rather than quietly widened.

**Per-flag isolation**, like ``orchestrate`` and unlike ``archive-on-merge``:
one flag the domain refuses does not stop the other nine from draining, and
every outcome gets its own reported line. The exception is a token that cannot
archive, which is provably not about any one flag — the sweep stops there rather
than spending a request per flag to print the same sentence.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from flag_cleanup.archive import ArchiveResult, deferred_detail
from flag_cleanup.client import (
    ArchivePermissionError,
    FeatureflipApiError,
    FeatureflipClient,
    FlagRecentlyEvaluatedError,
)
from flag_cleanup.config import Config
from flag_cleanup.github_ops import (
    GitHubApi,
    GitHubEnv,
    UnsafeFlagKeyError,
    merged_removal,
    removal_branch,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutstandingArchive:
    """One flag whose removal merged and whose archive has not landed."""

    key: str
    branch: str
    #: ``None`` when the merged pull request's response carried no number.
    pull_request: int | None
    #: ``""`` when it carried no URL. Both degrade rather than discarding the
    #: merge evidence itself, which is the part that authorizes the archive.
    pull_request_url: str
    #: The commit the merge produced on the base branch, or ``""``. Read by
    #: ``mode: archive-on-deploy`` alone — the sweep archives on the strength
    #: of the merge itself, and the merge-triggered mode never gets here.
    merge_commit_sha: str = ""


def outstanding_archives(
    config: Config,
    gh: GitHubApi,
    repo: str,
    *,
    client: FeatureflipClient,
) -> list[OutstandingArchive]:
    """Every flag whose removal pull request merged but which is still live.

    The shared lookup, and it now has two consumers rather than the one it was
    written for: this module's sweep and
    :func:`flag_cleanup.deploy.run_archive_on_deploy`, which asks the identical
    question — *which merged removals have a flag still outstanding* — and then
    narrows the answer to the removals a given deployed commit contains. That
    is why it lives on its own, and why :attr:`OutstandingArchive.merge_commit_sha`
    is carried here rather than fetched again a layer up.

    Neither the API client nor the GitHub client is created or closed here: a
    caller that already holds one should not be made to build a second, and this
    function owns neither.

    Order follows :meth:`FeatureflipClient.unarchived_flag_keys`, so two runs
    over an unchanged project report in the same order and diff cleanly.
    """
    outstanding: list[OutstandingArchive] = []
    for key in client.unarchived_flag_keys(config.org, config.project):
        try:
            branch = removal_branch(key)
        except UnsafeFlagKeyError as exc:
            # `removal_branch` refuses exactly the keys it could never have
            # written a branch for, so this flag cannot have a merged removal
            # pull request to finish. Not a failure, and not the sweep's to
            # report: the `remove` run that skipped this flag already said so
            # loudly, and repeating it here every day would be noise.
            logger.debug("flag %s has no representable removal branch: %s", key, exc)
            continue

        merged = merged_removal(gh, repo, branch)
        if merged is None:
            continue

        outstanding.append(
            OutstandingArchive(
                key=key,
                branch=branch,
                pull_request=merged.number,
                pull_request_url=merged.html_url,
                merge_commit_sha=merged.merge_commit_sha,
            )
        )
    return outstanding


def run_archive_sweep(
    config: Config,
    env: Mapping[str, str] | None = None,
    *,
    client: FeatureflipClient | None = None,
    gh: GitHubApi | None = None,
) -> list[ArchiveResult]:
    """Re-attempt every outstanding archive; return one result per flag.

    Both clients are injectable so tests never open a socket; each is closed
    only when this function built it.

    Raises :class:`~flag_cleanup.github_ops.GitHubConfigError` when the workflow
    did not pass ``GITHUB_TOKEN`` through — this mode, unlike
    ``archive-on-merge``, genuinely needs it, and ``__main__`` renders that as
    exit 2 ("the run could not start") alongside the other wiring faults.
    """
    github = GitHubEnv.from_env(env)

    owned_gh = gh is None
    owned_client = client is None
    active_gh = gh if gh is not None else GitHubApi(github.token, api_url=github.api_url)
    active = (
        client if client is not None else FeatureflipClient(config.api_url, config.api_token)
    )
    try:
        candidates = outstanding_archives(config, active_gh, github.repository, client=active)
        return archive_outstanding(config, candidates, active)
    finally:
        if owned_gh:
            active_gh.close()
        if owned_client:
            active.close()


def archive_outstanding(
    config: Config,
    candidates: list[OutstandingArchive],
    client: FeatureflipClient,
) -> list[ArchiveResult]:
    """One archive attempt per candidate, isolated from each other.

    Shared with ``mode: archive-on-deploy``, which hands it a NARROWER list —
    the outstanding archives a deployed commit actually contains — and nothing
    else. Keeping the loop here rather than copying it is what makes the two
    modes report in one vocabulary: same outcome names, same dry-run wording,
    and one place where the per-flag/whole-token distinction below is decided.
    """
    results: list[ArchiveResult] = []
    for candidate in candidates:
        opened = _merge_reference(candidate)

        if config.dry_run:
            # Dry-run parity: the real run's candidate set, computed exactly the
            # same way, with the POST withheld. It deliberately does NOT claim
            # each one would be archived — whether an attempt lands or defers is
            # a fact only the attempt has, and asserting it here would be the
            # one thing a preview must never do.
            results.append(
                ArchiveResult(
                    action_taken="would-archive",
                    detail=f"would attempt the archive {opened} (dry run: nothing was sent)",
                    key=candidate.key,
                    pull_request=candidate.pull_request,
                )
            )
            continue

        try:
            client.archive_flag(config.org, config.project, candidate.key)
        except FlagRecentlyEvaluatedError as exc:
            results.append(
                ArchiveResult(
                    action_taken="deferred",
                    detail=deferred_detail(exc),
                    key=candidate.key,
                    pull_request=candidate.pull_request,
                )
            )
            continue
        except ArchivePermissionError as exc:
            # Not this flag's problem — the token cannot archive anything, so
            # every remaining candidate would fail identically. Reported as a
            # failure like any other (the build still goes red) and then the
            # sweep stops, rather than spending one request per flag to print
            # the same sentence N times.
            results.append(
                ArchiveResult(
                    action_taken="failed",
                    detail=(
                        f"{exc} Stopping here: this is the token, not this flag, so "
                        f"every remaining outstanding archive would fail the same way."
                    ),
                    key=candidate.key,
                    pull_request=candidate.pull_request,
                )
            )
            break
        except FeatureflipApiError as exc:
            # Everything else IS one flag's problem — a dependent prerequisite,
            # a pending schedule, a flag deleted since its pull request merged.
            # Reported and carried on from, so one stuck flag cannot hold the
            # rest of the backlog hostage.
            results.append(
                ArchiveResult(
                    action_taken="failed",
                    detail=str(exc),
                    key=candidate.key,
                    pull_request=candidate.pull_request,
                )
            )
            continue

        logger.info("archived outstanding flag %s (%s)", candidate.key, opened)
        results.append(
            ArchiveResult(
                action_taken="archived",
                detail=f"archived {opened}",
                key=candidate.key,
                pull_request=candidate.pull_request,
            )
        )
    return results


def _merge_reference(candidate: OutstandingArchive) -> str:
    """How to name the merge in a report line, however degraded the response."""
    if candidate.pull_request is not None:
        return f"after pull request #{candidate.pull_request} merged"
    return f"after the pull request from {candidate.branch} merged"
