# Safety model

gardener never invokes `claude` with `bypassPermissions` or any
equivalent auto-approve-everything mode, for any mode, under any flag
combination — this is enforced in `dispatch.py` (`_build_invocation`
raises rather than silently proceeding if it's ever reached), mirroring
the same posture as another local Claude-dispatch endpoint used
elsewhere, which hard-rejects `bypassPermissions` server-side the same
way.

Beyond that floor, each mode is built from three independently-confirmed
layers (see the full writeup in `dispatch.py`'s module docstring):

1. **`--tools`** is a hard allow-list of which tool *types* exist in the
   session at all — report mode gets only `Read,Grep,Glob`. Confirmed
   directly: a session given that tool list has no Write/Edit/Bash object
   to call, not merely one that's denied.
2. **`--permission-mode default`** for implement/file-issue — every tool
   call needs approval, and headless mode has no human to give it, so
   anything not pre-approved is auto-denied (confirmed directly, and
   logged back via the JSON result's `permission_denials`). `acceptEdits`
   was tried and rejected: it auto-approved a Bash call that was
   deliberately left unscoped, defeating the point.
3. **`--allowedTools`** pre-approves narrow patterns (`Bash(git *)`,
   `Bash(gh pr *)` for implement; `Bash(gh issue *)` only, no Edit/Write at
   all, for file-issue) so only those specific commands run without a
   human.

`--strict-mcp-config` (with no `--mcp-config`) is passed for every mode,
so a dispatched run never inherits whatever MCP servers (Gmail, Drive,
Calendar, ...) happen to be configured for the invoking user.

## `tend` mode, and the headless "ask the user" problem

Every `<slug>-dev-loop` skill `tend` might dispatch is written to *stop and
ask a human* (via the `AskUserQuestion` tool) before merging or taking a
destructive action — real, observed behavior in every dev-loop skill on
this machine. Dispatched headlessly by `gardener tend`, nobody is there to
answer. This was confirmed by hand (2026-07-18, Claude Code CLI 2.1.214)
before being relied on, the same standard the rest of this safety model is
held to — full transcript-level detail lives in `dispatch.py`'s module
docstring; the short version:

- **`AskUserQuestion` is not present at all in a headless `-p` session, by
  default** — a session given `--tools default` (every built-in tool) was
  asked to list every tool available to it, direct or deferred; it wasn't
  in either list. `tend` never needs to add it to a `--tools` allow-list to
  exclude it — it's already absent before gardener does anything.
- **Without it, a session doesn't hang or fabricate an answer** — told
  (mirroring a real dev-loop's merge-confirmation language) to get human
  sign-off via `AskUserQuestion` before merging, a session with no such
  tool returned cleanly in 22s explaining it had no way to ask and would
  not proceed or invent a substitute. `tend`'s dispatched prompt still
  states this explicitly (`dev_loop.py`'s `HEADLESS_SAFETY_PREAMBLE`)
  rather than relying on that being the default every time: leave whatever
  was safely completed in place, and end with one line starting
  `DECISION NEEDED:` naming what's needed and by whom.
- **`Agent` and `ScheduleWakeup`** (used by a normal dev-loop cycle to fan
  out a review subagent and poll for its result) **are present by default
  and are excluded from `tend` mode's `--tools` the same structural way**
  — confirmed live: a session scoped to `tend`'s actual tool list reported
  back exactly that list, no Agent/ScheduleWakeup/AskUserQuestion/
  ToolSearch/ReportFindings among them. The dispatched prompt tells the
  run to perform any review step inline instead.
- **`gh pr merge` is never reachable via a broad `Bash(gh pr *)` pattern**
  in `tend` mode — confirmed live that such a pattern *does* let `gh pr
  merge` execute (this is a real, pre-existing gap in `align --implement`'s
  own `Bash(gh pr *)` allow-list, noted but out of scope to change here).
  `tend` enumerates specific non-merge `gh pr` subcommands instead and adds
  `Bash(gh pr merge *)` only per the merge allow-list below.
- **`gh pr review` and `gh api` are absent on purpose, so a dispatched run
  posts its self-review as a plain PR comment.** Every dev-loop skill tells
  its review phase to POST a real Review object (`gh api
  repos/<owner>/<repo>/pulls/<n>/reviews`, or `gh pr review --comment`);
  both are denied under `tend`, observed live (see
  [#40](https://github.com/dmccoystephenson/gardener/issues/40)). They stay
  denied because `gh pr review *` also spells `gh pr review --approve` — a
  session approving its own PR could satisfy a branch-protection approval
  requirement, which is exactly the human gate this model preserves — and
  because `gh api *` is a wildcard over the whole GitHub API, including
  merges and repo settings. The trade is a review without anchored inline
  comments; `dev_loop.py`'s `build_tend_prompt` covers it by telling the
  dispatched run up front to post via `gh pr comment` with each inline
  finding folded in as a `path:line` note, rather than letting every run
  rediscover the denial. It lives in that per-dispatch block rather than
  the `HEADLESS_SAFETY_PREAMBLE` both prompts share because `create-dev-loop`
  mode isn't granted `gh pr comment` either — a shared preamble must never
  name a command only one mode can actually run.

## Merge allow-list mechanics

`gardener tend --allow-merge` only ever results in `Bash(gh pr merge *)`
being added to the dispatched session's `--allowedTools` when BOTH
`--allow-merge` was passed AND the target repo is present in
`merge_allowlist.py`'s local JSON list (`gardener allowlist add`). Either
alone leaves the pattern out of the argv gardener builds entirely — under
`--permission-mode default` with nobody to approve an unlisted tool call,
an attempted `gh pr merge` in that case is auto-denied the same way any
other out-of-scope Bash call is (confirmed general mechanism, see point 2
in the layer list above) — not merely discouraged in the prompt.

## Why synchronous dispatch, not `--bg`

Some dashboards use `claude --bg` because they're answering an HTTP
request that can't hang open. `gardener align` is invoked from a terminal
or cron and can reasonably block until Claude finishes, so it uses
`claude -p` (headless "run once, print, exit" mode) instead — one
subprocess call, full output captured synchronously via
`--output-format json`, a meaningful exit code, nothing left running in
the background after the command returns. This is what makes
`gardener align`'s output pipeable and scriptable in a way `--bg` isn't.
