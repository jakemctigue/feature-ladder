"""Validate marketplace loading in a fresh, unauthenticated Codex test home."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[1]
test_home = root / '.test-work' / ('codex-' + uuid.uuid4().hex)
test_home.mkdir(parents=True)
env = dict(os.environ, CODEX_HOME=str(test_home))
commands = [
    ['codex', 'plugin', 'marketplace', 'add', str(root), '--json'],
    ['codex', 'plugin', 'add', 'feature-ladder-gpt@feature-ladder-gpt', '--json'],
    ['codex', 'plugin', 'list', '--json'],
]
for command in commands:
    result = subprocess.run(command, env=env, cwd=root, capture_output=True,
                            text=True, encoding='utf-8', timeout=60)
    print(json.dumps({'command': command[1:], 'exit_code': result.returncode,
                      'stdout': result.stdout, 'stderr': result.stderr}, indent=2))
    if result.returncode:
        sys.exit(result.returncode)
print(f'Test installation only: {test_home}')
