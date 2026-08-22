# Usage

```
gardener align --repo <owner/repo> [--implement] [--file-issue]
gardener tend --repo <owner/repo> [--allow-merge]
gardener allowlist list | add --repo <owner/repo> | remove --repo <owner/repo>
gardener garden list | add --repo <owner/repo> | remove --repo <owner/repo>
gardener overnight [--hours N] [--concurrency N] [--strategy round-robin|issue-count|random] [--no-self-update]
gardener doctor [-v | --verbose] [--offline]
gardener status [--repo <owner/repo>] [--limit N]
gardener tail-transcript <path> [-f | --follow]
gardener dashboard [--port N]
gardener update [--check]
```

- **`gardener align --repo owner/repo`** (no flags) — **report-only,
  dry-run by default.** Dispatches Claude to read the target repo and your
  [conventions repo](../README.md#conventions-repo)'s
  `ALIGNMENT_CHECKLIST.md`, and produce a gap checklist.
  Claude has no write or shell tool available in this mode — see
  [Safety model](SAFETY.md) — so it cannot modify the target repo,
  open a PR, or open an issue no matter what the prompt says. The gap
  checklist prints to stdout and is logged to gardener's local run
  history. If a Discord webhook is configured (see
  [Alerting (optional)](../README.md#alerting-optional)), an informational
  notification fires too.
- **`--implement`** — additionally authorizes Claude to implement fixes in
  the target repo: branch, commit, and open a PR, following *the target
  repo's own* conventions (language, build tool, test framework) rather
  than copying the conventions repo's examples literally.
- **`--file-issue`** — instead of implementing, authorizes Claude to open
  one scoped GitHub issue in the target repo summarizing the gaps, for
  repos that already have their own `*-dev-loop` skill to pick the work up
  next cycle. Mutually exclusive with `--implement` (gardener errors if
  both are given — enforced twice: once by argparse's mutually-exclusive
  group, once explicitly in `cli.py`).
- **`gardener status [--repo owner/repo]`** — reads gardener's local
  SQLite run history (`~/.local/state/gardener/gardener.sqlite3` by
  default) and prints it: target repo, timestamp, mode, outcome summary.
- **`gardener dashboard [--port N]`** — serves a small web page (default
  `http://127.0.0.1:8765`, binds loopback-only — see `dashboard.py`'s
  module docstring, there is no authentication) with the same run history
  `gardener status` prints, plus the [garden view](DASHBOARD.md) (the
  garden list and the merge allow-list joined with each repo's stats, as a
  table or as a plot of plants) and
  a live-updating tail of the most recently written `tend`/`overnight` run
  log (see [Run logs](#run-logs) — a dispatching run writes one itself;
  auto-refreshes every 4s). "Currently tending" and the batch bar are
  built from *every* run log still being written to, not just the tailed
  one, so a manual `tend` started alongside the overnight run doesn't hide
  it — and the tail names the log it's showing plus how many others it
  isn't. All of that is a best-effort parse of those logs' own progress
  lines, not a second source of truth — `gardener status`'s sqlite db
  remains the one authoritative outcome record. If `--port` is already bound (e.g. a previous
  invocation still running), gardener picks a free one instead of failing
  and says so on stderr.
- **`gardener update [--check]`** — fast-forwards gardener's *own* checkout
  to `origin`; see [Self-update](#self-update) below. `--check` fetches and
  reports whether an update is available without applying it.

## `gardener tend` — dispatching a target repo's own dev-loop

Where `align` checks a repo against your conventions repo, **`gardener tend
--repo owner/repo`** makes real, broader progress on the repo itself by
dispatching *that repo's own* `<slug>-dev-loop` Claude Code skill (the kind
normally invoked interactively as `/example-repo-dev-loop`,
`/another-repo-dev-loop`, etc.) — headlessly, unattended, safety-gated the
same way every other mode
in this file is. This is for a "tend to my garden overnight" use case:
several repos, dispatched one after another, nobody watching.

1. Clones/refreshes `owner/repo` into gardener's cache
   (`~/.cache/gardener/repos/<owner>__<repo>`, relocated along with the
   rest of the cache root by `$GARDENER_CACHE_DIR` — see
   [Conventions repo](../README.md#conventions-repo)), exactly like
   `align` does.
2. Derives the skill slug from the repo's actual name (`owner/repo` ->
   `repo-dev-loop`, matching the naming every generated
   `<slug>-dev-loop` skill/repo already uses — see `dev_loop.py`),
   and checks whether `~/.claude/commands/<slug>-dev-loop.md` already
   resolves to a real file.
3. **If no skill exists yet**, dispatches `create-dev-loop` first (a
   distinct, more tightly scoped mode — see `dispatch.py`) to generate and
   register one, then confirms the file actually landed before proceeding.
   If that dispatch fails or the file still isn't there afterward, `tend`
   stops and reports an error rather than guessing. `create-dev-loop`'s own
   Step 6 ("create a private GitHub repo for the skill", meant to serve as
   that skill's own issue tracker) now runs for real: this mode's allowed
   tools grant exactly the three `gh` invocations Step 6 needs
   (`gh repo create`, `gh api user`, `gh label create` — never a broader
   `gh api *`/`gh repo *`/`gh label *`, and never `gh repo delete`), so the
   dispatched session creates the private `<slug>-dev-loop` repo, pushes
   the skill file to it, and pre-creates its gap-issue labels itself. This
   was withheld originally as "a different, higher-stakes risk class than
   editing an already-existing target repo" (see git history / issue #12)
   — granted 2026-07-19 on the reasoning that a repo created here is always
   a brand-new, empty, private tracker this dispatch itself just named, so
   the actual blast radius is "one new empty private repo," not write
   access to something pre-existing. `dev_loop.step6_unreachable()` still
   checks this live against `MODE_SPECS` rather than assuming — if Step 6
   is ever withdrawn again, `cli.py`'s `cmd_tend` automatically goes back
   to treating the bootstrap as incomplete (a distinct WARNING plus its own
   notification, rather than a plain success) instead of silently
   over-reporting.
4. Dispatches the `<slug>-dev-loop` skill itself via `claude -p
   "/<slug>-dev-loop ..."`, with `cwd` set to gardener's own controlled
   clone (not wherever the skill's own hardcoded "Working directory" line
   says — the prompt explicitly overrides that; see `dev_loop.py`).

**`--allow-merge`** permits `gh pr merge` during this dispatch — but ONLY
when the target repo is also present in gardener's local merge allow-list
(`gardener allowlist add --repo owner/repo`). Either one alone is not
enough; see "Merge allow-list" below.

Every `*-dev-loop` skill is written to stop and ask a human (via
`AskUserQuestion`) before merging or taking a destructive action. Dispatched
headlessly there's nobody to answer, so this had to be verified for real,
not assumed — see [Safety model](SAFETY.md)'s "Headless dispatch and
the `AskUserQuestion` problem" section for exactly what was tested and
observed.

## Orphaned work recovery

If this device kills the whole `gardener overnight` process mid-dispatch
(see ["Wiring it to 'tend to my garden while I sleep'"](OVERNIGHT.md) in
the overnight docs), the repo it
was actively tending at that moment gets no chance to finish — but the
dispatched dev-loop session may already have pushed a branch and opened a
PR before the kill. Without anything accounting for this, the *next* run
would dispatch a brand-new `tend` cycle against the same repo from
scratch, unaware that PR already exists — at best duplicate work, at worst
a second, competing PR for the same fix.

`gardener tend` now checks for this before dispatching: every PR a `tend`
dispatch opens carries a fixed marker
(`<!-- gardener-tend-dispatch -->`) in its body (see `dev_loop.py`'s
`ORPHAN_MARKER`). Before building this run's prompt, `cli.py`'s
`find_orphaned_pr` looks for any still-open PR on the target repo whose
body carries that marker (`gh pr list --json body`, filtered client-side —
best-effort: any `gh` failure here is treated as "no orphan found," never
as a reason to fail an otherwise-normal dispatch). If one is found, the
dispatched session is explicitly instructed to `git fetch`/`git checkout`
that PR's branch and finish or assess that work instead of starting a new
branch from the default branch — see `dev_loop.py`'s `build_tend_prompt`
for the exact instructions. Once a human merges or closes that PR, it
naturally stops matching on the next run — no separate cleanup needed.

## Concurrent dispatch safety

Nothing stops two independent `gardener` invocations from targeting the
same repo at the same time — a manual `gardener tend --repo X` run by hand
while `gardener overnight` is already dispatching `X`, or two overlapping
`overnight` runs (e.g. one started by hand while a supervised one — `devsrv`,
a scheduled task, cron — is also up). Without anything guarding against
this, both processes would
clone/checkout/dispatch against the *same* shared working tree in
`~/.cache/gardener/repos/<owner>__<repo>` concurrently — the same class of
failure that has caused real `.git/objects` corruption before via
concurrent `git worktree add`/`remove` races, a different mechanism than
the concurrent-clone case here but the same underlying lesson: concurrent
git operations against one shared directory are not safe to assume away.

`gardener align` and `gardener tend` (and therefore `gardener overnight`,
which dispatches `tend` in-process) now take an exclusive, non-blocking,
cross-process lock on the target repo (`gardener/repo_lock.py`, keyed by
`owner/repo`, held for the full clone-through-dispatch duration) before
touching its clone directory. If another gardener process already holds
it, the dispatch is skipped rather than queued — you'll see an ERROR-level
outcome/notification along the lines of "owner/repo is already being
worked on by another gardener process" instead of a hang or a corrupted
clone. Deliberately non-blocking: a stuck lock must never turn into
`overnight` silently waiting out its own per-repo timeout budget. Distinct
repos never contend with each other, so this doesn't limit
`--concurrency`'s within-process parallelism — only two processes racing
on the *same* repo ever hit this path.

## Merge allow-list

`gardener allowlist add --repo owner/repo` / `remove --repo owner/repo` /
`list` manage a small local JSON file (`~/.local/state/gardener/
merge_allowlist.json` by default — same directory `status`'s run history
lives in, overridable via `GARDENER_STATE_DIR`) of repos `gardener tend
--allow-merge` is permitted to actually merge PRs in. A repo not listed
here can never be merged into by a `tend` dispatch, no matter what flags
are passed — see `merge_allowlist.py` and `dispatch.py`'s `tend_mode_spec()`.
See also [Merge allow-list mechanics](SAFETY.md#merge-allow-list-mechanics)
in the safety model for exactly how this is enforced at the tool-scoping
level.

## `gardener doctor` — pre-flight check on gardener's own state

`gardener doctor` is a read-only health check over everything an
`overnight` run depends on locally: the external CLIs it shells out to
(`git`, `gh`, `claude`, plus `gh`'s auth status), the state directory and
whether its JSON lists still parse, every cache clone under
`~/.cache/gardener/repos`, and whether each garden/allow-list entry still
resolves to itself on GitHub.

Every check exists because of a failure that really did consume a garden
slot on a real overnight run — see `doctor.py`'s module docstring for the
specific incident behind each. The two that matter most:

- **A cache clone with modified tracked files fails `tend` on every
  subsequent run, silently.** The refresh in
  `clone_or_refresh_target_repo` is `git checkout -B <default>
  origin/<default>`, which git refuses rather than overwrite local
  changes, and nothing ever cleans that up — so a clone left dirty by a
  killed dev-loop run burns that repo's slot every night until someone
  looks.
- **A renamed or transferred target repo fails in a way that looks fixed
  after you clear the cache.** `gh repo clone <old-name>` follows GitHub's
  rename redirect and writes the *canonical* URL as `origin`, so the fresh
  clone works — and then the next refresh trips the origin check and fails
  again. Only asking GitHub for each configured entry's current
  `nameWithOwner` surfaces this before it wastes another night.

**`doctor` never repairs anything.** Each finding carries the exact
command to run instead. That's deliberate: the remediations these findings
call for are `git stash`-versus-discard on a clone that may hold unpushed
work, and rewriting an opt-in list — the merge allow-list included. Neither
belongs to a health check to decide on the operator's behalf.

```bash
gardener doctor              # problems only
gardener doctor --verbose    # every check, including the passing ones
gardener doctor --offline    # skip the gh/network checks
```

Exit status is `1` if any check reported an ERROR (something will fail on
the next run) and `0` otherwise — a WARN or a skipped check is not a
failure, so `doctor --offline` on a phone with no signal still exits `0`.
That makes it usable as a gate in whatever wrapper starts the nightly run:

```bash
gardener doctor || echo "fix the above before tonight's run"
```

A repo another gardener process currently holds the [per-repo
lock](#concurrent-dispatch-safety) on is reported as skipped rather than
checked — a `tend` in flight has legitimately dirtied its own clone and
will clean up after itself, so `doctor` stays usable *during* the
unattended run it exists to protect.

## Self-update

`gardener overnight` runs unattended (Task Scheduler, cron, `devsrv`) with
nobody watching to notice new commits landed on gardener's own `main` and
run `git pull` by hand — so, by default, it fast-forwards gardener's own
checkout to `origin/<current-branch>` before reading the garden, logging
one line either way (`gardener: self-update: ...`) to the same stderr/run
log every other setup step writes to. Pass `--no-self-update` to skip it.

This only ever touches gardener's own repo checkout — never a target repo
or the conventions repo (those have their own separate refresh mechanics,
`clone_or_refresh_target_repo`/`conventions.ensure_conventions`) — and it
is conservative about when it's safe to: a dirty tree (tracked changes;
untracked files don't count), a detached `HEAD`, or a local branch that
has diverged from `origin` (not a fast-forward) all skip rather than force
anything, and none of those is a failure — but on the unattended
`overnight` path (not on `gardener update` run by hand), a skip or an
actual error also alerts through the configured notifier rather than only
logging, since an unattended box tending the garden with stale code is
exactly the case nobody is reading stderr for (see
[Alerting](ALERTING.md#self-update-alerts)). It's also a no-op if gardener isn't running from a git checkout
at all (e.g. installed from a built wheel rather than `pip install -e .`)
— see `selfupdate.py`'s module docstring for exactly why an editable
install is what makes this possible in the first place.

Because this checkout's already-imported Python modules don't retroactively
change mid-process, a self-update at the top of tonight's `overnight` run
benefits *tomorrow* night's run (and any other `gardener` invocation
after it), not the one currently executing.

**`gardener update [--check]`** runs the exact same fast-forward on demand,
for anyone who'd rather trigger it by hand than wait for the next
`overnight` run. `--check` fetches and reports whether an update is
available (and, if so, the old/new commit) without applying it.

## Other flags

- `--model <name>` — override the model `claude` uses (`align`, `tend`, and
  `overnight`, which threads it through to every `tend` dispatch in the
  batch).
- `--timeout <seconds>` — how long to wait for the dispatched run.
  `align`'s default is 1800s / 30 min (reading and analyzing a real repo
  against ~10 convention docs is not fast). `tend`'s default is 2700s / 45
  min — it runs a full triage/implement/test/PR/self-audit cycle in one
  session with no subagent fan-out or wait-for-review loop (see
  [Safety model](SAFETY.md)), so it needs more room than `align`, but
  must still fail fast enough that one stuck repo can't consume an entire
  overnight multi-repo batch; see `dispatch.py`'s
  `TEND_DEFAULT_TIMEOUT_SECONDS` for the full reasoning. The internal
  `create-dev-loop` dispatch `tend` runs when a repo has no skill yet has
  its own fixed 900s/15min ceiling, not exposed as a flag.
- `--limit <n>` (`status` only) — how many most-recent runs to show,
  newest first. Defaults to `20`; pass a larger number to page further
  back through the run history, or combine with `--repo` to scope the
  window to one repo.
- `-v` / `--verbose` (`doctor` only) — print every check, including the
  passing ones, instead of only the problems. Useful the first time, or
  to confirm a check actually ran rather than being skipped.
- `--offline` (`doctor` only) — skip every check that needs the network
  (the `gh auth status` probe and the per-repo rename lookups). Those
  checks already degrade to "skipped" on their own when `gh` can't answer;
  this avoids spending one `gh` call per configured repo when you already
  know there's no signal.
- `-f` / `--follow` (`tail-transcript` only) — keep reading as the file
  grows, like `tail -f`, instead of exiting at EOF. Useful against the
  transcript path a dispatch logs to stderr while that run is still going.
- `--conventions-repo <git-url>` (`align` only) — the conventions repo to
  audit against, overriding `$GARDENER_CONVENTIONS_URL`. There is no
  built-in default; with neither set, `align` exits 2 with setup
  instructions rather than dispatching. See
  [Conventions repo](../README.md#conventions-repo) for the required layout.
- `--no-refresh-conventions` (`align` only) / `--no-refresh-target`
  (`align` and `tend`) — reuse whatever is already cached instead of
  fetching latest first. One exception: if the cached conventions checkout
  belongs to a *different* conventions repo than the one now configured,
  `align` re-points and refreshes it anyway — reusing it would audit
  against the previously-configured repo, a wrong answer rather than a
  stale one.

## Live session visibility

`align`, `tend`, and `overnight` still dispatch synchronously (see [Why
synchronous dispatch, not `--bg`](SAFETY.md#why-synchronous-dispatch-not---bg)
in the safety model) and only capture output when the whole run finishes — for `tend`,
that's up to `TEND_DEFAULT_TIMEOUT_SECONDS` (45 min) per repo with
otherwise zero visibility into what's happening while it runs, a real gap
for anyone watching an unattended run's logs (via whichever process
supervisor manages it — `devsrv logs`, `journalctl`, a plain log file, or
the dashboard's own log viewer) overnight.

Claude Code already solves the actual data half of this on its own, for
every `-p` session, no special flag required: it writes a JSONL transcript
— growing in real time — to `~/.claude/projects/<encoded-cwd>/<session-
id>.jsonl`. Every dispatch now surfaces that file's path within seconds of
starting, printed to stderr the same way every other `gardener:` progress
line is:

```
gardener: session transcript: ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl (tail -f it for live detail, or `gardener tail-transcript -f <path>`)
```

This is a background daemon thread started right before the existing,
unmodified blocking dispatch call — it briefly polls for the new transcript
file to show up and prints its path once found, or gives up silently within
a few seconds if nothing does; either way it can never delay or fail the
real dispatch. See `dispatch.py`'s "Live transcript visibility" docstring
section and `transcript.py`'s own module docstring (which documents,
against two real confirmed examples, exactly how a working directory gets
encoded into that directory name) for the full design.

**`gardener tail-transcript <path> [-f]`** pretty-prints that file — one
line per meaningful event (a tool call and its truncated input, an
assistant text block, or a tool result flagged `ok`/`ERROR`) instead of raw
JSONL. Same shape as the ad hoc Python snippet originally hand-typed to
tail a live `tend` dispatch's transcript, now a permanent, tested part of
gardener. Without `-f` it dumps whatever's in the file right now and exits
(safe to run against a still-in-progress dispatch); with `-f` it keeps
reading as the file grows, like `tail -f`, until interrupted.

### Listing and stopping sessions

A *session* is one running dispatching process — an `align`, a `tend`, or
an `overnight`. Every one of them registers itself under
`~/.local/state/gardener/sessions/<id>.json` for the duration of its run,
which is what makes the three commands below possible. The names and flags
deliberately mirror the docker CLI, since that vocabulary is already in
most operators' fingers — but the resemblance is skin deep: a gardener
session is a plain OS process, and nothing here isolates, namespaces, or
restarts anything.

```
gardener ps [-a|--all] [-q|--quiet]
gardener stop <session>... | --all [-t|--time SECONDS]
gardener kill <session>... | --all [-s|--signal NAME]
```

`gardener ps` lists what is running right now:

```
SESSION    COMMAND    TARGET                             PID      STARTED  STATUS
--------------------------------------------------------------------------------
82cf55cd   overnight  garden                             9787     00:57    running (tending Preponderous-Software/viron)
```

`TARGET` is the `--repo` an `align`/`tend` was given, or `garden` for an
`overnight` run (whose target is the whole opt-in list). `STARTED` is age
since the session started, not runtime — no end time is recorded, since a
session killed outright never gets to write one, and a column that is only
sometimes a duration is worse than one that is always an age. The
parenthesised detail in `STATUS` is read back out of that session's own run
log with the same parser the dashboard's "Currently tending" panel uses, so
the two can't drift apart.

- `-a`/`--all` also shows recently finished sessions, like `docker ps -a`.
  These are not run history — `gardener status` and the dashboard read the
  SQLite db for that; the newest `sessions.DEFAULT_KEEP_EXITED` (50) exited
  session files are kept and the rest pruned once per run.
- `-q`/`--quiet` prints bare ids, so `gardener stop $(gardener ps -q)`
  works the way its docker equivalent does.

`gardener stop <session>` stops a run gracefully. A session is named by its
id, by any unique prefix of that id, or by its target repo — `gardener stop
owner/name` is valid whenever exactly one running session is pointed at it.
`--all` stops every running session.

**`stop` signals the session's whole process tree, not just gardener.** The
real work of a dispatch happens in a `claude` child process, which spawns
builds (`mvn`, `npm`, `pytest`) of its own; signalling gardener alone
leaves those running as orphans still holding the repo's clone directory —
the exact failure that motivated these commands. `stop` collects the
session pid's descendants from `/proc`, SIGTERMs the whole set deepest
first, waits `--time` seconds (default 10, matching `docker stop`), and
SIGKILLs whatever is still alive. Stopping is worth preferring over
killing: an `overnight` given the chance to exit cleanly still writes the
resume cursor that lets the next run pick up where it left off.

`gardener kill <session>` sends one signal — `--signal` (default `KILL`,
accepting `KILL`, `SIGKILL`, or `9`) — to the same process tree, with no
grace period and no escalation. Prefer it only for a session that is
already wedged.

Both commands report what actually happened rather than assuming the signal
worked; a session still alive afterwards is an error exit, not a silent
success.

**Liveness comes from a lock, not a pid check.** Each session holds an
exclusive `fcntl.flock` on its own registry file (the same stdlib mechanism
`repo_lock.py` uses), and `ps` decides "is this alive?" by trying to take a
shared lock. The kernel releases that lock however the process dies —
clean exit, SIGKILL, or Android killing every background process on a
task-swipe-away — so the registry self-heals with no reaper and no cleanup
on the crash path. `os.kill(pid, 0)` was deliberately not used: an
overnight run can outlive its own logs by hours on a phone that recycles
pids freely, and a stale record whose pid had been reused would otherwise
read as running and, far worse, become a target `gardener stop` would
signal. See `sessions.py`'s module docstring for the rest of the design.

### Run logs

`align`, `tend`, and `overnight` also mirror their own stderr narration to
a run log at `~/.local/state/gardener/logs/<command>-<YYYYmmdd-HHMMSS>.log`
(`run_log.py`; the path is printed as the run's first line). This is what
makes the dashboard's live panels work at all: those panels are built by
re-reading gardener's `gardener: tending <repo> ...` / `gardener: finished
tending <repo>` / `overnight dispatching ... (N-M/T candidates this run
...)` progress lines back out of the active logs, and before this existed
no such file was ever written — so for any run not launched in a terminal
someone was already watching (i.e. every unattended run, the entire case
the dashboard exists for) "Latest session", "Currently tending", and "Live log"
were permanently empty while "Recent runs" kept working off the SQLite
history.

It is a tee, not a redirect: stderr still goes to wherever it already went,
so running a dispatch in a terminal looks exactly as it did before.
Gardener writes this file itself rather than the dashboard learning where
some external process supervisor happens to capture stderr — that would
have been a smaller change, but it would make gardener's own live
visibility depend on being launched by one specific outside tool. Nothing
in gardener knows how its process was started.

The newest `run_log.DEFAULT_KEEP` (30) logs are retained, pruned once per
run; the run's own log is always the newest and so is never the file
deleted. A log that can't be opened (read-only state dir, out of disk)
prints one warning and the run continues without one — logging must never
be able to fail a dispatch.

Two dispatching runs at once each get their own log — a manual `gardener
tend` alongside the devsrv-managed `overnight` run is a supported
configuration, which is why `repo_lock.py` exists — so the live panels
read *every* log written to within `dashboard.ACTIVE_LOG_WINDOW_SECONDS`
(the real `tend` dispatch timeout plus a margin, since a single dispatch
can sit inside one `claude` subprocess that long without printing a line),
not just the newest one. "Currently tending" is the union across them and
the batch bar comes from the freshest log that actually has one, so
starting a one-repo tend no longer makes the overnight run beside it
disappear. The "Live log" tail still shows a single file — interleaving
two raw narrations would be unreadable — but it names that file and says
how many other live logs it isn't tailing.

A repo clears from "Currently tending" when its `gardener: finished
tending <repo>` line appears, which `_dispatch_tend` prints from a
`finally` on every one of its return paths. This used to be inferred from
the `notify: sent to Discord: ...` line instead, which meant an operator
with no webhook configured (a supported setup — notifications are then a
no-op) never cleared a single repo: the panel accumulated every repo the
run had ever touched instead of showing what was actually running.

### Blocked out-of-scope attempts

When a dispatched run tries something outside its mode's pre-approved
scope, `claude` blocks it and reports it back in the JSON result's
`permission_denials` (see [SAFETY.md](SAFETY.md)). `align` and `tend` both
print those to stderr, one per line, immediately before the NOTE that
refers to them:

```
gardener:   denied: Bash(gh pr review 12 --comment --body-file -)
gardener:   denied: Read(/root/.m2/repository)
gardener: NOTE — the dispatched run attempted action(s) outside this mode's pre-approved scope and they were blocked (see denials above)
```

Two limits keep this from flooding the log. The list is deduplicated first,
which is what absorbs a run retrying one blocked call fifty times, and then
capped at `cli.DENIAL_PRINT_LIMIT` (10) *distinct* entries with any
remainder collapsed to a count, which is what absorbs a run blocked on
fifty different things. Each entry renders as `ToolName(the argument the allow-list scopes on)` — the command
for `Bash`, the path for a file tool — truncated to
`cli.DENIAL_MAX_CHARS` and with newlines collapsed, so one denial is
always exactly one line. The `denials=N` count on the summary line above is
unchanged; these supplement it.

The point is telling a benign denial from a disabling one at a glance.
Several denials per repo per night are normal and expected — a dev-loop
skill instructed to post a real Review object hits the deliberate `gh pr
review`/`gh api` exclusion every time — but the same count can also mean a
run was structurally unable to do its job, and before these lines existed
distinguishing the two meant opening the session transcript by hand.
Because `run_log.py` tees stderr, they land in the dashboard's log view
too.

The entries come from `claude`'s output, whose structure gardener doesn't
control, so rendering degrades to `str()` rather than assuming keys — an
unrecognised shape prints as itself instead of breaking the run.
