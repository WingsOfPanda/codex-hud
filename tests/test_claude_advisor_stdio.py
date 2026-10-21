"""Actual remote-worker pipe failures; private stub only, no provider calls.

CODEX_PLUS_TEST_RUNTIME=/absolute/codex-plus-runtime python3 tests/test_claude_advisor_stdio.py
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
import uuid

from test_claude_advisor import MODEL, RUNTIME, alive, wait_for


STUB = r'''#!/usr/bin/python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ["CLAUDE_FIXTURE"])
sys.stdin.read()
child = subprocess.Popen([sys.executable, "-c",
    "import os,signal,time; from pathlib import Path; os.setsid(); "
    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    f"Path({str(root / 'ready')!r}).touch(); time.sleep(90)"])
while not (root / "ready").exists():
    time.sleep(.01)
(root / "pids.json").write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(90)
'''


def identity(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


class WorkerPipes(unittest.TestCase):
    def exercise_disconnect(self, closed):
        with tempfile.TemporaryDirectory(prefix="advisor-worker-pipes-") as directory:
            root = Path(directory)
            (root / "bin").mkdir(mode=0o700)
            stub = root / "bin/claude"
            stub.write_text(STUB)
            stub.chmod(0o700)
            source = Path(__file__).resolve().parents[1] / "rust-runtime/src"
            worker = hashlib.sha256((source / "advisor.rs").read_bytes() +
                                    (source / "advisor_claude.rs").read_bytes()).hexdigest()
            prompt = "Private closed-pipe cancellation regression."
            request = dict(protocol=1, worker=worker, model=MODEL, effort="xhigh",
                           request_id=str(uuid.uuid4()), prompt=prompt,
                           prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
            env = dict(os.environ, HOME=str(root), CODEX_HOME=str(root / "home"),
                       CLAUDE_FIXTURE=str(root), PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
            env.pop("CODEX_THREAD_ID", None)
            process = subprocess.Popen([str(RUNTIME), "advisor", "--remote-worker"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env)
            descendants = {}
            try:
                process.stdin.write(json.dumps(request).encode() + b"\n")
                process.stdin.flush()
                wait_for(lambda: (root / "pids.json").exists())
                pids = json.loads((root / "pids.json").read_text())
                descendants = {pid: identity(pid) for pid in pids}
                self.assertTrue(all(alive(pid) for pid in pids))
                self.assertEqual(os.getsid(pids[1]), pids[1])
                getattr(process, closed).close()
                # EOF is the real remote cancellation signal; cleanup must precede output.
                process.stdin.close()
                process.wait(timeout=15)
                wait_for(lambda: not any(alive(pid) and identity(pid) == start
                                         for pid, start in descendants.items()))
                if closed == "stderr":
                    output = process.stdout.read().decode()
                    self.assertEqual(process.returncode, 0, output)
                    envelope = json.loads(output)
                    expected = {key: request[key] for key in
                                ("protocol", "worker", "model", "effort", "request_id", "prompt_sha256")}
                    self.assertEqual(envelope, dict(expected, result=None, error="interrupted"))
                else:
                    diagnostic = process.stderr.read().decode()
                    self.assertNotIn("panicked", diagnostic)
                    self.assertEqual(process.returncode, 1, diagnostic)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                for pid, start in descendants.items():
                    if alive(pid) and identity(pid) == start:
                        os.kill(pid, signal.SIGKILL)

    def test_closed_stderr_keeps_request_bound_cancellation_envelope(self):
        self.exercise_disconnect("stderr")

    def test_closed_stdout_fails_without_panic_after_cleanup(self):
        self.exercise_disconnect("stdout")


if __name__ == "__main__":
    unittest.main()
