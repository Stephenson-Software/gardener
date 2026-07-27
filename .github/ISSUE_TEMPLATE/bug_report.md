---
name: Bug report
about: gardener did something wrong, crashed, or dispatched incorrectly
title: ""
labels: bug
assignees: ""
---

**Command**
Which one: `align` / `tend` / `garden` / `overnight` / `allowlist` /
`status`, and the flags you passed (redact `--repo` if it points at a
private repo — a description like "a private Python repo with no CI" is
enough).

**What happened**
A clear description of the incorrect behavior, including any
`gardener:`-prefixed stderr output.

**Expected behavior**
What you expected instead.

**Does this touch dispatch safety?**
Did this involve what tools/permissions a dispatched `claude` session had,
or a merge that shouldn't have happened (or didn't happen when it should
have)? If so, say so explicitly — see `CONTRIBUTING.md`, this class of bug
gets prioritized differently.

**Environment**
OS, Python version (`python3 --version`), `claude --version`, and gardener
commit/version.

**Additional context**
Anything else that would help track this down.
