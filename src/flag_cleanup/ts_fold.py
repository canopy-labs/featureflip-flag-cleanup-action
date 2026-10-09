"""Resolve a TypeScript local the fold bound to a literal it never saw coming.

``rules/ts_const.toml`` propagates a const bound to a flag READ. It cannot see a
const bound to an expression AROUND the read, which only becomes a literal once
the cascade has folded it::

    const on = useFlag('k');
    const show = on || user.enrolled;    // -> const show = true;
    return show && <Panel />;            // -> return show && <Panel />;

The read is inlined and the ``||`` folded, and the cascade stops at the
declaration: nothing reaches the ``show`` in the ``return``. The pull request
then carries ``const show = true;`` and a guard that reads as live code.

:func:`inline_literal_bindings` finishes it. It runs after the engine, with the
whole file in hand, deletes such a declaration and writes ``<marker>(true)`` at
each reference. The caller hands the result back to the engine, where
``rules/ts.toml``'s marker rule creates the literal inside the cascade — so a
derived binding folds exactly as a read written inline would, ``jsx_cleanup``
included. See :func:`flag_cleanup.piranha_runner._inline_bound_reads`, which
drives the same loop for Python.

Which bindings qualify, and why each condition is there:

* **The transform made it a literal.** Per NAME, the declaration must be a
  single-declarator ``const`` whose value is ``true``/``false`` in the output
  and was not in the input. ``const DEBUG = false`` at the top of a file is the
  customer's, and stays.
* **It is bound exactly once in the whole file**, counted with the same
  :func:`~flag_cleanup.ts_syntax.binding_nodes` census Gate 2 uses. A second
  binder anywhere — a parameter, a ``catch`` variable, a destructured
  property, an assignment — means a reference might resolve to the other one.
* **It lives in a block, not at the top of the file.** A top-level ``const``
  in a file that is a classic script rather than a module is a global another
  file can read, and that file does not contain the flag key, so it is not
  even in the candidate set.
* **Every reference is an ordinary expression inside that block, after the
  declaration.** A shorthand property (``{ show }``), a type position
  (``typeof show``), an export specifier or a JSX tag name is refused rather
  than rewritten, and so is a mention of the name outside the block, which
  can only be some other binding.
* **The declaration has its line(s) to itself**, so deleting it is deleting
  whole lines.

A refusal leaves the declaration standing as ``const show = true;`` — exactly
what the run produced before this pass existed. Nothing here can make a
rewrite that ran before stop running.
"""

from __future__ import annotations

from collections import Counter

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk, binding_nodes

#: Literal node types a binding may carry. Both grammars spell them this way.
_LITERALS = frozenset({"true", "false"})

#: A reference with one of these parents is not an expression the marker can
#: stand in for: the rewrite would emit `export { __ff_…(true) }` or
#: `<__ff_…(true) />`, or turn an update into an assignment to a call.
_NON_EXPRESSION_PARENTS = frozenset(
    {
        "export_specifier",
        "import_specifier",
        "import_clause",
        "namespace_import",
        "namespace_export",
        "jsx_opening_element",
        "jsx_closing_element",
        "jsx_self_closing_element",
        "nested_identifier",
        "update_expression",
        "decorator",
    }
)

#: An ancestor of one of these types puts the reference in TYPE space, where a
#: value cannot appear at all. Matches the type-context filter `ts_const.toml`
#: carries for the same reason.
_TYPE_CONTEXTS = frozenset(
    {
        "type_annotation",
        "type_alias_declaration",
        "type_query",
        "interface_declaration",
        "type_arguments",
        "type_parameters",
    }
)

#: Node types that spell a name without being a plain `identifier`. Any of
#: them spelling the candidate's name is a use this pass does not model.
_OTHER_NAME_NODES = frozenset(
    {
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "type_identifier",
    }
)


def inline_literal_bindings(before: str, after: str, language: str, marker: str) -> str:
    """Inline every local the transform newly bound to a literal; drop each.

    ``before`` is the customer's file and ``after`` the engine's output.
    Returns ``after`` unchanged when nothing qualifies (see the module
    docstring), and also when ``after`` already contains ``marker``: once the
    name is in the file, "did a marker survive the engine?" cannot tell ours
    from theirs, and that question is the only thing between a call to an
    identifier that does not exist and a pull request.
    """
    if after == before or marker in after:
        return after
    root = _parse(after, language)
    data = after.encode("utf-8")
    preexisting = Counter(
        _text(name, before_data)
        for before_data in [before.encode("utf-8")]
        for _decl, name, _value in _literal_consts(_parse(before, language), before_data)
    )
    candidates = _literal_consts(root, data)
    produced = Counter(_text(name, data) for _decl, name, _value in candidates)
    bindings = Counter(_text(node, data) for node in binding_nodes(root))

    edits: list[tuple[int, int, bytes]] = []
    for declaration, name_node, value in candidates:
        name = _text(name_node, data)
        if produced[name] <= preexisting[name] or bindings[name] != 1:
            continue
        span = _own_lines(declaration, data)
        if span is None:
            continue
        references = _references(root, data, name, name_node, declaration)
        if references is None:
            continue
        replacement = f"{marker}({_text(value, data)})".encode("utf-8")
        edits.append((span[0], span[1], b""))
        edits.extend((ref.start_byte, ref.end_byte, replacement) for ref in references)

    if not edits:
        return after
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8")


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def _literal_consts(root: Node, data: bytes) -> list[tuple[Node, Node, Node]]:
    """``(declaration, name, value)`` for each block-scoped ``const x = <literal>``.

    Single-declarator only, for the reason `ts_const.toml` gives: deleting
    ``const show = true, retries = 3;`` would take ``retries`` with it.
    """
    found = []
    for node in _walk(root):
        if node.type != "lexical_declaration" or node.parent is None:
            continue
        if node.parent.type != "statement_block":
            continue
        if not any(child.type == "const" for child in node.children):
            continue
        declarators = [c for c in node.named_children if c.type == "variable_declarator"]
        if len(declarators) != 1:
            continue
        name = declarators[0].child_by_field_name("name")
        value = declarators[0].child_by_field_name("value")
        if name is None or name.type != "identifier" or value is None:
            continue
        if value.type in _LITERALS:
            found.append((node, name, value))
    return found


def _own_lines(declaration: Node, data: bytes) -> tuple[int, int] | None:
    """The byte span of the declaration's whole lines, or ``None`` if it shares one."""
    start = data.rfind(b"\n", 0, declaration.start_byte) + 1
    newline = data.find(b"\n", declaration.end_byte)
    end = len(data) if newline == -1 else newline + 1
    if data[start : declaration.start_byte].strip():
        return None
    if data[declaration.end_byte : end].strip():
        return None
    return start, end


def _references(
    root: Node, data: bytes, name: str, name_node: Node, declaration: Node
) -> list[Node] | None:
    """Every reference to the binding, or ``None`` if one cannot be rewritten."""
    scope = declaration.parent
    references = []
    for node in _walk(root):
        if node.type in _OTHER_NAME_NODES:
            if _text(node, data) == name:
                return None
            continue
        if node.type != "identifier" or node == name_node or _text(node, data) != name:
            continue
        if not (declaration.end_byte <= node.start_byte and node.end_byte <= scope.end_byte):
            return None
        if node.parent is None or node.parent.type in _NON_EXPRESSION_PARENTS:
            return None
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.type in _TYPE_CONTEXTS:
                return None
            ancestor = ancestor.parent
        references.append(node)
    return references
