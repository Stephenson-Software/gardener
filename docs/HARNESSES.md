# Agent harnesses

gardener chooses the work: which repo, which mode, which prompt, whether
a merge is allowed. A separate **agent** does it. By default that agent is
Claude Code (`claude -p`), exactly as it was before harnesses existed. The
`command` harness hands the same job to any program you name instead, for
example a local-model runtime such as [Orket](https://github.com/McElyea/Orket)
on hardware that can serve a capable model.

| Harness | Selected by | Dispatches | Safety enforcement |
|---|---|---|---|
| `claude-code` (default) | nothing set, or `GARDENER_HARNESS=claude-code` | `claude -p` with the mode's `--tools`/`--permission-mode`/`--allowedTools` | Structural, by Claude Code itself. See [SAFETY.md](SAFETY.md) |
| `command` | `GARDENER_HARNESS=command` + `GARDENER_HARNESS_COMMAND` | your command, in the target clone | Report mode is checked after the run. Every other mode is refused unless you opt in (see below) |

## Configuration

Each setting is read from the environment first, then from the same
`notify.env` file alerting uses (`~/.local/state/gardener/notify.env`, or
under `$GARDENER_STATE_DIR`), so a cron/Task Scheduler run is configured
the same way.

| Setting | Meaning |
|---|---|
| `GARDENER_HARNESS` | `claude-code` (default) or `command`. Any other value is an error, never a silent fall back to Claude |
| `GARDENER_HARNESS_COMMAND` | The command to run, split like a shell would split it (`shlex`) but never run through a shell. Required for `command` |
| `GARDENER_HARNESS_ALLOW_UNSCOPED` | Modes the `command` harness may run beyond report: a comma-separated list (`implement,file-issue`), or `1`/`true`/`all` for every mode. Blank or `0` means none. An unknown mode name is an error. See [Safety](#safety) |
| `GARDENER_HARNESS_MODEL` | Default model for every dispatch on either harness, when `--model` isn't passed. Passed to `claude --model`, or to the command under the same name |
| `GARDENER_CLAUDE_BIN` | Path or name of the Claude Code executable (default `claude` on `PATH`) |
| `GARDENER_ALIGN_TIMEOUT` | Seconds an `align` run may take, any mode (default 1800). `--timeout` wins |
| `GARDENER_TEND_TIMEOUT` | Seconds a `tend` run may take (default 2700). `--timeout` on `tend` or `overnight` wins. `overnight` also uses it as its per-repo budget headroom |
| `GARDENER_CREATE_DEV_LOOP_TIMEOUT` | Seconds the create-dev-loop step before a first `tend` may take (default 900) |

The model and timeout settings apply to Claude Code too. A slower local
model usually needs longer timeouts than the defaults, which were sized
for Claude Code. A timeout that isn't a positive whole number is an error.

```bash
export GARDENER_HARNESS=command
export GARDENER_HARNESS_COMMAND="python3 /path/to/gardener/examples/harnesses/ollama_report_agent.py"
export OLLAMA_MODEL=qwen2.5-coder:32b
gardener align --repo owner/repo
```

`gardener doctor` checks for the configured command (or
`GARDENER_CLAUDE_BIN`) instead of `claude`, and reports a misconfigured
harness as an error.

## The command contract

For each dispatch gardener runs `GARDENER_HARNESS_COMMAND` once and waits
for it to exit, killing it after the mode's timeout (see the settings
above).

**Input**

- **Working directory:** the target repo's cached clone.
- **stdin:** the full prompt, the same text Claude Code would get.
- **Environment:** gardener's own environment, plus:

| Variable | Contents |
|---|---|
| `GARDENER_HARNESS_MODE` | `report`, `implement`, `file-issue`, `create-dev-loop`, or `tend` |
| `GARDENER_HARNESS_SPEC` | JSON: the mode's `tools`, `permission_mode`, `allowed_tools` (Claude Code's pattern syntax, e.g. `Bash(git *)`), `add_dirs`, and `merge_allowed` |
| `GARDENER_HARNESS_MODEL` | `--model`, else the `GARDENER_HARNESS_MODEL` setting; unset when neither is given |
| `GARDENER_HARNESS_TIMEOUT` | seconds gardener will wait before killing the command |

`merge_allowed` is `true` only when both `--allow-merge` was passed and
the repo is on the merge allow-list: the same rule that decides whether
`Bash(gh pr merge *)` reaches Claude Code.

**Output**

- **Exit code:** `0` means the run completed. Anything else is a failed
  run.
- **stdout:** either plain text, which becomes the run's result, or a JSON
  object shaped like `claude --output-format json`:

```json
{
  "result": "the answer gardener records and summarizes",
  "is_error": false,
  "session_id": "anything that identifies the run on your side",
  "total_cost_usd": 0,
  "permission_denials": [],
  "blocked": false
}
```

Only `result` is required. `session_id` is stored where Claude Code's
session id would be, so put something there you can find the run by
later (an Orket run id, a ledger path).

Some failures say nothing about the repo and will hit every repo in an
`overnight` batch the same way: a model server that's down, a missing
model, an exhausted quota. Set `"blocked": true` on such a failure and the
batch stops there without advancing its resume cursor, so the repos it
didn't reach are retried next run. A failed run whose text matches
gardener's own usage-limit or GitHub-unreachable wording
(`dispatch.is_device_global_failure`) is treated the same way. `blocked`
is ignored on a successful run.

## Safety

Everything in [SAFETY.md](SAFETY.md) describes Claude Code's own tool
scoping, which gardener *enforces* by building the `claude` argv itself.
gardener does not control a `command` harness's process, so it can only
*hand over* the same spec in `GARDENER_HARNESS_SPEC`. That changes what is
guaranteed:

- **`bypassPermissions` stays unreachable.** The same runtime check runs
  before either harness dispatches.
- **Report mode runs without opt-in, and is checked after the fact.**
  gardener records the clone's `HEAD` and `git status --porcelain` before
  the run and compares them after. A run that changed either is recorded
  as failed. That catches edits and commits in the clone. It cannot see
  side effects elsewhere, such as an API call to GitHub, so a report-mode
  command must still be one you trust not to make them.
- **Every other mode is refused** unless it is listed in
  `GARDENER_HARNESS_ALLOW_UNSCOPED` (or that is set to `1` for all of
  them). Listing a mode is your statement that the command enforces
  `GARDENER_HARNESS_SPEC` for it: only the listed tools, only the allowed
  command patterns, no `gh pr merge` unless `merge_allowed` is true.
  gardener cannot verify that. Opting in per mode lets you, say, allow
  `file-issue` without allowing `tend`. `overnight` runs `tend`, plus
  `create-dev-loop` for a repo with no dev-loop skill yet; it checks `tend`
  once before the batch starts and aborts with an alert rather than
  failing every garden repo.

The merge allow-list, the per-repo lock, orphaned-PR recovery, run
history, alerting and the hub work the same on both harnesses. What does
not carry over: auth-failure retries (they match Claude Code's own error
wording), live transcript discovery (`gardener tail-transcript` reads
Claude Code's transcript files), and `create-dev-loop` bootstrap
producing a Claude Code skill (a `tend` on the `command` harness still
needs that skill to exist, or an agent that can read
`~/local-skills/<slug>-dev-loop/` itself).

## Recipes

### Ollama, report mode (included)

[`examples/harnesses/ollama_report_agent.py`](../examples/harnesses/ollama_report_agent.py)
is a minimal, stdlib-only reference implementation of the contract. It
sends the prompt plus a bounded snapshot of the clone (tracked-file list,
the head of `README.md` and `CLAUDE.md`) to a local Ollama model and
returns the answer. It gives the model no tools and refuses every mode
but report. It is configured entirely through the environment:
`OLLAMA_MODEL` (or `GARDENER_HARNESS_MODEL`), `OLLAMA_HOST`,
`OLLAMA_NUM_CTX` (Ollama's default context window is small enough to cut
off gardener's prompt, so set this), and `OLLAMA_AGENT_MAX_FILES`,
`OLLAMA_AGENT_MAX_DOC_BYTES` and `OLLAMA_AGENT_DOCS` for the size and
contents of the snapshot. An unreachable server is reported as
`"blocked": true`. It shows the plumbing working end to end; how useful the
audit is depends entirely on the model, and a model that can only see a
snapshot will miss what it cannot see.

### Orket

Orket governs local-model agents: a model proposes an action, and Orket
decides whether it is allowed, asks for approval where its policy says
to, runs it, and records it in a hash-chained ledger. That makes it the
natural place to enforce `GARDENER_HARNESS_SPEC` for the write modes, which
is what `GARDENER_HARNESS_ALLOW_UNSCOPED` asks for.

Two Orket recipes exist. Both serve report mode only and refuse every other
mode.

#### Orket's governed harness (Orket 0.8.1+)

Orket ships
[`examples/gardener_harness/`](https://github.com/McElyea/Orket/tree/main/examples/gardener_harness),
adapted from
[McElyea/Orket#2](https://github.com/McElyea/Orket/pull/2). Each dispatch
becomes one governed `orket agent run`:
- Orket's `structured_report.v1` verifier checks report delivery and source
  references;
- the run's evidence is retained for `orket agent inspect` and `replay`.

Install Orket's `governed-report` example extension into an Orket project
first (see that example's README), then:

```bash
GARDENER_HARNESS=command
GARDENER_HARNESS_COMMAND="/path/to/orket-venv/bin/python /path/to/Orket/examples/gardener_harness/gardener_harness.py"
GARDENER_HARNESS_MODEL=<exact model your provider serves>
ORKET_GARDENER_CATALOG=/path/to/project/.orket/durable/config/extensions_catalog.json
ORKET_GARDENER_PROVIDER_BASE_URL=http://127.0.0.1:8080/v1   # llama.cpp, the default provider
```

It reads only committed Git content. **Both the target clone and the
conventions repo must be clean.** It also includes every tracked file of each
extra directory, within a 64 KiB document budget, so a large conventions
repo is refused before the model runs. Point `align` at a compact variant
of the rubric with `GARDENER_CONVENTIONS_SUBDIR` (see the README's "Compact
conventions"). Its own README has the full limits.

#### `gardener-orket-extension` (headless)

[Stephenson-Software/gardener-orket-extension](https://github.com/Stephenson-Software/gardener-orket-extension),
built from Orket's extension template, is a lighter alternative:
- `orket-extension gardener` sends one request to a running Orket host's
  generic runtime API (`python server.py`);
- it returns the report only if every citation in it refers to a supplied
  source.

It needs no extension installation and no clean repositories, and keeps no
durable run record beyond gardener's own. Configure it with
`GARDENER_HARNESS_COMMAND="/path/to/venv/bin/orket-extension gardener"` and
the host's `EXTENSION_HOST_API_KEY`; its README lists the rest.

#### What was verified

On one machine (Ollama `llama3.2:3b`, 2 GB GPU), with gardener 0.3.0:

- **Extension:** delivered a cited report once, in 105 s. It refused another
  run as unsourced, and a third timed out in the host's model call.
- **Orket's harness:** refused gardener's real conventions repo as over
  budget. With a minimal conventions repo, it refused the report because
  Ollama's default 4096-token context truncated the prompt. Its llama.cpp
  default was not tested.

So both paths are wired end to end and fail safely. Neither has yet produced
a useful audit on that hardware. A larger model on a host with a larger
context is the next step. Details are in the extension's
`docs/verification.md`.

**Write modes are not served** by either recipe. Enforcing
`GARDENER_HARNESS_SPEC` for them would need an effect-capable Orket workload
and approval flow, which hasn't been built.
