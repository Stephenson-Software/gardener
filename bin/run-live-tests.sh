#!/usr/bin/env bash
# Invoked weekly by a Windows Task Scheduler task (via wsl.exe), like
# run-overnight.sh, to run gardener's paid live integration tiers on the
# real box: the Claude Code report tier and, when GARDENER_LIVE_WRITE_REPO
# names a throwaway repository, the --implement / --file-issue / tend tier.
# About $1 a run. See docs/INTEGRATION_TESTS.md.
#
# The outcome goes to the configured Discord webhook (gardener's own
# notifier): INFO when every test passed, ERROR otherwise, so drift in
# `claude` or a prompt regression is noticed without anyone reading logs.
# The log lands in ~/.local/state/gardener/logs/live-tests/, a subdirectory,
# so the dashboard (which reads logs/*.log as overnight runs) ignores it.
set -uo pipefail

export PATH="/root/.local/bin:$PATH"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="/root/.venvs/gardener/bin/python"
LOG_DIR="${GARDENER_STATE_DIR:-$HOME/.local/state/gardener}/logs/live-tests"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/live-tests-$(date +%Y%m%d-%H%M%S).log"

MODULES=(integration.test_live_claude_code)
export GARDENER_IT_CLAUDE=1
if [ -n "${GARDENER_LIVE_WRITE_REPO:-}" ]; then
    export GARDENER_IT_WRITE_REPO="$GARDENER_LIVE_WRITE_REPO"
    MODULES+=(integration.test_live_write_modes)
fi

cd "$REPO_DIR" || exit 1
PYTHONPATH=. "$PYTHON" -m unittest "${MODULES[@]}" -v >"$LOG" 2>&1
STATUS=$?

# Keep the last 12 weekly logs.
ls -1t "$LOG_DIR"/live-tests-*.log 2>/dev/null | tail -n +13 | xargs -r rm -f

PYTHONPATH=. "$PYTHON" - "$STATUS" "$LOG" <<'EOF'
import re
import sys
from pathlib import Path

from gardener import notify

status, log = int(sys.argv[1]), Path(sys.argv[2])
text = log.read_text(errors="replace")
ran = re.search(r"^Ran (\d+) tests? in ([\d.]+)s", text, re.M)
result = re.search(r"^(OK.*|FAILED.*)$", text, re.M)
failed = re.findall(r"^(?:FAIL|ERROR): (\S+)", text, re.M)
summary = f"{ran.group(1) if ran else '?'} test(s), {result.group(1) if result else 'no result line'}"
lines = re.findall(r"^\[(?:implement|file-issue|tend)\] .*$", text, re.M)
message = "\n".join([summary, *("failed: " + name for name in failed), *lines, f"log: {log}"])
level = notify.Level.INFO if status == 0 else notify.Level.ERROR
title = "gardener live tests: passed" if status == 0 else "gardener live tests: FAILED"
print(f"{title}\n{message}")
notify.default_notifier().notify(title, message, level)
EOF
exit "$STATUS"
