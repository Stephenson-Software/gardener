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
night's rotation, or one manual `tend`.

The gap rule alone would chain, though — an `overnight` ending at 03:00 and
a manual `tend` at 08:30 are inside the threshold, and every further run
under it extends the window — so a session is additionally capped at
`state.MAX_SESSION_SPAN_SECONDS` (24 hours), measured back from the newest
run. A panel headed "Latest session" can then never be showing three days,
which is the exact failure it was rescoped to remove, and the walk gains a
ceiling on the rows it reads per poll.

The heading states the window it is showing — `20:45 – 03:36`, dated when
an end falls on a day other than today — rather than leaving it implied.
Both ends, not only the start: a session that finished this morning would
otherwise read as one still running, beside an "in flight" tile counting
something else entirely.

This panel used to be headed **Tonight** over the most recent
`run_limit` (40) rows of history, which is not a night and does not claim
to be — `state.list_runs()` applies no time predicate at all. With a garden
of ~32 repos one full cycle already fills most of that window, so the panel
routinely straddled two or more nights and reported a previous night's
failures and dollars as this one's. The error count there is the page's
most glanceable failure signal and the first thing read when deciding
whether an overnight run went badly, so it is the one number that most
needed a window it could actually name.

The heading also carries how long the session actually spent dispatching.
`overnight` is a time-budget scheduler, so wall-clock is what explains why
only a fraction of the candidates were attempted and which repo consumed
the budget — a number recorded on every run, aggregated per repo, and
until now displayed nowhere. It appears per run in Recent runs, per repo
in the garden table and detail card, and per night in the history panel.

The **Recent runs** table further down keeps the row window — "the last 40
runs" is exactly what that table claims to be, so no rescoping is owed
there. It does now name that window (`last 40 · Aug 12 – Aug 15`) and rule
between calendar days, because a bare `HH:MM` clock on every row made four
days of history read as one morning. `?repo=` and `?limit=` on
`/api/status` narrow it — `state.list_runs()` always accepted both, and
nothing ever passed them.

## The Failures this session panel

Shown only when the session has errors. The error *count* in the headline
panel cannot say what failed, and failures in a real overnight run are
overwhelmingly one systemic cause repeated across many repos — an
exhausted usage limit, a `claude` that left `PATH`, a cached clone stuck
dirty. Rendered as N rows interleaved among the successes, that reads as N
unrelated repo failures rather than as one dead run.

So `state.session_stats()` now returns the session's error rows
(`errors_detail`) alongside the count, collected during the same walk, and
the panel groups identical summaries into `reason ×N` with the affected
repos listed beneath. Nothing new is captured — `outcome` and
`gap_summary` were already columns on `runs`.

## The Per-night history panel

`session_stats` answers "how did tonight go" and `repo_stats` "how is this
repo doing"; neither can answer "is this getting better or worse", so a
night that was a total loss became invisible the moment it stopped being
the newest session. `state.daily_stats()` is one `GROUP BY` over the
existing table — runs, errors, cost and time per calendar day, newest
first. Days are grouped on the raw timestamp prefix, matching how
`state.now_iso()` writes them, so it inherits that function's timezone
rather than introducing handling of its own.

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

A filter box beside the tabs narrows both views by substring over the full
`owner/name`. At 135 rows the plot is 135 tiles labelled only by short
name — and once repos move between orgs those short names collide, so a
label that is ambiguous within the current garden is drawn as the full
`owner/name` instead. The summary line beside it also names how many repos
have **never been tended**, which is routinely the largest single group
and previously appeared nowhere.

- **Table** — repo, health, tends, errors, last tended, time, cost, merge.
  Every column header sorts; sorting Health ascending puts the repos that
  need attention first. Each header is a real `<button>`, so the sort is
  reachable by keyboard and not only by mouse, and the active column
  carries both `aria-sort` and a `▲`/`▼` caret — repeat-activating a column
  toggles the direction, and without the caret the inverse order was
  indistinguishable from the one asked for.

  The narrow-viewport layout turns each row into a card, which means
  `display: block` on the table elements — that strips the implicit
  `table`/`row`/`cell` roles in every engine, so the roles are re-asserted
  explicitly in the markup along with `scope="col"` headers and a
  visually hidden `<caption>`. Both tables are re-rendered only when a
  signature of their contents changes, for the same reason the plot is:
  an unconditional rebuild every four seconds destroys any text selection
  inside them before it can be copied.

  That card layout also hides the whole `<thead>`, and these headers are
  not only labels — they are the sort control — so below 720px the table
  was not sortable at all, on the layout the page is written for. A
  **Sort by** select and a direction toggle therefore render inside the
  table view at exactly those widths, from the same media block that
  hides the header row, sticky at the top of the view's own scroll box.
  Both controls change the order through one `setGardenSort` and are
  re-rendered from one read of `gardenSort`, so the caret, `aria-sort`
  and the select cannot describe different orders; the select's options
  are built from the header cells at load rather than listed a second
  time, so a new column appears in both controls or in neither.
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

  Every colour above is a CSS custom property resolved through the page's
  theme, not a literal in the SVG — half the plot already flipped with
  `prefers-color-scheme` and half didn't, which left the light-mode
  buckets within 0.3 of each other at under 3:1. The naturalistic leaf
  colours are the *illustration*; they are not a severity ramp, so the
  table's status dot takes the page's semantic tokens (`--err`, `--warn`,
  `--accent`) instead, where the escalation has to be unmistakable.

  Plants are ordered by attention rather than by name: in flight first,
  then struggling/wilting/dry, then steady/thriving, then never-tended,
  alphabetically within each band so nothing jumps between polls. The
  payload's own row order stays alphabetical — that is the canonical
  order, and both views treat ordering as a presentation decision. On a
  narrow viewport the plot scrolls inside its own box rather than making
  the page 135 plants tall.

  Each plant is a button. Tapping or activating one opens a detail card
  under the plot with that repo's full `owner/name`, health, tends out of
  total runs, errors, last successful tend, **last attempt and its
  outcome**, time spent, cost, merge eligibility and whether it is
  actually in the garden, plus links out to the repo on GitHub and a
  control that filters the Recent runs table to just that repo — the card
  used to report "17 errors" with no route to any of them. Because one
  shared card serves every plant and sits after the whole plot, opening it
  moves focus into it; it is a labelled `role="region"`, and closing it
  returns focus to the plant that opened it. The last-attempt pair (`last_run`/`last_outcome`) appears
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

A poll now fails loudly, and the ways it can fail are told apart rather
than collapsed into one caption:

| Failure | Reason shown |
|---|---|
| The request never completed | `fetch failed` |
| The request hung past the poll timeout, or no poll has landed in three intervals | `no response` |
| A non-2xx response (`res.ok`, checked before the body is parsed at all, so a 500 is detected as a 500 rather than as "the error body didn't parse") | `server returned 500` |
| A 2xx whose body doesn't parse | `bad response body` |
| A payload that throws while being rendered, including one whose `schema` doesn't match the page's | `render failed` |

Any of them marks the whole page stale: the content below the header is
desaturated, a `⚠ stale` badge appears beside the heartbeat, and the
caption is replaced with the *age* of the snapshot on screen (from the
payload's own `generated_at`) plus the reason and a count of consecutive
failures. Desaturation rather than heavy dimming is the deliberate choice
— when the server has died mid-overnight, the stale snapshot is the only
data there is, so it has to stay readable while still being unmistakably
not live. Desaturation is *not* the only signal, though: it says nothing
to a screen reader, on a monochrome display, or with severe colour vision
deficiency, which is what the badge and the live region below are for.
For the same readability reason the rule applies no opacity at all —
compositing muted text at 0.78 already dropped it under 4.5:1.

Three failures the caption alone used to miss:

- **A poll that stalls rather than errors.** No RST is delivered when a
  phone switches network or a host suspends mid-request, so the request
  sat open for the OS timeout — minutes — with the page still claiming to
  be live. Every fetch now carries an `AbortController` timeout, and a
  separate one-second ticker re-derives the caption's age from the last
  good payload, so a page whose polls simply stopped happening still goes
  stale on time.
- **A payload the page doesn't understand.** Missing keys stringify to
  `"undefined"` rather than throwing, so a renamed key rendered
  `undefined runs` / `undefined errors` under a confident heartbeat.
  `build_status` stamps a `PAYLOAD_SCHEMA` and the page refuses anything
  that doesn't match its own copy. Bump both together when a key the page
  reads is renamed, removed, or changes meaning — `overnight`
  self-updates before each run, so a tab open across a restart is routine.
- **First paint.** The static markup asserted "nothing in flight" over
  empty tables before any fetch had returned. The page starts in a
  `loading` state that says "checking…" and reserves the panel heights,
  and a first poll that fails says "could not reach the server — nothing
  loaded yet" rather than reporting an idle fleet.

The page is marked fresh only once every panel has actually rendered the
new snapshot, never on receipt of it. Polls don't overlap either: a slow
request against a restarting server could otherwise resolve *after* a
later successful one and re-mark a live page stale. The one reader-driven
refetch (the per-repo runs filter) supersedes the running poll instead of
racing it — the old request is aborted and a generation counter discards
its result, so "never two live requests" still holds. The first successful
poll clears all of it, and the existing `visibilitychange` listener is
still the fast path back.

Transitions — live↔stale, and the set of in-flight repos changing — are
also written to a visually hidden `role="status"` live region, so a
screen reader learns about them at all. Deliberately only on transitions:
the heartbeat caption changes every four seconds and would babble.
