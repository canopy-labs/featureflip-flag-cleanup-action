"""Go flag keys hoisted to a ``const`` read from another file.

The package is the unit. A package-level name is visible in every file of the
package (the same directory and the same ``package`` clause, test files of
that clause included), bare and with no import. From another package an
exported name is read as ``q.NAME``, where ``q`` is bound by an import of the
declaring package's import path: its alias, or its package clause.

Go constants are the simplest holder this tool resolves. A constant has no
address, no runtime identity and no name reflection can find, and Go has no
inheritance, so a constant is reached only by spelling its name: a bare name
the package's own scope resolves, or a qualified one through an import. The
lexical scope walk (:mod:`flag_cleanup.go_scope`) and the module reader
(:mod:`flag_cleanup.go_module`) answer both. No other language can read a Go
constant, so no other file type is asked.

Offered exactly what ``go_const.toml``'s delete rules remove: a package-level
``const`` spec with ONE name and ONE interpreted string literal value, typed or
not, alone in its declaration or one entry of a ``const ( ... )`` group (see
:func:`flag_cleanup.key_const._go_key_consts`). A file whose own gate keeps its
key constants offers none (:func:`flag_cleanup.key_const._go_withholds`): a
group whose entries depend on their position, since a spec with no value
repeats the expression above it and ``iota`` counts specs, or a group holding
the key twice. A ``var`` is not a constant and is never offered.

Refused, and the declaration kept: a read in a file of another module
unless :func:`flag_cleanup.go_module.resolve` proves every build of it reads
this directory (a ``replace`` directive, a workspace or a published version can
each decide what the import means), a read through a dot import, an import whose binding
the stranded-import cleanup cannot see (an unaliased path whose last segment,
version suffix dropped, is not the package clause), a qualifier or a bare name
a local declaration shadows, any mention other than the first argument of a
``BoolVariation`` call, the second of an OpenFeature ``Boolean`` call, or
either of a configured accessor, a read under ``go`` or
``defer``, a discarded read whose arguments call or receive, and any read in
an external test package that declares a named example (``go vet`` resolves
the example through that package's import, which the fold can strand).
Competing declarations, such as one per build constraint (``keys_linux.go``
beside ``keys_darwin.go``), are found by :func:`declared_names`: any
package-level name of the package counts, whatever it holds.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup import go_module, go_scope, go_setters, key_const, source_index, syntax
from flag_cleanup.unused import go_import_name

#: The SDK reads ``go_const.toml`` folds; the run's accessors are added per call.
_READ_CALLEES = key_const._GO_FLAG_READ_CALLEES
#: The SDK reads whose key is the SECOND argument (OpenFeature's ``Boolean``).
_SECOND_ARGUMENT_CALLEES = key_const._GO_SECOND_ARGUMENT_READ_CALLEES


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _parse(source: str) -> Node:
    return syntax._parse(source, "go")


def _module_id(directory: Path, clause: str) -> str:
    """A package: its resolved directory and its clause. ``flags`` and
    ``flags_test`` in one directory are two packages."""
    return f"{directory.resolve()}|{clause}"


def _package_of(module: str) -> tuple[Path, str]:
    directory, _, clause = module.rpartition("|")
    return Path(directory), clause


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    root = _parse(source)
    if root.has_error:
        return []
    clause = go_module.package_clause(root)
    if clause is None:
        return []
    module = _module_id(Path(path).parent, clause)
    data = source.encode("utf-8")
    consts = key_const._go_key_consts(root, data, flag_key)
    with key_const.go_file(path):
        withheld = key_const._go_withholds(root, data, consts)
    if withheld:
        # The file's own gate keeps every declaration of the key in it.
        return []
    return [
        xk.KeyDeclaration(path, const.name, None, module)
        for const in consts
        # Package level only: a local constant is no other file's to read.
        if const.declaration.parent is not None and const.declaration.parent.type == "source_file"
        and const.name != "_"
    ]


def entry_findings(path: str, source: str, flag_key: str) -> list[xk.Refusal]:
    """Why a typed flag handle declared here is kept: its constructor is not
    proven to carry the key (:mod:`flag_cleanup.go_handles`), or its own file
    mentions it. The in-file gate withholds such a file without a word, so
    this is what names the reason, at the declaration or the mention."""
    root = _parse(source)
    if root.has_error:
        return []
    data = source.encode("utf-8")
    consts = key_const._go_key_consts(root, data, flag_key)
    with key_const.go_file(path):
        found = key_const.go_handle_findings(root, data, consts)
    return [xk.refusal(path, node, reason, source) for node, reason in found]


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    """Every package-level name the file declares, whatever it holds: a
    ``const``, ``var``, type or function. A second declaration of the key's
    name in the package (another build constraint's file, say) means the
    build decides which one a read gets."""
    root = _parse(source)
    clause = go_module.package_clause(root)
    if root.has_error or clause is None:
        raise xk.DeclarationsUnreadable(path)
    module = _module_id(Path(path).parent, clause)
    found: set[xk.QualifiedName] = set()
    for declaration in root.named_children:
        if declaration.type == "function_declaration":
            name = declaration.child_by_field_name("name")
            if name is not None:
                found.add((module, None, _text(name)))
        elif declaration.type in ("const_declaration", "var_declaration", "type_declaration"):
            found |= {
                (module, None, _text(n))
                for n, _ in go_scope._statement_sites(declaration)
                # `name` also yields the commas of `const A, B = ...`
                if n.type in ("identifier", "type_identifier")
            }
    return found


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    """Go constants have no owner, so no type can stand in for one."""
    return set()


class _Reader:
    """One reading file: its package, its module and its imports."""

    def __init__(self, path: str, root: Node, repo_dir: str) -> None:
        self.path = path
        self.root = root
        self.directory = Path(path).resolve().parent
        self.clause = go_module.package_clause(root)
        self.stop = Path(source_index.checkout_root(repo_dir)).resolve()
        self.module = go_module.module_of(self.directory, self.stop)
        #: ``(import path, alias or None)`` for each import spec.
        self.imports: list[tuple[str, str | None]] = []
        for spec in go_scope.import_specs(root):
            path_node = spec.child_by_field_name("path")
            if path_node is None:
                continue
            alias = spec.child_by_field_name("name")
            self.imports.append((_text(path_node).strip('"`'), None if alias is None else _text(alias)))

    def same_package(self, decl: xk.KeyDeclaration) -> bool:
        directory, clause = _package_of(decl.module)
        return directory == self.directory and clause == self.clause


class _Target:
    """What a declaration's package is called from outside it."""

    def __init__(self, decl: xk.KeyDeclaration, stop: Path) -> None:
        self.directory, self.clause = _package_of(decl.module)
        self.module = go_module.module_of(self.directory, stop)
        self.import_path = None if self.module is None else self.module.import_path(self.directory)

    def named_by(self, imported: str, reader: _Reader) -> bool:
        """Whether an import of ``imported`` in ``reader`` MAY be this
        package. The path is compared as written AND resolved from the
        reader's module, since a symlinked directory or a ``replace`` gives a
        package a second import path. Without a module it may be any import.

        This must over-approximate: a read it misses is neither proven nor
        refused, and the declaration it still needs is deleted. So it is
        ``False`` only when every build is proven to read another directory,
        or none could read this one."""
        if self.import_path is None or imported == self.import_path:
            return True
        if reader.module is None:
            return False
        try:
            proven = go_module.resolve(imported, reader.module, reader.stop)
            if proven is not None:
                return proven == self.directory
            possible = go_module.may_read(imported, reader.module, reader.stop)
        except (OSError, RuntimeError):
            return True
        return possible is None or self.directory in possible

    def built_from(self, imported: str, reader: _Reader) -> bool:
        """Whether every build of ``reader`` reads this package's directory
        for ``imported``: its own module's, or another module's that the
        ``go.mod`` and every workspace resolve to this checkout alike."""
        if self.import_path is None or reader.module is None:
            return False
        return go_module.resolve(imported, reader.module, reader.stop) == self.directory


def _read_call(node: Node, accessors: frozenset[str]) -> Node | None:
    """The call ``node`` is the key of, when its callee is one the reader
    rules fold: ``node`` the FIRST argument of a ``BoolVariation`` or the
    SECOND of an OpenFeature ``Boolean``, or either of an accessor, written
    ``f(...)`` or ``x.f(...)``. A comment or parentheses around the key, or
    any other position, is not one."""
    arguments = node.parent
    if arguments is None or arguments.type != "argument_list":
        return None
    named = arguments.named_children
    if named and named[0].id == node.id:
        callees = _READ_CALLEES
    elif len(named) > 1 and named[1].id == node.id:
        callees = _SECOND_ARGUMENT_CALLEES
    else:
        return None
    call = arguments.parent
    if call is None or call.type != "call_expression" or call.child_by_field_name("type_arguments") is not None:
        return None
    function = call.child_by_field_name("function")
    if function is not None and function.type == "selector_expression":
        function = function.child_by_field_name("field")
    if function is None or function.type not in ("identifier", "field_identifier"):
        return None
    name = _text(function)
    return call if name in callees or name in accessors else None


#: Where a simple statement sits in a header rather than in a statement list.
_SIMPLE_STATEMENT_SLOTS = ("if_statement", "expression_switch_statement", "type_switch_statement", "for_clause")


def _position_reason(call: Node) -> str | None:
    """Why a read in this position is not folded cleanly, or None.

    ``go true`` and a bare ``true`` statement do not compile. A discarded read
    is deleted whole only when its statement holds no other argument list
    and no ``<-`` receive, as ``go_const.toml``'s discarded-read rule asks;
    otherwise the read rule would leave a bare ``true`` behind."""
    parent = call.parent
    if parent is not None and parent.type in ("go_statement", "defer_statement"):
        return xk.UNSUPPORTED_FORM
    while parent is not None and parent.type == "parenthesized_expression":
        # `(c.BoolVariation(k, …))` as a statement: the delete rule matches
        # only the bare call, and `(true)` is not used either.
        if parent.parent is not None and parent.parent.type == "expression_statement":
            return xk.UNSUPPORTED_FORM
        parent = parent.parent
    if parent is not None and parent.type == "expression_statement":
        holder = parent.parent
        if holder is not None and holder.type in _SIMPLE_STATEMENT_SLOTS:
            # `if <read>; x {`: deleting the statement leaves `if ; x {`.
            return xk.UNSUPPORTED_FORM
        lists = sum(1 for n in xk.walk(parent) if n.type == "argument_list")
        if lists > 1 or any(n.type == "unary_expression" for n in xk.walk(parent)):
            return xk.UNSUPPORTED_FORM
    return None


def _named_example(reader: _Reader) -> bool:
    """Whether ``reader`` is in an external test package that declares a
    named example, which ``go vet`` resolves through the package's import:
    see ``wrapper_go._example_reason``."""
    if reader.clause is None or not reader.clause.endswith("_test") or not reader.path.endswith("_test.go"):
        return False
    from flag_cleanup.wrapper_go import _NAMED_EXAMPLE

    packages = go_scope.Packages(reader.stop)
    for test_file in packages.package(reader.directory, reader.clause):
        for declaration in test_file.root.named_children:
            name = declaration.child_by_field_name("name") if declaration.type == "function_declaration" else None
            if name is not None and _NAMED_EXAMPLE.match(_text(name)):
                return True
    return False


def _setter_pin(path: str, source: str, root: Node, node: Node, spelling: str, repo_dir: str) -> bool:
    """Whether ``node`` is the key of a test setter (:mod:`flag_cleanup.go_setters`)
    the test-double pass will delete under EITHER treatment, so that the
    declaration may go with it. Asked of the pass itself, on the text as
    written: every setter of the spelling in the file has to clear, since a
    test it cannot decide keeps its setter and the file would not build
    without the constant. The runner re-checks the rewritten file with the
    setters suspended, which refuses the flag if one survived."""
    names = go_setters.declared()
    if not names or not path.endswith("_test.go"):
        return False
    spellings = frozenset({"".join(spelling.split())})
    if not any(site.key.id == node.id for site in go_setters.sites(path, root, spellings, repo_dir, names)):
        return False
    from flag_cleanup import mock_stubs

    for treatment in (True, False):
        resolved, _ = mock_stubs.resolve(
            source, "go", "\x00no-such-key", treatment, key_texts=frozenset(spellings), checkout=(repo_dir, path)
        )
        if go_setters.remaining(_parse(resolved), names, spellings):
            return False
    return True


def references(path, source, declarations, accessors, repo_dir):
    """The proven reads of ``declarations`` in ``path``, and its refusals.

    A file with any refusal yields no reads (see :func:`xk.resolve`), so
    every mention the reader rules could match by text is either proven here
    or refused: those rules compare the qualifier and the name as text, and
    cannot tell a shadowed one apart.
    """
    root = _parse(source)
    names = {d.name for d in declarations}
    if root.has_error or go_module.package_clause(root) is None:
        return [], _whole_file(path, source, names)
    reader = _Reader(path, root, repo_dir)
    accessor_set = frozenset(accessors)
    by_name: dict[str, list[xk.KeyDeclaration]] = {}
    for decl in declarations:
        by_name.setdefault(decl.name, []).append(decl)
    targets: dict[str, _Target] = {}

    def target(decl: xk.KeyDeclaration) -> _Target:
        if decl.module not in targets:
            targets[decl.module] = _Target(decl, reader.stop)
        return targets[decl.module]

    reads: list[xk.KeyReference] = []
    refused: list[xk.Refusal] = []
    #: Qualifiers proven to name a declaring package here, and every
    #: selector or bare name refused for being bound locally: a reader rule
    #: matching by text would reach the second as well, so it refuses.
    shadowed: list[tuple[Node, str | None, str]] = []
    owners: set[str] = set()
    bare_proven = False
    example = None

    for node in xk.walk(root):
        if node.type == "identifier" and _text(node) in names:
            name = _text(node)
            parent = node.parent
            if parent is not None and parent.type == "selector_expression":
                if parent.child_by_field_name("operand") is not None and parent.child_by_field_name("operand").id == node.id:
                    # `NAME.x`: a use of the constant's value (a typed key's
                    # method), never a read the rules fold.
                    if any(reader.same_package(d) for d in by_name[name]):
                        refused.append(xk.refusal(path, node, xk.NOT_A_READ, source))
                continue
            for decl in by_name[name]:
                if reader.same_package(decl):
                    if key_const._go_is_binding(node) or go_scope.local_binders(node, name):
                        refused.append(xk.refusal(path, node, xk.REBOUND, source))
                        break
                    call = _read_call(node, accessor_set)
                    if call is None:
                        if _setter_pin(path, source, root, node, name, repo_dir):
                            reads.append(xk.KeyReference(path, None, name))
                            bare_proven = True
                            break
                        refused.append(xk.refusal(path, node, xk.NOT_A_READ, source))
                        break
                    reason = _position_reason(call)
                    if reason is not None:
                        refused.append(xk.refusal(path, node, reason, source))
                        break
                    reads.append(xk.KeyReference(path, None, name))
                    bare_proven = True
                    break
                # Another package's bare name is ours only through a dot import.
                spot = target(decl)
                if any(alias == "." and spot.named_by(imported, reader) for imported, alias in reader.imports):
                    refused.append(xk.refusal(path, node, xk.IMPORT_UNRESOLVED, source))
                    break
            else:
                shadowed.append((node, None, name))
            continue
        if node.type != "selector_expression":
            continue
        field = node.child_by_field_name("field")
        operand = node.child_by_field_name("operand")
        if field is None or operand is None or _text(field) not in names:
            continue
        if operand.type != "identifier":
            continue  # `a.b.NAME`, `f().NAME`: a field, never a package's name
        name, qualifier = _text(field), _text(operand)
        if go_scope.local_binders(operand, qualifier):
            shadowed.append((node, qualifier, name))
            continue
        for decl in by_name[name]:
            if reader.same_package(decl):
                continue  # a package cannot import itself
            spot = target(decl)
            binds = [
                (imported, alias)
                for imported, alias in reader.imports
                if alias not in (".", "_")
                and spot.named_by(imported, reader)
                and qualifier in ({alias} if alias else {spot.clause, go_import_name(imported)})
            ]
            if not binds:
                continue
            imported, alias = binds[0]
            if not spot.built_from(imported, reader):
                # A file of another module (or a package outside any) may
                # import a published version of the path instead.
                refused.append(xk.refusal(path, node, xk.IMPORT_UNRESOLVED, source))
                break
            if not name[:1].isupper() or (alias is None and go_import_name(imported) != spot.clause):
                # Unexported names do not cross packages; an import whose
                # name the stranded-import cleanup would guess wrong is left
                # standing when the fold takes its last use.
                refused.append(xk.refusal(path, node, xk.IMPORT_UNRESOLVED, source))
                break
            call = _read_call(node, accessor_set)
            if call is None:
                if _setter_pin(path, source, root, node, f"{qualifier}.{name}", repo_dir):
                    reads.append(xk.KeyReference(path, qualifier, name))
                    owners.add(qualifier)
                    break
                refused.append(xk.refusal(path, node, xk.NOT_A_READ, source))
                break
            reason = _position_reason(call)
            if reason is None:
                if example is None:
                    example = _named_example(reader)
                if example:
                    reason = xk.UNSUPPORTED_FORM
            if reason is not None:
                refused.append(xk.refusal(path, node, reason, source))
                break
            reads.append(xk.KeyReference(path, qualifier, name))
            owners.add(qualifier)
            break
    for node, qualifier, name in shadowed:
        # The reader rules match `q.NAME` and a bare `NAME` by text, so a
        # shadowed spelling of a proven one would be folded too.
        if (qualifier is None and bare_proven) or (qualifier is not None and qualifier in owners):
            refused.append(xk.refusal(path, node, xk.REBOUND, source))
    if owners and not refused:
        # Any other binding of a proven qualifier's name in the file (a
        # parameter `flags int`, a local `flags := …`) is counted as a use
        # of the import by the stranded-import cleanup, which then keeps the
        # import the fold left unused, and the file does not build.
        for node in xk.walk(root):
            if node.type != "identifier" or _text(node) not in owners:
                continue
            parent = node.parent
            operand = parent.child_by_field_name("operand") if parent is not None and parent.type == "selector_expression" else None
            if operand is None or operand.id != node.id or go_scope.local_binders(node, _text(node)):
                refused.append(xk.refusal(path, node, xk.REBOUND, source))
                break
    if refused:
        return [], refused
    return reads, []


def _whole_file(path: str, source: str, names: set[str]) -> list[xk.Refusal]:
    """A file that does not parse, refused at the first line spelling a name."""
    token = xk.mention_pattern(names)
    lines = source.splitlines()
    number = next((i for i, line in enumerate(lines) if token.search(line)), 0)
    text = lines[number].strip() if lines else ""
    return [xk.Refusal(path, number + 1, xk.UNSUPPORTED_FORM, text)]


def constant_module_pattern(constants, repo_dir: str) -> re.Pattern[str]:
    """A file that may hold an example named after one of ``constants``.
    The token scan never finds ``ExampleNewCheckout`` for ``NewCheckout``."""
    names = "|".join(sorted({re.escape(d.name) for d in constants})) or "(?!)"
    return re.compile(rf"\bExample(?:{names})(?:_\w*)?\b")


def constant_escapes(path, source, constants, repo_dir) -> list[tuple[str, xk.Refusal]]:
    """An example function named after a constant keeps its declaration.

    ``go vet`` (which ``go test`` runs) resolves ``ExampleNewCheckout`` and
    ``ExampleNewCheckout_suffix`` to a package-level name of the package
    under test, so deleting ``NewCheckout`` fails every test run of that
    package even though no code reads it. Asked of every test file in the
    declaring directory, of either test clause."""
    if not path.endswith("_test.go"):
        return []
    here = Path(path).resolve().parent
    mine = [d for d in constants if Path(d.path).resolve().parent == here]
    if not mine:
        return []
    root = _parse(source)
    found: list[tuple[str, xk.Refusal]] = []
    for declaration in root.named_children:
        name = declaration.child_by_field_name("name") if declaration.type == "function_declaration" else None
        if name is None:
            continue
        for decl in mine:
            if re.fullmatch(rf"Example{re.escape(decl.name)}(?:_\w*)?", _text(name)):
                found.append((decl.path, xk.refusal(path, name, xk.UNSUPPORTED_FORM, source)))
    return found


xk.register(
    "go",
    xk.Profile(
        language_family=frozenset({"go"}),
        declarations=declarations,
        references=references,
        entry_findings=entry_findings,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        # `go vet` resolves an example's name to a package-level name.
        constant_escapes=constant_escapes,
        constant_module_pattern=constant_module_pattern,
    ),
)
