"""Files outside PHP that can still read a PHP constant.

A Twig, Latte, Smarty or Phalcon Volt template reads one with ``constant('Ns\\\\Owner::NAME')``
(Smarty also as ``{Owner::NAME}``), and Symfony configuration does with
``!php/const`` (``!php/enum`` for an enum case) in YAML or ``type="constant"``
in XML. This tool transforms none of them and parses none of them, so a
mention in one is never proven: this judge only refuses, by tokens, failing
closed. The cost of a wrong guess is one constant left in place, named in the
pull request.

A configuration file is a reader only when it holds a constant marker; YAML
and XML are everywhere and mostly spell nothing PHP can read. Files that hold
PHP under a suffix the runner does not transform (Drupal's ``.theme`` and
``.install``, CakePHP's ``.ctp``), and Nette's ``.neon``, are judged like the
templates.
"""

from __future__ import annotations

import re

from flag_cleanup import cross_file_keys as xk

#: Listed for the cross-file scan and judged by :func:`references`.
#: Files holding PHP under a suffix the runner does not transform (Drupal's
#: ``.theme``, ``.install``, ``.profile`` and ``.engine``, CakePHP's ``.ctp``,
#: ``.php7``) and Nette DI configuration, which names constants bare.
_PHP_ELSEWHERE = (".theme", ".install", ".profile", ".engine", ".ctp", ".php7", ".neon")
#: Listed for the cross-file scan and judged by :func:`references`.
PHP_GUEST_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "php_guest": (".twig", ".latte", ".tpl", ".volt", ".yaml", ".yml", ".xml", *_PHP_ELSEWHERE),
}
_TEMPLATES = (".twig", ".latte", ".tpl", ".volt")
#: Judged whatever their text: no marker is needed to name a constant.
_ALWAYS = _TEMPLATES + _PHP_ELSEWHERE
#: What makes a configuration file name a PHP constant or enum case at all.
#: Symfony YAML reads a case as ``!php/enum Ns\Owner::CASE`` (``->value`` for
#: its backing value), and an ExpressionLanguage expression (``@=...`` in YAML,
#: ``type="expression"`` in XML) as ``enum('Ns\Owner::CASE')``, the enum
#: counterpart of ``constant(...)``.
_CONSTANT_MARKER = re.compile(
    r"""!php/const\b|!php/enum\b|type\s*=\s*["']constant["']|\b(?:constant|enum)\s*\("""
)


def _owners(decl: xk.KeyDeclaration, repo_dir: str) -> set[str]:
    """Short names through which a file can reach ``decl``: the owner's, and
    every repository class that may inherit it."""
    from flag_cleanup import cross_file_php

    owner = decl.owner or ""
    fqn = cross_file_php._join(decl.module, [owner])
    index = cross_file_php.php_index(repo_dir)
    return {owner} | {
        name.rsplit("\\", 1)[-1] for name in index.by_fqn if name != fqn and index.reaches(name, fqn)
    }


def references(
    path: str,
    source: str,
    declarations: tuple[xk.KeyDeclaration, ...],
    accessors: frozenset[str],
    repo_dir: str,
) -> tuple[list[xk.KeyReference], list[xk.Refusal]]:
    """Never a read; a refusal at the first line that may name a constant.

    The name has to be spelled, and for a class constant one of its owner's
    names too, case-insensitively as PHP compares class names. A namespace
    constant needs only its name.
    """
    if not path.lower().endswith(_ALWAYS) and _CONSTANT_MARKER.search(source) is None:
        return [], []
    refused: list[xk.Refusal] = []
    lines = source.splitlines()
    for decl in declarations:
        hit = xk.mention_pattern([decl.name]).search(source)
        if hit is None:
            continue
        # Twig's `constant('NAME', obj)` reads the constant of obj's class,
        # which may implement or extend the owner without its name spelled.
        by_object = re.search(r"""constant\(\s*['"]%s['"]\s*,""" % re.escape(decl.name), source)
        if decl.owner is not None and by_object is None:
            owners = re.compile(xk.mention_pattern(_owners(decl, repo_dir)).pattern, re.IGNORECASE)
            if owners.search(source) is None:
                continue
        line = source.count("\n", 0, hit.start()) + 1
        text = lines[line - 1].strip() if line <= len(lines) else ""
        refused.append(xk.Refusal(path, line, xk.UNSUPPORTED_FORM, text, decl.entry))
    return [], refused


def constant_escapes(
    path: str, source: str, constants: tuple[xk.KeyDeclaration, ...], repo_dir: str
) -> list[tuple[str, xk.Refusal]]:
    """A class taken whole by text (``constant('Ns\\\\Owner::' ~ name)``,
    ``Owner::class``), in a file :func:`references` would judge."""
    if not path.lower().endswith(_ALWAYS) and _CONSTANT_MARKER.search(source) is None:
        return []
    from flag_cleanup import cross_file_php

    return cross_file_php.template_escapes(path, source, constants, repo_dir)


def declarations(path: str, source: str, flag_key: str) -> list[xk.KeyDeclaration]:
    return []


def declared_names(path: str, source: str) -> set[xk.QualifiedName]:
    return set()


def declared_classifiers(path: str, source: str) -> set[tuple[str, str]]:
    return set()


xk.register(
    "php_guest",
    xk.Profile(
        language_family=frozenset({"php_guest"}),
        declarations=declarations,
        references=references,
        declared_names=declared_names,
        declared_classifiers=declared_classifiers,
        # A template can build a constant's name from its class's at run time.
        constant_escapes=constant_escapes,
    ),
)
