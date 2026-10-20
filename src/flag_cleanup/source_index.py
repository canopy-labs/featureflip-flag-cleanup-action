"""Every source file in a checkout, listed once per run.

The cross-file key-constant passes all ask the same question of the whole
checkout -- which files could name this constant? -- and each used to answer
it with its own walk: the candidate scan, Gate 2's text index, the resolution's
mention scan, and the Java, Kotlin and Groovy/Scala type indexes, several of
them more than once per flag. This module lists the checkout once and every one
of them filters that listing by suffix.

The listing is the same set of files a walk finds -- ``node_modules`` and a Go
vendor tree pruned, a symlinked directory not followed -- but it comes from
git, and git can say which of them are IGNORED: build output nobody committed
(``dist/``, ``build/generated/``). Such a file is rewritten by the next build
and is never part of a removal commit, so its own mention of a constant must
not keep the declaration alive, and :func:`files` leaves it out unless asked.

It is still listed, because a file's mention is not the only thing it
contributes. A tracked reader can reach a constant through a supertype that
only generated code declares, or meet a competing declaration there, and a
type index or competing-declaration check that could not see it would prove
a read that the build resolves elsewhere. Dropping ignored files there would
let a tracked mention's deletion through. Those callers pass
``with_ignored=True``.

Not ignored, and so counted like any tracked file:

* a tracked file under an ignored directory (``git add -f``);
* an untracked file nothing ignores;
* a file of another repository inside the checkout (a submodule, or an
  unregistered nested clone). It is not this repository's, yet it can still
  read a constant this one declares.

Outside a git checkout, or if git cannot list it, the directory is walked and
nothing is called ignored.

A listing is reused only inside :func:`run_scope`. A run never adds or removes
a file between candidates -- each flag's changes are undone before the next
starts -- so a listing taken at the start holds for the whole run. Outside a
scope every call lists afresh. The one removal a transform makes, deleting a
file it proved empty of anything worth keeping
(:mod:`flag_cleanup.file_deletion`), is announced with :func:`forget`, and
lasts until the innermost :func:`forgetting` scope (one transform) ends. File CONTENTS are never cached here: they
change mid-flag, and each pass reads the text it needs.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceFile:
    #: Spelled under the checkout root, as a walk of it would spell it.
    path: Path
    #: ``str(path.resolve())``, worked out once per listing.
    resolved: str
    #: Untracked and matched by an ignore rule: build output, not source.
    ignored: bool = False


#: Per-run caches, or ``None`` outside :func:`run_scope`.
_listings: dict[str, tuple[SourceFile, ...]] | None = None
#: Resolved paths of the files the running transform deleted, or ``None``
#: outside :func:`forgetting`; see :func:`forget`.
_forgotten: set[str] | None = None
_roots: dict[str, str] | None = None
_links: dict[str, tuple[Path, ...]] | None = None

#: Single-file components: Vue, Svelte and Astro files, and MDX documents,
#: script inside markup that no profile parses. Listed, but only for a caller
#: that asks for them by suffix, so no other scan sees them.
COMPONENT_SUFFIXES = frozenset({".vue", ".svelte", ".astro", ".mdx"})
#: HTML pages, whose inline ``<script>`` can construct a class no parse here
#: reads. ``.html`` is in :func:`all_suffixes` already, for Play templates;
#: ``.htm`` is listed only for a caller that asks for it, as above.
PAGE_SUFFIXES = frozenset({".html", ".htm"})
#: Ruby under a suffix neither the ruby nor the erb language claims: views
#: (``.jbuilder``, ``.jb``, ``.builder``, ``.rabl``, ``.arb``, ``.axlsx`` and
#: ``.prawn`` are Ruby; ``.haml``, ``.slim`` and ``.rhtml`` hold it inside a
#: template) and project files (``config.ru``, a ``.gemspec``, ``.podspec`` or
#: Appraisal ``.gemfile``, a ``.thor`` or ``.rbw`` script). Listed only for a
#: caller that asks for them by suffix, as above: the Ruby key-constant checks.
RUBY_SUFFIXES = frozenset(
    {
        ".jbuilder", ".jb", ".builder", ".rabl", ".arb", ".axlsx", ".prawn",
        ".haml", ".slim", ".rhtml",
        ".gemspec", ".podspec", ".gemfile", ".ru", ".thor", ".rbw",
    }
)
#: Files with no suffix at all: a ``Rakefile``, a ``Gemfile``, a script under
#: ``bin/``, and also every ``Makefile`` and ``LICENSE``. Listed only for a
#: caller that asks, as above, which judges each by its name or first line.
NO_SUFFIX = frozenset({""})


@contextmanager
def run_scope() -> Iterator[None]:
    """Reuse one listing per checkout until the outermost scope exits."""
    global _listings, _roots, _links
    if _listings is not None:
        yield
        return
    _listings, _roots, _links = {}, {}, {}
    try:
        yield
    finally:
        _listings, _roots, _links = None, None, None


@contextmanager
def forgetting() -> Iterator[None]:
    """Scope :func:`forget` to one transform: when it ends the files are
    back on disk (the transform's undo, or the commit's branch switch), so
    every listing holds them again."""
    global _forgotten
    saved = _forgotten
    _forgotten = set(saved or ())
    try:
        yield
    finally:
        _forgotten = saved


def forget(paths: Iterable[str]) -> None:
    """Leave the files at ``paths`` out of every listing until the enclosing
    :func:`forgetting` scope ends: the transform deleted them, and a scan
    that met a listed file it cannot read would refuse over it.

    Outside a scope there is nothing to forget: a listing taken there is
    fresh, and git does not list a file that is gone from disk."""
    if _forgotten is not None:
        _forgotten.update(str(Path(p).resolve()) for p in paths)


#: ``(listing, forgotten, filtered)`` for each listing :func:`_present` has
#: filtered, so a caller keying its work on the listing's identity sees one
#: object for as long as the set of forgotten files stays the same.
_filtered: dict[int, tuple[tuple[SourceFile, ...], frozenset[str], tuple[SourceFile, ...]]] = {}


def _present(listing: tuple[SourceFile, ...]) -> tuple[SourceFile, ...]:
    if not _forgotten:
        return listing
    forgotten = frozenset(_forgotten)
    cached = _filtered.get(id(listing))
    if cached is not None and cached[0] is listing and cached[1] == forgotten:
        return cached[2]
    kept = tuple(f for f in listing if f.resolved not in forgotten)
    _filtered.clear()
    _filtered[id(listing)] = (listing, forgotten, kept)
    return kept


def all_suffixes() -> frozenset[str]:
    """Every suffix the listing keeps: each language's, the guests' included.

    The cross-file resolution's language table (which holds the guests) plus
    every extension the tool knows. By last suffix, as a walk compares
    ``Path.suffix``: ``x.scala.html`` is kept as ``.html``, and the caller
    filters the full extension after.
    """
    from flag_cleanup import cross_file_keys
    from flag_cleanup.cross_file_jvm_guest import walk_suffixes
    from flag_cleanup.piranha_runner import KNOWN_EXTENSIONS

    cross_file_keys._extensions("")  # populate the language table unless already set
    suffixes = set(KNOWN_EXTENSIONS)
    for extensions in cross_file_keys._EXTENSIONS_BY_LANGUAGE.values():
        suffixes |= walk_suffixes(extensions)
    return frozenset(suffixes)


def checkout_root(repo_dir: str) -> str:
    """:func:`piranha_runner.checkout_root`, asked of git once per run."""
    from flag_cleanup import piranha_runner

    if _roots is None:
        return piranha_runner.checkout_root(repo_dir)
    root = _roots.get(repo_dir)
    if root is None:
        root = _roots[repo_dir] = piranha_runner.checkout_root(repo_dir)
    return root


def files(
    repo_dir: str,
    suffixes: Iterable[str] | None = None,
    *,
    with_ignored: bool = False,
) -> list[SourceFile]:
    """The source files of ``repo_dir``'s whole checkout, sorted by path.

    ``suffixes`` narrows it by last suffix; ``None`` keeps every one in
    :func:`all_suffixes`. :data:`COMPONENT_SUFFIXES` are listed only when
    asked for, :data:`PAGE_SUFFIXES`, :data:`RUBY_SUFFIXES` and
    :data:`NO_SUFFIX` likewise, and asking for any other raises
    ``ValueError``, since the listing cannot hold it. Ignored files are left
    out unless ``with_ignored`` -- see the module docstring for which callers
    need them.
    """
    root = checkout_root(repo_dir)
    listing = _listings.get(root) if _listings is not None else None
    if listing is None:
        listing = _list(root)
        if _listings is not None:
            _listings[root] = listing
    known = _listed_suffixes()
    wanted = all_suffixes() if suffixes is None else frozenset(suffixes)
    if not wanted <= known:
        raise ValueError(f"not in the source listing: {sorted(wanted - known)}")
    return [
        f
        for f in _present(listing)
        if f.path.suffix in wanted and (with_ignored or not f.ignored)
    ]


def _listed_suffixes() -> frozenset[str]:
    """Every suffix the listing holds, the ones listed only on request included."""
    return all_suffixes() | COMPONENT_SUFFIXES | PAGE_SUFFIXES | RUBY_SUFFIXES | NO_SUFFIX


def run_listing(repo_dir: str) -> tuple[SourceFile, ...] | None:
    """The run's whole listing of ``repo_dir``'s checkout, every suffix and
    ignored files included, inside :func:`run_scope`; None outside one.

    The same object for the whole run and a new one for the next, so a
    caller can key what it works out from the listing on its identity. A
    transform that deletes a file (:func:`forget`) gets another object until
    it ends, without the file.
    """
    if _listings is None:
        return None
    root = checkout_root(repo_dir)
    listing = _listings.get(root)
    if listing is None:
        listing = _listings[root] = _list(root)
    return _present(listing)


def _list(root: str) -> tuple[SourceFile, ...]:
    from flag_cleanup.piranha_runner import _SKIP_DIRS, _is_go_vendor_tree, source_files

    suffixes = _listed_suffixes()
    base = Path(root)
    resolved_base = base.resolve()
    try:
        staged = _ls_files(root, "--cached", "--stage")
        others = _ls_files(root, "--others", "--exclude-standard")
        ignored = _ls_files(root, "--others", "--ignored", "--exclude-standard", "--directory")
    except (OSError, subprocess.CalledProcessError):
        return tuple(_walked(source_files(base, suffixes, _SKIP_DIRS), base, resolved_base))

    # `True` for an ignored entry. A directory -- a submodule's gitlink, an
    # unregistered repository (`dir/`), or a wholly ignored one -- is walked.
    entries: dict[str, bool] = {}
    directories: dict[str, bool] = {}
    for entry in staged:
        meta, _, rel = entry.partition("\t")
        (directories if meta.startswith("160000 ") else entries)[rel] = False
    for rel in others:
        (directories if rel.endswith("/") else entries)[rel.rstrip("/")] = False
    for rel in ignored:
        target = directories if rel.endswith("/") else entries
        target.setdefault(rel.rstrip("/"), True)

    vendor: dict[Path, bool] = {}

    def pruned(rel: Path) -> bool:
        """Whether a walk would have skipped ``rel``, a directory below the root.

        The root itself is never a candidate: a walk only prunes what it
        finds inside the directory it starts from.
        """
        if not rel.parts:
            return False
        if any(part in _SKIP_DIRS for part in rel.parts):
            return True
        for parent in (rel, *rel.parents[:-1]):
            if parent not in vendor:
                vendor[parent] = _is_go_vendor_tree(base / parent)
            if vendor[parent]:
                return True
        return False

    found: dict[Path, SourceFile] = {}
    for rel, is_ignored in entries.items():
        relative = Path(rel)
        if relative.suffix not in suffixes or pruned(relative.parent):
            continue
        path = base / relative
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue  # tracked, but no longer on disk
        if stat.S_ISREG(mode):
            # Nothing git lists sits below a symlink, so only the file itself
            # can be one, and a plain file needs no resolving.
            found[path] = SourceFile(path, str(resolved_base / relative), is_ignored)
        elif stat.S_ISLNK(mode) and not path.is_dir():
            found[path] = SourceFile(path, str(path.resolve()), is_ignored)
    for rel, is_ignored in directories.items():
        relative = Path(rel)
        if pruned(relative):
            continue
        walked = source_files(base / relative, suffixes, _SKIP_DIRS)
        for source in _walked(walked, base, resolved_base, is_ignored):
            # A file git listed on its own -- tracked inside an ignored
            # directory -- keeps what git said about it.
            found.setdefault(source.path, source)
    return tuple(found[path] for path in sorted(found))


def symlinks(repo_dir: str) -> tuple[Path, ...]:
    """Every symlink in ``repo_dir``'s checkout, a directory's included.

    :func:`files` keeps only source suffixes and never follows a symlinked
    directory, so a link to a directory (``lnk -> flags``) is listed only
    here: tracked ones by their index mode, untracked ones by what they are
    on disk, ignored ones included (a link a setup script creates and
    ignores). A directory git names whole -- a wholly ignored one, a
    submodule, an unregistered nested repository -- is walked for links, as
    :func:`files` walks it for source, since a link a setup script makes
    INSIDE an ignored directory is otherwise never seen. The walk skips what
    :func:`files` skips and follows no link. Outside a git checkout there
    are none.
    """
    from flag_cleanup.piranha_runner import _SKIP_DIRS

    root = checkout_root(repo_dir)
    cached = _links.get(root) if _links is not None else None
    if cached is not None:
        return cached
    base = Path(root)
    try:
        staged = _ls_files(root, "--cached", "--stage")
        others = _ls_files(root, "--others", "--exclude-standard")
        ignored = _ls_files(root, "--others", "--ignored", "--exclude-standard", "--directory")
    except (OSError, subprocess.CalledProcessError):
        return ()
    found: list[str] = []
    directories: list[str] = []
    for meta, _, rel in (e.partition("\t") for e in staged):
        if meta.startswith("120000 "):
            found.append(rel)
        elif meta.startswith("160000 "):
            directories.append(rel)
    for rel in (*others, *ignored):
        if (base / rel.rstrip("/")).is_symlink():
            found.append(rel)
        elif rel.endswith("/"):
            directories.append(rel)
    for rel in directories:
        if not any(part in _SKIP_DIRS for part in Path(rel).parts):
            found += [str(link.relative_to(base)) for link in _links_below(base / rel.rstrip("/"), _SKIP_DIRS)]
    links = tuple(
        base / rel.rstrip("/")
        for rel in sorted(set(found))
        if not any(part in _SKIP_DIRS for part in Path(rel).parts)
    )
    if _links is not None:
        _links[root] = links
    return links


def _links_below(directory: Path, skip_dirs: frozenset[str]) -> list[Path]:
    """Every symlink under ``directory``, ``skip_dirs`` pruned, none followed.

    ``os.scandir`` rather than ``os.walk``: its entries already know whether
    they are links, so only the directories cost a call.
    """
    found: list[Path] = []
    pending = [directory]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        found.append(Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False) and entry.name not in skip_dirs:
                        pending.append(Path(entry.path))
        except OSError:
            continue
    return found


def _ls_files(root: str, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *args],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return [os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry]


def _walked(
    paths: Iterable[Path], base: Path, resolved_base: Path, ignored: bool = False
) -> list[SourceFile]:
    """``paths`` from a walk of ``base``, resolved.

    A walk never follows a symlinked directory, so only a file that is itself
    a link needs resolving through the filesystem; any other is ``base``'s
    resolution plus the rest of its path. Spliced as strings, because
    ``Path.relative_to`` cost most of a large checkout's listing.
    """
    prefix, resolved_prefix = str(base), str(resolved_base)
    found: list[SourceFile] = []
    for path in paths:
        text = str(path)
        if path.is_symlink() or not text.startswith(prefix):
            resolved = str(path.resolve())
        else:
            resolved = resolved_prefix + text[len(prefix):]
        found.append(SourceFile(path, resolved, ignored))
    return found
