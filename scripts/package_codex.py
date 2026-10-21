"""Build reproducible Codex and ChatGPT distributions without local task data."""
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins/feature-ladder-gpt'
OUT = ROOT / 'dist'


def files(chatgpt=False):
    result = {}
    for path in sorted(PLUGIN.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(PLUGIN)
        if any(p in ('__pycache__', 'tests') for p in rel.parts) or path.suffix == '.pyc':
            continue
        if chatgpt and rel.parts[0] == 'hooks':
            continue
        result[rel.as_posix()] = path.read_bytes()
    return result


def archive(name, contents):
    with zipfile.ZipFile(OUT / name, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, data in sorted(contents.items()):
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data, compresslevel=9)


def standalone(contents, chatgpt=False):
    prefix = 'feature-ladder-gpt/'
    entry = contents['skills/feature-ladder-gpt/SKILL.md'].decode('utf-8')
    entry = entry.replace('The plugin root is two directories above this SKILL.md.',
                          'The plugin root is the `runtime` directory beside this SKILL.md.')
    entry = entry.replace('With trusted plugin hooks, run',
                          'This standalone install has no hooks. Use `spawn` or `exec` below. Only with a separate trusted Codex plugin installation, run')
    if chatgpt:
        entry += '\nThis ChatGPT package must use the `exec` transport in references/chatgpt-desktop.md. It installs no hooks.\n'
    result = {prefix + 'SKILL.md': entry.encode('utf-8')}
    for rel in ('agents/openai.yaml', 'references/codex-adapter.md', 'references/chatgpt-desktop.md'):
        result[prefix + rel] = contents['skills/feature-ladder-gpt/' + rel]
    for rel, data in contents.items():
        if not rel.startswith('hooks/'):
            result[prefix + 'runtime/' + rel] = data
    return result


def main():
    OUT.mkdir(exist_ok=True)
    for source, dest in [('CODEX.md', 'README.md'), ('CHATGPT.md', 'CHATGPT.md')]:
        data = (ROOT / source).read_text(encoding='utf-8')
        if source == 'CHATGPT.md':
            data = data.replace('(CODEX.md)', '(README.md)')
        (PLUGIN / dest).write_text(data, encoding='utf-8', newline='\n')
    codex, chatgpt = files(), files(chatgpt=True)
    packages = {
        'feature-ladder-gpt.plugin': codex,
        'feature-ladder-gpt-marketplace.zip': {
            **{'plugins/feature-ladder-gpt/' + p: data for p, data in codex.items()},
            '.agents/plugins/marketplace.json': (ROOT / '.agents/plugins/marketplace.json').read_bytes(),
            'CODEX.md': (ROOT / 'CODEX.md').read_bytes(),
            'CHATGPT.md': (ROOT / 'CHATGPT.md').read_bytes(),
        },
        'feature-ladder-gpt-skill.zip': standalone(codex),
        'feature-ladder-gpt-chatgpt.zip': chatgpt,
        'feature-ladder-gpt-chatgpt-skill.zip': standalone(chatgpt, chatgpt=True),
    }
    for name, contents in packages.items():
        archive(name, contents)
    hashes = {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest()
              for name in sorted(packages)}
    (OUT / 'SHA256SUMS.json').write_text(json.dumps(hashes, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(hashes, indent=2))


if __name__ == '__main__':
    main()
