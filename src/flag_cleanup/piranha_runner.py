"""Invoke ``polyglot-piranha`` to remove a single Featureflip flag from a repo.

The public entry point is :func:`run_piranha`. It writes the transformed files
in place and returns a unified diff of everything Piranha changed (empty string
when nothing matched — the safety default: no diff -> no risky PR).

Piranha configuration layout
----------------------------
``polyglot_piranha.execute_piranha(PiranhaArguments(...))`` reads its rule graph
from a *directory* (``path_to_configurations``) containing canonically-named
files: ``rules.toml`` (the ``[[rules]]``) and ``edges.toml`` (the ``[[edges]]``
that wire the seed rule to the cleanup cascade). Piranha will NOT read
``[[edges]]`` out of ``rules.toml`` — they must be a separate file.

The engine ships built-in feature-flag *cleanup* rules for every language this
tool supports (C#, Dart, Go, Java, Kotlin, PHP, Python, Ruby, Swift and
TypeScript/TSX, which share one rule base). So a language's ``<base>.toml``
carries mainly SEED rules that replace the read with a boolean literal, and the
engine's cascade does the rest (fold ``if``, simplify boolean expressions, drop
dead statements). A rule here with the same name as a
built-in REPLACES it, so never copy a built-in in order to adjust it: the copy
shadows the engine's rule, and a fix made on either side stops reaching the
other. What a language carries beyond its seeds (const, entries, JSX) is either
flag policy or a gap the engine does not cover yet.

Rule files live as per-language flat files under ``rules/``:

* ``<base>.toml``            -> ``rules.toml``   (generic read + cleanup rules)
* ``<base>_const.toml``      -> prepended to ``rules.toml`` for files that pass
  the Gate 2 shadow check (see below)
* ``<base>_edges.toml``      -> copied to ``edges.toml``   (seed -> cleanup wiring)
* ``<base>_arguments.toml``  -> extra ``PiranhaArguments`` keyword flags

Two safety gates wrap the engine, both reached through
:mod:`flag_cleanup.syntax` because Piranha 0.4.8's filter language cannot
express either one:

* **Gate 1 (post-transform)** — every rewritten file is re-parsed; if the
  transform introduced a syntax error or put a reserved word in a binding
  position, the WHOLE transform is rolled back and :class:`UnsafeRewriteError`
  raised naming the file(s) refused. It is raised rather than returned as an
  empty diff because a refusal and a genuine no-match are opposite facts: one
  needs the customer to act, the other needs nothing at all.
* **Gate 2 (pre-flight)** — the const-propagation rules are withheld from any
  file that binds the flag-read's name more than once, which is the only sound
  way to avoid folding a shadowed reference to the wrong branch. Only
  TypeScript HAS const-propagation rules of ours; elsewhere the equivalent
  inlining is Piranha's own, so there is nothing to withhold.

Gate 2 splits the candidates into two groups, so Piranha may run twice. If the
second run panics, the first run's writes are already on disk — one more reason
the caller must reset the tree after any refusal raised from here rather than
trusting the engine's all-or-nothing buffering.

Neither gate says anything about the *key*: that is checked first, by
:func:`ensure_supported_key`, because a key outside the safe alphabet is a
hazard to the rule TOMLs themselves rather than to their output.

``ts`` and ``tsx`` share the ``ts`` rule base; only the Piranha language id
differs. Every other language has a rule base of its own name.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - project requires >=3.11
    import tomli as tomllib

from polyglot_piranha import PiranhaArguments, execute_piranha

from flag_cleanup import (
    contradicting,
    csharp_residue,
    ctor_param_removal,
    dart_field_removal,
    cross_file_dart,
    cross_file_keys,
    cross_file_kotlin,
    cross_file_php,
    cross_file_python,
    file_deletion,
    go_dead_code,
    go_inline,
    go_stranded,
    kotlin_sentinel,
    mock_stubs,
    ruby_owner_reach,
    source_index,
    ts_bound_overrides,
    ts_dead_code,
    ts_fold,
    ts_gates,
    ts_param_pins,
    ts_tests,
    wrapper_inline,
)
from flag_cleanup.kotlin_sentinel import Rewrite
from flag_cleanup.php_fold import fold_literal_conditions as fold_php_literal_conditions
from flag_cleanup.python_fold import inline_literal_bindings, marker_for
from flag_cleanup.key_const_visibility import VisibleKeyConst
from flag_cleanup.syntax import (
    collapse_introduced_carriage_returns,
    collapse_introduced_double_spaces,
    const_path_is_safe,
    externally_visible_key_consts,
    remove_dead_loops,
    remove_unreachable_statements,
    unreachable_is_fatal,
    source_is_unreadable,
    strip_introduced_trailing_whitespace,
    refusal_reason,
)
from flag_cleanup.primary_constructor import unread_parameters
from flag_cleanup.ts_unread_members import unread_constructor_parameters, unread_members
from flag_cleanup.ts_syntax import strip_dangling_type_separators
from flag_cleanup.unused import MODULE_MARKER, go_import_facts, remove_stranded_bindings

logger = logging.getLogger(__name__)

from flag_cleanup import rule_synthesis

RULES_DIR = Path(__file__).parent / "rules"


class PiranhaTransformError(RuntimeError):
    """Piranha aborted instead of producing a transform.

    Raised when the Rust engine panics — most importantly on its own
    "Produced syntactically incorrect source code" self-check, which fires when
    a rule rewrote something it should not have. Piranha validates before it
    writes, so the codebase on disk is untouched when this is raised.

    Callers should treat it exactly like an empty diff: skip the flag, open no
    PR. It is a distinct type (rather than a silent ``""``) so the orchestrator
    can log it — a rule that trips the self-check is a rule bug, not a
    "nothing matched".
    """


#: How the engine words its refusal to rewrite a file it cannot parse in the
#: first place ("Failed to parse … will not rewrite a file it cannot read").
#: That is the ONE abort that is a property of the customer's source. Every
#: other abort — the post-rewrite "Produced syntactically incorrect source
#: code" self-check, a refusal to write a reserved word into a name position,
#: anything a later engine adds — fires after the engine read the file fine,
#: so it is a defect in the rewrite. :func:`_quarantine_and_retry` quarantines
#: only an abort carrying this phrase. Matched positively, rather than by
#: listing the others, so an abort nobody has seen yet stops the flag instead
#: of shipping a partial removal.
_UNREADABLE_INPUT = "will not rewrite a file it cannot read"


class UnsafeRewriteError(RuntimeError):
    """Gate 1 refused the rewrite; everything it wrote has been rolled back.

    Carries the paths so the refusal can be *named*. This is the whole point of
    the type: before it existed a Gate 1 rejection returned ``""``, which the
    run reported as ``[no-changes]`` and exit 0 — indistinguishable from
    "nothing in this repo reads the flag". A single file whose rewrite cannot
    be made safe (``} else if (<read>) { … }`` with no trailing ``else`` is a
    perfectly ordinary shape) therefore produced a permanent, silent
    ``[no-changes]`` on every scheduled run, for that flag, forever — the exact
    outcome the whole gate design was meant to avoid.

    ``refused`` are the files whose own rewrite did not survive the gate, and
    ``reasons`` says which check stopped each one;
    ``discarded`` are the files that rewrote cleanly but were rolled back with
    them, because the rollback is deliberately all-or-nothing (see
    :func:`run_piranha`).
    """

    def __init__(
        self,
        message: str,
        refused: Iterable[str] = (),
        discarded: Iterable[str] = (),
        reasons: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.refused = tuple(refused)
        self.discarded = tuple(discarded)
        # Which check refused each file, worded for the reader. Most are not
        # about syntax at all, so without this a refusal sent people looking for
        # a parse error in a file that parsed fine.
        self.reasons = dict(reasons or {})


class UnsupportedFlagKeyError(ValueError):
    """The flag key cannot be substituted into a Piranha rule safely.

    See :func:`ensure_supported_key`. Raised — never worked around — because
    every alternative is worse: the key reaches the rules as *regex* source, so
    the engine either panics (``)``, ``[``, ``{``), silently matches nothing
    (``$``, ``+``), or, for a ``"``, closes the query's string literal and lets
    the key's remainder be read as rule syntax.
    """


# Characters a flag key may hold and still be substituted into the rules
# verbatim. An ALLOW-list, not a list of dangerous characters, because the
# unsafe set is not knowable from here: Piranha compiles the `#eq?` predicate's
# operand to a REGEX (verified — a key of `a)b` fails with "regex parse error:
# unopened group"), so every regex metacharacter is a hazard whose exact
# behaviour is an engine implementation detail. `a$b` and `a+b` match nothing
# at all and report a clean "no changes"; `a"b`, `a)b`, `a[b`, `a{b` panic the
# engine; and a `"` is a query-injection surface into rules that run over the
# customer's own source.
#
# This is exactly the alphabet `github_ops._SAFE_BRANCH_CHARS` keeps verbatim,
# and a strict superset of the Featureflip canonical key format (lowercase
# alphanumeric segments joined by single `-`/`_`), so no key a customer can
# create today is refused. It is deliberately NOT the branch-name rule itself:
# `removal_branch` *escapes* anything outside the set (two keys must never
# collide onto one branch), which is possible for a ref name and is not
# possible for a tree-sitter query operand.
_SAFE_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def ensure_supported_key(flag_key: str) -> None:
    """Raise :class:`UnsupportedFlagKeyError` unless the key is safe to substitute.

    Called before the engine is constructed, so an unrepresentable key costs a
    clear per-flag refusal instead of a Rust panic (or, worse, a rewrite driven
    by a regex the key smuggled into the rules).
    """
    if not flag_key:
        raise UnsupportedFlagKeyError("flag key is empty")
    unsupported = sorted({char for char in flag_key if char not in _SAFE_KEY_CHARS})
    if unsupported:
        raise UnsupportedFlagKeyError(
            f"flag key {flag_key!r} contains character(s) this tool cannot search "
            f"for safely: {', '.join(repr(char) for char in unsupported)}. Keys are "
            "substituted into the rewrite rules, where anything outside "
            "[A-Za-z0-9_-] is either rejected by the engine or matched as a "
            "regular expression rather than as text. Rename the flag, or add the "
            "key to the `ignore` input to stop reporting it"
        )


# Maps the tool's language token -> (Piranha language id, rule-file basename,
# source extensions). See PiranhaLanguage in polyglot_piranha/__init__.pyi for
# the valid ids.
#
# The extensions mirror what Piranha itself accepts and are NOT a free choice:
# each PiranhaLanguage carries its own extension list and silently ignores any
# file outside it — no error, so a flag read in such a file is left behind
# while we report the flag "removed". Still true and still measured against the
# pinned wheel: handing `.js`, `.tsx`, `.txt` or `.es6` to the typescript
# language leaves the file completely untouched.
#
# What is no longer true is the CARDINALITY, and that is the half that decides
# whether widening this tuple is worth doing. A language used to carry exactly
# one extension, which made a wider tuple here pure decoration; piranha#25
# turned the engine's per-language `extension: String` into a `Vec<String>` and
# piranha#85 used it to claim `.mts`/`.cts` for typescript. So widening the
# tuple to an extension the ENGINE claims is now the whole change — all eight
# below transform, verified one file per extension. Widening it past what the
# engine claims still does nothing, silently, which is the trap this paragraph
# exists to keep marked.
#
# Whatever stays unclaimed is surfaced rather than closed. The
# `unprocessed_extensions` set below is derived from this table rather than
# hardcoded, and feeds the warning that reaches the run output and the
# pull-request body.
@dataclass(frozen=True)
class _Language:
    """How one language token maps onto the engine and the rule files.

    ``true_literal``/``false_literal`` exist because the ``@treated``
    substitution is pasted into the rules as TEXT, so it must be spelled the way
    the target language spells it. Everything Piranha supports except Python
    uses lowercase; Python needs ``True``/``False``. Getting this wrong is
    silent and severe — the engine happily emitted ``if true:``, which is a
    ``NameError`` at run time rather than a syntax error, so it survives a parse
    check.

    They spell ``@untreated`` too, which is the same two values swapped. That
    one is compared against a SOURCE literal rather than emitted into one, so a
    wrong spelling there fails the other way round: the comparison never
    matches, every entry prong keeps deleting, and #3050 comes back silently.
    ``contradicting.py`` reads the same two fields for its detector, so the two
    halves cannot disagree about how a language spells ``false``.
    """

    piranha_id: str
    rule_base: str
    extensions: tuple[str, ...]
    true_literal: str = "true"
    false_literal: str = "false"
    #: How the customer-facing surfaces spell this language — README's
    #: "Supported languages" sentence and `action.yml`'s own description.
    #: Keyword-only and DEFAULTLESS on purpose: it is the forcing function that
    #: makes adding a language to this dict fail until the published docs name
    #: it too. `tests/test_packaging.py` compares both surfaces against this
    #: registry, which is what stops a language shipping fully working and
    #: fully undocumented, green (see the roster tests there for why prose was
    #: the one part of adding a language that nothing checked).
    display_name: str = field(kw_only=True)


_LANGUAGES: dict[str, _Language] = {
    # `.mts` and `.cts` are the ESM/CommonJS suffixes TypeScript mirrors from
    # Node's `.mjs`/`.cjs`, and both are ordinary TypeScript to this grammar:
    # `.mts`'s top-level `await` is an expression statement, and `.cts`'s
    # `import x = require(...)` / `export =` are TypeScript's own import-equals
    # forms. Claimed by the engine as of piranha 0.10.0 (piranha#85); before
    # that the `typescript` language took `.ts` alone, so an `.mts` file was
    # never discovered and this tool listed it as unprocessed.
    "ts": _Language(
        "typescript", "ts", (".ts", ".mts", ".cts"), display_name="TypeScript"
    ),
    "tsx": _Language("tsx", "ts", (".tsx",), display_name="TSX"),
    # JavaScript reuses the TS cascade WHOLESALE — no rules of its own. The
    # engine's `javascript` arm parses with the TSX grammar (piranha#25), which
    # is the same grammar `tsx` above uses, so every node shape `rules/ts.toml`
    # matches is present. Verified against 0.6.0 rather than assumed: all four
    # extensions really are processed, `.ts` is correctly NOT (so `ts` and `js`
    # cannot double-process a file), and the unmodified ts cascade produces the
    # same rewrite it does for `.tsx`.
    #
    # This is also the first language to claim more than one extension, which
    # only became possible when piranha#25 turned the engine's per-language
    # `extension: String` into a `Vec<String>`.
    "js": _Language(
        "javascript",
        "ts",
        (".js", ".jsx", ".mjs", ".cjs"),
        display_name="JavaScript",
    ),
    "java": _Language("java", "java", (".java",), display_name="Java"),
    "go": _Language("go", "go", (".go",), display_name="Go"),
    # `True`/`False`, not `true`/`false` — see the docstring above. Python is
    # also the only language here whose fold is PARTIAL by design: the engine
    # cannot re-indent, so a branch of more than one statement is left standing
    # and `syntax.py`'s residual-condition check turns that into a refusal
    # rather than a half-removal. See the header of rules/python.toml.
    "python": _Language("python", "python", (".py",), "True", "False", display_name="Python"),
    # Kotlin only works because of a PRE-PASS: the engine's bundled grammar
    # cannot see inside a string literal, so a rule can match `boolVariation(…)`
    # but not check which flag it reads. `flag_cleanup.kotlin_sentinel` marks
    # the matching calls first. See rules/kt.toml.
    #
    # `.kts` is a Kotlin script — a Gradle build file is the common one. It
    # differs from `.kt` only in allowing statements at the top level, which
    # `source_file` already accepts, so the same grammar, the same cascade and
    # the same pre-pass apply. Claimed by the engine as of piranha 0.10.0
    # (piranha#85). BOTH grammars in the path were checked, not just the
    # engine's: the pre-pass parses with the separately-versioned
    # `tree-sitter-kotlin` wheel and sees the file FIRST, so a script it could
    # not read would be sentinel-ified wrong before the engine ever ran. It is
    # content-driven and never looks at the suffix, so nothing there needed a
    # change. Claiming build files is safe because coverage is decided by the
    # SEED RULE, not the suffix: a script with no flag read matches nothing.
    #
    # The obvious worry — a script is ALL top level, and a top-level binding is
    # the shape the engine does not inline — was measured rather than reasoned
    # about, and `.kt` and `.kts` come back byte-identical for it: a top-level
    # `val on = <read>` folds to `val on = true` in BOTH, references intact and
    # correct. So the residue is a pre-existing Kotlin property that scripts
    # merely meet more often, not something claiming `.kts` introduces.
    "kt": _Language("kotlin", "kt", (".kt", ".kts"), display_name="Kotlin"),
    # The shortest language here — see rules/csharp.toml's header. Piranha's
    # C# built-ins (added by the same fork program that unlocked this grammar)
    # already carry the trailing-else-if-false fix java.toml has to hand-roll,
    # so the seed rule is the whole file. `piranha_id="csharp"` and `"cs"` are
    # both accepted by the engine (verified against the installed wheel); the
    # spelled-out form is used for readability, matching "kotlin"/"typescript"
    # above rather than the two-letter `rule_base`/extension.
    "csharp": _Language("csharp", "csharp", (".cs",), display_name="C#"),
    # PHP rides the engine's built-in cascade (piranha#21) the way C# does, so
    # `rules/php.toml` is a seed rule plus two trailing-`elseif` rules — see its
    # header. The engine registers `LANGUAGE_PHP`, the HTML-aware variant, NOT
    # `LANGUAGE_PHP_ONLY`: a `.phtml` template is ordinary PHP with markup
    # around it, and the HTML-aware grammar is a strict superset for every
    # shape these rules match.
    #
    # `.inc` and `.module` are claimed because Drupal and a lot of older PHP
    # use them for real code. `.inc` is the loose one — it means "include", and
    # other ecosystems use it too — but non-PHP content is inert (it parses as
    # one `text` node and matches no rule). The one shape that is NOT inert is
    # an `.inc` holding an XML declaration: `<?xml` scans as a PHP short open
    # tag, the file fails the engine's parse check, and the run aborts. That
    # only happens when the file also MENTIONS the flag key (nothing else is
    # handed to the engine), and `_quarantine_and_retry` turns it into a
    # reported skip — at the cost of one engine invocation per candidate file.
    "php": _Language(
        "php", "php", (".php", ".phtml", ".inc", ".module"), display_name="PHP"
    ),
    # Ruby rides the engine's built-in cascade the way C# and PHP do, so
    # `rules/ruby.toml` is a seed rule and nothing else — the engine carries
    # 1,158 lines of Ruby cleanup rules, reached through the
    # `replace_expression_with_boolean_literal` group.
    #
    # `.rake` is ordinary Ruby — a Rakefile task body is method calls with
    # blocks and nothing else — so it takes the same grammar and the same
    # cascade. Claimed by the engine as of piranha 0.10.0 (piranha#85), and
    # worth claiming because task bodies are exactly where an operational flag
    # read tends to live.
    #
    "ruby": _Language("ruby", "ruby", (".rb", ".rake"), display_name="Ruby"),
    # ERB, and the FIRST TEMPLATE LANGUAGE here. `rule_base="ruby"` is the
    # load-bearing part: `.erb` runs `rules/ruby.toml`, `ruby_edges.toml` and
    # `ruby_arguments.toml` with no copy, so "ERB gets the Ruby cascade
    # unmodified" is true BY CONSTRUCTION rather than by discipline. There is
    # deliberately no `rules/erb.toml` — a copy is a second file to keep in
    # step, and a drift between the two would be silent. Precedent is the
    # `tsx` entry above, which rides `rules/ts.toml` the same way.
    #
    # The engine reads a template through a synthesized Ruby VIEW: markup
    # collapses to `_erb_rN` placeholder statements, the ordinary Ruby cascade
    # runs on the synthesized Ruby, and the template is reconstructed after the
    # last pass (piranha#88, 0.11.0). Before that view the engine could see no
    # Ruby statements between `<% if x %>` and `<% else %>`, so both arms were
    # invisible and the cascade folded the read and stopped, leaving
    # `<% if false %>` standing over both — a silent residue, not a removal.
    # (The reason recorded here before 0.11.0 was that the engine had no
    # HTML-aware Ruby variant the way it has `LANGUAGE_PHP` for `.phtml`. That
    # was never the blocker: `tree-sitter-embedded-template` plus
    # `included_ranges` already yields a correct Ruby tree over a template.)
    #
    # 0.12.0 (piranha#90) is what made claiming it worth doing. A read in an
    # `elsif` rung with a LIVE rung after it made the engine refuse the
    # promotion, so the file came back byte-identical on the treatment that
    # should have cleaned it — a green run that does nothing for half a
    # repository's flags. Re-measured against a 0.11.0 wheel while writing
    # `tests/fixtures/erb/dead_elsif`, so that fixture fails against one; the
    # oracle is `.rb`, since the same shape in plain Ruby and in a template
    # must reach the same decision.
    #
    # The other half of the claim is GATE 1, and it needed new machinery rather
    # than a profile row — see `syntax.py`'s `erb` profile and `code_view`. The
    # short version: the gate parses the file with the TEMPLATE grammar (a
    # bare Ruby grammar reads a template's markup as damage — 22 false refusals
    # over 324 real engine runs, zero true positives) and judges the host code
    # through a derived code view under Ruby's own profile.
    "erb": _Language("erb", "ruby", (".erb",), display_name="ERB"),
    # Dart rides the engine's built-in cascade (piranha#70/#71) like C#, PHP and
    # Ruby, so `rules/dart.toml` is a seed rule and nothing else.
    #
    # The grammar the RULES query (the engine's bundled tree-sitter-dart, 0.2.0
    # plus one arrow-body fix) and the grammar GATE 1 parses with (PyPI's
    # 0.1.0) are different grammars,
    # not two versions of one — see rules/dart.toml and CLAUDE.md.
    "dart": _Language("dart", "dart", (".dart",), display_name="Dart"),
    # Swift was parked for three reasons and all three are now resolved
    # (#2610), measured on fork 0.10.0 rather than inferred:
    #
    # 1. SPEED. The engine's Swift cascade took 67s to remove one flag from a
    #    nine-line file. It is now ~1.9s. Still the slowest language here by a
    #    wide margin — Java is 0.04s and Go 0.017s on the same shape — but that
    #    is ~46x, not the ~2000x that made two fixtures 93% of the suite. A
    #    file the seed rule does not match costs nothing (0.000s); the 1.9s is
    #    entirely the rewrite cascade, so the bill scales with removals, not
    #    with repository size.
    # 2. THE ABORT. The engine intermittently corrupted the heap and SIGABRTed,
    #    which is unrecoverable — an abort is not a panic, so
    #    `PiranhaTransformError` never fires and the rollback never runs. Not
    #    reproduced: 144 invocations in one process across six shapes and both
    #    treatments, zero aborts, RSS flat at ~44.9 MB. The tree-sitter 0.25
    #    Swift redesign (piranha#5) is the likely fix.
    # 3. THE INVERSION, which was the disqualifying one and was recorded as
    #    unexplained. It is explained: tree-sitter-swift gives prefix `!` the
    #    wrong precedence, so the bang parses INSIDE the call expression the
    #    seed rule replaces and was silently deleted. It has nothing to do with
    #    the edge wiring it was attributed to. Fixed by the matched rule pair in
    #    rules/swift.toml — read that file before touching either half.
    "swift": _Language("swift", "swift", (".swift",), display_name="Swift"),
}

# Every extension a flag read can plausibly live in, across every language
# family this tool knows about. The complement of whatever the configured
# languages cover is what this tool CANNOT process — see
# :func:`unprocessed_extensions`.
#
# Deliberately a strict superset of the extensions in ``_LANGUAGES``: adding a
# language must SHRINK the unprocessed set, and that only works if the whole
# universe is enumerated in one place. The entries with no language behind them
# are the honest part — each is a file a customer can really put a flag read in
# and this tool will really not touch. **That roster is EMPTY, for the first
# time**: every extension below is claimed by a language above, so
# `unprocessed_extensions(supported_languages())` returns nothing at all.
#
# An empty roster does NOT retire this set — it makes it more load-bearing, not
# less, and reading it as "nothing left to enumerate" is the way it would rot.
# Two derivations still run off it every time, and both stay live:
#
#   * a run configured with a SUBSET of the languages (`languages: ts` in a
#     repository whose Ruby also reads the flag) subtracts only what it
#     covers, so the caveat still fires and still names `.rb`/`.erb`. That is
#     the case the warning exists for, and it is unaffected by full coverage.
#   * a NEW extension a customer can hold a read in still has to be added here
#     the moment we ship an SDK for it — the bar for entry is "we ship an SDK
#     a customer can read a flag with", NOT "this tool can process it". A
#     suffix absent from this set is not reported as unprocessed, it is
#     INVISIBLE, because the derivation subtracts covered extensions FROM this
#     universe. So the next language ships two entries, not one, exactly as
#     every language before it did.
#
# That roster used to be five lines long. `.mts`/`.cts`, `.kts` and `.rake` all
# left it in #2675 — the engine claims them as of piranha 0.10.0 (piranha#85)
# and the `_LANGUAGES` tuples above claim them in turn, which is the pairing
# that matters: an extension listed in only one of the two places is either a
# caveat that never clears or a tuple entry that quietly does nothing.
# (`.js/.jsx/.mjs/.cjs` left the same way in #2607, `.swift` in #2610, and
# `.erb` — the last one — with the `erb` language above.)
#
# `.pyi` left it for the OPPOSITE reason and is deliberately GONE rather than
# unresolved (#2675). It fails the bar above from the far side: a stub declares
# an interface, so there is no live read in one to survive.
#
# This reverses an earlier call, and the argument it reverses deserves stating.
# `.pyi` was kept because a stub CAN carry a default argument, so a read was
# held to be possible rather than assumed away. The half that reasoning misses
# is that a stub is never EXECUTED — it is consumed by a type checker, and a
# default there is documentation (PEP 484 spells it `...` precisely because the
# value is inert). So even the one shape that argument identifies is not a live
# read: fold the flag out of the runtime module and a stale annotation beside
# it changes no behaviour. The warning's claim is that a read SURVIVES, and a
# file that cannot host one can only ever make that claim falsely.
#
# Dropping it narrows the universe, so re-read the paragraph above before
# copying this: doing that to a suffix that CAN hold a read is precisely the
# silent overclaim this set exists to prevent. `.pyi` qualifies only because no
# upstream work would ever make it processable — there is nothing to process.
#
# It spans FAMILIES on purpose. Configuring `languages: ts` in a repository
# whose Go code also reads the flag leaves a live read behind, and that is
# exactly the case the warning exists for — a pull request claiming the flag is
# removed while it demonstrably is not.
#
# The bar for entry is "we ship an SDK a customer can read a flag with", NOT
# "this tool can process it" — those are different questions and conflating
# them is what left PHP, Ruby and Dart out. An extension absent from this set
# is not reported as unprocessed, it is invisible: the derivation below
# subtracts covered extensions FROM this universe, so a suffix that was never
# in it cannot come out. Being unsupported is a caveat; being unenumerated is
# a pull request that claims a flag is gone while every read survives.
#
# `.inc` is the loose one — it means "include", and NASM, Pascal, C and
# Makefiles use it too. Claimed anyway, on the same reasoning the engine uses
# for it: the cost of a false entry here is one extra line in a caveat, while
# the cost of a missing one is the silent overclaim above. The scan only ever
# reaches files that already mention the flag key, so an unrelated `.inc` has
# to name the key to be listed at all.
KNOWN_EXTENSIONS = frozenset(
    {
        ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs",
        ".java",
        ".go",
        ".swift",  # claimed by the swift language since #2610
        ".kt", ".kts",
        ".py",  # NOT `.pyi` — see the note above
        ".cs",
        ".php", ".phtml", ".inc", ".module",  # claimed by the php language
        ".rb", ".rake",  # claimed by the ruby language
        ".erb",  # claimed by the erb language — the last unclaimed one
        ".dart",  # claimed by the dart language
    }
)

# Never customer source; skipped so a vendored copy of an SDK can't be rewritten
# and so the candidate scan doesn't read a whole dependency tree.
_SKIP_DIRS = frozenset({".git", "node_modules"})


def supported_languages() -> tuple[str, ...]:
    """Return the language tokens ``run_piranha`` accepts."""
    return tuple(_LANGUAGES)


def unprocessed_extensions(languages: Iterable[str]) -> tuple[str, ...]:
    """Extensions that can hold a flag read but ``languages`` will not process.

    DERIVED from the configured languages, never hardcoded: with
    ``languages=("ts",)`` a ``.tsx`` file holding a live read is just as
    unprocessed as a ``.js`` one, and a hardcoded "everything except .ts/.tsx"
    list would stay silent about it — the exact way this tool could mislead a
    customer into thinking a flag was fully removed.

    Unknown language tokens contribute nothing (they are rejected by
    ``Config.from_env`` long before this runs), which errs toward warning about
    MORE files rather than fewer.
    """
    processed = {
        extension
        for language in languages
        if language in _LANGUAGES
        for extension in _LANGUAGES[language].extensions
    }
    return tuple(sorted(KNOWN_EXTENSIONS - processed))


def candidate_files(
    repo_dir: str,
    language: str,
    flag_key: str,
    languages: Iterable[str] = (),
    accessors: tuple[str, ...] = (),
    inline_wrappers: bool = True,
) -> list[str]:
    """Every file :func:`run_piranha` could rewrite for this dir/language/key.

    Public because the orchestrator needs the exact same set to snapshot before
    the transform: ``run_piranha`` hands Piranha this explicit file list as
    ``paths_to_codebase``, so it is a sound *superset* of what the engine can
    possibly write. A snapshot of it is therefore a complete undo, which
    ``git checkout`` alone is not (it restores tracked files only — see
    ``git_ops.WorktreeSnapshot``).

    Files that only mention a hoisted key constant declared in another
    candidate are included too, so the undo snapshot covers the readers the
    cross-file pass may rewrite.

    ``languages`` is every language of the run. A mention in a file of
    another of them that shares this one's key-constant family (a ``.tsx``
    reader of a ``.ts`` constant) is included as well, when no other run of
    the family can rewrite it: it must not name the key itself, nor a constant
    declared in a file of another configured member of the family. Either
    would make it a candidate of that run too, and a file rewritten by two
    runs comes out depending on which ran first. Left out, it is reported as
    the reason the declaration stays.

    With ``inline_wrappers``, a file of this language that calls a member
    which may be a keyless wrapper of this flag (one whose whole body is a
    read, judged loosely from the files above; see
    :func:`wrapper_inline.candidate_names`) is included as well, so the undo
    snapshot covers the call sites the wrapper pass may rewrite -- and, for
    C#, a file naming a type that declares a member of such a name, where a
    test may set a fake the test-double pass resolves. A caller in a
    configured sibling of the family (a ``.tsx`` caller of a ``.ts`` wrapper) is
    included too, under the exclusions above: it must not hold the key, nor
    mention a constant or a candidate wrapper another configured member
    declares. ``accessors`` is needed for that judgement and nothing else. A
    JavaScript file's own declaration file (``flags.d.ts`` beside
    ``flags.js``) naming a candidate is admitted under the same exclusions,
    whether or not a TypeScript run is configured: the close-out deletes the
    declarations there that describe a wrapper it deletes.

    Raises ``ValueError`` for an unsupported ``language``, exactly like
    :func:`run_piranha`.
    """
    wrappers_on = inline_wrappers and wrapper_inline.profile(language) is not None
    # What the other configured family members' runs take, computed at most
    # once per call and only when a sibling mention turns up.
    claims: dict[tuple[str, ...], re.Pattern[str]] = {}
    found = _candidate_files_without_wrappers(
        repo_dir, language, flag_key, languages, accessors, wrappers_on, claims
    )
    if not wrappers_on:
        return found
    names = wrapper_inline.candidate_names(found, language, flag_key, accessors)
    if not names:
        return found
    if language == "csharp":
        # A test sets a C# fake (an inert sibling) through its knob, spelling
        # the fake but no wrapper; every type declaring a member of a
        # candidate's name is a superset of the fakes.
        from flag_cleanup import wrapper_csharp

        names = names | wrapper_csharp.candidate_fake_owners(repo_dir, names)
    spec = _language_spec(language)
    root = Path(repo_dir)
    resolved_root = root.resolve()
    siblings = sorted(set(wrapper_inline.profile(language).siblings) & set(languages))
    sibling_extensions = tuple(ext for sibling in siblings for ext in _LANGUAGES[sibling].extensions)
    described = _declaration_files(found)
    extra: list[str] = []
    for mention in cross_file_keys.mention_files(repo_dir, names, exclude=found):
        # A JavaScript wrapper's own `.d.ts`, whose declarations of it go with
        # it, whether or not a TypeScript run is configured. Admitted like a
        # sibling caller, under the same exclusions.
        sibling = (bool(sibling_extensions) and mention.endswith(sibling_extensions)) or (
            str(Path(mention).resolve()) in described
        )
        if not (sibling or mention.endswith(spec.extensions)):
            continue
        if not cross_file_keys.within(Path(mention), resolved_root):
            continue  # outside this run; the wrapper pass reports the call instead
        if sibling:
            try:
                text = Path(mention).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # never admitted; the wrapper pass reports it
            if flag_key in text:
                continue  # its own run rewrites it
            if _claimed(claims, root, siblings, flag_key, accessors, True).search(text):
                continue  # a candidate of another run of the family as well
        if root.is_dir():
            mention = str(root / os.path.relpath(mention, resolved_root))
        extra.append(mention)
    return found + sorted(set(extra) - set(found))


def _declaration_files(paths: Iterable[str]) -> frozenset[str]:
    """The hand-written declaration files (``flags.d.ts`` beside ``flags.js``)
    of ``paths`` that exist, resolved."""
    from flag_cleanup import cross_file_ts

    found = (cross_file_ts.declaration_file(path) for path in paths)
    return frozenset(str(Path(d).resolve()) for d in found if d is not None and Path(d).is_file())


def _claimed(
    claims: dict[tuple[str, ...], re.Pattern[str]],
    root: Path,
    siblings: Iterable[str],
    flag_key: str,
    accessors: tuple[str, ...],
    wrappers: bool,
) -> re.Pattern[str]:
    """:func:`_claimed_by_siblings` as a pattern, computed once per ``claims``."""
    key = (*siblings, str(wrappers))
    if key not in claims:
        claims[key] = cross_file_keys.mention_pattern(
            _claimed_by_siblings(root, siblings, flag_key, accessors, wrappers)
        )
    return claims[key]


def _own_candidates(root: Path, language: str, flag_key: str) -> list[str]:
    """The files of ``language``'s own extensions its run takes: those holding
    the key, and those reading a key constant they declare.

    Sibling-type readers are left out on purpose. They are what the caller is
    deciding about, and asking for them here would call back into the sibling.
    """
    extensions = _LANGUAGES[language].extensions
    holders = _candidate_files(root, extensions, flag_key)
    if not cross_file_keys.supported(language):
        return holders
    names = _key_const_names(holders, language, flag_key)
    resolved_root = root.resolve()
    readers = [
        mention
        for mention in cross_file_keys.mention_files(str(root), names, exclude=holders)
        if mention.endswith(extensions) and cross_file_keys.within(Path(mention), resolved_root)
    ]
    return holders + sorted(set(readers))


def _claimed_by_siblings(
    root: Path,
    siblings: Iterable[str],
    flag_key: str,
    accessors: tuple[str, ...],
    wrappers: bool,
) -> set[str]:
    """The key constants, and with ``wrappers`` the candidate wrapper names, the
    other configured members of the family take. A file mentioning one would be
    rewritten by that member's run as well, so this run must not take it.

    A wrapper may be declared in a file that only reads a key constant, so the
    names come from the member's own readers too (:func:`_own_candidates`).
    """
    names: set[str] = set()
    for other in siblings:
        names |= _key_const_names(
            _candidate_files(root, _LANGUAGES[other].extensions, flag_key), other, flag_key
        )
        if wrappers:
            names |= wrapper_inline.candidate_names(
                _own_candidates(root, other, flag_key), other, flag_key, accessors
            )
    return names


def _candidate_files_without_wrappers(
    repo_dir: str,
    language: str,
    flag_key: str,
    languages: Iterable[str],
    accessors: tuple[str, ...],
    wrappers: bool,
    claims: dict[tuple[str, ...], re.Pattern[str]],
) -> list[str]:
    spec = _language_spec(language)
    root = Path(repo_dir)
    found = _candidate_files(root, spec.extensions, flag_key)
    if not cross_file_keys.supported(language):
        return found
    names = _key_const_names(found, language, flag_key)
    siblings = [
        sibling
        for sibling in sorted(cross_file_keys.family(language) & set(languages))
        if sibling != language
    ]
    sibling_extensions = tuple(
        ext for sibling in siblings for ext in _LANGUAGES[sibling].extensions
    )
    resolved_root = root.resolve()
    extra: list[str] = []
    for mention in cross_file_keys.mention_files(repo_dir, names, exclude=found):
        sibling = mention.endswith(sibling_extensions)
        if not (sibling or mention.endswith(spec.extensions)):
            continue
        if not cross_file_keys.within(Path(mention), resolved_root):
            continue  # outside this run; the resolution refuses it instead
        try:
            text = Path(mention).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # never a reader; the resolution refuses it on its own
        if sibling:
            if flag_key in text:
                continue  # its own run rewrites it
            if _claimed(claims, root, siblings, flag_key, accessors, wrappers).search(text):
                continue  # a reader in another run of the family as well
        # Spelled under `repo_dir` like every other candidate, so a summary's
        # path and the snapshot's agree however `repo_dir` was written.
        if root.is_dir():
            mention = str(root / os.path.relpath(mention, resolved_root))
        extra.append(mention)
    return found + sorted(set(extra))


def _key_const_names(paths: Iterable[str], language: str, flag_key: str) -> set[str]:
    """The key constants and enum entries ``paths`` declare that another file can read."""
    names: set[str] = set()
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        names.update(
            c.name for c in externally_visible_key_consts(source, language, flag_key)
        )
        names.update(cross_file_keys.entry_names(path, source, language, flag_key))
    return names


def _grammar_of(path: str, language: str) -> str:
    """The language whose grammar parses ``path`` in a run for ``language``.

    ``language`` itself, except for a cross-file reader of a sibling file type
    in its key-constant family: a ``.tsx`` reader handed to the ``ts`` run is
    parsed, rewritten and checked as TSX. Every pass that parses a file asks
    this rather than trusting the run's language.
    """
    if path.endswith(_language_spec(language).extensions):
        return language
    for sibling in sorted(cross_file_keys.family(language)):
        if path.endswith(_LANGUAGES[sibling].extensions):
            return sibling
    return language


def _language_spec(language: str) -> _Language:
    try:
        return _LANGUAGES[language]
    except KeyError:
        raise ValueError(
            f"unsupported language {language!r}; expected one of {sorted(_LANGUAGES)}"
        ) from None


def _is_go_vendor_tree(directory: Path) -> bool:
    """Whether ``directory`` is a ``go mod vendor`` output tree.

    `vendor/` is to Go what `node_modules/` is to JS — third-party source the
    toolchain writes and rewrites — so cleaning a flag out of it is wrong twice
    over: the edit lands in the customer's pull request (unlike `node_modules`,
    a vendor tree is *committed*), and the next `go mod vendor` reverts it while
    `go build -mod=vendor` complains in between. A real run against a real
    repository committed exactly that.

    Detected by the `modules.txt` manifest the Go toolchain always writes there,
    NOT by the name alone. `vendor` is a perfectly ordinary directory name for
    hand-written code in other ecosystems, and this walk is shared by every
    language — pruning it unconditionally would silently stop cleaning a
    customer's own source.
    """
    return (directory / "modules.txt").is_file()


def source_files(
    root: Path, extensions: Iterable[str], skip_dirs: Iterable[str] = ()
) -> list[Path]:
    """Files under ``root`` with one of ``extensions``, skipped dirs pruned.

    ``os.walk`` rather than ``rglob("*")`` so a skipped directory is never
    DESCENDED INTO. The predicate form enumerated every path in
    ``node_modules`` — tens of thousands of entries in a normal checkout, plus
    a ``stat`` each — and then discarded them, so the skip list bought nothing
    on the axis it existed for. Here ``dirnames`` is edited in place, which is
    the documented way to tell ``os.walk`` not to recurse.

    Sorted, because the walk order varies between runs and machines and that
    ordering reaches the customer twice: as the order of the hunks in the
    reported diff, and as the order of the files named in the PR body.

    Shared by the transform's candidate scan and the orchestrator's
    unprocessed-reference scan. Those two ask *different* questions about the
    files (see ``orchestrate._mentions_flag_key``) but enumerate them
    identically, and duplicating the enumeration is how the two skip lists
    drifted apart in the first place.
    """
    suffixes = frozenset(extensions)
    if root.is_file():
        return [root] if root.suffix in suffixes else []
    skip = frozenset(skip_dirs)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in skip and not _is_go_vendor_tree(directory / d)
        ]
        found.extend(
            directory / name for name in filenames if Path(name).suffix in suffixes
        )
    return sorted(found)


def _candidate_files(root: Path, extensions: tuple[str, ...], flag_key: str) -> list[str]:
    """Source files that could possibly be rewritten for ``flag_key``.

    Every seed rule requires the flag key to appear as a string literal in the
    file it edits, so a file whose text doesn't contain the key can never
    change. Narrowing the codebase up front is not just an optimisation: the
    const-inlining rules are seeded on ``(identifier)``, i.e. Piranha evaluates
    their filters against every identifier in every file it is handed, which is
    quadratic-ish and takes tens of seconds over a large tree.
    """
    candidates = []
    for path in source_files(root, extensions, _SKIP_DIRS):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if flag_key in text:
            candidates.append(str(path))
    return candidates


def _load_argument_flags(base: str) -> dict[str, object]:
    """Read ``<base>_arguments.toml`` into PiranhaArguments keyword flags.

    Returns an empty dict when the file is absent. The ``[piranha_arguments]``
    table holds keyword flags (e.g. ``delete_consecutive_new_lines``) passed
    straight through to the ``PiranhaArguments`` constructor.
    """
    args_path = RULES_DIR / f"{base}_arguments.toml"
    if not args_path.exists():
        return {}
    with args_path.open("rb") as fh:
        data = tomllib.load(fh)
    return dict(data.get("piranha_arguments", {}))


def render_known_flag_keys(keys: Iterable[str], target: str, language: str) -> str:
    """Render the project's OTHER flag keys for the ``@known_flag_keys`` hole.

    The value is spliced textually into a tree-sitter query as the argument
    list of ``(#any-of? @sibling …)``, so three rules apply, in this order:

    * the target is excluded — it must never count as its own sibling;
    * every remaining key must pass :func:`ensure_supported_key`, the same
      grammar the target itself is held to; one that does not is dropped
      with a warning rather than spliced (it is a query-injection surface,
      and no rule could match it as a literal anyway);
    * keys are emitted as space-separated double-quoted literals, sorted so
      the rendering is stable across runs.

    Dart is the one language rendered differently: its ``string_literal`` has
    no content child, so ``dart_entries.toml`` compares the whole literal,
    quotes included, and a registry may spell its keys with either quote.
    Both spellings are emitted; the double-quoted one is escaped for the
    query string.

    An empty result is the empty string on purpose. ``(#any-of? @s )`` was
    measured to match nothing without error, which is exactly the behaviour
    wanted when the flag list could not be fetched: the registry prong
    switches itself off and the stub prong is unaffected.
    """
    rendered: list[str] = []
    for key in sorted(set(keys) - {target}):
        try:
            ensure_supported_key(key)
        except UnsupportedFlagKeyError:
            logger.warning(
                "dropping flag key %r from the sibling list: it cannot be "
                "spliced into a rewrite rule safely, so no registry entry "
                "will be recognised by it this run",
                key,
            )
            continue
        if language == "dart":
            rendered.append(f"\"'{key}'\"")
            rendered.append(f'"\\"{key}\\""')
        else:
            rendered.append(f'"{key}"')
    return " ".join(rendered)


#: Every rule in a ``rules/<base>_entries.toml`` is named with this prefix, and
#: ``test_every_entry_rule_carries_the_reporting_prefix`` fails the build if one
#: is not: the pull-request body's "flag-keyed entries were removed" paragraph
#: is built by matching it against ``Edit.matched_rule``.
_ENTRY_RULE_PREFIX = "delete_flag_keyed_"


def _flag_keyed_entries(
    summaries, repo_dir: str
) -> tuple[tuple[tuple[str, str], ...], set[str]]:
    """``((relative_path, entry_text), …)`` and the absolute paths they came from.

    Read off the ENGINE's summaries — only those carry ``rewrites`` with the
    matched rule's name — and therefore before any text pass replaces a
    changed summary with a plain :class:`Rewrite`, which has none. The
    engine's match range for a deleted entry already includes the entry's
    trailing separator (measured: ``'k': 2,``), so it is stripped here; the
    body is for a reviewer, not a diff.
    """
    entries: list[tuple[str, str]] = []
    paths: set[str] = set()
    for summary in summaries:
        for edit in getattr(summary, "rewrites", ()):
            if not edit.matched_rule.startswith(_ENTRY_RULE_PREFIX):
                continue
            text = edit.p_match.matched_string.strip().strip(",;").strip()
            entries.append((os.path.relpath(summary.path, repo_dir), text))
            paths.add(summary.path)
    return tuple(entries), paths


#: The engine's built-in rules that delete a loop its own fold made dead
#: (``while (false)``, Go's ``for false``, Ruby's ``until true``, the TS
#: family's ``for (…; false; …)``) across every language's cascade, or unwrap
#: one whose body runs once (the TS family's ``do … while (false)``). A Ruby
#: loop that was a body's last statement becomes ``nil`` instead, keeping the
#: body's value, and is listed all the same. Each
#: firing is listed with the stranded statements, as the deletion was when this
#: side made it: behaviour-neutral, but the loop was the customer's code.
_ENGINE_DEAD_LOOP_RULES = frozenset(
    {
        "delete_while_false",
        "delete_while_false_before_statement",
        "delete_while_false_at_end_to_nil",
        "delete_for_false",
        "delete_first_while_false",
        "delete_while_false_after_statement",
        "replace_sole_while_false_with_pass",
        "delete_for_false_without_initializer",
        "delete_for_false_with_inert_lexical_initializer",
        "replace_for_false_with_var_initializer",
        "replace_for_false_with_expression_initializer",
        "unwrap_do_false_splice",
        "unwrap_do_false_keep_block",
    }
)


#: The engine's built-in rules that delete a statement its own fold left
#: unreachable: Java's and Dart's code after a folded ``return``, and the TS
#: family's after a ``return``/``throw``/``break``/``continue`` (a ``var x = v``
#: there keeps ``var x;``, and is listed as it was). Listed only in a file whose
#: language rejects unreachable code (:func:`~flag_cleanup.syntax.unreachable_is_fatal`),
#: the languages the pull request's sentence names, exactly as
#: :func:`~flag_cleanup.syntax.remove_unreachable_statements` was scoped when
#: this side made every such deletion itself. Elsewhere the engine deletes the
#: same tail (C#, Kotlin, PHP, …) and the code would have compiled either way.
_ENGINE_UNREACHABLE_RULES = frozenset(
    {
        "delete_all_statements_after_return",
        "delete_all_statements_after_returning_block",
        "delete_unreachable_statement",
        "strip_unreachable_typed_var_initializer",
        "strip_unreachable_var_initializer",
    }
)


#: The engine's Go rules that delete a local bound to the folded read and
#: write its value for the local's name (``replace_identifier_with_value``,
#: ``replace_identifier_with_nil``): the capture names whose identifiers go.
#: The ``split_*`` pair drops the folded name from a two-name declaration
#: (``on, n := true, load()`` becomes ``n := load()``) and inlines it the same way.
_GO_INLINE_RULES = {
    "delete_variable_declaration": ("variable_name",),
    "delete_variable_declaration_with_nil": ("variable_name", "err"),
    "split_short_var_declaration_literal_first": ("variable_name",),
    "split_short_var_declaration_literal_last": ("variable_name",),
}


def _engine_inlined(summaries) -> dict[str, frozenset[str]]:
    """``{path: names}`` for each Go local the engine inlined, by rule name.

    Read off the engine's summaries under the same constraint as
    :func:`_engine_stranded`. See :mod:`flag_cleanup.go_inline`.
    """
    inlined: dict[str, frozenset[str]] = {}
    for summary in summaries:
        names = {
            edit.p_match.matches[capture]
            for edit in getattr(summary, "rewrites", ())
            for capture in _GO_INLINE_RULES.get(edit.matched_rule, ())
            if capture in edit.p_match.matches
        }
        if names:
            inlined[summary.path] = frozenset(names)
    return inlined


def _engine_stranded(summaries, language: str, repo_dir: str) -> tuple[tuple[str, str], ...]:
    """``((relative_path, text), …)`` for each dead loop or unreachable statement the engine deleted.

    Read off the engine's summaries by rule name, so under the same constraint
    as :func:`_flag_keyed_entries`: before any text pass replaces a changed
    summary with a plain :class:`Rewrite`. The engine's match range is the
    deleted node plus the whitespace in front of it, which is stripped.
    """
    return tuple(
        (os.path.relpath(summary.path, repo_dir), edit.p_match.matched_string.strip())
        for summary in summaries
        for edit in getattr(summary, "rewrites", ())
        if edit.matched_rule in _ENGINE_DEAD_LOOP_RULES
        or (
            edit.matched_rule in _ENGINE_UNREACHABLE_RULES
            and unreachable_is_fatal(_grammar_of(summary.path, language))
        )
    )


#: The engine's built-in rules that delete an import its own fold stranded,
#: each mapped to the capture the pull request names it by. Never the match: a
#: spec's match also holds the comments and blank lines it took (``spec``), and
#: the ``*_solo`` pair's starts at the declaration before it (``decl``). The
#: PHP rule deletes a one-clause ``use`` declaration and is named by the whole
#: of it (``decl``), since a deleted line's match also holds the comment after it.
#: The Java and Kotlin rules delete one import declaration and are named by the
#: whole of it (``decl``) for the same reason. The two TypeScript/JavaScript
#: rules remove one named specifier of an ``import { … }`` group and are named
#: by it (``spec``): each rewrites a span that also covers a neighbouring
#: specifier and writes that neighbour back, so the match holds two.
_ENGINE_STRANDED_IMPORT_RULES = {
    "delete_stranded_import_aliased": "spec",
    "delete_stranded_import_unaliased": "spec",
    "delete_stranded_import_aliased_solo": "decl",
    "delete_stranded_import_unaliased_solo": "decl",
    "delete_stranded_php_import": "decl",
    "delete_stranded_java_import": "decl",
    "delete_stranded_kotlin_import": "decl",
    "delete_stranded_ts_import_specifier": "spec",
    "delete_stranded_ts_import_specifier_last": "spec",
}


#: The engine's built-in rules that delete a local its own fold stranded,
#: discard it to ``_ = v``, or drop its name from a ``range``, receive or
#: type-switch header. The ``*_pair`` rules do the same for a two-name
#: declaration and report the whole of it (``x, y := 3, load()``). Each is
#: mapped to the capture the pull request names it by. Never the match: a
#: deleted declaration's match also takes the comment after it on its line,
#: and a name dropped from a ``var`` list spans the name kept beside it. The
#: two Ruby rules delete an ``x = v`` assignment, or keep ``v`` alone when it
#: is a call, and are named by the assignment (``a``), since a deleted line's
#: match also holds the comment after it. The two Dart rules delete an inert
#: local, or keep a call's value as a bare statement (``final x = call();``
#: becomes ``call();``), and are named by the whole declaration (``decl``).
#: The one Python rule deletes an ``x = <literal>`` statement, and the one PHP
#: rule a ``$x = <literal>;`` statement; each is named by the statement
#: (``decl``), since a deleted line's match also holds the comment after it.
#: The one Swift, Java, Kotlin and C# rules and the two TypeScript/JavaScript
#: rules (``const``/``let`` and ``var``) each delete a literal local's whole
#: declaration and are named by it (``decl``), for the same reason.
_ENGINE_STRANDED_LOCAL_RULES = {
    "delete_stranded_short_var": "decl",
    "blank_stranded_short_var": "decl",
    "delete_stranded_var_declaration": "decl",
    "blank_stranded_var_declaration": "decl",
    "blank_stranded_typed_var_declaration": "decl",
    "delete_stranded_short_var_pair": "decl",
    "blank_stranded_short_var_pair": "decl",
    "blank_stranded_short_var_pair_first": "decl",
    "blank_stranded_short_var_pair_last": "decl",
    "delete_stranded_var_pair": "decl",
    "blank_stranded_var_pair": "decl",
    "blank_stranded_var_pair_first": "decl",
    "blank_stranded_var_pair_last": "decl",
    "blank_stranded_typed_var_pair": "decl",
    "blank_stranded_typed_var_pair_first": "decl",
    "blank_stranded_typed_var_pair_last": "decl",
    "delete_stranded_var_block": "decl",
    "delete_stranded_var_spec_in_block": "spec",
    "delete_stranded_var_name_first": "n",
    "delete_stranded_var_name_later": "n",
    "stranded_range_last": "left",
    "stranded_range_blank_first": "whole",
    "stranded_range_first": "n",
    "stranded_range_only": "whole",
    "stranded_receive_last": "left",
    "stranded_receive_blank_first": "whole",
    "stranded_receive_first": "n",
    "stranded_receive_only": "whole",
    "stranded_type_switch_alias": "alias",
    "stranded_if_init": "init",
    "stranded_switch_init": "init",
    "delete_stranded_ruby_local": "a",
    "discard_stranded_ruby_local": "a",
    "delete_stranded_dart_local": "decl",
    "discard_stranded_dart_local": "decl",
    "delete_stranded_python_local": "decl",
    "delete_stranded_php_local": "decl",
    "delete_stranded_swift_local": "decl",
    "delete_stranded_java_local": "decl",
    "delete_stranded_kotlin_local": "decl",
    "delete_stranded_csharp_local": "decl",
    "delete_stranded_ts_local": "decl",
    "delete_stranded_ts_var": "decl",
}


def _engine_bindings(summaries, repo_dir: str) -> tuple[tuple[str, str, str], ...]:
    """``((relative_path, kind, text), …)`` for each import or local the engine
    removed because its own fold stranded it.

    The same record :func:`_remove_stranded_bindings` makes for one this side
    removes, so the pull request names it either way. Read off the engine's
    summaries by rule name, under the same constraint as
    :func:`_engine_stranded`.
    """
    found = []
    for summary in summaries:
        relative = os.path.relpath(summary.path, repo_dir)
        for edit in getattr(summary, "rewrites", ()):
            for kind, rules in (
                ("import", _ENGINE_STRANDED_IMPORT_RULES),
                ("variable", _ENGINE_STRANDED_LOCAL_RULES),
            ):
                capture = rules.get(edit.matched_rule)
                if capture is not None:
                    match = edit.p_match
                    text = match.matches.get(capture) or match.matched_string
                    found.append((relative, kind, text.strip()))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class TransformOutcome:
    """What one ``(repo_dir, language, flag)`` transform produced.

    ``unprocessable`` names files the ENGINE could not process — its bundled
    grammar is older than the source, so it aborted rather than rewriting them.
    They are carried out rather than swallowed because they still hold a live
    read of the flag: the pull request must say so, exactly as it does for a
    file whose extension no configured language covers. See
    :func:`_quarantine_and_retry`.

    Empty ``unprocessable`` is the overwhelmingly common case, and it is what a
    caller that only wants the diff can ignore — :func:`run_piranha` does.
    """

    diff: str
    unprocessable: tuple[str, ...] = ()
    #: ``(path, statement)`` for every statement the fold made unreachable and
    #: this transform therefore deleted. Scoped to the languages where leaving
    #: it standing breaks the build — Java, Dart and the TypeScript family as of
    #: #2807 — but ask :func:`~flag_cleanup.syntax._unreachable_dialect` rather
    #: than trusting a list written out here. See
    #: :func:`~flag_cleanup.syntax.remove_unreachable_statements`. Carried out so
    #: the pull request can name each one: the deletion is behaviour-neutral by
    #: construction, but it removes code the customer wrote, and a reviewer who
    #: cannot see that in the body has to find it in the diff.
    stranded: tuple[tuple[str, str], ...] = ()
    #: ``(path, kind, text)`` for every import or local variable the fold
    #: stranded and this transform therefore deleted or rewrote. Reported
    #: separately from ``stranded`` because that field's PR-body sentence is
    #: specifically about unreachable STATEMENTS — reusing it would put a false
    #: sentence in front of the customer. See
    #: :func:`~flag_cleanup.unused.remove_stranded_bindings`. An import cut down
    #: to a bare one rather than deleted has kind ``"bare import"`` and the
    #: line it became as its text, because that line is still in the diff and
    #: the body asks the reviewer to judge it. So does a CommonJS
    #: ``const { client } = require("./m")`` discarded to ``require("./m");``.
    #: A Python import is never cut down: it is kept whole and marked
    #: ``# noqa: F401``, with kind ``"marked import"`` and that line as text.
    #: A TS/JS import that was the file's last ``import`` or ``export`` is
    #: replaced by ``export {};`` so the file stays a module, with kind
    #: ``"module marker"`` and that line as text.
    #: A C# primary-constructor parameter the fold left unread is NOT deleted
    #: (that would change the constructor's signature) but is listed here with
    #: kind ``"unread parameter"`` and ``Type(<parameter>)`` as text, because
    #: CS9113 fails a ``TreatWarningsAsErrors`` build. See
    #: :mod:`~flag_cleanup.primary_constructor`. The TypeScript family's
    #: counterpart is kind ``"unread member"`` with ``Class.member`` as text: a
    #: ``private`` or ``#`` member the fold left unread, TS6133/TS6138 under
    #: ``noUnusedLocals``. See :mod:`~flag_cleanup.ts_unread_members`, which
    #: also finds a plain constructor parameter left unused, kind ``"unread
    #: constructor parameter"`` with ``Class(parameter)`` as text. A Dart
    #: private field the fold left unread (``unused_field``) is kind
    #: ``"unread member"`` too; see :mod:`~flag_cleanup.dart_field_removal`. The
    #: family adds two more kinds that name something KEPT, both reported and
    #: never edited:
    #: ``"unread binding"`` (the parameter or local an override deleted by
    #: :mod:`~flag_cleanup.ts_bound_overrides` was the last reader of) and
    #: ``"emptied override"`` (the line of an object literal the entry
    #: deletions left as ``{}``).
    bindings: tuple[tuple[str, str, str], ...] = ()
    #: ``(path, text)`` for every flag-keyed ENTRY the ``*_entries.toml``
    #: rules deleted — a registry line, an override property, an object-type
    #: member. Reported so the reviewer sees the registry line went and under
    #: which evidence rule, rather than finding it in the diff. Read off the
    #: engine's own summaries by rule-name prefix; see :func:`_flag_keyed_entries`.
    entries: tuple[tuple[str, str], ...] = ()
    #: ``(path, text)`` for every flag-keyed entry whose value is the boolean
    #: literal this run is REMOVING, which both `*_entries.toml` prongs decline
    #: to delete (#3050). The mirror image of ``entries``, and reported for the
    #: opposite reason: a rule that does not fire leaves no summary behind, so
    #: without this the file lands in the generic "still references this flag"
    #: caveat with none of what the tool actually knows about it. Found by
    #: :func:`~flag_cleanup.contradicting.find_contradicting_entries`, not by
    #: the engine.
    declined: tuple[tuple[str, str], ...] = ()
    #: ``(path, kind, text)`` for every test-double line and test this run
    #: deleted because it stubbed or verified the read being removed: ``kind``
    #: is ``stub`` or ``verification`` (``text`` is the line) or ``test``
    #: (``text`` is the test's name). Only when the run folded a real read; see
    #: :mod:`flag_cleanup.mock_stubs`. Reported because these were the
    #: customer's tests, and a reviewer should not find a deleted assertion
    #: only in the diff.
    test_doubles: tuple[tuple[str, str, str], ...] = ()
    #: ``(path, title)`` for every test deleted because its title says it
    #: exercises the branch the fold removed, and a flag-keyed entry inside it
    #: pinned the flag to that branch. TypeScript family only — the language
    #: whose tests carry titles — and the reason ``declined`` is always empty
    #: there: those rules strip every pin, and this names the tests that would
    #: otherwise have gone red asserting code that no longer exists. See
    #: :mod:`flag_cleanup.ts_tests`.
    deleted_tests: tuple[tuple[str, str], ...] = ()
    #: ``(path, reason)`` for every file this transform deliberately put back
    #: exactly as it was, although the rules had rewritten it: a test that
    #: still names the key as DATA once its rewrite is on disk (a gate's key
    #: prop, a string handed to the code under test), whose overrides are
    #: therefore still live. See :func:`~flag_cleanup.ts_tests.data_mention`.
    #: TypeScript family only. ``orchestrate`` adds the registry entries it
    #: keeps to the same list.
    kept: tuple[tuple[str, str], ...] = ()
    #: References to a cross-file key constant this run could not prove are
    #: reads of this flag: ``(path, line, reason, text)``, paths relative to the
    #: repository. Each one is why a declaration was kept, so each one is a
    #: line in the pull request.
    unresolved: tuple[tuple[str, int, str, str], ...] = ()
    #: What the run found about enum entries holding the key:
    #: ``(path, line, reason, text)``, paths relative to the repository.
    #: ``reason`` is ``kept`` for an entry whose every read was folded and
    #: which was left in place, ``enum-dynamic`` for a use of the whole enum
    #: that can reach the entry unseen, and otherwise a refusal reason for a
    #: mention that is not a proven read. Kept apart from ``unresolved``,
    #: whose wording is about a constant's declaration.
    enum_findings: tuple[tuple[str, int, str, str], ...] = ()
    #: What became of each keyless wrapper of this flag, and why any caller of
    #: one was left: ``(path, line, reason, text, wrapper)``, paths relative
    #: to the repository. Each qualified wrapper has one row at its
    #: declaration whose reason is ``complete`` (every caller folded, the
    #: wrapper deleted) or ``partial`` (kept, and every remaining caller has a
    #: row of its own). A wrapper that did not qualify has one row at its
    #: declaration giving the reason. See :mod:`flag_cleanup.wrapper_inline`.
    wrapper_findings: tuple[tuple[str, int, str, str, str], ...] = ()


def run_piranha(
    repo_dir: str,
    language: str,
    flag_key: str,
    treatment: bool,
    paths: list[str] | None = None,
    accessors: tuple[str, ...] = (),
    known_flag_keys: frozenset[str] = frozenset(),
    gates: tuple[ts_gates.Gate, ...] = (),
    inline_wrappers: bool = True,
) -> str:
    """:func:`transform_flag`, keeping only the diff.

    The original entry point, and still the right one wherever the caveat is
    not actionable — a test asserting what a rewrite produced, or the corpus
    check, which only asks whether the tree came back byte-identical.
    ``orchestrate`` uses :func:`transform_flag` instead, because it is the one
    caller that can put ``unprocessable`` in front of a human.

    ``accessors`` is forwarded so a fixture case can exercise the accessor
    path: those clones are generated per run rather than checked in, so a
    fixture that could not name a wrapper could only ever cover the SDK's own
    call shapes.

    ``known_flag_keys`` is the project's flag list, which the entry rules'
    registry prong uses as sibling evidence ("another key beside this one is
    also a flag of this project"); a fixture case supplies it through
    ``Case.known_flag_keys``.
    """
    return transform_flag(
        repo_dir, language, flag_key, treatment, paths, accessors,
        known_flag_keys=known_flag_keys, gates=gates, inline_wrappers=inline_wrappers,
    ).diff


def transform_flag(
    repo_dir: str,
    language: str,
    flag_key: str,
    treatment: bool,
    paths: list[str] | None = None,
    accessors: tuple[str, ...] = (),
    known_flag_keys: frozenset[str] = frozenset(),
    gates: tuple[ts_gates.Gate, ...] = (),
    inline_wrappers: bool = True,
) -> TransformOutcome:
    """Remove ``flag_key`` from the code under ``repo_dir`` and return a diff.

    The work is in :func:`_transform_flag`. This wrapper exists for the
    test-double pre-pass (:mod:`flag_cleanup.mock_stubs`), the one edit made
    to the customer's files before the engine runs, and it does two things:

    * Every way out of the transform that raises puts those files back, and
      there are a dozen. Restoring after a Gate 1 refusal repeats what the
      refusal already wrote, which is harmless — the bytes are the customer's
      either way.
    * When the engine folded no real read, the pass's edits have no
      justification, and the engine has already run over them. The whole
      transform is undone and run again WITHOUT the pass, so the outcome is
      exactly what it was before the pass existed: a stub the fold breaks is
      refused, a Moq stub is left alone. Withdrawing the edits and keeping the
      engine's output instead would hand the stubs back un-refused — and
      with ``tests/`` configured as its own directory, that is a pull request
      leaving stubs of a read the ``src/`` pass removed.
    """
    doubles: list[mock_stubs.Prepass] = []
    # One listing of the checkout for every scan this transform makes, and for
    # the retry: nothing here adds or removes a file. Inside a run's scope,
    # the run's listing.
    with source_index.run_scope(), source_index.forgetting():
        try:
            return _transform_flag(
                repo_dir, language, flag_key, treatment, paths, accessors,
                known_flag_keys, doubles, gates, inline_wrappers,
            )
        except _NoReadFolded:
            return _transform_flag(
                repo_dir, language, flag_key, treatment, paths, accessors,
                known_flag_keys, None, gates, inline_wrappers,
            )
        except BaseException:
            # Newest first: a later pass's "original" is an earlier pass's
            # output, and the customer's bytes must be the last ones written.
            for prepass in reversed(doubles):
                prepass.restore_all()
            raise


class _NoReadFolded(Exception):
    """The test-double pre-pass was undone because no real read was folded."""


def _transform_flag(
    repo_dir: str,
    language: str,
    flag_key: str,
    treatment: bool,
    paths: list[str] | None,
    accessors: tuple[str, ...],
    known_flag_keys: frozenset[str],
    doubles: list[mock_stubs.Prepass] | None,
    gates: tuple[ts_gates.Gate, ...] = (),
    inline_wrappers: bool = True,
) -> TransformOutcome:
    """Remove ``flag_key`` from the code under ``repo_dir`` and return a diff.

    Parameters
    ----------
    repo_dir:
        Directory (or file) of source to transform, in place.
    language:
        One of :func:`supported_languages` — selects the Piranha language id,
        the rule-file base, and the source extensions to scan.
    flag_key:
        The flag key, matched against the first (string-literal) argument of the
        SDK read call. A non-literal key never matches -> no change (safety).
    treatment:
        ``True`` keeps the on-branch (Piranha ``treated=true``); ``False`` keeps
        the off-branch. Mirrors the endpoint's ``treatment`` field.
    paths:
        The files to hand the engine. Default ``None`` scans ``repo_dir``.
        Pass the result of :func:`candidate_files` for the SAME
        ``(repo_dir, language, flag_key)`` and nothing else: the orchestrator
        already computes exactly that list to build its undo snapshot, and
        re-deriving it here walked and byte-read the whole tree a second time
        for every candidate. Anything wider than that list is outside the
        snapshot, i.e. outside the undo.
    known_flag_keys:
        Every flag key of the project (live and archived), for the
        ``rules/<base>_entries.toml`` registry prong. Rendered by
        :func:`render_known_flag_keys`; the empty set disables that prong and
        nothing else.
    gates:
        JSX components that render their children only while a flag is on,
        each with its key prop (``FlagGate[flag]`` in the ``accessors``
        input). Unwrapped on ``true``, refused on ``false``; see
        :mod:`flag_cleanup.ts_gates`.
    inline_wrappers:
        Inline keyless wrappers of this flag at their proven call sites and
        delete them once nothing calls them; see
        :mod:`flag_cleanup.wrapper_inline`. ``False`` folds the read inside a
        wrapper and stops there, as before that existed.

    Returns
    -------
    str
        Unified diff of every file Piranha rewrote (paths relative to
        ``repo_dir``). Empty string when nothing changed.

    Raises
    ------
    PiranhaTransformError
        The engine aborted (e.g. its own syntax self-check rejected the
        rewrite). Nothing was written — Piranha itself buffers until every
        file is processed, and anything an EARLIER invocation of it wrote in
        this call is rolled back below (Gate 2 splits the files into two
        groups and runs the engine once per group, so "an earlier
        invocation" is a real case). Treat it as "skip this flag".
    UnsafeRewriteError
        Gate 1 refused the rewrite of at least one file. Everything this call
        wrote has been rolled back; treat it as "skip this flag", and say so —
        it is NOT a no-match.
    UnsupportedFlagKeyError
        The key cannot be substituted into the rules safely (see
        :func:`ensure_supported_key`). Nothing ran.
    ValueError
        ``language`` is not one of :func:`supported_languages`.
    """
    ensure_supported_key(flag_key)
    spec = _language_spec(language)
    piranha_language, base = spec.piranha_id, spec.rule_base

    # Defined before the first return so every later reader of it can rely on
    # it; replaced once Gate 2 has partitioned the files.
    resolution = cross_file_keys.Resolution({}, (), frozenset())

    if paths is None:
        paths = candidate_files(
            repo_dir, language, flag_key, accessors=accessors, inline_wrappers=inline_wrappers
        )
    if not paths:
        return TransformOutcome("")

    # A file of a sibling file type (see `candidate_files`) is there only as a
    # possible cross-file reader, and reaches nothing but the resolution and
    # the reader invocation. Every pass before the engine asks about the KEY,
    # which such a file does not hold, and parses with this run's grammar,
    # which is not the file's own.
    sibling_readers = [p for p in paths if _grammar_of(p, language) != language]
    paths = [p for p in paths if p not in sibling_readers]

    # PRE-FLIGHT — drop any file the GATE cannot fully parse, before the engine
    # is handed it. Dart only; see `syntax.source_is_unreadable` for the defect
    # this closes and the corpus measurement that makes an absolute parse check
    # affordable there. Deliberately BEFORE Gate 2 and the engine: the whole
    # point is that neither of them can see the hazard, so a check that ran
    # afterwards would be reading a file the engine had already corrupted.
    paths, skipped = _partition_by_readability(paths, language)

    # Resolve the test doubles that stub or verify this read, on the customer's
    # bytes and BEFORE everything below reads them: Mockito, MockK, Dart
    # mockito, ts-mockito and gomock take a real call, which the engine would
    # fold with every other read, leaving nothing to recognise. Kept or undone
    # after the engine, once it is known whether a real read was folded; see
    # :mod:`flag_cleanup.mock_stubs`. Before the entry and pinned-test reads
    # below, so neither describes a test this has already deleted.
    if doubles is not None and mock_stubs.covers(language):
        stub_pass = mock_stubs.Prepass(language, flag_key, treatment, accessors)
        doubles.append(stub_pass)
        stub_pass.apply(paths)

    # Computed here, off the CUSTOMER's bytes, before anything rewrites them.
    # It is the only reading of the tree that has to happen first: every prong
    # in `<base>_entries.toml` declines this shape, so it is still on disk
    # afterwards — but the fold has rewritten the code around it by then, and a
    # file the engine quarantines never reaches the post state at all. Cheap,
    # and bounded by the same candidate list the engine gets.
    untreated = spec.false_literal if treatment else spec.true_literal
    declined = contradicting.find_contradicting_entries(
        paths, language, flag_key, untreated, repo_dir
    )
    # The TypeScript family's answer to the same shape, read off the same
    # customer bytes for the same reason: the rules strip the pin, so after the
    # engine there is nothing left to find. See :mod:`flag_cleanup.ts_tests`.
    pinned_tests = _find_pinned_tests(paths, language, flag_key, untreated)
    # The overrides a helper's parameter feeds, proven on the same bytes for
    # the same reason: no rule touches them, but the engine may still delete
    # one beside another flag's key. See :mod:`flag_cleanup.ts_param_pins`.
    parameter_pins = _find_parameter_pins(paths, language, flag_key)

    # A gate component is decided on the customer's bytes too, and its refusal
    # raised here, before the engine writes anything — the `false` treatment
    # cannot be unwrapped, and there is nothing to roll back yet. The test-
    # double pass above is undone by `transform_flag`, which catches this.
    gate_paths = _check_gates(paths, language, flag_key, gates, treatment)

    if not paths:
        # Every candidate was unreadable, so there is nothing to transform —
        # but this is emphatically NOT a no-match, and returning a bare empty
        # diff would report it as one. The files ride out on `unprocessable`,
        # which is what puts them in front of a human.
        return TransformOutcome(
            "", unprocessable=tuple(skipped), declined=declined
        )

    rules_file = RULES_DIR / f"{base}.toml"
    const_rules_file = RULES_DIR / f"{base}_const.toml"
    edges_file = RULES_DIR / f"{base}_edges.toml"
    if not rules_file.exists():
        raise FileNotFoundError(f"missing Piranha rules file: {rules_file}")

    # Flag-keyed ENTRY rules (registry lines, override properties, object-type
    # members) — the residue a read-anchored rule cannot see. Appended to the
    # base file; every rule in it is a seed, so order relative to the base file
    # is irrelevant. Absent for a language that has none yet: Swift and Java
    # wait on the fork shipping `replace_node_end`, because their map entries
    # cannot be deleted along with their separator in one edit.
    entries_rules_file = RULES_DIR / f"{base}_entries.toml"

    # A project's own accessors become extra rules, generated per run rather
    # than checked in: they depend on an input. Kotlin is excluded on purpose —
    # its rules match a sentinel, not a name, so its accessor support is in the
    # pre-pass below, which is the only part of this that can read a key.
    generated_rules: tempfile.TemporaryDirectory | None = None
    if accessors and language != "kt":
        generated_rules = tempfile.TemporaryDirectory(prefix="flag-cleanup-rules-")
        rules_file, const_rules_file = _write_generated_rules(
            Path(generated_rules.name), rules_file, const_rules_file, accessors
        )

    # `untreated` is `treated`'s complement — the literal spelling of the branch
    # this run is DELETING — and it exists for the `*_entries.toml` rules alone.
    # A flag-keyed entry whose value is that literal is the customer saying
    # "exercise the branch we are about to remove", which is evidence the
    # enclosing code is about to become dead rather than evidence the entry is
    # safe to strip; both entry prongs decline it and
    # `contradicting.find_contradicting_entries` reports it. See #3050 and the
    # header of any `rules/*_entries.toml`.
    substitutions = {
        "stale_flag_name": flag_key,
        "treated": spec.true_literal if treatment else spec.false_literal,
        "untreated": spec.false_literal if treatment else spec.true_literal,
        "known_flag_keys": render_known_flag_keys(known_flag_keys, flag_key, language),
        # Python only in practice — `python.toml`'s marker rule is the one
        # consumer — but supplied unconditionally, because a substitution no
        # rule names costs nothing and a rule whose hole is absent aborts the
        # engine. See `_inline_bound_reads`.
        "marker": marker_for(flag_key),
    }
    # The sentinel rules of Java, C#, Go, Dart and the TS family name the hole
    # whether or not this run marks a wrapper call, and a hole no substitution
    # supplies aborts the engine. Kotlin's is supplied by its pre-pass, which
    # always exists.
    if language in _SPAN_ONLY_PREPASS:
        substitutions["sentinel"] = kotlin_sentinel.sentinel_for(flag_key)
    argument_flags = _load_argument_flags(base)

    # GATE 2 — computed BEFORE the pre-pass, for two reasons that both matter
    # only since Kotlin gained a key-const profile (#2671). It has to read the
    # CUSTOMER's bytes: after the pre-pass a Kotlin file holds sentinel-ified
    # callees, and judging that text would ask the gate about source the
    # customer never wrote. And its verdict has to reach the pre-pass, because
    # for Kotlin the FOLD is the pre-pass's doing — sentinel-ifying a call is
    # what makes `kt.toml` match it — so a gate consulted afterwards could not
    # withhold anything. Every other language has no pre-pass, so the file bytes
    # here are identical either way and moving this changes nothing for them.
    with_const, without_const = _partition_by_const_safety(
        paths, language, flag_key, accessors, repo_dir=repo_dir
    )

    # CROSS-FILE KEY CONSTANTS -- also before the pre-pass, which has to see
    # the declaring files this re-admits. Gate 2 above withholds every
    # declaring file another file could read; the resolution can only WIDEN
    # that, by proving every mention in the repository is a read the reader
    # rules remove. See :mod:`flag_cleanup.cross_file_keys`.
    (
        resolution,
        reader_paths,
        reader_const,
        regained,
        with_const,
        without_const,
    ) = _resolve_cross_file(
        repo_dir,
        language,
        flag_key,
        accessors,
        paths + sibling_readers,
        with_const,
        without_const,
    )

    # THE KOTLIN PRE-PASS — besides the test-double pass, the only thing in
    # this tool that edits the customer's files before the engine does. It
    # marks the calls the rules are allowed to rewrite, because Kotlin rules
    # cannot read a string literal and so cannot tell one flag's guard from
    # another's. Everything it writes is undone below on every path: it owns
    # the whole responsibility, which is why the restore is
    # driven from `prepass.originals` rather than from the engine's summaries
    # (those carry the pre-pass's text as their "before", and writing THAT
    # back would leave a sentinel on disk).
    # A Kotlin reader of a constant declared in another file is folded by the
    # pre-pass like any other read: it marks each call whose first argument is
    # one of the texts the resolution proved names this flag's key there.
    proven = _proven_readers(resolution, paths + sibling_readers)

    # KEYLESS WRAPPERS -- a member whose whole body is a read this run folds,
    # inlined at every call site the file itself proves is bound to it. On
    # the customer's bytes (less the test-double pass's deletions), and before
    # the pre-pass, which is what marks the proven calls. See
    # :mod:`flag_cleanup.wrapper_inline`.
    wrappers = wrapper_inline.WrapperPlan()
    if inline_wrappers and wrapper_inline.profile(language) is not None:
        wrappers = _plan_wrappers(
            repo_dir, language, flag_key, treatment, accessors, paths + sibling_readers, resolution,
            with_const, without_const, reader_paths, reader_const, doubles,
        )
        # A call-site file that neither holds the key nor reads its constant
        # is in no engine group yet; the marks are its only reads.
        grouped = set(with_const) | set(without_const) | set(reader_paths)
        without_const = without_const + [p for p in wrappers.spans if p not in grouped]

    # A COMPOSER DEPENDENCY (a `vendor/` beside a `composer.json`) is never
    # rewritten: `composer install` puts it back, and a team that commits
    # `vendor/` would get a dependency edited in its pull request. It stays a
    # candidate up to here because the resolution needs it: a key constant or
    # enum case declared in a dependency is still read by the application, and
    # those reads fold while the declaration stays where it is.
    with_const = [p for p in with_const if not cross_file_php.vendored(Path(p).resolve())]
    without_const = [p for p in without_const if not cross_file_php.vendored(Path(p).resolve())]
    reader_paths = [p for p in reader_paths if not cross_file_php.vendored(Path(p).resolve())]

    prepass = _sentinel_prepass(
        language,
        flag_key,
        accessors,
        const_key_paths=frozenset(with_const),
        external_keys=_external_keys(language, resolution, proven),
        wrapper_spans={p: tuple(spans) for p, spans in wrappers.spans.items()},
    )
    if prepass is not None:
        substitutions["sentinel"] = prepass.sentinel
        try:
            # A sibling caller of a keyless wrapper (a `.tsx` caller of a `.ts`
            # wrapper; see `candidate_files`) is marked like any other. Every
            # other pre-engine pass still skips it: it holds no key.
            prepass.apply(paths + [p for p in sibling_readers if p in wrappers.spans])
        except kotlin_sentinel.SentinelCollisionError as exc:
            # "This file must not be transformed", and it can fire after earlier
            # files were already marked — so the undo runs before the refusal
            # propagates. Reported as a refusal rather than as an empty diff: it
            # is a shape the customer has to act on, and a silent `no-changes`
            # would repeat forever.
            prepass.restore_all()
            logger.warning("refusing to transform for flag %r: %s", flag_key, exc)
            raise UnsafeRewriteError(
                f"the {spec.display_name} pre-pass refused this transform for flag "
                f"{flag_key!r}: {exc}",
                # The one file the customer has to act on — NOT every candidate.
                # Falling back to the whole path list named files with no read
                # of the flag in them whenever the FIRST file was the one that
                # raised, which is the common case.
                refused=[prepass.failed_path] if prepass.failed_path else [],
                discarded=sorted(set(prepass.originals) - {prepass.failed_path}),
            ) from exc

    reader_rules = _reader_rules(
        spec, flag_key, rules_file, accessors, resolution, reader_paths, reader_const
    )

    run_groups = partial(
        _execute_groups,
        reader_rules=reader_rules,
        language=language,
        piranha_language=piranha_language,
        rules_file=rules_file,
        const_rules_file=const_rules_file,
        edges_file=edges_file,
        entries_rules_file=entries_rules_file,
        substitutions=substitutions,
        argument_flags=argument_flags,
        flag_key=flag_key,
        repo_dir=repo_dir,
    )

    unprocessable: list[str] = list(skipped)
    summaries: list = []
    # What the engine is actually handed. `paths` can be wider: the snapshot
    # also covers files that only mention a key constant, and a file that is
    # neither a proven reader nor holds the key gives every rule nothing to do.
    engine_paths = with_const + without_const + list(reader_paths)
    try:
        try:
            run_groups(
                with_const=with_const,
                without_const=without_const,
                reader_paths=reader_paths,
                collected=summaries,
            )
        except PiranhaTransformError as abort:
            # Gate 2 made this run the engine TWICE, which quietly falsified the
            # "nothing was written" contract this function and PiranhaTransformError
            # both advertise: the first group's rewrites are already on disk when the
            # second group panics, and the Gate 1 rollback below is never reached.
            # `orchestrate` happens to survive that (its snapshot undo is
            # unconditional), but a caller that believes the documented contract — the
            # M4 archive-on-merge mode, a direct CLI use — would carry one flag's
            # half-applied deletions into the next flag's pull request.
            #
            # Restoring the ENGINE's writes only, deliberately: that puts the files
            # back into the state the retry below needs (sentinel-ified, if this
            # language has a pre-pass), and the pre-pass owns its own undo either way.
            _restore(summaries)
            try:
                summaries, quarantined = _quarantine_and_retry(
                    engine_paths, language, flag_key, run_groups, accessors,
                    repo_dir=repo_dir,
                    reader_paths=reader_paths,
                    regained=regained,
                    proven=proven,
                    blocked=resolution.blocked,
                )
                # Extend, never rebind: the pre-flight skips above are already in
                # here, and `_quarantine_and_retry` only ever knows about the files
                # it was given.
                unprocessable.extend(quarantined)
            except BaseException:
                if prepass is not None:
                    prepass.restore_all()
                raise
            if len(quarantined) == len(engine_paths):
                # Nothing survived, so there is no partial removal to offer and no
                # reason to soften the failure: report it exactly as before.
                if prepass is not None:
                    prepass.restore_all()
                raise abort
            logger.warning(
                "the transform engine could not process %d file(s) while removing "
                "%r, and they were left untouched: %s. The remaining file(s) were "
                "still cleaned, and the pull request says so — this is usually a "
                "source file newer than the engine's bundled grammar",
                len(unprocessable),
                flag_key,
                ", ".join(unprocessable),
            )

        # Read BEFORE any text pass or the pre-pass rebase: both replace a
        # changed summary with a `Rewrite`, and only the engine's summary
        # carries the rule name each edit came from. Inside this statement
        # rather than after it because the Python pass below replaces
        # summaries with `Rewrite`s of its own, and the entries its rounds
        # report have to be accumulated as they are produced. The dead loops
        # and unreachable statements the engine deleted are read the same way,
        # for the same reason.
        entries, entry_paths = _flag_keyed_entries(summaries, repo_dir)
        engine_stranded = list(_engine_stranded(summaries, language, repo_dir))
        engine_inlined = _engine_inlined(summaries)
        engine_bindings = _engine_bindings(summaries, repo_dir)

        # Python and the TypeScript family. Resolve a local the fold left bound
        # to a literal and hand the result back to the engine, so the literal
        # is created inside the cascade where the edges apply. See
        # `_inline_bound_reads` (#3029) and :mod:`flag_cleanup.ts_fold`.
        inline = _BOUND_LITERAL_INLINERS.get(language)
        if inline is not None:
            summaries, entries, entry_paths = _inline_bound_reads(
                summaries,
                inline=inline,
                language=language,
                run_groups=run_groups,
                with_const=with_const,
                marker=substitutions["marker"],
                repo_dir=repo_dir,
                entries=entries,
                entry_paths=entry_paths,
                dead_loops=engine_stranded,
            )
    finally:
        # Every engine invocation — the first pass, the quarantine retry, and
        # each round of the Python marker pass — happens inside this
        # statement, and nothing after it reads a rule file. `raise abort`
        # above runs this on its way out.
        if generated_rules is not None:
            generated_rules.cleanup()

    # Swap the engine's sentinel-ified "before" for what the customer actually
    # had, and pull in any file the pre-pass marked that the engine returned no
    # summary for — that file is still sitting on disk with a sentinel in it,
    # and it has to reach the refusal path below rather than be left there.
    if prepass is not None:
        summaries = prepass.rebase(summaries)

    # Keep the test-double edits only if the engine folded a real read, then
    # give the summaries the customer's bytes as their "before". Asked BEFORE
    # that rebase: the engine's own "before" already holds the edits, so a
    # deleted stub is not mistaken for a fold.
    summaries, test_doubles = _settle_test_doubles(
        summaries, doubles, language, accessors, repo_dir, unprocessable
    )

    # PHP only, and BEFORE the whitespace tidy rather than beside the Python
    # fold below — that ordering is the point. Promoting a mid-chain `elseif`
    # leaves `} ` at end of line, the same artefact TypeScript's
    # `delete_else_if_false` produces, and the tidy is what removes it. Running
    # after would ship the space.
    if language == "php":
        summaries = _fold_php_literals(summaries)

    # The import a proven Kotlin reader bound the constant's name with names a
    # declaration this run deletes once its last read is folded. BEFORE the
    # whitespace tidy and Gate 1, like every other text pass.
    if language == "kt":
        summaries = _drop_stranded_key_imports(summaries, resolution)

    # The from-import a proven Python reader bound a key constant with, once
    # its folds leave nothing reading it. Deleting it would also stop its module
    # loading, so it is rewritten to keep the load -- see
    # :func:`cross_file_python.rewrite_stranded_imports`. Whether or not the
    # declaration goes: the post-transform check below refuses a reader still
    # importing the constant. Before the stranded-binding pass, which would
    # otherwise only mark the old statement.
    if language == "python":
        summaries = _rewrite_python_key_imports(summaries, resolution, repo_dir)

    # The import a proven Dart reader read a key constant through, once its
    # folds strand it: `dart analyze` fails on an unused import and on a
    # `show` naming a declaration this run deleted. See
    # :func:`cross_file_dart.rewrite_stranded_imports`.
    if language == "dart":
        summaries = _rewrite_dart_key_imports(summaries, resolution, repo_dir)

    # Replace each configured gate on this flag with the children it rendered.
    # Before the whitespace tidy and the stranded-binding pass: the gate's own
    # import is stranded by this edit, and that pass is what removes it.
    summaries = _unwrap_gates(summaries, gate_paths, language, flag_key, gates)

    # Tidy whitespace this transform stranded mid-line, BEFORE Gate 1 judges the
    # result and before the diff is built — the customer must be shown, and the
    # gate must validate, the exact bytes that stay on disk. Writing back is not
    # optional for the same reason: the PR path commits from the working tree.
    summaries = _tidy_trailing_whitespace(summaries, language)

    # The mid-line double space a single-line ENTRY deletion leaves (the
    # engine swallows the comma, not the space before the entry). Scoped to
    # the files the entry rules touched; see `_collapse_entry_double_spaces`.
    summaries = _collapse_entry_double_spaces(summaries, language, entry_paths)

    # `;`-separated object types: deleting the FIRST member leaves `{ ;`, which
    # parses clean — TS1131 to tsc, invisible to Gate 1. (A middle or last
    # member would leave `;;`, but the engine's own syntax check refuses that
    # edit before it reaches here.) See `strip_dangling_type_separators`.
    summaries = _strip_type_separators(summaries, language, entry_paths)

    # The stray `\r` a whole-line deletion leaves in a CRLF file. LAST of the
    # entry tidies on purpose — the `;`-separated object type only reaches the
    # `\r\r\n` shape once the pass above has cut its dangling `;`. See
    # `_collapse_carriage_returns`.
    summaries = _collapse_carriage_returns(summaries, language, entry_paths)

    # Strip each override a helper's parameter feeds, with the parameter and
    # its arguments. Before the restore below, which must judge the file
    # without them, and before the test deletion, which only runs on a file
    # this transform changed.
    summaries, parameter_entries = _strip_parameter_pins(
        summaries, parameter_pins, language, flag_key, repo_dir
    )

    # The override entries no rule can prove are about flags: `{ 'k': flag }`,
    # with `flag` a parameter or local provably bound to a boolean. After the
    # parameter pins, which prove more of the same shape (they rewrite the
    # calls too), so this sees only what they left; before the restore below,
    # so a test file still naming the key as data is put back with these
    # deletions undone too. See :mod:`flag_cleanup.ts_bound_overrides`.
    summaries, bound_entries, unread_bindings, unread_parameters, bound_constructors = _delete_bound_entries(
        summaries,
        [p for p in paths if p not in unprocessable],
        language,
        flag_key,
        repo_dir,
    )

    # Put back a test file the rewrite left still naming the key as data: its
    # overrides drive code that still reads the flag, so stripping them (and
    # deleting its tests) breaks it. Before the test deletion below, which
    # must not run on such a file, and before the stranded passes.
    summaries, kept = _restore_tests_using_key_as_data(
        summaries, language, flag_key, repo_dir
    )
    restored = {path for path, _reason in kept}
    entries = tuple(entries) + tuple(
        entry
        for entry in parameter_entries + bound_entries
        if entry[0] not in restored
    )
    unread_bindings = tuple(b for b in unread_bindings if b[0] not in restored)
    pinned_tests = {
        path: tests
        for path, tests in pinned_tests.items()
        if os.path.relpath(path, repo_dir) not in restored
    }

    # Delete the tests whose subject is the branch the fold removed. Before the
    # stranded-statement and stranded-binding passes, because a deleted test
    # can be the last user of an import, and that pass is what removes it.
    summaries, deleted_tests = _delete_pinned_tests(
        summaries, language, pinned_tests, repo_dir
    )

    # The parameters the bound-entry deletions left unread, where no call can
    # still pass one. After the test deletion, because the test about the
    # removed branch is usually the one caller that did; before the stranded
    # passes, because a dropped parameter's type can be an import's last use.
    summaries, unread_bindings, dropped_parameters = _drop_unread_parameters(
        summaries, language, unread_bindings, unread_parameters, repo_dir
    )

    # Delete what the fold stranded, for the same reason and in the same window:
    # Gate 1 must judge, and the customer must see, the bytes that stay on disk.
    # Before Gate 1 specifically — in Java these statements are the difference
    # between a branch that compiles and one that does not, which is what this
    # used to refuse over.
    summaries, stranded = _remove_stranded_statements(summaries, language, repo_dir)
    stranded = tuple(engine_stranded) + stranded

    # Delete the imports and locals the fold stranded, in the same window and
    # for the same reason: Gate 1 must judge, and the customer must see, the
    # bytes that stay on disk. In Go both shapes are compile errors, so this is
    # the difference between a branch that builds and one that does not.
    summaries, bindings = _remove_stranded_bindings(summaries, language, repo_dir)
    bindings = engine_bindings + bindings

    # Python only, and the same window again: the fallback for a file whose
    # marker the engine did not consume. The binding is inlined with the bare
    # literal, which no engine pass will fold, so a condition it exposes stays
    # standing and the residue gate refuses the file rather than shipping it
    # half-done.
    if language == "python":
        summaries = _inline_python_literals(summaries)

    # Delete each keyless wrapper nothing calls any more, and its interface
    # member. Before the stranded-method pass below, which removes the imports
    # and private helpers the deletion strands.
    summaries, wrapper_rows = wrapper_inline.close_out(
        wrappers, repo_dir, summaries, unprocessable, Rewrite, treatment=treatment
    )
    # A wrapper's test doubles were resolved on the strength of proof
    # alone. If close-out kept a wrapper proof called complete, those edits
    # describe a member that stays: refuse the flag rather than ship them.
    _refuse_wrappers_kept_at_close_out(wrappers, wrapper_rows, doubles, summaries, language, flag_key)

    # What a complete C# family left behind: the empty interface, the
    # member-less classes, their registrations and the constructor parameters
    # that still take the interface. One transaction per family; a proof that
    # fails keeps all of it and is reported, never refused. Before the
    # stranded-method pass, which removes what these deletions strand, and
    # before Gate 1, which judges every file this rewrites.
    if language == "csharp":
        summaries, residue_rows = csharp_residue.remove(
            wrappers,
            wrapper_rows,
            summaries,
            repo_dir,
            Rewrite,
            engine=lambda paths, rules: _execute_ctor_param_rules(paths, language, rules, repo_dir),
            flag_key=flag_key,
        )
        wrapper_rows = wrapper_rows + residue_rows
    # The passes below parse and rewrite what is on disk: a file this run
    # deleted is not handed to them (one that wrote back the empty text it
    # was given would re-create the file), and rejoins the rest for Gate 1
    # and the diff. See :mod:`flag_cleanup.file_deletion`.
    summaries, deleted_files = file_deletion.split(summaries)

    # Private methods the fold stranded, still before Gate 1. A second call
    # into the same pass rather than a new one: deleting a method can strand an
    # import, and that pass owns imports.
    summaries, method_bindings = _remove_stranded_bindings(
        summaries, language, repo_dir, include_methods=True
    )
    bindings = bindings + method_bindings

    # The labels the fold and the passes above left with nothing to jump to
    # them, which the customer's lint rejects (and Go's compiler).
    if language in _TS_GRAMMARS or language == "go":
        summaries = _remove_orphans(summaries, language)

    # C# primary-constructor parameters the fold left unread: CS9113, an error
    # under TreatWarningsAsErrors — see `primary_constructor`. After the method
    # pass, because a deleted private helper can be what last read the
    # parameter. The TypeScript family's counterpart: a `private` or `#` member
    # the fold left unread is TS6133/TS6138 under `noUnusedLocals` — see
    # `ts_unread_members`. After the method pass for the same reason.
    unread_constructor = _unread_constructor_rows(summaries, language, repo_dir)
    # Remove the constructor parameters among them whose every caller can be
    # proven, with each caller's argument; the rest stay reported. Still before
    # Gate 1, which must judge the callers this rewrites too.
    summaries, unread_constructor, removed_constructor = (
        _remove_unread_constructor_parameters(summaries, language, unread_constructor, repo_dir)
    )
    # A `const` left unread is deleted by the stranded-binding pass above like
    # any other local; only what is still on disk is reported as unread.
    on_disk = {os.path.relpath(s.path, repo_dir): s.content for s in summaries}
    unread_bindings = tuple(
        b for b in unread_bindings if b[2] in on_disk.get(b[0], b[2])
    )
    # A constructor parameter whose last read was a deleted override is named
    # once: as an unread binding, with its cause, while it stays; not at all
    # once the pass above removed it. Matched by class and parameter, since
    # the binding's text alone can belong to another function in the file.
    removed = {(path, label) for path, kind, label in removed_constructor if kind == "removed parameter"}
    gone = {(path, text) for path, text, owner in bound_constructors if (path, owner) in removed}
    unread_bindings = tuple(b for b in unread_bindings if (b[0], b[2]) not in gone)
    bound = {(path, owner) for path, _, owner in bound_constructors}
    unread_constructor = tuple(
        row
        for row in unread_constructor
        if row[1] != "unread constructor parameter" or (row[0], row[2]) not in bound
    )
    bindings = bindings + unread_constructor + removed_constructor
    bindings = bindings + dropped_parameters + unread_bindings

    # The override literals the entry deletions left empty (`flags={{}}`).
    # Reported, never edited: whether the prop carrying one can go depends on
    # a type declared elsewhere. Last, so it reads the bytes that stay on disk.
    bindings = bindings + _emptied_overrides(summaries, language, flag_key, repo_dir)

    # GATE 1 — if the transform broke any file, discard ALL of it. A partial
    # rewrite is not an option: the flag would look half-removed, and a diff
    # that removes the flag from three files while a fourth still reads it is
    # not reviewable — the reviewer cannot see the file that was left out.
    #
    # The suppression that buys is real (one unrewritable file costs the whole
    # group) and is therefore REPORTED rather than absorbed: the raise below
    # names both sets, the orchestrator abandons the flag with its own action,
    # and the run exits non-zero. Loud-and-nothing beats quiet-and-partial;
    # quiet-and-nothing — what this used to do — is the one unacceptable
    # combination.
    # A file the test-double pre-pass edited is judged from that pass's output:
    # its deletions are deliberate and self-checked (see
    # `mock_stubs.resolve`), and the differential checks below would read a
    # deleted test as a fold that lost something.
    summaries = summaries + deleted_files
    changed = [s for s in summaries if s.original_content != s.content]
    reasons: dict[str, str] = {}
    for s in changed:
        # A deleted file has no text left to judge: the pass that deleted it
        # proved every use of what it held was removed with it, and its own
        # postcondition re-read every file it touched.
        if file_deletion.deleted(s):
            continue
        reason = refusal_reason(
            _gate_baseline(doubles, s.path, s.original_content),
            s.content,
            _grammar_of(s.path, language),
        )
        # A surviving sentinel means the engine did not consume a read the
        # pre-pass marked. It parses, so Gate 1's checks are blind to it, and
        # it would ship an identifier that does not compile and that no
        # customer can interpret. Refuse — never diff.
        # The engine inlines a Go local bound to the read without asking which
        # declaration each same-named identifier belongs to. Asked of the
        # customer's source, where the names are still there to resolve.
        if reason is None and _grammar_of(s.path, language) == "go":
            reason = go_inline.refusal_reason(
                s.original_content, engine_inlined.get(s.path, ())
            )
        # A local the fold stranded that neither the engine nor the stranded-
        # binding pass could remove: Go rejects it as `declared and not used`.
        # Asked of the bytes that stay on disk, against the customer's source,
        # so whatever edit stranded it (the fold, a wrapper, a test double).
        if reason is None and _grammar_of(s.path, language) == "go":
            reason = go_stranded.refusal_reason(s.original_content, s.content)
        if reason is None and prepass is not None and prepass.survived(s.path, s.content):
            reason = "the engine left an internal marker in place of a flag read"
        # The same hazard from the other end of the run: a marker
        # `_inline_bound_reads` wrote and the engine did not consume. That pass
        # already unwinds such a file to its previous content, so this is the
        # belt to its braces — and it is the one outcome where a quiet
        # pass-through would ship a NameError.
        if (
            reason is None
            and language in _BOUND_LITERAL_INLINERS
            and substitutions["marker"] in s.content
        ):
            reason = "the engine left an internal marker in place of a flag read"
        if reason is not None:
            reasons[s.path] = reason
    # A proven cross-file reader must come out naming none of the constants it
    # was proven against: anything left would point at a declaration this run
    # may delete, and that breaks the build in another file, where no parse of
    # the reader alone can see it.
    for path in _proven_readers(resolution, paths + sibling_readers):
        if path in unprocessable or path in reasons:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        if cross_file_keys.still_mentions(
            path, text, language, resolution, accessors, repo_dir
        ):
            reasons[path] = (
                "a read of a key constant declared in another file was not rewritten"
            )
    refused = sorted(reasons)
    if refused:
        _restore(changed)
        discarded = sorted({s.path for s in changed} - set(refused))
        named = "; ".join(f"{path} ({reasons[path]})" for path in refused)
        logger.warning(
            "refusing the rewrite of %s for flag %r: the result did not survive "
            "the post-transform checks. %d other file(s) rewrote cleanly "
            "and were rolled back with it (%s), so this flag produces no diff at "
            "all until the refused file(s) are changed by hand or the key is "
            "added to the `ignore` input",
            named,
            flag_key,
            len(discarded),
            ", ".join(discarded) or "none",
        )
        raise UnsafeRewriteError(
            f"the rewrite of {named} for flag {flag_key!r} did not survive the "
            "post-transform checks, so it was rolled back along with "
            f"{len(discarded)} file(s) that rewrote cleanly",
            refused=refused,
            discarded=discarded,
            reasons=reasons,
        )

    return TransformOutcome(
        _summaries_to_diff(summaries, repo_dir),
        tuple(unprocessable),
        stranded,
        bindings,
        entries,
        declined,
        test_doubles,
        deleted_tests,
        kept,
        tuple(
            (os.path.relpath(r.path, repo_dir), r.line, r.reason, r.text)
            for r in resolution.refusals
            if not r.entry
        ),
        tuple(
            (os.path.relpath(r.path, repo_dir), r.line, r.reason, r.text)
            for r in cross_file_keys.enum_findings(resolution)
        ),
        tuple(
            (os.path.relpath(f.path, repo_dir), f.line, f.reason, f.text, f.wrapper)
            for f in wrapper_rows
        ),
    )


def _refuse_wrappers_kept_at_close_out(
    plan: wrapper_inline.WrapperPlan,
    rows: list,
    doubles: list[mock_stubs.Prepass] | None,
    summaries: list,
    language: str,
    flag_key: str,
) -> None:
    """Roll the whole flag back when a wrapper proof called complete did
    not end complete, and its test-double pass removed anything.

    The pass deleted stubs, sibling tests, pins and knob writes because the
    wrapper was going. Close-out can still keep it (its read was not folded,
    a mention is left, a sibling's member cannot be deleted), and then each
    of those edits is about a member that stays: a deleted test of surviving
    code, a loose mock now answering ``false`` to a live read. Nothing is
    re-resolved here, since the engine has edited the same test files since.
    Refused the way Gate 1 refuses, with every file put back.
    """
    if language not in _STUBS_GATED_PER_WRAPPER or not plan.wrappers:
        return
    if not any(p.wrapper is not None and p.removals for p in doubles or ()):
        return
    complete = {f.wrapper for f in rows if f.reason == wrapper_inline.COMPLETE}
    kept = [w for w in plan.wrappers if w.label in plan.complete and w.label not in complete]
    if not kept:
        return
    changed = [s for s in summaries if s.original_content != s.content]
    _restore(changed)
    for stub_pass in reversed(doubles or ()):
        stub_pass.restore_all()
    spelled = {str(Path(s.path).resolve()): s.path for s in summaries}
    reasons = {
        spelled.get(w.path, w.path): (
            f"the keyless wrapper {w.label} was kept after its test doubles were "
            "resolved for its removal"
        )
        for w in kept
    }
    refused = sorted(reasons)
    discarded = sorted({s.path for s in changed} - set(refused))
    labels = ", ".join(w.label for w in kept)
    logger.warning(
        "refusing every edit for flag %r: the keyless wrapper(s) %s were proven complete, "
        "but close-out kept them, and the test-double edits made for their removal "
        "would describe code that stays. %d file(s) were rolled back",
        flag_key,
        labels,
        len(changed),
    )
    raise UnsafeRewriteError(
        f"the keyless wrapper(s) {labels} for flag {flag_key!r} were kept after their "
        "test doubles were resolved for removal, so the rewrite was rolled back along "
        f"with {len(discarded)} other file(s)",
        refused=refused,
        discarded=discarded,
        reasons=reasons,
    )


#: The languages whose wrapper test-double edits are gated per wrapper: a
#: stub, test or pin goes only with a wrapper that ends complete. Each one's
#: stubs are resolved BEFORE proof, so a wrapper that stays has them put back.
_STUBS_GATED_PER_WRAPPER = frozenset({"csharp", "java", "kt"})


def _plan_wrappers(
    repo_dir: str,
    language: str,
    flag_key: str,
    treatment: bool,
    accessors: tuple[str, ...],
    paths: list[str],
    resolution: cross_file_keys.Resolution,
    with_const: list[str],
    without_const: list[str],
    reader_paths: list[str],
    reader_const: frozenset[str],
    doubles: list[mock_stubs.Prepass] | None,
) -> wrapper_inline.WrapperPlan:
    """Discover, qualify and prove this flag's keyless wrappers.

    Spans and receivers come back keyed by the spelling ``paths`` uses, which
    is what the pre-pass and the engine are handed.

    A wrapper is looked for only in a file whose reads this run folds, judged
    the way the fold will judge them: same-file constants only where Gate 2
    let the const rules run (for Kotlin, where it let the pre-pass resolve
    them), and constants of another file only by the texts the cross-file
    resolution proved there.
    """
    def external(path: str) -> frozenset[str]:
        return frozenset(
            ".".join(part for part in (ref.owner, ref.member, ref.accessor) if part)
            for ref in resolution.readers.get(str(Path(path).resolve()), ())
        )

    const_ok = set(with_const) | (set() if language == "kt" else set(reader_const))
    folded = {
        p: (p in const_ok, external(p))
        for p in [*with_const, *without_const, *reader_paths]
    }
    found = wrapper_inline.discover_all(language, flag_key, accessors, folded)
    qualified, refusals = wrapper_inline.qualify_all(repo_dir, found, treatment)
    by_resolved = {str(Path(p).resolve()): p for p in paths}
    stub_pass: mock_stubs.Prepass | None = None
    def fakes(kept: frozenset[str]) -> dict:
        """C#: the knobs and fake owners the pass may resolve: those of the
        wrappers that complete, and of no fake that also serves one that stays
        (its knob may be what a test is about)."""
        staying = {
            (s.namespace, s.owner) for w, _ in qualified if w.name in kept for s in w.siblings
        }
        going = [
            s
            for w, _ in qualified
            if w.name not in kept
            for s in w.siblings
            if s.test and s.knob and (s.namespace, s.owner) not in staying
        ]
        staying_knobs = {s.knob for w, _ in qualified if w.name in kept for s in w.siblings if s.knob}
        staying_owners = {owner for _, owner in staying}
        return {
            "knobs": frozenset(s.knob for s in going if not s.knob_primary) - staying_knobs,
            "sibling_owners": frozenset(
                (s.owner, s.knob, s.knob_primary) for s in going if s.owner not in staying_owners
            ),
            "owner_defaults": frozenset(
                (s.owner, (s.knob_default or "false") == "true")
                for s in going
                if s.owner not in staying_owners and not s.knob_primary
            ),
        }

    if doubles is not None:
        qualified, refusals = _resolve_wrapper_doubles(
            repo_dir, language, flag_key, treatment, accessors, found, qualified, refusals, by_resolved, doubles
        )
    if qualified and doubles is not None:
        receivers = wrapper_inline.stub_receivers(language, qualified)
        sibling_reads = wrapper_inline.sibling_receivers(language, qualified)
        knob_reads, built, test_code = wrapper_inline.fake_settings(language, qualified)

        def spell(found: dict) -> dict:
            return {by_resolved[r]: names for r, names in found.items() if r in by_resolved}

        spelled, sibling_spelled = spell(receivers), spell(sibling_reads)
        knob_spelled, built_spelled = spell(knob_reads), spell(built)
        if spelled or sibling_spelled or knob_spelled or built_spelled:
            stub_pass = mock_stubs.Prepass(
                language, flag_key, treatment, accessors,
                wrapper=mock_stubs.WrapperStub(tuple(sorted({w.name for w, _ in qualified})), **fakes(frozenset())),
                receivers=spelled,
                siblings=sibling_spelled,
                knob_receivers=knob_spelled,
                owners=built_spelled,
                test_code=frozenset(by_resolved[r] for r in test_code if r in by_resolved),
            )
            doubles.append(stub_pass)
            stub_pass.apply(sorted({*spelled, *sibling_spelled, *knob_spelled, *built_spelled}))
    def prove():
        return wrapper_inline.prove_all(language, qualified, refusals, lambda path: path in by_resolved)

    plan = prove()
    # A stub, a verification and a stub's test -- and for C# a sibling test,
    # a helper's pin, a fake's knob write and a test decided by a fake's
    # construction -- go only with a wrapper that completes. One that stays
    # keeps its reads (and C#'s, its sibling member), and what describes
    # them: a call the proof refused still runs against the mock in a test,
    # and without its stub a Mockito or MockK mock answers its default.
    # Keeping a test or a stub can leave another wrapper with a read in a
    # test, so this runs until no more wrappers stay.
    kept: frozenset[str] = frozenset()
    while stub_pass is not None and language in _STUBS_GATED_PER_WRAPPER and stub_pass.removals:
        staying = frozenset(w.name for w, _ in qualified if w.label not in plan.complete)
        if staying <= kept:
            break
        kept |= staying
        stub_pass.reapply(replace(stub_pass.wrapper, kept=kept, **fakes(kept)))
        plan = prove()
    plan.spans = {by_resolved[r]: spans for r, spans in plan.spans.items()}
    plan.receivers = {by_resolved[r]: names for r, names in plan.receivers.items() if r in by_resolved}
    return plan


def _resolve_wrapper_doubles(
    repo_dir: str,
    language: str,
    flag_key: str,
    treatment: bool,
    accessors: tuple[str, ...],
    found: list[wrapper_inline.Wrapper],
    qualified: list,
    refusals: list[wrapper_inline.Finding],
    by_resolved: dict[str, str],
    doubles: list[mock_stubs.Prepass],
) -> tuple[list, list[wrapper_inline.Finding]]:
    """Resolve the stubs of every wrapper refused only as ``test-double``, in a
    profile that can say where they are (TypeScript and JavaScript).

    All of them are resolved in one round, so a test stubbing two wrappers of
    the flag to opposite values is seen as stubbing both values. Afterwards
    each is indexed and judged again, and the call sites of those that
    qualify are proven. If every one now qualifies and every call it has
    folds, the round stands. If any does not, every edit of the round is
    withdrawn and the round runs again without those that failed, until it
    stands or none is left. So a refused wrapper's stubs are never edited,
    and no caller is folded while a test can still force the removed branch.

    A wrapper judged again and refused for another reason is reported with
    that reason: its doubles hid it, since that check runs first. A wrapper
    that qualifies but would stay partial (a call the proof refuses, such as
    ``f(u.trim())``) stays ``test-double``: that call is not folded and
    still reaches the double, so removing the double would turn the test
    red. So does a wrapper the proof finds no call of at all: a call it
    cannot see (a name spelled only in data) can still reach the double.

    Each pass joins ``doubles`` before it writes, so a raise anywhere in a
    round is rolled back with the rest of the transform, and leaves it again
    once withdrawn.
    """
    def is_double_of(f: wrapper_inline.Finding, wrapper: wrapper_inline.Wrapper) -> bool:
        return (
            f.reason == wrapper_inline.TEST_DOUBLE and f.path == wrapper.path
            and f.line == wrapper.line and f.wrapper == wrapper.label
        )

    def refused_as_double(wrapper: wrapper_inline.Wrapper) -> bool:
        return any(is_double_of(f, wrapper) for f in refusals)

    def forget(gone: list[mock_stubs.Prepass]) -> None:
        doubles[:] = [d for d in doubles if not any(d is g for g in gone)]

    pending = [
        w for w in found
        if refused_as_double(w)
        and (lang := wrapper_inline.profile(w.language)) is not None
        and lang.stub_targets is not None
    ]
    while pending:
        targets: dict[str, dict[str, mock_stubs.StubTargets]] = {}
        # A class mock whose only stub of the wrapper the pass deletes would
        # still read as a double afterwards, so it is recorded first.
        settled: dict[str, object] = {}
        for wrapper in pending:
            lang = wrapper_inline.profile(wrapper.language)
            before = lang.index(repo_dir, wrapper)
            for path, where in lang.stub_targets(wrapper, before).items():
                if path in by_resolved:
                    targets.setdefault(by_resolved[path], {})[wrapper.name] = where
            if lang.settled_mocks is not None:
                settled[wrapper.label] = lang.settled_mocks(wrapper, before, treatment)
        by_language: dict[str, list[str]] = {}
        for path in sorted(targets):
            grammar = wrapper_inline.language_for(path)
            if grammar is not None and mock_stubs.covers(grammar):
                by_language.setdefault(grammar, []).append(path)
        names = tuple(sorted({w.name for w in pending}))
        passes: list[mock_stubs.Prepass] = []
        for grammar, paths in sorted(by_language.items()):
            stub_pass = mock_stubs.Prepass(
                grammar, flag_key, treatment, accessors,
                wrapper=mock_stubs.WrapperStub(names),
                targets={p: targets[p] for p in paths},
            )
            doubles.append(stub_pass)
            passes.append(stub_pass)
            stub_pass.apply(paths)
        judged = []
        failed = False
        for wrapper in pending:
            lang = wrapper_inline.profile(wrapper.language)
            if wrapper.label in settled:
                index = lang.index(repo_dir, wrapper, settled[wrapper.label])
            else:
                index = lang.index(repo_dir, wrapper)
            member, reason = lang.qualify(wrapper, index)
            if member is None:
                failed = True
                if reason not in (None, wrapper_inline.TEST_DOUBLE):
                    refusals = [
                        wrapper_inline.Finding(wrapper.path, wrapper.line, reason, wrapper.label, wrapper.label)
                        if is_double_of(f, wrapper) else f
                        for f in refusals
                    ]
            else:
                judged.append((wrapper, member, index))
        if not failed:
            proof = wrapper_inline.prove_all(
                language, [(member, index) for _, member, index in judged], [], lambda path: path in by_resolved
            )
            partial = {member.label for _, member, _ in judged} - proof.complete
            if partial:
                failed = True
                judged = [j for j in judged if j[1].label not in partial]
        if not failed:
            forget([p for p in passes if not p.originals])
            done = {w.label for w, _, _ in judged}
            qualified = [*qualified, *((member, index) for _, member, index in judged)]
            refusals = [
                f for f in refusals
                if not (f.reason == wrapper_inline.TEST_DOUBLE and f.wrapper in done)
            ]
            break
        for stub_pass in reversed(passes):
            stub_pass.withdraw(list(stub_pass.originals))
        forget(passes)
        pending = [w for w, _, _ in judged]
    return qualified, refusals


def _check_gates(
    paths: list[str],
    language: str,
    flag_key: str,
    gates: tuple[ts_gates.Gate, ...],
    treatment: bool,
) -> list[str]:
    """The files holding a gate on ``flag_key`` to unwrap; raise if one cannot be.

    A test file is skipped, gate and all: see :mod:`flag_cleanup.ts_gates`.
    An unreadable file yields nothing, as in :func:`_find_pinned_tests`.
    """
    if not gates or not ts_gates.covers(language):
        return []
    found: list[str] = []
    reasons: dict[str, str] = {}
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not ts_gates.find_gates(source, language, flag_key, gates):
            continue
        if ts_tests.is_test_file(source, language):
            continue
        reason = ts_gates.refusal_reason(source, language, flag_key, gates, treatment)
        if reason is not None:
            reasons[path] = reason
        else:
            found.append(path)
    if reasons:
        refused = sorted(reasons)
        named = "; ".join(f"{path} ({reasons[path]})" for path in refused)
        logger.warning("refusing to transform for flag %r: %s", flag_key, named)
        raise UnsafeRewriteError(
            f"a gate component on flag {flag_key!r} cannot be removed: {named}",
            refused=refused,
            discarded=[],
            reasons=reasons,
        )
    return found


def _unwrap_gates(
    summaries,
    gate_paths: list[str],
    language: str,
    flag_key: str,
    gates: tuple[ts_gates.Gate, ...],
) -> list:
    """Unwrap the gates in ``gate_paths``, adding a summary for any file the
    engine did not touch — a file whose only mention of the flag is a gate."""
    if not gate_paths:
        return summaries
    by_path = {summary.path: summary for summary in summaries}
    result = list(summaries)
    for path in gate_paths:
        summary = by_path.get(path)
        before = (
            summary.content
            if summary is not None
            else Path(path).read_text(encoding="utf-8")
        )
        after, count = ts_gates.unwrap_gates(before, language, flag_key, gates)
        if not count:
            continue
        Path(path).write_text(after, encoding="utf-8")
        rewrite = Rewrite(path, summary.original_content if summary else before, after)
        if summary is None:
            result.append(rewrite)
        else:
            result[result.index(summary)] = rewrite
    return result


def _delete_bound_entries(
    summaries, paths: list[str], language: str, flag_key: str, repo_dir: str
) -> tuple[
    list,
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str, str], ...],
    dict[str, frozenset[tuple[str, str]]],
    frozenset[tuple[str, str, str]],
]:
    """Delete each override entry valued at a name bound to a boolean.

    Returns the summaries, the deleted entries as ``(path, text)`` for
    ``TransformOutcome.entries``, ``(path, "unread binding", text)`` for
    each binding those deletions left unread, per path the
    ``(function name, parameter)`` identity of each unread whole parameter,
    and ``(path, text, "Class(parameter)")`` for each one that is a
    constructor's. Asked of every candidate file,
    adding a summary for one the engine did not touch, as
    :func:`_unwrap_gates` does. TypeScript family only.
    """
    if not ts_tests.covers(language):
        return summaries, (), (), {}, frozenset()
    by_path = {summary.path: summary for summary in summaries}
    result = list(summaries)
    entries: list[tuple[str, str]] = []
    unread: list[tuple[str, str, str]] = []
    parameters: dict[str, frozenset[tuple[str, str]]] = {}
    constructors: set[tuple[str, str, str]] = set()
    for path in paths:
        summary = by_path.get(path)
        try:
            before = (
                summary.content
                if summary is not None
                else Path(path).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            continue
        outcome = ts_bound_overrides.delete_bound_entries(
            before, _grammar_of(path, language), flag_key
        )
        if not outcome.entries:
            continue
        Path(path).write_text(outcome.content, encoding="utf-8")
        rewrite = Rewrite(
            path, summary.original_content if summary else before, outcome.content
        )
        if summary is None:
            result.append(rewrite)
        else:
            result[result.index(summary)] = rewrite
        relative = os.path.relpath(path, repo_dir)
        entries.extend((relative, text) for text in outcome.entries)
        unread.extend((relative, "unread binding", text) for text in outcome.unread)
        if outcome.parameters:
            parameters[relative] = frozenset(outcome.parameters)
        constructors.update((relative, text, owner) for text, owner in outcome.constructor_parameters)
    return result, tuple(entries), tuple(unread), parameters, frozenset(constructors)


def _drop_unread_parameters(
    summaries,
    language: str,
    unread: tuple[tuple[str, str, str], ...],
    parameters: dict[str, frozenset[tuple[str, str]]],
    repo_dir: str,
) -> tuple[list, tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]]:
    """Remove each unread parameter no call can still pass; see
    :func:`~flag_cleanup.ts_bound_overrides.drop_unread_parameters`.

    Returns the summaries, the ``unread`` rows still unread, and an
    ``(path, "unused parameter", text)`` row for each parameter removed.
    """
    if not parameters:
        return summaries, unread, ()
    result = []
    dropped: list[tuple[str, str, str]] = []
    for summary in summaries:
        relative = os.path.relpath(summary.path, repo_dir)
        if relative not in parameters:
            result.append(summary)
            continue
        content, removed = ts_bound_overrides.drop_unread_parameters(
            summary.content,
            _grammar_of(summary.path, language),
            parameters[relative],
        )
        if not removed:
            result.append(summary)
            continue
        Path(summary.path).write_text(content, encoding="utf-8")
        result.append(Rewrite(summary.path, summary.original_content, content))
        dropped.extend((relative, "unused parameter", text) for text in removed)
    # One unread row per dropped parameter: two helpers can share a text, and
    # dropping one says nothing about the other.
    gone = [(path, text) for path, _kind, text in dropped]
    remaining = []
    for row in unread:
        if (row[0], row[2]) in gone:
            gone.remove((row[0], row[2]))
        else:
            remaining.append(row)
    return result, tuple(remaining), tuple(dropped)


def _emptied_overrides(
    summaries, language: str, flag_key: str, repo_dir: str
) -> tuple[tuple[str, str, str], ...]:
    """``(path, "emptied override", line)`` for each object literal this
    removal emptied; TypeScript family only. See
    :func:`~flag_cleanup.ts_bound_overrides.emptied_overrides`."""
    if not ts_tests.covers(language):
        return ()
    return tuple(
        (os.path.relpath(summary.path, repo_dir), "emptied override", line)
        for summary in summaries
        if summary.original_content != summary.content
        for line in ts_bound_overrides.emptied_overrides(
            summary.original_content,
            summary.content,
            _grammar_of(summary.path, language),
            flag_key,
        )
    )


def _restore_tests_using_key_as_data(
    summaries, language: str, flag_key: str, repo_dir: str
) -> tuple[list, tuple[tuple[str, str], ...]]:
    """Write back, whole, each rewritten test file still naming the key as data."""
    if not ts_tests.covers(language):
        return summaries, ()
    result = []
    kept: list[tuple[str, str]] = []
    for summary in summaries:
        grammar = _grammar_of(summary.path, language)
        if summary.original_content == summary.content or not ts_tests.is_test_file(
            summary.content, grammar
        ):
            result.append(summary)
            continue
        line = ts_tests.data_mention(summary.content, grammar, flag_key)
        if line is None:
            result.append(summary)
            continue
        Path(summary.path).write_text(summary.original_content, encoding="utf-8")
        result.append(Rewrite(summary.path, summary.original_content, summary.original_content))
        kept.append(
            (
                os.path.relpath(summary.path, repo_dir),
                f"this test still hands the key to the code under test ({line}), "
                "so its overrides still drive a read of the flag; it was left "
                "exactly as it was",
            )
        )
    return result, tuple(kept)


def _find_pinned_tests(
    paths: list[str], language: str, flag_key: str, untreated: str
) -> dict[str, list[ts_tests.PinnedTest]]:
    """The tests to delete per path; empty outside the TypeScript family.

    An unreadable file yields nothing rather than raising, as in
    :func:`~flag_cleanup.contradicting.find_contradicting_entries`: what is
    lost is a deletion, and the pin it would have explained is still stripped.
    """
    if not ts_tests.covers(language):
        return {}
    found: dict[str, list[ts_tests.PinnedTest]] = {}
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tests = ts_tests.find_pinned_tests(source, language, flag_key, untreated)
        if tests:
            found[path] = tests
    return found


def _find_parameter_pins(
    paths: list[str], language: str, flag_key: str
) -> dict[str, list[ts_param_pins.ParameterPin]]:
    """The parameter pins per path; empty outside the TypeScript family.

    An unreadable file yields nothing, as in :func:`_find_pinned_tests`: its
    override stays, and the residual scan reports it as before.
    """
    if not ts_tests.covers(language):
        return {}
    found: dict[str, list[ts_param_pins.ParameterPin]] = {}
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        pins = ts_param_pins.find_parameter_pins(
            source, _grammar_of(path, language), flag_key
        )
        if pins:
            found[path] = pins
    return found


def _strip_parameter_pins(
    summaries,
    pins: dict[str, list[ts_param_pins.ParameterPin]],
    language: str,
    flag_key: str,
    repo_dir: str,
) -> tuple[list, tuple[tuple[str, str], ...]]:
    """Strip each path's parameter pins, adding a summary for a file the
    engine did not touch — the usual case, since no rule matches the shape."""
    if not pins:
        return summaries, ()
    by_path = {summary.path: summary for summary in summaries}
    result = list(summaries)
    entries: list[tuple[str, str]] = []
    for path, path_pins in pins.items():
        summary = by_path.get(path)
        before = (
            summary.content
            if summary is not None
            else Path(path).read_text(encoding="utf-8")
        )
        after, stripped = ts_param_pins.strip_parameter_pins(
            before, _grammar_of(path, language), flag_key, path_pins
        )
        if after == before:
            continue
        Path(path).write_text(after, encoding="utf-8")
        rewrite = Rewrite(path, summary.original_content if summary else before, after)
        if summary is None:
            result.append(rewrite)
        else:
            result[result.index(summary)] = rewrite
        relative = os.path.relpath(path, repo_dir)
        entries.extend((relative, entry) for entry in stripped)
    return result, tuple(entries)


def _delete_pinned_tests(
    summaries, language: str, pinned: dict[str, list], repo_dir: str
) -> tuple[list, tuple[tuple[str, str], ...]]:
    """Delete the chosen tests from each changed file; report each by title."""
    if not pinned:
        return summaries, ()
    result = []
    deleted: list[tuple[str, str]] = []
    for summary in summaries:
        tests = pinned.get(summary.path)
        if not tests or summary.original_content == summary.content:
            result.append(summary)
            continue
        content, titles = ts_tests.delete_tests(summary.content, language, tests)
        if not titles:
            result.append(summary)
            continue
        Path(summary.path).write_text(content, encoding="utf-8")
        result.append(Rewrite(summary.path, summary.original_content, content))
        relative = os.path.relpath(summary.path, repo_dir)
        deleted.extend((relative, title) for title in titles)
    return result, tuple(deleted)


def _external_keys(
    language: str, resolution: cross_file_keys.Resolution, proven: list[str]
) -> dict[str, frozenset[str]]:
    """Per proven Kotlin reader, the argument texts that mean this flag's key.

    Empty for every other language: their readers are folded by generated
    reader rules, not by a pre-pass.
    """
    if language != "kt":
        return {}
    return {
        path: frozenset(
            ".".join(p for p in (ref.owner, ref.member, ref.accessor) if p)
            for ref in resolution.readers[str(Path(path).resolve())]
        )
        for path in proven
    }


def _drop_stranded_key_imports(summaries, resolution: cross_file_keys.Resolution) -> list:
    """Remove from each rewritten Kotlin reader the key imports it no longer uses."""
    if not resolution.readers:
        return summaries
    result = []
    for summary in summaries:
        if (
            summary.original_content == summary.content
            or str(Path(summary.path).resolve()) not in resolution.readers
        ):
            result.append(summary)
            continue
        cleaned = cross_file_kotlin.drop_stranded_imports(
            summary.content, resolution.declarations
        )
        if cleaned == summary.content:
            result.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        result.append(Rewrite(summary.path, summary.original_content, cleaned))
    return result


def _rewrite_python_key_imports(
    summaries, resolution: cross_file_keys.Resolution, repo_dir: str
) -> list:
    """Rewrite each Python reader's import of a key constant its folds stranded."""
    if not resolution.readers:
        return summaries
    result = []
    for summary in summaries:
        resolved = str(Path(summary.path).resolve())
        if summary.original_content == summary.content or resolved not in resolution.readers:
            result.append(summary)
            continue
        visible = tuple(d for d in resolution.declarations if d.path != resolved)
        cleaned = cross_file_python.rewrite_stranded_imports(
            resolved, summary.content, visible, repo_dir
        )
        if cleaned == summary.content:
            result.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        result.append(Rewrite(summary.path, summary.original_content, cleaned))
    return result


def _rewrite_dart_key_imports(
    summaries, resolution: cross_file_keys.Resolution, repo_dir: str
) -> list:
    """Trim or delete each Dart reader's import of a key constant its folds stranded."""
    if not resolution.readers:
        return summaries
    result = []
    for summary in summaries:
        resolved = str(Path(summary.path).resolve())
        if summary.original_content == summary.content or resolved not in resolution.readers:
            result.append(summary)
            continue
        visible = tuple(d for d in resolution.declarations if d.path != resolved)
        cleaned = cross_file_dart.rewrite_stranded_imports(
            resolved, summary.content, visible, resolution.readers[resolved], repo_dir
        )
        if cleaned == summary.content:
            result.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        result.append(Rewrite(summary.path, summary.original_content, cleaned))
    return result


def _proven_readers(
    resolution: cross_file_keys.Resolution, paths: list[str]
) -> list[str]:
    """The resolution's proven readers, spelled as they are in ``paths``.

    A reader missing from ``paths`` (outside what the caller let the engine
    touch) is left out; :func:`_resolve_cross_file` keeps every declaration
    when that happens.
    """
    by_resolved = {str(Path(p).resolve()): p for p in paths}
    return [by_resolved[r] for r in sorted(resolution.readers) if r in by_resolved]


def _resolve_cross_file(
    repo_dir: str,
    language: str,
    flag_key: str,
    accessors: tuple[str, ...],
    paths: list[str],
    with_const: list[str],
    without_const: list[str],
) -> tuple[
    cross_file_keys.Resolution,
    list[str],
    frozenset[str],
    list[str],
    list[str],
    list[str],
]:
    """Resolve key constants read from other files, and regroup the engine's files.

    Returns ``(resolution, reader_paths, reader_const, regained, with_const,
    without_const)``:

    * ``reader_paths`` -- the proven readers, for the reader invocation. Only
      when this language ships ``<base>_xconst.toml``; otherwise a reader stays
      in the Gate 2 group it was already in.
    * ``regained`` -- declaring files Gate 2 withheld only because another file
      reads their constant, which the resolution has now proven deletable. They
      move to the const group, and only if every proven reader is in
      ``reader_paths`` (one the reader invocation does not rewrite would be
      left pointing at nothing) and the file still passes the in-file Gate 2
      check. Kotlin has no reader invocation: its sentinel pre-pass folds a
      proven read wherever the reader sits, so there every proven reader has
      to be among the files handed to the transform instead, and each stays
      in its Gate 2 group. A declaring file that is also
      a reader is never regained, so the reader invocation never needs the
      const rules for a file whose declaration it would delete.
    * ``reader_const`` -- the readers Gate 2 allowed the const rules for (a
      reader that also holds the key may declare constants of its own).

    Every file that neither holds the key nor is a proven reader leaves both
    groups: it is in ``paths`` for the undo snapshot alone.
    """
    key_paths = _key_paths(paths, flag_key)
    resolution = cross_file_keys.resolve(repo_dir, language, flag_key, key_paths, accessors)
    proven = _proven_readers(resolution, paths)
    template = RULES_DIR / f"{_language_spec(language).rule_base}_xconst.toml"
    reader_paths = proven if template.exists() else []

    # The readers this run rewrites. Kotlin's are covered by the pre-pass's
    # external keys, which `_transform_flag` builds from exactly `proven`.
    covered = proven if language == "kt" else reader_paths
    # Every other proven reader -- one in another file type of the family, or
    # outside the files handed to the transform -- keeps its read, so it keeps
    # the declaration, and is reported as the reason.
    resolution = cross_file_keys.report_uncovered(
        resolution, covered, _language_spec(language).extensions
    )
    # A profile whose readers fold only with a deletable declaration drops
    # every reader an uncovered one keeps (see
    # `Profile.readers_need_deletable`); for every other profile this keeps
    # the lists as they are.
    still = set(resolution.readers)
    proven = [p for p in proven if str(Path(p).resolve()) in still]
    reader_paths = [p for p in reader_paths if str(Path(p).resolve()) in still]
    covered = [p for p in covered if str(Path(p).resolve()) in still]
    regained: list[str] = []
    if len(covered) == len(resolution.readers):
        deletable = resolution.deletable
        proven_set = set(proven)
        for path in without_const:
            if str(Path(path).resolve()) not in deletable or path in proven_set:
                continue
            source = Path(path).read_text(encoding="utf-8")
            if const_path_is_safe(source, language, flag_key, accessors):
                regained.append(path)
    elif resolution.readers:
        logger.info(
            "%d proven reader(s) of a key constant are outside the files this "
            "run rewrites, so every declaration is kept",
            len(resolution.readers) - len(covered),
        )

    # A declaring file some refusal keeps loses the const rules, whatever the
    # in-file gate decided: that gate asks only whether another file spells
    # the constant's name AND its owner, and a subclass reading it bare, say,
    # spells only the name. The refusal is what the pull request reports as
    # the reason the declaration stayed, so it has to stay.
    withheld = [p for p in with_const if _is_blocked(resolution, p)]
    with_const = [p for p in with_const if p not in withheld]
    without_const = without_const + withheld
    # A declaring file no other file names is the in-file gate's alone: its
    # own findings are the reason it stays only where that gate kept it too.
    if resolution.home_findings:
        folding = {str(Path(p).resolve()) for p in with_const}
        resolution = replace(
            resolution,
            refusals=resolution.refusals
            + tuple(r for r in resolution.home_findings if r.path not in folding),
        )

    keyed = set(key_paths)
    readers = set(reader_paths)
    reader_const = frozenset(p for p in with_const if p in readers)
    stays = keyed | set(proven)
    with_const = [p for p in with_const + regained if p in stays and p not in readers]
    without_const = [
        p for p in without_const if p not in regained and p in stays and p not in readers
    ]
    return resolution, reader_paths, reader_const, regained, with_const, without_const


def _is_blocked(resolution: cross_file_keys.Resolution, path: str) -> bool:
    """Whether a refusal keeps a declaration in ``path`` (see ``Resolution.blocked``)."""
    return str(Path(path).resolve()) in resolution.blocked


def _key_paths(paths: list[str], flag_key: str) -> list[str]:
    return [
        p
        for p in paths
        if flag_key in Path(p).read_text(encoding="utf-8", errors="replace")
    ]


@dataclass(frozen=True)
class _ReaderGroup:
    """The readers that share one set of proven references, and their rules.

    A clone matches its reference by TEXT alone -- the bare ``K`` of
    ``import { NAME as K }``, the ``ns`` of ``import * as ns`` -- and that text
    means the constant only in the file it was proven in. Another reader can
    bind the same text to something else entirely (``const K = "other"``), so
    each reader is run under the clones of its own references and no others.

    A reader whose text lacks the key needs the base seeds' key hole removed,
    or the engine skips the file (``keyless``). One whose text holds the key
    too needs them as shipped, or its own direct reads are left standing
    (``keyed``).
    """

    paths: frozenset[str]
    keyless: str
    keyed: str


@dataclass(frozen=True)
class _ReaderRules:
    """The rule texts for the reader invocations, and which reader gets which.

    A keyed reader also gets the const rules when Gate 2 allowed them.
    """

    groups: tuple[_ReaderGroup, ...]
    keyed_paths: frozenset[str]
    const_paths: frozenset[str]


def _reader_rules(
    spec: _Language,
    flag_key: str,
    rules_file: Path,
    accessors: tuple[str, ...],
    resolution: cross_file_keys.Resolution,
    reader_paths: list[str],
    reader_const: frozenset[str],
) -> _ReaderRules | None:
    if not reader_paths:
        return None
    template = (RULES_DIR / f"{spec.rule_base}_xconst.toml").read_text(encoding="utf-8")
    base = rules_file.read_text(encoding="utf-8")
    keyed = frozenset(_key_paths(reader_paths, flag_key))
    # The wrapper pass's sentinel, when a reader also calls a keyless wrapper
    # this run inlines (see `rule_synthesis.cross_file_key_rules`'s `marked`).
    sentinel = kotlin_sentinel.sentinel_for(flag_key)
    # Grouped by the exact set of references each reader was proven to make,
    # so readers proven alike still share one invocation, and by whether the
    # reader carries a wrapper mark.
    by_refs: dict[tuple[frozenset, bool], list[str]] = {}
    for path in reader_paths:
        refs = resolution.readers[str(Path(path).resolve())]
        marked = sentinel in Path(path).read_text(encoding="utf-8", errors="replace")
        by_refs.setdefault(
            (frozenset((r.owner, r.member, r.accessor) for r in refs), marked), []
        ).append(path)
    groups: list[_ReaderGroup] = []
    for (_refs, marked), paths in by_refs.items():
        refs = [
            ref
            for path in paths
            for ref in resolution.readers[str(Path(path).resolve())]
        ]
        groups.append(
            _ReaderGroup(
                paths=frozenset(paths),
                keyless=rule_synthesis.cross_file_key_rules(
                    base, template, refs, accessors=accessors, marked=marked
                ),
                keyed=rule_synthesis.cross_file_key_rules(
                    base, template, refs, accessors=accessors, strip_key_hole=False,
                    marked=marked,
                ),
            )
        )
    return _ReaderRules(
        groups=tuple(groups),
        keyed_paths=keyed,
        const_paths=reader_const & keyed,
    )


def _execute_groups(
    *,
    with_const: list[str],
    without_const: list[str],
    const_rules_file: Path,
    collected: list,
    reader_paths: Iterable[str] = (),
    reader_rules: _ReaderRules | None = None,
    **kwargs,
) -> list:
    """Run the engine over both Gate 2 groups, collecting every summary.

    Two invocations rather than one because the const-propagation rules are
    withheld from the files Gate 2 distrusts; an empty group is skipped.

    ``collected`` is extended IN PLACE and must be owned by the caller. That is
    not a style choice: the first group's rewrites are already on disk when the
    second group panics, so the caller needs those summaries to roll them back.
    Returning them instead loses them on exactly the path that needs them —
    which is the regression `test_an_engine_panic_in_the_second_group_rolls_back
    _the_first` exists to catch, and did.
    """
    for group, use_const_rules in ((with_const, True), (without_const, False)):
        if not group:
            continue
        _execute_each_grammar(
            collected,
            paths=group,
            const_rules_file=const_rules_file if use_const_rules else None,
            **kwargs,
        )
    reader_paths = list(reader_paths)
    if reader_paths and reader_rules is not None:
        _execute_readers(reader_paths, reader_rules, const_rules_file, collected, kwargs)
    return collected


def _execute_each_grammar(
    collected: list, *, paths: list[str], language: str, **kwargs
) -> None:
    """:func:`_execute` once per grammar among ``paths``, extending ``collected``.

    Every path is of ``language`` except a cross-file reader of a sibling file
    type, which the engine must be handed under its own grammar: the
    ``typescript`` language silently skips a ``.tsx`` file, and the JSX rules
    are a compile error under it. ``collected`` is extended after each
    invocation, for the reason :func:`_execute_groups` gives.
    """
    by_grammar: dict[str, list[str]] = {}
    for path in paths:
        by_grammar.setdefault(_grammar_of(path, language), []).append(path)
    for grammar, members in by_grammar.items():
        overrides: dict[str, object] = {}
        if grammar != language:
            overrides = {"piranha_language": _LANGUAGES[grammar].piranha_id}
        collected.extend(_execute(paths=members, **{**kwargs, **overrides}))


def _execute_readers(
    reader_paths: list[str],
    rules: _ReaderRules,
    const_rules_file: Path,
    collected: list,
    kwargs: dict,
) -> None:
    """The third invocation: proven cross-file readers, under the reader rules.

    Run once per :class:`_ReaderGroup`, so no reader is handed another
    reader's clones. A reader without the key gets no entry rules either: every
    one of them is a seed holding the key hole, and one such seed is enough for
    the engine to skip a file that lacks the key.
    """
    with tempfile.TemporaryDirectory(prefix="flag-cleanup-xconst-") as tmp:
        for index, group in enumerate(rules.groups):
            members = [p for p in reader_paths if p in group.paths]
            if not members:
                continue
            keyless = [p for p in members if p not in rules.keyed_paths]
            keyed_const = [
                p for p in members if p in rules.keyed_paths and p in rules.const_paths
            ]
            keyed_plain = [
                p
                for p in members
                if p in rules.keyed_paths and p not in rules.const_paths
            ]
            keyless_file = Path(tmp) / f"xconst_rules_{index}.toml"
            keyless_file.write_text(group.keyless, encoding="utf-8")
            keyed_file = Path(tmp) / f"xconst_keyed_rules_{index}.toml"
            keyed_file.write_text(group.keyed, encoding="utf-8")
            invocations = (
                (keyless, keyless_file, None, {**kwargs, "entries_rules_file": None}),
                (keyed_const, keyed_file, const_rules_file, kwargs),
                (keyed_plain, keyed_file, None, kwargs),
            )
            for paths, rules_file, const_file, group_kwargs in invocations:
                if not paths:
                    continue
                _execute_each_grammar(
                    collected,
                    paths=paths,
                    const_rules_file=const_file,
                    **{**group_kwargs, "rules_file": rules_file},
                )


def _execute_ctor_param_rules(
    paths: list[str], language: str, rules_text: str, repo_dir: str
) -> list:
    """Run the rendered constructor-parameter rules over ``paths``, once per grammar.

    A run of its own, after the fold: only the declaring files and the proven
    callers, with no const, JSX, entry or edge rules beside it. Returns the
    engine's summaries of the files it changed. See
    :mod:`flag_cleanup.ctor_param_removal`.
    """
    collected: list = []
    with tempfile.TemporaryDirectory(prefix="flag-cleanup-ctor-") as tmp:
        rules_file = Path(tmp) / "ctor_rules.toml"
        rules_file.write_text(rules_text, encoding="utf-8")
        by_grammar: dict[str, list[str]] = {}
        for path in paths:
            by_grammar.setdefault(_grammar_of(path, language), []).append(path)
        for grammar, members in sorted(by_grammar.items()):
            spec = _LANGUAGES[grammar]
            collected.extend(
                _execute(
                    paths=members,
                    piranha_language=spec.piranha_id,
                    rules_file=rules_file,
                    const_rules_file=None,
                    edges_file=Path(tmp) / "no_edges.toml",
                    substitutions={},
                    argument_flags=_load_argument_flags(spec.rule_base),
                    flag_key="(constructor parameters)",
                    repo_dir=repo_dir,
                )
            )
    return collected


def _quarantine_and_retry(
    paths: list[str],
    language: str,
    flag_key: str,
    run_groups,
    accessors: tuple[str, ...] = (),
    *,
    repo_dir: str | None = None,
    reader_paths: Iterable[str] = (),
    regained: Iterable[str] = (),
    proven: Iterable[str] = (),
    blocked: frozenset[str] = frozenset(),
) -> tuple[list, list[str]]:
    """Find the file(s) the engine cannot process, then transform the rest.

    An abort is all-or-nothing per INVOCATION, so one unprocessable file used to
    abandon the whole flag — permanently, on every future run. Measured against a
    real repository: a single file using `satisfies`, a `const` type parameter,
    `accessor` and `using` (all valid, all TypeScript 4.9-5.2) made every flag in
    that repository unproposable, because the engine's bundled grammar is older
    than the source. Neither escape hatch helped — `ignore` silences a *flag*,
    not a *file*, and `directories` only works if the file sits in a separable
    subtree. That is most current TypeScript repositories.

    This re-runs one file at a time to find which of them the engine chokes on.
    The survivors are then transformed together, and the offenders are RETURNED
    rather than swallowed: they hold a live read this tool did not remove, so
    they belong in the same "still references this flag, not processed" caveat
    that an unsupported extension earns, in the log and in the pull-request body.

    Deliberately reached only on the abort path. The batched run is the fast one,
    and paying N invocations per flag on every run to pre-empt a rare failure
    would be the wrong trade.

    Note what is NOT softened: Gate 1's :class:`UnsafeRewriteError` still
    abandons the whole flag. The two failures mean different things — an abort is
    "the engine cannot represent this file", which is a non-rewrite much like an
    unsupported extension, while a Gate 1 refusal is "our rules produced
    something bad for a file we did understand", and that one must never become a
    partial removal.

    Nor is every abort the first kind. Only the engine's refusal to read a file
    (:data:`_UNREADABLE_INPUT`) is; any other abort — its post-rewrite syntax
    self-check, its reserved-word check — is the second kind raised from inside
    the engine: it read the file, rewrote it, and rejected what it wrote. A
    probe that ends that way stops the whole flag, exactly as a Gate 1 refusal
    does.
    """
    readers = frozenset(reader_paths)
    regained_set = frozenset(regained)

    def unblocked(group: list[str]) -> tuple[list[str], list[str]]:
        """``group`` split into (not kept by a refusal, kept by one)."""
        kept = [p for p in group if str(Path(p).resolve()) in blocked]
        return [p for p in group if p not in kept], kept

    allowed, withheld = _partition_by_const_safety(
        [p for p in paths if p not in readers], language, flag_key, accessors,
        repo_dir=repo_dir,
    )
    allowed, kept = unblocked(allowed)
    withheld = withheld + kept
    # The cross-file regrouping is part of the decision the real run made: a
    # proven reader goes to the reader invocation, and a regained declaring
    # file to the const group.
    const_ok = frozenset(allowed) | (regained_set & frozenset(withheld))
    survivors: list[str] = []
    unprocessable: list[str] = []
    for path in paths:
        # Probed under the SAME Gate 2 decision the real run would make — which
        # is why `accessors` has to reach this far. Forcing the const rules on
        # here would abort for a file they were withheld from and quarantine it
        # for a reason the real run never had; computing the partition without
        # the accessors would do the mirror image, withholding from the probe a
        # file the real run cleans.
        one: dict[str, list[str]] = {"with_const": [path], "without_const": []}
        if path in readers:
            one = {"with_const": [], "without_const": [], "reader_paths": [path]}
        elif path not in const_ok:
            one = {"with_const": [], "without_const": [path]}
        probe: list = []
        try:
            run_groups(collected=probe, **one)
        except PiranhaTransformError as abort:
            if _UNREADABLE_INPUT not in str(abort):
                # The engine READ this file — it refuses one it cannot, with
                # that message, before any rule runs — and then rejected its
                # own rewrite of it. That is a defect in the rewrite, not in
                # the source, so it gets the Gate 1 treatment: the whole flag
                # stops. Quarantining it would offer the other files as a
                # partial removal (or, with nothing else to rewrite, a clean
                # "no changes") and blame a grammar that read the file fine.
                # Chained to the engine's own panic, as `_execute` chains it.
                raise PiranhaTransformError(
                    f"the transform engine rewrote {path} while removing "
                    f"{flag_key!r} and then refused its own result. The file "
                    "parses; the rewrite of it is wrong, so nothing was "
                    f"written for this flag. Engine message: {abort}"
                ) from (abort.__cause__ or abort)
            unprocessable.append(path)
        else:
            survivors.append(path)
        finally:
            # Always, including after an abort: the probe is a question, not a
            # transform, and the real run below re-does the work for whatever
            # survived. Leaving a probe's bytes on disk would double-apply the
            # rewrite and put it outside the summaries Gate 1 later judges.
            _restore(probe)
    if not survivors:
        return [], unprocessable
    with_const, without_const = _partition_by_const_safety(
        [p for p in survivors if p not in readers], language, flag_key, accessors,
        repo_dir=repo_dir,
    )
    with_const, kept = unblocked(with_const)
    without_const = without_const + kept
    # A regained declaration may go only while every proven reader is still
    # being rewritten. One the engine could not process keeps reading it.
    # `proven` adds the readers a pre-pass folds in their own Gate 2 group
    # (Kotlin), which `reader_paths` never lists.
    if not (readers | frozenset(proven)) & frozenset(unprocessable):
        back = [p for p in without_const if p in regained_set]
        with_const = with_const + back
        without_const = [p for p in without_const if p not in back]
    groups: dict[str, list[str]] = {
        "with_const": with_const,
        "without_const": without_const,
    }
    if readers:
        groups["reader_paths"] = [p for p in survivors if p in readers]
    summaries: list = []
    try:
        run_groups(collected=summaries, **groups)
    except PiranhaTransformError:
        # A file that passed its own probe and then aborted in company. Nothing
        # sound is left to offer, so restore and let the caller report the abort.
        _restore(summaries)
        raise
    return summaries, unprocessable


def _write_generated_rules(
    destination: Path,
    rules_file: Path,
    const_rules_file: Path,
    accessors: tuple[str, ...],
) -> tuple[Path, Path]:
    """Write accessor-extended copies of the rule files and return their paths.

    Copies rather than edits: the shipped rules are what the tests read and
    what a reader reviews, and a run that rewrote them in place would leave a
    customer's checkout of this image differing from the image.

    A missing const file is normal — only some languages have one — and is
    passed straight back so the caller's ``.exists()`` check still decides.

    A ``<base>_accessors.toml`` sibling, if present, supplies the blocks to
    CLONE in place of that file's own seed rules. Only Python has them, and
    only because its SDK read is generic over the flag's type: its seed anchors
    a boolean literal as the sole type evidence, which is right for
    ``variation`` and wrong for a wrapper that exists to hide that argument.
    Such a file is never staged into the engine's config directory, so it is
    inert unless something asks for it here. The lookup is by STEM, so
    ``python_const.toml`` finds ``python_const_accessors.toml`` and the const
    rules get the same treatment for the same reason.

    **The const file gets both treatments, and they are different questions.**
    ``with_accessor_guards`` extends a re-binding guard (which only a READ-const
    file has); ``with_accessor_const_rules`` clones the seed rules (which every
    KEY-const file has). Running only the first is #2730: a wrapper named in
    ``accessors`` could not reach a key hoisted to a constant, silently.
    Guards first, then clones, so a clone inherits an already-extended guard
    rather than needing its own pass -- no shipped file has both today, and this
    is the order that stays right when one does.
    """
    generated = destination / rules_file.name
    generated.write_text(
        rule_synthesis.with_accessors(
            rules_file.read_text(encoding="utf-8"),
            accessors,
            templates_text=_accessor_templates_for(rules_file),
        ),
        encoding="utf-8",
    )
    if not const_rules_file.exists():
        return generated, const_rules_file
    generated_const = destination / const_rules_file.name
    generated_const.write_text(
        rule_synthesis.with_accessor_const_rules(
            rule_synthesis.with_accessor_guards(
                const_rules_file.read_text(encoding="utf-8"), accessors
            ),
            accessors,
            templates_text=_accessor_templates_for(const_rules_file),
        ),
        encoding="utf-8",
    )
    return generated, generated_const


def _accessor_templates_for(rules_file: Path) -> str | None:
    """The ``<stem>_accessors.toml`` beside ``rules_file``, or ``None``."""
    templates = rules_file.with_name(f"{rules_file.stem}_accessors.toml")
    return templates.read_text(encoding="utf-8") if templates.exists() else None


#: The languages whose pre-pass exists only to mark keyless-wrapper calls.
_SPAN_ONLY_PREPASS = frozenset({"java", "csharp", "go", "ts", "tsx", "js", "python", "dart"})


def _sentinel_prepass(
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] = (),
    const_key_paths: frozenset[str] = frozenset(),
    external_keys: dict[str, frozenset[str]] | None = None,
    wrapper_spans: dict[str, tuple[kotlin_sentinel.Span, ...]] | None = None,
) -> kotlin_sentinel.SentinelPrePass | None:
    """The pre-pass this language needs, or ``None`` for the languages that don't.

    Kotlin always has one, and it is a workaround for a specific engine
    limitation rather than a general stage — see
    :mod:`flag_cleanup.kotlin_sentinel`. Java, C#, Go, Dart and the TS family have one only when
    there are keyless-wrapper calls to mark (``wrapper_spans``, see
    :mod:`flag_cleanup.wrapper_inline`): it marks those and nothing else, and
    the sentinel rules in ``rules/java.toml``, ``rules/csharp.toml``,
    ``rules/go.toml``, ``rules/dart.toml`` and ``rules/ts.toml`` fold them. Keeping the decision in one function means the places `run_piranha`
    has to care about it all ask the same question.
    """
    spans = dict(wrapper_spans or {})
    if language in _SPAN_ONLY_PREPASS and spans:
        return kotlin_sentinel.SentinelPrePass(
            flag_key=flag_key,
            sentinel=kotlin_sentinel.sentinel_for(flag_key),
            extra_spans=spans,
            marks_reads=False,
        )
    if language != "kt":
        return None
    return kotlin_sentinel.SentinelPrePass(
        flag_key=flag_key,
        sentinel=kotlin_sentinel.sentinel_for(flag_key),
        accessors=accessors,
        const_key_paths=const_key_paths,
        external_keys=dict(external_keys or {}),
        extra_spans=spans,
    )


def _tidy_trailing_whitespace(summaries, language: str) -> list:
    """Re-emit every changed summary with stranded trailing whitespace removed.

    A no-op for a language or a file that produced none, in which case the
    original summary object is passed through untouched rather than copied.
    """
    tidied = []
    for summary in summaries:
        if summary.original_content == summary.content:
            tidied.append(summary)
            continue
        cleaned = strip_introduced_trailing_whitespace(
            summary.original_content, summary.content, _grammar_of(summary.path, language)
        )
        if cleaned == summary.content:
            tidied.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        tidied.append(Rewrite(summary.path, summary.original_content, cleaned))
    return tidied


def _collapse_entry_double_spaces(summaries, language: str, entry_paths: set[str]) -> list:
    """Collapse the mid-line double space an entry deletion leaves.

    Only files the entry rules edited: every other deletion this tool makes
    is a statement or a branch, which never produces the shape, and a pass
    that looked at every rewritten file would be scanning for an artefact it
    cannot have made.
    """
    tidied = []
    for summary in summaries:
        if summary.original_content == summary.content or summary.path not in entry_paths:
            tidied.append(summary)
            continue
        cleaned = collapse_introduced_double_spaces(
            summary.original_content, summary.content, _grammar_of(summary.path, language)
        )
        if cleaned == summary.content:
            tidied.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        tidied.append(Rewrite(summary.path, summary.original_content, cleaned))
    return tidied


#: A carriage return that does not begin a CRLF: the one byte a file can hold
#: inside a token that the stray-`\r` tidy could mistake for its residue.
_LONE_CARRIAGE_RETURN = re.compile(r"\r(?!\n)")


def _collapse_carriage_returns(summaries, language: str, entry_paths: set[str]) -> list:
    """Drop the stray carriage return a whole-line deletion leaves in a CRLF file.

    The files the entry rules touched, every rewritten Python file, and every
    other rewritten file whose input held no LONE ``\r``. Any fold of an
    ``if`` leaves it, in every language, through the read rules alone: the
    kept branch's last line takes the ``\r`` of the line the fold removed
    (``return newPath(u);\r\r\n``). The guard that keeps a run inside a token
    asks for the grammar's LEAVES, and Dart's multi-line strings and PHP's
    heredocs hold bytes no leaf covers, so a ``\r`` inside one would be taken
    for whitespace. In a file where every ``\r`` began a ``\r\n`` that cannot
    matter: no token held a ``\r`` of its own, so a doubled one is a pair the
    transform broke. A file that did hold one is tidied only where the
    residue was measured, as before.

    A SEPARATE pass placed LAST, after the `;` strip — which is
    what makes it cover two residues rather than one. The engine leaves
    `},\r\r\n` directly (a whole-line deletion takes the line's `\n` and
    strands its `\r` on the line above), but the `;`-separated object type
    reaches the shape only once `strip_dangling_type_separators` has cut the
    dangling `;`: the engine hands over `{\r;\r\n`, which no `\r\r\n` rule
    can see. Running before that pass would fix the first and miss the second.

    Since engine 0.33.0 the engine keeps a file that was CRLF throughout CRLF
    through its own deletions and folds, so on such a file the first residue
    no longer arrives. The pass stays as defence in depth, and for the second.
    """
    tidied = []
    for summary in summaries:
        if summary.original_content == summary.content or not (
            summary.path in entry_paths
            or _grammar_of(summary.path, language) == "python"
            or _LONE_CARRIAGE_RETURN.search(summary.original_content) is None
        ):
            tidied.append(summary)
            continue
        cleaned = collapse_introduced_carriage_returns(
            summary.original_content, summary.content, _grammar_of(summary.path, language)
        )
        if cleaned == summary.content:
            tidied.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        tidied.append(Rewrite(summary.path, summary.original_content, cleaned))
    return tidied


def _strip_type_separators(summaries, language: str, entry_paths: set[str]) -> list:
    """TypeScript family only: drop the `;` a type-member deletion leaves dangling."""
    if language not in ("ts", "tsx", "js"):
        return summaries
    tidied = []
    for summary in summaries:
        if summary.original_content == summary.content or summary.path not in entry_paths:
            tidied.append(summary)
            continue
        cleaned = strip_dangling_type_separators(
            summary.content, _grammar_of(summary.path, language)
        )
        if cleaned == summary.content:
            tidied.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        tidied.append(Rewrite(summary.path, summary.original_content, cleaned))
    return tidied


def _fold_php_literals(summaries) -> list:
    """Finish the literal conditions the engine's PHP cascade cannot reach.

    One pass rather than the Python path's two: PHP has no binding to inline —
    the built-ins already do that — so the only thing left is the literal
    condition. See :mod:`flag_cleanup.php_fold` for which two shapes those are
    and why neither is expressible as a rule. The engine folds the
    ``switch (true)`` multi-way guard itself.
    """
    folded = []
    for summary in summaries:
        if summary.original_content == summary.content:
            folded.append(summary)
            continue
        # Before Gate 1, like every text pass.
        result = fold_php_literal_conditions(summary.original_content, summary.content)
        if result == summary.content:
            folded.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        folded.append(Rewrite(summary.path, summary.original_content, result))
    return folded


#: Rounds of (inline → re-run the engine) before the Python marker pass gives
#: up. A runaway guard only: every round deletes at least one binding and a
#: file holds finitely many, so it converges on its own. Chaining needs more
#: than one — `b = a or other` becomes a literal binding only once `a` has been
#: inlined and the engine has folded what that produced — but a second round
#: firing at all is already rare, and a file needing five is not a file this
#: tool should keep paying engine invocations for.
_MAX_MARKER_ROUNDS = 5


def _inline_python_bindings(before: str, after: str, marker: str, *, language: str) -> str:
    del language  # one grammar; the signature matches the TypeScript inliner's
    return inline_literal_bindings(before, after, marker)


def _inline_ts_bindings(before: str, after: str, marker: str, *, language: str) -> str:
    return ts_fold.inline_literal_bindings(before, after, language, marker)


#: The languages whose post-engine inliner hands its result back to the engine
#: through a marker rule. Each needs that rule in its base rules file; a
#: language listed here without one would have every marker survive and every
#: such file unwound, which is safe and useless.
_BOUND_LITERAL_INLINERS = {
    "python": _inline_python_bindings,
    "ts": _inline_ts_bindings,
    "tsx": _inline_ts_bindings,
    "js": _inline_ts_bindings,
}


def _inline_bound_reads(
    summaries,
    *,
    inline,
    language: str,
    run_groups,
    with_const: list[str],
    marker: str,
    repo_dir: str,
    entries: tuple[tuple[str, str], ...],
    entry_paths: set[str],
    dead_loops: list[tuple[str, str]] | None = None,
):
    """Resolve reads the fold left bound to a local, through the engine (#3029).

    `python_fold.inline_literal_bindings` is the only way a read bound to a
    name is resolved at all — no rule of ours can do it, because the useful
    edit deletes the binding AND rewrites its references, two edits in
    different places — and it runs after the engine because it needs the whole
    file in hand. That placement is what broke: **Piranha's cascade is driven
    by edges from the rule that created a literal**, so a literal written by
    Python code once the engine has exited reaches no edge and stops where it
    lands. `use_legacy or other` became `True or other` while the identical
    read written INLINE folded to `True`.

    So the inliner substitutes a call to a marker instead of the literal, and
    this hands the result back to the engine, where `python.toml`'s marker rule
    creates the literal inside the cascade. A bound read then folds exactly
    like a direct one — which is the property to assert, and the reason there
    is no Python re-implementation of `boolean_expression_simplify` here: a
    second copy of the engine's cascade is what kept the TypeScript parenthesis
    defect alive in the engine for a full release, because the downstream copy
    was the one running.

    The TypeScript family runs the same loop with
    :func:`flag_cleanup.ts_fold.inline_literal_bindings`, for a different gap:
    its const rules inline a binding of the READ, but not one of an expression
    around it that only the fold turns into a literal (``const show = on ||
    x``). ``inline`` is whichever of the two the language uses.

    Five things are load-bearing:

    * **It runs inside the rule-file window**, before the engine's first pass
      is torn down, and BEFORE every text pass. Later would mean re-rendering
      the accessor clones, and it would also mean the whitespace tidy, the
      stranded-statement pass and the re-indent all judging content the engine
      was about to rewrite again.
    * **The per-round path list is narrowed to the files the inline changed.**
      The cost of this pass is one engine invocation per round, and a
      repository whose Python never binds a read pays nothing: nothing is
      staged, so nothing runs.
    * **A round that aborts puts its own files back and stops.** The engine's
      panic is per invocation, so the previous round's output is still the
      best answer available; leaving the marker text on disk instead would
      ship an identifier that does not exist.
    * **A marker that survives its round costs that FILE, not the run.** It
      means the engine did not consume something this pass wrote, which is the
      one outcome that must never reach a diff — and unwinding to the
      pre-inline content leaves the binding standing, which the residue gate
      then refuses loudly rather than quietly.
    * **Entries are accumulated per round rather than read once at the end.**
      `_flag_keyed_entries` needs the engine's summaries, and everything this
      returns for a changed file is a plain `Rewrite`, which carries no rule
      names. In practice a later round finds none — the first pass has already
      taken the registry lines — but reading them here costs nothing and does
      not depend on that staying true. ``dead_loops``, when given, is extended
      the same way with :func:`_engine_stranded`, and there a later round
      matters: ``while use:`` over a bound read only becomes ``while False:``
      once this pass has inlined it.
    """
    const_group = set(with_const)
    originals = {s.path: s.original_content for s in summaries}
    current = {s.path: s for s in summaries}
    # A file whose marker the engine did not consume is unwound and then NOT
    # offered again. Without this it is staged, re-run and unwound once per
    # remaining round — the inline is deterministic, so every round reproduces
    # the round that just failed — which turns one bad file into
    # `_MAX_MARKER_ROUNDS` engine invocations that cannot succeed.
    declined: set[str] = set()

    for _ in range(_MAX_MARKER_ROUNDS):
        staged: dict[str, tuple[str, str]] = {}
        for path, summary in current.items():
            if summary.original_content == summary.content or path in declined:
                continue
            inlined = inline(
                summary.original_content,
                summary.content,
                marker,
                language=_grammar_of(path, language),
            )
            if inlined != summary.content:
                staged[path] = (summary.content, inlined)
        if not staged:
            break

        for path, (_, inlined) in staged.items():
            Path(path).write_text(inlined, encoding="utf-8")

        produced: list = []
        try:
            run_groups(
                with_const=[p for p in staged if p in const_group],
                without_const=[p for p in staged if p not in const_group],
                collected=produced,
            )
        except PiranhaTransformError:
            logger.warning(
                "the transform engine aborted while resolving %d "
                "read(s) bound to a local; those file(s) keep the result of "
                "the previous pass: %s",
                len(staged),
                ", ".join(sorted(staged)),
            )
            for path, (previous, _) in staged.items():
                Path(path).write_text(previous, encoding="utf-8")
            break

        round_entries, round_paths = _flag_keyed_entries(produced, repo_dir)
        entries += round_entries
        entry_paths |= round_paths

        results = {s.path: s for s in produced}
        for path, (previous, inlined) in staged.items():
            summary = results.get(path)
            content = summary.content if summary is not None else inlined
            if marker in content:
                # Never a diff. The engine left behind something this pass
                # wrote, so the file would ship a call to an identifier that
                # does not exist — and unlike a bad fold it parses, so Gate 1
                # could not see it from the syntax alone.
                logger.warning(
                    "a bound-read marker survived the engine in %s; that file "
                    "keeps the result of the previous pass",
                    path,
                )
                Path(path).write_text(previous, encoding="utf-8")
                declined.add(path)
                continue
            current[path] = Rewrite(path, originals[path], content)

        # After the unwind above, not with the entries: a loop the engine
        # deleted in a file this round put back is still on disk.
        if dead_loops is not None:
            dead_loops.extend(
                _engine_stranded(
                    [s for s in produced if s.path not in declined], language, repo_dir
                )
            )

    return list(current.values()), entries, entry_paths


def _inline_python_literals(summaries) -> list:
    """Inline, with the bare literal, a read still bound to a name, and write it back."""
    folded = []
    for summary in summaries:
        if summary.original_content == summary.content:
            folded.append(summary)
            continue
        result = inline_literal_bindings(summary.original_content, summary.content)
        if result == summary.content:
            folded.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        folded.append(Rewrite(summary.path, summary.original_content, result))
    return folded


def _remove_stranded_statements(
    summaries, language: str, repo_dir: str
) -> tuple[list, tuple[tuple[str, str], ...]]:
    """Delete unreachable statements and dead loops from each changed file; report what went.

    Paths are reported relative to ``repo_dir`` because they end up in a pull
    request body, where an absolute container path means nothing.
    """
    cleaned_summaries = []
    stranded: list[tuple[str, str]] = []
    for summary in summaries:
        if summary.original_content == summary.content:
            cleaned_summaries.append(summary)
            continue
        grammar = _grammar_of(summary.path, language)
        cleaned, removed = remove_unreachable_statements(
            summary.original_content, summary.content, grammar
        )
        # The engine deletes the loops its fold made dead and reports them by
        # rule name (`_engine_stranded`). What it declines and this side can
        # still finish goes in the same list: Swift's loop under a comment...
        cleaned, dead = remove_dead_loops(summary.original_content, cleaned, grammar)
        removed = removed + dead
        # ...and the TypeScript family's `for` it declined: its initialiser
        # runs. See :mod:`flag_cleanup.ts_dead_code`.
        if grammar in _TS_GRAMMARS:
            cleaned, dead = ts_dead_code.remove_dead_loops(
                summary.original_content, cleaned, grammar
            )
            removed = removed + dead
        if not removed:
            cleaned_summaries.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        cleaned_summaries.append(
            Rewrite(summary.path, summary.original_content, cleaned)
        )
        relative = os.path.relpath(summary.path, repo_dir)
        stranded.extend((relative, statement) for statement in removed)
    return cleaned_summaries, tuple(stranded)


#: The grammars :mod:`flag_cleanup.ts_dead_code` reads.
_TS_GRAMMARS = frozenset({"ts", "tsx", "js"})


def _remove_orphans(summaries, language: str) -> list:
    """:func:`flag_cleanup.ts_dead_code.remove_orphans` (or Go's
    :func:`flag_cleanup.go_dead_code.remove_orphans`) over each changed file."""
    cleaned = []
    for summary in summaries:
        grammar = _grammar_of(summary.path, language)
        if summary.original_content == summary.content or grammar not in _TS_GRAMMARS | {"go"}:
            cleaned.append(summary)
            continue
        if grammar == "go":
            result = go_dead_code.remove_orphans(summary.original_content, summary.content)
        else:
            result = ts_dead_code.remove_orphans(summary.original_content, summary.content, grammar)
        if result == summary.content:
            cleaned.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        cleaned.append(Rewrite(summary.path, summary.original_content, result))
    return cleaned


def _remove_stranded_bindings(
    summaries, language: str, repo_dir: str, *, include_methods: bool = False
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """Delete the imports and locals each fold stranded; report what went.

    Paths are relative to ``repo_dir`` because they end up in a pull request
    body, where an absolute container path means nothing.
    """
    cleaned_summaries = []
    bindings: list[tuple[str, str, str]] = []
    for summary in summaries:
        if summary.original_content == summary.content:
            cleaned_summaries.append(summary)
            continue
        grammar = _grammar_of(summary.path, language)
        cleaned, acted = remove_stranded_bindings(
            summary.original_content,
            summary.content,
            grammar,
            include_methods=include_methods,
            path=summary.path,
            root=repo_dir,
        )
        if not acted:
            cleaned_summaries.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        cleaned_summaries.append(
            Rewrite(summary.path, summary.original_content, cleaned)
        )
        relative = os.path.relpath(summary.path, repo_dir)
        kept = "marked import" if grammar == "python" else "bare import"
        bindings.extend(
            (relative, "module marker" if c.replacement == MODULE_MARKER else kept, c.replacement)
            if c.replacement is not None
            else (relative, c.kind, c.text)
            for c in acted
        )
    return cleaned_summaries, tuple(bindings)


def _unread_primary_parameters(
    summaries, language: str, repo_dir: str
) -> tuple[tuple[str, str, str], ...]:
    """``(path, "unread parameter", label)`` for each C# primary-constructor
    parameter the fold left unread. Nothing is written."""
    found: list[tuple[str, str, str]] = []
    for summary in summaries:
        if summary.original_content == summary.content:
            continue
        if _grammar_of(summary.path, language) != "csharp":
            continue
        relative = os.path.relpath(summary.path, repo_dir)
        found.extend(
            (relative, "unread parameter", label)
            for label in unread_parameters(summary.original_content, summary.content)
        )
    return tuple(found)


def _unread_private_members(
    summaries, language: str, repo_dir: str
) -> tuple[tuple[str, str, str], ...]:
    """``(path, "unread member", label)`` for each TypeScript or JavaScript
    private member, and each Dart private field, the fold left unread.
    Nothing is written."""
    found: list[tuple[str, str, str]] = []
    for summary in summaries:
        if summary.original_content == summary.content:
            continue
        grammar = _grammar_of(summary.path, language)
        if grammar == "dart":
            labels = dart_field_removal.unread_fields(summary.original_content, summary.content)
        elif grammar in ("ts", "tsx", "js"):
            labels = unread_members(summary.original_content, summary.content, grammar)
        else:
            continue
        relative = os.path.relpath(summary.path, repo_dir)
        found.extend((relative, "unread member", label) for label in labels)
    return tuple(found)


def _unread_plain_constructor_parameters(
    summaries, language: str, repo_dir: str
) -> tuple[tuple[str, str, str], ...]:
    """``(path, "unread constructor parameter", label)`` for each plain
    TypeScript or JavaScript constructor parameter the run left unread.
    Nothing is written."""
    found: list[tuple[str, str, str]] = []
    for summary in summaries:
        if summary.original_content == summary.content:
            continue
        grammar = _grammar_of(summary.path, language)
        if grammar not in ("ts", "tsx", "js"):
            continue
        relative = os.path.relpath(summary.path, repo_dir)
        found.extend(
            (relative, "unread constructor parameter", label)
            for label in unread_constructor_parameters(
                summary.original_content, summary.content, grammar
            )
        )
    return tuple(found)


def _unread_constructor_rows(
    summaries, language: str, repo_dir: str
) -> tuple[tuple[str, str, str], ...]:
    """Every row the removal pass below may act on: C# primary-constructor
    parameters, TypeScript and JavaScript private members, and plain
    TypeScript and JavaScript constructor parameters, each left unread."""
    return (
        _unread_primary_parameters(summaries, language, repo_dir)
        + _unread_private_members(summaries, language, repo_dir)
        + _unread_plain_constructor_parameters(summaries, language, repo_dir)
    )


def _remove_unread_constructor_parameters(
    summaries, language: str, unread: tuple[tuple[str, str, str], ...], repo_dir: str
) -> tuple[list, tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]]:
    """Remove each unread constructor parameter whose callers can be proven;
    see :mod:`~flag_cleanup.ctor_param_removal`.

    Returns the summaries, the unread rows still unread, and the rows naming
    what went, plus anything the deletions stranded. JavaScript has no
    parameter properties, but it has ``#`` fields.

    A deleted argument can be the last read of a parameter of the type that
    passed it: ``class Passing(IClient c) : Svc(c)`` leaves ``c`` unread once
    ``Svc`` loses ``client``, which is CS9113 again. Whatever the removal left
    unread is offered to the same pass, round after round until a round
    strands nothing, so it goes with its own callers or is reported like any
    other. Each row is offered once, and each round that rewrites deletes a
    parameter, so this ends.
    """
    if language not in ("ts", "tsx", "js", "csharp", "dart") or not unread:
        return summaries, unread, ()
    offered = set(unread)
    pending = unread
    still: tuple[tuple[str, str, str], ...] = ()
    rows: tuple[tuple[str, str, str], ...] = ()
    names: tuple[tuple[str, str], ...] = ()
    rewritten: set[str] = set()
    while pending:
        result = ctor_param_removal.remove(
            summaries,
            language,
            pending,
            repo_dir,
            engine=lambda paths, rules: _execute_ctor_param_rules(paths, language, rules, repo_dir),
        )
        still += result.unread
        if not result.rewritten:
            break
        summaries = result.summaries
        rewritten |= result.rewritten
        rows += result.rows
        names += result.argument_names
        pending = tuple(
            row
            for row in _unread_constructor_rows(summaries, language, repo_dir)
            if row not in offered
        )
        offered.update(pending)
    if not rewritten:
        return summaries, still, ()
    # A deleted argument can strand the local it named, and a deleted
    # parameter the import of its type: the same pass, over these files only.
    touched = [s for s in summaries if s.path in rewritten]
    cleaned, stranded = _remove_stranded_bindings(touched, language, repo_dir)
    by_path = {s.path: s for s in cleaned}
    summaries = [by_path.get(s.path, s) for s in summaries]
    kept = ctor_param_removal.kept_argument_locals(
        summaries, tuple(dict.fromkeys(names)), language, repo_dir
    )
    return summaries, still, tuple(dict.fromkeys(rows)) + stranded + kept


def _settle_test_doubles(
    summaries,
    doubles: list[mock_stubs.Prepass] | None,
    language: str,
    accessors: tuple[str, ...],
    repo_dir: str,
    unprocessable: list[str],
) -> tuple[list, tuple[tuple[str, str, str], ...]]:
    """Keep or undo the test-double pre-pass, after the engine.

    A stub is deleted on the strength of the read it stubs having been folded,
    and that read usually lives in another file. If no rewrite in this run
    removed a real read — a key the rules cannot match, or ``tests/``
    configured as a directory of its own — everything is undone and
    :func:`transform_flag` runs again without the pass (see there for why
    keeping the engine's output is not an option).

    Kept, every edited file becomes a summary whose "before" is the
    customer's own text, so the diff, Gate 1 and the rollback all see it.
    """
    passes = [p for p in doubles or () if p.originals]
    if not passes:
        return summaries, ()
    # A file the engine could not process is reported as left untouched. A
    # second pass (the keyless wrappers' doubles) withdraws first, since its
    # "original" is the first pass's output.
    for stub_pass in reversed(passes):
        stub_pass.withdraw(unprocessable)
    passes = [p for p in passes if p.originals]
    if not passes:
        return summaries, ()
    changed = [s for s in summaries if s.original_content != s.content]
    if any(
        mock_stubs.folded_a_read(
            s.original_content, s.content, _grammar_of(s.path, language), accessors
        )
        for s in changed
    ):
        for stub_pass in reversed(passes):
            summaries = stub_pass.rebase(summaries, Rewrite)
        return summaries, tuple(r for p in passes for r in p.report(repo_dir))
    _restore(changed)
    for stub_pass in reversed(passes):
        stub_pass.restore_all()
    raise _NoReadFolded


def _gate_baseline(doubles, path: str, original: str) -> str:
    """What Gate 1 compares ``path``'s result against: the text the last
    test-double pass that edited it left (what the engine saw), else
    ``original``."""
    for stub_pass in reversed(doubles or ()):
        if os.path.abspath(path) in stub_pass.edited:
            return stub_pass.edited[os.path.abspath(path)]
    return original


def _restore(summaries: Iterable) -> None:
    """Put every rewritten file back to the bytes it had before this call.

    The two rollback paths — Gate 1 refusing a rewrite, and an engine panic
    part-way through Gate 2's second invocation — are the same operation, and
    keeping them one function is what stops the second from being forgotten
    again. Summaries whose content did not change are skipped, so this is safe
    to hand the whole list.
    """
    for summary in summaries:
        if summary.original_content != summary.content:
            Path(summary.path).write_text(summary.original_content, encoding="utf-8")


def checkout_root(repo_dir: str) -> str:
    """The root of the git checkout holding ``repo_dir``, or ``repo_dir`` itself.

    ``repo_dir`` is the directory this run was configured to transform, which
    can be a subdirectory of the repository. A file elsewhere in the same
    checkout can still read a constant declared under it, so every question of
    the form "does anything else mention this?" is asked of the whole
    checkout. Read-only (`git rev-parse`); outside a git checkout the answer is
    ``repo_dir``.
    """
    directory = Path(repo_dir)
    if not directory.is_dir():
        directory = directory.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        top = ""
    else:
        top = result.stdout.strip()
    if top:
        return top
    if repo_dir not in _OUTSIDE_A_CHECKOUT:
        _OUTSIDE_A_CHECKOUT.add(repo_dir)
        logger.info(
            "%s is not inside a git checkout; other readers of a key constant are "
            "looked for under it only",
            repo_dir,
        )
    return repo_dir


#: The directories :func:`checkout_root` has already reported falling back for.
_OUTSIDE_A_CHECKOUT: set[str] = set()


def _source_index(repo_dir: str, language: str | None = None) -> dict[Path, str]:
    """Every readable source file in ``repo_dir``'s checkout (resolved path -> text).

    The whole checkout, not just ``repo_dir``: a reader outside the configured
    directory is still a reader (see :func:`checkout_root`). Built once per
    partition and shared by every declaring file in it. Every source extension
    the tool knows is included, because a constant declared in one language is
    routinely read from another (a Java constant from Kotlin, a TS constant from
    a .js file). The files are :mod:`~flag_cleanup.source_index`'s listing; the
    text is read here, because a pass earlier in the same flag may have
    rewritten it.

    For Ruby and ERB it also holds the Ruby no language here claims: views
    (``.jbuilder``, ``.haml``, ``.slim``), project files (``Rakefile``,
    ``Gemfile``, ``config.ru``) and scripts with a Ruby shebang (``bin/setup``),
    any of which can read a Ruby constant. No other language's index lists them.
    """
    suffixes: frozenset[str] = KNOWN_EXTENSIONS
    if language in ruby_owner_reach.LANGUAGES:
        suffixes |= source_index.RUBY_SUFFIXES | source_index.NO_SUFFIX
    index: dict[Path, str] = {}
    for source in source_index.files(repo_dir, suffixes):
        try:
            text = source.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not source.path.suffix and not ruby_owner_reach.is_ruby(source.path, text):
            continue  # a `Makefile`, a `LICENSE`, a shell script
        index[Path(source.resolved)] = text
    return index


def _token(word: str) -> re.Pattern[str]:
    # Shared with the cross-file resolution, so both count a `"$NAME"`
    # template as a mention; see `cross_file_keys.mention_pattern`.
    return cross_file_keys.mention_pattern([word])


def _referenced_elsewhere(
    index: dict[Path, str],
    declaring_path: str,
    consts: tuple[VisibleKeyConst, ...],
    language: str | None = None,
) -> list[str]:
    """Other source files that could be reading one of ``consts``.

    Deliberately textual and deliberately wide: a comment or a string naming the
    constant counts. The question is whether deleting the declaration could
    strand something, and over-answering "yes" only keeps a line that a later
    run will remove once the readers are resolved. A reader has to mention the
    constant's name and, for a type member, the owning type's name too; an
    unexported Go name is only readable from the declaring directory.

    A Ruby reader need not spell either: it can reach the owner whole and read
    the constant by a computed name (``Gate.const_get(n)``), or spell the
    constant through a class that inherits it (``Sub::NAME``). Both are
    :mod:`~flag_cleanup.ruby_owner_reach`'s to find, and each hit is logged
    with the line that reaches the owner. That one includes the declaring file:
    an owner listing its own constants (``def self.all = constants``) keeps
    them, so the declaring file can be among the paths returned.
    """
    declaring = Path(declaring_path).resolve()
    ruby = language in ruby_owner_reach.LANGUAGES
    families: dict[str, frozenset[str]] = {}
    if ruby:
        for owner in sorted({c.owner for c in consts if c.owner}):
            families[owner] = ruby_owner_reach.family(index, owner)
    patterns = [
        (
            c,
            _token(c.name),
            cross_file_keys.mention_pattern(families.get(c.owner, (c.owner,))) if c.owner else None,
        )
        for c in consts
    ]
    hits: list[str] = []
    for path, text in index.items():
        if path == declaring:
            continue
        for const, name_re, owner_re in patterns:
            if const.same_directory_only and path.parent != declaring.parent:
                continue
            # A template that is not parsed cannot show whose constant a name
            # it spells is, so the name alone keeps it.
            if name_re.search(text) and (
                owner_re is None or owner_re.search(text) or (ruby and ruby_owner_reach.unparsed(path))
            ):
                hits.append(str(path))
                break
    if ruby:
        for owner in sorted({c.owner for c in consts}, key=lambda o: o or ""):
            for path, line, form in ruby_owner_reach.reaching(index, owner, families):
                logger.info(
                    "%s:%d reaches %s whole (%s), so it can read key constant %s",
                    path,
                    line,
                    owner or "the top level",
                    form,
                    ", ".join(c.name for c in consts if c.owner == owner),
                )
                if str(path) not in hits:
                    hits.append(str(path))
    return hits


def _partition_by_const_safety(
    paths: list[str],
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] = (),
    *,
    repo_dir: str | None = None,
) -> tuple[list[str], list[str]]:
    """Split candidate files into (const rules allowed, const rules withheld).

    ``accessors`` must be the SAME tuple the const rules were cloned for in
    :func:`_write_generated_rules`. Gate 2 asks whether every surviving
    reference to a hoisted key is a read the const rules remove, and the clones
    are half the answer -- pass one without the other and the feature is either
    inert (gate withholds the file the clone would have cleaned) or unsafe (the
    delete rule strands a reference nothing rewrote). See #2730.

    ``repo_dir`` extends that question past the file. The const rules' delete
    guard is file-scoped, so a declaration that another file could read would be
    deleted with nothing left to define it. Such a file is withheld whole, and
    "another file" means anywhere in ``repo_dir``'s git checkout. A
    declaration no other file can name (private, local, not exported) is never
    withheld on that ground, however many other files reuse its identifier.
    """
    allowed, withheld = [], []
    index: dict[Path, str] | None = None
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - filtered earlier
            withheld.append(path)
            continue
        allowed_here = const_path_is_safe(source, language, flag_key, accessors)
        if allowed_here and repo_dir is not None:
            consts = externally_visible_key_consts(source, language, flag_key)
            if consts:
                if index is None:
                    index = _source_index(repo_dir, language)
                elsewhere = _referenced_elsewhere(index, path, consts, language)
                if elsewhere:
                    logger.info(
                        "%s: key constant %s is referenced from %d file(s), "
                        "so its declaration is kept",
                        path,
                        ", ".join(c.name for c in consts),
                        len(elsewhere),
                    )
                    allowed_here = False
        (allowed if allowed_here else withheld).append(path)
    return allowed, withheld


def _partition_by_readability(
    paths: list[str], language: str
) -> tuple[list[str], list[str]]:
    """Split candidate files into (hand to the engine, skip and report).

    Only ever splits anything for a language whose profile opts in — Dart today
    — so for every other language this is one dict lookup per file and both
    lists come back as they went in. See
    :func:`~flag_cleanup.syntax.source_is_unreadable`.

    An unreadable file is NOT dropped: it goes to the caller's `unprocessable`,
    the same channel a file the engine could not parse uses, so the pull request
    says the flag is still referenced there. A file that cannot be READ at all
    (a decoding error) is left in the transform list rather than skipped here —
    `_candidate_files` already byte-matched it, so this would be a new failure
    mode, and the engine's own abort path is the one that has always handled it.
    """
    readable, skipped = [], []
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - filtered earlier
            readable.append(path)
            continue
        (skipped if source_is_unreadable(source, language) else readable).append(path)
    if skipped:
        # Named by display name rather than hardcoded, because the opt-in lives
        # on the profile: the day a second language takes it, this line must not
        # still say Dart. The `await` hint stays — it is the only shape known to
        # reach here, and a warning that says only "could not be parsed" leaves
        # the reader with nothing to do about it.
        logger.warning(
            "%d %s file(s) could not be parsed and were left untouched: %s. "
            "In Dart this is almost always a local named `await` — valid Dart "
            "that no tree-sitter grammar can read back; rename it and the next "
            "run will clean the file",
            len(skipped),
            _LANGUAGES[language].display_name,
            ", ".join(skipped),
        )
    return readable, skipped


def _execute(
    *,
    paths: list[str],
    piranha_language: str,
    rules_file: Path,
    const_rules_file: Path | None,
    edges_file: Path,
    substitutions: dict[str, str],
    argument_flags: dict[str, object],
    flag_key: str,
    repo_dir: str,
    entries_rules_file: Path | None = None,
):
    """Run Piranha over ``paths``, optionally with the const rules prepended.

    Piranha requires a config directory with canonically-named files, so one is
    assembled per run from the flat per-language files. The const rules are
    PREPENDED because seed rules fire in declaration order and they must see the
    initialiser before the generic rule rewrites it.

    The entry rules are APPENDED, and order does not matter for them: every one
    is a seed rule that matches on its own.
    """
    with tempfile.TemporaryDirectory(prefix="ff-piranha-cfg-") as cfg_dir:
        cfg = Path(cfg_dir)
        rules = rules_file.read_text(encoding="utf-8")
        if const_rules_file is not None and const_rules_file.exists():
            rules = const_rules_file.read_text(encoding="utf-8") + "\n" + rules
        if entries_rules_file is not None and entries_rules_file.exists():
            rules = rules + "\n" + entries_rules_file.read_text(encoding="utf-8")
        (cfg / "rules.toml").write_text(rules, encoding="utf-8")
        edges = edges_file.read_text(encoding="utf-8") if edges_file.exists() else ""
        if edges:
            (cfg / "edges.toml").write_text(edges, encoding="utf-8")

        args = PiranhaArguments(
            piranha_language,
            paths_to_codebase=paths,
            substitutions=substitutions,
            path_to_configurations=str(cfg),
            dry_run=False,  # rewrite files in place
            # The engine deletes a file whose text becomes exactly empty unless
            # told not to. A removal that empties a file leaves it in place,
            # empty, as it always has: the file set a run commits and rolls
            # back is the one it started with.
            delete_file_if_empty=False,
            # Go only: the name each unaliased import is guessed to bind, so
            # the engine can delete one its own fold stranded. See
            # :func:`~flag_cleanup.unused.go_import_facts`.
            **({"file_facts": _go_file_facts(paths)} if piranha_language == "go" else {}),
            **argument_flags,
        )
        try:
            return execute_piranha(args)
        except BaseException as exc:  # noqa: BLE001 - see below
            # pyo3 surfaces a Rust panic as pyo3_runtime.PanicException, which
            # derives from BaseException rather than Exception, so it can only
            # be caught this broadly. Anything else (KeyboardInterrupt, a real
            # Exception) is re-raised untouched. Matched by module + name rather
            # than by import because pyo3_runtime is not importable directly.
            if (type(exc).__module__, type(exc).__name__) != (
                "pyo3_runtime",
                "PanicException",
            ):
                raise
            raise PiranhaTransformError(
                f"Piranha aborted while removing {flag_key!r} from {repo_dir}: {exc}"
            ) from exc


def _go_file_facts(paths: list[str]) -> dict[str, list[dict[str, str]]]:
    """:func:`~flag_cleanup.unused.go_import_facts` for each Go file in ``paths``, by absolute path."""
    facts = {}
    for path in paths:
        if not path.endswith(".go"):
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = go_import_facts(source)
        if found:
            facts[os.path.abspath(path)] = found
    return facts


def _summaries_to_diff(summaries, repo_dir: str) -> str:
    """Build a single unified diff from Piranha's per-file output summaries.

    Hunks are ordered by path. Piranha returns its summaries in an order that
    varies between runs, and Gate 2 can split one transform across two engine
    invocations whose results are simply concatenated — so without this the
    same flag over the same tree produces a differently-ordered diff each time.
    That string is what the customer reads in a dry run.
    """
    # `repo_dir` may name a single FILE (a supported `directories` value), in
    # which case relative_to(root) is `.` — and the whole diff header becomes
    # `--- a/.`, which is the entire visible output of a dry run. Relativise
    # against the parent so the file is named either way.
    root = Path(repo_dir)
    root = root.parent if root.is_file() else root
    chunks: list[str] = []
    for summary in sorted(summaries, key=lambda s: s.path):
        before = summary.original_content
        after = summary.content
        gone = file_deletion.deleted(summary)
        if before == after and not gone:
            continue
        try:
            rel = str(Path(summary.path).relative_to(root))
        except ValueError:
            rel = summary.path
        # A deleted file is spelled the way git spells one: its new side is
        # /dev/null. An empty file has no hunk, so its headers stand alone.
        tofile = "/dev/null" if gone else f"b/{rel}"
        diff = list(
            difflib.unified_diff(
                _lines(before),
                _lines(after),
                fromfile=f"a/{rel}",
                tofile=tofile,
            )
        )
        if not diff:
            diff = [f"--- a/{rel}\n", f"+++ {tofile}\n"]
        chunks.append("".join(_terminated(diff)))
    return "".join(chunks)


#: What git prints for a file whose final line carries no terminator. Emitted
#: verbatim so the diff says which side lacked one instead of quietly implying
#: both had it.
_NO_NEWLINE_MARKER = "\\ No newline at end of file\n"


def _lines(text: str) -> list[str]:
    """``text`` as newline-terminated lines, split the way **git** splits it.

    ``str.splitlines`` also breaks on ``\\f``, ``\\v``, ``\\x1c`` and
    ``\\u2028`` — all of which are legal *inside* a TypeScript string literal
    and none of which git treats as a line break. Splitting on those produced
    a diff with more lines than the file has, at offsets the reviewer cannot
    match up against their editor. Only the final line can come back
    unterminated here, which is what makes :func:`_terminated` exact.
    """
    if not text:
        return []
    parts = text.split("\n")
    lines = [f"{part}\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _terminated(diff: Iterable[str]) -> Iterator[str]:
    """``diff`` with every line newline-terminated, git's marker where it was not.

    ``unified_diff`` emits content lines exactly as handed to it, so the last
    line of a file with no trailing newline arrives unterminated and fuses
    with whatever is printed next — the other side of its own hunk, and then
    the following file's ``--- a/…`` header, which vanishes. The diff is the
    entire visible result of a dry run, so a swallowed header means the
    customer approves a change they were never shown.

    Only content lines can be unterminated: the headers carry ``unified_diff``'s
    own ``lineterm``, which is left at its ``"\\n"`` default.
    """
    for line in diff:
        if line.endswith("\n"):
            yield line
        else:
            yield f"{line}\n"
            yield _NO_NEWLINE_MARKER
