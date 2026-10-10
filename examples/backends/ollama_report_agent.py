#!/usr/bin/env python3
"""A minimal `command` backend for gardener: report mode against a local
Ollama model. A reference implementation of the contract in
docs/BACKENDS.md, not a capable agent.

The model gets no tools. It sees gardener's prompt plus a bounded snapshot
of the target clone (its tracked-file list and the head of its README and
CLAUDE.md), and its answer is returned as the run's result. Every mode
other than report is refused, because this wrapper does nothing that could
carry out a write-mode spec.

    GARDENER_AGENT_BACKEND=command
    GARDENER_AGENT_COMMAND="python3 /path/to/gardener/examples/backends/ollama_report_agent.py"
    OLLAMA_MODEL=qwen2.5-coder:32b          # or pass `gardener align --model ...`
    OLLAMA_HOST=http://127.0.0.1:11434      # optional

Stdlib-only, like gardener itself.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_FILES = 400
MAX_DOC_BYTES = 6000


def _emit(result: str, is_error: bool, model: str) -> None:
    print(json.dumps({
        "result": result,
        "is_error": is_error,
        "session_id": f"ollama:{model}" if model else None,
        "total_cost_usd": 0,
        "permission_denials": [],
    }))


def _snapshot(repo: Path) -> str:
    try:
        files = subprocess.run(
            ["git", "ls-files"], cwd=repo, capture_output=True, text=True, timeout=60, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        files = []
    parts = [f"Tracked files ({len(files)} total, first {MAX_FILES} shown):"]
    parts += files[:MAX_FILES]
    for name in ("README.md", "CLAUDE.md"):
        path = repo / name
        if path.is_file():
            text = path.read_bytes()[:MAX_DOC_BYTES].decode("utf-8", errors="replace")
            parts.append(f"\n--- {name} (first {MAX_DOC_BYTES} bytes) ---\n{text}")
    return "\n".join(parts)


def main() -> int:
    prompt = sys.stdin.read()
    mode = os.environ.get("GARDENER_AGENT_MODE", "")
    model = os.environ.get("GARDENER_AGENT_MODEL") or os.environ.get("OLLAMA_MODEL", "")
    if mode != "report":
        _emit(f"ollama_report_agent only supports report mode, not {mode!r}", True, model)
        return 2
    if not model:
        _emit("no model: set OLLAMA_MODEL or pass --model", True, model)
        return 2

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    timeout = int(os.environ.get("GARDENER_AGENT_TIMEOUT", "1800"))
    full_prompt = (
        f"{prompt}\n\n"
        "You have no tools in this run. Base your answer only on this "
        "snapshot of the repository, and say plainly what you could not check:\n\n"
        f"{_snapshot(Path.cwd())}"
    )
    body = json.dumps({"model": model, "prompt": full_prompt, "stream": False}).encode()
    request = urllib.request.Request(
        f"{host}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read()).get("response", "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        _emit(f"ollama request failed: {e}", True, model)
        return 1
    _emit(answer, False, model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
