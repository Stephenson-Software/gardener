# Testing

See also [CONTRIBUTING.md](../CONTRIBUTING.md) for how to propose a change
and branch naming.

## Automated Tests

Linux/macOS:

    PYTHONPATH=. python3 -m unittest discover -s tests -v

Windows (PowerShell):

    $env:PYTHONPATH = "."; python -m unittest discover -s tests -v

A passing run ends with `OK`. `tests/test_dispatch.py` mocks
`subprocess.run` and never actually invokes `claude`. It covers both halves
of the `bypassPermissions` posture, which are separate things: that no
entry in `MODE_SPECS` configures a forbidden or unrecognized permission
mode (the *table*), and that `_build_invocation` actually raises
`DispatchError` when handed a `ModeSpec` that does (the *check*). The
second is not implied by the first — delete the two `raise DispatchError`
lines and every table-level test still passes, since they assert about the
data the check defends rather than about the check firing. That check is
reachable in practice, not just in theory: `_build_invocation` accepts a
`mode_spec` override and `tend_mode_spec()` builds one at runtime rather
than reading it from the table. A fourth test pins that a *valid* override
still builds, so the guard can't be over-broad. Coverage also includes the
device-wide failure classification and retry policy (each of
`looks_like_auth_failure`/`looks_like_usage_limit`/`looks_like_network_failure`
matched against the *verbatim* text of a real recorded failure rather than
paraphrased, that `is_device_global_failure` covers all three while ordinary
build/test failures trip none of them, that the bare `could not determine
default branch` wrapper the network class always arrives inside is
deliberately *not* enough to classify on its own, that the retry stops as
soon as auth recovers and gives up once the backoff is exhausted, and that a
usage limit is flagged blocked but never retried), always with `sleep_fn`
injected so no test actually sleeps; `tests/test_state.py`
also pins the `GARDENER_STATE_DIR` contract across every module that
resolves it — `state.py`, `garden.py`, `merge_allowlist.py`,
`overnight.py`, `notify.py`, `run_log.py`, `repo_lock.py`, and
`sessions.py` each carry their own private copy of that resolution, so one
test asserts all nine helpers land under the override and another that they all fall back to
`~/.local/state/gardener`, filenames asserted verbatim so a rename that
would orphan a deployed box's on-disk state is caught too. It otherwise
uses a real sqlite3 file in a tmp dir (including `daily_stats`' per-day
rollup — grouping, newest-first ordering, the `days` limit, and null
`cost_usd`/`duration_ms` not poisoning the sums; `session_stats`'
`errors_detail` carrying the session's error rows alongside the count;
and `_connect`'s read path, whose assertions are deliberately scoped to
what was *measured* rather than to the lock contention issue #121
originally claimed — with the table already present `CREATE TABLE IF NOT
EXISTS` is a no-op that takes no write lock, so the one case the
parameter actually changes is a db with no `runs` table yet while another
connection holds RESERVED; `repo_stats`' all-time
per-repo aggregates — that every value in `state.KNOWN_OUTCOMES` is
classified as either a success or an error rather than falling silently
between the two, that a successful `align --implement`/`--file-issue` run
and a `created_incomplete` bootstrap all count as successes, that a later
`error` never overwrites `last_success`, and that `last_outcome` breaks a
same-second timestamp tie by row id). `session_stats` — the
newest-contiguous-burst window the dashboard's headline panel is scoped to
— is covered there too, on the boundary rather than the arithmetic: that a
previous night's runs are excluded, that the gap threshold is exact at the
edge and injectable, that unbroken sub-threshold activity is still capped
at the maximum span (measured back from the newest run, so a chain of runs
can't grow into a multi-day window), that a session spans every repo rather
than one, and that an unreadable timestamp ends the session instead of
silently folding two nights together. `tests/test_cli.py` covers argument
parsing (including `repo_arg`, the `type=` callable that rejects a
malformed `--repo` as a usage error at parse time on `align`/`tend`/
`allowlist add`/`garden add`, while `allowlist remove`/`garden remove`/
`status --repo` deliberately still accept one), the coupling between the
parser and `docs/USAGE.md` (every long flag argparse actually *shows* must
appear in the command reference — flags declared `help=argparse.SUPPRESS`,
i.e. the path/state overrides and the `--random-seed` test hook, are
exempt, and a second test asserts those stay suppressed so un-hiding one
tightens the requirement instead of silently widening the exemption),
`clone_or_refresh_target_repo`'s pre-flight guards (a malformed `--repo`
rejected before any subprocess runs, a missing `gh` reported as such rather
than as a confusing subprocess failure, a failed clone surfacing `gh`'s
stderr, and — the one that matters most — a cache directory whose `origin`
doesn't match the requested repo being *refused* rather than re-pointed, so
a dispatch can't be aimed at the wrong working tree under the right repo's
name), `_default_branch_name`'s success and both failure shapes (a nonzero
exit, and `gh` exiting 0 having printed nothing — whose wrapper wording is
pinned here because `dispatch.py`'s device-global classifier deliberately
does *not* match it), `current_branch`'s `main` fallback on empty output,
prompt templating, the
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
so the no-webhook case is what's actually asserted), the denial reporting
both dispatch paths print (`format_denial`/`denial_report_lines` directly —
that an entry which isn't the expected `{"tool_name", "tool_input"}` dict
degrades to `str()` rather than raising, since that structure comes from
`claude`'s output and gardener doesn't own it; that duplicates collapse and
the overflow becomes a count; and that newlines are collapsed, asserted by
feeding a denied command containing a verbatim `gardener: tending ...`
marker through `dashboard.parse_in_progress` and getting nothing back —
plus, through the real `cmd_align`/`_dispatch_tend`, that the denials are
printed *before* the NOTE whose "see denials above" refers to them, which
is the whole of issue #99), and its
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
per-repo error still does not abort the batch or hold the cursor —
`_blocking_reason`'s three named failure-class branches and its generic
fallback, and `_first_blocked_index`'s cursor-advance distance, are also
pinned directly, independent of `cmd_overnight`, in `TestBlockingReason`/
`TestFirstBlockedIndex`) and
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
without ever making a real HTTP call, and covers `load_device_name`'s
precedence (env var, then `notify.env`, then `socket.gethostname()`, then
the `unknown-device` sentinel) with `socket.gethostname` patched, including
the blank-value fall-through at each level and the never-raises
degradations for an unresolvable hostname and an unreadable config file;
two further tests assert that every `Level` carries the device footer
without disturbing the embed's existing fields, and that the device is
resolved once per notifier rather than on every alert. `TestDiscordNotifier`
scopes `GARDENER_STATE_DIR` to a tmp dir for every test in the class,
since constructing a notifier resolves a device name and would otherwise
read the operator's real `notify.env`; one test guards that redirection by
asserting a device name that exists only in the tmp dir; `tests/test_garden.py` and
`tests/test_overnight.py` cover the garden JSON list and `overnight.py`'s
pure rotation/batching/budget/resume-cursor/outcome-classification logic
with real files in a tmp dir, including `order_by_issue_count` (pure sort
over an already-fetched count mapping), `random_order` (injectable
`random.Random`), and `resume_order`/`next_attempted` (the name-keyed
cursor's cycle-completion and reset logic); `tests/test_conventions.py`
covers `ConventionsSource.verify_complete()`'s missing-doc detection and
`ensure_conventions()`'s clone/fetch-reset/no-refresh branches, with
`_run_git`/`subprocess.run` mocked so no real `git` process ever runs, and
pins the `GARDENER_CACHE_DIR` contract the same way `test_state.py` pins
the state-dir one — `conventions.default_cache_dir` and
`cli.default_repos_cache_dir` each carry their own private copy of that
resolution, so one test asserts both land under the override and another
that both fall back to `~/.cache/gardener`, the `conventions`/`repos`
suffixes asserted verbatim so a rename that would orphan a deployed box's
on-disk cache is caught too;
`tests/test_transcript.py` covers the encoding rule
(against the two real, empirically-confirmed examples in `transcript.py`'s
module docstring, not invented ones), the transcript-file-discovery polling
loop (real files in a tmp dir, but `time_fn`/`sleep_fn` always injected so
nothing ever sleeps for a real second), and the pretty-printer's
line-parsing logic (synthetic JSONL fixtures covering `tool_use`/`text`/
`tool_result`, malformed JSON, and blank lines). It also covers the
degrade-don't-raise paths, which matter because a transcript is a live file
another process is appending to in a format gardener doesn't own: a
`*.jsonl` deleted between `find_new_transcript`'s glob and its stat is
skipped rather than fatal, a `content` value that is neither a list nor a
string yields `None` instead of being iterated, a malformed block alongside
good ones doesn't lose the good ones, `_tool_result_text` falls back to
`str(...)` for a shape that is neither, and `print_transcript` exits `0` on
both `BrokenPipeError` (`tail-transcript ... | head`) and
`KeyboardInterrupt` (Ctrl-C out of `-f`) rather than surfacing a traceback
for a normal way of using it. `tests/test_notify.py` likewise covers a
Discord 4xx returned as an ordinary response — distinct from `urlopen`
raising `HTTPError`, and reached by a different branch — and a `notify.env`
that exists but can't be read; `tests/test_dashboard.py`
covers the dashboard's pure log-parsing and status-assembly functions —
`find_active_log`, `find_active_logs` (the recency window and its exact
boundary under an injected clock, its newest-log fallback, and that the
window outlasts a silent `tend` dispatch), `tail_lines`,
`parse_in_progress` (including the `finished tending` marker clearing a
repo with no notify line present at all, and a repo restarted after
finishing reading as in flight again),
`parse_batch_progress`, `find_free_port`, `_status_query` (the
`/api/status` query string — a bad `limit=` degrades to the default
rather than raising, since this runs inside the poll path), the
`/api/status` branch returning a real 500 with a JSON body when
`build_status` raises rather than closing the socket having written zero
bytes, `find_active_log` skipping a log pruned between its glob and its
stat (patching `is_file` alongside `stat` for the reason
`TestFindActiveLogs` spells out — patching `stat` alone passes via
`is_file`'s own `OSError` swallow without reaching the branch),
`build_garden_rows` (the
garden/allow-list/history join behind [the garden view](DASHBOARD.md),
including the allow-listed-but-not-planted row the folded-together panel
must not drop), and `build_status` (including its
`state_dir` override actually reaching `garden.py`/`merge_allowlist.py`/
`overnight.py`, not just `state.py`'s own db path, that a newer
manual-`tend` log does not hide the concurrent `overnight` run's in-flight
repos or batch bar, and that its stat tiles are scoped to the session
rather than to the `run_limit` row window the Recent runs table keeps). The page's in-page JavaScript has no test runner here
— stdlib-only Python means no JS toolchain — so `TestPageHtmlInvariants`
asserts it at the only level the Python side can see: the emitted source
text of `PAGE_HTML`. Those are deliberately narrow "this mechanism is still
present" checks over the properties that are load-bearing and silently
regressible — `esc` escaping quotes because it builds an attribute value,
the poll loop running through the visibility-gated wrapper, a plant being a
real `<button>` with an accessible name, the detail card rendering
`last_run`/`last_outcome`, the plot and card listeners being delegated
because both are replaced wholesale, the tablist's `aria-controls`/
`role="tabpanel"`/roving-`tabindex` wiring, focus surviving a rebuild of
either, the rendered age being part of the plot signature, every sortable
column header being a real `<button>` with the sort listener bound to it
rather than to the `<th>` around it, run summaries being linkified from
the *raw* string rather than the escaped one (escaping first and matching
`#\d+` over the result finds the digits inside numeric character
references, so the real summary "You've hit your session limit" rendered
as `You&#39;ve` with an anchor through the middle of the entity — caught
by rendering the page, not by any assertion, which is why the shape is
pinned here afterwards), the sorted column and direction being
written back into the `<thead>` from the same state the body is sorted
from, the session panel naming both ends of the window it shows (and
degrading to no caption at all rather than to a dangling preposition), and
each of the four poll-failure reasons marking the page stale.
`TestGardenSortOnNarrowViewports` reads the same emitted source for the
second sort control the phone layout needs, since the header cells that
carry the sort are hidden there: that the rule showing it and the rule
hiding the `<thead>` live in one media block (split across two
breakpoints, some range of widths would show neither, which is the bug
itself), that it renders inside the table view rather than the panel's
shared toolbar, that every control changes the order through the single
`setGardenSort` — asserted by counting the assignments to `gardenSort`,
so a third writer fails the test — that both are re-rendered from one
read of it, that the select's options are derived from the header cells
rather than listed a second time, and that its direction toggle names
its direction in words because the caret is `aria-hidden`. Anything about how the
plot *looks* is still verified by rendering it and looking at it, per
CLAUDE.md. `find_active_logs` additionally covers the prune race
(a log deleted between the `glob` and the `stat` is skipped, not fatal) —
that test patches `Path.is_file` as well as `Path.stat`, because `is_file`
calls `stat` internally and swallows `OSError`, so patching `stat` alone
passes without ever reaching the `except OSError` branch. `run_server` is
covered for both of its rejection paths (a non-loopback host and one that
fails to resolve, the latter surfacing as `ValueError` rather than a bare
`socket.gaierror`), for not constructing a server at all when the host is
rejected, and for its serve/shutdown lifecycle with `ThreadingHTTPServer`
mocked — `KeyboardInterrupt` exits cleanly, `server_close()` runs even when
`serve_forever()` raises something else, and the `state_dir` reaches
`_DashboardHandler` as a class attribute before serving begins. Its
`http.server` request-handling layer is still deliberately not covered
directly (mirroring how `test_dispatch.py` mocks rather than invokes the
real `claude` subprocess call), which is why `do_GET` and `log_message`
remain the module's only uncovered lines; `tests/test_repo_lock.py` covers
`lock_file_path`'s naming convention and the `repo_lock` context manager's
exclusivity and release-on-exit (normal and exception) using real
`fcntl.flock` calls against a tmp dir, not a mock, since the whole point is
proving the OS-level exclusion actually holds; `tests/test_sessions.py`
covers the session registry the same way — real `flock` calls decide
liveness, so one test proves a held lock reads as running and another that
an unlocked file reads as exited *even when its recorded pid is a live
process*, which is the whole reason liveness isn't a pid check — plus
id/prefix/target resolution (including the ambiguous and no-match errors,
asserted to name the candidates and point at `gardener ps` rather than just
failing), `descendants()` against a synthetic `/proc` tree written into a
tmp dir, and `stop()`'s signal-then-escalate sequence with `os.kill`,
liveness, and the clock all injected so nothing real is ever signalled;
`tests/test_cli.py`'s session-command tests likewise drive `cmd_ps`/
`cmd_stop`/`cmd_kill` against a real sessions directory with
`sessions.stop` itself patched out; `tests/test_selfupdate.py`
covers `self_update`'s every branch (up to date, a real fast-forward,
skipped for a dirty tree/detached HEAD/diverged branch, `--check`'s
update-available report, and a `git` failure/timeout/`OSError` all coming
back as `ERROR` rather than raising). The never-raises guarantee is
asserted against *every* call site rather than one: the suite fails each of
the eight `git` invocations `self_update` makes in turn — once with a
`TimeoutExpired`, once with an `OSError` — and requires an `UpdateResult`
back each time, with a third test asserting the happy path really does
reach all eight so that list can't silently go stale. The two non-raising
failures that aren't skips (`git status` exiting non-zero, which is not the
same as a clean tree despite both printing nothing, and an unresolvable
`origin/<branch>` after a successful fetch) are covered too. All of it runs
through the injectable `run_fn` — never a real `subprocess.run`, `git`, or
network call, which is why `_default_run` itself is the module's one
deliberately uncovered line — plus
`find_repo_root`'s upward filesystem walk against a real (but throwaway,
tmp-dir) directory tree; `tests/test_cli.py` covers `cmd_update` (with
`selfupdate.self_update` mocked) and `cmd_overnight`'s self-update wiring
specifically — called by default, skipped by `--no-self-update`, and
a raising/mocked self-update never aborting the run. None of the automated
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

**Self-update specifically** is also a pure orchestration change (no
`dispatch.py`/`dev_loop.py`/prompt-template involvement), but it's the one
piece of this feature set that does a real `git fetch`/`git merge
--ff-only` against gardener's own checkout, so it's worth a real run once
before trusting it unattended: from a clone that's a few commits behind
`origin`, run `gardener update` and confirm it reports `updated <old> ->
<new>` and `git log` shows the new commits with no local changes lost;
then run it again immediately and confirm it reports already up to date
(no-op the second time); then make an uncommitted tracked-file edit and
confirm a further `gardener update` reports the dirty-tree skip rather
than touching anything. `gardener overnight`'s own default self-update
step is exactly this same call, so this also covers it — no separate
overnight-specific real run is needed for this piece.

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

**`--concurrency 4` on the WSL2 device** was then exercised the same way on
2026-08-09, when an operator asked for a wider nightly run: `gardener
overnight --hours 8 --concurrency 4` against the 61-repo garden. The first
four-wide batch (`Fiefs`, `AlternateAccountFinder`, `Democracy`,
`Dans-Essentials`) completed with all four `ok=True`, and each repo's
recorded `state.Run` summary was confirmed against GitHub to name that
repo's own artifacts — `Fiefs` PR #172 and `AlternateAccountFinder` PR #91
both exist and are merged in their respective repos, `Dans-Essentials` #142
is a real issue there — so the swap/corruption failure mode above did not
appear at four either. Contention was measurable but not degrading: the
batch's dispatches took 482/631/704/936s against 764/770s for a
concurrency-2 batch immediately prior on the same box, i.e. the slowest
dispatch stretched by roughly a fifth while per-repo throughput improved
from about 6.4 to 3.9 minutes. The upper bound is still unmeasured — four
is now a verified floor for this device, not a demonstrated ceiling.

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
(`3066993`) color bar, and that its footer names the device it was sent
from (see [Device provenance](ALERTING.md#device-provenance) — the footer
is the one part of the embed whose value depends on the box you ran this
on, so it's worth reading rather than glancing past). Use a webhook
pointed at a private/test channel for this, not a shared production
alerting channel.
