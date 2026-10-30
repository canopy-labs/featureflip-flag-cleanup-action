"""Where a C# ``typeof(X)`` value can go, for the constructor-parameter proof.

A ``typeof(X)`` anywhere lets reflection build ``X`` with the arity its
constructor had before the run (``t.GetConstructors()[0].Invoke(args)``,
``Activator.CreateInstance(t, args)``). It is harmless when nothing ever does
that with the value. :func:`escapes` follows every value derived from one
``typeof`` within its file and accepts only when each of them ends in a read
that cannot construct anything.

How a value travels:

* as an element of a collection expression or an array creation of a
  declared plain element type, which then holds it;
* through a ``private`` field of a class that is not ``partial`` (every
  ``F``/``x.F`` in the class), a local (every read after its declarator in
  its block, a whole ``switch`` block, or the file's top-level statements),
  a ``foreach`` variable, a lambda's parameter, and a ``private`` method of
  the same class that is not ``partial`` and has no base class (the
  parameter it is passed to; what it returns goes to every call, and every
  mention of the method must be a call or a method group handed to a
  sequence method below);
* through ``System.Linq`` methods that neither compare nor order: those that
  keep their elements (``Where``, ``Concat``, ``ToList``, ``First`` and the
  like), those that project (``Select``/``SelectMany``, which hold it only
  when the projection returns it), and those that test (``Any``, ``All``,
  ``Count``), whose result holds nothing; a lambda or method group passed to
  one receives the elements. Equality and ordering (``Contains``,
  ``Distinct``, ``OrderBy``, ``ToHashSet``, ``Union``) are left out: an
  element of the checkout's type could be handed the value by them;
* through ``GetMethods``/``GetProperties``/``GetMethod``/``GetProperty``
  (a name and binding flags at most), whose ``MethodInfo``s and
  ``PropertyInfo``s are followed the same way;
* through parentheses, ``!``, a cast or ``as`` to a plain type, and a
  ``?:`` whose other branch is another ``typeof`` or ``null``.

Every type it is declared, cast or converted to must be ``var`` or one of
:data:`_PLAIN_TYPES` under a ``System`` namespace, which the checkout may not
redeclare: a user-defined conversion would otherwise run with it.

Where it may end: a closed list of reads (:data:`_READS`: ``Name``,
``FullName``, ``Namespace``, ``IsSpecialName`` and other flags), the
attribute lookups (``GetCustomAttribute(s)``, ``IsDefined``), ``==``/``!=``
against ``null`` or another ``typeof``, and an ``is`` pattern that binds
nothing. Anything else refuses, with the line. A name read may turn back
into a type through a lookup by name, so the name reads refuse when any C#
file of the checkout calls ``GetType`` with an argument.

An attribute read off the value is not followed. It cannot be ``X``, and an
attribute that kept a ``typeof(X)`` of its own is that ``typeof``'s site,
which the constructor proof judges separately (``_compared_type``).

Reflection by name could still reach a field or helper the value goes
through and take it elsewhere. :func:`_reflected` refuses a ``typeof`` of the
class declaring them, and, in any file that can reach that class's
``Type``, a string or ``nameof`` naming one of them, a member enumeration or
an ``UnsafeAccessor``. An extension method of the checkout named like one of
the library calls made on the value (or ``GetEnumerator``, for a ``foreach``)
refuses too, and so does a C# 14 ``extension`` block or a file that does not
parse spelling one of those names: the call could bind to it.
"""

from __future__ import annotations

import os
import re

from tree_sitter import Node

from flag_cleanup import source_index

#: Members whose value holds nothing of the type they are read on.
_NAME_READS = frozenset({"Name", "FullName", "Namespace"})
_READS = _NAME_READS | frozenset(
    {
        "IsSpecialName", "IsPublic", "IsPrivate", "IsFamily", "IsAssembly", "IsStatic", "IsAbstract", "IsSealed",
        "IsVirtual", "IsClass", "IsInterface", "IsEnum", "IsValueType", "IsGenericType", "IsGenericMethod",
        "IsNested", "CanRead", "CanWrite",
    }
)
#: Calls whose result holds nothing of the value they are made on: its
#: attributes, or whether it has one.
_ATTRIBUTE_CALLS = frozenset({"GetCustomAttribute", "GetCustomAttributes", "IsDefined"})
#: Calls returning the type's own methods or properties (never a
#: constructor), followed as the value, by how many arguments they may take:
#: a name and binding flags, and no binder of the checkout's.
_MEMBER_LOOKUPS = {"GetMethods": 1, "GetProperties": 1, "GetMethod": 2, "GetProperty": 2}
#: Sequence methods whose result holds the elements they are called on, and
#: which never compare them.
_KEEPING = frozenset(
    {
        "Where", "Concat", "ToList", "ToArray", "AsEnumerable", "Skip", "Take", "Reverse", "First",
        "FirstOrDefault", "Single", "SingleOrDefault", "Last", "LastOrDefault",
    }
)
#: Sequence methods whose result holds what their projection returns.
_PROJECTING = frozenset({"Select", "SelectMany"})
#: Sequence methods whose result holds nothing of the elements.
_TESTING = frozenset({"Any", "All", "Count"})
#: A sequence method taking another sequence, whose result holds it.
_ARGUMENT_KEEPING = frozenset({"Concat"})
#: Sequence methods whose one argument may be a value or a sequence rather
#: than a delegate: nothing of the checkout's is handed the elements.
_VALUE_ARGUMENT = frozenset({"Concat", "Skip", "Take", "FirstOrDefault", "SingleOrDefault", "LastOrDefault"})
_LIBRARY_CALLS = _ATTRIBUTE_CALLS | _KEEPING | _PROJECTING | _TESTING | frozenset(_MEMBER_LOOKUPS)
_ACCESS = frozenset({"public", "protected", "internal"})
_TYPES = ("class_declaration", "struct_declaration", "record_declaration")
#: Declared types a value may be held in: no user-defined conversion can
#: run between them and a ``Type`` or ``MemberInfo``.
_PLAIN_TYPES = frozenset(
    {
        "Type", "MemberInfo", "MethodInfo", "PropertyInfo", "MethodBase", "IEnumerable", "IList", "List",
        "ICollection", "IReadOnlyList", "IReadOnlyCollection", "HashSet", "ISet", "IReadOnlySet", "Array", "Object",
    }
)
_SYSTEM_NAMESPACES = frozenset({"System", "System.Reflection", "System.Collections", "System.Collections.Generic"})
#: Pattern nodes that bind the value or read members of it.
_BINDING_PATTERNS = frozenset(
    {"declaration_pattern", "var_pattern", "recursive_pattern", "list_pattern", "positional_pattern",
     "property_pattern_clause", "parenthesized_variable_designation", "single_variable_designation"}
)
_TRANSPARENT = frozenset({"parenthesized_expression", "cast_expression"})
_STRINGS = frozenset({"string_literal", "verbatim_string_literal", "raw_string_literal"})


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _modifiers(node: Node) -> set[str]:
    return {_text(c) for c in node.children if c.type == "modifier"}


def _member_name(node: Node | None) -> str | None:
    """``M`` of ``M`` or ``M<T>``."""
    if node is None:
        return None
    if node.type == "generic_name":
        node = node.named_children[0] if node.named_children else None
    return _text(node) if node is not None and node.type == "identifier" else None


def _type_arguments(node: Node | None) -> list[Node]:
    if node is None or node.type != "generic_name":
        return []
    listed = next((c for c in node.named_children if c.type == "type_argument_list"), None)
    return list(listed.named_children) if listed is not None else []


def _enclosing(node: Node, kinds: tuple[str, ...]) -> Node | None:
    current = node.parent
    while current is not None and current.type not in kinds:
        current = current.parent
    return current


def _items(arguments: Node | None) -> list[Node]:
    return [c for c in (arguments.named_children if arguments is not None else ()) if c.type == "argument"]


def _root(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


class _Refused(Exception):
    pass


class _Flow:
    """One ``typeof``'s walk through its file."""

    def __init__(self, root: Node, repo_dir: str):
        self.root = root
        self.repo_dir = repo_dir
        #: Library calls it is an argument of, by receiver (``Concat``).
        self.argument_calls: list[tuple[str, Node | None]] = []
        self.seen: set[int] = set()
        self.bound: set[tuple[int, str]] = set()
        #: Helpers known to return the value.
        self.returning: set[int] = set()
        #: Names reflection must not reach (fields and helpers it went through).
        self.members: set[str] = set()
        #: The classes declaring those fields and helpers.
        self.owners: set[str] = set()
        #: Library calls made on it, which no extension method may shadow.
        self.calls: set[str] = set()
        #: Whether a name read could be turned back into a type.
        self.named = False

    def refuse(self, node: Node, why: str) -> None:
        raise _Refused(f"line {node.start_point[0] + 1}: {why}")

    # --- the value at a node -------------------------------------------------

    def held(self, node: Node) -> None:
        """``node`` is an expression holding the value: judge its context."""
        if node.id in self.seen:
            return
        self.seen.add(node.id)
        parent = node.parent
        if parent is None:
            self.refuse(node, "an unexpected position")
        kind = parent.type
        if kind in _TRANSPARENT or (kind == "postfix_unary_expression" and [c.type for c in parent.children][1:] == ["!"]):
            if kind == "cast_expression":
                if parent.child_by_field_name("value") != node:
                    self.refuse(node, "a cast's type")
                self.typed(parent.child_by_field_name("type"), node)
            return self.held(parent)
        if kind == "as_expression" and parent.child_by_field_name("left") == node:
            self.typed(parent.child_by_field_name("right"), node)
            return self.held(parent)
        if kind == "conditional_expression":
            if parent.child_by_field_name("condition") == node:
                self.refuse(node, "a condition")
            other = parent.child_by_field_name("alternative" if parent.child_by_field_name("consequence") == node else "consequence")
            if other is None or other.type not in ("typeof_expression", "null_literal"):
                self.refuse(node, "a ?: whose other branch may convert it")
            return self.held(parent)
        if kind == "member_access_expression" and parent.child_by_field_name("expression") == node:
            return self.member(node, parent)
        if kind == "argument":
            return self.argument(node, parent)
        if kind in ("expression_element", "spread_element"):
            element = parent.parent
            if element is not None and element.type == "collection_element":
                element = element.parent
            if element is None or element.type != "collection_expression":
                self.refuse(node, "an element of something other than a collection expression")
            return self.held(element)
        if kind == "collection_expression":
            return self.held(parent)
        if kind == "initializer_expression":
            creation = parent.parent
            if creation is None or creation.type != "array_creation_expression":
                self.refuse(node, "an initializer that is not an array creation of a written type")
            self.typed(creation.child_by_field_name("type"), node)
            return self.held(creation)
        if kind == "equals_value_clause" or (kind == "variable_declarator" and parent.child_by_field_name("name") != node):
            declarator = parent if kind == "variable_declarator" else parent.parent
            if declarator is None or declarator.type != "variable_declarator":
                self.refuse(node, "an unexpected initializer")
            return self.declared(node, declarator)
        if kind == "foreach_statement" and parent.child_by_field_name("right") == node:
            return self.loop(node, parent)
        if kind == "binary_expression":
            operator = next((c for c in parent.children if c.type in ("==", "!=")), None)
            if operator is None:
                self.refuse(node, f"an operand of {_text(next((c for c in parent.children if not c.is_named), parent))}")
            other = parent.child_by_field_name("right" if parent.child_by_field_name("left") == node else "left")
            if other is None or other.type not in ("null_literal", "typeof_expression"):
                self.refuse(node, "compared with something that may define the operator")
            return None
        if kind in ("is_pattern_expression", "is_expression") and parent.child_by_field_name("expression") == node:
            pattern = parent.child_by_field_name("pattern") or parent.child_by_field_name("type")
            if pattern is not None and any(n.type in _BINDING_PATTERNS for n in _walk(pattern)):
                self.refuse(node, "a pattern that binds or reads it")
            return None
        if kind == "lambda_expression" and parent.child_by_field_name("body") == node:
            return self.lambda_returns(parent)
        if kind == "arrow_expression_clause" or kind == "return_statement":
            return self.returned(node, parent)
        self.refuse(node, f"used in {kind}")

    # --- uses -----------------------------------------------------------------

    def member(self, node: Node, access: Node) -> None:
        name_node = access.child_by_field_name("name")
        name = _member_name(name_node)
        call = access.parent
        invoked = call is not None and call.type == "invocation_expression" and call.child_by_field_name("function") == access
        if not invoked:
            if name in _READS and not (
                call is not None and call.type == "assignment_expression" and call.child_by_field_name("left") == access
            ):
                self.named = self.named or name in _NAME_READS
                return None
            self.refuse(node, f"reads {name}")
        for argument in _type_arguments(name_node):
            if name not in _ATTRIBUTE_CALLS:
                self.typed(argument, node)
        arguments = call.child_by_field_name("arguments")
        if name in _ATTRIBUTE_CALLS:
            self.calls.add(name)
            return None
        if name in _MEMBER_LOOKUPS:
            self.calls.add(name)
            if len(_items(arguments)) > _MEMBER_LOOKUPS[name]:
                self.refuse(node, f"{name} with a binder, types or modifiers")
            return self.held(call)
        if name in _KEEPING | _PROJECTING | _TESTING:
            self.calls.add(name)
            items = _items(arguments)
            if len(items) > 1:
                self.refuse(node, f"{name} with more than one argument (a comparer or selector of yours may receive it)")
            for argument in items:
                value = argument.named_children[-1] if argument.named_children else None
                if value is None or argument.child_by_field_name("name") is not None:
                    self.refuse(node, f"{name} with a named argument")
                if value.type == "lambda_expression":
                    self.lambda_receives(value)
                elif value.type == "identifier" and _declares_method(self.root, _text(value)):
                    self.group_receives(value, name, call)
                elif name not in _VALUE_ARGUMENT:
                    self.refuse(node, f"{name} given {_text(value)}, which may be a comparer or selector of yours")
            if name in _KEEPING:
                return self.held(call)
            return None  # a projection holds it only if it returns it (lambda_returns, returned)
        self.refuse(node, f"calls {name}")

    def argument(self, node: Node, argument: Node) -> None:
        if argument.child_by_field_name("name") is not None or any(c.type in ("ref", "out", "in") for c in argument.children):
            self.refuse(node, "a named or by-reference argument")
        arguments = argument.parent
        call = arguments.parent if arguments is not None else None
        if call is None or call.type != "invocation_expression":
            self.refuse(node, "an argument of something other than a call")
        function = call.child_by_field_name("function")
        if function is not None and function.type == "member_access_expression":
            name = _member_name(function.child_by_field_name("name"))
            receiver = function.child_by_field_name("expression")
            if name in _ARGUMENT_KEEPING and len(_items(arguments)) == 1:
                self.argument_calls.append((name, receiver))
                return self.held(call)
            holder = _enclosing(function, _TYPES)
            own = holder is not None and receiver is not None and (
                receiver.type in ("this", "this_expression")
                or (receiver.type == "identifier" and _text(receiver) == _text(holder.child_by_field_name("name")))
            )
            if own:
                helper = self.helper(function, name, bare=False)
                if helper is not None:
                    return self.passed(node, argument, helper, call)
            self.refuse(node, f"passed to {name}")
        helper = self.helper(function, _member_name(function), bare=True)
        if helper is None:
            self.refuse(node, f"passed to {_text(function) if function is not None else 'a call'}")
        return self.passed(node, argument, helper, call)

    def passed(self, node: Node, argument: Node, helper: Node, call: Node) -> None:
        items = _items(argument.parent)
        index = next(i for i, a in enumerate(items) if a.id == argument.id)
        parameters = _parameters(helper)
        if len(items) != len(parameters):
            self.refuse(node, "a call whose arity differs from the helper's (another overload or an extension may bind)")
        name = parameters[index].child_by_field_name("name")
        self.typed(parameters[index].child_by_field_name("type"), node)
        self.reads(helper, _text(name), helper.child_by_field_name("body"))

    # --- bindings -------------------------------------------------------------

    def declared(self, node: Node, declarator: Node) -> None:
        declaration = declarator.parent
        owner = declaration.parent if declaration is not None else None
        name = declarator.child_by_field_name("name")
        if declaration is None or declaration.type != "variable_declaration" or owner is None or name is None:
            self.refuse(node, "an unexpected declaration")
        declared_type = declaration.child_by_field_name("type")
        if declared_type is not None and _text(declared_type) == "dynamic":
            self.refuse(node, "a dynamic local")
        self.typed(declared_type, node)
        if owner.type == "local_declaration_statement":
            if any(c.type in ("using", "await") for c in owner.children):
                self.refuse(node, "a using declaration")
            scope = owner.parent
            while scope is not None and scope.type in ("global_statement", "switch_section"):
                scope = scope.parent  # top-level statements share the file; a switch block shares its sections
            if scope is None:
                self.refuse(node, "a local with no scope")
            return self.reads(scope, _text(name), scope, after=declarator.end_byte)
        if owner.type == "field_declaration":
            if _modifiers(owner) & _ACCESS:
                self.refuse(node, f"{_text(name)} is visible outside its class")
            holder = _enclosing(owner, _TYPES)
            if holder is None or "partial" in _modifiers(holder):
                self.refuse(node, f"{_text(name)} is a field of a partial type")
            self.members.add(_text(name))
            self.owners.add(_text(holder.child_by_field_name("name")))
            return self.reads(holder, _text(name), holder.child_by_field_name("body"), field=True)
        self.refuse(node, f"declared in {owner.type}")

    def loop(self, node: Node, statement: Node) -> None:
        left = statement.child_by_field_name("left")
        if left is None or left.type != "identifier":
            self.refuse(node, "a foreach that deconstructs")
        declared_type = statement.child_by_field_name("type")
        if declared_type is not None and _text(declared_type) == "dynamic":
            self.refuse(node, "a dynamic loop variable")
        if any(c.type == "await" for c in statement.children):
            self.refuse(node, "an await foreach")
        self.typed(declared_type, node)
        self.calls.add("GetEnumerator")  # an extension could enumerate it
        self.reads(statement, _text(left), statement.child_by_field_name("body"))

    def lambda_receives(self, lambda_: Node) -> None:
        parameters = lambda_.child_by_field_name("parameters")
        if parameters is None:
            self.refuse(lambda_, "a lambda without parameters")
        if parameters.type == "implicit_parameter":
            name = _text(parameters)
        else:
            listed = [c for c in parameters.named_children if c.type == "parameter"]
            if len(listed) != 1:
                self.refuse(lambda_, "a lambda that does not take one parameter")
            first = listed[0]
            if first.child_by_field_name("type") is not None and _text(first.child_by_field_name("type")) == "dynamic":
                self.refuse(lambda_, "a dynamic lambda parameter")
            self.typed(first.child_by_field_name("type"), lambda_)
            name = _text(first.child_by_field_name("name"))
        self.reads(lambda_, name, lambda_.child_by_field_name("body"))

    def group_receives(self, group: Node, sequence: str, call: Node) -> None:
        helper = self.helper(group, _text(group), bare=True)
        if helper is None:
            self.refuse(group, f"a method group {_text(group)} that is not a private method of this class")
        parameters = _parameters(helper)
        if len(parameters) != 1:
            self.refuse(group, f"{_text(group)} does not take exactly one parameter")
        self.typed(parameters[0].child_by_field_name("type"), group)
        self.reads(helper, _text(parameters[0].child_by_field_name("name")), helper.child_by_field_name("body"))

    def reads(self, scope: Node, name: str, body: Node | None, after: int = -1, field: bool = False) -> None:
        """Every read of ``name`` in ``body`` holds the value. For a
        ``field``, ``x.name`` on any receiver counts too: the field is
        private, so only its class's text reaches it, through whatever
        spelling of the class."""
        if body is None:
            self.refuse(scope, f"{name} has no body to follow")
        key = (scope.id, name)
        if key in self.bound:
            return
        self.bound.add(key)
        for node in _walk(body):
            if node.type != "identifier" or _text(node).removeprefix("@") != name or node.start_byte < after:
                continue
            parent = node.parent
            if parent is not None and parent.type == "member_access_expression" and parent.child_by_field_name("name") == node:
                receiver = parent.child_by_field_name("expression")
                if not field and (receiver is None or receiver.type not in ("this", "this_expression")):
                    continue  # another value's member of the name
                node = parent
                parent = node.parent
            if parent is not None and parent.type == "argument" and parent.child_by_field_name("name") == node:
                continue  # `name: …` names a parameter
            if parent is not None and parent.type in ("variable_declarator", "parameter") and parent.child_by_field_name("name") == node:
                continue  # a declaration of the name
            if parent is not None and parent.type == "argument" and _nameof(parent):
                continue  # judged by _reflected
            if parent is not None and parent.type == "assignment_expression" and parent.child_by_field_name("left") == node:
                continue  # written, not read: what it is given is judged where it comes from
            self.held(node)

    def typed(self, type_node: Node | None, at: Node) -> None:
        """Refuse a declared type a user-defined conversion could run code
        for: anything but ``var`` and the reflection and collection types of
        :data:`_PLAIN_TYPES` (under ``System`` namespaces), none of which the
        checkout may declare a type named like."""
        if type_node is None or type_node.type == "implicit_type":
            return
        from flag_cleanup import cross_file_csharp as xcs

        declared = xcs.csharp_index(self.repo_dir).by_last
        skip: set[int] = set()
        for n in _walk(type_node):
            if n.id in skip:
                continue
            if n.type == "qualified_name":
                qualifier = n.child_by_field_name("qualifier")
                if qualifier is not None:
                    if _text(qualifier).removeprefix("global::") not in _SYSTEM_NAMESPACES:
                        self.refuse(at, f"declared as {_text(type_node)}")
                    skip.update(m.id for m in _walk(qualifier))
            elif n.type == "identifier":
                if _text(n) not in _PLAIN_TYPES or declared.get(_text(n)):
                    self.refuse(at, f"declared as {_text(type_node)}")
            elif n.type == "predefined_type" and _text(n) != "object":
                self.refuse(at, f"declared as {_text(type_node)}")
            elif n.type in ("alias_qualified_name", "pointer_type", "function_pointer_type", "tuple_type", "ref_type"):
                self.refuse(at, f"declared as {_text(type_node)}")

    def plainly_typed(self, receiver: Node) -> bool:
        """Whether ``receiver`` names the one variable or field of its name
        in the file, declared with a type :meth:`typed` accepts (not
        ``var``)."""
        if receiver.type != "identifier":
            return False
        name = _text(receiver).removeprefix("@")
        found = [
            n for n in _walk(self.root)
            if n.type in ("variable_declarator", "parameter", "foreach_statement")
            and (k := n.child_by_field_name("name" if n.type != "foreach_statement" else "left")) is not None
            and _text(k) == name
        ]
        if len(found) != 1 or _declares_method(self.root, name):
            return False
        holder = found[0]
        type_node = (
            holder.parent.child_by_field_name("type") if holder.type == "variable_declarator" and holder.parent is not None
            else holder.child_by_field_name("type")
        )
        if type_node is None or type_node.type == "implicit_type":
            return False
        try:
            self.typed(type_node, receiver)
        except _Refused:
            return False
        return True

    # --- helpers --------------------------------------------------------------

    def helper(self, function: Node | None, name: str | None, bare: bool) -> Node | None:
        """The private method of the enclosing class ``name`` names, when it
        is one this follows: the only member of its name, not generic, no
        by-ref, ``params`` or optional parameter, in a class that is not
        ``partial`` and has no base list (an inherited overload could bind).
        A ``bare`` mention must not also name a value or local function of
        the file, which would shadow it."""
        if function is None or name is None:
            return None
        holder = _enclosing(function, _TYPES)
        if holder is None or "partial" in _modifiers(holder) or any(c.type == "base_list" for c in holder.children):
            return None
        if bare and (_declares_value(self.root, name) or _declares_local_function(self.root, name)):
            return None
        body = holder.child_by_field_name("body")
        members = [
            m for m in (body.named_children if body is not None else ())
            if (n := m.child_by_field_name("name")) is not None and _text(n) == name
        ]
        if len(members) != 1 or members[0].type != "method_declaration":
            return None
        method = members[0]
        modifiers = _modifiers(method)
        if modifiers & (_ACCESS | {"extern", "partial", "abstract", "virtual", "override", "new", "async"}):
            return None
        if any(c.type in ("type_parameter_list", "attribute_list") for c in method.children):
            return None
        parameters = method.child_by_field_name("parameters")
        if parameters is None or any(
            c.type != "parameter"
            or any(m.type == "modifier" for m in c.children)
            or any(m.type == "equals_value_clause" for m in c.children)
            for c in parameters.named_children if c.type != "comment"
        ):
            return None
        if any(n.type == "yield_statement" for n in _walk(method)):
            return None  # an iterator runs later, and its state machine is not followed
        self.members.add(name)
        self.owners.add(_text(holder.child_by_field_name("name")))
        return method

    def returned(self, node: Node, at: Node) -> None:
        function = _enclosing(node, ("lambda_expression", "method_declaration", "local_function_statement",
                                     "property_declaration", "accessor_declaration", "constructor_declaration",
                                     "anonymous_method_expression"))
        if function is not None and function.type == "lambda_expression":
            return self.lambda_returns(function)
        if function is None or function.type != "method_declaration":
            self.refuse(node, f"returned from {function.type if function is not None else 'nothing'}")
        name = function.child_by_field_name("name")
        helper = self.helper(function, _text(name) if name is not None else None, bare=False)
        if helper is None or helper.id != function.id:
            self.refuse(node, f"returned from {_text(name) if name is not None else 'a method'}, which is not a private method this follows")
        if function.id in self.returning:
            return None
        self.returning.add(function.id)
        self.typed(function.child_by_field_name("returns"), node)
        holder = _enclosing(function, _TYPES)
        method_name = _text(name)
        if _declares_value(self.root, method_name) or _declares_local_function(self.root, method_name):
            self.refuse(node, f"{method_name} is also the name of a value or local function")
        for mention in _walk(holder):
            if mention.type != "identifier" or _text(mention).removeprefix("@") != method_name or mention == name:
                continue
            parent = mention.parent
            target = mention
            if parent is not None and parent.type == "member_access_expression" and parent.child_by_field_name("name") == mention:
                receiver = parent.child_by_field_name("expression")
                if receiver is None or receiver.type not in ("this", "this_expression") and not (
                    receiver.type == "identifier" and _text(receiver) == _text(holder.child_by_field_name("name"))
                ):
                    self.refuse(mention, f"{method_name} is reached through another receiver")
                target = parent
                parent = parent.parent
            if parent is not None and parent.type == "invocation_expression" and parent.child_by_field_name("function") == target:
                if len(_items(parent.child_by_field_name("arguments"))) != len(_parameters(function)):
                    self.refuse(mention, f"a call of {method_name} whose arity differs (another overload may bind)")
                self.held(parent)
                continue
            if parent is not None and parent.type == "argument" and _nameof(parent):
                continue
            if parent is not None and parent.type == "argument":
                call = parent.parent.parent if parent.parent is not None else None
                function_of = call.child_by_field_name("function") if call is not None else None
                sequence = _member_name(function_of.child_by_field_name("name")) if function_of is not None and function_of.type == "member_access_expression" else None
                if sequence in _PROJECTING:
                    self.held(call)
                    continue
                if sequence in _KEEPING | _TESTING:
                    continue  # a predicate: its result is not the sequence's
            self.refuse(mention, f"{method_name} is used other than called")

    def lambda_returns(self, lambda_: Node) -> None:
        """The lambda's result holds the value: what it was passed to holds
        it when that projects."""
        argument = lambda_.parent
        call = argument.parent.parent if argument is not None and argument.type == "argument" and argument.parent is not None else None
        function = call.child_by_field_name("function") if call is not None and call.type == "invocation_expression" else None
        name = _member_name(function.child_by_field_name("name")) if function is not None and function.type == "member_access_expression" else None
        if name in _PROJECTING:
            return self.held(call)
        if name in _KEEPING | _TESTING:
            return None  # a predicate
        self.refuse(lambda_, "a lambda returning it to something this does not follow")


def _parameters(method: Node) -> list[Node]:
    listed = method.child_by_field_name("parameters")
    return [c for c in (listed.named_children if listed is not None else ()) if c.type == "parameter"]


def _declares_method(root: Node, name: str) -> bool:
    return any(
        n.type in ("method_declaration", "local_function_statement")
        and (k := n.child_by_field_name("name")) is not None and _text(k) == name
        for n in _walk(root)
    )


def _declares_local_function(root: Node, name: str) -> bool:
    return any(
        n.type == "local_function_statement" and (k := n.child_by_field_name("name")) is not None and _text(k) == name
        for n in _walk(root)
    )


def _declares_value(root: Node, name: str) -> bool:
    """Whether the file declares a local, parameter, loop variable, field or
    lambda parameter named ``name``."""
    for node in _walk(root):
        if node.type in ("variable_declarator", "parameter"):
            k = node.child_by_field_name("name")
            if k is not None and _text(k) == name:
                return True
        elif node.type == "foreach_statement":
            k = node.child_by_field_name("left")
            if k is not None and _text(k) == name:
                return True
        elif node.type == "implicit_parameter" and _text(node) == name:
            return True
    return False


def _nameof(argument: Node) -> bool:
    call = argument.parent.parent if argument.parent is not None else None
    function = call.child_by_field_name("function") if call is not None and call.type == "invocation_expression" else None
    return function is not None and _text(function) == "nameof"


# --- whole-checkout checks ---------------------------------------------------------

#: (resolved path, mtime_ns, size) -> (text or None, root or None).
_PARSED: dict[tuple[str, int, int], tuple[str | None, Node | None]] = {}


def _files(repo_dir: str, suffixes: set[str]):
    """``(path, resolved, text, root)`` of the checkout's files of
    ``suffixes``; text is None when unreadable, root when not C#."""
    from flag_cleanup import wrapper_csharp as wc

    for f in source_index.files(repo_dir, suffixes, with_ignored=True):
        try:
            stat = os.stat(f.resolved)
        except OSError:
            yield f.path, f.resolved, None, None
            continue
        key = (f.resolved, stat.st_mtime_ns, stat.st_size)
        if key not in _PARSED:
            text = wc._read(f.resolved)
            root = wc.parse(text) if text is not None and f.path.name.endswith(".cs") else None
            _PARSED[key] = (text, root)
        text, root = _PARSED[key]
        yield f.path, f.resolved, text, root


#: What reaches a class's ``Type`` in its own file without spelling it.
_SELF_TYPE = re.compile(r"(?:^|[^\w.])(?:this\s*\.\s*)?GetType\s*\(\s*\)")
#: An assembly scan, which reaches every class's ``Type``. Loading an
#: assembly or a type by name is ``ctor_param_csharp._LOADS``.
_SCAN = re.compile(r"\b(?:GetTypes|DefinedTypes|ExportedTypes|GetExportedTypes|GetModules|GetLoadedModules)\b")
#: Reflection that reaches members without spelling their names.
_MEMBER_REFLECTION = re.compile(
    r"\b(?:GetFields|GetRuntimeFields|DeclaredFields|GetMembers|GetRuntimeMethods|DeclaredMethods|DeclaredMembers"
    r"|FindMembers|GetMethods|GetProperties|UnsafeAccessor|GetField|GetMember|GetMethod|GetProperty|InvokeMember)\b"
)
#: A type looked up by its name.
_TYPE_BY_NAME = re.compile(r"\bGetType\s*\(\s*[^)\s]")


def _reflected(repo_dir: str, names: set[str], owners: set[str], owner_path: str | None) -> str | None:
    """Why reflection may reach a field or helper the value went through and
    take it elsewhere: a ``typeof`` of a class declaring them anywhere, a
    Razor file or a file that does not parse spelling one, or, in a file that
    may reach such a class's ``Type`` (another file spelling the class, the
    class's own file calling ``GetType()`` on itself, or an assembly scan), a
    string or ``nameof`` naming one of them, or reflection enumerating or
    looking up members (:data:`_MEMBER_REFLECTION`). A name computed at run
    time from parts in a file that cannot reach the ``Type`` is not seen
    (README limit)."""
    if not names:
        return None
    word = re.compile(r"(?<![\w@])(?:" + "|".join(re.escape(n) for n in sorted(names | owners)) + r")(?![\w])")
    owner_word = re.compile(r"(?<![\w@])(?:" + "|".join(re.escape(o) for o in sorted(owners)) + r")(?![\w])")
    typeof = re.compile(r"\btypeof\s*\(\s*(?:global::)?[\w.]*\b(?:" + "|".join(re.escape(o) for o in sorted(owners)) + r")\s*[<)]")
    from flag_cleanup import csharp_family
    from flag_cleanup.ctor_param_csharp import _LOADS

    for path, resolved, text, root in _files(repo_dir, {".cs", ".cshtml", ".razor"}):
        if text is None:
            return f"{path} could not be read"
        if word.search(text) is None and _SCAN.search(text) is None and _LOADS.search(text) is None:
            continue
        own = owner_path is not None and resolved == owner_path
        if not path.name.endswith(".cs"):
            if word.search(text) is not None:
                return f"{path} spells {word.search(text).group(0)}"
            continue
        if (m := typeof.search(text)) is not None:
            return f"{path}: {m.group(0).rstrip('<')} (reflection may reach what it declares)"
        if own and root is not None:
            handed = next(
                (n for n in _walk(root) if n.type in ("this", "this_expression") and n.parent is not None
                 and not (n.parent.type == "member_access_expression" and n.parent.child_by_field_name("expression") == n)),
                None,
            )
            if handed is not None:
                return f"{path}:{handed.start_point[0] + 1}: the class hands out this (another file may reflect over it)"
        # An assembly scan reaches the class only from a project that can
        # see it, or by loading assemblies at run time.
        scans = _LOADS.search(text) is not None or (_SCAN.search(text) is not None and (
            owner_path is None
            or (csharp_family.sees(repo_dir, resolved, owner_path) and not _scans_elsewhere(text, root, owner_path, repo_dir))
        ))
        reaches = scans or (_SELF_TYPE.search(text) is not None if own else owner_word.search(text) is not None)
        if not reaches:
            continue
        if root is None or root.has_error:
            return f"{path} does not parse"
        if (m := _MEMBER_REFLECTION.search(text)) is not None and not own:
            return f"{path}: {m.group(0)} in a file that can reach the class's type"
        if own and _SELF_TYPE.search(text) is not None and (m := _MEMBER_REFLECTION.search(text)) is not None:
            return f"{path}: {m.group(0)} in the class's own file, which reaches its own type"
        for n in _walk(root):
            if n.type in _STRINGS or n.type == "interpolated_string_expression":
                content = "".join(_text(c) for c in n.named_children if c.type.endswith("content"))
                if content in names:
                    return f"{path}:{n.start_point[0] + 1}: the string \"{content}\" (reflection may reach it by name)"
            elif n.type == "argument" and _nameof(n) and word.search(_text(n)) is not None:
                return f"{path}:{n.start_point[0] + 1}: {_text(n.parent.parent)} (reflection may reach it by name)"
    return None


_SCANS = frozenset({"GetTypes", "DefinedTypes", "ExportedTypes", "GetExportedTypes"})


def _scans_elsewhere(text: str, root: Node | None, owner_path: str, repo_dir: str) -> bool:
    """Whether every assembly the file scans is ``typeof(T).Assembly`` for a
    ``T`` the checkout declares only in projects that do not hold
    ``owner_path``: none of those assemblies holds the class."""
    from flag_cleanup import csharp_family
    from flag_cleanup import cross_file_csharp as xcs

    if root is None or root.has_error:
        return False
    projects = xcs._project_dirs(repo_dir)
    homes = set(csharp_family._enclosing(owner_path, projects, repo_dir))
    if not homes:
        return False
    index = xcs.csharp_index(repo_dir)
    checked = 0
    for node in _walk(root):
        if node.type != "member_access_expression" or _member_name(node.child_by_field_name("name")) not in _SCANS:
            continue
        assembly = node.child_by_field_name("expression")
        if assembly is None or assembly.type != "member_access_expression" or _member_name(assembly.child_by_field_name("name")) != "Assembly":
            return False
        typeof = assembly.child_by_field_name("expression")
        if typeof is None or typeof.type != "typeof_expression" or not typeof.named_children:
            return False
        spelled = typeof.named_children[0]
        last = _member_name(spelled.child_by_field_name("name") if spelled.type == "qualified_name" else spelled)
        infos = index.by_last.get(last or "", ())
        if not infos or any(homes & set(csharp_family._enclosing(str(i.path), projects, repo_dir)) for i in infos):
            return False
        checked += 1
    return checked > 0


def _shadowed(repo_dir: str, names: frozenset[str]) -> str | None:
    """A C# 14 ``extension`` block, or a file that does not parse, spelling
    one of ``names``: an extension member there could bind a call (or a
    property read) on the value."""
    if not names:
        return None
    word = re.compile(r"(?<![\w@])(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")(?![\w])")
    block = re.compile(r"(?<![\w@.])extension\s*[<(]")
    this_parameter = re.compile(r"\(\s*this\s+[\w@]")
    for path, _, text, root in _files(repo_dir, {".cs"}):
        if text is None:
            return f"{path} could not be read"
        if word.search(text) is None:
            continue
        if block.search(text) is not None:
            return f"{path}: an extension block may declare {word.search(text).group(0)}"
        if (root is None or root.has_error) and this_parameter.search(text) is not None:
            return f"{path} does not parse, declares an extension method and spells {word.search(text).group(0)}"
    return None


def _names_types(repo_dir: str) -> str | None:
    for path, _, text, _ in _files(repo_dir, {".cs"}):
        if text is None:
            return f"{path} could not be read"
        if (m := _TYPE_BY_NAME.search(text)) is not None:
            return f"{path}: {m.group(0)}…"
    return None


def escapes(node: Node, repo_dir: str, path: str | None = None) -> str | None:
    """``None`` when every value derived from the ``typeof_expression``
    ``node`` (in the file at ``path``) ends in a read that constructs
    nothing; else why not."""
    from flag_cleanup import csharp_residue

    flow = _Flow(_root(node), repo_dir)
    try:
        flow.held(node)
    except _Refused as refused:
        return str(refused)
    owner_path = os.path.realpath(path) if path is not None else None
    reason = _reflected(repo_dir, flow.members, flow.owners, owner_path)
    if reason is not None:
        return reason
    if flow.named:
        lookup = _names_types(repo_dir)
        if lookup is not None:
            return f"its name is read, and {lookup} may turn a name back into a type"
    # On a receiver that is itself the value (a reflection type or a
    # library collection of them, by the declared types allowed), only an
    # extension method can take the call; on any other receiver, whose type
    # is not known, any method of the name can.
    known = {
        name for name, receiver in flow.argument_calls
        if receiver is not None and (receiver.id in flow.seen or flow.plainly_typed(receiver))
    }
    unknown = {name for name, _ in flow.argument_calls} - known
    used = frozenset(flow.calls | known)
    shadow = csharp_residue._repository_methods(repo_dir, used, extensions_only=True)
    if shadow is not None:
        where, line, what = shadow
        return f"{where}:{line}: an extension method {what} may be what a call on it binds to"
    shadow = csharp_residue._repository_methods(repo_dir, frozenset(unknown))
    if shadow is not None:
        where, line, what = shadow
        return f"{where}:{line}: a method {what} may be what it is passed to"
    reason = _shadowed(repo_dir, used | frozenset(unknown) | _READS)
    if reason is not None:
        return reason
    return None
