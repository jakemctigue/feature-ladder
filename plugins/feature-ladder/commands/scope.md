---
description: Intake only - a Sonnet scout settles every routing variable for a feature, you answer only what is uncertain, and the confirmed intake is saved. Nothing is built; the next /feature-ladder:feature with the same request reuses it without asking again. Usage - /feature-ladder:scope <description>
argument-hint: <feature description>
---

Run feature-ladder intake (no build) for: $ARGUMENTS

Do exactly steps 1-3 of the `/feature-ladder:feature` command (the saved-intake
check, scoper agent, questions, `intake.py apply`). There are no switches here.
If step 1's `intake.py find` prints a path, this request is already scoped:
show that intake as below and stop. Scoping never uses an intake up: only a
build marks it used (`pr_checkpoint.py start --intake`), so running this
command twice changes nothing for the next `/feature-ladder:feature`.

Then stop and show:
- the confirmed rung (agent, model, effort, score) and which fields drove it,
- every field with its value and source (you, or the scout and its confidence),
- the decisions you answered (session budget, orchestrator, uncommitted changes),
- this line: "To build it, run `/feature-ladder:feature <the same request>`;
  the saved intake is reused and you won't be asked again."

Do not create branches and do not dispatch any other agent.
