---
name: orchestration-ladder
description: The feature-ladder operating protocol - which rung does what, how work flows coordinator -> experts -> workers -> verifier, and the handoff contracts between rungs. Load whenever coordinating or executing a feature-ladder task.
---

# Orchestration ladder

Fable is expensive and precise; use it like a senior engineer's time. Every other
rung exists so that Fable only plans, directs, fact-checks, inspects evidence and
accepts. Fable never writes feature code itself unless it has climbed to High+
for a specific unresolved problem.

## The ladder

| Rung | Agent | Model / effort | Daily job |
|---|---|---|---|
| T0 | `mechanic` | haiku / low | Mechanical patches, docs drafts, fixtures, executing defined checks |
| T1 | `evidence-collector` | haiku / medium | Collect current sources, extract claims, prepare evidence packets |
| T2-T3 | `implementer` | sonnet / medium or high | Bounded coding, regression tests, small domain analysis |
| T4-T5 | `senior-implementer` | opus / medium or high | Difficult implementation **under an agreed design** |
| D | `domain-expert` / `domain-expert-high` | opus / medium or high | Design, domain analysis, option comparison, spec writing |
| F-Low | `second-opinion` | fable / low | Short, focused second opinion where it saves a larger handoff |
| **F-Med** | **`coordinator`** | **fable / medium** | **Plan, direct workers, fact-check, inspect evidence, accept the result** |
| F-High | `deep-verifier` | fable / high | Subtle bugs, conflicting evidence, cross-system or consequential correctness |
| F-Extra | `architect-escalation` | fable / xhigh | Architecture or systemic problems still unresolved after High |
| F-Ultra | `ultra-investigator` | fable / max | Optional deep investigations; opt-in only |
| X | `codex-reviewer` | haiku / low driving OpenAI Codex | Cross-model second opinion on every feature diff; adversarial tests on request (`--adversarial`) |

Effort names map to Claude Code effort levels: Low=`low`, Medium=`medium`,
High=`high`, Extra=`xhigh`, Ultra=`max`.

## Flow for a feature

```
user -> /feature-ladder:feature "<description>"
  1. coordinator (F-Med)   recall memory -> scope -> decompose -> route each task
  2. domain-expert (D)     only for tasks whose router result is ESCALATE/design_agreed=false
                           or kind=analysis -> produces DESIGN.md (accepted by coordinator)
  3. workers (T0-T5)       execute routed tasks in parallel where independent
                           each returns an Evidence Packet (below)
  4. coordinator (F-Med)   inspects evidence, runs the defined checks via mechanic,
                           fact-checks claims against files/tests, accepts or bounces
  5. escalation            only on: conflicting evidence, a bug nobody can isolate,
                           correctness that spans systems -> deep-verifier (F-High)
                           -> architect-escalation (F-Extra) -> ultra (opt-in)
  6. coordinator           retains outcome to Hindsight, writes FEATURE-REPORT.md
```

## Rules that keep Fable sparse

1. **Fable Medium runs at most three "inspection passes" per feature**: after planning,
   after implementation, after fixes. Everything between is delegated.
2. **Never climb two Fable rungs at once.** Medium -> High -> Extra -> Ultra, and each
   climb needs a one-paragraph justification written into FEATURE-REPORT.md.
3. **Second opinions (F-Low) are for a single question** answerable in under ~300
   words; if the question needs code reading across files, it is a worker task instead.
4. **Fable never re-derives what a worker already proved.** It inspects the evidence
   packet; it re-runs only the checks it doubts.
5. **Opus rungs require `design_agreed=true`** from the router. No agreed design, no
   Opus implementation; send the task to a domain expert first.

## Handoff contracts

### Task Card (coordinator -> any worker)
```
TASK <id>: <one line>
Rung: <agent> (<model>/<effort>)   Router score: <n>   Flags: <none|...>
Scope: files/dirs the worker may touch
Design: path to DESIGN.md section, or "N/A - mechanical"
Done when: <observable checks, e.g. `pytest tests/x -q` green, lint clean>
Do not: <things explicitly out of scope>
Memory: <1-3 relevant Hindsight recall snippets, verbatim>
Return: Evidence Packet
<skill-selector --inject output, verbatim: the only skills this worker has>
```
Workers cannot invoke the Skill tool. If a worker needs a skill it was not
given, it returns `blocked` naming the skill and why; it does not improvise.

### Evidence Packet (any worker -> coordinator)
```
TASK <id> - <status: done | blocked | partial>
Changed: <file list with line ranges>
Checks run: <command -> exit code -> last 5 lines>
Claims: <each factual claim with how it was verified; "unverified" is allowed and honest>
Risks: <anything the worker is unsure about>
Memory candidates: <facts worth retaining, one line each, or "none">
```
Workers never say "should work". Either a check ran and is quoted, or the claim is marked unverified.

### Design Doc (domain expert -> coordinator)
`DESIGN.md` with: Problem, Constraints (from memory + repo), Options considered
(>=2) with trade-offs, Chosen approach, Interfaces/contracts, Test strategy,
Rollback plan, Open questions. The coordinator marks it ACCEPTED or returns it
with numbered objections. Only ACCEPTED designs unlock Opus implementation rungs.

## Parallelism

Independent tasks are launched in one message with multiple Agent calls. Tasks
that touch the same files are serialized. Use `isolation: worktree` for Opus
implementation tasks so a bad run never dirties the main tree.

## Acceptance criteria (coordinator's final gate)

- Every Task Card has a matching Evidence Packet with status `done`.
- All "Done when" checks were run by `mechanic` (not by the implementer who wrote the code) and are green.
- No unverified claim survives in anything user-visible without being labelled.
- FEATURE-REPORT.md written; outcome retained to Hindsight with tags `project:<name>`, `feature:<slug>`.
