"""Private noninteractive runner checks; no daemon or production-state writes."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_plus import runner

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'bin' / 'codex-plus'


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / 'home'
        self.env = dict(os.environ, CODEX_HOME=str(self.home), PYTHONPATH=str(ROOT))

    def tearDown(self):
        self.temporary.cleanup()

    def run_python(self, source, **kwargs):
        return subprocess.run([str(LAUNCHER), 'run', '--', sys.executable, '-c', source],
                              env=self.env, capture_output=True, timeout=10, **kwargs)

    def output_log(self, result):
        first = result.stdout.split(b'\n', 1)[0].decode()
        self.assertTrue(first.startswith('codex-plus log: '), first)
        return Path(first.removeprefix('codex-plus log: '))

    def test_full_raw_combined_output_and_exact_exit(self):
        result = self.run_python("import os; os.write(1,b'out\\xff\\n'); os.write(2,b'err\\xfe'); raise SystemExit(23)")
        log = self.output_log(result)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(log.read_bytes(), b'out\xff\nerr\xfe')
        self.assertEqual(result.stdout, f'codex-plus log: {log}\n'.encode() +
                         b'out\xff\nerr\xfe\n' + f'codex-plus result: exit=23 log={log}\n'.encode())
        self.assertEqual(result.stderr, b'')
        self.assertTrue(log.is_absolute())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        for directory in (log.parent, log.parent.parent):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_large_output_is_complete_and_retained_between_runs(self):
        result = self.run_python("import os; [os.write(1,b'x'*65536) for _ in range(192)]")
        log = self.output_log(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(log.stat().st_size, 12 * 1024 * 1024)
        self.assertEqual(log.read_bytes(), b'x' * (12 * 1024 * 1024))
        next_result = self.run_python("print('next')")
        self.assertNotEqual(log, self.output_log(next_result))
        self.assertTrue(log.exists())

    def test_cwd_environment_stdin_and_path(self):
        self.env['RUNNER_CHECK'] = 'inherited'
        source = "import os,sys; print(os.getcwd()); print(os.getenv('RUNNER_CHECK')); print(repr(sys.stdin.read())); print(os.environ['PATH'])"
        result = self.run_python(source, cwd=self.temporary.name)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output_log(result).read_text().splitlines(),
                         [self.temporary.name, 'inherited', "''", self.env['PATH']])

    def test_shell_composition_is_explicit(self):
        result = subprocess.run([str(LAUNCHER), 'run', '--', 'bash', '-lc',
                                 "printf first; printf second >&2; exit 19"],
                                env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 19)
        self.assertEqual(self.output_log(result).read_bytes(), b'firstsecond')

    def test_invalid_usage_does_not_create_logs(self):
        for arguments in (['run'], ['run', '--'], ['run', sys.executable]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(LAUNCHER), *arguments], env=self.env,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn(b'usage:', result.stderr)
                self.assertFalse(self.home.exists())

    def test_launch_failure_retains_log_and_result(self):
        result = subprocess.run([str(LAUNCHER), 'run', '--', '/nonexistent/codex-plus-program'],
                                env=self.env, capture_output=True, timeout=10)
        log = self.output_log(result)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(log.read_bytes(), b'')
        self.assertIn(b'cannot launch command', result.stderr)
        self.assertIn(f'codex-plus result: exit=127 log={log}'.encode(), result.stdout)

    def test_log_path_failure_prevents_command_execution(self):
        marker = Path(self.temporary.name) / 'executed'
        for level in ('home', 'codex-plus', 'logs'):
            with self.subTest(level=level):
                isolated = Path(self.temporary.name) / level
                isolated.mkdir()
                target = isolated / 'outside'
                target.mkdir()
                home = isolated / 'home'
                link = home
                if level != 'home':
                    home.mkdir()
                    link = home / 'codex-plus'
                if level == 'logs':
                    link.mkdir()
                    link = link / 'logs'
                link.symlink_to(target, target_is_directory=True)
                result = subprocess.run([str(LAUNCHER), 'run', '--', sys.executable, '-c',
                                         f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                                        env=dict(self.env, CODEX_HOME=str(home)),
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 74)
                self.assertIn(b'cannot open log', result.stderr)
                self.assertFalse(marker.exists())
                self.assertEqual(list(target.iterdir()), [])

    def test_write_failure_reports_child_outcome(self):
        source = "from codex_plus import runner; import sys; runner.write_all=lambda *args: (_ for _ in ()).throw(OSError('disk full')); sys.exit(runner.run_command([sys.executable,'-c',\"print('payload'); raise SystemExit(17)\"]))"
        result = subprocess.run([sys.executable, '-c', source], env=self.env,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 74)
        self.assertIn(b'log incomplete: disk full; command exit=17', result.stderr)
        self.assertIn(b'codex-plus result: exit=17', result.stdout)
        self.assertIn(b'log_incomplete=true', result.stdout)
        self.assertIn(b'payload', result.stdout)

    def test_short_writes_are_completed(self):
        with patch.object(runner.os, 'write', side_effect=[2, 1]) as write:
            runner.write_all(7, b'abc')
        self.assertEqual(bytes(write.call_args_list[0].args[1]), b'abc')
        self.assertEqual(bytes(write.call_args_list[1].args[1]), b'c')

    def test_zero_log_write_is_an_error(self):
        with patch.object(runner.os, 'write', return_value=0):
            with self.assertRaisesRegex(OSError, 'zero-byte log write'):
                runner.write_all(7, b'abc')

    def test_closed_output_before_header_prevents_launch(self):
        marker = Path(self.temporary.name) / 'executed'
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            result = subprocess.run([str(LAUNCHER), 'run', '--', sys.executable, '-c',
                                     f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                                    env=self.env, stdout=write_end, stderr=subprocess.PIPE, timeout=5)
        finally:
            os.close(write_end)
        self.assertEqual(result.returncode, 143)
        self.assertFalse(marker.exists())
        self.assertEqual(len(list((self.home / 'codex-plus' / 'logs').glob('*.log'))), 1)

    def test_output_streams_before_completion(self):
        process = subprocess.Popen([str(LAUNCHER), 'run', '--', sys.executable, '-c',
                                    "import time; print('ready',flush=True); time.sleep(0.5); print('done')"],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(process.stdout.readline().startswith(b'codex-plus log: '))
            self.assertEqual(process.stdout.readline(), b'ready\n')
            self.assertIsNone(process.poll())
            output, error = process.communicate(timeout=5)
            self.assertIn(b'done', output)
            self.assertEqual(error, b'')
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_group_cancellation_signals_child_and_preserves_final_record(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                source = "import os,time; print(os.getpid(),flush=True); time.sleep(60)"
                process = subprocess.Popen([str(LAUNCHER), 'run', '--', sys.executable, '-c', source],
                                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
                try:
                    self.assertTrue(process.stdout.readline().startswith(b'codex-plus log: '))
                    child_pid = int(process.stdout.readline())
                    self.assertEqual(os.getpgid(child_pid), process.pid)
                    process.send_signal(signum)
                    output, error = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 128 + signum)
                    self.assertIn(f'codex-plus result: exit={128 + signum}'.encode(), output)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def test_ignoring_child_is_killed_after_grace_even_with_closed_output(self):
        source = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); os.close(1); os.close(2); time.sleep(60)"
        process = subprocess.Popen([str(LAUNCHER), 'run', '--', sys.executable, '-c', source],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            process.stdout.readline()
            self.assertEqual(process.stdout.readline(), b'ready\n')
            process.send_signal(signal.SIGTERM)
            output, _error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertNotIn(b'codex-plus result:', output)
            self.assertEqual(list((self.home / 'codex-plus' / 'logs').glob('*.log'))[0].read_bytes(),
                             b'ready\n')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def test_forced_group_cancellation_catches_descendant_holding_pipe(self):
        descendant = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(os.getpid(),flush=True); time.sleep(60)"
        source = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{descendant!r}])"
        process = subprocess.Popen([str(LAUNCHER), 'run', '--', sys.executable, '-c', source],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            process.stdout.readline()
            descendant_pid = int(process.stdout.readline())
            process.send_signal(signal.SIGTERM)
            output, _error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertNotIn(b'codex-plus result:', output)
            state = Path(f'/proc/{descendant_pid}/stat')
            self.assertTrue(not state.exists() or state.read_text().split(') ', 1)[1].startswith('Z '))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == '__main__':
    unittest.main()
