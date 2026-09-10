"""Read the merged pull request out of the Actions event payload.

Archive-on-merge is driven by a ``pull_request`` event, not by a schedule, so
the one fact the run turns on — *which* pull request merged, and whether it
merged at all — comes from the payload the runner writes to
``GITHUB_EVENT_PATH`` rather than from any Action input.

Read from the payload rather than from ``GITHUB_HEAD_REF`` (which the runner
also sets for pull-request events) because the payload carries ``merged`` in
the same object as the branch name. A closed-but-unmerged pull request looks
identical in the environment variables, and archiving a flag whose removal was
*rejected* is the single worst thing this mode could do.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Event names that carry a ``pull_request`` object. ``pull_request_target``
#: is accepted because a repository with restricted fork permissions may have
#: to use it, and the payload shape is identical.
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")


class PullRequestEventError(RuntimeError):
    """The run is in archive mode but the event is not a usable pull request.

    A configuration fault, not a bad flag: raised before anything is called, so
    nothing has been archived. ``__main__`` renders it as exit 2 alongside the
    other "this workflow cannot work as written" failures — silently doing
    nothing would leave a customer believing archive-on-merge was running for
    as long as they cared to look.
    """


@dataclass(frozen=True, slots=True)
class MergedPullRequest:
    """A pull request that actually merged."""

    number: int
    head_ref: str


def merged_pull_request(env: Mapping[str, str] | None = None) -> MergedPullRequest | None:
    """The merged pull request this run was triggered by.

    ``None`` — a clean no-op, not a failure — when the pull request closed
    WITHOUT merging. That is a customer declining the removal, and it must
    leave their flag exactly as it is.

    Raises :class:`PullRequestEventError` for the shapes that mean the workflow
    itself is wrong: a non-pull-request trigger (archive mode on a schedule
    would otherwise sit there doing nothing forever), or a payload that is
    missing, unreadable, or not carrying a pull request.
    """
    env = os.environ if env is None else env

    event_name = (env.get("GITHUB_EVENT_NAME") or "").strip()
    if event_name not in PULL_REQUEST_EVENTS:
        raise PullRequestEventError(
            f"archive-on-merge runs on a pull request, but this run was triggered "
            f"by {event_name or '(no GITHUB_EVENT_NAME)'!r}. Trigger the workflow "
            f"with `on: pull_request: types: [closed]`"
        )

    event_path = (env.get("GITHUB_EVENT_PATH") or "").strip()
    if not event_path:
        raise PullRequestEventError(
            "GITHUB_EVENT_PATH is not set, so the pull request that triggered "
            "this run cannot be identified (it is provided by the Actions runner)"
        )

    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PullRequestEventError(f"could not read the event payload: {exc}") from exc
    except ValueError as exc:
        raise PullRequestEventError(f"the event payload is not valid JSON: {exc}") from exc

    pull_request = payload.get("pull_request") if isinstance(payload, dict) else None
    if not isinstance(pull_request, dict):
        raise PullRequestEventError(
            "the event payload carries no `pull_request` object, so there is "
            "nothing to archive against"
        )

    # Strictly `is True`. A payload that omits the field, or sends it as the
    # string "false", must never read as merged: truthiness says `bool("false")`
    # is True, and this is the flag that decides whether a customer's rejected
    # removal archives their flag anyway.
    if pull_request.get("merged") is not True:
        return None

    head = pull_request.get("head")
    head_ref = head.get("ref") if isinstance(head, dict) else None
    if not isinstance(head_ref, str) or not head_ref:
        raise PullRequestEventError(
            "the merged pull request's payload carries no `head.ref`, so the "
            "branch it merged from cannot be identified"
        )

    number = pull_request.get("number")
    return MergedPullRequest(
        number=number if isinstance(number, int) else 0,
        head_ref=head_ref,
    )
