"""Whether a Python file can change a namespace without spelling the name it
binds: the opaque-gateway scan, and the whitelist a package ``__init__.py`` (or
a star's source) is held to before ``from pkg import keys`` or ``import
pkg.keys`` is trusted to reach the submodule (see
:mod:`flag_cleanup.cross_file_python`)."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from flag_cleanup import cross_file_keys as xk
from flag_cleanup.cross_file_python_syntax import (
    _Module,
    _ast_bound,
    _import_bound_names,
    _imported_name,
    _in_import,
    _module_of,
    _nfkc,
    _not_a_mention,
    _parse,
    _python_parse,
    _read_python,
    _text,
    _walk,
)
from flag_cleanup.cross_file_python_imports import (
    _compiled_init,
    _loads_other_code,
    _module_dir,
    _resolve,
    xk_checkout_root,
)


#: Builtins, members and modules that reach or rewrite a module's namespace,
#: or load one, without spelling the name they bind.
_OPAQUE = xk.mention_pattern(
    [
        "globals", "vars", "locals", "exec", "eval", "setattr", "delattr", "__dict__", "__import__",
        "import_module", "__setattr__", "__delattr__", "__builtins__", "builtins", "__class__",
        "operator", "importlib", "pkgutil", "resolve_name",
        # a member taken by a computed name, a function's or a frame's
        # globals, and the interpreter's object graph
        "getattr", "f_globals", "f_locals", "get_referents", "get_referrers", "ctypes",
        "get_objects", "get_field",
        # the import system's hooks, which decide what an import loads
        "meta_path", "path_hooks", "path_importer_cache", "_frozen_importlib",
        "_frozen_importlib_external",
    ]
)


#: The only module-level dunders a package, or a star's source, may bind:
#: they describe it. Every other one (``__path__``, ``__getattr__``,
#: ``__spec__``, ...) can change what an import of the package finds.
_DESCRIPTIVE = frozenset({"__all__", "__version__", "__author__", "__doc__"})


#: Metadata a package (or a star's source) may also bind, but only by
#: assigning a literal to it (:func:`_literal_value`): an import or a computed
#: value could be anything, and none of these changes what an import finds.
_METADATA = frozenset(
    {
        "__date__", "__license__", "__licence__", "__copyright__", "__email__", "__maintainer__",
        "__credits__", "__status__", "__url__", "__title__", "__description__", "__summary__",
        "__uri__", "__author_email__", "__maintainer_email__", "__build__", "__docformat__",
        "__revision__", "__contact__",
    }
)


#: The only dunders a package or a star's source may spell at all, anywhere:
#: the ones it may bind, names that only describe a module, and
#: ``__slots__``, which a class body binds for its instances alone (a package
#: binding it itself is still refused, by :func:`_bindable`). Any other
#: (``__globals__``, ``__getattribute__``, ``__path__``, ...) can reach or
#: redirect a namespace.
_PLAIN_DUNDERS = _DESCRIPTIVE | _METADATA | {
    "__name__", "__file__", "__future__", "__init__", "__main__", "__slots__",
}


_WORD = re.compile(r"\w+")


def _spells_other_dunder(text: str) -> bool:
    return any(
        len(w) > 4 and w.startswith("__") and w.endswith("__") and w not in _PLAIN_DUNDERS
        for w in _WORD.findall(text)
    )


_STATEMENT_START = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING)


_STRING_TOKENS = (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING))


def _spells_opaque(text: str, importlib_uses: int = 0) -> bool:
    """Whether Python's tokens of ``text`` name an opaque gateway (:data:`_OPAQUE`)
    or a dunder off :data:`_PLAIN_DUNDERS`, NFKC-normalized as Python reads names.

    Names count, and so do strings, which a computed member or a format field
    turns into a name (``getattr(m, "__di" "ct__")``,
    ``Formatter().get_field("0.__dict__", ...)``). A comment does not, and
    neither does a statement that is a single plain string -- a docstring --
    which runs nothing. ``importlib`` as a name is fine only when it is
    spelled exactly ``importlib_uses`` times: the uses :func:`_allowed_calls`
    found in a version lookup, each of which spells it once.
    """
    return _opaque_line(text, importlib_uses) is not None


def _opaque_line(text: str, importlib_uses: int = 0) -> int | None:
    """The first line on which :func:`_spells_opaque` finds what it looks
    for, or None. Text the tokenizer cannot read is opaque at line 1."""
    return next((line for line, _column in _opaque_spots(text, importlib_uses)), None)


def _opaque_spots(text: str, importlib_uses: int = 0) -> Iterator[tuple[int, int | None]]:
    """Every token :func:`_spells_opaque` finds what it looks for in, as
    ``(line, character column)``. Text the tokenizer cannot read gives
    ``(1, None)`` and nothing else, and an error token ends the scan."""
    try:
        tokens = [
            t
            for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type not in (tokenize.COMMENT, tokenize.NL)
        ]
    except (tokenize.TokenError, SyntaxError):
        yield 1, None
        return
    names = [_nfkc(t.string) if t.type == tokenize.NAME else None for t in tokens]
    exempt = importlib_uses > 0 and names.count("importlib") == importlib_uses
    for index, token in enumerate(tokens):
        if token.type == tokenize.ERRORTOKEN:
            yield token.start[0], None
            return
        if token.type == tokenize.NAME:
            if exempt and names[index] == "importlib":
                continue
            piece = names[index]
        elif token.type in _STRING_TOKENS:
            before = tokens[index - 1].type if index else tokenize.NEWLINE
            after = tokens[index + 1].type if index + 1 < len(tokens) else tokenize.ENDMARKER
            prefix = token.string[: len(token.string) - len(token.string.lstrip("rRbBuUfFtT"))]
            if (
                token.type == tokenize.STRING
                and before in _STATEMENT_START
                and after in (tokenize.NEWLINE, tokenize.ENDMARKER)
                and not set(prefix) & set("fFtT")
            ):
                continue  # a docstring, or another string standing alone as a statement
            piece = _nfkc(token.string)
        else:
            continue
        if _OPAQUE.search(piece) or _spells_other_dunder(piece):
            yield token.start


def _sys_modules(root: Node) -> list[Node]:
    """Every place the file reaches ``sys.modules``, which maps a name to its module.

    ``sys.modules``, ``s.modules`` under ``import sys as s``, ``os.sys.modules``
    and ``from sys import modules`` reach it. So does ``sys`` handed on as a
    value (``getattr(sys, name)``), since nothing then says which member is
    taken.
    """
    names = {"sys"}
    found: list[Node] = []
    for node in _walk(root):
        if node.type == "import_statement":
            for entry, local in _import_bound_names(node):
                if entry.type == "aliased_import" and _imported_name(entry) == "sys":
                    names.add(local)
        elif node.type == "import_from_statement":
            source = node.child_by_field_name("module_name")
            for entry, local in _import_bound_names(node):
                if _imported_name(entry) == "sys":
                    names.add(local)  # `from os import sys as s`
                elif source is not None and _text(source) == "sys" and _imported_name(entry) == "modules":
                    found.append(node)
    for node in _walk(root):
        if node.type != "identifier" or _text(node) not in names or _in_import(node) is not None:
            continue
        parent = node.parent
        if parent is not None and parent.type == "keyword_argument" and _not_a_mention(node):
            continue
        # `x.sys` names the module through another one; `sys` names it itself.
        value = parent if parent is not None and parent.type == "attribute" and _not_a_mention(node) else node
        if value is not node and _text(node) != "sys":
            continue  # `obj.s`: an attribute that only shares an alias's spelling
        outer = value.parent
        if outer is not None and outer.type == "attribute" and outer.child_by_field_name("object").id == value.id:
            member = outer.child_by_field_name("attribute")
            if member is None or _text(member) != "modules":
                continue  # `sys.argv`, `sys.exit(...)`
        found.append(value)
    return found


def _bindable(name: str, sub: str, star_source: bool = False) -> bool:
    """Whether a package (or a star's source) binding ``name`` leaves its submodule ``sub`` alone.

    In a star's source ``__all__`` decides what the star copies, so it may be
    bound only by the one literal assignment :func:`_literal_all` accepts.
    """
    if name == sub or (star_source and name == "__all__"):
        return False
    return not (name.startswith("__") and name.endswith("__")) or name in _DESCRIPTIVE


def _literal_all(assignment: Node, sub: str) -> bool:
    """Whether ``__all__ = [...]`` in a star's source names only plain strings,
    none of them a dunder or ``sub``: the star binds exactly those names. Its
    annotation is judged with every other value, by :func:`_transparent_module`."""
    right = assignment.child_by_field_name("right")
    if right is None or right.type not in ("list", "tuple"):
        return False
    for item in right.named_children:
        if item.type == "comment":
            continue
        if item.type != "string" or any(
            c.type not in ("string_start", "string_content", "string_end") for c in item.children
        ):
            return False  # an escape, an f-string's `{...}`, or anything but a string
        prefix = _text(item.children[0]).lower()
        if any(c not in "ru" for c in prefix.rstrip("'\"")):
            return False  # a bytes or format literal
        try:
            name = ast.literal_eval(item.text.decode("utf-8"))
        except (ValueError, SyntaxError):
            return False
        if not isinstance(name, str) or sub in (name, _nfkc(name)):
            return False
        if name.startswith("__") and name.endswith("__"):
            return False
    return True


def _transparent_statement(
    statement: Node, sub: str, path: str, repo_dir: str, star_source: bool, below: tuple[str, ...] = ()
) -> bool:
    """Whether one top-level statement of a package ``__init__.py``, or of a
    star's source when ``star_source``, has a shape on the whitelist: which
    statements, and which names they bind. The VALUES they hold (an
    assignment's, an annotation, a docstring's ``{...}``) are judged once, on
    Python's own parse, by :func:`_transparent_module`."""
    kind = statement.type
    if kind == "comment":
        return True
    if kind == "expression_statement":
        children = statement.named_children
        if len(children) != 1:
            return False
        expression = children[0]
        if expression.type in ("string", "concatenated_string"):
            return True  # a docstring
        if expression.type != "assignment":
            return False
        left = expression.child_by_field_name("left")
        if star_source and left is not None and left.type == "identifier" and _text(left) == "__all__":
            return _literal_all(expression, sub)
        current = expression
        while True:
            left = current.child_by_field_name("left")
            if left is None or left.type != "identifier":
                return False
            name = _text(left)
            if not (name in _METADATA and name != sub) and not _bindable(name, sub, star_source):
                return False
            right = current.child_by_field_name("right")
            if right is None or right.type != "assignment":
                return True
            current = right
    if kind in ("import_statement", "future_import_statement"):
        return all(_bindable(local, sub, star_source) for _entry, local in _import_bound_names(statement))
    if kind == "import_from_statement":
        if any(c.type == "wildcard_import" for c in statement.named_children):
            # One level only: a star in the star's source is opaque.
            return not star_source and _star_source_transparent(statement, sub, path, repo_dir, below)
        return all(_bindable(local, sub, star_source) for _entry, local in _import_bound_names(statement))
    if kind == "function_definition":
        name = statement.child_by_field_name("name")
        if name is None or not _bindable(_text(name), sub, star_source):
            return False
        # Default values and annotations run when the `def` does. The body is
        # held to :func:`_inert_def` against Python's own parse.
        signature = [statement.child_by_field_name(f) for f in ("parameters", "return_type")]
        return not any(
            n.type in ("call", "await", "named_expression") for part in signature if part is not None for n in _walk(part)
        )
    if kind == "class_definition":
        # Its body runs as the statement does: :func:`_inert_class` judges
        # it against Python's own parse.
        name = statement.child_by_field_name("name")
        if name is None or not _bindable(_text(name), sub, star_source):
            return False
        bases = statement.child_by_field_name("superclasses")
        return bases is None or all(_dotted(base) for base in bases.named_children if base.type != "comment")
    return False


def _dotted(node: Node) -> bool:
    """Whether ``node`` is a name or a dotted name, with nothing called."""
    while node.type == "attribute":
        member = node.child_by_field_name("attribute")
        if member is None or member.type != "identifier":
            return False
        node = node.child_by_field_name("object")
    return node.type == "identifier"


#: What a value may hold in Python's parse: literals, names, containers,
#: operators and f-strings of them.
_CONSTANT_AST = (
    ast.Constant, ast.Name, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.UnaryOp, ast.BinOp,
    ast.BoolOp, ast.Compare, ast.JoinedStr, ast.FormattedValue, ast.expr_context, ast.operator,
    ast.unaryop, ast.boolop, ast.cmpop,
)


_ANNOTATION_AST = (*_CONSTANT_AST, ast.Attribute, ast.Subscript)


def _ast_only(node: ast.AST | None, kinds: tuple[type, ...]) -> bool:
    return node is None or all(
        isinstance(n, kinds) and not (isinstance(n, ast.Dict) and None in n.keys) for n in ast.walk(node)
    )


def _ast_signature(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a def's default values and annotations, which run when the
    ``def`` does, call nothing and bind nothing."""
    parts: list[ast.AST] = [function.args] + ([function.returns] if function.returns else [])
    return not any(
        isinstance(n, (ast.Call, ast.Await, ast.NamedExpr, ast.Yield, ast.YieldFrom))
        for part in parts
        for n in ast.walk(part)
    )


def _inert_class(cls: ast.ClassDef, text: str, sub: str, below: tuple[str, ...] = ()) -> bool:
    """Whether defining a class runs nothing that can bind attribute ``sub``.

    A class body runs when its statement does, so it is held to what the
    file's top level may hold: undecorated, its bases names or dotted names
    and no keyword (a ``metaclass=`` of the file's own would run its
    ``__init__`` here), and each statement in its body a docstring, ``pass``,
    an assignment of a constant expression to a name, an undecorated ``def``
    whose signature calls nothing, or such a class again. The whole class is
    also held to :func:`_inert_def`'s rules, so no method stores ``sub`` or
    anything ``below`` it, or spells ``sub``.
    """
    if cls.decorator_list or cls.keywords or getattr(cls, "type_params", None):
        return False
    if not all(_ast_only(base, (ast.Name, ast.Attribute, ast.expr_context)) for base in cls.bases):
        return False
    for statement in cls.body:
        if isinstance(statement, ast.Pass) or (
            isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
        ):
            continue
        if isinstance(statement, ast.Assign) and all(isinstance(t, ast.Name) for t in statement.targets):
            if _ast_only(statement.value, _CONSTANT_AST):
                continue
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if _ast_only(statement.annotation, _ANNOTATION_AST) and _ast_only(statement.value, _CONSTANT_AST):
                continue
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not statement.decorator_list and _ast_signature(statement):
                continue
        elif isinstance(statement, ast.ClassDef) and _inert_class(statement, text, sub, below):
            continue
        return False
    return _inert_def(cls, text, sub, below)


def _inert_def(
    function: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, text: str, sub: str, below: tuple[str, ...] = ()
) -> bool:
    """Whether a function (or a class), whenever it runs, leaves attribute ``sub`` alone.

    Its body may assign or delete no attribute named ``sub``, or named as any
    package or module ``below`` it on the way to the declaring file, in any
    target position Python's parse calls a store (``m.sub = ...``, ``m.sub +=
    ...``, ``del m.sub``, ``for m.sub in ...``, ``with ... as m.sub``), and
    may never spell ``sub`` as a name, an attribute, a keyword or an import
    path. Any other attribute (``self.url = url``) is its own business.
    """
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and _nfkc(node.attr) in (sub, *below)
        ):
            return False
    segment = ast.get_source_segment(text, function)
    if segment is None:
        return False
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(segment).readline))
    except (tokenize.TokenError, SyntaxError):
        return False
    return not any(t.type == tokenize.NAME and _nfkc(t.string) == sub for t in tokens)


def _transparent_module(
    path: Path,
    sub: str,
    repo_dir: str,
    star_source: bool,
    below: tuple[str, ...] = (),
    above: bool = False,
) -> bool:
    """Whether running the file at ``path`` surely binds nothing named ``sub``.

    The whitelist :func:`_transparent_init` describes, read as Python decodes
    the file; one Python and tree-sitter parse differently is opaque. In a
    star's source (``star_source``) a star is opaque and ``__all__`` must be a
    literal list naming neither ``sub`` nor a dunder. ``below`` names the
    packages and module further down the way, whose attributes no function
    or class may store either. For a package further up than the one holding
    the module (``above``), the statements :func:`_allowed_calls` accepts are
    fine as well.
    """
    text = _read_python(path)
    if text is None:
        return False
    root = _parse(text)
    if root.has_error or _sys_modules(root):
        return False
    tree = _python_parse(text, root)
    if tree is None:
        return False
    accepted, importlib_uses = _allowed_calls(tree, str(path), repo_dir, sub) if above else (set(), 0)
    if _spells_opaque(text, importlib_uses):
        return False
    for node in _walk(root):
        if node.type == "global_statement" and any(
            not _bindable(_text(n), sub, star_source) for n in node.named_children if n.type == "identifier"
        ):
            return False
    index = 0
    for statement in root.named_children:
        if statement.type not in ("comment", "line_continuation"):
            index += 1
            if index - 1 in accepted:
                continue
        if not _transparent_statement(statement, sub, str(path), repo_dir, star_source, below):
            return False
    for position, node in enumerate(tree.body):
        if position in accepted:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _inert_def(node, text, sub, below):
            return False
        if isinstance(node, ast.ClassDef) and not _inert_class(node, text, sub, below):
            return False
        # Every value a statement holds, judged once, here: what Python runs.
        # An f-string docstring's `{...}` runs code too.
        if isinstance(node, ast.Expr) and not _ast_only(node.value, _CONSTANT_AST):
            return False
        if isinstance(node, ast.Assign) and not _ast_only(node.value, _CONSTANT_AST):
            return False
        if isinstance(node, ast.AnnAssign) and not (
            _ast_only(node.annotation, _ANNOTATION_AST) and _ast_only(node.value, _CONSTANT_AST)
        ):
            return False
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in _METADATA for t in targets) and not _literal_value(
                node.value
            ):
                return False
    return True


def _literal_value(node: ast.AST | None) -> bool:
    """Whether ``node`` is a literal Python evaluates to itself: a string,
    number, ``True``/``False``/``None``, or a container of them."""
    if node is None:
        return False
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _star_source_transparent(
    statement: Node, sub: str, init_path: str, repo_dir: str, below: tuple[str, ...] = ()
) -> bool:
    """Whether ``from M import *`` in a package ``__init__.py`` cannot bind ``sub``.

    M must be exactly one source file of the checkout, with nothing compiled
    beside it, held to the same whitelist as the package (one level: a star
    inside it is opaque). When M is a package, it may have no ``sub``
    submodule: a loaded submodule is a public attribute, which a star without
    ``__all__`` copies.
    """
    source = statement.child_by_field_name("module_name")
    if source is None:
        return False
    files = _resolve(repo_dir, init_path, _module_of(source))
    if len(files) != 1:
        return False
    star = Path(files[0])
    if _loads_other_code(star, repo_dir):
        return False
    if star.name == "__init__.py" and ((star.parent / sub).is_dir() or any(star.parent.glob(f"{sub}.*"))):
        return False
    return _transparent_module(star, sub, repo_dir, star_source=True, below=below)


def _transparent_init(init_path: str, sub: str, repo_dir: str, below: tuple[str, ...] = ()) -> bool:
    """Whether a package ``__init__.py`` surely leaves attribute ``sub`` to its submodule.

    ``from pkg import sub`` and the attribute walk of ``import pkg.sub`` ask the
    package for ``sub`` first and get the submodule only when there is none. A
    whitelist, not a list of hazards. Every top-level statement must be one of:

    * a docstring;
    * an import, a ``__future__`` import or a non-star from-import binding
      neither ``sub`` nor a dunder other than ``__all__``, ``__version__``,
      ``__author__``, ``__doc__``;
    * an assignment of a literal to a metadata dunder (:data:`_METADATA`:
      ``__date__``, ``__license__``, ``__title__``, ...);
    * an assignment (or annotated assignment) to such a name of a constant
      expression -- literals, names, containers, operators and f-strings of
      them, calling, binding and subscripting nothing -- under an annotation
      that calls nothing (``dict[str, int]``, ``typing.Final``);
    * an undecorated ``def`` of such a name whose signature calls nothing and
      whose body stores no attribute ``sub`` (nor one named ``below``) and
      never spells ``sub``;
    * an undecorated class of such a name, held to :func:`_inert_class`;
    * a star from one source file of the checkout held to these same rules,
      with no star of its own, and an ``__all__``, if any, of plain strings
      naming neither ``sub`` nor a dunder.

    And nothing in the file (or the star's source) may name -- in code or in
    a string, though not in a comment or a docstring (:func:`_spells_opaque`)
    -- a way to reach a namespace, the builtins, a loader or the import
    system's hooks, a frame, the object graph, or a dunder beyond those
    and ``__name__``, ``__file__``, ``__future__``, ``__init__``,
    ``__main__``; nor reach ``sys.modules``, or rebind ``sub`` through
    ``global``. The file is read as
    Python decodes it. One that does not parse, or that Python parses
    differently, is opaque, and so is a package with a compiled ``__init__``
    or bytecode that runs without checking the source; a missing one is a
    namespace package, transparent.
    """
    path = Path(init_path)
    if _compiled_init(path, repo_dir):
        return False
    if not path.is_file():
        return True
    return _transparent_module(path, sub, repo_dir, star_source=False, below=below)


#: What a version lookup may import from ``importlib.metadata`` (or the
#: ``importlib_metadata`` backport): not ``entry_points``, which loads
#: modules by name, nor the modules the package re-exports.
_METADATA_NAMES = frozenset({"version", "metadata", "PackageNotFoundError"})


def _import_kinds(tree: ast.Module) -> tuple[dict[str, set[str]], bool]:
    """How each name in the file is bound, at any depth: ``import logging``,
    ``from logging import getLogger``, ``import x as`` (an alias), or
    ``other`` for any binding that is not an import; and whether there is a
    star import, which can bind anything."""
    kinds: dict[str, set[str]] = {}
    star = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = _nfkc(alias.asname or alias.name.split(".")[0])
                kind = f"import {alias.name}" + (" as" if alias.asname else "")
                kinds.setdefault(name, set()).add(kind)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star = True
                    continue
                name = _nfkc(alias.asname or alias.name)
                kind = f"from {'.' * node.level}{node.module or ''} import {alias.name}"
                kinds.setdefault(name, set()).add(kind + (" as" if name != alias.name else ""))
        else:
            for name in _ast_bound(node):
                kinds.setdefault(_nfkc(name), set()).add("other")
    return kinds, star


def _allowed_calls(tree: ast.Module, init_path: str, repo_dir: str, sub: str) -> tuple[set[int], int]:
    """The top-level statements of a package further up that make one of the
    few calls it may make, by position in ``tree.body``, and how many times
    they spell ``importlib``.

    Each statement must be, as a whole, one of:

    * ``logging.getLogger(__name__ or "literal").addHandler(logging.NullHandler())``,
      or the same with ``getLogger``/``NullHandler`` from ``logging``;
    * ``__version__ = version("literal")``, ``metadata.version(...)`` or
      ``importlib.metadata.version(...)``, from ``importlib.metadata`` or the
      ``importlib_metadata`` backport; or that, with the imports it needs, in
      the one ``try`` this allows, whose single ``except PackageNotFoundError``
      sets ``__version__`` to a literal;
    * ``warnings.simplefilter(...)`` or ``warnings.filterwarnings(...)`` with
      literal arguments only;
    * ``from . import sub``, which imports the real next package.

    Every name a call uses must be bound in the file only by the import it
    names (not rebound anywhere, nor by a star), and ``logging``,
    ``warnings`` and ``importlib_metadata`` must not be a module of the
    checkout, which would run in place of the library.
    """
    kinds, star = _import_kinds(tree)
    if star:
        return set(), 0

    def only(name: str, allowed: set[str]) -> bool:
        return name in kinds and kinds[name] <= allowed

    def library(module: str) -> bool:
        return not _resolve(repo_dir, init_path, _Module(0, (module,)))

    def literal(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None)))

    def string(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    def name_is(node: ast.AST, name: str) -> bool:
        return isinstance(node, ast.Name) and node.id == name

    def backport_ok(name: str) -> bool:
        """The backport, when ``name`` comes from it, is the library, not the checkout's."""
        return all("importlib_metadata" not in k for k in kinds[name]) or library("importlib_metadata")

    metadata_module = {"from importlib import metadata", "import importlib_metadata", "import importlib_metadata as"}

    def metadata_member(node: ast.AST, member: str) -> int | None:
        """``importlib`` spellings of a version-lookup name, or None if it is not one."""
        if name_is(node, member) and only(
            member, {f"from importlib.metadata import {member}", f"from importlib_metadata import {member}"}
        ):
            return 0 if backport_ok(member) else None
        if not isinstance(node, ast.Attribute) or node.attr != member:
            return None
        owner = node.value
        if isinstance(owner, ast.Name) and only(owner.id, metadata_module):
            return 0 if backport_ok(owner.id) else None
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == "metadata"
            and name_is(owner.value, "importlib")
            and only("importlib", {"import importlib.metadata"})
        ):
            return 1
        return None

    def version_import(node: ast.AST) -> int | None:
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in ("importlib.metadata", "importlib_metadata") and all(
                a.name in _METADATA_NAMES for a in node.names
            ):
                return 1 if node.module == "importlib.metadata" else 0
            if node.module == "importlib" and all(a.name == "metadata" for a in node.names):
                return 1
        if isinstance(node, ast.Import) and all(
            a.name in ("importlib.metadata", "importlib_metadata") for a in node.names
        ):
            return sum(a.name == "importlib.metadata" for a in node.names)
        return None

    def version_assignment(node: ast.AST) -> int | None:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and name_is(node.targets[0], "__version__")):
            return None
        call = node.value
        if not (isinstance(call, ast.Call) and len(call.args) == 1 and not call.keywords and string(call.args[0])):
            return None
        return metadata_member(call.func, "version")

    def logger(node: ast.AST) -> bool:
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            return False
        add = node.value
        if add.keywords or len(add.args) != 1 or not isinstance(add.func, ast.Attribute) or add.func.attr != "addHandler":
            return False
        get, handler = add.func.value, add.args[0]
        if not (isinstance(get, ast.Call) and len(get.args) == 1 and not get.keywords):
            return False
        if not (string(get.args[0]) or (name_is(get.args[0], "__name__") and "__name__" not in kinds)):
            return False
        if not (isinstance(handler, ast.Call) and not handler.args and not handler.keywords):
            return False

        def from_logging(callee: ast.AST, member: str) -> bool:
            if isinstance(callee, ast.Attribute) and callee.attr == member and name_is(callee.value, "logging"):
                return only("logging", {"import logging"})
            return name_is(callee, member) and only(member, {f"from logging import {member}"})

        return from_logging(get.func, "getLogger") and from_logging(handler.func, "NullHandler") and library("logging")

    def warning_filter(node: ast.AST) -> bool:
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            return False
        call = node.value
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in ("simplefilter", "filterwarnings")
            and name_is(call.func.value, "warnings")
            and only("warnings", {"import warnings"})
            and library("warnings")
            and all(literal(a) for a in call.args)
            and all(k.arg is not None and literal(k.value) for k in call.keywords)
        )

    def version_try(node: ast.AST) -> int | None:
        if not isinstance(node, ast.Try) or node.orelse or node.finalbody or len(node.handlers) != 1:
            return None
        handler = node.handlers[0]
        error = metadata_member(handler.type, "PackageNotFoundError") if handler.type is not None else None
        fallback = handler.body[0] if len(handler.body) == 1 else None
        if handler.name is not None or error is None or not (
            isinstance(fallback, ast.Assign)
            and len(fallback.targets) == 1
            and name_is(fallback.targets[0], "__version__")
            and string(fallback.value)
        ):
            return None
        uses, assignments = error, 0
        for statement in node.body:
            spelled = version_import(statement)
            if spelled is None:
                spelled = version_assignment(statement)
                assignments += 1
            if spelled is None:
                return None
            uses += spelled
        return uses if assignments == 1 else None

    def next_package(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and not node.module
            and any(a.name == sub and a.asname in (None, sub) for a in node.names)
            and all(
                (a.name == sub and a.asname in (None, sub)) or _bindable(_nfkc(a.asname or a.name), sub)
                for a in node.names
            )
        )

    accepted: set[int] = set()
    uses = 0
    for position, statement in enumerate(tree.body):
        if logger(statement) or warning_filter(statement) or next_package(statement):
            accepted.add(position)
            continue
        spelled = version_import(statement)
        if spelled is not None:
            uses += spelled  # an import the whitelist judges as usual, counted here
            continue
        for match in (version_assignment, version_try):
            spelled = match(statement)
            if spelled is not None:
                accepted.add(position)
                uses += spelled
                break
    return accepted, uses


def _transparent_above(init_path: str, chain: tuple[str, ...], repo_dir: str) -> bool:
    """Whether a package ``__init__.py`` further up than the one that holds a
    module leaves every package and module below it on the way alone.

    ``chain`` is that way down: ``("b", "keys")`` for ``a/__init__.py`` above
    ``a/b/keys.py``. Such a package is held to :func:`_transparent_init`'s
    whitelist for ``b``, and may also make the few calls
    :func:`_allowed_calls` names and import ``b`` itself with ``from .
    import b``. Nothing in it, at any depth, may bind or delete a name or
    store or delete an attribute spelling a segment of ``chain`` (NFKC),
    apart from that import; and it may hold no star import.
    """
    path = Path(init_path)
    if _compiled_init(path, repo_dir):
        return False
    if not path.is_file():
        return True
    if not _transparent_module(path, chain[0], repo_dir, star_source=False, below=chain[1:], above=True):
        return False
    text = _read_python(path)
    tree = _python_parse(text, _parse(text)) if text is not None else None
    if tree is None:
        return False
    segments = {_nfkc(s) for s in chain}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return False
                bound = _nfkc(alias.asname or alias.name.split(".")[0])
                plain_next = (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 1
                    and not node.module
                    and _nfkc(alias.name) == bound == _nfkc(chain[0])
                )
                if bound in segments and not plain_next:
                    return False
        elif any(_nfkc(name) in segments for name in _ast_bound(node)):
            return False
    return True


def _transparent_path(repo_dir: str, reader: str, module: _Module, member: str, start: int) -> bool:
    """Whether each package on ``module``'s path, from segment ``start`` on, hands
    on the next segment's submodule (``member`` being the file it resolves to).

    ``import a.b.keys`` walks ``a.b`` then ``.keys`` as attributes (start 1);
    ``from a.b import keys`` asks only ``a.b`` (start ``len(parts) - 1``).
    Every file the package can mean is checked, and the ``__init__.py`` beside
    ``member``, so a namespace package is seen as one.

    Every package above ``member`` in the checkout is checked too, whatever
    the import spells: each one's ``__init__.py`` runs before the next package
    loads and could set an attribute on it (``a/__init__.py`` doing
    ``b.keys = ...`` is what ``from a.b import keys``, or ``from . import
    keys`` inside ``a.b``, then finds).

    The package that holds ``member`` is held to :func:`_transparent_init`.
    One further up passes that, or :func:`_transparent_above` for the way
    down from it.
    """
    directory = _module_dir(member)
    count = len(module.parts)

    def transparent(init: str, way: tuple[str, ...]) -> bool:
        if _transparent_init(init, way[0], repo_dir, way[1:]):
            return True
        return len(way) > 1 and _transparent_above(init, way, repo_dir)

    for index in range(start, count):
        package = _Module(module.level, module.parts[:index])
        inits = set(_resolve(repo_dir, reader, package)) if (package.level or package.parts) else set()
        inits.add(str(directory.parents[count - 1 - index] / "__init__.py"))
        if not all(transparent(init, module.parts[index:]) for init in inits):
            return False
    checkout = Path(xk_checkout_root(repo_dir))
    for parent in directory.parents:
        if parent != checkout and checkout not in parent.parents:
            break
        if not transparent(str(parent / "__init__.py"), directory.relative_to(parent).parts):
            return False
    return True
