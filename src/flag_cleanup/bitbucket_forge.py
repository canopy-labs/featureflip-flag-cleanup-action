"""Bitbucket Cloud as a :class:`~flag_cleanup.forge.Forge`.

REST 2.0, authenticated with a repository access token as a bearer token.
Bitbucket Data Center has a different API and is not this module.

The same rule holds here as for every forge: a lookup that cannot get a
definite answer raises, and never reads as "absent". "Absent" is what reopens
a pull request the customer declined, or archives a flag whose removal never
merged.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from typing import NoReturn
from urllib.parse import quote

import httpx

from flag_cleanup import http_retry
from flag_cleanup.branches import Commit
from flag_cleanup.forge import (
    BaseRefError,
    Comment,
    MergedRemoval,
    ForgeApiError,
    ForgeAuthError,
    HandledPullRequest,
    OpenPullRequest,
    PullRequest,
)
from flag_cleanup.run_context import RunContext

logger = logging.getLogger(__name__)

#: The most list pages one lookup reads.
_MAX_PAGES = 10
_PAGE_LEN = "50"
#: The most commits a removal branch may carry before the ownership guard
#: refuses to judge it, the same bound the other forges have.
_MAX_BRANCH_COMMITS = 250

#: Every state a pull request can be in. The handled lookup asks for all of
#: them: a declined pull request is the customer's "no", never a gap to fill.
_ALL_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")

#: Put at the top of a pull request's description when Bitbucket would not
#: hold it as a draft. The markers make it removable byte for byte.
_BANNER = (
    "<!-- featureflip-cleanup:not-ready -->\n"
    "**Not ready to merge.** This removal is waiting on a review or on other "
    "flags; see the latest flag-cleanup comment.\n"
    "<!-- /featureflip-cleanup:not-ready -->\n\n"
)


def _has_banner(text: str) -> bool:
    return text.startswith(_BANNER)


def _with_banner(text: str) -> str:
    return text if _has_banner(text) else _BANNER + text


def _without_banner(text: str) -> str:
    return text[len(_BANNER):] if _has_banner(text) else text


class BitbucketApiError(ForgeApiError):
    """A Bitbucket API call failed. Never carries the token in its message."""


class BitbucketAuthError(BitbucketApiError, ForgeAuthError):
    """A Bitbucket call was refused with 401/403: a run-level fact, not a bad flag."""


Params = Sequence[tuple[str, str]]


class BitbucketApi:
    """Minimal authenticated transport; returns raw responses for the caller to judge."""

    def __init__(
        self,
        token: str,
        *,
        api_url: str,
        transport: httpx.BaseTransport | None = None,
        sleep: http_retry.Sleep = time.sleep,
    ) -> None:
        self.base_url = api_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=http_retry.TIMEOUT,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: Params | None = None) -> httpx.Response:
        return http_retry.get(
            self._client,
            path,
            params=list(params) if params else None,  # type: ignore[arg-type]
            sleep=self._sleep,
        )

    def post(self, path: str, json: dict) -> httpx.Response:
        return self._client.post(path, json=json)

    def put(self, path: str, json: dict) -> httpx.Response:
        return self._client.put(path, json=json)


def _message(response: httpx.Response) -> str:
    """Bitbucket's own error text, truncated. Never includes request headers."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
        return str(payload)[:200]
    return str(payload)[:200]


def _raise(response: httpx.Response, message: str) -> NoReturn:
    """Raise :class:`BitbucketAuthError` for 401/403, else :class:`BitbucketApiError`."""
    error = BitbucketAuthError if response.status_code in (401, 403) else BitbucketApiError
    raise error(f"{message} (HTTP {response.status_code}): {_message(response)}")


def _json_object(response: httpx.Response, message: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise BitbucketApiError(f"{message}: the response was not an object")
    return payload


def _pages(api: BitbucketApi, path: str, params: Params, message: str) -> list[dict]:
    """Every value of a paginated list, or a raise.

    Bitbucket names the next page by full URL. Only a URL on the API's own
    origin is followed: anything else would send the token somewhere new.
    """
    items: list[dict] = []
    next_path: str | None = path
    next_params: Params | None = [*params, ("pagelen", _PAGE_LEN)]
    for _ in range(_MAX_PAGES):
        response = api.get(next_path, next_params)
        if response.status_code != 200:
            _raise(response, message)
        page = _json_object(response, message)
        values = page.get("values")
        if not isinstance(values, list):
            raise BitbucketApiError(f"{message}: the response carried no list of values")
        items.extend(value for value in values if isinstance(value, dict))
        nxt = page.get("next")
        if not nxt:
            return items
        if not isinstance(nxt, str) or not nxt.startswith(api.base_url + "/"):
            raise BitbucketApiError(f"{message}: the next page is not on {api.base_url}")
        next_path, next_params = nxt[len(api.base_url):], None
    raise BitbucketApiError(f"{message}: more than {_MAX_PAGES} pages, not reading further")


def _link(pr: dict) -> str:
    links = pr.get("links") if isinstance(pr.get("links"), dict) else {}
    html = links.get("html") if isinstance(links.get("html"), dict) else {}
    return str(html.get("href") or "")


def _branch_name(side: object) -> str:
    branch = side.get("branch") if isinstance(side, dict) else None
    return str(branch.get("name") or "") if isinstance(branch, dict) else ""


def _is_draft(pr: dict) -> bool:
    return pr.get("draft") is True or _has_banner(str(pr.get("description") or ""))


def _open_pull(pr: dict) -> OpenPullRequest | None:
    try:
        return OpenPullRequest(
            number=int(pr["id"]),
            node_id="",
            draft=_is_draft(pr),
            author=str(pr["author"]["account_id"]),
            html_url=_link(pr),
        )
    except (KeyError, TypeError, ValueError):
        return None


_EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")


def _email(raw: str) -> str:
    """The address in a ``Name <email>`` author string, or ``""``."""
    match = _EMAIL.search(raw)
    return match.group(1) if match else ""


def _branch_query(branch: str) -> str:
    # Removal branch names come from `branches.removal_branch`, whose alphabet
    # has no double quote, so the value cannot end the BBQL string early.
    if '"' in branch:
        raise BitbucketApiError(f"cannot query a branch name containing a quote: {branch!r}")
    return f'source.branch.name="{branch}"'


class BitbucketForge:
    """:class:`~flag_cleanup.forge.Forge` over the Bitbucket Cloud REST 2.0 API."""

    name = "bitbucket"
    display_name = "Bitbucket"
    supports_draft = True
    #: Bitbucket's documented user name beside a repository access token.
    git_basic_user = "x-token-auth"
    token_refused_hint = (
        "Check that the repository access token has the pullrequest:write and "
        "repository:write scopes"
    )

    def __init__(self, api: BitbucketApi, ctx: RunContext, *, owns_api: bool = False) -> None:
        self._api = api
        self._ctx = ctx
        self._owns_api = owns_api
        self._repo = "/repositories/" + "/".join(
            quote(part, safe="") for part in ctx.repository.split("/")
        )

    def __repr__(self) -> str:
        return f"BitbucketForge(repository={self.repository!r}, api_url={self._ctx.api_url!r})"

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

    # --- is this flag already handled? ---------------------------------------

    def _pull_request_lookup(self, branch: str) -> dict | None:
        """The newest pull request from ``branch`` in ANY state, or ``None``."""
        # The states are asked twice, as parameters and inside the query, so
        # "a declined pull request counts" does not hang on one of them.
        states = " OR ".join(f'state="{state}"' for state in _ALL_STATES)
        response = self._api.get(
            f"{self._repo}/pullrequests",
            [
                ("q", f"{_branch_query(branch)} AND ({states})"),
                *(("state", state) for state in _ALL_STATES),
                ("sort", "-created_on"),
                ("pagelen", "1"),
            ],
        )
        message = (
            f"could not list pull requests for {branch}; refusing to assume "
            "there are none"
        )
        if response.status_code != 200:
            _raise(response, message)
        values = _json_object(response, message).get("values")
        if not isinstance(values, list):
            raise BitbucketApiError(f"{message}: the response carried no list of values")
        if not values:
            return None
        return values[0] if isinstance(values[0], dict) else {}

    def _branch_exists(self, branch: str) -> bool:
        response = self._api.get(f"{self._repo}/refs/branches/{quote(branch, safe='')}")
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
        return isinstance(payload, dict) and payload.get("name") == branch

    def handled(self, branch: str) -> HandledPullRequest:
        pr = self._pull_request_lookup(branch)
        if pr is not None:
            state = pr.get("state")
            url = _link(pr)
            if state == "OPEN":
                logger.info(
                    "pull request #%s from %s is still open — not proposing it again",
                    pr.get("id"),
                    branch,
                )
                return HandledPullRequest(
                    handled=True, open_pr_url=url, open_pull=_open_pull(pr), state="open"
                )
            merged = state == "MERGED"
            logger.info(
                "pull request #%s from %s was %s — not proposing it again, and not "
                "reporting it as a live pull request",
                pr.get("id"),
                branch,
                "merged" if merged else "closed without merging",
            )
            return HandledPullRequest(
                handled=True, state="merged" if merged else "closed", closed_pr_url=url
            )

        if self._branch_exists(branch):
            logger.info(
                "branch %s already exists on %s with no pull request — nothing to "
                "propose or report",
                branch,
                self.repository,
            )
            return HandledPullRequest(handled=True, state="branch")
        return HandledPullRequest(handled=False)

    def existing_change(self, branch: str) -> str | None:
        pr = self._pull_request_lookup(branch)
        if pr is None:
            return None
        logger.info(
            "pull request #%s was already opened from %s (state=%s) — not proposing it again",
            pr.get("id"),
            branch,
            pr.get("state"),
        )
        return _link(pr)

    # --- opening and maintaining pull requests -------------------------------

    def _pr_path(self, number: int) -> str:
        return f"{self._repo}/pullrequests/{number}"

    def _current(self, number: int) -> dict:
        """The pull request as it is now, for an update that must keep a field."""
        response = self._api.get(self._pr_path(number))
        if response.status_code != 200:
            _raise(response, f"could not read pull request #{number}")
        return _json_object(response, f"pull request #{number}")

    def open_change(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
        labels: Sequence[str],
        draft: bool,
    ) -> str:
        """Open a pull request; its URL, or ``""`` when Bitbucket named none.

        Raises only when THIS call created nothing. Past the 201 nothing
        raises: the caller reads a raise as "none was created" and deletes the
        pushed branch, which would orphan the pull request just opened.
        """
        if labels:
            logger.warning(
                "Bitbucket has no pull request labels, so %s will not be applied",
                ", ".join(labels),
            )
        response = self._api.post(
            f"{self._repo}/pullrequests",
            {
                "title": title,
                "description": body,
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": base}},
                "close_source_branch": True,
                "draft": draft,
            },
        )
        if response.status_code != 201:
            _raise(response, f"could not open a pull request for {branch}")

        try:
            pr = response.json()
        except ValueError:
            pr = {}
        if not isinstance(pr, dict):
            pr = {}
        url = _link(pr)
        if url:
            logger.info("opened %s%s", url, " (draft)" if draft else "")
        else:
            logger.warning(
                "opened a pull request for %s but the response carried no link; "
                "the pull request exists — check %s on Bitbucket",
                branch,
                self.repository,
            )
        if draft and not pr.get("id"):
            # Created, but the answer does not say as what: find it, or a stale
            # flag's removal could be left ready to merge without a word.
            pr = self._refind_created(branch)
            url = url or _link(pr)
        if draft and pr and pr.get("draft") is not True:
            self._banner_after_create(pr, body, title)
        return url

    def _refind_created(self, branch: str) -> dict:
        try:
            found = self._pull_request_lookup(branch)
        except Exception as exc:  # noqa: BLE001 - the pull request already exists
            found = None
            logger.warning("could not look up the pull request just opened for %s: %s", branch, exc)
        if not found or found.get("state") != "OPEN":
            logger.warning(
                "opened a pull request for %s as a draft, but could not confirm it is "
                "one; check that it is not marked ready to merge",
                branch,
            )
            return {}
        return found

    def _banner_after_create(self, pr: dict, body: str, title: str) -> None:
        """Mark a pull request Bitbucket would not open as a draft. Never raises."""
        try:
            update = self._api.put(
                self._pr_path(int(pr["id"])),
                {"title": str(pr.get("title") or title), "description": _with_banner(body)},
            )
            if update.status_code != 200:
                _raise(update, "could not add the not-ready banner")
        except Exception as exc:  # noqa: BLE001 - the pull request already exists
            logger.warning(
                "opened %s as a draft request Bitbucket did not honour, and could "
                "not mark it not ready either: %s",
                _link(pr) or "the pull request",
                exc,
            )

    def change(self, number: int) -> PullRequest:
        response = self._api.get(self._pr_path(number))
        message = f"could not read pull request #{number} in {self.repository}"
        if response.status_code != 200:
            _raise(response, message)
        pr = _json_object(response, message)
        source = pr.get("source") if isinstance(pr.get("source"), dict) else {}
        commit = source.get("commit") if isinstance(source.get("commit"), dict) else {}
        head_ref, head_sha = _branch_name(source), commit.get("hash")
        base_ref = _branch_name(pr.get("destination"))
        absent = [
            name
            for name, value in (
                ("source.branch.name", head_ref),
                ("source.commit.hash", head_sha),
                ("destination.branch.name", base_ref),
            )
            if not isinstance(value, str) or not value
        ]
        if absent:
            raise BitbucketApiError(
                f"pull request #{number} in {self.repository} carries no "
                f"{', '.join(absent)}, so it cannot be safely updated"
            )
        state = pr.get("state")
        return PullRequest(
            number=number,
            head_ref=head_ref,
            head_sha=head_sha,
            base_ref=base_ref,
            # A merged change is "closed" with `merged` set, as on every forge.
            state="open" if state == "OPEN" else "closed",
            merged=state == "MERGED",
            draft=_is_draft(pr),
            html_url=_link(pr),
        )

    def update_change(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> None:
        if state is not None:
            raise BitbucketApiError("reopening or declining is not supported on Bitbucket")
        if title is None and body is None:
            return
        changes: dict = {}
        if title is not None:
            changes["title"] = title
        if body is not None:
            # A pull request held by the not-ready banner keeps it through a
            # rewrite of its description; and every update carries a title.
            current = self._current(number)
            changes.setdefault("title", str(current.get("title") or ""))
            held = _has_banner(str(current.get("description") or ""))
            changes["description"] = _with_banner(body) if held else body
        response = self._api.put(self._pr_path(number), changes)
        if response.status_code != 200:
            _raise(
                response,
                f"could not update pull request #{number} in {self.repository} "
                f"({', '.join(sorted(changes))})",
            )

    def set_draft(self, pull: OpenPullRequest, *, draft: bool) -> bool:
        """Hold or release ``pull``. Never raises.

        The ``draft`` field first. Where the answer shows Bitbucket did not
        apply it, the not-ready banner in the description stands in, and a
        release removes the banner whichever way the hold was made.
        """
        try:
            current = self._current(pull.number)
            title = str(current.get("title") or "")
            response = self._api.put(self._pr_path(pull.number), {"title": title, "draft": draft})
            if response.status_code != 200:
                _raise(response, f"could not set the draft state of pull request #{pull.number}")
            pr = _json_object(response, "pull request")
            honoured = pr.get("draft") is draft
            if draft and honoured:
                return True
            description = str(pr.get("description", current.get("description")) or "")
            wanted = _with_banner(description) if draft else _without_banner(description)
            if wanted != description:
                update = self._api.put(
                    self._pr_path(pull.number), {"title": title, "description": wanted}
                )
                if update.status_code != 200:
                    _raise(update, f"could not update pull request #{pull.number}")
            return True
        except Exception as exc:  # noqa: BLE001 - the caller is best effort by design
            logger.warning(
                "could not %s pull request #%s: %s",
                "hold" if draft else "release",
                pull.number,
                exc,
            )
            return False

    def comment(self, number: int, body: str) -> bool:
        response = self._api.post(f"{self._pr_path(number)}/comments", {"content": {"raw": body}})
        if response.status_code != 201:
            logger.warning(
                "could not comment on pull request #%s in %s (HTTP %d): %s",
                number,
                self.repository,
                response.status_code,
                _message(response),
            )
            return False
        return True

    def comments(self, number: int) -> list[Comment] | None:
        """Every comment that is not deleted, oldest first; ``None`` when unreadable.

        A repository access token comments as an app user, and a new token is
        a new one, so an app user's comment counts as the tool's.
        """
        try:
            values = _pages(
                self._api,
                f"{self._pr_path(number)}/comments",
                [("sort", "created_on")],
                f"could not read the comments on pull request #{number}",
            )
        except Exception as exc:  # noqa: BLE001 - "could not tell" is None, never []
            logger.warning(
                "could not read the comments on pull request #%s in %s: %s",
                number,
                self.repository,
                exc,
            )
            return None
        comments: list[Comment] = []
        for value in values:
            if value.get("deleted") is True:
                continue
            user = value.get("user") if isinstance(value.get("user"), dict) else {}
            content = value.get("content") if isinstance(value.get("content"), dict) else {}
            comments.append(
                Comment(
                    author=str(user.get("account_id") or ""),
                    is_bot=user.get("type") == "app_user",
                    created_at=str(value.get("created_on") or ""),
                    body=str(content.get("raw") or ""),
                )
            )
        return comments

    # --- containment and merge evidence --------------------------------------

    def _ref_status(self, ref: str) -> httpx.Response:
        return self._api.get(
            f"{self._repo}/commits/{quote(ref, safe='')}", [("pagelen", "1")]
        )

    def _require_ref(self, ref: str, message: str) -> None:
        """Raise unless ``ref`` names a commit in this repository.

        Asked before every comparison: an empty "commits ahead" list is what
        "contained" looks like, and a ref the repository does not have must
        never be allowed to produce one.
        """
        response = self._ref_status(ref)
        if response.status_code == 404:
            raise BitbucketApiError(f"{message}: {ref!r} is not in {self.repository}")
        if response.status_code != 200:
            _raise(response, message)
        values = _json_object(response, message).get("values")
        if not isinstance(values, list) or not values:
            raise BitbucketApiError(f"{message}: {ref!r} is not in {self.repository}")

    def _ahead(self, include: str, exclude: str, message: str, *, limit: int) -> list[dict]:
        """Commits reachable from ``include`` and not from ``exclude``, up to ``limit``.

        Both refs are resolved first. ``limit=1`` asks a yes/no question in
        one request.
        """
        self._require_ref(include, message)
        self._require_ref(exclude, message)
        params = [("include", include), ("exclude", exclude)]
        if limit == 1:
            response = self._api.get(f"{self._repo}/commits", [*params, ("pagelen", "1")])
            if response.status_code != 200:
                _raise(response, message)
            values = _json_object(response, message).get("values")
            if not isinstance(values, list):
                raise BitbucketApiError(f"{message}: the response carried no list of commits")
            return [value for value in values if isinstance(value, dict)]
        return _pages(self._api, f"{self._repo}/commits", params, message)[:limit]

    def ensure_head_on_base(
        self, base: str, head_sha: str, *, base_is_configured_input: bool = True
    ) -> None:
        message = f"could not compare {head_sha[:12]} against {base}"
        for ref in (base, head_sha):
            response = self._ref_status(ref)
            if response.status_code in (401, 403):
                _raise(response, message)
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
        ahead = self._ahead(head_sha, base, message, limit=1)
        if not ahead:
            return
        raise BaseRefError(
            f"the checkout ({head_sha[:12]}) carries {len(ahead)} commit(s) "
            f"{base!r} does not, so every removal pull request cut from it would "
            "also contain them. Run this from a pipeline on the base branch, not "
            "from a pull request pipeline, or set `base-branch` to the branch this "
            "checkout is on"
        )

    def branch_commits(self, base: str, branch: str) -> tuple[Commit, ...]:
        commits = self._ahead(
            branch,
            base,
            f"could not list the commits on {branch} in {self.repository}; "
            "refusing to assume it carries none of its own",
            limit=_MAX_BRANCH_COMMITS + 1,
        )
        if len(commits) > _MAX_BRANCH_COMMITS:
            raise BitbucketApiError(
                f"{branch} carries too many commits to check safely. A removal "
                "branch should carry one; check what is on it by hand"
            )
        resolved: list[Commit] = []
        for entry in commits:
            author = entry.get("author") if isinstance(entry.get("author"), dict) else {}
            # Bitbucket reports the author only, as "Name <email>"; it stands
            # for the committer too. An absent email reads as foreign.
            email = _email(str(author.get("raw") or ""))
            resolved.append(Commit(sha=str(entry.get("hash") or ""), author_email=email, committer_email=email))
        return tuple(resolved)

    def commit_is_contained(self, commit: str, *, within: str) -> bool:
        missing = self._ahead(
            commit,
            within,
            f"could not tell whether {commit[:12]} is contained in {within}; "
            "refusing to assume it is",
            limit=1,
        )
        return not missing

    def merged_removal(self, branch: str, *, base: str) -> MergedRemoval | None:
        """The pull request from ``branch`` merged into ``base``, or ``None``.

        Only pull requests from this repository itself count: a fork can name
        a branch anything.
        """
        if '"' in base:
            raise BitbucketApiError(f"cannot query a base branch containing a quote: {base!r}")
        prs = _pages(
            self._api,
            f"{self._repo}/pullrequests",
            [
                ("q", f'{_branch_query(branch)} AND destination.branch.name="{base}"'),
                ("state", "MERGED"),
                ("sort", "-created_on"),
            ],
            f"could not list pull requests for {branch}; refusing to assume its "
            "removal never merged",
        )
        own = self.repository.casefold()
        for pr in prs:
            source = pr.get("source") if isinstance(pr.get("source"), dict) else {}
            repo = source.get("repository") if isinstance(source.get("repository"), dict) else {}
            if str(repo.get("full_name") or "").casefold() != own:
                continue
            if _branch_name(pr.get("destination")) != base:
                continue
            merge_commit = pr.get("merge_commit") if isinstance(pr.get("merge_commit"), dict) else {}
            sha = merge_commit.get("hash")
            number = pr.get("id")
            return MergedRemoval(
                number=number if isinstance(number, int) else None,
                html_url=_link(pr),
                merge_commit_sha=sha if isinstance(sha, str) else "",
            )
        return None

    def can_write(self, actor: str) -> bool:
        raise BitbucketApiError("change-request commands are not supported on Bitbucket")

    def default_branch(self) -> str:
        response = self._api.get(self._repo)
        message = (
            f"could not read {self.repository} to find its main branch; set the "
            "`base-branch` input explicitly to skip this lookup"
        )
        if response.status_code in (401, 403):
            # The first call of a run: a refusal here is the token, and naming
            # `base-branch` would send the reader to the wrong setting.
            _raise(
                response,
                f"Bitbucket refused the token reading {self.repository}. "
                f"{self.token_refused_hint}",
            )
        if response.status_code != 200:
            _raise(response, message)
        branch = _branch_name({"branch": _json_object(response, message).get("mainbranch")})
        if not branch:
            raise BitbucketApiError(
                f"{self.repository} reported no main branch; set the `base-branch` "
                "input explicitly"
            )
        logger.info("resolved the base branch for %s to %s", self.repository, branch)
        return branch
