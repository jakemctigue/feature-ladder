---
description: "Add ONE feature with the orchestration ladder, atomically - just the code change and its tests. A Sonnet scout settles every routing variable; you answer only what is uncertain (and, once per session, which model orchestrates - Opus 5.5 xhigh or Fable 5.2 - and how many subagents the session may spawn); then one worker (GLM 5.3 Flash or Sonnet medium for most work) builds it on its own branch, the stack is merged and labelled review: pending, and what ran outside the repo is listed for you. Apart from an orchestrator you picked, Fable only integrates when two or more coding agents wrote code. No reviews, no scribes - those are /feature-ladder:wrap at the end of the session. With --brainstorm, every application and architecture detail of the whole app is settled first, in one round of questions, and the app is then split into features that are built one ladder run each. Usage - /feature-ladder:feature <description> [--auto] [--no-merge] [--orchestrator fable|opus] [--brainstorm] [--researcher] [--second-opinion] [--escalate] [--ultra]"
argument-hint: <feature description> [--auto] [--no-merge] [--orchestrator fable|opus] [--brainstorm] [--researcher] [--second-opinion] [--escalate] [--ultra]
---

Start a feature-ladder run for: $ARGUMENTS

You (this conversation) run the intake and the questions, then hand off. Do not
scope, design, or implement anything yourself. Use `python` instead of
`python3` if that is the interpreter on this machine.

## 0. Switches

Separate the request text from these switches:

| Switch | Effect |
|---|---|
| `--auto` | Ask nothing. Use the scout's values and the recommended option for every decision (the session budget defaults to Small; the orchestrator is Opus xhigh unless `--orchestrator` says otherwise). |
| `--orchestrator fable\|opus` | The model that runs the coordinator for this run: Fable 5.2, or Opus 5.5 at xhigh effort. Answers the up-front orchestrator question in advance, so it is not asked. |
| `--no-merge` | Leave the PR stack open instead of merging it. By default `finish` merges the stack into your branch and labels it `review: pending`. |
| `--brainstorm` | Before any feature is scoped: settle every application and architecture detail of the whole app in one round of questions, split the app into features, then build them one atomic run each (step 0b). The scout does it; no Opus, no Fable, no extra agent. |
| `--researcher` | Show a short research brief first (what the feature wants from the codebase, a qualitative cost label per agent, what to expect), then continue the run from the same confirmed intake without re-asking. Uses no Opus and no Fable; see step 3b. |
| `--second-opinion` | Allows the Fable `second-opinion` rung for one sharp question the coordinator cannot settle. |
| `--escalate` | Allows `deep-verifier` (Fable high) and `architect-escalation` (Fable xhigh) for a bug the workers cannot isolate. |
| `--ultra` | Also allows the ultra-investigator (Fable max). Implies `--escalate`. |

Without these, the only Fable rung a run reaches on its own is the
`integrator`, and only when two or more different coding agents wrote code
for the feature. The coordinator runs on Fable only when the user picked
Fable 5.2 as the orchestrator (the up-front question or `--orchestrator
fable`); never pick it for them.
`--codex` and `--adversarial` no longer run per feature: if given, say in one
line that reviews run once at the end of the session with
`/feature-ladder:wrap --review [--adversarial]`, and continue without them.

Everything else is detected: the branch to start from is the current one; PRs
open when there is a remote and the `gh` CLI, otherwise the same branches are
made locally; a saved intake for this exact request is reused (step 1).
Nothing in the run waits for you: deploys, data steps and the merge happen
when the feature needs them and are logged `review: pending` for you to look
at afterwards.

## 0b. Brainstorm (only with `--brainstorm`)

The request is a whole app or a large piece of one. Everything about it is
gathered here, at the very front, and asked once; only then is it narrowed
into features. `B` below is
`python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/brainstorm.py"`.

1. **Reuse.** `B find "<request text>"`. If it prints a path, the app sheet
   for this exact request was confirmed earlier: say "Reusing the app sheet
   you confirmed at <time>" in one line and go to 5.
2. **Gather.** Dispatch the scout in brainstorm mode. It scans the repo,
   settles every detail (purpose, users, workflows, data model, permissions,
   integrations; platform, stack, data store, sign-in, hosting, secrets,
   testing and the rest), drafts the feature list and runs `B decide`:
   ```
   Agent(subagent_type="feature-ladder:scoper", model="sonnet", effort="medium", name="app-brainstorm",
         prompt="Request: <request text>\nMode: brainstorm\nDecide mode: <auto | default>\nFollow your instructions exactly.")
   ```
3. **Ask once** (skip with `--auto`). Show one line of counts (`counts`:
   details, settled without asking, asked). Then ask everything with
   AskUserQuestion, at most 4 questions per call, in this order: `decisions`
   (`orchestrator` and `session_agents` when present - skip `orchestrator`
   when `--orchestrator` was given - then `build`: every feature, the first
   only, or plan only), then every item of `questions` as given (`header`,
   `question`, options; the recommended option first), then `open_text` as
   one plain numbered message the user answers in their own words. This is
   the only round of questions in the whole run.
4. **Confirm.** Write `{"answers": {"<detail>": "<the chosen label, or the user's words>"}, "decisions": {"<id>": "<chosen label>"}}`
   (with `--auto` and no session budget yet: `"decisions": {"session_agents": "Small"}`)
   to `.feature-ladder/brainstorm/<slug>-answers.json` and run
   `B apply <the sheet path the scout returned> .feature-ladder/brainstorm/<slug>-answers.json`.
   It writes the confirmed app sheet (`readable`: a `.md` to point the user
   at) and queues the features in build order. If any feature has
   `"revise": true` (the user changed a detail it covers), or
   `feature_errors` is not empty, dispatch the same scout once more with
   `Mode: brainstorm revise\nApp sheet: <written_to>` and nothing else.
   Tell the user in one short message: the sheet's path, the features in
   order (one line each), what was dropped, and what `build` will do.
5. **Build from the queue.** `B next` returns the next feature. While
   `feature` is not null:
   - run steps 1 to 5 below for `feature.request` exactly as for a single
     feature, in `--auto` mode (nothing is asked again; the session answers
     are already stored), adding `"brainstorm": <feature.brainstorm>` to the
     answers file in step 3 and `App context: <feature.brainstorm.context>`
     to the scout's prompt in step 1;
   - then `B done <feature.id> --status <done | partial>` from the
     coordinator's report;
   - stop after this one when `build` is `first` or `--no-merge` was given
     (later features need the earlier ones merged); otherwise `B next` again.
   When `feature` is null, relay its `note` (finished, blocked, plan only, or
   the session budget is spent) with `remaining`, and add: "Run the same
   request with `--brainstorm` again to continue the queue."

Without `--brainstorm` none of this runs, and the scout never loads its
brainstorm instructions (`B guide`).

## 1. Intake (nothing is delegated before this finishes)

First check for a saved intake:
```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/intake.py" find "<request text>"
```
If it prints a path, that intake was confirmed by the user earlier
(`/feature-ladder:scope`, or an aborted run): use it, tell the user in one line
"Reusing the intake you confirmed at <time>", and skip to step 4.

Otherwise dispatch the scout:
```
Agent(subagent_type="feature-ladder:scoper", model="sonnet", effort="medium", name="feature-scoper",
      prompt="Feature request: <request text>\nMode: <auto | default>\nFollow your instructions exactly.")
```
(In a brainstorm run, step 0b adds an `App context: ...` block to this prompt.)

## 2. Ask (skip entirely with `--auto`)

From the scout's `decide` output:
1. Show a short summary: the predicted rung in one line, then the assumed
   values as one compact line each (`field = value - evidence`). Nothing more.
2. Ask with AskUserQuestion, at most 4 questions per call, in this order:
   `decisions` (the two session questions first when present, each asked
   once per coding session: `orchestrator` - which model runs the
   coordinator, Opus 5.5 xhigh or Fable 5.2; skip it when `--orchestrator`
   was given - then `session_agents`, how many subagents the whole session
   may spawn - Small 1-2, Medium 3-4, Large up to 8; then `workspace`), then
   `questions`. There is no "live changes" question: outside-repo steps run
   and are logged.
   Use each item's `header`, `question` and option `label`/`description` as
   given. Keep the recommended option first.
3. Map each answer back to the option's `value` (for `decisions`, record the
   chosen label). If the user typed their own answer, translate it into the
   field's value and add a note saying so.
4. If the user chose **Stop** for uncommitted changes, stop here.

## 3. Confirm

Write `{"<field>": <value>, ..., "decisions": {"<id>": "<chosen label>"}, "switches": ["second-opinion", ...], "notes": [...]}`
(with `--auto` and no session budget yet, add `"decisions": {"session_agents": "Small"}`;
with `--orchestrator <fable|opus>`, add `"orchestrator": "<fable|opus>"` at the top level)
to `.feature-ladder/intake/<slug>-answers.json` and run:
```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/intake.py" apply .feature-ladder/intake/<slug>-proposal.json .feature-ladder/intake/<slug>-answers.json
```
It writes the confirmed intake to `.feature-ladder/intake/<slug>.json` and the
session's subagent budget and orchestrator. Tell the user in one line: the
orchestrator (`orchestrator.label`), the size and what it
means (`budget.shape`), the rung and where it runs (GLM or Claude), the
subagents left in the session, whether the stack merges or stays open, and
"Running now; no more questions." For example: "Orchestrator Opus 5.5 xhigh. Size S: one GLM 5.3 Flash
worker, no design, no review. 3 of 4 session subagents left. Merges when
green, labelled review: pending. Running now." If `budget.workers` is 0 the
session budget is spent: say so and stop.

## 3b. Researcher (only with `--researcher`)

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/brief.py" find "<request text>"
```
If it prints a path, a brief for this exact request is saved: run
`brief.py show "<request text>"` and show it. Otherwise, only if the request
names an external library or API the repo does not already use, dispatch ONE
`feature-ladder:evidence-collector` (Haiku medium; at most 8 bullets, saved to
`.feature-ladder/intake/<slug>-evidence.md`), then build the brief from the
confirmed intake:
```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/brief.py" build --proposal .feature-ladder/intake/<slug>-proposal.json --intake .feature-ladder/intake/<slug>.json --switches researcher[,<other switches>] [--external "<name>" --evidence <file>]
```
It writes `.feature-ladder/intake/<slug>-brief.md` and prints it; show it. Then
continue to step 4 in the same turn: the intake is already confirmed, so do not
ask anything again.

## 4. Branch

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/pr_checkpoint.py" start --slug <slug> --workspace <include|stash from the intake> --intake .feature-ladder/intake/<slug>.json
```
Exit 2 means the folder is not a git repo: continue without branches. Mention
`pr_disabled_reason` in one line if PRs are off.

## 5. Hand off

The coordinator's `model` and `effort` are the confirmed intake's
`orchestrator.model` and `orchestrator.effort` (`opus` / `xhigh`, or `fable` /
`medium` when the user picked Fable 5.2). Copy them exactly; a hook denies a
Fable coordinator the user did not pick. Switches are the intake's `switches`. Secrets: the intake's `secrets.names`
lists the Secret Manager secrets the feature reads (names only).

```
Agent(
  subagent_type="feature-ladder:coordinator",
  model="<orchestrator.model from the intake: opus | fable>",
  effort="<orchestrator.effort from the intake: xhigh | medium>",
  name="feature-coordinator",
  prompt="Feature request from the user: <request text>

Confirmed intake (binding): <path to .feature-ladder/intake/<slug>.json>
Budget: size <S|M|L> - <budget.shape>
Run branch: <branch from step 4, or 'none - not a git repo'>  Base: <base>  PRs: <enabled | disabled: reason>
Merge: <finish merges the stack and labels it review: pending | the user asked for --no-merge: finish with --no-merge>
Project: infer the project name from the repo (package name, pyproject/package.json name, or directory name) and use it as the `project:<name>` tag for all Hindsight calls.
Switches ENABLED by the user: <second-opinion, escalate, ultra | none - no Fable rung but the integrator, no reviews>.
App context (only in a --brainstorm run; binding, settled with the user): <the intake's brainstorm.context, verbatim | none>
Secrets (Google Secret Manager, project <project>): <names, or 'none'>. Values never enter the transcript: workers run commands that need one through with_secrets.py.
Run to completion. Do not stop to ask the user anything: decide non-critical choices yourself, park critical design decisions in checkpoint PRs, and run the outside-repo steps the feature needs (the audit hook logs them with review: pending; there is no policy and no approve step).
Implement only this feature and its tests. Stay within the budget: it is the whole protocol. Recall once and save it. Run confidence.py card before each card and reshard once when it says so; confidence.py action before a risky or never-tier command. Build each Task Card with intake.py card. Dispatch the integrator (Fable) once if two or more different coding agents wrote code. Verify with verify.py. Queue memory with session_budget.py remember. Finish with pr_checkpoint.py finish. Report back with FEATURE-REPORT.md path, files changed, the exact passing check commands, the PR stack with each merge result, what is pending review, assumptions made, anything unverified."
)
```

When the coordinator returns, relay its report as-is, then one line pointing at
FEATURE-REPORT.md and one line listing what is labelled `review: pending`
(merged PRs, deploys, data steps) for the user to look at afterwards; if a
merge was refused by GitHub, say which PR and why. Last, run
```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/ledger.py" report
```
and show its table: it now includes the coordinator's own tokens, which the report written
during the run could not. End with one line:
"Subagents left this session: <left from session_budget.py status>. At the end of the session run `/feature-ladder:wrap` (add `--review` for one Codex pass over everything)."
