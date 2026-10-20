---
description: Intake only - a Sonnet scout settles every routing variable for a feature, you answer only what is uncertain, and the confirmed intake is saved. Nothing is built; the next /feature-ladder:feature with the same request reuses it without asking again. Usage - /feature-ladder:scope <description>
argument-hint: <feature description>
---

Run feature-ladder intake (no build) for: $ARGUMENTS

Do exactly steps 1-3 of the `/feature-ladder:feature` command (scoper agent,
questions, `intake.py apply`). There are no switches here.

Then stop and show:
- the confirmed rung (agent, model, effort, score) and which fields drove it,
- every field with its value and source (you, or the scout and its confidence),
- the decisions you answered, including the outside-repo policy if asked,
- this line: "To build it, run `/feature-ladder:feature <the same request>`;
  the saved intake is reused and you won't be asked again."

Do not create branches and do not dispatch any other agent.
