---
name: complexity-router
description: Deterministic model/effort selection for coding tasks (Haiku Low .. Sonnet High .. Opus High, or escalate to the Fable coordinator). Use before dispatching any implementation task.
---

# Complexity router

`scripts/route.py` is a pure function: task facts in, rung out. The coordinator
(or anyone dispatching work) fills the task fields from what it has already
learned about the repo; it does **not** guess the model and it does not let a
worker pick its own rung.

## How to use

```bash
# (use `python` instead of `python3` if that is the interpreter on this machine)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/route.py '{
  "kind": "code", "files": 3, "loc": 90, "modules": 1,
  "public_api": false, "persistence": false, "security": false,
  "concurrency": false, "cross_system": false, "irreversible": false,
  "no_tests": false, "ambiguity": 0, "novelty": 1, "blast_radius": 1,
  "design_agreed": true
}'
```

Output includes `agent`, `model`, `effort`. Spawn exactly that:

```
Agent(subagent_type="feature-ladder:<agent>", model="<model>", effort="<effort>", prompt=<Task Card>)
```

Run `route.py --table` to print the scoring rules; `--describe "<text>"` gives a
keyword pre-fill that must be reviewed (it cannot know files/loc).

## Filling the fields

- `files`, `loc`, `modules`: estimate from a quick `Grep`/`Glob` of the area, not from the feature description.
- `ambiguity`: 0 if the task card names the exact approach; 2+ if a worker would have to choose an approach. Ambiguity >= 2 always escalates - fix the ambiguity (domain expert or a decision) rather than sending it down the ladder.
- `novelty`: does a pattern for this already exist in the repo? Ask Hindsight (`recall`) and grep before answering.
- `blast_radius`: 3 means "every user or the whole app breaks if this is wrong". 3 escalates.
- `design_agreed`: true only if an ACCEPTED `DESIGN.md` section covers this task. Opus rungs are refused without it.

## When the router says ESCALATE

It is not a request for Fable to write code. The coordinator does one of:
1. Remove the ambiguity (decide, or dispatch a `domain-expert` to produce a design), then re-route.
2. Split the task into smaller cards that each route cleanly.
3. If the task is inherently consequential (irreversible, blast radius 3), keep it on the Opus rung but add a `deep-verifier` (Fable High) review of the Evidence Packet before acceptance.

## Re-routing after failure

A worker that returns `blocked` or whose checks fail twice gets the task re-routed
one band up (T2->T3->T4->T5). Never skip a band. After T5 fails, the coordinator
escalates to `deep-verifier` to diagnose, not to implement.
