---
description: Choose where the cheap rungs run - in-session Claude subagents, or GLM 5.3 through OpenRouter in a child `claude -p` (bills OpenRouter credits, not your Claude plan) - and check the setup. Usage - /feature-ladder:workers [openrouter | claude | check | --probe]
argument-hint: [openrouter | claude | check | --probe]
---

Arguments: $ARGUMENTS

- `openrouter` or `claude`: write `{"workers": "<choice>"}` into
  `~/.claude/feature-ladder/providers.json` (create the folder; merge into the
  file if it exists, keeping other keys). Then run the check below.
- `check` (or no argument): run
  ```
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/external_worker.py" check
  ```
  (`python` on Windows). Add `--probe` if the user asked for it; it sends one
  one-token request to each configured model and reports the provider that
  answered and the latency.

Show the result as a short table: workers setting, claude CLI found, key
source (never the key), OpenRouter key valid / credits remaining, each
model and its probe result. Exit 3 means something is missing; say what in
one line (install the Claude CLI, put `OPENROUTER_API_KEY` in Google Secret
Manager or the environment). Do not change any other setting.

Which rungs go external when `workers` is `openrouter`: `mechanic` and
`implementer` (both efforts). `senior-implementer`, the experts, every Fable
rung, `codex-reviewer` and `memory-scribe` always stay on Claude.
