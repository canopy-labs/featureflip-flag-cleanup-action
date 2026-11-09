"""A Python key constant whose declaration the run deleted must be named nowhere.

The same belt as :mod:`flag_cleanup.ts_key_survivors`, for a module constant
read through a positioned accessor (``flag_is_active[1]``). Which references
are reads the rules fold is decided twice, by the Gate 2 predicates
(``key_const.python_positioned_read``, ``_python_is_removable_read``) and by the
rules' own filters; a disagreement leaves a reference standing with nothing to
point at, and the module raises ``NameError`` when it runs. This check does not
ask either: it reads the bytes the run produced and refuses the flag when an
identifier still names a deleted constant, in its own file or, through an
import (``from consts import OLD``, ``consts.OLD``), in any file that imports it.
"""

from __future__ import annotations

from pathlib import Path

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const, source_index
from flag_cleanup.syntax import _parse, _walk


def refusals(changed, repo_dir: str, flag_key: str, deleted) -> dict[str, str]:
    """``{path: reason}`` for each file still naming a deleted key constant.

    ``changed`` are the run's summaries (``path``, ``original_content``,
    ``content``); ``deleted(s)`` says a summary's file was deleted whole."""
    gone: list[tuple[str, str]] = []
    texts = {str(Path(s.path).resolve()): s for s in changed if str(s.path).endswith(".py")}
    for resolved, s in texts.items():
        before = _constants(s.original_content, flag_key)
        after = set() if deleted(s) else _constants(s.content, flag_key)
        gone.extend((resolved, name) for name in sorted(before - after))
    if not gone:
        return {}
    found: dict[str, str] = {}
    listing = source_index.files(repo_dir, (".py",))
    for declaring, name in gone:
        own = texts[declaring]
        if not deleted(own):
            own_root = _parse(own.content, "python")
            if _names(own_root, name) or _spelled_by_name(own_root, name):
                found.setdefault(own.path, _reason(name))
        for listed in listing:
            resolved = str(Path(listed.path).resolve())
            if resolved == declaring:
                continue
            changed_file = texts.get(resolved)
            if changed_file is not None and deleted(changed_file):
                continue
            try:
                text = changed_file.content if changed_file is not None else listed.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if name not in text:
                continue
            problem = _imports_and_names(str(listed.path), text, declaring, name, repo_dir)
            if problem:
                found.setdefault(str(listed.path), _reason(name, problem))
    return found


def _reason(name: str, how: str = "") -> str:
    return (
        f"the key constant `{name}` was deleted but a reference to it survived "
        f"the rewrite{f' ({how})' if how else ''}, which would raise NameError, "
        "ImportError or AttributeError"
    )


def _constants(text: str, flag_key: str) -> set[str]:
    root = _parse(text, "python")
    return {c.name for c in key_const._python_key_consts(root, text.encode("utf-8"), flag_key)}


def _names(root, name: str) -> bool:
    """Whether an identifier ``name`` is USED (not bound, not imported) under ``root``."""
    for node in _walk(root):
        if node.type != "identifier" or node.text.decode("utf-8") != name:
            continue
        if key_const._python_is_binding(node) or _in_import(node):
            continue
        parent = node.parent
        if parent is not None and parent.type == "attribute":
            held = parent.child_by_field_name("attribute")
            if held is not None and held.id == node.id:
                continue  # `x.NAME` is somebody else's attribute
        if parent is not None and parent.type == "keyword_argument":
            held = parent.child_by_field_name("name")
            if held is not None and held.id == node.id:
                continue
        return True
    return False


def _in_import(node) -> bool:
    while node is not None:
        if node.type in ("import_statement", "import_from_statement", "future_import_statement"):
            return True
        node = node.parent
    return False


_DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})


def _callee(call) -> str:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "attribute":
        function = function.child_by_field_name("attribute")
    return function.text.decode("utf-8") if function is not None else ""


def _strings(node) -> list[str]:
    return [c.text.decode("utf-8") for c in _walk(node) if c.type == "string_content"]


def _spelled_by_name(root, name: str) -> str:
    """How the file names ``name`` without an identifier, or ``""``: an entry of
    ``__all__``, ``getattr(x, "NAME")``, a dynamic import of ``pkg.NAME``, or an
    attribute read off the module a dynamic import returned."""
    for node in _walk(root):
        if node.type in ("assignment", "augmented_assignment"):
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and left.text == b"__all__" and name in _strings(right):
                return "listed in `__all__`"
        elif node.type == "call":
            callee = _callee(node)
            arguments = node.child_by_field_name("arguments")
            strings = _strings(arguments) if arguments is not None else []
            if callee in ("getattr", "hasattr") and name in strings:
                return f"read by `{callee}`"
            if callee in _DYNAMIC_IMPORTS and any(name in s.split(".") for s in strings):
                return f"named in `{callee}`"
        elif node.type == "attribute":
            obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
            if (
                obj is not None
                and attr is not None
                and attr.text.decode("utf-8") == name
                and obj.type == "call"
                and _callee(obj) in _DYNAMIC_IMPORTS
            ):
                return "read off a dynamically imported module"
    return ""


def _imports_and_names(path: str, text: str, declaring: str, name: str, repo_dir: str) -> str:
    """How ``path`` still reaches the deleted ``name``, or ``""``.

    Fails closed: besides the imports it can resolve to the declaring module (an
    explicit ``from M import name``, ``owner.name``, a star import followed by
    the bare name), a file that uses the name and has ANY import it cannot
    resolve, or spells the name in ``__all__``, ``getattr`` or a dynamic import,
    is refused. Modules are resolved, not matched by name, so a same-named
    module elsewhere is not a hit."""
    from flag_cleanup import cross_file_python_proofs as proofs  # lazy: a cycle through unused

    root = _parse(text, "python")
    dynamic = _spelled_by_name(root, name)
    if dynamic:
        return dynamic
    decl = xk.KeyDeclaration(declaring, name, None, "")
    bindings = proofs._bindings(root, path, repo_dir, decl)
    for binding in bindings:
        if binding.kind == "bare":
            return "imported from the deleted constant's module"
        for node in _walk(root):
            if node.type != "attribute":
                continue
            obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
            if (
                obj is not None
                and attr is not None
                and obj.text.decode("utf-8").replace(" ", "") == binding.owner
                and attr.text.decode("utf-8") == name
            ):
                return "read through its module"
    own_binding = any(
        n.type == "identifier" and n.text.decode("utf-8") == name and key_const._python_is_binding(n)
        for n in _walk(root)
    )
    star_ours = False
    unresolved_locals: set[str] = set()
    unresolved = False
    for statement in _walk(root):
        if statement.type not in ("import_statement", "import_from_statement"):
            continue
        if statement.type == "import_from_statement":
            source_node = statement.child_by_field_name("module_name")
            if source_node is None:
                continue
            module = proofs._module_of(source_node)
            if module.parts == ("__future__",):
                continue
            files = proofs._resolve(repo_dir, path, module)
            if any(c.type == "wildcard_import" for c in statement.children):
                if declaring in files or not files:
                    star_ours = True
                    unresolved = unresolved or not files
                continue
            if not files:
                unresolved = True
                unresolved_locals.update(local for _entry, local in proofs._import_bound_names(statement))
        else:
            for entry, local in proofs._import_bound_names(statement):
                dotted = entry.child_by_field_name("name") if entry.type == "aliased_import" else entry
                if not proofs._resolve(repo_dir, path, proofs._module_of(dotted)):
                    unresolved = True
                    unresolved_locals.add(local)
    if not own_binding and (star_ours or unresolved) and _names(root, name):
        return "named in a file with a star or unresolvable import"
    if unresolved_locals:
        for node in _walk(root):
            if node.type != "attribute":
                continue
            obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
            if (
                obj is not None
                and attr is not None
                and attr.text.decode("utf-8") == name
                and obj.text.decode("utf-8").split(".")[0] in unresolved_locals
            ):
                return "read through an import that could not be resolved"
    return ""
