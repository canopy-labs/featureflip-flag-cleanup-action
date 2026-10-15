"""Go modules: which import path names a directory, and back.

A Go import path is the ``module`` directive of the nearest ``go.mod`` at or
above a package's directory, joined with the directory's path relative to
that ``go.mod``. Nothing else is read: not ``replace`` directives, a
``go.work`` or ``vendor/``. Each of those can make an import mean either a
directory or a published version, and a proof that picked one would be
guessing, so a caller outside the declaring module is refused instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

#: ``module example.com/app`` or ``module "example.com/app"``, with an
#: optional trailing comment. The parenthesised form is legal and unheard of;
#: it reads as no module, which refuses.
_MODULE = re.compile(r'^[ \t]*module[ \t]+("?)([^\s"]+)\1[ \t]*(?://.*)?$', re.MULTILINE)


@dataclass(frozen=True)
class Module:
    go_mod: Path
    #: The ``module`` directive.
    path: str

    @property
    def root(self) -> Path:
        return self.go_mod.parent

    def import_path(self, directory: Path) -> str | None:
        try:
            rel = directory.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None
        return self.path if rel == "." else f"{self.path}/{rel}"

    def directory(self, import_path: str) -> Path | None:
        """The directory ``import_path`` names inside this module, existing or
        not; ``None`` when the path is not this module's."""
        if import_path == self.path:
            return self.root
        if not import_path.startswith(self.path + "/"):
            return None
        return self.root / import_path[len(self.path) + 1 :]


def module_of(directory: Path, stop: Path) -> Module | None:
    """The module whose nearest ``go.mod`` holds ``directory``, looking no
    higher than ``stop``. An unreadable ``go.mod`` still ends the search: it
    is the module boundary whether or not its path can be read."""
    current = directory.resolve()
    stop = stop.resolve()
    while True:
        candidate = current / "go.mod"
        if candidate.is_file():
            path = _module_path(candidate)
            return None if path is None else Module(candidate, path)
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _module_path(go_mod: Path) -> str | None:
    try:
        text = go_mod.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = [match.group(2) for match in _MODULE.finditer(text)]
    return found[0] if len(found) == 1 else None


def package_clause(root: Node) -> str | None:
    """The name in the file's ``package`` clause."""
    for child in root.named_children:
        if child.type == "package_clause":
            name = next((c for c in child.named_children if c.type == "package_identifier"), None)
            return None if name is None else name.text.decode("utf-8", "replace")
    return None
