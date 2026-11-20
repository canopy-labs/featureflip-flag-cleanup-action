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


def has_cross_file_read(readers: Mapping[str, Iterable[object]], flag_maps: Iterable[str] | None = None) -> bool:
    """Whether a reader holds ``<map>[REF]`` the reader rules fold, for REF one
    of the references proven in it (``(path, refs)``, each ref with ``owner``
    and ``member``): the cross-file half of :func:`has_read`. The rules match a
    reference by its text, so the text is what is asked for here too."""
    from flag_cleanup import ts_syntax

    listed = _declared if flag_maps is None else frozenset(flag_maps)
    if not listed:
        return False
    for path, refs in readers.items():
        texts = {f"{r.owner}.{r.member}" if r.owner is not None else r.member for r in refs if not getattr(r, "accessor", "")}
        source = _read(path) if path.endswith(CODE_SUFFIXES) else None
        if not texts or source is None:
            continue
        root = ts_syntax._parse(source, _language(path))
        for node in [root, *_descendants(root)]:
            if node.type != "subscript_expression":
                continue
            index = node.child_by_field_name("index")
            if index is not None and _text(index) in texts and is_flag_map_read(index, listed):
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


#: The published GitLab CI template, as the README tells customers to include
#: it: a ``remote`` of this exact URL, at any ref that is not a ``.``/``..`` segment.
_OWN_TEMPLATE = re.compile(
    r"https://raw\.githubusercontent\.com/canopy-labs/featureflip-flag-cleanup-action/"
    r"(?!\.\.?/)[A-Za-z0-9._-]+/gitlab/templates/flag-cleanup\.yml"
)
#: The job the template defines.
_TEMPLATE_JOB = "featureflip-flag-cleanup"
#: The keys a GitLab job extending (or overriding) the template's job, and every
#: job it extends in turn, may set and still have its ``FEATUREFLIP_FLAGS``
#: exempt: none of them runs a command.
_TEMPLATE_JOB_KEYS = frozenset(
    {"extends", "variables", "stage", "rules", "tags", "timeout", "needs", "interruptible",
     "retry", "when", "allow_failure", "resource_group"}
)
#: The only keys a top-level ``default:`` may set: everything else it sets (a
#: ``before_script``, ``hooks``, ``services``) runs in the template's job.
_DEFAULT_KEYS = frozenset({"image", "tags", "retry", "interruptible", "timeout", "cache", "artifacts"})
#: The only top-level keys whose value is not a mapping. Any other (the
#: deprecated global ``before_script``/``after_script``/``image``/``services``, a
#: ``script``) applies to, or runs in, every job, the template's included.
_TOP_LEVEL_LISTS = frozenset({"include", "stages"})
#: Top-level names GitLab reserves that are not jobs, other than ``include``,
#: ``stages``, ``variables``, ``workflow`` and ``default``: each refuses the
#: exemption whatever its value (a global ``image`` mapping carries an
#: ``entrypoint``; ``pages`` is a deploy job of its own).
_RESERVED_TOP_LEVEL = frozenset(
    {"image", "services", "before_script", "after_script", "script", "cache", "hooks", "pages",
     "types", "spec", "true", "false", "nil"}
)
#: The YAML core tags; any other (``!reference``, ``!!python/...``) is unreadable here.
_CORE_TAGS = frozenset(
    f"tag:yaml.org,2002:{name}" for name in ("str", "int", "float", "bool", "null", "seq", "map", "timestamp")
)
#: The published CircleCI orb, matched exactly up to the ``@version``.
_OWN_ORB = "featureflip/flag-cleanup"
#: The orb's job and command that take a ``flags`` parameter.
_ORB_ENTRY = "cleanup"


class _Scalar(str):
    """A YAML scalar's text, with the ``(start, end)`` of its source in the file."""

    span: tuple[int, int] = (0, 0)


class _NotExempt(Exception):
    """The file is YAML, but nothing in it may be exempt (a duplicated key, a merge key)."""


class _Unreadable(Exception):
    """The file is not YAML this check can read (a custom tag such as ``!reference``)."""


def _plain(node: object) -> object:
    """``node`` (composed, never constructed: no date or number is resolved, so
    nothing raises on a value) as dicts, lists and :class:`_Scalar`."""
    import yaml

    tag = getattr(node, "tag", None)
    if tag and tag not in _CORE_TAGS:
        raise _Unreadable(tag)
    if isinstance(node, yaml.ScalarNode):
        scalar = _Scalar(node.value)
        scalar.span = (node.start_mark.index, node.end_mark.index)
        return scalar
    if isinstance(node, yaml.SequenceNode):
        return [_plain(child) for child in node.value]
    if isinstance(node, yaml.MappingNode):
        mapping: dict[str, object] = {}
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.value == "<<" or key.value in mapping:
                raise _NotExempt(getattr(key, "value", key))
            mapping[key.value] = _plain(value)
        return mapping
    return None


def _items(value: object, flag_key: str, text: str) -> list[tuple[int, int]]:
    """``[span]`` of ``value`` when it is a comma- or newline-separated list holding
    the key as an item, and every spelling of the key in its source is such an
    item: no escape, no line continuation, no item that merely contains it."""
    if not isinstance(value, _Scalar):
        return []
    found = sum(item.strip() == flag_key for item in re.split(r"[,\n]", value))
    start, end = value.span
    raw = text[start:end]
    if not found or "\\" in raw or decoded(raw) != raw or raw.count(flag_key) != found:
        return []
    return [value.span]


def _github_items(document: object, flag_key: str, text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    jobs = document.get("jobs") if isinstance(document, dict) else None
    for job in (jobs or {}).values() if isinstance(jobs, dict) else ():
        for step in (job.get("steps") or []) if isinstance(job, dict) else ():
            if not isinstance(step, dict) or not _is_own_action(step.get("uses")):
                continue
            if isinstance(step.get("with"), dict):
                spans += _items(step["with"].get("flags"), flag_key, text)
    return spans


def _gitlab_items(document: object, flag_key: str, text: str) -> list[tuple[int, int]]:
    """The template's ``flags`` input where the file includes it, and
    ``FEATUREFLIP_FLAGS`` in the template's job or a job that extends it, when
    that job, every job it extends (transitively, each defined in this file) and
    the file's ``default:`` and top level set nothing that runs a command, and no
    job extends it."""
    if not isinstance(document, dict):
        return []
    includes = document.get("include")
    includes = includes if isinstance(includes, list) else [includes]
    spans: list[tuple[int, int]] = []
    included = False
    for entry in includes:
        remote = entry.get("remote") if isinstance(entry, dict) else entry
        if not (isinstance(remote, str) and _OWN_TEMPLATE.fullmatch(remote.strip())):
            continue
        included = True
        inputs = entry.get("inputs") if isinstance(entry, dict) else None
        if isinstance(inputs, dict):
            spans += _items(inputs.get("flags"), flag_key, text)
    if not included:
        return spans
    default = document.get("default")
    if default is not None and not (
        isinstance(default, dict)
        and set(default) <= _DEFAULT_KEYS
        and isinstance(default.get("image", ""), str)  # a mapping can set an `entrypoint`
    ):
        return []
    if any(
        key in _RESERVED_TOP_LEVEL or (not isinstance(value, dict) and key not in _TOP_LEVEL_LISTS)
        for key, value in document.items()
    ):
        return []

    def parents(job: object) -> list[object] | None:
        """The jobs ``job`` extends; ``None`` when ``extends`` is not a name or a list of names."""
        extends = job.get("extends") if isinstance(job, dict) else None
        if extends is None:
            return []
        listed = [extends] if isinstance(extends, str) else extends if isinstance(extends, list) else None
        return listed if listed is not None and all(isinstance(p, str) for p in listed) else None

    def inert(name: str, seen: frozenset[str]) -> bool:
        """Whether job ``name`` and everything it extends set only inert keys.
        The template's own job is inert as published; a parent not in this file is not known."""
        if name in seen:
            return False
        job = document.get(name)
        if name == _TEMPLATE_JOB and job is None:
            return True
        above = parents(job)
        if not isinstance(job, dict) or not set(job) <= _TEMPLATE_JOB_KEYS or above is None:
            return False
        return all(inert(parent, seen | {name}) for parent in above)

    # A job another job extends hands its variables on to that job's own script.
    extended = {parent for job in document.values() for parent in (parents(job) or [])}
    for name, job in document.items():
        above = parents(job)
        if not isinstance(job, dict) or above is None:
            continue
        if name != _TEMPLATE_JOB and (name.startswith(".") or _TEMPLATE_JOB not in above):
            continue
        if name in extended or not inert(name, frozenset()):
            continue
        if isinstance(job.get("variables"), dict):
            spans += _items(job["variables"].get("FEATUREFLIP_FLAGS"), flag_key, text)
    return spans


def _circleci_items(document: object, flag_key: str, text: str) -> list[tuple[int, int]]:
    """The ``flags`` parameter of the orb's ``cleanup`` job in a workflow, or of
    its ``cleanup`` command in a job's steps, under an alias whose ``orbs:``
    entry names the published orb."""
    if not isinstance(document, dict) or not isinstance(document.get("orbs"), dict):
        return []
    names = {
        f"{alias}/{_ORB_ENTRY}"
        for alias, orb in document["orbs"].items()
        if isinstance(orb, str) and orb.strip().split("@", 1)[0] == _OWN_ORB
    }

    def entries(listing: object) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        for entry in listing if isinstance(listing, list) else ():
            if isinstance(entry, dict) and len(entry) == 1:
                ((name, params),) = entry.items()
                if name in names and isinstance(params, dict):
                    found += _items(params.get("flags"), flag_key, text)
        return found

    spans: list[tuple[int, int]] = []
    workflows = document.get("workflows")
    for workflow in workflows.values() if isinstance(workflows, dict) else ():
        if isinstance(workflow, dict):
            spans += entries(workflow.get("jobs"))
    jobs = document.get("jobs")
    for job in jobs.values() if isinstance(jobs, dict) else ():
        if isinstance(job, dict):
            spans += entries(job.get("steps"))
    return spans


_CI_ITEMS = {"github": _github_items, "gitlab": _gitlab_items, "circleci": _circleci_items}


def _ci_exempt(kind: str, text: str, flag_key: str) -> bool | None:
    """Whether every spelling of the key in a customer's CI file of ``kind`` (raw
    or :func:`decoded`) lies inside an item of this Action's flag list: the
    ``flags`` input of a step that uses the Action, the GitLab template's
    ``flags`` input or ``FEATUREFLIP_FLAGS``, the orb's ``flags`` parameter.
    Asked per occurrence: the items' source is blanked and what is left must not
    spell the key. ``None`` when the file cannot be read as YAML (a custom tag
    such as GitLab's ``!reference``, nesting too deep to parse, more than one
    document), False when it holds an anchor, an alias, a merge key or a
    duplicated key, which can each put a value somewhere it does not appear."""
    import yaml

    if text.startswith("﻿"):
        return None
    try:
        events = list(yaml.parse(text))
        node = yaml.compose(text)
        if any(isinstance(e, yaml.AliasEvent) or getattr(e, "anchor", None) for e in events):
            return False
        spans = _CI_ITEMS[kind](_plain(node), flag_key, text)
    except _NotExempt:
        return False
    except Exception:  # noqa: BLE001 -- yaml errors, recursion: unreadable, refused by name
        return None
    blanked = list(text)
    for start, end in spans:
        blanked[start:end] = " " * (end - start)
    return flag_key not in decoded("".join(blanked))


def _ci_kind(path: str) -> str | None:
    """Which CI system reads ``path``: a ``.github/workflows`` file, a
    ``.gitlab-ci.yml``, or a ``.circleci/config.yml``."""
    norm = path.replace("\\", "/")
    if "/.github/workflows/" in norm and norm.endswith((".yml", ".yaml")):
        return "github"
    if norm.rsplit("/", 1)[-1] in (".gitlab-ci.yml", ".gitlab-ci.yaml"):
        return "gitlab"
    if norm.endswith("/.circleci/config.yml"):
        return "circleci"
    return None


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
    this Action's flag list in a GitHub Actions workflow, a ``.gitlab-ci.yml`` or
    a ``.circleci/config.yml`` (:func:`_ci_exempt`). A file that cannot be
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
                line, _count = hit
                kind = _ci_kind(path)
                if kind is not None:
                    exempt = _ci_exempt(kind, open(path, encoding="utf-8").read(), flag_key)
                    if exempt:
                        continue
                    if exempt is None:
                        found[path] = f"line {line}: a CI file that cannot be read as YAML holds the key"
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
