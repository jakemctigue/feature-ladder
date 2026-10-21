---
description: Choose where the cheap rungs run - GLM 5.3 Flash through OpenRouter (the default whenever a key is present; bills OpenRouter credits, not your Claude plan), TypeSafe's Jev Router instead, or in-session Claude subagents - and check the setup. Usage - /feature-ladder:workers [glm | jev | claude | check | --probe]
argument-hint: [glm | jev | claude | check | --probe]
---

Arguments: $ARGUMENTS

- `glm` (the default): write `{"workers": "auto", "openrouter": {"preset": "glm"}}`
- `jev`: write `{"workers": "auto", "openrouter": {"preset": "jev"}}`
  (cheap cards go to `typesafe/jev-router`, which picks a model and effort per
  request at that model's price; the token ledger shows what each card cost)
- `claude`: write `{"workers": "claude"}` (the GLM tier off; cheap cards run
  as Haiku / Sonnet-medium subagents)

into `~/.claude/feature-ladder/providers.json` (create the folder; merge into
the file if it exists, keeping other keys). `openrouter` is accepted as a
synonym for `glm`. Then run the check below.

- `check` (or no argument): run
  ```
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/external_worker.py" check
  ```
  (`python` on Windows). Add `--probe` if the user asked for it; it sends one
  one-token request to each configured model and reports the provider that
  answered and the latency.

Show the result as a short table: workers setting, preset, claude CLI found,
key source (never the key), OpenRouter key valid / credits remaining, each
model and its probe result. Exit 3 means something is missing; say what in
one line (install the Claude CLI, put `OPENROUTER_API_KEY` in Google Secret
Manager or the environment). Do not change any other setting.

Which cards go to OpenRouter: the router's `provider: glm` cards - every
`mechanic` card and `implementer` cards at medium effort with a score of 6 or
less (T0 and small T2). Everything else runs on Claude: Sonnet medium is the
default implementation rung. Without the CLI or a key, `glm` cards run as
Claude subagents and the card says why.
