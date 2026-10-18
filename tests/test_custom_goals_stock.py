"""Custom goal acceptance against a private official daemon and loopback provider.

python3 tests/test_custom_goals_stock.py --runtime PATH --stock-binary PATH --evidence PATH
No installed runtime, production state, external provider, or daemon database is modified.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fixtures.custom_goal_server import CustomGoalServer
from fixtures.upstream_0163 import execute, function, message
from test_background_integration import coordinator_state, stop, wait
from test_rust_runtime_stock_integration import stop_rust_observers


def current(f):
    snapshot = f.goal()
    assert snapshot["version"] == 1 and snapshot["root"] == f.parent, snapshot
    assert isinstance(snapshot["input_epoch"], int), snapshot
    assert snapshot["report_barrier"] in (0, 1), snapshot
    return snapshot


def start(f, objective="Verify private custom goal acceptance", paused=True, expected=None):
    f.goal("provenance", "--cwd", f.work, "--load-kind", "fresh")
    identity = ("--id", expected["id"], "--revision", expected["revision"]) if expected else ()
    f.goal("start", "--objective", objective, *(["--paused"] if paused else []), *identity)
    goal = current(f)["goal"]
    assert goal["objective"] == objective and goal["root"] == f.parent, goal
    assert goal["state"] == ("paused" if paused else "active"), goal
    return goal


def admit(f, goal, turn, epoch=None):
    epoch = current(f)["input_epoch"] if epoch is None else epoch
    args = ("--id", goal["id"], "--revision", goal["revision"],
            "--turn", turn, "--input-epoch", epoch)
    f.goal("admit", *args)
    return args


def checkpoint(f, binding, disposition, ok=True):
    return f.goal("checkpoint", *binding, "--checkpoint", json.dumps(disposition), ok=ok)


def cli_transactions(f):
    """CAS must reject stale clients, preserve replaced history, and keep reads private."""
    before = f.provider.count
    first = start(f)
    assert first["revision"] >= 1
    f.mutate("review", first, "--review-seconds", "3600")
    first = current(f)["goal"]
    assert first["state"] == "paused" and first["review_seconds"] == 3600, first
    f.mutate("edit", first, "--objective", "Edited objective")
    edited = current(f)["goal"]
    assert edited["id"] == first["id"] and edited["revision"] > first["revision"], edited
    f.mutate("complete", first, ok=False)
    assert current(f)["goal"] == edited, "stale CAS modified edited goal"
    f.mutate("resume", edited)
    resumed = current(f)["goal"]
    assert resumed["state"] == "active" and resumed["revision"] > edited["revision"], resumed
    f.mutate("pause", resumed)
    paused = current(f)["goal"]
    assert paused["state"] == "paused", paused
    assert paused["review_seconds"] == 3600, "pause/resume discarded the chosen interval"
    f.mutate("review", paused, "--review-seconds", "0")
    paused = current(f)["goal"]
    assert paused["review_seconds"] is None, paused
    replacement = start(f, "Replacement preserves old results", expected=paused)
    assert replacement["id"] != first["id"], replacement
    f.mutate("resume", paused, ok=False)
    history = f.goal("history")
    assert first["id"] in json.dumps(history) and replacement["id"] in json.dumps(history), history
    f.mutate("clear", replacement)
    snapshot = current(f)
    assert snapshot["goal"] is None, snapshot
    assert f.provider.count == before, "CLI mutation or read called the model"
    directory = f.home / "codex-plus"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    database = directory / "background.sqlite"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    projection = directory / "goals" / f"{f.parent}.json"
    assert stat.S_IMODE(projection.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(projection.stat().st_mode) == 0o600
    projection_before = (projection.read_bytes(), projection.stat().st_mtime_ns)
    before_read = database.read_bytes()
    f.goal()
    f.goal("history")
    assert database.read_bytes() == before_read, "read-only goal projection changed durable state"
    assert (projection.read_bytes(), projection.stat().st_mtime_ns) == projection_before
    f.report["final_snapshot"] = snapshot


def checkpoint_before_eos_crash(f):
    """A saved Continue before EOS cannot authorize a turn after a daemon crash."""
    goal = start(f, paused=False)
    turn, gate = f.start_turn(held=True, text="admitted goal work without terminal evidence")
    binding = admit(f, goal, turn)
    checkpoint(f, binding, {"disposition": "continue", "next_action": "Must await successful EOS",
                             "evidence": ["fixture:pre-eos"]})
    before = f.provider.count
    observer = f.observe()
    f.quiet(.5)
    assert f.thread()["status"]["type"] == "active", f.thread()
    # SIGKILL excludes a graceful completed event from the exact checkpoint turn.
    f.rpc.close()
    f.rpc = None
    os.killpg(f.native.pid, signal.SIGKILL)
    f.native.wait(timeout=5)
    gate.set()
    f.launch()
    wait(lambda: current(f)["goal"]["state"] == "needs_input",
         "pre-EOS crash did not stop automatic continuation", timeout=15)
    f.quiet()
    assert f.provider.count == before, "staged checkpoint replayed after failed/interrupted turn"
    assert observer.poll() in (None, 0), "observer failed instead of surfacing uncertainty"
    f.report["final_snapshot"] = current(f)


def input_epoch(f):
    """Steering keeps a turn ID, so epoch changes must reject its older checkpoint."""
    goal = start(f, paused=False)
    turn, gate = f.start_turn(held=True, text="goal turn before steering")
    binding = admit(f, goal, turn)
    epoch = current(f)["input_epoch"]
    submission = f.goal("input")["submission_id"]
    assert current(f)["input_epoch"] == epoch + 1
    checkpoint(f, binding, {"disposition": "continue", "next_action": "stale instruction",
                             "evidence": ["fixture:stale-input"]}, ok=False)
    f.rpc.call("turn/steer", {"threadId": f.parent, "expectedTurnId": turn,
                              "input": [{"type": "text", "text": "New direction takes priority"}]})
    f.goal("input-settle", "--submission-id", submission, "--turn", turn, "--accepted")
    gate.set()
    assert f.terminal(turn)["status"] == "completed"
    before = current(f)["goal"]["no_progress"]
    unrelated_submission = f.goal("input")["submission_id"]
    unrelated, _ = f.start_turn(text="ordinary unrelated question")
    assert f.terminal(unrelated)["status"] == "completed"
    f.goal("input-settle", "--submission-id", unrelated_submission, "--turn", unrelated, "--accepted")
    followup_gate = f.provider.gate()
    f.provider.root_steps.append((message("goal reassessment after ordinary input"), followup_gate))
    count = f.provider.count
    observer = f.observe()
    wait(lambda: f.provider.count == count + 1, "goal did not reassess after unrelated user input")
    after = current(f)["goal"]
    assert after["no_progress"] == before, "unrelated/steered turn counted as failed goal progress"
    f.mutate("pause", after)
    followup_gate.set()
    wait(lambda: f.thread()["status"]["type"] == "idle", "paused reassessment did not settle")
    f.quiet()
    assert observer.poll() in (None, 0)
    f.report["final_snapshot"] = current(f)


def stock_isolation(f):
    """Fresh, unloaded, and managed restores hide native tools without model polling."""
    def probe(label):
        before = f.provider.count
        turn, _ = f.start_turn(text=f"isolated stock feature probe {label}")
        assert f.terminal(turn)["status"] == "completed"
        request = f.provider.requests[before]["body"]
        inventory = json.dumps(request.get("tools", []) + [item for item in request.get("input", [])
                                if item.get("type") == "additional_tools"])
        assert not any(name in inventory for name in ("create_goal", "get_goal", "update_goal")), inventory
        f.report.setdefault("native_isolation", []).append({"loading": label, "turn": turn})

    probe("fresh")
    f.rpc.call("thread/unsubscribe", {"threadId": f.parent})
    wait(lambda: f.thread()["status"]["type"] == "notLoaded", "idle root did not unload")
    generation = f.rpc.generation
    f.rpc.call("thread/resume", {"threadId": f.parent})
    assert f.rpc.generation == generation
    probe("same-generation-unloaded")
    f.restart()
    if f.managed:
        wait(lambda: f.thread()["status"]["type"] != "notLoaded", "managed root did not restore")
        f.report["managed_restored_before_attach"] = f.thread()
        f.rpc.call("thread/resume", {"threadId": f.parent})
    else:
        assert f.thread()["status"]["type"] == "notLoaded"
        f.rpc.call("thread/resume", {"threadId": f.parent})
    f.quiet()
    probe("managed-restore" if f.managed else "cold-resume")


def waiting_job(f, worker=False, finish_before_eos=False, register_after_finish=False, replace_then_interrupt=False):
    goal = start(f, paused=False)
    root_gate = f.provider.gate()
    if worker:
        job_gate = f.provider.gate()
        f.provider.child_gates.append(job_gate)
        item = function("spawn_agent", {"task_name": "goal_acceptance", "fork_turns": "none",
                         "message": "Return the isolated fixture result"}, "goal-worker", "collaboration")
        release = job_gate.set
    else:
        release_file = f.work / "shell-release"
        runs = f.work / "shell-runs"
        shell = (f'printf "run\\n" >> {shlex.quote(str(runs))}; '
                 f'i=0; while [ ! -f {shlex.quote(str(release_file))} ] && [ "$i" -lt 400 ]; '
                 'do sleep .1; i=$((i+1)); done; '
                 f'test -f {shlex.quote(str(release_file))} || exit 124; printf "goal-shell-complete\\n"')
        command = "# codex-plus-shell: auto\n" + shlex.join([str(f.runtime), "run", "--", "/bin/sh", "-c", shell])
        item = execute("text(await tools.exec_command(" + json.dumps({"cmd": command,
                       "yield_time_ms": 1000, "max_output_tokens": 100, "login": False}) + "));")
        release = release_file.touch
    f.provider.root_steps.extend([(item, None), (message("goal work is registered"), root_gate)])
    origin = f.rpc.call("turn/start", {"threadId": f.parent,
        "input": [{"type": "text", "text": "Execute the exact finite goal job"}]})["turn"]["id"]
    if worker:
        children = wait(lambda: f.rpc.call("thread/list", {"parentThreadId": f.parent,
            "sourceKinds": ["subAgentThreadSpawn"], "useStateDbOnly": True})["data"],
            "native goal child did not start")
        assert len(children) == 1, children
        selector = ("--worker", children[0]["id"])
        wait(lambda: any(request["child"] for request in f.provider.requests), "child provider request missing")
    else:
        terminals = wait(lambda: f.rpc.call("thread/backgroundTerminals/list",
            {"threadId": f.parent})["data"], "native goal shell did not start")
        selector = ("--process", terminals[0]["processId"])
    wait(lambda: len([request for request in f.provider.requests if not request["child"]]) == 2,
         "goal origin did not finish its finite tool call")
    binding = admit(f, goal, origin)
    if register_after_finish:
        release()
        wait(lambda: not f.rpc.call("thread/backgroundTerminals/list", {"threadId": f.parent})["data"],
             "already-finished shell remained live")
    registration = f.cli("park", "--parent", f.parent, *selector, "--reason", "goal acceptance exact job")
    if register_after_finish:
        assert registration["state"] == "already_finished", registration
        assert not coordinator_state(f.home)["jobs"], "already-finished registration created a watched job"
        checkpoint(f, binding, {"disposition": "wait", "jobs": []}, ok=False)
        checkpoint(f, binding, {"disposition": "continue", "next_action": "Review the already-finished result",
                                 "evidence": ["fixture:already-finished-shell"]})
        observer = f.observe()
        root_gate.set()
        assert f.terminal(origin)["status"] == "completed"
        return goal, observer, release
    assert registration["state"] == "armed", registration
    state = coordinator_state(f.home)
    assert len(state["jobs"]) == 1, state
    checkpoint(f, binding, {"disposition": "wait", "jobs": [state["jobs"][0]["id"]],
                             "evidence": ["fixture:registered-exact-job"]})
    observer = f.observe()
    assert f.thread()["status"]["type"] == "active", f.thread()
    assert current(f)["goal"]["state"] != "waiting", "waiting was published before true Idle"
    if replace_then_interrupt:
        stop(observer)
        replacement = start(f, "Replacement remains authorized despite an old origin interruption",
                            paused=False, expected=current(f)["goal"])
        f.rpc.call("turn/interrupt", {"threadId": f.parent, "turnId": origin})
        assert f.terminal(origin)["status"] == "interrupted"
        root_gate.set()
        return replacement, observer, release
    if finish_before_eos:
        before = f.provider.count
        release()
        wait(lambda: coordinator_state(f.home)["jobs"][0]["status"] == "completed",
             "completion before EOS was not observed")
        assert f.thread()["status"]["type"] == "active" and f.provider.count == before
    root_gate.set()
    assert f.terminal(origin)["status"] == "completed"
    if finish_before_eos:
        return goal, observer, release
    wait(lambda: current(f)["goal"]["state"] == "waiting", "goal did not become waiting after Idle")
    assert f.thread()["status"]["type"] == "idle", f.thread()
    f.quiet()
    f.report["waiting_snapshot"] = current(f)
    f.report["waiting_thread"] = f.thread()
    return goal, observer, release


def live_completion(f, worker=False):
    """Real shell/worker completion wakes an Idle goal without a held tool or polling."""
    _, observer, release = waiting_job(f, worker)
    before = len([request for request in f.provider.requests if not request["child"]])
    released = time.monotonic()
    release()
    wait(lambda: len([request for request in f.provider.requests if not request["child"]]) == before + 1,
         "goal job completion did not wake promptly", timeout=8)
    f.report["completion_wake_seconds"] = time.monotonic() - released
    wait(lambda: f.thread()["status"]["type"] == "idle", "goal completion wake did not settle")
    state = coordinator_state(f.home)
    assert len(state["jobs"]) == 1 and state["jobs"][0]["status"] == "completed", state
    assert len(state["callbacks"]) == 1 and state["callbacks"][0]["state"] in ("goal_owned", "goal_settled"), state
    dispatches = wait(lambda: (rows := f.goal("history")["dispatches"])
                      and rows[0]["accepted_turn"] and rows,
                      "goal completion was not positively attributed to its exact turn", timeout=8)
    assert len(dispatches) == 1 and dispatches[0]["accepted_turn"], dispatches
    stop(observer)
    f.observe()
    f.quiet()
    assert len(coordinator_state(f.home)["callbacks"]) == 1, "observer restart duplicated completion"
    if not worker:
        assert (f.work / "shell-runs").read_text() == "run\n", "completion replayed shell work"
    f.report["final_coordinator"] = coordinator_state(f.home)


def paused_results(f):
    """Pausing must retain job observation and results without autonomous continuation."""
    _, _, release = waiting_job(f)
    f.mutate("pause", current(f)["goal"])
    paused = current(f)["goal"]
    before = f.provider.count
    release()
    wait(lambda: coordinator_state(f.home)["jobs"][0]["status"] == "completed",
         "pause stopped observing the existing goal job", timeout=8)
    f.quiet()
    assert f.provider.count == before, "paused goal resumed automatically"
    assert current(f)["goal"]["state"] == "paused", current(f)
    assert current(f)["goal"]["id"] == paused["id"]
    assert coordinator_state(f.home)["parks"][0]["state"] != "cancelled", "goal pause cancelled its park"
    assert (f.work / "shell-runs").read_text() == "run\n"
    f.report["final_coordinator"] = coordinator_state(f.home)


def inactive_recovery(f, replace=False):
    """Recovered old results cannot renew a paused or replaced goal's authority."""
    original, observer, release = waiting_job(f)
    f.mutate("pause", current(f)["goal"])
    paused = current(f)["goal"]
    expected = start(f, "Paused replacement must not inherit the old job wake", expected=paused) if replace else paused
    stop(observer)
    before = f.provider.count
    release()
    wait(lambda: not f.rpc.call("thread/backgroundTerminals/list", {"threadId": f.parent})["data"],
         "shell did not finish while the observer was stopped")
    replacement_observer = f.observe()
    wait(lambda: coordinator_state(f.home)["jobs"][0]["status"] == "completed",
         "restarted observer did not preserve the inactive goal's result", timeout=8)
    f.quiet()
    snapshot = f.goal("history")
    assert f.provider.count == before, "inactive goal recovery invoked the report model"
    assert snapshot["goal"]["id"] == expected["id"] and snapshot["goal"]["state"] == "paused", snapshot
    assert snapshot["goal"]["revision"] == expected["revision"], snapshot
    assert not snapshot["report_barrier"] and not snapshot["report_id"], snapshot
    assert not snapshot["dispatches"], "inactive recovery created an autonomous report or continuation"
    assert len(snapshot["jobs"]) == 1 and snapshot["jobs"][0]["goal"] == original["id"], snapshot
    assert snapshot["jobs"][0]["status"] == "completed", snapshot
    assert coordinator_state(f.home)["parks"][0]["state"] != "cancelled"
    assert (f.work / "shell-runs").read_text() == "run\n"
    stop(replacement_observer)
    f.observe()
    f.quiet()
    assert f.provider.count == before and not current(f)["report_barrier"]
    f.report["final_snapshot"] = current(f)
    f.report["final_coordinator"] = coordinator_state(f.home)


def pending_input_before_turn(f):
    """Recording user input fences autonomous admission until its real turn is accepted."""
    first = start(f, paused=False)
    submitted = f.goal("input")
    submission = submitted["submission_id"]
    assert submitted["pending_input_state"] == "submitting", submitted
    assert submitted["goal"]["revision"] > first["revision"]
    f.observe()
    f.quiet()
    assert f.provider.count == 0, "goal model request raced ahead of the pending user submission"
    assert not f.goal("history")["dispatches"], "pending user input allowed queue/add"
    assert not f.rpc.call("thread/queue/list", {"threadId": f.parent})["data"]
    user_turn, user_gate = f.start_turn(held=True, text="User input must be admitted before any goal continuation")
    f.goal("input-settle", "--submission-id", submission, "--turn", user_turn, "--accepted")
    settled = current(f)
    assert not settled["pending_input_state"] and not settled["submission_id"], settled
    f.quiet(.3)
    assert f.provider.count == 1 and not f.goal("history")["dispatches"], "goal steered an ordinary active user turn"
    reassessment_gate = f.provider.gate()
    f.provider.root_steps.append((message("goal reassessment after accepted user input"), reassessment_gate))
    user_gate.set()
    assert f.terminal(user_turn)["status"] == "completed"
    wait(lambda: f.provider.count == 2, "settled user input permanently stranded the enabled goal", timeout=8)
    assert current(f)["goal"]["no_progress"] == 0, "ordinary user turn counted as a goal progress failure"
    f.mutate("pause", current(f)["goal"])
    reassessment_gate.set()
    wait(lambda: f.thread()["status"]["type"] == "idle", "goal reassessment did not settle after pause")
    f.quiet()
    assert f.provider.count == 2
    f.report["final_snapshot"] = current(f)


def stock_admitted_replay(f):
    """Q18: stock may replay admitted work before Plus reconnects, even with goals off."""
    origin, gate = f.start_turn(held=True, text="already admitted work interrupted by managed restart")
    before = f.provider.count
    f.rpc.close()
    f.rpc = None
    f.native.send_signal(signal.SIGTERM)
    snapshot = f.home / "app-server-daemon/loaded-threads.json"
    wait(lambda: snapshot.exists(), "managed daemon did not persist its shutdown snapshot")
    saved = json.loads(snapshot.read_text())
    assert origin in json.dumps(saved), "snapshot did not retain exact admitted work"
    # Match stock's own recovery test: stop after snapshot, before graceful turn cleanup.
    if f.native.poll() is None:
        os.killpg(f.native.pid, signal.SIGKILL)
    f.native.wait(timeout=5)
    gate.set()
    f.launch(connect=False)
    wait(lambda: f.provider.count == before + 1,
         "stock did not independently replay previously admitted interrupted work", timeout=15)
    request = f.provider.requests[-1]
    assert "daemon_recovery" in json.dumps(request["body"]), request
    f.report["stock_recovery_before_client_reconnect"] = True
    f.report["admitted_original_turn"] = origin
    f.connect()
    wait(lambda: f.thread()["status"]["type"] == "idle", "stock recovered work did not settle")
    f.quiet()


def progress_guard(f):
    """Rephrasing Continue with a repeated evidence reference cannot reset progress."""
    goal = start(f, paused=False)
    origin, first_gate = f.start_turn(held=True, text="first goal turn with stable evidence")
    first = admit(f, goal, origin)
    checkpoint(f, first, {"disposition": "complete"}, ok=False)
    checkpoint(f, first, {"disposition": "wait", "jobs": ["not-a-registered-job"]}, ok=False)
    checkpoint(f, first, {"disposition": "continue", "next_action": "First next action",
                          "evidence": ["fixture:stable-evidence"]})
    second_gate, third_gate = f.provider.gate(), f.provider.gate()
    f.provider.root_steps.extend([(message("second goal turn"), second_gate),
                                  (message("third goal turn"), third_gate)])
    observer = f.observe()
    first_gate.set()
    assert f.terminal(origin)["status"] == "completed"
    for number, gate in ((2, second_gate), (3, third_gate)):
        wait(lambda: f.provider.count == number, "useful Continue did not dispatch the next turn")
        latest = wait(lambda: (rows := f.rpc.call("thread/turns/list",
            {"threadId": f.parent, "limit": 1})["data"]) and
            rows[0]["status"] == "inProgress" and rows[0], "goal dispatch did not become an active turn")
        admitted = current(f)["goal"]
        assert admitted["no_progress"] == number - 2, admitted
        binding = admit(f, admitted, latest["id"])
        checkpoint(f, binding, {"disposition": "continue",
            "next_action": f"Entirely reworded action number {number}",
            "evidence": ["fixture:stable-evidence"]})
        gate.set()
        assert f.terminal(latest["id"])["status"] == "completed"
    wait(lambda: current(f)["goal"]["state"] == "needs_input", "two no-progress turns did not stop")
    f.quiet()
    assert f.provider.count == 3, "no-progress guard admitted another model turn"
    assert current(f)["goal"]["no_progress"] == 2
    assert observer.poll() in (None, 0)
    f.report["final_snapshot"] = current(f)


def pre_report_recovery_barrier(f):
    """Already-admitted stock work cannot renew recovery before a report is prepared."""
    start(f, paused=False)
    origin, gate = f.start_turn(held=True, text="Previously admitted stock work")
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as db:
        proof = json.loads(db.execute("SELECT value FROM goal_provenance WHERE root=?",
                                      (f.parent,)).fetchone()[0])
        proof["last_observed_uptime"] -= 45
        db.execute("UPDATE goal_provenance SET value=? WHERE root=?", (json.dumps(proof), f.parent))
    recovered = f.goal("reconnect")
    assert recovered["report_barrier"] == 1 and not f.goal("history")["dispatches"]

    def model_resume():
        goal = current(f)["goal"]
        result = subprocess.run([str(f.runtime), "goal", "resume", "--id", goal["id"],
            "--revision", str(goal["revision"]), "--json"],
            env=f.env | {"CODEX_THREAD_ID": f.parent}, cwd=f.work,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        f.report.setdefault("model_resumes", []).append(dict(exit=result.returncode,
            output=json.loads(result.stdout)))
        return result

    assert model_resume().returncode != 0, "stock work renewed recovery before a report or user receipt"
    assert current(f)["report_barrier"] == 1
    f.mutate("pause", current(f)["goal"])
    gate.set()
    assert f.terminal(origin)["status"] == "completed"
    f.quiet()
    assert f.provider.count == 1

    submission = str(uuid.uuid4())
    f.goal("input", "--submission-id", submission)
    fresh, fresh_gate = f.start_turn(held=True, text="New user direction: resume the Plus objective")
    f.goal("input-settle", "--submission-id", submission, "--turn", fresh, "--accepted")
    assert current(f)["report_barrier"] == 1, "ordinary input silently renewed recovery"
    assert model_resume().returncode == 0, "accepted user direction could not explicitly resume"
    assert current(f)["report_barrier"] == 0 and current(f)["goal"]["state"] == "active"
    f.mutate("pause", current(f)["goal"])
    fresh_gate.set()
    assert f.terminal(fresh)["status"] == "completed"
    f.quiet()
    assert f.provider.count == 2
    f.report.update(simulated_observation_gap=True, report_prepared_before_rejection=False,
                    accepted_fresh_user_turn=fresh, final_snapshot=f.goal("history"))


def report_barrier(f):
    """Unwatched completion is reported once; a report turn cannot renew its goal."""
    _, observer, release = waiting_job(f)
    stop(observer)
    release()
    wait(lambda: not f.rpc.call("thread/backgroundTerminals/list", {"threadId": f.parent})["data"],
         "unwatched shell did not finish")
    f.observe()
    wait(lambda: current(f)["report_barrier"], "unwatched completion did not persist report-only barrier")
    wait(lambda: current(f)["report_delivered"] == 1, "report-only result was not delivered")
    wait(lambda: f.thread()["status"]["type"] == "idle", "report turn did not settle")
    snapshot = current(f)
    assert snapshot["goal"]["state"] == "needs_input", snapshot
    assert snapshot["report_id"], snapshot
    report_dispatches = [row for row in f.goal("history")["dispatches"] if row["kind"] == "report"]
    assert len(report_dispatches) == 1 and report_dispatches[0]["report_id"] == snapshot["report_id"], snapshot
    assert "REPORT ONLY" in report_dispatches[0]["message"]
    assert coordinator_state(f.home)["callbacks"][0]["state"] in ("goal_owned", "goal_settled")
    report_turn = f.rpc.call("thread/turns/list", {"threadId": f.parent, "limit": 1})["data"][0]["id"]
    checkpoint(f, ("--id", snapshot["goal"]["id"], "--revision", snapshot["goal"]["revision"],
                   "--turn", report_turn, "--input-epoch", snapshot["input_epoch"]),
               {"disposition": "continue", "next_action": "Report must not renew old actions",
                "evidence": ["fixture:report-result"]}, ok=False)
    identity = ("--id", snapshot["goal"]["id"], "--revision", snapshot["goal"]["revision"])
    binding = (*identity, "--turn", report_turn, "--input-epoch", snapshot["input_epoch"])
    mutations = [("resume", *identity), ("pause", *identity), ("clear", *identity),
        ("review", *identity, "--review-seconds", 3600),
        ("edit", *identity, "--objective", "A report cannot edit its own authorization"),
        ("start", *identity, "--objective", "A report cannot replace its own goal", "--paused"),
        ("input", "--renew"),
        ("input-settle", "--submission-id", str(uuid.uuid4()), "--turn", report_turn, "--accepted"),
        ("provenance", "--cwd", f.work, "--load-kind", "cold-resume"), ("reconnect",),
        ("admit", *binding),
        ("complete", *binding, "--checkpoint", json.dumps({"evidence": ["fixture:report-self-completion"]})),
        ("continue", *binding, "--checkpoint", json.dumps({"next_action": "Report cannot continue",
                                                          "evidence": ["fixture:report-result"]})),
        ("wait", *binding, "--checkpoint", json.dumps({"jobs": ["report-cannot-adopt-work"]})),
        ("needs-input", *binding, "--checkpoint", json.dumps({"reason": "Report cannot mutate goal state"})),
        ("checkpoint", *binding, "--checkpoint", json.dumps({"disposition": "continue",
            "next_action": "A report cannot checkpoint new authority", "evidence": ["fixture:report-result"]}))]
    f.env["CODEX_THREAD_ID"] = f.parent
    try:
        for action, *args in mutations:
            denied = f.goal(action, *args, ok=False)
            assert "report-only model turn" in json.loads(denied.stdout)["error"], denied.stdout
        for action in ("status", "history", "instructions"):
            f.goal(action)
    finally:
        f.env.pop("CODEX_THREAD_ID", None)
    assert current(f)["goal"] == snapshot["goal"], "a report model mutation changed its own goal"
    f.report["report_model_denied_mutations"] = [mutation[0] for mutation in mutations]
    before = f.provider.count
    for process, _ in f.processes:
        stop(process)
    f.observe()
    f.quiet()
    assert f.provider.count == before, "restart repeated a report action"
    assert current(f)["report_barrier"] and current(f)["report_id"] == snapshot["report_id"]
    renewal = f.goal("input")["submission_id"]
    assert current(f)["report_barrier"], "ordinary host input cleared the report barrier"
    f.mutate("resume", current(f)["goal"])
    assert not current(f)["report_barrier"], "explicit host /goal resume did not clear the report-only barrier"
    renewed_turn, renewed_gate = f.start_turn(held=True, text="Explicit renewed direction after recovered report")
    f.goal("input-settle", "--submission-id", renewal, "--turn", renewed_turn, "--accepted")
    f.mutate("pause", current(f)["goal"])
    renewed_gate.set()
    assert f.terminal(renewed_turn)["status"] == "completed"
    f.report["final_snapshot"] = current(f)


def sandbox_checkpoint(f, workspace_write=False):
    """A denied model-side checkpoint must stop safely without widening permissions."""
    goal = start(f, paused=False)
    gate = f.provider.gate()
    item = message("placeholder until exact turn identity is admitted")
    f.provider.root_steps.append((item, gate))
    policy = {"type": "workspaceWrite", "writableRoots": [str(f.work)], "networkAccess": False,
              "excludeTmpdirEnvVar": True, "excludeSlashTmp": True} if workspace_write else {"type": "readOnly"}
    origin = f.rpc.call("turn/start", {"threadId": f.parent, "sandboxPolicy": policy,
        "collaborationMode": {"mode": "plan", "settings": {"model": "gpt-6.1-sol",
                              "reasoning_effort": "high", "developer_instructions": None}},
        "input": [{"type": "text", "text": "Stage a checkpoint with existing sandbox permissions"}]})["turn"]["id"]
    wait(lambda: f.provider.count == 1, "sandbox provider request missing")
    binding = admit(f, goal, origin)
    disposition = json.dumps({"disposition": "complete", "evidence": ["fixture:sandbox-checkpoint"]})
    command = shlex.join([str(f.runtime), "goal", "checkpoint", "--thread", f.parent,
        *map(str, binding), "--checkpoint", disposition, "--json"])
    item.clear()
    item.update(execute("text(await tools.exec_command(" + json.dumps({"cmd": command,
        "yield_time_ms": 1000, "max_output_tokens": 2000, "login": False}) + "));"))
    gate.set()
    assert f.terminal(origin)["status"] == "completed"
    assert f.provider.count == 2, f.provider.count
    output = "\n".join(str(part.get("output", "")) for part in f.provider.requests[-1]["body"]["input"]
                       if part.get("type") in ("custom_tool_call_output", "function_call_output"))
    f.report["sandbox_policy"] = policy
    f.report["sandbox_checkpoint_output"] = output
    denied = any(token in output.lower() for token in ("readonly", "read-only", "permission denied",
                                                      "operation not permitted", "sandbox"))
    assert denied, f"expected visible checkpoint denial, got {output[-2000:]}"
    f.observe()
    wait(lambda: current(f)["goal"]["state"] == "needs_input", "denied checkpoint did not become needs-input")
    f.quiet()
    assert f.provider.count == 2, "checkpoint denial widened sandbox or caused a model loop"
    assert not current(f)["goal"]["checkpoint"], "denied checkpoint was persisted"
    f.report["final_snapshot"] = current(f)


def unloaded_goal(f):
    """A zero-job goal survives UI exit and safely resumes an unloaded same-daemon root."""
    seed, _ = f.start_turn(text="Persist this root before the UI detaches")
    assert f.terminal(seed)["status"] == "completed"
    goal = start(f, paused=False)
    generation = f.rpc.generation
    f.rpc.call("thread/unsubscribe", {"threadId": f.parent})
    wait(lambda: f.thread()["status"]["type"] == "notLoaded", "goal parent did not unload")
    gate = f.provider.gate()
    f.provider.root_steps.append((message("resumed goal after UI exit"), gate))
    before = f.provider.count
    observer = f.observe()
    wait(lambda: f.provider.count == before + 1, "same-generation unloaded goal did not dispatch", timeout=12)
    assert f.rpc.generation == generation
    assert f.thread()["status"]["type"] == "active"
    assert not coordinator_state(f.home)["jobs"], "zero-job goal invented a background job"
    f.mutate("pause", current(f)["goal"])
    gate.set()
    wait(lambda: f.thread()["status"]["type"] == "idle", "resumed goal did not settle")
    f.quiet()
    assert current(f)["goal"]["id"] == goal["id"] and current(f)["goal"]["state"] == "paused"
    assert observer.poll() in (None, 0)
    f.report["same_generation_resume"] = generation


def unproven_isolation(f):
    """A disabled user-file key alone does not establish controlled loading provenance."""
    f.goal("start", "--objective", "Unproven root may be saved paused", "--paused")
    goal = current(f)["goal"]
    f.mutate("resume", goal, ok=False)
    assert current(f)["goal"] == goal
    f.observe()
    f.quiet()
    assert f.provider.count == 0, "unproven runtime scheduled a custom goal"
    f.report["final_snapshot"] = current(f)


def start_wire_fixture(f, hold_add=False):
    f.rpc.close()
    f.rpc = None
    stop(f.native)
    socket = f.home / "app-server-control/app-server-control.sock"
    socket.unlink(missing_ok=True)
    log = (f.evidence / "wire.log").open("w")
    env = dict(f.env)
    if hold_add:
        env["CODEX_PLUS_WIRE_HOLD_ADD"] = "1"
    process = subprocess.Popen([sys.executable, str(REPO / "tests/fixtures/custom_goal_wire.py"),
        str(f.home), str(f.work), f.parent, "-c", "features.goals=false"], env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    f.processes.append((process, log))
    wait(socket.exists, "private lost-ack wire fixture did not start")
    f.report["wire_fault_fixture_not_stock_compatibility"] = True


def lost_ack_replacement(f):
    """A wire-level ambiguous add blocks replacement scheduling across observer restart."""
    start_wire_fixture(f)
    first = start(f, paused=False)
    observer = f.observe()
    trace_path = f.home / "wire.json"

    def trace():
        return json.loads(trace_path.read_text()) if trace_path.exists() else {"enqueues": [], "starts": []}

    wait(lambda: len(trace()["enqueues"]) == 1, "autonomous goal never reached queue/add")
    wait(lambda: current(f)["goal"]["state"] == "needs_input", "lost add acknowledgement was not visible")
    original = f.goal("history")
    assert len(original["dispatches"]) == 1, original
    dispatch = original["dispatches"][0]
    assert dispatch["state"] == "uncertain" and dispatch["goal"] == first["id"], dispatch
    replacement = start(f, "New goal must wait for the old ambiguous admission", paused=False,
                        expected=original["goal"])
    stop(observer)
    f.observe()
    f.quiet()
    after = f.goal("history")
    assert after["goal"]["id"] == replacement["id"]
    assert len(trace()["enqueues"]) == 1 and not trace()["starts"], trace()
    assert len(after["dispatches"]) == 1, after["dispatches"]
    frozen = after["dispatches"][0]
    for field in ("id", "goal", "revision", "input_epoch", "client", "message", "created"):
        assert frozen[field] == dispatch[field], (field, frozen, dispatch)
    assert frozen["state"] == "uncertain", frozen
    f.report["final_snapshot"] = after
    f.report["wire_trace"] = trace()


def pause_admission_lock(f):
    """Pause cannot commit while the queue admission RPC is unresolved under the shared lock."""
    start_wire_fixture(f, hold_add=True)
    goal = start(f, paused=False)
    f.observe()
    wait((f.home / "queue-add-entered").exists, "goal queue admission did not reach the held RPC")
    pause_log = (f.evidence / "pause.log").open("w")
    pause = subprocess.Popen([str(f.runtime), "goal", "pause", "--thread", f.parent,
        "--id", goal["id"], "--revision", str(goal["revision"]), "--json"], env=f.env,
        cwd=f.work, stdin=subprocess.DEVNULL, stdout=pause_log, stderr=pause_log, start_new_session=True)
    f.processes.append((pause, pause_log))
    time.sleep(.25)
    assert pause.poll() is None, "pause committed during the unresolved queue admission RPC"
    during = current(f)
    assert during["goal"]["state"] == "active" and during["goal"]["revision"] == goal["revision"], during
    (f.home / "queue-add-release").touch()
    assert pause.wait(timeout=8) == 0, "pause did not complete after admission released its lock"
    f.quiet()
    after = current(f)
    assert after["goal"]["state"] == "paused" and after["goal"]["id"] == goal["id"], after
    trace = json.loads((f.home / "wire.json").read_text())
    assert len(trace["enqueues"]) == 1 and not trace["starts"], trace
    f.report["admission_snapshot"] = during
    f.report["final_snapshot"] = after
    f.report["wire_trace"] = trace


def model_checkpoint(f):
    """The native shell can stage an exact checkpoint under the existing full-access policy."""
    goal = start(f, paused=False)
    gate = f.provider.gate()
    item = message("placeholder until the exact turn exists")
    f.provider.root_steps.append((item, gate))
    origin = f.rpc.call("turn/start", {"threadId": f.parent,
        "input": [{"type": "text", "text": "Finish the authorized goal and record exact evidence"}]})["turn"]["id"]
    wait(lambda: f.provider.count == 1, "model checkpoint request missing")
    binding = admit(f, goal, origin)
    command = shlex.join([str(f.runtime), "goal", "checkpoint", "--thread", f.parent,
        *map(str, binding), "--checkpoint", json.dumps({"disposition": "complete",
        "evidence": ["fixture:model-shell-checkpoint"]}), "--json"])
    item.clear()
    item.update(execute("text(await tools.exec_command(" + json.dumps({"cmd": command,
        "yield_time_ms": 1000, "max_output_tokens": 1000, "login": False}) + "));"))
    gate.set()
    assert f.terminal(origin)["status"] == "completed"
    f.observe()
    wait(lambda: current(f)["goal"]["state"] == "complete", "successful model checkpoint did not complete goal")
    snapshot = current(f)
    assert snapshot["goal"]["checkpoint"]["evidence"] == ["fixture:model-shell-checkpoint"], snapshot
    f.quiet()
    assert f.provider.count == 2, "goal completion admitted an unnecessary continuation"
    submission = f.goal("input")["submission_id"]
    assert current(f)["goal"] is None, "ordinary user input did not dismiss completed indicator"
    user_turn, _ = f.start_turn(text="Ordinary message after goal completion")
    assert f.terminal(user_turn)["status"] == "completed"
    f.goal("input-settle", "--submission-id", submission, "--turn", user_turn, "--accepted")
    assert any(row["id"] == goal["id"] and row["state"] == "complete" for row in f.goal("history")["history"])
    f.report["completed_snapshot"] = snapshot


def active_edit_replace(f):
    """Pause/edit/replacement during a turn invalidate its saved Continue without interruption."""
    first = start(f, paused=False)
    origin, gate = f.start_turn(held=True, text="goal work remains active during goal edits")
    binding = admit(f, first, origin)
    checkpoint(f, binding, {"disposition": "continue", "next_action": "Old goal follow-up",
                             "evidence": ["fixture:old-goal-turn"]})
    f.mutate("edit", first, "--objective", "New direction while exact old turn is running")
    edited = current(f)["goal"]
    assert edited["revision"] > first["revision"] and f.thread()["status"]["type"] == "active"
    checkpoint(f, binding, {"disposition": "complete", "evidence": ["fixture:stale-model"]}, ok=False)
    f.mutate("pause", edited)
    paused = current(f)["goal"]
    replacement = start(f, "Replacement must ignore the old exact turn", expected=paused)
    assert replacement["id"] != first["id"] and f.thread()["status"]["type"] == "active"
    f.mutate("resume", paused, ok=False)
    gate.set()
    assert f.terminal(origin)["status"] == "completed", "goal edit interrupted a running turn"
    f.observe()
    f.quiet()
    snapshot = current(f)
    assert snapshot["goal"] == replacement, "late old turn changed replacement state"
    assert f.provider.count == 1, "stale Continue revived a paused replacement"
    f.report["final_snapshot"] = snapshot


def completion_before_eos(f):
    """Finishing after a staged Wait but before EOS cannot strand the goal in waiting."""
    waiting_job(f, finish_before_eos=True)
    wait(lambda: f.provider.count == 3, "already-completed Wait did not continue promptly", timeout=8)
    wait(lambda: f.thread()["status"]["type"] == "idle", "completion-before-EOS wake did not settle")
    f.quiet()
    snapshot = current(f)
    assert snapshot["goal"]["state"] != "waiting", snapshot
    dispatches = f.goal("history")["dispatches"]
    assert len(dispatches) == 1 and dispatches[0]["accepted_turn"], dispatches
    assert (f.work / "shell-runs").read_text() == "run\n"
    f.report["final_snapshot"] = snapshot


def already_finished_wait(f):
    """A job finishing before registration does not leave an empty Wait blocking the goal."""
    waiting_job(f, register_after_finish=True)
    wait(lambda: f.provider.count == 3, "already-finished registration stranded the goal", timeout=8)
    wait(lambda: f.thread()["status"]["type"] == "idle", "already-finished follow-up did not settle")
    f.quiet()
    assert not coordinator_state(f.home)["jobs"] and not coordinator_state(f.home)["callbacks"]
    assert current(f)["goal"]["state"] != "waiting"
    assert (f.work / "shell-runs").read_text() == "run\n"
    f.report["final_snapshot"] = current(f)


def seed_dispatch(f, goal, state, *, accepted_turn=None, queue=None):
    """Seed only the owned ledger to represent a precise coordinator commit/crash gap."""
    dispatch, client = str(uuid.uuid4()), str(uuid.uuid4())
    epoch = current(f)["input_epoch"]
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
        database.execute("INSERT INTO goal_dispatches"
            "(id,root,goal,revision,input_epoch,client,message,kind,state,queue,accepted_turn,created) "
            "VALUES (?,?,?,?,?,?,?,'continue',?,?,?,?)",
            (dispatch, f.parent, goal["id"], goal["revision"], epoch, client,
             "Private fault seed for exact admission recovery", state, queue, accepted_turn, time.time()))
    f.report.setdefault("private_ledger_fault_seed", []).append({"dispatch": dispatch, "state": state,
        "accepted_turn": accepted_turn, "queue": queue, "missing_goal_turn_binding": True})
    return dispatch, client


def accepted_before_admission(f):
    """A real completed turn cannot bypass missing-checkpoint rules after its binding commit is lost."""
    goal = start(f, paused=False)
    origin, gate = f.start_turn(held=True, text="Accepted goal work with a simulated lost admission commit")
    dispatch, _ = seed_dispatch(f, goal, "accepted", accepted_turn=origin)
    gate.set()
    assert f.terminal(origin)["status"] == "completed"
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
        assert database.execute("SELECT COUNT(*) FROM goal_turns WHERE root=?", (f.parent,)).fetchone()[0] == 0
    before = f.provider.count
    f.observe()
    wait(lambda: current(f)["goal"]["state"] == "needs_input",
         "accepted turn without its checkpoint binding did not require input", timeout=8)
    f.quiet()
    history = f.goal("history")
    assert f.provider.count == before, "acceptance/admission crash authorized another model turn"
    assert len(history["dispatches"]) == 1 and history["dispatches"][0]["id"] == dispatch, history
    assert not current(f)["goal"]["checkpoint"]
    f.report["final_snapshot"] = history


def starting_before_rpc(f):
    """Recovered starting with a positive queue entry needs input rather than another start RPC."""
    start_wire_fixture(f)
    goal = start(f, paused=False)
    dispatch, client = seed_dispatch(f, goal, "starting", queue="positive-pending-entry")
    (f.home / "pending-queue.json").write_text(json.dumps([{"id": "positive-pending-entry",
        "clientUserMessageId": client, "input": [{"type": "text", "text": "Frozen queued goal input"}]}]))
    observer = f.observe()
    wait(lambda: current(f)["goal"]["state"] == "needs_input",
         "recovered starting dispatch remained silently active", timeout=8)
    f.quiet()
    history = f.goal("history")
    assert len(history["dispatches"]) == 1 and history["dispatches"][0]["id"] == dispatch
    assert history["dispatches"][0]["state"] == "uncertain", history
    assert history["dispatches"][0]["queue"] == "positive-pending-entry"
    stop(observer)
    f.observe()
    f.quiet()
    trace = json.loads((f.home / "wire.json").read_text())
    assert not trace["enqueues"] and not trace["starts"], "recovery blindly retried queue admission/start"
    f.report["final_snapshot"] = f.goal("history")
    f.report["wire_trace"] = trace


def cold_provenance_gap(f):
    """A same-boot cold-load attestation cannot erase a known 40-second observation gap."""
    goal = start(f, paused=False)
    seed, _ = f.start_turn(text="Persist this active root before a simulated observer gap")
    assert f.terminal(seed)["status"] == "completed"
    f.rpc.call("thread/unsubscribe", {"threadId": f.parent})
    wait(lambda: f.thread()["status"]["type"] == "notLoaded", "root did not unload before cold attestation")
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
        proof = json.loads(database.execute("SELECT value FROM goal_provenance WHERE root=?",
                                            (f.parent,)).fetchone()[0])
        proof["last_observed_uptime"] = float(Path("/proc/uptime").read_text().split()[0]) - 40
        database.execute("UPDATE goal_provenance SET value=? WHERE root=?", (json.dumps(proof), f.parent))
    f.report["private_provenance_age_seed_seconds"] = 40
    f.rpc.call("thread/resume", {"threadId": f.parent, "config": {"features.goals": False}})
    f.goal("provenance", "--cwd", f.work, "--load-kind", "cold-resume")
    snapshot = current(f)
    assert snapshot["report_barrier"] and snapshot["report_id"], "cold attestation erased the same-boot gap"
    assert snapshot["goal"]["id"] == goal["id"] and snapshot["goal"]["state"] == "needs_input", snapshot
    report_id = snapshot["report_id"]
    f.goal("provenance", "--cwd", f.work, "--load-kind", "cold-resume")
    assert current(f)["report_barrier"] and current(f)["report_id"] == report_id
    f.quiet()
    f.report["final_snapshot"] = current(f)


def reconnect_restarts_observer(f):
    """Controlled reconnect restarts detached reporting without a manual observe call."""
    _, observer, release = waiting_job(f)
    stop(observer)
    release()
    wait(lambda: not f.rpc.call("thread/backgroundTerminals/list", {"threadId": f.parent})["data"],
         "shell did not finish across the client/observer gap")
    f.rpc.call("thread/unsubscribe", {"threadId": f.parent})
    wait(lambda: f.thread()["status"]["type"] == "notLoaded", "root did not unload before reconnect")
    f.rpc.call("thread/resume", {"threadId": f.parent, "config": {"features.goals": False}})
    stop_rust_observers(f.home)
    f.supervisor_lock.close()
    f.supervisor_lock = None
    before = f.provider.count
    f.goal("provenance", "--cwd", f.work, "--load-kind", "cold-resume")
    wait(lambda: current(f)["report_delivered"] == 1,
         "client reconnect did not automatically restart recovered-result reporting", timeout=10)
    wait(lambda: f.thread()["status"]["type"] == "idle", "automatic reconnect report did not settle")
    snapshot = current(f)
    assert snapshot["report_barrier"] and snapshot["goal"]["state"] == "needs_input", snapshot
    assert f.provider.count == before + 1, "reconnect admitted more than one recovered-results report"
    f.goal("provenance", "--cwd", f.work, "--load-kind", "cold-resume")
    f.quiet()
    assert f.provider.count == before + 1 and current(f)["report_id"] == snapshot["report_id"]
    assert coordinator_state(f.home)["jobs"][0]["status"] == "completed"
    f.report["automatic_reconnect_without_manual_observe"] = True
    f.report["final_snapshot"] = f.goal("history")


def interrupted_old_origin(f):
    """An interrupted replaced goal cannot stop its successor or defeat explicit resume."""
    replacement, _, release = waiting_job(f, replace_then_interrupt=True)
    gate = f.provider.gate()
    f.provider.root_steps.append((message("replacement goal work remains authorized"), gate))
    before = f.provider.count
    f.observe()
    wait(lambda: f.provider.count == before + 1,
         "old origin interruption stopped the replacement's goal turn", timeout=8)
    assert current(f)["goal"]["id"] == replacement["id"] and current(f)["goal"]["state"] == "active"
    f.mutate("pause", current(f)["goal"])
    f.mutate("resume", current(f)["goal"])
    f.quiet(.5)
    assert current(f)["goal"]["state"] == "active", "old interruption defeated explicit resume"
    f.mutate("pause", current(f)["goal"])
    gate.set()
    wait(lambda: f.thread()["status"]["type"] == "idle", "replacement turn did not settle")
    release()
    wait(lambda: coordinator_state(f.home)["jobs"][0]["status"] == "completed",
         "old goal job result was discarded after replacement")
    f.quiet()
    assert f.provider.count == before + 1 and current(f)["goal"]["state"] == "paused"
    history = f.goal("history")
    assert history["jobs"][0]["goal"] != replacement["id"], history
    f.report["final_snapshot"] = history


def partial_two_job_completion(f):
    """Each newly completed shell wakes once, while unfinished work stays Idle and observed."""
    goal = start(f, paused=False)
    arguments = []
    for label in ("a", "b"):
        release = f.work / f"shell-release-{label}"
        runs = f.work / f"shell-runs-{label}"
        shell = (f'printf "run\\n" >> {shlex.quote(str(runs))}; '
                 f'i=0; while [ ! -f {shlex.quote(str(release))} ] && [ "$i" -lt 400 ]; '
                 'do sleep .1; i=$((i+1)); done; '
                 f'test -f {shlex.quote(str(release))} || exit 124; printf "goal-shell-{label}-complete\\n"')
        command = "# codex-plus-shell: auto\n" + shlex.join([str(f.runtime), "run", "--", "/bin/sh", "-c", shell])
        arguments.append({"cmd": command, "yield_time_ms": 250, "max_output_tokens": 100, "login": False})
    code = "text(await Promise.all(" + json.dumps(arguments) + ".map(args => tools.exec_command(args))));"
    origin_gate = f.provider.gate()
    f.provider.root_steps.extend([(execute(code), None), (message("both exact shells registered"), origin_gate)])
    origin = f.rpc.call("turn/start", {"threadId": f.parent,
        "input": [{"type": "text", "text": "Run two finite shells; review each completion immediately"}]})["turn"]["id"]
    terminals = wait(lambda: (rows := f.rpc.call("thread/backgroundTerminals/list",
        {"threadId": f.parent})["data"]) and len(rows) == 2 and rows, "two actual shells did not start")
    processes = {label: next(row["processId"] for row in terminals if f"shell-release-{label}" in row["command"])
                 for label in ("a", "b")}
    wait(lambda: f.provider.count == 2, "two-shell tool did not return before the root's EOS")
    binding = admit(f, goal, origin)
    registration = f.cli("park", "--parent", f.parent, "--process", processes["a"],
                         "--process", processes["b"], "--reason", "review each finite completion")
    assert registration["state"] == "armed", registration
    jobs = coordinator_state(f.home)["jobs"]
    assert len(jobs) == 2, jobs
    job_ids = [job["id"] for job in jobs]
    checkpoint(f, binding, {"disposition": "wait", "jobs": job_ids,
                             "evidence": ["fixture:two-finite-shells-registered"]})
    f.observe()
    origin_gate.set()
    assert f.terminal(origin)["status"] == "completed"
    wait(lambda: current(f)["goal"]["state"] == "waiting", "two-job goal did not enter waiting")
    assert f.thread()["status"]["type"] == "idle"
    f.quiet()

    first_gate = f.provider.gate()
    f.provider.root_steps.append((message("review A and keep observing B"), first_gate))
    released = time.monotonic()
    (f.work / "shell-release-a").touch()
    try:
        wait(lambda: f.provider.count == 3, "A completed while B ran, but no immediate goal wake arrived", timeout=8)
    finally:
        f.report["first_partial_observation"] = {"provider_requests": f.provider.count,
            "parent_status": f.thread()["status"], "jobs": coordinator_state(f.home)["jobs"],
            "running_processes": [row["processId"] for row in f.rpc.call(
                "thread/backgroundTerminals/list", {"threadId": f.parent})["data"]]}
    f.report["first_partial_wake_seconds"] = time.monotonic() - released
    live = f.rpc.call("thread/backgroundTerminals/list", {"threadId": f.parent})["data"]
    assert len(live) == 1 and live[0]["processId"] == processes["b"], live
    first_turn = f.rpc.call("thread/turns/list", {"threadId": f.parent, "limit": 1})["data"][0]
    assert first_turn["status"] == "inProgress", first_turn
    first_binding = admit(f, current(f)["goal"], first_turn["id"])
    # Include A again: accepted results must not create repeated wakes for the same exact job.
    checkpoint(f, first_binding, {"disposition": "wait", "jobs": job_ids,
                                   "evidence": ["fixture:shell-a-result-reviewed"]})
    first_gate.set()
    assert f.terminal(first_turn["id"])["status"] == "completed"
    wait(lambda: current(f)["goal"]["state"] == "waiting", "goal did not wait again while B remained running")
    assert f.thread()["status"]["type"] == "idle"
    f.quiet()
    assert f.provider.count == 3, "already-reviewed A completion woke the model again"
    f.report["partial_wait_snapshot"] = current(f)

    final_gate = f.provider.gate()
    f.provider.root_steps.append((message("review B and complete the goal"), final_gate))
    (f.work / "shell-release-b").touch()
    wait(lambda: f.provider.count == 4, "B completion did not produce one prompt follow-up", timeout=8)
    final_turn = f.rpc.call("thread/turns/list", {"threadId": f.parent, "limit": 1})["data"][0]
    assert final_turn["status"] == "inProgress", final_turn
    final_binding = admit(f, current(f)["goal"], final_turn["id"])
    checkpoint(f, final_binding, {"disposition": "complete",
        "evidence": ["fixture:shell-a-result-reviewed", "fixture:shell-b-result-reviewed"]})
    final_gate.set()
    assert f.terminal(final_turn["id"])["status"] == "completed"
    wait(lambda: current(f)["goal"]["state"] == "complete", "two-job goal did not complete")
    f.quiet()
    history = f.goal("history")
    assert f.provider.count == 4 and len(history["dispatches"]) == 2, history
    assert all(job["status"] == "completed" for job in coordinator_state(f.home)["jobs"])
    for label in ("a", "b"):
        assert (f.work / f"shell-runs-{label}").read_text() == "run\n", "goal wake reran a shell"
    f.report["final_snapshot"] = history


def mixed_independent_priority(f):
    """Account for the old goal turn, then admit an independent result before Continue."""
    first = start(f)
    origin_gate, callback_gate = f.provider.gate(), f.provider.gate()
    release = f.work / "shell-release"
    shell = (f'i=0; while [ ! -f {shlex.quote(str(release))} ] && [ "$i" -lt 400 ]; '
             'do sleep .1; i=$((i+1)); done; '
             f'test -f {shlex.quote(str(release))} || exit 124; printf "independent-result\\n"')
    command = "# codex-plus-shell: auto\n" + shlex.join([str(f.runtime), "run", "--", "/bin/sh", "-c", shell])
    item = execute("text(await tools.exec_command(" + json.dumps({"cmd": command,
        "yield_time_ms": 1000, "max_output_tokens": 100, "login": False}) + "));")
    f.provider.root_steps.extend([(item, None), (message("origin waiting to finish"), origin_gate),
                                 (message("review independent result"), callback_gate)])
    origin = f.rpc.call("turn/start", {"threadId": f.parent,
        "input": [{"type": "text", "text": "Start the independent registered result"}]})["turn"]["id"]
    terminals = wait(lambda: f.rpc.call("thread/backgroundTerminals/list",
        {"threadId": f.parent})["data"], "independent native command did not start")
    wait(lambda: f.provider.count == 2, "origin command did not yield")
    # Registration precedes goal admission: this callback must remain independent.
    registration = f.cli("park", "--parent", f.parent, "--process", terminals[0]["processId"],
                         "--reason", "independent callback priority")
    assert registration["state"] == "armed", registration
    f.mutate("resume", first)
    binding = admit(f, current(f)["goal"], origin)
    checkpoint(f, binding, {"disposition": "continue", "next_action": "Continue after reviewing results",
                            "evidence": ["fixture:independent-registration"]})
    f.observe()
    release.touch()
    wait(lambda: coordinator_state(f.home)["jobs"][0]["status"] == "completed",
         "independent job result was not observed while origin held")
    assert f.provider.count == 2
    origin_gate.set()
    assert f.terminal(origin)["status"] == "completed"
    wait(lambda: f.provider.count == 3, "neither ordinary callback nor goal reached the parent")
    users = [item for item in f.provider.requests[-1]["body"]["input"]
             if item.get("type") == "message" and item.get("role") == "user"]
    notice = "\n".join(part.get("text", "") for part in users[-1].get("content", []))
    callback = coordinator_state(f.home)["callbacks"][0]
    assert notice.startswith("LIVE COMPLETION:") and callback["id"] in notice, notice
    assert "LIVE GOAL CONTINUATION:" not in notice, "goal Continue preempted independent callback"
    assert not f.goal("history")["dispatches"], "goal dispatched before independent result review"
    latest = f.rpc.call("thread/turns/list", {"threadId": f.parent, "limit": 1})["data"][0]
    # If ordinary delivery preceded old-turn accounting, this exact admission is blocked.
    second = admit(f, current(f)["goal"], latest["id"])
    checkpoint(f, second, {"disposition": "needs_input", "reason": "Independent result reviewed; fixture stop"})
    callback_gate.set()
    assert f.terminal(latest["id"])["status"] == "completed"
    wait(lambda: current(f)["goal"]["state"] == "needs_input", "callback goal checkpoint not finalized")
    f.quiet()
    assert f.provider.count == 3, "goal continued after the explicit needs-input checkpoint"
    f.report["final_snapshot"] = f.goal("history")


SCENARIOS = {"cli-transactions": cli_transactions,
             "checkpoint-before-eos-crash": checkpoint_before_eos_crash,
             "input-epoch": input_epoch,
             "stock-isolation": stock_isolation,
             "managed-isolation": stock_isolation,
             "shell-completion": live_completion,
             "worker-completion": lambda f: live_completion(f, worker=True),
             "paused-results": paused_results,
             "paused-recovery": inactive_recovery,
             "replaced-recovery": lambda f: inactive_recovery(f, replace=True),
             "pending-input-before-turn": pending_input_before_turn,
             "stock-admitted-replay": stock_admitted_replay,
             "progress-guard": progress_guard,
             "report-barrier": report_barrier,
             "read-only-plan-checkpoint": sandbox_checkpoint,
             "workspace-plan-checkpoint": lambda f: sandbox_checkpoint(f, workspace_write=True),
             "unloaded-goal": unloaded_goal,
             "unproven-isolation": unproven_isolation,
             "lost-ack-replacement": lost_ack_replacement,
             "pause-admission-lock": pause_admission_lock,
             "model-checkpoint": model_checkpoint,
             "active-edit-replace": active_edit_replace,
             "completion-before-eos": completion_before_eos,
             "already-finished-wait": already_finished_wait,
             "accepted-before-admission": accepted_before_admission,
             "starting-before-rpc": starting_before_rpc,
             "cold-provenance-gap": cold_provenance_gap,
             "reconnect-restarts-observer": reconnect_restarts_observer,
             "interrupted-old-origin": interrupted_old_origin,
             "partial-two-job-completion": partial_two_job_completion,
             "mixed-independent-priority": mixed_independent_priority,
             "pre-report-recovery-barrier": pre_report_recovery_barrier}


def run(runtime, binary, evidence, scenario):
    fixture = CustomGoalServer(runtime, binary, evidence,
                               managed=scenario in ("managed-isolation", "stock-admitted-replay"))
    passed = False
    try:
        SCENARIOS[scenario](fixture)
        passed = True
    finally:
        fixture.close(passed)
    print(json.dumps({"scenario": scenario, "passed": passed, "evidence": str(evidence),
                      "remote_model_requests": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--stock-binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS) + ("all",), default="all")
    args = parser.parse_args()
    failed = []
    for scenario in SCENARIOS if args.scenario == "all" else (args.scenario,):
        try:
            run(args.runtime.resolve(strict=True), args.stock_binary.resolve(strict=True),
                args.evidence.resolve() / scenario, scenario)
        except Exception:
            traceback.print_exc()
            failed.append(scenario)
    if failed:
        print(json.dumps({"passed": False, "failed_scenarios": failed}), file=sys.stderr)
        sys.exit(1)
