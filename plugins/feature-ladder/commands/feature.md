---
description: Add a feature with the orchestration ladder. Intake first (a Sonnet scout settles every routing variable and you answer only what is uncertain), then it runs to completion on its own branch; critical decisions and irreversible steps become draft PRs instead of stops. Usage - /feature-ladder:feature <description> [--auto | --ask-all] [--adversarial] [--ultra] [--no-pr] [--base <branch>] [--intake <file>]
argument-hint: <feature description> [--auto|--ask-all] [--adversarial] [--ultra] [--no-pr] [--base <branch>] [--intake <file>]
---

Start a feature-ladder run for: $ARGUMENTS

You (this conversation) run the intake and the questions, then hand off. Do not
scope, design, or implement anything yourself. Use `python` instead of
`python3` if that is the interpreter on this machine.

## 0. Switches

Separate the request text from these switches:

| Switch | Effect |
|---|---|
| `--auto` | Ask nothing. Use the scout's values, assume the recommended option for every open decision, and start. |
| `--ask-all` | Confirm every variable, not just the uncertain ones that change the model. |
| `--adversarial` | Codex also writes and runs attack tests before acceptance. |
| `--ultra` | Allows the ultra-investigator (Fable max) as the last escalation. |
| `--no-pr` | Keep everything on local branches; PR bodies are saved to `.feature-ladder/checkpoints/`. |
| `--base <branch>` | Branch to start from (default: the current branch). |
| `--intake <file>` | Reuse an intake confirmed earlier with `/feature-ladder:scope`; skip steps 1-3. |

## 1. Intake scout (nothing is delegated before this finishes)

```
Agent(subagent_type="feature-ladder:scoper", model="sonnet", effort="medium", name="feature-scoper",
      prompt="Feature request: <request text>\nMode: <auto | ask-all | default>\nFollow your instructions exactly.")
```

## 2. Ask (skip entirely with `--auto`)

From the scout's `decide` output:
1. Show a short summary: the predicted rung in one line, then the assumed
   values as one compact line each (`field = value - evidence`). Nothing more.
2. Ask with AskUserQuestion, at most 4 questions per call, in this order:
   `decisions` (the `workspace` one first if present), then `questions`.
   Use each item's `header`, `question` and option `label`/`description` as
   given. Keep the recommended option first.
3. Map each answer back to the option's `value` (for `decisions`, record the
   chosen label). If the user typed their own answer, translate it into the
   field's value and add a note saying so.
4. If the user chose **Stop** for uncommitted changes, stop here.

## 3. Confirm

Write `{"<field>": <value>, ..., "decisions": {"<id>": "<chosen label>"}, "notes": [...]}`
to `.feature-ladder/intake/<slug>-answers.json` and run:
```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/intake.py" apply .feature-ladder/intake/<slug>-proposal.json .feature-ladder/intake/<slug>-answers.json
```
It writes the confirmed intake to `.feature-ladder/intake/<slug>.json`. Tell
the user in one line: the confirmed rung, and "Running to completion now; no
more questions. Critical decisions and irreversible steps will come back as
draft PRs."

## 4. Branch

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/pr_checkpoint.py" start --slug <slug> --workspace <include|stash from the intake> [--base <branch>] [--no-pr]
```
Exit 2 means the folder is not a git repo: continue without branches (the
coordinator then lists irreversible steps in FEATURE-REPORT.md and never runs
them). Mention `pr_disabled_reason` in one line if PRs are off.

## 5. Hand off

```
Agent(
  subagent_type="feature-ladder:coordinator",
  model="fable",
  effort="medium",
  name="feature-coordinator",
  prompt="Feature request from the user: <request text>

Confirmed intake (binding): <path to .feature-ladder/intake/<slug>.json>
Run branch: <branch from step 4, or 'none - not a git repo'>  Base: <base>  PRs: <enabled | disabled: reason>
Project: infer the project name from the repo (package name, pyproject/package.json name, or directory name) and use it as the `project:<name>` tag for all Hindsight calls.
Ultra rung: <ENABLED by the user (--ultra) | NOT enabled; never use ultra-investigator>.
Codex adversarial tests: <ENABLED by the user (--adversarial) | not requested; run only the Codex second-opinion review>.
Run to completion. Do not stop to ask the user anything: decide non-critical choices yourself, and park critical decisions and irreversible steps in checkpoint PRs exactly as your Run-to-completion rules say.
Follow the orchestration-ladder, complexity-router, skill-selector, cross-model-review and hindsight-memory skills exactly. Recall first. Route every task with route.py, then pick its skills with select_skills.py --inject and paste that block into the card. Delegate everything you can. Three inspection passes maximum. Retain at acceptance. Finish with pr_checkpoint.py finish. Report back with FEATURE-REPORT.md path, files changed, the exact passing check commands, the PR stack in merge order, assumptions made, anything unverified, and what was retained."
)
```

When the coordinator returns, relay its report as-is, then one line pointing at
FEATURE-REPORT.md and, if there are checkpoint PRs, one line saying which PR
needs the user first.
