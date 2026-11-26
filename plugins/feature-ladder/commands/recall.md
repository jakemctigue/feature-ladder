---
description: Ask the project's Hindsight memory what it knows about a topic (decisions, conventions, pitfalls) without starting a feature run. Usage - /feature-ladder:recall <topic> [-- <files>]
argument-hint: "<topic> [-- <files>]"
---

Query Hindsight memory for: $ARGUMENTS

1. Run, with the topic as the query and any files given after `--` as paths:
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/hindsight-memory/scripts/recall.py" --query "<topic>" [--paths <files>] --out .feature-ladder/recall-topic.json --cap 12
   ```
   (`python` if that is the interpreter here.) It recalls this repository's
   bank (scoped to the project's tags, untagged memories excluded, at most
   1000 tokens) and keeps only the facts that bear on the topic.
2. If it prints `mcp_fallback` (the script could not reach Hindsight), call
   `mcp__plugin_feature-ladder_hindsight__recall` once with exactly those
   arguments, save the result to `.feature-ladder/recall-topic.json`, and run
   the same command again with `--from .feature-ladder/recall-topic.json`
   instead of `--out`.
3. Summarise its `facts`, grouped as Decisions / Conventions / Pitfalls /
   Interfaces / Test strategy, quoting each briefly. If there are none, say
   what its `memory` line says (nothing stored yet, nothing about this topic,
   or why Hindsight is off) in one line. `dropped` says how many recalled
   memories were left out and why (off-topic, stale, duplicate).

Do not retain anything in this command.
