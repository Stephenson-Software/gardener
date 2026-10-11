"""Deterministic stand-in agents for the offline integration tier.

    stub_agent.py command BEHAVIOUR   # a command-harness agent
    stub_agent.py claude [ARGS...]    # a fake `claude` binary

As a command harness, BEHAVIOUR picks what the agent does:

  ok-text       print a plain-text report (no JSON envelope)
  ok-json       print a successful JSON envelope
  error-json    print an envelope with is_error true, exit 0
  blocked-json  print an envelope with is_error and blocked true, exit 1
  exit-nonzero  print nothing useful and exit 3
  write-file    create an untracked file in the clone, then report success
  edit-file     modify a tracked file in the clone, then report success
  commit        commit to the clone, then report success
  sleep         start a child, then both sleep far past any test timeout

As `claude` (via GARDENER_CLAUDE_BIN), it writes its argv and cwd to
`$GARDENER_IT_RECORD_DIR/claude-*.json` and prints a successful
Claude-Code-shaped envelope. With `GARDENER_IT_CLAUDE_BEHAVIOUR=sleep` it
instead starts a child and sleeps, like a tend whose build outlives the
timeout. Nothing here touches the network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPORT = (
    "## Gap checklist\n\n- README documents the tests: N (no test command in README.md)\n\n"
    "## Summary\n\nGARDENER_SUMMARY: 1 gap(s) found across 1 section(s) — README lacks a test command\n"
)


def envelope(**overrides) -> str:
    body = {
        "result": REPORT,
        "is_error": False,
        "session_id": "stub-session",
        "total_cost_usd": 0.0,
        "permission_denials": [],
        "blocked": False,
    }
    body.update(overrides)
    return json.dumps(body)


def git(*args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.name=stub", "-c", "user.email=stub@example.invalid",
         *args],
        check=True, capture_output=True,
    )


def command(behaviour: str) -> int:
    sys.stdin.read()
    if behaviour == "ok-text":
        print(REPORT)
    elif behaviour == "ok-json":
        print(envelope())
    elif behaviour == "error-json":
        print(envelope(result="stub agent failed", is_error=True))
    elif behaviour == "blocked-json":
        print(envelope(result="model server unreachable", is_error=True, blocked=True))
        return 1
    elif behaviour == "exit-nonzero":
        print("stub agent crashed", file=sys.stderr)
        return 3
    elif behaviour == "write-file":
        Path("stub-untracked.txt").write_text("written during a report run\n")
        print(envelope())
    elif behaviour == "edit-file":
        with open("README.md", "a") as f:
            f.write("\nedited during a report run\n")
        print(envelope())
    elif behaviour == "commit":
        Path("stub-committed.txt").write_text("committed during a report run\n")
        git("add", "stub-committed.txt")
        git("commit", "-qm", "stub commit")
        print(envelope())
    elif behaviour == "sleep":
        # Sleep in a child, as a wrapper around a real agent would, and
        # leave its pid for the test to check it was killed too.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        record_dir = os.environ.get("GARDENER_IT_RECORD_DIR")
        if record_dir:
            Path(record_dir, f"sleep-child-{os.getpid()}.pid").write_text(str(child.pid))
        time.sleep(600)
    else:
        print(f"unknown stub behaviour: {behaviour}", file=sys.stderr)
        return 2
    return 0


def claude(args: list[str]) -> int:
    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    record_dir = os.environ.get("GARDENER_IT_RECORD_DIR")
    if os.environ.get("GARDENER_IT_CLAUDE_BEHAVIOUR") == "sleep":
        # Like a tend whose build outlives the timeout: a child, then sleep.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        if record_dir:
            Path(record_dir).mkdir(parents=True, exist_ok=True)
            Path(record_dir, f"sleep-child-{os.getpid()}.pid").write_text(str(child.pid))
        time.sleep(600)
    if record_dir:
        Path(record_dir).mkdir(parents=True, exist_ok=True)
        record = {"argv": args, "cwd": os.getcwd(), "stdin": prompt}
        (Path(record_dir) / f"claude-{time.time_ns()}.json").write_text(json.dumps(record, indent=1))
    print(envelope(total_cost_usd=0.01))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "claude":
        sys.exit(claude(sys.argv[2:]))
    if len(sys.argv) == 3 and sys.argv[1] == "command":
        sys.exit(command(sys.argv[2]))
    print(__doc__, file=sys.stderr)
    sys.exit(2)
