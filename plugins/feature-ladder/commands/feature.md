---
description: Add a feature with the orchestration ladder (Fable coordinates sparingly, Opus designs, routed Sonnet/Haiku workers build, Hindsight remembers). Usage - /feature-ladder:feature <description> [--adversarial] [--ultra]
argument-hint: <feature description> [--adversarial] [--ultra]
---

Start a feature-ladder run for: $ARGUMENTS

Spawn the coordinator and hand it the request verbatim. Do not scope, design, or
implement anything yourself in this conversation; the coordinator owns the run.

```
Agent(
  subagent_type="feature-ladder:coordinator",
  model="fable",
  effort="medium",
  name="feature-coordinator",
  prompt="Feature request from the user: $ARGUMENTS

Project: infer the project name from the repo (package name, pyproject/package.json name, or directory name) and use it as the `project:<name>` tag for all Hindsight calls.
Ultra rung: " + ("ENABLED by the user (--ultra present)" if "--ultra" in "$ARGUMENTS" else "NOT enabled; never use ultra-investigator") + ".
Codex adversarial tests: " + ("ENABLED by the user (--adversarial present)" if "--adversarial" in "$ARGUMENTS" else "not requested; run only the Codex second-opinion review") + ".
Follow the orchestration-ladder, complexity-router, skill-selector and hindsight-memory skills exactly. Recall first. Route every task with route.py, then pick its skills with select_skills.py --inject and paste that block into the card. Delegate everything you can. Three inspection passes maximum. Retain at acceptance. Report back with FEATURE-REPORT.md path, files changed, the exact passing check commands, anything unverified, and what was retained."
)
```

When the coordinator returns, relay its report to the user as-is, then add one
line pointing at FEATURE-REPORT.md.
