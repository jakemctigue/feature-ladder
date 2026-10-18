---
name: model-ladder-router
description: Deterministically pick the model, effort and skills for each coding or research task before delegating it to a subagent (feature-ladder for the Claude app). Use when splitting work across subagents or when asked which model or skills a task needs.
---

# Model ladder router (feature-ladder for the Claude app)

Pick the cheapest model that will do a task correctly, and give the worker only
the skills that task needs. Both decisions come from fixed scripts in the
public feature-ladder repo, not from judgment: same task facts in, same answer
out, every time. This is the Claude app counterpart of the feature-ladder
Claude Code plugin and uses the same scripts and rules.

## The ladder

| Rung | Role | Model / effort | Job |
|---|---|---|---|
| T0 | mechanic | haiku / low | Mechanical patches, docs drafts, fixtures, running defined checks |
| T1 | evidence-collector | haiku / medium | Collect current sources, extract claims with citations |
| T2 | implementer | sonnet / medium | Bounded coding, regression tests |
| T3 | implementer | sonnet / high | Bounded coding, harder |
| T4 | senior-implementer | opus / medium | Difficult implementation under an agreed design |
| T5 | senior-implementer | opus / high | Difficult implementation, high stakes |
| D | domain-expert(-high) | opus / medium or high | Design and analysis; no implementation |
| ESC | escalate | (coordinator) | The task is not ready to delegate; fix it first |

Fable rungs are never chosen by the router. They are reached only by deliberate
escalation, one step at a time: fable/low for one sharp second opinion,
fable/high for subtle bugs or conflicting evidence, fable/xhigh for unresolved
architecture problems, fable/max only if the user opts in.

## 1. Fetch the scripts (once per conversation)

If `.feature-ladder/v1.1.0/` does not exist in the working directory, run:

```bash
FL=.feature-ladder/v1.1.0
B=https://raw.githubusercontent.com/jakemctigue/feature-ladder/v1.1.0/plugins/feature-ladder/skills
for f in complexity-router/scripts/route.py skill-selector/scripts/select_skills.py skill-selector/skill-rules.json; do
  mkdir -p "$FL/$(dirname "$f")" && curl -fsSL "$B/$f" -o "$FL/$f" || break
done
python3 "$FL/complexity-router/scripts/route.py" --table >/dev/null && echo FETCH_OK
```

If that does not print `FETCH_OK`, try
`git clone --depth 1 --branch v1.1.0 https://github.com/jakemctigue/feature-ladder .feature-ladder/repo`
and use `.feature-ladder/repo/plugins/feature-ladder/skills` as the base path.
If both fail, tell the user the scripts could not be downloaded and stop; do not
route by judgment. Never edit the downloaded files.

## 2. Gather facts, then route each task

Look at the actual files or material first. Estimate `files`, `loc` and
`modules` from what you read, not from the request. Then:

```bash
python3 .feature-ladder/v1.1.0/complexity-router/scripts/route.py '<task json>'
```

Fields: `kind` (code, docs, fixture, check, research, analysis), `files`,
`loc`, `modules`; booleans `new_dependency`, `public_api`, `persistence`,
`security`, `concurrency`, `cross_system`, `irreversible`, `no_tests`,
`design_agreed`; 0-3 scales `ambiguity`, `novelty`, `blast_radius`.
`--describe "<sentence>"` gives a keyword pre-fill you must correct;
`--table` prints the scoring rules. Hard flags always escalate: irreversible,
ambiguity >= 2, cross_system together with security, blast_radius = 3. Opus
implementation rungs require `design_agreed: true`.

The result gives `agent` (the role), `model` and `effort`.

## 3. Select the task's skills

```bash
python3 .feature-ladder/v1.1.0/skill-selector/scripts/select_skills.py --inject '{"role":"<agent from step 2>","kind":"<kind>","paths":["<files in scope>"],"text":"<goal + description>","flags":{<router booleans>}}'
```

It returns at most three skills (within a size budget) chosen by rules, with
their full instructions. Run it without `--inject` to see which rule picked
each skill and what was dropped. `--list` shows every skill it can see in this
session. If the project in this session has `.feature-ladder/skill-rules.json`,
those rules are merged in automatically.

## 4. Act on the result exactly

- **ESCALATE** means the task is not ready. Resolve the ambiguity (decide, or
  get a design from a domain expert), split it into tasks that route cleanly,
  or for inherently consequential work keep it on an Opus rung and add a
  fable/high review before accepting. Escalation never means you write the
  code yourself.
- **If you can start subagents:** spawn a general-purpose subagent with the
  returned `model` and `effort` (this skill is the explicit instruction to set
  effort). Its prompt is: the role brief below, a task card (goal, scope of
  files allowed, "done when" checks, out of scope, relevant context), the
  `--inject` output, and this line: "Do not invoke the Skill tool. The skills
  above are the only ones you may use; if you need another, stop and say which
  and why."
- **If you cannot start subagents:** tell the user, in one line, the rung,
  the model and effort to switch to, and the skills to use, with the top
  contributors from the router's `breakdown`.
- Never let a worker choose its own model or skills. Never round a score by
  judgment. Re-route on failure one band at a time (T2 to T3 to T4 to T5);
  after T5 fails, escalate to fable/high to diagnose, not to implement.

Acceptance: the "done when" checks are re-run by a separate mechanic
(haiku/low), not by the worker who wrote the code.

## Role briefs (put at the top of the subagent prompt)

- **mechanic**: Do exactly what the task card says, nothing more. If anything
  is unclear, stop and ask. For checks, run the listed commands exactly and
  report each command, exit code and the last lines of output verbatim. Do not
  fix failures.
- **evidence-collector**: Gather facts, change nothing. Prefer the repo,
  official docs, changelogs and source code. Return a table of
  claim | source | version/date | confidence; list conflicting sources side by
  side without resolving them.
- **implementer**: Implement within the stated scope in the existing style,
  with tests for the new behaviour. Run the tests. If the approach is
  ambiguous, stop and return the one question that would unblock you. Report
  changed files, each check run with real output, and anything unverified.
- **senior-implementer**: Implement exactly the agreed design. If the design
  conflicts with the code, stop and report the conflict rather than
  improvising. Stay in scope; report real check output; label anything
  unverified.
- **domain-expert**: Produce a design, do not implement. Cover problem,
  constraints, at least two options with trade-offs, chosen approach,
  interfaces, test strategy, rollback, open questions. For each implied task,
  propose the router fields.

## Memory (Hindsight), when connected

If this session has Hindsight tools (names ending in `recall` and `retain`
from a Hindsight connector), recall project conventions and past pitfalls
before scoping, and after acceptance have a haiku/low subagent write one
`retain` per durable fact (decision and why, pitfall, interface, test gate),
tagged `project:<name>`. If no Hindsight tools are present, skip memory and say
so once; do not invent recalled context.
