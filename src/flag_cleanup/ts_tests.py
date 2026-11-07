"""Delete a JavaScript test whose subject is the branch the fold removed.

A test that pins a flag to one value says, with that pin, which branch it
exercises: ``renderProfile({ 'k': false })`` renders the off branch. When the
fold keeps the ON branch, that pin names code that is about to stop existing,
and one of two things is true of the test around it:

* **It is ABOUT the removed branch** — ``it('hides the section when the flag
  is off', …)``. Its assertion describes behaviour the code can no longer
  produce, so it fails whatever happens to the pin. Stripping the pin leaves a
  red build; keeping it leaves the same red build plus a residual reference.
  The only correct edit is to delete the test.
* **It merely sets the flag on the way to something else** — ``it('shows the
  disable button when the user is enrolled even if the flag is off', …)``, or
  an a11y check that happens to pass ``false``. The flag is noise there, and
  the pin is ordinary residue to strip.

Nothing structural tells those apart. The TITLE does, and in JavaScript every
test has one, which is why this lives in the TypeScript family only. A pin's
nearest enclosing ``it``/``test``/``describe`` (or their ``.only``/``.skip``
and ``x``/``f`` spellings) whose title names the flag in the removed state —
"the flag is off", "with the feature disabled" — is deleted whole. A title
that hedges with "even", "regardless" or "whether" is about something that
holds in both states and is kept. Everything else is left to
``rules/ts_entries.toml``, which strips the pin like any other entry.

A test that never sets the flag gets its default, so when the registry
declares the removed branch as that default (``'k': { default: false }``, see
:func:`registry_defaults`), the default is the pin. Nothing in such a test
names the flag, so its title has to, by a word of the key: "when the GitHub
flag is off".

This is a heuristic, and the pull request names every test it deleted, with
its title, so a reviewer can check each call. Its errors are bounded on both
sides: a test it should have deleted and did not fails loudly in CI, and one
it deleted wrongly is in the diff and in the body.

The work is split across the engine, deliberately. The tests are CHOSEN from
the customer's own bytes, before the transform: afterwards the pin has been
stripped and there is nothing left to find. They are DELETED from the
engine's output, after it, by title and position among same-titled tests —
the engine rewrites the code inside a test but never a title, and deleting
after the engine keeps this inside the same window as every other text pass,
under Gate 1 and the same all-or-nothing rollback.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup.ts_param_pins import pinned_calls
from flag_cleanup.ts_syntax import _parse, _walk

#: The callees that name a test or a group of tests.
_TEST_CALLEES = frozenset(
    {"it", "test", "describe", "fit", "xit", "ftest", "xtest", "fdescribe", "xdescribe"}
)

#: How a title names the flag. The flag key itself counts too; see
#: :func:`_removed_state_pattern`.
_SUBJECTS = r"flags?|features?|toggles?|gates?"

#: The words for each state, keyed by the literal of the branch REMOVED.
_STATES = {
    "false": r"off|disabled|false|turned\s+off|not\s+enabled",
    "true": r"on|enabled|true|turned\s+on",
}

#: A title using one of these describes behaviour that holds in BOTH states —
#: "even when the flag is off", "regardless of the flag" — so the test is not
#: about the removed branch, and it is kept.
#: The conjunctions an `even with` clause can leave hanging. Lowercase only:
#: `AND` / `OR` in capitals is a logic word in the title, not a connective.
_CONNECTIVES = ("and", "or", "not", "but", "nor", "then", "yet", "also", "so")
_DANGLING = re.compile(
    r"\b(?:"
    + "|".join(f"{w}|{w.capitalize()}" for w in _CONNECTIVES)
    + r")[\s,.]*$"
)

#: A qualifier whose condition was the clause just dropped: "renders the banner
#: only when the flag is on" must not become "renders the banner only".
_QUALIFIERS = re.compile(
    r"\b(?:only|except|unless|just|solely|exclusively|also|even|too|first|as\s+well)[\s,.]*$", re.IGNORECASE
)

#: A result ending in one of these has lost its second half.
_DANGLING_PUNCTUATION = re.compile(r"[-\u2014\u2013;,:]\s*$")

#: A matcher that stores its expectation outside the file, keyed by the test's
#: title. The inline variants live in the source and are fine.
_STORED_SNAPSHOT = re.compile(
    r"\b(?:toMatch(?!Inline)\w*Snapshot|toThrowError(?!MatchingInline)\w*Snapshot|toHaveScreenshot|toMatchScreenshot)\b"
)

_HEDGES = re.compile(r"\b(even|regardless|irrespective|whether|either)\b", re.IGNORECASE)


#: The languages this pass runs for: the ones parsed with the TypeScript
#: grammars, whose entry rules strip every pin rather than declining one.
_LANGUAGES = frozenset({"ts", "tsx", "js"})


def covers(language: str) -> bool:
    """Whether ``language``'s pins are resolved here rather than declined."""
    return language in _LANGUAGES


@dataclass(frozen=True)
class PinnedTest:
    """A test chosen for deletion, by the identity that survives the engine.

    ``ordinal`` counts earlier tests in the file with the same callee text and
    the same title, so two ``it('works')`` blocks are never confused.
    """

    callee: str
    title: str
    ordinal: int


@dataclass(frozen=True)
class FlagDefault:
    """What a run's registries say about the flag being removed.

    ``literal`` is the default every registry entry for the key agrees on, or
    ``None`` when there is none or they disagree. ``siblings`` are the OTHER
    flag keys the run knows of: the project's, and every key beside this one
    in a registry. A title word one of them shares names no flag in
    particular. See :func:`find_pinned_tests`.
    """

    literal: str | None = None
    siblings: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RegistryEntry:
    """One registry entry declaring a boolean default for a key."""

    literal: str
    #: The entry's text, on one line.
    entry: str
    #: The keys of the other entries in the same registry object.
    siblings: frozenset[str]


def find_pinned_tests(
    source: str,
    language: str,
    flag_key: str,
    untreated: str,
    default: FlagDefault = FlagDefault(),
) -> list[PinnedTest]:
    """Every test in ``source`` to delete because it exercises the removed branch.

    ``untreated`` is the literal of the branch the fold DELETES, as the rules
    spell it. ``default`` is what the run's registries say about the flag (see
    :func:`registry_defaults`). A test enclosing another chosen one is chosen
    instead of it, so nothing is deleted twice.
    """
    data = source.encode("utf-8")
    root = _parse(source, language)
    pattern = _removed_state_pattern(flag_key, untreated)
    titled = _titled_tests(root, data)
    index = {id_: n for n, id_ in enumerate(node.id for node, _, _ in titled)}
    # A helper's call pins the flag as plainly as a literal does when the
    # helper's parameter is proven to feed the override. See
    # :mod:`flag_cleanup.ts_param_pins`.
    calls = pinned_calls(source, language, flag_key)

    def pins(node: Node, value: str) -> bool:
        return _pins(node, data, flag_key, value) or (
            calls.get((node.start_byte, node.end_byte)) == value
            and node.type == "call_expression"
        )

    chosen: dict[int, Node] = {}
    for pair in _walk(root):
        if not pins(pair, untreated):
            continue
        ancestor = pair.parent
        while ancestor is not None:
            if ancestor.id in index:
                _node, _callee, title = titled[index[ancestor.id]]
                if pattern.search(title) and not _HEDGES.search(title):
                    chosen[ancestor.id] = ancestor
                    break
            ancestor = ancestor.parent

    # A test can exercise the removed branch WITHOUT a pin, by relying on the
    # flag's default: `it('hides the entry when my-flag is off (default)', …)`
    # with no override at all. There is no pin to anchor on, so the title alone
    # has to carry it — and the bar is raised to match: the title must name
    # the flag by its KEY, not just say "the flag", and the test must not pin
    # the key to the KEPT branch either, which would contradict its own title.
    keyed = _removed_state_pattern(flag_key, untreated, generic=False)
    treated = "true" if untreated == "false" else "false"
    for node, _callee, title in titled:
        if not keyed.search(title) or _HEDGES.search(title):
            continue
        if any(pins(pair, treated) for pair in _walk(node)):
            continue
        chosen[node.id] = node

    # A test can also reach the removed branch by never setting the flag at
    # all, when the registry's default IS that branch: `it('hides the link
    # when the GitHub flag is off', …)` rendering with no override. The default
    # stands in for the pin, so it anchors only when it equals the removed
    # literal and the test gives the key no value of its own. "The flag" alone
    # is too weak here, since a test file can be about several flags, so the
    # title has to qualify its subject with a word of the key, and not the
    # key's leading word: that is usually a namespace other flags share
    # (`web-`), and "the web flag" names none of them in particular. Nor does
    # a word another flag's key shares ("the login feature" beside
    # `web-social-login-google`), or a generic one ("the new feature"). A test
    # that sets another flag, or sits under a `beforeEach` that sets this one,
    # is left alone: it is not plainly about this flag left at its default.
    if default.literal == untreated:
        qualified = _qualified_removed_state_pattern(flag_key, untreated, default.siblings)
        setup = _setup_scopes(root, data, flag_key)
        for node, _callee, title in titled:
            if qualified is None or not qualified.search(title) or _HEDGES.search(title):
                continue
            if any(_contains(scope, node) for scope in setup):
                continue
            if any(
                _sets_key(inner, data, flag_key)
                or any(_sets_key(inner, data, key) for key in default.siblings)
                or (inner.start_byte, inner.end_byte) in calls
                for inner in _walk(node)
            ):
                continue
            chosen[node.id] = node

    outermost = [
        node
        for node in chosen.values()
        if not any(_contains(other, node) for other in chosen.values() if other is not node)
    ]
    return [_identity(node, titled) for node in sorted(outermost, key=lambda n: n.start_byte)]


def delete_tests(source: str, language: str, tests: list[PinnedTest]) -> tuple[str, list[str]]:
    """``source`` without ``tests``, and the title of each test it deleted.

    A test that is not found, or whose statement shares a line with other
    code, is left in place and not reported: its pin was stripped by the
    rules, and its assertion then fails in CI, which is where a reviewer will
    see it.
    """
    if not tests:
        return source, []
    data = source.encode("utf-8")
    titled = _titled_tests(_parse(source, language), data)
    wanted = set(tests)
    spans: list[tuple[int, int]] = []
    deleted: list[str] = []
    for node, _callee, title in titled:
        if _identity(node, titled) not in wanted:
            continue
        statement = node.parent
        if statement is None or statement.type != "expression_statement":
            continue
        span = _own_lines(statement, data)
        if span is None:
            continue
        spans.append(span)
        deleted.append(title)
    for start, end in sorted(spans, reverse=True):
        data = data[:start] + data[end:]
    return data.decode("utf-8"), deleted


#: The ``bindings`` kind of a row naming a title :func:`rename_kept_titles`
#: rewrote. Its text is the old title and the new one, separated by a newline,
#: which no title it rewrites contains.
RENAMED_TEST = "renamed test"


def rename_kept_titles(
    source: str,
    language: str,
    flag_key: str,
    kept: str,
    has_stored_snapshot: Callable[[], bool] | bool = False,
    before: str | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """``source`` with the flag's condition dropped from every title naming the
    KEPT branch, and ``(old, new)`` for each title it rewrote.

    ``kept`` is the literal of the branch the fold keeps. A test titled
    ``renders the checklist when the my-flag flag is on`` survives a removal
    that keeps the on branch, and its title then describes a condition that no
    longer exists. It becomes ``renders the checklist``; ``… when the my-flag
    flag is on and a project exists`` becomes ``… when a project exists``.

    The bar is set above the deletion's, because this edits text a person
    wrote rather than reading it: the title must spell the KEY, never just
    "the flag"; the clause must be one of a fixed set of shapes (see
    :func:`_kept_clauses`); a hedged title is left alone; and the result must
    name neither the key nor the removed state, and must not be empty. Any
    other title is left exactly as it was, and reported as it was before. A
    title is only a string, so a rewrite cannot break the build, and each one
    is named in the pull request.

    ``before`` is the file as it stood before the run. With it, a title that
    does not spell the key but ends in ``even with the gate on`` (or ``when the
    flag is on``) is rewritten too, when its OWN test pinned this flag to the
    kept value there and pinned no other flag: the "gate" is then this flag,
    and the entry that made the phrase true was stripped. See
    :func:`reworded_pinned_title`.
    """
    # Stored snapshots and screenshots are keyed by the full test name, so a
    # rename orphans them wherever the assertion sits (a helper, an
    # `afterEach`, a nested describe). A file that uses one is not renamed at
    # all, and neither is one the repository holds a snapshot file for (a
    # helper imported from another module).
    if has_stored_snapshot is True or _STORED_SNAPSHOT.search(source) or kept not in _STATES:
        return source, []
    # Asked lazily: looking for the repository's snapshot files walks the tree,
    # which only matters once a title could actually be renamed.
    checked = not callable(has_stored_snapshot)
    data = source.encode("utf-8")
    root = _parse(source, language)
    edits: list[tuple[int, int, bytes]] = []
    renamed: list[tuple[str, str]] = []
    # `it.each(rows)('title', cb)` is admitted: dropping a clause that names the
    # removed flag is true of every row, whatever the rows hold. The DELETION
    # does not admit it, because a title can describe one row and not another.
    titled = _titled_tests(root, data, parametrised=True)
    # Every test and describe title in the FILE, by decoded value and with
    # placeholders as wildcards. Unique leaf titles imply unique full names
    # (except across describe titles that contain spaces, which this accepts), so a rename is refused when its new
    # title could equal any other title here, existing or already produced.
    patterns = _title_patterns(root, data)
    pinned_titles = _pinned_titles(before, language, flag_key, kept) if before else frozenset()
    after_counts = Counter(title for _n, _c, title in titled)
    for node, _callee, title in titled:
        arguments = node.child_by_field_name("arguments")
        if arguments is None or not hands_callback(arguments):
            continue
        literal = arguments.named_children[0]
        raw = data[literal.start_byte + 1 : literal.end_byte - 1].decode("utf-8")
        new = reworded_title(title, flag_key, kept) if raw == title else None
        if new is None and raw == title and title in pinned_titles and after_counts[title] == 1:
            new = reworded_pinned_title(title, flag_key, kept)
        if new is None:
            continue
        if not checked:
            checked = True
            if has_stored_snapshot():  # type: ignore[operator]
                return source, []
        candidate = _tokens(_decode(new))
        if any(
            id_ != literal.id and _could_match(candidate, other)
            for id_, other in patterns.items()
        ):
            continue
        patterns[literal.id] = candidate
        edits.append((literal.start_byte + 1, literal.end_byte - 1, new.encode("utf-8")))
        renamed.append((title, new))
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text + data[end:]
    return data.decode("utf-8"), renamed


#: Words that make a call, a JSX attribute or a name "about flags". A call
#: named with one of them must carry nothing but this flag's own stripped pin.
_FLAG_WORDS = frozenset(
    {"flag", "flags", "feature", "features", "toggle", "toggles", "gate", "gates", "override", "overrides"}
)
#: Words of a call that can set state a flag read depends on.
_STATEFUL = _FLAG_WORDS | frozenset(
    {"set", "mock", "return", "value", "spy", "stub", "use", "enable", "disable", "enabled", "disabled",
     "configure", "config", "init", "initialize", "setup", "store", "state", "context"}
)
_HOOKS = frozenset({"beforeEach", "beforeAll", "afterEach", "afterAll"})
_FUNCTIONS = ("arrow_function", "function_expression", "function", "function_declaration")


def _words(text: str) -> list[str]:
    """``renderFlagRow`` -> ``["render", "flag", "row"]``."""
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", text)]


def _callee_words(call: Node, data: bytes) -> list[str]:
    callee = call.child_by_field_name("function") or call.child_by_field_name("constructor")
    if callee is not None and callee.type == "member_expression":
        callee = callee.child_by_field_name("property")
    return _words(_text(callee, data)) if callee is not None else []


def _plain(node: Node) -> list[Node]:
    return [c for c in node.named_children if c.type != "comment"]


def _only_pair(obj: Node, allowed: set[int]) -> bool:
    """``obj`` is ``{}`` or holds exactly one pair, one of ``allowed``."""
    if obj.type != "object":
        return False
    members = _plain(obj)
    return not members or (len(members) == 1 and members[0].id in allowed)


def _boolean_like(node: Node, data: bytes) -> bool:
    """``true``/``false``, ``!<literal>`` or the strings ``'true'``/``'false'``."""
    if node.type in ("true", "false"):
        return True
    if node.type == "parenthesized_expression" and _plain(node):
        return _boolean_like(_plain(node)[0], data)
    if node.type == "unary_expression":
        operand = node.child_by_field_name("argument")
        op = node.child_by_field_name("operator")
        return (
            op is not None
            and _text(op, data) == "!"
            and operand is not None
            and (operand.type in ("true", "false", "number") or _boolean_like(operand, data))
        )
    if node.type == "string":
        return _literal_text(node, data) in ("true", "false")
    return False


#: Names a test file may use without declaring.
_GLOBALS = frozenset({"undefined", "NaN", "Infinity", "window", "document", "globalThis", "expect", "vi", "jest",
                      "console", "process", "Math", "JSON", "Date", "Promise", "Object", "Array"})


class _File:
    """What the whole file says about names: bindings and same-file helpers."""

    def __init__(self, root: Node, data: bytes) -> None:
        self.data = data
        self.bad_bindings: dict[str, str] = {}  # name -> "boolean" | "object"
        self.local_functions: set[str] = set()
        self.imported: set[str] = set()
        self.module_state = False
        #: Every name the file binds: declarations, parameters, imports.
        self.declared: set[str] = set(_GLOBALS)
        for node in _walk(root):
            if node.type == "import_statement":
                for inner in _walk(node):
                    if inner.type == "identifier":
                        self.imported.add(_text(inner, data))
            if node.type in ("formal_parameters", "import_clause", "namespace_import", "import_specifier",
                             "catch_clause"):
                for inner in _walk(node):
                    if inner.type in ("identifier", "shorthand_property_identifier_pattern"):
                        self.declared.add(_text(inner, data))
            elif node.type in ("function_declaration", "class_declaration", "function_expression"):
                name = node.child_by_field_name("name")
                if name is not None:
                    self.declared.add(_text(name, data))
            elif node.type == "arrow_function":
                param = node.child_by_field_name("parameter")
                if param is not None:
                    self.declared.add(_text(param, data))
            if node.type == "variable_declarator":
                target = node.child_by_field_name("name")
                if target is not None:
                    for inner in _walk(target):
                        if inner.type in ("identifier", "shorthand_property_identifier_pattern"):
                            self.declared.add(_text(inner, data))
            if node.type == "variable_declarator":
                name = node.child_by_field_name("name")
                value = node.child_by_field_name("value")
                if name is None or name.type != "identifier" or value is None:
                    continue
                text = _text(name, data)
                if value.type in _FUNCTIONS:
                    self.local_functions.add(text)
                elif _boolean_like(value, data):
                    self.bad_bindings[text] = "boolean"
                elif value.type == "object":
                    self.bad_bindings.setdefault(text, "object")
            elif node.type == "function_declaration":
                name = node.child_by_field_name("name")
                if name is not None:
                    self.local_functions.add(_text(name, data))
            elif node.type == "call_expression":
                callee = node.child_by_field_name("function")
                text = _text(callee, data) if callee is not None else ""
                if text in ("vi.hoisted", "jest.hoisted"):
                    self.module_state = True


def _is_hook(call: Node, data: bytes) -> bool:
    callee = call.child_by_field_name("function")
    return callee is not None and _text(callee, data).split(".")[-1] in _HOOKS


def _is_describe(node: Node, data: bytes) -> bool:
    if node.type != "call_expression":
        return False
    callee = node.child_by_field_name("function")
    return callee is not None and "describe" in _text(callee, data).split(".")[0]


def _clean_scope(
    scope: Node,
    file: _File,
    pairs: dict[int, Node],
    calls: dict[int, Node],
    skipped: set[int],
    flag_key: str,
    *,
    helper: bool = False,
) -> bool:
    """Whether everything in ``scope`` that could set a flag is accounted for.

    ``pairs`` are this flag's own stripped pins and ``calls`` the helper calls
    that pinned it. In a STRICT scope (the test, its rows, its hooks) every
    boolean-like value must sit inside one of them, and there may be no object
    spread. In a ``helper`` body, which is full of ordinary options
    (``retry: false``), a boolean-like only matters where it can set state: as
    an argument of a call named for flags or for setting. In both, a call or
    JSX attribute named for flags may carry nothing but this pin, the object
    holding this flag's pair holds only that pair, and a name bound anywhere
    in the file to a boolean-like value (or, as an argument, an object) is
    refused.
    """
    data = file.data
    accounted: list[Node] = [*pairs.values(), *calls.values()]
    allowed = set(pairs)
    if helper:
        # In a helper the pin is the pair keyed by this flag, whatever feeds it.
        allowed |= {n.id for n in _walk(scope) if n.type == "pair" and _keyed_by(n, data, flag_key)}
    in_accounted = lambda n: any(_contains(a, n) for a in accounted)  # noqa: E731
    params: set[str] = set()
    if helper:
        for n in _walk(scope):
            if n.type in ("identifier", "shorthand_property_identifier_pattern") and n.parent is not None and (
                n.parent.type in ("required_parameter", "optional_parameter", "formal_parameters", "object_pattern",
                                  "object_assignment_pattern", "assignment_pattern", "arrow_function")
            ):
                params.add(_text(n, data))
    for node in _walk(scope):
        if node.id in skipped:
            continue
        t = node.type
        if _boolean_like(node, data) and not helper and not in_accounted(node):
            return False
        if t == "spread_element":
            return False
        if t == "object":
            members = _plain(node)
            has_pin = any(
                m.type == "pair" and _keyed_by(m, data, flag_key) for m in members
            )
            if has_pin and len(members) != 1:
                return False
            # A computed key can spell any flag.
            if any(
                m.type == "pair" and (k := m.child_by_field_name("key")) is not None
                and k.type == "computed_property_name"
                for m in members
            ):
                return False
        if t == "identifier" and node.parent is not None and node.parent.type not in (
            "property_identifier", "function_declaration", "variable_declarator", "required_parameter",
            "optional_parameter", "formal_parameters",
        ):
            text = _text(node, data)
            kind = file.bad_bindings.get(text)
            if kind == "boolean":
                return False
            # A name nothing in the file declares could be anything.
            if text not in file.declared and node.parent.type in ("arguments", "jsx_expression"):
                return False
            if kind == "object" and node.parent.type in ("arguments", "jsx_expression", "pair"):
                return False
        if t == "number" and _text(node, data) in ("0", "1"):
            call = node.parent.parent if node.parent is not None and node.parent.type == "arguments" else None
            if call is not None and call.type == "call_expression" and _STATEFUL & set(_callee_words(call, data)):
                return False
        if helper and _boolean_like(node, data) and not in_accounted(node):
            # climb to the call whose argument it is
            holder = node
            while holder is not None and holder.type != "arguments":
                if holder.type in ("statement_block", "variable_declarator"):
                    holder = None
                    break
                holder = holder.parent
            if holder is not None and holder.parent is not None and holder.parent.type == "call_expression":
                if _STATEFUL & set(_callee_words(holder.parent, data)):
                    return False
        if helper:
            if t in ("assignment_expression", "augmented_assignment_expression"):
                right = node.child_by_field_name("right")
                if right is not None and any(_boolean_like(n, data) for n in _walk(right)):
                    return False
            if t == "pair":
                key = node.child_by_field_name("key")
                value = node.child_by_field_name("value")
                if (
                    key is not None and key.type == "string" and value is not None
                    and _boolean_like(value, data) and not _keyed_by(node, data, flag_key)
                ):
                    return False
            if t == "jsx_attribute" and node.parent is not None and node.parent.type in (
                "jsx_opening_element", "jsx_self_closing_element"
            ):
                element = node.parent.child_by_field_name("name")
                if element is not None and _text(element, data)[:1].isupper():
                    value = [c for c in node.named_children[1:] if c.type != "comment"]
                    direct = [
                        (_plain(v)[0] if v.type == "jsx_expression" and _plain(v) else v) for v in value
                    ]
                    # A bare attribute (`<Beta enabled>`) or a boolean-like value.
                    if not value or any(_boolean_like(v, data) for v in direct):
                        return False
        if t == "jsx_attribute":
            name = node.named_children[0] if node.named_children else None
            if name is not None and _FLAG_WORDS & set(_words(_text(name, data))):
                holder = [c for c in node.named_children if c.type == "jsx_expression"]
                inner = _plain(holder[0]) if holder else []
                ok = len(inner) == 1 and (
                    _only_pair(inner[0], allowed)
                    or (helper and inner[0].type == "identifier" and _text(inner[0], data) in params)
                )
                if not ok:
                    return False
        if t in ("call_expression", "new_expression") and _FLAG_WORDS & set(_callee_words(node, data)):
            if node.id in calls:
                continue
            arguments = node.child_by_field_name("arguments")
            args = _plain(arguments) if arguments is not None else []
            if args and not (len(args) == 1 and _only_pair(args[0], allowed)):
                return False
        if helper and t in ("object_assignment_pattern", "assignment_pattern", "required_parameter", "optional_parameter"):
            default = node.child_by_field_name("right") or node.child_by_field_name("value")
            target = node.child_by_field_name("left") or node.child_by_field_name("pattern")
            if default is not None and default.type == "object" and not _only_pair(default, allowed):
                return False
            if (
                default is not None and target is not None and default.type != "object"
                and not _boolean_like(default, data)
            ):
                name = _text(target, data)
                for use in _walk(scope):
                    if use.type != "identifier" or _text(use, data) != name or use.id == target.id:
                        continue
                    if any(use.id == n.id for n in _walk(target)):
                        continue
                    chain = list(_ancestors(use, scope))
                    for up in chain:
                        if up.type == "pair" and up.child_by_field_name("value") is not None:
                            value = up.child_by_field_name("value")
                            if _contains(value, use) and not _keyed_by(up, data, flag_key):
                                return False
                            break
                    for up in chain:
                        if up.type == "jsx_attribute" and up.named_children and (
                            _FLAG_WORDS & set(_words(_text(up.named_children[0], data)))
                        ):
                            if not any(
                                p.type == "pair" and _keyed_by(p, data, flag_key) and _contains(p, use)
                                for p in chain
                            ):
                                return False
            if default is not None and target is not None and _boolean_like(default, data):
                name = _text(target, data)
                used = any(
                    p.type == "pair"
                    and _keyed_by(p, data, flag_key)
                    and (v := p.child_by_field_name("value")) is not None
                    and v.type == "identifier"
                    and _text(v, data) == name
                    for p in _walk(scope)
                )
                if not used:
                    return False
    return True


def _spells(scope: Node, data: bytes, noun: str, skipped: set[int]) -> bool:
    """Whether code or text in ``scope`` outside ``skipped`` spells ``noun``:
    an identifier, a property key, a string or JSX text."""
    for node in _walk(scope):
        if node.type in (
            "identifier", "property_identifier", "shorthand_property_identifier",
            "shorthand_property_identifier_pattern", "type_identifier", "string_fragment", "jsx_text",
        ):
            if node.id in skipped or any(a.id in skipped for a in _ancestors(node, scope)):
                continue
            if {noun, noun + "s"} & set(_words(_text(node, data))):
                return True
    return False


def _ancestors(node: Node, stop: Node):
    parent = node.parent
    while parent is not None:
        yield parent
        if parent.id == stop.id:
            return
        parent = parent.parent


def _pinned_clause(kept: str) -> re.Pattern[str]:
    noun = r"(?P<noun>flag|feature|toggle|gate)"
    state = rf"(?:{_STATES[kept]})"
    return re.compile(
        rf"(?:\s*[,—–-])?\s+(?:even\s+)?(?:with|when)\s+the\s+{noun}"
        rf"(?:\s+(?:is|are))?\s+{state}(?:\s*\((?:by\s+)?default\))?(?=[\s.]*$)",
        re.IGNORECASE,
    )


_AMBIGUOUS = object()


def _declared_here(container: Node, name: str, data: bytes) -> list[Node]:
    """The functions ``container`` directly declares under ``name``."""
    found: list[Node] = []
    for child in container.named_children:
        if child.type == "export_statement":
            child = child.child_by_field_name("declaration") or child
        if child.type == "function_declaration":
            target = child.child_by_field_name("name")
            if target is not None and _text(target, data) == name:
                found.append(child)
        elif child.type in ("lexical_declaration", "variable_declaration"):
            for declarator in child.named_children:
                target = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if (
                    target is not None and value is not None and value.type in _FUNCTIONS
                    and target.type == "identifier" and _text(target, data) == name
                ):
                    found.append(value)
    return found


def _resolve(name: str, at: Node, data: bytes):
    """The function ``name`` means at ``at``: the nearest enclosing lexical
    declaration. ``_AMBIGUOUS`` when that scope declares it twice, ``None``
    when no enclosing scope does."""
    node = at.parent
    while node is not None:
        if node.type in ("program", "statement_block"):
            found = _declared_here(node, name, data)
            if len(found) > 1:
                return _AMBIGUOUS
            if found:
                return found[0]
        node = node.parent
    return None


def _helper_closure(roots: list[Node], file: _File, skip: set[int]) -> list[Node] | None:
    """Every same-file function the scopes call, transitively, each resolved to
    its nearest enclosing declaration. ``None`` when a call is ambiguous (two
    declarations in one scope) or names a helper the call cannot see: a
    same-named helper in another group is not the one that runs."""
    found: dict[int, Node] = {}
    queue = list(roots)
    while queue:
        scope = queue.pop()
        for node in _walk(scope):
            if node.type not in ("call_expression", "new_expression"):
                continue
            callee = node.child_by_field_name("function") or node.child_by_field_name("constructor")
            if callee is None or callee.type != "identifier":
                continue
            name = _text(callee, file.data)
            fn = _resolve(name, node, file.data)
            if fn is _AMBIGUOUS:
                return None
            if fn is None:
                # An imported or global name is another file's business.
                if name in file.local_functions and name not in file.imported:
                    return None
                continue
            if fn.id not in found:
                found[fn.id] = fn
                queue.append(fn)
    return list(found.values())


def _factories_clean(root: Node, file: _File) -> bool:
    """No ``vi.mock``/``jest.mock`` factory in the file sets a boolean-like value
    or reads a name bound to one. Spreading the real module is fine."""
    data = file.data
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None or _text(callee, data) not in ("vi.mock", "jest.mock", "vi.doMock", "jest.doMock"):
            continue
        for inner in _walk(node):
            if _boolean_like(inner, data):
                return False
            if inner.type == "identifier" and file.bad_bindings.get(_text(inner, data)) == "boolean":
                return False
            # A literal a function in the factory returns is a value a read can see.
            if inner.type in ("arrow_function", "function_expression", "function"):
                body = inner.child_by_field_name("body")
                if body is not None and body.type in ("number", "string", "template_string", "true", "false"):
                    return False
            if inner.type == "return_statement" and _plain(inner) and _plain(inner)[0].type in (
                "number", "string", "template_string", "true", "false"
            ):
                return False
    return True


_MOCKS = ("vi.mock", "jest.mock", "vi.doMock", "jest.doMock")


def _enclosing_statements(test: Node, root: Node, data: bytes) -> list[Node]:
    """The statements at module scope and directly in each enclosing group's
    body that can set state: not function declarations, imports, mock
    factories (scanned on their own) or the test, group and hook calls
    themselves (scanned through their own paths)."""
    containers: list[Node] = [root]
    ancestor = test.parent
    while ancestor is not None:
        if _is_group(ancestor, data):
            args = ancestor.child_by_field_name("arguments")
            for part in (_plain(args)[1:] if args is not None else []):
                body = part.child_by_field_name("body")
                if body is not None and body.type == "statement_block":
                    containers.append(body)
        ancestor = ancestor.parent
    statements: list[Node] = []
    for container in containers:
        for child in container.named_children:
            if child.type in ("import_statement", "function_declaration", "comment"):
                continue
            if child.type in ("lexical_declaration", "variable_declaration") and all(
                (d.child_by_field_name("value") is not None and d.child_by_field_name("value").type in _FUNCTIONS)
                for d in child.named_children if d.type == "variable_declarator"
            ):
                continue
            if child.type == "expression_statement":
                inner = _plain(child)[0] if _plain(child) else None
                if inner is not None and inner.type == "await_expression":
                    inner = _plain(inner)[0] if _plain(inner) else None
                if inner is not None and inner.type == "call_expression":
                    fn = inner.child_by_field_name("function")
                    text = _text(fn, data) if fn is not None else ""
                    if text in _MOCKS or _is_hook(inner, data) or _is_group(inner, data):
                        continue
                    first = _plain(inner.child_by_field_name("arguments"))[:2] if inner.child_by_field_name("arguments") is not None else []
                    declares = len(first) == 2 and first[0].type in ("string", "template_string") and first[1].type in _FUNCTIONS
                    if declares and fn is not None and (_is_test_callee(fn, data) or _is_parametrised_callee(fn, data)):
                        continue
            statements.append(child)
    return statements


def _is_group(node: Node, data: bytes) -> bool:
    if node.type != "call_expression":
        return False
    callee = node.child_by_field_name("function")
    if callee is None:
        return False
    if callee.type == "call_expression":  # describe.each(rows)('t', cb)
        callee = callee.child_by_field_name("function")
        if callee is None:
            return False
    parts = _text(callee, data).split(".")
    return any(p in ("describe", "fdescribe", "xdescribe", "suite") for p in parts) and parts[-1] != "configure"


def _pinned_titles(before: str, language: str, flag_key: str, kept: str) -> frozenset[str]:
    """Titles of the tests in ``before`` that pinned THIS flag to ``kept`` and
    nothing else that could set state, and whose code never spells the title's
    subject.

    The whitelist covers the COMPLETE scope that can set state for the test:
    its callback, the ``.each`` rows, every enclosing hook (any callee ending
    in ``beforeEach``/``beforeAll``/``afterEach``/``afterAll``), the body of
    every same-file helper any of those calls, and the module: a file with a
    ``vi.hoisted`` call, or a ``vi.mock`` factory that sets a boolean-like
    value, is refused. See :func:`_clean_scope` for what each scope may hold.
    A title shared by two tests is left out.
    """
    data = before.encode("utf-8")
    root = _parse(before, language)
    file = _File(root, data)
    if file.module_state or not _factories_clean(root, file):
        return frozenset()
    helper_calls = pinned_calls(before, language, flag_key)
    titled = _titled_tests(root, data, parametrised=True)
    counts = Counter(title for _n, _c, title in titled)
    clause = _pinned_clause(kept)
    found = set()
    for node, callee_text, title in titled:
        match = clause.search(title)
        if counts[title] != 1 or match is None or "describe" in callee_text.split(".")[0]:
            continue
        # A nested test is its own test: only an innermost one has a body.
        if any(o is not node and _contains(node, o) for o, _c, _t in titled):
            continue
        arguments = node.child_by_field_name("arguments")
        callback = _plain(arguments)[1:] if arguments is not None else []
        function = node.child_by_field_name("function")
        rows = []
        if function is not None and function.type == "call_expression":
            row_args = function.child_by_field_name("arguments")
            if row_args is not None:
                rows.append(row_args)
        hooks: list[Node] = []
        groups: list[Node | None] = [None]
        ancestor = node.parent
        while ancestor is not None:
            if _is_group(ancestor, data):
                groups.append(ancestor)
            ancestor = ancestor.parent
        for hook in _walk(root):
            if hook.type == "call_expression" and _is_hook(hook, data):
                owner = hook.parent
                while owner is not None and not _is_group(owner, data):
                    owner = owner.parent
                if any((g is None and owner is None) or (g is not None and owner is not None and g.id == owner.id) for g in groups):
                    hooks.append(hook)
        for g in groups:
            if g is not None:
                gf = g.child_by_field_name("function")
                if gf is not None and gf.type == "call_expression":
                    gr = gf.child_by_field_name("arguments")
                    if gr is not None:
                        rows.append(gr)
        strict = [*callback, *rows, *hooks]
        pairs: dict[int, Node] = {}
        calls: dict[int, Node] = {}
        for scope in callback:
            for inner in _walk(scope):
                if _pins(inner, data, flag_key, kept):
                    pairs[inner.id] = inner
                elif (
                    inner.type == "call_expression"
                    and helper_calls.get((inner.start_byte, inner.end_byte)) == kept
                    # The one boolean the call passes is the pin: a call
                    # passing two could be pinning another flag as well.
                    and sum(1 for n in _walk(inner) if n.type in ("true", "false")) == 1
                ):
                    calls[inner.id] = inner
        if not pairs and not calls:
            continue
        # The pin must be alone in its object.
        if any(pair.parent is None or len(_plain(pair.parent)) != 1 for pair in pairs.values()):
            continue
        # The stripped pin and the call that delivers it are not "spelling".
        skipped: set[int] = set(pairs) | set(calls)
        for pair in pairs.values():
            holder = pair.parent.parent if pair.parent is not None else None
            if holder is not None and holder.type == "arguments" and holder.parent is not None:
                skipped.add(holder.parent.id)
        helpers = _helper_closure(strict, file, skipped)
        if helpers is None:
            continue
        if not all(_clean_scope(st, file, {}, {}, set(), flag_key) for st in _enclosing_statements(node, root, data)):
            continue
        if not all(_clean_scope(scope, file, pairs, calls, skipped, flag_key) for scope in strict):
            continue
        if not all(
            _clean_scope(fn, file, pairs, calls, skipped, flag_key, helper=True) for fn in helpers
        ):
            continue
        # What CARRIES the pin to the provider is not "spelling": the helper's
        # own name and parameters, the flag-named JSX attribute, this flag's pair.
        spell_skip = set(skipped)
        for fn in helpers:
            holder = fn.parent
            if holder is not None and holder.type == "variable_declarator":
                target = holder.child_by_field_name("name")
                if target is not None:
                    spell_skip.add(target.id)
            for part in ("name", "parameters", "parameter"):
                child = fn.child_by_field_name(part)
                if child is not None:
                    spell_skip.add(child.id)
            for inner in _walk(fn):
                if inner.type == "jsx_attribute" and inner.named_children and (
                    _FLAG_WORDS & set(_words(_text(inner.named_children[0], data)))
                ):
                    spell_skip.add(inner.id)
                elif inner.type == "pair" and _keyed_by(inner, data, flag_key):
                    spell_skip.add(inner.id)
        noun = match.group("noun").lower()
        if any(_spells(scope, data, noun, spell_skip) for scope in [*strict, *helpers]):
            continue
        found.add(title)
    return frozenset(found)


def reworded_pinned_title(title: str, flag_key: str, kept: str) -> str | None:
    """``title`` without a trailing ``even with the gate on`` / ``when the flag
    is on`` naming the KEPT state, or ``None``.

    Only called for a test that pinned this flag to ``kept`` itself, which is
    what makes "the gate" this flag. The same bar as :func:`reworded_title`
    applies to the result: no hedge left, no dangling conjunction, never empty,
    no removed state.
    """
    clause = _pinned_clause(kept)
    if "\n" in title or key_in(title, flag_key) or _HEDGES.search(clause.sub("", title, count=1)):
        return None
    new, count = clause.subn("", title, count=1)
    removed = _removed_state_pattern(flag_key, "true" if kept == "false" else "false")
    if (
        not count
        or not new.strip()
        or _DANGLING.search(new)
        or _QUALIFIERS.search(new)
        or _DANGLING_PUNCTUATION.search(new)
        or removed.search(new)
        or new.count("\\") != title.count("\\")
    ):
        return None
    return new


def key_in(title: str, flag_key: str) -> bool:
    return bool(
        re.search(rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])", title, re.IGNORECASE)
    )


_WILD = "\0"
_PLACEHOLDER = re.compile(r"%[sdifjoOpc#%]|\$\{[^}]*\}|\$[A-Za-z_][\w.]*")


def _tokens(text: str) -> tuple[str, ...]:
    """``text`` as characters, with each placeholder a single wildcard."""
    out: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(text):
        out.extend(text[last : match.start()])
        out.append(_WILD)
        last = match.end()
    out.extend(text[last:])
    return tuple(out)


def _decode(text: str) -> str:
    """A title's value rather than its source: ``\\'`` is ``'``."""
    return re.sub(r"\\([\\'\"`$/])", r"\1", text)


def _title_patterns(root: Node, data: bytes) -> dict[int, tuple[str, ...]]:
    """Every test or group title in the file, keyed by its first argument's node
    id, as tokens of its decoded value. A template's substitutions are
    wildcards, and so is any title that is not a string or template literal
    (a variable, a concatenation, a call): it could equal anything."""
    found: dict[int, tuple[str, ...]] = {}
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None or not arguments.named_children:
            continue
        if not _is_test_callee(callee, data) and not _is_parametrised_callee(callee, data):
            continue
        if callee.type == "member_expression":
            prop = callee.child_by_field_name("property")
            # `it.each(rows)`: its argument is the rows, and its title is on
            # the call that follows.
            if prop is not None and _text(prop, data) == "each":
                continue
        first = arguments.named_children[0]
        if first.type == "string":
            found[first.id] = _tokens(_decode(_literal_text(first, data) or ""))
        elif first.type == "template_string":
            out: list[str] = []
            for child in first.named_children:
                if child.type == "template_substitution":
                    out.append(_WILD)
                else:
                    out.extend(_tokens(_decode(_text(child, data))))
            found[first.id] = tuple(out)
        elif hands_callback(arguments):
            found[first.id] = (_WILD,)
    return found


def _could_match(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Whether some string matches both ``a`` and ``b``, wildcards standing for
    any text. Iterative, so a long title cannot exhaust the stack."""
    if _WILD not in a and _WILD not in b:
        return a == b
    la, lb = len(a), len(b)
    # `below[j]` is meet(i + 1, j); `row[j]` is meet(i, j).
    below = [False] * (lb + 1)
    for i in range(la, -1, -1):
        row = [False] * (lb + 1)
        for j in range(lb, -1, -1):
            if i == la and j == lb:
                row[j] = True
                continue
            ok = False
            if i < la and a[i] == _WILD and below[j]:
                ok = True
            elif j < lb and b[j] == _WILD and row[j + 1]:
                ok = True
            elif i < la and j < lb:
                if a[i] == _WILD and b[j] != _WILD and row[j + 1]:
                    ok = True
                elif b[j] == _WILD and a[i] != _WILD and below[j]:
                    ok = True
                elif a[i] == b[j] != _WILD and below[j + 1]:
                    ok = True
            row[j] = ok
        below = row
    return below[0]


#: Directories pruned at any depth.
_ALWAYS_SKIPPED = frozenset({"node_modules", ".git"})

#: Output and vendor directories, pruned only directly under the repository
#: root: nested, `packages/build/` is a package and may hold snapshots.
_ROOT_SKIPPED = frozenset(
    {
        "dist", "build", "out", ".next", ".nuxt", "coverage", "target", "vendor",
        ".venv", "venv", "Pods", ".turbo", ".cache",
    }
)


def stored_snapshot_names(repo_dir: str) -> frozenset[str]:
    """The names of the snapshot files and directories under ``repo_dir``:
    ``Foo.test.tsx.snap`` as ``Foo.test.tsx``, Playwright's
    ``Foo.test.tsx-snapshots`` and Vitest's ``__screenshots__/Foo.test.tsx``
    as the same."""
    import os

    names: set[str] = set()
    for here, dirs, files in os.walk(repo_dir):
        at_root = os.path.abspath(here) == os.path.abspath(repo_dir)
        dirs[:] = [
            d
            for d in dirs
            if d not in _ALWAYS_SKIPPED and not (at_root and d in _ROOT_SKIPPED)
        ]
        inside_screenshots = os.path.basename(here) == "__screenshots__"
        for d in dirs:
            if d.endswith("-snapshots"):
                names.add(d[: -len("-snapshots")])
            if inside_screenshots:
                names.add(d)
        for f in files:
            if f.endswith(".snap"):
                names.add(f[: -len(".snap")])
            if inside_screenshots:
                names.add(f)
    return frozenset(names)


def reworded_title(title: str, flag_key: str, kept: str) -> str | None:
    """``title`` with the flag's condition dropped, or ``None`` when it does not
    pass the bar :func:`rename_kept_titles` sets."""
    key = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])", re.IGNORECASE)
    if "\n" in title or not key.search(title):
        return None
    clauses = _kept_clauses(flag_key, kept)
    # The one hedge that is not one: `X even with REF STATE` says X holds while
    # the flag is in the kept state, which stays true once the flag is gone.
    # Only that exact trailing clause is set aside before the hedge check.
    even_with = clauses[0][0]
    dropped_even_with = bool(even_with.search(title))
    if dropped_even_with:
        if _HEDGES.search(even_with.sub("", title, count=1)):
            return None
    elif _HEDGES.search(title):
        return None
    new = _drop_kept_clause(title, clauses)
    removed = _removed_state_pattern(flag_key, "true" if kept == "false" else "false")
    if (
        new is None
        or not new.strip()
        # A clause cut from after a conjunction leaves it hanging.
        or (dropped_even_with and _DANGLING.search(new))
        or _QUALIFIERS.search(new)
        or _DANGLING_PUNCTUATION.search(new)
        or key.search(new)
        or removed.search(new)
        # Never cut through an escape sequence.
        or new.count("\\") != title.count("\\")
    ):
        return None
    return new


def _kept_clauses(flag_key: str, kept: str) -> list[tuple[re.Pattern[str], str]]:
    """The fixed set of clause shapes :func:`rename_kept_titles` drops, each
    with its replacement, tried in order.

    ``REF`` is the key, optionally with ``the`` and a ``flag``/``feature``/
    ``toggle``/``gate`` noun on either side; ``STATE`` a word for the kept
    branch. Every shape is anchored on what follows the state word — the end
    of the title, ``and``, or a comma — so "is on Free" is never read as a
    state.
    """
    key = rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])"
    noun = r"(?:flag|feature|toggle|gate)"
    ref = rf"(?:the\s+)?(?:{noun}\s+)?{key}(?:\s+{noun})?"
    state = rf"(?:{_STATES[kept]})"
    conj = r"(?:when|while|if|once)"
    # `when REF is STATE`, or `with REF STATE`.
    clause = rf"(?:{conj}\s+{ref}\s+(?:is|are)\s+{state}|with\s+{ref}\s+{state})"
    default = r"(?:\s*\((?:by\s+)?default\))?"
    flags = re.IGNORECASE
    return [
        # `renders X even with REF STATE` → `renders X`. First: it is the only
        # shape allowed to contain a hedge word, and `reworded_title` relies on it.
        (
            re.compile(
                rf"(?:\s*[,\u2014\u2013-])?\s+even\s+with\s+{ref}\s+{state}{default}(?=[\s.]*$)",
                flags,
            ),
            "",
        ),
        # `renders X when REF is STATE` → `renders X`.
        (re.compile(rf",?\s+{clause}{default}(?=[\s.]*$)", flags), ""),
        # `when REF is STATE and Y` → `when Y`.
        (
            re.compile(
                rf"\b({conj}|with)\s+(?:{ref}\s+(?:is|are)\s+{state}|{ref}\s+{state})\s+and\s+",
                flags,
            ),
            r"\1 ",
        ),
        # `when Y and REF is STATE` → `when Y`.
        (re.compile(rf"\s+and\s+{ref}\s+(?:is|are)\s+{state}{default}(?=\s*(?:,|$))", flags), ""),
        # `when REF is STATE, X` → `X`.
        (re.compile(rf"^\s*{clause}{default}\s*,\s*", flags), ""),
    ]


def _drop_kept_clause(
    title: str, clauses: list[tuple[re.Pattern[str], str]]
) -> str | None:
    """``title`` with the first matching clause shape dropped once, or ``None``."""
    for pattern, replacement in clauses:
        new, count = pattern.subn(replacement, title, count=1)
        if count:
            return new
    return None


#: The ``bindings`` kind of a row naming a registry entry whose default was
#: the branch the fold removed. Its text is the entry, on one line.
REMOVED_DEFAULT = "removed default"


def registry_defaults(source: str, language: str, flag_key: str) -> list[RegistryEntry]:
    """Each registry entry in ``source`` declaring a boolean default for
    ``flag_key``: ``'k': { default: false }``.

    A lookup by key reads its fallback out of the entry, so a test that never
    sets the flag gets that value; see :func:`find_pinned_tests`. Only the
    entry's OWN ``default`` member counts, and nothing in a test file does,
    where the same shape is fixture data.
    """
    data = source.encode("utf-8")
    root = _parse(source, language)
    if _titled_tests(root, data):
        return []
    found = []
    for pair in _walk(root):
        if pair.type != "pair" or not _keyed_by(pair, data, flag_key):
            continue
        value = pair.child_by_field_name("value")
        if value is None or value.type != "object":
            continue
        for member in value.named_children:
            if member.type != "pair":
                continue
            name = member.child_by_field_name("key")
            literal = member.child_by_field_name("value")
            if (
                name is not None
                and literal is not None
                and _key_text(name, data) == "default"
                and literal.type in ("true", "false")
            ):
                siblings = frozenset(
                    name
                    for other in (pair.parent.named_children if pair.parent else ())
                    if other.type == "pair" and other.id != pair.id
                    and (key := other.child_by_field_name("key")) is not None
                    and (name := _key_text(key, data)) is not None
                )
                found.append(
                    RegistryEntry(literal.type, " ".join(_text(pair, data).split()), siblings)
                )
    return found


def is_test_file(source: str, language: str) -> bool:
    """Whether ``source`` declares a test: an ``it``/``test``/``describe`` with a title."""
    return bool(_titled_tests(_parse(source, language), source.encode("utf-8"), parametrised=True))


def title_nodes(root: Node, data: bytes) -> frozenset[int]:
    """The ids of every test TITLE argument in ``root``, for any TS-family tree.

    ``data`` is the whole source as bytes, never ``root.text``: the root node
    starts after any leading whitespace, so a file opening with a blank line
    would shift every offset and no callee would read as a test.

    Narrower than :func:`_titled_tests`: the call must also hand over a
    callback as its second argument. A title is a string nothing is typed
    against and nothing looks up, which is what makes it inert — but a function
    that merely happens to be called ``test`` and is handed the key is a call,
    and may well be typed against the flag registry. The callback is what tells
    the two apart.
    """
    found = set()
    for node, _callee, _title in _titled_tests(root, data, parametrised=True):
        arguments = node.child_by_field_name("arguments")
        if arguments is not None and hands_callback(arguments):
            found.add(arguments.named_children[0].id)
    return frozenset(found)


#: The second argument of a call that declares a test.
_CALLBACKS = frozenset({"arrow_function", "function_expression", "function"})


def hands_callback(arguments: Node) -> bool:
    """Whether a call's ``arguments`` hand over a callback second, as a call
    declaring a test does: what tells a runner's title apart from a name
    handed to a function that only happens to be called ``test``. See
    :func:`title_nodes`."""
    named = arguments.named_children
    return len(named) >= 2 and named[1].type in _CALLBACKS


def data_mention(source: str, language: str, flag_key: str) -> str | None:
    """The first line of a test file naming ``flag_key`` as DATA, or ``None``.

    Data is any mention that is not one of the three things the transform
    knows how to account for in a test: an override entry (``'k': true``) or
    the type member declaring one (``'k'?: boolean``), a test's title, and a
    comment. ``<FlagGate flag="k">`` in a test of the gate itself is the case
    this exists for: the key there is fixture data, and the component it is
    handed still reads it.

    Asked of a test file's REWRITTEN text. Stripping the file's overrides and
    deleting its tests is only justified when nothing in it reads the flag any
    more; while something still does, those overrides are what drive it, and
    the file has to go back exactly as it was.
    """
    data = source.encode("utf-8")
    root = _parse(source, language)
    mention = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])"
    )
    titles = {
        arguments.named_children[0].id
        for node, _callee, _title in _titled_tests(root, data, parametrised=True)
        if (arguments := node.child_by_field_name("arguments")) is not None
    }
    for node in _walk(root):
        if node.type not in ("string", "template_string"):
            continue
        if not mention.search(_text(node, data)) or node.id in titles:
            continue
        parent = node.parent
        if parent is not None and parent.type in ("pair", "property_signature"):
            key = parent.child_by_field_name("key" if parent.type == "pair" else "name")
            if key is not None and key.id == node.id:
                continue
        line_start = data.rfind(b"\n", 0, node.start_byte) + 1
        line_end = data.find(b"\n", node.end_byte)
        line = data[line_start : len(data) if line_end == -1 else line_end]
        return line.decode("utf-8").strip()
    return None


def _removed_state_pattern(
    flag_key: str, untreated: str, generic: bool = True
) -> re.Pattern[str]:
    """A title naming the flag in the removed state.

    ``generic`` lets "the flag"/"the feature" stand for the key; without it the
    title has to spell the key itself. The key is bounded by key characters
    rather than ``\\b``, which does not fire beside a hyphen.
    """
    key = rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])"
    if not generic:
        state = rf"(?:{_STATES[untreated]})"
        return re.compile(
            rf"{key}(?:\W+\w+){{0,2}}?\W+{state}\b|\b{state}\W+{key}",
            re.IGNORECASE,
        )
    subject = rf"(?:{_SUBJECTS}|{re.escape(flag_key)})"
    state = rf"(?:{_STATES[untreated]})"
    return re.compile(
        rf"\b{subject}\b(?:\W+\w+){{0,2}}?\W+{state}\b|\b{state}\W+{subject}\b",
        re.IGNORECASE,
    )


#: Words a key is made of that never name it in a title: the subjects and
#: states the patterns already match on, and words that describe a flag's
#: life rather than what it gates ("the new feature", "the beta flag").
_NOT_QUALIFIERS = frozenset(
    {"flag", "flags", "feature", "features", "toggle", "toggles", "gate", "gates",
     "on", "off", "enabled", "disabled", "enable", "disable", "true", "false",
     "the", "is", "use", "show", "hide", "new", "old", "legacy", "beta", "alpha",
     "experiment", "experimental", "test", "temp", "tmp", "next", "preview", "rollout"}
)


def _key_words(flag_key: str) -> list[str]:
    """``flag_key``'s words, lowercased: split at punctuation and case."""
    return [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", flag_key)]


def _qualified_removed_state_pattern(
    flag_key: str, untreated: str, siblings: frozenset[str] = frozenset()
) -> re.Pattern[str] | None:
    """A title naming the flag in the removed state by a word of its key:
    "the GitHub flag is off" for ``web-social-login-github``.

    The word must sit directly before the subject noun, and be one of the
    key's words other than its first that no key in ``siblings`` shares (see
    :func:`find_pinned_tests` for why). ``None`` when the key has no such
    word, as a one-word key does: a title naming it names the whole key, which
    the keyed pattern already takes.
    """
    words = _key_words(flag_key)
    shared = {w for key in siblings if key != flag_key for w in _key_words(key)}
    qualifiers = (
        {w for w in words[1:] if len(w) > 1 and not w.isdigit()} - _NOT_QUALIFIERS - shared
    )
    if not qualifiers:
        return None
    qualifier = "|".join(sorted(map(re.escape, qualifiers)))
    subject = rf"\b(?:{qualifier})[\s-]+(?:{_SUBJECTS})\b"
    state = rf"(?:{_STATES[untreated]})"
    return re.compile(
        rf"{subject}(?:\W+\w+){{0,2}}?\W+{state}\b|\b{state}\W+{subject}",
        re.IGNORECASE,
    )


#: The hooks that run before each test of their scope.
_SETUP_CALLEES = frozenset({"beforeEach", "beforeAll"})


def _setup_scopes(root: Node, data: bytes, flag_key: str) -> list[Node]:
    """The scopes whose tests a ``beforeEach``/``beforeAll`` gives ``flag_key``
    a value in: the block around the hook, or the whole file."""
    scopes = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "identifier" or _text(callee, data) not in _SETUP_CALLEES:
            continue
        if not any(_sets_key(inner, data, flag_key) for inner in _walk(node)):
            continue
        scope = node.parent
        while scope is not None and scope.type not in ("statement_block", "program"):
            scope = scope.parent
        scopes.append(scope if scope is not None else root)
    return scopes


def _sets_key(node: Node, data: bytes, flag_key: str) -> bool:
    """Whether ``node`` gives ``flag_key`` a value: a pair keyed by it, with
    any value, or a shorthand property spelling it."""
    if node.type == "shorthand_property_identifier":
        return _text(node, data) == flag_key
    return node.type == "pair" and _keyed_by(node, data, flag_key)


def _keyed_by(pair: Node, data: bytes, flag_key: str) -> bool:
    key = pair.child_by_field_name("key")
    return key is not None and _key_text(key, data) == flag_key


def _key_text(key: Node, data: bytes) -> str | None:
    """A pair key's name: an identifier's text, or a plain string's."""
    if key.type == "property_identifier":
        return _text(key, data)
    if key.type == "string":
        fragments = key.named_children
        if len(fragments) == 1 and fragments[0].type == "string_fragment":
            return _text(fragments[0], data)
    return None


def _pins(pair: Node, data: bytes, flag_key: str, untreated: str) -> bool:
    """Whether ``pair`` is ``'<flag_key>': <untreated>``, keyed by a whole literal."""
    if pair.type != "pair":
        return False
    key = pair.child_by_field_name("key")
    value = pair.child_by_field_name("value")
    if key is None or value is None or key.type != "string" or value.type != untreated:
        return False
    fragments = key.named_children
    return (
        len(fragments) == 1
        and fragments[0].type == "string_fragment"
        and _text(fragments[0], data) == flag_key
    )


def _titled_tests(
    root: Node, data: bytes, *, parametrised: bool = False
) -> list[tuple[Node, str, str]]:
    """``(call, callee text, title)`` for every test-shaped call, in source order.

    ``parametrised`` also admits ``it.each(rows)('title', cb)`` and its
    spellings. The callers that ask whether a title is INERT pass it
    (:func:`title_nodes`, :func:`data_mention`), and so does
    :func:`rename_kept_titles`, because dropping a clause naming the removed
    flag is true of every row. :func:`find_pinned_tests` does not: it
    DELETES the test, and one declaration stands for several runs whose
    titles are filled in from the rows, so nothing here proves the title
    chosen for the declaration describes each of them.
    """
    found = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None:
            continue
        if not _is_test_callee(callee, data) and not (
            parametrised and _is_parametrised_callee(callee, data)
        ):
            continue
        first = arguments.named_children[0] if arguments.named_children else None
        title = _literal_text(first, data) if first is not None else None
        if title is not None:
            found.append((node, _text(callee, data), title))
    return sorted(found, key=lambda entry: entry[0].start_byte)


def _is_parametrised_callee(callee: Node, data: bytes) -> bool:
    """``it.each(rows)``, ``test.only.each(rows)`` or ``` it.each`table` ```.

    The callee of a parametrised test's own call is itself a call, whose
    function is a ``.each`` member chain rooted at a test callee. A chain rooted
    at anything else (``foo.each(rows)('k', cb)``) is not a test.
    """
    if callee.type != "call_expression":
        return False
    inner = callee.child_by_field_name("function")
    if inner is None or inner.type != "member_expression":
        return False
    prop = inner.child_by_field_name("property")
    if prop is None or _text(prop, data) != "each":
        return False
    target = inner.child_by_field_name("object")
    while target is not None and target.type == "member_expression":
        target = target.child_by_field_name("object")
    return (
        target is not None
        and target.type == "identifier"
        and _text(target, data) in _TEST_CALLEES
    )


def _is_test_callee(callee: Node, data: bytes) -> bool:
    if callee.type == "identifier":
        return _text(callee, data) in _TEST_CALLEES
    if callee.type == "member_expression":
        target = callee.child_by_field_name("object")
        return (
            target is not None
            and target.type == "identifier"
            and _text(target, data) in _TEST_CALLEES
        )
    return False


def _literal_text(node: Node, data: bytes) -> str | None:
    """The text of a plain string or substitution-free template literal."""
    if node.type == "string":
        return "".join(_text(c, data) for c in node.named_children)
    if node.type == "template_string" and all(
        c.type in {"string_fragment", "escape_sequence"} for c in node.named_children
    ):
        return "".join(_text(c, data) for c in node.named_children)
    return None


def _identity(node: Node, titled: list[tuple[Node, str, str]]) -> PinnedTest:
    callee, title = next((c, t) for n, c, t in titled if n.id == node.id)
    ordinal = sum(
        1
        for other, c, t in titled
        if c == callee and t == title and other.start_byte < node.start_byte
    )
    return PinnedTest(callee, title, ordinal)


def _contains(outer: Node, inner: Node) -> bool:
    return outer.start_byte <= inner.start_byte and inner.end_byte <= outer.end_byte


def _own_lines(statement: Node, data: bytes) -> tuple[int, int] | None:
    """The statement's whole lines, plus the blank line deleting it would strand.

    Tests are separated by one blank line, so taking a test out leaves either
    two blank lines in a row, or one directly after the block's opening line
    or before its closing one. One blank line goes with the test in each case,
    so the file reads as if the test had never been there.
    """
    start = data.rfind(b"\n", 0, statement.start_byte) + 1
    newline = data.find(b"\n", statement.end_byte)
    end = len(data) if newline == -1 else newline + 1
    if data[start : statement.start_byte].strip() or data[statement.end_byte : end].strip():
        return None
    previous = _line_before(data, start)
    following_newline = data.find(b"\n", end)
    following = data[end : len(data) if following_newline == -1 else following_newline + 1]
    if following.strip() == b"" and end < len(data):
        if previous is None or not previous.strip() or previous.rstrip().endswith(b"{"):
            return start, end + len(following)
    if previous is not None and not previous.strip() and (
        following.lstrip().startswith(b"}") or not data[end:].strip()
    ):
        return start - len(previous) - 1, end
    return start, end


def _line_before(data: bytes, start: int) -> bytes | None:
    """The line ending just before ``start``, without its newline; ``None`` at the top."""
    if start == 0:
        return None
    begin = data.rfind(b"\n", 0, start - 1) + 1
    return data[begin : start - 1]


def _text(node: Node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


# --- Test scaffolding left describing the removed branch (report only) -----

#: The ``bindings`` kind of a negative assertion on text only the removed
#: branch rendered: it now holds whatever the code does. Text ``"<line>: <statement>"``.
VACUOUS_ASSERTION = "vacuous assertion"

#: The ``bindings`` kind of a kept test whose title still describes what the
#: removed branch did. Text: the title.
STALE_TITLE = "stale test title"

#: Testing Library queries: ``getByText``, ``queryAllByRole``, ``findByLabelText``…
_QUERY = re.compile(r"^(get|query|find)(All)?By(Text|Role|LabelText|PlaceholderText|DisplayValue|AltText|Title|TestId)$")

#: Matchers that say an element is there (``not`` turns them around).
_PRESENT = frozenset({"toBeInTheDocument", "toBeVisible", "toBeTruthy", "toBeDefined"})
#: Matchers that say it is not.
_ABSENT = frozenset({"toBeNull", "toBeFalsy", "toBeUndefined"})

#: Words two titles share without being about the same behaviour.
_TITLE_STOPWORDS = frozenset(
    """
    a an the and or but if when while with without to of in on at for from by as is are
    be it its it's this that these those not no nothing only still even then than
    render renders rendered rendering show shows shown showing display displays
    return returns call calls work works should does do doing test tests page
    component user users value values state data default one two any all some
    new old instead after before once until
    """.split()
)


def _stems(title: str) -> set[str]:
    words = re.findall(r"[a-z]+", title.lower())
    stems = set()
    for word in words:
        if len(word) < 4 or word in _TITLE_STOPWORDS or re.fullmatch(_SUBJECTS, word):
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        if word not in _TITLE_STOPWORDS:
            stems.add(word)
    return stems


def _query_key(call: Node, data: bytes) -> tuple[str, tuple[str, str, str | None]] | None:
    """``(verb, (kind, text, name))`` of a Testing Library query call whose
    text is a plain string: ``queryByText('X')``, ``getByRole('button',
    { name: 'Add' })``."""
    callee = call.child_by_field_name("function")
    if callee is not None and callee.type == "member_expression":
        callee = callee.child_by_field_name("property")
    if callee is None or callee.type not in ("identifier", "property_identifier"):
        return None
    match = _QUERY.match(_text(callee, data))
    if match is None:
        return None
    arguments = call.child_by_field_name("arguments")
    named = [] if arguments is None else [c for c in arguments.named_children if c.type != "comment"]
    first = _literal_text(named[0], data) if named else None
    if first is None:
        return None
    name = None
    if match.group(3) == "Role" and len(named) > 1 and named[1].type == "object":
        for pair in named[1].named_children:
            key = pair.child_by_field_name("key") if pair.type == "pair" else None
            if key is not None and _key_text(key, data) == "name":
                value = pair.child_by_field_name("value")
                name = _literal_text(value, data) if value is not None else None
                if name is None:
                    return None
    return match.group(1), (match.group(3), first, name)


def _assertion_sense(call: Node, data: bytes) -> str | None:
    """``"present"``/``"absent"`` for ``expect(<call>)…<matcher>()``."""
    arguments = call.parent
    holder = arguments.parent if arguments is not None and arguments.type == "arguments" else None
    if holder is None or holder.type != "call_expression":
        return None
    callee = holder.child_by_field_name("function")
    if callee is None or callee.type != "identifier" or _text(callee, data) != "expect":
        return None
    negated, node = False, holder
    while node.parent is not None and node.parent.type == "member_expression":
        prop = node.parent.child_by_field_name("property")
        name = _text(prop, data) if prop is not None else ""
        if name == "not":
            negated = not negated
            node = node.parent
            continue
        outer = node.parent.parent
        if outer is None or outer.type != "call_expression":
            return None
        if name in _PRESENT:
            return "absent" if negated else "present"
        if name in _ABSENT:
            return "present" if negated else "absent"
        return None
    return None


def _assertions(root: Node, data: bytes) -> list[tuple[Node, str, tuple[str, str, str | None]]]:
    """``(call, sense, key)`` for each query call: ``present`` for a
    ``get``/``find`` (they throw when nothing matches) or an assertion that it
    is there, ``absent`` for one that it is not."""
    found = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        query = _query_key(node, data)
        if query is None:
            continue
        verb, key = query
        sense = "present" if verb in ("get", "find") else _assertion_sense(node, data)
        if sense is not None:
            found.append((node, sense, key))
    return found


def removed_texts(before: str, after: str, language: str) -> frozenset[str]:
    """The JSX text ``before`` rendered that ``after`` no longer holds."""
    if language == "ts":
        return frozenset()
    old = _jsx_texts(_parse(before, language), before.encode("utf-8"))
    return frozenset(t for t in old if t not in after)


def _jsx_texts(root: Node, data: bytes) -> set[str]:
    texts = set()
    for node in _walk(root):
        if node.type == "jsx_text":
            text = " ".join(_text(node, data).split())
            if len(text) > 1 and re.search(r"[^\W\d_]", text):
                texts.add(text)
    return texts


#: A subject word followed by one of the words for a flag's state: "the flag is
#: off", "with the gate on", "the feature enabled". `on` is not a state before
#: a capitalised word or an article ("the toggle on Free", "toggle on the page").
_STATE_WORDS = (
    r"(?:(?:turned\s+)?(?:off|disabled|enabled|false|true)"
    r"|(?:turned\s+)?on(?!\s+(?:the|a|an|this|that|every|each|any|(?-i:[A-Z]))))"
)
_SPEAKS_OF_STATE = re.compile(
    rf"(?<![\w-])(?:{_SUBJECTS})(?![\w-])\s+(?:(?:is|are|was|were|being|stays?|remains?|gets?|has|have)\s+)*{_STATE_WORDS}\b",
    re.IGNORECASE,
)
#: Readiness: "waits for flags to load", "until flags are fetched", "before the
#: flags have not loaded".
_SPEAKS_OF_READINESS = re.compile(
    rf"(?<![\w-])(?:{_SUBJECTS})(?![\w-])\s+"
    r"(?:(?:to|have|has|had|are|is|were|was|get|got|been|not|yet|finish(?:es|ed)?|still|already|all|finally)\s+)*"
    r"(?:load|loads|loaded|loading|resolve|resolves|resolved|ready|settle|settles|settled"
    r"|initiali[sz]e[sd]?|fetch|fetched|fetching|arrive|arrived)\b",
    re.IGNORECASE,
)
#: A hyphenated compound ("web-flag-permanence", "dead-flag") is a NAME: the
#: title is about some other flag. A compound made only of subject words
#: ("feature-flag", "feature-gated") is the subject itself, written as one word.
_FLAG_NAME = re.compile(r"[\w]+(?:-[\w]+)+")
_SUBJECT_PART = re.compile(r"(?:flags?|features?|toggles?|toggled|gates?|gated|flagged)", re.IGNORECASE)


def speaks_of_a_flag(title: str) -> bool:
    """Whether ``title`` says something about a flag's STATE or readiness.

    A flag product's tests say "flags" about their domain ("should filter
    flags by name", "the flag cleanup toggle"), which is not a statement about
    the flag this run removed. Only a subject word inside a state phrase
    ("when the flag is off") or a readiness phrase ("waits for flags to
    load") counts, and a title naming a hyphenated compound with a subject
    word in it ("web-flag-permanence", "dead-flag") is about another flag.
    """
    for name in _FLAG_NAME.findall(title):
        parts = name.split("-")
        if all(_SUBJECT_PART.fullmatch(part) for part in parts):
            title = title.replace(name, "flag", 1)
        elif any(_SUBJECT_PART.fullmatch(part) for part in parts):
            return False
    return bool(_SPEAKS_OF_STATE.search(title) or _SPEAKS_OF_READINESS.search(title))


def _chain_in_before(
    title: str, before_chains: dict[str, set[tuple[str, ...]]], after_chain: tuple[str, ...]
) -> tuple[str, ...]:
    """The describe chain a kept test had BEFORE the run, when its title says
    where: the run may have renamed a group around it."""
    chains = before_chains.get(title, set())
    return next(iter(chains)) if len(chains) == 1 else after_chain


def _describe_chain(node: Node, data: bytes) -> tuple[str, ...]:
    """The titles of the ``describe`` groups enclosing ``node``, outermost first."""
    chain: list[str] = []
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type == "call_expression":
            callee = ancestor.child_by_field_name("function")
            arguments = ancestor.child_by_field_name("arguments")
            if (
                callee is not None
                and arguments is not None
                and "describe" in _text(callee, data).split(".")[0]
                and arguments.named_children
            ):
                title = _literal_text(arguments.named_children[0], data)
                chain.append(title if title is not None else "\0")
        ancestor = ancestor.parent
    return tuple(reversed(chain))


def stale_scaffolding(
    before: str,
    after: str,
    language: str,
    flag_key: str,
    deleted: frozenset[str] = frozenset(),
    renamed: frozenset[str] = frozenset(),
    gone: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """What a changed test file still says about the branch the run removed:
    ``("<line>: <statement>", …)`` for each negative assertion now vacuous,
    and each kept title that still describes the removed behaviour.

    A negative assertion (``expect(queryByText('X')).not.toBeInTheDocument()``)
    is vacuous when only the removed branch could make ``X`` appear: every
    query that said it was there sat in a test this run deleted (``before``
    had one, ``after`` has none), or ``X`` is JSX text the run deleted from
    the code under test (``gone``). A kept title is stale when it speaks of
    the flag's state or readiness (:func:`speaks_of_a_flag`: "waits for flags
    to load …"), does not name the key (that is
    :func:`rename_kept_titles`' case), was not just renamed (``renamed``),
    and either shares a word with the title of a test the run deleted from
    the same ``describe`` group (``deleted``: "… instead of redirecting" beside "redirects a
    typed URL …") or holds one of the vacuous assertions. Report only: a
    reviewer pointer, never an edit.
    """
    if not covers(language) or before == after:
        return [], []
    data = after.encode("utf-8")
    root = _parse(after, language)
    titled = _titled_tests(root, data)
    if not titled:
        return [], []
    old_data = before.encode("utf-8")
    present_before = {k for _n, s, k in _assertions(_parse(before, language), old_data) if s == "present"}
    current = _assertions(root, data)
    present_after = {k for _n, s, k in current if s == "present"}
    vacuous: list[tuple[int, Node]] = []
    for node, sense, key in current:
        if sense != "absent" or key in present_after:
            continue
        if key in present_before or (key[0] != "TestId" and (key[2] or key[1]) in gone):
            statement = node
            while statement.parent is not None and statement.type != "expression_statement":
                statement = statement.parent
            vacuous.append((statement.start_point[0] + 1, statement))
    rows: list[str] = []
    seen: set[int] = set()
    for line, statement in vacuous:
        if statement.id in seen:
            continue
        seen.add(statement.id)
        rows.append(f"{line}: {_text(statement, data).splitlines()[0].strip()}")
    key = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])", re.IGNORECASE)
    # The stems of each deleted title, by the describe chain it sat in. A kept
    # title shares a word with a deleted one only when both are in the same
    # innermost group; the same word elsewhere is another behaviour. A group
    # the run emptied is matched one level up, so a sibling group ("with the
    # flag on" beside the emptied "with the flag off") still counts. Every
    # chain is taken from `before`, so a group the run renamed is still the
    # same group.
    old_titled = _titled_tests(_parse(before, language), old_data)
    before_chains: dict[str, set[tuple[str, ...]]] = {}
    for n, _c, t in old_titled:
        before_chains.setdefault(t, set()).add(_describe_chain(n, old_data))
    kept_chains = [
        _chain_in_before(t, before_chains, _describe_chain(n, data)) for n, c, t in titled
        if "describe" not in c.split(".")[0]
    ]
    deleted_stems: list[tuple[tuple[str, ...], bool, set[str]]] = []
    if deleted:
        for n, _c, t in old_titled:
            if t not in deleted:
                continue
            chain = _describe_chain(n, old_data)
            # A group the run emptied: a kept test counts when its own innermost
            # group is a direct SIBLING of it.
            emptied = len(chain) > 1 and not any(k[: len(chain)] == chain for k in kept_chains)
            deleted_stems.append((chain, emptied, _stems(t)))
    titles: list[str] = []
    for node, callee, title in titled:
        if "describe" in callee.split(".")[0]:
            continue  # a group's title names its subject, not a behaviour
        if title in renamed or key.search(title) or not speaks_of_a_flag(title):
            continue
        holds = any(_contains(node, statement) for _l, statement in vacuous)
        chain = _chain_in_before(title, before_chains, _describe_chain(node, data))
        shares = any(
            _stems(title) & stems and (chain == at or (emptied and len(chain) == len(at) and chain[:-1] == at[:-1]))
            for at, emptied, stems in deleted_stems
        )
        if holds or shares:
            if title not in titles:
                titles.append(title)
    return rows, titles

