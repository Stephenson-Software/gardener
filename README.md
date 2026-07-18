# gardener

`gardener` is a Python CLI that aligns one target software repo at a time
against [`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions)
— a private repo of engineering conventions and alignment checklists.
gardener is **phase 2** of a two-phase initiative: dms-conventions (phase 1)
is the source of truth for what "aligned" means; gardener is the tool that
consumes it.

gardener's own job is orchestration and safety-gating, in plain Python. The
actual reading/analysis/implementation judgment — what's missing, what a
fix should look like — is delegated to a dispatched, safety-gated `claude`
CLI invocation. gardener never itself decides what "aligned" means; it only
decides *how much a dispatched Claude run is allowed to do about it*.

## Description

Run against a repo, gardener clones it read-only, clones (or refreshes) a
local cache of dms-conventions, builds a prompt combining
dms-conventions' `ALIGNMENT_PROMPT.md` with the target repo's identity and
the requested mode's constraints, and dispatches one headless `claude -p`
run to produce a gap checklist — or, if explicitly authorized, to act on
it.

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
gardener status [--repo <owner/repo>]
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

### Merge allow-list

`gardener allowlist add --repo owner/repo` / `remove --repo owner/repo` /
`list` manage a small local JSON file (`~/.local/state/gardener/
merge_allowlist.json` by default — same directory `status`'s run history
lives in, overridable via `GARDENER_STATE_DIR`) of repos `gardener tend
--allow-merge` is permitted to actually merge PRs in. A repo not listed
here can never be merged into by a `tend` dispatch, no matter what flags
are passed — see `merge_allowlist.py` and `dispatch.py`'s `tend_mode_spec()`.

### Other flags

- `--model <name>` — override the model `claude` uses (`align` and `tend`).
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
parsing, prompt templating, and `_notify_run`'s severity mapping (mocking
the notifier, not `state.Run` construction); `tests/test_notify.py` mocks
`urllib.request.urlopen` so `DiscordNotifier` is fully covered — success,
a failed POST, and "no webhook configured" — without ever making a real
HTTP call. None of the automated tests hit the network or a real repo —
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
    conventions.py   — clones/refreshes the local dms-conventions cache
    state.py         — SQLite-backed run history
    notify.py        — pluggable outcome notifications (Notifier/DiscordNotifier/NullNotifier)
    prompts/align_repo.md.tmpl — the prompt template dispatched to Claude
  tests/             — unit tests (state, cli parsing/templating/notify-severity, mocked dispatch, notify)
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
