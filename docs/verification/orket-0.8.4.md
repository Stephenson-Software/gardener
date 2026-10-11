# Verification record: Orket 0.8.4 through gardener

Orket 0.8.3 and 0.8.4 resolved McElyea/Orket#6–#9, which were filed from
[orket-0.8.2.md](orket-0.8.2.md). This record is the recheck through
gardener 0.4.3 and its integration suite, with the Orket tier's new 0.8.4
checks.

- **Orket:** `v0.8.4` (`048e7468`). The governed report example was
  reinstalled from that tag.
- **gardener:** 0.4.3 plus branch `feature/orket-084-checks`, the new
  Orket-tier checks.
- **Date:** 2026-10-11.
- **Environment:** WSL2, CPU-only GT 1030 box, Ollama 0.24.0 back on its
  default settings (GPU allowed, server context 4,096). Orket sets each
  request's context itself.
- **Profiles:** an operator copy of Orket's prompt profiles, with 32,768
  tokens for the delivery runs and 131,072 for the large-input check.

## The four issues

| Issue | Check | Result |
|---|---|---|
| #6 generic `llm/generate` | the "secret word" probe through a 0.8.4 host | 664 characters: `PELICAN`, correct. 16.5 KB and 83 KB: HTTP 400 `E_MODEL_INPUT_BUDGET` before generation, where 0.8.2 dropped the message and returned `ok: true` |
| #7 inputs over 64 KiB | new `test_input_over_64_kib_reaches_the_model` | a 113,014-byte input reached the model, then hit the 60 s budget with a typed `E_AGENT_MODEL_PROVIDER_TIMEOUT`. 0.8.2 failed it in seconds with `E_AGENT_CHILD_DISCONNECTED` |
| #8 timed-out run left open | new `test_a_timed_out_run_is_closed_and_named` | the envelope carries `session_id: orket:agent-run:…` and the inspect command; the run is `failed_terminal`, the attempt `attempt_failed` with an end time, final truth `completion_unsatisfied` |
| #9 title-only report accepted | the delivery test, with `llama3.2:3b` and `qwen2.5:7b` | 3B was **refused** (`E_GARDENER_REPORT_NOT_ACCEPTED`): its summary copied the prompt's `<N> gap(s)` template and did not start with `GARDENER_SUMMARY:`. 7B **delivered** a summary line and a full checklist |

The eight Orket refusal tests all still pass on 0.8.4 (21 s), none
reaching the model.

## Deliveries

| Model | Result | Time |
|---|---|---|
| `qwen2.5:7b`, 32K | delivered, `completion_satisfied`, replay `matched`. 7 of 9 checklist items correct: it said the README does not document running the tool (it does), and its summary counts 4 gaps against 7 listed N's | 460 s (1,052 s on 0.8.2) |
| `llama3.2:3b`, 32K | refused, correctly (#9) | 277 s |

## The headless extension against a 0.8.4 host

| Host profile | Result |
|---|---|
| packaged 8,192 tokens | every snapshot size (3, 8 and 24 KB) refused with HTTP 400 `E_MODEL_INPUT_BUDGET` before generation. gardener's prompt plus the output budget exceeds the profile |
| 32,768 tokens | reached the model (77 s and 143 s). `llama3.2:3b` cited no source, so the extension's citation check refused both |

The extension now shows Orket's typed reason instead of a bare "HTTP 400",
and its README explains the host profile (gardener-orket-extension#2).

## Reading the results

- **All four issues are fixed as described.** Orket now refuses an empty or
  template report, carries large inputs, closes timed-out runs with their
  identity, and refuses oversized generic-API input with a typed error
  instead of dropping it.
- **Safety still held everywhere.** No run changed a clone or pushed, and
  every refusal named its cause.
- **The model remains the limit on this hardware.** 7B delivers a
  mostly-correct report in about 8 minutes. 3B no longer gets an empty report
  through, but cannot produce one that passes.
