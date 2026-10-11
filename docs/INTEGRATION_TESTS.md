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
| Offline contract | `test_offline_contract.py` | git | ~15 s, free | yes |
| Claude Code | `test_live_claude_code.py` | `GARDENER_IT_CLAUDE=1`, an authenticated `claude` | ~30 s, ~$0.15 | no |
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

### Claude Code

One real report dispatch. It must exit 0, print a `GARDENER_SUMMARY:` line,
record a cost, leave the clone unchanged and push nothing.

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

With the offline target, the fixture repo is seeded straight into
gardener's clone cache and `align` runs with `--no-refresh-target`. A fake
`gh` on `PATH` records every call and fails. The only calls allowed are
read-only auth queries, which `claude` itself makes at startup. The
fixture's `origin` is a GitHub URL that does not exist, so nothing can be
pushed.

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
[`docs/verification/`](verification/), next to the release records.
