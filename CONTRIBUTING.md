# Contributing to gardener

Thanks for considering a contribution. gardener is a safety-gated Python
CLI that dispatches Claude Code against a fleet of software repos — see
[`README.md`](README.md) for the full command set and design, and
[`CLAUDE.md`](CLAUDE.md) for the canonical project conventions (it's
written for an AI coding agent, but the rules apply equally to human
contributors).

## Before you start

- **Small fixes** (typos, a broken doc link, a missing test case) — just
  open a PR.
- **Anything that touches `dispatch.py` or its safety model** (tool
  scoping, permission modes, what `MODE_SPECS` allows) — open an issue
  first. `bypassPermissions` (or any equivalent auto-approve-everything
  mode) must never become reachable, for any mode, under any flag
  combination; see `CLAUDE.md`'s "Conventions" section and `dispatch.py`'s
  module docstring for why this is a hard rule, not a style preference.
- **A note on running this yourself:** `align` clones a private
  conventions repo (`dms-conventions`), and `tend` bootstraps a target
  repo's dev-loop skill via another private repo (`create-dev-loop`) —
  see the callout near the top of `README.md`. You can read, test, and
  modify gardener's own code without either, but exercising `align`/`tend`
  end to end against a real target repo requires your own equivalents.

## Project conventions

The full rules live in [`CLAUDE.md`](CLAUDE.md); the ones that come up
most for a typical contribution:

- **Stdlib-only Python.** No pip dependencies beyond the standard library.
  If you think a change genuinely needs one, justify it explicitly in
  `README.md`'s Architecture section as part of the same PR.
- Safety constraints for a `claude` invocation live in `dispatch.py`'s
  `MODE_SPECS`, not scattered across callers — don't add a one-off flag
  from `cli.py` directly.
- Any behavioral claim about the `claude` CLI in `dispatch.py`'s docstring
  must be confirmed against a real invocation before being relied on, not
  assumed from `--help` text.

## Making a change

1. Create a branch: `feature/<short-name>` for new capability,
   `fix/<short-name>` for corrections (see `CLAUDE.md`'s "Commit and PR
   conventions" for the full prefix list this repo uses, e.g. `docs/`,
   `ci/`, `chore/`).
2. Make your change, updating `README.md`/`CLAUDE.md` in the same PR if
   your change affects behavior they document (see `CLAUDE.md`'s
   "Documentation sources of truth" section).
3. Run the test suite:

   ```bash
   PYTHONPATH=. python3 -m unittest discover -s tests -v
   ```

   A passing run ends with `OK`. CI runs this automatically against
   Python 3.10 and the latest 3.x on every push/PR to `main`
   (`.github/workflows/ci.yml`). `test_dispatch.py` mocks
   `subprocess.run` and must never actually invoke `claude`/`gh` for real
   — if your change to that module needs a real invocation to verify (a
   new `claude` CLI behavior, for instance), do that manually and record
   what you observed in the PR description; don't add it to the automated
   suite.
4. Commit using imperative mood, no trailing period (e.g. `Add
   GARDENER_STATE_DIR override`).
5. Open a PR referencing any related issue with `Closes #N`, describing
   what you tested and how.

## License

By contributing, you agree that your contributions will be licensed under
the project's [MIT License](LICENSE).
