"""gardener: dispatches Claude Code against a fleet of software repos.

`align` checks one target repo at a time against a conventions repo you
configure (`$GARDENER_CONVENTIONS_URL`) and, if authorized, fixes what's
missing. `tend`/`garden`/`overnight` make real, broader progress on a repo
(or a whole opt-in list of them, unattended overnight) by dispatching that
repo's own dev-loop skill.

Orchestration and safety-gating live here in Python; the actual
reading/analysis/implementation judgment is delegated to a dispatched
Claude Code CLI invocation. See README.md for the full picture, including
the "Conventions repo" section on authoring the repo `align` reads.
"""

__version__ = "0.2.0.dev20260808"
