---
description: Get an independent second opinion from OpenAI Codex on the current branch or uncommitted changes, optionally with adversarial tests. Usage - /feature-ladder:codex-review [--base <ref>] [--uncommitted] [--adversarial]
argument-hint: [--base <ref>] [--uncommitted] [--adversarial]
---

Run a cross-model review with arguments: $ARGUMENTS

1. Dispatch `feature-ladder:codex-reviewer` (model haiku, effort low) with a
   Task Card whose command is:
   `python3 "${CLAUDE_PLUGIN_ROOT}/skills/cross-model-review/scripts/codex_review.py" review <--base/--uncommitted from the arguments, if any> --slug manual-<timestamp>`
2. If `--adversarial` is in the arguments, dispatch it again afterwards with
   `adversarial` in place of `review` and the same scope and slug.
3. Show the user Codex's findings as returned, grouped by severity, then for
   each critical or high finding say in one line whether you agree, based on
   the code, and why. Do not fix anything unless the user asks.
