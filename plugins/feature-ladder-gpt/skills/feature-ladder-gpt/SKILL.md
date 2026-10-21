---
name: feature-ladder-gpt
description: Implement features using Feature Ladder's deterministic GPT model routing, explicit skill injection, scoped coding agents, evidence checks, and escalation rules. Also use to scope a feature or inspect its model and skill route.
---

# Feature Ladder GPT

Use the bundled executable rules to choose every worker's model, effort and
skills. Delegate through the dispatcher; do not substitute personal model
preferences. This skill explicitly authorizes the workflow's scoped subagents.

The plugin root is two directories above this SKILL.md. Set `<root>` to that
absolute directory. Read [the Codex adapter](references/codex-adapter.md).
Python 3.11+ is required. No pip packages are required.

In ChatGPT desktop, also read [ChatGPT execution](references/chatgpt-desktop.md)
and use its `exec` transport for all dispatches. A skill cannot give an ordinary
chat session model-selection or local command capabilities it does not have.

## Entry flow

1. Separate the feature request from `--auto`, `--codex`, `--adversarial`,
   `--ultra`. Scope-only and route/skills inspection are supported without
   starting implementation. Adversarial implies review; neither security
   alone nor ordinary medium work enables adversarial tests.
2. Run `python <root>/skills/complexity-router/scripts/intake.py find "<request>"`
   from the target repo. Reuse a matching confirmed intake if found.
   Otherwise dispatch a task with `phase: scope`. Its scoper runs scan,
   settles the variables with evidence, and returns the proposal and questions.
3. Ask only the questions returned by `intake.py decide`; preserve prior
   authorization. `--auto` uses proposed values and recommended options, but
   does not grant new external permissions. Save answers and run
   `intake.py apply <proposal.json> <answers.json>`. For scope-only, return here.
4. Run the original branch helper at
   `<root>/skills/orchestration-ladder/scripts/pr_checkpoint.py start --slug <slug> --workspace <include|stash> --intake <confirmed.json>`.
   Honor the intake's workspace choice. Never silently stash or commit
   unrelated work. Existing sandbox and user permissions remain binding.
5. Dispatch `phase: coordinate`, including confirmed intake path, complete
   request, branch/base, size budget, authorized outside policy and explicit
   review/adversarial/ultra switches. The coordinator follows the preserved
   protocol, routes work, verifies with `verify.py`, records evidence and
   finishes the PR stack when authorized.
6. Return the report, passing checks, PR or local artifact, assumptions,
   unresolved work and measured usage. Never imply an unavailable reviewer,
   memory connector or model ran.

## Dispatch every role the same way

Write a JSON task file with `id`, `text`, `phase`, router fields, `paths`,
`checks`, `memory`, and any accepted design reference. Pass ONLY facts;
`model`, `effort`, `agent` and `role` overrides are rejected.

```json
{
  "id": "T1", "phase": "work",
  "text": "Implement the accepted retry behavior and return an Evidence Packet.",
  "kind": "code", "files": 3, "loc": 120, "modules": 2,
  "concurrency": true, "ambiguity": 0, "novelty": 0,
  "blast_radius": 1, "design_agreed": true,
  "paths": ["src/retry.ts", "src/retry.test.ts"],
  "checks": ["npm test -- src/retry.test.ts"], "memory": "none"
}
```

With trusted plugin hooks, run
`python <root>/scripts/dispatch.py marker --task <task.json> --cwd <repo>`.
Pass the resulting object verbatim to `collaboration.spawn_agent` / `spawn_agent`.
The hook recomputes the route and replaces the marker with the complete brief
and skill text before the agent starts. Preserve `fork_turns: none`.

Without hooks, use `dispatch.py spawn` instead: it prints the complete native
spawn arguments with all skills embedded. Pass them unchanged. The output can
be large; do not truncate or summarize it. Alternatively use `dispatch.py exec`
to stream the full prompt directly into a separate Codex CLI session with
explicit `--model` and reasoning effort. Its default sandbox is read-only;
`--sandbox workspace-write` is for an authorized coding task within the
current permission boundary. Never retry an unavailable model on a different
model. A runtime without explicit model selection cannot execute this ladder.

`dispatch.py prepare --task <task.json> --out <audit.json>` records the route,
selected/dropped/unresolved skills, hashes, and the exact spawn payload without
launching an agent. Treat the audit as local task data, not a public artifact.

For role responsibilities, source-derived briefs live in `<root>/agents/`.
The dispatcher loads them and their protocol preloads automatically. Additional
task skills use the original selector rules and budgets. Missing optional rule
matches are reported; an explicitly requested missing or over-budget skill
blocks dispatch. A zero-budget mechanic receives zero additional task skills.
