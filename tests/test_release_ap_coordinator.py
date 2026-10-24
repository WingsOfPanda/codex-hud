"""The coordinator composes only an actually successful, bound replay leaf."""
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_release_protocol as fixtures

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('release_ap_coordinator', SCRIPTS / 'release_ap_coordinator.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
p = adapter.protocol


class Coordinator(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ReleaseProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.base = fixture.base
        self.source = self.base / 'ap-source'
        self.tooling = self.base / 'original-tooling'
        self.recipe = self.tooling / 'scripts/release_recipes/compat'
        for root, names in ((self.source, ('src/commands/nudge.ts', 'src/core/tmux.ts',
                    'tests/codex-plus-footer.test.ts', 'node_modules/vitest/vitest.mjs')),
                (self.recipe, ('ap-release-replay-observer.test.ts', 'vitest.replay.config.ts'))):
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('pinned source\n')
        self.node = self.base / 'node'
        self.node.write_bytes(b'pinned Node')
        self.bundle = self.base / 'ap.cjs'
        self.bundle.write_bytes(b'released AP')
        fixture.plan['compat']['ap_replay'] = {'source_commit': 'd' * 40,
            'installed_plugin_commit': 'e' * 40, 'bundle_sha256': adapter.native.sha(self.bundle)}
        self.native = fixture.qualified(skip_ap=True)
        self.plan = fixture.plan
        self.root = self.base / 'coordinator'
        self.root.mkdir()
        self.profile = copy.deepcopy(fixture.profile)
        executor = self.base / 'executor'
        (executor / 'bin').mkdir(parents=True)
        (executor / 'bin/codex-plus-runtime').write_bytes(b'installed managed executor')
        self.profile.update(source_root=str(self.source), tooling_root=str(self.tooling),
            executor_package_root=str(executor), system_tools={str(self.node): adapter.native.sha(self.node)},
            python=dict(path=str(Path(sys.executable).resolve()), version='3.12.3', libpython=None),
            ap_replay={'node': str(self.node), 'node_modules': str(self.source / 'node_modules'),
                'installed_bundle': str(self.bundle), 'installed_registry': str(self.base / 'registry.json')})
        captures = p.load(self.native / 'outputs/captures/report.json')
        self.context = {'native_root': str(self.native), 'capture_data': {},
            'closure': {str(self.node): p.file_record(self.node)},
            'package_sha256': captures['package_sha256'], 'capture_manifest_sha256': captures['capture_manifest_sha256']}
        for name, value in (('ap-preparation.json', self.context), ('ap-profile.json', self.profile),
                ('native-plan.json', self.plan), ('native-captures.json', captures),
                ('native-capture-inventory.json', {}), ('ownership-baseline.json', {})):
            p.write_once(self.root / name, value)
        original = adapter.workflow.proposal(self.root, self.profile, 'run-ap', list(adapter.INPUTS), topic='Replay pinned AP captures')
        original.update(deadline_seconds=600, expected_duration_seconds=240)
        script = str(self.tooling / 'scripts/release_compat.py')
        check = {'argv': [self.profile['python']['path'], '-B', script, 'verify-ap', '--attempt', str(self.root)],
            'cwd': str(self.tooling), 'env': adapter.native.python_env(self.profile), 'deadline_seconds': 180}
        original['external_dependencies'] = [{'identity': 'Released AP bundle, clean source and exact module origins', 'precondition': check}]
        original['cleanup'] = dict(check, argv=[self.profile['python']['path'], '-B', script,
            'cleanup-ap', '--attempt', str(self.root), '--output', str(self.root / 'job-cleanup.json')])
        p.write_once(self.root / 'ap-job-proposal.json', original)
        self.original_bytes = (self.root / 'ap-job-proposal.json').read_bytes()
        self.addCleanup(patch.stopall)
        patch.object(adapter.workflow, 'verify_ap', return_value=(self.context, self.plan, self.profile)).start()
        patch.object(adapter, 'coordinator_identity', return_value={'commit': 'c' * 40,
            'files': {'release_protocol.py': {'sha256': '1' * 64}}}).start()

    def put(self, path, value):
        path.write_text(json.dumps(value, indent=3) + '\n')

    def frozen(self, command):
        spec = {key: command[key] for key in ('argv', 'cwd', 'env', 'deadline_seconds')}
        spec.update(env_refs={}, secret_refs={})
        executable = self.identity(Path(command['argv'][0]).resolve())
        info = Path(command['cwd']).stat()
        return dict(spec=spec, executable=executable, cwd_dev=info.st_dev, cwd_ino=info.st_ino, env_hashes={})

    def identity(self, path):
        info = path.stat()
        return dict(path=str(path), dev=info.st_dev, ino=info.st_ino, size=info.st_size, sha256=adapter.native.sha(path))

    def finish(self):
        self.proposal = adapter.prepare_leaf(self.root)
        self.directory = self.base / 'executions' / 'root-id' / 'job-id'
        self.directory.mkdir(parents=True)
        self.prepared_path = self.base / 'prepared-state.json'
        inputs = []
        for row in self.proposal['inputs']:
            source = Path(row['source'])
            target = self.directory / 'inputs' / row['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            inputs.append(dict(source=self.identity(source), path=row['path'], sha256=adapter.native.sha(source)))
        manifest = {key: self.proposal[key] for key in ('schema', 'topic', 'exclusive_keys',
            'expected_duration_seconds', 'cancel_grace_seconds')}
        actual = dict(self.proposal, argv=[arg.replace('{execution}', str(self.directory)) for arg in self.proposal['argv']])
        manifest.update(command=self.frozen(actual), cleanup=self.frozen(self.proposal['cleanup']), inputs=inputs,
            external_dependencies=[dict(identity=row['identity'], precondition=self.frozen(row['precondition']))
                for row in self.proposal['external_dependencies']])
        material = dict(schema=1, id='job-id', root='root-id', origin={}, manifest=manifest)
        review = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.prepared = dict(material, state='prepared', review_digest=review)
        self.put(self.prepared_path, self.prepared)
        self.put(self.directory / 'manifest.json', self.prepared)
        identity = dict(boot_id='fixture', pid=123, start_ticks=12, pgid=123, sid=42)
        self.result = dict(schema=1, id='job-id', root='root-id', state='completed', finished_at=123,
            exit_code=0, signal=None, workload_attempted=True, executor=identity, workload=identity,
            log_complete=True, cleanup=dict(status='completed', exit_code=0, signal=None, log_complete=True, error=None),
            termination=None, error=None)
        self.state = dict(self.prepared, state='completed', result=self.result, log_complete=True,
            output_closed=True, package=self.profile['executor_package_root'],
            runtime=self.identity(Path(self.profile['executor_package_root']) / 'bin/codex-plus-runtime'),
            executor=identity, workload=identity, error=None, resolution=None)
        self.save_job()
        self.put(self.directory / 'stdout.log', dict(passed=True, tests=23, captures=20, busy_pairs=2))
        (self.directory / 'cleanup.log').write_bytes(b'original cleanup output\n')
        (self.root / 'ap-home').mkdir()
        for label, count in (('release', 22), ('observer', 1)):
            self.put(self.root / (label + '-vitest.json'), dict(success=True, numPassedTests=count,
                numFailedTests=0, numPendingTests=0, numTodoTests=0))
            (self.root / (label + '.log')).write_bytes(b'actual Vitest output\n')
        self.put(self.root / 'observed-classifier.json', dict(passed=True, captures=list(range(20)), busy_pairs=[1, 2]))
        commands = [{'argv': [str(self.node), str(self.source / 'node_modules/vitest/vitest.mjs'), 'run', str(test),
            '--config', str(self.recipe / 'vitest.replay.config.ts'), '--reporter', 'json', '--outputFile',
            str(self.root / (label + '-vitest.json'))], 'exit_code': 0} for label, test in
            (('release', self.source / 'tests/codex-plus-footer.test.ts'), ('observer', self.recipe / 'ap-release-replay-observer.test.ts'))]
        self.put(self.root / 'replay-report.json', dict(passed=True, candidate_acceptance=True,
            ap_version=self.plan['compat']['ap_version'], **self.plan['compat']['ap_replay'], commands=commands,
            captures=20, busy_pairs=2, tests=23, live_nudge_invoked=False,
            classifier_sha256=adapter.native.sha(self.source / 'src/commands/nudge.ts'),
            cursor_sha256=adapter.native.sha(self.source / 'src/core/tmux.ts'),
            observer_sha256=adapter.native.sha(self.recipe / 'ap-release-replay-observer.test.ts')))
        cleanup = dict(passed=True, outcome='clean', owned_remaining=[], unknown=[], signals_sent=0,
                       completed_local_supervision=True)
        self.put(self.root / 'job-cleanup.json', cleanup)
        guard_argv = [self.profile['python']['path'], '-B', '-c', 'qualified trampoline', '3',
            str(Path(self.profile['executor_package_root']) / 'bin/codex-plus-runtime'), 'advisor', '--owned-child', '--',
            self.profile['python']['path'], '-B', str(self.tooling / 'scripts/release_compat.py'), 'replay-ap', '--attempt', str(self.root)]
        journal = [dict(event=event, identity=dict(boot_id='fixture', pid=456, start_ticks=78), argv=guard_argv,
            exit_code=code, at_ns=10) for event, code in (('started', None), ('finished', 0))]
        (self.root / 'process-journal.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in journal))
        self.audit_path = self.root / 'leaf-independent-cleanup.json'
        self.put(self.audit_path, cleanup)
        self.review_path = self.root / 'leaf-executor-review.json'
        self.save_review()

    def save_job(self):
        self.put(self.directory / 'state.json', self.state)
        self.put(self.directory / 'result.json', self.result)

    def save_review(self):
        self.put(self.review_path, dict(scope=str(self.root), job_id='job-id', execution=str(self.directory),
            state_sha256=adapter.native.sha(self.directory / 'state.json'),
            result_sha256=adapter.native.sha(self.directory / 'result.json'), audit_sha256=adapter.native.sha(self.audit_path),
            identities_absent=True, groups_absent=True, sessions_absent=True, executor_lock_free=True))

    def compose(self):
        return adapter.compose(self.root, self.directory, self.prepared_path, self.audit_path, self.review_path)

    def test_prepare_retires_original_without_rewriting_or_circular_inputs(self):
        proposal = adapter.prepare_leaf(self.root)
        self.assertEqual((self.root / 'ap-job-proposal.json').read_bytes(), self.original_bytes)
        self.assertEqual(proposal['argv'][2:4], ['-c', adapter.LEAF_CODE])
        self.assertEqual(proposal['cwd'], str(self.root))
        self.assertEqual(proposal['env']['CODEX_PLUS_RELEASE_QA_SCOPE'], str(self.root))
        self.assertNotIn('leaf-job-proposal.json', [row['path'] for row in proposal['inputs']])
        self.assertIn('leaf-prelaunch.json', [row['path'] for row in proposal['inputs']])
        self.assertEqual(p.load(self.root / 'leaf-prelaunch.json')['inventory']['ap-job-proposal.json']['sha256'],
                         hashlib.sha256(self.original_bytes).hexdigest())

    def test_preexisting_replay_and_partial_outputs_refused(self):
        for name in adapter.REPLAY + adapter.WRAPPER + adapter.LEAF:
            with self.subTest(name=name):
                target = self.root / name
                target.write_bytes(b'existing outcome')
                with self.assertRaisesRegex(ValueError, 'Preexisting'):
                    adapter.prepare_leaf(self.root)
                target.unlink()

    def test_composition_equals_run_ap_tail_and_preserves_raw_records(self):
        written = {}
        load = p.load
        def original_load(path):
            return {'started_ns': 42} if Path(path).name == 'ap-started.json' else load(path)
        self.finish()
        # Compare the unchanged tail after execution, bypassing only its already-started guard.
        with patch.object(adapter.workflow, 'SCRIPTS', self.tooling / 'scripts'), \
             patch.object(adapter.workflow, 'bound_execution', return_value=('job-id', self.state['runtime']['sha256'])), \
             patch.object(adapter.workflow.native, 'owned_run', return_value=0), \
             patch.object(adapter.workflow, 'cleanup_record', return_value=dict(outcome='clean', owned_remaining=0, unknown=0, report='job-cleanup.json')), \
             patch.object(p, 'write_once', side_effect=lambda path, value: written.update({Path(path).name: value})), \
             patch.object(p, 'load', side_effect=original_load), patch.object(p, 'write_ap_receipt') as receipt:
            original_report = adapter.workflow.run_ap(self.root, self.directory)
            original_execution = receipt.call_args.kwargs['execution']
            original_executed = receipt.call_args.kwargs['executed']
        (self.root / 'managed.log').unlink()
        before = adapter.inventory(self.native)
        self.assertEqual(self.compose(), original_report)
        original_execution['argv'] = self.state['manifest']['command']['spec']['argv']
        self.assertEqual(p.load(self.root / 'leaf-execution.json'), original_execution)
        self.assertEqual(p.load(self.root / 'leaf-executed.json'), original_executed)
        self.assertEqual(adapter.inventory(self.native), before)
        for name in ('state.json', 'result.json', 'manifest.json', 'stdout.log', 'cleanup.log'):
            self.assertEqual((self.root / 'leaf-records' / name).read_bytes(), (self.directory / name).read_bytes())
        self.assertEqual((self.root / 'leaf-records/prepared-state.json').read_bytes(), self.prepared_path.read_bytes())
        self.assertFalse(any((self.root / name).exists() for name in adapter.WRAPPER))
        sidecar = p._read_ap_receipt(self.native, self.root / 'ap-receipt.json')
        self.assertEqual(sidecar['evidence'], 'leaf-ap-report.json')
        self.assertIn('leaf-provenance.json', sidecar['outputs'])
        self.assertTrue(p.accept_attempt(self.native, source_root=SCRIPTS.parent, ap_receipt=self.root / 'ap-receipt.json')['accepted'])

    def test_validate_job_accepts_exact_and_adjacent_result_timestamps(self):
        self.finish()
        lower = 1792751962.6755495
        upper = math.nextafter(lower, math.inf)
        for saved, separate in ((lower, lower), (lower, upper), (upper, lower)):
            with self.subTest(saved=saved, separate=separate):
                self.state['result'] = dict(self.result, finished_at=saved)
                self.result['finished_at'] = separate
                self.save_job()
                before = {name: (self.directory / name).read_bytes() for name in ('state.json', 'result.json')}
                state, records = adapter.validate_job(self.directory, self.prepared_path, self.proposal, self.profile)
                self.assertEqual(state['result']['finished_at'], saved)
                self.assertEqual(records['result.json']['finished_at'], separate)
                self.assertEqual(before, {name: (self.directory / name).read_bytes() for name in before})

    def test_validate_job_rejects_result_drift_outside_adjacent_timestamps(self):
        self.finish()
        original = copy.deepcopy(self.result)
        lower = 1792751962.6755495
        upper = math.nextafter(lower, math.inf)
        cases = [(lower, math.nextafter(upper, math.inf)),
                 (lower, math.nextafter(math.nextafter(lower, -math.inf), -math.inf))]
        for invalid in (math.nan, math.inf, -math.inf, True, False):
            cases.extend(((lower, invalid), (invalid, lower)))
        for saved, separate in cases + [(lower, upper)]:
            with self.subTest(saved=saved, separate=separate):
                self.state['result'] = dict(original, finished_at=saved)
                self.result = dict(original, finished_at=separate)
                if (saved, separate) == (lower, upper):
                    self.result['extra'] = True
                self.save_job()
                before = {name: (self.directory / name).read_bytes() for name in ('state.json', 'result.json')}
                with self.assertRaisesRegex(ValueError, 'Managed workload did not complete successfully'):
                    adapter.validate_job(self.directory, self.prepared_path, self.proposal, self.profile)
                self.assertEqual(before, {name: (self.directory / name).read_bytes() for name in before})

    def test_composition_preserves_distinct_adjacent_result_snapshots(self):
        self.finish()
        self.state['result'] = dict(self.result, finished_at=1792751962.6755495)
        self.result['finished_at'] = math.nextafter(self.state['result']['finished_at'], math.inf)
        self.save_job()
        self.save_review()
        names = ('state.json', 'result.json', 'manifest.json', 'stdout.log', 'cleanup.log')
        before = {name: (self.directory / name).read_bytes() for name in names}
        self.assertNotEqual(p.load(self.directory / 'state.json')['result'], p.load(self.directory / 'result.json'))
        self.compose()
        for name, data in before.items():
            self.assertEqual((self.directory / name).read_bytes(), data)
            self.assertEqual((self.root / 'leaf-records' / name).read_bytes(), data)
        self.assertTrue(p.accept_attempt(self.native, source_root=SCRIPTS.parent,
            ap_receipt=self.root / 'ap-receipt.json')['accepted'])

    def test_wrong_job_runtime_commands_outcomes_and_input_refused(self):
        self.finish()
        state, result = copy.deepcopy(self.state), copy.deepcopy(self.result)
        mutations = {
            'job': lambda: self.state.update(id='other-job'),
            'root': lambda: self.result.update(root='other-root'),
            'runtime': lambda: self.state['runtime'].update(sha256='0' * 64),
            'package': lambda: self.state.update(package='/other/executor'),
            'argv': lambda: self.state['manifest']['command']['spec']['argv'].append('--wrong'),
            'env': lambda: self.state['manifest']['command']['spec']['env'].update(UNREVIEWED='yes'),
            'deadline': lambda: self.state['manifest']['command']['spec'].update(deadline_seconds=999),
            'exit': lambda: self.result.update(exit_code=7),
            'signal': lambda: self.result.update(signal=15),
            'cancel': lambda: self.result.update(termination='cancelled'),
            'unattempted': lambda: self.result.update(workload_attempted=False),
            'incomplete': lambda: self.result.update(log_complete=False),
            'cleanup': lambda: self.result['cleanup'].update(exit_code=7),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.state, self.result = copy.deepcopy(state), copy.deepcopy(result)
                self.state['result'] = self.result
                mutate()
                self.save_job()
                self.save_review()
                with self.assertRaises(ValueError):
                    self.compose()
                self.assertFalse((self.root / 'leaf-ap-report.json').exists())
        self.state, self.result = state, result
        self.save_job()
        (self.root / 'native-captures.json').chmod(0o600)
        (self.root / 'native-captures.json').write_bytes(b'{}\n')
        with self.assertRaisesRegex(ValueError, 'Prelaunch input changed'):
            self.compose()

    def test_prepared_manifest_and_snapshots_refused_when_changed(self):
        self.finish()
        self.put(self.prepared_path, dict(self.prepared, review_digest='0' * 64))
        with self.assertRaisesRegex(ValueError, 'Prepared manifest'):
            self.compose()
        self.put(self.prepared_path, self.prepared)
        (self.directory / 'inputs/ap-profile.json').write_bytes(b'{}\n')
        with self.assertRaisesRegex(ValueError, 'frozen input changed'):
            self.compose()

    def test_consistent_wrong_manifest_cannot_replace_reviewed_proposal(self):
        self.finish()
        for key, value in (('argv', ['/unreviewed']), ('env', {'UNREVIEWED': 'yes'}), ('cwd', '/tmp')):
            with self.subTest(key=key):
                prepared = copy.deepcopy(self.prepared)
                prepared['manifest']['command']['spec'][key] = value
                material = {name: prepared[name] for name in ('schema', 'id', 'root', 'origin', 'manifest')}
                prepared['review_digest'] = hashlib.sha256(json.dumps(material, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
                self.put(self.prepared_path, prepared)
                self.put(self.directory / 'manifest.json', prepared)
                self.state.update(manifest=prepared['manifest'], review_digest=prepared['review_digest'])
                self.save_job()
                self.save_review()
                with self.assertRaisesRegex(ValueError, 'command binding'):
                    self.compose()
                self.assertFalse((self.root / 'leaf-ap-report.json').exists())

    def test_failed_counts_observer_and_audits_refused_before_artifacts(self):
        self.finish()
        mutations = [('release-vitest.json', 'numPassedTests', 21),
            ('observer-vitest.json', 'numFailedTests', 1), ('observer-vitest.json', 'numPendingTests', 1),
            ('observer-vitest.json', 'numTodoTests', 1), ('replay-report.json', 'passed', False),
            ('observed-classifier.json', 'captures', []), ('observed-classifier.json', 'busy_pairs', []),
            ('job-cleanup.json', 'owned_remaining', [123]), ('leaf-independent-cleanup.json', 'unknown', [123]),
            ('job-cleanup.json', 'completed_local_supervision', False),
            ('leaf-executor-review.json', 'executor_lock_free', False), ('leaf-executor-review.json', 'job_id', 'wrong')]
        for filename, key, value in mutations:
            with self.subTest(file=filename, key=key):
                path = self.root / filename
                data = path.read_bytes()
                changed = p.load(path)
                changed[key] = value
                self.put(path, changed)
                with self.assertRaises(ValueError):
                    self.compose()
                self.assertFalse((self.root / 'leaf-ap-report.json').exists())
                path.write_bytes(data)

    def test_supervised_constant_binds_original_api_and_propagates_exit(self):
        original_argv = [self.profile['python']['path'], '-B', '-c', adapter.LEAF_CODE,
            str(self.root), '/executor/root/job', str(self.tooling / 'scripts/release_compat.py')]
        calls = Mock()
        calls.verify_ap.return_value = self.context, self.plan, self.profile
        calls.owned_run.return_value = 7
        fake_workflow = SimpleNamespace(verify_ap=calls.verify_ap, bound_execution=calls.bound_execution)
        fake_native = SimpleNamespace(owned_run=calls.owned_run, python_env=adapter.native.python_env)
        with patch.dict(sys.modules, workflow=fake_workflow, release_native=fake_native), \
             patch.object(sys, 'argv', ['-c', *original_argv[4:]]), patch.object(sys, 'orig_argv', original_argv), \
             patch.object(sys, 'path', list(sys.path)):
            with self.assertRaises(SystemExit) as stopped:
                exec(adapter.LEAF_CODE, {})
        self.assertEqual(stopped.exception.code, 7)
        self.assertEqual([call[0] for call in calls.mock_calls], ['verify_ap', 'bound_execution', 'owned_run'])
        calls.verify_ap.assert_called_once_with(self.root)
        calls.bound_execution.assert_called_once_with(Path('/executor/root/job'), self.profile, original_argv)
        calls.owned_run.assert_called_once_with([self.profile['python']['path'], '-B',
            str(self.tooling / 'scripts/release_compat.py'), 'replay-ap', '--attempt', str(self.root)],
            helper=Path(self.profile['executor_package_root']) / 'bin/codex-plus-runtime',
            python=self.profile['python']['path'], cwd=self.root, env=adapter.native.python_env(self.profile) |
            {'CODEX_PLUS_RELEASE_QA_SCOPE': str(self.root)}, stdout=sys.stdout, timeout=300)

    def test_missing_or_incomplete_supervision_refused(self):
        self.finish()
        journal = self.root / 'process-journal.jsonl'
        data = journal.read_bytes()
        journal.write_bytes(data.splitlines(keepends=True)[0])
        with self.assertRaisesRegex(ValueError, 'supervision journal'):
            self.compose()
        journal.unlink()
        with self.assertRaises(ValueError):
            self.compose()


if __name__ == '__main__':
    unittest.main()
