# Codex execution adapter

These rules translate the source's Claude-specific tool calls. Preserve the
source roles, score bands, skill rules and budgets. Where the translated brief
mentions Claude tools or contradicts this adapter, use this adapter.

- All dispatches use `scripts/dispatch.py`, followed by native `spawn_agent`
  with its exact arguments. The hook route uses `marker`; the standalone route
  uses `spawn` (full context). Never call the source's `Agent(...)` examples or
  hand-select a role/model. Use a fresh context (`fork_turns: none`). Complete
  required skills are already injected; no need to reopen their text files.
- To launch children, write structured task JSON and run the dispatcher.
  `phase` selects scope/scoper, coordinate/coordinator, work/router,
  design/domain expert, opinion/second opinion, review/Codex driver, or
  memory/scribe. Escalation uses `phase: escalate`, `previous_role` and a written
  `justification`; Ultra also requires the user's `ultra: true`. The chain is
  coordinator -> deep-verifier -> architect-escalation -> ultra-investigator.
- `work` with a hard flag or missing senior design returns to the coordinator,
  never to an implementer. Domain-expert-high includes the base expert brief.
  For implementation retries, keep the same route for one retry; after two
  failures use `phase: retry`, `failures: 2`, and a written `justification` to
  climb exactly one score band. Senior implementation still needs an agreed
  design. Never change task estimates simply to force a desired model.
- Size S permits one worker and ~12 coordinator turns; M permits at most two
  independent workers and ~20; L permits up to four and ~45. Respect the host's
  lower concurrency limit by queuing. Do not spend extra agents on S/M design,
  memory scribes or reviews unless the source's explicit review switch allows
  review. L uses one scribe at the end. Expensive agents return memory candidates
  to the coordinator rather than spawning one scribe per task.
- Use the source `intake.py card` to record routing and selections. For actual
  dispatch, pass its task facts to `dispatch.py`; the latter recomputes selection
  and includes full contents. Never treat a filename or a skill name alone as
  a loaded skill. Returned `add` requests need an updated task; arbitrary
  worker skill discovery is outside the contract.
- `Read`, `Grep`, `Glob`, and `Bash` mean the host's read/search/exec tools.
  Use the host's question tool or plain chat for intake questions. Instructions
  to continue do not expand user authorization or bypass host permissions.
- Use the connected Hindsight tool's actual schema; do not invent tool names
  or add unsupported `bank_id` arguments. If the configured direct Hindsight
  MCP supports bank_id, use the bundled resolver and its per-repo identity.
  Memory is optional when unavailable. Honor any host-provided automatic memory
  hook and avoid duplicate retain calls. `Retained: none - host hook handles
  memory` is valid when applicable. No separate model runs merely to summarize
  a closing response when the host already does that.
- Verification is `skills/orchestration-ladder/scripts/verify.py` at every size.
  The original source's occasional mechanic-for-every-check wording is stale.
  Mechanics only run review repros or checks requiring interpretation. Never
  accept a task without real verification output.
- `codex-reviewer` stays a Luna/low driver. Its script now pins a second GPT
  session (GPT-5.6 Sol), with the source's deterministic medium/high effort rule.
  This is an independent GPT review, not cross-provider diversity. Review is
  automatic only for L or explicitly requested via `--codex`; adversarial is
  only `--adversarial`. Preserve the source's one-attempt timeout behavior.
- Use `scripts/ledger.py report` for Codex usage. Missing telemetry is unknown,
  never zero usage. Codex cumulative token counters must not be summed per turn.
- The plugin's pre-tool hook preserves the original outside-command classifier
  for active runs. Its pattern matching is a guardrail, not a sandbox. A skill
  alone cannot enforce tool restrictions. A model verdict never grants user
  permission. The native runtime's sandbox and approvals remain authoritative.
- Use authorized isolated worktrees for senior implementation, passing their
  path to verification. Never change the original user's working branch to
  accommodate an unrelated task. Never merge, force-push, or push the base.
- The original maxTurns and size turn budgets are workflow ceilings recorded
  in the manifest; Codex native spawning does not enforce Claude's maxTurns
  field. Stop and report budget exhaustion rather than claiming hard runtime
  enforcement. Scope judgments and model reasoning are not deterministic;
  the route and skill payload are deterministic for fixed facts and inventory.
