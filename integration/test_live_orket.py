"""Live tier: Orket's governed Gardener harness
(`examples/gardener_harness/gardener_harness.py` in an Orket checkout),
driven through the real `gardener align`.

Skipped unless these are set:

    GARDENER_IT_ORKET_SRC       Orket source checkout
    GARDENER_IT_ORKET_PYTHON    Python with that Orket installed
    GARDENER_IT_ORKET_CATALOG   extensions_catalog.json with the governed
                                report example installed
    GARDENER_IT_ORKET_MODEL     exact served model, e.g. llama3.2:3b

Optional:

    GARDENER_IT_ORKET_PROVIDER  default ollama
    GARDENER_IT_ORKET_BASE_URL  default http://127.0.0.1:11434
    GARDENER_IT_ORKET_DELIVER_ENV
                                JSON object of extra settings for the runs
                                that reach the model, e.g. a raised
                                ORKET_GARDENER_MAX_INPUT_TOKENS and an
                                ORKET_LOCAL_PROMPT_PROFILE_REGISTRY_PATH
    GARDENER_IT_ORKET_INFERENCE=1
                                also run the tests that call the model
                                (slow on CPU: minutes per test)

The fast tests prove refusals happen before any model call; the inference
tests prove a delivered report is governed, replayable and leaves the
target untouched. Facts about report quality are out of scope: Orket
verifies delivery and source references, not findings.
"""
import json
import os
import re
import subprocess
import unittest
import urllib.request
from pathlib import Path

from integration.support import IntegrationCase, Sandbox, env_setting, git

SRC = env_setting("GARDENER_IT_ORKET_SRC")
PY = env_setting("GARDENER_IT_ORKET_PYTHON")
CATALOG = env_setting("GARDENER_IT_ORKET_CATALOG")
MODEL = env_setting("GARDENER_IT_ORKET_MODEL")
PROVIDER = env_setting("GARDENER_IT_ORKET_PROVIDER", "ollama")
BASE_URL = env_setting("GARDENER_IT_ORKET_BASE_URL", "http://127.0.0.1:11434")
INFERENCE = env_setting("GARDENER_IT_ORKET_INFERENCE") in ("1", "true")
CONFIGURED = all((SRC, PY, CATALOG, MODEL))
REASON = "set GARDENER_IT_ORKET_SRC/_PYTHON/_CATALOG/_MODEL to run the Orket tier"


def deliver_env() -> dict[str, str]:
    raw = env_setting("GARDENER_IT_ORKET_DELIVER_ENV")
    return {k: str(v) for k, v in json.loads(raw).items()} if raw else {}


def large_env() -> dict[str, str]:
    raw = env_setting("GARDENER_IT_ORKET_LARGE_ENV")
    return {k: str(v) for k, v in json.loads(raw).items()} if raw else {}


def orket_version() -> tuple:
    """The Orket checkout's version, from its pyproject.toml; (0,) if unknown."""
    try:
        text = (Path(SRC) / "pyproject.toml").read_text()
    except OSError:
        return (0,)
    match = re.search(r'^version\s*=\s*"([0-9.]+)"', text, re.M)
    return tuple(int(p) for p in match.group(1).split(".")) if match else (0,)


AT_LEAST_084 = CONFIGURED and orket_version() >= (0, 8, 4)
AT_LEAST_085 = CONFIGURED and orket_version() >= (0, 8, 5)


@unittest.skipUnless(CONFIGURED, REASON)
class OrketCase(IntegrationCase):
    def box(self, model: str | None = None, **settings: str) -> Sandbox:
        box = Sandbox(self)
        box.command_harness([PY, f"{SRC}/examples/gardener_harness/gardener_harness.py"], model=model or MODEL)
        box.extra_env.update({
            "ORKET_GARDENER_CATALOG": CATALOG,
            "ORKET_GARDENER_PROVIDER": PROVIDER,
            "ORKET_GARDENER_PROVIDER_BASE_URL": BASE_URL,
            "ORKET_GARDENER_WORK_DIR": str(box.root / "orket-work"),
            "ORKET_DISABLE_SANDBOX": "1",
        })
        box.extra_env.update(settings)
        return box

    def run_areas(self, box: Sandbox) -> list:
        work = box.root / "orket-work"
        return sorted(work.glob("run-*")) if work.exists() else []

    def assertRefused(self, r, *needles: str):
        self.assertEqual(r.code, 1, r.describe())
        self.assertIsNotNone(r.envelope, r.describe())
        self.assertTrue(r.envelope["is_error"], r.envelope)
        for needle in needles:
            self.assertIn(needle, r.envelope["result"])
        self.assertNotPushed(r)

    def inspect(self, box: Sandbox, r) -> dict:
        run_id = r.envelope["session_id"].removeprefix("orket:")
        db = next(box.root.glob("orket-work/run-*/agent.sqlite3"))
        out = subprocess.run([PY, "-m", "orket.cli", "agent", "inspect", run_id, "--db", str(db), "--json"],
                             capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout)


class Refusals(OrketCase):
    """Each of these must refuse in seconds, before the model is called."""

    def test_write_modes_are_refused_by_orket_itself(self):
        for flag in ("--implement", "--file-issue"):
            with self.subTest(flag=flag):
                box = self.box()
                box.settings["GARDENER_HARNESS_ALLOW_UNSCOPED"] = "1"  # let gardener pass it on
                r = box.align(flag)
                self.assertRefused(r, "report mode only")
                self.assertEqual(self.run_areas(box), [], "a run area was created")
                self.assertCloneUntouched(r)

    def test_dirty_target_is_refused(self):
        box = self.box()
        clone = box.seed_target()
        (clone / "README.md").write_text("uncommitted edit\n")
        r = box.align()
        self.assertRefused(r, "clean committed")
        self.assertEqual(r.clone_porcelain.strip(), "M README.md")

    def test_symlinked_selected_file_is_refused_without_leaking(self):
        box = self.box()
        secret = box.root / "outside.txt"
        secret.write_text("GARDENER_IT_SECRET_SENTINEL\n")
        clone = box.seed_target()
        (clone / "README.md").unlink()
        (clone / "README.md").symlink_to(secret)
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "symlink README")
        r = box.align()
        self.assertRefused(r)
        self.assertNotIn("GARDENER_IT_SECRET_SENTINEL", json.dumps(r.records))
        self.assertCloneUntouched(r)

    def test_oversized_file_is_refused_not_truncated(self):
        box = self.box()
        box.seed_target(files={"README.md": "# big\n" + "x" * 20_000 + "\n"})
        r = box.align()
        self.assertRefused(r)
        self.assertCloneUntouched(r)

    def test_unreachable_provider_is_blocked(self):
        box = self.box(ORKET_GARDENER_PROVIDER_BASE_URL="http://127.0.0.1:9")
        r = box.align()
        self.assertRefused(r, "UNAVAILABLE")
        self.assertTrue(r.envelope["blocked"], r.envelope)
        self.assertLess(r.record["seconds"], 120)
        self.assertCloneUntouched(r)

    def test_unknown_model_is_blocked_without_fallback(self):
        box = self.box(model="gardener-it-no-such-model:1b")
        r = box.align()
        self.assertRefused(r)
        self.assertTrue(r.envelope["blocked"], r.envelope)
        self.assertCloneUntouched(r)

    def test_input_budget_refuses_before_inference(self):
        box = self.box(ORKET_GARDENER_MAX_INPUT_TOKENS="256")
        r = box.align()
        self.assertRefused(r, "E_MODEL_INPUT_BUDGET", "not truncated")
        self.assertCloneUntouched(r)

    def test_output_request_above_host_ceiling_is_refused(self):
        settings = deliver_env()
        settings["ORKET_GARDENER_MAX_OUTPUT_TOKENS"] = "8192"
        box = self.box(**settings)
        r = box.align()
        self.assertRefused(r, "E_MODEL_OUTPUT_LIMIT")
        self.assertCloneUntouched(r)


@unittest.skipUnless(INFERENCE, "set GARDENER_IT_ORKET_INFERENCE=1 to run the tests that call the model")
class Inference(OrketCase):
    def test_delivered_report_is_governed_and_replayable(self):
        box = self.box(**deliver_env())
        r = box.align()
        self.assertEqual(r.code, 0, r.describe())
        self.assertSafeReport(r)
        self.assertFalse(r.envelope["is_error"])
        self.assertIn("verified delivery and source references only", r.stdout)
        self.assertEqual(r.run["outcome"], "report")
        if AT_LEAST_084:
            # Orket 0.8.4 (McElyea/Orket#9): a delivered Gardener report
            # carries a real summary line, not a title or a template.
            self.assertRegex(r.stdout, r"GARDENER_SUMMARY:\s*\S")
            self.assertNotIn("<N>", r.run["gap_summary"])

        report = self.inspect(box, r)
        self.assertEqual(report["final_truth"]["completion_classification"], "completion_satisfied")
        calls = report["iterations"][0]["model_calls"]
        self.assertEqual(len(calls), 1, "Orket made more than one model call")
        receipt = calls[0]["receipt"]
        self.assertEqual(receipt["provider"], PROVIDER)
        self.assertEqual(receipt["model"], MODEL)

        run_id = r.envelope["session_id"].removeprefix("orket:")
        db = next(box.root.glob("orket-work/run-*/agent.sqlite3"))
        replay = subprocess.run([PY, "-m", "orket.cli", "agent", "replay", run_id, "--db", str(db), "--json"],
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(json.loads(replay.stdout).get("status"), "matched", replay.stdout[-500:])

        if PROVIDER == "ollama":
            # Orket 0.8.2+ sends an explicit num_ctx (input + output budget)
            # instead of inheriting the server's default context.
            with urllib.request.urlopen(f"{BASE_URL}/api/ps", timeout=10) as resp:
                loaded = {m["name"]: m for m in json.load(resp)["models"]}
            if MODEL in loaded:
                settings = deliver_env()
                expected = int(settings.get("ORKET_GARDENER_MAX_INPUT_TOKENS", 8192)) + 2048
                self.assertEqual(loaded[MODEL].get("context_length"), expected)

    @unittest.skipUnless(AT_LEAST_084, "needs Orket 0.8.4+ (McElyea/Orket#8)")
    def test_a_timed_out_run_is_closed_and_named(self):
        """Orket 0.8.4: a timeout closes the run as failed, with an end
        time and final truth, and the envelope names it for inspection."""
        settings = deliver_env()
        box = self.box(**settings)
        box.settings["GARDENER_ALIGN_TIMEOUT"] = "60"
        r = box.align()
        self.assertEqual(r.code, 1, r.describe())
        self.assertTrue(r.envelope["is_error"])
        self.assertTrue(r.envelope["session_id"].startswith("orket:agent-run:"), r.envelope)
        self.assertCloneUntouched(r)
        report = self.inspect(box, r)
        self.assertEqual(report["run"]["lifecycle_state"], "failed_terminal")
        attempt = report["attempts"][0]
        self.assertEqual(attempt["attempt_state"], "attempt_failed")
        self.assertIsNotNone(attempt["end_timestamp"])
        self.assertEqual(report["final_truth"]["completion_classification"], "completion_unsatisfied")

    @unittest.skipUnless(AT_LEAST_084 and large_env(),
                         "needs Orket 0.8.4+ (McElyea/Orket#7) and GARDENER_IT_ORKET_LARGE_ENV")
    def test_input_over_64_kib_reaches_the_model(self):
        """Orket 0.8.4: inputs over 64 KiB used to fail with
        E_AGENT_CHILD_DISCONNECTED before inference. A 60 s budget keeps
        this short: reaching the model and timing out is a pass."""
        source = Path(self.box().root) / "large-conventions"
        import shutil

        from integration.support import FIXTURES

        shutil.copytree(FIXTURES / "conventions", source)
        for i in range(8):  # 8 x 12 KB = 96 KB, under the 16 KiB per-file cap
            (source / "docs" / f"PAD_{i}.md").write_text(("Padding for the size test. " * 460)[:12_000])
        box = self.box(**large_env())
        box.settings["GARDENER_ALIGN_TIMEOUT"] = "60"
        r = box.align(conventions_url=box.conventions(source=source, compact_copy=False))
        result = r.envelope["result"] if r.envelope else r.stderr
        self.assertNotIn("CHILD_DISCONNECTED", result)
        inputs = next(box.root.glob("orket-work/run-*/inputs.json"))
        self.assertGreater(inputs.stat().st_size, 65_536, "the input was not over 64 KiB")
        report = self.inspect(box, r)
        self.assertEqual(len(report["iterations"][0]["model_calls"]), 1, "the model was never called")
        self.assertCloneUntouched(r)

    def test_target_changed_during_the_run_is_refused(self):
        box = self.box(**deliver_env())
        clone = box.seed_target()
        work = box.root / "orket-work"
        script = (
            "import pathlib,sys,time\n"
            "work, readme = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "deadline = time.time() + 600\n"
            "while time.time() < deadline and not list(work.glob('run-*/inputs.json')):\n"
            "    time.sleep(0.5)\n"
            "time.sleep(2)\n"
            "readme.write_text(readme.read_text() + 'changed while the model ran\\n')\n"
        )
        changer = subprocess.Popen([PY, "-c", script, str(work), str(clone / "README.md")])
        try:
            r = box.align()
        finally:
            changer.wait(timeout=700)
        self.assertEqual(r.code, 1, r.describe())
        self.assertTrue(r.envelope["is_error"], r.envelope)
        self.assertNotIn("verified delivery", r.stdout)
        self.assertNotPushed(r)
        if AT_LEAST_085:
            # Orket 0.8.5: a delivery refused after the run still names the
            # retained run and the exact command to inspect it.
            self.assertTrue(r.envelope["session_id"].startswith("orket:agent-run:"), r.envelope)
            self.assertIn("orket agent inspect", r.envelope["result"])
            self.inspect(box, r)


if __name__ == "__main__":
    unittest.main()
