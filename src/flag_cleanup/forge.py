"""What every forge this tool opens change requests on has in common.

A forge is where the repository is hosted and where the removal change request
opens. The rewriting never depends on it; only the calls that look up, open and
update change requests do, and those go through :class:`Forge`.

The error bases here are what callers catch. Each forge raises its own
subclasses, so a message can name the forge while the handling stays shared.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from flag_cleanup.branches import Commit


class ForgeConfigError(ValueError):
    """The run's forge settings are missing, malformed or unsupported."""


class ForgeApiError(RuntimeError):
    """A forge API call failed. Never carries the token in its message."""


class ForgeAuthError(ForgeApiError):
    """A forge call was refused as unauthorized: a run-level fact, not a bad flag."""


class BaseRefError(RuntimeError):
    """The checkout cannot safely be the source of a removal branch.

    Raised before any candidate is processed, so nothing has been modified.
    """


@dataclass(frozen=True, slots=True)
class OpenPullRequest:
    """An open removal change request, as much of it as holding it needs.

    ``node_id`` is an opaque handle the forge may need to address it by (on
    GitHub, the GraphQL id, because REST cannot toggle draft); empty where a
    forge has no use for one. ``author`` is whose comments are trusted to say
    whether it is on hold.
    """

    number: int
    node_id: str
    draft: bool
    author: str
    html_url: str


@dataclass(frozen=True, slots=True)
class HandledPullRequest:
    """The idempotency answer :meth:`Forge.handled` gives — with a link, when
    there is a safe one to give.

    ``open_pr_url`` names a still-OPEN pull request from this branch, so the
    caller can report it to Featureflip again. It is deliberately empty in the
    other two ways ``handled`` can be true: a CLOSED pull request (the
    customer's "no thanks" — reporting it would look like a live PR) and a
    branch that exists with no pull request ever opened from it (nothing to
    link). ``handled`` is false only when neither the branch nor any pull
    request exists.

    ``open_pull`` carries what holding or releasing the pull request needs;
    ``None`` whenever ``open_pr_url`` is empty, or the forge's answer lacked
    one of those fields.

    ``state`` says which of those it was: ``"open"``, ``"merged"``, ``"closed"``
    (closed without merging), ``"branch"`` (a branch with no pull request), or
    empty when ``handled`` is false. A merged removal and a declined one can
    look alike in a forge's own API and are opposite facts to the customer:
    one is waiting to be archived, the other was turned down.

    ``closed_pr_url`` names a merged or closed pull request, for the run's
    report only. It is a separate field from ``open_pr_url`` so that nothing
    that links pull requests in Featureflip can pick it up by mistake.
    """

    handled: bool
    open_pr_url: str = ""
    open_pull: OpenPullRequest | None = None
    state: str = ""
    closed_pr_url: str = ""


@dataclass(frozen=True, slots=True)
class MergedRemoval:
    """The merged pull request a removal branch produced.

    ``number`` is ``None`` when the response did not carry one — the pull
    request still merged, and that is the fact the caller acts on, so the
    missing number degrades to an unnamed reference rather than discarding the
    evidence (same reasoning as ``pr_event.MergedPullRequest``'s ``number=0``).

    ``merge_commit_sha`` degrades to ``""`` for the same reason, and the
    consequence is stated here because it is not obvious from the field: it is
    what ``mode: archive-on-deploy`` compares against the deployed commit, so
    an empty one means that mode cannot establish the removal is live and
    leaves the flag outstanding. ``archive-on-merge`` and ``archive-sweep``
    never read it, so they are unaffected.
    """

    number: int | None
    html_url: str
    merge_commit_sha: str = ""


@dataclass(frozen=True, slots=True)
class PullRequest:
    """One pull request, as much of it as this tool acts on.

    Read in a single ``GET``: the head branch names the flag (through the
    :func:`removal_branch` bijection), ``head_sha`` is the value the
    force-push leases against, and ``state``/``merged``/``draft`` decide what
    a re-run may do to it.

    ``state`` is ``"open"`` or ``"closed"`` on every forge, translated from the
    forge's own words; a merged change is ``"closed"`` with ``merged`` set.
    :meth:`Forge.update_change` takes the same two values.
    """

    number: int
    head_ref: str
    head_sha: str
    base_ref: str
    state: str
    merged: bool
    draft: bool
    html_url: str


@dataclass(frozen=True, slots=True)
class Comment:
    """One comment on a change request, reduced to what a hold reads.

    ``author`` is the forge's stable identity for the commenter (a login, a
    username or an account id), compared only with the change request's own
    author. ``is_bot`` is the forge's own statement that an app or bot account
    wrote it, never inferred from a name.
    """

    author: str
    is_bot: bool
    created_at: str
    body: str


class Forge(Protocol):
    """Everything the run asks of the forge it opens change requests on.

    Implementations hold their own authenticated client and the repository's
    path, so callers pass only what changes per call. Every lookup that cannot
    get a definite answer raises :class:`ForgeApiError`; none of them reads an
    error as "absent", because "absent" is what reopens a change the customer
    closed.
    """

    #: ``"github"``, ``"gitlab"`` or ``"bitbucket"``.
    name: str
    #: The repository's path on the forge, e.g. ``owner/name``.
    repository: str
    #: The forge's name as a sentence spells it, e.g. ``GitHub``.
    display_name: str
    #: What to check when the forge refuses the token outright, one sentence.
    token_refused_hint: str
    #: Whether a change request can be held as a draft.
    supports_draft: bool
    #: The basic-auth user name git pushes with, beside the token.
    git_basic_user: str
    #: The push remote. Contains no credential.
    push_url: str
    #: The forge token. Implementations keep it out of ``repr``.
    token: str

    def handled(self, branch: str) -> HandledPullRequest:
        """Is there a branch or a change request in ANY state for ``branch``?"""

    def existing_change(self, branch: str) -> str | None:
        """The URL of a change request from ``branch`` in any state, if any."""

    def open_change(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
        labels: Sequence[str],
        draft: bool,
    ) -> str:
        """Open a change request from ``branch`` into ``base``; its URL."""

    def change(self, number: int) -> PullRequest: ...

    def update_change(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> None: ...

    def default_branch(self) -> str: ...

    def ensure_head_on_base(
        self, base: str, head_sha: str, *, base_is_configured_input: bool = True
    ) -> None:
        """Raise :class:`BaseRefError` unless ``head_sha`` is on ``base``."""

    def branch_commits(self, base: str, branch: str) -> tuple[Commit, ...]: ...

    def can_write(self, actor: str) -> bool:
        """May ``actor`` run a change-request command on this repository?"""

    def comment(self, number: int, body: str) -> bool: ...

    def comments(self, number: int) -> list[Comment] | None:
        """Every comment on the change request, oldest first; ``None`` if unreadable."""

    def set_draft(self, pull: OpenPullRequest, *, draft: bool) -> bool:
        """Convert to or from a draft; ``False`` when the forge refused this time."""

    def merged_removal(self, branch: str, *, base: str) -> MergedRemoval | None: ...

    def commit_is_contained(self, commit: str, *, within: str) -> bool: ...

    def close(self) -> None: ...
