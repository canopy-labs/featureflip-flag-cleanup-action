"""GitHub (and GitHub Enterprise Server) as a :class:`~flag_cleanup.forge.Forge`.

A thin adapter: every method is one call into :mod:`flag_cleanup.github_ops`,
which keeps the GitHub API knowledge and its fail-closed reasoning. The
functions are looked up on that module at call time rather than bound here, so
anything that replaces one (a test double, a debugging patch) is still reached.
"""

from __future__ import annotations

from collections.abc import Sequence

from flag_cleanup import github_ops
from flag_cleanup.branches import Commit
from flag_cleanup.forge import (
    Comment,
    HandledPullRequest,
    MergedRemoval,
    OpenPullRequest,
    PullRequest,
)
from flag_cleanup.github_ops import GitHubApi, GitHubEnv


def comment_from_github(raw: dict) -> Comment:
    """One issue-comment JSON object as a :class:`Comment`.

    A comment with no readable ``user`` gets an empty author and is never a
    bot, so it can be trusted by nothing.
    """
    user = raw.get("user")
    if not isinstance(user, dict):
        user = {}
    return Comment(
        author=str(user.get("login") or ""),
        is_bot=user.get("type") == "Bot",
        created_at=str(raw.get("created_at") or ""),
        body=str(raw.get("body") or ""),
    )


class GitHubForge:
    """:class:`~flag_cleanup.forge.Forge` over the GitHub REST and GraphQL APIs."""

    name = "github"
    display_name = "GitHub"
    supports_draft = True
    token_refused_hint = (
        "Check the workflow's permissions (contents: write, pull-requests: write) "
        "and, for pull-request creation, the repository setting 'Allow GitHub "
        "Actions to create and approve pull requests'"
    )
    #: GitHub accepts any user name beside an installation or Actions token;
    #: this is the one its own documentation uses.
    git_basic_user = "x-access-token"

    def __init__(self, api: GitHubApi, env: GitHubEnv, *, owns_api: bool = False) -> None:
        self._api = api
        self._env = env
        self._owns_api = owns_api

    def __repr__(self) -> str:
        return f"GitHubForge(repository={self.repository!r}, api_url={self._env.api_url!r})"

    @property
    def api(self) -> GitHubApi:
        return self._api

    @property
    def env(self) -> GitHubEnv:
        return self._env

    @property
    def repository(self) -> str:
        return self._env.repository

    @property
    def push_url(self) -> str:
        return self._env.push_url

    @property
    def token(self) -> str:
        return self._env.token

    def handled(self, branch: str) -> HandledPullRequest:
        return github_ops.already_handled_pull_request(self._api, self.repository, branch)

    def existing_change(self, branch: str) -> str | None:
        return github_ops.existing_pull_request(self._api, self.repository, branch)

    def open_change(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
        labels: Sequence[str],
        draft: bool,
    ) -> str:
        return github_ops.open_pr(
            self._api, self.repository, branch, base, title, body, labels, draft
        )

    def change(self, number: int) -> PullRequest:
        return github_ops.pull_request(self._api, self.repository, number)

    def update_change(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> None:
        github_ops.update_pull_request(
            self._api, self.repository, number, title=title, body=body, state=state
        )

    def default_branch(self) -> str:
        return github_ops.default_branch(self._api, self.repository)

    def ensure_head_on_base(
        self, base: str, head_sha: str, *, base_is_configured_input: bool = True
    ) -> None:
        github_ops.ensure_head_on_base(
            self._api,
            self.repository,
            base,
            head_sha,
            base_is_configured_input=base_is_configured_input,
        )

    def branch_commits(self, base: str, branch: str) -> tuple[Commit, ...]:
        return github_ops.branch_commits(self._api, self.repository, base, branch)

    def can_write(self, actor: str) -> bool:
        permission = github_ops.actor_permission(self._api, self.repository, actor)
        return permission in github_ops.WRITE_PERMISSIONS

    def comment(self, number: int, body: str) -> bool:
        return github_ops.comment_on_pull_request(self._api, self.repository, number, body)

    def comments(self, number: int) -> list[Comment] | None:
        raw = github_ops.issue_comments(self._api, self.repository, number)
        if raw is None:
            return None
        return [comment_from_github(c) if isinstance(c, dict) else comment_from_github({}) for c in raw]

    def set_draft(self, pull: OpenPullRequest, *, draft: bool) -> bool:
        return github_ops.set_draft(self._api, pull, draft=draft)

    def merged_removal(self, branch: str, *, base: str) -> MergedRemoval | None:
        return github_ops.merged_removal(self._api, self.repository, branch, base=base)

    def commit_is_contained(self, commit: str, *, within: str) -> bool:
        return github_ops.commit_is_contained(self._api, self.repository, commit, within=within)

    def close(self) -> None:
        if self._owns_api:
            self._api.close()


def github_forge(env: GitHubEnv, api: GitHubApi | None = None) -> GitHubForge:
    """A :class:`GitHubForge` for ``env``, building its client unless one is given.

    A client built here is closed with the forge; one passed in belongs to the
    caller.
    """
    if api is not None:
        return GitHubForge(api, env)
    return GitHubForge(GitHubApi(env.token, api_url=env.api_url), env, owns_api=True)
