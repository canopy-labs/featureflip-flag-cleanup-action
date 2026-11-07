"""Removal branch names and the commit-ownership guard, on any forge.

A removal branch name is derived from the flag key alone, so every forge sees
the same branch for the same flag, and a key can be read back from a branch.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


#: Frozen by the plan — one PR per flag on ``featureflip/remove-flag/<key>``.
BRANCH_PREFIX = "featureflip/remove-flag/"


# Characters kept verbatim in a branch name. Deliberately excludes ``.``: with
# no dot anywhere in the encoded key, git's dot rules (no leading ``.``, no
# ``..``, no trailing ``.``, no ``.lock`` suffix) are satisfied by construction
# rather than by a pile of special cases that would each be a chance to be
# subtly wrong.
_SAFE_BRANCH_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


_ESCAPE = "="


# Uppercase only: `_encode_char` emits `{byte:02X}`, so accepting lowercase
# here would decode a branch this tool could not have written.
_UPPER_HEX = frozenset("0123456789ABCDEF")


# The bound is on the ENCODED segment because that is what becomes a path
# component: a loose ref is stored at `.git/refs/heads/<...>/<encoded key>`,
# and git writes `<that>.lock` beside it while updating. Most filesystems cap a
# component at 255 bytes, so 240 leaves room for the 5-byte suffix. Counting
# raw characters instead would let a non-ASCII key (6 encoded characters per
# emoji) sail past the real limit and fail inside git.
_MAX_BRANCH_SEGMENT = 240


_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class UnsafeFlagKeyError(ValueError):
    """The flag key cannot be represented as a git branch name.

    Raised rather than guessed at: silently mangling a key risks two flags
    landing on one branch, and pushing a ref git will not accept fails deep in
    a subprocess with a far less obvious message.
    """


def removal_branch(key: str) -> str:
    """Return ``featureflip/remove-flag/<escaped key>``.

    The key is escaped, not sanitized: every character outside
    ``[A-Za-z0-9_-]`` (including the escape character itself) becomes
    ``=<UPPERCASE HEX>`` per UTF-8 byte. That is a bijection on the encoded
    alphabet, so distinct keys always produce distinct branches — a "strip the
    bad characters" scheme would map ``a/b`` and ``a-b`` (or ``a.b``) onto one
    branch and let two flags overwrite each other's PR.

    Ordinary kebab/snake-case keys pass through untouched, so the common case
    stays readable (``old-checkout`` -> ``featureflip/remove-flag/old-checkout``).

    Note that the key is NOT stripped first: ``"a "`` and ``"a"`` are different
    keys and must not share a branch, so the trailing space is escaped like any
    other character.

    Raises :class:`UnsafeFlagKeyError` when the key is blank or would produce a
    ref segment too long to store — skip such a flag loudly rather than guess.
    The limit is on the ENCODED length (see ``_MAX_BRANCH_SEGMENT``), so the
    message reports both numbers: a key well under the limit as written can
    still exceed it once escaped.
    """
    if not key.strip():
        raise UnsafeFlagKeyError(f"flag key {key!r} is blank")
    encoded = "".join(_encode_char(char) for char in key)
    if len(encoded) > _MAX_BRANCH_SEGMENT:
        raise UnsafeFlagKeyError(
            f"flag key {key!r} is {len(key)} character(s) long but escapes to "
            f"{len(encoded)} characters for use as a branch name, over the "
            f"{_MAX_BRANCH_SEGMENT}-character limit. Characters outside "
            "[A-Za-z0-9_-] cost 3 characters per byte once escaped"
        )
    return f"{BRANCH_PREFIX}{encoded}"


def _encode_char(char: str) -> str:
    if char in _SAFE_BRANCH_CHARS:
        return char
    return "".join(f"{_ESCAPE}{byte:02X}" for byte in char.encode("utf-8"))


def flag_key_from_branch(branch: str) -> str | None:
    """The flag key ``branch`` encodes, or ``None`` if it is not one of ours.

    The exact inverse of :func:`removal_branch`, which matters more than it
    looks: that function is a documented BIJECTION on the encoded alphabet, so
    the branch name a removal pull request already carries IS a lossless,
    machine-readable record of which flag it removes. Archive-on-merge
    therefore needs no marker written into the pull-request body at open time,
    and works on every pull request this Action has ever opened rather than
    only on ones opened after the feature shipped.

    ``None`` rather than an exception for the overwhelmingly common case: the
    customer's workflow fires on *every* closed pull request in the repository,
    and almost none of them are ours. A branch that is not a removal branch is
    an ordinary no-op, not an error.

    The scan rejects the malformed shapes — lowercase hex, a truncated escape,
    a character :func:`removal_branch` would have escaped. The re-encode check
    at the end exists for the one thing the scan cannot see: a **non-canonical**
    encoding of a perfectly valid key. ``=6F=6C=64`` decodes to ``old`` under
    any lenient reader, but this Action writes ``old``, so a branch spelled that
    way was made by something else. Without that check a hand-crafted branch
    under this prefix could name any flag it liked, and the value flows straight
    into a call that mutates the customer's flag state. It also catches an
    over-long segment, which :func:`removal_branch` refuses to write.

    Stated positively: this can only ever return a key that WOULD produce
    exactly this branch.
    """
    if not branch.startswith(BRANCH_PREFIX):
        return None
    encoded = branch[len(BRANCH_PREFIX) :]
    if not encoded:
        return None

    decoded = bytearray()
    index = 0
    while index < len(encoded):
        char = encoded[index]
        if char == _ESCAPE:
            pair = encoded[index + 1 : index + 3]
            if len(pair) != 2 or any(digit not in _UPPER_HEX for digit in pair):
                return None
            decoded.append(int(pair, 16))
            index += 3
        elif char in _SAFE_BRANCH_CHARS:
            decoded.append(ord(char))
            index += 1
        else:
            return None

    try:
        key = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None

    # The one check that makes this exact. `removal_branch` refuses a blank or
    # over-long key, and either refusal means the input was never something it
    # could have produced.
    try:
        if removal_branch(key) != branch:
            return None
    except UnsafeFlagKeyError:
        return None
    return key


def short_ref(ref: str) -> str:
    """``ref`` as a report line should print it: a full sha abbreviated, a
    name — ``prod/web``, ``release/2026-10`` — never, since cutting a name
    short can make it read as a different ref."""
    return ref[:12] if _FULL_SHA_RE.match(ref) else ref


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit on a branch, reduced to what the ownership guard reads."""

    sha: str
    author_email: str
    committer_email: str


def foreign_commits(commits: Sequence[Commit], bot_email: str) -> tuple[str, ...]:
    """SHAs of commits not both authored AND committed by ``bot_email``.

    Both fields, because a rebase or an accepted suggestion preserves the
    author and replaces the committer — checking only the author would wave
    through precisely the edit a human is most likely to have made.

    Compared case-insensitively: git preserves the case a commit was made
    with, and an address that differs only in case is the same mailbox.
    """
    expected = bot_email.strip().lower()
    return tuple(
        commit.sha
        for commit in commits
        if commit.author_email.strip().lower() != expected
        or commit.committer_email.strip().lower() != expected
    )
