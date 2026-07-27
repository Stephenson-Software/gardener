#!/usr/bin/env bash
# Invoked nightly by a Windows Task Scheduler task (via wsl.exe) to run
# `gardener overnight`. Not meant to be run interactively — use
# `gardener overnight --hours N` directly for that.
#
# Task Scheduler runs this via `wsl.exe ... bash -lc "..."`. `-c` mode is
# never interactive even with `-l`, so ~/.bashrc's own
# `[ -z "$PS1" ] && return` guard bails out before it ever reaches the
# `export PATH="$HOME/.local/bin:$PATH"` line further down that file --
# meaning `claude` (installed at /root/.local/bin, a real binary, not an
# npm/nvm shim) is invisible to a genuinely fresh, minimal-env wsl.exe
# launch even though it resolves fine in any shell that inherited an
# already-enriched PATH (e.g. an interactive terminal, or this script
# tested by hand from one). Confirmed directly: `env -i HOME="$HOME"
# USER=root bash -lc 'which claude'` fails while a normal interactive
# shell's `which claude` succeeds -- every dispatched repo failing
# instantly with "claude not found on PATH" (28 repos in ~1 minute, i.e.
# before any real clone/dispatch work even started) is this, not a
# gardener bug. Exporting PATH explicitly here, rather than depending on
# .bashrc sourcing, means this script no longer depends on how it's
# invoked.
# `gardener overnight` now writes and prunes its own run log internally
# (gardener/run_log.py, added since this script was first written) --
# every line this used to capture via its own `>>"$LOG_FILE" 2>&1` wrapper
# is already mirrored to `~/.local/state/gardener/logs/overnight-<ts>.log`
# by gardener itself, including the dashboard-required naming/format the
# old manual redirect only approximated. Keeping this script's OWN
# redirection alongside that produced doubled log lines whenever both
# happened to compute the same filename in the same second (confirmed
# directly: a real overnight run's log had every line duplicated) and, even
# when the filenames didn't collide, always a second full copy of the same
# content in a different file for no reason. So: run gardener directly and
# let it own its own log entirely; this script's only remaining job is
# fixing the PATH problem below.
set -uo pipefail

export PATH="/root/.local/bin:$PATH"

GARDENER_BIN="/root/.venvs/gardener/bin/gardener"
HOURS="${GARDENER_OVERNIGHT_HOURS:-8}"

"$GARDENER_BIN" overnight --hours "$HOURS"
