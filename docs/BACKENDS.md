# Agent backends

gardener chooses the work: which repo, which mode, which prompt, whether
a merge is allowed. A separate **agent** does it. By default that agent is
Claude Code (`claude -p`), exactly as it was before backends existed. The
`command` backend hands the same job to any program you name instead, for
example a local-model runtime such as [Orket](https://github.com/McElyea/Orket)
on hardware that can serve a capable model.

| Backend | Selected by | Dispatches | Safety enforcement |
|---|---|---|---|
| `claude-code` (default) | nothing set, or `GARDENER_AGENT_BACKEND=claude-code` | `claude -p` with the mode's `--tools`/`--permission-mode`/`--allowedTools` | Structural, by Claude Code itself. See [SAFETY.md](SAFETY.md) |
| `command` | `GARDENER_AGENT_BACKEND=command` + `GARDENER_AGENT_COMMAND` | your command, in the target clone | Report mode is checked after the run. Every other mode is refused unless you opt in (see below) |

## Configuration

Each setting is read from the environment first, then from the same
`notify.env` file alerting uses (`~/.local/state/gardener/notify.env`, or
under `$GARDENER_STATE_DIR`), so a cron/Task Scheduler run is configured
the same way.

| Setting | Meaning |
|---|---|
| `GARDENER_AGENT_BACKEND` | `claude-code` (default) or `command`. Any other value is an error, never a silent fall back to Claude |
| `GARDENER_AGENT_COMMAND` | The command to run, split like a shell would split it (`shlex`) but never run through a shell. Required for `command` |
| `GARDENER_AGENT_ALLOW_UNSCOPED` | `1`/`true` lets the `command` backend run modes other than report. See [Safety](#safety) |

```bash
export GARDENER_AGENT_BACKEND=command
export GARDENER_AGENT_COMMAND="python3 /path/to/gardener/examples/backends/ollama_report_agent.py"
export OLLAMA_MODEL=qwen2.5-coder:32b
gardener align --repo owner/repo
```

`gardener doctor` checks for the configured command instead of `claude`,
and reports a misconfigured backend as an error.

## The command contract

For each dispatch gardener runs `GARDENER_AGENT_COMMAND` once and waits
for it to exit (timeouts are the same as Claude Code's per mode).

**Input**

- **Working directory:** the target repo's cached clone.
- **stdin:** the full prompt, the same text Claude Code would get.
- **Environment:** gardener's own environment, plus:

| Variable | Contents |
|---|---|
| `GARDENER_AGENT_MODE` | `report`, `implement`, `file-issue`, `create-dev-loop`, or `tend` |
| `GARDENER_AGENT_SPEC` | JSON: the mode's `tools`, `permission_mode`, `allowed_tools` (Claude Code's pattern syntax, e.g. `Bash(git *)`), `add_dirs`, and `merge_allowed` |
| `GARDENER_AGENT_MODEL` | the `--model` value, when one was passed |
| `GARDENER_AGENT_TIMEOUT` | seconds gardener will wait before killing the command |

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
  "permission_denials": []
}
```

Only `result` is required. `session_id` is stored where Claude Code's
session id would be, so put something there you can find the run by
later (an Orket run id, a ledger path). An `is_error` or failure whose
text says a usage limit was hit, or that GitHub is unreachable, still
stops an `overnight` batch the same way it does for Claude Code
(`dispatch.is_device_global_failure`).

## Safety

Everything in [SAFETY.md](SAFETY.md) describes Claude Code's own tool
scoping, which gardener *enforces* by building the `claude` argv itself.
gardener does not control a `command` backend's process, so it can only
*hand over* the same spec in `GARDENER_AGENT_SPEC`. That changes what is
guaranteed:

- **`bypassPermissions` stays unreachable.** The same runtime check runs
  before either backend dispatches.
- **Report mode runs without opt-in, and is checked after the fact.**
  gardener records the clone's `HEAD` and `git status --porcelain` before
  the run and compares them after. A run that changed either is recorded
  as failed. That catches edits and commits in the clone. It cannot see
  side effects elsewhere, such as an API call to GitHub, so a report-mode
  command must still be one you trust not to make them.
- **Every other mode is refused** unless
  `GARDENER_AGENT_ALLOW_UNSCOPED=1` is set. Setting it is your statement
  that the command enforces `GARDENER_AGENT_SPEC` itself: only the listed
  tools, only the allowed command patterns, no `gh pr merge` unless
  `merge_allowed` is true. gardener cannot verify that. `overnight` checks
  this once before the batch starts and aborts with an alert rather than
  failing every garden repo.

The merge allow-list, the per-repo lock, orphaned-PR recovery, run
history, alerting and the hub work the same on both backends. What does
not carry over: auth-failure retries (they match Claude Code's own error
wording), live transcript discovery (`gardener tail-transcript` reads
Claude Code's transcript files), and `create-dev-loop` bootstrap
producing a Claude Code skill (a `tend` on the `command` backend still
needs that skill to exist, or an agent that can read
`~/local-skills/<slug>-dev-loop/` itself).

## Recipes

### Ollama, report mode (included)

[`examples/backends/ollama_report_agent.py`](../examples/backends/ollama_report_agent.py)
is a minimal, stdlib-only reference implementation of the contract. It
sends the prompt plus a bounded snapshot of the clone (tracked-file list,
the head of `README.md` and `CLAUDE.md`) to a local Ollama model and
returns the answer. It gives the model no tools and refuses every mode
but report. It shows the plumbing working end to end; how useful the
audit is depends entirely on the model, and a model that can only see a
snapshot will miss what it cannot see.

### Orket

Orket governs local-model agents: a model proposes an action, and Orket
decides whether it is allowed, asks for approval where its policy says
to, runs it, and records it in a hash-chained ledger. That makes it the
natural place to enforce `GARDENER_AGENT_SPEC` for the write modes, which
is what `GARDENER_AGENT_ALLOW_UNSCOPED` asks for.

As of Orket 0.8.0 there is no ready-made "run this prompt in this
directory" workload. `orket agent submit` needs an extension catalog, a
registered workload and an `agent_iteration_request.v1` JSON request. So
the Orket recipe is a small wrapper, owned on the Orket side, that:

1. reads the prompt from stdin and `GARDENER_AGENT_SPEC` from the
   environment;
2. maps the spec onto an Orket workload's tool allowlist and approval
   policy (report mode: read-only tools; `merge_allowed: false`: no merge
   tool at all);
3. runs `orket agent submit ... --json` against a model it serves (for
   example `--provider llama_cpp --model <exact served model>`);
4. prints the contract's JSON object, with the Orket run id as
   `session_id`.

The wrapper and workload have been requested upstream in
[McElyea/Orket#1](https://github.com/McElyea/Orket/issues/1). Until one exists, configure the Ollama
recipe above, or your own wrapper, for local-model report runs.
