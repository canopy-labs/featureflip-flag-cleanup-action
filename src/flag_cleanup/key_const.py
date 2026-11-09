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

import contextlib
import re
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

from tree_sitter import Node

from flag_cleanup import dead_branches, kotlin_sentinel, rule_synthesis, ts_syntax
from flag_cleanup.tree_walk import preorder

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

    ``withholds`` -- optional, a whole-file veto for what the per-reference loop
    cannot express: it is handed every hoisted key and returns ``True`` to
    withhold the file whatever the count says. Swift needs it because a bare
    name can mean a member of an enclosing type's supertype, which no binding
    in the file shows.

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
    withholds: Callable[[Node, bytes, list[KeyConst]], bool] | None = None


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
    """Every node under ``node``, parents first, in source order."""
    return preorder(node)


def path_is_safe(
    root: Node,
    source_bytes: bytes,
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] | frozenset[str] = (),
    treatment: bool | None = None,
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

    ``treatment``, when given, leaves out a reference inside a block the fold
    of one of the file's own removable reads deletes (see
    :func:`dead_references`): it goes with the block. The runner must then
    check the rewritten file, since this only predicts the fold.
    """
    accessor_names = frozenset(accessors)
    profile = _PROFILES.get(language)
    if profile is None:
        return True
    consts = profile.key_consts(root, source_bytes, flag_key)
    if not consts:
        return True
    if profile.withholds is not None and profile.withholds(root, source_bytes, consts):
        return False
    blocks = (
        _dead_blocks(root, source_bytes, language, consts, profile, accessor_names, treatment)
        if treatment is not None and dead_branches.supported(language)
        else []
    )

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
            profile.is_removable_read(node, accessor_names) or dead_branches.inside(node, blocks)
            for node in references
        ):
            return False
    return True


def dead_references(
    root: Node,
    source_bytes: bytes,
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] | frozenset[str],
    treatment: bool,
) -> dict[str, list[Node]]:
    """Per hoisted key's name, the references the gate leaves out under
    ``treatment``: neither a binding nor a removable read, and inside a block
    the fold of a removable read deletes. The runner asks it of the original
    file, then checks none of those names is still referenced once the
    declaration is gone (:func:`dangling_names`)."""
    profile = _PROFILES.get(language)
    if profile is None or not dead_branches.supported(language):
        return {}
    consts = profile.key_consts(root, source_bytes, flag_key)
    if not consts:
        return {}
    blocks = _dead_blocks(root, source_bytes, language, consts, profile, frozenset(accessors), treatment)
    if not blocks:
        return {}
    accessor_names = frozenset(accessors)
    found: dict[str, list[Node]] = {}
    for const in consts:
        for node in _walk(root):
            # A binding inside the block is no concern here: it already
            # withholds the file in `path_is_safe`, whose count it breaks.
            if (
                dead_branches.inside(node, blocks)
                and profile.is_reference(node, source_bytes, const.name)
                and not profile.is_removable_read(node, accessor_names)
            ):
                found.setdefault(const.name, []).append(node)
    return found


def dangling_names(
    root: Node, source_bytes: bytes, language: str, flag_key: str, names: set[str]
) -> set[str]:
    """Which of ``names`` the file still references although it no longer
    declares them as a hoisted key: what a deleted declaration left behind."""
    profile = _PROFILES.get(language)
    if profile is None or not names:
        return set()
    declared = {const.name for const in profile.key_consts(root, source_bytes, flag_key)}
    return {
        name
        for name in names - declared
        if any(profile.is_reference(node, source_bytes, name) for node in _walk(root))
    }


def dead_reference_survived(
    original: str,
    rewritten: str,
    language: str,
    flag_key: str,
    accessors: tuple[str, ...] | frozenset[str],
    treatment: bool,
) -> bool:
    """Whether a name :func:`path_is_safe` let go with a deleted block is
    still referenced in ``rewritten`` while its declaration is gone. The
    check that makes leaving it out safe."""
    if not dead_branches.supported(language) or flag_key not in original:
        return False
    from flag_cleanup import syntax

    left_out = dead_references(
        syntax._parse(original, language), original.encode("utf-8"), language, flag_key,
        accessors, treatment,
    )
    if not left_out:
        return False
    data = rewritten.encode("utf-8")
    return bool(
        dangling_names(syntax._parse(rewritten, language), data, language, flag_key, set(left_out))
    )


def _dead_blocks(
    root: Node,
    source_bytes: bytes,
    language: str,
    consts: list[KeyConst],
    profile: _Profile,
    accessor_names: frozenset[str],
    treatment: bool,
) -> list[tuple[int, int]]:
    """The blocks the fold of a removable read of one of ``consts`` deletes:
    the read must be the call that is an ``if``'s whole condition, not one
    nested inside it."""

    def is_read(call: Node) -> bool:
        for node in _walk(call):
            if (
                any(profile.is_reference(node, source_bytes, const.name) for const in consts)
                and profile.is_removable_read(node, accessor_names)
                and dead_branches.nearest_call(node, language) is not None
                and dead_branches.nearest_call(node, language).id == call.id
            ):
                return True
        return False

    return dead_branches.dead_blocks(root, language, treatment, is_read)


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
    """Every ``const string`` whose value is ``flag_key``, as a literal or as
    ``nameof(flag_key)``.

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
        if name is None or value is None:
            continue
        if value.type == "invocation_expression":
            content = _csharp_nameof_argument(value)
        elif value.type == "string_literal":
            content = next(
                (c for c in value.named_children if c.type == "string_literal_content"), None
            )
        else:
            continue
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


def _csharp_nameof_argument(invocation: Node) -> Node | None:
    """The plain name ``nameof(NAME)`` takes, None for any other call."""
    function = invocation.child_by_field_name("function")
    arguments = invocation.child_by_field_name("arguments")
    if function is None or function.type != "identifier" or function.text != b"nameof":
        return None
    if arguments is None or len(arguments.named_children) != 1:
        return None
    argument = arguments.named_children[0]
    if len(argument.named_children) != 1 or argument.named_children[0].type != "identifier":
        return None
    return argument.named_children[0]


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


def _csharp_attribute_name(attribute: Node) -> str:
    """An attribute's name as an ``accessors`` entry spells it: ``FeatureGate``
    for ``Mvc.FeatureGateAttribute``."""
    name = attribute.child_by_field_name("name")
    assert name is not None and name.text is not None
    last = name.text.decode("utf-8").rsplit(".", 1)[-1]
    return last.removesuffix("Attribute") or last


def _csharp_is_gate_argument(argument: Node, accessors: frozenset[str]) -> bool:
    """Whether ``argument``, an ``attribute_argument``, is all that an attribute
    gate takes. :mod:`flag_cleanup.csharp_gates` removes such a gate."""
    arguments = argument.parent
    attribute = arguments.parent if arguments is not None else None
    return (
        attribute is not None
        and attribute.type == "attribute"
        and len(argument.named_children) == 1
        and len(arguments.named_children) == 1
        and _csharp_attribute_name(attribute) in accessors
    )


def _csharp_names_itself(identifier: Node) -> bool:
    """Whether ``identifier`` is the ``Beta`` in ``Beta = nameof(Beta)``, which
    goes with the declaration."""
    current = identifier
    for _ in range(3):  # argument, argument_list, invocation_expression
        current = current.parent if current is not None else None
    if current is None or current.parent is None or current.type != "invocation_expression":
        return False
    declarator = current.parent
    argument = _csharp_nameof_argument(current)
    name = declarator.child_by_field_name("name") if declarator.type == "variable_declarator" else None
    return (
        argument is not None
        and argument.id == identifier.id
        and name is not None
        and name.text == identifier.text
    )


def _csharp_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """Whether ``identifier`` is a key argument ``csharp_const.toml`` removes.

    Mirrors those rules exactly -- first argument of a `BoolVariation` call, or
    of a `GetBooleanValueAsync` call that is itself awaited. Anything looser
    would let the delete rule strand a live reference; anything tighter would
    withhold a file the rules could have cleaned.

    A name in ``accessors`` is matched like `BoolVariation` -- plain, not
    awaited-only -- because that is the shape its clone takes. It may also name
    an attribute gate, which a pass after the rules removes.

    The key may also be spelled ``Owner.NAME`` when the owner is declared in
    this file (:func:`_csharp_qualified_by_owner`), which the rules match too.
    """
    argument = identifier
    parent = identifier.parent
    if _csharp_names_itself(identifier):
        return True
    if (
        parent is not None
        and parent.type == "member_access_expression"
        and (name := parent.child_by_field_name("name")) is not None
        and name.id == identifier.id
    ):
        if not _csharp_qualified_by_owner(parent):
            return False
        argument = parent
    if argument.parent is not None and argument.parent.type == "attribute_argument":
        return _csharp_is_gate_argument(argument.parent, accessors)
    invocation = _first_argument_of(argument, "argument", "argument_list")
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


#: Types whose body can declare a ``const`` field.
_CSHARP_OWNER_TYPES = frozenset(
    {
        "class_declaration",
        "struct_declaration",
        "interface_declaration",
        "record_declaration",
        "record_struct_declaration",
    }
)
#: Every C# declaration that can introduce a type's simple name.
_CSHARP_TYPE_DECLARATIONS = _CSHARP_OWNER_TYPES | {"enum_declaration", "delegate_declaration"}


def _csharp_namespace_of(node: Node) -> int | None:
    """The id of the nearest block ``namespace`` around ``node``, or None.

    A file-scoped namespace is a sibling of the types, not their ancestor, and
    a file holding one can hold no block namespace, so None stands for it too.
    """
    current = node.parent
    while current is not None:
        if current.type == "namespace_declaration":
            return current.id
        current = current.parent
    return None


def _csharp_qualified_by_owner(access: Node) -> bool:
    """Whether ``access`` (``Owner.NAME``) names the constant declared here.

    Only when ``Owner`` cannot mean anything else at the read: written as
    exactly ``Owner.NAME``; ``Owner`` the one non-generic, top-level type of
    that name in the file, in the read's own namespace (a type in the
    enclosing namespace is found before any ``using``); that type's body
    declaring a field ``NAME`` (bound exactly once in the file, so the key
    constant); and nothing else in the file -- a member, a nested type, a
    local, a parameter, an alias -- spelling a declaration of ``Owner``,
    which C# would find first. A member of that name inherited from a type
    in another file is not visible from here.
    """
    owner_node = access.child_by_field_name("expression")
    member = access.child_by_field_name("name")
    if owner_node is None or member is None or owner_node.type != "identifier":
        return False
    owner = owner_node.text.decode("utf-8")
    if access.text.decode("utf-8") != f"{owner}.{member.text.decode('utf-8')}":
        return False
    root = access
    while root.parent is not None:
        root = root.parent
    declared = []
    for node in _walk(root):
        if node.type != "identifier" or node.text != owner_node.text:
            continue
        holder = node.parent
        if holder is None:
            continue
        if holder.type == "type_parameter":
            return False
        name = holder.child_by_field_name("name")
        if name is None or name.id != node.id:
            continue
        if holder.type not in _CSHARP_TYPE_DECLARATIONS:
            return False  # a member, local, parameter or alias named the same
        declared.append(holder)
    if len(declared) != 1:
        return False
    owner_type = declared[0]
    container = owner_type.parent
    if owner_type.type not in _CSHARP_OWNER_TYPES or container is None:
        return False
    if any(c.type == "type_parameter_list" for c in owner_type.children):
        return False
    top_level = container.type == "compilation_unit" or (
        container.type == "declaration_list"
        and container.parent is not None
        and container.parent.type == "namespace_declaration"
    )
    if not top_level or _csharp_namespace_of(owner_type) != _csharp_namespace_of(access):
        return False
    body = owner_type.child_by_field_name("body")
    if body is None:
        return False
    wanted = member.text
    for field in body.named_children:
        if field.type != "field_declaration":
            continue
        for declaration in field.named_children:
            if declaration.type != "variable_declaration":
                continue
            for declarator in declaration.named_children:
                name = declarator.child_by_field_name("name") if declarator.type == "variable_declarator" else None
                if name is not None and name.text == wanted:
                    return True
    return False


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

#: The SHIPPED Go call names whose SECOND argument is the key: the OpenFeature
#: client's ``Boolean(ctx, key, default, evalCtx)``. A configured accessor is
#: read in either position, because ``go_const.toml`` is cloned for it from
#: both templates, as ``go.toml``'s literal rules are.
_GO_SECOND_ARGUMENT_READ_CALLEES = frozenset({"Boolean"})

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

    Requires exactly one name in the spec, alone in its declaration or one
    entry of a ``const ( ... )`` group. The rules mirror both: the read rules'
    scope and the two delete rules in ``go_const.toml`` match any spec of a
    declaration, and ``value: (expression_list . (...) .)`` bounds the number
    of VALUES -- a Go const spec binds as many names as it has values, so that
    anchor is what makes "one name" true on the rule side.

    Before that anchor existed the two halves disagreed on exactly that
    dimension: ``const a, b = "old-checkout", "other"`` is one spec, so the
    rules matched it, while this function skipped it for having two names. A
    file whose only key const is skipped here looks EMPTY to
    :func:`path_is_safe`, which reports it trivially safe -- so the rules ran
    with no gate at all and the delete rule took ``b`` with it (#2700).

    A spec of a group whose entries depend on their position (``iota``, or an
    entry with no value repeating the one above it) is still RETURNED here, so
    the file is not mistaken for one with no key constant; :func:`_go_withholds`
    then keeps the whole file. Deciding it here instead would make such a file
    look empty and run the rules ungated, the same inversion.

    ``var_declaration`` is a different node type in this grammar, so a mutable
    ``var`` key is structurally excluded rather than needing a guard.
    """
    found: list[KeyConst] = []
    for node in _walk(root):
        if node.type != "const_declaration":
            continue
        for spec in (c for c in node.named_children if c.type == "const_spec"):
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
    found.extend(_go_var_holders(root, source_bytes, flag_key))
    return found


def _go_constructor_call(value: Node, source_bytes: bytes) -> Node | None:
    """The ``Ctor("key", ...)`` call a package-level ``var``'s only value is,
    or ``None``: a plain function name or ``pkg.Name``, no type arguments,
    the key an interpreted string literal in the FIRST argument."""
    if value.type != "expression_list" or len(value.named_children) != 1:
        return None
    call = value.named_children[0]
    if call.type != "call_expression" or call.child_by_field_name("type_arguments") is not None:
        return None
    function = call.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "selector_expression":
        operand = function.child_by_field_name("operand")
        field = function.child_by_field_name("field")
        if operand is None or operand.type != "identifier" or field is None:
            return None
    elif function.type != "identifier":
        return None
    arguments = call.child_by_field_name("arguments")
    if arguments is None or not arguments.named_children:
        return None
    first = arguments.named_children[0]
    if first.type != "interpreted_string_literal":
        return None
    return first


def _go_var_holders(root: Node, source_bytes: bytes, flag_key: str) -> list[KeyConst]:
    """Every package-level ``var NAME = Ctor("flag-key", ...)``: a typed flag
    HANDLE that other files pass to a configured accessor
    (``ff.BoolFlag(ctx, pkg.NAME)``).

    A ``var`` can be reassigned and has an address, which a ``const`` does not,
    so what proves it a holder is that NOTHING else mentions it: in this file
    :func:`_go_withholds` keeps the file when its name occurs anywhere but the
    declaration, and in every other file the cross-file reader refuses any
    mention that is not a read it folds (an assignment, ``&NAME``, ``NAME.f``,
    a call that is not a configured accessor's). Whether the constructor really
    returns a value naming the key is the one thing no syntax proves; it is the
    customer's naming of the constructor by passing the key as its first
    argument."""
    found: list[KeyConst] = []
    for declaration in root.named_children:
        if declaration.type != "var_declaration":
            continue
        specs = [
            spec
            for child in declaration.named_children
            for spec in (child.named_children if child.type == "var_spec_list" else [child])
            if spec.type == "var_spec"
        ]
        for spec in specs:
            names = [c for c in spec.children_by_field_name("name") if c.type == "identifier"]
            value = spec.child_by_field_name("value")
            if len(names) != 1 or value is None:
                continue
            literal = _go_constructor_call(value, source_bytes)
            if literal is None:
                continue
            content = next((c for c in literal.named_children if "content" in c.type), None)
            text = _text(content, source_bytes) if content is not None else _text(literal, source_bytes)[1:-1]
            if text == flag_key and _text(names[0], source_bytes) != "_":
                found.append(
                    KeyConst(
                        name=_text(names[0], source_bytes),
                        declaration=declaration,
                        name_nodes=(names[0],),
                    )
                )
    return found


def go_is_var_holder(const: KeyConst) -> bool:
    return const.declaration.type == "var_declaration"


_GO_FILE: ContextVar[str | None] = ContextVar("go_key_const_file", default=None)


@contextlib.contextmanager
def go_file(path: str | None) -> Iterator[None]:
    """The Go file the next gate question is about. A handle's constructor is
    proven from the repository around the file, so a question asked without
    one cannot prove it and withholds the file (see :func:`_go_withholds`)."""
    token = _GO_FILE.set(None if path is None else str(path))
    try:
        yield
    finally:
        _GO_FILE.reset(token)


def _go_holder_call(const: KeyConst) -> Node | None:
    for spec in _walk(const.declaration):
        if spec.type != "var_spec":
            continue
        names = spec.children_by_field_name("name")
        if any(n.id == const.name_nodes[0].id for n in names):
            value = spec.child_by_field_name("value")
            return value.named_children[0] if value is not None and value.named_children else None
    return None


def _go_param_carried(constructor: Node) -> bool:
    """Whether a constructor's body is one ``return T{…}`` or ``return &T{…}``
    holding its first parameter, a ``string``, unmodified as a field value."""
    if constructor.child_by_field_name("type_parameters") is not None:
        return False
    parameters = constructor.child_by_field_name("parameters")
    body = constructor.child_by_field_name("body")
    if parameters is None or body is None:
        return False
    first = next(iter(parameters.named_children), None)
    if first is None or first.type != "parameter_declaration":
        return False
    names = first.children_by_field_name("name")
    kind = first.child_by_field_name("type")
    if not names or kind is None or kind.text != b"string":
        return False
    parameter = names[0].text
    listed = next((c for c in body.named_children if c.type == "statement_list"), None)
    statements = [c for c in (listed.named_children if listed is not None else []) if c.type != "comment"]
    if len(statements) != 1 or statements[0].type != "return_statement":
        return False
    results = statements[0].named_children
    if len(results) != 1:
        return False
    expression = results[0]
    if expression.type == "expression_list":
        if len(expression.named_children) != 1:
            return False
        expression = expression.named_children[0]
    if expression.type == "unary_expression":
        if not expression.children or expression.children[0].type != "&":
            return False
        expression = expression.child_by_field_name("operand")
    if expression is None or expression.type != "composite_literal":
        return False
    if expression.child_by_field_name("type").type not in ("type_identifier", "qualified_type"):
        return False
    literal = expression.child_by_field_name("body")
    if literal is None:
        return False
    carried = False
    for element in literal.named_children:
        value = element
        if element.type == "keyed_element":
            value = element.child_by_field_name("value") or element
        elif element.type == "literal_element":
            value = element
        # `literal_element` wraps the value in some grammar versions.
        while value.type in ("literal_element", "literal_value") and len(value.named_children) == 1:
            value = value.named_children[0]
        if value.type == "identifier" and value.text == parameter:
            carried = True
        elif any(n.type == "identifier" and n.text == parameter for n in _walk(value)):
            # Another field holds something derived from it.
            return False
    return carried


def go_constructor_proven(call: Node, path: str | None) -> bool:
    """Whether the ``Ctor`` of a handle's ``Ctor("key", …)`` is declared in the
    repository as a function that stores its first argument unmodified in what
    it returns (:func:`_go_param_carried`). ``Ctor`` is ``name`` of the file's
    own package or ``pkg.Name`` through an import of the same module; any other
    callee (the standard library, a dependency) is not proven."""
    if path is None:
        return False
    from pathlib import Path

    from flag_cleanup import go_scope

    function = call.child_by_field_name("function")
    if function is None:
        return False
    from flag_cleanup import source_index

    packages = go_scope.Packages(Path(source_index.checkout_root(str(Path(path).resolve().parent))))
    here = Path(path).resolve()
    file = packages.file(here)
    if file is None or file.clause is None:
        return False
    if function.type == "identifier":
        name = function.text
        directory, clause = here.parent, file.clause
    elif function.type == "selector_expression":
        operand = function.child_by_field_name("operand")
        field = function.child_by_field_name("field")
        if operand is None or field is None or operand.type != "identifier":
            return False
        if go_scope.local_binders(function, operand.text.decode()):
            return False
        imported = packages.imported(file, operand.text.decode())
        if imported is None:
            return False
        name, directory, clause = field.text, imported.directory, imported.clause
    else:
        return False
    found = [
        declaration
        for member in packages.package(directory, clause)
        for declaration in member.root.named_children
        if declaration.type == "function_declaration"
        and declaration.child_by_field_name("name") is not None
        and declaration.child_by_field_name("name").text == name
    ]
    return len(found) == 1 and _go_param_carried(found[0])


def go_group_is_positional(declaration: Node, source_bytes: bytes) -> bool:
    """Whether removing one spec of this ``const ( ... )`` group can change the
    others: an entry with no value repeats the expression above it, and
    ``iota`` counts entries. Either anywhere in the group keeps every entry.
    A declaration of one spec is never positional: it is deleted whole."""
    specs = [c for c in declaration.named_children if c.type == "const_spec"]
    if len(specs) < 2:
        return False
    if any(spec.child_by_field_name("value") is None for spec in specs):
        return True
    return any(
        n.type == "iota" or (n.type == "identifier" and _text(n, source_bytes) == "iota")
        for n in _walk(declaration)
    )


def go_positional_key_consts(source: str, flag_key: str) -> list[str]:
    """The names of the file's key constants that :func:`_go_withholds` keeps
    for sitting in a positional group, so the run can say why."""
    from flag_cleanup import syntax

    data = source.encode("utf-8")
    return [
        const.name
        for const in _go_key_consts(syntax._parse(source, "go"), data, flag_key)
        if go_group_is_positional(const.declaration, data)
    ]


def _go_withholds(root: Node, source_bytes: bytes, consts: list[KeyConst]) -> bool:
    """Keep the file when a key constant sits in a positional group (see
    :func:`go_group_is_positional`), or when one group holds the key twice:
    the delete rule for a group entry needs another entry beside it, so the
    second would be left as the group's last."""
    groups = [const.declaration.id for const in consts]
    if len(groups) != len(set(groups)) or any(
        go_group_is_positional(const.declaration, source_bytes) for const in consts
    ):
        return True
    # A `var` handle is folded only where another file reads it, and the
    # reader rules know no in-file read of one: any other mention here (a
    # registry listing it, a read, an assignment, `&NAME`) keeps the file.
    for const in consts:
        if not go_is_var_holder(const):
            continue
        call = _go_holder_call(const)
        if call is None or not go_constructor_proven(call, _GO_FILE.get()):
            # Not proven to carry the key: no holder, the file is kept.
            return True
        declared = {n.id for n in const.name_nodes}
        if any(
            n.type == "identifier" and n.id not in declared and _text(n, source_bytes) == const.name
            for n in _walk(root)
        ):
            return True
    return False


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
    """A read ``go_const.toml`` removes: the first argument of a
    ``BoolVariation`` call or the second of a ``Boolean`` call.

    ``accessors`` are the run's own wrapper names, matched on the same terms:
    the const rules are cloned for each of them from both templates, so a
    wrapper's read with the key in either position is one this file removes.
    See :func:`path_is_safe`.
    """
    first = _first_argument_of(identifier, "identifier", "argument_list")
    if first is not None:
        return _go_callee_in(first, _GO_FLAG_READ_CALLEES | accessors)
    second = _go_second_argument_of(identifier)
    return second is not None and _go_callee_in(second, _GO_SECOND_ARGUMENT_READ_CALLEES | accessors)


def _go_second_argument_of(identifier: Node) -> Node | None:
    """The call whose SECOND argument is ``identifier``, or ``None``: the
    ``. (_) . (identifier)`` anchor of ``go_const.toml``'s ``Boolean`` rules."""
    argument_list = identifier.parent
    if argument_list is None or argument_list.type != "argument_list":
        return None
    arguments = argument_list.named_children
    if len(arguments) < 2 or arguments[1].id != identifier.id:
        return None
    return argument_list.parent


def _go_callee_in(call: Node, callees: frozenset[str]) -> bool:
    if call.type != "call_expression":
        return False
    function = call.child_by_field_name("function")
    if function is None:
        return False
    if function.type == "selector_expression":
        function = function.child_by_field_name("field")
    if function is None or function.type not in ("identifier", "field_identifier"):
        return False
    return function.text.decode("utf-8") in callees


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
_TS_FLAG_READ_CALLEES = frozenset({"boolVariation", "useFeatureFlag", "useBooleanFlagValue", "getBooleanValue"})
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
    """A flag read ``ts_const.toml`` removes: the key is the first argument of
    a read callee, or the argument at the index a positioned accessor
    (``get[1]``) names.

    An OPTIONAL call is excluded for the reason ``ts.toml`` excludes it from
    the literal rules: `client.boolVariation?.(K, ...)` is `undefined` when the
    callee is nullish, so folding it is a behaviour change rather than a
    simplification -- and the const rules carry the same `not_contains` guard,
    so counting such a read as removable here would let the delete rule strand
    a reference the engine deliberately left standing.
    """
    arguments = identifier.parent
    if arguments is None or arguments.type != "arguments" or not arguments.named_children:
        return False
    index = next((i for i, c in enumerate(arguments.named_children) if c.id == identifier.id), None)
    if index is None:
        return False
    call = arguments.parent
    if call is None or call.type != "call_expression":
        return False
    if ts_syntax.declined_by_const_rules(call, positioned=bool(index)):
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
    if index:
        return rule_synthesis.positions_of(accessors).get(callee) == index
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
    return _php_is_removable_argument(candidate, accessors)


def _php_is_removable_argument(value: Node, accessors: frozenset[str]) -> bool:
    """Whether ``value`` is the first argument of a read the PHP rules fold:
    a ``boolVariation`` (or configured accessor) call, outside string
    interpolation. A static call (``Feature::active(…)``) is a read only for a
    configured accessor: the rules carry that arm in accessor clones alone
    (see the `; accessor-arm:` lines in ``php.toml``)."""
    call = _first_argument_of(value, "argument", "arguments")
    if call is None or call.type not in (
        "member_call_expression",
        "function_call_expression",
        "scoped_call_expression",
    ):
        return False
    callee = call.child_by_field_name("name") or call.child_by_field_name("function")
    if callee is None or callee.type != "name":
        return False
    name = callee.text.decode("utf-8")
    if call.type == "scoped_call_expression":
        if name not in accessors:
            return False
    elif name not in _PHP_FLAG_READ_CALLEES and name not in accessors:
        return False
    # The rules never fold a read inside string interpolation (`"{$c->...}"`
    # would become `"{true}"`), so it stays, and so must the const it reads.
    ancestor = call.parent
    while ancestor is not None:
        if ancestor.type in ("encapsed_string", "heredoc"):
            return False
        ancestor = ancestor.parent
    return True


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


def _ruby_withholds(root: Node, source_bytes: bytes, consts: list[KeyConst]) -> bool:
    """Whether the name is spelled somewhere no ``constant`` node covers.

    `private_constant :FLAG_KEY`, `deprecate_constant`, `public_constant`,
    `const_get(:FLAG_KEY)` and `const_defined?("FLAG_KEY")` all name the
    constant through a SYMBOL or a STRING, which `is_reference` never sees.
    Deleting the declaration under such a mention leaves code that raises
    `NameError` when it runs -- for `private_constant`, when the file loads.
    So any token spelling of the name outside a comment and outside a
    `constant` node (those the per-reference loop already judges) withholds
    the file and keeps the declaration. It is the same token scan the
    cross-file side runs over a declaring file, so the two agree on what a
    mention is; an over-count, such as the name inside a log message, only
    keeps a declaration.
    """
    names = sorted({const.name for const in consts})
    pattern = re.compile(
        rb"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])"
        % b"|".join(re.escape(n.encode("utf-8")) for n in names)
    )
    covered = [
        (node.start_byte, node.end_byte)
        for node in _walk(root)
        if node.type == "comment"
        or (node.type == "constant" and _text(node, source_bytes) in names)
    ]
    return any(
        not any(start <= hit.start() < end for start, end in covered)
        for hit in pattern.finditer(source_bytes)
    )


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
    # A capture pattern (`case X:`, `case [X]:`) binds X: a `dotted_name` of
    # one name directly in a `case_pattern`. A dotted value pattern
    # (`case a.X:`) binds nothing.
    if (
        parent.type == "dotted_name"
        and len(parent.named_children) == 1
        and parent.parent is not None
        and parent.parent.type == "case_pattern"
    ):
        return True
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


#: The `not_contains` filters the positioned Python key-constant rule templates
#: (`python_const_key_positions.toml`, `python_xconst_key_positions.toml`) carry,
#: by the name of what they decline. :func:`python_declined_by_const_rules` must
#: decline exactly these; a test parses the TOML and compares.
PYTHON_CONST_RULE_FILTERS = {
    "list_splat": "((list_splat) @spread)",
    "dictionary_splat": "((dictionary_splat) @spread)",
    "comment": "((comment) @comment)",
}


#: The `not_enclosing_node` filters the same templates carry (whitespace is not
#: significant; the test normalises it): a call inside a decorator whose own call
#: is the accessor (``@flag_is_active(request, OLD)``, left for `python_gates`),
#: or is ``method_decorator``. :func:`python_declined_by_const_rules` mirrors both.
PYTHON_CONST_NOT_ENCLOSING = {
    "gate": (
        "((decorator (call function: [(attribute attribute: (identifier) @gate_fn) "
        "(identifier) @gate_fn])) @gate (#eq? @gate_fn \"@sdk_fn\"))"
    ),
    "adapter": (
        "((decorator (call function: [(attribute attribute: (identifier) @adapter_fn) "
        "(identifier) @adapter_fn])) @adapted (#eq? @adapter_fn \"method_decorator\"))"
    ),
}


def _python_callee_name(call: Node) -> str:
    function = call.child_by_field_name("function")
    if function is not None and function.type == "attribute":
        function = function.child_by_field_name("attribute")
    return function.text.decode("utf-8") if function is not None and function.type == "identifier" else ""


def python_declined_by_const_rules(call: Node) -> bool:
    """Whether the positioned key-constant rules decline ``call``: a ``*args``,
    a ``**kwargs`` or a comment anywhere within it, or a call inside a decorator
    gate on the same accessor or on ``method_decorator``."""
    wanted = {"list_splat", "dictionary_splat", "comment"}
    if any(n.type in wanted for n in _walk(call)):
        return True
    callee = _python_callee_name(call)
    ancestor = call.parent
    while ancestor is not None:
        if ancestor.type == "decorator":
            gate = next((c for c in ancestor.named_children if c.type == "call"), None)
            if gate is not None and _python_callee_name(gate) in (callee, "method_decorator"):
                return True
        ancestor = ancestor.parent
    return False


def python_positioned_read(argument: Node, accessors: frozenset[str]) -> bool:
    """Whether ``argument`` is the key of a positioned accessor's read the rules remove.

    ``argument`` is the argument NODE (an identifier, or an attribute for a key
    of another module). It counts only in the slot the accessor was declared
    with (``get[1]``), and only when the rules would not decline the call."""
    arguments = argument.parent
    if arguments is None or arguments.type != "argument_list":
        return False
    index = next((i for i, c in enumerate(arguments.named_children) if c.id == argument.id), None)
    if not index:
        return False
    call = arguments.parent
    if call is None or call.type != "call" or python_declined_by_const_rules(call):
        return False
    function = call.child_by_field_name("function")
    if function is not None and function.type == "attribute":
        function = function.child_by_field_name("attribute")
    if function is None or function.type != "identifier":
        return False
    return rule_synthesis.positions_of(accessors).get(function.text.decode("utf-8")) == index


def _python_is_removable_read(identifier: Node, accessors: frozenset[str]) -> bool:
    """A read ``python_const.toml`` removes: the key is the first argument of a
    read callee, or the argument at the index a positioned accessor
    (``get[1]``) names.

    Mirrors those rules exactly, INCLUDING the boolean-literal requirement on
    `variation`: that SDK read is generic over the flag's type, so the literal
    default is the only evidence the flag is a boolean at all. `get_boolean_value`
    is boolean by name and needs none.
    """
    if python_positioned_read(identifier, accessors):
        return True
    call = _first_argument_of(identifier, "identifier", "argument_list")
    if call is None or call.type != "call":
        return False
    return _python_is_removable_call(call, accessors)


def _python_is_removable_call(call: Node, accessors: frozenset[str]) -> bool:
    """Whether ``call`` is a read ``python_const.toml`` (or an accessor clone) removes.

    The key's position is the caller's business: an identifier here, an
    attribute for a reader of another module's constant.
    """
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

#: The callees the pre-pass marks (`kotlin_sentinel.READ_CALLEES`), the same
#: object, so the gate cannot drift from the fold. The run's ``accessors`` are
#: added on top of it by :func:`_kotlin_is_removable_read`, and Kotlin is the
#: one language that needed NOTHING else for that to be safe: its fold is the
#: sentinel pre-pass, not a rule, and `kotlin_sentinel._callee_spans` has marked
#: `{SDK_FUNCTION_NAME, *accessors}` since accessors existed. So until #2730
#: this gate was the ONLY thing withholding an accessor's read of a hoisted key
#: -- the pre-pass would have folded it, `resolve_consts` was just never true
#: for such a file. The other nine languages needed a rule clone as well.
#: `getBooleanValue` is NOT import-gated here, on purpose: an import-less read
#: of a hoisted key is admitted, and the pre-pass then refuses the flag on it.
#: Withholding it here would keep the key unresolved, so nothing would refuse.
_KOTLIN_FLAG_READ_CALLEES = kotlin_sentinel.READ_CALLEES


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
    rules could have cleaned. That file names four scopes (`source_file`,
    `class_body`, `enum_class_body`, `statements`); this walk does not filter on scope at all,
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
        # EXACTLY ONE binding. `let a = "x", b = "y"` is one declaration holding
        # several name/value pairs, and the delete rule removes the declaration
        # whole, so a key in one pair would take its siblings with it. The rules
        # anchor each pattern to its own literal for the same reason.
        patterns = [c for c in node.named_children if c.type == "pattern"]
        if len(patterns) != 1:
            continue
        pattern = patterns[0]
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
                name=_swift_name(name_node, source_bytes),
                declaration=node,
                name_nodes=(name_node,),
            )
        )
    return consts


def _swift_name(node: Node, source_bytes: bytes) -> str:
    """The name an identifier spells. Backticks may wrap any Swift name
    without changing it (`` `k` `` is `k`), and the grammar keeps them in the
    node's text, so a name is compared without them -- or a backticked
    binding of the constant's name is invisible to the count."""
    text = _text(node, source_bytes)
    if len(text) > 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1]
    return text


def _swift_is_reference(node: Node, source_bytes: bytes, name: str) -> bool:
    return node.type == "simple_identifier" and _swift_name(node, source_bytes) == name


def _swift_opens_a_supertype(node: Node) -> bool:
    """Whether the members of something other than ``node``'s own body are in
    unqualified scope inside it: an inheritance clause on a type, or any
    `extension` -- whose members are the extended type's, declared wherever
    that is. A `where Self: X` constraint, the other way to bring X's members
    in, is written only on an extension (or a method in one) among the
    declarations that hold code, so the extension test covers it; a `where`
    on a generic type constrains its parameters, not `Self`."""
    if node.type not in ("class_declaration", "protocol_declaration"):
        return False
    return any(c.type in ("extension", "inheritance_specifier") for c in node.children)


def _swift_adds_a_supertype(extension: Node, source_bytes: bytes) -> bool:
    """Whether an `extension` gives its type a supertype: an inheritance
    clause (`extension S: P {}`), or a `where Self: X` / `where Self == X`
    constraint. A `where T: Q` on a generic parameter adds nothing."""
    if any(c.type == "inheritance_specifier" for c in extension.children):
        return True
    for clause in (c for c in extension.named_children if c.type == "type_constraints"):
        for constraint in (c for c in clause.named_children if c.type == "type_constraint"):
            for inner in constraint.named_children:
                constrained = inner.child_by_field_name("constrained_type")
                if constrained is not None and _swift_name(constrained, source_bytes) == "Self":
                    return True
    return False


@dataclass(frozen=True)
class _SwiftSupertypes:
    """What one Swift file says about which of its types have a supertype."""

    #: Last names of the types an extension here gives a supertype.
    extended: frozenset[str]
    #: Names of the types declared here with an inheritance clause.
    clause: frozenset[str]
    #: An extension here adds a supertype to a name this file cannot tie to
    #: one of its own types by its full path, so it may extend any of them.
    anything: bool

    def has_one(self, name: str) -> bool:
        return self.anything or name in self.extended or name in self.clause


def _swift_supertypes(root: Node, source_bytes: bytes) -> _SwiftSupertypes:
    """Which types of THIS file may have a supertype.

    A type declared with no inheritance clause still gains the protocol's
    extension members when an extension adds the conformance, and they are
    in unqualified scope in the type's ORIGINAL body too.

    An extension is TIED to a type of this file only when its full written
    path is a nesting chain declared here: `extension S` needs a TOP-LEVEL
    `S` (a nested or function-local `S` is not visible at file scope), and
    `extension Keys.V` needs `V` declared in `Keys`'s body (or in an
    extension of `Keys`). A tied extension counts for its type, matched on
    the read's path by LAST name, which is coarser and can only withhold
    more. Any other extension (spelled through a typealias from this file or
    another, `extension T: P {}`, `extension NS.T: P {}`; module-qualified,
    `extension App.S: P {}`, since this file cannot tell a module name from
    a type's; or of an outside type, `extension UIViewController: P {}`)
    cannot be tied, so it counts for every type here. An extension of sugar
    (`[Int]`, `Int?`) names a standard-library type, never one declared
    here, so it is not counted."""
    chains: set[tuple[str, ...]] = set()
    clause: set[str] = set()
    adding: list[list[str]] = []
    for node in _walk(root):
        own = _swift_type_name(node, source_bytes)
        if own is not None:
            chain = _swift_type_chain(node, source_bytes)
            if chain is not None:
                chains.add(chain)
            if any(c.type == "inheritance_specifier" for c in node.children):
                clause.add(own)
            continue
        if node.type != "class_declaration" or not any(c.type == "extension" for c in node.children):
            continue
        if not _swift_adds_a_supertype(node, source_bytes):
            continue
        written = node.child_by_field_name("name")
        if written is None or written.type != "user_type":
            continue
        segments = [_swift_name(c, source_bytes) for c in written.named_children if c.type == "type_identifier"]
        if segments:
            adding.append(segments)
    aliases = {
        _swift_name(name, source_bytes)
        for node in _walk(root)
        if node.type == "typealias_declaration"
        for name in [node.child_by_field_name("name")]
        if name is not None
    }
    anything = any(
        tuple(segments) not in chains or any(s in aliases for s in segments) for segments in adding
    )
    return _SwiftSupertypes(
        frozenset(segments[-1] for segments in adding), frozenset(clause), anything
    )


def _swift_type_chain(node: Node, source_bytes: bytes) -> tuple[str, ...] | None:
    """The dotted path a type declared here is reachable by from file scope
    (`("Keys", "V")` for `struct V` in `enum Keys`, or in an `extension
    Keys`); None for a type declared inside a function or anything else
    that is not a type body."""
    own = _swift_type_name(node, source_bytes)
    if own is None:
        return None
    body = node.parent
    if body is None:
        return None
    if body.type == "source_file":
        return (own,)
    holder = body.parent
    if body.type not in ("class_body", "enum_class_body", "protocol_body") or holder is None:
        return None
    if any(c.type == "extension" for c in holder.children):
        written = holder.child_by_field_name("name")
        if written is None or written.type != "user_type" or holder.parent is None or holder.parent.type != "source_file":
            return None
        outer = tuple(_swift_name(c, source_bytes) for c in written.named_children if c.type == "type_identifier")
        return (*outer, own) if outer else None
    outer_chain = _swift_type_chain(holder, source_bytes)
    return None if outer_chain is None else (*outer_chain, own)


def _swift_type_name(node: Node, source_bytes: bytes) -> str | None:
    """The name a struct/class/enum/actor/protocol declaration introduces;
    None for an extension (or anything else)."""
    if node.type not in ("class_declaration", "protocol_declaration"):
        return None
    if any(c.type == "extension" for c in node.children):
        return None
    name = node.child_by_field_name("name")
    return None if name is None else _swift_name(name, source_bytes)


def _swift_is_static_member(member: Node, source_bytes: bytes) -> bool:
    """A `static` or `class` member: a type-level context. The grammar puts
    `class func`'s `class` straight under the declaration, not in its
    modifiers."""
    if any(not c.is_named and c.type in ("static", "class") for c in member.children):
        return True
    return any(
        m.type == "property_modifier" and _text(m, source_bytes) in ("static", "class")
        for mods in member.named_children if mods.type == "modifiers"
        for m in mods.named_children
    )


def _swift_withholds(root: Node, source_bytes: bytes, consts: list[KeyConst]) -> bool:
    """The Swift hazards the per-reference count cannot see.

    1. **A backtick.** The rules compare the name as WRITTEN, so a backticked
       declaration and a plain read (or the reverse) never match: the fold
       skips the read and the delete strands it.
    2. **A supertype's member.** Unqualified lookup tries every enclosing
       type's members, its superclass's and its protocols' extensions' before
       an outer scope. So a read below a type (or extension) that the
       declaration's own scope does not hold, and that inherits anything,
       may mean a member declared in another file or outside the checkout.
       The `enclosing_node` filter cannot see that member and would fold this
       flag's value over a different string. A type with no inheritance
       clause of its own counts too when an extension in this file gives it
       a supertype (`extension S: P {}`), or when one here extends a name
       this file cannot tie to its own types. One added in ANOTHER file
       stays invisible here.
    3. **An instance context in the holder.** A `static let` cannot be used
       from an instance method, initializer, accessor or computed property of
       its own type: that does not compile. So a bare read there that does
       compile means an instance member something else supplies (a
       supertype, a conformance added in another file, a macro), and it is
       withheld whatever this file says about the holder's supertypes.
    4. **A mention outside the declaration's scope node.** The fold rules
       reach only reads inside the node that holds the declaration (the file,
       a type body, a function's statements). A read elsewhere, such as in the
       type's own body when the constant is declared in an extension of it,
       would be left behind by the delete.
    """
    types: _SwiftSupertypes | None = None
    for const in consts:
        declared = const.name_nodes[0]
        if _text(declared, source_bytes) != const.name:
            return True
        scope = const.declaration.parent
        if scope is None:
            return True
        holder = scope.parent if scope.type != "source_file" else None
        if holder is not None and holder.type not in ("class_declaration", "protocol_declaration"):
            holder = None
        if holder is not None and not _swift_is_static_member(const.declaration, source_bytes):
            holder = None  # an instance constant: the holder's own member wins in an instance context
        for node in _walk(root):
            if node.id == declared.id or not _swift_is_reference(node, source_bytes, const.name):
                continue
            current = node.parent
            member: Node | None = None
            nested = False
            while current is not None and current.id != scope.id:
                if _swift_opens_a_supertype(current):
                    return True
                own = _swift_type_name(current, source_bytes)
                if own is not None:
                    nested = True
                    if types is None:
                        types = _swift_supertypes(root, source_bytes)
                    if types.has_one(own):
                        return True
                member = current
                current = current.parent
            if current is None:
                return True  # outside the scope the rules fold in: the delete would strand it
            if holder is None or nested or member is None:
                continue
            if not _swift_is_static_member(member, source_bytes):
                return True
    return False


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
    # A backticked key is never removed: the rules compare it as written.
    if identifier.text.startswith(b"`"):
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
    """First argument of a read the pre-pass marks (``boolVariation``, ``getBooleanValue``).

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
            withholds=_go_withholds,
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
            is_reference=_swift_is_reference,
            is_binding=_swift_is_binding,
            is_removable_read=_swift_is_removable_read,
            withholds=_swift_withholds,
        ),
        "kt": _Profile(
            key_consts=_kotlin_key_consts,
            is_reference=lambda n, b, name: _is_named_identifier(n, b, name, ("identifier",)),
            is_binding=_kotlin_is_binding,
            is_removable_read=_kotlin_is_removable_read,
        ),
        "python": _Profile(
            key_consts=_python_key_consts,
            # A string whose whole content is the name counts too: `__all__`
            # listing it, or `globals()["NAME"]`, reaches the constant by
            # name, so deleting it breaks `from m import *` or the lookup.
            # It is neither a binding nor a removable read, so the file is
            # withheld.
            is_reference=lambda n, b, name: _is_named_identifier(
                n, b, name, ("identifier", "string_content")
            ),
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
            withholds=_ruby_withholds,
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

# Blade the same way, over PHP: `.blade.php` takes `php_const.toml` through
# `rule_base`, and `syntax.const_path_is_safe` judges the view's code view
# (`blade_code.code_view`, the PHP it compiles to) under PHP's profile.
_PROFILES["blade"] = _PROFILES["php"]
