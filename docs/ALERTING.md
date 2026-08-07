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
  consistent embed shape: a title, description, and color field, plus a
  "no webhook configured → log and return, never fail the caller"
  behavior. Colors: success is `3066993` (green), error is `15158332`
  (red), and warning is `16776960` (yellow, used for an "expiring soon"
  style alert). **Never raises** — a bad webhook,
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

## Device provenance

An alert says what happened; without provenance it never says *where*.
That is fine with one deployment and stops being fine the moment the same
garden is tended from more than one device — which
[Overnight](OVERNIGHT.md) documents wiring recipes for. It matters most
for the alerts that are about the *box itself* rather than about a repo: a
"self-update SKIPPED — tending with stale code" warning is only actionable
if you know which checkout to go fix.

`load_device_name()` resolves it with the same precedence
`load_webhook_url()` uses, and for the same reason — the contexts that
most need provenance (cron, systemd, `devsrv`) are the ones where
exporting an env var per invocation isn't practical:

1. `GARDENER_DEVICE_NAME` env var
2. `GARDENER_DEVICE_NAME=...` in `notify.env`
3. `socket.gethostname()`
4. `unknown-device` (`UNKNOWN_DEVICE_NAME`) if the hostname is
   unresolvable or blank

It never raises and never returns an empty string — it runs on the
alerting path, which must not break the run it reports on. A blank value
at any level falls through to the next.

`DiscordNotifier` renders it as the embed's **footer**, and resolves it
**once per notifier** rather than per alert (step 2 is a filesystem read,
and `overnight` alerts once per repo). The footer was chosen over the
title because titles are already long and are what the operator scans;
"which box was this" is what you look for only once an alert has your
attention.

The placement is the load-bearing part: provenance is applied in
`notify.py` at *presentation* time, not threaded through each call site,
so every current and future alert carries it by construction rather than
by everyone remembering to. This is the same `notify.py` owns-*how*,
callers own-*when/why* split described above — the device is a property of
the sender, not of any particular outcome, so it belongs on the
presentation side. `CompositeNotifier` needs no change for this: it only
fans out `(title, message, level)`, and each concrete notifier renders its
own provenance in whatever form suits it.

`cli.py` wires this in with `_notify_run(run)`, called via a small
`_record_and_notify(run, db_path)` wrapper right after each place
`state.record_run(...)` is called in `cmd_align`/`cmd_tend`. Both the
record and notify steps are individually failure-isolated — a
`state.record_run` failure (e.g. a locked/corrupt sqlite file) is caught
and logged rather than crashing the run whose successful dispatch it was
merely trying to persist, the same "must never break the run it reports
on" posture `_notify_run` itself already had for a bad webhook.
`_notify_run` owns the per-run alerting *business logic* — turning a
recorded `state.Run`'s `outcome`/`mode` into a severity — deliberately
kept out of `notify.py` itself, which only knows how to present an
already-decided `(title, message, level)`. (`_notify_self_update`, below,
is the one other place that decides a severity, and follows the same
split.)

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

## Self-update alerts

`gardener overnight` fast-forwards gardener's own checkout before tending
anything (see [Overnight](OVERNIGHT.md) and `selfupdate.py`'s module
docstring). That step is deliberately incapable of aborting the run: every
failure mode degrades to a skip. The cost of that tolerance is that a box
can go on tending the garden with stale code indefinitely, and the only
evidence is a line on stderr that, on an unattended device, nobody reads.

`cli.py`'s `_notify_self_update` closes that gap, mapping the
`UpdateStatus` to a severity via `_SELF_UPDATE_ALERT_LEVELS`:

- Every `SKIPPED_*` status (`NO_GIT`, `DIRTY`, `DETACHED`, `NO_UPSTREAM`,
  `NOT_FAST_FORWARD`) → `Level.WARNING`, titled "self-update SKIPPED —
  tending with stale code". They differ in cause but not in consequence:
  this run is using code that may be behind `origin`. When the result
  carries both SHAs (as `NOT_FAST_FORWARD` does), the message includes
  them, so the alert is actionable without logging into the box.
- `ERROR`, plus any exception that escapes `self_update` itself →
  `Level.ERROR`, titled "self-update FAILED".
- `UP_TO_DATE`/`UPDATED`/`UPDATE_AVAILABLE` → **no alert at all**. This
  silence is the point: a nightly notification for the routine path is
  noise, and noise gets muted, which would take the warnings above with
  it.

Scoped to `overnight` on purpose — `gardener update` typed by hand already
prints its outcome to someone watching, so alerting there would be noise
rather than visibility.

`SKIPPED_NO_GIT` is worth calling out: unlike the others it's a permanent
property of a non-editable install, so it will fire every night until the
deployment is changed to an editable checkout. That's intentional (an
install that can never self-update is worth knowing about) but it is the
one status likely to want suppressing if a wheel install is deliberate.
