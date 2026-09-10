"""Generate extra rewrite rules for a project's own flag-read accessors.

Most codebases do not call the SDK at the point of the flag read. They wrap it
-- ``useFlag('web-onboarding')`` over a typed registry -- because that is the
tidy thing to do. The rules ship knowing only the SDK's own names, so every
such call site is invisible to the engine and the flag reports ``no-changes``:
a green run, no pull request, and nothing anywhere saying why. This module is
what lets a project name its wrapper.

**Why generate rules instead of widening a predicate.** A query may hold only
one top-level pattern, so alternation over callee names is not expressible in
one rule, and the obvious escape -- ``(#any-of? @sdk_fn "a" "b")`` -- is
SILENTLY IGNORED by Piranha's bundled tree-sitter. An ignored predicate does
not fail; it stops constraining, leaving ``@sdk_fn`` unbound so the rule
matches *any* call whose first argument is the flag key. See the
``#any-of?`` section of this tool's CLAUDE.md, and
``test_rules_use_no_ignored_predicates``. So: one rule per name, each with
``#eq?``, which means cloning.

**What makes cloning cheap.** The seed read rules carry
``groups = [...]`` and the ``*_edges.toml`` files wire the cleanup cascade on
the GROUP, never on a rule name. A clone that keeps its template's groups
inherits the entire cascade with no edge changes at all.

**Kotlin does not come through here.** Its rule matches a sentinel rather than
a name, because Kotlin rules cannot read a string literal and so cannot tell
one flag's guard from another's. Accessor support for Kotlin lives in
``kotlin_sentinel``, which is what knows the names there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "SynthesisError",
    "generated_rule_names",
    "with_accessors",
    "with_accessor_const_rules",
    "with_accessor_guards",
]


class SynthesisError(RuntimeError):
    """A rule file did not have the shape this module requires.

    Raised rather than returning the text unchanged: a silent no-op here means
    the accessor the customer configured is quietly ignored, which is the exact
    failure this module exists to remove.
    """


#: The predicate that pins a seed rule to one callee name. The capture name is
#: shared by every language whose rules match a literal (`ts`, `java`, `go`,
#: `python`, `swift`); Kotlin uses the same capture for its sentinel, which is
#: why the sentinel value is excluded below.
_SDK_FN_PREDICATE = re.compile(r'\(#eq\? @sdk_fn "([^"]*)"\)')

#: A Kotlin seed rule reads `(#eq? @sdk_fn "@sentinel")`. `@` cannot start an
#: identifier, so this also rejects any other hole that lands in this position.
_HOLE = re.compile(r"\A@")

_RULE_HEADER = "[[rules]]"


@dataclass(frozen=True)
class _Block:
    """One ``[[rules]]`` block, kept as text rather than parsed.

    Text, because the round trip matters: these files carry the reasoning for
    every predicate in them, `tomllib` is read-only, and hand-writing TOML with
    multi-line query strings is exactly where a silent corruption would come
    from. A clone therefore differs from its template in the two lines we
    rewrite and nowhere else, which `test_a_clone_differs_only_in_name_and_callee`
    pins.
    """

    text: str
    name: str
    callee: str

    @property
    def shape(self) -> str:
        """The block with its identity removed, for de-duplicating templates.

        TypeScript has two seed rules that are identical apart from the callee
        name, so one template covers both. Python has two that are genuinely
        different shapes and both need cloning. Comparing shape rather than
        counting blocks is what gets both right.

        Comments and the name line are stripped before comparing. A block runs
        from its own ``[[rules]]`` to the next one, so it carries the comment
        block written for the rule BELOW it -- text that says nothing about
        this rule's shape. Leaving it in made TypeScript's two identical seeds
        look like two shapes and generated a redundant duplicate rule per
        accessor.
        """
        without_name = re.sub(
            r'^name = ".*"$', "", _strip_comments(self.text), flags=re.MULTILINE
        )
        return _SDK_FN_PREDICATE.sub("", without_name)


def _strip_comments(text: str) -> str:
    """Drop whole-line comments and blank lines.

    Only ever used for comparing and trimming, never for what is handed to the
    engine: the rules keep every word of their reasoning.
    """
    return "\n".join(
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _split_blocks(text: str) -> list[str]:
    """Split a rules file on ``[[rules]]``, keeping the preamble at index 0."""
    parts = text.split(f"\n{_RULE_HEADER}\n")
    return [parts[0]] + [f"{_RULE_HEADER}\n{part}" for part in parts[1:]]


#: A ``[[rules]]`` block carrying this is a seed rule that must NOT become an
#: accessor template. Explicit, for the same reason ``accessor-guard: none`` is:
#: a rule that should not be cloned looks, structurally, exactly like one nobody
#: has thought about yet, and inferring the difference would silently drop a
#: template the day a seed grew a filter.
#:
#: ``ts.toml``'s sync-OpenFeature seed carries it. That rule only fires in a file
#: importing ``@openfeature/web-sdk``, which says nothing about a customer's own
#: wrapper — and the ordinary call-shaped seed beside it already clones into a
#: rule matching that wrapper's bare call with no import condition at all. So the
#: clone would be a strictly narrower duplicate of one already generated, plus a
#: gate that reads as meaningful and is not.
#:
#: IT MUST BE WRITTEN INSIDE THE BLOCK, below the ``name`` line. Blocks are split
#: on ``[[rules]]``, so a marker placed in the comment header ABOVE that line
#: lands in the PREVIOUS rule's block and opts out the wrong seed — silently,
#: because the template COUNT is unchanged when the two rules have different
#: shapes. `test_an_opted_out_seed_is_not_cloned_for_an_accessor` asserts on the
#: clones themselves rather than on that count for exactly this reason; it is
#: how the misplacement was found.
_NO_ACCESSOR_TEMPLATE_MARKER = re.compile(
    r"^#\s*accessor-template:\s*none\b", re.MULTILINE
)


def _seed_blocks(blocks: list[str]) -> list[_Block]:
    seeds = []
    for block in blocks[1:]:
        match = _SDK_FN_PREDICATE.search(block)
        if match is None or _HOLE.match(match.group(1)):
            continue
        if _NO_ACCESSOR_TEMPLATE_MARKER.search(block) is not None:
            continue
        name = re.search(r'^name = "(.*)"$', block, flags=re.MULTILINE)
        if name is None:
            raise SynthesisError(f"a [[rules]] block has no name:\n{block[:200]}")
        seeds.append(_Block(text=block, name=name.group(1), callee=match.group(1)))
    return seeds


def _trim_trailing_commentary(block: str) -> str:
    lines = block.splitlines()
    while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
        lines.pop()
    return "\n".join(lines) + "\n"


def generated_rule_names(accessor: str, count: int) -> list[str]:
    """The names a given accessor's clones will carry.

    Exposed so tests and error messages can talk about them without
    re-deriving the scheme. Two rules sharing a name is a hard engine error, so
    the scheme has to be collision-free against both the shipped names and the
    other accessors': shipped names never contain ``__accessor_``.
    """
    return [f"generated__accessor_{accessor}__{index}" for index in range(count)]


def with_accessors(
    rules_text: str,
    accessors: tuple[str, ...],
    *,
    templates_text: str | None = None,
    before_trailing_rules: bool = False,
) -> str:
    """Return ``rules_text`` plus one cloned rule per accessor per shape.

    ``accessors`` empty returns the input unchanged and byte-identical -- the
    historical path stays exactly the historical path.

    ``templates_text`` names a DIFFERENT source for the blocks to clone, for a
    language whose seed rules carry a constraint that must not ride along.
    Python is the only one: its SDK read is generic over the flag's type, so
    its seed anchors a boolean literal as the sole type evidence, and a
    wrapper exists precisely to hide that argument. The templates are cloned
    and then discarded -- only ``rules_text`` and the clones are returned, so a
    template can never itself become an active rule. See
    ``rules/python_accessors.toml``.

    ``before_trailing_rules`` puts the generated block immediately after the
    LAST seed rule instead of at the end of the file. Seed rules fire in
    DECLARATION ORDER, so where the clones land is behaviour, not layout, and
    the two rule-file shapes want different answers:

    * A language file (the default) ends in cleanup rules that are reached by
      an edge from a group, never by matching a callee, so appending is right
      and is what has always shipped.
    * A ``<base>_const.toml`` ends in a CALLEE-AGNOSTIC declaration delete,
      guarded by "no call still takes this constant as its first argument".
      Appended at the end, every clone fired after that guard had already
      looked -- the wrapper's read folded away and the declaration survived,
      leaving a key string that makes every future run report the flag as still
      referenced. Measured on all nine const files while closing #2730.
    """
    if not accessors:
        return rules_text

    source = rules_text if templates_text is None else templates_text
    blocks = _split_blocks(source)
    seeds = _seed_blocks(blocks)
    if not seeds:
        raise SynthesisError(
            "no seed rule with a literal (#eq? @sdk_fn \"...\") predicate; "
            "custom accessors cannot be expressed for this language"
        )

    templates: list[_Block] = []
    seen_shapes: set[str] = set()
    for seed in seeds:
        if seed.shape not in seen_shapes:
            seen_shapes.add(seed.shape)
            templates.append(seed)

    generated = []
    for accessor in accessors:
        names = generated_rule_names(accessor, len(templates))
        for new_name, template in zip(names, templates, strict=True):
            clone = re.sub(
                r'^name = ".*"$',
                f'name = "{new_name}"',
                template.text,
                count=1,
                flags=re.MULTILINE,
            )
            clone = _SDK_FN_PREDICATE.sub(f'(#eq? @sdk_fn "{accessor}")', clone, count=1)
            # A block ends where the next `[[rules]]` begins, so its tail is the
            # comment block written for the rule BELOW it. Carried into a clone
            # appended at the end of the file, that commentary would describe a
            # rule that is not there.
            generated.append(_trim_trailing_commentary(clone))

    rule = "# " + "=" * 73 + "\n"
    banner = (
        "\n" + rule
        + "# GENERATED at run time from the seed rules above, one per name in the\n"
          "# `accessors` input. Not checked in, and not editable: change the input.\n"
        + rule
    )
    head, tail = _split_at_last_seed(rules_text) if before_trailing_rules else (rules_text, "")
    block = head.rstrip("\n") + "\n" + banner + "\n" + "\n".join(generated)
    if not tail:
        return block
    return block.rstrip("\n") + "\n" + rule + "# END GENERATED\n" + rule + "\n" + tail


def _split_at_last_seed(rules_text: str) -> tuple[str, str]:
    """``rules_text`` cut just after its last callee-pinned rule.

    The anchor is the last ``[[rules]]`` block carrying a literal
    ``(#eq? @sdk_fn "...")`` -- not the last block that became a TEMPLATE. The
    two differ for a seed carrying ``accessor-template: none`` and for a
    language whose templates come from a separate file (Python), and in both
    cases what is wanted here is the positional question -- where do the
    callee-pinned rules stop -- rather than the cloning one.

    Falls back to ``(rules_text, "")`` when there is no such block, which puts
    the caller back on the append path. Only reachable for a file with no seed
    at all, and :func:`with_accessors` has already raised for that.
    """
    blocks = _split_blocks(rules_text)
    anchor = None
    for index, block in enumerate(blocks[1:], start=1):
        match = _SDK_FN_PREDICATE.search(block)
        if match is not None and not _HOLE.match(match.group(1)):
            anchor = index
    if anchor is None or anchor == len(blocks) - 1:
        return rules_text, ""
    return "\n".join(blocks[: anchor + 1]), "\n".join(blocks[anchor + 1 :])


#: A ``<base>_const.toml`` whose header carries this declares that it binds no
#: name to a flag READ, so there is no re-binding guard for an accessor to
#: extend. It is an explicit opt-out rather than an inference from the file's
#: shape: "no guard present" is exactly what a TS-shaped file that FORGOT one
#: looks like, and that file must still fail loudly. `csharp_const.toml` carries
#: it because it resolves a flag KEY — the const holds the key string, never the
#: read's result, so no call of any name can be re-bound to it.
#:
#: **IT IS NOT A STATEMENT THAT THE FILE NEEDS NOTHING FROM THIS MODULE**, and
#: reading it that way is exactly what #2730 was. A key-const file has no GUARD
#: to extend and still has SEED RULES TO CLONE: without the clones, a wrapper
#: named in ``accessors`` cannot reach a key hoisted to a constant, and the run
#: reports `no-changes` with nothing saying why. The two questions are separate
#: and are asked by separate markers -- see :data:`_NO_ACCESSOR_CLONE_MARKER`.
_NO_ACCESSOR_GUARD_MARKER = re.compile(r"^#\s*accessor-guard:\s*none\b", re.MULTILINE)


#: A ``<base>_const.toml`` whose header carries this declares that its rules
#: must NOT be cloned per accessor. The other half of the split above.
#:
#: `kt_const.toml` carries it, and is the only file that does. Kotlin resolves a
#: hoisted key in the SENTINEL PRE-PASS rather than in a rule --
#: `kotlin_sentinel._callee_spans` already marks every accessor's call -- so the
#: file ships no callee-keyed seed at all and there is nothing to clone. Written
#: down rather than inferred from "this file has no `@sdk_fn` predicate": that
#: is also what a file whose seed rule was renamed looks like, and
#: :func:`with_accessor_const_rules` must keep failing loudly for that one.
#:
#: FILE-scoped, unlike :data:`_NO_ACCESSOR_TEMPLATE_MARKER`, which opts a single
#: block out of the template set. Deliberately a different word from that
#: marker's: the same string carrying two scopes is how a marker written in the
#: wrong place would silently mean the wrong thing.
_NO_ACCESSOR_CLONE_MARKER = re.compile(r"^#\s*accessor-clones:\s*none\b", re.MULTILINE)


def with_accessor_const_rules(
    const_rules_text: str,
    accessors: tuple[str, ...],
    *,
    templates_text: str | None = None,
) -> str:
    """Return ``const_rules_text`` plus one cloned rule per accessor per shape.

    The KEY-const rules resolve a flag key hoisted to a constant, and every one
    of them is pinned to an SDK callee name by the same
    ``(#eq? @sdk_fn "...")`` predicate the language's own seed rules carry. So
    they clone exactly the same way, and until #2730 nothing cloned them: a
    project that both wrapped the SDK and hoisted its keys -- the two things
    this tool's README recommends -- got a green run, no pull request, and no
    explanation. That is the same silent no-op #2525 and #2671 closed for the
    SDK's own names, one composition step further out.

    **This is only half a fix, and shipping the other half is not optional.**
    `key_const.path_is_safe` withholds the whole const file from any source
    where a surviving reference to the constant is not a read these rules
    remove. Emit the clones without telling that gate about the accessors and
    every such file is withheld, so the clones never run and the feature looks
    exactly as broken as before; tell the gate without emitting the clones and
    the delete rule takes a declaration a live call still reads. The runner
    passes one ``accessors`` tuple to both.

    A file carrying ``accessor-clones: none`` is returned untouched --
    `kt_const.toml`, whose fold is the sentinel pre-pass rather than a rule.

    ``templates_text`` behaves as it does in :func:`with_accessors`, for the
    same language: `python_const_accessors.toml`.
    """
    if not accessors or not const_rules_text:
        return const_rules_text
    if _NO_ACCESSOR_CLONE_MARKER.search(const_rules_text) is not None:
        return const_rules_text
    return with_accessors(
        const_rules_text,
        accessors,
        templates_text=templates_text,
        before_trailing_rules=True,
    )


def with_accessor_guards(const_rules_text: str, accessors: tuple[str, ...]) -> str:
    """Add each accessor to the const rules' re-binding guard.

    That guard is an AND of ``(#not-eq? @callee "...")`` predicates meaning
    "this name is bound to a call whose callee is not a flag read". An accessor
    missing from it is not cosmetic: the const-propagation rules will inline a
    binding they were supposed to leave alone.

    A const-rules file that propagates a flag KEY rather than a flag READ has no
    such guard and needs none; it says so with an ``accessor-guard: none`` line
    and is returned untouched. That line says nothing about whether the file's
    own rules get cloned -- :func:`with_accessor_const_rules` answers that, and
    conflating the two is #2730.
    """
    if not accessors or not const_rules_text:
        return const_rules_text

    if _NO_ACCESSOR_GUARD_MARKER.search(const_rules_text) is not None:
        return const_rules_text

    pattern = re.compile(r'(?P<block>(?:^[ \t]*\(#not-eq\? @callee "[^"]*"\)\n)+)', re.MULTILINE)
    if pattern.search(const_rules_text) is None:
        raise SynthesisError(
            "the const rules carry no (#not-eq? @callee ...) guard to extend; "
            "an accessor added without it would inline a binding it must not"
        )

    def extend(match: re.Match[str]) -> str:
        block = match.group("block")
        indent = re.match(r"[ \t]*", block).group(0)
        existing = set(re.findall(r'\(#not-eq\? @callee "([^"]*)"\)', block))
        additions = "".join(
            f'{indent}(#not-eq? @callee "{name}")\n'
            for name in accessors
            if name not in existing
        )
        return block + additions

    return pattern.sub(extend, const_rules_text)
