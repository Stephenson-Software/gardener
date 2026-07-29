# Testing

See also [CONTRIBUTING.md](../CONTRIBUTING.md) for how to propose a change
and branch naming.

## Automated Tests

Linux/macOS:

    PYTHONPATH=. python3 -m unittest discover -s tests -v

Windows (PowerShell):

    $env:PYTHONPATH = "."; python -m unittest discover -s tests -v

A passing run ends with `OK`. `tests/test_dispatch.py` mocks
`subprocess.run` and never actually invokes `claude` — including the
device-wide failure classification and retry policy (each of
`looks_like_auth_failure`/`looks_like_usage_limit`/`looks_like_network_failure`
matched against the *verbatim* text of a real recorded failure rather than
paraphrased, that `is_device_global_failure` covers all three while ordinary
build/test failures trip none of them, that the retry stops as soon as auth
recovers and gives up once the backoff is exhausted, and that a usage limit
is flagged blocked but never retried), always with `sleep_fn` injected so no
test actually sleeps; `tests/test_state.py`
uses a real sqlite3 file in a tmp dir (including `repo_stats`' all-time
per-repo aggregates — that every value in `state.KNOWN_OUTCOMES` is
classified as either a success or an error rather than falling silently
between the two, that a successful `align --implement`/`--file-issue` run
and a `created_incomplete` bootstrap all count as successes, that a later
`error` never overwrites `last_success`, and that `last_outcome` breaks a
same-second timestamp tie by row id); `tests/test_cli.py` covers argument
parsing (including `repo_arg`, the `type=` callable that rejects a
malformed `--repo` as a usage error at parse time on `align`/`tend`/
`allowlist add`/`garden add`, while `allowlist remove`/`garden remove`/
`status --repo` deliberately still accept one), prompt templating, the
coupling between `Mode`'s values and `state.KNOWN_OUTCOMES` (a successful
run records `mode.value` verbatim as its outcome, so a new `Mode` that
isn't a known outcome would go uncounted in `repo_stats`),
`_notify_run`'s severity mapping (mocking the
notifier, not `state.Run` construction), `cmd_align` and `cmd_tend` with
clone/dispatch mocked (mode selection, `state.record_run`/`_notify_run`
wiring, and exit codes), `cmd_status`'s own rendering (empty-history
message, header/row formatting, long-summary truncation, over a real
sqlite3 tmp-dir db), `cmd_tail_transcript`/`cmd_dashboard`'s argparse
wiring and thin pass-through behavior (path/follow forwarded to
`transcript.print_transcript`; port fallback and `state_dir` forwarded to
`dashboard.find_free_port`/`run_server`), `cmd_allowlist` and `cmd_garden`
(their structurally-identical list/add/remove branches, over the merge
allow-list and the garden respectively), `fetch_open_issue_count`/`fetch_issue_counts`
(the `issue-count` strategy's `gh`-calling side) with `_run` mocked the
same way `find_orphaned_pr`'s own tests are, and `cmd_overnight` with
`_dispatch_tend` itself mocked — including its `--concurrency` batching
(one test asserts every repo in a `ThreadPoolExecutor`-dispatched batch
still gets attempted regardless of completion order, another asserts
`concurrency=1` never touches `ThreadPoolExecutor` at all, and a third
feeds `cmd_overnight`'s real captured stderr through
`dashboard.parse_batch_progress` so the two log shapes it emits — `N/T`
sequential and `N-M/T` concurrent — can't drift away from the regex the
dashboard reads them with), the same drift guard applied to
`_dispatch_tend`'s own progress markers (the *real* function's captured
stderr fed through the *real* `dashboard.parse_in_progress`, down each of
its four return paths plus a `KeyboardInterrupt`, with the notifier silent
so the no-webhook case is what's actually asserted), and its
`--strategy` selection (`issue-count` with `fetch_issue_counts` mocked,
`random` with an injected `--random-seed` for a deterministic shuffle, both
asserting the repo-name-keyed resume cursor advances correctly across two
invocations without disturbing round-robin's own `next_index` in the same
cursor file) and its device-wide abort behavior (that an auth failure, a
usage limit, and a pre-dispatch GitHub outage each stop the run rather than
dispatching the rest of the garden, that the cursor keeps the progress made
before the failure but never advances past the repo that hit it — asserted
for both the positional round-robin cursor and the name-keyed strategy
cursor — that a dedicated ERROR notification fires alongside the batch
summary naming the specific class rather than always saying "authenticate",
that a failing notifier doesn't crash the run, and that an *ordinary*
per-repo error still does not abort the batch or hold the cursor) and
its cursor durability under a kill (a `BaseException` raised from the
mocked dispatch, which `_dispatch_one_for_overnight`'s `except Exception`
deliberately doesn't catch, so `cmd_overnight`'s post-loop code never runs
— the closest a unit test gets to this device's real task-swipe kill, and
what distinguishes per-batch persistence from the write-once-at-the-end
version that lost a whole run's progress) — and,
where the budget/headroom logic specifically is under test,
`time.monotonic` mocked too, so timing assertions never depend on
wall-clock jitter. `tests/test_cli.py` also covers the target-repo refresh's
`git clean` invocation with `_run` mocked (dependency caches excluded via
`-e`, build outputs still cleaned, the clean step's longer timeout, and a
failing clean still raising with the full command in the message), and
`main()`'s `log_name` wiring — that `build_parser()` sets it only on the
dispatching subcommands (`align`/`tend`/`overnight`) and leaves it unset on
the read-only ones (`status`/`allowlist`/`garden`/`tail-transcript`/
`dashboard`), and that `main()` itself opens a run log only for the former,
with `cmd_align`/`cmd_status` and `run_log.tee_stderr` mocked so no test
opens a real log file;
`tests/test_notify.py` mocks `urllib.request.urlopen` so `DiscordNotifier`
is fully covered — success, a failed POST, and "no webhook configured" —
without ever making a real HTTP call; `tests/test_garden.py` and
`tests/test_overnight.py` cover the garden JSON list and `overnight.py`'s
pure rotation/batching/budget/resume-cursor/outcome-classification logic
with real files in a tmp dir, including `order_by_issue_count` (pure sort
over an already-fetched count mapping), `random_order` (injectable
`random.Random`), and `resume_order`/`next_attempted` (the name-keyed
cursor's cycle-completion and reset logic); `tests/test_conventions.py`
covers `ConventionsSource.verify_complete()`'s missing-doc detection and
`ensure_conventions()`'s clone/fetch-reset/no-refresh branches, with
`_run_git`/`subprocess.run` mocked so no real `git` process ever runs;
`tests/test_transcript.py` covers the encoding rule
(against the two real, empirically-confirmed examples in `transcript.py`'s
module docstring, not invented ones), the transcript-file-discovery polling
loop (real files in a tmp dir, but `time_fn`/`sleep_fn` always injected so
nothing ever sleeps for a real second), and the pretty-printer's
line-parsing logic (synthetic JSONL fixtures covering `tool_use`/`text`/
`tool_result`, malformed JSON, and blank lines); `tests/test_dashboard.py`
covers the dashboard's pure log-parsing and status-assembly functions —
`find_active_log`, `find_active_logs` (the recency window and its exact
boundary under an injected clock, its newest-log fallback, and that the
window outlasts a silent `tend` dispatch), `tail_lines`,
`parse_in_progress` (including the `finished tending` marker clearing a
repo with no notify line present at all, and a repo restarted after
finishing reading as in flight again),
`parse_batch_progress`, `find_free_port`, `build_garden_rows` (the
garden/allow-list/history join behind [the garden view](DASHBOARD.md),
including the allow-listed-but-not-planted row the folded-together panel
must not drop), and `build_status` (including its
`state_dir` override actually reaching `garden.py`/`merge_allowlist.py`/
`overnight.py`, not just `state.py`'s own db path, and that a newer
manual-`tend` log does not hide the concurrent `overnight` run's in-flight
repos or batch bar) — plus `run_server`'s
loopback-only enforcement, without ever covering its `http.server` layer
directly (mirroring how `test_dispatch.py` mocks rather than invokes the
real `claude` subprocess call); `tests/test_repo_lock.py` covers
`lock_file_path`'s naming convention and the `repo_lock` context manager's
exclusivity and release-on-exit (normal and exception) using real
`fcntl.flock` calls against a tmp dir, not a mock, since the whole point is
proving the OS-level exclusion actually holds. None of the automated
tests hit the network or a real repo, or invoke a real `claude` process —
see [Manual/end-to-end verification](#manualend-to-end-verification) for
that.

## Manual/end-to-end verification

Because the whole point of this tool is dispatching a real Claude Code
run against a real repo, the automated suite deliberately can't cover the
full path end to end. Before trusting a change to `dispatch.py` or the
prompt template, run a real report-only pass against a low-stakes repo you
have access to and confirm three things:

1. `gardener align --repo <owner/repo>` exits 0 and prints a gap checklist
   ending in a `GARDENER_SUMMARY:` line.
2. `git -C <the cached clone> status --porcelain` is empty and `git log`
   shows no new commits — report mode must not have touched the clone.
3. `gh repo view <owner/repo> --json pushedAt` is unchanged from before the
   run, and `gh pr list` / `gh issue list` show nothing new — report mode
   must not have touched the real repo on GitHub either.

This exact sequence is what verified gardener's first working version
against `dmccoystephenson/create-dev-loop`.

**`gardener overnight`**: since it dispatches `tend` in-process per repo,
its own verification is the same as `tend`'s above, repeated per repo in
the garden, plus three things specific to the batch layer itself: (1) run
with a small `--hours` (e.g. `0.1`-`0.2`) against a garden of 1-2 low-stakes
repos and confirm it dispatches at least the first repo and stops well
short of running indefinitely; (2) run it twice in a row against a garden
too big for one budget window and confirm the second run tends a
*different* repo than the first (the resume cursor advanced, not reset);
(3) confirm exactly one additional summary notification fires per
invocation, on top of each repo's own per-repo notification. See [Project
Status](PROJECT_STATUS.md) for the actual run this verified against.

**`--strategy issue-count`/`random` specifically** are pure orchestration
changes (no change to `dispatch.py`, `dev_loop.py`, or a prompt template),
so this repo's manual-real-dispatch gate (see "Testing changes" in
`CLAUDE.md`) doesn't apply — but a small real check is still worth doing before relying
on either in an unattended run: `--strategy issue-count --hours 0.1`
against 2-3 low-stakes garden repos with varying real open-issue counts,
confirming the higher-count repo is attempted first and the stderr log's
`fetching open-issue counts...` line shows real `gh` calls succeeding; and
`--strategy random --hours 0.1`, run twice in a row, confirming the second
run's attempted repo differs from the first's (the name-keyed cursor
correctly avoided repeating it) rather than either strategy's automated
coverage (fully mocked `gh`/deterministic seeded shuffle) standing in for
this on its own.

**`--concurrency > 1` specifically** has now had real, unattended
end-to-end exercise on the Android/UserLand device: the `gardener-overnight`
devsrv service ran `--hours 6 --concurrency 3` against the then-15-repo
garden on 2026-07-25, dispatching all 15 in batches of 3 (11 PRs opened, 4
errored, one Discord summary). Concurrent dispatch itself held up — no repo's
recorded `state.Run` or notification was swapped with another's (the
failure mode the old `redirect_stdout`-based capture would have been
vulnerable to — see [issue
#15](https://github.com/dmccoystephenson/gardener/issues/15)). What
remains unverified is only the *upper bound*: how far concurrency can be
raised on this device before real CPU/RAM contention starts degrading
dispatches, which is why the default is a conservative `2` rather than
the `3` that run happened to use.

**Alerting**: `DiscordNotifier` is covered by mocked unit tests (see
above) rather than a real Discord send in the automated suite — same
reasoning as the rest of this section, a real send is an environment
check, not something the test suite should depend on. To verify it for
real once a webhook is configured (see
[Alerting (optional)](../README.md#alerting-optional)), run:

```bash
python3 -c "
from gardener.notify import DiscordNotifier, Level
DiscordNotifier().notify('gardener: manual test', 'if you see this in Discord, alerting works', Level.SUCCESS)
"
```

and confirm the embed shows up in the configured channel with a green
(`3066993`) color bar. Use a webhook pointed at a private/test channel
for this, not a shared production alerting channel.
