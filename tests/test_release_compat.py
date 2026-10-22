"""Cheap recipe binding checks; native compatibility remains a separate gate."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('release_compat', SCRIPTS / 'release_compat.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


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
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'source')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.package = self.root / 'package'
        self.package.mkdir()
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
                fixture_files={'fixture.py': compat.digest(self.source / 'fixture.py')}, ap_version='0.7.35'))
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
for filename in ('verify-mixes.py','archive-identity.py','run-native-compat-v5.py','old-tui-root-result-v5.py','frontend_contract.py'):
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

    def test_coordinator_reports_bind_dedicated_results_and_native_capture_bytes(self):
        plan, profile, config = self.inputs()
        prepared = self.prepare(plan, profile, config)
        scope = Path(prepared['scope'])
        bound = json.loads((scope / 'native.config.json').read_text())
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
            stages=[dict(name=name, passed=True, exit_code=0) for name in compat.GATES])
        (evidence / 'report.json').write_text(json.dumps(raw))
        result = compat.export_reports(scope)
        report = json.loads(Path(result['compat']).read_text())
        self.assertEqual(set(report['checks']), set(compat.GATES))
        self.assertIn('compat/evidence/rollback/gate.json', report['checks']['quiet-rollback']['evidence'])
        inventory = json.loads(Path(result['capture_manifest']).read_text())
        self.assertEqual(inventory['files']['compat/evidence/cursor-v2/screen.txt']['sha256'], compat.digest(captures / 'screen.txt'))
        (evidence / 'rollback/gate.json').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'dedicated report changed'):
            compat.export_reports(scope)

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


if __name__ == '__main__':
    unittest.main()
