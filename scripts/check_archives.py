"""Verify packaged standalone and marketplace payloads independently of source."""
import hashlib
import json
import os
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
test_root = ROOT / '.test-work' / ('archives-' + uuid.uuid4().hex)
test_root.mkdir(parents=True)
hashes = json.loads((ROOT / 'dist/SHA256SUMS.json').read_text(encoding='utf-8'))
for name, prefix in [('feature-ladder-gpt-skill.zip', 'feature-ladder-gpt/runtime'),
                     ('feature-ladder-gpt-chatgpt-skill.zip', 'feature-ladder-gpt/runtime'),
                     ('feature-ladder-gpt.plugin', '.'),
                     ('feature-ladder-gpt-chatgpt.zip', '.'),
                     ('feature-ladder-gpt-marketplace.zip', 'plugins/feature-ladder-gpt')]:
    dest = test_root / name.removesuffix('.zip')
    dest.mkdir()
    assert hashlib.sha256((ROOT / 'dist' / name).read_bytes()).hexdigest() == hashes[name]
    with zipfile.ZipFile(ROOT / 'dist' / name) as archive:
        assert archive.testzip() is None
        for item in archive.infolist():
            assert (dest / item.filename).resolve().is_relative_to(dest.resolve())
            assert not any(p in Path(item.filename).parts for p in ('__pycache__', '.test-work', 'tests'))
            assert not item.filename.endswith(('task-audit.json', '.pyc', '.env'))
        archive.extractall(dest)
    runtime = dest / prefix
    manifest = json.loads((runtime / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
    portable = json.loads((runtime / 'plugin.json').read_text(encoding='utf-8'))
    assert portable['name'] == manifest['name'] == 'feature-ladder-gpt'
    assert portable['version'] == manifest['version'] == '0.1.1'
    assert portable['extensions']['com.openai']['requires_local_executor'] is True
    assert 'hooks' not in manifest
    if 'chatgpt' in name or '-skill.zip' in name:
        assert not (runtime / 'hooks').exists()
    else:
        assert (runtime / 'hooks/hooks.json').is_file()
    out = dest / 'audit.json'
    result = subprocess.run([sys.executable, str(runtime / 'scripts/dispatch.py'), 'prepare',
                             '--task', str(runtime / 'examples/bounded-task.json'),
                             '--cwd', str(dest), '--out', str(out)], cwd=dest,
                            env=dict(os.environ, FEATURE_LADDER_SKILL_ROOTS='[]'),
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['spawn']['model'] == 'gpt-5.6-sol'
    assert all(Path(s['path']).is_relative_to(runtime.resolve()) for s in data['loaded_skills'])
    print(f'{name}: archive integrity and self-contained dispatch passed')

subprocess.run([sys.executable, str(ROOT / 'scripts/package_codex.py')], check=True, capture_output=True)
after = json.loads((ROOT / 'dist/SHA256SUMS.json').read_text(encoding='utf-8'))
assert hashes == after, 'Rebuilding identical sources must yield identical archive bytes'
print('Reproducible build: identical SHA-256 for all five archives')
