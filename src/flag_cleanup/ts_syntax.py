"""TypeScript syntax analysis backing the two structural safety gates.

The Piranha rules in ``rules/`` can only guard themselves with tree-sitter
queries, and 0.4.8's filter language cannot express "is this identifier bound
somewhere else" or "did my own rewrite break the file". Both of those are
trivial with a parser in hand, so the guarantees live here instead:

* :func:`transform_broke_syntax` backs **Gate 1** — after Piranha rewrites a
  file, the result is re-parsed and the transform is discarded if it broke the
  file. It is a strong net over one failure class, NOT a correctness proof: it
  asks "does this still parse as intended", and a wrong rewrite can pass that.
* :func:`const_path_is_safe` backs **Gate 2** — if the identifier bound to the
  flag read is bound *anywhere else in the file*, the const-propagation rules
  are withheld for that file. No scope analysis and no enumeration of
  initialiser forms: any second binder at all is disqualifying.

Why tree-sitter and not ``node --check``
---------------------------------------
``node --check`` parses JavaScript. Measured against 431 real TypeScript files
in this repo it rejected **286** of them — type annotations, generics,
``interface``, and JSX are all syntax errors to it. A gate with that false
positive rate silently produces no diffs forever and looks like "nothing to
clean up", which is worse than no gate. tree-sitter with the same grammar
Piranha itself uses rejected 4 of the 431, and those 4 are handled by the
differential design (see :func:`introduced_syntax_errors`). It is also a pure
wheel, so it adds nothing to the ``python:3.12-slim`` action image, needs no
network, and agrees with the rules by construction.

The catch, and why Gate 1 has FOUR checks
----------------------------------------
tree-sitter grammars are deliberately error-tolerant, so an ERROR-node count is
nowhere near sufficient on its own. Each of the other three checks was added
after a real escape — none of them moved the error count at all:

* the grammar does not enforce that a binding is a legal identifier, so
  ``for (const false of xs)``, ``catch (false)`` and ``function true() {}`` all
  parse clean (check 2);
* deleting a declaration out of ``export const on = <read>;`` left a bare
  ``export ``, which re-parses as an identifier expression statement (check 3);
* in semicolon-free code, deleting a statement can fuse its two neighbours into
  one — output that parses, type-checks, and silently drops a call (check 4).

The pattern worth keeping: every one of these is a DIFFERENTIAL structural
question asked of the whole file, not a list of forms to match. Lists leaked
twice on this branch; differentials have not.
"""

from __future__ import annotations

import difflib
from functools import lru_cache

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

# The SDK read functions a `const` may be bound to. Kept deliberately broader
# than the Piranha rules (any call arity, `let`/`var` too, optional chaining
# included): Gate 2 must never consider FEWER declarations than the rules can
# act on, and over-matching only costs a skipped cleanup.
_FLAG_READ_CALLEES = frozenset({"boolVariation", "useFeatureFlag"})

# Grammar FIELDS that introduce a binding. Field names are a small closed set in
# the grammar and mean "this is the thing being declared", which is why the gate
# keys off them rather than off node types: node types for *initialisers* are
# open-ended (`as_expression`, `satisfies_expression`, `non_null_expression`, …
# keep being added to TypeScript) and enumerating them is what leaked twice.
_BINDING_FIELDS = ("name", "pattern", "left", "parameter")

# …with one exception, because `left` is the one OVERLOADED field name in the
# set. It names the declared variable on `for_in_statement` and the assignment
# target on `assignment_expression`, but it is also just the first operand of a
# `binary_expression` — so `const on = <read>` followed by `if (on && other)`
# counted `on` twice and Gate 2 silently withheld the cleanup, leaving
# `const on = true;` with the `if` intact. The tell that this was a bug rather
# than caution: `other && on` was allowed while `on && other` was refused, and
# nothing sane depends on which side of `&&` the flag sits.
#
# A DENY-list on purpose, and this is the one place in this module where that is
# the safe shape. The asymmetry that makes it so: over-collecting a binding only
# costs a skipped cleanup, while under-collecting one propagates a literal past
# a real re-binding and ships code that compiles and is wrong. So a grammar
# upgrade adding a new `left`-bearing node fails CLOSED here — it is treated as
# a binder until someone proves otherwise — and it is ADDING to this set that
# needs proof, not omitting from it. Measured over 470 real `.ts`/`.tsx` files
# in this repo, the only node types carrying a `left` field are
# `binary_expression` (2177), `assignment_expression` (405), `for_in_statement`
# (63), `object_assignment_pattern` (37), `augmented_assignment_expression`
# (19), `assignment_pattern` (9) and `conditional_type` (3); every one but the
# first is either a genuine binder or a mutation of one, and both of those are
# exactly what Gate 2 must see.
_NON_BINDING_LEFT_NODES = frozenset({"binary_expression"})

# Nodes that destructure; identifiers underneath them are bindings, and the
# shorthand form (`const { on } = cfg`) has no field at all.
_PATTERN_NODES = frozenset(
    {
        "array_pattern",
        "object_pattern",
        "object_assignment_pattern",
        "assignment_pattern",
        "pair_pattern",
        "rest_pattern",
    }
)
# `type_identifier` is here because `class on {}` / `class on extends …` name
# their binding with that node type, not `identifier`. It also picks up
# `type X = …` and `interface X`, which are type-space rather than value
# bindings — collecting them only makes the gate more conservative, which is the
# safe direction.
_BINDING_LEAVES = frozenset(
    {"identifier", "shorthand_property_identifier_pattern", "type_identifier"}
)

# Words that can never legally name a binding. Deliberately excludes TypeScript's
# CONTEXTUAL keywords (`let`, `of`, `as`, `from`, `type`, `async`, `await`,
# `yield`, `static`, `get`, `set`, `namespace`, `declare`, …) — those are legal
# identifiers, and flagging them would be a false positive. `true`/`false` are
# the two this tool can actually emit; the rest are defence in depth.
_RESERVED_WORDS = frozenset(
    {
        "true", "false", "null", "this", "super", "void", "typeof", "instanceof",
        "in", "new", "delete", "return", "if", "else", "for", "while", "do",
        "break", "continue", "function", "class", "const", "var", "import",
        "export", "default", "extends", "switch", "case", "try", "catch",
        "finally", "throw", "with", "debugger", "enum",
    }
)


@lru_cache(maxsize=None)
def _parser(language: str) -> Parser:
    """Return a cached parser for ``"ts"``, ``"tsx"`` or ``"js"``.

    ``js`` maps to the TSX grammar deliberately, and for the same reason the
    ENGINE does (piranha#25 points its `javascript` arm at `LANGUAGE_TSX`):
    Gate 1 must parse with the same grammar the engine rewrote with, or its
    checks silently stop matching. The TSX grammar is a superset of
    JavaScript's, so nothing valid is rejected — and JSX in a `.js` file, which
    the plain JavaScript grammar would refuse, is ordinary in React codebases.
    """
    try:
        grammar = {
            "ts": tree_sitter_typescript.language_typescript,
            "tsx": tree_sitter_typescript.language_tsx,
            "js": tree_sitter_typescript.language_tsx,
        }[language]
    except KeyError:
        raise ValueError(f"unsupported language {language!r}") from None
    return Parser(Language(grammar()))


def _walk(node: Node):
    """Yield every node in the tree, depth first."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _parse(source: str, language: str) -> Node:
    return _parser(language).parse(source.encode("utf-8")).root_node


def count_syntax_errors(source: str, language: str) -> int:
    """Return the number of ERROR / MISSING nodes in ``source``."""
    return sum(
        1 for node in _walk(_parse(source, language)) if node.type == "ERROR" or node.is_missing
    )


def strip_dangling_type_separators(source: str, language: str) -> str:
    """Remove a ``;`` a member deletion left leading or doubled in an object type.

    The engine's separator handling knows ``,`` and not ``;``. Deleting the
    first member of ``{ 'k'?: boolean; 'a'?: boolean }`` leaves
    ``{ ; 'a'?: boolean }``, which tree-sitter parses clean — invisible to
    Gate 1 and TS1131 to ``tsc`` — so it has to be removed here. Deleting a
    middle or last member would leave ``; ;``, an ERROR node, but the
    engine's own per-edit syntax check panics on that before it reaches this
    pass, so in practice the leading ``;`` is the only shape this sees; the
    algorithm is written for any run of stray ``;`` tokens anyway, because
    that is no harder and a later engine may hand it one. A trailing ``;``
    is valid and is left alone.

    A ``(comment)`` ahead of the first member (``rules/ts_entries.toml``'s
    ``_after_comment`` rules match it) is transparent here: it does not count
    as "something real" for deciding whether a ``;`` behind it is leading, so
    ``{ // note\n ; 'a'?: boolean }`` is cleaned exactly like the
    comment-free shape.

    Not differential on purpose: neither shape can exist in code that
    compiled before this transform ran, so anything found here is ours.
    """
    data = source.encode("utf-8")
    cuts: list[tuple[int, int]] = []
    for node in _walk(_parse(source, language)):
        if node.type != "object_type":
            continue
        previous_kind = None
        for child in node.children:
            if child.type == "comment":
                # A comment is a NAMED child too (the `rules/dart.toml`
                # finding for argument lists, holding here as well — see
                # `rules/ts_entries.toml`'s first-member rules), so it must
                # not read as "something real sat here": a `;` left dangling
                # right after one or more comments is still LEADING, exactly
                # as if `{` were its immediate predecessor.
                continue
            if child.type == "ERROR" and set(child.text) <= set(b"; \t\r\n"):
                # A run of stray `;` tokens the grammar couldn't attach
                # anywhere else merges into one ERROR node; unpack its `;`
                # leaves, in document order, as separate tokens.
                tokens = [
                    (";", leaf.start_byte, leaf.end_byte)
                    for leaf in sorted(
                        (n for n in _walk(child) if n.type == ";"),
                        key=lambda n: n.start_byte,
                    )
                ]
            else:
                tokens = [(child.type, child.start_byte, child.end_byte)]
            for kind, start, end in tokens:
                if kind == ";" and previous_kind in ("{", ";"):
                    while end < len(data) and data[end:end + 1] in (b" ", b"\t"):
                        end += 1
                    cuts.append((start, end))
                previous_kind = kind
    for start, end in sorted(cuts, reverse=True):
        data = data[:start] + data[end:]
    return data.decode("utf-8")


def introduced_syntax_errors(before: str, after: str, language: str) -> bool:
    """Whether rewriting ``before`` into ``after`` ADDED syntax errors.

    Differential rather than absolute, which is what makes the gate usable: the
    tree-sitter TypeScript grammar has a handful of genuine gaps (a bare ``&``
    in JSX text, ``typeof import("…")`` in a type position) that make perfectly
    valid files parse with an ERROR node. Judging the output on its own would
    permanently refuse to clean those files up. Comparing against the input's
    own error count means a pre-existing quirk is carried through and only a
    NEW breakage is rejected.
    """
    return count_syntax_errors(after, language) > count_syntax_errors(before, language)


def _reserved_bindings(source: str, language: str) -> int:
    """How many binding positions in ``source`` hold a reserved word."""
    source_bytes = source.encode("utf-8")
    return sum(
        1
        for name in _binding_names(_parse(source, language), source_bytes)
        if name in _RESERVED_WORDS
    )


def _reserved_word_identifiers(source: str, language: str) -> int:
    """How many ``identifier`` nodes in ``source`` are actually keywords.

    A keyword can only ever surface as an ``identifier`` when the parse has
    degraded — in valid source, ``export`` is part of an ``export_statement``,
    ``false`` is a ``false`` node, and so on. So this counts symptoms of a
    broken parse that the grammar was too permissive to call an error.
    """
    source_bytes = source.encode("utf-8")
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.type == "identifier"
        and source_bytes[node.start_byte : node.end_byte].decode("utf-8") in _RESERVED_WORDS
    )


#: Node types that can hold TWO ADJACENT statements, which is what an ASI fusion
#: needs — the survivors have to be siblings in one container to merge. That
#: makes this a closed set derived from an argument rather than a list of forms
#: to keep up with: a braceless `if`/`for`/`while` body holds exactly one
#: statement so nothing can fuse inside it, and a `class_body` holds members,
#: not statements. Confirmed empirically over 470 real `.ts`/`.tsx` files in this
#: repo — `statement_block` (3456), `program` (466) and `switch_case` (4) are the
#: only holders that occur; `switch_default` is the same shape as `switch_case`
#: and is included for the case the corpus happened not to contain.
#:
#: `switch_case` was missing, and its absence was not theoretical: statements in
#: a `case:` body are direct children of `switch_case`, NOT of a
#: `statement_block`, so the counter saw one statement either side of the probe
#: and reported no fusion. Semicolon-free code in a `case:` therefore shipped
#: `runLegacyImporter()` and `(run)()` merged into `runLegacyImporter()(run)()`
#: — parses, type-checks, silently drops the call — to a ready-for-review PR.
_STATEMENT_CONTAINERS = frozenset(
    {"program", "statement_block", "switch_case", "switch_default"}
)


def _statement_count(source: str, language: str) -> int:
    """How many statements ``source`` contains, at every block depth."""
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.is_named
        and node.parent is not None
        and node.parent.type in _STATEMENT_CONTAINERS
        and node.type not in ("empty_statement", "comment")
    )


def _asi_merged_statements(before: str, after: str, language: str) -> bool:
    """Whether removing code let two surviving statements silently merge.

    In semicolon-free TypeScript (Prettier ``semi: false``, StandardJS) a
    statement is terminated by Automatic Semicolon Insertion, which only fires
    when the next line *cannot* continue the expression. Delete the statement
    between two such lines and the survivors can fuse into one::

        const run = pickRunner()
        if (<flag read>) { runLegacyImporter() }
        (run)()

    becomes ``const run = pickRunner()(run)()`` — the invocation never happens,
    it still parses, it still type-checks, and the diff shows only the `if`
    being removed. Nothing about the surviving lines appears in the diff at all.

    The hazard is *concentrated* exactly where this tool cuts: the defensive
    leading ``;`` those style guides mandate is absent precisely because the
    deleted statement was shielding that line.

    Rather than enumerate the tokens that can continue an expression
    (``( [ ` + - /``, and whatever a future TypeScript adds), this asks the
    question directly: at each point where content was removed, insert a ``;``
    at the junction and re-parse. If that yields MORE statements than the
    unmodified output, the two neighbours had been fused — the semicolon
    separated something that was joined. A probe that instead introduces parse
    errors means the line legitimately continues (a multi-line call, say), so
    it proves nothing and is skipped.

    The ``;`` goes in as its OWN LINE rather than being appended to the
    preceding one. Appending is what a first version did, and a single ``//``
    comment at the junction silently defeated it: the ``;`` landed inside the
    comment, the probe became a no-op, and the fusion was reported as absent —
    shipping a wrong rewrite to a ready-for-review pull request. A whole-line
    comment above the deleted statement is the ordinary case, and the very
    comment a linter suggests for this hazard
    (``// eslint-disable-next-line no-unexpected-multiline``) was itself enough
    to disable the check. A ``/** … */`` block was unaffected, which is exactly
    why it looked covered.

    Still not airtight: a junction landing inside an unterminated block comment
    or a multi-line template literal would put the ``;`` somewhere inert. Those
    probes are expected to either change nothing or add parse errors (and so be
    skipped), not to report a false negative — but this is a net, not a proof.
    """
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    baseline_statements = _statement_count(after, language)
    baseline_errors = count_syntax_errors(after, language)

    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # Both edges of a replaced range are junctions; a pure deletion collapses
        # to one. Line j-1 is the surviving line that precedes the junction.
        for junction in {j1, j2}:
            if junction <= 0 or junction > len(after_lines):
                continue
            probe = "".join(
                after_lines[:junction] + [";\n"] + after_lines[junction:]
            )
            if count_syntax_errors(probe, language) > baseline_errors:
                continue
            if _statement_count(probe, language) > baseline_statements:
                return True
    return False


def transform_broke_syntax(before: str, after: str, language: str) -> bool:
    """Whether rewriting ``before`` into ``after`` produced invalid source.

    Gate 1. Every check is differential, so a file that was already unusual
    stays eligible and only a NEW breakage is rejected:

    1. more ERROR / MISSING nodes than the input had — catches `switch true`,
       `true = recompute();`, dropped delimiters;
    2. more reserved words in binding positions — catches
       `for (const false of xs)`, `catch (false)`, `function true() {}`;
    3. more keywords parsed as bare ``identifier`` nodes — catches a deletion
       that strips a statement out from under its keyword, leaving that keyword
       stranded as an expression;
    4. two surviving statements fused by Automatic Semicolon Insertion — see
       :func:`_asi_merged_statements`.

    Checks 2, 3 and 4 exist because the ERROR-node count is not a sufficient
    signal: tree-sitter grammars are deliberately error-tolerant, so a
    surprising amount of broken output still parses clean. Deleting the
    declaration out of `export const on = <read>;` left a bare `export `, which
    re-parses as `(program (expression_statement (identifier)))` — no ERROR, no
    MISSING, nothing in a binding position. Check 3 is the general form of that
    failure: any deletion that strands a keyword where an expression can be read
    shows up as a keyword-shaped identifier.

    Check 4 is the one that does not fit the "broken parse" framing at all: an
    ASI merge produces output that parses, type-checks, and is wrong. It is here
    because it is caused by, and detectable from, the same edit.

    None of this makes bad output impossible — the first three checks ask
    whether the result still parses as intended, which a wrong rewrite can pass.
    Gate 2 and check 4 each close one specific parses-but-wrong case; a new one
    needs its own check.
    """
    return (
        introduced_syntax_errors(before, after, language)
        or _reserved_bindings(after, language) > _reserved_bindings(before, language)
        or _reserved_word_identifiers(after, language)
        > _reserved_word_identifiers(before, language)
        or _asi_merged_statements(before, after, language)
    )


def binding_nodes(root: Node) -> list[Node]:
    """Every identifier NODE introduced as a binding anywhere under ``root``.

    The node-level form is the primary one because two callers need different
    things from it: the read-const gate below counts names, while
    :mod:`flag_cleanup.key_const` needs to tell a binding OCCURRENCE of a name
    from a reference to it, which only node identity settles. Deriving both
    from one traversal is deliberate -- re-deriving this field logic elsewhere
    is exactly what leaked twice (see ``_BINDING_FIELDS`` above).
    """
    found: list[Node] = []

    def _collect(node: Node) -> None:
        """Add the identifiers a binder's target introduces (patterns recurse)."""
        if node.type in _BINDING_LEAVES:
            found.append(node)
        elif node.type in _PATTERN_NODES:
            found.extend(c for c in _walk(node) if c.type in _BINDING_LEAVES)

    for node in _walk(root):
        for field in _BINDING_FIELDS:
            if field == "left" and node.type in _NON_BINDING_LEFT_NODES:
                continue
            target = node.child_by_field_name(field)
            if target is not None:
                _collect(target)
    return found


def _binding_names(root: Node, source_bytes: bytes) -> list[str]:
    """Every identifier introduced as a binding anywhere under ``root``."""
    return [
        source_bytes[node.start_byte : node.end_byte].decode("utf-8")
        for node in binding_nodes(root)
    ]


def _flag_read_consts(source: str, language: str, flag_key: str) -> list[tuple[str, bool]]:
    """``(bound name, read is an optional call)`` for every read of ``flag_key``."""
    source_bytes = source.encode("utf-8")

    def text(node: Node) -> str:
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8")

    found: list[tuple[str, bool]] = []
    for node in _walk(_parse(source, language)):
        if node.type != "variable_declarator":
            continue
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or name.type != "identifier" or value is None:
            continue
        if value.type != "call_expression":
            continue
        callee = value.child_by_field_name("function")
        if callee is None:
            continue
        if callee.type == "member_expression":
            callee = callee.child_by_field_name("property")
        if callee is None or text(callee) not in _FLAG_READ_CALLEES:
            continue
        arguments = value.child_by_field_name("arguments")
        first = arguments.named_child(0) if arguments is not None else None
        if first is None or first.type != "string":
            continue
        # `"key"` -> the string_fragment child, or empty for `""`.
        fragment = first.named_child(0)
        if fragment is None or text(fragment) != flag_key:
            continue
        # `client.boolVariation?.(…)`: the call's `?.` is an anonymous child,
        # not a field, in either grammar.
        optional_call = any(child.type == "?." for child in value.children)
        found.append((text(name), optional_call))
    return found


def const_path_is_safe(source: str, language: str, flag_key: str) -> bool:
    """Whether the const-propagation rules may run over ``source``.

    They may only when every name bound to a read of ``flag_key`` is bound
    exactly once in the whole file. A second binder — a nested re-declaration,
    a loop or `catch` variable, a parameter, a named function expression, a
    destructured property, an import — means propagating the literal could
    replace a reference that resolves to the *other* binding, which produces
    code that compiles and is wrong. Refusing costs one uncleaned
    ``const on = true;`` and is always safe.

    It also refuses when the read is an OPTIONAL call
    (``const on = client.boolVariation?.("KEY", …)``). That expression is
    ``undefined`` when the callee is missing, so folding the binding away is a
    behaviour change. `rules/ts.toml` excludes the same shape from the generic
    rule, but the const rules cannot: Piranha's grammar does not expose the
    call's `?.` anywhere a query's `value:` pattern can reach.

    Files with no flag-read const at all are trivially safe: there is nothing
    for the const rules to act on.
    """
    root = _parse(source, language)
    reads = _flag_read_consts(source, language, flag_key)
    if not reads:
        return True
    if any(optional_call for _, optional_call in reads):
        return False
    bindings = _binding_names(root, source.encode("utf-8"))
    return all(bindings.count(name) == 1 for name, _ in reads)
