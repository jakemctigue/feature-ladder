# feature-ladder (Claude Code plugin)

Orchestration ladder for adding features, atomically: one cheap worker per
feature (GLM 5.3 Flash through OpenRouter, or Sonnet medium), checked by a
script, under a session subagent budget you pick once (Small 1-2, Medium 3-4,
Large up to 8) and hooks that enforce it. No reviews, scribes or Fable rungs
inside a feature run; Codex review, second opinions and memory run once per
session at `/feature-ladder:wrap`. Model choice
(`skills/complexity-router/scripts/route.py`) and per-task skill choice
(`skills/skill-selector/scripts/select_skills.py`) are deterministic, and
Hindsight memory sits behind a strict gate (`memory_gate.py`).

Full documentation, install steps and the skill-selection rule format:
https://github.com/jakemctigue/feature-ladder

```bash
claude plugin marketplace add jakemctigue/feature-ladder
claude plugin install feature-ladder@feature-ladder
```

Commands: `/feature-ladder:feature`, `/feature-ladder:wrap`,
`/feature-ladder:workers`, `/feature-ladder:scope`, `/feature-ladder:route`,
`/feature-ladder:skills`, `/feature-ladder:recall`,
`/feature-ladder:codex-review`.

Environment: `HINDSIGHT_MCP_URL` (default Hindsight Cloud),
`HINDSIGHT_API_KEY`, `HINDSIGHT_BANK_ID` (default `feature-ladder`).

MIT License.
