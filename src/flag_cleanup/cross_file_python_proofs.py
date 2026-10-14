"""The reference proofs of the Python profile: which mentions of a declaration
in another file are reads the const rules remove (see
:mod:`flag_cleanup.cross_file_python`)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const
from flag_cleanup import unused
from flag_cleanup.cross_file_python_syntax import (
    _IMPORT_TYPES,
    _Module,
    _alias,
    _decoded_otherwise,
    _import_bound_names,
    _imported_name,
    _in_import,
    _line_end,
    _module_of,
    _nfkc,
    _not_a_mention,
    _parse,
    _text,
    _walk,
    declared_names,
)
from flag_cleanup.cross_file_python_imports import (
    _loads_other_code,
    _resolve,
)
from flag_cleanup.cross_file_python_packages import (
    _sys_modules,
    _transparent_path,
)


_DYNAMIC = xk.mention_pattern(["import_module", "__import__"])


#: Builtins that hand out a module's namespace, or run code in it: through
#: one, ``globals().update(overrides)`` or ``exec(code)`` rebinds a name the
#: reader imported without ever spelling it.
_NAMESPACE = frozenset({"globals", "vars", "locals", "exec", "eval"})


def _bound(decl: xk.KeyDeclaration) -> str:
    """The module-level name an import binds to reach ``decl``: the constant's
    own, or an enum entry's class."""
    return decl.owner if decl.kind == xk.ENUM_ENTRY and decl.owner is not None else decl.name


@dataclass(frozen=True)
class _Binding:
    """One import statement binding the declaring file, as the reader spells it."""

    statement: Node
    #: The ``dotted_name`` or ``aliased_import`` that binds it.
    name_node: Node
    kind: str  # "bare" | "qualified"
    #: The name the reader's tokens spell: ``K``, ``NAME``, or the owner's head.
    local: str
    #: Qualified only: the text before ``.NAME`` at a read.
    owner: str | None
    #: The module the statement imports from (``from M import ...``) or imports.
    module: _Module
    ambiguous: bool


def _bindings(root: Node, path: str, repo_dir: str, decl: xk.KeyDeclaration) -> list[_Binding]:
    """Every import statement in the file that can bind ``decl``'s file.

    Each is ``ambiguous`` when Python could load other code for that module:
    two files, a package on the way that can bind the next segment itself, or
    something compiled beside the declaring file.
    """
    found: list[_Binding] = []
    compiled = _loads_other_code(Path(decl.path), repo_dir)
    for statement in _walk(root):
        if statement.type == "import_statement":
            for entry, local in _import_bound_names(statement):
                dotted = entry.child_by_field_name("name") if entry.type == "aliased_import" else entry
                module = _module_of(dotted)
                files = _resolve(repo_dir, path, module)
                if decl.path not in files:
                    continue
                owner = local if entry.type == "aliased_import" else module.spelled
                # The read walks `a`, `a.b`, `a.b.keys` as attributes, which a
                # package `__init__.py` can bind to something else.
                opaque = not _transparent_path(repo_dir, path, module, decl.path, 1)
                ambiguous = len(files) > 1 or opaque or compiled
                found.append(_Binding(statement, entry, "qualified", local, owner, module, ambiguous))
        elif statement.type == "import_from_statement":
            source_node = statement.child_by_field_name("module_name")
            if source_node is None:
                continue
            module = _module_of(source_node)
            module_files = _resolve(repo_dir, path, module)
            for entry, local in _import_bound_names(statement):
                imported = _imported_name(entry)
                if decl.path in module_files and imported == _bound(decl):
                    ambiguous = len(module_files) > 1 or compiled
                    found.append(_Binding(statement, entry, "bare", local, None, module, ambiguous))
                    continue
                submodule = _Module(module.level, (*module.parts, imported))
                sub_files = _resolve(repo_dir, path, submodule)
                if decl.path not in sub_files:
                    continue
                # `from pkg import keys` gets pkg's ATTRIBUTE `keys` when its
                # `__init__.py` binds one, and only otherwise the submodule.
                opaque = not _transparent_path(repo_dir, path, submodule, decl.path, len(submodule.parts) - 1)
                ambiguous = len(sub_files) > 1 or opaque or compiled
                found.append(_Binding(statement, entry, "qualified", local, local, submodule, ambiguous))
    return found


def _is_read_at(node: Node, accessors: frozenset[str]) -> bool:
    """Whether ``node`` (the key expression) is the first argument of a removable read."""
    arguments = node.parent
    if arguments is None or arguments.type != "argument_list":
        return False
    if not arguments.named_children or arguments.named_children[0].id != node.id:
        return False
    call = arguments.parent
    return call is not None and call.type == "call" and key_const._python_is_removable_call(call, accessors)


def _refusal(path: str, node: Node, reason: str, source: str) -> xk.Refusal:
    return xk.refusal(path, node, reason, source)


def _refusal_at(path: str, source: str, offset: int, reason: str) -> xk.Refusal:
    """A refusal at the line holding ``offset``, for a match in the text, not a node."""
    line = source.count("\n", 0, offset)
    lines = source.splitlines()
    return xk.Refusal(path, line + 1, reason, lines[line].strip() if line < len(lines) else "")


def _bare_mention(node: Node, binding: _Binding, accessors: frozenset[str]) -> str | None:
    """None for a proven read of a bare binding, else the refusal reason."""
    statement = _in_import(node)
    if statement is not None:
        bound = [entry for entry, local in _import_bound_names(statement) if local == binding.local]
        if statement.id == binding.statement.id and any(e.id == binding.name_node.id for e in bound):
            return None  # the binding itself
        # Another import binding the same local name rebinds it; a module path
        # segment or the original name before `as` binds nothing.
        return xk.REBOUND if bound else None
    if key_const._python_is_binding(node):
        return xk.REBOUND
    if key_const._python_is_removable_read(node, accessors):
        return None
    return xk.NOT_A_READ


def _chain_top(node: Node) -> Node:
    """The outermost ``attribute`` whose object chain starts at ``node``."""
    current = node
    while (
        current.parent is not None
        and current.parent.type == "attribute"
        and current.parent.child_by_field_name("object").id == current.id
    ):
        current = current.parent
    return current


#: Module dunders that describe the module and cannot reach its namespace.
#: ``__spec__`` is not one: its loader runs the module's code again.
_INERT_DUNDERS = frozenset({"__name__", "__file__", "__doc__", "__package__", "__version__", "__path__"})


def _is_dunder(attribute: Node) -> bool:
    """Whether ``attribute`` is ``x.__member__`` for a member that can hand out
    the module's namespace: ``__dict__``, ``__getattr__``, ``__loader__`` and
    every other dunder not in :data:`_INERT_DUNDERS`."""
    name = attribute.child_by_field_name("attribute")
    text = _text(name) if name is not None else ""
    return (
        len(text) > 4
        and text.startswith("__")
        and text.endswith("__")
        and text not in _INERT_DUNDERS
    )


def _head_use(node: Node, binding: _Binding, twins: frozenset[int] = frozenset()) -> str | None:
    """None when a token of a qualified binding's head cannot hand the module on.

    ``owner`` is ``a.b.keys`` (head ``a``) or ``k``. Any attribute chain from
    the head that leaves the owner's path (``a.util.go()``) is fine. One that
    stops on the owner or on a prefix of it hands the module on
    (``helper(k)``, ``K = a.b.keys``); one that continues to ``.NAME`` is the
    read itself, judged at the name. ``twins`` are the import entries that
    bind the same name to the same module, each proven like ``binding``
    (``import a.b.keys as k`` twice): binding it again is no rebinding.
    """
    statement = _in_import(node)
    if statement is not None:
        if statement.id == binding.statement.id:
            return None
        rebinding = [entry for entry, local in _import_bound_names(statement) if local == binding.local]
        if not rebinding or all(e.id in twins for e in rebinding):
            return None
        # `import a.other` binds `a` to the same package; anything else does not.
        same_package = statement.type == "import_statement" and all(
            e.type == "dotted_name" for e in rebinding
        )
        return None if same_package and binding.owner != binding.local else xk.REBOUND
    if key_const._python_is_binding(node):
        return xk.REBOUND
    top = _chain_top(node)
    owner_parts = binding.owner.split(".")
    current = node
    depth = 1
    while True:
        prefix = ".".join(owner_parts[:depth])
        if _text(current).replace(" ", "") != prefix:
            # Left the owner's path. A dunder there (`a.__dict__`) is the
            # namespace the owner lives in, handed out.
            return xk.NOT_A_READ if _is_dunder(current) else None
        if current.id == top.id:
            return xk.NOT_A_READ  # stopped on the owner or a prefix of it
        if depth == len(owner_parts):
            # Continues past the owner: `.NAME` or another member, unless the
            # member is a dunder (`k.__dict__`, `k.__getattr__`), which hands
            # out the module's namespace.
            return xk.NOT_A_READ if _is_dunder(current.parent) else None
        current = current.parent
        depth += 1


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    root = _parse(source)
    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []
    stars = [n for n in _walk(root) if n.type == "wildcard_import"]
    plain = _nfkc(source)
    dynamic = _DYNAMIC.search(plain)
    namespace = [
        n
        for n in _walk(root)
        if n.type == "identifier" and _text(n) in _NAMESPACE and not _not_a_mention(n)
    ]
    sys_modules = _sys_modules(root)
    unreadable = root.has_error or _decoded_otherwise(source)
    for decl in declarations:
        if not xk.mention_pattern([decl.name]).search(plain):
            # A file that never spells the name reaches the constant only by
            # a name built at run time (the documented residual), and no
            # refusal here would keep it: only a file spelling it does.
            continue
        if unreadable:
            refused.append(xk.Refusal(path, 1, xk.UNSUPPORTED_FORM, ""))
            continue
        # A qualified binding whose name the file spells only in imports
        # reads nothing, so which module it means does not matter: the
        # `import pkg.keys as _keys` a rewrite leaves is one.
        bindings = [
            b for b in _bindings(root, path, repo_dir, decl) if b.kind != "qualified" or _spelled_outside_imports(root, b.local)
        ]
        top_level = [b for b in bindings if b.statement.parent is not None and b.statement.parent.type == "module"]
        for binding in bindings:
            if binding not in top_level:
                refused.append(_refusal(path, binding.statement, xk.UNSUPPORTED_FORM, source))
            elif binding.ambiguous:
                refused.append(_refusal(path, binding.statement, xk.IMPORT_UNRESOLVED, source))
        proven = [b for b in top_level if not b.ambiguous]
        if len(proven) != len(bindings):
            continue
        bare = [b for b in proven if b.kind == "bare"]
        qualified = [b for b in proven if b.kind == "qualified"]
        bare_locals = {b.local for b in bare}
        owners = {b.owner: b for b in qualified}

        if not proven:
            # A string naming the constant in a file that does not bind it:
            # `mock.patch("app.keys.NAME")` breaks once the constant is gone.
            named = xk.mention_pattern([decl.name])
            for node in _walk(root):
                if node.type == "string_content" and named.search(_text(node)):
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
        else:
            # A string mentioning the declaration, wherever the file binds it:
            # `getattr(k, "NAME")`, `exec("NAME = ...")`, `__all__`.
            named = xk.mention_pattern(bare_locals | {decl.name})
            for node in _walk(root):
                if node.type == "string_content" and named.search(_text(node)):
                    refused.append(_refusal(path, node, xk.NOT_A_READ, source))
            # Whatever can rebind the bound name, or the owner's head, without
            # spelling it: a star import, the namespace handed out, a dynamic
            # import.
            for node in stars:
                refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source))
            for node in namespace + sys_modules:
                refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
            if dynamic:
                refused.append(_refusal_at(path, plain, dynamic.start(), xk.UNSUPPORTED_FORM))

        for binding in bare:
            count = 0
            for node in _walk(root):
                if node.type != "identifier" or _text(node) != binding.local or _not_a_mention(node):
                    continue
                verdict = _bare_mention(node, binding, accessors)
                if verdict is None and _in_import(node) is None and _raw(node) != binding.local:
                    # A spelling Python reads as the name, but the reader rule's
                    # `#eq?` compares the text as written.
                    verdict = xk.UNSUPPORTED_FORM
                if verdict is not None:
                    refused.append(_refusal(path, node, verdict, source))
                elif _in_import(node) is None:
                    reads.append(xk.KeyReference(path, None, binding.local))
                    count += 1
            if count == 0:
                refused.append(_refusal(path, binding.statement, xk.NOT_A_READ, source))
            reason = _rewrite_refusal(root, binding, path, repo_dir, declarations)
            if reason is not None:
                refused.append(_refusal(path, binding.statement, reason, source))

        for binding in qualified:
            head = binding.owner.split(".")[0]
            twins = frozenset(
                b.name_node.id for b in qualified if b.local == binding.local and b.owner == binding.owner
            )
            for node in _walk(root):
                if node.type != "identifier" or _text(node) != head or _not_a_mention(node):
                    continue
                verdict = _head_use(node, binding, twins)
                if verdict is not None:
                    refused.append(_refusal(path, node, verdict, source))

        # Every `.NAME`: a proven owner's read, someone else's, or refused.
        for node in _walk(root):
            if node.type != "identifier" or _text(node) != decl.name:
                continue
            parent = node.parent
            if (
                parent is not None
                and parent.type == "dotted_name"
                and parent.named_children[0].id != node.id
                and _in_import(node) is None
            ):
                # A member in a `case a.b.NAME:` value pattern: a dotted name,
                # not an attribute, so no qualifier can be proven for it.
                refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                continue
            if parent is not None and parent.type == "attribute" and _not_a_mention(node):
                qualifier = _text(parent.child_by_field_name("object")).replace(" ", "")
                if qualifier in owners and _in_import(node) is None:
                    if _raw(parent) != f"{qualifier}.{decl.name}":
                        # `app.keys .NAME`, a chain split over lines: Python
                        # reads it as the owner's, but the reader rule's `#eq?`
                        # compares the text as written and would not match.
                        refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                    elif _is_read_at(parent, accessors):
                        reads.append(xk.KeyReference(path, qualifier, decl.name))
                    else:
                        refused.append(_refusal(path, node, xk.NOT_A_READ, source))
                elif stars:
                    refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source))
                elif (
                    dynamic
                    or namespace
                    or sys_modules
                    or not _someone_elses(root, parent.child_by_field_name("object"), path, repo_dir, decl)
                ):
                    refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
                continue
            if decl.name in bare_locals or _not_a_mention(node) or _in_import(node) is not None:
                continue
            # A bare NAME that no import of ours binds.
            if stars:
                refused.append(_refusal(path, node, xk.IMPORT_UNRESOLVED, source))
            elif dynamic:
                refused.append(_refusal(path, node, xk.UNSUPPORTED_FORM, source))
        # A qualified `m.NAME` off a dynamically imported module is refused by
        # the attribute branch above: `m` is bound by no import, so it is never
        # someone else's.

        # `from M import NAME [as X]` that is not one of ours: someone else's
        # only when M is one file, not the declaring one, that assigns NAME
        # itself. A re-exporter (`from .keys import *` in a package
        # `__init__.py`) hands on ours, and an import nothing resolves cannot
        # be told apart from one.
        ours = {b.name_node.id for b in bindings}
        for statement in _walk(root):
            if statement.type != "import_from_statement":
                continue
            source_node = statement.child_by_field_name("module_name")
            if source_node is None:
                continue
            for entry, _local in _import_bound_names(statement):
                if entry.id in ours or _imported_name(entry) != decl.name:
                    continue
                if not _assigns_itself(_resolve(repo_dir, path, _module_of(source_node)), decl):
                    refused.append(_refusal(path, entry, xk.IMPORT_UNRESOLVED, source))
    if refused:
        # One line refused for two declarations is one row.
        unique: list[xk.Refusal] = []
        for refusal in refused:
            if refusal not in unique:
                unique.append(refusal)
        return [], unique
    return reads, refused


def _raw(node: Node) -> str:
    """``node``'s text exactly as written, not normalized as Python reads names."""
    return node.text.decode("utf-8", "replace")


def _spelled_outside_imports(root: Node, name: str) -> bool:
    """Whether any token outside an import statement spells ``name``: an
    identifier, wherever it stands, or a word in a string or a comment."""
    word = xk.mention_pattern([name])
    for node in _walk(root):
        if node.type in _IMPORT_TYPES:
            continue
        if node.type == "identifier" and _text(node) == name and _in_import(node) is None:
            return True
        if node.type in ("string_content", "comment") and word.search(_text(node)) and _in_import(node) is None:
            return True
    return False


def _someone_elses(root: Node, qualifier: Node, path: str, repo_dir: str, decl: xk.KeyDeclaration) -> bool:
    """Whether ``qualifier.NAME`` surely reads another module's own ``NAME``.

    The qualifier's head must be bound only by top-level imports, each
    resolving to exactly one file that is not ``decl``'s and that assigns
    ``NAME`` at module level itself.
    """
    head_node = qualifier
    while head_node.type == "attribute":
        head_node = head_node.child_by_field_name("object")
    if head_node.type != "identifier":
        return False
    head = _text(head_node)
    spelled = _text(qualifier).replace(" ", "")
    found = False
    for node in _walk(root):
        if node.type != "identifier" or _text(node) != head or _not_a_mention(node):
            continue
        statement = _in_import(node)
        if statement is None:
            # Only `head.<member>` keeps the import's binding: by elimination,
            # any other token -- a parameter, a lambda's or a comprehension's
            # target, a walrus, the head used as a value -- may be a second
            # binding, and no list of binder forms is complete.
            parent = node.parent
            if parent is None or parent.type != "attribute" or parent.child_by_field_name("object").id != node.id:
                return False
            continue
        if statement.parent is None or statement.parent.type != "module":
            return False
        for entry, local in _import_bound_names(statement):
            if local != head:
                continue
            target = _target_of(statement, entry, spelled, path, repo_dir)
            if target is None or target == decl.path:
                return False
            text = Path(target).read_text(encoding="utf-8", errors="replace")
            if (target, None, decl.name) not in declared_names(target, text):
                return False
            found = True
    return found


def _assigns_itself(files: tuple[str, ...], decl: xk.KeyDeclaration) -> bool:
    """Whether ``files`` is one file, not ``decl``'s, that assigns ``decl.name`` itself."""
    if len(files) != 1 or files[0] == decl.path or _loads_other_code(Path(files[0])):
        return False
    text = Path(files[0]).read_text(encoding="utf-8", errors="replace")
    return (files[0], None, decl.name) in declared_names(files[0], text)


def _target_of(statement: Node, entry: Node, spelled: str, path: str, repo_dir: str) -> str | None:
    """The one file ``spelled`` (``o``, ``other.keys``) means through this import entry.

    None when it could be another: two files, something compiled beside it,
    or a package on the way whose ``__init__.py`` can bind the next segment
    itself.
    """
    if statement.type == "future_import_statement":
        return None  # a `__future__` feature, not a module of the checkout
    if statement.type == "import_statement":
        if entry.type == "aliased_import":
            module = _module_of(entry.child_by_field_name("name"))
        else:
            module = _Module(0, tuple(spelled.split(".")))
        start = 1
    else:
        base = _module_of(statement.child_by_field_name("module_name"))
        module = _Module(base.level, (*base.parts, _imported_name(entry)))
        start = len(module.parts) - 1
    files = _resolve(repo_dir, path, module)
    if len(files) != 1 or _loads_other_code(Path(files[0]), repo_dir):
        return None
    if not _transparent_path(repo_dir, path, module, files[0], start):
        return None
    return files[0]


def _rewrite_refusal(
    root: Node,
    binding: _Binding,
    path: str,
    repo_dir: str,
    declarations: tuple[xk.KeyDeclaration, ...] = (),
) -> str | None:
    """Why the from-import cannot be rewritten to keep its load, if it cannot.

    A name beside others leaves them to keep the load, but only one that
    survives the fold: a name importing any of ``declarations`` from the same
    module (a second constant holding the key, or this one again under
    another alias) folds away too. When none survives, the import is a sole
    name for this purpose, and becomes ``import a.b.keys as _keys`` or
    ``from .sub import keys as _keys`` (:func:`_alias`), marked ``# noqa:
    F401`` at the end of its line, which must be able to end in a comment.
    The alias must be
    free: the reader may spell it nowhere else, as a name or in a string
    (``__all__ = ["_keys"]``), and a package ``__init__.py`` reader may not
    sit beside a submodule of that name, which the alias would shadow. For
    the relative form the package ``__init__.py`` must also be transparent
    for the module (:func:`_transparent_path`), or Python would hand back its
    attribute instead of loading the module.
    """
    module = binding.module
    module_files = _resolve(repo_dir, path, module)
    folding = {_imported_name(binding.name_node)} | {
        d.name for d in declarations if d.path in module_files
    }
    if any(_imported_name(entry) not in folding for entry, _local in _import_bound_names(binding.statement)):
        return None
    if module.level and not module.parts:
        return xk.UNSUPPORTED_FORM  # `from . import NAME`: the module is the package itself
    data = root.text
    line_end = _line_end(data, binding.statement.end_byte)
    if not unused._python_line_can_end_in_a_comment(root, line_end):
        # The rewrite has to mark its line `# noqa: F401`, and a comment
        # there would be inside a string or after a `\\` continuation.
        return xk.UNSUPPORTED_FORM
    if any(
        node.type in ("import_statement", "import_from_statement")
        and node.id != binding.statement.id
        and node.start_byte < line_end < node.end_byte
        for node in root.named_children
    ):
        # Another import goes on past the end of that line, and its own
        # rewrite can drop a name from where the mark would go.
        return xk.UNSUPPORTED_FORM
    alias = _alias(module)
    for node in _walk(root):
        if node.type == "string_content" and _text(node) == alias:
            return xk.UNSUPPORTED_FORM
        if node.type != "identifier" or _text(node) != alias or _not_a_mention(node):
            continue
        statement = _in_import(node)
        if statement is None or statement.id != binding.statement.id:
            return xk.UNSUPPORTED_FORM
    reader = Path(path)
    if reader.name == "__init__.py":
        try:
            siblings = [entry.name for entry in reader.parent.iterdir()]
        except OSError:
            return xk.UNSUPPORTED_FORM
        if any(name == alias or name.startswith(alias + ".") for name in siblings):
            return xk.UNSUPPORTED_FORM
    if module.level and (
        len(module_files) != 1 or not _transparent_path(repo_dir, path, module, module_files[0], len(module.parts) - 1)
    ):
        return xk.UNSUPPORTED_FORM
    return None
