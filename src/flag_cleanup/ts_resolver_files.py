"""Which scripts of a checkout can take part in module resolution.

A bare specifier that names a workspace package is followed to the package's
source only when nothing could send the name elsewhere
(:mod:`flag_cleanup.ts_workspace`). A mention of the name in an ordinary
module cannot; one in code a resolver runs can (a Vite plugin's
``resolveId``, an esbuild ``onResolve``, a Jest ``resolver``, a Node loader
hook, a TypeScript transformer). Guessing that code by its name or by tokens
in it missed every one of those, so capability FLOWS FROM THE ENGINES
instead, and a script is resolver-capable when:

1. a tool loads it by name as its config (:data:`_CONFIG_SCRIPT`:
   ``vite.config.ts``, ``jest.config.js``, ``.babelrc.js``,
   ``metro.config.js``, ``.storybook/main.ts``, ``.pnpmfile.cjs``, ...);
2. a value import of it loads a bundler's or resolver's API
   (:data:`_ENGINES`: Vite, esbuild, Rollup, webpack, Rspack, Parcel, Metro,
   ``jest-resolve``, ``@babel/core``, ``@swc/core``, the TypeScript compiler
   API, ``module-alias``, ``tsconfig-paths``, a TS runner's register entry,
   and ``node:module`` only beside ``register``, ``_resolveFilename`` or
   ``_load``), or it holds a resolution hook (:data:`_HOOKS`). A test
   runner's API, an app framework, ``createRequire`` and a type-only import
   seed nothing;
3. a capable script imports it, directly or through other in-repo modules;
4. a capable script or a resolver config names it by a path in a string
   (a Jest ``resolver``, a plugin path, a ``register()`` argument); or
5. a command hands it to Node: ``--import``, ``--loader``,
   ``--experimental-loader``, ``--require`` or ``-r``, in any file of the
   checkout (``package.json`` scripts, a Dockerfile, a shell script).

Everything here over-approximates: a specifier read off a comment, a string
that names a file nobody loads, every file of a package whose entry cannot be
told, all make more scripts capable, which only refuses more.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

#: Scripts a tool loads by their name as its configuration.
_CONFIG_SCRIPT = re.compile(
    r"^(?:(?:vite|vitest|webpack|rollup|rolldown|rspack|rsbuild|esbuild|tsup|tsdown|jest|babel|metro|next|nuxt"
    r"|astro|svelte|remix|craco|snowpack|parcel|turbo|vue|quasar|electron\.vite|umi|unbuild|build|playwright"
    r"|cypress|karma|ava|wmr|farm|modern|rsdoctor|histoire|ladle|uno|windi)\.(?:config|workspace)(?:\.[\w-]+)?"
    r"\.[cm]?[jt]sx?|\.babelrc\.[cm]?[jt]s|\.swcrc\.[cm]?js|\.pnpmfile\.cjs|config-overrides\.[cm]?js"
    r"|gatsby-(?:node|config|browser|ssr)\.[cm]?[jt]sx?|\.umirc\.[cm]?[jt]s|\.mocharc\.[cm]?js|\.?jest\.preset\.[cm]?js"
    r"|jest-preset\.[cm]?js|metro\.config\.[cm]?js)$",
    re.IGNORECASE,
)
#: A script whose name says it configures a tool: ``webpack.*``, or a tool's
#: name as a word of it (``vite.config.ts``, ``vite.lib.config.ts``,
#: ``rollup.base.js``). An app's own ``env.config.ts`` is not one.
_TOOL_NAME = re.compile(
    r"^webpack\.|(?:^|[.\-_])(?:vite|vitest|jest|rollup|tsup|esbuild|babel|metro|next|nuxt|astro"
    r"|svelte|remix|craco|storybook|parcel|rspack|snowpack)(?:[.\-_]|$)",
    re.IGNORECASE,
)
#: Packages that resolve modules, or let code that runs inside them do so.
_ENGINES = frozenset(
    {
        "vite", "esbuild", "rollup", "webpack", "parcel", "metro", "jest-resolve", "@babel/core", "@swc/core",
        "typescript", "module-alias", "tsconfig-paths", "ts-node",
    }
)
#: Engines published under a scope or a name family.
_ENGINE_PREFIXES = ("@rollup/", "@rspack/", "@parcel/", "metro-", "@babel/")
#: Register entry points of TypeScript runners.
_REGISTER_ENTRIES = frozenset({"tsx/esm", "tsx/cjs", "tsx/esm/api", "tsx/cjs/api"})
#: Node's module API, an engine only beside one of its resolution hooks
#: (``createRequire`` alone loads; it does not redirect).
_NODE_MODULE = frozenset({"module", "node:module"})
_NODE_HOOKS = re.compile(
    rb"""\{[^}]*\bregister\b[^}]*\}\s*(?:from\s*|=\s*require\s*\(\s*)["'](?:node:)?module["']"""
    rb"|\.\s*register\s*\(|_resolveFilename|\b_load\b"
)
#: A resolution hook, wherever it stands (an extra trigger, cheap).
_HOOKS = re.compile(rb"module-alias|addAlias|_resolveFilename|tsconfig-paths|Module\._load|NormalModuleReplacementPlugin")
#: A type-only import or re-export: erased, so it loads nothing.
_TYPE_ONLY = re.compile(rb"""\b(?:import|export)\s+type\s+[^;'"`]*?\bfrom\s*(["'])([^"'\n]+)\1""")
#: A resolver hook's name: a seed only beside a type-only import of an
#: engine (a plugin typed against it), since the names are common words.
_TYPED_HOOKS = re.compile(rb"resolveId\s*\(|onResolve\s*\(|resolveRequest|resolveSync")
#: A command-line option naming a tool's config file.
_CONFIG_FLAG = re.compile(
    rb"""(?:--config(?:File|-file)?|(?<![\w-])-c)(?:=|\s+)(["']?)([^\s"';&|]+)\1"""
)
#: A specifier a script loads (over-matching: comments and strings count too).
_SPECIFIER = re.compile(
    rb"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+|\brequire\.resolve\s*\(\s*)"""
    rb"""(["'`])([^"'`\n$]+)\1"""
)
#: A quoted string that may be a path to a file.
_STRING = re.compile(rb"""(["'`])([^"'`\n]{2,300})\1""")
#: A command-line option handing Node a module to load first.
_NODE_FLAG = re.compile(
    rb"""(?:--import|--loader|--experimental-loader|--require|(?<![\w-])-r)(?:=|\s+)(["']?)([^\s"';&|]+)\1"""
)
_SCRIPTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro")
#: Files that run commands: a manifest's scripts, Nx targets, a Makefile, a
#: shell script, a Dockerfile, CI config (see :func:`_ci_file`).
_COMMAND_FILE = re.compile(
    r"^(?:package\.json|project\.json|nx\.json|turbo\.json|Makefile.*|.*\.mk|.*\.(?:sh|bash|zsh)|Dockerfile.*"
    r"|.*\.dockerfile|Procfile|justfile|Justfile|Taskfile\.ya?ml|.*\.toml|\.gitlab-ci\.ya?ml"
    r"|bitbucket-pipelines\.ya?ml|docker-compose.*\.ya?ml|compose\.ya?ml)$"
)
_EXTENSIONS = ("", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
#: Larger files are not read for Node options: no command line is that long.
_MAX_TEXT = 4 * 1024 * 1024


def engine(specifier: str) -> bool:
    """Whether ``specifier`` loads a bundler's or resolver's API (see the
    module docstring); ``node:module`` is judged with its file
    (:func:`_seeds_engine`)."""
    return (
        specifier in _ENGINES
        or specifier.startswith(_ENGINE_PREFIXES)
        or specifier in _REGISTER_ENTRIES
        or any(specifier.startswith(f"{e}/") for e in _ENGINES)
    )


def _seeds_engine(data: bytes) -> str | None:
    """The engine a script's value imports load, if one: type-only imports
    are erased, and ``node:module`` counts only beside a resolution hook."""
    erased = [m.span() for m in _TYPE_ONLY.finditer(data)]
    for match in _SPECIFIER.finditer(data):
        if any(start <= match.start() < end for start, end in erased):
            continue
        spec = match.group(2).decode("utf-8", "replace")
        if engine(spec) or (spec in _NODE_MODULE and _NODE_HOOKS.search(data)):
            return spec
    return None


def _files_named(base: Path) -> list[Path]:
    """Every existing file ``base`` may name: itself, with a script extension,
    or as a directory's ``index``. Over-matches on purpose."""
    found = []
    for ext in _EXTENSIONS:
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            found.append(candidate)
    for ext in _EXTENSIONS[1:]:
        candidate = base / f"index{ext}"
        if candidate.is_file():
            found.append(candidate)
    if base.suffix in (".js", ".jsx", ".mjs", ".cjs"):
        stem = base.with_suffix("")
        for ext in (".ts", ".tsx", ".mts", ".cts"):
            candidate = Path(str(stem) + ext)
            if candidate.is_file():
                found.append(candidate)
    return found


def _package_root(path: Path, stop: Path) -> Path:
    """The nearest directory above ``path`` holding a ``package.json``."""
    current = path.parent
    while True:
        if (current / "package.json").is_file() or current == stop or current.parent == current:
            return current
        current = current.parent


def capable(
    files: Iterable[Path],
    stop: Path,
    configs: Iterable[Path] = (),
    reasons: dict[Path, str] | None = None,
    primaries: set[Path] | None = None,
) -> frozenset[Path]:
    """Every script of ``files`` that is resolver-capable (module docstring).
    ``configs`` are the non-executable resolver configs, whose strings may
    name scripts (rule 4). ``reasons``, when given, gets why each one is."""
    from flag_cleanup import cross_file_ts as ts
    from flag_cleanup import ts_workspace

    files = list(files)
    scripts = [f for f in files if f.name.endswith(_SCRIPTS) and not f.name.endswith((".d.ts", ".d.mts", ".d.cts"))]
    texts: dict[Path, bytes] = {}

    def text(path: Path) -> bytes:
        if path not in texts:
            try:
                texts[path] = path.read_bytes()
            except OSError:
                texts[path] = b""
        return texts[path]

    capable_set: set[Path] = set()
    #: Capable by rules 1, 2, 4 or 5, not only as something one imports: such
    #: a script's import of another package's PROVEN entry is followed too.
    primary: set[Path] = set()
    work: list[Path] = []

    def add(path: Path, why: str, first: bool = True) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(stop) or not resolved.name.endswith(_SCRIPTS):
            return
        fresh = resolved not in capable_set
        promoted = first and resolved not in primary
        if fresh:
            capable_set.add(resolved)
            if reasons is not None:
                reasons[resolved] = why
        if first:
            primary.add(resolved)
        if fresh or promoted:
            work.append(resolved)

    def named_by(origin: Path, value: str, packages: bool = False) -> list[Path]:
        """The in-repo files a path string in ``origin`` may name, and with
        ``packages`` (``origin`` is a tool's config: a plugin or resolver is
        named there by package) a package's too."""
        value = value.split("?", 1)[0].split("#", 1)[0]
        if not value or "\n" in value or value.startswith(("http:", "https:", "node:", "data:")):
            return []
        root = _package_root(origin, stop)
        value = value.replace("<rootDir>", str(root)).replace("${__dirname}", str(origin.parent))
        if packages and not value.startswith((".", "/", "<")):
            found = package_files(value)
            if found:
                return found
        bases = []
        if value.startswith("/") and Path(value).is_relative_to(stop):
            bases.append(Path(value))
        elif value.startswith((".", "/")) or value.endswith(_SCRIPTS):
            # A path a tool reads relative to its config or its package; a
            # bare `a/b` without an extension is a package's (above) or prose.
            bases += [origin.parent / value, root / value]
        found: list[Path] = []
        for base in bases:
            try:
                found += _files_named(Path(str(base)))
            except (OSError, ValueError):
                continue
        return found

    reached: dict[str, object] = {}
    expanded: dict[Path, list[Path]] = {}
    resolved_scripts = sorted(str(p.resolve()) for p in scripts)

    def under(directory: Path) -> list[Path]:
        """Every script under ``directory``."""
        if directory not in expanded:
            prefix = str(directory.resolve()) + "/"
            expanded[directory] = [Path(s) for s in resolved_scripts if s.startswith(prefix)]
        return expanded[directory]

    def package_files(text: str) -> list[Path]:
        """The files a bare string naming a package of the checkout may load:
        its proven entry, or every script of it. Babel's short names count
        under each name they expand to."""
        names = [text]
        if text.startswith("@") and "/" not in text:
            names += [f"{text}/babel-plugin", f"{text}/babel-preset"]
        elif text.startswith("@"):
            scope, rest = text.split("/", 1)
            names += [f"{scope}/babel-plugin-{rest}", f"{scope}/babel-preset-{rest}"]
        else:
            names += [f"babel-plugin-{text}", f"babel-preset-{text}"]
        found: list[Path] = []
        for name in names:
            if name not in reached:
                reached[name] = ts_workspace.reach(name, str(stop))
            package = reached[name]
            if package is None:
                continue
            if package.entry is not None:
                found.append(Path(package.entry))
            elif package.files is not None:
                found += [Path(f) for f in package.files]
            else:
                for directory in package.directories:
                    found += under(directory)
        return found

    # Rules 1, 2 and the hook trigger.
    for path in scripts:
        data = text(path)
        if _CONFIG_SCRIPT.match(path.name) or _TOOL_NAME.search(path.name) or ".storybook" in path.parts:
            add(path, "named like a tool's config")
            continue
        if _TYPED_HOOKS.search(data) and any(
            engine(m.group(2).decode("utf-8", "replace")) for m in _TYPE_ONLY.finditer(data)
        ):
            add(path, "a resolver hook typed against an engine")
            continue
        hook = _HOOKS.search(data)
        if hook:
            add(path, f"holds {hook.group().decode('utf-8', 'replace')}")
            continue
        used = _seeds_engine(data)
        if used is not None:
            add(path, f"imports {used}")
    # Rule 5: Node options anywhere; rule 4 for the non-executable configs.
    for path in files:
        try:
            if path.stat().st_size > _MAX_TEXT:
                continue
        except OSError:
            continue
        if not (path.name.endswith(_SCRIPTS) or _COMMAND_FILE.search(path.name) or _ci_file(path)):
            continue  # only files that run commands: not docs or data
        data = text(path) if path.name.endswith(_SCRIPTS) else _read(path)
        for match in _NODE_FLAG.finditer(data):
            for found in named_by(path, match.group(2).decode("utf-8", "replace")):
                add(found, f"handed to Node by {path}")
        for match in _CONFIG_FLAG.finditer(data):
            for found in named_by(path, match.group(2).decode("utf-8", "replace")):
                add(found, f"named as a config by {path}")
    for config in configs:
        for match in _STRING.finditer(_read(config)):
            for found in named_by(config, match.group(2).decode("utf-8", "replace"), packages=True):
                add(found, f"named by {config}")
    # Rules 3 and 4, to a fixpoint.
    options_by_dir: dict[Path, object] = {}

    def options(directory: Path):
        if directory not in options_by_dir:
            try:
                options_by_dir[directory] = ts._config(directory, stop, strict=False)
            except ts._Unknown:
                options_by_dir[directory] = None
        return options_by_dir[directory]

    while work:
        path = work.pop()
        data = text(path)
        spans = _string_spans(path, data) if _SPECIFIER.search(data) or _STRING.search(data) else None
        first = path in primary
        for match in _SPECIFIER.finditer(data):
            if spans is not None and match.span(1)[0] not in {start for start, _ in spans[0]}:
                continue  # `from `@a/b`` in a comment: no specifier at all
            spec = match.group(2).decode("utf-8", "replace")
            # Rule 3 follows what a script imports by a path: relative, a
            # `paths` alias, `imports`. Another package's entry only from a
            # script capable on its own, and only an entry `reach` proves
            # (or the files a subpath names): never every file of a package.
            if spec.startswith((".", "/")):
                for found in _files_named(Path(str(path.parent / spec))):
                    add(found, f"imported by {path}", first=False)
                continue
            config = options(path.parent)
            targets = config.targets(spec) if config is not None else None
            for target in targets or ():
                for found in _files_named(Path(target)):
                    add(found, f"imported by {path}", first=False)
            if spec.startswith("#"):
                for found in _subpath_imports(path, spec, stop) or under(_package_root(path, stop)):
                    add(found, f"imported by {path} through `imports`", first=False)
                continue
            if not first:
                continue
            if spec not in reached:
                reached[spec] = ts_workspace.reach(spec, str(stop))
            package = reached[spec]
            if package is not None and package.entry is not None:
                add(Path(package.entry), f"in a package {path} imports by {spec}", first=False)
            elif package is not None and package.files is not None:
                for found in package.files:
                    add(Path(found), f"in a package {path} imports by {spec}", first=False)
        for directory in _computed_loads(path, data, stop):
            for found in under(directory):
                add(found, f"maybe loaded by a computed specifier in {path}", first=False)
        is_config = bool(_CONFIG_SCRIPT.match(path.name) or _TOOL_NAME.search(path.name) or ".storybook" in path.parts)
        for match in _STRING.finditer(data):
            named = named_by(path, match.group(2).decode("utf-8", "replace"), packages=is_config)
            if not named:
                continue
            if spans is not None:
                strings, inputs = spans
                if match.span() not in strings:
                    continue  # quotes in a comment or around prose: no string at all
                if match.span() in inputs:
                    continue  # a file the tool compiles or runs as a program, not resolver code
            for found in named:
                # Named by a script capable on its own (rule 4); one capable
                # only through an import or a computed load passes its
                # capability on, but not the right to follow other packages.
                add(found, f"named by {path}", first=first)
    if primaries is not None:
        primaries |= primary
    return frozenset(capable_set)


#: Option keys whose files a tool compiles, bundles, tests or writes, never
#: runs as part of resolving a specifier: a bundler's entry, a test glob, an
#: alias target.
_INPUT_KEYS = frozenset(
    {
        "entry", "entries", "entryPoints", "input", "include", "exclude", "files", "setupFiles",
        "setupFilesAfterEnv", "testMatch", "roots", "outDir", "outdir", "outfile", "outFile", "rootDir", "srcDir",
        "root", "cwd", "publicDir", "cacheDir", "tsconfig", "tsConfig", "project", "dts", "source", "main", "types",
        "lib", "src", "dir", "path", "paths", "watch", "ignore", "ignored", "public",
        # An alias's TARGET is a module compiled in place of another, not
        # resolver code; an alias KEY naming the package refuses on its own.
        "alias", "replacement", "extraNodeModules", "resolveAlias",
        # Packages a bundler compiles or leaves external: inputs, not plugins.
        "transpilePackages", "serverExternalPackages", "external", "externals", "noExternal", "optimizeDeps",
    }
)


def _string_spans(path: Path, data: bytes) -> tuple[set[tuple[int, int]], set[tuple[int, int]]] | None:
    """The byte spans (quotes included) of every plain string of a script,
    and of those that stand under an :data:`_INPUT_KEYS` option (directly or
    in an array or object under it); None when the script does not parse
    (then every quoted text counts)."""
    from flag_cleanup import cross_file_ts as ts
    from flag_cleanup import ts_syntax

    try:
        root = ts_syntax._parse(data.decode("utf-8"), ts._language(str(path)))
    except UnicodeDecodeError:
        return None
    if root.has_error:
        return None
    spans: set[tuple[int, int]] = set()
    strings: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("string", "template_string"):
            strings.add((node.start_byte, node.end_byte))
            if _is_specifier(node):
                spans.add((node.start_byte, node.end_byte))  # rule 3's, not a file named by a string
        if node.type == "pair":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            if key is not None and key.type == "string":
                # A key names the specifier an option is about (an alias's
                # `'@a/b'`), never a file a resolver runs; a mention of the
                # package there is the mention check's.
                spans.add((key.start_byte, key.end_byte))
            name = key.text.decode("utf-8", "replace").strip("'\"`") if key is not None else ""
            if name in _INPUT_KEYS and value is not None:
                inner = [value]
                while inner:
                    item = inner.pop()
                    if item.type in ("string", "template_string"):
                        spans.add((item.start_byte, item.end_byte))
                        strings.add((item.start_byte, item.end_byte))
                    inner.extend(item.children)
                continue
        stack.extend(node.children)
    return strings, spans


def _subpath_imports(origin: Path, spec: str, stop: Path) -> list[Path] | None:
    """The files a ``#`` specifier maps to through the nearest
    ``package.json`` ``imports``; None when that cannot be listed (no
    mapping, a pattern, a condition this reading cannot list)."""
    import json

    root = _package_root(origin, stop)
    try:
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    imports = manifest.get("imports") if isinstance(manifest, dict) else None
    if not isinstance(imports, dict) or spec not in imports:
        return None
    leaves: list[str] = []
    stack = [imports[spec]]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            leaves.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    found: list[Path] = []
    for leaf in leaves:
        if not leaf.startswith("./"):
            return None  # a package: not listed here
        found += _files_named(root / leaf)
    return found or None


def _computed_loads(path: Path, data: bytes, stop: Path) -> list[Path]:
    """The directories a computed ``require()`` or ``import()`` in a capable
    script may load from: the literal directory before the first computed
    part (``./plugins/`` of ``"./plugins/" + f``), or the script's whole
    package when nothing literal comes first (``path.join(__dirname, x)``)."""
    if not re.search(rb"\b(?:require|import)\s*\(", data):
        return []
    from flag_cleanup import cross_file_ts as ts
    from flag_cleanup import ts_syntax

    try:
        root = ts_syntax._parse(data.decode("utf-8"), ts._language(str(path)))
    except UnicodeDecodeError:
        return [_package_root(path, stop)]
    found: list[Path] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or not (function.type == "import" or function.text == b"require"):
            continue
        args = node.child_by_field_name("arguments")
        first = next((c for c in args.named_children if c.type != "comment"), None) if args is not None else None
        if first is None or ts._string_value(first) is not None:
            continue  # a plain specifier: rule 3 reads it
        pattern = ts._computed_pattern(first)
        literal = pattern.split("*", 1)[0] if pattern else ""
        if not literal.startswith("."):
            found.append(_package_root(path, stop))
            continue
        directory = Path(str(path.parent / literal)) if literal.endswith("/") else (path.parent / literal).parent
        found.append(directory)
    return found


def _ci_file(path: Path) -> bool:
    """A CI workflow or pipeline: YAML under a CI directory."""
    return path.suffix in (".yml", ".yaml") and any(
        part in (".github", ".circleci", ".buildkite", ".gitlab", ".azure-pipelines") for part in path.parts
    )


def _is_specifier(string) -> bool:
    """Whether ``string`` is an import or export source, or the argument of
    ``require``/``import()``: what rule 3 follows, not rule 4."""
    parent = string.parent
    if parent is None:
        return False
    if parent.type in ("import_statement", "export_statement"):
        return parent.child_by_field_name("source") == string
    if parent.type == "import_require_clause":
        return True
    call = parent.parent if parent.type == "arguments" else None
    function = call.child_by_field_name("function") if call is not None and call.type == "call_expression" else None
    return function is not None and (function.type == "import" or function.text == b"require")


def _read(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    return b"" if b"\0" in data[:8192] else data
