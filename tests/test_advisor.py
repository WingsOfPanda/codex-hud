"""Advisor lifecycle checks with private homes and fake native binaries only."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_plus import advisor

ROOT = Path(__file__).resolve().parents[1]
PARENT = '11111111-1111-4111-8111-111111111111'
RESULT = dict(verdict='approve', summary='Evidence checked.', findings=[], next_steps=[])


def model(name='gpt-6-astra', hidden=False, effort='xhigh'):
    return dict(id='picker-id', model=name, hidden=hidden,
                supportedReasoningEfforts=[{'reasoningEffort': effort}])


class FakeClient:
    def __init__(self, source='cli', parent=None, models=None):
        self.meta = dict(id=PARENT, source=source, parentThreadId=parent)
        self.models = [model()] if models is None else models
        self.calls, self.closed = [], False

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': self.meta}
        if method == 'model/list':
            return {'data': self.models, 'nextCursor': None}
        raise AssertionError(method)

    def close(self):
        self.closed = True


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.home = self.directory / 'home'
        self.native = self.directory / 'native'
        self.client = FakeClient()
        self.env = patch.dict(os.environ, CODEX_HOME=str(self.home), CODEX_THREAD_ID=PARENT)
        self.env.start()
        self.make_native()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def make_native(self, source=None):
        source = source or (
            'import json,os,sys\nfrom pathlib import Path\n'
            'args=sys.argv[1:]\n'
            f'Path({str(self.directory / "argv")!r}).write_text(json.dumps(args))\n'
            f'Path({str(self.directory / "prompt")!r}).write_text(sys.stdin.read())\n'
            f'Path({str(self.directory / "env")!r}).write_text(json.dumps(dict(os.environ)))\n'
            'p=Path(args[args.index("--output-last-message")+1])\n'
            f'p.write_text({json.dumps(RESULT)!r})\nprint("private native output")\n')
        self.native.write_text(f'#!{sys.executable}\n' + source)
        self.native.chmod(0o700)

    def consult(self, **kwargs):
        return advisor.consult('Self-contained proposal.', self.home, PARENT,
                               client_factory=lambda: self.client, native=self.native, **kwargs)

    def status(self):
        return json.loads((self.home / 'codex-plus' / f'{PARENT}.advisor.json').read_text())

    def test_success_is_quiet_private_and_constrained(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.consult()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(result['verdict'], 'approve')
        self.assertEqual(result['model'], 'gpt-6-astra')
        log = Path(result['log'])
        self.assertIn('private native output', log.read_text())
        self.assertIn('Self-contained proposal.', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        args = json.loads((self.directory / 'argv').read_text())
        self.assertEqual(args[:12], ['exec', '--ignore-user-config', '--ignore-rules', '--sandbox',
                                    'read-only', '--skip-git-repo-check', '--ephemeral', '-m',
                                    'gpt-6-astra', '-c', 'model_reasoning_effort="xhigh"', '-c'])
        self.assertIn('agents.enabled=false', args)
        self.assertIn('features.multi_agent_v2=false', args)
        self.assertNotIn('features.multi_agent=false', args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
        self.assertEqual(args[-1], '-')
        self.assertNotIn('CODEX_THREAD_ID', json.loads((self.directory / 'env').read_text()))
        self.assertEqual(json.loads((self.directory / 'env').read_text())['CODEX_PLUS_ADVISOR_ACTIVE'], '1')
        status = self.status()
        self.assertEqual(status['state'], 'done')
        self.assertEqual(status['calls'], 1)
        self.assertNotIn('brief', status)
        self.assertNotIn('findings', status)
        self.assertEqual((self.home / 'codex-plus' / f'{PARENT}.advisor.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.client.closed)
        self.assertEqual(self.client.calls[0], ('thread/read', {'threadId': PARENT, 'includeTurns': False}))

    def test_attempt_count_persists_and_stale_running_record_is_replaced(self):
        self.consult()
        path = self.home / 'codex-plus' / f'{PARENT}.advisor.json'
        previous = self.status()
        previous.update(state='running', updated_at=1)
        path.write_text(json.dumps(previous))
        self.consult()
        self.assertEqual(self.status()['calls'], 2)

    def test_unknown_status_version_is_preserved_without_incrementing(self):
        self.consult()
        path = self.home / 'codex-plus' / f'{PARENT}.advisor.json'
        previous = self.status()
        for version in (None, 2, True, 1.0):
            previous['version'] = version
            path.write_text(json.dumps(previous))
            before = path.read_bytes()
            with self.subTest(version=version), self.assertRaisesRegex(advisor.AdvisorError, 'invalid_status'):
                self.consult()
            self.assertEqual(path.read_bytes(), before)

    def test_git_pointer_includes_clean_committed_feature_and_dirty_stats(self):
        repo = self.directory / 'repo'
        repo.mkdir()

        def git(*args):
            return subprocess.run(['git', '-c', 'user.name=Advisor Test',
                                   '-c', 'user.email=advisor@example.invalid', *args],
                                  cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

        git('init', '-b', 'main')
        changed = repo / 'feature.txt'
        changed.write_text('original\n')
        git('add', '.')
        git('commit', '-m', 'base')
        base = git('rev-parse', 'HEAD')
        git('checkout', '-b', 'feature')
        changed.write_text('original\ncommitted feature\n')
        git('commit', '-am', 'feature')
        head = git('rev-parse', 'HEAD')
        self.assertEqual(git('status', '--short'), '')
        pointer = advisor.git_pointer(repo)
        self.assertIn(f'Merge base against refs/heads/main: {base}', pointer)
        self.assertIn(head, pointer)
        self.assertIn(f'git diff --stat {base} HEAD:\n feature.txt', pointer)
        self.assertNotIn('committed feature', pointer)
        changed.write_text('original\ncommitted feature\nstaged\n')
        git('add', '.')
        changed.write_text('original\ncommitted feature\nstaged\nunstaged\n')
        pointer = advisor.git_pointer(repo)
        self.assertIn('git diff --stat:\n feature.txt', pointer)
        self.assertIn('git diff --cached --stat:\n feature.txt', pointer)
        git('branch', '-D', 'main')
        self.assertIn('Merge base unavailable', advisor.git_pointer(repo))

    def test_root_guard_rejects_workers_mismatch_and_invalid_ids(self):
        for source, parent in (({'subAgent': 'review'}, None), ('subAgent', None),
                               ({'subAgentThreadSpawn': {}}, None), ('cli', PARENT)):
            self.client = FakeClient(source=source, parent=parent)
            with self.subTest(source=source), self.assertRaisesRegex(advisor.AdvisorError, 'root_required'):
                self.consult()
        self.client = FakeClient()
        self.client.meta['id'] = 'wrong'
        with self.assertRaisesRegex(advisor.AdvisorError, 'root_required'):
            self.consult()
        for parent in (None, '../escape', 'not-a-uuid'):
            with self.assertRaisesRegex(advisor.AdvisorError, 'root_required'):
                advisor.consult('brief', self.home, parent, client_factory=FakeClient)
        self.assertFalse(self.home.exists())

    def test_recursive_advisor_is_rejected_before_daemon_or_files(self):
        with patch.dict(os.environ, CODEX_PLUS_ADVISOR_ACTIVE='1'):
            with self.assertRaisesRegex(advisor.AdvisorError, 'recursive_advisor'):
                self.consult()
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.home.exists())

    def test_numeric_model_selection_and_allowlist(self):
        self.client.models = [model('gpt-6-astra'), model('gpt-6.9-astra'), model('gpt-6.10-astra'),
                              model('gpt-20-astra', hidden=True), model('gpt-19-astra', effort='high'),
                              model('gpt-99-sol'), model('gpt-99-astra-preview'), model('astra')]
        self.assertEqual(advisor.select_model(self.client), 'gpt-6.10-astra')
        for models in ([], [model('gpt-6-sol')], [model(effort='high')], [model(hidden=True)]):
            self.client.models = models
            with self.assertRaisesRegex(advisor.AdvisorError, 'astra_unavailable'):
                advisor.select_model(self.client)

    def test_catalog_pagination_and_cycle_fail_closed(self):
        class Paginated(FakeClient):
            def call(inner, method, params):
                inner.calls.append((method, params))
                if params['cursor'] is None:
                    return {'data': [model('gpt-6-astra')], 'nextCursor': 'next'}
                return {'data': [model('gpt-7-astra')], 'nextCursor': None}
        client = Paginated()
        self.assertEqual(advisor.select_model(client), 'gpt-7-astra')
        self.assertEqual(client.calls[1][1], {'limit': 100, 'cursor': 'next', 'includeHidden': False})
        client.call = lambda *_: {'data': [model()], 'nextCursor': 'repeated'}
        with self.assertRaisesRegex(advisor.AdvisorError, 'invalid_catalog'):
            advisor.select_model(client)

    def test_nonblocking_lock_rejects_overlap(self):
        directory = advisor.state_directory(self.home)
        with open(directory / f'{PARENT}.advisor.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(advisor.AdvisorError, 'already_running'):
                self.consult()
        self.assertFalse((self.directory / 'argv').exists())

    def test_status_symlink_and_fifo_fail_without_following(self):
        directory = advisor.state_directory(self.home)
        path = directory / f'{PARENT}.advisor.json'
        target = self.directory / 'target'
        target.write_text('unchanged')
        for kind in ('symlink', 'fifo'):
            if kind == 'symlink':
                path.symlink_to(target)
            else:
                os.mkfifo(path)
            with self.subTest(kind=kind), self.assertRaises((OSError, advisor.AdvisorError)):
                self.consult()
            path.unlink()
        self.assertEqual(target.read_text(), 'unchanged')

    def test_nonzero_invalid_missing_and_oversized_results_never_approve(self):
        prefix = 'import sys\nfrom pathlib import Path\na=sys.argv\np=Path(a[a.index("--output-last-message")+1])\n'
        for source, code in (('p.write_text(' + repr(json.dumps(RESULT)) + ');raise SystemExit(9)', 'exec_failed'),
                             ('p.write_text("not json")', 'invalid_result'),
                             ('p.unlink()', 'invalid_result'),
                             ('p.write_text("x"*70000)', 'invalid_result'),
                             ('p.write_text(' + repr(json.dumps(dict(RESULT, verdict='unknown'))) + ')', 'invalid_result')):
            self.make_native(prefix + source)
            with self.subTest(source=source), self.assertRaisesRegex(advisor.AdvisorError, code) as caught:
                self.consult()
            self.assertTrue(Path(caught.exception.log).exists())
            self.assertEqual(self.status()['state'], 'error')
            self.assertEqual(self.status()['error'], code)

    def test_validation_rejects_wrong_shapes_and_extra_keys(self):
        for value in ([], dict(RESULT, extra=True), dict(RESULT, summary=''),
                      dict(RESULT, findings=[{'severity': 'high', 'message': 'x', 'other': 1}]),
                      dict(RESULT, next_steps='bad'), dict(RESULT, summary='x' * 2001)):
            with self.subTest(value=value), self.assertRaises(advisor.AdvisorError):
                advisor.validate_result(value)

    def test_timeout_terminates_owned_group_and_keeps_error_status(self):
        marker = self.directory / 'late-write'
        self.make_native('import subprocess,sys,time\n'
                         f'subprocess.Popen([sys.executable,"-c",{("import time;from pathlib import Path;time.sleep(1);Path(" + repr(str(marker)) + ").touch()")!r}])\n'
                         'time.sleep(10)\n')
        with self.assertRaisesRegex(advisor.AdvisorError, 'timeout'):
            self.consult(timeout=0.15)
        time.sleep(1.1)
        self.assertFalse(marker.exists())
        self.assertEqual(self.status()['error'], 'timeout')

    def test_signal_terminates_group_and_heartbeat_is_content_free(self):
        self.make_native('import time\ntime.sleep(20)\n')
        source = ('import os,sys\nfrom pathlib import Path\nfrom codex_plus import advisor\n'
                  'from tests.test_advisor import FakeClient,PARENT\nadvisor.HEARTBEAT=0.1\n'
                  f'advisor.consult("secret brief",Path({str(self.home)!r}),PARENT,client_factory=FakeClient,native=Path({str(self.native)!r}))')
        # tests is not a package; load the fake dependency by its source directory.
        source = source.replace('from tests.test_advisor', 'from test_advisor')
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'tests') + os.pathsep + str(ROOT))
        child = subprocess.Popen([sys.executable, '-c', source], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.home.exists():
                    try:
                        value = self.status()
                        if value['updated_at'] > value['started_at']:
                            break
                    except FileNotFoundError:
                        pass
                time.sleep(0.02)
            else:
                self.fail('no heartbeat')
            self.assertNotIn('secret brief', json.dumps(value))
            child.send_signal(signal.SIGTERM)
            stdout, _ = child.communicate(timeout=5)
            self.assertEqual(stdout, b'')
            self.assertNotEqual(child.returncode, 0)
            self.assertEqual(self.status()['error'], 'interrupted')
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_native_resolution_and_missing_native_are_explicit(self):
        with patch.object(advisor, 'PACKAGE_ROOT', self.directory):
            with self.assertRaisesRegex(advisor.AdvisorError, 'native_unavailable'):
                advisor.native_binary()
            (self.directory / '.build').mkdir()
            native = self.directory / '.build' / 'codex'
            native.write_text('')
            native.chmod(0o700)
            self.assertEqual(advisor.native_binary(), native)

    def test_launcher_routes_advisor_without_native(self):
        env = dict(os.environ)
        env.pop('CODEX_THREAD_ID', None)
        result = subprocess.run([str(ROOT / 'bin' / 'codex-plus'), 'advisor', '--brief', 'brief'],
                                env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'root_required', result.stderr)
        self.assertEqual(result.stdout, b'')


if __name__ == '__main__':
    unittest.main()
