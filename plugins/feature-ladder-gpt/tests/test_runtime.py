import contextlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime
import dispatch
import ledger
hook = runtime.module('_test_hook', ROOT / 'hooks/dispatch_hook.py')


class Routing(unittest.TestCase):
    def test_parity_with_source_over_boundary_cases_and_random_tasks(self):
        rng = random.Random(13)
        tasks = []
        for _ in range(3000):
            tasks.append(dict(kind=rng.choice(['code', 'docs', 'fixture', 'check', 'research', 'analysis']),
                files=rng.choice([0, 1, 2, 3, 4, 6, 7, 15]), loc=rng.choice([0, 30, 31, 120, 121, 300, 301]),
                modules=rng.choice([0, 1, 2, 3]), ambiguity=rng.randrange(4), novelty=rng.randrange(4),
                blast_radius=rng.randrange(4), design_agreed=rng.choice([True, False]),
                **{k: rng.choice([True, False]) for k in runtime.router.POINTS if k not in runtime.router.NUMERIC}))
        for task in tasks:
            source, gpt = runtime.router.route(task), runtime.route(task)
            for key in ('agent', 'effort', 'score', 'hard_flags', 'breakdown', 'size'):
                self.assertEqual(source[key], gpt[key], (task, key))
            self.assertEqual(gpt['model'], runtime.CONFIG['models'][source['model']])

    def test_roles_match_all_fourteen_source_definitions(self):
        self.assertEqual(len(runtime.CONFIG['roles']), 14)
        for role, config in runtime.CONFIG['roles'].items():
            front, _ = runtime.selector._frontmatter(ROOT.parents[0] / 'feature-ladder/agents' / (role + '.md'))
            self.assertEqual(config['model'], runtime.CONFIG['models'][front['model']])
            self.assertEqual(config['effort'], front['effort'])

    def test_hard_flags_and_design_gate(self):
        for task in ({'ambiguity': 2}, {'blast_radius': 3},
                     {'files': 8, 'loc': 400, 'modules': 3, 'novelty': 3, 'security': True,
                      'persistence': True, 'concurrency': True, 'cross_system': True}):
            self.assertEqual(runtime.resolve_role(dict(task, phase='work'))['role'], 'coordinator')

    def test_escalations_require_evidence_and_opt_in(self):
        with self.assertRaises(ValueError):
            runtime.resolve_role({'phase': 'escalate', 'previous_role': 'mechanic', 'justification': 'hard'})
        with self.assertRaises(ValueError):
            runtime.resolve_role({'phase': 'escalate', 'previous_role': 'deep-verifier'})
        with self.assertRaises(ValueError):
            runtime.resolve_role({'phase': 'escalate', 'previous_role': 'architect-escalation', 'justification': 'still unknown'})
        result = runtime.resolve_role({'phase': 'escalate', 'previous_role': 'architect-escalation',
                                       'justification': 'experiment needed', 'ultra': True})
        self.assertEqual((result['role'], result['effort']), ('ultra-investigator', 'max'))

    def test_retry_climbs_one_band_and_preserves_design_gate(self):
        result = runtime.resolve_role({'phase': 'retry', 'failures': 2, 'justification': 'repro still fails'})
        self.assertEqual((result['role'], result['effort']), ('implementer', 'medium'))
        with self.assertRaises(ValueError):
            runtime.resolve_role({'phase': 'retry', 'failures': 1})

    def test_model_overrides_rejected(self):
        for key in ('model', 'effort', 'agent', 'role'):
            with self.assertRaises(ValueError):
                runtime.resolve_role({key: 'something'})


class Context(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.skill = self.repo / 'skills/demo'
        self.skill.mkdir(parents=True)
        self.body = '---\nname: demo\ndescription: Demo workflow.\n---\nFULL CONTENT: run the explicit regression check.\n'
        (self.skill / 'SKILL.md').write_text(self.body, encoding='utf-8')
        rules = self.repo / '.feature-ladder/skill-rules.json'
        rules.parent.mkdir()
        rules.write_text(json.dumps({'rules': [{'id': 'demo', 'skill': 'demo', 'priority': 100,
                                               'when': {'keywords': ['demo']}}]}), encoding='utf-8')
        self.env = patch.dict(os.environ, {'FEATURE_LADDER_SKILL_ROOTS': json.dumps([str(self.repo / 'skills')])})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.task = {'id': 'T1', 'text': 'Implement demo', 'files': 4, 'loc': 121,
                     'paths': ['src/demo.py'], 'checks': ['python -m unittest']}

    def test_complete_protocol_and_task_skill_loaded_before_spawn(self):
        first = runtime.prepare(self.task, self.repo)
        second = runtime.prepare(self.task, self.repo)
        self.assertEqual(first, second)
        self.assertIn(self.body, first['spawn']['message'])
        self.assertIn('feature-ladder-gpt:worker-contract', [s['name'] for s in first['loaded_skills']])
        self.assertIn('demo', [s['name'] for s in first['loaded_skills']])
        self.assertEqual(first['spawn']['model'], 'gpt-5.6-sol')
        self.assertEqual(first['spawn']['fork_turns'], 'none')
        self.assertNotIn('${PLUGIN_ROOT}', first['spawn']['message'])

    def test_preload_missing_fails_closed(self):
        original = runtime.CONFIG['roles']['implementer']['preload']
        with patch.dict(runtime.CONFIG['roles']['implementer'], preload=original + ['does-not-exist']):
            with self.assertRaises(FileNotFoundError):
                runtime.prepare(self.task, self.repo)

    def test_missing_explicit_skill_or_budget_overflow_blocks(self):
        with self.assertRaisesRegex(ValueError, 'Explicit skills'):
            runtime.prepare(dict(self.task, add=['unknown']), self.repo)
        with self.assertRaisesRegex(ValueError, 'over budget'):
            runtime.prepare(dict(self.task, files=1, loc=1, add=['demo']), self.repo)

    def test_mechanic_has_no_extra_task_skills(self):
        out = runtime.prepare(dict(self.task, files=1, loc=1), self.repo)
        self.assertEqual(out['selection']['skills'], [])
        self.assertNotIn('FULL CONTENT', out['spawn']['message'])

    def test_high_expert_receives_base_procedure(self):
        out = runtime.prepare(dict(self.task, phase='design', novelty=3), self.repo)
        self.assertIn('Options considered', out['spawn']['message'])
        self.assertIn('Failure analysis', out['spawn']['message'])

    def test_hook_overwrites_wrong_model_role_and_full_history(self):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'spawn_agent', 'cwd': str(self.repo),
                   'tool_input': {'task_name': 'bad', 'model': 'wrong', 'reasoning_effort': 'low',
                                  'agent_type': 'code-reviewer', 'fork_turns': 'all',
                                  'message': runtime.MARKER + json.dumps(self.task)}}
        out = hook.handle(payload)['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'allow')
        spawn = out['updatedInput']
        self.assertEqual(spawn, runtime.prepare(self.task, self.repo)['spawn'])
        self.assertIn(self.body, spawn['message'])

    def test_hook_does_not_change_unrelated_agents(self):
        self.assertEqual(runtime.hook({'tool_name': 'spawn_agent', 'tool_input': {'message': 'normal task'}}), {})

    def test_active_run_rejects_unrouted_spawns_then_releases_on_finish(self):
        state = self.repo / '.feature-ladder/run.json'
        state.write_text(json.dumps({'active': True}), encoding='utf-8')
        event = {'hook_event_name': 'PreToolUse', 'tool_name': 'spawn_agent', 'cwd': str(self.repo),
                 'tool_input': {'message': runtime.MARKER + json.dumps(self.task)}}
        self.assertEqual(hook.handle(event)['hookSpecificOutput']['permissionDecision'], 'allow')
        self.assertTrue(runtime.read_json(state)['codex_dispatch'])
        event['tool_input']['message'] = 'skip routing please'
        self.assertEqual(hook.handle(event)['hookSpecificOutput']['permissionDecision'], 'deny')
        state.write_text(json.dumps({'active': False, 'codex_dispatch': True}), encoding='utf-8')
        self.assertEqual(hook.handle(event), {})

    def test_guard_normalizes_codex_shell_arguments(self):
        state = self.repo / '.feature-ladder/run.json'
        state.write_text(json.dumps({'active': True, 'outside_policy': 'all', 'base': 'main'}), encoding='utf-8')
        result = hook.handle({'hook_event_name': 'PreToolUse', 'tool_name': 'exec_command',
                              'cwd': str(self.repo), 'tool_input': {'cmd': 'git push --force origin main'}})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_bad_marker_blocks_instead_of_falling_back(self):
        out = runtime.hook({'tool_name': 'Agent', 'tool_input': {'message': runtime.MARKER + '{broken'}})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_cli_routes_without_loading_model_default(self):
        prepared = runtime.prepare(self.task, self.repo)
        argv = dispatch.command(prepared, self.repo, 'read-only')
        self.assertEqual(argv[argv.index('--model') + 1], 'gpt-5.6-sol')
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
        self.assertEqual(argv[-1], '-')

    def test_cli_and_hook_subprocess_wire_format(self):
        task_file = self.repo / 'task.json'
        task_file.write_text(json.dumps(self.task), encoding='utf-8')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/dispatch.py'), 'marker',
                                 '--task', str(task_file), '--cwd', str(self.repo)],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = json.loads(result.stdout)
        event = {'hook_event_name': 'PreToolUse', 'tool_name': 'spawn_agent',
                 'cwd': str(self.repo), 'tool_input': marker}
        response = subprocess.run([sys.executable, str(ROOT / 'hooks/dispatch_hook.py')], input=json.dumps(event),
                                  capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn(self.body, json.loads(response.stdout)['hookSpecificOutput']['updatedInput']['message'])

    def test_intake_card_propagates_selector_failure(self):
        task = dict(self.task, add=['missing-explicit-skill'])
        result = subprocess.run([sys.executable, str(ROOT / 'skills/complexity-router/scripts/intake.py'),
                                 'card', json.dumps(task)], cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Skill selection failed', result.stderr)


class Usage(unittest.TestCase):
    def test_cumulative_tokens_are_not_added_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'transcript.jsonl'
            events = [{'type': 'event_msg', 'payload': {'type': 'token_count', 'info':
                       {'total_token_usage': {'input_tokens': n, 'cached_input_tokens': 5,
                                              'output_tokens': 10, 'total_tokens': n + 10}}}} for n in (20, 30)]
            path.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
            out = ledger.summarize_transcript(path)
            self.assertEqual((out['input'], out['cache_read'], out['output'], out['total']), (25, 5, 10, 40))
            self.assertTrue(out['measured'])

    def test_missing_usage_is_not_reported_as_measured_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'transcript.jsonl'
            path.write_text('{}\n', encoding='utf-8')
            self.assertFalse(ledger.summarize_transcript(path)['measured'])


if __name__ == '__main__':
    unittest.main()
