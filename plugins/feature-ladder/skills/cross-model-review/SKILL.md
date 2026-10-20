---
name: cross-model-review
description: Cross-model second opinion for feature-ladder. OpenAI Codex (official CLI, the user's own Codex sign-in) reviews all Claude-written code before acceptance, and optionally writes and runs adversarial tests. Covers when it runs, how findings are triaged, and the guards.
---

# Cross-model review (Codex)

A different model family catches different mistakes. Before any feature is
accepted, OpenAI Codex reviews the full diff. Claude never grades its own
homework alone.

It uses only the official Codex CLI with whatever sign-in the user already has
(`codex login`: ChatGPT plan or API key). Usage counts against the user's Codex
limits. No credentials are read, copied, or passed around by this plugin.

## When it runs (deterministic)

| Trigger | Mode | Codex reasoning effort |
|---|---|---|
| `/feature-ladder:feature ... --codex`, or the confirmed intake's `codex.review` is true (any task routed to Opus, complexity score >= 10, a risk flag, novelty >= 2, or algorithmic work such as parsers, schedulers, RNG, ranking, sync, concurrency) | `review`, before acceptance | medium; high if any task routed T4/T5/ESC or had flags security, persistence, concurrency, cross_system, irreversible, public_api |
| `--adversarial`, or intake `security: true` | `review` then `adversarial` | same rule as review |
| `/feature-ladder:codex-review [--adversarial]` | on demand, any branch or uncommitted work | same rules |

Features that need none of this (small, well-trodden Sonnet/Haiku work) skip
Codex; the report says so with the intake's reason.

Effort and model can be overridden with `FEATURE_LADDER_CODEX_EFFORT` and
`FEATURE_LADDER_CODEX_MODEL`; otherwise Codex's own configured model is used.
If Codex is not installed or not signed in, the script exits 3, the report
says "second opinion skipped", and the feature proceeds.

A review usually takes a few minutes. Each Codex call is capped at
`FEATURE_LADDER_CODEX_TIMEOUT` seconds (default 570, just under Claude Code's
10-minute command limit); the driver runs it with a 600000 ms Bash timeout.

## How it is run

The coordinator dispatches `codex-reviewer` (haiku/low), which runs
`scripts/codex_review.py` and returns Codex's output verbatim. Codex runs with:

- **review**: read-only sandbox. It may read the repo and run git, nothing else.
- **adversarial**: workspace-write sandbox, told to write tests only under
  `.feature-ladder/adversarial/<slug>/` and run them with the repo's own test
  runner. The script snapshots `git status` before and after; any change outside
  that folder is reported as a `GUARD VIOLATION`.

Results are saved to `.feature-ladder/reviews/<slug>-codex-<mode>.json`.

## Triage (coordinator)

The script has already trimmed the output with no model tokens: `low` findings
are kept in the JSON record but not printed, duplicates (same file and title,
or same file and line) are collapsed, and the rest is sorted by severity. Each
finding carries a `repro` command when Codex could name one. When anything
remains, the script writes
`.feature-ladder/reviews/<slug>-codex-verify-card.md`: one Task Card that has a
single `mechanic` run every finding's repro and return one verdict line per
finding (`confirmed`, `not reproduced` or `unclear`).

For each finding, decide using the evidence, not deference to either model.
Verify first, then fix:

0. **Verify every remaining finding in one dispatch**: send the verification
   card to one `mechanic` (haiku/low), not one mechanic per finding. Confirmed
   -> fix. `not reproduced` or `unclear` on a claim about behaviour ->
   `second-opinion` (fable/low) with the code excerpt and Codex's claim
   decides. Record the checks in the report.
1. **critical / high (confirmed)**: must be resolved before acceptance. Route
   a fix Task Card through the router like any other task (the finding's
   file, line, suggested fix and the reproducing check go in the card), then
   re-run `review`.
2. **medium (confirmed)**: fix; otherwise record why not in FEATURE-REPORT.md.
3. **low**: already set aside by the script; list them in the report from the
   JSON record. No automatic fix.
4. **Disputed** (you believe Codex is wrong): send the single question to
   `second-opinion` (fable/low) with the code excerpt and Codex's claim. Its
   answer decides. Record the dispute and outcome.
5. **Adversarial tests that fail with `real_bug: true`**: group them by root
   cause first (several tests often expose one bug); route one fix per root cause; when the
   fix passes, a `mechanic` moves the test into the repo's real test suite.
   Tests that fail for test reasons are discarded. Passing tests may be kept as
   regression tests if they cover new behaviour.
6. **GUARD VIOLATION**: a `mechanic` restores the touched files
   (`git checkout -- <file>` / remove new files) before anything else.

At most two review rounds per feature; if critical/high findings remain after
the second fix round, escalate to `deep-verifier` with both models' positions.

## Memory

After triage, the coordinator's memory list includes each confirmed Codex
finding as a `pitfall` (what was wrong and the fix), and each disproved one
as a `pitfall` noting the false alarm, so future reviews of this repo improve.
