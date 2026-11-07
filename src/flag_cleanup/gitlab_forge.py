"""GitLab (gitlab.com and self-managed) as a :class:`~flag_cleanup.forge.Forge`.

REST v4 only, authenticated with a project, group or personal access token in
the ``PRIVATE-TOKEN`` header. ``CI_JOB_TOKEN`` is never used: it cannot open
merge requests.

The same rule holds here as for every forge: a lookup that cannot get a
definite answer raises, and never reads as "absent". "Absent" is what reopens
a change the customer closed, or archives a flag whose removal never merged.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping, Sequence
from typing import NoReturn
from urllib.parse import quote

import httpx

from flag_cleanup import http_retry
from flag_cleanup.branches import Commit
from flag_cleanup.forge import (
    BaseRefError,
    Comment,
    ForgeApiError,
    ForgeAuthError,
    HandledPullRequest,
    MergedRemoval,
    OpenPullRequest,
    PullRequest,
)
from flag_cleanup.run_context import RunContext

logger = logging.getLogger(__name__)

#: The most list pages one lookup reads. A removal branch has one merge
#: request, or a handful; more pages than this is not a list to trust.
_MAX_PAGES = 10
_PAGE_SIZE = 100
#: The most commits a removal branch may carry before the ownership guard
#: refuses to judge it, the same bound the GitHub comparison has.
_MAX_BRANCH_COMMITS = 250


class GitLabApiError(ForgeApiError):
    """A GitLab API call failed. Never carries the token in its message."""


class GitLabAuthError(GitLabApiError, ForgeAuthError):
    """A GitLab call was refused with 401/403: a run-level fact, not a bad flag."""


class GitLabApi:
    """Minimal authenticated transport for the endpoints this tool uses.

    Returns raw responses, like the GitHub transport: callers must tell a 404
    that means "absent" from every other answer, and only they know which
    endpoints those are.
    """

    def __init__(
        self,
        token: str,
        *,
        api_url: str,
        transport: httpx.BaseTransport | None = None,
        sleep: http_retry.Sleep = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            transport=transport,
            timeout=http_retry.TIMEOUT,
            headers={"PRIVATE-TOKEN": token},
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        return http_retry.get(
            self._client, path, params=dict(params) if params else None, sleep=self._sleep
        )

    def post(self, path: str, json: dict) -> httpx.Response:
        return self._client.post(path, json=json)

    def put(self, path: str, json: dict) -> httpx.Response:
        return self._client.put(path, json=json)


def _message(response: httpx.Response) -> str:
    """GitLab's own error text, truncated. Never includes request headers."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)[:200]
    return str(payload)[:200]


def _raise(response: httpx.Response, message: str) -> NoReturn:
    """Raise :class:`GitLabAuthError` for 401/403, else :class:`GitLabApiError`."""
    error = GitLabAuthError if response.status_code in (401, 403) else GitLabApiError
    raise error(f"{message} (HTTP {response.status_code}): {_message(response)}")


def _json_object(response: httpx.Response, message: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise GitLabApiError(f"{message}: the response was not an object")
    return payload


def _pages(
    api: GitLabApi, path: str, params: Mapping[str, str], message: str
) -> list[dict]:
    """Every item of a paginated list, or a raise.

    A 200 whose body is not a list is not an empty answer, and a list longer
    than :data:`_MAX_PAGES` pages is not read as complete.
    """
    items: list[dict] = []
    page = "1"
    for _ in range(_MAX_PAGES):
        response = api.get(path, {**params, "per_page": str(_PAGE_SIZE), "page": page})
        if response.status_code != 200:
            _raise(response, message)
        try:
            batch = response.json()
        except ValueError:
            batch = None
        if not isinstance(batch, list):
            raise GitLabApiError(f"{message}: the response was not a list")
        items.extend(item for item in batch if isinstance(item, dict))
        page = response.headers.get("X-Next-Page", "").strip()
        if not page:
            return items
    raise GitLabApiError(f"{message}: more than {_MAX_PAGES} pages, not reading further")


#: GitLab's merge-request states in this tool's vocabulary. ``locked`` is a
#: merge request mid-merge or frozen: not one to propose again.
_STATES = {"opened": "open", "closed": "closed", "locked": "closed", "merged": "merged"}
#: The ``state_event`` that moves a merge request to each state.
_STATE_EVENTS = {"open": "reopen", "closed": "close"}

#: Every title prefix GitLab reads as "draft", case-insensitive.
_DRAFT_PREFIX = re.compile(r"^\s*(?:\[draft\]|\(draft\)|draft:)\s*", re.IGNORECASE)


def _is_draft(mr: dict) -> bool:
    """GitLab's own ``draft`` field, else the title prefix older versions use."""
    draft = mr.get("draft")
    if isinstance(draft, bool):
        return draft
    return bool(_DRAFT_PREFIX.match(str(mr.get("title") or "")))


def _open_pull(mr: dict) -> OpenPullRequest | None:
    try:
        return OpenPullRequest(
            number=int(mr["iid"]),
            node_id="",
            draft=_is_draft(mr),
            author=str(mr["author"]["username"]),
            html_url=str(mr["web_url"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


class GitLabForge:
    """:class:`~flag_cleanup.forge.Forge` over the GitLab REST v4 API."""

    name = "gitlab"
    display_name = "GitLab"
    supports_draft = True
    #: GitLab accepts any user name beside an access token over HTTPS; this
    #: is the one its documentation uses.
    git_basic_user = "oauth2"
    token_refused_hint = (
        "Check that the token can create merge requests and push branches: a "
        "project or group access token with the api scope and at least the "
        "Developer role"
    )

    def __init__(self, api: GitLabApi, ctx: RunContext, *, owns_api: bool = False) -> None:
        self._api = api
        self._ctx = ctx
        self._owns_api = owns_api
        self._project = "/projects/" + quote(ctx.repository, safe="")
        self._project_id: int | None = None
        self._token_user: str | None = None

    def __repr__(self) -> str:
        return f"GitLabForge(repository={self.repository!r}, api_url={self._ctx.api_url!r})"

    @property
    def repository(self) -> str:
        return self._ctx.repository

    @property
    def token(self) -> str:
        return self._ctx.token

    @property
    def push_url(self) -> str:
        return f"{self._ctx.server_url.rstrip('/')}/{self._ctx.repository}.git"

    def close(self) -> None:
        if self._owns_api:
            self._api.close()

    def project_id(self) -> int:
        """The project's numeric id, read once."""
        if self._project_id is None:
            response = self._api.get(self._project)
            if response.status_code != 200:
                _raise(response, f"could not read the project {self.repository}")
            project = _json_object(response, f"could not read the project {self.repository}")
            project_id = project.get("id")
            if not isinstance(project_id, int):
                raise GitLabApiError(f"the project {self.repository} reported no id")
            self._project_id = project_id
        return self._project_id

    # --- is this flag already handled? ---------------------------------------

    def _merge_request_lookup(self, branch: str) -> dict | None:
        """The newest merge request from ``branch`` in ANY state, or ``None``.

        ``state=all``, so a merge request the customer closed counts, and a
        fork's merge request from a branch of the same name counts too: being
        wrong that way skips a flag, which is the cheap mistake.
        """
        response = self._api.get(
            f"{self._project}/merge_requests",
            {
                "source_branch": branch,
                "state": "all",
                "order_by": "created_at",
                "sort": "desc",
                "per_page": "1",
            },
        )
        message = (
            f"could not list merge requests for {branch}; refusing to assume "
            "there are none"
        )
        if response.status_code != 200:
            _raise(response, message)
        try:
            mrs = response.json()
        except ValueError:
            mrs = None
        if not isinstance(mrs, list):
            raise GitLabApiError(f"{message}: the response was not a list")
        if not mrs:
            return None
        return mrs[0] if isinstance(mrs[0], dict) else {}

    def _branch_exists(self, branch: str) -> bool:
        response = self._api.get(
            f"{self._project}/repository/branches/{quote(branch, safe='')}"
        )
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            _raise(
                response,
                f"could not check whether branch {branch} exists; refusing to "
                "assume it does not",
            )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        # Only an answer naming THIS branch means it exists.
        return isinstance(payload, dict) and payload.get("name") == branch

    def handled(self, branch: str) -> HandledPullRequest:
        mr = self._merge_request_lookup(branch)
        if mr is not None:
            state = mr.get("state")
            url = str(mr.get("web_url") or "")
            if state == "opened":
                logger.info(
                    "merge request !%s from %s is still open — not proposing it again",
                    mr.get("iid"),
                    branch,
                )
                return HandledPullRequest(
                    handled=True,
                    open_pr_url=url,
                    open_pull=_open_pull(mr),
                    state="open",
                )
            merged = state == "merged"
            logger.info(
                "merge request !%s from %s was %s — not proposing it again, and not "
                "reporting it as a live merge request",
                mr.get("iid"),
                branch,
                "merged" if merged else "closed without merging",
            )
            return HandledPullRequest(
                handled=True, state="merged" if merged else "closed", closed_pr_url=url
            )

        if self._branch_exists(branch):
            logger.info(
                "branch %s already exists on %s with no merge request — nothing to "
                "propose or report",
                branch,
                self.repository,
            )
            return HandledPullRequest(handled=True, state="branch")
        return HandledPullRequest(handled=False)

    def existing_change(self, branch: str) -> str | None:
        mr = self._merge_request_lookup(branch)
        if mr is None:
            return None
        logger.info(
            "merge request !%s was already opened from %s (state=%s) — not proposing it again",
            mr.get("iid"),
            branch,
            mr.get("state"),
        )
        return str(mr.get("web_url") or "")

    def default_branch(self) -> str:
        response = self._api.get(self._project)
        message = (
            f"could not read {self.repository} to find its default branch; set "
            "the `base-branch` input explicitly to skip this lookup"
        )
        if response.status_code in (401, 403):
            # The first call of a run: a refusal here is the token, and naming
            # `base-branch` would send the reader to the wrong setting.
            _raise(
                response,
                f"GitLab refused the token reading {self.repository}. "
                f"{self.token_refused_hint}",
            )
        if response.status_code != 200:
            _raise(response, message)
        branch = _json_object(response, message).get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise GitLabApiError(
                f"{self.repository} reported no default branch; set the "
                "`base-branch` input explicitly"
            )
        logger.info("resolved the base branch for %s to %s", self.repository, branch)
        return branch

    # --- opening and maintaining merge requests ------------------------------

    def _mr_path(self, number: int) -> str:
        return f"{self._project}/merge_requests/{number}"

    def open_change(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
        labels: Sequence[str],
        draft: bool,
    ) -> str:
        """Open a merge request; its URL, or ``""`` when GitLab named none.

        Raises only when THIS call created nothing, a 409 for an existing merge
        request included. Past the 201 nothing raises: the caller reads a raise
        as "none was created" and deletes the pushed branch, and deleting a
        merge request's source branch closes it.
        """
        payload: dict = {
            "source_branch": branch,
            "target_branch": base,
            "title": f"Draft: {title}" if draft else title,
            "description": body,
            "remove_source_branch": True,
        }
        if labels:
            # Labels ride on the create itself, so there is no second call to fail.
            payload["labels"] = ",".join(labels)
        response = self._api.post(f"{self._project}/merge_requests", payload)
        if response.status_code != 201:
            _raise(response, f"could not open a merge request for {branch}")

        try:
            mr = response.json()
        except ValueError:
            mr = {}
        url = str(mr.get("web_url") or "") if isinstance(mr, dict) else ""
        if url:
            logger.info("opened %s%s", url, " (draft)" if draft else "")
        else:
            logger.warning(
                "opened a merge request for %s but the response carried no web_url; "
                "the merge request exists — check %s on GitLab",
                branch,
                self.repository,
            )
        return url

    def change(self, number: int) -> PullRequest:
        response = self._api.get(self._mr_path(number))
        message = f"could not read merge request !{number} in {self.repository}"
        if response.status_code != 200:
            _raise(response, message)
        mr = _json_object(response, message)
        head_ref, head_sha, base_ref = mr.get("source_branch"), mr.get("sha"), mr.get("target_branch")
        absent = [
            name
            for name, value in (("source_branch", head_ref), ("sha", head_sha), ("target_branch", base_ref))
            if not isinstance(value, str) or not value
        ]
        if absent:
            raise GitLabApiError(
                f"merge request !{number} in {self.repository} carries no "
                f"{', '.join(absent)}, so it cannot be safely updated"
            )
        state = _STATES.get(str(mr.get("state") or ""), "")
        # A merged change is "closed" with `merged` set, as on every forge.
        return PullRequest(
            number=number,
            head_ref=head_ref,
            head_sha=head_sha,
            base_ref=base_ref,
            state="closed" if state == "merged" else state,
            merged=state == "merged",
            draft=_is_draft(mr),
            html_url=str(mr.get("web_url") or ""),
        )

    def update_change(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> None:
        changes: dict = {}
        if title is not None:
            changes["title"] = title
        if body is not None:
            changes["description"] = body
        if state is not None:
            if state not in _STATE_EVENTS:
                raise ValueError(f"cannot set a merge request's state to {state!r}")
            changes["state_event"] = _STATE_EVENTS[state]
        if not changes:
            return
        response = self._api.put(self._mr_path(number), changes)
        if response.status_code != 200:
            _raise(
                response,
                f"could not update merge request !{number} in {self.repository} "
                f"({', '.join(sorted(changes))})",
            )

    def set_draft(self, pull: OpenPullRequest, *, draft: bool) -> bool:
        """Add or strip the ``Draft:`` title prefix. Never raises.

        The title is read fresh and only its prefix changes, so a title a
        person edited keeps every other character.
        """
        try:
            response = self._api.get(self._mr_path(pull.number))
            if response.status_code != 200:
                _raise(response, f"could not read merge request !{pull.number}")
            title = str(_json_object(response, "merge request").get("title") or "")
            bare = _DRAFT_PREFIX.sub("", title, count=1)
            wanted = f"Draft: {bare}" if draft else bare
            if wanted == title:
                return True
            update = self._api.put(self._mr_path(pull.number), {"title": wanted})
            if update.status_code != 200:
                _raise(update, f"could not retitle merge request !{pull.number}")
            return True
        except Exception as exc:  # noqa: BLE001 - the caller is best effort by design
            logger.warning(
                "could not %s merge request !%s: %s",
                "convert to a draft" if draft else "mark ready",
                pull.number,
                exc,
            )
            return False

    def comment(self, number: int, body: str) -> bool:
        response = self._api.post(f"{self._mr_path(number)}/notes", {"body": body})
        if response.status_code != 201:
            logger.warning(
                "could not comment on merge request !%s in %s (HTTP %d): %s",
                number,
                self.repository,
                response.status_code,
                _message(response),
            )
            return False
        return True

    def comments(self, number: int) -> list[Comment] | None:
        """Every human or bot note, oldest first; ``None`` when unreadable.

        GitLab has no "this is a bot" flag on a note's author, and a project
        access token is its own bot user, so a rotated token is a new author.
        A note by the token's own user is what counts as the tool's, or a hold
        the old token placed could never be lifted by the new one.
        """
        try:
            notes = _pages(
                self._api,
                f"{self._mr_path(number)}/notes",
                {"sort": "asc", "order_by": "created_at"},
                f"could not read the notes on merge request !{number}",
            )
            me = self.token_user()
        except Exception as exc:  # noqa: BLE001 - "could not tell" is None, never []
            logger.warning(
                "could not read the comments on merge request !%s in %s: %s",
                number,
                self.repository,
                exc,
            )
            return None
        comments: list[Comment] = []
        for note in notes:
            if note.get("system") is True:
                continue
            author = note.get("author") if isinstance(note.get("author"), dict) else {}
            username = str(author.get("username") or "")
            comments.append(
                Comment(
                    author=username,
                    is_bot=bool(username) and username == me,
                    created_at=str(note.get("created_at") or ""),
                    body=str(note.get("body") or ""),
                )
            )
        return comments

    # --- containment and merge evidence --------------------------------------

    def _compare_response(self, from_ref: str, to_ref: str) -> httpx.Response:
        # `straight=false` compares from the merge base, like GitHub's `...`:
        # the commits reachable from `to_ref` that `from_ref` does not have.
        return self._api.get(
            f"{self._project}/repository/compare",
            {"from": from_ref, "to": to_ref, "straight": "false"},
        )

    def _compare_commits(self, response: httpx.Response, message: str) -> list:
        payload = _json_object(response, message)
        if payload.get("compare_timeout") is True:
            raise GitLabApiError(f"{message}: GitLab's comparison timed out")
        commits = payload.get("commits")
        if not isinstance(commits, list):
            raise GitLabApiError(f"{message}: the comparison carried no commit list")
        return commits

    def _ref_status(self, ref: str) -> httpx.Response:
        return self._api.get(f"{self._project}/repository/commits/{quote(ref, safe='')}")

    def _require_ref(self, ref: str, message: str) -> None:
        """Raise unless ``ref`` names a commit in this project.

        Asked before every comparison because a compare against a ref GitLab
        cannot resolve has been answered with an EMPTY commit list, not a 404,
        and empty reads as "contained": a typo in a deployed ref would archive
        a flag whose removal never shipped.
        """
        response = self._ref_status(ref)
        if response.status_code == 404:
            raise GitLabApiError(f"{message}: {ref!r} is not in {self.repository}")
        if response.status_code != 200:
            _raise(response, message)

    def _compare(self, from_ref: str, to_ref: str, message: str) -> list:
        """The commits ``to_ref`` carries that ``from_ref`` does not, or a raise.

        Both refs are resolved first (:meth:`_require_ref`), and a failed
        comparison raises like any other failure: "could not tell", never
        "none".
        """
        self._require_ref(from_ref, message)
        self._require_ref(to_ref, message)
        response = self._compare_response(from_ref, to_ref)
        if response.status_code != 200:
            _raise(response, message)
        return self._compare_commits(response, message)

    def ensure_head_on_base(
        self, base: str, head_sha: str, *, base_is_configured_input: bool = True
    ) -> None:
        # The base is resolved first, for the reason `_require_ref` gives; a
        # base that does not resolve is the same misconfiguration a failed
        # comparison is, and gets the same remedy.
        response = self._ref_status(base)
        if response.status_code == 200:
            response = self._ref_status(head_sha)
        if response.status_code == 200:
            response = self._compare_response(base, head_sha)
        if response.status_code in (401, 403):
            _raise(response, f"could not compare {head_sha[:12]} against {base}")
        if response.status_code != 200:
            if base_is_configured_input:
                remedy = (
                    "Check that `base-branch` names a branch that exists and "
                    "that this commit has been pushed"
                )
            else:
                remedy = (
                    f"Check that `{base}` — this pull request's own base branch "
                    "— still exists and that this commit has been pushed"
                )
            raise BaseRefError(
                f"could not compare the checkout ({head_sha[:12]}) against base "
                f"branch {base!r} in {self.repository} (HTTP {response.status_code}): "
                f"{_message(response)}. {remedy}"
            )
        ahead = self._compare_commits(
            response, f"could not compare {head_sha[:12]} against {base}"
        )
        if not ahead:
            return
        raise BaseRefError(
            f"the checkout ({head_sha[:12]}) carries {len(ahead)} commit(s) "
            f"{base!r} does not, so every removal merge request cut from it would "
            "also contain them. Run this from a checkout of the base branch (a "
            "scheduled pipeline on that branch), not from a merge request "
            "pipeline, or set `base-branch` to the branch this checkout is on"
        )

    def branch_commits(self, base: str, branch: str) -> tuple[Commit, ...]:
        commits = self._compare(
            base,
            branch,
            f"could not list the commits on {branch} in {self.repository}; "
            "refusing to assume it carries none of its own",
        )
        # GitLab reports no total, so a truncated list cannot be told from a
        # whole one; a removal branch should carry one commit anyway.
        if len(commits) > _MAX_BRANCH_COMMITS:
            raise GitLabApiError(
                f"{branch} carries too many commits ({len(commits)}) to check "
                "safely. A removal branch should carry one; check what is on it by hand"
            )
        return tuple(
            Commit(
                sha=str(entry.get("id") or ""),
                # An absent email reads as foreign, never as the bot.
                author_email=str(entry.get("author_email") or ""),
                committer_email=str(entry.get("committer_email") or ""),
            )
            for entry in commits
            if isinstance(entry, dict)
        )

    def commit_is_contained(self, commit: str, *, within: str) -> bool:
        missing = self._compare(
            within,
            commit,
            f"could not tell whether {commit[:12]} is contained in {within}; "
            "refusing to assume it is",
        )
        return not missing

    def merged_removal(self, branch: str, *, base: str) -> MergedRemoval | None:
        """The merge request from ``branch`` merged into ``base``, or ``None``.

        Only this project's own merge requests count. A fork can name a branch
        anything, so its merged merge request is not evidence that the
        removal reached this project's base branch.
        """
        mrs = _pages(
            self._api,
            f"{self._project}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": base,
                "state": "merged",
                "order_by": "created_at",
                "sort": "desc",
            },
            f"could not list merge requests for {branch}; refusing to assume "
            "its removal never merged",
        )
        own = self.project_id()
        for mr in mrs:
            if mr.get("source_project_id") != own or mr.get("target_branch", base) != base:
                continue
            # The commit that landed on the base: a merge commit, else the
            # squashed commit, else (fast-forward) the merge request's head.
            sha = next(
                (
                    value
                    for value in (mr.get("merge_commit_sha"), mr.get("squash_commit_sha"), mr.get("sha"))
                    if isinstance(value, str) and value
                ),
                "",
            )
            iid = mr.get("iid")
            return MergedRemoval(
                number=iid if isinstance(iid, int) else None,
                html_url=str(mr.get("web_url") or ""),
                merge_commit_sha=sha,
            )
        return None

    def can_write(self, actor: str) -> bool:
        raise GitLabApiError("change-request commands are not supported on GitLab")

    def token_user(self) -> str:
        """The username the token acts as, read once."""
        if self._token_user is None:
            response = self._api.get("/user")
            if response.status_code != 200:
                _raise(response, "could not read the user this token acts as")
            username = _json_object(response, "could not read the token's user").get("username")
            if not isinstance(username, str) or not username:
                raise GitLabApiError("the token's user has no username")
            self._token_user = username
        return self._token_user
