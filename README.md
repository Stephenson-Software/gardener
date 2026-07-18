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
#    Stephenson-Software/a-private-repo's `.monitor.env`):
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

### Other flags

- `--model <name>` — override the model `claude` uses.
- `--timeout <seconds>` — how long to wait for the dispatched run (default
  1800s / 30 min — reading and analyzing a real repo against ~10
  convention docs is not fast).
- `--no-refresh-conventions` / `--no-refresh-target` — reuse whatever is
  already cached instead of fetching latest first.

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
[`~/a-private-repo-2/dashboard/`](https://github.com/dmccoystephenson/a-private-repo-2)'s
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

### Why synchronous dispatch, not `--bg`

a-private-repo-2's dashboard uses `claude --bg` because it's answering an HTTP
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
  `Stephenson-Software/a-private-repo`'s `monitoring/notify-discord.sh` exactly:
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

## Architecture

Stdlib-only Python — no third-party pip dependencies. This matches the
established house style in this ecosystem (see e.g.
`Stephenson-Software/a-private-repo`'s `services/a-private-repo-dashboard/app.py`: "Plain
stdlib Python... no build step / third-party deps"). gardener shells out
to three external CLIs (`git`, `gh`, `claude`) rather than reimplementing
git hosting, GitHub API auth, or an agent loop — that's a process
dependency, not a pip one, and is the same shape a-private-repo-2's tools use.

```
gardener/
  gardener/
    cli.py          — argparse CLI (align, status), prompt building, orchestration
    dispatch.py      — the safety-gated subprocess wrapper around `claude -p`
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
