---
description: Dry-run the deterministic skill selector - show which skills a task would get and why. Usage - /feature-ladder:skills <task JSON | free text | --list>
argument-hint: <task JSON, a task description, or --list>
---

Run the skill selector for: $ARGUMENTS

- If the argument is `--list`, run:
  `python3 "${CLAUDE_PLUGIN_ROOT}/skills/skill-selector/scripts/select_skills.py" --list`
  and show the output, highlighting any rule problems.
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
