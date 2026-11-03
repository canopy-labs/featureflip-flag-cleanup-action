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
import re

__all__ = ["balance", "builtin", "closers", "code_view", "whitelist_violation"]

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

    A ``?>`` inside a quoted string does not end it (an Atom feed's
    ``<?= '<?xml …?>' ?>``).
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


def _tokens(text: str) -> list[tuple[str, str, str | None]]:
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
            # `@click="…"`, `@submit.prevent="…"`: an Alpine or Vue event
            # attribute, which Blade leaves as text.
            following = text[j : j + 2]
            if following[:1] == "=" or (
                following[:1] == "." and following[1:].isalpha() and builtin(name, None) is None
            ):
                i = j
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


def _directives(source: str) -> list[tuple[str, str | None]]:
    return [(name, args) for kind, name, args in _tokens(source) if kind == "directive"]


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


def balance(source: str, names: frozenset[str] = frozenset()) -> int:
    """Block openers minus block closers in a Blade view, by counting directives.

    The grammar cannot be asked: tree-sitter-php reads a bare ``endif;`` without
    an error, so a code view that lost its OPENER still parses. Built-ins count
    by what they compile to, argument-aware (``@push('s')`` opens a block,
    ``@push('s', 'x')`` does not). A name that is not a built-in counts as an
    opener when ``@endname`` is in the file or in ``names`` (with its
    ``@unlessname``), and its ``@endname`` as a closer. Raw PHP counts its
    alternative-syntax blocks. A view Blade can compile scores zero; a correct
    fold removes an opener and its closer together.
    """
    tokens = _tokens(source)
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
        known = builtin(name, args)
        if known is not None:
            score += {IF_OPEN: 1, BLOCK_OPEN: 1, IF_CLOSE: -1, BLOCK_CLOSE: -1}.get(known[0], 0)
            continue
        lower = name.lower()
        if lower in custom or (lower.startswith("unless") and lower[6:] in custom):
            score += 1
        elif lower.startswith("end") and lower[3:] in custom:
            score -= 1
    return score


def _spans(directives: list[tuple[str, str | None]]) -> tuple[dict[int, int], dict[int, int]]:
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
        known = builtin(name, args)
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


def whitelist_violation(before: str, after: str) -> str | None:
    """The first directive outside the built-in table the rewrite reached, or None.

    A rewrite reaches a directive it DELETED, and every directive between a
    conditional's opener and closer when it deleted any part of that
    conditional (its opener, a rung, its closer, or a directive inside it). A
    non-built-in there may own an ``@else`` or a closer the fold attributed to
    the flag's conditional, so the rewrite cannot be trusted, whatever the
    engine decided.
    """
    old, new = _directives(before), _directives(after)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    deleted = [
        index
        for tag, i1, i2, _, _ in matcher.get_opcodes()
        if tag in ("delete", "replace")
        for index in range(i1, i2)
    ]
    if not deleted:
        return None
    pairs, parents = _spans(old)
    closers_of = {closer: opener for opener, closer in pairs.items()}
    for index in deleted:
        name, args = old[index]
        known = builtin(name, args)
        if known is None:
            return f"@{name}"
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
            if builtin(*old[inner]) is None:
                return f"@{old[inner][0]}"
    return None
