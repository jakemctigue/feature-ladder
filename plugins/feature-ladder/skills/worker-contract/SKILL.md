---
name: worker-contract
description: The short contract every feature-ladder worker (implementer, senior-implementer, domain experts, Fable verifiers) carries - how to read a Task Card, stay in scope, handle secrets, defer memory to the end of the session, and return an Evidence Packet with the Retained footer. Workers load this instead of the full orchestration protocol.
---

# Worker contract

You received a Task Card from the coordinator. Everything you need is in it;
the full protocol stays with the coordinator so your context stays small.

**Read in this order:** the file named in the card's "Skills for this task"
block (the only skills you have; the Skill tool is disabled), the card's
`Memory:` lines, the `Design:` section if named, then the files in `Scope:`.

**Scope.** Edit only files in `Scope:`. Needing another file is a `partial`
return with the reason, never a silent expansion. Never merge, push to the
base branch, force-push, or delete anything outside the repo.

**Checks.** Run the `Done when:` commands and the repo's lint/format. Quote
real output; `verify.py` re-runs the same commands for acceptance, so never
report a check you did not run. If a check fails twice for reasons outside
your task, return `blocked` with the output.

**Secrets.** A command that needs an API key runs as
`python3 ${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/with_secrets.py --secret NAME[=ENV_VAR] -- <command>`
(names on the card's `Secrets:` line). Never print, echo, log, commit or quote
a value; new code reads secrets the way the repo already does. A missing or
unreadable secret is `blocked: secret NAME`; finish what does not depend on it.

**Memory.** The card's `Memory:` lines are the recall; query Hindsight yourself
only when that line is `none` and the area is unfamiliar (one narrow `recall`,
tags `project:<name>`). Never call `retain` and never hand anything to
`memory-scribe`: put 0-3 durable, new facts under `Memory candidates:`, one
per line as `[kind] fact` (kind: `decision|convention|pitfall|interface|test-strategy`),
naming the path, identifier or command it is about. Only what a future task
in this repo would act on; never a summary of what you did.
The coordinator queues them and memory is written once, at the end of the
session (`/feature-ladder:wrap`). Never include secrets or personal data, or
anything already in the repo's docs.

**Atomic.** Do the change the card asks for and its tests, nothing more: no
refactors, hardening, reviews or deploys it did not ask for. Commands with
effects outside the repo run only as far as the intake's policy allows (a
guard hook decides); follow a denial exactly and never work around it.

**Economy.** Read only the files in Scope and what they import. Do not survey
the repo, reformat untouched code, add tests beyond `Done when`, or write
documentation unless the card asks. Every turn re-reads your whole context;
finish in as few turns as the work allows.

**Return an Evidence Packet**, exactly:
```
TASK <id> - <status: done | blocked | partial>
Changed: <file list with line ranges>
Checks run: <command -> exit code -> last 5 lines>
Claims: <each factual claim with how it was verified; "unverified" is allowed and honest>
Risks: <anything you are unsure about>
Memory candidates: <facts worth retaining, one line each, or "none">
Retained: deferred - <n> candidates   |   Retained: none - <reason>
```
Never say "should work": a check ran and is quoted, or the claim is unverified.
A verifier that specifies a fix returns it as a Task Card the coordinator can
route: `TASK <id>: <one line>` / `Scope:` / `Design:` / `Done when:` / `Do not:`.
