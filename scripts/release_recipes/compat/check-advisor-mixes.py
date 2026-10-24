#!/usr/bin/env python3
"""Bounded native helper checks with loopback daemon and stub providers only."""
from recipe_context import CONFIG, DATA, OLD_VERSION, NEW_VERSION, UPSTREAM, source_identity
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import unittest

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--new-runtime', type=Path, required=True)
parser.add_argument('--old-runtime', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
FROZEN = CONFIG['source_commit']
source_identity(args.source, FROZEN)
assert not args.report.exists(), "preserve the prior advisor proof"
os.environ['CODEX_PLUS_TEST_RUNTIME'] = str(args.new_runtime.resolve(strict=True))
sys.path.insert(0, str(args.source.resolve(strict=True) / 'tests'))
spec = importlib.util.spec_from_file_location('claude_contract', args.source / 'tests/test_claude_advisor.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
OLD = str(args.old_runtime.resolve(strict=True))
NEW = str(args.new_runtime.resolve(strict=True))


class Mixes(contract.ClaudeAdvisor):
    # Inherit only the existing cancellation check; other contracts have separate qualification.
    def seed(self, model, version=1, calls=9):
        path = self.home / 'codex-plus' / f'{contract.ROOT}.advisor.json'
        path.parent.mkdir(exist_ok=True, mode=0o700)
        path.write_text(json.dumps(dict(version=version, parent=contract.ROOT, calls=calls, model=model)))
        return path

    def old_command(self):
        return [OLD, 'advisor', '--brief', 'Private stub compatibility check', '--host', 'local']

    def old(self):
        return subprocess.run(self.old_command(), env=self.env, cwd=self.workspace,
                              capture_output=True, text=True, timeout=15)

    def test_status_models_are_bidirectionally_readable(self):
        self.seed('gpt-6.1-astra')
        new = self.run_cli()
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(json.loads(new.stdout)['model'], contract.MODEL)
        self.assertEqual(self.status()['calls'], 10)
        old = self.old()
        self.assertEqual(old.returncode, 0, old.stderr)
        value = json.loads(old.stdout)
        self.assertEqual(value['model'], contract.MODEL)
        self.assertEqual(value['summary'], contract.RESULT['summary'])
        self.assertEqual(self.status()['calls'], 11)

    def test_invalid_status_versions_are_preserved_by_both(self):
        for runtime in ('old', 'new'):
            for version in (2, True, 1.0, None):
                with self.subTest(runtime=runtime, version=version):
                    path = self.seed(contract.MODEL, version=version)
                    before = path.read_bytes()
                    result = self.old() if runtime == 'old' else self.run_cli()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('invalid_status', result.stderr)
                    self.assertEqual(path.read_bytes(), before)

    def test_shared_lock_blocks_both_helpers(self):
        self.seed(contract.MODEL)
        lock = self.home / 'codex-plus' / f'{contract.ROOT}.advisor.lock'
        with lock.open('w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for result in (self.old(), self.run_cli()):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('already_running', result.stderr)

    def test_new_worker_rejects_source_mismatch_before_provider(self):
        payload = dict(protocol=1, worker='old', model=contract.MODEL, effort='xhigh',
                       prompt='stub', request_id=contract.ROOT, prompt_sha256='0' * 64)
        result = subprocess.run([NEW, 'advisor', '--remote-worker'], input=json.dumps(payload)+'\n',
                                env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)['error'], 'worker_version_mismatch')
        self.assertFalse((self.root / 'invoked.json').exists())

    def test_adjacent_callers_workers_succeed_bidirectionally(self):
        ssh = self.bin / 'ssh'
        for caller, worker in ((NEW, OLD), (OLD, NEW)):
            with self.subTest(caller=caller, worker=worker):
                ssh.write_text('#!/usr/bin/python3\nimport os\nos.execv(' + repr(worker) + ', [' + repr(worker) + ', "advisor", "--remote-worker"])\n')
                ssh.chmod(0o700)
                result = subprocess.run([caller, *self.command()[1:-1], 'fixture-central'],
                    cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertEqual({key:value[key] for key in contract.RESULT}, contract.RESULT)
                self.assertEqual(value['model'], contract.MODEL)
                self.assertEqual(value['effort'], 'xhigh')
                self.assertTrue((self.root / 'invoked.json').exists())
                self.assertNotIn('model/list', {r['method'] for r in self.server.requests})

    def test_same_release_ssh_mismatch_diagnostics(self):
        ssh = self.bin / 'ssh'
        ssh.write_text('#!/usr/bin/python3\nimport os\nos.execv(' + repr(NEW) + ', [' + repr(NEW) + ', "advisor", "--remote-worker"])\n')
        ssh.chmod(0o700)
        self.check_model_mismatch_diagnostic(host='fixture-central')

    def test_caller_sigkill_closes_worker_lifeline_and_reaps_descendants(self):
        (self.root / 'mode').write_text('hold')
        child = subprocess.Popen(self.command(), cwd=self.workspace, env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        contract.wait_for(lambda: (self.root / 'pids.json').exists())
        pids = json.loads((self.root / 'pids.json').read_text())
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=10)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        contract.wait_for(lambda: not any(contract.alive(pid) for pid in pids))


names = ['test_status_models_are_bidirectionally_readable',
         'test_invalid_status_versions_are_preserved_by_both',
         'test_shared_lock_blocks_both_helpers',
         'test_new_worker_rejects_source_mismatch_before_provider',
         'test_adjacent_callers_workers_succeed_bidirectionally',
         'test_model_mismatch_logs_returned_metadata',
         'test_same_release_ssh_mismatch_diagnostics',
         'test_caller_sigkill_closes_worker_lifeline_and_reaps_descendants',
         'test_cancel_reaps_detached_descendants_and_excludes_second_call']
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Mixes(name) for name in names))
report = dict(passed=result.wasSuccessful(), checks=names, tests_run=result.testsRun,
              failures=len(result.failures), errors=len(result.errors),
              source=str(args.source.resolve()),
              source_commit=subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', 'HEAD'], text=True).strip(),
              runtimes={name: {'path': path, 'sha256': hashlib.file_digest(Path(path).open('rb'), 'sha256').hexdigest()}
                        for name, path in [('old', OLD), ('new', NEW)]},
              provider='stub only', transport='local worker and stub SSH; no remote hosts',
              qualification='final package helper bytes; package qualification is bound by the native combined runner')
with args.report.open('x') as stream:
    stream.write(json.dumps(report, indent=2) + '\n')
raise SystemExit(0 if result.wasSuccessful() else 1)
