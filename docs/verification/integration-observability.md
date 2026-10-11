# Verification record: run observability through the real CLI

The integration suite already covered `align`, `tend`, `overnight` and
session control. This adds the features around a run, checked through the
real `gardener` CLI and real servers on 127.0.0.1:
- alerts and usage reporting;
- `status`;
- the dashboard;
- the hub;
- `doctor`.

No gardener code changed.

- **Code verified:** branch `feature/integration-observability`, on gardener
  0.4.3.
- **Date:** 2026-10-11.
- **Environment:** WSL2 (Linux 5.10), Python 3.11.15 and 3.10.20.

## Results

`integration/test_offline_observability.py`: 12 tests, all passing in about
7 s; no hub or dashboard process left afterwards.

| Area | What was checked | Result |
|---|---|---|
| Discord alerts | one alert per run to a stub webhook: repo in the embed, device in the footer, a non-`Python-urllib` User-Agent (Discord's edge 403s the default one); error and success alerts differ in colour; no webhook means no alert and no warning | pass |
| Usage reporting | exactly one `startup` event with gardener's version reaches a stub endpoint; `GARDENER_USAGE_REPORTING_ENABLED=false` sends nothing | pass |
| `status` | lists the repo with both a `report` and an `error` run | pass |
| Dashboard | `/api/status` contains the recorded run; `/` and `/live` answer 200; nothing answers on the machine's non-loopback address | pass |
| Hub | a real `hub serve` with a device token and operator password; the device's `align` arrives under the token's device name; `status --all-devices` reads it back; an unknown token is refused | pass |
| Hub, local first | with the hub down, `align` still succeeds and records the run locally; `hub status` shows it queued; after the hub starts, `hub sync` delivers it; a second sync does not duplicate it | pass |
| `doctor --offline` | clean after a run; an untracked file in a cache clone is flagged by repo and left in place (doctor never repairs) | pass |

## Do the tests bite?

These cover existing behaviour, so each was checked against a deliberate
break:

- **Every alert sent in the success colour:** the colour test failed
  (`an error alert looks like a success`).
- **The dashboard's default host changed to `0.0.0.0`:** the dashboard
  refused to start (`run_server` rejects any non-loopback host because it
  has no authentication), so the test failed before reaching its own
  reachability check. That makes two layers of protection: the startup
  guard, then the test's check that nothing answers on the network address.

The unit suite passes, and the integration suite (60 tests, 15 live ones skipped) passes on Python 3.11 and 3.10.
