---
description: Dry-run the deterministic skill selector - show which skills a task would get and why. Usage - /feature-ladder:skills <task JSON | free text | --list | --scan>
argument-hint: <task JSON, a task description, --list, or --scan>
---

Run the skill selector for: $ARGUMENTS

- If the argument is `--list`, run:
  `python3 "${CLAUDE_PLUGIN_ROOT}/skills/skill-selector/scripts/select_skills.py" --list`
  and show the output, highlighting any rule problems.
- If the argument is `--scan` (optionally with `--check`), run:
  `python3 "${CLAUDE_PLUGIN_ROOT}/skills/skill-selector/scripts/select_skills.py" --scan`
  It rescans every installed skill into `~/.claude/feature-ladder/skill-catalog.json`
  with no model. Show: how many skills and names it found, the catalog digest,
  the rules whose skill is not installed, how many installed skills no rule
  reaches (list them only if the user asks), and what was added, removed or
  edited since the last scan. Do not write or change any rule yourself: say
  which rule file the user would edit (`~/.claude/feature-ladder/skill-rules.json`).
- If the argument starts with `{`, run:
  `python3 "${CLAUDE_PLUGIN_ROOT}/skills/skill-selector/scripts/select_skills.py" '$ARGUMENTS'`
- Otherwise build the task JSON as `{"role": "implementer", "kind": "code", "text": "$ARGUMENTS"}`
  and run the same command with it.

(Use `python` instead of `python3` if that is the interpreter on this machine.)

Show the chosen skills with the rule that picked each, anything dropped for
budget, and any names that are not installed. If you built the JSON from free
text, say that `role`, `paths` and `flags` were guessed and that real cards fill
them from the router and the card's scope. Do not dispatch any agent and do not
load any skill.
