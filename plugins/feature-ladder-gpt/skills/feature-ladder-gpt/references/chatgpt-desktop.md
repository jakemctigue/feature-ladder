# ChatGPT desktop execution

Apply these transport rules when this skill is invoked in ChatGPT desktop.
Keep the entry flow and the original role, skill, scope and verification rules.

1. Confirm local command and repository access. Check `python --version` and
   `codex --version` without changing global configuration. Python 3.11+ and
   authenticated access to the models in `<root>/routing.json` are required.
2. Prepare each structured task with the bundled `scripts/dispatch.py prepare`
   command. This calculates its route and full skill payload without calling a
   model. Treat its output as private task context.
3. Use `python <root>/scripts/dispatch.py exec --task <task.json> --cwd <repo>`
   for each dispatch. For an authorized coding task, append
   `--sandbox workspace-write`; read-only is the default. This skill explicitly
   requests delegated coding sessions through this launcher.
4. Do not use `marker` in ChatGPT: this package installs no hooks. Do not pass
   Codex-native `spawn` arguments into a different subagent API. The `exec`
   transport sends the complete context through stdin and pins model and effort
   as process arguments. Never substitute the current chat model.
5. Review the process output, record the actual result, and run the independent
   verification commands from the original workflow. Any further delegation
   must use the same dispatcher. Model availability failures stop the task.

If this ChatGPT surface cannot run local commands, report the missing execution
capability and provide the prepared task for a capable local Codex session.
Do not claim that an agent ran, loaded its skills, or changed files.
