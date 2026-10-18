---
description: Dry-run the deterministic complexity router on a task description or JSON and show which rung (model/effort) it would pick. Usage - /feature-ladder:route <task description or JSON>
argument-hint: <task description or JSON fields>
---

Run the router on: $ARGUMENTS

If the argument starts with `{`, run:
```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/route.py" '$ARGUMENTS'   # or `python` on Windows
```
Otherwise run:
```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/complexity-router/scripts/route.py" --describe "$ARGUMENTS"   # or `python` on Windows
```

Show the JSON result, then explain in two or three sentences which fields drove
the score and which fields the `--describe` pre-fill could not know (files, loc,
modules, novelty, blast_radius, design_agreed) so the user can supply them for an
exact answer. Do not dispatch any agent.
