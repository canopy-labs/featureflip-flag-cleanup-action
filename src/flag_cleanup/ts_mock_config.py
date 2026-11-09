"""Mock shapes the call-by-call mock proofs cannot see, and the test runner
configuration that mocks modules without a call.

A test that asserts on the arguments a function was called with can tell an
argument that was dropped. The proofs that drop one (``ts_default_arguments``,
``ts_unused_options``, the audited-package check of ``ts_constant_params``)
look for the calls that replace a module or spy on a function; four more ways
to do it are decided here, once, so every proof refuses them alike:

* ``jest.enableAutomock()``, and a jest configuration with ``automock: true``,
  replace every module with a recording stand-in;
* a jest ``moduleNameMapper`` entry that maps a module to a mock file does the
  same for the modules it matches;
* ``mock.module(...)`` and ``mock.method(...)`` from ``node:test`` and
  ``bun:test`` are mockers and spies under other names;
* a template literal without substitutions is a plain string.

A configuration this module cannot read counts as mocking everything.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tree_sitter import Node

#: Files that configure a test runner, found by NAME anywhere in the checkout.
#: A `vite.config.*` is one only when the repository uses vitest.
_CONFIG_NAME = re.compile(
    r"^(?:jest.*\.(?:js|cjs|mjs|ts|cts|mts|json)"
    r"|.*jest[-.]preset.*"
    r"|vitest.*\.(?:js|ts|mjs|cjs|mts)"
    r"|vitest\.workspace\..*)$",
    re.IGNORECASE,
)
_VITE_NAME = re.compile(r"^vite\.config\..*$", re.IGNORECASE)
#: In a JS/TS config, any of these (case-insensitive) means a module may be
#: remapped or replaced, and the verdict is "everything mocked". Deliberately
#: not parsed: a config is code, and only text that cannot spell them is trusted.
_TOKENS = (
    "namemapper", "alias", "automock", "projects", "setupfiles", "preset",
    "resolver", "moduledirectories", "modulepaths",
)
#: JSON keys with the same meaning, wherever they appear.
_REMAP_KEYS = frozenset(
    k.lower()
    for k in (
        "preset", "setupFiles", "setupFilesAfterEnv", "setupFilesAfterEach",
        "resolver", "moduleDirectories", "modulePaths",
    )
)
#: A package.json script that runs a test runner with a config of its own.
_RUNNER_SCRIPT = re.compile(r"jest|vitest", re.IGNORECASE)
_SCRIPT_FLAGS = re.compile(r"(?:^|\s)(?:--config\b|--automock\b|-c\s)")
#: Mapper keys that only match files a function does not live in.
_ASSET_EXTENSIONS = frozenset(
    "css scss sass less svg png jpg jpeg gif webp woff woff2 ttf eot".split()
)
_KEY_EXTENSIONS = re.compile(r"\\\.\(?(?:\?:)?([a-z0-9|]+)\)?\$?$", re.IGNORECASE)

def plain_string(node: Node | None) -> str | None:
    """The text of a string literal, or of a template literal with no
    substitutions; None for anything else."""
    if node is None:
        return None
    if node.type == "template_string":
        if any(c.type != "string_fragment" for c in node.named_children):
            return None
        return "".join(c.text.decode("utf-8") for c in node.named_children)
    from flag_cleanup import cross_file_ts

    return cross_file_ts._string_value(node)


def automock_call(function: Node | None) -> bool:
    """Whether ``function`` is ``jest.enableAutomock``."""
    if function is None or function.type != "member_expression":
        return False
    member = function.child_by_field_name("property")
    receiver = function.child_by_field_name("object")
    return (
        member is not None
        and member.text == b"enableAutomock"
        and receiver is not None
        and receiver.text in (b"jest", b"vi")
    )


def node_mocker(function: Node | None) -> str | None:
    """``module`` or ``method`` for ``mock.module`` / ``t.mock.method``, the
    mock APIs of ``node:test`` and ``bun:test``; None for anything else."""
    if function is None or function.type != "member_expression":
        return None
    member = function.child_by_field_name("property")
    receiver = function.child_by_field_name("object")
    if member is None or member.text not in (b"module", b"method", b"getter", b"setter") or receiver is None:
        return None
    if receiver.type == "identifier":
        mock = receiver.text == b"mock"
    elif receiver.type == "member_expression":
        tail = receiver.child_by_field_name("property")
        mock = tail is not None and tail.text == b"mock"
    else:
        mock = False
    return member.text.decode("utf-8") if mock else None


def everything_mocked(repo_dir: str, *, automock_only: bool = False) -> bool:
    """Whether the checkout's test runner configuration may mock or remap a
    module without a call in a test. It fails CLOSED, on purpose:

    * a JS or TS config (named like ``jest*``, ``vitest*``, a jest preset, and
      ``vite.config.*`` when the repository uses vitest) that spells any of
      :data:`_TOKENS`, or cannot be read, counts as mocking everything; it is
      never parsed to clear it;
    * a JSON config, the ``jest`` key of a ``package.json`` and its ``projects``
      are parsed and walked (:func:`_clean`);
    * a ``package.json`` script running jest or vitest with ``--config``,
      ``--automock`` or ``-c`` counts.

    ``automock_only`` asks about automock alone, for a caller that decides
    aliases and mappers for itself (the audited-package check)."""
    from flag_cleanup import source_index
    from flag_cleanup.piranha_runner import _SKIP_DIRS, source_files

    root = source_index.checkout_root(repo_dir)
    memo = source_index.run_memo("mock_config")
    if memo is not None and (root, automock_only) in memo:
        return memo[(root, automock_only)]
    files = source_files(Path(root), {".json", ".js", ".cjs", ".mjs", ".ts", ".cts", ".mts"}, _SKIP_DIRS)
    texts: dict[Path, str | None] = {}
    for path in files:
        if path.name == "package.json" or _CONFIG_NAME.match(path.name) or _VITE_NAME.match(path.name):
            try:
                texts[path] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                texts[path] = None
    vitest = any(
        re.match(r"vitest.*\.config\.|vitest\.workspace\.", path.name, re.IGNORECASE) for path in files
    ) or any(
        path.name == "package.json" and text is not None and _uses_vitest(text)
        for path, text in texts.items()
    )
    found = False
    for path, text in texts.items():
        package = path.name == "package.json"
        if _VITE_NAME.match(path.name) and not vitest:
            continue  # a vite config of a repository that does not run vitest
        if text is None or _file_mocks(text, package, path.suffix == ".json", automock_only):
            found = True
            break
    if memo is not None:
        memo[(root, automock_only)] = found
    return found


def _uses_vitest(text: str) -> bool:
    """Whether a ``package.json`` names vitest in its dependencies or scripts."""
    try:
        data = json.loads(text)
    except ValueError:
        return "vitest" in text
    if not isinstance(data, dict):
        return False
    for field in ("dependencies", "devDependencies", "scripts"):
        value = data.get(field)
        if value is not None and "vitest" in json.dumps(value):
            return True
    return False


def _file_mocks(text: str, package: bool, json_file: bool, automock_only: bool = False) -> bool:
    if package:
        try:
            data = json.loads(text)
        except ValueError:
            return True
        if not isinstance(data, dict):
            return True
        scripts = data.get("scripts")
        if isinstance(scripts, dict):
            for script in scripts.values():
                if not isinstance(script, str):
                    return True
                if automock_only:
                    if "--automock" in script:
                        return True
                elif _RUNNER_SCRIPT.search(script) and _SCRIPT_FLAGS.search(script):
                    return True
        return "jest" in data and not _clean(data["jest"], automock_only)
    if json_file:
        try:
            return not _clean(json.loads(text), automock_only)
        except ValueError:
            return True
    lowered = text.lower()
    if automock_only:
        return "automock" in lowered
    return any(token in lowered for token in _TOKENS)


def _clean(node, automock_only: bool = False) -> bool:
    """Whether a parsed JSON runner config turns no mocking on: ``automock`` is
    absent or false wherever it appears (``projects`` entries included), no
    key of :data:`_REMAP_KEYS` appears, and every ``moduleNameMapper`` entry is
    harmless (:func:`_harmless`)."""
    if isinstance(node, list):
        return all(_clean(item, automock_only) for item in node)
    if not isinstance(node, dict):
        return True
    for key, value in node.items():
        if key == "automock":
            if value not in (False, None):
                return False
        elif automock_only:
            if not _clean(value, True):
                return False
        elif key.lower() in _REMAP_KEYS:
            return False
        elif key == "moduleNameMapper":
            if not isinstance(value, dict) or not all(_harmless(k, v) for k, v in value.items()):
                return False
        elif not _clean(value, False):
            return False
    return True


def _harmless(pattern: str, target) -> bool:
    """Whether a mapper entry cannot stand in for a module a function lives
    in: ``identity-obj-proxy``, or a pattern matching only style and asset
    extensions."""
    targets = target if isinstance(target, list) else [target]
    if all(isinstance(t, str) and t == "identity-obj-proxy" for t in targets):
        return True
    found = _KEY_EXTENSIONS.search(pattern)
    if found is None or "/" in pattern:
        return False
    return all(ext in _ASSET_EXTENSIONS for ext in found.group(1).split("|") if ext)
