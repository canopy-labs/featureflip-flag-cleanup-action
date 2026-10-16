"""Where a GitHub Actions ``run:`` step runs from.

A step's directory is the first of: the step's ``working-directory``, the
job's ``defaults.run.working-directory``, the workflow's, and the checkout
root. It is unknown (``None``) when the setting holds an expression
(``${{ }}``) or a path no lookup accepts (a NUL byte), or when the ``run:``
block changes directory itself (``cd``, ``pushd``, ``Set-Location``). The
shell is not modelled.

A workflow that does not parse is unreadable: :func:`located_steps` returns
``None`` for it, and the caller publishes every project. If the pack steps
found in ``run:`` blocks do not add up to the pack steps in the file's text
(one sits in a ``with:`` input, a comment, a composite action), no step is
trusted to a directory: every raw-text step comes back with an unknown one.

Only ``.github/workflows/*.yml|*.yaml``. Every other file's directory is
unknown.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from flag_cleanup.csharp_packing import PackStep, pack_steps

_CHANGES_DIRECTORY = re.compile(r"(?:^|[\s;&|(])(?:cd|pushd|popd|Push-Location|Pop-Location|Set-Location|sl|chdir)\b", re.IGNORECASE)


def _is_workflow(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    parts = relative.parts
    return len(parts) == 3 and parts[:2] == (".github", "workflows") and path.suffix.lower() in (".yml", ".yaml")


def _directory(setting: object, root: Path) -> Path | None:
    if not isinstance(setting, str) or "${{" in setting:
        return None
    try:
        return (root / setting).resolve()
    except (ValueError, OSError, RuntimeError):
        # A setting no lookup accepts (a NUL byte), or one that loops.
        return None


def _default_directory(holder: object) -> object:
    if not isinstance(holder, dict):
        return None
    defaults = holder.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return run.get("working-directory") if isinstance(run, dict) else None


def located_steps(path: Path, text: str, root: Path) -> list[tuple[PackStep, Path | None]] | None:
    """Every pack step in ``text`` with the directory it runs from, or ``None``
    in that slot when unknown. ``None`` instead of a list: a workflow that
    cannot be read."""
    raw = pack_steps(text)
    if not _is_workflow(path, root):
        return [(step, None) for step in raw]
    try:
        workflow = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    if not isinstance(workflow, dict):
        return None
    found: list[tuple[PackStep, Path | None]] = []
    workflow_default = _default_directory(workflow)
    jobs = workflow.get("jobs") if isinstance(workflow.get("jobs"), dict) else {}
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        job_default = _default_directory(job)
        steps = job.get("steps")
        for step in steps if isinstance(steps, list) else ():
            if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                continue
            script = step["run"]
            setting = next(
                (s for s in (step.get("working-directory"), job_default, workflow_default) if s is not None),
                ".",
            )
            directory = None if _CHANGES_DIRECTORY.search(script) else _directory(setting, root)
            found.extend((s, directory) for s in pack_steps(script))
    if len(found) != len(raw):
        return [(step, None) for step in raw]
    return found
