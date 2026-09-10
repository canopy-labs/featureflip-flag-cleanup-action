"""Gate 2 for the KEY-const path: the flag key hoisted to a named constant.

    private const string FlagKey = "old-checkout";           // C#
    const oldCheckout = "old-checkout"                       // Go
    private static final String FLAG_KEY = "old-checkout";   // Java

Hoisting the key is the conventional thing to do in every typed language this
tool supports, and every seed rule in ``rules/<base>.toml`` anchors a string
LITERAL first argument -- so an identifier there is not a weak match, it is
structurally unmatchable. Before a ``<base>_const.toml`` exists for a language,
such a file reports ``no-changes``: no pull request, and nothing anywhere
saying why. That silent no-op is the whole reason this module exists (#2671,
after #2525 closed it for C# alone).

**What this module is NOT.** It does not rewrite anything. The rewriting lives
in ``rules/<base>_const.toml``, which resolves the key at MATCH time through an
``enclosing_node`` filter and only ever edits the fold and the deletion. This
module is the pre-flight check that decides, per file, whether those rules may
run at all -- the runner partitions its candidate list on :func:`path_is_safe`
and invokes the engine twice.

**Why the safety argument is shared and the grammar is not.** Every language
withholds for the same TWO reasons, and both are parses-but-wrong, so Gate 1's
re-parse sees neither:

1. **Wrong branch.** The rules' ``enclosing_node`` filter finds the declaration
   from anywhere inside the enclosing scope, INCLUDING a narrower scope that
   shadows the name with a different key. That other flag's read then folds to
   THIS flag's value. Valid source, no ERROR nodes, and the diff looks right.
2. **Dangling reference.** The delete rule removes the declaration. Anything
   else still reading the name -- a log line, a map literal, an accessor call --
   then does not resolve. That is a name-resolution error one layer below
   anything a re-parse can see.

So the rule is the same everywhere: **the name must be bound exactly once, and
every remaining reference to it must be a read these rules themselves remove.**
That loop is :func:`path_is_safe` and is written once. What differs per language
is only which nodes declare a constant, which occurrences of a name BIND rather
than reference it, and which call shapes the const rules will actually remove --
so each language contributes a :class:`_Profile` of four small functions and
nothing else. Copying the loop per language is how the two halves would drift
apart, and a drift here is silent by construction.

Withholding is all-or-nothing per file on purpose: folding the read but keeping
the declaration would leave a string whose text makes every later run report
the flag as still referenced, forever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from tree_sitter import Node

from flag_cleanup import kotlin_sentinel, ts_syntax

__all__ = ["KeyConst", "path_is_safe", "supported_languages", "key_consts"]


@dataclass(frozen=True)
class KeyConst:
    """One resolvable hoisted flag key.

    ``name_nodes`` is the declaration's own naming occurrence(s), carried
    explicitly rather than re-derived while walking: a reference and a
    declaration are frequently the SAME node type (Go's ``identifier``, Ruby's
    ``constant``), so the only reliable discriminator is node identity, and it
    has to be captured where the declaration was recognised.

    Compared by ``.id``, never by ``is``: tree-sitter returns a FRESH ``Node``
    object from every accessor call, so an identity test silently never
    matches. That exact defect made the C# gate vacuously true for every file
    -- it presents as the gate not existing rather than as a failure. See
    ``test_the_declarator_name_is_found_by_id_not_by_identity``.
    """

    name: str
    declaration: Node
    name_nodes: tuple[Node, ...]


@dataclass(frozen=True)
class _Profile:
    """Everything language-specific about resolving a hoisted flag key.

    ``key_consts`` -- every constant in the file whose initialiser is exactly
    the flag key. It must accept only what the matching ``<base>_const.toml``
    accepts: looser here and the delete rule strands a live reference, tighter
    and a file is withheld that the rules could have cleaned.

    ``is_reference`` -- whether a node is an occurrence of the name at all.
    Most languages spell one as a bare ``identifier``, but PHP reaches a class
    constant through ``self::NAME`` and Ruby capitalises, so the node type is
    part of the profile rather than assumed.

    ``is_binding`` -- whether that occurrence INTRODUCES the name (a
    declarator, a parameter, a loop or ``catch`` variable, an import) rather
    than reading it. Used only to count bindings; the count must be one.

    ``is_removable_read`` -- whether this reference is a flag read the const
    rules will themselves delete. Mirrors those rules exactly, INCLUDING the
    clones generated for the run's ``accessors``: it is handed that set and
    must accept a wrapper's call exactly when a clone of the const rules would
    remove it. The two widen together or not at all -- a gate that counted an
    accessor read as removable without a rule to remove it would let the delete
    rule strand a live reference, and a rule without the gate is inert, since
    the whole file is withheld before it runs (#2730).

    Exactly one of ``is_binding`` (per node) or ``binding_ids`` (whole tree at
    once) must be supplied. The second exists for TypeScript, whose binding
    rules are already written -- and carefully reasoned about -- in
    ``ts_syntax.binding_nodes``; re-deriving them per node is precisely the
    duplication that leaked twice there.
    """

    key_consts: Callable[[Node, bytes, str], list[KeyConst]]
    is_reference: Callable[[Node, bytes, str], bool]
    is_removable_read: Callable[[Node, frozenset[str]], bool]
    is_binding: Callable[[Node], bool] | None = None
    binding_ids: Callable[[Node], set[int]] | None = None


# Populated at the bottom of this module, once every profile function exists.
_PROFILES: dict[str, _Profile] = {}


def supported_languages() -> frozenset[str]:
    """Languages with a key-const profile, i.e. with key-const rules shipped.

    A language absent here is trivially safe -- there are no rules to withhold.
    """
    return frozenset(_PROFILES)


def key_consts(root: Node, source_bytes: bytes, language: str, flag_key: str) -> list[KeyConst]:
    """Every hoisted key in the file, for tests that pin the grammar reading.

    Pinned directly because a vacuously-empty result makes the whole gate
    vacuously ``True``, and a gate that is vacuously true is indistinguishable
    from a gate that is working.
    """
    profile = _PROFILES.get(language)
    return [] if profile is None else profile.key_consts(root, source_bytes, flag_key)


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def path_is_safe(
    root: Node,
    source_bytes: bytes,
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] | frozenset[str] = (),
) -> bool:
    """Whether the key-const rules may run over this file.

    ``False`` withholds the whole ``<base>_const.toml`` for this file: no fold
    and no deletion, which costs one uncleaned declaration and is always safe.

    ``accessors`` is the run's configured wrapper names. It MUST be the same set
    the runner cloned the const rules for: those clones remove a wrapper's read
    of a hoisted key, so a reference to one is removable and no longer a reason
    to withhold. Passing it here and not to the clones (or the reverse) is the
    only way this gets dangerous -- see :class:`_Profile` (#2730).

    Files with no hoisted key at all are trivially safe -- the const rules match
    nothing in them. That is also the overwhelmingly common case, so this
    returns after one walk for almost every file the runner hands it.
    """
    accessor_names = frozenset(accessors)
    profile = _PROFILES.get(language)
    if profile is None:
        return True
    consts = profile.key_consts(root, source_bytes, flag_key)
    if not consts:
        return True

    declared_ids = {node.id for const in consts for node in const.name_nodes}
    # Computed once for the whole file rather than per candidate node.
    binding_ids = profile.binding_ids(root) if profile.binding_ids is not None else None

    def _binds(node: Node) -> bool:
        if binding_ids is not None:
            return node.id in binding_ids
        return profile.is_binding is not None and profile.is_binding(node)

    for const in consts:
        bindings = 0
        references: list[Node] = []
        for node in _walk(root):
            if not profile.is_reference(node, source_bytes, const.name):
                continue
            if node.id in declared_ids or _binds(node):
                bindings += 1
                continue
            references.append(node)
        # Exactly one: a second binder means a reference may resolve to
        # something other than the declaration the rules matched, and the
        # `enclosing_node` filter cannot tell the difference.
        if bindings != 1:
            return False
        if not all(
            profile.is_removable_read(node, accessor_names) for node in references
        ):
            return False
    return True


def _text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8")


def _is_named_identifier(node: Node, source_bytes: bytes, name: str, types: tuple[str, ...]) -> bool:
    return node.type in types and _text(node, source_bytes) == name


def _first_argument_of(identifier: Node, argument_type: str, list_type: str) -> Node | None:
    """The invocation whose FIRST argument is ``identifier``, or ``None``.

    ``argument_type`` is the wrapper a language puts around an argument (C#
    ``argument``, PHP ``argument``) or the identifier's own type where there is
    no wrapper (Go, Java, TypeScript pass expressions directly). Anchoring on
    the first argument mirrors the leading ``.`` anchor in every const query.
    """
    node = identifier
    if argument_type != identifier.type:
        node = identifier.parent
        if node is None or node.type != argument_type:
            return None
    argument_list = node.parent
    if argument_list is None or argument_list.type != list_type:
        return None
    if not argument_list.named_children or argument_list.named_children[0].id != node.id:
        return None
    return argument_list.parent


# ===========================================================================
# C#  --  `private const string FlagKey = "old-checkout";`   (#2525)
# ===========================================================================

#: The SHIPPED C# call names whose FIRST argument is a flag key that
#: ``csharp_const.toml`` will itself replace. The run's ``accessors`` are added
#: on top of it by :func:`_csharp_is_removable_read`, never baked in here: they
#: are per-run input, and the set they widen has to stay readable as "what the
#: shipped rules cover".
#:
#: They may only be added because ``csharp_const.toml`` is now CLONED per
#: accessor too (#2730). Before that the clones existed for the literal-key
#: seeds in ``csharp.toml`` alone, so an accessor call reading the const
#: survived the transform and counting it as removable would have deleted a
#: declaration that was still referenced. Widening this set without the clones
#: -- or emitting the clones without widening it -- is how this breaks in
#: either direction.
_CSHARP_FLAG_READ_CALLEES = frozenset({"BoolVariation", "GetBooleanValueAsync"})

#: Grammar fields that INTRODUCE a C# name.
_CSHARP_BINDING_PARENTS = (
    "variable_declarator",
    "parameter",
    "catch_declaration",
    "foreach_statement",
    "declaration_expression",
    "tuple_element",
    "from_clause",
    "let_clause",
    "join_clause",
    "query_continuation",
    "singleton_variable_designation",
)


def _csharp_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every ``const string`` whose value is ``flag_key``.

    Both `field_declaration` (class level) and `local_declaration_statement`
    (inside a method) qualify, matching the two shapes ``csharp_const.toml``
    queries. ``const`` is required rather than merely preferred: `static
    readonly` is not a compile-time constant and may be assigned in a static
    constructor, so its value cannot be resolved by reading the declaration.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type not in ("field_declaration", "local_declaration_statement"):
            continue
        modifiers = [
            _text(c, source_bytes) for c in node.children if c.type == "modifier"
        ]
        if "const" not in modifiers:
            continue
        declaration = next(
            (c for c in node.children if c.type == "variable_declaration"), None
        )
        if declaration is None:
            continue
        declarators = [
            c for c in declaration.named_children if c.type == "variable_declarator"
        ]
        # Exactly one, mirroring the `. ... .` anchors in the rules: a multi-
        # declarator statement is never matched there, so it must not be
        # treated as resolvable here either.
        if len(declarators) != 1:
            continue
        declarator = declarators[0]
        name = declarator.child_by_field_name("name")
        # `.id`, never `is`: tree-sitter hands back a FRESH Node object per
        # accessor call, so identity comparison never excludes the name node
        # and `value` silently becomes the name itself.
        value = next(
            (c for c in declarator.named_children if name is None or c.id != name.id),
            None,
        )
        if name is None or value is None or value.type != "string_literal":
            continue
        content = next(
            (c for c in value.named_children if c.type == "string_literal_content"), None
        )
        # `""` has no content child. Not a key we could match either way.
        if content is None or _text(content, source_bytes) != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(name, source_bytes),
                declaration=declarator,
                name_nodes=(name,),
            )
        )
    return found


def _csharp_lambda_parameter_name(lambda_node: Node) -> str | None:
    """The single parameter name of ``lambda_node``, or ``None``.

    Covers the three spellings a mock setup is written in: ``c => ...``
    (`implicit_parameter`), ``(c) => ...`` and ``(IClient c) => ...`` (both
    `parameter_list`). In the typed form the name is the LAST identifier under
    the `parameter` -- the first is the type -- which is also why the rules'
    query anchors its capture with a trailing ``.``.
    """
    parameters = lambda_node.child_by_field_name("parameters")
    if parameters is None:
        return None
    if parameters.type == "implicit_parameter":
        return parameters.text.decode("utf-8")
    if parameters.type != "parameter_list":
        return None
    params = [c for c in parameters.named_children if c.type == "parameter"]
    if len(params) != 1:
        return None
    identifiers = [c for c in params[0].named_children if c.type == "identifier"]
    return identifiers[-1].text.decode("utf-8") if identifiers else None


def _csharp_is_mock_expression_read(invocation: Node) -> bool:
    """Whether ``invocation`` is a call *described* to a mock, not performed.

    ``client.Setup(c => c.BoolVariation(FlagKey, ...))`` passes an
    ``Expression<Func<T, bool>>``: the text of the call IS the payload, so
    folding it to a literal breaks the setup without breaking the build. The
    discriminator is that the read's receiver is the lambda's own parameter --
    the shape of "describe a call on the mock" in any library that takes an
    expression tree, and not the shape of a genuine ``Func<>`` lambda, which
    reads the flag off a captured client.

    Kept deliberately in lockstep with the ``not_enclosing_node`` guard on the
    seed rules in ``csharp.toml`` / ``csharp_const.toml``: this half decides
    whether Gate 2 withholds the const path, that half decides whether the
    engine folds the read, and a disagreement between them is what would let
    the delete rule strand a live reference.
    """
    lambda_node = invocation.parent
    if lambda_node is None or lambda_node.type != "lambda_expression":
        return False
    body = lambda_node.child_by_field_name("body")
    if body is None or body.id != invocation.id:
        return False
    function = invocation.child_by_field_name("function")
    if function is None or function.type != "member_access_expression":
        return False
    receiver = function.child_by_field_name("expression")
    if receiver is None or receiver.type != "identifier":
        return False
    return receiver.text.decode("utf-8") == _csharp_lambda_parameter_name(lambda_node)


def _csharp_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """Whether ``identifier`` is a key argument ``csharp_const.toml`` removes.

    Mirrors those rules exactly -- first argument of a `BoolVariation` call, or
    of a `GetBooleanValueAsync` call that is itself awaited. Anything looser
    would let the delete rule strand a live reference; anything tighter would
    withhold a file the rules could have cleaned.

    A name in ``accessors`` is matched like `BoolVariation` -- plain, not
    awaited-only -- because that is the shape its clone takes.
    """
    invocation = _first_argument_of(identifier, "argument", "argument_list")
    if invocation is None or invocation.type != "invocation_expression":
        return False
    function = invocation.child_by_field_name("function")
    if function is None:
        return False
    if function.type == "member_access_expression":
        function = function.child_by_field_name("name")
    if function is None or function.type != "identifier":
        return False
    callee = function.text.decode("utf-8")
    if callee not in _CSHARP_FLAG_READ_CALLEES and callee not in accessors:
        return False
    if callee == "GetBooleanValueAsync":
        # An `await` cannot appear in an expression tree and an `async` lambda
        # cannot be converted to one, so requiring it already rules the mock
        # shape out here -- no second check needed.
        parent = invocation.parent
        return parent is not None and parent.type == "await_expression"
    return not _csharp_is_mock_expression_read(invocation)


def _csharp_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None or parent.type not in _CSHARP_BINDING_PARENTS:
        return False
    name = parent.child_by_field_name("name")
    return name is not None and name.id == node.id


# ===========================================================================
# Go  --  `const oldCheckout = "old-checkout"`
# ===========================================================================

_GO_FLAG_READ_CALLEES = frozenset({"BoolVariation"})

#: Parents whose ``name`` field introduces a Go name.
_GO_NAME_FIELD_PARENTS = (
    "const_spec",
    "var_spec",
    "parameter_declaration",
    "function_declaration",
    "method_declaration",
    "type_parameter_declaration",
)

#: Statements whose ``left`` expression list introduces (or re-assigns) names.
#: A plain ``assignment_statement`` is included deliberately: a Go ``const``
#: cannot be assigned to, so seeing the name on the left of one means the
#: declaration this gate matched is not the constant it appears to be. Counting
#: it as a binding pushes the count past one and refuses, which is the safe
#: direction.
_GO_LEFT_FIELD_PARENTS = ("short_var_declaration", "range_clause", "assignment_statement")


def _go_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every ``const`` spec whose value is ``flag_key``.

    Requires EXACTLY ONE ``const_spec`` in the declaration and exactly one name
    in that spec. BOTH halves are mirrored in ``go_const.toml``, by two
    different anchors, and it is worth naming which does which because getting
    that wrong is what #2700 was: ``. (const_spec ...) .`` bounds the number of
    SPECS, while ``value: (expression_list . (...) .)`` bounds the number of
    VALUES — and a Go const spec binds as many names as it has values, so the
    second is what makes "one name" true on the rule side.

    Before that second anchor existed the two halves disagreed on exactly that
    dimension: ``const a, b = "old-checkout", "other"`` is one spec, so the
    rules matched it, while this function skipped it for having two names. A
    file whose only key const is skipped here looks EMPTY to
    :func:`path_is_safe`, which reports it trivially safe -- so the rules ran
    with no gate at all and the delete rule took ``b`` with it.

    A grouped ``const ( a = "x"; b = "y" )`` is unmatchable on both sides for
    the first reason: the delete rule removes the whole ``const_declaration``
    and would take constants it was never asked about. Both fail closed to a
    silent no-op rather than to a broken build.

    ``var_declaration`` is a different node type in this grammar, so a mutable
    ``var`` key is structurally excluded rather than needing a guard.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type != "const_declaration":
            continue
        specs = [c for c in node.named_children if c.type == "const_spec"]
        if len(specs) != 1:
            continue
        spec = specs[0]
        names = [c for c in spec.named_children if c.type == "identifier"]
        value = spec.child_by_field_name("value")
        if len(names) != 1 or value is None or value.type != "expression_list":
            continue
        literals = [
            c for c in value.named_children if c.type == "interpreted_string_literal"
        ]
        if len(literals) != 1:
            continue
        # The gate's grammar exposes `interpreted_string_literal_content`; the
        # ENGINE's bundled Go grammar does not, which is why the rules match it
        # through a wildcard child instead. Read the content here, and fall
        # back to stripping the quotes so a grammar that drops the child node
        # cannot silently make this gate vacuous.
        content = next(
            (c for c in literals[0].named_children if "content" in c.type), None
        )
        text = (
            _text(content, source_bytes)
            if content is not None
            else _text(literals[0], source_bytes)[1:-1]
        )
        if text != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(names[0], source_bytes),
                declaration=node,
                name_nodes=(names[0],),
            )
        )
    return found


def _go_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _GO_NAME_FIELD_PARENTS:
        name = parent.child_by_field_name("name")
        if name is not None and name.id == node.id:
            return True
    if parent.type != "expression_list":
        return False
    grandparent = parent.parent
    if grandparent is None:
        return False
    # `switch x := v.(type)` binds through an expression list that is not the
    # statement's `left` field, so it is matched on the statement type alone.
    if grandparent.type == "type_switch_statement":
        return True
    if grandparent.type not in _GO_LEFT_FIELD_PARENTS:
        return False
    left = grandparent.child_by_field_name("left")
    return left is not None and left.id == parent.id


def _go_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """First argument of a ``BoolVariation`` call -- what ``go_const.toml`` removes.

    ``accessors`` are the run's own wrapper names, matched on the same terms:
    the const rules are cloned for each of them, so a wrapper's read is one
    this file removes. See :func:`path_is_safe`.
    """
    call = _first_argument_of(identifier, "identifier", "argument_list")
    if call is None or call.type != "call_expression":
        return False
    function = call.child_by_field_name("function")
    if function is None:
        return False
    if function.type == "selector_expression":
        function = function.child_by_field_name("field")
    if function is None or function.type not in ("identifier", "field_identifier"):
        return False
    callee = function.text.decode("utf-8")
    return callee in _GO_FLAG_READ_CALLEES or callee in accessors


# ===========================================================================
# Java  --  `private static final String FLAG_KEY = "old-checkout";`
# ===========================================================================

_JAVA_FLAG_READ_CALLEES = frozenset({"boolVariation"})

#: The SAME boundary pattern `java_const.toml` carries as a `#match?`. The two
#: must agree on what counts as a constant: Gate 2 reports "trivially safe" for
#: a file it finds no constant in, so a declaration the rules match and this
#: does not would be rewritten with no gate at all. Spelled with explicit
#: character classes rather than `\b` so it is copy-identical to the query,
#: where `\b` would be consumed by TOML as a backspace escape.
_JAVA_FINAL = re.compile(r"(^|[^A-Za-z])final([^A-Za-z]|$)")

#: Parents whose ``name`` field introduces a Java name.
_JAVA_NAME_FIELD_PARENTS = (
    "variable_declarator",
    "formal_parameter",
    "spread_parameter",
    "catch_formal_parameter",
    "enhanced_for_statement",
    "method_declaration",
    "class_declaration",
    "interface_declaration",
    "record_declaration",
    "type_parameter",
)


def _java_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every ``final`` string constant whose value is ``flag_key``.

    `final` is REQUIRED, mirroring C#'s insistence on `const`: a mutable field
    may be reassigned, so its declaration is not a resolvable value. An
    `interface_body` field is exempt because it is implicitly
    `public static final` -- there is nothing to assert.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type in ("field_declaration", "local_variable_declaration"):
            modifiers = next(
                (c for c in node.children if c.type == "modifiers"), None
            )
            if modifiers is None or not _JAVA_FINAL.search(_text(modifiers, source_bytes)):
                continue
        elif node.type != "constant_declaration":
            continue
        declarators = [
            c for c in node.named_children if c.type == "variable_declarator"
        ]
        # Exactly one, mirroring the `. ... .` anchors in the rules.
        if len(declarators) != 1:
            continue
        declarator = declarators[0]
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name is None or value is None or value.type != "string_literal":
            continue
        fragment = next(
            (c for c in value.named_children if c.type == "string_fragment"), None
        )
        # `""` has no fragment child. Not a key we could match either way.
        if fragment is None or _text(fragment, source_bytes) != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(name, source_bytes),
                declaration=node,
                name_nodes=(name,),
            )
        )
    return found


def _java_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _JAVA_NAME_FIELD_PARENTS:
        name = parent.child_by_field_name("name")
        if name is not None and name.id == node.id:
            return True
    # `x -> ...` binds through the `parameters` field; `(x, y) -> ...` through
    # an `inferred_parameters` list whose every identifier child is a binder.
    if parent.type == "lambda_expression":
        parameters = parent.child_by_field_name("parameters")
        if parameters is not None and parameters.id == node.id:
            return True
    if parent.type == "inferred_parameters":
        return True
    # A `final` constant cannot be assigned to, so the name appearing on the
    # left of an assignment means the declaration matched is not the constant
    # it appears to be. Counting it pushes the total past one and refuses,
    # which is the safe direction.
    if parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        if left is not None and left.id == node.id:
            return True
    return False


def _java_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """First argument of a ``boolVariation`` call -- what ``java_const.toml`` removes.

    ``accessors`` are the run's own wrapper names, matched on the same terms:
    the const rules are cloned for each of them, so a wrapper's read is one
    this file removes. See :func:`path_is_safe`.
    """
    call = _first_argument_of(identifier, "identifier", "argument_list")
    if call is None or call.type != "method_invocation":
        return False
    name = call.child_by_field_name("name")
    if name is None:
        return False
    callee = name.text.decode("utf-8")
    return callee in _JAVA_FLAG_READ_CALLEES or callee in accessors


# ===========================================================================
# TypeScript / TSX / JavaScript  --  `const OLD_CHECKOUT = "old-checkout"`
# ===========================================================================

#: Every callee whose FIRST argument `ts_const.toml`'s key-const rules resolve.
#: `getBooleanValue` is included but is only removable when AWAITED, which
#: :func:`_ts_is_removable_read` checks separately -- the bare call is a
#: `Promise<boolean>`, not a boolean, exactly as in ``ts.toml``.
_TS_FLAG_READ_CALLEES = frozenset({"boolVariation", "useFeatureFlag", "getBooleanValue"})
_TS_AWAITED_ONLY_CALLEES = frozenset({"getBooleanValue"})

#: A reference can also be an object shorthand (`{ OLD_CHECKOUT }`), which is a
#: real use of the value under a node type of its own. Counting it as a
#: reference makes it a non-removable read, so such a file is withheld -- the
#: safe direction, since the delete rule would otherwise strand it.
_TS_REFERENCE_TYPES = ("identifier", "shorthand_property_identifier")


def _ts_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every ``const`` binding whose initialiser is exactly ``flag_key``.

    ``const`` is required and is matched as an anonymous child, because
    `lexical_declaration` covers `let` too and a `let` may be reassigned. `var`
    is a different node type (`variable_declaration`) and is excluded
    structurally. The rules express the same requirement the same way.

    An `export const` is reached through its `export_statement`, but the
    declaration node found here is the `lexical_declaration` either way -- the
    delete rule is what has to care about the wrapper.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type != "lexical_declaration":
            continue
        if not any(c.type == "const" for c in node.children):
            continue
        declarators = [c for c in node.named_children if c.type == "variable_declarator"]
        # Exactly one, mirroring the `. ... .` anchors in the rules.
        if len(declarators) != 1:
            continue
        declarator = declarators[0]
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name is None or name.type != "identifier" or value is None:
            continue
        if value.type != "string":
            continue
        fragment = value.named_child(0)
        # `''` has no fragment child. Not a key we could match either way.
        if fragment is None or _text(fragment, source_bytes) != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(name, source_bytes),
                declaration=node,
                name_nodes=(name,),
            )
        )
    return found


def _ts_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """First argument of a flag read ``ts_const.toml`` removes.

    An OPTIONAL call is excluded for the reason ``ts.toml`` excludes it from
    the literal rules: `client.boolVariation?.(K, ...)` is `undefined` when the
    callee is nullish, so folding it is a behaviour change rather than a
    simplification -- and the const rules carry the same `not_contains` guard,
    so counting such a read as removable here would let the delete rule strand
    a reference the engine deliberately left standing.
    """
    call = _first_argument_of(identifier, "identifier", "arguments")
    if call is None or call.type != "call_expression":
        return False
    if any(c.type == "?." for c in call.children):
        return False
    function = call.child_by_field_name("function")
    if function is None:
        return False
    if function.type == "member_expression":
        if function.child_by_field_name("optional_chain") is not None:
            return False
        if any(c.type == "?." for c in function.children):
            return False
        function = function.child_by_field_name("property")
    if function is None or function.type not in ("identifier", "property_identifier"):
        return False
    callee = function.text.decode("utf-8")
    if callee not in _TS_FLAG_READ_CALLEES and callee not in accessors:
        return False
    if callee in _TS_AWAITED_ONLY_CALLEES:
        parent = call.parent
        return parent is not None and parent.type == "await_expression"
    return True


# ===========================================================================
# PHP  --  `const FLAG_KEY = 'old-checkout';`   (class-level or top-level)
# ===========================================================================

_PHP_FLAG_READ_CALLEES = frozenset({"boolVariation"})

#: PHP spells a string literal as two node types -- `'single'` is `string`,
#: `"double"` is `encapsed_string` -- and `php.toml` matches both. So does this.
_PHP_STRING_TYPES = ("string", "encapsed_string")

#: Nodes whose `(name)` child NAMES the thing rather than reading it. Broader
#: than strictly necessary on purpose: PHP reuses the bare `name` node for
#: class names, function names, method names and constant references alike, so
#: over-collecting here only pushes the binding count past one and refuses,
#: which is the safe direction.
_PHP_DECLARATION_PARENTS = (
    "const_element",
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
    "enum_case",
    "function_definition",
    "method_declaration",
    "namespace_use_clause",
    "namespace_aliasing_clause",
)


def _php_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every ``const`` element whose value is ``flag_key``.

    `property_declaration` -- including `public static $flagKey = '...'` -- is a
    different node type and is therefore unmatchable rather than refused, which
    is right: a static property is mutable and its declaration is not a
    resolvable value.

    ``define('FLAG_KEY', '...')`` is deliberately NOT resolved. It is a runtime
    function call, so it can sit behind a condition or in a loop, and deleting
    it is a different question from deleting a declaration. It stays a
    documented gap rather than a guess.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type != "const_declaration":
            continue
        elements = [c for c in node.named_children if c.type == "const_element"]
        # Exactly one, mirroring the TRAILING anchor in the rules -- naming
        # which anchor bounds what, because the pair bound different dimensions
        # until #2702 and a docstring that blurred them is a good part of why
        # that gap was easy to miss (#2700 records the same correction for Go).
        # A multi-element `const A = 'x', B = 'y';` is never matched there, so
        # the delete rule can never take a statement that also declares
        # something else.
        #
        # Nothing here filters on `visibility_modifier`, `final_modifier` or a
        # PHP 8.3 `type:`, and that is deliberate rather than incidental: a
        # modifier changes who may READ the constant, never whether its value is
        # resolvable. The rules' LEADING anchor used to disagree -- it required
        # the element to be the declaration's first named child, which every one
        # of those adds a node ahead of -- so `public const` was resolved here
        # and matched nowhere, the silent no-op of #2702.
        if len(elements) != 1:
            continue
        element = elements[0]
        name = next((c for c in element.named_children if c.type == "name"), None)
        value = next(
            (c for c in element.named_children if c.type in _PHP_STRING_TYPES), None
        )
        if name is None or value is None:
            continue
        content = next(
            (c for c in value.named_children if c.type == "string_content"), None
        )
        if content is None or _text(content, source_bytes) != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(name, source_bytes),
                declaration=node,
                name_nodes=(name,),
            )
        )
    return found


def _php_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None or parent.type not in _PHP_DECLARATION_PARENTS:
        return False
    named = parent.child_by_field_name("name")
    if named is not None:
        return named.id == node.id
    # `const_element` names its constant through a positional `(name)` child.
    first = next((c for c in parent.named_children if c.type == "name"), None)
    return first is not None and first.id == node.id


def _php_is_removable_read(name_node: Node, accessors: frozenset[str]) -> bool:
    """First argument of a ``boolVariation`` call -- what ``php_const.toml`` removes.

    Reached two ways, and both must be accepted or the gate disagrees with the
    rules: a top-level constant is referenced BARE (`FLAG_KEY`), a class
    constant through a `class_constant_access_expression` (`self::FLAG_KEY`,
    `Gate::FLAG_KEY`), which puts one extra node between the name and its
    argument.
    """
    candidate = name_node
    parent = name_node.parent
    if parent is not None and parent.type == "class_constant_access_expression":
        # The constant is the LAST child; the first is the scope (`self`, a
        # class name). A `self::FLAG_KEY` whose scope half happened to share the
        # name must not be read as the constant reference.
        last = parent.named_children[-1] if parent.named_children else None
        if last is None or last.id != name_node.id:
            return False
        candidate = parent
    call = _first_argument_of(candidate, "argument", "arguments")
    if call is None or call.type not in (
        "member_call_expression",
        "function_call_expression",
    ):
        return False
    callee = call.child_by_field_name("name") or call.child_by_field_name("function")
    if callee is None or callee.type != "name":
        return False
    name = callee.text.decode("utf-8")
    return name in _PHP_FLAG_READ_CALLEES or name in accessors


# ===========================================================================
# Ruby  --  `FLAG_KEY = 'old-checkout'`  (and the `.freeze` idiom)
# ===========================================================================
#
# ONE OF THE TWO WEAK-CONSTANT LANGUAGES. Ruby has no compile-time constant:
# `FLAG_KEY = 'x'` is an ordinary assignment that merely WARNS when reassigned,
# so unlike C#'s `const`, Go's `const` or Java's `final` there is no language
# guarantee behind the declaration this resolves. The whole safety argument is
# therefore Gate 2's, and specifically its bound-exactly-once half: a
# reassignment is itself an `assignment` with the constant on the left, so it
# is counted as a binding, pushes the total past one, and refuses the file.
# That is a genuinely weaker footing than the typed languages have, and it is
# named here rather than glossed over -- see also `_python_key_consts`.

_RUBY_FLAG_READ_CALLEES = frozenset({"bool_variation"})


def _ruby_string_content(node: Node, source_bytes: bytes) -> str | None:
    """The text of a string literal, seeing through a trailing ``.freeze``.

    `FLAG_KEY = 'old-checkout'.freeze` is idiomatic Ruby -- common enough that
    ignoring it would leave most real codebases unmatched -- and it parses as a
    `call` whose RECEIVER is the string. `rules/ruby_const.toml` matches the
    same two shapes.
    """
    if node.type == "call":
        method = node.child_by_field_name("method")
        receiver = node.child_by_field_name("receiver")
        if (
            method is None
            or receiver is None
            or _text(method, source_bytes) != "freeze"
        ):
            return None
        node = receiver
    if node.type != "string":
        return None
    content = next(
        (c for c in node.named_children if c.type == "string_content"), None
    )
    return None if content is None else _text(content, source_bytes)


def _ruby_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every constant assignment whose value is ``flag_key``.

    ``left: (constant)`` is what separates a constant from a local: Ruby spells
    a lowercase local as `identifier`, so `flag_key = '...'` is a different node
    type and is unmatchable rather than refused.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "constant":
            continue
        if _ruby_string_content(right, source_bytes) != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(left, source_bytes),
                declaration=node,
                name_nodes=(left,),
            )
        )
    return found


def _ruby_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    # A reassignment binds too, and MUST be counted: it is the only thing
    # standing in for the compile-time guarantee Ruby does not provide.
    if parent.type in ("assignment", "operator_assignment"):
        left = parent.child_by_field_name("left")
        if left is not None and left.id == node.id:
            return True
    if parent.type in ("class", "module"):
        name = parent.child_by_field_name("name")
        if name is not None and name.id == node.id:
            return True
    return False


def _ruby_is_removable_read(constant: Node, accessors: frozenset[str]) -> bool:
    """First argument of a ``bool_variation`` call -- what ``ruby_const.toml`` removes.

    ``accessors`` are the run's own wrapper names, matched on the same terms:
    the const rules are cloned for each of them, so a wrapper's read is one
    this file removes. See :func:`path_is_safe`.
    """
    call = _first_argument_of(constant, "constant", "argument_list")
    if call is None or call.type != "call":
        return False
    method = call.child_by_field_name("method")
    if method is None:
        return False
    callee = method.text.decode("utf-8")
    return callee in _RUBY_FLAG_READ_CALLEES or callee in accessors


# ===========================================================================
# Dart  --  `const flagKey = 'old-checkout';`
# ===========================================================================

_DART_FLAG_READ_CALLEES = frozenset({"boolVariation"})

#: A `string_literal` in the GATE's grammar (tree-sitter-dart 0.1.0) exposes no
#: content child at all, so its text arrives WITH quotes. This is the only
#: language here whose key has to be recovered by unwrapping rather than by
#: reading a node. Anchored at both ends and requiring MATCHING quotes, which
#: is what excludes the two shapes an unwrap would otherwise get wrong: a raw
#: string (`r'...'`, whose text starts with `r`) and adjacent-literal
#: concatenation (`'a' 'b'`, whose text contains an inner quote pair). Both are
#: then treated as "not a resolvable key", which fails closed.
_DART_QUOTED = re.compile(r"^(['\"])([^'\"]*)\1$", re.DOTALL)


def _dart_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every ``const``/``final`` declaration whose value is ``flag_key``.

    Immutability is STRUCTURAL in both Dart grammars: `const`/`final` parse
    into `static_final_declaration_list`, a mutable `var`/typed declaration
    into `initialized_identifier_list`. So there is no modifier to assert on --
    a reassignable key is unmatchable rather than refused, on the gate side
    exactly as in `dart_const.toml`.

    That agreement between the 0.1.0 gate grammar and the engine's 0.2.0 was
    CHECKED, not assumed. Dart is the language where the two differ most and
    where the seam has already produced corruption once (see CLAUDE.md), so
    anything else read from one side must be re-checked on the other.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type != "static_final_declaration":
            continue
        name = next((c for c in node.named_children if c.type == "identifier"), None)
        value = next(
            (c for c in node.named_children if c.type == "string_literal"), None
        )
        if name is None or value is None:
            continue
        quoted = _DART_QUOTED.match(_text(value, source_bytes))
        if quoted is None or quoted.group(2) != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(name, source_bytes),
                declaration=node,
                name_nodes=(name,),
            )
        )
    return found


def _dart_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "static_final_declaration":
        first = next((c for c in parent.named_children if c.type == "identifier"), None)
        return first is not None and first.id == node.id
    for holder in (
        "initialized_identifier",
        "initialized_variable_definition",
        "formal_parameter",
        "function_signature",
        "class_definition",
        "declared_identifier",
    ):
        if parent.type == holder:
            named = parent.child_by_field_name("name")
            if named is not None:
                if named.id == node.id:
                    return True
            else:
                first = next(
                    (c for c in parent.named_children if c.type == "identifier"), None
                )
                if first is not None and first.id == node.id:
                    return True
    # An assignment to the name means the declaration matched is not the
    # constant it appears to be. Counting it refuses, which is the safe way.
    if parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        if left is not None and left.id == node.id:
            return True
    return False


def _dart_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """First argument of a ``boolVariation`` call.

    The GATE's Dart grammar parses a method call into a flat
    `identifier (selector (argument_part (arguments (argument ...))))` chain
    rather than the `call_expression` the engine sees, so this walks that shape
    instead: the argument's `arguments` ancestor is reached through
    `argument_part`, and the callee is the identifier of the PRECEDING
    selector.
    """
    argument = identifier.parent
    if argument is None or argument.type != "argument":
        return False
    arguments = argument.parent
    if arguments is None or arguments.type != "arguments":
        return False
    if not arguments.named_children or arguments.named_children[0].id != argument.id:
        return False
    argument_part = arguments.parent
    if argument_part is None or argument_part.type != "argument_part":
        return False
    selector = argument_part.parent
    if selector is None or selector.type != "selector":
        return False
    parent = selector.parent
    if parent is None:
        return False
    # The callee is the selector immediately before this one -- either
    # `client.boolVariation(...)` (a preceding `unconditional_assignable_selector`)
    # or a bare `boolVariation(...)` (the identifier before the selector).
    previous = None
    for child in parent.named_children:
        if child.id == selector.id:
            break
        previous = child
    if previous is None:
        return False
    if previous.type == "selector":
        previous = next(
            (
                c
                for c in _walk(previous)
                if c.type == "identifier"
            ),
            None,
        )
    if previous is None or previous.type != "identifier":
        return False
    callee = _text_of(previous)
    return callee in _DART_FLAG_READ_CALLEES or callee in accessors


def _text_of(node: Node) -> str:
    return node.text.decode("utf-8")


# ===========================================================================
# Python  --  `OLD_CHECKOUT = "old-checkout"` at module level
# ===========================================================================
#
# THE OTHER WEAK-CONSTANT LANGUAGE. Python has no constant at all: the
# UPPER_CASE name is a convention with nothing behind it, so `OLD_CHECKOUT` can
# be rebound anywhere. As in Ruby, the entire safety argument is Gate 2's
# bound-exactly-once half — a rebinding is an `assignment` with the name on the
# left and is counted, which pushes the total past one and withholds the file.
#
# Restricted to MODULE level, which is both where the convention lives and what
# `python_const.toml` matches: a name assigned inside a function is an ordinary
# local, and a class-body constant is reached as an ATTRIBUTE (`Flags.OLD`), a
# different node shape that neither half handles.

#: Callees whose FIRST argument `python_const.toml` resolves. `variation` is
#: generic over the flag's type in this SDK, so its rules additionally require a
#: BOOLEAN LITERAL default as the only available type evidence -- see
#: :func:`_python_is_removable_read`, which mirrors that exactly. Widening
#: either half without the other is how a non-boolean flag would get folded to
#: a boolean.
_PYTHON_FLAG_READ_CALLEES = frozenset({"variation", "get_boolean_value"})

#: Fields and parents that introduce a Python name.
_PYTHON_NAME_FIELD_PARENTS = ("function_definition", "class_definition")
_PYTHON_BINDING_PARENTS = (
    "parameters",
    "default_parameter",
    "typed_parameter",
    "typed_default_parameter",
    "list_splat_pattern",
    "dictionary_splat_pattern",
    "as_pattern_target",
    "global_statement",
    "nonlocal_statement",
    "aliased_import",
)


def _python_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every MODULE-LEVEL assignment whose value is ``flag_key``."""
    found: list[KeyConst] = []
    for statement in root.named_children if root.type == "module" else ():
        if statement.type != "expression_statement":
            continue
        assignments = [
            c for c in statement.named_children if c.type == "assignment"
        ]
        if len(assignments) != 1:
            continue
        assignment = assignments[0]
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            continue
        if right.type != "string":
            continue
        content = next(
            (c for c in right.named_children if c.type == "string_content"), None
        )
        # An f-string carries `interpolation` children; a concatenation of
        # adjacent literals is a `concatenated_string`. Requiring exactly one
        # `string_content` and no interpolation keeps both out.
        if content is None or any(
            c.type == "interpolation" for c in right.named_children
        ):
            continue
        if _text(content, source_bytes) != flag_key:
            continue
        found.append(
            KeyConst(
                name=_text(left, source_bytes),
                declaration=statement,
                name_nodes=(left,),
            )
        )
    return found


def _python_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _PYTHON_NAME_FIELD_PARENTS:
        name = parent.child_by_field_name("name")
        if name is not None and name.id == node.id:
            return True
    if parent.type in _PYTHON_BINDING_PARENTS:
        return True
    # Any rebinding counts, and MUST: it is the only thing standing in for the
    # compile-time guarantee Python does not have. Covers `X = ...`,
    # `X: str = ...`, `X += ...`, `for X in ...`, `with ... as X`,
    # `except ... as X` (all `as_pattern`), and tuple targets.
    if parent.type in ("assignment", "augmented_assignment"):
        left = parent.child_by_field_name("left")
        if left is not None and (left.id == node.id or _contains(left, node)):
            return True
    if parent.type == "for_statement":
        left = parent.child_by_field_name("left")
        if left is not None and (left.id == node.id or _contains(left, node)):
            return True
    if parent.type in ("pattern_list", "tuple_pattern", "list_pattern"):
        return True
    if parent.type == "as_pattern":
        alias = parent.child_by_field_name("alias")
        if alias is not None and (alias.id == node.id or _contains(alias, node)):
            return True
        # `with open() as f` puts the target in an `as_pattern_target` child.
        return parent.named_children and parent.named_children[-1].id == node.id
    return False


def _contains(ancestor: Node, node: Node) -> bool:
    return any(child.id == node.id for child in _walk(ancestor))


def _python_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """First argument of a read ``python_const.toml`` removes.

    Mirrors those rules exactly, INCLUDING the boolean-literal requirement on
    `variation`: that SDK read is generic over the flag's type, so the literal
    default is the only evidence the flag is a boolean at all. `get_boolean_value`
    is boolean by name and needs none.
    """
    call = _first_argument_of(identifier, "identifier", "argument_list")
    if call is None or call.type != "call":
        return False
    function = call.child_by_field_name("function")
    if function is None:
        return False
    if function.type == "attribute":
        function = function.child_by_field_name("attribute")
    if function is None or function.type != "identifier":
        return False
    callee = function.text.decode("utf-8")
    if callee in accessors:
        # An accessor's clone comes from `rules/python_const_accessors.toml`,
        # which anchors the key and NOTHING else. Requiring the boolean literal
        # here would withhold every file whose wrapper hides the default -- the
        # shape the accessor feature exists for, and #2617 all over again.
        return True
    if callee not in _PYTHON_FLAG_READ_CALLEES:
        return False
    if callee == "get_boolean_value":
        return True
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return False
    positional = arguments.named_children
    # `variation(KEY, ctx, False)` -- third argument a boolean literal.
    if len(positional) >= 3 and positional[2].type in ("true", "false"):
        return True
    # `variation(KEY, context=ctx, default=False)` -- a `default=` keyword whose
    # value is a boolean literal, anywhere in the list.
    for argument in positional:
        if argument.type != "keyword_argument":
            continue
        name = argument.child_by_field_name("name")
        value = argument.child_by_field_name("value")
        if (
            name is not None
            and value is not None
            and name.text.decode("utf-8") == "default"
            and value.type in ("true", "false")
        ):
            return True
    return False


# ===========================================================================
# Kotlin  --  `const val FLAG_KEY = "old-checkout"`
# ===========================================================================
#
# THE ONE LANGUAGE WHERE THE RESOLUTION IS NOT A RULE. Kotlin's rules match a
# SENTINEL that `kotlin_sentinel` substitutes for the callee before the engine
# runs, so they never see the key at all -- which is why resolving a hoisted key
# lives in `kotlin_sentinel.const_val_keys` and only the DECLARATION DELETE is a
# rule (`kt_const.toml`). This profile is the dangling-reference half, and it is
# what makes Kotlin hold the same invariant as the other ten rather than a
# weaker one: the pre-pass already refuses a name bound more than once, but only
# this sees a reference that is not a flag read.
#
# `const` is REQUIRED, and not as a formality: in this grammar a plain `val` and
# a `var` are shaped IDENTICALLY (the keyword is an anonymous node), so without
# the modifier check a MUTABLE `var` would be resolved as a constant. `const
# val` is Kotlin's genuine compile-time constant.

#: The SHIPPED Kotlin callee. The run's ``accessors`` are added on top of it by
#: :func:`_kotlin_is_removable_read`, and Kotlin is the one language that needed
#: NOTHING else for that to be safe: its fold is the sentinel pre-pass, not a
#: rule, and `kotlin_sentinel._callee_spans` has marked
#: `{SDK_FUNCTION_NAME, *accessors}` since accessors existed. So until #2730
#: this gate was the ONLY thing withholding an accessor's read of a hoisted key
#: -- the pre-pass would have folded it, `resolve_consts` was just never true
#: for such a file. The other nine languages needed a rule clone as well.
_KOTLIN_FLAG_READ_CALLEES = frozenset({"boolVariation"})


_SWIFT_FLAG_READ_CALLEES = frozenset({"boolVariation"})

#: Parents under which a Swift `simple_identifier` INTRODUCES the name.
#:
#: `pattern` is the shared one and covers three constructs at once -- a
#: `property_declaration`'s name, a `for … in` variable and a `catch let` -- all
#: of which wrap the name in `pattern`. `guard_statement`/`if_statement` are
#: how the grammar spells an optional binding (`guard let X = …`), where the
#: name is a DIRECT child of the statement with no `pattern` around it.
#:
#: Deliberately over-inclusive. This set only ever raises the binding COUNT,
#: and the gate withholds when the count is not exactly one -- so a false
#: positive costs a file that could have been cleaned, while a miss costs a
#: WRONG-BRANCH FOLD. `parameter` is counted whole for the same reason: a Swift
#: parameter carries both an argument label and a local name, and separating
#: them buys nothing the gate can use.
_SWIFT_BINDING_PARENTS = (
    "pattern",
    "parameter",
    "lambda_parameter",
    "guard_statement",
    "if_statement",
    "function_declaration",
)


def _swift_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every `let` property declaration whose initialiser is exactly ``flag_key``.

    Must accept exactly what `swift_const.toml` accepts -- looser here and the
    delete rule strands a live reference, tighter and a file is withheld the
    rules could have cleaned. That file names three scopes (`source_file`,
    `class_body`, `statements`); this walk does not filter on scope at all,
    which is the safe direction of the two: a declaration seen HERE but not
    matched by the rules is simply never deleted.

    `var` is excluded to match the rules. Its value cannot be read off the
    declaration, so the key it names is not knowable -- the same call every
    other language makes for a reassignable binding.
    """
    consts: list[KeyConst] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "property_declaration":
            continue
        binding = next(
            (c for c in node.named_children if c.type == "value_binding_pattern"), None
        )
        if binding is None or _text(binding, source_bytes) != "let":
            continue
        pattern = next((c for c in node.named_children if c.type == "pattern"), None)
        if pattern is None:
            continue
        name_node = next(
            (c for c in pattern.named_children if c.type == "simple_identifier"), None
        )
        if name_node is None:
            continue
        literal = next(
            (c for c in node.named_children if c.type == "line_string_literal"), None
        )
        if literal is None:
            continue
        # The literal's text carries its quotes, so read the content child. An
        # interpolated key has more than one child and is not a fixed key.
        content = literal.named_children
        if len(content) != 1 or _text(content[0], source_bytes) != flag_key:
            continue
        consts.append(
            KeyConst(
                name=_text(name_node, source_bytes),
                declaration=node,
                name_nodes=(name_node,),
            )
        )
    return consts


def _swift_is_binding(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type in _SWIFT_BINDING_PARENTS


def _swift_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """First argument of a ``boolVariation`` call -- what the const rules fold.

    Swift puts a `call_suffix` between the argument list and the call, which no
    other language here does, so `_first_argument_of` returns the SUFFIX and the
    call is one level further up.
    """
    suffix = _first_argument_of(identifier, "value_argument", "value_arguments")
    if suffix is None or suffix.type != "call_suffix":
        return False
    call = suffix.parent
    if call is None or call.type != "call_expression":
        return False
    callee = call.named_children[0] if call.named_children else None
    if callee is None:
        return False
    # The three callee shapes swift_const.toml matches: a method
    # (`client.boolVariation`), a bare accessor (`useFlag`), and a negated bare
    # accessor, where the grammar's `!` precedence bug wraps the callee in a
    # `prefix_expression` (see rules/swift.toml).
    if callee.type == "navigation_expression":
        suffixes = [c for c in callee.named_children if c.type == "navigation_suffix"]
        if not suffixes:
            return False
        callee = next(
            (c for c in suffixes[-1].named_children if c.type == "simple_identifier"),
            None,
        )
    elif callee.type == "prefix_expression":
        callee = next(
            (c for c in callee.named_children if c.type == "simple_identifier"), None
        )
    if callee is None or callee.type != "simple_identifier":
        return False
    name = callee.text.decode("utf-8")
    return name in _SWIFT_FLAG_READ_CALLEES or name in accessors


def _kotlin_key_consts(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every ``const val`` whose value is ``flag_key``.

    Shares its reading with `kotlin_sentinel.const_val_keys`, which is the half
    that decides what the pre-pass rewrites -- the two must agree on what counts
    as a resolvable constant or the delete rule can strand a live reference.
    """
    return [
        KeyConst(name=name, declaration=node, name_nodes=(name_node,))
        for name, node, name_node, key in kotlin_sentinel.const_val_declarations(root)
        if key == flag_key
    ]


def _kotlin_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("variable_declaration", "parameter"):
        first = next((c for c in parent.named_children if c.type == "identifier"), None)
        return first is not None and first.id == node.id
    for holder in ("function_declaration", "class_declaration", "object_declaration"):
        if parent.type == holder:
            name = parent.child_by_field_name("name")
            if name is not None and name.id == node.id:
                return True
    if parent.type == "assignment":
        left = parent.child_by_field_name("left")
        if left is not None and left.id == node.id:
            return True
    return False


def _kotlin_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """First argument of a ``boolVariation`` call -- what the pre-pass rewrites.

    ``accessors`` are matched here for a reason unique to Kotlin: the pre-pass
    has ALWAYS marked them (`kotlin_sentinel._callee_spans`), so this gate was
    the only thing withholding a wrapper's read of a hoisted key. No rule clone
    was needed on the Kotlin side. See :func:`path_is_safe`.
    """
    call = _first_argument_of(identifier, "value_argument", "value_arguments")
    if call is None or call.type != "call_expression":
        return False
    callee = call.named_children[0] if call.named_children else None
    if callee is None:
        return False
    # An OPTIONAL call is skipped by the pre-pass (`_is_optional_call`), so it
    # is never sentinel-ified and never folded. Counting it as removable here
    # would let the delete rule take a declaration that call still reads --
    # this half must not be more permissive than the pre-pass.
    if kotlin_sentinel._is_optional_call(callee):
        return False
    # `client.boolVariation(...)` is a navigation expression whose LAST
    # identifier is the method; a bare `boolVariation(...)` is the identifier.
    if callee.type == "navigation_expression":
        identifiers = [c for c in callee.named_children if c.type == "identifier"]
        callee = identifiers[-1] if identifiers else None
    if callee is None or callee.type != "identifier":
        return False
    name = callee.text.decode("utf-8")
    return name in _KOTLIN_FLAG_READ_CALLEES or name in accessors


# ===========================================================================
# The registry
# ===========================================================================

_PROFILES.update(
    {
        "csharp": _Profile(
            key_consts=_csharp_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("identifier",)),
            is_binding=_csharp_is_binding,
            is_removable_read=_csharp_is_removable_read,
        ),
        "go": _Profile(
            key_consts=_go_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("identifier",)),
            is_binding=_go_is_binding,
            is_removable_read=_go_is_removable_read,
        ),
        **{
            language: _Profile(
                key_consts=_ts_key_consts,
                is_reference=lambda n, b, name: _is_named_identifier(
                    n, b, name, _TS_REFERENCE_TYPES
                ),
                # Reuses `ts_syntax`'s own binding traversal rather than a
                # second copy of its field rules — see `binding_nodes` there.
                binding_ids=lambda root: {n.id for n in ts_syntax.binding_nodes(root)},
                is_removable_read=_ts_is_removable_read,
            )
            for language in ("ts", "tsx", "js")
        },
        "swift": _Profile(
            key_consts=_swift_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(
                n, b, name, ("simple_identifier",)
            ),
            is_binding=_swift_is_binding,
            is_removable_read=_swift_is_removable_read,
        ),
        "kt": _Profile(
            key_consts=_kotlin_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("identifier",)),
            is_binding=_kotlin_is_binding,
            is_removable_read=_kotlin_is_removable_read,
        ),
        "python": _Profile(
            key_consts=_python_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("identifier",)),
            is_binding=_python_is_binding,
            is_removable_read=_python_is_removable_read,
        ),
        "dart": _Profile(
            key_consts=_dart_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("identifier",)),
            is_binding=_dart_is_binding,
            is_removable_read=_dart_is_removable_read,
        ),
        "ruby": _Profile(
            key_consts=_ruby_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("constant",)),
            is_binding=_ruby_is_binding,
            is_removable_read=_ruby_is_removable_read,
        ),
        "php": _Profile(
            key_consts=_php_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("name",)),
            is_binding=_php_is_binding,
            is_removable_read=_php_is_removable_read,
        ),
        "java": _Profile(
            key_consts=_java_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("identifier",)),
            is_binding=_java_is_binding,
            is_removable_read=_java_is_removable_read,
        ),
    }
)

# ERB shares RUBY's profile object outright, and that is the honest spelling of
# what is going on: `.erb` takes `ruby_const.toml` through `rule_base`, so the
# rules this gate has to mirror are Ruby's, node for node. A copy would be a
# second thing to keep in step with those rules for no benefit at all.
#
# **The TREE it is handed is NOT the template's.** `syntax.const_path_is_safe`
# substitutes the CODE VIEW — every `code` token of the template, joined — and
# parses THAT as Ruby before calling in here. Without that step a Ruby profile
# over a template tree finds no constant in any file and reports every one
# trivially safe, which is the vacuous gate this module's docstring warns
# about: `ruby_const.toml` would run over `.erb` with no shadow check at all.
# It is the one place where "no profile" and "a profile that can never match"
# fail identically, and neither is visible from the outside.
#
# Dropping the markup on the way is correct rather than merely convenient: a
# constant's name appearing in the page's TEXT is not a reference to it, and
# counting one would withhold a file over a word.
#
# Assigned after the update above rather than inside it, because a dict literal
# cannot read the mapping it is building.
#
# What the RUNTIME actually uses this entry for is MEMBERSHIP, not lookup:
# `syntax.const_path_is_safe` rebinds `language` to `"ruby"` before calling in
# here, so `_PROFILES["ruby"]` is what gets fetched. The `erb` key earns its
# place by making `supported_languages()` contain `"erb"`, which is the test
# that decides whether the code view is derived at all. Sharing Ruby's object
# rather than copying it is still the right spelling — it is what
# `test_the_erb_profile_is_rubys_own_object` pins — but do not read this line as
# the thing that answers an `erb` question. Anything that calls `key_consts`
# for `erb` directly owes it the same derivation.
_PROFILES["erb"] = _PROFILES["ruby"]
