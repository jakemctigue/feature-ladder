---
description: Get an independent second opinion from OpenAI Codex on your current work, optionally with adversarial tests. Reviews the commits since main (or origin/main) if there are any, otherwise your uncommitted changes. Usage - /feature-ladder:codex-review [--adversarial]
argument-hint: [--adversarial]
---

Run a cross-model review with arguments: $ARGUMENTS

1. Dispatch `feature-ladder:codex-reviewer` (model haiku, effort low) with a
   Task Card whose command is:
   `python3 "${CLAUDE_PLUGIN_ROOT}/skills/cross-model-review/scripts/codex_review.py" review --slug manual-<timestamp>`
   (the script picks the scope itself: commits since `origin/main`, `main` or
   `master` when there are any, otherwise uncommitted changes).
2. If `--adversarial` is in the arguments, dispatch it again afterwards with
   `adversarial` in place of `review` and the same slug.
3. Show the user Codex's findings as returned, grouped by severity, then for
   each critical or high finding say in one line whether you agree, based on
   the code, and why. Do not fix anything unless the user asks.
