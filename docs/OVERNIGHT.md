# Overnight / unattended operation

**`gardener garden`** manages a second, independent opt-in list — separate
from the merge allow-list (see [Merge allow-list](USAGE.md#merge-allow-list))
above — of repos `gardener overnight` is allowed to tend while nobody is
watching. Same file-location convention and same
safe default as the merge allow-list: `gardener garden add --repo
owner/repo` / `remove --repo owner/repo` / `list` manage a small local JSON
file (`~/.local/state/gardener/garden.json` by default, overridable via
`GARDENER_STATE_DIR`), and a missing file means an empty garden — a repo is
never touched overnight just because it exists on this machine, only
because it was explicitly added. See `gardener/garden.py`.

**`gardener overnight [--hours N] [--concurrency N] [--strategy round-robin|issue-count|random] [--no-self-update]`** is the actual "tend
to my garden while I sleep" entry point:

0. Fast-forwards gardener's own checkout to `origin` first (on by default;
   `--no-self-update` skips it) — see [Self-update](USAGE.md#self-update)
   for the full design and exactly what makes this safe to do
   unattended. Logs one line either way and never aborts the run, even if
   the self-update step itself hits something unexpected. A skip or an
   error additionally alerts through the configured notifier (see
   [Alerting](ALERTING.md#self-update-alerts)) — the run proceeds either
   way, but tending the garden with code that may be behind `origin` is no
   longer evidence only a stderr line nobody reads carries.
1. Reads the garden. An empty garden prints a clear message and exits `0`
   — nothing to do is not an error.
2. Dispatches `gardener tend --repo <repo> --allow-merge` **in-process**
   (calls `_dispatch_tend` directly, no `gardener` subprocess-of-itself) for
   each garden repo, in batches of `--concurrency` repos at a time (default
   `2`; pass `--concurrency 1` for strictly one after another), starting
   from wherever the *previous* `overnight` run left off
   (see "Resuming across nights" below), until either the garden is
   exhausted for this run or the time budget runs out. Repos within a batch
   run concurrently on a `ThreadPoolExecutor` (stdlib-only) when
   `--concurrency` > 1 — each is still just one independent, blocking
   `claude -p` subprocess (see [Why synchronous
   dispatch](SAFETY.md#why-synchronous-dispatch-not---bg)), now several running in parallel
   OS processes rather than one at a time. Whatever machine this actually
   runs on shares real CPU/RAM across every process on it, with no
   guaranteed isolation between them (see the "no true always-on daemon
   guarantee" caveat below, itself a property of the host device, not of
   gardener) — `2` is a deliberately modest default, and raising it further
   is a decision to make with that tradeoff in mind, not a free speedup.
3. `--allow-merge` is passed unconditionally to every dispatch. This is
   safe *without* `overnight` needing any merge-decision logic of its own:
   `tend`'s own `merge_eligible()` check still requires the target repo to
   *also* be present on the separate merge allow-list before `gh pr merge`
   is ever reachable in the dispatched session (see
   [Merge allow-list](USAGE.md#merge-allow-list) above) — being in the
   garden alone never authorizes a merge. The garden
   and the merge allow-list are two independent, both-opt-in gates.
4. **Time budget (`--hours`, default `8.0` — a full night's sleep).** The
   very first *batch* of a run is always attempted (as long as `--hours` is
   positive) so a run never silently dispatches nothing; every batch after
   the first requires enough headroom left in the budget for one more
   worst-case `tend` call (`TEND_DEFAULT_TIMEOUT_SECONDS`, 45 min) before
   it's started — checked once per batch rather than once per repo when
   `--concurrency` > 1, since a batch's own wall-clock time is bounded by
   one repo's worst-case timeout (everything inside a batch runs in
   parallel, not stacked). Computed from real elapsed time so far, not a
   precomputed worst-case-per-repo plan, so a night of faster-than-worst-case
   dispatches (79-250s observed in practice — see [Project
   Status](PROJECT_STATUS.md)) can fit
   more repos than the naive arithmetic would suggest. A repo already in
   progress is never hard-killed mid-run to respect the budget; the budget
   only gates whether a *new* repo is started.
5. **Repo-selection strategy (`--strategy`, default `random`).** Picks
   which order this run attempts the garden in — see `gardener/overnight.py`'s
   `Strategy` enum for the pluggable `garden -> ordered list[str]`-shaped
   implementations:
   - **`round-robin`** (byte-for-byte the original and only behavior before
     this flag existed, and the default until the garden grew past a size
     where a fixed rotation kept pairing the same neighbours in a batch):
     the alphabetically-sorted garden, rotated to start wherever the
     *previous* run's resume cursor left off.
   - **`issue-count`**: sorts the garden descending by each repo's live
     open-GitHub-issue count (`gh issue list --state open`, one call per
     garden repo — `cli.py`'s `fetch_issue_counts`), so repos with more
     waiting work get attempted first. A repo whose count fetch fails is
     treated as count 0 (lowest priority), not a crash.
   - **`random`**: reshuffles the garden fresh every run, so which repos
     get skipped when the time budget runs out varies night to night
     instead of consistently penalizing whichever repos happen to sort (or
     count) last.
6. **Resuming across nights, and the cursor design per strategy.** If the
   garden is longer than one night's budget can cover, a resume cursor
   (`~/.local/state/gardener/overnight_cursor.json`) tracks where the next
   `overnight` run should pick up, so a garden of 20 repos and a budget
   that only fits 6 per night eventually reaches every repo across several
   nights instead of only ever tending the first 6 (or the first 6 by
   issue count, or the first 6 of a reshuffle). **The cursor works
   differently depending on the active strategy, a deliberate design
   decision** (see `overnight.py`'s module docstring for the full
   reasoning): `round-robin`'s ordering is stable across runs (the same
   alphabetically-sorted list every time), so a bare list index
   (`next_index`) genuinely means "the Nth repo in that stable order" from
   one run to the next — unchanged from before this flag existed.
   `issue-count` and `random` do **not** have a stable ordering across runs
   (a live issue count can change; a shuffle is fresh every time), so a
   bare index would silently resume at the *wrong* repo — worse than no
   cursor at all. Both instead resume by **repo name**: the cursor file
   gains a second field, `attempted` (a list of repo full names already
   attempted since the current pass through the garden began), read/written
   by `overnight.py`'s `read_attempted`/`write_attempted` and applied by
   `resume_order`. Each run computes a fresh strategy-ordered list, then
   filters out whichever names are already in `attempted` — once every
   repo has been attempted at least once, the next run detects the cycle
   is complete and starts a fresh one. `next_index` and `attempted` live in
   the *same* cursor file under different keys, so switching `--strategy`
   between runs never clobbers the other strategy's own progress — round-
   robin's index and issue-count/random's attempted-name list simply sit
   side by side, each only ever read/written by its own strategy.
   **The cursor is written after every batch, not once when the run
   finishes** (`cmd_overnight`'s `persist_cursor`). On this device that's
   the difference between a working resume and a decorative one: a long
   run is more likely to be killed mid-garden than to reach the end of its
   loop, and a cursor written only at the end is lost in exactly that case
   — a run that tended six repos on 2026-07-25 was killed, and the next
   run restarted the cycle from zero because none of the six had been
   persisted ([issue #42](https://github.com/dmccoystephenson/gardener/issues/42)).
   Persistence is per *batch*, so a batch interrupted partway is
   re-attempted whole rather than having its finished repos recorded
   individually — re-tending is idempotent enough, and this keeps the
   round-robin index's "advance by N repos" meaning intact.
7. **Notifications.** Each repo's own outcome is logged and alerted via the
   *existing* `state.record_run`/`_notify_run` machinery `_dispatch_tend`
   already uses (unchanged, and safe to call from more than one thread at
   once — see [Alerting design](ALERTING.md)) — you get one
   Discord message per repo for free. `overnight` additionally fires **one
   summary notification at the end of the whole batch** — total repos
   attempted, how many opened a PR, how many merged, how many hit a
   `DECISION NEEDED:` line, how many errored, and elapsed time — so you wake
   up to one clear digest instead of piecing together N separate messages.
   With no Discord webhook configured, the summary is still printed to
   stderr; the notification call itself is a clean no-op (`NullNotifier`,
   see [Alerting (optional)](../README.md#alerting-optional)), not a failure.
8. One repo failing or timing out does not abort the batch — it's logged,
   notified, and `overnight` moves on to the next repo.

## Wiring it to "tend to my garden while I sleep"

`gardener overnight --hours N` is a single long-running foreground
command — it doesn't daemonize, background itself, install anything, or
know or care what invoked it. Making it actually run unattended, on a
schedule, and survive an interruption is entirely a property of *how the
host device invokes and supervises it* — gardener has no opinion on that
beyond the resume cursor described above, which is what makes *any* of
the recipes below safe to interrupt and restart rather than something
each one has to solve itself.

**No device this has actually been run on gives an uninterrupted
"runs the whole night no matter what" guarantee.** The honest baseline,
true regardless of device, is: something restarts or reschedules
`gardener overnight`, and the resume cursor means that restart picks up
roughly where the last one left off instead of re-tending the garden from
scratch. What differs per device is *what* does the restarting/scheduling
and *why* an interruption happens at all. Below are the recipes this has
actually been run under — pick the one matching your device, or adapt the
pattern to whatever scheduler yours has; don't assume any one of these is
"the" way gardener expects to be run.

### Android / UserLand sandbox — `devsrv`, no cron or systemd

This device class has no systemd, no cron, and no true always-on daemon
guarantee — Android can and will kill background processes on a task
swipe-away, aggressive battery/OOM management, or extended idle, the same
way it kills every other background process on the device. There is no
way around that here. What a small local process-supervisor script gives
you instead (`devsrv` below is a stand-in name — substitute whatever
registers/restarts long-running commands on your own setup): the command
is *registered* so it's visible, restartable without
retyping it, and comes back on its own the next time an interactive shell
starts (wired into `.bashrc`) if it gets killed — **not** an uninterrupted
guarantee that it runs the whole night no matter what.

The actual invocation:

```bash
devsrv start gardener-overnight --autostart -- gardener overnight --hours 6
devsrv status gardener-overnight   # confirm it's running
devsrv logs gardener-overnight -f  # watch progress live
```

Stopping a night early is `gardener stop`, not the supervisor's own stop:
a supervisor kills the process it started, which is not necessarily the
one dispatching, and never the `claude` process underneath it or the
builds that `claude` spawned — those survive as orphans still holding the
repo's clone directory. `gardener ps` shows the run and what it is
currently tending, and `gardener stop <id>` signals its whole process tree
(see [Listing and stopping sessions](USAGE.md#listing-and-stopping-sessions)).
Prefer `stop` over `kill` here: an overnight run allowed to exit on
SIGTERM still leaves the resume cursor pointing at the next untended repo.
Note that a supervisor configured to restart the command (`--autostart`
above) will start a *fresh* run the next time it fires — stopping the
session is not the same as cancelling the schedule.

`--hours 6` here is a deliberate local choice on this device, not
gardener's default (`8.0`, `overnight.DEFAULT_OVERNIGHT_HOURS` — see the
time budget section above). The registered service is the source of truth
for what a given device actually runs — check it with `devsrv status
gardener-overnight` rather than trusting this snippet if the two ever
disagree.

`--autostart` means: if Android kills every background process (confirmed
directly to happen on a task swipe-away), this specific run will NOT
silently resume mid-budget on its own — the `claude` subprocess it was
waiting on is gone too. What `--autostart` actually buys you is that the
*next* interactive shell you open on this device re-runs `devsrv
autostart-all`, which restarts the registered command fresh — a new full
budget from that point, whatever `--hours` it was registered with, not a
continuation of the interrupted run's remaining time — if it isn't already
running. Combined with the resume cursor above, a run that
gets interrupted partway through the garden doesn't lose progress on the
repos it already finished — the next invocation (autostart-triggered or
manually re-run) picks up from the next untended repo, not the top of the
list again. The repo that was *actively* being tended at the moment of the
kill is still re-dispatched fresh on the next run (the resume cursor only
advances past *completed* repos) — but that re-dispatch now recognizes and
continues any PR the interrupted session already opened rather than
starting a duplicate; see [Orphaned work recovery](USAGE.md#orphaned-work-recovery) above.

### WSL2 + Windows Task Scheduler

A different device this has actually been run on: a WSL2 Ubuntu instance
with no true `cron` daemon of its own (WSL2 doesn't run background
services unless something starts them, and nothing keeps a `cron` job
alive across a WSL shutdown) but a real, reliable host-side scheduler —
Windows Task Scheduler — one directory up. The registered task runs

```
wsl.exe -d <distro> -u <user> -- bash -lc "/path/to/gardener/bin/run-overnight.sh"
```

on a daily trigger, and `bin/run-overnight.sh` in this repo is that
script — tracked in git specifically so its fixes (below) aren't
device-local knowledge that quietly disappears:

```bash
#!/usr/bin/env bash
set -uo pipefail
export PATH="/root/.local/bin:$PATH"
GARDENER_BIN="/root/.venvs/gardener/bin/gardener"
HOURS="${GARDENER_OVERNIGHT_HOURS:-8}"
CONCURRENCY="${GARDENER_OVERNIGHT_CONCURRENCY:-4}"
"$GARDENER_BIN" overnight --hours "$HOURS" --concurrency "$CONCURRENCY"
```

Both knobs are env-overridable so a single night can be re-tuned from the
Task Scheduler action without editing tracked code. The concurrency default
here is deliberately `4` rather than `cmd_overnight`'s own `2`: the flag's
default stays conservative for anyone invoking `gardener overnight` by hand
on an unknown machine, whereas this script is device-specific — it only
ever runs on this one WSL2 box, at an operator's explicit request for a
wider nightly run, and four-wide has been exercised for real on that box
before being made the default here — see [Manual/end-to-end
verification](TESTING.md#manualend-to-end-verification) for the run and what
it did and did not establish. What it establishes is a floor, not a
ceiling: the upper bound at which CPU/RAM contention starts genuinely
degrading dispatches is still unmeasured on every device this has run on,
so raising this further remains the tradeoff described in step 2 of the
`gardener overnight` walkthrough at the top of this document, not a free
speedup.

Two gotchas confirmed the hard way against a real scheduled failure, both
now baked into the script above rather than left as tribal knowledge:

1. **`bash -lc "…"` is never interactive, even with `-l`.** Stock Ubuntu's
   `~/.bashrc` starts with `[ -z "$PS1" ] && return`, which bails out
   before ever reaching whatever line further down that file puts `claude`
   on `PATH` (e.g. `export PATH="$HOME/.local/bin:$PATH"`, if that's where
   `claude` lives on your setup) — a genuinely fresh Task Scheduler launch
   never sees that PATH entry, so every dispatch fails instantly with
   `claude not found on PATH`, and it's easy to miss in testing because an
   interactive shell you test the script from *already* has the enriched
   PATH inherited, masking the bug. Confirmed directly:
   `env -i HOME="$HOME" USER=root bash -lc 'which claude'` fails against
   an unpatched script's environment. The fix is exporting `PATH`
   explicitly in the script itself, as above, rather than depending on
   `.bashrc` sourcing at all.
2. **Don't also redirect the script's own output to a log file.**
   `gardener overnight` already writes and prunes its own run log
   internally (`run_log.py` — see [Run logs](USAGE.md#run-logs)); a script-level
   `>>"$LOG_FILE" 2>&1` wrapper around the invocation is redundant, and
   when it happens to compute the same filename gardener's own logger
   does in the same second, the result is every line duplicated in that
   file. Let gardener own its own logging entirely.

Real-verified on 2026-07-26: the unpatched script failed 28/28 repos in
about a minute, confirmed as the PATH issue above via the same `env -i`
reproduction, fixed, and re-verified with a full live `gardener overnight`
run that dispatched real work end to end through the exact scheduled
invocation path.

### Generic Linux — cron or a systemd timer

Neither of the above applies to a plain Linux server or desktop with a
working `cron`/`systemd` — the straightforward version, for a user with
`gardener` on `PATH` in their crontab's environment already:

```cron
# crontab -e
0 1 * * * /home/you/.venvs/gardener/bin/gardener overnight --hours 8
```

or, as a systemd user timer:

```ini
# ~/.config/systemd/user/gardener-overnight.service
[Service]
ExecStart=/home/you/.venvs/gardener/bin/gardener overnight --hours 8

# ~/.config/systemd/user/gardener-overnight.timer
[Timer]
OnCalendar=*-*-* 01:00:00
[Install]
WantedBy=timers.target
```

`cron`'s own invocation environment is famously minimal (no login shell,
no `.bashrc`) — the same PATH lesson from the Task Scheduler recipe above
applies here too: use an absolute path to the `gardener` binary (as
above) rather than relying on `PATH` at all, and don't assume anything
`~/.bashrc`/`~/.profile` sets up will be present.

## Device-wide failures abort the run instead of consuming the garden

Three dispatch outcomes say nothing about the repo being tended: broken
credentials, an exhausted usage/session window, and an unreachable GitHub.
All three are global to the device, all three fail in seconds rather than
minutes, and after any of them every remaining repo in the garden is about
to fail in exactly the same way. `overnight` therefore treats them
differently from an ordinary per-repo error — `dispatch.is_device_global_failure`
is the single predicate that recognizes all three:

1. An **auth** failure is **retried** a small number of times with backoff
   (`dispatch.py`'s `AUTH_RETRY_BACKOFF_SECONDS`, currently 30s/120s/300s),
   which is enough to ride out a token-refresh blip. Only auth failures are
   retried — a tend cycle that genuinely failed has already mutated its
   branch and needs triage, not a blind second run. A **usage limit** is
   deliberately *not* retried: it resets at a wall-clock hour routinely
   hours away, so the backoff cannot outlast it and would only burn the
   night's budget asleep. A **GitHub outage** strikes before dispatch (while
   resolving the default branch or cloning), so there is no dispatch to
   retry at all — it is classified from the raised error's text instead.
2. Whichever it was, the run then **stops**, and the resume cursor is **not
   advanced past the affected repo(s)** — so the next invocation re-attempts
   them rather than skipping them. Repos that tended successfully earlier in
   the run still count as done.
3. A dedicated ERROR notification fires alongside the usual batch summary,
   since the summary alone reads as a pile of ordinary per-repo errors and
   buries the one thing that actually needs a human. It **names the specific
   class**, because the recoveries are not interchangeable: log `claude`
   back in, wait out a quota reset, or check connectivity.

Note what this deliberately does *not* do: an ordinary repo failure (a
broken build, a failing test suite) never aborts the garden or holds the
cursor. One unhealthy repo stalling every subsequent night would be a worse
failure than the one this protects against.

Calibrated against real failures, not hypothetical ones — all three classes
have already consumed a cycle, and 64 of the 220 runs in one recorded
history were device-wide failures that the pre-existing logic charged to the
repos they hit:

| Date | Class | Cost |
|---|---|---|
| 2026-07-24 | auth (`Failed to authenticate: OAuth session expired and could not be refreshed`) | 12 of 15 repos in ~1 minute, each $0.00 and 2-5s; auth recovered on its own ~20 minutes later |
| 2026-07-20, 2026-07-25 | usage limit (`You've hit your session limit · resets 12am (UTC)`) | 35 repos, 20 of them in a four-minute window |
| 2026-07-21, 2026-07-25 | GitHub unreachable (`error connecting to api.github.com`, GraphQL `unexpected EOF`) | 17 repos |
| 2026-08-04 | GitHub unreachable in a third wording (`http2: client conn could not be established`) | 2 repos |

In every case the cursor advanced past the affected repos, so they stayed
untended for the night through no fault of their own.

The 2026-08-04 pair is the instructive one: the class was already covered,
but only by the two *wordings* recorded above, and Go's http2 transport
error is neither. Every one of these markers is a substring of some tool's
un-versioned error prose, so the marker set decays silently as those tools
reword — a marker set is not a fix you make once. The cheap periodic check
is to replay the recorded run history through the current classifier and
look at what it *doesn't* catch, since a device-wide outage is recognisable
in that history without any classifier at all: several repos failing within
the same second or two, each in seconds and at $0.00.

One caveat on every count on this page, including the 2026-08-04 row and the
"24 recorded failures" below: `state.py`'s sqlite db lives under
`GARDENER_STATE_DIR` on whichever machine ran the batch, and is never synced
between devices. Each figure therefore describes *one* device's history, not
a fleet-wide total, and replaying a different box's db will legitimately
produce different numbers — the 2026-08-04 pair, for instance, appears in no
run history but the device that hit it. Quote which device a replay figure
came from when adding a row here, and read a mismatch as the dbs being
separate rather than as one of them being wrong.

Also worth stating explicitly, because it is the tempting wrong fix: all 24
recorded failures of this class share the wrapper sentence `could not
determine default branch for <repo>`, and that sentence must **not** be a
marker. `cli.py`'s `_default_branch_name` raises it for a deleted, renamed,
or permission-denied repo just as readily as for an outage — matching it
would abort the whole batch, and park the resume cursor, on a repo that is
permanently gone. Only the transport error `gh` appends to it may classify.

## Dependency caches survive the target-repo refresh

Each `tend`/`align` run refreshes its cached clone
(`~/.cache/gardener/repos/<owner>__<repo>`) with fetch + `checkout -B` +
`git clean -fdx`, so no leftover state from a previous run can leak into
the next one. The clean explicitly **preserves** a short list of dependency
caches (`cli.py`'s `PRESERVED_DEPENDENCY_DIRS`: `node_modules`, `.venv`,
`venv`, `.gradle`) via `git clean`'s `-e` flag, which is still honored when
`-x` is passed. Build *outputs* (`build/`, `target/`, `dist/`) are
deliberately not preserved — a stale one leaking into a later run is
exactly what the `-x` clean exists to prevent, whereas a stale dependency
cache is just a download the next run doesn't have to repeat.

The clean step also gets its own, longer timeout
(`CLEAN_TIMEOUT_SECONDS`, 300s) than the network-bound fetch/checkout steps
(`REFRESH_TIMEOUT_SECONDS`, 60s), because it's bound by how fast this
device can unlink a large number of files. Both halves come from a real,
every-single-run failure: `Dans-Plugins/dansplugins-dot-com`'s cache clone
carries a 190MB `node_modules`, which `git clean -fdx` could not remove
inside the shared 60s timeout, so that repo failed with
`Command '['git', 'clean', '-fdx']' timed out after 60 seconds` before its
dispatch could even start.
