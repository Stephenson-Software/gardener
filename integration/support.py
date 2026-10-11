"""Shared machinery for gardener's integration suite.

Every test drives the real CLI, `python -m gardener align ...`, as a
subprocess in an isolated sandbox:

- its own `GARDENER_STATE_DIR` and `GARDENER_CACHE_DIR`;
- no ambient `GARDENER_*` setting, Discord webhook, hub or usage
  reporting;
- a conventions repository built from `fixtures/conventions` (or one the
  operator names);
- either an offline target or a real GitHub one.

**Offline target.** A local Git repo seeded straight into gardener's clone
cache from `fixtures/target`, run with `--no-refresh-target`. A fake `gh` on
`PATH` records any call and fails, so an offline test proves it made none.

**Live target.** `GARDENER_IT_REPO`, cloned through the real `gh`. Its
GitHub `pushedAt` is compared before and after the run.

The harness command is wrapped in `recorder.py`, so each test can read what
the harness was given and what it returned. See docs/INTEGRATION_TESTS.md.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
RECORDER = HERE / "recorder.py"
STUB_AGENT = HERE / "stub_agent.py"
OFFLINE_REPO = "it-fixture/multiply"
# gh calls that only read. `claude` itself makes the first two at startup;
# gardener makes the rest. Anything else in the offline tier is a failure.
READ_ONLY_GH_CALLS = ("auth token", "auth status", "repo view", "repo clone", "pr list", "issue list")
FAKE_GH = HERE / "fake_gh.py"

# Ambient settings that would leak the operator's real configuration into a
# test run. Every GARDENER_* variable is dropped as well.
_SCRUBBED = {"DISCORD_WEBHOOK_URL"}


def env_setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _passthrough(name: str) -> bool:
    """`GARDENER_IT_PASSTHROUGH` lets a harness's own GARDENER_-prefixed
    settings (for example GARDENER_ORKET_*) through the scrub: a
    comma-separated list of names, each optionally ending in `*`."""
    for pattern in filter(None, (p.strip() for p in env_setting("GARDENER_IT_PASSTHROUGH").split(","))):
        if name == pattern or (pattern.endswith("*") and name.startswith(pattern[:-1])):
            return True
    return False


def live_repo() -> str | None:
    """`GARDENER_IT_REPO` (owner/name) for a real GitHub target, else None
    for the offline fixture target."""
    return env_setting("GARDENER_IT_REPO") or None


def live_conventions_url() -> str | None:
    return env_setting("GARDENER_IT_CONVENTIONS_URL") or None


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.name=gardener-it",
         "-c", "user.email=gardener-it@example.invalid", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check, timeout=120,
    )


def make_repo(path: Path, source: Path | None = None, files: dict[str, str] | None = None) -> Path:
    """A committed Git repo at `path` from a fixture directory and/or a
    mapping of relative path -> text."""
    path.mkdir(parents=True, exist_ok=True)
    if source is not None:
        shutil.copytree(source, path, dirs_exist_ok=True)
    for rel, text in (files or {}).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")  # `init -b` needs git 2.28+
    git(path, "add", "-A")
    git(path, "commit", "-qm", "fixture")
    return path


def pid_alive(pid: int) -> bool:
    """True while `pid` exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


def wait_dead(pids: list[int], wait: float = 10) -> list[int]:
    """The pids still alive after up to `wait` seconds."""
    import time

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.1)
    return [p for p in pids if pid_alive(p)]


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StubServer:
    """A loopback HTTP server that records every request and answers 200
    with `{}` (or `reply`). It stands in for an outside service, such as a
    Discord webhook or the usage-reporting endpoint, so a test can assert
    exactly what gardener sent without anything leaving the machine."""

    def __init__(self, reply: bytes = b"{}", status: int = 200):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                server.requests.append({"method": self.command, "path": self.path,
                                        "headers": dict(self.headers), "body": body})
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(reply)

            do_POST = do_GET = do_PUT = _record

            def log_message(self, *_args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def json_bodies(self) -> list:
        out = []
        for r in self.requests:
            try:
                out.append(json.loads(r["body"] or b"null"))
            except ValueError:
                out.append(None)
        return out

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def wait_http(url: str, wait: float = 30, headers: dict | None = None) -> bool:
    """Whether `url` answered (any HTTP status) within `wait` seconds."""
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=2).close()
            return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            time.sleep(0.2)
    return False


def pushed_at(repo: str) -> str:
    proc = subprocess.run(["gh", "repo", "view", repo, "--json", "pushedAt", "-q", ".pushedAt"],
                          capture_output=True, text=True, timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else f"unknown: {proc.stderr.strip()}"


@dataclass
class AlignResult:
    code: int
    stdout: str
    stderr: str
    records: list[dict]
    run: dict | None
    clone: Path
    clone_porcelain: str
    clone_head_before: str
    clone_head_after: str
    gh_calls: list[str]
    seconds: float
    pushed_at_before: str = ""
    pushed_at_after: str = ""
    sandbox: "Sandbox | None" = field(default=None, repr=False)

    @property
    def record(self) -> dict | None:
        """The last harness invocation's record, if the harness ran."""
        return self.records[-1] if self.records else None

    @property
    def envelope(self) -> dict | None:
        return self.record.get("envelope") if self.record else None

    def describe(self) -> str:
        return (f"exit={self.code} seconds={self.seconds:.1f} records={len(self.records)}\n"
                f"--- stderr (tail)\n{self.stderr[-2500:]}\n--- stdout (tail)\n{self.stdout[-1500:]}")


class Sandbox:
    """One isolated gardener installation for one test."""

    def __init__(self, test: unittest.TestCase | None, repo: str | None = None, root: Path | None = None,
                 local_github: bool = False, fake_home: bool = False):
        """`test` registers cleanup of the sandbox (unless GARDENER_IT_KEEP
        is set); the matrix runner passes None and its own kept `root`.

        `local_github` makes the fake `gh` serve clones and refreshes from
        local bare repositories (`add_remote`), so the paths that clone or
        refresh a target, which `tend` and `overnight` always do, run
        offline too. `fake_home` points HOME into the sandbox, for commands
        that read or write under it (`tend` looks for dev-loop skills in
        `~/.claude/commands`)."""
        if root is not None:
            root.mkdir(parents=True, exist_ok=False)
            self.root = root
        else:
            self.root = Path(tempfile.mkdtemp(prefix="gardener-it-"))
        if test is not None and env_setting("GARDENER_IT_KEEP") not in ("1", "true"):
            test.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state"
        self.cache = self.root / "cache"
        self.records = self.root / "records"
        self.bin = self.root / "bin"
        for d in (self.state, self.cache, self.records, self.bin):
            d.mkdir()
        self.repo = repo or OFFLINE_REPO
        self.offline = repo is None
        self.settings: dict[str, str] = {}
        self.extra_env: dict[str, str] = {}
        self.remotes = self.root / "remotes" if local_github else None
        self.home = self.root / "home" if fake_home else None
        if self.home is not None:
            self.home.mkdir()
        if self.offline:
            self._install_fake_gh()

    # -- fixtures -------------------------------------------------------

    def _install_fake_gh(self) -> None:
        script = self.bin / "gh"
        script.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_GH))} \"$@\"\n")
        script.chmod(0o755)

    def add_remote(self, repo: str | None = None, source: Path | None = FIXTURES / "target",
                   files: dict[str, str] | None = None) -> Path:
        """A local bare repository the fake `gh` serves as `repo` on GitHub."""
        assert self.remotes is not None, "add_remote needs Sandbox(local_github=True)"
        repo = repo or self.repo
        work = make_repo(self.root / "remote-src" / repo, source=source, files=files)
        bare = self.remotes / f"{repo}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "clone", "-q", "--bare", str(work), str(bare))
        return bare

    def install_dev_loop_skill(self, repo: str | None = None) -> Path:
        """What `tend` checks for before dispatching a repo's dev loop, under
        the sandbox's HOME."""
        assert self.home is not None, "install_dev_loop_skill needs Sandbox(fake_home=True)"
        name = (repo or self.repo).split("/", 1)[1].lower()
        slug = "".join(c if c.isalnum() else "-" for c in name).strip("-") + "-dev-loop"
        command = self.home / ".claude" / "commands" / f"{slug}.md"
        command.parent.mkdir(parents=True, exist_ok=True)
        command.write_text(f"# /{slug}\n\nA stand-in dev-loop skill for the integration suite.\n")
        return command

    def gh_calls(self) -> list[str]:
        log = self.root / "gh-calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def clone_path(self) -> Path:
        return self.cache / "repos" / self.repo.replace("/", "__")

    def seed_target(self, files: dict[str, str] | None = None, source: Path | None = FIXTURES / "target") -> Path:
        """Put a committed fixture repo where gardener keeps its clone of
        `self.repo`, so `--no-refresh-target` uses it as-is."""
        clone = make_repo(self.clone_path(), source=source, files=files)
        git(clone, "remote", "add", "origin", f"https://github.com/{self.repo}.git")
        return clone

    def conventions(self, source: Path | None = None, compact_copy: bool = True) -> str:
        """A local conventions repo built from `source` (default: the bundled
        fixture). `compact_copy` adds a copy of the same docs under
        `compact/` for the subdirectory tests. Returns a file:// URL."""
        source = source or FIXTURES / "conventions"
        path = self.root / "conventions-src"
        if (path / ".git").is_dir():
            # Built once per sandbox, so a test can run gardener repeatedly.
            return path.as_uri()
        files = {}
        if compact_copy:
            for f in source.rglob("*"):
                if f.is_file():
                    files[f"compact/{f.relative_to(source)}"] = f.read_text()
        make_repo(path, source=source, files=files)
        return path.as_uri()

    def fake_claude(self) -> str:
        script = self.bin / "claude"
        script.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(STUB_AGENT))} claude \"$@\"\n")
        script.chmod(0o755)
        return str(script)

    # -- harness wiring -------------------------------------------------

    def command_harness(self, argv: list[str], model: str = "") -> None:
        """Use the `command` harness with `argv`, wrapped in the recorder."""
        wrapped = [sys.executable, str(RECORDER), "--", *argv]
        self.settings["GARDENER_HARNESS"] = "command"
        self.settings["GARDENER_HARNESS_COMMAND"] = shlex.join(wrapped)
        if model:
            self.settings["GARDENER_HARNESS_MODEL"] = model

    def stub_harness(self, behaviour: str) -> None:
        self.command_harness([sys.executable, str(STUB_AGENT), "command", behaviour])

    # -- running ----------------------------------------------------------

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if (not k.startswith("GARDENER_") or _passthrough(k)) and k not in _SCRUBBED}
        env.update({
            "PYTHONPATH": str(ROOT),
            "GARDENER_STATE_DIR": str(self.state),
            "GARDENER_CACHE_DIR": str(self.cache),
            "GARDENER_USAGE_REPORTING_ENABLED": "false",
            "GARDENER_IT_RECORD_DIR": str(self.records),
        })
        if self.offline:
            env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
            env["GARDENER_IT_GH_LOG"] = str(self.root / "gh-calls.log")
        if self.remotes is not None:
            env["GARDENER_IT_GH_REMOTES"] = str(self.remotes)
        if self.home is not None:
            env["HOME"] = str(self.home)
        env.update(self.settings)
        env.update(self.extra_env)
        return env

    def align(self, *args: str, conventions_url: str | None = None, timeout: float = 4000) -> AlignResult:
        import time

        argv = [sys.executable, "-m", "gardener", "align", "--repo", self.repo,
                "--conventions-repo", conventions_url or self.conventions(), *args]
        self._prepare_target(argv)
        clone = self.clone_path()
        head_before = git(clone, "rev-parse", "HEAD", check=False).stdout.strip() if clone.exists() else ""
        before = "" if self.offline else pushed_at(self.repo)
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(self.root), env=self.env(), capture_output=True,
                                  text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            code, out, err = -9, _text(e.stdout), _text(e.stderr) + f"\n[test] killed after {timeout}s"
        seconds = time.monotonic() - start
        after = "" if self.offline else pushed_at(self.repo)
        porcelain = git(clone, "status", "--porcelain", check=False).stdout if clone.exists() else "<no clone>"
        head_after = git(clone, "rev-parse", "HEAD", check=False).stdout.strip() if clone.exists() else ""
        if not head_before and clone.exists():
            # A live target cloned during this run had no HEAD before it; the
            # untouched state is then the upstream branch it was cloned from.
            head_before = git(clone, "rev-parse", "@{upstream}", check=False).stdout.strip()
        return AlignResult(
            code=code, stdout=out, stderr=err, records=self._records(), run=self.last_run(),
            clone=clone, clone_porcelain=porcelain, clone_head_before=head_before,
            clone_head_after=head_after, gh_calls=self.gh_calls(), seconds=seconds,
            pushed_at_before=before, pushed_at_after=after, sandbox=self,
        )

    def start_align(self, *args: str, conventions_url: str | None = None) -> subprocess.Popen:
        """`align` started in the background, for tests that signal gardener
        mid-run. The caller waits on and reaps the process."""
        argv = [sys.executable, "-m", "gardener", "align", "--repo", self.repo,
                "--conventions-repo", conventions_url or self.conventions(), *args]
        self._prepare_target(argv)
        return subprocess.Popen(argv, cwd=str(self.root), env=self.env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _prepare_target(self, argv: list[str]) -> None:
        if self.remotes is not None:
            if not (self.remotes / f"{self.repo}.git").exists():
                self.add_remote()
        elif self.offline:
            argv.append("--no-refresh-target")
            if not self.clone_path().exists():
                self.seed_target()

    def cli(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess:
        """Any gardener command, run to completion in this sandbox."""
        return subprocess.run([sys.executable, "-m", "gardener", *args], cwd=str(self.root), env=self.env(),
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def start_cli(self, *args: str) -> subprocess.Popen:
        """Any gardener command, started in the background. The caller waits
        on and reaps it."""
        return subprocess.Popen([sys.executable, "-m", "gardener", *args], cwd=str(self.root), env=self.env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True)

    def runs(self) -> list[dict]:
        db = self.state / "gardener.sqlite3"
        if not db.exists():
            return []
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]

    def sleeping_agents(self, count: int = 1, wait: float = 60) -> list[int]:
        """Pids of the children the `sleep` stand-in agents started, waiting
        up to `wait` seconds for `count` of them."""
        import time

        deadline = time.monotonic() + wait
        while True:
            pids = [int(p.read_text()) for p in sorted(self.records.glob("sleep-child-*.pid"))
                    if p.read_text().strip()]
            if len(pids) >= count or time.monotonic() > deadline:
                return pids
            time.sleep(0.1)

    def write_gh_calls(self) -> list[str]:
        """gh calls that were not read-only."""
        return [c for c in self.gh_calls() if not c.startswith(READ_ONLY_GH_CALLS)]

    def _records(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.records.glob("*.json"))]

    def last_run(self) -> dict | None:
        db = self.state / "gardener.sqlite3"
        if not db.exists():
            return None
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


class IntegrationCase(unittest.TestCase):
    """Assertions shared by every tier."""

    def assertCloneUntouched(self, r: AlignResult) -> None:
        self.assertEqual(r.clone_porcelain, "", f"report run left the clone dirty\n{r.describe()}")
        self.assertEqual(r.clone_head_before, r.clone_head_after, f"report run moved HEAD\n{r.describe()}")

    def assertNotPushed(self, r: AlignResult) -> None:
        if r.sandbox is not None and r.sandbox.offline:
            # `claude` itself runs `gh auth token` at startup; read-only auth
            # queries are fine. Anything else (and every call from gardener
            # in the offline tier) is not.
            calls = [c for c in r.gh_calls if not c.startswith(READ_ONLY_GH_CALLS)]
            self.assertEqual(calls, [], "offline run made a gh call other than a read-only auth query")
        else:
            self.assertEqual(r.pushed_at_before, r.pushed_at_after, "target was pushed to during the run")

    def assertSafeReport(self, r: AlignResult) -> None:
        self.assertCloneUntouched(r)
        self.assertNotPushed(r)
