---
description: Add a feature with the orchestration ladder. A Sonnet scout settles every routing variable from your code; you answer only what is uncertain, once; then it runs to completion on its own branch, live changes included under the policy you chose, and opens PRs. Usage - /feature-ladder:feature <description> [--auto] [--codex] [--adversarial] [--ultra]
argument-hint: <feature description> [--auto] [--codex] [--adversarial] [--ultra]
---

Start a feature-ladder run for: $ARGUMENTS

You (this conversation) run the intake and the questions, then hand off. Do not
scope, design, or implement anything yourself. Use `python` instead of
`python3` if that is the interpreter on this machine.

## 0. Switches

Separate the request text from these switches. There are only four:

| Switch | Effect |
|---|---|
| `--auto` | Ask nothing. Use the scout's values and the recommended option for every decision. |
| `--codex` | OpenAI Codex reviews all the code before acceptance; confirmed bugs are fixed. (Automatic only for size L features.) |
| `--adversarial` | Codex also writes and runs attack tests. Implies `--codex`. Never automatic; use it at the end of a phased rollout. |
| `--ultra` | Allows the ultra-investigator (Fable max) as the last escalation. |

Everything else is detected: the branch to start from is the current one; PRs
open when there is a remote and the `gh` CLI, otherwise the same branches are
made locally; a saved intake for this exact request is reused (step 1).

## 1. Intake (nothing is delegated before this finishes)

First check for a saved intake:
```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/intake.py" find "<request text>"
```
If it prints a path, that intake was confirmed by the user earlier
(`/feature-ladder:scope`, or an aborted run): use it, tell the user in one line
"Reusing the intake you confirmed at <time>", and skip to step 4.

Otherwise dispatch the scout:
```
Agent(subagent_type="feature-ladder:scoper", model="sonnet", effort="medium", name="feature-scoper",
      prompt="Feature request: <request text>\nMode: <auto | default>\nFollow your instructions exactly.")
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
the user in one line: the size and what it means (`budget.shape`), the rung,
the outside-repo policy, and "Running to completion now; no more questions."
For example: "Size M: one Sonnet-high worker, no design doc, no Codex. Live
changes: all. Running now."

## 4. Branch

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/pr_checkpoint.py" start --slug <slug> --workspace <include|stash from the intake> --intake .feature-ladder/intake/<slug>.json
```
Exit 2 means the folder is not a git repo: continue without branches. Mention
`pr_disabled_reason` in one line if PRs are off.

## 5. Hand off

Adversarial is ENABLED only if `--adversarial` was given. Codex review is
ENABLED if `--codex` or adversarial was given, or the intake's `codex.review`
is true (size L; quote its `why`). Secrets: the intake's `secrets.names`
lists the Secret Manager secrets the feature reads (names only).

```
Agent(
  subagent_type="feature-ladder:coordinator",
  model="fable",
  effort="medium",
  name="feature-coordinator",
  prompt="Feature request from the user: <request text>

Confirmed intake (binding): <path to .feature-ladder/intake/<slug>.json>
Budget: size <S|M|L> - <budget.shape>
Run branch: <branch from step 4, or 'none - not a git repo'>  Base: <base>  PRs: <enabled | disabled: reason>
Project: infer the project name from the repo (package name, pyproject/package.json name, or directory name) and use it as the `project:<name>` tag for all Hindsight calls.
Ultra rung: <ENABLED by the user (--ultra) | NOT enabled; never use ultra-investigator>.
Codex second opinion: <ENABLED (--codex | intake: <why>) | not needed for this feature (intake)>.
Codex adversarial tests: <ENABLED (--adversarial | intake: security) | not requested>.
Secrets (Google Secret Manager, project <project>): <names, or 'none'>. Values never enter the transcript: workers run commands that need one through with_secrets.py.
Run to completion. Do not stop to ask the user anything: decide non-critical choices yourself, park critical design decisions in checkpoint PRs, and run outside-repo steps under the intake's outside_policy exactly as your Run-to-completion rules say.
Stay within the budget: it is the whole protocol for this feature. Recall once. Build each Task Card with intake.py card. Delegate everything. Finish with pr_checkpoint.py finish. Report back with FEATURE-REPORT.md path, files changed, the exact passing check commands, the PR stack in merge order, assumptions made, outside-repo actions taken or left, anything unverified, and what was retained."
)
```

When the coordinator returns, relay its report as-is, then one line pointing at
FEATURE-REPORT.md and, if there are checkpoint PRs, one line saying which PR
needs the user first.
