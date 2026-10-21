"""Reproduce the Codex port from the pinned, unmodified Claude scaffolding."""
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'plugins/feature-ladder'
TARGET = ROOT / 'plugins/feature-ladder-gpt'
MODELS = {'haiku': 'gpt-5.6-luna', 'sonnet': 'gpt-5.6-sol',
          'opus': 'gpt-6-astra', 'fable': 'gpt-6-astra'}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def adapt(text):
    text = text.replace('${CLAUDE_PLUGIN_ROOT}', '${PLUGIN_ROOT}')
    text = text.replace('hooks/scripts/ledger.py', 'scripts/ledger.py')
    text = text.replace('`Agent(subagent_type="feature-ladder:<agent>", model="<model>", effort="<effort>", prompt="<Task Card>")`',
                        '`spawn_agent` with the exact output of `scripts/dispatch.py marker --task <task.json>`')
    text = text.replace('feature-ladder:', 'feature-ladder-gpt:')
    text = text.replace('~/.claude/', '~/.codex/')
    text = re.sub(r'\b(Haiku|Sonnet|Opus|Fable)(?: 5\.[15])?\b',
                  lambda m: MODELS[m[1].lower()], text)
    text = re.sub(r'\b(haiku|sonnet|opus|fable)\b', lambda m: MODELS[m[1]], text)
    text = text.replace('Claude Code', 'Codex').replace('Claude-written', 'GPT-written')
    text = text.replace('A different model family catches different mistakes. Before any feature is\naccepted, OpenAI Codex reviews the full diff. Claude never grades its own\nhomework alone.',
                        'A separate Codex session reviews the diff when the triggers below apply.\nThis GPT port provides independent context, not cross-provider model diversity.')
    text = text.replace('Effort and model can be overridden with `FEATURE_LADDER_CODEX_EFFORT` and\n`FEATURE_LADDER_CODEX_MODEL`; otherwise Codex\'s own configured model is used.',
                        'The model is pinned in `routing.json`; effort follows the source rules.\nCaller model and effort overrides are rejected.')
    return text


def main():
    hashes = {}
    for path in sorted(SOURCE.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        rel = path.relative_to(SOURCE)
        hashes[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if rel.parts[0] not in ('skills', 'agents'):
            continue
        target = TARGET / rel
        if path.suffix == '.md':
            write(target, adapt(path.read_text(encoding='utf-8')))
        elif path.name in ('route.py', 'select_skills.py'):
            # The port wraps these functions; the original algorithms stay intact.
            write(TARGET / 'upstream' / path.name, path.read_text(encoding='utf-8'))
        elif path.name == 'intake.py':
            text = path.read_text(encoding='utf-8')
            old = '''            sys.stdout.write(res.stdout if res.returncode == 0 else
                             "## Skills for this task\\nNone selected (selector error: " + res.stderr.strip()[-200:] + ")\\n")
            return 0'''
            new = '''            if res.returncode != 0:
                sys.stderr.write("Skill selection failed: " + res.stderr)
                return 2
            sys.stdout.write(res.stdout)
            return 0'''
            if old not in text:
                raise ValueError('Upstream intake card changed; review adapter')
            write(target, adapt(text.replace(old, new)))
        elif path.name == 'codex_review.py':
            text = path.read_text(encoding='utf-8')
            text = text.replace('model = os.environ.get("FEATURE_LADDER_CODEX_MODEL")',
                                'model = json.loads((Path(__file__).resolve().parents[3] / "routing.json").read_text(encoding="utf-8"))["review_engine"]["model"]')
            text = text.replace('    env = os.environ.get("FEATURE_LADDER_CODEX_EFFORT")\n    if env:\n        return env\n', '')
            text = text.replace('    effort = opt("--effort") or effort_for(mode, tier, flags)',
                                '    if "--effort" in argv:\n        print("Review effort is selected by the ladder, not a CLI override", file=sys.stderr)\n        return 2\n    effort = effort_for(mode, tier, flags)')
            text = text.replace('Model: Codex\'s own configured default, or FEATURE_LADDER_CODEX_MODEL.',
                                'Model: pinned review_engine.model in routing.json.')
            text = text.replace('Deterministic reasoning effort (unless --effort or FEATURE_LADDER_CODEX_EFFORT):',
                                'Deterministic reasoning effort (caller overrides are rejected):')
            write(target, text)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    roles = {}
    for path in sorted((SOURCE / 'agents').glob('*.md')):
        text = path.read_text(encoding='utf-8')
        front, body = text.split('---', 2)[1:]
        model = re.search(r'^model: (\S+)', front, re.M)[1]
        effort = re.search(r'^effort: (\S+)', front, re.M)[1]
        skills = re.search(r'^skills:\n((?:  - .+\n)+)', front, re.M)
        roles[path.stem] = {'model': MODELS[model], 'effort': effort,
                            'source_model': model,
                            'max_turns': int(re.search(r'^maxTurns: (\d+)', front, re.M)[1]),
                            'preload': re.findall(r'feature-ladder:([\w-]+)', skills[1]) if skills else []}
    write(TARGET / 'routing.json', json.dumps({'models': MODELS, 'roles': roles,
                                              'review_engine': {'model': 'gpt-5.6-sol'}}, indent=2) + '\n')
    write(TARGET / 'upstream-lock.json', json.dumps({
        'repository': 'https://github.com/jakemctigue/feature-ladder',
        'commit': 'a91ffc155e6bc2492c05760a2bda5890d670456f',
        'files': hashes}, indent=2) + '\n')
    shutil.copy2(ROOT / 'LICENSE', TARGET / 'LICENSE')
    for name in ('test_verify.py', 'test_codex_triage.py'):
        write(TARGET / 'tests' / name, (SOURCE / 'tests' / name).read_text(encoding='utf-8'))
    # Copy executable helpers, not Claude-specific hook configuration or transcripts.
    for name in ('guard_irreversible.py', 'require_retained.py', 'ledger.py'):
        write(TARGET / 'upstream' / name,
              (SOURCE / 'hooks/scripts' / name).read_text(encoding='utf-8'))
    manifest = {
        'name': 'feature-ladder-gpt', 'version': '0.1.1',
        'description': 'Feature Ladder with deterministic GPT routing and skill injection.',
        'author': {'name': 'Jake McTigue'}, 'license': 'MIT',
        'repository': 'https://github.com/jakemctigue/feature-ladder',
        'homepage': 'https://github.com/jakemctigue/feature-ladder',
        'skills': './skills/',
        'interface': {'displayName': 'Feature Ladder GPT',
                      'shortDescription': 'Deterministic GPT agents and task skills',
                      'longDescription': 'Preserves Feature Ladder routing, role briefs and skill budgets. Requires local Python and a coding runtime with explicit GPT model selection.',
                      'developerName': 'Jake McTigue',
                      'category': 'Developer Tools',
                      'capabilities': ['Read', 'Write', 'Interactive'],
                      'defaultPrompt': ['Use $feature-ladder-gpt to implement this feature.']}}
    write(TARGET / '.codex-plugin/plugin.json', json.dumps(manifest, indent=2) + '\n')
    portable = {key: value for key, value in manifest.items() if key not in ('skills', 'interface')}
    portable['$schema'] = 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'
    portable['extensions'] = {'com.openai': {'requires_local_executor': True, 'interface': manifest['interface']}}
    write(TARGET / 'plugin.json', json.dumps(portable, indent=2) + '\n')
    marketplace = ROOT / '.agents/plugins/marketplace.json'
    if not marketplace.exists():
        write(marketplace, json.dumps({
        'name': 'feature-ladder-gpt', 'owner': {'name': 'Jake McTigue'},
        'interface': {'displayName': 'Feature Ladder GPT'},
        'plugins': [{'name': 'feature-ladder-gpt', 'source': {'source': 'local', 'path': './plugins/feature-ladder-gpt'},
                     'category': 'Developer Tools',
                     'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'}}]}, indent=2) + '\n')
    print(f'Generated {len(roles)} GPT role briefs and preserved upstream helpers.')


if __name__ == '__main__':
    main()
