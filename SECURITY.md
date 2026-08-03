# Security Policy

## Reporting a vulnerability

gardener dispatches Claude Code headlessly and can merge PRs unattended
overnight. If you find a way to defeat its safety model — for example, a
path that reaches `bypassPermissions`, a tool-scoping gap that lets a
headless `tend` run merge without going through the merge allow-list, or a
prompt-injection vector in a target repo that escalates beyond what
`docs/SAFETY.md` describes — please report it privately via the
maintainer's [GitHub profile](https://github.com/dmccoystephenson) rather
than opening a public issue. Include:

- The mode involved (`align`, `tend`, `overnight`, `file-issue`, …)
- What unsafe behavior it enables
- A minimal repro (a description of the target-repo content or invocation
  that triggers it is enough — no need to share a private repo)

You should expect an initial response within a few days.

## Trust model

gardener dispatches `claude` headlessly against repos you point it at, and
in `tend`/`overnight` mode can create branches, open PRs, push commits, and
merge PRs via `gh`/`git` without a human present to approve individual tool
calls. **Only point gardener at repositories you trust**, and only add a
repo to the `overnight` opt-in list once you trust its generated dev-loop
skill to run unattended.

The full safety model — the `--tools`/`--permission-mode`/`--allowedTools`
layering per mode, why `bypassPermissions` is a hard reject enforced in
`dispatch.py` rather than a convention, how headless `tend` handles the
"ask a human before merging" problem with nobody there to ask, and the
merge allow-list mechanics (`merge_allowlist.py` defaults to
nothing-allowed when its config file is missing) — is documented in
**[docs/SAFETY.md](docs/SAFETY.md)**. Read it before running `tend` or
`overnight` against anything you haven't reviewed.

A repository crafted to manipulate a dispatched run (e.g. planted
instructions in `CLAUDE.md` aimed at the agent rather than at humans) is
the same class of risk as running any AI coding agent against untrusted
input — treat it accordingly. This policy covers gardener's dispatch and
safety-gating layer. It does not cover the behavior of a generated
`<slug>-dev-loop` skill once dispatched — that's covered by
[create-dev-loop's security policy](https://github.com/dmccoystephenson/create-dev-loop/blob/main/SECURITY.md).
