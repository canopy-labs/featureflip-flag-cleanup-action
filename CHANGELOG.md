# Changelog

What changes for you in each release. The newest is first.

A release takes its section from this file: the section for the version being
released becomes the body of its GitHub Release, and a release whose version
has no section here is refused before anything is built. Changes that have not
been released yet are kept as separate notes and gathered into the version's
section when it is released.

## 2.8.1

### Fixed

- The GitLab CI/CD template (`gitlab/templates/flag-cleanup.yml`) and the Bitbucket Pipelines example (`bitbucket/bitbucket-pipelines.yml`) are now published in this repository. In 2.8.0 they were missing, so the README's `include: remote:` URL returned 404 and a GitLab pipeline using it could not start.

## 2.8.0

### Added

- Bitbucket Cloud support. On a repository hosted on Bitbucket Cloud, the image runs as a Bitbucket Pipelines step and opens one pull request per stale flag, with the same guarantees as on GitHub: a pull request you declined is never opened again, and `archive-sweep` (with or without `deployed-refs`) archives a flag only once its removal merged into the base branch from the repository itself. A stale flag's removal opens as a draft; where Bitbucket does not hold it as one, the description carries a "Not ready to merge" notice instead. Bitbucket pull requests have no labels, so `pr-labels` is logged and ignored there. An example pipeline ships as `bitbucket/bitbucket-pipelines.yml`, and the run needs a `BITBUCKET_ACCESS_TOKEN` repository access token with the `pullrequest:write` and `repository:write` scopes.
- A C# `//` or `///` comment that names the removed gate (by its key, or by a type the run deleted) now loses the clause that only the gate made true, instead of being left for you to reword: `, once <gate> is on`, `only once that gate is on`, the purpose clause `so that turning the flag on takes effect without a redeploy, and so <rest>` (leaving `so that <rest>`), `once <gate> is on and the <noun> has run` (leaving `once the <noun> has run`) and `, gated on <gate>, so flipping that flag takes effect without a redeploy`. Only for a flag that served `true`, only these shapes, and only in comment text: XML documentation stays well-formed, the paragraph is filled again no wider than it was, and a comment that names another gate, holds a tool directive or uses another state word is left and listed as before.
- The CircleCI orb takes a `forge` parameter (`github`, `gitlab` or `bitbucket`), with `forge-token` naming the variable that holds the GitLab or Bitbucket token, and `forge-server-url`/`forge-api-url` for a self-managed GitLab. On GitLab or Bitbucket the repository's path, including a GitLab subgroup, is read from the project's clone URL. Left at `github`, the orb runs exactly as before.
- Any CI provider can run the image by naming the forge with `FLAG_CLEANUP_FORGE`, `FLAG_CLEANUP_REPOSITORY` and `FLAG_CLEANUP_FORGE_TOKEN`, plus `FLAG_CLEANUP_API_URL` and `FLAG_CLEANUP_SERVER_URL` for GitHub Enterprise Server or a self-managed GitLab. The README's "On any other CI" section has a `docker run` example.
- GitLab support. On a project hosted on gitlab.com or a self-managed GitLab instance, the image opens one merge request per stale flag, with the same guarantees as on GitHub: a stale flag's removal opens as a draft, a merge request you closed is never opened again, and `archive-sweep` (with or without `deployed-refs`) archives a flag only once its removal merged into the base branch from the project itself. It ships as a CI/CD template, `gitlab/templates/flag-cleanup.yml`, and needs a `GITLAB_TOKEN` project access token with the `api` scope (`read_api` is enough for a pipeline that only runs `archive-sweep`). `archive-on-merge`, `archive-on-deploy` and `pr-command` stay GitHub-only and exit `2` elsewhere with a message saying so.
- `FLAG_CLEANUP_FORGE_TOKEN`, when set, is the forge token in every mode, in place of `GITHUB_TOKEN` or `GITLAB_TOKEN`.

### Changed

- A comment that joins the removed flag to something else with `+`, `&`, `&&`, `and` or `AND` now loses the flag half when the flag served `true`: `// release flag + Pro plan` becomes `// Pro plan`, and a doc line "the `k` release flag AND a Pro plan." becomes "a Pro plan.". This applies to a comment that names the key, and to the comment at a call of a function the removal took the flag's only read out of. A comment with an `or`, a third part, a negation, a condition or another key is still left and listed.
- A `key=value` item naming the removed flag in a comma-separated list inside a comment (`FORCE_FLAGS=k=true,a=1`) is removed with one comma, unless that would empty the list.
- C#: a test whose middle the run cut out, left named for what happened at the removed value (`Job_DoesNothing_UntilTheGateIsOn`), is now folded into its sibling when what is left arranges and acts, token for token, as exactly one other bare `[Fact]` of its class does and both then only assert. Its assertions the sibling does not already make are added at the end of the sibling, and the test is deleted. The pull request lists each fold instead of asking you to rename the test or compare it with the sibling.
- C#: a file that names the flag's key only as plain string arguments of calls that read no flag (a Quartz job identity, a scope or log name, an `Assert.Contains`) is no longer listed under "still reference this flag". The pull request lists each such line, with its text, in a note of its own.
- The C# "comments next to changed code describe a flag's state" report no longer fires on ordinary words that a deleted comment happened to contain, or on a bare "the flag" role mention.
- The report of test titles that still describe the removed branch is stricter. A kept title is listed only when it speaks of a flag's state or readiness ("when the flag is off", "waits for flags to load") and shares a word with a test deleted from the same `describe` group. In a repository about feature flags, titles that merely mention "flags", "toggle" or another flag's name are no longer listed.
- A kept test whose own body pinned the removed flag to the kept value now has a trailing "even with the gate on" (or "when the flag is on") dropped from its title, and is reported as a renamed test. A test that pinned no flag, or another flag as well, keeps its title.

### Fixed

- A Featureflip test provider that the removal left empty is now unwrapped when the test file imports it through an `export { name } from './module'` barrel, as a repository's `index` file usually exports it.
- C#: a reflection lookup that loops over a `private` field holding only constant names (`private static readonly string[] Names = ["_a", "_b"]; foreach (var n in Names) type.GetField(n, …)`) no longer makes every string in the repository that spells a removed gate's member count as a possible mention. Such a lookup in a test helper used to leave the whole gate family partial. The same goes for a local `string[]` written as a collection expression.
- C#: a lookup name looped over an array of another element type (`new Name[] { "A" }`), or through a loop variable of another type (`foreach (Name n in …)`), now counts as computed: a conversion of your own may turn each string into any name.
- C#: a lookup name looped over a local array whose `Count` is read, or through `var` in a repository that declares a type named `var`, now counts as computed; so does a name written with a Unicode escape or a soft hyphen that decodes to the array's name.
- C#: a private field's constant names now hold only while no reflection lookup anywhere in the repository, in any project, could reach that field by a computed name, a pattern, an ignore-case match, an alias or an escaped type name.
- C#: a by-name lookup whose binding flags are held outside the call (a constant of another class, a local) now has the strings of its file matched in any case, as `IgnoreCase` written in the file already did.
- C#: binding flags count as plain only when they name real members of the framework's enum that no type, alias or extension block of the repository could shadow; a `/*` on a preprocessor line, or a comment that swallows a preprocessor line, now makes its file count as holding a lookup of any name.
- **TypeScript and JavaScript (JSX):** a gate component that the removal left
  only waiting for the flags and then rendering its `children` is now deleted
  in more of the cases where nothing can still reach it. A comment elsewhere
  that names it no longer keeps it. A comparison opening a sentence
  (`Like BillingGate, it waits…`) is cut, the next word capitalised
  (`It waits…`), and listed as edited, when that word is plain lowercase and
  no same-named component stays in the directory the run sweeps; any other mention is
  left as written and listed for you to reword. A triple-slash directive
  (`/// <reference path="…" />`) naming the component or its file still keeps
  it, since the compiler reads it. A same-named
  component of another package, with its own users and tests, no longer keeps
  it either, and nor does a mention in prose or in source of another language
  (`.md`, `.py`, …). A mention in markup that can load a component (`.mdx`,
  `.html`) still keeps it.
- Before deleting such a component's file, every module the file imported,
  named or bare, must still load wherever the component was used: it is
  React or React Router, declared in `side-effect-free-modules`, proven to do
  nothing when it loads, or imported anyway by every file that rendered the
  component. Otherwise the file stays, and the pull request names the module.
  This replaces the rule that kept the file only for a bare import.
- A test file other than the component's own that renders it now keeps the
  wait, named in the pull request, rather than having its elements unwrapped.
  The component's own test file is deleted with it only when it tests nothing
  else: every module it imports is the component, a test library, React or
  React Router, a Featureflip SDK, or a module declared in
  `side-effect-free-modules` whose only names it uses are the SDK's
  `TestFeatureflipProvider` or your wrapper of it, as an element. A test that
  loads another module some other way (`import()`, `vi.importActual`,
  `jest.requireActual`, `importOriginal` given a module) keeps the component.
- **Every language:** the pull request no longer lists what other steps did
  inside a file it deletes outright (a flag-keyed entry removed, a test
  deleted or renamed, an argument left out): the note announcing the
  deletion covers that file. Rows about files that stay are unchanged.
- A discarded hook call kept because a test mocks the hook, and then deleted
  because `side-effect-free-calls` names it, is now reported once, as
  deleted, and its package's import goes with it as when the hook's own audit
  deletes it. A note that code was kept is dropped when a later step of the
  same run removed that statement.

## 2.7.0

### Added

- **C#:** a kept test that is renamed because its name held the removed gate's
  state now takes its references in the same file along: a `nameof(Test)`,
  `nameof(Type.Test)` or a `///` `cref="Test"` pointing at it is renamed too,
  where one used to keep the test as it was. Any other spelling of the name
  (another file, a string, a comment that is not a `cref`, a document, a
  `nameof` qualified by a different type) still keeps the rename from
  happening.
- **C#:** a kept test whose name speaks of several gates (`EitherGateOff_…`,
  `BothGatesOn_…`) after the run removed one of them is listed in the pull
  request, since the name no longer says which gate is left.
- **C#:** a test the run cut down, whose statements begin exactly
  as a sibling test in the same class does, up to its first assertions, is listed as one that may
  now duplicate that sibling. Nothing is deleted.

### Changed

- A kept TypeScript or JavaScript test titled `… even with <flag> on` (or `off`, when that is the kept state) now loses that clause, including in an `it.each([…])('…', …)` title. Other `even`, `regardless` and `whether` titles are still left as they were.

### Fixed

- **TypeScript and JavaScript:** a call or parameter list holding one item on
  its own line with a trailing comma, such as `label(\n  true,\n)`, could be
  left as `label(\n  ,\n)` when that item was removed. That does not parse, and
  nothing refused it. The list now collapses to `label()`, and any edit that
  would leave a comma with nothing before it is refused. Check any such
  removal already merged.
- **Kotlin:** two kept tests whose names reduced to the same name were both
  renamed to it, which does not compile (conflicting overloads). A rename is now
  refused when its new name is already declared in the file, backticked or not,
  or was produced earlier in the same run. Check any such removal already
  merged.
- **TypeScript and JavaScript:** a kept test could be renamed even though a
  stored snapshot was keyed by its old title, so the snapshot no longer matched
  and the test run failed. A file that uses a stored snapshot or screenshot
  matcher, or whose snapshots are stored in the repository, is no longer
  renamed, and a rename that could give two tests in a file the same name is
  refused. Check any such removal already merged.
- **C#:** when a kept test could not be renamed because its name is spelled
  elsewhere, the pull request could name the test's own declaration as that other
  spelling. It now names the spellings that actually keep the rename, up to
  three and a count of the rest.
- C#: when a removal deletes a type, such as a gate interface or its test fake, a file that used it no longer keeps a `using` of that type's namespace with nothing left to import. The directive is deleted only when the namespace is your own (under a project's root namespace), the file spells none of the namespace's other types, and nothing in the namespace can add a name the file uses without spelling it (an extension method or block, a `partial` or attributed type a source generator may extend, a type inside `#if`, a Razor component). The pull request lists each one with the other unused bindings, and IDE0005 no longer flags them.
- C#: a type declared inside `#if` is no longer invisible to the checks that look across files. A file holding one is treated like a file that does not parse, so every question about a name it spells fails safe. In particular, a removal no longer deletes the `using` directives of a namespace it would empty while such a type is declared in it; the leftovers are kept and listed instead. Check any such removal already merged.
- **TypeScript:** a test helper whose parameter every call now passes `{}` to loses the parameter. After a removal turns `renderWithFlags({ 'old-checkout': true })` into `renderWithFlags({})`, the helper becomes `renderWithFlags()`, each `flags={flags}` it handed the parameter to becomes `flags={{}}`, and that is left out in turn where the provider's declaration defaults it. A parameter typed `Record<string, never>` goes the same way when it sits in a test file the removal changed. The helper can be declared inside a `describe` block. A parameter read more than once, or inside a function the helper builds such as a `wrapper` handed to `render` (including a curried one), goes only when every receiver is the React SDK's `TestFeatureflipProvider` or a component that defaults the prop to `{}` and only passes it on to one, so no component sees a new object where the helper held one. A file that mocks the SDK package (including with a computed name), a setup or shared mocks file that does, a `__mocks__` replacement for it, a config that aliases the package (including by wildcard, or through Babel), or a vendored or patched copy of it, keeps the helper. The reads go to an element with no type arguments and no generic declaration in the file, and an edit that would leave a stray comma such as `f(\n  ,\n)` is refused. The pull request no longer lists these calls as override literals left empty.

## 2.6.0

### Added

- **Comments:** a comment that names the flag (or a type the run deleted) and
  also says something else is now cut down to what is still true instead of
  being kept for you to reword: ``// Gated on the `k` flag. Retries twice.``
  becomes `// Retries twice.`. Only a sentence that names the flag and says
  nothing else is deleted (the same word test as for a whole comment, applied to
  that sentence), so a fact, a condition, a warning or another name in the
  sentence keeps it, and so does `no`, `not`, `never`, `still`, `true`, `false` or `check`, or two of `enabled`, `disabled`, `on`, `off`. Nothing is cut inside a numbered list or from an item with indented children. The comment is also left alone when the cut would leave a
  `<summary>`, `<param>` or JSDoc tag empty, when the next sentence leans on the
  cut one (`This …`, `It …`, or a common dependent phrasing such as `Only for admins.`, `Off by default.`, `Rolls out to 10% of users.` or `Remove after launch.`; this is a list of common phrasings, not a proof that a sentence stands alone, and a dependent sentence worded some other way, like `Users see the new UI.`, can still stay), when a lead-in or a comma is left hanging, when
  brackets or quotes would stop pairing, when a full stop cannot be told from a
  sentence end, and when any line is a tool directive (`//go:embed`,
  `eslint-disable`, `noqa`, a licence header, …). Line prefixes, indentation,
  line endings and a byte-order mark are kept, and the pull request lists each
  reworded comment, before and after. This covers every language the comment
  removal covers.
- **TypeScript and JavaScript:** when a function called by its bare name read the
  flag in its body and no longer does, a comment at a call of it that contains
  "flag" or "gate" (a trailing `// release flag + Pro plan`, or the comment
  directly above the statement) is listed in the pull request. It is only
  reported, never edited: the call did not change and the comment names no key.
  A call counts in the function's own file or a file that imports it from that
  module (also under an `as` alias), within the directory being processed, and
  not where a parameter or local of the same name stands in for it; methods are
  skipped.

### Fixed

- Archiving a flag now sends a change reason, so `archive-on-merge`, `archive-sweep` and `archive-on-deploy` work in a Featureflip project with a critical environment that requires one. The reason names the merged removal pull request and appears in the project's audit log. If Featureflip still refuses an archive for a missing reason (`422 change_reason_required`), the run reports that directly instead of a generic error.
- **TypeScript:** when a removal takes an option off an exported function and writes its default where the function read it, a literal that now equals the next callee's own default is left out too. `useThing(id, { enabled })` forwarding to a callee declared `{ enabled = true }` becomes `useThing(id)` instead of `useThing(id, { enabled: true })`. Every existing check on a left-out default still applies, including the refusal when a test mocks the callee's module. The pull request now describes such an option as removed because every call in the checkout was read, not as a parameter of a function that nothing outside its file can call.

## 2.5.0

### Added

- **TypeScript and JavaScript:** a discarded call to a read-only framework
  hook that a removal leaves behind (`useParams<{ orgId: string }>();` once
  nothing reads the route parameter) is deleted without being listed in
  `side-effect-free-calls`: React Router's `useParams`, `useLocation` and
  `useMatch`, React's `useContext`, and the Featureflip React SDK's
  `useFeatureflipClient` and `useFeatureflipStatus`.
  Only when the call is that package's own export and runs unconditionally at
  the top of its component; one inside a condition, a loop or after an early
  return is kept, and so is one whose hook a test mocks or spies on (it is
  listed). When it was the file's last use of the import, the import
  goes with it. The pull request lists each call deleted this way.
- **TypeScript and JavaScript tests:** a kept test that still describes the
  removed branch is now listed in the pull request, under "test lines may
  still describe the removed branch": a negative assertion
  (`expect(screen.queryByText('X')).not.toBeInTheDocument()`, `.toBeNull()`)
  on text only the removed branch showed, and a title that speaks of the flag
  without naming it and shares a word with a test the removal deleted from
  the same file. Nothing is edited.
- **TypeScript and JavaScript tests:** a Featureflip test provider whose flag
  overrides the removal emptied (`<TestFeatureflipProvider flags={{}}>`) is
  replaced by what it wrapped, and its import removed, when nothing the test
  renders can read a flag: no module the test file loads, followed through
  every module those load, loads a `@featureflip/` package. The React SDK's
  own provider and a wrapper of it in your repository both count. A provider
  around a tree that reaches a flag read, however deep, is kept, and so is
  one around a module that cannot be resolved or parsed. The pull request
  lists each provider unwrapped.
- **TypeScript and JavaScript (JSX):** a wait for the flags to load that the
  removal left guarding nothing now goes. When a component waited for the
  flag client to be ready (`if (!resolved) return <Spinner />;`) only so that
  its own flag read would not act on the value read before then, and the
  removal took out the last flag read in it, the early return and the hook
  call it tests are deleted. The wait is recognised from the React SDK's
  `useFeatureflipStatus()` (destructured `isReady`) or from a hook of your
  own that reaches it through named imports and re-exports and does nothing
  but read the client's status, call its `initialize`, and keep React state.
  The hook must also return a wait (`isReady`, combined only with its own
  state and `|| isError`): one returning `!isError` is an error fallback and
  is left alone. Nothing else left in the component may be able to read a
  flag: every name left in it, called, rendered or handed on as a value
  (`createElement(Page)`, `<Route Component={Page} />`), must be bound inside
  it, an HTML element, or a React or React Router hook or one of a few of
  their components (`Navigate`, `Outlet`, `Link`, …). The wait is kept, and
  named in the pull request, when a test file drives it (it names the hook,
  as a mock does, and imports the component through your modules) and, when
  the component itself stays (below), while a test file renders the
  component.
- When that leaves a component named for a gate (`…Gate`, `…Guard`) that
  only renders its `children`, every `<Gate>…</Gate>` in the repository is
  replaced by its one child element, the import of it goes, and the
  component's file and its own test file (named for it, holding nothing but
  setup and groups named for the component) are deleted. This needs every use
  of the component proven, as for a prop removed from an exported component:
  an element with attributes or with anything but one child element, a
  re-export, a namespace import, a mention of the component or its file
  anywhere but the import (a `vi.mock` of its path), or a bare import in its
  file (`import './theme.css';`) keeps the component and its files.
- The pull request lists each wait and component removed, and each wait kept
  with the reason.

### Changed

- **C#:** a static helper that the removal leaves doing nothing but calling
  one method on one of its parameters
  (`ForStorage(string secret, IProtector protector) => protector.Protect(secret)`)
  is inlined: every call becomes the call it forwarded
  (`_protector.Protect(secret)`) and the helper goes with its doc comment.
  This happens only for a helper the run itself reduced, when every call in
  the repository is found and passes plain values, nothing can reach the
  helper by reflection, a `cref` or a method group, and the called method
  is the only one of its name on a class or interface the repository
  declares, taking and returning the same types. A helper that cannot be
  inlined is kept and listed in the pull request with the reason, since its
  doc comment may still describe the removed flag.
- **C#:** when a gate interface's removal leaves an xUnit `[Theory]` with a
  single `[InlineData]` row, the theory becomes a `[Fact]`: the row and the
  parameters go, and each parameter's value is written where it was read,
  with whatever that value makes constant folded like a flag read
  (`if (false) { … }` goes). This is done for `bool`, `int` and plain
  `string` parameters, and only when the method carries nothing but
  `[Theory]`, the row and `[Trait]`. A `string` value is written only
  where it cannot become part of a constant expression: as an argument, an
  assigned or initial value, a `return` value or an interpolated value. An
  `int` value is written only as an assigned or initial value, a method's
  `return` value or an interpolated value, where its type is fixed: as an
  argument, an initializer's element or a lambda's result, the constant
  could choose another overload than the variable did (`F(2)` calls
  `F(byte)` where `F(n)` called `F(long)`).
  A theory with `[MemberData]`, `[ClassData]` or any other attribute that can
  add rows, whose parameter is assigned, passed by `ref`/`out`, read under
  `nameof`, declared again inside it or used anywhere else, or with a file
  named for it (a snapshot such as `Tests.Method_on=True.verified.txt`),
  keeps its one row. The pull request lists each theory rewritten.
- **C#:** a test kept by a gate interface's removal whose name holds the
  gate's kept state as a part of its own (`EnsureCanSet_GateOn_Passes`,
  `Process_WithTheExpirationGateOn`) is renamed without that part
  (`EnsureCanSet_Passes`, `Process`), when nothing else in the repository
  spells the method's name (comments and documents included), no file is
  named for it (a snapshot such as `Tests.Method.verified.txt`), neither the
  test's class, another part of it nor a class it derives from already has a
  member with the new name, and the test itself ties the name to the removed
  gate: the name names the gate (`ExpirationGateOn`), the test's body uses
  the removed gate, or the removal deleted a test beside it whose name holds
  the opposite state and shares another part with it (`GateOn_PastExpiry_…`
  and `GateOff_PastExpiry_…`). A name that also names another gate, or a
  test whose body uses another gate, is left alone.
  A name about the removed gate that cannot be renamed, or that names the
  state the removal deletes, is listed in the pull request for a person to
  rename.
- **C#:** when a test is cut down to the part that runs at the kept value, and
  its name says what happened at the removed one
  (`Job_DoesNothing_UntilTheEncryptionGateIsOn`, `GateSwitchedOff_…`), the
  pull request's note about the cut now says the name is false, so a reviewer
  can rename the test or delete it.
- **Every language:** the doc comment of the function, method, class or
  component that holds a folded read (a `/** … */` directly above it, or above
  the `export`/`const` that names it) is now reported when it describes a
  flag's state or says "the flag", even when the fold sits far down the body
  and the comment never names the key. It is listed by the branch it
  describes, from the sentence that describes one. It is never edited.
- **TypeScript and JavaScript:** in a repository with several packages, a computed `import(name)` or `require(name)` in an unrelated package no longer blocks removing an unused prop or option from an exported component or function. Such a load still blocks it when the file holding it could load the component's package: a file in the same package (the same nearest `package.json`), in a package that lists the component's package by name in its `dependencies`, `devDependencies`, `peerDependencies` or `optionalDependencies`, or in no package at all. It also still blocks it when a `package.json` on the way cannot be read, or when the component's own file has no `package.json` above it. A bundler glob (`import.meta.glob`, `require.context`) blocks it wherever it is, since its pattern can name another package's files by a relative path.
- **TypeScript and JavaScript:** when a removal reduces a local to another local's name (`const visible = enabled && canManage;` becoming `const visible = canManage;`), the alias is removed and its reads use the original name, `{ visible }` becoming `{ visible: canManage }`. Both must be `const` declarations of the same block, and neither name may be declared or assigned again there. An alias the removal did not produce is left alone.

### Fixed

- **C#:** a flag `if` whose kept arm holds a `return`, `throw`, `break`,
  `continue` or `goto` inside a `#if` branch is no longer rewritten when code
  follows it. The fold keeps the arm's braces because of the directive, so in
  a build that defines the symbol the code after the `if` became unreachable
  (CS0162, an error under `TreatWarningsAsErrors`). The flag is refused,
  rather than left as a pull request that breaks one build, when something
  follows the arm. A jump that is conditional, that leaves a loop inside the
  arm, or that has nothing after it is still folded.
- A comment directly above the flag's own read, a variable holding it, or its registry entry is now deleted with that line instead of being left behind with nothing to explain. Registry entries are covered in every language; reads and variables in every language except Python and Ruby, where such a comment is still left in place. Before, a comment above a deleted read that did not describe the flag's state was only listed, and a comment above a deleted registry entry was not noticed at all: it stayed in place, and in the middle of a registry it then sat above the next flag's entry as if it described that one. Each removed comment appears in full in the pull request. A comment above a registry entry that the next entry may share (the next entry follows with no comment of its own) is kept and listed rather than deleted, as is a comment above any other statement the run deletes, including one that uses the read inside something else, such as an assertion or an argument.
- **TypeScript:** a parametrised test title, such as `it.each([...])('hides the field when the flag is on', cb)` (also `test.each`, `describe.each`, the `.only.each` and `.skip.each` chains and the tagged-template form), is now treated as a title instead of live data. A title naming the flag no longer keeps its registry entry and every other flag-keyed entry in place, which could leave a test failing because its override no longer did anything. These tests are not deleted or retitled, since one declaration stands for several runs.
- **TypeScript:** an override deleted because its value is a name that is provably a boolean is no longer also listed under "deliberately left in place" when a registry entry was being kept. It was listed in both places, and the diff showed it removed.
- **TypeScript:** a test helper that sets a flag override from a destructured option, `function renderGate({ ssoFlag = false } = {})` feeding `flags={{ 'my-flag': ssoFlag }}`, no longer keeps the option after the override is removed, which left an unused binding for ESLint to reject. The option now goes from the helper and from every call, and when it was the only option, the whole parameter and the argument go with it: `renderGate({ ssoFlag: true })` becomes `renderGate()`. Each call pins the flag to the value it passes, or to the option's default, so a test passing the removed value is chosen by its title like any other pinned test. This needs a `true`/`false` default, a helper declared once in the file and not exported, and calls that pass an object literal with a `true`/`false` value or no value for the option. A spread, a computed key or a non-literal value leaves the helper and its calls as written.
- **TypeScript and JavaScript:** a stranded import of a local module that only loads an audited `@featureflip/*` SDK is now deleted in a project whose root `tsconfig.json` is a solution config (`"files": []` plus `references`, the Vite template's default). Before, such a config made the proof give up, so the import stayed behind as a bare `import './useFlag';`. The configs a solution config names are read the way module resolution already reads them. The import is still kept when the referenced configs disagree, when one cannot be read or sits outside the checkout, or when a referenced config has a `paths` entry or `baseUrl` file that could answer the SDK's name.
- **TypeScript:** when the removal leaves out the last argument that set an option, `useRole(id, { enabled: true })` becoming `useRole(id)`, the option no longer stays on the function with nothing left to set it. It now goes from the declaration, the whole parameter when it was the only option, and each read inside the function gets the default instead: `useMembers(id, { enabled })` becomes `useMembers(id, { enabled: true })`. This works for an exported function, including a hook called from other files, when every other mention of its name in the repository is a call or a plain named import of it. A re-export, a namespace import, an alias, a call that spreads its options or still sets the option, or a read in a condition leaves the option as written.

## 2.4.0

### Added

- **Kotlin:** OpenFeature reads are removed. `client.getBooleanValue("KEY", default)`, with or without an `options` argument (and the OpenFeature Java client's forms when called from Kotlin), folds the way `boolVariation("KEY", default)` does, with the key as a literal or a `const val` in the same file, in a file that imports something from `dev.openfeature`. A test's MockK or Mockito stub of the read is resolved the same way as for `boolVariation`, under the same per-file import rule: in a test file that imports nothing from `dev.openfeature`, a stub is left as it is, and one that names the key directly refuses the flag like any other read. `getBooleanDetails`, the non-boolean accessors and a safe call (`client?.getBooleanValue(…)`) are left alone. A read of the flag through `getBooleanValue` in a file that imports nothing from `dev.openfeature` refuses the flag and names the file, since nothing there proves the receiver is an OpenFeature client. A `getBooleanValue` read of a key constant declared in another file, and a function that only returns the read, are followed as they are for `boolVariation`: the read folds and the constant goes once nothing else mentions it, and the function is inlined at its callers and deleted. A `getBooleanValue` read on a line of its own, whose value is discarded, is deleted as a `boolVariation` one is. The same per-file import rule applies to the file holding the call, so a reader of another file's constant, or a function wrapping the read, in a file that imports nothing from `dev.openfeature` refuses the flag and names that file.

### Fixed

- **Kotlin:** a read whose value is discarded, such as a warm-up call `client.boolVariation("KEY", default)` on a line of its own, is now deleted instead of being left behind as a bare `true` or `false` statement. That happens where the statement's value cannot be used: before another statement, anywhere in a function's `{ … }` body, an `init` block or a loop body, and in an `if` with no `else`. The last statement of a lambda, or of an `if`, `when` or `try` branch, is that construct's value, so a read there still folds to its literal.

## 2.3.0

### Added

- Laravel Blade views (`.blade.php`) are rewritten, as the new `blade` language (on by default). A flag read in an `@if`, `@elseif` or `@unless` condition folds the directive and keeps the surviving arm's markup byte for byte; a rung served true in the middle of a chain becomes its `@else`. A read in a `{{ … }}` or `{!! … !!}` echo folds in place, keeping the echo's delimiters. With `accessors: active`, Laravel Pennant's `@feature('KEY')`, `@elsefeature('KEY')` and `@unlessfeature('KEY')` fold the way `Feature::active('KEY')` does. A deleted directive takes its line, so the view renders as before. Left alone: `@feature` with a value or an array, `@verbatim`, `{{-- --}}` comments, `@{{ }}` and `@@` escapes, `@php` blocks, raw `<?php ?>` and the arguments of other directives; a read there is listed under "still reference this flag". Inside `<pre>` and `<textarea>` a deleted directive's indentation stays, since it was rendered. Only Laravel's own, Pennant's and Livewire 3's directives may sit inside a conditional that is folded: a view with a custom `Blade::if` or `Blade::directive` there, or raw PHP or `@php` opening or closing a block there, is left as it is and listed with the files the engine could not process. A rewrite that would reach a custom directive, or leave a Blade block without its opener or closer, is refused.
- PHP: Laravel Pennant class-based features are removed. A class whose `#[Name('KEY')]` attribute or public `$name` property is the flag's key is read as `Feature::active(Voucher::class)`, `feature(Voucher::class)` or `Feature::for($user)->active(Voucher::class)`; those reads now fold like `Feature::active('KEY')`, the imports they leave unused are removed, and the class's file is deleted once nothing else in the repository names the class. A class something else still names (a test's `Feature::define`, a Blade view, `new`, `extends`, a string) is kept, its proven reads still fold, and the pull request lists the line that kept it. A class that holds the key but cannot be proven to be named by it is listed as `feature-unproven`.

### Changed

- **All languages:** runs are faster. Each rewrite rule is now prepared once
  per run instead of once per flag, so a run that removes several flags no
  longer repeats that work for each one. Swift gains the most, since two of
  its rules took seconds each to prepare.
- The `php` language no longer takes `.blade.php` files; they are the `blade` language's. A run configured with `languages: php` alone lists a Blade view that reads the flag under "still reference this flag", as before.
- **All languages:** before a call to a removed wrapper, or to a member the
  cleanup reduced to a constant, is folded, the Action now checks that the
  call it proved is still at the position it recorded. If it is not, the flag
  (or, for a constant member, the member) is left as written and the run
  reports why, instead of editing whatever code had moved into that position.

### Fixed

- **C#:** a flag read in or under an `if` or loop that a preprocessor
  directive splits (an `#if … #endif`, `#region` or `#pragma` between its arms
  or standing as one of them, outside an arm's braces) is now refused instead
  of rewritten. These shapes used to be rewritten in a way that changed what
  some build runs, or did not compile:
  - an `if` alone in a `#if` block after an `else` (`else` / `#if X` /
    `if (<read>) { … }` / `#endif`): only its first statement stayed under
    the `else`, or the statement after the `#endif` became the `else`'s body;
  - an `if` inside a `#if` block followed by an `else` after the `#endif`,
    which belongs to that `if` in builds defining the symbol;
  - an `else if (<read>)` inside a `#if` block followed by an `else` after
    the `#endif`, which was left with no `if` in builds defining the symbol;
  - an `if` right after an `if` or loop whose arm is a `#if` block with no
    `#else`: in builds without the symbol that `if` is the arm, and only its
    first statement stayed under it;
  - an `if` whose body is a `#if` block with no `#else`: the statement after
    the `#endif`, which is the body in builds without the symbol, was kept
    running;
  - an `else if (<read>)` whose body is a `#if` block, written as
    `else #if`, which does not compile;
  - a `#region`, `#pragma` or `#nullable` line between an `if`'s arms, which
    was deleted with the `if`.
  - a `#region` pair in the code after a folded `return`: the `#region` line
    was deleted on its own, leaving an `#endregion` that does not compile.

  If an earlier release opened a pull request removing a flag read in one of
  these shapes, check its diff against every build configuration you ship.
- **Go:** removing the longest key constant from a `const ( … )` block, or the
  longest key from a flag-keyed map literal, no longer leaves the lines around
  it padded for a name that is gone. They are re-padded the way `gofmt` pads
  them, so `gofmt -l` stays clean and there is no `gofmt -w` step to run before
  merging. A block that was not `gofmt`-formatted to begin with is left as it
  was.
- **Python:** a decorator gate named with the position of its key
  (`accessors: flag_required[1]`) is now handled like the first-argument form.
  `@flag_required(SWITCHES, "old-checkout")` is deleted when the flag is
  removed serving `true`, and the flag is refused, naming the decorator, when
  it is removed serving `false`. Before, the decorator was left in place in
  both directions. A name, attribute or plain literal before the key is
  allowed; anything else there refuses the flag in both directions.
- **C#, Dart, Python and Swift:** a wrapper named after its own flag, such as
  `boops()` reading `boolVariation("boops", …)`, was kept by that read's key,
  and by the key of any other read of the flag. The string is the flag's key,
  not a lookup by name, so the wrapper is now removed and its callers inlined,
  as in PHP, Kotlin and Ruby. A string that names the wrapper anywhere else
  still keeps it.
- **Swift:** `if ready(), <read>` (a call ahead of the flag read in a
  condition list) served false no longer deletes the statement, which dropped
  the `ready()` call that Swift runs before it reaches the `false`. The file
  is refused instead, and the same goes for `guard` and `while`. The refusal
  now says that an earlier clause of the condition list runs code, rather than
  that the literal is "still bound to a name or still tested". If an earlier
  release removed a flag served false from such a condition list, check that
  the call it deleted is not one you need.
- **Swift:** `while let item = queue.next(), <read>` served false is refused
  instead of left as `while let item = queue.next(), false`, a loop that still
  calls `next()` once with the flag key gone from the file.
- **Swift:** a flag read in a `while` condition list served true is removed
  from the list, as it already was for `if` and `guard`:
  `while <read>, let item = queue.next()` becomes
  `while let item = queue.next()` instead of `while true, let item = …`.
- **Swift:** a labelled `if` (`outer: if <read> { … } else { … }`) whose
  `break outer` sits only in the branch the removal deletes keeps the other
  branch as plain code. It used to be wrapped in `outer: do { … }`, a label
  nothing jumps to any more.

## 2.2.0

### Added

- **Java and Kotlin:** a private field the removal leaves assigned `true` or
  `false` in the constructor (`this.allowScottish = true;` in Java's only
  constructor, `allowScottish = true` in a Kotlin `init` block) is now inlined
  into its readers and removed, as one initialized at its declaration already
  was. It happens only when nothing can read the field before the assignment
  runs: no superclass, and no method of the class called, no `this` used and
  no anonymous object or class of the file constructed earlier in
  construction.
- **Java, Kotlin and Go:** a parameter of a private helper (a Java `private`
  method, a Kotlin `private fun`, a Go unexported function) is removed when
  every call now passes it the same `true` or `false` the removal wrote
  (`buildQuery(true)`): from each call and from the helper, and what it
  guarded inside is simplified, as TypeScript and JavaScript already did. In
  Go the calls are proven across every file of the package. A helper whose
  name is spelled any other way, or reachable by name from a string or a
  configuration file, is left alone.
- **Python:** a decorator gate named in `accessors`, such as
  `@waffle_flag("k")` or `@method_decorator(flag_required("k"), name="dispatch")`,
  is deleted when the flag is removed serving `true`, and a removal serving
  `false` is refused with the decorator named. Before this release the
  decorator became `@True`, which raises `TypeError` when the module is
  imported. A decorator passing more than the key, or the same call outside a
  decorator (`flag_required("k")(view)`), is refused instead of rewritten.
- **Python:** an accessor can name the index of its key argument, so
  django-waffle's `flag_is_active(request, "k")` is matched with
  `accessors: flag_is_active[1]`. Only that slot matches; a call with the key
  anywhere else is untouched. Before this release such a read was not matched
  at all and the run made no change.
- **Python:** django-waffle test pins of the removed flag (`override_flag`,
  `override_switch` and `override_sample`, as a decorator or a `with`) are
  handled like other test doubles. A pin of the kept value is deleted and its
  test stays; a test pinned to the removed value is deleted and named in the
  pull request. A pin of the removed value on a class or in a fixture refuses
  the flag, naming where it is. Before this release every pin stayed, so a test
  pinned to the removed branch failed after the merge.
- **New input `side-effect-free-calls`:** name the functions whose call does
  nothing you need, such as a getter that only returns your flag client
  (`GetClient, getClient`). A call to one that the removal left running only as
  a discard (`_ = getClient(p)` in Go, `getClient(p);` elsewhere) is deleted
  instead of kept, when every argument is a name, a literal or a member read,
  and the pull request names each one. Anything only that call used, such as
  its import, goes too. Every language.

### Changed

- **Python:** a read stored into a dict or list entry, as a Django view does
  with `context["new_checkout"] = flag_is_active(...)`, now becomes
  `context["new_checkout"] = True` instead of refusing every file of the flag.
  A read stored into an attribute (`self.enabled = <read>`) is still refused,
  and the reason now says it is an attribute.
- **Python:** a keyless wrapper may now hold locals before its `return` that
  only feed the read's arguments, such as `context = {"schema": schema}`; it
  is inlined and deleted like a one-line wrapper. A function whose `return`
  is the read but that does anything else first (a log line, a call, a
  branch) is still left alone, and the run now logs which statement stopped
  it.
- **Python:** when a removed wrapper's import would be kept as
  `import app.flags as _flags  # noqa: F401` for its module load, it is now
  deleted instead if another top-level import of the file already loads that
  module, above it or directly below it.
- **Python:** a refusal because the flag's value was left bound to a name now
  names the variable, its line and why it was not inlined.
- **Swift:** a computed property or method listed under "now returns a
  constant" is now folded into its calls and deleted when every call of it is
  in its own module and is proven to reach it, and nothing else can (no
  protocol it may implement, no override, no key path, no `@objc`, not
  `public` in a library), as a wrapper whose whole body reads the flag already
  is. Anything short of that keeps the member and every call, and it is listed
  as before.

### Fixed

- **Dart:** a test of the flag's own value,
  `expect(AppConfig.isFeatureEnabled('k'), AppConfig.enableK)`, is now deleted
  and listed in the pull request, with a `test(…)` it leaves empty. Before, it
  became `expect(true, AppConfig.enableK)`, which fails wherever that setting is
  `false`, and the pull request did not mention it.
- **A deeply nested expression in a file that mentions the key no longer ends
  the run.** A long string concatenation (a generated `docs.go` that writes a
  whole document as one constant, for one) is a tree thousands of levels deep,
  and walking it raised `RecursionError` out of the whole run, with a message
  naming neither the file nor the flag, so every flag after it was lost too.
  The file is now analysed. If a file still cannot be, only that flag is
  refused (`unsafe-rewrite`, the build goes red), the refusal names the file
  and how deep it is, and the next flag runs.
- **Python:** a removal's pull request passes `black --check` and
  `ruff format --check` in more places. The blank lines a deleted statement
  leaves are cut to what the formatters write there: two before a top-level
  definition, one inside a block, none at the start of a block or the end of
  the file. Blank lines the removal did not touch stay. A `# type: ignore`,
  `# noqa`, `# pragma: ...`, `# pyright: ignore` or `# fmt: skip` on the line
  of a folded `if` goes with the `if` instead of landing alone above the code
  it kept. A comment directly above a statement deleted after a folded
  `return` goes with that statement. A condition or call argument the
  formatter had wrapped in parentheses because it was too long goes back on
  one line once the removal shortens it enough: `if (\n    x\n):` becomes
  `if x:`.
- **TypeScript and JavaScript:** an `// eslint-disable-line` or `// NOSONAR`
  on the line of a folded `if` goes with the `if`.
- **Dart:** a declaration's value that `dart format` had moved to the line
  after its `=` goes back beside it once the removal lets the line fit, so the
  pull request passes `dart format --set-exit-if-changed`. The width is the
  `formatter: page_width` of your `analysis_options.yaml`, or 80.
- **C#:** C# 14 syntax now parses, including `extension` blocks, and so does a
  local named `partial`, `async` or `required` at the start of a statement, such
  as `partial.Uris = null;`. Before, one such file could stop key constants
  resolving across the repository. Several ordinary shapes that were also
  rejected now parse, including `#if` blocks inside initializers and an `else`
  inside a directive.
- **C#:** two C# 14 shapes that now parse are read for what they do. A write
  through `?.`, such as `fake?.Enabled = false;`, counts as a write, so it keeps
  the wrapper a test fake serves, and it keeps a method holding a field's last
  write. A lambda parameter with a modifier, as in `(ref x, y) => …`, hides a
  field of its name, so a read of it is not taken for a read of that field.
- **C#:** a flag read testing an `if` whose `else` sits inside `#if … #endif` is
  refused. That `else` now parses, and folding the `if` would have deleted it
  with the code it holds, even where it is the branch the flag leaves running.
- **Go:** `if on := client.BoolVariation(…); on {`, a read bound in an `if`
  header, is now inlined and folded. It used to make the transform engine
  abort on the file, so no pull request was opened for the flag and the
  output named no line. The same read in a `switch` header is refused with the
  file named instead.
- **Go:** when the removal strands one name of `x, err := f()` and the other
  is still read, the stranded one becomes `_` (`_, err := f()`). The flag used
  to be refused because the local was left declared and unused.
- **Go:** an unreachable tail removed after a `return` no longer leaves a
  blank line before the closing `}`, and removed code no longer leaves one
  just after an opening `{`. `gofmt` accepts both, but `gofumpt` and
  golangci-lint's `whitespace` check do not.
- **Go:** a `range` loop over a slice, array or map parameter or `var` is
  removed when the removal leaves it starting with `continue` or `break`.
  It used to be left as `for range xs { continue }`.
- **Go:** a flag is no longer refused because the code the removal deletes
  holds an `if` with an initializer, such as `if err := run(); err != nil {`
  in the removed branch or after a `return` the flag made unconditional. That
  code never ran, so nothing is lost. It used to be refused as `the fold would
  drop code that runs whatever the flag says`, which still applies to an
  initializer on the `if` that reads the flag.
- **Go:** a removal that would leave an `if` or `for` condition such as
  `strings.HasPrefix(name, "x") && false` (or `track(u) || true`) now refuses
  the flag and names the file, as TypeScript and JavaScript already do. The
  call beside the literal cannot be dropped, so the branch the flag made dead
  stayed, with everything it references, and the pull request did not mention
  it. With an operand that cannot run code, the condition folds as before.
- **Go:** a key held in a constant now folds when it is the second argument:
  OpenFeature's `client.Boolean(ctx, oldCheckout, false, evalCtx)`, or a
  wrapper you named in `accessors` that takes something else first, such as
  `flags.IsEnabled(org, oldCheckout)`. Before, only the literal key folded in
  that position, so the run made no change and said only that the file still
  named the flag.
- **Go:** a flag key declared as one entry of a `const ( … )` block now folds,
  and only that entry's line is removed. Before, a block declaring more than
  one name was skipped, so its reads stayed and the run made no change. A block
  that uses `iota` or has an entry with no value is still left alone, since
  removing one entry could change the others, and the run log now says so.
  When the removed entry had the block's longest name, run `gofmt -w` on the
  branch: the others stay padded for it.
- **Go:** a read through a call that returns `(bool, error)`, such as
  `on, err := client.BoolVariation("k", ctx, false)` with an SDK that declares
  it that way, is no longer rewritten into code that does not compile. The run
  used to write `on, err := true`, or `return true` from a function returning
  two values, and open the pull request. Such a flag is now refused
  (`unsafe-rewrite`), and the output names the file and the functions that
  read it this way, so those reads can be removed by hand.
- **Go and Python:** a key constant kept because another file names it in
  something that is not a read, such as a test seeding a store or pinning a
  setting with it, no longer keeps the reads of it. They fold, the declaration
  stays, and the pull request lists the mention that kept it. Before, the run
  made no change for that flag. In Python the reads still stay when any file
  could write the constant (`keys.NAME = …`, `setattr`, `mock.patch` and the
  like).
- **Python:** a flag read bound to a local is now inlined in a file that also
  uses Python 3.14 syntax elsewhere, an unparenthesised `except A, B:` or a
  template string `t"…"`. Before, one such line anywhere in the file refused
  the flag with a reason that did not mention it. Other syntax newer than the
  Action's Python still refuses, and the reason now says so.
- **Python:** a flag read bound to a local name (`res = client.is_enabled(…)`)
  is inlined when other functions in the same file use the same name. Before,
  the whole flag was refused with "the removal stopped part-way" as soon as a
  second function said `res`.
- **PHP:** Laravel's `abort_unless`, `abort_if`, `throw_unless` and `throw_if`
  are decided when the fold makes their condition a literal. A guard that
  passes is deleted, provided its other arguments are literals, names,
  `Foo::class` or a `new` of a class with literal arguments, so nothing with an
  effect goes with it, and never as the body of a braceless `if`. One that
  always stops becomes the call it was (`abort_unless(false, 404)` is
  `abort(404)`, `throw_if(true, new E())` is `throw new E();`). The pull request
  lists each, and says that code after a stopping guard is unreachable. A guard
  with an argument that can have an effect is left as written.
- **PHP:** Pest assertions get the same treatment PHPUnit's do. `expect(true)->toBeTrue()`
  and the other literal forms the fold made trivially true (`toBeTruthy`,
  `toBeFalse`, `toBeFalsy`, `toBe(<literal>)`, `toEqual(<literal>)`, with or
  without `->not`) are deleted, and one the fold made fail
  (`expect(true)->toBeFalse()`) is listed in the pull request as an assertion
  that can only fail. Before, both stayed with no mention.
- **PHP, Kotlin and Ruby:** a wrapper named after its own flag, such as
  `boops()` reading `enabled('boops')`, was kept by that read's key. The
  string is the flag's key, not a call by name, so the wrapper is now removed
  and its callers inlined, and the pull request no longer lists lines it
  removed itself. A string that names the wrapper anywhere else still keeps it.

## 2.1.0

### Added

- **Java and Kotlin:** a `private` field or property whose every read was in
  the removed code, such as the `private final FeatureflipClient flags;` a
  service was handed only to read the flag, is now removed, with the
  constructor parameter and statement that only set it and the argument every
  caller in the repository passed for it. When a framework may construct the
  class (a Spring stereotype, `@Inject`, `@Bean`, Lombok, reflection) or a
  caller cannot be proven, it is kept and named in the pull request instead,
  which before this release did not mention it at all. A plain Kotlin
  constructor parameter whose only read was in an `init` block the removal
  emptied is handled the same way.
- **TypeScript and JavaScript:** a JSX component that takes a boolean instead
  of a flag key can be named in `accessors` as
  `ConditionallyRender[condition?show:elseShow]`. When its condition folds to a
  literal, the element is replaced by the branch it renders, a render function
  included, so the pull request no longer keeps `condition={true}` and a dead
  subtree.
- **Go:** the pull request lists each call the removal kept running only as
  a discard (`_ = featureflags.GetClient(platform)`), which happens when a
  local that only the removed flag read used was initialized by a call. The
  call may have side effects, so it still runs; the note says to delete the
  line when the call only fetched a client or built a context for that read.
- **Swift:** a computed property or method of a type that the rewrite reduced
  to a constant (`var showInvite: Bool { !viewed && <read> }` served off
  becomes `{ false }`) is listed in the pull request under "now returns a
  constant", with the types it conforms to, other implementations of them and
  the files that call it, as C# and Java members already were. It is reported,
  not inlined.

- **Go:** a flag-keyed map entry written as a statement,
  `flags["old-checkout"] = true`, is deleted on the same evidence as one in a
  map literal: its value is a boolean agreeing with the kept branch, or the map
  is provably a flag map (a literal assigned to it, or another key assigned into
  it, is another flag of your project). One pinning the removed branch is kept
  and named in the pull request, as in a literal.
- **Go, Ruby, PHP, Swift and Dart:** a test assertion the removal turns into
  one that always passes is deleted, as it already was in C#, Java, Kotlin,
  Python, TypeScript and JavaScript, and listed in the pull request with the
  deleted mock lines. The assertions recognised are testify's
  `assert.True(t, true)`, `assert.False(t, false)` and `assert.Equal(t, b, b)`
  (and their `f` forms, and the same on `require`), RSpec's
  `expect(true).to be(true)`, `eq(true)`, `be_truthy` and `be_falsey`,
  minitest's `assert true`, `refute false` and `assert_equal true, true`,
  PHPUnit's `assertTrue`, `assertFalse`, `assertSame` and `assertEquals` on
  `$this`, `self::`, `static::` or `Assert::`, XCTest's `XCTAssertTrue`,
  `XCTAssert`, `XCTAssertFalse` and `XCTAssertEqual`, Swift Testing's
  `#expect(true)`, and package:test's `expect(true, isTrue)`,
  `expect(false, isFalse)`, `expect(b, b)` and `expect(b, equals(b))`. Only an
  assertion the removal itself produced is touched, and only one that is a
  statement of its own: one you wrote that way stays, and so does
  `ok := assert.True(t, true)`. An assertion the removal turns into one that
  always FAILS is not edited; the pull request names it so you can delete or
  rewrite the test.
- PHP: a name in `accessors` is now matched as a static call too, so with
  `accessors: active` Laravel Pennant's `Feature::active('my-flag')` folds as
  `Feature::for($user)->active('my-flag')` already did, including through a
  key constant. `Feature::someAreActive([…])`, `Feature::allAreActive([…])` and
  `Feature::inactive(…)` are left in place and reported.

- `accessors` takes Ruby predicate and bang method names such as `enabled?`
  and `is_enabled?`, so `Toggles.enabled?("my-flag")` and an Unleash-style
  `client.is_enabled?("my-flag", context)` are matched in `.rb` files and ERB
  templates. Before, a name ending in `?` failed the run at start-up. Such a
  name applies to Ruby and ERB only, and a run whose `languages` includes
  neither fails at start-up.
- A Ruby accessor named in `accessors` is also matched when it takes the key
  as a symbol, such as `Flipper.enabled?(:my_flag)` or
  `Feature.enabled?(:"my-flag")`. The SDK's own `bool_variation` still takes
  only a string.
- A Ruby flag key held in a constant declared in another file
  (`module Flags; NEW_CHECKOUT = "new-checkout"; end`, read as
  `client.bool_variation(Flags::NEW_CHECKOUT, ctx, false)`) is now resolved:
  each proven read is folded and the declaration is deleted with the last
  one. A read is proven only where nothing else in the checkout's Ruby could
  stand for the owner it names. Any other mention, a reflective use of the
  owner, or a gem's constant spelled elsewhere keeps the declaration and
  every read of it, each listed in the pull request. See "Declared in another
  file" in the README.
- **Java and Swift:** flag-keyed entries are now removed, as they already were
  in every other language. In Java that is a key/value pair of `Map.of(…)` or
  `ImmutableMap.of(…)` (including the map passed to
  `FeatureflipClient.forTesting`), a `Map.entry` argument of `Map.ofEntries`,
  a `put("old-checkout", true);` statement, and a `.put(…)` link of a builder
  chain. In Swift it is a pair of a dictionary literal, so
  `FeatureflipClient.forTesting(["old-checkout": true])` becomes
  `FeatureflipClient.forTesting([:])`. An entry set to the branch being
  removed is kept and named in the pull request, as in the other languages.

- **C#:** a key read as `nameof(Flags.Beta)` from an enum is resolved as an
  enum entry, and a key constant may be written
  `public const string Beta = nameof(Beta);`. Both were skipped before, because
  no string in the code holds the key.
- **C#:** an attribute gate named in `accessors`, such as `FeatureGate` or
  `RequireFeature`, is deleted when the flag is removed serving `true`, and a
  removal serving `false` is refused with the attribute named. An attribute
  naming a key constant no longer stops the reads beside it from folding.

### Changed

- **C# and Java:** a key constant named inside the branch the fold deletes no
  longer keeps the flag from being removed. A guard such as
  `if (!features.IsEnabled(FlagKeys.NewCheckout)) { log.Info("…", FlagKeys.NewCheckout); return; }`
  used to refuse every read in its file, because the logged constant is not a
  read. Under the treatment whose fold deletes that block, the read now folds
  and the declaration is deleted; the rewritten file is checked afterwards,
  and the flag is refused if the mention survived.
- **Kotlin, Swift, Dart and Go:** when the removal keeps a branch that
  declares a local, the branch is now flattened into the surrounding code if
  nothing else in the function uses those names. Before, Kotlin and Swift refused the
  whole flag and Dart and Go left a bare `{ … }` block.
- **Python:** code left unreachable after a `return`, `raise`, `continue` or
  `break` the removal produced is deleted, as it already was in the other
  languages.
- A bare `return` the removal leaves as the last statement of a function that
  returns nothing is deleted (not in Ruby, where it changes the return value).
- **TypeScript, JavaScript and Python:** a condition the removal reduces to
  `x && false` or `x || true` is simplified when `x` has no side effects (no
  calls or property reads).
- **Go, TypeScript, JavaScript, Python, Ruby and Swift:** parentheses the
  removal leaves around a whole `if`, `while` or similar condition are dropped
  where the language's formatter would drop them, so `gofmt`, Prettier, ruff,
  RuboCop and swift-format stay clean.
- Large TypeScript and JavaScript files are processed several times faster.
- **Java and Kotlin:** proving that a constructor parameter can be removed is
  several times faster in a large repository. It no longer reads every
  configuration and data file of the checkout to look for the class's name;
  git finds the files that spell it, and only those are read.
- A file that names the flag's key only as a segment of a package, namespace
  or import path (`package com.example.qos`, `import com.example.qos.Policy;`)
  is listed in a note of its own instead of under "still reference this
  flag". A short key that is also a package name no longer floods that
  caveat; a dotted string or code naming the key still stays in it.
- In a Dart package whose `pubspec.lock` resolves the Featureflip SDK to
  2.8.1, a plain `import 'package:featureflip/featureflip.dart';` that the
  removal leaves unused is now deleted, as it already was for 2.8.0 and
  earlier. Before, the import stayed in place on 2.8.1.

### Fixed

- **ERB:** a template with a `case` and its `when`s in separate tags is no
  longer skipped. A deleted tag now takes its whole line instead of leaving
  whitespace behind, which Rails renders the same. Plain `ERB.new` with no trim
  mode loses the blank line it rendered for that tag. A rewritten tag keeps its
  `-%>` and `<%-` and the layout of a multi-line tag.
- **Ruby:** code after a `return` the removal leaves is deleted whole. A comment
  or a final `if`/`else` used to leave part of it behind, sometimes reading a
  variable whose assignment had gone.
- **TypeScript and JavaScript:** a conditional spread no longer leaves
  `...([a])`, `...([])`, `...({})` or `...(false)`: it's spliced in or deleted,
  and a spliced-in literal keeps its order. A `.filter((x) => true)` the removal
  leaves is dropped when it's chained or its receiver is a fresh array. A
  standalone one stays, because `filter` copies and later code may rely on that.
- **C#, Java and the other languages that collapse blank lines:** a double blank
  line away from the removal stays as it was.
- **C#:** a kept branch holding `#pragma` lines is re-indented correctly.
- **TypeScript and JavaScript:** a call of a wrapper with no key that took one
  boolean argument, such as `isNew(false)`, folded to that argument instead of
  the flag's value, so a pull request could silently invert the code it kept.
  Check any such removal already merged.
- **PHP:** a Laravel Pennant write in application code, such as the
  `Feature::activate('k');` of an admin toggle, was deleted as a test double,
  which could leave one arm of an `if` empty. Only a write inside a test, a Pest
  hook or a PHPUnit `setUp` of a `*TestCase` is a test double now; any other
  stays and is listed as a remaining reference. Check any such removal already
  merged.
- **PHP:** a Pennant pin in a `beforeEach`, `beforeAll`, `setUp` or
  `setUpBeforeClass` is now decided. A pin of the kept value goes, and the hook
  with it when nothing else is left in it. A pin of the removed value means every
  test under the hook ran the removed branch, so those tests go, except any that
  pin the kept value themselves, and the hook with them. Before, the pin stayed
  and the tests under it failed, or the hook was left empty.
- **A read whose result is used as an object now refuses the flag, in every
  language.** `useFeature('k').on` folded to `true.on`, which in JavaScript is
  `undefined`, so a pull request could silently ship the opposite of the served
  value. A C# NSubstitute stub that could not be resolved folded the same way, to
  `true.Returns(v)`, which compiles and only throws when the test runs.
- A test double whose key is a constant of another file is now resolved like one
  with a literal key, in every language with double resolution. For C# that
  includes a receiver like `sutProvider.GetDependency<IFeatures>()`.
- **Kotlin:** a keyless wrapper called bare and negated inside its own class,
  as in the early-return guard `if (!enabled()) return`, is now inlined and
  the wrapper deleted. Before, the negation hid the call from the receiver
  proof, so the wrapper was kept returning the flag's value and every such
  caller was reported `receiver-unproven`.
- **Dart:** a private method or top-level function that only the removed
  branch called is now deleted, with its doc comment, and listed in the pull
  request. Before, it stayed, and `dart analyze` failed the pull request on
  `unused_element`. A private method only the deleted one called goes too.
- A flag-keyed entry (a seeded default, a test override, a registry line) is
  no longer deleted while any file still names the flag outside a comment.
  Before, a boolean entry was deleted even when no read of the flag had been
  rewritten, so the pull request's only effect was to change the value those
  reads got. The entry is now kept and listed, and when the entries were the
  only change the run could make the flag is refused (`unsafe-rewrite`) with
  the reason on the entry's file.
- **PHP, Java, C#, Dart and Kotlin:** an assertion the removal made trivially true
  is kept when it's the whole body of an `if` or a loop with no braces. Deleting
  it made the next statement run conditionally: `if (c) assertTrue(read); next();`
  became `if (c) next();`, which compiles. In Java and C# a read discarded as a
  statement in that position, in any file, did the same, and now refuses the flag.
  Check any such removal already merged.
- A TypeScript or JavaScript flag-keyed entry with an unquoted key
  (`{ oldCheckout: true }`) is now removed like a quoted one.
- The key's member of a TypeScript string-literal union is now removed, once
  nothing else names the key. Before, it stayed, and the file was listed as
  still naming the flag.
- The pull request no longer says a comment's code "is now unconditional", or
  describes behaviour "this pull request removed", while a read of the flag is
  still in place.
- A Ruby `recv&.read("k")` used as a condition now folds: to `false`, or served
  true to `recv`, which keeps the nil check. As a value it is still left alone.
- **C#:** one source file in a legacy encoding, such as a Latin-1 comment in a
  test, no longer stops every key constant in the repository from being
  resolved. Such a file is now read like any other. A UTF-16 file still can't
  be, and still makes those proofs refuse.
- **Ruby:** a key constant named by `private_constant :NAME`,
  `deprecate_constant`, `public_constant` or another symbol or string in the
  file that declares it is no longer deleted. Removing it left
  `private_constant` naming a constant that no longer exists, so loading the
  file raised `NameError`. The declaration and the reads it backs now stay,
  and the pull request lists the mention, as it already did when another file
  read the constant.
- **Kotlin and Dart:** a removal is no longer refused because the branch it
  deletes declares a variable with the same name as one a `$name` string
  template in the kept code reads. The check that catches a template left
  reading a deleted variable now looks at which declaration each template
  reads, so a declaration that goes with its dead branch no longer counts.
  A template left reading a deleted variable, or reading an outer variable of
  the same name instead, is still refused.
- **Kotlin:** removing a map's only flag-keyed entry no longer leaves a call
  that does not compile. `val overrides = mutableMapOf("k" to true)` used to
  become `val overrides = mutableMapOf()`, which cannot infer its type
  arguments. A sole entry is now deleted only where the map's type is fixed
  without it (a declaration typed as a map with type arguments, a builder with
  its own type arguments, or the argument of `FeatureflipClient.forTesting`),
  and is otherwise left in place and listed in the pull request.
- **Kotlin:** removing one flag-keyed entry from a larger map no longer
  narrows the map's type. In `val m = mutableMapOf("k" to true, "limit" to 5)`
  the value type is inferred from both entries, so with the flag's entry gone
  `m["x"] = false` stopped compiling. Such an entry is now deleted only where
  the map's type is fixed (the same three places as a sole entry) or where
  every entry maps a plain string to `true` or `false`, and only from the
  standard map builders, so a function taking two `Pair` arguments no longer
  loses one.
- A `directories` entry relative to the checkout, which is how a workflow
  normally sets it and what the default `.` is, now gets the same result as an
  absolute path. Before, a C# file's project could not be found from a
  relative path, so an unpublished project was treated as published and the
  removal of a gate's interface and constructor parameters stopped short.
- In C# and Java, a file that does not parse now stops a read of a key constant
  only if it could be on that reader's supertype chain. Before, one unrelated
  file stopped every such read in any class with a base type, across the
  repository, reported as `import-unresolved`.
- **PHP:** a static wrapper such as `Features::telegram()` was not inlined
  when any other class had a method of the same name, which is the normal case
  for an Eloquent relation. Only a call through its class reaches a static
  method, so its callers are now folded; a call of the shared name that cannot
  be told apart from it, such as `$model->telegram()`, is still left and
  named in the pull request.
- A C# target-typed `new(…)` is no longer mistaken for a call to a constructor
  when a literal before its first named argument rules that constructor out,
  so an unrelated `new(1, name: x)` no longer keeps a gate's constructor
  parameter.
- A stranded `require` of a local CommonJS module that only declares and exports
  is now deleted, as an ES import of such a module already was, and so is one
  whose load another top-level `require` in the file still makes.
- Deleting an argument at the end of a line no longer leaves a trailing space
  on that line.
- A Ruby key constant kept because another file reaches its owner no longer keeps
  the reads beside it, unless a file spells `const_set` or `remove_const`, which
  could replace the constant. Before, a Slim view naming a concern meant no diff
  at all, which in a Rails app was most key constants.
- A Java or Kotlin wrapper's own unit test, one that asserts on the member
  through a real instance of its class, now goes with the member. Before, it
  was folded to an assertion on a literal such as
  `assertThat(true).isEqualTo(toggleState)`, and a parameterised one failed for
  the row of the removed value.
- An assertion with a string-literal message is now handled in every language,
  as testify's already was: `assert true, "m"` is deleted, and `assert false, "m"`
  is named in the pull request. Before, only the form with no message was, so one
  that could only fail shipped unnamed. Rails' `assert_not` is handled too.
- A Java or Kotlin parameterised test that stubs a `true` or `false` literal is
  now decided like any other test, since a literal holds in every row. Before,
  it was left as it was and could block the removal of its wrapper. A stub of
  the test's own parameter is still left alone.
- **Dart:** the lines a removal deletes no longer leave a blank line just
  above a `}` or two blank lines in a row, which `dart format
  --set-exit-if-changed` rejects. Both came from deleting a statement that
  could no longer run, or an import left unused, next to a blank line.
- **PHP and Python:** a flag read in a server-side template, such as a Twig
  `{% if isFeatureEnabled('loans') %}` or a Django `{% flag "old-checkout" %}`,
  is now listed in the pull request among the files that still reference the
  flag. The Action does not rewrite templates, and it used to say nothing about
  them, so a pull request could present the removal as complete while the
  template kept reading a flag that no longer exists. A flag-keyed entry (a
  PHP `'loans' => true` default, for one) is now kept while a template still
  names the key, as it already was for a `.php` file. `.twig`, `.latte`,
  `.tpl`, `.volt`, `.j2`, `.jinja` and `.jinja2` files are checked for the key,
  and `.html` and `.htm` files only when it sits inside a `{% %}` or `{{ }}`
  tag, so a static page that mentions it stays quiet.
- A removal in a large file no longer takes minutes. Cleaning up the
  variables and imports a removal leaves unused re-read the whole file for
  every local in it, so its time grew with the square of the file's length,
  and with the cube in PHP: one flag read in a 2,000-line PHP controller took
  about ten minutes per treatment. It now takes a few seconds, and the
  changes it makes are the same.
- **Dart:** a removal in a large Flutter app no longer takes minutes per
  flag. Cleaning up what it left unused re-read the file once for every name
  an imported library declares, re-read a function once for every local in
  it, and re-read each imported library once for every file importing it.
  Each is now read once, and the changes are the same.
- A mockito-kotlin `whenever(…) doReturn true` is now resolved like
  `whenever(…).thenReturn(true)`. Before, the stub stayed, so a test could go on
  asserting the removed branch, or the whole flag was refused.
- Sinon's keyed stub of a read, `stub.withArgs('k').returns(true)`, is now
  resolved. Before, it stayed, so a test could go on asserting the removed branch.
- In a repository mixing Java and Kotlin, a test in one language that stubs a
  read in the other is now resolved. Before, the whole flag was refused.
- Kotlin stubs inside minutest, Spek and Kotest tests are now resolved, and so
  are stubs in any other lambda of a test. A stub of the removed value in a
  `context`'s `before` takes the whole context with it. Before, the whole flag
  was refused.
- RSpec and Mocha stubs of a Ruby wrapper are now resolved when the stub stands
  on the wrapper's own class. A stub of the kept value is deleted, and a test
  or nested `context` stubbing the removed value goes with it. Before, any stub
  left the wrapper and its callers in place, with only its body folded.
- `unittest.mock` patches of a Python function wrapper are now resolved when
  they state their answer: `@patch(…, return_value=…)`, `with patch(…)` and
  `mocker.patch(…)`. A patch of the kept value goes with the parameter it
  injects, and a test patching the removed value goes with it. Before, any patch
  left the wrapper and its callers in place, with only its body folded.
- Laravel Pennant's `Feature::activate('k')` and `Feature::deactivate('k')` in a
  PHP test are now resolved. One setting the kept value is deleted, and a test
  setting the removed value goes with it. Before, the line stayed, naming a flag
  that no longer exists.
- A stub of another method of a Ruby wrapper's class no longer keeps the wrapper.
- **Java and Kotlin:** a configuration or data file the Action cannot read now
  keeps a constructor parameter in place, named in the pull request. Before,
  such a file was skipped as if it did not name the class, though it could be
  the bean definition or service entry that constructs it.
- A spec title spelling a Ruby wrapper (`describe ".on?" do`) no longer keeps it.
- A data file in a legacy encoding no longer counts as naming every wrapper and
  key constant in the repository. It still does if it spells the name.
- A Java or Kotlin test class whose last test a removal deleted now goes, with
  its file when nothing else is declared there, and so does an emptied `@Nested`
  class. Before, it stayed with only its mock fields, or an empty body. A class
  that extends or implements anything, or that anything else names, stays.
- Deleting a Java import group whole no longer leaves two blank lines in a row.
- A Java or Kotlin test title that names the kept branch by the flag's key now
  loses that condition, as a TypeScript title does: `@DisplayName("…")`, a Kotlin
  backtick name, and a Kotest, Spek or minutest `test("…")`. The file then no
  longer names the key, so it is no longer listed as left unprocessed.
- **TypeScript and JavaScript:** a removal that would leave a condition such as
  `if (req.path() !== "/health" && false)` is now refused instead of opening a
  pull request. ESLint's recommended `no-constant-condition` rule fails on
  that condition, and it cannot be simplified without dropping the call, so
  the pull request would have failed your lint. The same applies to
  `|| true`, and to `while`, `for` and `do` loops.
- **PHP:** the README now says that a Laravel Blade view is not rewritten. A
  view that reads the flag was already left untouched and listed under "still
  reference this flag", but nothing said so.
- **PHP:** a `use` stranded by a removal stayed when the file had a variable
  with the same name in another case, such as `use App\\Entity\\Tag;` beside
  `$tag`. A `use` never binds a variable, so the import is now removed.

## 2.0.0

This is a new major version, so `@v1` stays on 1.19.0. Change
`featureflip-flag-cleanup-action@v1` to `@v2` in your workflow to get this
release and the ones after it. The CircleCI orb moves to 2.0.0 for the same
reason: its default image is now `:v2`.

### Changed

- The transform engine's lines in the job log (`File: … with N rewrites`,
  `Rewrites used the following rules: …`) are now labelled
  `featureflip.engine` instead of `polyglot_piranha`. If a log filter of yours
  matches the old name, update it.
- **Breaking:** the outcome reported when the transform engine rejects its own
  rewrite is now `[engine-error]`, renamed from `[piranha-error]`. It still
  fails the run with exit code `1`. Update any script or log search that
  matches the old label.

### Fixed

- **C#:** a few values were given the wrong static type when proving a removal
  safe, which could let a removal through that the C# compiler binds
  differently: `list.ToArray()` (an array, not the list), a call that no
  method of your type with that name can take (an extension method binds
  instead), and a local declared after a label (`L: object o = …;`), which
  hides a field of the same name like any local.

- **C#:** a setting of an options class shared with other settings is no
  longer kept by its binding when the section is held in a local
  (`var section = configuration.GetSection(…)`), when names are qualified by
  the class's namespace, or when a `PostConfigure<T>(o => …)` sets the other
  settings from subsections of other names.

## 1.19.0

### Added

- **TypeScript and JavaScript:** when the removal leaves every use of a
  component in the file passing the same `true` or `false` to a prop
  (`<Row showExpiry={true} />`), the prop is removed: from each element, from
  the component's destructured parameter and from its props type, and what it
  guarded inside the component is simplified. This applies to a component that
  is not exported and is used only as a JSX element in its own file, whose
  props type is its own. A prop that already received that literal before the
  run is left alone.
- **TypeScript and JavaScript:** a prop the removal leaves unread inside such
  a component (`showExpiry && isExpired(expiresAt)` folding to `false` leaves
  `expiresAt` passed and never used) is removed the same way: from each
  element, the destructured parameter and the props type. A value passed to
  it that could do work (a call) keeps the prop.
- **TypeScript and JavaScript:** removing a prop every element passes as the
  same `true` or `false` now reaches an exported component, when every file
  that renders it imports it by name from the file that declares it and does
  nothing else with it. The prop goes from the component and from every
  element, in each of those files. A component that is re-exported, imported
  under another name or as a namespace, or mentioned any other way is left
  alone.
- **TypeScript and JavaScript:** a parameter of a function that is not
  exported and is only ever called by name in its own file is removed when
  every call now passes it the same `true` or `false` the removal wrote
  (`formatRow(name, true)`): from each call and from the function, and what
  it guarded inside is simplified. A parameter the removal leaves unread in
  such a function goes the same way, with the value each call passed for it,
  unless a call passes something that could do work.
- **C#, Java, Kotlin, Python, TypeScript and JavaScript:** a test assertion
  the removal turns into one that always passes (`Assert.True(true)`,
  `assertTrue(true)`, `expect(true).toBe(true)`, `assert True`) is deleted,
  and listed in the pull request with the deleted mock lines. Only an
  assertion the removal itself produced is touched: one you wrote that way
  stays. An assertion the removal turns into one that always FAILS
  (`Assert.True(false)`) is not edited; the pull request names it so you can
  delete or rewrite the test.

### Changed

- A comment directly above a statement the removal deleted outright (an
  inlined declaration, a guard of the removed branch) no longer stays behind
  unmentioned. When it describes the flag's state ("while the flag is off")
  it is deleted with the statement; otherwise it is listed under "comments
  describe behaviour this pull request removed". A comment separated from the
  statement by a blank line, a doc comment, and one that may also describe
  the next statement are left alone.
- A comment that says "the flag" or names a local the removal inlined away
  is now reported when it sits above the changed code in the same block, not
  only on or directly above the changed line. Nothing is edited.
- **TypeScript and JavaScript:** a file whose only mention of the removed
  flag's key is a string element of an array that also holds a computed
  value, such as a query cache key `['scheduled-changes', orgId] as const`,
  is now listed in its own informational group in the pull request body
  instead of under "still reference this flag". Nothing is rewritten. A list
  of nothing but string keys, an array handed straight to a call, and a
  template string still get the warning.

### Fixed

- **C#:** a string a test passes to a private helper that only binds it as a
  parameter of interpolated SQL (`ExecuteSqlAsync($"… {payloadJson} …")`) no
  longer keeps an option of the same name in place, even in a file that
  compares strings ignoring case.

- **C#:** an options class that declares a constant other than a string
  (`public const long MaxBytes = 64 * 1024;`) no longer keeps its removed
  setting in place, when the constant's value is only literals and operators.

- **C#:** a Terraform file that names a removed type or options class only
  in a comment no longer keeps it. The comment is listed in the pull request
  with the other prose. A name in a value or a heredoc still keeps it.

- **C#:** a read through a local created as a class of yours that derives
  from a library class (`using var factory = new MyAppFactory(); …
  factory.Gate.Enabled`) no longer counts as possibly going through a member
  the library class supplies, when the member read is one your class
  declares. Before, it kept a gate family's options setting in place.

- **C#:** a configuration key of another section that ends in a removed
  option's name (`configuration.GetValue("Quartz:Enabled", true)`, the option
  bound to section `Scheduled`) no longer keeps that option and its
  configuration key.

- **C#:** a string that matches a removed option's name (for example
  `nameof(Feature.Enabled)`) no longer keeps that option and its
  configuration key. This applies when the string is in a file whose every
  by-name lookup goes through `typeof(T)` of an unrelated type, or passes only
  constants that name something else (`Entry(e).Property(nameof(E.Date))`).

- Every file path in a pull request body, in a dry run's diff and in the
  run's report is now written relative to the repository root. A run over
  several directories used to mix three spellings (from the root, from the
  swept directory, and absolute), and a dry-run diff carried absolute headers
  that `git apply` rejects; it now applies from the checkout root.
- A file whose every comment naming the flag is already listed under what
  that comment describes is no longer listed again, with no lines under it,
  under "mention this flag only in comments".
- **TypeScript and JavaScript:** in a file holding JSX, a named import the
  removal left unused is now removed even when its `import { … }` keeps other
  names, if the file's compiler config sets `"jsx": "react-jsx"` or
  `"react-jsxdev"` and the file has no `@jsx` pragma. It used to be kept,
  which fails the build under `noUnusedLocals`. Under the classic runtime, or
  when the runtime cannot be told, such imports are still kept, since an
  element can call one of them without naming it.
- **C#:** a `typeof(X)` no longer keeps `X`'s constructor parameter when every
  value made from it in its file only reads names, flags and attributes. That
  holds through private fields, loops, LINQ lambdas, method groups and private
  helpers, as in a structural test over a `private static readonly Type[]` of
  controllers that checks each one's attributes. Anything that could construct
  the class or hand the value elsewhere still keeps it.

- **C#:** a target-typed `new(…)` that passes a literal where a constructor
  takes a class no literal converts to (a class or struct of your repository
  with no conversion operator, or a framework type such as `TimeProvider`,
  `HttpClient` or `ILogger<T>`) is no longer taken for a possible call to it.
  `new(null, 1)` beside a constructor taking `(IGate gate, TimeProvider
  clock)` used to keep the whole gate family's leftovers.

- **C#:** a constructor parameter is no longer kept just because your
  repository has another type of the same name in another namespace (two
  `OrdersController`s, say). Each spelling of the name is looked up
  the way C# does, through the file's namespaces, `using` directives,
  global usings and `<Using>` items, and spellings of the other type are
  left alone. A target-typed `new(…)` is looked up from the type its
  context spells (`private OrdersController Make() => new(…)`). A spelling
  that could name either still keeps the parameter.

- **C#, Kotlin, Swift, Go, Ruby and Python:** an inlined local whose
  declaration sat directly under a comment no longer leaves its `;` at the
  end of that comment (`// Read once per run.;`).
- **C#, Java, TypeScript and JavaScript:** a kept branch that declares a
  local is now unwrapped into the surrounding code, as a branch without one
  already was, when nothing else in the enclosing method or function uses
  that name. It used to stay as a bare `{ … }` block. A branch holding a
  `using` declaration keeps its block, since the block decides when the
  resource is disposed.
- **TypeScript and JavaScript:** a destructured name the removal left unused
  is dropped from its pattern when the pattern keeps another name
  (`const { orgId, tab } = useParams()` becomes `const { tab } =
  useParams()`). When it was the pattern's only name and the value is a
  call, the call is kept as a statement, as it already was for
  `const orgId = useParams()`; a type argument that spells the name
  (`useParams<{ orgId: string }>()`) no longer keeps the whole declaration.

- **C#:** a test host's registration through its own helper,
  `Replace<IGate>(services, new StubGate(enabled: true))`, now goes with the
  gate family when the fake is built from literals by a constructor that only
  stores them, also inside a lambda. When the family's leftovers are kept,
  the pull request now lists every reason, not only the first one found.

- **C#:** a test whose only subject is the removed gate's type, every
  statement an assertion naming it in `typeof(…)` (for example, that a class's
  constructors never take the gate), is now deleted with the gate family and
  listed in the pull request. Before, it kept the family's empty interface and
  classes in place.

- **C#:** a test class whose every test the removal deleted is now deleted
  too, with its file when nothing else is declared in it, even when the gate
  family's other leftovers are kept. Before, the pull request shipped the
  empty class. A class with an attribute, a base class or another part is
  kept.

- **C#:** a private helper's parameter of the removed gate's interface that
  only registers it (`services.AddScoped(_ => gate);`) is now removed with
  those registrations and every caller's argument, and the test doubles passed
  for it then go with the gate family. Before, it kept the family's empty
  interface and classes in place. A named argument (`gate: mock.Object`) no
  longer keeps a test double either.

- **C#:** a static helper's parameter of the removed gate's interface that the
  removal left unread is now removed, together with the argument every caller
  passes for it. The field or constructor parameter that a caller passed in
  then goes with the rest of the gate family. Before, those leftovers kept the
  family's empty interface and classes in place.

- **C#:** a gate family's leftovers now also go when they reach a primary
  constructor (`class Job(IGate gate, …)` whose `gate` the removal left
  unread), a test local built as a fake and only passed to such a
  constructor, or a test harness's public auto-property holding a fake that no
  other file names.

- **C#:** `attributes.Add(new(…))` on a local `List<T>` (declared, `var` from
  a method of the same class, or `var rows = new List<T>()`, also from inside
  a loop or lambda) is read as creating a `T`, so it no longer
  counts as a possible call to an unrelated constructor. Neither does a
  property initializer `public HashSet<string> Names { get; } = new(comparer);`,
  which creates the property's type.

- **C#:** a test's Moq field built by a target-typed `new()`
  (`private readonly Mock<IGate> _gate = new();`) now counts as a double of the
  gate, like one built with `new Mock<IGate>()`. Before, it kept the gate
  family's empty interface and classes in place.

- **C#:** a statement that only asks the container for the removed gate's
  interface, `scope.ServiceProvider.GetRequiredService<IGate>();` (what the
  removal leaves of `var gate = …` once nothing reads `gate`), now goes with the
  gate family. Before, it kept the family's empty interface and classes in
  place. The container then no longer creates the gate at that point.

## 1.18.1

### Fixed

- A Featureflip organization that is read-only because it is over its plan's
  limits is no longer reported as a token problem. When linking a removal PR
  to its flag is refused for that reason, the run says so once, stops trying to
  link for the rest of the run, and the PRs open as usual.

## 1.18.0

### Fixed

- **C#:** more of the constructor parameters a removal leaves typed as a
  deleted interface are now removed instead of kept with a finding. A
  target-typed `new(…)` elsewhere is no longer taken for a possible caller
  when it passes a `const` of a built-in type where that constructor takes
  the interface, or when its own type is clear from where it stands: assigned
  to a local, parameter, array element or `List<T>` element of a declared
  type, returned from an operator or conversion operator, or set by an index
  initializer of a `Dictionary<K, V>` or `List<T>`.

- **C#:** the gate class's own unit test, which builds the class and reads
  the member (`var gate = new FeatureflipGate(client); Assert.Equal(flagValue,
  gate.IsEnabled);`), is now deleted with the member, as a test fake's test
  is. Before, the read was replaced with the kept value, leaving
  `Assert.Equal(flagValue, true)`: an analyzer error that broke the build, and
  a failing row. Reads through the class outside test methods are still
  replaced.

- **C#:** a test helper whose `bool` parameter defaults to the value being
  removed, called somewhere without an argument, is now left as it is and
  your gate kept. Such a call never chose the removed value and may depend on
  it, so rebuilding it with the kept value could leave a failing test. Before,
  the call ran with the kept value and was listed for review. A test that
  passes the removed value itself to a public helper is now deleted, as for a
  private one.

- **C#:** a read of a member named like your gate's, through a value whose
  type can't be a gate, no longer keeps the gate in a file that also holds
  one. The value's type is followed through locals, your types' declared
  fields, properties and methods (base classes included), `new`, `await`,
  and the LINQ and EF Core operators that keep their source's element type
  (`(await h.Context.Subscriptions.SingleAsync(s => …)).IsEnabled`). A
  projection such as `Select`, `dynamic`, or an extension method of the
  operator's name in your code still keeps it.

- **C#:** a string naming the gate's member passed to a private helper of
  the same class, which only uses it as a key into such a value
  (`ex.Errors[field]`) or in a test framework's value assertion, no longer
  keeps your gate.

- **C#:** a test that builds its fake at the removed value and switches it
  to the kept one halfway (`var gate = new StubGate(enabled: false); …
  gate.Enabled = true;`) now loses its removed-value half. The fake is built
  with the kept value, and a local built from it in between, such as
  `var job = new Job(gate, …)`, stays, as does one read from a property that
  only reads a field or throws (`factory.TenantContext`). The other way round, a fake built at
  the kept value and switched to the removed one (`var stub = new
  StubGate(true); … stub.Enabled = false; …`), loses the write and what
  follows it, as a fake set up in a field already did. Before, any
  construction in such a test kept the gate.

- **C#:** a constructor argument that builds a class whose constructors only
  assign their parameters (`new StubGate(enabled: true)`) no longer keeps an
  unread constructor parameter. Before, such an argument counted as one that
  may do work.

- **C#:** a private test helper whose `bool` parameter builds a test fake and
  is also passed on to a public static helper building the same fake
  (`TestExpiry.Create(ctx, expiration: gateOn)`) is now pinned. That argument
  takes the kept value, which the public helper's own rebuild then removes.
  Before, the second use of the parameter kept your gate.

- **C#:** a statement whose comment or string happens to hold a mocking
  word ("checked every tick", `"Returns"`) is no longer taken for a mock's
  setup. Before, a gate read in it kept your gate as a test double.

- **C#:** a string naming the gate's member in a file that holds the gate no
  longer keeps it when it is only part of an exception thrown with nothing
  but constants (`throw new ValidationException(new[] { new
  ValidationFailure("IsEnabled", "IsEnabled is required.") })`). A by-name
  lookup's own name argument still counts.

- **C#:** a mock whose name a lambda's body also reads in the same file
  (`services.AddScoped(_ => gate)` in a helper) is now resolved. Before, its
  `Setup` lines stayed and kept your gate.

- **C#:** a test class whose constructor only stubs the kept value
  (`public Tests() => _gate.SetupGet(g => g.IsEnabled).Returns(true);`) now
  loses that constructor, when it is the class's only one. Before, the stub
  kept your gate.

- **C#:** a test phase run at the removed value is now cut even when it
  builds an object whose initializer sets a member named like something the
  rest of the test reads (`new HttpRequestMessage(…) { Content = … }`, then
  `res.Content`). Before, the whole test stayed and kept your gate.

- **C#:** a string naming the gate's member passed to a test framework's
  value assertion (`Assert.Contains("IsEnabled", ex.Errors.Keys)`,
  `Assert.Equal`, `Assert.AreEqual`, …) no longer keeps your gate, when
  `Assert` can only be the framework's class.

- **C#:** a gate read through a primary-constructor parameter of a class
  that implements a library interface (`class Job(IGate gate) : IJob`) is
  now proven. Before, the library base was taken to maybe declare a member
  of the parameter's name, and the read kept your gate. A library base spelled
  like a class (`JobBase`, `IOException`) still does.

- **C#:** a file is no longer taken to reach a gate just because it calls
  some `Create(…)` when a static helper of that name returns something
  holding the gate (`TestProtectors.Create()`). Such a static member is
  reached only through its class's name, which the file would spell. An
  instance or extension method of the name, or a `using static`, still
  counts.

- **C#:** a test harness from a static factory that returns its own class
  (`using var h = Harness.Create();`) is now typed like `new Harness()`, so
  a fake's knob written through it (`h.Gate.Enabled = false`) is resolved.
  Before, the write kept your gate.

- **C#:** fewer target-typed `new(…)` calls elsewhere keep an unread
  constructor parameter. One in a project that can't reference the class no
  longer counts for a primary constructor either, nor does one passing a
  literal (`new(Limits.Projects, 1, 3)`) where the constructor takes an
  interface your code declares. Before, either kept the parameter.

- **C#:** a constructor parameter of a type the removal deletes is now
  removed when some file builds a class with a target-typed `new(…)` in a
  conditional whose other branch is `null`, `default` or another `new(…)`
  (`dto is null ? null : new(dto.Id, …)`). Before, that `new(…)` kept the
  parameter, whatever class it built.

- **C#:** a public instance helper of a `sealed` test class whose `bool`
  parameter builds a test fake (`h.CreateHandler(planGated: true)` on a
  harness from `using var h = Harness.Create()`) is now rebuilt like a public
  static one. The fake is built with the kept value, the parameter and every
  argument go, and each test that passed the removed value is listed for
  review. Calls on another class's method of the same name are left alone. A
  `<paramref>` naming the parameter in either kind of helper's doc comment
  becomes `<c>…</c>` instead of keeping your gate.

- **C#:** a pinned test helper whose every call keeps its test (each passes
  the kept value, or the default rebuilds it) is now edited. Before, its
  parameter, arguments and fake stayed as they were, and the fake kept your
  gate's member.

- **C#:** more of an emptied gate family's leftovers are now removed instead
  of keeping the interface and its fakes in place:
  - a test fake that also serves other gates (`class OnGate : IGateA, IGateB`)
    loses the removed gate's interface from its base list, and its
    registration for that interface goes;
  - two private fakes of the same name nested in different test classes both
    go;
  - a registration whose factory passes only literals
    (`services.AddScoped<IGate>(_ => new FixedGate(true))`) goes;
  - a `new(…)` whose context names the consumer being rebuilt (`Handler
    Make() => new(…)`) loses its argument like `new Handler(…)`;
  - a C# file of a project that cannot see yours, which does not parse or
    builds things with `new(…)`, no longer keeps a constructor parameter.

- **C#:** a private static test helper whose `bool` parameter builds a test
  fake no longer keeps your gate just because its body calls another method of
  the same name on a value. `BuildServiceProvider(…, bool planGated = false)`
  ending in `return services.BuildServiceProvider();` is now pinned like any
  other helper.

- **C#:** a test whose call to a pinned private helper gets the value being
  removed only through the parameter's default (`BuildServiceProvider(cs)` with
  `planGated = false`) is no longer deleted. It runs with the kept value, and
  the pull request lists it for review, as for a public helper. A call that
  passes the removed value explicitly still deletes its test.

- **C#:** a read through a dependency-injection lookup used directly
  (`Assert.True(sp.GetRequiredService<IGate>().IsEnabled)`) is now rewritten
  like one through a `var` it initializes, instead of keeping your gate. Lookup
  reads also stay proven in a repository where some C# file does not parse
  but only *calls* `GetRequiredService<…>()`. Before, any such file spelling the
  name switched the proof off for every file.

- A request to the Featureflip or GitHub API that stalls or drops no longer
  fails the run on its own. Each read now waits up to 30 seconds for an answer
  rather than 5, and one that times out or loses its connection is tried again
  twice, after 1 and then 4 seconds. A write is never repeated, since one that
  timed out may still have landed. An error the API answered with is reported
  as before.

- A symlink that points at itself, or into a loop of links, no longer stops
  the run. A checkout holding one (`node_modules -> node_modules`, say) failed
  every flag with `Symlink loop from …` when the link had no file extension or
  a source one. Such a link names no file, so it is now skipped like any other
  file nothing can read.

- **C#:** a wrapper member could be removed while a controller still read it
  through MVC's `ViewBag`. `ViewBag` is a library member typed `dynamic`, so a
  file that stored your gate in it (`ViewBag.Gate = _gate;`) could hand it to
  another controller that read `ViewBag.Gate.IsEnabled` while spelling none of
  your gate's types. That read was not counted. Now it is judged like one in
  a file that names your gate. This covers a read inside a class with a
  library class among its bases (directly or through bases of your own, a
  class nested in one included) whose receiver may start at an inherited
  member. It also covers a read anywhere through a member such a class of
  yours inherits (`_inner.ViewBag…`, `new HomeController().ViewBag…`, but
  not `_db.Flags…` when your context declares `Flags`). A read through a
  lambda's parameter or a declared variable of another type is cleared as
  before, and so is a base named like an interface (`I` and a capital).

## 1.17.0

### New

- **TypeScript and TSX:** OpenFeature's React SDK. `useBooleanFlagValue("KEY",
  default)` from `@openfeature/react-sdk` is removed like `useFeatureFlag`,
  including through a key constant, a key imported from another file, and a
  hook of your own that wraps it. `client.getBooleanValue("KEY", default)` is
  removed in a file importing `@openfeature/react-sdk` when `client` is what
  that file got from `useOpenFeatureClient()`, as it already was in a file
  importing `@openfeature/web-sdk`. A client from one of your own modules is
  left alone there, since it may be a server client. A file that also mentions any other `@openfeature/…`
  package, a server client included, is still left alone. `useBooleanFlagDetails`
  and `useFlag` return details rather than a value and are not rewritten. The
  import a removal strands is kept as `import "@openfeature/react-sdk";` unless
  you list the package in `side-effect-free-modules`.

- **TypeScript and JavaScript:** a value the removal shortened goes back onto
  the line of its `=` or `:` once it fits. `const pollEnabled =\n
  onboardingEnabled && !dismissed;` comes out as `const pollEnabled =
  !dismissed;`, and the same goes for an assignment, an object property and a
  class field. A JSX element written over several lines goes back onto its
  tag's line the same way: `<Provider\n  flags={{ a: true, b: false }}\n>`
  with `b` removed becomes `<Provider flags={{ a: true }}>`. Only a layout the
  line's length needed is joined: a value you wrapped although it fit, one
  that still spans lines, and one with a comment where it breaks are left as
  they are. The width is the one the list joins use.
- **TSX and JavaScript:** a fragment the removal leaves directly inside
  another fragment or an HTML element, as `{flag && (<>…</>)}` folds to, is
  replaced by its children, re-indented. Under a component (`<Menu>`), a
  fragment holding text, and a `React.Fragment` with a `key` stay as they
  are, as do `<option>`, `<textarea>` and `<title>`.

- **TypeScript and JavaScript:** a JSX element whose attributes the removal
  deleted no longer leaves its `>` on a line of its own. When an emptied or
  default-equal prop is dropped from an element written one attribute per
  line, `<TestProvider\n  flags={{}}\n>` comes out as `<TestProvider>`,
  and `<Panel\n  a={1}\n  b\n/>` with `b` removed as `<Panel a={1} />`.
  The conditions are the ones the parameter and argument lists follow: every
  attribute left on one line, no comment inside the element, and a joined
  line that fits your Prettier `printWidth`, your `.editorconfig`
  `max_line_length`, or, with neither, the file's longest line. A Prettier
  configuration that sets `singleAttributePerLine` now joins nothing, lists
  included.
- **TypeScript and JavaScript:** an argument equal to the callee's default is
  now left out when the callee is a test helper declared inside a block, such
  as `const renderWithProviders = (ui, flags = {}) => …` inside
  `describe(…, () => { … })`. `renderWithProviders(<Page />, {})` comes out
  as `renderWithProviders(<Page />)`. The helper must be bound once, to a
  function, in a block that encloses the call, and never re-assigned. An
  unexported top-level `let` helper is followed the same way as a `const`.
- **Every language:** a file that names the flag's key only as a URL path
  segment in a string, such as
  `` fetch(`/environments/${id}/scheduled-changes`) `` or
  `[HttpPost("{id}/scheduled-changes")]`, is now listed in its own ℹ️
  paragraph of the pull request instead of under "still reference this
  flag". An endpoint named after its feature used to make a complete
  removal look unfinished. Nothing about what is rewritten changes.
- **Every language:** when `directories` leaves part of the checkout out,
  the pull request now names the files there that also mention the flag,
  in an ℹ️ paragraph ("N file(s) outside the swept directories also name
  this flag; not examined"). Before, a script or `README` outside the
  configured directories that still named the key went unmentioned, and the
  pull request read as if nothing was left. Nothing outside `directories` is
  edited.
- **C#:** a test fake whose one constructor stores its `bool` argument in the
  knob its member reads (`public StubGate(bool enabled) => Enabled = enabled;`)
  no longer refuses its gate family as `sibling-not-inert`. Each construction
  is judged by its argument like any other knob setting: `new StubGate(true)`
  with the kept value stays, a test building it with the removed value is
  deleted, and a construction passing a variable keeps the wrapper.
- **Every language:** a comment beside the change that describes the branch
  the removal kept ("the member lookup only runs while the flag is on", when
  the flag serves `true`) is now listed in its own ℹ️ paragraph, since its
  condition is gone and the code it describes now always runs. It used to
  sit in the general list of flag-state comments, or, in a comment of
  several lines where another line named the flag, not be listed at all.
  Comments are still never edited.

- **TypeScript and JavaScript:** a new input, `side-effect-free-modules`,
  takes module specifiers whose load does nothing, spelled exactly as your
  imports spell them (`@/lib/featureflip, ./keys`). An import the removal
  left with nothing to import is deleted instead of being kept as a bare
  `import "…";`, and a CommonJS `require` likewise. Since this rests on your
  word rather than a proof, the pull request names each import deleted this
  way. The default is empty, which changes nothing.

### Fixed

- **C#:** a `[Theory]` writing its own `bool` parameter to a test fake's knob
  (`_gate.Enabled = on;`, the rows saying `on`) no longer keeps the gate. Each
  row passing the value being removed is deleted, the column and the
  parameter go with the write, and a theory left with no row goes whole.

- **C#:** a public static test helper whose `bool` parameter builds a test
  fake, called from other files (`TestExpiry.Create(ctx, expiration: false)`,
  `TestExpiry.Off(ctx)`), no longer keeps the gate. The fake is built with the
  kept value, the parameter and every call's argument go, and no test is
  deleted: each one that passed the value being removed is listed in the pull
  request for review.

- **C#:** a receiver read with a member none of your gate's types declares
  is proven no gate even when its type comes from a library
  (`var config = await response.Content.ReadFromJsonAsync<Dto>()`) or an
  untyped lambda parameter (`c => c.IsEnabled && c.Strategy == 1`), and when
  that member is passed as an argument (`Assert.Equal(key, config.Key)`) or
  compared with `==`/`!=`. These assume no library hands one of your types
  back as `dynamic`, as the per-member proof already did. The argument and
  comparison cases also require that no file of yours declares or calls a
  method of that name. A `foreach` variable is judged like any other local; any
  `foreach` in a member used to leave its name unproven.

- **C#:** a read through a receiver also read with a member none of your gate's
  types declares is proven again in a repository where some C# file does not
  parse, unless that file may declare one of the types or a type they inherit
  from. A `dynamic` in a comment or a string no longer counts as one, and a
  file outside every project no longer counts just because some project
  compiles a file from outside its own folder.

- **TypeScript and JavaScript:** in a file importing `@openfeature/web-sdk`,
  a `getBooleanValue` read on a client imported from one of your own modules
  was rewritten too. If that client was a server client, its read was a
  promise, and the result (`true.then(...)`) failed at runtime. The read is
  now rewritten only when the same file binds the client to
  `OpenFeature.getClient()`, with `OpenFeature` imported from
  `@openfeature/web-sdk` under that name. Other reads are left alone, and the
  pull request names the file as still referencing the flag.

- **C#:** one test or method that names your gate's interface no longer makes
  every unrelated `IsEnabled` in the same file block the gate's removal. Each
  member is judged on its own text: one that names nothing leading to the
  gate (an Entity Framework row's `config.IsEnabled`, say) is not counted.
  A file of your repository outside every project (a test fixture, say) no
  longer counts for whether the repository uses `dynamic`.

- **C#:** two ways a wrapper member could be removed while a file still read
  it. A file that spelled none of your gate's types could take one from a
  member typed `dynamic` in another file (`var x = D.Get(); x.IsEnabled`), or
  call a static wrapper's member by its bare name under a `global using
  static` (or a project `<Using Static="true">` item). Such a read was not
  counted, and the removal broke the build. A member typed `dynamic` now
  counts like one returning your gate's interface, and a `global using
  static` naming one of your gate's types means no file is cleared just for
  the names it spells.

- **C#:** a project that compiles a file from another project's folder
  (`<Compile Include="..\Other\Stub.cs" Link="Stub.cs" />`) no longer makes
  every gate in the repository count as published. The file now counts as
  compiled by both projects. A link the tool cannot resolve, through a
  property or a wildcard, still makes every project count as reachable.

- **C#:** a target-typed `new(…)` is read by the type its context declares.
  A property `Dictionary<string, string> Txt { get; } = new(comparer)` no
  longer counts as a possible construction of a test fake built through its
  constructor's `bool`, and a test helper `Svc Make() => new(_fake)` hands the
  fake to the code under test rather than letting it escape.

- **C#:** a private test helper whose `bool` parameter builds a test fake
  (`private static Policy Make(bool enabled = true) => new(new
  StubGate(enabled));`) is resolved like one whose parameter chooses a stub:
  each test calling it with the value being removed is deleted, the others
  lose the argument, the parameter goes, and the fake is built with the kept
  value. A helper passing its own `bool` parameter on to such a helper
  (`Make(bool enabled = true) => Build(enabled)`) is resolved the same way, by
  its own calls. A call passing the kept value no longer has to be in a plain
  test: one in a `[Theory]` just loses the argument.

- **C#:** a test fake whose member is a get-only auto-property set from its
  primary constructor (`class FixedGate(bool on) : IGate { public bool
  IsEnabled { get; } = on; }`) is now handled like one returning `=> on`. A
  target-typed `new(…)` that cannot build a fake (it passes the wrong number
  of arguments, or sits outside the type a private fake is nested in) no
  longer keeps the gate.

- **C#:** an xUnit `[Theory]` that passes its own `bool` parameter to a test
  helper choosing the flag's stubbed value (`[InlineData(true, "new")]
  [InlineData(false, "old")] … Make(on)`) no longer keeps the gate. The rows
  for the value being removed are deleted, that column and the parameter go
  from the rest, and a theory with no row left is deleted. The pull request
  lists each one.

- **C#:** a test fake that serves several gates with one member
  (`class OnGate : ICheckoutGate, IWebhooksGate { public bool IsEnabled =>
  true; }`) no longer blocks removing each gate. When the member already
  returns the value the flag keeps serving, it is left as it is for the other
  gates, and the pull request lists it.

- **C#:** a class that takes your gate's interface through a constructor
  (`public Job(IGate gate)`, the usual dependency-injection shape) no longer
  stops every file that names the class from being cleared. Those files used
  to need a proof for each unrelated `IsEnabled` they read, such as one on an
  Entity Framework row, and when the proof failed the gate was left
  unremoved.

- **TSX and JavaScript:** a comment inside a folded `{...}` in JSX, as in
  `{/* why */ flag && <Panel />}`, is no longer dropped with the braces. Such
  a container is now left as it is.

- **C#:** a test that set a fake's knob to the value being removed and then
  handed the fake to code that sets it back (a helper method, a lambda) was
  deleted as a test of the removed branch, though it ran the kept one. When
  any test code in your repository writes the fake's knob outside a test
  body, a test is now decided by such a write or construction only if the
  fake stays within the test (written there, read, or passed to an object
  being constructed); otherwise the test and the wrapper are left for you.

## 1.16.0

### New

- **TypeScript and JavaScript:** a test about the branch being removed is now
  deleted even when it never sets the flag, as long as your flag registry
  declares that branch as the flag's default
  (`'my-flag': { default: false }`) and the test's title names the flag by a
  word of its key (`hides the GitHub link when the GitHub flag is off` for
  `web-social-login-github`). Such tests used to survive the removal and fail,
  since the code they asserted against was gone. Whenever the default was the
  removed branch, the pull request now also warns that tests leaving the flag
  unset run the kept branch from now on, and asks you to run the suite before
  merging.
- **TypeScript and JavaScript:** a removed constructor parameter now leaves
  its lists on one line too. When the removal deletes a constructor parameter
  the flag left unread, together with the argument each caller passed for it,
  and either list was written one element per line with one element left,
  `constructor(\n  private readonly log: Logger,\n) {}` comes out as
  `constructor(private readonly log: Logger) {}` and
  `new ReportService(\n  logger,\n)` as `new ReportService(logger)`. The
  conditions are the ones the helper parameters and their arguments already
  follow: two or more elements before the run, no comment inside the
  brackets, and a joined line that fits your Prettier `printWidth`, your
  `.editorconfig` `max_line_length`, or, with neither, the file's longest
  line.

## 1.15.0

### New

- **Dart:** a plain `import 'package:featureflip/featureflip.dart';` that a
  removal leaves unused is now removed, so `dart analyze` no longer fails on
  `unused_import`. Before, it was always kept, and a private field whose type
  was its last use (`final FeatureflipClient _client;`) was kept with it and
  named in the pull request. Now both go. What the Featureflip Dart SDK
  exports was read from every published version of the `featureflip`
  package, and none of them declares an extension, so nothing can rely on
  the import without naming something it exports. This applies only when the
  package's `pubspec.lock` resolves `featureflip` to one of those versions
  from pub.dev, and nothing overrides it. Without a lock, with a path, git or
  other-server source, or on a newer release, the import and the field stay
  as before.
- **TypeScript and JavaScript:** `@featureflip/edge`, the Cloudflare Workers
  SDK, is treated like the other Featureflip JavaScript SDKs. It does nothing
  when it loads, so an import of it that a removal leaves unused is deleted
  instead of being kept as a bare `import "@featureflip/edge";`.
- **TypeScript and JavaScript:** a test that stays because it tests the
  branch being kept no longer keeps a title describing a flag that is gone.
  When the title names the flag by its key in the kept state
  (`renders the summary when the my-flag flag is on and the cart has items`),
  the flag's condition is dropped from it
  (`renders the summary when the cart has items`). Only a fixed set of title
  shapes is rewritten, and only in files the run changed; any other title is
  reported as before. The pull request lists each rewrite, old title and new.
- **Every language:** the pull request now lists comments next to the code it
  changed that describe a flag's state without naming the flag, such as
  `// Skips polling while the flag is off.` above the line the fold
  rewrote. Before, only comments naming the key were reported. This is a guess
  from the wording ("the flag is on", "flag is disabled", "while the flag",
  "when the flag"), so these comments are listed and never edited.
- **TypeScript and JavaScript:** a parameter or argument list written one
  element per line is put back on one line when the removal leaves it with a
  single element. `function renderChecklist(\n  steps: number\n) {` now
  comes out as `function renderChecklist(steps: number) {`, so you no longer
  join it by hand in review. It applies to the helper parameters, their
  arguments, and the default-valued arguments this tool removes. A list is
  joined only when it had two or more elements before the run, holds no
  comment, and the joined line fits your Prettier `printWidth`, your
  `.editorconfig` `max_line_length`, or, with neither, the file's longest
  line. See README's known gaps for the details.
- **Every language:** a reported comment that describes the branch this pull
  request deleted ("the flag is off" when the flag serves `true`) is listed
  first, under its own heading, because it is now false rather than just out
  of date. That covers kept comments that name the flag too. A comment that
  hedges ("even when the flag is off") is not counted as one.
- **Ruby and ERB:** a keyless wrapper that is a Rails helper is now inlined
  where your views call it (`<% if new_checkout? %>`, `<%= new_checkout? %>`),
  and deleted once nothing else names it. It must be an instance method of a
  module in `app/helpers/` of a Rails application, named as Rails derives it
  from its file, and the template must be under that application's
  `app/views/`. Calls inside a block, a bare name without `?`, `!` or
  parentheses, and a statement tag whose value nothing uses are left alone and
  reported. Nothing in the repository may spell `include_all_helpers`,
  `clear_helpers` or `helpers_path`. Any other template naming a wrapper keeps
  it, as before (`other-language`). See README, "Wrapping the SDK".
- **Dart:** a private field set by a constructor is now removed when a caller
  passes it a variable or a Flutter widget's field. Before, that kept the
  field and its parameter and named them in the pull request. Two shapes are
  covered. The first is a variable declared in the caller's own file, either
  top-level or a `static` field of the enclosing class, whose initializer
  only builds a value (`final client = FeatureClient();` passed as
  `Svc(client)`). The same goes for a field built from one
  (`final _flags = Flags(client);`). Dart initializes such a variable when
  it is first read, and that initializer does nothing else. The second is
  `widget.client` inside a `State<CheckoutPage>` subclass, when `client` is
  a plain public field of the one `CheckoutPage` in the repository, typed
  with a class of the repository or of the Featureflip Dart SDK, and nothing
  in the repository declares a `client` or `widget` getter or a
  `noSuchMethod`, subclasses the state, or subclasses, implements or mocks
  the widget. Any other property
  (`this.widget.client`, `widget?.client`, `config.client`) is still kept,
  and so are a `State` with a `with` clause and a file that imports another
  package without a prefix or a `show` list: what it brings cannot be read,
  and a top-level `widget` it declared would be the one `widget.client`
  reads. See README, "Supported Dart/Flutter flag-read shapes".

### Fixed

- **Ruby:** a wrapper call whose argument is a name an earlier, already closed
  block bound (`items.each { |u| … }` then `Flags.on?(u)`) is no longer
  inlined. That name is a method call there, which inlining would drop
  (`argument-has-call`).
- **Dart:** a flag value held in a local and read in a string as `'$enabled\$'`
  (a template followed by an escaped dollar sign) is now inlined, giving
  `'${true}\$'`. The escape used to be read as part of the name, so that read
  was left behind and the whole flag was refused with "a string template still
  reads a variable the rewrite deleted".

## 1.14.0

### New

- **TypeScript and JavaScript:** a stranded import is no longer left behind as
  a bare `import "./m";` when the file's other imports already load everything
  in that module's graph that does work. A barrel that loads a store or a
  client the same file reaches anyway is the usual case. Only imports the
  compiler keeps count, and other files' imports never do. See README's
  caveats for the rules and the one ordering change it can make.
- **TypeScript and JavaScript:** a `paths` alias is now followed through a
  solution `tsconfig.json` (`references` plus `"files": []`, as in Vite's
  template) when the referenced configs that may compile the file agree. A
  stranded import of an inert module behind such an alias used to keep its
  bare import.
- **TypeScript and JavaScript:** an override left as `{}` is now dropped from
  the call it was passed to when it was the last argument and the function,
  declared at the top level of the same file and not generic, defaults that
  parameter to `{}`. `createWrapper(['/login'], {})` becomes
  `createWrapper(['/login'])`. The pull request lists each call it shortened.

### Fixed

- **TypeScript and JavaScript:** an override whose flag entry sat on a line of
  its own, leaving `{` and `}` on separate lines, was left empty without being
  listed in the pull request. It is now listed, or dropped as above.

## 1.13.0

### New

- **Every language:** a comment that names only the removed flag (or, in C#,
  only a type the run deleted) is removed, so the pull request no longer
  leaves `// Gated by the new-checkout flag.` behind for you to delete. A
  comment that also says anything else is kept and listed line by line, and
  the run's log reports it as a note rather than as a file that "still
  references" the flag. Licence headers, `TODO`s naming other work, doc
  comments as a whole, and text inside strings are never touched. See
  README's caveats for how "only" is decided.
- **Dart:** more unread private fields are deleted instead of being named in
  the pull request:
  - a field built by a constructor that only builds the object, a
    `FeatureClient();` or `FeatureClient() {}` with no initializer list in a
    class with no `extends` or `with` clause and no field initializer that
    may do work. That includes a keyless wrapper class
    (`final _flags = Flags();`) once its wrappers are deleted. Before, only a
    `const` constructor counted;
  - a field set by a constructor whose callers build the argument that way
    (`Svc(FeatureClient())`): the argument goes with the parameter;
  - a field that was the last use of an import: one of a library in the
    repository goes when nothing else it brings is used, and one of another
    package goes too when it has no prefix and one `show` list whose every
    name goes with the field
    (`import 'package:featureflip/featureflip.dart' show FeatureflipClient;`).
    A plain import of another package still keeps the field, since what it
    brings, extensions included, cannot be read.
- **Ruby:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a method of a class or module (an
  instance method, a `def self.` class method or a module function) whose
  whole body is one read of the flag
  (`def new_checkout? = @client.bool_variation("old-checkout", {}, false)`).
  A call is inlined only where its file proves it reaches that method: bare
  or through `self` inside the class's own methods, or through the class's
  name for a class method, with its value used and its arguments plain
  values. Because Ruby can define and call a method by name, a symbol or a
  string spelling the method anywhere keeps it, as do a test double, a class
  reopened in another file, `method_missing`, `prepend` and methods defined
  from computed names. A gem's instance methods are not inlined, and its
  public class methods are kept. Calls from ERB templates are not inlined
  yet; one keeps the wrapper. See README's "Wrapping the SDK".
- **Ruby:** Capistrano `.cap` tasks, Sorbet `.rbi` files and RBS `.rbs`
  signatures now count as Ruby files that can name a key constant or a
  wrapper, so a mention there keeps it.
- **Swift:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a function, or a method or
  computed property of a struct, enum or class, declared to return `Bool`,
  whose whole body is one read of the flag
  (`var newCheckout: Bool { client.boolVariation("old-checkout", default: false) }`).
  A call is inlined only where its file proves it reaches that declaration,
  in the wrapper's own module: a bare call of a function, the type's name or
  `Self` for a static member, and `self`, or a parameter, local or property
  written with the type as its type, for an instance member. A type with an
  inheritance clause, a subclassed or `open` class, and a member that is
  `open`, `class`, `override`, `dynamic` or has an attribute keep their
  wrappers, as do a key path, a `#selector` and a string naming the member. A
  `public` wrapper is kept unless its `Package.swift` declares no library
  product. See README's
  "Wrapping the SDK" for what is left alone, and why.

### Fixed

- **Dart:** a keyless wrapper called for nothing but its value, as the only
  statement of a braceless body (`if (c) flags.legacyCheckout;`, an `else`, a
  `for`, `while` or `do` loop), behind a label, or as the only statement of a
  `case`, is no longer deleted. Deleting it handed the body to the next
  statement, which then ran under the condition. The call is now kept and
  named in the pull request, and so is the wrapper.
- **Dart:** a private field whose type was named only in an import's `show`
  list is no longer deleted on the strength of that entry, which left the
  import unused (`unused_import`) when it came from another package.

## 1.12.0

### New

- **PHP:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a function, or a method of a
  class, declared to return `bool` (or with no return type), whose whole body
  is one read of the flag
  (`return $this->client->boolVariation('old-checkout', [], false);`). A call
  is inlined only where its file proves it reaches that declaration: a
  function name the file's namespace or a `use function` import resolves to
  it, the class's name or `self::` for a static method, and `$this`, a
  parameter typed as the class, or a `private` property typed as the class
  for an instance method. A class that is extended, abstract, has a parent,
  an interface, a trait or `__call`, or has a test double keeps its wrappers,
  as does a string naming the wrapper (a callable). A public wrapper of a
  Composer package that is not a `project` is kept. See README's "Wrapping
  the SDK" for what is left alone, and why.
- **Dart:** a private field left unread that a constructor sets is now
  deleted too, with what sets it: a `this._client` parameter, or an
  initializer-list entry (`: _client = client`) and the parameter it read
  when nothing else uses it. Every call of that constructor in the repository
  loses its argument, positional or named, through `new`, `const`, an import
  prefix, a redirecting `this(…)` or a subclass's `super(…)`. It happens only
  when every use of the class is accounted for: a private class in its own
  file, a public one in every Dart file of a package that is not published.
  A field whose initializer calls a `const` constructor with constant
  arguments (`final _client = FeatureClient();`) is deleted too. See README's
  Dart section for what is still kept and named in the pull request.

### Fixed

- **C#:** more gate families are removed whole instead of being kept by a
  proof that was too coarse:
  - a lookup over a local `Dictionary<K, Type>` of `typeof(…)` values no
    longer counts every string just because a file that does not parse uses
    a `Dictionary`; such a file may declare a type only under a name written
    right after `class`, `struct`, `interface`, `enum` or `record`;
  - in a file that compares names ignoring case, a string passed straight
    into another file's method (`Assert.True(Allowlist.IsAllowed(Kind.X,
    "IsEnabled"))`) no longer counts as naming the member;
  - a member lookup only tested for an attribute no longer keeps a gate
    whose wrapper file the run itself rewrote;
  - a constructor taking the gate interface is no longer kept by a
    target-typed `new(…)` whose type its context names
    (`static Resp Make() => new(…)`, `Resp r = new(…)`, an element of
    `new List<Resp> { new(…) }`) or that sits in a project that cannot see
    the class;
  - nor by `typeof` passed to your own attribute that keeps the `Type` in a
    get-only property only compared with `==` or `!=` (a test-coverage
    marker such as `[Covers(typeof(Job), …)]`).

## 1.11.0

### New

- **Python:** a function, method or `@property` whose whole body is one read
  of the flag (`return client.variation("new-checkout", ctx, False)`) is now
  inlined at every call its file proves is bound to it, and deleted once
  nothing names it. A function is proven when it is called bare in its own
  module, or through a top-level `from <its module> import name` or
  `import <its module> as m`; a method through `self` or `cls` inside its own
  class, or a static or class method through the class. The import of a
  deleted function is rewritten so the module still loads. A wrapper with a
  test double (`patch("app.flags.new_checkout")`, `MagicMock(new_checkout=...)`,
  `m.new_checkout.return_value = ...`) is left alone, and so is one exported
  through `__all__`, though its callers are still inlined. Set
  `inline-wrappers: false` to keep the old behaviour.
- **Dart:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a
  top-level function or getter, or a method or getter of a class, declared to
  return `bool`, whose whole body is one read of the flag
  (`bool get newCheckout => client.boolVariation('old-checkout', defaultValue: false);`).
  A call is inlined only where its file proves it reaches that declaration: a
  bare or prefixed call through an import of your package, the class's name
  for a static member, or a receiver every declaration of which in the file
  is written with the class as its type. A public wrapper of a package that
  may be published is kept. See README's "Wrapping the SDK" for what is left
  alone, and why.
- **Dart:** a private field whose every read was a flag read the run removed
  is deleted too, since `dart analyze` reports it as `unused_field` and exits
  non-zero. It goes when nothing else in its library sets it and its
  initializer cannot do work: absent, constant, or never run because the field
  is `late` or `static`. One that a constructor sets (`this._client` or an
  initializer list), one whose initializer may do work, one whose deletion
  would leave an import or a private declaration unused, an annotated field
  and a field of a library with `part` files are kept and named in the pull
  request instead.

### Behaviour changes

- **C#:** a gate method that builds its evaluation context before it reads
  the flag (`var ctx = new EvaluationContext { … }; if (orgId is { } o)
  ctx.Set(…); return _client.BoolVariation(…, ctx, false);`) is now inlined
  at its callers and removed like a one-line gate. Those statements may only
  build values that end up in the read's arguments.
- **C#:** a test fake set by a knob no longer has to default to the value the
  flag keeps serving, and may be a `private` class nested in a test class. A
  test that sets it to the removed value through a host's field
  (`factory.Gate.Enabled = false;`) is resolved like any other write, a test
  that hands it, never set, straight to what it builds is decided by its
  default, and a test that
  switches it to the removed value part-way through loses only the
  statements run that way. The pull request lists each such part.
- **C#:** once a gate family is complete, the run also removes a test host's
  field holding a fake (with the registration of it as the instance), a call
  of a test host's own registration helper, a middleware that only consulted
  the gate and now passes every request on (with its `UseMiddleware<…>()`
  registration and its tests), a test class the run's test edits left empty,
  and a comment standing alone above a deleted registration that names the
  flag or a deleted type.
- **C#:** a fallback's setting in a shared options class is removed even when
  tests build the options class into `new ConfigGate(Options.Create(new
  FeaturesOptions { … }))`, and when callers pass a configuration they build
  themselves or a top-level program's `builder.Configuration`.
- **C#:** a string such as `"*"` no longer keeps a gate as a possible prefix
  lookup unless your C# spells a lookup that takes a prefix (`GetMember`,
  `GetMembers`, `FindMembers`, `Type.FilterName`). Another language's source
  outside every C# project, such as a script's fixture, now counts as prose
  when it names a gate's type.

### Fixed

- A removal that deletes a whole file no longer fails the run after its
  branch is pushed. Switching the checkout back to your base branch was
  refused because the deleted files were back on disk but no longer tracked,
  which in `mode: remove` stopped every later flag in that run and in
  `mode: pr-command` reported a re-run that had updated the pull request as
  failed.

## 1.10.0

### Behaviour changes

- **TypeScript and JavaScript:** when a loop like `for (var i = 0; <flag>; i++)`
  is removed, the `var i = 0;` kept in its place is removed too if nothing
  else reads `i`. It used to be left behind.
- **TypeScript and JavaScript:** in a file that holds JSX, a named import the
  fold left unused is kept when another name in the same braces stays
  (`import { h, other } from "preact"`). With the classic JSX runtime every
  element calls its factory, such as Preact's `h`, without spelling it, so
  removing it broke the elements left behind. In a file that calls `eval`, no
  import is removed, since `eval` can read any of them by a string.
- **TypeScript and JavaScript:** in a file that holds JSX, the import of the
  JSX factory is kept even when it is the only name in its statement, such
  as `import React from "react"` or `import { h } from "preact"`. The factory
  is the `jsxFactory` or `jsxFragmentFactory` your `tsconfig.json` or
  `jsconfig.json` sets, or else `React` (and `h` and `Fragment` in a file that
  imports from `preact`), along with any name a `@jsx` or `@jsxFrag` comment
  gives. It used to be removed once the cleanup took out its last written
  use, which broke every JSX element left in the file.

## 1.9.0

### Behaviour changes

- **Java:** a local holding a literal that the fold left unread is removed
  inside a lambda, an anonymous class or a `static` initializer too. It used to
  be left there.
- **Kotlin:** a local holding a literal that the fold left unread is removed
  inside a lambda, an anonymous function, an object expression, an `init`
  block or a property accessor too. It used to be left there. A lambda's last
  statement still stays, since it is the lambda's value.
- **Kotlin:** an aliased import the fold left unused (`import a.b.C as D`) is
  removed when the file spells neither `D` nor `C`. It used to be kept in every
  case.
- **Java and Kotlin:** removing an import that sat alone between two blank
  lines also removes one of them, so the file is not left with two blank lines
  in a row.

## 1.8.0

### Behaviour changes

- **Python:** a local the fold left unread is kept, not deleted, when deleting
  it could change or break the function. That covers a function that spells any
  non-ASCII name (Python reads `ｒetries` as `retries`), a dunder such as
  `__class__`, a `~` on a number (`~1.5` raises), a container inside a set or
  used as a dict key (`{[1]}` raises), and a first statement followed by a
  parenthesised string (`("doc")` would become the docstring).
- **C#, Dart, Go, Java, JavaScript, Kotlin, PHP, Ruby, Swift and TypeScript:**
  a comment at the end of a removed statement's line is removed with it. It
  used to be left at the end of the line above (`{ // remove after rollout`)
  or after the member that stays, where it read as a note about kept code.
- **C#, Dart, Go, Java, JavaScript, Kotlin, PHP, Ruby, Swift and TypeScript:**
  code that followed a removed statement on its line keeps a line of its own.
  It used to be joined onto the line above, and could end up inside a comment
  there. In Kotlin and Swift, a removed statement followed on its line by a
  block comment and more code (`dead(); /* note */ next()`) is refused
  instead, because the line left behind does not parse.
- **Python:** removing the last `elif`, `else`, `except` or `finally` clause
  keeps the blank lines after it, so the two blank lines before the next
  top-level function stay. The same holds for a `finally` on its own line
  after a `catch` in TypeScript and JavaScript.
- **Python:** a literal local the fold left unread is removed even when a
  comment on its line mentions its name (`retries = 3  # retries left`).
- **Ruby:** more locals the fold left unread are removed: one written in a
  block inside a method, an only `x ||= <literal>` or `x &&= <literal>`, and a
  method's or block's last `x = value`, which becomes `value`, so what it
  returns is unchanged. Removing a last `elsif` or `else` also removes a blank
  line directly above it, which would have sat just before `end`.
- **Ruby:** a local is kept when deleting it could change or break the method:
  a `~` on anything but an integer (`~1.5` raises), a range whose ends do not
  compare (`1..'a'` raises), a range end under `!`, `not` or `defined?`
  (`!1..2` raises), and a local
  next to a range left open at the end of a line (`x = 1..`), which Ruby
  continues onto the next line.
- **PHP:** a local is kept when its function names `assert`,
  `mb_ereg_replace` or `mb_eregi_replace`, which on PHP 7 can run a string
  that reads it as code. An `Assert::` class counts too.

## 1.7.0

### Removes more

- **C#:** what a complete keyless wrapper with an interface leaves behind is
  removed too: the emptied interface and classes (and their files), their
  dependency-injection registrations, the constructor parameters, fields and
  arguments that passed the interface around, the test mocks and tests that
  only built them, the options class (or setting) an inert implementation read
  with its binding and `appsettings*.json` key, and the `using`s of a namespace
  this empties. Anything that cannot be proven keeps all of it, and the pull
  request says why; the flag is removed either way.
- **Go:** a label whose last `break`, `continue` or `goto` the fold removed is
  removed too. Go rejects an unused label, so the pull request did not build.
- **TypeScript and JavaScript:** an unread `private` or `#` field is removed,
  not just named in the pull request, when deleting it skips no work. The
  constructor parameter that fed it, and every caller's argument, go too when
  that can be proved.
- **TypeScript and JavaScript:** an unread constructor parameter is removed
  through a subclass that has no constructor of its own. When the removal
  leaves a subclass's plain constructor parameter unused
  (`constructor(c, l) { super(c, l); }`), that parameter and every caller's
  argument are removed too. A case that cannot be proved is named in the pull
  request, for `noUnusedParameters`.
- **C#:** removing an unread primary-constructor parameter now continues down
  a forwarding chain (`class Passing(IClient c, ILog l) : Svc(c, l)`), so no
  CS9113 is left behind. Callers that pass named arguments
  (`new Svc(client: c, log: l)`, `: base(…)`, `: this(…)`) no longer block the
  removal.
- **JavaScript:** a keyless wrapper with its own hand-written `.d.ts` is
  deleted together with its declarations, instead of being kept.
- **Java and Kotlin:** a keyless wrapper behind a public interface is inlined
  when its Gradle or Maven module provably publishes nothing. Anything the
  Action does not recognise counts as published, and the interface stays.
- **TypeScript:** a jest-mock-extended or ts-mockito class mock declared again
  in every test (`const m = mock<Flags>()`) no longer keeps its wrapper from
  being removed. The `let m; beforeEach(…)` shape still does.
- **TypeScript and JavaScript:** a stranded import of a Featureflip SDK
  (`@featureflip/js`, `/node`, `/browser`, `/react`, `/react/testing`) is
  deleted, instead of being left as a bare `import '@featureflip/react';`.
  This includes a `tsconfig.json` or `jsconfig.json` that extends a base config
  by relative path, which the Action reads the way the compiler does. The
  import is kept when the compiler could read the name as a file in your
  repository, or when a config cannot be read for certain. That covers a
  package `extends` such as a `@tsconfig/*` preset, a `baseUrl` that starts
  with `${configDir}` or is `null`, `paths` set to `null`, `moduleSuffixes`, a
  repeated key, and a file such as `@featureflip/react.ts` in the importing
  file's directory or one above it.
- **TypeScript and JavaScript:** a stranded CommonJS `require` of a module that
  does nothing on load is deleted (`import x = require(…)`,
  `const client = require(…)` and the destructured form), instead of being
  left as `require("./m");`.
- **TypeScript and JavaScript:** a `do { … } while (false)` left by a fold is
  unwrapped, and an emptied `static {}` block is deleted.
- **Kotlin:** dead `when` arms that share a line with another arm
  (`a -> 1; false -> 2`) fold. They were refused before.
- **C#:** a key constant read through its type from the same file
  (`Flags.NewCheckout` beside `public static class Flags`) is folded and its
  declaration removed. The run used to report no changes.
- **C#:** an interpolated string with an escaped brace right before a hole
  (`$"{{{Flags.On("k")}}}"`) is now analysed, so a flag read inside the hole
  folds. The file used to be refused. A key constant read only inside such a
  hole is now seen, so its declaration is kept.
- **Java:** the statement after a `try` whose body the fold left always
  exiting is removed and listed, unless a `catch` block can fall through, and
  so is a statement after a folded `yield`. Both are `unreachable statement`
  errors, and the pull request did not build.
- **Go:** two names declared together and both left unused by the fold
  (`x, y := 3, load()`, `var low, high = 1, 9`) are removed, the value kept as
  `_ = load()` when it may have a side effect. So is a local whose name a
  function literal's parameter, a same-named local in another block or a
  struct literal key also spells. These flags used to be refused.
- **Kotlin and Dart:** a short string template that reads an inlined local
  (`"$ok yes"`, `'$enabled'`) is rewritten as the braced literal,
  `"${true} yes"`. The flag used to be refused.
- **PHP:** an `if` without braces folds (`if (!<read>) return 1;`), and so does
  a `match`: an arm of `match (true)` with the read as its condition, a
  `match` left with only its `default`, and a `match` on the read itself.
  These used to be refused.
- **Dart:** a collection-`for` whose element is a collection-`if` on the read
  folds. Served false, the whole `for` goes when it loops over a `List` or
  `Set` parameter inside a literal whose element type is written out;
  anywhere else the flag is still refused.

### Refuses instead of rewriting

Each of these used to be rewritten in a way that could change what the code
does. The run now exits 1 and names the file. Fix it by hand, or add the key
to `ignore`.

- **Java and C#:** a pattern guard served true (`case String s when <read>`)
  ahead of any later case but `default` / `_`, once a later case with the very
  same pattern has been deleted (it could never run). Both compilers read
  `when true` as no guard, so a later case the pattern covers no longer
  compiled. In Java, a colon-form `case … when <read>:` is refused on both
  values, since `javac` rejects a constant guard there.
- **Go:** a local bound to the read is refused when the function reads an
  outer variable of the same name above the local's declaration. The inlining
  rewrote that read too. When the function spells the name for anything else
  (a `range` variable, an `if` initializer, a function literal's parameter, a
  later `:=`, an assignment, a struct literal key or `&name`), the local is not
  inlined and `name := true` stays with its reads. The inlining used to rewrite
  those too: most results did not build, and a `range` variable became
  `for _, true := range …`, which builds and runs the loop differently.
- **Go:** a flag whose removal leaves a local declared and no longer used,
  when neither cleanup can remove it: two names declared together when one is
  still read, three or more names, a `:=` that also redeclares a name, several
  names on one spec in a `var ( … )` group, or a local whose name is also a
  struct literal key of a type not declared in the file. The pull request used
  to open anyway, and Go rejected it with `declared and not used`. The output
  names the local and its function.
- **PHP:** a dangling `else` (`if ($a) if (<read>) { … } else { … }`, also
  behind a brace-less `foreach`, `for` or `while`). PHP gives the `else` to the
  inner `if`, and the fold used to hand it to the outer one, on both values.
- **PHP:** a `match` on the read whose arm lists a call before the literal
  (`audit(), true => …`). The fold used to pick the arm and drop the call.
- **TypeScript and JavaScript:** a dead branch that declares a `var`. The name
  is hoisted and may be read after the `if`, so the branch is kept, and the
  `if` left testing a literal is refused. Deleting the branch used to drop the
  declaration.
- **Kotlin:** an `if` used as an expression whose surviving branch holds more
  than one statement. Splicing it flat made the branch's first statement the
  value.
- **C# and TypeScript constructor-parameter removal** is refused when the
  class is also constructed in a way the Action cannot follow: a C# attribute
  (`[Foo(…)]`), an XML-doc `cref`, `Activator.CreateInstance` with the type
  named in a string, or a TypeScript computed construction
  (`new m['Svc'](…)`, `Reflect.construct`).
- **C#** constructor-parameter removal is refused when the class has another
  explicit constructor with parameters (which used to produce a duplicate
  constructor, CS0111), and for a `partial` derived type.
- **TypeScript and JavaScript** constructor-parameter removal is refused when
  an `.html` page mentions the class, because a `<script>` there may call it.
- **TypeScript and JavaScript:** a `do { … } while (false)` that cannot be
  unwrapped safely is refused. A comment outside its braces used to be dropped.
- **Ruby:** an `if` or `unless` used as a value folds only when the branch it
  keeps is one simple expression. Several statements, a call whose arguments
  have no parentheses, and a dead branch in an argument are refused. So is a
  subject-less `case` in the same position, a `when a, b`, and the only `when`
  of a `case` that keeps an `else`. `x = if <read>; a; b end` used to become
  `x = a` followed by a loose `b`.

### Behaviour changes

- **Every language but TypeScript, JavaScript and Python:** a call to the left
  of the read is kept. `track() || <read>` served true and `track() && <read>`
  served false used to fold to the bare literal, dropping the call and what it
  did; they now come out as `track() || true` and `track()`.
- **TypeScript and JavaScript:** a conditional callee folded to one side keeps
  the `this` the call had. `(<read> ? obj.m : other)()` becomes `(0, obj.m)()`
  or `other()`, never `obj.m()` (which binds `obj`) or `(other)()` (which
  TypeScript's CommonJS output binds to the module object).
- **Every language:** blank lines inside a multi-line string literal (a
  template literal, a raw or verbatim string, a text block, a heredoc) are left
  alone. Every rewritten file used to have them collapsed, changing the value.
- **Go:** a read bound beside another name (`a, on := load(), <read>`) keeps the
  other binding, `a := load()`. Both names used to be deleted, taking the call
  with them. What follows a folded `break`, `continue` or `goto`, a `return` in
  a `case` or `select` clause, or a `switch` whose clauses all return, is
  deleted, so `go vet` reports no unreachable code. A comment after the `}` the
  fold removes trails the spliced body instead of landing on a line of its own.
- **Ruby:** a local written more than once, read past the branch or block that
  writes it, or an instance variable keeps its declaration as `x = true`.
  Inlining it used to leave `nil`, a `NameError` or `true ||= x`. A write used
  as a value (`if (x = <read>)`, `x = <read> or y`) is replaced by its value
  rather than deleted, and a dead modifier `x if <read>` that was a method's
  last statement becomes `nil`, keeping what the method returns.
- **Ruby:** a local that nothing in its method reads is also cleaned up when
  the removed branch spelled its name as a method or a hash key (`retries = 3`
  beside a dead `g(retries: 1)` or `obj.retries`): a literal is deleted and a
  call kept as a statement, as for a local the fold stranded. It was already
  unused, and RuboCop flags it; the pull request names it with the rest.
- **Swift:** a `var` bound to the read that is later assigned, has a method
  called on it (`x.toggle()`) or is passed `inout` keeps its declaration.
  Inlining it used to write `true = …` or `true.toggle()`.
- **C#:** a local passed as an `out` or `ref` argument, or written by a
  deconstruction, keeps its declaration (`out true` did not compile). A local
  function declared after a folded `return` is kept, since the code that runs
  may call it.
- **Java and C#:** a later `switch` case with the same pattern as a guard
  served true is deleted. It could never run, and the flag used to be refused.
- **Kotlin:** a private property read as `this.name` folds (`this.true` used
  to be written). A surviving branch that is an unbraced loop body keeps its
  braces.
- **Dart:** a redundant `x = true;` that is an unbraced `if` body becomes `{ }`.
  Deleting it handed the body slot to the next statement.
- **PHP:** where a logical operator folds away in a value position, the operand
  it keeps is cast (`return (bool) $x;`), since `&&`, `||`, `and` and `or`
  always yield a bool.
- **PHP:** a `match` folded inside a larger expression keeps its parentheses:
  `match (true) { <read> => $a + 1, default => 0 } * 3` becomes
  `($a + 1) * 3`, never `$a + 1 * 3`. A `goto` label after a folded `return`
  is kept.
- **Go:** a comment at the end of a removed local's line goes with the local.
  Left behind, it ended up on the function's signature line.
- **Kotlin, Java and Go:** a function with the same name and parameters as the
  one reading the flag, in another class of the same file, is left alone. The
  inlining used to rewrite it as well, which in Kotlin could write
  `val true = …`.
- **Kotlin and Dart:** a local is inlined only when every read of its name lies
  between its declaration and the end of its block. A Kotlin `private`
  property read bare inside a lambda, an extension, an object expression or a
  nested class, or shadowed by a constructor parameter, keeps its declaration.
- **TypeScript and JavaScript:** a comment between `else` and its unbraced
  statement no longer swallows that statement, and an arrow body that folds to
  an object literal is parenthesised (`=> ({ … })`) rather than becoming a
  block.
- **ERB:** a deleted tag written with trim markers takes the newline and
  indentation they trimmed, so a `.text.erb` template renders exactly as before.
- **Every language:** a flag key is matched only against a string literal's
  whole value. A literal that merely starts or ends with the key, because an
  interpolation or an escape splits it (Python `f"old-checkout{env}"`, PHP
  `"old-checkout$env"`, Swift `"old-checkout\(env)"`, `"old-checkout\x2dv2"`),
  reads a different flag, and that read used to be folded and its key constant
  deleted. It is now left alone.
- **Archive modes:** only a removal merged into the base branch counts. A
  removal pull request retargeted to another branch and merged there used to
  archive the flag although the base branch still read it. The base branch is
  `base-branch`, or the repository's default branch when that is unset, so a
  cleanup workflow that sets `base-branch` needs the same value in its
  `archive-sweep` and `archive-on-deploy` workflows.
- **`archive-on-merge`:** a pull request from a fork never archives anything,
  whatever its branch is called. Under `pull_request_target`, a fork branch
  named like a removal branch could archive any flag once merged.
- **Archive modes and `remove`:** a GitHub answer to a pull-request lookup that
  is not a list (a proxy's HTML page, an error object sent with status 200) now
  fails the run instead of reading as "no pull request". Before, the sweep
  could drop a merged removal from its backlog, and `remove` could treat a
  branch as having no pull request.
- **C#:** a stranded `private` method is kept when deleting it would leave a
  field that only it assigned (CS0649) or only it read (CS0169, CS0414), each
  an error under `TreatWarningsAsErrors`.
- **C#:** a key constant declared in another file is kept, and the pull
  request says why, when a file reflects over its class as a whole
  (`typeof(FeatureKeys).GetFields()`, `Type.GetType("Shop.Flags.FeatureKeys")`,
  a subclass or an alias of it). Before, the constant was deleted once every
  read that named it was folded, and the reflection silently lost it.
- **C#:** the same holds when the reflection is in a Razor view, a XAML file,
  or a VB, F# or other .NET file the Action does not rewrite
  (`typeof(FeatureKeys)`, `GetType(FeatureKeys)`, `typeof<FeatureKeys>`,
  `{x:Type local:FeatureKeys}`). Reading another constant there
  (`FeatureKeys.Other`) still lets the removed one go.
- **Dart:** a key constant declared in another file is kept when any file
  imports or exports `dart:mirrors`. A mirror reaches a constant without
  naming it (`reflectClass(FeatureKeys).declarations`, or every loaded library
  through `currentMirrorSystem()`), so the constant used to be deleted while
  still reachable. Mirrors exist only on the Dart VM, so Flutter and AOT
  packages are unaffected.
- **Java and Kotlin:** a key constant declared in another file is kept, and
  the pull request says why, when any Java, Kotlin, Groovy or Scala file takes
  its owner whole: a class literal (`RuntimeKeys.class.getFields()`,
  `RuntimeKeys::class.java`), `Class.forName("app.RuntimeKeys")`, the same for
  a subclass, or a Kotlin object handed on as a value. A class handed to a
  test double or a logger (`mock(CheckoutGate.class)`, Spock's
  `Mock(CheckoutGate)`, `mockk`, `getLogger`) or named in Spring's `@Import`
  or a Spring test double's annotation (`@MockBean`, `@MockkBean`) does not
  count. Before, the
  constant was deleted once every read that named it was folded.
- **PHP:** a key constant declared in another file is kept when a heredoc, a
  nowdoc, a `b'...'` string or a string spelling the class through escapes
  (`"App\\Flags\\\x4Beys"`) names its class. Those strings were not read
  before, so the constant was deleted while `constant()` or reflection could
  still reach it.
- **PHP:** a namespace-level key constant (`const NEW_CHECKOUT = '...';`) is
  kept when its name can be built at run time: `constant('App\\Flags\\' . $n)`,
  `constant(__NAMESPACE__ . '\\' . $n)` inside that namespace, or
  `get_defined_constants()`, which lists every user constant. These used to
  let it be deleted.
- **PHP:** a key constant declared in another file is kept, and the pull
  request names the line, when a Twig, Latte, Smarty, Volt or Blade template
  builds a constant's name from its class
  (`constant('App\\Flags\\Keys::' ~ name)`,
  `constant(\App\Flags\Keys::class . '::' . $name)`). Before, the constant
  was deleted and the template's lookup failed at run time. A template that
  reads another constant by its whole name (`constant('App\\Flags\\Keys::ALL')`)
  does not keep the key constant.
- **PHP:** a class key constant is also kept when an instance of its class
  is created (`new Keys()`), received through a typed parameter, property or
  return (`Keys $k`, which a container autowires), or handed on as `$this`
  from a subclass. An instance reaches every constant
  (`new \ReflectionObject($k)`, `get_class($k)`), and where it goes is not
  followed. A service that declares its own flag constants and is constructed
  or injected now keeps them.
- **Ruby:** a key constant is kept when another file holding Ruby reads it,
  not only a `.rb`, `.rake` or `.erb` file. That covers views (`.jbuilder`,
  `.jb`, `.haml`, `.slim`, …), project files (`Rakefile`, `Gemfile`,
  `config.ru`, a `.gemspec`, …) and `bin/` scripts with a Ruby shebang.
  `Gate::FLAG_KEY` in a HAML view used to let the constant be deleted. HAML and
  Slim are not parsed, so one keeps the constant whenever it names it or its
  owner.
- **Ruby:** a flag read in one of those files is now listed in the pull
  request among the files that still reference the flag. The Action does not
  rewrite them, and it used to say nothing about them, so the pull request
  looked like a complete removal.
- **PHP, Java, Kotlin, C#, Python, Ruby and Dart:** a key constant is kept,
  and the pull request names the line, when the file declaring it takes its
  owner whole: `(new \ReflectionClass(self::class))->getConstants()` inside
  the class, `Keys.class.getFields()` inside `Keys`, `typeof(FeatureKeys)`
  inside `FeatureKeys`, `constants` or `const_get` in a Ruby module, a Python
  module importing and handing on itself, or `dart:mirrors` imported there.
  Before, only another file counted, so the constant was deleted and the
  owner's own listing silently lost it. The file is left as it is.
- **C# and Java:** naming a class for a logger or an equality contract no
  longer keeps its key constants, in any file: `ILogger<Owner>`,
  `Log.ForContext<Owner>()`, `LogManager.GetLogger(typeof(Owner))`,
  `IEquatable<Owner>`, `IComparable<Owner>`, and
  `Logger.getLogger(Owner.class.getName())`. None of them reads a field.
- **Ruby:** a dead `if`, `unless`, `case` or `while` that was the last
  statement of a method or block becomes `nil`, so the method returns what it
  did before. A dead loop there is listed in the pull request.
- **Ruby:** a callback condition you wrote yourself (`if: -> { true }`) and an
  empty RSpec hook (`before(:each) { }`) are left alone. Only ones a flag
  removal produced are cleaned up.
- **Java and Dart:** every statement a fold left unreachable after a `return`
  is listed in the pull request. The first one used to be missing.
- **Dart:** a local the fold left unused that held a number, a string, a
  boolean, `null`, a list, set or map of those, or no value at all, goes with
  the comment at the end of its line. The comment used to be left behind on a
  line of its own.
- **Every language but Go and ERB:** a blank line a removal leaves just inside
  a class, method or block, after its opening line or before its closing `}`
  or `end`, is removed. RuboCop's `Layout/EmptyLinesAround*Body` cops used to
  flag it. A blank line that was already there is kept.
- **Every language:** a file with Windows (CRLF) line endings throughout keeps
  exactly one `\r\n` per line wherever the removal deletes or folds code.

### Output

- **Go:** a comment on a removed import's line goes with it, and so does the
  comment heading a section the import was alone in. Left behind, either one
  made `gofmt -l` list the file.
- A `//` comment that trailed a removed argument or parameter goes with it,
  instead of moving onto the item before. A tab between items stays a tab.
- The text a fold moves is re-indented to where it lands, and the blank line a
  removed `}` or `end` left behind is gone (Go, Ruby, Swift).
- Deleting a file's first statement takes the line break after it, so the file
  no longer starts with a blank line.
- **ERB:** a multi-line tag the removal rewrote comes back as one tag. Split in
  two, it rendered an extra newline.

### Integrations

- **CircleCI:** the `featureflip/flag-cleanup` orb is published. The README has
  a "Running it on CircleCI" section. The orb offers `remove` and
  `archive-sweep`.
