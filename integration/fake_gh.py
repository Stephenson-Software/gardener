"""A local stand-in for the GitHub CLI, for the offline integration tier.

Installed as `gh` on the sandbox's PATH. Every call is appended to
`$GARDENER_IT_GH_LOG`. With `$GARDENER_IT_GH_REMOTES` set, it serves the
read-only calls gardener makes from local bare repositories at
`<remotes>/<owner>/<name>.git`:

- `repo view R --json defaultBranchRef ...` prints `main`;
- `repo clone R DEST -- ARGS` clones the bare repository;
- `pr list ...` and `issue list ...` print `[]`.

Every other call fails, so a test can assert that nothing tried to write.
`claude`'s own startup call, `auth token`, also fails, as it does with no
GitHub login.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    log = os.environ.get("GARDENER_IT_GH_LOG")
    if log:
        with open(log, "a") as f:
            f.write(" ".join(argv) + "\n")
    remotes = os.environ.get("GARDENER_IT_GH_REMOTES")
    if remotes and len(argv) >= 3:
        if argv[:2] == ["repo", "view"] and "defaultBranchRef" in argv:
            print("main")
            return 0
        if argv[:2] == ["repo", "clone"]:
            source = Path(remotes) / f"{argv[2]}.git"
            if not source.is_dir():
                print(f"GraphQL: Could not resolve to a Repository with the name '{argv[2]}'.", file=sys.stderr)
                return 1
            extra = argv[argv.index("--") + 1:] if "--" in argv else []
            return subprocess.run(["git", "clone", "-q", *extra, source.as_uri(), argv[3]]).returncode
    if argv[:2] in (["pr", "list"], ["issue", "list"]) and remotes:
        print("[]")
        return 0
    print("gh is not available in the offline integration tier", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
