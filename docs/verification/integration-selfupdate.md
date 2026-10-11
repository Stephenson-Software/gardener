# Verification record: self-update end to end

Self-update had unit tests over a mocked `git`, but had never been checked
end to end in a repeatable way. `integration/test_offline_selfupdate.py`
now runs the real CLI and real `git` against a local origin.

- **Code verified:** gardener 0.4.4 plus branch
  `feature/integration-selfupdate`.
- **Date:** 2026-10-11.
- **Environment:** WSL2 (Linux 5.10), Python 3.11.15 and 3.10.20, git
  2.25.1.

## Results

No bugs were found. All 11 tests pass:

| Area | Checked |
|---|---|
| `update --check` | reports `update available: <old> -> <new>`; HEAD unchanged |
| `update` | `updated <old> -> <new>`; clean tree; a new process imports the new `__version__`; a second run says `already up to date` |
| Untracked files | don't block the update and are kept |
| Tracked local changes | skipped, exit 0; the edit is kept |
| Diverged branch | skipped as `not a fast-forward`; the local commit is kept |
| Detached HEAD, branch without an upstream | skipped, exit 0 |
| Unreachable origin | `git fetch failed`, exit 1 |
| `overnight` | the update is logged with no alert, then the run continues. A skip sends one `self-update SKIPPED — tending with stale code` alert and the run still continues. `--no-self-update` does nothing |

As documented in USAGE.md, the running process keeps the code it already
imported. Every module the `overnight` path uses is imported at startup, so
an update takes effect from the next run, with no mix of versions within
one run.

## Deliberate breaks

| Break | Caught by |
|---|---|
| `self_update` ignores a dirty tree | `test_tracked_local_changes_are_never_overwritten`, `test_a_skipped_update_alerts_and_the_run_carries_on` |
| a routine update sends an alert | `test_overnight_updates_silently_and_carries_on` |

## Suites

The integration suite now has 72 tests, 15 of them live and skipped. It
passes on Python 3.11 and 3.10.
