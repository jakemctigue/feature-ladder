#!/usr/bin/env python3
"""Codex wire adapter; only marked ladder dispatches and active ladder runs."""
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runtime
import ledger


def handle(payload):
    event = payload.get('hook_event_name')
    if event == 'SessionStart':
        return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext':
                'Feature Ladder GPT dispatch hook is available. When using $feature-ladder-gpt, '
                'pass dispatch.py marker output to spawn_agent; the hook injects exact routed skills and GPT settings.'}}
    if event == 'PreToolUse':
        if payload.get('tool_name') in ('Bash', 'exec_command'):
            root, state = ledger.source.find_run(payload.get('cwd') or os.getcwd())
            if state:
                run = runtime.read_json(state)
                command = (payload.get('tool_input') or {}).get('command') or (payload.get('tool_input') or {}).get('cmd')
                if run.get('active') and command:
                    irreversible = runtime.module('_ladder_gate', runtime.ROOT / 'skills/orchestration-ladder/scripts/irreversible.py')
                    allowed, reason, changed = irreversible.gate(command, run)
                    if changed:
                        Path(state).write_text(json.dumps(run, indent=2) + '\n', encoding='utf-8')
                    if not allowed:
                        return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                                'permissionDecisionReason': reason}}
            return {}
        if payload.get('tool_name') in ('spawn_agent', 'Agent', 'collaboration.spawn_agent'):
            root, state = ledger.source.find_run(payload.get('cwd') or os.getcwd())
            run = runtime.read_json(state) if state else {}
            marked = str((payload.get('tool_input') or {}).get('message', '')).startswith(runtime.MARKER)
            if run.get('active') and run.get('codex_dispatch') and not marked:
                return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                        'permissionDecisionReason': 'Active GPT ladder run: use dispatch.py marker for every agent; un-routed spawns are blocked.'}}
            result = runtime.hook(payload)
            if run.get('active') and result.get('hookSpecificOutput', {}).get('permissionDecision') == 'allow':
                run['codex_dispatch'] = True
                Path(state).write_text(json.dumps(run, indent=2) + '\n', encoding='utf-8')
            return result
        return {}
    if event == 'SubagentStop':
        path = payload.get('agent_transcript_path')
        summary = ledger.summarize_transcript(path) if path and Path(path).is_file() else {}
        card = summary.get('first_user', '')
        match = re.search(r'Role: ([\w-]+);', card)
        if 'FEATURE LADDER GPT' not in card or not match:
            return {}
        role = match[1]
        if summary.get('measured'):
            ledger.source.record(dict(payload, agent_type=role))
        if role not in ('mechanic', 'codex-reviewer', 'memory-scribe') and not payload.get('stop_hook_active'):
            message = payload.get('last_assistant_message') or ''
            if message and not re.search(r'^Retained:\s*\S', message, re.M):
                return {'decision': 'block', 'reason': 'Feature Ladder: add the Retained footer with actual memory status. '
                        'Do not duplicate host memory hooks or fabricate a memory write.'}
    return {}


if __name__ == '__main__':
    try:
        print(json.dumps(handle(json.loads(sys.stdin.read().lstrip('\ufeff')))))
    except Exception as exc:
        # A failed enforcement hook must not silently permit a marked call.
        print(f'Feature Ladder hook error: {exc}', file=sys.stderr)
        sys.exit(2)
