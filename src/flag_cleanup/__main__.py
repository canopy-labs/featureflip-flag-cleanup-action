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
closed pull request, not failures. So is ``deferred``: live traffic still
evaluating the flag is a refusal that lifts by itself once the removal deploys,
with nothing for anyone to do, and it is the ordinary case wherever merge and
deploy are separate events. A trigger that cannot work at all is ``2``; every
OTHER API refusal is ``1``.

``mode: archive-sweep`` reports a list rather than one line and follows
``remove``'s partial-failure rule rather than ``archive-on-merge``'s: ``0`` when
every outstanding archive either landed or deferred (including when there were
none, and including a dry run), ``1`` when any flag could not be archived —
which names the flags, because the remedies differ per flag. ``2`` is the run
failing to start: no ``GITHUB_TOKEN``, which the two GitHub-reading archive
modes genuinely need.

``mode: archive-on-deploy`` reports the same way and reads the same codes, with
two outcomes of its own that are both ``0``. A delivery this mode does not act
on — a status that is not ``success``, or a deployment to an environment other
than the configured one — is ONE line and no summary, because there was no
backlog to summarise. A merged removal the deployed commit does not contain is
``pending-deploy``: named, left outstanding, and picked up by the next deploy
that ships it (or by the sweep). ``2`` is again the run failing to start — the
wrong trigger, or no ``GITHUB_TOKEN``.

``mode: pr-command`` answers ONE command a human left as a comment, so almost
every outcome is ``0`` — including a comment from someone without write
access, a branch this Action never opened, a re-run that comes back empty
because nothing reads the flag any more, or a **dry run**, which regenerates
and reports exactly what it would have done but pushes nothing and leaves the
pull request untouched: each of those is an answer, not a failure. ``1`` is
for the four outcomes where the command genuinely could not do what it was
asked (the branch carries commits this Action did not make, the pull request
already merged, the flag is no longer a removal candidate, or the flag is in
this workflow's ``ignore`` list — the same "silence it, don't resurrect it"
rule the ``flags`` allowlist is held to), for a transform that raised
outright, and for ``BaseRefError`` — which in THIS mode
means the one pull request the command is acting on targets a base the
checkout never saw, not that the workflow itself is broken, so it is
deliberately excluded from the exit-2 set below even though the identical type
means exactly that for ``remove``. ``issue_comment`` triggers attach no check
run to the pull request itself, so a raised failure also gets a reply posted
on the pull request before the non-zero exit — otherwise the one person
waiting on an answer, the one who typed the command, is the one person who
never gets one. ``2`` is the workflow wired to the wrong trigger, exactly as
for ``archive-on-merge``, and — reached through the same preflight ``remove``
mode opens with — a checkout this tool cannot safely operate in: a configured
directory that is not a git working tree, that belongs to a different
repository than the first one, or that carries uncommitted changes to tracked
files, which this mode would otherwise commit onto the customer's branch and
then destroy. Nothing has been transformed, committed or pushed when that
fires, so exit 2's "the run could not start" reading holds, and the reply is
still posted.

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

from flag_cleanup import github_ops
from flag_cleanup.archive import ArchiveResult, run_archive
from flag_cleanup.backlog import run_archive_sweep
from flag_cleanup.client import FeatureflipApiError
from flag_cleanup.config import (
    MODE_ARCHIVE_ON_DEPLOY,
    MODE_ARCHIVE_ON_MERGE,
    MODE_ARCHIVE_SWEEP,
    MODE_PR_COMMAND,
    Config,
    ConfigError,
)
from flag_cleanup.deploy import SKIPPED_ACTIONS, run_archive_on_deploy
from flag_cleanup.git_ops import ForceWithLeaseRejected, GitCommandError, PreflightError
from flag_cleanup.github_ops import (
    BaseRefError,
    GitHubApi,
    GitHubApiError,
    GitHubConfigError,
    GitHubEnv,
)
from flag_cleanup.orchestrate import (
    PARTIAL_RESULTS_ATTR,
    RUN_STARTED_ATTR,
    FlagResult,
    run,
)
from flag_cleanup.pr_command import (
    PrCommandResult,
    branch_was_force_pushed,
    pull_request_was_updated,
    run_pr_command,
)
from flag_cleanup.pr_event import PullRequestEventError, commented_pull_request

logger = logging.getLogger(__name__)

#: Outcomes that mean "this flag was not proposed and will not be until
#: something changes". Every one of them turns the build red — see the module
#: docstring for why ``no-changes`` is not among them.
UNPROPOSABLE_ACTIONS = ("failed", "unsafe-key", "piranha-error", "unsafe-rewrite")


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI flags yet — everything comes from the environment
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request at INFO as a full URL — including any userinfo
    # in `api-url`. That is the exact value `pr_content._without_userinfo`
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
        if config.mode == MODE_ARCHIVE_SWEEP:
            # Same reasoning again: a workflow that did not pass GITHUB_TOKEN
            # through raises `GitHubConfigError`, which the handler below
            # renders as exit 2 — nothing was archived.
            return _archive_sweep(config)
        if config.mode == MODE_ARCHIVE_ON_DEPLOY:
            # Same again, with two ways to reach exit 2: a trigger that is not
            # `deployment_status` (`PullRequestEventError`) and a workflow that
            # did not pass GITHUB_TOKEN through (`GitHubConfigError`). Both are
            # raised before the environment gate, so nothing was archived.
            return _archive_on_deploy(config)
        if config.mode == MODE_PR_COMMAND:
            # Same reasoning: a misconfigured trigger (`PullRequestEventError`)
            # falls through to exit 2 below, and a raised transform failure
            # falls through to exit 1 — `_pr_command` only adds the reply.
            return _pr_command(config)
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
        #
        # `BaseRefError` is the one type in this tuple whose MEANING depends on
        # the mode, not just on whether the run had started. In `remove` mode
        # it names a misconfigured `base-branch` input or trigger — the
        # workflow itself cannot work as written, so exit 2 is right. In
        # `pr-command` mode the identical type means something else entirely:
        # `ensure_head_on_base` raised it for THIS ONE pull request, whose
        # base the checkout never saw — the workflow is fine, and every other
        # comment on every other pull request would work. Exit 2 would send
        # the customer to fix a workflow file that has nothing wrong with it,
        # so it is excluded here and falls through to the ordinary exit-1
        # path below instead — `config` is always bound by this point, since
        # the type can only be raised from `run`/`_pr_command`, both reached
        # only after `Config.from_env()` already succeeded.
        pr_command_base_ref_failure = (
            isinstance(exc, BaseRefError) and config.mode == MODE_PR_COMMAND
        )
        if (
            isinstance(
                exc,
                (
                    ConfigError,
                    GitHubConfigError,
                    PreflightError,
                    BaseRefError,
                    PullRequestEventError,
                ),
            )
            and not getattr(exc, RUN_STARTED_ATTR, False)
            and not pr_command_base_ref_failure
        ):
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
    _print_archive_result(result)
    return 0


def _print_archive_result(result: ArchiveResult) -> None:
    key = f" {result.key}" if result.key else ""
    print(f"[{result.action_taken}]{key} {result.detail}", flush=True)


def _archive_sweep(config: Config) -> int:
    """Run archive-sweep and print one line per outstanding flag, then a total.

    Follows `remove`'s partial-failure shape rather than `_archive`'s all-or-
    nothing one, because this mode HAS partial results: `deferred` and
    `archived` are both fine, one flag the domain refuses does not invalidate
    the others, and the flags that failed are named because their remedies
    differ from each other.

    Printed rather than logged, matching every other reporter here: stdout is
    what a workflow log and job summary capture.
    """
    results = run_archive_sweep(config)
    for result in results:
        _print_archive_result(result)
    _summarize_sweep(results, dry_run=config.dry_run)
    return _archive_exit_code(results)


def _archive_on_deploy(config: Config) -> int:
    """Run archive-on-deploy: one line per outstanding flag, then a total.

    Shares `_archive_sweep`'s reporting because it shares its candidate set —
    same outcome vocabulary, same partial-failure rule, same summary sentence
    for an empty backlog.

    The one addition is the delivery-level no-op. A status that is not
    `success`, or a deployment to another environment, comes back as a single
    result and is printed WITHOUT the summary: "0 outstanding flags" would
    claim this run looked at the backlog, and it did not — that is the whole
    point of the gate it stopped at.
    """
    results = run_archive_on_deploy(config)
    for result in results:
        _print_archive_result(result)

    if len(results) == 1 and results[0].action_taken in SKIPPED_ACTIONS:
        return 0

    _summarize_sweep(results, dry_run=config.dry_run)
    return _archive_exit_code(results)


def _archive_exit_code(results: list[ArchiveResult]) -> int:
    """`0`, or `1` naming every flag that could not be archived.

    Named per flag because the remedies differ per flag — a dependent
    prerequisite, a pending schedule and a deleted flag each need something
    different, and a bare count sends the reader back to the log to find out
    which.
    """
    failed = [result.key or "?" for result in results if result.action_taken == "failed"]
    if failed:
        print(
            f"flag-cleanup: {len(failed)} outstanding archive(s) failed: "
            f"{', '.join(failed)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _summarize_sweep(results: list[ArchiveResult], *, dry_run: bool) -> None:
    """Close the sweep with one line saying what it did.

    An empty backlog is the HEALTHY steady state of this mode and prints its
    own sentence, for `_summarize`'s reason one mode over: without it a clean
    sweep emits nothing at all, and "everything is archived" is byte-for-byte
    identical in the job log to a sweep that was pointed at the wrong project
    and found nothing because nothing was there to find.
    """
    if not results:
        print(
            "flag-cleanup: no outstanding archives — every merged removal in "
            "this project is already archived",
            flush=True,
        )
        return

    counts = Counter(result.action_taken for result in results)
    breakdown = ", ".join(
        f"{count} {action}"
        for action, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    noun = "flag" if len(results) == 1 else "flags"
    suffix = " (dry run — nothing was archived)" if dry_run else ""
    print(
        f"flag-cleanup: {len(results)} outstanding {noun}: {breakdown}{suffix}",
        flush=True,
    )


#: pr-command outcomes that must turn the build red. Each is a command a human
#: typed and is waiting on, which did not happen — the reply says why, and the
#: red check is what a reader scanning the pull request sees without opening
#: comments. `rerun-empty` and `rerun-dry-run` are deliberately NOT here:
#: "the code no longer reads this flag" and "here is what would have happened"
#: are both answers, not failures.
PR_COMMAND_FAILURES = (
    "rerun-refused",
    "rerun-merged",
    "rerun-not-a-candidate",
    "rerun-ignored",
)


def _pr_command(config: Config) -> int:
    """Run pr-command mode and print the single line that says what happened.

    Printed rather than logged, matching `_archive` and `_report`: stdout is
    what a workflow log and job summary capture.

    `run_pr_command` RAISES rather than returning an outcome for a transform
    refusal (see its module docstring) — every ordinary outcome, including
    every member of `PR_COMMAND_FAILURES`, already replies for itself via
    `pr_command._reply` before returning here. A raised failure gets no such
    reply, and `issue_comment` triggers attach no check run to the pull
    request, so without one the human who typed the command — the one person
    actually waiting on this run — is the one person who never sees an answer.
    `_reply_to_pr_command_failure` closes that gap; the exit code itself is
    still decided by `main`'s existing handler, exactly as the module
    docstring's exit table says.
    """
    try:
        result: PrCommandResult = run_pr_command(config)
    except PullRequestEventError:
        # Raised before any comment is even identified (wrong trigger, unusable
        # payload) — there is structurally no pull request to reply to, and
        # `main`'s handler already renders this as exit 2. Nothing to add here.
        raise
    except Exception as exc:
        # Best-effort and never allowed to mask `exc`: whatever happens inside
        # the reply, the bare `raise` below re-raises the ORIGINAL exception
        # untouched, so the exit code and the "run aborted" message on stderr
        # are always about the real failure, never about a broken reply.
        _reply_to_pr_command_failure(exc)
        raise
    key = f" {result.key}" if result.key else ""
    print(f"[{result.action_taken}]{key} {result.detail}", flush=True)
    if result.action_taken in PR_COMMAND_FAILURES:
        print(f"flag-cleanup: {result.detail}", file=sys.stderr)
        return 1
    return 0


def _reply_to_pr_command_failure(exc: Exception) -> None:
    """Tell the human who typed the command that the re-run failed.

    Re-reads the triggering event rather than threading anything out of
    `run_pr_command`: that function closes its OWN GitHub client in a
    `finally` before an exception it raised can reach here, so this needs its
    own comment lookup and its own client regardless.

    Every step is best-effort and wrapped in one broad `except`, deliberately:
    a workflow broken enough to have raised in the first place (a missing
    `GITHUB_TOKEN`, a non-comment trigger) fails the SAME way here, and that
    must be swallowed — logged, not raised — rather than replacing the
    failure this function exists to report.
    """
    try:
        comment = commented_pull_request()
        if comment is None:
            return
        github = GitHubEnv.from_env()
        gh = GitHubApi(github.token, api_url=github.api_url)
        try:
            github_ops.comment_on_pull_request(
                gh,
                github.repository,
                comment.number,
                _pr_command_failure_body(exc),
            )
        finally:
            gh.close()
    except Exception as reply_exc:  # noqa: BLE001 - best-effort only
        logger.warning(
            "could not reply on the pull request about a failed pr-command "
            "run: %s",
            reply_exc,
        )


def _pr_command_failure_body(exc: Exception) -> str:
    """A short, readable comment body naming what failed.

    `exc`'s own message can be multi-line engine output (a Piranha abort, a
    Gate 1 refusal listing several files) — reduced to its first line so the
    reply stays a sentence a non-expert can act on, rather than pasting raw
    engine output into a pull request comment. The full message is still one
    `gh run view` away in the workflow log.

    A REJECTED LEASE gets its own branch rather than that truncation, because
    truncating it drops the only informative line: `GitCommandError`'s first
    line is the command plus `To <url>`, and git puts the
    `! [rejected] ... (stale info)` reason on the second. Widening the
    truncation for everything would paste engine output into a comment, which
    is what it exists to prevent — so the fix is a branch for the one failure
    whose remedy is a sentence. `git_ops` classifies it; this only phrases it.

    How far the run GOT is read off the exception rather than assumed, and it
    has three states rather than two. The force-push is not the last step: the
    `PATCH` that rewrites the title and body follows it, and the reply, the ref
    restore and the worktree verification follow that.

    * Nothing pushed — the ordinary case, and worth saying plainly.
    * Pushed, not updated (a token without `pull-requests: write`, a 422, a
      locked pull request): the branch is new and the body still narrates the
      old diff, which is the one thing that person has to know.
    * Pushed AND updated, then something later failed (a refused undo, a
      cancelled job, a failed reply): both halves of the change are correct and
      the RUN is what did not finish.

    Collapsing the last two — which a single "was it pushed" flag forces — sent
    the reader to a pull-request description that was already right, and away
    from the checkout a refused `snapshot.verify()` may have left holding this
    flag's edits. That failure deliberately keeps its traceback (see the note
    on `expected` in `main`), so the reply's job is to stop misdirecting rather
    than to describe the working tree, which it cannot honestly do from here.
    """
    if isinstance(exc, ForceWithLeaseRejected):
        return (
            "**flag-cleanup — command failed**\n\n"
            f"`{exc.branch}` moved on the remote after this run read it, so the "
            "force-push was refused rather than overwriting whatever landed "
            "there.\n\n"
            "Nothing was pushed. Comment the command again to regenerate "
            "against the branch as it now stands."
        )
    message = str(exc).strip()
    first_line = message.splitlines()[0] if message else exc.__class__.__name__
    if not branch_was_force_pushed(exc):
        outcome = "Nothing was pushed."
    elif not pull_request_was_updated(exc):
        outcome = (
            "The branch WAS force-pushed, but this pull request could not be "
            "updated, so its description still describes the previous diff."
        )
    else:
        outcome = (
            "The branch was force-pushed and this pull request was updated, "
            "but the run did not finish."
        )
    return (
        "**flag-cleanup — command failed**\n\n"
        f"{first_line}\n\n"
        f"{outcome} See this workflow run's log for the full output."
    )


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
