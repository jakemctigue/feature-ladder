---
description: Intake only - a Sonnet scout settles every routing variable for a feature, you answer only what is uncertain, and the confirmed intake is saved for a later run. Nothing is built. Usage - /feature-ladder:scope <description> [--ask-all]
argument-hint: <feature description> [--ask-all]
---

Run feature-ladder intake (no build) for: $ARGUMENTS

Do exactly steps 0-3 of the `/feature-ladder:feature` command (switches,
scoper agent, questions, `intake.py apply`). Accept `--ask-all`; there is no
`--auto` here because asking is the point.

Then stop and show:
- the confirmed rung (agent, model, effort, score) and which fields drove it,
- every field with its value and source (you, or the scout and its confidence),
- the decisions you answered,
- the command to build it later:
  `/feature-ladder:feature --intake .feature-ladder/intake/<slug>.json [--adversarial]`

Do not create branches and do not dispatch any other agent.
