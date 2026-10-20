"""Remove a configuration key from an ``appsettings*.json`` file, keeping
everything else byte for byte.

.NET reads these files with comments skipped and trailing commas allowed,
so they are JSON with comments, and a customer's formatting, comments and
comma style are theirs. Nothing here re-serialises: a member is cut out of
the text where it stands (its own lines when it has them, its own span on a
shared line), with the comma that separated it, and the result is parsed
again and compared with the original less exactly the removed keys. Any
difference, or anything this cannot parse, is a refusal, never a guess.

Keys are matched as the configuration binder matches them: ordinally,
ignoring case. Two keys of one object that differ only in case are a
refusal (which one binds is the provider's business).
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ConfigError(ValueError):
    """The file cannot be edited safely."""


@dataclass
class _Node:
    start: int
    end: int


@dataclass
class _Scalar(_Node):
    text: str = ""


@dataclass
class _Member:
    key: str
    start: int
    value: _Node
    #: Offset of the comma after the value, if there is one.
    comma: int | None = None

    @property
    def end(self) -> int:
        return self.value.end


@dataclass
class _Object(_Node):
    members: list[_Member] = field(default_factory=list)


@dataclass
class _Array(_Node):
    items: list[_Node] = field(default_factory=list)


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.at = 0

    def fail(self, why: str) -> ConfigError:
        line = self.text.count("\n", 0, self.at) + 1
        return ConfigError(f"line {line}: {why}")

    def skip(self) -> None:
        text = self.text
        while self.at < len(text):
            char = text[self.at]
            if char in " \t\r\n﻿":
                self.at += 1
            elif text.startswith("//", self.at):
                end = text.find("\n", self.at)
                self.at = len(text) if end == -1 else end
            elif text.startswith("/*", self.at):
                end = text.find("*/", self.at + 2)
                if end == -1:
                    raise self.fail("an unterminated comment")
                self.at = end + 2
            else:
                return

    def string(self) -> tuple[str, int, int]:
        start = self.at
        text = self.text
        if text[start] != '"':
            raise self.fail("a string was expected")
        index = start + 1
        out: list[str] = []
        while index < len(text):
            char = text[index]
            if char == '"':
                self.at = index + 1
                return "".join(out), start, self.at
            if char == "\\":
                if index + 1 >= len(text):
                    break
                escape = text[index + 1]
                if escape == "u":
                    digits = text[index + 2 : index + 6]
                    if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                        raise self.fail("a malformed \\u escape")
                    out.append(chr(int(digits, 16)))
                    index += 6
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(escape, escape))
                index += 2
                continue
            if char == "\n":
                break
            out.append(char)
            index += 1
        raise self.fail("an unterminated string")

    def value(self) -> _Node:
        self.skip()
        if self.at >= len(self.text):
            raise self.fail("a value was expected")
        char = self.text[self.at]
        if char == "{":
            return self.object()
        if char == "[":
            return self.array()
        if char == '"':
            text, start, end = self.string()
            return _Scalar(start, end, '"' + text)
        start = self.at
        while self.at < len(self.text) and self.text[self.at] not in ",}] \t\r\n/":
            self.at += 1
        word = self.text[start : self.at]
        if not word:
            raise self.fail(f"an unexpected {char!r}")
        return _Scalar(start, self.at, word)

    def object(self) -> _Object:
        node = _Object(self.at, self.at)
        self.at += 1
        while True:
            self.skip()
            if self.at >= len(self.text):
                raise self.fail("an unterminated object")
            if self.text[self.at] == "}":
                self.at += 1
                node.end = self.at
                return node
            key, start, _ = self.string()
            self.skip()
            if self.at >= len(self.text) or self.text[self.at] != ":":
                raise self.fail("a ':' was expected")
            self.at += 1
            member = _Member(key, start, self.value())
            node.members.append(member)
            self.skip()
            if self.at < len(self.text) and self.text[self.at] == ",":
                member.comma = self.at
                self.at += 1

    def array(self) -> _Array:
        node = _Array(self.at, self.at)
        self.at += 1
        while True:
            self.skip()
            if self.at >= len(self.text):
                raise self.fail("an unterminated array")
            if self.text[self.at] == "]":
                self.at += 1
                node.end = self.at
                return node
            node.items.append(self.value())
            self.skip()
            if self.at < len(self.text) and self.text[self.at] == ",":
                self.at += 1


def _parse(text: str) -> _Node:
    parser = _Parser(text)
    root = parser.value()
    parser.skip()
    if parser.at != len(text):
        raise parser.fail("text after the document")
    return root


def _plain(node: _Node) -> object:
    """``node`` as values only, keys folded to lower case as the binder
    folds them, so layout and comments do not count."""
    if isinstance(node, _Object):
        return tuple((m.key.lower(), _plain(m.value)) for m in node.members)
    if isinstance(node, _Array):
        return ("[", tuple(_plain(i) for i in node.items))
    return node.text if isinstance(node, _Scalar) else None


def _find(node: _Node, path: list[str]) -> _Object | None:
    """The object at ``path`` (keys ignoring case), or ``None`` when the
    path is absent. A path through a non-object, or an object holding a key
    twice in different cases, refuses."""
    current = node
    for key in path:
        if not isinstance(current, _Object):
            raise ConfigError(f"{key!r} sits under a value that is not an object")
        matches = [m for m in current.members if m.key.lower() == key.lower()]
        if len(matches) > 1:
            raise ConfigError(f"the key {key!r} is written {len(matches)} times")
        if not matches:
            return None
        current = matches[0].value
    if not isinstance(current, _Object):
        raise ConfigError(f"{':'.join(path)} is not an object")
    return current


def _line_start(text: str, at: int) -> int:
    return text.rfind("\n", 0, at) + 1


def _cut(text: str, parent: _Object, member: _Member) -> tuple[int, int, list[int]]:
    """The span to delete for ``member`` of ``parent``, and any earlier
    commas to delete with it (the one before a last member that had none
    of its own: the file had no trailing comma, and keeps none)."""
    index = parent.members.index(member)
    before = text[_line_start(text, member.start) : member.start]
    end = (member.comma + 1) if member.comma is not None else member.end
    rest_end = end
    while rest_end < len(text) and text[rest_end] in " \t":
        rest_end += 1
    if text.startswith("//", rest_end):
        newline = text.find("\n", rest_end)
        rest_end = len(text) if newline == -1 else newline
    own_line = not before.strip() and (rest_end >= len(text) or text[rest_end] in "\r\n")
    commas: list[int] = []
    last = index == len(parent.members) - 1
    if last and member.comma is None and index > 0:
        previous = parent.members[index - 1]
        if previous.comma is not None:
            commas.append(previous.comma)
    if own_line:
        start = _line_start(text, member.start)
        stop = rest_end
        if stop < len(text) and text[stop] == "\r":
            stop += 1
        if stop < len(text) and text[stop] == "\n":
            stop += 1
        return start, stop, commas
    # Shared line: the member and the spaces after its comma, or, for a
    # last member, the comma and spaces before it.
    stop = end
    while stop < len(text) and text[stop] in " \t":
        stop += 1
    start = member.start
    if last and member.comma is None and commas:
        start = commas.pop()
    return start, stop if member.comma is not None else end, commas


def remove_keys(text: str, section: list[str], keys: list[str]) -> tuple[str, list[str]]:
    """``text`` without each of ``keys`` (ignoring case) under the object at
    ``section``, and the section itself when that leaves it empty; and the
    ``Section:Key`` paths removed. Unchanged, with no paths, when the
    section or every key is absent. Raises :class:`ConfigError` when the
    file cannot be edited exactly."""
    root = _parse(text)
    target = _find(root, section)
    if target is None:
        return text, []
    wanted = {k.lower() for k in keys}
    doomed = [m for m in target.members if m.key.lower() in wanted]
    for key in wanted:
        if sum(1 for m in target.members if m.key.lower() == key) > 1:
            raise ConfigError(f"the key {key!r} is written more than once")
    if not doomed:
        return text, []
    removed = [":".join([*section, m.key]) for m in doomed]
    if len(doomed) == len(target.members):
        # The section holds nothing else: it goes as a whole.
        parent = _find(root, section[:-1]) if len(section) > 1 else root
        if not isinstance(parent, _Object):
            raise ConfigError("the section sits under a value that is not an object")
        holder = next(m for m in parent.members if m.key.lower() == section[-1].lower())
        cuts = [_cut(text, parent, holder)]
        expected = _without(_plain(root), [k.lower() for k in section], None)
    else:
        cuts = []
        # Cut from the last member back, so earlier offsets stay valid; a
        # middle member's own comma goes with it, and only the final
        # member can need the comma before it.
        survivors = [m for m in target.members if m not in doomed]
        for member in doomed:
            trimmed = _Object(target.start, target.end, [m for m in target.members if m in survivors or m is member])
            cuts.append(_cut(text, trimmed, member))
        expected = _without(_plain(root), [k.lower() for k in section], wanted)
    edits: list[tuple[int, int]] = []
    for start, stop, commas in cuts:
        edits.append((start, stop))
        edits += [(c, c + 1) for c in commas]
    edits.sort()
    for (_, a_end), (b_start, _) in zip(edits, edits[1:]):
        if b_start < a_end:
            raise ConfigError("two cuts overlap")
    out = text
    for start, stop in reversed(edits):
        out = out[:start] + out[stop:]
    try:
        after = _parse(out)
    except ConfigError as exc:
        raise ConfigError(f"the edit would not parse: {exc}") from exc
    if _plain(after) != expected:
        raise ConfigError("the edit would change more than the removed keys")
    return out, removed


def _without(plain: object, path: list[str], keys: set[str] | None) -> object:
    """``plain`` (see :func:`_plain`) less ``keys`` under ``path``, or less
    the whole of ``path`` when ``keys`` is ``None``."""
    assert isinstance(plain, tuple)
    head, rest = path[0], path[1:]
    out = []
    for key, value in plain:
        if key != head:
            out.append((key, value))
        elif not rest and keys is None:
            continue
        elif not rest:
            out.append((key, tuple((k, v) for k, v in value if k not in keys)))
        else:
            out.append((key, _without(value, rest, keys)))
    return tuple(out)
