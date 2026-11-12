"""Leave a literal the fold wrote out of a React hook's dependency array.

``useEffect(() => { … }, [isEnabled])`` becomes ``[true]`` when the flag read
``isEnabled`` is folded: legal, and pointless, since a literal never changes.
The element is dropped (``[]`` when it was the only one) when all of this holds:

* the array is the dependency argument of a known React hook, imported from
  ``react``, ``preact/hooks`` or ``preact/compat`` (named, possibly aliased, or
  as a member of the namespace or default import: ``React.useEffect``) and
  bound nowhere else in the file, and the call has no argument after it;
* the hook calls of the original and of the rewritten text pair up one for one
  (same names, same order), the arrays hold the same number of elements, and
  the element is a boolean literal where the original's was not: only a
  literal the run wrote is touched, never one the customer wrote;
* the array is on one line and holds no comment.

Anything else is left as the fold wrote it. Placement: Action. Which calls are
React hooks is policy, and nothing here needs the engine's rewrite machinery.
"""

from __future__ import annotations

from tree_sitter import Node

from flag_cleanup.ts_syntax import _parse, _walk

#: Hook name to the index of its dependency argument.
_HOOKS = {
    "useEffect": 1,
    "useLayoutEffect": 1,
    "useInsertionEffect": 1,
    "useMemo": 1,
    "useCallback": 1,
    "useImperativeHandle": 2,
}


#: The modules whose hooks these are.
_MODULES = frozenset({"react", "preact/hooks", "preact/compat"})

#: Parents under which an identifier is a binding, not a use.
_BINDING_PARENTS = frozenset(
    {
        "variable_declarator", "function_declaration", "generator_function_declaration", "function_expression",
        "function", "class_declaration", "class", "required_parameter", "optional_parameter", "formal_parameters",
        "arrow_function", "catch_clause", "pair_pattern", "object_pattern", "array_pattern", "rest_pattern",
        "assignment_pattern", "shorthand_property_identifier_pattern", "for_in_statement", "method_definition",
        "function_signature", "namespace_import", "import_clause", "import_specifier", "import_require_clause",
        "type_alias_declaration", "interface_declaration", "enum_declaration",
    }
)


def _bindings(root: Node, name: str) -> int:
    return sum(
        1
        for node in _walk(root)
        if node.type in ("identifier", "shorthand_property_identifier_pattern", "type_identifier")
        and node.text.decode("utf-8") == name
        and node.parent is not None
        and node.parent.type in _BINDING_PARENTS
        and not (node.parent.type == "variable_declarator" and node.parent.child_by_field_name("value") is node)
        and not (node.parent.type == "arrow_function" and node.parent.child_by_field_name("body") is node)
        and not (node.parent.type == "assignment_pattern" and node.parent.child_by_field_name("right") is node)
    )


def _react_imports(root: Node) -> tuple[dict[str, str], set[str]]:
    """``(local name -> hook name, local namespace names)`` bound by an import
    from React or Preact and bound nowhere else in the file."""
    named: dict[str, str] = {}
    spaces: set[str] = set()
    for node in root.children:
        if node.type != "import_statement":
            continue
        source = node.child_by_field_name("source")
        if source is None or source.text.decode("utf-8").strip("'\"`") not in _MODULES:
            continue
        for child in _walk(node):
            if child.type == "import_specifier":
                imported = child.child_by_field_name("name")
                local = child.child_by_field_name("alias") or imported
                if imported is not None and local is not None:
                    named[local.text.decode("utf-8")] = imported.text.decode("utf-8")
            elif child.type == "namespace_import":
                ident = next((c for c in child.children if c.type == "identifier"), None)
                if ident is not None:
                    spaces.add(ident.text.decode("utf-8"))
            elif child.type == "import_clause":
                ident = next((c for c in child.children if c.type == "identifier"), None)
                if ident is not None:
                    spaces.add(ident.text.decode("utf-8"))
    named = {k: v for k, v in named.items() if _bindings(root, k) == 1}
    spaces = {k for k in spaces if _bindings(root, k) == 1}
    return named, spaces


def _dependency_arrays(root: Node) -> list[tuple[str, Node]]:
    named, spaces = _react_imports(root)
    if not named and not spaces:
        return []
    found: list[tuple[str, Node]] = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None:
            continue
        if callee.type == "identifier":
            name = named.get(callee.text.decode("utf-8"))
        elif callee.type == "member_expression":
            obj, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
            name = (
                prop.text.decode("utf-8")
                if obj is not None and obj.type == "identifier" and obj.text.decode("utf-8") in spaces
                and prop is not None and prop.type == "property_identifier"
                else None
            )
        else:
            continue
        index = _HOOKS.get(name or "")
        args = node.child_by_field_name("arguments")
        if index is None or args is None or len(args.named_children) != index + 1:
            continue
        deps = args.named_children[index]
        if deps.type == "array":
            found.append((name, deps))
    found.sort(key=lambda pair: pair[1].start_byte)
    return found


def _literal(node: Node) -> bool:
    return node.type in ("true", "false")


def drop_literal_dependencies(original: str, rewritten: str, grammar: str) -> str:
    """``rewritten`` with each literal the run wrote out of a hook's dependency
    array taken out; ``rewritten`` itself when there is none."""
    if original == rewritten or not any(name in rewritten for name in _HOOKS):
        return rewritten
    before = _dependency_arrays(_parse(original, grammar))
    root = _parse(rewritten, grammar)
    if root.has_error:
        return rewritten
    after = _dependency_arrays(root)
    if [name for name, _ in before] != [name for name, _ in after]:
        return rewritten
    data = rewritten.encode("utf-8")
    cuts: list[tuple[int, int, str]] = []
    for (_, old), (_, new) in zip(before, after, strict=True):
        olds, news = old.named_children, new.named_children
        if len(olds) != len(news) or b"\n" in data[new.start_byte : new.end_byte]:
            continue
        if any(c.type == "comment" for c in news):
            continue
        drop = [i for i, (o, n) in enumerate(zip(olds, news, strict=True)) if _literal(n) and not _literal(o)]
        if not drop:
            continue
        keep = [n for i, n in enumerate(news) if i not in drop]
        # The array re-spelled from the elements that stay, in their own text.
        text = "[" + ", ".join(data[n.start_byte : n.end_byte].decode("utf-8") for n in keep) + "]"
        cuts.append((new.start_byte, new.end_byte, text))
    if not cuts:
        return rewritten
    out = bytearray(data)
    for start, end, text in sorted(cuts, reverse=True):
        out[start:end] = text.encode("utf-8")
    return out.decode("utf-8")

