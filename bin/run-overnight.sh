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
set -uo pipefail

export PATH="/root/.local/bin:$PATH"

GARDENER_BIN="/root/.venvs/gardener/bin/gardener"
LOG_DIR="/root/.local/state/gardener/logs"
LOG_FILE="$LOG_DIR/overnight-$(date +%Y%m%d-%H%M%S).log"
HOURS="${GARDENER_OVERNIGHT_HOURS:-8}"

mkdir -p "$LOG_DIR"

{
  echo "=== gardener overnight starting: $(date -Is) (budget ${HOURS}h) ==="
  "$GARDENER_BIN" overnight --hours "$HOURS"
  echo "=== gardener overnight finished: $(date -Is) (exit $?) ==="
} >>"$LOG_FILE" 2>&1

# Keep the last 30 days of logs, no more.
find "$LOG_DIR" -name 'overnight-*.log' -mtime +30 -delete

ln -sf "$LOG_FILE" "$LOG_DIR/latest.log"
