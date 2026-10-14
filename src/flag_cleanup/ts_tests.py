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


def find_pinned_tests(
    source: str, language: str, flag_key: str, untreated: str
) -> list[PinnedTest]:
    """Every test in ``source`` to delete because it exercises the removed branch.

    ``untreated`` is the literal of the branch the fold DELETES, as the rules
    spell it. A test enclosing another chosen one is chosen instead of it, so
    nothing is deleted twice.
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


def is_test_file(source: str, language: str) -> bool:
    """Whether ``source`` declares a test: an ``it``/``test``/``describe`` with a title."""
    return bool(_titled_tests(_parse(source, language), source.encode("utf-8")))


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
    for node, _callee, _title in _titled_tests(root, data):
        arguments = node.child_by_field_name("arguments")
        named = arguments.named_children if arguments is not None else []
        if len(named) >= 2 and named[1].type in _CALLBACKS:
            found.add(named[0].id)
    return frozenset(found)


#: The second argument of a call that declares a test.
_CALLBACKS = frozenset({"arrow_function", "function_expression", "function"})


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
        for node, _callee, _title in _titled_tests(root, data)
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


def _titled_tests(root: Node, data: bytes) -> list[tuple[Node, str, str]]:
    """``(call, callee text, title)`` for every test-shaped call, in source order."""
    found = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None or not _is_test_callee(callee, data):
            continue
        first = arguments.named_children[0] if arguments.named_children else None
        title = _literal_text(first, data) if first is not None else None
        if title is not None:
            found.append((node, _text(callee, data), title))
    return sorted(found, key=lambda entry: entry[0].start_byte)


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
