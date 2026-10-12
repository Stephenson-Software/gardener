# Verification record: the weekly task, self-update against GitHub, and phone taps

Three checks that had only been run in stand-ins until now:
- the weekly live-test task, launched by Windows Task Scheduler itself;
- `gardener update` against the real GitHub origin, not a local bare repo;
- the dashboard and live view driven by touch on a phone profile, rather
  than only rendered.

- **Code verified:** gardener 0.4.7 (`ce40949`).
- **Date:** 2026-10-11.
- **Environment:** WSL2 (Ubuntu) under Windows; Claude Code with the
  operator's subscription; Orket 0.8.8 with Ollama for the Orket tier.

## 1. The weekly task, as Task Scheduler launches it

The task `\Gardener\Gardener Live Tests` was started with
`schtasks /Run`. It ran its real command line:
`wsl.exe -d Ubuntu -u root -- bash -lc /root/.config/gardener/run-live-tests-weekly.sh`.
That box-local wrapper sets the sandbox repositories and the Orket
environment, then hands off to `bin/run-live-tests.sh`. The task had never
run before; its last result was 267011, "has not run".

| Check | Result |
|---|---|
| Windows → WSL launch | the wrapper started and handed off to `run-live-tests.sh` within 20 s |
| Modules run | `test_live_claude_code`, `test_live_write_modes`, `test_live_merge_and_overnight`, `test_live_orket` |
| Outcome | `Ran 20 tests in 1967.431s` — `OK` (about 33 minutes) |
| Task Scheduler's record | `Last Result: 0` |
| Log | `~/.local/state/gardener/logs/live-tests/live-tests-20261011-193145.log` |
| Test skills | the two temporary `*-sandbox*-dev-loop` skills were installed during the run and removed after it (0 left in `~/.claude/commands`) |
| Alert | the script sends one "gardener live tests: passed" notification; receipt in Discord was not checked from the box |

All 20 passed: the read-only report; `--file-issue`; `--implement`; `tend`
without merge; the merge gate both ways plus the tool-scoping probe; a
concurrent `overnight` carrying into the next cycle; and the 12 Orket
tests.

## 2. `gardener update` against the real origin

A fresh clone of `https://github.com/Stephenson-Software/gardener.git` was
reset to `v0.4.6`. That clone's own `gardener update` was then run, with
`PYTHONPATH` pointing at the clone and the working directory outside it.

| Case | Output | Exit |
|---|---|---|
| Behind origin (0.4.6) | `updated 49e8be46b -> ce409493e`; `__version__` now `0.4.7` | 0 |
| Run again | `already up to date (ce409493e)` | 0 |
| Uncommitted change | `has uncommitted local changes — skipping self-update` | 0 |
| Detached HEAD | `is on a detached HEAD — skipping self-update` | 0 |
| Local commit not on origin | `has diverged from origin/main … not a fast-forward, skipping self-update` | 0 |
| No upstream | `branch 'main' has no upstream on origin — skipping self-update` | 0 |
| Origin unreachable (nonexistent repository) | `git fetch failed: remote: Repository not found.` | 1 |

The checkout was clean at the end. This matches the offline suite in
[integration-selfupdate](integration-selfupdate.md), now over HTTPS against
GitHub.

## 3. Phone taps on the dashboard and live view

`gardener dashboard` was served against the box's real state: 184 repos
and the real run history. Headless Chromium with Playwright's `iPhone 13`
profile (390 px wide, touch enabled) drove it with `tap()`. Every step was
checked for horizontal overflow (`scrollWidth <= clientWidth`).

| Check | Result |
|---|---|
| Plot renders | 184 plants |
| Plant tap target | 78×174 px |
| Tap a plant | its detail card opens, naming the repo |
| Wait 16 s (background refreshes) | the card stays open |
| "Show runs" chip | the runs list filters to that repo; "show all repos" clears it |
| Close button; tapping the same plant twice | the card closes |
| Legend `<details>` | opens on tap |
| Table tab | table shown, plot hidden |
| Sort-direction chip; sort select (Cost) | the order reverses; the order changes |
| Filter "gardener" | 3 rows |
| Reload | the table view is remembered |
| `/live` | "Finished", started 1:00 AM, last activity 1:07 AM (the 0.4.7 timing) |
| Horizontal overflow | none at any step (390/390) |
| Console errors | one: a 404 for `/favicon.ico`, which the browser requests on its own. Neither page declares an icon. Cosmetic |

21 of 22 checks passed; the one failure is the favicon 404 above.
