"""Comparison matrix: run the same `gardener align` across harnesses,
models and settings, and tabulate what each did.

    PYTHONPATH=. python -m integration.matrix ROWS_FILE [--out DIR] [--repo OWNER/NAME]

The tests answer "does this still hold?"; the matrix answers "how do these
compare?" (which model delivers, how long it takes, what each refuses).
Every row runs in its own kept sandbox under `--out`, with the same
isolation as the tests: own state and cache, no ambient gardener settings,
report mode only.

ROWS_FILE (blank lines and # comments ignored):

    harness NAME = COMMAND...        define a command harness (absolute
                                     paths: it runs inside the target clone)
    NAME HARNESS MODEL CONVENTIONS [SUBDIR] [KEY=VALUE ...]

- HARNESS is `claude` (built in) or a defined name.
- MODEL may be `-` for none.
- CONVENTIONS is `fixture` (the bundled conventions) or a Git URL.
- SUBDIR may be `-` for the conventions root.
- KEY=VALUE pairs are set for that row only.

Without `--repo` the target is the offline fixture; with it, a real GitHub
repo, cloned through `gh`, whose `pushedAt` is checked per row.

Writes summary.tsv and summary.md under --out, plus each row's sandbox
(out.txt, err.txt, records/, state/).
"""
from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

from integration.support import READ_ONLY_GH_CALLS, Sandbox


def parse_rows(text: str) -> tuple[dict[str, list[str]], list[dict]]:
    harnesses: dict[str, list[str]] = {}
    rows: list[dict] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("harness "):
            name, sep, command = line[len("harness "):].partition("=")
            if not sep or not name.strip() or not command.strip():
                raise SystemExit(f"line {n}: expected `harness NAME = COMMAND`")
            harnesses[name.strip()] = shlex.split(command)
            continue
        fields = shlex.split(line)
        if len(fields) < 4:
            raise SystemExit(f"line {n}: expected NAME HARNESS MODEL CONVENTIONS [SUBDIR] [KEY=VALUE ...]")
        name, harness, model, conventions = fields[:4]
        rest = fields[4:]
        subdir = ""
        if rest and "=" not in rest[0]:
            subdir = "" if rest[0] == "-" else rest[0]
            rest = rest[1:]
        settings = {}
        for pair in rest:
            key, sep, value = pair.partition("=")
            if not sep:
                raise SystemExit(f"line {n}: `{pair}` is not KEY=VALUE")
            settings[key] = value
        rows.append({"name": name, "harness": harness, "model": "" if model == "-" else model,
                     "conventions": conventions, "subdir": subdir, "settings": settings})
    return harnesses, rows


def run_row(row: dict, harnesses: dict[str, list[str]], out: Path, repo: str | None) -> dict:
    box = Sandbox(None, repo=repo, root=out / row["name"])
    if row["harness"] == "claude":
        if row["model"]:
            box.settings["GARDENER_HARNESS_MODEL"] = row["model"]
    elif row["harness"] in harnesses:
        box.command_harness(harnesses[row["harness"]], model=row["model"])
    else:
        raise SystemExit(f"row {row['name']}: unknown harness `{row['harness']}`")
    if row["subdir"]:
        box.settings["GARDENER_CONVENTIONS_SUBDIR"] = row["subdir"]
    box.extra_env.update(row["settings"])
    box.extra_env.setdefault("ORKET_GARDENER_WORK_DIR", str(box.root / "orket-work"))
    url = None if row["conventions"] == "fixture" else row["conventions"]
    r = box.align(conventions_url=url)
    (box.root / "out.txt").write_text(r.stdout)
    (box.root / "err.txt").write_text(r.stderr)
    envelope = r.envelope or {}
    return {
        "name": row["name"], "harness": row["harness"], "model": row["model"] or "-",
        "subdir": row["subdir"] or "-", "exit": r.code, "seconds": round(r.seconds),
        "clean": r.clone_porcelain == "" and r.clone_head_before == r.clone_head_after,
        "pushed": any(not c.startswith(READ_ONLY_GH_CALLS) for c in r.gh_calls) if box.offline else (r.pushed_at_before != r.pushed_at_after),
        "blocked": envelope.get("blocked", "-"),
        "result": _summary(r),
    }


def _summary(r) -> str:
    """The recorded run's summary, else gardener's last stderr line."""
    text = (r.run or {}).get("gap_summary") or ""
    if not text:
        lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
        text = lines[-1] if lines else ""
    return " ".join(text.split())[:140]


COLUMNS = ["name", "harness", "model", "subdir", "exit", "seconds", "clean", "pushed", "blocked", "result"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m integration.matrix", description=__doc__.split("\n\n")[0])
    parser.add_argument("rows", type=Path)
    parser.add_argument("--out", type=Path, default=Path("matrix-out") / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    parser.add_argument("--repo", default=None, help="real GitHub target (default: the offline fixture)")
    args = parser.parse_args(argv)

    harnesses, rows = parse_rows(args.rows.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    tsv = args.out / "summary.tsv"
    tsv.write_text("\t".join(COLUMNS) + "\n")
    for row in rows:
        print(f"== {row['name']} ({row['harness']}, {row['model'] or 'default model'})", flush=True)
        result = run_row(row, harnesses, args.out, args.repo)
        results.append(result)
        with tsv.open("a") as f:
            f.write("\t".join(str(result[c]) for c in COLUMNS) + "\n")
        print(f"   exit={result['exit']} {result['seconds']}s clean={result['clean']} "
              f"pushed={result['pushed']} blocked={result['blocked']}\n   {result['result']}", flush=True)

    md = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    md += ["| " + " | ".join(str(r[c]).replace("|", "\\|") for c in COLUMNS) + " |" for r in results]
    (args.out / "summary.md").write_text("\n".join(md) + "\n")
    print(f"summary: {tsv}")
    unsafe = [r["name"] for r in results if not r["clean"] or r["pushed"]]
    if unsafe:
        print(f"UNSAFE: {', '.join(unsafe)} changed the clone or pushed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
