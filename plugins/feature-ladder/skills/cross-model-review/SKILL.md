---
name: cross-model-review
description: Cross-model second opinion for feature-ladder. OpenAI Codex (official CLI, the user's own Codex sign-in) reviews a whole session's Claude-written code once, at /feature-ladder:wrap --review, and optionally writes and runs adversarial tests. Never per feature. Covers when it runs, how findings are triaged, and the guards.
---

# Cross-model review (Codex)

A different model family catches different mistakes. Once a session's
features are built, OpenAI Codex reviews the combined diff in one pass, when
the user schedules it. Individual features are accepted on `verify.py`.

It uses only the official Codex CLI with whatever sign-in the user already has
(`codex login`: ChatGPT plan or API key). Usage counts against the user's Codex
limits. No credentials are read, copied, or passed around by this plugin.

## When it runs (deterministic)

| Trigger | Mode | Codex reasoning effort |
|---|---|---|
| `/feature-ladder:wrap --review`, once at the end of a session (many features) | `review`, once, over the whole session's diff | medium; high if the diff touches security, persistence, concurrency or public API code |
| `/feature-ladder:wrap --adversarial` | `review` then `adversarial`, once each | same rule as review |
| `/feature-ladder:codex-review [--adversarial]` | on demand, any branch or uncommitted work | same rules |

Never during a feature run: the spawn hook denies `codex-reviewer` while a
run is active. The intake's `codex.at_wrap` lists reasons (Opus-tier, novel or
algorithmic work) to schedule `--review` at the end of the session. **One attempt per mode.** A
timeout (exit 1 after `FEATURE_LADDER_CODEX_TIMEOUT` seconds) or exit 3 is
recorded as "skipped" and the run continues; no retries, no background runs.

Effort and model can be overridden with `FEATURE_LADDER_CODEX_EFFORT` and
`FEATURE_LADDER_CODEX_MODEL`; otherwise Codex's own configured model is used.
If Codex is not installed or not signed in, the script exits 3, the report
says "second opinion skipped", and the feature proceeds.

A review usually takes a few minutes. Each Codex call is capped at
`FEATURE_LADDER_CODEX_TIMEOUT` seconds (default 570, just under Claude Code's
10-minute command limit); the driver runs it with a 600000 ms Bash timeout.

## How it is run

`/feature-ladder:wrap` dispatches `codex-reviewer` (haiku/low), which runs
`scripts/codex_review.py` and returns Codex's output verbatim. Codex runs with:

- **review**: read-only sandbox. It may read the repo and run git, nothing else.
- **adversarial**: workspace-write sandbox, told to write tests only under
  `.feature-ladder/adversarial/<slug>/` and run them with the repo's own test
  runner. The script snapshots `git status` before and after; any change outside
  that folder is reported as a `GUARD VIOLATION`.

Results are saved to `.feature-ladder/reviews/<slug>-codex-<mode>.json`.

## Triage (at wrap)

The script has already trimmed the output with no model tokens: `low` findings
are kept in the JSON record but not printed, duplicates (same file and title,
or same file and line) are collapsed, and the rest is sorted by severity. Each
finding carries a `repro` command when Codex could name one. When anything
remains, the script writes
`.feature-ladder/reviews/<slug>-codex-verify-card.md` listing every finding's
repro command, for the user (or the next feature's worker) to run.

At `/feature-ladder:wrap` the findings are reported, not fixed:

- **critical / high / medium**: listed with file, line, the suggested fix and
  the repro command; each confirmed bug becomes its own
  `/feature-ladder:feature` request that the user starts (one cheap worker,
  the repro as its "Done when").
- **low**: already set aside by the script; listed from the JSON record.
- **Disputed**: one `second-opinion` (fable/low) question only with
  `--second-opinion`; otherwise marked "unverified".
- **Adversarial tests that fail with `real_bug: true`**: grouped by root
  cause and reported the same way.
- **GUARD VIOLATION**: restore the touched files (`git checkout -- <file>`,
  remove new files) before anything else.

One review per wrap. No fix rounds, no re-review, no mechanic per finding.

## Memory

A confirmed finding that a future change in this repo could repeat goes in
the session queue as `[pitfall] <what was wrong> in <path>; <the fix>`, which
passes the memory gate. False alarms and the review itself are not stored.
