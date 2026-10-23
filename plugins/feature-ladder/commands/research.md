---
description: Research what a requested feature wants from this codebase, without building it and without asking questions. Reuses the intake scan and the Sonnet scout's proposal, the router and the skill selector, then writes and prints a short research brief - what the feature touches, a qualitative cost label per agent (never a number) and what to expect. No Opus, no Fable, at most one Haiku evidence-collector call. Usage - /feature-ladder:research <description>
argument-hint: <feature description>
---

Research (no build) for: $ARGUMENTS

You (this conversation) run this; do not scope, design or implement anything
yourself. Use `python` instead of `python3` if that is the interpreter on this
machine. There are no switches here, no Ask step, and no confirmed intake is
saved, so a later `/feature-ladder:feature` does its own Ask step.

## 1. Reuse

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/brief.py" find "<request text>"
```
If it prints a path, a brief for this exact request already exists: run
`brief.py show "<request text>"`, print it, say "Reusing the saved brief" in
one line, and stop. (The second printed line, when present, is the saved
scoper proposal.)

## 2. Scan and propose

If `.feature-ladder/intake/<slug>-proposal.json` exists for this request text
(its `request` field matches), reuse it and do not dispatch the scoper.
Otherwise dispatch the scout; it runs the intake scan itself and writes the
proposal:
```
Agent(subagent_type="feature-ladder:scoper", model="sonnet", effort="medium", name="feature-scoper",
      prompt="Feature request: <request text>\nMode: auto\nFollow your instructions exactly.")
```
Never use an Opus or Fable agent on this path.

## 3. Route and skills

The brief builder routes the proposal's values with `ambiguity=1` and
`blast_radius=1` assumed (no questions are asked) and runs the skill
selector, so there is nothing to do by hand. To look at the routing alone:
`/feature-ladder:route` and `/feature-ladder:skills` do the same dry-runs.

## 4. Evidence (only when needed)

Only if the request names an external library or API that the repo does not
already use (check the proposal's `new_dependency` field and the repo's
manifests), dispatch ONE collector and save its short answer to
`.feature-ladder/intake/<slug>-evidence.md`:
```
Agent(subagent_type="feature-ladder:evidence-collector", model="haiku", effort="medium", name="research-evidence",
      prompt="Library or API: <name>. Request: <request text>\nReturn at most 8 short bullets: the current API surface the request needs, install or auth steps, and one pitfall. Cite sources. No code changes.")
```
Otherwise dispatch nothing else.

## 5. Build and print

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/brief.py" build --proposal .feature-ladder/intake/<slug>-proposal.json [--external "<name>" --evidence .feature-ladder/intake/<slug>-evidence.md]
```
It writes `.feature-ladder/intake/<slug>-brief.md` and prints it. Show the brief
as printed, then add this line: "To build it, run `/feature-ladder:feature
<the same request>` (add `--researcher` to see the brief first); the saved
brief and proposal are reused, and the Ask step is yours."

Do not create branches and do not dispatch any other agent.
