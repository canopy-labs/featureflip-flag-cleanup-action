"""One spelling for every path a reader sees: relative to the repository root.

A run touches files through several passes, and each pass knows a path in its
own way: as the engine was handed it, relative to the directory the pass
swept, or absolute. Printed as they come, one pull request body named the same
file from two roots, and a dry-run diff carried absolute headers that
``git apply`` refuses. Everything shown to a person (the diff, the pull
request body, the run's report) goes through :func:`shown` instead, so a path
always reads the way ``git`` prints it from the checkout root.
"""

from __future__ import annotations

import functools
import os
import subprocess


def repository_root(directory: str) -> str | None:
    """The root of the git checkout holding ``directory``, or ``None``.

    ``directory`` may name a file (a supported ``directories`` value), or a
    path that does not exist yet; the nearest existing directory is asked. Outside
    a checkout there is no repository root, and :func:`shown` then leaves
    every path as it was given.
    """
    absolute = os.path.abspath(directory)
    while not os.path.isdir(absolute):
        parent = os.path.dirname(absolute)
        if parent == absolute:
            return None
        absolute = parent
    return _toplevel(absolute)


#: Directories already resolved to a root. Only answers are kept: a
#: directory that is not a checkout yet may become one.
_ROOTS: dict[str, str] = {}


def _toplevel(directory: str) -> str | None:
    if directory in _ROOTS:
        return _ROOTS[directory]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    top = result.stdout.strip()
    if not top:
        return None
    _ROOTS[directory] = top
    return top


def shown(path: str, root: str | None) -> str:
    """``path`` relative to ``root``, with ``/`` separators.

    A relative ``path`` is read against the working directory, the way every
    pass opens it. A path outside ``root`` (or no root at all) is returned
    unchanged: rewriting it as ``../…`` would name a file the reader cannot
    find from the checkout either.

    ``git rev-parse`` reports the root with symlinks resolved, so the path's
    directory is compared both as written and resolved. The file name itself
    is never resolved: a symlinked file is shown as the link the run edited.
    """
    if root is None:
        return path
    absolute = os.path.abspath(path)
    parent, name = os.path.split(absolute)
    for base in dict.fromkeys((root, os.path.realpath(root))):
        for candidate in dict.fromkeys((absolute, os.path.join(os.path.realpath(parent), name))):
            relative = os.path.relpath(candidate, base)
            if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
                return relative.replace(os.sep, "/")
    return path


def displayer(directories) -> "functools.partial[str]":
    """:func:`shown` bound to the repository holding ``directories[0]``.

    Every configured directory belongs to the same repository (the preflight
    refuses anything else), so the first one names it.
    """
    directories = tuple(directories)
    root = repository_root(directories[0]) if directories else None
    return functools.partial(shown, root=root)
