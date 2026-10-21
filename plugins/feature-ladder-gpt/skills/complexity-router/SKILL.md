---
name: complexity-router
description: Deterministic model/effort selection for coding tasks (gpt-5.6-luna Low .. gpt-5.6-sol High .. gpt-6-astra High, or escalate to the gpt-6-astra coordinator). Use before dispatching any implementation task.
---

# Complexity router

`scripts/route.py` is a pure function: task facts in, rung out. The coordinator
(or anyone dispatching work) fills the task fields from what it has already
learned about the repo; it does **not** guess the model and it does not let a
worker pick its own rung.

## How to use

```bash
# (use `python` instead of `python3` if that is the interpreter on this machine)
python3 ${PLUGIN_ROOT}/skills/complexity-router/scripts/route.py '{
  "kind": "code", "files": 3, "loc": 90, "modules": 1,
  "public_api": false, "persistence": false, "security": false,
  "concurrency": false, "cross_system": false, "irreversible": false,
  "no_tests": false, "ambiguity": 0, "novelty": 1, "blast_radius": 1,
  "design_agreed": true
}'
```

Output includes `agent`, `model`, `effort`. Spawn exactly that:

```
Agent(subagent_type="feature-ladder-gpt:<agent>", model="<model>", effort="<effort>", prompt=<Task Card>)
```

Run `route.py --table` to print the scoring rules; `--describe "<text>"` gives a
keyword pre-fill that must be reviewed (it cannot know files/loc).

## Intake: settling the fields before work starts

`scripts/intake.py` fills the feature-level fields before anything is
delegated (driven by the `scoper` agent and `/feature-ladder-gpt:feature`):

1. `scan "<request>"` - read-only repo scan: first guess, confidence and evidence
   for every field; candidate files; test command; git state.
2. The scoper reads the code and corrects each field (value, confidence,
   evidence, optional range) and lists open decisions.
3. `decide PROPOSAL --mode default|auto` - asks a field only if its
   confidence is not high **and** flipping it changes the rung (one field at a
   time, plus a joint check for several small guesses that add up). Open
   decisions are always asked so the run never has to stop for them later,
   and so is the outside-repo policy when a production target is detected
   (`.firebaserc` aliases, or the scout flagged `irreversible`).
   `auto` asks nothing. `find "<request>"` returns a saved, confirmed intake
   for the same request so it is not asked twice.
4. `apply PROPOSAL ANSWERS` - writes the confirmed intake
   (`.feature-ladder/intake/<slug>.json`): values, the source of each (user or
   scout + confidence), answered decisions, and the route.

The coordinator treats the confirmed intake as binding for per-task fields.

## Filling the fields

- `files`, `loc`, `modules`: estimate from a quick `Grep`/`Glob` of the area, not from the feature description.
- `ambiguity`: 0 if the task card names the exact approach; 2+ if a worker would have to choose an approach. Ambiguity >= 2 always escalates - fix the ambiguity (domain expert or a decision) rather than sending it down the ladder.
- `novelty`: does a pattern for this already exist in the repo? Ask Hindsight (`recall`) and grep before answering.
- `blast_radius`: 3 means "every user or the whole app breaks if this is wrong". 3 escalates.
- `design_agreed`: true only if an ACCEPTED `DESIGN.md` section covers this task. gpt-6-astra rungs are refused without it.

## When the router says ESCALATE

It is not a request for gpt-6-astra to write code. The coordinator does one of:
1. Remove the ambiguity (decide, or dispatch a `domain-expert` to produce a design), then re-route.
2. Split the task into smaller cards that each route cleanly.
3. If the task is inherently consequential (blast radius 3), keep it on the gpt-6-astra rung but add a `deep-verifier` (gpt-6-astra High) review of the Evidence Packet before acceptance.

`irreversible` (effects outside the repo) is a +3 risk weight, not a stop: the
step runs under the intake's outside-repo policy (orchestration-ladder skill).

## Re-routing after failure

A worker that returns `blocked` or whose checks fail twice gets the task re-routed
one band up (T2->T3->T4->T5). Never skip a band. After T5 fails, the coordinator
escalates to `deep-verifier` to diagnose, not to implement.
