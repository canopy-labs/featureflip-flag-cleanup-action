"""The PHP a Laravel Blade view compiles to, for the safety gates.

A Blade view is markup with directives (``@if(...)``, ``@endif``) and echoes
(``{{ ... }}``). The PHP grammar reads the whole file as one inert ``text``
node, so Gate 1's structural half sees nothing in it, and the checks that ask
a question about the CODE (the error count, the stranded keywords, the literal
residues) need the code itself. This module derives it: the code view of a
Blade file, the twin of ``syntax._code_view`` for ERB, judged under the ``php``
profile.

It is written here rather than taken from the engine on purpose. The engine
rewrites a view through a PHP view of its own, and a check that read the
transform's own view would share its blind spots: a directive the engine's
scanner missed would be missing from both sides of every comparison. So this
follows what Blade itself does instead -- the ``BladeCompiler`` -- closely
enough that what the gate parses is the program the template renders as:

* ``@verbatim … @endverbatim`` and ``@php … @endphp`` are set aside first
  (``@php`` up to the first later ``@endphp``, an inline ``@php(...)`` before a
  block included); ``{{-- … --}}`` comments go; raw ``<?php … ?>`` is code.
* ``@@name`` and ``@{{ … }}`` are escapes and contribute nothing.
* A directive is ``@name`` where the ``@`` does not follow a word character
  (``me@if.com`` is text), then optional blanks and a balanced, string-aware
  parenthesised argument list.
* Conditionals and loops compile to PHP's alternative syntax, exactly as Blade
  writes them (``if (C):`` … ``endif;``); an echo is ``echo E;``. A directive
  this module does not know contributes nothing, as markup does.

Only the shape matters to the checks, and the gate compares a file before and
after, so a construct modelled loosely here is modelled the same loose way on
both sides. What it must not do is lose the conditionals: a rewrite that drops
an ``@endif`` makes this view stop parsing, and one that leaves
``@if(true)`` behind leaves ``if (true):`` in it.

Offsets in this string are its own. Nothing that edits the file may be pointed
at it (see ``syntax._code_view``).
"""

from __future__ import annotations

import difflib
import functools
import re
from collections.abc import Mapping

__all__ = ["balance", "builtin", "closers", "code_view", "custom_kind", "whitelist_violation"]

#: Where a project's own directive's argument goes in the text it compiles to
#: (`blade_directives.ARGUMENT`).
_ARGUMENT = "\0"

#: Built-in conditionals, by lower-cased name, and the PHP condition the view
#: gives each. The arguments are kept where Blade keeps them, so a rewrite of
#: one changes the view.
_CONDITIONALS = {
    "if": "{args}",
    "elseif": "{args}",
    "unless": "!({args})",
    "isset": "isset({args})",
    "empty": "empty({args})",
    "auth": "auth({args})",
    "guest": "guest({args})",
    "env": "env({args})",
    "production": "production()",
    "hassection": "has_section({args})",
    "sectionmissing": "section_missing({args})",
    "can": "can({args})",
    "cannot": "cannot({args})",
    "canany": "can_any({args})",
    "error": "error({args})",
    "session": "session({args})",
    "context": "context({args})",
    "once": "once({args})",
    "pushif": "push_if({args})",
}
#: Pennant's `Blade::if('feature')` family, which Blade looks up by exact name.
_FEATURE = {"feature": "feature({args})", "unlessfeature": "!feature({args})"}
_LOOPS = {"foreach": "foreach", "for": "for", "while": "while", "forelse": "foreach"}
#: Openers whose block and inline forms share a name; never paired by name.
_NEVER_PAIRED = frozenset({"section", "slot", "php", "verbatim"})

def _is_word(char: str) -> bool:
    """Whether ``char`` is a regex ``\\w`` character in Blade's ASCII sense."""
    return char.isascii() and (char.isalnum() or char == "_")


def _closing_paren(text: str, start: int) -> int | None:
    """The index of the ``)`` closing the ``(`` at ``start``, PHP strings skipped."""
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char in "'\"":
            i += 1
            while i < len(text) and text[i] != char:
                if text[i] == "\\":
                    i += 1
                i += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
            if depth < 0:
                return None
        i += 1
    return None


def _php_close(text: str, start: int) -> int:
    """The index of the ``?>`` ending the PHP opened at ``start``, or -1.

    As PHP's lexer reads it: a ``?>`` inside a quoted string or a ``/* */``
    comment does not end it (an Atom feed's ``<?= '<?xml …?>' ?>``), and one in
    a ``//`` or ``#`` line comment does, quotes or not (``<?php // it's ?>``).
    """
    i = start + 2
    while i < len(text) - 1:
        char = text[i]
        if char in "'\"":
            i += 1
            while i < len(text) and text[i] != char:
                if text[i] == "\\":
                    i += 1
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                return -1
            i = end + 1
        elif text.startswith("//", i) or (char == "#" and not text.startswith("#[", i)):
            while i < len(text) and text[i] != "\n":
                if text.startswith("?>", i):
                    return i
                i += 1
            continue
        elif text.startswith("?>", i):
            return i
        i += 1
    return -1


def _block(text: str, i: int, open_: str, close: str) -> int | None:
    """The end of ``open_ … close`` at ``i``, at the FIRST ``close``, or None."""
    if not text.startswith(open_, i):
        return None
    at = text.find(close, i + len(open_))
    return None if at < 0 else at + len(close)


def _modifier(text: str, at: int) -> bool:
    """Whether a ``.mod`` event modifier starts at ``at``."""
    first = text[at + 1 : at + 2]
    return text[at : at + 1] == "." and first.isascii() and first.isalpha()


def _attribute_end(text: str, name: str, j: int) -> int | None:
    """Where an Alpine or Vue event attribute ends when ``@name`` (its name
    ending at ``j``) is one, or ``None`` for a directive. Blade leaves a name it
    does not know as text:

    * ``@click="…"``: the name is followed by ``=``;
    * ``@submit.prevent="…"``: by a ``.`` modifier, for a name that is not a
      built-in;
    * ``@modal-ok.window="…"``, ``@clock-changed="…"``: by ``-word`` segments and
      then ``=`` or a modifier, for the same names. Blade's name is ``\\w+``, so
      it reads ``@modal`` and leaves it as text, as for ``@click``.
    """
    if text[j : j + 1] == "=":
        return j
    known = builtin(name, None) is not None
    if _modifier(text, j) and not known:
        return j
    end = j
    while text[end : end + 1] == "-" and end + 1 < len(text) and _is_word(text[end + 1]):
        end += 1
        while end < len(text) and _is_word(text[end]):
            end += 1
    if end > j and not known and (text[end : end + 1] == "=" or _modifier(text, end)):
        return end
    return None


def _tokens(text: str, registered: frozenset[str] = frozenset()) -> list[tuple[str, str, str | None]]:
    """``(kind, name or code, args)`` in order: ``("code", php, None)``,
    ``("echo", expr, None)`` or ``("directive", name, args)``."""
    out: list[tuple[str, str, str | None]] = []
    n = len(text)
    i = 0
    while i < n:
        char = text[i]
        if char not in "@{<":
            i += 1
            continue
        after_at = i > 0 and text[i - 1] == "@"
        if char == "@" and not after_at:
            end = _block(text, i, "@verbatim", "@endverbatim")
            if end is not None:
                i = end
                continue
            end = _block(text, i, "@php", "@endphp")
            if end is not None:
                out.append(("code", text[i + len("@php") : end - len("@endphp")], None))
                i = end
                continue
        end = _block(text, i, "{{--", "--}}")
        if end is not None:
            i = end
            continue
        if text.startswith(("<?php", "<?="), i):
            close = _php_close(text, i)
            stop = n if close < 0 else close
            code = text[i + 5 : stop] if text.startswith("<?php", i) else f"echo {text[i + 3 : stop]};"
            out.append(("code", code, None))
            i = n if close < 0 else close + 2
            continue
        end = _block(text, i, "@{!!", "!!}") or _block(text, i, "@{{", "}}")
        if end is not None:
            i = end
            continue
        for open_, close, raw in (("{!!", "!!}", True), ("{{{", "}}}", False), ("{{", "}}", False)):
            end = _block(text, i, open_, close)
            if end is not None:
                expr = text[i + len(open_) : end - len(close)].strip()
                if expr:
                    out.append(("echo", expr if raw else f"e({expr})", None))
                i = end
                break
        else:
            end = None
        if end is not None:
            continue
        if char == "@" and (i == 0 or not _is_word(text[i - 1])):
            if i + 1 < n and text[i + 1] == "@":
                j = i + 2
                while j < n and _is_word(text[j]):
                    j += 1
                i = j
                continue
            j = i + 1
            while j < n and _is_word(text[j]):
                j += 1
            if j == i + 1:
                i += 1
                continue
            name = text[i + 1 : j]
            # A name the project registers is its directive, whatever follows.
            end = None if name in registered else _attribute_end(text, name, j)
            if end is not None:
                i = end
                continue
            k = j
            while k < n and text[k] in " \t":
                k += 1
            args = None
            if k < n and text[k] == "(":
                close_at = _closing_paren(text, k)
                if close_at is not None:
                    args = text[k + 1 : close_at]
                    j = close_at + 1
            out.append(("directive", name, args))
            i = j
            continue
        i += 1
    return out


def code_view(source: str) -> str:
    """The PHP ``source``, a Blade view, compiles to, as far as the gates need it.

    Starts with ``<?php`` because the ``php`` profile's grammar is the
    HTML-aware one. Each piece is on its own line.
    """
    tokens = _tokens(source)
    closers = {
        name.lower()[3:]
        for kind, name, _ in tokens
        if kind == "directive" and name.lower().startswith("end") and len(name) > 3
    }
    lines = ["<?php"]
    # What each open block closes with: `endif`, `endforeach`, … so a closer
    # that names its own block (`@endauth`) writes the PHP one.
    for kind, name, args in tokens:
        if kind == "code":
            # `?>` ends a statement the way `;` does, so one follows the code.
            lines.extend((name, ";"))
            continue
        if kind == "echo":
            lines.append(f"echo {name};")
            continue
        lower = name.lower()
        a = args if args is not None else ""
        if name in _FEATURE:
            lines.append(f"if ({_FEATURE[name].format(args=a)}):")
        elif name == "elsefeature":
            lines.append(f"elseif (feature({a})):")
        elif lower == "else":
            lines.append("else:")
        elif lower == "elseif":
            lines.append(f"elseif ({a}):")
        elif lower in _CONDITIONALS and not (lower == "empty" and args is None):
            lines.append(f"if ({_CONDITIONALS[lower].format(args=a)}):")
        elif lower in _LOOPS:
            lines.append(f"{_LOOPS[lower]} ({a}):")
        elif lower == "empty":  # `@forelse … @empty … @endforelse`
            lines.append("endforeach; if (true):")
        elif lower == "endforelse":
            lines.append("endif;")
        elif lower in ("endforeach", "endfor", "endwhile"):
            lines.append(f"{lower};")
        elif lower == "switch":
            lines.append(f"switch ({a}):")
        elif lower == "case":
            lines.append(f"case {a}:")
        elif lower == "default":
            lines.append("default:")
        elif lower in ("break", "continue"):
            lines.append(f"if ({a}) {lower};" if args else f"{lower};")
        elif lower == "endswitch":
            lines.append("endswitch;")
        elif (known := builtin(name, args)) is not None:
            # The remaining built-ins, by what they compile to: a function block
            # (`@section('c')`, `@push('s')`) as a block, its inline form as nothing.
            kind = known[0]
            if kind in (BLOCK_OPEN, IF_OPEN):
                lines.append(f"if ({lower}({a})):")
            elif kind in (BLOCK_CLOSE, IF_CLOSE):
                lines.append("endif;")
            elif kind == IF_RUNG:
                lines.append(f"elseif ({lower[4:]}({a})):")
            elif lower == "php" and args is not None:
                lines.append(f"({args});")
        elif undecided(name, args):
            pass  # decided at run time, inline as written: nothing to close
        elif lower.startswith("else") and len(lower) > 4:
            # `@elsecan`, `@elseauth`, a `Blade::if` conditional's `@elsename`.
            lines.append(f"elseif ({lower[4:]}({a})):")
        elif lower.startswith("unless") and lower[6:] in closers:
            lines.append(f"if (!{lower[6:]}({a})):")
        elif lower.startswith("end") and len(lower) > 3 and lower[3:] not in _NEVER_PAIRED:
            lines.append("endif;")
        elif lower in closers and lower not in _NEVER_PAIRED:
            # A `Blade::if` custom conditional, or any block its file closes.
            lines.append(f"if ({lower}({a})):")
        elif lower == "php" and args is not None:
            lines.append(f"({args});")
    return "\n".join(lines) + "\n"


#: Loops, which pair with a closer of their own.
_LOOP_CLOSERS = {"foreach": "endforeach", "for": "endfor", "while": "endwhile",
                 "forelse": "endforelse", "switch": "endswitch"}
_ALT_HEADER = re.compile(r"\b(if|foreach|for|while|switch|declare)\s*\(", re.IGNORECASE)
_ALT_CLOSER = re.compile(r"\bend(?:if|foreach|for|while|switch|declare)\b", re.IGNORECASE)
_PHP_STRING_OR_COMMENT = re.compile(
    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|//[^\n]*|#[^\n]*|/\*.*?\*/", re.DOTALL
)

# ---------------------------------------------------------------------------
# The built-in directives, as the gate knows them
# ---------------------------------------------------------------------------
#
# Written here, not taken from the engine: a check that read the transform's
# own table would share its blind spots. What each built-in compiles to, by
# Laravel's `BladeCompiler` (and Pennant's `featureany` family). Anything not
# here may be a `Blade::if` or `Blade::directive` doing anything at all.

IF_OPEN, IF_RUNG, IF_CLOSE, BLOCK_OPEN, BLOCK_CLOSE, SEPARATOR, INLINE = (
    "if-open", "if-rung", "if-close", "block-open", "block-close", "separator", "inline",
)

_IF_OPENERS = frozenset({
    "if", "unless", "auth", "guest", "env", "production", "isset", "can", "cannot",
    "canany", "hassection", "sectionmissing", "error", "session", "context", "once",
    "pushif",
})
_IF_RUNGS = frozenset({
    "else", "elseif", "elseauth", "elseguest", "elsecan", "elsecannot", "elsecanany",
})
_IF_CLOSERS = frozenset({
    "endif", "endunless", "endauth", "endguest", "endenv", "endproduction", "endisset",
    "endempty", "endcan", "endcannot", "endcanany", "enderror", "endsession",
    "endcontext", "endonce", "endpushif",
})
#: Registered with `Blade::if` / `Blade::directive`, an array lookup by EXACT
#: name (Laravel's own are compile methods, which PHP finds case-blind). Pennant's
#: `feature` and `featureany` families, and Livewire 3's directives: each of
#: those compiles to whole statements or markup (`@persist` is a `<div>`,
#: `@script` an `ob_start()`) and opens no PHP block, so all are inline.
_EXACT = {
    "feature": IF_OPEN, "unlessfeature": IF_OPEN, "elsefeature": IF_RUNG, "endfeature": IF_CLOSE,
    "featureany": IF_OPEN, "unlessfeatureany": IF_OPEN, "elsefeatureany": IF_RUNG,
    "endfeatureany": IF_CLOSE,
    **dict.fromkeys(
        (
            "livewire", "livewireStyles", "livewireScripts", "livewireScriptConfig", "assets",
            "endassets", "script", "endscript", "persist", "endpersist", "teleport",
            "endteleport", "entangle", "this",
        ),
        INLINE,
    ),
}
_BLOCKS = frozenset({
    "foreach", "for", "while", "forelse", "switch", "component", "componentfirst",
    "fragment", "pushonce", "prependonce",
})
#: A block with ONE argument; see :func:`builtin` for two or three.
_BLOCK_WITH_ONE = frozenset({"section", "push", "prepend", "slot"})
_BLOCK_CLOSERS = {
    "endforeach": "foreach", "endfor": "for", "endwhile": "while", "endforelse": "forelse",
    "endswitch": "switch", "endcomponent": "component", "endcomponentfirst": "componentfirst",
    "endfragment": "fragment", "endpushonce": "pushonce", "endprependonce": "prependonce",
    "endsection": "section", "show": "section", "stop": "section", "append": "section",
    "overwrite": "section", "endpush": "push", "endprepend": "prepend", "endslot": "slot",
    "endlang": "lang",
}
_SEPARATORS = frozenset({"case", "default", "break", "continue"})
_INLINE = frozenset({
    "include", "includeif", "includewhen", "includeunless", "includefirst", "each",
    "extends", "extendsfirst", "yield", "parent", "csrf", "method", "json", "js", "class",
    "style", "checked", "selected", "disabled", "readonly", "required", "dd", "dump",
    "stack", "props", "aware", "inject", "vite", "vitereactrefresh", "use", "choice", "php",
})


def _argument_count(args: str | None) -> int:
    """Top-level arguments in ``args``; strings and brackets skipped."""
    if args is None or not args.strip():
        return 0
    depth, count, i = 0, 1, 0
    while i < len(args):
        char = args[i]
        if char in "'\"":
            i += 1
            while i < len(args) and args[i] != char:
                if args[i] == "\\":
                    i += 1
                i += 1
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            count += 1
        i += 1
    return count


def _arguments(args: str | None) -> list[str]:
    """The top-level arguments of ``args``, stripped; strings and brackets skipped."""
    if args is None or not args.strip():
        return []
    found, depth, start, i = [], 0, 0, 0
    while i < len(args):
        char = args[i]
        if char in "'\"":
            i += 1
            while i < len(args) and args[i] != char:
                if args[i] == "\\":
                    i += 1
                i += 1
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            found.append(args[start:i].strip())
            start = i + 1
        i += 1
    found.append(args[start:].strip())
    return found


def _string_literal(text: str) -> bool | None:
    """Whether a PHP string literal is non-empty; None when ``text`` is not one
    (or interpolates, so its value is not known here)."""
    if len(text) < 2 or text[0] not in "'\"" or text[-1] != text[0]:
        return None
    inner = text[1:-1]
    if text[0] in inner or (text[0] == '"' and ("$" in inner or "{" in inner)):
        return None
    return bool(inner)


def builtin(name: str, args: str | None) -> tuple[str, str | None] | None:
    """``(kind, block class)`` for a built-in directive, ``None`` for anything else.

    Arguments decide some, as Laravel's runtime does: ``@section``, ``@push``,
    ``@prepend`` and ``@slot`` open a block with one argument; with two,
    ``@section`` opens one only for a ``null`` content, ``@push``/``@prepend``
    only for ``''``, and ``@slot`` never; ``@slot('x', null, [...])`` opens one.
    ``@lang`` opens one with no argument or an array. A content that is not a
    literal cannot be decided, so the directive is not a built-in.
    """
    if name in _EXACT:
        return _EXACT[name], None
    lower = name.lower()
    arguments = _arguments(args)
    second = arguments[1] if len(arguments) > 1 else None
    if lower == "empty":
        return (IF_OPEN, None) if args is not None else (SEPARATOR, None)
    if lower in _IF_OPENERS:
        return IF_OPEN, None
    if lower in _IF_RUNGS:
        return IF_RUNG, None
    if lower in _IF_CLOSERS:
        return IF_CLOSE, None
    if lower in _BLOCKS:
        return BLOCK_OPEN, lower
    if lower in _BLOCK_WITH_ONE:
        if len(arguments) == 1:
            return BLOCK_OPEN, lower
        literal = _string_literal(second) if second is not None else None
        if lower == "section" and len(arguments) == 2:
            if literal is not None:
                return INLINE, None
            return (BLOCK_OPEN, "section") if second == "null" else None
        if lower in ("push", "prepend") and len(arguments) == 2:
            if literal is None:
                return None
            return (INLINE, None) if literal else (BLOCK_OPEN, lower)
        if lower == "slot" and len(arguments) == 2:
            return INLINE, None
        if lower == "slot" and len(arguments) == 3 and second == "null":
            return BLOCK_OPEN, "slot"
        return None
    if lower == "lang":
        if args is None or not arguments or (args or "").startswith("["):
            return BLOCK_OPEN, "lang"
        return INLINE, None
    if lower in _BLOCK_CLOSERS:
        return BLOCK_CLOSE, _BLOCK_CLOSERS[lower]
    if lower in _SEPARATORS:
        return SEPARATOR, None
    if lower in _INLINE:
        return INLINE, None
    return None


#: Directive names the views model; a project's own directive may not replace one.
_MODELLED = frozenset({
    "if", "elseif", "else", "endif", "unless", "endunless", "feature", "unlessfeature",
    "elsefeature", "endfeature",
})


@functools.cache
def _php_parser():
    import tree_sitter_php
    from tree_sitter import Language, Parser

    return Parser(Language(tree_sitter_php.language_php()))


#: What may stand at the top level of PHP a folded conditional holds (raw PHP,
#: or what a project's directive compiles to): whole statements that cannot
#: open or close a block across the fragment's edges.
_WHOLE_TOP_LEVEL = frozenset({
    "php_tag", "text_interpolation", "text", "comment", "echo_statement",
    "expression_statement", "unset_statement", "compound_statement", "if_statement",
    "foreach_statement", "for_statement", "while_statement",
})
#: Never anywhere in it. tree-sitter-php reads a lone `else:`, `ELSE :` or
#: `default:` as a goto label, without an error.
_NEVER_IN_WHOLE = frozenset({"named_label_statement", "goto_statement", "colon_block"})
#: A name spelling one of these is the alternative syntax read as a constant
#: (`endif;` parses alone).
_ALT_KEYWORDS = frozenset({
    "else", "elseif", "endif", "endforeach", "endfor", "endwhile", "endswitch", "enddeclare",
})


def whole_statements(php: str) -> bool:
    """Whether ``php``, a whole PHP file (opening with its own ``<?php``), is
    only whole statements: no ERROR or MISSING node, every top-level statement
    a kind in :data:`_WHOLE_TOP_LEVEL`, and no label, ``goto``, alternative-
    syntax body or alternative-syntax keyword spelled as a name anywhere. A
    WHITELIST: anything it does not name is refused."""
    tree = _php_parser().parse(php.encode("utf-8"))
    root = tree.root_node
    if root.has_error or any(child.type not in _WHOLE_TOP_LEVEL for child in root.children):
        return False
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _NEVER_IN_WHOLE:
            return False
        if node.type == "name" and (node.text or b"").decode("utf-8").lower() in _ALT_KEYWORDS:
            return False
        stack.extend(node.children)
    return True


#: The name a raw PHP block (`<?php … ?>`, `@php … @endphp`) takes among the
#: directives :func:`whitelist_violation` aligns; no directive can spell it.
_RAW_PHP = "\0php"


def custom_kind(
    name: str, args: str | None, custom: Mapping[str, str | None] | None
) -> tuple[str, str | None] | None:
    """:func:`builtin`, knowing what the project's own directives compile to.

    Blade looks a project's directive up first, by exact name, so it wins over
    a built-in of the same name. One whose text, with this use's argument, is
    :func:`whole_statements` is inline; one registered in a way that could not
    be read (``None``), one replacing a directive the views model, and any
    other use, are unknown. A raw PHP block (:data:`_RAW_PHP`) is inline when
    it is whole statements.
    """
    if name == _RAW_PHP:
        return (INLINE, None) if whole_statements(f"<?php\n{args or ''}\n") else None
    if custom and name in custom:
        compiled = custom[name]
        if compiled is None or name.lower() in _MODELLED:
            return None
        compiled = compiled.replace(_ARGUMENT, (args or "").strip())
        return (INLINE, None) if whole_statements(f"<?php\n?>{compiled}<?php\n") else None
    return builtin(name, args)


def undecided(name: str, args: str | None) -> bool:
    """Whether ``@name(args)`` is a function-block directive written with a
    content argument that is not a literal: ``@section('t', $title)``,
    ``@push('s', $html)``, ``@slot('x', $c, [])``.

    Laravel decides at RUN time (``startSection`` buffers when the content is
    ``null``), so the table cannot call it a block or inline. It is not a
    built-in (inside a span a rewrite reached, it still refuses), but it is no
    custom opener either: it never pairs a closer by name, so it adds nothing to
    the block count. Written inline, as every such use is, it holds no body.
    """
    lower = name.lower()
    return (
        lower in _BLOCK_WITH_ONE
        and builtin(name, args) is None
        and len(_arguments(args)) > 1
    )


def _directives(source: str) -> list[tuple[str, str | None]]:
    return [(name, args) for kind, name, args in _tokens(source) if kind == "directive"]


def _items(source: str, registered: frozenset[str]) -> list[tuple[str, str | None]]:
    """The directives, and each raw PHP block as ``(_RAW_PHP, code)``, in order.
    A name the project registers is never read as an Alpine attribute."""
    return [
        (name, args) if kind == "directive" else (_RAW_PHP, name)
        for kind, name, args in _tokens(source, registered)
        if kind in ("directive", "code")
    ]


def closers(source: str) -> frozenset[str]:
    """The names ``x`` whose ``@endx`` the Blade ``source`` spells, lower-cased."""
    return frozenset(
        name.lower()[3:]
        for name, _ in _directives(source)
        if name.lower().startswith("end") and len(name) > 3
    )


def _alt_balance(code: str) -> int:
    """Alternative-syntax openers minus closers in raw PHP ``code``."""
    code = _PHP_STRING_OR_COMMENT.sub(" ", code)
    opened = 0
    for match in _ALT_HEADER.finditer(code):
        close = _closing_paren(code, match.end() - 1)
        if close is not None and code[close + 1 :].lstrip().startswith(":"):
            opened += 1
    return opened - len(_ALT_CLOSER.findall(code))


def balance(
    source: str,
    names: frozenset[str] = frozenset(),
    directives: Mapping[str, str | None] | None = None,
) -> int:
    """Block openers minus block closers in a Blade view, by counting directives.

    The grammar cannot be asked: tree-sitter-php reads a bare ``endif;`` without
    an error, so a code view that lost its OPENER still parses. Built-ins count
    by what they compile to, argument-aware (``@push('s')`` opens a block,
    ``@push('s', 'x')`` does not). A name that is not a built-in counts as an
    opener when ``@endname`` is in the file or in ``names`` (with its
    ``@unlessname``), and its ``@endname`` as a closer. Raw PHP counts its
    alternative-syntax blocks. A view Blade can compile scores zero; a correct
    fold removes an opener and its closer together. ``directives`` are the
    project's own (:func:`custom_kind`): one proven inline counts nothing.
    """
    tokens = _tokens(source, frozenset(directives or ()))
    custom = names | frozenset(
        name.lower()[3:]
        for kind, name, _ in tokens
        if kind == "directive" and name.lower().startswith("end") and len(name) > 3
    )
    score = 0
    for kind, name, args in tokens:
        if kind == "code":
            score += _alt_balance(name)
            continue
        if kind != "directive":
            continue
        known = custom_kind(name, args, directives)
        if known is None and undecided(name, args):
            continue
        if known is not None:
            score += {IF_OPEN: 1, BLOCK_OPEN: 1, IF_CLOSE: -1, BLOCK_CLOSE: -1}.get(known[0], 0)
            continue
        lower = name.lower()
        if lower in custom or (lower.startswith("unless") and lower[6:] in custom):
            score += 1
        elif lower.startswith("end") and lower[3:] in custom:
            score -= 1
    return score


def _spans(
    directives: list[tuple[str, str | None]], custom: Mapping[str, str | None] | None = None
) -> tuple[dict[int, int], dict[int, int]]:
    """``(pairs, parents)``: each built-in opener's closer index, and each
    directive's innermost enclosing ``if`` opener, paired by built-ins alone.
    A name that is not a built-in is ignored here; what matters is that it is
    inside a span, which is asked separately."""
    pairs: dict[int, int] = {}
    parents: dict[int, int] = {}
    stack: list[tuple[int, str | None]] = []  # (index, block class; None for an `if`)
    for index, (name, args) in enumerate(directives):
        ifs = [at for at, cls in stack if cls is None]
        if ifs:
            parents[index] = ifs[-1]
        known = custom_kind(name, args, custom)
        if known is None:
            continue
        kind, cls = known
        if kind == IF_OPEN:
            stack.append((index, None))
        elif kind == BLOCK_OPEN:
            stack.append((index, cls))
        elif kind == IF_CLOSE and stack and stack[-1][1] is None:
            pairs[stack.pop()[0]] = index
        elif kind == BLOCK_CLOSE and stack and stack[-1][1] == cls:
            pairs[stack.pop()[0]] = index
    return pairs, parents


def whitelist_violation(
    before: str, after: str, custom: Mapping[str, str | None] | None = None
) -> str | None:
    """The first directive outside the built-in table the rewrite reached, or None.

    A rewrite reaches a directive it DELETED, and every directive between a
    conditional's opener and closer when it deleted any part of that
    conditional (its opener, a rung, its closer, or a directive inside it). A
    non-built-in there may own an ``@else`` or a closer the fold attributed to
    the flag's conditional, so the rewrite cannot be trusted, whatever the
    engine decided. A project's own directive in ``custom`` counts as a
    built-in only where :func:`custom_kind` proves that use inline.
    """
    registered = frozenset(custom or ())
    old, new = _items(before, registered), _items(after, registered)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    deleted = [
        index
        for tag, i1, i2, _, _ in matcher.get_opcodes()
        if tag in ("delete", "replace")
        for index in range(i1, i2)
    ]
    if not deleted:
        return None
    pairs, parents = _spans(old, custom)
    closers_of = {closer: opener for opener, closer in pairs.items()}
    for index in deleted:
        name, args = old[index]
        known = custom_kind(name, args, custom)
        if known is None:
            return _spelled(name)
        if known[0] == IF_OPEN:
            opener = index
        elif known[0] == IF_CLOSE and index in closers_of:
            opener = closers_of[index]
        else:
            opener = parents.get(index)
        if opener is None:
            continue
        end = pairs.get(opener, len(old) - 1)
        for inner in range(opener, end + 1):
            if custom_kind(*old[inner], custom) is None:
                return _spelled(old[inner][0])
    return None


def _spelled(name: str) -> str:
    return "raw PHP that is not whole statements" if name == _RAW_PHP else f"@{name}"
