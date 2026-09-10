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

Piranha ships built-in feature-flag *cleanup* rules only for
go/java/kt/ruby/scala/swift/cs — there are NONE for TypeScript/JavaScript or
Python. So for those two this tool carries the whole cascade itself (fold
``if (true/false)``, simplify boolean expressions, drop dead statements)
alongside the seed rules, while Java/Go/Swift/C# need only a seed rule and get
the rest from the engine — C# needs LESS than Java: its built-ins already
carry the trailing-else-if-false fix java.toml has to hand-roll as a second
rule (see rules/csharp.toml). That asymmetry is the single biggest thing to
know before editing this directory: **TypeScript is the worst case, not the
representative one**, and a new language is usually a much smaller job than
``ts.toml`` makes it look.

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
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - project requires >=3.11
    import tomli as tomllib

from polyglot_piranha import PiranhaArguments, execute_piranha

from flag_cleanup import kotlin_sentinel
from flag_cleanup.kotlin_sentinel import Rewrite
from flag_cleanup.go_fold import fold_literal_cases as fold_go_literal_cases
from flag_cleanup.ruby_fold import fold_literal_whens as fold_ruby_literal_whens
from flag_cleanup.php_fold import (
    fold_literal_cases as fold_php_literal_cases,
    fold_literal_conditions as fold_php_literal_conditions,
)
from flag_cleanup.python_fold import (
    fold_literal_conditions,
    inline_literal_bindings,
)
from flag_cleanup.reindent import reindent_spliced_lines
from flag_cleanup.syntax import (
    collapse_introduced_carriage_returns,
    collapse_introduced_double_spaces,
    const_path_is_safe,
    remove_unreachable_statements,
    source_is_unreadable,
    strip_introduced_trailing_whitespace,
    transform_broke_syntax,
)
from flag_cleanup.ts_syntax import strip_dangling_type_separators
from flag_cleanup.unused import remove_stranded_bindings

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

    ``refused`` are the files whose own rewrite did not survive the gate;
    ``discarded`` are the files that rewrote cleanly but were rolled back with
    them, because the rollback is deliberately all-or-nothing (see
    :func:`run_piranha`).
    """

    def __init__(
        self, message: str, refused: Iterable[str] = (), discarded: Iterable[str] = ()
    ) -> None:
        super().__init__(message)
        self.refused = tuple(refused)
        self.discarded = tuple(discarded)


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
    """

    piranha_id: str
    rule_base: str
    extensions: tuple[str, ...]
    true_literal: str = "true"
    false_literal: str = "false"
    #: Whether this language's grammar has JSX nodes, which decides whether
    #: ``rules/<base>_jsx.toml`` is appended to its rule set. NOT derivable from
    #: ``rule_base``: `ts`, `tsx` and `js` all share `rules/ts.toml` and do not
    #: share a grammar, and a query naming `jsx_expression` under the plain
    #: `typescript` grammar is a COMPILE error, not an unmatched query — the
    #: engine panics before reading a file, so one misplaced rule aborts every
    #: `.ts` run. A language with no `_jsx.toml` beside its base can set this
    #: freely; the file is only loaded if it exists.
    parses_jsx: bool = False
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
    "tsx": _Language("tsx", "ts", (".tsx",), parses_jsx=True, display_name="TSX"),
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
        parses_jsx=True,
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
    # The grammar the RULES query (the engine's bundled tree-sitter-dart 0.2.0)
    # and the grammar GATE 1 parses with (PyPI's 0.1.0) are different grammars,
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


def candidate_files(repo_dir: str, language: str, flag_key: str) -> list[str]:
    """Every file :func:`run_piranha` could rewrite for this dir/language/key.

    Public because the orchestrator needs the exact same set to snapshot before
    the transform: ``run_piranha`` hands Piranha this explicit file list as
    ``paths_to_codebase``, so it is a sound *superset* of what the engine can
    possibly write. A snapshot of it is therefore a complete undo, which
    ``git checkout`` alone is not (it restores tracked files only — see
    ``git_ops.WorktreeSnapshot``).

    Raises ``ValueError`` for an unsupported ``language``, exactly like
    :func:`run_piranha`.
    """
    return _candidate_files(Path(repo_dir), _language_spec(language).extensions, flag_key)


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
    #: :func:`~flag_cleanup.unused.remove_stranded_bindings`.
    bindings: tuple[tuple[str, str, str], ...] = ()
    #: ``(path, text)`` for every flag-keyed ENTRY the ``*_entries.toml``
    #: rules deleted — a registry line, an override property, an object-type
    #: member. Reported so the reviewer sees the registry line went and under
    #: which evidence rule, rather than finding it in the diff. Read off the
    #: engine's own summaries by rule-name prefix; see :func:`_flag_keyed_entries`.
    entries: tuple[tuple[str, str], ...] = ()


def run_piranha(
    repo_dir: str,
    language: str,
    flag_key: str,
    treatment: bool,
    paths: list[str] | None = None,
    accessors: tuple[str, ...] = (),
    known_flag_keys: frozenset[str] = frozenset(),
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
        known_flag_keys=known_flag_keys,
    ).diff


def transform_flag(
    repo_dir: str,
    language: str,
    flag_key: str,
    treatment: bool,
    paths: list[str] | None = None,
    accessors: tuple[str, ...] = (),
    known_flag_keys: frozenset[str] = frozenset(),
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

    if paths is None:
        paths = _candidate_files(Path(repo_dir), spec.extensions, flag_key)
    if not paths:
        return TransformOutcome("")

    # PRE-FLIGHT — drop any file the GATE cannot fully parse, before the engine
    # is handed it. Dart only; see `syntax.source_is_unreadable` for the defect
    # this closes and the corpus measurement that makes an absolute parse check
    # affordable there. Deliberately BEFORE Gate 2 and the engine: the whole
    # point is that neither of them can see the hazard, so a check that ran
    # afterwards would be reading a file the engine had already corrupted.
    paths, skipped = _partition_by_readability(paths, language)
    if not paths:
        # Every candidate was unreadable, so there is nothing to transform —
        # but this is emphatically NOT a no-match, and returning a bare empty
        # diff would report it as one. The files ride out on `unprocessable`,
        # which is what puts them in front of a human.
        return TransformOutcome("", unprocessable=tuple(skipped))

    rules_file = RULES_DIR / f"{base}.toml"
    const_rules_file = RULES_DIR / f"{base}_const.toml"
    edges_file = RULES_DIR / f"{base}_edges.toml"
    if not rules_file.exists():
        raise FileNotFoundError(f"missing Piranha rules file: {rules_file}")

    # Grammar-scoped, not base-scoped. Appended rather than merged into
    # `{base}.toml` because languages sharing a rule base do not share a
    # grammar, and a rule whose query names a node the grammar lacks aborts the
    # ENGINE, not just that rule — see `_Language.parses_jsx`. Resolved after
    # the accessor generation below deliberately leaves `rules_file` alone:
    # these rules are reached by edges and match no flag key, so nothing in them
    # varies with the accessors.
    jsx_rules_file = RULES_DIR / f"{base}_jsx.toml" if spec.parses_jsx else None
    jsx_edges_file = RULES_DIR / f"{base}_jsx_edges.toml" if spec.parses_jsx else None

    # Flag-keyed ENTRY rules (registry lines, override properties, object-type
    # members) — the residue a read-anchored rule cannot see. Appended like the
    # JSX file; every rule in it is a seed, so order relative to the base file
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

    substitutions = {
        "stale_flag_name": flag_key,
        "treated": spec.true_literal if treatment else spec.false_literal,
        "known_flag_keys": render_known_flag_keys(known_flag_keys, flag_key, language),
    }
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
        paths, language, flag_key, accessors
    )

    # THE KOTLIN PRE-PASS — the only thing in this tool that edits the
    # customer's files before the engine does. It marks the calls the rules are
    # allowed to rewrite, because Kotlin rules cannot read a string literal and
    # so cannot tell one flag's guard from another's. Everything it writes is
    # undone below on every path: it owns the whole responsibility, which is
    # why the restore is driven from `prepass.originals` rather than from the
    # engine's summaries (those carry the sentinel-ified text as their
    # "before", and writing THAT back would leave a sentinel on disk).
    prepass = _sentinel_prepass(
        language, flag_key, accessors, const_key_paths=frozenset(with_const)
    )
    if prepass is not None:
        substitutions["sentinel"] = prepass.sentinel
        try:
            prepass.apply(paths)
        except kotlin_sentinel.SentinelCollisionError as exc:
            # "This file must not be transformed", and it can fire after earlier
            # files were already marked — so the undo runs before the refusal
            # propagates. Reported as a refusal rather than as an empty diff: it
            # is a shape the customer has to act on, and a silent `no-changes`
            # would repeat forever.
            prepass.restore_all()
            logger.warning("refusing to transform for flag %r: %s", flag_key, exc)
            raise UnsafeRewriteError(
                f"the Kotlin pre-pass refused this transform for flag "
                f"{flag_key!r}: {exc}",
                # The one file the customer has to act on — NOT every candidate.
                # Falling back to the whole path list named files with no read
                # of the flag in them whenever the FIRST file was the one that
                # raised, which is the common case.
                refused=[prepass.failed_path] if prepass.failed_path else [],
                discarded=sorted(set(prepass.originals) - {prepass.failed_path}),
            ) from exc

    run_groups = partial(
        _execute_groups,
        piranha_language=piranha_language,
        rules_file=rules_file,
        const_rules_file=const_rules_file,
        edges_file=edges_file,
        jsx_rules_file=jsx_rules_file,
        jsx_edges_file=jsx_edges_file,
        entries_rules_file=entries_rules_file,
        substitutions=substitutions,
        argument_flags=argument_flags,
        flag_key=flag_key,
        repo_dir=repo_dir,
    )

    unprocessable: list[str] = list(skipped)
    summaries: list = []
    try:
        run_groups(
            with_const=with_const, without_const=without_const, collected=summaries
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
                paths, language, flag_key, run_groups, accessors
            )
            # Extend, never rebind: the pre-flight skips above are already in
            # here, and `_quarantine_and_retry` only ever knows about the files
            # it was given.
            unprocessable.extend(quarantined)
        except BaseException:
            if prepass is not None:
                prepass.restore_all()
            raise
        if len(quarantined) == len(paths):
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
    finally:
        # Every engine invocation — the first pass and the quarantine retry —
        # happens inside this statement, and nothing after it reads a rule
        # file. `raise abort` above runs this on its way out.
        if generated_rules is not None:
            generated_rules.cleanup()

    # Read BEFORE any text pass or the pre-pass rebase: both replace a changed
    # summary with a `Rewrite`, and only the engine's summary carries the
    # rule name each edit came from.
    entries, entry_paths = _flag_keyed_entries(summaries, repo_dir)

    # Swap the engine's sentinel-ified "before" for what the customer actually
    # had, and pull in any file the pre-pass marked that the engine returned no
    # summary for — that file is still sitting on disk with a sentinel in it,
    # and it has to reach the refusal path below rather than be left there.
    if prepass is not None:
        summaries = prepass.rebase(summaries)

    # PHP only, and BEFORE the whitespace tidy rather than beside the Python
    # fold below — that ordering is the point. Promoting a mid-chain `elseif`
    # leaves `} ` at end of line, the same artefact TypeScript's
    # `delete_else_if_false` produces, and the tidy is what removes it. Running
    # after would ship the space.
    if language == "php":
        summaries = _fold_php_literals(summaries)

    # Go only, and in the same window as PHP's and for the same reason: the
    # engine folds the `if` family, so a tagless `switch { case <read>: … }`
    # keeps a dead arm the built-ins cannot reach. BEFORE the whitespace tidy —
    # deleting an arm leaves the line it sat on holding only its indent, and the
    # tidy is what clears that. See :mod:`flag_cleanup.go_fold` for why no
    # Piranha rule can finish this one.
    if language == "go":
        summaries = _fold_go_literals(summaries)

    # Ruby's subject-less `case` is the same construct as Go's tagless switch,
    # and had the same gap — except Ruby carried no residue entry, so the dead
    # arm SHIPPED rather than being refused. See :mod:`flag_cleanup.ruby_fold`.
    if language == "ruby":
        summaries = _fold_ruby_literals(summaries)

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

    # The stray `\r` an entry deletion leaves in a CRLF file. LAST of the
    # entry tidies on purpose — the `;`-separated object type only reaches the
    # `\r\r\n` shape once the pass above has cut its dangling `;`. See
    # `_collapse_entry_carriage_returns`.
    summaries = _collapse_entry_carriage_returns(summaries, language, entry_paths)

    # Delete what the fold stranded, for the same reason and in the same window:
    # Gate 1 must judge, and the customer must see, the bytes that stay on disk.
    # Before Gate 1 specifically — in Java these statements are the difference
    # between a branch that compiles and one that does not, which is what this
    # used to refuse over.
    summaries, stranded = _remove_stranded_statements(summaries, language, repo_dir)

    # Delete the imports and locals the fold stranded, in the same window and
    # for the same reason: Gate 1 must judge, and the customer must see, the
    # bytes that stay on disk. In Go both shapes are compile errors, so this is
    # the difference between a branch that builds and one that does not.
    summaries, bindings = _remove_stranded_bindings(summaries, language, repo_dir)

    # Python only, and the same window again. The engine cannot re-indent, so
    # `python.toml` folds only single-simple-statement branches and leaves the
    # rest standing as `if True:` — which Gate 1 then refuses. This finishes
    # those folds where the whole file is in hand.
    if language == "python":
        summaries = _fold_python_literals(summaries)

    # Give the spliced lines back the indentation the engine dropped. LAST of
    # the text passes and still inside the same window, because it reads the
    # bytes every earlier pass produced: a fold the Python pass performed, or a
    # statement the stranded-statement pass deleted, is part of the shift this
    # has to measure. See :mod:`flag_cleanup.reindent` for the three artefact
    # shapes and why the INPUT is what holds the answer to all of them.
    summaries = _reindent_folds(summaries, language)

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
    changed = [s for s in summaries if s.original_content != s.content]
    refused = sorted(
        {
            s.path
            for s in changed
            if transform_broke_syntax(s.original_content, s.content, language)
            # A surviving sentinel means the engine did not consume a read the
            # pre-pass marked. It parses, so Gate 1's checks are blind to it,
            # and it would ship an identifier that does not compile and that no
            # customer can interpret. Refuse — never diff.
            or (prepass is not None and prepass.survived(s.content))
        }
    )
    if refused:
        _restore(changed)
        discarded = sorted({s.path for s in changed} - set(refused))
        logger.warning(
            "refusing the rewrite of %s for flag %r: the result did not survive "
            "the post-transform syntax check. %d other file(s) rewrote cleanly "
            "and were rolled back with it (%s), so this flag produces no diff at "
            "all until the refused file(s) are changed by hand or the key is "
            "added to the `ignore` input",
            ", ".join(refused),
            flag_key,
            len(discarded),
            ", ".join(discarded) or "none",
        )
        raise UnsafeRewriteError(
            f"the rewrite of {', '.join(refused)} for flag {flag_key!r} did not "
            "survive the post-transform syntax check, so it was rolled back "
            f"along with {len(discarded)} file(s) that rewrote cleanly",
            refused=refused,
            discarded=discarded,
        )

    return TransformOutcome(
        _summaries_to_diff(summaries, repo_dir),
        tuple(unprocessable),
        stranded,
        bindings,
        entries,
    )


def _execute_groups(
    *,
    with_const: list[str],
    without_const: list[str],
    const_rules_file: Path,
    collected: list,
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
        collected.extend(
            _execute(
                paths=group,
                const_rules_file=const_rules_file if use_const_rules else None,
                **kwargs,
            )
        )
    return collected


def _quarantine_and_retry(
    paths: list[str],
    language: str,
    flag_key: str,
    run_groups,
    accessors: tuple[str, ...] = (),
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
    """
    allowed, _withheld = _partition_by_const_safety(paths, language, flag_key, accessors)
    const_ok = frozenset(allowed)
    survivors: list[str] = []
    unprocessable: list[str] = []
    for path in paths:
        # Probed under the SAME Gate 2 decision the real run would make — which
        # is why `accessors` has to reach this far. Forcing the const rules on
        # here would abort for a file they were withheld from and quarantine it
        # for a reason the real run never had; computing the partition without
        # the accessors would do the mirror image, withholding from the probe a
        # file the real run cleans.
        one = {"with_const": [path], "without_const": []}
        if path not in const_ok:
            one = {"with_const": [], "without_const": [path]}
        probe: list = []
        try:
            run_groups(collected=probe, **one)
        except PiranhaTransformError:
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
    with_const, without_const = _partition_by_const_safety(survivors, language, flag_key)
    summaries: list = []
    try:
        run_groups(
            with_const=with_const, without_const=without_const, collected=summaries
        )
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


def _sentinel_prepass(
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] = (),
    const_key_paths: frozenset[str] = frozenset(),
) -> kotlin_sentinel.SentinelPrePass | None:
    """The pre-pass this language needs, or ``None`` for the languages that don't.

    Only Kotlin has one, and it is a workaround for a specific engine
    limitation rather than a general stage — see
    :mod:`flag_cleanup.kotlin_sentinel`. Keeping the decision in one function
    means the three places `run_piranha` has to care about it all ask the same
    question.
    """
    if language != "kt":
        return None
    return kotlin_sentinel.SentinelPrePass(
        flag_key=flag_key,
        sentinel=kotlin_sentinel.sentinel_for(flag_key),
        accessors=accessors,
        const_key_paths=const_key_paths,
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
            summary.original_content, summary.content, language
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
            summary.original_content, summary.content, language
        )
        if cleaned == summary.content:
            tidied.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        tidied.append(Rewrite(summary.path, summary.original_content, cleaned))
    return tidied


def _collapse_entry_carriage_returns(summaries, language: str, entry_paths: set[str]) -> list:
    """Drop the stray carriage return an entry deletion leaves in a CRLF file.

    Same scoping as :func:`_collapse_entry_double_spaces` and for the same
    reason, but a SEPARATE pass placed LAST, after the `;` strip — which is
    what makes it cover two residues rather than one. The engine leaves
    `},\r\r\n` directly (a whole-line deletion takes the line's `\n` and
    strands its `\r` on the line above), but the `;`-separated object type
    reaches the shape only once `strip_dangling_type_separators` has cut the
    dangling `;`: the engine hands over `{\r;\r\n`, which no `\r\r\n` rule
    can see. Running before that pass would fix the first and miss the second.
    """
    tidied = []
    for summary in summaries:
        if summary.original_content == summary.content or summary.path not in entry_paths:
            tidied.append(summary)
            continue
        cleaned = collapse_introduced_carriage_returns(
            summary.original_content, summary.content, language
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
        cleaned = strip_dangling_type_separators(summary.content, language)
        if cleaned == summary.content:
            tidied.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        tidied.append(Rewrite(summary.path, summary.original_content, cleaned))
    return tidied


def _fold_php_literals(summaries) -> list:
    """Finish the two folds the engine's PHP cascade cannot reach.

    One pass rather than the Python path's two: PHP has no binding to inline —
    the built-ins already do that — so the only thing left is the literal
    condition. See :mod:`flag_cleanup.php_fold` for which two shapes those are
    and why neither is expressible as a rule.
    """
    folded = []
    for summary in summaries:
        if summary.original_content == summary.content:
            folded.append(summary)
            continue
        # Two folds, both PHP's: the `if`/`elseif` family and the
        # `switch (true)` multi-way guard (#2692). Independent shapes, so the
        # order between them does not matter — but both must run before Gate 1.
        result = fold_php_literal_cases(
            summary.original_content,
            fold_php_literal_conditions(summary.original_content, summary.content),
        )
        if result == summary.content:
            folded.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        folded.append(Rewrite(summary.path, summary.original_content, result))
    return folded


def _fold_go_literals(summaries) -> list:
    """Finish the tagless-switch folds the engine's Go cascade cannot reach.

    One pass, like PHP's: Go has no binding to inline — the built-ins already
    do that — so the only thing left is the literal `case` arm.
    """
    folded = []
    for summary in summaries:
        if summary.original_content == summary.content:
            folded.append(summary)
            continue
        result = fold_go_literal_cases(summary.original_content, summary.content)
        if result == summary.content:
            folded.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        folded.append(Rewrite(summary.path, summary.original_content, result))
    return folded


def _fold_ruby_literals(summaries) -> list:
    """Finish the subject-less `case` folds the engine's Ruby cascade leaves."""
    folded = []
    for summary in summaries:
        if summary.original_content == summary.content:
            folded.append(summary)
            continue
        result = fold_ruby_literal_whens(summary.original_content, summary.content)
        if result == summary.content:
            folded.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        folded.append(Rewrite(summary.path, summary.original_content, result))
    return folded


def _fold_python_literals(summaries) -> list:
    """Finish the folds the engine could not re-indent, and write them back.

    Two passes, and the ORDER is the point rather than an implementation
    detail: inlining a name the transform bound to a literal is what turns
    `if use_legacy:` into `if True:`, which is the only thing the condition
    folder can act on. Reversed, the folder would find nothing and the file
    would still reach the residue gate half-done.
    """
    folded = []
    for summary in summaries:
        if summary.original_content == summary.content:
            folded.append(summary)
            continue
        result = fold_literal_conditions(
            summary.original_content,
            inline_literal_bindings(summary.original_content, summary.content),
        )
        if result == summary.content:
            folded.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        folded.append(Rewrite(summary.path, summary.original_content, result))
    return folded


def _reindent_folds(summaries, language: str) -> list:
    """Re-emit every changed summary with the fold's indentation restored.

    Same passthrough shape as :func:`_tidy_trailing_whitespace`: a file whose
    lines the engine placed correctly is handed back untouched rather than
    copied.
    """
    reindented = []
    for summary in summaries:
        if summary.original_content == summary.content:
            reindented.append(summary)
            continue
        result = reindent_spliced_lines(
            summary.original_content, summary.content, language
        )
        if result == summary.content:
            reindented.append(summary)
            continue
        Path(summary.path).write_text(result, encoding="utf-8")
        reindented.append(Rewrite(summary.path, summary.original_content, result))
    return reindented


def _remove_stranded_statements(
    summaries, language: str, repo_dir: str
) -> tuple[list, tuple[tuple[str, str], ...]]:
    """Delete unreachable statements from each changed file; report what went.

    Paths are reported relative to ``repo_dir`` because they end up in a pull
    request body, where an absolute container path means nothing.
    """
    cleaned_summaries = []
    stranded: list[tuple[str, str]] = []
    for summary in summaries:
        if summary.original_content == summary.content:
            cleaned_summaries.append(summary)
            continue
        cleaned, removed = remove_unreachable_statements(
            summary.original_content, summary.content, language
        )
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


def _remove_stranded_bindings(
    summaries, language: str, repo_dir: str
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
        cleaned, acted = remove_stranded_bindings(
            summary.original_content, summary.content, language
        )
        if not acted:
            cleaned_summaries.append(summary)
            continue
        Path(summary.path).write_text(cleaned, encoding="utf-8")
        cleaned_summaries.append(
            Rewrite(summary.path, summary.original_content, cleaned)
        )
        relative = os.path.relpath(summary.path, repo_dir)
        bindings.extend((relative, c.kind, c.text) for c in acted)
    return cleaned_summaries, tuple(bindings)


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


def _partition_by_const_safety(
    paths: list[str], language: str, flag_key: str, accessors: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """Split candidate files into (const rules allowed, const rules withheld).

    ``accessors`` must be the SAME tuple the const rules were cloned for in
    :func:`_write_generated_rules`. Gate 2 asks whether every surviving
    reference to a hoisted key is a read the const rules remove, and the clones
    are half the answer -- pass one without the other and the feature is either
    inert (gate withholds the file the clone would have cleaned) or unsafe (the
    delete rule strands a reference nothing rewrote). See #2730.
    """
    allowed, withheld = [], []
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - filtered earlier
            withheld.append(path)
            continue
        allowed_here = const_path_is_safe(source, language, flag_key, accessors)
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
    jsx_rules_file: Path | None = None,
    jsx_edges_file: Path | None = None,
    entries_rules_file: Path | None = None,
):
    """Run Piranha over ``paths``, optionally with the const rules prepended.

    Piranha requires a config directory with canonically-named files, so one is
    assembled per run from the flat per-language files. The const rules are
    PREPENDED because seed rules fire in declaration order and they must see the
    initialiser before the generic rule rewrites it.

    The JSX rules are APPENDED, and order genuinely does not matter for them:
    every one is ``is_seed_rule = false`` and reachable only through an edge, so
    declaration order — which decides nothing but seed precedence — never
    applies. They are separated from the base file by grammar rather than by
    rule base; see :attr:`_Language.parses_jsx`. The entry rules are appended
    for the same reason and with the same indifference to order: every one is
    a seed rule that matches on its own.
    """
    with tempfile.TemporaryDirectory(prefix="ff-piranha-cfg-") as cfg_dir:
        cfg = Path(cfg_dir)
        rules = rules_file.read_text(encoding="utf-8")
        if const_rules_file is not None and const_rules_file.exists():
            rules = const_rules_file.read_text(encoding="utf-8") + "\n" + rules
        if jsx_rules_file is not None and jsx_rules_file.exists():
            rules = rules + "\n" + jsx_rules_file.read_text(encoding="utf-8")
        if entries_rules_file is not None and entries_rules_file.exists():
            rules = rules + "\n" + entries_rules_file.read_text(encoding="utf-8")
        (cfg / "rules.toml").write_text(rules, encoding="utf-8")
        edges = edges_file.read_text(encoding="utf-8") if edges_file.exists() else ""
        if jsx_edges_file is not None and jsx_edges_file.exists():
            edges = edges + "\n" + jsx_edges_file.read_text(encoding="utf-8")
        if edges:
            (cfg / "edges.toml").write_text(edges, encoding="utf-8")

        args = PiranhaArguments(
            piranha_language,
            paths_to_codebase=paths,
            substitutions=substitutions,
            path_to_configurations=str(cfg),
            dry_run=False,  # rewrite files in place
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
        if before == after:
            continue
        try:
            rel = str(Path(summary.path).relative_to(root))
        except ValueError:
            rel = summary.path
        diff = difflib.unified_diff(
            _lines(before),
            _lines(after),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
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
