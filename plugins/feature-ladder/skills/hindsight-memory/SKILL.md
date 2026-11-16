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
`<owner>-<repo>`, `coding-agent::<project>`, `<project>` (and the ids earlier
versions used). `<owner>` is the remote's whole namespace (group and subgroup),
so two repositories never share a bank by accident. If none exists, nothing is
created yet: `coding-agent::<owner>-<repo>` is created by the first memory
write, so opening a session in a repository never leaves an empty bank
behind. Linked worktrees share the main checkout's bank. Agents never call
`list_banks` or `create_bank`.

## Reads: one scoped recall

- `recall` only. The hook forces `bank_id`, `tags: ["project:<name>"]`,
  `tags_match: "all"`, `max_tokens <= 1000`, and (unless given) `budget: low`
  and `types: ["world", "experience"]`: Hindsight's consolidated
  `observation` copies restate the stored facts and would double the result.
  `max_tokens` counts fact text only; the JSON around each fact is extra.
- The session-start line prints these exact arguments. Pass them with the
  query: a call that already matches is not rewritten, which matters in auto
  permission mode, where a call the hook rewrites has to be sent twice.
- Before the first memory is written there is no bank: recall is answered
  "no memories yet" without a network call.
- At most 3 recalls per feature run: the intake scout, the coordinator, and
  one worker whose card's `Memory:` line is `none`. More are denied.
- The query names the feature area and its files. The coordinator pastes
  only the snippets that bear on the card into `Memory:`; workers do not
  re-query.
- The coordinator saves its recall verbatim to
  `.feature-ladder/runs/<slug>/recall.json` and hands it to
  `orchestration-ladder/scripts/confidence.py --memories`, which scores each
  memory's sentiment and relevance into the confidence surface that decides
  card size (see the orchestration-ladder skill). `confidence.py --live`
  can recall on its own, through the same gate and counted the same way.

## Writes: once per session, through the gate

- No `retain` during a feature run; the hook denies it.
- Workers put facts under `Memory candidates:` as `[kind] fact`; the
  coordinator queues the ones a future task could act on with
  `session_budget.py remember`, which checks each line on the spot.
- `/feature-ladder:wrap` runs `memory_gate.py flush`: it re-checks the queue
  and writes the survivors straight to the bank over MCP. No model on our side
  reads, summarises or rewords them, and the bank is set to
  `retain_extraction_mode: verbatim` so Hindsight stores the text as given
  instead of rewriting it (its default, `concise`, rewords every fact).
- At most 12 memories per session. Lines over the cap are reported as
  `deferred` and stay in the queue for the next wrap; nothing is dropped.
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

Stored with `context: feature-ladder`, tags `project:<name>`, `feature:<slug>`
and the kind. Never secrets, tokens, credentials or personal data.

## Everything else is off

`reflect`, knowledge pages, tag listing and bank edits are denied to agents:
`reflect` is a free-form summary written by the server's own model.
New banks are created with `retain_extraction_mode: verbatim` and
`enable_observations: false`. A bank made by an earlier version keeps the
server default until you run
`python3 scripts/resolve_bank.py --configure` in its repository. When
self-hosting, `hindsight/hindsight.env.example` sets the same default for the
whole server and runs Hindsight's remaining model calls on Haiku.

Check a line by hand: `python3 scripts/memory_gate.py check "[pitfall] ..."`.
