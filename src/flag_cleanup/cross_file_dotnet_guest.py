"""Files outside C# that can still read a C# constant.

A Razor view compiles to C#, a XAML file binds a constant with
``{x:Static ns:Owner.NAME}``, and a VB or F# project can reference a C# one.
This tool transforms none of them and parses none of them, so a mention in
one is never proven: this judge only refuses, by tokens, failing closed. The
cost of a wrong guess is one constant left in place, named in the pull
request.

MSBuild files are listed alongside them for a different reason: a
``<Using Include="..." />`` item in a project file is a ``global using`` for
every file of that project, and the C# profile has to see it.
"""

from __future__ import annotations

import re
from pathlib import Path

from flag_cleanup import cross_file_keys as xk

#: Project files: read for their ``<Using>`` items, never judged as readers.
MSBUILD_SUFFIXES: tuple[str, ...] = (".csproj", ".props", ".targets")

#: The suffixes listed for the cross-file scan. ``dotnet_guest`` files are
#: judged by :func:`references`; ``msbuild`` files have no profile, so the scan
#: lists them and the resolve loop skips them.
DOTNET_GUEST_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "dotnet_guest": (
        ".cshtml", ".razor", ".xaml", ".axaml", ".vb", ".fs", ".fsx", ".fsi",
        # Web Forms pages and controls (compiled at run time, so a broken
        # reference reaches production rather than the build), VB Razor, C#
        # scripts, Cake build scripts and T4 templates.
        ".aspx", ".ascx", ".master", ".asax", ".vbhtml", ".csx", ".cake", ".tt",
    ),
    "msbuild": MSBUILD_SUFFIXES,
}

#: Files that import into every Razor file below them.
_RAZOR_IMPORTS = ("_ViewImports.cshtml", "_Imports.razor")
_RAZOR = (".cshtml", ".razor")
#: Files that refuse on any spelling of the name: the import that brings the
#: owner in can live where this cannot read it (a VB or F# project file, a
#: Web Forms page's `web.config` namespaces, a script's `#load`, a template).
_ANY_SPELLING = (
    ".vb", ".fs", ".fsx", ".fsi", ".aspx", ".ascx", ".master", ".asax", ".vbhtml",
    ".csx", ".cake", ".tt",
)
#: VB is case-insensitive, so `FEATUREKEYS.NEWCHECKOUT` reads the constant.
CASE_INSENSITIVE_SUFFIXES = (".vb", ".vbhtml")


def _pattern(names, case_insensitive: bool) -> re.Pattern[str]:
    base = xk.mention_pattern(names)
    return re.compile(base.pattern, re.IGNORECASE) if case_insensitive else base


def _owners(decl: xk.KeyDeclaration, repo_dir: str) -> set[str]:
    """Names through which a file can reach ``decl``: the owner chain's first
    and last names, and every repository C# type that may extend the owner."""
    from flag_cleanup import cross_file_csharp

    owner = decl.owner or ""
    owner_last = owner.rsplit(".", 1)[-1]
    names = {owner.split(".")[0], owner_last}
    index = cross_file_csharp.csharp_index(repo_dir)
    names |= {
        name for name in index.by_last if name != owner_last and index.reaches([name], owner_last)
    }
    # A global or project alias of the owner reaches it under another name.
    full = f"{decl.module}.{owner}" if decl.module else owner
    project, _readable = cross_file_csharp.msbuild_usings(repo_dir)
    names |= {
        u.alias
        for u in index.global_usings + project
        if u.kind == "alias" and u.alias and (u.target is None or u.target == full)
    }
    return {n for n in names if n}


def _razor_imports_spell(path: str, owners: set[str], repo_dir: str) -> bool:
    """Whether an imports file in ``path``'s directory or above, up to the
    checkout root, spells one of ``owners``. An unreadable one counts."""
    from flag_cleanup.source_index import checkout_root

    pattern = _pattern(owners, False)
    root = Path(checkout_root(repo_dir)).resolve()
    current = Path(path).resolve().parent
    while True:
        for name in _RAZOR_IMPORTS:
            candidate = current / name
            try:
                if candidate.is_file() and pattern.search(candidate.read_text(encoding="utf-8")):
                    return True
            except (OSError, UnicodeDecodeError):
                return True
        if current == root or current.parent == current:
            return False
        current = current.parent


def _project_static(decl: xk.KeyDeclaration, repo_dir: str) -> bool:
    """Whether a global or project ``using static`` may name the owner."""
    from flag_cleanup import cross_file_csharp

    full = f"{decl.module}.{decl.owner}" if decl.module else str(decl.owner)
    project, readable = cross_file_csharp.msbuild_usings(repo_dir)
    if not readable:
        return True
    usings = cross_file_csharp.csharp_index(repo_dir).global_usings + project
    return any(
        u.kind == "static"
        and (u.target is None or full == u.target or full.endswith("." + u.target))
        for u in usings
    )


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    """Never a read; a refusal at the first line that may name a constant.

    A VB or F# file refuses on any spelling of the name (VB case-insensitively):
    the import that would bring the owner in can sit in a project file. A
    Razor or XAML file refuses when it also spells the owner (or a repository
    type that may extend it), and a Razor file when an imports file above it,
    or a global or project ``using static``, may bring the owner in.
    """
    refused: list[xk.Refusal] = []
    case_insensitive = path.lower().endswith(CASE_INSENSITIVE_SUFFIXES)
    lines = source.splitlines()
    for decl in declarations:
        hit = _pattern([decl.name], case_insensitive).search(source)
        if hit is None:
            continue
        if not path.endswith(_ANY_SPELLING):
            owners = _owners(decl, repo_dir)
            spelled = _pattern(owners, case_insensitive).search(source) is not None
            imported = path.endswith(_RAZOR) and (
                _razor_imports_spell(path, owners, repo_dir) or _project_static(decl, repo_dir)
            )
            if not (spelled or imported):
                continue
        line = source.count("\n", 0, hit.start()) + 1
        text = lines[line - 1].strip() if line <= len(lines) else ""
        refused.append(xk.Refusal(path, line, xk.UNSUPPORTED_FORM, text))
    return [], refused


#: A member access after a name: ``FeatureKeys.Other`` reads ``Other`` alone.
_MEMBER_AFTER = r"(?!\s*\.\s*[^\W\d])"


def constant_escapes(
    path: str, source: str, constants: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[tuple[str, xk.Refusal]]:
    """``(declaring file, refusal)`` at each spelling in ``path`` of a type that
    takes the owner of one of ``constants`` whole, from which reflection reads
    any constant without spelling one: the owner, a type of its chain, a
    repository type deriving from it, or a global or project alias of one
    (:func:`cross_file_csharp.whole_owner_names`).

    Judged by tokens, as :func:`references` is: any spelling not followed by a
    member access counts (``typeof(FeatureKeys)``, VB's
    ``GetType(FeatureKeys)``, F#'s ``typeof<FeatureKeys>``, XAML's
    ``{x:Type local:FeatureKeys}``, ``"Shop.Flags.FeatureKeys"``), so a file
    that only reads another constant (``FeatureKeys.Other``) does not. A
    comment or an unrelated type of the same name counts too: the cost is a
    declaration kept. VB is matched in any case."""
    from flag_cleanup import cross_file_csharp

    names = cross_file_csharp.whole_owner_names(constants, repo_dir)
    if not names:
        return []
    flags = re.IGNORECASE if path.lower().endswith(CASE_INSENSITIVE_SUFFIXES) else 0
    pattern = re.compile(xk.mention_pattern(names).pattern + _MEMBER_AFTER, flags)
    folded: dict[str, set[str]] = names
    if flags:
        folded = {}
        for name, held in names.items():
            folded.setdefault(name.lower(), set()).update(held)
    lines = source.splitlines()
    found: set[tuple[str, xk.Refusal]] = set()
    for hit in pattern.finditer(source):
        line = source.count("\n", 0, hit.start()) + 1
        text = lines[line - 1].strip() if line <= len(lines) else ""
        spelled = hit.group(0).lower() if flags else hit.group(0)
        for holder in folded.get(spelled, ()):
            found.add((holder, xk.Refusal(path, line, xk.UNSUPPORTED_FORM, text)))
    return sorted(found, key=lambda h: (h[0], h[1].line))


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    return []


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    return set()


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    return set()


xk.register(
    "dotnet_guest",
    xk.Profile(
        language_family=frozenset({"dotnet_guest"}),
        declarations=declarations,
        references=references,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        # Asked by the C# profile for the guest files it reaches.
        constant_escapes=constant_escapes,
    ),
)
