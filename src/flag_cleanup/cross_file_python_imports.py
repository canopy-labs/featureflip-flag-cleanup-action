"""Which file a Python import means: unique-in-checkout resolution, and what
else Python could load in its place (see :mod:`flag_cleanup.cross_file_python`)."""

from __future__ import annotations

from pathlib import Path

from flag_cleanup.cross_file_python_syntax import (
    _Module,
)


def _python_files(repo_dir: str) -> tuple[Path, ...]:
    from flag_cleanup import source_index

    # Ignored files included: a second package of the same dotted path in
    # build output can still be the one an interpreter finds first.
    return tuple(
        Path(s.resolved) for s in source_index.files(repo_dir, (".py",), with_ignored=True)
    )


class _RunMemo:
    """What import resolution works out from the file system, kept for one run.

    A run never adds or removes a file (see :mod:`~flag_cleanup.source_index`),
    so which files a module can mean, and what Python would load beside a
    file, hold for the whole of it; file CONTENTS are never kept here. Keyed
    on the identity of the run's listing, so the next run starts afresh.
    Outside a run nothing is kept.
    """

    def __init__(self, listing: tuple) -> None:
        self.listing = listing
        #: Every ``.py`` file of the checkout, ignored ones included, by name.
        self.by_name: dict[str, list[Path]] = {}
        for source in listing:
            if source.path.suffix == ".py":
                resolved = Path(source.resolved)
                self.by_name.setdefault(resolved.name, []).append(resolved)
        self.resolved: dict[tuple, tuple[str, ...]] = {}
        self.loads_other: dict[Path, bool] = {}


_memo: _RunMemo | None = None


def _run_memo(repo_dir: str) -> _RunMemo | None:
    global _memo
    from flag_cleanup import source_index

    listing = source_index.run_listing(repo_dir)
    if listing is None:
        return None
    if _memo is None or _memo.listing is not listing:
        _memo = _RunMemo(listing)
    return _memo


def _resolve(repo_dir: str, reader: str, module: _Module) -> tuple[str, ...]:
    """Every file in the checkout ``module`` can mean, read from ``reader``.

    Relative: from the reader's directory, one level up per extra dot. Absolute:
    any file whose path ENDS in the dotted path, under whatever root -- a src
    layout and a test directory beside it both resolve without reading any
    packaging config. More than one answer is the caller's refusal.
    """
    memo = _run_memo(repo_dir)
    if memo is None:
        return _resolved(repo_dir, reader, module, None)
    key = (str(Path(reader).resolve().parent) if module.level else None, module)
    found = memo.resolved.get(key)
    if found is None:
        found = memo.resolved[key] = _resolved(repo_dir, reader, module, memo)
    return found


def _resolved(repo_dir: str, reader: str, module: _Module, memo: _RunMemo | None) -> tuple[str, ...]:
    tails = [
        (*module.parts[:-1], module.parts[-1] + ".py") if module.parts else ("__init__.py",),
        (*module.parts, "__init__.py"),
    ]
    if module.level:
        base = Path(reader).resolve().parent
        for _ in range(module.level - 1):
            base = base.parent
        checkout = Path(xk_checkout_root(repo_dir))
        if base != checkout and checkout not in base.parents:
            return ()
        found = {base.joinpath(*tail) for tail in tails if base.joinpath(*tail).is_file()}
        return tuple(sorted(str(p.resolve()) for p in found))
    if not module.parts:
        return ()
    if memo is None:
        candidates = {tail: _python_files(repo_dir) for tail in tails}
    else:
        candidates = {tail: memo.by_name.get(tail[-1], ()) for tail in tails}
    found = {
        str(path)
        for tail, paths in candidates.items()
        for path in paths
        if path.parts[-len(tail):] == tail
    }
    return tuple(sorted(found))


def xk_checkout_root(repo_dir: str) -> str:
    """The checkout's root, resolved like every path it is compared with:
    outside a git checkout it is ``repo_dir`` as given, a link or relative."""
    from flag_cleanup import source_index

    return str(Path(source_index.checkout_root(repo_dir)).resolve())


def _loads_other_code(path: Path, repo_dir: str | None = None) -> bool:
    """:func:`_loaded_otherwise`, kept for the run when ``repo_dir`` names one."""
    memo = _run_memo(repo_dir) if repo_dir is not None else None
    if memo is None:
        return _loaded_otherwise(path)
    found = memo.loads_other.get(path)
    if found is None:
        found = memo.loads_other[path] = _loaded_otherwise(path)
    return found


def _loaded_otherwise(path: Path) -> bool:
    """Whether Python can load something other than the source at ``path`` for its module.

    Python's file finder tries an extension module (``keys.so``,
    ``keys.cpython-312-x86_64-linux-gnu.so``, ``keys.pyd``,
    ``keys.cp312-win_amd64.pyd``) before a source of the same name, and a
    package directory of that name before either. And a cached
    ``__pycache__/keys.*.pyc`` compiled as an unchecked hash runs instead of
    the source without ever comparing the two.
    """
    stem = path.stem
    try:
        cache = path.parent / "__pycache__"
        if cache.is_dir():
            for pyc in cache.iterdir():
                if pyc.name.startswith(stem + ".") and pyc.name.endswith(".pyc"):
                    with pyc.open("rb") as handle:
                        flags = int.from_bytes(handle.read(8)[4:8], "little")
                    if flags & 0b01 and not flags & 0b10:
                        return True
        for sibling in path.parent.iterdir():
            name = sibling.name
            if name.startswith(stem + ".") and name.endswith((".so", ".pyd")):
                return True
        package = path.with_suffix("")
        if path.name != "__init__.py" and package.is_dir():
            for inside in package.iterdir():
                if inside.name.startswith("__init__.") and inside.suffix not in (".py", ".pyi"):
                    return True
    except OSError:
        return True
    return False


def _compiled_init(path: Path, repo_dir: str | None = None) -> bool:
    """Whether something other than the source at ``path`` (a package
    ``__init__.py``) can initialise the package: a compiled module of the
    same name loads before the source, and stands in for a missing one."""
    if any(p.suffix not in (".py", ".pyi") for p in path.parent.glob(f"{path.stem}.*")):
        return True
    return path.is_file() and _loads_other_code(path, repo_dir)


def _module_dir(path: str) -> Path:
    """The directory a module's dotted path names: ``a/b/keys`` for ``a/b/keys.py``."""
    file = Path(path)
    return file.parent if file.name == "__init__.py" else file.with_suffix("")
