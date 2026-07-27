# Project Status

Working end to end: built, unit-tested, and verified with one real
report-only run against `dmccoystephenson/create-dev-loop` (29 gaps found,
target repo confirmed untouched afterward — see
[Manual/end-to-end verification](TESTING.md#manualend-to-end-verification)).
`--implement` and `--file-issue` are implemented and unit-tested at the
argv-construction level but have not yet been run for real against a live
repo — only report-only mode has been exercised end to end so far.

`tend` (no `--allow-merge`) has also been run for real, end to end,
against `dmccoystephenson/example-repo` (2026-07-18): no
existing `/example-repo-dev-loop` skill, so `create-dev-loop`
dispatched first (194s, $0.71, produced a usable skill), then `tend`
dispatched it (250s, $1.00, `ok=True`, 7 permission denials — out-of-scope
attempts correctly blocked, not itemized in gardener's own output beyond
the count). Result: the dispatched run found no open issues/PRs, added
missing CLI test coverage for two untested code paths, opened PR #5
(green on all 4 CI matrix legs), did not attempt to merge it, and ended
its final answer with a `DECISION NEEDED:` line naming the PR and that a
human must review/merge — exactly the fallback documented in
[the `tend` mode section](SAFETY.md#tend-mode-and-the-headless-ask-the-user-problem).
Confirmed afterward: `origin/main` on the target repo is unchanged
(`947474e`, the same commit as before the run) and PR #5 remains open,
not merged — `tend` without `--allow-merge` made real progress without
ever mutating the target repo's main branch. The run also completed in
250s, far inside `TEND_DEFAULT_TIMEOUT_SECONDS` (2700s), so the
"next cycle" loop-back risk noted in `dispatch.py`'s docstring did not
manifest here either.

`tend --allow-merge` has also been run for real, end to end, against the
same repo (2026-07-18), after deliberately adding it to the merge
allow-list first (`gardener allowlist add --repo
dmccoystephenson/example-repo`) — chosen because PR #5 was
already known-safe (test-only diff, +38/-0, zero application-code
changes, green on all 4 CI matrix legs, already reviewed once during the
no-flags run above). The dispatched run (79s, $0.32, `ok=True`, one
permission denial — an out-of-scope `find /` blocked, unrelated to
merging) re-confirmed CI was green, then squash-merged PR #5. Confirmed
afterward via the GitHub API: PR #5's `merged` flag is `true`, `main`'s
HEAD commit is exactly `Add test coverage for CLI output-file writing and
no-valid-data exit (#5)`, and the post-merge CI run on `main` completed
`success`. This is the one dispatch mode capable of a real merge, and it
worked exactly as designed: the merge pattern was reachable only because
both `--allow-merge` and the allow-list entry were present (see
`dispatch.py`'s `tend_mode_spec()`) — this is a deliberate, real merge
into a low-stakes personal repo, not an accidental one; it was not
attempted, and would not have been permitted, without both conditions
explicitly set up first.

`gardener overnight` has also been run for real, end to end (2026-07-18),
against a 2-repo garden (`dmccoystephenson/create-dev-loop`,
`dmccoystephenson/example-repo`) with an isolated
`GARDENER_STATE_DIR` and an empty merge allow-list (deliberately — neither
repo was eligible to merge for this test, same "err toward not merging"
posture as the `tend --allow-merge` verification above), invoked twice in a
row with `--hours 0.15` (a small budget, on purpose, so the run cycle could
be observed without waiting out a full 8h window):

- **Run 1**: started at the resume cursor's default (index 0,
  `create-dev-loop`, alphabetically first). No `/create-dev-loop-dev-loop`
  skill existed yet under that exact derived slug (the repo's actual local
  skill had been hand-named with an abbreviation, a different slug than gardener
  derives from the repo name — see `dev_loop.py`'s slug derivation), so
  `tend` bootstrapped one via `create-dev-loop` first, then dispatched it
  (403.6s dispatch, $1.87, `ok=True`, 3 permission denials — correctly
  blocked out-of-scope attempts). It opened PR #55, ended with a
  `DECISION NEEDED:` line (merge wasn't authorized — the repo isn't on the
  merge allow-list), and correctly did **not** attempt a second repo:
  `overnight stopping — insufficient budget remaining for another repo
  (-238s left, need 2700s headroom)`. The batch summary
  (`1 repo(s) attempted in 13.0m — 1 PR(s) opened, 0 merged, 1 awaiting a
  decision, 0 errored, 1 not reached this run`) printed to stderr exactly
  as designed (no Discord webhook was configured for this test, so the
  notify call itself was a clean `NullNotifier` no-op — see
  [Alerting (optional)](../README.md#alerting-optional)). The resume cursor advanced to
  index 1.
- **Run 2**: re-invoked identically. Correctly resumed at index 1
  (`example-repo`), NOT index 0 again — confirming the
  round-robin resume mechanism works across separate invocations, not just
  within a loop in one process. That repo already had its
  `/example-repo-dev-loop` skill (no bootstrap needed this time,
  hence a faster overall run: 340.2s dispatch, $1.22, `ok=True`, 4
  permission denials), opened PR #6, again ended in `DECISION NEEDED:`, and
  again stopped before attempting a second repo (`193s left, need 2700s
  headroom`). The cursor wrapped back to index 0 — both repos in the
  garden had now been attempted exactly once across the two runs.

Confirmed afterward via `gh pr list`: PR #55 (`create-dev-loop`) and PR #6
(`example-repo`) are both open, not merged — `overnight` made
real progress on two separate repos across two separate invocations without
ever mutating either target's default branch, exactly as designed. The
Android/UserLand `devsrv` wiring documented in [the overnight
docs](OVERNIGHT.md) was also verified for
real: `devsrv start <name> --autostart -- gardener overnight --hours 8`
(the budget the service was registered with at the time of that test; it
has since been re-registered with `--hours 6` — the wiring is what was
being verified here, not the number) (tested against both
the exact real command — confirmed its stderr output lands correctly in
`devsrv logs`, though a run against an empty garden exits before devsrv's
brief startup poll window closes, which devsrv reports as "failed to
start" even though it ran correctly and exited 0 — an artifact of testing
with a near-instant no-op, not a real failure mode for an actual
multi-repo overnight run — and, separately, a long-running placeholder
process to confirm the full `start`/`status`/`stop`/`restart`/`remove`
lifecycle and `--autostart` persistence all work exactly as devsrv's own
docs describe).

The WSL2/Windows Task Scheduler wiring documented in [the overnight
docs](OVERNIGHT.md) was separately
real-verified (2026-07-26), on the device that actually runs it: the
unpatched `bin/run-overnight.sh` failed 28/28 garden repos in about a
minute with `claude not found on PATH`, reproduced directly with
`env -i HOME="$HOME" USER=root bash -lc 'which claude'` against the exact
Task Scheduler invocation shape, fixed by exporting `PATH` explicitly in
the script, and confirmed by re-running the exact fixed script the same
way, which dispatched real work end to end. The log-duplication fix in
the same recipe was confirmed by inspection of a real run's log showing
every line doubled, traced to the script's own redirect and `run_log.py`
both writing the same path.

**Live transcript visibility** (see [Live session
visibility](USAGE.md#live-session-visibility)) has also been run for real
(2026-07-18), end to end, with a real `gardener align` (report-only)
against an unrelated low-stakes repo — deliberately not any repo still
possibly in use by a concurrent `gardener overnight` run at the time —
isolated to a scratch `GARDENER_CACHE_DIR`/`GARDENER_STATE_DIR` and with
stderr captured to a file for timestamped evidence:

- The dispatched `claude` subprocess's own transcript file records its
  first JSONL line at `15:00:58.278Z`. gardener's `gardener: session
  transcript: ...` line was already present in the captured stderr log the
  very next time it was checked, at essentially the same wall-clock second
  — comfortably inside the 5-second poll bound `transcript.py` uses.
- The dispatch itself did not finish until `gardener: done in 154001ms`
  (~154s later, landing at approximately `15:03:32Z`) — so the transcript
  path was visible roughly **2.5 minutes before the dispatch completed**,
  the actual gap this feature exists to close.
- While the dispatch was still in progress, `gardener tail-transcript
  <path>` (no `-f`) was run against the live, growing file and printed a
  real, readable, partial event stream — `Glob`/`Read` tool calls with
  their inputs and truncated results, and assistant reasoning text — well
  before the run finished.
- Afterward: `git -C <the cached clone> status --porcelain` was empty,
  `git log -1` showed no new commit, and `gh repo view ... --json
  pushedAt`/`gh pr list`/`gh issue list` were all unchanged from before the
  run — report mode's existing safety guarantee (see [Manual/end-to-end
  verification](TESTING.md#manualend-to-end-verification)) held with this addition
  in place, exactly as with every dispatch mode before it.

**`create-dev-loop`'s `add_dirs` fix** (see `dispatch.py`'s module
docstring finding #8) has also been verified for real, end to end
(2026-07-18). Root cause: `cmd_tend`'s `create-dev-loop` dispatch never
passed `add_dirs`, unlike `align`'s `add_dirs=[conv.path]` — `Write` isn't
sandboxed to `cwd`/`--add-dir` (finding #3), but `Read`/`Bash` are, so a
stale partial skill file from an earlier failed attempt had no recovery
path on retry. This was root-caused directly from a real
`gardener overnight` run's transcripts, then confirmed for real with the
actual failure's leftover artifact still on disk
(`~/local-skills/gardener-dev-loop/gardener-dev-loop.md` existed,
`~/.claude/commands/gardener-dev-loop.md` did not):

- An isolated scratch-directory probe (a real `claude -p` invocation using
  `MODE_SPECS[Mode.CREATE_DEV_LOOP]`'s exact tool/`--allowedTools` list,
  unchanged, plus `--add-dir` for two throwaway directories) confirmed
  `--add-dir` alone was sufficient: `Read` on a pre-existing file,
  `Bash(mkdir *)`, `Write` (fresh and overwrite-after-read), `Bash(ln -sf
  ...)`, and `Bash(ls -la ...)` all succeeded, zero `permission_denials` —
  no `MODE_SPECS` tool/pattern change was needed.
- After the fix, the stale artifact was removed
  (`rm -rf ~/local-skills/gardener-dev-loop`) and a real
  `gardener tend --repo dmccoystephenson/gardener` (no `--allow-merge`) was
  dispatched from this fix's own code. `create-dev-loop` succeeded this
  time: both `~/local-skills/gardener-dev-loop/gardener-dev-loop.md` and
  the `~/.claude/commands/gardener-dev-loop.md` symlink existed
  immediately afterward. `tend` then proceeded past the bootstrap step and
  dispatched `/gardener-dev-loop` itself (591.6s, $2.33, `ok=True`, 9
  permission denials — correctly blocked out-of-scope attempts), which
  found real work (two open bugs, #2 and #3), opened
  [PR #7](https://github.com/dmccoystephenson/gardener/pull/7) hardening
  `cmd_align`/`cmd_tend`/`cmd_overnight` against raw crashes from
  subprocess timeouts and corrupted garden/allow-list JSON, and correctly
  ended with a `DECISION NEEDED:` line (merge wasn't authorized). Confirmed
  afterward: `origin/main` is unchanged (`030d3fc`, same commit as before
  the run) and PR #7 remains open, not merged — gardener's first real
  self-tend made genuine progress on its own codebase without ever
  mutating its own main branch.
