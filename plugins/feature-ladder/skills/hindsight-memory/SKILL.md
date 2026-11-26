---
name: hindsight-memory
description: How feature-ladder uses Hindsight (vectorize-io/hindsight) in every mode - one scripted recall per feature that keeps only the facts bearing on the work, memory written through a deterministic gate when a session ends, per-repository banks. Reference for the rules the scripts and hooks enforce.
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
`<owner>-<repo>`, `coding-agent::<project>`, `<project>` (and the ids earlier
versions used). `<owner>` is the remote's whole namespace (group and subgroup),
so two repositories never share a bank by accident. If none exists, nothing is
created yet: `coding-agent::<owner>-<repo>` is created by the first memory
write, so opening a session in a repository never leaves an empty bank
behind. Linked worktrees share the main checkout's bank. Agents never call
`list_banks` or `create_bank`.

## Where the key comes from

`HINDSIGHT_API_KEY` (and `HINDSIGHT_MCP_URL`, `HINDSIGHT_BANK_ID`) are read
from the environment, then `<repo>/.env`, then `~/.claude/feature-ladder/.env`,
then (Windows) the user's saved environment variables. Without a key the
session-start line says plainly that memory is OFF and why; nothing fails
silently.

## Reads: one scripted recall, only relevant facts

Every mode (feature runs, `--brainstorm`, `--researcher`, `--no-bs`,
`/feature-ladder:research`, `/feature-ladder:recall`, the Claude app) reads
memory through one script, with no model:

```
python3 scripts/recall.py --query "<request or card title>" --paths <files> [--reuse FILE] [--out FILE]
```

- It recalls this repository's bank with `tags: ["project:<repo>",
  "project:<folder>"]`, `tags_match: "any_strict"`, `max_tokens <= 1000`,
  `budget: low`, `types: ["world", "experience"]`. `any_strict` matters:
  with `all` or `any` Hindsight also returns every untagged memory in the
  bank. The repo tag is what every clone shares; the folder tag finds
  memories earlier versions wrote. Writes carry the repo tag.
- It saves the raw result (default `.feature-ladder/runs/<slug>/recall.json`
  during a run) for `confidence.py`, and prints `memory`: the `Memory:` line
  to paste, verbatim. No model reads the raw recall.
- Only facts that bear on the work survive (`confidence.select_relevant`,
  shared with `confidence.py` and `external_worker.py`): a fact must name one
  of the paths (or a distinctive file stem) or two distinctive words of the
  query; duplicates go; a fact whose named repository paths no longer exist
  is stale and goes; the rest are ranked by file hits, word hits and recency
  and capped at 5. `Memory: none (...)` says why nothing is left, and is a
  real answer: workers do not re-query.
- The app's architecture, stored by a brainstorm (`brainstorm.py remember`)
  as one `` [decision] `arch:<detail>` Header: value `` fact per detail, is
  chosen apart: a feature rarely repeats its architecture's words ("add a
  sign-up page" never says "Firebase Auth"), so an `arch:` fact counts when
  the work touches its area (`confidence.ARCH_WORDS`: sign-in, data,
  screens, deploys...) and the stack, layout and tests always count. Only
  the newest decision per detail is kept, at most 4 (`--arch-cap`), listed
  first and on top of the 5 facts. `--no-architecture` leaves them out (a
  `--brainstorm` feature run, whose cards carry the App context); the
  brainstorm itself passes `--arch-cap 40` to see all of it.
- The scout recalls once and saves to `.feature-ladder/intake/<slug>-recall.json`;
  the coordinator passes it as `--reuse`, so a feature makes one network
  recall. A reshard filters the same file per shard.
- When the script cannot reach Hindsight it prints `mcp_fallback`: the exact
  arguments for one MCP `recall`; save that result and run `recall.py --from`.
  The PreToolUse hook forces the same arguments on any direct MCP recall.
- Before the first memory is written there is no bank: recall answers
  "no memories yet" without a network call. A cached "no bank yet" is
  re-checked at session start, so a bank another clone created is found.
- At most 3 recalls per feature run are counted; more are denied.
- `confidence.py` scores only the relevant memories into the confidence
  surface; a pitfall about another part of the repository no longer makes a
  card look risky.

## Writes: through the gate, whenever a session ends

- No `retain` during a feature run; the hook denies it, also on any other
  Hindsight MCP server the user has.
- Workers put facts under `Memory candidates:` as `[kind] fact`; the
  coordinator queues the ones a future task could act on with
  `session_budget.py remember`, which checks each line on the spot.
- `memory_gate.py flush` re-checks the queue and writes the survivors
  straight to the bank over MCP. No model reads, summarises or rewords them,
  and the bank is set to `retain_extraction_mode: verbatim`.
- It runs unattended from the SessionStart and SessionEnd hooks
  (`flush --background`: detached, never during an active run, never from a
  GLM child, not again for 10 minutes after a failure), so a session that
  ends without `/feature-ladder:wrap` still stores its memory. Wrap runs it
  in the foreground and shows what was written.
- At most 12 memories per flush. Lines over the cap are reported as
  `deferred` and stay in the queue; nothing is dropped.
- A brainstorm's architecture (`brainstorm.py remember`) has its own queue,
  `.feature-ladder/session-memory-architecture.md`: the same gate, written
  first and outside the 12-per-flush cap (one fact per detail, at most 40),
  tagged `architecture` instead of `feature:session`. `remember` starts the
  background flush right away, before the first feature run, so later runs
  and other machines can recall it. `flush --emit` prints `retain_calls`,
  the exact arguments for each `retain` when the Claude app's connector
  makes the writes.
- A fact counts as retained only after the write happened (flush records it
  after the call; for a hand-made `retain` the PostToolUse hook does). A write
  that failed can be retried.

A memory passes the gate only if it is:
- `[kind] fact` with kind `decision`, `convention`, `pitfall`, `interface`
  or `test-strategy` (no `outcome`: what a feature did is in git and the PR);
- one fact, 25-280 characters, at most two sentences;
- anchored: it names a path, identifier, command, flag or `token` in the repo;
- not narration or a summary ("we implemented", "successfully", "this
  session", "overall", ...);
- not already retained from this checkout (`.feature-ladder/memory-ledger.jsonl`
  is local and git-ignored, so another clone does not see it).

Stored with `context: feature-ladder`, tags `project:<repo>`, `feature:session`
and the kind. Never secrets, tokens, credentials or personal data.

## Everything else is off

On the plugin's own server, `reflect`, knowledge pages, tag listing and bank
edits are denied to agents (another Hindsight server of the user's is left
alone outside a run):
`reflect` is a free-form summary written by the server's own model.
New banks are created with `retain_extraction_mode: verbatim` and
`enable_observations: false`. A bank made by an earlier version keeps the
server default until you run
`python3 scripts/resolve_bank.py --configure` in its repository. When
self-hosting, `hindsight/hindsight.env.example` sets the same default for the
whole server and runs Hindsight's remaining model calls on Haiku.

Check a line by hand: `python3 scripts/memory_gate.py check "[pitfall] ..."`.
