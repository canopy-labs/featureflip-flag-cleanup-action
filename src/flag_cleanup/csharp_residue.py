"""Remove what a completed C# gate family leaves behind.

A complete keyless wrapper (:mod:`flag_cleanup.wrapper_inline`) takes its
member, the interface member and every inert sibling's member with it, and
stops there. What it leaves builds, but is dead: the empty interface ``I``,
the member-less wrapper class ``W`` and siblings ``C``, the dependency
injection registrations binding them, and the ``I`` constructor parameter
(with the field it was stored in) that each consumer still takes. This pass
removes them, as one transaction per family:

* **K1/K2** the declarations of ``I``, ``W`` and every ``C`` (the file, when
  nothing else is declared in it);
* **K3** each registration statement naming only family types
  (``services.AddScoped<I, W>();``);
* **K4** each consumer's explicit-constructor parameter of a family type,
  with the field it stored and every caller's argument
  (:func:`flag_cleanup.ctor_param_csharp.plan_classic`).

**The core rule.** After the run every spelling of every deleted type name
must lie inside what this pass deletes. Every spelling is found by text, in
every file of the checkout, and each must be classified: inside a deleted
declaration, a deleted registration statement or a K4 site, or prose (a
``//`` comment, doc-comment text, a Markdown file), which is reported and
never edited. Anything else keeps the whole family exactly as it is: a
string naming the type (reflection, a plugin loader), a file of another
language or a configuration file naming it, a cref to it, a use the proofs
cannot follow. The obligations each kind adds are checked before anything is
written, and the same rule is checked again on the result.

**Never a refusal of the flag.** The residue builds as it is (an unread
field of an empty interface type is not even a warning), so a failed proof,
a failed postcondition or an engine abort puts back every byte this pass
wrote, re-creates every file it deleted, and reports one ``residue-kept`` row
naming the first blocker.

Placement: split. This module proves and marks (repository facts, R3, and
flag policy, R1); the engine deletes the marked spans through the rendered
rules of ``rules/csharp_ctor_param.toml`` (R4); deleting a whole file is
this run's own decision (:mod:`flag_cleanup.file_deletion`).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import types
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import appsettings_edit, ctor_param_csharp, file_deletion, git_ops, source_index, syntax
from flag_cleanup import cross_file_csharp as xcs
from flag_cleanup import wrapper_inline as wi
from flag_cleanup.cross_file_dotnet_guest import DOTNET_GUEST_EXTENSIONS
from flag_cleanup.ctor_param_model import (
    Removal,
    Refusal,
    choose_sentinel,
    forms_for,
    mark,
    render_rules,
    tidy,
    tidy_lines,
)
from flag_cleanup.unused import _parse

logger = logging.getLogger(__name__)

_GUESTS = DOTNET_GUEST_EXTENSIONS["dotnet_guest"]
#: Directories never read for spellings: VCS metadata, packages, and .NET
#: build output, which is rebuilt from the sources this pass edits.
_PRUNED = frozenset({".git", "node_modules", "bin", "obj"})
#: Prose: a mention there is reported, never edited.
_PROSE_SUFFIXES = frozenset({".md", ".markdown"})
_TYPE_DECLARATIONS = frozenset(
    {
        "class_declaration", "struct_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "record_struct_declaration", "delegate_declaration",
    }
)
_STRINGS = frozenset(
    {"string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression"}
)
#: Registration methods whose type arguments name what they register.
_REGISTRATIONS = re.compile(
    r"(?:Try)?Add(?:Keyed)?(?:Scoped|Singleton|Transient)|RemoveAll"
)
_DESCRIPTORS = frozenset({"Scoped", "Singleton", "Transient"})
_RESOLVERS = frozenset({"GetRequiredService", "GetService"})
#: Calls that enumerate an assembly's types.
_SCANS = frozenset({"GetTypes", "GetExportedTypes"})
_SCAN_PROPERTIES = frozenset({"DefinedTypes", "ExportedTypes"})
_SCRUTOR = re.compile(r"^(?:Scan|AddClasses|RegisterAssemblyTypes|FromAssembl\w*|FromApplicationDependencies)$")
_SUPERTYPE_TESTS = frozenset({"IsAssignableFrom", "IsAssignableTo", "IsSubclassOf"})
_MEMBER_LISTS = frozenset({"GetMethods", "GetProperties", "GetEvents", "GetNestedTypes"})
_OBJECT_NAMES = frozenset({"object", "Object", "System.Object", "global::System.Object"})
_CREF = re.compile(rb"""cref\s*=\s*["']([^"']*)["']""")
#: A self-closing ``<see>`` or ``<seealso>`` whose only attribute is a cref:
#: the one shape rewritten to ``<c>…</c>`` when what it names goes.
_SEE = re.compile(rb"""<(?:see|seealso)\s+cref\s*=\s*(["'])([^"'<>]*)\1\s*/>""")
#: A cref target that is a dotted name, a method's empty parameter list
#: allowed. An ID string (``T:Ns.I``), generics and ``global::`` are not.
_CREF_TARGET = re.compile(r"@?[^\W\d]\w*(?:\.@?[^\W\d]\w*)*(?:\(\))?")
_INHERITDOC = re.compile(rb"<inheritdoc\b[^>]*cref\s*=\s*[\"']([^\"']*)[\"']")


class Kept(Exception):
    """A proof failed: the family's residue stays, and why."""

    def __init__(self, path: str, line: int, reason: str):
        super().__init__(f"{path}:{line}: {reason}")
        self.path = path
        self.line = line
        self.reason = reason


@dataclass(frozen=True)
class Target:
    """A family type to delete."""

    name: str
    namespace: str
    #: Resolved path of the declaring file.
    path: str
    #: Byte span of the declaration with its doc comment, byte-order mark
    #: included.
    start: int
    end: int
    line: int
    kind: str

    @property
    def full(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


@dataclass
class Residue:
    label: str
    targets: list[Target]
    #: Resolved path -> ``(start, end, form)`` marks, byte-order mark included.
    spans: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    removals: list[Removal] = field(default_factory=list)
    #: Resolved paths of the files to delete whole.
    delete: set[str] = field(default_factory=set)
    #: ``(path, line, text)`` for each thing removed.
    rows: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(path, line, text)`` for each prose mention left.
    prose: list[tuple[str, int, str]] = field(default_factory=list)
    #: Resolved path -> ``(tag, text)`` for each doc-comment reference to a
    #: deleted type rewritten to plain text: ``<see cref="I"/>`` becomes
    #: ``<c>I</c>``.
    rewrites: dict[str, list[tuple[bytes, bytes]]] = field(default_factory=dict)
    #: Resolved path -> the configuration file's new text, for each
    #: ``appsettings*.json`` whose binding the run deletes.
    config: dict[str, str] = field(default_factory=dict)
    #: A tier that was kept while the rest went (the options class, K7).
    kept: list[Kept] = field(default_factory=list)


@dataclass
class _File:
    path: str
    data: bytes
    #: 3 when the file starts with a byte-order mark: every node offset is
    #: into the text after it.
    offset: int
    root: Node


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _line(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _tidy_reason(reason: str, repo_dir: str) -> str:
    """``reason`` with the checkout's absolute prefix dropped from any path
    it names: a pull request reader knows the repository, not the runner."""
    for root in {str(Path(source_index.checkout_root(repo_dir)).resolve()), str(Path(repo_dir).resolve())}:
        reason = reason.replace(root + os.sep, "")
    return reason


def _rel(path: str, repo_dir: str) -> str:
    """``path`` as a finding spells it: resolved, since the runner makes
    every finding relative to the directory it ran on."""
    del repo_dir
    return str(Path(path).resolve()) if path else path


def _read(path: str) -> _File:
    data = Path(path).read_bytes()
    offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    return _File(path, data, offset, _parse(data[offset:].decode("utf-8"), "csharp"))


def _word(names: Iterable[str]) -> re.Pattern[bytes]:
    return re.compile(rb"(?<![\w@])(?:" + b"|".join(re.escape(n.encode()) for n in sorted(names)) + rb")(?!\w)")


# --- the family's own declarations (K1, K2) ----------------------------------


def _modifiers(node: Node) -> set[str]:
    return {_text(c) for c in node.children if c.type == "modifier"}


def _base_names(node: Node) -> list[str | None]:
    base_list = next((c for c in node.children if c.type == "base_list"), None)
    if base_list is None:
        return []
    return [ctor_param_csharp._simple_type(c) for c in base_list.named_children if c.type != "argument_list"]


def _inert_constructor(member: Node, fields: set[str]) -> bool:
    """``W(IClient c) => _c = c;``, or a block of such statements, also
    ``_o = o.Value;``: running it does nothing but store its arguments."""
    if any(c.type == "constructor_initializer" for c in member.children):
        return False
    parameters = member.child_by_field_name("parameters")
    names = {
        _text(n)
        for p in (parameters.named_children if parameters is not None else [])
        if p.type == "parameter" and (n := p.child_by_field_name("name")) is not None
    }
    body = member.child_by_field_name("body")
    if body is None:
        return False
    if body.type == "arrow_expression_clause":
        expressions = body.named_children
    elif body.type == "block":
        expressions = []
        for statement in body.named_children:
            if statement.type == "comment":
                continue
            if statement.type != "expression_statement" or len(statement.named_children) != 1:
                return False
            expressions.append(statement.named_children[0])
    else:
        return False
    for expression in expressions:
        if expression.type != "assignment_expression":
            return False
        left = expression.child_by_field_name("left")
        right = expression.child_by_field_name("right")
        operator = next((c for c in expression.children if not c.is_named), None)
        if operator is None or _text(operator) != "=" or left is None or right is None:
            return False
        if left.type == "member_access_expression":
            receiver = left.child_by_field_name("expression")
            if receiver is None or receiver.type not in ("this_expression", "this"):
                return False
            left = left.child_by_field_name("name")
        if left is None or left.type != "identifier" or _text(left) not in fields:
            return False
        if right.type == "member_access_expression":
            name = right.child_by_field_name("name")
            if name is None or _text(name) != "Value":
                return False
            right = right.child_by_field_name("expression")
        if right is None or right.type != "identifier" or _text(right) not in names:
            return False
    return True


def _member_less(declaration: Node, kind: str) -> str | None:
    """Why ``declaration`` still holds something, or ``None`` when all it
    declares is private fields and inert constructors (or, for the
    interface, nothing)."""
    body = declaration.child_by_field_name("body")
    members = [m for m in (body.named_children if body is not None else []) if m.type != "comment"]
    if kind == "interface":
        return f"it still declares {_text(members[0]).split(chr(10))[0][:60]}" if members else None
    fields: set[str] = set()
    for member in members:
        if member.type != "field_declaration":
            continue
        if _modifiers(member) & {"public", "protected", "internal"} or any(
            c.type == "attribute_list" for c in member.children
        ):
            return "it declares a field that is not private, or carries an attribute"
        variable = next((c for c in member.named_children if c.type == "variable_declaration"), None)
        for declarator in (variable.named_children if variable is not None else []):
            if declarator.type != "variable_declarator":
                continue
            name = ctor_param_csharp._declarator_name(declarator)
            initial = [c for c in declarator.named_children if c.type not in ("identifier",)]
            if initial and not all(ctor_param_csharp._inert(c) for c in initial):
                return f"the field {name} is initialised by code that may run"
            if name is not None:
                fields.add(name)
    for member in members:
        if member.type == "field_declaration":
            continue
        if member.type == "constructor_declaration":
            if "static" in _modifiers(member) or any(c.type == "attribute_list" for c in member.children):
                return "it declares a static or attributed constructor"
            if not _inert_constructor(member, fields):
                return "a constructor does more than store its arguments"
            continue
        return f"it still declares {_text(member).split(chr(10))[0].strip()[:60]}"
    return None


def _targets(wrapper: wi.Wrapper, repo_dir: str) -> list[Target]:
    """The family's types, each proven deletable on its own."""
    wanted: list[tuple[str, str, str, str]] = []
    if wrapper.interface is not None:
        wanted.append((wrapper.interface.name, wrapper.interface.package, wrapper.interface.path, "interface"))
    wanted.append((wrapper.owner, wrapper.package, wrapper.path, "wrapper"))
    wanted += [(s.owner, s.namespace, s.path, "sibling") for s in wrapper.siblings]
    interface = wrapper.interface.name if wrapper.interface is not None else None
    targets: list[Target] = []
    for name, namespace, path, kind in wanted:
        path = str(Path(path).resolve())
        try:
            file = _read(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise Kept(_rel(path, repo_dir), 1, f"could not be read: {exc}") from exc
        if file.root.has_error:
            raise Kept(_rel(path, repo_dir), 1, "does not parse")
        found = [
            n
            for n in _walk(file.root)
            if n.type in _TYPE_DECLARATIONS
            and (cn := n.child_by_field_name("name")) is not None
            and _text(cn) == name
            and xcs._namespace_of(n) == namespace
        ]
        if len(found) != 1:
            raise Kept(_rel(path, repo_dir), 1, f"{name} is declared {len(found)} times in its file")
        node = found[0]
        line = node.start_point[0] + 1
        where = _rel(path, repo_dir)
        expected = "interface_declaration" if kind == "interface" else "class_declaration"
        if node.type != expected:
            raise Kept(where, line, f"{name} is not a plain {expected.split('_')[0]}")
        from flag_cleanup import wrapper_csharp

        if not wrapper_csharp._top_level(node):
            raise Kept(where, line, f"{name} is nested in another type")
        if _modifiers(node) & {"partial", "static", "abstract"}:
            raise Kept(where, line, f"{name} is partial, static or abstract")
        if any(c.type in ("type_parameter_list", "attribute_list") for c in node.children):
            raise Kept(where, line, f"{name} is generic or carries an attribute")
        bases = _base_names(node)
        if kind == "interface" and bases:
            raise Kept(where, line, f"{name} extends another interface")
        if kind != "interface" and (interface is None or bases != [interface]):
            raise Kept(where, line, f"{name}'s base list holds more than the family interface")
        reason = _member_less(node, kind)
        if reason is not None:
            raise Kept(where, line, f"{name}: {reason}")
        if wrapper_csharp.published(path, repo_dir):
            raise Kept(where, line, f"{name}'s project may be packed and used outside the repository")
        start, end = ctor_param_csharp.declaration_span(node, file.data[file.offset :])
        targets.append(Target(name, namespace, path, start + file.offset, end + file.offset, line, kind))
    return targets


def _reduces_to_nothing(file: _File, spans: list[tuple[int, int]]) -> bool:
    """Whether ``file`` holds nothing once ``spans`` (byte-order mark
    included) are deleted but namespaces, comments, preprocessor lines and
    plain ``using`` directives, which only serve the file itself. Anything
    else is someone else's: an ``[assembly: …]`` attribute, a ``global
    using``, an ``extern alias``, a global statement, another declaration.
    Such a file loses only the spans, never the file."""

    def clean(node: Node) -> bool:
        for child in node.named_children:
            if _inside(spans, child.start_byte + file.offset):
                continue
            kind = child.type
            if kind == "comment" or kind.startswith("preproc"):
                continue
            if kind == "using_directive":
                if any(c.type == "global" for c in child.children):
                    return False
                continue
            if kind == "file_scoped_namespace_declaration":
                if any(c.type not in ("identifier", "qualified_name") for c in child.named_children):
                    return False
                continue
            if kind == "namespace_declaration":
                body = child.child_by_field_name("body")
                if body is None or not clean(body):
                    return False
                continue
            return False
        return True

    return clean(file.root)


def _whole_files(targets: list[Target], repo_dir: str) -> tuple[set[str], dict[str, list[Target]]]:
    """The target files that hold nothing but targets (to delete whole), and
    the targets to delete from files that hold something else."""
    by_path: dict[str, list[Target]] = {}
    for target in targets:
        by_path.setdefault(target.path, []).append(target)
    whole: set[str] = set()
    partial: dict[str, list[Target]] = {}
    ignored = {f.resolved for f in source_index.files(repo_dir, (".cs",), with_ignored=True) if f.ignored}
    for path, held in by_path.items():
        file = _read(path)
        if not _reduces_to_nothing(file, [(t.start, t.end) for t in held]):
            partial[path] = held
            continue
        if path in ignored:
            raise Kept(_rel(path, repo_dir), 1, "an ignored file, which the pull request cannot carry")
        scope = Path(repo_dir).resolve()
        if scope not in Path(path).parents:
            raise Kept(_rel(path, repo_dir), 1, f"outside {repo_dir}")
        whole.add(path)
    return whole, partial


# --- every spelling in the checkout -------------------------------------------


def _text_files(repo_dir: str) -> list[Path]:
    """Every file of the checkout, tracked, untracked and ignored, less
    :data:`_PRUNED` directories."""
    root = Path(source_index.checkout_root(repo_dir))
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--directory", "--no-empty-directory"],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", "replace").split("\0")
    except (OSError, subprocess.CalledProcessError):
        listed = [""]
    found: set[Path] = set()

    def walk(directory: Path) -> None:
        for current, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in _PRUNED]
            found.update(Path(current) / f for f in files)

    if listed == [""]:
        walk(root)
    for entry in listed:
        if not entry:
            continue
        relative = Path(entry)
        if any(part in _PRUNED for part in relative.parts):
            continue
        path = root / relative
        if entry.endswith("/"):
            walk(path)
        elif path.is_file():
            found.add(path)
    return sorted(found)


def _binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def _scan(names: frozenset[str], repo_dir: str, residue: Residue) -> list[str]:
    """The C# files spelling any of ``names``. Every other file that does
    is prose (reported) or keeps the family."""
    word = _word(names)
    cs: list[str] = []
    for path in _text_files(repo_dir):
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise Kept(_rel(str(path), repo_dir), 1, f"could not be read: {exc}") from exc
        if _binary(data) or word.search(data) is None:
            continue
        spelled = str(path.resolve())
        suffix = path.suffix.lower()
        if suffix == ".cs":
            cs.append(spelled)
        elif suffix in _PROSE_SUFFIXES:
            for match in word.finditer(data):
                line = _line(data, match.start())
                residue.prose.append((spelled, line, _line_text(data, match.start())))
        elif path.name.lower().endswith(_GUESTS):
            raise Kept(_rel(spelled, repo_dir), _line(data, word.search(data).start()), "a .NET guest file names it")
        else:
            match = word.search(data)
            raise Kept(
                _rel(spelled, repo_dir),
                _line(data, match.start()),
                f"a {suffix or 'suffix-less'} file names {match.group().decode()} (type-name configuration?)",
            )
    return cs


def _line_text(data: bytes, offset: int) -> str:
    start = data.rfind(b"\n", 0, offset) + 1
    end = data.find(b"\n", offset)
    return " ".join(data[start : end if end != -1 else len(data)].decode("utf-8", "replace").split())


# --- registrations (K3) ------------------------------------------------------


def _statement_of(node: Node) -> Node | None:
    current = node
    while current is not None and current.type != "expression_statement":
        if current.type in ("block", "declaration_list", "compilation_unit"):
            return None
        current = current.parent
    return current


def _call_name(invocation: Node) -> tuple[Node | None, Node | None, Node | None]:
    """``(receiver, name, generic)`` of ``receiver.Name<…>(…)``."""
    function = invocation.child_by_field_name("function")
    if function is None or function.type != "member_access_expression":
        return None, None, None
    receiver = function.child_by_field_name("expression")
    name = function.child_by_field_name("name")
    if name is not None and name.type == "generic_name":
        return receiver, name.named_children[0], name
    return receiver, name, None


def _type_arguments(generic: Node | None) -> list[Node]:
    arguments = next((c for c in generic.named_children if c.type == "type_argument_list"), None) if generic else None
    return list(arguments.named_children) if arguments is not None else []


def _lambda_parameters(node: Node) -> list[str]:
    """The names a lambda binds: ``sp => …`` or ``(sp) => …``."""
    names: list[str] = []
    for child in node.children:
        if child.type == "implicit_parameter":
            names.append(_text(child))
        elif child.type == "parameter_list":
            names += [
                _text(n) for p in child.named_children
                if p.type == "parameter" and (n := p.child_by_field_name("name")) is not None
            ]
    return names


def _factory(node: Node, names: frozenset[str]) -> bool:
    """``sp => new W(sp.GetRequiredService<X>(), …)``: builds a family type
    from resolved services and nothing else."""
    if node.type != "lambda_expression":
        return False
    params = _lambda_parameters(node)
    if len(params) != 1:
        return False
    param = params[0]
    body = node.child_by_field_name("body")
    if body is None or body.type != "object_creation_expression":
        return False
    if ctor_param_csharp._simple_type(body.child_by_field_name("type")) not in names:
        return False
    if body.child_by_field_name("initializer") is not None:
        return False
    arguments = body.child_by_field_name("arguments")
    for argument in ctor_param_csharp._items(arguments) if arguments is not None else []:
        value = ctor_param_csharp._expression(argument)
        if value is None or value.type != "invocation_expression":
            return False
        receiver, name, generic = _call_name(value)
        if receiver is None or _text(receiver) != param or name is None or _text(name) not in _RESOLVERS:
            return False
        arguments_of = value.child_by_field_name("arguments")
        if generic is None or (arguments_of is not None and ctor_param_csharp._items(arguments_of)):
            return False
    return True


def _within(node: Node, ancestor: Node | None) -> bool:
    return ancestor is not None and ancestor.start_byte <= node.start_byte and node.end_byte <= ancestor.end_byte


def _registration(node: Node, names: frozenset[str]) -> tuple[Node, str] | str | None:
    """The registration statement a spelling names a registered type in,
    with its text, or why it is not one this pass may delete; ``None`` when
    the spelling is not what a registration registers (it may still lie
    inside something else this pass deletes)."""
    statement = _statement_of(node)
    if statement is None or len(statement.named_children) != 1:
        return None
    invocation = statement.named_children[0]
    if invocation.type != "invocation_expression":
        return None
    receiver, name, generic = _call_name(invocation)
    if name is None:
        return None
    method = _text(name)
    arguments = invocation.child_by_field_name("arguments")
    items = ctor_param_csharp._items(arguments) if arguments is not None else []
    values = [ctor_param_csharp._expression(a) for a in items]
    in_types = _within(node, generic)
    in_value = next((v for v in values if v is not None and _within(node, v)), None)
    replacing = method == "Replace" and generic is None
    if not _REGISTRATIONS.fullmatch(method) and not replacing:
        return None
    if not in_types and in_value is None:
        return None
    if in_value is not None and not in_types:
        typed = in_value.type == "typeof_expression" and generic is None
        if not (typed or replacing or _factory(in_value, names) or in_value.type == "lambda_expression"):
            return None
    text = " ".join(_text(statement).split())
    if receiver is None or receiver.type != "identifier":
        return f"a registration this pass cannot read ({text[:60]})"
    if statement.parent is None or statement.parent.type != "block":
        return f"a registration outside a block ({text[:60]})"
    if replacing:
        inner = values[0] if len(values) == 1 else None
        if inner is not None and inner.type == "invocation_expression":
            inner_receiver, inner_name, inner_generic = _call_name(inner)
            inner_arguments = inner.child_by_field_name("arguments")
            if (
                inner_receiver is not None
                and _text(inner_receiver) == "ServiceDescriptor"
                and inner_name is not None
                and _text(inner_name) in _DESCRIPTORS
                and inner_generic is not None
                and all(ctor_param_csharp._simple_type(t) in names for t in _type_arguments(inner_generic))
                and inner_arguments is not None
                and not ctor_param_csharp._items(inner_arguments)
            ):
                return statement, text
        return f"a replacement this pass cannot read ({text[:60]})"
    if generic is not None:
        type_arguments = _type_arguments(generic)
        if not type_arguments or not all(ctor_param_csharp._simple_type(t) in names for t in type_arguments):
            return f"a registration naming a type this run keeps ({text[:60]})"
        keyed = "Keyed" in method
        rest = values[1:] if keyed else values
        if keyed and (not values or values[0] is None or not ctor_param_csharp._inert(values[0])):
            return f"a keyed registration whose key may do work ({text[:60]})"
        if len(rest) > 1 or (rest and (rest[0] is None or not _factory(rest[0], names))):
            return f"a registration with arguments this pass cannot read ({text[:60]})"
        return statement, text
    if values and all(
        v is not None
        and v.type == "typeof_expression"
        and ctor_param_csharp._simple_type(next(iter(v.named_children), None)) in names
        for v in values
    ):
        return statement, text
    return f"a registration this pass cannot read ({text[:60]})"


# --- assembly scans ------------------------------------------------------------


def _lambda_body(call: Node) -> tuple[str, Node] | None:
    arguments = call.child_by_field_name("arguments")
    items = ctor_param_csharp._items(arguments) if arguments is not None else []
    if len(items) != 1:
        return None
    value = ctor_param_csharp._expression(items[0])
    if value is None or value.type != "lambda_expression":
        return None
    params = _lambda_parameters(value)
    body = value.child_by_field_name("body")
    if len(params) != 1 or body is None:
        return None
    return params[0], body


def _conjuncts(node: Node) -> Iterator[Node]:
    if node.type == "parenthesized_expression" and node.named_children:
        yield from _conjuncts(node.named_children[0])
        return
    operator = node.child_by_field_name("operator")
    if node.type == "binary_expression" and operator is not None and _text(operator) == "&&":
        for side in ("left", "right"):
            child = node.child_by_field_name(side)
            if child is not None:
                yield from _conjuncts(child)
        return
    yield node


def _supertype_test(term: Node, param: str, names: frozenset[str]) -> bool:
    """``typeof(X).IsAssignableFrom(t)``, ``t.IsAssignableTo(typeof(X))`` or
    ``t.IsSubclassOf(typeof(X))`` with ``X`` neither a family type nor
    ``object``: a family type's only supertype is the family interface."""
    if term.type != "invocation_expression":
        return False
    receiver, name, _ = _call_name(term)
    arguments = term.child_by_field_name("arguments")
    items = ctor_param_csharp._items(arguments) if arguments is not None else []
    if receiver is None or name is None or _text(name) not in _SUPERTYPE_TESTS or len(items) != 1:
        return False
    value = ctor_param_csharp._expression(items[0])
    typed, other = (receiver, value) if _text(name) == "IsAssignableFrom" else (value, receiver)
    if other is None or other.type != "identifier" or _text(other) != param:
        return False
    if typed is None or typed.type != "typeof_expression":
        return False
    tested = next((c for c in typed.named_children), None)
    if tested is None:
        return False
    spelled = _text(tested)
    if tested.type == "generic_name":
        simple = _text(tested.named_children[0])
    else:
        simple = ctor_param_csharp._simple_type(tested) or spelled
    return spelled not in _OBJECT_NAMES and simple not in names


def _scan_filtered(scan: Node, names: frozenset[str]) -> bool:
    """Whether what an assembly scan finds provably never held a family
    type: its next step is a ``Where`` whose condition is a conjunction
    with a supertype test against another type, or a ``SelectMany`` over
    the members a family type does not declare (``DeclaredOnly`` methods,
    properties, events or nested types)."""
    holder = scan.parent
    if holder is not None and holder.type == "member_access_expression" and holder.parent is not None:
        call = holder.parent
    else:
        return False
    if call.type != "invocation_expression":
        return False
    _, name, _ = _call_name(call)
    if name is None:
        return False
    found = _lambda_body(call)
    if found is None:
        return False
    param, body = found
    if _text(name) == "Where":
        return any(_supertype_test(term, param, names) for term in _conjuncts(body))
    if _text(name) == "SelectMany" and body.type == "invocation_expression":
        receiver, member, _ = _call_name(body)
        return (
            receiver is not None
            and _text(receiver) == param
            and member is not None
            and _text(member) in _MEMBER_LISTS
            and "DeclaredOnly" in _text(body)
        )
    return False


def _assembly_scans(repo_dir: str, targets: list[Target], names: frozenset[str]) -> None:
    """Raise :class:`Kept` for a type enumeration, in a file that can reach
    a family type, whose filter is not provably blind to the family."""
    from flag_cleanup import csharp_family

    projects_all = xcs._project_dirs(repo_dir)
    homes = [csharp_family._enclosing(t.path, projects_all) for t in targets]
    # `reaches` reads only these two facts of a family.
    family = types.SimpleNamespace(
        projects=frozenset(p for home in homes for p in home),
        homeless=any(not home for home in homes),
    )
    needle = re.compile(rb"\b(?:GetTypes|GetExportedTypes|DefinedTypes|ExportedTypes|Scan|AddClasses|RegisterAssemblyTypes|FromAssembl\w*|FromApplicationDependencies)\b")
    for source in source_index.files(repo_dir, (".cs",), with_ignored=True):
        try:
            data = source.path.read_bytes()
        except OSError as exc:
            raise Kept(_rel(str(source.path), repo_dir), 1, f"could not be read: {exc}") from exc
        if needle.search(data) is None or not csharp_family.reaches(repo_dir, source.resolved, family):
            continue
        offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        root = _parse(data[offset:].decode("utf-8", "replace"), "csharp")
        for node in _walk(root):
            if node.type != "member_access_expression":
                continue
            name = node.child_by_field_name("name")
            if name is None:
                continue
            spelled = _text(name.named_children[0] if name.type == "generic_name" else name)
            if _SCRUTOR.fullmatch(spelled):
                raise Kept(
                    _rel(source.resolved, repo_dir), node.start_point[0] + 1,
                    f"an assembly scan registers types by convention ({spelled})",
                )
            scan = None
            if spelled in _SCANS and node.parent is not None and node.parent.type == "invocation_expression":
                scan = node.parent
            elif spelled in _SCAN_PROPERTIES and (node.parent is None or node.parent.type != "invocation_expression"):
                scan = node
            if scan is not None and not _scan_filtered(scan, names):
                raise Kept(
                    _rel(source.resolved, repo_dir), node.start_point[0] + 1,
                    f"an assembly scan ({spelled}) whose filter cannot be proven blind to the family",
                )


# --- the proof ---------------------------------------------------------------


def _inside(spans: Iterable[tuple[int, int]], start: int) -> bool:
    return any(s <= start < e for s, e in spans)


@dataclass(frozen=True)
class _Mention:
    """A spelling in a comment or a string, judged once the deletions are
    known: inside one it goes with it."""

    path: str
    #: Byte offset, byte-order mark included.
    at: int
    line: int
    #: ``prose``, ``cref``, ``inheritdoc`` or ``string``.
    kind: str
    text: str
    #: For a cref inside a self-closing ``<see>``/``<seealso>`` naming a
    #: dotted target: ``(tag, plain text)`` to write in its place.
    rewrite: tuple[bytes, bytes] | None = None


def _mentions(file: _File, word: re.Pattern[bytes], fulls: frozenset[str]) -> list[_Mention]:
    """Each mention inside a comment or a string literal."""
    found: list[_Mention] = []
    for node in _walk(file.root):
        start = node.start_byte + file.offset
        text = node.text
        if node.type == "comment":
            claimed: list[tuple[int, int]] = []
            for match in _INHERITDOC.finditer(text):
                if word.search(match.group(1)):
                    found.append(_Mention(file.path, start + match.start(), node.start_point[0] + _line(text, match.start()),
                                          "inheritdoc", match.group(0).decode("utf-8", "replace")))
                    claimed.append(match.span())
            for match in _CREF.finditer(text):
                if word.search(match.group(1)) and not _inside(claimed, match.start()):
                    target = match.group(1).decode("utf-8", "replace")
                    tag = next(
                        (m for m in _SEE.finditer(text) if m.start() <= match.start() and match.end() <= m.end()),
                        None,
                    )
                    rewrite = None
                    if tag is not None and _CREF_TARGET.fullmatch(target):
                        rewrite = (tag.group(0), b"<c>" + match.group(1) + b"</c>")
                    found.append(_Mention(file.path, start + match.start(), node.start_point[0] + _line(text, match.start()),
                                          "cref", target, rewrite))
                    claimed.append(tag.span() if tag is not None else match.span())
            for match in word.finditer(text):
                if _inside(claimed, match.start()):
                    continue
                found.append(_Mention(file.path, start + match.start(), node.start_point[0] + _line(text, match.start()),
                                      "prose", _line_text(file.data, start + match.start())))
        elif node.type in _STRINGS:
            if word.search(text) or any(f.encode() in text for f in fulls):
                found.append(_Mention(file.path, start, node.start_point[0] + 1, "string", _text(node)[:60]))
    return found


def _spellings(file: _File, names: frozenset[str]) -> list[Node]:
    return [n for n in _walk(file.root) if n.type == "identifier" and _text(n) in names]


def _constructor_parameter(node: Node) -> tuple[Node, Node] | None:
    """``(class, parameter)`` when ``node`` is the type of an explicit
    constructor's parameter."""
    current = node
    while current.parent is not None and current.parent.type in ("qualified_name", "alias_qualified_name", "nullable_type"):
        current = current.parent
    parameter = current.parent
    if parameter is None or parameter.type != "parameter" or parameter.child_by_field_name("type") != current:
        return None
    constructor = parameter.parent.parent if parameter.parent is not None else None
    if constructor is None or constructor.type != "constructor_declaration":
        return None
    body = constructor.parent
    declaration = body.parent if body is not None else None
    if declaration is None or declaration.type not in _TYPE_DECLARATIONS:
        return None
    return declaration, parameter


def prove(wrapper: wi.Wrapper, repo_dir: str) -> Residue:
    """The residue of ``wrapper``'s family and everything that must go with
    it, or :class:`Kept` naming the first thing that keeps it."""
    targets = _targets(wrapper, repo_dir)
    residue = Residue(wrapper.label, targets)
    names = frozenset(t.name for t in targets)
    fulls = frozenset(t.full for t in targets if t.namespace)
    index = xcs.csharp_index(repo_dir)
    for target in targets:
        if any(i.full != target.full for i in index.by_last.get(target.name, ())):
            raise Kept(_rel(target.path, repo_dir), target.line, f"another type is also named {target.name}")
    whole, partial = _whole_files(targets, repo_dir)
    residue.delete = whole
    for path, held in partial.items():
        for target in held:
            residue.spans.setdefault(path, []).append((target.start, target.end, "type"))
    for target in targets:
        what = {"interface": "interface", "wrapper": "wrapper class", "sibling": "sibling class"}[target.kind]
        residue.rows.append((target.path, target.line, f"{what} {target.name}"))
    for path in sorted(whole):
        residue.rows.append((path, 1, "file deleted: nothing else was declared in it"))

    word = _word(names)
    cs_files = _scan(names, repo_dir, residue)
    protected: dict[str, list[tuple[int, int]]] = {}
    for target in targets:
        protected.setdefault(target.path, []).append((target.start, target.end))
    for path in whole:
        protected[path] = [(0, 1 << 62)]

    files: dict[str, _File] = {}
    pending: list[tuple[str, Node]] = []
    mentions: list[_Mention] = []
    candidates: dict[tuple[str, str, str], Node] = {}
    statements: dict[tuple[str, int], tuple[Node, str]] = {}
    for path in cs_files:
        if path in whole:
            continue
        try:
            file = files[path] = _read(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise Kept(_rel(path, repo_dir), 1, f"could not be read: {exc}") from exc
        if file.root.has_error:
            raise Kept(_rel(path, repo_dir), 1, "does not parse")
        own = protected.get(path, [])
        mentions += [m for m in _mentions(file, word, fulls) if not _inside(own, m.at)]
        for node in _spellings(file, names):
            if _inside(own, node.start_byte + file.offset):
                continue
            registration = _registration(node, names)
            if isinstance(registration, str):
                raise Kept(_rel(path, repo_dir), node.start_point[0] + 1, registration)
            if registration is not None:
                statement, text = registration
                statements[(path, statement.start_byte)] = (statement, text)
                continue
            owner = _constructor_parameter(node)
            if owner is not None:
                declaration, parameter = owner
                class_name = _text(declaration.child_by_field_name("name"))
                param_name = _text(parameter.child_by_field_name("name"))
                candidates.setdefault((path, class_name, param_name), node)
                continue
            pending.append((path, node))

    for (path, _), (statement, text) in sorted(statements.items(), key=lambda kv: kv[0]):
        file = files[path]
        start, end = ctor_param_csharp.declaration_span(statement, file.data[file.offset :])
        residue.spans.setdefault(path, []).append((start + file.offset, end + file.offset, "statement"))
        residue.rows.append((path, statement.start_point[0] + 1, f"registration {text}"))

    for (path, class_name, param_name), node in sorted(candidates.items(), key=lambda kv: kv[0]):
        file = files[path]
        outcome = ctor_param_csharp.plan_classic(
            path, file.data.decode("utf-8"), class_name, param_name, repo_dir, names
        )
        if isinstance(outcome, Refusal):
            raise Kept(
                _rel(path, repo_dir), node.start_point[0] + 1,
                f"{outcome.label}: {_tidy_reason(outcome.reason, repo_dir)}",
            )
        from flag_cleanup import wrapper_csharp

        if outcome.exported and wrapper_csharp.published(path, repo_dir):
            raise Kept(
                _rel(path, repo_dir), node.start_point[0] + 1,
                f"{outcome.label}: a public constructor of a project that may be packed",
            )
        residue.removals.append(outcome)
        line = node.start_point[0] + 1
        residue.rows.append((path, line, f"constructor parameter {outcome.label}"))
        if outcome.field is not None:
            residue.rows.append((path, line, f"field {class_name}.{outcome.field}"))
        if outcome.exported:
            residue.rows.append((path, line, f"changed constructor {class_name}"))

    for removal in residue.removals:
        for site in removal.sites:
            residue.spans.setdefault(str(Path(site.path).resolve()), []).append((site.start, site.end, site.form))
    argument_sites = {
        (str(Path(s.path).resolve()), s.start, s.end)
        for r in residue.removals
        for s in r.sites
        if s.form == "argument"
    }
    for removal in residue.removals:
        for site in removal.forwarded:
            if (str(Path(site.path).resolve()), site.start, site.end) not in argument_sites:
                raise Kept(
                    _rel(site.path, repo_dir), _line(Path(site.path).read_bytes(), site.start),
                    f"{removal.label} is passed to a base constructor whose parameter stays",
                )

    # K5: the tests' doubles of the family and the tests that only build a
    # member of it, then any test class they leave empty.
    _test_doubles(pending, files, residue, names, fulls, repo_dir)

    # The core rule: every spelling not yet accounted for lies inside a span
    # this pass deletes; a comment or a string inside one goes with it.
    covered = {path: [(s, e) for s, e, _ in spans] for path, spans in residue.spans.items()}
    for path, node in pending:
        file = files[path]
        if path in residue.delete:
            continue
        if not _inside(covered.get(path, []), node.start_byte + file.offset):
            parent = node.parent.type if node.parent is not None else "nothing"
            raise Kept(
                _rel(path, repo_dir), node.start_point[0] + 1,
                f"a use of {_text(node)} this pass cannot remove (in {parent})",
            )
    for mention in mentions:
        if mention.path in residue.delete or _inside(covered.get(mention.path, []), mention.at):
            continue
        where = _rel(mention.path, repo_dir)
        if mention.kind == "string":
            raise Kept(where, mention.line, "a string names it (reflection or a type-name lookup may load it)")
        if mention.kind == "inheritdoc":
            raise Kept(where, mention.line, "an <inheritdoc cref> names it")
        if mention.kind == "cref" and mention.rewrite is not None:
            residue.rewrites.setdefault(mention.path, []).append(mention.rewrite)
            residue.prose.append(
                (mention.path, mention.line,
                 f"{mention.rewrite[0].decode('utf-8', 'replace')} is now {mention.rewrite[1].decode('utf-8', 'replace')}")
            )
            continue
        if mention.kind == "cref":
            raise Kept(where, mention.line, f"a doc comment cref names it, not as <see cref=\"…\"/> ({mention.text})")
        residue.prose.append((mention.path, mention.line, mention.text))
    _assembly_scans(repo_dir, targets, names)
    family_deleted = [(t.full, t.namespace) for t in targets]
    try:
        options = _options_tier(wrapper, residue, targets, repo_dir)
    except Kept as kept:
        options = None
        residue.kept.append(kept)
    usings: list[tuple[str, int, int, int]] | None = None
    if options is not None:
        try:
            usings = _emptied_namespaces(family_deleted + options.deleted, residue, repo_dir)
        except Kept as kept:
            # Only the options class can be what empties it now: the
            # family is checked alone below.
            residue.kept.append(kept)
            options = None
    if usings is None:
        usings = _emptied_namespaces(family_deleted, residue, repo_dir)
    if options is not None:
        options.merge(residue)
    for path, start, end, line in usings:
        residue.spans.setdefault(path, []).append((start, end, "using"))
        residue.rows.append((path, line, "using of a namespace the run empties"))
    residue.rows += _emptied_blocks(residue, files)
    return residue


def _emptied_blocks(residue: Residue, files: dict[str, _File]) -> list[tuple[str, int, str]]:
    """``(path, line, text)`` for each block whose every statement the run
    deletes: an arm of an ``if`` left as ``{ }`` is the reviewer's to fold."""
    rows: list[tuple[str, int, str]] = []
    for path, spans in residue.spans.items():
        file = files.get(path)
        if file is None or path in residue.delete:
            continue
        cuts = [(s, e) for s, e, form in spans if form == "statement"]
        if not cuts:
            continue
        for block in (n for n in _walk(file.root) if n.type == "block"):
            statements = [c for c in block.named_children if c.type != "comment"]
            if statements and all(_inside(cuts, c.start_byte + file.offset) for c in statements):
                owner = block.parent.type.replace("_", " ") if block.parent is not None else "block"
                rows.append((path, block.start_point[0] + 1, f"emptied block ({owner}) left as {{ }}"))
    return rows


def _emptied_namespaces(
    deleted: list[tuple[str, str]], residue: Residue, repo_dir: str
) -> list[tuple[str, int, int, int]]:
    """The ``using`` directives of each namespace the run empties, as
    ``(path, start, end, line)`` marks: a ``using`` of a namespace no type is
    declared in any more is CS0246. ``deleted`` holds ``(full name,
    namespace)`` of every type the run deletes.

    A namespace is empty when the checkout declares no other type in it or
    below it, and no file the index could not read whole spells its last
    segment. A type an assembly outside the repository declares inside the
    repository's own namespace is not seen (README limit). A project
    ``<Using Include>`` of it cannot be edited here and keeps the family."""
    index = xcs.csharp_index(repo_dir)
    gone = {full for full, _ in deleted}
    marks: list[tuple[str, int, int, int]] = []
    for namespace in sorted({ns for _, ns in deleted if ns}):
        survivors = [
            info
            for infos in index.by_full.values()
            for info in infos
            if (info.namespace == namespace or info.namespace.startswith(namespace + "."))
            and info.full not in gone
        ]
        if survivors:
            continue
        if index.spelled_where_unparsed(namespace.rsplit(".", 1)[-1]):
            raise Kept("", 1, f"the run may empty namespace {namespace}, which a file that does not parse may declare into")
        marks += _usings_of(namespace, residue, repo_dir)
    return marks


_USING_TEMPLATE = r"^[ \t]*(?:global[ \t]+)?using[ \t]+{}[ \t]*;"


def _usings_of(namespace: str, residue: Residue, repo_dir: str) -> list[tuple[str, int, int, int]]:
    """The C# ``using`` directives that import ``namespace``: written in full,
    as ``global::…``, or relative to an enclosing namespace (``using
    Options;`` inside ``namespace App``). A spelling of the namespace in any
    other file (``@using`` in Razor, a project ``<Using>``, configuration)
    is one this cannot edit, and keeps what empties it."""
    index = xcs.csharp_index(repo_dir)
    known = set(index.namespaces) | {namespace}
    dotted = re.compile(rb"(?<![\w.])" + re.escape(namespace.encode()) + rb"(?![\w])")
    last = _word(frozenset({namespace.rsplit(".", 1)[-1]}))
    found: list[tuple[str, int, int, int]] = []
    for file in _text_files(repo_dir):
        resolved = str(file.resolve())
        if resolved in residue.delete:
            continue
        try:
            data = file.read_bytes()
        except OSError as exc:
            raise Kept(_rel(resolved, repo_dir), 1, f"could not be read: {exc}") from exc
        if _binary(data):
            continue
        suffix = file.suffix.lower()
        if suffix != ".cs":
            if suffix in _PROSE_SUFFIXES:
                continue
            match = dotted.search(data)
            if match is not None:
                raise Kept(
                    _rel(resolved, repo_dir), _line(data, match.start()),
                    f"namespace {namespace}, which the run empties, is named in a file this pass does not edit",
                )
            continue
        if last.search(data) is None or b"using" not in data:
            continue
        offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        try:
            root = _parse(data[offset:].decode("utf-8"), "csharp")
        except UnicodeDecodeError as exc:
            raise Kept(_rel(resolved, repo_dir), 1, "not UTF-8, and it may import a namespace the run empties") from exc
        scoped = next((c for c in root.named_children if c.type == "file_scoped_namespace_declaration"), None)
        for node in _walk(root):
            if node.type != "using_directive" or node.child_by_field_name("name") is not None:
                continue
            if any(c.type == "static" for c in node.children) or not node.named_children:
                continue
            target = "".join(_text(node.named_children[-1]).split()).removeprefix("global::")
            enclosing: list[str] = []
            current = node.parent
            while current is not None:
                if current.type == "namespace_declaration":
                    name = current.child_by_field_name("name")
                    if name is not None:
                        enclosing.insert(0, _text(name))
                current = current.parent
            if scoped is not None and scoped.start_byte < node.start_byte:
                name = scoped.child_by_field_name("name")
                if name is not None:
                    enclosing.insert(0, _text(name))
            outer = ".".join(enclosing).split(".") if enclosing else []
            candidates = [".".join([*outer[:i], target]) for i in range(len(outer), 0, -1)] + [target]
            meant = next((c for c in candidates if c in known), None)
            if meant != namespace:
                continue
            start, end = ctor_param_csharp.declaration_span(node, data[offset:])
            found.append((resolved, start + offset, end + offset, node.start_point[0] + 1))
    return found


# --- applying it -------------------------------------------------------------


def _normalised(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """``spans`` without the ones inside another (a caller's argument inside
    a deleted statement goes with it); a partial overlap keeps the family."""
    ordered = sorted(set(spans), key=lambda s: (s[0], -s[1]))
    kept: list[tuple[int, int, str]] = []
    for span in ordered:
        if kept and span[0] < kept[-1][1]:
            if span[1] <= kept[-1][1]:
                continue
            raise Kept("", 1, "two deletions overlap")
        kept.append(span)
    return kept


def _postcondition(
    paths: list[str],
    marked: dict[str, bytes],
    marks: dict[str, list[tuple[int, int, str]]],
    residue: Residue,
    sentinel: str,
    names: frozenset[str],
) -> str | None:
    needle = sentinel.encode("ascii")
    for path in paths:
        after = Path(path).read_bytes()
        if needle in after:
            return f"{path}: a marked span was not deleted"
        offset = 3 if after.startswith(b"\xef\xbb\xbf") else 0
        root = _parse(after[offset:].decode("utf-8"), "csharp")
        if root.has_error:
            return f"{path}: no longer parses"
        moffset = 3 if marked[path].startswith(b"\xef\xbb\xbf") else 0
        before_args = _arguments(_parse(marked[path][moffset:].decode("utf-8"), "csharp"))
        expected = before_args - sum(1 for m in marks.get(path, ()) if m[2] == "argument")
        if _arguments(root) != expected:
            return f"{path}: lost a different number of arguments than it had marks"
        left = [n for n in _walk(root) if n.type == "identifier" and _text(n) in names]
        if left:
            return f"{path}:{left[0].start_point[0] + 1}: {_text(left[0])} is still spelled"
    for removal in residue.removals:
        source = Path(removal.declaring_path).read_text(encoding="utf-8")
        if ctor_param_csharp.has_parameter(source.removeprefix("﻿"), removal.class_name, removal.param_name, classic=True):
            return f"{removal.declaring_path}: the parameter {removal.param_name} is still declared"
    return None


def _rewrite_crefs(path: str, rewrites: list[tuple[bytes, bytes]]) -> str | None:
    """Write each ``(tag, text)`` of ``path`` in place, on the file as the
    deletions left it. A tag is found by its own bytes, which must occur
    exactly as often as it is to be rewritten: the copies inside a deleted
    span are gone with it."""
    if not rewrites:
        return None
    data = Path(path).read_bytes()
    counts: dict[tuple[bytes, bytes], int] = {}
    for rewrite in rewrites:
        counts[rewrite] = counts.get(rewrite, 0) + 1
    for (tag, text), count in counts.items():
        if data.count(tag) != count:
            return f"{path}: a doc reference to rewrite occurs {data.count(tag)} times, not {count}"
        data = data.replace(tag, text)
    Path(path).write_bytes(data)
    return None


def _arguments(root: Node) -> int:
    return sum(
        len([c for c in node.named_children if c.type != "comment"])
        for node in _walk(root)
        if node.type == "argument_list"
    )


def apply(
    residue: Residue,
    summaries: list,
    repo_dir: str,
    rewrite: Callable,
    engine: Callable[[list[str], str], object],
) -> list:
    """Write ``residue``'s deletions through the engine and delete its whole
    files. On any failure every byte goes back and :class:`Kept` is raised."""
    names = frozenset(t.name for t in residue.targets)
    marked_paths = sorted(set(residue.spans) | {str(Path(r.declaring_path).resolve()) for r in residue.removals})
    marked_paths = [p for p in marked_paths if p not in residue.delete]
    rewritten = [p for p in residue.rewrites if p not in residue.delete and p not in marked_paths]
    edited = sorted(set(marked_paths) | set(rewritten))
    configured = sorted(residue.config)
    everything = sorted(set(edited) | residue.delete | set(configured))
    git_ops.before_writing(everything)
    before = {path: Path(path).read_bytes() for path in everything}
    marks = {path: _normalised(residue.spans.get(path, [])) for path in marked_paths}
    sentinel = choose_sentinel(before.values())
    if sentinel is None:
        raise Kept("", 1, "no free sentinel name")
    parameters: dict[str, list[tuple[int, int]]] = {}
    for removal in residue.removals:
        path = str(Path(removal.declaring_path).resolve())
        offset = 3 if before[path].startswith(b"\xef\xbb\xbf") else 0
        source = before[path][offset:].decode("utf-8")
        parameters.setdefault(path, []).extend(
            (s + offset, e + offset)
            for s, e in ctor_param_csharp.parameter_spans(source, removal.class_name, removal.param_name, classic=True)
        )
    marked: dict[str, bytes] = {}
    failure: str | None = None
    try:
        for path in edited:
            marked[path] = mark(before[path], marks.get(path, []), sentinel, parameters.get(path, ()), forms=forms_for("csharp"))
            if marked[path] != before[path]:
                Path(path).write_bytes(marked[path])
        if marked_paths:
            engine(marked_paths, render_rules("csharp", residue.removals, sentinel))
        for path in marked_paths:
            out = tidy(marked[path], Path(path).read_bytes())
            if b"\r" in out:
                bom = out.startswith(b"\xef\xbb\xbf")
                fixed = syntax.collapse_introduced_carriage_returns(
                    marked[path].decode("utf-8").removeprefix("﻿"),
                    out.decode("utf-8").removeprefix("﻿"),
                    "csharp",
                ).encode("utf-8")
                out = (b"\xef\xbb\xbf" if bom else b"") + fixed
            out = tidy_lines(marked[path], out)
            Path(path).write_bytes(out)
        for path in edited:
            failure = failure or _rewrite_crefs(path, residue.rewrites.get(path, []))
        failure = failure or _postcondition(edited, marked, marks, residue, sentinel, names)
        if failure is None:
            for path in configured:
                Path(path).write_bytes(residue.config[path].encode("utf-8"))
    except Kept:
        raise
    except Exception as exc:  # noqa: BLE001 - any surprise: put the bytes back
        failure = f"{type(exc).__name__}: {exc}"
    if failure is not None:
        for path, data in before.items():
            Path(path).write_bytes(data)
        raise Kept("", 1, f"postcondition: {failure}")

    by_key = {str(Path(s.path).resolve()): i for i, s in enumerate(summaries)}
    summaries = list(summaries)
    for path in edited + configured:
        after = Path(path).read_bytes()
        if after == before[path]:
            continue
        text = after.decode("utf-8")
        at = by_key.get(path)
        if at is None:
            summaries.append(rewrite(path, before[path].decode("utf-8"), text))
        else:
            old = summaries[at]
            summaries[at] = rewrite(old.path, old.original_content, text)
    try:
        summaries = file_deletion.delete(summaries, sorted(residue.delete), rewrite)
    except OSError as exc:
        for path, data in before.items():
            Path(path).write_bytes(data)
        raise Kept("", 1, f"could not delete a file: {exc}") from exc
    return summaries


def remove(
    plan: wi.WrapperPlan,
    statuses: list,
    summaries: list,
    repo_dir: str,
    rewrite: Callable,
    engine: Callable[[list[str], str], object],
) -> tuple[list, list[wi.Finding]]:
    """Remove the residue of every complete C# wrapper with an interface in
    ``plan``, by ``statuses`` (close-out's rows). Returns the summaries and
    the rows to report: what went, prose left naming it, or why it stays."""
    complete = {f.wrapper for f in statuses if f.reason == wi.COMPLETE}
    rows: list[wi.Finding] = []
    for wrapper in plan.wrappers:
        if wrapper.language != "csharp" or wrapper.interface is None or wrapper.label not in complete:
            continue
        try:
            residue = prove(wrapper, repo_dir)
            summaries = apply(residue, summaries, repo_dir, rewrite, engine)
        except Kept as kept:
            logger.info("keeping the residue of %s: %s", wrapper.label, kept)
            path = kept.path or wrapper.path
            rows.append(wi.Finding(path, kept.line, wi.RESIDUE_KEPT, kept.reason, wrapper.label))
            continue
        except Exception as exc:  # noqa: BLE001 - residue never refuses the flag
            # A file this cannot read (not UTF-8, say) or any other surprise
            # in the proof keeps everything as it is; `apply` restores what
            # it wrote before raising.
            logger.warning("keeping the residue of %s: %s: %s", wrapper.label, type(exc).__name__, exc)
            rows.append(
                wi.Finding(wrapper.path, wrapper.line, wi.RESIDUE_KEPT, f"could not be judged: {type(exc).__name__}: {exc}"[:200], wrapper.label)
            )
            continue
        rows += [
            wi.Finding(_rel(path, repo_dir), line, wi.RESIDUE, text, wrapper.label)
            for path, line, text in residue.rows
        ]
        rows += [
            wi.Finding(kept.path or wrapper.path, kept.line, wi.RESIDUE_KEPT, kept.reason, wrapper.label)
            for kept in residue.kept
        ]
        rows += [
            wi.Finding(_rel(path, repo_dir), line, wi.RESIDUE_PROSE, text, wrapper.label)
            for path, line, text in dict.fromkeys(residue.prose)
        ]
    return summaries, rows


# --- test doubles and construct-only tests (K5) -------------------------------

_TEST_ATTRIBUTES = frozenset({"Fact", "Theory", "InlineData"})
_ASSERT_LITERALS = frozenset({("True", "true"), ("False", "false")})
_SCOPES = frozenset(
    {"method_declaration", "constructor_declaration", "local_function_statement", "accessor_declaration"}
)


def _enclosing(node: Node, kinds: Iterable[str]) -> Node | None:
    current = node.parent
    while current is not None and current.type not in kinds:
        current = current.parent
    return current


def _type_position(node: Node) -> Node:
    """``node`` widened over the qualifiers and ``?`` that spell its type."""
    current = node
    while current.parent is not None and current.parent.type in ("qualified_name", "alias_qualified_name", "nullable_type"):
        current = current.parent
    return current


def _double_creation(value: Node | None, names: frozenset[str] | None = None) -> bool:
    """``new Mock<T>(<inert>…)`` or ``Substitute.For<T>()``, ``T`` one of
    ``names`` (any type when ``None``): building it runs no code of the
    repository's."""
    if value is None:
        return False
    if value.type == "object_creation_expression":
        generic = value.child_by_field_name("type")
        if generic is not None and generic.type == "qualified_name":
            qualifier = generic.child_by_field_name("qualifier")
            if qualifier is None or _text(qualifier) != "Moq":
                return False
            generic = generic.child_by_field_name("name")
        if generic is None or generic.type != "generic_name" or _text(generic.named_children[0]) != "Mock":
            return False
        types = _type_arguments(generic)
        if len(types) != 1 or (names is not None and ctor_param_csharp._simple_type(types[0]) not in names):
            return False
        if value.child_by_field_name("initializer") is not None:
            return False
        arguments = value.child_by_field_name("arguments")
        return all(
            (e := ctor_param_csharp._expression(a)) is not None and ctor_param_csharp._inert(e)
            for a in (ctor_param_csharp._items(arguments) if arguments is not None else [])
        )
    if value.type == "invocation_expression":
        receiver, name, generic = _call_name(value)
        arguments = value.child_by_field_name("arguments")
        types = _type_arguments(generic)
        return (
            receiver is not None
            and _text(receiver) == "Substitute"
            and name is not None
            and _text(name) == "For"
            and len(types) == 1
            and (names is None or ctor_param_csharp._simple_type(types[0]) in names)
            and arguments is not None
            and not ctor_param_csharp._items(arguments)
        )
    return False


def _declarators(declaration: Node) -> list[Node]:
    variable = declaration if declaration.type == "variable_declaration" else next(
        (c for c in declaration.named_children if c.type == "variable_declaration"), None
    )
    return [c for c in (variable.named_children if variable is not None else []) if c.type == "variable_declarator"]


def _initial_value(declarator: Node) -> Node | None:
    values = [c for c in declarator.named_children if c != declarator.child_by_field_name("name") and c.type != "identifier"]
    if len(values) == 1 and values[0].type == "equals_value_clause":
        values = values[0].named_children
    return values[0] if len(values) == 1 else None


def _binding_of(node: Node, names: frozenset[str]) -> tuple[str, Node] | None:
    """``(name, scope)`` of the field or local a family spelling is the
    double of: ``Mock<I> m``, ``var m = new Mock<I>()``, ``_m = new
    Mock<I>()``, ``Substitute.For<I>()`` the same ways, or ``I s`` (a
    substitute's declared type)."""
    spelled = _type_position(node)
    holder = spelled.parent
    creation: Node | None = None
    if holder is not None and holder.type == "type_argument_list":
        generic = holder.parent
        user = generic.parent if generic is not None else None
        if user is not None and user.type == "qualified_name":
            user = user.parent
        if user is not None and user.type == "object_creation_expression":
            creation = user
        elif user is not None and user.type == "member_access_expression" and user.parent is not None and user.parent.type == "invocation_expression":
            creation = user.parent
        elif user is not None and user.type == "variable_declaration":
            holder = user
        else:
            return None
    if creation is not None:
        if not _double_creation(creation, names):
            return None
        parent = creation.parent
        if parent is not None and parent.type == "equals_value_clause":
            parent = parent.parent
        if parent is not None and parent.type == "variable_declarator":
            holder = parent.parent
        elif parent is not None and parent.type == "assignment_expression":
            left = parent.child_by_field_name("left")
            if left is not None and left.type == "member_access_expression":
                left = left.child_by_field_name("name")
            if left is None or left.type != "identifier":
                return None
            scope = _enclosing(parent, ("class_declaration",))
            return (_text(left), scope) if scope is not None else None
        else:
            return None
    if holder is None or holder.type != "variable_declaration":
        return None
    declarators = _declarators(holder)
    if len(declarators) != 1:
        return None
    name = ctor_param_csharp._declarator_name(declarators[0])
    owner = holder.parent
    if owner is None or name is None:
        return None
    if owner.type == "field_declaration":
        scope = _enclosing(owner, ("class_declaration",))
    elif owner.type == "local_declaration_statement":
        scope = _enclosing(owner, _SCOPES)
    else:
        return None
    return (name, scope) if scope is not None else None


def _construct_only(method: Node, names: frozenset[str]) -> bool:
    """A test that only builds a family member (and the doubles it is built
    from) and asserts a literal: ``[Fact]`` or ``[Theory]``, nothing but
    double locals, ``var g = new W(<inert or double>…)`` read by nothing or a
    bare ``new W(…);``, and ``Assert.True(true)``/``Assert.False(false)``."""
    from flag_cleanup import wrapper_csharp

    if method.type != "method_declaration":
        return False
    attributes = set(wrapper_csharp._attributes(method))
    if not attributes & {"Fact", "Theory"} or attributes - _TEST_ATTRIBUTES:
        return False
    body = method.child_by_field_name("body")
    if body is None or body.type != "block":
        return False
    doubles: set[str] = set()
    built = False
    unread: list[str] = []

    def buildable(value: Node | None) -> bool:
        if value is None or value.type != "object_creation_expression":
            return False
        if ctor_param_csharp._simple_type(value.child_by_field_name("type")) not in names:
            return False
        if value.child_by_field_name("initializer") is not None:
            return False
        arguments = value.child_by_field_name("arguments")
        for argument in ctor_param_csharp._items(arguments) if arguments is not None else []:
            expression = ctor_param_csharp._expression(argument)
            if expression is None:
                return False
            if expression.type == "identifier" and _text(expression) in doubles:
                continue
            if expression.type == "member_access_expression":
                receiver = expression.child_by_field_name("expression")
                member = expression.child_by_field_name("name")
                if receiver is not None and _text(receiver) in doubles and member is not None and _text(member) == "Object":
                    continue
            if not ctor_param_csharp._inert(expression):
                return False
        return True

    for statement in body.named_children:
        if statement.type == "comment":
            continue
        if statement.type == "local_declaration_statement":
            declarators = _declarators(statement)
            if len(declarators) != 1:
                return False
            name = ctor_param_csharp._declarator_name(declarators[0])
            value = _initial_value(declarators[0])
            if _double_creation(value) and name is not None:
                doubles.add(name)
                continue
            if buildable(value) and name is not None:
                built = True
                unread.append(name)
                continue
            return False
        if statement.type == "expression_statement" and len(statement.named_children) == 1:
            expression = statement.named_children[0]
            if buildable(expression):
                built = True
                continue
            if expression.type == "invocation_expression":
                receiver, member, _ = _call_name(expression)
                arguments = expression.child_by_field_name("arguments")
                items = ctor_param_csharp._items(arguments) if arguments is not None else []
                if (
                    receiver is not None
                    and _text(receiver) == "Assert"
                    and member is not None
                    and len(items) == 1
                    and (_text(member), _text(items[0])) in _ASSERT_LITERALS
                ):
                    continue
        return False
    for name in unread:
        if sum(1 for n in _walk(body) if n.type == "identifier" and _text(n) == name) != 1:
            return False
    return built


def _test_doubles(
    pending: list[tuple[str, Node]],
    files: dict[str, _File],
    residue: Residue,
    names: frozenset[str],
    fulls: frozenset[str],
    repo_dir: str,
) -> None:
    """Add the K5 deletions: in a test project only, each test that only
    builds a family member, each double of a family type whose every use is
    its declaration, its creation and an argument something else here
    deletes, and each test class those leave without members (its file too
    when nothing else is declared in it)."""
    from flag_cleanup import wrapper_csharp

    tests: dict[str, bool | None] = {}

    def in_tests(path: str) -> bool:
        if path not in tests:
            tests[path] = wrapper_csharp._test_project(path, repo_dir)
        return tests[path] is True

    def covered(path: str, start: int) -> bool:
        return _inside([(s, e) for s, e, _ in residue.spans.get(path, [])], start)

    readers = wrapper_csharp._Readers(repo_dir)
    bindings: dict[tuple[str, str, int], tuple[str, Node]] = {}
    touched: set[str] = set()
    for path, node in pending:
        file = files[path]
        if covered(path, node.start_byte + file.offset) or not in_tests(path):
            continue
        spelled = _type_position(node)
        creation = spelled.parent
        method = _enclosing(node, ("method_declaration",))
        if (
            creation is not None
            and creation.type == "object_creation_expression"
            and creation.child_by_field_name("type") == spelled
            and method is not None
            and _construct_only(method, names)
        ):
            start, end = ctor_param_csharp.declaration_span(method, file.data[file.offset :])
            if (path, start + file.offset, end + file.offset, "member") not in {(path, *s) for s in residue.spans.get(path, [])}:
                residue.spans.setdefault(path, []).append((start + file.offset, end + file.offset, "member"))
                owner = _enclosing(method, ("class_declaration",))
                label = f"{_text(owner.child_by_field_name('name'))}." if owner is not None else ""
                residue.rows.append((path, method.start_point[0] + 1, f"test {label}{_text(method.child_by_field_name('name'))}"))
                touched.add(path)
            continue
        binding = _binding_of(node, names)
        if binding is not None:
            name, scope = binding
            bindings.setdefault((path, name, scope.start_byte), (name, scope))

    for (path, name, _), (_, scope) in sorted(bindings.items(), key=lambda kv: kv[0]):
        file = files[path]
        reader = readers.get(path)
        if reader is None or not wrapper_csharp._bound_only_as_mock(reader.root, name, reader, readers, set(fulls)):
            continue
        spans: list[tuple[int, int, str]] = []
        ok = True
        for use in (n for n in _walk(scope) if n.type == "identifier" and _text(n) == name):
            parent = use.parent
            if parent is not None and parent.type == "variable_declarator" and parent.child_by_field_name("name") == use:
                declaration = parent.parent.parent if parent.parent is not None else None
                value = _initial_value(parent)
                if declaration is None or len(_declarators(parent.parent)) != 1 or (value is not None and not _double_creation(value, names)):
                    ok = False
                    break
                if declaration.type == "field_declaration" and _modifiers(declaration) & {"public", "protected", "internal"}:
                    # Visible outside its class: its uses are not all here.
                    ok = False
                    break
                form = "member" if declaration.type == "field_declaration" else "statement"
                start, end = ctor_param_csharp.declaration_span(declaration, file.data[file.offset :])
                spans.append((start + file.offset, end + file.offset, form))
                continue
            target = parent
            if target is not None and target.type == "member_access_expression" and target.child_by_field_name("name") == use:
                receiver = target.child_by_field_name("expression")
                if receiver is not None and receiver.type in ("this_expression", "this"):
                    target = target.parent
            assignment = target if target is not None and target.type == "assignment_expression" else None
            if (
                assignment is not None
                and assignment.child_by_field_name("right") is not None
                and _double_creation(assignment.child_by_field_name("right"), names)
                and assignment.parent is not None
                and assignment.parent.type == "expression_statement"
                and assignment.parent.parent is not None
                and assignment.parent.parent.type == "block"
            ):
                statement = assignment.parent
                start, end = ctor_param_csharp.declaration_span(statement, file.data[file.offset :])
                spans.append((start + file.offset, end + file.offset, "statement"))
                continue
            if covered(path, use.start_byte + file.offset):
                continue
            ok = False
            break
        if not ok or not spans:
            continue
        residue.spans.setdefault(path, []).extend(spans)
        residue.rows.append((path, scope.start_point[0] + 1, f"test double {name}"))
        touched.add(path)

    # A test class every member of which is deleted goes whole, and its
    # file when nothing else is declared in it.
    for path in sorted(touched):
        file = files[path]
        spans = [(s, e) for s, e, _ in residue.spans.get(path, [])]
        emptied: list[Node] = []
        for declaration in (n for n in _walk(file.root) if n.type == "class_declaration"):
            body = declaration.child_by_field_name("body")
            members = [m for m in (body.named_children if body is not None else []) if m.type != "comment"]
            if not members or not all(_inside(spans, m.start_byte + file.offset) for m in members):
                continue
            # Inherited tests run in it, a class attribute configures them,
            # another part may hold more, or something else names it: it
            # stays, emptied.
            if "partial" in _modifiers(declaration) or any(
                c.type in ("base_list", "attribute_list", "type_parameter_list") for c in declaration.children
            ):
                continue
            own = ctor_param_csharp.declaration_span(declaration, file.data[file.offset :])
            if _named_elsewhere(_text(declaration.child_by_field_name("name")), path, (own[0] + file.offset, own[1] + file.offset), repo_dir):
                continue
            emptied.append(declaration)
        for declaration in emptied:
            start, end = ctor_param_csharp.declaration_span(declaration, file.data[file.offset :])
            residue.spans[path].append((start + file.offset, end + file.offset, "type"))
            residue.rows.append((path, declaration.start_point[0] + 1, f"empty test class {_text(declaration.child_by_field_name('name'))}"))
        spans = [(s, e) for s, e, _ in residue.spans.get(path, [])]
        if emptied and _reduces_to_nothing(file, spans):
            ignored = {f.resolved for f in source_index.files(repo_dir, (".cs",), with_ignored=True) if f.ignored}
            if path not in ignored and Path(repo_dir).resolve() in Path(path).parents:
                residue.delete.add(path)
                residue.spans.pop(path, None)
                residue.rows.append((path, 1, "file deleted: nothing else was declared in it"))


def _named_elsewhere(name: str, path: str, span: tuple[int, int], repo_dir: str) -> bool:
    """Whether any file spells ``name`` outside ``span`` of ``path``: a
    fixture in ``IClassFixture<T>``, a ``typeof``, a doc reference."""
    word = _word(frozenset({name}))
    for file in _text_files(repo_dir):
        try:
            data = file.read_bytes()
        except OSError:
            return True
        if _binary(data):
            continue
        for match in word.finditer(data):
            if str(file.resolve()) != path or not (span[0] <= match.start() < span[1]):
                return True
    return False


# --- the options class a config sibling read (K7a, K7d) ------------------------

#: Calls that bind an options class to a configuration section.
_BINDERS = frozenset({"Configure", "AddOptions"})
_BINDER_SETTINGS = re.compile(rb"\b(?:ErrorOnUnknownConfiguration|BindNonPublicProperties)\b")
_APPSETTINGS = re.compile(r"^appsettings(?:\.[^/\\]+)?\.json$", re.IGNORECASE)


@dataclass
class _Options:
    """What deleting the options class adds to the transaction."""

    name: str
    deleted: list[tuple[str, str]]
    spans: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    delete: set[str] = field(default_factory=set)
    rows: list[tuple[str, int, str]] = field(default_factory=list)
    prose: list[tuple[str, int, str]] = field(default_factory=list)
    rewrites: dict[str, list[tuple[bytes, bytes]]] = field(default_factory=dict)
    config: dict[str, str] = field(default_factory=dict)

    def merge(self, residue: Residue) -> None:
        for path, spans in self.spans.items():
            residue.spans.setdefault(path, []).extend(spans)
        residue.delete |= self.delete
        residue.rows += self.rows
        residue.prose += self.prose
        for path, rewrites in self.rewrites.items():
            residue.rewrites.setdefault(path, []).extend(rewrites)
        residue.config.update(self.config)


def _knob(sibling, targets: list[Target], repo_dir: str) -> tuple[str, str] | None:
    """``(options type, property)`` a config sibling's member read:
    ``_options.Enabled`` where ``_options`` is a field of the sibling."""
    parts = sibling.returns.split(".")
    if len(parts) != 2 or not all(re.fullmatch(r"[^\W\d]\w*", p) for p in parts):
        return None
    target = next((t for t in targets if t.kind == "sibling" and t.name == sibling.owner), None)
    if target is None:
        return None
    file = _read(target.path)
    node = next(
        (n for n in _walk(file.root) if n.type == "class_declaration" and _text(n.child_by_field_name("name")) == sibling.owner),
        None,
    )
    body = node.child_by_field_name("body") if node is not None else None
    field_node = ctor_param_csharp._field(body, parts[0]) if body is not None else None
    if field_node is None:
        return None
    type_name = ctor_param_csharp._simple_type(ctor_param_csharp._field_type(field_node))
    return (type_name, parts[1]) if type_name is not None else None


def _section_of(value: Node | None, consts: dict[str, str], options: str) -> str | None:
    """The section a binder argument names: ``"Gate"``, or ``O.SectionName``
    for a string constant of the options class."""
    if value is None:
        return None
    if value.type == "string_literal" and not any(c.type == "interpolation" for c in value.children):
        return ctor_param_csharp._literal(value)
    if value.type == "member_access_expression":
        receiver = value.child_by_field_name("expression")
        name = value.child_by_field_name("name")
        if receiver is not None and name is not None and _text(receiver) == options:
            return consts.get(_text(name))
    return None


def _binding(
    node: Node, options: str, consts: dict[str, str], rooted: Callable[[Node], bool] = lambda _: False
) -> tuple[Node, str, str] | str | None:
    """``(statement, text, section)`` when a spelling of the options class is
    the type argument of a statement binding it to a section; why not, when
    it binds it some other way; ``None`` when it is no binding."""
    statement = _statement_of(node)
    if statement is None or len(statement.named_children) != 1:
        return None
    invocation = statement.named_children[0]
    if invocation.type != "invocation_expression":
        return None
    receiver, name, generic = _call_name(invocation)
    text = " ".join(_text(statement).split())
    chained = None
    if receiver is not None and receiver.type == "invocation_expression":
        chained = (invocation, _text(name) if name is not None else "")
        invocation = receiver
        receiver, name, generic = _call_name(invocation)
    if name is None or _text(name) not in _BINDERS or not _within(node, generic):
        return None
    if receiver is None or receiver.type != "identifier" or statement.parent is None or statement.parent.type != "block":
        return f"a binding this pass cannot read ({text[:60]})"
    if [ctor_param_csharp._simple_type(t) for t in _type_arguments(generic)] != [options]:
        return f"a binding this pass cannot read ({text[:60]})"

    def section_argument(call: Node, method: str) -> str | None:
        arguments = call.child_by_field_name("arguments")
        items = ctor_param_csharp._items(arguments) if arguments is not None else []
        values = [ctor_param_csharp._expression(a) for a in items]
        if len(values) != 1 or values[0] is None:
            return None
        if method == "BindConfiguration":
            return _section_of(values[0], consts, options)
        value = values[0]
        if value.type != "invocation_expression":
            return None
        config, getter, _ = _call_name(value)
        inner = value.child_by_field_name("arguments")
        inner_items = ctor_param_csharp._items(inner) if inner is not None else []
        if config is None or getter is None or _text(getter) != "GetSection":
            return None
        if not rooted(config):
            raise _Unrooted(_text(config))
        if len(inner_items) != 1:
            return None
        return _section_of(ctor_param_csharp._expression(inner_items[0]), consts, options)

    try:
        return _bound_section(statement, text, name, chained, invocation, section_argument)
    except _Unrooted as unrooted:
        return (
            f"the binding reads a section of {unrooted.args[0]}, which is not proven to be the "
            f"configuration root ({text[:60]})"
        )


#: Hosts whose ``Configuration`` is the application's root configuration.
_HOST_TYPES = frozenset(
    {
        "WebApplicationBuilder", "HostApplicationBuilder", "IHostApplicationBuilder", "WebApplication",
        "HostBuilderContext", "WebHostBuilderContext",
    }
)
#: The framework namespaces those types live in: a repository type of the
#: same name elsewhere is not one.
_HOST_NAMESPACES = frozenset({"Microsoft.AspNetCore.Builder", "Microsoft.Extensions.Hosting", "Microsoft.AspNetCore.Hosting"})
#: Where every framework type the root proof recognises by name lives.
_FRAMEWORK_NAMESPACES = _HOST_NAMESPACES | {"Microsoft.Extensions.Configuration"}
#: ``(type, method)`` factories returning a host builder.
_HOST_FACTORIES = frozenset(
    {("WebApplication", "CreateBuilder"), ("WebApplication", "CreateSlimBuilder"), ("WebApplication", "CreateEmptyBuilder"),
     ("Host", "CreateApplicationBuilder"), ("Host", "CreateEmptyApplicationBuilder")}
)
#: Host contexts a typed callback parameter may be declared as.
_HOST_CONTEXTS = frozenset({"HostBuilderContext", "WebHostBuilderContext"})
#: Framework host builders whose configuring callbacks pass a host context.
_HOST_BUILDERS = frozenset({"IHostBuilder", "IWebHostBuilder", "HostBuilder", "ConfigureHostBuilder", "ConfigureWebHostBuilder"})
#: The framework's builder methods that return the same builder, a closed
#: list: a call of any other name in a builder chain proves nothing, since a
#: repository extension of that name could return anything.
_BUILDER_CHAIN = frozenset(
    {
        "ConfigureServices", "ConfigureAppConfiguration", "ConfigureHostConfiguration", "ConfigureLogging",
        "ConfigureContainer", "ConfigureDefaults", "ConfigureHostOptions", "ConfigureMetrics",
        "ConfigureWebHostDefaults", "ConfigureWebHost", "UseEnvironment", "UseContentRoot",
        "UseDefaultServiceProvider", "UseServiceProviderFactory", "UseConsoleLifetime",
    }
)
#: ``(type, method)`` factories returning one.
_BUILDER_FACTORIES = frozenset({("Host", "CreateDefaultBuilder"), ("WebHost", "CreateDefaultBuilder")})
#: Builder callbacks whose first lambda parameter is a host context.
_HOST_CALLBACKS = frozenset({"ConfigureServices", "ConfigureAppConfiguration"})
#: Declared types that are always the configuration root.
_CONFIG_ROOTS = frozenset({"IConfigurationRoot", "ConfigurationManager", "IConfigurationManager"})


def _rooted(repo_dir: str) -> Callable[[Node], bool]:
    """Whether a binder's configuration is provably the root, so that
    ``GetSection("Gate")`` names the top-level ``Gate`` key: a host's
    ``….Configuration``; a parameter declared ``IConfigurationRoot`` (or a
    configuration manager); or a parameter declared ``IConfiguration``,
    never reassigned or shadowed in its method, of a method every call of
    which passes ``….Configuration`` for it (an ``IConfigurationSection``
    is an ``IConfiguration`` too, so the declared type alone proves
    nothing). Anything else, a local included, is not proven."""

    def _host(receiver: Node) -> bool:
        """Whether ``receiver`` is provably a host builder or context, whose
        ``Configuration`` is the root: declared with one of
        :data:`_HOST_TYPES` (a parameter, a field, a local), a local
        initialised by a host factory (``WebApplication.CreateBuilder(…)``),
        or the context parameter of ``ConfigureServices``/
        ``ConfigureAppConfiguration``; and no type of that name is declared
        in the repository outside the framework's own namespaces."""
        if receiver.type != "identifier":
            return False
        name = _text(receiver)
        scope = _enclosing(receiver, ("method_declaration", "constructor_declaration", "local_function_statement", "lambda_expression"))
        while scope is not None:
            if scope.type == "lambda_expression":
                params = _lambda_parameters(scope)
                if name in params:
                    # Typed `(HostBuilderContext ctx, …) =>`: the declaration says so.
                    listed = next((c for c in scope.children if c.type == "parameter_list"), None)
                    for parameter in ctor_param_csharp._parameters(listed) if listed is not None else []:
                        if (n := parameter.child_by_field_name("name")) is not None and _text(n) == name:
                            declared = parameter.child_by_field_name("type")
                            if declared is not None:
                                return _host_type(declared, _HOST_CONTEXTS)
                    # Untyped: only the context of a framework host builder's
                    # own callback, the builder itself proven.
                    call = scope.parent.parent.parent if scope.parent is not None and scope.parent.parent is not None else None
                    receiver, callee, _ = _call_name(call) if call is not None and call.type == "invocation_expression" else (None, None, None)
                    return (
                        callee is not None
                        and _text(callee) in _HOST_CALLBACKS
                        and _repository_methods(repo_dir, frozenset({_text(callee)})) is None
                        and len(params) == 2
                        and params.index(name) == 0
                        and receiver is not None
                        and _host_builder(receiver)
                    )
            else:
                parameters = scope.child_by_field_name("parameters")
                for parameter in ctor_param_csharp._parameters(parameters) if parameters is not None else []:
                    if (n := parameter.child_by_field_name("name")) is not None and _text(n) == name:
                        return _host_type(parameter.child_by_field_name("type"))
                for declarator in (d for d in _walk(scope) if d.type == "variable_declarator"):
                    if ctor_param_csharp._declarator_name(declarator) != name:
                        continue
                    declaration = declarator.parent
                    declared = declaration.child_by_field_name("type") if declaration is not None else None
                    if declared is not None and declared.type != "implicit_type":
                        return _host_type(declared)
                    value = _initial_value(declarator)
                    if value is None or value.type != "invocation_expression":
                        return False
                    factory, made, _ = _call_name(value)
                    return (
                        factory is not None
                        and made is not None
                        and (_text(factory).split(".")[-1], _text(made)) in _HOST_FACTORIES
                        and _framework_name(_text(factory).split(".")[-1], factory)
                    )
            scope = _enclosing(scope, ("method_declaration", "constructor_declaration", "local_function_statement", "lambda_expression"))
        return False

    def _host_type(type_node: Node | None, kinds: frozenset[str] = _HOST_TYPES) -> bool:
        """Whether a declared type is one of ``kinds``: by its simple name,
        when no repository type of that name lives outside the framework's
        namespaces and no ``using`` alias spells it (in the file, or a
        ``global using`` anywhere)."""
        simple = ctor_param_csharp._simple_type(type_node)
        if simple not in kinds or type_node is None:
            return False
        return _framework_name(simple, type_node)

    def _framework_name(simple: str, at: Node) -> bool:
        """Whether ``simple``, spelled at ``at``, can only mean the
        framework's type of that name: no repository type of the name lives
        outside the framework's namespaces, and no ``using`` alias spells
        it (in the file, a ``global using`` anywhere, a project
        ``<Using Alias>``). Every framework type or member this proof
        recognises by its spelling goes through here."""
        index = xcs.csharp_index(repo_dir)
        if index.spelled_where_unparsed(simple):
            return False
        if not all(info.namespace in _FRAMEWORK_NAMESPACES for info in index.by_last.get(simple, ())):
            return False
        root = at
        while root.parent is not None:
            root = root.parent
        if any(
            n.type == "using_directive" and (alias := n.child_by_field_name("name")) is not None and _text(alias) == simple
            for n in _walk(root)
        ):
            return False
        return not _globally_aliased(simple)

    def _globally_aliased(simple: str) -> bool:
        pattern = re.compile(rb"\bglobal\s+using\s+" + re.escape(simple.encode()) + rb"\s*=")
        project = re.compile(rb"<Using\s+[^>]*Alias=\"" + re.escape(simple.encode()) + rb"\"")
        for source in source_index.files(repo_dir, (".cs", ".csproj", ".props", ".targets"), with_ignored=True):
            try:
                data = source.path.read_bytes()
            except OSError:
                return True
            if (pattern if source.path.suffix == ".cs" else project).search(data):
                return True
        return False

    def _host_builder(receiver: Node) -> bool:
        """Whether ``receiver`` is provably a framework host builder:
        declared ``IHostBuilder``/``IWebHostBuilder``/``IHostApplicationBuilder``
        (or kin), a host's ``.Host``/``.WebHost``, or a configuring call on
        one (``Host.CreateDefaultBuilder(args).ConfigureLogging(…)``)."""
        if receiver.type == "identifier":
            name = _text(receiver)
            scope = _enclosing(receiver, ("method_declaration", "constructor_declaration", "local_function_statement"))
            parameters = scope.child_by_field_name("parameters") if scope is not None else None
            for parameter in ctor_param_csharp._parameters(parameters) if parameters is not None else []:
                if (n := parameter.child_by_field_name("name")) is not None and _text(n) == name:
                    return _host_type(parameter.child_by_field_name("type"), _HOST_BUILDERS)
            return False
        if receiver.type == "member_access_expression":
            member = receiver.child_by_field_name("name")
            inner = receiver.child_by_field_name("expression")
            return member is not None and _text(member) in ("Host", "WebHost") and inner is not None and _host(inner)
        if receiver.type == "invocation_expression":
            inner, made, _ = _call_name(receiver)
            if inner is None or made is None:
                return False
            if (_text(inner).split(".")[-1], _text(made)) in _BUILDER_FACTORIES:
                return _framework_name(_text(inner).split(".")[-1], inner)
            return (
                _text(made) in _BUILDER_CHAIN
                and _repository_methods(repo_dir, frozenset({_text(made)})) is None
                and _host_builder(inner)
            )
        return False


    def rooted(node: Node, depth: int = 0) -> bool:
        if node.type == "member_access_expression":
            member = node.child_by_field_name("name")
            receiver = node.child_by_field_name("expression")
            return (
                member is not None
                and _text(member) == "Configuration"
                and receiver is not None
                and _host(receiver)
            )
        if node.type != "identifier":
            return False
        name = _text(node)
        method = _enclosing(node, ("method_declaration", "constructor_declaration", "local_function_statement"))
        if method is None or _enclosing(node, ("lambda_expression", "anonymous_method_expression")) is not None:
            return False
        for other in _walk(method):
            if other.type == "variable_declarator" and ctor_param_csharp._declarator_name(other) == name:
                return False
            if other.type == "assignment_expression":
                left = other.child_by_field_name("left")
                if left is not None and _text(left) == name:
                    return False
        parameters = method.child_by_field_name("parameters")
        listed = ctor_param_csharp._parameters(parameters) if parameters is not None else []
        found = [(i, p) for i, p in enumerate(listed) if (n := p.child_by_field_name("name")) is not None and _text(n) == name]
        if len(found) != 1:
            return False
        index, parameter = found[0]
        declared_node = parameter.child_by_field_name("type")
        declared = ctor_param_csharp._simple_type(declared_node)
        if declared is None or declared_node is None or not _framework_name(declared, declared_node):
            return False
        if declared in _CONFIG_ROOTS:
            return True
        if declared != "IConfiguration" or depth > 0 or method.type != "method_declaration":
            return False
        method_name = _text(method.child_by_field_name("name"))
        extension = bool(listed) and any(c.type == "modifier" and _text(c) == "this" for c in listed[0].children)
        word = _word(frozenset({method_name}))
        calls = 0
        for source in source_index.files(repo_dir, (".cs",), with_ignored=True):
            try:
                data = source.path.read_bytes()
            except OSError:
                return False
            if word.search(data) is None:
                continue
            try:
                current = _parse(data.lstrip(b"\xef\xbb\xbf").decode("utf-8"), "csharp")
            except UnicodeDecodeError:
                return False
            for spelled in (n for n in _walk(current) if n.type == "identifier" and _text(n) == method_name):
                if spelled.parent is not None and spelled.parent.type == "method_declaration":
                    continue
                callee = spelled
                if callee.parent is not None and callee.parent.type == "generic_name":
                    callee = callee.parent
                via_member = callee.parent is not None and callee.parent.type == "member_access_expression" and callee.parent.child_by_field_name("name") == callee
                holder = callee.parent if via_member else callee
                call = holder.parent
                if call is None or call.type != "invocation_expression" or call.child_by_field_name("function") != holder:
                    return False  # a method group, or a name this cannot read
                items = ctor_param_csharp._items(call.child_by_field_name("arguments"))
                # `services.Register(…)` passes the receiver for `this`;
                # `Startup.Register(services, …)` passes it first.
                owner = _enclosing(method, ("class_declaration",))
                owner_name = _text(owner.child_by_field_name("name")) if owner is not None else None
                receiver_text = _text(holder.child_by_field_name("expression")) if via_member else ""
                static_form = owner_name is not None and receiver_text.split(".")[-1] == owner_name
                position = index - 1 if extension and via_member and not static_form else index
                argument = next(
                    (a for a in items if (n := a.child_by_field_name("name")) is not None and _text(n) == name),
                    items[position] if 0 <= position < len(items) and all(a.child_by_field_name("name") is None for a in items[: position + 1]) else None,
                )
                value = ctor_param_csharp._expression(argument) if argument is not None else None
                if value is None or not rooted(value, depth + 1):
                    return False
                calls += 1
        return calls > 0

    return rooted


class _Unrooted(Exception):
    """A binder's configuration is not proven to be the root."""


def _bound_section(statement, text, name, chained, invocation, section_argument):
    if _text(name) == "Configure" and chained is None:
        section = section_argument(invocation, "Configure")
    elif _text(name) == "AddOptions" and chained is not None and chained[1] in ("Bind", "BindConfiguration"):
        own = invocation.child_by_field_name("arguments")
        if own is not None and ctor_param_csharp._items(own):
            return f"a binding this pass cannot read ({text[:60]})"
        section = section_argument(chained[0], chained[1])
    else:
        return f"a binding this pass cannot read ({text[:60]})"
    if section is None:
        return f"a binding whose section cannot be read ({text[:60]})"
    return statement, text, section


def _options_class(name: str, knob: str, repo_dir: str) -> tuple[str, Node, _File, dict[str, str], Node, bool]:
    """The options class's file, declaration, string constants, the knob's
    declaration, and whether it holds another setting (then only the knob
    can go, K7b)."""
    index = xcs.csharp_index(repo_dir)
    infos = index.by_last.get(name, ())
    if len(infos) != 1 or index.spelled_where_unparsed(name):
        raise Kept("", 1, f"the options class {name} is not declared exactly once")
    info = infos[0]
    file = _read(info.path)
    found = [
        n for n in _walk(file.root)
        if n.type in _TYPE_DECLARATIONS and _text(n.child_by_field_name("name")) == name
    ]
    if len(found) != 1 or found[0].type != "class_declaration":
        raise Kept(_rel(info.path, repo_dir), 1, f"the options class {name} is not one plain class")
    node = found[0]
    line = node.start_point[0] + 1
    where = _rel(info.path, repo_dir)
    from flag_cleanup import wrapper_csharp

    if not wrapper_csharp._top_level(node) or _modifiers(node) & {"partial", "static", "abstract"}:
        raise Kept(where, line, f"the options class {name} is nested, partial, static or abstract")
    if any(c.type in ("type_parameter_list", "attribute_list", "base_list") for c in node.children):
        raise Kept(where, line, f"the options class {name} is generic, has a base or carries an attribute")
    if wrapper_csharp.published(info.path, repo_dir):
        raise Kept(where, line, f"the options class {name}'s project may be packed")
    consts: dict[str, str] = {}
    seen_knob: Node | None = None
    others = False
    body = node.child_by_field_name("body")
    members = [m for m in (body.named_children if body is not None else []) if m.type != "comment"]
    knob_member = next(
        (m for m in members if m.type == "property_declaration" and _text(m.child_by_field_name("name")) == knob), None
    )
    for member in members:
        attributed = any(c.type == "attribute_list" for c in member.children)
        if attributed and member is knob_member:
            raise Kept(where, member.start_point[0] + 1, f"an attribute on {name}.{knob} ([Required], [ConfigurationKeyName], ...)")
        if attributed and member.type != "property_declaration":
            raise Kept(where, member.start_point[0] + 1, f"an attribute on a member of {name}")
        if member.type == "field_declaration" and "const" in _modifiers(member):
            for declarator in _declarators(member):
                value = _initial_value(declarator)
                key = ctor_param_csharp._declarator_name(declarator)
                if key is None or value is None or value.type != "string_literal":
                    raise Kept(where, member.start_point[0] + 1, f"{name} declares a constant that is not a string")
                consts[key] = ctor_param_csharp._literal(value)
            continue
        if member.type == "property_declaration" and _text(member.child_by_field_name("name")) == knob:
            if _modifiers(member) & {"virtual", "abstract", "override", "static", "new"}:
                raise Kept(where, member.start_point[0] + 1, f"{name}.{knob} is not a plain auto-property")
            accessors = member.child_by_field_name("accessors")
            if accessors is None or any(a.child_by_field_name("body") is not None for a in accessors.named_children if a.type == "accessor_declaration"):
                raise Kept(where, member.start_point[0] + 1, f"{name}.{knob} is not a plain auto-property")
            value = member.child_by_field_name("value")
            if value is not None and not ctor_param_csharp._inert(value):
                raise Kept(where, member.start_point[0] + 1, f"{name}.{knob}'s initial value may run code")
            seen_knob = member
            continue
        if member.type == "property_declaration":
            others = True
            continue
        raise Kept(
            where, member.start_point[0] + 1,
            f"{name} declares {_text(member).split(chr(10))[0].strip()[:50]}, which this pass cannot judge",
        )
    if seen_knob is None:
        raise Kept(where, line, f"{name} does not declare {knob}")
    return info.path, node, file, consts, seen_knob, others


def _options_tier(wrapper: wi.Wrapper, residue: Residue, targets: list[Target], repo_dir: str) -> _Options | None:
    """The options class a config sibling read (``_options.Enabled``), when
    nothing but this family and the statement binding it name it, with its
    binding and the configuration keys the binding read; ``None`` when no
    sibling reads one. :class:`Kept` keeps this tier alone."""
    knobs = {k for s in wrapper.siblings if (k := _knob(s, targets, repo_dir)) is not None}
    if not knobs:
        return None
    if len(knobs) > 1:
        raise Kept("", 1, "the siblings read more than one options setting")
    (name, knob), = knobs
    if name in {t.name for t in targets}:
        return None
    path, node, file, consts, member, others_held = _options_class(name, knob, repo_dir)
    if others_held:
        return _options_property(name, knob, path, node, file, consts, member, residue, targets, repo_dir)
    options = _Options(name, [(f"{xcs._namespace_of(node)}.{name}".lstrip("."), xcs._namespace_of(node))])
    start, end = ctor_param_csharp.declaration_span(node, file.data[file.offset :])
    own = (start + file.offset, end + file.offset)
    if not _reduces_to_nothing(file, [own]):
        options.spans.setdefault(path, []).append((*own, "type"))
    else:
        options.delete.add(path)
        options.rows.append((path, 1, "file deleted: nothing else was declared in it"))
    options.rows.append((path, node.start_point[0] + 1, f"options class {name}"))

    # Every spelling of the options class: inside what the family's
    # transaction already deletes, its own declaration, or a binding.
    scratch = Residue(residue.label, targets)
    names = frozenset({name})
    word = _word(names)
    covered = {p: [(s, e) for s, e, _ in spans] for p, spans in residue.spans.items()}
    covered.setdefault(path, []).append(own)
    sections: set[str] = set()
    binding_projects: set[str] = set()
    for spelled in _scan(names, repo_dir, scratch):
        if spelled in residue.delete or spelled in options.delete:
            continue
        current = _read(spelled)
        if current.root.has_error:
            raise Kept(_rel(spelled, repo_dir), 1, "does not parse")
        mentions = _mentions(current, word, frozenset())
        for node_ in _spellings(current, names):
            at = node_.start_byte + current.offset
            if _inside(covered.get(spelled, []), at):
                continue
            bound = _binding(node_, name, consts, _rooted(repo_dir))
            if isinstance(bound, str):
                raise Kept(_rel(spelled, repo_dir), node_.start_point[0] + 1, bound)
            if bound is None:
                raise Kept(
                    _rel(spelled, repo_dir), node_.start_point[0] + 1,
                    f"a use of the options class {name} this pass cannot remove",
                )
            statement, text, section = bound
            s_start, s_end = ctor_param_csharp.declaration_span(statement, current.data[current.offset :])
            span = (s_start + current.offset, s_end + current.offset)
            covered.setdefault(spelled, []).append(span)
            options.spans.setdefault(spelled, []).append((*span, "statement"))
            options.rows.append((spelled, statement.start_point[0] + 1, f"binding {text}"))
            sections.add(section)
            binding_projects.add(spelled)
        for mention in mentions:
            if _inside(covered.get(spelled, []), mention.at):
                continue
            if mention.kind == "cref" and mention.rewrite is not None:
                options.rewrites.setdefault(spelled, []).append(mention.rewrite)
                options.prose.append((spelled, mention.line, f"{mention.rewrite[0].decode()} is now {mention.rewrite[1].decode()}"))
            elif mention.kind == "prose":
                options.prose.append((spelled, mention.line, mention.text))
            else:
                raise Kept(_rel(spelled, repo_dir), mention.line, f"a {mention.kind} names the options class {name}")
    options.prose += scratch.prose
    if len(sections) > 1:
        raise Kept("", 1, f"{name} is bound to more than one section ({', '.join(sorted(sections))})")
    if sections:
        _config_keys(options, sections.pop(), knob, binding_projects, residue, repo_dir)
    return options


def _config_keys(
    options: _Options,
    section: str,
    knob: str,
    bindings: set[str],
    residue: Residue,
    repo_dir: str,
    exempt: dict[str, list[tuple[int, int]]] | None = None,
    whole: bool = True,
) -> None:
    """Remove ``section:knob`` from every ``appsettings*.json`` of a project
    that can load the binding, or keep the options tier: a configuration key
    the run cannot see the end of (a string in C#, an environment variable,
    a chart's values, a binder setting that rejects unknown keys) may be
    read by something this run does not delete."""
    from flag_cleanup import csharp_family

    declared = _repository_methods(repo_dir, _BINDING_MEMBERS)
    if declared is not None:
        raise Kept(
            declared[0], declared[1],
            f"the repository declares its own {declared[2]}, so the binding may not be the framework's",
        )
    path_keys = [part for part in section.split(":") if part]
    if not path_keys:
        raise Kept("", 1, f"the options section {section!r} is empty")
    joined = "(?:__|:)".join(re.escape(part) for part in [*path_keys, knob])
    # `MYAPP_Gate__Enabled`: an environment prefix ends in `_`, so only a
    # letter or digit before the key makes it another key.
    spelled = re.compile(rf"(?<![A-Za-z0-9]){joined}(?![\w])".encode(), re.IGNORECASE)
    flat = re.compile(rb"""["']""" + re.escape(":".join([*path_keys, knob]).encode()) + rb"""["']""", re.IGNORECASE)
    yaml_key = re.compile(rf"^[ \t-]*{re.escape(path_keys[-1])}[ \t]*:".encode(), re.IGNORECASE | re.MULTILINE)
    # With the class gone any read of the section binds nothing of it; with
    # the class staying (K7b) only a read of the section whole, or of the
    # key itself, can reach the removed property.
    tail = rb"""(?::[^"']*)?""" if whole else rb"""(?::""" + re.escape(knob.encode()) + rb""")?"""
    quoted = re.compile(rb"""["']""" + re.escape(section.encode()) + tail + rb"""["']""", re.IGNORECASE)
    graph = csharp_family.project_graph(repo_dir)
    projects = xcs._project_dirs(repo_dir)
    homes = {p for b in bindings for p in csharp_family._enclosing(b, projects)}

    def loads(config: Path) -> bool | None:
        enclosing = csharp_family._enclosing(str(config.resolve()), projects)
        if not enclosing or graph is None:
            return None
        pending, seen = list(enclosing), set()
        while pending:
            current = pending.pop()
            if current in homes:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(graph.get(current, ()))
        return False

    for file in _text_files(repo_dir):
        try:
            data = file.read_bytes()
        except OSError as exc:
            raise Kept(_rel(str(file), repo_dir), 1, f"could not be read: {exc}") from exc
        if _binary(data):
            continue
        resolved = str(file.resolve())
        suffix = file.suffix.lower()
        if suffix == ".cs":
            if resolved in residue.delete or resolved in options.delete:
                continue
            if _BINDER_SETTINGS.search(data):
                match = _BINDER_SETTINGS.search(data)
                raise Kept(_rel(resolved, repo_dir), _line(data, match.start()), "a binder setting rejects unknown configuration keys")
            covered = [(s, e) for s, e, _ in residue.spans.get(resolved, []) + options.spans.get(resolved, [])]
            covered += (exempt or {}).get(resolved, [])
            for match in quoted.finditer(data):
                if not _inside(covered, match.start()):
                    raise Kept(
                        _rel(resolved, repo_dir), _line(data, match.start()),
                        f"a string reads the configuration section {section}",
                    )
            continue
        if suffix in _PROSE_SUFFIXES:
            continue
        if _APPSETTINGS.match(file.name):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise Kept(_rel(resolved, repo_dir), 1, "the file is not UTF-8, so it is never rewritten") from exc
            flat_key = flat.search(data)
            if flat_key is not None:
                raise Kept(
                    _rel(resolved, repo_dir), _line(data, flat_key.start()),
                    f"{section}:{knob} is written as a flat key, which this pass does not edit",
                )
            try:
                after, removed = appsettings_edit.remove_keys(text, path_keys, [knob])
            except (appsettings_edit.ConfigError, ValueError) as exc:
                raise Kept(_rel(resolved, repo_dir), 1, f"cannot remove {section}:{knob} exactly: {exc}") from exc
            if not removed:
                continue
            loaded = loads(file)
            if loaded is None:
                raise Kept(_rel(resolved, repo_dir), 1, f"{section}:{knob} is set in a file no project can be tied to")
            if not loaded:
                options.rows.append((resolved, 1, f"left {section}:{knob}: its project cannot bind {options.name}"))
                continue
            options.config[resolved] = after
            for key in removed:
                options.rows.append((resolved, 1, f"configuration key {key}"))
            continue
        match = spelled.search(data)
        if match is None and suffix in (".json", ".yaml", ".yml", ".xml", ".config"):
            match = quoted.search(data) or (yaml_key.search(data) if suffix in (".yaml", ".yml") else None)
        if match is not None:
            raise Kept(
                _rel(resolved, repo_dir), _line(data, match.start()),
                f"the configuration section {section} is set outside appsettings (environment, chart or manifest)",
            )


#: Framework members the binding proof recognises by name.
_BINDING_MEMBERS = frozenset({"Configure", "AddOptions", "Bind", "BindConfiguration", "GetSection"})


def _repository_methods(repo_dir: str, names: frozenset[str]) -> tuple[str, int, str] | None:
    """``(path, line, name)`` of a method the repository declares under one
    of ``names``: an extension method of that name could be what a call
    binds to instead of the framework's."""
    word = _word(names)
    for source in source_index.files(repo_dir, (".cs",), with_ignored=True):
        try:
            data = source.path.read_bytes()
        except OSError:
            return (source.resolved, 1, "unreadable file")
        if word.search(data) is None:
            continue
        try:
            root = _parse(data.lstrip(b"\xef\xbb\xbf").decode("utf-8"), "csharp")
        except UnicodeDecodeError:
            return (source.resolved, 1, "file that is not UTF-8")
        for node in _walk(root):
            if node.type in ("method_declaration", "local_function_statement"):
                name = node.child_by_field_name("name")
                if name is None or _text(name) not in names:
                    continue
                namespace = xcs._namespace_of(node)
                if namespace in _FRAMEWORK_NAMESPACES or namespace.startswith("Microsoft.Extensions."):
                    continue  # the framework's own, vendored or stubbed
                return (source.resolved, node.start_point[0] + 1, f"{_text(name)}(…)")
    return None


def _options_property(
    name: str,
    knob: str,
    path: str,
    node: Node,
    file: _File,
    consts: dict[str, str],
    member: Node,
    residue: Residue,
    targets: list[Target],
    repo_dir: str,
) -> _Options:
    """K7b: the options class holds other settings and stays; only the knob
    property the sibling read goes, with its configuration key, when every
    mention that may be it is inside this transaction. Judged by the same
    verdicts as a wrapper's member (:func:`wrapper_csharp.leftovers` on a
    property-wrapper standing for ``O.Knob``): project reachability,
    family-free files, receivers and by-name routes."""
    from flag_cleanup import wrapper_csharp

    where = _rel(path, repo_dir)
    line = member.start_point[0] + 1
    index = xcs.csharp_index(repo_dir)
    full = f"{xcs._namespace_of(node)}.{name}".lstrip(".")
    if any(name in info.supertypes for infos in index.by_full.values() for info in infos):
        raise Kept(where, line, f"a type derives from the options class {name}")
    start, end = ctor_param_csharp.declaration_span(member, file.data[file.offset :])
    span = (start + file.offset, end + file.offset)
    stand_in = wi.Wrapper(
        path=path,
        language="csharp",
        name=knob,
        owner=name,
        package=xcs._namespace_of(node),
        line=line,
        start=member.start_byte + file.offset,
        end=member.end_byte + file.offset,
        kind="property",
    )
    covered: dict[str, list[tuple[int, int]]] = {p: [(a, b) for a, b, _ in spans] for p, spans in residue.spans.items()}
    covered.setdefault(path, []).append(span)
    lines: dict[str, list[tuple[int, int]]] = {}
    for spelled, spans in covered.items():
        try:
            data = Path(spelled).read_bytes()
        except OSError:
            continue
        lines[spelled] = [(_line(data, a), _line(data, max(a, b - 1))) for a, b in spans]
    for mention_path, mention_line, text in wrapper_csharp.leftovers(repo_dir, stand_in):
        resolved = str(Path(mention_path).resolve())
        if resolved in residue.delete:
            continue
        if any(a <= mention_line <= b for a, b in lines.get(resolved, [])):
            continue
        raise Kept(
            _rel(resolved, repo_dir), mention_line,
            f"{name}.{knob} may still be read here ({' '.join(text.split())[:60]})",
        )
    properties = {
        _text(m.child_by_field_name("name"))
        for m in (node.child_by_field_name("body").named_children if node.child_by_field_name("body") is not None else [])
        if m.type == "property_declaration"
    } - {knob}
    own_span = ctor_param_csharp.declaration_span(node, file.data[file.offset :])
    _flows_only_into_reads(
        name, properties, {path: [(own_span[0] + file.offset, own_span[1] + file.offset)]}, covered, consts, repo_dir,
        residue.delete,
    )
    options = _Options(name, [])
    options.spans.setdefault(path, []).append((*span, "member"))
    options.rows.append((path, line, f"options property {name}.{knob}"))
    # The class stays, and so do its bindings: they tell which key it read.
    sections: set[str] = set()
    bindings: set[str] = set()
    scratch = Residue(residue.label, targets)
    for spelled in _scan(frozenset({name}), repo_dir, scratch):
        current = _read(spelled)
        for node_ in _spellings(current, frozenset({name})):
            bound = _binding(node_, name, consts, _rooted(repo_dir))
            if isinstance(bound, str):
                raise Kept(_rel(spelled, repo_dir), node_.start_point[0] + 1, bound)
            if bound is not None:
                sections.add(bound[2])
                bindings.add(spelled)
    if len(sections) > 1:
        raise Kept("", 1, f"{name} is bound to more than one section ({', '.join(sorted(sections))})")
    if sections:
        own = ctor_param_csharp.declaration_span(node, file.data[file.offset :])
        _config_keys(
            options, sections.pop(), knob, bindings, residue, repo_dir,
            exempt={path: [(own[0] + file.offset, own[1] + file.offset)]}, whole=False,
        )
    return options


_OPTION_WRAPPERS = frozenset({"IOptions", "IOptionsSnapshot", "IOptionsMonitor"})
_OPTION_VALUES = frozenset({"Value", "CurrentValue"})


def _flows_only_into_reads(
    name: str,
    properties: set[str],
    exempt: dict[str, list[tuple[int, int]]],
    covered: dict[str, list[tuple[int, int]]],
    consts: dict[str, str],
    repo_dir: str,
    deleted: set[str] = frozenset(),
) -> None:
    """Raise :class:`Kept` unless every value of the options class is only
    ever read for one of its other ``properties``: each spelling of the
    class outside the transaction, its own declaration and its bindings is
    the declared type of a private field, a parameter or a local (or
    ``IOptions<O>`` and kin of one), and every use of that binding reads a
    known property (``o.X``, ``o.Value.X``) or stores the value in another
    such binding. A value that is serialised, returned, passed, reflected
    over or bound again would see the removed property go."""
    word = _word(frozenset({name}))
    for spelled in _scan(frozenset({name}), repo_dir, Residue("", [])):
        if spelled in deleted:
            continue
        current = _read(spelled)
        skip = exempt.get(spelled, []) + covered.get(spelled, [])
        where = _rel(spelled, repo_dir)
        for node in _spellings(current, frozenset({name})):
            at = node.start_byte + current.offset
            if _inside(skip, at):
                continue
            if _binding(node, name, consts) is not None:
                continue
            parent = node.parent
            if (
                parent is not None
                and parent.type == "member_access_expression"
                and parent.child_by_field_name("expression") == node
                and (member := parent.child_by_field_name("name")) is not None
                and _text(member) in consts
            ):
                continue  # `O.SectionName`: a constant, no value of the class
            binding = _value_binding(node)
            if binding is None:
                raise Kept(where, node.start_point[0] + 1, f"a value of {name} is used in a way this pass cannot follow")
            bound, scope, wrapped = binding
            for use in (n for n in _walk(scope) if n.type == "identifier" and _text(n) == bound):
                if use.parent is not None and use.parent.type in ("variable_declarator", "parameter") and use.parent.child_by_field_name("name") == use:
                    continue
                if not _known_read(use, properties, wrapped, name):
                    raise Kept(
                        where, use.start_point[0] + 1,
                        f"a value of {name} flows where the removed setting may be seen ({_text(use.parent)[:50]})",
                    )



def _value_binding(node: Node) -> tuple[str, Node, bool] | None:
    """``(name, scope, wrapped)`` when a spelling of the options class is
    the declared type of a private field, a parameter or a local, or of the
    ``IOptions<…>``-like type of one."""
    spelled = _type_position(node)
    wrapped = False
    holder = spelled.parent
    if holder is not None and holder.type == "type_argument_list":
        generic = holder.parent
        if generic is None or generic.type != "generic_name" or _text(generic.named_children[0]) not in _OPTION_WRAPPERS:
            return None
        if len(_type_arguments(generic)) != 1:
            return None
        spelled = _type_position(generic)
        holder = spelled.parent
        wrapped = True
    if holder is None:
        return None
    if holder.type == "parameter" and holder.child_by_field_name("type") == spelled:
        name = holder.child_by_field_name("name")
        scope = _enclosing(holder, _SCOPES | {"lambda_expression"})
        return (_text(name), scope, wrapped) if name is not None and scope is not None else None
    if holder.type == "variable_declaration":
        declarators = _declarators(holder)
        owner = holder.parent
        if len(declarators) != 1 or owner is None:
            return None
        bound = ctor_param_csharp._declarator_name(declarators[0])
        if owner.type == "field_declaration":
            if _modifiers(owner) & {"public", "protected", "internal"}:
                return None
            scope = _enclosing(owner, ("class_declaration", "struct_declaration", "record_declaration"))
        elif owner.type == "local_declaration_statement":
            scope = _enclosing(owner, _SCOPES)
        else:
            return None
        value = _initial_value(declarators[0])
        if value is not None:
            return None
        return (bound, scope, wrapped) if bound is not None and scope is not None else None
    return None


def _known_read(use: Node, properties: set[str], wrapped: bool, options_name: str) -> bool:
    """``o.X`` (``o.Value.X`` when wrapped) for a known property ``X``, a
    store ``_f = o`` / ``_f = o.Value`` into another binding of the class,
    or this binding stored into (its value is judged where it comes from:
    anything that yields one spells the class)."""
    current = use
    access = current.parent
    target = access
    if target is not None and target.type == "member_access_expression" and target.child_by_field_name("name") == use:
        receiver = target.child_by_field_name("expression")
        if receiver is not None and receiver.type in ("this_expression", "this"):
            target = target.parent
    if (
        target is not None
        and target.type == "assignment_expression"
        and target.child_by_field_name("left") in (use, access)
        and target.parent is not None
        and target.parent.type in ("expression_statement", "arrow_expression_clause")
    ):
        operator = next((c for c in target.children if not c.is_named), None)
        return operator is not None and _text(operator) == "="
    if wrapped:
        if access is None or access.type != "member_access_expression" or access.child_by_field_name("expression") != current:
            return False
        member = access.child_by_field_name("name")
        if member is None or _text(member) not in _OPTION_VALUES:
            return False
        current = access
        access = current.parent
    if access is not None and access.type == "member_access_expression" and access.child_by_field_name("expression") == current:
        member = access.child_by_field_name("name")
        return member is not None and _text(member) in properties
    if access is not None and access.type == "assignment_expression" and access.child_by_field_name("right") == current:
        left = access.child_by_field_name("left")
        if left is not None and left.type == "member_access_expression":
            left = left.child_by_field_name("name")
        statement = access.parent
        if left is None or left.type != "identifier" or statement is None:
            return False
        if statement.type not in ("expression_statement", "arrow_expression_clause"):
            return False
        # Into a field of the class's own type, itself judged as a binding.
        owner = _enclosing(access, ("class_declaration", "struct_declaration", "record_declaration"))
        body = owner.child_by_field_name("body") if owner is not None else None
        field_node = ctor_param_csharp._field(body, _text(left)) if body is not None else None
        return field_node is not None and ctor_param_csharp._simple_type(ctor_param_csharp._field_type(field_node)) == options_name
    return False
