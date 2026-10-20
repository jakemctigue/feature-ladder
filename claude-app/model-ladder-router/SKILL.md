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

If `.feature-ladder/v1.6.0/` does not exist in the working directory, run:

```bash
FL=.feature-ladder/v1.6.0
B=https://raw.githubusercontent.com/jakemctigue/feature-ladder/v1.6.0/plugins/feature-ladder/skills
for f in complexity-router/scripts/route.py complexity-router/scripts/intake.py orchestration-ladder/scripts/pr_checkpoint.py orchestration-ladder/scripts/irreversible.py orchestration-ladder/scripts/with_secrets.py skill-selector/scripts/select_skills.py skill-selector/skill-rules.json hindsight-memory/scripts/resolve_bank.py; do
  mkdir -p "$FL/$(dirname "$f")" && curl -fsSL "$B/$f" -o "$FL/$f" || break
done
# added after v1.6.0; absent on older release tags, so its absence is not an error
curl -fsSL "$B/orchestration-ladder/scripts/verify.py" -o "$FL/orchestration-ladder/scripts/verify.py" 2>/dev/null || true
python3 "$FL/complexity-router/scripts/route.py" --table >/dev/null && echo FETCH_OK
```

If that does not print `FETCH_OK`, try
`git clone --depth 1 --branch v1.6.0 https://github.com/jakemctigue/feature-ladder .feature-ladder/repo`
and use `.feature-ladder/repo/plugins/feature-ladder/skills` as the base path.
If both fail, tell the user the scripts could not be downloaded and stop; do not
route by judgment. Never edit the downloaded files.

## 2. Intake: gather everything first, ask once, then run to completion

Before delegating anything, settle the facts for the whole job and ask the user
only what is uncertain and matters. After that, do not stop to ask again.

1. If the work is in a folder or repo here, run
   `python3 .feature-ladder/v1.6.0/complexity-router/scripts/intake.py scan "<request>" --cwd <folder> > .feature-ladder/proposal.json`.
   Otherwise start from `{"request": "...", "slug": "...", "fields": {}, "open_decisions": []}`.
2. Read the actual files or material. For every field write
   `{"value": ..., "confidence": "high|medium|low", "evidence": [...]}` into
   `fields`, and list up to 4 `open_decisions` (choices a worker would
   otherwise guess; each with `id`, `header`, `question`, 2-4 `options`,
   recommended first).
3. `python3 .feature-ladder/v1.6.0/complexity-router/scripts/intake.py decide .feature-ladder/proposal.json`
   lists the questions: fields that are uncertain **and** change the model,
   plus the open decisions. Ask them all in one go (the question tool if this
   session has one, otherwise one short numbered message). If the user said to
   just go, assume the recommended options.
4. Save the answers as `{"<field>": value, "decisions": {"<id>": "<label>"}}`
   and run `intake.py apply .feature-ladder/proposal.json <answers file> --out .feature-ladder/intake.json`.
   That file is binding for every task below.

Then route each task:

```bash
python3 .feature-ladder/v1.6.0/complexity-router/scripts/route.py '<task json>'
```

Fields: `kind` (code, docs, fixture, check, research, analysis), `files`,
`loc`, `modules`; booleans `new_dependency`, `public_api`, `persistence`,
`security`, `concurrency`, `cross_system`, `irreversible`, `no_tests`,
`design_agreed`; 0-3 scales `ambiguity`, `novelty`, `blast_radius`.
`--describe "<sentence>"` gives a keyword pre-fill you must correct;
`--table` prints the scoring rules. Hard flags always escalate: ambiguity >= 2,
cross_system together with security, blast_radius = 3. Opus implementation
rungs require `design_agreed: true`.

`irreversible` means effects outside the files being edited (real data,
deploys, publishing, messages or payments sent to outside services). It adds
risk points but never stops the run. Before running such a command, classify
it: `python3 .feature-ladder/v1.6.0/orchestration-ladder/scripts/irreversible.py classify "<command>"`.
`mundane` (non-production targets, deploys, exports, dry runs): run it and log
it. `risky` (overwrites or deletes production data): get one fable/low verdict
on the exact command (`VERDICT: APPROVE` or `REJECT`), then run or skip it.
`never` (deleting a project, database or backups, force-pushing main): leave it
for the user. The intake's `outside_policy` (`all`, `nonprod`, `none`) narrows
this. No undo scripts or rehearsals; list every such action in the final
report. A critical design decision that comes up mid-run is decided with the
recommended option and recorded under "Assumptions". If the work is in a git
repo with the `gh` CLI, `pr_checkpoint.py` (`start`, `checkpoint`, `finish`)
puts each decision in its own draft PR while work continues on the next branch.

**Secrets.** The intake's `secrets.names` lists the Google Secret Manager
secrets the feature reads (names only). A command that needs one is run as
`python3 .feature-ladder/v1.6.0/orchestration-ladder/scripts/with_secrets.py --secret NAME[=ENV_VAR] -- <command>`,
which fetches the value with the user's gcloud sign-in and masks it in the
output. Never print, echo, log, commit or quote a secret value.

**Second opinion.** The intake's `codex` says whether a second-model review is
worth it (Opus-tier, risk-flagged, novel or algorithmic work). When it is, and
the official Codex CLI is signed in on this machine, run it over the diff,
verify each finding with a check before fixing, and route confirmed bugs as
fix tasks. Otherwise skip it and say why.

The result gives `agent` (the role), `model` and `effort`.

## 3. Select the task's skills

```bash
python3 .feature-ladder/v1.6.0/skill-selector/scripts/select_skills.py --inject --out .feature-ladder/cards/<task-id>-skills.md '{"role":"<agent from step 2>","kind":"<kind>","paths":["<files in scope>"],"text":"<goal + description>","flags":{<router booleans>}}'
```

It chooses at most three skills (within a per-role size budget) by rules, writes their
full instructions to that file, and prints a short `## Skills for this task`
block naming the file. Put that block in the subagent's prompt, not the file's
contents: the skill text is for the worker, not for you. Run it without
`--inject` to see which rule picked each skill and what was dropped. `--list`
shows every skill it can see in this session. If the project in this session
has `.feature-ladder/skill-rules.json`, those rules are merged in automatically.
(If this session cannot write files, use plain `--inject` and paste its output.)

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
  `## Skills for this task` block from step 3, and this line: "Read the skills
  file named above before anything else. Do not invoke the Skill tool. Those
  are the only skills you may use; if you need another, stop and say which
  and why."
- **If you cannot start subagents:** tell the user, in one line, the rung,
  the model and effort to switch to, and the skills to use, with the top
  contributors from the router's `breakdown`.
- Never let a worker choose its own model or skills. Never round a score by
  judgment. Re-route on failure one band at a time (T2 to T3 to T4 to T5);
  after T5 fails, escalate to fable/high to diagnose, not to implement.

Acceptance: the "done when" checks are re-run without a model:
`python3 .feature-ladder/v1.6.0/orchestration-ladder/scripts/verify.py run --id <task> --check "<cmd>" ... --scope <paths> ... --changed <files the worker claims>`
runs them exactly as written, saves the evidence under
`.feature-ladder/evidence/`, and flags files changed outside scope or a wrong
claim (exit 0 green, 1 a check failed, 2 scope or claim mismatch). If that
script was not fetched (older release), or a check's output needs
interpreting, a separate mechanic (haiku/low) re-runs it instead. Never the
worker who wrote the code.

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

Each repository has its own Hindsight bank, chosen deterministically. If this
session has Hindsight tools from a connector pointed at the multi-bank endpoint
(`.../mcp/`, so `list_banks` and `create_bank` exist):

1. In the project's directory run
   `python3 .feature-ladder/v1.6.0/hindsight-memory/scripts/resolve_bank.py --candidates`.
   It prints the project name, the git owner/repo, and the candidate bank ids
   in priority order. If it reports `"is_git": false`, use the connector's
   default bank and skip to step 5.
2. Call `list_banks` with `query` set to the project name (and again with the
   repo name if it differs). Save the combined results to
   `.feature-ladder/banks.json` as `{"banks": [...]}`.
3. Run `python3 .feature-ladder/v1.6.0/hindsight-memory/scripts/resolve_bank.py --choose .feature-ladder/banks.json`.
   It returns `bank_id` and `action` (`pinned`, `existing` or `create`).
4. If `action` is `create`, call `create_bank` with that `bank_id`, a `name`
   of `owner/repo` (or the project name), and a `mission` describing the
   codebase's engineering memory.
5. Pass that `bank_id` on every Hindsight call. Recall conventions and past
   pitfalls before scoping; after acceptance have a haiku/low subagent write one
   `retain` per durable fact (decision and why, pitfall, interface, test gate),
   tagged `project:<name>` and `feature:<slug>`.

If the connector is single-bank (no `list_banks` tool), use it as is. If no
Hindsight tools are present, skip memory and say so once; do not invent
recalled context.
