"""Production jobs in a dedicated SSH login scope, controlled by an external logout gate.

Launch this script independently of the SSH command's lifetime. The controller waits
for evidence/ready.json, closes that dedicated non-multiplexed login, verifies its
logind state, then creates evidence/login-closed. All daemons and workloads are private.
"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fixtures.detached_jobs import DetachedJobServer
from test_background_integration import wait
from test_detached_jobs_stock import finished, launched, operations


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def process_facts(pid):
    process = Path("/proc") / str(pid)
    fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, state=fields[0], parent=int(fields[1]), pgid=int(fields[2]),
        sid=int(fields[3]), start_ticks=int(fields[19]),
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        cgroup=(process / "cgroup").read_text(),
        executable=str((process / "exe").resolve(strict=True)))


def login_state(session):
    result = subprocess.run(["loginctl", "show-session", session, "--property=State", "--value"],
        capture_output=True, text=True, timeout=5, env=dict(os.environ, LC_ALL="C"))
    return dict(returncode=result.returncode, state=result.stdout.strip(), error=result.stderr.strip())


def run(runtime, stock, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    evidence.chmod(0o700)
    work_deadline = time.monotonic() + 105
    fixture = None
    passed = False
    report = dict(passed=False, remote_model_requests=0, timeout_seconds=120)

    def timeout(_signal, _frame):
        raise TimeoutError("dedicated-login fixture exceeded its 120-second budget")

    previous_handler = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(120)

    def bounded(check, reason, seconds=20):
        remaining = work_deadline - time.monotonic()
        assert remaining > 0, "dedicated-login fixture work budget exhausted"
        return wait(check, reason, timeout=min(seconds, remaining))

    def stage(name, **facts):
        report.setdefault("stages", []).append(dict(stage=name, time=time.time(), **facts))
        atomic_json(evidence / "session-progress.json", report)

    try:
        controller_gate = evidence / "login-closed"
        assert not controller_gate.exists(), "login-closed gate already exists; use fresh evidence"
        own = process_facts(os.getpid())
        match = re.search(r"/session-([0-9]+)\.scope(?:/|\n|$)", own["cgroup"])
        assert match, "fixture must start inside a dedicated numeric SSH session-N.scope"
        session = match[1]
        environment_session = os.environ.get("XDG_SESSION_ID")
        assert environment_session in (None, "", session), (environment_session, own)
        kill_users = subprocess.run(["busctl", "get-property", "org.freedesktop.login1",
            "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "KillUserProcesses"],
            capture_output=True, text=True, timeout=5)
        assert kill_users.returncode == 0 and kill_users.stdout.strip() == "b false", (
            "requires live logind KillUserProcesses=false", kill_users.stdout, kill_users.stderr)
        original_login = login_state(session)
        assert original_login["returncode"] == 0 and original_login["state"] in ("active", "online"), original_login
        report.update(session_id=session, script=own, login_before=original_login,
            logind=dict(kill_user_processes=False, evidence=kill_users.stdout.strip()),
            shell_session=dict(XDG_SESSION_ID=environment_session, SSH_TTY=os.environ.get("SSH_TTY"),
                               SSH_CONNECTION_present=bool(os.environ.get("SSH_CONNECTION"))))
        fixture = DetachedJobServer(runtime, stock, evidence)
        f = fixture
        assert f.report["stock_version"] == "codex-cli 0.163.0", f.report
        jobs = []

        def start_job(topic):
            manifest, directory = f.manifest(topic, deadline=110)
            source = directory / "workload.py"
            script = source.read_text()
            assert "time.monotonic()+25" in script
            source.write_text(script.replace("time.monotonic()+25", "time.monotonic()+105"))
            command = f.commands(operations(manifest))
            ident, running = launched(f, command)
            f.finish(command)
            bounded(lambda: f.thread()["status"]["type"] == "idle", "job origin did not become idle")
            executor = process_facts(running["executor"]["pid"])
            assert executor["start_ticks"] == running["executor"]["start_ticks"]
            assert executor["cgroup"] == own["cgroup"], executor
            item = dict(id=ident, directory=directory, executor=executor,
                        log=f.job_directory(ident) / "stdout.log")
            jobs.append(item)
            return item

        def unchanged(item):
            executor = process_facts(item["executor"]["pid"])
            for key in ("pid", "start_ticks", "boot_id", "cgroup", "executable"):
                assert executor[key] == item["executor"][key], (key, executor, item)
            assert executor["state"] != "Z", executor
            assert f.job(item["id"])["state"] == "running"
            size = item["log"].stat().st_size
            bounded(lambda: item["log"].stat().st_size > size,
                    "same execution stopped logging across logout/restart", seconds=5)
            assert (item["directory"] / "runs").read_text() == "run\n"
            return executor

        first = start_job("before-logout")
        daemon = process_facts(f.native.pid)
        assert daemon["cgroup"] == own["cgroup"], daemon
        ready = dict(schema=1, session_id=session, session_scope=f"session-{session}.scope",
            script=own, daemon=daemon, executor=first["executor"], execution=first["id"],
            private_home=str(f.home), login_before=original_login, logind=report["logind"],
            controller_gate=str(controller_gate), fixture_deadline_seconds=120)
        atomic_json(evidence / "ready.json", ready)
        stage("ready-before-logout", daemon=daemon, executor=first["executor"])
        bounded(controller_gate.exists, "controller did not confirm dedicated SSH logout", seconds=65)
        after_login = login_state(session)
        absent = (after_login["returncode"] != 0 and "session" in after_login["error"].lower()
                  and any(text in after_login["error"].lower()
                          for text in ("no session", "not found", "does not exist")))
        assert after_login["state"] == "closing" or absent, after_login
        after_daemon = process_facts(f.native.pid)
        for key in ("pid", "start_ticks", "boot_id", "cgroup", "executable"):
            assert after_daemon[key] == daemon[key], (key, after_daemon, daemon)
        assert after_daemon["state"] != "Z", after_daemon
        stage("login-closed", login=after_login, executor=unchanged(first), daemon=after_daemon,
              controller_evidence=controller_gate.read_text()[:8192])

        # This native launch runs in the same now-closing login scope, after logout.
        second = start_job("after-logout")
        stage("admitted-after-logout", executor=unchanged(second))

        for kind in ("graceful", "abrupt"):
            previous_daemon = f.native
            f.restart_daemon(abrupt=kind == "abrupt")
            replacement = process_facts(f.native.pid)
            assert replacement["cgroup"] == own["cgroup"], replacement
            def reconfirmed():
                state = f.state()
                for item in jobs:
                    watched = next(job for job in state["jobs"] if job["execution"] == item["id"])
                    park = next(park for park in state["parks"] if park["id"] == watched["park"])
                    if (park["generation"] != f.rpc.generation or park["recovery_generation"]
                            or watched["status"] != "armed" or watched["recovery_pending"] != 0):
                        return False
                return True
            bounded(reconfirmed, "replacement daemon did not reconfirm both running executions")
            stage(f"{kind}-daemon-restart", daemon=replacement,
                  previous_daemon_exit=previous_daemon.returncode,
                  executors=[unchanged(item) for item in jobs])

        before = f.provider.count
        outcomes = []
        for index, item in enumerate(jobs, 1):
            (item["directory"] / "release").touch()
            outcome = finished(f, item["id"], item["directory"])
            assert outcome["result"]["exit_code"] == 0, outcome
            bounded(lambda: f.delivered(index), "session-scope completion did not wake root exactly once")
            bounded(lambda: f.thread()["status"]["type"] == "idle", "completion wake stayed active")
            assert f.provider.count == before + index, "duplicate or missing completion wake"
            outcomes.append(outcome)
            if index == 1:
                unchanged(second)
        report.update(passed=True, outcomes=outcomes, completion_wakes=2,
                      login_after=after_login, final_state=f.state())
        f.report["dedicated_session"] = report
        passed = True
    except BaseException:
        report["failure"] = traceback.format_exc()
        if fixture:
            fixture.report["dedicated_session"] = report
        raise
    finally:
        try:
            if fixture:
                fixture.close(passed)
        finally:
            atomic_json(evidence / "session-result.json", report)
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
    print(json.dumps(dict(passed=True, session_id=report["session_id"], completion_wakes=2,
                          remote_model_requests=0, evidence=str(evidence / "session-result.json"))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--stock-binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    run(args.runtime.resolve(strict=True), args.stock_binary.resolve(strict=True), args.evidence.resolve())
