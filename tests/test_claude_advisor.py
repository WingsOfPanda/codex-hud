"""Private black-box contracts for the Claude advisor, without provider calls.

CODEX_PLUS_TEST_RUNTIME=/absolute/codex-plus-runtime python3 tests/test_claude_advisor.py
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

from fixtures.native_server import AppServer


RUNTIME = Path(os.environ["CODEX_PLUS_TEST_RUNTIME"]).resolve()
ROOT = "11111111-1111-4111-8111-111111111111"
MODEL = "claude-opus-5-5"
RESULT = {"verdict": "revise", "summary": "Review the supplied evidence.",
          "findings": [{"severity": "medium", "message": "Exercise disconnect cleanup."}],
          "next_steps": ["Verify cleanup."]}

FAKE_CLAUDE = r'''#!/usr/bin/python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ["CLAUDE_FIXTURE"])
args = sys.argv[1:]
prompt = sys.stdin.read()
(root / "invoked.json").write_text(json.dumps({"argv": args, "prompt": prompt}))
mode = (root / "mode").read_text()
if mode == "hold":
    child = subprocess.Popen([sys.executable, "-c",
        "import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(90)"])
    (root / "pids.json").write_text(json.dumps([os.getpid(), child.pid]))
    time.sleep(90)
if mode == "failure":
    print("fixture failure", file=sys.stderr)
    raise SystemExit(9)
if mode == "malformed":
    print("not json")
    raise SystemExit(0)
result = json.loads((root / "result.json").read_text())
if mode == "bad-schema":
    result["unexpected"] = True
envelope = {"type":"result","subtype":"success","is_error":False,
            "modelUsage":{"claude-opus-5-5":{"canonicalModel":"claude-opus-5-5"}},
            "structured_output":result}
if mode == "wrong-model":
    envelope["modelUsage"] = {"claude-fable-5-1":{"canonicalModel":"claude-fable-5-1"}}
if mode == "api-error":
    envelope["is_error"] = True
    envelope["subtype"] = "error_during_execution"
print(json.dumps(envelope))
'''


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.025)
    raise AssertionError("condition did not become true")


def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


class ClaudeAdvisor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-advisor-contract-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.claude = self.bin / "claude"
        self.claude.write_text(FAKE_CLAUDE)
        self.claude.chmod(0o700)
        (self.root / "mode").write_text("success")
        (self.root / "result.json").write_text(json.dumps(RESULT))
        socket = self.home / "app-server-control/app-server-control.sock"
        socket.parent.mkdir()
        self.server = AppServer(ROOT, self.workspace, 0, models=[], socket_path=socket)
        self.env = dict(os.environ, CODEX_HOME=str(self.home), CODEX_THREAD_ID=ROOT,
                        CLAUDE_FIXTURE=str(self.root),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        pids = self.root / "pids.json"
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)
        self.server.close()
        self.temp.cleanup()

    def command(self, *args, host="local"):
        return [str(RUNTIME), "advisor", "--brief",
                "Review this general engineering question; preserve technical constraints.",
                "--host", host, *args]

    def run_cli(self, *args, host="local"):
        return subprocess.run(self.command(*args, host=host), cwd=self.workspace,
                              env=self.env, capture_output=True, text=True, timeout=15)

    def status(self):
        return json.loads((self.home / "codex-plus" / f"{ROOT}.advisor.json").read_text())

    def test_help_is_self_contained_without_daemon(self):
        result = subprocess.run([str(RUNTIME), "advisor", "--help"],
                                env=dict(self.env, CODEX_HOME=str(self.root / "missing"),
                                         CODEX_THREAD_ID=""),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ["claude-opus-5-5", "xhigh", "xjp-l20-2",
                         "--context-file", "foreground", "cancel"]:
            self.assertIn(expected, result.stdout)

    def test_default_claude_uses_xhigh_and_only_explicit_context(self):
        evidence = self.workspace / "review file ' $(touch unwanted).txt"
        evidence.write_text("Exact input evidence 汉字.\n")
        (self.workspace / "not-supplied.txt").write_text("OMITTED_PRIVATE_CONTENT")
        result = self.run_cli("--context-file", str(evidence))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual({key: value[key] for key in RESULT}, RESULT)
        self.assertEqual(value["model"], MODEL)
        self.assertEqual(value["effort"], "xhigh")
        invoked = json.loads((self.root / "invoked.json").read_text())
        args = invoked["argv"]
        for flag in ["--safe-mode", "--restricted", "--strict-mcp-config",
                     "--no-session-persistence", "--print"]:
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--model") + 1], MODEL)
        self.assertEqual(args[args.index("--effort") + 1], "xhigh")
        self.assertIn("Exact input evidence 汉字.", invoked["prompt"])
        self.assertNotIn("OMITTED_PRIVATE_CONTENT", invoked["prompt"])
        self.assertFalse((self.workspace / "unwanted").exists())
        status = self.status()
        self.assertEqual((status["state"], status["model"], status["effort"], status["calls"]),
                         ("done", MODEL, "xhigh", 1))
        self.assertNotIn("Exact input evidence", json.dumps(status))
        self.assertEqual(Path(value["log"]).stat().st_mode & 0o777, 0o600)
        methods = {entry["method"] for entry in self.server.requests}
        self.assertLessEqual(methods, {"initialize", "initialized", "thread/read"})

    def test_invalid_responses_never_become_advice(self):
        for mode in ["failure", "malformed", "bad-schema", "wrong-model", "api-error"]:
            with self.subTest(mode=mode):
                (self.root / "mode").write_text(mode)
                result = self.run_cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.stdout.strip(), result.stdout)
                self.assertEqual(self.status()["state"], "error")

    def test_invalid_context_never_launches_claude(self):
        target = self.workspace / "target"
        target.write_text("contents")
        link = self.workspace / "link"
        link.symlink_to(target)
        binary = self.workspace / "binary"
        binary.write_bytes(b"\xff")
        oversized = self.workspace / "large"
        oversized.write_bytes(b"x" * (512 * 1024 + 1))
        for path in [link, binary, oversized, self.workspace, self.workspace / "missing"]:
            with self.subTest(path=path.name):
                result = self.run_cli("--context-file", str(path))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "invoked.json").exists())

    def check_model_mismatch_diagnostic(self, host="local", remote_command=None):
        (self.root / "mode").write_text("wrong-model")
        private_result = dict(RESULT, summary="RESPONSE_MUST_NOT_BE_LOGGED")
        (self.root / "result.json").write_text(json.dumps(private_result))
        args = ["--remote-command", str(remote_command)] if remote_command else []
        result = self.run_cli(*args, host=host)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()["error"], "model_mismatch")
        self.assertFalse(result.stdout.strip(), result.stdout)
        log = Path(result.stderr.strip().split("log=", 1)[1]).read_text()
        prefix = "advisor model metadata: "
        diagnostics = [json.loads(line[len(prefix):]) for line in log.splitlines()
                       if line.startswith(prefix)]
        self.assertEqual(len(diagnostics), 1, log[-1000:])
        metadata = diagnostics[0]
        self.assertEqual(metadata["expected_model"], MODEL)
        self.assertEqual([{key: model[key] for key in ("model", "canonical_model")}
                          for model in metadata["models"]], [
            {"model": "claude-fable-5-1", "canonical_model": "claude-fable-5-1"}])
        self.assertEqual(metadata["model_usage_type"], "object")
        self.assertEqual(metadata["model_count"], 1)
        self.assertEqual(metadata["models"][0]["canonical_model_type"], "string")
        self.assertNotIn("RESPONSE_MUST_NOT_BE_LOGGED", log)
        self.assertNotIn("Review this general engineering question", json.dumps(metadata))

    def test_model_mismatch_logs_returned_metadata(self):
        self.check_model_mismatch_diagnostic()

    @unittest.skipUnless(os.environ.get("CODEX_PLUS_TEST_SSH_HOST"),
                         "set CODEX_PLUS_TEST_SSH_HOST for real SSH diagnostics")
    def test_real_ssh_model_mismatch_logs_returned_metadata(self):
        self.check_model_mismatch_diagnostic(
            host=os.environ["CODEX_PLUS_TEST_SSH_HOST"],
            remote_command=self.remote_fixture())

    def test_root_required(self):
        self.env["CODEX_THREAD_ID"] = ""
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root_required", result.stderr)
        self.assertFalse((self.root / "invoked.json").exists())

    def test_cancel_reaps_detached_descendants_and_excludes_second_call(self):
        (self.root / "mode").write_text("hold")
        child = subprocess.Popen(self.command(), cwd=self.workspace, env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        wait_for(lambda: (self.root / "pids.json").exists())
        pids = json.loads((self.root / "pids.json").read_text())
        second = self.run_cli()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already_running", second.stderr)
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertFalse(stdout.strip(), stdout)
        self.assertIn("interrupted", stderr)
        wait_for(lambda: not any(alive(pid) for pid in pids))
        self.assertEqual(self.status()["state"], "error")
        self.assertEqual(self.status()["calls"], 1)

    def remote_fixture(self):
        """An isolated remote command; never changes installed helpers or SSH config."""
        wrapper = self.bin / "remote helper ' with spaces"
        wrapper.write_text(
            "#!/usr/bin/python3\nimport os,sys\n"
            f"os.environ['PATH']={self.env['PATH']!r}\n"
            f"os.environ['CLAUDE_FIXTURE']={str(self.root)!r}\n"
            f"os.execv({str(RUNTIME)!r}, [{str(RUNTIME)!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o700)
        return wrapper

    @unittest.skipUnless(os.environ.get("CODEX_PLUS_TEST_SSH_HOST"),
                         "set CODEX_PLUS_TEST_SSH_HOST for same-host real SSH acceptance")
    def test_real_ssh_returns_matching_claude_result(self):
        wrapper = self.remote_fixture()
        result = self.run_cli("--remote-command", str(wrapper),
                              host=os.environ["CODEX_PLUS_TEST_SSH_HOST"])
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["model"], MODEL)
        self.assertEqual(value["summary"], RESULT["summary"])
        self.assertEqual(self.status()["state"], "done")

    @unittest.skipUnless(os.environ.get("CODEX_PLUS_TEST_SSH_HOST"),
                         "set CODEX_PLUS_TEST_SSH_HOST for same-host real SSH acceptance")
    def test_real_ssh_caller_death_reaps_remote_detached_descendants(self):
        wrapper = self.remote_fixture()
        (self.root / "mode").write_text("hold")
        child = subprocess.Popen(
            self.command("--remote-command", str(wrapper),
                         host=os.environ["CODEX_PLUS_TEST_SSH_HOST"]),
            cwd=self.workspace, env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        wait_for(lambda: (self.root / "pids.json").exists())
        pids = json.loads((self.root / "pids.json").read_text())
        child.kill()
        child.communicate(timeout=10)
        wait_for(lambda: not any(alive(pid) for pid in pids), seconds=15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
