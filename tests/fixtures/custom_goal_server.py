"""Private stock-daemon fixture for Plus goal acceptance; providers stay loopback."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from codex_plus.background import classify_turn, turn
from fixtures.upstream_0163 import BoundaryProvider, message
from test_background_integration import RecordingClient, stop, wait
from test_rust_runtime_stock_integration import stop_rust_observers


class CustomGoalServer:
    def __init__(self, runtime, binary, evidence, *, managed=False, config=""):
        self.evidence = evidence
        evidence.mkdir(parents=True, exist_ok=True)
        evidence.chmod(0o700)
        self.temporary = tempfile.TemporaryDirectory(prefix="cg-")
        self.root = Path(self.temporary.name)
        self.home, self.work = self.root / "home", self.root / "work"
        self.home.mkdir(mode=0o700)
        self.work.mkdir()
        self.runtime = self.root / "package/bin/codex-plus-runtime"
        self.runtime.parent.mkdir(parents=True)
        shutil.copy2(runtime, self.runtime)
        self.binary, self.managed = binary, managed
        self.provider = BoundaryProvider()
        self.env = {key: value for key, value in os.environ.items()
                    if not any(secret in key.upper() for secret in
                               ("API_KEY", "AUTH_TOKEN", "BASE_URL"))}
        self.env.update(CODEX_HOME=str(self.home), RUST_LOG="warn")
        self.env.pop("CODEX_THREAD_ID", None)
        (self.home / "config.toml").write_text(f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
sandbox_mode="danger-full-access"
approval_policy="never"
analytics.enabled=false
thread_unload_delay_secs=1
features.daemon_auto_start=false
features.goals=false
features.plugins=false
features.multi_agent=true
features.multi_agent_v2=true
features.code_mode=true
features.unified_exec=true
features.shell_tool=true
{config}
[model_providers.isolated]
name="loopback custom goal acceptance"
base_url="{self.provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        self.log = (evidence / "daemon.log").open("w")
        self.processes = []
        self.report = {"runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                       "stock_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                       "stock_version": subprocess.check_output([str(binary), "--version"], text=True).strip(),
                       "remote_model_requests": 0, "cli": []}
        self.rpc = self.native = self.supervisor_lock = None
        try:
            self.launch()
            self.parent = self.rpc.call("thread/start", {"cwd": str(self.work),
                "model": "gpt-6.1-sol", "approvalPolicy": "never",
                "sandbox": "danger-full-access"})["thread"]["id"]
            self.provider.parent = self.parent
            directory = self.home / "codex-plus"
            directory.mkdir(mode=0o700, exist_ok=True)
            self.supervisor_lock = (directory / f"{self.parent}.supervisor.lock").open("w")
            fcntl.flock(self.supervisor_lock, fcntl.LOCK_EX)
        except BaseException:
            self.close(False)
            raise

    def launch(self, *, connect=True):
        self.native = subprocess.Popen([str(self.binary), "-c", "features.goals=false", "app-server",
            *(["--managed-daemon"] if self.managed else []), "--listen", "unix://"],
            env=self.env, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            start_new_session=True)

        if connect:
            self.connect()

    def connect(self):
        def connect():
            assert self.native.poll() is None, "private stock daemon exited: inspect daemon.log"
            try:
                return RecordingClient(self.home / "app-server-control/app-server-control.sock")
            except (OSError, ValueError):
                return None

        self.rpc = wait(connect, "private stock daemon did not accept RPC")
        assert not self.rpc.initialize_result.get("queueIdempotency"), "requires official stock daemon"

    def restart(self):
        self.rpc.close()
        self.rpc = None
        stop(self.native)
        self.launch()

    def cli(self, *args, ok=True):
        result = subprocess.run([str(self.runtime), *map(str, args)], env=self.env,
            cwd=self.work, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        record = dict(args=args, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        self.report["cli"].append(record)
        if ok:
            assert result.returncode == 0, record
        else:
            assert result.returncode != 0, record
        return json.loads(result.stdout) if ok and result.stdout.strip() else result

    def goal(self, action="status", *args, ok=True):
        return self.cli("goal", action, "--thread", self.parent, *args, "--json", ok=ok)

    def mutate(self, action, goal, *args, ok=True):
        return self.goal(action, "--id", goal["id"], "--revision", goal["revision"], *args, ok=ok)

    def start_turn(self, *, held=False, text="ordinary unrelated input"):
        before = self.provider.count
        gate = self.provider.gate() if held else None
        self.provider.root_steps.append((message("fixture turn result"), gate))
        ident = self.rpc.call("turn/start", {"threadId": self.parent,
            "input": [{"type": "text", "text": text}], "effort": "high"})["turn"]["id"]
        wait(lambda: self.provider.count > before,
             "root provider request missing")
        return ident, gate

    def terminal(self, ident):
        def check():
            current = turn(self.rpc, self.parent, ident)
            status, evidence = classify_turn(self.rpc, self.home, self.parent, current)
            if status in ("completed", "failed", "interrupted"):
                return dict(current, status=status, terminalEvidence=evidence)
        return wait(check, "exact stock turn did not finish")

    def thread(self):
        return self.rpc.call("thread/read", {"threadId": self.parent})["thread"]

    def observe(self):
        log = (self.evidence / f"observer-{len(self.processes)}.log").open("w")
        process = subprocess.Popen([str(self.runtime), "observe", "--parent", self.parent],
            env=self.env, cwd=self.work, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)
        self.processes.append((process, log))
        return process

    def quiet(self, seconds=1.2):
        before = self.provider.count
        time.sleep(seconds)
        assert self.provider.count == before, {"before": before, "after": self.provider.count,
                                              "evidence": str(self.evidence / "report.json")}
        self.report.setdefault("quiet_windows", []).append({"seconds": seconds, "requests": 0})

    def close(self, passed):
        for name in ("shell-release", "shell-release-a", "shell-release-b"):
            (self.work / name).touch()
        self.provider.close()
        for process, log in self.processes:
            stop(process)
            log.close()
        stop_rust_observers(self.home)
        if self.supervisor_lock:
            self.supervisor_lock.close()
        if self.rpc:
            self.report["rpc_messages"] = self.rpc.messages
            self.rpc.close()
        if self.native:
            stop(self.native)
        self.log.close()
        self.report.update(passed=passed, provider_requests=self.provider.requests)
        (self.evidence / "report.json").write_text(json.dumps(self.report, indent=2, default=str) + "\n")
        for source in (self.home / "codex-plus").glob("*"):
            if source.is_file() and source.suffix in (".json", ".sqlite"):
                shutil.copy2(source, self.evidence / source.name)
        self.temporary.cleanup()
