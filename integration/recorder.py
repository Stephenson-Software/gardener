"""Transparent recording shim for a command harness.

    GARDENER_HARNESS_COMMAND="python integration/recorder.py -- <real command...>"

Runs the real command exactly as gardener would have: same stdin, cwd and
environment. Its stdout, stderr and exit code are passed through unchanged.
It also writes one JSON record per invocation to `$GARDENER_IT_RECORD_DIR`,
so a test can assert on what the harness was given (prompt, mode, spec,
model, timeout, cwd) and what it returned (the envelope gardener parsed).
gardener itself keeps only the result text and its own run row, so without
this shim fields like `blocked` would not be observable from outside.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HARNESS_ENV_PREFIX = "GARDENER_HARNESS_"


def main(argv: list[str]) -> int:
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print("usage: recorder.py -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    command = argv[argv.index("--") + 1:]
    stdin = sys.stdin.read()
    start = time.monotonic()
    proc = subprocess.run(command, input=stdin, capture_output=True, text=True)
    record = {
        "argv": command,
        "cwd": os.getcwd(),
        "stdin": stdin,
        "env": {k: v for k, v in os.environ.items() if k.startswith(HARNESS_ENV_PREFIX)},
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "seconds": round(time.monotonic() - start, 3),
    }
    try:
        record["envelope"] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        record["envelope"] = None
    record_dir = os.environ.get("GARDENER_IT_RECORD_DIR")
    if record_dir:
        path = Path(record_dir)
        path.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{os.getpid()}.json"
        (path / name).write_text(json.dumps(record, indent=1))
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
