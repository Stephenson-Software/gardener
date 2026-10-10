#!/usr/bin/env python3
"""A minimal `command` harness for gardener: report mode against a local
Ollama model. A reference implementation of the contract in
docs/HARNESSES.md, not a capable agent.

The model gets no tools. It sees gardener's prompt plus a bounded snapshot
of the target clone (its tracked-file list and the head of its README and
CLAUDE.md), and its answer is returned as the run's result. Every mode
other than report is refused, because this wrapper does nothing that could
carry out a write-mode spec.

    GARDENER_HARNESS=command
    GARDENER_HARNESS_COMMAND="python3 /path/to/gardener/examples/harnesses/ollama_report_agent.py"
    OLLAMA_MODEL=qwen2.5-coder:32b          # or GARDENER_HARNESS_MODEL / --model

Optional, all read from the environment:

    OLLAMA_HOST=http://127.0.0.1:11434      # Ollama server
    OLLAMA_NUM_CTX=32768                    # context window; Ollama's own default
                                            # is small enough to truncate the prompt
    OLLAMA_AGENT_MAX_FILES=400              # tracked files listed in the snapshot
    OLLAMA_AGENT_MAX_DOC_BYTES=6000         # bytes included from each doc below
    OLLAMA_AGENT_DOCS=README.md,CLAUDE.md   # docs included in the snapshot

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

DEFAULT_MAX_FILES = 400
DEFAULT_MAX_DOC_BYTES = 6000
DEFAULT_DOCS = "README.md,CLAUDE.md"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)  # a malformed value fails the run loudly, not silently
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


def _emit(result: str, is_error: bool, model: str, blocked: bool = False) -> None:
    print(json.dumps({
        "result": result,
        "is_error": is_error,
        "blocked": blocked,
        "session_id": f"ollama:{model}" if model else None,
        "total_cost_usd": 0,
        "permission_denials": [],
    }))


def _snapshot(repo: Path, max_files: int, max_doc_bytes: int, docs: list[str]) -> str:
    try:
        files = subprocess.run(
            ["git", "ls-files"], cwd=repo, capture_output=True, text=True, timeout=60, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        files = []
    parts = [f"Tracked files ({len(files)} total, first {max_files} shown):"]
    parts += files[:max_files]
    for name in docs:
        path = repo / name
        if path.is_file():
            text = path.read_bytes()[:max_doc_bytes].decode("utf-8", errors="replace")
            parts.append(f"\n--- {name} (first {max_doc_bytes} bytes) ---\n{text}")
    return "\n".join(parts)


def main() -> int:
    prompt = sys.stdin.read()
    mode = os.environ.get("GARDENER_HARNESS_MODE", "")
    model = os.environ.get("GARDENER_HARNESS_MODEL") or os.environ.get("OLLAMA_MODEL", "")
    if mode != "report":
        _emit(f"ollama_report_agent only supports report mode, not {mode!r}", True, model)
        return 2
    if not model:
        _emit("no model: set OLLAMA_MODEL or pass --model", True, model)
        return 2

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    try:
        timeout = _int_env("GARDENER_HARNESS_TIMEOUT", 1800)
        max_files = _int_env("OLLAMA_AGENT_MAX_FILES", DEFAULT_MAX_FILES)
        max_doc_bytes = _int_env("OLLAMA_AGENT_MAX_DOC_BYTES", DEFAULT_MAX_DOC_BYTES)
        num_ctx = _int_env("OLLAMA_NUM_CTX", 0)  # 0: leave Ollama's default
    except ValueError as e:
        _emit(f"bad setting: {e}", True, model)
        return 2
    docs = [d.strip() for d in os.environ.get("OLLAMA_AGENT_DOCS", DEFAULT_DOCS).split(",") if d.strip()]
    full_prompt = (
        f"{prompt}\n\n"
        "You have no tools in this run. Base your answer only on this "
        "snapshot of the repository, and say plainly what you could not check:\n\n"
        f"{_snapshot(Path.cwd(), max_files, max_doc_bytes, docs)}"
    )
    payload = {"model": model, "prompt": full_prompt, "stream": False}
    if num_ctx:
        payload["options"] = {"num_ctx": num_ctx}
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{host}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read()).get("response", "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        # An unreachable Ollama server fails every repo identically, so it
        # is declared device-global: an overnight batch stops here.
        _emit(f"ollama request failed: {e}", True, model, blocked=isinstance(e, urllib.error.URLError))
        return 1
    _emit(answer, False, model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
