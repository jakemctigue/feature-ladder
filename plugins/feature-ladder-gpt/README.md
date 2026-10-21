# Feature Ladder GPT

Codex plugin, ChatGPT desktop package and portable skill adapted from Jake McTigue's
[Feature Ladder](https://github.com/jakemctigue/feature-ladder), local source
commit `a91ffc155e6bc2492c05760a2bda5890d670456f`. The router and selector blobs
are unchanged in upstream commit `5b651146771f190b3214deb77723471993bd69f7`. The Claude
package remains intact under `plugins/feature-ladder`; the GPT implementation
is under `plugins/feature-ladder-gpt`.

## Install in Codex

Download the archives in [release 0.1.1](https://github.com/jakemctigue/feature-ladder/tree/codex/feature-ladder-gpt-packages/releases/feature-ladder-gpt/0.1.1).
For ChatGPT desktop, see [CHATGPT.md](CHATGPT.md). This release is distributed
through GitHub; it has not been submitted to OpenAI's public plugin directory.

Requires Python 3.11+, Codex with explicit model selection and the configured
GPT models. Hooks must be trusted through the host's normal trust flow.
The installer never bypasses hook trust or permissions.

Install directly from the published Git branch:

```powershell
codex plugin marketplace add jakemctigue/feature-ladder --ref codex/feature-ladder-gpt-packages
codex plugin add feature-ladder-gpt@feature-ladder-gpt
```

Or from the extracted marketplace (or this source directory):

```powershell
codex plugin marketplace add .
codex plugin add feature-ladder-gpt@feature-ladder-gpt
```

Start a new Codex session, invoke `$feature-ladder-gpt`, and give it a feature
request. The `.plugin` archive contains the plugin root; the marketplace ZIP
contains the local marketplace and plugin together for the commands above.

For a standalone skill, extract `dist/feature-ladder-gpt-skill.zip` into your
user skills directory (`~/.agents/skills`) or a project's `.agents/skills`.
Keep its `runtime` folder beside SKILL.md. Standalone mode uses explicit native
spawn payloads or `codex exec` and has no installed lifecycle hooks.

The Codex loader is checked in an isolated test profile. Start a new task after
installing or updating so the client loads the new skill. Trust hooks through
the client's normal review flow; untrusted hooks do not execute.

## Model mapping

| Source | GPT model | Roles / effort |
|---|---|---|
| Haiku | `gpt-5.6-luna` | mechanic, memory-scribe, review driver: low; evidence-collector: medium |
| Sonnet | `gpt-5.6-sol` | scoper: medium; implementer: medium/high |
| Opus | `gpt-6-astra` | senior implementer and domain experts: medium/high |
| Fable | `gpt-6-astra` | second opinion: low; coordinator: medium; verifier: high; architect: xhigh; opt-in investigator: max |
| Independent review session | `gpt-5.6-sol` | original reviewer medium/high effort function |

Mapping is explicit in `routing.json`. Opus and Fable map to the same frontier
GPT model while keeping their different roles and gates. This is a task-tier
mapping, not a claim that their performance or prices are identical. There is
no silent fallback if a configured model is unavailable. The separate review
engine runs in a separate GPT session; it does not provide cross-provider independence.
These identifiers match the client model catalog used to prepare this release.
Changing that catalog requires a reviewed routing configuration update, never
an automatic fallback to the session's default model.

## Determinism and source analysis

The source already had strong deterministic primitives: a pure router,
ordered rule selection with skill/character budgets, evidence-based intake,
independent script verification and explicit role briefs. The port reuses
those algorithms and copies required protocol preloads from all 14 source
agents. The hook/dispatcher embeds full skill text before agent execution,
with source paths and SHA-256 hashes. It rejects caller model/role overrides,
missing required preloads, and missing or over-budget explicit skills.

Actual executable behavior is preserved where the README disagrees:

- `irreversible` contributes **2** points, not the README's 3.
- Hard flags are ambiguity >=2 and blast radius >=3. Cross-system plus
  security adds points; it is not an independent hard flag.
- Automatic review is only size L. Security does not automatically enable
  adversarial testing; that requires `--adversarial`.
- Mechanics have zero additional task-skill budget. “Each skill” means each
  required preload and selected skill, not every installed skill.
- Senior implementation requires an agreed design; domain analysis can route
  directly to a domain expert without an implementation design.

The port fixes one source error path: `intake.py card` now propagates selector
failure instead of quietly continuing with no skills. Native `fork_turns=none`
keeps the worker's history focused. A pre-dispatch hook overwrites model, effort,
agent type and message for marked ladder calls; unrelated calls are untouched.
The standalone dispatcher produces the same full spawn payload or sends it
via stdin to a model-pinned Codex CLI process.

## Honest enforcement boundaries

Routing is deterministic for fixed task facts, rules, inventory and source
files. Choosing estimates, design acceptance and executing model instructions
still involve judgment. Codex does not natively enforce Claude's `maxTurns`;
these remain explicit workflow budgets. Source `disallowedTools: Skill` cannot
be reproduced as a filesystem security boundary in a general Codex session:
the selected skill contents are injected, and the worker contract disallows
loading others, but tools can still read other files. Native sandbox/approval
policies govern effects. The original command classifier is a pattern guard,
not protection against arbitrary hidden behavior inside a script.

Hindsight uses the existing connected tool and its actual schema. The original
bank resolver is bundled for a direct Hindsight deployment, but this plugin
does not silently create or reconfigure a memory server. Per-repo automatic
bank rewriting is not enabled for connectors that do not expose bank_id.
Optional unavailable memory is reported. Host automatic memory hooks take
precedence over duplicate manual retain calls.

Codex ledger parsing uses cumulative counters once, separates cached input,
and reports missing telemetry as unavailable. Native hook behavior, model
access and live service integrations still require a real authenticated run;
offline tests and a plugin-loader installation are not proof of such a run.

## Inspect or test

```powershell
python plugins/feature-ladder-gpt/scripts/dispatch.py prepare --task plugins/feature-ladder-gpt/examples/bounded-task.json --cwd . --out task-audit.json
python scripts/test_codex.py
python scripts/check_plugin.py
python scripts/package_codex.py
python scripts/check_archives.py
```

`prepare` does not launch a model. `check_plugin.py` installs only into a new
unauthenticated test home under `.test-work`; it leaves your user config alone.
`test_codex.py` uses workspace-inherited temporary-directory ACLs on Windows
to support restricted-token sandboxes. The original source tests can run with
`python scripts/test_codex.py plugins/feature-ladder/tests`.

External skill discovery uses nearest project `.agents/skills`/`.codex/skills`,
then user skills, then enabled Codex plugin caches. Set
`FEATURE_LADDER_SKILL_ROOTS` to a JSON array of explicit paths for a fixed
external inventory. Personal rule overrides live under the Codex home at
`feature-ladder/skill-rules.json`; repo overrides remain
`.feature-ladder/skill-rules.json`. Optional unresolved defaults are visible in
the audit; explicitly added skills fail closed. No third-party skill content
is redistributed in these packages.

Regenerate the source-derived GPT briefs and helpers with
`python scripts/build_codex.py`. `upstream-lock.json` records source hashes.
Adapter implementation and tests remain separate from generated files.

References: [Codex plugin packaging](https://developers.openai.com/plugins/build/plugins),
[native hook schemas](https://learn.chatgpt.com/docs/hooks),
[subagent configuration](https://learn.chatgpt.com/docs/agent-configuration/subagents).
