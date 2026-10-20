---
name: hindsight-memory
description: How feature-ladder agents use the Hindsight MCP server (vectorize-io/hindsight) - recall before work, retain after, reflect for design questions, tag conventions and what never to store.
---

# Hindsight memory protocol

The plugin registers the Hindsight MCP server as `hindsight`. Because it is
plugin-provided, the tools appear as:

- `mcp__plugin_feature-ladder_hindsight__recall`
- `mcp__plugin_feature-ladder_hindsight__retain`
- `mcp__plugin_feature-ladder_hindsight__reflect`
- `mcp__plugin_feature-ladder_hindsight__search_knowledge_base`, `get_knowledge_page`, `list_tags`, ...

Run `/mcp` once to confirm the server shows as connected before trusting memory.
If the server is unreachable, say so in the Evidence Packet and proceed from the
repo alone; never fabricate "recalled" context.

## Recall (start of every coordinator run; workers only when the card has no Memory lines)

```
recall(query="<project name> <feature area> conventions, pitfalls, prior decisions",
       budget="mid", max_tokens=1500,
       tags=["project:<name>"], tags_match="any")
```
Ask 2-3 narrow queries rather than one broad one: conventions for the module,
prior decisions about the same feature area, known pitfalls / failed approaches.
Quote the useful snippets verbatim into the Task Card so workers do not re-query:
a worker runs its own `recall` only when the card's `Memory:` line is `none`.

## Reflect (domain experts only)

Use `reflect` when the question is "what approach fits this codebase's history?"
rather than "what did we store?". Budget `mid`; include the answer's reasoning in
DESIGN.md under *Constraints (from memory)* and mark it as reflect-derived.

## Retain - two moments

**A. Every subagent, before its final message.** Each worker, expert and verifier
makes sure what it learned *in its own run* is retained, and ends its final
message with a `Retained:` line listing what was stored (or `Retained: none -
<reason>`). A `SubagentStop` hook bounces any subagent whose final message lacks
that line, so do it before you write the Evidence Packet, not after. `mechanic`,
`codex-reviewer` and `memory-scribe` are exempt from the bounce (their footer is
always `none`, or written by design); they still write the line.

**B. The coordinator, at acceptance.** It ensures the feature-level facts are
retained (accepted design decision and why, interfaces other code now depends
on, the real test gate) plus a one-line outcome memory for the feature.

### Who writes the retain call - cheapest model that can do it correctly

| Rung | How it retains |
|---|---|
| `mechanic`, `evidence-collector`, `implementer`, `senior-implementer`, `second-opinion` | Calls `retain` directly (0-3 facts; cheaper than a handoff) |
| `coordinator`, `domain-expert`, `domain-expert-high`, `deep-verifier`, `architect-escalation`, `ultra-investigator` | Hands a **candidate list** to `memory-scribe` (haiku/low) which writes the retains and returns the `Retained:` footer; the expensive rung copies that footer into its own final message |

Candidate list format for the scribe (one per line, raw facts, no polishing -
the scribe does the summarising):
```
project: <name>   feature: <slug>   rung: <your agent name>
- [decision] <fact> because <why>
- [pitfall] <what happened>; <what fixed it>
- [interface] <signature/contract> in <path>
- [outcome] <one line>
```
Spawn: `Agent(subagent_type="feature-ladder:memory-scribe", model="haiku", effort="low", prompt=<list>)`.

Server side, Hindsight's own extraction/consolidation/page-refresh LLM is also
set to the cheapest adequate model (Haiku) - see `hindsight/hindsight.env.example`
in the plugin. Keep `retain_extraction_mode` at its default `concise`.

Retain one memory per durable fact, not a session dump:

```
retain(content="<one concrete fact, decision, or pitfall>",
       context="feature-ladder",
       tags=["project:<name>", "feature:<slug>", "<kind>"],
       metadata={"source": "feature-ladder", "rung": "<agent>"})
```
`<kind>` is one of `decision`, `convention`, `pitfall`, `interface`,
`test-strategy`, `outcome`. Always include `rung:<agent>` in metadata so later
recalls can weigh who learned it. Use `sync_retain` only when a later step in
the same run must recall the fact.

What each rung retains at finish:
- `mechanic` / `evidence-collector`: usually `none`; a pitfall if a defined check
  behaved unexpectedly (flaky test, tool quirk) or a source that proved reliable.
- `implementer` / `senior-implementer`: a pitfall that cost a retry; a convention
  you discovered the hard way; an interface you introduced. 1-3 memories.
- `domain-expert*`: the key decision and the rejected options with reasons
  (one retain each), constraints that are not written down anywhere in the repo.
- `second-opinion` / `deep-verifier` / `architect-escalation` / `ultra-investigator`:
  the root cause and the evidence that settled it; what *looked* like the cause
  but was not. These are the highest-value memories in the system.
- `coordinator`: feature outcome, accepted design decision, real test gate.

Worth retaining: the accepted design's key decision and why; interfaces that
other code now depends on; a pitfall that cost a retry; a test command that is
the real gate for an area; a wrong hypothesis that was expensively disproved.
Not worth retaining: progress narration, file lists, anything already in the
repo's docs, anything another rung in this run already retained (check the
`Retained:` lines in packets you received).

Final-message footer, exact form:
```
Retained: <n> - <kind>: <first ~10 words>; <kind>: ...
```
or
```
Retained: none - <nothing durable learned | hindsight unreachable>
```

## Never store

Secrets, tokens, credentials, personal data about people, or anything the user
marked confidential. Enable Hindsight's Memory Defense on the bank if the repo
handles secrets (`update_bank` with the memory-defense policy, or via the UI).

## Bank and tags

Each repository gets its own bank, chosen deterministically by
`scripts/resolve_bank.py` and applied by hooks; agents never choose a bank.

- At session start the resolver finds the repository's bank or creates it:
  a pinned id in `<repo>/.feature-ladder/hindsight.json` wins; otherwise the
  first existing bank among `coding-agent::<project>`, `<owner>-<repo>` (from
  the git remote) and `<project>`; otherwise it creates
  `coding-agent::<project>`. `<project>` is the main worktree's folder name, so
  linked worktrees share the bank. The result is cached per repository in
  `~/.claude/feature-ladder/banks.json`.
- A PreToolUse hook adds that `bank_id` to every Hindsight tool call. Do not
  pass `bank_id` yourself, and do not call `create_bank` or `list_banks`.
- Outside a git repository no per-repo bank is used; calls go to the default
  bank (`HINDSIGHT_BANK_ID`).
- Keep tagging every retain with `project:<name>` and `feature:<slug>`; tags
  still scope recalls inside a bank and keep memories portable if banks are
  merged later.

To move a repository to a different bank, write
`{"bank_id": "<id>"}` to `<repo>/.feature-ladder/hindsight.json`, or run
`resolve_bank.py --refresh` after renaming.
