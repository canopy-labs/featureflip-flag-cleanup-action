"""Sort the files that still mention a flag once its removal is on disk.

``orchestrate.all_unprocessed`` answers "does this pull request remove every
mention of the flag?" with a list of files. That list mixes things a reviewer
should treat very differently, and the pull request used to print it as one
caveat:

* a TEST THAT STUBS THE READ this pull request removed. Its setup is now dead,
  and an assertion beside it about what the flag returned can start failing —
  the most urgent entry, because it can turn the pull request red;
* a file that names the flag ONLY IN COMMENTS — prose to update, nothing that
  can break;
* a JavaScript-family file that names it only in TEST TITLES (and comments):
  prose as well — ``it('renders the page when the flag is on')`` is a string
  no code reads as a key — but prose in front of every run of the suite, so
  the pull request lists it apart from the comments;
* a file that names the key only as a URL PATH SEGMENT in a string
  (``/environments/${id}/scheduled-changes``): an endpoint named after the
  feature, which is common and is no flag read, so it is listed apart;
* a file that names the key only as a SEGMENT OF A PACKAGE, NAMESPACE OR
  IMPORT PATH (``package com.example.qos``, ``import com.example.qos.Policy``,
  ``namespace App\\qos;``): code organised under a package named after the
  feature, which no flag read can be, so it is listed apart;
* a TypeScript-family file that names it only as a STRING VALUE IN A
  LITERAL, an element of an array that also holds something computed
  (``['scheduled-changes', orgId] as const``, a query cache key): a value
  named after the feature, listed apart for the same reason;
* a file whose only mention of the key is an ENUM MEMBER's value (or, in
  TypeScript, a property of an ``as const`` object, which the run reads as
  one): resolved for some languages and forms, so the pull request says which;
  otherwise it would sit in the generic caveat with no explanation;
* everything else, which keeps the original, generic caveat.

Both verdicts are POSITIVE ones, and a file this module cannot parse, or whose
extension it does not know, is simply left in the generic group. That is the
safe direction: the generic caveat already asks the reviewer to look.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from tree_sitter import Node

from flag_cleanup import cross_file_csharp, cross_file_ts, generated_files, mock_stubs, rule_synthesis, syntax, ts_tests, unused

#: Extension to the grammar this module parses it with. ERB is absent: its
#: Ruby lives inside a template, and a comment verdict there would need the
#: template's own code view. It stays in the generic group.
_GRAMMAR_BY_EXTENSION = {
    **{ext: "ts" for ext in (".ts", ".mts", ".cts")},
    ".tsx": "tsx",
    **{ext: "js" for ext in (".js", ".jsx", ".mjs", ".cjs")},
    ".java": "java",
    ".go": "go",
    ".py": "python",
    ".kt": "kt",
    ".kts": "kt",
    ".cs": "csharp",
    **{ext: "php" for ext in (".php", ".phtml", ".inc", ".module")},
    ".rb": "ruby",
    ".rake": "ruby",
    ".dart": "dart",
    ".swift": "swift",
}


def _grammar_by_extension(path: str) -> str | None:
    """The grammar for ``path``, or None. A Blade view (``.blade.php``) is absent
    for ERB's reason: to the PHP grammar it is one text node, so it stays in the
    generic group."""
    if path.endswith(".blade.php"):
        return None
    return _GRAMMAR_BY_EXTENSION.get(os.path.splitext(path)[1])


@dataclass(frozen=True)
class Residue:
    #: ``(path, statement)`` for every C# statement still describing a read
    #: of the flag to a mock. See :func:`flag_cleanup.mock_stubs.mock_descriptions`.
    stubbed: tuple[tuple[str, str], ...] = ()
    #: Files every mention of the flag in which sits inside a comment.
    comment_only: tuple[str, ...] = ()
    #: ``(path, member)`` for every file whose every live mention of the flag
    #: is an enum member's value: the member's first line. Resolved for some
    #: languages and some forms only, so the pull request says which.
    enum_held: tuple[tuple[str, str], ...] = ()
    #: TypeScript-family files every mention of the flag in which sits in a
    #: test's title or a comment, with at least one in a title.
    title_only: tuple[str, ...] = ()
    #: ``(path, line, text)`` for each comment line naming the flag in a
    #: ``comment_only`` file: the prose the run kept because it says
    #: something besides the flag (see :mod:`flag_cleanup.stale_comments`).
    comment_lines: tuple[tuple[str, int, str], ...] = ()
    #: Files every live mention of the flag in which is a URL path segment in
    #: a string (and the rest comments or test titles); see :func:`_path_only`.
    path_only: tuple[str, ...] = ()
    #: Files outside the run's ``directories`` that also name the flag,
    #: relative to the checkout root. Never examined and never edited; see
    #: :func:`flag_cleanup.orchestrate.outside_mentions`.
    outside: tuple[str, ...] = ()
    #: TypeScript-family files every live mention of the flag in which is a
    #: whole string element of an array literal beside a non-literal element
    #: (a query cache key), and the rest comments or test titles; see
    #: :func:`_literal_only`.
    literal_only: tuple[str, ...] = ()
    #: Files every mention of the flag in which outside comments is a segment
    #: of a package, namespace or import path; see :func:`_namespace_only`.
    namespace_only: tuple[str, ...] = ()
    #: C# files every mention of the flag in which outside comments is a
    #: plain string argument of a call that is no flag read; see
    #: :func:`_plain_string_lines`.
    plain_strings: tuple[str, ...] = ()
    #: ``(path, line, text)`` for each line of a ``plain_strings`` file that
    #: names the key, comments included, so the reviewer need not search.
    plain_string_lines: tuple[tuple[str, int, str], ...] = ()
    #: ``(path, statement)`` for every test line that stubs or verifies a read
    #: callee this run removed a call of with a WILDCARD key (``any()``,
    #: ``It.IsAny<string>()``): it may stub the removed flag, and no key says
    #: so. See :mod:`flag_cleanup.wildcard_stubs`.
    wildcard_stubbed: tuple[tuple[str, str], ...] = ()
    #: Generated files every mention of the flag in which sits inside a
    #: comment. Their comments are not in ``comment_only`` or
    #: ``comment_lines``: rewording a generated file by hand is wrong, so the
    #: pull request names each as generated instead. See
    #: :mod:`flag_cleanup.generated_files`.
    generated_comment_only: tuple[str, ...] = ()
    #: ``(path, line, text)`` for each line of the checkout, relative to its
    #: root, that spells a refusal message the run deleted: code that still
    #: handles a response the removal means the server can no longer send.
    #: See :mod:`flag_cleanup.removed_refusals`.
    refusals: tuple[tuple[str, int, str], ...] = ()


def _mention(flag_key: str) -> re.Pattern[str]:
    """The key as a whole token: not inside a longer key or identifier. A
    full stop after it ends a sentence; one with a word after it is a
    member read."""
    return re.compile(rf"(?<![\w.-]){re.escape(flag_key)}(?![\w-])(?!\.\w)")


def _comment_only(
    root: Node, mention: re.Pattern[str], inert: frozenset[int] = frozenset()
) -> bool:
    """Whether every mention sits in a comment, or in a node ``inert`` names.

    At least one has to be found there: a file whose mention matched no node
    at all is not proven anything.
    """
    in_comments = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type or node.id in inert:
            in_comments += len(mention.findall(node.text.decode("utf-8", "replace")))
            continue
        if node.child_count == 0:
            if mention.search(node.text.decode("utf-8", "replace")):
                return False
            continue
        stack.extend(node.children)
    return in_comments > 0


def _comment_lines(root: Node, source: str, mention: re.Pattern[str]) -> list[tuple[int, str]]:
    """``(line, text)`` for each source line where a comment names the key."""
    lines = source.split("\n")
    found: dict[int, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            text = node.text.decode("utf-8", "replace")
            for match in mention.finditer(text):
                row = node.start_point[0] + text.count("\n", 0, match.start())
                found.setdefault(row + 1, lines[row].strip() if row < len(lines) else "")
            continue
        stack.extend(node.children)
    return sorted(found.items())


#: The grammars whose tests carry a title :mod:`flag_cleanup.ts_tests` can read.
_TITLED = frozenset({"ts", "tsx", "js"})


def _title_only(
    root: Node, source: str, mention: re.Pattern[str], language: str
) -> bool:
    """Whether every mention sits in a test title or a comment, one in a title."""
    if language not in _TITLED:
        return False
    titles = ts_tests.title_nodes(root, source.encode("utf-8"))
    in_titles = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            continue
        if node.id in titles:
            in_titles += len(mention.findall(node.text.decode("utf-8", "replace")))
            continue
        if node.child_count == 0:
            if mention.search(node.text.decode("utf-8", "replace")):
                return False
            continue
        stack.extend(node.children)
    return in_titles > 0


#: What may follow a key that is a whole path segment: another segment, a
#: query or fragment, or the string's closing quote.
_PATH_END = frozenset(b"/?#\"'`")


def _path_only(root: Node, source: str, mention: re.Pattern[str], language: str) -> bool:
    """Whether every mention outside comments and test titles is a whole
    path segment of a URL in a string: ``/`` in front of it, and ``/``,
    ``?``, ``#`` or the closing quote after it, with one such mention found.

    Read off the bytes around each mention rather than a grammar's string
    node, so every language answers the same way; the leaf must still be a
    string's (its type, or its parent's, names one), or ``a/b`` in code
    would count."""
    data = source.encode("utf-8")
    titles = ts_tests.title_nodes(root, data) if language in _TITLED else frozenset()
    in_paths = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type or node.id in titles:
            continue
        if node.child_count:
            stack.extend(node.children)
            continue
        text = node.text.decode("utf-8", "replace")
        matches = list(mention.finditer(text))
        if not matches:
            continue
        if "str" not in node.type and (node.parent is None or "str" not in node.parent.type):
            return False
        for match in matches:
            start = node.start_byte + len(text[: match.start()].encode("utf-8"))
            end = start + len(match.group().encode("utf-8"))
            if data[start - 1 : start] != b"/" or (end < len(data) and data[end] not in _PATH_END):
                return False
            in_paths += 1
    return in_paths > 0


#: Per language: the declarations that NAME the file's own package or
#: namespace, where the key may be any segment, the last one included.
_OWN_NAMESPACE = {
    "java": frozenset({"package_declaration"}),
    "kt": frozenset({"package_header"}),
    "csharp": frozenset({"namespace_declaration", "file_scoped_namespace_declaration"}),
    "go": frozenset({"package_clause"}),
    "php": frozenset({"namespace_definition"}),
}
#: Per language: the declarations that IMPORT something by a path. The
#: imported name itself (the last segment) can be a constant named like the
#: key, so there the key must be followed by a separator: a package or a type
#: that holds what is imported. Python's are judged on their own; see
#: :func:`_python_module_path`.
_IMPORT = {
    "java": frozenset({"import_declaration"}),
    "kt": frozenset({"import", "import_header"}),
    "csharp": frozenset({"using_directive"}),
    "php": frozenset({"namespace_use_declaration"}),
    "python": frozenset({"import_statement", "import_from_statement"}),
}
#: Nodes a path's segment may sit in between it and its declaration. Anything
#: else on the way (a body, a string, an alias) is not the declared path.
_PATH_NODES = frozenset({
    "scoped_identifier",
    "qualified_identifier",
    "qualified_name",
    "dotted_name",
    "namespace_name",
    "relative_import",
    "aliased_import",
    "namespace_use_clause",
    "namespace_use_group",
    "namespace_use_group_clause",
})
#: What separates a path's segments: ``.``, and PHP's ``\``.
_SEPARATORS = frozenset(b".\\")


def _scan_mention(flag_key: str) -> re.Pattern[str]:
    """The key as the residual scan finds it: whole, but with a ``.`` allowed
    on either side, which :func:`_mention` excludes."""
    return re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(flag_key)}(?![A-Za-z0-9_-])")


def _python_module_path(leaf: Node, declaration: Node) -> bool:
    """Whether ``leaf`` is a segment of a MODULE path in a Python import:
    anywhere in ``import a.b`` (but not its alias), or in the module of
    ``from a.b import c`` (but not in what it imports, which may be a
    constant)."""
    dotted = leaf.parent
    if dotted is None or dotted.type != "dotted_name":
        return False
    if declaration.type == "import_statement":
        return True
    module = declaration.child_by_field_name("module_name")
    return module is not None and module.start_byte <= leaf.start_byte < module.end_byte


def _go_import_segment(leaf: Node, flag_key: str) -> bool:
    """Whether ``leaf``, a Go import path's content, names the key only as
    whole segments after the first: ``"example.com/qos/sub"``, never ``"qos"``
    (a module named like the key) or ``"example.com/qos-v2"``."""
    spec = leaf.parent.parent if leaf.parent is not None else None
    if leaf.type != "interpreted_string_literal_content" or spec is None or spec.type != "import_spec":
        return False
    first, *rest = leaf.text.decode("utf-8", "replace").split("/")
    scan = _scan_mention(flag_key)
    return (
        not scan.search(first)
        and flag_key in rest
        and all(segment == flag_key or not scan.search(segment) for segment in rest)
    )


def _namespace_segment(leaf: Node, data: bytes, flag_key: str, language: str) -> bool:
    """Whether ``leaf`` is the key as one segment of the path a package,
    namespace or import declaration names."""
    if language == "go" and "str" in leaf.type:
        return _go_import_segment(leaf, flag_key)
    if leaf.text.decode("utf-8", "replace") != flag_key:
        return False
    own = _OWN_NAMESPACE.get(language, frozenset())
    imports = _IMPORT.get(language, frozenset())
    current = leaf.parent
    while current is not None and current.type in _PATH_NODES:
        current = current.parent
    if current is None:
        return False
    if current.type in own:
        return True
    if current.type not in imports:
        return False
    if language == "python":
        return _python_module_path(leaf, current)
    return leaf.end_byte < len(data) and data[leaf.end_byte] in _SEPARATORS


def _namespace_only(root: Node, source: str, flag_key: str, language: str) -> bool:
    """Whether every mention of the key outside comments is one segment of a
    package, namespace or import path, with one such mention found.

    Judged with the residual scan's own match (:func:`_scan_mention`), so a
    dotted mention anywhere else, ``"pollers.qos"`` or ``qos.enabled``,
    keeps the file in the generic caveat: a string may hold a key, and code
    may read one through a property named after it. Only the declaration's
    PATH counts, never a body it encloses (a C# block namespace) or an alias
    it introduces."""
    if language not in _OWN_NAMESPACE and language not in _IMPORT:
        return False
    data = source.encode("utf-8")
    scan = _scan_mention(flag_key)
    found = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            continue
        if node.child_count:
            stack.extend(node.children)
            continue
        if not scan.search(node.text.decode("utf-8", "replace")):
            continue
        if not _namespace_segment(node, data, flag_key, language):
            return False
        found += 1
    return found > 0


def _literal_only(root: Node, source: str, mention: re.Pattern[str], language: str) -> bool:
    """Whether every mention outside comments and test titles is a string
    element of an array literal, the WHOLE string being the key, with one
    such mention found. TypeScript family only.

    Three things keep a list of flag keys out, since that is a flag read in
    all but name: the array must hold at least one element that is not a
    string literal (``['key', orgId]``; ``['key', 'other']`` stays in the
    generic caveat), it must not be handed straight to a call (the callee may
    read flags by key), and the string must be a plain one (a template can
    splice anything in)."""
    if language not in _TITLED:
        return False
    data = source.encode("utf-8")
    titles = ts_tests.title_nodes(root, data)
    found = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type or node.id in titles:
            continue
        if node.child_count:
            stack.extend(node.children)
            continue
        if not mention.search(node.text.decode("utf-8", "replace")):
            continue
        string = node.parent
        if (
            node.type != "string_fragment"
            or string is None
            or string.type != "string"
            or not mention.fullmatch(node.text.decode("utf-8", "replace"))
            or len(string.named_children) != 1
            or not _array_value(string)
        ):
            return False
        found += 1
    return found > 0


def _array_value(string: Node) -> bool:
    """Whether ``string`` is an element of an array literal that also holds a
    non-literal element and is not an argument of a call."""
    array = string.parent
    if array is None or array.type != "array":
        return False
    if array.parent is not None and array.parent.type == "arguments":
        return False
    return any(
        element.type not in ("string", "comment") for element in array.named_children
    )


#: Words that keep a callee, a constructed type or a receiver from being
#: "not a flag read": a name holding one may read a flag by key (an SDK call
#: of any type, ``GetBooleanValueAsync``, a wrapper the run does not know,
#: ``IsFeatureOn``) or look a value up by it (``GetSection``, ``GetValue``).
#: Matched case-insensitively anywhere in the name.
_FLAG_WORDS = re.compile(
    r"flag|feature|toggle|switch|gate|variation|variant|evaluat|experiment|treatment|rollout"
    r"|enabled|disabled|active|ison|isoff|value|detail|config|setting|option|section|override",
    re.IGNORECASE,
)
#: Other vendors' reads by key, and lookups by key, matched against the
#: whole name: Optimizely's ``Decide``, Eppo's ``GetStringAssignment``,
#: Statsig's ``GetLayer``/``GetExperiment``/``GetConfig``, DevCycle's
#: ``Variable``/``VariableValue``, Split's ``GetTreatment``, a bare ``Get``.
_KEYED_READS = re.compile(
    r"Decide\w*|Get\w*Assignment\w*|GetLayer|GetExperiment|GetConfig|Variable|VariableValue"
    r"|Evaluate\w*|IsFeatureEnabled|IsEnabled|GetFeatureValue|GetFeature|GetTreatment\w*"
    r"|(?:Try)?Get(?:Async)?|Lookup|Find|Fetch|Read"
)
#: Mocking calls (Moq, NSubstitute, FakeItEasy): a string argument of a call
#: set up, verified or stubbed through one is a stub of that call, whatever
#: its name.
_MOCK_CALLS = frozenset({
    "Setup", "SetupGet", "SetupSequence", "SetupProperty", "Verify", "VerifyGet",
    "Returns", "ReturnsAsync", "ReturnsForAnyArgs", "Received", "DidNotReceive",
    "ReceivedWithAnyArgs", "When", "Do", "CallTo", "Throws", "ThrowsAsync", "Is", "IsAny",
})
#: Argument matchers: a call holding one is a mock's.
_MATCHERS = frozenset({"Arg", "It", "A"})
#: Where a call's value is not used by any other code: the statement, the
#: resource a ``using`` disposes, and nothing else (see :func:`_value_discarded`).
_MEMBER_SCOPES = frozenset({
    "method_declaration", "local_function_statement", "constructor_declaration",
    "property_declaration", "field_declaration", "class_declaration", "struct_declaration",
    "record_declaration", "compilation_unit",
})


@dataclass(frozen=True)
class _Callee:
    name: str
    receiver: Node | None
    generic: bool


def _csharp_callee(call: Node) -> _Callee | None:
    """The simple name, receiver and genericness of a C# invocation or
    object creation, or ``None`` when the callee is not a plain name (a
    delegate called through an expression, an indexer's result)."""
    if call.type == "object_creation_expression":
        spelled = call.child_by_field_name("type")
        if spelled is None:
            return None
        text = _node_text(spelled)
        return _Callee(re.split(r"[.:]", re.sub(r"<.*", "", text, flags=re.DOTALL))[-1].strip(), None, "<" in text)
    function = call.child_by_field_name("function")
    receiver = None
    if function is not None and function.type == "member_access_expression":
        receiver = function.child_by_field_name("expression")
        function = function.child_by_field_name("name")
    elif function is not None and function.type == "conditional_access_expression":
        receiver = function.child_by_field_name("condition")
        binding = function.named_children[-1] if function.named_children else None
        function = binding.child_by_field_name("name") if binding is not None and binding.type == "member_binding_expression" else None
    generic = function is not None and function.type == "generic_name"
    if generic:
        function = next((c for c in function.named_children if c.type == "identifier"), None)
    if function is None or function.type != "identifier":
        return None
    return _Callee(_node_text(function), receiver, generic)


def _node_text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _reading_wrappers(root: Node) -> frozenset[str]:
    """Names of the file's own methods and local functions that hand one of
    their parameters to a call: a wrapper that may read a flag by the key it
    is given (``bool On(string k) => _client.BoolVariation(k, …)``)."""
    found: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type not in ("method_declaration", "local_function_statement"):
            continue
        name = node.child_by_field_name("name")
        listed = node.child_by_field_name("parameters")
        if name is None or listed is None:
            continue
        parameters = {
            _node_text(p.child_by_field_name("name"))
            for p in listed.named_children
            if p.type == "parameter" and p.child_by_field_name("name") is not None
        }
        inner = [node]
        while inner:
            current = inner.pop()
            inner.extend(current.children)
            if current.type == "argument" and any(
                n.type == "identifier" and _node_text(n) in parameters for n in _subtree(current)
            ):
                found.add(_node_text(name))
                break
    return frozenset(found)


def _subtree(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _read_like(call: Node, reads: frozenset[str], wrappers: frozenset[str]) -> bool:
    """Whether ``call`` may read a flag by key, or is a mock's: an unknown
    callee, a read, an accessor or a same-file wrapper by name, a name with a
    flag word or a keyed read's name, a generic ``Get…<T>``, or a receiver
    with a flag word or a mocking call or matcher anywhere in it."""
    callee = _csharp_callee(call)
    if callee is None:
        return True
    name = callee.name
    if (
        name in reads or name in wrappers or name in _MOCK_CALLS
        or _FLAG_WORDS.search(name) or _KEYED_READS.fullmatch(name)
        or (callee.generic and name.startswith("Get"))
    ):
        return True
    if callee.receiver is None:
        return False
    for node in _subtree(callee.receiver):
        # Names only: a string in the receiver chain (`t.ForJob("k")`) is data.
        if node.type == "identifier" and (_node_text(node) in _MATCHERS or _FLAG_WORDS.search(_node_text(node))):
            return True
        if node.type == "invocation_expression":
            inner = _csharp_callee(node)
            if inner is None or inner.name in _MOCK_CALLS:
                return True
    return False


def _lambda_parameters(lam: Node) -> set[str]:
    names: set[str] = set()
    for child in lam.named_children:
        if child.type == "implicit_parameter":
            names.add(_node_text(child))
        elif child.type == "parameter_list":
            names |= {
                _node_text(p.child_by_field_name("name"))
                for p in child.named_children
                if p.type == "parameter" and p.child_by_field_name("name") is not None
            }
    return names


def _chain_root(node: Node) -> Node:
    """The expression a builder chain (``t.A(…).B(…)``) starts from."""
    while True:
        if node.type == "invocation_expression":
            node = node.child_by_field_name("function") or node
            if node.type != "member_access_expression":
                return node
        if node.type == "member_access_expression":
            node = node.child_by_field_name("expression") or node
            continue
        return node


def _value_discarded(call: Node) -> bool:
    """Whether nothing reads the value of ``call``: the end of the builder
    chain it starts (``t.ForJob("k").WithIdentity(…)``) is a statement of
    its own, a resource a ``using`` disposes, a ``using var`` read nowhere
    else, or the result of a lambda configuring the parameter it is handed
    (``j => j.WithIdentity("k")``) as an argument of a call whose own value
    is discarded, the call or the next in its chain being a builder verb
    (:data:`_BUILDER_VERBS`; ``sp => sp.Resolve("k")`` is a factory). A
    local, a field, a return, a condition, an argument or a property read of
    the result (``new Key("k").Value``) uses it."""
    node = call
    first = _csharp_callee(call)
    chain = [first.name] if first is not None else [""]
    while True:
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "member_access_expression" and parent.child_by_field_name("expression") == node:
            outer = parent.parent
            if outer is not None and outer.type == "invocation_expression" and outer.child_by_field_name("function") == parent:
                node = outer
                named = _csharp_callee(outer)
                chain.append(named.name if named is not None else "")
                continue
            return False
        if parent.type == "await_expression":
            node = parent
            continue
        break
    parent = node.parent
    if parent.type == "expression_statement":
        return True
    if parent.type == "using_statement" and parent.child_by_field_name("body") != node:
        return True
    if parent.type == "variable_declarator":
        declaration = parent.parent
        statement = declaration.parent if declaration is not None else None
        if (
            statement is None
            or statement.type != "local_declaration_statement"
            or not any(not c.is_named and _node_text(c) == "using" for c in statement.children)
        ):
            return False
        name = parent.child_by_field_name("name")
        scope = statement.parent
        if name is None or scope is None:
            return False
        spelled = _node_text(name)
        return not any(
            n.type == "identifier" and _node_text(n) == spelled and n.start_byte != name.start_byte
            for n in _subtree(scope)
        )
    if parent.type == "lambda_expression" and parent.child_by_field_name("body") == node:
        root = _chain_root(node)
        if root.type != "identifier" or _node_text(root) not in _lambda_parameters(parent):
            return False
        # A configurator (`j => j.WithIdentity("k")`), not a factory whose
        # result is used (`sp => sp.Resolve("k")`): the call holding the
        # string, or the next one in its chain, is a builder verb.
        if not any(_BUILDER_VERBS.match(name) for name in chain[:2]):
            return False
        argument = parent.parent
        arguments = argument.parent if argument is not None and argument.type == "argument" else None
        outer = arguments.parent if arguments is not None and arguments.type == "argument_list" else None
        return outer is not None and outer.type == "invocation_expression" and _value_discarded(outer)
    return False


#: Names a configurator's calls start with, in a lambda handed to a builder.
_BUILDER_VERBS = re.compile(r"(?:With|For|Named|Set|Use|Configure|Add|Map|Has)(?![a-z])")
#: Receivers that name the type's own members, which a base class may declare.
_OWN_RECEIVERS = frozenset({"this", "base"})


def _own_members(root: Node) -> frozenset[str]:
    """Names of the methods and local functions the file declares."""
    return frozenset(
        _node_text(name)
        for node in _subtree(root)
        if node.type in ("method_declaration", "local_function_statement")
        and (name := node.child_by_field_name("name")) is not None
    )


#: What another argument of the call may be: a name, a member read or a
#: string. A computed value (``new Flag(true)``, ``1 == 1``, ``$"…"``, a
#: literal of another type) may be a registry or stub entry for the key.
_PLAIN_OTHER_ARGUMENTS = frozenset({"identifier", "member_access_expression", "string_literal"})


def _plain_argument(
    leaf: Node, flag_key: str, reads: frozenset[str], wrappers: frozenset[str] = frozenset(), own: frozenset[str] = frozenset()
) -> bool:
    """Whether ``leaf`` is the whole content of a plain C# string literal
    passed to a call the run can see reads no flag with it:

    * the call, and every call around it up to its member, is not
      :func:`_read_like` (``BoolVariation(Keys.Normalize("k"), …)`` is a read);
    * its value is used by nothing (:func:`_value_discarded`), so it cannot
      carry the key on to a read (``var key = Normalize("k")``);
    * every other argument is a name, a member read or a string, and a named
      argument's label does not say key or flag;
    * a call with no receiver, or through ``this``, is to a method ``own``
      (the file's own) declares; never through ``base``."""
    string = leaf.parent
    if (
        leaf.type != "string_literal_content"
        or _node_text(leaf) != flag_key
        or string is None
        or string.type != "string_literal"
        or len(string.named_children) != 1
    ):
        return False
    argument = string.parent
    arguments = argument.parent if argument is not None and argument.type == "argument" else None
    call = arguments.parent if arguments is not None and arguments.type == "argument_list" else None
    if call is None or call.type not in ("invocation_expression", "object_creation_expression"):
        return False
    if any(not c.is_named and _node_text(c) in ("ref", "out", "in") for c in argument.children):
        return False
    if call.type == "invocation_expression":
        # A bare `Require("k")`, `this.Check("k")` or `base.Require("k")` may be
        # a guard a base class in another file declares: only a method of
        # this file, read above as no wrapper, is known.
        callee = _csharp_callee(call)
        receiver = _node_text(callee.receiver) if callee is not None and callee.receiver is not None else None
        if callee is not None and (receiver is None or receiver in _OWN_RECEIVERS):
            if receiver == "base" or callee.name not in own:
                return False
    # A named argument (`key: "k"`) is an identifier and a `:` before the value.
    label = argument.children[0] if len(argument.children) > 2 and _node_text(argument.children[1]) == ":" else None
    if label is not None and (_FLAG_WORDS.search(_node_text(label)) or "key" in _node_text(label).lower()):
        return False
    for other in arguments.named_children:
        if other.type != "argument" or other == argument:
            continue
        value = other.named_children[-1] if other.named_children else None
        if value is None or value.type not in _PLAIN_OTHER_ARGUMENTS:
            return False
    current: Node | None = call
    while current is not None and current.type not in _MEMBER_SCOPES:
        if current.type in ("invocation_expression", "object_creation_expression") and _read_like(current, reads, wrappers):
            return False
        current = current.parent
    return _value_discarded(call)


def _plain_string_lines(
    root: Node, source: str, flag_key: str, language: str, reads: frozenset[str]
) -> list[tuple[int, str]] | None:
    """``(line, text)`` of each line naming the key, when every mention of
    it outside comments is a plain string argument of a call that is no
    flag read (:func:`_plain_argument`), with one such mention found; else
    ``None``. C# only.

    Judged with the residual scan's own match (:func:`_scan_mention`), so
    every hit the scan made is accounted for: a key-const declaration, a
    dictionary entry, an attribute's argument, an interpolated or verbatim
    string or anything else keeps the file in the generic caveat, and so
    does a file that does not parse whole."""
    if language != "csharp" or root.has_error:
        return None
    scan = _scan_mention(flag_key)
    lines = source.split("\n")
    found: dict[int, str] = {}
    strings = judged = 0
    wrappers = _reading_wrappers(root)
    own = _own_members(root)
    stack = [root]
    while stack:
        node = stack.pop()
        text = _node_text(node)
        if not scan.search(text):
            continue
        if "comment" in node.type:
            for match in scan.finditer(text):
                judged += 1
                row = node.start_point[0] + text.count("\n", 0, match.start())
                found.setdefault(row + 1, lines[row].strip() if row < len(lines) else "")
            continue
        if node.child_count:
            stack.extend(node.children)
            continue
        if not _plain_argument(node, flag_key, reads, wrappers, own):
            return None
        strings += 1
        judged += 1
        row = node.start_point[0]
        found.setdefault(row + 1, lines[row].strip() if row < len(lines) else "")
    # A mention no single leaf holds (`my-flag` spelled as `my - flag` in
    # code) was never judged above.
    if not strings or judged != len(scan.findall(source)):
        return None
    return sorted(found.items())


#: The grammar node of one enum member, per language (measured, not guessed).
#: Python has no enum syntax: an assignment in a class whose bases name an
#: ``...Enum`` or a Django ``...Choices`` is the member there.
_ENUM_MEMBERS = {
    "java": "enum_constant",
    "dart": "enum_constant",
    "kt": "enum_entry",
    "swift": "enum_entry",
    "ts": "enum_assignment",
    "tsx": "enum_assignment",
    "js": "enum_assignment",
    "csharp": "enum_member_declaration",
    "php": "enum_case",
}
_PYTHON_ENUM_BASE = re.compile(r"\w*(?:Enum|Choices)\b")


#: Substrings of a node type that mark a callable or a body. A mention below
#: one of these, between it and the member, is code that runs (a method of an
#: enum constant, a lambda in its arguments), not the member's own value.
_BODY_MARKERS = ("block", "body", "function", "method", "lambda", "constructor", "closure", "statements")


def _python_member(node: Node) -> Node | None:
    """The assignment that is a DIRECT statement of an Enum class's body."""
    current: Node | None = node
    while current is not None and current.type != "assignment":
        if any(m in current.type for m in _BODY_MARKERS):
            return None
        current = current.parent
    if current is None:
        return None
    statement = current.parent
    body = statement.parent if statement is not None else None
    owner = body.parent if body is not None else None
    if (
        statement is None
        or statement.type != "expression_statement"
        or body is None
        or body.type != "block"
        or owner is None
        or owner.type != "class_definition"
    ):
        return None
    bases = owner.child_by_field_name("superclasses")
    text = bases.text.decode("utf-8", "replace") if bases is not None else ""
    return current if _PYTHON_ENUM_BASE.search(text) else None


def _enum_member(node: Node, language: str) -> Node | None:
    """The enum member whose OWN value ``node`` is, or None when ``node`` sits
    in a body or callable on the way up (a live read, not the stored key)."""
    if language == "python":
        return _python_member(node)
    kind = _ENUM_MEMBERS.get(language)
    current: Node | None = node
    while current is not None:
        if kind is not None and current.type == kind:
            return current
        if kind == "enum_assignment" and current.type == "pair" and _as_const_property(current, node):
            return current
        if kind == "enum_assignment" and current.type == "public_field_definition" and _static_field(current, node):
            return current
        if any(m in current.type for m in _BODY_MARKERS):
            return None
        current = current.parent
    return None


def _static_field(field: Node, mention: Node) -> bool:
    """Whether ``mention`` is the plain string value (also ``as const``) of
    ``field``, a ``static readonly`` field of a class declared at the top of
    its file: a key registry read as ``Owner.FIELD``, like an ``as const``
    object's property."""
    value = field.child_by_field_name("value")
    if value is not None and value.type == "as_expression" and any(c.type == "const" for c in value.children):
        value = next((c for c in value.named_children if c.type != "comment"), None)
    if value is None or value.type != "string" or not (value.start_byte <= mention.start_byte < value.end_byte):
        return False
    if not {"static", "readonly"} <= {c.type for c in field.children}:
        return False
    body = field.parent
    declaration = body.parent if body is not None else None
    top = declaration.parent if declaration is not None else None
    if top is not None and top.type == "export_statement":
        top = top.parent
    return (
        body is not None
        and body.type == "class_body"
        and declaration is not None
        and declaration.type in ("class_declaration", "abstract_class_declaration")
        and top is not None
        and top.type == "program"
    )


def _as_const_property(pair: Node, mention: Node) -> bool:
    """Whether ``mention`` is the string value of ``pair``, a property holding
    it as its literal type: a plain string in an ``as const`` object literal,
    or one under ``as const`` (``X: "<key>" as const``) in any object literal
    (see :func:`cross_file_ts.entry_string`). Not its name (a registry keyed
    by the flag), and not something nested in its value: no entry holds
    those."""
    obj = pair.parent
    if obj is None or obj.type != "object":
        return False
    outer = obj
    while outer.parent is not None and outer.parent.type == "parenthesized_expression":
        outer = outer.parent
    const_object = outer.parent is not None and cross_file_ts.as_const_object(outer.parent) is not None
    value = cross_file_ts.entry_string(pair.child_by_field_name("value"), const_object)
    return value is not None and value.start_byte <= mention.start_byte < value.end_byte


def _enum_held(root: Node, mention: re.Pattern[str], language: str) -> str | None:
    """The first enum member holding the key, when every live mention sits in one.

    Each mention is judged at the SMALLEST node whose text contains it, so a
    grammar whose string content is not a leaf (Dart's) is judged the same way.
    """
    first: Node | None = None
    stack = [root]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            continue
        if not mention.search(node.text.decode("utf-8", "replace")):
            continue
        inner = [c for c in node.children if mention.search(c.text.decode("utf-8", "replace"))]
        if inner:
            stack.extend(inner)
            continue
        member = _enum_member(node, language)
        if member is None:
            return None
        if first is None or member.start_byte < first.start_byte:
            first = member
    if first is None:
        return None
    return first.text.decode("utf-8", "replace").splitlines()[0].strip()


def _parse(source: str, language: str) -> Node:
    if language in ("ts", "tsx", "js"):
        return unused._parse(source, language)
    return syntax._parse(source, language)


def classify(
    paths: tuple[str, ...], flag_key: str, accessors: tuple[str, ...] = ()
) -> Residue:
    """Sort ``paths`` — the still-referencing files — into the two groups a
    pull request can say something specific about. Must be asked while the
    rewrite is on disk, like the scan that produced ``paths``."""
    mention = _mention(flag_key)
    stubbed: list[tuple[str, str]] = []
    comment_only: list[str] = []
    title_only: list[str] = []
    enum_held: list[tuple[str, str]] = []
    comment_lines: list[tuple[str, int, str]] = []
    path_only: list[str] = []
    literal_only: list[str] = []
    namespace_only: list[str] = []
    plain_strings: list[str] = []
    plain_string_lines: list[tuple[str, int, str]] = []
    generated_comment_only: list[str] = []
    for path in paths:
        language = _grammar_by_extension(path)
        if language is None:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        if language == "csharp":
            described = mock_stubs.mock_descriptions(
                source, flag_key, rule_synthesis.accessors_for(language, accessors)
            )
            if described:
                stubbed.extend((path, text) for text in described)
                continue
        root = _parse(source, language)
        if _comment_only(root, mention):
            if generated_files.is_generated(path, source):
                generated_comment_only.append(path)
                continue
            comment_only.append(path)
            comment_lines.extend((path, line, text) for line, text in _comment_lines(root, source, mention))
            continue
        if _title_only(root, source, mention, language):
            title_only.append(path)
            continue
        if _path_only(root, source, mention, language):
            path_only.append(path)
            continue
        if _literal_only(root, source, mention, language):
            literal_only.append(path)
            continue
        if _namespace_only(root, source, flag_key, language):
            namespace_only.append(path)
            continue
        plain = _plain_string_lines(
            root, source, flag_key, language,
            frozenset({*cross_file_csharp._READ_CALLEES, *rule_synthesis.accessors_for(language, accessors)}),
        )
        if plain is not None:
            plain_strings.append(path)
            plain_string_lines.extend((path, line, text) for line, text in plain)
            continue
        held = _enum_held(root, mention, language)
        if held is not None:
            enum_held.append((path, held))
    return Residue(
        tuple(stubbed),
        tuple(comment_only),
        tuple(enum_held),
        tuple(title_only),
        tuple(comment_lines),
        tuple(path_only),
        literal_only=tuple(literal_only),
        namespace_only=tuple(namespace_only),
        plain_strings=tuple(plain_strings),
        plain_string_lines=tuple(plain_string_lines),
        generated_comment_only=tuple(generated_comment_only),
    )


def live_mention(path: str, source: str, flag_key: str) -> bool:
    """Whether ``source`` (the text of ``path``) names ``flag_key`` other than
    in a comment or, in the TypeScript family, a test title. One file's
    answer to :func:`live_references`, for text that is not on disk yet."""
    mention = _mention(flag_key)
    if not mention.search(source):
        return False
    language = _grammar_by_extension(path)
    if language is None:
        return True
    root = _parse(source, language)
    inert = (
        ts_tests.title_nodes(root, source.encode("utf-8"))
        if ts_tests.covers(language)
        else frozenset()
    )
    return not _comment_only(root, mention, inert)


def live_references(paths: tuple[str, ...], flag_key: str) -> tuple[str, ...]:
    """``paths`` without the files that name the flag only in comments and,
    in the TypeScript family, test titles.

    The NEGATIVE of the comment verdict, so it errs the other way from
    :func:`classify`: a file this module cannot parse, or whose extension it
    does not know, counts as a live reference. The caller keeps a registry
    entry while any survives, and an entry kept for nothing costs one line.

    A test title is the one string let through, because it is the one that is
    inert by construction: nothing is typed against it and nothing looks it
    up, so deleting the entry cannot break it. Any other string stays live
    even in a test file, where fixture data borrowing the key may still reach
    a lookup by key — see :func:`flag_cleanup.ts_tests.title_nodes`.
    """
    mention = _mention(flag_key)
    live: list[str] = []
    for path in paths:
        language = _grammar_by_extension(path)
        try:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        except (OSError, UnicodeDecodeError):
            live.append(path)
            continue
        if language is None:
            live.append(path)
            continue
        root = _parse(source, language)
        inert = (
            ts_tests.title_nodes(root, source.encode("utf-8"))
            if ts_tests.covers(language)
            else frozenset()
        )
        if not _comment_only(root, mention, inert):
            live.append(path)
    return tuple(live)
