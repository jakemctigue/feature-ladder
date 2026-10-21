#!/usr/bin/env python3
"""Source selection algorithm with Codex skill discovery and strict errors."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
import runtime


def main():
    args = sys.argv[1:]
    settings, rules, used = runtime.load_rules()
    inv = runtime.inventory()
    if '--list' in args:
        runtime.selector.list_all(settings, rules, used, inv)
        return 0
    inject = '--inject' in args
    if inject:
        args.remove('--inject')
    out = None
    if '--out' in args:
        i = args.index('--out')
        out = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    task = runtime.read_json(args[1]) if args[0] == '--file' else json.loads(args[0])
    result = runtime.selector.select(task, settings, rules, inv)
    missing = set(task.get('add', [])) - {s['skill'] for s in result['skills']}
    if missing:
        raise ValueError('Explicit skills unavailable or over budget: ' + ', '.join(sorted(missing)))
    body = '\n'.join(runtime.skill_text(Path(s['path']), s['skill'])[0] for s in result['skills'])
    body = body or 'No task skills selected by the configured rules.\n'
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding='utf-8')
        runtime.selector._ignore_in_feature_ladder(out)
        print(f'## Skills for this task\nRead `{out}` before work. Only use these selected skills.')
    elif inject:
        print(body)
    else:
        print(json.dumps(dict(result, rule_files=used), indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, IndexError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
