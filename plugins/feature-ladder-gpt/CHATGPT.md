# Feature Ladder GPT for ChatGPT desktop

Download `feature-ladder-gpt-chatgpt-skill.zip` or
`feature-ladder-gpt-chatgpt.zip` from
[release 0.1.1](https://github.com/jakemctigue/feature-ladder/tree/codex/feature-ladder-gpt-packages/releases/feature-ladder-gpt/0.1.1).
The skill ZIP contains one top-level `feature-ladder-gpt` directory with
`SKILL.md` and its complete runtime. The plugin ZIP contains the manifest,
skills and runtime at its root. Neither ChatGPT archive installs lifecycle hooks.

## Install the skill

In ChatGPT, open **Plugins → Skills → Create → Upload from your computer**
and select `feature-ladder-gpt-chatgpt-skill.zip`. Availability depends on your
plan and workspace settings. Start a new task and invoke the installed skill.

For a workspace plugin, an eligible administrator can upload
`feature-ladder-gpt-chatgpt.zip` through **Admin console → Plugins → Add →
Upload plugin**. This package is marked as requiring local execution.
GitHub publication does not install it into a ChatGPT account or publish it
in the OpenAI directory.

## Execution requirements

Use a ChatGPT desktop environment with local file and command access, Python
3.11+ and a signed-in Codex CLI. The skill runs the bundled dispatcher with
`exec`, which passes the exact selected model, effort and complete skill text
to Codex. Model assignments and workflow limits are in [CODEX.md](README.md).

The ChatGPT entry point does not assume its native subagent API accepts the
Codex desktop arguments. It uses the explicit CLI launcher. The CLI's own
sandbox and approval policy govern the work. Workspace-write mode requires
an authorized coding task in that workspace; unrestricted execution is never
selected by the dispatcher.

If local execution, the CLI, or a configured model is unavailable, stop and
report the missing requirement. Do not claim that ordinary ChatGPT conversation
mode routed a coding agent. Inspection can still produce a task card, but
execution requires the runtime above. Live ChatGPT upload and authenticated
model dispatch have not been verified by the offline package tests.

## Package integrity

`SHA256SUMS.json` beside the downloads records every archive's hash. Rebuild
with `python scripts/package_codex.py`, then verify extracted archives with
`python scripts/check_archives.py`. Archives are reproducible from identical
source bytes and exclude credentials, local task audits, installed third-party
skills, test workspaces and Python caches.

Official references: [Skills in ChatGPT](https://help.openai.com/en/articles/20001066-skills-in-chatgpt),
[plugin upload](https://help.openai.com/en/articles/20001256-plugins-in-chatgpt),
[desktop execution flag](https://developers.openai.com/plugins/deploy/desktop-only).
