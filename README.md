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
whether that flag's removal branch ever produced a pull request *merged into the
base branch*. A removal retargeted to some other branch and merged there does
not count, since the base branch still reads the flag. The base branch is your
`base-branch` input, or the repository's default branch when that is unset — so
if your cleanup workflow sets `base-branch`, set the same value here. It never
writes anything to your repository, and needs no `actions/checkout`.

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
  — including any pull request from a fork, whatever its branch is called —
  and `[not-merged]` for one closed without merging or merged into a branch
  other than the base branch (`base-branch`, or the repository's default
  branch), all exit `0` — declining a removal never archives anything;
* needs no `actions/checkout` and no `permissions:` block. Passing
  `GITHUB_TOKEN` with `pull-requests: read` is optional, and only lets a
  `FLAG_HAS_DEPENDENTS` refusal be reported as `[deferred]` when every
  dependent's removal has merged too;
* reads only `api-token`, `org`, `project`, `api-url` and `base-branch`, and
  ignores `dry-run`.

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
buried in a longer sentence; it must stand alone on its own line. A command
inside a code block (fenced, or indented four spaces) or an HTML comment does
not fire either, so a comment can show someone how to ask for one without
asking.

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

### On CircleCI

The same image ships as a CircleCI orb,
[`featureflip/flag-cleanup`](https://circleci.com/developer/orbs/orb/featureflip/flag-cleanup),
built from [`orb/orb.yml`](orb/orb.yml) in this repository. The repository
still has to be hosted on GitHub, because the pull requests are opened there.

```yaml
version: 2.1
orbs:
  featureflip: featureflip/flag-cleanup@1
workflows:
  flag-cleanup:
    jobs:
      - featureflip/cleanup:
          context: featureflip
          org: my-org
          project: my-project
          dry-run: true
```

Put `FEATUREFLIP_API_TOKEN` and a `GITHUB_TOKEN` that can push branches and
open pull requests in that context, then add a scheduled trigger for the
workflow in the project settings. Its parameters are the inputs below, under
the same names, and it offers two modes: `remove` and `archive-sweep`. `archive-on-merge`,
`archive-on-deploy` and `pr-command` read a GitHub Actions event payload, which
a CircleCI pipeline does not have.

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
| `mode` | `FEATUREFLIP_MODE` | `remove` | `remove` opens removal pull requests. `archive-sweep` archives the flags whose removal has merged, on a schedule, and reads only `deployed-refs`, `base-branch`, `api-url` and `dry-run` from the inputs below — read [Archive the flag once the removal is deployed](#archive-the-flag-once-the-removal-is-deployed) before using it, because merging is not deploying. `archive-on-deploy` archives the outstanding flags a successful deployment shipped, and reads only `deployment-environment`, `base-branch`, `api-url` and `dry-run` from the inputs below — see [Archive when the deploy actually lands](#archive-when-the-deploy-actually-lands). `pr-command` re-runs the removal for the flag whose pull request a comment was left on — see [Re-run a removal from a comment](#re-run-a-removal-from-a-comment). `archive-on-merge` is deprecated — see [Deprecated: archive on merge](#deprecated-archive-on-merge). |
| `deployment-environment` | `FEATUREFLIP_DEPLOYMENT_ENVIRONMENT` | `production` | `archive-on-deploy` only. Matched case-insensitively against the payload's `deployment.environment`; a deployment to anything else archives nothing and says so. |
| `deployed-refs` | `FEATUREFLIP_DEPLOYED_REFS` | *(none)* | `archive-sweep` only. Tags, branches or commit shas that stand for what is deployed, one per line or comma-separated. When set, a flag is archived only once its removal is contained in every one of them. See [Only archive what is deployed](#only-archive-what-is-deployed). |
| `api-url` | `FEATUREFLIP_API_URL` | `https://api.featureflip.io` | Override only for a staging/self-hosted instance. |
| `staleness` | `FEATUREFLIP_STALENESS` | `dead` | `dead` (ready-for-review PRs) or `stale` (draft PRs). |
| `languages` | `FEATUREFLIP_LANGUAGES` | every language this Action supports (`ts`, `tsx`, `js`, `php`, `ruby`, `erb`, `dart`, `java`, `go`, `python`, `kt`, `csharp`, `swift`) | Comma-separated. |
| `directories` | `FEATUREFLIP_DIRECTORIES` | `.` | Comma-separated, relative to the checkout. |
| `accessors` | `FEATUREFLIP_ACCESSORS` | *(none)* | Comma-separated function names your own code uses to read a flag, on top of the SDK's, plus any JSX gate components as `Component[prop]` (`FlagGate[flag]`). See [Wrapping the SDK](#wrapping-the-sdk). |
| `inline-wrappers` | `FEATUREFLIP_INLINE_WRAPPERS` | `true` | Kotlin, Java, C#, Go, TypeScript, JavaScript, Python, Dart, PHP, Ruby and Swift: inline a method, Kotlin property getter, Kotlin top-level function, C# get-only `bool` property, Go function, TypeScript or JavaScript function or class method, Python function, method or `@property`, Dart function, method or getter, PHP function or class method, Ruby method, or Swift function, method or computed property whose whole body is one read of the flag at every call its file proves is bound to it, and delete it once nothing calls it. `false` folds the read inside the method and stops there. See [Wrapping the SDK](#wrapping-the-sdk). |
| `ignore` | `FEATUREFLIP_IGNORE` | *(none)* | Comma-separated flag keys to always skip. |
| `flags` | `FEATUREFLIP_FLAGS` | *(none)* | Comma-separated flag keys this run may propose, as an ALLOWlist — leave unset to consider every candidate. Does not re-propose a flag that already has a pull request — to re-run against one whose pull request is already open or closed, comment `@featureflip-cleanup rerun` on it instead, see [Re-run a removal from a comment](#re-run-a-removal-from-a-comment). `ignore` wins when a key is in both. |
| `base-branch` | `FEATUREFLIP_BASE_BRANCH` | the repository's default branch | Base branch each removal PR targets. Unset means "look it up" — see below. In the archive modes, the only branch a removal's merge counts on: give them the same value as the cleanup workflow. |
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
So is `[duplicate]`: a flag the platform lists twice in one run is previewed
once and counted once against `max-prs`, as a real run proposes it once.
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
  - `[piranha-error]` — the transform engine rejected its own rewrite (its
    syntax self-check, or its check for a reserved word left where a name
    belongs). Nothing was written. Please report it.
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
    `0-65535`, or it holds a character or host name no request can be built
    from (a tab, a control character, a malformed internationalised domain).
    `api.featureflip.io` without a scheme is the common case; all of these are
    refused here rather than failing on the first request, where they would
    arrive as an HTTP-client traceback;
  - `api-url` carries `user:password@` credentials. Requests authenticate with
    `api-token` in the `Authorization` header, which URL credentials would
    replace, so the token would never be sent;
  - `api-token` contains whitespace, a control character or a non-ASCII
    character — most often a secret holding a second line. The value is not
    printed;
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

**A wrapper that takes no key is inlined at its callers, in Kotlin, Java, C#, Go, TypeScript, JavaScript, Python, Dart, PHP, Ruby and Swift.**
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
shape, and the read is `BoolVariation` (or one of your `accessors`). A method's
`return read;` may follow statements that only build the context the read
targets:

```csharp
public bool IsEnabledFor(Guid? userId, Guid? orgId)
{
    var ctx = new EvaluationContext { UserId = userId?.ToString() ?? "service" };
    if (orgId is { } org)
        ctx.Set("organizationId", org.ToString());
    return _client.BoolVariation("old-checkout", ctx, false);
}
```

Each of them declares a local, or calls a method on one or sets a member of
one built right there with `new`, optionally under an `if` with no `else`;
every local ends up in the read's arguments and is read nowhere else; and
nothing assigns, increments, awaits, throws or passes an argument `ref`,
`out` or `in` (an object initializer's members aside). Deleting the
method drops what they compute along with the read's arguments, as folding
the read does.
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
  a `Directory.Build.props` or `.targets` above it, and no step that packs it.
  A pack step (`dotnet pack`, or `-t:Pack`/`-target:Pack` on `msbuild` or any
  `dotnet` command) publishes the project or solution it names. Every
  project a packed project references, directly or through others, is
  published too, since its public types ship with the package. A test
  project whose only sign of packing is an `<Import>` is not taken as packed,
  but only when the .NET SDK itself makes it one: it references
  `Microsoft.NET.Test.Sdk`, uses a test SDK, or sets
  `<IsTestProject>true</IsTestProject>`. A library that only references a
  test framework (an assertion helper, say) counts as packed.
  - In a GitHub Actions workflow the path is read against the step's
    `working-directory` (then the job's and the workflow's `defaults.run`). A
    path that resolves nowhere from that directory publishes every project.
  - Elsewhere, and in a workflow when the directory is unknown (a
    `working-directory` holding `${{ }}` or a NUL byte, or a script that
    changes directory itself), it publishes every project whose path ends
    with the argument, and every project when none does. A directory
    argument that a solution's directory also ends with publishes every
    project too.
  - A step that names nothing from an unknown directory, or names a path
    holding a variable or a NUL byte, publishes every project. So do `nuget
    pack`, any `.nuspec`, a workflow that does not parse, and a `nuget push`
    that no pack step or `GeneratePackageOnBuild` explains.
  - So does a package property set outside a project file:
    `GeneratePackageOnBuild` or `IsPackable` set to anything but `false`, or
    any `PackageId`. That covers a switch (`-p:GeneratePackageOnBuild=true`,
    `/p:`, `--property:`), including one in a `Directory.Build.rsp`, which
    MSBuild applies to every command-line build; an environment variable,
    which MSBuild reads as a property (a workflow's `env:`, `export`, a
    Dockerfile `ENV`, `SetEnvironmentVariable(…)`); and a build script's
    property call such as Cake's `WithProperty(…)`.
  - When something is packed and a project reference cannot be followed (a
    property or a wildcard in its path, or one added outside a project file),
    every project is published.

  An `internal` interface qualifies either way, unless its project names a
  friend assembly (`InternalsVisibleTo`) and can be packed.
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
`unparsed-mention`. A doc comment whose `cref` names the member of the
interface, the wrapper's class or another implementation (written
`<see cref="IFlags.IsX"/>` or `<seealso cref="IFlags.IsX"/>`, or
unqualified inside one of those types) does not keep the wrapper: when the
wrapper goes, it becomes `<c>IFlags.IsX</c>`, and the pull request lists
each one so the prose around it can be checked. A `cref` naming another
type's own member of that name is not the wrapper's. Any other `cref` naming
it keeps the wrapper (`unparsed-mention`), since deleting the member would
leave a broken reference: an `<inheritdoc>`, a `<see>` with content, another
tag, an ID string (`P:Ns.T.IsX`), or a type that cannot be told apart from
another of the same name. A VB or F# file mentioning it is
`other-language`. A class in one of those files that implements the interface
(`@implements`, `Implements`, `interface … with`) counts as an
implementation.

Two things are not checked in C#. A property getter in an argument
(`IsCheckoutEnabled(user.Name)`) or in the receiver is a call the inlining
drops, as it is in Kotlin. Code that serializes or binds the wrapper by
reflection with no attribute on it (a view model passed to a JSON serializer)
loses the member when it is deleted. Read the deletion in the diff when the
wrapper's class is also a data object.

In Go, a wrapper is a top-level function or a method with one unnamed `bool`
result and no type parameters, whose body is exactly `{ return read }`:

```go
func NewCheckout(u string) bool {
	return client.BoolVariation("old-checkout", u, false)
}

// elsewhere
if flags.NewCheckout(u) { … }
```

The read is `BoolVariation` (or one of your `accessors`) with the key as a
literal or as a constant in the same file, or OpenFeature's
`Boolean(ctx, "key", …)`. Go has no inheritance, so a call is folded when its
file shows which function or method it reaches:

- a function called bare in its own package (same directory, same `package`
  clause) with no local of that name in scope, or called as
  `pkg.NewCheckout(…)` through the file's import of its package. The import
  path is read from the nearest `go.mod`, and it must be the same `go.mod`
  for the caller and the function. The import must be aliased, or its path
  must end in the package's own name, so that it can be removed once its
  last use is folded;
- a method called on a receiver whose type the file spells. That covers a
  parameter or method receiver with a written type, `var x T`, `x := &T{…}`,
  `x := T{…}`, `x := new(T)`, and a package-level variable of one of those
  forms (also as `pkg.Default`). It also covers a struct field declared with
  the type, followed one `.field` at a time (an embedded field is selected by
  its type name, as in `s.Flags.NewCheckout()`).

Folding a call drops its receiver and its arguments, and Go does not build
with an unused local or import. So a local that only the call reads must be
one the cleanup removes afterwards (see [Supported Go flag-read
shapes](#supported-go-flag-read-shapes)). A local declared together with
other names that have values (`var a, b = …`, `a, b := …`), declared inside a
function literal or in a function sharing its name with another in the same
file, or spelled again by a comment,
a struct key, a shadowing declaration or an assignment, is refused
(`unremovable-local`). A map literal's key (`map[string]bool{k: true}`) is a
read of `k`, so it counts like any other argument. An argument read through an
import whose path does not end in its package's name, or through a dot
import, is refused too (`import-unresolved`), since that import could not be
removed either.

A method with a value receiver (`func (f Flags) On() bool`) called through a
pointer dereferences it, so on a nil pointer the call panics. Folding it would
take that panic away, so such a call is refused (`nil-receiver`) unless the
pointer cannot be nil: a local initialised to `&T{…}` or `new(T)` that nothing
assigns or takes the address of. A parameter, a `var p *T`, a package-level
variable and a pointer field may all be nil. A method with a pointer receiver
is folded as before.

A call made through `go` or `defer`, with type arguments, or through a
promoted method (`s.NewCheckout()` where `S` embeds the type) is left alone.
So is a function or method used without a call (`f := flags.NewCheckout`).

Go interfaces are satisfied without any declaration, so an interface method of
the same name anywhere in the repository keeps the wrapper
(`interface-structural`). For the same reason an **exported** method is never
deleted: a value of its type may be handed to code outside the repository that
expects an interface with that method. Its callers are still folded, and the
removal is reported as partial. An unexported method can only satisfy an
interface of its own package, which the scan covers. A function marked with a
cgo `//export` comment is kept (`annotated`), and a `//go:linkname` comment
naming it keeps it too (`unparsed-mention`), as does an `Example` test
function named after it in its own directory, which `go test` would otherwise
reject once the function is gone. No call is folded in an external test
package (`package flags_test`) that declares an `Example` named after
something (`unparsed-mention`): `go vet` looks that name up through the test
package's import of `flags`, which a fold can remove. Templates cannot reach anything
this deletes: they call exported methods, which are never deleted, and reach
functions only through a `FuncMap` your Go code builds, which keeps them.

In TypeScript and JavaScript, a wrapper is one of these, with a whole body
that is one read of the flag being removed:

- a top-level `function`;
- a top-level `const` holding an arrow function or a function expression;
- a method of a non-generic class, static or not;
- a field of such a class holding an arrow function or a function
  expression (`banner = (u) => ...`), with no type, `?` or `!` written on it.
  A field is an own key of every instance, so code that enumerates an
  instance (`Object.keys(panel)`, `{ ...panel }`) no longer sees it once it
  is deleted. Instances are passed around too freely to check for that, so
  read the deletion in the diff if your code does;
- a method of an object literal that a top-level `const` holds with no
  written type, or a key of it holding an arrow function or a function
  expression (`const flags = { banner(u) { ... } }`). The literal must hold
  no spread and no computed key, since either can put another value under
  the name. Its members are the object's own keys, so the object must be
  used only as the receiver of a member (`flags.banner(u)`, `flags.label`)
  anywhere in the repository, `this` inside it included. `Object.keys(flags)`,
  `{ ...flags }`, `for (k in flags)`, `flags[k]`, `Object.assign(flags, o)`,
  passing or storing it, a re-export of its module, or a component naming it
  would see a deleted member go, or replace it without spelling its name, so
  any of these refuses it (`overridable`).

The read is `boolVariation` or `useFeatureFlag` (or one of your `accessors`),
with the key first. The following are not wrappers: `async` functions,
generators, getters, members of an object literal nested in another, and any
wrapper of `getBooleanValue`, whose folded form is awaited. None of them is inlined or
deleted. Only the read inside is folded, like any other read. The same goes
for a wrapper declared as `export default` (a function, or a method of a
class exported that way): its callers import it under a name of their own,
which the proof cannot follow, so it is not inlined.

TypeScript's class types are structural. Anything with the same members can
stand in for a class, so a written type proves nothing on its own. The
wrapper's name must therefore be declared exactly once across the
repository's TypeScript and JavaScript (`name-not-unique`). All of these
count as declarations:

- functions and methods;
- class fields and constructor parameter properties;
- object-literal keys and methods;
- module-level bindings;
- enum members;
- declarations inside a namespace or a block;
- an assignment such as `obj.banner = ...` or `Flags.prototype.banner = ...`;
- an `export { other as newCheckout }`.

Any string literal in the repository's TypeScript or JavaScript that equals
the wrapper's name also refuses it, since it may be a computed key (a route,
a log line or `"banner"` as a key counts). A test fake such as
`{ newCheckout: () => false }` is a second declaration. So is a committed
build output (`dist/flags.js`, `flags.d.ts`). The one exception is a
JavaScript wrapper's own declaration file: `flags.d.ts` beside `flags.js`
(`.d.mts` beside `.mjs`, `.d.cts` beside `.cjs`). Its declaration of the
wrapper (a function signature, or the member in the class or the object
type of the owner's name) describes the wrapper instead of competing with
it, and an import of `./flags` reaches `flags.js`, the file that runs. The
callers are inlined, and the declaration goes with the wrapper: every
overload of a function signature, a `declare const` of the name, or the
member, each with its doc comment. If that leaves the declaration file with
no `import` or `export`, the first deleted statement becomes `export {};` so
the file stays a module rather than turning into a global script. The
wrapper is kept with its declaration (`declaration-file`) in two cases:
when the declaration shares a `declare const` with another name, and when a
TypeScript run of the same removal also edits the declaration file (it
holds the flag key, or names a constant or a wrapper that run removes), since a
file edited by two runs would depend on their order. A
`module.exports = { newCheckout }` in the wrapper's own file is the wrapper
itself, not a second declaration. It still keeps the wrapper. Its callers in
that file are inlined, and those in other files are `import-unresolved` (see
below). Re-exporting the wrapper itself
(`export { newCheckout as nc }`, a barrel) does not refuse it either, and the
wrapper is kept as partial.

Two more checks refuse a wrapper:

- a property or method signature of the name in any interface or type
  literal (`interface-structural`);
- a method whose class extends or implements a type the repository does not
  declare (`overridable`).

A call is inlined when the calling file binds every name the proof uses
exactly once. The shapes are:

- `newCheckout(u)`, in the wrapper's own file, through
  `import { newCheckout }` (aliased or not), or through a top-level
  `const { newCheckout } = require(...)`;
- `ns.newCheckout(u)`, through `import * as ns` or `const ns = require(...)`;
- `Flags.legacy()`, for a static method;
- `flags.banner(u)`, on a module-level `const flags = new Flags()` in the
  same file or imported, or on the object literal a module-level
  `const flags = { ... }` holds, for its own member;
- a class field wrapper, called the way a method is;
- `f.banner(u)`, `this.gate.banner(u)` and longer `.field` chains. Each
  parameter, local, field or constructor parameter property in the chain
  must be written as exactly `Flags`, or initialized with `new Flags(...)`.
  In JavaScript, a field assigned `new Flags()` once in the constructor
  counts. An optional field (`gate?: Flags`) is not proven.

Imports resolve the way cross-file key constants' do: relative paths,
`index` files and `tsconfig`/`jsconfig` `paths`. An explicit `baseUrl` is
followed as well, for a bare specifier no `paths` pattern fits (`lib/flags`
under `"baseUrl": "src"`), unless a runtime could read it as a package
first: a Node built-in, a package any `package.json` in the checkout
declares or a lockfile lists, or one installed in a `node_modules` above the
importing file. Nor is it followed for a specifier a `paths` pattern fits
whose targets name no file. Default imports,
re-exports and barrels, and package names are `import-unresolved`. A
function, a class or an instance resolves through an import (or `ns.` or
`require`) only when its module exports it directly, as `export function`,
`export const`, `export class` or `export { newCheckout }`. If that module
also has an `export *`, re-exports another module's function of that name
(`export { newCheckout } from "..."`), or assigns `module.exports`, an
importer may get something else under the name, so its callers there are
`import-unresolved`. So a wrapper exported only through `module.exports`
(CommonJS) is not inlined outside its own file, while a function exported
with `export` and called through `require` is. A sloppy-JavaScript `with`
block refuses every call under it.

A function wrapper is also kept when anything hands its module on as an
object, because a name computed at run time can then call it without
spelling it (`table[gate](u)` after `const table = flags as any`). This
applies to a namespace import or `require` of the module used as anything
but `ns.<name>` or a type, and so to it being cast, passed, spread, indexed
or re-exported. It also applies to `export * as`, a dynamic `import()`,
`import m = require(...)`, and a glob loader that can load the file. Each
such use is reported as `receiver-unproven`. The wrapper's callers are still
inlined, and the removal is partial. An `export * from` the module (a
barrel) is followed rather than refused, so a namespace of the barrel handed
on keeps the wrapper too. A method wrapper is reached through an instance,
so this does not apply to it.

The wrapper's own run also rewrites callers in another file type of the
family, such as `Checkout.tsx` calling `flags.ts`. Two conditions apply: that
file type must be configured, and no other run may also rewrite the file. A
caller that fails either condition is `outside-run`, and the wrapper stays.

A test that stubs the wrapper is resolved before any caller is inlined, the
way stubs of the SDK's read are. A stub returning the kept value is deleted,
and a test whose stub returns the removed value is deleted. These spellings
are resolved:

- `jest.spyOn(target, "m")` or `vi.spyOn(target, "m")`, followed by
  `.mockReturnValue(bool)`, `.mockReturnValueOnce(bool)` or
  `.mockImplementation(() => bool)`;
- `sinon.stub(target, "m").returns(bool)`;
- `target.m.mockReturnValue(bool)` (or `mockReturnValueOnce`);
- an entry `m: () => bool`, `m: jest.fn(() => bool)` or
  `m: vi.fn(() => bool)` in a `jest.mock` or `vi.mock` factory of the
  wrapper's own module. The entry is deleted, and a
  factory left empty, or holding only `...jest.requireActual(...)` of the
  same module, is deleted with its call.

The stub's target must be one the call itself would be proven on: a
namespace import of the wrapper's module, the module's exported instance, an
instance whose type is written as the class, `Flags.prototype`, the class
itself for a static method, or `vi.mocked(...)` or `jest.mocked(...)` of any
of those.

Stubs are resolved only for a wrapper whose every call is inlined. If a call
is left, for example `newCheckout(u.trim())` (`argument-has-call`), that call
still reaches the stub, so the wrapper keeps its stubs and is refused
(`test-double`).

A mock of a method wrapper's whole class also refuses it, unless the run can
see that the mock always answers the kept value. An unstubbed class mock
answers `undefined` (or `null`) for the wrapper, so a test using one may be
exercising the removed branch. A jest-mock-extended (or
vitest-mock-extended) `mock<Flags>()` or a ts-mockito `mock(Flags)` is
resolved when all of these hold:

- it is a `const` declared directly in the callback of an `it` or `test`
  (and their forms above), or of a `beforeEach`;
- the first thing done with it is a stub of the wrapper to the kept value,
  written directly in that callback: `m.banner.mockReturnValue(true)` on a
  jest-mock-extended mock, or `when(m.banner(anything())).thenReturn(true)`
  on a ts-mockito one, with one `anything()` for each of the method's
  parameters, all of which must be required;
- after that, it is only used through its members (`m.other`), wrapped by
  ts-mockito's `instance(m)`, or passed to `new` of a class imported from a
  file that is not test code (`new Svc(m)`). Passing it to any other call
  (a helper that may call `mockReset`), putting it in an object, an array or
  a JSX attribute, or using it inside a function declaration (which may run
  before the stub) does not count;
- nothing else inside that callback binds its name. Other tests may declare
  their own `m`, and each one is judged on its own;
- every stub of the wrapper on a receiver of that name, anywhere in the
  file, is on a mock the run has identified, and the file never calls
  `resetAllMocks` or `restoreAllMocks`.

The stub is then deleted and the mock is kept. A test that stubs the removed
value is deleted along with its mock. Every other class mock still refuses
the wrapper. That includes `createMock<Flags>()`,
`sinon.createStubInstance(Flags)` and the others listed below, a
`mockReturnValueOnce` stub, a ts-mockito stub that matches particular
argument values, a mock declared with `let` and assigned in a `beforeEach`,
and a mock declared at the top of the file or in a `describe` body: a
runner's `resetMocks` setting can wipe that stub before each test.

A test title that spells the wrapper's name is not a stub. That means a
string that is the first argument of `describe`, `it` or `test` (including
`fit`, `xit`, `xtest`, `fdescribe`, `xdescribe`, the `.only`, `.skip`,
`.concurrent`, `.todo` and `.failing` forms, and the call `.each(...)`
returns), when the same call also takes a callback. Any other string that
spells the name still counts, for example `toHaveBeenCalledWith("banner")`,
an entry in an `.each` table, `obj["banner"]`, or `it.todo("banner")` with no
callback.

Every stub of a wrapper is resolved, or none is. If one cannot be, the
wrapper is refused (`test-double`) and none of its stubs is edited, though a
test may still be deleted for another wrapper's removed value.
Wrappers are resolved together, so two wrappers stubbed to opposite values
are both refused. The following cannot be resolved:

- a stub returning a variable;
- a test stubbing both values (two wrappers of one flag count as one value);
- a removed-value stub in a `beforeEach` or at the top of a file;
- an automock (`jest.mock("./flags")` with no factory, including one
  followed by `jest.mocked(f).mockReturnValue(...)`);
- a Sinon sandbox;
- an `expect(spy).toHaveBeenCalled()`;
- any spelling not listed above.

Some doubles replace the wrapper without spelling its name. They are never
resolved, and each one refuses the wrapper:

- a `jest.mock` or `vi.mock` (or their `doMock` forms, or
  `jest.unstable_mockModule`) of the wrapper's own module with no factory, or
  with a factory this cannot read or whose entry of the wrapper's name cannot
  be resolved. A factory with no entry of that name does not replace the
  wrapper and is left alone, whether or not it spreads the real module;
- `jest.setMock`, `jest.createMockFromModule`, `jest.requireMock`,
  `vi.importMock`, or the `mock.module` of Node's and Bun's test runners, of
  the wrapper's own module;
- a module mock whose target is computed (a template literal, a variable),
  or does not resolve to a file in the repository and is neither a Node
  built-in (`fs`, `node:fs/promises`) nor a package (`axios`, `lodash/fp`,
  `@scope/name`) that a `package.json` in the repository declares in
  `dependencies`, `devDependencies`, `peerDependencies` or
  `optionalDependencies`. It refuses every wrapper in the run, because it
  could be any wrapper's module: a relative path that names no file, or an
  alias defined only in a bundler or test config (`vite.config`, Jest's
  `moduleNameMapper`) rather than in `tsconfig`/`jsconfig` `paths`. A target
  whose last segment is the wrapper file's own name (`@/flags` for
  `flags.ts`) is refused even when it names a declared package. The target
  may be written `import("./flags")` or `require.resolve("./flags")` around a
  plain string;
- for a method, a mock of any module that names the method's class, or that
  cannot be read, unless its factory only spreads `jest.requireActual(...)`;
- a stub of the wrapper's whole class, which never names the method:
  `sinon.createStubInstance(Flags)`, `mock<Flags>()`, `mockDeep<Flags>()`,
  `createMock<Flags>()`, ts-mockito's `mock(Flags)`, `stubInterface<Flags>()`,
  `Substitute.for<Flags>()`, `td.object(Flags)` or `td.instance(Flags)`,
  under any import name or as a member of the module (`ns.Flags`,
  `require("./flags").Flags`), except a jest-mock-extended or ts-mockito
  mock that is resolved as described above;
- a dependency-injection test bed that mocks every dependency (NestJS
  `.useMocker(...)`, `TestBed.solitary(...)` or `TestBed.sociable(...)` from
  `@suites/unit`), for every method wrapper in the run;
- two stubs that name no member: `sinon.stub(obj)` with one argument, and
  `stub(obj, name)` or `spyOn(obj, name)` whose member name is not a string
  literal. Each refuses every wrapper unless `obj` provably is something
  else: an import of another file, a package, or a global such as `console`;
- test-runner configuration. Jest's `automock: true` (in `jest.config.*` or
  the `jest` key of a `package.json`) refuses every wrapper. So does a
  `moduleNameMapper` or `alias` entry in a Jest, Vite or Vitest config that
  points into a `__mocks__` or `mocks` directory, ends in the wrapper
  module's own name, or resolves to its file. Aliases defined anywhere else,
  such as a webpack config or a Babel plugin, are not read.

A stub in a setup file, a story, a `__mocks__/` directory, or a Cypress or
end-to-end helper counts as a stub, and so does any file that uses `jest.`,
`vi.mock`, `vi.spyOn`, `vi.fn`, `sinon.` or `cy.stub`.

A call is not inlined (`argument-has-call`) when the wrapper has a parameter
default that may run code, such as `u = currentUser()` or the destructuring
`({ id } = { id: currentUser() })`. That holds even when the call passes the
argument, since an `undefined` argument still runs the default. A default of
literals and names (`u = "anon"`) does not count.

A property getter in an argument (`isCheckoutEnabled(session.user)`, where
`user` is a `get` accessor) is not detected, as in Kotlin and C#. It reads as
a plain property, and the inlining drops it with the call, so whatever the
getter does stops happening. The same is true of a direct read,
`client.boolVariation("KEY", session.user, false)`: the fold drops the whole
call, arguments included. Read the diff where an argument is computed by a
getter that does work.

A decorated method (`@Get("/checkout")`) has its callers inlined but is
itself kept (`annotated`), whatever the decorator: a decorator is code of your
own, and may register the method with a framework that calls it by name.

A `private` field or constructor parameter property that only the folded
calls read is left unread, which under `noUnusedLocals` is error TS6138 or
TS6133. A constructor parameter property is removed with every caller's
argument when every caller can be found, and a field when its initializer can
do no work; anything else is named in a warning. See "Supported JS/TS
flag-read shapes".

In Python, a wrapper is a module-level function, or a method of a
module-level class (a plain method, a `@staticmethod`, a `@classmethod` or a
`@property`), whose body is `return <read>`, after an optional docstring:

```python
def new_checkout(user):
    return client.variation("old-checkout", user, False)

# elsewhere
from app.flags import new_checkout

if new_checkout(user): …
```

The read is `variation` with a boolean literal default (positional or
`default=`), `get_boolean_value`, or one of your `accessors`, with the key as
a literal or a constant, as for any other Python read. An `async def`, a
wrapper with any other decorator (a decorator can change what it returns) and
one whose parameter default calls something are not wrappers: only the read
inside them is folded. Python has no static types, so a call is folded only
where its file ties it to the wrapper:

- a function called bare in its own module, or in a module that imports it
  with a top-level `from <its module> import new_checkout` (relative imports
  too), or called as `m.new_checkout(…)` through a top-level
  `import <its module> as m`, `import <its module>` or
  `from <package> import <its module>`. The module is found the way a
  key constant's is (see "Declared in another file"). Each import of a
  deleted function is rewritten so the module still loads: the name is
  dropped from its list, or a statement left with no name becomes
  `import app.flags as _flags  # noqa: F401`;
- a method called as `self.banner(…)` (or a property read as `self.beta`)
  inside a method of its own class, or `cls.banner(…)` inside a class method;
- a static or class method called as `Flags.banner(…)`, where `Flags` is the
  class in its own module or a top-level `from <its module> import Flags`.

A call is left alone when the file binds the receiver's name anywhere else
(a parameter, an assignment, a loop or `with` target, a nested function, a
star import), names `globals`, `vars`, `locals`, `exec`, `eval`, `__dict__`
or `__getattribute__`, or imports the function under an alias. So is an
argument that calls, awaits, yields, unpacks (`*args`), iterates or assigns
(`argument-has-call`), and a function or method used without a call. A
method called through any other object (`flags.banner(u)` on a parameter) is
not folded: an annotation proves nothing at run time.

The name must be declared once across the repository's Python files
(`name-not-unique`): any other function, method, class, parameter,
assignment (`self.banner = …` and `app.flags.new_checkout = …` included) or
import alias of it counts, and so does a string naming it in `setattr(…)`
or `__setattr__(…)`, or in a file that names `globals`, `vars` or the like.
For a method, any name of it in a class body counts (an import there, too),
and its class must be bound once: a class rebound later in its module
(`Flags = wrap(Flags)`), or decorated with anything but `@dataclass`,
`@final` or `@total_ordering`, makes none of its methods a wrapper. A test double refuses the whole wrapper before any
call is folded (`test-double`): a string naming it in `patch(…)`,
`patch.object(…)` or `monkeypatch.setattr(…)`, a keyword naming it in
`Mock(…)`/`MagicMock(…)` or `patch.multiple(…)`, a write to it or to its
`return_value`/`side_effect` on a mock, and for a method, a mock of its class
(`create_autospec(Flags)`, `patch("app.flags.Flags")`). A string naming it
anywhere else (`__all__`, `getattr(flags, "new_checkout")`) keeps it, though
its proven callers are still folded (`unparsed-mention`); a docstring does
not count unless it holds a doctest (`>>>`). A call in a self-documenting
f-string field (`f"{new_checkout(u)=}"`) is not folded, since that prints its
source text. An import of a deleted function is kept, and keeps the function,
when the private alias its rewrite would bind is already used in the file. A method that is its class's only statement is kept, since the
class would be left with no body.

In Dart, a wrapper is a top-level function or getter, or a method or getter
of a `class`, static or not, declared to return `bool` (not `bool?`), with no
`async` or `sync*`, whose whole body is one read of the flag being removed:

```dart
class Flags {
  Flags(this.client);
  final FeatureflipClient client;

  bool get newCheckout => client.boolVariation('old-checkout', defaultValue: false);
}

// elsewhere
if (flags.newCheckout) { … }
```

The read is `boolVariation` (or one of your `accessors`) with the key first,
as a literal, a constant in the same file, or a constant of another file the
run proved names it. A call is folded when its file shows which declaration
it reaches. Dart looks a bare name up in the enclosing scopes first, then in
the library and its imports, and only then in inherited members, so:

- the wrapper's name must be declared once across the repository's Dart
  files, counting every local, parameter (of a function type too), field,
  extension type representation, loop, `catch` or pattern variable, record
  type field and named constructor, so that nothing can hide it;
- a top-level wrapper is called bare in its own file or in a file whose import
  of a library in your package reaches it (directly or through an `export`,
  with no `hide` of it and any `show` naming it), or through a prefix of such
  an import (`ff.newCheckout()`), but not a `deferred` one;
- a static member is called through its class's name, reached the same way,
  or bare inside its class;
- an instance member is called bare or through `this` inside its class, or
  through a name whose every declaration in the file is written with the
  class as its type, or initialised with `Flags(…)`, `const Flags(…)` or
  `new Flags(…)` and no type, where one of those declarations encloses the
  call: a parameter, a local or a field of the enclosing class.

Everything else is left alone: `widget.flags.newCheckout`,
`context.read<Flags>().newCheckout`, `flags?.newCheckout`, `flags!.newCheckout`,
a cascade, a `late`, nullable or `dynamic` declaration, a prefixed type
(`p.Flags`), a constructor parameter, a method torn off without a call, a
call with type arguments, a mention inside a string, a symbol (`#newCheckout`)
a statement label, a record field or any other place that is not an
expression, a call made only for its value that is the whole body of a
braceless `if`, `else`, `for`, `while` or `do`, a labelled statement or the
only statement of a `case` (deleting it would hand its place to the next
statement), and every call in a library with `part` files or in a part.

A class anything in the repository extends, implements or mixes in, that is
`abstract`, `sealed`, `interface` or a `mixin class`, or that itself has an
`extends`, `implements` or `with` clause, keeps its wrappers (`overridable`),
as does a member marked `@override`: `@override` is optional, so a member of
a class with a supertype may override one of the SDK's or a package's. A mock or fake that
implements the class (`class MockFlags extends Mock implements Flags`, or a
generated `*.mocks.dart`) keeps them as `test-double`, and so does a call
inside a `when(() => …)` stub. A member of an extension, mixin, enum or
extension type is not treated as a wrapper; the read in it is folded where it
is. A wrapper with any annotation but `@deprecated` keeps its callers inlined
and is itself kept (`annotated`): `@pragma('vm:entry-point')` means native
code calls it.

A public wrapper (no leading `_`) in your package's `lib/` is never deleted
(`published`) unless `pubspec.yaml` says `publish_to: none`, as an app's does:
another package that depends on yours may call it. Its callers in the
repository are still inlined, and the removal is reported as partial. An
instance member of such a package's public class is not inlined at all
(`interface-published`) unless the class is `final` or `base`: every Dart
class is an interface, and another package may implement it and pass its own
member into your code. A doc comment referring to the wrapper
(`[newCheckout]`, `[Flags.banner]`) keeps it too (`unparsed-mention`), since
the analyzer resolves the reference. A string
whose whole text is the wrapper's name (a platform channel's method name, a
lookup key) also keeps it (`unparsed-mention`); a test's description and
prose that only mentions the name do not.

In PHP, a wrapper is a function declared at the top of a file or namespace,
or a method of a `class`, static or not, declared to return `bool` or with no
return type, whose whole body is `return <read>;`:

```php
final class Flags
{
    public function __construct(private FeatureflipClient $client) {}

    public function newCheckout(): bool
    {
        return $this->client->boolVariation('old-checkout', [], false);
    }
}

// elsewhere
if ($this->flags->newCheckout()) { … }
```

The read is `boolVariation` (or one of your `accessors`) or OpenFeature's
`getBooleanValue`, with the key first, as a literal, a constant in the same
file, or a constant of another file the run proved names it. PHP binds a
function or method name by its namespace and its class, never by what a
local holds, and compares it without case, so:

- the wrapper's name must be declared once, as a function or a method of any
  class, interface, trait or enum, across the repository's PHP files
  (Composer's `vendor/` aside);
- a function is called bare where the file's namespace, a `use function`
  import or PHP's fallback to the global namespace reaches it, or by a
  qualified name that resolves to it (`\App\Flags\newCheckout()`);
- a static method is called through its class's name, resolved through the
  file's namespace and `use` imports, or as `self::` inside its class
  (`static::` too, unless another package may extend the class);
- an instance method is called as `$this->newCheckout()` inside its class,
  through a parameter of the enclosing function typed as the class (not
  nullable, no default, not by reference) that the function only ever uses as
  an object, or as `$this->flags->newCheckout()` where `flags` is a `private`
  property of the enclosing class typed as the class, declared or promoted by
  the constructor.

Everything else is left alone: a local (`$flags = new Flags(…)`), a public
or protected property, `$flags?->newCheckout()`, a call inside a closure, an
arrow function or an anonymous class (`Closure::bind` can rebind `$this`), a
first-class callable (`$flags->newCheckout(...)`), a call passed as an
argument, parenthesized or not (a literal cannot be passed by reference, and
the callee's signature is not read), a call that is the whole body of a
braceless `if`, `else`, loop or `do`, a call whose result is used as an object, a call inside string
interpolation, a call with an argument that does work (a call, an assignment,
`new`, an increment, `...$spread`), and every call in a file that does not
parse. A function with a by-reference parameter, or a default that does work,
is not treated as a wrapper.

A class anything in the repository extends (an anonymous class too), that is
`abstract`, that has an `extends` or `implements` clause or uses a trait, or
that declares `__call` or `__callStatic` keeps its wrappers (`overridable`),
as does a method marked `#[\Override]`. A `class_alias` of the class keeps
them as `name-not-unique`. A PHPUnit, Mockery or Prophecy double of the class
(`createMock(Flags::class)`, `Mockery::mock(Flags::class)`,
`prophesize(Flags::class)`) or a stub of the method (`->method('newCheckout')`,
`->shouldReceive('newCheckout')`, a php-mock `getFunctionMock`) keeps them as
`test-double`. A wrapper with an attribute other than `#[Deprecated]` (or a
docblock annotation such as `@Route(...)` or `@VirtualProperty`) keeps its callers inlined and is
itself kept (`annotated`).

A string whose text names the wrapper (`'newCheckout'`,
`'App\Flags\newCheckout'`, `'Flags::check'`, `'App\Flags\Flags@check'`) keeps it (`unparsed-mention`):
PHP calls a callable by its name, as in `call_user_func`, `[$flags,
'newCheckout']` or a framework's registration. So does the name in inline
markup, and in any other file (a Twig template, a YAML config) in any case,
and for an `isX`, `getX` or `hasX` wrapper as the `x` a template reads. Its
callers are still inlined. A dynamic call that
spells no name (`$flags->$method()`) cannot be seen. A public or protected
wrapper of a Composer package whose `composer.json` does not say `"type":
"project"` is never deleted (`published`): Composer's default type is
`library`, and another package may call it. So is one of a WordPress plugin
or theme (a `Plugin Name:` or `Theme Name:` header). An instance method of such a
package's class is not inlined at all (`interface-published`) unless the
class is `final` or the method `private`, since another package may extend
the class and pass its own method into your code.

In Ruby, a wrapper is a method of a `class` or `module`: an instance method,
a `def self.` class method, or a module function (a `def` after a bare
`module_function`), with plain parameters (or optional ones with a literal
default), whose whole body is one read, written as a body, an endless
`def … =` or a `return`:

```ruby
class Flags
  def new_checkout?
    @client.bool_variation("old-checkout", {}, false)
  end

  def self.on?(user) = CLIENT.bool_variation("old-checkout", { key: user }, false)

  def banner
    new_checkout? ? "new" : "old"
  end
end

# elsewhere
return :off unless Flags.on?(user)
```

The read is `bool_variation` (or one of your `accessors`) with the key first,
as a literal or a constant in the same file, or OpenFeature's
`fetch_boolean_value(flag_key: …)`. Ruby is untyped and almost anything can be
redefined at run time, so very little is proven:

- the method's name must be defined once across the repository's Ruby (`.rb`,
  `.rake`, a `Rakefile`, a `.gemspec`, a `.jbuilder`, a Sorbet `.rbi` …), and its class or
  module's name declared once: a class reopened in another file keeps its
  wrappers (`name-not-unique`);
- an instance method of a class that nothing in the repository subclasses
  is called bare (`new_checkout?`) or through `self` inside an instance
  method of that class, never inside a block or a lambda (whatever receives
  the block may `instance_exec` it, even a `select`), and never from a
  module's instance methods (they run on whatever includes the module); a
  bare name without `?` or `!` that a local or a parameter of the enclosing
  method also binds is a read of that local, not a call;
- a class method or a module function is called through its class or
  module's name (`Flags.on?`, `Admin::Flags.on?`, `::Flags.on?`), which must
  resolve to it through the caller's nesting, or bare or through `self`
  inside one of the owner's own `def self.` methods (for a class, one nothing
  subclasses).

The call's value must be used: a condition, an operand of `!`, `&&` or `||`,
the right side of an assignment, an argument, a `return`, or the last
expression of a method or a block. Everything else is left alone: a call as
a statement of its own, `&.`, `Flags::on?`, a receiver Ruby does not
spell as the owner (`@flags.new_checkout?`, `flags.new_checkout?`), a call
inside string interpolation, a call with a block, a call with an argument
that is not a literal, a constant, an instance variable or a local (in Ruby
`user.id` and `current_user` are calls, which inlining would drop), a call
whose number of arguments the method does not take, and every call in a file
that does not parse.

Any symbol or string spelling the method's name, anywhere in the
repository's Ruby, keeps the wrapper and its callers (`unparsed-mention`):
`attr_reader :new_checkout?`, `define_method(:new_checkout?)`,
`alias_method`, `delegate`, `helper_method`, `send("new_checkout?")`,
`respond_to?(:new_checkout?)`, a `new_checkout?:` hash key, or a
`class_eval` heredoc can each define, call or rename it by name. An RSpec,
Minitest or Mocha stub of the method or double of the class
(`allow(flags).to receive(:new_checkout?)`, `instance_double(Flags)`,
`stub_const("Flags", …)`, `Flags.stubs(:on?)`) keeps them as `test-double`.
A class or module that defines or inherits `method_missing` or
`respond_to_missing?`, prepends a module, defines methods from computed
names (`define_method(name)`), has an `inherited` or `included` hook, or is
reopened through `class_eval`,
`class << Flags` or a refinement keeps its wrappers (`overridable`). A call
through a computed name (`send(name)`) cannot be seen. A method under a
Sorbet `sig` keeps its callers inlined and is itself kept (`annotated`). An
ERB, HAML or Slim template, an RBS signature, or any other file, naming the method keeps it
too, with its Ruby callers still inlined (`other-language` for ERB,
`unparsed-mention` otherwise). In a gem (a `.gemspec` beside or above the
wrapper), another repository may subclass the class and override the method,
so an instance method is not inlined at all (`interface-published`), only a
class method's calls through the class's name are, and a public one is never
deleted (`published`).

In Swift, a wrapper is a function declared at the top of a file, or a method
or computed property of a top-level `struct`, `enum` or `class`, static or
not, declared to return `Bool` (not `Bool?`), with no `async`, `throws`,
generic parameter or default argument, whose whole body is `{ read }` or
`{ return read }`:

```swift
final class Flags {
    let client: FeatureflipClient

    var newCheckout: Bool { client.boolVariation("old-checkout", default: false) }
}

// elsewhere
if flags.newCheckout { … }
```

The read is `boolVariation` (or one of your `accessors`) with the key first,
as a literal, a constant in the same file, or a constant of another file the
run proved names it, and not negated or optional-chained. Swift looks a bare
name up in the enclosing scopes and the enclosing types' members (inherited
ones too) before the module's top-level names, and a member through its
receiver's static type, so:

- the wrapper's name must be bound once across the repository's Swift files,
  counting every declaration, protocol requirement, enum case, parameter,
  local, closure parameter or capture, loop, `case let`, `if let`/`guard
  let`/`while let` binding and `set`/`willSet`/`didSet` parameter, so that
  nothing can hide or overload it;
- every call must be in the wrapper's own module (the SwiftPM target or Xcode
  target that compiles its file; a checkout with no build file is one module);
- a function is called bare, outside any type with an inheritance clause,
  any extension and any type an extension anywhere in the repository extends
  (which may add a conformance whose protocol brings a member of that name);
- a static member is called through its type's name, outside those same
  types, or bare or through `Self` inside its own type;
- an instance member is called bare or through `self` inside its type,
  through `self.flags` where `flags` is a stored property of the enclosing
  type written as `let flags: Flags` (or initialised with `Flags(…)`), or
  through a name whose nearest binding (a parameter, a closure parameter with
  a type, a local or a stored property of the enclosing type) is written that
  way, and every binding of which in the file is. Not `Flags!` or `Flags?`,
  and not a computed or `lazy` property, whose getter the inlining would
  drop.

A computed property is read with no call (`flags.newCheckout`); a method or
function is called. Everything else is left alone: `flags?.newCheckout`,
`flags!.newCheckout`, an optional or untyped binding, `Flags.shared`, a call
through another module, a name used without being called, a call whose value
is dropped or used as an object, `!!`, a call with a trailing closure or an
argument that is not a name, a member access or a literal, and every call in
a file that does not parse. A key path (`\.newCheckout`, `\Flags.newCheckout`),
a `#selector`, or a string whose text names the member (`"newCheckout"`,
`"newCheckout:"`) keeps it (`unparsed-mention`), as does its name in any
other source file this tool reads. Objective-C, storyboards and plists are not
read, which is why an `@objc` member is never a wrapper.

A type with an inheritance clause, its own or an extension's, keeps its
wrappers (`overridable`): a protocol it conforms to may call the member
through an existential, and deleting it would break the conformance. So does
an `open` class, a class anything in the repository subclasses, and a member
that is `open`, `class`, `override` or `dynamic`; a subclass, or another type
conforming to the same protocol, in a test target (`Tests`, `Mocks`, `Fakes`)
keeps them as `test-double`. A wrapper with any attribute (`@objc`,
`@available`, `@MainActor`) is not inlined at all (`annotated`). Members of an
extension, a protocol or an actor, and declarations between `#if` and
`#endif`, are not treated as wrappers. A `public` wrapper (of a `public` type,
for a member) is never deleted (`published`), unless the nearest
`Package.swift` declares no `.library` product: an Xcode framework or a pod
ships public API with no manifest this tool can read.

A method qualifies when its whole body is one read of the flag being removed
(`= read`, `{ return read }`, `{ return read; }`), and:

- its name, and its class's name, are each declared once across the
  repository's Java and Kotlin files, or across its Go files, where a struct
  field or a package-level variable, constant or type of that name counts too
  (for a getter, a Kotlin function or a
  Java `getX()`/`isX()` of that name counts too, and for a top-level function,
  so does any function, property, class, object or typealias of that name).
  In C# the class's name must be declared once, and the member's name (a
  field, event or record parameter of that name counts too) once in the
  class, once in each other implementation of its interface and nowhere else
  in that interface. Other types may declare the name (see below), but not as
  an extension method, and no other base type of the class may;
- it is not `open`, `abstract` or an override, and nothing in the repository
  extends its class, anonymous classes included;
- if it implements an interface method, that interface is declared in the
  repository, has exactly one implementation there (Groovy and Scala classes
  count), is not public (Java package-private, Kotlin `internal` or
  `private`) or sits in a module that cannot be published (see below; for C#
  see above), extends no other interface, and is not functional: a lambda can
  implement an interface with one abstract method (or a Kotlin
  `fun interface`) anywhere, so its implementations cannot be counted. Code
  outside the repository can implement a public interface, and inlining would
  override it. In C#, other implementations may exist when each one's member
  is inert, described below.

A public Java or Kotlin interface qualifies only when nothing can publish its
module, judged from what the repository shows:

- Gradle: every plugin applied by the settings file, by the build file of the
  project and of each project above it (a moved project included), and by
  any build file that configures other projects
  (`rootProject.subprojects {}`, `configure(…)`, `project(":x") { … }`) is one known to publish
  nothing (Gradle's `java`, `java-library`, `application` and the like, the
  Kotlin JVM plugins, Spring Boot, Jib, Shadow, common linters). A plugin
  from a version catalog is looked up in `gradle/libs.versions.toml`. Any
  other plugin, a convention plugin included, publishes. So does any build
  script that spells `publish` (`maven-publish`, `publishing {}`,
  `mavenPublishing {}`; a build scan's `publishing.onlyIf` does not count),
  `uploadArchives`, a `signing {}` block or `jreleaser`, an `apply from:`,
  an `apply {}` block naming a plugin or a script, or `buildSrc` or an
  included build that registers one of the known plugin ids itself.
- Maven: the `pom.xml` and every parent up the chain is in the repository at
  the version asked for, or the parent is `spring-boot-starter-parent`, and
  none has
  `<distributionManagement>`, an `altDeploymentRepository`, a build
  extension, a DTD, or a plugin outside a known set (compiler, surefire,
  jar, shade, Spring Boot and so on; `exec` and `antrun` are not in it).
  Nor does any `.mvn/extensions.xml` or `.mvn/maven.config` between the
  module and the repository root.
- No script in the repository deploys it. A `mvn … deploy` step (also
  `mvnw`, `mvnd`, or a variable such as `$(MVN)`) publishes the modules its
  `-pl` names (all of them without one) in the reactor its `-f` names,
  matched by path suffix (a YAML `run: >` is read as the one command it
  folds into). Every module is published by a Gradle
  `--init-script`/`-I`, an `init.d/` directory, `jreleaser`, the JFrog CLI
  (`jf mvn`, `jf gradle`, a `.jfrog/` directory) or a Jenkins Artifactory
  step, an `altDeploymentRepository` in any script or XML file (a
  `settings.xml` included), a `deploy:deploy-file`, or a Maven command whose
  goals are a variable.
- No published module depends on it (`project(":x")`, `projects.x`, or a
  Maven dependency or unpacked `artifactItem` naming its artifactId), since
  it ships with that module. A published module that compiles sources from
  outside its own directory (`srcDir("../shared")`, build-helper's
  `add-source`) publishes every module.

A file under no build file, or under a `settings.gradle` whose projects
cannot be listed (`includeFlat`, a computed `include`), is published. Two
cases are not seen: JitPack, which builds any public repository without
configuration, and a deploy configured only outside the repository (a
runner's `settings.xml`, an init script in its Gradle user home). If yours
works that way, read the deletion in the diff.

In C#, an interface's other implementations do not block the wrapper when
each one's member is inert. Callers then get the flag's value whichever
implementation was bound. The member is inert when it is one of:

- a `bool` literal, when it is the value the flag keeps serving;
- a read of a field or auto-property of its own class (`_enabled`), or a
  chain of reads through one (`_options.Enabled`). Each step must be a field
  or an auto-property that is not `virtual`, `abstract` or `override`, on a
  non-generic class, struct or record of the repository that declares no
  operator. The last must be declared `bool`;
- an auto-property with a setter or `init` and no initializer
  (`bool IsCheckoutEnabled { get; set; }`). It counts as returning `false`,
  so it is inert only when the flag keeps serving `false` (a test fake's
  aside, below).

A configuration fallback (`ConfigCheckoutGate`, registered in place of the
flag-backed gate when no SDK key is set) is such an implementation: it reads
its options class and nothing else, so it takes the flag's value and goes
with the family.

The implementation must be a top-level class or struct, not `abstract`,
`partial`, `static` or generic; a test fake may also be a `private` class
nested in another type (below). It must be sealed or never extended, have every
base type in the repository, and, if it is public, sit in a project that cannot
be packed. It must sit in a file of its own too: one in the wrapper's or the
interface's file is `sibling-not-inert`. An implementation that returns the
value being removed is `sibling-not-inert`, since whatever builds it relies on
that value. So is a member shared with another interface, any call, a
`virtual` member, and anything else. The pull request lists each
implementation deleted with the wrapper and what it returned.

A test fake is one of these implementations declared in a test project: one
whose project file, or a file it imports, adds a test framework's package with
`<PackageReference Include="…">`, names a test SDK, or sets
`<IsTestProject>true</IsTestProject>`. An `Update` of a package does not
count. A test attribute such as `[Fact]` in a file of another project does not
make one. An implementation in such a file is `sibling-not-inert`, since the
tests beside it are never edited outside a test project and could still check
the value being removed. A project file that does not parse, or computes one
of these values, makes the implementation `sibling-not-inert`. A `Condition`
is not evaluated: a test package that a `Directory.Build.props` adds under one
counts for every project below it, so a production project there is read as a
test project. Read the deletion in the diff if your repository does that.

A fake's member may instead return its knob: a settable field or property of
its own, or a `bool` parameter of its primary constructor, read nowhere else
in the fake. The knob only chooses which branch a test runs, whatever its
default. Tests that set the knob are resolved like stubs (below):
`fake.Enabled = false;`, `host.Gate.Enabled = false;` (where `host`'s declared
type is a class of yours whose own field or auto-property `Gate` is declared
as the fake), `new FakeFlags { Enabled = false }` and `new FakeFlags(false)`.
A test that hands a fake built with nothing set straight to what it builds
(`new Svc(new FakeFlags())`) and writes its knob nowhere is decided by the
knob's default. A test that sets the knob to the value being removed
part-way through and back (or to the end of its body) loses only the
statements in between, unless they hold a `return`, a label or a local
function, declare or write a name the rest of the test reads, hold the
test's only `await`, or are the only use of a local declared before them;
the pull request lists each such part. What those statements did to the
objects the rest of the test uses is not judged: a failure there shows in the
pull request's checks. A test relying on the default of a fake
built somewhere else (a field, a factory) now runs the kept branch, and a
failure there shows in the pull request's checks.

A call is folded only when its file proves the receiver is that method's class:

- a field, property, constructor parameter (a C# primary constructor's too),
  parameter or local declared with the class (or the qualifying interface) as
  its type, or created as `FeatureFlags(…)`, `new FeatureFlags(…)` (also as
  `var x = new FeatureFlags(…)` in C#), `mockk<FeatureFlags>()` or
  `mock(FeatureFlags.class)`;
- in C#, `var x = services.GetRequiredService<IFeatureFlags>()` (and
  `GetService`, `GetRequiredKeyedService`, `GetKeyedService`), when
  `Microsoft.Extensions.DependencyInjection` is in scope and no type of yours
  declares a method of one of those names (a C# file that does not parse
  counts as declaring any name it spells);
- `this.field` of such a field;
- the class's name, for a `static` method or a member of an `object` or
  companion object;
- no receiver at all, inside the class itself.

The type must reach the class through the file's own package or its imports.
No argument of a folded call may contain a call or an assignment, because
folding the call drops its arguments.

A C# member name may also be declared on other types. Call the wrapper's
class, its interface and the interface's other implementations its types. A
mention of the name counts only when it may be this member. It does not count
when:

- its file's project cannot reference a project declaring one of the types
  (a `.cs` file outside every project included);
- its file spells none of the types' names, no alias of one, no member or
  type whose signature leads to one, and no `dynamic`;
- its receiver's declared type is another type;
- the same receiver is also read with a member none of the types declares (not
  counted when a file of yours spells `dynamic` or declares an `extension`
  block);
- it is assigned to, and none of the types declares a setter;
- it is called, and the member is a property.

A mention that counts and whose receiver is not proven, as above, keeps the
wrapper (`receiver-unproven`). A read through a receiver typed as another
implementation is `sibling-receiver` in production code (tests: see below).

A string literal spelling the name counts as an `unparsed-mention` only in a
file whose project can reference the types. There it counts when the file
spells one of them (as above), or spells a lookup by name: `GetType()`,
`GetProperty`, `GetMethod`, `GetField`, `GetMember` (and their plurals and
`GetRuntime…` forms), `InvokeMember`, `TypeDescriptor`, `PropertyDescriptor`,
`DataBinder`, `new Binding(…)`, `Expression.Property` or
`Expression.PropertyOrField`. When any such file passes one of those lookups a
name that is neither a string literal nor a `nameof(…)`, uses `TypeDescriptor`
or `PropertyDescriptor` at all, or aliases one of those types, a string
spelling the name counts in every file that can reference the types. `nameof`
is not a lookup, but a `nameof(…)` ending in the name counts wherever a string
spelling it would (unless something in your code is itself named `nameof`, in
which case `nameof(…)` passed to a lookup is not taken as a constant). In a
file that spells none of the types, while no lookup anywhere takes a computed
name, only a string or `nameof` passed as a lookup's name counts, unless the
file also spells `GetType()`, a lookup that lists members (`GetProperties`,
`GetMembers`, `DeclaredProperties`, ...), `GetRuntimeMethod`,
`GetRuntimeField`, `FindMembers`, `Type.FilterName`, a `GetDeclared…` lookup,
`Expression.Field`, `Call`, `Property` or `PropertyOrField`, `TypeDescriptor`
or `PropertyDescriptor`, or does not parse. A C# file that spells
`IgnoreCase` (`BindingFlags.IgnoreCase`, `StringComparison.OrdinalIgnoreCase`,
...) or holds a prefix pattern such as `"IsX*"` (as `GetMember` takes)
counts every string naming the member in any case, or by such a prefix, even
where it never spells the name itself, except a string passed straight into
a method of another file (`Check.True(Allowlist.Has(Kind.X, "IsX"))`: the
call is a statement of its own, or an argument of one, its qualifier names a
type, its other arguments read only constants of such types, and nothing on
the way spells `IgnoreCase`), which is judged where that method is. A prefix (`"*"` included) counts only
while some C# file of yours spells a lookup that takes one: `GetMember`,
`GetMembers`, `FindMembers` or `Type.FilterName`. A string spelling an accessor's name
(`"get_IsX"`, `"set_IsX"`) or the backing field's (`"<IsX>k__BackingField"`)
names the member too.

A lookup's name also counts as a constant when it can hold nothing else: a
parameter nothing in its method writes or binds again, of a `private` method
every call to which (in its file, with no method group, qualified call,
overload or string naming the method) passes a constant, or of an xunit
`[Theory]` fed only `[InlineData]` constants and called from nowhere; or a
`foreach` variable over an array of constants, written in place or held by a
local nothing writes, passes or returns. Those constants then count where the
lookup's own would. A lookup on `typeof(C)`, or on a `foreach` variable over
such a local `Dictionary<K, Type>` (its values) or array of `typeof(…)`, takes
any name without counting, when no `C` is, derives from or otherwise leads to
the types (a member of a class does not see an interface's members). The
`Dictionary` must be the framework's: no file of yours declares a type of
that name, and one that does not parse may declare only a name it writes
right after `class`, `struct`, `interface`, `enum` or `record` (any name, if
it spells `delegate`).

A `GetProperty` or `GetRuntimeProperty` with a computed name does not make
strings count when its result is only asked whether it carries an attribute:
`p?.GetCustomAttribute<T>()` compared with `null`, or `p?.IsDefined(typeof(T))`
compared with `true` or defaulted with `?? false`, directly or through a local
used only that way. A deleted property and one with no attribute answer
alike, so this holds only when no deleted declaration (the member, the
interface's, another implementation's) has an attribute or is `virtual`,
`abstract`, `override` or `new`, nothing derives from the classes, and no base
of theirs declares the name or lies outside your code. (`GetMethod` and
`GetField` are not taken: an auto-property's accessors and backing field carry
attributes the compiler adds.) Other by-name APIs (Dynamic LINQ strings, a JSON path,
`Activator`, your own reflection helpers) are not seen, and neither is
reflection that finds the type by scanning assemblies. Files in other
languages (VB and F# apart, which are `other-language`) and documentation are
never mentions. A Razor, XAML or Web Forms file always is. Anything this
cannot read counts as a mention.

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
A wrapper that ends partial keeps its stubs, its `verify` lines and their tests
as they were, since a call left in place still runs against the mock. If every
call was proven but the deletion still keeps the wrapper, the stubs were
already resolved for a removal that does not happen, so every edit for the
flag is rolled back and the flag is refused.

In C#, Moq's `Setup`, `SetupGet`, `Verify` and `VerifyGet` of the member
(`flags.Setup(f => f.IsCheckoutEnabled).Returns(false)`) on a
`Mock<IFeatureFlags>`, and NSubstitute's `.Returns(…)`,
`.ReturnsForAnyArgs(…)`, `Received(…)` and `DidNotReceive()` on a substitute
(`Substitute.For<IFeatureFlags>()`), are resolved the same way: a stub of the
value the flag keeps serving goes, and a test stubbing the value being removed
goes whole. The mock
must be declared with that type, or created as `new Mock<IFeatureFlags>(…)`,
and every binding of its name in the file must be one. No type of yours may
be named `Mock` or `Substitute`, and a C# file that does not parse counts as
declaring any name it spells. In a test project, a
private test helper's `bool` parameter that only chooses a stub's value
(`Returns(enabled)`) is pinned the same way, when every call in the file
passes a literal or relies on a literal default. The stub, the parameter and
the argument at every call go, and a test whose call passes the value being
removed goes whole. A helper that is
not private, is generic, a test, `partial` or has an attribute, a parameter
named in a doc comment or read by anything else, a call that passes something
other than a literal, and a use of the helper other than a plain call each
keep the stub, and the wrapper stays partial.

A test that reads the member through a receiver typed as another
implementation (`var config = new ConfigFlags(o); … config.IsCheckoutEnabled`)
is deleted with that implementation. That needs the wrapper to complete, the
test to be in a test project, the receiver to be a plain name or `this.name`,
every read of the member through that name in the test to be through the
implementation, and every binding of the name the test can see (its own
locals, parameters and patterns, and its class's members) to be typed as the
implementation. The same name may mean something else in another test. A
test whose name another method in the file shares is not deleted.
A wrapper that ends partial keeps all its stubs and these tests as they were.
If every mention was proven but the deletion still keeps the wrapper (its own
read was not folded, a mention is left, or another implementation's member
cannot be deleted), the tests were already changed for a removal that does not
happen, so every edit for the flag is rolled back and the flag is refused.
The pull request lists the tests deleted with an implementation, and each
helper parameter removed, separately from the stubs.
`Mock.Of<IFeatureFlags>(f => f.IsCheckoutEnabled == false)` is not resolved:
its read is reported `receiver-unproven`, the test is left as written, and the
wrapper stays partial.

Some C# shapes are not resolved, and keep the wrapper partial or need a look:

- a test reading the member with no named receiver
  (`new ConfigFlags(o).IsCheckoutEnabled`) keeps the wrapper partial;
- a read through another implementation after `?` or `??`, after a comment
  ending in `.`, or written `(config).IsCheckoutEnabled`, is not removed, and
  the wrapper stays partial;
- a test is matched by the receiver's name, so a comment or string in it
  spelling `config.IsCheckoutEnabled`, where `config` is proven to be another
  implementation in that file, deletes it too;
- a fake's knob set in a file that names none of the fake, the wrapper, its
  class or its interface (through a factory, say) is not seen, and can leave a
  test failing in the pull request, as can a test relying on the removed
  value as the default of a fake it did not build itself;
- a pack or push made only through a build tool's own API is not seen:
  Cake's `DotNetPack(…)` or `DotNetNuGetPush(…)`, or a Nuke build class. A
  repository that publishes packages only that way reads as packing nothing,
  so a public interface there can qualify. Read the deletion in the diff if
  yours does;
- a `Directory.Build.props` or `.targets` above the repository root is not
  read, and neither is a project's use of a wrapper with no interface through
  a package built from the repository (`<PackageReference>`, not
  `<ProjectReference>`). A mention in such a project can be judged unable to
  reach the wrapper, so read the deletion in the diff if your repository does
  either.

Anything unproven is left exactly as it was, and the pull request (and a dry
run) names its file, line and reason. The method is then kept, and the removal
is reported as **partial** rather than **complete**:

| Reason | Meaning |
| --- | --- |
| `name-not-unique` | The method, its class or the interface method is declared more than once. In TypeScript and JavaScript, any function, method, class field, parameter property, object key, module-level binding or enum member of the name counts, a committed build output included, and so does any string literal equal to it. |
| `overridable` | The method is open, abstract or an override, or its class is extended. In C#, also `virtual`, `new`, or a class with a base type outside the repository. In TypeScript, a class that extends or implements a type the repository does not declare, or an object literal used other than as a member's receiver (`Object.keys(flags)`, a spread, `flags[k]`, passed or re-exported). |
| `interface-published` | The interface it implements is public and its module may be published, or has another implementation. In C#, an implementation in a Razor, Web Forms, VB or F# file, or an interface extending it, counts; any other implementation must be inert instead (see `sibling-not-inert`). |
| `interface-structural` | A Go interface declares a method of the same name, or the wrapper is an exported Go method, which may satisfy an interface outside the repository. Or a TypeScript interface or type literal declares a property or method of the same name. |
| `published` | Dart, PHP, Ruby and Swift only: a public wrapper in a package's `lib/` whose `pubspec.yaml` does not say `publish_to: none`, a public or protected PHP wrapper of a Composer package whose `composer.json` does not say `"type": "project"`, a public Ruby class method of a gem (a `.gemspec` beside or above it), or a `public` Swift wrapper not under a `Package.swift` that declares no `.library` product, which another package may call. Its callers are inlined and it is kept. |
| `receiver-unproven` | Nothing in the file ties the receiver to the method's class. In TypeScript and JavaScript, also a use that hands a function wrapper's module on as an object (see above). |
| `outside-run` | The call, or the interface the method implements, is in a file outside the directories this run rewrites. In TypeScript and JavaScript, also a caller of another file type of the family that is not configured, or that another run of the family also rewrites. |
| `import-unresolved` | The receiver's type names the class, but the file's imports do not reach it. In Go, the call is in another package with no import of the function's, in another module, or through an unaliased import whose path does not end in the package's name, or the call's receiver or an argument is read through such an import or a dot import. In TypeScript and JavaScript, a default import, a re-export or barrel, a package name, or a wrapper whose module does not export it directly (an `export *`, an `export { name } from` or a `module.exports` there). |
| `argument-has-call` | An argument holds a call, a lambda or an assignment. In Go, a receive (`<-ch`) or a dereference (`*p`) counts too. In TypeScript and JavaScript, `new`, `await`, `yield`, `delete`, a spread or `++`/`--` counts too, and an arrow or function argument counts only when its body holds one of these or a call. So does a parameter default of the wrapper that may run code, whether or not the call passes that argument. |
| `unremovable-local` | Go only: folding the call would leave a local unused that the cleanup does not remove (see above), and Go does not build with an unused local. |
| `nil-receiver` | Go only: the call reaches a value-receiver method through a pointer that may be nil, so it panics there, and folding it would remove the panic. |
| `other-language` | The call is in a Groovy or Scala file, in a Java file calling a Kotlin method (or the reverse), in Java, Groovy or Scala naming a Kotlin getter (`getEnableX`), or in Kotlin reading a Java getter as a property (`flags.enabled`), or a VB or F# file naming a C# wrapper. |
| `unparsed-mention` | The name appears in a string (reflection, SpEL), a template, a layout, a Razor view, a C# `cref`, a `//go:linkname` comment, the name of a Go `Example` test, or another file this tool does not parse. Also a Go call in an external test package that declares a named `Example`. For a Java or Kotlin getter method with no parameters (`isEnableX()` or `getEnableX()`), the bean property name a template or SpEL reads it by (`enableX`) counts too, in a string or in a file this tool does not parse. |
| `declaration-file` | TypeScript and JavaScript: the wrapper is a JavaScript function or member that its own hand-written declaration file declares (`flags.d.ts` beside `flags.js`), and that declaration cannot go with it: it shares a `declare const` with another name, or a TypeScript run of the same removal also edits the file. Its callers are inlined, and the wrapper stays with its declaration, for you to delete together. |
| `not-folded` | The read inside the method was not folded, because its file could not be processed. |
| `test-double` | A stub of the method this run could not resolve. In TypeScript and JavaScript, one stub of the wrapper that cannot be resolved, or any stub of a wrapper with a call that is not inlined, refuses it before a caller is inlined, and none of its stubs is edited. So does a stub of its whole class, a stub of an object it cannot rule out, an auto-mocking test bed or runner config, or a module mock of a target that does not resolve to a file or a declared package. In Python, any double of it refuses it (see above), so does a mock of a method's class. |
| `sibling-not-inert` | Another implementation of the interface returns the value being removed, calls something, is `virtual`, is shared with another interface, or is otherwise not inert (C#). Reported at that implementation. |
| `sibling-receiver` | Production code reads the member through a receiver typed as another implementation of the interface (C#). |
| `annotated` | The method carries an annotation a framework may call it through (`@GetMapping`, `@Bean`, `@JvmName`, …), so it is kept even with no caller left. A Go function marked `//export` is kept the same way, and so is a TypeScript or JavaScript method with any decorator. |

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

**What a complete C# gate leaves behind is removed too.** Once a C#
wrapper with an interface is complete, its member is gone everywhere, but the
interface, the wrapper class and each inert implementation are left empty,
and so are their dependency-injection registrations and the constructor
parameters that take the interface. These are removed as well, all together
or not at all:

- the interface, the wrapper class and every inert implementation, and a file
  that declares nothing else;
- each registration statement that names only those types
  (`services.AddScoped<ICheckoutGate, FeatureflipCheckoutGate>();`, the
  `TryAdd`, keyed, `typeof(…)` and `Replace(ServiceDescriptor.…)` forms, or a
  factory building one from resolved services). An `if` or `else` arm left
  empty is named in the pull request;
- in each class that takes the interface in its one explicit constructor,
  the parameter, the private field it was stored in, the statement that stored
  it, and the argument every caller passes. A constructor whose only parameter
  that was, written `=> _gate = gate;`, is deleted, and C# supplies the
  parameterless one;
- in test projects, each `Mock<T>` or `Substitute.For<T>()` of these types
  that is only passed to such a constructor, each test that only builds one of
  them and asserts a literal, and a test class those leave empty, or that the
  run's test-double edits left empty;
- in test projects, a field declared as a test fake and initialised by
  nothing, `new()` or `new Fake()`, when every use of it is something else
  listed here (registering it as the instance, `services.AddSingleton<I>(Gate)`,
  included) and nothing outside its class can see it (it is `private`, or its
  class is a `private` nested one); a `<see cref="Gate"/>` naming it becomes
  `<c>Gate</c>`;
- a call of a test host's own registration helper with these types
  (`Replace<I>(services, new Fake())`), when the helper is a `private` generic
  method of the same class whose body only calls `RemoveAll<T>()` and
  `AddSingleton`/`AddScoped`/`AddTransient` (or `TryAdd…`) on its two
  parameters;
- a conventional middleware whose `Invoke`/`InvokeAsync(HttpContext, …)`
  took one of these types and now only passes the request on: its fields are
  private, its one constructor only stores its arguments, and its method ends
  `await _next(context);` after nothing but locals no one reads and `if`s
  holding only those, whose every call is made through a library type's name
  (`Guid.TryParse(…)`) or on the request's context with no extension method
  of its name in your C# (`context.Request.Path.StartsWithSegments(…)`). Before the run, those locals must have been read only in
  the arguments of the calls of the deleted member, and each `if` must have
  held such a call: they computed the gate's targeting values, and what they
  call is assumed to do nothing else. The class goes with each
  `app.UseMiddleware<…>()` registering it, and each test class of a test
  project every test of which builds it, itself or through a helper of the
  class declared to return it;
- a `//` comment block standing alone above a deleted registration, which
  names the flag or one of the deleted types;
- a `<see cref="…"/>` naming one of them becomes `<c>…</c>`;
- the options class an inert implementation read (`_options.Enabled`), with the
  statement binding it to a section (`services.Configure<GateOptions>(
  configuration.GetSection("Gate"))`, `AddOptions<T>().Bind(…)` or
  `.BindConfiguration(…)`), or, when the class holds other settings, only that
  property; and the `Gate:Enabled` key in each `appsettings*.json` of a project
  that loads the binding. The edit keeps the file's comments and commas;
- each `using` of a namespace these deletions leave empty.

Every mention of each deleted type is looked for by text in every file of the
repository (build output under `bin` and `obj` aside). Each must be one of the
above, or a comment, Markdown text or the source of another language outside
every C# project's directory (another tool's fixture, say), which is listed
in the pull request and left as it is. Anything else keeps all of it as it was: a string naming the
type, a configuration or Razor file naming it, a use not listed above, an
assembly scan whose filter could include the types, a public constructor in a
project that may be packed, a project `<Using>` of an emptied namespace. The
pull request then says what kept it, and the flag is still removed. When only
the options class or its configuration cannot be followed (the key set as an
environment variable or in a chart, a string reading the section, a binder
setting that rejects unknown keys), that part stays and the rest goes.

Two results to know about:

- a `using` of an emptied namespace is deleted on the assumption that no
  assembly outside the repository declares types in your own namespace;
- the SDK client the wrapper class received is no longer created through it.
  If nothing else asks the container for it, it is created later, or not at
  all.

What still keeps it, by design, until it can be proven:

- a constructor taking the interface is kept, with everything else, when its
  class is also named in `typeof(…)`, built with a target-typed `new(…)`
  that has the same number of arguments, or has a second constructor. A
  target-typed `new(…)` does not count in a project that cannot reference the
  class, or where its context names the type it creates: the arrow body or a
  `return` of a method, local function or property that is not `async`, an
  explicitly typed declaration, or an element of a `new List<T> { … }` or
  `new HashSet<T> { … }` initializer (taken to be the framework's when no
  file of yours declares one), naming a type of another name that no
  `using` alias and no type parameter takes. A `typeof(…)` does not count
  when it is a positional argument of your own `sealed` attribute, named
  without a qualifier, deriving from `Attribute` alone with a primary
  constructor and no other, that keeps it only in a get-only `Type`
  property; every file that may hold the attribute reads that property only
  to compare it (`==`, `!=`) and spells no string of its name; and no C#
  file of yours that references the attribute's project, loads an assembly
  or a type by name at run time, or is a source generator, reads attribute data raw
  (`CustomAttributes`, `GetCustomAttributesData`, `CustomAttributeData`,
  `ConstructorArguments`, `TypeDescriptor`), reads a member's value by
  reflection (`GetValue`, `GetGetMethod`, `PropertyInfo.GetMethod`,
  `CreateDelegate`, `Expression.Property`, `dynamic`), invokes a method it
  looked up anywhere but on `typeof(X)` of another type, or takes
  attributes other than as one named type (`GetCustomAttributes(true)`,
  `GetCustomAttributes<Attribute>()`). Library code that reflects over every
  attribute's properties is not seen;
- a binding whose configuration is not provably the root (a local, a
  `GetSection(…)` result, a parameter some caller passes something other
  than a host's `Configuration` or a configuration it built itself with
  `new ConfigurationBuilder()…Build()`) keeps the options class and its keys;
- a setting of an options class shared with other settings is kept when a
  value of the class is passed on, returned, serialised or reflected over,
  except straight into `new C(Options.Create(new O { … }))` where `C`'s one
  constructor declares that parameter `IOptions<O>`;
- an emptied test class with a base class, an attribute or another part, or
  one named elsewhere, is kept, emptied;
- a file is deleted only when nothing but namespaces, comments and its own
  `using`s would remain; an `[assembly: …]` attribute, a `global using` or an
  `extern alias` keeps it;
- build output under `bin` and `obj` is not searched for mentions. A
  comment that named only a deleted type goes (see "the mention is not a
  flag read" under the caveats for what "only" means); one that says more is
  listed, not rewritten, so its prose may be stale.

**A gate that takes no key and is not inlined is reported, not rewritten.**
That is every such gate outside Kotlin, Java, C#, Go, TypeScript,
JavaScript and Python, and one of theirs that does not qualify, such as a gate whose
interface has a second implementation that is not inert:

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
longer overrides anything. In TypeScript and JavaScript, when that `{}` is the
last argument of a call to a function declared at the top level of the same
file, and the matching parameter defaults to `{}` (`flags = {}`), the argument
is left out: the call passes the same value without it. The pull request lists
each call it shortened, and each other override it left empty
(`<Provider flags={{}}>`), because the prop or argument carrying it may now be
pointless; whether it can go depends on whether it is optional, which this
tool cannot see from one file.

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
| C# | A `const string` in a class, struct, record or interface, including one nested in another type (`FeatureFlags.Checkout.NAME`, spelled from the outermost type). `Owner.NAME`, with `Owner`'s namespace enclosing the reader or imported by a `using` in the file, in the enclosing namespace block, as a `global using`, or as a `<Using>` item in a project file; `Alias.NAME` under `using Alias = Ns.Owner;` at file level (or `global using`, or a project `<Using Alias="...">`); bare `NAME` under `using static Ns.Owner;` at file level. The read is the first argument of a `BoolVariation(...)` call, of an awaited `GetBooleanValueAsync(...)`, or of an accessor you named. A read spelled with its namespace (`Ns.Owner.NAME`, `global::Ns.Owner.NAME`), a `partial` owner, and a constant declared `static readonly` are left alone, and the declaration is kept. A type taken whole keeps every key constant it owns, since reflection reaches a constant without spelling it: `typeof(Owner)` (but not as a logging category, `GetLogger(typeof(Owner))`, `ForContext(typeof(Owner))`, `CreateLogger(typeof(Owner))`), `nameof(Owner)`, `Owner` as a type argument (`All<Owner>()`, but not a logger's or an equality contract's: `ILogger<Owner>`, `Logger<Owner>`, `CreateLogger<Owner>()`, `ForContext<Owner>()`, `IEquatable<Owner>`, `IComparable<Owner>`), or a string holding its qualified name (`Type.GetType("Ns.Owner")`, `"Ns.Owner, Assembly"`, `"Ns.Outer+Owner"`) or exactly its simple name (`t.Name == "Owner"`), in any file, the owner's own included, each refused as `unsupported-form`. The same holds for an outer type of a nested owner, a repository type deriving from the owner, and, except in a string, an alias of either. Types are matched by name, so a same-named type in another namespace keeps the declaration too. In a Razor view, XAML, VB, F# or other .NET file the Action does not parse, any spelling of one of those types not followed by a member access (`typeof(Owner)`, `GetType(Owner)`, `typeof<Owner>`, `{x:Type local:Owner}`, `"Ns.Owner"`) keeps it the same way; `Owner.OTHER` there reads only `OTHER`. Not seen: a name assembled from parts (`"Ns." + name`), escapes in the string, `GetType()` on an instance, a scan of every type (`assembly.GetTypes()`), and a derived type outside the checkout |
| Dart | A top-level `const` or `final`, or a `static const`/`static final` member of a class, mixin or extension type. Bare `NAME` through an `import` of the declaring file (a relative URI or `package:` of the reader's own package), `show` and `hide` honoured; `p.NAME` through `import '...' as p`; `Owner.NAME` and `p.Owner.NAME` for a member. The read is the first argument of a non-null-aware `boolVariation(...)` call or of an accessor you named. A read through a re-exporting library, a conditional or deferred import, another package of the repository, a typedef, or from a `part` file is left alone, and the declaration is kept. A file that imports or exports `dart:mirrors`, the declaring file included, keeps every key constant of the run, refused as `unsupported-form`: `reflectClass(Owner).declarations`, a library's top-level constants through any class of it, and `currentMirrorSystem().libraries` all reach a constant without spelling it. An import the fold leaves unused is removed, and a `show` naming the deleted constant is trimmed |
| Java | `Owner.NAME`, with `Owner` in the same package or imported (`import pkg.Owner;`, `import pkg.*;`); bare `NAME` under `import static pkg.Owner.NAME;` or `import static pkg.Owner.*;`. The read is the first argument of a `boolVariation(...)` call or of an accessor you named |
| Kotlin | `Owner.NAME` for a `const val` in a top-level `object`, or in the `companion object` of a top-level class (`Owner` is the class; `Owner.Companion.NAME`, or `Owner.Keys.NAME` for a companion named `Keys`, works too), with `Owner` imported or in the same package; bare `NAME` (or its `as` alias) under `import pkg.Owner.NAME`, `import pkg.Owner.Companion.NAME` or `import pkg.NAME`, or in the same package for a top-level `const val`. The read is the first argument of a `boolVariation(...)` call or of an accessor you named. A subclass of the class can read a companion's constant without naming it, so a constant read that way is kept |
| TypeScript / TSX / JavaScript | an `export const` read through `import { NAME }` (or `as`), `import * as ns` (`ns.NAME`), `const ns = require(...)`, or `const { NAME } = require(...)`. Relative specifiers, and `paths` (against its `baseUrl`) from the nearest `tsconfig.json` or `jsconfig.json`, written there or inherited through `extends`, are followed; a bare specifier that only `baseUrl` would resolve is not. The read is the first argument of a non-optional `boolVariation(...)` or `useFeatureFlag(...)`, an awaited `getBooleanValue(...)`, or an accessor you named |
| PHP | A single-element `const` in a class, interface or enum declared at the top of a file or namespace block and not `private` (typed or not), or a namespace-level `const`. `Owner::NAME` with `Owner` imported (`use Ns\Owner;`, `use Ns\Owner as Alias;`, `use Ns\{Owner, ...};`), in the reader's namespace, fully qualified (`\Ns\Owner::NAME`), qualified through an imported namespace, or `namespace\`-relative; class names match in any case. Bare `NAME` under `use const Ns\NAME;` or in the constant's own namespace (a global constant only from a file with no namespace). The read is the whole first argument of a `boolVariation(...)` method or function call, or of an accessor you named. Left alone, and the declaration kept: a read through a subclass, an implementer, a `class_alias`, or `self::`/`static::`/`parent::` that may reach it (inside a closure, always), a `use const ... as Alias`, a qualified constant name (`\Ns\NAME`), a named argument, a read inside string interpolation, the name in a string (`constant('...')`), a heredoc or a docblock annotation, a Blade view, a Twig, Latte, Smarty or Volt template or Symfony YAML/XML or Nette configuration that spells it, and a file holding PHP under another suffix (`.theme`, `.install`, `.profile`, `.engine`, `.ctp`, `.php7`). A trait's constant is never resolved across files. A file under a Composer `vendor/` directory (beside a `composer.json`) is never rewritten; it keeps the declaration only when it names the owner itself, and it competes only when it parses. A class taken whole keeps every key constant it owns, since reflection, `constant()` with a computed name and `$cls::{$n}` all reach a constant without spelling it: `Owner::class`, a dynamic fetch (`Owner::{$n}`) or a string naming the class (`'Ns\Owner'`, `'Ns\Owner::' . $n`, `'Ns\Owner::NEW_' . $x`, `"Ns\\Owner::$n"`, the same in a heredoc or nowdoc or with a `b` prefix, and with double-quoted escapes such as `\x4B` decoded; a class in the global namespace only when the string starts with a capital, `'Owner'`, or holds `::`) in any PHP file, the class's own included, and the same for a class proven to inherit the constants (`Sub::class`) and, inside the class or one of those, `static::class`, `self::class`, `$this::class`, `__CLASS__`, `get_called_class()`, `get_class($this)`, `get_parent_class()`, `static::{$n}` or `'static::'`, each refused as `unsupported-form`. `static`, `$this` and a trait's `self` count for every class that may stand for them, a base class's `static` included. A string holding one of the class's constants' whole names after `::` reads that constant alone. An instance reaches every constant too (`new \ReflectionObject($k)`, `get_class($k)`, `$k::{$n}`), and where it goes is not followed, so creating one (`new Owner()`, `new static()`), a parameter, property or return type naming the class (which a container autowires), and `$this` handed on or returned inside such a class each keep the declaration. A service that declares its own flag constants and is constructed or injected therefore keeps them. A namespace constant is kept the same way when its name is built at run time: a string that starts its qualified name and holds a backslash (`'Ns\\' . $n`, `"Ns\\$n"`, `'Ns\\NEW_' . $x`), `__NAMESPACE__` inside that namespace, and `get_defined_constants()`, which lists every user constant and so keeps every namespace and global key constant of the run. A Twig, Latte, Smarty or Volt template, a Blade view, a file holding PHP under another suffix, or Symfony configuration with a constant marker keeps every key constant of the class too, refused as `unsupported-form`, when its text holds the class's name followed by `::` and anything but one of its constants' whole names, a static property or a method call (`constant('Ns\\Owner::' ~ name)`, `constant(\Ns\Owner::class . '::' . $name)`, `Owner::{$n}`, `"Ns\\Owner::$n"`), or a quoted string that is the class's qualified name (`'Ns\\Owner' ~ '::' ~ name`, `new ReflectionClass('Ns\Owner')`). It is matched by text, a bare class name by its short name or by an alias the text imports it as (`use Ns\Owner as K`, `@use('Ns\Owner', 'K')`), and the file is never rewritten. Not seen: a template under a plain `.html` suffix (Craft CMS), an inheritance through a class outside the checkout or under Composer's `vendor/`, and a class name assembled from parts |
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

A TypeScript or JavaScript key constant is also kept when any file hands the
module that declares it on as an object. Code holding that object can reach
the constant without spelling its name (`Object.values(K)`, `K[name]`), so
deleting it would change what that code sees and still compile. This applies
to a namespace import or `require` of the module used as anything but
`ns.<name>` or a type, and so to it being cast, passed, spread, indexed or
re-exported. It also applies to `export * as`, a dynamic `import()`,
`import m = require(...)`, and a glob loader that can load the file. A
specifier reaches the module however it is spelled, through a `paths` alias
too, and a Vue, Svelte or Astro component or an MDX document counts. Each
such use is reported as `unsupported-form`. Such a component or document is
never rewritten, so one that spells the constant's name anywhere, an import
of it included, keeps the declaration the same way. The constant's proven
reads are still folded; only the declaration stays. An `export * from` the
module (a barrel) is followed rather than refused, so a namespace of the
barrel handed on keeps the declaration too.

A Ruby key constant is not resolved across files, so its declaration is kept
whenever another file in the checkout holding Ruby could read it, and Ruby can
read a constant without spelling its name. That is a `.rb`, `.rake` or `.erb`
file, a view (`.jbuilder`, `.jb`, `.builder`, `.rabl`, `.arb`, `.axlsx`,
`.prawn`, `.rhtml`, `.haml`, `.slim`), a project file (`Rakefile`, `Gemfile`,
`Guardfile`, `Capfile`, `Thorfile`, `Vagrantfile`, `Podfile`, `Fastfile`,
`Appfile`, `Dangerfile`, `Brewfile`, `Berksfile`, `Steepfile`, `config.ru`, a
`.gemspec`, `.podspec`, `.gemfile`, `.thor` or `.rbw` file), or a file with no
suffix whose first line runs it with Ruby (`#!/usr/bin/env ruby`, as in
`bin/`). HAML and Slim are not parsed, so a template keeps the declaration
whenever it names the constant or the owner. The declaration is
kept when another file spells the constant together with its owner, or with a
class that inherits it: one that subclasses the owner or includes, extends or
prepends it, at any depth, including a `Struct.new do ... end` or
`Class.new do ... end` block assigned to a constant (`Sub::FLAG_KEY`). It is
also kept when another file reaches the owner, or such a class, as a whole:

* reflection on it, such as `Gate.const_get(name)`, `Gate.constants`,
  `Gate.const_defined?(name)`, `Gate.send(:const_get, name)`,
  `Gate.class_eval`, or the same through an instance
  (`Gate.new.class.const_get(name)`). A lookup of a literal name, such as
  `Gate.const_get(:OTHER)`, reads that name only and keeps nothing;
* the owner handed on as a value: passed (`register(Gate)`), assigned, put
  in an array, interpolated, or turned into something that is still the owner
  or names it (`name`, `to_s`, `dup`, `tap`, `ancestors`, `method`);
* a string or symbol that a lookup can complete: the owner's whole name
  (`"Gate"`, `:Gate`, `"#{ns}::Gate"`), `Gate::` followed by an interpolation,
  a format directive or an appended string (`"Gate::#{name}"`,
  `"Gate::" + name`, `%W[Gate::#{name}]`), or any mention in a string handed to
  `eval`, `class_eval`, `module_eval` or `instance_eval`;
* the owner reopened (`class Gate`, `class << Gate`, or a class inheriting
  it) with `constants`, `const_get`, `ancestors`, `class_eval` or
  `self.class.constants` inside, or with the class's own `self` handed on
  (an instance's `self` is not);
* a file that does not parse and names the owner.

The file declaring the constant is asked the same: an owner listing its own
constants (`def self.all = constants`, `const_get(name)` in its body,
`self.class.constants`, or `Gate.constants` after it) keeps them.

A top-level constant has no owner, and `const_get` finds one from any class,
so a `const_get` or `constantize` of a computed name anywhere keeps it, and
so does `Object.constants`. Each of these is logged with the file and line
that reaches the owner. These keep nothing: comparing the owner
(`x.is_a?(Gate)`, `when Gate`), rescuing it, calling its own methods
(`Gate.new`, `Gate.enabled?`, `Gate.send(:enabled?)`), handing on an instance
of it, a spec or double of it (`RSpec.describe Gate`, `allow(Gate)`,
`class_double("Gate")`), and `class_name: "Gate"`. Not inspected: Ruby in
a file under a suffix or name not listed above, a lookup whose owner name
arrives from data (a YAML file, an environment variable) or through a
computed `send`, reflection
through a relative of the owner (`descendants`, `subclasses`, `superclass`,
`module_parent`), and an owner reached through a class outside the
checkout.

A Java or Kotlin key constant is also kept when any file takes its owner
whole. A key constant is a static field of its owner (a companion's `const
val` is one of the class around it), so reflection over the class lists it
without spelling its name, and deleting it would change what that code sees
and still compile. This applies to a class literal (`RuntimeKeys.class`,
`RuntimeKeys::class`, qualified or through an import alias), to a string
holding the class's binary name (`Class.forName("app.RuntimeKeys")`, or
`"app.RuntimeKeys$Companion"`), and to the same for any repository class that
may extend the owner, since `getFields()` lists a superclass's public static
fields too, in any file, the owner's own included (`Keys.class.getFields()`
inside `Keys`). For a Kotlin `object`, or a class whose companion holds the
constant, it also applies to the object handed on as a value (`fun dump(): Any
= RuntimeKeys`, `RuntimeKeys.Companion`, or `RuntimeKeys.INSTANCE` from Java)
or used as the receiver of anything but one of its own members
(`RuntimeKeys.javaClass`, `RuntimeKeys.let { }`, and from Java
`RuntimeKeys.INSTANCE.getClass()`). A
file in any of the four JVM languages counts. Each such use is reported as
`unsupported-form`; the constant's proven reads in other files are still
folded and only the declaration stays (a read inside the owner's own file stays
with it). A class literal is matched by the class's name, so one handed to
your own code (`FlagRegistry.register(CheckoutGate.class)`) counts. One that is
the first argument of a test double or a logger does not, because those never
read a class's static fields: `mock`, `spy`, `any`, `isA`, `forClass`,
`getLogger`, `getLog`, Spock's `Mock`, `Stub`, `Spy` and their `Groovy`
variants, and MockK's `mockk`, `mockkClass`, `mockkObject`, `mockkStatic` and
`spyk` (so `mockkObject(CheckoutGate)` does not count as handing the object
on either). In Java, the class's name handed to one of those counts no more
than the literal does (`Logger.getLogger(CheckoutGate.class.getName())`, or
`getSimpleName()`, `getCanonicalName()`). Neither does a class named in Spring's `@Import`, which registers
it as a bean, or in a Spring test double's annotation (`@MockBean`, `@SpyBean`,
`@MockitoBean`, `@MockitoSpyBean`, springmockk's `@MockkBean` and `@SpykBean`),
as any of their arguments (`@Import({Other.class, CheckoutGate.class})`,
`types = CheckoutGate.class`). An annotation of your own still counts. Not
seen: an instance of the owner handed on (`new
RuntimeKeys().getClass()`), a class name assembled from parts, reflection
inside the owner or a class extending it through `getClass()` or
`this::class`, and the facade class of a Kotlin top-level constant
(`Class.forName("app.KeysKt")`).

Groovy (`.groovy`, `.gvy`, `.gy`, `.gsh`, `.gradle`, and `.gsp` views) and
Scala (`.scala`, `.sc`, and `.scala.html`, `.scala.txt` and `.scala.xml`
templates) files can read a Java or Kotlin constant too, and this tool neither
rewrites nor parses them, so a mention there is refused as `other-language`
whenever it might be a read: the file names the owner, a subclass of it or an
alias, reads the name bare (a static import or an inherited constant), or
qualifies it with anything other than a class the repository declares, the
file imports or shares a package with, and never uses as a variable. A Spock spec reading `CheckoutGate.FLAG_KEY`
keeps `CheckoutGate`'s constant; one reading `SearchGate.FLAG_KEY` from an
unrelated class does not. Read the same way, any other mention of the owner's
name, or of a class that may extend it, keeps every key constant it holds
unless it declares, extends or constructs that class, names it as a type
(`RuntimeKeys keys = Mock()`, `go(RuntimeKeys keys)`, Scala's `keys:
RuntimeKeys`), reads one of its members (`RuntimeKeys.OTHER`,
`RuntimeKeys.describe()`), or hands it to a test double (`Mock(RuntimeKeys)`,
ScalaMock's `mock[RuntimeKeys]`): `RuntimeKeys.fields`, a Groovy map value
(`[gate: RuntimeKeys]`),
`classOf[RuntimeKeys]` and the class handed on all count, refused as
`unsupported-form`.

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
through `sys.modules` by a computed name, or by an import path built at run
time or named in a string by anything but its whole dotted name
(`import_module(".keys", "pkg")`, `import_module("keys")`); another module, or
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

A Python module is a value, so a file can reach every constant in it without
spelling one. Any Python file of the checkout that binds the declaring module,
or a package holding it (`import pkg.keys`, `import pkg.keys as k`, `from pkg
import keys`, `import pkg` or `import pkg as p` and then `p.keys`, `from pkg
import *`, which binds a loaded submodule), and uses it as anything but a
member read (`k.OTHER`, `pkg.keys.OTHER`) keeps every key constant of that
module: handed on (`register(keys)`, `register(pkg)`), stored, iterated,
passed to `getattr(k, name)`, `getattr(pkg, "keys")`, `setattr`, `vars(k)` or
`dir(k)`, or read through a dunder such as `k.__dict__`. Each is refused as
`not-a-read`, as the same use is in a file that spells the name, however the
chain is written (split over lines, continued with `\`, a comment inside it).
That includes a test patching the module (`monkeypatch.setattr(keys, ...)`,
`patch.object(keys, ...)`). Such an import in a class body, which makes the
module a class attribute, is refused as `unsupported-form`. A star import of
the declaring module (`from pkg.keys import *`, or `from . import *` beside a
declaring `__init__.py`) is refused as `import-unresolved`, since it puts
every constant into a namespace the Action does not follow, and a string
whose whole text is the module's dotted name
(`importlib.import_module("pkg.keys")`, `sys.modules["pkg.keys"]`,
`logging.getLogger("pkg.keys")`) as `unsupported-form`. A member read keeps
nothing. Such a file also counts as naming the module's constants when the
in-file rules alone would otherwise decide them (see below). A file is read
for this only when its text names the module (`keys`, or the package for an
`__init__.py`), so a package handed on by a file that never names the module
and a namespace package (one without an `__init__.py`) are not seen. The
declaring module is asked too: one that imports and hands on itself keeps its
constants.

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
any language, spells one of its key constants or reaches the module whole,
nothing elsewhere reads them,
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

A key held in an enum entry is resolved too, in Dart, Java, Kotlin, PHP, Python, Swift and TypeScript:

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
| Swift | a case of an `enum` declared at the top level or nested in a struct, class, enum or actor, in a module read from `Package.swift` or an Xcode project; the enum and every type around it are not `private` or `fileprivate`, carry no attribute and sit outside `#if`, and the case is alone in its `case` clause with no associated values. Either a raw value, `enum Flags: String` (or `Swift.String`) with `case X = "key"` as a plain single-line string, or a computed `var key: String` (any name; not `static`, `private` or `fileprivate`, no attribute, outside `#if`, declared in the enum's own body) whose getter is one `switch self` whose arms name the enum's cases as `.X` only (no binding, `where`, `_` or other member; `default:` is fine) and whose arm for `X` is `return "key"` or just `"key"`. A raw-value enum's other conformances must be protocols the repository declares or `Equatable`, `Hashable`, `Comparable`, `CaseIterable`, `Codable`, `Encodable`, `Decodable`, `CodingKey`, `Sendable`, `Identifiable`, `CustomStringConvertible` or `CustomDebugStringConvertible`, and nothing in the repository may declare a `rawValue` the enum could pick up instead of its own: in the enum's body, in a protocol it conforms to, or in any extension at all, of any type and however it is spelled (an extension of an unrelated type counts too). With either shape, no member of the enum, and no member of an extension that could reach it, may carry an attribute other than `@available`, `@objc`, `@nonobjc`, `@discardableResult`, `@inlinable`, `@usableFromInline`, `@inline`, `@frozen`, `@preconcurrency`, `@warn_unqualified_access`, `@_disfavoredOverload`, `@Sendable` or `@MainActor` (the last only while the repository declares no macro named `MainActor`): an attached macro there may add members | `Flags.X.rawValue`, or `Flags.X.key` through the property, with `Flags` resolved as a constant's owner is: in the enum's module with no import, from another module after a plain `import` that allows the access. Also `Outer.Flags.X.rawValue` for a nested enum, and `Module.Flags.X.rawValue` when no type, member or local named like the module is in scope. An implicit member (`.X`, anywhere in the repository, `case .X:` included), `Flags.X` passed as a value, another member of the case (`.hashValue`), a read inside the enum's own file, and a read in a module holding an extension of the enum that declares a member named like the accessor are not resolved (`unsupported-form` or `not-a-read`). Neither is a read whose `Flags` (or `Module.Flags`) a typealias in the repository may stand for: one in the reading module, at the top level or inside a type, and for `Module.Flags` one declared anywhere (`import-unresolved`) |
| TypeScript / TSX / JavaScript | an `export enum` (or `export const enum`) declared once in its file, with `X = "key"`; or an `export const Flags = { X: "key" } as const` object (see below) | `Flags.X` (or the alias's text) through `import { Flags }`, `const { Flags } = require(...)` or `const { Flags: F } = require(...)` (read as `F.X`); `ns.Flags.X` through `import * as ns` or `const ns = require(...)` |

An entry holds the key when its arguments (or a TypeScript member's
initialiser) spell the key as a whole token: `"new-checkout-v2"` is another
flag's key, while `PREFIX + "new-checkout"` still holds this one and makes the
entry `enum-unproven`.

A Swift case holds the key when its raw value, or the literal of a switch arm
naming it, spells it. Two Swift reads are still resolved and can differ. An
extension of `RawRepresentable` (or of a protocol on the list above) in a
library, declaring `rawValue`, gives every raw-value enum that value: the
same extension in this repository is seen and leaves the case in place, but
a library's cannot be. And `Module.Flags.X.rawValue` reads a type named
`Module` from an imported library instead of the module, if a library
declares one. A library macro named `MainActor` is not seen either: a
member attribute `@MainActor` is taken to mean the global actor, so a case
beside it is proven.

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
iterating it), also as a bare `values` inside the enum. In Swift it is
`Flags.allCases`, `Flags(rawValue:)` and `Flags.init(rawValue:)`, also as a
bare `allCases` inside the enum or an extension of it. Such a use does
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
wherever it mentions the enum or its file. An MDX document has no script
block, so only the imports and loads it holds count there, as in markup.
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
| `import-unresolved` | nothing in the reading file ties the name to the declaring file: no import or same-package route, an import that resolves to another file or to none, a TypeScript default import, a re-export through a barrel (`export ... from`), a TypeScript import alias of the constant off a name that is not a namespace import of its file (`import K = m.NAME` under `import m = require(...)`), or a TypeScript enum taken off a name that is not a namespace import of its file (`use(m.Flags)`, `const F = m.Flags`, `import F = m.Flags`) or used as a value through an import that does not resolve (`use(Flags)`), or, in a file that names that enum, a computed lookup (`m[k]`) off a namespace or default import of a script that does not resolve, such as a package (`Icons[name]`; a `.css` or `.json` import is not a script); a Python star import beside the name, an absolute import that two files in the checkout could mean, or a `from pkg import keys` or `import pkg.keys` through a package `__init__.py` that is not plain or with a compiled module beside the declaring one, or a PHP read whose `use` is written after it, a class the checkout does not declare (such as a `class_alias`), or a global constant read from inside a namespace, or a Dart import that re-exports the declaring library, is conditional or deferred, names another package of the repository, or does not resolve, or a Swift read from another module with no plain `import` of the declaring one, an access level the import does not allow, or a second imported module that declares the name, or a Swift `Module.Flags.X.rawValue` whose module name a declaration of that name in the reading module, or in a module it imports, could stand for, or a Swift `Flags` or `Module.Flags` a typealias in the repository could stand for |
| `not-a-read` | the name is used for something other than a flag read: a log line, a map entry, a callable reference (`Owner::NAME`), a TypeScript type-only import, an import left with no read to remove, or a TypeScript enum taken as a value off a namespace import of its file (`use(ns.Flags)`) |
| `rebound` | the reading file binds the same name itself, or an in-repository supertype supplies a member of that name (in Swift, also an enclosing type, an extension of it, or an extension of a protocol it conforms to, declaring a member or generic parameter of the read's first name); or a Python declaring file binds its constant a second time |
| `other-language` | the constant is mentioned, in a way that can reach it, from a file in another JVM language (Java reading a Kotlin constant, Kotlin reading a Java one, or a Groovy or Scala file reading either), from a Razor, XAML, VB or F# file reading a C# constant, from a Twig, Latte or Smarty template or Symfony YAML or XML configuration reading a PHP constant or enum case, or a read of it that would be resolved sits in another file type of the family than the declaration (a `.tsx` or `.js` file reading a `.ts` constant) and is not rewritten with it: its language is not configured, or another run rewrites the file |
| `unsupported-form` | a shape outside the table above, such as a fully qualified `pkg.Owner.NAME` or a `?.` qualifier; the name inside a string or string template; a Kotlin named argument; a renaming `require` destructure (`const { NAME: X } = require(...)`), whose pattern would still name the constant after the read is folded; any Java or Kotlin file that spells the name or its owner but cannot be read or parsed; a mention that can reach the constant from a file outside the directory the run transforms, which is never rewritten and so keeps the declaration alive; a `.kts` script; for an enum entry, a nested enum (outside Swift), a Swift implicit member (`.X`) or a Swift read in a module holding an extension of the enum that declares a member named like the accessor, a quoted TypeScript member, a read of an entry inside the file that declares it, a proven accessor called with anything but an empty argument list (`Flags.X.key(arg)`; a call that is not an accessor, such as `.name()`, is `not-a-read`), a Kotlin read through an alias of the enum's import (`F.X.key` under `import pkg.Flags as F`), a TypeScript spelling of the entry outside the table (`Flags["X"]`, `(Flags).X`, `Flags!.X`, `(Flags as T).X`, `const { X } = Flags`), a TypeScript enum taken off anything but a name (`use(require("./flags").Flags)`, `let F = require("./flags").Flags`), or any other use of a TypeScript enum's file or of the enum that hands it on (see Enum entries); any use that hands a TypeScript key constant's module on as an object (see Declared in another file); any use that takes a Java or Kotlin key constant's owner whole, by reflection or as a value (see Declared in another file); a Python import of the declaring module nested in a function or a `try`, or a sole-name import whose private alias is already used in the file (or sits beside a submodule of that name in a package `__init__.py` reader), or a relative one through a package `__init__.py` that is not plain, or one whose line cannot take a comment; a Python declaring file with a star import or a route to its own namespace; a Swift file whose module cannot be read from `Package.swift` or an Xcode project |
| `declaration-kept` | Python only: a read of the constant that would have been folded, left as it is because another refusal listed beside it keeps the declaration it reads. A Python constant can be reassigned, so a kept declaration keeps every read of it (see the note on partial close-out above). For an enum entry, a read left because its file also reads a kept constant, or because an escape listed beside it can change the member's value (see Enum entries) |
| `enum-unproven` | an enum entry holds the key, but its accessor cannot be proven to return it: more than one constructor, a constructor that writes its own parameter, a non-`final` field or a Kotlin `var`, an entry with a body, a named or computed argument, a TypeScript enum that is not exported directly, is `declare`d or merges with another declaration of its name, a PHP enum that is not string-backed (pure or `int`), a PHP case whose value is not the key as a plain string (concatenated, interpolated or a constant expression), a PHP enum file that does not parse, a Python enum whose class is nested, has another base, a metaclass, a decorator other than `@unique`, a `value`, `name` or `label` method or one whose name starts with `_` (`__init__`, `__new__`), `_ignore_`, or a member valued by code, a Python enum its own module hands to other code, a Python member whose value is not the key as a plain string, or a Python enum file with a star import or a route to its own namespace, a Dart enum with more than one constructor (a factory or a second generative one), a field with an initializer or assigned twice, an argument that is not the key as a plain string (interpolated or concatenated), the key at a position the constructor does not store, type arguments on the entry, no public accessor of the field, a private enum or entry, a Dart enum in a part or in a file that does not parse, or a Swift case whose enum (or a type around it) is `private`, `fileprivate`, attributed or inside `#if`, or has a type around it with a supertype, or sits in a file whose module cannot be read or that does not parse, whose clause declares another case or associated values, whose raw type is not `String` (a typealias of it included), whose raw value is not the key as a plain single-line string, whose enum conforms to a protocol outside the list above or may pick up another `rawValue` (any extension in the repository declaring one counts), or could gain members from an attached macro: on an attributed extension anywhere in the repository that extends the enum, a typealias or protocol the repository declares, a type the enum's clause names, `RawRepresentable`, `Encodable`, `Decodable` or a protocol on the list above, or on a member of the enum or of such an extension carrying an attribute outside the built-in list above, or whose switch property is `static`, `private`, `fileprivate`, attributed, inside `#if`, has a setter, is declared in an extension, or has an arm with a binding, `where`, `_` or a name that is not a case of the enum |
| `enum-dynamic` | not a refusal: a Dart, Java, Kotlin, PHP, Python or Swift use of the whole enum that can reach the entry without naming it. Listed so it can be checked before the flag is archived |
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
  `jsconfig.json` (a webpack or Vite `resolve.alias`, say). A config is read
  with the `paths` and `baseUrl` it inherits from a config it extends by
  relative path, the way the compiler reads them. A specifier is not resolved
  at all, and the mention is refused, when two files could be meant, or when
  the nearest config, or one it extends, cannot be read for certain: it
  extends a package (such as a `@tsconfig/*` preset) or a file outside the
  repository, uses `references`, sets `paths` or `baseUrl` to `null`, sets
  `moduleSuffixes`, repeats a key, or does not parse.

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
into an `else`, turning `if (<read>) { return a; }` into a bare `return a;` or
`if (<read>) { break outer; }` into a bare `break outer;`, or turning a loop
condition into `while (true)` — that statement is **deleted** and listed in the
pull request body. `no-unreachable` is part of `eslint:recommended` and is an
error, so leaving it would open a pull request that cannot go green. A `break`
that exits the loop keeps what follows reachable, and nothing after it is
touched. Hoisted declarations are the exception, since code above them can
still use them: a stranded `function` declaration stays, and a stranded
`var last = x;` becomes `var last;`, losing only the initialiser that could
never run. A stranded destructuring `var` refuses the flag, and so does a dead
`if` branch that declares a `var`: the name is hoisted to the function and may
be read after the `if`, so the branch is not deleted, and the `if` left
testing a literal is refused rather than shipped.

A loop condition the fold makes constant is finished the way your lint
expects, because `no-constant-condition` rejects every constant loop except
`while (true)`:

- served **true**, `for (init; <read>; update)` becomes `for (init; ; update)`
  and `do { … } while (<read>);` becomes `while (true) { … }`;
- served **false**, `while (<read>)` is deleted, `for (init; <read>; update)`
  is replaced by its initialiser (nothing for a `let` or `const` whose values
  cannot run code, the declaration for a `var`, the expression for an
  assignment), and `do { … } while (<read>);` is replaced by its body, which
  runs once either way. The body keeps its braces when anything in it is scoped
  to the block: a `let`, `const`, class, function or `using`, even inside a
  callback. Each loop removed is listed in the pull request body.

A `do` whose body has an unlabelled `break` or `continue` (even one that belongs
to a loop inside it) or a comment outside its braces, a `for` whose
`let` or `const` initialiser runs code, and a loop that is not directly in a
block (an unbraced `if` body, a labelled loop, a `switch` arm) are not
rewritten, and the flag is **refused** instead of opening a pull request your
lint fails. As with unreachable statements, a file that already had a dead
loop is left alone. See [Known gaps](#known-gaps).

Two leftovers are removed without a mention, since nothing of yours goes with
them: a label whose last `break label` or `continue label` the fold removed
(`no-unused-labels`, and TypeScript's TS7028), and a `static { … }` block the
fold emptied (`no-empty-static-block`). `<read> ?? fallback` folds to the
literal, since a boolean is never nullish. Your own empty static blocks are
kept, and so are your own unused labels, except one directly around code the
fold changed.

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
becomes `require("./ff");` for the same reason. A `require` destructuring that
binds other names too only loses the stranded one, with its comma
(`const { client, Client } = require("./ff")` becomes
`const { Client } = require("./ff")`), as long as the pattern holds nothing
but plain names. The pull request body names
each `import` and `require` kept this way, with the exact line, so a reviewer
can delete the ones whose module does nothing when it loads.

The statement is deleted outright in three cases, because none of them loaded
anything to begin with: an `import type`, an import whose names the file only
ever used as types, and an import of a module that another import in the same
file still loads. It is also deleted when the module is a file in the
repository, named by a relative path or a `tsconfig.json` `paths` alias, and
loading it provably does nothing: that file, and every module it loads in turn,
only declares types, functions, classes and constants built from literals. A
module of flag-key constants is the usual case. If anything in that graph calls
a function, builds an object with `new`, uses a decorator, loads a package or
cannot be found, the bare import stays.

The bare import is also deleted when the file's other imports already load
everything in that graph that does work. A barrel such as `@/lib/flags` often
loads a store or a client module that the same file reaches anyway through its
other imports, so the bare import would load nothing new. Every module in the
graph that is not provably inert has to be loaded by another import kept in the
file, directly or through the files that import loads. A package counts when the
other import names the same package from the same `package.json`. An import
counts only if the compiler keeps it: one that binds nothing, or one whose name
is read as a value and declared nowhere else in the file. An import used only as
a type, or one whose every read could be a local of the same name, does not
count, and neither does a re-export of a type. Imports in other files that
merely load the same module do not count either, because this tool cannot know
which files share a program or which loads first. One thing can change: when
only an import further down the file reaches a module, that module now loads a
few lines later. That matters only if some module needs another module's
load-time work done first without importing it.

A `paths` alias is read through a solution `tsconfig.json` (one with
`references`, as in Vite's template) when every referenced config whose `files`
or `include` may hold the importing file reads it the same way. If they
disagree, if none of them may hold the file, or if the solution config could
compile the file itself, the alias counts as not found.

The Featureflip JavaScript SDKs are the one exception to "loads a package".
`@featureflip/js`, `@featureflip/node`, `@featureflip/browser`,
`@featureflip/react` and `@featureflip/react/testing` do nothing when they load,
in every published version, so a stranded import of one of them is deleted
outright, and a repository module that loads one can still count as inert. Only
those five specifiers count. Any other `@featureflip/` package keeps its bare
import. The name is trusted only when it can mean nothing but the published
package, so the bare import stays when:

- a `tsconfig*.json` or `jsconfig*.json` in the importing file's directory or
  any directory above it has a `paths` pattern that fits the name, or a
  `baseUrl` with a file that answers it, either written there or inherited
  through `extends`. A config extended by relative path is read the way the
  compiler reads it. The bare import also stays when a config, or one it
  extends, cannot be read for certain: it extends a package (such as a
  `@tsconfig/*` preset) or a file outside the repository, uses `references`,
  sets `paths` or `baseUrl` to `null`, sets `moduleSuffixes`, repeats a key, or
  does not parse;
- a file such as `@featureflip/react.ts` sits in the importing file's directory
  or any directory above it, where the compiler's classic module resolution
  looks;
- anything in the repository can put other code under the name of any of the
  four SDK packages, since they load each other (`@featureflip/node` loads
  `@featureflip/js`, `@featureflip/react` loads `@featureflip/browser`). That
  means a `package.json` that is one of them (a workspace member or a vendored
  copy), depends on one with anything but a registry version (`file:`, `npm:`,
  `link:`, `workspace:`, a Git URL, a `.tgz`), or names one anywhere else, such
  as `overrides`, `resolutions` or a Jest mapper. It also means a
  patch-package patch for one, a committed `node_modules` holding the
  `@featureflip` scope, or a bundler, Babel, Jest, Deno or pnpm config or an
  HTML page that mentions one;
- a directory in the repository is a symlink to somewhere outside it, or a
  file the check needs cannot be read.

An alias written anywhere else, such as a build script, is not seen.

CommonJS gets the same treatment. A stranded `const client = require("./ff")`
or `const { client } = require("./ff")` whose module qualifies is deleted rather
than kept as `require("./ff");`. Two shapes keep the `require` line even then:
an array pattern (`const [client] = require("./ff")`), which runs the exports
object's iterator, and any file that defines its own `require` or uses the name
other than to call it.

A value `import client = require("./ff")` whose last use was folded is deleted
when its module qualifies. Otherwise it is left as it is, because no single
spelling keeps its load under every compiler setting, and ESLint's
`no-unused-vars` will report it.

When a deleted statement was the file's last `import` or `export`, it becomes
`export {};` instead. An `import` or `export` is what makes a file a module, and
without one the file would turn into a script, where top-level `await` does not
compile and top-level names become globals. `export {};` keeps the file a module
and loads nothing. The pull request body names each file where this happened.

A `private` or `#` class member whose every read was a flag read the fold
removed is left unread. With `noUnusedLocals` on, `tsc` reports it: TS6138 for
a constructor parameter property (`constructor(private client: Client)`),
TS6133 for a field, method or accessor. ESLint's recommended
`no-unused-private-class-members` reports a `#` member in JavaScript too.

A constructor parameter property is removed, together with the argument every
caller passes for it, when every mention of the class in the repository's
TypeScript and JavaScript can be accounted for: `new Svc(…)`, a subclass's
`super(…)`, a type annotation, an import or export, or `instanceof`. A named
subclass with no constructor of its own (`class Child extends Svc {}`)
inherits Svc's, so every mention of `Child` must be accounted for the same
way, and its `new Child(…)` calls lose the same argument. Each
argument removed must be a literal, a name, `this` or a property access, so
deleting it cannot skip work, and a local that only fed it is cleaned up
like any other the fold strands. A `//` comment at the end of a removed
argument's or parameter's own line goes with it. Every other comment stays.
The parameter stays, and is named in a warning of its own, when any of these
holds:

- the class, its constructor or the parameter has a decorator, which is how
  Angular, NestJS and other dependency-injection containers construct it;
- the class is a default export, imported under another name, or used as a
  value (`useClass: Svc`, `[Svc]`, `typeof Svc`, `<Svc />`, `new this()`);
- a call spreads its arguments or passes one that may do work
  (`new Svc(createClient(), log)`);
- a subclass with no constructor of its own is a class expression
  (`const Child = class extends Svc {}`), or any of these holds for it;
- the class, or a subclass that inherits its constructor, reaches it through
  `this.constructor` or uses `this` in a static member (`const C = this;
  new C(…)`), where `this` is the class itself;
- the constructor is overloaded, or still reads the parameter by its bare name;
- a file that uses the class does not import it from the file that declares
  it (a package's class of the same name, or one re-exported through another
  module);
- a `.vue`, `.svelte` or `.astro` component, an `.mdx` document, or an
  `.html` or `.htm` page,
  mentions it (a page whose only script is `<script src="dist/app.js">` does
  not);
- a file holds the string `'Svc'` and constructs something through a computed
  expression (`new m['Svc'](…)`, `new (registry.get('Svc'))(…)`,
  `Reflect.construct`);
- a caller lies outside the directory being cleaned;
- a file that mentions the class is ignored by git, or does not parse.

A class constructed through a name the repository never spells, such as
`new m[key](…)` with `key` computed at run time, cannot be seen. If your code
does that, add the flag to `ignore` or check the pull request's callers
yourself: the argument such a call passes would shift by one. The same holds
for inline script in a server-side template (ERB, PHP, Jinja, a Razor view),
which is not read either.

A `private` or `#` field is removed when deleting it cannot skip any work:

- it has no initializer, or an inert one: a literal, a name, a property access
  such as `Defaults.retries`, or an array or object literal made of those
  (`#enabled = false`, `private items: Item[] = []`);
- nothing writes it except, at most, one statement of the constructor's own
  body, `this.flags = flags;`, whose right side is a constructor parameter or
  a literal. That statement is deleted with the field.

When that statement was the only thing reading the parameter, the parameter is
removed too, with the argument every caller passes for it, under every
condition listed above for a parameter property. If a caller cannot be proven,
the field stays and is named in the warning, rather than leaving a parameter
nothing reads. A `/** … */` comment directly above the field, a
`@ts-expect-error`, `@ts-ignore` or `eslint-disable-next-line` comment above
it, and a comment on its own line go with it. The pull request lists each field it removed.

The field stays, and is named in the warning, when its initializer may do
work (`= new Client()`, `= createClient()`, `= this.client`, a template with
`${…}`), when it is decorated, `declare` or `accessor`, when something other
than that one constructor statement assigns it, or when deleting it or that
statement would let the code above continue into the code below (a field or
statement with no `;` followed by one starting with `[`, `(` or `*`). A
parameter that is itself a property (`readonly flags`, `public flags`) is never
removed this way; only the assignment goes. A `private` field also stays when any
file indexes something with its name as a string (`svc['enabled']`), a
component's or HTML page's inline script included, since
`private` is checked only by `tsc`. A `#` field cannot be reached that way.
Code that reaches a `private` field through `any` (`(svc as any).enabled`) or
lists an instance's own properties (`Object.keys(svc)`, `{ ...svc }`,
`JSON.stringify(svc)`) cannot be seen, and stops seeing the field.

If the class is exported, code outside this repository may construct it too,
so the pull request lists it under "Constructor signature changed", along with
each exported subclass that inherits its constructor.

A plain constructor parameter (`constructor(c: Client)`, not a property) can
be left unused as well, which `tsc` reports as TS6133 when `noUnusedParameters`
is on. The usual case is a subclass that only passed it on: `super(c, l)` loses
`c` once the base class's parameter is removed. Such a parameter is removed the
same way, with the argument each of its callers passes, and so on down a chain
of subclasses. It stays, and is named in a warning of its own, under any
condition listed above for a parameter property, or when it has a default
value, since a caller that leaves the argument out runs that default.

Any other member stays, and the pull request names each one so you can remove
it. A member that
was already unread before the fold is not listed, and neither is a `protected`
or `public` one, which code elsewhere may still read. A member assigned with
`this.client = c` but never read counts as unread, as it does for `tsc`.

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
* every local of a function that spells any name outside ASCII. Python
  compares names after NFKC normalization, so `ｒetries` (with a fullwidth
  `ｒ`) reads `retries`;
* a dunder such as `__class__`, which the language reads itself (a
  zero-argument `super()` reads `__class__`);
* a value that looks literal but raises: `~` on a number (`~1.5` and `~2j`
  raise, so no `~` is taken), and a container inside a set or used as a dict
  key (`{[1]}` raises, since a list is unhashable);
* the only statement in a block (`if ready:` needs a body);
* a statement that shares its line with another (`n = 3; go()`), or that the
  line above continues into with `\`;
* a function's first statement when a bare string follows it, parenthesised
  or not (`("doc")`), since that string would become the docstring.

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
**reassigned**, passed as an `out` or `ref` argument or written by a
deconstruction is left standing as `bool useNew = true;`, and `VariationDetail<T>(…)` — a different name
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
withholds the whole file. A key constant in a type declared in the same file as
its reader may be read through that type (`Flags.NewCheckout`), when the type is
not nested and not generic, sits in the reader's namespace, and nothing else in
the file declares that name.

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
no longer reads stays, along with the constructor that sets it. In C# that
also means a method stays when deleting it would leave a field of the file
that the compiler warns about, an error under `TreatWarningsAsErrors`: one only
the method assigned (CS0649), or one only the method read (CS0169, CS0414).

A primary-constructor parameter is the one place that costs a build. When
every read of `client` in `class Svc(IClient client)` was a flag read the fold
removed, the parameter is left unread and the compiler warns CS9113, which is
an error under `TreatWarningsAsErrors`.

The parameter is removed, together with the argument every caller passes for
it, when every mention of the type in the repository's C# can be accounted
for: `new Svc(…)`, a derived type's `: Svc(…)` or `: base(…)` (a derived
constructor without one, or the one C# supplies to a derived type that
declares none, calls `base()` with no arguments, which has nothing to
remove), the type's own
`: this(…)`, a type position, `nameof(Svc)`, or a generic type argument such
as `services.AddScoped<Svc>()` (the container resolves constructor parameters
by type, not position). A caller's argument for the parameter is found by
position, or by name when the call names it (`new Svc(log: l, client: c)`),
and a call that leaves a defaulted parameter out has nothing to remove. Each
argument removed must be a literal, a name, `this`, `default` or a member
access. A `//` comment at the end of a removed argument's or parameter's own
line goes with it. The parameter stays, and is named in a warning
of its own so you can remove it or suppress the warning, when any of these
holds:

- a call passes an argument that may do work, names an argument no parameter
  of the primary constructor has, or passes a positional argument after a
  named one out of its position;
- `typeof(Svc)` or `ActivatorUtilities` can construct it positionally;
- a file holds a string naming the type (`"App.Svc"`, `"App.Svc, App"`) and
  calls `Activator.CreateInstance` or `GetType("…")`;
- it is an attribute class applied as `[Foo(…)]` or `[FooAttribute(…)]`;
- an XML-doc `cref` names its constructor (`<see cref="Svc(IClient)"/>`),
  which is CS1574 once the signature changes;
- a target-typed `new(…)` anywhere in the repository passes a number of
  arguments the constructor could take;
- a Razor, `.cshtml` or other non-C# .NET file mentions it, or a `using`
  alias names it;
- a spelling of the name may be another type: qualified with a different
  namespace, or unqualified in a file that neither sits in the type's
  namespace nor imports it (with `using`, `global using` or an MSBuild
  `<Using>` item);
- a generic call passes arguments while naming the type
  (`new Mock<Svc>(client, log)`, `Substitute.ForPartsOf<Svc>(…)`), which a
  mock or proxy library uses to construct it positionally;
- a caller lies outside the directory being cleaned;
- the parameter has an attribute (`[FromKeyedServices]`) or a modifier, or the
  type is generic;
- the type declares another constructor that takes parameters (a call may be
  bound to it rather than to the primary constructor), or every parameter the
  primary constructor keeps is optional (it has a default value or an
  attribute, which may be `[Optional]`) while the type declares a
  parameterless one or is a `struct`, so that a call left with no arguments
  (`new Svc()`) could reach a different constructor than before;
- a derived type is `partial`, so its constructors may be in another file.

Removing the argument a derived type passes in `: Svc(c, l)` can leave that
type's own parameter `c` unread, which is CS9113 again. That parameter is
removed the same way, with the argument each of its own callers passes, and so
on down a chain of derived types. When its callers cannot be proven, it is
named in the warning instead.

A type constructed from a name the repository's C# never spells, such as one
read from configuration and passed to `Type.GetType`, cannot be seen, and
`Activator.CreateInstance` then throws `MissingMethodException` at run time.
If your code does that, add the flag to `ignore`.

If the type is `public`, code outside this repository may construct it too, so
the pull request lists it under "Constructor signature changed". A parameter
that was already unread before the fold is not listed. Neither is one of a
`record`, whose parameters become properties, or of a `partial` type, whose
other parts may still read it.

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

A collection-`for` whose element is such an `if`
(`[for (final i in xs) if (<read>) i]`) folds too. Served true, the `for` keeps
its element. Served false, the whole `for` goes, since a `for` with no element
does not parse, but only where dropping the loop cannot change anything: a
plain `for`-`in` over a parameter of the enclosing function declared `List` or
`Set`, in a literal whose element type the source states. Anywhere else the
flag is refused.

A local bound to the read is inlined and its declaration removed; one that is
later **reassigned** is left standing. A short-form string template that reads
it is rewritten with the other reads, as the braced literal:
`'enabled=$enabled'` becomes `'enabled=${true}'`. One followed directly by an
escaped dollar sign (`'$enabled\$'`) keeps the local, as its literal.
`stringVariation` / `numberVariation` /
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

A private field (`_name`) whose every read was in the removed code is left
unread, which `dart analyze` reports as `unused_field`. It is deleted, with
the `///` comment above it (and any `//` line between the two) and a comment
on its own line, when nothing else in its library reads or assigns it and its
initializer cannot do work: there is none, it is a literal, a `const`
expression or a collection literal built of those, it calls with constant
arguments a constructor that only builds the object
(`final _client = FeatureClient();` where `FeatureClient`, declared in the
same file or in a file of the repository it imports, has a
`const FeatureClient();` with no `assert`, or a `FeatureClient();` or `FeatureClient() {}` with
no initializer list, in a class with no `extends` or `with` clause and no
field initializer that may do work), or the field is `late` or `static`, whose
initializer only runs when the field is first read. So a field holding a
keyless wrapper class built that way (`final _flags = Flags();`) is deleted
once the wrappers it was read for are.

When the field was the last use of a name an import brings, the import goes
too: one of a library in the repository when nothing else it brings is
spelled, not even a member of an extension it declares (otherwise the field
is kept), and one of another package (`package:featureflip/featureflip.dart`) only when
it has no prefix and a single `show` list whose every name goes with the
field. What any other import of another package brings, its extensions
included, cannot be read, so that field is kept.

When a constructor sets it, that goes too:

* a `this._client` parameter is deleted, and so is the `[...]` around it when
  nothing else is in the brackets;
* an initializer-list entry (`: _client = client`) is deleted, with the `:`
  when it was the only entry. When `client` is a parameter of that
  constructor that nothing else in it uses, the parameter goes as well, with
  its `required` and its default value, or the whole `{...}` when it was the
  only named parameter.

A constructor that loses a parameter changes for every caller, so each call
in the repository loses its argument: by position, or by name for a named
parameter. That covers `Svc(…)`, `Svc.named(…)`, `Svc.new(…)`, `new` and
`const` calls, calls through an import prefix, a redirecting `: this(…)` and a
subclass's `super(…)`. This happens only when every use of the class can be
accounted for: a private class (`_Svc`) is checked in its own file, any other
class in every Dart file of the repository that imports its library,
directly or through a library of the repository that exports it. A file that
does not import it names some other class of that name (a `Timer` from
`dart:async`), and is left alone.

The field is kept, and named in the pull request body, when:

* its class is public in `lib/` of a package that may be published (no
  `publish_to: none` in `pubspec.yaml`), since another package may construct
  it;
* the class is used some other way: as a value (`Svc`), as a torn-off
  constructor (`Svc.new`), through a `typedef`, in a part file, in a
  redirecting factory or a mixin application
  (`class B = Svc with M;`), or by a subclass with a `super.` parameter; or
  the class name is declared twice, or a file of the repository imports
  `dart:mirrors` or does not parse;
* the class, the constructor or the parameter carries an annotation (other
  than `@immutable` and `@deprecated`), since a code generator or an injector
  may construct it;
* a constructor sets it from something other than a parameter or a literal,
  sets it through a named `this._client` parameter, or is a factory;
* an argument that would be deleted is anything but a literal, a `const`
  object, a call with constant arguments of a constructor that only builds
  the object (as for an initializer, below), a parameter of the function
  around the call, or a top-level `const` of the file (`Svc(widget.client)`
  may run a getter, `Svc(FeatureClient(config))` reads a variable that may be
  initialised on first use, and a local would be left unused), or is the last
  use of an import or a private declaration;
* something else assigns it, or its initializer may do work
  (`final _client = FeatureClient();` whose constructor has a body,
  an initializer list or a superclass, `Foo.instance`, `'$x'`);
* it carries an annotation, is `external` or `abstract`, shares a declaration
  with another field, or shares its name with a getter, setter or method;
* its type or initializer is the last use of an import that cannot go with
  it (`Timer? _timer;` as the only `Timer` in a file that imports
  `dart:async`, or `final FeatureClient _client;` under a plain
  `import 'package:featureflip/featureflip.dart';`) or of a private class,
  function or variable, since deleting it would leave that unused instead;
* its library has `part` files, since another part may read it.

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
binding removed. A local that is written more than once (`x = true if admin`,
`x ||= y`), whose reads reach past the branch or block that writes it, or that
is an instance variable (`@x`, visible to the object's other methods) is left
standing as `x = true`. A write used as a **value** (`if (x = <read>)`,
`x = <read> or log(…)`, a method's last statement) is replaced by its value, so
the expression keeps what it evaluated to. `string_variation` / `number_variation` / `json_variation` are never matched —
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

A dead `if`, `unless` (the modifier forms `x if <read>` and `x unless <read>`
included), subject-less `case` or `while` that was the **last
statement of a method or block** becomes `nil` rather than disappearing. That
statement is what the method returns, and `nil` is what it returned before;
deleting it would hand the return value to the statement above. At the top of
a file, in a class body, an `ensure` clause or a loop body, where that value is
never used, it is deleted.

An `if` or `unless` used as a **value** (`x = if …`, an argument, an array or
hash element) folds only when the branch it keeps is one simple expression: a
literal, a name, a collection, or a call with no arguments or with
parenthesised ones. Otherwise the run **refuses** the file and exits 1 naming
it, rather than ship an `if true` with the flag key already gone. The refused
shapes are several statements kept as a value or behind an inline `then`
(`if <read> then a; b end`), a call whose arguments have no parentheses
(`foo(if <read> then bar 1 end, 2)` would become `foo(bar 1, 2)`, which Ruby
reads as `foo(bar(1, 2))`), and a dead branch in an argument. Fix those by
hand, or add the key to `ignore`.

A local that only the removed branch read is cleaned up too. RuboCop's default
`Lint/UselessAssignment` fails on it, and `ruby -w` warns. In a method,
including in a block or lambda inside it, a local holding a literal (a number, `nil`,
`true`, `false`, a string without interpolation, a symbol, or an array, hash
or range of those) is deleted, along with a comment on the same line, and so is
an only `x ||= <literal>` or `x &&= <literal>`. One that calls something keeps
the call: `flow = LegacyFlow.new(ctx)` becomes `LegacyFlow.new(ctx)`. A
method's or block's last statement keeps its value, since that is what it
returns: `total = 80` there becomes `80`. A local nothing read even before,
whose name the removed branch spelled as a method or a hash key
(`obj.retries`, `g(retries: 1)`), is cleaned up the same way. Some locals are
left alone:

* the last statement of any other body (an `if` branch, a loop, a `begin`)
  holding a literal, and a statement before a `rescue`, `else` or `ensure`
  (a call is still kept, which returns the same value);
* locals in a block or lambda outside any method or in a class body, at the
  top of a file, and a block's own `|a; x|` locals;
* every local of a method that uses `binding`, `local_variables` or a
  string `eval`, by name or as a symbol (`ERB.new(template).result(binding)`
  and `send(:binding)` read them all);
* a value that is neither a literal nor a call with a receiver, arguments or
  a block: a bare name (which may be a method or a local), a constant (which
  can autoload a file, and which RuboCop flags when it stands alone), an
  interpolated string, a heredoc, an operator, an index, or a hash using the
  `key:` shorthand, which reads `key`;
* a value that looks literal but raises: `~` on anything but an integer
  (`~1.5`), and a range whose ends do not compare (`1..'a'`, `!1..2`);
* a local next to a range left open at the end of a line (`x = 1..`), which
  Ruby continues onto the next line;
* any other write (`+=`, `||= compute`, `a, b = …`);
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
leaves a blank line where each tag line stood. A tag written with trim markers
takes what they trimmed with it (the newline after `-%>`, the indentation
before `<%-`), so a `.text.erb` template renders exactly as before.

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
inlined and removed only when it is `private` (read bare or as `this.name`);
any other property keeps its declaration as the literal (see "What is
deliberately NOT rewritten"). An `if` used as an expression whose surviving
branch holds more than one statement is **refused**, since its last statement
is the value and splicing the branch flat would change it; a surviving branch
that is an unbraced loop body keeps its braces.

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

A short-form string template that reads an inlined `val` is rewritten with the
other reads, as the braced literal: `"$ok yes"` becomes `"${true} yes"`, in a
raw string too. A `val` whose name is in backticks is not inlined and keeps its
literal. A `when` whose subject is the read
(`when (<read>) { true -> … false -> … }`) is refused: the read becomes a
literal and the `when` around it is left standing.

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
  `init` block or a property accessor, unless the value is a literal (a
  number, a character, a string without `$`, `true`, `false` or `null`), which
  is deleted there too. A lambda's last statement stays even then, because it
  is the lambda's value;
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
backticks still counts. An aliased import (`import a.b.C as D`) goes only when
neither `D` nor `C` is spelled anywhere in the file: it also hides `C` from the
star imports, so while a bare `C` is left, removing it could change what `C`
means. Some imports are never removed: star imports, and imports of a name
Kotlin calls by convention without spelling it, such as `getValue` (used by
`by`), `plus` (used by `+`), `invoke` or `component1`.

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
`useNew := client.BoolVariation(…)` — is inlined and removed. When the same
function also spells that name for something other than a read of it (a
`range` variable, an `if` initializer, a function literal's parameter, a later
`n, useNew := …`, an assignment, a struct literal key, or `&useNew`), the local
is not inlined: `useNew := true` is left standing with its reads, which is
valid Go and the same program. One shape is **refused** instead, and the run
exits 1 naming the file: a read of an outer `useNew` (a package-level variable,
say) **above** the local's declaration, which inlining would rewrite too.
Rename one of them and the next run cleans the file. A read bound beside
another name (`a, useNew := load(), <read>`) leaves the other binding as it
was: `a := load()`.

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
too, at package level or inside the function. The read as the call a `go` or
`defer` statement runs (`go client.BoolVariation("KEY", ctx, false)`) is
discarded just the same and is deleted under the same rule; `go true` would
not build.

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
to discard the value, so whatever it called still runs. A local declared with
`var` is treated the same way, including a spec on its own line inside a
`var ( … )` group, and one name of `var a, b T` goes on its own. A variable
declared by a statement header leaves the header instead: `for _, v := range us`
becomes `for range us`, `for i, v := range us` becomes `for i := range us`,
`case v, ok := <-ch:` becomes `case _, ok := <-ch:`, and `switch v := x.(type)`
becomes `switch x.(type)`. Two names declared together go the same way when
the fold strands both: `a, b := 3, load()` becomes `_ = load()`, and
`var a, b = 1, 9` goes. So does a local whose name something else in its
function also spells, such as a function literal's parameter, a local of the
same name in another block, or a struct literal key like `Opts{retries: 1}`.
Some declarations are not removed at all: two names when one of them is still
read, three or more names declared together, a `:=` that also redeclares an
existing name, several names on one spec inside a `var ( … )` group, and a
local whose name is also a struct literal key whose type cannot be resolved
in the file. If a fold leaves one of those declared and unused,
no pull request is opened for the flag: it is refused as `[unsafe-rewrite]`,
and the output names the file, the local and the function it is in. Remove or
use that declaration by hand (or add the key to `ignore`) and the next run
cleans the flag.

A label whose last `break`, `continue` or `goto` the fold removed is removed
too, since Go rejects an unused label; the statement it labelled stays where it
was.

Fold output itself is `gofmt`-clean, including a multi-statement branch, and so
is what a removed import or declaration leaves behind (no blank line just inside
`import (`, no two blank lines in a row, no blank lines at the end of the file),
so a repo gating on `gofmt -l` will not flag the pull request over the fold. A
comment on a removed import's line goes with it, and so does the comment heading
a section of imports when that import was the section's only one. A comment at
the end of a removed local's line goes with the local, and a comment on its own
line above the local stays.

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
left standing. PHP's logical operators always yield a bool, so where one folds
away in a value position (`return $x && <read>`, an array element, an
assignment) the operand it keeps is cast: `return (bool) $x;`. In a condition,
where only truthiness counts, it is left bare.

An `if` without braces folds the same way, so `if (!<read>) return 1;` becomes
`return 1;` or goes. A `match` folds where the read is a condition: an arm of
`match (true) { <read> => …, default => … }` becomes that arm's value or goes,
a `match` left with only its `default` becomes the default's value, and a
`match` on the read itself becomes the arm the value selects. A value that sits
inside a larger expression keeps its parentheses, so
`match (true) { <read> => $a + 1, default => 0 } * 3` becomes `($a + 1) * 3`.
Two shapes refuse the flag instead. One is an arm that lists a call before the
literal (`audit(), true => …`), since picking the arm would skip the call. The
other is a dangling `else`: in `if ($a) if (<read>) { … } else { … }`,
including one behind a brace-less `foreach`, `for` or `while`, PHP gives the
`else` to the inner `if`, and folding that `if` away would hand it to the outer
one.

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
  `get_defined_vars()`, `eval()`, `parse_str()`, `include`/`require` or
  `$$name` in any spelling, all of which reach variables without naming them,
  or that names `assert`, `mb_ereg_replace` or `mb_eregi_replace` anywhere
  (an `Assert::` class included), which on PHP 7 can run a string that reads
  them as code;
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
not a safety refusal. A `var` bound to the read is left the same way when it
is later assigned something other than a literal, has a method called on it
(`useLegacy.toggle()`) or is passed `inout` (`&useLegacy`): it keeps its
declaration, `var useLegacy = true`.

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
| A key literal that only **starts or ends** with the key — interpolated (`f"KEY{env}"`, `"KEY$env"`, `"KEY\(env)"`) or split by an escape (`"KEY\x2dv2"`) | no change at all — its value at run time is a different key, so it reads a different flag. The same holds for a key constant initialised that way |
| A C# `const string` key that is **also** read by anything other than a flag read | no change at all — not even the read is folded. Deleting the declaration would strand the other reference, and folding without deleting would leave a `const` the next run reports as a live reference forever |
| A C# `const string` key whose name is **bound again** anywhere in the file | no change at all — a shadowing binding means a reference may resolve to the other one, and the key could be read off the wrong declaration |
| A key held in a REASSIGNABLE binding (C# `static readonly`, a JS `let`, a Kotlin `var`, a non-`final` Java field) | no change at all — its value cannot be read off the declaration, so the flag it names is not knowable |
| A key hoisted to a constant in a DIFFERENT file, in a language other than C#, Dart, Java, Kotlin, Swift, TypeScript, TSX, JavaScript, Python or PHP, or read in a shape outside [Declared in another file](#declared-in-another-file) | no change to the reader, and the declaration is **kept**. The pull request lists each such mention with its reason |
| A C# read inside an **expression tree** (`mock.Setup(c => c.BoolVariation("KEY", …))`) | no change to the read — the lambda *describes* the call instead of performing it, so the text of the read is the payload a mocking library reads at run time. This one is not detected by naming `Setup`/`Verify`: the tell is that the read's receiver is the lambda's own parameter, so any library taking an `Expression<>` is covered, while a genuine `Func<>` lambda (`list.Where(x => flags.BoolVariation("KEY", …))`, reading off a captured client) is still rewritten. If the key is a `const`, the whole const path is withheld for that file — the mock setup is a live reference to the declaration. Once the run has folded a real read of the key, a `Setup` returning a literal and a `Verify` are resolved first, before this applies (see "Tests that mock the flag read") |
| A read handed to a **mocking DSL** that takes the call itself — `when(<read>).thenReturn(…)` (Mockito, mockito-kotlin, `package:mockito`, ts-mockito), `given(<read>).willReturn(…)` (BDDMockito), `every { <read> } returns …` (MockK), or `when(() => <read>)` / `verify(() => <read>)` (`package:mocktail`) | when the run folds a real read and the stub returns a literal, **resolved first**: the stub or its whole test is deleted (see "Tests that mock the flag read"). Otherwise **the whole flag is refused**, and the run exits 1 naming the file. These libraries stub by *performing* the call and capturing the invocation it registers, so the text of the read is the payload — the same property that makes the C# row above untouchable. Folding it leaves `when(true).thenReturn(true)`, which **compiles**: the stub silently stops stubbing, and the flag key is gone from the file so no later run looks again. Unlike C#, there is nothing structural to key on — no lambda exists, because the call is real — so the guard is a short list of stubbing entry points by name. mocktail's `() => <read>` form is the exception to the first point: the lambda is there but only wraps the call. Delete or rewrite those lines yourself, scope the run with `directories` to exclude your tests, or add the key to `ignore` |
| A **Go read inside a gomock expectation** (`m.EXPECT().BoolVariation("KEY", ctx, false).Return(true)`) | resolved first in the same way as the row above, when it can be. Otherwise **the whole flag is refused**, and the run exits 1 naming the file. Same shape as the row above, caught a different way: the fold leaves `true.Return(true)`, and because Go's `bool` is a builtin with no method set, a selector on a boolean literal is broken Go *by construction* — so this one needs no list of library names. Before this was refused it shipped, and the customer's build was what reported it |
| A **Java field or Kotlin property** bound to the read that is not `private` (`public final boolean enabled = <read>`, a bare `val enabled = <read>`) | read replaced, **declaration kept** → `public final boolean enabled = true;` / `val enabled = true`. Other files read it (`checkout.enabled`), and they do not contain the flag key, so this tool never sees them. Removing the field would break files it never parsed. A `private` one is inlined and removed, read bare or as `this.name`; one also read through another receiver (`other.name`), which may be another instance, keeps its declaration. In Java that holds only for a top-level class: a `private` field of a nested, local or anonymous class is kept the same way, because the enclosing class can read it (`settings.on`). Delete the field yourself once its readers are gone |
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
| A **multi-way guard** — Go's tagless `switch { case <read>: … }`, Ruby's subject-less `case` / `when <read>`, Kotlin's subject-less `when { <read> -> … }`, or `switch (true) { case <read>: … }` in PHP, TypeScript and JavaScript | **rewritten.** Each arm is a condition and the first true one wins, so this is an `if`/`else if` chain by another spelling. Where the flag serves **false** the dead arm is removed, in any position. Where it serves **true**, an arm with nothing live above it collapses the whole statement to its body (Go and Ruby), and an arm with a live arm above it becomes the terminal `default:` / `else`, taking the arms below it — they sit behind a guard that now always passes. Kotlin collapses only where that cannot change meaning — an expression body in a slot that takes any expression, or a braced body in statement position that declares nothing — and otherwise takes the `else` form, which also keeps a `when` used as a value exhaustive. Ruby collapses a `case` used as a value (`x = case … end`) only when that arm is a single simple expression (a name, literal, call or collection); a longer body there is refused, because spliced in place it would assign only its first statement. PHP, TypeScript and JavaScript always take the `default:` form, because a body ending in `break;` is a syntax error outside a switch, or silently becomes a loop's `break` inside one. A switch on a **value** (`switch ($v)`, `case v`, `when (v)`, `switch (v)`) is untouched: there the arms compare values rather than act as guards. In TypeScript and JavaScript, a switch whose **subject is the read** (`switch (<read>) { case true: … case false: … }`) is finished the same way: the arm matching the value served becomes the `default:`, and an arm that can never match is removed, which TypeScript requires (`case true:` under `switch (false)` is error TS2678) |
| A multi-way guard the fold **cannot** finish | **the whole flag is refused**, and the run exits 1 naming the file. In Go: a `fallthrough` anywhere in the switch, a `break` in a body that would be lifted out of it, or a `switch v := f(); {` initializer whose binding the arms use. In Ruby: a `when` listing several patterns (`when <read>, isAdmin(ctx)`), where a literal among alternatives is not the whole condition. In PHP: an arm before the flag's that does not end in `break`/`return`/`continue`/`goto`/`exit`, because PHP falls through **by default** and that arm runs into the flag's with nothing in the source saying so — an arm ending in `throw` or `die()` counts as not terminating, since the grammar cannot tell either from an assignment. In TypeScript and JavaScript: the same fall-through rule, except that `throw` does count as terminating (it is its own statement there), as does a braced arm body ending in a jump. In Kotlin: an arm listing several conditions (`isAdmin(ctx), <read> ->`), or a `when` whose only arm is dead and which is not a statement of a block — a lambda's last statement is its value. In TypeScript and JavaScript: a dead arm sharing its line with another arm |
| A switch or match whose **subject is the read** in Go, Python, Ruby or Swift (`switch <read> {`, `match <read>:`, `case <read>`, including Ruby's pattern-matching `case <read> in …`), a tagless Go `case` listing the read beside other conditions (`case n == 3, <read>:`), or a Dart read with a null-aware default (`<read> ?? fallback`) | **the whole flag is refused**, and the run exits 1 naming the file. The fold would leave the switch testing a literal (`switch true {`, `match True:`, `case true`), an arm that always matches, or `true ?? fallback`, which `dart analyze` reports as dead code. The program would behave the same, but the dead arms would stay with the flag key gone from the file, so no later run could finish the job. TypeScript and JavaScript finish a switch on the read (see the multi-way guard row above) |
| A PHP **`elseif` that is not last in its chain**, where the flag serves **true** | **rewritten**: the clause becomes the chain's `else`, and every branch after it is deleted. That is not tidying — those branches sit behind a guard that now always passes, so they are unreachable, and leaving them would keep an `else` in front of them, which does not parse. Both spellings (`elseif` and `else if`) behave the same. Where the flag serves **false** the dead branch is removed, in any position |
| A **pattern guard** in Java or C# — `case String s when <read> ->`, `{ IsVip: true } when <read> => …` | Where the flag serves **false** the arm is removed when another arm follows it (a Java arrow rule or a C# switch-expression arm). Where it serves **true**, both compilers read `when true` as no guard at all, so the pattern now covers every value a later case with the same or a narrower pattern would match, and that case no longer compiles. A later case with the very same pattern, guarded or not, could never run, so it is deleted (a Java arrow rule or a C# switch-expression arm). The flag is **refused** when any other case besides the catch-all (`default`, `_`) still follows a guard served true, and in Java when a `case … when <read>:` label (colon form) is left with either literal, since `javac` rejects a constant guard there |
| A PHP read **inside string interpolation** (`"mode={$client->boolVariation('KEY', $ctx, false)}"`, or the same in a heredoc) | **no change** to that read: PHP's `{$...}` needs a variable after the brace, so a literal cannot take the call's place, and folding it would print the text `{true}`. Other reads in the file are still rewritten, the pull request reports the file as still mentioning the flag, and a key constant that read uses is kept |
| A **Python read bound to a name outside a function** (`use_legacy = <read>` at module or class level) | **the whole flag is refused**. Inside a function the binding is removed and its references replaced with the literal; at module or class level that name is public API, and the modules importing it do not contain the flag key, so they are never in the candidate set — this tool cannot see them, let alone update them. Leaving `use_legacy = True` standing would be permanent, since the flag key is gone from the file. Move the read inside the function that uses it, fix it by hand, or add the key to `ignore` |

### Known gaps

* **A file that still mentions the key after the rewrite is named in the PR
  body**, under "still reference this flag". That is the caveat to read before
  merging: it means the pull request does not remove every trace of the flag.
  Three different things put a file on that list, and only the first is a
  language-coverage problem:

  1. no configured language rewrites the file, so nothing looked at it. This
     fires whenever you NARROW `languages`: set `languages: ts` in a repository
     whose ERB templates also read the flag and the caveat names those `.erb`
     files. With every language enabled it fires for one family only: Ruby that
     no language rewrites, in a view (`.haml`, `.slim`, `.jbuilder`, `.jb`,
     `.builder`, `.rabl`, `.arb`, `.axlsx`, `.prawn`, `.rhtml`), a project file
     (every name and suffix listed for Ruby key constants above, such as
     `Rakefile`, `Gemfile` or `config.ru`) or a script with no suffix and a Ruby shebang
     (`bin/backfill`). A flag read there is not rewritten, so remove it by
     hand;
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
  prose to update rather than code that can break, with each comment line
  that names it. Both are positive verdicts reached by parsing the file;
  anything this cannot decide stays in the generic list. Such a file is not
  counted as still referencing the flag in the run's log either: it is
  printed as a note, line by line.

  **A comment that names only the flag is removed** rather than listed, in
  every language. "Only" is decided by words: once the flag's key (and, for a
  C# gate family, the name of a type the run deleted, with any `.Member` read
  off it) is taken out, what is left must be words like `the`, `by`, `gated`,
  `behind`, `flag`, `enabled`, `remove once rolled out`. One other word keeps
  the comment and lists it, so a licence header, a `TODO` naming other work
  (`TODO(PAY-12): …`) or a sentence that also explains something else is
  never touched. The unit is a comment, or one line of one when that line is
  a sentence of its own; a line in the middle of a sentence stays with it. A
  comment after code on the same line goes with the spaces before it; one
  with code after it on its line stays. A doc comment (`///`, `/**`) loses
  such lines but is never removed whole, since it documents something that
  survives. Text inside a string or a docstring is not a comment and is not
  touched. Nothing is removed while any file of that language and directory
  still names the key outside a comment, because then the flag is still in
  use there and the comment may still be true. Each removed comment is listed
  in the pull request, and every rewrite is checked by parsing the file again:
  its code must be exactly what it was, or the file is left alone.

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
  verbatim in the PR body, since it is still code you wrote. That includes the
  statement after a `try` whose body now always exits (unless a `catch` block
  can fall through) and, in Java, what follows a folded `yield`. A file that
  *already* contained unreachable code is left alone entirely — there the tool
  cannot tell its own strands from yours. In C#, Go, Kotlin, PHP and Swift
  the statements after a folded `return` are removed as part of the fold
  itself (C# and Kotlin would otherwise warn about them, which fails a build
  that treats warnings as errors). Those removals show in the diff like the
  rest of the fold, but are not listed separately in the PR body. A C# local
  function declared after the `return` stays, since it is in scope for its
  whole block and the code that runs may call it. Go goes further, so that
  `go vet` has nothing to report: what follows a folded `return`, `break`,
  `continue` or `goto` is removed in a block and in a `case`, `default` or
  `select` clause (stopping at a label), and so is what follows a `switch`
  whose clauses, a `default` among them, all return. Python and Ruby leave
  them in place.
* **A loop the fold makes dead is deleted in every language, and the PR says
  which.** A flag read as a loop condition, served false, leaves
  `while (false) { … }` (Go's `for false { … }`, Ruby's `until true`), a loop
  that can never run its body. It is removed and listed with the statements
  above. Left standing, because each runs or matters: a `do … while (false)`,
  `repeat … while false` or Ruby `begin … end while false` (the body runs
  once); a labelled loop (in C# and Go a `goto` can target the label); a loop
  that is not directly in a block, such as an unbraced `if` body or, in some
  languages, a `switch` arm; a Python `while False: … else: …` (the `else` runs); and a `for` loop
  with an initialiser, which still runs (Java refuses that flag instead; see
  its section). Java and Dart reject any dead loop, so there it never stands:
  a labelled one goes with its label, one that was the body of an unbraced
  `if`, `else` or loop becomes `{}`, and one that was a Dart `case`'s only
  statement becomes `break;`, since an empty case would run the next one.
  TypeScript and JavaScript go further, because their lint rejects each of
  these: a `do … while (false)` becomes its body, a `for` becomes its
  initialiser, and what cannot be finished is refused (see the TypeScript
  section). A Python loop that was its block's only statement becomes `pass`.
  A dead loop you wrote yourself in the same block as the one the flag made
  dead is removed with it.
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
  repository permanently unproposable. Quarantine covers only files the engine
  cannot *read*. Any other engine abort comes after it read the file — its
  syntax self-check or another check rejecting the rewrite it produced — so the
  source is fine and the rewrite is not, and the whole flag reports
  `[piranha-error]` and exits `1`, naming that file.
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
  refused outright for the same reason and no longer are: the engine folds
  them and re-indents what it moves. This inlining is what still runs after
  the engine, because it needs the whole file in hand.)
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
