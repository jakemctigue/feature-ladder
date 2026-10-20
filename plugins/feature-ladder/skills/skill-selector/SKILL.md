---
name: skill-selector
description: Deterministic per-task skill selection for feature-ladder workers. Run after the complexity router for every Task Card; it decides which skills the worker gets and produces the block to inject into the card.
---

# Skill selector

Workers in this plugin cannot load skills themselves (`Skill` is in their
`disallowedTools`). The only skills a worker sees are the ones this selector
picks and the coordinator pastes into its Task Card. Selection is a pure
function of the task facts, the rule files and what is installed.

## Run it

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/skill-selector/scripts/select_skills.py --inject --out .feature-ladder/cards/<task-id>-skills.md '{
  "role": "implementer",
  "kind": "code",
  "paths": ["src/upload.py", "tests/test_upload.py"],
  "text": "Fix crash when the S3 upload times out; add retry with backoff",
  "flags": {"cross_system": true}
}'
```

With `--out` the skills' full text is written to that file and the command
prints a three-line `## Skills for this task` block naming the file by its
absolute path. Paste that block at the end of the Task Card; the worker reads
the file first. The skill text never enters the coordinator's context or its
output, which is where most of a run's expensive tokens used to go. The file is
git-ignored under `.feature-ladder/`.

Without `--out`, `--inject` prints the full block itself (for dry runs and for
callers that cannot write files).

- `role`: the router's `agent` value (or the agent you are about to dispatch).
- `paths`: the card's Scope, exactly as written there.
- `text`: the card's goal line plus its description; keywords match this.
- `flags`: the router booleans you already filled in.
- `add` / `exclude`: explicit coordinator overrides; use only when a worker
  returned `blocked` for a missing skill, and record why in FEATURE-REPORT.md.

Without `--inject` or `--out` it prints JSON: chosen skills with the rule and matches that
selected each, skills dropped for `max_skills` / `max_chars`, and names that are
not installed. `--list` prints every skill it can resolve and every rule that
points at a skill that is not installed.

## Rules

Rules live in `skill-rules.json`, merged in this order (same `id` replaces,
`disable` removes):

1. `${CLAUDE_PLUGIN_ROOT}/skills/skill-selector/skill-rules.json` - shipped defaults
2. `~/.claude/feature-ladder/skill-rules.json` - personal rules, survive plugin updates
3. `<repo>/.feature-ladder/skill-rules.json` - per-project rules

A rule fires when every group in `when` matches (any value within a group) and
nothing in `unless` matches:

```json
{"id": "bigquery", "skill": "data-agent-kit:bigquery-sql", "priority": 80,
 "when": {"keywords": ["bigquery", "bq"], "roles": ["implementer", "senior-implementer"]},
 "unless": {"keywords": ["prototype"]}}
```

Groups: `roles`, `kinds`, `globs` (matched against each scope path and its file
name; `*` also crosses folders), `keywords` (whole-word, case-insensitive),
`all_keywords`, `flags`. Highest priority wins; ties keep file order. At most
`max_skills` skills and `max_chars` characters of skill text are injected per
card; `max_chars_by_role` (keyed by the router role) overrides `max_chars` for
that role, so a mechanic gets none and a senior implementer gets more than an
implementer. Anything cut is reported, never silently included.

Skill names: personal and project skills by their own name (`aws-cdk`,
`eh:debug`); plugin skills as `plugin:skill` (`superpowers:test-driven-development`).

## When a rule is wrong

Do not override it in the moment. Note the miss in FEATURE-REPORT.md with the
task JSON and what should have been selected; the user adjusts the rule file.
