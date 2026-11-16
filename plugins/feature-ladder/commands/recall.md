---
description: Ask the project's Hindsight memory what it knows about a topic (decisions, conventions, pitfalls) without starting a feature run. Usage - /feature-ladder:recall <topic>
argument-hint: "<topic>"
---

Query Hindsight memory for: $ARGUMENTS

1. Call `mcp__plugin_feature-ladder_hindsight__recall` once with
   `query="$ARGUMENTS"` plus exactly the arguments the session-start line
   printed for this repository (`bank_id`, `tags=["project:<name>"]`,
   `tags_match="all"`, `max_tokens=1000`, `budget="low"`,
   `types=["world","experience"]`). The memory gate forces these values
   whatever you pass; sending them yourself means the hook rewrites nothing
   and the call runs the first time. If you do not have that line, pass only
   `query` and, when the call is returned with a rewritten input, repeat it
   with that input.
2. Do not retry without the tags filter: the gate adds it back. An empty
   result means this repository has no memory on the topic; say so.
3. If the call is denied with "no memories yet", the repository's bank has not
   been created (it is created by the first `/feature-ladder:wrap` that writes
   a memory); say that.
4. Summarise what memory holds, grouped as Decisions / Conventions / Pitfalls /
   Interfaces / Test strategy, quoting each memory briefly. If the MCP server
   is unreachable, say so plainly and point at the setup steps in the plugin
   README.

Do not retain anything in this command.
