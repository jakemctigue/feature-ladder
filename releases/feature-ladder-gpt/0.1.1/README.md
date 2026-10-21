# Feature Ladder GPT 0.1.1

| Client | Download | Use |
|---|---|---|
| ChatGPT desktop | [Skill ZIP](feature-ladder-gpt-chatgpt-skill.zip?raw=true) | Upload through Plugins → Skills → Create |
| ChatGPT workspace | [Plugin ZIP](feature-ladder-gpt-chatgpt.zip?raw=true) | Upload through the workspace plugin admin console |
| Codex desktop / CLI | [Marketplace ZIP](feature-ladder-gpt-marketplace.zip?raw=true) | Extract, add the folder as a marketplace, install the plugin |
| Codex standalone skill | [Skill ZIP](feature-ladder-gpt-skill.zip?raw=true) | Extract into `.agents/skills`; keep the runtime folder |
| Codex raw plugin | [Plugin archive](feature-ladder-gpt.plugin?raw=true) | ZIP containing the plugin root |

[SHA-256 checksums](SHA256SUMS.json) accompany the five downloads.

See [Codex instructions](../../../CODEX.md) and
[ChatGPT instructions](../../../CHATGPT.md) for requirements and installation.

The ChatGPT editions require local execution and a signed-in Codex CLI for
explicit model routing. The Codex plugin includes opt-in lifecycle hooks;
standalone and ChatGPT archives contain no installed hooks. ChatGPT account
installation and authenticated agent dispatch have not been verified.

Verified locally: 39 tests (including 3,000 source-router comparisons), Codex
loader installation in an isolated profile, skill validation, extraction and
dispatch preparation for every archive, and byte-identical rebuilds.
