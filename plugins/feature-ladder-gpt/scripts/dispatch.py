#!/usr/bin/env python3
"""Prepare an exact native spawn payload or launch it through Codex CLI."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
import runtime


def command(prepared, cwd, sandbox):
    spawn = prepared['spawn']
    return ['codex', '-a', 'never', 'exec', '-C', str(cwd), '--sandbox', sandbox,
            '--model', spawn['model'], '-c', f'model_reasoning_effort={json.dumps(spawn["reasoning_effort"])}',
            '-c', 'skills.include_instructions=false', '--json', '-']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'spawn', 'marker', 'exec', 'route'))
    parser.add_argument('--task', required=True)
    parser.add_argument('--cwd', type=Path, default=Path.cwd())
    parser.add_argument('--out', type=Path)
    parser.add_argument('--sandbox', choices=('read-only', 'workspace-write'), default='read-only')
    args = parser.parse_args()
    task = runtime.read_json(args.task)
    if args.action == 'route':
        result = runtime.route(task)
    else:
        prepared = runtime.prepare(task, args.cwd)
        if args.action == 'exec':
            # No shell interpolation, model fallback, permission bypass, or prompt argv.
            return subprocess.run(command(prepared, args.cwd, args.sandbox),
                                  input=prepared['spawn']['message'], text=True, encoding='utf-8',
                                  cwd=args.cwd, check=False).returncode
        if args.action == 'spawn':
            result = prepared['spawn']
        elif args.action == 'marker':
            result = dict(prepared['spawn'], message=runtime.MARKER + json.dumps(task))
        else:
            result = prepared
    text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
    else:
        print(text, end='')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Feature Ladder: {exc}', file=sys.stderr)
        sys.exit(2)
