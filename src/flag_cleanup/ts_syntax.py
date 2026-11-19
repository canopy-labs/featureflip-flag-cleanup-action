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

import bisect
import difflib
import re
from collections import Counter
from functools import lru_cache

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

# The SDK read functions a `const` may be bound to. Kept deliberately broader
# than the Piranha rules (any call arity, `let`/`var` too, optional chaining
# included): Gate 2 must never consider FEWER declarations than the rules can
# act on, and over-matching only costs a skipped cleanup.
_FLAG_READ_CALLEES = frozenset({"boolVariation", "useFeatureFlag", "useBooleanFlagValue"})

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


#: The `not_contains` filters the key-constant rule templates carry, by the
#: name of what they decline. The Gate 2 predicates (`key_const._ts_is_removable_read`,
#: `cross_file_ts._is_read`) must decline exactly these, so a reference counts
#: as a removable read only when a rule will fold it; a test parses the TOML and
#: compares it with this table.
CONST_RULE_FILTERS = {
    "optional_call": '((call_expression "?." arguments: (arguments)) @optional_call)',
    "spread": "((spread_element) @spread)",
    "comment": "((comment) @comment)",
}


def declined_by_const_rules(call: Node, *, positioned: bool) -> bool:
    """Whether the key-constant rules decline ``call``: an optional call
    anywhere within it, and for a positioned accessor a spread or a comment too."""
    wanted = {"call_expression"} | ({"spread_element", "comment"} if positioned else set())
    for node in _walk(call):
        if node.type not in wanted:
            continue
        if node.type != "call_expression" or any(c.type == "?." for c in node.children):
            return True
    return False


#: The object-literal argument of a property-keyed read (``getFlag{key}``) is
#: admitted by a WHITELIST over its text, not a list of what is forbidden: the
#: call is replaced whole, so every other property is dropped with it and must be
#: able to do nothing. The rules (`ts_key_positions.toml` and its two siblings)
#: carry the same four regular expressions as `#match?` / `#not-match?`
#: predicates, and `tests/test_ts_object_key_accessors.py` compares the text and
#: runs both over the same generated objects.
#:
#: 1. ``OBJECT_TOKENS``: the text is only whitespace, ``{ } [ ] , :``, words
#:    (identifiers, keywords, integers) and plain quoted strings. No ``.``, ``(``,
#:    ``?``, operator, backtick, ``/`` (comment or regex), ``...`` or escape can
#:    occur, so no member access, call, spread, unary, binary, template or arrow.
#:    The one dotted token allowed is the key's own value (``Keys.X``), passed in.
#: 2. ``OBJECT_ADJACENT`` and ``OBJECT_INDEXED`` must not match: two words in a row are ``new X``,
#:    ``typeof x``, ``await x``, ``void 0``, ``delete x``.
#: 3. ``OBJECT_KEYWORDS`` must not match: a lone ``this``, ``class {}``, ``__proto__``.
#: 4. ``object_duplicate_key(prop)`` must not match: two properties named ``prop``
#:    (identifier, string, shorthand, method or getter), of which the last wins.
_WORD = "[A-Za-z0-9_$]"
_WS = "[ \t\r\n]"
OBJECT_ADJACENT = f"{_WORD}{_WS}+{_WORD}"
#: A ``[`` right after a word, a quote or a ``]`` (blank space and newlines
#: between included) is an index read, ``o[0]``, ``o['a']``, ``a\n[0]``; an array
#: literal follows ``:``, ``,``, ``[`` or ``{`` instead.
OBJECT_INDEXED = r"[A-Za-z0-9_$'\"\]][ \t\r\n]*\["
OBJECT_KEYWORDS = (
    r"(?:^|[ \t\r\n{,:\[])(?:this|super|class|function|async|import|await|yield|new)(?:[ \t\r\n{},:\]]|$)"
    "|__proto__"
)


def regex_safe(text: str) -> str:
    """``text`` as regex source with no backslash: ``$`` and ``.`` as one-char classes."""
    return text.replace("$", "[$]").replace(".", "[.]")


_regex_safe = regex_safe


def object_tokens(dotted: str | None = None) -> str:
    """The whole-text whitelist.

    ``dotted`` (``Keys.X``) is the one dotted word allowed outside a string. A
    string may hold ``.`` (a flag key such as ``'my.flag'``, which the rule cannot
    exempt on its own: the key hole may sit only in an ``#eq?`` string), but
    nothing that could end it early or escape: no quote, backslash or newline.
    """
    quoted = "'[A-Za-z0-9_$./:@#%+=-]*'"
    double = '"[A-Za-z0-9_$./:@#%+=-]*"'
    extra = r"|[ \t\r\n,:\[]" + _regex_safe(dotted) + r"[ \t\r\n,}\]]" if dotted else ""
    return rf"^(?:[ \t\r\n]|[{{}},:\[\]]|{_WORD}+|{quoted}|{double}{extra})*$"


def object_duplicate_key(prop: str) -> str:
    """Two properties of the object named ``prop`` (the last one wins)."""
    name = _regex_safe(prop)
    s = f"(?:{name}|'{name}'|\"{name}\")"
    w = _WS + "*"
    return f"(?s)[{{,]{w}{s}{w}(?:[:}}].*[{{,]|,(?:.*[{{,])?){w}{s}{w}[:,}}]"


def _object_regexes(prop: str, dotted: str | None) -> tuple[re.Pattern, ...]:
    return (
        re.compile(object_tokens(dotted)),
        re.compile(OBJECT_ADJACENT),
        re.compile(OBJECT_INDEXED),
        re.compile(OBJECT_KEYWORDS),
        re.compile(object_duplicate_key(prop)),
    )


def read_slot(argument: Node) -> tuple[Node, int | str] | None:
    """``(call, slot)`` for the node that could hold a read's key, else ``None``.

    The slot is the argument's index when ``argument`` is one, and for a value
    of a ``name: value`` property of an object literal that is the FIRST
    argument, the property's name.
    """
    parent = argument.parent
    if parent is None:
        return None
    if parent.type == "arguments":
        index = next((i for i, c in enumerate(parent.named_children) if c.id == argument.id), None)
        return None if index is None or parent.parent is None else (parent.parent, index)
    if parent.type != "pair" or parent.child_by_field_name("value") is None:
        return None
    key = parent.child_by_field_name("key")
    value = parent.child_by_field_name("value")
    obj = parent.parent
    if key is None or key.type != "property_identifier" or value.id != argument.id:
        return None
    if obj is None or obj.type != "object" or obj.parent is None or obj.parent.type != "arguments":
        return None
    arguments = obj.parent
    if not arguments.named_children or arguments.named_children[0].id != obj.id or arguments.parent is None:
        return None
    return arguments.parent, key.text.decode("utf-8")


def declined_by_object_key_rules(call: Node, prop: str = "key", *, awaited: bool = True) -> bool:
    """Whether the property-key rules decline ``call``.

    They match the ``await`` around it only, with the object its only argument,
    and decline an optional call, a spread, a comment or a computed name
    anywhere in it, and any object whose text is not admitted by the whitelist
    above (see the regular expressions' comment). ``awaited=False`` asks the
    question of an un-awaited read, which ``ts_async_reads`` decides per site
    and the sentinel pass folds: everything but the ``await`` requirement.
    """
    wrapper = call.parent
    if awaited and (wrapper is None or wrapper.type != "await_expression"):
        return True
    if not awaited:
        wrapper = call
    if declined_by_const_rules(call, positioned=True):
        return True
    arguments = call.child_by_field_name("arguments")
    obj = arguments.named_children[0] if arguments is not None and len(arguments.named_children) == 1 else None
    if obj is None or obj.type != "object":
        return True
    if any(node.type == "computed_property_name" for node in _walk(wrapper)):
        return True
    dotted = None
    for pair in obj.named_children:
        if pair.type != "pair":
            continue
        name, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
        if name is not None and value is not None and name.text.decode("utf-8") == prop and value.type == "member_expression":
            dotted = value.text.decode("utf-8")
    tokens, adjacent, indexed, keywords, duplicate = _object_regexes(prop, dotted)
    text = obj.text.decode("utf-8")
    return not tokens.search(text) or any(
        r.search(text) for r in (adjacent, indexed, keywords, duplicate)
    )


def _walk(node: Node):
    """Yield every node in the tree, depth first."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def chain_is_optional(callee: Node | None) -> bool:
    """Whether ANY link of ``callee`` is optional (``svc?.a.useFlag``,
    ``svc.a?.b.read``, ``svc?.["a"].read``).

    A nullish link makes the whole call ``undefined`` without running it, so
    folding it to a boolean changes the value. The rules' ``!optional_chain``
    sees only the last link; this looks at every one (and over-refuses a ``?.``
    nested inside the callee's own sub-expressions, the safe direction).
    """
    return callee is not None and any(n.type in ("optional_chain", "?.") for n in _walk(callee))


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
    first member of ``{ 'k'?: boolean; 'a'?: boolean }`` used to leave
    ``{ ; 'a'?: boolean }``, which tree-sitter parses clean — invisible to
    Gate 1 and TS1131 to ``tsc`` — so it had to be removed here. A trailing
    ``;`` is valid and is left alone.

    **Nothing produces that shape any more**, and this pass is kept as
    insurance rather than as a step anything depends on. Since #3049
    ``rules/ts_entries.toml`` rewrites the ``;`` after the doomed member to a
    ``,`` in a separate edit FIRST, so the engine deletes a comma-separated
    member and swallows its own separator; measured, this function changes
    nothing on the entry fixtures under the current rules and changes
    ``{ ; 'a'?: boolean }`` under the previous ones. The algorithm is written
    for any run of stray ``;`` tokens because that is no harder than the one
    shape, and a later engine or rule may hand it one.

    A ``(comment)`` ahead of the first member is transparent here: it does not
    count as "something real" for deciding whether a ``;`` behind it is
    leading, so ``{ // note\n ; 'a'?: boolean }`` is cleaned exactly like the
    comment-free shape. (``rules/ts_entries.toml`` used to carry
    ``_after_comment`` rule variants for that shape; they went with the
    first-member anchor they existed to work around.)

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

    from flag_cleanup.syntax import _line_opcodes, _SegmentTooLarge

    try:
        # Anchored on the lines unique to both sides: a bare `SequenceMatcher`
        # is quadratic on a big file of repeated braces.
        opcodes = _line_opcodes(before_lines, after_lines)
    except _SegmentTooLarge:
        opcodes = difflib.SequenceMatcher(
            None, before_lines, after_lines, autojunk=False
        ).get_opcodes()
    junctions: set[int] = set()
    for tag, _i1, _i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        # Both edges of a replaced range are junctions; a pure deletion collapses
        # to one. Line j-1 is the surviving line that precedes the junction.
        for junction in (j1, j2):
            if 0 < junction <= len(after_lines):
                junctions.add(junction)

    # One probe per junction re-parsed the whole file each time: quadratic in
    # the number of changed regions. Probe them TOGETHER and bisect only when
    # the batch adds parse errors (a junction inside a multi-line construct,
    # which on its own proves nothing and is skipped). A `;` on its own line
    # is an `empty_statement`, never counted, so the statement count rises only
    # where a `;` separated two fused statements.
    def probe_finds_fusion(group: list[int]) -> bool:
        pieces: list[str] = []
        previous = 0
        for junction in group:
            pieces.extend(after_lines[previous:junction])
            pieces.append(";\n")
            previous = junction
        pieces.extend(after_lines[previous:])
        probe = "".join(pieces)
        if count_syntax_errors(probe, language) > baseline_errors:
            if len(group) == 1:
                return False
            middle = len(group) // 2
            return probe_finds_fusion(group[:middle]) or probe_finds_fusion(group[middle:])
        return _statement_count(probe, language) > baseline_statements

    return bool(junctions) and probe_finds_fusion(sorted(junctions))


#: The text an expression statement may not start with, because in statement
#: position JavaScript reads that token as the start of something else: a
#: `function`/`async function`/`class` declaration, a `{` block, a `let [`
#: destructuring declaration (ECMAScript's ExpressionStatement lookahead
#: restriction). `function () {…}.bind(null);` is a SyntaxError to `node` and
#: `tsc`, yet tree-sitter's grammars parse it as an `expression_statement` with no
#: ERROR node, so the first four checks pass it. A rewrite that turns a
#: declaration's value into a statement (a discarded initializer) is how one
#: arrives. `async` and `function` must share a line: across a line break
#: `async` is an expression statement of its own.
RESTRICTED_STATEMENT_START = re.compile(
    r"(?:function\b|class\b|\{|async[ \t]+function\b|let[ \t\r\n]*\[)"
)


def _restricted_statements(source: str, language: str) -> int:
    """How many expression statements in ``source`` start with a token that
    statement position reads as a declaration or a block."""
    source_bytes = source.encode("utf-8")
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.type == "expression_statement"
        and RESTRICTED_STATEMENT_START.match(
            source_bytes[node.start_byte : node.end_byte].decode("utf-8", "replace")
        )
    )


#: The kinds whose `body` block starts with a directive prologue. A script
#: (`program`) has one too; a class static block, a `catch` or a plain block
#: does not.
_FUNCTION_KINDS = frozenset(
    {
        "function_declaration",
        "function_expression",
        "generator_function",
        "generator_function_declaration",
        "arrow_function",
        "method_definition",
    }
)

#: A body's children that are not statements and do not end its prologue.
_PROLOGUE_TRANSPARENT = frozenset({"comment", "html_comment", "hash_bang_line"})


def _prologue_bodies(root: Node):
    for node in _walk(root):
        if node.type == "program":
            yield node
        elif (
            node.type == "statement_block"
            and node.parent is not None
            and node.parent.type in _FUNCTION_KINDS
            and (body := node.parent.child_by_field_name("body")) is not None
            and body.start_byte == node.start_byte
            and body.end_byte == node.end_byte
        ):
            yield node


def _is_directive_shape(statement: Node) -> bool:
    if statement.type != "expression_statement":
        return False
    named = [c for c in statement.named_children if c.type not in _PROLOGUE_TRANSPARENT]
    return len(named) == 1 and named[0].type == "string"


def _directives(source: str, language: str) -> list[tuple[bool, int, int, int, int, bytes]]:
    """Every directive in ``source``: the bare strings each function body and
    the script start with, as ``(whether the body is the script, header row,
    row, column past the line's indent, body's last row, text)``.

    The header row is where the body's owner starts (the `function`, the
    arrow's parameters, the method's name), or 0 for the script. The column is
    counted from the line's first non-blank byte, so a directive the fold
    re-indented with its block is still the same directive.
    """
    data = source.encode("utf-8")
    last_row = source.count("\n")
    found: list[tuple[bool, int, int, int, int, bytes]] = []
    for body in _prologue_bodies(_parse(source, language)):
        if body.type == "program":
            header, end = 0, last_row
        else:
            header, end = body.parent.start_point[0], body.end_point[0]
        for child in body.named_children:
            if child.type in _PROLOGUE_TRANSPARENT:
                continue
            if not _is_directive_shape(child):
                break
            line_start = data.rfind(b"\n", 0, child.start_byte) + 1
            indent = len(data[line_start : child.start_byte]) - len(
                data[line_start : child.start_byte].lstrip(b" \t")
            )
            found.append(
                (
                    body.type == "program",
                    header,
                    child.start_point[0],
                    child.start_point[1] - indent,
                    end,
                    data[child.start_byte : child.end_byte],
                )
            )
    return found


#: Why a rewrite that changed a directive prologue is refused, by rule.
_ADDED_STRING = (
    "the rewrite would add a string statement (a conditional folded to one of "
    'its strings), which first in a function becomes a directive such as "use strict"'
)
_NEW_DIRECTIVE = (
    "a string statement after code would become the first statement of a function "
    'or the file, where JavaScript reads it as a directive such as "use strict"'
)
_LOST_DIRECTIVE = (
    'a directive such as "use strict" would no longer be the first statement '
    "of its function or file"
)
_DIRECTIVES_UNCHECKED = (
    "the file holds a directive such as \"use strict\", and it is too long and "
    "repetitive for the check that the rewrite left its directives as they were"
)


def _bare_strings(source: str, language: str) -> tuple[Counter, set[bytes], set[bytes]]:
    """Every string statement in ``source`` (an expression statement that is one
    string literal, in any body or block), as the multiset of their texts, and
    the texts of those that are directives and of those that are not."""
    data = source.encode("utf-8")
    root = _parse(source, language)
    directive_starts = set()
    for body in _prologue_bodies(root):
        for child in body.named_children:
            if child.type in _PROLOGUE_TRANSPARENT:
                continue
            if not _is_directive_shape(child):
                break
            directive_starts.add(child.start_byte)
    every: Counter = Counter()
    directives: set[bytes] = set()
    others: set[bytes] = set()
    for node in _walk(root):
        if not _is_directive_shape(node):
            continue
        literal = next(c for c in node.named_children if c.type == "string")
        text = data[literal.start_byte : literal.end_byte]
        every[text] += 1
        (directives if node.start_byte in directive_starts else others).add(text)
    return every, directives, others


_STRING_OPERAND_FOLDED = (
    "a discarded conditional or logical statement would be folded to one of its "
    'strings, which first in a function becomes a directive such as "use strict"'
)


def _string_operands(source: str, language: str) -> Counter:
    """The texts of every string literal inside an expression statement that
    is not itself one string literal, at any depth, as a multiset: the only
    code a fold can make a NEW string statement of.

    Conservative on purpose. Direct operands of a ternary or `&&`/`||` were
    not enough (review: `<read> || (<read> || 'use strict');` folds to the
    nested operand), and listing the shapes a fold can reach through
    (`??`, a sequence, parentheses, a nested ternary) is the patching this
    check exists to stop. A string in a call's arguments is counted too; it
    only matters when its text vanishes AND a string statement of that text
    stays."""
    data = source.encode("utf-8")
    found: Counter = Counter()
    for node in _walk(_parse(source, language)):
        if node.type != "expression_statement" or _is_directive_shape(node):
            continue
        for inner in _walk(node):
            if inner.type == "string":
                found[data[inner.start_byte : inner.end_byte]] += 1
    return found


def _directive_change(before: str, after: str, language: str) -> str | None:
    """Why the rewrite changed a DIRECTIVE PROLOGUE, or ``None``.

    The string statements a function body or a script starts with are its
    directive prologue: `"use strict"` makes the function strict (`this` in a
    plain call becomes `undefined`), and `"use client"`/`"use server"` mean
    something to a bundler. Delete the statements ahead of a string statement,
    splice it out of a block, or fold a discarded `c ? "a" : "b";` into one, and
    an ordinary statement becomes a directive; put code ahead of a directive and
    it stops being one. It parses, and nothing else here can see it.

    Three rules that pair nothing, because two line-diff pairings were fooled
    in review (a deleted twin holding the same `'use strict'` line, and the
    common tail beside it):

    1. the output's string statements, as a multiset of texts, are contained in
       the input's: no rewrite needs to ADD one, so a fold that writes one is
       refused;
    2. no text that is a string statement but not a directive in the input is a
       directive in the output: that string is the only other raw material a
       new directive can be made of;
    3. no text that is a directive in the input is a string statement but not a
       directive in the output: a demoted directive leaves exactly that.

    Rule 1 counts texts, so a fold writing a string statement of text T while
    a deletion removes another statement of text T elsewhere passes it (review:
    a deleted `if (<read>) { run(function () { 'use strict'; … }) }` ahead of
    `<read> ? 'x' : 'use strict';`). So a fourth: a string statement can only be
    CREATED from a string operand of a discarded conditional or logical
    statement (at any depth inside it), and when such a string vanished from
    the input, no string statement of its text may remain in the output.

    Over-refuses a file holding the same text both as a directive and as an
    ordinary string statement (legacy `var x; 'use strict';` beside a strict
    function), and a fold of a string-valued discarded conditional. What they
    do not see: a directive deleted while its function stays. The second
    is what the line-diff net below is for, which no pass does and the engine
    guards: an input directive must be kept (every line from its body's header
    through its own line in one unchanged block, the same directive on the
    other side) unless both its header and its own line were deleted with the
    code around them. The diff is :func:`flag_cleanup.syntax._line_opcodes`,
    bounded; a file it cannot diff in time is refused, never passed.
    """
    before_every, before_directives, before_others = _bare_strings(before, language)
    after_every, after_directives, after_others = _bare_strings(after, language)
    if after_every - before_every:
        return _ADDED_STRING
    # A string operand that left its discarded statement may have become a
    # string statement whose text another one, deleted, had: the multiset above
    # then counts the two as one. Any string statement of that text refuses.
    vanished = _string_operands(before, language) - _string_operands(after, language)
    if any(text in vanished for text in after_every):
        return _STRING_OPERAND_FOLDED
    if before_others & after_directives:
        return _NEW_DIRECTIVE
    if before_directives & after_others:
        return _LOST_DIRECTIVE
    return _directive_deleted(before, after, language)


def _directive_deleted(before: str, after: str, language: str) -> str | None:
    """The second net of :func:`_directive_change`: an input directive the
    rewrite deleted or moved while the code holding it stayed."""
    before_directives = _directives(before, language)
    if not before_directives:
        return None
    from flag_cleanup.syntax import _line_opcodes, _SegmentTooLarge

    old = [line.strip() for line in before.splitlines()]
    new = [line.strip() for line in after.splitlines()]
    try:
        opcodes = _line_opcodes(old, new)
    except _SegmentTooLarge:
        return _DIRECTIVES_UNCHECKED
    # Adjacent unchanged blocks at one offset are one block: an anchor line is
    # an `equal` block of its own.
    equal: list[tuple[int, int, int, int]] = []
    deleted: set[int] = set()
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "delete":
            deleted.update(range(i1, i2))
        if tag != "equal":
            continue
        if equal and equal[-1][1] == i1 and equal[-1][3] == j1:
            equal[-1] = (equal[-1][0], i2, equal[-1][2], j2)
        else:
            equal.append((i1, i2, j1, j2))
    starts = [i1 for i1, _i2, _j1, _j2 in equal]
    after_set = {(row, column, text) for _s, _h, row, column, _e, text in _directives(after, language)}
    for script, header, row, column, _end, text in before_directives:
        # The one block that can hold `header`: the last starting at or before it.
        at = bisect.bisect_right(starts, header) - 1
        there = None
        if at >= 0 and row < equal[at][1]:
            there = equal[at][2] + row - equal[at][0]
        if there is not None:
            if (there, column, text) not in after_set:
                return _LOST_DIRECTIVE
            continue
        if script or header not in deleted or row not in deleted:
            return _LOST_DIRECTIVE
    return None


#: The containers where a `,` may end an EMPTY slot: an array literal's hole
#: (`[a, , b]`) and an array pattern's elision (`const [, b] = xs`).
_HOLE_CONTAINERS = frozenset({"array", "array_pattern"})

#: The member lists whose `;` separates members too: a leading one
#: (`{ ; a: number }`) is a SyntaxError to tsc and no ERROR to the grammar. A
#: class body is not one of them, where an empty `;` member is legal.
_TYPE_MEMBER_LISTS = frozenset({"object_type", "interface_body"})


def _empty_slots(source: str, language: str) -> int:
    """How many commas in ``source`` end an empty slot outside an array.

    A comma with no element before it (`{ , b: 1 }`, `f(a, , b)`) is a
    SyntaxError everywhere but an array, yet the grammar's lists accept it
    (`object`, `object_pattern`, `arguments` parse it with no ERROR node), so
    the first check cannot see it. A deletion that takes an entry but leaves its
    comma (behind a comment between the two: `{ k: 1 /* c */, b: 2 }`) writes
    one. Comments between the commas do not count as an element. Nor, in an
    object type or interface, does a leading `;` (`{ /* c */; a: number }`).
    """
    count = 0
    for node in _walk(_parse(source, language)):
        if node.type in _HOLE_CONTAINERS or node.type == "ERROR":
            continue
        previous = None
        for child in node.children:
            if child.type in _PROLOGUE_TRANSPARENT:
                continue
            if child.type == "," and (previous is None or previous.type in (",", "{", "(", "[")):
                count += 1
            elif (
                child.type == ";"
                and node.type in _TYPE_MEMBER_LISTS
                and previous is not None
                and previous.type == "{"
            ):
                count += 1
            previous = child
    return count


def _array_holes(source: str, language: str) -> list[tuple[int, int, bytes, int]]:
    """Each array literal and pattern in ``source`` that holds a hole (a comma
    with no element before it, `[a, , b]`), as ``(row of its `[`, column past
    the line's indent, the line's text before it with the indent stripped,
    holes)``, sorted by row."""
    data = source.encode("utf-8")
    found: list[tuple[int, int, bytes, int]] = []
    for node in _walk(_parse(source, language)):
        if node.type not in _HOLE_CONTAINERS:
            continue
        holes = 0
        previous = None
        for child in node.children:
            if child.type in _PROLOGUE_TRANSPARENT:
                continue
            if child.type == "," and (previous is None or previous.type in (",", "[")):
                holes += 1
            previous = child
        if holes:
            line_start = data.rfind(b"\n", 0, node.start_byte) + 1
            ahead = data[line_start : node.start_byte]
            indent = len(ahead) - len(ahead.lstrip(b" \t"))
            found.append(
                (node.start_point[0], node.start_point[1] - indent, ahead.strip(), holes)
            )
    found.sort()
    return found


def _new_holes(before: str, after: str, language: str) -> bool:
    """Whether the rewrite left an array element behind as a hole.

    No removal writes one: deleting an element keeps every other slot, a hole
    included. A deletion that left an element's slot behind turns the element
    into a hole (`[a, , k, b]` -> `[a, , , b]`): the array keeps its length but
    `k` reads `undefined`. Checked per array, so a deleted array that held a
    hole cannot mask a new one elsewhere: an output array on an unchanged line
    may hold no more holes than the input array at the same `[`, and one on a
    changed line no more than an input array of the stretch it replaced with
    the same text ahead of it on its line (an element deleted ahead of it on
    its line changes that, and is refused: over-refusal). It can still be
    masked: a deleted `const r = [1, , 2];` beside a `const r = [a, k, b]`
    that kept `k`'s slot has the same key. This pairs lines,
    which twice let a directive check be fooled; it is acceptable here because
    it is a second net: the engine checks every list deletion against the
    slots it must leave (its own invariant). A lost hole is not seen here.
    """
    after_arrays = _array_holes(after, language)
    if not after_arrays:
        return False
    before_arrays = _array_holes(before, language)
    from flag_cleanup.syntax import _line_opcodes, _SegmentTooLarge

    old = [line.strip() for line in before.splitlines()]
    new = [line.strip() for line in after.splitlines()]
    try:
        opcodes = _line_opcodes(old, new)
    except _SegmentTooLarge:
        return sum(a[3] for a in after_arrays) > sum(b[3] for b in before_arrays)
    before_rows = [array[0] for array in before_arrays]
    after_rows = [array[0] for array in after_arrays]
    before_at = {(row, column, ahead): holes for row, column, ahead, holes in before_arrays}

    def within(arrays, rows, low, high):
        return arrays[bisect.bisect_left(rows, low) : bisect.bisect_left(rows, high)]

    for tag, i1, i2, j1, j2 in opcodes:
        written = within(after_arrays, after_rows, j1, j2)
        if not written:
            continue
        if tag == "equal":
            for row, column, ahead, holes in written:
                if holes > before_at.get((i1 + row - j1, column, ahead), 0):
                    return True
            continue
        # A changed stretch: each array there holding a hole must have one in
        # the stretch it replaced with the same text ahead of it, at least as
        # many holes, each used once.
        had = sorted(
            ((column, ahead, holes) for _r, column, ahead, holes in within(
                before_arrays, before_rows, i1, i2
            )),
            key=lambda entry: -entry[2],
        )
        for _row, column, ahead, holes in written:
            match = next(
                (e for e in had if e[0] == column and e[1] == ahead and e[2] >= holes), None
            )
            if match is None:
                return True
            had.remove(match)
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
       :func:`_asi_merged_statements`;
    5. more expression statements starting with `function`, `class`, `{`,
       `async function` or `let [` — see ``RESTRICTED_STATEMENT_START``.
    6. more commas ending an empty slot outside an array (`{ , b: 1 }`) — see
       :func:`_empty_slots` — or an array element left as a hole (:func:`_new_holes`);
    7. a directive prologue the rewrite changed: a bare string that became a
       directive (`"use strict"` left first in a function), or one that no
       longer is — see :func:`_directive_change`.

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
    return refusal_reason(before, after, language) is not None


def refusal_reason(before: str, after: str, language: str) -> str | None:
    """Which of :func:`transform_broke_syntax`'s checks refused; ``None`` if none.

    The same seven checks in the same order, each worded for the person reading
    the run's output — a refusal that names no cause sends them looking for a
    syntax error in a file that may parse perfectly well.
    """
    if introduced_syntax_errors(before, after, language):
        return "the result has a syntax error the original did not"
    if _reserved_bindings(after, language) > _reserved_bindings(before, language):
        return "a boolean literal landed where a name is declared"
    if _reserved_word_identifiers(after, language) > _reserved_word_identifiers(
        before, language
    ):
        return "a keyword was left stranded where an expression is read"
    if _asi_merged_statements(before, after, language):
        return "two statements would run together without a semicolon"
    if _restricted_statements(after, language) > _restricted_statements(before, language):
        return (
            "a statement would start with `function`, `class` or `{`, which "
            "JavaScript reads as a declaration or a block"
        )
    if _empty_slots(after, language) > _empty_slots(before, language):
        return "a comma was left with nothing before it, which only an array allows"
    if _new_holes(before, after, language):
        return "an array element would be left as a hole (`[a, , b]`)"
    directive = _directive_change(before, after, language)
    if directive is not None:
        return directive
    return None


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
