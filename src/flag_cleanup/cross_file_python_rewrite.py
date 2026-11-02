"""The reader's import, once its reads fold: rewritten so the module still
loads (see :mod:`flag_cleanup.cross_file_python`)."""

from __future__ import annotations

import re

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import unused
from flag_cleanup.cross_file_python_syntax import (
    _Module,
    _alias,
    _module_of,
    _import_bound_names,
    _in_import,
    _line_end,
    _not_a_mention,
    _parse,
    _text,
    _walk,
)
from flag_cleanup.cross_file_python_proofs import (
    _bindings,
)


#: The code ruff and flake8 share for an unused import.
_F401 = "F401"


#: A noqa directive and its codes, as flake8 reads one: a bare ``# noqa``
#: silences the whole line, a coded one only its codes.
_NOQA = re.compile(
    r"#\s*noqa(?::\s*(?P<codes>[A-Z]+[0-9]+(?:(?:\s*,\s*|\s+)[A-Z]+[0-9]+)*))?", re.IGNORECASE
)


def _own_lines(data: bytes, start: int, end: int) -> tuple[int, int, bytes] | None:
    """The edit removing the whole lines from ``start`` to ``end``, when
    nothing else is on them: only indentation before ``start``, and after
    ``end`` only a comma, spaces and a comment before the line ends."""
    line_start = data.rfind(b"\n", 0, start) + 1
    if data[line_start:start].strip(b" \t"):
        return None
    newline = data.find(b"\n", end)
    if newline == -1:
        return None
    rest = data[end:newline].rstrip(b"\r").lstrip(b" \t")
    if rest.startswith(b","):
        rest = rest[1:].lstrip(b" \t")
    if rest and not rest.startswith(b"#"):
        return None
    return line_start, newline + 1, b""


def rewrite_stranded_imports(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    repo_dir: str,
) -> str:
    """Rewrite each from-import of a declaration that no read uses any more.

    Run on a reader after its reads were folded. Deleting the import would
    also stop its module loading, and loading a module runs it, so the load is
    kept. Names beside others that survive are dropped from the list, all in
    one edit, and the rest keep the load. When every name in the statement is
    a binding of one of ``declarations`` that nothing reads, the statement
    becomes ONE import of the module itself under a private alias --
    ``import a.b.keys as _keys`` or ``from .sub import keys as _keys`` --
    marked ``# noqa: F401`` at the end of its line, after any comment there,
    where ruff and flake8 read it (merged into a noqa already there). Reads
    proven before the transform guarantee the alias is free (see
    :func:`_rewrite_refusal`, which vets exactly these forms). A qualified
    import is left to the stranded-import pass, which marks it. Anything else
    is left as it is, for the post-transform check to refuse.
    """
    root = _parse(source)
    if root.has_error:
        return source
    stranded: dict[int, tuple[Node, _Module, set[int]]] = {}
    # An enum entry's class is kept, and another module may import it from
    # this one, so its import is never rewritten: its line is only marked.
    kept_classes: dict[int, Node] = {}
    for decl in declarations:
        for binding in _bindings(root, path, repo_dir, decl):
            statement = binding.statement
            if (
                binding.kind != "bare"
                or binding.ambiguous
                or statement.parent is None
                or statement.parent.type != "module"
                or _still_used(root, binding.local)
            ):
                continue
            if decl.kind == xk.ENUM_ENTRY:
                kept_classes[statement.id] = statement
                continue
            entry = stranded.setdefault(statement.id, (statement, binding.module, set()))
            entry[2].add(binding.name_node.id)
    data = source.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    marked: set[int] = set()
    for statement in kept_classes.values():
        line_end = _line_end(data, statement.end_byte)
        if line_end not in marked and unused._python_line_can_end_in_a_comment(root, line_end):
            marked.add(line_end)
            edit = _mark(data, root, line_end)
            if edit is not None:
                edits.append(edit)
    # Source order: a statement deleted below no longer loads anything, so
    # it must not count as the load that lets a LATER statement go too.
    deleted: set[int] = set()
    for statement, module, ids in sorted(stranded.values(), key=lambda item: item[0].start_byte):
        entries = [entry for entry, _local in _import_bound_names(statement)]
        parenthesised = any(child.type == "(" for child in statement.children)
        if any(entry.id not in ids for entry in entries):
            edits.extend(_dropped_entries(data, entries, ids, parenthesised))
            continue
        whole = _own_statement_lines(data, statement)
        if whole is not None and _loaded_beside(root, statement, module, stranded, deleted):
            # Another import of this file loads the module anyway, so the
            # statement is deleted rather than kept for its load.
            deleted.add(statement.id)
            edits.append(whole)
            continue
        line_end = _line_end(data, statement.end_byte)
        if not module.parts or not unused._python_line_can_end_in_a_comment(root, line_end):
            continue
        # The comments inside the list stay, after the import that replaces
        # it on its one line. Never on a line of their own: at the top of the
        # file a `# type: ignore` there would silence mypy for the whole
        # module and a coding cookie would change how Python decodes it, and a
        # `# pylint:` pragma would reach every line below.
        kept = b"".join(
            b"  " + node.text.rstrip(b"\r")
            for node in sorted(_walk(statement), key=lambda n: n.start_byte)
            if node.type == "comment" and node.end_byte <= statement.end_byte
        )
        edits.append((statement.start_byte, statement.end_byte, _module_import(module) + kept))
        if line_end not in marked:
            # One mark per line: statements joined by `;` share it.
            marked.add(line_end)
            edit = _mark(data, root, line_end)
            if edit is not None:
                edits.append(edit)
    # Each statement's edit covers only its own bytes and a mark only the end
    # of its line, so none can overlap. Were two ever to, applying them would
    # corrupt the file: leave it for the post-transform check to refuse.
    edits.sort()
    if any(
        a[1] > b[0] or (a[0] == a[1] == b[0] and b[1] > b[0])  # an insertion where a removal starts
        for a, b in zip(edits, edits[1:])
    ):
        return source
    for start, end, text in reversed(edits):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8")


def _own_statement_lines(data: bytes, statement: Node) -> tuple[int, int, bytes] | None:
    """The edit deleting ``statement``'s whole lines, when nothing but
    indentation and a trailing comment shares them (no ``;`` neighbour)."""
    line_start = data.rfind(b"\n", 0, statement.start_byte) + 1
    if data[line_start : statement.start_byte].strip(b" \t"):
        return None
    newline = data.find(b"\n", statement.end_byte)
    end = len(data) if newline == -1 else newline + 1
    rest = data[statement.end_byte : end].strip()
    if rest and not rest.startswith(b"#"):
        return None
    return line_start, end, b""


def _loads(statement: Node) -> list[_Module]:
    """The modules an import statement names as its own (not the names it
    imports from one): ``import a.b.c`` names ``a.b.c``, ``from a.b import
    c`` names ``a.b``. A name imported FROM a module may be an attribute
    rather than a submodule, so it is never counted as a load."""
    if statement.type == "import_from_statement":
        module = statement.child_by_field_name("module_name")
        return [] if module is None else [_module_of(module)]
    if statement.type != "import_statement":
        return []
    found = []
    for child in statement.named_children:
        dotted = child.child_by_field_name("name") if child.type == "aliased_import" else child
        if dotted is not None and dotted.type == "dotted_name":
            found.append(_module_of(dotted))
    return found


def _loaded_beside(
    root: Node,
    statement: Node,
    module: _Module,
    stranded: dict[int, tuple[Node, _Module, set[int]]],
    deleted: set[int],
) -> bool:
    """Whether another top-level import of the file loads ``module`` no later
    than ``statement`` did.

    Importing ``a.b.c`` loads ``a``, ``a.b`` and ``a.b.c`` in turn, so a
    statement naming ``module`` or any submodule below it loads it, and a
    module is executed once per process: a second import of it does nothing.
    Two placements keep the program exactly as it was. An import EARLIER in
    the file has already loaded the module when ``statement`` ran. An import
    IMMEDIATELY after it, with nothing between, loads it before anything else
    runs. Anything later would move the module's load past the statements in
    between, which can matter, so it does not count. Only unconditional
    top-level statements count: an import inside an ``if``, ``try`` or
    function may not run. Relative and absolute spellings are compared only
    with their own kind, since telling them apart would need the package
    this file belongs to.
    """

    def loads_it(other: Node) -> bool:
        if other.id in deleted:
            return False
        return any(
            loaded.level == module.level and loaded.parts[: len(module.parts)] == module.parts
            for loaded in _loads(other)
        )

    top = [child for child in root.named_children if child.type != "comment"]
    index = next((i for i, child in enumerate(top) if child.id == statement.id), None)
    if index is None or not module.parts:
        return False
    if any(loads_it(other) for other in top[:index]):
        return True
    if index + 1 < len(top):
        following = top[index + 1]
        # One the same pass deletes may in turn lean on this statement.
        return following.id not in stranded and loads_it(following)
    return False


def _still_used(root: Node, local: str) -> bool:
    """Whether any token outside an import spells ``local`` as a variable."""
    return any(
        node.type == "identifier"
        and _text(node) == local
        and not _not_a_mention(node)
        and _in_import(node) is None
        for node in _walk(root)
    )


def _module_import(module: _Module) -> bytes:
    """An import of ``module`` itself under its alias, which replaces a sole-name import.

    The caller writes it only where the line can carry the mark that
    follows: not ``from . import NAME`` (the module is the package itself,
    refused before the transform), and not a line whose end a comment cannot
    reach (a ``\\`` continuation, or a string opened after a ``;``).
    """
    alias = _alias(module)
    if module.level:
        text = f"from {'.' * module.level}{'.'.join(module.parts[:-1])} import {module.parts[-1]} as {alias}"
    else:
        text = f"import {'.'.join(module.parts)} as {alias}"
    return text.encode("utf-8")


def _mark(data: bytes, root: Node, line_end: int) -> tuple[int, int, bytes] | None:
    """The edit marking the line ending at ``line_end`` with ``# noqa: F401``.

    A coded noqa already in the line's comment gains ``F401`` in the same
    directive, since flake8 reads only the first; a bare one, or one that
    names F401, already covers it. Otherwise the mark goes at the end of
    the line, over any trailing whitespace. Either way the edit covers only
    the comment or the line's end, never a statement.
    """
    line_start = data.rfind(b"\n", 0, line_end) + 1
    comment = next(
        (node for node in _walk(root) if node.type == "comment" and line_start <= node.start_byte < line_end),
        None,
    )
    if comment is not None:
        text = comment.text.decode("utf-8")
        match = _NOQA.search(text)
        if match is not None:
            codes = match.group("codes")
            if codes is None or _F401 in re.split(r"[\s,]+", codes.upper()):
                return None
            at = comment.start_byte + len(text[: match.end("codes")].encode("utf-8"))
            return at, at, b", " + _F401.encode()
    content_end = line_start + len(data[line_start:line_end].rstrip())
    return content_end, line_end, b"  # noqa: " + _F401.encode()


def _dropped_entries(
    data: bytes, entries: list[Node], ids: set[int], parenthesised: bool = False
) -> list[tuple[int, int, bytes]]:
    """The edits removing every entry in ``ids``, the survivors' layout untouched.

    In a parenthesised list, a run of removed names that has its lines to
    itself goes as those whole lines, its comma and comments with it, so
    every survivor keeps its own comment. Otherwise a run followed by a
    survivor goes from its first name to that survivor, and a run at the end
    from the end of the survivor before it to its last name's end, so a
    trailing comma and a closing parenthesis keep their lines. Survivors
    separate the runs, so the edits never overlap.
    """
    edits = []
    index = 0
    while index < len(entries):
        if entries[index].id not in ids:
            index += 1
            continue
        first = index
        while index < len(entries) and entries[index].id in ids:
            index += 1
        lines = _own_lines(data, entries[first].start_byte, entries[index - 1].end_byte) if parenthesised else None
        if lines is not None:
            edits.append(lines)
        elif index < len(entries):
            edits.append((entries[first].start_byte, entries[index].start_byte, b""))
        else:
            edits.append((entries[first - 1].end_byte, entries[index - 1].end_byte, b""))
    return edits
