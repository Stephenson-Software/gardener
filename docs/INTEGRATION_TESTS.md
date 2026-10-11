# Integration tests

`tests/` is the unit suite. It mocks every process boundary and never runs
a real `claude`, `gh` or model. `integration/` is the other half: every test
there runs the real `gardener align` CLI as a subprocess, against real Git
repositories and, in the live tiers, a real agent. It automates the manual
end-to-end checks in [TESTING.md](TESTING.md#manualend-to-end-verification)
and makes them repeatable for any harness.

```bash
PYTHONPATH=. python3 -m unittest discover -s integration -t . -v
```

With nothing configured, only the offline tier runs. Each live tier skips
itself, with the reason, until its settings are present.

## Tiers

| Tier | File | Needs | Cost | Runs in CI |
|---|---|---|---|---|
| Offline contract | `test_offline_contract.py` | git | ~10 s, free | yes |
| Offline lifecycle | `test_offline_lifecycle.py` | git | ~10 s, free | yes |
| Offline observability | `test_offline_observability.py` | git | ~7 s, free | yes |
| Offline self-update | `test_offline_selfupdate.py` | git | ~4 s, free | yes |
| Claude Code | `test_live_claude_code.py` | `GARDENER_IT_CLAUDE=1`, an authenticated `claude` | ~30 s, ~$0.15 | no |
| Write modes and `tend` | `test_live_write_modes.py` | as Claude Code, plus `GARDENER_IT_WRITE_REPO`, a throwaway GitHub repo | ~3 min, ~$0.85 | no |
| Any command harness | `test_live_command.py` | `GARDENER_IT_COMMAND` | depends on the harness | no |
| Orket | `test_live_orket.py` | Orket checkout, catalog, model (below) | refusals ~20 s; inference minutes per test on CPU | no |

### Offline contract

Stand-in agents (`stub_agent.py`) replace the model, so the tier is
deterministic and needs no network. Every assertion still crosses the real
process boundary:

- **The command contract:** prompt on stdin; mode, spec, model and timeout
  in the environment; cwd is the target clone. Plain-text and JSON answers,
  error and `blocked` envelopes, a non-zero exit, and a timeout each map to
  the right exit code and recorded run.
- **Report-mode tamper check:** an agent that adds a file, edits a tracked
  file or commits, and then claims success, still fails the run, and the
  run says why.
- **Write-mode gate:** `--implement`/`--file-issue` are refused before
  dispatch without `GARDENER_HARNESS_ALLOW_UNSCOPED`. The opt-in is
  per-mode.
- **Conventions subdirectory:** it reaches the harness as its only
  `add_dirs` entry and in the prompt. The flag beats the setting. A missing
  or escaping path is refused before dispatch.
- **Claude Code argv:** a fake `claude` records the real invocation:
  `--permission-mode plan` and `--tools Read,Grep,Glob` in report mode, and
  no `bypassPermissions` or `--dangerously-skip-permissions` in any mode.
- **Timeouts kill the whole tree:** on both harnesses, a child the agent
  started dies with it when gardener's timeout fires.
- **Signals reach the agent:** a SIGTERM or Ctrl-C to `gardener align`
  kills the running agent too, though it runs in its own session.

### Offline lifecycle

The rest of a dispatch's life, still with stand-in agents and no network:

- **Sessions:** `gardener ps` lists a running `align`, and `gardener stop`
  and `gardener kill` end it and its agent.
- **`tend`:**
  - it is refused without the unscoped opt-in;
  - `merge_allowed` is set only with `--allow-merge` and the repo on the
    allow-list;
  - a missing dev-loop skill is bootstrapped inside the sandbox's HOME,
    never the operator's.
- **`overnight`:**
  - with `--concurrency 2`, every garden repo is tended, and no log line is
    glued to another;
  - an unready harness aborts before any dispatch;
  - Ctrl-C mid-batch kills every agent;
  - a `random` run that starts with one repo left in its cycle tends it,
    then carries on into the next cycle, and the cursor records the new
    cycle.
- **Cloning and refreshing:** a second `align` refreshes the clone the
  first one made.

These use `Sandbox(local_github=True)`: the fake `gh` serves `repo view`,
`repo clone`, `pr list` and `issue list` from local bare repositories, so
the real clone-and-refresh path runs offline. Every test asserts that no
other `gh` call was made.

### Offline observability

What happens around a run. Every service runs on 127.0.0.1, and nothing
leaves the machine:

- **Discord alerts:** a successful run sends one alert to a stub webhook,
  naming the repo and the device, with a non-default User-Agent. A failed
  run's alert is a different colour. With no webhook, nothing is sent and
  nothing is printed.
- **Usage reporting:** one `startup` event carrying the version reaches a
  stub endpoint; the opt-out sends nothing.
- **`gardener status`:** lists both a successful and a failed run.
- **The dashboard:** `/api/status` serves the recorded run, and `/` and
  `/live` load. It is not reachable on the machine's network address.
- **The hub,** a real `gardener hub serve`:
  - a device's run arrives under its token's device name;
  - `status --all-devices` reads it back;
  - an unknown token is refused;
  - a run made while the hub is down is kept locally, delivered by
    `gardener hub sync` once it is up, and not duplicated by a second sync.
- **`gardener doctor --offline`:** clean on a fresh install. It flags an
  untracked file in a cache clone without removing it.

`support.StubServer` is the recording loopback server these use.

### Offline self-update

Each test builds a copy of this checkout's `gardener/` package as an older
commit that tracks a local bare origin, then pushes a newer commit to that
origin. The CLI runs from the copy, the way an editable install would, so
`selfupdate` finds it through its own module file.

- **`gardener update --check`** reports the update without applying it.
- **`gardener update`** fast-forwards the copy. A new process then imports
  the new version, and a second `update` reports it up to date. Untracked
  files don't block it.
- **Skips:** tracked local changes, a diverged branch, a detached HEAD, and
  a branch with no upstream all leave HEAD where it was, and exit 0.
- **An unreachable origin** is an error, exit 1.
- **`overnight`:**
  - the update is logged, sends no alert, and the run carries on;
  - a skip sends one "self-update SKIPPED" alert, and the run still carries
    on;
  - `--no-self-update` leaves the copy alone.

The origin is a local bare repository and the webhook a loopback stub.

### Claude Code

One real report dispatch. It must exit 0, print a `GARDENER_SUMMARY:` line,
record a cost, leave the clone unchanged and push nothing.

### Write modes

`align --implement` and `align --file-issue`, for real, against the GitHub
repository named in `GARDENER_IT_WRITE_REPO`. **Point it at a throwaway
repository:** each run opens a real pull request or issue there. They are
closed afterwards (a pull request with its branch) unless
`GARDENER_IT_KEEP=1`.

- `--implement` opens exactly one pull request from a new branch, with at
  least one changed file. It does not merge it, move the default branch or
  open an issue. Since 0.4.6 its tool scoping blocks `gh pr merge`; the
  outcome check stays as a second line of defence.
- `--file-issue` opens exactly one issue with a body. It opens no pull
  request, does not move the default branch, and leaves the clone
  unchanged.
- Neither puts the conventions repo's URL or name in a title, body, branch
  name or commit message.
- `tend` (no `--allow-merge`) runs the repository's dev-loop skill. The
  test installs a small, deterministic one: add a dated line to
  `CHANGELOG.md` on a new branch and open a pull request. It goes in the
  real `~/.claude/commands/<slug>-dev-loop.md`, because `claude` reads
  skills from the HOME that holds its login. The test refuses to overwrite
  an existing skill of that name and removes its own afterwards. The run
  must report `merge_eligible=False`, open at most one pull request, merge
  nothing, open no issue, and leave the default branch alone.

#### Weekly on the box

`bin/run-live-tests.sh` runs the Claude Code tier, and, when
`GARDENER_LIVE_WRITE_REPO` names a throwaway repository, this tier too. It
then sends one alert through gardener's notifier: INFO when everything
passed, ERROR with the failing tests otherwise. A Windows Task Scheduler
task runs it weekly, the same way `bin/run-overnight.sh` runs nightly
(see [OVERNIGHT.md](OVERNIGHT.md#wsl2--windows-task-scheduler)):

```
wsl.exe -d <distro> -u <user> -- bash -lc "GARDENER_LIVE_WRITE_REPO=<owner/throwaway> /path/to/gardener/bin/run-live-tests.sh"
```

Logs go to `~/.local/state/gardener/logs/live-tests/`, outside the
directory the dashboard reads, and the last 12 are kept. A run costs about
$1.

### Any command harness

`GARDENER_IT_COMMAND` is the harness command, exactly as it would appear in
`GARDENER_HARNESS_COMMAND`. The test checks the invariants every harness
must keep:
- a report run never changes the clone or pushes;
- an envelope, if one is printed, has `result` and `is_error`;
- with gardener's write-mode gate opened, a report-only harness still
  refuses `--implement` (offline target only).

`GARDENER_IT_EXPECT=any` accepts a clean, well-formed failure in place of a
delivered report, for small local models that may not produce one.

### Orket

For [Orket's governed harness](HARNESSES.md#orket). It needs:

| Setting | Meaning |
|---|---|
| `GARDENER_IT_ORKET_SRC` | Orket source checkout (for `examples/gardener_harness/`) |
| `GARDENER_IT_ORKET_PYTHON` | Python with that Orket installed |
| `GARDENER_IT_ORKET_CATALOG` | `extensions_catalog.json` with the governed report example installed |
| `GARDENER_IT_ORKET_MODEL` | the exact served model |
| `GARDENER_IT_ORKET_PROVIDER` | default `ollama` |
| `GARDENER_IT_ORKET_BASE_URL` | default `http://127.0.0.1:11434` |
| `GARDENER_IT_ORKET_DELIVER_ENV` | JSON object of extra settings for runs that reach the model, e.g. `{"ORKET_GARDENER_MAX_INPUT_TOKENS": "30000", "ORKET_LOCAL_PROMPT_PROFILE_REGISTRY_PATH": "/path/profiles.json"}` |
| `GARDENER_IT_ORKET_LARGE_ENV` | JSON object of settings for the over-64-KiB input test (Orket 0.8.4+): a profile registry and input budget large enough for about 100 KB, e.g. `{"ORKET_LOCAL_PROMPT_PROFILE_REGISTRY_PATH": "/path/profiles-128k.json", "ORKET_GARDENER_MAX_INPUT_TOKENS": "129024", "ORKET_GARDENER_MAX_SNAPSHOT_BYTES": "131072"}`. Unset skips that test |
| `GARDENER_IT_ORKET_INFERENCE=1` | also run the tests that call the model |

**Refusals.** These must happen before any model call:
- write modes, refused by Orket itself with no run area created;
- a dirty target;
- a symlinked selected file, refused without leaking its target;
- a file over the per-file limit;
- an unreachable provider, or an unknown model: `blocked: true`;
- an input budget too small: `E_MODEL_INPUT_BUDGET`;
- an output request above the host ceiling: `E_MODEL_OUTPUT_LIMIT`.

**Inference.** A delivered report must meet all of these:
- exit 0, with the clone unchanged;
- exactly one model call;
- `completion_satisfied` in `orket agent inspect`;
- `matched` in `orket agent replay`;
- on Ollama, the loaded context equals the requested input plus output
  budget.

A target edited while the model runs must be refused. Orket verifies
delivery and source references, not findings, so report quality is not
asserted.

**Orket 0.8.4+.** Read from the checkout's `pyproject.toml`; older
checkouts skip these:
- a delivered report's summary starts with a real `GARDENER_SUMMARY:` line,
  not a title or a template (Orket#9);
- a timed-out run comes back with its run ID, and is closed as
  `failed_terminal` with an ended, failed attempt and
  `completion_unsatisfied` (Orket#8);
- an input over 64 KiB reaches the model instead of failing with
  `E_AGENT_CHILD_DISCONNECTED` (Orket#7). This needs
  `GARDENER_IT_ORKET_LARGE_ENV`.

**Orket 0.8.5+:** a delivery refused because the target changed during the
run still names the retained run (`session_id`) and gives the exact `orket
agent inspect` command, and that inspection works.

## Shared settings

| Setting | Meaning |
|---|---|
| `GARDENER_IT_REPO` | a real `owner/name` target, cloned through `gh`; its `pushedAt` is compared before and after. Default: the offline fixture |
| `GARDENER_IT_CONVENTIONS_URL` | a real conventions repo. Default: `integration/fixtures/conventions` |
| `GARDENER_IT_PASSTHROUGH` | comma-separated `GARDENER_*` names (a trailing `*` allowed) the harness itself reads, e.g. `GARDENER_ORKET_*` |
| `GARDENER_IT_KEEP=1` | keep each test's sandbox under `/tmp/gardener-it-*` for inspection |

## Isolation

Every test runs in its own sandbox (`support.Sandbox`):
- its own `GARDENER_STATE_DIR` and `GARDENER_CACHE_DIR`;
- every ambient `GARDENER_*` setting removed, plus `DISCORD_WEBHOOK_URL`;
- usage reporting off, and no hub.

A test can't write to your run history, alert your Discord, push to your
hub, or pick up your own harness configuration.

With the offline target, a fake `gh` (`fake_gh.py`) on `PATH` records
every call:
- **By default**, the fixture repo is seeded straight into gardener's clone
  cache, `align` runs with `--no-refresh-target`, and every `gh` call fails.
- **With `local_github=True`**, the fake `gh` serves read-only calls from
  local bare repositories instead.

Either way, a call outside the read-only set is a test failure. The
fixture's `origin` is never a real GitHub repository, so nothing can be
pushed. With `fake_home=True`, `HOME` points into the sandbox too, so
nothing reads or writes the operator's `~/.claude` or `~/local-skills`.

The harness command is wrapped in `recorder.py`, which passes everything
through unchanged. It also saves what the harness received and printed, so
tests can assert on fields gardener itself does not keep, such as
`blocked`.

## Comparison matrix

The tests answer "does this still hold?". `integration/matrix.py` answers
"how do these compare?": the same `align` across harnesses, models and
settings, tabulated.

```bash
PYTHONPATH=. python3 -m integration.matrix integration/matrix.example.rows --out /tmp/matrix
```

See [`matrix.example.rows`](../integration/matrix.example.rows) for the
format. Each row keeps its sandbox under `--out`, next to `summary.tsv` and
`summary.md` (name, exit, seconds, clean, pushed, blocked, result). It exits
non-zero if any row changed the clone or pushed.

## Recorded runs

Results from real runs of this suite are in
[`docs/verification/`](verification/), next to the release records. The
first is [orket-0.8.2](verification/orket-0.8.2.md).
