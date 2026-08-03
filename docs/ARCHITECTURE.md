# Architecture

Stdlib-only Python — no third-party pip dependencies: no build step,
nothing to audit or pin. gardener shells out
to three external CLIs (`git`, `gh`, `claude`) rather than reimplementing
git hosting, GitHub API auth, or an agent loop — that's a process
dependency, not a pip one, and is the same shape other local tooling here
uses.

```
gardener/
  gardener/
    __init__.py      — package docstring and __version__; no runtime logic
    __main__.py      — `python3 -m gardener` entry point (delegates to
                       cli.main, same as the `gardener` console script)
    cli.py          — argparse CLI (align, tend, allowlist, garden, overnight,
                       status, tail-transcript, dashboard, update), prompt
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
    conventions.py   — resolves the configured conventions repo URL (no
                       built-in default) and clones/refreshes its local cache
    state.py         — SQLite-backed run history, plus repo_stats()'s
                       all-time per-repo aggregates for the garden view
    repo_lock.py     — cross-process, per-repo fcntl.flock exclusion so two
                       gardener invocations never clone/checkout/dispatch
                       against the same shared clone directory at once (see
                       Usage's "Concurrent dispatch safety")
    selfupdate.py    — fast-forwards gardener's own checkout to origin (never
                       a target repo) for `gardener update` and, by default,
                       at the start of `gardener overnight`; a dirty tree,
                       detached HEAD, or diverged branch all skip rather than
                       force anything (see Usage's "Self-update" section)
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
                       run_log, dashboard, selfupdate)
```

## Relationship to a conventions repo

`align` treats an operator-configured **conventions repo** as its only
source of truth for what "aligned" means. `conventions.py` resolves that
repo's URL from `--conventions-repo` or `$GARDENER_CONVENTIONS_URL`, clones
a local cache of it (default `~/.cache/gardener/conventions`, refreshed on
every run unless `--no-refresh-conventions` is given), and reads its
`ALIGNMENT_PROMPT.md`, `ALIGNMENT_CHECKLIST.md`, and every doc under
`docs/` to build the prompt it dispatches. gardener adds no conventions of
its own beyond the safety/dispatch mechanics described above — it doesn't
decide what a README or CI workflow should look like.

There is deliberately **no default conventions repo**. A baked-in default
would make gardener quietly opinionated about every target repo it touches,
which is exactly the thing this separation exists to prevent — so `align`
raises `ConventionsError` with setup instructions instead of guessing. See
[README.md's "Conventions repo"](../README.md#conventions-repo) for the
required layout, which `verify_complete` enforces as a file-existence
contract only, never a content one.

## Relationship to create-dev-loop

`tend`/`garden`/`overnight` dispatch a target repo's own
`<slug>-dev-loop` skill, generated by
[`create-dev-loop`](https://github.com/dmccoystephenson/create-dev-loop) —
a separate open-source project. `dev_loop.py` is the seam: it derives the
skill slug from the repo name, detects whether the skill already exists,
and builds the bootstrap prompt that invokes `/create-dev-loop` when it
doesn't. gardener never generates skill content itself, the same way it
never authors convention content — both are inputs it orchestrates.
