---
name: complexity-router
description: Deterministic model/effort/provider selection for coding tasks (GLM 5.3 Flash or Haiku for T0, GLM or Sonnet Medium for T2, Sonnet High, Opus with an agreed design). Never picks Fable. Use before dispatching any implementation task.
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

Output includes `agent`, `model`, `effort`, `provider` (`glm` for T0 and
implementer/medium with score <= 6, else `claude`), `size` (from the score
alone) and `design_first` (one design step before the worker). `intake.py card`
turns `provider: glm` into an external OpenRouter runner when it is set up.
Spawn exactly what it returns:

```
Agent(subagent_type="feature-ladder:<agent>", model="<model>", effort="<effort>", prompt=<Task Card>)
```

Run `route.py --table` to print the scoring rules; `--describe "<text>"` gives a
keyword pre-fill that must be reviewed (it cannot know files/loc).

## Intake: settling the fields before work starts

`scripts/intake.py` fills the feature-level fields before anything is
delegated (driven by the `scoper` agent and `/feature-ladder:feature`):

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
- `ambiguity`: 0 if the task card names the exact approach; 2+ only if a worker would have to choose between real alternatives. Never from the request's length.
- `novelty`: does a pattern for this already exist in the repo? Grep before answering.
- `blast_radius`: 3 means "every user or the whole app breaks if this is wrong".
- `irreversible`: only for a step the run itself performs outside the repo. Code that writes to Firestore is not irreversible.
- `design_agreed`: true if the request plus the answered decisions specify the approach. Opus rungs need it.

## Hard flags and design

`ambiguity >= 2` and `blast_radius = 3` are hard flags. A hard flag (or an
Opus rung without an agreed design) sets `design_first`: ONE `domain-expert`
step before the worker, or the coordinator decides the open point itself. It
never changes the size, never adds a review, a Fable rung or a scribe.

## Re-routing after failure

A worker whose checks fail is re-dispatched once with the failure; a second
failure goes one band up once (GLM -> Sonnet medium -> Sonnet high). After
that the feature is reported `partial`. Fable rungs (`deep-verifier`,
`architect-escalation`) are reached only with the user's `--escalate`.
