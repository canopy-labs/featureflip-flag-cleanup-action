"""Resolve django-waffle's test pins of the flag: ``override_flag`` and kin.

A Django test states the branch it exercises by pinning the flag, not by
mocking a read:

.. code-block:: python

    @override_flag("new-checkout", active=False)
    def test_old_checkout(self): ...

    with override_flag("new-checkout", active=True):
        ...

``override_switch`` and ``override_sample`` (from ``waffle.testutils``) take
the same ``(name, active)``. What is decided is what :mod:`flag_cleanup.mock_stubs`
decides for a stub. A pin of the KEPT value is dead once the flag is gone: a
decorator's line goes, and a ``with`` hands its body back to the block around
it. A test holding a pin of the REMOVED value is about code this removal
deletes, so the whole test goes, the same thing the fold did to the branch.

A pin this cannot settle is left: one whose value is not a literal, one on a
class or in a function that is not a test (a pytest fixture), and one in a
shape this does not read. :func:`refusal_reason` finds what is left after the
transform, and a removed or unknown value there refuses the flag, because a
test still pinned to the removed branch fails once it is gone. A kept-value pin
left over does nothing and is only a remaining mention of the key.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from tree_sitter import Node

from flag_cleanup import mock_stubs, python_patches, syntax
from flag_cleanup.mock_stubs import STUB, TEST, Removal

#: The pins ``waffle.testutils`` provides, all ``(name, active)``.
_PINS = frozenset({"override_flag", "override_switch", "override_sample"})
_LITERALS = {"true": True, "false": False}


@dataclass
class _Pin:
    #: The decorator, or the ``with_item``.
    node: Node
    call: Node
    #: The value it pins; ``None`` when that is not a literal.
    value: bool | None


@dataclass
class _Test:
    node: Node
    name: str
    #: The whole statement, decorators included.
    outer: Node
    pins: list[_Pin] = field(default_factory=list)


def _text(node: Node | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def _pin_name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "attribute":
        function = function.child_by_field_name("attribute")
    name = _text(function) if function is not None and function.type == "identifier" else None
    return name if name in _PINS else None


def _pins_key(call: Node, flag_key: str, key_texts: frozenset[str]) -> bool:
    """A pin call whose name argument is this flag's key."""
    if call.type != "call" or _pin_name(call) is None:
        return False
    positional, keywords = python_patches._arguments(call)
    name = positional[0] if positional else keywords.get("name")
    if name is None:
        return False
    if name.type == "string":
        return python_patches._string(name) == flag_key and not _text(name)[:1].lower() in ("f", "b")
    return name.type in ("identifier", "attribute") and _text(name) in key_texts


def _value(call: Node) -> bool | None:
    """What the pin sets, when its call says so as a literal and nothing else
    can change it."""
    positional, keywords = python_patches._arguments(call)
    if len(positional) > 2 or set(keywords) - {"name", "active"}:
        return None
    answer = keywords.get("active") if len(positional) < 2 else positional[1]
    if answer is None or (len(positional) == 2 and "active" in keywords):
        return None
    return _LITERALS.get(answer.type)


def _calls(root: Node, flag_key: str, key_texts: frozenset[str]) -> list[Node]:
    return [n for n in mock_stubs._walk(root) if _pins_key(n, flag_key, key_texts)]


def _with_item_of(call: Node) -> Node | None:
    item = call.parent
    return item if item is not None and item.type == "with_item" else None


def _decorator_of(call: Node) -> Node | None:
    decorator = call.parent
    return decorator if decorator is not None and decorator.type == "decorator" else None


def _test_owning(node: Node, tests: dict[int, _Test]) -> _Test | None:
    """The test ``node`` is a decorator of, or sits inside."""
    current: Node | None = node
    while current is not None:
        if current.id in tests:
            return tests[current.id]
        if current.type == "decorated_definition":
            inner = current.child_by_field_name("definition")
            if inner is not None and inner.id in tests:
                return tests[inner.id]
        current = current.parent
    return None


def _kept_edit(pin: _Pin, data: bytes) -> tuple[int, int, bytes] | None:
    if pin.node.type == "decorator":
        return (
            python_patches._line_start(data, pin.node.start_byte),
            python_patches._line_after(data, pin.node.end_byte),
            b"",
        )
    alias = pin.node.child_by_field_name("value")
    if alias is not None and alias.type == "as_pattern":
        return None
    return python_patches._with_edit(pin.node, data)


def resolve(
    source: str, flag_key: str, treatment: bool, key_texts: Iterable[str] = ()
) -> tuple[str, list[Removal]]:
    """``source`` with every decidable waffle pin of ``flag_key`` resolved; what went."""
    if not any(name in source for name in _PINS) or flag_key not in source and not key_texts:
        return source, []
    texts = frozenset(key_texts)
    data = source.encode("utf-8")
    root = syntax._parse(source, "python")
    calls = _calls(root, flag_key, texts)
    if not calls:
        return source, []
    tests = {
        t.node.id: _Test(t.node, t.name, t.outer) for t in python_patches._tests(root)
    }
    # A pin outside any test (on a class, in a fixture or a helper) cannot
    # take a test with it, so only the kept value is resolved there.
    loose: list[_Pin] = []
    for call in calls:
        node = _decorator_of(call) or _with_item_of(call)
        if node is None:
            continue
        test = _test_owning(call, tests)
        if test is None or (
            node.type == "decorator" and node.parent is not None and node.parent.id != test.outer.id
        ):
            loose.append(_Pin(node, call, _value(call)))
            continue
        test.pins.append(_Pin(node, call, _value(call)))

    # Each kept-value edit beside the line it is reported as.
    stubs: list[tuple[tuple[int, int, bytes], Removal]] = []
    deleted: list[_Test] = []
    for test in tests.values():
        if not test.pins or any(pin.value is None for pin in test.pins):
            continue
        if any(pin.value is not treatment for pin in test.pins):
            deleted.append(test)
            continue
        kept = [_kept_edit(pin, data) for pin in test.pins]
        if any(edit is None for edit in kept) or python_patches._overlap(kept):
            continue
        stubs += [(edit, Removal(STUB, " ".join(_text(pin.node).split()))) for edit, pin in zip(kept, test.pins)]
    # A test inside another deleted test goes with it, and so does any edit
    # there: overlapping spans would cut the text twice.
    deleted = [
        t for t in deleted
        if not any(o is not t and o.outer.start_byte <= t.outer.start_byte < o.outer.end_byte for o in deleted)
    ]
    gone = [(t.outer.start_byte, t.outer.end_byte) for t in deleted]
    stubs = [(e, r) for e, r in stubs if not any(a <= e[0] < b for a, b in gone)]
    for pin in loose:
        if pin.value is not treatment or any(a <= pin.node.start_byte < b for a, b in gone):
            continue
        edit = _kept_edit(pin, data)
        if edit is None or python_patches._overlap([*(e for e, _ in stubs), edit]):
            continue
        stubs.append((edit, Removal(STUB, " ".join(_text(pin.node).split()))))
    edits = [edit for edit, _ in stubs]
    removals = [Removal(TEST, t.name) for t in deleted] + [removal for _, removal in stubs]
    if not removals:
        return source, []
    result = source
    # A block every statement of which goes keeps a `pass` in place of its
    # first test: Python has no empty suite.
    emptied: dict[int, _Test] = {}
    by_block: dict[int, list[_Test]] = {}
    for test in deleted:
        if test.outer.parent is not None:
            by_block.setdefault(test.outer.parent.id, []).append(test)
    for tests_in_block in by_block.values():
        block = tests_in_block[0].outer.parent
        statements = [c for c in block.named_children if c.type != "comment"]
        if block.type == "block" and len(statements) == len(tests_in_block):
            first = min(tests_in_block, key=lambda t: t.outer.start_byte)
            emptied[first.outer.id] = first
    # Last first, so every earlier span's offsets still hold.
    spans: list[tuple[int, int, bytes | _Test]] = [
        *edits,
        *(
            _filler(t.outer, data) if t.outer.id in emptied
            else (mock_stubs._with_leading_comments(t.outer, data), t.outer.end_byte, t)
            for t in deleted
        ),
    ]
    for start, end, payload in sorted(spans, key=lambda span: span[0], reverse=True):
        if isinstance(payload, _Test):
            result = _delete_lines(result, start, end)
        else:
            encoded = result.encode("utf-8")
            result = (encoded[:start] + payload + encoded[end:]).decode("utf-8")
    if mock_stubs._parse_errors(syntax._parse(result, "python")) > mock_stubs._parse_errors(root):
        return source, []
    return result, removals


def _filler(outer: Node, data: bytes) -> tuple[int, int, bytes]:
    """The edit replacing ``outer``'s whole lines with a ``pass`` at its indent."""
    start = python_patches._line_start(data, outer.start_byte)
    end = python_patches._line_after(data, outer.end_byte)
    return start, end, data[start : outer.start_byte] + b"pass\n"


def _delete_lines(source: str, start: int, end: int) -> str:
    """``source`` without the whole lines ``start``..``end`` cover, leaving the
    blank lines a formatter would: as many as the wider gap beside it held
    (one between methods, two between top-level definitions), none after a
    block's ``:`` and none at the end of the file."""
    data = source.encode("utf-8")
    start = data.rfind(b"\n", 0, start) + 1
    end = python_patches._line_after(data, max(end - 1, start))
    before = start
    while before > 0:
        previous = data.rfind(b"\n", 0, before - 1) + 1
        if data[previous:before].strip():
            break
        before = previous
    after = end
    while after < len(data):
        following = python_patches._line_after(data, after)
        if data[after:following].strip():
            break
        after = following
    opener = data[:before].rstrip().endswith(b":")
    gap = 0 if opener or after >= len(data) else max(
        data[before:start].count(b"\n"), data[end:after].count(b"\n")
    )
    return (data[:before] + b"\n" * gap + data[after:]).decode("utf-8")


def _owner(node: Node) -> str:
    """`` on class X`` / `` in function f``: where a pin sits, by name rather
    than by line, since the line is the transformed file's and the refusal
    hands the customer's back."""
    current = node.parent
    while current is not None:
        if current.type in ("class_definition", "function_definition"):
            kind = "class" if current.type == "class_definition" else "function"
            return f" in {kind} {_text(current.child_by_field_name('name'))}"
        if current.type == "decorated_definition":
            inner = current.child_by_field_name("definition")
            if inner is not None:
                kind = "class" if inner.type == "class_definition" else "function"
                return f" on {kind} {_text(inner.child_by_field_name('name'))}"
        current = current.parent
    return ""


def refusal_reason(
    source: str, flag_key: str, treatment: bool, key_texts: Iterable[str] = ()
) -> str | None:
    """Why a pin of ``flag_key`` left in ``source`` stops the removal, or ``None``.

    Asked after the transform, of a run that folded a read: a pin of the
    removed value, or of a value that is not a literal, keeps a test on the
    branch that is gone.
    """
    if not any(name in source for name in _PINS):
        return None
    for call in _calls(syntax._parse(source, "python"), flag_key, frozenset(key_texts)):
        value = _value(call)
        if value is treatment:
            continue
        where = " ".join(_text(call).split())
        what = "the removed value" if value is not None else "a value this run cannot read"
        return (
            f"{where}{_owner(call)} pins the flag to {what} outside a single test this run "
            "can delete, so the tests it covers would fail once the branch is gone"
        )
    return None
