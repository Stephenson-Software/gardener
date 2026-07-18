# CLAUDE.md — gardener

## What this repo is

gardener is a Python CLI that aligns one target software repo at a time
against [`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions).
It's phase 2 of a two-phase initiative — dms-conventions is the source of
truth for what "aligned" means; this repo is the orchestration and
safety-gating layer around a dispatched `claude -p` run that does the
actual reading/analysis/implementation. See `README.md` for the full CLI
shape and safety model.

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
`~/pocket-rig/dashboard/server.py`'s existing Claude-dispatch endpoint (for
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
  `test_cli.py` covers argument parsing and prompt templating.
- **Manual (required for anything touching `dispatch.py` or the prompt
  template):** run a real `gardener align --repo <owner/repo>` in
  report-only mode (no flags) against a low-stakes repo you have access
  to, then confirm the target repo was not mutated:
  `git -C <cached clone> status --porcelain` is empty, `git log` shows no
  new commit, and `gh repo view <owner/repo> --json pushedAt` matches
  what it was before the run. This is the actual verification gardener's
  first working version was held to (see `README.md`'s
  "Manual/end-to-end verification" section) — don't consider a
  dispatch-layer change done without repeating it.
- `--implement` and `--file-issue` should be exercised for real (not just
  unit-tested) before being trusted against anything that matters, the
  same way report mode was — this hasn't happened yet as of this repo's
  first version (see `README.md`'s Project Status).

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
| `README.md` | CLI usage, flags, and safety-model claims match what `cli.py`/`dispatch.py` actually do |
| `dispatch.py` module docstring | Every claim about `claude` CLI behavior (`--tools`, `--allowedTools`, `--permission-mode`) still holds against the currently-installed `claude` version |
| `gardener/prompts/align_repo.md.tmpl` | Placeholders match exactly what `cli.py`'s `build_prompt` substitutes; references to dms-conventions doc paths match that repo's actual current layout |
| `tests/` | Still passes (`PYTHONPATH=. python3 -m unittest discover -s tests -v`) and still never invokes a real `claude`/`gh` process |
