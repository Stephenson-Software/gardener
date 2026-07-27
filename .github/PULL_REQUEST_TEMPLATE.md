## Summary

<!-- What changed and why. Reference the issue with "Closes #N" if applicable. -->

## Safety model impact

<!--
Delete this section if your change doesn't touch dispatch.py/MODE_SPECS.
Otherwise, per CLAUDE.md's "Conventions" section:
-->

- [ ] `bypassPermissions` (or any equivalent) is still unreachable under
      every mode/flag combination
- [ ] Any new/changed `--allowedTools` pattern is as narrow as the mode
      actually needs, not a broad catch-all
- [ ] Any behavioral claim about the `claude` CLI added to `dispatch.py`'s
      docstring was confirmed against a real invocation, not assumed

## Doc sync check

<!-- Delete any line that doesn't apply to this PR. -->

- [ ] `README.md` updated for any CLI flag, safety-model, or behavior
      change
- [ ] `CLAUDE.md` updated if this changes a convention or hard rule

## Test plan

- [ ] `PYTHONPATH=. python3 -m unittest discover -s tests -v` passes
      locally (also runs in CI on this PR)
- [ ] If this changes `dispatch.py`'s real interaction with the `claude`
      CLI, describe how you verified it manually (mocked tests alone
      aren't sufficient for a new CLI-behavior claim — see `CLAUDE.md`'s
      "Grounding work in research" section)
