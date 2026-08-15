# The dashboard

Covers the headline session panel, the garden view — the panel the
dashboard is mostly *for* — and what the page does when it can no longer
reach the server behind it.

## The Latest session panel

The page's headline panel — runs, cost, errors, in flight — is scoped to
one **session**: the newest run in the history plus every run contiguous
with it, where "contiguous" means no quiet gap longer than
`state.SESSION_GAP_SECONDS` (6 hours). `state.session_stats()` walks the
history newest-first and stops at the first such gap.

Six hours is picked from what the history actually looks like rather than
rounded off: inside one `overnight` batch the gap between two recorded runs
is at most a single dispatch, well under an hour, while the gap between one
night's run and the next is most of a waking day. So a session is one
night's rotation, or one manual `tend`, and the panel's heading states the
window it is showing (`since 23:04`, dated when the session started on a
previous day) rather than leaving it implied.

This panel used to be headed **Tonight** over the most recent
`run_limit` (40) rows of history, which is not a night and does not claim
to be — `state.list_runs()` applies no time predicate at all. With a garden
of ~32 repos one full cycle already fills most of that window, so the panel
routinely straddled two or more nights and reported a previous night's
failures and dollars as this one's. The error count there is the page's
most glanceable failure signal and the first thing read when deciding
whether an overnight run went badly, so it is the one number that most
needed a window it could actually name.

The **Recent runs** table further down keeps the row window — "the last 40
runs" is exactly what that table claims to be, so no rescoping is owed
there.

## The garden view

`gardener dashboard` (see [Usage](USAGE.md)) renders a garden panel over
`dashboard.build_garden_rows()`'s joined view of the garden list, the merge
allow-list, and each repo's all-time run history.

The dashboard's garden panel used to be two panels: one listing the garden,
one listing the merge allow-list, both as bare repo pills. Once both lists
were opted in garden-wide that was the same ~32 repo names printed twice,
and neither answered the question actually worth asking — *how is each repo
doing?*

They're now one panel, built by `dashboard.build_garden_rows()`: one row per
repo opted in to either list, joined with that repo's all-time run history
from `state.repo_stats()`. Merge-allow-list membership is a column
(`can_merge`), not a second list. A repo on the allow-list but *not* in the
garden still gets a row, flagged — that mismatch means something is
permitted to merge that `overnight` will never dispatch, which is exactly
the kind of thing a list of names printed twice was hiding.

The panel renders those rows two ways, toggled in the page (the choice is
remembered in `localStorage`):

- **Table** — repo, health, tends, errors, last tended, cost, merge. Every
  column header sorts; sorting Health ascending puts the repos that need
  attention first. Each header is a real `<button>`, so the sort is
  reachable by keyboard and not only by mouse, and the active column
  carries both `aria-sort` and a `▲`/`▼` caret — repeat-activating a column
  toggles the direction, and without the caret the inverse order was
  indistinguishable from the one asked for.
- **Plot** — each repo drawn as a plant, in SVG, generated in the page from
  the same row. This is the "look at the garden" view rather than the "read
  the garden" one:

  | Plant | Data behind it |
  |---|---|
  | Stem height, leaf count | All-time successful dispatches (`successes`) — every non-`error` outcome in `state.SUCCESS_OUTCOMES`, so an `align` run or a dev-loop bootstrap grows the plant too, not just a `tend` |
  | Leaf colour, how far the leaves droop | Days since `last_success` — thriving (<2d), steady (<5d), dry (<10d), wilting beyond that |
  | Short brown stem with a single drooping leaf | Runs recorded but none of them succeeded — the leaf count starts at one, so there is no leafless state |
  | Seed in the ground | Never dispatched — sitting in bare soil, or in the pot below when the repo isn't in the garden |
  | Blossom | On the merge allow-list, i.e. allowed to merge its own PRs |
  | Brown leaf litter on the soil | `error` runs, up to six |
  | Width of the soil mound | Dollars spent tending that repo |
  | Terracotta pot instead of soil | Allow-listed but not in the garden |
  | Pulsing glow | Being tended right now (from the same best-effort log parse the "Currently tending" panel uses) |

  Each plant is a button. Tapping or activating one opens a detail card
  under the plot with that repo's full `owner/name`, health, tends out of
  total runs, errors, last successful tend, **last attempt and its
  outcome**, cost, merge eligibility and whether it is actually in the
  garden. The last-attempt pair (`last_run`/`last_outcome`) appears
  nowhere else on the page, and "the most recent attempt errored" is a
  different fact from "the last success was three days ago". The card is
  the only way any of this is reachable on a touch device: the equivalent
  `title` tooltip is kept for pointer users, but a hover is the one
  interaction a phone cannot perform, and the plot is the view the page
  defaults to.

  Each plant's lean, leaf jitter, grass tufts and flower colour are
  decoration, deterministic per repo name (an FNV-1a hash seeding a small
  LCG, never `Math.random()`), so a plant keeps its shape between refreshes
  and only changes when its data does. The plot is re-rendered only when a
  signature of the drawn values changes, so the 4 s poll doesn't restart the
  in-flight glow animation every cycle.

  Growth is drawn from the *whole* run history rather than the `run_limit`
  window the Recent runs table uses — a plant's size means "how much tending
  this repo has had", not "how recently it appeared in the log tail". That's
  why `state.repo_stats()` exists as its own aggregate query instead of the
  dashboard folding `list_runs()` in Python.

## When the page stops being live

Every panel on the page renders whatever the last successful poll
returned, so a failed poll that only changed the header caption left a
dead server looking like a healthy dashboard — the failure mode most
likely to occur during an unattended overnight run (the server
OOM-killed, the phone off the network) and the hardest to spot.

A poll now fails loudly, and the four ways it can fail are told apart
rather than collapsed into one caption:

| Failure | Reason shown |
|---|---|
| The request never completed | `fetch failed` |
| A non-2xx response (`res.ok`, checked before the body is parsed at all, so a 500 is detected as a 500 rather than as "the error body didn't parse") | `server returned 500` |
| A 2xx whose body doesn't parse | `bad response body` |
| A payload that throws while being rendered | `render failed` |

Any of them marks the whole page stale: the content below the header is
desaturated, and the heartbeat caption is replaced with the *age* of the
snapshot on screen (from the payload's own `generated_at`) plus the
reason and a count of consecutive failures. Desaturation rather than
heavy dimming is the deliberate choice — when the server has died
mid-overnight, the stale snapshot is the only data there is, so it has to
stay readable while still being unmistakably not live.

The page is marked fresh only once every panel has actually rendered the
new snapshot, never on receipt of it. Polls don't overlap either: a slow
request against a restarting server could otherwise resolve *after* a
later successful one and re-mark a live page stale. The first successful
poll clears all of it, and the existing `visibilitychange` listener is
still the fast path back.
