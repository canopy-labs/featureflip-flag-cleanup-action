"""Deleting a whole file as part of a transform.

A pass that proves a file holds nothing worth keeping (a type every one of
whose uses this run removed) deletes it. A deletion is a run outcome like any
rewrite, and the rest of the run must treat it as one:

* the file is brought under the transform's undo first
  (:func:`git_ops.before_writing`), so a dry run, a refusal and the next flag
  all get it back byte for byte;
* its summary is a :class:`~flag_cleanup.kotlin_sentinel.Rewrite` with
  ``deleted`` set and empty content. The diff names it as a deletion, a
  rollback re-creates it from ``original_content``, and the pull request
  commits it (``git add --update`` stages the removal of a tracked file);
* the run's listing of the checkout forgets it
  (:func:`source_index.forget`), so a later scan in the same transform never
  meets a listed file it cannot read and refuses something else over it.

Passes that run after a deletion are handed only the live summaries
(:func:`split`): a pass that parsed the empty content and wrote back what it
saw would re-create the file empty.

Deleting a file is a decision the run makes and reports, never one the engine
makes on its own, so this is the only place a file disappears.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from flag_cleanup import git_ops, source_index


def deleted(summary) -> bool:
    """Whether ``summary`` stands for a file this run deleted."""
    return bool(getattr(summary, "deleted", False))


def split(summaries: Iterable) -> tuple[list, list]:
    """``(live, gone)``: the summaries of files still on disk, and the
    deletions, each in the order given."""
    live: list = []
    gone: list = []
    for summary in summaries:
        (gone if deleted(summary) else live).append(summary)
    return live, gone


def delete(summaries: Iterable, paths: Iterable[str], rewrite: Callable) -> list:
    """``summaries`` with each file in ``paths`` deleted from disk and
    described by ``rewrite(path, original, "", deleted=True)``.

    ``original`` is the customer's text: the summary's ``original_content``
    when the run already rewrote the file, else what is on disk now. Paths are
    matched by their resolved form, so a file spelled two ways is deleted
    once.
    """
    summaries = list(summaries)
    targets = list(dict.fromkeys(str(Path(p).resolve()) for p in paths))
    if not targets:
        return summaries
    git_ops.before_writing(targets)
    by_key = {str(Path(s.path).resolve()): i for i, s in enumerate(summaries)}
    for target in targets:
        at = by_key.get(target)
        if at is not None and deleted(summaries[at]):
            continue
        if at is None:
            original = Path(target).read_text(encoding="utf-8")
            spelled = target
        else:
            original = summaries[at].original_content
            spelled = summaries[at].path
        os.remove(target)
        replacement = rewrite(spelled, original, "", deleted=True)
        if at is None:
            summaries.append(replacement)
        else:
            summaries[at] = replacement
    source_index.forget(targets)
    return summaries
