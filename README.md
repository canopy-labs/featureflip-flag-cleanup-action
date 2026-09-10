# Featureflip Flag Cleanup Action

Deterministic dead/stale feature-flag removal, powered by the
`polyglot-piranha` AST transform engine. Runs inside the
customer's CI, fetches removal candidates from the Featureflip public API, and
opens one PR per flag.

Supported languages: **TypeScript** (`.ts`, `.mts`, `.cts`), **TSX** (`.tsx`),
**JavaScript** (`.js`, `.jsx`, `.mjs`, `.cjs`), **PHP** (`.php`, `.phtml`,
`.inc`, `.module`), **Ruby** (`.rb`, `.rake`), **ERB** (`.erb`), **Dart**
(`.dart`), **Java** (`.java`), **Go** (`.go`), **Python** (`.py`), **Kotlin**
(`.kt`, `.kts`), **C#** (`.cs`) and **Swift** (`.swift`).

> **Versioning.** `@v1` tracks the newest v1 release. It always resolves to a
> commit naming one exact image digest, so a run stays reproducible even though
> the tag itself moves. Pin a released `vX.Y.Z` instead if you would rather not
> move at all.

## Usage

Add a workflow to the repository you want cleaned up. The two permissions
below are not optional — see "Required permissions".

```yaml
name: Featureflip flag cleanup
on:
  schedule:
    - cron: '0 9 * * 1'   # every Monday at 09:00 UTC
  workflow_dispatch: {}

permissions:
  contents: write
  pull-requests: write

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: canopy-labs/featureflip-flag-cleanup-action@v1
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        with:
          api-token: ${{ secrets.FEATUREFLIP_API_TOKEN }}
          org: my-org
          project: my-project
          staleness: dead
          dry-run: true   # first run: see the diffs before anything is opened
```

`FEATUREFLIP_API_TOKEN` is **your** Featureflip API token (store it as a
repository or organization secret) — never a Featureflip-internal one, and
this Action never reads or contacts anything Featureflip-internal.

In this mode the Action only ever **reads** from that API, so give it the least
privilege that works: a `Viewer` token restricted to the one project is enough,
and is what these instructions are verified against. Nothing here archives,
toggles or deletes a flag — removing the code is proposed as a pull request for
you to merge, and what happens to the flag afterwards stays your decision.

### Archive the flag when the cleanup PR merges

Merging a removal pull request deletes the code, but the flag itself stays live
in Featureflip until somebody archives it by hand. `mode: archive-on-merge`
closes that loop. It is **opt-in and separate**, in its own workflow on its own
trigger:

```yaml
name: Featureflip archive on merge
on:
  pull_request:
    types: [closed]

jobs:
  archive:
    if: github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    steps:
      - uses: canopy-labs/featureflip-flag-cleanup-action@v1
        with:
          mode: archive-on-merge
          api-token: ${{ secrets.FEATUREFLIP_ARCHIVE_TOKEN }}
          org: my-org
          project: my-project
```

Three things to note.

**It needs a different token, and this is the whole reason the mode is
separate.** Archiving requires a token with the **Member** role, while fetching
removal candidates needs only read access. Give this workflow its own secret
rather than reusing the read-only one from the cleanup workflow. The difference
matters if a token ever leaks: a read token discloses flag names, a Member token
can change flag state.

**It needs no `actions/checkout` and no `permissions:` block.** The run reads
the pull request that triggered it and makes one API call. It touches no source,
opens no branch, and never calls the GitHub API, so it needs nothing from
`GITHUB_TOKEN`.

**It identifies the flag from the branch name**, which this Action encodes
reversibly when it opens the pull request. Nothing is parsed out of the pull
request title or body, so editing those is safe, and the mode works on removal
pull requests opened before you added this workflow.

The workflow above fires on every closed pull request in the repository. That is
expected: anything not opened by this Action is skipped with a one-line
`[not-a-removal-branch]` and exits 0. A pull request you closed **without**
merging is likewise `[not-merged]` and leaves the flag alone — declining a
removal never archives anything.

Archiving is idempotent, so a redelivered event or a re-run is harmless.

It can legitimately fail, and the message says which case you are in:

| What happened | What to do |
| --- | --- |
| Another live flag lists this one as a prerequisite (`FLAG_HAS_DEPENDENTS`) | Remove that prerequisite, then archive the flag yourself. The code is already merged. |
| A scheduled change still targets the flag (`FLAG_HAS_PENDING_SCHEDULES`) | Cancel the pending schedule, then archive. |
| `403` | The token cannot archive. Check this workflow is not reusing the read-only cleanup secret. |
| `404` | The flag no longer exists, or `org`/`project` do not name the project it lives in. |

### Inputs

Every input is bridged into the container as the environment variable named
beside it below. Each value is trimmed of surrounding whitespace; a set-but-empty
value falls back to the default shown rather than being read as empty; and a
comma-separated input is split on commas with each entry trimmed and blank
entries dropped.

| Input | Environment variable | Default | Notes |
| --- | --- | --- | --- |
| `api-token` | `FEATUREFLIP_API_TOKEN` | *(required)* | Your Featureflip API token. |
| `org` | `FEATUREFLIP_ORG` | *(required)* | Featureflip organization slug. |
| `project` | `FEATUREFLIP_PROJECT` | *(required)* | Featureflip project slug. |
| `mode` | `FEATUREFLIP_MODE` | `remove` | `remove` opens removal pull requests. `archive-on-merge` archives the flag whose removal PR just merged and ignores every input below except `api-url` — see [Archive the flag when the cleanup PR merges](#archive-the-flag-when-the-cleanup-pr-merges). |
| `api-url` | `FEATUREFLIP_API_URL` | `https://api.featureflip.io` | Override only for a staging/self-hosted instance. |
| `staleness` | `FEATUREFLIP_STALENESS` | `dead` | `dead` (ready-for-review PRs) or `stale` (draft PRs). |
| `languages` | `FEATUREFLIP_LANGUAGES` | every language this Action supports (`ts`, `tsx`, `js`, `php`, `ruby`, `erb`, `dart`, `java`, `go`, `python`, `kt`, `csharp`, `swift`) | Comma-separated. |
| `directories` | `FEATUREFLIP_DIRECTORIES` | `.` | Comma-separated, relative to the checkout. |
| `accessors` | `FEATUREFLIP_ACCESSORS` | *(none)* | Comma-separated function names your own code uses to read a flag, on top of the SDK's. See [Wrapping the SDK](#wrapping-the-sdk). |
| `ignore` | `FEATUREFLIP_IGNORE` | *(none)* | Comma-separated flag keys to always skip. |
| `base-branch` | `FEATUREFLIP_BASE_BRANCH` | the repository's default branch | Base branch each removal PR targets. Unset means "look it up" — see below. |
| `pr-labels` | `FEATUREFLIP_PR_LABELS` | *(none)* | Comma-separated. A label that fails to apply is logged as a warning and does not fail the PR. **May create labels** — see below. |
| `max-prs` | `FEATUREFLIP_MAX_PRS` | `10` | Most pull requests one run may propose (`0` = no limit). See "How much one run can do" below. |
| `dry-run` | `FEATUREFLIP_DRY_RUN` | `false` | See "Dry run" below. |

`GITHUB_TOKEN` is **not** a declared input — GitHub does not expose it to a
container as a default environment variable, so it must be passed through the
workflow step's own `env:`, exactly as in the example above. `GITHUB_REPOSITORY`,
`GITHUB_API_URL`, and `GITHUB_SERVER_URL` (the last two matter only on GHES)
*are* default environment variables the runner already injects into the
container, so they need no wiring.

### Which branch PRs target, and which branch you must run from

Leave `base-branch` unset and the Action asks the repository for its own
default branch, once per run. It does **not** assume `main`: in a `master`
repository that assumption made GitHub reject every pull request, so the run
pushed one branch per flag and opened nothing. Set `base-branch` explicitly to
target something else (a release branch, say) and no lookup happens.

Each removal branch is cut from **the commit you checked out**, because that is
what the diff was computed against. The Action therefore refuses to start
(exit `2`) if that commit carries anything the base branch does not — which is
the case on a `pull_request` trigger, where `actions/checkout` gives you a
merge commit, and where every removal PR would otherwise also contain that
pull request's commits. Run this from `on: schedule` or `on: push` against the
base branch. (A checkout that is merely *behind* the base is fine: it
introduces nothing new.)

### `pr-labels` can create labels

The values are sent to GitHub's add-labels API. A label that does not already
exist in your repository may be **created** by that call (with a default
colour) rather than rejected. If you want to control label colours and
descriptions, create them before setting this input.

### How much one run can do

`max-prs` caps how many pull requests a single run proposes (default `10`).
Everything else in this Action is an argument about whether one pull request is
*right*; this is the only thing bounding how many of them arrive if something is
wrong. A project with 200 dead flags would otherwise get 200 branches and 200
pull requests on its first run, and if that run was mistaken, all 200 were.

Nothing is skipped permanently: the run stops cleanly, says so, and the next
run continues where it left off, because flags that already have a branch or a
pull request are not proposed again. Candidates that are ignored, already
handled, or have nothing to change do not count against the limit — only
proposals do.

The limit applies to `dry-run` as well, so the preview is of the run that would
actually happen rather than of one that never will. Set `max-prs: 0` for no
limit once you have seen a few rounds of diffs and trust them.

### Dry run

`dry-run` defaults to `false` — if you omit it, the Action opens real
branches, commits, and pull requests on its first run. There is no friendlier
default hiding in the code. Set `dry-run: true` explicitly (as the quick-start
above does) to only compute and print each candidate's diff; in that mode
GitHub is never contacted at all, not even to check whether a PR already
exists for a flag.

Every refusal a real run can produce is reachable in a dry run and is reported
identically — `[unsafe-key]`, `[piranha-error]` and `[unsafe-rewrite]` all
appear with the same label and the same exit code. That is what makes the dry
run worth reading: it cannot approve a flag that the next real run turns down.
Only the outcomes that need GitHub (`[pr-opened]`, `[already-handled]`) are
unreachable.

### What a run prints

Every candidate gets a line on stdout — `[pr-opened]`, `[no-changes]`,
`[unsafe-rewrite]` and so on — along with any caveat that changes what the
result means. A completed run then closes with a one-line tally:

```
flag-cleanup: 3 candidates: 2 pr-opened, 1 no-changes
```

A run that found nothing says so, rather than printing nothing:

```
flag-cleanup: 0 removal candidates (staleness=dead), nothing to do
```

That distinction is the point. "There was nothing to clean up" and "this job
did not work" otherwise look identical in a job log, and the staleness tier is
named because it is what tells them apart: an empty `dead` sweep is expected on
a young project, and suspicious on one you know has retired flags.

The summary is also the marker of a finished run. A run that aborted part-way
reports the candidates it got through and then explains itself on stderr, so no
tally means the run did not reach the end.

### Exit codes

- `0` — every candidate the run attempted succeeded (this includes
  "no changes", "already handled" and "ignored" outcomes, not just opened PRs).
  Nothing to do is success; **declining** to do something is not — see below.
  `[already-handled]` also covers two runs overlapping (the schedule plus a
  manual dispatch, or a job re-run): the second one finds that a pull request
  for the flag now exists and reports it as handled, naming that PR. It never
  deletes the branch in that case, because deleting a pull request's head
  branch closes it.
- `1` — at least one flag **could not be proposed**, and will not be on any
  future run either until something changes. The run still attempted every
  other flag, so a red build can still have opened PRs — check the per-flag
  lines. The stderr summary names each flag with the outcome that produced it,
  because the remedies differ:
  - `[failed]` — a push was rejected, the GitHub API refused, and so on.
    Usually retryable. One cause worth knowing because it repeats forever until
    you act on it: **a flag whose only reads live in untracked or gitignored
    files.** The transform rewrites any file whose text holds the key, but only
    tracked files can be committed, so there is a real diff that can never
    become a PR. The failure names the files; either track them or add the flag
    key to `ignore`.
  - `[unsafe-key]` — the key cannot be turned into a git branch name (blank, or
    too long once escaped), or holds characters the rewrite rules cannot search
    for safely (anything outside `A-Z a-z 0-9 - _`; see "Flag keys" below).
    Rename the flag, or add the key to `ignore`.
  - `[piranha-error]` — the transform engine aborted on its own syntax
    self-check. Nothing was written. Please report it.
  - `[unsafe-rewrite]` — the rewrite of one or more files did not survive the
    post-transform syntax check, so it was rolled back **along with every other
    file for that flag** (a half-removed flag is worse than an unremoved one).
    The output names the refused files. Until they are changed by hand — or the
    key is added to `ignore` — this flag produces nothing on every run. It is
    reported rather than passed off as "no changes" precisely so it does not
    look like a repository with nothing to clean up.

  Exit `1` also covers a run that aborted part-way, including one that could not
  read the repository's default branch when no `base-branch` was given: that
  lookup happens over the network once the run is already under way, so unlike
  the exit-2 causes below it cannot promise nothing was modified.
- `2` — the run could not start at all, and **nothing was modified**. Causes:
  - a required `FEATUREFLIP_*` variable, or the runner-provided
    `GITHUB_TOKEN`/`GITHUB_REPOSITORY`, was missing;
  - `languages` names something unsupported (see the supported list at the top
    of this file) —
    reported as a config error rather than a traceback from deep inside the
    transform engine;
  - `api-url` is not an absolute `http(s)` URL, or its port is not a number in
    `0-65535`. `api.featureflip.io` without a scheme is the common case; both
    are refused here rather than failing on the first request, where they would
    arrive as an HTTP-client traceback;
  - a configured directory is not inside a git work tree, or doesn't exist;
  - the checkout carries commits the base branch does not (see "Which branch
    PRs target" above) — typically a `pull_request` trigger's merge commit;
  - `dry-run` is set to something that is not clearly a boolean. A typo is
    refused rather than read as `false`, since guessing wrong would open real
    pull requests;
  - **a configured directory has uncommitted changes to tracked files.** The
    run undoes its own edits between flags to keep each PR to a single flag,
    and that undo cannot distinguish your work from its own — so it refuses
    rather than risk discarding it. Untracked files are deliberately allowed
    (build output shouldn't block a run). In CI the checkout is clean and this
    never fires; it exists for local invocations.

### Flag keys

Keys are processed verbatim when they consist of `A-Z`, `a-z`, `0-9`, `-` and
`_` — which covers every key Featureflip itself will create. Anything else is
**refused per flag** (`[unsafe-key]`, exit `1`), not guessed at: the key is
substituted into the tree-sitter queries that drive the rewrite, where it is
compiled to a regular expression. A `.` or `*` would silently mean something
other than the literal text you wrote, `$` and `+` match nothing at all, `)`
and `[` are rejected by the engine, and a `"` would end the query's string and
let the rest of the key be read as rule syntax. Refusing loudly is the only
option that can't quietly rewrite the wrong code.

### Required permissions

```yaml
permissions:
  contents: write        # push the removal branch
  pull-requests: write   # open the PR
```

Without both, the Action stops at the first flag with one clear message rather
than repeating the same refusal per candidate, and exits `1`. The same applies
to a repository with **"Allow GitHub Actions to create and approve pull
requests"** disabled (Settings → Actions → General), which 403s on PR creation
regardless of token scope.

Nothing is left behind when that happens: if a branch was pushed but its pull
request could not be opened, the Action deletes the branch again before
reporting the failure. That matters because a branch on the remote with no PR
is indistinguishable from "already proposed" — leaving one would retire the
flag silently and permanently. In the rare case the delete *also* fails, the
log names the exact branch to remove by hand.

### Your checkout is left exactly as it was found

Every flag is rewritten, diffed and then undone before the next one starts, and
the undo is verified byte-for-byte rather than assumed — a file that cannot be
proved back to its original contents stops the run instead of letting one
flag's edits reach another flag's pull request.

**File ownership is part of that.** This is a container action, so it runs as
`root` while your checkout belongs to the runner user, and `git` replaces files
rather than editing them in place — which would otherwise hand back source
files, and parts of `.git`, that your own later steps cannot write. The
original owner is restored for everything touched, so a step after this one can
still write the files and commit. If any of it cannot be restored, the log says
which paths and the run continues; the contents are unaffected either way.

The restore is re-asserted **once at the end of the run**, not only after each
flag, because a later flag's whole-worktree `git checkout` rewrites files an
earlier flag had already handed back. It restores from per-file owners recorded
before the run touched anything, so it can only ever put back an owner it saw,
on a path it captured — a file this Action never wrote is never reassigned.

## Wrapping the SDK

Most codebases do not call the SDK where they read a flag. They wrap it:

```ts
// src/flags.ts
export function useFlag<K extends FlagKey>(key: K): boolean {
  return useFeatureFlag(key, FLAGS[key].default);
}

// src/Checkout.tsx
if (useFlag('old-checkout')) { … }
```

Out of the box this Action does not see `useFlag`. It matches the SDK's own
names, so every call site is invisible, every flag reports no changes, the run
goes green, and nothing tells you why. Name your wrapper and it is matched
exactly as an SDK call is:

```yaml
with:
  accessors: useFlag
```

Comma-separate more than one. Each name must be a plain function name
(`[A-Za-z_][A-Za-z0-9_]*`) — anything else fails the run immediately, before
anything is modified, because these names are substituted into the rewrite
rules.

**What this widens, and what it does not.** It widens which *callee* counts as
a flag read. It does not change how the *key* is matched: the key must be a
**string literal** in the call's first argument. So `useFlag('old-checkout')`
is matched, but `useFlag(FLAGS.oldCheckout)` is not, because nothing can prove
which flag a property lookup names.

**A key hoisted to a constant is resolved for a wrapper too**, in every
language — `useFlag(OLD_CHECKOUT, …)` is rewritten exactly as
`client.boolVariation(OLD_CHECKOUT, …)` is, and the declaration goes with the
last read of it. See [The key hoisted to a
constant](#the-key-hoisted-to-a-constant) for what counts as one; the same
conditions apply, including that every *other* reference to the name must be a
read this tool removes.

That also bounds the blast radius. Declaring `useFlag` does not make every
call a flag read: a call is rewritten only when its name is one you declared
**and** its first argument is the exact key being removed. A `useFlag` in
someone else's library, or a `trackEvent('old-checkout')` alongside it, is
untouched.

**A wrapper that takes the key indirectly cannot be reached this way.** If your
wrapper is called as `useFlag(FLAGS.oldCheckout)` or `flags.oldCheckout()`, the
key is not at the call site and no name will make it matchable. Those reads are
reported as unremoved rather than guessed at.

**The wrapper's own registry is cleaned up too.** A typed wrapper usually
comes with a registry (`FLAGS` above), and tests that stub flags by name.
Neither is a flag read, so no read rule touches them — instead a separate set
of rules deletes an **entry keyed by the flag** when one of two things is
true:

* another key in the same object is a different flag of your project (the
  object is a flag registry) — the run fetches your project's flag list, live
  and archived, to decide this; or
* the entry's value is a boolean (`{ 'old-checkout': true }` in a test
  override).

That covers the registry line, a test's override property, and a type alias or
inline object type beside it (`type Overrides = { 'old-checkout'?: boolean }`)
— in every supported language except Java and Swift, whose map entries wait on
an engine change, and the pull request lists each one under "flag-keyed
entries were removed". An `interface` body is **not** covered yet: the members
look identical, but they are a different node to the rules, so an
`interface Overrides { 'old-checkout'?: boolean }` member is left for you and
listed under "still reference this flag".
One TypeScript shape is only half covered for now: in a `;`-separated object
type the rules delete the flag's member when it is the first one (or when the
type uses commas); a middle or last `;`-terminated member is left for you and
listed under "still reference this flag", because the engine cannot yet delete
the member and its `;` in one edit.
What it does **not** do is rewrite a test whose *assertion* was the gate:
`it('hides the link when the flag is off')` mentions no key, fails because the
gate is gone, and is yours to delete. Nor does it touch statements that write a
map (`flags.put("old-checkout", true)`).

**A comment is not moved with the entry it annotated.** Deleting an entry
deletes the entry, not the line, so a comment that shared that line stays
exactly where it lands — which is the end of the line above, attached to the
entry *before* the one that went:

```ts
export type Overrides = {
  'old-checkout'?: boolean; // gone soon
  'legacy-banner'?: boolean;
};
```

becomes `export type Overrides = {// gone soon` with the surviving member
below it. Nothing is lost, but the comment now annotates the wrong thing;
delete or move it in review.

## The key hoisted to a constant

Reading a key more than once usually means hoisting it, and every language here
spells that differently:

```go
const oldCheckout = "old-checkout"
...
if client.BoolVariation(oldCheckout, ctx, false) { ... }
```

The seed rules match a string literal, so a name in that position is not a weak
match — it is no match at all, and before this was supported such a file
reported "no changes" with nothing saying why. The declaration is now resolved,
the reads are folded, and the declaration is removed along with them.

What counts as a resolvable constant, per language:

| Language | Shape |
| --- | --- |
| JavaScript / TypeScript / JSX / TSX | `const K = 'key'`, including `export const`. A `let` or `var` is **not** — it may be reassigned |
| Java | `static final String K = "key"`, a `final` local, or an interface field (implicitly `final`). A non-`final` field is **not** |
| Go | `const k = "key"`. A `var` is **not**, and neither is a grouped `const ( … )` block declaring more than one name |
| C# | `const string K = "key"`. `static readonly` is **not** a compile-time constant — it may be assigned in a static constructor |
| Kotlin | `const val K = "key"`. A plain `val` or `var` is **not** |
| Dart | `const` or `final`. A `var` or a bare-typed declaration is **not** |
| PHP | `const K = 'key'`, at class, trait or file level, read as `self::K`, `Cls::K`, `static::K` or bare. A visibility modifier, `final`, a PHP 8.3 type and an attribute are all resolved (`#[A] final public const string K = 'key'`). A `static $k` property is **not** a constant, and `define()` is left alone |
| Python | a module-level `K = "key"` |
| Ruby | `K = 'key'`, with or without a trailing `.freeze` |
| Swift | `let K = "key"`, at file scope, in a type body (`static let`) or local to a function. A `var` is **not** — it may be reassigned |

Four conditions apply everywhere, and all of them fail closed to "no change at
all" rather than to a partial rewrite:

* the declaration must bind **one name** — `const string A = "k", B = "j";` and
  its equivalents are skipped, so removing it can never take something else
  with it;
* the initialiser must be a **plain string literal** — not interpolated
  (`$"…"`, `"…${x}"`, an f-string), not concatenated, not computed by a call;
* the name must be bound **exactly once** in the file. A narrower scope that
  rebinds it to a different key would otherwise have *that* flag's read folded
  to *this* flag's value, which compiles and is wrong;
* every other reference to the name must itself be a flag read this tool
  removes. If anything else reads it — a log line, a map entry, a wrapper you
  have **not** named in `accessors` — nothing in the file is touched, because
  removing the declaration would leave code that does not resolve. A wrapper you
  *have* named counts as a read and does not withhold the file.

**Python and Ruby rest on a weaker guarantee than the rest**, and it is worth
knowing which: neither language has a real constant, so the third condition is
doing all the work. A rebinding elsewhere in the same file is seen and refuses
the file; one in *another* file is not visible to this tool. If you hoist keys
in either language, prefer a module that only declares them.

## Supported JS/TS flag-read shapes

`ts` and `tsx` share one rule set, which removes:

| Shape | SDK |
| --- | --- |
| `client.boolVariation("KEY", ctx, default)` | js, node |
| `client.boolVariation("KEY", default)` | browser |
| `useFeatureFlag("KEY", default)` | react |
| `await client.getBooleanValue("KEY", default, ctx)` | OpenFeature, async client (`@featureflip/openfeature-node`) |
| `client.getBooleanValue("KEY", default)` | OpenFeature, sync client (`@openfeature/web-sdk`) — see below |

…in `if`/`else` (with or without an `else`, including `else if` chains), `!`
negation, ternaries, `&&`/`||` with the flag read on the left, and via a
`const` bound to the read (the binding is inlined and the declaration removed —
except where the declaration is *exported*, see the table below).

**JSX gates are unwrapped, not just folded.** In `.tsx`, `.jsx` and `.js`, a
gate in child position leaves an expression container behind once its condition
is gone — `{flag && (<Panel />)}` would fold to `{(<Panel />)}` and
`{flag && <Panel />}` to `{<Panel />}`, both of which read as a half-finished
rewrite. The container goes too, and the surviving element is re-indented to the
column the gate occupied. Served the other way the whole child is deleted rather
than left as `{false}`.

Attribute values are untouched, and deliberately so: the grammar gives
`disabled={flag}` and `{flag && <Panel />}` the same node, but there the literal
is the value. `disabled={flag}` becomes `disabled={true}` or `disabled={false}`
and stops there.

The two OpenFeature shapes are the same method name, and which one you have is
decided by **what the file imports**, because nothing at the call site says.
`@openfeature/server-sdk`'s `getBooleanValue` returns a `Promise<boolean>`, so
there the read is matched only when it is awaited, and the `await` goes with it:
substituting a literal for an un-awaited call would break a `.then(…)` chain,
and a bare read used as a condition is already always-truthy in your own code —
a bug this tool will not quietly convert into the other branch.
`@openfeature/web-sdk`'s returns a plain boolean, so the bare call is rewritten
there — but only in a file that imports `@openfeature/web-sdk` **and mentions no
other `@openfeature/…` package** (`@openfeature/core`, which ships types rather
than a client, excepted). A file importing both clients is left alone entirely:
there the imports no longer say which client any one read belongs to. See
[Known gaps](#known-gaps) for the reads this cannot reach.

A `const` bound to either read has its read replaced but is not inlined
(`const enabled = true;` with the guard below it left standing); that is the
same conservative outcome as any other binding this tool declines to inline, and
it is correct, just not fully folded.

When a fold makes a following statement unreachable — collapsing an `else if`
into an `else`, turning `if (<read>) { return a; }` into a bare `return a;`, or
turning a loop condition into `while (true)` — that statement is **deleted** and
listed in the pull request body. `no-unreachable` is part of
`eslint:recommended` and is an error, so leaving it would open a pull request
that cannot go green. A `break` that exits the loop keeps what follows
reachable, and nothing after it is touched. A loop the fold makes *dead* rather
than infinite (`while (false) { … }`, from a flag serving false) is left
standing: ESLint's recommended set says nothing about it, so removing it would
be an edit you did not ask for.

A local variable left with no remaining use after a fold is cleaned up the same
way an import is — except one level inside a `const Component = () => { ... }`
function. An arrow function has no name of its own for the grammar to expose
(only the constant it happens to be assigned to does), and the check that
scopes a variable to its one enclosing function needs a name to match the code
from before the fold against the code after — so a local declared inside one is
always left standing rather than guessed at. In a codebase built out of
arrow-function components, that is most functions.

## Supported Python flag-read shapes

The `python` rule set removes:

| Shape | Note |
| --- | --- |
| `client.variation("KEY", ctx, False)` | the boolean literal must be the **third positional** argument |
| `client.variation("KEY", ctx, default=True)` | keyword form |
| `client.get_boolean_value("KEY", default, ctx)` | OpenFeature (`featureflip-openfeature-provider`); key must be the first positional argument |

…in `if`/`else`, `not`, `a if <read> else b`, and `and`/`or` with the read on
the left. The `default` argument must be a boolean literal: it is the only
evidence the call site carries that this is a boolean flag, because the Python
SDK has no boolean-specific read method.

That requirement applies to the SDK's own `variation` only. A wrapper you name
in [`accessors`](#wrapping-the-sdk) is matched on the key alone, at any arity —
`useFlag("KEY")`, `useFlag("KEY", False)` and `useFlag("KEY", ctx, False)` all
work — because naming it is itself the statement that it reads a boolean.

`elif <read>:` is handled in both directions: serving `false` deletes the dead
clause, serving `true` promotes it to `else:` and drops every clause after it.

A branch of any size is folded, re-indented to the `if`'s own column — as in
every other language here, though in Python it is the difference between a file
that runs and one that does not. The indentation of a multi-line string is
never changed — those lines are data.

## Supported C# flag-read shapes

`client.BoolVariation("KEY", context, default)` — as a member call on any
receiver (the shape a DI-injected client produces), or as a bare
`BoolVariation(…)` where a project aliases the SDK call — in
`if`/`else if`/`else`, `!` negation, `&&`/`||` expressions, ternaries and
switch-expression arms, and via a local `bool useNew = client.BoolVariation(…);`,
which is inlined and its declaration removed. A local that is later
**reassigned** is left standing, and `VariationDetail<T>(…)` — a different name
under exact comparison — is never matched.

The key may be a **`const string`** rather than a literal at the call site —

```csharp
private const string FlagKey = "old-checkout";
...
_client.BoolVariation(FlagKey, context, defaultValue: false);
```

— which is the common shape once a key is read more than once. The declaration
is resolved and then removed along with the reads. This works in every language
now; the conditions, and what counts as a constant in each, are in [The key
hoisted to a constant](#the-key-hoisted-to-a-constant). For C# specifically the
constant must be `const` — `static readonly` may be assigned in a static
constructor, so its value cannot be read off the declaration — and an un-awaited
`GetBooleanValueAsync` reading the const counts as an other reference, which
withholds the whole file.

The OpenFeature .NET provider (`Featureflip.OpenFeature`) is matched as
`await client.GetBooleanValueAsync("KEY", default, context)`, and — as in
JS/TS — **only when awaited**, with the `await` removed alongside the call.
Here that is a build requirement rather than a preference: an un-awaited
`GetBooleanValueAsync(…)` is a `Task<bool>`, and leaving an `await` in front of
a substituted `true` would not compile, because `bool` has no `GetAwaiter`.

A read that is **a whole statement** rather than a value — a warm-up call made
for the SDK's own exposure event, whose result is discarded — has the whole
statement deleted, as in Java and Go. Both read shapes are covered, including
`await client.GetBooleanValueAsync(…);`. The statement must contain **no other
call**, so a computed, allocated or mutating argument is refused instead. An
UN-awaited `client.GetBooleanValueAsync("KEY", …);` on its own line is left
alone: that is a `Task` nobody waited on, which may well be deliberate. Both
read shapes work with the key hoisted to a `const string` too, whether it is
declared on the class or local to the method.

Removing a method's only `await` this way does not warn: an `async Task` method
left with no `await` compiles with zero warnings on net8.0, net9.0 and net10.0,
verified with `TreatWarningsAsErrors` enabled.

A local variable left with no remaining use after a fold is cleaned up the same
way as in Go, Java and TypeScript: its declaration is deleted when the
initializer cannot have a side effect, or rewritten to discard the value when
it can, so whatever it called still runs. A stranded `using` directive is not,
and cannot be: `using System;` introduces no name that appears anywhere in the
file — C# code writes `Console.WriteLine`, never `System.Console.WriteLine` —
so there is no name for this tool to check references against. Only
`using Alias = Foo.Bar;`, which does bind a name, can be judged this way, and
is removed like an import everywhere else.

## Supported Dart/Flutter flag-read shapes

`client.boolVariation('KEY', defaultValue: false)` and
`client.flagProvider.boolVariation('KEY', defaultValue: false)` — the two
receivers the SDK documents — plus a bare `boolVariation(…)` where a project
aliases the call. Both quote styles are matched, and the call may be split
across lines the way `dart format` writes a long one.

They fold in `if`/`else if`/`else`, `!` negation, `&&`/`||` expressions, a
conditional expression, and — the Flutter one — a **collection-`if` inside a
widget tree's `children:` list**, including its `else` arm:

```dart
children: [
  const Header(),
  if (client.flagProvider.boolVariation('KEY', defaultValue: false))
    const NewBanner()
  else
    const OldBanner(),
]
```

A local bound to the read is inlined and its declaration removed; one that is
later **reassigned** is left standing. `stringVariation` / `numberVariation` /
`jsonVariation` are never matched — the method name is the only type signal
Dart's untyped call site carries, and it is compared exactly.

Two shapes are deliberately left alone: **null-aware access**
(`client?.boolVariation(…)` is `null`, not a bool, when the receiver is null)
and an **interpolated key** (`'KEY$suffix'`, whose runtime value is not the
key).

When folding an `if`/`else` makes a following statement unreachable, that
statement is **deleted** and listed in the pull request body. Dart is one of
the languages where this happens (the others are Java, JavaScript and
TypeScript): `dart analyze` reports dead code as a warning but **exits
non-zero** on it, so leaving it would open a pull request that cannot go
green.

A local whose name is one of Dart's **built-in identifiers** — `on`, `show`,
`hide`, `late`, `required`, `covariant`, `get`, `set`, `factory`, `operator`,
`typedef`, `part`, `base`, `interface`, `sealed`, `when`, `external` — used to
fold to `final on = true;` without being inlined, leaving the `if` that read it
standing. That residue is gone: all seventeen now inline exactly like an
ordinary name.

**A local named `await` is the one exception, and such a file is skipped
rather than rewritten.** Dart accepts `final bool await = …` and every read of
it, but no tree-sitter Dart grammar can represent the read, so the declaration
could be removed while the read was left pointing at a name that no longer
existed. Any `.dart` file this Action cannot fully parse is now left untouched
and listed in the pull request under "still references this flag", the same
caveat a file with an unsupported extension earns. Rename the local and the
next run cleans the file normally. In practice nothing else trips this: across
**5,473 `.dart` files** from the Flutter SDK, zero would be skipped.

## Supported Ruby flag-read shapes

`client.bool_variation("KEY", context, default)` — the instance method — and
`Featureflip.bool_variation("KEY", context, default)` — the module-level
singleton — plus a bare `bool_variation(…)` where a project aliases the call.
Both quote styles and `%q()` are matched; the receiver may be anything
(`@client`, `Featureflip::Client`, `app.client`).

They fold in `if`/`elsif`/`else`, `unless`, `!` negation, `&&`/`||`
expressions, ternaries, the `x if cond` / `x unless cond` modifier forms, and
via a local `use_new = client.bool_variation(…)`, which is inlined and its
binding removed. A local that is later **reassigned** is left standing, and
`string_variation` / `number_variation` / `json_variation` are never matched —
Ruby is untyped, so the method name is the only type signal a call site
carries, and it is compared exactly.

Three shapes are deliberately left alone:

* **safe navigation** — `client&.bool_variation(…)` is `nil`, not a boolean,
  when the receiver is nil, so folding it to a literal would change what the
  method does;
* **an interpolated key** — `"old-checkout-#{suffix}"`, whose runtime value is
  not the key;
* **a symbol key** — `:"old-checkout"`. The SDK takes a string.

A **top-level** `use_new = client.bool_variation(…)` — outside any method, as a
`config.ru`, a Rails initializer or a `Rakefile` would write it — now inlines
too, including where a block (`configure do … end`) closes over it. A `def`,
`class` or `module` opens a fresh scope and does not see the top-level local,
so a same-named read inside one is correctly left alone.

A read **compared against a boolean literal** — `if client.bool_variation(…) ==
true`, `unless use_new != false` — folds through as well. Three neighbouring
shapes deliberately do not, and each leaves a correct but unsimplified
`if true == …` behind rather than a wrong answer: `===`, `equal?` and `eql?`
are identity and case-subsumption rather than value equality; and a comparison
whose other operand is *not* a literal is left alone, because Ruby's only falsy
values are `nil` and `false`, so `x == true` and a bare `x` disagree for every
truthy non-`true` `x`.

Indentation after a fold can be off by a level. Ruby blocks are `end`-delimited
so this is cosmetic, and `rubocop -a` normalises it.

## Supported ERB flag-read shapes

`.erb` templates run the **Ruby** rules above, unchanged. Every call form,
every folding shape and every deliberately-untouched shape in that section
applies here too — the difference is where the code lives, not what is matched.
So `<% if client.bool_variation("KEY", context, false) %>` folds like the `if`
it is, across separate tags:

```erb
<div class="cart">
<% if client.bool_variation("old-checkout", ctx, false) %>
  <%= link_to "Pay", legacy_path %>
<% else %>
  <%= link_to "Pay", pay_path %>
<% end %>
</div>
```

Removing the flag keeps the surviving arm's markup exactly as it was, and
leaves a blank line where each tag line stood.

Three things are specific to templates:

* **Output tags fold too.** `<%= <read> ? "Legacy" : "New" %>` becomes
  `<%= "Legacy" %>`; the tag's own delimiters come back unchanged.
* **Trim markers are preserved.** `<%-` and `-%>` change what the template
  renders, so a tag nothing matches comes back byte for byte, markers
  included, and a guard written with them folds exactly like one without.
* **ERB comments (`<%# … %>`) and literal tags (`<%% … %>`) are not code.** A
  flag read written inside either is text, and nothing rewrites it.

One shape is **refused** rather than rewritten, and the run exits 1 naming the
file: a **subject-less `case`** whose `when` is the flag read — `<% case %>` /
`<% when <read> %>`. In a `.rb` file that shape is folded; in a template it is
not, so the Action declines rather than shipping a template with the dead arm
still standing and the flag key already gone. Fix it by hand, or add the key to
`ignore`.

## Supported Kotlin flag-read shapes

`client.boolVariation("KEY", default)` (android SDK), in `if`/`else if`/`else`,
`!` negation, an `if`/`else` used as an expression, and via a `val` bound to
the read — which is inlined and its declaration removed.

Two shapes are deliberately left alone: a **safe call**
(`client?.boolVariation(…)`, whose type is `Boolean?` rather than `Boolean`, so
folding it would drop the null case) and an **interpolated key**
(`"KEY-${suffix}"`, whose runtime value is not the key).

A `val` bound to the read is inlined only when its **name is bound just once**
in scope. If the same name is also a lambda parameter, a `for` variable or a
`catch` parameter — `items.forEach { useLegacy -> … }` beside
`val useLegacy = client.boolVariation(…)` — the read still folds to its
literal, but the binding and the `if` reading it are left in place. The cost is
one uncleaned `val useLegacy = true`, which is the conservative half of the
same trade Gate 2 makes for TypeScript and C#.

An override map emptied by this cleanup becomes `mutableMapOf()`; add the type
arguments if inference needed the entry.

## Supported Java flag-read shapes

`client.boolVariation("KEY", context, default)` — the Java SDK's only boolean
read — as a member call on any receiver (the shape a DI-injected client
produces), or as a bare `boolVariation(…)` where a project static-imports or
aliases it. The `default` argument is not inspected: unlike Python's generic
`variation`, the method name is itself the type signal, so a non-literal
default is matched too.

They fold in `if`/`else if`/`else`, `!` negation, `&&`/`||` expressions,
ternaries — including in a **field initializer**, where there is no enclosing
statement to fold and the initialiser is rewritten in place — switch-expression
arms, and inside a **lambda**, in the two positions that are different
questions: a guard in a block body folds like any `if`, and the whole
expression body of a `Predicate` folds to the literal, because there the value
really is the payload.

A local bound to the read is inlined and its declaration removed, and a boxed
`Boolean useNew = client.boolVariation(…)` folds exactly like the primitive
`boolean`. A local that is later **reassigned** is left standing. A boxed local
that is tested against `null` — `if (useNew != null && useNew)`, the usual
reason to write the boxed type — refuses the flag when it serves **true**,
because inlining it there produces `true != null`, which does not compile.
Served false the whole `if` goes and the rewrite is clean.

A read in a **loop condition** is handled, and the three loop forms differ:

* `while (<read>) { … }` — served false the loop can never run, so the whole
  statement is removed; served true it becomes `while (true)`, and any
  statement the loop now makes unreachable is deleted and listed in the pull
  request body. A `break` that exits the loop keeps what follows reachable, and
  nothing after it is touched.
* `do { … } while (<read>);` — the body runs once before the condition is
  tested, so serving false leaves `while (false)`, which is both legal and
  exactly right. Left alone.
* `for (init; <read>; update)` — **refused** when the flag serves false. The
  body is dead but the initialiser still runs, so removing the statement could
  drop a side effect; there is no rewrite that is confidently correct, and this
  tool emits no diff rather than a wrong one. Fix it by hand, or add the key to
  `ignore`.

`boolVariationDetail` is never matched — it returns an
`EvaluationDetail<Boolean>` rather than a `boolean` — and neither are
`stringVariation` / `intVariation` / `doubleVariation` / `jsonVariation`. The
method name is compared exactly.

The OpenFeature Java provider (`io.featureflip:featureflip-openfeature`) is
matched as `client.getBooleanValue("KEY", default, context)`. The OpenFeature
Java client is synchronous, so unlike the JS/TS and C# shapes there is no
`await` to carry along and this is an ordinary call replacement. Arity is not
constrained, so the two-argument form matches too. `getBooleanDetails` returns a
`FlagEvaluationDetails<Boolean>` and is never matched, and neither is a
non-boolean accessor such as `getStringValue` carrying the same key.

The key may be hoisted to a `static final String`, to a `final` local, or to an
interface field (implicitly `final`) — see [The key hoisted to a
constant](#the-key-hoisted-to-a-constant).

A read that is **a whole statement** rather than a value — a warm-up call made
for the SDK's own exposure event, whose result is discarded — has the whole
statement deleted, because `true;` is not a Java statement and the call was
never read for its value. The statement must contain **no other call**: the
read's own argument list is the only one allowed, so
`verify(client).boolVariation("KEY", ctx, false);` and
`client.boolVariation("KEY", buildCtx(), false);` are refused rather than
rewritten. Both halves are in [What is deliberately NOT
rewritten](#what-is-deliberately-not-rewritten). This works with the key hoisted
too — to a `static final`, a `final` local or an interface field — under the
same conditions as any other hoisted key.

One further Java behaviour lives elsewhere in this document because it is not
about which shapes match: statements the fold makes **unreachable** are deleted
and listed in the pull request body, because Java rejects those at compile time
rather than warning about them. See [Known gaps](#known-gaps). Java is one of
the languages where this happens; the others are Dart, JavaScript and
TypeScript.

## Supported Go flag-read shapes

`client.BoolVariation("KEY", ctx, default)` — a method call on any receiver, or
a bare `BoolVariation(…)` where a package dot-imports or re-exports it. The
name is compared exactly and case-sensitively, so `StringVariation`,
`Float64Variation`, `JSONVariation` and `VariationDetail` are never matched. As
in Java the `default` argument is not inspected; the method name carries the
type.

`client.Boolean(ctx, "KEY", default, evalCtx)` — the OpenFeature Go client's
boolean read, for users of the Featureflip OpenFeature provider
(`github.com/canopy-labs/featureflip-go-openfeature`). Two things differ from
every other language's OpenFeature shape, and both are Go conventions rather
than choices. The key is the **second** argument, because the client takes a
`context.Context` first. And the client spells the same read twice, of which
only `Boolean` is matched: `BooleanValue(ctx, "KEY", default, evalCtx)` returns
`(bool, error)`, so standing a literal in its place would leave
`enabled, err := true`, which does not compile. The two-value form and
`BooleanValueDetails` are both left exactly as written.

The key must be an **interpreted** string literal. A raw string —
``client.BoolVariation(`old-checkout`, ctx, false)`` — is a different node to
the grammar and is not matched at all, so a repo that back-quotes its keys
reports "no changes" with nothing saying why.

Reads fold in `if`/`else if`/`else`, `!` negation, `&&`/`||` expressions, a
`return`, an argument position, a struct-literal field, and inside a function
literal or `defer`. A short variable declaration bound to the read —
`useNew := client.BoolVariation(…)` — is inlined and removed.

`var useNew = client.BoolVariation(…)` is **not** inlined: the read is replaced
and `var useNew = true` is left standing with the `if` that reads it. That is
valid, correct Go and it is as far as this goes — the conservative half of the
same trade Gate 2 makes for TypeScript and C#.

The key may be hoisted to a `const`, at package level or inside a function,
with or without an explicit type; a parenthesised `const ( … )` block is
resolved when it declares exactly one name. A `var` is not a constant here —
see [The key hoisted to a constant](#the-key-hoisted-to-a-constant).

A read that is **a whole statement** rather than a value — a warm-up call whose
result is discarded — has the whole statement deleted, for the reason given
under Java above: Go will not even let the value be discarded, so there is
nothing to fold it into and nothing to lose. The statement must hold **no other
call**, so `newClient().BoolVariation("KEY", ctx, false)` and
`client.BoolVariation("KEY", buildCtx(), false)` are refused instead. A
composite literal argument (`Context{User: "u"}`) is fine — Go has no
constructors — but `<-ready` is not, because receiving from a channel is a side
effect written without a call. This works with the key hoisted to a `const`
too, at package level or inside the function.

**Three shapes refuse the whole flag** rather than rewrite it, and the run
exits 1 naming the file. Two are in [What is deliberately NOT
rewritten](#what-is-deliberately-not-rewritten) — a discarded read holding
another call, and a tagless `switch` carrying a `fallthrough`, a `break` in the
arm that would be lifted out, or an initializer. (A tagless `switch` without
those is folded like an `if`/`else if` chain.) The third is specific to Go's
`if`:

```go
if v := prime(); client.BoolVariation("old-checkout", ctx, false) {
	legacyCheckout(v)
}
```

An `if` initializer runs **whether or not** the condition holds, and to the
grammar it is a sibling of the condition rather than part of it. Folding the
guard away would take `prime()` with it — and on the off-branch what is left
still compiles, so nothing downstream would ever tell you the call had gone.
Initializers are counted before and after the rewrite and the transform is
discarded if one goes missing. Lift the initializer above the `if` and the next
run cleans the file normally.

A fold can leave a variable or an import whose only remaining use was the
folded read, and in Go each of those is a compile error rather than a warning.
Both are cleaned up automatically: a pass looks for exactly that shape — a
binding referenced before the fold and referenced nowhere after it — and
removes it. An import is deleted outright. A variable's declaration goes with
it when the initializer cannot have a side effect, and is otherwise rewritten
to discard the value, so whatever it called still runs.

Fold output itself is `gofmt`-clean, including a multi-statement branch, so a
repo gating on `gofmt -l` will not flag the pull request over the fold.

**A flag-keyed map entry is the one exception, and only when `gofmt` aligned
it.** `gofmt` pads a multi-line map literal's values into a column sized by its
longest key, so deleting the longest key leaves the survivors padded for a key
that is no longer there — still valid Go, but `gofmt -l` will list the file.
Run `gofmt -w` on the branch before merging.

Files under a `go mod vendor` tree are skipped, detected by the
`vendor/modules.txt` manifest rather than by directory name.

## Supported PHP flag-read shapes

The `php` rule set covers `.php`, `.phtml`, `.inc` and `.module`.

`$client->boolVariation('KEY', $ctx, $default)` — a method call on any receiver
(`$this->client`, `Featureflip::client()`, …) — plus a bare `boolVariation(…)`,
which is the form a wrapper named in [`accessors`](#wrapping-the-sdk) takes.
Both quote styles are matched. The `default` argument is not inspected, because
the method name carries the type: `stringVariation` / `numberVariation` /
`jsonVariation` / `variationDetail` are never matched. That name is compared
**case-sensitively**, which PHP itself is not — `$client->BoolVariation(…)` is
left alone.

Three call forms are deliberately not matched: a **nullsafe**
`$client?->boolVariation(…)`, which is `null` rather than a bool when the
receiver is; a **static** `SomeClass::boolVariation(…)`, since the SDK has no
static accessor and a `::` call of that name belongs to somebody else's class;
and a **namespace-qualified** call such as `\App\Flags\boolVariation(…)`. There
is no Featureflip OpenFeature provider for PHP.

Reads fold in `if`/`elseif`/`else` — including the two-word `else if`, which
leaves no residue — `!` negation, `&&`/`||` and their `and`/`or` spellings,
ternaries, `??`, a `return`, and a `<?= … ?>` short echo. A local bound to the
read is inlined and its binding removed; one that is later **reassigned** is
left standing.

Unlike Java and Go, a read that is **a whole statement** does not refuse the
flag here — `true;` is a legal PHP no-op, so it is left standing rather than
blocking the run.

The key may be hoisted to a `const` at class, trait or file level, and read as
`self::K`, `Cls::K`, `static::K` or bare. A visibility modifier, `final` and a
PHP 8.3 type are all resolved, in any combination and with an attribute in
front — `#[Deprecated] final public const string FLAG_KEY = '…'` is removed
exactly like the bare spelling. A `static` property is not a constant and
`define()` is left alone — see [The key hoisted to a
constant](#the-key-hoisted-to-a-constant).

In a template, the surrounding markup is rewritten along with the code. A
**braced** block reads exactly as it would in a `.php` file:

```php
<div>
<?php
if ($client->boolVariation('old-checkout', $ctx, false)) {
    echo renderLegacy();
} else {
    echo renderModern();
}
?>
</div>
```

The **alternative syntax** (`<?php if (…): ?> … <?php else: ?> … <?php endif;
?>`) is rewritten the same way, in every arm, including `elseif` chains and a
rung in the middle of one. The arm that runs stays exactly as written, the
guard and the dead arms go, and a `<?php … ?>` pair left bracketing nothing goes
with them — so a folded template has no empty tags and no blank indented lines
where the guard used to be. The markup the page emits is unchanged; only
insignificant whitespace around the removed lines moves, because the whole
`<?php … ?>` line is taken rather than an empty tag pair left in place.

## Supported Swift flag-read shapes

`client.boolVariation("KEY", default: value)` (swift SDK). The key is the first
argument and is matched as a literal; the fallback is the labelled `default:`
argument and its value is not constrained. Any receiver works — `client`,
`self.client`, `flagProvider`, or a longer chain such as
`Featureflip.shared.client`.

Folded away entirely in `if` / `else`, under `!` negation, in a `&&` or `||`
operand, in a `guard … else` and in a ternary.

Two positions fold the read to a literal but leave the surrounding code
standing. A read bound to a `let` is not inlined, so
`let useLegacy = client.boolVariation(…)` becomes `let useLegacy = true` and
the `if useLegacy` reading it stays. A read used as a `while` condition
likewise becomes `while true` or `while false` rather than having the loop
resolved. In both the flag key is gone — which is what makes the flag
removable — but you are left with a line or two to tidy by hand. These are
limits of the transform engine's Swift cleanup, not safety refusals.

A key hoisted to a `let` constant is resolved, and the declaration removed with
the last read of it — see [The key hoisted to a constant](#the-key-hoisted-to-a-constant),
which applies to Swift on the same terms as every other language.

### The `@FeatureFlag` SwiftUI property wrapper

The property wrapper is also removed, and it is the one Swift shape whose
*declaration* is rewritten rather than an expression folded:

```swift
@FeatureFlag("KEY") var oldCheckout = false   ->   var oldCheckout = true
```

The key is the attribute argument and the fallback is the property's
initialiser, so both are replaced together: the initialiser is the wrapper's
*default* value, not the flag's, and leaving it behind would change what the
property reads. A type annotation and a visibility modifier are both carried
across (`@FeatureFlag("KEY") private var x: Bool = false` becomes
`private var x: Bool = true`), and where the property is `private` the cleanup
usually goes further and inlines it at its use sites.

The wrapper is left alone when any of the following holds, and the key then
stays in the file so the run reports the flag as not fully removed:

* the file also names **`_x` or `$x`** — a property wrapper synthesises both,
  and neither survives the rewrite to a plain stored property;
* the declaration carries **any other attribute or modifier**
  (`@FeatureFlag("KEY") @State var x = false`), since only what is matched can
  be carried across and silently dropping a modifier would change the
  declaration's meaning;
* the wrapper is applied to a **local variable inside a function**, which the
  wrapper does not support anyway — it reads its provider from the SwiftUI
  environment, which is only injected into a view's stored property.

One further shape is left alone entirely, and likewise leaves the key in place:
a **doubly-negated** read (`!!client.boolVariation(…)`).

### Two safety gates

Correctness does not rest on the Piranha rules guarding themselves — twice, a
tree-sitter query guard was written as a list of forms and twice a form outside
the list leaked through. Both guarantees are enforced outside the rules, by
code that parses the whole file:

* **Gate 1 — post-transform re-parse.** Every rewritten file is re-parsed before
  the diff is produced. The transform is discarded — entirely, across all files
  — if it introduced a syntax error, put a reserved word in a binding position,
  stranded a keyword where an expression can be read, or let two surviving
  statements fuse under Automatic Semicolon Insertion. All four checks are
  differential against the input, so a file the grammar already disliked stays
  eligible and only new breakage is rejected. Measured false-positive rate: 0 of
  833 simulated deletions across 463 real `.ts`/`.tsx` files, in both
  semicolon-terminated and semicolon-free style.

  **Gate 1 is a strong net over specific failure classes, not a correctness
  proof.** Three of its four checks ask whether the result still parses as
  intended, which a wrong rewrite can pass; the fourth (ASI) and Gate 2
  (shadowed references) each close one known parses-but-wrong case. A new one
  needs its own check. Every check here was added after a real escape, and each
  of them left the ERROR-node count untouched. Read the diff.
* **Gate 2 — pre-flight shadow check.** If the identifier bound to the flag read
  is bound *anywhere else in the file* — by any binder, whatever its initialiser
  looks like — the const-propagation rules are withheld for that file. It keys
  off the grammar's binding *fields* (a small closed set), not off initialiser
  node types (an open-ended set TypeScript keeps growing), which is what makes
  it sound without scope analysis.

  In **every** language it also guards the same file-scoped question about a
  different const: the one holding the flag **key** rather than the read's
  result. It withholds when that name is bound more than once, and also when
  anything other than a flag read references it — that second half is what
  keeps the declaration from being deleted out from under a live reference.

Gate 2 is deliberately conservative and file-scoped: a same-named variable in an
unrelated function in the same file is enough to withhold it. The cost is one
uncleaned `const on = true;` — or, in C#, one flag read left as it was.

`node --check` was evaluated for Gate 1 and rejected: it parses JavaScript, and
it flagged **286 of 431** real TypeScript files in a production codebase as
syntax errors. A gate with that false-positive rate silently produces no diffs
forever and looks like "nothing to clean up". tree-sitter flagged 4 of the 431,
all handled by the differential design, and needs no Node in the action image.

### What is deliberately NOT rewritten

Anything the rules can't rewrite with confidence is left alone — the read is
replaced by the literal at most, and often nothing changes at all. No diff
means no PR.

| Shape | Behaviour |
| --- | --- |
| A **different SDK's** same-named method with the same key (`ld.boolVariation("KEY", …)`) | **rewritten** — matching is by method name plus key string, never by import origin. It takes both a collision in the method name *and* a collision in the flag key, but if your repo has two flag systems sharing a key, scope the run with `directories` |
| **Your own wrapper** around the SDK (`useFlag("KEY")`, `isOn("KEY")`) | no change at all — the callee name must be one of the shapes above. This is the common case in real codebases, and it means the Action may report `no-changes` for a flag your code reads everywhere. Call the SDK directly at the read site, or add the key to `ignore` so it stops being reported |
| Any other call that happens to take the key as its first argument (`trackEvent("KEY")`, `api.deleteFlag("KEY")`) | no change at all — same rule. These are not flag reads and rewriting one would delete a real call |
| Key is not a string literal (`boolVariation(keyVar, …)`) | no change at all — **unless** `keyVar` is a constant declared in the same file, which is resolved and removed (see [The key hoisted to a constant](#the-key-hoisted-to-a-constant)) |
| A C# `const string` key that is **also** read by anything other than a flag read | no change at all — not even the read is folded. Deleting the declaration would strand the other reference, and folding without deleting would leave a `const` the next run reports as a live reference forever |
| A C# `const string` key whose name is **bound again** anywhere in the file | no change at all — a shadowing binding means a reference may resolve to the other one, and the key could be read off the wrong declaration |
| A key held in a REASSIGNABLE binding (C# `static readonly`, a JS `let`, a Kotlin `var`, a non-`final` Java field) | no change at all — its value cannot be read off the declaration, so the flag it names is not knowable |
| A key hoisted to a constant in a DIFFERENT file | no change at all — this tool reads one file at a time, so a declaration it cannot see is a non-literal key |
| A C# read inside an **expression tree** (`mock.Setup(c => c.BoolVariation("KEY", …))`) | no change at all — the lambda *describes* the call instead of performing it, so the text of the read is the payload a mocking library reads at run time. This one is not detected by naming `Setup`/`Verify`: the tell is that the read's receiver is the lambda's own parameter, so any library taking an `Expression<>` is covered, while a genuine `Func<>` lambda (`list.Where(x => flags.BoolVariation("KEY", …))`, reading off a captured client) is still rewritten. If the key is a `const`, the whole const path is withheld for that file — the mock setup is a live reference to the declaration |
| A read handed to a **mocking DSL** that takes the call itself — `when(<read>).thenReturn(…)` (Mockito, mockito-kotlin, `package:mockito`, ts-mockito), `given(<read>).willReturn(…)` (BDDMockito), or `every { <read> } returns …` (MockK) | **the whole flag is refused**, and the run exits 1 naming the file. These libraries stub by *performing* the call and capturing the invocation it registers, so the text of the read is the payload — the same property that makes the C# row above untouchable. Folding it leaves `when(true).thenReturn(true)`, which **compiles**: the stub silently stops stubbing, and the flag key is gone from the file so no later run looks again. Unlike C#, there is nothing structural to key on — no lambda exists, because the call is real — so the guard is a short list of stubbing entry points by name. Delete or rewrite those lines yourself, scope the run with `directories` to exclude your tests, or add the key to `ignore` |
| A **Go read inside a gomock expectation** (`m.EXPECT().BoolVariation("KEY", ctx, false).Return(true)`) | **the whole flag is refused**, and the run exits 1 naming the file. Same shape as the row above, caught a different way: the fold leaves `true.Return(true)`, and because Go's `bool` is a builtin with no method set, a selector on a boolean literal is broken Go *by construction* — so this one needs no list of library names. Before this was refused it shipped, and the customer's build was what reported it |
| `export const on = <read>` | read replaced, **declaration kept** → `export const on = true;`. An exported binding is module public API, and the modules importing it do not contain the flag key, so they are never in the candidate set — this tool cannot see them, let alone update them. Removing the binding would break files it never parsed. Delete it yourself once the importers are gone |
| An **un-awaited** OpenFeature read (`client.getBooleanValue("KEY", …)`, `client.GetBooleanValueAsync("KEY", …)`) | no change at all — the value is a `Promise<boolean>`/`Task<bool>`, not a boolean. Await it at the read site and it is rewritten |
| The **two-value** form of an OpenFeature Go read (`enabled, err := client.BooleanValue(ctx, "KEY", …)`) | no change at all — it returns `(bool, error)`, so standing a literal in its place would leave `enabled, err := true`, which does not compile. The single-value `client.Boolean(ctx, "KEY", …)` is rewritten; switch the read to that form if you want it folded |
| `getBooleanDetails` / `GetBooleanDetailsAsync` / `get_boolean_details` / `BooleanValueDetails` | no change at all — these return an evaluation-details object, not a boolean |
| A non-boolean OpenFeature accessor carrying the key (`getStringValue("KEY", …)`) | no change at all — this tool only removes boolean flags |
| `client?.boolVariation("KEY", …)` | no change at all — it is `undefined` when `client` is nullish, so a literal would change behaviour |
| `client.boolVariation?.("KEY", …)` | no change at all — same reasoning: the *call* is optional, so the expression is `undefined` when the method is missing |
| `let on = <read>` | read replaced; never inlined (a `let` may be reassigned) |
| `const on = <read>, other = 1` | read replaced; the const is not inlined or removed |
| The name is bound anywhere else in the file (Gate 2) | read replaced; the const is not inlined or removed |
| `on` used in a type position (`typeof on`) | that reference is left; the declaration is kept |
| `{ on }` shorthand or `export { on }` | that reference is left; the declaration is kept |
| Folding an `if` whose branch declares a `const`/`let`/`class`/`function`/`enum`/`interface`/`type` | the branch keeps its `{ … }` so the declaration stays scoped |
| Folding an `if` that sits in an `else`, or as a braceless `if`/`while`/`for` body | the branch keeps its `{ … }`. Only a position that can hold several statements gets them spliced in flat |
| A **Java, Go or C# read that is a whole statement** and holds **another call** — `verify(client).boolVariation("KEY", ctx, false);` (Mockito), `client.boolVariation("KEY", buildCtx(), false);`, `new Ctx()`, `contexts[index()]`, Go's `<-ready`, or C#'s `ctxs[i++]` | **the whole flag is refused**, and the run exits 1 naming the file. Deleting the statement would delete that call with it — the mock's assertion, or a context the customer builds — and replacing only the read leaves `true;` standing alone, which none of the three accepts (javac: `not a statement`; `go build`: an unused constant expression; C#: CS0201). Delete or rewrite that line yourself, or add the key to `ignore`. A discarded read holding **no** other call is rewritten: see the row below |
| A **Java, Go or C# read that is a whole statement** and holds no other call — a warm-up call made for the SDK's exposure event, `client.boolVariation("KEY", ctx, false);` | **the whole statement is deleted.** Its value is discarded by definition, so there is nothing to fold it into and nothing to lose. Any receiver works, including a static import with no receiver and a field chain; the argument list may hold names and literals (and, in Go, composite literals, which have no constructors to run). The key may be a literal or hoisted to a constant — see the row below. In C# the awaited OpenFeature read is covered too, while an un-awaited one is left alone; in Go the OpenFeature `client.Boolean(ctx, "KEY", …)` read and in Java the OpenFeature `client.getBooleanValue("KEY", …)` read are covered the same way. A project's own wrapper named in `accessors` is covered the same way, whether the key is a **literal** or hoisted to a constant |
| A discarded read whose **key is hoisted to a constant** (`client.boolVariation(FLAG_KEY, ctx, false);`) | **the whole statement is deleted**, and the declaration with it, exactly as for a literal key. The constant may be declared at file scope or local to the method, and must meet the same four conditions as any other hoisted key — see [The key hoisted to a constant](#the-key-hoisted-to-a-constant). A statement holding another call is refused here too: the row above applies unchanged, because the safety rule does not depend on how the key is spelled |
| A **PHP template written in the alternative syntax** (`<?php if (<read>): ?> … <?php else: ?> … <?php endif; ?>`) | **rewritten**, markup and all: the arm that runs stays, the guard and the dead arm go, and a `<?php … ?>` pair left bracketing nothing goes with them. The rendered HTML keeps the same markup; only insignificant whitespace around the removed lines moves, because the whole `<?php … ?>` line is taken rather than an empty tag pair left behind. `elseif` chains are handled in the same way, in any position |
| A **multi-way guard** — Go's tagless `switch { case <read>: … }`, Ruby's subject-less `case` / `when <read>`, or PHP's `switch (true) { case <read>: … }` | **rewritten.** Each arm is a condition and the first true one wins, so this is an `if`/`else if` chain by another spelling. Where the flag serves **false** the dead arm is removed, in any position. Where it serves **true**, an arm with nothing live above it collapses the whole statement to its body (Go and Ruby), and an arm with a live arm above it becomes the terminal `default:` / `else`, taking the arms below it — they sit behind a guard that now always passes. PHP always takes the `default:` form, because a body ending in `break;` is fatal outside a switch. A switch on a **value** (`switch ($v)`, `case v`) is untouched: there the arms compare values rather than act as guards |
| A multi-way guard the fold **cannot** finish | **the whole flag is refused**, and the run exits 1 naming the file. In Go: a `fallthrough` anywhere in the switch, a `break` in a body that would be lifted out of it, or a `switch v := f(); {` initializer whose binding the arms use. In Ruby: a `when` listing several patterns (`when <read>, isAdmin(ctx)`), where a literal among alternatives is not the whole condition. In PHP: an arm before the flag's that does not end in `break`/`return`/`continue`/`goto`/`exit`, because PHP falls through **by default** and that arm runs into the flag's with nothing in the source saying so — an arm ending in `throw` or `die()` counts as not terminating, since the grammar cannot tell either from an assignment |
| A PHP **`elseif` that is not last in its chain**, where the flag serves **true** | **rewritten**: the clause becomes the chain's `else`, and every branch after it is deleted. That is not tidying — those branches sit behind a guard that now always passes, so they are unreachable, and leaving them would keep an `else` in front of them, which does not parse. Both spellings (`elseif` and `else if`) behave the same. Where the flag serves **false** the dead branch is removed, in any position |
| A **Python read bound to a name outside a function** (`use_legacy = <read>` at module or class level) | **the whole flag is refused**. Inside a function the binding is removed and its references replaced with the literal; at module or class level that name is public API, and the modules importing it do not contain the flag key, so they are never in the candidate set — this tool cannot see them, let alone update them. Leaving `use_legacy = True` standing would be permanent, since the flag key is gone from the file. Move the read inside the function that uses it, fix it by hand, or add the key to `ignore` |

### Known gaps

* **A file that still mentions the key after the rewrite is named in the PR
  body**, under "still reference this flag". That is the caveat to read before
  merging: it means the pull request does not remove every trace of the flag.
  Three different things put a file on that list, and only the first is a
  language-coverage problem:

  1. the file's extension is outside the configured `languages`, so nothing
     looked at it. With every language enabled there is **nothing on this list
     by default** — every extension this Action knows a flag read can live in is
     claimed by a language it supports — but it fires whenever you NARROW
     `languages`, which is the case it exists for: set `languages: ts` in a
     repository whose ERB templates also read the flag and the caveat names
     those `.erb` files;
  2. the engine could not parse the file, so it was quarantined (see the
     grammar gap further down);
  3. **the mention is not a flag read and not a flag-keyed entry.** The rules
     rewrite reads (a call taking the key as a string literal) and delete
     entries keyed by the flag in a literal that is provably about flags (see
     "Wrapping the SDK"). What is left: a test whose assertion was the gate, a
     comment, a string passed to something other than an accessor, a map
     entry whose siblings are not flags and whose value is not a boolean, a
     middle or last member of a `;`-separated TypeScript object type, a Java
     `Map.of` or Swift dictionary entry (not yet). Each is a line you have to
     delete yourself.

  On cause 1 specifically: Ruby views (`.erb`), the TypeScript module suffixes
  (`.mts`, `.cts`), Kotlin scripts (`.kts`), Rake tasks (`.rake`) and Swift
  (`.swift`) were each an uncovered extension once and are now processed
  normally.

  **One set of directories is exempt from the naming entirely:** `dist/`,
  `build/`, `out/`, `coverage/`, `.next/`, `.nuxt/`, `.output/`, `.svelte-kit/`
  and `.turbo/` are not searched for surviving references, because a repository
  that commits its compiled output would otherwise get that caveat on every PR
  pointing at bundles the next build regenerates. Those names are skipped only
  for the *warning* — a hand-written `.ts` file in one of them is transformed
  exactly like any other. (`node_modules/`, `.git/` and a Go `vendor/` tree are
  skipped everywhere — see the `vendor/` gap below.)
* **In C# only, a mock that stubs the flag read WITHOUT a lambda is
  indistinguishable from a real read, and is rewritten.** The expression-tree
  guard above works because `mock.Setup(c => c.BoolVariation("KEY", …))` puts
  the read inside a lambda whose parameter is its own receiver. A library that
  stubs by calling the member on the substitute directly — NSubstitute's
  `sub.BoolVariation("KEY", ctx, false).Returns(true)` — produces a call
  expression identical in shape to a genuine read, and nothing in the syntax
  says otherwise. It is rewritten to `false.Returns(true)`, so you will see it:
  the build breaks rather than the test quietly passing. Scope the run with
  `directories` to exclude your test projects if you stub flags that way.

  The `when` / `given` / `every` family in Java, Kotlin, Dart and TypeScript,
  and gomock in Go, are **refused** rather than rewritten — see the two rows
  above. C# is the one that stays a gap, and the reason is specific: neither
  half of what catches those works here. There is no stubbing entry point to
  name, because the read is called on the substitute directly; and Go's
  structural tell does not transfer, because `false.ToString()` is ordinary
  C# and NSubstitute's `Returns` is an extension method on `T`, so
  `false.Returns(true)` is not broken by construction the way `true.Return(…)`
  is in Go — it may even compile.

* **A flag read inside an ASSERTION is folded, and the assertion becomes
  vacuous.** `assertTrue(client.boolVariation("KEY", ctx, false))` becomes
  `assertTrue(true)`, and the same goes for `expect(<read>).to eq(true)`,
  `$this->assertTrue(<read>)` and their equivalents. This is deliberate, and it
  is where the line sits: a stub is refused because the *text* of the call is
  the payload and folding it changes what the test does, whereas an assertion
  genuinely was about a flag that no longer exists. Folding a read into an
  ordinary argument is also correct everywhere else — `render(<read>)` must
  keep working — so there is no rule that could refuse the assertion without
  refusing those too. The test still compiles and still passes; it just no
  longer asserts anything about that flag, so delete it with the flag.

* **OpenFeature's synchronous client is matched only where the file's imports
  say so.** `@openfeature/server-sdk` and `@openfeature/web-sdk` spell the read
  identically — same method name, same arguments — but the first returns a
  promise and the second a plain boolean. Nothing at the call site tells them
  apart, so the rules use the file's imports: a bare `getBooleanValue(...)` is
  rewritten in a file that imports `@openfeature/web-sdk` and mentions no other
  `@openfeature/…` package, and is left alone everywhere else. A file that
  imports both clients is left alone entirely, because there the imports no
  longer say which client any one read belongs to.

  What that does not reach is a client the file never imports — passed in as a
  parameter, read off a context object, or re-exported through a module of your
  own. There the read survives. The file IS named in the PR body's "still
  reference this flag" list, since the key is still in it, but the caveat cannot
  tell you the surviving mention is a live read rather than a comment. If you
  read flags that way, check the diff before merging, or name your read site in
  [`accessors`](#wrapping-the-sdk).

* **If you commit your build output, it is rewritten too, and the PR body says
  which files.** A bundle is source as far as the transform is concerned — the
  flag read survives minification intact, so skipping it would leave a live
  read in a tracked file while the PR claimed the flag was gone. Rewriting it
  keeps the claim true, at the cost of hunks you did not write appearing in the
  diff (a minified one is not reviewable in practice). Your next build
  regenerates those files from the sources the same PR cleaned, so the tree
  converges either way; the PR body lists them so you know which hunks to skip
  over. If you would rather they never appear, exclude the directory with
  `directories`, or stop committing it.
* **Statements the fold makes unreachable are deleted, and the PR says which.**
  Collapsing the flag's `else if` into an `else`, or folding `if (<read>) {
  return a; }` down to a bare `return a;`, can leave a trailing statement no
  path can reach. Where the language's standard gate rejects that, the
  statements are removed with the fold: a compile *error* in Java (JLS 14.21),
  a non-zero exit from `dart analyze`, and a `no-unreachable` error under
  `eslint:recommended` in JavaScript and TypeScript. The toolchain itself
  proves they can never run, so removing them cannot change behaviour; the
  alternative is a pull request that cannot go green. Each one is listed
  verbatim in the PR body, since it is still code you wrote. A file that
  *already* contained unreachable code is left alone entirely — there the tool
  cannot tell its own strands from yours. The remaining languages report
  nothing on the same output, so nothing is deleted there.
* **A parameter the fold leaves unused is not removed, and
  `eslint:recommended` reports it.** Where the flag read was a function's only
  use of one of its parameters — `function typed(ctx) { const on =
  client.boolVariation("KEY", ctx, false); … }` — folding the read away leaves
  `ctx` unreferenced, and `no-unused-vars` defaults to reporting a trailing
  unused parameter. It is left standing on purpose: removing a parameter
  changes the function's signature, and every caller of it lives outside the
  diff this tool is allowed to make. A local variable or an import in the same
  position IS cleaned up, precisely because neither is anyone else's business.
  Delete the parameter yourself in the same pull request, or keep it. Every
  other language leaves the parameter too; JavaScript and TypeScript are where
  a standard gate has something to say about it.

  Two narrower cases of the same shape are not cleaned either, and these ones
  are simply not reached yet: a binding declared at the **top level of a
  module** rather than inside a function (`const client =
  OpenFeature.getClient();`, once every read through it is gone), and
  TypeScript's `import x = require("…")` form in a `.cts` file. Both are
  reported by `no-unused-vars`; delete them by hand.
* Empty blocks left by a fold are not cleaned up. A fold can
  also leave a blank line where the guard's own line was, in Go and Ruby. Fold
  output **is** re-indented, in every language: a spliced branch comes back at
  the `if`'s own column with its internal structure — a nested `if`, a loop —
  kept intact, so the diff reads as a diff rather than as a reformat, and a Go
  multi-statement fold is `gofmt`-clean. Nothing else in the file is touched:
  this re-indents only lines the fold moved, and never a line whose leading
  whitespace is part of a string. In a PHP template the fold also
  removes the whole `<?php … ?>` line rather than leaving an empty tag pair
  behind, so the surviving markup keeps its own indentation instead of the
  guard's — the markup emitted is the same, only the whitespace between it
  moves.
* **A comparison against a non-literal is propagated but not folded.**
  `if (on === true)` reduces fully, because both sides end up literal. But
  `if (on === other)` becomes `if (true === other)` and stops — correct, and
  deliberately not reduced further: `===` produces a boolean while `other`
  produces whatever it is, so folding it away would change the expression's
  value. Rewrite it as a plain truthiness test if you want the `if` to
  disappear too.
* **The transform engine's TypeScript grammar is older than TypeScript.**
  Piranha bundles its own parser, and a file it cannot parse makes it abort
  before any rule runs. Such files are **quarantined**: they are left untouched,
  the rest of the flag's files are still cleaned, and the pull request lists them
  under "still reference this flag" so nobody merges it
  believing the flag is gone. Only when *every* candidate file is unparseable
  does the flag report `[piranha-error]` and exit `1`. Confirmed cases: a
  type-level `import()` inside type arguments
  (`importOriginal<typeof import('./x')>()`), and JSX in a `.ts` file — the
  latter correctly, since `tsc` rejects it too. TC39 auto-accessors
  (`accessor x = 1`), `satisfies`, `const` type parameters and `using`
  declarations were all on this list and are now parsed cleanly. Before
  quarantining existed, one such file anywhere made every flag in the
  repository permanently unproposable.
* **A Go `vendor/` tree is never rewritten.** It is third-party source the
  toolchain owns: `go mod vendor` regenerates it, so a cleanup committed there
  is reverted on the next sync while `go build -mod=vendor` complains in
  between. Detected by the `vendor/modules.txt` manifest rather than by the
  directory name, so a hand-written directory called `vendor` in another
  ecosystem is still cleaned normally.
* **Only files git tracks are rewritten.** A commit stages tracked files, so an
  untracked, gitignored or submodule-owned file could never reach the pull
  request — it is skipped rather than rewritten and reported, so a dry run
  previews exactly what a real run would open.
* **Swift covers the client's own read shapes and the `@FeatureFlag` property
  wrapper; three narrower shapes are left standing.** A doubly-negated read
  (`!!client.boolVariation(…)`) is deliberately skipped rather than guessed at,
  and the property wrapper is left alone where the file names its synthesised
  `_x`/`$x`, where the declaration carries another attribute or modifier, or
  where it is applied to a local variable. In every case the read survives, the
  flag is reported as not fully removed, and the surviving-reference caveat
  names the file — so they cost you a manual edit, never a wrong one.
* **Swift is the slowest language here.** Removing one flag costs roughly two
  seconds per rewritten file against hundredths of a second for Go or Java.
  Files the flag does not appear in are not affected, so the cost tracks how
  many places the flag is actually read, not the size of your repository.
* **A Python read bound to a name is inlined only where it is provably a
  local.** `use_legacy = client.variation(…)` has its binding removed and every
  reference replaced with the literal — but only when the name belongs to
  exactly one *function* scope and every mention of it in the file is one the
  safety analysis accounts for. Four shapes are left standing instead, and the
  run then **refuses** the file: a binding at module or class level (public
  API — see the table above); a name bound a second time anywhere in the file;
  one a nested function closes over; and one sharing its line with another
  statement. A refusal names the file and exits `1`, because a partial rewrite
  would be silent and permanent: the flag key is gone from the file afterwards,
  so every later run would report the flag clean. Fix it by hand or add the key
  to `ignore`. (Multi-statement branches and `elif` promotion used to be
  refused outright for the same reason and no longer are — like this inlining,
  they are finished after the engine has run, where the whole file is in hand.)
* **Python cleanup can leave extra blank lines at the deletion site.** Removing
  a module-level guard leaves the blank lines that were above and below it, so
  up to four in a row (`E303`). This is deliberate: the engine's blank-line
  collapsing is whole-file, and enabling it rewrote PEP 8's two-blank-line
  spacing between definitions the removal never touched — turning every Python
  PR into an unrelated reformatting. Local damage at the edit beats global
  damage; run the PR through `black`/`ruff format` to clear it.
* **A Python flag whose SDK call has no boolean-literal `default` is left
  alone.** The Python SDK exposes only a generic `variation(key, context,
  default)`, so the call carries no evidence of the flag's type — folding
  `variation("KEY", ctx, "control")` to `True` would silently change a string
  flag into a boolean. Only a boolean literal in the `default` position (or
  `default=`) counts as that evidence. A wrapper named in `accessors` is not
  subject to this: declaring it supplies the evidence instead.
* **Java and Go have no shadow check on the READ const**, because they do not
  need one: there the variable inlining is the engine's own rather than this
  tool's, so there is no rule set to withhold per file. Both are covered on the
  **key** const, like every other language — a shadowed or otherwise-referenced
  key withholds the file. Gate 1 (the post-transform re-parse) applies
  everywhere.
* Gate 2 is a *binding* check, not a scope resolution. It is sound for
  withholding — any second binder disqualifies the file — but it cannot tell a
  real shadow from a harmless same-named variable elsewhere, so it withholds
  more often than strictly necessary. The rule set carries guards of its own
  against the same hazard, but those are defence in depth only: their lists are
  known to be incomplete and nothing relies on them.
* Gate 1 rejects transforms that break *syntax*. It cannot detect a change that
  parses and compiles but alters behaviour; Gate 2 is what covers the known
  instance of that class (shadowed references).
