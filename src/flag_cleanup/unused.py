"""Delete the bindings a flag fold stranded — imports and local variables.

Folding a flag out of an ``if`` splices the surviving branch up a level.
Anything whose only remaining use was inside the branch that went away is now
unused. In Go both an unused import and an unused local are **compile
errors**, so the pull request this tool opens does not build. The other three
languages are handled for consistency, not because leaving them standing is
as severe: `javac` has no built-in lint for either and never warns; an
unused local is a real compiler warning in C# (CS0219) but an unused
``using`` is an IDE/analyzer concern (IDE0005), not the base compiler; and
TypeScript's `noUnusedLocals`/`noUnusedParameters` are off by default, so a
default project sees nothing at all. Removing them there is tidiness, not a
build fix.

The safety argument is one sentence: **a binding is only touched if it was
referenced BEFORE the fold and is referenced nowhere AFTER it.** That
differential framing is what makes this sound without a symbol resolver.

That sentence needs one correction to be true for a VARIABLE. `references()`
already excludes a name from inside its own import declaration (structurally,
via ``_inside_import``), so counting references in an import's source is
already "outside the declaration". A variable's declaration carries no such
exclusion — `x := 3` reads as one reference to `x`, its own name — so a naive
before/after count can never reject a variable the customer had already left
unused: its declaration alone always keeps the "before" count above zero.
The correction is to blank, before counting, the NAME TOKEN that each
declaration of the name binds — the token, never the whole declaration, since
everything else a declaration contains is a read like any other, its
initializer included. Blanking the whole span instead hides a nested re-bind's
genuine read of the binding it shadows (`x := x + 1`), which deletes a live
binding; only the bound position is not a read.

Both counters do that blanking, and which one runs depends on the KIND, so the
two names are worth keeping straight: an import is counted file-wide by
``_references_excluding_own_declaration``, and a variable is counted by
``_references_in_scope`` instead — never by the former, despite that being
where this correction is easiest to describe.

The scope restriction is why a variable needs its own counter: a NAME is not a
BINDING. Two functions in the same file can each declare their own local `x`,
and a file-wide blanking of every `x` binder cannot tell them apart — one
function's use of its own `x` would then look like a reference to the OTHER
function's same-named, unrelated `x`. So a variable's differential is scoped
one level tighter than an import's: `_matching_scope` finds the candidate's
enclosing named function in ``current`` and the ONE function in ``before``
that is provably the same one (same node type, same name — never more than
one match, since two overloads or two same-named siblings make the match
ambiguous rather than wrong), and `_references_in_scope` counts references
— and blanks declarations — inside that function's subtree only. A binding
whose nearest enclosing function is anonymous (a lambda, an arrow function, a
Go func literal) can never be matched this way and is always left alone.

A third correction is needed for the one case where blanking a variable's own
declaration span blanks a REAL reference along with it:
`_bound_name_occurs_once_in_own_span` refuses a candidate whose own
declaration node contains more than one leaf spelling its name, which happens
when an ASI-hazard fold fuses an adjacent statement into the same declaration
node (``const run = pickRunner()\n(run)()`` becomes, after the ``if`` between
them is deleted, ONE ``lexical_declaration`` whose own initializer already
reads ``run``) or when a binding is genuinely self-recursive
(``const handler = () => handler()``). Both cases would otherwise strip the
declaration and leave a reference to a name that no longer exists, and no
downstream gate catches it — it parses, and the statement count an ASI check
watches does not move when only the ``const run = `` prefix is dropped.

Go's implicit local name for an import is the imported package's *declared*
name, which is not reliably the last path segment (``gopkg.in/yaml.v3`` binds
``yaml``); resolving it truly needs the module graph. But if the last-segment
guess is wrong, that guessed name was never referenced BEFORE either — so the
import is not a candidate and is left alone. A wrong guess costs a missed tidy,
never a broken build, and it does so by construction rather than by care.

Pre-existing unused imports need no special case for the same reason: they were
not referenced before, so they are never candidates.

This module owns its own parsers rather than borrowing ``syntax``'s, following
``go_fold`` / ``ruby_fold`` / ``php_fold`` / ``python_fold``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator

import tree_sitter_c_sharp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

#: ``js`` maps to the TSX grammar deliberately, for the same reason
#: ``ts_syntax._parser`` does: the engine rewrote with it, and JSX in a ``.js``
#: file is ordinary in React codebases.
_GRAMMARS = {
    "go": tree_sitter_go.language,
    "java": tree_sitter_java.language,
    "csharp": tree_sitter_c_sharp.language,
    "ts": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "js": tree_sitter_typescript.language_tsx,
}

#: The languages this pass acts on at all. Python, PHP, Ruby, Kotlin, Dart and
#: Swift are absent because they are not where the compile errors are, not
#: because the mechanism forbids them — each would need its own measured
#: decidability table before being added.
SUPPORTED = frozenset(_GRAMMARS)

#: The node that holds an import declaration, per language. A name occurring
#: INSIDE one of these does not count as a reference to itself.
_IMPORT_ROOTS = {
    "go": "import_declaration",
    "java": "import_declaration",
    "csharp": "using_directive",
    "ts": "import_statement",
    "tsx": "import_statement",
    "js": "import_statement",
}


@lru_cache(maxsize=None)
def _parser(language: str) -> Parser:
    try:
        grammar = _GRAMMARS[language]
    except KeyError:
        raise ValueError(f"unsupported language {language!r}") from None
    return Parser(Language(grammar()))


def _parse(source: str, language: str) -> Node:
    return _parser(language).parse(source.encode("utf-8")).root_node


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _inside_import(node: Node, language: str) -> bool:
    root_type = _IMPORT_ROOTS[language]
    current = node.parent
    while current is not None:
        if current.type == root_type:
            return True
        current = current.parent
    return False


def references(source: str, name: str, language: str) -> int:
    """How many times ``name`` is referenced outside the import declarations.

    A mention inside a comment counts, on purpose. It costs only a missed tidy
    when a name coincidentally appears in prose, and it is what makes a Javadoc
    ``{@link Map}`` safe without a Javadoc-specific carve-out.

    Only childless nodes are considered, because those are the file's actual
    tokens — a name inside a string literal therefore counts too, which is the
    conservative direction.
    """
    word = re.compile(rf"\b{re.escape(name)}\b")
    total = 0
    for node in _walk(_parse(source, language)):
        if node.child_count:
            continue
        text = node.text.decode("utf-8", "replace")
        if "comment" in node.type:
            total += len(word.findall(text))
        elif text == name and not _inside_import(node, language):
            total += 1
    return total


#: A trailing `.vN` on a Go module path is a semantic-import-versioning suffix,
#: not a package name — `gopkg.in/yaml.v3` binds `yaml`.
_GO_VERSION_SUFFIX = re.compile(r"\.v\d+$")

#: A trailing `/vN` PATH SEGMENT is the other semantic-import-versioning
#: spelling — `github.com/x/y/v2` binds `y`, not `v2`. Distinct from the dot
#: form above: this one is a whole segment, stripped before the last-segment
#: guess rather than off the end of it.
_GO_VERSION_SEGMENT = re.compile(r"^v\d+$")


@dataclass(frozen=True)
class Candidate:
    """One binding that could be removed, and the bytes that would go with it.

    ``text`` is captured for the pull request body: every removal is named
    there, because the deletion is sound by construction but the code was the
    customer's.
    """

    name: str
    start: int
    end: int
    kind: str
    text: str
    #: Byte span of the initializer expression, for a variable. ``None`` for an
    #: import, and for a declaration with no initializer.
    initializer: tuple[int, int] | None = None
    #: Byte span of the NAME TOKEN this declaration binds — the one position
    #: inside the declaration that is not a read. ``None`` for an import,
    #: which self-excludes structurally via ``_inside_import``.
    binder: tuple[int, int] | None = None


def _candidate(name: str, node: Node, source_bytes: bytes, kind: str) -> Candidate:
    return Candidate(
        name=name,
        start=node.start_byte,
        end=node.end_byte,
        kind=kind,
        text=source_bytes[node.start_byte:node.end_byte].decode("utf-8", "replace"),
    )


def _go_import_candidates(root: Node, source_bytes: bytes) -> list[Candidate]:
    """Every `import_spec` whose binding name can be decided.

    Two spellings are excluded structurally rather than by name:

    * a ``blank_identifier`` child (``_ "github.com/lib/pq"``) — imported for
      its ``init()`` alone, so it is never unused;
    * a ``dot`` child (``. "path"``) — its names arrive unqualified, so nothing
      in the file spells the import, and usage is undecidable.

    An `import_spec` that is the ONLY spec in its `import_declaration` spans
    the whole declaration instead of just itself — the TS-family "solo
    binding" treatment, one grammar over. Two shapes hit this, and the
    parenthesized one is cosmetic while the bare one is not: a lone spec
    inside `import (...)` (`import_spec_list`) leaves an empty
    `import (\\n)` if only the spec goes, which is legal but untidy; a lone
    spec with NO parens at all (`import "fmt"`, `import_spec`'s parent is the
    declaration directly) leaves a bare `import` keyword with no path if only
    the spec goes — not cosmetic, `gofmt` calls it "missing import path" and
    Gate 1 correctly refuses the whole flag over it, which is a missed
    cleanup dressed up as a refusal. Both are fixed by the same rule: decide
    "solo" from a COUNT of `import_spec` children in the immediate parent
    (`import_spec_list` or the bare `import_declaration`), never from which
    of the two shapes it is.
    """
    candidates = []
    for node in _walk(root):
        if node.type != "import_spec":
            continue
        children = {child.type for child in node.children}
        if "blank_identifier" in children or "dot" in children:
            continue
        alias = node.child_by_field_name("name")
        if alias is not None:
            name = alias.text.decode("utf-8", "replace")
        else:
            path_node = node.child_by_field_name("path")
            if path_node is None:
                continue
            path = path_node.text.decode("utf-8", "replace").strip('"`')
            segments = path.split("/")
            last = segments[-1]
            if len(segments) > 1 and _GO_VERSION_SEGMENT.match(last):
                last = segments[-2]
            name = _GO_VERSION_SUFFIX.sub("", last)
        if not name.isidentifier():
            continue
        parent = node.parent
        declaration = parent if parent is not None and parent.type == "import_declaration" else (
            parent.parent if parent is not None else None
        )
        solo = (
            parent is not None
            and sum(1 for c in parent.children if c.type == "import_spec") == 1
        )
        target = declaration if solo and declaration is not None else node
        candidates.append(_candidate(name, target, source_bytes, "import"))
    return candidates


def _java_import_candidates(root: Node, source_bytes: bytes) -> list[Candidate]:
    """Every `import_declaration` that binds a name the file can spell.

    A wildcard is excluded structurally, by its ``asterisk`` child: it binds
    every type in the package without naming any of them, so no name-based
    analysis can decide whether it is needed.

    A static import binds its LAST segment as a bare name
    (``import static org.junit.Assert.assertEquals`` binds ``assertEquals``),
    which is the same last-segment rule the non-static form uses for the simple
    type name — so one branch serves both.
    """
    candidates = []
    for node in _walk(root):
        if node.type != "import_declaration":
            continue
        if any(child.type == "asterisk" for child in node.children):
            continue
        scoped = next(
            (c for c in node.children if c.type in {"scoped_identifier", "identifier"}),
            None,
        )
        if scoped is None:
            continue
        name = scoped.text.decode("utf-8", "replace").rsplit(".", 1)[-1]
        if not name.isidentifier():
            continue
        candidates.append(_candidate(name, node, source_bytes, "import"))
    return candidates


def _ts_import_candidates(root: Node, source_bytes: bytes) -> list[Candidate]:
    """Every binding an `import_statement` introduces, at specifier granularity.

    A side-effect import (``import './se'``) is excluded structurally: it has no
    ``import_clause`` child at all. That is a property of the node rather than a
    name on a list, and it is exactly right — such a statement binds nothing, so
    there is no name to have gone unused, and the module's load-time effect is
    the only reason it is written.

    A named specifier spans only itself, so ``import { a, b }`` can lose ``a``
    and keep ``b``. Removing the LAST specifier in a group is different,
    because it empties the group, and the candidate then grows to whatever
    the emptied node makes redundant. That is one cascade with two steps: the
    group's last specifier takes the ``named_imports`` node with it
    (``import React, { useState }`` becomes ``import React``), and a group
    that is also the clause's only binding takes the whole statement
    (``import { client } from './ff'`` goes entirely). An empty
    ``import {  } from './ff'`` parses, so no gate would ever catch it — it
    would simply ship to a reviewer on a tool whose pitch is that the diff
    arrives ready to review — and it changes nothing to remove, since an
    emptied clause still loads the module exactly like the statement it
    stands in for.

    A default or namespace import spans the whole statement ONLY when it is
    the clause's only binding — that is the statement's one reason to exist,
    so removing it removes the whole line. When it shares the clause with a
    sibling (``import React, { useState } from 'react'``;
    ``import d, * as ns from './m'``), the whole statement also binds that
    sibling, so a candidate spanning the statement would delete a name the
    fold never touched. There the candidate spans only the binding's own node
    (the ``identifier`` or the ``namespace_import``), and the comma that
    separated it from its sibling goes with it in ``_apply`` — a clause left
    reading ``import , { useState }`` or ``import d,  from './m'`` does not
    parse, and Gate 1 then refuses the whole flag, so a cleanup meant to make
    the diff reviewable would delete the diff.

    Deciding "solo or shared" is a count over the clause's own children — the
    binding-bearing node types, not the punctuation between them — never an
    assumption from one example shape.

    ``import type`` needs no special case: it is erased at compile time, so
    removing an unreferenced one is always safe, and it parses with the same
    clause shapes as a value import.
    """
    candidates = []
    for node in _walk(root):
        if node.type != "import_statement":
            continue
        clause = next(
            (c for c in node.children if c.type == "import_clause"), None
        )
        if clause is None:
            continue
        bindings = [
            c for c in clause.children
            if c.type in {"identifier", "namespace_import", "named_imports"}
        ]
        solo = len(bindings) == 1
        for child in clause.children:
            if child.type == "identifier":
                # Solo (`import d from './d'`) spans the whole statement, its
                # one binding. Shared (`import d, { a } from './m'`) spans
                # only `d`, so the sibling `{ a }` survives.
                candidates.append(
                    _candidate(
                        child.text.decode("utf-8", "replace"),
                        node if solo else child,
                        source_bytes,
                        "import",
                    )
                )
            elif child.type == "namespace_import":
                local = child.children[-1]
                candidates.append(
                    _candidate(
                        local.text.decode("utf-8", "replace"),
                        node if solo else child,
                        source_bytes,
                        "import",
                    )
                )
            elif child.type == "named_imports":
                specifiers = [
                    c for c in child.children if c.type == "import_specifier"
                ]
                # The group's LAST specifier takes the emptied group with
                # it, and the group takes the whole statement when it is the
                # clause's only binding — the cascade the docstring
                # describes, decided by a count at each level rather than by
                # which example shape this happens to be. With a sibling
                # left in the group, the specifier spans only itself.
                emptied = (
                    (node if solo else child) if len(specifiers) == 1 else None
                )
                for specifier in specifiers:
                    alias = specifier.child_by_field_name("alias")
                    local = alias if alias is not None else specifier.child_by_field_name("name")
                    if local is None:
                        continue
                    candidates.append(
                        _candidate(
                            local.text.decode("utf-8", "replace"),
                            specifier if emptied is None else emptied,
                            source_bytes,
                            "import",
                        )
                    )
    return candidates


def _csharp_import_candidates(root: Node, source_bytes: bytes) -> list[Candidate]:
    """Only ``using Alias = Foo.Bar;`` — the one decidable form.

    A plain ``using System;`` introduces no name that appears anywhere in the
    source: C# code writes ``Console.WriteLine``, never
    ``System.Console.WriteLine``. Knowing whether the directive is needed means
    resolving ``Console`` to ``System.Console`` across the compilation and every
    referenced assembly, which this tool cannot do and must not guess at.

    ``using static`` has the same problem one level down, and ``global using``
    affects files this pass never sees. All three are excluded structurally —
    the alias form is the one with a ``name`` field.
    """
    candidates = []
    for node in _walk(root):
        if node.type != "using_directive":
            continue
        if any(child.type in {"global", "static"} for child in node.children):
            continue
        alias = node.child_by_field_name("name")
        if alias is None:
            continue
        candidates.append(
            _candidate(
                alias.text.decode("utf-8", "replace"), node, source_bytes, "import"
            )
        )
    return candidates


def _import_candidates(source: str, language: str) -> list[Candidate]:
    """Every import binding in ``source`` whose usage this pass can decide."""
    source_bytes = source.encode("utf-8")
    root = _parse(source, language)
    if language == "go":
        return _go_import_candidates(root, source_bytes)
    if language == "java":
        return _java_import_candidates(root, source_bytes)
    if language in {"ts", "tsx", "js"}:
        return _ts_import_candidates(root, source_bytes)
    if language == "csharp":
        return _csharp_import_candidates(root, source_bytes)
    return []


#: Node types whose evaluation cannot have a side effect. Everything absent is
#: treated as effectful, which is the safe default: a call, an `await`, a
#: channel receive and a pointer dereference can all do work or panic, and
#: deleting the declaration that holds one would silently drop it.
#:
#: Measured directly against the four grammars this pass acts on (go, java,
#: csharp, ts) rather than assumed. Six entries were missing from an earlier
#: draft of this list and are called out here because each was found by
#: parsing real source, not by reasoning about the grammar: `integer_literal`
#: and `boolean_literal` are C#'s spellings (its `true`/`false` are anonymous
#: tokens wrapped in a `boolean_literal`, unlike Go/Java's own directly-named
#: `true`/`false` nodes); `member_expression` is TS's member access (its own
#: node type, distinct from `selector_expression`/`field_access`/
#: `member_access_expression`); `object` and `array` are TS literal
#: containers; `pair` is TS's `object -> pair -> property_identifier + value`
#: nesting, without which `{a: 1}` never reads as inert. TS arrays hold their
#: values directly as children, so `array` alone suffices there.
#:
#: Literal *content* leaves are deliberately NOT listed. `_subtree_is_inert`
#: recurses into every named child regardless of the parent's classification,
#: so each string-literal type here needs its content child to pass too — and
#: three of them used to be named here for that alone. They are answered
#: structurally now (a childless node is one token, and a token runs nothing),
#: which is what stops the next grammar's content leaf from silently making
#: its whole literal effectful.
_INERT_NODES = frozenset({
    "int_literal", "float_literal", "true", "false", "nil", "null",
    "string_literal", "interpreted_string_literal", "raw_string_literal",
    "rune_literal", "char_literal", "decimal_integer_literal",
    "hex_integer_literal", "decimal_floating_point_literal", "number",
    "string", "identifier", "field_identifier", "type_identifier",
    "selector_expression", "field_access", "member_access_expression",
    "index_expression", "array_access", "element_access_expression",
    "subscript_expression", "composite_literal", "literal_value",
    "keyed_element", "array_creation_expression", "property_identifier",
    "this", "predefined_type", "shorthand_property_identifier",
    "integer_literal", "boolean_literal", "member_expression", "object",
    "array", "pair",
})

#: Node types an inert initializer may be WRAPPED in without becoming
#: effectful. Kept separate from `_INERT_NODES` because these carry no value of
#: their own — they are punctuation around one, so they are judged ENTIRELY by
#: what they contain.
#:
#: The array members are why a `new int[3]` is inert while a `new int[f()]` is
#: not: the size sits inside one of these, so recursing through it reaches the
#: call and rejects it, where listing the array node as inert outright would
#: have swallowed the call with it.
_INERT_WRAPPERS = frozenset({
    "parenthesized_expression", "expression_list", "literal_element",
    "array_type", "array_rank_specifier", "integral_type", "dimensions_expr",
})

#: `unary_expression` (go/java/ts) and `prefix_unary_expression` (csharp) are
#: shared by a purely computational negation (`-1`, `!b`, `~c`) AND, in Go
#: only, a channel receive (`<-ch`) and a pointer dereference (`*p`) — both of
#: which can block or panic. The node type alone cannot distinguish them; the
#: OPERATOR CHILD can, structurally, without a per-language name list.
_UNARY_EXPRESSION_NODES = frozenset({"unary_expression", "prefix_unary_expression"})

#: Operators that are purely computational regardless of operand — safe to
#: treat as inert exactly when the operand itself is inert. Every other
#: operator sharing these node types (Go's `<-` and `*`) is effectful.
_COMPUTATIONAL_UNARY_OPERATORS = frozenset({"-", "+", "!", "~", "^"})


def is_inert(candidate: Candidate, source: str, language: str) -> bool:
    """Whether this declaration's initializer can be deleted without loss.

    Inert means no call, no object creation, no ``await``, no channel
    operation, no pointer dereference — nothing that could run code or panic.
    A declaration with no initializer at all is inert by definition.

    The classification is a whitelist and everything unknown is effectful. That
    asymmetry is deliberate: a wrongly-inert node deletes a side effect
    silently, while a wrongly-effectful one only costs the tidier of two
    correct outcomes.
    """
    if candidate.initializer is None:
        return True
    start, end = candidate.initializer
    for node in _walk(_parse(source, language)):
        if node.start_byte != start or node.end_byte != end:
            continue
        return _subtree_is_inert(node)
    return False


def _subtree_is_inert(node: Node) -> bool:
    # A CHILDLESS node is one token, and a token cannot run anything — the
    # behaviour in every effectful shape lives in the node that COMBINES
    # tokens (a call, an `await`, a channel receive), and that node is judged
    # on its own type before this is ever reached. So a leaf is only ever
    # asked about from inside a construct already classified inert, where it
    # is that construct's own punctuation or content.
    #
    # Structural, and it replaces three hand-listed content leaves that were
    # here for exactly this reason. A name list could only ever cover the
    # grammars someone had opened: `raw_string_literal_content` was missing,
    # so a Go raw string was judged effectful and ``x := `raw` `` was
    # rewritten to a discard while the identical `x := "plain"` beside it was
    # deleted — two string literals, two different diffs, for no reason a
    # reviewer of the output could defend.
    if node.child_count == 0:
        return True
    if node.type in _INERT_WRAPPERS:
        return all(
            _subtree_is_inert(child) for child in node.named_children
        )
    if node.type in _UNARY_EXPRESSION_NODES:
        return _unary_is_inert(node)
    if node.type not in _INERT_NODES:
        return False
    return all(_subtree_is_inert(child) for child in node.named_children)


def _unary_is_inert(node: Node) -> bool:
    """A unary expression is inert only when its operator is purely
    computational and its operand is inert.

    Structural, not a per-language name list: Go's ``<-`` (channel receive)
    and ``*`` (pointer dereference) share `unary_expression` with negation,
    and the only way to tell them apart is to read the operator token itself.
    go/java/ts name it with an ``operator`` field (and the operand with
    ``operand`` or, for ts, ``argument``); csharp's `prefix_unary_expression`
    names neither child, so its operator is the node's first child and its
    operand the second — always exactly two children.
    """
    operator_node = node.child_by_field_name("operator")
    if operator_node is not None:
        operand = node.child_by_field_name("operand")
        if operand is None:
            operand = node.child_by_field_name("argument")
    else:
        if node.child_count != 2:
            return False
        operator_node, operand = node.children
    if operand is None:
        return False
    operator = operator_node.text.decode("utf-8", "replace")
    if operator not in _COMPUTATIONAL_UNARY_OPERATORS:
        return False
    return _subtree_is_inert(operand)


#: ``(declaration node type, the node type its enclosing statement list uses)``
#: per language, plus the field names holding the bound name and the value.
_VARIABLE_DECLARATIONS = {
    "go": ("short_var_declaration", "left", "right"),
    "java": ("local_variable_declaration", None, None),
    "csharp": ("local_declaration_statement", None, None),
    "ts": ("lexical_declaration", None, None),
    "tsx": ("lexical_declaration", None, None),
    "js": ("lexical_declaration", None, None),
}

#: Node types that make a declaration LOCAL. A declaration not inside one of
#: these is a field, a parameter or a package-level binding, all of which are
#: referenced from files this pass never parses.
_FUNCTION_BODIES = frozenset({
    "block", "function_body", "statement_block", "function_declaration",
    "method_declaration", "function_definition",
})


def _variable_candidates(source: str, language: str) -> list[Candidate]:
    """Every single-name local declaration, with its initializer span."""
    declaration_type, name_field, value_field = _VARIABLE_DECLARATIONS[language]
    source_bytes = source.encode("utf-8")
    candidates = []
    for node in _walk(_parse(source, language)):
        if node.type != declaration_type:
            continue
        if not _has_ancestor(node, _FUNCTION_BODIES):
            continue
        pair = _declared_name_and_value(node, name_field, value_field)
        if pair is None:
            continue
        name_node, value_node = pair
        name = name_node.text.decode("utf-8", "replace")
        # A binding whose declared name is not a NAME is never a candidate.
        # The bound position of a destructuring declarator is a whole pattern
        # (`const { a, b } = load()`, `const [x, y] = load()`), and taking its
        # source text as the binding name runs the differential against a
        # string nothing can reference — `{ a, b }` reaching zero says nothing
        # about `a` or `b`, and the declaration was deleted with both of them
        # still in use below it. Two properties, because they answer two
        # different questions: a pattern is not one token, in any of these
        # four grammars, and a token is not a name unless it spells like one.
        # Two of the four import paths carry the second check already; the
        # TS and C# ones do not, and do not need it — every name they can
        # produce comes from a field the grammar has already committed to
        # being an identifier.
        if name_node.child_count or not name.isidentifier():
            continue
        candidates.append(
            Candidate(
                name=name,
                start=node.start_byte,
                end=node.end_byte,
                kind="variable",
                text=source_bytes[node.start_byte:node.end_byte].decode(
                    "utf-8", "replace"
                ),
                initializer=(
                    (value_node.start_byte, value_node.end_byte)
                    if value_node is not None
                    else None
                ),
                binder=(name_node.start_byte, name_node.end_byte),
            )
        )
    return candidates


def _has_ancestor(node: Node, types: frozenset[str]) -> bool:
    current = node.parent
    while current is not None:
        if current.type in types:
            return True
        current = current.parent
    return False


def _declared_name_and_value(
    node: Node, name_field: str | None, value_field: str | None
) -> tuple[Node, Node | None] | None:
    """The single bound name and its initializer, or ``None`` if not single.

    Go's ``short_var_declaration`` names its sides with fields; Java and TS
    nest a declarator whose own ``name``/``value`` fields carry the answer
    directly. C# nests ONE LEVEL DEEPER than those two
    (``local_declaration_statement -> variable_declaration ->
    variable_declarator``) and its declarator has NO ``value`` field at all —
    both measured, not assumed. Missing either correction leaves C# variables
    unreachable (no candidates ever produced) or, worse, produces a candidate
    whose initializer always reads as absent — which would classify every C#
    variable as inert and delete its declaration, call included.
    """
    if name_field is not None:
        left = node.child_by_field_name(name_field)
        right = node.child_by_field_name(value_field)
        if left is None or len(left.named_children) > 1:
            return None
        name_node = left.named_children[0] if left.named_children else left
        if right is not None and len(right.named_children) > 1:
            return None
        value_node = right.named_children[0] if right and right.named_children else right
        return name_node, value_node
    declarators = [
        child
        for child in node.named_children
        if child.type
        in {"variable_declarator", "variable_declaration"}
    ]
    if len(declarators) != 1:
        return None
    declarator = declarators[0]
    if declarator.type == "variable_declaration":
        # C#'s intermediate wrapper. Descend to the real declarator, and
        # require exactly one — `int x = 1, y = 2;` binds more than one name
        # and must be skipped, the same as Go's `a, b := f()`.
        inner = [
            child for child in declarator.named_children
            if child.type == "variable_declarator"
        ]
        if len(inner) != 1:
            return None
        declarator = inner[0]
    name_node = declarator.child_by_field_name("name")
    if name_node is None:
        return None
    value_node = declarator.child_by_field_name("value")
    if value_node is None:
        # C# leaves the initializer with no field name at all. Fall back to
        # whichever named child is not the name — present only when an
        # initializer actually exists (a bare `int x;` declarator has just
        # the one named child, the name itself).
        rest = [child for child in declarator.named_children if child is not name_node]
        if rest:
            value_node = rest[-1]
    return name_node, value_node


#: How each language spells "evaluate this and throw the value away".
#: Go's ``_ = expr`` is legal for EVERY expression, which is why Go never
#: reaches the leave-alone branch. The other three have no such universal
#: spelling: a bare expression statement is legal exactly for the invocation /
#: object-creation / ``await`` forms, which is what makes an initializer
#: effectful in the first place — so the overlap is total in practice and the
#: guard below is the honest backstop for the rest.
_DISCARD_PREFIX = {"go": "_ = "}

#: Node types that ARE a legal expression statement in Java, C# and TS. Java's
#: list is JLS 14.8; C# and TS admit the same shapes plus ``await``.
#:
#: `unary_expression`/`prefix_unary_expression` is deliberately absent: it is
#: how a plain negation (`-1`) reaches an initializer, and `-1;` is not a
#: legal statement in any of these languages under JLS 14.8 — an earlier
#: draft of this set included it, which would have emitted uncompilable Java
#: for exactly that shape. A negation IS caught upstream, by `is_inert`
#: (`_unary_is_inert`), which deletes the whole declaration instead of
#: routing it through this discard path; this set only needs to describe what
#: is safe to keep as a bare statement, and a bare unary expression never is.
_STATEMENT_EXPRESSIONS = frozenset({
    "method_invocation", "object_creation_expression", "invocation_expression",
    "call_expression", "new_expression", "await_expression",
    "assignment_expression", "update_expression",
})

def _apply(source: str, candidate: Candidate, language: str) -> str | None:
    """Remove or neutralise ``candidate``; ``None`` when neither is possible.

    Imports are deleted outright — deleting one cannot change behaviour.

    A variable is three-way: an inert initializer is deleted with its
    declaration, an effectful one is rewritten to a discard so the call
    survives, and anything that is neither is left exactly as it was.

    Only the DELETE half is refused for a declaration that fills a slot in
    its parent's grammar (``_fills_a_parent_slot``); such a candidate FALLS
    THROUGH to the discard half rather than being abandoned. That is the
    whole point of the distinction: `if _ = 3; cond {` and
    `for (compute(); hasNext(); )` are legal in their headers, so refusing a
    header declaration outright gives up a working outcome — and in Go it
    gives up a REQUIRED one. A stranded Go local that is neither deleted nor
    discarded is `declared and not used`, which is a compile error, and one
    that no gate downstream can see: the file still parses, so Gate 1 passes
    it and the pull request simply does not build. Abandoning an inert header
    declaration is therefore strictly worse than the hole it was meant to
    avoid — a hole is loud, a non-building branch is silent.
    """
    data = source.encode("utf-8")
    deletable = candidate.kind == "import" or is_inert(candidate, source, language)
    if deletable and not _fills_a_parent_slot(source, candidate, language):
        start, end = candidate.start, candidate.end
        if _sits_in_comma_separated_list(source, candidate, language):
            start, end = _expand_over_separator(data, start, end)
        start, end = _expand_to_whole_lines(data, start, end)
        return (data[:start] + data[end:]).decode("utf-8")
    if candidate.initializer is None:
        return None
    init_start, init_end = candidate.initializer
    initializer = data[init_start:init_end].decode("utf-8")
    prefix = _DISCARD_PREFIX.get(language)
    if prefix is None and not _is_statement_expression(source, candidate, language):
        return None
    replacement = f"{prefix or ''}{initializer}"
    if language != "go":
        replacement += ";"
    return (
        data[: candidate.start] + replacement.encode("utf-8") + data[candidate.end :]
    ).decode("utf-8")


def _fills_a_parent_slot(
    source: str, candidate: Candidate, language: str
) -> bool:
    """Whether the candidate's node is its parent's ONLY child under some
    named field — a slot the grammar requires something in, rather than one
    entry in a list.

    A free-standing statement is an unnamed child of a block: deleting it
    leaves a shorter block, which is exactly what this pass wants. A
    declaration in a header is bound to a field — Go's `if`/`for`
    ``initializer``, Java's for ``init``, TS's for ``initializer`` — and
    deleting it leaves the hole the field described: `for ( hasNext(); ) {`,
    `if ; cond {`. Both parse as errors, so Gate 1 refuses the whole flag and
    the customer loses the fold along with the cleanup.

    Having a field name is NOT enough on its own, and assuming it was cost a
    real capability: tree-sitter's TS grammar binds every statement in an
    unbraced `case:`/`default:` body to a REPEATED ``body`` field, so an
    ordinary declaration there claimed to fill a slot and stopped being
    cleaned, while the identical declaration one brace deeper was cleaned
    normally. A repeated field is a list wearing a name — deleting one entry
    leaves the others and no hole — so what actually distinguishes a slot is
    that the parent has exactly ONE child under that name.

    Asked of the grammar rather than of a list of header node types, which
    is what makes it answer for shapes nobody enumerated: C#'s for-initializer
    is a `variable_declaration` and never reaches this pass at all, and a
    grammar that starts naming a field this pass has never seen is handled on
    the first run rather than after someone remembers to add it.
    """
    node = _node_at(source, language, candidate.start, candidate.end)
    if node is None or node.parent is None:
        return False
    parent = node.parent
    field = None
    for index in range(parent.child_count):
        if parent.child(index).id == node.id:
            field = parent.field_name_for_child(index)
            break
    if field is None:
        return False
    siblings = sum(
        1
        for index in range(parent.child_count)
        if parent.field_name_for_child(index) == field
    )
    return siblings == 1


def _is_statement_expression(
    source: str, candidate: Candidate, language: str
) -> bool:
    start, end = candidate.initializer
    for node in _walk(_parse(source, language)):
        if node.start_byte == start and node.end_byte == end:
            return node.type in _STATEMENT_EXPRESSIONS
    return False


def _sits_in_comma_separated_list(
    source: str, candidate: Candidate, language: str
) -> bool:
    """Whether the candidate's own node is one element of a comma-separated
    list, so that deleting it would leave a dangling separator behind.

    Asked of the PARENT NODE — does it hold a comma of its own? — and never
    of a list of parent type names. The three TS shapes that need this
    (a specifier inside ``{ a, b }``, a default or namespace binding sharing
    an ``import_clause`` with a sibling, and the emptied group that replaces
    that sibling) have three different parent types and one property in
    common, and a name list had already been written with only the first of
    them on it. Go's ``import_spec_list`` separates with newlines rather
    than commas and simply answers no.

    It is safe to over-answer here: ``_expand_over_separator`` consumes a
    comma only when one is actually adjacent to the span, so a parent that
    holds a comma somewhere else leaves the span exactly as it was.
    """
    for node in _walk(_parse(source, language)):
        if node.start_byte == candidate.start and node.end_byte == candidate.end:
            parent = node.parent
            return parent is not None and any(
                child.type == "," for child in parent.children
            )
    return False


def _expand_to_whole_lines(data: bytes, start: int, end: int) -> tuple[int, int]:
    """Widen a byte range to swallow its line when nothing else shares it.

    Deleting only the node's own span leaves the indentation that preceded it
    and the newline that followed — a blank line where the binding was. Widening
    is conditional: a binding sharing its line with live code takes only its own
    span, so the neighbour survives. Mirrors
    :func:`flag_cleanup.syntax._expand_to_whole_lines`, deliberately, since the
    two solve the same problem for different node kinds.
    """
    line_start = data.rfind(b"\n", 0, start) + 1
    line_end = data.find(b"\n", end)
    line_end = len(data) if line_end == -1 else line_end + 1
    before_blank = data[line_start:start].strip() == b""
    after_blank = data[end : line_end - 1 if line_end > end else line_end].strip() == b""
    if before_blank and after_blank:
        return line_start, line_end
    return start, end


def _expand_over_separator(data: bytes, start: int, end: int) -> tuple[int, int]:
    """Swallow the comma that would otherwise be left dangling."""
    after = end
    while after < len(data) and data[after : after + 1] in (b" ", b"\t"):
        after += 1
    if data[after : after + 1] == b",":
        after += 1
        while after < len(data) and data[after : after + 1] in (b" ", b"\t"):
            after += 1
        return start, after
    before = start
    while before > 0 and data[before - 1 : before] in (b" ", b"\t"):
        before -= 1
    if data[before - 1 : before] == b",":
        return before - 1, end
    return start, end


def _own_binder_spans(source: str, name: str, language: str) -> list[tuple[int, int]]:
    """Byte spans, in ``source``, of every NAME TOKEN a local variable
    declaration of ``name`` binds.

    The token, never the declaration. A declaration's own name is the one
    position inside it that is not a read — everything else it contains,
    initializer included, is a reference like any other. Hiding the whole
    declaration span instead hides a nested re-bind's initializer along with
    it, and ``x := x + 1`` reads the OUTER ``x`` there: blanking that span
    made the outer binding look unreferenced and deleted it out from under a
    live read (`undefined: x`, or a TDZ `ReferenceError` in TS).

    Only the variable form is collected here. An import needs no such
    treatment: ``references()`` already excludes a name from inside its own
    import declaration structurally, via ``_inside_import``. A variable's
    declaration has no equivalent exclusion, which is the gap this function
    exists to close — see ``_references_excluding_own_declaration``.

    Re-derived from ``source`` on every call rather than taken from some
    already-computed candidate: a candidate's byte offsets belong to
    whichever source it was parsed from, and the whole point of this
    function is to answer the same question about a DIFFERENT source (most
    often ``before``, while iterating candidates parsed from ``current``).
    """
    return [
        candidate.binder
        for candidate in _variable_candidates(source, language)
        if candidate.name == name and candidate.binder is not None
    ]


def _references_excluding_own_declaration(source: str, name: str, language: str) -> int:
    """``references(source, name, language)``, blind to the NAME TOKEN
    ``name``'s own variable declaration(s) in ``source`` bind.

    This is the correction the module docstring describes: gating on
    ``references(before, name) == 0`` alone can never reject a variable,
    because a variable's declaration always counts as one reference to
    itself. Blanking the bound TOKEN of every declaration of ``name`` in
    THIS source before counting makes "referenced before/after the fold"
    mean the same thing for a variable that it already means for an import —
    a binding the customer had already left unused must never look
    referenced merely because it was declared, on either side of the
    comparison. The token and not the declaration, for the reason
    ``_own_binder_spans`` gives: a nested re-bind's initializer is a real
    read of the binding it shadows, and hiding it deletes live code.
    """
    data = source.encode("utf-8")
    for start, end in _own_binder_spans(source, name, language):
        data = data[:start] + b" " * (end - start) + data[end:]
    return references(data.decode("utf-8", "replace"), name, language)


#: The node types, per language, whose subtree bounds ONE local-variable
#: scope. This is a different, FINER set than ``_FUNCTION_BODIES``:
#: ``_FUNCTION_BODIES`` only asks whether a declaration is local at all, and
#: is satisfied by any nested ``block`` — it cannot tell two sibling
#: functions apart, which is exactly the defect this set exists to fix (a
#: file-wide same-name blanking conflated two unrelated functions' `x`).
#: Every entry here is the node kind that OWNS a function/method/lambda body,
#: named or not — an anonymous member (a Go ``func_literal``, a Java
#: ``lambda_expression``, a bare TS ``arrow_function``) is exactly what makes
#: a scope AMBIGUOUS, via ``_scope_name`` below.
_SCOPE_ROOTS = {
    "go": frozenset({"function_declaration", "method_declaration", "func_literal"}),
    "java": frozenset({
        "method_declaration", "constructor_declaration", "lambda_expression",
    }),
    "csharp": frozenset({
        "method_declaration", "constructor_declaration", "local_function_statement",
        "lambda_expression", "anonymous_method_expression",
    }),
    "ts": frozenset({
        "function_declaration", "method_definition", "function_expression",
        "arrow_function", "generator_function_declaration",
    }),
}
_SCOPE_ROOTS["tsx"] = _SCOPE_ROOTS["ts"]
_SCOPE_ROOTS["js"] = _SCOPE_ROOTS["ts"]


def _enclosing_scope(node: Node, language: str) -> Node | None:
    """The nearest ancestor whose type bounds a local-variable scope.

    ``None`` when ``node`` is not inside one of ``_SCOPE_ROOTS`` at all — a
    conditional at module scope in TS/JS, outside any function, is the
    measured case; there is no function to match against ``before``, so the
    candidate is left alone rather than matched against nothing.
    """
    roots = _SCOPE_ROOTS[language]
    current = node.parent
    while current is not None:
        if current.type in roots:
            return current
        current = current.parent
    return None


def _scope_name(scope: Node) -> str | None:
    """The scope's own identifier, or ``None`` if it has none.

    Measured (not assumed) across go/java/csharp/ts/tsx/js: every NAMED
    function/method/constructor node type in ``_SCOPE_ROOTS`` spells its own
    name with a ``name`` field, and every ANONYMOUS one — a Go
    ``func_literal``, a Java ``lambda_expression``, a C#
    ``anonymous_method_expression``, a bare TS ``arrow_function`` or an
    unnamed ``function_expression`` — has no such field at all. So this one
    field lookup IS the anonymity test; no separate per-language "which node
    types are anonymous" table is needed.
    """
    name_node = scope.child_by_field_name("name")
    return name_node.text.decode("utf-8", "replace") if name_node is not None else None


def _node_at(source: str, language: str, start: int, end: int) -> Node | None:
    for node in _walk(_parse(source, language)):
        if node.start_byte == start and node.end_byte == end:
            return node
    return None


def _matching_scope(
    before: str, current: str, candidate: Candidate, language: str
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The candidate's enclosing scope in ``current``, paired with the ONE
    scope in ``before`` that is provably the same function — as byte spans,
    since a ``Node`` is only valid against the parse it came from and the two
    scopes come from two different parses. ``None`` when that pairing cannot
    be decided safely.

    Two same-named locals in different functions must never be conflated —
    that is the entire reason to scope this at all. A scope is usable only
    when it has a NAME (an anonymous function can never be matched between
    two sources) and when exactly one scope of the SAME NODE TYPE and NAME
    exists in ``before``: more than one (two overloads, a duplicate name in a
    sibling function) is exactly as unsafe to guess between as zero, so both
    refuse rather than pick one.
    """
    candidate_node = _node_at(current, language, candidate.start, candidate.end)
    if candidate_node is None:
        return None
    current_scope = _enclosing_scope(candidate_node, language)
    if current_scope is None:
        return None
    name = _scope_name(current_scope)
    if name is None:
        return None
    matches = [
        node
        for node in _walk(_parse(before, language))
        if node.type == current_scope.type and _scope_name(node) == name
    ]
    if len(matches) != 1:
        return None
    return (matches[0].start_byte, matches[0].end_byte), (
        current_scope.start_byte,
        current_scope.end_byte,
    )


def _references_in_scope(
    source: str, name: str, language: str, start: int, end: int
) -> int:
    """``references()``, restricted to leaf nodes inside the half-open byte
    span ``[start, end)`` of ``source``, and blind to the NAME TOKEN each of
    ``name``'s own variable declarations inside that SAME span binds.

    Scoping both the count and the exclusion to one function's subtree is
    what stops two same-named locals in different functions from
    cross-contaminating: a file-wide version of the binder-blanking made
    one function's own use of `x` look like a reference to a completely
    unrelated function's same-named, untouched `x` — deleting the second
    function's binding as a side effect of folding the first.

    Excluding the bound TOKEN and not the whole declaration span is the
    other half. A declaration of `x` nested inside a scope that already has
    one is a SHADOWING re-bind, and its initializer (`x := x + 1`) reads the
    outer `x`: hiding the whole span hid that read too, so the outer binding
    read as unreferenced after the fold and was deleted while the inner one
    still asked for it. Only the binding position is not a read; every other
    leaf inside a declaration is counted like any other leaf in the scope.
    """
    own_binders = [
        candidate.binder
        for candidate in _variable_candidates(source, language)
        if candidate.name == name
        and candidate.binder is not None
        and start <= candidate.binder[0]
        and candidate.binder[1] <= end
    ]
    word = re.compile(rf"\b{re.escape(name)}\b")
    total = 0
    for node in _walk(_parse(source, language)):
        if node.child_count:
            continue
        if node.start_byte < start or node.end_byte > end:
            continue
        if any(
            d_start <= node.start_byte and node.end_byte <= d_end
            for d_start, d_end in own_binders
        ):
            continue
        # `obj.x`'s `x` is a member of `obj`, never a read of a binding
        # spelled `x` — the same distinction `_bound_name_occurs_once_in_own_span`
        # already draws, and needed here for the same reason. Blanking a
        # declaration's whole span used to hide `const x = obj.x`'s property
        # read as a side effect; now that only the bound token is hidden, the
        # exclusion has to be made where it is actually true. It keys on the
        # grammar FIELD (see `_is_member_access_property`), because java and
        # csharp spell a property with the same node type a real reference uses.
        if _is_member_access_property(node, language):
            continue
        text = node.text.decode("utf-8", "replace")
        if "comment" in node.type:
            total += len(word.findall(text))
        elif text == name and not _inside_import(node, language):
            total += 1
    return total


#: ``(member-access node type, property/field NAME)`` per language — the
#: grammar shape of ``obj.x``. Measured directly against all four grammars
#: this pass acts on rather than assumed: java and csharp both spell the
#: property as a plain ``identifier``, the SAME node type a genuine
#: reference uses, so node type alone cannot tell ``obj.x``'s ``x`` from a
#: real read of a binding named ``x`` — only the FIELD the parent binds it
#: under can. ts/go happen to give the property its own node type
#: (``property_identifier`` / ``field_identifier``), which is why a
#: type-based rule would look like it worked: it would pass for two
#: languages and silently miss the other two.
_MEMBER_ACCESS_PROPERTY = {
    "go": ("selector_expression", "field"),
    "java": ("field_access", "field"),
    "csharp": ("member_access_expression", "name"),
    "ts": ("member_expression", "property"),
    "tsx": ("member_expression", "property"),
    "js": ("member_expression", "property"),
}


def _is_member_access_property(leaf: Node, language: str) -> bool:
    """Whether ``leaf`` sits in the PROPERTY/FIELD position of a member
    access — ``obj.x``'s ``x``, never ``x`` read as a name in its own right.

    Keyed on the grammar FIELD, not the leaf's node type, because ``obj.x``
    and a genuine reference to ``x`` can share the exact same leaf type
    (java's and csharp's property is a bare ``identifier``, indistinguishable
    from an ordinary name by type alone). ``child_by_field_name`` returns a
    FRESH ``Node`` per call, so the comparison is by ``.id``, never ``is`` —
    the same lesson ``key_const``'s C# declarator lookup already paid for.
    """
    member_type, field = _MEMBER_ACCESS_PROPERTY.get(language, (None, None))
    if member_type is None:
        return False
    parent = leaf.parent
    if parent is None or parent.type != member_type:
        return False
    field_node = parent.child_by_field_name(field)
    return field_node is not None and field_node.id == leaf.id


def _bound_name_occurs_once_in_own_span(
    candidate: Candidate, current: str, language: str
) -> bool:
    """Whether ``candidate.name`` names exactly one leaf inside the
    CANDIDATE'S OWN declaration span — the binding itself, and nothing else.

    A variable's own declaration is the one span this pass otherwise treats
    as blind to the name it binds: ``_references_in_scope`` blanks it before
    counting, for the reason the module docstring gives (a declaration always
    "references" its own name once, so counting it would make an already-dead
    binding look referenced). That blanking silently swallows a REAL
    reference too when a fold fuses two adjacent statements into one
    declaration node via ASI — written without a semicolon,
    ``const run = pickRunner()\\n(run)()`` parses, once whatever separated
    them is deleted, as a SINGLE ``lexical_declaration`` whose own
    initializer already contains a genuine use of ``run``. Blanking that
    whole span before counting then hides that reference along with the
    declaration, so the binding reads as unreferenced after the fold and gets
    "removed" — stripping ``const run = `` and shipping
    ``pickRunner()\\n(run)();``, a `ReferenceError` at a name that no longer
    exists anywhere in the file. No gate downstream catches it: it parses,
    strands no keyword, and the statement count the ASI check watches is
    unchanged by dropping four characters from the front of one declaration.

    Counting LEAVES inside the declaration's own span instead — never
    blanking anything, since here the binding's own occurrence is exactly
    what should count as the first one — catches both the ASI-fusion case
    above and the deliberate variant, a self-recursive closure genuinely
    reading its own binding (``const handler = () => handler()``): either
    way, more than one occurrence means something besides the declaration
    itself lives in this span, and this candidate must be left alone rather
    than acted on. An ordinary declaration (``const x = 3``, ``const items =
    load()``) names itself exactly once and is unaffected.

    A leaf sitting in the PROPERTY/FIELD position of a member access is
    excluded from the count — ``obj.x``'s ``x`` is not a reference to a
    binding named ``x``, it is a property with the same spelling, and
    ``const x = obj.x`` is one of the most common initializer idioms there
    is. Without the exclusion this guard refuses that shape forever (a
    coverage regression, not a broken build, but a real one): see
    ``_is_member_access_property`` for why the exclusion has to key on the
    grammar FIELD rather than the leaf's node type.
    """
    node = _node_at(current, language, candidate.start, candidate.end)
    if node is None:
        return True
    count = 0
    for leaf in _walk(node):
        if leaf.child_count:
            continue
        if _is_member_access_property(leaf, language):
            continue
        if leaf.text.decode("utf-8", "replace") == candidate.name:
            count += 1
            if count > 1:
                return False
    return True


def remove_stranded_bindings(
    before: str, after: str, language: str
) -> tuple[str, list[Candidate]]:
    """Delete or neutralise the bindings this fold stranded.

    Returns the new source and a record per binding acted on, so the pull
    request can name each one — the changes are sound by construction, but the
    code was the customer's.

    A binding qualifies only if it was referenced in ``before`` and is
    referenced nowhere in ``after``. For an IMPORT that count is file-wide
    (``_references_excluding_own_declaration`` — imports self-exclude from
    their own declaration structurally, via ``_inside_import``). For a
    VARIABLE the count is scoped to its one matched enclosing function
    (``_matching_scope`` + ``_references_in_scope``): a name is not a
    binding, so two functions' own same-named locals must never be
    conflated, and a candidate whose scope cannot be matched unambiguously
    between ``before`` and ``current`` — anonymous, ambiguous, or simply
    absent — is left alone rather than guessed at. That is the entire safety
    argument: it excludes the customer's pre-existing unused bindings, and it
    makes a wrong guess (of a name OR of a scope) a missed tidy rather than a
    broken build.

    Iterated to a fixpoint because the two analyses feed each other — deleting
    a stranded variable can strand the import of its type, which no single pass
    over either would catch. Each round re-derives candidates from ``current``
    and stops at the first successful edit, because byte offsets in the
    remaining candidates go stale the moment the source changes. A round IS an
    edit, therefore, and the budget is one round per binding this file could
    possibly lose plus one to observe the fixpoint — measured from the file,
    never a constant. A constant is what this had (ten), and a file with
    eleven stranded imports kept the eleventh: in Go that is a compile error,
    so the pull request did not build, and the pass returned as though it had
    finished.

    The bound cannot be reached. One edit deletes or neutralises exactly one
    binding and creates none, so the candidate set strictly shrinks and the
    pass converges in at most as many rounds as it had candidates to begin
    with. Exhausting it means this module is not converging — a bug here, not
    a property of the customer's file — so it raises rather than handing back
    a half-cleaned file, which every layer above cannot tell from a finished
    one.
    """
    if language not in SUPPORTED:
        return after, []
    removed: list[Candidate] = []
    current = after
    budget = (
        len(_import_candidates(after, language))
        + len(_variable_candidates(after, language))
        + 1
    )
    for _ in range(budget):
        acted = False
        for candidate in _import_candidates(current, language) + _variable_candidates(
            current, language
        ):
            if candidate.kind == "import":
                before_count = _references_excluding_own_declaration(
                    before, candidate.name, language
                )
                current_count = _references_excluding_own_declaration(
                    current, candidate.name, language
                )
            else:
                if not _bound_name_occurs_once_in_own_span(
                    candidate, current, language
                ):
                    continue
                scopes = _matching_scope(before, current, candidate, language)
                if scopes is None:
                    continue
                before_span, current_span = scopes
                before_count = _references_in_scope(
                    before, candidate.name, language, *before_span
                )
                current_count = _references_in_scope(
                    current, candidate.name, language, *current_span
                )
            if before_count == 0:
                continue
            if current_count != 0:
                continue
            updated = _apply(current, candidate, language)
            if updated is None or updated == current:
                continue
            current = updated
            removed.append(candidate)
            acted = True
            break
        if not acted:
            break
    else:
        raise RuntimeError(
            "the stranded-binding pass did not reach a fixpoint in "
            f"{budget} rounds over one {language} file, which is one round "
            "per binding it could lose plus one. Each round removes a "
            "binding and adds none, so this cannot happen unless this pass "
            "is looping; refusing rather than returning a file that is only "
            "part-cleaned"
        )
    return current, removed
