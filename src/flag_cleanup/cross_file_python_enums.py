"""Enum members holding a flag key, for the Python profile (see
:mod:`flag_cleanup.cross_file_python`).

A member is a declaration when its class is a top-level ``StrEnum``,
``(str, Enum)``, ``Enum`` or Django ``TextChoices`` whose body can run no code
at class creation beyond ``auto()`` and a translation call
(:func:`_class_kind`), and its value is the key as a plain string. Python
refuses to assign or delete a member through its class, and an enum with
members cannot be subclassed, so ``Flags.X.value`` is that literal unless
something writes the member's ``_value_``, which :func:`enum_escapes`
refuses. A member is never deleted, so its readers fold although it is kept.
"""

from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const
from flag_cleanup.cross_file_python_imports import _loads_other_code, _resolve
from flag_cleanup.cross_file_python_packages import _sys_modules
from flag_cleanup.cross_file_python_proofs import (
    _DYNAMIC,
    _NAMESPACE,
    _Binding,
    _bindings,
    _head_use,
    _is_read_at,
    _raw,
    _target_of,
)
from flag_cleanup.cross_file_python_syntax import (
    _ast_bound,
    _decoded_otherwise,
    _import_bound_names,
    _in_import,
    _line,
    _module_of,
    _nfkc,
    _not_a_mention,
    _parse,
    _python_parse,
    _text,
    _walk,
)


#: A base whose last name ends like an enum's: only such a class is judged
#: here. Any other class holding the key is not an enum, and not ours.
_ENUM_LIKE = re.compile(r"\w*(?:Enum|Choices)\b")

_STR_ENUMS = frozenset({"enum.StrEnum", "enum.Enum"})
_CHOICES = frozenset({"django.db.models.TextChoices", "django.db.models.enums.TextChoices"})
_TRANSLATIONS = frozenset(
    f"django.utils.translation.{name}"
    for name in ("gettext", "gettext_lazy", "pgettext", "pgettext_lazy")
)
_METHOD_DECORATORS = frozenset({"property", "classmethod", "staticmethod"})
#: Method names that would replace what a read of the member returns.
_RESERVED_METHODS = frozenset({"value", "name", "label"})


def entries(
    path: str, source: str, flag_key: str, root: Node, tree: ast.Module | None
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """The enum members holding ``flag_key`` in ``path`` that are declarations,
    and the refusals about every member holding it.

    ``tree`` is Python's own parse of the file, or None when the file's
    namespace can change without spelling a name (a star import, ``exec``,
    ...) or its parse disagrees with tree-sitter's: then no member is proven.
    """
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for cls, member in _holders(root, xk.key_token(flag_key), _aliased_enums(tree)):
        owner = _text(cls.child_by_field_name("name"))
        decl = xk.KeyDeclaration(
            path, _text(member.child_by_field_name("left")), owner, path,
            kind=xk.ENUM_ENTRY, accessors=frozenset({"value"}),
        )
        node = _class_node(tree, cls) if tree is not None else None
        kind = _class_kind(node, tree) if node is not None else None
        if (
            kind is None
            or not _plain_key(member.child_by_field_name("right"), flag_key, kind)
            or _handed_on(root, owner, cls)
        ):
            line = member.start_point[0] + 1
            refused.append(xk.Refusal(path, line, xk.ENUM_UNPROVEN, _line(source, line), decl.entry))
            continue
        found.append(decl)
        for refusal in _in_file_mentions(path, source, root, decl, member):
            if refusal not in refused:
                refused.append(refusal)
    return found, refused


def _aliased_enums(tree: ast.Module | None) -> set[tuple[int, int]]:
    """Where each top-level class starts whose base is an enum only through
    an import alias (``from enum import StrEnum as SE``), as ``(line, column)``."""
    if tree is None:
        return set()
    imports = _imports(tree)
    counts = _bound_counts(tree)
    return {
        (s.lineno, s.col_offset)
        for s in tree.body
        if isinstance(s, ast.ClassDef)
        and any(
            (meaning := _meaning(base, imports, counts)) is not None
            and _ENUM_LIKE.fullmatch(meaning.rsplit(".", 1)[-1])
            for base in s.bases
        )
    }


def _holders(root: Node, key: re.Pattern[str], aliased: set[tuple[int, int]]) -> list[tuple[Node, Node]]:
    """``(class, assignment)`` for every member, at any depth, of an
    enum-like class whose value spells ``key`` as a token: a base spelled
    like an enum's, or one in ``aliased``."""
    found = []
    for cls in _walk(root):
        if cls.type != "class_definition":
            continue
        bases = cls.child_by_field_name("superclasses")
        body = cls.child_by_field_name("body")
        at = (cls.start_point[0] + 1, cls.start_point[1])
        if bases is None or body is None or not (_ENUM_LIKE.search(_text(bases)) or at in aliased):
            continue
        for statement in body.named_children:
            if statement.type != "expression_statement":
                continue
            for assignment in statement.named_children:
                if assignment.type != "assignment":
                    continue
                left = assignment.child_by_field_name("left")
                right = assignment.child_by_field_name("right")
                if left is not None and left.type == "identifier" and right is not None and key.search(_text(right)):
                    found.append((cls, assignment))
    return found


def _class_node(tree: ast.Module, cls: Node) -> ast.ClassDef | None:
    """``cls`` in Python's parse, when it is a top-level statement of the module."""
    at = (cls.start_point[0] + 1, cls.start_point[1])
    return next(
        (s for s in tree.body if isinstance(s, ast.ClassDef) and (s.lineno, s.col_offset) == at),
        None,
    )


def _imports(tree: ast.Module) -> dict[str, list[str]]:
    """What each name a top-level absolute import binds means, dotted: ``a``
    for ``import a.b``, ``a.b`` for ``import a.b as m``, ``m.n`` for ``from m
    import n [as x]``. A name bound by two imports keeps both meanings."""
    found: dict[str, list[str]] = defaultdict(list)
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.asname:
                    found[_nfkc(alias.asname)].append(alias.name)
                else:
                    head = alias.name.split(".")[0]
                    found[_nfkc(head)].append(head)
        elif isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module:
            for alias in statement.names:
                found[_nfkc(alias.asname or alias.name)].append(f"{statement.module}.{alias.name}")
    return found


def _bound_counts(tree: ast.Module) -> Counter[str]:
    """How many times each name is bound anywhere in the module, in any scope,
    imports included, and each attribute name stored or deleted."""
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            counts.update(_nfkc(a.asname or a.name.split(".")[0]) for a in node.names)
        else:
            counts.update(_nfkc(name) for name in _ast_bound(node))
    return counts


def _meaning(node: ast.expr, imports: dict[str, list[str]], counts: Counter[str]) -> str | None:
    """The dotted name ``node`` (a name or an attribute chain) means through
    a top-level import, when its head is bound by exactly that import."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = _nfkc(node.id)
    meanings = imports.get(head, [])
    if len(meanings) != 1 or counts[head] != 1:
        return None
    return ".".join([meanings[0], *reversed(parts)])


def _class_kind(cls: ast.ClassDef, tree: ast.Module) -> str | None:
    """``"enum"`` or ``"choices"`` when ``cls`` is a class whose members'
    values nothing in the module can change, else None."""
    imports = _imports(tree)
    counts = _bound_counts(tree)
    if counts[_nfkc(cls.name)] != 1 or cls.keywords:
        return None
    bases = [_meaning(base, imports, counts) for base in cls.bases]
    if len(bases) == 1 and bases[0] in _STR_ENUMS:
        kind = "enum"
    elif (
        len(bases) == 2
        and isinstance(cls.bases[0], ast.Name)
        and cls.bases[0].id == "str"
        and counts["str"] == 0
        and bases[1] == "enum.Enum"
    ):
        kind = "enum"
    elif len(bases) == 1 and bases[0] in _CHOICES:
        kind = "choices"
    else:
        return None
    if any(_meaning(d, imports, counts) != "enum.unique" for d in cls.decorator_list):
        return None
    if not all(_allowed(s, i, kind, imports, counts) for i, s in enumerate(cls.body)):
        return None
    return kind


def _allowed(
    statement: ast.stmt, index: int, kind: str, imports: dict[str, list[str]], counts: Counter[str]
) -> bool:
    """Whether ``statement`` may stand in the class body: it runs no code at
    class creation and cannot change what a member's ``.value`` returns."""
    if (
        index == 0
        and isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    ):
        return True  # the docstring
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Assign):
        target = statement.targets[0] if len(statement.targets) == 1 else None
        return (
            isinstance(target, ast.Name)
            and not target.id.startswith("_")
            and _inert(statement.value, kind, imports, counts)
        )
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if statement.name.startswith("_") or statement.name in _RESERVED_METHODS:
            return False
        if any(
            not (isinstance(d, ast.Name) and d.id in _METHOD_DECORATORS and counts[d.id] == 0)
            for d in statement.decorator_list
        ):
            return False
        # Defaults and annotations are evaluated when the class is created.
        arguments = statement.args
        evaluated: list[ast.AST] = [*arguments.defaults, *(d for d in arguments.kw_defaults if d is not None)]
        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg):
            if arg is not None and arg.annotation is not None:
                evaluated.append(arg.annotation)
        if statement.returns is not None:
            evaluated.append(statement.returns)
        return not any(
            isinstance(node, (ast.Call, ast.NamedExpr)) for each in evaluated for node in ast.walk(each)
        )
    return False


def _constant(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float, complex))
    )


def _inert(node: ast.expr, kind: str, imports: dict[str, list[str]], counts: Counter[str]) -> bool:
    """Whether a member's value runs no code: a literal, a tuple of literals,
    ``auto()``, or for ``TextChoices`` a pair ending in a translation call."""
    if _constant(node):
        return True
    if isinstance(node, ast.Tuple):
        items = node.elts
        if kind == "choices" and len(items) >= 2 and _translation(items[-1], imports, counts):
            items = items[:-1]
        return all(_constant(item) for item in items)
    return (
        isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and _meaning(node.func, imports, counts) == "enum.auto"
    )


def _translation(node: ast.expr, imports: dict[str, list[str]], counts: Counter[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and not node.keywords
        and _meaning(node.func, imports, counts) in _TRANSLATIONS
        and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args)
    )


def _plain_key(right: Node, flag_key: str, kind: str) -> bool:
    """Whether a member's value is ``flag_key`` as a plain string: for
    ``TextChoices`` also the first element of a ``(value, label)`` pair."""
    if kind == "choices" and right.type in ("expression_list", "tuple"):
        items = [c for c in right.named_children if c.type != "comment"]
        if len(items) < 2:
            return False
        right = items[0]
    return right.type == "string" and _plain_string(right) == flag_key


def _plain_string(node: Node) -> str | None:
    """The text of a string with no prefix but ``r``/``u``, one content part and
    no interpolation; None for anything else."""
    if any(c.type not in ("string_start", "string_content", "string_end") for c in node.children):
        return None
    contents = [c for c in node.children if c.type == "string_content"]
    prefix = _text(node.children[0]).rstrip("'\"").lower()
    if prefix not in ("", "r", "u") or len(contents) != 1:
        return None
    return contents[0].text.decode("utf-8")


def _is_docstring(content: Node) -> bool:
    """Whether a ``string_content`` is a module, class or function docstring."""
    string = content.parent
    statement = string.parent if string is not None else None
    block = statement.parent if statement is not None else None
    if statement is None or block is None or statement.type != "expression_statement":
        return False
    if block.type not in ("block", "module") or len(statement.named_children) != 1:
        return False
    first = next((c for c in block.named_children if c.type != "comment"), None)
    return first is not None and first.id == statement.id


def _member_target(node: Node) -> bool:
    """Whether ``node`` is the name a class-body assignment binds: a member
    of that class, which no read of another class's member can reach."""
    assignment = node.parent
    if assignment is None or assignment.type != "assignment":
        return False
    left = assignment.child_by_field_name("left")
    statement = assignment.parent
    body = statement.parent if statement is not None else None
    return (
        left is not None
        and left.id == node.id
        and statement is not None
        and statement.type == "expression_statement"
        and body is not None
        and body.type == "block"
        and body.parent is not None
        and body.parent.type == "class_definition"
    )


def _handed_on(root: Node, owner: str, cls: Node) -> bool:
    """Whether the declaring module hands the class to code: an argument, an
    assignment's value, an element of a container. Such code can rewrite
    every member's ``_value_`` without spelling it (``prefixed(Flags)``).
    Reading an attribute, calling the class (a lookup by value), subscripting
    it (by name), iterating it and annotating with it hand nothing on."""
    own = cls.child_by_field_name("name")
    for node in _walk(root):
        if node.type != "identifier" or _text(node) != owner or node.id == own.id:
            continue
        if _not_a_mention(node) or _in_import(node) is not None or _in_annotation(node):
            continue
        parent = node.parent
        field = {
            "attribute": "object",
            "call": "function",
            "subscript": "value",
            "for_statement": "right",
            "for_in_clause": "right",
        }.get(parent.type if parent is not None else "")
        if field is not None and parent.child_by_field_name(field).id == node.id:
            continue
        return True
    return False


def _in_file_mentions(
    path: str, source: str, root: Node, decl: xk.KeyDeclaration, member: Node
) -> list[xk.Refusal]:
    """Every mention of the member in its own file other than its assignment:
    a read the reader invocation, which never rewrites this file, cannot fold."""
    target = member.child_by_field_name("left")
    word = xk.mention_pattern([decl.name])
    found: list[xk.Refusal] = []
    for node in _walk(root):
        spelled = (
            node.type == "identifier"
            and _text(node) == decl.name
            and node.id != target.id
            and not _member_target(node)  # another class's member of the same name
        )
        named = node.type == "string_content" and word.search(_text(node)) and not _is_docstring(node)
        if spelled or named:
            refusal = xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry)
            if refusal not in found:
                found.append(refusal)
    return found


def references(
    path: str,
    source: str,
    entries: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    """The proven reads of ``entries`` in ``path``, and the refusals.

    The owner is bound as a constant's module is (``_bindings``, with the
    class as the imported name). Every ``.X`` spelling a member is then a
    proven read, another module's own class's member, or refused. A use of
    the owner alone is not refused: whatever it hands on can reach the member
    only by spelling it, which is judged here, or dynamically, which
    :func:`enum_dynamic_uses` notes.
    """
    root = _parse(source)
    plain = _nfkc(source)
    spelling = [d for d in entries if xk.mention_pattern([d.name]).search(plain)]
    if not spelling:
        return [], []
    if root.has_error or _decoded_otherwise(source):
        return [], [xk.Refusal(path, 1, xk.UNSUPPORTED_FORM, "", d.entry) for d in spelling]
    stars = [n for n in _walk(root) if n.type == "wildcard_import"]
    namespace = [
        n for n in _walk(root) if n.type == "identifier" and _text(n) in _NAMESPACE and not _not_a_mention(n)
    ]
    sys_modules = _sys_modules(root)
    dynamic = _DYNAMIC.search(plain)
    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []

    def refuse(node: Node, reason: str, decl: xk.KeyDeclaration) -> None:
        refusal = xk.refusal(path, node, reason, source, decl.entry)
        if refusal not in refused:
            refused.append(refusal)

    for decl in spelling:
        bindings = _bindings(root, path, repo_dir, decl)
        for binding in bindings:
            if binding.statement.parent is None or binding.statement.parent.type != "module":
                refuse(binding.statement, xk.UNSUPPORTED_FORM, decl)
            elif binding.ambiguous:
                refuse(binding.statement, xk.IMPORT_UNRESOLVED, decl)
        proven = [
            b for b in bindings
            if b.statement.parent is not None and b.statement.parent.type == "module" and not b.ambiguous
        ]
        if len(proven) != len(bindings):
            continue
        owners = {(b.local if b.kind == "bare" else f"{b.owner}.{decl.owner}"): b for b in proven}
        word = xk.mention_pattern([decl.name])
        for node in _walk(root):
            if node.type == "string_content" and word.search(_text(node)):
                # `Flags["X"]`, `getattr(Flags, "X")`; or, where nothing binds
                # the owner, `mock.patch("app.flags.Flags.X")`.
                refuse(node, xk.NOT_A_READ if proven else xk.UNSUPPORTED_FORM, decl)
        if proven:
            for node in stars:
                refuse(node, xk.IMPORT_UNRESOLVED, decl)
            for node in namespace + sys_modules:
                refuse(node, xk.UNSUPPORTED_FORM, decl)
            if dynamic:
                line = plain.count("\n", 0, dynamic.start()) + 1
                refusal = xk.Refusal(path, line, xk.UNSUPPORTED_FORM, _line(plain, line), decl.entry)
                if refusal not in refused:
                    refused.append(refusal)
        for binding in proven:
            if binding.kind == "bare":
                for node in _walk(root):
                    if node.type == "identifier" and _text(node) == binding.local and not _not_a_mention(node):
                        reason = _owner_use(node, binding)
                        if reason is not None:
                            refuse(node, reason, decl)
            else:
                head = binding.owner.split(".")[0]
                twins = frozenset(
                    b.name_node.id
                    for b in proven
                    if b.kind == "qualified" and b.local == binding.local and b.owner == binding.owner
                )
                for node in _walk(root):
                    if node.type == "identifier" and _text(node) == head and not _not_a_mention(node):
                        reason = _head_use(node, binding, twins)
                        if reason is not None:
                            refuse(node, reason, decl)
        for node in _walk(root):
            if node.type != "identifier" or _text(node) != decl.name or _in_import(node) is not None:
                continue
            parent = node.parent
            if parent is not None and parent.type == "dotted_name" and parent.named_children[0].id != node.id:
                refuse(node, xk.UNSUPPORTED_FORM, decl)  # `case Flags.X:`
                continue
            if parent is None or parent.type != "attribute" or not _not_a_mention(node):
                continue  # a bare X: a member is only reached through its class
            qualifier = parent.child_by_field_name("object")
            spelled = _text(qualifier).replace(" ", "")
            if spelled in owners:
                reason = _member_read(parent, spelled, decl, accessors)
                if reason is None:
                    reads.append(xk.KeyReference(path, spelled, decl.name, "value", decl))
                else:
                    refuse(node, reason, decl)
            elif stars:
                refuse(node, xk.IMPORT_UNRESOLVED, decl)
            elif not _someone_elses_class(root, qualifier, path, repo_dir, decl):
                refuse(node, xk.UNSUPPORTED_FORM, decl)
    if refused:
        return [], refused
    return reads, refused


def _member_read(attribute: Node, owner: str, decl: xk.KeyDeclaration, accessors: frozenset[str]) -> str | None:
    """None when ``owner.X`` at ``attribute`` is a proven read, else the refusal."""
    if _raw(attribute) != f"{owner}.{decl.name}":
        # Spelled with whitespace or across lines: Python reads it as the
        # owner's, but the reader rule's `#eq?` compares the text as written.
        return xk.UNSUPPORTED_FORM
    outer = attribute.parent
    if outer is None or outer.type != "attribute" or outer.child_by_field_name("object").id != attribute.id:
        return xk.NOT_A_READ  # the member itself, handed on
    if _text(outer.child_by_field_name("attribute")) != "value" or _raw(outer) != f"{owner}.{decl.name}.value":
        return xk.UNSUPPORTED_FORM  # `.name`, `.label`, a method
    return None if _is_read_at(outer, accessors) else xk.NOT_A_READ


def _owner_use(node: Node, binding: _Binding) -> str | None:
    """None when a token spelling a bare binding's owner keeps the binding."""
    statement = _in_import(node)
    if statement is not None:
        bound = [entry for entry, local in _import_bound_names(statement) if local == binding.local]
        if statement.id == binding.statement.id and any(e.id == binding.name_node.id for e in bound):
            return None
        return xk.REBOUND if bound else None
    return xk.REBOUND if key_const._python_is_binding(node) else None


def _someone_elses_class(root: Node, qualifier: Node, path: str, repo_dir: str, decl: xk.KeyDeclaration) -> bool:
    """Whether ``qualifier.X`` surely reads a member of another module's own class.

    The qualifier is a name or a dotted chain whose head is bound only by
    top-level imports, each meaning exactly one file, not ``decl``'s, that
    defines the qualifier's last name itself. ``self.X`` is not: ``self`` is
    no import, so it might be anything, including the enum.
    """
    parts = _text(qualifier).replace(" ", "").split(".")
    if not all(part.isidentifier() for part in parts):
        return False
    head = parts[0]
    found = False
    for node in _walk(root):
        if node.type != "identifier" or _text(node) != head or _not_a_mention(node):
            continue
        statement = _in_import(node)
        if statement is None:
            parent = node.parent
            if parent is None or parent.type != "attribute" or parent.child_by_field_name("object").id != node.id:
                return False
            continue
        if statement.parent is None or statement.parent.type != "module":
            return False
        for entry, local in _import_bound_names(statement):
            if local != head:
                continue
            target = _class_file(statement, entry, parts, path, repo_dir)
            if target is None or (target == decl.path and parts[-1] == decl.owner):
                return False
            if not _defines_class(target, parts[-1]):
                return False
            found = True
    return found


def _class_file(statement: Node, entry: Node, parts: list[str], path: str, repo_dir: str) -> str | None:
    """The one file whose top-level ``parts[-1]`` the qualifier means through
    this import entry, or None when it could be another."""
    if len(parts) == 1:
        if statement.type != "import_from_statement":
            return None
        files = _resolve(repo_dir, path, _module_of(statement.child_by_field_name("module_name")))
        if len(files) != 1 or _loads_other_code(Path(files[0]), repo_dir):
            return None
        return files[0]
    return _target_of(statement, entry, ".".join(parts[:-1]), path, repo_dir)


def _defines_class(path: str, name: str) -> bool:
    """Whether the file at ``path`` defines a class ``name`` at top level, and
    binds that name nowhere else. An assignment is not one: ``Config = Flags``
    is the enum under another name."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    tree = _python_parse(text, _parse(text))
    if tree is None or _bound_counts(tree)[name] != 1:
        return False
    return any(isinstance(s, ast.ClassDef) and s.name == name for s in tree.body)


def enum_dynamic_uses(
    path: str, source: str, entries: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """The ``enum-dynamic`` notes for ``path``: each use of an entry's class
    that can reach its members without naming one -- a call, a subscript,
    iteration, the class handed on, ``__members__``, Django's ``choices``.
    Matched on the name without proving which class it means: a note only
    asks a reviewer to look. A read of a declared member, an import, a
    binding and an annotation are not uses."""
    root = _parse(source)
    by_owner: dict[str, list[xk.KeyDeclaration]] = defaultdict(list)
    for decl in entries:
        if decl.owner is not None:
            by_owner[decl.owner].append(decl)
    members = {owner: _members(decls[0].path, owner) for owner, decls in by_owner.items()}
    notes: list[xk.Refusal] = []
    for node in _walk(root):
        if node.type != "identifier" or _text(node) not in by_owner or _in_import(node) is not None:
            continue
        owner = _text(node)
        parent = node.parent
        if (
            parent is not None
            and parent.type == "keyword_argument"
            and parent.child_by_field_name("name").id == node.id
        ):
            continue
        use = node
        if parent is not None and parent.type == "attribute" and parent.child_by_field_name("attribute").id == node.id:
            use = parent  # `m.Flags`
        if use.id == node.id and key_const._python_is_binding(node):
            continue  # the class's own name, a parameter: not a use
        if _in_annotation(use):
            continue
        outer = use.parent
        if (
            outer is not None
            and outer.type == "attribute"
            and outer.child_by_field_name("object").id == use.id
            and _text(outer.child_by_field_name("attribute")) in members[owner]
        ):
            continue  # `Flags.M`: judged as a mention of M
        for decl in by_owner[owner]:
            note = xk.refusal(path, use, xk.ENUM_DYNAMIC, source, decl.entry)
            if note not in notes:
                notes.append(note)
    return notes


def _members(path: str, owner: str) -> set[str]:
    """The names the top-level class ``owner`` in ``path`` assigns in its body."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    found: set[str] = set()
    for statement in _parse(text).named_children:
        if statement.type == "decorated_definition":
            statement = statement.child_by_field_name("definition")
        if statement is None or statement.type != "class_definition":
            continue
        if _text(statement.child_by_field_name("name")) != owner:
            continue
        for line in statement.child_by_field_name("body").named_children:
            for assignment in line.named_children if line.type == "expression_statement" else ():
                left = assignment.child_by_field_name("left") if assignment.type == "assignment" else None
                if left is not None and left.type == "identifier":
                    found.add(_text(left))
    return found


def _in_annotation(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == "type":
            return True
        current = current.parent
    return False


#: Enum internals: a write through one changes what a member's ``.value``
#: returns, or what a lookup finds, without naming the member.
_INTERNALS = re.compile(r"(?<!\w)(?:_value_|_member_map_|_value2member_map_)(?!\w)")
#: Calls whose string argument names an attribute they write or replace:
#: ``setattr``, ``delattr``, ``monkeypatch.setattr``, ``mock.patch``,
#: ``mock.patch.object``.
_WRITERS = frozenset({"setattr", "delattr", "patch", "object"})


def enum_escapes(
    path: str, source: str, entries: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """Refusals of every entry of a class at each place in ``path`` that can
    change what its members' ``.value`` returns: an enum internal spelled
    anywhere, the class's module attribute stored or deleted, an attribute of
    the class stored or deleted (``Flags.value = property(...)``), the class
    handed to ``setattr``/``delattr``, or a writer call naming the class in a
    string."""
    root = _parse(source)
    found: list[xk.Refusal] = []

    def refuse(node: Node, owners: set[str]) -> None:
        for decl in entries:
            if decl.owner in owners:
                refusal = xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry)
                if refusal not in found:
                    found.append(refusal)

    owners = {d.owner for d in entries if d.owner is not None}
    for node in _walk(root):
        if node.type in ("identifier", "string_content") and _INTERNALS.search(_text(node)):
            refuse(node, owners)
        elif node.type == "attribute":
            name = node.child_by_field_name("attribute")
            target = _last_name(node.child_by_field_name("object"))
            if not _stored_or_deleted(node):
                continue
            if name is not None and _text(name) in owners:
                refuse(node, {_text(name)})
            elif target in owners:
                refuse(node, {target})
        elif node.type == "call" and _callee(node) in ("setattr", "delattr"):
            arguments = node.child_by_field_name("arguments")
            first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
            if first is not None and _last_name(first) in owners:
                refuse(node, {_last_name(first)})
        elif node.type == "string_content" and _written_by_name(node):
            named = {o for o in owners if re.fullmatch(rf"(?:\w+\.)*{re.escape(o)}(?:\.\w+)*", _text(node))}
            if named:
                refuse(node, named)
    return found


def _last_name(node: Node | None) -> str | None:
    """The last name of a name or attribute chain: ``Flags`` for ``app.flags.Flags``."""
    if node is None:
        return None
    if node.type == "attribute":
        node = node.child_by_field_name("attribute")
    return _text(node) if node is not None and node.type == "identifier" else None


def _callee(call: Node) -> str | None:
    return _last_name(call.child_by_field_name("function"))


def enum_module_pattern(entries: tuple[xk.KeyDeclaration, ...], repo_dir: str) -> re.Pattern[str]:
    """What a file's text holds when it can rewrite a member without naming
    the enum or the member: an enum internal (``m._value_ = ...`` over a class
    handed to it). Such a file joins the ones :func:`enum_escapes` is asked of."""
    return _INTERNALS


def _stored_or_deleted(attribute: Node) -> bool:
    if key_const._python_is_binding(attribute):
        return True
    current = attribute.parent
    while current is not None and current.type == "expression_list":
        current = current.parent
    return current is not None and current.type == "delete_statement"


def _written_by_name(content: Node) -> bool:
    """Whether a string is an argument of a call that writes the attribute it names."""
    string = content.parent
    arguments = string.parent if string is not None else None
    call = arguments.parent if arguments is not None else None
    if arguments is None or arguments.type != "argument_list" or call is None or call.type != "call":
        return False
    return _callee(call) in _WRITERS
