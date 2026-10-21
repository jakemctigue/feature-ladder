"""Shared, deterministic routing and context construction. No model calls."""
import hashlib
import importlib.util
import json
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = 'FEATURE_LADDER_DISPATCH\n'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


router = module('_ladder_source_router', ROOT / 'upstream/route.py')
selector = module('_ladder_source_selector', ROOT / 'upstream/select_skills.py')
selector.SKILL_DIR = ROOT / 'skills/skill-selector'
selector.PLUGIN_ROOT = ROOT
CONFIG = json.loads((ROOT / 'routing.json').read_text(encoding='utf-8'))


def route(task):
    result = router.route(task)
    result['source_model'] = result['model']
    result['model'] = CONFIG['models'][result['model']]
    for old, new in CONFIG['models'].items():
        for field in ('rung', 'reason'):
            result[field] = re.sub(r'\b' + old + r'\b', new, result[field], flags=re.I)
    return result


def codex_home():
    return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def inventory(cwd=None):
    """Codex paths, nearest project first, then user, then enabled plugins.

    Explicit FEATURE_LADDER_SKILL_ROOTS (JSON array) replace external discovery,
    useful for a portable, fully pinned inventory. Bundled protocol remains.
    """
    found = {}
    cwd = Path(cwd or Path.cwd()).resolve()
    home = codex_home()
    config = tomllib.loads((home / 'config.toml').read_text(encoding='utf-8-sig')) if (home / 'config.toml').is_file() else {}
    disabled = {str(Path(c['path']).expanduser().resolve()) for c in config.get('skills', {}).get('config', [])
                if c.get('enabled') is False and c.get('path')}

    def add(root, namespace=None):
        for path in selector._skill_files(root):
            if str(path.resolve()) in disabled:
                continue
            meta, _ = selector._frontmatter(path)
            name = meta.get('name') or path.parent.name
            key = f'{namespace}:{name}' if namespace and ':' not in name else name
            found.setdefault(key, path.resolve())

    roots = os.environ.get('FEATURE_LADDER_SKILL_ROOTS')
    if roots is not None:
        parsed = json.loads(roots)
        if not isinstance(parsed, list) or any(not isinstance(p, str) for p in parsed):
            raise ValueError('FEATURE_LADDER_SKILL_ROOTS must be a JSON array of paths')
        for path in parsed:
            add(Path(path).expanduser())
    else:
        for parent in (cwd, *cwd.parents):
            for folder in ('.agents/skills', '.codex/skills'):
                add(parent / folder)
            if (parent / '.git').exists():
                break
        add(Path.home() / '.agents/skills')
        add(home / 'skills')
        for key, value in sorted(config.get('plugins', {}).items()):
            if value.get('enabled') is not True or '@' not in key:
                continue
            plugin, marketplace = key.rsplit('@', 1)
            cache = home / 'plugins/cache' / marketplace / plugin
            def version(p):
                return tuple(int(n) for n in re.findall(r'\d+', p.name)), p.name
            versions = sorted(cache.iterdir(), key=version, reverse=True) if cache.is_dir() else []
            for directory in versions:
                manifest = directory / '.codex-plugin/plugin.json'
                if not manifest.is_file():
                    continue
                data = read_json(manifest)
                skill_root = directory / data.get('skills', './skills')
                add(skill_root, data.get('name', plugin))
                break
    add(ROOT / 'skills', 'feature-ladder-gpt')
    # Original rules use these identifiers for bundled protocol skills.
    for name, path in list(found.items()):
        if name.startswith('feature-ladder-gpt:'):
            found.setdefault(name.replace('feature-ladder-gpt:', 'feature-ladder:'), path)
    return found


def load_rules(cwd=None):
    cwd = Path(cwd or Path.cwd()).resolve()
    project = None
    for parent in (cwd, *cwd.parents):
        candidate = parent / '.feature-ladder/skill-rules.json'
        if candidate.is_file():
            project = candidate
            break
        if (parent / '.git').exists():
            break
    files = [ROOT / 'skills/skill-selector/skill-rules.json',
             codex_home() / 'feature-ladder/skill-rules.json', project]
    settings = {'max_skills': 3, 'max_chars': 20000, 'max_chars_by_role': {}}
    rules, used = [], []
    for path in files:
        if path is None or not path.is_file():
            continue
        data = read_json(path)
        used.append(str(path))
        for key in ('max_skills', 'max_chars'):
            if key in data:
                settings[key] = int(data[key])
        settings['max_chars_by_role'].update(data.get('max_chars_by_role', {}))
        rules = [r for r in rules if r.get('id') not in data.get('disable', [])]
        for rule in data.get('rules', []):
            replacement = dict(rule, _source=str(path))
            index = next((i for i, r in enumerate(rules) if r.get('id') and r['id'] == rule.get('id')), None)
            if index is None:
                rules.append(replacement)
            else:
                rules[index] = replacement
    return settings, rules, used


def selection(task, cwd=None):
    settings, rules, used = load_rules(cwd)
    result = selector.select(task, settings, rules, inventory(cwd))
    result['rule_files'] = used
    return result


def resolve_role(task):
    phase = task.get('phase', 'work')
    routed = route(task)
    if phase == 'work':
        role, effort = routed['agent'], routed['effort']
        if role == 'escalate':
            role = 'coordinator'
    elif phase == 'retry':
        if task.get('failures') != 2 or not str(task.get('justification', '')).strip():
            raise ValueError('One-band retry requires failures=2 and a written justification')
        bands = router.BANDS
        if routed['agent'] == 'escalate' or task.get('kind', 'code') != 'code':
            raise ValueError('Escalated or non-code tasks require coordinator review, not a score-band retry')
        index = next(i for i, (low, high, *_) in enumerate(bands) if low <= routed['score'] <= high)
        _, _, role, source_model, effort, _ = bands[index + 1]
        if role == 'escalate' or (source_model == 'opus' and not router.clean(task).get('design_agreed')):
            role, effort = 'coordinator', 'medium'
    elif phase in ('scope', 'coordinate', 'design', 'opinion', 'review', 'memory'):
        role = {'scope': 'scoper', 'coordinate': 'coordinator',
                'design': 'domain-expert-high' if router.clean(task).get('novelty', 0) >= 3 else 'domain-expert',
                'opinion': 'second-opinion', 'review': 'codex-reviewer', 'memory': 'memory-scribe'}[phase]
        effort = CONFIG['roles'][role]['effort']
    elif phase == 'escalate':
        previous = task.get('previous_role')
        role = {'coordinator': 'deep-verifier', 'deep-verifier': 'architect-escalation',
                'architect-escalation': 'ultra-investigator'}.get(previous)
        if not role or not str(task.get('justification', '')).strip():
            raise ValueError('Escalation requires a previous role and written justification; no rung skipping')
        if role == 'ultra-investigator' and task.get('ultra') is not True:
            raise ValueError('ultra-investigator requires explicit ultra=true opt-in')
        effort = CONFIG['roles'][role]['effort']
    else:
        raise ValueError(f'Unknown phase: {phase}')
    if any(k in task for k in ('model', 'effort', 'agent', 'role')):
        raise ValueError('Task facts cannot override model, effort, agent or role; use phase and router fields')
    details = dict(CONFIG['roles'][role], role=role, effort=effort, route=routed)
    return details


def skill_text(path, name):
    text = path.read_text(encoding='utf-8-sig')
    if not text.strip():
        raise ValueError(f'Empty required skill: {name}')
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return (f'\n## Loaded skill: {name}\nSource: {path}\nBase directory: {path.parent}\n'
            f'SHA256: {digest}\n\n{text}\n'), {'name': name, 'path': str(path), 'sha256': digest}


def prepare(task, cwd=None):
    if not isinstance(task, dict):
        raise ValueError('Task must be a JSON object')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(task.get('id', ''))):
        raise ValueError('Task id must have 1-80 letters, digits, underscores or hyphens')
    if not str(task.get('text', '')).strip():
        raise ValueError('Task text is required')
    details = resolve_role(task)
    role = details['role']
    normalized = router.clean(task)
    sel_task = dict(task, role=role, flags={k: normalized.get(k, False) for k in router.POINTS if k not in router.NUMERIC})
    selected = selection(sel_task, cwd)
    requested = set(task.get('add', []))
    missing = requested - {s['skill'] for s in selected['skills']}
    if missing:
        raise ValueError('Explicit skills missing, excluded or over budget: ' + ', '.join(sorted(missing)))
    _, brief = selector._frontmatter(ROOT / 'agents' / f'{role}.md')
    # High expert extends the base brief in the source: include both deterministically.
    if role == 'domain-expert-high':
        _, base = selector._frontmatter(ROOT / 'agents/domain-expert.md')
        brief = base + '\n' + brief
    preamble = (f'FEATURE LADDER GPT — TASK {task["id"]}:\n'
                f'Role: {role}; model: {details["model"]}; effort: {details["effort"]}.\n'
                f'Rung: {role} ({details["model"]}/{details["effort"]})\n'
                f'Plugin root: {ROOT}\n'
                'Apply the Codex adapter below to the translated source briefs. '
                'The complete required skill texts are already in this message. '
                'Do not invoke additional skills; return blocked naming a missing skill. '
                'You are not alone in the codebase: preserve others\' edits and stay within the task Scope.\n')
    adapter = (ROOT / 'skills/feature-ladder-gpt/references/codex-adapter.md').read_text(encoding='utf-8')
    message = preamble + '\n' + brief + '\n' + adapter + '\n## Task facts and instructions\n' + json.dumps(task, indent=2)
    loaded, seen = [], set()
    paths = [(f'feature-ladder-gpt:{name}', ROOT / 'skills' / name / 'SKILL.md') for name in details['preload']]
    paths += [(s['skill'], Path(s['path'])) for s in selected['skills']]
    for name, path in paths:
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        body, evidence = skill_text(path, name)
        message += body
        loaded.append(evidence)
    message = message.replace('${PLUGIN_ROOT}', str(ROOT))
    spawn = {'task_name': f'ladder_{task["id"]}', 'agent_type': 'default', 'fork_turns': 'none',
             'model': details['model'], 'reasoning_effort': details['effort'], 'message': message}
    return {'route': details, 'selection': selected, 'loaded_skills': loaded,
            'prompt_sha256': hashlib.sha256(message.encode('utf-8')).hexdigest(), 'spawn': spawn}


def hook(payload):
    if payload.get('tool_name') not in ('spawn_agent', 'Agent', 'collaboration.spawn_agent'):
        return {}
    args = payload.get('tool_input') or {}
    message = args.get('message', '')
    if not isinstance(message, str) or not message.startswith(MARKER):
        return {}
    try:
        prepared = prepare(json.loads(message[len(MARKER):]), payload.get('cwd'))
        # Replace the entire call, so fixed-model custom roles cannot override the route.
        updated = prepared['spawn']
        if 'task_name' not in args:
            updated.pop('task_name')
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow', 'updatedInput': updated}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                'permissionDecisionReason': f'Feature Ladder dispatch blocked: {exc}'}}
