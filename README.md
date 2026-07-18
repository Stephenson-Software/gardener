# gardener

`gardener` is a safety-gated Python CLI that dispatches Claude Code against
a fleet of software repos, in two distinct ways:

- **`align`** checks one target repo at a time against
  [`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions)
  — a private repo of engineering conventions and alignment checklists —
  and, if authorized, fixes what's missing.
- **`tend`**/**`garden`**/**`overnight`** make real, broader progress on a
  repo (or a whole opt-in list of them, unattended overnight) by
  dispatching *that repo's own* `<slug>-dev-loop` Claude Code skill —
  triage, implement, test, PR — never merging without an explicit,
  separate per-repo opt-in.

gardener's own job is orchestration and safety-gating, in plain Python. The
actual reading/analysis/implementation judgment is delegated to a
dispatched, safety-gated `claude` CLI invocation in every mode — gardener
never itself decides what "aligned" means or what a fix should look like;
it only decides *how much a dispatched Claude run is allowed to do about
it*. See [Usage](#usage) below for the full command set.

## Description

- **`gardener align --repo <owner/repo>`**: clones the target repo
  read-only, clones (or refreshes) a local cache of
  [`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions)
  (phase 1 of this two-phase initiative — dms-conventions is the source of
  truth for what "aligned" means, gardener is the tool that consumes it),
  builds a prompt combining dms-conventions' `ALIGNMENT_PROMPT.md` with the
  target repo's identity and the requested mode's constraints, and
  dispatches one headless `claude -p` run to produce a gap checklist — or,
  if explicitly authorized, to act on it.
- **`gardener tend --repo <owner/repo>`**: dispatches the target repo's
  *own* `<slug>-dev-loop` skill instead — real triage/implement/test/PR
  work, not a conventions gap-check. See
  [`gardener tend`](#gardener-tend--dispatching-a-target-repos-own-dev-loop)
  below.
- **`gardener garden`** + **`gardener overnight`**: an opt-in list of repos
  and the unattended batch dispatcher that tends them one after another
  overnight. See [Overnight / unattended operation](#overnight--unattended-operation)
  below.

## Installation

### First Time Setup

Requires Python 3.10+, and the `git`, `gh`, and `claude` CLIs already
installed and authenticated (`gh auth status`, and a working `claude`
login) — gardener shells out to all three rather than reimplementing git
hosting, auth, or the agent loop itself.

```bash
git clone https://github.com/dmccoystephenson/gardener.git
cd gardener
pip install -e .
```

This installs the `gardener` console script (via `pyproject.toml`'s
`[project.scripts]` entry point) and leaves the source editable.

### Alerting (optional)

By default gardener alerts nowhere except its own local run history — you
have to run `gardener status` or watch terminal output to see how a run
went. To get a Discord notification on every run's outcome instead,
configure a webhook one of two ways (checked in this order):

```bash
# 1. Environment variable (simplest — set it wherever gardener runs)
export GARDENER_DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/XXX/YYY"

# 2. A gitignored config file, for a persistent/cron context where
#    exporting an env var per-invocation isn't practical (same shape as
#    Stephenson-Software/gateway's `.monitor.env`):
mkdir -p ~/.local/state/gardener   # or $GARDENER_STATE_DIR if overridden
umask 077
echo 'DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXX/YYY' \
  > ~/.local/state/gardener/notify.env
```

No webhook configured (neither of the above) means notifications are a
clean no-op — `gardener align` still works exactly the same, nothing
prints or fails because of it. See [Alerting design](#alerting-design)
below for how this is implemented.

## Usage

```
gardener align --repo <owner/repo> [--implement] [--file-issue]
gardener tend --repo <owner/repo> [--allow-merge]
gardener allowlist list | add --repo <owner/repo> | remove --repo <owner/repo>
gardener garden list | add --repo <owner/repo> | remove --repo <owner/repo>
gardener overnight [--hours N] [--concurrency N]
gardener status [--repo <owner/repo>]
gardener tail-transcript <path> [-f]
```

- **`gardener align --repo owner/repo`** (no flags) — **report-only,
  dry-run by default.** Dispatches Claude to read the target repo and
  dms-conventions' `ALIGNMENT_CHECKLIST.md`, and produce a gap checklist.
  Claude has no write or shell tool available in this mode — see
  [Safety model](#safety-model) — so it cannot modify the target repo,
  open a PR, or open an issue no matter what the prompt says. The gap
  checklist prints to stdout and is logged to gardener's local run
  history. If a Discord webhook is configured (see
  [Alerting (optional)](#alerting-optional)), an informational
  notification fires too.
- **`--implement`** — additionally authorizes Claude to implement fixes in
  the target repo: branch, commit, and open a PR, following *the target
  repo's own* conventions (language, build tool, test framework) rather
  than copying dms-conventions' stack-agnostic examples literally.
- **`--file-issue`** — instead of implementing, authorizes Claude to open
  one scoped GitHub issue in the target repo summarizing the gaps, for
  repos that already have their own `*-dev-loop` skill to pick the work up
  next cycle. Mutually exclusive with `--implement` (gardener errors if
  both are given — enforced twice: once by argparse's mutually-exclusive
  group, once explicitly in `cli.py`).
- **`gardener status [--repo owner/repo]`** — reads gardener's local
  SQLite run history (`~/.local/state/gardener/gardener.sqlite3` by
  default) and prints it: target repo, timestamp, mode, outcome summary.

### `gardener tend` — dispatching a target repo's own dev-loop

Where `align` checks a repo against dms-conventions, **`gardener tend
--repo owner/repo`** makes real, broader progress on the repo itself by
dispatching *that repo's own* `<slug>-dev-loop` Claude Code skill (the kind
normally invoked interactively as `/gateway-dev-loop`, `/dpm-dev-loop`,
etc.) — headlessly, unattended, safety-gated the same way every other mode
in this file is. This is for a "tend to my garden overnight" use case:
several repos, dispatched one after another, nobody watching.

1. Clones/refreshes `owner/repo` into gardener's cache, exactly like
   `align` does.
2. Derives the skill slug from the repo's actual name (`owner/repo` ->
   `repo-dev-loop`, matching the naming already used by every
   `<slug>-dev-loop` skill/repo in this ecosystem — see `dev_loop.py`),
   and checks whether `~/.claude/commands/<slug>-dev-loop.md` already
   resolves to a real file.
3. **If no skill exists yet**, dispatches `create-dev-loop` first (a
   distinct, more tightly scoped mode — see `dispatch.py`) to generate and
   register one, then confirms the file actually landed before proceeding.
   If that dispatch fails or the file still isn't there afterward, `tend`
   stops and reports an error rather than guessing.
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
not assumed — see [Safety model](#safety-model)'s "Headless dispatch and
the `AskUserQuestion` problem" section for exactly what was tested and
observed.

### Orphaned work recovery

If this device kills the whole `gardener overnight` process mid-dispatch
(see "Wiring it to 'tend to my garden while I sleep'" below), the repo it
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

### Merge allow-list

`gardener allowlist add --repo owner/repo` / `remove --repo owner/repo` /
`list` manage a small local JSON file (`~/.local/state/gardener/
merge_allowlist.json` by default — same directory `status`'s run history
lives in, overridable via `GARDENER_STATE_DIR`) of repos `gardener tend
--allow-merge` is permitted to actually merge PRs in. A repo not listed
here can never be merged into by a `tend` dispatch, no matter what flags
are passed — see `merge_allowlist.py` and `dispatch.py`'s `tend_mode_spec()`.

### Overnight / unattended operation

**`gardener garden`** manages a second, independent opt-in list — separate
from the merge allow-list above — of repos `gardener overnight` is allowed
to tend while nobody is watching. Same file-location convention and same
safe default as the merge allow-list: `gardener garden add --repo
owner/repo` / `remove --repo owner/repo` / `list` manage a small local JSON
file (`~/.local/state/gardener/garden.json` by default, overridable via
`GARDENER_STATE_DIR`), and a missing file means an empty garden — a repo is
never touched overnight just because it exists on this machine, only
because it was explicitly added. See `gardener/garden.py`.

**`gardener overnight [--hours N] [--concurrency N]`** is the actual "tend
to my garden while I sleep" entry point:

1. Reads the garden. An empty garden prints a clear message and exits `0`
   — nothing to do is not an error.
2. Dispatches `gardener tend --repo <repo> --allow-merge` **in-process**
   (calls `_dispatch_tend` directly, no `gardener` subprocess-of-itself) for
   each garden repo, in batches of `--concurrency` repos at a time (default
   `1`, i.e. strictly one after another — unchanged from before this flag
   existed), starting from wherever the *previous* `overnight` run left off
   (see "Resuming across nights" below), until either the garden is
   exhausted for this run or the time budget runs out. Repos within a batch
   run concurrently on a `ThreadPoolExecutor` (stdlib-only) when
   `--concurrency` > 1 — each is still just one independent, blocking
   `claude -p` subprocess (see [Why synchronous
   dispatch](#why-synchronous-dispatch)), now several running in parallel
   OS processes rather than one at a time. This device has no true process
   isolation and real, shared CPU/RAM (see the "no true always-on daemon
   guarantee" caveat below) — `--concurrency` stays `1` unless you
   explicitly raise it with that tradeoff in mind.
3. `--allow-merge` is passed unconditionally to every dispatch. This is
   safe *without* `overnight` needing any merge-decision logic of its own:
   `tend`'s own `merge_eligible()` check still requires the target repo to
   *also* be present on the separate merge allow-list before `gh pr merge`
   is ever reachable in the dispatched session (see "Merge allow-list"
   above) — being in the garden alone never authorizes a merge. The garden
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
   dispatches (79-250s observed in practice — see Project Status) can fit
   more repos than the naive arithmetic would suggest. A repo already in
   progress is never hard-killed mid-run to respect the budget; the budget
   only gates whether a *new* repo is started.
5. **Resuming across nights.** If the garden is longer than one night's
   budget can cover, a small resume cursor
   (`~/.local/state/gardener/overnight_cursor.json`) tracks which repo to
   start from next, round-robin — so a garden of 20 repos and a budget that
   only fits 6 per night eventually reaches every repo across several
   nights instead of only ever tending the first 6. See `gardener/overnight.py`.
6. **Notifications.** Each repo's own outcome is logged and alerted via the
   *existing* `state.record_run`/`_notify_run` machinery `_dispatch_tend`
   already uses (unchanged, and safe to call from more than one thread at
   once — see [Alerting design](#alerting-design)) — you get one
   Discord message per repo for free. `overnight` additionally fires **one
   summary notification at the end of the whole batch** — total repos
   attempted, how many opened a PR, how many merged, how many hit a
   `DECISION NEEDED:` line, how many errored, and elapsed time — so you wake
   up to one clear digest instead of piecing together N separate messages.
   With no Discord webhook configured, the summary is still printed to
   stderr; the notification call itself is a clean no-op (`NullNotifier`,
   see [Alerting (optional)](#alerting-optional)), not a failure.
7. One repo failing or timing out does not abort the batch — it's logged,
   notified, and `overnight` moves on to the next repo.

#### Wiring it to "tend to my garden while I sleep"

This device (a UserLand/Android sandbox) has no systemd, no cron, and no
true always-on daemon guarantee — Android can and will kill background
processes on a task swipe-away, aggressive battery/OOM management, or
extended idle, the same way it kills every other background process on
this machine. There is no way around that here. What `~/pocket-rig/bin/
devsrv` gives you instead: the command is *registered* so it's visible,
restartable without retyping it, and comes back on its own the next time an
interactive shell starts (wired into `.bashrc`) if it gets killed —
**not** an uninterrupted guarantee that it runs the whole night no matter
what.

The actual invocation:

```bash
devsrv start gardener-overnight --autostart -- gardener overnight --hours 8
devsrv status gardener-overnight   # confirm it's running
devsrv logs gardener-overnight -f  # watch progress live
```

`--autostart` means: if Android kills every background process (confirmed
directly to happen on a task swipe-away), this specific run will NOT
silently resume mid-budget on its own — the `claude` subprocess it was
waiting on is gone too. What `--autostart` actually buys you is that the
*next* interactive shell you open on this device re-runs `devsrv
autostart-all`, which restarts `gardener overnight --hours 8` fresh (a new
8-hour budget from that point, not a continuation of the old one) if it
isn't already running. Combined with the resume cursor above, a run that
gets interrupted partway through the garden doesn't lose progress on the
repos it already finished — the next invocation (autostart-triggered or
manually re-run) picks up from the next untended repo, not the top of the
list again. The repo that was *actively* being tended at the moment of the
kill is still re-dispatched fresh on the next run (the resume cursor only
advances past *completed* repos) — but that re-dispatch now recognizes and
continues any PR the interrupted session already opened rather than
starting a duplicate; see "Orphaned work recovery" above.

### Other flags

- `--model <name>` — override the model `claude` uses (`align`, `tend`, and
  `overnight`, which threads it through to every `tend` dispatch in the
  batch).
- `--timeout <seconds>` — how long to wait for the dispatched run.
  `align`'s default is 1800s / 30 min (reading and analyzing a real repo
  against ~10 convention docs is not fast). `tend`'s default is 2700s / 45
  min — it runs a full triage/implement/test/PR/self-audit cycle in one
  session with no subagent fan-out or wait-for-review loop (see
  [Safety model](#safety-model)), so it needs more room than `align`, but
  must still fail fast enough that one stuck repo can't consume an entire
  overnight multi-repo batch; see `dispatch.py`'s
  `TEND_DEFAULT_TIMEOUT_SECONDS` for the full reasoning. The internal
  `create-dev-loop` dispatch `tend` runs when a repo has no skill yet has
  its own fixed 900s/15min ceiling, not exposed as a flag.
- `--no-refresh-conventions` (`align` only) / `--no-refresh-target`
  (`align` and `tend`) — reuse whatever is already cached instead of
  fetching latest first.

### Live session visibility

`align`, `tend`, and `overnight` still dispatch synchronously (see [Why
synchronous dispatch, not `--bg`](#why-synchronous-dispatch-not---bg)
below) and only capture output when the whole run finishes — for `tend`,
that's up to `TEND_DEFAULT_TIMEOUT_SECONDS` (45 min) per repo with
otherwise zero visibility into what's happening while it runs, a real gap
for anyone watching `devsrv logs gardener-overnight -f` or the dashboard's
log viewer overnight.

Claude Code already solves the actual data half of this on its own, for
every `-p` session, no special flag required: it writes a JSONL transcript
— growing in real time — to `~/.claude/projects/<encoded-cwd>/<session-
id>.jsonl`. Every dispatch now surfaces that file's path within seconds of
starting, printed to stderr the same way every other `gardener:` progress
line is:

```
gardener: session transcript: /home/userland/.claude/projects/-home-userland...-repo/<session-id>.jsonl (tail -f it for live detail, or `gardener tail-transcript -f <path>`)
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

## Support

### Experiencing a bug?

Please file a bug report [here](https://github.com/dmccoystephenson/gardener/issues).

## Contributing

- [CONTRIBUTING.md](CONTRIBUTING.md) — not yet written (see
  [Project Status](#project-status)); until then, open an issue or PR
  directly and ground any convention proposal in dms-conventions the same
  way dms-conventions grounds its own docs in a real source repo.

## Testing

### Automated Tests

Linux/macOS:

    PYTHONPATH=. python3 -m unittest discover -s tests -v

Windows (PowerShell):

    $env:PYTHONPATH = "."; python -m unittest discover -s tests -v

A passing run ends with `OK`. `tests/test_dispatch.py` mocks
`subprocess.run` and never actually invokes `claude`; `tests/test_state.py`
uses a real sqlite3 file in a tmp dir; `tests/test_cli.py` covers argument
parsing, prompt templating, `_notify_run`'s severity mapping (mocking the
notifier, not `state.Run` construction), `cmd_tend` with clone/dispatch
mocked, and `cmd_overnight` with `_dispatch_tend` itself mocked — including
its `--concurrency` batching (one test asserts every repo in a
`ThreadPoolExecutor`-dispatched batch still gets attempted regardless of
completion order, another asserts `concurrency=1` never touches
`ThreadPoolExecutor` at all) — and, where the budget/headroom logic
specifically is under test, `time.monotonic` mocked too, so timing
assertions never depend on wall-clock jitter;
`tests/test_notify.py` mocks `urllib.request.urlopen` so `DiscordNotifier`
is fully covered — success, a failed POST, and "no webhook configured" —
without ever making a real HTTP call; `tests/test_garden.py` and
`tests/test_overnight.py` cover the garden JSON list and `overnight.py`'s
pure rotation/batching/budget/resume-cursor/outcome-classification logic
with real files in a tmp dir; `tests/test_transcript.py` covers the encoding rule
(against the two real, empirically-confirmed examples in `transcript.py`'s
module docstring, not invented ones), the transcript-file-discovery polling
loop (real files in a tmp dir, but `time_fn`/`sleep_fn` always injected so
nothing ever sleeps for a real second), and the pretty-printer's
line-parsing logic (synthetic JSONL fixtures covering `tool_use`/`text`/
`tool_result`, malformed JSON, and blank lines). None of the automated
tests hit the network or a real repo, or invoke a real `claude` process —
see [Manual/end-to-end verification](#manualend-to-end-verification) for
that.

### Manual/end-to-end verification

Because the whole point of this tool is dispatching a real Claude Code
run against a real repo, the automated suite deliberately can't cover the
full path end to end. Before trusting a change to `dispatch.py` or the
prompt template, run a real report-only pass against a low-stakes repo you
have access to and confirm three things:

1. `gardener align --repo <owner/repo>` exits 0 and prints a gap checklist
   ending in a `GARDENER_SUMMARY:` line.
2. `git -C <the cached clone> status --porcelain` is empty and `git log`
   shows no new commits — report mode must not have touched the clone.
3. `gh repo view <owner/repo> --json pushedAt` is unchanged from before the
   run, and `gh pr list` / `gh issue list` show nothing new — report mode
   must not have touched the real repo on GitHub either.

This exact sequence is what verified gardener's first working version
against `dmccoystephenson/create-dev-loop`.

**`gardener overnight`**: since it dispatches `tend` in-process per repo,
its own verification is the same as `tend`'s above, repeated per repo in
the garden, plus three things specific to the batch layer itself: (1) run
with a small `--hours` (e.g. `0.1`-`0.2`) against a garden of 1-2 low-stakes
repos and confirm it dispatches at least the first repo and stops well
short of running indefinitely; (2) run it twice in a row against a garden
too big for one budget window and confirm the second run tends a
*different* repo than the first (the resume cursor advanced, not reset);
(3) confirm exactly one additional summary notification fires per
invocation, on top of each repo's own per-repo notification. See Project
Status below for the actual run this verified against.

**`--concurrency > 1` specifically** has not yet had its own real,
end-to-end verification run on this device as of this writing (see Project
Status) — the automated suite covers the orchestration logic (batching,
per-batch ordering, one repo's crash not aborting the batch) with
`_dispatch_tend` mocked, but real concurrent `claude -p` processes
contending for this device's actual CPU/RAM is a different thing to
confirm than mocked logic. Before relying on `--concurrency > 1` in an
unattended overnight run, do a small real check first: `--hours 0.1
--concurrency 2` against a garden of 2 low-stakes repos, confirming both
dispatch, both get their own per-repo notification, and neither's recorded
`state.Run`/notification data is corrupted or swapped with the other's
(the exact failure mode the old `redirect_stdout`-based capture would have
been vulnerable to — see [issue
#15](https://github.com/dmccoystephenson/gardener/issues/15)).

**Alerting**: `DiscordNotifier` is covered by mocked unit tests (see
above) rather than a real Discord send in the automated suite — same
reasoning as the rest of this section, a real send is an environment
check, not something the test suite should depend on. To verify it for
real once a webhook is configured (see
[Alerting (optional)](#alerting-optional)), run:

```bash
python3 -c "
from gardener.notify import DiscordNotifier, Level
DiscordNotifier().notify('gardener: manual test', 'if you see this in Discord, alerting works', Level.SUCCESS)
"
```

and confirm the embed shows up in the configured channel with a green
(`3066993`) color bar. Use a webhook pointed at a private/test channel
for this, not a shared production alerting channel.

## Safety model

gardener never invokes `claude` with `bypassPermissions` or any
equivalent auto-approve-everything mode, for any mode, under any flag
combination — this is enforced in `dispatch.py` (`_build_invocation`
raises rather than silently proceeding if it's ever reached), mirroring
[`~/pocket-rig/dashboard/`](https://github.com/dmccoystephenson/pocket-rig)'s
existing Claude-dispatch endpoint, which hard-rejects `bypassPermissions`
server-side the same way.

Beyond that floor, each mode is built from three independently-confirmed
layers (see the full writeup in `dispatch.py`'s module docstring):

1. **`--tools`** is a hard allow-list of which tool *types* exist in the
   session at all — report mode gets only `Read,Grep,Glob`. Confirmed
   directly: a session given that tool list has no Write/Edit/Bash object
   to call, not merely one that's denied.
2. **`--permission-mode default`** for implement/file-issue — every tool
   call needs approval, and headless mode has no human to give it, so
   anything not pre-approved is auto-denied (confirmed directly, and
   logged back via the JSON result's `permission_denials`). `acceptEdits`
   was tried and rejected: it auto-approved a Bash call that was
   deliberately left unscoped, defeating the point.
3. **`--allowedTools`** pre-approves narrow patterns (`Bash(git *)`,
   `Bash(gh pr *)` for implement; `Bash(gh issue *)` only, no Edit/Write at
   all, for file-issue) so only those specific commands run without a
   human.

`--strict-mcp-config` (with no `--mcp-config`) is passed for every mode,
so a dispatched run never inherits whatever MCP servers (Gmail, Drive,
Calendar, ...) happen to be configured for the invoking user.

### `tend` mode, and the headless "ask the user" problem

Every `<slug>-dev-loop` skill `tend` might dispatch is written to *stop and
ask a human* (via the `AskUserQuestion` tool) before merging or taking a
destructive action — real, observed behavior in every dev-loop skill on
this machine. Dispatched headlessly by `gardener tend`, nobody is there to
answer. This was confirmed by hand (2026-07-18, Claude Code CLI 2.1.214)
before being relied on, the same standard the rest of this safety model is
held to — full transcript-level detail lives in `dispatch.py`'s module
docstring; the short version:

- **`AskUserQuestion` is not present at all in a headless `-p` session, by
  default** — a session given `--tools default` (every built-in tool) was
  asked to list every tool available to it, direct or deferred; it wasn't
  in either list. `tend` never needs to add it to a `--tools` allow-list to
  exclude it — it's already absent before gardener does anything.
- **Without it, a session doesn't hang or fabricate an answer** — told
  (mirroring a real dev-loop's merge-confirmation language) to get human
  sign-off via `AskUserQuestion` before merging, a session with no such
  tool returned cleanly in 22s explaining it had no way to ask and would
  not proceed or invent a substitute. `tend`'s dispatched prompt still
  states this explicitly (`dev_loop.py`'s `HEADLESS_SAFETY_PREAMBLE`)
  rather than relying on that being the default every time: leave whatever
  was safely completed in place, and end with one line starting
  `DECISION NEEDED:` naming what's needed and by whom.
- **`Agent` and `ScheduleWakeup`** (used by a normal dev-loop cycle to fan
  out a review subagent and poll for its result) **are present by default
  and are excluded from `tend` mode's `--tools` the same structural way**
  — confirmed live: a session scoped to `tend`'s actual tool list reported
  back exactly that list, no Agent/ScheduleWakeup/AskUserQuestion/
  ToolSearch/ReportFindings among them. The dispatched prompt tells the
  run to perform any review step inline instead.
- **`gh pr merge` is never reachable via a broad `Bash(gh pr *)` pattern**
  in `tend` mode — confirmed live that such a pattern *does* let `gh pr
  merge` execute (this is a real, pre-existing gap in `align --implement`'s
  own `Bash(gh pr *)` allow-list, noted but out of scope to change here).
  `tend` enumerates specific non-merge `gh pr` subcommands instead and adds
  `Bash(gh pr merge *)` only per the merge allow-list below.

### Merge allow-list mechanics

`gardener tend --allow-merge` only ever results in `Bash(gh pr merge *)`
being added to the dispatched session's `--allowedTools` when BOTH
`--allow-merge` was passed AND the target repo is present in
`merge_allowlist.py`'s local JSON list (`gardener allowlist add`). Either
alone leaves the pattern out of the argv gardener builds entirely — under
`--permission-mode default` with nobody to approve an unlisted tool call,
an attempted `gh pr merge` in that case is auto-denied the same way any
other out-of-scope Bash call is (confirmed general mechanism, see point 2
in the layer list above) — not merely discouraged in the prompt.

### Why synchronous dispatch, not `--bg`

pocket-rig's dashboard uses `claude --bg` because it's answering an HTTP
request that can't hang open. `gardener align` is invoked from a terminal
or cron and can reasonably block until Claude finishes, so it uses
`claude -p` (headless "run once, print, exit" mode) instead — one
subprocess call, full output captured synchronously via
`--output-format json`, a meaningful exit code, nothing left running in
the background after the command returns. This is what makes
`gardener align`'s output pipeable and scriptable in a way `--bg` isn't.

## Alerting design

`gardener/notify.py` defines a small `Notifier` abstraction so alerting
isn't hardcoded to Discord — `Notifier` is an ABC with one method,
`notify(title, message, level)`, where `level` is a `Level` enum
(`INFO`/`SUCCESS`/`WARNING`/`ERROR`) that each concrete notifier maps to
its own presentation.

- **`DiscordNotifier`** — the first/primary implementation. Posts a
  Discord embed via a webhook URL using stdlib `urllib.request` only (no
  `requests` dependency — see [Architecture](#architecture)). Mirrors
  `Stephenson-Software/gateway`'s `monitoring/notify-discord.sh` exactly:
  same title/description/color embed shape, same "no webhook configured →
  log and return, never fail the caller" behavior, and the same Discord
  embed colors for success (`3066993`, green) and error (`15158332`,
  red); the warning color (`16776960`, yellow) matches that repo's
  `cert-check.sh` "expiring soon" alert. **Never raises** — a bad webhook,
  a network error, or Discord being down must never be able to break the
  actual `gardener align` run it's reporting on; every failure path is
  caught and logged to stderr instead.
- **`NullNotifier`** — a clean no-op, returned automatically when no
  webhook is configured (see [Alerting (optional)](#alerting-optional)).
  "Nothing configured" is a normal state, not an error path callers need
  to special-case.
- **`CompositeNotifier`** — fans one notification out to zero-to-many
  concrete notifiers, for anyone who wants to register more than one
  destination later without touching any call site.

`cli.py` wires this in with one thin helper, `_notify_run(run)`, called
right after each place `state.record_run(...)` is called in `cmd_align`.
It owns the only alerting *business logic* in the codebase — turning a
recorded `state.Run`'s `outcome`/`mode` into a severity — deliberately
kept out of `notify.py` itself, which only knows how to present an
already-decided `(title, message, level)`:

- `outcome == "error"` → always `Level.ERROR`, regardless of mode. This is
  the case most worth getting right (an error is the easiest outcome to
  silently miss without an alert), so it's checked first and
  unconditionally.
- `mode == "report"` (report-only, no mutation possible — see
  [Safety model](#safety-model)) → `Level.INFO`.
- Anything else (`--implement`, `--file-issue`, or any future mode that
  also authorizes a mutation) → `Level.WARNING`, so a run that actually
  branched/committed/opened a PR or issue stands out from a routine
  report-only run rather than blending in with it. This check is written
  as an `else`, not an explicit list of mode names, specifically so a
  future mode never has to be added to `notify.py` or `_notify_run` by
  hand to be covered — it falls into the mutation branch automatically
  unless it's literally `"report"`.

## Development

No build step — this is a stdlib-only Python CLI (see
[Architecture](#architecture) below). Clone it, `pip install -e .`, edit,
re-run the tests.

## Project Status

Working end to end: built, unit-tested, and verified with one real
report-only run against `dmccoystephenson/create-dev-loop` (29 gaps found,
target repo confirmed untouched afterward — see
[Manual/end-to-end verification](#manualend-to-end-verification)).
`--implement` and `--file-issue` are implemented and unit-tested at the
argv-construction level but have not yet been run for real against a live
repo — only report-only mode has been exercised end to end so far.

`tend` (no `--allow-merge`) has also been run for real, end to end,
against `dmccoystephenson/snmp-command-generator` (2026-07-18): no
existing `/snmp-command-generator-dev-loop` skill, so `create-dev-loop`
dispatched first (194s, $0.71, produced a usable skill), then `tend`
dispatched it (250s, $1.00, `ok=True`, 7 permission denials — out-of-scope
attempts correctly blocked, not itemized in gardener's own output beyond
the count). Result: the dispatched run found no open issues/PRs, added
missing CLI test coverage for two untested code paths, opened
[PR #5](https://github.com/dmccoystephenson/snmp-command-generator/pull/5)
(green on all 4 CI matrix legs), did not attempt to merge it, and ended
its final answer with a `DECISION NEEDED:` line naming the PR and that a
human must review/merge — exactly the fallback documented in
[the `tend` mode section above](#tend-mode-and-the-headless-ask-the-user-problem).
Confirmed afterward: `origin/main` on the target repo is unchanged
(`947474e`, the same commit as before the run) and PR #5 remains open,
not merged — `tend` without `--allow-merge` made real progress without
ever mutating the target repo's main branch. The run also completed in
250s, far inside `TEND_DEFAULT_TIMEOUT_SECONDS` (2700s), so the
"next cycle" loop-back risk noted in `dispatch.py`'s docstring did not
manifest here either.

`tend --allow-merge` has also been run for real, end to end, against the
same repo (2026-07-18), after deliberately adding it to the merge
allow-list first (`gardener allowlist add --repo
dmccoystephenson/snmp-command-generator`) — chosen because PR #5 was
already known-safe (test-only diff, +38/-0, zero application-code
changes, green on all 4 CI matrix legs, already reviewed once during the
no-flags run above). The dispatched run (79s, $0.32, `ok=True`, one
permission denial — an out-of-scope `find /` blocked, unrelated to
merging) re-confirmed CI was green, then squash-merged PR #5. Confirmed
afterward via the GitHub API: PR #5's `merged` flag is `true`, `main`'s
HEAD commit is exactly `Add test coverage for CLI output-file writing and
no-valid-data exit (#5)`, and the post-merge CI run on `main` completed
`success`. This is the one dispatch mode capable of a real merge, and it
worked exactly as designed: the merge pattern was reachable only because
both `--allow-merge` and the allow-list entry were present (see
`dispatch.py`'s `tend_mode_spec()`) — this is a deliberate, real merge
into a low-stakes personal repo, not an accidental one; it was not
attempted, and would not have been permitted, without both conditions
explicitly set up first.

`gardener overnight` has also been run for real, end to end (2026-07-18),
against a 2-repo garden (`dmccoystephenson/create-dev-loop`,
`dmccoystephenson/snmp-command-generator`) with an isolated
`GARDENER_STATE_DIR` and an empty merge allow-list (deliberately — neither
repo was eligible to merge for this test, same "err toward not merging"
posture as the `tend --allow-merge` verification above), invoked twice in a
row with `--hours 0.15` (a small budget, on purpose, so the run cycle could
be observed without waiting out a full 8h window):

- **Run 1**: started at the resume cursor's default (index 0,
  `create-dev-loop`, alphabetically first). No `/create-dev-loop-dev-loop`
  skill existed yet under that exact derived slug (the repo's actual local
  skill was hand-named `cdl-dev-loop`, a different slug than gardener
  derives from the repo name — see `dev_loop.py`'s slug derivation), so
  `tend` bootstrapped one via `create-dev-loop` first, then dispatched it
  (403.6s dispatch, $1.87, `ok=True`, 3 permission denials — correctly
  blocked out-of-scope attempts). It opened PR #55, ended with a
  `DECISION NEEDED:` line (merge wasn't authorized — the repo isn't on the
  merge allow-list), and correctly did **not** attempt a second repo:
  `overnight stopping — insufficient budget remaining for another repo
  (-238s left, need 2700s headroom)`. The batch summary
  (`1 repo(s) attempted in 13.0m — 1 PR(s) opened, 0 merged, 1 awaiting a
  decision, 0 errored, 1 not reached this run`) printed to stderr exactly
  as designed (no Discord webhook was configured for this test, so the
  notify call itself was a clean `NullNotifier` no-op — see
  [Alerting (optional)](#alerting-optional)). The resume cursor advanced to
  index 1.
- **Run 2**: re-invoked identically. Correctly resumed at index 1
  (`snmp-command-generator`), NOT index 0 again — confirming the
  round-robin resume mechanism works across separate invocations, not just
  within a loop in one process. That repo already had its
  `/snmp-command-generator-dev-loop` skill (no bootstrap needed this time,
  hence a faster overall run: 340.2s dispatch, $1.22, `ok=True`, 4
  permission denials), opened PR #6, again ended in `DECISION NEEDED:`, and
  again stopped before attempting a second repo (`193s left, need 2700s
  headroom`). The cursor wrapped back to index 0 — both repos in the
  garden had now been attempted exactly once across the two runs.

Confirmed afterward via `gh pr list`: PR #55 (`create-dev-loop`) and PR #6
(`snmp-command-generator`) are both open, not merged — `overnight` made
real progress on two separate repos across two separate invocations without
ever mutating either target's default branch, exactly as designed. The
`devsrv` wiring documented above was also verified for real: `devsrv start
<name> --autostart -- gardener overnight --hours 8` (tested against both
the exact real command — confirmed its stderr output lands correctly in
`devsrv logs`, though a run against an empty garden exits before devsrv's
brief startup poll window closes, which devsrv reports as "failed to
start" even though it ran correctly and exited 0 — an artifact of testing
with a near-instant no-op, not a real failure mode for an actual
multi-repo overnight run — and, separately, a long-running placeholder
process to confirm the full `start`/`status`/`stop`/`restart`/`remove`
lifecycle and `--autostart` persistence all work exactly as
`~/pocket-rig/bin/devsrv`'s own docs describe).

**Live transcript visibility** (see [Live session
visibility](#live-session-visibility)) has also been run for real
(2026-07-18), end to end, with a real `gardener align --repo
dmccoystephenson/Simple-Calculator-GUI-Using-SDL` (report-only, an
unrelated low-stakes repo — deliberately not `Stephenson-Software/gateway`,
`dmccoystephenson/pocket-rig`, or `dmccoystephenson/gardener`, all still
possibly in use by a concurrent `gardener overnight` run at the time),
isolated to a scratch `GARDENER_CACHE_DIR`/`GARDENER_STATE_DIR` and with
stderr captured to a file for timestamped evidence:

- The dispatched `claude` subprocess's own transcript file records its
  first JSONL line at `15:00:58.278Z`. gardener's `gardener: session
  transcript: ...` line was already present in the captured stderr log the
  very next time it was checked, at essentially the same wall-clock second
  — comfortably inside the 5-second poll bound `transcript.py` uses.
- The dispatch itself did not finish until `gardener: done in 154001ms`
  (~154s later, landing at approximately `15:03:32Z`) — so the transcript
  path was visible roughly **2.5 minutes before the dispatch completed**,
  the actual gap this feature exists to close.
- While the dispatch was still in progress, `gardener tail-transcript
  <path>` (no `-f`) was run against the live, growing file and printed a
  real, readable, partial event stream — `Glob`/`Read` tool calls with
  their inputs and truncated results, and assistant reasoning text — well
  before the run finished.
- Afterward: `git -C <the cached clone> status --porcelain` was empty,
  `git log -1` showed no new commit, and `gh repo view ... --json
  pushedAt`/`gh pr list`/`gh issue list` were all unchanged from before the
  run — report mode's existing safety guarantee (see [Manual/end-to-end
  verification](#manualend-to-end-verification)) held with this addition
  in place, exactly as with every dispatch mode before it.

**`create-dev-loop`'s `add_dirs` fix** (see `dispatch.py`'s module
docstring finding #8) has also been verified for real, end to end
(2026-07-18). Root cause: `cmd_tend`'s `create-dev-loop` dispatch never
passed `add_dirs`, unlike `align`'s `add_dirs=[conv.path]` — `Write` isn't
sandboxed to `cwd`/`--add-dir` (finding #3), but `Read`/`Bash` are, so a
stale partial skill file from an earlier failed attempt had no recovery
path on retry. This was root-caused directly from a real
`gardener overnight` run's transcripts, then confirmed for real with the
actual failure's leftover artifact still on disk
(`~/local-skills/gardener-dev-loop/gardener-dev-loop.md` existed,
`~/.claude/commands/gardener-dev-loop.md` did not):

- An isolated scratch-directory probe (a real `claude -p` invocation using
  `MODE_SPECS[Mode.CREATE_DEV_LOOP]`'s exact tool/`--allowedTools` list,
  unchanged, plus `--add-dir` for two throwaway directories) confirmed
  `--add-dir` alone was sufficient: `Read` on a pre-existing file,
  `Bash(mkdir *)`, `Write` (fresh and overwrite-after-read), `Bash(ln -sf
  ...)`, and `Bash(ls -la ...)` all succeeded, zero `permission_denials` —
  no `MODE_SPECS` tool/pattern change was needed.
- After the fix, the stale artifact was removed
  (`rm -rf ~/local-skills/gardener-dev-loop`) and a real
  `gardener tend --repo dmccoystephenson/gardener` (no `--allow-merge`) was
  dispatched from this fix's own code. `create-dev-loop` succeeded this
  time: both `~/local-skills/gardener-dev-loop/gardener-dev-loop.md` and
  the `~/.claude/commands/gardener-dev-loop.md` symlink existed
  immediately afterward. `tend` then proceeded past the bootstrap step and
  dispatched `/gardener-dev-loop` itself (591.6s, $2.33, `ok=True`, 9
  permission denials — correctly blocked out-of-scope attempts), which
  found real work (two open bugs, #2 and #3), opened
  [PR #7](https://github.com/dmccoystephenson/gardener/pull/7) hardening
  `cmd_align`/`cmd_tend`/`cmd_overnight` against raw crashes from
  subprocess timeouts and corrupted garden/allow-list JSON, and correctly
  ended with a `DECISION NEEDED:` line (merge wasn't authorized). Confirmed
  afterward: `origin/main` is unchanged (`030d3fc`, same commit as before
  the run) and PR #7 remains open, not merged — gardener's first real
  self-tend made genuine progress on its own codebase without ever
  mutating its own main branch.

## Architecture

Stdlib-only Python — no third-party pip dependencies. This matches the
established house style in this ecosystem (see e.g.
`Stephenson-Software/gateway`'s `services/gateway-dashboard/app.py`: "Plain
stdlib Python... no build step / third-party deps"). gardener shells out
to three external CLIs (`git`, `gh`, `claude`) rather than reimplementing
git hosting, GitHub API auth, or an agent loop — that's a process
dependency, not a pip one, and is the same shape pocket-rig's tools use.

```
gardener/
  gardener/
    cli.py          — argparse CLI (align, tend, allowlist, status), prompt
                       building, orchestration
    dispatch.py      — the safety-gated subprocess wrapper around `claude -p`
                       (Mode/ModeSpec definitions and tend_mode_spec() for
                       every mode, including tend's per-invocation merge gate)
    dev_loop.py      — resolves/derives a target repo's <slug>-dev-loop skill,
                       builds the create-dev-loop and tend prompts (including
                       the headless-safety preamble)
    merge_allowlist.py — local JSON allow-list of repos `tend --allow-merge`
                       is permitted to actually merge PRs in
    garden.py        — local JSON opt-in list of repos `gardener overnight`
                       is permitted to tend unattended (independent of the
                       merge allow-list above — see garden.py's docstring)
    overnight.py     — pure budget/rotation/batching/resume-cursor/outcome-
                       classification logic for `gardener overnight`;
                       cli.py's cmd_overnight composes it with real time and
                       a real (in-process, optionally concurrent via
                       ThreadPoolExecutor) tend dispatch
    conventions.py   — clones/refreshes the local dms-conventions cache
    state.py         — SQLite-backed run history
    notify.py        — pluggable outcome notifications (Notifier/DiscordNotifier/NullNotifier)
    transcript.py    — live transcript-file discovery (encoding rule + bounded
                       poll, run from a background thread `dispatch.run_claude`
                       starts) and the `gardener tail-transcript` pretty-printer
    prompts/align_repo.md.tmpl — the prompt template dispatched to Claude
  tests/             — unit tests (state, cli parsing/templating/notify-severity,
                       mocked dispatch, notify, garden, overnight, transcript)
```

## Relationship to dms-conventions

gardener consumes [`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions)
as its only source of truth for what "aligned" means — it clones a local
cache of that repo (default `~/.cache/gardener/dms-conventions`, refreshed
on every run unless `--no-refresh-conventions` is given) and reads its
`ALIGNMENT_PROMPT.md`, `ALIGNMENT_CHECKLIST.md`, and every doc under
`docs/` to build the prompt it dispatches. gardener adds no conventions of
its own beyond the safety/dispatch mechanics described above — it doesn't
decide what a README or CI workflow should look like, dms-conventions
does.
