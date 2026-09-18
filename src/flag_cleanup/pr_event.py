"""Read the event this run was triggered by out of the Actions payload.

Archive-on-merge is driven by a ``pull_request`` event, not by a schedule, so
the one fact the run turns on — *which* pull request merged, and whether it
merged at all — comes from the payload the runner writes to
``GITHUB_EVENT_PATH`` rather than from any Action input.

Read from the payload rather than from ``GITHUB_HEAD_REF`` (which the runner
also sets for pull-request events) because the payload carries ``merged`` in
the same object as the branch name. A closed-but-unmerged pull request looks
identical in the environment variables, and archiving a flag whose removal was
*rejected* is the single worst thing this mode could do.

`pr-command` mode is driven by an ``issue_comment`` event instead. GitHub
delivers a comment on an issue and a comment on a pull request through that
SAME event — the only discriminator is whether the payload's ``issue`` object
carries a ``pull_request`` key — so a customer's workflow sees both, and this
module's job is to pick the pull-request comments out and quietly ignore the
rest.

`archive-on-deploy` mode is driven by a ``deployment_status`` event, and the
shape of the problem is the same a third time: the workflow fires on every
status of every deployment to every environment, and almost none of those is
the one event that authorizes an archive. Deciding WHICH of them counts needs
the Action's own configuration, so this module reads the payload and
:mod:`flag_cleanup.deploy` applies the policy — which is why
:func:`deployment_event` hands back the state and the environment rather than
judging them.
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
    """This mode's trigger is wrong, or its event payload cannot be read.

    Named for the first mode that needed it and kept for all three: every
    event-driven mode here — ``archive-on-merge`` (``pull_request``),
    ``pr-command`` (``issue_comment``) and ``archive-on-deploy``
    (``deployment_status``) — has exactly one shape of "the workflow cannot
    work as written", and one type is what lets ``__main__`` map every one of
    them to exit 2 without knowing which mode raised it.

    A configuration fault, not a bad flag: raised before anything is called, so
    nothing has been archived. Silently doing nothing would leave a customer
    believing the mode was running for as long as they cared to look.
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

    payload = _event_payload(env)

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


def _event_payload(env: Mapping[str, str]) -> object:
    """The parsed Actions event payload, or a `PullRequestEventError`."""
    event_path = (env.get("GITHUB_EVENT_PATH") or "").strip()
    if not event_path:
        raise PullRequestEventError(
            "GITHUB_EVENT_PATH is not set, so the event that triggered this "
            "run cannot be identified (it is provided by the Actions runner)"
        )
    try:
        return json.loads(Path(event_path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PullRequestEventError(f"could not read the event payload: {exc}") from exc
    except ValueError as exc:
        raise PullRequestEventError(f"the event payload is not valid JSON: {exc}") from exc


#: The event that carries a pull-request comment. GitHub delivers comments on
#: issues and on pull requests through the SAME event; the discriminator is
#: `issue.pull_request`, which is present only for a pull request.
COMMENT_EVENTS = ("issue_comment",)


@dataclass(frozen=True, slots=True)
class PullRequestComment:
    """A comment left on a pull request."""

    number: int
    body: str
    commenter: str


def commented_pull_request(
    env: Mapping[str, str] | None = None,
) -> PullRequestComment | None:
    """The pull-request comment this run was triggered by.

    ``None`` — a clean no-op, not a failure — when the comment was left on a
    plain ISSUE. A customer wires this workflow on `issue_comment`, which
    fires for both, and almost none of what it sees is a removal pull request.

    Raises :class:`PullRequestEventError` for the shapes that mean the workflow
    itself is wrong (a non-comment trigger, a missing or unreadable payload) and
    for a comment whose body or author cannot be read. The author is NOT
    allowed to degrade the way ``merged_pull_request``'s ``number`` is: it is
    what authorizes the command, so an unattributable one must not run.
    """
    env = os.environ if env is None else env

    event_name = (env.get("GITHUB_EVENT_NAME") or "").strip()
    if event_name not in COMMENT_EVENTS:
        raise PullRequestEventError(
            f"pr-command mode runs on a comment, but this run was triggered by "
            f"{event_name or '(no GITHUB_EVENT_NAME)'!r}. Trigger the workflow "
            f"with `on: issue_comment: types: [created]`"
        )

    payload = _event_payload(env)

    issue = payload.get("issue") if isinstance(payload, dict) else None
    if not isinstance(issue, dict):
        raise PullRequestEventError(
            "the event payload carries no `issue` object, so there is no pull "
            "request to act on"
        )
    if not isinstance(issue.get("pull_request"), dict):
        return None

    number = issue.get("number")
    if not isinstance(number, int):
        raise PullRequestEventError(
            "the commented pull request's payload carries no `issue.number`, "
            "so the pull request cannot be identified"
        )

    comment = payload.get("comment")
    body = comment.get("body") if isinstance(comment, dict) else None
    if not isinstance(body, str) or not body.strip():
        raise PullRequestEventError(
            "the event payload carries no usable comment body, so there is no "
            "command to read"
        )

    user = comment.get("user") if isinstance(comment, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise PullRequestEventError(
            "the comment carries no `user.login`, so there is no way to tell "
            "who wrote it — and the author is what authorizes the command"
        )

    return PullRequestComment(number=number, body=body, commenter=login)


#: The event a completed deployment arrives on. There is no narrower trigger:
#: GitHub delivers pending, in-progress, success, failure, error and inactive
#: through this one event, for every environment the repository deploys to.
DEPLOYMENT_EVENTS = ("deployment_status",)

#: The one ``deployment_status`` state that says a build is actually running.
#: Every other one — ``pending``, ``in_progress``, ``failure``, ``error``,
#: ``inactive`` — leaves the previous build in place, so a removal it would
#: otherwise authorize is not live and the flag must stay exactly as it is.
DEPLOYMENT_SUCCESS = "success"


@dataclass(frozen=True, slots=True)
class DeploymentEvent:
    """One ``deployment_status`` delivery, read but not judged.

    ``state`` and ``environment`` are handed back verbatim so
    :mod:`flag_cleanup.deploy` can decide which combination authorizes an
    archive — that decision needs the Action's configured environment, which
    this module has no business knowing.

    ``sha`` is the commit the deployment shipped, and is the one field this
    module validates rather than forwards: see :func:`deployment_event`.
    """

    state: str
    environment: str
    sha: str


def deployment_event(env: Mapping[str, str] | None = None) -> DeploymentEvent:
    """The deployment status this run was triggered by.

    Raises :class:`PullRequestEventError` for the shapes that mean the workflow
    itself is wrong: a trigger other than ``deployment_status`` (this mode on a
    schedule would otherwise sit there doing nothing forever), a payload that
    is missing or unreadable, or one carrying no ``deployment`` object.

    **``sha`` is required only when the state is ``success``**, and the
    asymmetry is deliberate. A payload without it is one this mode cannot read
    at all, so exit 2 is the honest answer — but only once GitHub has said a
    deployment actually finished. Demanding it of every delivery would redden
    the run on a ``pending`` status this mode ignores by design, which is the
    commonest thing the trigger produces.

    Nothing here is returned as ``None``: unlike a pull request that closed
    without merging, *which* deployments are ignorable is configuration rather
    than a property of the payload, so the no-op outcomes are named by
    :func:`~flag_cleanup.deploy.run_archive_on_deploy` instead.
    """
    env = os.environ if env is None else env

    event_name = (env.get("GITHUB_EVENT_NAME") or "").strip()
    if event_name not in DEPLOYMENT_EVENTS:
        raise PullRequestEventError(
            f"archive-on-deploy runs on a deployment status, but this run was "
            f"triggered by {event_name or '(no GITHUB_EVENT_NAME)'!r}. Trigger "
            f"the workflow with `on: deployment_status`"
        )

    payload = _event_payload(env)

    deployment = payload.get("deployment") if isinstance(payload, dict) else None
    if not isinstance(deployment, dict):
        raise PullRequestEventError(
            "the event payload carries no `deployment` object, so there is "
            "nothing to archive against"
        )

    status = payload.get("deployment_status")
    state = status.get("state") if isinstance(status, dict) else None
    state = state.strip() if isinstance(state, str) else ""

    environment = deployment.get("environment")
    environment = environment.strip() if isinstance(environment, str) else ""

    sha = deployment.get("sha")
    sha = sha.strip() if isinstance(sha, str) else ""
    if state == DEPLOYMENT_SUCCESS and not sha:
        raise PullRequestEventError(
            "the successful deployment's payload carries no `deployment.sha`, "
            "so the commit it deployed cannot be identified"
        )

    return DeploymentEvent(state=state, environment=environment, sha=sha)
