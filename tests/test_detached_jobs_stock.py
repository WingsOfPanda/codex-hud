"""Production detached jobs through native tools on a private official 0.163 daemon.

python3 tests/test_detached_jobs_stock.py --runtime PATH --stock-binary PATH \\
    --scenario completion --evidence .build/detached-jobs/acceptance/completion
All provider requests are scripted loopback responses. No real model is called.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import time
import traceback
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fixtures.detached_jobs import DetachedJobServer
from fixtures.upstream_0163 import execute, function, message
from test_background_integration import wait
from test_rust_runtime_stock_integration import stop_rust_observers
from test_rust_runtime_upgrade import seal


def operations(manifest, duplicate=False):
    result = [["job", "prepare", "--manifest", str(manifest), "--turn", "{turn}"],
              ["job", "launch", "--id", "{id}", "--review-digest", "{digest}",
               "--reason", "Review the private detached result"]]
    if duplicate:
        result.append(result[-1])
    return result


def launched(f, command):
    rows = f.responses(command)
    assert all(row["returncode"] == 0 for row in rows), rows
    prepared, launch = rows[0]["output"], rows[1]["output"]
    ident = prepared["id"]
    assert launch["id"] == ident and launch["execution"]["id"] == ident, launch
    assert prepared["state"] == "prepared" and prepared["root"] == f.parent
    assert prepared["origin"]["turn"] == command["turn"], prepared
    current = wait(lambda: (job := f.job(ident))["state"] == "running" and job,
                   "execution never became running")
    assert current["executor"] and current["workload"], current
    assert current["observation"]["state"] == "armed", current
    jobs = f.state()["jobs"]
    watched = next(job for job in jobs if job["execution"] == ident)
    assert watched["kind"] == "execution" and watched["process"] is None and watched["item"] is None, watched
    return ident, current


def finished(f, ident, directory, state="completed"):
    current = wait(lambda: (job := f.job(ident))["result"] is not None and job,
                   "execution did not publish a durable result", timeout=12)
    result = current["result"]
    assert current["state"] == result["state"] == state, current
    assert result["log_complete"] and result["workload_attempted"], result
    assert (directory / "runs").read_text() == "run\n", "workload ran more than once"
    if state == "completed":
        assert (f.job_directory(ident) / "stdout.log").read_text().endswith("complete\n")
    durable = json.loads((f.job_directory(ident) / "result.json").read_text())
    assert abs(durable["finished_at"] - result["finished_at"]) < .00001, (durable, result)
    assert {k: v for k, v in durable.items() if k != "finished_at"} == {
        k: v for k, v in result.items() if k != "finished_at"}, (durable, result)
    f.report.setdefault("verified_results", []).append(current)
    return current


def completion(f):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest, duplicate=True))
    ident, running = launched(f, command)
    rows = f.responses(command)
    assert rows[2]["output"]["id"] == ident
    assert rows[2]["output"]["execution"]["executor"] == running["executor"], rows
    f.finish(command)
    wait(lambda: (directory / "runs").exists(), "workload side effect missing")
    # Expected duration is informational: the held job must outlive that estimate.
    time.sleep(1.1)
    assert f.job(ident)["state"] == "running"
    before = f.provider.count
    (directory / "release").touch()
    result = finished(f, ident, directory)
    assert result["result"]["exit_code"] == 0
    wait(lambda: f.delivered(), "idle root was not woken without a TUI")
    wait(lambda: f.thread()["status"]["type"] == "idle", "completion callback stayed active")
    assert f.provider.count == before + 1
    f.cli("observe", "--parent", f.parent)
    assert f.delivered(), "observer restart duplicated the completion"
    f.quiet(.3)


def restart(f, abrupt=False, observation_gap=False):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest))
    ident, running = launched(f, command)
    f.finish(command)
    wait(lambda: (directory / "runs").exists(), "workload never started")
    executor = running["executor"]
    old_requests = f.provider.count
    if observation_gap:
        stop_rust_observers(f.home)
    def complete_offline():
        (directory / "release").touch()
        finished(f, ident, directory)
    f.restart_daemon(abrupt, before_start=complete_offline if observation_gap else None)
    current = f.job(ident)
    assert current["executor"] == executor, current
    if observation_gap:
        f.observe()
    else:
        assert current["state"] == "running", current
    log = f.job_directory(ident) / "workload.log"
    if not log.exists():
        log = f.job_directory(ident) / "stdout.log"
    if not observation_gap:
        before = log.stat().st_size
        wait(lambda: log.stat().st_size > before, "workload logs stopped after daemon death")
        # Positive observation in the replacement generation permits LIVE completion.
        def reconfirmed():
            state = f.state()
            watched = next(job for job in state["jobs"] if job["execution"] == ident)
            park = next(park for park in state["parks"] if park["id"] == watched["park"])
            return (park["generation"] == f.rpc.generation and not park["recovery_generation"]
                    and watched["status"] == "armed" and watched["recovery_pending"] == 0)
        wait(reconfirmed, "replacement did not observe running execution")
    (directory / "release").touch()
    finished(f, ident, directory)
    wait(lambda: f.delivered(), "restarted daemon did not receive durable execution completion")
    wait(lambda: f.thread()["status"]["type"] == "idle", "restarted callback did not settle")
    callbacks = f.state()["callbacks"]
    text = json.dumps(callbacks)
    expected = "REPORT ONLY" if observation_gap else "LIVE COMPLETION"
    assert expected in text, (expected, callbacks)
    assert f.provider.count == old_requests + 1


def changed_input(f):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest), pause_after=1)
    with (directory / "workload.py").open("a") as out:
        out.write("# changed after review\n")
    (command["directory"] / "continue").touch()
    wait(lambda: (command["directory"] / "done").exists(), "changed-input launch did not return")
    rows = f.responses(command)
    assert rows[0]["returncode"] == 0 and rows[1]["returncode"] != 0, rows
    assert "changed" in (rows[1]["stderr"] + rows[1]["stdout"]).lower(), rows
    assert not (directory / "runs").exists()
    assert f.job(rows[0]["output"]["id"])["state"] == "prepared"
    f.finish(command)


def stale_turn(f):
    manifest, directory = f.manifest()
    prepare = f.commands(operations(manifest)[:1])
    record = f.responses(prepare)[0]["output"]
    assert record["state"] == "prepared", f.responses(prepare)
    f.finish(prepare)
    launch = f.commands([["job", "launch", "--id", record["id"],
                          "--review-digest", record["review_digest"]]])
    response = f.responses(launch)[0]
    assert response["returncode"] != 0 and "turn" in response["stderr"] + response["stdout"], response
    assert not (directory / "runs").exists()
    f.finish(launch)


def root_mismatch(f):
    manifest, directory = f.manifest()
    command = f.commands([["job", "prepare", "--parent", str(uuid.uuid4()),
                           "--manifest", str(manifest)]])
    response = f.responses(command)[0]
    assert response["returncode"] != 0 and "another root" in response["stderr"] + response["stdout"], response
    assert not (directory / "runs").exists()
    f.finish(command)


def restricted(f, policy=None, permissions=None):
    manifest, directory = f.manifest()
    command = shlex.join([str(f.runtime), "job", "prepare", "--manifest", str(manifest)])
    args = dict(cmd=command, yield_time_ms=1000, max_output_tokens=2000, login=False)
    before = f.provider.count
    f.provider.root_steps.extend([
        (execute("text(await tools.exec_command(" + json.dumps(args) + "));"), None),
        (message("restricted detached request was refused"), None)])
    params = dict(threadId=f.parent,
        input=[dict(type="text", text="Probe private restricted detached admission")])
    if policy:
        params["sandboxPolicy"] = policy
    else:
        params["permissions"] = permissions
    turn = f.rpc.call("turn/start", params)["turn"]["id"]
    wait(lambda: f.provider.count == before + 2, "restricted native result did not return")
    assert f.terminal(turn)["status"] == "completed"
    assert f.provider.count == before + 2
    output = json.dumps(f.provider.requests[-1]["body"])
    assert "permission" in output.lower() or "unrestricted" in output.lower(), output
    assert "exit_code" in output or "Exit code" in output, output
    assert not (directory / "runs").exists(), "restricted tool launched detached workload"
    assert f.cli("job", "list", "--parent", f.parent) == []


def native_child(f):
    root = f.parent
    f.provider.root_steps.extend([
        (function("spawn_agent", dict(task_name="detached_child_refusal", fork_turns="none",
            message="Finish the private child fixture"), "detached-native-child", "collaboration"), None),
        (message("private child prepared"), None)])
    turn = f.rpc.call("turn/start", dict(threadId=root,
        input=[dict(type="text", text="Start the private native child")]))["turn"]["id"]
    children = wait(lambda: f.rpc.call("thread/list", dict(parentThreadId=root,
        sourceKinds=["subAgentThreadSpawn"], useStateDbOnly=True))["data"], "native child missing")
    child = children[0]["id"]
    wait(lambda: f.rpc.call("thread/read", {"threadId": child})["thread"]["status"]["type"]
         in ("idle", "notLoaded"), "native child did not finish")
    assert f.terminal(turn)["status"] == "completed"
    f.parent = child
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest), followup_from=root)
    responses = f.responses(command)
    assert responses[0]["returncode"] != 0, responses
    assert "root" in responses[0]["stdout"].lower(), responses
    assert not (directory / "runs").exists()
    f.finish(command)
    f.report["refused_native_child"] = child
    f.parent = f.provider.parent = root


def expiry(f):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest))
    ident, _ = launched(f, command)
    f.finish(command)
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
        database.execute("UPDATE parks SET deadline=? WHERE parent=?", (time.time() - 90000, f.parent))
    # An old deadline must not expire the execution watch during subsequent observer ticks.
    time.sleep(.5)
    assert f.state()["parks"][0]["state"] == "armed", f.state()
    (directory / "release").touch()
    finished(f, ident, directory)
    wait(lambda: f.delivered(), "over-24-hour execution watch lost its completion")


def watch_recovery(f):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest))
    ident, running = launched(f, command)
    f.finish(command)
    stop_rust_observers(f.home)
    (f.job_directory(ident) / "observation.json").unlink()
    assert f.job(ident)["observation"]["state"] == "recovery_needed"
    recovery = f.commands([["job", "watch", "--id", ident,
                            "--reason", "Report the private observation recovery"]])
    response = f.responses(recovery)[0]
    assert response["returncode"] == 0, response
    assert response["output"]["watch"]["mode"] == "report_only", response
    assert response["output"]["execution"]["executor"] == running["executor"]
    f.finish(recovery)
    (directory / "release").touch()
    finished(f, ident, directory)
    wait(lambda: f.delivered(), "explicit watch recovery did not deliver")
    assert "REPORT ONLY" in f.state()["callbacks"][0]["message"]


def monitoring_registration_failure(f):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest, duplicate=True), pause_after=1)
    # A SQLite trigger fails the real registration transaction after the executor launches.
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
        database.execute("""CREATE TRIGGER fixture_fail_execution_watch BEFORE INSERT ON jobs
            WHEN NEW.kind='execution' BEGIN
            SELECT RAISE(FAIL, 'fixture post-spawn monitoring registration failure'); END""")
    (command["directory"] / "continue").touch()
    wait(lambda: (command["directory"] / "done").exists(), "failed registration did not return")
    responses = f.responses(command)
    assert responses[0]["returncode"] == 0, responses
    prepared = responses[0]["output"]
    ident = prepared["id"]
    for response in responses[1:]:
        assert response["returncode"] == 1, response
        assert response["output"]["id"] == ident, response
        assert response["output"]["monitoring"]["state"] == "recovery_needed", response
        assert "fixture post-spawn monitoring registration failure" in response["stdout"], response
    running = wait(lambda: (job := f.job(ident))["state"] == "running" and job,
                   "monitoring failure lost execution truth")
    assert running["executor"] == responses[1]["output"]["execution"]["executor"]
    assert running["executor"] == responses[2]["output"]["execution"]["executor"]
    assert running["observation"]["state"] == "recovery_needed"
    assert not any(f.state().values()), f.state()
    assert (directory / "runs").read_text() == "run\n", "failed monitoring replayed workload"
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
        database.execute("DROP TRIGGER fixture_fail_execution_watch")
    f.finish(command)
    bypass = f.commands([["park", "--execution", ident,
                          "--reason", "Attempt ordinary registration from a later root turn"]])
    refusal = f.responses(bypass)[0]
    assert bypass["turn"] != command["turn"]
    assert refusal["returncode"] != 0, refusal
    assert not any(f.state().values()), "later-turn park acquired a LIVE execution watch"
    retained = f.job(ident)
    assert retained["executor"] == running["executor"]
    assert retained["observation"]["state"] == "recovery_needed", retained
    assert (directory / "runs").read_text() == "run\n", "later-turn park replayed workload"
    f.finish(bypass)
    recovery = f.commands([["job", "watch", "--id", ident,
                            "--reason", "Report the failed monitoring registration"]])
    response = f.responses(recovery)[0]
    assert response["returncode"] == 0 and response["output"]["watch"]["mode"] == "report_only", response
    assert response["output"]["execution"]["executor"] == running["executor"]
    f.finish(recovery)
    (directory / "release").touch()
    finished(f, ident, directory)
    wait(lambda: f.delivered(), "post-spawn monitoring recovery did not deliver")
    assert "REPORT ONLY" in f.state()["callbacks"][0]["message"]
    f.report["registration_failures"] = responses[1:]
    f.report["later_turn_park_refusal"] = dict(turn=bypass["turn"], response=refusal)


def package_switch(f):
    old_package = f.runtime.parent.parent
    candidate = seal(f.root / "candidate", f.runtime, 3)
    seal(old_package, candidate / "bin/codex-plus-runtime", 2)
    installed_manifest = json.loads((old_package / "codex-plus.json").read_text())
    candidate_manifest = json.loads((candidate / "codex-plus.json").read_text())
    assert installed_manifest["files"] == candidate_manifest["files"]
    current = f.root / "current"
    current.symlink_to(old_package.name)
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest))
    ident, running = launched(f, command)
    f.finish(command)
    executor = running["executor"]
    executor_proc = Path("/proc") / str(executor["pid"])
    old_runtime = f.runtime
    assert executor_proc.joinpath("exe").resolve(strict=True) == old_runtime
    assert running["package"] == str(old_package)
    hold = f.cli("job", "maintenance", "enter")
    assert hold["held"] and hold["admission_quiet"], hold
    before = f.provider.count
    previous_owner = f.state()["jobs"][0]["observer_run"]
    replacement = f.root / ".next"
    replacement.symlink_to(candidate.name)
    os.replace(replacement, current)
    candidate_runtime = candidate / "bin/codex-plus-runtime"
    wait(lambda: any(process["role"] == "observe" and
                    process["executable"] == str(candidate_runtime)
                    for process in f.coordinator_processes()), "coordinator did not select compatible package")
    upgraded_processes = f.coordinator_processes()
    wait(lambda: any(job["execution"] == ident and job["observer_run"] != previous_owner
                     and job["recovery_pending"] == 0 for job in f.state()["jobs"]),
         "candidate coordinator did not reconfirm existing execution")
    assert executor_proc.joinpath("exe").resolve(strict=True) == old_runtime
    assert executor_proc.joinpath("cwd").resolve(strict=True) == old_package
    pinned = set()
    for path in executor_proc.joinpath("fd").iterdir():
        try:
            pinned.add(os.readlink(path))
        except FileNotFoundError:
            pass
    assert str(old_package) in pinned and str(old_runtime) in pinned, pinned
    current_execution = f.job(ident)
    assert current_execution["executor"] == executor and current_execution["state"] == "running"
    assert current_execution["package"] == str(old_package) and old_runtime.exists()
    log = f.job_directory(ident) / "stdout.log"
    size = log.stat().st_size
    wait(lambda: log.stat().st_size > size, "old executor stopped after current switch")
    assert f.provider.count == before, "package switch or maintenance called the provider"
    f.runtime = candidate_runtime
    f.cli("job", "maintenance", "leave")
    (directory / "release").touch()
    finished(f, ident, directory)
    wait(lambda: f.delivered(), "candidate coordinator lost old executor completion")
    f.report["package_switch"] = dict(old_package=str(old_package), candidate=str(candidate),
        installed_manifest=installed_manifest, candidate_manifest=candidate_manifest,
        executor=executor, old_package_pins=sorted(pinned), coordinator_processes=upgraded_processes,
        qualification="same native payload in compatible protocol-4 sibling packages; no upstream migration")


def deadline(f):
    manifest, directory = f.manifest(deadline=2)
    command = f.commands(operations(manifest))
    ident, _ = launched(f, command)
    f.finish(command)
    current = finished(f, ident, directory, "failed")
    assert current["result"]["termination"] == "deadline", current


def cancellation(f):
    cleanup = f.work / "cleanup.py"
    marker = f.work / "cleanup-done"
    cleanup.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('clean\\n')\n")
    spec = dict(argv=[str(Path(sys.executable).resolve()), str(cleanup)], cwd=str(f.work),
                env={}, deadline_seconds=5)
    manifest, directory = f.manifest(cleanup=spec)
    command = f.commands(operations(manifest))
    ident, _ = launched(f, command)
    f.finish(command)
    cancel = f.commands([["job", "cancel", "--id", ident]])
    assert f.responses(cancel)[0]["returncode"] == 0
    f.finish(cancel)
    current = finished(f, ident, directory, "cancelled")
    assert current["result"]["cleanup"]["status"] == "completed", current
    assert marker.read_text() == "clean\n"
    assert current["result"]["cleanup"]["log_complete"]


def offline_cancellation(f):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest))
    ident, _ = launched(f, command)
    f.finish(command)
    stop_rust_observers(f.home)
    f.rpc.close()
    f.rpc = None
    f.native.terminate()
    f.native.wait(timeout=6)
    result = f.cli("job", "cancel", "--parent", f.parent, "--id", ident)
    assert result["state"] in ("cancel_requested", "cancelled"), result
    finished(f, ident, directory, "cancelled")
    assert not f.state()["callbacks"], "offline cancellation invented a callback"


def maintenance(f):
    manifest, directory = f.manifest()
    command = f.commands(operations(manifest))
    ident, _ = launched(f, command)
    f.finish(command)
    hold = f.cli("job", "maintenance", "enter")
    assert hold["held"] is True
    blocked_manifest, blocked_directory = f.manifest("blocked")
    blocked = f.commands(operations(blocked_manifest))
    assert any(row["returncode"] != 0 for row in f.responses(blocked)), f.responses(blocked)
    assert not (blocked_directory / "runs").exists()
    f.finish(blocked)
    before = f.provider.count
    (directory / "release").touch()
    finished(f, ident, directory)
    f.quiet(.5)
    status = f.cli("job", "maintenance", "status")
    assert status["held"] is True and status["id"] == hold["id"], status
    manual_turn, manual_gate = f.start_turn(held=True)
    queued_gate = f.provider.gate()
    f.provider.root_steps.append((message("previously queued native work completed"), queued_gate))
    queued = f.rpc.call("thread/queue/add", dict(threadId=f.parent,
        clientUserMessageId="maintenance-fixture",
        input=[dict(type="text", text="queued native work must prevent quiet")]))["queuedSubmission"]
    status = f.cli("job", "maintenance", "status")
    assert status["admission_quiet"] is False, status
    assert any(row["id"] == queued["id"] for row in
               f.rpc.call("thread/queue/list", {"threadId": f.parent})["data"]), "hold erased queued work"
    manual_gate.set()
    assert f.terminal(manual_turn)["status"] == "completed"
    wait(lambda: f.provider.count == before + 2, "native queue did not become accepted work")
    status = f.cli("job", "maintenance", "status")
    assert status["admission_quiet"] is False, status
    queued_gate.set()
    wait(lambda: f.thread()["status"]["type"] == "idle", "accepted native work did not settle")
    before = f.provider.count
    f.cli("job", "maintenance", "leave")
    wait(lambda: f.delivered(), "maintenance leave did not release preserved completion")
    assert f.provider.count == before + 1


def goal_wait(f):
    f.supervisor_lock = (f.home / "codex-plus" / f"{f.parent}.supervisor.lock").open("w")
    fcntl.flock(f.supervisor_lock, fcntl.LOCK_EX)
    f.goal("provenance", "--cwd", f.work, "--load-kind", "fresh")
    f.goal("start", "--objective", "Verify exact detached execution goal wait")
    snapshot = f.goal()
    goal, epoch = snapshot["goal"], snapshot["input_epoch"]
    manifest, directory = f.manifest()
    ops = operations(manifest)
    ops[0].extend(["--goal", goal["id"], "--revision", str(goal["revision"]),
                   "--input-epoch", str(epoch)])
    binding = ["--id", goal["id"], "--revision", goal["revision"], "--input-epoch", epoch]
    def admit(turn):
        f.goal("admit", *binding, "--turn", turn)
        f.supervisor_lock.close()
        f.supervisor_lock = None
    command = f.commands(ops, on_turn=admit)
    ident, running = launched(f, command)
    assert running["origin"]["goal"] == {"id": goal["id"], "revision": goal["revision"]}
    f.goal("checkpoint", *binding, "--turn", command["turn"], "--checkpoint",
           json.dumps({"disposition": "wait", "jobs": [f"{f.parent}:execution:{ident}"],
                       "evidence": ["fixture:detached-launch-and-armed-watch"]}))
    f.finish(command)
    wait(lambda: f.goal()["goal"]["state"] == "waiting", "goal did not wait for exact execution")
    before = f.provider.count
    (directory / "release").touch()
    finished(f, ident, directory)
    wait(lambda: f.provider.count == before + 1, "execution result did not wake its admitted goal")
    current = f.goal()
    assert current["goal"]["id"] == goal["id"]
    f.report["goal_after_execution"] = current
    f.mutate("pause", current["goal"])


def resources(f):
    first_root = f.parent
    manifest, directory = f.manifest("first", keys=["fixture-exclusive"])
    first = f.commands(operations(manifest))
    first_id, _ = launched(f, first)
    f.finish(first)
    second_root = f.rpc.call("thread/start", {"cwd": str(f.work), "model": "gpt-6.1-sol",
        "approvalPolicy": "never", "sandbox": "danger-full-access"})["thread"]["id"]
    f.parent = f.provider.parent = second_root
    conflict_manifest, conflict_directory = f.manifest("conflict", keys=["fixture-exclusive"])
    conflict = f.commands(operations(conflict_manifest))
    responses = f.responses(conflict)
    assert responses[0]["returncode"] == 0 and responses[1]["returncode"] != 0, responses
    error = responses[1]["stderr"] + responses[1]["stdout"]
    assert "resource conflict" in error and first_id in error, responses
    assert not (conflict_directory / "runs").exists()
    f.finish(conflict)
    other_manifest, other_directory = f.manifest("independent", keys=["fixture-other"])
    other = f.commands(operations(other_manifest))
    other_id, _ = launched(f, other)
    f.finish(other)
    assert f.job(other_id)["state"] == "running"
    f.parent = first_root
    assert f.job(first_id)["state"] == "running", "independent root serialized existing execution"
    f.parent = f.provider.parent = second_root
    (other_directory / "release").touch()
    finished(f, other_id, other_directory)
    wait(lambda: any(row["parent"] == second_root and row["state"] == "delivered"
                     for row in f.state()["callbacks"]), "second root completion missing")
    f.parent = f.provider.parent = first_root
    (directory / "release").touch()
    finished(f, first_id, directory)
    wait(lambda: f.delivered(2), "first root completion missing")


SCENARIOS = {"completion": completion,
    "daemon-graceful": lambda f: restart(f),
    "daemon-abrupt": lambda f: restart(f, abrupt=True),
    "observation-gap": lambda f: restart(f, abrupt=True, observation_gap=True),
    "changed-input": changed_input,
    "stale-turn": stale_turn,
    "root-mismatch": root_mismatch,
    "read-only": lambda f: restricted(f, {"type": "readOnly", "networkAccess": False}),
    "workspace": lambda f: restricted(f, {"type": "workspaceWrite", "networkAccess": True}),
    "managed-unrestricted": lambda f: restricted(f, permissions="unrestricted"),
    "managed-network": lambda f: restricted(f, permissions="unrestricted"),
    "native-child": native_child,
    "cancellation": cancellation,
    "offline-cancellation": offline_cancellation,
    "maintenance": maintenance,
    "goal-wait": goal_wait,
    "resources": resources,
    "over-24-hours": expiry,
    "watch-recovery": watch_recovery,
    "monitoring-registration-failure": monitoring_registration_failure,
    "package-switch": package_switch,
    "deadline": deadline}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--stock-binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    args = parser.parse_args()
    runtime, binary = args.runtime.resolve(strict=True), args.stock_binary.resolve(strict=True)
    config = ""
    if args.scenario.startswith("managed-"):
        config = ('[permissions.unrestricted.filesystem]\n":root"="write"\n'
                  '[permissions.unrestricted.network]\nenabled=true\n')
        if args.scenario == "managed-network":
            config += '[features.network_proxy]\nenabled=true\n'
    f = DetachedJobServer(runtime, binary, args.evidence.resolve(), config=config)
    passed = False
    try:
        assert f.report["stock_version"] == "codex-cli 0.163.0", f.report
        assert str(f.home) != str(Path.home() / ".codex")
        f.report["scenario"] = args.scenario
        SCENARIOS[args.scenario](f)
        passed = True
    except BaseException:
        f.report["failure"] = traceback.format_exc()
        raise
    finally:
        f.close(passed)
    print(json.dumps(dict(passed=True, scenario=args.scenario, remote_model_requests=0,
                          evidence=str(args.evidence / "report.json"))))


if __name__ == "__main__":
    main()
