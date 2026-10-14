"""PHP profile for cross-file key constants.

A declaration is a single-element ``const`` whose value is exactly the key
(:func:`flag_cleanup.key_const._php_key_consts`), either in a class,
interface or enum declared at the top of the file or of a namespace block,
and not ``private``, or at namespace level. Its owner is the class-like's name
and its module the enclosing namespace, both lowercased: PHP resolves class
and namespace names case-insensitively. A namespace constant has no owner.
Constant names keep their case, as PHP compares them.

A trait's constant is never offered: PHP rejects ``Trait::NAME``, so no read
of one could be proven, and the in-file rules already handle one that no
other file mentions.

A mention in another file is proven in two shapes, only as the whole first
argument of a flag read:

* ``Owner::NAME`` -- the scope, spelled only as names and backslashes,
  resolves to the owner's fully qualified name through the namespace
  section's ``use`` imports written before it, or the section's namespace.
  PHP classes have no global fallback, so this is lexical and complete.
* ``NAME`` -- a ``use const`` of the constant earlier in the section, or a
  reader in the constant's own namespace, or, for a global constant, a reader
  with no namespace.

Anything that may be the constant but is not proven is refused, and the
declaration is kept: a read through a subclass, implementer or trait user;
``self::``, ``static::`` or ``parent::`` that may reach it; ``$x::NAME``; a
qualified constant read (``\\Ns\\NAME``); an import of the name written after
the read; and the name in a string (``constant('Keys::NAME')``) or in inline
HTML (a Blade echo). A mention that can only be another class's own constant
(``Other::NAME`` for a repository class that does not reach the owner) is
skipped.

Repository classes come from an index over every PHP file except Composer's
``vendor/`` directory, whose code cannot name the application's constants.
"""

from __future__ import annotations

import functools
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import key_const, key_const_visibility, syntax

#: The suffixes the runner treats as PHP. Spelled out rather than imported
#: from the runner, which imports the shared module at load; a test holds the
#: two equal.
PHP_EXTENSIONS: tuple[str, ...] = (".php", ".phtml", ".inc", ".module")

#: Class-likes whose constants another file can name as ``Owner::NAME``.
_OWNERS = frozenset({"class_declaration", "interface_declaration", "enum_declaration"})
#: Every class-like: what can compete with an owner, or stand between a
#: reader's class and it.
_CLASS_LIKES = _OWNERS | {"trait_declaration"}
_BODIES = frozenset({"declaration_list", "enum_declaration_list"})
#: The ways a class name is written.
_CLASS_NAMES = frozenset({"name", "qualified_name", "relative_name"})


def _typed(root: Node, types: frozenset[str] | set[str], nodes: dict[str, list[Node]] | None) -> list[Node]:
    """Every node of ``types`` under ``root``: from ``nodes`` (one walk's
    buckets, see :func:`_by_type`) when given, else by walking."""
    if nodes is not None:
        return [n for t in types for n in nodes.get(t, ())]
    return [n for n in xk.walk(root) if n.type in types]


def _by_type(root: Node) -> dict[str, list[Node]]:
    """One walk of ``root``, bucketed by node type, for several collectors."""
    nodes: dict[str, list[Node]] = defaultdict(list)
    for node in xk.walk(root):
        nodes[node.type].append(node)
    return nodes


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _field(parent: Node, child: Node) -> str | None:
    for index, candidate in enumerate(parent.children):
        if candidate.id == child.id:
            return parent.field_name_for_child(index)
    return None


def _join(namespace: str, parts: list[str]) -> str:
    """A fully qualified name, lowercased: PHP ignores case in class and
    namespace names."""
    return "\\".join([namespace, *parts] if namespace else parts).lower()


def _segments(node: Node | None) -> tuple[str, list[str]] | None:
    """How a class or namespace name is written: ``("fq", parts)`` behind a
    leading backslash, ``("rel", parts)`` behind ``namespace\\``, otherwise
    ``("plain", parts)``. None for anything else (``$x``, an expression)."""
    if node is None:
        return None
    if node.type == "name":
        return "plain", [_text(node)]
    if node.type not in ("namespace_name", "qualified_name", "relative_name"):
        return None
    parts: list[str] = []
    for child in node.named_children:
        if child.type == "name":
            parts.append(_text(child))
        elif child.type == "namespace_name":
            parts.extend(_text(n) for n in child.named_children if n.type == "name")
        else:
            return None
    if not parts:
        return None
    if node.type == "relative_name":
        return "rel", parts
    leading = node.children[0] if node.children else None
    return ("fq" if leading is not None and leading.type == "\\" else "plain"), parts


def _spelled(kind: str, parts: list[str]) -> str:
    """The canonical text of a name, as a reader rule compares it."""
    return {"fq": "\\", "rel": "namespace\\"}.get(kind, "") + "\\".join(parts)


# --- namespace sections and imports ----------------------------------------------


@dataclass(frozen=True)
class _Section:
    """One namespace's stretch of a file: ``namespace X;`` up to the next
    ``namespace``, a braced ``namespace X { }`` body, or the whole of a file
    that declares none. A ``use`` applies only inside its own section."""

    namespace: str  # lowercased; "" for the global namespace
    start: int
    end: int


def _sections(root: Node) -> list[_Section]:
    definitions = [c for c in root.named_children if c.type == "namespace_definition"]
    if not definitions:
        return [_Section("", 0, root.end_byte)]
    found: list[_Section] = []
    for index, definition in enumerate(definitions):
        written = _segments(definition.child_by_field_name("name"))
        namespace = _join("", written[1]) if written is not None else ""
        body = definition.child_by_field_name("body")
        if body is not None:
            found.append(_Section(namespace, body.start_byte, body.end_byte))
            continue
        end = definitions[index + 1].start_byte if index + 1 < len(definitions) else root.end_byte
        found.append(_Section(namespace, definition.end_byte, end))
    return found


def _section_at(sections: list[_Section], byte: int) -> _Section | None:
    return next((s for s in sections if s.start <= byte < s.end), None)


@dataclass(frozen=True)
class _Import:
    """One clause of a ``use`` declaration."""

    kind: str  # "class", "const", "function", or "unreadable"
    #: The imported name as written, without a leading backslash.
    target: str
    #: The name it binds in its section: the alias, else the last segment.
    alias: str
    aliased: bool
    #: Where its declaration starts: an import applies only after it.
    start: int
    node: Node


def _use_kind(node: Node) -> str | None:
    """``const`` or ``function`` when ``node`` carries that keyword."""
    return next(
        (c.type for c in node.children if not c.is_named and c.type in ("const", "function")),
        None,
    )


def _imports(root: Node, nodes: dict[str, list[Node]] | None = None) -> list[_Import]:
    found: list[_Import] = []
    for declaration in _typed(root, {"namespace_use_declaration"}, nodes):
        group = declaration.child_by_field_name("body")
        prefix: list[str] = []
        if group is not None:
            written = _segments(
                next((c for c in declaration.named_children if c.type == "namespace_name"), None)
            )
            if written is None:
                found.append(_Import("unreadable", "", "", False, declaration.start_byte, declaration))
                continue
            prefix = written[1]
            clauses = [c for c in group.named_children if c.type == "namespace_use_clause"]
        else:
            clauses = [c for c in declaration.named_children if c.type == "namespace_use_clause"]
        for clause in clauses:
            alias = clause.child_by_field_name("alias")
            written = _segments(
                next(
                    (
                        c
                        for c in clause.named_children
                        if c.type in ("name", "qualified_name")
                        and (alias is None or c.id != alias.id)
                    ),
                    None,
                )
            )
            if written is None:
                found.append(_Import("unreadable", "", "", False, declaration.start_byte, clause))
                continue
            parts = prefix + written[1]
            found.append(
                _Import(
                    _use_kind(clause) or _use_kind(declaration) or "class",
                    "\\".join(parts),
                    _text(alias) if alias is not None else parts[-1],
                    alias is not None,
                    declaration.start_byte,
                    clause,
                )
            )
    return found


def _bound(
    imports: list[_Import], section: _Section, kind: str, alias: str, at: int, *, fold: bool
) -> tuple[list[_Import], bool]:
    """The imports of ``kind`` binding ``alias`` in ``section`` before ``at``,
    and whether one binds it at or after ``at``. An import that cannot be read
    counts as binding everything after it, so it can only refuse."""
    before: list[_Import] = []
    later = False
    for each in imports:
        if not section.start <= each.start < section.end:
            continue
        if each.kind == "unreadable":
            later = True
            continue
        if each.kind != kind:
            continue
        if (each.alias.lower() == alias.lower()) if fold else (each.alias == alias):
            if each.start < at:
                before.append(each)
            else:
                later = True
    return before, later


def _resolve_class(sections: list[_Section], imports: list[_Import], scope: Node) -> str | None:
    """The lowercased fully qualified name a class name means at ``scope``.

    None when it cannot be told: an import written after it, or one that
    cannot be read, could decide it, or two bind it (a fatal error PHP raises
    at compile time, so never in running code).
    """
    written = _segments(scope)
    section = _section_at(sections, scope.start_byte)
    if written is None or section is None:
        return None
    kind, parts = written
    if kind == "fq":
        return _join("", parts)
    if kind == "rel":
        return _join(section.namespace, parts)
    before, later = _bound(imports, section, "class", parts[0], scope.start_byte, fold=True)
    if later or len(before) > 1:
        return None
    if before:
        return _join("", [*before[0].target.split("\\"), *parts[1:]])
    return _join(section.namespace, parts)


# --- declarations -----------------------------------------------------------------


def _top_level(node: Node) -> bool:
    """Whether ``node`` is a statement of the file or of a braced namespace."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "program":
        return True
    return (
        parent.type == "compound_statement"
        and parent.parent is not None
        and parent.parent.type == "namespace_definition"
    )


def _backing(enum: Node) -> str | None:
    """The backing type an enum declares (``string``, ``int``), lowercased;
    None for a pure enum."""
    typ = next((c for c in enum.named_children if c.type == "primitive_type"), None)
    return _text(typ).lower() if typ is not None else None


def _enum_entries(
    path: str, root: Node, source: str, flag_key: str
) -> tuple[list[xk.KeyDeclaration], list[xk.Refusal]]:
    """The provable cases holding ``flag_key``, and a refusal for every other one.

    A case is proven when its enum is string-backed and declared at the top of
    the file or of a braced namespace, its value is the key as a plain string,
    and the file parses whole. ``->value`` on a backed case is a built-in
    readonly property no enum can intercept (an enum declares no ``__get`` and
    cannot be extended), so it is the case's only accessor.
    """
    sections = _sections(root)
    held = xk.key_token(flag_key)
    found: list[xk.KeyDeclaration] = []
    refused: list[xk.Refusal] = []
    for enum in xk.walk(root):
        if enum.type != "enum_declaration":
            continue
        name = enum.child_by_field_name("name")
        body = enum.child_by_field_name("body")
        if name is None or body is None:
            continue
        holding = [
            case
            for case in body.named_children
            if case.type == "enum_case"
            and (value := case.child_by_field_name("value")) is not None
            and held.search(_text(value))
        ]
        section = _section_at(sections, enum.start_byte)
        for case in holding:
            case_name = _text(case.child_by_field_name("name"))
            entry = f"{_text(name)}::{case_name}"
            if not _top_level(enum) or section is None:
                refused.append(xk.refusal(path, case, xk.UNSUPPORTED_FORM, source, entry))
                continue
            if (
                root.has_error
                or _backing(enum) != "string"
                or _literal(case.child_by_field_name("value")) != flag_key
            ):
                refused.append(xk.refusal(path, case, xk.ENUM_UNPROVEN, source, entry))
                continue
            found.append(
                xk.KeyDeclaration(
                    path,
                    case_name,
                    _text(name).lower(),
                    section.namespace,
                    kind=xk.ENUM_ENTRY,
                    accessors=frozenset({"value"}),
                    entry_text=entry,
                )
            )
    return found, refused


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """The cases that cannot be proven, and mentions of proven ones in their own file.

    The reader invocation never rewrites the declaring file, and a mention
    there (``self::NAME->value``, ``const ALIAS = self::NAME;``, a
    ``constant('self::NAME')`` string) is a read or a second path to the case
    that no rule follows, so it is reported rather than left silently.
    """
    root = syntax._parse(source, "php")
    found, refused = _enum_entries(path, root, source, flag_key)
    for decl in found:
        token = xk.mention_pattern([decl.name])
        for node in xk.walk(root):
            if node.type == "name" and _text(node) == decl.name:
                if node.parent is not None and node.parent.type == "enum_case":
                    continue  # a case's own name
            elif not (node.type in _TEXT_NODES and token.search(_text(node))):
                continue
            refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry))
            break
    return refused


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    root = syntax._parse(source, "php")
    sections = _sections(root)
    found: list[xk.KeyDeclaration] = []
    for const in key_const.key_consts(root, source.encode("utf-8"), "php", flag_key):
        if not key_const_visibility.visible_key_consts([const], "php"):
            continue  # private, or local: no other file can name it
        node = const.declaration
        body = node.parent
        if body is not None and body.type in _BODIES:
            owner = body.parent
            name = owner.child_by_field_name("name") if owner is not None else None
            if owner is None or owner.type not in _OWNERS or name is None or not _top_level(owner):
                continue  # a trait, an anonymous class, or one declared conditionally
            owner_name: str | None = _text(name).lower()
        elif _top_level(node):
            owner_name = None
        else:
            continue
        section = _section_at(sections, node.start_byte)
        if section is not None:
            found.append(xk.KeyDeclaration(path, const.name, owner_name, section.namespace))
    entries, _refused = _enum_entries(path, root, source, flag_key)
    if vendored(Path(path).resolve()):
        return [replace(d, third_party=True) for d in found + entries]
    return found + entries


def _class_likes(
    root: Node, sections: list[_Section], nodes: dict[str, list[Node]] | None = None
) -> list[tuple[str, str, Node]]:
    """``(namespace, name as written, node)`` of every named class-like in the
    file, wherever it is declared: a conditional declaration competes too."""
    found: list[tuple[str, str, Node]] = []
    for node in _typed(root, _CLASS_LIKES, nodes):
        name = node.child_by_field_name("name")
        section = _section_at(sections, node.start_byte)
        if name is not None and section is not None:
            found.append((section.namespace, _text(name), node))
    return found


def _constant_names(class_like: Node) -> set[str]:
    """Every constant and enum case a class-like declares directly."""
    body = class_like.child_by_field_name("body")
    names: set[str] = set()
    for child in body.named_children if body is not None else []:
        if child.type == "const_declaration":
            for element in child.named_children:
                if element.type != "const_element":
                    continue
                name = next((c for c in element.named_children if c.type == "name"), None)
                if name is not None:
                    names.add(_text(name))
        elif child.type == "enum_case":
            name = child.child_by_field_name("name")
            if name is not None:
                names.add(_text(name))
    return names


def _literal(node: Node | None) -> str | None:
    """A plain string literal's text, backslashes unescaped and any leading
    one dropped; None for interpolation or anything that is not a string."""
    if node is None or node.type not in ("string", "encapsed_string"):
        return None
    raw = _text(node)
    if len(raw) < 2 or raw[0] not in "'\"" or raw[-1] != raw[0]:
        return None
    body = raw[1:-1]
    if raw[0] == '"' and ("$" in body or "{" in body):
        return None
    return body.replace("\\\\", "\\").lstrip("\\")


def _arguments(call: Node) -> list[Node | None]:
    """The value of each positional argument of a call; None for a named or
    spread one."""
    arguments = call.child_by_field_name("arguments")
    values: list[Node | None] = []
    for argument in arguments.named_children if arguments is not None else []:
        if argument.type != "argument" or len(argument.named_children) != 1:
            values.append(None)
            continue
        values.append(argument.named_children[0])
    return values


def _runtime_names(
    root: Node,
    sections: list[_Section],
    imports: list[_Import],
    nodes: dict[str, list[Node]] | None = None,
) -> tuple[set[xk.QualifiedName], set[tuple[str, str]]]:
    """What ``define()`` and ``class_alias()`` declare at run time.

    A constant ``define()``d, or a class aliased, under the owner's name can
    stand in for a declaration, so each counts against it. One whose name
    cannot be read might be anything, and makes the file unreadable.
    """
    names: set[xk.QualifiedName] = set()
    classes: set[tuple[str, str]] = set()
    for call in _typed(root, {"function_call_expression"}, nodes):
        written = _segments(call.child_by_field_name("function"))
        callee = written[1][-1].lower() if written is not None else ""
        if callee not in ("define", "class_alias"):
            continue
        values = _arguments(call)
        if callee == "define":
            target = _literal(values[0] if values else None)
            if not target:
                raise xk.DeclarationsUnreadable(call)
            *namespace, name = target.split("\\")
            names.add((_join("", namespace), None, name))
            continue
        alias = values[1] if len(values) > 1 else None
        target = _literal(alias)
        if target is None and alias is not None and alias.type == "class_constant_access_expression":
            parts = alias.named_children
            if len(parts) == 2 and _text(parts[1]).lower() == "class":
                target = _resolve_class(sections, imports, parts[0])
        if not target:
            raise xk.DeclarationsUnreadable(call)
        *namespace, name = target.split("\\")
        classes.add((_join("", namespace), name.lower()))
    return names, classes


def _vendored_or_unreadable(read):
    """``read`` for a file, except that a Composer ``vendor/`` file (see
    :func:`vendored`) this cannot read declares nothing instead of failing
    closed. A real vendor tree holds dozens of files this grammar cannot parse
    or whose ``define()`` names are computed, and each would void every key
    whose owner it happens to spell (``keys`` is a common word). A vendored
    file that does parse still competes: a path repository can copy the
    application's own classes there."""

    def guarded(path: str, source: str):
        try:
            return read(path, source)
        except xk.DeclarationsUnreadable:
            if vendored(Path(path)):
                return set()
            raise

    guarded.__doc__ = read.__doc__
    return guarded


@functools.lru_cache(maxsize=1024)
def _declared(source: str) -> tuple[frozenset[xk.QualifiedName], frozenset[tuple[str, str]]] | None:
    """:func:`declared_names` and :func:`declared_classifiers` of one text,
    from one parse and one walk, or None when they cannot all be read.

    Cached by text: the competition check asks both of every file that spells
    a name or an owner, for every flag of a run, and on a real Laravel
    checkout that is hundreds of vendored files spelling ``keys``.
    """
    root = syntax._parse(source, "php")
    if root.has_error:
        return None
    nodes = _by_type(root)
    sections = _sections(root)
    class_likes = _class_likes(root, sections, nodes)
    names: set[xk.QualifiedName] = {
        (namespace, name.lower(), member)
        for namespace, name, node in class_likes
        for member in _constant_names(node)
    }
    for node in nodes.get("const_declaration", ()):
        if node.parent is not None and node.parent.type in _BODIES:
            continue
        section = _section_at(sections, node.start_byte)
        for element in node.named_children:
            name = next((c for c in element.named_children if c.type == "name"), None)
            if element.type == "const_element" and name is not None and section is not None:
                names.add((section.namespace, None, _text(name)))
    try:
        defined, aliased = _runtime_names(root, sections, _imports(root, nodes), nodes)
    except xk.DeclarationsUnreadable:
        return None
    classifiers = {(namespace, name.lower()) for namespace, name, _node in class_likes}
    return frozenset(names | defined), frozenset(classifiers | aliased)


@_vendored_or_unreadable
def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every constant and enum case of every class-like here, every namespace
    constant, and every ``define()``, whatever its value or visibility."""
    declared = _declared(source)
    if declared is None:
        raise xk.DeclarationsUnreadable(path)
    return set(declared[0])


@_vendored_or_unreadable
def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """``(namespace, name)`` of every class-like here and every ``class_alias()``."""
    declared = _declared(source)
    if declared is None:
        raise xk.DeclarationsUnreadable(path)
    return set(declared[1])


# --- the class index --------------------------------------------------------------


def vendored(path: Path) -> bool:
    """Whether ``path`` sits in a Composer ``vendor/`` directory: one beside a
    ``composer.json``. A dependency's code cannot name the application's own
    constants without a dependency cycle, and parsing it dominates the cost."""
    return any(p.name == "vendor" and (p.parent / "composer.json").is_file() for p in path.parents)


@dataclass(frozen=True)
class _ClassInfo:
    fqn: str
    kind: str
    #: What it extends, implements and uses (traits), as lowercased fully
    #: qualified names; None for one that cannot be resolved.
    parents: tuple[str | None, ...]
    #: Its own constants and enum cases.
    constants: frozenset[str]


@dataclass(frozen=True)
class _Share:
    classes: tuple[_ClassInfo, ...]
    #: Every name a ``class_alias()`` here makes an alias.
    aliases: frozenset[str] = frozenset()


def _parents(node: Node, sections: list[_Section], imports: list[_Import]) -> tuple[str | None, ...]:
    found: list[str | None] = []
    for child in node.named_children:
        if child.type in ("base_clause", "class_interface_clause"):
            found.extend(
                _resolve_class(sections, imports, n) for n in child.named_children if n.type in _CLASS_NAMES
            )
    body = node.child_by_field_name("body")
    for child in body.named_children if body is not None else []:
        if child.type == "use_declaration":
            found.extend(
                _resolve_class(sections, imports, n) for n in child.named_children if n.type in _CLASS_NAMES
            )
    return tuple(found)


def _share(text: str) -> tuple[_Share, bool]:
    root = syntax._parse(text, "php")
    sections, imports = _sections(root), _imports(root)
    classes = tuple(
        _ClassInfo(
            _join(namespace, [name]),
            node.type,
            _parents(node, sections, imports),
            frozenset(_constant_names(node)),
        )
        for namespace, name, node in _class_likes(root, sections)
    )
    try:
        aliases = frozenset(
            _join(namespace, [name]) for namespace, name in _runtime_names(root, sections, imports)[1]
        )
    except xk.DeclarationsUnreadable:
        return _Share(classes), False  # an alias of anything, under any name
    return _Share(classes, aliases), not root.has_error


@dataclass(frozen=True)
class _Index:
    by_fqn: dict[str, tuple[_ClassInfo, ...]]
    #: A PHP file could not be read or parsed whole: it may declare anything.
    incomplete: bool
    #: Names some ``class_alias()`` binds. Which class one stands for is a
    #: run-time fact, so it may be the owner, whatever else declares the name
    #: (an IDE stub, an ``if (false) { class X {} }`` shim).
    aliases: frozenset[str] = frozenset()

    def reaches(self, fqn: str, owner: str) -> bool:
        """Whether the class ``fqn`` may be, or inherit from, ``owner``.

        Fails closed: an incomplete index, or an ancestor outside it or that
        cannot be resolved (it may be a ``class_alias`` of the owner), may
        reach anything.
        """
        if self.incomplete:
            return True
        pending: list[str | None] = [fqn]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current is None or current == owner or current in self.aliases:
                return True
            if current in seen:
                continue
            seen.add(current)
            infos = self.by_fqn.get(current)
            if not infos:
                return True
            for info in infos:
                pending.extend(info.parents)
        return False

    def unrelated(self, fqn: str, owner: str) -> bool:
        """Whether ``fqn`` is surely a repository class that is not the owner
        and does not inherit from it: ``fqn::NAME`` is then its own constant."""
        return fqn != owner and fqn in self.by_fqn and not self.reaches(fqn, owner)


def _combine(shares: list[tuple[str, _Share]], incomplete: bool, _unreadable: bool) -> _Index:
    by_fqn: dict[str, list[_ClassInfo]] = defaultdict(list)
    aliases: set[str] = set()
    for _path, share in shares:
        for info in share.classes:
            by_fqn[info.fqn].append(info)
        aliases |= share.aliases
    return _Index({k: tuple(v) for k, v in by_fqn.items()}, incomplete, frozenset(aliases))


#: Every PHP class-like in a checkout, Composer's ``vendor/`` aside.
php_index = xk.CheckoutIndex("PHP", PHP_EXTENSIONS, _share, _combine, exclude=vendored)


# --- proving a read ---------------------------------------------------------------

#: Where a string's or an inline-HTML run's characters live. ``constant()`` and
#: ``defined()`` reach a constant through text, and a Blade echo is inline HTML.
_TEXT_NODES = frozenset({"string_content", "nowdoc_string", "text"})
#: Parents under which a ``name`` is never a constant read.
_NEVER_A_CONSTANT = frozenset(
    {
        "namespace_name", "namespace_use_clause", "named_type", "base_clause",
        "class_interface_clause", "use_declaration", "use_instead_of_clause",
        "use_as_clause", "object_creation_expression", "attribute", "type_list",
        "variable_name", "named_label_statement", "goto_statement", "declare_directive",
    }
)
#: Fields that never hold a constant read: a member, method, function,
#: declaration or named-argument name, a class scope, a type.
_NEVER_A_CONSTANT_FIELDS = frozenset({"name", "function", "scope", "type", "return_type"})


def _position(node: Node) -> str:
    """What a ``name`` (or a qualified name) is where it stands.

    ``member``: the constant of a class constant access. ``class``: the scope
    of one, which names a class. ``qualified``: a name inside a qualified
    constant read (``\\Ns\\NAME``). ``constant``: a bare constant read.
    ``other``: a declaration, member, function, type, label or import.
    Decided by elimination, so an unlisted position is read as a constant
    and can only refuse.
    """
    parent = node.parent
    if parent is None:
        return "other"
    if parent.type == "class_constant_access_expression":
        children = parent.named_children
        return "member" if len(children) > 1 and children[-1].id == node.id else "class"
    if parent.type in ("qualified_name", "relative_name"):
        return "qualified" if _position(parent) == "constant" else "other"
    if node.type == "name" and key_const._php_is_binding(node):
        return "other"
    field = _field(parent, node)
    if field in _NEVER_A_CONSTANT_FIELDS or parent.type in _NEVER_A_CONSTANT:
        return "other"
    if (
        parent.type == "binary_expression"
        and field == "right"
        and any(not c.is_named and c.type == "instanceof" for c in parent.children)
    ):
        return "other"
    return "constant"


def _read_call(node: Node, accessors: frozenset[str]) -> bool:
    """Whether ``node`` (the constant's name) is the WHOLE first argument of a
    read the reader rules fold: positional, so ``(argument . X .)`` matches."""
    parent = node.parent
    value = parent if parent is not None and parent.type == "class_constant_access_expression" else node
    argument = value.parent
    if argument is None or argument.type != "argument" or len(argument.named_children) != 1:
        return False
    # Also False inside string interpolation, which no rule folds.
    return key_const._php_is_removable_read(node, accessors)


def _read_value(value: Node, accessors: frozenset[str]) -> bool:
    """Whether ``value`` is the WHOLE first argument of a read the reader
    rules fold, as :func:`_read_call` asks of a constant's name."""
    argument = value.parent
    if argument is None or argument.type != "argument" or len(argument.named_children) != 1:
        return False
    return key_const._php_is_removable_argument(value, accessors)


#: What an expression on a case can be read through. Only a plain property
#: access can be proven; a nullsafe one or a method call is another shape.
_ACCESSES = frozenset(
    {
        "member_access_expression",
        "nullsafe_member_access_expression",
        "member_call_expression",
        "nullsafe_member_call_expression",
    }
)


def _entry_read(
    node: Node, decl: xk.KeyDeclaration, accessors: frozenset[str], owner_text: str
) -> xk.KeyReference | str:
    """``Owner::NAME`` resolved to the enum: proven only as ``Owner::NAME->value``,
    the whole first argument of a read. Anything else naming the case is refused."""
    access = node.parent  # the class_constant_access_expression
    outer = access.parent
    target = outer.child_by_field_name("object") if outer is not None else None
    if outer is None or outer.type not in _ACCESSES or target is None or target.id != access.id:
        return xk.NOT_A_READ  # the case itself, handed on as a value
    name = outer.child_by_field_name("name")
    accessor = _text(name) if name is not None and name.type == "name" else ""
    if outer.type != "member_access_expression" or accessor not in decl.accessors:
        return xk.UNSUPPORTED_FORM
    if not _read_value(outer, accessors):
        return xk.NOT_A_READ
    return xk.KeyReference("", owner_text, decl.name, accessor, decl)


def _in_read_position(node: Node, decl: xk.KeyDeclaration, accessors: frozenset[str]) -> bool:
    """Whether a mention spelled like ``decl``'s read sits where a clone of
    its rule would match it."""
    if decl.kind != xk.ENUM_ENTRY:
        return _read_call(node, accessors)
    access = node.parent
    outer = access.parent if access is not None else None
    target = outer.child_by_field_name("object") if outer is not None else None
    return (
        outer is not None
        and outer.type == "member_access_expression"
        and target is not None
        and target.id == access.id
        and _read_value(outer, accessors)
    )


#: What stops ``self``/``static``/``parent`` meaning the enclosing class: an
#: anonymous class, and a closure, whose scope ``Closure::bind``, ``bindTo``
#: and ``call`` can rebind (a ``static`` one included).
_SCOPE_BREAKS = frozenset({"object_creation_expression", "anonymous_function", "arrow_function"})


def _enclosing_class(node: Node) -> Node | None:
    """The named class-like whose body ``node`` is in. None inside an
    anonymous class or a closure (see :data:`_SCOPE_BREAKS`), or outside any
    class."""
    current = node.parent
    while current is not None:
        if current.type in _CLASS_LIKES:
            return current
        if current.type in _SCOPE_BREAKS:
            return None
        current = current.parent
    return None


def _fqn(decl: xk.KeyDeclaration) -> str:
    return _join(decl.module, [decl.owner or ""])


class _Reader:
    """One reader file: its sections, imports and names, and the checkout's
    class index, built only when a verdict needs it."""

    def __init__(self, root: Node, path: str, repo_dir: str):
        self.root = root
        self.path = path
        self.repo_dir = repo_dir
        self.sections = _sections(root)
        self.imports = _imports(root)
        self.names: dict[str, list[Node]] = defaultdict(list)
        self.texts: list[Node] = []
        for node in xk.walk(root):
            if node.type == "name":
                self.names[_text(node)].append(node)
            elif node.type in _TEXT_NODES or (
                # A Doctrine or Symfony annotation resolves `Keys::NAME`
                # through the file's imports at run time.
                node.type == "comment" and _text(node).startswith("/**")
            ):
                self.texts.append(node)
        self._index: _Index | None = None

    @property
    def index(self) -> _Index:
        if self._index is None:
            self._index = php_index(self.repo_dir)
        return self._index

    def resolve(self, scope: Node) -> str | None:
        return _resolve_class(self.sections, self.imports, scope)


def _judge_relative(reader: _Reader, scope: Node, decl: xk.KeyDeclaration) -> str | None:
    """``self::NAME``, ``static::NAME`` or ``parent::NAME``: skipped only when
    it surely names another class's constant; never proven."""
    keyword = _text(scope).lower()
    cls = _enclosing_class(scope)
    if cls is None or cls.type == "trait_declaration":
        return xk.UNSUPPORTED_FORM  # a trait's `self` is whichever class uses it
    name = cls.child_by_field_name("name")
    section = _section_at(reader.sections, cls.start_byte)
    if name is None or section is None:
        return xk.UNSUPPORTED_FORM
    here = _join(section.namespace, [_text(name)])
    owner = _fqn(decl)
    if keyword == "parent":
        parents = [
            reader.resolve(n)
            for clause in cls.named_children
            if clause.type == "base_clause"
            for n in clause.named_children
            if n.type in _CLASS_NAMES
        ]
        if len(parents) != 1 or parents[0] is None:
            return xk.UNSUPPORTED_FORM
        return None if reader.index.unrelated(parents[0], owner) else xk.UNSUPPORTED_FORM
    if keyword == "static" and any(
        reader.index.reaches(candidate, here) and reader.index.reaches(candidate, owner)
        for candidate in (owner, *reader.index.by_fqn)
    ):
        # `static` is whichever class the call was made on: one that extends
        # this class and reaches the owner (implements an owner interface,
        # or is the owner) reads its constant.
        return xk.UNSUPPORTED_FORM
    if decl.name in _constant_names(cls) or reader.index.unrelated(here, owner):
        return None
    return xk.UNSUPPORTED_FORM


def _judge_member(
    reader: _Reader, node: Node, decl: xk.KeyDeclaration, accessors: frozenset[str]
) -> xk.KeyReference | str | None:
    scope = node.parent.named_children[0]
    if scope.type == "relative_scope":
        return _judge_relative(reader, scope, decl)
    written = _segments(scope) if scope.type in _CLASS_NAMES else None
    # The rule compares the scope's whole text, so it has to be spelled as
    # names and backslashes and nothing else.
    if written is None or _text(scope) != _spelled(*written):
        return xk.UNSUPPORTED_FORM  # `$x::NAME`, `(expr)::NAME`
    fqn = reader.resolve(scope)
    if fqn is None:
        return xk.IMPORT_UNRESOLVED
    owner = _fqn(decl)
    if fqn == owner:
        if decl.kind == xk.ENUM_ENTRY:
            return _entry_read(node, decl, accessors, _text(scope))
        if not _read_call(node, accessors):
            return xk.NOT_A_READ
        return xk.KeyReference("", _text(scope), decl.name)
    if reader.index.unrelated(fqn, owner):
        return None  # another repository class's own constant of the same name
    return xk.UNSUPPORTED_FORM if fqn in reader.index.by_fqn else xk.IMPORT_UNRESOLVED


def _const_target(imported: _Import) -> tuple[str, str]:
    """``(namespace, name)`` of a ``use const``: the namespace lowercased,
    the constant's own name as written."""
    *namespace, name = imported.target.split("\\")
    return _join("", namespace), name


def _judge_qualified_constant(reader: _Reader, qualified: Node, decl: xk.KeyDeclaration) -> str | None:
    """``\\Ns\\NAME``, ``Sub\\NAME`` or ``namespace\\NAME``: ours only when it
    resolves to the constant's namespace, and then refused, since no rule
    rewrites it. A qualified constant name has no global fallback."""
    written = _segments(qualified)
    section = _section_at(reader.sections, qualified.start_byte)
    if written is None or section is None:
        return xk.UNSUPPORTED_FORM
    kind, parts = written
    if kind == "fq":
        namespace = _join("", parts[:-1])
    elif kind == "rel":
        namespace = _join(section.namespace, parts[:-1])
    else:
        before, later = _bound(reader.imports, section, "class", parts[0], qualified.start_byte, fold=True)
        if later or len(before) > 1:
            return xk.IMPORT_UNRESOLVED
        if before:  # the first segment is an imported namespace
            namespace = _join("", [*before[0].target.split("\\"), *parts[1:-1]])
        else:
            namespace = _join(section.namespace, parts[:-1])
    return xk.UNSUPPORTED_FORM if namespace == decl.module else None


def _judge_constant(
    reader: _Reader, node: Node, decl: xk.KeyDeclaration, accessors: frozenset[str]
) -> xk.KeyReference | str | None:
    """A bare ``NAME`` against a namespace constant."""
    section = _section_at(reader.sections, node.start_byte)
    if section is None:
        return xk.UNSUPPORTED_FORM
    before, later = _bound(reader.imports, section, "const", decl.name, node.start_byte, fold=False)
    if later or len(before) > 1:
        return xk.IMPORT_UNRESOLVED
    if before:
        if _const_target(before[0]) != (decl.module, decl.name):
            return None  # another constant, imported under this name
    elif section.namespace != decl.module:
        # Unqualified, PHP tries `<section>\NAME` first and then the global
        # `NAME`: a global constant is reached only through that fallback,
        # which a `define()` anywhere can pre-empt.
        return xk.IMPORT_UNRESOLVED if decl.module == "" else None
    if not _read_call(node, accessors):
        return xk.NOT_A_READ
    return xk.KeyReference("", None, decl.name)


def _judge(
    reader: _Reader, node: Node, decl: xk.KeyDeclaration, accessors: frozenset[str]
) -> xk.KeyReference | str | None:
    """A proven read, a refusal reason, or None for a mention that is not ours."""
    position = _position(node)
    if decl.owner is not None:
        return _judge_member(reader, node, decl, accessors) if position == "member" else None
    if position == "qualified":
        return _judge_qualified_constant(reader, node.parent, decl)
    if position == "constant":
        return _judge_constant(reader, node, decl, accessors)
    return None


def _spelling(node: Node) -> tuple[str | None, str] | None:
    """What a reader rule would match a read here by: the scope's text and the
    name, or None and the name for a bare read."""
    position = _position(node)
    if position == "member":
        return _text(node.parent.named_children[0]), _text(node)
    if position == "constant":
        return None, _text(node)
    return None


def _vendored_mentions(
    reader: _Reader,
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
) -> list[xk.Refusal]:
    """A Composer ``vendor/`` file is never rewritten, and its classes are not
    in the index, so every class it names is unknown here. Only a mention that
    resolves to the owner itself, or text spelling the name and the owner's
    short name, refuses: PHP has no compile-time dependency graph, and a path
    repository copies application code into ``vendor/``."""
    refused: list[xk.Refusal] = []
    for decl in declarations:
        for node in reader.names.get(decl.name, ()):
            verdict = _judge(reader, node, decl, accessors)
            if isinstance(verdict, xk.KeyReference) or verdict == xk.NOT_A_READ:
                refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry))
        owner = re.compile(xk.mention_pattern([decl.owner or decl.name]).pattern, re.IGNORECASE)
        token = xk.mention_pattern([decl.name])
        for node in reader.texts:
            if token.search(_text(node)) and owner.search(_text(node)):
                refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry))
    return refused


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    root = syntax._parse(source, "php")
    reader = _Reader(root, path, repo_dir)
    if vendored(Path(path)):
        return [], _vendored_mentions(reader, path, source, declarations, accessors)
    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []
    for decl in declarations:
        # Skipped mentions in read position, by the text a clone would match.
        skipped: list[tuple[Node, tuple[str | None, str]]] = []
        proven: set[tuple[str | None, str]] = set()
        for node in reader.names.get(decl.name, ()):
            verdict = _judge(reader, node, decl, accessors)
            spelling = _spelling(node)
            if verdict is None:
                if spelling is not None and _in_read_position(node, decl, accessors):
                    skipped.append((node, spelling))
                continue
            if isinstance(verdict, xk.KeyReference):
                reads.append(replace(verdict, path=path))
                proven.add(spelling)
            else:
                refused.append(xk.refusal(path, node, verdict, source, decl.entry))
        # The reader rules match by TEXT across the whole file, not per
        # namespace section: a skipped read spelled like a proven one would be
        # folded with it.
        for node, spelling in skipped:
            if spelling in proven:
                refused.append(xk.refusal(path, node, xk.UNSUPPORTED_FORM, source, decl.entry))
        # `constant('Keys::NAME')`, `defined('NAME')`, a heredoc, a Blade echo:
        # the name reached through text, which no rule rewrites.
        token = xk.mention_pattern([decl.name])
        lines = source.splitlines()
        for node in reader.texts:
            text = _text(node)
            hit = token.search(text)
            if hit is not None:
                # At the mention's own line: a whole Blade view is one node.
                line = node.start_point[0] + text.count("\n", 0, hit.start())
                quoted = lines[line].strip() if line < len(lines) else ""
                refused.append(xk.Refusal(path, line + 1, xk.UNSUPPORTED_FORM, quoted, decl.entry))
        # Reads of `use const M\NAME as A` spell `A`, which no rule for NAME
        # matches; the import itself is the mention.
        if decl.owner is None:
            for imported in reader.imports:
                if (
                    imported.kind == "const"
                    and imported.alias != decl.name
                    and _const_target(imported) == (decl.module, decl.name)
                ):
                    refused.append(xk.refusal(path, imported.node, xk.UNSUPPORTED_FORM, source))
    if refused:
        return [], refused
    return reads, refused


#: Static calls on a backed enum that reach its cases by value or as a list.
_DYNAMIC_CALLS = frozenset({"cases", "from", "tryfrom"})


def _owner_name(scope: Node | None) -> str | None:
    """The lowercased short name a class scope spells, or the enclosing
    enum's for ``self``/``static``."""
    if scope is None:
        return None
    if scope.type == "relative_scope":
        cls = _enclosing_class(scope)
        name = cls.child_by_field_name("name") if cls is not None else None
        if cls is None or cls.type != "enum_declaration" or name is None:
            return None
        return _text(name).lower()
    written = _segments(scope) if scope.type in _CLASS_NAMES else None
    return written[1][-1].lower() if written is not None else None


def enum_dynamic_uses(
    path: str, source: str, entries: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[xk.Refusal]:
    """``Flags::cases()``, ``Flags::from(...)``, ``Flags::tryFrom(...)``,
    ``Flags::{$expr}`` and ``Flags::class`` in ``path``, qualified or not, and
    ``self::cases()`` (or ``from``/``tryFrom``) inside the enum itself.

    Matched on the owner's short name, case-insensitively, without proving
    which class it means: a note only asks a reviewer to look, so
    over-reporting costs a line and under-reporting costs a missed read.
    """
    root = syntax._parse(source, "php")
    by_owner: dict[str, list[xk.KeyDeclaration]] = defaultdict(list)
    for decl in entries:
        if decl.owner is not None:
            by_owner[decl.owner].append(decl)
    notes: list[xk.Refusal] = []
    for node in xk.walk(root):
        owner: str | None = None
        if node.type == "scoped_call_expression":
            name = node.child_by_field_name("name")
            if name is not None and _text(name).lower() in _DYNAMIC_CALLS:
                owner = _owner_name(node.child_by_field_name("scope"))
        elif node.type == "class_constant_access_expression" and len(node.named_children) == 2:
            scope, member = node.named_children
            if member.named_children or _text(member).lower() == "class":
                owner = _owner_name(scope)
        elif node.type in ("string", "encapsed_string"):
            # The enum's name as a string (`$cls = 'App\Flags'; $cls::from(...)`,
            # `class_alias(...)`, `new ReflectionEnum(...)`) hands it on like
            # `Flags::class` does.
            named = (_literal(node) or "").lower()
            for decl in entries:
                if decl.owner is not None and named == _fqn(decl):
                    notes.append(xk.refusal(path, node, xk.ENUM_DYNAMIC, source, decl.entry))
        for decl in by_owner.get(owner or "", ()):
            notes.append(xk.refusal(path, node, xk.ENUM_DYNAMIC, source, decl.entry))
    return sorted(set(notes), key=lambda n: (n.line, n.entry))


xk.register(
    "php",
    xk.Profile(
        language_family=frozenset({"php"}),
        declarations=declarations,
        references=references,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        reaches=frozenset({"php_guest"}),
        entry_findings=entry_findings,
        enum_dynamic_uses=enum_dynamic_uses,
    ),
)
