"""Git operations against the CUSTOMER's checkout: reset, branch, commit, push.

``run_piranha`` has no dry-run mode of its own — it always rewrites
files under its ``repo_dir`` in place and hands back a diff computed from what
it wrote. The per-flag undo must therefore be **total and provable**, because a
reset that silently half-worked is how one flag's deletions end up in another
flag's pull request.

Two mechanisms, in that order, and the second is the one that is trusted:

* :func:`reset_worktree` — ``git checkout -- .``, the coarse net. It restores
  *tracked* files only, and (this is the trap) exits **0** while doing nothing
  for an untracked one as long as the pathspec matched at least one tracked
  file. A customer repo with any gitignored codegen output under a configured
  directory hits that case, so this cannot be the whole story.
* :class:`WorktreeSnapshot` — the byte-exact contents of every file the engine
  could possibly rewrite, captured *before* the transform. Restoring from it
  covers tracked and untracked files alike, and :meth:`WorktreeSnapshot.verify`
  then re-reads each one and raises :class:`WorktreeResetError` unless it is
  identical. Nothing here assumes a reset worked.

Both preflights — :func:`ensure_work_tree` and :func:`ensure_clean` — run for
every configured directory *before* the first transform, so the two ways this
tool could damage a repository (writing where it cannot undo, and undoing work
that was never ours) are ruled out while the tree is still untouched.

The PR-opening flow adds the write side. Four deliberate choices there, all
about blast radius inside somebody else's repository:

* **Identity is per-invocation, never global.** ``actions/checkout`` does not
  configure ``user.name``/``user.email``, so a bare ``git commit`` in a
  customer's CI dies with *"Please tell me who you are"*. The bot identity is
  passed as ``-c`` overrides on the one commit this tool makes rather than
  written into their config with ``git config --global``.
* **The token never touches ``.git/config`` or a command line.** The push
  remote is the plain, credential-free HTTPS URL; authentication rides in a
  request header supplied through ``GIT_CONFIG_*`` **environment** variables
  for that single subprocess. So it cannot persist into a later workflow step,
  cannot be scraped out of ``.git/config`` by an artifact upload, and is not
  visible in ``/proc/<pid>/cmdline`` the way a
  ``https://x-access-token:TOKEN@github.com/...`` argument would be. Every
  git subprocess's output is additionally redacted before it is logged or
  wrapped in an exception — a failed ``git push`` is exactly where a
  credential would otherwise surface.
* **Only tracked modifications are staged**, scoped to the configured
  directories (``git add --update``, with every pathspec made absolute — see
  :func:`commit_changes`). Piranha never creates files, so nothing untracked in
  the customer's tree (build output, caches, an unrelated scratch file) can
  ever be swept into our commit.
* **A push is undone when the pull request it was for never opened.** See
  :func:`delete_remote_branch`: a branch on the remote with no PR reads as
  "already handled" on every later run, so leaving one behind silently retires
  the flag forever.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Author/committer of the commits this tool makes. Not a real GitHub account:
#: the ``users.noreply.github.com`` domain guarantees it cannot be attributed
#: to (or bounce mail at) an actual person.
BOT_NAME = "featureflip-flag-cleanup[bot]"
BOT_EMAIL = "featureflip-flag-cleanup[bot]@users.noreply.github.com"

_REDACTED = "***"


class GitCommandError(RuntimeError):
    """A git subprocess failed. Its message is redacted before construction."""


class WorktreeResetError(RuntimeError):
    """The working tree could not be proved clean after a transform.

    Raised by :meth:`WorktreeSnapshot.verify` when a file the engine could have
    rewritten does not match its pre-transform bytes. Callers must treat this
    as fatal to the whole run, never as "this one flag failed": the next flag's
    diff — and its commit — would silently carry this flag's edits.
    """


class PreflightError(RuntimeError):
    """A configured directory cannot be worked on safely. Raised before any write.

    Deliberately raised up front rather than discovered mid-run: both causes
    (not a git work tree, uncommitted changes) are conditions under which this
    tool would either be unable to undo what it wrote or would destroy work
    that was never its own, and finding that out *after* Piranha has rewritten
    a file leaves the customer with a mutated tree and a traceback.
    """


def reset_worktree(directory: str) -> None:
    """Restore tracked files under ``directory`` to their **staged** state.

    ``git checkout -- .`` restores from the **index**, not from the last
    commit: a staged-but-uncommitted change survives it unchanged. That is only
    equivalent to "the committed state" because :func:`ensure_clean` has
    already established that the index matches HEAD for these paths.

    This is the coarse net, NOT the guarantee. Two limits make that so:

    * it restores **tracked** files only, and in a directory holding at least
      one tracked file it exits 0 having silently ignored every untracked one
      (a gitignored codegen output, say) that Piranha rewrote;
    * it restores from the index rather than from what was actually on disk
      when this run started.

    :class:`WorktreeSnapshot` closes both. Deliberately still not ``git
    clean``: that would be free to delete untracked files under ``directory``
    that have nothing to do with the transform.

    Raises ``subprocess.CalledProcessError`` if the checkout fails — this must
    NOT be swallowed, since a failed reset risks silently contaminating the
    next candidate's diff with this one's edits.

    The one tolerated failure is a directory git is not tracking ANY file in:
    ``git checkout -- .`` exits 1 there with ``pathspec '.' did not match any
    file(s) known to git``. That is not a failed reset, it is an empty one —
    there is nothing tracked to restore, and :class:`WorktreeSnapshot` covers
    the untracked files that *are* there. Treating it as fatal made a
    ``directories`` entry naming a generated or gitignored tree (``dist``, a
    codegen output, an empty directory) abort the whole run on the first
    candidate, forever, with a message about a failed reset — while both
    preflights passed, because ``ensure_work_tree`` only asks whether the path
    is inside a work tree and ``ensure_clean`` finds nothing dirty in it.
    """
    logger.debug("resetting worktree: %s", directory)
    cwd, pathspec = _cwd_and_pathspec(directory)
    try:
        subprocess.run(
            ["git", "checkout", "--", pathspec],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if not _tracks_any_file(cwd, pathspec):
            logger.debug(
                "nothing to reset under %s: git tracks no file there", directory
            )
            return
        logger.error(
            "failed to reset worktree %s (git checkout -- . exited %d): %s",
            directory,
            exc.returncode,
            exc.stderr,
        )
        raise


def _restore_owners(
    ownership: Mapping[str, tuple[int, int]],
) -> tuple[list[str], list[str]]:
    """Chown each path back to its recorded ``(uid, gid)``; return (done, failed).

    Shared by the per-flag pass (:meth:`WorktreeSnapshot.restore_ownership`) and
    the per-run one (:meth:`OwnershipLedger.restore`), which differ only in what
    they have recorded and what they say afterwards. A path whose owner already
    matches is skipped rather than chowned, so a caller can report exactly what
    it moved instead of claiming every path it holds.

    A chown that cannot be done is collected, never raised — both callers run
    where an exception would replace a real failure with a cosmetic one.
    """
    corrected: list[str] = []
    failed: list[str] = []
    for path, (uid, gid) in ownership.items():
        current = _owner_of(path)
        if current is None or current == (uid, gid):
            continue
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            logger.debug("could not chown %s: %s", path, exc)
            failed.append(path)
            continue
        corrected.append(path)
    return corrected, failed


@dataclass(slots=True)
class OwnershipLedger:
    """Every path this RUN wrote, with the owner it had before the run began.

    The per-flag pass in :meth:`WorktreeSnapshot.restore_ownership` is correct
    and still insufficient, because it cannot be the last word: the NEXT flag's
    :func:`reset_worktree` is a whole-worktree ``git checkout`` that rewrites
    files an earlier flag touched — their stat cache is stale, because root
    wrote them — and by then the earlier flag's snapshot is long finished.

    Measured in the container rather than reasoned about: a run over two flags
    left the first flag's three source files owned by ``root``, while either
    flag on its own left nothing behind. A ten-flag run left six, spread across
    several flags, two of them belonging to a flag whose rewrite was *refused* —
    a refusal still writes before it rolls back, which is why this records at
    snapshot time and not at diff time.

    Restoring from **recorded per-file values** is what keeps this narrow: it
    can only ever put back an owner it saw, on a path this run captured. It
    never walks the worktree, so a file the run did not touch — including one a
    previous workflow step legitimately created as root — is untouchable here.
    """

    original: dict[str, tuple[int, int]]

    def __init__(self) -> None:
        self.original = {}

    def record(self, ownership: Mapping[str, tuple[int, int]]) -> None:
        """Merge one snapshot's owners. The **first** value for a path wins.

        A file that reads two flags is snapshotted twice, and by the second
        snapshot the first flag's processing may already have left it
        root-owned. Taking the later value would restore that file *to* root —
        cementing the bug this exists to undo, while reporting success.
        """
        for path, owner in ownership.items():
            self.original.setdefault(path, owner)

    def restore(self) -> list[str]:
        """Re-assert every recorded owner that has since moved; return those paths.

        Called once, at the very end of the run, after the last operation that
        can touch the worktree. Non-fatal for the same reason as the per-flag
        pass: the bytes are correct, and this runs in the run's ``finally``,
        where raising would replace whatever really went wrong.
        """
        corrected, failed = _restore_owners(self.original)
        if corrected:
            logger.debug(
                "handed %d file(s) back to their pre-run owner at the end of "
                "the run: %s",
                len(corrected),
                ", ".join(corrected[:10]),
            )
        if failed:
            logger.warning(
                "could not restore the original owner of %d file(s): %s%s. "
                "Their contents are correct, so this run is unaffected, but "
                "later steps in this workflow may not be able to write them",
                len(failed),
                ", ".join(failed[:10]),
                "..." if len(failed) > 10 else "",
            )
        return corrected


@dataclass(frozen=True, slots=True)
class WorktreeSnapshot:
    """Byte-exact contents of every file a transform could rewrite.

    This is what makes the per-flag undo *provable* rather than assumed.
    ``run_piranha`` hands the engine an explicit ``paths_to_codebase`` list, so
    :func:`~flag_cleanup.piranha_runner.candidate_files` is a sound superset of
    what can be written; capturing those bytes first turns the undo into a
    comparison rather than a hope.

    Content-addressed on purpose. ``git status --porcelain`` cannot serve as
    the check: a gitignored file shows up in neither the before nor the after
    listing, and a merely-untracked one shows as ``??`` in both — so a status
    diff reports "nothing changed" for exactly the files ``git checkout``
    failed to restore.

    ``ownership`` is captured alongside the bytes because the published image
    is a **Docker action running as root** against a checkout the runner owns
    as another uid. See :meth:`restore_ownership`.
    """

    contents: Mapping[str, bytes]
    ownership: Mapping[str, tuple[int, int]]

    @classmethod
    def take(cls, paths: Iterable[str]) -> WorktreeSnapshot:
        """Read every path in ``paths`` (deduplicated, absolute) into memory.

        Unreadable paths are skipped: a file that cannot be read now could not
        have been read by the engine either, so there is nothing to restore.

        Ownership is recorded for the files whose bytes were captured, and only
        those: the undo may only put back what it can prove it took.
        """
        captured: dict[str, bytes] = {}
        owners: dict[str, tuple[int, int]] = {}
        for path in paths:
            resolved = os.path.abspath(path)
            if resolved in captured:
                continue
            content = _read_bytes(resolved)
            if content is not None:
                captured[resolved] = content
                owner = _owner_of(resolved)
                if owner is not None:
                    owners[resolved] = owner
        return cls(captured, owners)

    def restore_ownership(self) -> list[str]:
        """Put back the uid/gid of every snapshotted file whose owner changed.

        A second pass, and it has to be: the published image is a **Docker
        action, so it runs as root** against a checkout the runner owns as
        another uid, and ``git checkout`` REPLACES a modified file rather than
        rewriting it in place. The file comes back with the right bytes and
        root's ownership, after which the customer's own later workflow steps
        cannot write it. Measured in the container, not inferred: a tracked
        file went ``1000:1000`` -> ``0:0`` while an untracked one — which
        :meth:`restore` rewrites in place, preserving the owner — did not.

        :meth:`restore` cannot cover this. git puts the BYTES back correctly,
        so the content pass sees no difference and skips the file; the only
        thing wrong with it is who owns it.

        Runs after the LAST operation that can touch the tree, which on the
        pull-request path is ``restore_ref`` rather than ``reset_worktree`` —
        switching off the removal branch rewrites the same files again.

        A chown that cannot be done is logged and skipped, NOT raised. The
        bytes are correct, and the bytes are what the next flag's diff depends
        on; refusing the whole run over an ownership quirk would cost the
        customer every remaining pull request to fix something that corrupts
        nothing. (It is close to unreachable anyway: a process that is not root
        cannot have caused the change in the first place.)
        """
        corrected, failed = _restore_owners(self.ownership)
        if corrected:
            logger.debug(
                "put ownership back on %d file(s) after the reset: %s",
                len(corrected),
                ", ".join(corrected[:10]),
            )
        if failed:
            logger.warning(
                "could not restore the original owner of %d file(s): %s%s. "
                "Their contents are correct, so this run is unaffected, but "
                "later steps in this workflow may not be able to write them",
                len(failed),
                ", ".join(failed[:10]),
                "..." if len(failed) > 10 else "",
            )
        return corrected

    def restore(self) -> list[str]:
        """Rewrite every snapshotted file whose bytes differ; return those paths.

        A non-empty return is the signal that something upstream (i.e.
        ``git checkout -- .``) did not fully undo the transform — normal for an
        untracked file, and precisely the case that used to pass unnoticed.
        """
        restored: list[str] = []
        for path, original in self.contents.items():
            if _read_bytes(path) == original:
                continue
            with open(path, "wb") as handle:
                handle.write(original)
            restored.append(path)
        return restored

    def verify(self) -> None:
        """Raise :class:`WorktreeResetError` unless every file matches the snapshot.

        Re-reads from disk rather than trusting :meth:`restore`'s return value,
        so a write that silently failed (read-only file, full disk) is caught
        here instead of surfacing as a contaminated pull request.
        """
        differing = sorted(
            path for path, original in self.contents.items() if _read_bytes(path) != original
        )
        if differing:
            raise WorktreeResetError(
                f"{len(differing)} file(s) still differ from their pre-transform "
                f"contents after the reset: {', '.join(differing[:10])}"
                f"{'...' if len(differing) > 10 else ''}"
            )

    def restore_and_verify(self) -> None:
        """:meth:`restore`, put ownership back, then :meth:`verify`.

        The total, checked undo. Ownership goes between the two because
        :meth:`restore` can recreate a file that was deleted — which gives it
        the *current* process's owner, so it is one more thing to put back.
        """
        restored = self.restore()
        if restored:
            logger.debug(
                "snapshot restored %d file(s) that git's reset did not: %s",
                len(restored),
                ", ".join(restored[:10]),
            )
        self.restore_ownership()
        self.verify()


def ensure_work_tree(directory: str) -> None:
    """Preflight: ``directory`` exists and is inside a git working tree.

    Runs for every configured directory before the first transform. Without it
    a typo'd or non-git path is discovered only when the *reset* fails — after
    Piranha has already rewritten the customer's files, leaving a mutated tree
    plus a raw ``fatal: not a git repository`` traceback.
    """
    cwd = work_dir(directory)
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise PreflightError(
            f"configured directory {directory!r} is not inside a git working "
            "tree, so nothing written there could be reset or committed "
            "(check FEATUREFLIP_DIRECTORIES)"
        )


def ensure_clean(directory: str) -> None:
    """Preflight: ``directory`` has no uncommitted changes to tracked files.

    ``reset_worktree`` restores tracked files from the index unconditionally,
    so a pre-existing uncommitted edit anywhere under a configured directory is
    destroyed by the first flag processed. Refusing to start is the safe half
    of that trade: a CI checkout is always clean, so this costs a real run
    nothing, while a local invocation against a working repo is stopped before
    it eats somebody's afternoon.

    Untracked files (``??``) are NOT a reason to refuse — ``git checkout``
    cannot touch them, and :class:`WorktreeSnapshot` restores the ones the
    transform does rewrite.
    """
    dirty = dirty_tracked_paths(directory)
    if dirty:
        shown = ", ".join(dirty[:10]) + ("..." if len(dirty) > 10 else "")
        raise PreflightError(
            f"configured directory {directory!r} has uncommitted changes to "
            f"{len(dirty)} tracked file(s): {shown}. This tool resets the "
            "working tree between flags, which would destroy them — commit or "
            "stash first."
        )


def ensure_one_repository(directories: Sequence[str]) -> None:
    """Preflight: every configured directory belongs to the SAME repository.

    ``ensure_work_tree`` only asks whether each path is inside *a* work tree,
    which is equally true of a submodule or a second checkout. But the commit is
    made in ``directories[0]``'s repository with every configured path passed as
    an absolute pathspec, and git rejects a pathspec outside its repository — so
    a mixed set fails EVERY flag, on every run, with an opaque
    ``fatal: … is outside repository`` after Piranha has already rewritten files
    in both trees.

    Checked up front for the same reason as the other two preflights: this is a
    configuration mistake, and the only useful moment to report one is before
    anything has been written.
    """
    toplevels: dict[str, str] = {}
    for directory in directories:
        cwd = work_dir(directory)
        try:
            top = _git(["rev-parse", "--show-toplevel"], cwd=cwd).strip()
        except GitCommandError as exc:  # pragma: no cover - ensure_work_tree ran first
            raise PreflightError(
                f"could not determine the repository for {directory!r}: {exc}"
            ) from exc
        toplevels[directory] = os.path.realpath(top)

    distinct = sorted(set(toplevels.values()))
    if len(distinct) > 1:
        detail = ", ".join(f"{d!r} -> {toplevels[d]}" for d in directories)
        raise PreflightError(
            "every configured directory must belong to the same git repository, "
            f"but {len(distinct)} were found ({detail}). This tool commits in the "
            "first one, and git refuses a pathspec outside its own repository — "
            "so a submodule or a second checkout would fail every flag. Run the "
            "Action once per repository instead (check FEATUREFLIP_DIRECTORIES)"
        )


def dirty_tracked_paths(directory: str) -> list[str]:
    """Tracked paths under ``directory`` with staged or unstaged changes.

    Untracked (``??``) entries are excluded; ignored entries never appear at
    all without ``--ignored``. Paths come back relative to the repository root,
    which is what ``--porcelain`` always reports.

    Scoped to exactly what was configured: when the entry names a single file,
    only that file is examined, not everything beside it.
    """
    cwd, pathspec = _cwd_and_pathspec(directory)
    output = _git(["status", "--porcelain", "--", pathspec], cwd=cwd)
    return [
        line[3:].strip()
        for line in output.splitlines()
        if line.strip() and not line.startswith("??")
    ]


def work_dir(directory: str) -> str:
    """The directory to run git in, raising :class:`PreflightError` if absent.

    A configured entry may point at a single file (``run_piranha`` accepts one,
    and the preflight allows it), in which case git runs in its parent. Every
    caller that uses a configured entry as a subprocess ``cwd`` must go through
    here: handing ``subprocess.run`` a *file* as ``cwd`` raises
    ``NotADirectoryError``, which is neither a git error nor a per-flag one.
    """
    if os.path.isdir(directory):
        return directory
    if os.path.isfile(directory):
        return os.path.dirname(os.path.abspath(directory))
    raise PreflightError(
        f"configured directory {directory!r} does not exist "
        "(check FEATUREFLIP_DIRECTORIES)"
    )


def _cwd_and_pathspec(entry: str) -> tuple[str, str]:
    """Split a configured entry into (git's cwd, the pathspec to scope to).

    A directory entry keeps the existing shape: run inside it, scope to ``.``.
    A *file* entry runs in its parent and scopes to the file itself — running
    in the file is impossible (``NotADirectoryError``), and widening to the
    parent would silently operate on every sibling.
    """
    cwd = work_dir(entry)
    if os.path.isdir(entry):
        return cwd, "."
    return cwd, os.path.abspath(entry)


def _tracks_any_file(cwd: str, pathspec: str) -> bool:
    """Whether git tracks at least one file under ``pathspec``.

    Used only to tell :func:`reset_worktree`'s two failure modes apart: a
    checkout that could not restore a tracked file (fatal — this flag's edits
    may still be on disk) versus one that had nothing tracked to restore
    (benign). Deliberately narrow: on any doubt — ``git ls-files`` itself
    failing — it answers ``True``, which keeps the original error fatal.
    """
    result = subprocess.run(
        ["git", "ls-files", "--", pathspec], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def restore_repository_ownership(directories: Sequence[str]) -> list[str]:
    """Hand git's own metadata back to whoever owns the repository.

    The other half of the root-Docker-action problem. :class:`WorktreeSnapshot`
    covers *source* files; this covers everything git writes about them.
    Measured in the container, not inferred — after one pull-request run as
    root against a checkout owned by uid 1000, these were root's:

        .git/config   .git/index   .git/HEAD   .git/objects/xx (+ the objects)

    Only one of those actually hurts, and it is not the obvious one. The
    root-owned *files* are harmless: git rewrites ``config``, ``index`` and
    ``HEAD`` atomically — a ``.lock`` beside them, then ``rename()`` — which
    needs write permission on the containing directory and not on the file, and
    ``.git`` itself is still the customer's. Verified by running the customer's
    next step as uid 1000: ``git config``, ``git add`` and ``git commit`` all
    succeeded over root-owned files.

    The **directories** are the problem. Committing creates new
    ``.git/objects/xx/`` fanout directories, and root leaves those at ``0:0``
    mode 755 — so a later loose object whose sha begins with one of those two
    hex characters cannot be written at all, and the customer's build fails
    with a permission error. It is intermittent by construction (a handful of
    prefixes out of 256), which makes it the worse kind of failure to be handed
    rather than the milder one.

    The rule is narrow on purpose: **chown only what this process owns**, and
    only to the owner of the git directory itself. A file belonging to anyone
    else was not created here and reassigning it would be the same class of
    damage this exists to undo. Comparing the full ``(uid, gid)`` pair rather
    than the uid alone is also what makes the whole pass a no-op — no walk, no
    log — in the ordinary case where the tool runs as the repository's owner.

    Scoped to the git directory. The worktree is deliberately NOT walked: it
    can legitimately hold files owned by others, and the snapshot already
    restores the ones this tool touched, from recorded per-file values.

    A chown that cannot be done is logged and skipped, never raised — the same
    reasoning as :meth:`WorktreeSnapshot.restore_ownership`, and this runs in a
    ``finally`` where raising would replace a real failure with a cosmetic one.
    """
    corrected: list[str] = []
    failed: list[str] = []
    ours = os.geteuid()
    for git_dir in _git_dirs(directories):
        target = _owner_of(git_dir)
        if target is None or target == (ours, os.getegid()):
            continue
        for path in _walk(git_dir):
            current = _owner_of_link(path)
            if current is None or current == target or current[0] != ours:
                continue
            try:
                os.chown(path, target[0], target[1], follow_symlinks=False)
            except OSError as exc:
                logger.debug("could not chown %s: %s", path, exc)
                failed.append(path)
                continue
            corrected.append(path)
    if corrected:
        logger.debug(
            "handed %d git metadata file(s) back to the repository's owner",
            len(corrected),
        )
    if failed:
        logger.warning(
            "could not restore the original owner of %d path(s) under .git: "
            "%s%s. This run is unaffected, but a later `git add` in this "
            "workflow may fail if an object hashes into one of the object "
            "directories left behind",
            len(failed),
            ", ".join(failed[:10]),
            "..." if len(failed) > 10 else "",
        )
    return corrected


def _git_dirs(directories: Sequence[str]) -> list[str]:
    """Every distinct git metadata directory behind ``directories``.

    Both ``--git-dir`` and ``--git-common-dir``: in a linked worktree they
    differ, and the shared one is where the objects and config live. A path
    that is not a repository contributes nothing rather than raising — this
    runs in a ``finally``, including after a preflight that refused precisely
    because the path was not a work tree.
    """
    found: list[str] = []
    for directory in directories:
        try:
            cwd = work_dir(directory)
            output = _git(["rev-parse", "--git-dir", "--git-common-dir"], cwd=cwd)
        except (OSError, subprocess.SubprocessError, GitCommandError, PreflightError):
            continue
        for line in output.split("\n"):
            if not line.strip():
                continue
            resolved = os.path.abspath(os.path.join(cwd, line.strip()))
            if os.path.isdir(resolved) and resolved not in found:
                found.append(resolved)
    return found


def _walk(root: str) -> Iterator[str]:
    """``root`` and everything under it, without following symlinked directories."""
    yield root
    for parent, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            yield os.path.join(parent, name)


def _owner_of_link(path: str) -> tuple[int, int] | None:
    """:func:`_owner_of` that does not follow a symlink."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_uid, info.st_gid


def _owner_of(path: str) -> tuple[int, int] | None:
    """``(uid, gid)`` of ``path``, or ``None`` if it cannot be stat-ed.

    ``None`` rather than an exception because both callers meet paths that may
    legitimately not exist — :meth:`WorktreeSnapshot.take` skips unreadable
    ones, and :meth:`WorktreeSnapshot.restore_ownership` runs over the whole
    snapshot, where a missing file is the content restore's problem, not this
    pass's.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_uid, info.st_gid


def current_ref(directory: str) -> str:
    """The ref to come back to: the checked-out branch, else the HEAD sha.

    A CI checkout is frequently detached (``actions/checkout`` on a
    ``pull_request`` event), so a branch name cannot be assumed.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        cwd=directory,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return _git(["rev-parse", "HEAD"], cwd=directory).strip()


def create_branch(directory: str, branch: str) -> None:
    """Create ``branch`` at the current HEAD and switch to it, keeping edits.

    ``-B`` rather than ``-b`` so a branch left behind by an interrupted
    earlier run is reused instead of failing the flag. That is safe because
    the name lives in this tool's own ``featureflip/remove-flag/*`` namespace
    AND the caller has already established (via ``github_ops.already_handled``)
    that the remote has neither the branch nor a PR for it.

    The branch is created from the CURRENT HEAD, which is what the Piranha
    diff was computed against — not from a freshly fetched base. Branching
    from anywhere else would put the edits on top of a tree they were never
    computed against.
    """
    logger.debug("creating branch %s in %s", branch, directory)
    _git(["checkout", "-B", branch, "--"], cwd=directory)


def restore_ref(directory: str, ref: str) -> None:
    """Switch back to ``ref`` (branch name or sha) after the PR work."""
    logger.debug("restoring %s in %s", ref, directory)
    _git(["checkout", "--quiet", ref, "--"], cwd=directory)


def delete_branch(directory: str, branch: str) -> None:
    """Delete the local removal branch. Must run after :func:`restore_ref`."""
    _git(["branch", "--quiet", "-D", branch], cwd=directory)


def commit_changes(directory: str, paths: Iterable[str], message: str) -> None:
    """Stage tracked modifications under ``paths`` and commit them.

    ``--update`` stages modifications and deletions of *tracked* files only —
    never anything untracked. Combined with the ``paths`` pathspec that bounds
    what this run STAGES to the directories it was configured to touch.

    Staging is not the same as committing, and bounding only the staging step
    was not enough. ``git commit`` with no pathspec commits the whole index,
    and the index is not necessarily ours: ``ensure_clean`` is deliberately
    scoped to the configured directories, so a workflow step that staged
    something elsewhere before this one runs (a generated lockfile, a previous
    action's ``git add``) passes preflight. That content then defeated the
    "nothing was staged" guard below — ``git diff --cached`` was repo-wide too,
    so it saw the foreign change and reported a full index — and was committed
    and pushed under a "Remove dead feature flag" title, in a PR that could
    contain none of the removal it claimed. So the commit is bounded to the
    exact staged paths under ``paths``, and anything else in the index is left
    staged and uncommitted, exactly as this tool found it.

    ``--no-verify`` skips the customer's commit hooks: this commit must
    contain exactly what Piranha produced, and a repo whose ``pre-commit``
    reformats (or rejects) files would otherwise make the PR contents
    unpredictable.

    Raises :class:`GitCommandError` when nothing was staged *under those paths*
    — the caller only commits after seeing a non-empty diff, so an empty result
    means the edits landed outside the configured directories and must not be
    reported as a successful removal.

    Every path is made absolute first. A pathspec is resolved against **git's**
    working directory, which here is ``directory`` (the first configured entry)
    — so passing a *relative* configured path through verbatim resolved it
    inside itself: with ``directories: src``, git looked for ``src/src`` and
    failed the flag with ``pathspec 'src' did not match any files``. Relative
    entries are exactly what ``action.yml`` documents ("relative to the
    checkout"), so this was every flag in the most common configuration.
    ``os.path.abspath`` resolves against the *process* cwd, which is what the
    customer's workflow means by "relative to the checkout".
    """
    pathspecs = [os.path.abspath(path) for path in paths]
    try:
        _stage_and_commit(directory, pathspecs, message)
    except BaseException:
        # `git add --update` has already run, and the caller's recovery
        # (`reset_worktree`) restores from the INDEX — so leaving this run's
        # edits staged made that recovery write them straight back onto the
        # disk the transform's undo had just cleaned. `restore_ref` then
        # carried them across and the snapshot refused the tree, turning one
        # flag's commit failure into a whole-run abort that left the customer
        # with a staged half-removal. Put the index back before anything else
        # reads it.
        _unstage(directory, pathspecs)
        raise


def _unstage(directory: str, pathspecs: Sequence[str]) -> None:
    """Drop this run's index entries under ``pathspecs``. Best effort.

    Scoped to ``pathspecs`` rather than the repository, so a workflow step that
    staged something OUTSIDE the configured directories — which ``ensure_clean``
    deliberately permits — survives. Inside them nothing else can be staged:
    that is exactly what ``ensure_clean`` refuses to run with.

    Only the index. The bytes on disk belong to the transform's undo, and a
    second, competing rollback here is precisely the confusion this closes.

    A failure is logged and swallowed rather than raised, because this runs
    while an exception is already propagating and replacing it would hide the
    reason the commit failed.
    """
    try:
        _git(["reset", "--quiet", "--", *pathspecs], cwd=directory)
    except Exception as exc:  # noqa: BLE001 - must not mask the real failure
        logger.warning(
            "could not unstage %s after a failed commit: %s. The next reset "
            "may restore those staged edits; the run will abort rather than "
            "carry them into another flag's pull request",
            ", ".join(pathspecs),
            exc,
        )


def _stage_and_commit(directory: str, pathspecs: Sequence[str], message: str) -> None:
    """Stage the tracked edits under ``pathspecs`` and commit exactly those."""
    _git(["add", "--update", "--", *pathspecs], cwd=directory)

    staged = _staged_files(directory, pathspecs)
    if not staged:
        raise GitCommandError(_nothing_staged_message(directory, pathspecs))

    _git(
        [
            "-c",
            f"user.name={BOT_NAME}",
            "-c",
            f"user.email={BOT_EMAIL}",
            # A repo configured to sign commits would otherwise fail here:
            # there is no key for the bot identity, and a signature it could
            # produce would be meaningless anyway.
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            message,
            # The files, not the configured directories. Naming a directory
            # that happens to hold no tracked file aborts the commit
            # (`pathspec … did not match any file(s) known to git`), which a
            # second configured entry can easily be; naming the staged files
            # cannot, because git is where the list came from.
            "--",
            *staged,
        ],
        cwd=directory,
    )


def _staged_files(directory: str, pathspecs: Sequence[str]) -> list[str]:
    """Absolute paths of the staged changes under ``pathspecs``.

    Absolute because a pathspec is resolved against git's working directory,
    and ``directory`` may be a subdirectory of the repository while these names
    come back relative to its root. ``--no-relative`` pins that: a customer
    with ``diff.relative=true`` in their git config would otherwise get names
    relative to ``directory`` and a commit that named the wrong files.

    ``--no-renames`` because a rename is reported as its destination alone, and
    committing only that would add the new file while leaving the deletion of
    the old one staged — half a rename. This tool never creates one (Piranha
    rewrites in place and ``--update`` never adds), so the flag costs nothing
    and removes the failure mode entirely.
    """
    top = _git(["rev-parse", "--show-toplevel"], cwd=directory).strip()
    output = _git(
        [
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-relative",
            "--",
            *pathspecs,
        ],
        cwd=directory,
    )
    return [os.path.join(top, name) for name in output.split("\0") if name]


def push_branch(directory: str, branch: str, push_url: str, token: str) -> None:
    """Push ``branch`` to ``push_url``, authenticating via a per-call header.

    ``push_url`` carries no credential, so a push failure echoes a clean URL.
    The token is supplied as an ``http.extraHeader`` through ``GIT_CONFIG_*``
    environment variables scoped to this subprocess (git >= 2.31) — it is
    never written to ``.git/config`` and never appears in ``argv``. The empty
    first value resets any ``extraHeader`` list a previous step (e.g.
    ``actions/checkout`` with ``persist-credentials``) already installed, so
    exactly one ``Authorization`` header is sent.

    Not a force push: if the remote ref appeared since the idempotency check,
    the push is rejected and this flag fails loudly rather than overwriting
    somebody's work.
    """
    logger.info("pushing %s to %s", branch, push_url)
    _git(
        ["push", "--quiet", push_url, f"refs/heads/{branch}:refs/heads/{branch}"],
        cwd=directory,
        env=_authenticated_env(token),
        secret=token,
    )


def _nothing_staged_message(directory: str, pathspecs: Sequence[str]) -> str:
    """Explain an empty index after a non-empty Piranha diff.

    There is one overwhelmingly likely cause, and it is not obvious: Piranha
    rewrites any file whose *text* holds the flag key, tracked or not, while
    ``git add --update`` can only stage tracked ones. A flag read exclusively
    from a gitignored or untracked file therefore produces a real diff that can
    never become a commit — on every run, forever, since nothing about the
    repository changes in between. Naming those files turns a permanently red
    weekly build into an actionable one.
    """
    base = (
        "nothing was staged for commit even though Piranha reported a diff "
        f"(paths: {', '.join(pathspecs)})"
    )
    untracked = _untracked_or_ignored(directory, pathspecs)
    if not untracked:
        return base
    shown = ", ".join(untracked[:10]) + ("..." if len(untracked) > 10 else "")
    return (
        f"{base}. The rewritten file(s) are not tracked by git (untracked or "
        f"gitignored): {shown}. Only tracked files can be committed, so this "
        "flag cannot be proposed until they are added to git — or add the flag "
        "key to the `ignore` input to stop trying"
    )


def _untracked_or_ignored(directory: str, pathspecs: Sequence[str]) -> list[str]:
    """Paths under ``pathspecs`` git is not tracking (untracked or ignored)."""
    try:
        output = _git(
            [
                "status",
                "--porcelain",
                "--ignored",
                "--untracked-files=all",
                "--",
                *pathspecs,
            ],
            cwd=directory,
        )
    except GitCommandError:  # pragma: no cover - diagnostics must not mask the error
        return []
    return [
        line[3:].strip()
        for line in output.splitlines()
        if line.startswith(("??", "!!"))
    ]


def delete_remote_branch(directory: str, branch: str, push_url: str, token: str) -> None:
    """Delete ``branch`` from the remote. Undoes a push whose PR never opened.

    Without this, a run that pushed successfully but then failed to open the
    pull request (a 422 from a wrong base branch, a repository with PR creation
    disabled) left the branch behind — and ``github_ops.already_handled`` reads
    a bare branch as "handled", so the flag could never be proposed again
    without somebody deleting the branch by hand.

    Raises :class:`GitCommandError` if the delete is refused; the caller must
    report that loudly, since what is left behind is exactly the silent wedge
    this function exists to prevent.
    """
    logger.info("deleting the pushed branch %s from %s", branch, push_url)
    _git(
        ["push", "--quiet", push_url, f":refs/heads/{branch}"],
        cwd=directory,
        env=_authenticated_env(token),
        secret=token,
    )


def head_sha(directory: str) -> str:
    """The full commit sha at HEAD — what a removal branch would be cut from."""
    return _git(["rev-parse", "HEAD"], cwd=directory).strip()


def tracked_files(directory: str) -> frozenset[str]:
    """Absolute paths of the files git would actually commit under ``directory``.

    ``commit_changes`` stages with ``--update``, which touches TRACKED files
    only, so anything outside this set can be rewritten and reported and can
    still never reach the pull request. Two real cases, both found by running
    against a purpose-built repository:

    * a **gitignored** build tree — the dry run showed `build/generated.go`
      changing, the real run's PR did not contain it;
    * a **submodule** — its contents are not the parent's files at all, so the
      tool was proposing edits to a repository the customer may not own.

    Both left the transform doing work that the snapshot then undid, and — the
    part that matters — made ``--dry-run`` preview more than a real run can
    deliver, which is the one thing that preview exists to be trusted for.

    ``-z`` because a path may contain a newline, and ``--cached`` rather than
    the default so the answer is "what is in the index", not "what is on disk".
    A submodule appears here as its single gitlink path, never as the files
    inside it, which is exactly the filter wanted.

    Returns an EMPTY set only when git genuinely reports nothing tracked. A
    failure raises, because silently returning nothing would make this filter
    look like "there is nothing to transform" and turn every flag into a quiet
    ``no-changes``.
    """
    output = _git(["ls-files", "--cached", "-z"], cwd=work_dir(directory))
    root = work_dir(directory)
    return frozenset(
        os.path.abspath(os.path.join(root, entry))
        for entry in output.split("\0")
        if entry
    )


def _authenticated_env(token: str) -> dict[str, str]:
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)

    # Append to whatever env-based config the workflow already set rather than
    # overwriting it — clobbering GIT_CONFIG_COUNT would silently drop a
    # customer's own settings (a proxy, say) for this one command.
    try:
        existing = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        existing = 0
    if existing < 0:
        existing = 0

    env.update(
        {
            # Never block on a credential prompt in a non-interactive runner.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": str(existing + 2),
            # An empty value resets the extraHeader list, so a credential
            # persisted by actions/checkout cannot ride along as a second
            # Authorization header.
            f"GIT_CONFIG_KEY_{existing}": "http.extraheader",
            f"GIT_CONFIG_VALUE_{existing}": "",
            f"GIT_CONFIG_KEY_{existing + 1}": "http.extraheader",
            f"GIT_CONFIG_VALUE_{existing + 1}": f"AUTHORIZATION: basic {header}",
        }
    )
    return env


def _git(
    args: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    secret: str | None = None,
) -> str:
    """Run git, returning stdout; raise :class:`GitCommandError` on failure.

    Output is captured (never inherited) and redacted so nothing a git
    subprocess prints can carry a credential into the workflow log.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitCommandError(
            f"git {_redact(' '.join(args), secret)} failed with exit code "
            f"{result.returncode}: {_redact(detail, secret)}"
        )
    return result.stdout


def _redact(text: str, secret: str | None) -> str:
    """Replace ``secret`` (and its base64 form) with ``***``.

    Belt and braces: nothing here is *supposed* to put the token into a
    command line or a git message, but this is the last place before text
    reaches a log, so it does not rely on that holding.
    """
    if not secret:
        return text
    encoded = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
    return text.replace(secret, _REDACTED).replace(encoded, _REDACTED)
