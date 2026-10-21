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
| T0 | mechanic | GLM 5.3 Flash (OpenRouter), else haiku / low | Mechanical patches, docs drafts, fixtures; at most one per feature |
| T1 | evidence-collector | haiku / medium | Collect current sources, extract claims with citations |
| T2 | implementer | GLM 5.3 Flash for score <= 6, else **sonnet / medium** | Bounded coding and its tests: the default rung |
| T3 | implementer | sonnet / high | Bounded coding, harder |
| T4 | senior-implementer | opus / medium | Difficult implementation under an agreed design |
| T5 | senior-implementer | opus / high | Difficult implementation, high stakes |
| D | domain-expert(-high) | opus / medium or high | One design step, only when the route says `design_first` |

The router never picks Fable. A Fable subagent (second opinion, deep
verification) runs only when the user asks for one by name.

**Atomic.** A feature is the requested code change and its tests, nothing
else: no review, no second opinion, no extra hardening, no deploy the user did
not ask for. Reviews (Codex, adversarial tests, a second opinion) and memory
writes happen once, at the end of a long session, and only when the user
schedules them (section 5).

## 1. Fetch the scripts (once per conversation)

If `.feature-ladder/v1.10.0/` does not exist in the working directory, run:

```bash
FL=.feature-ladder/v1.10.0
B=https://raw.githubusercontent.com/jakemctigue/feature-ladder/v1.10.0/plugins/feature-ladder/skills
for f in complexity-router/scripts/route.py complexity-router/scripts/intake.py orchestration-ladder/scripts/pr_checkpoint.py orchestration-ladder/scripts/irreversible.py orchestration-ladder/scripts/with_secrets.py orchestration-ladder/scripts/verify.py orchestration-ladder/scripts/external_worker.py orchestration-ladder/scripts/session_budget.py skill-selector/scripts/select_skills.py skill-selector/skill-rules.json hindsight-memory/scripts/resolve_bank.py hindsight-memory/scripts/memory_gate.py; do
  mkdir -p "$FL/$(dirname "$f")" && curl -fsSL "$B/$f" -o "$FL/$f" || break
done
python3 "$FL/complexity-router/scripts/route.py" --table >/dev/null && echo FETCH_OK
```

Then, once per conversation (a conversation is one coding session), run
`python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/session_budget.py new`
so the session's subagent budget is asked again.

If that does not print `FETCH_OK`, try
`git clone --depth 1 --branch v1.10.0 https://github.com/jakemctigue/feature-ladder .feature-ladder/repo`
and use `.feature-ladder/repo/plugins/feature-ladder/skills` as the base path.
If both fail, tell the user the scripts could not be downloaded and stop; do not
route by judgment. Never edit the downloaded files.

## 2. Intake: gather everything first, ask once, then run to completion

Before delegating anything, settle the facts for the whole job and ask the user
only what is uncertain and matters. After that, do not stop to ask again.

1. If the work is in a folder or repo here, run
   `python3 .feature-ladder/v1.10.0/complexity-router/scripts/intake.py scan "<request>" --cwd <folder> > .feature-ladder/proposal.json`.
   Otherwise start from `{"request": "...", "slug": "...", "fields": {}, "open_decisions": []}`.
2. Read the actual files or material. For every field write
   `{"value": ..., "confidence": "high|medium|low", "evidence": [...]}` into
   `fields`, and list up to 4 `open_decisions` (choices a worker would
   otherwise guess; each with `id`, `header`, `question`, 2-4 `options`,
   recommended first).
3. `python3 .feature-ladder/v1.10.0/complexity-router/scripts/intake.py decide .feature-ladder/proposal.json`
   lists the questions: on the first feature of the conversation, how many
   subagents the whole session may spawn (`session_agents`: Small 1-2,
   Medium 3-4, Large up to 8), then fields that are uncertain **and** change
   the model, the open decisions and the live-changes policy. Ask them all in one go (the question tool if this
   session has one, otherwise one short numbered message). If the user said to
   just go, assume the recommended options.
4. Save the answers as `{"<field>": value, "decisions": {"<id>": "<label>"}}`
   and run `intake.py apply .feature-ladder/proposal.json <answers file> --out .feature-ladder/intake.json`.
   That file is binding for every task below.

The confirmed intake carries `route` (the rung for the whole feature),
`budget` (its size) and `secrets`; `apply` also stores the session budget.
Tell the user one line before anything runs: "Size S: one GLM Flash worker,
no design, no review. 3 of 4 session subagents left. Running now."

**The session budget is a hard stop.** This app has no hooks, so before
starting ANY subagent run
`python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/session_budget.py charge --agent <role> --model <model>`.
Exit 4 means the budget is spent (or the spawn is one a feature never makes):
do not start it; finish with what you have and say what is left. You, the
conversation, are the coordinator and are not counted.

## 2b. The budget is binding

| Size | Score | What you may spend |
|---|---|---|
| S | 0-9 | ONE subagent does the whole feature. About a dozen of your own turns. |
| M | 10-19 | One subagent; two only if the files form two independent sets. About 20 turns. |
| L | 20+ | Up to four subagents on independent cards. About 24 turns. |

At every size: no second subagent to re-check, no Codex, no scribe, no Fable,
at most one mechanic, and a design step (one opus subagent writing
DESIGN.md) only when the route says `design_first` (a hard flag). Every
subagent and every one of your turns re-reads its whole context, so the
budget is where the cost is controlled. For S the whole job is: one
`intake.py card` call, one subagent, one `verify.py run`, accept.

Build each card and pick its skills in one call:

```bash
python3 .feature-ladder/v1.10.0/complexity-router/scripts/intake.py card '{"id":"T1", <router fields from the intake>, "paths":["<files in scope>"], "text":"<goal + description>"}'
```

It prints the route (`agent`, `model`, `effort`, `size`) and a short
`## Skills for this task` block naming a file the worker reads; paste both
into the card and never read that file yourself. Router fields: `kind`
(code, docs, fixture, check, research, analysis), `files`, `loc`, `modules`;
booleans `new_dependency`, `public_api`, `persistence`, `security`,
`concurrency`, `cross_system`, `irreversible`, `no_tests`, `design_agreed`;
0-3 scales `ambiguity`, `novelty`, `blast_radius`. `route.py --table` prints
the scoring rules. Hard flags (ambiguity >= 2, blast_radius = 3) add one
design step and nothing else; they never change the size. Opus
rungs require `design_agreed: true`.

Verify with no model: after the worker returns, run
`python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/verify.py run --id T1 --check "<cmd>" --scope <files> --changed <files the worker listed>`.
Exit 0 accepts; exit 1 re-dispatches once with the failure (a second failure
goes one band up once, then report `partial`); exit 2 means the worker
touched files outside scope, so bounce the packet to it.

`irreversible` means effects outside the files being edited (real data,
deploys, publishing, messages or payments sent to outside services). It adds
risk points but never stops the run. Before running such a command, classify
it: `python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/irreversible.py classify "<command>"`.
The intake's `outside_policy` (`none` by default, `nonprod`, `all`) decides
what runs at all. `mundane` (non-production targets, deploys, exports, dry
runs): run it if the policy allows and log it. `risky` (overwrites or deletes
production data) and `never` (deleting a project, database or backups,
force-pushing main): leave it for the user with the exact command. No Fable
verdict unless the user asked for a second opinion. No undo scripts or rehearsals; list every such action in the final
report. A critical design decision that comes up mid-run is decided with the
recommended option and recorded under "Assumptions". If the work is in a git
repo with the `gh` CLI, `pr_checkpoint.py` (`start`, `checkpoint`, `finish`)
puts each decision in its own draft PR while work continues on the next branch.

**Secrets.** The intake's `secrets.names` lists the Google Secret Manager
secrets the feature reads (names only). A command that needs one is run as
`python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/with_secrets.py --secret NAME[=ENV_VAR] -- <command>`,
which fetches the value with the user's gcloud sign-in and masks it in the
output. Never print, echo, log, commit or quote a secret value.

**The GLM tier.** When the route's `provider` is `glm` and `runner.kind` is
`external` (the Claude CLI and an OpenRouter key are present on this
machine), write the Task Card to `runner.card_file` and run `runner.command`
in the shell instead of starting a subagent: it launches a child `claude -p`
on GLM 5.3 Flash through OpenRouter (key from Google Secret Manager or
`OPENROUTER_API_KEY`), checks the session budget itself, and prints the
Evidence Packet. Exit 3: OpenRouter unavailable, start the routed Claude
subagent instead. Exit 4: the session budget is spent. Otherwise
(`runner.kind` `subagent`) start the routed Claude subagent; the runner's
`note` says why GLM was not used. `{"openrouter": {"preset": "jev"}}` in
`~/.claude/feature-ladder/providers.json` sends these cards to
`typesafe/jev-router` instead.

## 3. Selecting skills on their own

`intake.py card` already did this. To see the choice by itself:

```bash
python3 .feature-ladder/v1.10.0/skill-selector/scripts/select_skills.py --inject --out .feature-ladder/cards/<task-id>-skills.md '{"role":"<agent>","kind":"<kind>","paths":["<files in scope>"],"text":"<goal + description>","flags":{<router booleans>}}'
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

- **`design_first: true`** means the approach is genuinely open (or whole-app
  blast radius, or an Opus rung without an agreed design). Decide the open
  point yourself if it is cheap to change later, or start ONE opus
  domain-expert subagent for a DESIGN.md; then re-run `intake.py card` with
  `design_agreed: true`. It never means you write the code yourself, and it
  never adds agents beyond the budget.
- **If you can start subagents:** run `session_budget.py charge` first (2b),
  then spawn a general-purpose subagent with the
  returned `model` and `effort` (this skill is the explicit instruction to set
  effort). Its prompt is: the role brief below, a task card (goal, scope of
  files allowed, "done when" checks, out of scope, relevant context), the
  `## Skills for this task` block from `intake.py card`, and this line: "Read the skills
  file named above before anything else. Do not invoke the Skill tool. Those
  are the only skills you may use; if you need another, stop and say which
  and why."
- **If you cannot start subagents:** tell the user, in one line, the rung,
  the model and effort to switch to, and the skills to use, with the top
  contributors from the router's `breakdown`.
- Never let a worker choose its own model or skills. Never round a score by
  judgment. On failure re-dispatch once, then one band up once (GLM to
  Sonnet medium to Sonnet high); after that report the feature `partial`.

Acceptance: the "done when" checks are re-run without a model:
`python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/verify.py run --id <task> --check "<cmd>" ... --scope <paths> ... --changed <files the worker claims>`
runs them exactly as written, saves the evidence under
`.feature-ladder/evidence/`, and flags files changed outside scope or a wrong
claim (exit 0 green, 1 a check failed, 2 scope or claim mismatch). No
subagent re-checks the work.

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

## 5. End of session (only when the user asks)

When the user says the session is done, or asks for a review:
- **Review**, only if asked: run the Codex review once over the whole
  session's diff (`codex_review.py review --base <session base>`, official
  Codex CLI signed in on this machine) and report the findings; each
  confirmed bug becomes its own feature request. A Fable second opinion only
  if the user asks for one.
- **Memory**, once: see below.
- `python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/session_budget.py close`.

## Memory (Hindsight), when connected

Memory is read once per feature and written once per session, and only
concrete facts a future task in this repo could act on.

Bank: each repository has its own, chosen deterministically. If this session
has Hindsight tools from a connector pointed at the multi-bank endpoint
(`.../mcp/`, so `list_banks` and `create_bank` exist):

1. In the project's directory run
   `python3 .feature-ladder/v1.10.0/hindsight-memory/scripts/resolve_bank.py --candidates`.
   It prints the project name, the git owner/repo, and the candidate bank ids
   in priority order. If it reports `"is_git": false`, use the connector's
   default bank and skip to step 5.
2. Call `list_banks` with `query` set to the project name (and again with the
   repo name if it differs). Save the combined results to
   `.feature-ladder/banks.json` as `{"banks": [...]}`.
3. Run `python3 .feature-ladder/v1.10.0/hindsight-memory/scripts/resolve_bank.py --choose .feature-ladder/banks.json`.
   It returns `bank_id` and `action` (`pinned`, `existing` or `create`).
4. If `action` is `create`, call `create_bank` with that `bank_id`, a `name`
   of `owner/repo` (or the project name), and a `mission` describing the
   codebase's engineering memory.
5. Pass that `bank_id` on every Hindsight call.

Reads: ONE `recall` per feature, before writing the card, with `tags:
["project:<name>"]`, `tags_match: "all"`, `max_tokens` at most 1000 and a
query naming the feature area and its files. Paste only the snippets that
bear on the card. Never `reflect`, knowledge pages or bank edits.

Writes: none during a feature. After each feature, queue the worker's
memory candidates that a future task could act on:
`python3 .feature-ladder/v1.10.0/orchestration-ladder/scripts/session_budget.py remember --line "[kind] fact" ...`
(kind: decision, convention, pitfall, interface, test-strategy; name the
path, identifier or command). The gate refuses summaries, narration, vague
or duplicate lines; drop those, do not rewrite them. At the end of the
session run `python3 .feature-ladder/v1.10.0/hindsight-memory/scripts/memory_gate.py flush --emit`
and make one `retain` per `would_write` line exactly as printed (`context:
feature-ladder`, tags `project:<name>`, `feature:session`, the kind). No
scribe subagent, no summaries.

If the connector is single-bank (no `list_banks` tool), use it as is. If no
Hindsight tools are present, skip memory and say so once; do not invent
recalled context.
