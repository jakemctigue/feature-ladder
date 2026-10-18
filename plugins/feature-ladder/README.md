# feature-ladder (Claude Code plugin)

Orchestration ladder for adding features with Claude subagents: Fable 5.1
coordinates and verifies sparingly, Opus 5.5 experts design and do the hard
implementation, Sonnet and Haiku workers handle routed coding and evidence
work. Model choice (`skills/complexity-router/scripts/route.py`) and per-task
skill choice (`skills/skill-selector/scripts/select_skills.py`) are
deterministic, and Hindsight supplies project memory.

Full documentation, install steps and the skill-selection rule format:
https://github.com/jakemctigue/feature-ladder

```bash
claude plugin marketplace add jakemctigue/feature-ladder
claude plugin install feature-ladder@feature-ladder
```

Commands: `/feature-ladder:feature`, `/feature-ladder:route`,
`/feature-ladder:skills`, `/feature-ladder:recall`.

Environment: `HINDSIGHT_MCP_URL` (default Hindsight Cloud),
`HINDSIGHT_API_KEY`, `HINDSIGHT_BANK_ID` (default `feature-ladder`).

MIT License.
