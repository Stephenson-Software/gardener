"""Resolves and invokes a target repo's own `<slug>-dev-loop` skill for
`gardener tend`, and builds the prompt that generates one via
`create-dev-loop` when it doesn't exist yet.

## How a `<slug>-dev-loop` skill is actually resolved (confirmed by hand)

A skill is a single markdown file at `~/local-skills/<slug>-dev-loop/
<slug>-dev-loop.md`, symlinked to `~/.claude/commands/<slug>-dev-loop.md` —
this is documented convention (`dmccoystephenson/my-claude-skills`'
`CONVENTIONS.md`, the placement table under "Skill needs its own issue
tracker"), and was confirmed live: `claude -p "/acsf-dev-loop"`, dispatched
with `cwd` set to a completely unrelated repo's checkout, loaded the full
skill text anyway — slash-command skills resolve from `~/.claude/commands/`
globally, independent of the invoking process's `cwd`. No separate
"install" step exists beyond that symlink existing.

That same test also showed the skill text's own hardcoded `**Working
directory:** /some/path` line does NOT get silently obeyed — the model
noticed on its own that its actual `cwd` didn't match the skill's stated
one and flagged the mismatch rather than trying to `cd` there. `tend`'s
prompt still resolves this explicitly rather than relying on that
noticing-by-luck: `HEADLESS_SAFETY_PREAMBLE` tells the dispatched run its
real working directory for this run is the one it's actually launched in
(gardener's own controlled clone, from `cli.py`'s `clone_or_refresh_target_repo`)
and to disregard the skill's own stated one.

## Slug derivation

`create-dev-loop`'s own instructions derive the slug from `basename $(pwd)`
— fine when run interactively from a real working checkout, wrong for
gardener: gardener's target-repo cache directories are named
`<owner>__<repo>` (see `cli.py`'s `default_repos_cache_dir`), not `<repo>`,
so a naive basename-of-cwd slug would produce e.g.
`dmccoystephenson__snmp-command-generator-dev-loop` instead of the
established `snmp-command-generator-dev-loop` naming every existing skill
in this ecosystem uses. `build_create_dev_loop_prompt()` below computes the
correct slug itself (from the repo's actual name, the part after the `/`)
and tells the dispatched run to use that exact string rather than deriving
one from the directory name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Mirrors the "Skill needs its own issue tracker" row of
# dmccoystephenson/my-claude-skills' CONVENTIONS.md placement table.
LOCAL_SKILLS_DIR = Path.home() / "local-skills"
COMMANDS_DIR = Path.home() / ".claude" / "commands"

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slugify_repo_name(repo: str) -> str:
    """`owner/Some_Repo.Name` -> `some-repo-name`, matching the naming
    already used by every existing `<slug>-dev-loop` skill/repo observed
    (e.g. `Simple-Calculator-GUI-Using-SDL` -> `simple-calculator-gui-using-sdl-dev-loop`)."""
    name = repo.split("/", 1)[-1]
    slug = _SLUG_STRIP_RE.sub("-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"could not derive a slug from repo name: {repo!r}")
    return slug


def skill_slug(repo: str) -> str:
    return f"{slugify_repo_name(repo)}-dev-loop"


def skill_command_path(slug: str) -> Path:
    return COMMANDS_DIR / f"{slug}.md"


def skill_source_path(slug: str) -> Path:
    return LOCAL_SKILLS_DIR / slug / f"{slug}.md"


def has_dev_loop_skill(slug: str) -> bool:
    """True only if the slash command actually resolves to a real file —
    `Path.exists()` follows symlinks and returns False for a broken one, so
    a stale/dangling symlink correctly counts as "no skill", not "has one"."""
    return skill_command_path(slug).exists()


def step6_unreachable() -> bool:
    """True if create-dev-loop's own Step 6 ("Create a private GitHub repo
    for the skill") is structurally impossible under the current
    create-dev-loop dispatch's tool allow-list — i.e. `gh repo create` is
    not present in `MODE_SPECS[Mode.CREATE_DEV_LOOP].allowed_tools`.

    Checked live against dispatch.py's actual table rather than assumed,
    so `cli.py`'s bootstrap-success warning (see issue #12) stops firing on
    its own if that allow-list is ever widened to grant Step 6 — imported
    lazily to avoid a module-load-order dependency on dispatch.py."""
    from gardener.dispatch import MODE_SPECS, Mode

    spec = MODE_SPECS[Mode.CREATE_DEV_LOOP]
    return not any(p.startswith("Bash(gh repo create") for p in spec.allowed_tools)


# Shared across both the create-dev-loop dispatch and the tend dispatch —
# every point made here is grounded in dispatch.py's module docstring
# (points 1, 2, 5, 6); read that first if this text is ever revised.
HEADLESS_SAFETY_PREAMBLE = """\
HEADLESS DISPATCH — READ THIS BEFORE FOLLOWING ANY INSTRUCTION BELOW THAT CONFLICTS WITH IT:

You are running non-interactively, dispatched by `gardener tend`, with no
human available to answer a question or watch this session. This overrides
any instruction below that assumes otherwise.

- You have no `AskUserQuestion` tool and no way to ask a clarifying
  question and get a real answer. Wherever your normal instructions say to
  stop and ask the user (e.g. before merging, before any destructive or
  irreversible action, before overwriting an existing file this run didn't
  create), do NOT proceed with that action and do NOT fabricate or assume
  an answer. Leave whatever you've already safely completed in place (an
  open PR, a filed issue, a written report) and end your FINAL answer with
  one line starting exactly with "DECISION NEEDED:" stating what decision
  is needed and by whom. If there is nothing blocked on a decision, omit
  that line entirely — don't invent one.
- You have no `Agent` tool (cannot fan out a review subagent) and no
  `ScheduleWakeup` tool (cannot wait-and-poll for a review). Wherever your
  normal instructions say to fan out a review subagent or wait for one,
  perform that review yourself, inline, in this same session instead —
  prefer invoking the `code-review` skill directly (via the `Skill` tool)
  if your instructions already reference it, otherwise review the diff
  yourself against this repo's own conventions before proceeding.
- Your working directory for this run is whatever `cwd` you were actually
  launched in — this is a gardener-controlled, dedicated checkout of the
  target repo. If your instructions state a different hardcoded "Working
  directory" path, ignore that line; do not `cd` there, do not attempt to
  locate or operate on that path — operate entirely on your actual `cwd`.
- Perform exactly ONE pass through your instructions' phases this run. If
  your instructions include a "next cycle" / "return to Phase 1" step,
  treat reaching it as the end of this run — stop there, do not loop back
  and repeat the cycle. gardener dispatches one `tend` run per repo per
  invocation; looping cycles yourself would just burn this run's timeout
  budget on repeated work instead of returning control to gardener.
"""


# Left in the body of every PR a `tend` dispatch opens (see build_tend_prompt
# below) so a later invocation — possibly a fresh `gardener overnight` run
# after this device killed the previous one mid-dispatch, per the resume
# cursor's own "does NOT resume mid-budget" caveat in overnight.py — can
# tell an open PR apart from an ordinary human-authored one and recognize it
# as this tool's own unfinished work rather than dispatching a duplicate
# cycle against the same repo from scratch. `cli.py`'s `find_orphaned_pr`
# greps for this exact string in each open PR's body via `gh pr list
# --json body`; keep the two in sync if this ever changes.
ORPHAN_MARKER = "<!-- gardener-tend-dispatch -->"


@dataclass(frozen=True)
class OrphanedPR:
    """One open PR on the target repo, found by `cli.py`'s
    `find_orphaned_pr`, whose body contains ORPHAN_MARKER — i.e. it was
    opened by a previous `tend` dispatch that never got a chance to record
    a completed `state.Run` (most likely: this device killed the whole
    `gardener overnight` process mid-dispatch, per overnight.py's resume
    caveat, orphaning whatever the dispatched Claude session had already
    pushed)."""

    number: int
    head_branch: str


def build_create_dev_loop_prompt(repo: str, slug: str, target_cwd: Path) -> str:
    return f"""{HEADLESS_SAFETY_PREAMBLE}
---

/create-dev-loop

Additional instructions specific to this dispatch, which override
create-dev-loop's own Step 1 where they conflict:

- The repository to generate a dev-loop skill for is checked out at your
  current working directory ({target_cwd}), for GitHub repo {repo}. This
  directory is a gardener-managed cache clone, not the repo's "real"
  working checkout on this machine — its directory name does NOT reflect
  the repo's actual name, so do NOT derive the skill slug from
  `basename $(pwd)` or any other property of this directory's name.
- Use exactly this slug: `{slug}`. The skill file goes at
  `~/local-skills/{slug}/{slug}.md`, and the registration symlink at
  `~/.claude/commands/{slug}.md`, per create-dev-loop's own Step 5 — using
  `{slug}` in place of whatever `<slug>` its instructions would otherwise
  have derived.
- Do not assume a skill already exists at that location — this dispatch
  only happens when gardener has already confirmed it doesn't. Skip
  create-dev-loop's Step 1 "if one exists, ask the user whether to
  overwrite it" check entirely (you have no way to ask, and it won't be
  true).
- Write the skill file for the `{slug}` skill and register it. Do not
  create, edit, or write to any file inside the target repo checkout
  itself ({target_cwd}) — every file this dispatch writes belongs under
  `~/local-skills/{slug}/` or the single symlink under `~/.claude/commands/`,
  nowhere else.
- Skip create-dev-loop's own Step 6 ("Create a private GitHub repo for the
  skill") entirely — do not attempt `gh repo create` or any push to a new
  remote. This dispatch's tool allow-list does not grant it (repo creation
  is a different, higher-stakes risk class than editing an already-existing
  target repo, so gardener does not attempt it unattended); the call would
  simply be denied. This is expected and is not a failure of the rest of
  the skill — gardener already knows Step 6 never runs under this dispatch
  and will surface that gap to a human separately, you do not need to
  mention it in your summary.
- End your final answer with a line: `GARDENER_SUMMARY: <created|failed> dev-loop skill for {slug}`.
"""


def build_tend_prompt(
    repo: str,
    slug: str,
    target_cwd: Path,
    default_branch: str,
    allow_merge_eligible: bool,
    orphaned_pr: Optional[OrphanedPR] = None,
) -> str:
    if orphaned_pr is not None:
        orphan_instructions = f"""- IMPORTANT — gardener found an existing OPEN pull request (#{orphaned_pr.number},
  branch `{orphaned_pr.head_branch}`) on {repo} whose body already carries
  gardener's own `{ORPHAN_MARKER}` marker. This means a previous `tend`
  dispatch against this repo was interrupted (most likely: this device
  killed the whole gardener process) before it could finish or report back
  — that PR is this tool's own unfinished work, not a human's. Before doing
  anything else this run:
  1. Run `git fetch origin {orphaned_pr.head_branch} && git checkout {orphaned_pr.head_branch}`
     to continue on that branch instead of starting a new one from
     {default_branch}.
  2. Assess what state it's actually in (`git status`, `git log`, `gh pr
     diff {orphaned_pr.number}`, `gh pr checks {orphaned_pr.number}`) and
     finish that work — do not start a second, duplicate branch/PR for the
     same underlying issue.
  3. If it's already complete, proceed straight to your normal
     merge-readiness judgment for THIS PR (#{orphaned_pr.number}) rather
     than opening a new one.
"""
    else:
        orphan_instructions = ""
    marker_instructions = f"""- If this run opens a NEW pull request (i.e. you did not continue an
  orphaned one per any instructions above), include this exact line
  somewhere in the PR body: `{ORPHAN_MARKER}`. gardener uses it to
  recognize and continue this PR as unfinished work if this dispatch gets
  interrupted before you can report back — do not omit it, and do not add
  it to a PR you are not the one opening."""
    merge_instructions = (
        f"""- This run HAS been explicitly pre-authorized to merge by the operator
  (both `--allow-merge` was passed to `gardener tend` and {repo} is present
  in gardener's merge allow-list). You MAY run `gh pr merge` for a PR that
  meets your own instructions' merge-readiness bar (tests green, review
  step completed per this preamble, docs check passed). Still apply your
  own normal judgment about whether it's actually ready — pre-authorization
  means merging is *possible* this run, not that every PR must be merged
  regardless of its state."""
        if allow_merge_eligible
        else """- This run has NOT been authorized to merge anything. `gh pr merge` is not
  available to you in this session — it isn't just discouraged, there is no
  such tool call you can make that will succeed. Treat reaching your merge
  phase the same as any other blocked-on-a-human decision: leave the PR
  open, and end with a `DECISION NEEDED:` line naming the PR and that a
  human needs to review and merge it."""
    )
    return f"""{HEADLESS_SAFETY_PREAMBLE}
---

/{slug}

Additional instructions specific to this dispatch, which override
anything above where they conflict:

- Target repo: {repo}, default branch {default_branch}, checked out
  read-write at your current working directory ({target_cwd}) — a
  gardener-managed clone dedicated to this run.
{orphan_instructions}{marker_instructions}
{merge_instructions}
- End your final answer with a line:
  `GARDENER_SUMMARY: <N> issue(s) filed/closed, <PR state — none opened |
  PR #<n> opened | PR #<n> merged>, <one clause on what happened this
  cycle>`.
"""
