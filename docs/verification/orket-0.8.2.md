# Verification record: integration suite, and Orket 0.8.2 through gardener

What was run, for real, when the integration suite (`integration/`,
[INTEGRATION_TESTS.md](../INTEGRATION_TESTS.md)) was added, and what it
showed about Orket 0.8.2. That release was published in response to field
reports McElyea/Orket#3, #4 and #5.

- **Code verified:** branch `feature/integration-suite`.
- **Date:** 2026-10-10 to 11 (UTC).
- **Environment:**
  - WSL2 (Linux 5.10); Python 3.11.15 and 3.10.20; git 2.25.1.
  - Claude Code CLI as installed; GeForce GT 1030 (2 GB), with all local
    inference on CPU.
  - Ollama 0.24.0, models `llama3.2:3b` and `qwen2.5:7b`. The server ran
    with `OLLAMA_CONTEXT_LENGTH=16384`; the governed path no longer depends
    on it.
- **Orket:** `v0.8.2` (`555685f9`). The governed report example was
  reinstalled from that tag.
- **Isolation:** every run used the suite's sandbox: its own state and
  cache, ambient `GARDENER_*` settings removed, usage reporting off, no hub.
  The operator's real conventions repo is private (about 95 KB, 14 docs)
  and is not named here.

## The suite itself

| Run | Result |
|---|---|
| Unit suite (`tests/`) | 1,110 OK (2 new) |
| Offline tier, Python 3.11 and 3.10, locally and in CI | 21 OK, about 11 s (13 live tests skipped) |
| Claude Code tier (fixture target and conventions) | OK: exit 0 in 26 s, $0.12, clone unchanged |
| Orket tier, refusals (8) | 8 OK in 19 s, none reaching the model |
| Orket tier, inference (`llama3.2:3b`, 32K) | 2 OK in 380 s: a delivered report is governed and replayable, and a mid-run target edit is refused |
| Command tier, `gardener-orket-extension` + `llama3.2:3b` | 2 OK in 90 s: a delivered report, and its own refusal of `--implement` |
| Command tier, stub agent against the real GitHub target | OK: cloned through `gh`, `pushedAt` compared |

### Bugs the suite found in gardener (fixed on this branch)

1. **A tampered report run failed silently.** A command-harness report run
   that changed the clone was failed. But the reason only reached
   `DispatchResult.stderr`, which `align` never prints, and the run
   recorded the agent's own success summary. It is now `failure_reason`,
   printed and recorded.
2. **A timeout left the agent running.** A command-harness timeout killed
   only the direct child. The offline tier found three stand-in agents
   still asleep minutes after their runs had "timed out". The harness now
   runs in its own session and the group is killed. A unit test and the
   offline timeout test both fail with the fix reverted. The Claude Code
   path has the same pattern; it is filed as gardener#187, since it needs
   its own real verification.

## Orket 0.8.2 against the field reports

| Report | 0.8.1 (before) | 0.8.2, observed here |
|---|---|---|
| #3: Ollama prompts cut to the server default | every call charged exactly 4,096 tokens; output garbage | Fixed on the governed path. A run requesting 30,000 + 2,048 tokens loaded the model at **32,048** context, though the server default was 16,384. An over-budget prompt is refused before inference with `E_MODEL_INPUT_BUDGET … input was not truncated`. **Not fixed on the host's generic `llm/generate` API**: a message over the server context is dropped entirely (19 input tokens reach the model) and the host returns `ok: true`. Filed as Orket#6. |
| #4: an unreachable provider misreported | `E_LOGGING_PREPARATION_REQUIRED` | Fixed: `E_AGENT_LOCAL_INVENTORY_UNAVAILABLE:ollama:ConnectError` with `blocked: true` in 4 s; an unknown model is also `blocked` |
| #5: an 8,192-token profile against a 64 KiB budget, and a 512-token output cap | over budget, refused | Fixed for budgets: input and output limits are configurable and refused with typed errors (`E_MODEL_INPUT_BUDGET`, `E_MODEL_OUTPUT_LIMIT`), and output ceilings are 2,048. **But inputs over about 64 KiB fail with an opaque `E_AGENT_CHILD_DISCONNECTED`** before inference, whatever the raised byte limits (bisected to 64,710 OK vs 66,705 failing). The real 95 KB conventions still cannot run. Filed as Orket#7. |

The input-budget estimate is conservative. The compact fixture was
estimated at 26,919 "tokens" against a measured 7,126 to 7,557, so it was
refused under the default 8,192 profile. That is about 3.8× too high, but
it errs toward refusing, and the README says so.

## Real-target comparison matrix

`python -m integration.matrix` against the public
`Stephenson-Software/multiplyTwoNumbers` (one LC-3 assembly file, a README
and a LICENSE). Every row left the clone clean (HEAD equal to upstream,
empty `git status`). The target's `pushedAt` stayed `2026-09-04T02:27:40Z`
throughout.

| Row | Conventions | Exit | Time | Outcome |
|---|---|---|---|---|
| Claude Code (default model) | real (private) | 0 | 70 s | 47 gaps across 8 sections, $0.47 |
| Orket, 3B, timeout 60 s | fixture | 1 | 43 s | `E_AGENT_MODEL_PROVIDER_TIMEOUT`, `blocked: true`, one model call, no retry. The retained run is left `recovery_pending` with no final truth and no run ID in the envelope (Orket#8) |
| Orket, 3B, 32K (run a) | fixture | 0 | 438 s | "delivered", but the report is only its title line (Orket#9) |
| Orket, 3B, 32K (run b) | fixture | 0 | 254 s | byte-identical to run a (temperature 0, fixed seed) |
| Orket, 7B, 32K | fixture | 0 | 638 s | a full checklist with a valid `GARDENER_SUMMARY:` line; 8 of 9 items correct (it claims a CI workflow that does not exist) |
| Extension, 3B, 8 KB snapshot | real (private) | 1 | 178 s | refused by the extension's citation check: `E_REPORT_UNSOURCED` |
| Extension, 7B, 8 KB snapshot | real (private) | 1 | 303 s | host per-call timeout surfaced as HTTP 500 (comment on Orket#6) |
| Orket, 3B, 128K, raised byte limits | real (private) | 1 | 4 s | `E_AGENT_CHILD_DISCONNECTED` (Orket#7) |

Earlier in the same session, before the suite existed, the same harness
delivered with `qwen2.5:7b` at 32K on a compact copy of the real
conventions: 7,557 input and 759 output tokens, replay `matched`. The
report was well formed but marked every item N.

## Reading the results

- **Safety held everywhere.** Across every tier and row, no run changed
  the clone or pushed. Every refusal Orket documents happened before the
  model was called, and every failure was reported as a failure.
- **Delivery is now possible on this hardware.** In 0.8.1 nothing delivered.
  In 0.8.2, with raised budgets on a fixture-sized rubric, 3B and 7B both
  deliver, replay and stay governed. Only 7B produced a useful report.
- **The remaining gaps are on Orket's side and filed:** #6 (generic runtime
  API context and timeouts), #7 (64 KiB child limit), #8 (timeout
  bookkeeping), #9 (delivery bar).

## Reproducing

```bash
# Offline tier (no network, no model):
PYTHONPATH=. python3 -m unittest discover -s integration -t . -v
# Orket tier: see docs/INTEGRATION_TESTS.md for GARDENER_IT_ORKET_*.
GARDENER_IT_ORKET_INFERENCE=1 PYTHONPATH=. python3 -m unittest integration.test_live_orket -v
# Matrix:
PYTHONPATH=. python3 -m integration.matrix my.rows --repo owner/name --out /tmp/matrix
```
