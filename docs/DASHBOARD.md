# The garden view

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
  attention first.
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
