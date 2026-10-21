---
name: hindsight-memory
description: How feature-ladder uses the Hindsight MCP server (vectorize-io/hindsight) - one scoped recall per feature, memory written once per session through a deterministic gate, per-repository banks. Reference for the rules the hooks enforce.
---

# Hindsight memory, tightly controlled

Memory is worth its tokens only when a future task in the same repository
acts on it. Everything below is enforced by scripts, not by judgment:
`scripts/resolve_bank.py` (PreToolUse hook on every Hindsight tool) routes the
call to this repository's bank and applies `scripts/memory_gate.py`.

## Which bank

Each repository has its own bank, resolved deterministically at session
start: a pinned id in `<repo>/.feature-ladder/hindsight.json` wins; otherwise
the first existing bank among `coding-agent::<owner>-<repo>`,
`<owner>-<repo>`, `coding-agent::<project>`, `<project>`; otherwise
`coding-agent::<owner>-<repo>` is created. Linked worktrees share the main
checkout's bank. Agents never pass `bank_id`, `list_banks` or `create_bank`.

## Reads: one scoped recall

- `recall` only. The hook forces `tags: ["project:<name>"]`, `tags_match:
  "all"`, `max_tokens <= 1000` and `budget: low` unless given.
- At most 3 recalls per feature run: the intake scout, the coordinator, and
  one worker whose card's `Memory:` line is `none`. More are denied.
- The query names the feature area and its files. The coordinator pastes
  only the snippets that bear on the card into `Memory:`; workers do not
  re-query.

## Writes: once per session, through the gate

- No `retain` during a feature run; the hook denies it.
- Workers put facts under `Memory candidates:` as `[kind] fact`; the
  coordinator queues the ones a future task could act on with
  `session_budget.py remember`, which checks each line on the spot.
- `/feature-ladder:wrap` runs `memory_gate.py flush`: it re-checks the queue
  and writes the survivors straight to the bank over MCP. No model reads,
  summarises or rewords them.

A memory passes the gate only if it is:
- `[kind] fact` with kind `decision`, `convention`, `pitfall`, `interface`
  or `test-strategy` (no `outcome`: what a feature did is in git and the PR);
- one fact, 25-280 characters, at most two sentences;
- anchored: it names a path, identifier, command, flag or `token` in the repo;
- not narration or a summary ("we implemented", "successfully", "this
  session", "overall", ...);
- not already retained from this repo (`.feature-ladder/memory-ledger.jsonl`).

Stored with `context: feature-ladder`, tags `project:<name>`, `feature:<slug>`
and the kind. Never secrets, tokens, credentials or personal data.

## Everything else is off

`reflect`, knowledge pages, tag listing and bank edits are denied to agents:
`reflect` is a free-form summary written by the server's own model.
Server-side, Hindsight's fact extraction and consolidation run on Haiku
(`hindsight/hindsight.env.example`); keep `retain_extraction_mode` at
`concise`.

Check a line by hand: `python3 scripts/memory_gate.py check "[pitfall] ..."`.
