"""Reuse existing public runner behavior checks against the Rust executable.

Run: python3 tests/test_rust_runtime_runner.py --runtime PATH -v
"""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_runner as reference

RUNTIME = Path(os.environ['CODEX_PLUS_TEST_RUNTIME']).resolve() if os.environ.get('CODEX_PLUS_TEST_RUNTIME') else None


@unittest.skipUnless(RUNTIME, 'set CODEX_PLUS_TEST_RUNTIME or pass --runtime')
class RustRunnerTests(reference.RunnerTests):
    def setUp(self):
        super().setUp()
        self.launcher = reference.LAUNCHER
        reference.LAUNCHER = RUNTIME

    def tearDown(self):
        reference.LAUNCHER = self.launcher
        super().tearDown()

    @unittest.skip('Python monkeypatch implementation test; Rust write_all has native unit coverage')
    def test_short_writes_are_completed(self):
        pass

    @unittest.skip('Python monkeypatch implementation test; Rust write_all has native unit coverage')
    def test_zero_log_write_is_an_error(self):
        pass

    @unittest.skip('Python monkeypatch fault injection does not invoke Rust; needs filesystem fault fixture')
    def test_write_failure_reports_child_outcome(self):
        pass

    def test_raw_non_utf8_argv_reaches_native_child_unchanged(self):
        result = subprocess.run([os.fsencode(RUNTIME), b'run', b'--', b'/usr/bin/printf', b'%s', b'raw-\xff'],
                                env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output_log(result).read_bytes(), b'raw-\xff')

    def test_executable_text_without_shebang_is_not_run_by_an_implicit_shell(self):
        marker = Path(self.temporary.name) / 'must-not-execute'
        script = Path(self.temporary.name) / 'executable-text'
        script.write_text(f'touch "{marker}"\n')
        script.chmod(0o755)
        result = subprocess.run([str(RUNTIME), 'run', '--', str(script)], env=self.env,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 126)
        self.assertFalse(marker.exists(), 'ENOEXEC text was executed through an implicit shell')
        self.assertEqual(self.output_log(result).read_bytes(), b'')

    def test_hup_reaches_owned_child_and_retains_result(self):
        process = subprocess.Popen([str(RUNTIME), 'run', '--', '/bin/sh', '-c', 'echo $$; exec sleep 60'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            self.assertTrue(process.stdout.readline().startswith(b'codex-plus log: '))
            child = int(process.stdout.readline())
            process.send_signal(signal.SIGHUP)
            output, _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 128 + signal.SIGHUP)
            self.assertIn(f'codex-plus result: exit={128 + signal.SIGHUP}'.encode(), output)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)

    def test_cancellation_remains_bounded_with_unread_backpressured_stdout(self):
        process = subprocess.Popen([str(RUNTIME), 'run', '--', '/usr/bin/yes', 'runner-backpressure'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            header = process.stdout.readline()
            self.assertTrue(header.startswith(b'codex-plus log: '), header)
            time.sleep(.1)
            process.send_signal(signal.SIGTERM)
            result = process.wait(timeout=5)  # Deliberately keep stdout unread through cancellation.
            self.assertIn(result, (128 + signal.SIGTERM, -signal.SIGKILL))
            log = Path(os.fsdecode(header.removeprefix(b'codex-plus log: ').strip()))
            self.assertGreater(log.stat().st_size, 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def test_forced_group_cancellation_catches_descendant_holding_pipe(self):
        descendant = ('import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                      'print(os.getpid(),flush=True); time.sleep(60)')
        source = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{descendant!r}])'
        process = subprocess.Popen([str(RUNTIME), 'run', '--', sys.executable, '-c', source],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            self.assertTrue(process.stdout.readline().startswith(b'codex-plus log: '))
            child = int(process.stdout.readline())
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertNotIn(b'codex-plus result:', output)
            # Group SIGKILL can reap the runner before the kernel schedules its descendant.
            deadline = time.monotonic() + 1
            while True:
                try:
                    stopped = Path(f'/proc/{child}/stat').read_text().split(') ', 1)[1].startswith('Z ')
                except (FileNotFoundError, ProcessLookupError):
                    stopped = True
                if stopped or time.monotonic() >= deadline:
                    break
                time.sleep(.005)
            self.assertTrue(stopped, f'descendant {child} survived group cancellation')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    args, remaining = parser.parse_known_args()
    RUNTIME = args.runtime.resolve(strict=True)
    RustRunnerTests.__unittest_skip__ = False
    RustRunnerTests.__unittest_skip_why__ = ''
    unittest.main(argv=[sys.argv[0], *remaining])
