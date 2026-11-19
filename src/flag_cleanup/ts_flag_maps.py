"""The ``flag-maps`` input: names of objects whose keys are flag keys.

``flags[KEY]`` (``this.flags[KEY]``, ``props.flags[KEY]``) is a read of the flag
when ``flags`` is listed and KEY is the key's string literal or a constant of the
same file. Every object with a listed name, in every file and scope, is taken to
be the flag map: nothing here can tell a flag map from another object of the
same name, so a name should be distinctive.

Two halves, which must agree:

* The RULES (``flag_map__*`` blocks of ``ts_key_positions.toml`` and
  ``ts_const_key_positions.toml``, cloned per name by
  :func:`rule_synthesis.with_flag_maps`) fold the read, and only in a
  WHITELIST of positions (:func:`is_flag_map_read` states the same list in code,
  and a test feeds one corpus to both): the condition of an ``if``, a ``while``
  or a ternary, the operand of ``!``, and an operand of ``&&``/``||`` that is
  itself in such a position, nested at most :data:`OPERAND_DEPTH` deep, with at
  most one pair of parentheses around the whole. Every other position (a value, a
  comparison, an argument, a ``return``, a write, a call) is left as written.
* KEY ACCOUNTING over the whole checkout, which applies only to a run with at
  least one such read (:func:`has_read`). A fold is sound only if nothing else
  writes the flag into the map, and a write has to spell the key somewhere, so:
  before the transform (:func:`refusals`) a key that is spelled as an object
  property, a ``Map`` entry or the argument of ``.set``/``Reflect.*``/
  ``Object.defineProperty``, anywhere in the checkout, or any occurrence of it
  in a file type no pass parses, refuses the flag; after it
  (:func:`survivors`) any code file that still holds the key refuses it too, and
  the rewrite is rolled back. Not covered: a key assembled at the write site
  with no spelling of it (`` `token-${'swap'}` ``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
import re
from contextlib import contextmanager
from pathlib import Path

from tree_sitter import Node

#: How many ``&&``/``||`` operands the whitelist climbs through.
OPERAND_DEPTH = 3

_declared: frozenset[str] = frozenset()


@contextmanager
def declaring(names: Iterable[str]) -> Iterator[None]:
    """The ``flag-maps`` names of this run, for the scope."""
    global _declared
    previous = _declared
    _declared = frozenset(names)
    try:
        yield
    finally:
        _declared = previous


def names() -> frozenset[str]:
    return _declared


def _text(node: Node) -> str:
    return node.text.decode("utf-8")


def _descendants(node: Node) -> Iterator[Node]:
    stack = list(node.children)
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _ends_in(obj: Node | None) -> str | None:
    """The name an object expression ends in (``flags``, ``a.flags``), calls and all."""
    if obj is None:
        return None
    if obj.type == "identifier":
        return _text(obj)
    if obj.type == "member_expression":
        prop = obj.child_by_field_name("property")
        if prop is not None and prop.type == "property_identifier":
            return _text(prop)
    return None


def _map_name(obj: Node | None) -> str | None:
    """:func:`_ends_in`, when the chain holds no call, no ``?.`` and no comment."""
    name = _ends_in(obj)
    if name is None:
        return None
    if any(n.type in ("call_expression", "optional_chain", "comment") for n in _descendants(obj)):
        return None
    return name


def _is_and_or(node: Node) -> bool:
    return node.type == "binary_expression" and any(c.type in ("&&", "||") for c in node.children)


def _in_slot(node: Node) -> bool:
    """Whether ``node`` is the condition of an ``if``/``while``/ternary or the operand of ``!``."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "parenthesized_expression" and parent.parent is not None:
        # `if (X)` / `while (X)`: the condition IS this parenthesised expression.
        grand = parent.parent
        if grand.type in ("if_statement", "while_statement"):
            cond = grand.child_by_field_name("condition")
            return cond is not None and cond.id == parent.id
        return False
    if parent.type == "ternary_expression":
        cond = parent.child_by_field_name("condition")
        return cond is not None and cond.id == node.id
    if parent.type == "unary_expression":
        arg = parent.child_by_field_name("argument")
        return arg is not None and arg.id == node.id and any(c.type == "!" for c in parent.children)
    return False


def whitelisted(sub: Node) -> bool:
    """Whether the subscript ``sub`` sits where the rules fold it."""
    node = sub
    for _ in range(OPERAND_DEPTH):
        parent = node.parent
        if parent is not None and _is_and_or(parent):
            node = parent
        else:
            break
    if _in_slot(node):
        return True
    # One pair of parentheses around the whole.
    parent = node.parent
    return parent is not None and parent.type == "parenthesized_expression" and _in_slot(parent)


def is_flag_map_read(index: Node, flag_maps: Iterable[str] | None = None) -> bool:
    """Whether ``index`` (a string literal or identifier) is the index of a
    ``flags[KEY]`` read the rules fold, for a map listed in ``flag_maps``."""
    listed = _declared if flag_maps is None else frozenset(flag_maps)
    sub = index.parent
    if sub is None or sub.type != "subscript_expression" or not listed:
        return False
    idx = sub.child_by_field_name("index")
    if idx is None or idx.id != index.id:
        return False
    name = _map_name(sub.child_by_field_name("object"))
    if name is None or name not in listed:
        return False
    if any(c.type == "optional_chain" for c in sub.children):
        return False
    if any(n.type == "comment" for n in _descendants(sub)):
        return False
    return whitelisted(sub)


# ---------------------------------------------------------------------------
# The proof: the map is never written, and never bound to a literal holding the key.
# ---------------------------------------------------------------------------

_TRANSPARENT = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression", "type_assertion"}
)


def _climb(node: Node) -> Node:
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        node = node.parent
    return node


def _use(node: Node) -> str | None:
    """How the (climbed) subscript ``node`` is used when that is a write or an
    object use, else ``None`` (a value read)."""
    parent = node.parent
    if parent is None:
        return None
    kind = parent.type

    def field(name: str) -> bool:
        child = parent.child_by_field_name(name)
        return child is not None and child.id == node.id

    if kind in ("assignment_expression", "augmented_assignment_expression") and field("left"):
        return "is assigned to"
    if kind == "update_expression" and field("argument"):
        return "is incremented or decremented"
    if kind == "unary_expression" and field("argument") and any(c.type == "delete" for c in parent.children):
        return "is deleted"
    if kind == "for_in_statement" and field("left"):
        return "is the target of a `for` loop"
    if kind in ("array_pattern", "rest_pattern"):
        return "is a destructuring target"
    if kind == "pair_pattern" and field("value"):
        return "is a destructuring target"
    if kind == "assignment_pattern" and field("left"):
        return "is a destructuring target"
    if kind == "call_expression" and field("function"):
        return "is called"
    if kind == "new_expression" and field("constructor"):
        return "is constructed"
    if kind == "member_expression" and field("object"):
        return "has a member read off it"
    if kind == "subscript_expression" and field("object"):
        return "is subscripted"
    return None


def _language(path: str) -> str:
    if path.endswith(".tsx"):
        return "tsx"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "js"
    return "ts"


def _fragments(node: Node) -> list[str]:
    return [_text(n) for n in _descendants(node) if n.type == "string_fragment"]


def _key_constants(root: Node, flag_key: str) -> frozenset[str]:
    from flag_cleanup import key_const  # lazy: key_const imports this module

    found = set()
    for node in [root, *_descendants(root)]:
        if node.type != "variable_declarator":
            continue
        name = node.child_by_field_name("name")
        value = key_const.ts_key_literal(node.child_by_field_name("value"))
        if name is not None and name.type == "identifier" and value is not None and _string_value(value) == flag_key:
            found.add(_text(name))
    return frozenset(found)


def _string_value(node: Node) -> str:
    """The text a string literal spells, with escapes written out."""
    text = _text(node)
    return decoded(text[1:-1]) if text[:1] in "'\"" else "".join(_fragments(node))


def _holds_key(node: Node, flag_key: str, consts: frozenset[str]) -> bool:
    if flag_key in decoded(_text(node)):
        return True
    return any(n.type == "identifier" and _text(n) in consts for n in _descendants(node))


def _is_key(index: Node | None, flag_key: str, consts: frozenset[str]) -> bool:
    if index is None:
        return False
    if index.type == "string":
        return _string_value(index) == flag_key
    if index.type == "template_string":
        return _text(index) == f"`{flag_key}`"
    return index.type == "identifier" and _text(index) in consts


#: Code the run parses, and file types it cannot: a key in one of the second
#: kind might be written into the map by a framework, so it refuses the flag.
CODE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
UNPARSED_SUFFIXES = (
    ".vue", ".svelte", ".astro", ".mdx", ".html", ".htm", ".ejs", ".hbs", ".handlebars",
    ".njk", ".liquid", ".pug", ".json", ".jsonc", ".json5",
)


def _files(root: str, suffixes: tuple[str, ...]) -> list[str]:
    return [p for p in listing(root) if p.endswith(suffixes)]


def _read(path: str) -> str | None:
    try:
        return open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return None


def has_read(paths: Iterable[str], flag_key: str, flag_maps: Iterable[str] | None = None) -> bool:
    """Whether some file of ``paths`` holds a read of ``<map>[KEY]`` the rules fold."""
    from flag_cleanup import ts_syntax

    listed = _declared if flag_maps is None else frozenset(flag_maps)
    for path in paths:
        source = _read(path) if path.endswith(CODE_SUFFIXES) else None
        if not listed or source is None or flag_key not in source:
            continue
        root = ts_syntax._parse(source, _language(path))
        consts = _key_constants(root, flag_key)
        for node in [root, *_descendants(root)]:
            if node.type != "subscript_expression":
                continue
            index = node.child_by_field_name("index")
            if _is_key(index, flag_key, consts) and is_flag_map_read(index, listed):
                return True
    return False


#: The only files whose mention of the key does not refuse the flag: prose a
#: build never reads. `.mdx` is not among them (it is code), nor is `.txt`
#: (runtime-loaded override lists use it), nor any other suffix, known or not.
_INERT_SUFFIXES = (".md", ".rst")
_INERT_NAMES = frozenset({"CHANGELOG", "LICENSE", "LICENCE", "COPYING"})
_SKIP = frozenset({".git", "node_modules"})
_CHUNK = 1 << 20

_ESCAPES = (
    (re.compile(r"\\\r?\n"), None),  # a line continuation spells nothing
    (re.compile(r"\\x([0-9a-fA-F]{2})"), 16),
    (re.compile(r"\\u\{([0-9a-fA-F]{1,6})\}"), 16),
    (re.compile(r"\\u([0-9a-fA-F]{4})"), 16),
    (re.compile(r"\\([0-7]{1,3})"), 8),
    (re.compile(r"&#[xX]([0-9a-fA-F]+);?"), 16),
    (re.compile(r"&#([0-9]+);?"), 10),
    (re.compile(r"%([0-9a-fA-F]{2})"), 16),
    (re.compile(r"\\([^xu0-7\r\n])"), 0),  # an identity escape: `\-` is `-`
)
_NAMED = (("&hyphen;", "-"), ("&dash;", "-"))
_PASSES = 4


def _decode_once(text: str) -> str:
    def make(base: int):
        def sub(match: re.Match[str]) -> str:
            if base == 0:
                return match.group(1)
            try:
                return chr(int(match.group(1), base))
            except (ValueError, OverflowError):
                return match.group(0)

        return sub

    for pattern, base in _ESCAPES:
        text = pattern.sub("" if base is None else make(base), text)
    for name, char in _NAMED:
        text = text.replace(name, char)
    return text


def decoded(text: str) -> str:
    """``text`` with the spellings a key can hide behind written out: the
    JavaScript escapes (``\\xHH``, ``\\uHHHH``, ``\\u{H…}``, octal, an identity
    escape ``\\-`` and a line continuation), HTML numeric entities (the ``;`` is
    optional) and ``&hyphen;``/``&dash;``, and ``%HH``, to a fixpoint of at most
    :data:`_PASSES` passes so that a double encoding (``%26%2345%3B``) resolves."""
    for _ in range(_PASSES):
        after = _decode_once(text)
        if after == text:
            break
        text = after
    return text


def _inert(name: str, head: bytes) -> bool:
    if name.endswith(_INERT_SUFFIXES):
        return True
    return name in _INERT_NAMES and not head.startswith(b"#!")


def _roots(checkout_roots: str | Iterable[str]) -> list[str]:
    roots = [checkout_roots] if isinstance(checkout_roots, str) else list(checkout_roots)
    return list(dict.fromkeys(roots))


def listing(root: str) -> list[str]:
    """Regular files of the checkout at ``root``, as absolute paths.

    Inside a git checkout, what git lists (tracked and untracked, not ignored):
    gitignored build output and caches cannot ship, so they cannot write the map.
    Outside one, a walk that skips ``.git`` and ``node_modules``. Symlinks,
    FIFOs, devices and sockets are never returned (``lstat``), never followed.
    """
    import os
    import stat
    import subprocess

    names: list[str] = []
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, check=True, timeout=120,
        ).stdout
        names = [os.path.join(root, n) for n in out.decode("utf-8", "surrogateescape").split("\0") if n]
        # A submodule is one entry (a gitlink) in its parent: list it as a checkout of its own.
        staged = subprocess.run(
            ["git", "-C", root, "ls-files", "-s", "-z"], capture_output=True, check=True, timeout=120
        ).stdout.decode("utf-8", "surrogateescape")
        for entry in filter(None, staged.split("\0")):
            meta, _, rel = entry.partition("\t")
            if meta.startswith("160000"):
                sub = os.path.join(root, rel)
                if os.path.isdir(sub):
                    names.extend(listing(sub))
    except (OSError, subprocess.SubprocessError):
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP]
            names.extend(os.path.join(current, f) for f in files)
    regular = []
    for path in names:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode):
            regular.append(path)
    return regular


#: The references to this Action that the README tells customers to use. Matched
#: exactly, up to the `@ref` (or the `:tag` of an image).
_OWN_ACTIONS = frozenset(
    {
        "canopy-labs/featureflip-flag-cleanup-action",
        "docker://ghcr.io/canopy-labs/featureflip-flag-cleanup-action",
    }
)


def _is_own_action(uses: object) -> bool:
    if not isinstance(uses, str):
        return False
    reference = uses.strip()
    base = reference.split("@", 1)[0]
    if base.startswith("docker://"):
        base = base.rsplit(":", 1)[0] if ":" in base[len("docker://"):] else base
    return base in _OWN_ACTIONS


def _workflow_items(path: str, data_text: str, flag_key: str) -> int | None:
    """How many occurrences of the key in a customer's workflow file are items of the
    ``flags`` input of a step that uses this Action; ``None`` when the file cannot
    be parsed, 0 when it holds a YAML anchor or alias (which can route a value to
    anywhere)."""
    import yaml

    try:
        events = list(yaml.parse(data_text))
        document = yaml.safe_load(data_text)
    except yaml.YAMLError:
        return None
    if any(isinstance(e, yaml.AliasEvent) or getattr(e, "anchor", None) for e in events):
        return 0
    count = 0
    jobs = document.get("jobs") if isinstance(document, dict) else None
    for job in (jobs or {}).values() if isinstance(jobs, dict) else ():
        for step in (job.get("steps") or []) if isinstance(job, dict) else ():
            if not isinstance(step, dict) or not _is_own_action(step.get("uses")):
                continue
            flags = (step.get("with") or {}).get("flags") if isinstance(step.get("with"), dict) else None
            if isinstance(flags, str):
                count += sum(item.strip() == flag_key for item in re.split(r"[,\n]", flags))
    return count


def _is_workflow(path: str) -> bool:
    norm = path.replace("\\", "/")
    return "/.github/workflows/" in norm and norm.endswith((".yml", ".yaml"))


def _occurrences(path: str, flag_key: str) -> tuple[int, int] | None:
    """``(line of the first occurrence, how many)`` of the key in the file, spelled
    raw or through :func:`decoded`; ``None`` when absent. Read in chunks, each
    carrying the end of the last so a spelling cannot hide across a boundary."""
    overlap = max(len(flag_key.encode("utf-8")) * 48, 512)
    pattern = re.compile(re.escape(flag_key))
    first = None
    total = 0
    lines = 0
    tail = b""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            window = tail + chunk
            if not window:
                break
            view = decoded(window.decode("utf-8", "replace"))
            carried = len(decoded(tail.decode("utf-8", "replace"))) if tail else 0
            for match in pattern.finditer(view):
                if match.end() <= carried:
                    continue
                total += 1
                if first is None:
                    first = lines + view[: match.start()].count("\n") + 1
            if len(chunk) < _CHUNK:
                break
            keep = window[-overlap:]
            lines += window[: len(window) - len(keep)].count(b"\n")
            tail = keep
    return (first or 1, total) if total else None


def survivors(checkout_roots: str | Iterable[str], flag_key: str) -> Mapping[str, str]:
    """``{path: reason}`` for each file of the checkout(s) that still holds the key.

    Asked ONCE, after every pass of the flag has run, of the whole checkout
    (:func:`flag_cleanup.piranha_runner.checkout_root`, the union over every
    configured directory), whatever ``directories`` and ``languages`` said. The
    key counts however it is spelled: raw, or through :func:`decoded`. Files that
    may keep it: ``.md`` and ``.rst``, the extension-less ``CHANGELOG``/``LICENSE``/
    ``LICENCE``/``COPYING`` (unless they start ``#!``), and the key as an item of
    the ``flags`` input of this Action in a workflow file. A file that cannot be
    read is refused by name, never skipped.
    """
    import os

    found: dict[str, str] = {}
    for root in _roots(checkout_roots):
        for path in listing(root):
            if path in found:
                continue
            try:
                with open(path, "rb") as handle:
                    head = handle.read(2)
                if _inert(os.path.basename(path), head):
                    continue
                hit = _occurrences(path, flag_key)
                if hit is None:
                    continue
                line, count = hit
                if _is_workflow(path):
                    allowed = _workflow_items(path, open(path, encoding="utf-8").read(), flag_key)
                    if allowed is not None and allowed >= count:
                        continue
                    if allowed is None:
                        found[path] = f"line {line}: a workflow file that cannot be read as YAML holds the key"
                        continue
            except FileNotFoundError:
                continue
            except (OSError, MemoryError, UnicodeDecodeError) as error:
                found[path] = f"could not be read to check it ({type(error).__name__}), so it is refused"
                continue
            found[path] = (
                f"line {line}: the key is still spelled here after the fold, so something besides "
                "the folded reads of a `flag-maps` map may use it"
            )
    return found


def refusals(repo_dir: str, flag_key: str, flag_maps: Iterable[str] | None = None) -> Mapping[str, str]:
    """``{path: reason}`` for each file of the whole checkout that stops the flag
    being folded through a map: a write or object use of ``<map>[KEY]``, an
    ``Object.assign`` carrying the key, a map bound to an object holding it, the
    key spelled as an object property, a ``Map`` entry or the argument of a
    ``.set``/``Reflect.*``/``Object.defineProperty`` call, or the key in a file
    type no pass parses. A value read is allowed: it is left as written.
    """
    from flag_cleanup import ts_syntax

    listed = _declared if flag_maps is None else frozenset(flag_maps)
    found: dict[str, str] = {}
    if not listed:
        return found
    for path in _files(repo_dir, UNPARSED_SUFFIXES):
        source = _read(path)
        if source is not None and flag_key in source:
            line = source[: source.index(flag_key)].count("\n") + 1
            found[path] = f"line {line}: a file type the run does not parse holds the key, and may write it into a flag map"
    code = {path: source for path in _files(repo_dir, CODE_SUFFIXES) if (source := _read(path)) is not None}
    consts: set[str] = set()
    parsed: dict[str, Node] = {}
    for path, source in code.items():
        if flag_key in source:
            parsed[path] = ts_syntax._parse(source, _language(path))
            consts |= _key_constants(parsed[path], flag_key)
    for path, source in code.items():
        if path in found or not (
            flag_key in source or any(name in source for name in consts | listed)
        ):
            continue
        root = parsed.get(path) or ts_syntax._parse(source, _language(path))
        reason = _file_reason(root, flag_key, frozenset(consts), listed)
        if reason is not None:
            found[path] = reason
    return found


_SPELLING_CALLS = frozenset({"Object.defineProperty", "Object.defineProperties", "Object.assign", "Object.fromEntries"})


def _spelling_reason(node: Node, flag_key: str, consts: frozenset[str]) -> str | None:
    """Why ``node`` (a string or identifier spelling the key) writes it somewhere."""
    if node.type == "identifier":
        if _text(node) not in consts:
            return None
    elif node.type != "string" or _string_value(node) != flag_key:
        return None
    parent = node.parent
    if parent is None:
        return None
    if parent.type == "computed_property_name":
        node, parent = parent, parent.parent
        if parent is None:
            return None
    if parent.type == "pair":
        key = parent.child_by_field_name("key")
        return "an object holds the key as a property" if key is not None and key.id == node.id else None
    if parent.type == "array" and parent.parent is not None and parent.parent.type in ("array", "arguments"):
        first = parent.named_children[0] if parent.named_children else None
        return "a `Map` entry holds the key" if first is not None and first.id == node.id else None
    if parent.type == "arguments" and parent.parent is not None and parent.parent.type == "call_expression":
        callee = parent.parent.child_by_field_name("function")
        name = _text(callee) if callee is not None else ""
        if name.endswith(".set") or name.startswith("Reflect.") or name in _SPELLING_CALLS:
            return f"the key is passed to `{name}`"
    return None


def _literal_constants(root: Node) -> Mapping[str, str]:
    """``{name: value}`` of the file's constants holding a plain string."""
    from flag_cleanup import key_const

    found = {}
    for node in [root, *_descendants(root)]:
        if node.type != "variable_declarator":
            continue
        name = node.child_by_field_name("name")
        value = key_const.ts_key_literal(node.child_by_field_name("value"))
        if name is not None and name.type == "identifier" and value is not None:
            found[_text(name)] = "".join(_fragments(value))
    return found


def _is_plain_index(index: Node | None, flag_key: str, literals: Mapping[str, str]) -> bool:
    """Whether a write's index is something the run can read: a string, a number or a
    constant of a known value, none of which is the key (those are handled apart)."""
    if index is None:
        return False
    if index.type == "string":
        return not any(c.type == "escape_sequence" for c in index.children)
    if index.type == "number":
        return True
    return index.type == "identifier" and _text(index) in literals and literals[_text(index)] != flag_key


def _aliases(root: Node, listed: frozenset[str]) -> frozenset[str]:
    """Names bound to a listed map (``const t = flags``), through any number of hops."""
    found = set(listed)
    changed = True
    while changed:
        changed = False
        for node in [root, *_descendants(root)]:
            if node.type not in ("variable_declarator", "assignment_expression"):
                continue
            target = node.child_by_field_name("name") or node.child_by_field_name("left")
            value = node.child_by_field_name("value") or node.child_by_field_name("right")
            if target is not None and target.type == "identifier" and _text(target) not in found:
                if value is not None and _ends_in(value) in found:
                    found.add(_text(target))
                    changed = True
    return frozenset(found)


def _file_reason(root: Node, flag_key: str, consts: frozenset[str], listed: frozenset[str]) -> str | None:
    maps = _aliases(root, listed)
    literals = _literal_constants(root)
    for node in [root, *_descendants(root)]:
        line = node.start_point[0] + 1
        if node.type == "subscript_expression":
            name = _ends_in(node.child_by_field_name("object"))
            if name in maps and _is_key(node.child_by_field_name("index"), flag_key, consts) and not (
                is_flag_map_read(node.child_by_field_name("index"), listed)
            ):
                how = _use(_climb(node))
                if how is not None:
                    return f"line {line}: `{name}[…]` holds the flag's key and {how}, so `{name}` is not a read-only flag map"
            elif name in maps and not _is_plain_index(node.child_by_field_name("index"), flag_key, literals):
                how = _use(_climb(node))
                if how is not None:
                    return (
                        f"line {line}: `{name}[…]` {how} with a key the run cannot read, so it may be the flag's"
                    )
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn is not None and args is not None and _text(fn) == "Object.assign" and len(args.named_children) > 1:
                target = _ends_in(args.named_children[0])
                if target in listed and any(_holds_key(a, flag_key, consts) for a in args.named_children[1:]):
                    return f"line {line}: `Object.assign` writes the flag's key onto `{target}`"
        elif node.type in ("variable_declarator", "public_field_definition", "field_definition", "pair"):
            name_node = node.child_by_field_name("name") or node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            if (
                name_node is not None
                and value is not None
                and _text(name_node).lstrip("#") in listed
                and value.type in ("object", "new_expression")
                and _holds_key(value, flag_key, consts)
            ):
                return f"line {line}: `{_text(name_node)}` is bound to a value that holds the flag's key"
        if node.type in ("string", "identifier"):
            why = _spelling_reason(node, flag_key, consts)
            if why is not None:
                return f"line {line}: {why}, which a `flag-maps` fold cannot account for"
    return None
