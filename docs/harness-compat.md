# Harness compatibility

What each agent surface is known to support for feature-ladder. Only the
surfaces CI checks are marked supported; everything else is `untested`, which
means nobody has run it there, not that it fails. Do not read `untested` as
"unsupported" or as "works".

| Surface | Plugin manifest | Agents | Commands | Skills | Hooks | MCP |
|---|---|---|---|---|---|---|
| Claude Code | supported (manifest tested) | supported (frontmatter tested) | supported (frontmatter tested) | supported (SKILL.md tested) | supported (hooks.json tested) | supported (.mcp.json tested) |
| Claude app | n/a (skill only) | n/a | n/a | supported (SKILL.md tested) | n/a (the app has no hooks) | n/a (a user-added connector) |
| Codex | untested | untested | untested | untested | untested | untested |
| OpenCode | untested | untested | untested | untested | untested | untested |
| Zed | untested | untested | untested | untested | untested | untested |
| dmux | untested | untested | untested | untested | untested | untested |
| Generic agent | untested | untested | untested | untested | untested | untested |

## What "tested" means

"Tested" is a static check, not a run inside the host.
`plugins/feature-ladder/tests/test_harness_manifest.py` verifies that:

- `plugin.json` and `marketplace.json` parse, carry the required keys, and the
  marketplace entry points at this plugin directory.
- `.mcp.json` parses, each server has a `url` (http) or a `command`, and the
  auth header takes its value from the environment.
- `hooks/hooks.json` parses, uses only known events, and every hook command
  names a script under `${CLAUDE_PLUGIN_ROOT}` that exists.
- Every agent and command has frontmatter, every agent's preloaded skill
  exists, and every `${CLAUDE_PLUGIN_ROOT}/...` path in the commands, agents
  and SKILL.md files exists.
- The Claude app skill has valid frontmatter. The release pin for it is
  checked separately by `test_release_pins.py`.

Whether Claude Code then loads and runs these correctly is not exercised in CI.

## Adding a surface

Run the plugin there once, then change that row's cells from `untested` to a
word that says what you did, and add a check that proves the claim. The test
`CompatMatrix` fails if a surface other than Claude Code or the Claude app
carries anything but `untested`, so the matrix cannot drift ahead of the
evidence without a deliberate edit to the test.
