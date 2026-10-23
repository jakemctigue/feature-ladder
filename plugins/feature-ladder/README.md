# feature-ladder (Claude Code plugin)

Orchestration ladder for adding features, atomically and without waiting:
one cheap worker per feature (GLM 5.3 Flash through OpenRouter, or Sonnet
medium), sized by a Hindsight-backed confidence surface (`confidence.py`:
>= 0.60 proceeds, below that the card is resharded), checked by a script,
then merged and labelled `review: pending`. Outside-repo steps (deploys, data
steps, the merge) run and are logged by the audit hook, never gated; the one
denial left is a command that would print a secret value. Fable is reached
on the run's own initiative only as the `integrator`, when two or more
different coding agents wrote code for one feature. A session subagent
budget you pick once (Small 1-2, Medium 3-4, Large up to 8) is enforced by
hooks. Codex review, second opinions and memory run once per session at
`/feature-ladder:wrap`. Model choice (`skills/complexity-router/scripts/route.py`)
and per-task skill choice (`skills/skill-selector/scripts/select_skills.py`)
are deterministic, and Hindsight memory sits behind a strict gate
(`memory_gate.py`).

Full documentation, install steps and the skill-selection rule format:
https://github.com/jakemctigue/feature-ladder

```bash
claude plugin marketplace add jakemctigue/feature-ladder
claude plugin install feature-ladder@feature-ladder
```

Commands: `/feature-ladder:feature [--no-merge]`, `/feature-ladder:wrap`,
`/feature-ladder:workers`, `/feature-ladder:scope`, `/feature-ladder:route`,
`/feature-ladder:skills`, `/feature-ladder:recall`,
`/feature-ladder:codex-review`.

Environment: `HINDSIGHT_MCP_URL` (default Hindsight Cloud),
`HINDSIGHT_API_KEY`, `HINDSIGHT_BANK_ID` (default `feature-ladder`),
`FEATURE_LADDER_CONFIDENCE_THRESHOLD` (default `0.60`).

MIT License.
