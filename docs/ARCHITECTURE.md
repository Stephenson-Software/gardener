# Architecture

Stdlib-only Python — no third-party pip dependencies. This matches the
established house style used elsewhere in this ecosystem ("Plain stdlib
Python... no build step / third-party deps"). gardener shells out
to three external CLIs (`git`, `gh`, `claude`) rather than reimplementing
git hosting, GitHub API auth, or an agent loop — that's a process
dependency, not a pip one, and is the same shape other local tooling here
uses.

```
gardener/
  gardener/
    cli.py          — argparse CLI (align, tend, allowlist, status), prompt
                       building, orchestration
    dispatch.py      — the safety-gated subprocess wrapper around `claude -p`
                       (Mode/ModeSpec definitions and tend_mode_spec() for
                       every mode, including tend's per-invocation merge gate)
    dev_loop.py      — resolves/derives a target repo's <slug>-dev-loop skill,
                       builds the create-dev-loop and tend prompts (including
                       the headless-safety preamble)
    merge_allowlist.py — local JSON allow-list of repos `tend --allow-merge`
                       is permitted to actually merge PRs in
    garden.py        — local JSON opt-in list of repos `gardener overnight`
                       is permitted to tend unattended (independent of the
                       merge allow-list above — see garden.py's docstring)
    overnight.py     — pure budget/rotation/batching/resume-cursor/outcome-
                       classification logic for `gardener overnight`;
                       cli.py's cmd_overnight composes it with real time and
                       a real (in-process, optionally concurrent via
                       ThreadPoolExecutor) tend dispatch
    conventions.py   — clones/refreshes the local dms-conventions cache
    state.py         — SQLite-backed run history, plus repo_stats()'s
                       all-time per-repo aggregates for the garden view
    repo_lock.py     — cross-process, per-repo fcntl.flock exclusion so two
                       gardener invocations never clone/checkout/dispatch
                       against the same shared clone directory at once (see
                       Usage's "Concurrent dispatch safety")
    notify.py        — pluggable outcome notifications (Notifier/DiscordNotifier/NullNotifier)
    transcript.py    — live transcript-file discovery (encoding rule + bounded
                       poll, run from a background thread `dispatch.run_claude`
                       starts) and the `gardener tail-transcript` pretty-printer
    run_log.py       — tees a dispatching run's stderr narration to
                       <state>/logs/<command>-<stamp>.log, which is the file
                       dashboard.py's live panels read back (see Usage's
                       "Run logs")
    dashboard.py     — read-only stdlib http.server UI over the run history,
                       garden/allow-list files, and the active run log;
                       build_garden_rows() joins the first three into the
                       garden view's table/plant-plot rows
    prompts/align_repo.md.tmpl — the prompt template dispatched to Claude
  tests/             — unit tests (state, cli parsing/templating/notify-severity,
                       mocked dispatch, notify, garden, overnight, transcript,
                       run_log, dashboard)
```

## Relationship to dms-conventions

gardener consumes [`dmccoystephenson/dms-conventions`](https://github.com/dmccoystephenson/dms-conventions)
as its only source of truth for what "aligned" means — it clones a local
cache of that repo (default `~/.cache/gardener/dms-conventions`, refreshed
on every run unless `--no-refresh-conventions` is given) and reads its
`ALIGNMENT_PROMPT.md`, `ALIGNMENT_CHECKLIST.md`, and every doc under
`docs/` to build the prompt it dispatches. gardener adds no conventions of
its own beyond the safety/dispatch mechanics described above — it doesn't
decide what a README or CI workflow should look like, dms-conventions
does.
