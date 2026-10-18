"""Small private-stock fixture; all execution and daemon state belongs to this test."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import time

from fixtures.custom_goal_server import CustomGoalServer
from fixtures.upstream_0163 import execute, function, message
from test_background_integration import coordinator_state, wait


class DetachedJobServer(CustomGoalServer):
    def __init__(self, runtime, binary, evidence, **kwargs):
        super().__init__(runtime, binary, evidence, **kwargs)
        self.supervisor_lock.close()
        self.supervisor_lock = None
        self.command_count = 0
        self.releases = []
        self.report.update(architecture=os.uname().machine, parent=self.parent)

    def manifest(self, name="job", *, keys=(), deadline=30, cleanup=None):
        directory = self.work / name
        directory.mkdir()
        release = directory / "release"
        self.releases.append(release)
        source = directory / "workload.py"
        source.write_text('''from pathlib import Path
import sys,time
p=Path(sys.argv[1])
with (p/'runs').open('a') as f: f.write('run\\n')
print('started',flush=True)
end=time.monotonic()+25
while not (p/'release').exists() and time.monotonic()<end:
    print('progress',flush=True); time.sleep(.08)
assert (p/'release').exists(), 'fixture release timeout'
print('complete',flush=True)
''')
        manifest = dict(schema=1, topic=name,
            argv=[str(Path(sys.executable).resolve()), "{inputs}/workload.py", str(directory)],
            cwd=str(self.work), inputs=[dict(source=str(source), path="workload.py")],
            env={}, exclusive_keys=list(keys), expected_duration_seconds=1,
            deadline_seconds=deadline, cancel_grace_seconds=1, cleanup=cleanup)
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest))
        return path, directory

    def commands(self, operations, *, pause_after=None, on_turn=None, followup_from=None):
        """Run the actual CLI under a stock native tool, never a forged actor env."""
        self.command_count += 1
        directory = self.work / f"native-{self.command_count}"
        directory.mkdir()
        self.releases.append(directory / "continue")
        spec = dict(runtime=str(self.runtime), operations=operations, pause_after=pause_after)
        (directory / "spec.json").write_text(json.dumps(spec))
        helper = directory / "driver.py"
        helper.write_text(NATIVE_DRIVER)
        command = shlex.join([str(Path(sys.executable).resolve()), str(helper), str(directory)])
        tool_gate = self.provider.gate()
        root_gate = self.provider.gate()
        before = self.provider.count
        args = dict(cmd=command, yield_time_ms=1000, max_output_tokens=1000, login=False)
        followup_gate = None
        if followup_from:
            followup_gate = self.provider.gate()
            self.provider.root_steps.append((function("followup_task", dict(target=self.parent,
                message="Run the private detached admission fixture"),
                "detached-child-followup", "collaboration"), followup_gate))
        self.provider.root_steps.extend([
            (execute("text(await tools.exec_command(" + json.dumps(args) + "));",
                     f"detached-{self.command_count}"), tool_gate),
            (message("detached fixture command returned"), root_gate)])
        params = dict(threadId=followup_from or self.parent,
            input=[dict(type="text", text="Run the private detached fixture command")])
        origin = self.rpc.call("turn/start", params)["turn"]["id"]
        if followup_from:
            wait(lambda: self.provider.count > before, "native followup provider request missing")
            self.provider.parent = self.parent
            followup_gate.set()
            def child_turn():
                rows = self.rpc.call("thread/turns/list", {"threadId": self.parent, "limit": 1})["data"]
                return rows and rows[0]["status"] == "inProgress" and rows[0]["id"]
            origin = wait(child_turn, "native child followup did not start")
        (directory / "turn").write_text(origin)
        if on_turn:
            wait(lambda: self.provider.count > before, "native originating provider request missing")
            on_turn(origin)
        tool_gate.set()
        wait(lambda: (directory / ("paused" if pause_after else "done")).exists(),
             "native detached CLI did not publish its result", timeout=18)
        if pause_after is None:
            wait(lambda: self.provider.count >= before + 2,
                 "native detached tool result did not return to the stock daemon")
        return dict(turn=origin, gate=root_gate, directory=directory)

    def responses(self, command):
        return json.loads((command["directory"] / "responses.json").read_text())

    def finish(self, command):
        (command["directory"] / "continue").touch()
        wait(lambda: (command["directory"] / "done").exists(), "native command stayed paused")
        if command["gate"]:
            command["gate"].set()
        assert self.terminal(command["turn"])["status"] == "completed"

    def job(self, ident):
        return self.cli("job", "status", "--parent", self.parent, "--id", ident)

    def job_directory(self, ident):
        return self.home / "codex-plus/executions/v1" / self.parent / ident

    def state(self):
        return coordinator_state(self.home)

    def coordinator_processes(self):
        processes = []
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                command = (entry / "cmdline").read_bytes().split(b"\0")
                role = next((role for role in (b"observe", b"supervise") if role in command), None)
                if (role and f"CODEX_HOME={self.home}".encode() in
                        (entry / "environ").read_bytes().split(b"\0")):
                    processes.append(dict(pid=int(entry.name), role=role.decode(),
                                          executable=str((entry / "exe").resolve(strict=True))))
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        return processes

    def delivered(self, count=1):
        rows = self.state()["callbacks"]
        return len(rows) == count and all(row["state"] == "delivered" for row in rows)

    def restart_daemon(self, abrupt=False, before_start=None):
        old = self.rpc.generation
        self.rpc.close()
        self.rpc = None
        self.native.send_signal(signal.SIGKILL if abrupt else signal.SIGTERM)
        self.native.wait(timeout=6)
        if before_start:
            before_start()
        self.launch()
        assert self.rpc.generation != old
        self.report.setdefault("daemon_generations", []).append([old, self.rpc.generation])

    def close(self, passed):
        for release in getattr(self, "releases", []):
            release.touch()
        end = time.monotonic() + 4
        while time.monotonic() < end:
            states = [json.loads(path.read_text()) for path in
                      self.home.glob("codex-plus/executions/v1/*/*/state.json")]
            if all(state["state"] in ("prepared", "completed", "failed", "cancelled", "unknown")
                   for state in states):
                break
            time.sleep(.05)
        # Receipts, logs and frozen manifests remain reviewable after private-home cleanup.
        execution_root = self.home / "codex-plus/executions"
        if execution_root.exists():
            shutil.copytree(execution_root, self.evidence / "executions", dirs_exist_ok=True)
        if self.work.exists():
            shutil.copytree(self.work, self.evidence / "work", dirs_exist_ok=True)
        super().close(passed)


NATIVE_DRIVER = '''import json,subprocess,sys,time
from pathlib import Path
p=Path(sys.argv[1]); spec=json.loads((p/'spec.json').read_text())
responses=[]; prepared={}
for index,operation in enumerate(spec['operations'],1):
    values={'{turn}':(p/'turn').read_text(),'{id}':prepared.get('id',''),
            '{digest}':prepared.get('review_digest','')}
    args=[values.get(str(arg),str(arg)) for arg in operation]
    result=subprocess.run([spec['runtime'],*args],capture_output=True,text=True,timeout=12)
    try: output=json.loads(result.stdout)
    except ValueError: output=None
    responses.append(dict(args=args,returncode=result.returncode,output=output,
                          stdout=result.stdout,stderr=result.stderr))
    if output and isinstance(output,dict) and 'review_digest' in output:
        prepared=output
    temporary=p/'responses.tmp'; temporary.write_text(json.dumps(responses)); temporary.replace(p/'responses.json')
    if spec.get('pause_after')==index:
        (p/'paused').touch(); end=time.monotonic()+20
        while not (p/'continue').exists() and time.monotonic()<end: time.sleep(.02)
        assert (p/'continue').exists(), 'native fixture continuation timeout'
(p/'done').touch()
print(json.dumps(responses))
'''
