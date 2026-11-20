"""A key constant whose declaration the run keeps: do its reads still fold?

Gate 2 (:mod:`flag_cleanup.key_const`) withholds a file whose key constant has
any use the const rules do not themselves remove, because the delete rule
would strand it. When the constant cannot be given another value, that use is
no reason to leave the READS: the run folds them with the delete rules
stripped, keeps the declaration, and every other use keeps resolving
(:func:`fold_refusal`). Where it cannot prove that, the file is withheld whole,
and :func:`withheld_message` says so by name: a withheld file with a flag read
left in it must never read like a file in which nothing matched.

Per language, what makes the kept constant hold its key at every read:

* Dart, Go, TypeScript, JavaScript and Java: the language forbids assigning it
  (``const``/``final`` in Dart, ``const`` in Go and the TypeScript family, a
  ``final`` initialised with a string literal in Java, which is a constant
  variable ``javac`` inlines). What is left is name resolution, which the
  profile's ``fold_only_withholds`` checks: every other spelling of the name is
  a read of it, and no read can mean an inherited member.
* Python, which has no constant: the module binds the name once and nothing
  in it reaches its namespace unnamed (``cross_file_python.home_constant_names``),
  and no file of the checkout writes the attribute
  (``cross_file_python.constant_writes``: an attribute assignment, ``setattr``,
  ``mock.patch``, ``monkeypatch.setattr``).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path

from flag_cleanup import key_const
from flag_cleanup.syntax import const_path_is_safe, key_const_names, key_const_withheld

#: Languages whose key constant folds its reads whatever else uses the name.
FOLDS_ANY_KEPT_CONSTANT = frozenset({"dart", "go", "java", "ts", "tsx", "js", "python"})

#: :func:`fold_refusal`'s answer, given by the runner, for a file whose
#: declaration a mention the cross-file resolution reports keeps.
KEPT_ELSEWHERE = (
    "a use of the constant reported with this flag's unresolved mentions keeps "
    "the declaration, and with it the reads"
)

_UNPROVEN = (
    "the run could not prove the constant holds the key at every read (another "
    "file may read it, or the read is bound to a name the run does not follow)"
)


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """Asking the gate again must not repeat what it logged the first time."""
    gate = logging.getLogger(key_const.__name__)
    was = gate.disabled
    gate.disabled = True
    try:
        yield
    finally:
        gate.disabled = was


def fold_refusal(
    path: str, language: str, flag_key: str, accessors: tuple[str, ...], repo_dir: str | None
) -> str | None:
    """Why the reads of ``path``'s key constant may NOT fold with its
    declaration kept, or ``None`` when they may. Only for a language in
    :data:`FOLDS_ANY_KEPT_CONSTANT`."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "the file could not be read"
    if language == "dart" and "dart:mirrors" in source:
        # `cross_file_dart.constant_escapes`: mirrors reach a member by name.
        return "the file imports `dart:mirrors`, which can reach the constant by name"
    with _quiet(), key_const.go_file(path):
        safe = const_path_is_safe(source, language, flag_key, accessors, fold_only=True)
    if not safe:
        with _quiet(), key_const.go_file(path):
            reason = key_const_withheld(source, language, flag_key, accessors, fold_only=True)[1]
        return reason or _UNPROVEN
    if language == "python":
        return _python_refusal(path, source, flag_key, repo_dir)
    return None


def _python_refusal(path: str, source: str, flag_key: str, repo_dir: str | None) -> str | None:
    from flag_cleanup import cross_file_python

    names = set(key_const_names(source, "python", flag_key))
    if not names:
        return None
    listed = ", ".join(sorted(names))
    if not names <= cross_file_python.home_constant_names(path, source, flag_key):
        return (
            f"key constant {listed} may be given another value in this file (the name "
            "is bound again, or the module's namespace is written without naming it: "
            "a star import, `globals()`, `setattr`, `exec`)"
        )
    if repo_dir is None:
        return _UNPROVEN
    written = cross_file_python.constant_writes(repo_dir, names)
    if written:
        return (
            f"key constant {', '.join(sorted(written))} is written from a file of the "
            "checkout (an attribute assignment, `setattr`, `mock.patch` or "
            "`monkeypatch.setattr`), so a read may not hold the key"
        )
    return None


def kept_message(path: str, before: str, language: str, flag_key: str) -> str | None:
    """The log line for a file whose key constant was kept while its reads
    folded, said once the rewrite stands: ``None`` when the constant is gone
    (a later pass removed it once nothing read it), and a line saying so when
    nothing in the file changed."""
    try:
        after = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    names = key_const_names(after, language, flag_key)
    if not names:
        return None
    listed = ", ".join(names)
    if after == before:
        return (
            f"{path}: key constant {listed} is kept, and no read of the flag through it "
            "here could be folded (the file is unchanged: check that the read's accessor "
            "and key position are among the run's accessors, and that the read is one "
            "the tool can prove synchronous)"
        )
    return (
        f"{path}: key constant {listed} is also used outside a flag read or another "
        "file reads it, so its declaration is kept; the reads of it are folded"
    )


def withheld_message(
    path: str,
    language: str,
    flag_key: str,
    accessors: tuple[str, ...],
    refusal: str | None = None,
) -> str | None:
    """The log line for a file Gate 2 withheld whole that still holds a read
    of the flag through a key constant, or ``None`` when it holds none.
    ``refusal`` is :func:`fold_refusal`'s answer for it, where one was asked."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    with _quiet(), key_const.go_file(path):
        reads, reason = key_const_withheld(
            source, language, flag_key, accessors, fold_only=language in FOLDS_ANY_KEPT_CONSTANT
        )
    if not reads:
        return None
    names = ", ".join(dict.fromkeys(name for name, _, _ in reads))
    lines = ", ".join(str(line) for _, line, _ in reads)
    if any(ambiguous for _, _, ambiguous in reads):
        # Which binding each read means is the open question: say so.
        return (
            f"{path}: the flag is read through the name {names} (line {lines}), which may "
            f"or may not mean the key constant of that name, and those reads are not "
            f"folded and the constant is kept: {reason or refusal or _UNPROVEN}"
        )
    return (
        f"{path}: the flag is read through key constant {names} (line {lines}), and "
        f"those reads are not folded and the constant is kept: {reason or refusal or _UNPROVEN}"
    )
