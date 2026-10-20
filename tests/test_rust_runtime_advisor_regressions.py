"""Advisor audit regressions against the actual Rust helper, with no remote provider.

python3 tests/test_rust_runtime_advisor_regressions.py --runtime PATH --evidence DIRECTORY
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

from fixtures.native_server import AppServer


PARENT = '11111111-1111-4111-8111-111111111111'
MODEL = dict(model='gpt-6-astra', hidden=False,
             supportedReasoningEfforts=[dict(reasoningEffort='xhigh')])
RUNTIME = None
EVIDENCE = None
RESULT = dict(verdict='approve', summary='Checked.', findings=[], next_steps=[])
HELD = '''import json,os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = os.fork()
if child == 0:
    while True: time.sleep(1)
Path(os.environ['CODEX_HOME']).joinpath('owned-pids').write_text(json.dumps([os.getpid(), child]))
while True: time.sleep(1)
'''
DETACHED = '''import json,os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
leader = os.getpid()
child = os.fork()
if child == 0:
    os.setsid()
    grandchild = os.fork()
    if grandchild == 0:
        os.setsid()
        while True: time.sleep(1)
    Path(os.environ['CODEX_HOME']).joinpath('owned-pids').write_text(
        json.dumps([leader, os.getpid(), grandchild]))
while True: time.sleep(1)
'''


def wait_for(check, description, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.02)
    raise AssertionError(description)


class AdvisorRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if RUNTIME is None:
            raise unittest.SkipTest('run with --runtime to select the actual Rust binary')
        # Collect leaked baseline children rather than leaving zombies in the test host.
        assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='advisor-regression-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home, self.package, self.workspace = [self.root / name for name in ('home', 'package', 'workspace')]
        (self.home / 'app-server-control').mkdir(parents=True)
        (self.package / 'bin').mkdir(parents=True)
        self.workspace.mkdir()
        self.server = AppServer(PARENT, self.workspace, 0, models=[MODEL],
                                socket_path=self.home / 'app-server-control/app-server-control.sock')
        self.addCleanup(self.server.close)
        self.env = dict(os.environ, CODEX_HOME=str(self.home), CODEX_THREAD_ID=PARENT)
        self.env.pop('CODEX_PLUS_ADVISOR_ACTIVE', None)
        self.command = [str(RUNTIME), '--package-root', str(self.package), 'advisor', '--provider', 'astra', '--brief', 'Local fixture only.']
        self.write_native('''import os,sys
from pathlib import Path
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_bytes(
    Path(os.environ['CODEX_HOME']).joinpath('payload').read_bytes())
''')

    def write_native(self, source):
        native = self.package / 'bin/codex'
        native.write_text('#!/usr/bin/python3\n' + source)
        native.chmod(0o700)

    def launch(self, payload, *, success=True, name='result'):
        (self.home / 'payload').write_bytes(payload)
        result = subprocess.run(self.command, cwd=self.workspace, env=self.env,
                                capture_output=True, timeout=15)
        if EVIDENCE:
            (EVIDENCE / f'{self._testMethodName}-{name}.stdout').write_bytes(result.stdout)
            (EVIDENCE / f'{self._testMethodName}-{name}.stderr').write_bytes(result.stderr)
        self.assertEqual(result.returncode == 0, success, result.stderr.decode())
        if success:
            return json.loads(result.stdout)
        self.assertIn(b'invalid_result', result.stderr)

    def test_result_transport_accepts_maximum_schema_text_encodings(self):
        for character, ascii_only in [('汉', False), ('😀', False), ('😀', True), ('x\x00\n\\"', True)]:
            with self.subTest(character=character, ascii_only=ascii_only):
                def text(size):
                    return (character * size)[:size]
                value = dict(verdict='revise', summary=text(2000),
                             findings=[dict(severity='medium', message=text(1000)) for _ in range(20)],
                             next_steps=[text(500) for _ in range(20)])
                payload = json.dumps(value, ensure_ascii=ascii_only).encode()
                result = self.launch(payload, name=f'{ord(character[0])}-{ascii_only}')
                self.assertEqual({key: result[key] for key in value}, value)

    def test_result_transport_still_rejects_character_and_file_overflow(self):
        value = dict(RESULT, summary='汉' * 2001)
        self.launch(json.dumps(value).encode(), success=False, name='characters')
        self.launch(json.dumps(RESULT).encode() + b' ' * (512 * 1024), success=False, name='bytes')

    def test_result_file_rejects_symlinks_and_nonregular_files(self):
        for kind in ('symlink', 'fifo'):
            self.write_native('''import os,sys
from pathlib import Path
result=Path(sys.argv[sys.argv.index('--output-last-message')+1])
result.unlink()
''' + ("result.symlink_to(Path(os.environ['CODEX_HOME'])/'payload')\n" if kind == 'symlink'
       else 'os.mkfifo(result)\n'))
            self.launch(json.dumps(RESULT).encode(), success=False, name=kind)

    def test_native_executable_without_shebang_has_no_shell_fallback(self):
        marker = self.root / 'executed'
        (self.package / 'bin/codex').write_text(f'touch "{marker}"\n')
        result = subprocess.run(self.command, cwd=self.workspace, env=self.env,
                                capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'exec_failed', result.stderr)
        self.assertFalse(marker.exists())

    def test_git_pointer_never_runs_configured_fsmonitor(self):
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.workspace, check=True,
                                  capture_output=True)
        git('init', '-b', 'main')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (self.workspace / 'tracked').write_text('unchanged\n')
        git('add', 'tracked')
        git('commit', '-m', 'fixture')
        outside = self.root / 'outside-marker'
        hook = self.root / 'fsmonitor'
        hook.write_text(f'#!/bin/sh\ntouch "{outside}"\nprintf "1\\0"\n')
        hook.chmod(0o700)
        git('config', 'core.fsmonitor', str(hook))
        before = {name: (self.workspace / '.git' / name).read_bytes() for name in ('index', 'config')}
        result = self.launch(json.dumps(RESULT).encode())
        self.assertFalse(outside.exists(), 'pointer collector executed local fsmonitor hook')
        self.assertIn('git status --short:', Path(result['log']).read_text())
        self.assertIn('Merge base against refs/heads/main:', Path(result['log']).read_text())
        self.assertEqual(before, {name: (self.workspace / '.git' / name).read_bytes() for name in before})
        self.assertEqual((self.workspace / 'tracked').read_text(), 'unchanged\n')

    def cancellation(self, cancellation, *, git_probe=False, detached=False):
        source = DETACHED if detached else HELD
        if git_probe:
            git = self.root / 'git'
            git.write_text('#!/usr/bin/python3\n' + source)
            git.chmod(0o700)
            self.env['PATH'] = str(self.root) + os.pathsep + self.env['PATH']
        else:
            self.write_native(source)
        command = self.command
        if cancellation == 'runner':
            command = [str(RUNTIME), 'run', '--', *command]
        pane = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
        process = subprocess.Popen(command, cwd=self.workspace, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        pids = []
        try:
            marker = self.home / 'owned-pids'
            wait_for(lambda: marker.exists() and marker.stat().st_size > 0, 'owned command did not start')
            pids = json.loads(marker.read_text())
            if cancellation == 'runner':
                process.send_signal(signal.SIGTERM)
            elif cancellation == 'owner':
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=10)
            if EVIDENCE:
                (EVIDENCE / f'{self._testMethodName}.json').write_text(json.dumps(dict(
                    pids=pids, returncode=process.returncode, stdout=stdout.decode(), stderr=stderr.decode()), indent=2))
            self.assertNotEqual(process.returncode, 0)
            wait_for(lambda: all(not Path(f'/proc/{pid}').exists() for pid in pids),
                     f'owned leader or descendant survived {cancellation}: {pids}', seconds=5)
            self.assertIsNone(pane.poll(), 'cleanup harmed another pane')
        finally:
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            pane.terminate()
            pane.wait(timeout=5)
            for pid in pids:
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            while True:
                try:
                    if os.waitpid(-1, os.WNOHANG)[0] == 0:
                        break
                except ChildProcessError:
                    break

    def test_nested_runner_cancellation_reaps_consultation_group(self):
        self.cancellation('runner')

    def test_immediate_native_group_sigkill_reaps_consultation_group(self):
        self.cancellation('group')

    def test_immediate_owner_sigkill_reaps_consultation_group(self):
        self.cancellation('owner')

    def test_immediate_native_group_sigkill_reaps_git_probe_group(self):
        self.cancellation('group', git_probe=True)

    def test_nested_runner_cancellation_reaps_git_probe_group(self):
        self.cancellation('runner', git_probe=True)

    def test_owner_sigkill_reaps_independently_detached_grandchildren(self):
        self.cancellation('owner', detached=True)

    def test_native_group_sigkill_reaps_independently_detached_git_grandchildren(self):
        self.cancellation('group', git_probe=True, detached=True)

    def test_successful_leader_exit_still_reaps_detached_descendants(self):
        source = '''import sys
from pathlib import Path
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(''' + repr(json.dumps(RESULT)) + ''')
''' + DETACHED.rsplit('while True: time.sleep(1)', 1)[0] + '''
while not Path(os.environ['CODEX_HOME']).joinpath('owned-pids').exists(): time.sleep(.01)
os._exit(0)
'''
        self.write_native(source)
        self.launch(json.dumps(RESULT).encode())
        pids = json.loads((self.home / 'owned-pids').read_text())
        self.assertTrue(all(not Path(f'/proc/{pid}').exists() for pid in pids), pids)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--evidence', type=Path)
    args, remaining = parser.parse_known_args()
    RUNTIME = args.runtime.resolve(strict=True)
    EVIDENCE = args.evidence.resolve() if args.evidence else None
    if EVIDENCE:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__, *remaining])
