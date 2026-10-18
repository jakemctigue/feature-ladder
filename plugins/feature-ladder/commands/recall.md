---
description: Ask the project's Hindsight memory what it knows about a topic (decisions, conventions, pitfalls) without starting a feature run. Usage - /feature-ladder:recall <topic>
argument-hint: <topic>
---

Query Hindsight memory for: $ARGUMENTS

1. Infer the project name from the repo (package name or directory name).
2. Call `mcp__plugin_feature-ladder_hindsight__recall` with
   `query="$ARGUMENTS"`, `tags=["project:<name>"]`, `tags_match="any"`,
   `budget="mid"`, `max_tokens=2000`.
3. If that returns nothing, retry once without the tags filter and say so.
4. Summarise what memory holds, grouped as Decisions / Conventions / Pitfalls /
   Interfaces, quoting each memory briefly. If the MCP server is unreachable,
   say so plainly and point at the setup steps in the plugin README.

Do not retain anything in this command.
