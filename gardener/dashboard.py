"""`gardener dashboard` — a small local, read-only web UI over gardener's
own state (sqlite run history, the garden/merge-allowlist JSON files, and
whichever `tend`/`overnight` log file was written to most recently) so a
human doesn't have to poll `gardener status` or tail log files by hand to
see what an unattended run is doing.

Stdlib-only (`http.server`), matching gardener's own stdlib-only rule (see
CLAUDE.md) — no Flask/FastAPI, no new dependency. Read-only: this module
never writes anything gardener itself owns (state db, garden/allow-list
files, logs) — it only reads and renders them. `run_server` binds to
127.0.0.1 only, never 0.0.0.0 — this serves local run history and dollar
costs, and has no auth, so it must never be reachable off this machine.

## Live-progress parsing is a best-effort UI aid, not a new source of truth

`state.list_runs()` (the sqlite db `cli.py` already writes to on every
completed dispatch) is gardener's one real outcome record — this module
never invents a second one. The one thing the db can't show is "what's
dispatched *right now*, before it's finished and been recorded" — for
that, `parse_in_progress`/`parse_batch_progress` regex-parse the active
log file's own `gardener: tending <repo> (allow_merge=...)` and `notify:
sent to Discord: gardener <mode>: ...` lines (exactly what `cli.py`'s
`_dispatch_tend`/`_notify_run` print — see their docstrings), which is
inherently best-effort: a repo whose Discord notify failed or was
suppressed (no webhook configured) will keep showing as "in progress"
until the log moves on, even though gardener itself already finished with
it. Treat the in-progress list as "what the log suggests is still
running," not gardener's authoritative record of it.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from gardener import garden, merge_allowlist, overnight, state

LOGS_DIR_NAME = "logs"
DEFAULT_PORT = 8765

TENDING_RE = re.compile(r"^gardener: tending (\S+) \(allow_merge=")
NOTIFY_RE = re.compile(r"^notify: sent to Discord: gardener (\S+): (?:MUTATION — |FAILED — )?(.+)$")
# Matches both shapes `cmd_overnight`'s progress line can take: the bare
# `N/T` a single-repo batch prints (the default `--concurrency 1`, i.e. what
# every existing cron/devsrv invocation produces) and the `N-M/T` range a
# concurrent batch prints. The end of the range is optional precisely because
# the sequential form omits it — see `parse_batch_progress`.
BATCH_RE = re.compile(r"\((\d+)(?:-(\d+))?/(\d+) candidates this run")


def default_logs_dir(state_dir: Optional[Path] = None) -> Path:
    base = state_dir or state.default_state_dir()
    return base / LOGS_DIR_NAME


def find_active_log(logs_dir: Path) -> Optional[Path]:
    """The most recently modified `*.log` file, or None if the logs dir is
    missing/empty. 'Active' is a heuristic (most-recently-written), not a
    guarantee the process behind it is still running — a just-finished
    run's log is still the most relevant one to show until a newer run
    starts writing."""
    if not logs_dir.exists():
        return None
    logs = [p for p in logs_dir.glob("*.log") if p.is_file()]
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)


def tail_lines(path: Path, n: int = 400) -> list[str]:
    """Return the last `n` lines of `path` without loading the entire file.

    Seeks backwards from the end of the file in chunks, stopping as soon as
    `n` newlines have been collected — so only the tail is ever in memory,
    regardless of file size. This matters at a 4 s poll rate when a
    long-running overnight session can produce a multi-MB log."""
    try:
        f = path.open("rb")
    except OSError:
        return []
    with f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return []
        chunk_size = 4096
        collected: list[bytes] = []
        pos = size
        newlines_found = 0
        # Read backwards in chunks until we have n+1 newlines (n+1 so we
        # can discard any partial leading line) or reach the start.
        while pos > 0 and newlines_found <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            collected.append(chunk)
            newlines_found += chunk.count(b"\n")
        raw = b"".join(reversed(collected))
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]


def parse_in_progress(lines: list[str]) -> list[str]:
    """Repos with a `gardener: tending X` line in this log that have no
    terminal notification yet — `tend`-mode notify (success or failure) or
    a *failed* `create-dev-loop` notify (a create-dev-loop success is not
    terminal; the real tend dispatch still has to run and report). See
    module docstring for why this is best-effort, not authoritative."""
    started: list[str] = []
    terminal: set[str] = set()
    for line in lines:
        m = TENDING_RE.match(line)
        if m:
            if m.group(1) not in started:
                started.append(m.group(1))
            continue
        m = NOTIFY_RE.match(line)
        if m:
            mode, repo = m.group(1), m.group(2)
            if mode == "tend" or (mode == "create-dev-loop" and "FAILED — " in line):
                terminal.add(repo)
    return [r for r in started if r not in terminal]


def _safe_list(fn) -> list:
    """Call `fn()` and return its result, or `[]` on `ValueError`.

    Guards `build_status` against a corrupt/mid-write garden or
    merge-allowlist JSON file — the dashboard must stay usable even when
    those files are temporarily invalid."""
    try:
        return fn()
    except ValueError:
        return []


def parse_batch_progress(lines: list[str]) -> Optional[tuple[int, int, int]]:
    """(range_start, range_end, total) from the most recent 'candidates this
    run' line `cmd_overnight` prints, or None if this log has no such line
    (e.g. a plain `tend --repo` dispatch, not `overnight`).

    Handles both progress shapes `cmd_overnight` emits: a concurrent batch's
    `N-M/T`, and a single-repo batch's bare `N/T` — which is what the default
    `--concurrency 1` prints, so parsing only the range form left this
    returning None for the common case (see issue #35). A bare `N/T` is
    reported as the one-repo range `(N, N, T)`, since a batch of one starts
    and ends at the same candidate."""
    match = None
    for line in lines:
        m = BATCH_RE.search(line)
        if m:
            match = m
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) is not None else start
    return (start, end, int(match.group(3)))


def build_status(
    state_dir: Optional[Path] = None,
    run_limit: int = 40,
    log_tail_lines: int = 400,
) -> dict:
    base = state_dir or state.default_state_dir()
    db_path = base / "gardener.sqlite3"
    logs_dir = default_logs_dir(base)

    runs = state.list_runs(db_path=db_path, limit=run_limit)
    active_log = find_active_log(logs_dir)
    log_lines = tail_lines(active_log, log_tail_lines) if active_log else []
    in_progress = parse_in_progress(log_lines) if active_log else []
    batch = parse_batch_progress(log_lines) if active_log else None

    recent_cost = sum(r.cost_usd for r in runs if r.cost_usd)
    recent_error_count = sum(1 for r in runs if r.outcome == "error")

    return {
        "generated_at": state.now_iso(),
        "active_log": str(active_log) if active_log else None,
        "log_tail": log_lines[-200:],
        "in_progress": in_progress,
        "batch_progress": (
            {"start": batch[0], "end": batch[1], "total": batch[2]} if batch else None
        ),
        "runs": [
            {
                "id": r.id,
                "repo": r.repo,
                "mode": r.mode,
                "outcome": r.outcome,
                "timestamp": r.timestamp,
                "summary": r.gap_summary,
                "duration_ms": r.duration_ms,
                "cost_usd": r.cost_usd,
            }
            for r in runs
        ],
        "stats": {
            "recent_run_count": len(runs),
            "recent_cost_usd": round(recent_cost, 2),
            "recent_error_count": recent_error_count,
        },
        "garden": _safe_list(lambda: garden.list_garden(path=base / "garden.json")),
        "merge_allowlist": _safe_list(
            lambda: merge_allowlist.list_allowed(path=base / "merge_allowlist.json")
        ),
        "overnight_next_index": overnight.read_cursor(path=base / "overnight_cursor.json"),
    }


PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gardener dashboard</title>
<style>
  :root {
    color-scheme: dark light;
    --bg: #14171a; --panel: #1c2023; --text: #e7ece8; --muted: #9aa39c;
    --border: #2c3236; --accent: #5fbf85; --warn: #e3a35a; --err: #ef6a63;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f5f6f4; --panel: #ffffff; --text: #1b1f1c; --muted: #5b645d;
      --border: #dfe3de; --accent: #2f7a4f; --warn: #b3661a; --err: #b3261e;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  header {
    padding: 1rem 1.5rem; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap;
  }
  header h1 { font-size: 1.1rem; margin: 0; }
  header .sub { color: var(--muted); font-size: 0.85rem; }
  main {
    padding: 1.5rem; display: grid; gap: 1.25rem;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    max-width: 1400px; margin: 0 auto;
  }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem 1.2rem; overflow: auto;
  }
  .panel h2 {
    font-size: 0.75rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--muted); margin: 0 0 0.75rem;
  }
  .wide { grid-column: 1 / -1; }
  .stats { display: flex; gap: 1.5rem; flex-wrap: wrap; }
  .stat .n { font-size: 1.6rem; font-weight: 600; }
  .stat .l { color: var(--muted); font-size: 0.78rem; }
  .pill {
    display: inline-block; padding: 0.1rem 0.55rem; border-radius: 999px;
    font-size: 0.75rem; border: 1px solid var(--border); margin: 0.15rem;
  }
  .pill.live { border-color: var(--accent); color: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }
  td.repo { white-space: nowrap; font-family: var(--mono); font-size: 0.8rem; }
  td.summary { color: var(--text); }
  .outcome-error { color: var(--err); }
  .outcome-tend, .outcome-created { color: var(--accent); }
  pre#log {
    font-family: var(--mono); font-size: 0.78rem; white-space: pre-wrap;
    word-break: break-word; max-height: 480px; overflow-y: auto; margin: 0;
    line-height: 1.45;
  }
  .empty { color: var(--muted); font-style: italic; }
  .progress-bar {
    height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: 0.4rem;
  }
  .progress-bar > div { height: 100%; background: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>🌱 gardener dashboard</h1>
  <span class="sub" id="updated">loading…</span>
</header>
<main>
  <div class="panel">
    <h2>Tonight</h2>
    <div class="stats" id="stats"></div>
    <div id="batch"></div>
  </div>
  <div class="panel">
    <h2>Currently tending</h2>
    <div id="in-progress" class="empty">nothing in flight</div>
  </div>
  <div class="panel">
    <h2 id="garden-heading">Garden</h2>
    <div id="garden"></div>
  </div>
  <div class="panel">
    <h2>Merge allow-list</h2>
    <div id="allowlist"></div>
  </div>
  <div class="panel wide">
    <h2>Recent runs</h2>
    <table>
      <thead><tr><th>Time</th><th>Repo</th><th>Mode</th><th>Outcome</th><th>Cost</th><th>Summary</th></tr></thead>
      <tbody id="runs"></tbody>
    </table>
  </div>
  <div class="panel wide">
    <h2>Live log <span class="sub" id="log-path"></span></h2>
    <pre id="log"></pre>
  </div>
</main>
<script>
function esc(s) {
  return (s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}
function fmtCost(c) { return c == null ? "—" : "$" + c.toFixed(2); }
function fmtDur(ms) { return ms == null ? "—" : (ms / 1000).toFixed(0) + "s"; }
function shortTime(ts) {
  if (!ts) return "—";
  try { return new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
  catch { return ts; }
}

async function refresh() {
  let data;
  try {
    const res = await fetch("/api/status");
    data = await res.json();
  } catch (e) {
    document.getElementById("updated").textContent = "fetch failed — retrying…";
    return;
  }

  document.getElementById("updated").textContent = "updated " + new Date(data.generated_at).toLocaleTimeString();

  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="n">${data.stats.recent_run_count}</div><div class="l">recent runs</div></div>
    <div class="stat"><div class="n">${fmtCost(data.stats.recent_cost_usd)}</div><div class="l">recent cost</div></div>
    <div class="stat"><div class="n">${data.stats.recent_error_count}</div><div class="l">errors</div></div>
    <div class="stat"><div class="n">${data.in_progress.length}</div><div class="l">in flight</div></div>
  `;

  const bp = data.batch_progress;
  // A single-repo batch (the default --concurrency 1) reports start === end;
  // render it as "candidate 3 of 5" rather than the odd-looking "3–3 of 5".
  const bpLabel = bp && (bp.start === bp.end
    ? `candidate ${bp.start} of ${bp.total} this run`
    : `candidates ${bp.start}–${bp.end} of ${bp.total} this run`);
  document.getElementById("batch").innerHTML = bp
    ? `<div class="sub">${bpLabel}</div>
       <div class="progress-bar"><div style="width:${Math.min(100, 100 * bp.end / bp.total)}%"></div></div>`
    : `<div class="empty">no overnight batch in this log</div>`;

  const ip = document.getElementById("in-progress");
  ip.innerHTML = data.in_progress.length
    ? data.in_progress.map(r => `<span class="pill live">${esc(r)}</span>`).join("")
    : `<span class="empty">nothing in flight</span>`;
  ip.className = "";

  document.getElementById("garden-heading").textContent = `Garden (${data.garden.length})`;
  document.getElementById("garden").innerHTML = data.garden.length
    ? data.garden.map(r => `<span class="pill">${esc(r)}</span>`).join("")
    : `<span class="empty">empty</span>`;

  document.getElementById("allowlist").innerHTML = data.merge_allowlist.length
    ? data.merge_allowlist.map(r => `<span class="pill">${esc(r)}</span>`).join("")
    : `<span class="empty">empty — nothing can auto-merge</span>`;

  document.getElementById("runs").innerHTML = data.runs.map(r => `
    <tr>
      <td>${shortTime(r.timestamp)}</td>
      <td class="repo">${esc(r.repo)}</td>
      <td>${esc(r.mode)}</td>
      <td class="outcome-${esc(r.outcome)}">${esc(r.outcome)}</td>
      <td>${fmtCost(r.cost_usd)}</td>
      <td class="summary">${esc(r.summary)}</td>
    </tr>
  `).join("") || `<tr><td colspan="6" class="empty">no runs recorded yet</td></tr>`;

  document.getElementById("log-path").textContent = data.active_log ? "(" + data.active_log + ")" : "";
  const logEl = document.getElementById("log");
  const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logEl.textContent = data.log_tail.join("\\n") || "(no active log)";
  if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
}

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


class _DashboardHandler(BaseHTTPRequestHandler):
    state_dir: Optional[Path] = None

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        # BaseHTTPRequestHandler logs every request to stderr by default;
        # a 4s-polling dashboard would spam gardener's own terminal output
        # with a request line every 4 seconds for no benefit.
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/" or self.path == "/index.html":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            payload = build_status(state_dir=self.state_dir)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def run_server(port: int = DEFAULT_PORT, state_dir: Optional[Path] = None, host: str = "127.0.0.1") -> None:
    """Blocks forever serving the dashboard. Binds to `host` (127.0.0.1 by
    default) only — this has no authentication, so it must never be bound
    to 0.0.0.0/a real interface. WSL2 forwards a loopback bind through to
    the Windows host's own localhost automatically, so the default is
    reachable from a Windows browser without any extra network config.

    Raises `ValueError` if `host` does not resolve to a loopback address,
    so a future caller can't accidentally expose the dashboard on 0.0.0.0
    just by passing a different `host`."""
    try:
        addr_info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"dashboard host {host!r} could not be resolved: {exc}") from exc
    for _family, _type, _proto, _canonname, sockaddr in addr_info:
        ip = sockaddr[0]
        try:
            if not ipaddress.ip_address(ip).is_loopback:
                raise ValueError(
                    f"dashboard host {host!r} resolves to non-loopback address {ip!r}; "
                    "the dashboard has no authentication and must only bind to loopback"
                )
        except ValueError as exc:
            # Re-raise ValueErrors from our own check unchanged; swallow
            # ipaddress.ip_address() parse errors (shouldn't happen —
            # getaddrinfo returns valid IPs — but guard against unexpected
            # formats).
            if "loopback" in str(exc):
                raise
    # Class attribute, not an instance one: http.server instantiates
    # handler_class(request, client_address, server) itself per request,
    # so this is how per-server config (which state dir to read) reaches
    # each handler instance.
    _DashboardHandler.state_dir = state_dir
    # ThreadingHTTPServer so the 4s-polling browser tab and any concurrent
    # request never block on each other — each request only does local
    # file/sqlite reads, never a `claude`/`git`/`gh` subprocess.
    httpd = ThreadingHTTPServer((host, port), _DashboardHandler)
    print(f"gardener dashboard: serving on http://{host}:{port} (Ctrl+C to stop)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def find_free_port(preferred: int = DEFAULT_PORT, host: str = "127.0.0.1") -> int:
    """`preferred` if it's free, otherwise the next port the OS hands back
    — so a second `gardener dashboard` invocation (e.g. after a crash left
    the first one running) doesn't just fail with 'address in use'.

    Sets SO_REUSEADDR on the probe socket to match `run_server`'s actual
    server socket (`http.server.HTTPServer.allow_reuse_address = 1`,
    inherited by `ThreadingHTTPServer`) — without it, this probe reports a
    false "in use" for a port still sitting in TIME_WAIT from a just-killed
    previous instance, even though the real server would bind it fine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return s.getsockname()[1]
