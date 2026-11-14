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

:func:`remove_emptied_effects` deletes an effect the fold left with nothing to
run: ``useEffect(() => {}, [])`` as a statement of its own. Taking an
unconditional hook call out moves every later hook up one place, the same in
every render, which React allows. See its docstring for what qualifies.
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



#: The ``bindings`` kind of an effect :func:`remove_emptied_effects` deleted.
EMPTIED_EFFECT = "emptied effect"

#: The effect hooks: their first argument runs after a render, and a callback
#: with an empty body does nothing there.
_EFFECTS = frozenset({"useEffect", "useLayoutEffect", "useInsertionEffect"})


def _effect_calls(root: Node) -> list[tuple[str, Node]]:
    """Each call of an imported React effect hook, ``(hook name, call)``, in
    source order."""
    named, spaces = _react_imports(root)
    if not named and not spaces:
        return []
    found: list[tuple[str, Node]] = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        name = None
        if callee is not None and callee.type == "identifier":
            name = named.get(callee.text.decode("utf-8"))
        elif callee is not None and callee.type == "member_expression":
            obj, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
            if (
                obj is not None and obj.type == "identifier" and obj.text.decode("utf-8") in spaces
                and prop is not None and prop.type == "property_identifier"
            ):
                name = prop.text.decode("utf-8")
        if name in _EFFECTS:
            found.append((name, node))
    found.sort(key=lambda pair: pair[1].start_byte)
    return found


def _callback_body(call: Node) -> Node | None:
    """The block body of the effect's callback, a plain (not generator) arrow
    or function written in place; ``None`` for any other first argument."""
    args = call.child_by_field_name("arguments")
    if args is None or not args.named_children:
        return None
    callback = args.named_children[0]
    if callback.type not in ("arrow_function", "function_expression", "function"):
        return None
    body = callback.child_by_field_name("body")
    return body if body is not None and body.type == "statement_block" else None


def _inert(node: Node) -> bool:
    """Whether evaluating ``node`` runs no code: a name, a literal, ``this``,
    or a property read off one of those."""
    if node.type in ("identifier", "this", "true", "false", "null", "undefined", "number", "string"):
        return True
    if node.type == "member_expression":
        obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
        return obj is not None and prop is not None and prop.type == "property_identifier" and _inert(obj)
    return False


def _deletable(call: Node) -> bool:
    """Whether the effect ``call`` is a statement of its own whose callback is
    empty and whose other argument, a dependency array, runs no code."""
    statement = call.parent
    if statement is None or statement.type != "expression_statement":
        return False
    if len(statement.named_children) != 1 or statement.named_children[0].id != call.id:
        return False
    if statement.parent is None or statement.parent.type != "statement_block":
        return False
    body = _callback_body(call)
    if body is None or body.named_children:
        return False
    args = call.child_by_field_name("arguments")
    rest = args.named_children[1:] if args is not None else []
    if len(rest) > 1 or any(n.type == "comment" for n in _walk(statement)):
        return False
    if rest and (rest[0].type != "array" or not all(_inert(n) for n in rest[0].named_children)):
        return False
    return not rest or all(_read_elsewhere(statement, name) for name in _names(rest[0]))


#: The nodes whose body is a scope a name in a dependency array can come from.
_FUNCTIONS = frozenset(
    {"function_declaration", "function_expression", "function", "arrow_function", "method_definition",
     "generator_function_declaration"}
)


def _names(node: Node) -> set[str]:
    """The names a dependency array reads: each element's leftmost name."""
    found = set()
    for element in node.named_children:
        while element.type == "member_expression":
            element = element.child_by_field_name("object")
        if element is not None and element.type == "identifier":
            found.add(element.text.decode("utf-8"))
    return found


def _read_elsewhere(statement: Node, name: str) -> bool:
    """Whether ``name`` is spelled in the function around ``statement``
    outside it and outside the function's parameters. A parameter or a prop
    read only by the deleted effect would be left unused, which lint rejects,
    so its effect stays."""
    function = statement.parent
    while function is not None and function.type not in _FUNCTIONS:
        function = function.parent
    body = function.child_by_field_name("body") if function is not None else None
    if body is None:
        return False
    return any(
        node.type in ("identifier", "shorthand_property_identifier")
        and node.text.decode("utf-8") == name
        and not (statement.start_byte <= node.start_byte < statement.end_byte)
        for node in _walk(body)
    )


def remove_emptied_effects(original: str, rewritten: str, grammar: str) -> tuple[str, list[str]]:
    """``rewritten`` without each effect the run emptied, and the deleted
    statements, each on one line.

    An effect goes when all of this holds: its hook is ``useEffect``,
    ``useLayoutEffect`` or ``useInsertionEffect``, resolved as
    :func:`drop_literal_dependencies` resolves one; the call is a statement of
    its own, directly in a block, on lines of its own, with no comment; its
    first argument is an arrow or function written in place whose block body is
    empty now and was not in the original (the effect calls of the two texts
    pair up one for one, same hooks in the same order, or nothing is deleted);
    and a second argument, if any, is an array of names, literals and property
    reads, whose evaluation the deletion cannot be missing. An effect the
    customer wrote empty is never touched."""
    if original == rewritten or not any(name in rewritten for name in _EFFECTS):
        return rewritten, []
    root = _parse(rewritten, grammar)
    if root.has_error:
        return rewritten, []
    before = _effect_calls(_parse(original, grammar))
    after = _effect_calls(root)
    if [name for name, _ in before] != [name for name, _ in after]:
        return rewritten, []
    data = rewritten.encode("utf-8")
    cuts: list[tuple[int, int]] = []
    removed: list[str] = []
    for (_, old), (_, new) in zip(before, after, strict=True):
        old_body = _callback_body(old)
        if old_body is None or not old_body.named_children or not _deletable(new):
            continue
        statement = new.parent
        block = statement.parent
        if len(block.named_children) == 1:
            # The block's only statement: the block is written `{}`, as a
            # formatter writes an empty one.
            cuts.append((block.start_byte + 1, block.end_byte - 1))
            removed.append(" ".join(data[statement.start_byte : statement.end_byte].decode("utf-8").split()))
            continue
        start = data.rfind(b"\n", 0, statement.start_byte) + 1
        end = data.find(b"\n", statement.end_byte)
        end = len(data) if end < 0 else end + 1
        if data[start : statement.start_byte].strip() or data[statement.end_byte : end].strip():
            continue
        cuts.append((start, end))
        removed.append(" ".join(data[statement.start_byte : statement.end_byte].decode("utf-8").split()))
    if not cuts:
        return rewritten, []
    out = bytearray(data)
    for start, end in sorted(cuts, reverse=True):
        del out[start:end]
    return out.decode("utf-8"), removed


def unused_effect_imports(text: str, grammar: str) -> frozenset[str]:
    """The local names of the effect hooks imported from React or Preact that
    nothing in ``text`` outside its import statements spells.

    The stranded-import pass withholds a named specifier in a file holding JSX
    under the classic runtime (it could be the element factory), so deleting an
    effect there can leave its hook's import unused, which lint rejects. The
    caller compares this before and after, and keeps the effect when it grew."""
    root = _parse(text, grammar)
    named, _ = _react_imports(root)
    locals_ = {local for local, hook in named.items() if hook in _EFFECTS}
    if not locals_:
        return frozenset()
    used = {
        node.text.decode("utf-8")
        for node in _walk(root)
        if node.type == "identifier"
        and node.text.decode("utf-8") in locals_
        and not _in_import(node)
    }
    return frozenset(locals_ - used)


def _in_import(node: Node) -> bool:
    while node is not None:
        if node.type == "import_statement":
            return True
        node = node.parent
    return False
