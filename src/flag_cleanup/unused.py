"""Delete the bindings a flag fold stranded — imports, local variables, and
(C# and Java only) private methods.

A private method is the third kind, and the simplest to argue: once partial
types are refused, its reach IS the file, so the differential below needs no
scope matching — see :func:`_method_candidates`. The transform asks for it in
a separate, later call; see :func:`remove_stranded_bindings`.

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

One scope has no name and is matched anyway: the FILE. A TS/JS module's
top-level `const` is reachable from nowhere else, and pairing two programs
needs no name because each parse has exactly one — the ambiguity a name
resolves cannot arise. `_module_scopes` is that case, gated on the file
proving it is a module (`_top_level_bindings_are_private`), since a top-level
`const` in a plain script is shared with every other script and this pass can
only see one file.

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

import codecs
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache, partial
from typing import Iterator

import tree_sitter_c_sharp
import tree_sitter_dart
import tree_sitter_go
import tree_sitter_java
import tree_sitter_kotlin
import tree_sitter_php
import tree_sitter_python
import tree_sitter_ruby
import tree_sitter_swift
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from flag_cleanup import dart_namespace, inert_load

#: ``js`` maps to the TSX grammar deliberately, for the same reason
#: ``ts_syntax._parser`` does: the engine rewrote with it, and JSX in a ``.js``
#: file is ordinary in React codebases.
#:
#: ``dart`` is the GATE's grammar (tree-sitter-dart 0.1.0), not the engine's
#: 0.2.0 — the only one with Python bindings. Its shapes differ from the other
#: grammars here in two ways every Dart branch below exists for: a postfix
#: chain is FLATTENED into its parent (``LegacyFlow()`` is an ``identifier``
#: followed by a ``selector`` sibling, not one call node), and a function's
#: body is the SIBLING of its signature rather than its child.
#:
#: ``kt`` is the gate's wheel grammar too (``tree-sitter-kotlin``). Its one
#: shape every Kotlin branch below has to know: a ``$name`` template in an
#: ordinary string is NOT a node. ``"x $name y"`` arrives as the
#: ``string_content`` leaves ``'x '``, ``'$'`` and ``'name y'``, so no leaf
#: spells the name it reads (see ``_spelled_in``).
#:
#: ``python`` is the gate's grammar too, and the one whose IMPORTS this pass
#: never deletes: see ``_python_import_candidates``.
#:
#: ``swift`` is the gate's grammar too. Only its LOCALS are cleaned; no Swift
#: import is ever a candidate (see ``_import_candidates``).
#:
#: ``ruby`` is the gate's grammar too, and only its locals are cleaned: a
#: ``require`` runs the file it names, so none is ever a candidate.
#:
#: ``php`` is the PHP grammar with its inline-HTML text, the one every other
#: PHP pass here parses with.
_GRAMMARS = {
    "dart": tree_sitter_dart.language,
    "go": tree_sitter_go.language,
    "java": tree_sitter_java.language,
    "kt": tree_sitter_kotlin.language,
    "php": tree_sitter_php.language_php,
    "python": tree_sitter_python.language,
    "ruby": tree_sitter_ruby.language,
    "swift": tree_sitter_swift.language,
    "csharp": tree_sitter_c_sharp.language,
    "ts": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "js": tree_sitter_typescript.language_tsx,
}

#: The languages this pass acts on at all. Ruby is here because RuboCop's
#: DEFAULT `Lint/UselessAssignment` flags an unused local and exits 1 on it
#: (and `ruby -w` warns). PHP is here for tidiness, like Java: `php -l` reports neither an unused local nor
#: an unused `use`, and the linters that do (PHP-CS-Fixer's
#: `no_unused_imports`, Psalm's unused-variable check) are opt-in rule sets.
#: Swift is here because `swiftc` warns on an unused local
#: ("initialization of immutable value 'x' was never used"), and
#: `-warnings-as-errors` fails the build on it. Python is here because ruff's DEFAULT rule set
#: includes F401 (unused import) and F841 (unused local) and exits non-zero on
#: either, so a project that lints in CI cannot merge a pull request leaving
#: one. Dart is here because `dart analyze` reports both
#: an unused local and an unused import as warnings and exits non-zero on a
#: warning, which is what a Flutter CI runs. Kotlin is here for tidiness, like
#: Java and TypeScript, not for a build fix: measured, neither `kotlinc` 2.4
#: nor ktlint 1.8's standard rules report an unused local or import (the 1.x
#: compiler did warn on the local, which `allWarningsAsErrors` fails). What
#: ktlint DOES reject is what a careless removal adds, so the Kotlin branches
#: below avoid both: a `;` (`no-semi`) and two blank lines in a row
#: (`no-consecutive-blank-lines`).
SUPPORTED = frozenset(_GRAMMARS)

#: The nodes that hold an import declaration, per language. A name occurring
#: INSIDE one of these does not count as a reference to itself. Python has
#: two statements, and a third for ``from __future__``.
_IMPORT_ROOTS = {
    "dart": frozenset({"import_specification"}),
    "go": frozenset({"import_declaration"}),
    "java": frozenset({"import_declaration"}),
    "kt": frozenset({"import"}),
    "php": frozenset({"namespace_use_declaration"}),
    "python": frozenset({
        "import_statement", "import_from_statement", "future_import_statement",
    }),
    # A `require` is a call here, so this root matches nothing; no Ruby import
    # is ever a candidate.
    "ruby": frozenset(),
    "swift": frozenset({"import_declaration"}),
    "csharp": frozenset({"using_directive"}),
    "ts": frozenset({"import_statement"}),
    "tsx": frozenset({"import_statement"}),
    "js": frozenset({"import_statement"}),
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
    roots = _IMPORT_ROOTS[language]
    current = node.parent
    while current is not None:
        if current.type in roots:
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

    PHP compares without case: a class or function name resolves through a
    ``use`` whatever its case (``new flow()`` reads ``use App\\Flow``). A
    constant's does not, and counting it anyway only costs a missed tidy.
    """
    folded = language == "php"
    word = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE if folded else 0)
    wanted = name.casefold() if folded else name
    total = 0
    for node in _walk(_parse(source, language)):
        if node.child_count and not _read_by_words(node, language):
            continue
        text = node.text.decode("utf-8", "replace")
        if _read_by_words(node, language):
            total += len(word.findall(text))
        elif not _inside_import(node, language) and (
            _spelling(text, language).casefold() if folded else _spelling(text, language)
        ) == wanted:
            total += 1
    return total


def _spelling(text: str, language: str) -> str:
    """The name a leaf spells. The Kotlin and Swift grammars keep an
    identifier's backticks in its text, and `` `count` `` reads ``count``."""
    if language in {"kt", "swift"} and len(text) > 2 and text[0] == text[-1] == "`":
        return text[1:-1]
    return text


def _read_by_words(leaf: Node, language: str) -> bool:
    """Whether ``leaf`` is counted by the WORDS in it rather than by being
    the name: a comment, and a Kotlin or Python string's content.

    Kotlin's is a correctness requirement, not caution. A ``$name`` template
    in an ordinary string reads ``name``, and the grammar splits it across
    ``string_content`` leaves (``'$'``, then ``'name y'``) instead of giving
    it a node, so the only place that read is visible is the text. Counting
    every word of a string's content also counts a name that is merely
    prose, which costs a missed tidy and never a deleted binding.

    Python's is the same requirement for a different reason. A string can be
    a forward-reference annotation (``flows: "list[LegacyFlow]"``) that a type
    checker resolves, or an ``__all__`` entry that re-exports an import, and
    neither has a leaf spelling the name alone. A Python ``string_content``
    holding an escape is NOT a leaf (the ``\\n`` is its child), so the
    counters ask this before skipping a node with children; its escape
    children are then visited too, and spell no name.
    """
    return "comment" in leaf.type or (
        language in {"kt", "python"} and leaf.type == "string_content"
    )


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
    #: Every name a Dart import binds, ``name`` among them when the directive
    #: spells one (a plain import's ``name`` is only its URI, for the record).
    #: Empty for every other candidate, whose one binding is ``name``. When
    #: set, the differential runs over these instead: referenced before if
    #: any one was, stranded only if none is referenced after.
    names: tuple[str, ...] = ()
    #: What the span becomes instead of nothing — a TS/JS import statement
    #: reduced to the bare ``import "./m";`` that still loads its module (see
    #: ``_ts_import_candidates``). ``None`` means the span is deleted. On a
    #: record :func:`remove_stranded_bindings` returns it is also set for a
    #: ``const { client } = require("./m")`` discarded to ``require("./m");``
    #: (see ``_kept_require``), the same kept load in CommonJS.
    replacement: str | None = None
    #: The specifier a bare-import ``replacement`` keeps loading, so the pass
    #: can delete it instead when that module's load is provably inert (see
    #: :mod:`flag_cleanup.inert_load`). ``None`` without such a replacement.
    loads: str | None = None


def _candidate(
    name: str,
    node: Node,
    source_bytes: bytes,
    kind: str,
    replacement: str | None = None,
    loads: str | None = None,
) -> Candidate:
    return Candidate(
        name=name,
        start=node.start_byte,
        end=node.end_byte,
        kind=kind,
        text=source_bytes[node.start_byte:node.end_byte].decode("utf-8", "replace"),
        replacement=replacement,
        loads=loads if replacement else None,
    )


def go_import_name(path: str) -> str:
    """The name an unaliased Go import of ``path`` is GUESSED to bind: its
    last segment, with a ``/vN`` segment or a ``.vN`` suffix dropped.

    A guess, not a lookup: the package's own clause may say otherwise. Why a
    wrong guess is still safe here is in ``_go_import_candidates``. The
    keyless-wrapper proof relies on it from the other side, and only proves a
    call through an unaliased import when this guess IS the clause, so the
    import can go once the call folds.
    """
    segments = path.split("/")
    last = segments[-1]
    if len(segments) > 1 and _GO_VERSION_SEGMENT.match(last):
        last = segments[-2]
    return _GO_VERSION_SUFFIX.sub("", last)


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
            name = go_import_name(path_node.text.decode("utf-8", "replace").strip('"`'))
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


def _ts_import_candidates(
    root: Node, source_bytes: bytes, before: Node | None
) -> list[Candidate]:
    """Every binding an `import_statement` introduces, at specifier granularity.

    A side-effect import (``import './se'``) is excluded structurally: it has no
    ``import_clause`` child at all. That is a property of the node rather than a
    name on a list, and it is exactly right — such a statement binds nothing, so
    there is no name to have gone unused, and the module's load-time effect is
    the only reason it is written.

    ``import ff = require('./ff')`` has no ``import_clause`` either, and used
    to be passed over by that same test — which read as the side-effect rule
    working and was a different thing wearing its shape (#2825). It carries an
    ``import_require_clause`` instead, and that clause DOES bind a name, so the
    two are told apart by which clause type is present rather than by the
    absence of one. Every other spelling of an unused import is already
    removed here; this one landed a `no-unused-vars` error in the customer's
    repository for no reason but a node type nobody had looked at. Note the
    OTHER import-equals form, ``import ff = A.B.C``, is a wholly separate node
    type (``import_alias``, not ``import_statement``) and does not reach this
    loop at all — it is out of scope here rather than silently included.

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

    ``import type`` parses with the same clause shapes as a value import, and
    is found by the same loop.

    A candidate spanning the WHOLE statement is the one that can change what
    runs. A value import statement loads its module, and loading a module can
    do work (initialise a client, register a handler), so deleting the
    statement can stop that work for this file. TypeScript's import elision
    does NOT make the deletion harmless, which is easy to get backwards. It
    makes the deletion invisible next to the FOLDED file, whose binding is
    already unused and so already elided. Next to the customer's original
    file, whose binding was read and so kept, the load is gone either way. The
    one spelling that keeps it under every compiler and every setting is a
    bare ``import "./m";``, which binds nothing and which no compiler elides.
    So the candidate carries that as its ``replacement``. It is also the
    spelling ``const { client } = require("./m")`` already reduces to in
    CommonJS (``require("./m");``, see ``_apply``), so both module systems
    keep the load. ``_module_load`` decides which statements need it. A
    candidate spanning only part of a clause needs no replacement, because
    the statement survives and still loads the module.

    A replacement also records the specifier it ``loads``. When
    :func:`remove_stranded_bindings` is about to write the bare import, it
    first asks :mod:`flag_cleanup.inert_load` whether loading that module
    does anything, and deletes the statement if it provably does not. That
    question reads files, so it is asked only of a statement this pass is
    acting on, never of every import while candidates are being listed.
    """
    candidates = []
    for node in _walk(root):
        if node.type != "import_statement":
            continue
        clause = next(
            (
                c
                for c in node.children
                if c.type in {"import_clause", "import_require_clause"}
            ),
            None,
        )
        if clause is None:
            continue
        load = _module_load(node, source_bytes, before)
        loads = _specifier(node)
        if clause.type == "import_require_clause":
            # `import ff = require("./ff")`. The clause binds exactly one
            # name and the statement has no other reason to exist, so the
            # candidate spans the whole statement — the same "solo" outcome
            # a lone default import gets, reached without a count because
            # this clause shape admits no sibling to share with.
            #
            # No spelling keeps its load in every configuration, so a load
            # that has to be kept leaves the statement alone. `import "./ff"`
            # is an error in a CommonJS file under `verbatimModuleSyntax`,
            # the setting that makes this form necessary, and a bare
            # `require("./ff")` needs a `require` declared for the compiler,
            # which `import ff = require` does not.
            if load is not None:
                continue
            local = next(
                (c for c in clause.children if c.type == "identifier"), None
            )
            if local is not None:
                candidates.append(
                    _candidate(
                        local.text.decode("utf-8", "replace"),
                        node,
                        source_bytes,
                        "import",
                    )
                )
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
                        load if solo else None,
                        loads,
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
                        load if solo else None,
                        loads,
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
                            load if emptied is node else None,
                            loads,
                        )
                    )
    return candidates


def _specifier(statement: Node) -> str | None:
    """The module an import statement names, without its quotes."""
    source = statement.child_by_field_name("source")
    if source is None:
        return None
    return source.text.decode("utf-8", "replace")[1:-1]


def _is_type_only(statement: Node) -> bool:
    """``import type …``: the keyword is a direct child of the statement.

    An inline ``import { type A }`` is NOT this. Whether that statement loads
    its module depends on the compiler, and ``_loaded_before`` judges it.
    """
    return any(child.type == "type" for child in statement.children)


#: Nodes whose subtree is a TYPE, erased at compile time. A name read only
#: under one of these never made its import load anything. Measured against
#: the TS grammar: a plain type name is already a ``type_identifier`` leaf,
#: and these are the places a VALUE-shaped ``identifier`` leaf turns up in a
#: type (``ns.T``, ``typeof client``, ``implements ns.I``).
_TYPE_CONTEXTS = frozenset({
    "type_annotation", "type_arguments", "type_parameters", "type_query",
    "nested_type_identifier", "implements_clause", "extends_type_clause",
    "type_alias_declaration", "interface_declaration",
})

#: The leaf types a value read of an imported name takes. A ``type_identifier``
#: is never one, and neither is a ``property_identifier`` (``obj.client`` is
#: not the binding).
_VALUE_LEAVES = frozenset({"identifier", "shorthand_property_identifier"})


def _read_as_value(root: Node, name: str) -> bool:
    """Whether ``root`` reads ``name`` anywhere a compiler would emit it."""
    for node in _walk(root):
        if node.child_count or node.type not in _VALUE_LEAVES:
            continue
        if node.text.decode("utf-8", "replace") != name:
            continue
        if _has_ancestor(node, _TYPE_CONTEXTS | {"import_statement"}):
            continue
        return True
    return False


def _bound_names(statement: Node) -> list[str]:
    """The local names an ``import_clause`` binds, minus inline ``type`` ones."""
    names = []
    for clause in statement.children:
        if clause.type != "import_clause":
            continue
        for child in clause.children:
            if child.type == "identifier":
                names.append(child)
            elif child.type == "namespace_import":
                names.append(child.children[-1])
            elif child.type == "named_imports":
                for specifier in child.children:
                    if specifier.type != "import_specifier" or any(
                        c.type == "type" for c in specifier.children
                    ):
                        continue
                    alias = specifier.child_by_field_name("alias")
                    names.append(
                        alias
                        if alias is not None
                        else specifier.child_by_field_name("name")
                    )
    return [n.text.decode("utf-8", "replace") for n in names if n is not None]


def _loaded_before(before: Node | None, specifier: str) -> bool:
    """Whether the ORIGINAL file loaded ``specifier`` at runtime.

    It did when some import of it that is not ``import type`` either binds
    nothing (``import "./m"``) or binds a name the file read as a value. An
    import whose names were only ever read as types loaded nothing: the
    default compiler elides it, and ``verbatimModuleSyntax`` refuses to
    compile it at all. Reducing that one to a bare import would ADD a load
    the customer never had, which can expose a circular import that type-only
    use had kept dormant.

    ``None`` (no original to ask) answers yes, the direction that keeps a
    load. One shape answers no where one configuration loads:
    ``import { type A } from "./m"`` under ``verbatimModuleSyntax`` emits
    ``import {} from "./m"``. Its names are types by declaration, so it is
    judged like the default compiler judges it.
    """
    if before is None:
        return True
    for statement in _walk(before):
        if (
            statement.type != "import_statement"
            or _is_type_only(statement)
            or _specifier(statement) != specifier
        ):
            continue
        if not any(c.type == "import_clause" for c in statement.children):
            return True
        if any(_read_as_value(before, name) for name in _bound_names(statement)):
            return True
    return False


def _module_load(
    statement: Node, source_bytes: bytes, before: Node | None
) -> str | None:
    """The bare import that keeps ``statement``'s module load, or ``None``.

    ``None`` means deleting the statement drops no load the original file
    had, which holds in three cases:

    * the statement is ``import type``, which never loads anything;
    * the original file never loaded the module (``_loaded_before``): every
      name imported from it was read only as a type;
    * a sibling statement that does load (not type-only) imports the same
      module, so the module still loads without this one. Asked again in every
      round of the fixpoint, so when two stranded statements import one
      module, whichever is offered first is deleted, because the other still
      loads it, and the other then has no sibling left and becomes the bare
      import. Each edit leaves the module loaded.

    The bare import keeps everything after ``from``: the specifier as written
    (its quotes included) and any ``with { type: "json" }`` attribute, which a
    runtime requires on a JSON import whether or not it binds a name.
    """
    if _is_type_only(statement):
        return None
    specifier = _specifier(statement)
    source = statement.child_by_field_name("source")
    if specifier is None or source is None:
        # `import ff = require(...)` carries its specifier inside the clause,
        # and the caller leaves that form alone whenever this is not None.
        return ""
    if not _loaded_before(before, specifier):
        return None
    parent = statement.parent
    for sibling in parent.children if parent is not None else ():
        if (
            sibling.type == "import_statement"
            and sibling.id != statement.id
            and not _is_type_only(sibling)
            and _specifier(sibling) == specifier
        ):
            return None
    tail = source_bytes[source.start_byte:statement.end_byte].decode("utf-8", "replace")
    return f"import {tail}"


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


def _dart_import_candidates(
    root: Node,
    source_bytes: bytes,
    resolve: Callable[[str], dart_namespace.Namespace | None] | None = None,
) -> list[Candidate]:
    """Every Dart import whose bindings this pass can name.

    Every form needs the library itself, which ``resolve`` finds when it is
    in the checkout (see :mod:`flag_cleanup.dart_namespace`); one it cannot
    read — a ``dart:`` library, another package's — is never offered. Even
    a prefix needs it: an extension the library declares applies through
    ``as legacy`` too, and a call to its member spells neither the prefix nor
    any shown name, so every candidate's ``names`` carries the library's
    extension members beside what the directive binds.

    A plain ``import 'package:app/legacy.dart';`` binds every public name that
    library declares and spells none of them; its candidate carries that
    whole namespace. ``hide`` is the same import minus the hidden names. The
    other two forms bind names the directive spells:

    * a PREFIX (``as legacy``, ``deferred as legacy``), which binds exactly
      one name and is the import's only reason to exist, so the candidate is
      the whole directive. A ``show`` beside a prefix narrows what ``legacy.``
      reaches and binds nothing of its own;
    * a ``show`` list with no prefix, which binds each shown name. One name
      spans the whole directive; with siblings it spans only itself, and the
      comma goes with it in ``_apply``. More than one combinator is not
      offered: what ``show A hide B`` binds is a set difference, not a list.

    A library with a ``part`` directive offers nothing. Its part files share
    its import namespace, and they are files this pass never sees, so a name
    no longer spelled HERE can still be what a part file uses. A part file
    itself cannot carry imports, so it needs no check of its own.

    An import has no side effect in Dart to lose: a library's top-level
    initialisers run lazily, on first use, never at import time.
    """
    if any(child.type == "part_directive" for child in root.children):
        return []
    candidates = []
    for node in _walk(root):
        if node.type != "import_specification":
            continue
        directive = node.parent.parent if node.parent is not None else None
        if directive is None or directive.type != "import_or_export":
            continue
        uri = dart_namespace.uri_of(node)
        namespace = None if uri is None or resolve is None else resolve(uri)
        if namespace is None:
            continue
        members = tuple(sorted(namespace.members))
        children = node.children
        prefix = next(
            (
                children[index + 1]
                for index, child in enumerate(children[:-1])
                if child.type == "as" and children[index + 1].type == "identifier"
            ),
            None,
        )
        if prefix is not None:
            name = prefix.text.decode("utf-8", "replace")
            candidate = _candidate(name, directive, source_bytes, "import")
            candidates.append(replace(candidate, names=(name, *members)))
            continue
        combinators = [c for c in children if c.type == "combinator"]
        if all(c.children[0].type == "hide" for c in combinators):
            names = set(namespace.names)
            for combinator in combinators:
                names -= {
                    c.text.decode("utf-8", "replace")
                    for c in combinator.children
                    if c.type == "identifier"
                }
            names |= namespace.members
            if names:
                candidate = _candidate(uri, directive, source_bytes, "import")
                candidates.append(replace(candidate, names=tuple(sorted(names))))
            continue
        if len(combinators) != 1 or combinators[0].children[0].type != "show":
            continue
        shown = [c for c in combinators[0].children if c.type == "identifier"]
        for name_node in shown:
            name = name_node.text.decode("utf-8", "replace")
            candidate = _candidate(
                name,
                directive if len(shown) == 1 else name_node,
                source_bytes,
                "import",
            )
            candidates.append(replace(candidate, names=(name, *members)))
    return candidates


#: The functions Kotlin calls by CONVENTION, from an operator or a language
#: construct that spells no name: `a + b` calls `plus`, `var x by remember
#: { … }` calls `getValue`, `val (a, b) = p` calls `component1`, a `for` loop
#: calls `iterator`. Any of them can be an imported extension, and the import
#: then has a use this pass cannot count. `toString` is here too, for the
#: template that calls it.
_KOTLIN_CONVENTION_NAME = re.compile(
    r"component\d+|getValue|setValue|provideDelegate|invoke|iterator|next|"
    r"hasNext|contains|get|set|compareTo|equals|rangeTo|rangeUntil|plus|minus|"
    r"times|div|rem|mod|plusAssign|minusAssign|timesAssign|divAssign|"
    r"remAssign|modAssign|unaryPlus|unaryMinus|not|inc|dec|toString"
)


def _kotlin_import_candidates(root: Node, source_bytes: bytes) -> list[Candidate]:
    """Every Kotlin ``import`` that binds one name this file can spell.

    ``import a.b.C`` binds ``C``, and the candidate is the whole directive.
    An ALIASED import is never offered: ``import java.util.Date as JDate``
    also hides ``java.util.Date`` from the file's star imports, so deleting
    it can change what a bare ``Date`` resolves to, with no ``JDate`` left to
    show it. A star import binds names it never spells and is excluded
    structurally, by its ``*`` child. A name in backticks fails the
    identifier test and is left alone.

    Kotlin spells almost every use of an import, an extension call included
    (``"s".shout()`` — the import count, unlike a local's, keeps a member
    position). The exception is a function called by convention, which no
    use spells: see ``_KOTLIN_CONVENTION_NAME``. An import of one of those
    is never offered.

    Loading has no effect to lose: a JVM class initialises on first use,
    never at import.
    """
    candidates = []
    for node in _walk(root):
        if node.type != "import" or not node.is_named:
            continue
        if any(child.type == "*" for child in node.children):
            continue
        path = next(
            (c for c in node.children if c.type == "qualified_identifier"), None
        )
        if path is None or not path.named_children:
            continue
        original = path.named_children[-1].text.decode("utf-8", "replace")
        alias = next(
            (
                node.children[index + 1]
                for index, child in enumerate(node.children[:-1])
                if child.type == "as"
            ),
            None,
        )
        if alias is not None:
            continue
        if not original.isidentifier() or _KOTLIN_CONVENTION_NAME.fullmatch(original):
            continue
        candidates.append(_candidate(original, node, source_bytes, "import"))
    return candidates


#: What a stranded Python import is marked with. F401 is the code ruff and
#: flake8 share for an unused import.
_PYTHON_NOQA = "  # noqa: F401"

#: A noqa directive already on the line, in any spelling a linter reads.
_PYTHON_HAS_NOQA = re.compile(r"#\s*noqa", re.IGNORECASE)


def _python_line_can_end_in_a_comment(root: Node, line_end: int) -> bool:
    """Whether a comment appended at ``line_end`` is a comment.

    Not inside a token that runs on past the line. A ``\\`` continuation is
    one (tree-sitter's ``line_continuation`` spans the backslash AND the
    newline), and any character after it is a SyntaxError. A triple-quoted
    string opened later on the same line is another, and would take the mark
    into its value; the string is asked as a whole, because its content token
    can START at the newline and so never straddle it.
    """
    return not any(
        node.start_byte < line_end < node.end_byte
        for node in _walk(root)
        if not node.child_count or node.type == "string"
    )


def _python_import_candidates(root: Node, source_bytes: bytes) -> list[Candidate]:
    """Every name a Python import binds, each offered as a MARK on its
    statement rather than a deletion.

    Python never loses an import here, because no removal keeps what the
    statement loads. Loading a module runs it, and that can register a
    handler or configure a client. The name is no safer to drop than the
    statement: ``from app import legacy`` loads the SUBMODULE ``app.legacy``
    when ``legacy`` is one, and nothing in one file can tell a submodule from
    an attribute. ``import app`` would then lose the load, and
    ``import app.legacy`` raises when ``legacy`` is an attribute. So a
    stranded import keeps every byte and gains ``# noqa: F401``, which is what
    the lint failure this pass exists for asks for. It also keeps the name for
    a module that re-exports it, an ``__init__.py`` above all.

    The mark goes at the end of the statement's FIRST line, after any comment
    already there, and only where a comment can go (see
    ``_python_line_can_end_in_a_comment``). Measured, ruff and flake8 both read an import's noqa from
    that line and accept it after a comment, and neither honours one on a
    parenthesised import's closing line. The candidate spans from the
    statement's start to the end of that line, so the record the pull request
    shows is the whole line as it now reads.

    Not offered:

    * a statement whose first line already carries a noqa, which is the
      customer's call (and the state a statement this pass marked is left
      in, so the fixpoint never marks one twice);
    * a star import, which spells nothing it binds and has no ``name``;
    * ``from __future__``, a compiler directive and a statement type of its
      own.
    """
    candidates = []
    for node in _walk(root):
        if node.type == "import_statement":
            bound = [
                child for child in node.children
                if child.type in {"dotted_name", "aliased_import"}
            ]
        elif node.type == "import_from_statement":
            # A star import has no `name` child, so it offers nothing.
            bound = [
                node.child(index)
                for index in range(node.child_count)
                if node.field_name_for_child(index) == "name"
            ]
        else:
            continue
        line_end = source_bytes.find(b"\n", node.start_byte)
        line_end = len(source_bytes) if line_end == -1 else line_end
        if source_bytes[line_end - 1 : line_end] == b"\r":
            line_end -= 1
        line = source_bytes[node.start_byte:line_end].decode("utf-8", "replace")
        # A bare `\r` ends a line too, and then "up to the next `\n`" is not
        # the first line. Old Mac line endings are rare enough to decline.
        if "\r" in line or _PYTHON_HAS_NOQA.search(line) or not _python_line_can_end_in_a_comment(
            root, line_end
        ):
            continue
        for binding in bound:
            if binding.type == "aliased_import":
                name_node = binding.child_by_field_name("alias")
            elif node.type == "import_statement":
                # `import a.b` binds `a`.
                name_node = binding.named_children[0] if binding.named_children else None
            else:
                name_node = binding
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8", "replace")
            if not name.isidentifier():
                continue
            candidates.append(
                Candidate(
                    name=name,
                    start=node.start_byte,
                    end=line_end,
                    kind="import",
                    text=line,
                    replacement=line + _PYTHON_NOQA,
                )
            )
    return candidates


def _php_import_candidates(root: Node, source_bytes: bytes) -> list[Candidate]:
    """Every PHP ``use`` that binds one name, offered as a deletion.

    ``use App\\Legacy\\Flow;`` binds ``Flow``, and ``use App\\Ledger as
    Book;`` binds ``Book``; ``use function`` and ``use const`` bind the same
    way. A ``use`` runs nothing: it only tells the compiler how to spell a
    name, and no class is loaded until one is used. A use the file still
    makes is still spelled, in code, an attribute or a docblock's
    ``@param Flow``, and every one of those is counted.

    Not offered: a group (``use App\\{A, B};``) or a list (``use A, B;``),
    which would need one clause cut from the middle, and kept for the rest;
    and any use in a file with more than one ``namespace``.
    """
    if sum(1 for node in _walk(root) if node.type == "namespace_definition") > 1:
        # Each `namespace` block has its own uses, and the count here is the
        # file's, so a stranded `Foo` in one would take an unused `Foo` in
        # another with it.
        return []
    candidates = []
    for node in _walk(root):
        if node.type != "namespace_use_declaration":
            continue
        clauses = [c for c in node.named_children if c.type == "namespace_use_clause"]
        # A group's clauses sit inside its `namespace_use_group`, so a group
        # has none here.
        if len(clauses) != 1:
            continue
        clause = clauses[0]
        name_node = clause.child_by_field_name("alias")
        if name_node is None:
            path = next(
                (c for c in clause.named_children if c.type in {"qualified_name", "name"}),
                None,
            )
            if path is not None and path.type == "qualified_name":
                path = next(
                    (c for c in reversed(path.named_children) if c.type == "name"), None
                )
            name_node = path
        if name_node is None:
            continue
        name = name_node.text.decode("utf-8", "replace")
        if not name.isidentifier():
            continue
        candidates.append(_candidate(name, node, source_bytes, "import"))
    return candidates


def _import_candidates(
    source: str,
    language: str,
    before: Node | None = None,
    resolve: Callable[[str], dart_namespace.Namespace | None] | None = None,
) -> list[Candidate]:
    """Every import binding in ``source`` whose usage this pass can decide.

    ``before`` is the parsed original file, which the TS family asks whether a
    stranded statement's module was ever loaded (see ``_loaded_before``).
    ``resolve`` reads the library a Dart import names (see
    ``_dart_import_candidates``).
    """
    source_bytes = source.encode("utf-8")
    root = _parse(source, language)
    if language == "dart":
        return _dart_import_candidates(root, source_bytes, resolve)
    if language == "go":
        return _go_import_candidates(root, source_bytes)
    if language == "java":
        return _java_import_candidates(root, source_bytes)
    if language == "kt":
        return _kotlin_import_candidates(root, source_bytes)
    if language == "php":
        return _php_import_candidates(root, source_bytes)
    if language == "python":
        return _python_import_candidates(root, source_bytes)
    if language == "swift":
        # Never one. `swiftc` does not report an unused import, so leaving one
        # costs nothing, and spelling cannot decide one: an import also brings
        # in extensions, conformances and operators that no use spells.
        # `import Combine` is what makes `publisher.sink { … }` resolve.
        return []
    if language in {"ts", "tsx", "js"}:
        return _ts_import_candidates(root, source_bytes, before)
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

#: Dart's additions to the two sets above, kept per language because several
#: of these names mean something else, or something effectful, in a grammar
#: that never had them measured. ``arguments`` is the sharpest: here it is
#: reached only through a ``const_object_expression`` (a constant, evaluated
#: at compile time, so it runs nothing), because the call shapes that also
#: hold one — ``argument_part``, ``new_expression`` — are absent and stop the
#: walk first.
#:
#: A member access (``obj.field``, ``obj?.field``) and an index
#: (``items[0]``) are inert, as they already are for C# and TypeScript. A
#: Dart getter can run code, exactly as a C# property can; the pass accepts
#: the same trade here that it accepts there.
#:
#: An interpolated string is NOT inert: ``'$obj'`` calls ``obj.toString()``,
#: which is user code. ``template_substitution`` is absent for that reason,
#: so such a literal is judged effectful — and then left standing, since a
#: string is not a statement this pass will write (see
#: ``_dart_initializer_is_a_statement``).
_DART_INERT_NODES = frozenset({
    "list_literal", "set_or_map_literal", "const_object_expression",
    "null_literal", "symbol_literal",
})
_DART_INERT_WRAPPERS = frozenset({
    "selector", "unconditional_assignable_selector",
    "conditional_assignable_selector", "index_selector", "type_arguments",
    "arguments", "argument", "named_argument", "label",
})

#: Kotlin's additions, kept per language for the reason Dart's are. A boolean
#: and ``null`` are plain ``identifier`` leaves in this grammar, so they are
#: inert already. A member access (``navigation_expression``, which ``?.``
#: and ``::`` share) is inert as it is for C#, Dart and TypeScript, getters
#: and all. An index is NOT: ``items[0]`` calls an ``operator get``, which is
#: as likely to be the customer's as the standard library's.
#:
#: A string is inert only when it interpolates nothing, and the grammar's
#: shapes make that a TEXT test rather than a node test (see
#: ``_kotlin_string_is_inert``).
_KOTLIN_INERT_NODES = frozenset({
    "number_literal", "character_literal", "navigation_expression",
    "this_expression",
})
_KOTLIN_STRINGS = frozenset({"string_literal", "multiline_string_literal"})

#: Node names Kotlin SHARES with a grammar that listed them as inert, for a
#: shape that is not inert here: Go's ``index_expression`` is a slice read,
#: Kotlin's is a call to ``operator get``.
_KOTLIN_EFFECTFUL_NODES = frozenset({"index_expression"})

#: A ``$`` that starts a template: a name (any letter, not only ASCII), a
#: backticked name, or ``${``. The
#: ``$`` of ``"cost: $"`` or ``"$5"`` interpolates nothing.
_KOTLIN_TEMPLATE = re.compile(r"\$(?:[^\W\d]|[`{])")


def _kotlin_string_is_inert(node: Node) -> bool:
    """Whether a Kotlin string literal interpolates nothing.

    Asked of the TEXT, because the grammar gives a template a node in some
    strings and not in others: a raw string's ``$name`` is an
    ``interpolation``, an ordinary string's is two ``string_content`` leaves
    with no node of its own. A template calls ``toString()``, which is user
    code, so a string holding one is effectful.
    """
    return not _KOTLIN_TEMPLATE.search(node.text.decode("utf-8", "replace"))


#: What a Python local may hold and still be deleted: a literal, for Swift's
#: and PHP's reason. CPython counts references, and measured on 3.12 a local
#: holds its object until the function returns, so deleting one that holds
#: an object (a name, an attribute, a call's result) or keeping its call as
#: a bare statement runs the object's ``__del__`` at once instead:
#: ``tempfile.TemporaryDirectory()`` then removes its directory before the
#: code after it runs. Every named node, leaves included, must be one of
#: these, so a bare ``identifier`` fails. An f-string's ``interpolation``
#: calls ``__format__`` and is absent, so such a string fails too.
_PYTHON_INERT_NODES = frozenset({
    "integer", "float", "true", "false", "none", "ellipsis", "string",
    "string_start", "string_content", "string_end", "escape_sequence",
    "concatenated_string", "list", "tuple", "set", "dictionary", "pair",
    "parenthesized_expression",
})


#: What a Swift local may hold and still be deleted: a VALUE, never a
#: reference. Swift's set is the narrowest here, and not out of caution about
#: side effects alone. A `let` keeps its object alive to the end of its
#: scope, and measured on `swiftc` 6.0, at `-Onone` and `-O` alike, deleting
#: one moves the object's `deinit` earlier. A name, a member and a closure
#: (which captures) can each hold the last strong reference to something
#: whose `deinit` cancels a subscription or releases a lock, so none of them
#: is here. A literal is a standard-library value with no `deinit`.
#:
#: A string literal is judged by its children, so one holding an
#: ``interpolated_expression`` (``"\(x)"`` calls ``description``) is not
#: inert. A dictionary is absent: a duplicate key traps at run time.
_SWIFT_INERT_NODES = frozenset({
    "integer_literal", "real_literal", "boolean_literal", "hex_literal",
    "oct_literal", "bin_literal", "line_string_literal",
    "multi_line_string_literal", "raw_string_literal", "array_literal",
    "tuple_expression",
})

#: What a PHP local may hold and still be deleted: a literal, for Swift's
#: reason. PHP counts references, and measured on PHP 8.2 an object held by
#: a local is destroyed at the end of the function, but at once when the
#: assignment is gone, so its ``__destruct`` would move. Every named node in
#: the value must be one of these, leaves included: a bare ``name`` is a
#: constant, which throws when undefined, and a ``variable_name`` can hold an
#: object. An interpolated string holds a ``variable_name`` and so fails.
_PHP_INERT_NODES = frozenset({
    "integer", "float", "boolean", "null", "string", "string_content",
    "escape_sequence", "encapsed_string", "heredoc", "heredoc_start",
    "heredoc_body", "heredoc_end", "nowdoc", "nowdoc_body", "nowdoc_string",
    "array_creation_expression", "array_element_initializer",
})
_PHP_NUMBERS = frozenset({"integer", "float"})


#: What a stranded Ruby local may hold and still be deleted: a literal, and
#: every named node in it, leaves included, must be one of these. A bare
#: ``identifier`` is absent because it can be a method call (``c = compute``),
#: and a ``constant`` because a reference to one can autoload a file. A
#: heredoc is absent for a reason of SHAPE: its body is a sibling after the
#: statement, outside the assignment's span, and deleting the line would
#: leave the body behind.
_RUBY_INERT_NODES = frozenset({
    "integer", "float", "rational", "complex", "nil", "true", "false",
    "string", "string_content", "escape_sequence", "simple_symbol",
    "delimited_symbol", "array", "hash", "pair", "hash_key_symbol", "range",
})


#: Swift's literals, the only operands its ``-``, ``+`` and ``!`` are known to
#: apply the built-in operator to. Any type can overload a prefix operator,
#: and a CUSTOM one (``√4``) is the customer's function whatever it applies
#: to.
_SWIFT_LITERALS = frozenset({
    "integer_literal", "real_literal", "boolean_literal", "hex_literal",
    "oct_literal", "bin_literal",
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
    nodes = _initializer_nodes(source, candidate, language)
    return bool(nodes) and all(_subtree_is_inert(node, language) for node in nodes)


def _initializer_nodes(
    source: str, candidate: Candidate, language: str
) -> list[Node]:
    """The node, or the run of SIBLING nodes, that spans the initializer.

    One node everywhere except Dart, whose grammar flattens a postfix chain
    into its parent: ``repo.load(id)`` is an ``identifier`` followed by two
    ``selector`` siblings, each under the declaration's ``value`` field, and
    no single node spans the three. Asked of the span rather than of a field
    name, so it answers for both shapes: the outermost nodes lying wholly
    inside it, in order. Empty when they do not cover the span exactly.
    """
    start, end = candidate.initializer
    found: list[Node] = []
    stack = [_parse(source, language)]
    while stack:
        node = stack.pop()
        if node.end_byte <= start or node.start_byte >= end:
            continue
        if start <= node.start_byte and node.end_byte <= end:
            found.append(node)
            continue
        stack.extend(node.children)
    found.sort(key=lambda node: node.start_byte)
    if not found or found[0].start_byte != start or found[-1].end_byte != end:
        return []
    return found


def _subtree_is_inert(node: Node, language: str = "") -> bool:
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
    if language == "python":
        if not node.is_named:
            return True
        if node.type == "unary_operator":
            # `-x` calls `__neg__`; only a number takes the built-in one.
            operand = node.child_by_field_name("argument")
            return operand is not None and operand.type in {"integer", "float"}
        return node.type in _PYTHON_INERT_NODES and all(
            _subtree_is_inert(child, language) for child in node.children
        )
    if language == "ruby":
        if not node.is_named:
            return True
        if node.type == "unary":
            # Ruby reads `-3` as a negative literal; the grammar spells it as
            # an operator. Any other operand is a method call.
            operand = node.child_by_field_name("operand")
            return operand is not None and operand.type in {
                "integer", "float", "rational", "complex",
            }
        if node.type == "pair" and node.child_by_field_name("value") is None:
            # `{compute:}` reads the local `compute` or calls the method.
            return False
        return node.type in _RUBY_INERT_NODES and all(
            _subtree_is_inert(child, language) for child in node.children
        )
    if language == "php":
        if not node.is_named:
            return True
        if node.type == "unary_op_expression":
            # `~true` throws and `~1.5` warns, so only the operators that do
            # neither: a sign on a number, a `!` on a boolean.
            operand = node.child_by_field_name("argument")
            operator = node.children[0].type
            return operand is not None and (
                (operator in {"-", "+"} and operand.type in _PHP_NUMBERS)
                or (operator == "!" and operand.type == "boolean")
            )
        if node.type == "array_element_initializer" and any(
            child.type == "=>" for child in node.children
        ):
            # A float key warns that it loses precision, and only an integer
            # or a string is a key PHP keeps as written.
            key = node.named_children[0] if node.named_children else None
            if key is None or key.type not in {"integer", "string", "encapsed_string"}:
                return False
        return node.type in _PHP_INERT_NODES and all(
            _subtree_is_inert(child, language) for child in node.children
        )
    if language == "swift" and node.type == "simple_identifier":
        # A name is a token, but in Swift it can hold a reference, and the
        # `let` holding it is what keeps the object alive; see
        # `_SWIFT_INERT_NODES`.
        return False
    if node.child_count == 0:
        return True
    if language == "swift":
        if node.type == "prefix_expression":
            operation = node.child_by_field_name("operation")
            target = node.child_by_field_name("target")
            return (
                operation is not None
                and operation.text in {b"-", b"+", b"!"}
                and target is not None
                and target.type in _SWIFT_LITERALS
            )
        if node.type not in _SWIFT_INERT_NODES:
            return False
        return all(_subtree_is_inert(child, language) for child in node.named_children)
    if language == "kt":
        if node.type in _KOTLIN_STRINGS:
            return _kotlin_string_is_inert(node)
        if node.type in _KOTLIN_INERT_NODES:
            return all(
                _subtree_is_inert(child, language) for child in node.named_children
            )
        if node.type in _KOTLIN_EFFECTFUL_NODES:
            return False
    dart = language == "dart"
    if node.type in _INERT_WRAPPERS or (dart and node.type in _DART_INERT_WRAPPERS):
        return all(
            _subtree_is_inert(child, language) for child in node.named_children
        )
    if node.type in _UNARY_EXPRESSION_NODES:
        if language == "kt" and not _kotlin_unary_operand_is_literal(node):
            return False
        return _unary_is_inert(node, language)
    if node.type not in _INERT_NODES and not (
        dart and node.type in _DART_INERT_NODES
    ):
        return False
    return all(_subtree_is_inert(child, language) for child in node.named_children)


def _kotlin_unary_operand_is_literal(node: Node) -> bool:
    """Whether a Kotlin unary operator applies to a literal.

    ``-x``, ``+x`` and ``!x`` call ``unaryMinus``, ``unaryPlus`` and ``not``,
    which any type can define as an operator (kotlinx.html's ``+"text"``
    appends to the document). Only a number or a boolean literal is known to
    take the built-in one.
    """
    operand = node.child_by_field_name("argument")
    return operand is not None and (
        operand.type == "number_literal"
        or (operand.type == "identifier" and operand.text in {b"true", b"false"})
    )


def _unary_is_inert(node: Node, language: str = "") -> bool:
    """A unary expression is inert only when its operator is purely
    computational and its operand is inert.

    Structural, not a per-language name list: Go's ``<-`` (channel receive)
    and ``*`` (pointer dereference) share `unary_expression` with negation,
    and the only way to tell them apart is to read the operator token itself.
    go/java/ts name it with an ``operator`` field (and the operand with
    ``operand`` or, for ts, ``argument``); csharp's `prefix_unary_expression`
    names neither child, so its operator is the node's first child and its
    operand the second — always exactly two children. Dart's names neither child
    either, and wraps the operator token one level deeper
    (``prefix_operator > minus_operator``); the wrapper's text is the
    operator's, so the same comparison reads it. A Dart ``await`` is a
    ``unary_expression`` with ONE child and is rejected by the count.
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
    return _subtree_is_inert(operand, language)


#: ``(declaration node type, the node type its enclosing statement list uses)``
#: per language, plus the field names holding the bound name and the value.
_VARIABLE_DECLARATIONS = {
    "dart": ("local_variable_declaration", None, None),
    "go": ("short_var_declaration", "left", "right"),
    "java": ("local_variable_declaration", None, None),
    "kt": ("property_declaration", None, None),
    "php": ("expression_statement", None, None),
    "python": ("expression_statement", None, None),
    "ruby": ("assignment", None, None),
    "swift": ("property_declaration", None, None),
    "csharp": ("local_declaration_statement", None, None),
    "ts": ("lexical_declaration", None, None),
    "tsx": ("lexical_declaration", None, None),
    "js": ("lexical_declaration", None, None),
}

#: Node types that make a declaration LOCAL. A declaration not inside one of
#: these is a field, a parameter or a package-level binding, all of which are
#: referenced from files this pass never parses — with the one exception
#: ``_variable_candidates`` describes, a TS/JS module's top level, which is
#: reachable from no other file at all.
_FUNCTION_BODIES = frozenset({
    "block", "function_body", "statement_block", "function_declaration",
    "method_declaration", "function_definition", "method", "singleton_method",
})

#: The languages where a top-level declaration can be FILE-PRIVATE, plus the
#: node that is the file. Deliberately only the TS family: Go's package-level
#: `var` is visible to every other file in the package, and java/csharp have
#: no top-level variable declaration at all, so the widening below has nothing
#: to say about any of them.
#:
#: Today this is the statement of intent rather than the mechanism, and that
#: is worth knowing before relying on it. Two accidents already refuse each
#: non-TS language on their own — none of the three spells its root node
#: `program` except java, and none spells an import `import_statement` — so
#: removing this line would change no current answer. It stays because
#: `_top_level_bindings_are_private` should be right when READ, not right
#: because of two facts about other grammars that a bump could take away.
_TS_MODULE_LANGUAGES = frozenset({"ts", "tsx", "js"})
_TS_PROGRAM = "program"

#: What proves a TS/JS file is a module — i.e. that its top-level bindings are
#: private to it. `import`/`export` is TypeScript's own test and Node's: a file
#: carrying either is an ES module and its top-level `const` is reachable from
#: nowhere else. A `require(...)` call proves the other half, CommonJS, whose
#: module wrapper gives the same guarantee.
_MODULE_EVIDENCE = frozenset({"import_statement", "export_statement"})


#: What a deleted TS/JS import becomes when it was the file's last ``import`` or
#: ``export``. Without one the file is a script, not a module: top-level
#: ``await`` stops compiling (TS1375), and a top-level ``const`` becomes a
#: global that can collide with another script's or with ``lib.dom``'s
#: (TS2451). This keeps the file a module and, unlike a bare import, loads
#: nothing, so it is right whether the deleted import loaded its module, loaded
#: an inert one, or (``import type``) never loaded anything.
MODULE_MARKER = "export {};"


def _deletes_last_module_marker(source: str, candidate: Candidate, language: str) -> bool:
    """Whether deleting ``candidate`` leaves ``source`` with no ``import`` or
    ``export`` statement, having had one.

    Only statements DIRECTLY in the program count: an ``export`` inside a
    ``declare module "x" { … }`` block declares that ambient module and does not
    make this file one. A candidate that is one specifier of a statement covers
    less than the statement, so the statement still counts as remaining.
    """
    if language not in _TS_MODULE_LANGUAGES:
        return False
    covered = False
    for node in _parse(source, language).children:
        if node.type not in _MODULE_EVIDENCE:
            continue
        if candidate.start <= node.start_byte and node.end_byte <= candidate.end:
            covered = True
        else:
            return False
    return covered


def _top_level_bindings_are_private(root: Node, language: str) -> bool:
    """Whether a declaration sitting directly in ``root`` is file-private.

    This is the one thing that makes a module-level candidate decidable, and
    it is not a formality. A top-level ``const`` in a CLASSIC script is not
    module-scoped at all — it lands in the shared script-scope every other
    ``<script>`` on the page reads, so a differential over this one file
    proves nothing about it and neutralising it breaks a caller this pass
    never saw. A module's top-level ``const`` is unreachable from outside by
    construction, so the same differential is complete.

    Asked of the file rather than of its extension, because this pass is
    handed source and a language, never a path — and it is the same question
    TypeScript and Node each answer from the file's own contents.

    Exporting is NOT tested here, and needs no test: an exported declaration
    is a child of an ``export_statement`` rather than of the program, so it
    never reaches the widened gate, and a separate ``export { client }``
    re-export is a leaf spelling the name, which the reference count already
    sees as the read it is.
    """
    if language not in _TS_MODULE_LANGUAGES:
        return False
    for node in _walk(root):
        if node.type in _MODULE_EVIDENCE:
            return True
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type == "identifier":
                if callee.text == b"require":
                    return True
    return False


def _variable_candidates(source: str, language: str) -> list[Candidate]:
    """Every single-name local declaration, with its initializer span.

    "Local" means inside a function body — with one widening, and the reason
    it is safe is not the reason the guard was written. ``_FUNCTION_BODIES``
    stands in for "not reachable from another file", and for a field, a
    parameter or a Go package-level binding that is exactly right. It is
    wrong for a TS/JS MODULE's top-level ``const``, which is reachable from
    nowhere else at all, and the cost of getting it wrong there was a
    `no-unused-vars` error shipped to the customer in eight of the ten
    findings #2825 enumerates. So a declaration sitting directly in the
    program of a file whose top-level bindings are private (see
    ``_top_level_bindings_are_private``) is a candidate too.

    Directly in the PROGRAM, not merely outside a function: a declaration in
    a top-level ``if`` block is scoped to that block, and claiming the file
    for it would be describing a wider scope than it has. It stays out.
    """
    declaration_type, name_field, value_field = _VARIABLE_DECLARATIONS[language]
    source_bytes = source.encode("utf-8")
    root = _parse(source, language)
    top_level_is_private = _top_level_bindings_are_private(root, language)
    candidates = []
    for node in _walk(root):
        if node.type != declaration_type:
            if language == "go" and node.type == "var_declaration" and _has_ancestor(node, _FUNCTION_BODIES):
                candidates += _go_var_candidates(node, source_bytes)
            continue
        if not _has_ancestor(node, _FUNCTION_BODIES) and not (
            top_level_is_private
            and node.parent is not None
            and node.parent.type == _TS_PROGRAM
        ):
            continue
        if language == "dart":
            pair = _dart_declared_name_and_value(node)
        elif language == "kt":
            pair = _kotlin_declared_name_and_value(node)
        elif language == "python":
            pair = _python_declared_name_and_value(node)
        elif language == "swift":
            pair = _swift_declared_name_and_value(node)
        elif language == "php":
            pair = _php_declared_name_and_value(node)
        elif language == "ruby":
            pair = _ruby_declared_name_and_value(node)
        else:
            if language in _TS_MODULE_LANGUAGES:
                candidates += _require_pattern_candidates(node, source_bytes)
            pair = _declared_name_and_value(node, name_field, value_field)
            if pair is not None:
                name_node, value_node = pair
                pair = name_node, (
                    (value_node.start_byte, value_node.end_byte)
                    if value_node is not None
                    else None
                )
        if pair is None:
            continue
        name_node, initializer = pair
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
                initializer=initializer,
                binder=(name_node.start_byte, name_node.end_byte),
            )
        )
    return candidates


def _go_var_candidates(declaration: Node, data: bytes) -> list[Candidate]:
    """The candidates a Go ``var`` declaration inside a function offers.

    ``var x T`` is a local exactly as ``x := v`` is, and Go rejects it unused
    the same way, so leaving it to the ``:=`` path alone shipped
    ``declared and not used`` whenever a fold removed the last read of one.
    Three shapes, each decided by what the deletion or discard leaves:

    * **A spec binding one name.** Its span is the whole declaration when the
      spec is the declaration's only one, and the spec alone inside a
      ``var ( … )`` holding others. ``_apply`` then treats it as it treats
      ``x := v``: deleted when its value is inert or absent, rewritten to
      ``_ = v`` when it is not. That discard is a statement on its own and a
      spec inside a group (``var ( _ = load() )``), so both spans take it.
      A grouped spec sharing a line with anything (``var (a = 1; b = 2)``)
      is not offered: deleting it would leave the ``;`` behind.
    * **A spec binding several names with no value** (``var v, w T``). Each
      name is a candidate on its own, spanning its token: no value belongs to
      it, so dropping it and its comma leaves the spec meaning what it meant
      for the others. The last name left is the first shape again.
    * **Several names with values** (``var v, w = a, b``) are never offered.
      Each name owns a value, possibly one shared call, and removing one is
      two edits this pass does not make. The keyless-wrapper proof refuses a
      receiver declared that way rather than strand it.
    """
    specs = [c for c in declaration.named_children if c.type == "var_spec"]
    for group in (c for c in declaration.named_children if c.type == "var_spec_list"):
        specs += [c for c in group.named_children if c.type == "var_spec"]
    found: list[Candidate] = []
    whole = data[declaration.start_byte : declaration.end_byte].decode("utf-8", "replace")
    for spec in specs:
        names = spec.children_by_field_name("name")
        if not names or any(n.type != "identifier" or n.text in (b"_", b"") for n in names):
            continue
        value = spec.child_by_field_name("value")
        values = [] if value is None else [c for c in value.named_children if c.type != "comment"]
        if len(names) == 1:
            if value is not None and len(values) != 1:
                continue
            node = declaration if len(specs) == 1 else spec
            if node is spec and not _alone_on_its_lines(data, spec):
                # `var (a = 1; b = 2)`: deleting `a = 1` leaves `(; b = 2)`.
                continue
            found.append(
                Candidate(
                    name=names[0].text.decode("utf-8", "replace"),
                    start=node.start_byte,
                    end=node.end_byte,
                    kind="variable",
                    text=data[node.start_byte : node.end_byte].decode("utf-8", "replace"),
                    initializer=None if not values else (values[0].start_byte, values[0].end_byte),
                    binder=(names[0].start_byte, names[0].end_byte),
                )
            )
        elif value is None:
            found += [
                Candidate(
                    name=name.text.decode("utf-8", "replace"),
                    start=name.start_byte,
                    end=name.end_byte,
                    kind="variable",
                    text=whole if len(specs) == 1 else data[spec.start_byte : spec.end_byte].decode("utf-8", "replace"),
                    binder=(name.start_byte, name.end_byte),
                )
                for name in names
            ]
    return found


def _alone_on_its_lines(data: bytes, node: Node) -> bool:
    """Whether nothing but whitespace shares ``node``'s first and last lines."""
    line_start = data.rfind(b"\n", 0, node.start_byte) + 1
    line_end = data.find(b"\n", node.end_byte)
    line_end = len(data) if line_end == -1 else line_end
    return not data[line_start : node.start_byte].strip() and not data[node.end_byte : line_end].strip()


def _dart_declared_name_and_value(
    node: Node,
) -> tuple[Node, tuple[int, int] | None] | None:
    """The single bound name of a Dart ``local_variable_declaration`` and the
    byte span of its initializer, or ``None`` if it binds more than one name.

    The initializer is a SPAN, not a node, because the grammar flattens a
    postfix chain: ``LegacyFlow()`` arrives as an ``identifier`` and a
    ``selector`` that are both children of the declaration under the
    ``value`` field, and nothing spans the pair. From the first ``value``
    child to the last is the expression.

    Two shapes bind more than one name and are declined structurally: a
    second declarator (``final a = 1, b = 2;`` puts ``b`` in an
    ``initialized_identifier`` beside the first) and a pattern
    (``final (a, b) = pair();`` is a ``pattern_variable_declaration``, a
    different node from the ``initialized_variable_definition`` read here).
    """
    definitions = [
        child for child in node.named_children
        if child.type == "initialized_variable_definition"
    ]
    if len(definitions) != 1 or len(node.named_children) != 1:
        return None
    definition = definitions[0]
    if any(child.type == "late" for child in definition.children):
        # Its initializer runs on first READ, and a stranded one is never
        # read: deleting the declaration is right but this pass's discard
        # would make the call unconditional, and an inert initializer is
        # judged as such anyway. Neither half is worth a third rule here.
        return None
    if any(c.type == "initialized_identifier" for c in definition.named_children):
        return None
    name_node = definition.child_by_field_name("name")
    if name_node is None:
        return None
    values = [
        definition.child(index)
        for index in range(definition.child_count)
        if definition.field_name_for_child(index) == "value"
    ]
    if not values:
        return name_node, None
    typed = any(
        child.end_byte <= name_node.start_byte
        and child.type not in _DART_UNTYPED_MODIFIERS
        for child in definition.named_children
    )
    if typed and any(
        node.type == "identifier" for value in values for node in _walk(value)
    ):
        # `final int id = json['id'];` casts from `dynamic` and throws on bad
        # data; deleting the declaration or keeping only its initializer
        # both lose that. A literal cannot be `dynamic`, so only an
        # initializer that names something is declined.
        return None
    return name_node, (values[0].start_byte, values[-1].end_byte)


def _kotlin_declared_name_and_value(
    node: Node,
) -> tuple[Node, tuple[int, int] | None] | None:
    """The single bound name of a Kotlin local ``val``/``var`` and the byte
    span of its initializer, or ``None`` when it is not one this pass can
    judge.

    ``property_declaration`` is also a class's PROPERTY, and a local class or
    an object expression inside a function puts one inside a function body.
    A member is read as ``local.x``, which the local count deliberately does
    not see, so only a declaration that is a statement of a ``block`` is a
    local here.

    Declined outright:

    * a destructuring declaration (``val (a, b) = p``), which binds more
      than one name and calls ``componentN`` doing it;
    * a delegated one (``val x by lazy { … }``): ``provideDelegate`` can run
      code at the declaration, and the initializer is not an expression;
    * a TYPED local whose initializer names anything. ``val x: Foo =
      javaApi()`` checks a platform type for null and throws on it, and
      deleting the line or keeping only the call both lose the throw. A
      literal has no platform type, so ``val x: Long = 3`` is still judged.
      A boolean and ``null`` are ``identifier`` leaves in this grammar and
      are told apart by their text.
    """
    if node.parent is None or node.parent.type != "block":
        return None
    if any(
        child.type in {"multi_variable_declaration", "property_delegate"}
        for child in node.children
    ):
        return None
    declarations = [c for c in node.named_children if c.type == "variable_declaration"]
    if len(declarations) != 1:
        return None
    declaration = declarations[0]
    name_node = declaration.named_children[0] if declaration.named_children else None
    if name_node is None or name_node.type != "identifier":
        return None
    value = None
    for index, child in enumerate(node.children[:-1]):
        if child.type == "=":
            value = node.children[index + 1]
            break
    if value is None:
        return name_node, None
    typed = len(declaration.named_children) > 1
    if typed and any(
        leaf.type == "identifier" and leaf.text not in _KOTLIN_LITERAL_WORDS
        for leaf in _walk(value)
    ):
        return None
    return name_node, (value.start_byte, value.end_byte)


#: The Kotlin literals this grammar parses as an ``identifier``.
_KOTLIN_LITERAL_WORDS = frozenset({b"true", b"false", b"null"})

#: Names whose call reads a function's locals without spelling any of them:
#: ``render(request, "t.html", locals())`` is a Django idiom. A scope that
#: spells one offers no local at all. ``eval``/``exec`` of a literal string is
#: already counted by its words; this covers the computed one too.
_PYTHON_NAMESPACE_READERS = frozenset({
    b"locals", b"vars", b"dir", b"eval", b"exec", b"_getframe",
    b"currentframe", b"f_locals",
})

#: The Python nodes that start a new binding scope, or (a class body) a
#: namespace whose names are attributes rather than locals.
_PYTHON_SCOPES = frozenset({"function_definition", "class_definition", "lambda"})


def _python_declared_name_and_value(
    node: Node,
) -> tuple[Node, tuple[int, int] | None] | None:
    """The one name a Python assignment statement binds in a function, and
    the span of the value it assigns, or ``None`` when it is not one this pass
    can judge.

    Python has no declaration: an ``expression_statement`` holding an
    ``assignment`` to a bare name is the binding. Only one inside a FUNCTION
    is local. A class body's assignment is an attribute anyone can read, and
    a module's is a global other files import, so the nearest enclosing scope
    must be a ``def``. A ``global``/``nonlocal`` declaration makes the name
    somebody else's, and needs no check of its own: it spells the name, so
    the scope's count never reaches zero.

    Declined: any local of a function that spells one of
    ``_PYTHON_NAMESPACE_READERS``, a target that is not a bare name (``a, b = …``, ``self.x =
    …``, ``items[0] = …``), an annotation with no value (``x: int``),
    which binds nothing at run time, and a name assigned more than once in
    its function. Each assignment's own name is blanked, so ``lock = None``
    releasing what ``lock = Lock()`` held reads as stranded, and deleting it
    keeps the lock to the end of the function. A chain (``a = b = make()``)
    is offered and then left: its value is the inner assignment, which is
    not a literal.
    """
    scope = node.parent
    while scope is not None and scope.type not in _PYTHON_SCOPES:
        scope = scope.parent
    if scope is None or scope.type != "function_definition":
        return None
    if any(
        leaf.type == "identifier" and leaf.text in _PYTHON_NAMESPACE_READERS
        for leaf in _walk(scope)
    ):
        return None
    statements = [c for c in node.named_children if c.type != "comment"]
    if len(statements) != 1 or statements[0].type != "assignment":
        return None
    assignment = statements[0]
    name_node = assignment.child_by_field_name("left")
    value = assignment.child_by_field_name("right")
    if name_node is None or name_node.type != "identifier" or value is None:
        return None
    if sum(
        1
        for other in _walk(scope)
        if other.type == "assignment"
        and (target := other.child_by_field_name("left")) is not None
        and target.type == "identifier"
        and target.text == name_node.text
    ) > 1:
        return None
    return name_node, (value.start_byte, value.end_byte)


#: The children a Swift local may have and still be judged here: ``let``/
#: ``var``, its name, a type and a value. Anything else is declined, and each
#: for a reason: an ``attribute`` is a property wrapper, whose ``init`` runs
#: code; ``lazy`` runs the initializer on first read, never at the
#: declaration; ``weak``/``unowned`` arrive as ``modifiers``; and a
#: ``willset_didset_block`` or ``computed_property`` is code of its own.
#: ``async let`` starts a child task, and its ``async`` is an ANONYMOUS
#: child, so it is checked for apart from these.
_SWIFT_LOCAL_PARTS = frozenset({"value_binding_pattern", "pattern", "type_annotation"})


def _swift_declared_name_and_value(
    node: Node,
) -> tuple[Node, tuple[int, int] | None] | None:
    """The one name a Swift local ``let``/``var`` binds and the span of its
    value, or ``None`` when it is not one this pass can judge.

    ``property_declaration`` is a type's stored property as well, and a
    file's top-level ``let`` in a ``main.swift`` is a global, so only a
    declaration that is a statement (its parent is ``statements``) is a local.

    Declined: a second name (``let a = 1, b = 2``) or a pattern (``let (a,
    b) = pair()``), any child outside ``_SWIFT_LOCAL_PARTS``, and a TYPED
    local with a value. A literal is only a standard-library value when
    nothing names its type: ``let x: Money = 3`` runs the customer's
    ``init(integerLiteral:)``.
    """
    if node.parent is None or node.parent.type != "statements":
        return None
    if any(
        (child.is_named and child.type not in _SWIFT_LOCAL_PARTS
         and node.field_name_for_child(index) != "value")
        or child.type == "async"
        for index, child in enumerate(node.children)
    ):
        return None
    names = [
        node.child(index)
        for index in range(node.child_count)
        if node.field_name_for_child(index) == "name"
    ]
    if len(names) != 1:
        return None
    # A pattern (`(a, b)`) has no `bound_identifier`; only a plain name does.
    bound = names[0].child_by_field_name("bound_identifier")
    if bound is None:
        return None
    value = node.child_by_field_name("value")
    if value is None:
        return bound, None
    if any(child.type == "type_annotation" for child in node.children):
        return None
    return bound, (value.start_byte, value.end_byte)


#: The PHP nodes that read every local of their scope without spelling one:
#: an included file runs in the including function's scope, and ``$$name`` is
#: any local at all. A scope holding one offers no local.
_PHP_NAMESPACE_READERS = frozenset({
    "include_expression", "include_once_expression", "require_expression",
    "require_once_expression", "dynamic_variable_name",
})

#: The PHP functions that do the same by name: ``compact('total')`` reads
#: ``$total`` by a string. Matched as ANY ``name`` leaf, so a qualified
#: ``\\get_defined_vars()`` counts, and PHP refuses to call any of them
#: dynamically, so a name is the only way to reach one.
_PHP_NAMESPACE_FUNCTIONS = frozenset({
    b"compact", b"extract", b"get_defined_vars", b"eval", b"parse_str",
})

#: Variables every scope shares. Writing one is never local.
_PHP_SUPERGLOBALS = frozenset({
    "GLOBALS", "_SERVER", "_GET", "_POST", "_FILES", "_COOKIE", "_SESSION",
    "_REQUEST", "_ENV", "this",
})

#: The PHP nodes that start a new variable scope.
_PHP_SCOPES = frozenset({
    "function_definition", "method_declaration", "anonymous_function",
    "arrow_function",
})


def _php_reader_aliases(node: Node) -> frozenset[bytes]:
    """The local names a ``use function`` gives one of
    ``_PHP_NAMESPACE_FUNCTIONS`` in the file holding ``node``
    (``use function get_defined_vars as vars;``)."""
    root = node
    while root.parent is not None:
        root = root.parent
    aliases = set()
    for clause in _walk(root):
        if clause.type != "namespace_use_clause":
            continue
        alias = clause.child_by_field_name("alias")
        target = next(
            (c for c in clause.named_children if c.type in {"qualified_name", "name"}),
            None,
        )
        if alias is None or target is None:
            continue
        last = target.text.rsplit(b"\\", 1)[-1].lower()
        if last in _PHP_NAMESPACE_FUNCTIONS:
            aliases.add(alias.text.lower())
    return frozenset(aliases)


def _php_assigned_name(node: Node) -> str | None:
    """The variable a plain ``$name = value;`` statement writes, or ``None``."""
    if node.type != "expression_statement":
        return None
    statements = [c for c in node.named_children if "comment" not in c.type]
    if len(statements) != 1 or statements[0].type != "assignment_expression":
        return None
    left = statements[0].child_by_field_name("left")
    if left is None or left.type != "variable_name":
        return None
    name_node = next((c for c in left.named_children if c.type == "name"), None)
    return None if name_node is None else name_node.text.decode("utf-8", "replace")


def _php_declared_name_and_value(
    node: Node,
) -> tuple[Node, tuple[int, int] | None] | None:
    """The one variable a PHP assignment statement writes in a function, and
    the span of the value, or ``None`` when it is not one this pass can judge.

    PHP has no declaration: ``$total = 3;`` is the binding, and it may also
    be a REASSIGNMENT. Every way to bind the name other than a plain
    assignment spells it (a parameter, ``global``, ``static``, ``foreach …
    as``, a reference), and a spelling keeps the count above zero. A second
    plain assignment does not, since each one's own name is blanked, so a
    variable assigned more than once in its scope is declined: the one left
    standing may be what releases an object the other held.

    Only a function's or a method's own scope: a closure or an arrow
    function has no name to pair, and a file's top-level variables are
    globals that an including file shares. Declined: any local of a scope
    holding one of ``_PHP_NAMESPACE_READERS`` or calling one of
    ``_PHP_NAMESPACE_FUNCTIONS``, a superglobal, and anything but a plain
    ``$name = value`` (``$a = &$b``, ``$a += 1`` and ``$a[] = 1`` are other
    nodes or another left side).
    """
    scope = node.parent
    while scope is not None and scope.type not in _PHP_SCOPES:
        scope = scope.parent
    if scope is None:
        return None
    readers = _PHP_NAMESPACE_FUNCTIONS | _php_reader_aliases(node)
    if any(
        leaf.type in _PHP_NAMESPACE_READERS
        or (leaf.type == "name" and leaf.text.lower() in readers)
        for leaf in _walk(scope)
    ):
        return None
    name = _php_assigned_name(node)
    if name is None or name in _PHP_SUPERGLOBALS:
        return None
    if sum(1 for other in _walk(scope) if _php_assigned_name(other) == name) > 1:
        # `$lock = null;` releasing what `$lock = new Lock();` held: deleting
        # it would move that object's `__destruct` to the end of the function.
        return None
    assignment = next(c for c in node.named_children if "comment" not in c.type)
    left = assignment.child_by_field_name("left")
    value = assignment.child_by_field_name("right")
    if value is None:
        return None
    name_node = next(c for c in left.named_children if c.type == "name")
    return name_node, (value.start_byte, value.end_byte)


#: The Ruby nodes that start a new local scope. A block and a lambda see the
#: locals around them, but bind their own.
_RUBY_SCOPES = frozenset({
    "method", "singleton_method", "block", "do_block", "lambda", "class",
    "module", "singleton_class",
})

#: Names whose use reads a method's locals without spelling them: ERB's
#: ``template.result(binding)`` is the everyday one. A scope that spells one
#: offers no local at all.
_RUBY_NAMESPACE_READERS = frozenset({
    b"binding", b"local_variables", b"eval", b"instance_eval", b"class_eval",
    b"module_eval", b"instance_exec", b"class_exec", b"module_exec",
})


def _ruby_declared_name_and_value(
    node: Node,
) -> tuple[Node, tuple[int, int] | None] | None:
    """The local a Ruby assignment statement writes in a method, and the span
    of its value, or ``None`` when it is not one this pass can judge.

    Ruby has no declaration: the first ``legacy = …`` makes ``legacy`` a
    local, and a later one reassigns it. Every other way the name comes to
    exist spells it (a parameter, a block parameter, ``legacy ||= …``, a
    multiple assignment), and a spelling keeps the count above zero.

    Declined: every local of a scope that spells one of
    ``_RUBY_NAMESPACE_READERS``. A scope that is not a ``def`` is declined
    when it is paired (``_ruby_scope_key``). A left side that is not a plain
    name (``@x``, ``$x``, ``obj.x``, ``a, b``) fails the name test every
    candidate gets, and a constant cannot be assigned in a method.

    An assignment inside an expression (``log(a = 3)``, ``a = 3 if ok``) is
    offered like any other. Its value is its right side, so keeping the call
    keeps the expression's value, and a deletion there always shares its line
    with the rest of the expression, which ``_ruby_deletable_lines`` declines.
    """
    scope = node.parent
    while scope is not None and scope.type not in _RUBY_SCOPES:
        scope = scope.parent
    if scope is None:
        return None
    if any(
        leaf.text.lstrip(b":") in _RUBY_NAMESPACE_READERS
        for leaf in _walk(scope)
        if not leaf.child_count
    ):
        # Spelled as a name or as a symbol: `send(:binding)` and
        # `method(:eval)` reach the same thing.
        return None
    left = node.child_by_field_name("left")
    value = node.child_by_field_name("right")
    if left is None or value is None:
        return None
    return left, (value.start_byte, value.end_byte)


#: The words before a Dart local's name that are NOT a type annotation.
_DART_UNTYPED_MODIFIERS = frozenset({
    "final_builtin", "const_builtin", "inferred_type", "late",
})


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
    # Two distinct nodes from here on, and conflating them is a live trap.
    # `_sole_bound_name` can descend INTO the bound position — for
    # `const { client } = …` the name is a child of the pattern, not a child
    # of the declarator — so the C# fallback below, which identifies the
    # initializer as "the named child that is not the name", has to keep
    # comparing against the declarator's own child. Reassigning over
    # `name_node` would leave the pattern itself looking like an initializer.
    bound_name = _sole_bound_name(name_node)
    if bound_name is None:
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
    return bound_name, value_node


#: The TS destructuring patterns whose bound position is a whole pattern
#: rather than a name. Both are containers, and their punctuation is
#: anonymous, so "how many names does this bind" is a count of named children.
_DESTRUCTURING_PATTERNS = frozenset({"object_pattern", "array_pattern"})


def _sole_bound_name(name_node: Node) -> Node | None:
    """``name_node`` itself, or the ONE name a destructuring pattern binds.

    ``const { client } = require('./ff')`` binds exactly one name, and the
    declaration's own span is the whole statement — so the "referenced
    before, referenced nowhere after" differential answers for it exactly as
    it does for ``const client = ...``. That was not true of a pattern in
    general, which is why the caller declines one: taking ``{ a, b }``'s
    source text as the binding name runs the differential against a string
    nothing can reference, and the declaration goes with both names still in
    use below it.

    So the restriction is a COUNT, and it is the same "solo or shared" count
    ``_ts_import_candidates`` makes over an ``import_clause``: a span may
    only be removed for a binding when that binding is the span's only
    reason to exist. More than one name, and there is a sibling to strand.

    Only the count is decided here. The caller's existing "a token, and it
    spells like a name" guard is what rejects the shapes that bind one name
    through a node that is not one — ``{ a: b }``'s ``pair_pattern`` and
    ``{ ...r }``'s ``rest_pattern`` both have children, so both fall out
    there rather than needing a list of their own.
    """
    if name_node.type not in _DESTRUCTURING_PATTERNS:
        return name_node
    bound = name_node.named_children
    return bound[0] if len(bound) == 1 else None


#: The node a shorthand name in an object pattern is (``a`` in ``{ a, b }``).
_PATTERN_NAME = "shorthand_property_identifier_pattern"


def _is_require(node: Node | None) -> bool:
    """``require("./m")``: the callee is the identifier, one string argument."""
    if node is None or node.type != "call_expression":
        return False
    callee = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    specs = [] if arguments is None else [c for c in arguments.named_children if c.type != "comment"]
    return (
        callee is not None
        and callee.type == "identifier"
        and callee.text == b"require"
        and len(specs) == 1
        and specs[0].type == "string"
    )


def _require_pattern_candidates(node: Node, source_bytes: bytes) -> list[Candidate]:
    """One candidate per name of ``const { a, b } = require("./m")``.

    That declaration is an import in all but spelling, so it loses one name
    the way ``import { a, b }`` does: the shorthand name alone, with its comma
    (``_sits_in_comma_separated_list``), and the declaration stays for the
    others. The candidate's span is the name, its initializer ``None``, so
    ``_apply`` can only delete it and the ``require`` still runs. Once one
    name is left the pattern binds one name, and the whole-declaration
    candidate takes over (``require("./m");``).

    Declined unless the pattern holds nothing but shorthand names. A rest
    element would receive the dropped property, a default (``{ a = f() }``)
    is code, and a renamed ``{ a: b }`` is not one token. And only for
    ``require``: dropping a name from any other object skips a property read,
    which a getter can make do work. A module's exports are read the same way
    an import binding is.
    """
    declarators = [c for c in node.named_children if c.type == "variable_declarator"]
    if len(declarators) != 1:
        return []
    pattern = declarators[0].child_by_field_name("name")
    if pattern is None or pattern.type != "object_pattern":
        return []
    if not _is_require(declarators[0].child_by_field_name("value")):
        return []
    names = [c for c in pattern.named_children if c.type != "comment"]
    if len(names) < 2 or any(c.type != _PATTERN_NAME for c in names):
        return []
    found = []
    for name in names:
        text = source_bytes[name.start_byte:name.end_byte].decode("utf-8", "replace")
        if not text.isidentifier():
            continue
        found.append(
            Candidate(
                name=text,
                start=name.start_byte,
                end=name.end_byte,
                kind="variable",
                text=text,
                binder=(name.start_byte, name.end_byte),
            )
        )
    return found


#: How each language spells "evaluate this and throw the value away".
#: Go's ``_ = expr`` is legal for EVERY expression, which is why Go never
#: reaches the leave-alone branch. The other three have no such universal
#: spelling: a bare expression statement is legal exactly for the invocation /
#: object-creation / ``await`` forms, which is what makes an initializer
#: effectful in the first place — so the overlap is total in practice and the
#: guard below is the honest backstop for the rest.
_DISCARD_PREFIX = {"go": "_ = "}

#: The languages whose discard needs no ``;``. Kotlin, Python and Ruby end a
#: statement at the newline, and ktlint's `no-semi` and RuboCop's
#: `Style/Semicolon` flag one written anyway.
_NO_STATEMENT_TERMINATOR = frozenset({"go", "kt", "python", "ruby"})

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

#: The node a Dart initializer may START with and still stand as a statement
#: once its declaration is gone. Dart admits any expression as a statement,
#: so what this guards is not legality but how a statement's FIRST TOKEN is
#: read: one starting with `{` is a block (a map literal's `{'a': f()};`
#: does not parse), one starting with `const` is a declaration, and a
#: function literal at statement start is not an expression statement at
#: all. Starting from a name, `new` or `await` is unambiguous, and those are
#: also the shapes an effectful initializer overwhelmingly takes: a call, a
#: constructor, a cascade, an awaited future.
_DART_STATEMENT_STARTS = frozenset({"identifier", "new_expression", "this"})

#: The languages whose deleted first-line import takes the blank line below it
#: along. None of their grammars gives a leading blank line any meaning, and
#: each formatter (`dotnet format`, `dart format`, Prettier) strips one. Go is
#: absent because its first line is always `package`, never an import.
_LEADING_BLANK_IS_MEANINGLESS = frozenset({"ts", "tsx", "js", "csharp", "dart", "java", "kt"})

#: The languages whose standard linter fails on two blank lines in a row,
#: which a deleted line between two blank lines leaves behind: ktlint's
#: `no-consecutive-blank-lines` and SwiftLint's `vertical_whitespace`, each
#: in its default rule set, PHP-CS-Fixer's `no_extra_blank_lines`, which
#: every PSR-12 and Symfony rule set includes, and RuboCop's default
#: `Layout/EmptyLines`.
_BLANK_RUN_IS_AN_ERROR = frozenset({"kt", "php", "ruby", "swift"})


def _apply(source: str, candidate: Candidate, language: str) -> str | None:
    """Remove or neutralise ``candidate``; ``None`` when neither is possible.

    An import is deleted, unless it carries a ``replacement``. That is a TS/JS
    statement whose module load must survive, and it becomes the bare
    ``import "./m";`` in place (see ``_ts_import_candidates``). No other
    import in these languages is kept for its load: Java and C# imports only
    name things, and Go refuses to compile an unused import, so it has to go.

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
    if (
        language == "kt"
        and candidate.kind == "variable"
        and _kotlin_ends_a_value_block(source, candidate)
    ):
        return None
    if candidate.replacement is not None:
        return (
            data[: candidate.start]
            + candidate.replacement.encode("utf-8")
            + data[candidate.end :]
        ).decode("utf-8")
    deletable = candidate.kind == "import" or is_inert(candidate, source, language)
    lines = None
    if language == "python":
        lines = _python_deletable_lines(source, candidate)
        if lines is None:
            deletable = False
    elif language == "swift" and candidate.kind == "variable":
        lines = _swift_deletable_lines(source, candidate)
        if lines is None:
            deletable = False
    elif language == "php":
        if candidate.kind == "import":
            block = _php_lone_block(source, candidate)
            if block is not None:
                return (data[: block[0]] + data[block[1] :]).decode("utf-8")
        lines = _php_deletable_lines(source, candidate)
        if lines is None and candidate.kind == "variable":
            deletable = False
        if lines is None and candidate.kind == "import":
            # Sharing its line (`<?php use X; $y = 1; ?>`): the space after it
            # goes too, or the tag is left followed by two.
            end = candidate.end
            while data[end : end + 1] in (b" ", b"\t"):
                end += 1
            if data[candidate.end : end] and data[end : end + 1] not in (b"\n", b"\r", b""):
                return (data[: candidate.start] + data[end:]).decode("utf-8")
    elif language == "ruby":
        lines = _ruby_deletable_lines(source, candidate)
        if lines is None:
            deletable = False
    if deletable and not _fills_a_parent_slot(source, candidate, language):
        start, end = candidate.start, candidate.end
        if _sits_in_comma_separated_list(source, candidate, language):
            start, end = _expand_over_separator(data, start, end)
        start, end = lines or _expand_to_whole_lines(data, start, end)
        if (
            start == _bom_length(data)
            and candidate.kind == "import"
            and language in _LEADING_BLANK_IS_MEANINGLESS
        ):
            # The file's first line went, so the blank line that separated
            # the imports from the code would now open the file.
            while data[end:].startswith((b"\n", b"\r\n")):
                end += 1 if data[end : end + 1] == b"\n" else 2
        elif (
            language in _BLANK_RUN_IS_AN_ERROR
            and (start, end) != (candidate.start, candidate.end)
            and data[:start].endswith((b"\n\n", b"\n\r\n"))
        ):
            # The line above is blank, so a blank line below would make two.
            while data[end:].startswith((b"\n", b"\r\n")):
                end += 1 if data[end : end + 1] == b"\n" else 2
        return (data[:start] + data[end:]).decode("utf-8")
    if candidate.initializer is None:
        return None
    init_start, init_end = candidate.initializer
    initializer = data[init_start:init_end].decode("utf-8")
    prefix = _DISCARD_PREFIX.get(language)
    if language == "dart":
        if not _dart_initializer_is_a_statement(source, candidate):
            return None
    elif language == "kt":
        if not _kotlin_initializer_is_a_statement(source, candidate):
            return None
    elif language == "python":
        # Never, for PHP's reason: CPython would free the call's object at
        # once, where the local held it until the function returned, and its
        # `__del__` would move. Measured on 3.12.
        return None
    elif language == "ruby":
        # Only a call, which a statement can be on its own: RuboCop's
        # `Lint/Void` flags a constant, a literal or a variable kept as one,
        # and a bare name may be a method call or may not.
        value = _node_at(source, "ruby", init_start, init_end)
        if value is None or value.type != "call":
            return None
    elif language == "php":
        # Never, for Swift's reason below: a call's object would be
        # destroyed at once, where the variable held it to the end of the
        # function, and its `__destruct` would move.
        return None
    elif language == "swift":
        # Never. `_ = LegacyFlow(ctx)` is `swiftc`'s own fix-it, and it
        # releases the object at once where the `let` held it to the end of
        # the scope: measured, a `deinit` that cancels a subscription or
        # releases a lock then runs before the code after it. No spelling
        # keeps both the call and the lifetime, so the declaration stays.
        return None
    elif prefix is None and not _is_statement_expression(source, candidate, language):
        return None
    replacement = f"{prefix or ''}{initializer}"
    if language not in _NO_STATEMENT_TERMINATOR:
        replacement += ";"
    return (
        data[: candidate.start] + replacement.encode("utf-8") + data[candidate.end :]
    ).decode("utf-8")


def _python_deletable_lines(source: str, candidate: Candidate) -> tuple[int, int] | None:
    """The whole lines a Python statement can be deleted as, or ``None``.

    Two shapes cannot lose the statement, and both would otherwise ship
    source CPython rejects (Gate 1 would then refuse the whole flag, losing
    the fold along with the tidy):

    * a statement sharing its line with another (``n = 3; go()`` would
      leave ``; go()``). A trailing COMMENT is not another statement: it
      annotates the binding, and goes with it;
    * the only statement of its block. ``if c:`` with nothing under it is an
      ``IndentationError`` that tree-sitter does not report;
    * a statement the line above continues into with ``\\``;
    * a body's first statement when a bare string follows it, which would
      become the ``__doc__``.

    A call kept as a statement (the discard) fills either just as well, so
    ``None`` here only stops the deletion.
    """
    data = source.encode("utf-8")
    line_start = max(data.rfind(b"\n", 0, candidate.start) + 1, _bom_length(data))
    line_end = data.find(b"\n", candidate.end)
    line_end = len(data) if line_end == -1 else line_end + 1
    tail = data[candidate.end:line_end].strip()
    if data[line_start:candidate.start].strip() or (tail and not tail.startswith(b"#")):
        return None
    if data[:line_start].endswith((b"\\\n", b"\\\r\n")):
        # The line above is continued into this one.
        return None
    # The statement itself, by type: a block holding only this statement
    # spans exactly the same bytes, and ``_node_at`` would answer with it.
    node = next(
        (
            n for n in _walk(_parse(source, "python"))
            if n.type == "expression_statement"
            and (n.start_byte, n.end_byte) == (candidate.start, candidate.end)
        ),
        None,
    )
    if node is None or node.parent is None:
        return None
    if node.parent.type == "block" and not any(
        sibling.id != node.id and sibling.type != "comment"
        for sibling in node.parent.named_children
    ):
        return None
    statements = [c for c in node.parent.named_children if c.type != "comment"]
    if statements[0].id == node.id and len(statements) > 1:
        following = statements[1]
        if following.type == "expression_statement" and following.named_child_count == 1 and (
            following.named_children[0].type in {"string", "concatenated_string"}
        ):
            # It would become the body's docstring.
            return None
    return line_start, line_end


def _ruby_deletable_lines(source: str, candidate: Candidate) -> tuple[int, int] | None:
    """The whole lines a Ruby assignment can be deleted as, or ``None``.

    Two shapes cannot lose the statement:

    * the LAST statement of its body, whose value is the method's, the
      block's or the branch's: deleting ``limit = 3`` at the end of a method
      changes what it returns. The kept call (the discard) returns the same
      value, so it is still offered;
    * one sharing its line with another (``n = 3; go``). A trailing ``#``
      comment goes with it.

    A line the one above continues into with ``\\`` needs no check: the
    assignment is then an argument, and was never a statement here.
    """
    data = source.encode("utf-8")
    line_start = max(data.rfind(b"\n", 0, candidate.start) + 1, _bom_length(data))
    line_end = data.find(b"\n", candidate.end)
    line_end = len(data) if line_end == -1 else line_end + 1
    tail = data[candidate.end:line_end].strip()
    if data[line_start:candidate.start].strip() or (tail and not tail.startswith(b"#")):
        return None
    node = next(
        (
            n for n in _walk(_parse(source, "ruby"))
            if n.type == "assignment"
            and (n.start_byte, n.end_byte) == (candidate.start, candidate.end)
        ),
        None,
    )
    if node is None:
        return None
    following = node.next_named_sibling
    while following is not None and following.type == "comment":
        following = following.next_named_sibling
    if following is None or following.type in {"rescue", "else", "ensure"}:
        # A `rescue`, `else` or `ensure` clause is a sibling in the body, not
        # a statement after this one: the value is still the body's.
        return None
    return line_start, line_end


def _php_lone_block(source: str, candidate: Candidate) -> tuple[int, int] | None:
    """The bytes of a ``<?php ... ?>`` block whose only statement is the
    ``use`` ``candidate``, with the one newline straight after ``?>``; None
    for any other ``use``.

    A template's ``<?php use App\\Flags; ?>`` would otherwise be left as an
    empty ``<?php  ?>``. PHP never outputs the newline that follows ``?>``,
    and the block outputs nothing, so the page renders the same without it.
    """
    data = source.encode("utf-8")
    node = next(
        (
            n
            for n in _walk(_parse(source, "php"))
            if n.type == "namespace_use_declaration" and n.start_byte == candidate.start
        ),
        None,
    )
    if node is None:
        return None
    before, after = node.prev_sibling, node.next_sibling
    if before is not None and before.type == "text_interpolation" and before.child_count:
        before = before.children[-1]
    if before is None or before.type != "php_tag" or before.text.lower() != b"<?php":
        return None
    if after is None or after.type != "text_interpolation" or not after.child_count:
        return None
    close = after.children[0]
    if close.type != "php_end_tag":
        return None
    end = close.end_byte
    if data[end : end + 2] == b"\r\n":
        end += 2
    elif data[end : end + 1] == b"\n":
        end += 1
    return before.start_byte, end


def _php_deletable_lines(source: str, candidate: Candidate) -> tuple[int, int] | None:
    """The whole lines a PHP assignment or ``use`` can be deleted as, or
    ``None`` when it shares its line with something that is not a comment,
    or is the unbraced body of a ``declare``.

    Deleting the statement alone would be legal PHP, but a statement left
    behind on the line would keep the deleted one's indentation and the
    space after its ``;``, so an assignment is then left. A ``use`` is
    deleted alone, since nothing indents one. A trailing ``//`` or ``#`` comment goes with the
    binding, unless it holds a ``?>``, which ends PHP mode even inside a line
    comment and so takes markup with it.
    """
    data = source.encode("utf-8")
    line_start = max(data.rfind(b"\n", 0, candidate.start) + 1, _bom_length(data))
    line_end = data.find(b"\n", candidate.end)
    line_end = len(data) if line_end == -1 else line_end + 1
    tail = data[candidate.end:line_end].strip()
    if data[line_start:candidate.start].strip():
        return None
    node = _node_at(source, "php", candidate.start, candidate.end)
    if node is not None and node.parent is not None and node.parent.type == "declare_statement":
        # `declare(ticks=1) $v = 2;` holds its body in no field, so
        # `_fills_a_parent_slot` cannot see it; the next statement would
        # become the body.
        return None
    if tail and (
        not tail.startswith((b"//", b"#")) or tail.startswith(b"#[") or b"?>" in tail
    ):
        return None
    return line_start, line_end


def _swift_deletable_lines(source: str, candidate: Candidate) -> tuple[int, int] | None:
    """The whole lines a Swift local can be deleted as, or ``None``.

    Two shapes cannot lose the declaration, and each would ship source
    ``swiftc`` rejects although tree-sitter parses it:

    * one sharing its line with another statement. ``let n = 3; go()``
      would leave ``; go()``, and "';' statements are not allowed". A
      trailing ``//`` comment is not another statement: it annotates the
      binding, and goes with it. A ``/*`` one is declined, because the line
      can go on after it closes, or the comment can close on a later line;
    * the only statement of a ``case``, which "must have at least one
      executable statement".
    """
    data = source.encode("utf-8")
    line_start = max(data.rfind(b"\n", 0, candidate.start) + 1, _bom_length(data))
    line_end = data.find(b"\n", candidate.end)
    line_end = len(data) if line_end == -1 else line_end + 1
    tail = data[candidate.end:line_end].strip()
    if data[line_start:candidate.start].strip() or (
        tail and not tail.startswith(b"//")
    ):
        return None
    # By type: a block holding only this declaration spans the same bytes,
    # and ``_node_at`` would answer with it.
    node = next(
        (
            n for n in _walk(_parse(source, "swift"))
            if n.type == "property_declaration"
            and (n.start_byte, n.end_byte) == (candidate.start, candidate.end)
        ),
        None,
    )
    if node is None or node.parent is None:
        return None
    statements = node.parent
    if (
        statements.parent is not None
        and statements.parent.type == "switch_entry"
        and not any(
            sibling.id != node.id and "comment" not in sibling.type
            for sibling in statements.named_children
        )
    ):
        return None
    return line_start, line_end


def _kept_require(source: str, candidate: Candidate, language: str) -> str | None:
    """The ``require("./m");`` a CommonJS binding was discarded to, or ``None``.

    ``const { client } = require("./m")`` is a variable, so ``_apply`` never
    deletes it: a call is not inert, and the declaration becomes the discard
    ``require("./m");``. That line is the CommonJS spelling of a bare
    ``import "./m";`` — the binding is gone and only the module load is left —
    so the pull request names it alongside the bare imports rather than
    counting it as removed.

    Only a call whose callee IS the identifier ``require``. Any other discard
    (``compute();``, ``require("./m")();``) keeps a call the customer wrote for
    what it does, not a load, and stays reported as the binding it removed.
    """
    if language not in _TS_MODULE_LANGUAGES or candidate.initializer is None:
        return None
    start, end = candidate.initializer
    node = _node_at(source, language, start, end)
    if node is None or node.type != "call_expression":
        return None
    callee = node.child_by_field_name("function")
    if callee is None or callee.type != "identifier" or callee.text != b"require":
        return None
    # `_apply`'s discard for these languages: the initializer, then `;`.
    return source.encode("utf-8")[start:end].decode("utf-8") + ";"


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


def _dart_initializer_is_a_statement(source: str, candidate: Candidate) -> bool:
    """Whether this Dart initializer still parses as the statement it would
    become; see ``_DART_STATEMENT_STARTS``. An ``await`` arrives wrapped in a
    one-child ``unary_expression`` and is admitted through it."""
    nodes = _initializer_nodes(source, candidate, "dart")
    if not nodes:
        return False
    first = nodes[0]
    if first.type == "unary_expression" and first.child_count == 1:
        first = first.children[0]
        return first.type == "await_expression"
    return first.type in _DART_STATEMENT_STARTS


#: The first token a Kotlin discard may START with. A name, ``this`` or
#: ``super`` can never be read as the tail of the statement above, which a
#: line starting with ``(`` or ``{`` can.
_KOTLIN_STATEMENT_STARTS = frozenset({"identifier", "this", "super"})

#: The parents of a block whose last statement is NOT a value: a function's
#: or a constructor's body, a loop's body and a `finally`. An `if`, `when`,
#: `try` or `catch` block, and a lambda, have a value.
_KOTLIN_STATEMENT_BLOCKS = frozenset({
    "function_body", "secondary_constructor", "anonymous_initializer",
    "for_statement", "while_statement", "do_while_statement", "finally_block",
})


def _kotlin_ends_a_value_block(source: str, candidate: Candidate) -> bool:
    """Whether this Kotlin declaration is the last statement of a block whose
    last statement is the block's VALUE.

    A declaration there makes the value ``Unit``. Deleting it hands the value
    to the statement before it, and discarding it hands the value to the
    call; either can change the type of the ``if``/``when``/``try`` around it
    (``val r: Unit = if (c) { track(); val n = 3 } else { Unit }`` stops
    compiling) or silently what it returns. So such a declaration is neither
    deleted nor discarded.
    """
    node = _node_at(source, "kt", candidate.start, candidate.end)
    if node is None or node.parent is None:
        return True
    following = node.next_named_sibling
    while following is not None and "comment" in following.type:
        following = following.next_named_sibling
    block = node.parent
    return following is None and (
        block.parent is None or block.parent.type not in _KOTLIN_STATEMENT_BLOCKS
    )


def _kotlin_initializer_is_a_statement(source: str, candidate: Candidate) -> bool:
    """Whether this Kotlin initializer can stand as the statement it would
    become.

    Only a CALL. Kotlin admits any expression as a statement, but anything
    else that can run code (``items[0]``'s ``operator get``, ``x!!``, a
    template's ``toString``, a cast, an elvis) would stand as an expression
    whose value nobody reads. ``kotlinc`` warns on some of those (a template
    is "expression is unused"), and a reviewer reads a bare ``items[0]`` as a
    mistake. Leaving the declaration standing is the smaller cost.

    A declaration that ends a block with a value never reaches this; see
    ``_kotlin_ends_a_value_block``.
    """
    start, end = candidate.initializer
    value = _node_at(source, "kt", start, end)
    if value is None or value.type != "call_expression":
        return False
    first = value
    while first.child_count:
        first = first.children[0]
    return first.type in _KOTLIN_STATEMENT_STARTS


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
    line_start = max(data.rfind(b"\n", 0, start) + 1, _bom_length(data))
    line_end = data.find(b"\n", end)
    line_end = len(data) if line_end == -1 else line_end + 1
    before_blank = data[line_start:start].strip() == b""
    after_blank = data[end : line_end - 1 if line_end > end else line_end].strip() == b""
    if before_blank and after_blank:
        return line_start, line_end
    return start, end


def _bom_length(data: bytes) -> int:
    """How many bytes a UTF-8 byte-order mark takes at the start of ``data``.

    The first line starts after it. Visual Studio writes one by default, and
    a mark is not text on that line: counting it as such leaves a first-line
    deletion with its newline behind, and deleting it would change an
    encoding marker the customer chose.
    """
    return len(codecs.BOM_UTF8) if data.startswith(codecs.BOM_UTF8) else 0


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
#: Dart's body is the SIBLING of its signature (``method_signature`` then
#: ``function_body`` under a class body), so the scope root is the body and
#: its name is read off the signature beside it — see ``_dart_scope_key``.
#: ``function_expression`` is the anonymous closure, whose own body is a
#: ``function_expression_body`` and so never reaches the first entry.
_SCOPE_ROOTS["dart"] = frozenset({"function_body", "function_expression"})
#: Kotlin's named roots are the function and the secondary constructor. The
#: rest are here so that a local inside one stops at it rather than at the
#: function around it, and is then declined by ``_kotlin_scope_key``: a lambda,
#: an anonymous function and an object expression have no name, a class can
#: hold several ``init`` blocks, and every getter is spelled ``get()``.
_SCOPE_ROOTS["kt"] = frozenset({
    "function_declaration", "secondary_constructor", "anonymous_initializer",
    "getter", "setter", "lambda_literal", "anonymous_function",
})

#: Python's one named local scope is a ``def``. A class stops the walk so a
#: method is qualified by it, and a lambda so it is declined: it has no name,
#: and cannot hold an assignment anyway.
_SCOPE_ROOTS["python"] = _PYTHON_SCOPES

#: Swift's named scopes are a ``func``, an ``init`` and a ``deinit``. The
#: rest stop the walk so that a local inside one is declined by
#: ``_swift_scope_key`` rather than paired by the function around it: a
#: closure has no name, a getter and a setter are each spelled ``get``/``set``
#: in every property, and a subscript's accessors are the same.
_SWIFT_ANONYMOUS_SCOPES = frozenset({
    "lambda_literal", "computed_property", "computed_getter", "computed_setter",
    "computed_modify", "willset_clause", "didset_clause", "subscript_declaration",
})
_SWIFT_NAMED_SCOPES = frozenset({
    "function_declaration", "init_declaration", "deinit_declaration",
})
_SCOPE_ROOTS["swift"] = _SWIFT_NAMED_SCOPES | _SWIFT_ANONYMOUS_SCOPES

#: Ruby's named scopes are a ``def`` and a ``def self.``. The rest stop the
#: walk and are declined by ``_ruby_scope_key``: a block and a lambda have no
#: name, and a class body's locals are not a method's.
_SCOPE_ROOTS["ruby"] = _RUBY_SCOPES

#: PHP's named scopes are a function and a method; a closure and an arrow
#: function stop the walk and are declined by ``_php_scope_key``.
_SCOPE_ROOTS["php"] = _PHP_SCOPES

#: The PHP declarations whose header qualifies a method inside them.
_PHP_TYPE_DECLARATIONS = frozenset({
    "class_declaration", "trait_declaration", "interface_declaration",
    "enum_declaration",
})

#: The Swift declarations whose header qualifies a scope inside them.
#: ``class_declaration`` is also every ``struct``, ``enum``, ``actor`` and
#: ``extension``.
_SWIFT_TYPE_DECLARATIONS = frozenset({"class_declaration", "protocol_declaration"})

#: The Kotlin scopes ``_kotlin_scope_key`` cannot name.
_KOTLIN_ANONYMOUS_SCOPES = frozenset({
    "anonymous_initializer", "getter", "setter", "lambda_literal",
    "anonymous_function", "object_literal",
})

#: The Kotlin declarations whose header qualifies a scope inside them.
_KOTLIN_TYPE_DECLARATIONS = frozenset({
    "class_declaration", "object_declaration", "companion_object",
})

#: The Dart declarations a function body can sit inside whose NAME qualifies
#: it. Every Flutter widget has a `build`, so a scope named by its method
#: alone is ambiguous in almost every Flutter file, and an ambiguous scope is
#: one this pass declines to act in.
_DART_TYPE_DECLARATIONS = frozenset({
    "class_definition", "mixin_declaration", "extension_declaration",
    "enum_declaration", "extension_type_declaration",
})


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


def _scope_key(scope: Node, language: str) -> str | None:
    """What identifies ``scope`` across the fold; ``None`` when nothing does."""
    if language == "dart":
        return _dart_scope_key(scope)
    if language == "kt":
        return _kotlin_scope_key(scope)
    if language == "python":
        return _python_scope_key(scope)
    if language == "swift":
        return _swift_scope_key(scope)
    if language == "php":
        return _php_scope_key(scope)
    if language == "ruby":
        return _ruby_scope_key(scope)
    return _scope_name(scope)


def _normalised(node: Node) -> str:
    return " ".join(node.text.decode("utf-8", "replace").split())


def _dart_signature(body: Node) -> Node | None:
    """The signature a Dart ``function_body`` belongs to.

    A method's or top-level function's signature is the body's previous
    sibling (comments between the two skipped); a named local function puts
    both under a ``lambda_expression``, the signature in its ``parameters``
    field. Anything else (an anonymous closure's body is never a
    ``function_body``) has no signature to name it by.
    """
    parent = body.parent
    if parent is not None and parent.type == "lambda_expression":
        return parent.child_by_field_name("parameters")
    previous = body.prev_named_sibling
    while previous is not None and "comment" in previous.type:
        previous = previous.prev_named_sibling
    if previous is None or not previous.type.endswith("signature"):
        return None
    return previous


def _dart_scope_key(scope: Node) -> str | None:
    """The scope's signature, qualified by every named scope and type it sits
    in, or ``None`` when any of them is anonymous.

    The signature TEXT rather than its name field: Dart spells a name four
    ways across signature kinds (a ``name`` field, several ``name`` fields
    for ``A.named``, none at all for a factory), and a getter and a setter
    share one name while their signatures differ. A fold never touches a
    signature, so the text is stable across it; and if it ever were not, the
    pairing fails, which is a missed tidy. A type contributes its header
    (everything before its body), which names it whether or not it has a
    ``name`` field — an unnamed ``extension on String`` included.
    """
    parts: list[str] = []
    node: Node | None = scope
    while node is not None:
        if node.type == "function_expression":
            return None
        if node.type == "function_body":
            signature = _dart_signature(node)
            if signature is None:
                return None
            parts.append(_normalised(signature))
        elif node.type in _DART_TYPE_DECLARATIONS:
            body = next(
                (c for c in node.children if c.type.endswith("_body")), None
            )
            end = body.start_byte if body is not None else node.end_byte
            header = node.text[: end - node.start_byte].decode("utf-8", "replace")
            parts.append(" ".join(header.split()))
        node = node.parent
    return " / ".join(reversed(parts))


def _kotlin_scope_key(scope: Node) -> str | None:
    """The scope's header, qualified by every function and type it sits in,
    or ``None`` when any of them is anonymous.

    The header is the declaration's text up to its body, whitespace
    normalised: the name alone is not enough, because overloads share one
    and an ``override`` has the same name in every subclass. A fold never
    touches a header unless a flag is read in a default argument, and then
    the pairing fails, which is a missed tidy.
    """
    parts: list[str] = []
    node: Node | None = scope
    while node is not None:
        if node.type in _KOTLIN_ANONYMOUS_SCOPES:
            return None
        if node.type in {"function_declaration", "secondary_constructor"} | (
            _KOTLIN_TYPE_DECLARATIONS
        ):
            body = next(
                (
                    c for c in node.children
                    if c.type in {"function_body", "block", "class_body", "enum_class_body"}
                ),
                None,
            )
            end = body.start_byte if body is not None else node.end_byte
            header = node.text[: end - node.start_byte].decode("utf-8", "replace")
            parts.append(" ".join(header.split()))
        node = node.parent
    return " / ".join(reversed(parts))


def _python_scope_key(scope: Node) -> str | None:
    """The ``def`` line, qualified by every ``def`` and ``class`` it sits in,
    or ``None`` inside a lambda.

    The header rather than the name, for the reason Kotlin's is: every class
    in a file can have a ``run``, and a ``@property`` and its setter share
    one name while their headers differ. Two identical headers in one class
    (an ``@overload`` stub beside its implementation) pair with nothing, which
    is a missed tidy.
    """
    parts: list[str] = []
    node: Node | None = scope
    while node is not None:
        if node.type == "lambda":
            return None
        if node.type in {"function_definition", "class_definition"}:
            body = node.child_by_field_name("body")
            end = body.start_byte if body is not None else node.end_byte
            header = node.text[: end - node.start_byte].decode("utf-8", "replace")
            parts.append(" ".join(header.split()))
        node = node.parent
    return " / ".join(reversed(parts))


def _swift_scope_key(scope: Node) -> str | None:
    """The scope's header, qualified by every function and type it sits in,
    or ``None`` when any of them is anonymous.

    The header rather than the name, for the reason Kotlin's is: overloads
    share a name (``func run(_ id: Int)`` beside ``func run(_ name:
    String)``), and every type in a file can have its own ``init``.
    """
    parts: list[str] = []
    node: Node | None = scope
    while node is not None:
        if node.type in _SWIFT_ANONYMOUS_SCOPES:
            return None
        if node.type in _SWIFT_NAMED_SCOPES | _SWIFT_TYPE_DECLARATIONS:
            body = node.child_by_field_name("body")
            end = body.start_byte if body is not None else node.end_byte
            header = node.text[: end - node.start_byte].decode("utf-8", "replace")
            parts.append(" ".join(header.split()))
        node = node.parent
    return " / ".join(reversed(parts))


def _ruby_scope_key(scope: Node) -> str | None:
    """The ``def`` line, qualified by every class and module it sits in, or
    ``None`` when the scope is not a method: a block's, a lambda's and a
    class body's locals have no name to pair. The header rather than the
    name: every class in a file can have a ``call``."""
    if scope.type not in {"method", "singleton_method"}:
        return None
    parts: list[str] = []
    node: Node | None = scope
    while node is not None:
        if node.type in {"method", "singleton_method", "class", "module", "singleton_class"}:
            body = node.child_by_field_name("body")
            end = body.start_byte if body is not None else node.end_byte
            header = node.text[: end - node.start_byte].decode("utf-8", "replace")
            parts.append(" ".join(header.split()))
        node = node.parent
    return " / ".join(reversed(parts))


def _php_scope_key(scope: Node) -> str | None:
    """The function's header, qualified by every type and function it sits
    in, or ``None`` inside a closure or an arrow function. The header rather
    than the name: every class in a file can have a ``run``."""
    parts: list[str] = []
    node: Node | None = scope
    while node is not None:
        if node.type in {"anonymous_function", "arrow_function"}:
            return None
        if node.type in {"function_definition", "method_declaration"} | (
            _PHP_TYPE_DECLARATIONS
        ):
            body = node.child_by_field_name("body")
            end = body.start_byte if body is not None else node.end_byte
            header = node.text[: end - node.start_byte].decode("utf-8", "replace")
            parts.append(" ".join(header.split()))
        node = node.parent
    return " / ".join(reversed(parts))


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
    if candidate_node.type == _PATTERN_NAME:
        # One name of a `require` pattern: its scope is its declaration's.
        while candidate_node is not None and candidate_node.type != _VARIABLE_DECLARATIONS[language][0]:
            candidate_node = candidate_node.parent
        if candidate_node is None:
            return None
    current_scope = _enclosing_scope(candidate_node, language)
    if current_scope is None:
        return _module_scopes(before, current, candidate_node, language)
    name = _scope_key(current_scope, language)
    if name is None:
        return None
    matches = [
        node
        for node in _walk(_parse(before, language))
        if node.type == current_scope.type and _scope_key(node, language) == name
    ]
    if len(matches) != 1:
        return None
    return (matches[0].start_byte, matches[0].end_byte), (
        current_scope.start_byte,
        current_scope.end_byte,
    )


def _module_scopes(
    before: str, current: str, candidate_node: Node, language: str
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The whole-file spans, when the candidate's scope IS the file.

    ``_matching_scope`` pairs two scopes by NAME, and a module has none — the
    reason the module-level bindings in #2825 were unreachable even once
    ``_variable_candidates`` was willing to produce them. But a name is only
    needed to tell two candidate scopes apart, and there are never two: each
    parse has exactly one program, so the pairing that is ambiguous for two
    same-named functions is forced here. The span is the file.

    Counting file-wide cannot delete a live binding, which is the property
    that matters, and it holds for the same reason the function-scoped count
    does. ``_references_in_scope`` hides only BINDER TOKENS, so widening the
    span can only hide more binders and count more leaves; reaching zero
    still means no leaf anywhere in the file spells the name outside a
    binding position. A nested function's own same-named local therefore
    makes the module binding read as REFERENCED — a missed tidy, never a
    wrong deletion, which is the direction this pass errs in everywhere else.

    Both sides are re-tested for module-ness rather than assumed from one.
    They cannot in practice disagree — a fold only removes code, so it cannot
    turn a module into a script — but the invariant that makes a module-level
    ``before`` count honest is that ``_variable_candidates(before, …)``
    blanked the same binder, and that is a property of ``before``, not of
    this fold.
    """
    if language not in _TS_MODULE_LANGUAGES:
        return None
    program = candidate_node.parent
    if program is None or program.type != _TS_PROGRAM:
        return None
    if not _top_level_bindings_are_private(program, language):
        return None
    if not _top_level_bindings_are_private(_parse(before, language), language):
        return None
    return (0, len(before.encode("utf-8"))), (0, len(current.encode("utf-8")))


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
    return sum(count for _, count in _counted_leaves(source, name, language, start, end))


def _counted_leaves(
    source: str, name: str, language: str, start: int, end: int
) -> list[tuple[Node, int]]:
    """Each leaf :func:`_references_in_scope` counts, with its count."""
    own_binders = [
        candidate.binder
        for candidate in _variable_candidates(source, language)
        if candidate.name == name
        and candidate.binder is not None
        and start <= candidate.binder[0]
        and candidate.binder[1] <= end
    ]
    word = re.compile(rf"\b{re.escape(name)}\b")
    found: list[tuple[Node, int]] = []
    for node in _walk(_parse(source, language)):
        if node.child_count and not _read_by_words(node, language):
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
        if _read_by_words(node, language):
            if count := len(word.findall(text)):
                found.append((node, count))
        elif _spelling(text, language) == name and not _inside_import(node, language):
            found.append((node, 1))
    return found


def go_local_reads(source: str, binder: tuple[int, int]) -> list[tuple[int, int]] | None:
    """What this pass would count as references to the Go local whose name
    token spans ``binder``, as byte spans; ``None`` when it never removes or
    neutralises that local, however unused it ends up.

    The keyless-wrapper proof asks this before folding a call that reads the
    local. Go does not build with an unused local, so a call whose fold would
    leave one standing has to be refused rather than folded. ``None`` covers
    every shape this pass declines: several names with values in one
    declaration, a scope it cannot pair across the fold (a closure, two
    functions of one name), a declaration naming the local twice. The spans
    let the caller check the rest: each one must be a read Go counts as a use,
    or the count here stays above zero after Go has stopped seeing a use.
    """
    for candidate in _variable_candidates(source, "go"):
        if candidate.binder != binder:
            continue
        if not _bound_name_occurs_once_in_own_span(candidate, source, "go"):
            return None
        scopes = _matching_scope(source, source, candidate, "go")
        if scopes is None:
            return None
        start, end = scopes[1]
        return [
            (node.start_byte, node.end_byte)
            for node, _ in _counted_leaves(source, candidate.name, "go", start, end)
        ]
    return None


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
    "php": ("member_access_expression", "name"),
    "python": ("attribute", "attribute"),
    "swift": ("navigation_suffix", "suffix"),
    "csharp": ("member_access_expression", "name"),
    "ts": ("member_expression", "property"),
    "tsx": ("member_expression", "property"),
    "js": ("member_expression", "property"),
}


#: Dart's member access is a ``selector`` chain rather than one node, so the
#: PARENT of the property token tells it apart: ``.x``, ``?.x`` and ``..x``
#: each wrap the name in one of these, and nothing else does. An index
#: (``items[x]``) wraps its ``x`` in an ``index_selector`` first, so its read
#: is still counted.
_DART_PROPERTY_PARENTS = frozenset({
    "unconditional_assignable_selector", "conditional_assignable_selector",
    "cascade_selector",
})


#: The tokens that make the name after them a MEMBER in a Kotlin
#: ``navigation_expression``, whose children carry no field names.
_KOTLIN_MEMBER_OPERATORS = frozenset({".", "?.", "::"})


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
    if language == "dart":
        return (
            leaf.type == "identifier"
            and leaf.parent is not None
            and leaf.parent.type in _DART_PROPERTY_PARENTS
        )
    if language == "kt":
        # A CALLED member is not excluded: `"s".t()` is how a local of
        # extension-function type (`val t: String.() -> String`) is invoked,
        # and that is a read of the local. Counting every `obj.start()` too
        # costs only a missed tidy when a local shares a method's name.
        previous = leaf.prev_sibling
        access = leaf.parent
        return (
            leaf.type == "identifier"
            and access is not None
            and access.type == "navigation_expression"
            and previous is not None
            and previous.type in _KOTLIN_MEMBER_OPERATORS
            and not (
                access.parent is not None
                and access.parent.type == "call_expression"
                and access.parent.children[0].id == access.id
            )
        )
    if language == "ruby":
        if leaf.type == "hash_key_symbol":
            # `retries: 3` names a key; `retries:` alone (Ruby 3.1) reads the
            # local `retries`, so only a key with a value is excluded.
            pair = leaf.parent
            return (
                pair is not None
                and pair.type == "pair"
                and pair.child_by_field_name("value") is not None
            )
        # `obj.legacy`, `legacy(1)` and `legacy { }` call a method even
        # beside a local of that name. Arguments WITHOUT parentheses do not:
        # the grammar cannot know `legacy` is a local, so `legacy [1]` and
        # `legacy -1` arrive as calls, and Ruby, which can, indexes and
        # subtracts. A bare `legacy` is an `identifier`, never this.
        call = leaf.parent
        if call is None or call.type != "call":
            return False
        method = call.child_by_field_name("method")
        if method is None or method.id != leaf.id:
            return False
        arguments = call.child_by_field_name("arguments")
        return (
            call.child_by_field_name("receiver") is not None
            or call.child_by_field_name("block") is not None
            or (arguments is not None and arguments.text.startswith(b"("))
        )
    if (
        language == "swift"
        and leaf.parent is not None
        and leaf.parent.type == "value_argument_label"
    ):
        # `modern(retries: config.retries)` reads neither `retries` the
        # label nor `retries` the member; a label is never a read.
        return True
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
        if leaf.child_count and not _read_by_words(leaf, language):
            continue
        if _is_member_access_property(leaf, language):
            continue
        text = leaf.text.decode("utf-8", "replace")
        if _read_by_words(leaf, language) and "comment" not in leaf.type:
            count += len(re.findall(rf"\b{re.escape(candidate.name)}\b", text))
        elif _spelling(text, language) == candidate.name:
            count += 1
        if count > 1:
            return False
    return True


#: The languages where a method can be private to ONE FILE's worth of source.
#: Go and the TypeScript family are absent on purpose: an unexported Go func is
#: package-scoped and a package spans files, and a TS `private` member is a
#: type-check fiction — the method is reachable at run time, by string, from
#: anywhere. A one-file differential can decide neither.
_METHOD_LANGUAGES = frozenset({"csharp", "java"})

#: C# modifiers that disqualify a method even beside `private`. `partial` is
#: the one that matters most: its other half lives in another file. `extern`
#: is a body supplied from outside. `protected` turns `private protected` into
#: a member derived types can call.
_CSHARP_DISQUALIFYING_MODIFIERS = frozenset({"partial", "extern", "protected"})

#: C# type declarations whose `partial` modifier spreads a type across files.
_CSHARP_TYPE_DECLARATIONS = frozenset({
    "class_declaration", "struct_declaration", "record_declaration",
    "interface_declaration",
})

#: The comment node types, per language, that can document a method.
_METHOD_COMMENTS = {
    "csharp": frozenset({"comment"}),
    "java": frozenset({"line_comment", "block_comment"}),
}


def _csharp_modifiers(node: Node) -> set[str]:
    return {
        child.text.decode("utf-8")
        for child in node.children
        if child.type == "modifier"
    }


def _is_class_private_method(node: Node, language: str) -> bool:
    """Whether ``node`` is a method nothing outside this file can call.

    Explicit `private` only. C# makes an unmodified member private too, but
    an interface member with no modifier is PUBLIC, and telling the two apart
    is one more question this pass would have to get right for no gain.

    An annotated or attributed method is refused outright: an attribute is how
    a method becomes an entry point that no source calls — a test, a
    serialization hook, a native callback, a JSON-RPC handler — and the list of
    such attributes is open-ended.
    """
    if language == "csharp":
        if any(child.type == "attribute_list" for child in node.children):
            return False
        modifiers = _csharp_modifiers(node)
        if "private" not in modifiers or modifiers & _CSHARP_DISQUALIFYING_MODIFIERS:
            return False
        ancestor = node.parent
        while ancestor is not None:
            if (
                ancestor.type in _CSHARP_TYPE_DECLARATIONS
                and "partial" in _csharp_modifiers(ancestor)
            ):
                return False
            ancestor = ancestor.parent
        return True
    modifiers = next((c for c in node.children if c.type == "modifiers"), None)
    if modifiers is None:
        return False
    words = set()
    for child in modifiers.children:
        if "annotation" in child.type:
            return False
        words.add(child.type)
    return "private" in words and "native" not in words


def _method_candidates(source: str, language: str) -> list[Candidate]:
    """Every class-private method in ``source`` whose name no other method
    declaration in the file shares.

    The uniqueness requirement is what lets a NAME stand in for the method:
    with two declarations of `Context` (overloads, or one in a nested type),
    a surviving call names one of them and this pass cannot tell which.

    ``start`` reaches back over a comment that touches the declaration — a
    doc comment left above the next member would describe the wrong method.
    """
    if language not in _METHOD_LANGUAGES:
        return []
    root = _parse(source, language)
    source_bytes = source.encode("utf-8")
    methods = [n for n in _walk(root) if n.type == "method_declaration"]
    names: dict[str, int] = {}
    for method in methods:
        name_node = method.child_by_field_name("name")
        if name_node is not None:
            key = name_node.text.decode("utf-8")
            names[key] = names.get(key, 0) + 1
    candidates = []
    for method in methods:
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        name = name_node.text.decode("utf-8")
        if names[name] != 1 or not _is_class_private_method(method, language):
            continue
        start = method.start_byte
        previous = method.prev_sibling
        while (
            previous is not None
            and previous.type in _METHOD_COMMENTS[language]
            and source_bytes[previous.end_byte:start].count(b"\n") == 1
            and not source_bytes[previous.end_byte:start].strip()
        ):
            start = previous.start_byte
            previous = previous.prev_sibling
        candidates.append(
            Candidate(
                name=name,
                start=start,
                end=method.end_byte,
                kind="private method",
                text=source_bytes[method.start_byte:method.end_byte].decode(
                    "utf-8", "replace"
                ),
            )
        )
    return candidates


def _method_references(source: str, name: str, language: str) -> int | None:
    """``references()`` outside the declaration of ``name`` itself; ``None``
    when ``source`` holds no single class-private method of that name.

    The WHOLE declaration is blanked, attached doc comment included, not just
    its name token. A doc comment naming the method it documents is not a use
    of it, and neither is a recursive call: a method whose only caller is
    itself is as dead as one with no caller.

    File-wide, which is exactly a private method's reach once partial types
    are refused. A mention in a string or another comment counts, as it does
    for every other kind here — reflection names a method by string, and a
    missed tidy is the cheap direction.
    """
    own = [c for c in _method_candidates(source, language) if c.name == name]
    if len(own) != 1:
        return None
    data = source.encode("utf-8")
    start, end = own[0].start, own[0].end
    data = data[:start] + re.sub(rb"[^\n]", b" ", data[start:end]) + data[end:]
    return references(data.decode("utf-8", "replace"), name, language)


def _delete_method(source: str, candidate: Candidate) -> str:
    """Delete a method's whole lines and the blank line that would be left
    standing beside it.

    Members are conventionally separated by one blank line. Deleting a member
    between two others must leave exactly one; deleting the LAST member must
    not leave a blank line before the closing brace, nor the first one a blank
    line after the opening brace.
    """
    data = source.encode("utf-8")
    start, end = _expand_to_whole_lines(data, candidate.start, candidate.end)
    if start == candidate.start and end == candidate.end:
        return (data[:start] + data[end:]).decode("utf-8")

    def line_before(position: int) -> tuple[int, bytes]:
        line_start = data.rfind(b"\n", 0, max(position - 1, 0)) + 1
        return line_start, data[line_start:position]

    def line_at(position: int) -> tuple[int, bytes]:
        line_end = data.find(b"\n", position)
        line_end = len(data) if line_end == -1 else line_end + 1
        return line_end, data[position:line_end]

    previous_start, previous = line_before(start)
    following_end, following = line_at(end)
    if start > 0 and not previous.strip() and (
        not following.strip() or following.strip().startswith(b"}")
    ):
        start = previous_start
    elif not following.strip() and following and previous.rstrip().endswith(b"{"):
        end = following_end
    return (data[:start] + data[end:]).decode("utf-8")


def _import_references(source: str, candidate: Candidate, language: str) -> int:
    """How often ``source`` spells any binding of the import ``candidate``."""
    return sum(
        _references_excluding_own_declaration(source, name, language)
        - (_php_member_spellings(source, name) if language == "php" else 0)
        for name in (candidate.names or (candidate.name,))
    )


#: PHP nodes whose ``name`` field is a member of an object or class.
_PHP_MEMBER_ACCESSES = frozenset(
    {
        "member_access_expression",
        "nullsafe_member_access_expression",
        "member_call_expression",
        "nullsafe_member_call_expression",
        "scoped_call_expression",
    }
)


def _php_member_spellings(source: str, name: str) -> int:
    """How many of the leaves :func:`references` counts for ``name`` are a
    member's name: ``X::NAME``, ``$x->NAME``, ``$x?->NAME``, ``$x->NAME()`` or
    ``X::NAME()``. No ``use`` binds a member, so none of them reads one, and a
    fold that removes one strands nothing."""
    wanted = name.casefold()
    total = 0
    for node in _walk(_parse(source, "php")):
        if node.type != "name" or node.text.decode("utf-8", "replace").casefold() != wanted:
            continue
        parent = node.parent
        if parent is None:
            continue
        if parent.type == "class_constant_access_expression":
            total += parent.named_children[0].id != node.id
        elif parent.type in _PHP_MEMBER_ACCESSES:
            member = parent.child_by_field_name("name")
            total += member is not None and member.id == node.id
    return total


def remove_stranded_bindings(
    before: str,
    after: str,
    language: str,
    *,
    include_methods: bool = False,
    path: str | None = None,
    root: str | None = None,
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

    ``path`` and ``root`` — the file's own path and the checkout it lives in
    — let a plain Dart import be judged by the library it names (see
    :func:`_dart_import_candidates`). Without them such an import is never a
    candidate, which is the conservative reading. In the TS family they let a
    stranded statement of a local module whose load is provably inert be
    deleted rather than kept as a bare import (see
    :mod:`flag_cleanup.inert_load`); without them it is always kept.

    PRIVATE METHODS are offered only with ``include_methods``, and the
    transform asks for them in a SECOND call, after the reindent pass rather
    than beside the first. A method deletion removes whole lines, so it needs
    no reindenting itself — but the reindent pass reads the diff between the
    engine's input and the current text, and a deleted method sitting in the
    same hunk as a fold beside it threw off the indentation it gave the folded
    line. Offered after variables and before imports: deleting a method can
    strand the import of a type only it used.

    VARIABLES ARE OFFERED BEFORE IMPORTS, and that order is load-bearing in
    one direction only. It is the cheaper order anyway — the feeding runs
    one way, since deleting a variable can strand the import it read while
    deleting an import can strand no variable (``_references_in_scope``
    already ignores every leaf inside an import). What makes it necessary is
    that a TS/JS file's top-level bindings are private BECAUSE it is a
    module, and the thing that proves it is a module can be an import THIS
    PASS DELETES. Imports-first, a file whose only `import` and whose only
    top-level `const` were both stranded lost the import in the first round
    and then read as a plain script — so the `const` stopped being a
    candidate at all and shipped as the `no-unused-vars` error this pass
    exists to prevent. Variables-first cannot reach that state: no import is
    touched in a round where any variable is still actionable, and an
    import-only round changes no variable's reference count, so by the time
    the last import goes there is nothing left for it to strand.

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
    methods = _method_candidates if include_methods else (lambda _s, _l: [])
    resolve = (
        dart_namespace.resolver(path, root)
        if language == "dart" and path is not None and root is not None
        else None
    )
    inert = (
        lru_cache(maxsize=None)(partial(inert_load.is_inert, path, repo_dir=root))
        if language in {"ts", "tsx", "js"} and path is not None and root is not None
        else None
    )
    removed: list[Candidate] = []
    current = after
    original = _parse(before, language)
    budget = (
        len(_import_candidates(after, language, original, resolve))
        + len(_variable_candidates(after, language))
        + len(methods(after, language))
        + 1
    )
    for _ in range(budget):
        acted = False
        for candidate in (
            _variable_candidates(current, language)
            + methods(current, language)
            + _import_candidates(current, language, original, resolve)
        ):
            if candidate.kind == "private method":
                before_count = _method_references(before, candidate.name, language)
                current_count = _method_references(current, candidate.name, language)
                if not before_count or current_count != 0:
                    continue
                current = _delete_method(current, candidate)
                removed.append(candidate)
                acted = True
                break
            if candidate.kind == "import":
                before_count = _import_references(before, candidate, language)
                current_count = _import_references(current, candidate, language)
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
            if candidate.loads is not None and inert is not None and inert(candidate.loads):
                # The bare import would keep a load that does nothing.
                candidate = replace(candidate, replacement=None, loads=None)
            if (
                candidate.kind == "import"
                and candidate.replacement is None
                and _deletes_last_module_marker(current, candidate, language)
            ):
                candidate = replace(candidate, replacement=MODULE_MARKER)
            updated = _apply(current, candidate, language)
            if updated is None or updated == current:
                continue
            kept = _kept_require(current, candidate, language)
            current = updated
            removed.append(
                candidate if kept is None else replace(candidate, replacement=kept)
            )
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
