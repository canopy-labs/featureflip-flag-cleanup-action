"""CLI entrypoint: build :class:`Config` from the environment and run.

``python -m flag_cleanup`` is what the Docker action's ``ENTRYPOINT`` invokes.
It prints one line per candidate and, for a dry run, the diff that *would* have
been proposed, and closes with a one-line summary of the whole run — including
when there were no candidates at all, which is the case that otherwise printed
nothing and left a healthy run looking like a broken one.

Exit codes: ``0`` everything the run attempted worked; ``1`` at least one flag
could not be proposed, or the run aborted part-way (the candidates it got
through are still reported — see ``orchestrate``'s per-flag isolation); ``2``
the run could not start at all (bad configuration, or a configured directory
this tool cannot work in safely).

"Could not be proposed" is deliberately wider than "errored": a key this tool
cannot turn into a branch or a rule operand, an engine abort, and a Gate 1
refusal all mean the same thing to the customer — that flag will never get a
pull request, on this run or any future one, until something changes. Exiting
0 for those made a scheduled run look healthy while it silently proposed
nothing, which is the failure this tool exists to avoid rather than cause.
A genuine ``no-changes`` is NOT in that set: nothing to do is success.

``mode: archive-on-merge`` reads the same codes but reaches far fewer of them:
it archives one flag or it does not, so there is no partial result. A pull
request that did not merge, or did not come from a branch this Action created,
is ``0`` — those are the ordinary outcomes of a workflow that fires on every
closed pull request, not failures. A trigger that cannot work at all is ``2``;
an API that refuses the archive is ``1``.

Nothing reaches the customer as a bare traceback: a misconfigured workflow is
by far the most common failure and deserves a one-liner, and an abort mid-run
still has real results to report. The exceptions are the two things that are
not failures of this tool — a cancelled run (``KeyboardInterrupt``) and an
explicit ``SystemExit``. Both are reported and then re-raised untouched.
"""

from __future__ import annotations

import logging
import sys
from collections import Counter

from flag_cleanup.archive import run_archive
from flag_cleanup.client import FeatureflipApiError
from flag_cleanup.config import MODE_ARCHIVE_ON_MERGE, Config, ConfigError
from flag_cleanup.git_ops import GitCommandError, PreflightError
from flag_cleanup.github_ops import BaseRefError, GitHubApiError, GitHubConfigError
from flag_cleanup.orchestrate import (
    PARTIAL_RESULTS_ATTR,
    RUN_STARTED_ATTR,
    FlagResult,
    run,
)
from flag_cleanup.pr_event import PullRequestEventError

logger = logging.getLogger(__name__)

#: Outcomes that mean "this flag was not proposed and will not be until
#: something changes". Every one of them turns the build red — see the module
#: docstring for why ``no-changes`` is not among them.
UNPROPOSABLE_ACTIONS = ("failed", "unsafe-key", "piranha-error", "unsafe-rewrite")


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI flags yet — everything comes from the environment
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request at INFO as a full URL — including any userinfo
    # in `api-url`. That is the exact value `orchestrate._without_userinfo`
    # strips before a self-hosted URL can reach a PR body, and a workflow log
    # is just as readable, so silencing it here closes the same leak on the
    # other side. (The API token itself never appears: it rides in a header.)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    dry_run = False
    try:
        config = Config.from_env()
        dry_run = config.dry_run
        if config.mode == MODE_ARCHIVE_ON_MERGE:
            # Returns from inside the `try` so the handler below still maps its
            # two failure types: a misconfigured trigger to exit 2 (nothing was
            # archived), an API refusal to exit 1.
            return _archive(config)
        results = run(config)
    except BaseException as exc:  # noqa: BLE001 - an abort still has results to report
        # A clean one-liner beats a traceback for the most common failures: a
        # workflow that forgot to pass a secret through, a `directories` entry
        # that is not a git working tree, or a checkout that removal branches
        # cannot safely be cut from. Raised before any candidate is processed,
        # so "nothing was modified" holds for exit 2.
        #
        # The TYPE alone does not establish that, which is why the marker is
        # also required. `PreflightError` escapes mid-run too — from
        # `git_ops.work_dir`, when a configured directory disappears under the
        # run — by which point earlier flags may already have pull requests.
        # Reporting that as exit 2 told the customer nothing had been modified
        # AND skipped the report naming the PRs they had just been given.
        if isinstance(
            exc,
            (
                ConfigError,
                GitHubConfigError,
                PreflightError,
                BaseRefError,
                PullRequestEventError,
            ),
        ) and not getattr(exc, RUN_STARTED_ATTR, False):
            print(f"flag-cleanup: {exc}", file=sys.stderr)
            return 2

        # `run` stamps the completed results onto whatever aborted it — plus a
        # line for the flag it died on — so a failure on candidate 3 does not
        # also hide what candidates 1 and 2 did.
        _report(getattr(exc, PARTIAL_RESULTS_ATTR, []), dry_run=dry_run)

        # `run` catches BaseException to do that stamping, so this must too, or
        # the one case the stamping exists for throws the report away: a
        # cancelled workflow (Actions sends SIGINT -> `KeyboardInterrupt`)
        # after three pull requests were opened printed nothing about them.
        # The report is out; what happens next is not this tool's to decide.
        # `KeyboardInterrupt` and `SystemExit` are not failures of the run and
        # must not be flattened into exit 1 — re-raise and let the interpreter
        # apply its own exit semantics.
        if not isinstance(exc, Exception):
            raise
        # A refusal we already explained (GitHub said no, git said no) has
        # nothing to add in a stack trace, and burying the one clear message
        # under one is the opposite of what those messages are for. Anything
        # else is a bug in this tool and keeps its traceback.
        #
        # Note git failures arrive as TWO types and are treated oppositely on
        # purpose. `GitCommandError` is git *declining* something, raised with
        # the command, exit code and stderr already in the message. A bare
        # `subprocess.CalledProcessError` only escapes from `reset_worktree`,
        # which deliberately does not wrap it: a failed undo may have left this
        # flag's edits on disk, so it is a condition to investigate rather than
        # a refusal to summarise. Same for `WorktreeResetError`. Both keep
        # their tracebacks by being absent from `expected`.
        # Pinned by test_main.py's refused-git-command / failed-undo pair.
        # `FeatureflipApiError` belongs here rather than in the exit-2 tuple
        # above: candidates are paginated lazily, so a 4xx on page two arrives
        # after earlier flags already have pull requests, and "nothing was
        # modified" would be a lie. Its message is already credential-stripped
        # and actionable, so it needs no traceback either.
        expected = isinstance(
            exc, (GitHubApiError, GitCommandError, FeatureflipApiError)
        )
        logger.error("run aborted: %s", exc, exc_info=not expected)
        print(f"flag-cleanup: run aborted: {exc}", file=sys.stderr)
        return 1

    _report(results, dry_run=dry_run)
    _summarize(results, staleness=config.staleness)

    unproposable = [
        f"{result.key} ({result.action_taken})"
        for result in results
        if result.action_taken in UNPROPOSABLE_ACTIONS
    ]
    if unproposable:
        # Surface partial failure as a red build: the run did as much as it
        # could, but the customer needs to know some flags were not proposed.
        # The action is named per flag because the remedies differ — a rejected
        # push is retryable, a refused rewrite needs the named file edited, and
        # an unrepresentable key needs the flag renamed.
        print(
            f"flag-cleanup: {len(unproposable)} flag(s) could not be proposed: "
            f"{', '.join(unproposable)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _archive(config: Config) -> int:
    """Run archive-on-merge and print the single line that says what happened.

    Always exit 0 on a clean run, including both no-ops. A workflow triggered
    on every closed pull request will take the `not-a-removal-branch` path far
    more often than any other, and a red build for "this pull request was not
    one of ours" would train people to ignore the one that matters.

    Printed rather than logged, matching `_report`/`_summarize`: stdout is what
    a workflow log and job summary capture.
    """
    result = run_archive(config)
    key = f" {result.key}" if result.key else ""
    print(f"[{result.action_taken}]{key} {result.detail}", flush=True)
    return 0


def _report(results: list[FlagResult], *, dry_run: bool) -> None:
    """Print one line per candidate (plus the diff, when dry-running)."""
    for result in results:
        detail = f" {result.pr_url}" if result.pr_url else ""
        print(
            f"[{result.action_taken}] {result.key} "
            f"(status={result.status}, treatment={result.treatment}){detail}"
        )
        if result.unprocessed_files:
            # Printed, not merely logged: this is the caveat that changes what
            # the diff means, and stdout is what a workflow summary captures.
            print(
                f"    warning: {len(result.unprocessed_files)} file(s) still "
                f"mention this flag: {', '.join(result.unprocessed_files)}"
            )
        if result.refused_files:
            # Same reasoning, one step stronger: this flag produced NOTHING,
            # and these files are the reason. Without the list the customer
            # sees a refusal they cannot act on.
            print(
                f"    refused: the rewrite of {len(result.refused_files)} file(s) "
                "did not survive the syntax check, so the whole flag was "
                f"abandoned: {', '.join(result.refused_files)}"
            )
        if dry_run and result.diff:
            print(result.diff)


def _summarize(results: list[FlagResult], *, staleness: str) -> None:
    """Close a COMPLETED run with one line saying what it did.

    Without this, a run whose candidate list came back empty printed literally
    nothing — `_report` only emits per-candidate lines — so a healthy "nothing
    to do" was byte-for-byte identical in the job log to a run that did nothing
    for a bad reason. That ambiguity is what the first dogfood dispatches hit,
    and it costs a real investigation every time because the log offers no
    thread to pull.

    Printed, not logged, for the same reason the unprocessed/refused caveats
    are: stdout is what a workflow log and job summary capture.

    ``flush=True`` is what makes it read as the CLOSING line. The image sets no
    ``PYTHONUNBUFFERED``, so a piped stdout — which is exactly what Actions
    gives the container — is block-buffered while stderr is not, and the
    "could not be proposed" line below would otherwise surface ABOVE the report
    it summarises. Flushing here drains everything ``_report`` wrote too, so the
    whole success path lands in the order it was written.

    Deliberately NOT called from the abort path, even though it also reports a
    (partial) result list. An abort that completed no candidates has an empty
    list too, but "0 removal candidates, nothing to do" would assert something
    the run never got far enough to know; the abort message on stderr is the
    honest account there. Pinned by test_main.py's
    `test_an_abort_is_not_summarised_as_nothing_to_do`.
    """
    if not results:
        # The staleness tier is the one input that decides whether an empty
        # result is the right answer or the query was simply pointed at the
        # wrong bucket, so it goes in the line that reports the emptiness.
        print(
            f"flag-cleanup: 0 removal candidates (staleness={staleness}), "
            "nothing to do",
            flush=True,
        )
        return

    # Busiest outcome first, ties broken alphabetically, so the line is stable
    # across runs and diffable between them. Counting whatever `action_taken`
    # actually holds — rather than enumerating the known actions — means a new
    # outcome shows up here the day it is added instead of being silently
    # dropped from the tally.
    counts = Counter(result.action_taken for result in results)
    breakdown = ", ".join(
        f"{count} {action}"
        for action, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    noun = "candidate" if len(results) == 1 else "candidates"
    print(f"flag-cleanup: {len(results)} {noun}: {breakdown}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
