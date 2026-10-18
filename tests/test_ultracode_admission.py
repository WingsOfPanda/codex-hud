"""Private official-daemon admission checks; all provider requests stay loopback."""

import argparse
import http.server
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import uuid

from fixtures.ultracode_bootstrap import BootstrapServer
from fixtures.custom_goal_server import CustomGoalServer
from fixtures.upstream_0163 import BoundaryProvider, execute, message
from test_background_integration import RecordingClient, stop, wait


REPO = Path(__file__).resolve().parents[1]


class AdmissionProvider(BoundaryProvider):
    """Add scripted usage to existing gates so observer compaction is deterministic."""

    def __init__(self):
        super().__init__()
        self.install(self)

    @staticmethod
    def install(fixture):

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fixture.lock:
                    step = fixture.root_steps.pop(0) if fixture.root_steps else (message(), None)
                    item, gate = step[:2]
                    tokens = step[2] if len(step) == 3 else 0
                    fixture.requests.append(dict(body=body, thread=self.headers.get("thread-id"), child=False))
                    number = len(fixture.requests)
                if gate is not None and not gate.wait(45):
                    self.send_error(500, "admission fixture gate expired")
                    return
                events = [dict(type="response.created", response={"id": f"response-{number}"}),
                          dict(type="response.output_item.done", item=item),
                          dict(type="response.completed", response={"id": f"response-{number}",
                              "usage": {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}})]
                payload = "".join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                                  for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        fixture.server.RequestHandlerClass = Handler


def run(binary, runtime, evidence):
    server = BootstrapServer(binary, evidence)
    old_provider = server.provider
    server.provider = AdmissionProvider()
    config = server.home / "config.toml"
    config.write_text(config.read_text().replace(old_provider.url, server.provider.url))
    old_provider.close()
    bootstrap = (REPO / "docs/ultracode-bootstrap.txt").read_text()
    instructions = "Preserve admission fixture developer text 81073.\n\n" + bootstrap
    passed = False

    def cli(*args, model=False, stdin=None):
        env = dict(server.env)
        if model:
            env["CODEX_THREAD_ID"] = server.parent
        result = subprocess.run([str(runtime), *args, "--thread", server.parent, "--json"],
            env=env, input=stdin, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        value = json.loads(result.stdout)
        server.report.setdefault("helper_calls", []).append({"args": args, "model": model,
                                                            "result": value})
        return value

    def check(name, **details):
        server.report["checks"].append(dict(name=name, **details))

    def reserve():
        submission = str(uuid.uuid4())
        cli("goal", "input", "--submission-id", submission)
        return submission

    def start(text, submission=None, lost_ack=False):
        gate = server.provider.gate()
        server.provider.root_steps.append((message(text), gate))
        before = server.provider.count
        params = {"threadId": server.parent, "input": [{"type": "text", "text": text}]}
        if submission:
            params["clientUserMessageId"] = submission
        if lost_ack:
            client = RecordingClient(server.home / "app-server-control/app-server-control.sock")
            client.send({"id": 9999, "method": "turn/start", "params": params})
            wait(lambda: server.provider.count > before, "lost-ACK turn did not start")
            client.close()
            turn = server.rpc.call("thread/turns/list", {"threadId": server.parent,
                "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"})["data"][0]["id"]
        else:
            turn = server.rpc.call("turn/start", params)["turn"]["id"]
            wait(lambda: server.provider.count > before, "turn provider request did not start")
        return turn, gate

    def finish(turn, gate):
        gate.set()
        server.terminal(turn)

    def settle(submission, turn):
        cli("goal", "input-settle", "--submission-id", submission, "--accepted", "--turn", turn)

    try:
        server.launch()
        server.parent = server.rpc.call("thread/start", {"cwd": str(server.work),
            "developerInstructions": instructions, "approvalPolicy": "never",
            "sandbox": "danger-full-access"})["thread"]["id"]
        server.provider.parent = server.parent
        cli("ultracode", "provenance", "--load-kind", "fresh",
            "--developer-instructions-stdin", stdin=instructions)
        enabled = cli("ultracode", "on")["requested"]
        private = subprocess.run([str(runtime), "ultracode", "load-instructions",
            "--thread", server.parent, "--json"], env=server.env, capture_output=True,
            text=True, timeout=20)
        assert private.returncode == 0
        assert json.loads(private.stdout)["developer_instructions"] == instructions
        for action in ("on", "off", "provenance", "load-instructions"):
            env = dict(server.env, CODEX_THREAD_ID=server.parent)
            denied = subprocess.run([str(runtime), "ultracode", action,
                "--thread", server.parent, "--json"], env=env, capture_output=True,
                text=True, timeout=20)
            assert denied.returncode != 0 and "Preserve admission fixture developer text 81073." not in denied.stdout
        assert "Preserve admission fixture developer text 81073." not in json.dumps(cli("ultracode", "status"))
        assert server.provider.count == 0
        check("qualification and controls cause zero provider calls; private instructions are host-only")

        initial = reserve()
        turn, gate = start("initial reserved turn", initial)
        snapshot = cli("ultracode", "status", model=True)
        assert snapshot["effective"]["enabled"] is True
        assert snapshot["effective"]["submission"] == initial
        assert snapshot["effective"]["turn"] == turn
        assert snapshot["effective"]["revision"] == enabled["revision"]
        before = server.provider.count
        off = cli("ultracode", "off")
        assert off["requested"]["enabled"] is False and off["effective"]["enabled"] is True
        assert server.provider.count == before
        check("status before input-settle proves exact initial admission", turn=turn)

        steer = reserve()
        server.rpc.call("turn/steer", {"threadId": server.parent, "expectedTurnId": turn,
            "clientUserMessageId": steer, "input": [{"type": "text", "text": "steering while off"}]})
        settle(steer, turn)
        snapshot = cli("ultracode", "status", model=True)
        assert snapshot["effective"]["submission"] == initial
        assert snapshot["effective"]["enabled"] is True
        assert all(item["submission"] != steer for item in snapshot["pending"])
        assert server.provider.count == before
        check("off toggle and steering retain active turn snapshot without control model calls")
        finish(turn, gate)

        initial_off = reserve()
        turn, gate = start("next reserved turn is off", initial_off)
        snapshot = cli("ultracode", "status", model=True)
        assert snapshot["effective"]["enabled"] is False and not snapshot["instructions"]
        settle(initial_off, turn)
        finish(turn, gate)
        check("next admitted turn receives explicit off snapshot")

        cli("ultracode", "on")
        turn, gate = start("unmatched external admission", str(uuid.uuid4()))
        before = server.provider.count
        snapshot = cli("ultracode", "status", model=True)
        assert snapshot["effective"] is None and not snapshot["instructions"]
        assert server.provider.count == before
        finish(turn, gate)
        check("unreserved external turn cannot activate latest requested on")

        lost = reserve()
        turn, gate = start("lost start acknowledgement", lost, lost_ack=True)
        cli("goal", "input-settle", "--submission-id", lost, "--outcome", "uncertain")
        before = server.provider.count
        snapshot = cli("ultracode", "status", model=True)
        assert snapshot["effective"]["submission"] == lost
        assert snapshot["effective"]["enabled"] is True
        assert server.provider.count == before
        settle(lost, turn)
        finish(turn, gate)
        check("lost start ACK reconciles positive client item without replay", turn=turn)

        server.restart()
        server.rpc.call("thread/resume", {"threadId": server.parent,
            "developerInstructions": instructions})
        cli("ultracode", "provenance", "--load-kind", "cold-resume")
        queued = reserve()
        gate = server.provider.gate()
        server.provider.root_steps.append((message("queued wake"), gate))
        server.rpc.call("thread/queue/add", {"threadId": server.parent,
            "clientUserMessageId": queued, "input": [{"type": "text", "text": "queued wake after controlled cold load"}]})
        wait(lambda: server.provider.requests[-1]["body"]["input"] and
             "queued wake after controlled cold load" in json.dumps(server.provider.requests[-1]["body"]),
             "queued wake did not auto-consume")
        turn = server.rpc.call("thread/turns/list", {"threadId": server.parent,
            "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"})["data"][0]["id"]
        snapshot = cli("ultracode", "status", model=True)
        assert snapshot["effective"]["submission"] == queued
        assert snapshot["effective"]["enabled"] is True
        cli("ultracode", "off")
        assert cli("ultracode", "status", model=True)["effective"] == snapshot["effective"]
        finish(turn, gate)
        check("qualified root cold-load queued wake retains pre-add snapshot", turn=turn)
        server.report.update(tested_observer_callback=False, tested_goal_callback=False,
                             tested_real_provider=False)
        passed = True
    finally:
        server.close(passed)
    print(json.dumps({"passed": passed, "checks": server.report["checks"],
                      "evidence": str(evidence / "report.json")}, indent=2))


def observer_resume(binary, runtime, evidence):
    """Use the actual observer cold-load and goal queue admission, then compact mid-turn."""
    bootstrap = (REPO / "docs/ultracode-bootstrap.txt").read_text()
    original = "Original observer developer text 81073.\n\n" + bootstrap
    config = ("developer_instructions=" + json.dumps(original) + "\n"
              "model_auto_compact_token_limit=200000\n")
    f = CustomGoalServer(runtime, binary, evidence, config=config)
    AdmissionProvider.install(f.provider)
    passed = False
    try:
        result = subprocess.run([str(f.runtime), "ultracode", "provenance", "--thread", f.parent,
            "--load-kind", "fresh", "--developer-instructions-stdin", "--json"],
            env=f.env, input=original, text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, (result.stdout, result.stderr)
        f.cli("ultracode", "on", "--thread", f.parent, "--json")
        initial, _ = f.start_turn(text="materialize the qualified root before restart")
        assert f.terminal(initial)["status"] == "completed"
        f.goal("provenance", "--cwd", f.work, "--load-kind", "fresh")
        f.goal("start", "--objective", "Verify actual Ultracode observer cold wake")
        f.restart()
        assert f.thread()["status"]["type"] == "notLoaded"
        final_gate = f.provider.gate()
        tool = {"type": "function_call", "name": "unknown_admission_fixture_tool",
                "arguments": "{}", "call_id": "observer-midturn-compact"}
        f.provider.root_steps.extend([(tool, None, 330000), (message("local compact summary"), None),
                                      (message("observer wake completed"), final_gate)])
        before = f.provider.count
        observer = f.observe()
        wait(lambda: f.provider.count >= before + 3, "actual observer wake did not compact mid-turn", timeout=20)
        for request in f.provider.requests[before:before + 3]:
            developer = "\n".join(part.get("text", "") for item in request["body"].get("input", [])
                if item.get("role") == "developer" for part in item.get("content", []))
            assert original in developer, "observer cold resume lost original complete developer composition"
        status = f.cli("ultracode", "status", "--thread", f.parent, "--json")
        assert status["effective"]["enabled"] is True, status
        assert "Original observer developer text 81073." not in json.dumps(status), "private startup text leaked into status"
        assert f.rpc.generation == status["effective"]["generation"]
        with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
            row = database.execute("SELECT client,accepted_turn FROM goal_dispatches WHERE root=?",
                                   (f.parent,)).fetchone()
        assert row == (status["effective"]["submission"], status["effective"]["turn"]), row
        off = f.cli("ultracode", "off", "--thread", f.parent, "--json")
        assert off["effective"] == status["effective"] and off["requested"]["enabled"] is False
        goal = f.goal()["goal"]
        f.mutate("pause", goal)
        stop(observer)
        final_gate.set()
        completed = f.terminal(status["effective"]["turn"])
        assert completed["status"] == "completed"
        items = f.rpc.call("thread/items/list", {"threadId": f.parent,
            "turnId": status["effective"]["turn"], "sortDirection": "desc", "limit": 100})["data"]
        assert any(item["item"]["type"] == "contextCompaction" for item in items), items
        f.report.update(checks=["actual observer prior-qualified cold load preserves original text",
                               "actual goal callback reserves snapshot before queue/add",
                               "automatic mid-turn compaction retains complete original bootstrap",
                               "status excludes private developer text",
                               "toggle off preserves in-flight observer turn snapshot"],
                        tested_automatic_compaction=True, tested_goal_callback=True)
        ordinary_park_callback(f)
        passed = True
    finally:
        f.close(passed)
    print(json.dumps({"passed": passed, "evidence": str(evidence / "report.json")}))


def ordinary_park_callback(f):
    """Exercise deliver(), whose lock/reservation boundary differs from goal_step()."""
    # This ordinary park has a live terminal client, unlike the preceding detached wake.
    f.rpc.call("thread/resume", {"threadId": f.parent})
    f.cli("ultracode", "on", "--thread", f.parent, "--json")
    initial = str(uuid.uuid4())
    f.goal("input", "--submission-id", initial)
    release = f.work / "shell-release"
    shell = (f'i=0; while [ ! -f {shlex.quote(str(release))} ] && [ "$i" -lt 400 ]; '
             'do sleep .1; i=$((i+1)); done; '
             f'test -f {shlex.quote(str(release))} || exit 124; printf "ordinary-shell-complete\\n"')
    command = "# codex-plus-shell: auto\n" + shlex.join([str(f.runtime), "run", "--", "/bin/sh", "-c", shell])
    code = "text(await tools.exec_command(" + json.dumps({"cmd": command, "yield_time_ms": 1000,
                                                          "max_output_tokens": 100, "login": False}) + "));"
    origin_gate = f.provider.gate()
    f.provider.root_steps.extend([(execute(code), None), (message("ordinary work parked"), origin_gate)])
    before = f.provider.count
    origin = f.rpc.call("turn/start", {"threadId": f.parent, "clientUserMessageId": initial,
        "input": [{"type": "text", "text": "run ordinary finite work"}]})["turn"]["id"]
    terminals = wait(lambda: f.rpc.call("thread/backgroundTerminals/list", {"threadId": f.parent})["data"],
                     "ordinary native shell did not start")
    wait(lambda: f.provider.count >= before + 2, "ordinary root did not wait after shell launch")
    status = f.cli("ultracode", "status", "--thread", f.parent, "--json")
    assert status["effective"]["submission"] == initial and status["effective"]["enabled"] is True
    f.goal("input-settle", "--submission-id", initial, "--accepted", "--turn", origin)
    registered = f.cli("park", "--parent", f.parent, "--process", terminals[0]["processId"],
                       "--reason", "ordinary Ultracode admission acceptance")
    assert registered["state"] == "armed", registered
    origin_gate.set()
    assert f.terminal(origin)["status"] == "completed"
    callback_gate = f.provider.gate()
    f.provider.root_steps.append((message("ordinary callback complete"), callback_gate))
    before = f.provider.count
    observer = f.observe()
    release.touch()
    wait(lambda: f.provider.count == before + 1, "ordinary completion callback did not start", timeout=20)
    status = f.cli("ultracode", "status", "--thread", f.parent, "--json")
    assert status["effective"]["enabled"] is True
    with sqlite3.connect(f.home / "codex-plus/background.sqlite") as database:
        client = database.execute("SELECT client FROM callbacks WHERE parent=?", (f.parent,)).fetchone()[0]
    assert status["effective"]["submission"] == client and client != initial
    off = f.cli("ultracode", "off", "--thread", f.parent, "--json")
    assert off["effective"] == status["effective"] and off["requested"]["enabled"] is False
    stop(observer)
    callback_gate.set()
    assert f.terminal(status["effective"]["turn"])["status"] == "completed"
    f.report["checks"].extend(["actual ordinary park callback reserves before queue/add",
                                "ordinary callback keeps frozen initial client snapshot across toggle"])
    f.report["tested_ordinary_park_callback"] = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(".build/upstream-0163/official-x86/bin/codex"))
    parser.add_argument("--runtime", type=Path, default=Path(".build/runtime-target/debug/codex-plus-runtime"))
    parser.add_argument("--evidence", type=Path, default=Path(".build/ultracode/admission"))
    args = parser.parse_args()
    run(args.binary.resolve(), args.runtime.resolve(), args.evidence.resolve())
    observer_resume(args.binary.resolve(), args.runtime.resolve(), args.evidence.resolve() / "observer")
