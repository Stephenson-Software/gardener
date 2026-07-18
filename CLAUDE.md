# CLAUDE.md — gardener

## What this repo is

gardener is a safety-gated Python CLI that dispatches Claude Code against a
fleet of software repos: `align` checks one target repo at a time against
[`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions)
(phase 1 of this two-phase initiative — dms-conventions is the source of
truth for what "aligned" means; gardener is the tool that consumes it),
while `tend`/`garden`/`overnight` make real, broader progress on a repo (or
a whole opt-in list of them, unattended overnight) by dispatching that
repo's *own* dev-loop skill instead. This repo is the orchestration and
safety-gating layer around a dispatched `claude -p` run that does the
actual reading/analysis/implementation in every mode. See `README.md` for
the full CLI shape and safety model.

gardener also has a second, distinct command, `tend`, that makes real,
broader progress on a target repo by dispatching *that repo's own*
`<slug>-dev-loop` Claude Code skill headlessly (rather than dms-conventions'
alignment prompt) — for an eventual "tend to my garden overnight" use case,
several repos dispatched in sequence, unattended. Its safety mechanics
(`dev_loop.py`, `merge_allowlist.py`, `dispatch.py`'s `tend_mode_spec()`)
follow the exact same "structural exclusion + explicit instruction" pattern
`align` established, extended to a new problem `align` never had to solve:
a dispatched dev-loop skill is written to stop and ask a human before
merging, and headless dispatch has no human to ask. See README's Safety
model section for what was actually tested and observed before this was
trusted.

The "tend to my garden overnight" use case above is now real:
`gardener garden` (`garden.py`) is a small opt-in list of repos, and
`gardener overnight` (`overnight.py` for the pure budget/rotation/resume
logic, `cli.py`'s `cmd_overnight` for the real orchestration) dispatches
`tend --allow-merge` in-process across that list within a time budget,
resuming across invocations via a small cursor file when the garden is
bigger than one budget window. It adds no new merge-decision logic of its
own — `tend`'s existing `merge_eligible()` gate (repo must *also* be on the
separate merge allow-list) is what actually decides whether a merge can
happen, unchanged. See README's "Overnight / unattended operation" section
for the full design and the honest reliability caveat about this device
having no true always-on daemon guarantee.

Every dispatch (`align`/`tend`/`overnight`) still runs synchronously — see
`dispatch.py`'s "Why synchronous dispatch" note — but is no longer a total
black box while it runs: `transcript.py` polls briefly, from a background
thread, for the live JSONL transcript file Claude Code already writes for
every `-p` session, and logs its path to stderr within seconds of dispatch
start. `gardener tail-transcript <path> [-f]` pretty-prints that file. See
README's "Live session visibility" section.

## Conventions

- **Stdlib-only Python.** No pip dependencies beyond the standard library.
  If a future change genuinely needs one, justify it explicitly in
  `README.md`'s Architecture section before adding it — this has held
  since the first commit and shouldn't erode silently.
- **Safety constraints live in `dispatch.py`, not scattered across
  callers.** Every mode's `claude` invocation (tool list, permission mode,
  allowed-tools scoping) is defined once in `MODE_SPECS` and built by
  `_build_invocation`. If you're tempted to pass an extra flag to `claude`
  from `cli.py` directly instead of adding it to a `ModeSpec`, don't —
  that's exactly the "bolted on after" shape the safety model is designed
  to avoid.
- **`bypassPermissions` (or any equivalent) must never be reachable**, for
  any mode, under any flag combination. `_build_invocation` has a runtime
  check that raises `DispatchError` if it's ever configured — this is
  deliberately redundant with `MODE_SPECS` never containing it; don't
  remove the runtime check as "belt and suspenders no one needs," it's
  there so a future edit to `MODE_SPECS` can't silently regress this.
- **Every behavioral claim about the `claude` CLI in `dispatch.py`'s
  docstring was confirmed by hand** against a real invocation before being
  relied on (see the docstring itself for what was tested and how). If
  `claude --help`'s output for `--tools`, `--allowedTools`, or
  `--permission-mode` ever changes, re-verify against a real invocation
  before trusting the old notes — don't assume the mechanism still works
  the same way.
- **The prompt template (`gardener/prompts/align_repo.md.tmpl`) is
  substituted with `string.Template` (`$name` placeholders), not
  `str.format`** — the template is markdown that may contain literal `{`
  from a table or code fence, which `str.format` would choke on.

## What belongs here vs. elsewhere

Conventions about what a README, CLAUDE.md, CONTRIBUTING.md, CI workflow,
etc. *should* look like belong in `dms-conventions`, never here — gardener
only orchestrates dispatching that content, it doesn't define it. If you
find yourself wanting to hardcode a convention rule directly into
`gardener/prompts/align_repo.md.tmpl` instead of pointing at the relevant
dms-conventions doc, that rule almost certainly belongs in dms-conventions
itself; open a PR there instead.

Conversely, anything about *how gardener safely dispatches Claude* — tool
scoping, permission modes, the sync-vs-background call, state schema —
belongs here, not in dms-conventions (which has no opinion on gardener's
own implementation, per dms-conventions' own README: "nothing in this repo
assumes or depends on it").

## Grounding work in research

Not research-doc-driven in the `RESEARCH.md` sense. The safety model is
grounded in two things instead, both cited directly in `dispatch.py`'s
docstring and `README.md`'s Safety model section: reading
`~/a-private-repo-2/dashboard/server.py`'s existing Claude-dispatch endpoint (for
the `bypassPermissions`-hard-reject posture), and hands-on confirmation
against a real `claude -p` invocation for exactly what `--tools`,
`--allowedTools`, and each `--permission-mode` actually do — not assumed
from the help text alone. If you change the dispatch mechanism, ground the
change the same way: run it for real against a throwaway prompt/repo and
record what you actually observed before changing the docstring's claims.

## Testing changes

- **Automated:** `PYTHONPATH=. python3 -m unittest discover -s tests -v`.
  `test_dispatch.py` mocks `subprocess.run` — it must never actually
  invoke `claude`. `test_state.py` uses a real sqlite3 file in a tmp dir.
  `test_cli.py` covers argument parsing, prompt templating, `cmd_tend` with
  clone/dispatch mocked, and `cmd_overnight` with `cmd_tend` mocked (and
  `time.monotonic` mocked for the budget-specific assertions).
  `test_dev_loop.py` covers slug derivation and prompt content,
  `test_merge_allowlist.py` covers the allow-list's JSON read/write, and
  `test_garden.py`/`test_overnight.py` cover the garden JSON list and
  `overnight.py`'s pure rotation/budget/resume-cursor/outcome-classification
  logic the same way — none of these invoke `claude`, `git`, or `gh` either.
  `test_transcript.py` covers `encode_cwd` against the two real,
  empirically-confirmed examples in `transcript.py`'s module docstring
  (never invented ones — if `claude`'s actual encoding rule ever changes,
  re-confirm against a real dispatch the same way before updating these),
  the polling loop (`time_fn`/`sleep_fn` always injected, never a real
  sleep), and the pretty-printer's line parsing (synthetic JSONL fixtures).
- **Manual (required for anything touching `dispatch.py`, `dev_loop.py`, or
  a prompt template/preamble):** run a real dispatch against a low-stakes
  repo you have access to and confirm the target repo was not mutated
  beyond what the mode is actually meant to do:
  - `align` (no flags): `git -C <cached clone> status --porcelain` is
    empty, `git log` shows no new commit, and
    `gh repo view <owner/repo> --json pushedAt` matches what it was before
    the run. This is the actual verification gardener's first working
    version was held to (see `README.md`'s "Manual/end-to-end
    verification" section) — don't consider a dispatch-layer change done
    without repeating it.
  - `tend` (no `--allow-merge`): confirm no merge occurred
    (`gh pr list --state all` shows no new merged PR), confirm
    `permission_denials` is empty or only contains attempts that were
    correctly out of scope, and confirm the run did not hang — it should
    return well inside `TEND_DEFAULT_TIMEOUT_SECONDS`.
  - `tend --allow-merge`: only run this against a repo you've deliberately
    added to the merge allow-list yourself as part of the test
    (`gardener allowlist add`), and only if you're confident merging
    whatever it produces is actually safe — this is the one dispatch mode
    capable of a real, semi-autonomous merge; don't skip reviewing what it
    actually did afterward.
  - `overnight`: real-verify with a small `--hours` (e.g. `0.1`-`0.2`)
    against a garden of 1-2 low-stakes repos (`gardener garden add`) —
    confirm it dispatches, stops within budget, and produces the batch
    summary notification. Separately, run it twice against a garden bigger
    than one budget window and confirm the second run's resume cursor
    (`~/.local/state/gardener/overnight_cursor.json` by default) advanced
    past what the first run already attempted rather than restarting from
    the top of the garden. Keep the merge allow-list empty (or scoped to
    only repos you're already confident about, per the `tend --allow-merge`
    note above) for this test — `overnight` passes `--allow-merge`
    unconditionally, so whether anything can actually merge depends
    entirely on the separate merge allow-list, same as a direct `tend
    --allow-merge` call.
- `--implement` and `--file-issue` should be exercised for real (not just
  unit-tested) before being trusted against anything that matters, the
  same way report mode was — this hadn't happened as of this repo's first
  version (see `README.md`'s Project Status) and is unrelated to `tend`'s
  own verification above.

## Commit and PR conventions

Branch prefixes `feature/` and `fix/`, imperative-mood commit messages, no
trailing period. Squash-merge PRs. When Claude Code authors a commit, use
the HEREDOC + `Co-Authored-By` trailer form:

```bash
git commit -m "$(cat <<'EOF'
<description, imperative mood, no trailing period>

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

Match the model name in the trailer to whichever model actually authored
the commit. This is the same convention documented in
`dms-conventions/docs/COMMIT_PR_CONVENTIONS.md` — gardener follows its own
target-repo alignment rules, not just the ones it enforces on others.

## Documentation sources of truth

| File | What to verify |
|---|---|
| `README.md` | CLI usage, flags, and safety-model claims match what `cli.py`/`dispatch.py`/`dev_loop.py` actually do |
| `dispatch.py` module docstring | Every claim about `claude` CLI behavior (`--tools`, `--allowedTools`, `--permission-mode`, and the `tend`-specific `AskUserQuestion`/`Agent`/`ScheduleWakeup` findings) still holds against the currently-installed `claude` version |
| `gardener/prompts/align_repo.md.tmpl` | Placeholders match exactly what `cli.py`'s `build_prompt` substitutes; references to dms-conventions doc paths match that repo's actual current layout |
| `dev_loop.py`'s `HEADLESS_SAFETY_PREAMBLE` and prompt builders | Still accurately describes which tools are absent/excluded in `tend`/`create-dev-loop` mode (must match `dispatch.py`'s actual `tend_mode_spec()`/`MODE_SPECS[Mode.CREATE_DEV_LOOP]`) |
| README's "Overnight / unattended operation" section | Matches what `garden.py`/`overnight.py`/`cli.py`'s `cmd_overnight` actually do (default `--hours`, budget/headroom rule, resume-cursor file path, the exact `devsrv` invocation) and still states the "no true always-on daemon guarantee on this device" caveat plainly, not oversold |
| `transcript.py` module docstring | The transcript-path encoding rule (`encode_cwd`) still matches a real `claude -p` session's actual `~/.claude/projects/<encoded-cwd>/` directory naming — re-verify against a real dispatch (not assumption) before trusting the old notes if this ever seems off |
| `tests/` | Still passes (`PYTHONPATH=. python3 -m unittest discover -s tests -v`) and still never invokes a real `claude`/`gh` process |
