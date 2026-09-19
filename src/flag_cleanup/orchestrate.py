"""The run loop: fetch removal candidates, run Piranha per flag, open one PR each.

Runs inside the CUSTOMER's CI. Every configured directory is preflighted once
(inside a git work tree, no uncommitted changes) before anything is written.
Then for every candidate this module fans the flag out across each configured
directory/language, collects Piranha's diff, and ALWAYS undoes the transform —
success, no match, or a caught refusal (:class:`PiranhaTransformError` /
:class:`UnsafeRewriteError`) — before moving to the next candidate.

That undo is the safety-critical operation in the whole tool, so it is
**verified, not assumed**: the files Piranha could rewrite are snapshotted
before it runs, ``git checkout`` is the coarse net, and the snapshot then
restores anything git missed (untracked/gitignored source is the ordinary
case) and re-reads every file to prove it matches. A reset that cannot be
proved raises and aborts the run — one flag's deletions reaching another
flag's pull request is the worst outcome this tool has.

Two things a naive "diff is non-empty -> flag fully removed" story would hide
are surfaced explicitly instead of swallowed:

* a REFUSED transform — the engine aborting (``PiranhaTransformError``) or
  Gate 1 rejecting a rewrite (``UnsafeRewriteError``) — abandons the flag
  entirely, under its own ``action_taken`` (``piranha-error`` /
  ``unsafe-rewrite``) and never as ``no-changes``. Refusing is the safe
  answer; refusing *quietly* is not, because "this repository has nothing to
  clean up" and "I declined to clean it up" are opposite facts that the
  customer reads off the same line. Both make the run exit non-zero;
* the refusal is flag-wide, not per (directory x language): ``run_piranha``'s
  all-or-nothing rollback covers ONE invocation, so the loop below is what
  makes it cover the flag. Without that, a repository whose ``.ts`` rewrite
  was refused and whose ``.tsx`` rewrite succeeded would get a pull request
  claiming to remove the flag while half of it stayed behind;
* any reference to the flag key still in the tree AFTER the rewrite is both
  logged and written into the PR body, so a customer is never misled into
  thinking the flag was fully removed. The scan asks the question the customer
  is actually asking — "does this repository still mention the key?" — over
  every source extension the tool knows, not only the ones the configured
  languages leave out, because an extension being configured is not evidence
  that the engine had anything to say about a given occurrence of the key.

With ``config.dry_run`` set, that is all that happens: diffs are computed and
returned, and GitHub is never contacted. Otherwise each flag with a non-empty
diff also gets a branch, a commit and a pull request — **draft** when the flag
is merely ``Stale``, ready for review when it is ``Dead``. Three properties
that path is built around:

* **Idempotent.** ``github_ops.already_handled`` runs BEFORE Piranha, so a
  flag already proposed costs nothing and can never get a second PR.
* **One flag per PR.** Branch/commit/push happen while that flag's edits (and
  only that flag's) are on disk, inside the same window that the unconditional
  reset closes.
* **Per-flag failure isolation.** Anything that goes wrong for one flag —
  a rejected push, a 403 from the API, an unrepresentable key — is logged,
  recorded on that flag's :class:`FlagResult`, and the run continues with the
  next candidate, leaving the checkout on its original ref with no stray
  branch. The one deliberate exception is a failure to restore that ref, which
  aborts the run: continuing would build every later PR on top of this flag's
  commit.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

import httpx

from flag_cleanup import git_ops, github_ops
from flag_cleanup.client import Candidate, FeatureflipApiError, FeatureflipClient
from flag_cleanup.config import Config
from flag_cleanup.github_ops import (
    GitHubApi,
    GitHubEnv,
    UnsafeFlagKeyError,
)
from flag_cleanup.piranha_runner import (
    KNOWN_EXTENSIONS,
    PiranhaTransformError,
    UnsafeRewriteError,
    UnsupportedFlagKeyError,
    candidate_files,
    ensure_supported_key,
    transform_flag,
    source_files,
)
from flag_cleanup.pr_content import (
    _GENERATED_DIRS,
    commit_message,
    is_dead,
    note_declined_entries,
    note_removed_entries,
    note_rewritten_build_output,
    pr_body,
    pr_title,
)

logger = logging.getLogger(__name__)

#: The two ways the transform can REFUSE rather than find nothing. Both are
#: caught in one place and both abandon the whole flag — see
#: :func:`piranha_transform`.
TransformRefusal = PiranhaTransformError | UnsafeRewriteError

# Mirrors piranha_runner._SKIP_DIRS: never scan a vendored dependency tree.
_SKIP_DIRS = frozenset({".git", "node_modules"})

_RECOGNIZED_STATUSES = frozenset({"dead", "stale"})

#: Attribute stamped onto an exception that aborts :func:`run`, carrying the
#: results of the candidates that DID complete. An abort is rare and always
#: serious, but the flags already proposed are real work whose outcome the
#: customer still needs — throwing it away turns one bad flag into a run with
#: no report at all.
PARTIAL_RESULTS_ATTR = "partial_results"

#: Attribute stamped onto an aborting exception carrying the :class:`FlagResult`
#: of the candidate that was *being processed* when it aborted. That candidate
#: never returns a result the normal way, so without this the flag the run
#: actually died on is the one flag missing from the report.
ABORTING_RESULT_ATTR = "aborting_result"

#: Attribute stamped onto any exception that escapes :func:`run` AFTER the
#: preflight passed — i.e. once candidates could have been modified.
#:
#: It exists because the same exception TYPE means opposite things on either
#: side of that line. :class:`~flag_cleanup.git_ops.PreflightError` raised by
#: :func:`preflight` means "the run never started"; raised mid-run by
#: ``git_ops.work_dir`` (a configured directory that vanished under us) it means
#: "some flags already have pull requests". Keying the exit code off the type
#: alone reported the second case as exit 2 — documented as *nothing was
#: modified* — and skipped the partial report, so a customer whose fourth flag
#: hit a vanished directory saw no per-flag lines and no record of the three
#: pull requests that had just been opened for them.
RUN_STARTED_ATTR = "run_started"


class _Client(Protocol):
    """Structural type for the ``client`` parameter of :func:`run`.

    Matches :class:`~flag_cleanup.client.FeatureflipClient` — a plain stub is
    enough in tests, no need to subclass or monkeypatch httpx.
    """

    def removal_candidates(
        self, org: str, project: str, staleness: str
    ) -> Iterable[Candidate]: ...

    def flag_keys(self, org: str, project: str) -> Iterable[str]: ...


@dataclass(frozen=True, slots=True)
class FlagResult:
    """The outcome of processing one removal candidate.

    ``action_taken`` is one of:

    * ``"ignored"`` — the key was in ``config.ignore``; Piranha never ran.
    * ``"not-selected"`` — ``config.flags`` names an allowlist and this key
      was not on it; Piranha never ran.
    * ``"piranha-error"`` — the engine aborted (its own syntax self-check);
      ``diff`` is empty, but this is a REFUSAL, not a no-match.
    * ``"unsafe-rewrite"`` — Gate 1 refused the rewrite of at least one file
      and it was rolled back, taking the whole flag's transform with it;
      ``refused_files`` names the file(s). Also a refusal, and the one most
      likely to repeat on every future run until a human changes that file.
    * ``"dry-run"`` — Piranha ran to completion; ``diff`` may still be empty
      if nothing matched.
    * ``"already-handled"`` — the branch or a prior PR (open **or** closed)
      already exists on the remote; nothing was run, nothing was opened.
    * ``"duplicate"`` — the same key appeared earlier in THIS run and was
      already dealt with; nothing was run, nothing was opened.
    * ``"unsafe-key"`` — the key cannot be represented as a git branch name,
      or cannot be substituted into the rewrite rules safely.
    * ``"no-changes"`` — Piranha found nothing to rewrite; no branch, no PR.
    * ``"pr-opened"`` — a PR was opened; ``pr_url`` is set.
    * ``"failed"`` — this flag errored (push rejected, API refused, ...); the
      run continued with the next candidate.

    A dry run produces only ``dry-run``, ``ignored``, ``unsafe-key``,
    ``piranha-error`` and ``unsafe-rewrite`` — everything that needs GitHub is
    unreachable, but every refusal is reachable and is reported identically in
    both modes. That symmetry is the point of dry-run: a flag a real run would
    refuse must be visible in the dry run that preceded it.

    ``diff`` is what the transform COMPUTED, not a claim that it survived:
    ``failed`` carries the diff it would have proposed, because the failure
    (a rejected push, a refused API call) happened after the rewrite and
    knowing what was computed is diagnostic. That is the opposite rule to
    ``unprocessed_files`` below, and the difference is the point — a diff
    describes work, while ``unprocessed_files`` describes the repository, and
    only the second one can be a lie once the transform is rolled back.

    ``unprocessed_files`` lists files that still mention the key once the
    rewrite is on disk — an uncovered extension, a file the engine could not
    parse, or (most often) a mention no rule can match, such as a registry
    entry or a test stub. It is on the result (not merely logged) because it is
    the one caveat that changes what the diff *means*: a PR that removes a flag
    from ``.ts`` while a registry still declares it is not the "flag is gone"
    the title claims, so it is surfaced in the PR body too. Populated only for
    the actions where the transform actually ran and a claim was made
    (``dry-run``, ``no-changes``, ``pr-opened``).
    """

    key: str
    treatment: bool
    status: str
    diff: str
    action_taken: str
    pr_url: str = ""
    unprocessed_files: tuple[str, ...] = ()
    #: Files whose rewrite Gate 1 refused (``unsafe-rewrite`` only). On the
    #: result rather than only in a log line for the same reason
    #: ``unprocessed_files`` is: it is the one thing the customer can act on,
    #: and it decides whether this flag will ever be proposable.
    refused_files: tuple[str, ...] = ()


def run(
    config: Config,
    client: _Client | None = None,
    github: GitHubEnv | None = None,
    gh: GitHubApi | None = None,
) -> list[FlagResult]:
    """Fetch removal candidates and, unless dry-running, open a PR per flag.

    ``client`` defaults to a real :class:`FeatureflipClient` built from
    ``config``; ``github``/``gh`` default to the runner's ``GITHUB_*``
    environment and an authenticated :class:`GitHubApi`. All three are
    injectable so tests never touch the network.

    With ``config.dry_run`` set, ``github``/``gh`` are ignored entirely — not
    one request is made — and every result is a ``"dry-run"``.

    Raises :class:`~flag_cleanup.git_ops.PreflightError` before touching the
    network or the filesystem if any configured directory is missing, is not
    inside a git working tree, or has uncommitted changes, and
    :class:`~flag_cleanup.github_ops.BaseRefError` if the checkout is not a
    safe place to cut removal branches from. Both fire before any candidate is
    processed, so nothing has been modified when they do.
    """
    preflight(config)

    owns_client = client is None
    resolved_client: _Client = (
        client if client is not None else FeatureflipClient(config.api_url, config.api_token)
    )

    resolved_github: GitHubEnv | None = None
    resolved_gh: GitHubApi | None = None
    owns_gh = False
    if not config.dry_run:
        # Resolved before the first API call so a missing GITHUB_TOKEN fails
        # immediately rather than after a page of candidates has been fetched.
        resolved_github = github if github is not None else GitHubEnv.from_env()
        owns_gh = gh is None
        resolved_gh = (
            gh
            if gh is not None
            else GitHubApi(resolved_github.token, api_url=resolved_github.api_url)
        )

    # Accumulated (not built as a comprehension) so an abort mid-run — a
    # failed reset on candidate 3 — does not also destroy the record of what
    # candidates 1 and 2 did. See PARTIAL_RESULTS_ATTR.
    results: list[FlagResult] = []
    # Flipped once every read-only setup step has succeeded and candidate
    # processing is about to begin — NOT on entry to the `try`. The marker means
    # "a candidate may have been modified", and `_resolve_base` runs before any
    # of them: it only issues GETs (the default branch, then the base..HEAD
    # comparison). Stamping it on everything inside the block made
    # `BaseRefError` unreachable in `__main__`'s exit-2 branch even though it is
    # listed there, so the `pull_request`-trigger misconfiguration that
    # `ensure_head_on_base` exists to catch exited 1 with a traceback instead of
    # the documented exit 2 and one-liner (README "Which branch PRs target").
    started = False
    # Run-scoped, because the per-flag ownership pass structurally cannot be the
    # last word: flag N+1's `reset_worktree` rewrites flag N's files. See
    # `git_ops.OwnershipLedger`.
    ledger = git_ops.OwnershipLedger()
    try:
        known_flag_keys = fetch_known_flag_keys(resolved_client, config)
        if config.dry_run:
            process = partial(
                _process_candidate,
                config=config,
                ledger=ledger,
                known_flag_keys=known_flag_keys,
            )
        else:
            # One flag key can legitimately appear twice in one paginated fetch
            # (a cursor re-read, a backend listing it under two reasons).
            # Without this the second copy's `already_handled` would have to
            # see the branch the first copy pushed *seconds* earlier, making
            # correctness depend on the remote's read-after-write timing.
            process = partial(
                _process_candidate_for_pr,
                config=config,
                gh=resolved_gh,
                github=resolved_github,
                base=_resolve_base(config, resolved_gh, resolved_github),
                seen_branches=set(),
                ledger=ledger,
                known_flag_keys=known_flag_keys,
            )
        started = True
        proposed = 0
        for candidate in resolved_client.removal_candidates(
            config.org, config.project, config.staleness
        ):
            result = process(candidate)
            results.append(result)
            proposed += 1 if _is_proposal(result) else 0
            if config.max_prs and proposed >= config.max_prs:
                # Stop cleanly rather than propose the rest. This is a
                # blast-radius limit, not an error: correctness arguments bound
                # what a bug can do to ONE pull request, and nothing bounded how
                # many of them a single run could open against a customer's
                # repository. Counted on proposals, so ignored/no-changes/
                # already-handled candidates cost nothing against it.
                logger.warning(
                    "stopping after %d proposal(s): the max-prs limit. Remaining "
                    "candidates were not attempted and will be proposed on the "
                    "next run — raise `max-prs` (or set it to 0 for no limit) to "
                    "do more in one go",
                    proposed,
                )
                break
    except BaseException as exc:
        # Stamped BEFORE the results check inside `_attach_partial_results`:
        # "candidate processing began" is true even when no candidate completed,
        # and that is precisely the case (abort on candidate 1) where the
        # distinction is easiest to get wrong.
        if started:
            try:
                setattr(exc, RUN_STARTED_ATTR, True)
            except AttributeError:  # pragma: no cover - types with __slots__
                pass
        _attach_partial_results(exc, results)
        raise
    finally:
        # Once per run, not per flag: nothing between candidates reads `.git`
        # as the customer's user, and this walks the whole git directory.
        # Deliberately last, and in the `finally`, because an aborted run is
        # exactly when a half-written `.git` is left behind.
        #
        # The two passes are disjoint — the ledger holds source files it
        # captured, this one walks `.git` — so their order is free. Both are
        # here rather than per-flag, and for the same reason: only the end of
        # the run is after the last thing that can take a file back.
        ledger.restore()
        git_ops.restore_repository_ownership(config.directories)
        if owns_client:
            resolved_client.close()  # type: ignore[union-attr]
        if owns_gh and resolved_gh is not None:
            resolved_gh.close()
    return results


def _is_proposal(result: FlagResult) -> bool:
    """Did this candidate cost one of the run's proposals?

    A pull request in a real run; in a dry run, a diff that a real run WOULD
    have turned into one. Counting them identically is what keeps `max-prs`
    inside dry-run's promise: a dry run that surveyed fifty flags while the
    real run would stop at ten is a preview of a run that never happens.

    Everything else is free. A flag that is ignored, already handled, or has
    nothing to change did not use up any of the customer's review capacity,
    and a refusal did not either — capping those would only delay the report
    of a problem.
    """
    if result.action_taken == "pr-opened":
        return True
    return result.action_taken == "dry-run" and bool(result.diff)


def fetch_known_flag_keys(client: _Client, config: Config) -> frozenset[str]:
    """The project's flag list, for the entry rules' sibling evidence.

    Public (no leading underscore): ``pr_command`` regenerates a flag's
    removal through :func:`piranha_transform` and has to hand it the SAME
    evidence set the run that opened the pull request handed it, or the
    regenerated removal quietly under-removes. Calling ``client.flag_keys``
    from there instead would duplicate the degradation policy below, and a
    second copy is free to drift into aborting a command on an API hiccup.

    Once per run, never per candidate. A failure DEGRADES rather than aborts,
    because this is a tidiness improvement layered on the primary job and a run
    that removed no flag because a second endpoint hiccupped would be a
    regression on that job. With the empty set the registry prong
    matches nothing, the stub prong is unaffected, and the residual caveat
    still names the registry file — nothing is silently lost.
    """
    try:
        keys = frozenset(client.flag_keys(config.org, config.project))
    except (FeatureflipApiError, httpx.HTTPError) as exc:
        logger.warning(
            "could not fetch the project's flag list (%s); flag-keyed registry "
            "entries will be reported under \"still reference this flag\" "
            "rather than removed this run",
            exc,
        )
        return frozenset()
    logger.info(
        "fetched %d flag key(s) for %s/%s; an entry keyed by a removed flag is "
        "deleted when another key beside it is one of these",
        len(keys), config.org, config.project,
    )
    return keys


def preflight(config: Config) -> None:
    """Validate every configured directory BEFORE anything can be written.

    Ordered first in :func:`run` on purpose. Every check describes a condition
    under which this tool cannot safely operate, and all of them are cheap;
    finding out mid-run instead means Piranha has already rewritten source
    files that the failing reset then cannot restore.

    Public, and called from :mod:`flag_cleanup.pr_command` as well, because
    ``pr-command`` mode transforms, commits and FORCE-PUSHES the same working
    tree from a second entry point — one shared function is what stops the two
    entry points disagreeing about what "safe to operate in" means, and means a
    check added here covers both the day it is added.
    """
    try:
        for directory in config.directories:
            git_ops.ensure_work_tree(directory)
            git_ops.ensure_clean(directory)
        git_ops.ensure_one_repository(config.directories)
    except BaseException:
        # A refused preflight can still have written to `.git`: `ensure_clean`
        # runs `git status`, which opportunistically rewrites a stale index.
        # That particular file is harmless left as root's (see
        # `restore_repository_ownership` on why), so this is here to keep the
        # property unconditional — "this tool never leaves paths the customer
        # cannot write" — rather than true only for the preflights that happen
        # not to write today. Inside the function rather than around its call
        # site so both callers get it; `pr_command` reaches this before its own
        # `finally` exists.
        git_ops.restore_repository_ownership(config.directories)
        raise


def _resolve_base(config: Config, gh: GitHubApi, github: GitHubEnv) -> str:
    """Settle which branch every pull request targets, and check we can cut from HEAD.

    Two failures this closes, both of which used to be guaranteed on a first
    run for a large class of repositories:

    * an unset ``base-branch`` meant ``main``, so every pull request in a
      ``master`` repository was rejected;
    * removal branches are cut from HEAD, which is only sound while HEAD is
      contained in the base branch (see
      :func:`~flag_cleanup.github_ops.ensure_head_on_base`).

    Both are answered once per run, not once per flag: the answer cannot change
    mid-run, and N copies of the same failure is not a better report than one.
    """
    base = config.base_branch.strip()
    if not base:
        base = github_ops.default_branch(gh, github.repository)
    github_ops.ensure_head_on_base(
        gh,
        github.repository,
        base,
        git_ops.head_sha(git_ops.work_dir(config.directories[0])),
    )
    return base


def _attach_partial_results(exc: BaseException, results: list[FlagResult]) -> None:
    """Record the completed results on the aborting exception, and log them.

    Includes the candidate the run died on, if it managed to say what happened
    to it (see :data:`ABORTING_RESULT_ATTR`) — that flag is the most
    interesting line in the report, and it is the one that would otherwise be
    missing.
    """
    aborting = getattr(exc, ABORTING_RESULT_ATTR, None)
    if aborting is not None:
        results = [*results, aborting]
    if not results:
        return
    logger.error(
        "run aborted after %d candidate(s): %s",
        len(results),
        ", ".join(f"{r.key}={r.action_taken}" for r in results),
    )
    try:
        setattr(exc, PARTIAL_RESULTS_ATTR, list(results))
    except AttributeError:  # pragma: no cover - exception types with __slots__
        pass


def _process_candidate(
    candidate: Candidate,
    config: Config,
    ledger: git_ops.OwnershipLedger,
    known_flag_keys: frozenset[str] = frozenset(),
) -> FlagResult:
    """Dry run: compute the diff, touch nothing else."""
    if candidate.key in config.ignore:
        logger.info("skipping %s: listed in the ignore config", candidate.key)
        return FlagResult(candidate.key, candidate.treatment, candidate.status, "", "ignored")

    if config.flags and candidate.key not in config.flags:
        # After `ignore`, so a key in both lists is reported as ignored: deny
        # beats allow. Free against `max_prs` — `_is_proposal` does not count
        # it — so an allowlisted sweep is not silently truncated by the flags
        # it skipped on the way.
        logger.info(
            "skipping %s: the flags allowlist does not name it", candidate.key
        )
        return FlagResult(
            candidate.key, candidate.treatment, candidate.status, "", "not-selected"
        )

    _warn_on_unrecognized_status(candidate)

    # Asked here too, in the same order as the PR path, even though a dry run
    # never uses the branch name: `removal_branch` is the ONLY check that
    # refuses a key for being unrepresentable as a git ref (blank once
    # stripped, or over 240 characters escaped), and skipping it let a dry run
    # print a clean `[dry-run]` plus the diff it would propose for a flag the
    # very next real run refuses outright. Pure string work — no request is
    # made, so `run`'s "not one request in dry-run mode" still holds.
    _branch, unsafe_key = _branch_or_refusal(candidate)
    if unsafe_key is not None:
        return unsafe_key

    unsupported = _unsupported_key_result(candidate)
    if unsupported is not None:
        return unsupported

    unprocessed: tuple[str, ...] = ()
    with piranha_transform(candidate, config, ledger, known_flag_keys) as (
        diff,
        transform_error,
        _snapshot,
        unprocessable,
        _stranded,
        _bindings,
        entries,
        declined,
    ):
        # Inside the `with`, and for the same reason the PR path computes it
        # there: `_unprocessed_references` reads the REWRITTEN tree, and the
        # context manager rolls those edits back on exit. Asked one line later
        # it would answer about the original files and report every call site
        # this run had just cleaned — a dry run whose whole promise is to
        # preview a real run would have contradicted the real run's own body.
        if transform_error is None:
            unprocessed = all_unprocessed(config, candidate.key, unprocessable)

    if transform_error is not None:
        return _refusal_result(candidate, transform_error)

    _warn_on_unprocessed_references(config, candidate.key, unprocessed)
    note_rewritten_build_output(diff)
    note_removed_entries(entries)
    note_declined_entries(declined)

    return FlagResult(
        candidate.key,
        candidate.treatment,
        candidate.status,
        diff,
        "dry-run",
        unprocessed_files=unprocessed,
    )


def _process_candidate_for_pr(
    candidate: Candidate,
    config: Config,
    gh: GitHubApi,
    github: GitHubEnv,
    base: str,
    seen_branches: set[str],
    ledger: git_ops.OwnershipLedger,
    known_flag_keys: frozenset[str] = frozenset(),
) -> FlagResult:
    """Compute the diff and, if there is one, open this flag's removal PR."""
    if candidate.key in config.ignore:
        logger.info("skipping %s: listed in the ignore config", candidate.key)
        return FlagResult(candidate.key, candidate.treatment, candidate.status, "", "ignored")

    if config.flags and candidate.key not in config.flags:
        # After `ignore`, so a key in both lists is reported as ignored: deny
        # beats allow. Free against `max_prs` — `_is_proposal` does not count
        # it — so an allowlisted sweep is not silently truncated by the flags
        # it skipped on the way.
        logger.info(
            "skipping %s: the flags allowlist does not name it", candidate.key
        )
        return FlagResult(
            candidate.key, candidate.treatment, candidate.status, "", "not-selected"
        )

    _warn_on_unrecognized_status(candidate)

    branch, unsafe_key = _branch_or_refusal(candidate)
    if unsafe_key is not None:
        return unsafe_key

    unsupported = _unsupported_key_result(candidate)
    if unsupported is not None:
        return unsupported

    if branch in seen_branches:
        logger.info(
            "skipping %s: this run already handled %s", candidate.key, branch
        )
        return FlagResult(
            candidate.key, candidate.treatment, candidate.status, "", "duplicate"
        )
    seen_branches.add(branch)

    # Not `config.directories[0]` raw: a configured entry may name a single
    # file, and a file cannot be a subprocess cwd.
    repo_dir = git_ops.work_dir(config.directories[0])
    diff = ""
    pr_url = ""
    action = "failed"
    original_ref = ""
    created_branch = False
    pushed = False
    pr_created = False
    unprocessed: tuple[str, ...] = ()
    # Pre-bound beside `unprocessed` and for its reason: both are read after
    # the `with` below, and a path that never enters it must not turn a
    # reportable failure into a NameError on the way out.
    declined: tuple[tuple[str, str], ...] = ()
    refused: tuple[str, ...] = ()
    snapshot: git_ops.WorktreeSnapshot | None = None
    try:
        # Asked first: an already-proposed flag must cost nothing and must
        # never reach the point where a second PR could be opened.
        if github_ops.already_handled(gh, github.repository, branch):
            return FlagResult(
                candidate.key, candidate.treatment, candidate.status, "", "already-handled"
            )

        original_ref = git_ops.current_ref(repo_dir)
        with piranha_transform(candidate, config, ledger, known_flag_keys) as (
            diff,
            transform_error,
            snapshot,
            unprocessable,
            stranded,
            bindings,
            entries,
            declined,
        ):
            # Inside the `with`, this flag's edits — and only this flag's —
            # are on disk. That is the only window in which the commit can be
            # made, and it closes with the unconditional reset on exit.
            if transform_error is not None:
                action = _refusal_action(transform_error)
                refused = _refused_files(transform_error)
            elif not diff:
                logger.info("no changes for %s: opening no PR", candidate.key)
                action = "no-changes"
                unprocessed = all_unprocessed(config, candidate.key, unprocessable)
            else:
                # Computed BEFORE the PR is opened so the body can carry the
                # caveat. A PR titled "remove flag X" whose repo still reads X
                # from a .js file must say so where the reviewer will see it.
                unprocessed = all_unprocessed(config, candidate.key, unprocessable)
                git_ops.create_branch(repo_dir, branch)
                created_branch = True
                git_ops.commit_changes(
                    repo_dir, config.directories, commit_message(candidate)
                )
                git_ops.push_branch(repo_dir, branch, github.push_url, github.token)
                pushed = True
                pr_url = github_ops.open_pr(
                    gh,
                    github.repository,
                    branch=branch,
                    base=base,
                    title=pr_title(candidate),
                    body=pr_body(
                        candidate,
                        config,
                        unprocessed,
                        stranded,
                        bindings,
                        note_rewritten_build_output(diff),
                        entries=entries,
                        declined=declined,
                    ),
                    labels=config.pr_labels,
                    draft=not is_dead(candidate.status),
                )
                # Tracked separately from `pr_url`: `open_pr` raises only when
                # no PR was created, but it can legitimately return an empty
                # URL for one that was. Deleting the branch in that case would
                # close the pull request we just opened.
                pr_created = True
                action = "pr-opened"
    except github_ops.GitHubAuthError as exc:
        # Not a bad flag: this token cannot do this for ANY flag, so failing
        # each one in turn would produce N identical errors (and, before the
        # cleanup below, N orphan branches). Abort with the one real message.
        logger.error(
            "aborting: GitHub refused this token while processing %s. Check the "
            "workflow's permissions (contents: write, pull-requests: write) and, "
            "for pull-request creation, the repository setting 'Allow GitHub "
            "Actions to create and approve pull requests'",
            candidate.key,
        )
        _record_aborting_flag(exc, candidate, "failed")
        raise
    except (
        subprocess.CalledProcessError,
        git_ops.WorktreeResetError,
        git_ops.PreflightError,
    ) as exc:
        # The only failures raised as these are from the transform's undo:
        # `git checkout` exiting non-zero, the snapshot proving a file is still
        # not back to its pre-transform bytes, or a configured directory having
        # gone missing under us (which stops the reset before it starts).
        # Either way this flag's edits may still be on disk, so continuing
        # would contaminate the next flag's diff — and its commit. Abort the
        # whole run instead; this is not a "bad flag".
        logger.error("aborting: could not reset the worktree after %s", candidate.key)
        _record_aborting_flag(exc, candidate, action, pr_url)
        raise
    except Exception as exc:  # noqa: BLE001 - one bad flag must not end the run
        # An expected refusal (GitHub said no, git said no) is one clear line.
        # Anything else is a bug in this tool and needs its stack.
        expected = isinstance(exc, (github_ops.GitHubApiError, git_ops.GitCommandError))
        logger.error(
            "failed to propose removal of %s: %s",
            candidate.key,
            exc,
            exc_info=not expected,
        )
        action = "failed"
    finally:
        if pushed and not pr_created:
            # The push landed but THIS run opened no pull request. Left alone,
            # that branch makes `already_handled` report this flag as handled on
            # EVERY future run — a silent, permanent wedge that only a human
            # deleting the branch can clear. Undo it here, before the local
            # cleanup, so the network operation happens while the checkout is
            # still exactly where the push came from.
            #
            # But "we did not open one" is not "there is none": an overlapping
            # run (the weekly `schedule` plus a `workflow_dispatch`, or a job
            # re-run) produces a byte-identical tree and commit, so its push
            # succeeds as a no-op and only its POST fails, with GitHub's 422
            # "A pull request already exists". Deleting a pull request's head
            # branch CLOSES it — so this used to destroy the PR the other run
            # had just opened, and `already_handled` counts closed PRs, so the
            # flag was then retired forever. Ask before deleting.
            existing = _existing_pull_request(gh, github.repository, branch)
            if existing is None:
                _discard_pushed_branch(repo_dir, branch, github)
            elif existing.confirmed:
                logger.info(
                    "%s already has a pull request (%s) — keeping the branch "
                    "and reporting the flag as already handled",
                    branch,
                    existing.url or "URL not reported",
                )
                pr_url = existing.url
                action = "already-handled"
                # This run computed a diff and pushed it, but the pull request
                # is the OTHER run's — so reporting a removal diff here says
                # "this is what I did" about work this run did not do, and
                # `--dry-run` would print it under a flag it also calls
                # already handled. `FlagResult` documents `already-handled` as
                # "nothing was run, nothing was opened"; keep that true. The
                # `else` below already drops `unprocessed` for the same reason.
                diff = ""
            else:
                # Could not tell. Keeping a branch that has no pull request
                # wedges one flag until a human deletes it; deleting one that
                # does closes a pull request. Keep, and stay loud: `action`
                # remains whatever the failure set, so the build is still red.
                logger.error(
                    "pushed %s but could not open a pull request for it, and "
                    "could not check whether one already exists — leaving the "
                    "branch in place rather than risk closing a pull request. "
                    "If %s has no pull request, delete it by hand or this flag "
                    "will be reported as already handled on every future run",
                    branch,
                    branch,
                )
        if created_branch:
            # The transform's undo has already put the pre-transform bytes
            # back, which leaves the tree dirty *relative to the removal-branch
            # commit* — so this checkout would be refused without clearing it
            # first. Clearing is safe: it discards only bytes this tool wrote,
            # and switching to `original_ref` immediately restores the very
            # same content the snapshot holds.
            # Every raising step below is wrapped so the outcome reached so far
            # — including a pull request that WAS opened — still reaches the
            # report. These run in a `finally`, so nothing above has recorded
            # this candidate: `pr_url` is a local and the `return` at the end of
            # the function is never reached once one of them raises. Without
            # this, a run that opened a PR and then failed to restore the ref
            # reported the abort with no line for the flag and no URL, which is
            # exactly what ABORTING_RESULT_ATTR exists to prevent.
            try:
                for directory in config.directories:
                    git_ops.reset_worktree(directory)
                # A failure here is NOT swallowed: leaving the repo on our
                # branch would put this flag's commit into every later flag's
                # PR. It is only annotated on the way past.
                git_ops.restore_ref(repo_dir, original_ref)
            except BaseException as exc:
                logger.error(
                    "aborting: could not restore the checkout after %s", candidate.key
                )
                _record_aborting_flag(exc, candidate, action, pr_url)
                raise
            try:
                git_ops.delete_branch(repo_dir, branch)
            except Exception as exc:  # noqa: BLE001 - cosmetic cleanup only
                logger.warning("could not delete local branch %s: %s", branch, exc)
            if snapshot is not None:
                # Re-assert AFTER the ref switch, because that is the last
                # thing to touch the tree. The verification the next candidate
                # depends on has to be the final word, not an earlier one.
                #
                # Ownership for the same reason, and it is not redundant with
                # the pass inside `_undo_transform`: `restore_ref` is another
                # `git checkout`, so running as root it replaces these files
                # again and re-breaks the owner that pass had just put back.
                # Non-fatal by design — see `restore_ownership`.
                snapshot.restore_ownership()
                try:
                    snapshot.verify()
                except git_ops.WorktreeResetError as exc:
                    logger.error(
                        "aborting: the worktree could not be proved clean "
                        "after %s",
                        candidate.key,
                    )
                    _record_aborting_flag(exc, candidate, action, pr_url)
                    raise

    if action in {"pr-opened", "no-changes"}:
        _warn_on_unprocessed_references(config, candidate.key, unprocessed)
        # Logged on BOTH outcomes, not only alongside a pull request, because
        # `no-changes` is the one where the body cannot say it: a repository
        # whose only mention of the flag is an override pinning it to the
        # branch being removed produces an empty diff, opens nothing, and would
        # otherwise report a bare "no changes" about a decision the tool made
        # deliberately. Duplicating the body's paragraph on `pr-opened` is the
        # cheaper half of that trade, and matches `unprocessed` above.
        note_declined_entries(declined)
    else:
        # `unprocessed` is computed BEFORE the branch/commit/push (the PR body
        # needs it), so a flag that fails after that point still holds a list
        # describing a transform that has since been rolled back. Reporting it
        # printed "[failed] my-flag" followed by "warning: 3 unprocessed
        # reference(s) remain" — a caveat about an incomplete removal for a
        # flag that was not removed at all, pointing at files the customer
        # cannot act on. `FlagResult` documents the field as populated only
        # where the transform ran AND a claim was made; this keeps that true.
        unprocessed = ()

    return FlagResult(
        candidate.key,
        candidate.treatment,
        candidate.status,
        diff,
        action,
        pr_url,
        unprocessed_files=unprocessed,
        refused_files=refused,
    )


def _branch_or_refusal(candidate: Candidate) -> tuple[str, FlagResult | None]:
    """The removal branch for ``candidate``, or the refusal that replaces it.

    One helper rather than an inline ``try`` in the PR path, because the dry
    run has to ask the identical question in the identical order. It used to
    live only where the branch was needed, which made the branch-name rules
    (blank once stripped, or too long once escaped) the one refusal a dry run
    could not see — and a refusal invisible in dry-run mode is the one the
    customer meets for the first time on a real run, as a red build with no
    pull request.
    """
    try:
        return github_ops.removal_branch(candidate.key), None
    except UnsafeFlagKeyError as exc:
        logger.error(
            "skipping %r: cannot build a safe branch name for it (%s)", candidate.key, exc
        )
        return "", FlagResult(
            candidate.key, candidate.treatment, candidate.status, "", "unsafe-key"
        )


def _unsupported_key_result(candidate: Candidate) -> FlagResult | None:
    """Refuse, loudly, a key that cannot be substituted into the rules.

    Asked in BOTH modes and before the transform, so a dry run reports exactly
    what a real run would refuse. Reported as ``unsafe-key`` — the same outcome
    as a key that cannot be a branch name, because it is the same fact from the
    customer's side: this flag can never be proposed until it is renamed.
    """
    try:
        ensure_supported_key(candidate.key)
    except UnsupportedFlagKeyError as exc:
        logger.error("skipping %s", exc)
        return FlagResult(
            candidate.key, candidate.treatment, candidate.status, "", "unsafe-key"
        )
    return None


def _refusal_action(error: TransformRefusal) -> str:
    """Which ``action_taken`` a refused transform reports.

    Never ``no-changes``: a refusal and a no-match are opposite facts and the
    customer needs to tell them apart. That distinction is the whole reason
    ``piranha-error`` exists, and ``unsafe-rewrite`` is its Gate 1 twin.
    """
    return "unsafe-rewrite" if isinstance(error, UnsafeRewriteError) else "piranha-error"


def _refused_files(error: TransformRefusal) -> tuple[str, ...]:
    return error.refused if isinstance(error, UnsafeRewriteError) else ()


def _refusal_result(candidate: Candidate, error: TransformRefusal) -> FlagResult:
    return FlagResult(
        candidate.key,
        candidate.treatment,
        candidate.status,
        "",
        _refusal_action(error),
        refused_files=_refused_files(error),
    )


def _record_aborting_flag(
    exc: BaseException, candidate: Candidate, action: str, pr_url: str = ""
) -> None:
    """Stamp this candidate's outcome onto the exception aborting the run.

    ``action`` is the outcome reached so far, NOT a hardcoded ``"failed"``: a
    worktree-undo failure happens *after* the body ran, so the flag may well
    have had its pull request opened successfully. Reporting that one as failed
    would hide a PR the customer now has.
    """
    try:
        setattr(
            exc,
            ABORTING_RESULT_ATTR,
            FlagResult(
                candidate.key, candidate.treatment, candidate.status, "", action, pr_url
            ),
        )
    except AttributeError:  # pragma: no cover - exception types with __slots__
        pass


@dataclass(frozen=True, slots=True)
class _ExistingPullRequest:
    """What a post-failure lookup could establish about a pushed branch.

    ``confirmed`` is the only thing that may downgrade the flag to
    ``already-handled``; a lookup that failed answers ``confirmed=False``,
    which keeps the branch without claiming anything about it. ``url`` can be
    empty even when confirmed — GitHub does not always name it.
    """

    confirmed: bool
    url: str = ""


def _existing_pull_request(
    gh: GitHubApi, repo: str, branch: str
) -> _ExistingPullRequest | None:
    """Is there already a pull request from ``branch``? ``None`` = provably not.

    ``None`` is the ONLY answer that permits deleting the pushed branch, which
    is why the lookup failing is not folded into it: deleting the head branch
    of a pull request closes that pull request and (via ``already_handled``,
    which counts closed ones) retires the flag permanently, while keeping a
    branch that has none costs one flag until a human deletes it. The two
    mistakes are not the same size, so an unanswerable lookup takes the
    smaller one.
    """
    try:
        url = github_ops.existing_pull_request(gh, repo, branch)
    except Exception as exc:  # noqa: BLE001 - the caller cannot act on the type
        logger.warning(
            "could not check whether %s already has a pull request: %s", branch, exc
        )
        return _ExistingPullRequest(confirmed=False)
    if url is None:
        return None
    return _ExistingPullRequest(confirmed=True, url=url)


def _discard_pushed_branch(repo_dir: str, branch: str, github: GitHubEnv) -> None:
    """Delete a pushed branch whose pull request never opened. Never raises.

    Runs inside a ``finally`` that may already be propagating a failure, so it
    cannot be allowed to replace it. But it also cannot fail quietly: a branch
    left on the remote is read as "already handled" forever, so a delete that
    itself fails is reported at ERROR naming exactly what a human must remove.
    """
    try:
        git_ops.delete_remote_branch(repo_dir, branch, github.push_url, github.token)
    except Exception as exc:  # noqa: BLE001 - reported, never raised from here
        logger.error(
            "pushed %s but could not open a pull request for it, AND could not "
            "delete the branch afterwards (%s). Delete %s in %s by hand — until "
            "it is gone, this flag will be reported as already handled on every "
            "future run and no pull request will be opened for it",
            branch,
            exc,
            branch,
            github.repository,
        )


@contextmanager
def piranha_transform(
    candidate: Candidate,
    config: Config,
    ledger: git_ops.OwnershipLedger,
    known_flag_keys: frozenset[str] = frozenset(),
) -> Iterator[
    tuple[
        str,
        TransformRefusal | None,
        git_ops.WorktreeSnapshot,
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, str, str], ...],
        tuple[tuple[str, str], ...],
    ]
]:
    """Run Piranha for one candidate; yield the outcome; always undo.

    Public (no leading underscore): ``pr_command`` regenerates a flag's
    removal through this exact context manager, so a re-run produces the same
    diff the original run did rather than a second implementation that could
    drift from it.

    Yields ``(diff, error, snapshot, unprocessable, stranded, bindings,
    entries)`` — ``unprocessable`` names files the ENGINE could not process,
    ``stranded`` the unreachable statements the fold deleted in the languages
    where leaving them standing breaks the build (Java, Dart, the TypeScript
    family — see :func:`~flag_cleanup.syntax._unreachable_dialect`),
    ``bindings`` the imports/local variables the fold left stranded, and
    ``entries`` the flag-keyed registry/override/type-member entries deleted
    under the ``rules/<base>_entries.toml`` prong, and ``declined`` the ones
    those same prongs LEFT because their value is the branch being removed
    (#3050) — see :class:`~flag_cleanup.piranha_runner.TransformOutcome`.

    The body of the ``with`` runs while the edits are still on disk — that is
    what lets the PR path commit them. On exit the tree is restored no matter
    how the body finished, and the restoration is *proved* rather than assumed
    (see :func:`_undo_transform`).

    The snapshot is taken BEFORE the first ``run_piranha`` call and covers
    every file the engine could rewrite for this candidate, across all
    configured directories and languages. It is yielded so the PR path can
    re-assert it after the branch cleanup, which is the last thing to touch
    the tree.
    """
    plan = _transform_plan(candidate, config)
    snapshot = git_ops.WorktreeSnapshot.take(_transform_targets(plan))
    # Registered here, BEFORE the engine runs and regardless of how this
    # candidate ends: a refused rewrite has still written to these files before
    # its rollback, and the container run left leaked files behind exactly such
    # a refusal. Recording at snapshot time is also what makes the ledger's
    # first-value-wins rule meaningful — this is the owner from before the run.
    ledger.record(snapshot.ownership)
    diff_parts: list[str] = []
    unprocessable: list[str] = []
    stranded: list[tuple[str, str]] = []
    bindings: list[tuple[str, str, str]] = []
    entries: list[tuple[str, str]] = []
    declined: list[tuple[str, str]] = []
    transform_error: TransformRefusal | None = None
    try:
        try:
            for directory, language, files in plan:
                if not files:
                    continue
                outcome = transform_flag(
                    directory,
                    language,
                    candidate.key,
                    candidate.treatment,
                    paths=files,
                    accessors=config.accessors,
                    known_flag_keys=known_flag_keys,
                )
                if outcome.diff:
                    diff_parts.append(outcome.diff)
                # Files the engine could not process at all. They still hold a
                # live read, so they belong in the same caveat an unsupported
                # extension earns — see `TransformOutcome.unprocessable`.
                unprocessable.extend(outcome.unprocessable)
                stranded.extend(outcome.stranded)
                bindings.extend(outcome.bindings)
                entries.extend(outcome.entries)
                declined.extend(outcome.declined)
        except (PiranhaTransformError, UnsafeRewriteError) as exc:
            # Both are refusals, and both abandon the flag ENTIRELY — note the
            # try wraps the whole directory x language loop, so a refusal in
            # the `.ts` pass is not followed by a `.tsx` pass whose diff would
            # then be proposed on its own. That matters: `run_piranha`'s
            # all-or-nothing rollback is scoped to ONE invocation, so without
            # this the tool would open a pull request titled "removes flag X"
            # carrying only the half of the removal that happened to succeed,
            # with no mention of the half it refused.
            transform_error = exc
            if isinstance(exc, UnsafeRewriteError):
                logger.error(
                    "abandoning the removal of %s: %s. No pull request will be "
                    "opened for this flag, and none will be on any future run "
                    "until those file(s) change",
                    candidate.key,
                    exc,
                )
            else:
                logger.error("Piranha aborted while removing %s: %s", candidate.key, exc)
        # A refused transform yields NO diff, even if an earlier directory or
        # language produced one: that diff is a partial removal nobody will
        # ever be shown, and printing it in a dry run would advertise a change
        # this tool has just decided not to make.
        yield (
            ("" if transform_error is not None else "".join(diff_parts)),
            transform_error,
            snapshot,
            # Dropped alongside the diff when the flag was refused: nothing
            # was removed, so there is no incomplete removal to caveat — and
            # nothing was deleted, so there is no deletion to disclose.
            () if transform_error is not None else tuple(unprocessable),
            () if transform_error is not None else tuple(stranded),
            () if transform_error is not None else tuple(bindings),
            # Dropped with the rest on refusal: nothing was deleted, so there
            # is nothing to disclose.
            () if transform_error is not None else tuple(entries),
            # And dropped for the OPPOSITE reason, which is worth keeping
            # straight: this list is what the rules DECLINED to delete. On a
            # refusal the whole removal is abandoned and the file is untouched
            # either way, so naming a preserved entry would explain a decision
            # inside a pull request that was never opened.
            () if transform_error is not None else tuple(declined),
        )
    except BaseException as body_error:
        # try/except/else rather than `finally`: the undo needs to know whether
        # a failure is already in flight so it can chain onto it instead of
        # replacing it. `finally` cannot see that without sys.exc_info(), which
        # also picks up an unrelated except-block further up the stack.
        _undo_transform(config, snapshot, pending=body_error)
        raise
    else:
        _undo_transform(config, snapshot, pending=None)


def _transform_plan(
    candidate: Candidate, config: Config
) -> list[tuple[str, str, list[str]]]:
    """``(directory, language, files)`` for every pass this candidate will make.

    Computed ONCE and then handed to both the snapshot and each
    ``run_piranha`` call. Deriving it twice — once here, once inside the engine
    wrapper — walked and byte-read the entire tree a second time per directory
    per language per candidate, so a 50-candidate run over a large checkout
    paid for a hundred full traversals it already had the answer to.

    A later pass therefore sees the file list as it was BEFORE an earlier
    pass rewrote anything. That is a superset (a rewrite can only remove
    mentions of the key, never add them), which is the safe direction: it is
    the same superset the snapshot covers, and handing the engine a file that
    no longer mentions the key matches no rule.
    """
    return [
        (
            directory,
            language,
            _committable(directory, candidate_files(directory, language, candidate.key)),
        )
        for directory in config.directories
        for language in config.languages
    ]


def _committable(directory: str, files: list[str]) -> list[str]:
    """``files`` narrowed to the ones a commit could actually carry.

    ``git_ops.commit_changes`` stages with ``--update`` — tracked files only —
    so an untracked, gitignored or submodule-owned file can be rewritten, appear
    in the reported diff, and still be absent from the pull request. That made
    ``--dry-run`` preview more than a real run delivers, which defeats the
    purpose of previewing at all.

    Filtering here rather than in ``piranha_runner`` keeps the engine wrapper
    free of any git knowledge and puts the decision next to the commit whose
    behaviour defines it. It also shrinks the snapshot, since the plan is what
    ``_transform_targets`` is built from.

    A directory git cannot answer for is left ALONE rather than emptied: the
    preflight has already established every configured directory is inside a
    work tree, so a failure here is something unexpected, and treating it as
    "nothing is committable" would silently turn every flag into ``no-changes``.
    """
    if not files:
        return files
    try:
        tracked = git_ops.tracked_files(directory)
    except (git_ops.GitCommandError, git_ops.PreflightError, OSError) as exc:
        logger.warning(
            "could not list tracked files under %s (%s); transforming every "
            "candidate file rather than assuming none are committable",
            directory,
            exc,
        )
        return files
    keep = [path for path in files if os.path.abspath(path) in tracked]
    for path in files:
        if os.path.abspath(path) not in tracked:
            logger.info(
                "skipping %s: git does not track it, so a removal commit "
                "could not include it",
                path,
            )
    return keep


def _transform_targets(plan: list[tuple[str, str, list[str]]]) -> list[str]:
    """Every file ``run_piranha`` could rewrite for this candidate.

    A superset of what the engine actually writes: ``run_piranha`` is passed
    exactly these paths as ``paths_to_codebase``, so nothing outside them can
    be modified, which is what makes a snapshot of it a complete undo.
    """
    return [path for _, _, files in plan for path in files]


def _undo_transform(
    config: Config,
    snapshot: git_ops.WorktreeSnapshot,
    *,
    pending: BaseException | None,
) -> None:
    """Undo everything Piranha wrote, and prove it — unconditionally.

    Runs on success, on no-match, after a caught ``PiranhaTransformError`` and
    after an exception from the ``with`` body: don't rely on today's "Piranha
    validates before writing" guarantee holding under an OOM mid-run.

    Two passes, because neither alone is sufficient:

    1. ``git checkout -- .`` per directory — restores tracked files, and also
       covers the (theoretical) case of a write outside the snapshot.
    2. the snapshot — restores what git did not (an untracked or gitignored
       source file, for which ``git checkout`` exits 0 having done nothing) and
       then re-reads every file to confirm it matches, raising
       :class:`~flag_cleanup.git_ops.WorktreeResetError` if not.

    Ordering matters: git runs first so the snapshot always has the last word.

    ``pending`` is the failure already propagating, if any. A failed undo still
    wins — an unreset tree is more dangerous than any single flag's error — but
    it is raised ``from`` the original and the original is logged, so the real
    cause is never replaced by "the reset failed".
    """
    try:
        for directory in config.directories:
            git_ops.reset_worktree(directory)
        snapshot.restore_and_verify()
    except BaseException as undo_error:
        if pending is None:
            raise
        logger.error(
            "could not undo the transform, and a failure was already in "
            "flight. The undo failure is raised because an unreset tree is "
            "the more dangerous of the two, but the ORIGINAL failure was: %r",
            pending,
        )
        raise undo_error from pending


def _warn_on_unrecognized_status(candidate: Candidate) -> None:
    """Log (not raise) when ``status`` is neither ``Dead`` nor ``Stale``.

    The contract says the API only ever returns those two, but a future
    backend change could add a third value. Nothing downstream crashes on it:
    :func:`is_dead` returns ``False`` for anything unrecognized, so the flag
    gets the safer treatment (a **draft** PR, like ``Stale``) — which is what
    the warning below promises, and is covered by
    ``test_run_treats_an_unrecognized_status_as_the_safer_draft_case``.
    ``candidate.status`` is NOT rewritten here; ``FlagResult.status`` and the
    PR body still carry the raw wire value.
    """
    if candidate.status.strip().lower() not in _RECOGNIZED_STATUSES:
        logger.warning(
            "flag %s has unrecognized status %r from the removal-candidates "
            "API (expected 'Dead' or 'Stale'); treating it as the safer "
            "'Stale' case downstream (its PR opens as a draft)",
            candidate.key,
            candidate.status,
        )


def _unprocessed_references(config: Config, flag_key: str) -> tuple[str, ...]:
    """Files that still mention ``flag_key`` once the rewrite is on disk.

    **Every known source extension, not only the unconfigured ones**, and the
    difference is the whole point of the scan. It used to derive the set from
    ``config.languages`` and look at what those languages do NOT cover, which
    encodes the premise "a configured language means the engine handled every
    occurrence in that file". That premise holds only for occurrences the rules
    can match, and every seed rule in ``rules/*.toml`` is anchored to a flag
    READ — a call whose first argument is the key as a literal. A key that
    appears anywhere else is invisible to them:

    * a registry or map entry (``'my-flag': { default: false },``), which is how
      a typed wrapper around the SDK usually declares its keys;
    * a test that stubs the flag by name (``renderWith({ 'my-flag': true })``),
      and the type annotation that goes with it;
    * a comment, or a string passed to something other than an accessor.

    None of those is a read, so the engine leaves them; all of them are things
    the reviewer has to delete by hand; and under the old rule none of them
    could be reported, because they live in ``.ts``/``.tsx`` files in a run
    configured for ``ts,tsx``. The caveat was silent exactly where the residue
    was, and a partial removal shipped as a clean one.

    **Read AFTER the transform, never before.** The question is about the
    rewritten tree, so a file the engine really did clean does not appear here
    and no rewritten call site is reported back at the customer. Both callers
    run it inside the ``piranha_transform`` window for that reason; running it
    once the context has rolled the edits back would name every file the run
    just fixed. A file the engine cleaned only PARTIALLY still mentions the key
    and is still reported, which is the case no before-and-subtract scheme
    could see.

    **It costs a tree walk it did not used to cost, and that is affordable
    because the tool already pays that price many times over.** With every
    language configured — the default — ``unprocessed_extensions`` returns the
    empty set, so the old scan returned immediately without walking anything at
    all. This one always walks. But ``piranha_runner._candidate_files`` already
    walks the same trees and reads every source file in them ONCE PER
    CONFIGURED LANGUAGE for each candidate, so this adds roughly a thirteenth
    to a cost that is already there, not a new kind of cost. Do not "optimise"
    it by scanning before the transform and subtracting the diff's files: that
    is the partial-rewrite blind spot described above.
    """
    # Deduplicated by resolved path, keeping the first spelling seen.
    # `directories: ., apps/web` is a natural monorepo config and the second is
    # inside the first, so without this one file is counted twice — and the PR
    # body says "2 file(s) still reference this flag" and lists it twice.
    seen: dict[str, str] = {}
    for directory in config.directories:
        for path in _files_matching(Path(directory), KNOWN_EXTENSIONS, flag_key):
            seen.setdefault(str(path.resolve()), str(path))
    return tuple(seen.values())


def all_unprocessed(
    config: Config, flag_key: str, engine_skipped: Iterable[str]
) -> tuple[str, ...]:
    """Every file still reading ``flag_key`` that this run did not process.

    Public (no leading underscore): ``pr_command`` regenerates through the
    same path and needs the identical caveat, computed the identical way.

    Two sources, one caveat. :func:`_unprocessed_references` finds files that
    still mention the key once the rewrite is on disk, whatever the reason;
    ``engine_skipped`` holds files the engine could not parse at all,
    quarantined by
    :func:`~flag_cleanup.piranha_runner._quarantine_and_retry` so one file newer
    than its bundled grammar no longer kills the whole flag.

    They are merged rather than reported separately because the customer's
    question is the same for both — "does this pull request really remove the
    flag?" — and the answer has to be one list. Deduplicated by resolved path,
    keeping the first spelling seen, for the reason the extension scan already
    does it: overlapping configured directories can otherwise name one file
    twice.
    """
    seen: dict[str, str] = {}
    for path in (*_unprocessed_references(config, flag_key), *engine_skipped):
        seen.setdefault(os.path.abspath(path), path)
    return tuple(seen.values())


def _warn_on_unprocessed_references(
    config: Config, flag_key: str, unprocessed: tuple[str, ...]
) -> None:
    if unprocessed:
        # Deliberately does NOT name a single cause. The list has three sources
        # — an extension no configured language covers, a file the engine could
        # not parse, and (the common one, and the one that used to be invisible)
        # a mention that is not a flag read for any rule to match. It used to
        # say "with unsupported extensions", which reads as nonsense against a
        # `.ts` file in a run configured for `ts`.
        logger.warning(
            "%d file(s) still reference flag %r after the rewrite: the mention "
            "is not a flag read the rules can match (a registry entry, a test "
            "stub, a comment), or the extension is outside the configured "
            "languages (%s), or the transform engine could not parse the file "
            "— see the messages above: %s",
            len(unprocessed),
            flag_key,
            ", ".join(config.languages),
            ", ".join(unprocessed),
        )


def _mentions_flag_key(text: str, flag_key: str) -> bool:
    """Whether ``text`` references ``flag_key`` as a whole token.

    A bare substring test is fine in ``piranha_runner._candidate_files``, where
    it is only a prefilter and the rules decide. Here the answer is rendered in
    the PR body as "N file(s) still reference this flag and were not processed",
    so a false hit is a scary, wrong caveat attached to a complete removal —
    and short keys make that the common case, not the rare one: `search` matches
    `searchParams`, `new-ui` matches `new-ui-shell`.

    Boundaries are non-word characters rather than `\\b`, because a flag key may
    legitimately start or end with `-` (`\\b` does not fire between a space and a
    hyphen, so `\\bnew-ui\\b` misbehaves at the leading edge). `[A-Za-z0-9_-]`
    matches `ensure_supported_key`'s allow-list, so a neighbouring character
    from that set means this is a LONGER key, not this one.
    """
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text) is not None


def _files_matching(root: Path, extensions: Iterable[str], flag_key: str) -> list[Path]:
    """Files under ``root`` with one of ``extensions`` whose text mentions the key.

    The *question* is deliberately not ``piranha_runner._candidate_files``'s:
    that one is keyed on a language and prefilters with a substring, while this
    one is for the extensions no language covers and must be exact (see
    :func:`_mentions_flag_key`). Only the enumeration is shared, and it is
    shared precisely so the two skip lists cannot drift apart again.
    """
    hits: list[Path] = []
    for path in source_files(root, extensions, _SKIP_DIRS | _GENERATED_DIRS):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _mentions_flag_key(text, flag_key):
            hits.append(path)
    return hits
