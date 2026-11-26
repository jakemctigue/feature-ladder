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
| T0 | mechanic | GLM 5.3 Flash (OpenRouter; DeepSeek V4.1 Flash Fast on Silk with `--no-bs`), else haiku / low | Mechanical patches, docs drafts, fixtures; at most one per feature |
| T1 | evidence-collector | haiku / medium | Collect current sources, extract claims with citations |
| T2 | implementer | GLM 5.3 Flash for score <= 6, else **sonnet / medium** (with `--no-bs`: DeepSeek V4.1 Flash for <= 6, Kimi K3.1 Pro for 7-14, on Silk) | Bounded coding and its tests: the default rung |
| T3 | implementer | sonnet / high | Bounded coding, harder |
| T4 | senior-implementer | opus / medium | Difficult implementation under an agreed design |
| T5 | senior-implementer | opus / high | Difficult implementation, high stakes |
| D | domain-expert(-high) | opus / medium or high | One design step, only when the route says `design_first` |

**The orchestrator is the user's pick, asked up front.** You, the
conversation, coordinate; the user chooses once per session whether the
orchestrator is Opus 5.5 at xhigh effort or Fable 5.2 (section 2).

The router never picks Fable. A Fable subagent (second opinion, deep
verification) runs only when the user asks for one by name, with one
exception: the **integrator** (Fable, coding tools to reconcile seams) runs
once, on its own, when two or more different coding agents wrote code for the
same feature.

**Nothing waits for the user.** Deploys, data steps and the merge happen when
the feature needs them and are logged `review: pending` for the user to look
at afterwards. There is no outside-repo policy and no approve step; the only
command that is refused is one that would print a secret value.

**Atomic.** A feature is the requested code change and its tests, nothing
else: no review, no second opinion, no extra hardening, no deploy the user did
not ask for. Reviews (Codex, adversarial tests, a second opinion) happen
once, at the end of a long session, and only when the user schedules them
(section 5). Memory is written by a script after each feature (see Memory).

## 1. Fetch the scripts (once per conversation)

If `.feature-ladder/v1.21.0/` does not exist in the working directory, run:

```bash
FL=.feature-ladder/v1.21.0
B=https://raw.githubusercontent.com/jakemctigue/feature-ladder/v1.21.0/plugins/feature-ladder/skills
for f in complexity-router/scripts/route.py complexity-router/scripts/intake.py complexity-router/scripts/brainstorm.py orchestration-ladder/scripts/pr_checkpoint.py orchestration-ladder/scripts/irreversible.py orchestration-ladder/scripts/with_secrets.py orchestration-ladder/scripts/verify.py orchestration-ladder/scripts/confidence.py orchestration-ladder/scripts/external_worker.py orchestration-ladder/scripts/session_budget.py skill-selector/scripts/select_skills.py skill-selector/skill-rules.json hindsight-memory/scripts/resolve_bank.py hindsight-memory/scripts/memory_gate.py hindsight-memory/scripts/recall.py cross-model-review/scripts/codex_review.py worker-contract/SKILL.md orchestration-ladder/scripts/ladder_state.py; do
  mkdir -p "$FL/$(dirname "$f")" && curl -fsSL "$B/$f" -o "$FL/$f" || break
done
H=https://raw.githubusercontent.com/jakemctigue/feature-ladder/v1.21.0/plugins/feature-ladder/hooks/scripts
for f in ledger.py guard_irreversible.py; do
  curl -fsSL "$H/$f" -o "$FL/orchestration-ladder/scripts/$f" || break
done
python3 "$FL/complexity-router/scripts/route.py" --table >/dev/null && echo FETCH_OK
```

The second loop puts the token ledger (and the run finder it imports) beside
`external_worker.py`, which is where that script looks for them here.
`ladder_state.py` is the state helper the other scripts import;
`worker-contract/SKILL.md` is the contract `external_worker.py` gives GLM
workers.

Then, once per conversation (a conversation is one coding session), run
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/session_budget.py new`
so the session's subagent budget is asked again.

If that does not print `FETCH_OK`, try
`git clone --depth 1 --branch v1.21.0 https://github.com/jakemctigue/feature-ladder .feature-ladder/repo`
and use `.feature-ladder/repo/plugins/feature-ladder/skills` as the base path.
If both fail, tell the user the scripts could not be downloaded and stop; do not
route by judgment. Never edit the downloaded files.

## 2. Intake: gather everything first, ask once, then run to completion

Before delegating anything, settle the facts for the whole job and ask the user
only what is uncertain and matters. After that, do not stop to ask again.

1. If the work is in a folder or repo here, run
   `python3 .feature-ladder/v1.21.0/complexity-router/scripts/intake.py scan "<request>" --cwd <folder> > .feature-ladder/proposal.json`.
   Otherwise start from `{"request": "...", "slug": "...", "fields": {}, "open_decisions": []}`.
2. Recall, with no model:
   `python3 .feature-ladder/v1.21.0/hindsight-memory/scripts/recall.py --query "<request>" --paths <the scan's candidate files> --out .feature-ladder/recall.json`
   (see "Memory" below; it prints only the facts that bear on the request).
   A recorded decision or convention answers its question: confidence
   `high`, evidence `memory: [kind] ...`. Keep its `memory` line for the card.
3. Read the actual files or material. For every field write
   `{"value": ..., "confidence": "high|medium|low", "evidence": [...]}` into
   `fields`, and list up to 4 `open_decisions` (choices a worker would
   otherwise guess; each with `id`, `header`, `question`, 2-4 `options`,
   recommended first).
4. `python3 .feature-ladder/v1.21.0/complexity-router/scripts/intake.py decide .feature-ladder/proposal.json`
   lists the questions: on the first feature of the conversation, which
   model orchestrates (`orchestrator`: Opus 5.5 xhigh or Fable 5.2; always
   ask it, never pick Fable for the user) and how many subagents the whole
   session may spawn (`session_agents`: Small 1-2, Medium 3-4, Large up to
   8), then fields that are uncertain **and** change
   the model, and the open decisions. Ask them all in one go (the question tool if this
   session has one, otherwise one short numbered message). If the user said to
   just go, assume the recommended options.
5. Save the answers as `{"<field>": value, "decisions": {"<id>": "<label>"}}`
   (with `--no-bs` on the request, also `"switches": ["no_bs"]`; see "The
   GLM tier" below) and run `intake.py apply .feature-ladder/proposal.json <answers file> --out .feature-ladder/intake.json`.
   That file is binding for every task below.

The confirmed intake carries `route` (the rung for the whole feature),
`budget` (its size), `orchestrator` and `secrets`; `apply` also stores the
session budget and the orchestrator. Tell the user one line before anything
runs, naming where the worker runs (GLM, Silk with `--no-bs`, or Claude):
"Orchestrator Opus 5.5 xhigh. Size S: one GLM Flash worker, no design,
no review. 3 of 4 session subagents left. Running now."

**The orchestrator is you.** This app cannot change the model of a running
conversation. If you are not the model the user picked (`orchestrator.label`;
Opus needs effort xhigh), say so once, in one line, with what to switch the
model picker to, and carry on coordinating as you are unless they switch.
Never start a Fable subagent to coordinate.

## 2a. Brainstorm first (only when the user asks: `--brainstorm`, or "brainstorm this first")

For a whole app, or a large piece of one. Every application and architecture
detail is gathered at the very front, with as many questions as it takes;
only then is the app narrowed into features, each built as its own run. `B`
is `python3 .feature-ladder/v1.21.0/complexity-router/scripts/brainstorm.py`.
Without the switch, skip this section, except to continue a concluded
brainstorm (`--from-brainstorm [app-slug] [feature-id]`, or "build the next
feature of the app"): then go straight to 5.

1. `B find "<request>"`: a path means the app sheet for this request is
   already confirmed; go to 5.
2. `B guide` and follow what it prints. You are the scout: scan, recall,
   settle every detail (and add any this app needs that the sheet lacks),
   draft the features, `B decide`. Start no subagent for this.
3. Ask everything `decide` lists, however many questions that is:
   `decisions` (orchestrator, session subagents, and `build`: every feature,
   the first only, or plan only), every item of `questions`, then
   `open_text` in the user's own words. If the user said to just go, ask
   nothing. Save
   `{"answers": {"<detail>": "<chosen label or their words>"}, "decisions": {"<id>": "<label>"}}`
   and run `B apply <sheet> <answers file>`.
4. While `next_step` is `revise` (`revise_because` says why): do the guide's
   REVISE part yourself - propose the open and newly needed details and run
   `B followup <app> <details file>`, rewrite features with `B features` -
   then ask what `B followup` printed (unless the user said to just go),
   save `{"answers": {...}}` and run `B refine <app> <answers file>`. At most
   3 follow-up rounds, all before anything is built. Then
   `B remember --no-flush` and write the architecture to memory right away
   with `memory_gate.py flush` (see Memory; with a connector, `flush --emit`
   and one `retain` per `retain_calls` entry). Tell the user the sheet's
   `readable` path, the features in order, one line each, and how many
   architecture facts went to memory.
5. `B next` gives the next feature (to continue a concluded brainstorm:
   `B next --build all [--app <slug>]`, or `--build first --feature <id>`
   for one feature). Run its `request` through sections 2 to 4 as one normal
   feature, asking nothing more (recommended options), with
   `"brainstorm": <feature.brainstorm>` in the intake answers and
   `--no-architecture` on its `recall.py`; put `brainstorm.context` on every
   card as its `App:` block. Then `B done <id> --status done|partial`.
   Repeat while `build` is `all` and `next` returns a feature; stop after one
   when it is `first`. When `feature` is null, relay its `note` and what
   remains, and how to continue (`handoff.command` in the sheet:
   `--from-brainstorm <slug>`).

Later features of the same app, even plain ones without the switch and in
another conversation, get the stored architecture from `recall.py`: up to 4
`` `arch:<detail>` `` decisions in the feature's area on its `Memory:` line.
Treat them as binding unless the user says otherwise.

**The session budget is a hard stop.** This app has no hooks, so before
starting ANY subagent run
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/session_budget.py charge --agent <role> --model <model>`.
Exit 4 means the budget is spent (or the spawn is one a feature never makes):
do not start it; finish with what you have and say what is left. You, the
conversation, are the coordinator and are not counted.

## 2b. The budget is binding

| Size | Score | What you may spend |
|---|---|---|
| S | 0-9 | ONE subagent does the whole feature. About a dozen of your own turns. |
| M | 10-19 | One subagent; two only if the files form two independent sets. About 20 turns. |
| L | 20+ | Up to four subagents on independent cards. About 24 turns. |

At every size: no second subagent to re-check, no Codex, no scribe, no Fable
but the integrator (once, after two coding agents; it is not a worker),
at most one mechanic, and a design step (one opus subagent writing
DESIGN.md) only when the route says `design_first` (a hard flag). Every
subagent and every one of your turns re-reads its whole context, so the
budget is where the cost is controlled. For S the whole job is: one
`intake.py card` call, one subagent, one `verify.py run`, accept.

Build each card and pick its skills in one call:

```bash
python3 .feature-ladder/v1.21.0/complexity-router/scripts/intake.py card '{"id":"T1", <router fields from the intake>, "paths":["<files in scope>"], "text":"<goal + description>"}'
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
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/verify.py run --id T1 --check "<cmd>" --scope <files> --changed <files the worker listed>`.
Exit 0 accepts; exit 1 re-dispatches once with the failure (a second failure
goes one band up once, then report `partial`); exit 2 means the worker
touched files outside scope, so bounce the packet to it.

**Confidence before each card.** The intake's `recall.py` call saved the
recall to `.feature-ladder/recall.json`; paste its `memory` line verbatim as
the card's `Memory:` line (for a shard, run `recall.py` again with the
shard's title and files and `--reuse .feature-ladder/recall.json`). Then run
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/confidence.py card --action "<card title>" --paths <scope files> --size <S|M|L> --memories .feature-ladder/recall.json`.
It scores every memory's sentiment and relevance into one number: exit 0
`proceed`; exit 5 `reshard` - split the card into the shards it names by
independent file sets and run it once more per shard (once per feature,
then proceed). Put `Confidence: <n> <decision> (<mood>)` on the card. It is
advice about size, never a stop.

`irreversible` means effects outside the files being edited (real data,
deploys, publishing, messages or payments sent to outside services). It adds
risk points and never stops the run: run what the feature needs, when it
needs it. Classify each such command first,
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/irreversible.py classify "<command>"`,
and record its tier in the report: `mundane` runs and is listed; `risky`
(overwrites or deletes production data) and `never` (the former never-list:
merging, force-pushing, deleting a project, database or backups) run and are
listed first as `review: pending`; `secret` (the output would print a secret
value) is the one refusal - use `with_secrets.py`. Before a risky or never
command, `confidence.py action --action "<command>"`: `reshard` means run the
preview or dry-run form first when the tool has one, then the real command.
No Fable verdict, no undo scripts, no rehearsals. A critical design decision
that comes up mid-run is decided with the recommended option and recorded
under "Assumptions". If the work is in a git repo with the `gh` CLI,
`pr_checkpoint.py` (`start`, `checkpoint`, `finish`) puts each decision in
its own draft PR while work continues on the next branch, and `finish`
labels the stack `review: pending` and merges every entry, in order, into the
ORIGINAL base recorded at `start` (stacked PRs are retargeted first; a queued
auto-merge does not block the later entries; `--no-merge` leaves the stack
open; a merge GitHub refuses is reported, never forced).

**Integrate** only when two or more different coding agents wrote code for
the feature: run `git add -N .` then write `git diff <run.base> > <file>` (the ORIGINAL
feature base from run.json, no `...HEAD`, so uncommitted and untracked work and
earlier checkpoints are included) and start ONE Fable
subagent with the integrator brief below, every Evidence Packet and the
diff path; it integrates the seams itself with its coding tools and returns
an Evidence Packet, which verify.py checks like any worker's; a verify
failure uses the normal re-dispatch/escalation path.

**Secrets.** The intake's `secrets.names` lists the Google Secret Manager
secrets the feature reads (names only). A command that needs one is run as
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/with_secrets.py --secret NAME[=ENV_VAR] -- <command>`,
which fetches the value with the user's gcloud sign-in and masks it in the
output. Never print, echo, log, commit or quote a secret value.

**The GLM tier.** When the route's `runner.kind` is `external` (the route's
`provider` is `glm` and the Claude CLI and an OpenRouter key are present on
this machine, or a `--no-bs` card below, whatever its `provider`), write the Task Card to `runner.card_file` and run `runner.command`
in the shell instead of starting a subagent: it launches a child `claude -p`
on GLM 5.3 Flash through OpenRouter (key from Google Secret Manager or
`OPENROUTER_API_KEY`), checks the session budget itself, and prints the
Evidence Packet. Exit 3: OpenRouter unavailable, start the routed Claude
subagent instead. Exit 4: the session budget is spent. Otherwise
(`runner.kind` `subagent`) start the routed Claude subagent; the runner's
`note` says why the cheap tier was not used. `{"openrouter": {"preset": "jev"}}` in
`~/.claude/feature-ladder/providers.json` sends these cards to
`typesafe/jev-router` instead.

**`--no-bs` (Silk Compute), per feature.** When the user puts `--no-bs` on a
feature request (or asks for that feature to run "no-bs"), its cheap tier
runs on [Silk Compute](https://silkcompute.com/docs/claude-code)'s open-weight
models instead of OpenRouter, each on the tier it is capable of: mechanic
cards on DeepSeek V4.1 Flash 1017 Fast, implementer cards up to score 6 on
DeepSeek V4.1 Flash 1017, and implementer/medium cards scoring 7-14 (Sonnet
medium otherwise) on Kimi K3.1 Pro. T3 and up stay on Claude. Take the switch
out of the request text, add `"switches": ["no_bs"]` to the answers in step 4
of section 2, and build each of that feature's cards with
`python3 .feature-ladder/v1.21.0/complexity-router/scripts/intake.py card --no-bs '<task json>'`.
This app has no hooks and often no `run.json`, so the flag is what carries the
switch: the route's `runner` names the Silk model and its `command` already
ends in `--no-bs`. Run that command as printed, exactly like a GLM card, also
for a 7-14 card whose `provider` still says `claude` (`runner.kind` decides). A
model that is busy before doing any work hands the card to its fallback once
(Hy4, or GLM-5.3 Flash for mechanic cards). Exit 3 (no key, or the model and
its fallback both busy): start the routed Claude subagent instead. The key is
`SILK_API_KEY` from the environment, the repo's `.env` (git-ignored),
`~/.claude/feature-ladder/.env` or Google Secret Manager; without one the
runner is `subagent` and its `note` says why. The switch lasts for that
feature only: leave `--no-bs` off the next feature's cards unless the user
asks again, and never set `FEATURE_LADDER_NO_BS` for the session or put Silk in
`providers.json` (a `"silk"` block there may only change a tier's model).
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/external_worker.py check --no-bs --probe`
shows the Silk setup, the balance and each model's probe. Cost per card is the
approximate drop in the Silk balance.

## 3. Selecting skills on their own

`intake.py card` already did this. To see the choice by itself:

```bash
python3 .feature-ladder/v1.21.0/skill-selector/scripts/select_skills.py --inject --out .feature-ladder/cards/<task-id>-skills.md '{"role":"<agent>","kind":"<kind>","paths":["<files in scope>"],"text":"<goal + description>","flags":{<router booleans>}}'
```

It chooses at most three skills (within a per-role size budget) by rules, writes their
full instructions to that file, and prints a short `## Skills for this task`
block naming the file. Put that block in the subagent's prompt, not the file's
contents: the skill text is for the worker, not for you. Run it without
`--inject` to see which rule picked each skill and what was dropped. `--list`
shows every skill it can see in this session; `--scan` rescans them into a
catalog (`~/.claude/feature-ladder/skill-catalog.json`) and reports rules
whose skill is not installed, installed skills no rule reaches, and what
changed since the last scan. If the project in this session
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
  judgment. On failure re-dispatch once, then one band up once (GLM or
  Silk to Sonnet medium to Sonnet high); after that report the feature `partial`.

Acceptance: the "done when" checks are re-run without a model:
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/verify.py run --id <task> --check "<cmd>" ... --scope <paths> ... --changed <files the worker claims>`
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
- **integrator** (Fable, only after two or more coding agents): Edit only to
  reconcile seams between the agents' parts, and run the build/type/lint/test
  checks; no new feature work. Read every packet and the combined diff; find
  the seams between the agents' parts (one source of truth, contracts that
  must agree, boundaries, tests that contradict each other, the repo's
  conventions; for React + Vite also one owner per piece of state, composable
  components, `import.meta.env` in one place, the build/type/lint commands as
  the checks). Return `INTEGRATION: coherent | integrated`, the seams with
  file:line on both sides, the files you edited to fix seams, and an Evidence
  Packet with the real passing check output.

## 5. End of session (only when the user asks)

When the user says the session is done, or asks for a review:
- **Review**, only if asked: run the Codex review once over the whole
  session's diff
  (`python3 .feature-ladder/v1.21.0/cross-model-review/scripts/codex_review.py review --base <session base> --flags auto`,
  official Codex CLI signed in on this machine; `<session base>` is
  `session_base_sha` in `.feature-ladder/run.json`) and report the findings; each
  confirmed bug becomes its own feature request. A Fable second opinion only
  if the user asks for one.
- **Memory**: anything still queued, with `memory_gate.py flush` (see Memory).
- `python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/session_budget.py close`.

## Memory (Hindsight), in every mode

Memory is read once per feature and written after each feature, only as
concrete facts a future task in this repo could act on, and never through a
model when the scripts can reach Hindsight themselves.

**Reads.** `recall.py` (intake step 2) is the only read, in every mode
(plain features, `--brainstorm`, `--no-bs`, research briefs). It finds this
repository's bank, recalls with the project's tags (`tags_match:
any_strict`, at most 1000 tokens, no `observation` copies), saves the raw
result, and prints `memory`: only the facts that name the work's files or
two distinctive words of the request, with stale and duplicate memories
dropped and at most 5 kept. Paste that line as it is; `Memory: none (...)`
is a real answer, so no worker re-queries. It needs `HINDSIGHT_API_KEY` in
the environment, the project's `.env` or `~/.claude/feature-ladder/.env`.

If it prints `mcp_fallback` instead (no key here, or no network to
Hindsight) and this session has Hindsight tools from a connector:

1. If the connector is multi-bank (`list_banks` exists): run
   `python3 .feature-ladder/v1.21.0/hindsight-memory/scripts/resolve_bank.py --candidates`
   (if it reports `"is_git": false`, use the connector's default bank and go
   to 3), call `list_banks` with `query` set to the project name (and the
   repo name if it differs), save the results to `.feature-ladder/banks.json`
   as `{"banks": [...]}`, and run
   `python3 .feature-ladder/v1.21.0/hindsight-memory/scripts/resolve_bank.py --choose .feature-ladder/banks.json`.
   `action: create` means the bank does not exist yet: skip recall (there is
   nothing to find); it is created by the first write below with
   `create_bank` (`bank_id`, a `name` of `owner/repo`, a `mission` describing
   the codebase's engineering memory) and then `update_bank` with the
   `config_updates` it printed (if refused, carry on).
2. Pass that `bank_id` on every Hindsight call.
3. Make ONE `recall` with exactly the `mcp_fallback` arguments, save the
   result to `.feature-ladder/recall.json`, and run `recall.py` again with
   the same `--query` and `--paths` and `--from .feature-ladder/recall.json`.
   Never `reflect`, knowledge pages or bank edits.

If there is neither a key nor a connector, say once that memory is off and
why (the `memory` line says it), and do not invent recalled context.

**Writes.** None during a feature. After each feature, queue the worker's
memory candidates that a future task could act on:
`python3 .feature-ladder/v1.21.0/orchestration-ladder/scripts/session_budget.py remember --line "[kind] fact" ...`
(kind: decision, convention, pitfall, interface, test-strategy; name the
path, identifier or command). The gate refuses summaries, narration, vague
or duplicate lines; drop those, do not rewrite them. Then write the queue
right away, because a conversation has no end the scripts can see:
`python3 .feature-ladder/v1.21.0/hindsight-memory/scripts/memory_gate.py flush`
(no model). If it fails (no key or no network) and a connector is present,
run `memory_gate.py flush --emit` and make one `retain` per entry of
`retain_calls`, with exactly those arguments (`context: feature-ladder`, tags
`project:<repo>`, then `feature:session` or, for a brainstorm's
architecture, `architecture`, then the kind). Lines under `deferred` are over the
12-per-flush cap and stay queued; the architecture (`architecture` in the
output) is written outside that cap. No scribe subagent, no summaries.
