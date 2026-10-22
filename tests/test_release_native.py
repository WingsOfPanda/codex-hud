"""Real failures at the native runner boundary, with expensive bodies stubbed."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_release_protocol as core_tests

ROOT, protocol = core_tests.ROOT, core_tests.protocol

sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('release_native', ROOT / 'scripts/release_native.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeTests(unittest.TestCase):
    def setUp(self):
        fixture = core_tests.ReleaseProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.base, self.profile, self.plan = fixture.base, fixture.profile, fixture.plan
        self.profile['python']['path'] = str(Path(sys.executable).resolve())
        self.profile['python']['version'] = sys.version.split()[0]
        self.profile['native_build'] = {'upstream_repository': str(self.base / 'upstream'),
            'cargo_home': str(self.base / 'cargo'), 'cargo_target_dir': str(self.base / 'target'),
            'v8_cache': str(self.base / 'v8'), 'license_cache': str(self.base / 'licenses'),
            'cargo_jobs': 3, 'test_threads': 2, 'official_binary': str(self.base / 'official'),
            'official_sha256': 'd' * 64}
        self.profile['system_tools'] = {str(Path(path).resolve()): native.sha(Path(path).resolve())
                                        for path in native.SYSTEM_TOOLS}
        self.profile['short_root'] = str(self.base / 's')
        Path(self.profile['short_root']).mkdir()
        self.observed = {name: copy.deepcopy(self.profile[name]) for name in
                         ('hostname', 'architecture', 'toolchain', 'python', 'executor_package_root')}
        self.observed.update(plus_source_commit=self.plan['plus_source_commit'], tooling_commit=self.plan['tooling_commit'])
        self.plan['targets']['x86_64']['profile_sha256'] = protocol.digest(self.profile)
        self.hook = {'argv': [str(Path(sys.executable).resolve()), '-B', '-c',
            'import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps('
            '{"outcome":"clean","owned_remaining":[],"unknown":[]}))', '{report}'],
            'cwd': str(ROOT), 'env': {}, 'deadline_seconds': 10}

    def attempt(self):
        return protocol.init_attempt(self.plan, self.profile, ROOT, observed=self.observed, rehearsal=True)

    def setup_runner(self, root):
        context = {'closure': {str(ROOT / 'scripts/release_native.py'):
                              protocol.file_record(ROOT / 'scripts/release_native.py')}, 'cleanup': self.hook}
        protocol.write_once(root / 'native-preparation.json', context)
        return context

    def fake_stage(self, argv, *, stdout, **kwargs):
        root = Path(argv[argv.index('--attempt') + 1])
        name = argv[argv.index('--stage') + 1]
        stdout.write(b'stage output\n')
        protocol.write_once(root / 'outputs' / (name + '.json'),
                            {'architecture': 'x86_64', 'plus_source_commit': self.plan['plus_source_commit']})
        return 0

    def test_path_derivation_and_child_only_marker(self):
        root = self.attempt()
        context = native.Context(root, 'runtime')
        self.assertTrue(context.UPSTREAM.is_relative_to(root))
        self.assertEqual(context.PACKAGE.name, 'codex-plus-' + self.plan['release'] + '-' + self.profile['triple'])
        self.assertEqual(context.ENV['CODEX_PLUS_RELEASE_ATTEMPT'], protocol.load(root / 'attempt.json')['attempt'])
        self.assertNotIn('CODEX_PLUS_RELEASE_ATTEMPT', native.python_env(self.profile))
        self.assertEqual(context.ENV['CARGO_BUILD_JOBS'], '3')
        self.assertEqual(context.ENV['RUST_TEST_THREADS'], '2')

    def test_scoped_loader_comes_from_profile(self):
        self.profile['python']['libpython'] = '/private/python/lib/libpython3.12.so.1.0'
        self.assertEqual(native.python_env(self.profile)['LD_LIBRARY_PATH'], '/private/python/lib')
        self.profile['python']['libpython'] = None
        self.assertNotIn('LD_LIBRARY_PATH', native.python_env(self.profile))

    def test_checked_in_cargo_adapter_handles_notice_generator_version(self):
        env = {'PATH': '/usr/bin:/bin', 'CODEX_PLUS_RELEASE_CARGO': '/usr/bin/printf',
               'CODEX_PLUS_RELEASE_RUST_VERSION': '1.99.1'}
        result = subprocess.check_output([str(native.RECIPE / 'cargo'), '+1.99.1', '%s', 'metadata'],
                                         env=env, text=True)
        self.assertEqual(result, 'metadata')
        self.assertNotIn('CODEX_PLUS_RELEASE_CARGO', os.environ)

    def test_prepare_missing_input_refuses_before_any_process_launch(self):
        root = self.attempt()
        with patch.object(native, 'observe') as observe, patch.object(native, 'owned_run') as execute:
            with self.assertRaisesRegex(ValueError, 'Missing native input'):
                native.prepare(root, self.hook)
            observe.assert_not_called()
            execute.assert_not_called()
        self.assertFalse((root / 'job-proposal.json').exists())

    def test_prepare_freezes_normal_proposal_without_launch(self):
        build = self.profile['native_build']
        for name in ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache', 'license_cache'):
            Path(build[name]).mkdir()
        Path(build['official_binary']).write_text('official')
        build['official_sha256'] = native.sha(build['official_binary'])
        self.plan['targets']['x86_64']['profile_sha256'] = protocol.digest(self.profile)
        root = self.attempt()
        closure = {str(ROOT / 'scripts/release_native.py'): protocol.file_record(ROOT / 'scripts/release_native.py')}
        with patch.object(native, 'observe', return_value=(self.observed, {'version': 'test'})), \
             patch.object(native, 'checkout_identity', return_value={'head': 'fixture'}), \
             patch.object(native, 'input_closure', return_value=closure), \
             patch.object(native, 'module') as ownership, \
             patch.object(native, 'owned_run') as execute:
            ownership.return_value.capture_baseline.return_value = {'boot_id': 'fixture', 'processes': {}}
            proposal = native.prepare(root, self.hook, rehearsal_failure='frontend')
        execute.assert_not_called()
        self.assertIn('{execution}', proposal['argv'])
        self.assertEqual(proposal['cwd'], str(ROOT))
        self.assertNotIn('CODEX_PLUS_RELEASE_ATTEMPT', proposal['env'])
        self.assertIn('cleanup', proposal)
        self.assertTrue((root / 'ownership-baseline.json').is_file())
        self.assertEqual(protocol.load(root / 'job-proposal.json'), proposal)
        self.assertEqual(protocol.load(root / 'native-preparation.json')['rehearsal_failure'], 'frontend')

    def test_each_stage_failure_has_immutable_receipt_and_stops(self):
        for failed_stage in native.STAGES:
            with self.subTest(stage=failed_stage):
                root = self.attempt()
                context = self.setup_runner(root)
                invoked = []
                def fail(argv, **kwargs):
                    stage = argv[argv.index('--stage') + 1]
                    invoked.append(stage)
                    code = self.fake_stage(argv, **kwargs)
                    return 17 if stage == failed_stage else code
                with patch.object(native, 'verify', return_value=context), \
                     patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
                     patch.object(native, 'owned_run', side_effect=fail):
                    with self.assertRaisesRegex(ValueError, 'preserve logs'):
                        native.run(root, self.base / 'execution')
                    failed = root / 'receipts' / (failed_stage + '.json')
                    before = failed.read_bytes()
                    receipt = protocol.load(failed)
                    self.assertEqual(receipt['input']['execution']['exit_code'], 17)
                    self.assertEqual(receipt['input']['execution']['outcome'], 'failed')
                    self.assertEqual(invoked, list(native.STAGES[:native.STAGES.index(failed_stage) + 1]))
                    with self.assertRaisesRegex(ValueError, 'immutable'):
                        native.run(root, self.base / 'execution')
                    self.assertEqual(before, failed.read_bytes())

    def test_success_resume_preserves_passed_outputs_and_tamper_refuses(self):
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=self.fake_stage) as execute:
            state = native.run(root, self.base / 'execution')
            self.assertFalse(state['refused'])
            self.assertEqual(execute.call_count, len(native.STAGES))
            snapshot = {path: path.read_bytes() for path in (root / 'receipts').glob('*.json')}
            native.run(root, self.base / 'execution')
            self.assertEqual(execute.call_count, len(native.STAGES))
            self.assertEqual(snapshot, {path: path.read_bytes() for path in snapshot})
            target = root / 'outputs/preflight.json'
            target.chmod(0o600)
            target.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                native.run(root, self.base / 'execution')
            self.assertEqual(execute.call_count, len(native.STAGES))

    def test_interrupted_stage_is_unknown_and_not_replayed(self):
        root = self.attempt()
        context = self.setup_runner(root)
        protocol.write_once(root / 'native-starts/preflight.json', {'job_id': 'old-job'})
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run') as execute:
            with self.assertRaisesRegex(ValueError, 'unknown outcome'):
                native.run(root, self.base / 'execution')
            execute.assert_not_called()

    def test_missing_stage_report_cannot_pass_on_zero_exit(self):
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', return_value=0):
            with self.assertRaisesRegex(ValueError, 'failed'):
                native.run(root, self.base / 'execution')
        self.assertEqual(protocol.load(root / 'receipts/preflight.json')['input']['execution']['outcome'], 'failed')

    def test_cleanup_failure_remains_unknown_and_preserves_report(self):
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=self.fake_stage), \
             patch.object(native, 'cleanup', side_effect=RuntimeError('lost cleanup')):
            with self.assertRaisesRegex(ValueError, 'preserve logs'):
                native.run(root, self.base / 'execution')
        row = protocol.load(root / 'receipts/preflight.json')
        self.assertEqual(row['cleanup']['outcome'], 'unknown')
        self.assertEqual(row['cleanup']['unknown'], 1)
        self.assertTrue((root / row['cleanup']['report']).exists())

    def test_blocked_cleanup_retains_raw_owned_counts_without_forging_clean(self):
        root = self.attempt()
        context = self.setup_runner(root)
        def blocked(root, report):
            protocol.write_once(report, {'outcome': 'blocked', 'owned_remaining': [{'pid': 4}], 'unknown': []})
            raise ValueError('live owned process')
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=self.fake_stage), \
             patch.object(native, 'cleanup', side_effect=blocked):
            with self.assertRaisesRegex(ValueError, 'preserve logs'):
                native.run(root, self.base / 'execution')
        row = protocol.load(root / 'receipts/preflight.json')
        self.assertEqual(row['cleanup']['owned_remaining'], 1)
        self.assertEqual(row['cleanup']['outcome'], 'unknown')
        self.assertEqual(protocol.load(root / 'outputs/preflight-cleanup.json')['outcome'], 'blocked')
        self.assertEqual(protocol.status(root)['refused'], ['preflight'])

    def test_supervisor_unknown_outcome_is_never_replayed(self):
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=subprocess.TimeoutExpired(['stage'], 10)) as execute:
            with self.assertRaisesRegex(ValueError, 'unknown'):
                native.run(root, self.base / 'execution')
            row = protocol.load(root / 'receipts/preflight.json')
            self.assertIsNone(row['input']['execution']['exit_code'])
            self.assertEqual(row['input']['execution']['outcome'], 'unknown')
            with self.assertRaisesRegex(ValueError, 'immutable'):
                native.run(root, self.base / 'execution')
            self.assertEqual(execute.call_count, 1)

    def test_command_failure_preserves_actual_exit_and_logs(self):
        root = self.attempt()
        (root / 'logs').mkdir()
        c = native.Context(root, 'preflight')
        with self.assertRaisesRegex(ValueError, 'failed \\(9\\)'):
            c.run('fails', [sys.executable, '-c', 'print("failure detail");raise SystemExit(9)'])
        result = protocol.load(root / 'evidence/commands/fails.json')
        self.assertEqual(result['exit_code'], 9)
        self.assertEqual(result['executable'], str(Path(sys.executable).resolve()))
        self.assertIn('failure detail', (root / 'logs/fails.log').read_text())

    def test_direct_run_requires_exact_executor_package_and_command(self):
        root = self.attempt()
        helper = self.base / 'executor/bin/codex-plus-runtime'
        helper.parent.mkdir(parents=True)
        helper.write_text('installed helper')
        profile = dict(self.profile, executor_package_root=str(helper.parent.parent))
        directory = self.base / 'execution-id'
        directory.mkdir()
        state = {'id': directory.name, 'state': 'running', 'package': profile['executor_package_root'],
                 'runtime': {'sha256': native.sha(helper)},
                 'workload': native.process_identity(os.getpid()),
                 'manifest': {'command': {'spec': {'argv': [sys.executable, '-B', native.__file__,
                     'run', '--attempt', str(root), '--execution', str(directory)]}}}}
        path = directory / 'state.json'
        path.write_text(json.dumps(state))
        self.assertEqual(native.execution_identity(directory, root, profile)[0], directory.name)
        state['workload']['start_ticks'] += 1
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'does not own this process'):
            native.execution_identity(directory, root, profile)
        state['workload'] = native.process_identity(os.getpid())
        state['package'] = '/another/package'
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            native.execution_identity(directory, root, profile)

    def test_alternate_index_bytes_and_mtime_survive_verify(self):
        source = self.base / 'repository'
        source.mkdir()
        env = os.environ | {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        def git(*args):
            return subprocess.check_output(['/usr/bin/git', '-C', str(source), *args], env=env,
                                           stderr=subprocess.STDOUT)
        git('init')
        (source / 'file').write_text('source\n')
        git('add', 'file')
        git('-c', 'user.email=fixture@example.test', '-c', 'user.name=Fixture', 'commit', '-m', 'fixture')
        index = source / '.git/index'
        alternate = self.base / 'sealed-alternate.index'
        shutil.copy2(index, alternate)
        stat = (source / 'file').stat()
        os.utime(source / 'file', ns=(stat.st_atime_ns, stat.st_mtime_ns - 1000000))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (index, alternate)}
        with patch.dict(os.environ, {'GIT_INDEX_FILE': str(alternate)}):
            first = native.checkout_identity(source)
            second = native.checkout_identity(source)
        self.assertEqual(first, second)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_owned_supervisor_actual_exit_and_journal(self):
        helper = Path('/home/liupan/Aerius/codex-plus/.build/codex-plus-runtime')
        if not helper.is_file():
            self.skipTest('No local built supervisor; composed proof remains required')
        root = self.attempt()
        with (root / 'owned.log').open('wb') as stream:
            code = native.owned_run([sys.executable, '-c', 'print("owned child");raise SystemExit(7)'],
                helper=helper, python=sys.executable, cwd=root, env=native.python_env(self.profile),
                stdout=stream, timeout=10)
        self.assertEqual(code, 7)
        self.assertIn('owned child', (root / 'owned.log').read_text())
        rows = [json.loads(line) for line in (root / 'process-journal.jsonl').read_text().splitlines()]
        self.assertEqual([row['event'] for row in rows], ['started', 'finished'])
        self.assertEqual(rows[0]['identity'], rows[1]['identity'])
        self.assertEqual(rows[1]['exit_code'], 7)

    def test_recipe_is_version_data_and_keeps_bwrap_before_frontend(self):
        recipe = native.module(native.RECIPE / 'build.py')
        source = (native.RECIPE / 'build.py').read_text()
        self.assertLess(source.index("c.run('bwrap-build'"), source.index("c.run('frontend-build'"))
        for text in ('release047', 'release030', 'd25cc24', '/data/liupan'):
            self.assertNotIn(text, '\n'.join(path.read_text() for path in native.RECIPE.glob('*.py')))
        root = self.attempt()
        old = native.Context(root, 'frontend')
        next_plan = copy.deepcopy(self.plan)
        next_plan['release'] = '0.163.0-plus.0.4.8'
        next_plan['plus_source_commit'] = 'e' * 40
        with patch.object(native.protocol, 'read_attempt', return_value=(protocol.load(root / 'attempt.json'), next_plan, self.profile)):
            new = native.Context(root, 'frontend')
        self.assertNotEqual(old.PACKAGE, new.PACKAGE)
        self.assertEqual(old.CARGO, new.CARGO)
        self.assertEqual(len(recipe.FILTERS), 8)

    def test_frontend_composition_uses_profile_paths_and_real_bwrap_dependency(self):
        root = self.attempt()
        (root / 'native').mkdir()
        c = native.Context(root, 'frontend')
        calls = []
        def execute(name, argv, **kwargs):
            calls.append((name, list(map(str, argv)), dict(c.ENV)))
            if name == 'v8-resolve':
                return json.dumps({'RUSTY_V8_ARCHIVE': str(c.V8_CACHE / 'archive'),
                                   'RUSTY_V8_SRC_BINDING_PATH': str(c.V8_CACHE / 'binding')})
            return ''
        def install(name):
            (c.NATIVE / name).write_text('native artifact ' + name)
        c.run = execute
        c.install_native = install
        c.cargo_timing = lambda name: None
        c.digest = lambda path: 'a' * 64
        native.module(native.RECIPE / 'build.py').frontend(c)
        names = [name for name, _, _ in calls]
        self.assertLess(names.index('bwrap-build'), names.index('frontend-build'))
        self.assertEqual(calls[0][1][-1], str(root / 'upstream'))
        frontend = next(row for row in calls if row[0] == 'frontend-build')
        self.assertIn(str(c.CARGO), frontend[1])
        self.assertEqual(frontend[1][frontend[1].index('--jobs') + 1], '3')
        self.assertEqual(frontend[2]['CODEX_BWRAP_SHA256'], 'a' * 64)
        self.assertTrue(frontend[2]['RUSTY_V8_ARCHIVE'].startswith(str(c.V8_CACHE)))


if __name__ == '__main__':
    unittest.main()
