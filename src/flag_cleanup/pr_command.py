"""Re-run the transform for the flag whose removal pull request was commented on.

`remove` mode opens one pull request per dead flag and, by design, never opens
a second: `github_ops.already_handled` counts a CLOSED pull request too,
because re-proposing a removal a customer turned down is worse than proposing
nothing. That is right for a schedule, and it leaves no way for a human to say
"actually, do that one again" — which is what this mode is.

**Which flag, without an input.** `github_ops.removal_branch` is a documented
bijection, so the branch a removal pull request already carries encodes its
flag key losslessly, and `flag_key_from_branch` inverts it with an exact
re-encode check. The pull request the comment is on therefore names the flag:
there is nothing to type and no way to aim a re-run at a different one.
`archive.py` resolves its flag the same way.

**Why it regenerates from base.** Re-running the transform on the pull
request's own branch would need no force push at all — and produces nothing.
Seed rules are synthesized per flag from a call site naming the key as a
string literal (`rule_synthesis`), so on a branch where the previous run
already deleted those call sites nothing seeds and the diff comes back empty.
The regeneration has to start from a tree that still reads the flag, which is
exactly the checkout `actions/checkout` hands an `issue_comment` run: the base
branch. `ensure_head_on_base` therefore passes here unchanged — HEAD *is* the
base — and it is called with the PULL REQUEST's own base, not the configured
one, so a pull request targeting some other branch is refused rather than
regenerated against the wrong tree.

**What it never does.** It does not delete the branch. `orchestrate` deletes a
pushed branch when a run opened no pull request, to stop an orphan wedging the
flag forever — but here the branch already HAS a pull request, and deleting a
pull request's head branch closes it. Since `already_handled` counts closed
pull requests, that would retire the flag permanently: a failure strictly
worse than the one it was recovering from.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from flag_cleanup import git_ops, github_ops, orchestrate, pr_content
from flag_cleanup.client import Candidate, FeatureflipClient
from flag_cleanup.config import Config, STALENESS_VALUES
from flag_cleanup.github_ops import GitHubApi, GitHubEnv, flag_key_from_branch
from flag_cleanup.pr_event import PullRequestComment, commented_pull_request

logger = logging.getLogger(__name__)

#: The mention a command must start with. Not configurable: a customer who
#: could rename it would also have to keep every reply and every line of the
#: README in step with it, and there is nothing to gain.
COMMAND_PREFIX = "@featureflip-cleanup"

#: Regenerate, refusing if a human has committed to the branch.
COMMAND_RERUN = "rerun"
#: Regenerate over those commits anyway. The escape hatch is a different word
#: a human types on the pull request rather than a workflow input, because an
#: input is set once and then applies to every future run in silence.
COMMAND_RECREATE = "recreate"
COMMANDS = (COMMAND_RERUN, COMMAND_RECREATE)

#: Attribute stamped onto any exception that escapes a run which had ALREADY
#: replaced the pull request's branch.
#:
#: The force-push is not the last thing this mode does: the `PATCH` that
#: rewrites the title and body comes after it, so does the reply, and so does
#: the worktree restore in `_regenerate`'s `finally`. Every one of those can
#: fail with the branch already rewritten — a token without
#: `pull-requests: write`, a 422, a locked pull request, a refused undo — and
#: the failure reply's most actionable sentence is whether anything was
#: pushed. Without this marker that sentence read "Nothing was pushed" on all
#: of them, which is precisely backwards: the branch is gone and the pull
#: request's description still narrates the diff it used to hold.
FORCE_PUSHED_ATTR = "flag_cleanup_force_pushed"

#: Attribute stamped onto any exception that escapes a run whose `PATCH` had
#: ALSO landed — so the pull request's title and body already describe the new
#: diff and only the run's own tail failed.
#:
#: Needed because the marker above is BINARY while the outcome has three
#: states, and the third is the one that matters most. `snapshot.verify()`, the
#: `restore_ref`, `reset_worktree`, `ledger.restore()` and the reply POST all
#: come after the `PATCH`, so a run can reach the failure reply with the branch
#: replaced AND the pull request correctly updated. Reporting that as "the pull
#: request could not be updated, so its description still describes the
#: previous diff" is false twice over, and it points the reader at the pull
#: request when a refused undo means the thing to look at is their CHECKOUT —
#: which is exactly why `__main__` keeps a traceback for that failure rather
#: than summarising it.
PULL_REQUEST_UPDATED_ATTR = "flag_cleanup_pull_request_updated"


def branch_was_force_pushed(exc: BaseException) -> bool:
    """Whether ``exc`` escaped a run that had already replaced the branch.

    Read by ``__main__`` when it composes the failure reply. An attribute on
    the exception rather than a return value because the information has to
    survive the stack unwinding — which is the only way this fact ever reaches
    a caller.
    """
    return getattr(exc, FORCE_PUSHED_ATTR, False) is True


def pull_request_was_updated(exc: BaseException) -> bool:
    """Whether ``exc`` escaped a run whose ``PATCH`` had also landed.

    Only ever meaningful alongside :func:`branch_was_force_pushed` — the
    `PATCH` is unreachable before the push — so the reply reads them as a
    sequence rather than as two independent facts.
    """
    return getattr(exc, PULL_REQUEST_UPDATED_ATTR, False) is True


@dataclass(slots=True)
class _RemoteWrites:
    """How far the run got through the two writes it makes to the customer's
    repository, readable after the stack unwinds past them.

    Passed down rather than returned for the same reason as the attributes
    above: the paths that need it are the ones where nothing is returned at
    all. Owned by :func:`_run` so the marking happens in one place, which
    keeps the `try`/`finally` in :func:`_regenerate` — already the most
    delicate block in this module — exactly as it was.

    Each field is set immediately after its own call RETURNS, never before it
    is attempted: a field set ahead of the call it describes would report a
    write that had failed, which is the same class of lie the whole record
    exists to stop telling.
    """

    pushed: bool = False
    patched: bool = False


@dataclass(frozen=True, slots=True)
class PrCommandResult:
    """What one pr-command run did, for a single reportable line."""

    action_taken: str
    detail: str
    key: str | None = None
    pull_request: int | None = None
    diff: str = ""


def parse_command(body: str) -> str | None:
    """The command in ``body``, or ``None``.

    A command must be a WHOLE LINE — the mention, whitespace, one known verb,
    nothing else — matched case-insensitively. Prose that merely names it
    ("we should @featureflip-cleanup rerun this next week") does not fire it,
    and neither does a backticked mention, because a line beginning with a
    backtick does not begin with the mention.

    Deliberately strict about the trailing text: "@featureflip-cleanup rerun
    please" is a person talking, and guessing that they meant the command is
    the sort of helpfulness that force-pushes a branch nobody asked about.
    """
    for line in body.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not lowered.startswith(COMMAND_PREFIX):
            continue
        # Require a boundary character (or end of line) right after the
        # mention — without this, "@featureflip-cleanuprerun" (no space)
        # would slice at len(COMMAND_PREFIX) and parse as `rerun`.
        rest = stripped[len(COMMAND_PREFIX) :]
        if rest and not rest[0].isspace():
            continue
        verb = rest.strip().lower()
        if verb in COMMANDS:
            return verb
    return None


def run_pr_command(
    config: Config,
    env: Mapping[str, str] | None = None,
    *,
    gh: GitHubApi | None = None,
    github: GitHubEnv | None = None,
    client: object | None = None,
) -> PrCommandResult:
    """Act on the command in the comment that triggered this run.

    ``gh``/``github``/``client`` are injectable so tests never open a socket.

    Raises :class:`~flag_cleanup.pr_event.PullRequestEventError` when the
    workflow is wired wrongly, and the ``github_ops``/``git_ops`` errors when
    an operation is refused. Nothing here is caught and turned into a green
    run: a command a human typed and watched must not report success it did
    not have.
    """
    comment = commented_pull_request(env)
    if comment is None:
        return PrCommandResult(
            action_taken="not-a-pull-request",
            detail="the comment was left on an issue, so there is nothing to re-run",
        )

    command = parse_command(comment.body)
    if command is None:
        return PrCommandResult(
            action_taken="not-a-command",
            detail=f"the comment carries no `{COMMAND_PREFIX}` command",
            pull_request=comment.number,
        )

    resolved_github = github if github is not None else GitHubEnv.from_env(env)
    owns_gh = gh is None
    resolved_gh = (
        gh
        if gh is not None
        else GitHubApi(resolved_github.token, api_url=resolved_github.api_url)
    )
    try:
        return _run(config, comment, command, resolved_gh, resolved_github, client)
    finally:
        if owns_gh:
            resolved_gh.close()


def _run(
    config: Config,
    comment: PullRequestComment,
    command: str,
    gh: GitHubApi,
    github: GitHubEnv,
    client: object | None,
) -> PrCommandResult:
    repo = github.repository

    permission = github_ops.actor_permission(gh, repo, comment.commenter)
    if permission not in github_ops.WRITE_PERMISSIONS:
        # SILENT: no reply, exit 0. `issue_comment` fires for anyone who can
        # comment, which on a public repository is anyone at all — a reply
        # here would turn this into an amplifier for whoever types the mention.
        logger.info(
            "ignoring a %s command from %s: permission %r is not enough",
            command,
            comment.commenter,
            permission,
        )
        return PrCommandResult(
            action_taken="not-permitted",
            detail=(
                f"{comment.commenter} does not have write access, so the "
                f"{command} command was ignored"
            ),
            pull_request=comment.number,
        )

    pull = github_ops.pull_request(gh, repo, comment.number)

    key = flag_key_from_branch(pull.head_ref)
    if key is None:
        return _reply(
            gh,
            repo,
            comment.number,
            PrCommandResult(
                action_taken="not-a-removal-branch",
                detail=(
                    f"`{pull.head_ref}` was not opened by this Action, so there "
                    "is no flag to re-run"
                ),
                pull_request=comment.number,
            ),
        )

    if key in config.ignore:
        # `ignore` is documented as the way to silence a flag this Action
        # refuses to process — an allowlist (`flags`) must not resurrect it,
        # and neither may a comment command: that would let anyone with write
        # access reach back around a decision already made in the workflow's
        # own configuration. Checked before the merged/foreign-commit/
        # candidate checks below, mirroring `orchestrate._process_candidate*`,
        # where `ignore` is also the very first thing asked.
        return _reply(
            gh,
            repo,
            comment.number,
            PrCommandResult(
                action_taken="rerun-ignored",
                detail=(
                    f"`{key}` is in this workflow's `ignore` list, so the "
                    f"{command} command was refused. Remove it from `ignore` "
                    "first if you want this flag regenerated. Nothing was pushed"
                ),
                key=key,
                pull_request=comment.number,
            ),
        )

    if pull.merged:
        return _reply(
            gh,
            repo,
            comment.number,
            PrCommandResult(
                action_taken="rerun-merged",
                detail=(
                    f"this pull request already merged, so `{key}` is gone from "
                    "the base branch and there is nothing left to remove"
                ),
                key=key,
                pull_request=comment.number,
            ),
        )

    if command == COMMAND_RERUN:
        commits = github_ops.branch_commits(gh, repo, pull.base_ref, pull.head_ref)
        foreign = github_ops.foreign_commits(commits, git_ops.BOT_EMAIL)
        if foreign:
            # Nothing has been pushed at this point and nothing will be.
            named = ", ".join(f"`{sha[:12]}`" for sha in foreign)
            return _reply(
                gh,
                repo,
                comment.number,
                PrCommandResult(
                    action_taken="rerun-refused",
                    detail=(
                        f"{len(foreign)} commit(s) on `{pull.head_ref}` were not "
                        f"made by this Action ({named}), and a re-run replaces the "
                        f"branch. Nothing was pushed. Comment "
                        f"`{COMMAND_PREFIX} {COMMAND_RECREATE}` to regenerate "
                        f"anyway — those commits will be lost. Full SHAs: "
                        f"{', '.join(foreign)}"
                    ),
                    key=key,
                    pull_request=comment.number,
                ),
            )

    owns_client = client is None
    active = client if client is not None else FeatureflipClient(config.api_url, config.api_token)
    try:
        found = _find_candidate(active, config, key)
        # Read here, inside the client's lifetime, because the transform needs
        # it and the transform runs after this block closes. Skipped when the
        # flag is not a candidate: that run is about to refuse and has nothing
        # to spend a second request on.
        #
        # `orchestrate`'s own helper, not a second call to `client.flag_keys`:
        # a failure to fetch this list must DEGRADE (the flag-keyed entries are
        # reported instead of removed), never abort. A private copy of that
        # policy here would be free to drift into failing the command on an API
        # hiccup, and the regenerated removal would then differ from the one
        # that opened the pull request for a reason nobody could see.
        known_flag_keys = (
            orchestrate.fetch_known_flag_keys(active, config)
            if found is not None
            else frozenset()
        )
    finally:
        if owns_client:
            active.close()

    if found is None:
        return _reply(
            gh,
            repo,
            comment.number,
            PrCommandResult(
                action_taken="rerun-not-a-candidate",
                detail=(
                    f"`{key}` is not among the removal candidates for "
                    f"`{config.org}/{config.project}` in either tier "
                    f"({', '.join(STALENESS_VALUES)}). It may have been archived, "
                    "deleted, or brought back into use. Nothing was pushed"
                ),
                key=key,
                pull_request=comment.number,
            ),
        )

    candidate, tier = found
    written = _RemoteWrites()
    try:
        return _regenerate(
            config,
            comment,
            command,
            gh,
            github,
            pull,
            candidate,
            tier,
            known_flag_keys,
            written,
        )
    except BaseException as exc:
        # `BaseException`, because a cancelled workflow (Actions sends SIGINT)
        # between the push and the `PATCH` leaves exactly the same half-done
        # state a `GitHubApiError` does.
        if written.pushed:
            setattr(exc, FORCE_PUSHED_ATTR, True)
            # And the marker `__main__` reads to decide exit 2 vs exit 1, for
            # the reason `orchestrate` set it: exit 2 is DOCUMENTED as "nothing
            # was modified", and once the branch is replaced that is no longer
            # true. It matters for one narrow path and the path is real —
            # `reset_worktree` in `_regenerate`'s `finally` resolves each
            # configured directory through `git_ops.work_dir`, which raises
            # `PreflightError` if one vanished under the run. Without this, a
            # directory deleted mid-run reported the same exit code as a
            # checkout the preflight refused before touching anything.
            setattr(exc, orchestrate.RUN_STARTED_ATTR, True)
        if written.patched:
            setattr(exc, PULL_REQUEST_UPDATED_ATTR, True)
        raise


def _regenerate(
    config: Config,
    comment: PullRequestComment,
    command: str,
    gh: GitHubApi,
    github: GitHubEnv,
    pull: github_ops.PullRequest,
    candidate: Candidate,
    tier: str,
    known_flag_keys: frozenset[str],
    written: _RemoteWrites,
) -> PrCommandResult:
    """Rebuild the diff from base and put it on the branch.

    The checkout is the BASE branch — ``actions/checkout`` gives an
    ``issue_comment`` run the default branch, and the pull request's branch is
    never checked out, only pushed to. ``ensure_head_on_base`` is therefore
    satisfied by construction; it is called anyway, against the PULL REQUEST's
    base rather than the configured one, because a pull request targeting some
    other branch would otherwise be regenerated against a tree this run never
    saw.

    Everything that composes the pull request comes from the same functions
    the scheduled run composes it with, given the same arguments — the
    transform through :func:`~flag_cleanup.orchestrate.piranha_transform`, the
    caveat through :func:`~flag_cleanup.orchestrate.all_unprocessed`, the
    description through :func:`~flag_cleanup.pr_content.pr_body`. An argument
    dropped here is a field that silently disappears from the customer's pull
    request on a re-run, so this call site mirrors the one that opened it.
    """
    key = candidate.key
    repo = github.repository
    # The SAME preflight `orchestrate.run` opens with, and it is load-bearing
    # in a way it is not there. This mode commits every configured directory
    # and FORCE-PUSHES the result over a pull request's branch, then resets
    # the worktree in the `finally` below: without `ensure_clean`, an
    # uncommitted edit a customer happened to have in their checkout is
    # committed under a `chore: remove feature flag ...` message, published
    # over their pull request, and then destroyed locally by that reset.
    # `remove` mode is immune only because it refuses to start; nothing but
    # this call makes the same true here.
    #
    # The whole preflight rather than `ensure_clean` alone, for two more
    # reasons that bite on this path exactly as they do on that one:
    # `ensure_work_tree` turns a mistyped directory into a refusal instead of
    # a raw git error out of `head_sha` two lines down, and
    # `ensure_one_repository` covers the pathspec `commit_changes` builds from
    # every configured directory against the FIRST one's repository. Sharing
    # the function is also what stops the two entry points drifting apart
    # about what "safe to operate in" means.
    #
    # Placed at the top of the REGENERATION rather than at the top of the
    # mode, on purpose. Everything upstream of here is a read or a reply --
    # the permission check (whose silence for a non-writer is deliberate, and
    # which a raise ahead of it would turn into an amplifier), and the
    # `ignore`/merged/foreign-commit refusals, each of which has a better
    # answer to give than a git error. This is the last statement before the
    # transform, so it still refuses before anything is transformed,
    # committed or pushed.
    orchestrate.preflight(config)
    repo_dir = git_ops.work_dir(config.directories[0])
    # `base_is_configured_input=False`: `pull.base_ref` is THIS pull request's
    # own base, not the workflow's `base-branch` input, so a refusal here must
    # not tell the customer to change that input — see
    # `github_ops.ensure_head_on_base`'s docstring.
    github_ops.ensure_head_on_base(
        gh, repo, pull.base_ref, git_ops.head_sha(repo_dir), base_is_configured_input=False
    )

    ledger = git_ops.OwnershipLedger()
    original_ref = git_ops.current_ref(repo_dir)
    created_branch = False
    snapshot: git_ops.WorktreeSnapshot | None = None
    try:
        with orchestrate.piranha_transform(
            candidate, config, ledger, known_flag_keys
        ) as (
            diff,
            transform_error,
            snapshot,
            unprocessable,
            stranded,
            bindings,
            entries,
        ):
            if transform_error is not None:
                # Not turned into a reply: a refused transform is the one
                # outcome where the branch, the pull request and the customer's
                # code are all still exactly as they were, and the reason is a
                # multi-line engine or gate message that belongs in the job log
                # rather than compressed into a comment. Raising keeps the run
                # red, which is the module's contract for everything it cannot
                # complete.
                raise transform_error
            unprocessed = orchestrate.all_unprocessed(config, key, unprocessable)
            if not diff:
                # Loud, not silent: an open pull request is proposing a removal
                # the code no longer needs. Nothing is pushed and the pull
                # request is not touched — in particular the branch is left
                # alone, because deleting it would close the very pull request
                # this is telling a human to look at.
                return _reply(
                    gh,
                    repo,
                    comment.number,
                    PrCommandResult(
                        action_taken="rerun-empty",
                        detail=(
                            f"regenerating `{key}` from `{pull.base_ref}` produced "
                            "an empty diff — nothing in the configured directories "
                            "reads this flag any more. The branch and this pull "
                            "request are unchanged; it can be closed"
                        ),
                        key=key,
                        pull_request=comment.number,
                    ),
                )

            if config.dry_run:
                # No branch, no commit, no push, no PATCH: a dry run proves
                # the regeneration would succeed and stops there. `diff` was
                # already computed above by the same transform a real run
                # uses, so this reports the real regeneration's shape rather
                # than a separate, possibly-drifting preview of it.
                #
                # An input literally named `dry-run` being silently ignored on
                # the one destructive path in this tool is not a documentation
                # gap to paper over — a customer who carries `dry-run: true`
                # over from their `remove` workflow, which is a completely
                # reasonable habit, must not get a real force-push for it.
                commits = github_ops.branch_commits(gh, repo, pull.base_ref, pull.head_ref)
                draft_note = _draft_note(pull, candidate)
                caveats = _dry_run_caveats(unprocessed, stranded, bindings, entries)
                return _reply(
                    gh,
                    repo,
                    comment.number,
                    PrCommandResult(
                        action_taken="rerun-dry-run",
                        detail=(
                            f"regenerating `{key}` from `{pull.base_ref}` would "
                            f"force-push `{pull.head_ref}` ({command}), replacing "
                            f"{len(commits)} existing commit(s) on it. Nothing was "
                            "pushed and this pull request was not updated. The "
                            f"flag is in the `{tier}` tier."
                            + (f" {draft_note.strip()}" if draft_note else "")
                            + caveats
                        ),
                        key=key,
                        pull_request=comment.number,
                        diff=diff,
                    ),
                )

            git_ops.create_branch(repo_dir, pull.head_ref)
            created_branch = True
            git_ops.commit_changes(
                repo_dir, config.directories, pr_content.commit_message(candidate)
            )
            # Leased against the head sha this run READ from the pull request,
            # for both commands: `recreate` waives the ownership guard above,
            # never the lease. A write that landed on the branch between that
            # read and this push is rejected rather than silently lost.
            git_ops.force_push_branch(
                repo_dir, pull.head_ref, github.push_url, github.token, pull.head_sha
            )
            # From here on the customer's branch has been replaced, so every
            # later failure -- the `PATCH`, the reply, the undo in the
            # `finally` -- has to be reported as such rather than as "nothing
            # was pushed". Set immediately after the call and nowhere else: a
            # rejected lease raises out of it and leaves this `False`, which is
            # correct, because a rejected lease pushes nothing.
            written.pushed = True
            body = pr_content.pr_body(
                candidate,
                config,
                unprocessed,
                stranded,
                bindings,
                pr_content.note_rewritten_build_output(diff),
                entries=entries,
            )
            draft_note = _draft_note(pull, candidate)
            github_ops.update_pull_request(
                gh,
                repo,
                pull.number,
                title=pr_content.pr_title(candidate),
                body=body + draft_note,
                # The same call reopens a closed pull request; `None` leaves an
                # open one alone rather than sending a redundant field.
                state="open" if pull.state == "closed" else None,
            )
            # AFTER the `PATCH` returns, for the reason the record's docstring
            # gives: the reply, the ref restore, `snapshot.verify()` and
            # `ledger.restore()` are all still to come, and a failure in any of
            # them leaves the branch replaced and the pull request correctly
            # updated. Without this the reply told that person their pull
            # request still described the previous diff, which sends them to
            # read a body that is already right — and away from the checkout a
            # refused undo may have left holding this flag's edits.
            written.patched = True
            action = "rerun-reopened" if pull.state == "closed" else "rerun-updated"
            return _reply(
                gh,
                repo,
                comment.number,
                PrCommandResult(
                    action_taken=action,
                    detail=(
                        f"regenerated `{key}` from `{pull.base_ref}` and "
                        f"force-pushed `{pull.head_ref}` ({command}). "
                        f"The flag is in the `{tier}` tier."
                        + (f" {draft_note.strip()}" if draft_note else "")
                    ),
                    key=key,
                    pull_request=comment.number,
                    diff=diff,
                ),
            )
    finally:
        # Deliberately NOT `orchestrate`'s pushed-branch cleanup. That deletes
        # a branch whose run opened no pull request; this branch HAS one, and
        # deleting a pull request's head branch CLOSES it — which, because
        # `already_handled` counts closed pull requests, would retire the flag
        # permanently. A failed re-run leaves the remote branch exactly where
        # it is, on every path including this one.
        if created_branch:
            # The transform's undo has already put the pre-transform bytes
            # back, which leaves the tree dirty relative to the removal-branch
            # commit — so the checkout below would be refused without clearing
            # it first.
            #
            # `reset_worktree` restores tracked files from the index
            # unconditionally, so "this discards only bytes this tool wrote"
            # is NOT a property of this line. It is a property of the
            # `orchestrate.preflight` call at the top of this function, which
            # refuses the whole command unless every configured directory is
            # already free of uncommitted changes to tracked files. Delete
            # that call and this line quietly destroys the customer's own
            # work — after the commit above has already published it.
            for directory in config.directories:
                git_ops.reset_worktree(directory)
            # NOT swallowed: leaving the customer's checkout sitting on the
            # removal branch would put this commit into whatever the rest of
            # their job does with the working tree.
            git_ops.restore_ref(repo_dir, original_ref)
            try:
                git_ops.delete_branch(repo_dir, pull.head_ref)
            except Exception as exc:  # noqa: BLE001 - LOCAL cleanup only
                logger.warning(
                    "could not delete local branch %s: %s", pull.head_ref, exc
                )
        if snapshot is not None:
            # Re-asserted AFTER the ref switch, because that is the last thing
            # to touch the tree: `restore_ref` is another `git checkout`, so
            # running as root it replaces these files again and re-breaks the
            # owner the transform's own undo had just put back. The
            # verification is what proves the customer's checkout really is
            # back to the bytes it arrived with, rather than assuming it.
            snapshot.restore_ownership()
            snapshot.verify()
        ledger.restore()
        git_ops.restore_repository_ownership(config.directories)


def _dry_run_caveats(
    unprocessed: tuple[str, ...],
    stranded: tuple[tuple[str, str], ...],
    bindings: tuple[tuple[str, str, str], ...],
    entries: tuple[tuple[str, str], ...],
) -> str:
    """A short summary of what a real run's pull-request body would call out.

    A real run folds all four of these into `pr_content.pr_body` — the
    still-referenced-flag warning, the unreachable statements a language's own
    toolchain proves dead, the stranded imports/locals a fold left with no
    remaining use, and the flag-keyed registry/override/type-member entries a
    removal strands. A dry run computes every one of them (they come from the
    same `piranha_transform` call the real run uses) and, without this,
    reported NONE of them — a preview quieter than the run it is supposed to
    preview, which is backwards for a preview. Counts only, not the full
    lists: this is a short reply on a pull request, not the pull request body
    itself, and a customer who wants the detail gets it from a real (or a
    `remove`-mode dry) run over the same code.
    """
    notes: list[str] = []
    if unprocessed:
        notes.append(f"{len(unprocessed)} file(s) would still reference this flag")
    if stranded:
        notes.append(f"{len(stranded)} unreachable statement(s) would be deleted")
    if bindings:
        notes.append(f"{len(bindings)} stranded import(s)/local(s) would be cleaned up")
    if entries:
        noun = "entry" if len(entries) == 1 else "entries"
        notes.append(f"{len(entries)} flag-keyed {noun} would be removed")
    if not notes:
        return ""
    return " " + "; ".join(notes) + "."


def _draft_note(pull: github_ops.PullRequest, candidate: Candidate) -> str:
    """A line for the body when the draft state no longer matches the tier.

    ``PATCH /pulls/{n}`` cannot flip draft <-> ready — that needs GraphQL — so
    the mismatch is reported rather than fixed. Silently leaving a stale draft
    is the outcome worth avoiding; adding a GraphQL client for one boolean is
    not worth it.
    """
    should_be_ready = pr_content.is_dead(candidate.status)
    if pull.draft and should_be_ready:
        return (
            f"\n\n> This pull request is still a **draft**, but `{candidate.key}` "
            f"is now `{candidate.status}`. Mark it ready for review when you are "
            "satisfied with the diff.\n"
        )
    if not pull.draft and not should_be_ready:
        return (
            f"\n\n> This pull request is **ready for review**, but "
            f"`{candidate.key}` is now `{candidate.status}` — a suggestion "
            "rather than a certainty. Convert it to a draft if you would "
            "rather hold it.\n"
        )
    return ""


def _find_candidate(client, config: Config, key: str):
    """The candidate for ``key`` and the tier it was found in, or ``None``.

    Searches the CONFIGURED tier first, then the other. A flag genuinely moves
    from `stale` to `dead` over the life of a pull request, and searching only
    the configured one would make this command permanently fail for exactly
    the pull requests most likely to need it. The tier is returned because it
    decides draft-vs-ready — which a `PATCH` cannot change, so the reply says
    it instead.

    `client` has no per-flag GET, so this is a listing scan. It stops at the
    first match rather than draining the pages behind it.
    """
    tiers = [config.staleness, *(t for t in STALENESS_VALUES if t != config.staleness)]
    for tier in tiers:
        for candidate in client.removal_candidates(config.org, config.project, tier):
            if candidate.key == key:
                return candidate, tier
    return None


def _reply(gh, repo: str, number: int, result: PrCommandResult) -> PrCommandResult:
    """Post the outcome on the pull request and return it unchanged."""
    github_ops.comment_on_pull_request(gh, repo, number, _reply_body(result))
    return result


def _reply_body(result: PrCommandResult) -> str:
    return f"**flag-cleanup — `{result.action_taken}`**\n\n{result.detail}\n"
