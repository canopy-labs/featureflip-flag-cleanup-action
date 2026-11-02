"""Members the fold reduced to one value, inlined at their callers when proven.

A member that reads the flag among other things is a constant only once the
fold has run, and often in one treatment only::

    var hideBanner: Bool { !client.boolVariation("k", default: false) }
    var showInvite: Bool { !viewed && client.boolVariation("k", default: false) }

Served off, both become ``{ true }`` / ``{ false }``. Served on, the second is
``!viewed`` and stays a member that computes something. The keyless-wrapper
pass (:mod:`flag_cleanup.wrapper_inline`) runs before the engine and knows only
a member whose whole body IS a read, so it never sees either.
:mod:`flag_cleanup.constant_wrappers` reports them, and that report used to be
the whole treatment.

This pass runs after the engine's first pass. For each member the fold reduced
to ``{ <literal> }`` or ``{ return <literal> }`` (the same members the report
finds, shaped as the language's wrapper profile shapes a wrapper), it asks the
profile the same questions the keyless-wrapper pass asks: the repository index,
qualification (one binding of the name, no override, no protocol requirement,
no attribute, no ``open``/``dynamic``), and the proof of every call site
(receiver bound by its declared type, same module, a value position, plain
arguments, no key path, selector or string naming it). It is all or nothing
per member: one mention that is not a proven call, or a member that may be
called from outside the module, and nothing is marked. The member stays and is
still reported as returning a constant.

A proven member's calls are marked with a sentinel and handed back to the
engine, which folds each to the member's value inside its own cleanup cascade,
exactly as a keyless wrapper's calls are folded. The member is then deleted at
close-out with the keyless wrappers.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from flag_cleanup import constant_wrappers
from flag_cleanup import wrapper_inline as wi

logger = logging.getLogger(__name__)

#: The prefix every sentinel of this tool carries, so a survivor is found by
#: the same scans.
_PREFIX = "__ff_cleanup_"


@dataclass(frozen=True)
class Planned:
    """A member whose every mention is a proven call, and the value it returns."""

    wrapper: wi.Wrapper
    value: bool
    #: Resolved path -> the call spans to mark there.
    spans: dict[str, list] = field(default_factory=dict)


def supported(language: str) -> bool:
    lang = wi.profile(language)
    return lang is not None and lang.constants is not None


def sentinel_for(flag_key: str, value: bool) -> str:
    """The identifier a proven call is marked with, one per value: the engine
    folds every mark of one invocation to one literal."""
    digest = hashlib.sha256(f"{flag_key}\0constant\0{value}".encode()).hexdigest()[:16]
    return f"{_PREFIX}{digest}"


def plan(
    language: str,
    repo_dir: str,
    changed: Mapping[str, tuple[str, str]],
    known: frozenset[str],
    rewritable: Callable[[str], bool],
) -> list[Planned]:
    """The members ``changed`` (path -> the engine's before and after) reduced
    to a constant whose every mention is a proven call.

    ``known`` holds the labels the keyless-wrapper pass already judged: those
    were decided before the engine, on the same proofs. ``rewritable`` says
    whether this run may rewrite a (resolved) file.
    """
    lang = wi.profile(language)
    if lang is None or lang.constants is None:
        return []
    found: list[tuple[wi.Wrapper, bool]] = []
    for path, (before, after) in sorted(changed.items()):
        if not path.endswith(lang.extensions) or before == after:
            continue
        reduced = {
            (f.type_name, f.member): f.value for f in constant_wrappers.reduced_members(path, before, after)
        }
        if not reduced:
            continue
        for wrapper in lang.constants(str(Path(path).resolve()), after):
            value = reduced.get((wrapper.owner, wrapper.name))
            if value is None or wrapper.label in known:
                continue
            found.append((wrapper, value))
    if not found:
        return []
    values = {w.label: v for w, v in found}
    qualified, refusals = wi.qualify_all(repo_dir, [w for w, _ in found])
    for refusal in refusals:
        logger.info(
            "%s returns a constant but is not inlined (%s at %s:%d)",
            refusal.wrapper, refusal.reason, refusal.path, refusal.line,
        )
    planned: list[Planned] = []
    for wrapper, index in qualified:
        proven = wi.prove_all(language, [(wrapper, index)], [], rewritable)
        if wrapper.label not in proven.complete:
            for refusal in proven.findings:
                logger.info(
                    "%s returns a constant but is not inlined (%s at %s:%d)",
                    wrapper.label, refusal.reason, refusal.path, refusal.line,
                )
            continue
        planned.append(Planned(wrapper, values[wrapper.label], dict(proven.spans)))
    return planned
