"""gardener: dispatches Claude Code against a fleet of software repos.

`align` checks one target repo at a time against dms-conventions and,
if authorized, fixes what's missing. `tend`/`garden`/`overnight` make
real, broader progress on a repo (or a whole opt-in list of them,
unattended overnight) by dispatching that repo's own dev-loop skill.

Orchestration and safety-gating live here in Python; the actual
reading/analysis/implementation judgment is delegated to a dispatched
Claude Code CLI invocation. See README.md for the full picture and
https://github.com/dmccoystephenson/dms-conventions for the conventions
`align` checks repos against.
"""

__version__ = "0.1.0"
