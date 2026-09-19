"""Flag-keyed entries the entry rules DECLINE to delete, so the PR can say so.

Since #3050 neither prong in ``rules/*_entries.toml`` deletes an entry whose
value is the boolean literal this run is REMOVING — ``'k': false`` while the
flag is being folded in as ``true``. That entry is the customer pinning the
flag to the branch about to disappear, which is evidence the code around it is
becoming dead rather than evidence the entry is stale residue. Stripping one
turned a test that asserted the removed branch into one asserting behaviour the
code can no longer produce, and a falsified test is strictly worse than a
residual reference: the residual reference is in the pull-request body, the
falsified test is only in a red build somebody has to bisect.

**A rule that declines to match reports nothing**, which is the whole reason
this module exists. The engine's summaries carry the rewrites it made; there is
no summary for a query that did not fire, so the "N file(s) still reference this
flag" caveat would name the file with the tool's generic shrug — *it may be a
registry entry, a test stub, a comment* — when in fact the tool knows exactly
what it is and why it left it. So the shape is looked for here, with
tree-sitter, and the body names it with its reason.

**The queries below are deliberately WEAKER than the rules they mirror**, and
that is sound rather than sloppy. Each entry file carries two prongs — a stub
prong keyed on the value being a boolean and a registry prong keyed on a
sibling flag key — and #3050 put the same guard on both, so an entry whose
direct value is the contradicting literal is declined by BOTH. That makes the
sibling evidence irrelevant here: any entry of this shape is a declined entry,
whatever its neighbours look like, so the detector needs the key and the value
and nothing else. ``test_the_detector_reports_exactly_what_the_rules_declined``
is what holds the two halves together; if a future prong learns to delete one
of these again, that test goes red rather than the body going quietly wrong.

**Nothing the customer supplies is ever interpolated into a query.** The flag
key comes from the API, so building ``(#eq? @flag_name "<key>")`` by string
substitution would put an untrusted value inside a tree-sitter query, where a
``"`` ends the operand and the rest becomes query syntax — the same hazard
``piranha_runner.ensure_supported_key`` exists for one layer down, and one the
allow-list there does NOT cover for this module, because this module runs
whatever it is handed. The queries are constant; the key and the value are
compared in Python against the captured text.

Java and Swift have no entry rules at all (their map entries cannot be deleted
with their separator in one edit until the fork ships ``replace_node_end``), so
they have nothing to decline and no table row here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import tree_sitter_c_sharp
import tree_sitter_dart
import tree_sitter_go
import tree_sitter_kotlin
import tree_sitter_php
import tree_sitter_python
import tree_sitter_ruby
import tree_sitter_typescript
from tree_sitter import Language, Parser, Query, QueryCursor

from flag_cleanup import syntax

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Detector:
    """How to find a flag-keyed boolean entry in one language.

    ``queries`` are CONSTANT — see the module docstring for why nothing is
    interpolated into them. Each must capture ``@entry`` (the node whose text
    the body prints), ``@flag_name`` (the key, compared against the flag) and
    ``@value`` (the boolean literal). More than one query where a language
    spells the same idea two ways — C#'s collection initializer and its indexer
    form — rather than one query with an alternation, because the two shapes do
    not share a parent node.

    ``case_sensitive`` is False for PHP alone, whose ``TRUE``/``True``/``true``
    are one keyword to the language and one ``boolean`` node to its grammar.
    Every other language here spells its literals exactly one way, and the
    ENTRY RULES carry the same split — php's prong compares with ``#match?``
    and ``(?i)``, the rest with ``#eq?``. A wrong value here is a wrong
    REPORT, never a wrong rewrite.

    ``quoted_key`` is True for the one grammar whose key capture INCLUDES its
    quotes. Dart's ``string_literal`` has no content child to anchor on (see
    ``dart_entries.toml``), so its key arrives as ``'old-checkout'`` and a bare
    comparison would match nothing — silently, which is the failure this field
    exists to make impossible to write by accident.

    ``operator`` is Kotlin's ``to``, the infix function that makes a pair. It
    is a capture rather than a literal in the query for the same reason the
    rule spells it that way: the grammar gives it no distinguished node.
    """

    grammar: object
    queries: tuple[str, ...]
    case_sensitive: bool = True
    quoted_key: bool = False
    operator: str | None = None


# The key capture is DOUBLE-ANCHORED (`. (content) @k .`) wherever the grammar
# has a content node, which is what makes the comparison whole-key rather than
# prefix — and what makes an escape sequence, which splits the literal into two
# children, decline the match instead of matching a longer key. Same anchoring,
# same reason, as every `rules/*_entries.toml`.
_TS_FAMILY_QUERY = """(
  (pair
    key: (string . (string_fragment) @flag_name .)
    value: [(true) (false)] @value) @entry
)"""

_DETECTORS: dict[str, _Detector] = {
    "ts": _Detector(tree_sitter_typescript.language_typescript, (_TS_FAMILY_QUERY,)),
    # `tsx` and `js` share the TSX grammar, exactly as `ts_syntax._parser` and
    # the engine's own `javascript` arm do (piranha#25).
    "tsx": _Detector(tree_sitter_typescript.language_tsx, (_TS_FAMILY_QUERY,)),
    "js": _Detector(tree_sitter_typescript.language_tsx, (_TS_FAMILY_QUERY,)),
    "python": _Detector(
        tree_sitter_python.language,
        ("""(
  (pair
    key: (string (string_start) . (string_content) @flag_name . (string_end))
    value: [(true) (false)] @value) @entry
)""",),
    ),
    "ruby": _Detector(
        tree_sitter_ruby.language,
        ("""(
  (pair
    key: [(string . (string_content) @flag_name .) (hash_key_symbol) @flag_name]
    value: [(true) (false)] @value) @entry
)""",),
    ),
    "php": _Detector(
        tree_sitter_php.language_php,
        ("""(
  (array_element_initializer
    . [(string . (string_content) @flag_name .)
       (encapsed_string . (string_content) @flag_name .)]
    . (boolean) @value) @entry
)""",),
        case_sensitive=False,
    ),
    "go": _Detector(
        tree_sitter_go.language,
        ("""(
  (keyed_element
    . (literal_element
        (interpreted_string_literal . (interpreted_string_literal_content) @flag_name .))
    . (literal_element [(true) (false)]) @value) @entry
)""",),
    ),
    # Kotlin parses `true`/`false` in this position as a plain `identifier`
    # rather than as their own node types, which is why the rule compares the
    # value by TEXT and why the query here cannot filter on node type either.
    # `_BOOLEAN_TEXTS` does that filtering instead — an `identifier` that is
    # neither literal is some other expression and not an entry this tool would
    # ever have deleted.
    "kt": _Detector(
        tree_sitter_kotlin.language,
        ("""(
  (value_argument
    (infix_expression
      . (string_literal . (string_content) @flag_name .)
      . (identifier) @operator
      . (identifier) @value)) @entry
)""",),
        operator="to",
    ),
    "dart": _Detector(
        tree_sitter_dart.language,
        ("""(
  (pair . (string_literal) @flag_name . [(true) (false)] @value) @entry
)""",),
        quoted_key=True,
    ),
    "csharp": _Detector(
        tree_sitter_c_sharp.language,
        (
            """(
  (initializer_expression
    (initializer_expression
      . (string_literal . (string_literal_content) @flag_name .)
      . (boolean_literal) @value) @entry)
)""",
            """(
  (assignment_expression
    left: (element_binding_expression
            (argument (string_literal . (string_literal_content) @flag_name .)))
    right: (boolean_literal) @value) @entry
)""",
        ),
    ),
}

#: Node text a language accepts as a boolean literal, lowercased. Kotlin needs
#: this because its grammar hands back an `identifier`; everywhere else it is a
#: cheap second opinion that costs nothing.
_BOOLEAN_TEXTS = frozenset({"true", "false"})


def covers(language: str) -> bool:
    """Whether a declined entry in ``language`` would be reported.

    True for every language with a ``rules/<base>_entries.toml``, counting a
    template language that borrows another's rules. False means the language
    has no entry rules, so it declines nothing and there is nothing to miss —
    which is why ``test_every_language_with_entry_rules_has_a_detector`` asks
    this of the rules on disk rather than trusting the table above.
    """
    if language in _DETECTORS:
        return True
    view = syntax.host_code_view("", language)
    return view is not None and view[1] in _DETECTORS


@lru_cache(maxsize=None)
def _compiled(language: str) -> tuple[Query, ...]:
    detector = _DETECTORS[language]
    lang = Language(detector.grammar())
    return tuple(Query(lang, text) for text in detector.queries)


@lru_cache(maxsize=None)
def _parser(language: str) -> Parser:
    return Parser(Language(_DETECTORS[language].grammar()))


def _text(node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _entries_in(source: str, language: str, flag_key: str, untreated: str) -> list[str]:
    """Text of every flag-keyed entry in ``source`` valued ``untreated``."""
    detector = _DETECTORS[language]
    keys = (f"'{flag_key}'", f'"{flag_key}"') if detector.quoted_key else (flag_key,)
    wanted = untreated if detector.case_sensitive else untreated.lower()
    data = source.encode("utf-8")
    tree = _parser(language).parse(data)
    found: dict[int, str] = {}
    for query in _compiled(language):
        for _pattern, captures in QueryCursor(query).matches(tree.root_node):
            entry = (captures.get("entry") or (None,))[0]
            name = (captures.get("flag_name") or (None,))[0]
            value = (captures.get("value") or (None,))[0]
            if entry is None or name is None or value is None:
                continue
            if _text(name, data) not in keys:
                continue
            if detector.operator is not None:
                operator = (captures.get("operator") or (None,))[0]
                if operator is None or _text(operator, data) != detector.operator:
                    continue
            text = _text(value, data)
            if text.lower() not in _BOOLEAN_TEXTS:
                continue
            if (text if detector.case_sensitive else text.lower()) != wanted:
                continue
            # The engine's own match range for a deleted entry includes the
            # trailing separator, and `_flag_keyed_entries` strips it for the
            # same reason this does: the body is for a reviewer, not a diff.
            found[entry.start_byte] = (
                _text(entry, data).strip().strip(",;").strip()
            )
    # Source order, and keyed by position: C#'s two queries cannot match the
    # same node today, but a future third shape might, and a PR body that names
    # one entry twice reads as two problems.
    return [found[start] for start in sorted(found)]


def find_contradicting_entries(
    paths: list[str],
    language: str,
    flag_key: str,
    untreated: str,
    repo_dir: str,
) -> tuple[tuple[str, str], ...]:
    """``((relative path, entry text), …)`` for every entry the rules declined.

    ``untreated`` is the literal spelling of the branch being REMOVED — the
    same value ``piranha_runner`` substitutes into ``@untreated`` — and is
    passed in rather than derived here so the rules and the report cannot
    disagree about how a language spells ``false``.

    Read from the files on disk, BEFORE the transform, deliberately. The
    surviving entry is still there afterwards — that is the point — but the
    fold has rewritten the code around it, and a file the engine quarantined as
    unprocessable never reaches the post state at all. The entries this names
    are the customer's own bytes either way.

    An unreadable or unparseable file yields nothing rather than raising. This
    is a REPORT: a missing line costs a reviewer the reason for a residual
    reference they can still see, where an exception here would abandon a
    removal that is otherwise correct.

    One imprecision is accepted rather than engineered away: a file the engine
    later QUARANTINES (`_quarantine_and_retry`) is still scanned here, so its
    entry is attributed to this guard when the real reason it survived is that
    the engine could not parse the file. The claim the body makes about it — it
    pins the flag to the branch being removed — is true either way, and the
    same file is named in the unprocessed caveat beside it, so the reviewer has
    both halves. Files the GATE cannot parse are already excluded: this runs
    after `_partition_by_readability`.
    """
    if not covers(language):
        return ()
    results: list[tuple[str, str]] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        view = syntax.host_code_view(source, language)
        read_as = language
        if view is not None:
            # A template's host code, judged under the host's own profile —
            # the same substitution `syntax.const_path_is_safe` makes, and
            # sound for the same reason: this produces a STRING to print and
            # never an offset to edit, so the derived view's own offsets are
            # harmless.
            source, read_as = view
        if read_as not in _DETECTORS:
            continue
        try:
            texts = _entries_in(source, read_as, flag_key, untreated)
        except Exception:  # pragma: no cover - a grammar or query fault
            logger.debug("contradicting-entry scan failed for %s", path, exc_info=True)
            continue
        results.extend((os.path.relpath(path, repo_dir), text) for text in texts)
    return tuple(results)
