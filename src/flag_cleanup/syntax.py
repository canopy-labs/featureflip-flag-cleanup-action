"""The post-transform syntax gate (Gate 1), for every supported language.

This is the single entry point the runner calls. TypeScript keeps its own
implementation in :mod:`flag_cleanup.ts_syntax` — four checks, each added after
a real escape — and everything else is handled here.

Why the other languages need FEWER checks, and why that is a finding rather
than a shortcut
---------------------------------------------------------------------------
M2's checks 2-4 were not general truths about rewriting code; each was a
TypeScript hazard:

* **Check 4, ASI fusion**, is the sharpest example. Deleting the statement
  between ``const run = pick()`` and ``(run)()`` fuses them into one call in
  JavaScript, because ASI only inserts a semicolon when the next line *cannot*
  continue the expression. Every other language here terminates a statement on
  the newline itself (Go, Swift, Kotlin) or on the indentation (Python), so the
  survivors cannot merge. **Measured**, not assumed: parsing exactly that pair
  with each grammar yields two statements everywhere except TypeScript, which
  yields one.
* **Check 3, stranded keywords**, generalises and is kept — it is the differential
  form of "a deletion left a keyword where an expression is now read".
* **Check 2, reserved words in binding positions**, was driven by TypeScript's
  const-propagation rules, which only exist for TypeScript. The generalisation
  that survives is check 3.

So the gate below is: **a differential parse-error count** (the universal one)
plus **a differential stranded-keyword count**. Both compare against the input's
own parse, so a file that was already unusual stays eligible and only a NEW
breakage is refused.

Python needs two checks the others do not
-----------------------------------------
Both exist because Python's block structure is *whitespace*, and the transform
engine cannot re-indent (see the header of ``rules/python.toml``).

* **A native ``compile``**, because tree-sitter is not a sufficient gate for
  this language. Measured: tree-sitter-python reports **no ERROR node** for a
  line that is over-indented, for a line dedented out of its function, or for
  a ``def`` left with no body at all — every one of which CPython rejects
  outright. Those are exactly the three ways a bad Python rewrite fails, so the
  universal check above is blind to the whole failure class. ``compile`` is
  the real CPython front end and costs nothing to reach. It stays *differential*
  like everything else here, which also makes it degrade safely: a file using
  syntax this interpreter does not know fails both parses and is judged on the
  rest.
* **A differential count of boolean-literal conditions**, because for Python a
  rule set can legitimately decline to fold. A multi-statement ``if True:``
  body and an ``elif True:`` are both left standing by ``rules/python.toml``,
  and shipping those as a "flag removal" is the half-finished cleanup that
  looks deliberate — the flag key is gone, so nothing else in this tool would
  ever notice. Turning it into a refusal is what makes "no diff or a complete
  diff" true for a language whose fold is partial.

Go needs one the others do not
------------------------------
* **A differential count of ``if`` initializers.** ``if v := prime(); cond {…}``
  runs ``prime()`` whatever the condition is, so folding that ``if`` away
  destroys a side effect and unbinds ``v``. Two separate rewrites got there —
  one of ours and one of the engine's built-ins — and the built-in's output
  COMPILES when nothing else referenced ``v``, which is the failure class with
  no other net. See :func:`_lost_side_effects`.

Java and Go need one that C# gets from its grammar for free
-----------------------------------------------------------
* **A differential count of boolean literals ALONE in statement position.**
  Replacing a read that was called for its own sake — ``verify(client)
  .boolVariation("KEY", ctx, false);`` in a Mockito test, or a bare warm-up
  call — leaves ``true;`` behind, and that is not a statement in either
  language: javac rejects it under JLS 14.8 and Go rejects it as
  ``true (untyped bool constant) is not used``. Both were measured, and so was
  the reason nothing caught it: **tree-sitter parses ``true;`` as a perfectly
  ordinary ``(expression_statement (true))`` with no ERROR node**, so the
  engine's own self-check passes it and the differential error count does not
  move. C# is immune by accident of ITS grammar — the same shape is an ERROR
  there, so the engine aborts before Gate 1 is reached — which is exactly why
  the hazard was invisible: the one language with a fixture for it is the one
  language that could not exhibit it. See :func:`_literal_statements`.

None of this makes bad output impossible. It asks whether the result still
parses as intended, and a wrong rewrite can pass that. It is a net, not a proof
— exactly as in :mod:`flag_cleanup.ts_syntax`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
import re

import tree_sitter_c_sharp
import tree_sitter_dart
import tree_sitter_embedded_template
import tree_sitter_go
import tree_sitter_java
import tree_sitter_kotlin
import tree_sitter_php
import tree_sitter_python
import tree_sitter_ruby
import tree_sitter_swift
from tree_sitter import Language, Node, Parser

from flag_cleanup import key_const, ts_syntax

#: Languages :mod:`flag_cleanup.ts_syntax` owns outright. `js` is here rather
#: than in `_PROFILES` below because it needs the WHOLE ts_syntax machinery —
#: the ASI-fusion check and the Gate 2 const path are as load-bearing for
#: JavaScript as for TypeScript (semicolon-free JS is if anything more common).
#: A `_PROFILES` row would have given it the generic checks and silently
#: dropped both.
_TS_LANGUAGES = frozenset({"ts", "tsx", "js"})


@dataclass(frozen=True)
class _Profile:
    """Per-language knobs for the checks below.

    ``identifier_nodes`` are the node types a bare name parses as, which is
    where a stranded keyword surfaces. ``keywords`` is deliberately a SMALL,
    conservative set: it costs nothing to omit a word (the check simply catches
    less) and a false entry would refuse valid rewrites, so only words that can
    never name a value are listed. The booleans this tool emits are present for
    every language whose grammar gives them their own node type, since emitting
    one into a binding position is the failure this check exists to see — the
    Kotlin profile is the documented exception, and says why.
    """

    grammar: object
    identifier_nodes: frozenset[str]
    keywords: frozenset[str]
    #: ``(node type, field name)`` pairs whose field holding a boolean literal
    #: means the rewrite left something behind that must not ship. Empty for
    #: Java and Swift, which lean on Piranha's built-in cleanup — it folds the
    #: condition AND inlines a local bound to the read, so a count there would
    #: only ever be zero. Populated for Python, where declining to fold is a
    #: designed-in outcome rather than a bug (see the module docstring), for
    #: PHP, and for Go — whose two entries are deliberately different kinds and
    #: say so at the profile.
    literal_residues: tuple[tuple[str, str], ...] = ()
    #: Node types this grammar spells a boolean literal as, when they are not
    #: the usual ``true``/``false``. PHP is the one language here that gives
    #: both values a single ``boolean`` node type, which is why the residue
    #: check could not see PHP at all before it was made to ask the profile.
    boolean_nodes: frozenset[str] = frozenset()
    #: Node types :func:`_literal_residues` descends THROUGH when looking for a
    #: literal in a field. Each wraps an expression without changing what it
    #: evaluates to, so a literal underneath one is still a literal in that
    #: position — PHP parenthesises every condition, and Go wraps a `case`
    #: value in a list. Kept per-language rather than global because
    #: "transparent" is a claim about a specific grammar, and a wrong entry
    #: here would make the check refuse valid rewrites.
    transparent_nodes: frozenset[str] = frozenset()
    #: ``(node type, field name)`` pairs that a rewrite must never make FEWER
    #: of. For a construct whose field carries a side effect, deleting or
    #: folding the construct destroys it, and no rewrite this tool performs can
    #: legitimately do that. Populated for Go's ``if`` initializer — see
    #: :func:`_lost_side_effects`.
    preserved_fields: tuple[tuple[str, str], ...] = ()
    #: Whether a real parser for this language is available in-process, giving
    #: an exact answer where tree-sitter gives a permissive one.
    native_parser: bool = False
    #: Whether a statement this rewrite made UNREACHABLE is a compile error in
    #: this language. Java and Dart — but this flag is not the whole answer, and
    #: reading it as one is how #2807 happened: the TypeScript family is on the
    #: same side of the line and has no profile row to carry it, so it is
    #: admitted by :func:`_unreachable_dialect` directly. That function is the
    #: single decision point; ask it, not this field. See
    #: :func:`_unreachable_statements` for why the languages that merely
    #: tolerate the shape are deliberately excluded.
    unreachable_is_fatal: bool = False
    #: Whether a boolean literal ALONE in statement position is a compile error
    #: in this language. Java and Go only, both measured against the real
    #: toolchain — see :func:`_literal_statements`. Off everywhere else because
    #: the shape is genuinely legal there (PHP and Ruby run it as a no-op,
    #: verified with `php -l`), or because the grammar rejects it outright so
    #: the engine aborts first (C#).
    literal_statement_is_fatal: bool = False
    #: Whether comparing a boolean literal against ``null`` is a compile error
    #: in this language. Java only, measured — see
    #: :func:`_null_literal_comparisons` for the shape that produces one.
    null_comparison_is_fatal: bool = False
    #: Whether a file this grammar cannot fully parse should be SKIPPED rather
    #: than handed to the engine. Dart only — see :func:`source_is_unreadable`
    #: for the defect and the measurement. Off everywhere else, and it must stay
    #: off for the TS family in particular: a pre-existing ERROR node is
    #: ordinary there (that is the whole reason Gate 1 is differential), so
    #: turning this on would refuse to clean a large slice of real repositories.
    unreadable_source_is_skipped: bool = False
    #: For a TEMPLATE language, the profile key whose checks judge the HOST
    #: code inside the tags — applied to a derived code view rather than to the
    #: file's own bytes. See :func:`_code_view`. ``erb`` only.
    #:
    #: Naming ANOTHER PROFILE rather than carrying a second grammar field is
    #: the whole point: it makes "ERB is judged as Ruby" true by construction,
    #: inheriting Ruby's tuned keyword set and its ``("when", "pattern")``
    #: residue entry rather than duplicating either. A duplicate of the second
    #: would re-open #2692's permanent-half-cleanup class the first time the
    #: two copies drifted.
    code_view: str | None = None


_PROFILES: dict[str, _Profile] = {
    "java": _Profile(
        tree_sitter_java.language,
        frozenset({"identifier", "type_identifier"}),
        frozenset({"true", "false", "null", "class", "return", "if", "else", "void",
                   "new", "this", "super", "static", "final", "import", "package"}),
        # Java is the language this check was BUILT for — code this tool made
        # unreachable is a compile error here (JLS 14.21), not a warning. It is
        # no longer the only one: Dart's profile carries the flag too, and the
        # TypeScript family is admitted without a profile row. See
        # `_unreachable_dialect` for the set and `_unreachable_statements` for
        # the shape.
        unreachable_is_fatal=True,
        # `true;` is not a statement in Java — JLS 14.8 admits only an
        # assignment, a pre/post increment or decrement, a method invocation
        # and an instance creation. A read called for its own sake reduces to
        # exactly that, and `verify(client).boolVariation("KEY", ctx, false);`
        # is the idiomatic way to write one.
        literal_statement_is_fatal=True,
        # `true != null` is `error: bad operand types` — a primitive against
        # `<null>`. Reached by inlining a BOXED `Boolean` binding that its own
        # null-guard tests, which is the ordinary reason to write the boxed
        # type at all. See `_null_literal_comparisons`.
        null_comparison_is_fatal=True,
        # Consumed ONLY by the null-comparison check above: Java sets no
        # `literal_residues`, which is the other reader of this field. It is
        # here so a parenthesised operand — `(true) != null`, which `javac`
        # rejects identically — cannot slip past by being wrapped.
        transparent_nodes=frozenset({"parenthesized_expression"}),
    ),
    "go": _Profile(
        tree_sitter_go.language,
        frozenset({"identifier", "field_identifier", "type_identifier", "package_identifier"}),
        frozenset({"true", "false", "nil", "func", "return", "if", "else", "range",
                   "package", "import", "var", "const", "type", "struct", "chan"}),
        # Go's `if` may carry an initializer — `if v, err := load(); cond {…}` —
        # which RUNS WHATEVER THE CONDITION IS. Folding or deleting that `if`
        # therefore destroys a side effect and unbinds names the rest of the
        # function uses, and there is no rewrite that keeps it. Both ways of
        # getting there produced broken Go: our own `delete_trailing_else_if_false`
        # rebuilt the statement without it, and Piranha's built-in deletion took
        # the whole thing. The first does not compile (loud); the SECOND does —
        # `if v := prime(); <read> {…}` simply loses the `prime()` call, which is
        # the compiles-and-is-wrong class this gate exists for.
        preserved_fields=(("if_statement", "initializer"),),
        # A tagless `switch { case <read>: … }` is Go's idiomatic multi-way
        # guard, and the built-ins fold the `if` family only: measured, the
        # read becomes `case false:` and nothing else moves, so the dead arm
        # stays with the flag key gone from the file.
        #
        # This used to refuse the shape outright. Since #2692 `go_fold` folds
        # it, so what survives to be counted here is only what that module
        # DECLINES — a `fallthrough` anywhere in the switch, a `break` in a body
        # about to be spliced out of it, an initializer whose bindings the arms
        # use, or a `default:` above the flag's arm. Each of those is a shape
        # with no correct local edit, so this stays the backstop that turns them
        # into a refusal rather than a permanent residue. Narrowed in scope, not
        # weakened: deleting this entry would ship those four silently.
        #
        # A TAGGED `switch enabled { case true: … }` is ordinary customer code
        # this tool never creates, and the differential comparison is what
        # keeps it eligible: the count has to RISE for the rewrite to be
        # refused.
        #
        # The second entry is a different animal from every other row in this
        # table, and the distinction is worth keeping: the others catch a fold
        # that STOPPED SHORT, while this one catches a fold that finished and
        # produced source `go build` rejects. It shares the mechanism because
        # the shape is identical — a field of a node holding a boolean literal
        # — not because the failure is.
        #
        # `m.EXPECT().BoolVariation("k", ctx, false).Return(true)` is gomock's
        # entire surface, and the read inside it is an ordinary call
        # expression: nothing in the syntax marks the chain as a stub rather
        # than a real read, so the read folds and the receiver becomes a
        # literal. `true.Return undefined (type untyped bool has no field or
        # method Return)`, measured with `go build`.
        #
        # Safe as a residue precisely because Go's `bool` is a builtin with no
        # method set and no way to acquire one, so `true.X` is broken Go BY
        # CONSTRUCTION — there is no legitimate rewrite, and no legitimate
        # customer source, that produces one. That is what makes this a
        # structural tell rather than a guess, and it is also why it stays
        # Go-only: `false.ToString()` is ordinary C#, Kotlin and Dart both give
        # `Boolean` real members, and NSubstitute's `Returns` is an extension
        # method on `T` so C#'s identical-looking `false.Returns(true)` may
        # even compile. See the README's mocking-DSL gap for the languages
        # where the same fold is silent instead.
        literal_residues=(
            ("expression_case", "value"),
            ("selector_expression", "operand"),
        ),
        # `case false:` holds its value in an `expression_list`, which is
        # transparent only when it has exactly one child — `case a, b:` is a
        # genuine list and `_through_wrappers` deliberately leaves it alone.
        #
        # `parenthesized_expression` is here for the selector entry above, for
        # the same reason Java carries it: a customer who wrote
        # `(c.BoolVariation(…)).Return(true)` gets `(true).Return(true)`, which
        # `go build` rejects identically, and an operand that slipped past by
        # being wrapped would be the one shape this check could not see.
        transparent_nodes=frozenset({"expression_list", "parenthesized_expression"}),
        # Go rejects an unused constant expression outright: a bare `true`
        # where a call used to be is `true (untyped bool constant) is not
        # used`, measured with `go build`. Same shape and same reasoning as
        # Java's above, from a compiler that is stricter still — Go will not
        # even let the value be discarded.
        literal_statement_is_fatal=True,
    ),
    "swift": _Profile(
        tree_sitter_swift.language,
        frozenset({"simple_identifier", "type_identifier"}),
        frozenset({"true", "false", "nil", "func", "return", "if", "else", "let",
                   "var", "class", "struct", "guard", "import", "self"}),
    ),
    "kt": _Profile(
        tree_sitter_kotlin.language,
        # `identifier`. TWO Kotlin grammars are in play in this tool — the
        # engine's bundled one (`rules/kt.toml` queries it) and the wheel one
        # parsed with here (`tree-sitter-kotlin`, a third, independent
        # package — see `kotlin_sentinel`'s module docstring). Before the
        # 0.5.0 engine bump they used DIFFERENT names for this node
        # (`simple_identifier` bundled vs `identifier` wheel); naming the
        # bundled one here made `_stranded_keywords` match nothing at all, so
        # the check was DEAD for Kotlin: it returned 0 for a rewrite that
        # stranded a `val`, where the Java profile returns 1. Since 0.5.0
        # (tree-sitter-kotlin-ng) both grammars happen to agree on
        # `identifier` — coincidence, not a guarantee that holds across a
        # future bump of either grammar independently, so keep verifying this
        # against the WHEEL grammar specifically (`tree_sitter_kotlin`, this
        # profile's own parser) rather than assuming it tracks the engine's.
        frozenset({"identifier"}),
        # `true`/`false` are deliberately ABSENT, and this is the one profile
        # they are absent from. In this grammar a boolean literal parses as a
        # plain `identifier` (`val x = false` -> identifier 'false'), so listing
        # them would count every legitimate literal this tool emits and refuse
        # every Kotlin rewrite. That also makes the check thinner here than
        # elsewhere: it can no longer see a boolean landing in a binding
        # position. The differential ERROR count carries that weight instead —
        # measured, the stranded-`val` case this grammar rejects outright.
        frozenset({"null", "fun", "return", "if", "else", "val",
                   "var", "class", "object", "import", "package", "this", "when"}),
    ),
    "python": _Profile(
        tree_sitter_python.language,
        frozenset({"identifier"}),
        frozenset({"True", "False", "None", "def", "class", "return", "if", "else",
                   "elif", "import", "from", "pass", "lambda", "while", "for"}),
        # Two residues, both of which this tool's Python rules can really
        # leave: a condition it declined to fold, and a name bound to the
        # literal that `python_fold` declined to inline. Neither survives the
        # common case any more — the post-engine fold finishes both — but the
        # gate is what makes DECLINING safe, so it stays exactly as strict.
        # `python_fold.inline_literal_bindings` refuses a binding it cannot
        # prove is a function-local (module-level and class-body ones are
        # public API, a second binder may shadow it), and this is what turns
        # that refusal into a rolled-back transform instead of a half-done file.
        #
        # `while` is deliberately absent from the condition half: `while True:`
        # is an ordinary Python idiom and this tool never creates one — a flag
        # read is not a loop condition it can reach — so counting it would
        # refuse files for something that was always there. Everything here is
        # differential anyway, but a check that can only ever fire on the
        # customer's own code is noise.
        #
        # `pair` (a dict value) is absent for the opposite reason: `{"k": True}`
        # is a COMPLETE fold, not a half-finished one. There is nothing further
        # to clean once the read becomes a constant in that position.
        literal_residues=(
            ("if_statement", "condition"),
            ("elif_clause", "condition"),
            ("assignment", "right"),
            ("augmented_assignment", "right"),
            ("named_expression", "value"),
        ),
        native_parser=True,
    ),
    "php": _Profile(
        # The HTML-aware grammar, matching the engine's `LANGUAGE_PHP`. Gate 1
        # must see the same node shapes the engine rewrites against, and
        # `language_php_only` refuses the markup a `.phtml` template is made of.
        tree_sitter_php.language_php,
        # A bare name is `name`; a variable is `variable_name` (which wraps its
        # own `name` child). Both are collected — `variable_name` because a
        # deletion that strands `$false` should be seen, `name` because that is
        # what a stranded function or constant reference parses as.
        frozenset({"name", "variable_name"}),
        # PHP keywords are case-insensitive, so these are matched by the
        # generic check against lowercased text. `true`/`false`/`null` are here
        # even though this grammar gives them their own node types
        # (`boolean`/`null`) rather than parsing them as identifiers — the
        # check costs nothing when they never appear as `name`, and a grammar
        # bump that changed that would be caught rather than missed.
        frozenset({"true", "false", "null", "function", "return", "if", "else",
                   "elseif", "class", "new", "echo", "require", "include",
                   "namespace", "use"}),
        # THE ALTERNATIVE SYNTAX is why PHP needs this after all. The earlier
        # measurement here looked only at braced code, where the built-ins
        # always finish the fold, and concluded the check had nothing to see.
        # A `.phtml` template does not use braces:
        #
        #     <?php if ($c->boolVariation('KEY', $ctx, false)): ?>
        #       <p>legacy</p>
        #     <?php else: ?>
        #       <p>modern</p>
        #     <?php endif; ?>
        #
        # folds the READ and stops — measured — leaving `if (false):` with both
        # arms of markup standing. So the single most common shape in the one
        # extension `.phtml` exists for was producing a permanent half-cleanup:
        # correct output, dead branch kept, and the flag key GONE, so no later
        # run would ever look again.
        #
        # `match_conditional_expression` is the same failure in an expression:
        # `false => 10,` stays as a dead arm.
        #
        # **`flag_cleanup.php_fold` now FINISHES the two `if`-shaped residues
        # (#2693), and this table is unchanged and still strict.** It runs
        # before this gate and only where it can produce a complete rewrite; a
        # shape it declines — a braceless body, a chain mixing body spellings,
        # the `match` arm — still lands here and still refuses. Same
        # relationship `python_fold` has with the Python entries above: the
        # fold is what makes the shapes rare, the gate is what makes declining
        # them safe. Do not relax the table because the fold covers a case; the
        # fold's coverage is measured per shape and the gate's is not.
        literal_residues=(
            ("if_statement", "condition"),
            ("else_if_clause", "condition"),
            ("match_conditional_expression", "conditional_expressions"),
            # `switch (true) { case <read>: … }` is PHP's multi-way guard, the
            # same construct as Go's tagless switch and Ruby's subject-less
            # `case`. This entry was missing, so the read left as `case false:`
            # SHIPPED rather than being refused — with the flag key already gone
            # from the file, nothing could revisit it (#2692). `php_fold` folds
            # the shape now, so what reaches here is only what that module
            # declines: an implicit fall-through into the dead arm, an arm
            # ending in `throw`/`die()` (indistinguishable from an assignment in
            # this grammar), the `switch (…): … endswitch;` alternative syntax,
            # or a `default:` above the flag's arm.
            ("case_statement", "value"),
        ),
        # This grammar gives BOTH values one `boolean` node type, and wraps
        # every condition in `parenthesized_expression` — a `match` arm's
        # condition in a `match_condition_list` besides.
        boolean_nodes=frozenset({"boolean"}),
        transparent_nodes=frozenset({"parenthesized_expression", "match_condition_list"}),
        # Unreachable code after a folded if/else is not fatal in PHP — there is
        # no compile step to reject it, and no gate in the standard PHP toolchain
        # that fails on it — so `unreachable_is_fatal` stays off, as it does
        # everywhere except Java, Dart and the TypeScript family. See
        # `_unreachable_dialect` for the current set; it is the one place that
        # decides, so an enumeration written out here can only ever go stale.
        #
        # `literal_statement_is_fatal` stays off too, and that is measured
        # rather than assumed: `true;` is a legal no-op statement in PHP
        # (`php -l` accepts it and it runs), unlike Java and Go.
    ),
    "dart": _Profile(
        # PyPI's 0.1.0 — NOT the 0.2.0 the engine bundles and rewrites against.
        # This is the one place in this tool where Gate 1 and the engine parse
        # with genuinely different grammars rather than two builds of one, and
        # the reasoning for why it is nevertheless sound is in CLAUDE.md; the
        # short version is that Gate 1 asks only two questions of a tree (did
        # the error count rise, did a keyword end up in a name position) and
        # both were measured to behave on 0.1.0.
        tree_sitter_dart.language,
        # Both occur: a value name is `identifier`, a type name is
        # `type_identifier`.
        frozenset({"identifier", "type_identifier"}),
        # `true`/`false`/`null` get their own node types inside a function body
        # (`true`, `false`, `null_literal`), so listing them costs nothing and
        # would catch a grammar bump that reclassified them — the same reasoning
        # the `php` and `ruby` profiles record. Measured rather than assumed:
        # every one of these parses as a plain `identifier` after a dot
        # (`x.class`, `x.late`), which is FINE, because the check is
        # differential and fires only on an increase.
        frozenset({"true", "false", "null", "class", "return", "if", "else",
                   "void", "new", "this", "super", "static", "final", "const",
                   "import", "late", "required"}),
        # No `literal_residues`: the engine's Dart cascade folds the condition
        # AND inlines a local bound to the read, so a count here could only ever
        # be zero.
        #
        # `unreachable_is_fatal` IS on, making Dart the second language after
        # Java to carry it — and the decision was made by running the real
        # toolchain rather than by reading the severity word. `dart analyze`
        # calls dead code a WARNING, which would suggest Dart belongs with
        # Go/Kotlin, but it **exits 2** on it and 0 without it:
        #
        #     warning - lib/probe.dart:12:3 - Dead code. … - dead_code
        #     $ dart analyze; echo $?   ->   2
        #
        # `dart analyze` (or `flutter analyze`) is the standard gate in
        # essentially every Flutter CI, so the practical severity is Java's, not
        # Go's `gofmt -l`: the pull request this Action opens would not go green.
        # The test in this module's suite pins the exit code, not the wording.
        #
        # TypeScript turned out to be this same case and was read the other way
        # for as long as the line existed — `no-unreachable` is in
        # `eslint:recommended` and is an ERROR (#2807). It is admitted by
        # `_unreachable_dialect` rather than by a profile row, since the TS
        # family has none. Which is why the enumeration above no longer names
        # it: a language whose severity word disagrees with its gate's exit
        # code belongs here, not in the contrast set.
        #
        # Every node name `_unreachable_statements` and `_completes_abruptly`
        # rely on is present and identical here — `if_statement` with
        # `consequence:`/`alternative:` fields, `block`, `return_statement` —
        # so this needed the flag and no new code. Verified, not assumed.
        unreachable_is_fatal=True,
        # The one language that skips a file it cannot parse. See
        # `source_is_unreadable` — this is where the two Dart grammars being
        # genuinely different stops being merely tolerable and starts being
        # useful, because 0.1.0 sees a hazard 0.2.0 silently mis-parses.
        unreadable_source_is_skipped=True,
    ),
    "ruby": _Profile(
        tree_sitter_ruby.language,
        # A bare name is `identifier`; a method name in a call is the same node
        # type, and `constant` is what a capitalised name parses as. Both are
        # collected — a deletion that strands `Featureflip` should be seen the
        # same way one that strands `client` is.
        frozenset({"identifier", "constant"}),
        # RUBY BREAKS THE DOCSTRING'S RULE FOR CHOOSING THIS SET, and the
        # substitution matters more here than anywhere else.
        #
        # The rule above says to list only words that can never name a value.
        # In Ruby every keyword can: `x.class`, `x.then`, `x.begin` and `x.end`
        # all parse the keyword as an `identifier`. Applied literally the rule
        # yields an EMPTY set, which would leave this language with no keyword
        # check at all.
        #
        # So the set is chosen by what a broken rewrite can STRAND instead —
        # and measured, because for Ruby this check is not a supplement to the
        # error count, it is the only thing that sees most breakage:
        #
        #   broken rewrite      ERROR nodes   caught by
        #   stranded `end`          0         keywords only
        #   orphan `end`            0         keywords only
        #   stranded `rescue`       0         keywords only
        #   stranded `elsif`        1         either
        #   stranded `when`         1         either
        #
        # Three of five shapes produce ZERO error nodes. The false-positive
        # risk from an ordinary `x.class` is nil because the check is
        # differential and fires only on an INCREASE — deleting code lowers the
        # count, never raises it.
        #
        # `true`/`false`/`nil` never parse as `identifier` here (`true = 1` is
        # `(assignment left: (true))`, which is also why the engine turns its
        # own `reserved_identifiers` off for Ruby), so they cost nothing today
        # and would catch a grammar bump that changed it — the same reasoning
        # the `php` profile above records.
        frozenset({"end", "else", "elsif", "when", "rescue", "ensure", "then",
                   "do", "true", "false", "nil"}),
        # The residues CLAUDE.md's Ruby section measured are all in the
        # `if`/`elsif` family, and it was right that none of them leaves a
        # literal to count. It did not cover `case`/`when`, and that shape does
        # (#2692): a SUBJECT-LESS `case` is Ruby's spelling of Go's tagless
        # switch, the built-ins fold the `if` family only, and the read was
        # left as `when false` — shipped, not refused, because there was no
        # entry here to catch it. With the flag key already gone from the file,
        # no later run could revisit it. Ruby was the worst of the three
        # languages carrying this construct for exactly that reason: Go refused
        # loudly, PHP's entry covered `if`/`else_if`/`match` but not `case`,
        # and Ruby said nothing at all.
        #
        # `ruby_fold` folds the shape now, so what reaches this entry is only
        # what that module declines — a `when` listing several patterns, where a
        # literal among alternatives is not the whole condition. The pattern
        # node wraps the literal, which `transparent_nodes` below is what walks
        # through.
        literal_residues=(("when", "pattern"),),
        transparent_nodes=frozenset({"pattern"}),
        # Unreachable code after a folded `if`/`else` is not fatal in Ruby
        # (there is no compile step to reject it, and no standard gate that
        # fails on it), so `unreachable_is_fatal` stays off as it does
        # everywhere except Java, Dart and the TypeScript family — see
        # `_unreachable_dialect`, which is the one place that decides.
    ),
    # ERB — the first TEMPLATE language here, and the first profile whose
    # grammar is not the language its rules are written for. `.erb` runs
    # `rules/ruby.toml` unmodified (see `piranha_runner._LANGUAGES`), and the
    # engine rewrites it through a synthesized Ruby view; this profile is the
    # only place that split has to be modelled downstream.
    #
    # **The grammar is the TEMPLATE one, and pointing it at `tree_sitter_ruby`
    # instead is the mistake this entry exists to prevent.** Measured over 162
    # shapes and 324 real engine runs, a Ruby-grammar profile produced 22
    # refusals, ALL of them false — correct, behaviour-preserving rewrites
    # abandoned — while catching none of twelve realistic damage forms. It is
    # not a weak gate; on this transform its true-positive rate is zero and its
    # false-positive rate is not, and a Gate 1 refusal is flag-wide and
    # permanent. The cause is structural rather than a corpus artefact: an
    # HTML-heavy template is not Ruby at all, so the Ruby parse is 100% error
    # recovery (min 1, max 15, mean 4.3 ERROR/MISSING nodes, never zero), and
    # error recovery has no stability contract across an edit. A correct
    # rewrite re-partitions those spans and the differential reads the shift as
    # damage — the same mechanism the TypeScript grammar pin exists for,
    # arriving for a reason no pin can fix. Under this grammar all 162 inputs
    # parse with ZERO errors and none of the 324 runs is refused.
    #
    # It also fixes a SECOND, independent defect, and that one writes to disk:
    # registering any `erb` profile arms `strip_introduced_trailing_whitespace`
    # and `reindent.reindent_spliced_lines`, both gated on `supported()` and
    # both reading the profile's grammar over the REAL file. Under the Ruby
    # grammar `_leaf_spans` shreds markup into identifiers with the whitespace
    # falling BETWEEN leaves, so the pass strips significant trailing spaces
    # out of `<pre>` and `<textarea>` — changing what the page renders. Under
    # this grammar a markup region is ONE `content` leaf, so it is protected
    # exactly the way a Go raw string is.
    #
    # The cost, stated plainly: this grammar is blind to the Ruby INSIDE a tag,
    # because `code` is one opaque token. That is what `code_view` is for.
    "erb": _Profile(
        tree_sitter_embedded_template.language,
        # EMPTY, and deliberately not a copy of Ruby's. On a template tree the
        # stranded-keyword check is VACUOUS — there are no `identifier` nodes
        # for a keyword to surface in — so Ruby's carefully-tuned set would be
        # dead weight that reads as coverage. The check is not lost: it runs
        # against the code view under the `ruby` profile, which is where the
        # identifiers actually are.
        frozenset(),
        frozenset(),
        # `literal_residues` is empty here for the same reason and is likewise
        # not lost: `("when", "pattern")` is inherited from `ruby` through
        # `code_view`. That entry is the one that matters most, and without it
        # `<% when <read> %>` ships as `<% when true %>` with the flag key
        # already gone from the file — #2692's permanent-half-cleanup class,
        # re-opened for `.erb`. A template does not parse as a `case` under any
        # grammar in play, so nothing on THIS side of the split could see it.
        #
        # `ruby_fold` is deliberately NOT extended to `erb`. `piranha_runner`
        # gates `_fold_ruby_literals` on `language == "ruby"` and it parses
        # with the Ruby grammar, so it cannot see a template's `when` either.
        # The consequence is that for `.erb` a `when` residue is REFUSED rather
        # than folded — loud instead of silent, which is the right side to fail
        # on, and the same state Go was in before #2692. Do not quietly extend
        # the fold to close it; extending it means teaching that module the
        # template grammar, which is its own decision.
        code_view="ruby",
    ),
    "csharp": _Profile(
        tree_sitter_c_sharp.language,
        # No separate `type_identifier` in this grammar (verified against
        # node-types.json) — every name, value or type, parses as `identifier`.
        frozenset({"identifier"}),
        frozenset({"true", "false", "null", "class", "return", "if", "else", "void",
                   "new", "this", "base", "static", "readonly", "using", "namespace"}),
        # Unlike Java, an unreachable statement after a folded if/else (both
        # arms return) is CS0162 — a WARNING, not a compile error. Verified
        # with `dotnet run` on the exact shape java's `_unreachable_statements`
        # exists for: the program still compiled and ran. So C# joins
        # Go/Swift/Python here rather than Java — this gate does not need to
        # delete anything to keep the output compiling. (TypeScript was in that
        # list until #2807 measured its standard gate and moved it across; the
        # `dotnet run` measurement above is what keeps C# on this side, and it
        # is the measurement, not the CS0162 severity word, that decides.)
        unreachable_is_fatal=False,
    ),
}

#: Boolean-literal node types, by the name the grammar gives them.
_BOOLEAN_LITERALS = frozenset({"true", "false"})


def supported(language: str) -> bool:
    """Whether Gate 1 can judge a rewrite in ``language``."""
    return language in _TS_LANGUAGES or language in _PROFILES


@lru_cache(maxsize=None)
def _parser(language: str) -> Parser:
    try:
        profile = _PROFILES[language]
    except KeyError:
        raise ValueError(f"unsupported language {language!r}") from None
    return Parser(Language(profile.grammar()))


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _parse(source: str, language: str) -> Node:
    return _parser(language).parse(source.encode("utf-8")).root_node


@lru_cache(maxsize=4)
def _code_view(source: str, language: str) -> str:
    """The host-language code inside a template's tags, and nothing else.

    Every ``code`` token of ``source``'s template tree, in SOURCE ORDER, joined
    with newlines. Markup vanishes entirely, so the HTML noise that makes a
    bare Ruby grammar unworkable over a template (see the ``erb`` profile) is
    simply not there: measured over 162 real templates, every one produces a
    code view that is clean Ruby — 0 ERROR/MISSING nodes and 0 stranded
    keywords — against a raw-template baseline of mean 4.3, max 15, never zero.

    It composes because ERB's tags are pieces of one Ruby program:
    ``if``/``else``/``end`` across separate tags reassemble, ``<%= expr %>`` is
    an expression statement, ``do |f|`` … ``end`` pairs up, and so does
    ``case``/``when``. A comment directive carries a ``comment`` child rather
    than a ``code`` one and so contributes nothing, as does ``<%% … %>``.

    **This is a DERIVED string and its byte offsets are its own.** Nothing that
    edits the file may be pointed at it — :func:`_leaf_spans`,
    :func:`_construct_at` and :func:`_construct_spans` all reason about offsets
    into the REAL file and are consumed by
    :func:`strip_introduced_trailing_whitespace` and by
    :mod:`flag_cleanup.reindent`, so aiming those here would make the
    whitespace pass index a different document. Only the three checks that ask
    a question ABOUT THE CODE (the error count, the stranded keywords and the
    literal residues) look at it; the structural half keeps parsing real bytes.

    Source order is taken explicitly rather than from :func:`_walk`, which is a
    stack and therefore visits siblings in reverse. Getting that wrong would
    silently reverse the program — ``end`` before ``if`` — turning every code
    view into a pile of errors on both sides of the differential, which cancels
    and reads exactly like a working check.

    Cached on the same reasoning and with the same bound as
    :func:`_root_node`: one pass asks three independent questions of one file's
    ``before`` and ``after``.
    """
    root = _parse(source, language)
    data = source.encode("utf-8")
    tags = sorted(
        (node for node in _walk(root) if node.type == "code"),
        key=lambda node: node.start_byte,
    )
    return "\n".join(
        data[node.start_byte : node.end_byte].decode("utf-8", "replace") for node in tags
    )


def _leaf_spans(source: str, language: str) -> list[tuple[int, int]]:
    """Byte ranges of every childless node — the file's actual tokens.

    Ordinary whitespace between tokens belongs to no leaf. Whitespace INSIDE a
    leaf is part of a literal or a comment: a multi-line template string, a Java
    text block, a Go raw string, a block comment. That is the distinction
    :func:`strip_introduced_trailing_whitespace` needs, and asking the parse tree
    for it costs nothing and names no node types — the enumerations this
    codebase has had leak on it three times already.
    """
    return [
        (node.start_byte, node.end_byte)
        for node in _walk(_root_node(source, language))
        if node.child_count == 0
    ]


@lru_cache(maxsize=4)
def _root_node(source: str, language: str) -> Node:
    """The parse tree root, from whichever of the two parsers owns ``language``.

    Cached because a single pass over one file asks several independent
    questions of the same tree — :func:`_leaf_spans`, :func:`_construct_at` and
    :func:`_construct_spans` each want it, and :mod:`flag_cleanup.reindent` asks
    once per changed hunk. Four entries is deliberately just enough to hold one
    file's ``before`` and ``after`` without the interleaving evicting either;
    holding a node holds its whole tree, so this is a bound, not a store.
    """
    return (
        ts_syntax._parse(source, language)
        if language in _TS_LANGUAGES
        else _parse(source, language)
    )


def _construct_at(source: str, language: str, start: int, beyond: int) -> str | None:
    """Type of the innermost node that BEGINS at ``start`` and ends past ``beyond``.

    "The construct this line opens" — for an ``if`` header, the ``if`` statement
    itself. Naming no type and reading one off the tree is the same trade
    :func:`_leaf_spans` makes: the caller compares what it finds here against
    what it finds in the rewritten file, so the type is data, never a list to
    keep current.

    INNERMOST, and found by descending rather than by comparing sizes, because a
    body container routinely begins at the very same byte. Go's
    ``statement_list``, Ruby's ``body_statement``, Swift's ``statements`` and
    Python's ``block`` all start at their block's FIRST STATEMENT, which IS the
    header whenever the header is the first thing in an enclosing block — and in
    Ruby the container and the ``if`` even share an end byte, so "the smaller of
    the two" does not separate them while "the deeper of the two" does.

    ``None`` when the line opens nothing that outlives it: a ``} else {`` line
    begins with the brace closing the block above it, and a line holding a
    complete statement is over before ``beyond``.
    """
    node, found = _root_node(source, language), None
    while True:
        for child in node.children:
            if child.start_byte <= start < child.end_byte:
                if child.start_byte == start and child.end_byte > beyond:
                    found = child
                node = child
                break
        else:
            return found.type if found is not None else None


def _construct_spans(source: str, language: str, kind: str) -> list[tuple[int, int]]:
    """Byte ranges of every node whose type is ``kind``."""
    return [
        (node.start_byte, node.end_byte)
        for node in _walk(_root_node(source, language))
        if node.type == kind
    ]


def strip_introduced_trailing_whitespace(before: str, after: str, language: str) -> str:
    """Remove trailing whitespace THIS transform stranded, and nothing else.

    Deleting a node that does not begin its own line leaves the whitespace that
    separated it from its predecessor at the end of that line. TypeScript's
    ``delete_else_if_false`` is the rule that produces it — ``} else if (…) { … }``
    becomes ``} `` — and it cannot be fixed in the rule. Piranha will not match a
    query rooted at the outer ``if`` under the edge graph's ``Parent`` scope (it
    does as a *seed* rule, which is what makes this easy to misdiagnose), and
    ``replace_node`` cannot name a capture from a filter's ``enclosing_node``.
    Both were measured against the real pipeline, not inferred. The space lies
    outside every node the rule can target, so it has to be removed here.

    Two conditions, both required, which together mean this can only ever delete
    an artefact:

    * **The line must not appear verbatim in the input.** A line whose trailing
      whitespace the customer wrote is present before and after, so it is left
      exactly as found. This is what keeps the pass from reformatting code the
      removal never touched — the same trap that made whole-file blank-line
      collapsing unacceptable for Python.
    * **The whitespace must not sit inside a token.** Trailing spaces inside a
      multi-line template literal are *data*: stripping them changes what the
      program prints. Decided from the parse tree (see :func:`_leaf_spans`), not
      from a list of string-ish node types.

    Language-general and gated on nothing: a language whose rules stand no node
    mid-line simply never produces a candidate line, so the pass is a no-op.
    """
    if after == before or not supported(language):
        return after
    kept = {line.rstrip("\r\n") for line in before.splitlines(keepends=True)}
    leaves: list[tuple[int, int]] | None = None
    out: list[str] = []
    offset = 0
    for raw in after.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        ending = raw[len(body) :]
        stripped = body.rstrip()
        if stripped == body or body in kept:
            out.append(raw)
            offset += len(raw.encode("utf-8"))
            continue
        # Parsed once, and only if some line is actually a candidate.
        if leaves is None:
            leaves = _leaf_spans(after, language)
        start = offset + len(stripped.encode("utf-8"))
        inside_token = any(begin <= start < end for begin, end in leaves)
        out.append(raw if inside_token else stripped + ending)
        offset += len(raw.encode("utf-8"))
    return "".join(out)


#: The three runs a single-line keyed-entry deletion leaves behind. The engine
#: swallows the entry and its separator but not the space that preceded the
#: entry, so what remains is the separator followed by two spaces, or an
#: opening bracket followed by two spaces (first entry deleted, or the object
#: emptied). Alternation order matters: an emptied pair must be tried before
#: the bare opening-bracket form.
_INTRODUCED_RUN = re.compile(r"([,;])[ \t]{2,}|([\[({])[ \t]{2,}([\])}])|([\[({])[ \t]{2,}")


def _collapse_run(match: re.Match) -> str:
    if match.group(1) is not None:
        return match.group(1) + " "
    if match.group(2) is not None:
        return match.group(2) + match.group(3)
    return match.group(4) + " "


def collapse_introduced_double_spaces(before: str, after: str, language: str) -> str:
    """Collapse the double space a keyed-entry deletion leaves mid-line.

    ``{ 'a': 1, 'k': 2 }`` with the second entry deleted comes back from the
    engine as ``{ 'a': 1,  }``-shaped residue — see :data:`_INTRODUCED_RUN`
    for the three forms. Cosmetic in every language, and fixed so a
    ``no-multi-spaces`` lint in the customer's repository does not flag this
    tool's output.

    Same two conditions as :func:`strip_introduced_trailing_whitespace`, for
    the same reasons: a line present verbatim in ``before`` is the customer's
    and is left alone, and a run inside a token (``'a,  b'``) is data.
    """
    if after == before or not supported(language):
        return after
    kept = {line.rstrip("\r\n") for line in before.splitlines(keepends=True)}
    leaves: list[tuple[int, int]] | None = None
    out: list[str] = []
    offset = 0
    for raw in after.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        if body in kept or not _INTRODUCED_RUN.search(body):
            out.append(raw)
            offset += len(raw.encode("utf-8"))
            continue
        if leaves is None:
            leaves = _leaf_spans(after, language)
        spans = leaves

        def _replace(match: re.Match, base: int = offset) -> str:
            start = base + len(body[: match.start()].encode("utf-8"))
            end = base + len(body[: match.end()].encode("utf-8"))
            # Full containment, not mere overlap: the separator/bracket the
            # match starts on (`,`, `;`, `{`, ...) is itself a childless leaf
            # one byte wide, so a partial-overlap test is trivially true for
            # every structural match and the pass would never collapse
            # anything. A run is DATA only when one leaf's span swallows the
            # whole match (e.g. a string_fragment covering 'a,  b') — a
            # narrow punctuation leaf touching just the match's edge does not
            # count.
            inside_token = any(s <= start and end <= e for s, e in spans)
            return match.group(0) if inside_token else _collapse_run(match)

        out.append(_INTRODUCED_RUN.sub(_replace, body) + raw[len(body) :])
        offset += len(raw.encode("utf-8"))
    return "".join(out)


#: A line body followed by MORE THAN ONE carriage return and a newline — the
#: residue a keyed-entry deletion leaves in a CRLF file. The engine treats
#: `\n` as the line terminator and `\r` as ordinary whitespace, so deleting a
#: whole line takes that line's `\n` (with `delete_consecutive_new_lines`) and
#: leaves its `\r` stranded on the end of the line above.
_INTRODUCED_CR_RUN = re.compile(r"([^\r\n]*)\r\r+\n")


def collapse_introduced_carriage_returns(before: str, after: str, language: str) -> str:
    """Drop the stray carriage return an entry deletion leaves in a CRLF file.

    A NEW residue class, and one the read/statement path does not have: those
    rules delete a node mid-line and the engine keeps the line, so its ending is
    untouched. An entry deletion takes a WHOLE line, and the engine's idea of a
    line ends at `\n` — the `\r` in front of it is just whitespace to the
    grammar. So `…},\r\n…'old-checkout': …,\r\n` comes back as `…},\r\r\n`,
    a line whose ending git renders as a change and most editors render as a
    stray character (measured on TypeScript and Python).

    Same two conditions as :func:`strip_introduced_trailing_whitespace` and
    :func:`collapse_introduced_double_spaces`, for the same reasons:

    * **The run must not appear verbatim in the input.** A file that already
      had a doubled carriage return on some line keeps it — this pass fixes
      what the transform made, never what it found.
    * **The run must not sit inside a token.** A lone `\r` inside a multi-line
      template literal or a raw string is *data*, and decided from the parse
      tree rather than from a list of node types.
    """
    if after == before or not supported(language):
        return after
    leaves: list[tuple[int, int]] | None = None

    def _replace(match: re.Match) -> str:
        nonlocal leaves
        if match.group(0) in before:
            return match.group(0)
        if leaves is None:
            leaves = _leaf_spans(after, language)
        start = len(after[: match.end(1)].encode("utf-8"))
        inside_token = any(begin <= start < end for begin, end in leaves)
        return match.group(0) if inside_token else match.group(1) + "\r\n"

    return _INTRODUCED_CR_RUN.sub(_replace, after)


def count_syntax_errors(source: str, language: str) -> int:
    """Number of ERROR / MISSING nodes in ``source``.

    For a template language this is the sum of BOTH halves, and both earn their
    place: the template tree sees a tag that lost its ``%>``, and the code view
    sees broken Ruby inside a tag. Neither can see the other's — ``code`` is
    one opaque token to the template grammar, and the code view has thrown the
    tags away by the time it is parsed.

    Summing means a FALL in one half could in principle mask a RISE in the
    other, since the caller compares one number. Left as a sum deliberately,
    because the masking needs the template half to fall and it effectively
    cannot: a template that reaches this gate parsed cleanly on the way in
    (measured, 162 of 162 real templates score zero structural errors), so the
    template term is almost always 0 and 0 does not fall. Splitting the
    comparison would be the fix if that ever stops holding — what makes it safe
    today is a property of the input, not of the arithmetic.
    """
    if language in _TS_LANGUAGES:
        return ts_syntax.count_syntax_errors(source, language)
    errors = sum(
        1
        for node in _walk(_parse(source, language))
        if node.type == "ERROR" or node.is_missing
    )
    view = _PROFILES[language].code_view
    if view is not None:
        errors += count_syntax_errors(_code_view(source, language), view)
    return errors


def source_is_unreadable(source: str, language: str) -> bool:
    """Whether this file must be SKIPPED because the gate cannot fully parse it.

    "Do not rewrite a file you cannot read" is the engine's own rule — it is
    what `--on-parse-error abort` says, and the quarantine path in
    `piranha_runner` exists to soften it to one file rather than a whole flag.
    This applies the same rule with the GATE's grammar, for the one language
    where the gate's grammar is the stricter of the two.

    Dart, and Dart only. `final bool await = client.boolVariation(…)` with any
    read of `await` is valid Dart — `dart analyze` reports no issues — but the
    two grammars in play fail on it differently, and the combination corrupts:

    * the engine's 0.2.0 parses `if (await)` as an await-expression with no
      operand, so the local appears to have NO occurrences at all. The built-in
      `delete_variable_declaration` then fires and the read is left behind,
      pointing at a name that no longer exists (`Undefined name 'await'`).
    * the gate's 0.1.0 emits a MISSING `identifier` — BEFORE and after — so the
      differential error count in :func:`transform_broke_syntax` never moves and
      Gate 1 passes the rewrite through.

    Neither half is visible on its own; only the pair is, and only from here.
    `await` is the only affected name: every other Dart built-in identifier and
    contextual keyword (`on`, `show`, `required`, `sync`, `yield`, `when`, …)
    was swept and cleans normally, as does an `await` declaration that is never
    read — which is why this asks about READABILITY rather than carrying a list
    of words. A name list would refuse the seventeen shapes piranha#77 just
    fixed, trading a rare silent corruption for a common silent non-removal.

    Absolute rather than differential, which for a grammar this strict would
    normally be far too blunt — the TS family carries pre-existing ERROR nodes
    routinely, and that is exactly why `unreadable_source_is_skipped` is off
    there. It is affordable here because the Dart grammar is quiet on real
    code: **0 of 4,259 `.dart` files** across the Flutter SDK's `packages/` and
    `dev/` trees produce a single ERROR or MISSING node, alongside the 1,310
    third-party files measured when Dart support landed. So the cost of this
    check on a real repository is expected to be nothing at all, and its
    benefit is that the one shape it does catch is a file that would otherwise
    stop compiling.

    A skipped file is reported, never swallowed: it still holds a live read, so
    it reaches `TransformOutcome.unprocessable` and from there the pull-request
    caveat and the run log, exactly as a file the ENGINE could not parse does.
    """
    profile = _PROFILES.get(language)
    if profile is None or not profile.unreadable_source_is_skipped:
        return False
    return count_syntax_errors(source, language) > 0


def _stranded_keywords(source: str, language: str) -> int:
    """How many keyword-shaped names parse as plain identifiers in ``source``.

    A keyword can only surface as an identifier when the parse has degraded: in
    valid source ``return`` belongs to a return statement and ``false`` is its
    own literal node. So this counts symptoms of a broken parse that the grammar
    was too permissive to call an error — tree-sitter grammars are deliberately
    error-tolerant, and an ERROR count alone misses a surprising amount of
    broken output.
    """
    profile = _PROFILES[language]
    # A template tree has no `identifier` nodes at all, so asking it this
    # question is vacuous. The identifiers are in the tags, so the code view is
    # what gets asked — under the HOST profile, whose keyword set was tuned for
    # the language actually written there.
    if profile.code_view is not None:
        return _stranded_keywords(_code_view(source, language), profile.code_view)
    source_bytes = source.encode("utf-8")
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.type in profile.identifier_nodes
        and source_bytes[node.start_byte : node.end_byte].decode("utf-8", "replace")
        in profile.keywords
    )


def _native_parse_fails(source: str, language: str) -> bool:
    """Whether a real compiler for ``language`` rejects ``source``.

    Only Python has one to hand, and it is not a luxury there: the three ways a
    Python rewrite breaks — over-indent, dedent out of the enclosing block, and
    a block emptied of every statement — are all accepted without complaint by
    tree-sitter-python, so :func:`count_syntax_errors` returns 0 for source
    CPython will not run. Measured, in that order, against the outputs the fold
    rules produce when they are wrong.

    ``compile`` rather than ``ast.parse``, and the difference is load-bearing:
    **`ast.parse` accepts a `return` at module level.** "`return` outside
    function" is raised by the compiler, not the parser, so a fold that
    dedented a `return` clean out of its function — one of the three failure
    modes above, and the most likely one — sailed through `ast.parse`. Caught
    by `tests/test_syntax.py`, which is the reason that file exists; the
    original hand-check had used a case whose *next* line was also
    over-indented, so the refusal came from the wrong mechanism.

    ``ValueError`` joins ``SyntaxError`` because source containing a NUL byte
    raises it instead — rare, but it reaches the same conclusion.
    """
    if not _PROFILES[language].native_parser:
        return False
    try:
        compile(source, "<gate>", "exec")
    except (SyntaxError, ValueError):
        return True
    return False


def _literal_residues(source: str, language: str) -> int:
    """How many boolean literals in ``source`` sit where a fold stopped short.

    A residue measure, not a syntax check: the source parses perfectly well.
    What it catches is the fold that started and did not finish — the flag read
    replaced by ``True``, and then either the ``if`` around it left standing
    because the branch is too big to move, or the name it was bound to left
    standing because inlining it could not be proved safe. Both are worse than
    no output at all, because the flag key is gone from the file: every other
    check in this tool, and every later run, sees a clean removal, so the
    leftover is permanent.

    This is the residue the metamorphic suite already measures per case, raised
    to a gate for the one language whose rules can produce it.
    """
    profile = _PROFILES[language]
    # The one dispatch of the three that is load-bearing rather than tidy. A
    # template does not parse as a `case` under any grammar in play, so on this
    # side of the split there is nothing for `("when", "pattern")` to match —
    # and without it `<% when <read> %>` ships as `<% when true %>` with the
    # flag key already gone from the file, so no later run can revisit it. The
    # code view restores Ruby's entry verbatim.
    if profile.code_view is not None:
        return _literal_residues(_code_view(source, language), profile.code_view)
    if not profile.literal_residues:
        return 0
    wanted = dict(profile.literal_residues)
    booleans = profile.boolean_nodes or _BOOLEAN_LITERALS
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.type in wanted
        and (condition := node.child_by_field_name(wanted[node.type])) is not None
        and _through_wrappers(condition, profile).type in booleans
    )


def _through_wrappers(node: Node, profile: _Profile) -> Node:
    """``node``, descended through any transparent wrapper this grammar adds.

    Only follows a wrapper holding EXACTLY ONE named child: `(1)` parenthesises
    one expression, but Go's `case 1, 2:` is an `expression_list` of two and is
    not a single value at all, so descending into it would report whichever
    child happened to come first. A multi-child wrapper is therefore returned
    as-is and simply fails the literal test above.
    """
    while node.type in profile.transparent_nodes and node.named_child_count == 1:
        node = node.named_children[0]
    return node


def _null_literal_comparisons(source: str, language: str) -> int:
    """Boolean literals compared against ``null`` — output that does not compile.

    The residue of inlining a BOXED binding that guards itself against null,
    which is the ordinary reason anyone writes `Boolean` rather than `boolean`:

        Boolean on = client.boolVariation("KEY", c, false);
        if (on != null && on) { … }          ->   if (true != null) { … }

    `error: bad operand types for binary operator '!='` — a primitive against
    `<null>`. Like :func:`_literal_statements` this is a syntax check wearing a
    residue check's clothes: it PARSES, so neither the engine's self-check nor
    the differential error count can see it, and it is a type error one layer
    below everything else Gate 1 reaches — the `await true` blind spot again.

    REFUSED rather than repaired, deliberately. `true != null` reduces to
    `true`, but the `if` around it would then want collapsing and the branch
    below that — the whole cascade the engine has already finished and left.
    Re-implementing it here to salvage one shape is a worse trade than one
    uncleaned file, and a refusal is reported rather than silent.

    Either operand order and both operators, because the check is on the PAIR:
    `null == false` is the false treatment's version and a codebase that writes
    Yoda comparisons produces it.
    """
    profile = _PROFILES[language]
    if not profile.null_comparison_is_fatal:
        return 0
    booleans = profile.boolean_nodes or _BOOLEAN_LITERALS
    total = 0
    for node in _walk(_parse(source, language)):
        if node.type != "binary_expression":
            continue
        operator = node.child_by_field_name("operator")
        if operator is None or operator.text not in (b"==", b"!="):
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            continue
        sides = {
            _through_wrappers(left, profile).type,
            _through_wrappers(right, profile).type,
        }
        if "null_literal" in sides and sides & booleans:
            total += 1
    return total


def _literal_statements(source: str, language: str) -> int:
    """How many boolean literals in ``source`` stand ALONE as a statement.

    A syntax check wearing a residue check's clothes. ``true;`` parses — that
    is the whole problem — but it does not COMPILE in Java or Go, so unlike
    :func:`_literal_residues` this is not a fold that stopped short; it is
    output the customer's build rejects outright.

    It exists because a flag read is not always read for its value. A warm-up
    call made for the SDK's exposure event, and far more commonly a Mockito
    ``verify(client).boolVariation("KEY", ctx, false);``, are whole statements
    whose only content is the read. Replacing the read with a literal leaves
    the literal holding the statement up by itself.

    Nothing else here can see it. Piranha's own post-transform check and this
    module's :func:`count_syntax_errors` both ask tree-sitter, and tree-sitter
    is content: ``(expression_statement (true))``, no ERROR node, in both
    grammars. C# is the instructive contrast — its grammar DOES reject the
    shape, so the engine aborts and the language never reaches this gate, which
    is why the tool's only fixture for a mock-framework read was written for
    the one language that could not exhibit the bug.

    Differential like everything else: a file that somehow already contained
    one is judged on the rest, and only a rewrite that ADDS one is refused.
    """
    profile = _PROFILES[language]
    if not profile.literal_statement_is_fatal:
        return 0
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.type == "expression_statement"
        and node.named_child_count == 1
        and node.named_children[0].type in _BOOLEAN_LITERALS
    )


def _lost_side_effects(source: str, language: str) -> int:
    """Count the side-effect-bearing fields in ``source`` that must be preserved.

    Differential like everything else here, and the direction matters: a
    rewrite may not REDUCE this count. Only Go has an entry — its `if`
    initializer, which executes regardless of the condition, so folding the
    `if` away silently drops it.

    Deliberately a count of the FIELD rather than a check on the surrounding
    statement: it is the initializer that must survive, and counting it
    directly makes the check indifferent to which rule removed it. That is what
    lets one gate cover both a rule of ours and a built-in of the engine's.
    """
    profile = _PROFILES[language]
    if not profile.preserved_fields:
        return 0
    wanted = dict(profile.preserved_fields)
    return sum(
        1
        for node in _walk(_parse(source, language))
        if node.type in wanted and node.child_by_field_name(wanted[node.type]) is not None
    )


#: Statements after which control cannot continue in the same block. Small and
#: conservative on purpose: this set only ever makes the check see LESS, and
#: the check's only power is to refuse, so an omission costs a missed refusal
#: while a wrong entry would refuse a valid rewrite.
_TERMINAL_STATEMENTS = frozenset({"return_statement", "throw_statement"})


#: Loops whose ``condition`` the fold can turn into a constant. An
#: ``enhanced_for_statement`` (``for (X x : xs)``) has no condition to fold and
#: is deliberately absent, as is any loop form the grammar spells differently.
_CONDITION_LOOPS = frozenset({"while_statement", "for_statement", "do_statement"})

#: Constructs that swallow an unlabelled ``break``, so one inside them does not
#: escape an enclosing loop. Measured: a ``break`` bound by a nested ``switch``
#: leaves the outer ``while (true)`` infinite, and ``javac`` says so.
_BREAKABLE = _CONDITION_LOOPS | frozenset(
    {"enhanced_for_statement", "switch_expression", "switch_statement"}
)


@dataclass(frozen=True)
class _Dialect:
    """The node names the unreachable-statement pass reads out of one grammar.

    A second registry beside :data:`_PROFILES` rather than more fields on it,
    because the two do not partition the languages the same way: `ts`/`tsx`/`js`
    have no profile row ON PURPOSE (see :data:`_TS_LANGUAGES`) and still need
    this pass, while Java and Dart have a row each and share one vocabulary.
    Keying the pass off the profile alone is precisely what left the TypeScript
    family unable to reach it (#2807).

    ``parse`` is the grammar's own parser rather than :func:`_parse`, which can
    only find a grammar through a profile.

    ``blocks`` is what abruptness recurses INTO; ``statement_lists`` is what can
    hold a FOLLOWING sibling. Java spells both with one node type, which is why
    they were one thing until TypeScript arrived — there a module body and a
    ``case:`` arm hold statements without being blocks.
    """

    parse: Callable[[str, str], Node]
    blocks: frozenset[str]
    statement_lists: frozenset[str]
    #: Nodes sitting BETWEEN an ``if``'s ``alternative`` field and the statement
    #: it holds. Empty where the field points straight at the statement.
    else_wrappers: frozenset[str]
    #: What a label parses as, on both ``break`` and ``labeled_statement``.
    label_nodes: frozenset[str]
    #: Constructs that swallow an unlabelled ``break``, so one inside them does
    #: not escape an enclosing loop.
    breakable: frozenset[str]
    #: Whether a loop the fold made DEAD (``while (false)``) is also rejected.
    #: Separate from the rest because it is the one part of this pass the
    #: toolchains disagree about — see :data:`_TS_DIALECT`.
    dead_loop_is_fatal: bool


#: Java's, and Dart's: the Dart profile records that every node name here is
#: present and identical in that grammar, which is why it needed the flag and
#: no new code.
_JAVA_DIALECT = _Dialect(
    parse=_parse,
    blocks=frozenset({"block"}),
    statement_lists=frozenset({"block"}),
    else_wrappers=frozenset(),
    label_nodes=frozenset({"identifier"}),
    breakable=_BREAKABLE,
    dead_loop_is_fatal=True,
)

#: The TypeScript family's. Four node names differ from Java's and each was
#: measured against the grammar rather than read off a reference:
#:
#: * a block is ``statement_block``;
#: * ``alternative`` points at an ``else_clause`` that wraps the arm, so
#:   reading the field without unwrapping finds a node type abruptness knows
#:   nothing about and silently strands nothing;
#: * a module body (``program``) and a ``case:`` arm (``switch_case`` /
#:   ``switch_default``) hold statements directly — the same containers
#:   :data:`ts_syntax._STATEMENT_CONTAINERS` had to learn about for ASI fusion,
#:   for the same structural reason;
#: * a label is a ``statement_identifier``, not an ``identifier``, and reading
#:   the wrong one makes a labelled ``break`` look unlabelled — which would
#:   delete a statement the customer's build considers reachable.
#:
#: ``dead_loop_is_fatal`` is OFF, and that is the one place this dialect stops
#: short of Java's. Membership of this pass at all was decided the way Dart's
#: was, by running the real toolchain: ``no-unreachable`` is in
#: ``eslint:recommended`` and is an ERROR, and it fires on every shape the true
#: treatment emits — a stranded statement after a folded ``if``/``else``, after
#: ``while (true)``, after ``do … while (true)``, at a module's top level and
#: inside a ``case:``. It says NOTHING about ``while (false) { … }`` or
#: ``for (…; false; …) { … }``, which is what the FALSE treatment emits;
#: ``no-unreachable-loop`` is the rule that would, and it is not in the
#: recommended set. So deleting those loops would be an unrequested edit and
#: refusing them would trade a working pull request for no pull request, which
#: is the wrong way round.
_TS_DIALECT = _Dialect(
    parse=ts_syntax._parse,
    blocks=frozenset({"statement_block"}),
    statement_lists=frozenset(
        {"statement_block", "program", "switch_case", "switch_default"}
    ),
    else_wrappers=frozenset({"else_clause"}),
    label_nodes=frozenset({"statement_identifier"}),
    # `for_in_statement` is this grammar's spelling of BOTH `for…of` and
    # `for…in`, and is the analogue of Java's `enhanced_for_statement`. There is
    # no switch EXPRESSION in this language, so Java's third entry has no twin.
    breakable=_CONDITION_LOOPS | frozenset({"for_in_statement", "switch_statement"}),
    dead_loop_is_fatal=False,
)


def _unreachable_dialect(language: str) -> _Dialect | None:
    """How to read ``language``'s grammar for this pass, or None if it is off there.

    The single place that decides whether unreachable code is fatal in a
    language. For a profile language the decision stays on the profile, next to
    that language's other measured decisions; for the TypeScript family, which
    has no profile, membership IS the branch below. Both routes end here so the
    count and the repair can never disagree about a language — which is the way
    a check of this shape usually rots.
    """
    profile = _PROFILES.get(language)
    if profile is not None:
        return _JAVA_DIALECT if profile.unreachable_is_fatal else None
    return _TS_DIALECT if language in _TS_LANGUAGES else None


def _loop_condition_literal(loop: Node) -> str | None:
    """``"true"``/``"false"`` when ``loop``'s condition is that literal, else None.

    Descends only through PARENTHESES, never through an arbitrary single-child
    wrapper: `while (!false)` means `while (true)`, and a generic descent would
    read the `false` under the negation and call the loop dead. Java spells a
    `while`'s condition as a `parenthesized_expression` and a `for`'s as the
    bare expression, so both need handling.
    """
    condition = loop.child_by_field_name("condition")
    while condition is not None and condition.type == "parenthesized_expression":
        named = [c for c in condition.named_children if c.type != "comment"]
        condition = named[0] if len(named) == 1 else None
    if condition is None or condition.type not in _BOOLEAN_LITERALS:
        return None
    return condition.type


def _escapes(loop: Node, dialect: _Dialect) -> bool:
    """Whether a ``break`` inside ``loop`` exits ``loop`` itself.

    JLS 14.21: a loop whose condition is the constant ``true`` completes
    normally exactly when a reachable ``break`` exits it. Both halves were
    measured with ``javac``, and both directions matter — claiming an escape
    that is not there ships a build the customer cannot compile, while missing
    one deletes a statement their build considers reachable.

    An unlabelled ``break`` binds to the innermost enclosing loop or switch, so
    one swallowed by a nested construct is not an escape. A labelled ``break``
    is an escape only when the label is ``loop``'s OWN — ``break outer;`` aimed
    at an enclosing statement transfers control past this loop rather than out
    of its bottom, and the statement after it stays unreachable.
    """
    own_labels: set[bytes] = set()
    parent = loop.parent
    while parent is not None and parent.type == "labeled_statement":
        label = next(
            (c for c in parent.named_children if c.type in dialect.label_nodes), None
        )
        if label is not None and label.text is not None:
            own_labels.add(label.text)
        parent = parent.parent

    for node in _walk(loop):
        if node.type != "break_statement":
            continue
        label = next(
            (c for c in node.named_children if c.type in dialect.label_nodes), None
        )
        if label is not None:
            if label.text in own_labels:
                return True
            continue
        enclosing = node.parent
        while enclosing is not None and enclosing.type not in dialect.breakable:
            enclosing = enclosing.parent
        if enclosing is not None and enclosing.id == loop.id:
            return True
    return False


def _completes_abruptly(node: Node | None, dialect: _Dialect) -> bool:
    """Whether control can never fall out of the bottom of ``node``.

    Mirrors the shape of JLS 14.21 rather than implementing it: a block is
    abrupt if its last statement is, and an ``if`` is abrupt only when it has an
    ``else`` AND both arms are — which is exactly the rule that makes the
    statement after an ``if``/``else`` unreachable.

    A loop the fold made INFINITE is abrupt for the same reason and was the
    same failure one construct over (#2701): `while (<read>)` on the true
    treatment becomes `while (true)`, and whatever followed it stops compiling.
    """
    if node is None:
        return False
    # An `else_clause` is not a statement, it CONTAINS one. Unwrapping here
    # rather than at the `alternative` lookup keeps the recursion's own
    # `else if` step covered by the same line.
    while node.type in dialect.else_wrappers:
        inner = [c for c in node.named_children if c.type != "comment"]
        if len(inner) != 1:
            return False
        node = inner[0]
    if node.type in _TERMINAL_STATEMENTS:
        return True
    if node.type in dialect.blocks:
        statements = [c for c in node.named_children if c.type != "comment"]
        return bool(statements) and _completes_abruptly(statements[-1], dialect)
    if node.type == "if_statement":
        return _completes_abruptly(
            node.child_by_field_name("consequence"), dialect
        ) and _completes_abruptly(node.child_by_field_name("alternative"), dialect)
    if node.type in _CONDITION_LOOPS:
        return _loop_condition_literal(node) == "true" and not _escapes(node, dialect)
    return False


def _unreachable_statements(source: str, language: str) -> int:
    """Statements in ``source`` that a preceding statement makes unreachable.

    Folding a flag out of the last arm of a chain turns ``else if (true)`` into
    a plain ``else``. Both arms then return, so anything after the chain becomes
    unreachable — and in Java that is a compile **error** (JLS 14.21), not a
    warning:

        CheckoutRouter.java:30: error: unreachable statement

    Which makes it the failure class this tool exists to avoid: a
    ready-for-review pull request whose branch does not build. It reached a real
    PR against a real repository before this check existed, and no other gate
    could see it — the output parses, strands no keyword and leaves no literal.

    Scoped by :func:`_unreachable_dialect`, which currently admits Java, Dart
    and the TypeScript family. Elsewhere the identical rewrite compiles and
    behaves correctly, and refusing there would trade a working pull request
    for no pull request — the point of this gate is that these languages have
    no working version to trade.

    **TypeScript was on the wrong side of that line for as long as the line
    existed (#2807),** on the strength of a claim recorded here that ESLint
    "says nothing". It does: ``no-unreachable`` is in ``eslint:recommended``
    and is an ERROR, so the pull request this Action opens did not go green.
    The lesson is the one the Dart profile already records — decide membership
    by RUNNING the language's standard gate, not by reading a severity word.

    Counting rather than flagging keeps it differential with everything else in
    this module: source that already had unreachable code (it happens, behind
    ``if (DEBUG)``-style constants) stays eligible, and only a rewrite that adds
    to the count is refused.
    """
    return len(_unreachable_statement_nodes(source, language)) + _unrepairable_loops(
        source, language
    )


def _unrepairable_loops(source: str, language: str) -> int:
    """Dead loops that must be REFUSED rather than deleted (#2701).

    `for (init; false; update) <body>` never runs its body — but its
    INITIALISER does, so deleting the statement would destroy a side effect,
    which is precisely what :func:`_lost_side_effects` exists to prevent
    elsewhere. There is no safe repair (removing only the body leaves
    `for (…; false; …) { }`, which `javac` rejects for the same reason), so it
    is counted here, where Gate 1 refuses on it and the repair never sees it.

    A `while` is deleted rather than counted: its condition is the only thing
    that would have run and the fold has already reduced that to a literal. The
    asymmetry between the two is the whole reason this is a separate function.
    """
    dialect = _unreachable_dialect(language)
    if dialect is None or not dialect.dead_loop_is_fatal:
        return 0
    return sum(
        1
        for node in _walk(dialect.parse(source, language))
        if node.type == "for_statement" and _loop_condition_literal(node) == "false"
    )


def _unreachable_statement_nodes(source: str, language: str) -> list[Node]:
    """The stranded statements themselves — what :func:`_unreachable_statements` counts.

    Split out so the same walk can both judge a rewrite and repair it: counting
    them in one place and re-deriving them somewhere else is how the two answers
    drift apart.

    One rule, asked of every statement LIST: after the first statement that
    completes abruptly, nothing in that list is reachable. Asking it of the list
    rather than of an ``if`` or a loop is what makes a folded early return
    (``return newA(); return oldA();``) visible — it contains neither, and was
    invisible in EVERY language, Java included, until #2807.
    """
    dialect = _unreachable_dialect(language)
    if dialect is None:
        return []
    stranded: list[Node] = []
    for node in _walk(dialect.parse(source, language)):
        # A `while` the fold emptied is dead in its ENTIRETY, so the statement
        # itself is what goes. Deleting only the body leaves `while (false) { }`,
        # which is the same compile error — measured, along with
        # `while (false);`, whose body is merely an empty statement.
        if (
            dialect.dead_loop_is_fatal
            and node.type == "while_statement"
            and _loop_condition_literal(node) == "false"
        ):
            stranded.append(node)
            continue
        # Everything else is ONE rule applied to statement LISTS, because a
        # statement list is the only thing that can hold a following sibling:
        # once a statement completes abruptly, every statement after it in the
        # same list is unreachable. Asking it of the list rather than of each
        # construct is what makes `return newA(); return oldA();` visible —
        # the shape a folded early return leaves, which is a `javac` error and
        # an ESLint `no-unreachable` error alike, and which the previous form
        # could not see because it only ever asked the question of an `if` or a
        # loop (#2807). An `else if`'s inner `if_statement` hangs off the outer
        # one — off the enclosing `if_statement` in Java, off an `else_clause`
        # in TypeScript, neither of which is a statement list — so a chain is
        # still counted once, at the top, rather than once per arm.
        if node.type not in dialect.statement_lists:
            continue
        siblings = [c for c in node.named_children if c.type != "comment"]
        abrupt = next(
            (i for i, c in enumerate(siblings) if _completes_abruptly(c, dialect)),
            None,
        )
        if abrupt is not None:
            stranded.extend(siblings[abrupt + 1 :])
    return stranded


def _expand_to_whole_lines(data: bytes, start: int, end: int) -> tuple[int, int]:
    """Widen a byte range to swallow its line when nothing else shares it.

    Deleting only the statement's own span leaves the indentation that preceded
    it and the newline that followed, i.e. a blank line where the statement was.
    Widening is conditional: a statement sharing its line with live code (``a();
    return b();``) takes only its own span, so the neighbour survives untouched.
    """
    line_start = data.rfind(b"\n", 0, start) + 1
    line_end = data.find(b"\n", end)
    line_end = len(data) if line_end == -1 else line_end + 1
    before_is_blank = data[line_start:start].strip() == b""
    after_is_blank = data[end : line_end - 1 if line_end > end else line_end].strip() == b""
    if before_is_blank and after_is_blank:
        return line_start, line_end
    return start, end


def remove_unreachable_statements(
    before: str, after: str, language: str
) -> tuple[str, list[str]]:
    """Delete statements the fold stranded; return the new source and their text.

    Folding a flag out of the last arm of a chain turns ``else if (true)`` into a
    plain ``else``; folding ``if (<read>) { return a; }`` leaves a bare
    ``return a;``. Either way whatever followed becomes unreachable — a compile
    **error** in Java (JLS 14.21), which is why this ran as a refusal before: a
    ready-for-review pull request whose branch does not build is the failure
    class this tool exists to avoid. In the TypeScript family the branch builds
    and ``eslint:recommended`` fails it instead, which costs the customer the
    same green check.

    Deleting them is sound in a way that deleting ordinary code is not: javac
    itself proves the statements can never execute, so removing them cannot
    change behaviour. What it does cost is the author's intent — a trailing
    ``return false;`` was somebody's fall-through default — so every statement
    removed is returned here and named in the pull request body. Silent is the
    one thing this must not be.

    Two conservative rules:

    * **Scoped by :func:`_unreachable_dialect`, exactly as the count is** — one
      decision serving both, so a language can never be repaired without being
      counted or counted without being repaired. Every language outside it
      compiles the same output and behaves correctly; deleting their statements
      would be a behaviour-neutral edit nobody asked for.
    * **Nothing is touched if ``before`` already had unreachable statements.**
      Then this cannot tell which are the fold's and which the customer's, and
      guessing would delete code the flag removal had nothing to do with.

    Iterated to a fixpoint, because removal can strand more: a block whose last
    live statement goes becomes abrupt itself, which can strand what follows the
    construct containing it. That is javac's own analysis, and stopping after
    one pass would leave a file that still does not compile.
    """
    if _unreachable_dialect(language) is None:
        return after, []
    if _unreachable_statements(before, language):
        return after, []
    removed: list[str] = []
    data = after.encode("utf-8")
    # Bounded purely as a runaway guard: each round strictly shrinks the file,
    # so it terminates on its own.
    for _ in range(10):
        nodes = _unreachable_statement_nodes(data.decode("utf-8"), language)
        if not nodes:
            break
        spans = _outermost([(n.start_byte, n.end_byte) for n in nodes])
        for start, end in sorted(spans, reverse=True):
            removed.append(data[start:end].decode("utf-8"))
            wide_start, wide_end = _expand_to_whole_lines(data, start, end)
            data = data[:wide_start] + data[wide_end:]
    return data.decode("utf-8"), removed


def _outermost(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop any span contained in another, so deletions cannot overlap.

    A stranded statement can itself contain an ``if`` that strands statements
    inside it. Removing the inner one first and then the outer one would splice
    at offsets the first deletion has already moved.
    """
    return [
        span
        for span in spans
        if not any(
            other != span and other[0] <= span[0] and span[1] <= other[1]
            for other in spans
        )
    ]


#: Stubbing entry points that take the flag read AS AN ARGUMENT, split by the
#: bracket their library uses. Both lists are deliberately short: a name here
#: costs an over-refusal when a customer happens to own a function by that name
#: and a read folds into it, so only names whose whole purpose is to receive a
#: DESCRIPTION of a call are listed. `whenever` precedes `when` for reading
#: order only — each alternative requires its own bracket immediately after, so
#: `whenever(` can never be matched by the `when` entry.
_MOCK_STUBBING_PAREN_NAMES = ("whenever", "when", "given")

#: The brace form is MockK's, where the read is the sole expression of a
#: trailing lambda (`every { <read> } returns true`). It is a separate list
#: because the two spellings are not interchangeable in any of these libraries,
#: and accepting either bracket for either name would widen the match for no
#: gain.
_MOCK_STUBBING_BRACE_NAMES = ("coEvery", "every", "coVerify", "verify")

#: Languages whose mocking libraries pass a REAL call to the DSL. Go is absent
#: because `literal_residues` catches its gomock shape structurally, and C#
#: because `rules/csharp.toml`'s expression-tree filter stops the fold before
#: it happens. Python was measured not to fold into argument position at all,
#: and Ruby and PHP name the method as a symbol or string
#: (`receive(:bool_variation)`, `shouldReceive('boolVariation')`) so the read
#: never reaches the DSL. Swift has no such library surface in the rules.
_MOCK_STUBBING_DSL_LANGUAGES = frozenset({"java", "kt", "dart", "ts", "tsx", "js"})

_MOCK_STUBBED_LITERAL = re.compile(
    r"\b(?:" + "|".join(_MOCK_STUBBING_PAREN_NAMES) + r")\s*\(\s*(?:true|false)\s*\)"
    r"|\b(?:" + "|".join(_MOCK_STUBBING_BRACE_NAMES) + r")\s*\{\s*(?:true|false)\s*\}"
)


def _mock_stubbed_literals(source: str, language: str) -> int:
    """Boolean literals sitting where a mocking DSL wanted a CALL.

    The #2714 shape in the four languages where it COMPILES:

        when(client.boolVariation("KEY", ctx, false)).thenReturn(true)
        ->  when(true).thenReturn(true)

    Mockito, BDDMockito, mockito-kotlin, MockK and mockito-dart all stub by
    passing a real call and capturing the invocation it registers, so the TEXT
    of the read is the payload — exactly the property that makes C#'s
    `Setup(c => …)` untouchable. Folding it away leaves a stub that no longer
    stubs anything: the test then throws at run time or, worse, passes
    vacuously, and the flag key is gone from the file so no later run looks
    again.

    Go has the same bug and is NOT handled here — `literal_residues` catches it
    structurally, because `true.Return(true)` is broken Go by construction. No
    equivalent exists for these four. C#'s discriminator (the read's receiver
    is the lambda's own parameter) does not transfer either, because none of
    these libraries takes a lambda: they take the call itself. What is left is
    the entry point's NAME, so this check is a small keyed list, chosen
    deliberately over a structural query rather than for want of one.

    Text-anchored on purpose. The four grammars disagree underneath — Kotlin
    spells a boolean literal as a plain `identifier`, Dart hangs the argument
    off a `selector`, Java uses `argument_list`, TS `arguments` — so the
    structural form would be four queries and four ways to silently stop
    guarding, which is the failure mode `rules/csharp.toml` documents for an
    uncaptured `not_enclosing_node`. The differential is what makes text safe
    here: a match inside a pre-existing string or comment is counted in BOTH
    treatments and cancels, and this tool only deletes and folds, so it cannot
    introduce one.

    REFUSED rather than repaired, like every other check in this module. The
    read could in principle be left alone instead — that is what C# does, and
    it is the better OUTPUT — but doing it here would mean a name-keyed filter
    per name per language in five rule files, with no `#any-of?` to collapse
    them (the engine accepts and ignores it), and the csharp rules argue
    against name-matching on principle. A loud refusal costs the customer this
    flag's diff; the alternative costs them a test that silently stopped
    asserting.
    """
    if language not in _MOCK_STUBBING_DSL_LANGUAGES:
        return 0
    return len(_MOCK_STUBBED_LITERAL.findall(source))


def transform_broke_syntax(before: str, after: str, language: str) -> bool:
    """Whether rewriting ``before`` into ``after`` produced invalid source.

    Delegates ts/tsx to :mod:`flag_cleanup.ts_syntax`, which carries four checks
    for hazards specific to that grammar. For every other language this is the
    differential checks documented at the top of this module. Two checks run
    AHEAD of that delegation because they are keyed by language rather than by
    grammar and must reach the TS family as well; see the comments on them.

    Every check is differential — ``after`` is judged against ``before``, never
    against an absolute standard — so a file that was already unusual stays
    eligible and only a NEW breakage is refused. That is also what keeps the
    native parse honest across interpreter versions: source using syntax this
    Python does not know fails both parses and is judged on the other checks.

    An unsupported language is a programming error and raises, rather than
    returning ``False`` — a gate that silently passes everything is worse than
    no gate, because the run still reports the rewrite as checked.
    """
    # Ahead of the ts delegation, which returns wholesale: these checks cover
    # the TS family too, and putting either after the branch would silently
    # exclude the languages it is keyed for. That is not hypothetical — the
    # unreachable-statement count sat below the branch and so never ran for
    # `ts`/`tsx`/`js`, which is half of what #2807 was.
    if _mock_stubbed_literals(after, language) > _mock_stubbed_literals(before, language):
        return True
    # The repair in `remove_unreachable_statements` has already run by the time
    # the runner asks this, so what reaches here is what the repair declined:
    # a file that ALREADY had unreachable code, where the fold's strands cannot
    # be told from the customer's. Differential, so having some is not itself
    # disqualifying — only adding to the count is.
    if _unreachable_statements(after, language) > _unreachable_statements(
        before, language
    ):
        return True
    if language in _TS_LANGUAGES:
        return ts_syntax.transform_broke_syntax(before, after, language)
    if language not in _PROFILES:
        raise ValueError(f"unsupported language {language!r}")
    return (
        count_syntax_errors(after, language) > count_syntax_errors(before, language)
        or _stranded_keywords(after, language) > _stranded_keywords(before, language)
        or (_native_parse_fails(after, language) and not _native_parse_fails(before, language))
        or _literal_residues(after, language) > _literal_residues(before, language)
        or _literal_statements(after, language) > _literal_statements(before, language)
        or _null_literal_comparisons(after, language)
        > _null_literal_comparisons(before, language)
        or _lost_side_effects(after, language) < _lost_side_effects(before, language)
    )


def const_path_is_safe(
    source: str,
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] = (),
) -> bool:
    """Whether the const-propagation rules may run over ``source`` (Gate 2).

    TWO independent questions live behind this one call, because two different
    const mechanisms exist and they propagate different things:

    * the READ-const question -- a name bound to the flag read itself
      (``const on = boolVariation('k')``), which only ``ts_const.toml`` does.
      Answered by :func:`ts_syntax.const_path_is_safe`.
    * the KEY-const question -- a name bound to the flag KEY
      (``const K = 'k'; boolVariation(K, ...)``), which every language's
      ``<base>_const.toml`` does. Answered by :mod:`flag_cleanup.key_const`.

    TypeScript is the one language that has both, and it must satisfy BOTH:
    the runner partitions on a single boolean and prepends one file, so a
    refusal from either question withholds the whole file. That costs nothing
    it could otherwise have cleaned -- in a file whose key is hoisted, the
    literal-anchored seed rules cannot match that flag's reads at all, so a
    file newly withheld by the key question was already a no-op.

    A language with neither mechanism returns ``True`` rather than raising: the
    runner calls this unconditionally to partition its file list, and a
    language with no const rules simply gets one group.

    ``accessors`` reaches the KEY-const question only. The run clones the
    key-const rules for each wrapper name (#2730), so a wrapper's read of a
    hoisted key is a read those rules remove and must stop being a reason to
    withhold the file. The READ-const question does not take it: a name bound
    to a wrapper's call is already handled by the ``(#not-eq? @callee ...)``
    guard `rule_synthesis.with_accessor_guards` extends, one layer up.
    """
    if language in _TS_LANGUAGES:
        if not ts_syntax.const_path_is_safe(source, language, flag_key):
            return False
        root = ts_syntax._parse(source, language)
    elif language in key_const.supported_languages():
        # A TEMPLATE language asks this question of the CODE VIEW, for the same
        # reason `_literal_residues` does — and here the consequence of getting
        # it wrong is worse than a missed refusal. `.erb` takes `ruby_const.toml`
        # through `rule_base`, so those rules really do run over a template; a
        # Ruby key-const profile handed a TEMPLATE tree would find no constant
        # in any file and report every one of them trivially safe, which is the
        # vacuous gate `key_const`'s own docstring warns about — rules running
        # on a guarantee nobody made.
        #
        # Safe to point at the derived string precisely because this question
        # produces a BOOLEAN and no edit: `path_is_safe` reads text out of the
        # tree it is given and never hands an offset back to a caller. That is
        # what separates it from `_leaf_spans`/`_construct_at`, which must keep
        # indexing the real file.
        #
        # Markup is dropped, and dropping it is correct rather than merely
        # convenient: a constant's name occurring in the page's TEXT is not a
        # reference to it, and counting one would withhold a file for a word.
        profile = _PROFILES[language]
        if profile.code_view is not None:
            source, language = _code_view(source, language), profile.code_view
        root = _parse(source, language)
    else:
        return True
    return key_const.path_is_safe(
        root, source.encode("utf-8"), language, flag_key, accessors
    )
