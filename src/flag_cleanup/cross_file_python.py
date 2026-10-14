"""Python profile for cross-file key constants.

A declaration is a module-level ``NAME = "key"``, exactly what the in-file
key-const gate accepts, in a file that cannot give the name another value: it
binds the name nowhere else, has no star import, and reaches its own
namespace by no route that skips spelling the name (:func:`declarations`).
Its module is its FILE: an import resolves to one file or is refused, so two
declarations never compete by name. And since any other module can still
assign the constant's attribute, a read folds only when its declaration is
deleted: one refusal that keeps it keeps every read of it
(``readers_need_deletable``).

A mention in another file is proven when that file binds the declaring file
through a top-level import, and every token spelling the bound name is the
import itself or the first argument of a read the const rules remove:

* bare -- ``from pkg.keys import NAME`` (or ``as K``, or a relative
  ``from .keys import NAME``), read as ``NAME`` or ``K``;
* qualified -- ``import pkg.keys`` (``pkg.keys.NAME``), ``import pkg.keys as
  k`` (``k.NAME``) or ``from pkg import keys`` (``keys.NAME``).

An absolute import resolves when exactly one file in the checkout ends in
``pkg/keys.py`` or ``pkg/keys/__init__.py``; a relative one, from the reader's
directory. A declaring file with an extension module of its name beside it is
refused, since Python loads that first (:func:`_loads_other_code`). Anything
else that can reach the constant -- a second binding, a use as a value or
inside a string, the module handed on, a star import, an import nested in a
block, ``importlib``, the module's namespace taken through
``globals()``, ``vars()``, ``locals()``, ``exec``, ``eval`` or ``sys.modules``
-- is refused. A qualified import whose name the file spells only in imports
reads nothing and is not judged: which module it means does not matter.
``from pkg import keys`` and ``import pkg.keys`` take ``keys``
as an attribute of the package, so its ``__init__.py`` must be one that cannot
bind it (:func:`_transparent_init`), and every one above it one that leaves
the way down alone (:func:`_transparent_above`). A module reached only through
``sys.modules`` in a file that does not bind the declaration, or a path built
at run time, is not seen.

An enum member holding the key is an entry, proven by
:mod:`~flag_cleanup.cross_file_python_enums`: its readers fold although the
member is kept, since Python refuses to reassign a member through its class.

The profile is split by layer, each module importing only from the ones
before it: :mod:`~flag_cleanup.cross_file_python_syntax` (the parse, names,
a module as an import spells it), :mod:`~flag_cleanup.cross_file_python_imports`
(which file an import means), :mod:`~flag_cleanup.cross_file_python_packages`
(the namespace scan and the package whitelist), this module (declarations and
the profile), :mod:`~flag_cleanup.cross_file_python_proofs` (the reference
proofs), :mod:`~flag_cleanup.cross_file_python_enums` (enum members) and
:mod:`~flag_cleanup.cross_file_python_rewrite` (the reader's import, once its
reads fold).
"""

from __future__ import annotations

import ast
import re

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import cross_file_python_enums as enums
from flag_cleanup import key_const
from flag_cleanup.cross_file_python_packages import _opaque_line, _opaque_spots, _sys_modules
from flag_cleanup.cross_file_python_proofs import references as _constant_references
from flag_cleanup.cross_file_python_syntax import (
    _ast_bound,
    _decoded_otherwise,
    _line,
    _nfkc,
    _parse,
    _python_parse,
    _text,
    _walk,
    declared_names,
)

# The rest of the profile, reached through this module by its callers and tests.
from flag_cleanup.cross_file_python_imports import _resolve  # noqa: F401
from flag_cleanup.cross_file_python_rewrite import rewrite_stranded_imports  # noqa: F401
from flag_cleanup.cross_file_python_syntax import _Module, _module_of  # noqa: F401


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    """The constants holding ``flag_key`` whose value the file surely never
    changes, and the enum members holding it that are proven.

    Python has no ``const``: the module's name can be bound again after the
    assignment, by the file itself or through its namespace, and a reader
    then gets that value, not the key. So a constant is a declaration only
    when :func:`_unfixed` finds nothing; :func:`entry_findings` reports why
    any other one is not. An enum member is judged by
    :func:`~flag_cleanup.cross_file_python_enums.entries`.
    """
    found = [decl for decl, why in _judged_declarations(path, source, flag_key) if not why]
    return found + _entries(path, source, flag_key)[0]


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """Why each constant holding the key in ``path`` is not a declaration,
    and the refusals about each enum member holding it.

    Reported at the declaring file, on the line that can change its value;
    the file then keeps every declaration, so no reader of it is folded. An
    entry's refusal names the entry, and keeps only its own reads.
    """
    found: list[xk.Refusal] = []
    for _decl, why in _judged_declarations(path, source, flag_key):
        for refusal in why:
            if refusal not in found:
                found.append(refusal)
    for refusal in _entries(path, source, flag_key)[1]:
        if refusal not in found:
            found.append(refusal)
    return found


def _entries(path: str, source: str, flag_key: str) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """:func:`~flag_cleanup.cross_file_python_enums.entries` for ``path``, given
    Python's parse only when nothing reaches the file's namespace unnamed."""
    if not xk.key_token(flag_key).search(source):
        return [], []
    root = _parse(source)
    tree = None if _namespace_findings(path, source, root) else _python_parse(source, root)
    return enums.entries(path, source, flag_key, root, tree)


def home_constant_names(path: str, source: str, flag_key: str) -> set[str]:
    """The names of the file's constants holding ``flag_key`` when nothing in
    the file can give one of them another value; empty otherwise.

    What :func:`entry_findings` refuses here may only keep OTHER files from
    trusting the value: the file binds each name once, holds no star import
    and no ``sys.modules``, and every gateway :func:`_opaque_spots` finds is
    one :func:`_inert_spots` knows writes nothing -- a ``def`` of a dunder
    (a ``__repr__`` method, a module ``__getattr__``) or a ``getattr`` of a
    literal name, which is an attribute read. Everything else (``exec``,
    ``globals()``, ``setattr``, a computed ``getattr``, ``importlib``, ...)
    can rebind a name inside the file without spelling it, where the in-file
    gate cannot see it, so it leaves the answer empty.
    """
    root = _parse(source)
    consts = key_const._python_key_consts(root, source.encode("utf-8"), flag_key)
    if not consts or root.has_error or _decoded_otherwise(source):
        return set()
    if any(node.type == "wildcard_import" for node in _walk(root)) or _sys_modules(root):
        return set()
    tree = _python_parse(source, root)
    if tree is None:
        return set()
    if any(_rebinders(path, source, tree, _nfkc(c.name), c.declaration) for c in consts):
        return set()
    inert = _inert_spots(root)
    lines = source.splitlines()
    for line, column in _opaque_spots(source):
        if column is None or line > len(lines):
            return set()
        if (line, len(lines[line - 1][:column].encode("utf-8"))) not in inert:
            return set()
    return {c.name for c in consts}


def _inert_spots(root: Node) -> set[tuple[int, int]]:
    """Where a token :func:`_opaque_spots` finds writes nothing, as ``(line,
    byte column)``: the name of a ``def`` (a dunder method), and ``getattr``
    called with an object, a plain string literal and at most a default."""
    found: set[tuple[int, int]] = set()
    for node in _walk(root):
        spot = None
        if node.type == "function_definition":
            spot = node.child_by_field_name("name")
        elif node.type == "call":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is None or function.type != "identifier" or _text(function) != "getattr":
                continue
            args = arguments.named_children if arguments is not None and arguments.type == "argument_list" else []
            if (
                len(args) in (2, 3)
                and all(a.type not in ("keyword_argument", "list_splat", "dictionary_splat", "comment") for a in args)
                and args[1].type == "string"
                and all(c.type in ("string_start", "string_content", "string_end") for c in args[1].children)
                and not re.search(r"[fFtT]", _text(args[1].children[0]))
            ):
                spot = function
        if spot is not None:
            found.add((spot.start_point[0] + 1, spot.start_point[1]))
    return found


def _judged_declarations(
    path: str, source: str, flag_key: str
) -> list[tuple[xk.KeyDeclaration, list[xk.Refusal]]]:
    root = _parse(source)
    consts = key_const._python_key_consts(root, source.encode("utf-8"), flag_key)
    if not consts:
        return []
    shared = _namespace_findings(path, source, root)
    tree = None if shared else _python_parse(source, root)
    judged = []
    for const in consts:
        decl = xk.KeyDeclaration(path, const.name, None, path)
        why = list(shared)
        if not why:
            if tree is None:
                why = [xk.Refusal(path, 1, xk.UNSUPPORTED_FORM, _line(source, 1))]
            else:
                why = _rebinders(path, source, tree, _nfkc(const.name), const.declaration)
        judged.append((decl, why))
    return judged


def _namespace_findings(path: str, source: str, root: Node) -> list[xk.Refusal]:
    """What lets the file's namespace change without the constant's name
    being spelled: a star import, which binds whatever its module holds,
    and every way to reach the namespace itself or run code in it -- the
    gateways and dunders :func:`_spells_opaque` names (``globals()``,
    ``vars()``, ``exec``, ``setattr``, ``__dict__``, a frame's
    ``f_globals``, a function's ``__globals__``, a module ``__getattr__``,
    ``importlib``, the builtins, ...) and ``sys.modules``. A file that does
    not parse, that Python decodes into other text, or that Python and
    tree-sitter parse differently, is refused too: the declaration might sit
    where the parse cannot see what follows it."""
    if root.has_error or _decoded_otherwise(source):
        return [xk.Refusal(path, 1, xk.UNSUPPORTED_FORM, _line(source, 1))]
    found = [
        xk.refusal(path, node, xk.UNSUPPORTED_FORM, source)
        for node in _walk(root)
        if node.type == "wildcard_import"
    ]
    opaque = _opaque_line(source)
    if opaque is not None:
        found.append(xk.Refusal(path, opaque, xk.UNSUPPORTED_FORM, _line(source, opaque)))
    found.extend(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source) for node in _sys_modules(root))
    return found


def _rebinders(path: str, source: str, tree: ast.Module, name: str, declaration: Node) -> list[xk.Refusal]:
    """Every place in the file, at any depth and in any scope, that binds,
    deletes or stores ``name`` other than the declaration itself, read from
    Python's own parse: a store or a delete of the name or of an attribute
    so named (``app.keys.NAME = ...`` through the module imported into
    itself), a parameter, a ``def`` or ``class``, ``global``/``nonlocal``,
    an exception, ``match`` or type-parameter capture, and an import.

    Python's parse marks every target it binds with a store or delete
    context, so this needs no list of binding statements. A binding in
    another scope does not rebind the module's name, and is counted anyway.
    """
    line = declaration.start_point[0] + 1
    found: list[xk.Refusal] = []
    own = False
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [_nfkc(a.asname or a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.Attribute):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                names = [_nfkc(node.attr)]
        else:
            names = [_nfkc(n) for n in _ast_bound(node)]
        if name not in names:
            continue
        if (
            not own
            and isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.lineno == line
            and node.col_offset == declaration.start_point[1]
        ):
            own = True  # the declaration's own target
            continue
        number = getattr(node, "lineno", line)
        refusal = xk.Refusal(path, number, xk.REBOUND, _line(source, number))
        if refusal not in found:
            found.append(refusal)
    if not own:
        # Python's parse found no assignment where the declaration is.
        found.append(xk.Refusal(path, line, xk.UNSUPPORTED_FORM, _line(source, line)))
    return found


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    """The proven reads in ``path``: of constants by the module's proof, of
    enum entries by :func:`~flag_cleanup.cross_file_python_enums.references`.
    A file with any refusal folds nothing."""
    constants = tuple(d for d in declarations if d.kind != xk.ENUM_ENTRY)
    entries = tuple(d for d in declarations if d.kind == xk.ENUM_ENTRY)
    reads, refused = _constant_references(path, source, constants, accessors, repo_dir) if constants else ([], [])
    if entries:
        entry_reads, entry_refused = enums.references(path, source, entries, accessors, repo_dir)
        reads = [*reads, *entry_reads]
        refused = [*refused, *(r for r in entry_refused if r not in refused)]
    if refused:
        return [], refused
    return reads, refused


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """Empty: a Python key constant has no owner, so no owner can compete."""
    return set()


xk.register(
    "python",
    xk.Profile(
        language_family=frozenset({"python"}),
        declarations=declarations,
        references=references,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        entry_findings=entry_findings,
        home_constant_names=home_constant_names,
        enum_dynamic_uses=enums.enum_dynamic_uses,
        # A class is a value any module can rebind or patch by name, and
        # `_value_` writes reach a member unnamed: each refuses its entries.
        enum_escapes=enums.enum_escapes,
        enum_module_pattern=enums.enum_module_pattern,
        escapes_void_readers=True,
        # Any module can write a constant's attribute, and no rule tells such
        # a write from a harmless mention: a kept declaration keeps every read.
        readers_need_deletable=True,
    ),
)
