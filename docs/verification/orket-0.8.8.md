# Verification record: Orket 0.8.5–0.8.8 through gardener

Orket 0.8.5–0.8.8 were released on 2026-10-10 in answer to the field report
behind [orket-0.8.4](orket-0.8.4.md):

| Release | Change |
|---|---|
| 0.8.5 | a delivery refused after the run keeps its run ID and inspect command; llama.cpp refusals say the input was not truncated |
| 0.8.6 | an input-budget refusal names the host setting to change |
| 0.8.7 | a gardener installation and upgrade guide (`examples/gardener_harness/README.md`) |
| 0.8.8 | documentation audit only |

This record is the recheck through gardener 0.4.4 and its integration suite.

- **Orket:** `v0.8.8` (`631444b5`), an editable install of that checkout.
  The governed report extension and catalog installed for 0.8.4 were kept;
  0.8.5's notes say they stay compatible, and only the example's README
  changed.
- **gardener:** 0.4.4 plus branch `feature/orket-088-checks`.
- **Date:** 2026-10-11.
- **Environment:** WSL2, CPU-only GT 1030 box, Ollama 0.24.0 on its default
  settings, `qwen2.5:7b`.
- **Profiles:** the operator copies used for 0.8.4: 32,768 tokens for
  delivery, 131,072 for the large-input check. They still match the packaged
  registry apart from `context_budget_tokens`.

## The Orket tier

All 12 tests pass on 0.8.8 (490 s): 8 refusals, plus 4 tests that call the
model.

| Test | Result |
|---|---|
| delivery | delivered, `completion_satisfied`, replay `matched`, 189 s (460 s on 0.8.4) |
| timed-out run | closed `failed_terminal` with its run ID |
| input over 64 KiB | reached the model |
| target changed during the run | refused; see below |

**The delivered report:**
- **Checklist:** 8 of 9 items correct for the fixture. It again said the
  README has no run instructions, though it shows `python multiply.py 6 7`.
- **Summary line:** says "4 gap(s)" but names 7, the same slip as on 0.8.4.

Orket verifies delivery and references, not findings, so these are model
errors.

## 0.8.5: a refused delivery stays inspectable

A run whose target README was edited while the model ran:
- gardener's own tamper check failed it (`report-mode dispatch modified the
  target clone`);
- Orket's wrapper refused the delivery (`snapshot requires a clean
  committed source repository ... Git status: ' M README.md'`);
- the envelope carried `session_id: orket:agent-run:…`, and the result ended
  with the exact `orket agent inspect "agent-run:…" --db "…" --json` command;
- that inspection worked, and showed the captured snapshot's own outcome
  (`completion_satisfied`) unchanged by the later edit.

The tier now asserts the run ID, the inspect command, and a working
inspection on 0.8.5+ (`test_target_changed_during_the_run_is_refused`).
These assertions were not run against an older Orket; on an older checkout
they are skipped by version.

## 0.8.6: the headless extension's refusal names the setting

Run through gardener's real `align` prompt with `gardener-orket-extension`
against a 0.8.8 API host:

| Host profile | Output requested | Result |
|---|---|---|
| packaged 8,192 tokens | 1,024 (the extension's old default) | HTTP 400 `E_MODEL_OUTPUT_LIMIT: requested=1024, effective=768`, in 0.4 s |
| packaged 8,192 tokens | 768 | HTTP 400 `E_MODEL_INPUT_BUDGET: utf8_byte_estimate=7538, input_limit=8192, context_limit=8192, output_limit=768; reduce evidence or increase request limits and the selected host profile context_budget_tokens (ORKET_LOCAL_PROMPT_PROFILE_REGISTRY_PATH), within model capacity; input was not truncated` |
| 32,768 tokens | 768 | reached the model (70 s). `qwen2.5:7b` cited no source, so the extension refused it (`E_REPORT_UNSOURCED`) |

The second row is 0.8.6's fix: the refusal now names the setting and says
the input was not truncated.

The first row is a change on Orket's side: since 0.8.5 a request above the
output ceiling is refused instead of lowered. The extension's old default
of 1,024 is above the packaged ceiling for this API (768 on most profiles),
so every dispatch failed before reaching the model. Orket documented this
in its guide. The extension is being changed to request 768 by default and
to report this refusal as `blocked`, since it repeats for every repository.

## Reading the results

- **0.8.5 and 0.8.6 work as described.** A refused delivery stays
  inspectable, and the host refusal names the setting to change.
- **0.8.7's guide matches what was needed here.** The one thing it adds is
  that an older editable install keeps stale package metadata. This box's
  venv reported `orket.__version__ == "0.8.1"` until the install was re-run
  with `uv pip install -e`, as the guide's install step says.
- **Safety held throughout.** No run changed a clone or pushed, and every
  refusal named its cause.
- **The model is still the limit.** `qwen2.5:7b` delivers through Orket's
  governed harness, in about 3 minutes this time. It has not yet produced a
  cited report through the headless extension.
