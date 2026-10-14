# Featureflip Flag Cleanup Action

Deterministic dead/stale feature-flag removal for
[Featureflip](https://featureflip.io/), powered by the
`polyglot-piranha` AST transform engine. Runs inside the
customer's CI, fetches removal candidates from the Featureflip public API, and
opens one PR per flag. The setup guide lives at
[featureflip.io/docs/integrations/flag-cleanup-action](https://featureflip.io/docs/integrations/flag-cleanup-action/).

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

In this mode the Action reads removal candidates from that API and makes one
small write: it records a removal pull request's link on the flag, so the
notice Featureflip sends the flag's owner can link straight to it. That
happens after opening a pull request, and again on any later run that finds
the same pull request still open — so a link a prior run couldn't record (a
transient error, a token still on `Viewer`, or a pull request opened before
this existed) still reaches Featureflip once the run succeeds. Recording the
link needs a token with the `Member` role. A `Viewer` token
restricted to the one project still works, and is the least privilege that
does: pull requests are opened exactly the same way, the run logs one warning,
and owner notices simply don't link them. Nothing here archives, toggles or
deletes a flag — removing the code is proposed as a pull request for you to
merge, and what happens to the flag afterwards stays your decision.

### Archive the flag once the removal is deployed

Merging a removal pull request deletes the code, but the flag itself stays live
in Featureflip until somebody archives it. `mode: archive-sweep` closes that
loop on a schedule. It is **opt-in and separate**, in its own workflow on its
own trigger — and it is the one part of this Action that can change what your
users see, so read this section before wiring it up.

**The loop closes on the deploy, not on the merge**, and those are the same
moment in some pipelines and nowhere near it in others. Archiving is not a soft
state: the flag leaves the evaluation payload within seconds, every connected
SDK drops it, and each read falls back to the default value written beside it at
the call site. If a build that still reads the flag is running anywhere in
production at that moment, that fallback is what production serves from then on.

**Which way the feature moves is not a coin flip.** This Action only proposes
flags whose telemetry says they have served one variation and nothing else for
months. For a flag that is fully rolled out, that variation is the on one, while
the default beside the read — the value its author wrote for the case where the
flag cannot be reached at all — is almost always the off one. So archiving a
rolled-out flag ahead of its deploy does not disturb the feature at random. It
turns the feature **off**, for everyone, until the deploy lands.

**Featureflip refuses the archive while it can still see the flag being
evaluated**, which is what stops that being a silent outcome. The refusal is
`FLAG_RECENTLY_EVALUATED`, and this Action reports it as `[deferred]` and exits
`0` — nothing failed, nothing is for you to do, and the next sweep tries again.
It is a backstop reading telemetry that has already been reported, though, not
permission to treat a merge as a deploy: it can only rule on environments it has
seen traffic from, and a flag nothing has evaluated recently is allowed through.
That is right for a flag that is truly dead and wrong for code that is still
deployed but rarely reached — a monthly job, an admin-only page, an error path.
If your deploys can trail your merges by more than a day and you have code like
that, add [`archive-on-deploy`](#archive-when-the-deploy-actually-lands), which
asks git whether the removal is in the commit you are running.

The sweep fits comfortably when merging *is* deploying — the merge triggers the
pipeline, the pipeline finishes on its own, the old build is gone minutes later
— and it is also workable when they are hours or days apart, because the
deferral covers that gap:

* **Promotion to production is manual, staged or gated** — a release train, a
  canary, an approval step. A removal will usually report `[deferred]` on the
  first runs after it merges, and the sweep archives it once traffic drains. If
  your pipeline creates GitHub Deployments,
  [`archive-on-deploy`](#archive-when-the-deploy-actually-lands) adds a second,
  deterministic check on top — it will not even attempt a flag whose removal is
  not in the commit you are running. It does **not** replace the sweep, and
  does not avoid the deferral.
* **Long-lived browser sessions are normal for your users** — a tab opened
  before the deploy is still running the old bundle and still reading the flag.
  Its evaluations keep the refusal in place until those sessions drain, which is
  exactly what you want; the sweep archives afterwards.

**One case is genuinely not covered, and no amount of retrying fixes it:**

* **You ship to clients you cannot update** — mobile apps, desktop apps,
  embedded devices, anything installed rather than served. Old versions keep
  evaluating the flag for as long as people keep running them, which can be
  months, so the refusal never clears on its own and the sweep defers
  indefinitely. That is the correct behaviour — the flag really is still being
  read — but it means archiving is a decision rather than a schedule. Archive
  those by hand once the old versions are genuinely gone, or deliberately with
  `?force=true` on the archive endpoint, accepting that whatever is still
  running falls back to its own default.

Nothing about `mode: remove` depends on these workflows: the removal pull
requests keep arriving regardless, and what happens to the flag afterwards stays
your decision.

```yaml
name: Featureflip archive sweep
on:
  schedule:
    - cron: '0 6 * * *'
  workflow_dispatch:

permissions:
  pull-requests: read

jobs:
  sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: canopy-labs/featureflip-flag-cleanup-action@v1
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        with:
          mode: archive-sweep
          api-token: ${{ secrets.FEATUREFLIP_ARCHIVE_TOKEN }}
          org: my-org
          project: my-project
```

Each run finds every flag in the project whose removal pull request merged but
which is still live, and tries to archive it. Five things to note.

**It needs a different token, and this is the whole reason the mode is
separate.** Archiving requires a token with the **Member** role. Fetching
removal candidates needs only read access — the cleanup workflow's one small
write, recording a pull request's link (see above), wants `Member` too but
degrades cleanly to `Viewer`. Give this workflow its own secret rather than
reusing the cleanup workflow's. The difference matters if a token ever leaks:
the cleanup token discloses flag names at worst, while a Member token here can
change flag state for real users.

**It needs `GITHUB_TOKEN`, with `pull-requests: read`.** It runs on a schedule,
so no pull request is handed to it; it asks GitHub — once per live flag —
whether that flag's removal branch ever produced a *merged* pull request. It
never writes anything to your repository, and needs no `actions/checkout`.

**Evidence of a merge is what authorises each archive.** A removal pull request
you closed *without* merging is a removal you declined, and reads to this mode
exactly like a flag that was never proposed: it is left alone. Nothing is
archived that this Action did not open a pull request for.

**There is no state to manage.** Nothing is written when an archive defers — a
flag still being live *is* the record — so running this more often, less often,
or twice at once is harmless, and there is nothing to reset if you turn it off.
Archiving is idempotent, so a flag archived between two runs is simply not
outstanding on the second. `dry-run: true` prints the flags it would attempt and
sends nothing.

**A flag that is not deployed yet is an outcome, not a failure.** When
Featureflip refuses because live traffic is still evaluating the flag — the
hazard described above, caught — the run prints:

```
[deferred] old-checkout not archived yet: live traffic is still evaluating this
flag in production. The removal merged but has not finished deploying — nothing
to do, and `mode: archive-sweep` re-attempts it once traffic drains
```

and moves on to the next flag. It is deliberately not a red build: wherever
deploys lag merges this is the ordinary result for a few runs after each
removal, and a failing job nobody can act on is one people learn to ignore.

A flag can also legitimately fail, and the message says which case you are in.
Each is named on its own line, the rest of the backlog is still attempted, and
the run exits 1, because each needs a person:

| What happened | What to do |
| --- | --- |
| Another live flag lists this one as a prerequisite (`FLAG_HAS_DEPENDENTS`), and that flag's removal has not merged | Remove that prerequisite. The next sweep archives the flag. |
| A scheduled change still targets the flag (`FLAG_HAS_PENDING_SCHEDULES`) | Cancel the pending schedule. The next sweep archives the flag. |
| `403` | The token cannot archive. Check this workflow is not reusing the cleanup workflow's secret. A `403` stops the run, since every other flag would fail the same way. |
| `404` | `org`/`project` do not name the project the flags live in, or the flag was deleted while the run was in progress. |

**A flag whose dependents are being removed too is deferred, not failed.**
Removing a client-side flag and then the server flag it lists as a prerequisite
is the usual order, and until the client flag is archived Featureflip refuses
the server flag with `FLAG_HAS_DEPENDENTS`. When every dependent it names has a
merged removal pull request of its own, nothing is wrong: both flags are waiting
on the same deploy, so the run reports `[deferred]`. The sweep also archives the
dependents first, so once traffic has drained one run archives the whole chain.

A run with nothing to do says so:

```
flag-cleanup: no outstanding archives — every merged removal in this project is
already archived
```

### Only archive what is deployed

On its own, the sweep archives on the evidence of a merge, and the only thing
between a merge and an archive is Featureflip's refusal to archive a flag it
can still see being evaluated. That refusal reads traffic, so it cannot tell a
dead flag from one whose code is deployed but was not reached recently: a
monthly job, an admin-only screen, a rarely hit error path. For those, an
ungated sweep can archive while a build that reads the flag is still running.

If something in your pipeline points a git ref at what is deployed — a tag
your production deploy moves to the commit it shipped, say — give the sweep
that ref and it archives only removals the ref contains:

```yaml
permissions:
  contents: read         # is the removal's merge commit in each deployed ref?
  pull-requests: read    # which merged removals exist?

# ...

        with:
          mode: archive-sweep
          api-token: ${{ secrets.FEATUREFLIP_ARCHIVE_TOKEN }}
          org: my-org
          project: my-project
          deployed-refs: |
            deployed/web
            deployed/api
```

**A flag is archived only once its removal is in every ref you list.** A flag
key does not say which of your builds reads it, so a removal shipped in the
web bundle but not yet in the API is still being read by the API. A flag some
ref does not contain yet is reported and left alone, and exits `0`:

```
[pending-deploy] old-checkout the removal merged as 4f2c8ab19e01, which the
deployed ref deployed/api does not contain — so the code that reads this flag
is still running. Left outstanding for the deploy that ships it
```

Nothing records that, so the first sweep after the deploy that moves the ref
picks the flag up. The check runs in addition to Featureflip's refusal, not
instead of it: a flag the refs contain can still come back `[deferred]` while
old traffic drains.

**Each ref is resolved by GitHub, not by a checkout**, so the sweep still
needs no `actions/checkout`. Each must be a plain tag, branch or commit sha
(letters, digits and `. _ / + -`); anything else stops the run before it
starts. A ref GitHub cannot resolve — a typo, a tag deleted — is a `[failed]`
line for every outstanding flag and a red run, never an archive. So is a
comparison GitHub will not answer for one flag, which leaves the rest of the
backlog to drain as normal.

**A merged pull request that does not name its merge commit stays
outstanding.** Ungated, the sweep archives it on the merge alone. Gated, there
is nothing to compare, so it is reported `[pending-deploy]` on every run until
you archive the flag by hand.

### Archive when the deploy actually lands

`archive-sweep` acts on the evidence of a merge, with Featureflip's refusal
standing between that and an archive, plus your `deployed-refs` if you set
them. If your pipeline creates **GitHub Deployments**, `mode: archive-on-deploy`
fires on the deploy instead, and
archives only the outstanding flags whose removal that deployment actually
contains:

```yaml
name: Featureflip archive on deploy
on:
  deployment_status

permissions:
  contents: read         # is the removal's merge commit in the deployed one?
  pull-requests: read    # which merged removals exist?

jobs:
  archive:
    runs-on: ubuntu-latest
    steps:
      - uses: canopy-labs/featureflip-flag-cleanup-action@v1
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        with:
          mode: archive-on-deploy
          api-token: ${{ secrets.FEATUREFLIP_ARCHIVE_TOKEN }}
          org: my-org
          project: my-project
          deployment-environment: production
```

**It does not replace the sweep, and does not avoid the deferral.** Featureflip
refuses an archive while it can still see the flag being evaluated, and a
deployment finishes seconds after the build it replaced was serving that flag —
so for any flag with live traffic this mode's ordinary outcome is `[deferred]`
too, and the archive lands later once the old traffic has drained. **Run
`archive-sweep` on a schedule alongside it**; without one, a repository that
deploys weekly defers every removal and never drains.

What this mode adds is the half that refusal cannot do. The refusal reads
traffic your environments have already reported, so it can only rule on
environments it has seen traffic from and lets through a flag nothing has
evaluated — right when the flag really is dead, wrong when the code reading it
is deployed but rarely reached. This mode asks git instead, and git cannot be
vague about a commit: either your deployment contains the removal or it does
not. The two checks fail in opposite directions, which is the whole reason to
run both.

**Then read the two limits**, because they decide whether this mode is worth
wiring up at all:

* **It only helps if something creates GitHub Deployments.** Plenty of pipelines
  do — Vercel, Netlify, `actions/deploy-pages`, Argo and friends via webhooks,
  and any workflow that uses a GitHub `environment:`. Plenty do not, and there
  is no event for this mode to hang on in those. Nothing is lost by not using
  it: `archive-sweep` behaves exactly as it does on its own.
* **It does nothing for clients you cannot update.** Mobile, desktop and
  embedded builds keep evaluating the flag long after your servers stop, and no
  git event corresponds to "the last old version stopped calling us". Deploying
  is simply not the completion event there, which is why those flags stay
  covered by Featureflip's own refusal and the `[deferred]` outcome, exactly as
  described above.

**It asks whether the deployed commit contains the removal, rather than
assuming.** For each flag whose removal pull request merged but which is still
live — the same set `archive-sweep` computes, by the same route — it compares
that pull request's merge commit against the commit the deployment shipped. A
flag whose removal is not in that commit yet is reported and left alone:

```
[pending-deploy] old-checkout the removal merged as 4f2c8ab19e01, which this
deployment (91ee0a7c33d4) does not contain — so the code that reads this flag is
still running. Left outstanding for the deploy that ships it
```

Nothing is written to record that, so nothing has to be cleaned up: the flag
still being unarchived *is* the record, and the next deployment that contains
the merge picks it up — as does the scheduled `archive-sweep` you are running
alongside this.

**A flag GitHub will not answer for is named and turns the run red**, rather
than being folded in with the ones that are merely not deployed yet. A merge
commit orphaned by a history rewrite, two commits with no common ancestor, or a
comparison too large for the API to compute all mean *I could not tell* — which
is not the same as *not yet* and will not clear by itself:

```
[failed] old-checkout could not tell whether this deployment (91ee0a7c33d4)
contains the removal: could not tell whether 4f2c8ab19e01 is contained in the
deployed commit 91ee0a7c33d4; refusing to assume it is (HTTP 422): No common
ancestor. The flag is left outstanding; `mode: archive-sweep` without
`deployed-refs` archives it on the merge alone
```

The rest of the backlog is archived anyway. One flag the API cannot place does
not stop the flags this deployment provably shipped from being archived, and
does not cost you their report lines.

**A deployment to another environment archives nothing**, and says so rather
than passing silently — a workflow that quietly never fires is indistinguishable
from a repository with nothing to clean up:

```
[other-environment] this deployment was to 'staging', not 'production' —
nothing is archived. Set the `deployment-environment` input if that is the
wrong environment to archive on
```

The environment name is matched case-insensitively against the payload's
`deployment.environment`. Any status that is not `success` is likewise reported
and archives nothing — a deployment that failed left the old build running, so
the flag is still being read.

**It needs `GITHUB_TOKEN` and two read permissions**, one more than the sweep.
Like the sweep it asks GitHub which removals merged (`pull-requests: read`), and
it also asks whether the deployed commit contains each of them
(`contents: read`). It writes nothing to your repository.
It needs the same Member-role Featureflip token the other archive modes use, for
the same reason.

**The same quiet/loud split applies.** `archived`, `deferred`, `pending-deploy`
and both no-ops above exit `0`; a flag the API refuses for any other reason is
named on its own line and exits `1` without stopping the rest of the backlog.
`dry-run: true` computes the same set — including the containment check — and
sends nothing.

### Deprecated: archive on merge

`mode: archive-on-merge` is **deprecated and will be removed in the next major
version**. Every run prints a warning saying so; until it is removed it behaves
exactly as before.

It archives the flag on the `pull_request` closed event, the moment its removal
merges, which is the wrong moment. The code is merged, not deployed. Since
Featureflip started refusing to archive a flag it can still see being evaluated,
nearly every run of this mode reports `[deferred]` and leaves the actual archive
to `archive-sweep`. The only flags it still archives on the spot are the ones
nothing has evaluated recently — and those include code that is deployed but
rarely reached, which it archives at the earliest possible moment, before any
deploy has had a chance to land.

**To migrate**, delete the workflow that runs `mode: archive-on-merge` and run
[`archive-sweep`](#archive-the-flag-once-the-removal-is-deployed) on a schedule
instead, adding [`archive-on-deploy`](#archive-when-the-deploy-actually-lands)
if your pipeline creates GitHub Deployments. Keep the same Member-role secret.
The sweep finds every merged removal whose flag is still live, including those
merged while the old workflow was running, so nothing is lost in the switch.

For reference while you migrate, the mode:

* identifies its flag from the pull request's **branch name**, which this Action
  encodes reversibly when it opens the pull request — nothing is parsed out of
  the title or body;
* reports `[not-a-removal-branch]` for a pull request this Action did not open
  and `[not-merged]` for one closed without merging, both exit `0` — declining
  a removal never archives anything;
* needs no `actions/checkout` and no `permissions:` block. Passing
  `GITHUB_TOKEN` with `pull-requests: read` is optional, and only lets a
  `FLAG_HAS_DEPENDENTS` refusal be reported as `[deferred]` when every
  dependent's removal has merged too;
* reads only `api-token`, `org`, `project` and `api-url`, and ignores
  `dry-run`.

### Re-run a removal from a comment

A removal pull request can go stale — the base branch moved on, or somebody
pushed a fix by hand and wants the tool to try again from scratch. `mode:
pr-command` lets a human ask for that directly, by commenting on the pull
request this Action already opened, rather than waiting for the next
scheduled sweep. Like the archive modes it is **opt-in and separate**, in its
own workflow on its own trigger:

```yaml
name: Featureflip flag cleanup — pull request commands
on:
  issue_comment:
    types: [created]

permissions:
  contents: write
  pull-requests: write

jobs:
  pr-command:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: canopy-labs/featureflip-flag-cleanup-action@v1
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        with:
          mode: pr-command
          api-token: ${{ secrets.FEATUREFLIP_API_TOKEN }}
          org: my-org
          project: my-project
          # Must match the scheduled workflow's values — see below.
          directories: src
          languages: ts
```

Two commands, both left as a comment on the pull request itself. The mention
must be followed by whitespace or the end of the line — `@featureflip-cleanuprerun`,
with no space, is not a command, and neither is `@featureflip-cleanup rerun`
buried in a longer sentence; it must stand alone on its own line.

- **`@featureflip-cleanup rerun`** regenerates the removal from the pull
  request's own base branch and force-pushes it over the pull request's
  branch. It refuses if anyone other than this Action has committed to that
  branch since it was opened, so a fix-up commit you pushed by hand is never
  silently thrown away.
- **`@featureflip-cleanup recreate`** does the same regeneration, but
  proceeds even when the branch carries commits this Action did not make —
  **and discards them.** Only comment this once you are sure those commits
  should not survive.

Both commands push under a lease keyed to the branch's current commit, so a
write that lands on the branch between this run reading it and pushing is
**rejected**, never silently overwritten.

Six things to know.

**The flag is identified by the pull request's own branch**, the same
reversible encoding the archive modes read — there is nothing to type in the
comment besides the command itself, and no way to aim a re-run at a different
flag than the one the pull request is already about.

**Only someone with write access to the repository can run either command.**
A comment from anyone else is silently ignored — no reply, exit `0` — for the
same reason the archive modes' no-ops stay quiet: `issue_comment` fires
for anyone who can comment at all, and replying would turn this Action into an
amplifier for whoever types the mention.

**`GITHUB_TOKEN` must be passed through explicitly**, exactly as for the
scheduled workflow above — GitHub does not expose it to a container by
default.

**This mode reads a narrower set of inputs than `remove` does, and getting the
list right matters.** `api-token`, `org`, `project`, `api-url`, `directories`,
`languages`, `accessors` and `ignore` all feed the same transform a scheduled
run uses, so a re-run configured differently regenerates a *different* pull
request under the same title — keep this workflow's values in step with the
one that opened the pull request. `staleness` does NOT belong on that list:
this command looks the flag up by the key encoded in its branch name and
checks both tiers regardless of which one is configured, trying the
configured tier first and falling back to the other — a flag genuinely moving
from `stale` to `dead` while its pull request sits open must still be
findable. So the diff, the draft-vs-ready state and the pull request body are
unaffected by this input either way; the only thing it can change is which
tier name this command's own reply quotes, in the rare case a flag qualifies
under both. Three inputs `remove` reads do nothing here, deliberately: `flags`
scopes an ordinary sweep and is not how you re-run one flag, since the pull
request's own branch already identifies it unambiguously; `max-prs` bounds how
many *new* pull requests a sweep opens, and this mode opens none; and
`pr-labels` only ever applies the moment a pull request is first opened.
`base-branch` is a fourth exception, in the other
direction: this mode always regenerates from the pull request's **own** base
branch, never from a configured one, so a pull request targeting some other
branch than usual is still handled correctly — or refused, if this checkout
never saw that branch at all.

**`dry-run` is honoured here too, but not identically to `remove` mode.** Set
it on this workflow and a comment still runs the whole regeneration — the
guards, the transform, everything — but stops before creating a branch,
force-pushing, or updating the pull request; it replies naming what it would
have done (including how many existing commits the push would have replaced)
and exits `0` as `rerun-dry-run`. Unlike a `remove`-mode dry run, which never
writes to GitHub at all, this one still checks your write access, reads
the pull request, and posts that reply — only the push and the `PATCH` are
skipped. Carrying `dry-run: true` over from the scheduled workflow, out of
habit or caution, previews safely here rather than doing nothing silently,
which is the whole reason this exists: the alternative was a real force-push
with no warning. An empty regeneration wins over a dry run: if nothing in the
configured directories reads the flag any more, the outcome is `rerun-empty`,
not `rerun-dry-run` — there is nothing left for dry-run's own preview to
show once the diff is empty.

**A closed pull request is reopened, then regenerated.** The reopen happens
before the push, because GitHub will not reopen a pull request whose branch
was force-pushed while it was closed. If the push then fails, the pull request
stays open at its previous commit and the reply says so; close it again if you
do not want it. A **merged** pull request is refused outright: the code is
already on the base branch, so there is nothing left to remove.

The command always answers on the pull request itself, and the run closes
with an exit code:

- `0` — the command was answered. This covers a comment this Action ignores
  entirely (not left on a pull request, no recognized command, or from
  someone without write access — all three silently, with no reply), a
  branch this Action never opened (replied to, so you know why nothing
  happened), and every successful regeneration — including one whose diff
  came back **empty** (nothing in the configured directories reads the flag
  any more; nothing is pushed and the pull request is left alone, so it can be
  closed) and including a **dry run** (regenerates and reports, but pushes
  nothing and leaves the pull request untouched).
- `1` — the command was refused, or the regeneration itself failed. Refused
  covers `rerun` finding foreign commits on the branch (comment `recreate` to
  proceed and discard them), the pull request having already merged, the flag
  no longer being a removal candidate in either tier (archived, deleted, or
  brought back into use), and the flag key being in this workflow's `ignore`
  list (`rerun-ignored`) — the same "silence it, do not resurrect it" rule the
  `flags` allowlist is held to. Every one of these replies on the pull request
  saying which; a failed regeneration replies too, with a short summary and a
  pointer to this run's log for the rest. This is also where a pull request
  targeting a base your checkout never saw lands — one pull request's problem,
  not a broken workflow, so it is exit `1` here even though the identical
  refusal is exit `2` for the scheduled `remove` mode.
- `2` — the run could not start. Either the workflow itself is wired wrong —
  for example triggered by something other than `issue_comment` — or the
  checkout is one this Action cannot work in safely: a `directories` entry that
  is not inside a git working tree, entries belonging to two different
  repositories, or **uncommitted changes to tracked files**, which a re-run
  would otherwise commit onto the pull request's branch and then discard when
  it restores your working tree. All of those are checked before anything is
  transformed, committed or pushed, so nothing has been modified when this
  happens, and the reply on the pull request names the offending path.

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
| `mode` | `FEATUREFLIP_MODE` | `remove` | `remove` opens removal pull requests. `archive-sweep` archives the flags whose removal has merged, on a schedule, and reads only `deployed-refs`, `api-url` and `dry-run` from the inputs below — read [Archive the flag once the removal is deployed](#archive-the-flag-once-the-removal-is-deployed) before using it, because merging is not deploying. `archive-on-deploy` archives the outstanding flags a successful deployment shipped, and reads only `deployment-environment`, `api-url` and `dry-run` from the inputs below — see [Archive when the deploy actually lands](#archive-when-the-deploy-actually-lands). `pr-command` re-runs the removal for the flag whose pull request a comment was left on — see [Re-run a removal from a comment](#re-run-a-removal-from-a-comment). `archive-on-merge` is deprecated — see [Deprecated: archive on merge](#deprecated-archive-on-merge). |
| `deployment-environment` | `FEATUREFLIP_DEPLOYMENT_ENVIRONMENT` | `production` | `archive-on-deploy` only. Matched case-insensitively against the payload's `deployment.environment`; a deployment to anything else archives nothing and says so. |
| `deployed-refs` | `FEATUREFLIP_DEPLOYED_REFS` | *(none)* | `archive-sweep` only. Tags, branches or commit shas that stand for what is deployed, one per line or comma-separated. When set, a flag is archived only once its removal is contained in every one of them. See [Only archive what is deployed](#only-archive-what-is-deployed). |
| `api-url` | `FEATUREFLIP_API_URL` | `https://api.featureflip.io` | Override only for a staging/self-hosted instance. |
| `staleness` | `FEATUREFLIP_STALENESS` | `dead` | `dead` (ready-for-review PRs) or `stale` (draft PRs). |
| `languages` | `FEATUREFLIP_LANGUAGES` | every language this Action supports (`ts`, `tsx`, `js`, `php`, `ruby`, `erb`, `dart`, `java`, `go`, `python`, `kt`, `csharp`, `swift`) | Comma-separated. |
| `directories` | `FEATUREFLIP_DIRECTORIES` | `.` | Comma-separated, relative to the checkout. |
| `accessors` | `FEATUREFLIP_ACCESSORS` | *(none)* | Comma-separated function names your own code uses to read a flag, on top of the SDK's, plus any JSX gate components as `Component[prop]` (`FlagGate[flag]`). See [Wrapping the SDK](#wrapping-the-sdk). |
| `inline-wrappers` | `FEATUREFLIP_INLINE_WRAPPERS` | `true` | Kotlin, Java and C#: inline a method, Kotlin property getter, Kotlin top-level function or C# get-only `bool` property whose whole body is one read of the flag at every call its file proves is bound to it, and delete it once nothing calls it. `false` folds the read inside the method and stops there. See [Wrapping the SDK](#wrapping-the-sdk). |
| `ignore` | `FEATUREFLIP_IGNORE` | *(none)* | Comma-separated flag keys to always skip. |
| `flags` | `FEATUREFLIP_FLAGS` | *(none)* | Comma-separated flag keys this run may propose, as an ALLOWlist — leave unset to consider every candidate. Does not re-propose a flag that already has a pull request — to re-run against one whose pull request is already open or closed, comment `@featureflip-cleanup rerun` on it instead, see [Re-run a removal from a comment](#re-run-a-removal-from-a-comment). `ignore` wins when a key is in both. |
| `base-branch` | `FEATUREFLIP_BASE_BRANCH` | the repository's default branch | Base branch each removal PR targets. Unset means "look it up" — see below. |
| `pr-labels` | `FEATUREFLIP_PR_LABELS` | *(none)* | Comma-separated. A label that fails to apply is logged as a warning and does not fail the PR. **May create labels** — see below. |
| `max-prs` | `FEATUREFLIP_MAX_PRS` | `10` | Most pull requests one run may propose (`0` = no limit). See "How much one run can do" below. |
| `dry-run` | `FEATUREFLIP_DRY_RUN` | `false` | See "Dry run" below for `remove` mode; `archive-sweep`, `archive-on-deploy` and `pr-command` honour it too, each differently — see [Archive the flag once the removal is deployed](#archive-the-flag-once-the-removal-is-deployed), [Archive when the deploy actually lands](#archive-when-the-deploy-actually-lands) and [Re-run a removal from a comment](#re-run-a-removal-from-a-comment). |

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

### Flags that other flags depend on

A flag that another live flag lists as a **prerequisite** is not proposed for
removal until that flag is gone. Archiving it earlier would make every flag
that depends on it fail its prerequisite check and serve its off variation, so
the order is always: remove and archive the dependents, then the prerequisite.

Such a flag is reported as `[blocked]`, with the flags it is waiting on:

```
[blocked] checkout-v2 (status=Dead, treatment=True)
    waiting on: web-checkout-v2 (still use this flag as a prerequisite; ...)
```

It costs nothing against `max-prs`, is reported the same way in a dry run, and
is proposed automatically on the first run after its last dependent is
archived. If its removal pull request is already open, the run converts it to a
draft and comments to say what it is waiting on; once nothing depends on the
flag any more, a later run restores it (a pull request that was already a draft
stays one) and says so. That needs no permission beyond the
`pull-requests: write` the Action already uses. `rerun` and `recreate` comments
on such a pull request are refused for the same reason.

A flag whose removal pull request was already **merged** is not waiting for a
proposal, so the line says so instead:

```
[blocked] checkout-v2 (status=Dead, treatment=True)
    waiting on: web-checkout-v2 (still use this flag as a prerequisite). Its removal was already merged in https://github.com/acme/web/pull/41, so it can be archived once they are
```

One that was closed without merging is not proposed again either, and its line
names that pull request instead.

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

This section is about `mode: remove`. `pr-command` also honours `dry-run`,
but not identically — see [Re-run a removal from a
comment](#re-run-a-removal-from-a-comment) for what it does there.

`dry-run` defaults to `false` — if you omit it, the Action opens real
branches, commits, and pull requests on its first run. There is no friendlier
default hiding in the code. Set `dry-run: true` explicitly (as the quick-start
above does) to only compute and print each candidate's diff. A dry run never
writes to GitHub: no branch, no push, no pull request, no comment, no label.

It does still **read**, when the workflow passes `GITHUB_TOKEN` through. Each
flag gets the same lookup a real run makes before anything else, so a flag
whose removal pull request is already open, merged or closed prints
`[already-handled]` with that pull request named, not a diff for a pull
request the next real run would never open. In a repository that has used the
Action for a while this is the most common outcome, so a dry run that could not
see it would mostly be previewing work that will not happen. Without a token the
dry run asks nothing, says so in its log, and previews every diff as if no pull
request existed.

Every refusal a real run can produce is reachable in a dry run and is reported
identically — `[unsafe-key]`, `[piranha-error]` and `[unsafe-rewrite]` all
appear with the same label and the same exit code. That is what makes the dry
run worth reading: it cannot approve a flag that the next real run turns down.
`[blocked]` is too, though a dry run never puts an open pull request on hold.
Only `[pr-opened]` is unreachable.

A `[dry-run]` line with no diff under it says why: either no file in the
configured directories mentions the flag any more, or the files that do (listed
above it) hold nothing the rules can rewrite. Either way a real run opens no
pull request for that flag.

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

`archive-sweep` prints the same shape one flag at a time — `[archived]`,
`[deferred]`, `[would-archive]`, `[failed]`, and `[pending-deploy]` when
`deployed-refs` is set — and closes with its own tally:

```
flag-cleanup: 2 outstanding flags: 1 archived, 1 deferred
```

`archive-on-deploy` prints and tallies identically, adding `[pending-deploy]`
for a flag this deployment does not contain yet. The two deliveries it declines
to act on print one line and **no** tally, deliberately: a run that stopped at
the environment gate never looked at the backlog, and "0 outstanding flags"
would claim it had.

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
    post-transform checks, so it was rolled back **along with every other
    file for that flag** (a half-removed flag is worse than an unremoved one).
    The output names the refused files and which check stopped each one. Most
    are not syntax errors: a common one is a removal that would stop part-way,
    leaving the flag's value bound to a name or still tested. Until they are
    changed by hand — or the key is added to `ignore` — this flag produces
    nothing on every run. It is reported rather than passed off as "no
    changes" precisely so it does not look like a repository with nothing to
    clean up.

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
  - a `deployed-refs` entry is not a plain tag, branch or commit sha. Each is
    compared in a GitHub API path, so one that is not would compare against a
    ref you did not name;
  - **a configured directory has uncommitted changes to tracked files.** The
    run undoes its own edits between flags to keep each PR to a single flag,
    and that undo cannot distinguish your work from its own — so it refuses
    rather than risk discarding it. Untracked files are deliberately allowed
    (build output shouldn't block a run). In CI the checkout is clean and this
    never fires; it exists for local invocations.

The archive modes read the same three codes, with far fewer ways to reach them.
`archive-sweep` reports a list and follows `remove`'s partial-failure rule:
`0` when every outstanding archive
either landed, deferred or (with `deployed-refs` set) is `[pending-deploy]` —
including when there were none, and including a dry run — `1` naming each flag that could not be archived, and `2` when
`GITHUB_TOKEN` was not passed through. `archive-on-deploy` reads exactly like
the sweep, plus `0` for the two deliveries it declines to act on (a status that
is not `success`, a deployment to another environment) and `0` for each
`[pending-deploy]` flag the deployed commit does not contain yet. The
deprecated `archive-on-merge` archives one flag or it does not, so there is no
partial result: `0` for `archived`, `deferred`, and both quiet no-ops
(`not-merged`, `not-a-removal-branch`); `1` for any other API refusal; `2` for a
workflow wired to the wrong trigger.

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

For `remove` and `pr-command`:

```yaml
permissions:
  contents: write        # push the removal branch
  pull-requests: write   # open the PR
```

The deprecated `archive-on-merge` needs **none of this** — it reads the pull
request out of the event payload and makes one API call, so it needs nothing
from `GITHUB_TOKEN` and no `permissions:` block at all. Granting it
`pull-requests: read` and passing `GITHUB_TOKEN` through is optional, and only
lets a prerequisite refusal be reported as `[deferred]` when every dependent's
removal has merged.

`archive-sweep` sits between the two:

```yaml
permissions:
  pull-requests: read    # find each flag's merged removal PR
```

and needs `GITHUB_TOKEN` passed through the step's `env:`. It writes nothing to
your repository. With `deployed-refs` set it also needs `contents: read`, to
compare each merge commit against each ref.

`archive-on-deploy` needs that plus one more, because it also has to ask whether
the deployed commit contains each merged removal:

```yaml
permissions:
  contents: read         # compare the merge commit against the deployed one
  pull-requests: read    # find each flag's merged removal PR
```

It too needs `GITHUB_TOKEN` in the step's `env:`, and writes nothing.

Without both of the write permissions above, `remove` stops at the first flag
with one clear message rather
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

**A wrapper that takes no key is inlined at its callers, in Kotlin, Java and C#.**
A flag is often read once, inside a method, and checked everywhere else through
that method:

```kotlin
class FeatureFlags(private val client: DynamicValuesClient) {
  fun shouldEnableX(userId: String): Boolean =
    client.getBoolean(ENABLE_X, withContext("user_id" to userId), false)
}

// elsewhere
if (featureFlags.shouldEnableX(userId)) newPath() else oldPath()
```

The read inside `shouldEnableX` is folded like any other read. The run then
follows the method to its callers: each call its file proves is bound to this
method is folded too, and once nothing calls the method, it is deleted. Here
that leaves `newPath()` and no `shouldEnableX` at all. With `ENABLE_X` held in
a constant in another file, the constant goes as well (see
[Declared in another file](#declared-in-another-file)).

Two more Kotlin shapes are followed the same way:

- **A property getter**, `val enableX: Boolean get() = read`, read as
  `featureFlags.enableX`. It must be a `val` with only a getter: no
  initializer, delegate, setter or receiver type, and not `expect`,
  `external` or `abstract`. A read that is called (`flags.enableX()`),
  assigned to, taken as a reference (`flags::enableX`) or made with `?.` is not
  folded. A bare `enableX` or `this.enableX` is folded only inside the owning
  class body itself. Inside a companion, a nested or inner class, an object
  literal or an extension it is left alone, as is `this@Owner.enableX`. So is
  a read in the owner's header (an annotation, a constructor default, a
  supertype argument such as `: Base(enableX)`) or in the arguments of a
  `this(...)` delegation, and so is every bare `enableX` in a class whose
  constructor has a parameter of that name, since that parameter wins over the
  property in initializers and `init` blocks. Any mention of `getEnableX` in a Java, Groovy or Scala file
  (a call, a method reference, or Scala's `f.getEnableX` without parentheses)
  is `other-language`. Kotlin reads a Java `getEnableX()` as the
  same property, so one declared anywhere in the repository makes the name
  `name-not-unique`, as does a property or function of that name.
- **A top-level function** in a `.kt` file, `fun shouldEnableX() = read`,
  called bare. The call must reach it the way Kotlin resolves the name: an
  explicit `import pkg.shouldEnableX`, then the same package, then
  `import pkg.*`. Another explicit or aliased import that brings the same name
  from elsewhere keeps the call unfolded. A qualified call
  (`pkg.shouldEnableX()`), a reference (`::shouldEnableX`), type arguments,
  named or spread arguments, and an argument count the function cannot accept
  are not folded either. Neither is a call inside a class or object that
  extends another type (an inherited member of the same name would win), an
  object literal, an extension, or a `.kts` script. An aliased import
  (`import pkg.shouldEnableX as gate`) is reported at the import. A `private`
  function is folded only at calls in its own file. When the function is
  deleted, its explicit imports go with it in the files this run rewrote, if
  each stands on a line of its own. An import sharing its line with other code,
  or an import of it anywhere else, keeps it. Star imports are left alone.

A lambda with a receiver (`with(flags) { enableX }`) is not tracked, for
getters and functions as for methods.

In C#, a wrapper is a `bool` method or a get-only `bool` property of a
non-generic class, struct or record that is not nested in another type:

```csharp
public sealed class FeatureFlags : IFeatureFlags
{
    public bool IsCheckoutEnabled => _client.BoolVariation("old-checkout", ctx, false);
}

// elsewhere
if (_flags.IsCheckoutEnabled) { … }
```

The body is `=> read`, `{ return read; }`, or a lone `get` accessor of either
shape, and the read is `BoolVariation` (or one of your `accessors`).
`GetBooleanValueAsync` is never one: it is folded only under `await`, and an
`async` member returns a `Task<bool>`. A property read is folded as a value:
`!_flags.IsCheckoutEnabled` keeps its `!`. A property read that is called,
made with `?.`, or named in `nameof(…)` is not folded, and neither is a method
used without a call (`Func<bool> f = flags.IsCheckoutEnabled;`). Nor is a read
that also gives a name, an anonymous-object member or a tuple element written
without one (`new { _flags.IsCheckoutEnabled }`, `(_flags.IsCheckoutEnabled, 1)`),
since a literal there does not compile, or a receiver that is a lambda's
parameter, typed or not, since the lambda may be an expression tree. An extension
method, an explicit interface implementation (`bool IFeatureFlags.IsCheckoutEnabled`),
and a member that is `virtual`, `override`, `abstract`, `new`, `partial` or
`extern` are not wrappers this run inlines.

C# differs from Java and Kotlin in three places:

- A public member implements a same-named interface member without any
  keyword. So every base type of the wrapper's class, followed through the
  repository, must be declared in the repository; `class FeatureFlags :
  IDisposable` keeps the wrapper (`overridable`).
- A public interface still qualifies when its project cannot be packed and
  implemented elsewhere: no `IsPackable` (other than `false`), `PackageId`,
  `GeneratePackageOnBuild`, package metadata or `<Import>` in the `.csproj` or
  a `Directory.Build.props` or `.targets` above it, and no `.nuspec` or packing
  step (`dotnet pack`, `nuget push`, `-t:Pack`) anywhere in the repository,
  since that may pack any project. An `internal` interface qualifies either
  way, unless its project names a friend assembly (`InternalsVisibleTo`) and
  can be packed.
- Base types are resolved by namespace, the way the compiler resolves them, so
  a library base that shares a simple name with an unrelated type of yours is
  still outside the repository.
- A receiver's type is resolved the way the compiler resolves it: the file's
  namespace and the namespaces around it, its `using` directives (`global`
  ones, aliases and project `<Using>` items included), and nothing nearer
  claiming the name, such as a type parameter, a nested type, or a namespace
  of that name. A `partial` class, a class with a base type outside the
  repository, or a pattern or `out var` variable of the receiver's name in the
  same member keeps the call unfolded. So does a primary constructor
  parameter that a member of the same name shadows, and a read through a
  verbatim identifier (`f.@IsCheckoutEnabled`).

A Razor view, a XAML file or a Web Forms page mentioning the name is
`unparsed-mention`, as is a doc comment whose `cref` names it, since deleting
the member would leave a broken reference. A VB or F# file mentioning it is
`other-language`. A class in one of those files that implements the interface
(`@implements`, `Implements`, `interface … with`) counts as an
implementation.

Two things are not checked in C#. A property getter in an argument
(`IsCheckoutEnabled(user.Name)`) or in the receiver is a call the inlining
drops, as it is in Kotlin. Code that serializes or binds the wrapper by
reflection with no attribute on it (a view model passed to a JSON serializer)
loses the member when it is deleted. Read the deletion in the diff when the
wrapper's class is also a data object.

A method qualifies when its whole body is one read of the flag being removed
(`= read`, `{ return read }`, `{ return read; }`), and:

- its name, and its class's name, are each declared once across the
  repository's Java and Kotlin files, or across its C# files, where a field,
  event or record parameter of that name counts too (for a getter, a Kotlin function or a
  Java `getX()`/`isX()` of that name counts too, and for a top-level function,
  so does any function, property, class, object or typealias of that name);
- it is not `open`, `abstract` or an override, and nothing in the repository
  extends its class, anonymous classes included;
- if it implements an interface method, that interface is declared in the
  repository, has exactly one implementation there (Groovy and Scala classes
  count), is not public (Java package-private, Kotlin `internal` or
  `private`, or for C# see above), extends no other interface, and is not functional: a lambda can
  implement an interface with one abstract method (or a Kotlin
  `fun interface`) anywhere, so its implementations cannot be counted. Code
  outside the repository can implement a public interface, and inlining would
  override it.

A call is folded only when its file proves the receiver is that method's class:

- a field, property, constructor parameter (a C# primary constructor's too),
  parameter or local declared with the class (or the qualifying interface) as
  its type, or created as `FeatureFlags(…)`, `new FeatureFlags(…)` (also as
  `var x = new FeatureFlags(…)` in C#), `mockk<FeatureFlags>()` or
  `mock(FeatureFlags.class)`;
- `this.field` of such a field;
- the class's name, for a `static` method or a member of an `object` or
  companion object;
- no receiver at all, inside the class itself.

The type must reach the class through the file's own package or its imports.
No argument of a folded call may contain a call or an assignment, because
folding the call drops its arguments.

In Kotlin, a call with no receiver or through `this` is folded only in the
owning class body itself. A companion's method is also folded in the
companion's own body, and, called bare, in the body of an owner that extends
no other type. Inside a nested or inner class, an object literal or another
companion the call is left alone, as is a call in the owner's header or in a
`this(...)` delegation, and one through `this@label`. So is every bare call in
a class whose constructor has a parameter of that name, which wins over the
method in initializers, and a bare call where a local or parameter of that name
is in scope. A call with type arguments, named or spread arguments, or an
argument count the method cannot accept is not folded either, since Kotlin
would bind it to something else.

Stubs of the method in tests are resolved the way stubs of the SDK's read are
(see [Tests that mock the flag read](#tests-that-mock-the-flag-read)): `when(flags.shouldEnableX(any())).thenReturn(…)`,
`doReturn(…).when(flags).shouldEnableX(…)`, MockK's `every { … } returns …` and
their `verify` forms, on a mock the file declares with the class as its type. A MockK stub of a getter
or of a top-level function is not resolved yet, and is reported as `test-double`.
So is a C# stub (Moq's `Setup(f => f.IsCheckoutEnabled)`, NSubstitute's
`.Returns(…)`): the wrapper is kept while one remains.

Anything unproven is left exactly as it was, and the pull request (and a dry
run) names its file, line and reason. The method is then kept, and the removal
is reported as **partial** rather than **complete**:

| Reason | Meaning |
| --- | --- |
| `name-not-unique` | The method, its class or the interface method is declared more than once. |
| `overridable` | The method is open, abstract or an override, or its class is extended. In C#, also `virtual`, `new`, or a class with a base type outside the repository. |
| `interface-published` | The interface it implements is public, or has another implementation. |
| `receiver-unproven` | Nothing in the file ties the receiver to the method's class. |
| `outside-run` | The call, or the interface the method implements, is in a file outside the directories this run rewrites. |
| `import-unresolved` | The receiver's type names the class, but the file's imports do not reach it. |
| `argument-has-call` | An argument holds a call, a lambda or an assignment. |
| `other-language` | The call is in a Groovy or Scala file, in a Java file calling a Kotlin method (or the reverse), in Java, Groovy or Scala naming a Kotlin getter (`getEnableX`), or in Kotlin reading a Java getter as a property (`flags.enabled`), or a VB or F# file naming a C# wrapper. |
| `unparsed-mention` | The name appears in a string (reflection, SpEL), a template, a layout, a Razor view, a C# `cref`, or another file this tool does not parse. |
| `not-folded` | The read inside the method was not folded, because its file could not be processed. |
| `test-double` | A stub of the method this run could not resolve. |
| `annotated` | The method carries an annotation a framework may call it through (`@GetMapping`, `@Bean`, `@JvmName`, …), so it is kept even with no caller left. |

In a partial removal, every call that was folded evaluated to the value the
flag serves. The method that is kept returns that value too, unless it is
`not-folded`, in which case it still reads the flag. Either way, check each
remaining caller the pull request lists before you rely on the method staying.

A method nothing in the repository calls is left as the fold leaves it,
returning the constant, since code outside the repository may still call it.
For the same reason, if your repository is a published library, read the
deletion of a public method in the diff before merging. Set
`inline-wrappers: false` to fold only inside such methods, as earlier versions
did.

**A gate that takes no key and is not inlined is reported, not rewritten.**
That is every such gate outside Kotlin, Java and C#, and one of theirs that
does not qualify, such as a family of gates sharing `IsEnabled` behind
interfaces with a second implementation each:

```csharp
public class FeatureflipCheckoutGate : ICheckoutGate
{
    public bool IsEnabled() => _client.BoolVariation("old-checkout", ctx, false);
}

// elsewhere
if (gate.IsEnabled()) { … }
```

The read inside the gate is rewritten, so `IsEnabled()` becomes `=> true`, but
`gate.IsEnabled()` names no key and is left as it is. The pull request lists
each member the rewrite reduced to a constant under "now returns a constant":
the value it returns, the types it was declared against, any other
implementations of those types, and the files that name one of those types and
call the member. Remove the checks and the gate by hand. The same list covers a
Java or C# member this run did not inline (see "Wrappers not inlined" in the pull
request for the reason).

**A JSX gate component is unwrapped, if you name it.** React code often gates
a whole route or panel with a component instead of a hook:

```tsx
<FlagGate flag="old-checkout" redirectTo="/cart">
  <CheckoutPage />
</FlagGate>
```

The key is a prop here, not a call argument, so no function name reaches it.
Name the component and its key prop in the same input:

```yaml
with:
  accessors: useFlag, FlagGate[flag]
```

When the flag is removed serving `true`, the gate is replaced by what it
rendered: the single child element (`<CheckoutPage />`), moved out to the
gate's indentation, or a fragment (`<>…</>`) around the children when there is
more than one. The gate's import goes too once nothing else in the file uses
it. The key must be a string literal (`flag="old-checkout"` or
`flag={'old-checkout'}`), as for a function.

Served `false`, the flag is **refused**, with the file and line named. What a
gate renders when its flag is off is up to the component (a redirect, a
fallback, nothing at all), and the call site doesn't say which, so the run
won't guess. The same goes for a gate with no children and a gate with a
spread attribute (`{...props}`) after the key prop, which could override it.

A gate inside a **test file** is left alone. A test that renders
`<FlagGate flag="old-checkout">` is nearly always a test of the gate itself,
with the key as fixture data, and unwrapping it would delete what the test is
testing. See below for what happens to the rest of that file.

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

In TypeScript and JavaScript the value can also be a **name**, when that name
is provably a boolean. A test helper often passes the value through a
parameter:

```tsx
function renderChecklist({ flag = true }: { flag?: boolean } = {}) {
  return render(<Provider flags={{ 'old-checkout': flag }}>…</Provider>);
}
```

The entry is deleted when the nearest binding of the name is a parameter, or a
`const`/`let`, that is typed `boolean` or defaulted to `true` or `false` (as a
plain parameter, or as a shorthand property of an object-pattern parameter),
**and** nothing else in that function or block uses the name. That second
condition rejects plenty that would have been safe — any other read, a
reassignment, a re-binding in a nested function — so that the proof needs no
list of the ways a name can be re-bound. Once the entry is gone nothing reads
the binding, which an unused-variable lint rule rejects, so it goes too when
that is safe: a `const` like any other unused local, and a parameter when it
is the last one of a function nothing outside the file can call (not
exported, never passed around as a value) and no remaining call passes it —
often because the one call that did was in a test about the removed branch,
deleted in the same run. Any other parameter is left in place and listed
under "no longer read", because removing it changes the calls that pass it.

**A registry entry is kept while anything still names the key.** The
registry is usually what the key's *type* comes from (`keyof typeof FLAGS`),
and what a lookup by key reads its default out of. If the rewrite leaves any
file naming the key outside a comment (a gate nobody declared, a test that
uses the key as data, a string passed to something that is not a flag read),
deleting the entry would turn that mention into a compile error or a crash.
So the run keeps the entry and lists it under "deliberately left in place",
naming the files that still mention the key. Clean those up and the next run
removes the entry. A comment never keeps an entry, and neither does a
JavaScript or TypeScript test's title (`it('shows the banner when old-checkout
is on', () => …)`): nothing is typed against a title and nothing looks it up.
A file whose only mentions are titles and comments is listed on its own, under
"name this flag only in test titles", so you can reword the titles. Any other
string keeps the entry, even in a test, because fixture data that borrows the
key can still reach a lookup by key.

That covers the registry line, a test's override property, and a type alias or
inline object type beside it (`type Overrides = { 'old-checkout'?: boolean }`)
— in every supported language except Java and Swift, whose map entries wait on
an engine change, and the pull request lists each one under "flag-keyed
entries were removed". An `interface` body is **not** covered yet: the members
look identical, but they are a different node to the rules, so an
`interface Overrides { 'old-checkout'?: boolean }` member is left for you and
listed under "still reference this flag".

Every member position in a `;`-separated object type is covered — first,
middle and last, with or without a trailing `;` — with one exception, below.

**A type's last surviving member is never deleted.** `{}` does not mean "an
empty object" in a type position; it means *any non-nullish value*, so
narrowing

```ts
const render = (flags: { 'old-checkout'?: boolean } = {}) => { ... }
```

to `(flags: {})` would quietly widen the parameter to accept `0`, `""` and
`true`, and most TypeScript lint configurations reject it outright
(`@typescript-eslint/no-empty-object-type`). A cleanup that loosens your types
is worse than one that leaves you a line to delete, so the member stays and
the file is listed under "still reference this flag" — with one exception.
When the type is the **whole annotation of a function parameter**, as above,
it becomes `Record<string, never>`: the type an object with no properties
actually has, and the replacement that lint rule itself suggests. It still
accepts `{}` and the default, and it can still be passed on to a `flags`
prop of any record type. Anywhere else — inside an intersection
(`{ 'old-checkout'?: boolean } & Base`), on a variable, behind a comment —
the member stays, because there the same rewrite would change other types
too. In a *value* position
the same run does empty the object — `{ 'old-checkout': true }` becomes `{}`,
which is an ordinary empty object and the right shape for an override that no
longer overrides anything. In TypeScript and JavaScript the pull request lists
each override it left empty (`<Provider flags={{}}>`), because the prop or
argument carrying it may now be pointless; whether it can go depends on
whether it is optional, which this tool cannot see from one file.

**An entry set to the branch being removed is evidence about the code around
it**, not just leftover residue. If the flag serves `true` and an entry reads
`{ 'old-checkout': false }`, it pins the flag to the branch this removal
deletes. What the run does with that depends on the language.

*TypeScript and JavaScript* tests have titles, and the title says whether the
test is *about* the removed branch. When the nearest enclosing `it`, `test` or
`describe` (or `.only`/`.skip`, `xit`/`fit`, …) has a title naming the flag in
the removed state, the whole test is deleted:

```ts
it('hides the panel when the flag is off', () => {   // <- deleted, with its body
  render({ 'old-checkout': false });
  expect(screen.queryByText(/panel/i)).not.toBeInTheDocument();
});

it('shows the footer even if the flag is off', () => {
  render({ 'old-checkout': false });          // <- override stripped, test kept
  expect(screen.getByRole('contentinfo')).toBeInTheDocument();
});
```

That test asserts behaviour the code can no longer produce, so it would fail
whatever happened to the override. "The flag is off", "with the feature
disabled", "when old-checkout is off" all count. A title that hedges with
*even*, *regardless* or *whether* describes behaviour that holds in both
states, so that test is kept and its override stripped, and so is any test
whose title does not mention the flag at all. Choosing from a title is a
heuristic, so the pull request lists every test it deleted, with its title,
under "tests were deleted". A test it should have deleted and did not fails in
your CI, and one it deleted wrongly is in the diff.

A test with **no override at all** can still be about the removed branch, if
it relies on the flag's default:
`it('hides the tokens link when old-checkout is off (default)', …)`. With no
override to go on, the title has to carry the whole decision, so the bar is
higher. The title must name the flag by its **key**, since "the flag" alone
could mean any flag, and the test must not override the key to the branch
being kept. When both hold, it's deleted and listed like the others.

An override can also reach the literal **through a helper's parameter**:

```ts
const createWrapper = (seatCapOn = true) => ({ children }) => (
  <TestFeatureflipProvider flags={{ 'old-checkout': seatCapOn }}>{children}</TestFeatureflipProvider>
);

it('falls back to a toast when old-checkout is off', () => {   // <- deleted
  render(<Invite />, { wrapper: createWrapper(false) });
});
```

Each call then pins the flag to its argument, or to the parameter's default
when it passes none, and its test is judged by its title exactly as above. The
override, the parameter and the matching argument at every call site are all
removed (`createWrapper(false)` becomes `createWrapper()`). This happens only
when every step can be checked in the file itself: the parameter is the
helper's last, it defaults to `true`/`false` or is typed `boolean`, nothing
else in the helper reads it, and the helper is a `const` or function
declared once, not exported, and only ever called directly with a `true` or
`false` literal or nothing in that position. Anything else, such as a call
passing a variable, leaves the calls and their tests as written. The override
itself still goes when the parameter is provably a boolean, as described
earlier for a value that is a name, and the parameter is then listed under
"no longer read".

**A test file that still hands the key to the code under test is left exactly
as it was.** Stripping a test's overrides, and deleting its tests about the
removed branch, is only right when nothing in the file reads the flag any
more. A test of a generic component that takes the key as data
(`<FlagGate flag="old-checkout">`, `isEnabled('old-checkout')` on a helper you
didn't name in `accessors`) still drives a real read through those overrides.
So when a test file still names the key anywhere other than an override, its
type, a test title or a comment once the rewrite is done, the whole file goes
back as it was. The pull request lists it under "deliberately left in place",
with the line that kept it. Point that test at another flag, and the next run
cleans up the rest.

*Every other language* keeps the entry instead, because its tests carry no
title to read. The pull request lists it under "deliberately left in place",
and you delete the entry or the test around it by hand. A type member
declaring the flag (`'old-checkout'?: boolean`) is kept in any such file too,
so the kept override still type-checks.

The signal is not perfect and is not meant to be: a test can carry an off
override and still be about something else entirely, in which case the entry
really is residue and deleting it is one line of review. That trade is
deliberate — a leftover entry is visible in the pull request, and a test
rewritten into a false assertion is not.

The same rule applies outside tests, because the tool cannot tell the two
apart and the evidence is the same either way. A boolean-valued *registry*
(`{ 'old-checkout': false, 'legacy-banner': true }`) therefore keeps its entry
too, and is reported. A registry whose value is not a boolean —
`{ 'old-checkout': { default: false } }` — is removed exactly as before: the
discriminator is the value position the entry occupies, not the literal
underneath it.

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

becomes `export type Overrides = { // gone soon` with the surviving member
below it — and the same entry one line further down would leave the comment
on the end of `'legacy-banner'?: boolean;`, where it now reads as a note about
a flag that is staying. Nothing is lost, but the comment annotates the wrong
thing; delete or move it in review.

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
| Swift | `let K = "key"`, at file scope, as a `static let` in a struct, class, enum (including a caseless `enum` used as a namespace), actor or extension body, or local to a function. A `var` is **not** — it may be reassigned |

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

**Swift also withholds a file for what its lookup order can hide.** Swift looks
a bare name up in the members of every enclosing type, its superclass and its
protocols before it looks outside them, and those members may be declared in
another file or in a framework. So a file is left untouched when a bare read of
the constant sits inside a type that inherits anything (`class V:
UIViewController`), or inside any `extension`, that does not also hold the
constant's declaration. A type with no inheritance clause of its own counts
when an extension in the same file gives it a supertype (`extension S: P {}`,
or a `where Self:` constraint). An extension in the file that gives a supertype
to anything other than a type the file declares at that exact path (`extension
S` for a top-level `S`, `extension Keys.V` for a `V` declared in `Keys`) counts
for every type in the file: a typealias, a module-qualified name or a type from
elsewhere, such as `extension UIViewController: P {}`. A bare read of a
`static` constant from an instance method, initializer or computed property of
the type whose body declares it is always withheld: Swift does not let an
instance member use the static that way, so code that compiles can only mean
an instance member supplied from elsewhere. So is a read the cleanup cannot
reach, such as one in a type's own body when the constant is declared in an
extension of that type. A file is also left untouched when the constant or its
read is written in backticks (`` `darkMode` ``).

### Declared in another file

A key constant declared in one file and read from another is resolved for
C#, Dart, Java, Kotlin, Swift, TypeScript, TSX, JavaScript, Python and PHP. Other languages keep the
behaviour above: the declaration is left in place and nothing is deleted.

| Language | Read forms resolved |
| --- | --- |
| C# | A `const string` in a class, struct, record or interface, including one nested in another type (`FeatureFlags.Checkout.NAME`, spelled from the outermost type). `Owner.NAME`, with `Owner`'s namespace enclosing the reader or imported by a `using` in the file, in the enclosing namespace block, as a `global using`, or as a `<Using>` item in a project file; `Alias.NAME` under `using Alias = Ns.Owner;` at file level (or `global using`, or a project `<Using Alias="...">`); bare `NAME` under `using static Ns.Owner;` at file level. The read is the first argument of a `BoolVariation(...)` call, of an awaited `GetBooleanValueAsync(...)`, or of an accessor you named. A read spelled with its namespace (`Ns.Owner.NAME`, `global::Ns.Owner.NAME`), a `partial` owner, and a constant declared `static readonly` are left alone, and the declaration is kept |
| Dart | A top-level `const` or `final`, or a `static const`/`static final` member of a class, mixin or extension type. Bare `NAME` through an `import` of the declaring file (a relative URI or `package:` of the reader's own package), `show` and `hide` honoured; `p.NAME` through `import '...' as p`; `Owner.NAME` and `p.Owner.NAME` for a member. The read is the first argument of a non-null-aware `boolVariation(...)` call or of an accessor you named. A read through a re-exporting library, a conditional or deferred import, another package of the repository, a typedef, or from a `part` file is left alone, and the declaration is kept. An import the fold leaves unused is removed, and a `show` naming the deleted constant is trimmed |
| Java | `Owner.NAME`, with `Owner` in the same package or imported (`import pkg.Owner;`, `import pkg.*;`); bare `NAME` under `import static pkg.Owner.NAME;` or `import static pkg.Owner.*;`. The read is the first argument of a `boolVariation(...)` call or of an accessor you named |
| Kotlin | `Owner.NAME` for a `const val` in a top-level `object`, or in the `companion object` of a top-level class (`Owner` is the class; `Owner.Companion.NAME`, or `Owner.Keys.NAME` for a companion named `Keys`, works too), with `Owner` imported or in the same package; bare `NAME` (or its `as` alias) under `import pkg.Owner.NAME`, `import pkg.Owner.Companion.NAME` or `import pkg.NAME`, or in the same package for a top-level `const val`. The read is the first argument of a `boolVariation(...)` call or of an accessor you named. A subclass of the class can read a companion's constant without naming it, so a constant read that way is kept |
| TypeScript / TSX / JavaScript | an `export const` read through `import { NAME }` (or `as`), `import * as ns` (`ns.NAME`), `const ns = require(...)`, or `const { NAME } = require(...)`. Relative specifiers, and `baseUrl` and `paths` from the nearest `tsconfig.json` or `jsconfig.json`, are followed. The read is the first argument of a non-optional `boolVariation(...)` or `useFeatureFlag(...)`, an awaited `getBooleanValue(...)`, or an accessor you named |
| PHP | A single-element `const` in a class, interface or enum declared at the top of a file or namespace block and not `private` (typed or not), or a namespace-level `const`. `Owner::NAME` with `Owner` imported (`use Ns\Owner;`, `use Ns\Owner as Alias;`, `use Ns\{Owner, ...};`), in the reader's namespace, fully qualified (`\Ns\Owner::NAME`), qualified through an imported namespace, or `namespace\`-relative; class names match in any case. Bare `NAME` under `use const Ns\NAME;` or in the constant's own namespace (a global constant only from a file with no namespace). The read is the whole first argument of a `boolVariation(...)` method or function call, or of an accessor you named. Left alone, and the declaration kept: a read through a subclass, an implementer, a `class_alias`, or `self::`/`static::`/`parent::` that may reach it (inside a closure, always), a `use const ... as Alias`, a qualified constant name (`\Ns\NAME`), a named argument, a read inside string interpolation, the name in a string (`constant('...')`), a heredoc or a docblock annotation, a Blade view, a Twig, Latte or Smarty template or Symfony YAML/XML or Nette configuration that spells it, and a file holding PHP under another suffix (`.theme`, `.install`, `.profile`, `.engine`, `.ctp`, `.php7`). A trait's constant is never resolved across files. A file under a Composer `vendor/` directory (beside a `composer.json`) is never rewritten; it keeps the declaration only when it names the owner itself, and it competes only when it parses |
| Python | a module-level `NAME = "key"` (annotated, `NAME: str = "key"`, and a plain f-string with nothing to fill in count too), read through `from pkg.keys import NAME` (or `as K`), a relative form (`from .keys import NAME`), `import pkg.keys` (`pkg.keys.NAME`), `import pkg.keys as k` (`k.NAME`) or `from pkg import keys` (`keys.NAME`, `as` alias included). An absolute import is resolved when exactly one file in the checkout ends in `pkg/keys.py` or `pkg/keys/__init__.py`; with two, as in a repository holding two services that each have a `pkg` package, the read is refused. The read is the first argument of `variation(...)` with a `True`/`False` default (positional or `default=`), of `get_boolean_value(...)`, or of an accessor you named. Only an import at the top of the module counts |
| Swift | A top-level `let`, or a `static let` in a struct, class, enum, actor, or an extension of one declared in the same module, including nested types (`Flags.Checkout.NAME`, spelled from the outermost type). The module of each file is read from `Package.swift` targets and committed Xcode projects (classic file lists and Xcode 16 synchronized folders). Within a module, a static is read as `Owner.NAME` and a top-level `let` as bare `NAME`, with no import; a static is never resolved as a bare `NAME`. From another module, after `import Module` with the constant `public` (or `package` within one Swift package, or `internal` under `@testable import`). The read is the first argument of a non-optional-chained `boolVariation(...)` call or of an accessor you named, negated or not. Left alone, and the declaration kept: a bare `NAME` of a static constant read inside its owner, an extension of the owner, or a type that inherits from it (write `Owner.NAME`), a constant carrying an attribute (`@MainActor`, a property wrapper), a `private` or `fileprivate` constant or owner, one inside `#if`, an owner with an inheritance or conformance clause, a module-qualified read (`Module.Owner.NAME`), an implicit member (`.NAME`), a read through a typealias, a subclass or `self`/`Self`, a read inside an extension of a type that has a generic parameter of the owner's name, or of a nested type whose outer type is generic, a read inside an extension whose extended type the tool cannot name (a generic specialisation such as `extension Array<Int>`), a read inside an extension written through a typealias, or in a type whose supertype or protocol is spelled through a typealias, a dotted owner one of whose intermediate types is also redeclared by an extension (an extension adding `typealias Checkout = …`), a bare read inside a type whose supertype or protocol is declared outside the repository, or inside an extension constrained `where Self:` to such a type (a SwiftUI `View`, say: qualify it as `Owner.NAME`), an owner given a member of the same name by an extension written through a typealias (`extension K`, or a dotted spelling with a typealias anywhere in it, such as `extension NS.Keys`), a constant or read whose name is written in backticks, an import inside `#if` or of one declaration (`import struct Module.Owner`), an `import` of a module name that two packages or projects in the repository share, a second imported module that declares the same name or re-exports another, and any file whose module cannot be read (a manifest that computes its targets or lists `sources:`/`exclude:`, a file in no target). One case is still resolved and can differ: a qualified read (`Owner.NAME`) inside a type whose superclass or protocol comes from outside the repository, inside an extension of a type declared outside the repository (`extension UIViewController`), or inside an extension constrained `where Self:` to one. If that outside type declares, or an extension of it declares, a nested type, typealias or generic parameter with the owner's name, Swift reads that one, and the fold uses this repository's value. Bare reads in such types are left alone |

`.ts` (with `.mts` and `.cts`), `.tsx` and JavaScript (`.js`, `.jsx`, `.mjs`,
`.cjs`) are separate languages, but a file of one can read a constant declared
in another, as a `.tsx` component reading a key from a `.ts` module does. The
run that handles the declaration rewrites those readers too, each with its own
grammar, so the declaration and every read of it go in the same pull request.
Two readers are left alone, reported as `other-language`, and keep the
declaration: one whose language is not in `languages`, and one that another of
these runs would rewrite as well, because it names the flag key itself or also
reads a key constant declared in a file of another type. A Kotlin file reading
a Java constant, or the reverse, is refused as `other-language` in the same
way.

Groovy (`.groovy`, `.gvy`, `.gy`, `.gsh`, `.gradle`, and `.gsp` views) and
Scala (`.scala`, `.sc`, and `.scala.html`, `.scala.txt` and `.scala.xml`
templates) files can read a Java or Kotlin constant too, and this tool neither
rewrites nor parses them, so a mention there is refused as `other-language`
whenever it might be a read: the file names the owner, a subclass of it or an
alias, reads the name bare (a static import or an inherited constant), or
qualifies it with anything other than a class the repository declares, the
file imports or shares a package with, and never uses as a variable. A Spock spec reading `CheckoutGate.FLAG_KEY`
keeps `CheckoutGate`'s constant; one reading `SearchGate.FLAG_KEY` from an
unrelated class does not.

A C# constant can be read from files this tool does not rewrite either: a
Razor view (`.cshtml`, `.razor`), a XAML binding (`{x:Static local:Owner.NAME}`
in `.xaml` or `.axaml`), VB or F# code in a project that references the C#
one, a Web Forms page or control (`.aspx`, `.ascx`, `.master`, `.asax`), a VB
Razor view (`.vbhtml`), a C# or Cake script (`.csx`, `.cake`) or a T4
template (`.tt`). A Razor or XAML file that spells the constant's name
together with its owner (or a class the repository derives from it, or a
global or project alias of it) is refused as `other-language`, and so is a
Razor file that reads the name bare where a `_ViewImports.cshtml`/`_Imports.razor`
above it, or a `global using static`, could bring the owner in. Any of the
other file types that spells the name at all is refused, since the import that
reaches the owner can live where this tool does not read it; VB is matched in
any case, as VB itself does. In each case the declaration is kept and the
pull request names the file.

In a repository with several C# projects, a `global using` in a `.cs` file and
a `<Using>` item in a `.csproj` count only for files of the same project (the
nearest directory holding a `.csproj`), and one in a `Directory.Build.props` or
`Directory.Build.targets` counts for every project below it. An item in any
other `.props` or `.targets` file may be imported by any project, so it can
keep a declaration but never resolves a read. A class of the constant's owner
name in another project is never taken as proof that a read means that class.

A reader whose class is `partial` is judged against every part of it, so a
member or a base class another part declares counts. A part written in markup
(a Blazor component's `.razor` file beside its `.razor.cs`, a XAML window
beside its `.xaml.cs`) cannot be read, so such a reader is left alone and keeps
the declaration. A part produced by a source generator is not seen either.

A C# doc comment that names the constant in a `cref` (`/// <see
cref="FeatureKeys.NewCheckout"/>`) keeps the declaration too: once the constant
is gone that reference is a build error wherever warnings are.

A reader is resolved only when its file can mean nothing else by the name. The
name must not be bound a second time in the reader (a local, a parameter, a
second import, a member of a type the repository declares as its supertype), and
an owner or namespace must not be reachable by another route.

In Python, deleting an import also stops its module loading, and loading a
module runs it. When a reader's `from pkg.keys import NAME` has no read left,
the Action keeps the load. A name beside others is dropped from the list. A
sole name, or every name of the statement, becomes a private alias of the
module, `import pkg.keys as _keys  # noqa: F401` (`from . import keys as _keys  # noqa: F401`
for a relative import), which keeps the load and binds no public
name. A `# noqa: CODE` already on the line gains `F401`. If the alias is
already used in the file, or the line cannot take the mark (it ends in a `\`
continuation, or a `;` precedes a string or another import that runs past
it), that reader is refused as `unsupported-form` before anything is
rewritten, and the declaration is kept. The `import pkg.keys as _keys` this leaves reads nothing,
so it holds whatever `pkg/__init__.py` does.

The `from pkg import keys` and `import pkg.keys` forms take `keys` as an
attribute of the package, so they are resolved only when every `__init__.py`
on the way is plain: imports, literal assignments, `__all__` and `__version__`,
a literal assigned to package metadata (`__date__`, `__license__`,
`__licence__`, `__copyright__`, `__email__`, `__maintainer__`, `__credits__`,
`__status__`, `__url__`, `__title__`, `__description__`, `__summary__`,
`__uri__`, `__author_email__`, `__maintainer_email__`, `__build__`,
`__docformat__`, `__revision__`, `__contact__`), undecorated functions and
classes (a class body may set `__slots__`), and a star import from one
module in the checkout that is itself plain. An ancestor package may also set up a `logging`
`NullHandler`, read its version from `importlib.metadata` (with one `try` for
`PackageNotFoundError`), or set `warnings` filters. Anything else there (other calls, `if`, any `try` but one, `__getattr__`,
`globals()`, a compiled module beside it) keeps the constant and refuses those
readers. A compiled module, an unchecked `.pyc`, or a package directory of the
same name beside the declaring module refuses every form, `from pkg.keys import
NAME` included.

Some Python routes to a constant are not inspected: a module reached only
through `sys.modules` or an import path built at run time; a constant's name
built at run time in a file that never spells it (`setattr(pkg.keys, "NA" +
"ME", ...)`, `pkg.keys.__dict__.update(...)`); another module, or
a function called later from code the Action does not read, that assigns a
package's submodule attribute at import time; an override module kept out of
version control (a git-ignored `local_settings.py` that assigns
`pkg.keys.NAME`), which the Action takes for build output;
identifiers spelled with characters Python normalises to the name (fullwidth
letters, say); and source hidden behind an unusual coding declaration.

Python mentions that keep the declaration: a string whose whole text is the
name (an `__all__` entry, `globals()["NAME"]`), which applies to removal
inside the declaring file too; any string that names the constant as a word,
in any Python file of the checkout, a docstring and a
`mock.patch("pkg.keys.NAME")` included; `globals`, `vars`, `locals`, `exec`,
`eval`, `sys.modules` or `importlib.import_module` or `__import__` in a
reading file; and a star import beside a bare name.

A Python constant can be given another value after its declaration, so the
declaring file is held to more than the in-file rules above before any read
of it in another file is resolved. It must bind the name exactly once,
anywhere in the file and in any scope: a second assignment, an `if` branch
assigning it, `global NAME` in a function, `del`, a `for`, `with`, `except`,
walrus or comprehension target, a parameter, a `def` or `class` of that name,
an import of it, or `pkg.keys.NAME = ...` through the module imported into
itself are each refused as `rebound`. It must also hold no star import and
nothing that reaches its own namespace without spelling the name: `globals`,
`vars`, `locals`, `exec`, `eval`, `setattr`, `getattr`, `__dict__`, a frame's
`f_globals`, a function's `__globals__`, a module `__getattr__` or any other
dunder outside the ones that only describe a module, `importlib`, `builtins`,
or `sys.modules`; each is refused as `unsupported-form`. Both are reported at
the declaring file, on the line that causes them, and the declaration is
kept with every read of it. Some of these findings cannot change the value
inside the file itself: a `def` of a dunder (a `__repr__` method, a module
`__getattr__`) and a `getattr` of a literal name, which reads an attribute.
When those are a file's only findings and no other file in the checkout, in
any language, spells one of its key constants, nothing elsewhere reads them,
so the file is left to the in-file rules alone and a read inside it folds as
it would if no other file existed. Everything else, including a second
binding, a star import, `exec`, `globals()` or a computed `getattr`, keeps
the declaration even with no reader elsewhere.

**Python has no partial close-out.** Another module can assign the constant
(`pkg.keys.NAME = "other-flag"` at start-up), and that write cannot be told
from a harmless mention without a list of hazards, which is never complete.
So a refusal anywhere that keeps a Python declaration keeps every read of it:
each reader that would have been folded is listed as `declaration-kept` at
its first mention of the constant, nothing in it is rewritten, and each
other declaration it reads is kept too. In C#, Java, Kotlin and TypeScript,
whose constants cannot be reassigned, the proven reads are still folded.

Every file in the git checkout that spells the constant's name is checked,
whether or not it sits under the directory the run transforms, a submodule's
included. The exception is a file git ignores and nobody committed, such as
`dist/` or `build/generated/` output. The next build rewrites it and the pull
request can never contain it, so its mention keeps nothing. It is still read
for what it declares: a supertype or a second declaration of the constant
that only generated code holds still counts against the files that are
checked. A generator that writes the constant's name from input this Action
does not read, such as a template or a schema, brings that read back on the
next build. Commit its output, or add the flag to `ignore` until the
generator's input no longer names the constant.

#### Enum entries

A key held in an enum entry is resolved too, in Dart, Java, Kotlin, PHP, Python and TypeScript:

```java
public enum Flags {
  NEW_CHECKOUT("new-checkout");
  private final String key;
  Flags(String key) { this.key = key; }
  public String key() { return key; }
}

client.boolVariation(Flags.NEW_CHECKOUT.key(), ctx, false);
```

| Language | Entry | Read forms resolved |
| --- | --- | --- |
| Dart | a public top-level enhanced `enum` with exactly one constructor, a `const` one, that stores the parameter holding the key in a `final` field declared without an initializer (`this.key`, `{required this.key}`, or `key = k` in its initializer list); the entry passes the key as a plain string and has no type arguments | `Flags.X.key` when the field is public, or a public getter or method with no parameters whose whole body returns the field (`Flags.X.flagKey()`); `p.Flags.X.key` through `import ... as p`. The enum's own field wins over a mixin's or an extension's member of the same name, so neither stops the read. A dot shorthand (`.X`, anywhere in the checkout), `.name`, `.index`, `case Flags.X:` and a read inside the enum's own library are not resolved (`unsupported-form` or `not-a-read`) |
| Java | a top-level `enum` with exactly one constructor that assigns the parameter holding the key to a `final` field, and never writes that parameter (`key = key.trim();` makes the entry `enum-unproven`); the entry has no body | `Flags.X.key()` through a non-private method whose whole body is `return key;` (any name), or `Flags.X.key` when the field is not private; bare `X.key()` / `X.key` under `import static pkg.Flags.X;` or `.*` |
| Kotlin | a top-level `enum class Flags(val key: String)` with no secondary constructor; the entry passes the key positionally and has no body | `Flags.X.key`; bare `X.key` under `import pkg.Flags.X`, or `Y.key` under `import pkg.Flags.X as Y`. Only an alias of the entry's own import counts: `F.X.key` under `import pkg.Flags as F` is not resolved (`unsupported-form`) |
| PHP | a string-backed `enum Flags: string` declared at the top of its file or of a braced namespace, with `case X = 'key';` as a plain string | `Flags::X->value` (or the import alias, a fully qualified `\Ns\Flags::X->value`, or `Flags::X->value` in the enum's own namespace). `?->value`, `->name` and methods of the enum are not resolved (`unsupported-form`); a read inside the enum's own file is not resolved either, nor is a Twig, Latte or Smarty template or Symfony YAML (`!php/enum`, an `enum('...')` expression) or XML configuration that names the case |
| Python | a top-level `class Flags(StrEnum)`, `(str, Enum)`, `(Enum)` or Django `(models.TextChoices)` whose body holds only members (literals, `auto()`, or for `TextChoices` a `"key", "Label"` or `"key", _("Label")` pair), a docstring and public methods, with no metaclass and no decorator but `@unique`; the member is `X = "key"` as a plain string | `Flags.X.value` through `from pkg.flags import Flags` (or `as F`), `import pkg.flags` (`pkg.flags.Flags.X.value`), `import pkg.flags as m` (`m.Flags.X.value`) or `from pkg import flags` (`flags.Flags.X.value`). A bare `Flags.X` passed as the key is not resolved (`not-a-read`): a `(str, Enum)` member formats as `Flags.X`, and a wrapper receiving the member may read `.name` or `.value` from it. `.name`, `.label` and methods are `unsupported-form`, as is a read inside the enum's own file |
| TypeScript / TSX / JavaScript | an `export enum` (or `export const enum`) declared once in its file, with `X = "key"`; or an `export const Flags = { X: "key" } as const` object (see below) | `Flags.X` (or the alias's text) through `import { Flags }`, `const { Flags } = require(...)` or `const { Flags: F } = require(...)` (read as `F.X`); `ns.Flags.X` through `import * as ns` or `const ns = require(...)` |

An entry holds the key when its arguments (or a TypeScript member's
initialiser) spell the key as a whole token: `"new-checkout-v2"` is another
flag's key, while `PREFIX + "new-checkout"` still holds this one and makes the
entry `enum-unproven`.

In TypeScript, a property of an `as const` object is treated as an enum entry
throughout, so everything this section says about an enum applies to the object
too, and the pull request reports the property as an enum entry:

```ts
export const Flags = {
  NewCheckout: "new-checkout",
} as const;

client.boolVariation(Flags.NewCheckout, ctx, false);
```

The object must be an `export const` at the top of its file, written
`{ ... } as const` (`as const satisfies T` is fine), and contain only plain
`Name: value` properties. The property holding the key must have an unquoted
name that appears once in the object, and its value must be the key as a plain
string. A spread (`...Base`), a computed key (`[k]: ...`), a shorthand
property, a method, a getter, a `__proto__` property or a name written with an
escape (`EnableX`) can add, replace or hand out a property the run cannot
see, so any of them makes every entry of the object `enum-unproven`, and so
does a `let`, an object that is not exported, or a type annotation on the
constant (`Flags: Record<string, string>`), which lets other files write the
property. A property whose name appears twice is `enum-unproven` as well, and
so is an enum member whose name appears twice. An
object without `as const` (including `Object.freeze({ ... })`), and one declared
inside a function, is not resolved at all.

The reads are folded and **the entry is left in place**. Removing an enum
entry can change stored values (a Java ordinal, a PHP backing value read back
with `from()`) and break lookups by name or value or iteration over the enum,
and no build catches any of that. So the pull request lists
each entry whose reads were all folded as safe to delete once nothing depends
on it, and each entry with a read it could not resolve as left in place, with
those reads listed beside it. An entry with a competing declaration
(`name-not-unique`) is listed as left in place too, with the competing
declarations beside it. Which one a reader gets is the build's choice, so no
read through it was resolved, and those reads are not listed: find them by
hand.

An entry passed around as a value (`isEnabled(Flags.X)`) is how most code
wraps an enum flag, and the read inside that wrapper never names the entry. So
any mention of the entry that is not one of the reads above keeps that file's
reads in place and is listed, exactly as for a constant. That includes a bare
entry used as a Java `case` label (`case X:` / `case X ->`) or as a Kotlin
`when` branch condition. In TypeScript any other spelling of the entry
(`Flags["X"]`, `(Flags).X`, `Flags!.X`, `(Flags as T).X`, `const { X } = Flags`,
a nested `require` destructure, or another member of the enum initialised
from it, `Legacy = X`) is listed as `unsupported-form`. In Java and Kotlin a
use of the whole enum can reach the entry too: `values()`, `entries`,
`valueOf(...)`, a method or function reference to one of them
(`Flags::valueOf`), a class literal, `enumValues<Flags>()` or
`enumValueOf<Flags>(...)`, whether the owner is spelled bare, qualified
(`app.Flags.values()`) or through an import alias, and, in Java only, a bare
`values()` or `valueOf(...)` inside the enum itself. In PHP the same goes for
`Flags::cases()`, `Flags::from(...)` and `Flags::tryFrom(...)` (also as
`self::` inside the enum), a dynamic `Flags::{$name}` fetch, `Flags::class`
and a string naming the enum (`$cls = 'App\Flags\Flags'; $cls::from(...)`).
In Python the same goes for `Flags(...)`, `Flags[...]`, iterating or counting
the class, handing it on (`register(Flags)`), `Flags.__members__`, and
Django's `Flags.choices`, `.values`, `.labels` and `.names`. In Dart it is
`Flags.values` (and so `values.byName(...)`, `values.asNameMap()` and
iterating it), also as a bare `values` inside the enum. Such a use does
not stop the removal, but it is listed under `enum-dynamic` so you can check
it before archiving the flag.

A Python enum member's value cannot be reassigned through its class, but it
can be through the member's `_value_`, through an attribute set on the class
(`Flags.value = property(...)`), and the class itself can be replaced in its
module. So each of these is listed as `unsupported-form` at its line, and no
read of that enum's entries is cleaned up anywhere (each reader is listed as
`declaration-kept`):

- a spelling of `_value_`, `_member_map_` or `_value2member_map_` in any
  Python file, including one that never names the enum;
- an assignment or `del` of the class's module attribute
  (`app.flags.Flags = ...`) or of an attribute of the class, and a
  `setattr`/`delattr` call on the class;
- a `setattr`, `delattr`, `patch` or `patch.object` call naming the class in
  a string (`monkeypatch.setattr(app.flags, "Flags", ...)`,
  `mock.patch("app.flags.Flags")`).

The enum's own module handing the class to other code (`prefixed(Flags)`,
`REGISTRY = {"flags": Flags}`) makes its entries `enum-unproven` for the same
reason; calling, subscripting, iterating and annotating with it does not. A
member of the same name read through any other qualifier (`self.X`,
`other.X`) is listed as `unsupported-form`, unless the qualifier is a class
another module (or the enum's own module) defines itself, imported directly.
A reader's `from app.flags import Flags` that the cleanup leaves unused is
kept and marked `# noqa: F401`, never rewritten: the class still exists, and
another module may import it from that reader.

A TypeScript or JavaScript enum is a value that any code holding it can read
every entry of, so its reads are cleaned up only when every import of its file
in the repository reads members directly (`Flags.X`, `ns.Flags.X`) or uses it
as a type. Re-exporting it, for example through a barrel file
(`export { Flags } from "./flags"`), passing it on as a value
(`Object.values(Flags)`, `use(ns.Flags)`), looking it up by a computed name,
loading its file any other way (`module.require("./flags")`,
`jest.requireActual("./flags")`), a test mock whose factory is handed the real
module (`vi.mock("./flags", async (importOriginal) => ...)`), a direct `eval`
or a `with` statement in a file that imports it, or any other use leaves every
entry of the enum in place, and the file responsible is named. A mock with no
such factory (`jest.mock("./flags", () => ({}))`, Vitest's typed
`vi.mock(import("./flags"))`) is not a use. A string or glob that could load the enum's
file (`import.meta.glob("./*.ts")`, Astro's `Astro.glob("./*.ts")`,
`require.context("./")`) is treated as a
use of it. A pattern starting with `/` is read from the bundler's root, which
could be any directory above the enum's file, or one holding a symlink into
it. A `base` option moves where a `./` pattern starts, and a pattern starting
with `**` can match any file. A specifier computed at run time is a use too,
read as the glob a bundler makes of it: `` import(`./pages/${p}`) `` or `require("./pages/" + p)` counts
only when the enum's file is under `./pages`, the directory its literal text
names. A computed specifier with no literal text in front (`require(path)`), or
`require` used as anything but a call (`const r = require`,
`(0, require)(...)`, `const c = require.context`), can load any file and
counts everywhere. A specifier computed for `module.require`,
`jest.requireActual` or `vi.importActual` is read the same way, and any use of
`createRequire`, whose result loads files under whatever name it is given,
counts everywhere too. The scripts of a Vue, Svelte or Astro file, and an
Astro file's frontmatter, are judged the same way, except that such a file is
never rewritten, so a read of the flag's entry there (`Flags.X`) leaves the
entries in place as well, and so does the enum's name anywhere in its script
outside an import of the enum's file that can be followed, such as through an
import that cannot be resolved or an auto-import with none written. Its markup
is not parsed: a name its script binds to the enum or its file leaves them in
place wherever the markup spells it, hyphenated too (`:feature-flags` for
`featureFlags`), as do Svelte's `$Flags` and a Vue `<template src>`. So does markup that imports a
module (`{#await import("./flags")}`) or holds a specifier that could name the
file (`from "./flags"`). Other text in the markup does not count. A file whose
scripts cannot be told apart from its markup leaves the entries in place
wherever it mentions the enum or its file.
The reads that could be resolved are still folded.

A key held in an enum in any other language, or in a form outside the table,
is not resolved. The pull request names the file and the entry rather than
reporting a bare "still references this flag", and asks you to resolve the
reads through it by hand before deleting the entry.

Only a mention that can reach the declaration counts. Anything else is
another constant that happens to share the name, such as a sibling class's
own `FLAG_KEY`, and is ignored:

* a file in a language that cannot name the constant at all is ignored: a
  TypeScript or Python file beside a Java one, say. Java and Kotlin can name
  each other's constants, so each judges the other's files;
* in Java and Kotlin, a bare name counts only when something in its file can
  tie it to the declaration. That can be an import of it, a star or static
  import from the owner or from a repository class that may extend it (a
  library class such as `java.util.Objects` cannot), the same package for a
  Kotlin top-level constant, a Kotlin lambda or extension receiver around it,
  or a supertype path from a type in the file to the owner;
* in TypeScript and JavaScript, a name the file binds itself (a local, a
  parameter, its own `export const`) is ignored, and so is a named import from
  a module that declares that name itself. A destructuring, a name bound
  nowhere, or an import from a module that might re-export the constant
  still counts;
* `Other.NAME` is ignored when `Other` can only be a repository type that is
  neither the owner nor extends it: no variable, field or static import of
  that name in the file, and no Kotlin top-level property or typealias of that
  name anywhere. A qualifier the repository does not declare still counts.

Supertype paths are followed through the repository's Java and Kotlin types.
A file the Action could not read, or a Java file it could not parse, counts as
a possible path. A Kotlin file it could not fully parse (newer syntax its
grammar lacks, such as context parameters) counts only where it can matter: as
a step on a path when it names one of that path's types, and as a receiver's
member when it spells the name being looked up. Every
mention that counts has to be a read this run removes. Any other keeps the
declaration, and the declaring file's own reads of the constant stay with it.
It also drops that reader's reads and is listed in the pull request with its
reason:

| Reason | What triggers it |
| --- | --- |
| `name-not-unique` | the same qualified constant is declared more than once, or another file declares a competing one: the same name or owner in another source set, build variant or module, or in the other JVM language (a Java `Owner` beside a Kotlin `Owner`). A Kotlin top-level property named like the owner (`val Owner = ...`) or a `@file:JvmName("Owner")` facade counts too. Which declaration a reader gets is then decided by the build, so none is touched. The pull request lists these apart from the other reasons, since a build variant is usually deliberate |
| `import-unresolved` | nothing in the reading file ties the name to the declaring file: no import or same-package route, an import that resolves to another file or to none, a TypeScript default import, a re-export through a barrel (`export ... from`), a TypeScript import alias of the constant off a name that is not a namespace import of its file (`import K = m.NAME` under `import m = require(...)`), or a TypeScript enum taken off a name that is not a namespace import of its file (`use(m.Flags)`, `const F = m.Flags`, `import F = m.Flags`) or used as a value through an import that does not resolve (`use(Flags)`), or, in a file that names that enum, a computed lookup (`m[k]`) off a namespace or default import of a script that does not resolve, such as a package (`Icons[name]`; a `.css` or `.json` import is not a script); a Python star import beside the name, an absolute import that two files in the checkout could mean, or a `from pkg import keys` or `import pkg.keys` through a package `__init__.py` that is not plain or with a compiled module beside the declaring one, or a PHP read whose `use` is written after it, a class the checkout does not declare (such as a `class_alias`), or a global constant read from inside a namespace, or a Dart import that re-exports the declaring library, is conditional or deferred, names another package of the repository, or does not resolve, or a Swift read from another module with no plain `import` of the declaring one, an access level the import does not allow, or a second imported module that declares the name |
| `not-a-read` | the name is used for something other than a flag read: a log line, a map entry, a callable reference (`Owner::NAME`), a TypeScript type-only import, an import left with no read to remove, or a TypeScript enum taken as a value off a namespace import of its file (`use(ns.Flags)`) |
| `rebound` | the reading file binds the same name itself, or an in-repository supertype supplies a member of that name (in Swift, also an enclosing type, an extension of it, or an extension of a protocol it conforms to, declaring a member or generic parameter of the read's first name); or a Python declaring file binds its constant a second time |
| `other-language` | the constant is mentioned, in a way that can reach it, from a file in another JVM language (Java reading a Kotlin constant, Kotlin reading a Java one, or a Groovy or Scala file reading either), from a Razor, XAML, VB or F# file reading a C# constant, from a Twig, Latte or Smarty template or Symfony YAML or XML configuration reading a PHP constant or enum case, or a read of it that would be resolved sits in another file type of the family than the declaration (a `.tsx` or `.js` file reading a `.ts` constant) and is not rewritten with it: its language is not configured, or another run rewrites the file |
| `unsupported-form` | a shape outside the table above, such as a fully qualified `pkg.Owner.NAME` or a `?.` qualifier; the name inside a string or string template; a Kotlin named argument; a renaming `require` destructure (`const { NAME: X } = require(...)`), whose pattern would still name the constant after the read is folded; any Java or Kotlin file that spells the name or its owner but cannot be read or parsed; a mention that can reach the constant from a file outside the directory the run transforms, which is never rewritten and so keeps the declaration alive; a `.kts` script; for an enum entry, a nested enum, a quoted TypeScript member, a read of an entry inside the file that declares it, a proven accessor called with anything but an empty argument list (`Flags.X.key(arg)`; a call that is not an accessor, such as `.name()`, is `not-a-read`), a Kotlin read through an alias of the enum's import (`F.X.key` under `import pkg.Flags as F`), a TypeScript spelling of the entry outside the table (`Flags["X"]`, `(Flags).X`, `Flags!.X`, `(Flags as T).X`, `const { X } = Flags`), a TypeScript enum taken off anything but a name (`use(require("./flags").Flags)`, `let F = require("./flags").Flags`), or any other use of a TypeScript enum's file or of the enum that hands it on (see Enum entries); a Python import of the declaring module nested in a function or a `try`, or a sole-name import whose private alias is already used in the file (or sits beside a submodule of that name in a package `__init__.py` reader), or a relative one through a package `__init__.py` that is not plain, or one whose line cannot take a comment; a Python declaring file with a star import or a route to its own namespace; a Swift file whose module cannot be read from `Package.swift` or an Xcode project |
| `declaration-kept` | Python only: a read of the constant that would have been folded, left as it is because another refusal listed beside it keeps the declaration it reads. A Python constant can be reassigned, so a kept declaration keeps every read of it (see the note on partial close-out above). For an enum entry, a read left because its file also reads a kept constant, or because an escape listed beside it can change the member's value (see Enum entries) |
| `enum-unproven` | an enum entry holds the key, but its accessor cannot be proven to return it: more than one constructor, a constructor that writes its own parameter, a non-`final` field or a Kotlin `var`, an entry with a body, a named or computed argument, a TypeScript enum that is not exported directly, is `declare`d or merges with another declaration of its name, a PHP enum that is not string-backed (pure or `int`), a PHP case whose value is not the key as a plain string (concatenated, interpolated or a constant expression), a PHP enum file that does not parse, a Python enum whose class is nested, has another base, a metaclass, a decorator other than `@unique`, a `value`, `name` or `label` method or one whose name starts with `_` (`__init__`, `__new__`), `_ignore_`, or a member valued by code, a Python enum its own module hands to other code, a Python member whose value is not the key as a plain string, or a Python enum file with a star import or a route to its own namespace, a Dart enum with more than one constructor (a factory or a second generative one), a field with an initializer or assigned twice, an argument that is not the key as a plain string (interpolated or concatenated), the key at a position the constructor does not store, type arguments on the entry, no public accessor of the field, a private enum or entry, or a Dart enum in a part or in a file that does not parse |
| `enum-dynamic` | not a refusal: a Dart, Java, Kotlin, PHP or Python use of the whole enum that can reach the entry without naming it. Listed so it can be checked before the flag is archived |
| `kept`, `left`, `left-competing` | not refusals: what happened to an enum entry, reported at its own declaration. `kept`: every read through it found was folded. `left`: a refusal above names it, and those reads stay. `left-competing`: only a competing declaration (`name-not-unique`) names it, so no read through it was resolved, and those reads are not listed |

One refusal in a file drops every read in that file, so a file is either
rewritten whole or left alone. After the rewrite the Action checks each reader
again. If a proven read, or an import naming the constant, survived the rewrite,
the whole flag is refused: every file it rewrote is restored, so that flag
produces no diff at all until the file is changed by hand or the key is added to
`ignore`, and a warning says which file.

Two things are not inspected, and a mention that only they could affect is
still treated as proven:

* a supertype or receiver declared outside the repository (a library base class,
  the receiver of a library lambda), whose members could shadow an imported name;
* bundler aliases and build configs other than `tsconfig.json` and
  `jsconfig.json` (a webpack or Vite `resolve.alias`, say). A config that
  `extends` another, or two files a specifier could mean, is not resolved at all
  and the mention is refused.

## Tests that mock the flag read

A test that mocks the SDK says, in its stub, which branch it exercises. Once
the read is folded those lines describe a call that no longer happens, so a run
that folds a read of the flag also resolves the mocks of it, and the pull
request lists every line and every test it deleted:

| In a test | What happens |
| --- | --- |
| A stub returning the **kept** value — `when(<read>).thenReturn(true)` when the flag serves `true` | the stub is deleted. The test stays, and runs against the branch that stays |
| A stub returning the **removed** value | **the whole test is deleted**, with any comment directly above it. Its assertions describe the branch this pull request deletes, so they can no longer pass. The pull request names each one, under a warning |
| A verification of the read — `verify(client).boolVariation("KEY", …)`, `client.Verify(c => c.BoolVariation("KEY", …), …)`, `Received().BoolVariation("KEY", …)` | deleted, whatever the test is about: it asserts a call that no longer exists |

Recognised spellings, each with the key as a literal, a constant declared in the
same file, or inside an equality matcher (`eq("KEY")`, `gomock.Eq("KEY")`), and
with any name in `accessors` counting as a read:

* **Java** — Mockito `when(…).thenReturn(…)`, `doReturn(…).when(mock).<read>(…)`
  and `verify(mock[, times(n)]).<read>(…)`; BDDMockito `given(…).willReturn(…)`.
  A test is a method annotated `@Test`.
* **Kotlin** — MockK `every { … } returns …` / `coEvery`, `verify { … }` /
  `coVerify`; mockito-kotlin `whenever(…).thenReturn(…)`. A test is a function
  annotated `@Test`.
* **Dart** — `package:mockito` `when(…).thenReturn(…)`, `verify(…)` and
  `verifyNever(…)`, and the same three in `package:mocktail`'s lambda form,
  `when(() => …)`. Either library's stub may answer with
  `thenAnswer((_) => true)` (or `async =>`) instead of `thenReturn`. A test is
  a `test(…)` or `testWidgets(…)` call.
* **TypeScript / JavaScript** — ts-mockito `when(…).thenReturn(…)` /
  `thenResolve(…)` and `verify(…).once()` and its siblings. A test is an `it(…)`
  or `test(…)` call (and their `.only`/`.skip`/`x`/`f` spellings), never a
  `describe`.
* **Go** — gomock `m.EXPECT().BoolVariation(…).Return(…)`, with or without
  `Times`/`AnyTimes`. A test is a `func TestX(t *testing.T)` or one `t.Run`
  subtest.
* **C#** — Moq `Setup(c => c.<read>(…)).Returns(…)` / `ReturnsAsync(…)` and
  `Verify(c => c.<read>(…), …)`; NSubstitute `sub.<read>(…).Returns(…)`,
  `Received()` and `DidNotReceive()`. A test is a method carrying `[Fact]`,
  `[Test]` or `[TestMethod]`.

The decision is taken only from the stub's own literal, so these are left
exactly as they are:

* a stub returning anything but `true` or `false` — a variable, a parameter;
* a parameterised test (`[Theory]`, `[TestCase]`, `@ParameterizedTest`, a
  TestNG `dataProvider`, …), whose stub may be one row of a table;
* a test that stubs both values;
* a stub of the removed value outside any single test — a setup method, a
  constructor, a `beforeEach` — since there is no one test to delete (a stub of
  the kept value there is still deleted);
* every line of a test that also holds a mock of the read in a spelling not
  listed above.

**Nothing in a test is touched unless the same run folds a real read** of the
flag. If no read could be rewritten — the key held in a field, say — the mocks
are still true and stay. That makes the configuration matter: a `directories`
entry is transformed on its own, so tests listed as a separate entry from the
code they test never see that code's read fold, and their mocks are not
resolved. Keep the two in one entry.

What is left over is handled as it always was. In C# a Moq stub survives the
removal and is listed in the pull request. In Java, Kotlin, Dart, TypeScript and
Go the fold would rewrite a leftover stub into `when(true)` or `true.Return(…)`,
so **the whole flag is refused** — see "What is deliberately NOT rewritten".

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

A `const` bound to an awaited read is inlined like any other binding the fold
leaves holding a literal: `const enabled = await client.getBooleanValue(…)`
and the `if (enabled)` under it fold away together.

**A local the fold turns into a literal is inlined too, not only one bound to
the read itself.** `const show = on || user.enrolled` becomes
`const show = true` once `on` is folded, and the run then inlines `show` into
its uses and folds those in turn, so `{show && <Panel />}` becomes
`<Panel />`. It does this only for a `const` declared inside a function or
block, holding a single binding on its own line, with its name bound nowhere
else in the file and every use an ordinary expression after it. Anything else
— `{ show }` shorthand, `typeof show`, a top-level `const`, a second binding
of the name — keeps `const show = true;` for you to inline by hand. A `const`
you had already set to a literal yourself (`const DEBUG = false`) is never
touched.

When a fold makes a following statement unreachable — collapsing an `else if`
into an `else`, turning `if (<read>) { return a; }` into a bare `return a;`, or
turning a loop condition into `while (true)` — that statement is **deleted** and
listed in the pull request body. `no-unreachable` is part of
`eslint:recommended` and is an error, so leaving it would open a pull request
that cannot go green. A `break` that exits the loop keeps what follows
reachable, and nothing after it is touched. A loop the fold makes *dead* rather
than infinite (`while (false) { … }`, from a flag serving false) is removed
and listed too — ESLint's recommended set does not require it, but the loop
can never run, and the same rule holds in every language. See
[Known gaps](#known-gaps).

A local variable left with no remaining use after a fold is cleaned up the same
way an import is — except one level inside a `const Component = () => { ... }`
function. An arrow function has no name of its own for the grammar to expose
(only the constant it happens to be assigned to does), and the check that
scopes a variable to its one enclosing function needs a name to match the code
from before the fold against the code after — so a local declared inside one is
always left standing rather than guessed at. In a codebase built out of
arrow-function components, that is most functions.

A binding at the **top level of a module** is cleaned too — the usual
`const client = OpenFeature.getClient();` above the functions that read the
flag. It needs no name to match against, because a file has exactly one top
level and nothing to confuse it with. What makes this safe is that the file is
a module: it has an `import`, an `export` or a `require`, so a top-level `const`
in it is reachable from nowhere else and this tool can see its whole scope. A
file with none of those is a plain script whose top-level `const` is shared
with every other script on the page, and its bindings are left alone. Exported
bindings are left alone in either case.

Deleting a whole import statement can also stop its module from loading in
this file. That matters when loading the module does work, such as creating a
client or registering a handler. So a statement whose last binding the fold
stranded is cut down to `import "./ff";`, which keeps the load and binds
nothing, rather than deleted. TypeScript's import elision does not make
deleting it safe: once the read is folded the name is unused, so an eliding
compiler would drop the whole statement, while the code before the change read
the name and so loaded the module. A `const { client } = require("./ff")`
becomes `require("./ff");` for the same reason. The pull request body names
each `import` and `require` kept this way, with the exact line, so a reviewer
can delete the ones whose module does nothing when it loads.

The statement is deleted outright in three cases, because none of them loaded
anything to begin with: an `import type`, an import whose names the file only
ever used as types, and an import of a module that another import in the same
file still loads. A value `import client = require("./ff")` whose last use was
folded is left as it is, because no single spelling keeps its load under every
compiler setting. ESLint's `no-unused-vars` will report it.

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

An awaited read — `if await client.variation(…)`, the shape an async wrapper
produces — is folded together with its `await`, since `await True` would raise
`TypeError` the first time it ran. A read bound to a name that is later
re-assigned (`on = <read>` and then `on = on and eligible(ctx)`) is refused
rather than half-removed: the name cannot be inlined, and leaving `on = True`
behind would delete the flag key while the guard stays. The run output says
so for that file.

A branch of any size is folded, re-indented to the `if`'s own column — as in
every other language here, though in Python it is the difference between a file
that runs and one that does not. The indentation of a multi-line string is
never changed — those lines are data.

A local or an import that only the removed branch used is cleaned up too.
Ruff's default rules include F401 (unused import) and F841 (unused local
variable), and `ruff check` exits non-zero on either, so leaving them would
open a pull request that a project linting with ruff cannot merge.

A local in a function whose value is a literal (a number, a string that is
not an f-string, `True`, `False`, `None`, or a tuple, list, set or dict of
those) is deleted, along with a comment on the same line. Any other value
stays, and so does ruff's report on it. CPython frees an object as soon as
nothing refers to it, and the local is what refers to it until the function
returns. Deleting `tmp = tempfile.TemporaryDirectory()`, or keeping only its
call, would remove the directory before the code after it runs. Some literal
locals are left alone too:

* assignments in a class body or at module level, which other code can read
  as attributes;
* targets that are not a single name (`a, b = …`, `self.x = …`);
* a name assigned more than once in the function: `lock = None` can be what
  lets go of an object an earlier assignment made;
* every local of a function that calls `locals()`, `vars()`, `dir()`, `eval`
  or `exec`, which read locals without naming them
  (`render(request, "home.html", locals())`);
* the only statement in a block (`if ready:` needs a body);
* a statement that shares its line with another (`n = 3; go()`), or that the
  line above continues into with `\`;
* a function's first statement when a bare string follows it, since that
  string would become the docstring.

**An import is never deleted.** It is kept as it was and marked
`# noqa: F401`, which is what ruff and flake8 read to accept it. Importing a
module runs it, which can register a handler or configure a client. Even
dropping just the unused name is not safe: `from app import legacy` is the
only thing that loads `app.legacy` when `legacy` is a submodule, and nothing
in one file can tell a submodule from an attribute. The mark goes at the end
of the statement's first line, after any comment already there, because both
linters read an import's noqa from that line. A parenthesised import is marked
after its `(`. An import whose first line runs on, through a `\` continuation
or a string that opens on that line, is left as it is, because a comment
there would break the code or land inside the string. So are an import that
already carries a noqa, a star import and `from __future__`. The pull request body lists every
import marked this way, so you can delete the ones whose module does nothing
when it loads.

A name inside a string still counts as a use. This covers a forward-reference
annotation (`flows: "list[Ledger]"`) and an `__all__` entry, as well as a
name a comment mentions.

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

A `private` method the fold leaves with no caller is deleted too, along with a
doc comment directly above it — typically the helper a gate class used to build
the evaluation context for its reads. Java gets the same treatment. Only a
method nothing outside its file can reach qualifies, and only when the fold is
what stranded it: an explicit `private` (never `private protected`, nor a
member whose privacy is implied), in a type that is not `partial`, carrying no
attribute or annotation, with no other method of the same name in the file,
and named nowhere else in the file — a string counts, since reflection names a
method by string. A private method the file already had no use for is left
alone. Fields and constructors are never touched, so a client field the gate
no longer reads stays, along with the constructor that sets it.

A primary-constructor parameter is the one place that costs a build. When
every read of `client` in `class Svc(IClient client)` was a flag read the fold
removed, the parameter is left unread and the compiler warns CS9113, which is
an error under `TreatWarningsAsErrors`. The parameter stays, since removing it
changes the constructor every caller and dependency-injection registration
uses, and the pull request names each one in a warning of its own so you can
remove it or suppress the warning. A parameter that was already unread before
the fold is not listed. Neither is one of a `record`, whose parameters become
properties, or of a `partial` type, whose other parts may still read it.

Tests that mock the read — a Moq `Setup` or `Verify`, an NSubstitute stub —
are resolved along with it: see "Tests that mock the flag read". A `Setup`
that cannot be resolved is left in place, and the pull request lists those
test files in their own paragraph, naming each line that still describes the
read to a mock.

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

A read that is the body of an **arrow function** folds inside it, so
`items.where((item) => client.boolVariation('KEY', defaultValue: false))`
becomes `items.where((item) => true)`. That includes a lambda passed as a
named argument, one bound to a local, an `async` one, and a read behind `!` or
`&&` at the end of the body. Earlier versions replaced the whole lambda with
the literal (`items.where(true)`), which does not compile. One nesting is
refused instead of rewritten: a read ending a lambda that is itself the body of
another lambda (`(a) => (b) => <read>`). No grammar available to this Action
parses that shape reliably, so the run exits 1 naming the file.

When folding an `if`/`else` makes a following statement unreachable, that
statement is **deleted** and listed in the pull request body. Dart is one of
the languages where this happens (the others are Java, JavaScript and
TypeScript): `dart analyze` reports dead code as a warning but **exits
non-zero** on it, so leaving it would open a pull request that cannot go
green.

A local or an import that only the removed branch used is cleaned up too, for
the same reason: `dart analyze` reports `unused_local_variable` and
`unused_import` as warnings and exits non-zero on them. A local whose
initializer cannot have a side effect is deleted; one that calls, constructs or
awaits something is rewritten to keep that as a statement
(`final legacy = LegacyFlow();` becomes `LegacyFlow();`). Left alone: locals
inside an anonymous closure, declarations binding more than one name, `late`
locals (their initializer only runs when read), and a local with a declared
type whose initializer is not a literal (`final int id = json['id'];` is a
cast that can throw, and removing it would lose the throw).

An import goes when nothing it binds is spelled any more. What it binds is read
from the imported library, so this works for relative imports and for your own
package's `package:` imports, and never for `dart:` libraries or another
package, which are always left in place:

* `import '…' as legacy;` binds its prefix;
* `import '…' show A, B;` binds each shown name, so `show A, B` can lose `A`
  and keep `B`;
* a plain `import '…';` (or one with `hide`) binds every public name the
  library declares or exports.

Every form also binds the members of every extension the library brings in,
because Dart applies an extension through any import of it and a call such as
`context.l10n` spells only the member. A library with an extension operator or
a `call` member is left in place, since using those spells no name at all.
Imports in a library that has `part` files are never removed: a part shares its
library's imports, and nothing here reads the part to see what it uses. Every
removal is listed in the pull request body.

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

A local that only the removed branch read is cleaned up too. RuboCop's default
`Lint/UselessAssignment` fails on it, and `ruby -w` warns. In a method, a local
holding a literal (a number, `nil`, `true`, `false`, a string without
interpolation, a symbol, or an array, hash or range of those) is deleted,
along with a comment on the same line. One that calls something keeps the
call: `flow = LegacyFlow.new(ctx)` becomes `LegacyFlow.new(ctx)`. Some locals
are left alone:

* a method's last statement holding a literal, since that value is what the
  method returns, including before a `rescue`, `else` or `ensure` (a call is
  still kept, which returns the same value);
* locals in a block, a lambda or a class body, and at the top of a file;
* every local of a method that uses `binding`, `local_variables` or a
  string `eval`, by name or as a symbol (`ERB.new(template).result(binding)`
  and `send(:binding)` read them all);
* a value that is neither a literal nor a call with a receiver, arguments or
  a block: a bare name (which may be a method or a local), a constant (which
  can autoload a file, and which RuboCop flags when it stands alone), an
  interpolated string, a heredoc, an operator, an index, or a hash using the
  `key:` shorthand, which reads `key`;
* a write other than `name = value` (`||=`, `+=`, `a, b = …`);
* a statement sharing its line with another (`limit = 3; go`).

A `require` is never removed: it runs the file it names.

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
the read — which is inlined and its declaration removed. A class property is
inlined and removed only when it is `private`; any other property keeps its
declaration as the literal (see "What is deliberately NOT rewritten").

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

A local or an import that only the removed branch used is cleaned up too, the
same way as in Java and TypeScript. Current `kotlinc` and ktlint's standard
rules don't fail a build on either, but detekt and the IDE flag both, and a
removal that leaves them behind isn't finished. A local whose initializer
cannot have a side effect is deleted. One that calls
or constructs something is rewritten to keep the call as a statement:
`val legacy = LegacyFlow(context)` becomes `LegacyFlow(context)`. Some locals
are left alone:

* locals inside a lambda, an anonymous function, an object expression, an
  `init` block or a property accessor;
* destructuring declarations and delegated locals (`val x by lazy { … }`);
* a local with a declared type whose initializer is not a literal.
  `val user: User = javaApi()` checks a Java value for null and throws on it,
  and removing the declaration would lose that throw.
* a local whose initializer can run code but is not a plain call
  (`items[0]`, `x!!`, `-x`, a string template, a cast);
* a local that is the last statement of an `if`, `when` or `try` block,
  because that statement is the block's value.

An import goes when the name it binds (`import a.b.C` binds `C`) is spelled
nowhere in the file any more. A read inside a string template or in
backticks still counts. Some imports are never removed: star imports, aliased
imports (`import a.b.C as D` also hides `C` from the star imports, so removing
it could change what `C` means), and imports of a name Kotlin calls by
convention without spelling it, such as `getValue` (used by `by`), `plus`
(used by `+`), `invoke` or `component1`.

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

A `private` method the fold leaves with no caller is deleted with its Javadoc,
under the conditions described for C# in [Supported C# flag-read
shapes](#supported-c-flag-read-shapes): no annotation, not `native`, no other
method of that name in the file, and named nowhere else in it.

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

The `php` rule set covers `.php`, `.phtml`, `.inc` and `.module`. Files under a
Composer `vendor/` directory (one beside a `composer.json`) are never
rewritten; see "What is deliberately NOT rewritten".

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

A `use` that only the removed branch needed is deleted, along with a comment
on the same line. A `use` runs nothing; it only says how a name is spelled.
A name still written anywhere in the file keeps its `use`, in any letter
case, because PHP class and function names ignore case. That includes a
qualified name starting with the alias, an attribute, a docblock (`@var
Ledger`) and a string. A group (`use App\{A, B};`) or a list
(`use A, B;`) is left as it is, and so is every `use` in a file with more
than one `namespace` block.

A variable that only the removed branch read is deleted when the value
assigned to it in a function or method is a literal: a number, a boolean,
`null`, a string without interpolation, or an array of those with integer or
string keys. Any other value stays. PHP destroys an object as soon as nothing holds it, so deleting
`$token = new Lock();` would run its `__destruct` at once rather than at the
end of the function. Variables are also left alone when they:

* are at the top level of a file (an including file shares them) or in a
  closure or arrow function;
* are in a function that also uses `compact()`, `extract()`,
  `get_defined_vars()`, `eval()`, `include`/`require` or `$$name` in any
  spelling, all of which reach variables without naming them;
* are assigned more than once in the function: `$lock = null;` can be what
  lets go of an object an earlier assignment made;
* are a superglobal such as `$_SESSION`;
* are written any way but a plain `$name = value;` (`$a = &$b`, `$a += 1`,
  `$a[] = 1`, a property);
* share a line with another statement, or are the unbraced body of an
  `if`, `else` or `declare`.

## Supported Swift flag-read shapes

`client.boolVariation("KEY", default: value)` (swift SDK). The key is the first
argument and is matched as a literal; the fallback is the labelled `default:`
argument and its value is not constrained. Any receiver works — `client`,
`self.client`, `flagProvider`, or a longer chain such as
`Featureflip.shared.client`.

Folded away entirely in `if` / `else` (including an `else if` chain, where the
surviving branch keeps its own braces and a dead last branch takes its `else`
with it), under `!` negation, in a `&&` or `||` operand, in a `guard … else`
and in a ternary.

A read bound to a `let` is inlined, and the `if useLegacy` reading it is folded
the same way.

When the bound name is bound a second time in the same function (a closure
parameter, a `for` variable, an `if let`, a nested function), it is not
inlined, so `let useLegacy = client.boolVariation(…)` becomes
`let useLegacy = true` and the `if useLegacy` reading it stays. The flag key is
gone, which is what makes the flag removable, but you are left with a line or
two to tidy by hand. That is a limit of the transform engine's Swift cleanup,
not a safety refusal.

A read used as a `while` condition: served false, the loop can never run, so
it is removed and listed in the pull request body, as in every language (see
[Known gaps](#known-gaps)). Served true it becomes `while true`, a loop you
meant to keep running, and it stays. Code after that loop is left alone too:
Swift's compiler only warns that it will never be executed, and statements are
deleted for being *unreachable* only where a language's standard build rejects
them.

A key hoisted to a `let` constant is resolved, and the declaration removed with
the last read of it — see [The key hoisted to a constant](#the-key-hoisted-to-a-constant),
which applies to Swift on the same terms as every other language.

A local that only the removed branch used is deleted when its value is a
literal: a number, a boolean, a string without interpolation, or an array or
tuple of those. `swiftc` warns "initialization of immutable value 'x' was never
used", which fails a build that treats warnings as errors. A comment on the
same line goes with it.

Any other stranded local stays, and so does its warning. A `let` keeps the
object it holds alive until the end of its scope, so deleting
`let token = Guard()` or `let observer = owner`, or rewriting it to
`_ = Guard()` as the compiler suggests, would release the object early. If that
object's `deinit` cancels a subscription or releases a lock, the code after it
would then behave differently. Locals are also left alone when they:

* are in a closure, a getter or setter, an observer or a subscript (only a
  `func`, an `init` or a `deinit` is cleaned);
* are stored properties, including one of a type declared inside a function;
* have a type annotation (`let x: Money = 3` can run your
  `init(integerLiteral:)`);
* are `lazy`, `weak`, `unowned` or `async let`, carry a property wrapper, an
  observer or a computed body, or bind more than one name (`let a = 1, b = 2`,
  `let (a, b) = pair()`);
* hold an interpolated string, which calls `description`, or a dictionary,
  which traps on a duplicate key;
* share their line with another statement (`let n = 3; go()`), or are the only
  statement of a `case`. Neither can be deleted without breaking the code.

Imports are never removed. `swiftc` does not report an unused one, and an
import can provide an extension, a conformance or an operator that the file
uses without ever naming the module.

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
| A key hoisted to a constant in a DIFFERENT file, in a language other than C#, Dart, Java, Kotlin, Swift, TypeScript, TSX, JavaScript, Python or PHP, or read in a shape outside [Declared in another file](#declared-in-another-file) | no change to the reader, and the declaration is **kept**. The pull request lists each such mention with its reason |
| A C# read inside an **expression tree** (`mock.Setup(c => c.BoolVariation("KEY", …))`) | no change to the read — the lambda *describes* the call instead of performing it, so the text of the read is the payload a mocking library reads at run time. This one is not detected by naming `Setup`/`Verify`: the tell is that the read's receiver is the lambda's own parameter, so any library taking an `Expression<>` is covered, while a genuine `Func<>` lambda (`list.Where(x => flags.BoolVariation("KEY", …))`, reading off a captured client) is still rewritten. If the key is a `const`, the whole const path is withheld for that file — the mock setup is a live reference to the declaration. Once the run has folded a real read of the key, a `Setup` returning a literal and a `Verify` are resolved first, before this applies (see "Tests that mock the flag read") |
| A read handed to a **mocking DSL** that takes the call itself — `when(<read>).thenReturn(…)` (Mockito, mockito-kotlin, `package:mockito`, ts-mockito), `given(<read>).willReturn(…)` (BDDMockito), `every { <read> } returns …` (MockK), or `when(() => <read>)` / `verify(() => <read>)` (`package:mocktail`) | when the run folds a real read and the stub returns a literal, **resolved first**: the stub or its whole test is deleted (see "Tests that mock the flag read"). Otherwise **the whole flag is refused**, and the run exits 1 naming the file. These libraries stub by *performing* the call and capturing the invocation it registers, so the text of the read is the payload — the same property that makes the C# row above untouchable. Folding it leaves `when(true).thenReturn(true)`, which **compiles**: the stub silently stops stubbing, and the flag key is gone from the file so no later run looks again. Unlike C#, there is nothing structural to key on — no lambda exists, because the call is real — so the guard is a short list of stubbing entry points by name. mocktail's `() => <read>` form is the exception to the first point: the lambda is there but only wraps the call. Delete or rewrite those lines yourself, scope the run with `directories` to exclude your tests, or add the key to `ignore` |
| A **Go read inside a gomock expectation** (`m.EXPECT().BoolVariation("KEY", ctx, false).Return(true)`) | resolved first in the same way as the row above, when it can be. Otherwise **the whole flag is refused**, and the run exits 1 naming the file. Same shape as the row above, caught a different way: the fold leaves `true.Return(true)`, and because Go's `bool` is a builtin with no method set, a selector on a boolean literal is broken Go *by construction* — so this one needs no list of library names. Before this was refused it shipped, and the customer's build was what reported it |
| A **Java field or Kotlin property** bound to the read that is not `private` (`public final boolean enabled = <read>`, a bare `val enabled = <read>`) | read replaced, **declaration kept** → `public final boolean enabled = true;` / `val enabled = true`. Other files read it (`checkout.enabled`), and they do not contain the flag key, so this tool never sees them. Removing the field would break files it never parsed. A `private` one is inlined and removed. In Java that holds only for a top-level class: a `private` field of a nested, local or anonymous class is kept the same way, because the enclosing class can read it (`settings.on`). Delete the field yourself once its readers are gone |
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
| A **Java, Go or C# read that is a whole statement** and holds **another call** — `verify(client).boolVariation("KEY", ctx, false);` (Mockito), `client.boolVariation("KEY", buildCtx(), false);`, `new Ctx()`, `contexts[index()]`, Go's `<-ready`, or C#'s `ctxs[i++]` | **the whole flag is refused**, and the run exits 1 naming the file — except a mock verification of the read, which is deleted when the run folds a real read (see "Tests that mock the flag read"). Deleting the statement would delete that call with it — the mock's assertion, or a context the customer builds — and replacing only the read leaves `true;` standing alone, which none of the three accepts (javac: `not a statement`; `go build`: an unused constant expression; C#: CS0201). Delete or rewrite that line yourself, or add the key to `ignore`. A discarded read holding **no** other call is rewritten: see the row below |
| A **Java, Go or C# read that is a whole statement** and holds no other call — a warm-up call made for the SDK's exposure event, `client.boolVariation("KEY", ctx, false);` | **the whole statement is deleted.** Its value is discarded by definition, so there is nothing to fold it into and nothing to lose. Any receiver works, including a static import with no receiver and a field chain; the argument list may hold names and literals (and, in Go, composite literals, which have no constructors to run). The key may be a literal or hoisted to a constant — see the row below. In C# the awaited OpenFeature read is covered too, while an un-awaited one is left alone; in Go the OpenFeature `client.Boolean(ctx, "KEY", …)` read and in Java the OpenFeature `client.getBooleanValue("KEY", …)` read are covered the same way. A project's own wrapper named in `accessors` is covered the same way, whether the key is a **literal** or hoisted to a constant |
| A discarded read whose **key is hoisted to a constant** (`client.boolVariation(FLAG_KEY, ctx, false);`) | **the whole statement is deleted**, and the declaration with it, exactly as for a literal key. The constant may be declared at file scope or local to the method, and must meet the same four conditions as any other hoisted key — see [The key hoisted to a constant](#the-key-hoisted-to-a-constant). A statement holding another call is refused here too: the row above applies unchanged, because the safety rule does not depend on how the key is spelled |
| A **PHP template written in the alternative syntax** (`<?php if (<read>): ?> … <?php else: ?> … <?php endif; ?>`) | **rewritten**, markup and all: the arm that runs stays, the guard and the dead arm go, and a `<?php … ?>` pair left bracketing nothing goes with them. The rendered HTML keeps the same markup; only insignificant whitespace around the removed lines moves, because the whole `<?php … ?>` line is taken rather than an empty tag pair left behind. `elseif` chains are handled in the same way, in any position |
| A **multi-way guard** — Go's tagless `switch { case <read>: … }`, Ruby's subject-less `case` / `when <read>`, Kotlin's subject-less `when { <read> -> … }`, or `switch (true) { case <read>: … }` in PHP, TypeScript and JavaScript | **rewritten.** Each arm is a condition and the first true one wins, so this is an `if`/`else if` chain by another spelling. Where the flag serves **false** the dead arm is removed, in any position. Where it serves **true**, an arm with nothing live above it collapses the whole statement to its body (Go and Ruby), and an arm with a live arm above it becomes the terminal `default:` / `else`, taking the arms below it — they sit behind a guard that now always passes. Kotlin collapses only where that cannot change meaning — an expression body in a slot that takes any expression, or a braced body in statement position that declares nothing — and otherwise takes the `else` form, which also keeps a `when` used as a value exhaustive. PHP, TypeScript and JavaScript always take the `default:` form, because a body ending in `break;` is a syntax error outside a switch, or silently becomes a loop's `break` inside one. A switch on a **value** (`switch ($v)`, `case v`, `when (v)`, `switch (v)`) is untouched: there the arms compare values rather than act as guards |
| A multi-way guard the fold **cannot** finish | **the whole flag is refused**, and the run exits 1 naming the file. In Go: a `fallthrough` anywhere in the switch, a `break` in a body that would be lifted out of it, or a `switch v := f(); {` initializer whose binding the arms use. In Ruby: a `when` listing several patterns (`when <read>, isAdmin(ctx)`), where a literal among alternatives is not the whole condition. In PHP: an arm before the flag's that does not end in `break`/`return`/`continue`/`goto`/`exit`, because PHP falls through **by default** and that arm runs into the flag's with nothing in the source saying so — an arm ending in `throw` or `die()` counts as not terminating, since the grammar cannot tell either from an assignment. In TypeScript and JavaScript: the same fall-through rule, except that `throw` does count as terminating (it is its own statement there), as does a braced arm body ending in a jump. In Kotlin: an arm listing several conditions (`isAdmin(ctx), <read> ->`). In Kotlin, TypeScript and JavaScript: a dead arm sharing its line with another arm |
| A PHP **`elseif` that is not last in its chain**, where the flag serves **true** | **rewritten**: the clause becomes the chain's `else`, and every branch after it is deleted. That is not tidying — those branches sit behind a guard that now always passes, so they are unreachable, and leaving them would keep an `else` in front of them, which does not parse. Both spellings (`elseif` and `else if`) behave the same. Where the flag serves **false** the dead branch is removed, in any position |
| A PHP read **inside string interpolation** (`"mode={$client->boolVariation('KEY', $ctx, false)}"`, or the same in a heredoc) | **no change** to that read: PHP's `{$...}` needs a variable after the brace, so a literal cannot take the call's place, and folding it would print the text `{true}`. Other reads in the file are still rewritten, the pull request reports the file as still mentioning the flag, and a key constant that read uses is kept |
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
     entry whose siblings are not flags and whose value is not a boolean, the
     last surviving member of a TypeScript object type (see "Wrapping the
     SDK"), a Java `Map.of` or Swift dictionary entry (not yet). Each is a
     line you have to delete yourself;
  4. **the mention is a flag-keyed entry the run deliberately kept**, because
     its value is the branch being removed (see "Wrapping the SDK"; never in
     TypeScript or JavaScript, where it is stripped or its test deleted). The
     body explains this cause in its own paragraph, naming each entry.

  Two kinds of cause 3 are sorted out of the generic list, each into its own
  paragraph. A **C# test that still stubs the removed read** (a Moq `Setup`
  that "Tests that mock the flag read" could not resolve) comes first, naming each such line, because it is the one entry that can turn
  the pull request red: an assertion beside it about what the flag returned may
  now fail. A file that names the flag **only in comments** comes last, as
  prose to update rather than code that can break. Both are positive verdicts
  reached by parsing the file; anything this cannot decide stays in the generic
  list.

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
  indistinguishable from a real read, and is rewritten** — unless it is one
  "Tests that mock the flag read" resolves first, an NSubstitute
  `sub.BoolVariation("KEY", …).Returns(true)` returning a literal in a run that
  folds a real read. The expression-tree
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
  is where the line sits: a stub is resolved or refused because the *text* of the call is
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
  cannot tell its own strands from yours. In C#, Go, Kotlin, PHP and Swift
  the statements after a folded `return` are removed as part of the fold
  itself (C# and Kotlin would otherwise warn about them, which fails a build
  that treats warnings as errors). Those removals show in the diff like the
  rest of the fold, but are not listed separately in the PR body. Python and
  Ruby leave them in place.
* **A loop the fold makes dead is deleted in every language, and the PR says
  which.** A flag read as a loop condition, served false, leaves
  `while (false) { … }` (Go's `for false { … }`, Ruby's `until true`), a loop
  that can never run its body. It is removed and listed with the statements
  above. Left standing, because each runs or matters: a `do … while (false)`,
  `repeat … while false` or Ruby `begin … end while false` (the body runs
  once); a labelled loop (in C# and Go a `goto` can target the label); a loop
  that is not directly in a block, such as an unbraced `if` body or (outside
  Ruby, where an empty `when` is valid) a `switch` arm; a Python
  `while False: … else: …` (the `else` runs); and a `for` loop with an
  initialiser, which still runs (Java refuses that flag instead; see its
  section). A Python loop that was its block's only statement
  becomes `pass`. As with unreachable statements, a file that already
  contained a dead loop is left alone.
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

  One narrower case of the same shape is also left standing, for the same
  reason: a binding at the top level of a file that is **not a module** — no
  `import`, no `export`, no `require` anywhere in it. A top-level `const` there
  is shared with every other script on the page, so its readers can live
  outside this file exactly as a parameter's callers do, and one file is not
  enough to prove it unused. The same binding in a module IS cleaned up (see
  [above](#supported-jsts-flag-read-shapes)); so is
  TypeScript's `import x = require("…")`.

  A top-level binding whose initializer is a **call** is rewritten to just that
  call — `OpenFeature.getClient();` where `const client = OpenFeature.getClient();`
  stood — rather than removed. This is not a gap so much as a deliberate half
  measure: the call may do work the rest of the file still depends on, and this
  tool will not decide for you that it does not. It satisfies `no-unused-vars`,
  and it is the same treatment the equivalent local inside a function has always
  had. Delete the leftover line yourself if the call was only ever there to
  produce the binding.
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
* **A Composer `vendor/` tree is never rewritten either** — a `vendor/`
  directory beside a `composer.json`. `composer install` puts a dependency
  back, so a cleanup there would be reverted, and a team that commits
  `vendor/` would get a dependency edited in its pull request. A key constant
  or enum case declared in a dependency is still read through: the
  application's reads of it are folded, the declaration stays, and an enum
  case there is never listed as safe to delete. A dependency that still names
  the flag is reported as a surviving reference.
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
  reference replaced with the literal, and the expression around it is then
  folded exactly as it would have been had the read been written inline —
  `use_legacy or other` becomes what `client.variation(…) or other` becomes,
  in every position. But only when the name belongs to
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
