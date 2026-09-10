"""The Kotlin sentinel pre-pass — the only language that needs one.

Why it exists
-------------
Piranha's rules cannot tell WHICH flag a Kotlin call reads. Measured against
the engine's own bundled grammar: ``string_literal`` there has **no named
children at all** (``((string_literal (_) @in) @lit)`` returns no match, and
``string_content`` is not even a known node type), so there is nothing to hand
``#eq?``. Capturing the literal whole is useless too, because ``#eq?`` cannot be
given a value containing a ``"`` — it splits on the raw quote and reports
*Wrong number of arguments … Expected 2, got 4*.

So a Kotlin rule can match ``boolVariation(…)`` but not check its key, and a
rule that matched every ``boolVariation`` call would rewrite every OTHER flag's
guard in the file. ``#match?`` is the obvious escape hatch and is banned here
(see ``tests/test_piranha_predicate_nondeterminism.py``).

What it does instead
--------------------
The wheel-installed ``tree-sitter-kotlin`` grammar — a much newer one than the
engine bundles — DOES expose ``string_content``. This module uses it to find
exactly the calls the rules should rewrite, and replaces each one's **callee**
with a unique sentinel identifier:

    client.boolVariation("old-checkout", false)
    -> __ff_cleanup_<hash>("old-checkout", false)

The engine then matches a BARE identifier callee, which it can constrain:
``((call_expression (identifier) @sdk_fn) @call (#eq? @sdk_fn "…"))``.
Measured — that is the one callee shape in the bundled grammar that ``#eq?``
does constrain. ``(navigation_expression (identifier) @f)`` binds the
RECEIVER, not the method, so an ``#eq?`` on it never matches.

.. note::
   As of the polyglot-piranha 0.5.0 fork bump (tree-sitter-kotlin-ng), the
   engine's bundled grammar no longer has opaque string literals either —
   ``string_literal`` now exposes a real ``string_content`` child, and
   ``#eq?`` matches it directly (re-verified against the installed wheel; see
   the header of ``rules/kt.toml`` for the probe and the reasoning). The
   sentinel is kept anyway: it also backs the dead-end-``else if`` refusal
   below and the ``!``-precedence workaround, both proven against this wheel
   grammar, and folding those into direct string-keyed rules is a redesign
   this port deliberately did not take on. See ``rules/kt.toml`` for the full
   argument.

Only the callee is replaced, never the arguments: the key stays in the source
between the pre-pass and the engine, so the intermediate state is as close to
the customer's file as it can be, and the surviving-sentinel check below has
something specific to look for.

The safety contract
-------------------
This is the only place in the tool that writes to the customer's checkout
BEFORE the engine runs, so it owns three guarantees:

1. **Reversible byte-for-byte.** :class:`SentinelPrePass` keeps each file's
   original bytes and restores the whole file, rather than trying to undo
   individual edits. A whole-file restore cannot drift; an edit-level one can.
2. **Nothing is left on disk after a failure.** The restore runs from a
   ``finally``, so an engine panic — which writes nothing itself — cannot leave
   a sentinel-ified file behind.
3. **A surviving sentinel is a refusal, never a diff.** If the engine somehow
   leaves one in its output, that file is refused and rolled back. Shipping a
   PR containing ``__ff_cleanup_…`` would be a syntax-valid file that does not
   compile, and it would carry an identifier no customer can interpret.

The sentinel is derived from the flag key, so it is stable across runs and
identical for every occurrence — which is what makes a single ``#eq?`` rule
enough. Uniqueness per occurrence is deliberately NOT needed, because the undo
is whole-file.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import tree_sitter_kotlin
from tree_sitter import Language, Node, Parser

logger = logging.getLogger(__name__)

#: The Kotlin SDK's boolean read. One name, matched exactly — the Kotlin/Android
#: client SDK exposes `boolVariation(key, defaultValue)` and nothing else that
#: returns a bare Boolean. `stringVariation`/`variationDetail` must not match,
#: and an exact comparison is what guarantees that.
SDK_FUNCTION_NAME = "boolVariation"

#: Names beyond the SDK's own that count as a flag read here. A project that
#: wraps `boolVariation` behind its own function is invisible to the rules
#: otherwise -- see the `accessors` input. Kotlin cannot take these through the
#: rules the way every other language does, because a Kotlin rule cannot read a
#: string literal and so cannot tell one flag's guard from another's; the name
#: check therefore lives here, in the pass that CAN read the key.
DEFAULT_ACCESSORS: tuple[str, ...] = ()

#: Prefix for the generated identifier. Deliberately double-underscored and
#: long: it has to be something no real Kotlin codebase contains, because the
#: pre-pass refuses to run over a file that already mentions it.
_SENTINEL_PREFIX = "__ff_cleanup_"


class SentinelCollisionError(RuntimeError):
    """The generated sentinel already appears in the source.

    Astronomically unlikely (it is a SHA-256 prefix behind a reserved-looking
    name) but not impossible — the customer could be running this tool's own
    output back through it, or vendoring a fixture. Raised rather than worked
    around: if the name is already there, the post-run "did a sentinel survive?"
    check cannot tell our identifier from theirs, and that check is what stops a
    broken file reaching a pull request.
    """


def sentinel_for(flag_key: str) -> str:
    """The identifier that stands in for a read of ``flag_key``.

    Derived from the key rather than random so a run is reproducible and a
    failure can be reasoned about from the flag alone. Hex only, so the result
    is always a valid Kotlin identifier no matter what the key looks like.
    """
    digest = hashlib.sha256(flag_key.encode("utf-8")).hexdigest()[:16]
    return f"{_SENTINEL_PREFIX}{digest}"


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_kotlin.language()))


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _method_name(callee: Node) -> str | None:
    """The method a call's callee names, or ``None`` if it names none.

    ``boolVariation(…)`` gives an ``identifier`` callee; ``client.boolVariation``
    and ``client.flags.boolVariation`` give a ``navigation_expression`` whose
    LAST named child is the method. Anything else (a call returning a lambda, a
    parenthesised expression) is not a shape this tool rewrites.
    """
    if callee.type == "identifier":
        return callee.text.decode("utf-8", "replace")
    if callee.type == "navigation_expression" and callee.named_children:
        last = callee.named_children[-1]
        if last.type == "identifier":
            return last.text.decode("utf-8", "replace")
    return None


def _is_optional_call(callee: Node) -> bool:
    """Whether the callee reaches its method through ``?.``.

    Named for what it DETECTS, not for the verdict. "Safe call" is Kotlin's own
    term for ``?.``, so ``_is_safe_call`` returning True at a call site that
    reads ``or _is_safe_call(callee): continue`` said the opposite of what it
    meant — an easy inversion for whoever edits this next.

    ``client?.boolVariation("KEY", false)`` has type ``Boolean?``, not
    ``Boolean``, so folding it to ``true`` drops the null case — the same reason
    ``ts.toml`` skips an optional call. Detected on the anonymous ``?.`` token
    rather than by searching the text, so a ``?`` inside the receiver cannot be
    mistaken for one.
    """
    return any(
        child.type == "?." for child in callee.children if not child.is_named
    )


def _callee_start(callee: Node) -> int:
    """The byte where the callee really begins, skipping mis-parsed ``!``.

    The wheel grammar gets Kotlin's precedence wrong for a negated read. ``!``
    binds looser than ``.`` and than a call, so ``!client.boolVariation(k, d)``
    is ``!(client.boolVariation(k, d))`` — but the tree comes out as

        call_expression        '!client.boolVariation("gate", false)'
          navigation_expression  '!client.boolVariation'
            unary_expression       '!client'     <-- the `!` bound to the RECEIVER
              ! / identifier client
            . / identifier boolVariation
          value_arguments

    so ``callee.start_byte`` points at the ``!``. Replacing that span with the
    sentinel therefore **deleted the negation**, and every negated Kotlin read
    folded to the opposite of its real value: with the flag serving ``true``,
    ``if (!read) { dead } else { live }`` kept ``dead``. It compiled, it looked
    plausible, and it deleted the branch that runs — found by hand-testing a
    real application, because no fixture here covered ``!`` for Kotlin.

    Returning the operand's start leaves the ``!`` in the source, where the
    engine's own boolean-literal cleanup folds ``!true`` correctly (verified for
    both treatments across `if`, `if`/`else`, an `if`/`else` expression and a
    `val` binding). Nested ``!!read`` is handled by the same descent.
    """
    node = callee
    while node.children:
        first = node.children[0]
        if first.type == "!":
            if len(node.children) < 2:
                break  # pragma: no cover - `!` with no operand does not parse
            node = node.children[1]
            continue
        if first.start_byte != node.start_byte:
            break
        node = first
    return node.start_byte


def _literal_key(argument: Node) -> str | None:
    """The plain-string key an argument holds, or ``None``.

    Accepts both ``boolVariation("KEY", …)`` and the named form
    ``boolVariation(key = "KEY", …)`` by looking at the argument's LAST named
    child, which is the value in both.

    A string with an interpolation (``"KEY-${suffix}"``) carries more than one
    child and is rejected: its runtime value is not the key, and rewriting it
    would fold a guard that reads a different flag on every call.
    """
    if not argument.named_children:
        return None
    value = argument.named_children[-1]
    if value.type != "string_literal":
        return None
    children = value.named_children
    if len(children) != 1 or children[0].type != "string_content":
        return None
    return children[0].text.decode("utf-8", "replace")


def const_val_keys(root: Node) -> dict[str, str]:
    """``{bound name: key string}`` for every ``const val`` holding a plain string.

    KEY-const indirection for Kotlin (#2671). `boolVariation(FLAG_KEY, ...)` is
    the conventional shape, and the sentinel pre-pass could not see through it:
    `_literal_key` reads a string literal, so an identifier argument simply did
    not match and the whole file reported `no-changes`. Resolution belongs HERE
    rather than in a rule, because Kotlin's rules match a sentinel and never see
    the key at all.

    ``const`` is REQUIRED and is not a formality: in this grammar a plain `val`
    and a `var` are shaped IDENTICALLY — the `val`/`var` keyword is an anonymous
    node — so without the modifier check a mutable `var` would be resolved as
    though it were a constant. `const val` is Kotlin's genuine compile-time
    constant, so requiring it also makes the value safe to resolve at all.

    Only names bound EXACTLY ONCE in the file are returned: a second binding
    means a reference could resolve to something other than the declaration read
    here, which is the wrong-branch fold every other language's Gate 2 refuses.
    `key_const` enforces the same rule for the other ten.
    """
    found: dict[str, str] = {}
    seen: list[str] = []
    for name, _declaration, _name_node, key in const_val_declarations(root):
        seen.append(name)
        found[name] = key
    for name in seen:
        if seen.count(name) != 1:
            found.pop(name, None)
    return found


def const_val_declarations(root: Node) -> list[tuple[str, Node, Node, str]]:
    """``(name, property_declaration, name node, key)`` for every ``const val``.

    The single reading of what counts as a resolvable Kotlin constant, shared by
    :func:`const_val_keys` (which decides what the pre-pass rewrites) and
    `key_const._kotlin_key_consts` (which decides whether the file is safe to
    rewrite at all). They MUST agree: a declaration one accepts and the other
    does not is either an ungated rewrite or a stranded reference.

    Unlike :func:`const_val_keys` this does NOT drop a name bound twice -- the
    caller decides what to do about that, and `key_const` needs to SEE the
    duplicate in order to refuse the file.
    """
    found: list[tuple[str, Node, Node, str]] = []
    for node in _walk(root):
        if node.type != "property_declaration":
            continue
        modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
        if modifiers is None or not any(
            c.type == "property_modifier" and c.text.decode("utf-8", "replace") == "const"
            for c in modifiers.named_children
        ):
            continue
        declaration = next(
            (c for c in node.named_children if c.type == "variable_declaration"), None
        )
        literal = next(
            (c for c in node.named_children if c.type == "string_literal"), None
        )
        if declaration is None or literal is None:
            continue
        name_node = next(
            (c for c in declaration.named_children if c.type == "identifier"), None
        )
        children = literal.named_children
        # An interpolated key is not a key; see `_literal_key`.
        if (
            name_node is None
            or len(children) != 1
            or children[0].type != "string_content"
        ):
            continue
        found.append(
            (
                name_node.text.decode("utf-8", "replace"),
                node,
                name_node,
                children[0].text.decode("utf-8", "replace"),
            )
        )
    return found


def _resolved_key(argument: Node, consts: dict[str, str]) -> str | None:
    """The key an argument holds, resolving a ``const val`` name through ``consts``.

    Mirrors `_literal_key`'s handling of the named form (`key = FLAG_KEY`) by
    looking at the argument's LAST named child.
    """
    literal = _literal_key(argument)
    if literal is not None:
        return literal
    if not argument.named_children:
        return None
    value = argument.named_children[-1]
    if value.type != "simple_identifier" and value.type != "identifier":
        return None
    return consts.get(value.text.decode("utf-8", "replace"))


def _callee_spans(
    source: str,
    flag_key: str,
    accessors: tuple[str, ...] = DEFAULT_ACCESSORS,
    resolve_consts: bool = True,
) -> list[tuple[int, int]]:
    """Byte ranges of every callee this tool should replace, last first.

    Returned in reverse document order so the caller can splice them in without
    recomputing offsets.
    """
    tree = _parser().parse(source.encode("utf-8"))
    # `resolve_consts` is Gate 2's verdict for THIS file, computed by
    # `piranha_runner` before the pre-pass runs. It has to be honoured here
    # rather than at the rules: for Kotlin the pre-pass IS the fold, so a gate
    # applied only to `kt_const.toml` would withhold the declaration delete
    # while still folding every read — the exact half-finished rewrite the gate
    # exists to prevent.
    consts = const_val_keys(tree.root_node) if resolve_consts else {}
    spans: list[tuple[int, int]] = []
    for node in _walk(tree.root_node):
        if node.type != "call_expression" or not node.named_children:
            continue
        callee = node.named_children[0]
        if _method_name(callee) not in {SDK_FUNCTION_NAME, *accessors} or _is_optional_call(
            callee
        ):
            continue
        arguments = [c for c in node.named_children if c.type == "value_arguments"]
        if not arguments or not arguments[0].named_children:
            continue
        if _resolved_key(arguments[0].named_children[0], consts) != flag_key:
            continue
        # NOT `callee.start_byte` — see `_callee_start`. That spelling swallowed
        # a leading `!` into the sentinel and inverted every negated read.
        spans.append((_callee_start(callee), callee.end_byte))
    return sorted(spans, reverse=True)


def rewrite(
    source: str,
    flag_key: str,
    sentinel: str,
    accessors: tuple[str, ...] = DEFAULT_ACCESSORS,
    resolve_consts: bool = True,
) -> str | None:
    """``source`` with every matching callee replaced by ``sentinel``.

    ``None`` when the file holds no read of ``flag_key`` — the caller then
    leaves it untouched, which keeps a file that cannot change out of the
    restore bookkeeping entirely.

    Raises :class:`SentinelCollisionError` if ``sentinel`` already occurs.
    """
    if sentinel in source:
        raise SentinelCollisionError(
            f"{sentinel!r} already appears in this source, so a rewrite could "
            "not be told apart from what was already there"
        )
    spans = _callee_spans(source, flag_key, accessors, resolve_consts)
    if not spans:
        return None
    raw = source.encode("utf-8")
    for start, end in spans:
        raw = raw[:start] + sentinel.encode("ascii") + raw[end:]
    return raw.decode("utf-8")


@dataclass(frozen=True)
class Rewrite:
    """One file's before/after, with ``before`` being the CUSTOMER's bytes.

    The engine reports the sentinel-ified text as a summary's
    ``original_content``, which would put ``__ff_cleanup_…`` on the removed
    side of the diff the customer reads — and would make ``_restore`` write a
    sentinel back to disk. :meth:`SentinelPrePass.rebase` swaps in the real
    original, and the field names match the engine's summaries so everything
    downstream is indifferent to which it got.
    """

    path: str
    original_content: str
    content: str


@dataclass
class SentinelPrePass:
    """Sentinel-ifies a set of files and can put every one of them back."""

    flag_key: str
    sentinel: str
    #: Extra callee names that count as a flag read. See `DEFAULT_ACCESSORS`.
    accessors: tuple[str, ...] = DEFAULT_ACCESSORS
    #: Paths Gate 2 cleared for KEY-const resolution. A path outside this set
    #: still gets literal-key sentinel-ification -- that has never needed a gate
    #: -- but its `const val` names are not resolved, so a file the gate refused
    #: is left exactly as it was for the const path. See
    #: `key_const` and `rules/kt_const.toml`.
    const_key_paths: frozenset[str] = frozenset()

    #: path -> the file's text before this pre-pass touched it.
    originals: dict[str, str] = field(default_factory=dict)

    #: The file that raised, if one did. The refusal must name the file the
    #: customer has to act on; reporting every candidate — which is what
    #: falling back to the whole path list did when the FIRST file raised and
    #: `originals` was still empty — points them at files that have no read of
    #: the flag in them at all.
    failed_path: str | None = None

    def apply(self, paths: list[str]) -> None:
        """Rewrite each of ``paths`` that reads the flag, remembering the original."""
        for path in paths:
            try:
                source = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):  # pragma: no cover - filtered earlier
                continue
            try:
                rewritten = rewrite(
                    source,
                    self.flag_key,
                    self.sentinel,
                    self.accessors,
                    resolve_consts=path in self.const_key_paths,
                )
            except SentinelCollisionError:
                self.failed_path = path
                raise
            if rewritten is None:
                continue
            self.originals[path] = source
            Path(path).write_text(rewritten, encoding="utf-8")

    def restore_all(self) -> None:
        """Put every file this pre-pass wrote back to its original bytes.

        For the paths that failed outright: the engine panics BEFORE writing
        anything, so at that point the only edits on disk are this pre-pass's
        own and every one of them has to go.
        """
        for path, source in self.originals.items():
            Path(path).write_text(source, encoding="utf-8")

    def rebase(self, summaries) -> list[Rewrite]:
        """Engine summaries with the sentinel-ified 'before' swapped for the real one.

        Also fills in the files the engine returned no summary for. A file this
        pre-pass rewrote and the engine then ignored is still sitting on disk
        with a sentinel in it, and it must reach the Gate 1 refusal path rather
        than be silently left there — so it is emitted as a ``Rewrite`` whose
        ``content`` is what is actually on disk.
        """
        rebased = [
            Rewrite(s.path, self.originals.get(s.path, s.original_content), s.content)
            for s in summaries
        ]
        seen = {r.path for r in rebased}
        for path, source in self.originals.items():
            if path in seen:
                continue
            try:
                current = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):  # pragma: no cover - just written
                current = source
            rebased.append(Rewrite(path, source, current))
        return rebased

    def survived(self, content: str) -> bool:
        """Whether ``content`` still holds the sentinel.

        A true answer means the engine did not consume a read this pre-pass
        marked, which is never an acceptable thing to ship: the file would
        carry an identifier that does not compile and that no customer can
        interpret. The caller turns it into a refusal.
        """
        return self.sentinel in content
