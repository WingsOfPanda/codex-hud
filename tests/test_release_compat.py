"""Cheap recipe binding checks; native compatibility remains a separate gate."""
import ast
import importlib.util
import copy
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('release_compat', SCRIPTS / 'release_compat.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


class CursorCaptureContract(unittest.TestCase):
    def test_affected_unknown_job_requires_needs_review_row(self):
        tree = ast.parse((compat.RECIPE / 'cursor-capture.py').read_text())
        predicate = next(node.value for node in ast.walk(tree) if isinstance(node, ast.keyword)
                         and node.arg == 'affected_unknown_job_visible')
        code = compile(ast.Expression(predicate), 'cursor-capture.py', 'eval')
        row = '  Background · cursor-detached-0 · needs review · ?'
        cases = {
            'needs-review': ([row], True),
            'unknown': ([row.replace('needs review', 'unknown')], False),
            'stale': ([row.replace('needs review', 'stale')], False),
            'missing-row': ([], False),
            'wrong-job': ([row.replace('cursor-detached-0', 'cursor-detached-1')], False),
            'missing-label': ([row.replace(' · needs review', '')], False),
            'missing-uncertainty': ([row.removesuffix(' · ?')], False),
        }
        for name, (below, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(eval(code, {'below': below}), expected)


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-q')
        (self.source / 'patch').write_text('frontend patch')
        (self.source / 'fixture.py').write_text('fixture source')
        for name in compat.RECOVERY_FIXTURES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('recovery fixture source')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'source')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.package = self.root / 'package'
        self.package.mkdir()
        for package, value in ((self.package, b'new runtime'), (self.root / 'old', b'old runtime')):
            (package / 'bin').mkdir(parents=True)
            (package / 'bin/codex-plus-runtime').write_bytes(value)
        self.archive = self.root / 'package.tar.gz'
        self.archive.write_bytes(b'archive')
        self.stock = self.root / 'stock'
        self.stock.write_bytes(b'official stock')
        self.helper = self.root / 'helper'
        self.helper.write_bytes(b'owned supervisor')
        self.short = self.root / 'short'
        self.short.mkdir()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args], text=True)

    def inputs(self, version='0.4.7'):
        release = '0.163.0-plus.' + version
        manifest = dict(release=release, plus_commit=self.commit, target='x86_64-unknown-linux-gnu',
                        upstream=compat.UPSTREAM, coordinator_protocol=5, execution_record_format=1)
        (self.package / 'codex-plus.json').write_text(json.dumps(manifest))
        approval = self.root / 'approval.json'
        approval.write_text(json.dumps(dict(accepted=True, cleanup_passed=True, native=True, source=self.commit,
            architecture='x86_64', archive_sha256=compat.digest(self.archive), manifest_sha256=compat.digest(self.package / 'codex-plus.json'))))
        plan = dict(release=release, plus_source_commit=self.commit, upstream=compat.UPSTREAM,
            gates={'x86_64': {'compat': {'checks': list(compat.GATES)}, 'captures': {'checks': ['native-cursor-states']}}},
            patch=dict(path='patch', sha256=compat.digest(self.source / 'patch')),
            previous_release=dict(tag='0.163.0-plus.0.4.6', targets={'x86_64': {'manifest_sha256': '1' * 64}}),
            compat=dict(recipe_revision=1, previous_source_commit='2' * 40,
                stock_binary_sha256={'x86_64': compat.digest(self.stock)},
                fixture_files={name: compat.digest(self.source / name)
                    for name in ('fixture.py', *compat.RECOVERY_FIXTURES)}, ap_version='0.7.35'))
        profile = dict(architecture='x86_64', source_root=str(self.source), short_root=str(self.short), python={'path': sys.executable})
        config = dict(old_package=str(self.root / 'old'), new_package=str(self.package), new_archive=str(self.archive),
            stock_binary=str(self.stock), qualification_evidence=str(approval),
            new_manifest_sha256=compat.digest(self.package / 'codex-plus.json'), new_archive_sha256=compat.digest(self.archive),
            qualification_evidence_sha256=compat.digest(approval), root_accepted_final_package=True)
        return plan, profile, config

    def prepare(self, plan, profile, config, name='attempt'):
        attempt = self.root / name
        attempt.mkdir()
        return compat.prepare(plan, profile, attempt, config=config,
                              helper_binding={'helper': str(self.helper), 'sha256': compat.digest(self.helper)})

    def paired_inputs(self):
        plan, profile, config = self.inputs('0.4.9')
        plan.update(upstream=compat.UPSTREAM_165, release='0.165.0-plus.0.4.9')
        plan['previous_release'].update(tag='0.163.0-plus.0.4.9', upstream=compat.UPSTREAM)
        plan['compat'].update(recipe_revision=2,
            stock_binary_sha256={'x86_64': compat.digest(self.stock), 'aarch64': '6' * 64})
        old_stock = self.root / 'old-stock'
        old_stock.write_bytes(b'previous official stock')
        plan['compat']['previous_stock_binary_sha256'] = {'x86_64': compat.digest(old_stock), 'aarch64': '7' * 64}
        for name, mapping in (('TRANSITION_STOCK', plan['compat']['stock_binary_sha256']),
                ('TRANSITION_PREVIOUS_STOCK', plan['compat']['previous_stock_binary_sha256'])):
            mocked = patch.object(compat, name, mapping)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name in (compat.PAIRED_FIXTURE, *compat.MEMORY_FIXTURES):
            path = self.source / name
            path.write_text('paired fixture')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'paired fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        plan['plus_source_commit'] = self.commit
        plan['compat']['fixture_files'].update({name: compat.digest(self.source / name)
            for name in (compat.PAIRED_FIXTURE, *compat.MEMORY_FIXTURES)})
        profile['native_build'] = dict(previous_official_binary=str(old_stock), previous_official_sha256=compat.digest(old_stock))
        plan['gates']['x86_64']['compat']['checks'] = list(compat.gate_names(2))
        manifest = self.package / 'codex-plus.json'
        value = json.loads(manifest.read_text())
        value.update(upstream=plan['upstream'], release=plan['release'], plus_commit=self.commit)
        manifest.write_text(json.dumps(value))
        config['new_manifest_sha256'] = compat.digest(manifest)
        approval = Path(config['qualification_evidence'])
        value = json.loads(approval.read_text())
        value.update(source=self.commit, manifest_sha256=config['new_manifest_sha256'])
        approval.write_text(json.dumps(value))
        config['qualification_evidence_sha256'] = compat.digest(approval)
        return plan, profile, config

    def test_paired_recipe_freezes_old_stock_and_equal_plus_versions_by_full_release(self):
        plan, profile, config = self.paired_inputs()
        prepared = self.prepare(plan, profile, config)
        bound = json.loads((Path(prepared['scope']) / 'native.config.json').read_text())
        self.assertEqual(bound['previous_upstream'], compat.UPSTREAM)
        self.assertEqual(bound['previous_stock_binary'], profile['native_build']['previous_official_binary'])
        self.assertEqual(bound['previous_stock_binary_sha256'], plan['compat']['previous_stock_binary_sha256'])
        self.assertEqual(prepared['gates'], list(compat.GATES) + ['paired-stock-transition'])
        code = """import importlib.util,pathlib
from recipe_context import CONFIG, OLD_VERSION, NEW_VERSION
p=pathlib.Path(__import__('sys').argv[1])
spec=importlib.util.spec_from_file_location('gate',p/'verify-mixes.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
assert OLD_VERSION == NEW_VERSION == '0.4.9'
identities={role:dict(helper_sha256='equal',manifest_sha256=role,manifest=dict(
    release=CONFIG['previous_release'] if role=='old' else CONFIG['release'],
    upstream=CONFIG['previous_upstream'] if role=='old' else CONFIG['upstream'])) for role in ('old','new')}
gate.distinct_packages(identities)
identities['old']['manifest']['upstream']=CONFIG['upstream']
try: gate.distinct_packages(identities)
except AssertionError: pass
else: raise AssertionError('old role accepted candidate upstream')
"""
        result = subprocess.run([sys.executable, '-c', code, str(compat.RECIPE)],
            env=dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=prepared['scope'], PYTHONPATH=str(compat.RECIPE),
                PYTHONDONTWRITEBYTECODE='1'), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_paired_recipe_rejects_old_stock_tamper_and_missing_gate_before_allocation(self):
        plan, profile, config = self.paired_inputs()
        old_stock = Path(profile['native_build']['previous_official_binary'])
        old_stock.write_bytes(b'changed stock')
        with self.assertRaisesRegex(ValueError, 'Previous stock binary digest mismatch'):
            self.prepare(plan, profile, config, 'tampered')
        self.assertFalse((self.root / 'tampered/compat').exists())
        old_stock.write_bytes(b'previous official stock')
        plan['gates']['x86_64']['compat']['checks'] = list(compat.GATES)
        with self.assertRaisesRegex(ValueError, 'Unsupported compatibility checks'):
            self.prepare(plan, profile, config, 'missing-gate')
        self.assertFalse((self.root / 'missing-gate/compat').exists())
        plan['gates']['x86_64']['compat']['checks'] = list(compat.gate_names(2))
        plan['compat']['fixture_files'].pop(compat.MEMORY_FIXTURES[0])
        with self.assertRaisesRegex(ValueError, 'Memory fixture closure missing'):
            self.prepare(plan, profile, config, 'missing-memory')
        self.assertFalse((self.root / 'missing-memory/compat').exists())

    def test_next_revision_changes_data_only_and_loads_actual_validators(self):
        hashes = {path.name: compat.digest(path) for path in compat.RECIPE.iterdir() if path.is_file()}
        for version in ('0.4.7', '0.4.8'):
            with self.subTest(version=version):
                plan, profile, config = self.inputs(version)
                result = self.prepare(plan, profile, config, version)
                scope = Path(result['scope'])
                bound = json.loads((scope / 'native.config.json').read_text())
                self.assertEqual(bound['release'], plan['release'])
                self.assertEqual(bound['recipe_hashes'], hashes)
                self.assertFalse(any(path.suffix == '.py' for path in scope.iterdir()))
                code = """import importlib.util, pathlib
from recipe_context import CONFIG, NEW_VERSION
root = pathlib.Path(__import__('sys').argv[1])
for filename in ('verify-mixes.py','archive-identity.py','run-native-compat-v5.py','old-tui-root-result-v5.py','frontend_contract.py','recovery-policy.py'):
    spec = importlib.util.spec_from_file_location('check', root/filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if hasattr(module, 'validate_config'): module.validate_config(CONFIG)
    if hasattr(module, 'self_check'): module.self_check()
assert NEW_VERSION == __import__('sys').argv[2]
"""
                env = dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=str(scope), PYTHONPATH=str(compat.RECIPE), PYTHONDONTWRITEBYTECODE='1')
                result = subprocess.run([sys.executable, '-c', code, str(compat.RECIPE), version], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_wrong_source_arch_package_and_fixture_bytes(self):
        for mutation, message in ((lambda p, h, c: p.update(plus_source_commit='3' * 40), 'source'),
                                  (lambda p, h, c: h.update(architecture='aarch64'), 'identity'),
                                  (lambda p, h, c: c.update(new_manifest_sha256='4' * 64), 'approval'),
                                  (lambda p, h, c: p['compat']['fixture_files'].update({'fixture.py': '5' * 64}), 'fixture')):
            plan, profile, config = self.inputs()
            mutation(plan, profile, config)
            with self.assertRaises((ValueError, KeyError)):
                self.prepare(plan, profile, config, 'refused-' + message)
            self.assertFalse((self.root / ('refused-' + message) / 'compat').exists())

    def test_new_upstream_or_protocol_refuses_before_scope_creation(self):
        plan, profile, config = self.inputs()
        plan['upstream'] = dict(compat.UPSTREAM, version='0.164.0')
        with self.assertRaisesRegex(ValueError, 'upstream'):
            self.prepare(plan, profile, config)
        plan, profile, config = self.inputs()
        manifest = self.package / 'codex-plus.json'
        value = json.loads(manifest.read_text())
        value['coordinator_protocol'] = 6
        manifest.write_text(json.dumps(value))
        config['new_manifest_sha256'] = compat.digest(manifest)
        approval = Path(config['qualification_evidence'])
        value = json.loads(approval.read_text())
        value['manifest_sha256'] = config['new_manifest_sha256']
        approval.write_text(json.dumps(value))
        config['qualification_evidence_sha256'] = compat.digest(approval)
        with self.assertRaisesRegex(ValueError, 'protocol'):
            self.prepare(plan, profile, config, 'protocol')

    def test_0165_refused_by_config_fixture_and_accepted_package_guards(self):
        import test_packaging as packaging
        plan, profile, config = self.inputs()
        prepared = self.prepare(plan, profile, config)
        bound = json.loads((Path(prepared['scope']) / 'native.config.json').read_text())
        compat.validate_config(bound)
        compat.fixture_inputs(plan, profile)
        compat.accepted_package(bound)
        product = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        product.setUp()
        self.addCleanup(product.doCleanups)
        product.manifest['plus_commit'] = self.commit
        product.seal()
        current = product.manifest['upstream']
        with self.assertRaisesRegex(ValueError, 'New upstream requires recipe qualification'):
            compat.validate_config(dict(bound, upstream=current))
        with self.assertRaisesRegex(ValueError, 'Unsupported compatibility recipe/upstream'):
            compat.fixture_inputs(dict(plan, upstream=current), profile)
        bound.update(new_package=str(product.package), release=product.manifest['release'],
                     new_manifest_sha256=compat.digest(product.package / 'codex-plus.json'))
        approval = Path(bound['qualification_evidence'])
        accepted = json.loads(approval.read_text())
        accepted['manifest_sha256'] = bound['new_manifest_sha256']
        approval.write_text(json.dumps(accepted))
        bound['qualification_evidence_sha256'] = compat.digest(approval)
        with self.assertRaisesRegex(ValueError, 'Unsupported package protocol'):
            compat.accepted_package(bound)

    def test_0165_package_refused_by_legacy_mixer(self):
        import test_packaging as packaging
        plan, profile, config = self.inputs()
        prepared = self.prepare(plan, profile, config)
        product = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        product.setUp()
        self.addCleanup(product.doCleanups)
        code = """import importlib.util,pathlib,sys,unittest
spec=importlib.util.spec_from_file_location('gate',pathlib.Path(sys.argv[1])/'verify-mixes.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
package=pathlib.Path(sys.argv[2])
with unittest.TestCase().assertRaisesRegex(AssertionError,'0.165.0-plus.'):
    gate.package_identity(package,'0.4.9','a'*40,gate.digest(package/'codex-plus.json'))
"""
        env = dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=prepared['scope'],
                   PYTHONPATH=str(compat.RECIPE), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-c', code, str(compat.RECIPE), str(product.package)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def native_evidence(self, name='attempt', paired=False):
        plan, profile, config = self.paired_inputs() if paired else self.inputs()
        prepared = self.prepare(plan, profile, config, name)
        scope = Path(prepared['scope'])
        bound = json.loads((scope / 'native.config.json').read_text())
        (scope / 'short-binding.json').write_text(json.dumps({'path': str(self.short / 'aa')}))
        evidence = scope / 'evidence'
        captures = evidence / 'cursor-v2'
        captures.mkdir(parents=True)
        for name in ('rollback/gate.json', 'advisor-nine-checks.json', 'old-tui-root-result/report.json'):
            path = evidence / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"passed":true}')
        (captures / 'manifest.json').write_text(json.dumps(dict(candidate_acceptance=True, capture_complete=True, captures=list(range(20)))))
        (captures / 'integrity.json').write_text('{"passed":true}')
        (captures / 'screen.txt').write_text('native captured screen')
        raw = dict(passed=True, architecture='x86_64', source_commit=self.commit,
            configuration_sha256=compat.digest(scope / 'native.config.json'), gate_sha256=bound['recipe_hashes']['verify-mixes.py'],
            archive={'archive_sha256': config['new_archive_sha256']}, packages={'new': {'manifest_sha256': config['new_manifest_sha256']}},
            evidence_sha256={str(path.relative_to(evidence)): compat.digest(path) for path in evidence.rglob('*.json') if not path.is_relative_to(captures)},
            stages=[dict(name=name, passed=True, exit_code=0) for name in compat.gate_names(bound['recipe_revision'])])
        cases = []
        new_sha = compat.digest(self.package / 'bin/codex-plus-runtime')
        old_sha = compat.digest(self.root / 'old/bin/codex-plus-runtime')
        for suite, scenarios in (
                ('detached', ('cleanup-recovery-success', 'cleanup-recovery-failure', 'cleanup-recovery-old-reader',
                    'goal-cleanup-recovery', 'goal-cleanup-recovery-old-reader', 'daemon-graceful',
                    'observation-gap', 'cancellation', 'watch-recovery')),
                ('goals', ('input-epoch', 'paused-recovery', 'replaced-recovery',
                    'mixed-independent-priority', 'pre-report-recovery-barrier'))):
            for scenario in scenarios:
                path = evidence / 'recovery-policy' / suite / scenario / 'report.json'
                path.parent.mkdir(parents=True)
                case = dict(passed=True, scenario=scenario, runtime_sha256=new_sha,
                    stock_sha256=compat.digest(self.stock), stock_version='codex-cli ' + bound['upstream']['version'],
                    remote_model_requests=0, private_home_removed=True,
                    private_home=str(self.short / 'aa' / scenario / 'home'))
                if scenario.endswith('old-reader'):
                    case['old_runtime_sha256'] = old_sha
                if paired:
                    case.update(stock_role='candidate', stock_upstream=bound['upstream'])
                if scenario.startswith('cleanup-recovery'):
                    case['recovery_policy'] = dict(exit_code=0 if scenario.endswith('success') else 7,
                        old_reader=scenario.endswith('old-reader'),
                        notification='REPORT ONLY:' if scenario.endswith('old-reader') else 'RECOVERY REQUIRED:',
                        one_workload=True, original_result_preserved=True,
                        claims_held_until_verified_resolution=True)
                elif scenario.startswith('goal-cleanup-recovery'):
                    old_reader = scenario.endswith('old-reader')
                    case['goal_recovery'] = dict(old_reader=old_reader,
                        notification='REPORT ONLY:' if old_reader else 'RECOVERY REQUIRED:',
                        report_barrier=int(old_reader), original_result_preserved=True,
                        one_workload=True, cleanup_claim_retained=old_reader, recovered_in_same_callback=not old_reader)
                path.write_text(json.dumps(case))
                cases.append(dict(suite=suite, scenario=scenario,
                    report=str(path.relative_to(evidence / 'recovery-policy')), sha256=compat.digest(path)))
        recovery = evidence / 'recovery-policy/report.json'
        recovery_value = dict(schema=1, passed=True, source_commit=self.commit,
            runtime_sha256=new_sha, old_runtime_sha256=old_sha,
            stock_sha256=compat.digest(self.stock), cases=cases)
        if paired:
            recovery_value.update(stock_role='candidate', stock_upstream=bound['upstream'])
        recovery.write_text(json.dumps(recovery_value))
        raw['packages']['new']['helper_sha256'] = new_sha
        raw['packages']['old'] = {'helper_sha256': old_sha}
        raw['evidence_sha256'].update({str(path.relative_to(evidence)): compat.digest(path)
            for path in (recovery, *(evidence / 'recovery-policy' / row['report'] for row in cases))})
        if paired:
            self.paired_report(scope, bound)
            path = evidence / 'paired-stock-transition/report.json'
            raw['evidence_sha256']['paired-stock-transition/report.json'] = compat.digest(path)
            paired_report = json.loads(path.read_text())
            raw['evidence_sha256'].update({'paired-stock-transition/' + name: sha
                for name, sha in paired_report['memory_evidence_sha256'].items()})
        (evidence / 'report.json').write_text(json.dumps(raw))
        return scope, raw

    def paired_report(self, scope, bound):
        path = scope / 'evidence/paired-stock-transition/report.json'
        fixture_path = path.parent / 'fixture/report.json'
        fixture_path.parent.mkdir(parents=True)
        parent, execution = '1' * 36, '2' * 36
        high = dict(type='configuration_update', reasoning=dict(effort='high'))
        xhigh = dict(type='configuration_update', reasoning=dict(effort='xhigh'))
        fixture = dict(passed=True, remote_model_requests=0, parent=parent,
            stock_sha256=bound['previous_stock_binary_sha256']['x86_64'], stock_version='codex-cli 0.163.0',
            runtime_sha256=compat.digest(self.root / 'old/bin/codex-plus-runtime'),
            provider_requests=[dict(thread=parent, body=dict(model='gpt-6.1-sol', reasoning=dict(effort='high'), input=updates,
                prompt_cache_key='stable-paired-key')) for updates in ([high], [high, xhigh], [])])
        fixture_path.write_text(json.dumps(fixture))
        (path.parent / 'fixture/config.toml').write_text('model_provider="isolated"\n[model_providers.isolated]\nname="OpenAI"\n')
        (path.parent / 'fixture/paired-models.json').write_text(json.dumps({'models': [dict(
            slug='gpt-6.1-sol', supports_reasoning_effort_updates=True)]}))
        before_history = path.parent / 'fixture/stock-history-before-rollback.jsonl'
        after_history = path.parent / 'fixture/stock-history-after-rollback.jsonl'
        history_rows = [dict(type='session_meta', payload=dict(id=parent))]
        for turn, effort, update in (('candidate-first', 'high', high), ('candidate-turn', 'xhigh', xhigh)):
            history_rows += [dict(type='turn_context', payload=dict(turn_id=turn, effort=effort)),
                dict(type='response_item', payload=update),
                dict(type='event_msg', payload=dict(type='task_complete', turn_id=turn))]
        before_history.write_text(''.join(json.dumps(row) + '\n' for row in history_rows))
        appended = [dict(type='turn_context', payload=dict(turn_id='rollback-turn', effort='high')),
            dict(type='event_msg', payload=dict(type='task_complete', turn_id='rollback-turn'))]
        after_history.write_bytes(before_history.read_bytes() + ''.join(json.dumps(row) + '\n' for row in appended).encode())
        full_history_path = path.parent / 'fixture/stock-read-before-rollback.json'
        full_history_path.write_text(json.dumps(dict(id=parent, turns=[dict(id=turn, status='completed')
            for turn in ('candidate-first', 'candidate-turn')])))
        report = {key: bound[key] for key in
            ('source_commit', 'release', 'previous_release', 'upstream', 'previous_upstream', 'architecture')}
        report.update(schema=1, passed=True, old_stock_readable=True, old_stock_resumed_inference=True,
            original_result_preserved=True, rows_unchanged=True, private_home_removed=True,
            snapshot_restore=False, remote_model_requests=0, provider_requests_from_reads_holds_restart=0,
            workload_runs=1, callbacks=1, owned_survivors_after_cleanup=[],
            private_home=str(self.short / 'aa/home'), configuration_sha256=compat.digest(scope / 'native.config.json'),
            new_manifest_sha256=bound['new_manifest_sha256'], old_manifest_sha256=bound['previous_identity']['manifests']['x86_64'],
            fixture_report_sha256=compat.digest(fixture_path), new_turn='candidate-turn', rollback_turn='rollback-turn', execution_id=execution)
        report['effort_override'] = dict(selected_efforts=['high', 'xhigh'], request_efforts=['high', 'high'],
            configuration_updates=[[high], [high, xhigh]], persisted_updates=[high, xhigh],
            stable_prompt_cache_key='stable-paired-key', candidate_turns=['candidate-first', 'candidate-turn'],
            history_before_sha256=compat.digest(before_history), history_after_sha256=compat.digest(after_history),
            stock_read_before_sha256=compat.digest(full_history_path),
            original_history_prefix_preserved=True, rollback_selected_effort='high', rollback_request_effort='high')
        report['phases'] = [dict(name=name, generation=str(index),
            stock_sha256=bound['previous_stock_binary_sha256' if old else 'stock_binary_sha256']['x86_64'],
            stock_version='codex-cli ' + bound['previous_upstream' if old else 'upstream']['version'],
            runtime_sha256=compat.digest(self.root / 'old/bin/codex-plus-runtime' if old else self.package / 'bin/codex-plus-runtime'))
            for index, (name, old) in enumerate((('old-before', True), ('candidate', False), ('old-rollback', True)))]
        report['ledger_before'] = report['ledger_after'] = {'tables': {name: [{'id': 'original'}] for name in
            ('jobs', 'callbacks', 'goals', 'ultracode_turns', 'ultracode_submissions')}}
        for label, directory, clock in (('before', path.parent / 'fixture/execution-baseline', 1),
                ('after', path.parent / 'fixture/executions/v1' / parent / execution, 2)):
            directory.mkdir(parents=True)
            for name in ('manifest.json', 'state.json', 'result.json', 'observation.json'):
                (directory / name).write_text('{"updated_at":' + str(clock) + ',"state":"completed"}' if name == 'state.json' else '{}')
            report['execution_' + label + '_sha256'] = {item.name: compat.digest(item) for item in directory.iterdir()}
            report['execution_state_' + label] = dict(clock=clock,
                bytes_sha256=hashlib.sha256(b'{"updated_at":<updated_at>,"state":"completed"}').hexdigest())
        workload = path.parent / 'fixture/work/job/runs'
        workload.parent.mkdir(parents=True)
        workload.write_text('run\n')
        report['holds'] = [dict(entered=dict(id=ident, held=True, admission_quiet=True, execution_quiet=True),
            observed=dict(id=ident, held=True, admission_quiet=True, execution_quiet=True), left=dict(id=ident, held=False))
            for ident in ('old-entered', 'new-entered')]
        from test_release_memory_reports import memory_evidence
        report['memory_lanes'], report['memory_evidence_sha256'] = memory_evidence(path.parent, bound)
        path.write_text(json.dumps(report))
        return path, report

    def test_paired_export_requires_complete_hashed_role_bound_transition_proof(self):
        scope, raw = self.native_evidence(paired=True)
        result = compat.export_reports(scope)
        exported = json.loads(Path(result['compat']).read_text())
        self.assertEqual(set(exported['checks']), set(compat.gate_names(2)))
        self.assertIn('compat/evidence/paired-stock-transition/report.json',
            exported['checks']['paired-stock-transition']['evidence'])
        bound = json.loads((scope / 'native.config.json').read_text())
        path = scope / 'evidence/paired-stock-transition/report.json'
        original = json.loads(path.read_text())
        mutations = {
            'wrong-old-stock': lambda r: r['phases'][2].update(stock_sha256=bound['stock_binary_sha256']['x86_64']),
            'wrong-helper': lambda r: r['phases'][0].update(runtime_sha256='0' * 64),
            'missing-phase': lambda r: r['phases'].pop(),
            'same-generation': lambda r: r['phases'][2].update(generation=r['phases'][0]['generation']),
            'no-inference': lambda r: r.update(old_stock_resumed_inference=False),
            'snapshot-fallback': lambda r: r.update(snapshot_restore=True),
            'changed-rows': lambda r: r['ledger_after']['tables']['callbacks'].append({'id': 'duplicate'}),
            'changed-record': lambda r: r['execution_after_sha256'].update(result_json='3' * 64),
            'changed-state': lambda r: r['execution_state_after'].update(bytes_sha256='3' * 64),
            'hold-not-quiet': lambda r: r['holds'][0]['observed'].update(execution_quiet=False),
            'wrong-fixture-hash': lambda r: r.update(fixture_report_sha256='0' * 64),
            'no-configuration-updates': lambda r: r['effort_override'].update(configuration_updates=[[], []]),
            'no-persisted-updates': lambda r: r['effort_override'].update(persisted_updates=[]),
            'no-full-history-read': lambda r: r['effort_override'].update(stock_read_before_sha256='0' * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                report = copy.deepcopy(original)
                mutate(report)
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    compat.validate_paired_report(path, bound)
        path.write_text(json.dumps(original))
        raw['evidence_sha256'].pop('paired-stock-transition/report.json')
        (scope / 'evidence/report.json').write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, 'Paired dedicated report missing'):
            compat.export_reports(scope)

    def test_paired_report_rejects_missing_wire_updates_after_rehashing_all_summaries(self):
        scope, raw = self.native_evidence(paired=True)
        config = json.loads((scope / 'native.config.json').read_text())
        path = scope / 'evidence/paired-stock-transition/report.json'
        report = json.loads(path.read_text())
        fixture_path = path.parent / 'fixture/report.json'
        fixture = json.loads(fixture_path.read_text())
        for request in fixture['provider_requests']:
            request['body']['input'] = []
        fixture_path.write_text(json.dumps(fixture))
        report['fixture_report_sha256'] = compat.digest(fixture_path)
        report['effort_override']['configuration_updates'] = [[], []]
        path.write_text(json.dumps(report))
        raw['evidence_sha256']['paired-stock-transition/report.json'] = compat.digest(path)
        (scope / 'evidence/report.json').write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, 'effort override configuration updates missing'):
            compat.export_reports(scope)

    def test_paired_recovery_cases_bind_candidate_daemon_even_for_old_reader(self):
        scope, unused = self.native_evidence(paired=True)
        config = json.loads((scope / 'native.config.json').read_text())
        evidence = scope / 'evidence/recovery-policy'
        compat.validate_recovery_reports(evidence, config)
        report = json.loads((evidence / 'report.json').read_text())
        row = next(row for row in report['cases'] if row['scenario'] == 'cleanup-recovery-old-reader')
        case_path = evidence / row['report']
        original = json.loads(case_path.read_text())
        for patch_value in ({'stock_role': 'previous'}, {'stock_upstream': compat.UPSTREAM},
                {'stock_version': 'codex-cli 0.163.0'}, {'old_runtime_sha256': '0' * 64}):
            with self.subTest(patch=patch_value):
                case_path.write_text(json.dumps(dict(original, **patch_value)))
                row['sha256'] = compat.digest(case_path)
                (evidence / 'report.json').write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    compat.validate_recovery_reports(evidence, config)

    def test_coordinator_reports_bind_dedicated_results_and_native_capture_bytes(self):
        scope, raw = self.native_evidence()
        evidence = scope / 'evidence'
        captures = evidence / 'cursor-v2'
        result = compat.export_reports(scope)
        report = json.loads(Path(result['compat']).read_text())
        self.assertEqual(set(report['checks']), set(compat.GATES))
        self.assertIn('compat/evidence/rollback/gate.json', report['checks']['quiet-rollback']['evidence'])
        recovery_binding = report['checks']['packaged-recovery-policy']['evidence']
        self.assertIn('compat/evidence/recovery-policy/report.json', recovery_binding)
        self.assertEqual(sum(name.endswith('/report.json') and '/recovery-policy/' in name
            for name in recovery_binding), 15)
        inventory = json.loads(Path(result['capture_manifest']).read_text())
        self.assertEqual(inventory['files']['compat/evidence/cursor-v2/screen.txt']['sha256'], compat.digest(captures / 'screen.txt'))
        (evidence / 'rollback/gate.json').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'dedicated report changed'):
            compat.export_reports(scope)

    def test_export_rejects_invalid_recovery_reports_despite_passing_stage(self):
        mutations = {
            'missing': lambda report: report['cases'].pop(),
            'failed': lambda report: report.update(passed=False),
            'wrong-helper': lambda report: report.update(runtime_sha256='c' * 64),
            'wrong-old-helper': lambda report: report.update(old_runtime_sha256='c' * 64),
            'wrong-stock': lambda report: report.update(stock_sha256='c' * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                scope, raw = self.native_evidence(name)
                path = scope / 'evidence/recovery-policy/report.json'
                recovery = json.loads(path.read_text())
                mutate(recovery)
                path.write_text(json.dumps(recovery))
                raw['evidence_sha256']['recovery-policy/report.json'] = compat.digest(path)
                (scope / 'evidence/report.json').write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, '[Rr]ecovery'):
                    compat.export_reports(scope)

    def test_export_reads_each_recovery_case_and_its_cleanup_and_policy(self):
        mutations = {
            'failed-case': lambda case: case.update(passed=False),
            'wrong-helper-case': lambda case: case.update(runtime_sha256='c' * 64),
            'wrong-old-helper-case': lambda case: case.update(old_runtime_sha256='c' * 64),
            'wrong-stock-case': lambda case: case.update(stock_sha256='c' * 64),
            'paid-request': lambda case: case.update(remote_model_requests=1),
            'unclean-home': lambda case: case.update(private_home_removed=False),
            'escaped-home': lambda case: case.update(private_home='/outside-owned-scope/home'),
            'sibling-home': lambda case: case.update(private_home=str(self.short / 'bb/home')),
            'dotdot-home': lambda case: case.update(private_home=str(self.short / 'aa/../outside/home')),
            'allocation-root': lambda case: case.update(private_home=str(self.short / 'aa')),
            'wrong-policy': lambda case: case['recovery_policy'].update(notification='RECOVERY REQUIRED:'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                scope, raw = self.native_evidence(name)
                root = scope / 'evidence/recovery-policy'
                recovery = json.loads((root / 'report.json').read_text())
                row = recovery['cases'][2]
                path = root / row['report']
                case = json.loads(path.read_text())
                mutate(case)
                path.write_text(json.dumps(case))
                row['sha256'] = compat.digest(path)
                (root / 'report.json').write_text(json.dumps(recovery))
                for path in (path, root / 'report.json'):
                    raw['evidence_sha256'][str(path.relative_to(scope / 'evidence'))] = compat.digest(path)
                (scope / 'evidence/report.json').write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, '[Rr]ecovery'):
                    compat.export_reports(scope)

    def test_recovery_report_hash_and_native_evidence_inventory_are_required(self):
        for changed in ('case-hash', 'native-inventory'):
            with self.subTest(changed=changed):
                scope, raw = self.native_evidence(changed)
                path = scope / 'evidence/recovery-policy/detached/cleanup-recovery-success/report.json'
                if changed == 'case-hash':
                    path.write_text(path.read_text() + '\n')
                    raw['evidence_sha256'][str(path.relative_to(scope / 'evidence'))] = compat.digest(path)
                else:
                    del raw['evidence_sha256'][str(path.relative_to(scope / 'evidence'))]
                (scope / 'evidence/report.json').write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, '[Rr]ecovery'):
                    compat.export_reports(scope)

    def test_recovery_runner_executes_all_cases_with_bound_helpers_and_owned_homes(self):
        scope, raw = self.native_evidence()
        config = json.loads((scope / 'native.config.json').read_text())
        short = self.short / 'aa'
        short.mkdir()
        called = []
        templates = scope / 'evidence/recovery-policy'
        class Fixture:
            def __init__(inner, runtime, binary, evidence):
                self.assertEqual(runtime, self.package / 'bin/codex-plus-runtime')
                self.assertEqual(binary, self.stock)
                inner.evidence = evidence
                evidence.mkdir(parents=True)
                inner.home = short / evidence.name / 'home'
                inner.home.mkdir(parents=True)
                inner.report = json.loads((templates / evidence.parent.name / evidence.name / 'report.json').read_text())
            def close(inner, passed):
                self.assertEqual(inner.old_runtime, self.root / 'old/bin/codex-plus-runtime')
                inner.report['passed'] = passed
                (inner.evidence / 'report.json').write_text(json.dumps(inner.report))
                inner.home.rmdir()
        modules = {}
        for suite, name in (('detached', 'test_detached_jobs_stock'), ('goals', 'test_custom_goals_stock')):
            modules[name] = SimpleNamespace(SCENARIOS={scenario: lambda fixture, scenario=scenario:
                called.append(scenario) for scenario in compat.RECOVERY_CASES[suite]})
        fixture = SimpleNamespace(CustomGoalServer=Fixture)
        modules.update(fixtures=SimpleNamespace(custom_goal_server=fixture),
            **{'fixtures.detached_jobs': SimpleNamespace(DetachedJobServer=Fixture),
               'recipe_context': SimpleNamespace(CONFIG=config, DATA=scope),
               'fixture_scope': SimpleNamespace(validate_short=lambda: short)})
        spec = importlib.util.spec_from_file_location('recovery_recipe', compat.RECIPE / 'recovery-policy.py')
        recipe = importlib.util.module_from_spec(spec)
        with patch.object(os, 'umask'), patch.dict(sys.modules, modules), patch.object(sys, 'path', list(sys.path)), patch.dict(os.environ,
                TMPDIR=str(short), CODEX_PLUS_RELEASE_QA_SCOPE=str(scope)):
            spec.loader.exec_module(recipe)
            recipe.run(scope / 'runner-evidence')
        self.assertEqual(called, [scenario for cases in compat.RECOVERY_CASES.values() for scenario in cases])
        self.assertEqual(len(compat.validate_recovery_reports(scope / 'runner-evidence', config)), 15)

    def test_setup_failure_stops_before_native_gates_and_preserves_allocation(self):
        plan, profile, config = self.inputs()
        result = self.prepare(plan, profile, config)
        calls = []
        def execute(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=1)
        supervisor = SimpleNamespace(run=execute)
        cleanup = SimpleNamespace(capture_baseline=lambda *args: {'boot_id': 'fixture', 'processes': {}})
        with patch.dict(sys.modules, owned_run=supervisor, permission_cleanup=cleanup), patch.dict(os.environ):
            with self.assertRaisesRegex(ValueError, 'Setup failed'):
                compat.run(result['scope'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], [sys.executable, compat.RECIPE / 'preflight.py'])
        scope = Path(result['scope'])
        self.assertTrue((scope / 'short-state.json').exists())
        self.assertTrue((scope / 'short-binding.json').exists())
        self.assertFalse((scope / 'native-owned.log').exists())

    def test_wrong_native_host_refuses_before_constructor(self):
        plan, profile, config = self.inputs()
        result = self.prepare(plan, profile, config)
        with patch.object(compat.platform, 'machine', return_value='aarch64'):
            with self.assertRaisesRegex(ValueError, 'native host'):
                compat.run(result['scope'], setup_only=True)
        self.assertFalse((Path(result['scope']) / 'short-state.json').exists())

    def test_outer_inner_cursor_logs_are_distinct_and_setup_precedes_gates(self):
        setup = (compat.RECIPE / 'preflight.py').read_text()
        cursor = (compat.RECIPE / 'run-cursor.py').read_text()
        self.assertIn("setup-smoke/cursor-controller.log", setup)
        self.assertIn("args.evidence.with_suffix('.log').open('x')", cursor)
        self.assertNotEqual(Path('setup-smoke/cursor-controller.log'), Path('setup-smoke/cursor').with_suffix('.log'))
        self.assertIn('fixture = DetachedJobServer(', setup)
        self.assertIn('case.setUp()', setup)
        self.assertIn("'--setup-only'", setup)
        self.assertIn('geometry only, no runtime semantics claim', (compat.RECIPE / 'cursor-capture.py').read_text())

    def test_source_status_preserves_alternate_index_bytes(self):
        plan, profile, config = self.inputs()
        result = self.prepare(plan, profile, config)
        alternate = self.root / 'alternate-index'
        alternate.write_bytes((self.source / '.git/index').read_bytes())
        before = alternate.read_bytes(), alternate.stat().st_mtime_ns
        code = 'from recipe_context import CONFIG,source_identity; source_identity(CONFIG["source"],CONFIG["source_commit"])'
        env = dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=result['scope'], PYTHONPATH=str(compat.RECIPE),
                   GIT_INDEX_FILE=str(alternate), PYTHONDONTWRITEBYTECODE='1')
        completed = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((alternate.read_bytes(), alternate.stat().st_mtime_ns), before)

    def guard_module(self, scope):
        context = SimpleNamespace(CONFIG={}, DATA=scope, RECIPE=compat.RECIPE)
        spec = importlib.util.spec_from_file_location('journal_guard', compat.RECIPE / 'owned_run.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, recipe_context=context):
            spec.loader.exec_module(module)
        compat.write(scope / 'helper-binding.json', {'helper': str(self.helper), 'sha256': compat.digest(self.helper)})
        return module

    def test_helper_only_release_accepts_identical_frontend_bytes(self):
        import test_packaging as packaging
        from release_fixture_source import legacy_source
        from test_release_protocol import protocol
        source = legacy_source(self.root / 'source-0163')
        packager = protocol._module(source / 'scripts/package-release.py')
        old = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        old.setUp(packager=packager)
        self.addCleanup(old.doCleanups)
        new = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        new.setUp(packager=packager)
        self.addCleanup(new.doCleanups)
        old.manifest.update(release='0.163.0-plus.0.4.6', plus_commit='2' * 40)
        (old.package / 'bin/codex-plus-runtime').write_bytes((old.package / 'bin/codex-plus-runtime').read_bytes() + b'old helper')
        old.seal()
        plan, profile, config = self.inputs(packager.PLUS_VERSION)
        prepared = self.prepare(plan, profile, config)
        code = """import importlib.util,pathlib,sys
from recipe_context import CONFIG,OLD_VERSION,NEW_VERSION
spec=importlib.util.spec_from_file_location('gate',pathlib.Path(sys.argv[1])/'verify-mixes.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
old=gate.package_identity(pathlib.Path(sys.argv[2]),OLD_VERSION,'2'*40,gate.digest(pathlib.Path(sys.argv[2])/'codex-plus.json'))
new=gate.package_identity(pathlib.Path(sys.argv[3]),NEW_VERSION,'a'*40,gate.digest(pathlib.Path(sys.argv[3])/'codex-plus.json'))
assert old['manifest']['files']['bin/codex']['sha256']==new['manifest']['files']['bin/codex']['sha256']
gate.distinct_packages(dict(old=old,new=new))
driver=(pathlib.Path(sys.argv[1])/'run-native-compat-v5.py').read_text()
assert 'changed frontend requires fresh package build' not in driver
"""
        env = dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=prepared['scope'], PYTHONPATH=str(compat.RECIPE), PYTHONDONTWRITEBYTECODE='1')
        completed = subprocess.run([sys.executable, '-c', code, str(compat.RECIPE), str(old.package), str(new.package)], env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def workflow(self):
        spec = importlib.util.spec_from_file_location('compat_workflow', compat.RECIPE / 'workflow.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_production_composition_writes_real_core_compat_and_capture_receipts(self):
        self.production_composition(paired=False)

    def test_paired_composition_freezes_and_executes_previous_stock_with_previous_upstream(self):
        self.production_composition(paired=True)

    def production_composition(self, *, paired):
        import test_release_protocol as fixtures
        import test_packaging as packaging
        workflow = self.workflow()
        fixture = fixtures.ReleaseProtocolTests()
        fixture.setUp(legacy=not paired)
        self.addCleanup(fixture.doCleanups)
        if paired:
            fixture.plan = fixture.transition_plan()
        old = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        old_packager = fixture.packager if not paired else fixtures.protocol._module(
            fixtures.legacy_source(fixture.base / 'previous-source-0163') / 'scripts/package-release.py')
        old.setUp(packager=old_packager)
        self.addCleanup(old.doCleanups)
        old.manifest.update(release=fixture.plan['previous_release']['tag'], plus_commit=fixture.plan['compat']['previous_source_commit'])
        old.seal()
        previous = fixture.plan['previous_release']['targets']['x86_64']
        previous.update(manifest_sha256=compat.digest(old.package / 'codex-plus.json'), tree_sha256=old.manifest['tree_sha256'])
        fixture.profile['python']['path'] = sys.executable
        site = fixture.base / 'fixture-site'
        for name in ('pyte/__init__.py', 'pyte/escape.py', 'wcwidth.py'):
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('FIXTURE_MODULE = True\n')
        fixture.profile['python'].update(fixture_sites=[str(site)], fixture_imports={
            name: {'origin': str(site / path), 'sha256': compat.digest(site / path)}
            for name, path in (('pyte', 'pyte/__init__.py'), ('wcwidth', 'wcwidth.py'))})
        executor = fixture.base / 'executor'
        (executor / 'bin').mkdir(parents=True)
        (executor / 'bin/codex-plus-runtime').write_bytes(b'pinned executor')
        fixture.profile['executor_package_root'] = str(executor)
        official = fixture.base / 'official'
        official.write_bytes(b'pinned stock')
        fixture.profile['native_build'] = {name: str(fixture.base / name) for name in
            ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache', 'license_cache')}
        fixture.profile['native_build'].update(official_binary=str(official), official_sha256=compat.digest(official), cargo_jobs=1, test_threads=1)
        if paired:
            previous_stock = fixture.base / 'previous-official'
            previous_stock.write_bytes(b'pinned previous stock')
            fixture.profile['native_build'].update(previous_official_binary=str(previous_stock),
                previous_official_sha256=compat.digest(previous_stock))
            fixture.plan['compat']['previous_stock_binary_sha256']['x86_64'] = compat.digest(previous_stock)
        source = fixture.base / 'product-source'
        for name in ('scripts/package-release.py', 'scripts/native-results.py', 'rust-runtime/Cargo.toml',
                     'rust-runtime/Cargo.lock', 'bin/codex-plus', fixture.plan['patch']['path'], *compat.RECOVERY_FIXTURES,
                     *((compat.PAIRED_FIXTURE, *compat.MEMORY_FIXTURES) if paired else ())):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((fixture.source / name).read_bytes())
            path.chmod((fixture.source / name).stat().st_mode & 0o777)
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'source'], check=True)
        fixture.plan['plus_source_commit'] = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        fixture.observed['plus_source_commit'] = fixture.plan['plus_source_commit']
        fixture.profile['source_root'] = str(source)
        fixture.plan['compat']['stock_binary_sha256']['x86_64'] = compat.digest(official)
        if paired:
            for module in (fixtures.protocol, workflow.protocol, compat, workflow.compat):
                for name, mapping in (('TRANSITION_STOCK', fixture.plan['compat']['stock_binary_sha256']),
                        ('TRANSITION_PREVIOUS_STOCK', fixture.plan['compat']['previous_stock_binary_sha256'])):
                    mocked = patch.object(module, name, mapping)
                    mocked.start()
                    self.addCleanup(mocked.stop)
        fixture.plan['compat']['fixture_files'].update({name: compat.digest(fixture.source / name)
            for name in compat.RECOVERY_FIXTURES})
        if paired:
            fixture.plan['compat']['fixture_files'].update({name: compat.digest(source / name)
                for name in (compat.PAIRED_FIXTURE, *compat.MEMORY_FIXTURES)})
        for name in ('python', 'executor_package_root'):
            fixture.observed[name] = copy.deepcopy(fixture.profile[name])
        for arch in fixture.plan['targets']:
            fixture.plan['targets'][arch]['profile_sha256'] = fixtures.protocol.digest(fixture.profile)
            fixture.plan['gates'][arch]['compat']['checks'] = list(compat.gate_names(2 if paired else 1))
            fixture.plan['gates'][arch]['captures']['checks'] = ['native-cursor-states']
        original_setup = packaging.PackageIntegrityTests.setUp
        def package_setup(value, **kwargs):
            original_setup(value, **kwargs)
            (value.package / 'bin/codex-plus').write_bytes((fixture.source / 'bin/codex-plus').read_bytes())
            value.manifest['plus_commit'] = fixture.plan['plus_source_commit']
            value.seal()
        with patch.object(packaging.PackageIntegrityTests, 'setUp', package_setup):
            root = fixture.qualified(package_only=True)
        report = fixtures.protocol.load(root / 'outputs/package/report.json')
        package = root / report['package_dir']
        fixtures.protocol.write_once(root / 'native-preparation.json', {'sealed': 'fixture'})
        fixtures.protocol.write_once(root / 'ownership-baseline.json', {'processes': {}})
        for defect in ('read-only-modes', 'content', 'missing', 'extra', 'symlink'):
            candidate = fixture.base / ('previous-' + defect)
            shutil.copytree(old.package, candidate)
            helper = candidate / 'bin/codex-plus-runtime'
            if defect == 'read-only-modes':
                for path in candidate.rglob('*'):
                    if path.is_file():
                        path.chmod(path.stat().st_mode & ~0o222)
                self.assertEqual({name: (row['sha256'], row['size']) for name, row in
                    packaging.package_release.tree_files(candidate).items()},
                    {name: (row['sha256'], row['size']) for name, row in old.manifest['files'].items()})
            elif defect == 'content':
                helper.write_bytes(helper.read_bytes() + b'changed')
            elif defect == 'missing':
                helper.unlink()
            elif defect == 'extra':
                (candidate / 'extra').write_text('unqualified')
            else:
                helper.unlink()
                helper.symlink_to(old.package / 'bin/codex-plus-runtime')
            with patch.object(workflow.native, 'verify', return_value={}):
                with self.assertRaisesRegex(ValueError, 'inventory|package entry', msg=defect):
                    workflow.prepare_attempt(root, candidate)
            for name in ('outputs/compat-package-approval.json', 'outputs/compat-previous-package.json',
                         'compat', 'compat-preparation.json', 'compat-job-proposal.json'):
                self.assertFalse((root / name).exists(), (defect, name))
        canonical = fixture.base / 'previous-canonical'
        shutil.copytree(old.package, canonical)
        with patch.object(workflow.native, 'verify', return_value={}):
            proposal = workflow.prepare_attempt(root, canonical)
            workflow.verify_attempt(root)
        self.assertEqual(proposal['argv'][3], 'run-attempt')
        self.assertEqual(proposal['deadline_seconds'], 1680 if paired else 1500)
        scope = root / 'compat'
        self.assertFalse(any(path.suffix == '.py' for path in scope.iterdir()))
        def execute(*args, **kwargs):
            raw = fixture.put(root, 'compat/raw.log', 'actual simulated child completion')
            evidence = {raw: fixtures.protocol.file_record(root / raw)['sha256']}
            common = dict(architecture='x86_64', plus_source_commit=fixture.plan['plus_source_commit'])
            fixture.put(root, 'compat/compat-report.json', dict(common, checks={name: {'outcome': 'passed', 'evidence': evidence} for name in compat.gate_names(2 if paired else 1)}))
            screen = fixture.put(root, 'compat/screen.txt', 'native geometry bytes')
            inventory = fixture.put(root, 'compat/capture-manifest.json', dict(architecture='x86_64', package_sha256=compat.digest(root / report['archive']),
                files={screen: fixtures.protocol.file_record(root / screen)}))
            fixture.put(root, 'compat/captures-report.json', dict(common, package_sha256=compat.digest(root / report['archive']),
                capture_manifest=inventory, capture_manifest_sha256=compat.digest(root / inventory),
                checks={'native-cursor-states': {'outcome': 'passed', 'evidence': evidence}}))
            return 0
        def clean(target, output):
            result = {'outcome': 'clean', 'owned_remaining': [], 'unknown': []}
            fixtures.protocol.write_once(output, result)
            return result
        with patch.object(workflow.native, 'verify', return_value={}), \
             patch.object(workflow, 'bound_execution', return_value=('managed-compatible', 'c' * 64)), \
             patch.object(workflow.native, 'owned_run', side_effect=execute), \
             patch.object(workflow, 'cleanup_attempt', side_effect=clean), \
             patch.object(workflow.native, 'cleanup', side_effect=clean):
            state = workflow.run_attempt(root, fixture.base)
        self.assertEqual(state['missing'], ['ap'])
        self.assertFalse(state['refused'])
        rows = fixtures.protocol._receipts(root)
        self.assertEqual(rows['compat']['input']['execution']['job_id'], 'managed-compatible')
        self.assertEqual(rows['compat']['input']['previous_package'], 'outputs/compat-previous-package.json')
        self.assertIn(str(canonical / 'bin/codex'), rows['compat']['input']['executed'])
        self.assertIn(str(SCRIPTS / 'package-release.py'), rows['compat']['input']['executed'])
        if paired:
            self.assertIn(str(previous_stock), rows['compat']['input']['executed'])
            frozen = fixtures.protocol.load(root / 'compat-preparation.json')['closure']
            self.assertEqual(frozen[str(previous_stock)]['sha256'], compat.digest(previous_stock))
            self.assertEqual(fixtures.protocol.load(scope / 'native.config.json')['previous_stock_binary'], str(previous_stock))
        self.assertIn(str(site / 'pyte/__init__.py'), rows['compat']['input']['executed'])
        self.assertNotIn(str(site / 'pyte/escape.py'), rows['compat']['input']['executed'])
        self.assertIn(str(site / 'pyte/escape.py'), fixtures.protocol.load(root / 'compat-preparation.json')['closure'])
        for name in compat.RECOVERY_FIXTURES:
            self.assertIn(str(source / name), fixtures.protocol.load(root / 'compat-preparation.json')['closure'])
            self.assertIn(str(source / name), rows['compat']['input']['executed'])
        self.assertEqual(rows['captures']['cleanup']['outcome'], 'clean')
        (site / 'pyte/escape.py').write_text('CHANGED_SUBMODULE = True\n')
        with patch.object(workflow.native, 'verify', return_value={}):
            with self.assertRaisesRegex(ValueError, 'input closure changed'):
                workflow.verify_attempt(root)

    def test_cleanup_failure_records_unknown_without_replacing_original_audit(self):
        workflow = self.workflow()
        report = self.root / 'cleanup.json'
        compat.write(report, {'outcome': 'blocked', 'owned_remaining': [123], 'unknown': []})
        before = report.read_bytes()
        def fail():
            raise RuntimeError('Audit unresolved')
        result = workflow.cleanup_record(self.root, report, fail)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(result['unknown'], 1)
        self.assertEqual(report.read_bytes(), before)
        self.assertTrue((self.root / result['report']).is_file())

    def test_ap_dependency_origin_escape_is_rejected(self):
        workflow = self.workflow()
        modules = self.root / 'node_modules'
        modules.mkdir()
        (modules / 'dependency.js').write_text('export default 1')
        self.assertIn(str(modules / 'dependency.js'), workflow.dependency_files(modules))
        (modules / 'escape.js').symlink_to(self.stock)
        with self.assertRaisesRegex(ValueError, 'origin escapes'):
            workflow.dependency_files(modules)
        with self.assertRaisesRegex(ValueError, 'outside the native import'):
            workflow.prepare_ap(self.root, self.root / 'ap-output', {})
        self.assertFalse((self.root / 'ap-output').exists())

    def short_scope(self):
        workflow = self.workflow()
        plan, profile, config = self.inputs()
        prepared = self.prepare(plan, profile, config)
        scope = Path(prepared['scope'])
        short = self.short / 'ab'
        short.mkdir(mode=0o700)
        info = short.stat()
        state = dict(path=str(short), device=info.st_dev, inode=info.st_ino, state='allocated', uid=info.st_uid,
                     boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        compat.write(scope / 'short-state.json', state)
        baseline = scope / 'unreadable-baseline.json'
        compat.write(baseline, {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'processes': {}})
        compat.write(scope / 'short-binding.json', dict(path=str(short), device=info.st_dev, inode=info.st_ino,
                                                       baseline_sha256=compat.digest(baseline)))
        return workflow, scope, short, state

    def test_compat_short_cleanup_preserves_swap_at_atomic_move(self):
        workflow, scope, short, _ = self.short_scope()
        (short / 'original.txt').write_text('owned original')
        rename = workflow.ownership.os.rename
        def replace(source, destination, **kwargs):
            rename(short, self.short / 'original')
            short.mkdir(mode=0o700)
            (short / 'unrelated.txt').write_text('preserve other work')
            return rename(source, destination, **kwargs)
        audit = dict(passed=True, live_owned_processes=[], unknown_unreadable_same_user_processes=[])
        with patch.object(workflow.ownership, 'inspect_processes', return_value=audit), \
             patch.object(workflow.ownership.os, 'rename', side_effect=replace):
            with self.assertRaisesRegex(ValueError, 'preserved in quarantine'):
                workflow.audit_scope(scope, remove_short=True)
        intent = workflow.protocol.load(scope / 'socket-removal-intent.json')
        self.assertEqual((Path(intent['quarantine']) / 'owned/unrelated.txt').read_text(), 'preserve other work')
        self.assertEqual((self.short / 'original/original.txt').read_text(), 'owned original')
        self.assertFalse((scope / 'socket-removal.json').exists())

    def test_compat_short_cleanup_reuses_shared_proof_and_is_idempotent(self):
        workflow, scope, short, state = self.short_scope()
        (short / 'owned.txt').write_text('owned fixture')
        audit = dict(passed=True, live_owned_processes=[], unknown_unreadable_same_user_processes=[])
        with patch.object(workflow.ownership, 'inspect_processes', return_value=audit):
            result = workflow.audit_scope(scope, remove_short=True)
            again = workflow.audit_scope(scope, remove_short=True)
        self.assertTrue(result['short_removed'] and again['short_removed'])
        self.assertFalse(short.exists())
        expected = dict(path=str(short), dev=state['device'], ino=state['inode'], uid=state['uid'], boot_id=state['boot_id'])
        self.assertEqual(result['short_identity'], expected)
        self.assertEqual(workflow.protocol.load(scope / 'socket-removal.json'), {'identity': expected, 'removed': True})
        for name in ('socket-removal-intent.json', 'socket-quarantine.json', 'socket-moved.json', 'socket-deleted.json'):
            self.assertTrue((scope / name).is_file())

    def test_ap_composition_writes_external_receipt_with_distinct_executor_architecture(self):
        import test_release_protocol as fixtures
        workflow = self.workflow()
        fixture = fixtures.ReleaseProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        node = fixture.base / 'node'
        node.write_bytes(b'pinned replay Node')
        bundle = fixture.base / 'ap.cjs'
        bundle.write_bytes(b'released AP bundle')
        fixture.plan['compat']['ap_replay'] = {'source_commit': 'd' * 40,
            'installed_plugin_commit': 'e' * 40, 'bundle_sha256': compat.digest(bundle)}
        native_root = fixture.qualified(skip_ap=True)
        coordinator = fixture.base / 'coordinator'
        coordinator.mkdir()
        profile = copy.deepcopy(fixture.profile)
        profile.update(architecture='aarch64', triple=fixtures.protocol.TARGETS['aarch64'],
            source_root=str(fixture.base / 'ap-source'), system_tools={str(node): compat.digest(node)},
            ap_replay={'node': str(node), 'node_modules': str(fixture.base / 'ap-source/node_modules'),
                'installed_bundle': str(bundle), 'installed_registry': str(fixture.base / 'registry.json')})
        captures = fixtures.protocol.load(native_root / 'outputs/captures/report.json')
        context = dict(native_root=str(native_root), package_sha256=captures['package_sha256'],
            capture_manifest_sha256=captures['capture_manifest_sha256'], closure={str(node): fixtures.protocol.file_record(node)})
        for name, value in (('ap-preparation.json', context), ('ap-profile.json', profile), ('native-plan.json', fixture.plan),
                            ('native-captures.json', captures), ('native-capture-inventory.json', {}), ('ownership-baseline.json', {})):
            fixtures.protocol.write_once(coordinator / name, value)
        before = {path.relative_to(native_root).as_posix(): fixtures.protocol.file_record(path) for path in native_root.rglob('*') if path.is_file()}
        def execute(*args, **kwargs):
            for name, value in (('replay-report.json', {'passed': True, 'tests': 23}),
                                ('release-vitest.json', {'numPassedTests': 22}), ('observer-vitest.json', {'numPassedTests': 1}),
                                ('observed-classifier.json', {'passed': True, 'captures': list(range(20)), 'busy_pairs': [1, 2]})):
                fixtures.protocol.write_once(coordinator / name, value)
            return 0
        def cleanup(target, output):
            result = {'outcome': 'clean', 'owned_remaining': [], 'unknown': []}
            fixtures.protocol.write_once(output, result)
            return result
        with patch.object(workflow, 'verify_ap', return_value=(context, fixture.plan, profile)), \
             patch.object(workflow, 'bound_execution', return_value=('managed-ap-replay', 'c' * 64)), \
             patch.object(workflow.native, 'owned_run', side_effect=execute), \
             patch.object(workflow, 'cleanup_ap', side_effect=cleanup):
            report = workflow.run_ap(coordinator, fixture.base)
        self.assertEqual(report['architecture'], 'x86_64')
        sidecar = fixtures.protocol.load(coordinator / 'ap-receipt.json')
        self.assertEqual(sidecar['input']['execution']['architecture'], 'aarch64')
        self.assertEqual(sidecar['input']['native']['architecture'], 'x86_64')
        accepted = fixtures.protocol.accept_attempt(native_root, source_root=SCRIPTS.parent, ap_receipt=coordinator / 'ap-receipt.json')
        self.assertTrue(accepted['accepted'])
        after = {path.relative_to(native_root).as_posix(): fixtures.protocol.file_record(path) for path in native_root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_ap_preparation_accepts_real_readonly_exported_capture_projection(self):
        import test_release_protocol as fixtures
        workflow = self.workflow()
        fixture = fixtures.ReleaseProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.qualified(package_only=True)
        short_identity = fixture.allocate_compat_short(root)
        fixture.finish_compat_short(root, short_identity)
        fixture.receipt(root, 'compat', {}, ['outputs/compat-cleanup.json',
            'outputs/compat-root-cleanup.json', 'outputs/compat-combined-cleanup.json'])
        captures = root / 'compat/evidence/cursor-v2'
        captures.mkdir(parents=True)
        rows = []
        for index in range(20):
            directory = captures / ('state-' + str(index))
            directory.mkdir()
            screen, look = directory / 'screen.txt', directory / 'look.txt'
            screen.write_text('native captured screen\n')
            look.write_text('0 0 40 100 1\n')
            screen.chmod(0o600)
            look.chmod(0o600)
            rows.append({'name': directory.name, 'expected': 'idle',
                'screen_sha256': compat.digest(screen), 'look_sha256': compat.digest(look)})
        fixture.put(root, 'compat/evidence/cursor-v2/manifest.json', {'candidate_acceptance': True,
            'capture_complete': True, 'captures': rows})
        fixture.put(root, 'compat/evidence/cursor-v2/integrity.json', {'passed': True})
        package = fixtures.protocol.load(root / 'outputs/package/report.json')
        package_sha = compat.digest(root / package['archive'])
        fixture.put(root, 'compat/evidence/cursor-v2/package-binding.json', {'accepted': True, 'native': True,
            'source': fixture.plan['plus_source_commit'], 'archive_sha256': package_sha,
            'capture_manifest_sha256': compat.digest(captures / 'manifest.json'),
            'integrity_sha256': compat.digest(captures / 'integrity.json')})
        raw = [path for path in captures.rglob('*') if path.is_file()]
        for path in raw:
            path.chmod(0o600)
        inventory = 'compat/capture-manifest.json'
        fixture.put(root, inventory, {'architecture': 'x86_64', 'package_sha256': package_sha,
            'files': {path.relative_to(root).as_posix(): fixtures.protocol.file_record(path) for path in raw}})
        fixture.receipt(root, 'captures', {'package_sha256': package_sha, 'capture_manifest': inventory,
            'capture_manifest_sha256': compat.digest(root / inventory)},
            [inventory, *(path.relative_to(root).as_posix() for path in raw)])
        bundle = fixture.base / 'native-captures.tar.gz'
        exported = fixture.export(root, bundle)
        imported = fixtures.protocol.import_attempt(bundle, fixture.base / 'readonly-native', expected_sha256=exported['sha256'])
        imported_screen = imported / 'compat/evidence/cursor-v2/state-0/screen.txt'
        self.assertEqual(imported_screen.stat().st_mode & 0o777, 0o400)
        before = {path.relative_to(imported).as_posix(): fixtures.protocol.file_record(path) for path in imported.rglob('*') if path.is_file()}
        profile = copy.deepcopy(fixture.profile)
        profile['python']['path'] = sys.executable
        source = fixture.base / 'AP-source'
        (source / 'node_modules').mkdir(parents=True)
        executor = fixture.base / 'AP-executor'
        (executor / 'bin').mkdir(parents=True)
        (executor / 'bin/codex-plus-runtime').write_bytes(b'pinned executor')
        profile.update(source_root=str(source), executor_package_root=str(executor),
            ap_replay={'node': str(self.stock), 'node_modules': str(source / 'node_modules'),
                'installed_bundle': str(self.stock), 'installed_registry': str(self.helper)})
        runtime = fixture.base / 'original-tooling'
        shutil.copytree(SCRIPTS, runtime / 'scripts')
        profile['tooling_root'] = str(runtime)
        coordinator = fixture.base / 'AP-evidence'
        with patch.object(workflow, 'ap_profile', return_value={'source': {'files': {}}}):
            proposal = workflow.prepare_ap(imported, coordinator, profile)
            self.assertEqual(proposal['argv'][3], 'run-ap')
            self.assertEqual(proposal['argv'][2], str(runtime / 'scripts/release_compat.py'))
            self.assertEqual(proposal['external_dependencies'][0]['precondition']['argv'][2], proposal['argv'][2])
            self.assertEqual(proposal['cleanup']['argv'][2], proposal['argv'][2])
            context = workflow.protocol.load(coordinator / 'ap-preparation.json')
            self.assertIn(str(runtime / 'scripts/release_recipes/compat/workflow.py'), context['closure'])
            self.assertNotIn(str(SCRIPTS / 'release_protocol.py'), context['closure'])
            original_workflow = workflow.native.module(runtime / 'scripts/release_recipes/compat/workflow.py')
            with patch.object(original_workflow, 'ap_profile', return_value={'source': {'files': {}}}):
                self.assertEqual(original_workflow.verify_ap(coordinator)[0], context)
            self.assertEqual(compat.digest(coordinator / 'captures/state-0/screen.txt'), compat.digest(imported_screen))
            after = {path.relative_to(imported).as_posix(): fixtures.protocol.file_record(path) for path in imported.rglob('*') if path.is_file()}
            self.assertEqual(before, after)
            imported_screen.chmod(0o600)
            imported_screen.write_text('tampered raw capture')
            with self.assertRaisesRegex(ValueError, 'evidence changed'):
                workflow.prepare_ap(imported, fixture.base / 'AP-rejected', profile)
            self.assertFalse((fixture.base / 'AP-rejected').exists())

    def test_ap_leaf_uses_checked_in_replay_files_and_reconciles_expected_counts(self):
        workflow = self.workflow()
        coordinator = self.root / 'ap-coordinator'
        coordinator.mkdir()
        source = self.root / 'ap-source'
        for name in ('src/commands/nudge.ts', 'src/core/tmux.ts'):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('AP pure export')
        modules = source / 'node_modules'
        modules.mkdir()
        profile = {'python': {'libpython': None}, 'source_root': str(source),
                   'ap_replay': {'node': str(self.stock), 'node_modules': str(modules)}}
        plan = {'compat': {'ap_version': '0.7.35', 'ap_replay': {'source_commit': 'd' * 40,
            'installed_plugin_commit': 'e' * 40, 'bundle_sha256': 'f' * 64}}}
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            output = Path(argv[-1])
            count = 22 if output.name == 'release-vitest.json' else 1
            compat.write(output, dict(success=True, numPassedTests=count, numFailedTests=0, numPendingTests=0, numTodoTests=0))
            if count == 1:
                compat.write(coordinator / 'observed-classifier.json', dict(passed=True, captures=list(range(20)), busy_pairs=[1, 2]))
            return SimpleNamespace(returncode=0)
        with patch.object(workflow, 'verify_ap', return_value=({}, plan, profile)), \
             patch.object(workflow.subprocess, 'run', side_effect=execute):
            result = workflow.replay_ap(coordinator)
        self.assertEqual(result, {'passed': True, 'tests': 23, 'captures': 20, 'busy_pairs': 2})
        self.assertEqual(Path(calls[0][3]), source / 'tests/codex-plus-footer.test.ts')
        self.assertEqual(Path(calls[1][3]), compat.RECIPE / 'ap-release-replay-observer.test.ts')
        self.assertFalse(any(path.suffix in ('.py', '.js', '.ts') for path in coordinator.rglob('*')))

    def test_ap_profile_checks_real_bundle_registry_and_module_prerequisites(self):
        import test_release_protocol as fixtures
        workflow = self.workflow()
        fixture = fixtures.ReleaseProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = self.root / 'ap-source'
        (source / 'dist').mkdir(parents=True)
        (source / 'dist/ap.cjs').write_bytes(b'released bundle')
        (source / 'package.json').write_text('{"version":"0.7.35"}')
        (source / '.gitignore').write_text('node_modules/\n')
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'AP source'], check=True)
        source_commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        modules = source / 'node_modules'
        (modules / 'vitest/dist').mkdir(parents=True)
        (modules / 'vitest/vitest.mjs').write_text('pinned Vitest entry')
        (modules / 'vitest/dist/index.js').write_text('pinned Vitest exports')
        installed = self.root / 'installed-ap/dist/ap.cjs'
        installed.parent.mkdir(parents=True)
        installed.write_bytes((source / 'dist/ap.cjs').read_bytes())
        registry = self.root / 'registry.json'
        compat.write(registry, {'plugins': {'ap@fixture': [{'installPath': str(installed.parent.parent),
            'version': '0.7.35', 'gitCommitSha': 'e' * 40}]}})
        plan = fixture.plan
        plan['compat']['ap_replay'] = {'source_commit': source_commit, 'installed_plugin_commit': 'e' * 40,
                                      'bundle_sha256': compat.digest(installed)}
        profile = fixture.profile
        libraries = {str(Path(line.split()[-1]).resolve()) for line in Path('/proc/self/maps').read_text().splitlines()
                     if 'libpython' in line and line.split()[-1].startswith('/')}
        profile.update(hostname=platform.node(), architecture=platform.machine(), triple=fixtures.protocol.TARGETS[platform.machine()],
            source_root=str(source), system_tools={str(self.stock): compat.digest(self.stock)},
            ap_replay={'node': str(self.stock), 'node_modules': str(modules), 'installed_bundle': str(installed), 'installed_registry': str(registry)})
        profile['python'].update(path=sys.executable, version=sys.version.split()[0], libpython=next(iter(libraries), None))
        checkout = workflow.native.checkout_identity
        def identity(path):
            return {'head': plan['tooling_commit']} if Path(path).resolve() == SCRIPTS.parent else checkout(path)
        with patch.object(workflow.native, 'checkout_identity', side_effect=identity):
            self.assertEqual(workflow.ap_profile(profile, plan)['source']['head'], source_commit)
            installed.write_bytes(b'wrong released bundle')
            with self.assertRaisesRegex(ValueError, 'bundle differs'):
                workflow.ap_profile(profile, plan)
            installed.write_bytes((source / 'dist/ap.cjs').read_bytes())
            (modules / 'vitest/vitest.mjs').unlink()
            with self.assertRaisesRegex(ValueError, 'dependencies are unavailable'):
                workflow.ap_profile(profile, plan)

    def test_guard_journal_pairs_only_observed_terminal_outcomes(self):
        for outcome in (0, 1, -9, subprocess.TimeoutExpired(['command'], 1)):
            with self.subTest(outcome=outcome):
                scope = self.root / ('guard-' + str(len(list(self.root.glob('guard-*')))))
                scope.mkdir()
                module = self.guard_module(scope)
                identity = dict(start_ticks='1234', cmdline_sha256='a' * 64)
                cleanup = SimpleNamespace(process_identity=lambda proc: identity)
                reader, writer = Mock(), Mock()
                writer.recv.return_value = b'\x01'
                guard = Mock(pid=999999)
                journal = scope / 'owned-process-identities.jsonl'
                def wait(**kwargs):
                    rows = [json.loads(row) for row in journal.read_text().splitlines()]
                    self.assertEqual(rows[0]['event'], 'started')
                    self.assertEqual(rows[0]['argv'], ['command', '--fixture'])
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome
                guard.wait.side_effect = wait
                with patch.dict(sys.modules, permission_cleanup=cleanup), patch.object(module.socket, 'socketpair', return_value=(reader, writer)), patch.object(module.subprocess, 'Popen', return_value=guard):
                    if isinstance(outcome, BaseException):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            module.run(['command', '--fixture'], cwd=scope, env={}, stdout=None, timeout=1)
                    else:
                        self.assertEqual(module.run(['command', '--fixture'], cwd=scope, env={}, stdout=None, timeout=1).returncode, outcome)
                rows = [json.loads(row) for row in journal.read_text().splitlines()]
                if isinstance(outcome, int) and outcome >= 0:
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(rows[1], dict(rows[0], event='finished', exit_code=outcome))
                else:
                    self.assertEqual(len(rows), 1)

    def test_scope_requires_completed_event_journal_and_keeps_positive_ownership_blocking(self):
        scope = self.root / 'scope'
        scope.mkdir(mode=0o700)
        short = self.short / 'ab'
        short.mkdir(mode=0o700)
        compat.write(scope / 'unreadable-baseline.json', {'processes': {}})
        info = short.stat()
        compat.write(scope / 'short-binding.json', dict(path=str(short), device=info.st_dev, inode=info.st_ino,
            baseline_sha256=compat.digest(scope / 'unreadable-baseline.json')))
        context = SimpleNamespace(CONFIG={'short_root': str(self.short)}, DATA=scope, RECIPE=compat.RECIPE)
        spec = importlib.util.spec_from_file_location('journal_scope', compat.RECIPE / 'fixture_scope.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, recipe_context=context):
            spec.loader.exec_module(module)
        for records, completed, owned, expected in (([{'event': 'started'}], False, False, False),
                ([{'event': 'started'}, {'event': 'finished'}], True, False, True),
                ([{'event': 'started'}, {'event': 'finished'}], True, True, False),
                ([{'pid': 123}], False, False, True)):
            journal = scope / 'owned-process-identities.jsonl'
            journal.write_text(''.join(json.dumps(row) + '\n' for row in records))
            audit = Mock(return_value={'passed': not owned, 'live_owned_processes': [123] if owned else []})
            cleanup = SimpleNamespace(inspect_processes=audit, verify_guard_journal=Mock(return_value=completed))
            with patch.dict(sys.modules, permission_cleanup=cleanup):
                result = module.inspect_scope()
            self.assertEqual(result['passed'], expected)
            self.assertEqual(audit.call_args.kwargs['completed_guards'], completed)
            self.assertEqual(audit.call_args.kwargs['recorded'], records)


if __name__ == '__main__':
    unittest.main()
