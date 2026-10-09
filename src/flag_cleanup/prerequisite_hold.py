"""Hold a removal pull request while live flags still use its flag as a prerequisite.

Archiving a flag that another live flag lists as a prerequisite makes that flag
fail its prerequisite check and serve its off variation, so its removal has to
wait until those dependents are removed and archived. A removal pull request
that is already open when that is discovered is converted to a draft, with a
comment saying what it waits on; once nothing depends on the flag any more, it
is put back the way it was.

The comment is the state. Its hidden marker records whether the pull request
was already a draft, so a release restores exactly that. It is written only
once the pull request IS a draft, so a hold on record always means the
conversion happened; a conversion GitHub refused leaves nothing behind, and the
next run simply tries again.

Only comments by the pull request's own author, or by a GitHub App's bot
account, are believed, so a person cannot place or lift a hold by commenting.
Bots are trusted beyond the author because the token this Action runs with can
change between runs (the default token swapped for an app's), and a hold its
new identity placed must still be readable, or every run would re-hold and the
hold could never be released. Commenting as a bot takes an app installed on the
repository.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from flag_cleanup import github_ops
from flag_cleanup.github_ops import GitHubApi, OpenPullRequest

logger = logging.getLogger(__name__)

HOLD_MARKER_PREFIX = "<!-- featureflip-cleanup:prerequisite-hold"
RELEASE_MARKER = "<!-- featureflip-cleanup:prerequisite-release -->"
_HOLD = re.compile(re.escape(HOLD_MARKER_PREFIX) + r" was-draft=(true|false) -->")


@dataclass(frozen=True, slots=True)
class Hold:
    """An active hold, and whether the pull request was a draft before it."""

    was_draft: bool


def active_hold(comments: list[dict], author: str) -> Hold | None:
    """The hold the latest trusted marker describes, or ``None`` if none is active."""
    trusted = [c for c in comments if _trusted(c, author)]
    trusted.sort(key=lambda c: str(c.get("created_at") or ""))
    current: Hold | None = None
    for comment in trusted:
        body = str(comment.get("body") or "")
        match = _HOLD.search(body)
        if match:
            current = Hold(was_draft=match.group(1) == "true")
        elif RELEASE_MARKER in body:
            current = None
    return current


def hold(
    gh: GitHubApi, repo: str, pull: OpenPullRequest, key: str, blocked_by: tuple[str, ...]
) -> None:
    """Convert ``pull`` to a draft and say why. Best effort; never raises on API refusals."""
    comments = github_ops.issue_comments(gh, repo, pull.number)
    if comments is None:
        logger.warning(
            "could not read the comments on pull request #%s, so it was not put on "
            "hold; %s is still a prerequisite of %s",
            pull.number, key, ", ".join(blocked_by),
        )
        return
    if active_hold(comments, pull.author) is not None:
        if not pull.draft:
            logger.warning(
                "pull request #%s is on hold for %s's dependents but was marked "
                "ready by hand; leaving it as it is",
                pull.number, key,
            )
        return
    # Draft first, so the comment is only ever a record of a conversion that
    # happened. A refused conversion records nothing: the next run tries again,
    # rather than reading its own hold on a ready pull request as a human's
    # decision to mark it ready.
    if not pull.draft and not github_ops.set_draft(gh, pull, draft=True):
        return
    if not github_ops.comment_on_pull_request(
        gh, repo, pull.number, _hold_body(key, blocked_by, was_draft=pull.draft)
    ):
        if not pull.draft:
            # The comment is the record of what to restore; a draft without it
            # is one nothing knows to undo.
            github_ops.set_draft(gh, pull, draft=False)
        return


def release(gh: GitHubApi, repo: str, pull: OpenPullRequest, key: str) -> None:
    """Undo an active hold on ``pull``. A no-op when it is not on hold."""
    comments = github_ops.issue_comments(gh, repo, pull.number)
    if comments is None:
        return
    current = active_hold(comments, pull.author)
    if current is None:
        return
    marked_ready = False
    if not current.was_draft and pull.draft:
        if not github_ops.set_draft(gh, pull, draft=False):
            # The hold stays recorded, so the next run tries again.
            return
        marked_ready = True
    github_ops.comment_on_pull_request(gh, repo, pull.number, _release_body(key, marked_ready))


def _trusted(comment: dict, author: str) -> bool:
    user = comment.get("user")
    if not isinstance(user, dict):
        return False
    return user.get("login") == author or user.get("type") == "Bot"


def _names(keys: tuple[str, ...]) -> str:
    return ", ".join(f"`{k}`" for k in keys)


def _hold_body(key: str, blocked_by: tuple[str, ...], *, was_draft: bool) -> str:
    state = (
        "This pull request was already a draft and stays one."
        if was_draft
        else "This pull request has been converted to a draft."
    )
    return (
        "**flag-cleanup — on hold**\n\n"
        f"`{key}` is still a prerequisite of {len(blocked_by)} live flag(s): "
        f"{_names(blocked_by)}. Archiving it now would make each of them fail its "
        "prerequisite check and serve its off variation. Remove and archive those "
        f"flags first. {state} It is released automatically on the first run "
        "after nothing depends on this flag any more.\n\n"
        f"{HOLD_MARKER_PREFIX} was-draft={'true' if was_draft else 'false'} -->"
    )


def _release_body(key: str, marked_ready: bool) -> str:
    state = " and has been marked ready for review" if marked_ready else ""
    return (
        "**flag-cleanup — hold released**\n\n"
        f"No live flag lists `{key}` as a prerequisite any more, so this pull "
        f"request is no longer on hold{state}.\n\n"
        f"{RELEASE_MARKER}"
    )
