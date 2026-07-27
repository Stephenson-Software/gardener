# gardener

[![CI](https://github.com/dmccoystephenson/gardener/actions/workflows/ci.yml/badge.svg)](https://github.com/dmccoystephenson/gardener/actions/workflows/ci.yml)

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

> **Note on running this yourself:** `align` clones `dms-conventions`
> (above), and `tend` bootstraps a target repo's dev-loop skill via a
> `create-dev-loop` skill — both are currently private repos of the
> author's own. Cloning gardener and running it against your own repos
> works today for reading the code, the tests, and the design docs linked
> below, but `align`/`tend` won't fully function for you out of the box
> unless you swap in your own equivalent conventions doc and
> skill-bootstrapping mechanism.

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
  [`gardener tend`](docs/USAGE.md#gardener-tend--dispatching-a-target-repos-own-dev-loop)
  in the usage docs.
- **`gardener garden`** + **`gardener overnight`**: an opt-in list of repos
  and the unattended batch dispatcher that tends them one after another
  overnight. See [docs/OVERNIGHT.md](docs/OVERNIGHT.md).
- **`gardener dashboard`**: a local, read-only web UI over `gardener
  status`'s own run history plus whatever `tend`/`overnight` log was
  written to most recently, so an unattended overnight run doesn't require
  polling the CLI by hand to see what it's doing. See
  [docs/DASHBOARD.md](docs/DASHBOARD.md).

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
#    the `.monitor.env` convention used elsewhere in this ecosystem):
mkdir -p ~/.local/state/gardener   # or $GARDENER_STATE_DIR if overridden
umask 077
echo 'DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXX/YYY' \
  > ~/.local/state/gardener/notify.env
```

No webhook configured (neither of the above) means notifications are a
clean no-op — `gardener align` still works exactly the same, nothing
prints or fails because of it. See [docs/ALERTING.md](docs/ALERTING.md)
for how this is implemented.

## Usage

```
gardener align --repo <owner/repo> [--implement] [--file-issue]
gardener tend --repo <owner/repo> [--allow-merge]
gardener allowlist list | add --repo <owner/repo> | remove --repo <owner/repo>
gardener garden list | add --repo <owner/repo> | remove --repo <owner/repo>
gardener overnight [--hours N] [--concurrency N] [--strategy round-robin|issue-count|random]
gardener status [--repo <owner/repo>]
gardener tail-transcript <path> [-f]
gardener dashboard [--port N]
```

See **[docs/USAGE.md](docs/USAGE.md)** for the full command reference:
every flag (`--implement`, `--file-issue`, `--model`, `--timeout`,
`--no-refresh-*`), how `tend` bootstraps and dispatches a target repo's own
dev-loop skill, orphaned-work recovery, concurrent-dispatch safety, the
merge allow-list, live session/transcript visibility, and run logs.

For the unattended "tend to my garden while I sleep" flow (the `garden`
opt-in list, `overnight`'s batching/budget/resume-cursor design, and the
per-device wiring recipes it's actually been deployed with), see
**[docs/OVERNIGHT.md](docs/OVERNIGHT.md)**.

For the dashboard's garden view (the table/plant-plot of every repo's
health), see **[docs/DASHBOARD.md](docs/DASHBOARD.md)**.

## Support

### Experiencing a bug?

Please file a bug report [here](https://github.com/dmccoystephenson/gardener/issues).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to propose a change, branch
naming, and running the test suite. Community participation is governed by
the [Code of Conduct](CODE_OF_CONDUCT.md).

## Testing

    PYTHONPATH=. python3 -m unittest discover -s tests -v

A passing run ends with `OK`. None of the automated tests hit the network,
invoke a real `claude`/`git`/`gh` process, or mutate a real repo. See
**[docs/TESTING.md](docs/TESTING.md)** for exactly what each test module
covers and for the manual/end-to-end verification steps required before
trusting a change to the dispatch layer.

## Safety model

gardener never invokes `claude` with `bypassPermissions` or any equivalent
auto-approve-everything mode, for any mode, under any flag combination —
enforced in `dispatch.py`, which raises rather than silently proceeding if
it's ever reached. See **[docs/SAFETY.md](docs/SAFETY.md)** for the full
three-layer tool-scoping model, how headless `tend` dispatch handles the
"ask the user before merging" problem with nobody there to ask, and the
merge allow-list mechanics.

## Alerting design

See **[docs/ALERTING.md](docs/ALERTING.md)** for the `Notifier`
abstraction, `DiscordNotifier`/`NullNotifier`/`CompositeNotifier`, and how
`_notify_run` maps a run's outcome to a severity.

## Development

No build step — this is a stdlib-only Python CLI (see
[Architecture](docs/ARCHITECTURE.md) below). Clone it, `pip install -e .`, edit,
re-run the tests.

## Project Status

Working end to end, real-verified against live repos in every dispatch
mode (`align` report-only, `tend` with and without `--allow-merge`,
`overnight` including its resume cursor and `--concurrency`, live
transcript visibility, and the `create-dev-loop` bootstrap path). See
**[docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)** for the full history
of what was run, when, and what was confirmed afterward.

## Architecture

Stdlib-only Python — no third-party pip dependencies; gardener shells out
to `git`, `gh`, and `claude` rather than reimplementing git hosting,
GitHub API auth, or an agent loop. See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
for the full module tree and gardener's relationship to
[`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions).
