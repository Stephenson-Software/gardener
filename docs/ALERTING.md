# Alerting design

See [Alerting (optional)](../README.md#alerting-optional) in the README for
how to configure a webhook. This document covers the implementation.

`gardener/notify.py` defines a small `Notifier` abstraction so alerting
isn't hardcoded to Discord — `Notifier` is an ABC with one method,
`notify(title, message, level)`, where `level` is a `Level` enum
(`INFO`/`SUCCESS`/`WARNING`/`ERROR`) that each concrete notifier maps to
its own presentation.

- **`DiscordNotifier`** — the first/primary implementation. Posts a
  Discord embed via a webhook URL using stdlib `urllib.request` only (no
  `requests` dependency — see [Architecture](ARCHITECTURE.md)). Uses a
  consistent Discord embed shape: title/description/color, same "no webhook configured →
  log and return, never fail the caller" behavior, and the same Discord
  embed colors for success (`3066993`, green) and error (`15158332`,
  red); the warning color (`16776960`, yellow) is used for an
  "expiring soon" style alert. **Never raises** — a bad webhook,
  a network error, or Discord being down must never be able to break the
  actual `gardener align` run it's reporting on; every failure path is
  caught and logged to stderr instead.
- **`NullNotifier`** — a clean no-op, returned automatically when no
  webhook is configured (see [Alerting (optional)](../README.md#alerting-optional)).
  "Nothing configured" is a normal state, not an error path callers need
  to special-case.
- **`CompositeNotifier`** — fans one notification out to zero-to-many
  concrete notifiers, for anyone who wants to register more than one
  destination later without touching any call site.

`cli.py` wires this in with `_notify_run(run)`, called via a small
`_record_and_notify(run, db_path)` wrapper right after each place
`state.record_run(...)` is called in `cmd_align`/`cmd_tend`. Both the
record and notify steps are individually failure-isolated — a
`state.record_run` failure (e.g. a locked/corrupt sqlite file) is caught
and logged rather than crashing the run whose successful dispatch it was
merely trying to persist, the same "must never break the run it reports
on" posture `_notify_run` itself already had for a bad webhook.
`_notify_run` owns the only alerting *business logic* in the codebase — turning a
recorded `state.Run`'s `outcome`/`mode` into a severity — deliberately
kept out of `notify.py` itself, which only knows how to present an
already-decided `(title, message, level)`:

- `outcome == "error"` → always `Level.ERROR`, regardless of mode. This is
  the case most worth getting right (an error is the easiest outcome to
  silently miss without an alert), so it's checked first and
  unconditionally.
- `mode == "report"` (report-only, no mutation possible — see
  [Safety model](SAFETY.md)) → `Level.INFO`.
- Anything else (`--implement`, `--file-issue`, or any future mode that
  also authorizes a mutation) → `Level.WARNING`, so a run that actually
  branched/committed/opened a PR or issue stands out from a routine
  report-only run rather than blending in with it. This check is written
  as an `else`, not an explicit list of mode names, specifically so a
  future mode never has to be added to `notify.py` or `_notify_run` by
  hand to be covered — it falls into the mutation branch automatically
  unless it's literally `"report"`.
