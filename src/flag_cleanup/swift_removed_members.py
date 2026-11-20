"""A stored Swift property the run deleted must be named nowhere afterwards.

The engine folds ``showPlans = isEnabled("k")`` in an ``init`` and may then
delete the property. It rewrites a bare ``showPlans`` and ``self.showPlans``
and nothing else, so a read through another value (``layout.showPlans``, a key
path, a projected ``$showPlans``, a capture ``[showPlans]``) or a binding of
the same name in a nested scope is left naming a member that no longer exists,
or is rewritten into a literal where a name stood (``true = true``).

This check does not ask the engine's guard anything. It reads the bytes the run
produced, takes the stored properties a changed file declared and no longer
declares, and refuses the flag when the name is still spelled as an identifier
in that file, or as a member (``x.name``, ``\\.name``) or ``$name`` in any Swift
file of the checkout, or, for a property that was not ``private``, as a bare
identifier in any Swift file of the checkout. Over-refusing is the safe direction: a same-named member
of an unrelated type refuses too, because a deleted property is a build error
and an unneeded refusal is a note to the reviewer.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import source_index, wrapper_swift


def _walk(node: Node) -> Iterable[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _name(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _spelled(source: str, name: str, scope: Callable[[Node], bool] | None = None) -> tuple[bool, bool]:
    """``(as an identifier, as a member or projection)`` in ``source``.

    ``scope`` narrows the search to the nodes it accepts (a type body)."""
    if name not in source:
        return False, False
    identifier = member = False
    for node in _walk(wrapper_swift.parse(source)):
        # `_name` is a property wrapper's backing storage, named by the
        # declaration of `name` and gone with it.
        if node.type != "simple_identifier" or _name(node) not in (name, f"_{name}"):
            continue
        if scope is not None and not scope(node):
            continue
        identifier = True
        parent = node.parent
        if parent is not None and parent.type == "navigation_suffix":
            member = True
    if scope is None and re.search(rf"\${re.escape(name)}\b", source):
        member = True
    elif scope is not None:
        for node in _walk(wrapper_swift.parse(source)):
            if node.type == "simple_identifier" and _name(node) == f"${name}" and scope(node):
                member = True
    return identifier, member


def _type_name(declaration: Node) -> str:
    return _name(declaration.child_by_field_name("name"))


def _is_extension(declaration: Node) -> bool:
    return any(not c.is_named and c.type == "extension" for c in declaration.children)


def stored(source: str) -> dict[tuple[str, str], list[tuple[bool, bool]]]:
    """``{(type name, property name): [(private, exact), ...]}``, one entry per
    stored property declared directly in a type body. Two types can share a
    name (``A.Model`` and ``B.Model``), so a key can hold several entries and a
    deletion is a drop in their COUNT, never a key that disappears.

    ``exact`` is false when the enclosing type is not a plain top-level
    declaration this file declares once (a nested type, an extension, a name
    two declarations share), so the caller cannot bound where the property is
    visible."""
    root = wrapper_swift.parse(source)
    declarations = [n for n in _walk(root) if n.type == "class_declaration"]
    names = Counter(_type_name(d) for d in declarations if not _is_extension(d))
    found: dict[tuple[str, str], list[tuple[bool, bool]]] = {}
    for node in _walk(root):
        if node.type != "property_declaration" or node.parent is None:
            continue
        # An enum holds no stored instance property, so the engine's init
        # fold never deletes one there; its static `let`s go only through the
        # key-constant mechanism, which carries its own proof.
        if node.parent.type != "class_body":
            continue
        if any(child.type == "computed_property" for child in node.children):
            continue
        owner = node.parent.parent
        if owner is None or owner.type != "class_declaration":
            continue
        # Visibility only: an attribute or wrapper does not widen it, and
        # `private(set)` is a different visibility_modifier text.
        private = any(
            c.type == "visibility_modifier" and c.text == b"private"
            for m in node.children
            if m.type == "modifiers"
            for c in m.children
        )
        nested = any(a.type in ("class_declaration", "function_declaration", "init_declaration") for a in _ancestors(owner))
        exact = not nested and not _is_extension(owner) and names[_type_name(owner)] == 1
        for pattern in node.children_by_field_name("name"):
            for leaf in _walk(pattern):
                if leaf.type == "simple_identifier":
                    found.setdefault((_type_name(owner), _name(leaf)), []).append((private, exact))
    return found


def _ancestors(node: Node) -> Iterable[Node]:
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def _within(type_name: str) -> Callable[[Node], bool] | None:
    """A predicate for nodes inside ``type_name``'s body or one of its
    extensions in the same file, or ``None`` when an extension's name cannot be
    matched exactly (a qualified or generic spelling that mentions it)."""

    def inside(node: Node) -> bool:
        for a in _ancestors(node):
            if a.type == "class_declaration" and _type_name(a) == type_name:
                return True
        return False

    return inside


def _unbounded_extensions(source: str) -> bool:
    """Some extension in ``source`` names a target this file does not declare
    as a type, spelled exactly: a typealias (``extension G2`` with ``typealias
    G2 = Gate`` here or in any file), a qualified or generic spelling, or a
    type declared elsewhere. Any of these may extend the owner of a private
    property, so the search cannot be bounded to the owner's own name."""
    root = wrapper_swift.parse(source)
    declarations = [n for n in _walk(root) if n.type == "class_declaration"]
    types = {_type_name(d) for d in declarations if not _is_extension(d)}
    return any(_type_name(d) not in types for d in declarations if _is_extension(d))


def _literal_bindings(before: str, after: str) -> bool:
    """The run wrote a boolean literal where a name is assigned or captured."""

    def count(source: str) -> int:
        total = 0
        for node in _walk(wrapper_swift.parse(source)):
            if node.type == "directly_assignable_expression" and any(
                child.type == "boolean_literal" for child in node.children
            ):
                total += 1
            elif node.type == "capture_list_item" and any(
                child.type == "boolean_literal" for child in node.children
            ):
                total += 1
        return total

    return count(after) > count(before)


def refusals(
    changed: Iterable,
    repo_dir: str,
    deleted: Callable[[object], bool],
) -> dict[str, str]:
    """``{path: reason}`` for each changed Swift file that deleted a stored
    property something still names, or wrote a literal where a name was bound.

    A property is judged by where it is VISIBLE. A ``private`` one is visible
    only inside its own type declaration and in same-file extensions of that
    type, so only those bodies are searched, and only when the type is a plain
    top-level one this file declares once; anything else, and any property that
    is not ``private``, is searched for in every Swift file of the checkout.

    ``changed`` are the run's summaries (``path``, ``original_content``,
    ``content``); ``deleted(s)`` says a summary's file was deleted whole."""
    swift = [s for s in changed if str(s.path).endswith(".swift") and not deleted(s)]
    if not swift:
        return {}
    reasons: dict[str, str] = {}
    removed: dict[str, list[tuple[str, str, bool, bool]]] = {}
    for s in swift:
        if _literal_bindings(s.original_content, s.content):
            reasons[s.path] = "the rewrite wrote a boolean literal where a name is assigned or captured"
            continue
        before, after = stored(s.original_content), stored(s.content)
        gone = [
            (t, n, all(p for p, _ in entries), all(e for _, e in entries))
            for (t, n), entries in sorted(before.items())
            if len(entries) > len(after.get((t, n), []))
        ]
        if gone:
            removed[s.path] = gone
    if not removed:
        return reasons
    # Every Swift file as it will stand: the run's text for a changed one.
    texts = {s.path: s.content for s in changed if str(s.path).endswith(".swift") and not deleted(s)}
    resolved = {str(Path(p).resolve()) for p in texts}
    for entry in source_index.files(repo_dir, (".swift",)):
        path = entry.resolved
        if path in resolved:
            continue
        try:
            texts[path] = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    for path, gone in removed.items():
        for type_name, name, private, exact in gone:
            local = private and exact and not _unbounded_extensions(texts[path])
            for where, text in texts.items():
                if local:
                    if where != path:
                        continue
                    identifier, member = _spelled(text, name, _within(type_name))
                    hit = identifier or member
                else:
                    identifier, member = _spelled(text, name)
                    # A name that is not `private` is readable bare from an
                    # extension or a subclass in any file of the module.
                    hit = (where == path or not private) and identifier or member
                if hit:
                    reasons.setdefault(
                        path,
                        f"the stored property `{name}` was deleted, but `{name}` is still named "
                        f"in {Path(where).name}: a read through another value, a key path, a "
                        "projected value or a capture the rewrite did not reach",
                    )
                    break
            if path in reasons:
                break
    return reasons
