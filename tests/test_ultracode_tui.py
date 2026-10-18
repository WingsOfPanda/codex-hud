"""Actual native PTY with a private helper stub and app-server protocol fixture.

This checks frontend transport/layout, not runtime or stock-daemon compatibility.
Run with the native-smoke pyte interpreter and --binary PATH.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

from fixtures.native_server import AppServer
from native_smoke import Terminal, assert_compact_hud, composer_ready, wait


REPO = Path(__file__).resolve().parents[1]
EXISTING = "Ultracode PTY existing developer instructions sentinel 65781."


class LocalServer(AppServer):
    def __init__(self, root, home, workspace, status_line):
        self.status_line = status_line
        self.developer_instructions = EXISTING
        socket = home / "app-server-control/app-server-control.sock"
        socket.parent.mkdir(mode=0o700)
        super().__init__(root, workspace, 2, socket_path=socket)
        socket.chmod(0o600)
        self.shells = [self.shell(64321, "ultracode-private-background-fixture")]

    def reply(self, method, params):
        result = super().reply(method, params)
        if method == "config/read":
            config = result["result"]["config"]
            config["developer_instructions"] = self.developer_instructions
            config["tui"] = {"status_line": self.status_line}
            if "model-with-reasoning" in self.status_line:
                config["model_reasoning_effort"] = "high"
        if method in ("thread/start", "thread/resume") and "model-with-reasoning" in self.status_line:
            result["result"]["reasoningEffort"] = "high"
        return result


def helper_script(bootstrap):
    # The stub stores full bytes privately, but records only hashes and booleans in its log.
    return f'''#!{sys.executable}
import hashlib, json, os
from pathlib import Path
import sys
args = sys.argv[1:]
home = Path(os.environ["CODEX_HOME"])
root = args[args.index("--thread")+1] if "--thread" in args else ""
stdin = sys.stdin.buffer.read(1024*1024+1)
assert len(stdin) <= 1024*1024
record = {{"args":args,"stdin_bytes":len(stdin),"stdin_sha256":hashlib.sha256(stdin).hexdigest(),
          "inherited_thread_removed":"CODEX_THREAD_ID" not in os.environ}}
if args[:2] == ["ultracode","provenance"]:
    record.update(bootstrap_complete={bootstrap!r}.encode() in stdin,
                  existing_preserved={EXISTING!r}.encode() in stdin)
    (home / "provenance-input.bin").write_bytes(stdin)
with (home / "helper-calls.jsonl").open("a") as log:
    log.write(json.dumps(record)+"\\n")
if args and args[0] == "goal":
    print(json.dumps({{"version":1,"root":root,"goal":None,"attention":None}}))
elif args[:2] == ["ultracode","load-instructions"]:
    saved = home / "provenance-input.bin"
    print(json.dumps({{"root":root,"developer_instructions":saved.read_text() if saved.exists() else None}}))
elif args and args[0] == "ultracode":
    path = home / "codex-plus/ultracode" / (root+".json")
    path.parent.mkdir(parents=True,exist_ok=True)
    snapshot = json.loads(path.read_text()) if path.exists() else {{
        "version":1,"root":root,"requested":{{"enabled":False,"revision":0,"policy_version":1}},
        "effective":None,"availability":"available","pending":[]}}
    if (home / "unqualified").exists():
        snapshot.update(availability="unavailable",unavailable_reason="No qualified bootstrap for this legacy root. Start a new Plus root session.")
    if len(args)>1 and args[1] in ("on","off"):
        snapshot["requested"]["enabled"] = args[1] == "on"
        snapshot["requested"]["revision"] += 1
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot))
    temporary.replace(path)
    print(json.dumps(snapshot))
else:
    print("{{}}")
'''


def calls(home):
    path = home / "helper-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def project_effective(home, root, enabled):
    path = home / "codex-plus/ultracode" / f"{root}.json"
    snapshot = json.loads(path.read_text())
    snapshot["effective"] = dict(enabled=enabled, revision=snapshot["requested"]["revision"],
        policy_version=1, submission="fixture-submission", turn="fixture-turn", generation="fixture-generation")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot))
    temporary.replace(path)


def footer(terminal):
    lines = terminal.screen.display
    composer = max(index for index, line in enumerate(lines) if line.lstrip().startswith("›"))
    return [line for line in lines[composer + 1:] if line.strip()]


def run_case(binary, root_directory, evidence, bootstrap, *, empty=False, unqualified=False, model_line=False):
    evidence.mkdir(parents=True, exist_ok=True)
    home, workspace = root_directory / "home", root_directory / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir()
    root = "00000000-0000-0000-0009-000000000001"
    status_line = [] if empty else (["model-with-reasoning", "thread-id"] if model_line else ["thread-id"])
    config = (f'model="gpt-6-sol"\nmodel_provider="openai"\n'
        f'developer_instructions={json.dumps(EXISTING)}\n'
        'suppress_unstable_features_warning=true\ncheck_for_update_on_startup=false\n'
        'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
        f'[tui]\nstatus_line={json.dumps(status_line)}\n'
        f'[projects."{workspace}"]\ntrust_level="trusted"\n')
    (home / "config.toml").write_text(config)
    (home / "auth.json").write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
    if unqualified:
        (home / "unqualified").touch()
    server = LocalServer(root, home, workspace, status_line)
    terminal = None
    passed = False
    report = dict(fixture="native PTY with helper stub and app-server fixture",
        stock_compatibility_test=False, runtime_semantics_test=False,
        explicit_empty_status_line=empty, unqualified_projection=unqualified)
    try:
        terminal = Terminal(binary, home, workspace)
        wait([terminal], lambda: composer_ready(terminal.text)
            and any(call["args"][:2] == ["ultracode", "provenance"] for call in calls(home)),
            "native startup did not send bounded Ultracode provenance", 30)
        proof = next(call for call in calls(home) if call["args"][:2] == ["ultracode", "provenance"])
        stdin = (home / "provenance-input.bin").read_bytes()
        assert proof["bootstrap_complete"] and proof["existing_preserved"], proof
        assert bootstrap.encode() in stdin and EXISTING.encode() in stdin
        assert stdin.count(bootstrap.encode()) == 1, "bootstrap appended more than once"
        assert "--developer-instructions-stdin" in proof["args"]
        assert proof["inherited_thread_removed"]
        startup = next(request for request in server.requests if request.get("method") == "thread/start")
        assert startup["params"]["developerInstructions"].encode() == stdin, "attested bytes differ from startup RPC"
        proof["startup_rpc_equal_stdin"] = True
        report["provenance"] = proof

        def command(action):
            before = len(calls(home))
            os.write(terminal.master, f"\x15/ultracode {action}".encode())
            wait([terminal], lambda: f"/ultracode {action}" in terminal.text, "slash command not editable")
            os.write(terminal.master, b"\r")
            wait([terminal], lambda: any(call["args"][:2] == ["ultracode", action]
                for call in calls(home)[before:]), f"/ultracode {action} did not reach local helper")

        command("on")
        if unqualified:
            wait([terminal], lambda: "Ultracode unavailable" in terminal.text
                 and "Start a new Plus root session" in terminal.text,
                 "legacy root did not explain unavailable mode")
        else:
            wait([terminal], lambda: "Ultracode on pending" in terminal.text,
                 "on command falsely confirmed activation or lost pending acknowledgement")
            project_effective(home, root, True)
            if not empty:
                wait([terminal], lambda: any(root in line and "ultracode" in line.lower()
                    and "pending" not in line for line in footer(terminal)), "active mode was not on configured status line")
        os.write(terminal.master, b"ultracode-editable-draft")
        wait([terminal], lambda: "ultracode-editable-draft" in terminal.text, "mode refresh blocked composer")
        assert_compact_hud(terminal.text)
        if empty:
            assert not any(root in line and "ultracode" in line.lower() for line in footer(terminal)), "explicit empty status line rendered badge"
        else:
            badge = [line for line in footer(terminal) if root in line and "ultracode" in line.lower()]
            assert len(badge) == 1 and root in badge[0], "mode added a separate activity row"
            if model_line:
                assert " high ultracode · " in badge[0], badge[0]
                assert badge[0].lower().count("ultracode") == 1, badge[0]
        (evidence / "on-draft-screen.txt").write_text(terminal.text + "\n")
        command("status")
        command("off")
        if not unqualified:
            wait([terminal], lambda: "Ultracode off pending" in terminal.text, "off request lost pending acknowledgement")
            project_effective(home, root, False)
            if not empty:
                wait([terminal], lambda: not any(root in line and "ultracode" in line.lower() for line in footer(terminal)), "effective off retained active badge")
        os.write(terminal.master, b"after-off-draft")
        wait([terminal], lambda: "after-off-draft" in terminal.text, "off command blocked composer")
        assert_compact_hud(terminal.text)
        saved_config = tomllib.loads((home / "config.toml").read_text())
        assert saved_config["tui"]["status_line"] == status_line, "mode command rewrote custom status-line config"
        assert not any(request.get("method") in ("turn/start", "turn/steer", "thread/queue/add", "thread/queue/start")
                       for request in server.requests), "display/control initiated model work"
        assert not server.errors, server.errors
        control = [call for call in calls(home) if call["args"][:1] == ["ultracode"]
                   and call["args"][1] in ("on", "off", "status")]
        assert [call["args"][1] for call in control] == ["on", "status", "off"], control
        assert all(call["stdin_bytes"] == 0 and call["inherited_thread_removed"] for call in control)
        report.update(model_turn_requests=0, controls=["on", "status", "off"],
            configured_status_line_preserved=True, composer_editable=True, hud_maximum_rows=3)
        passed = True
    finally:
        if terminal:
            (evidence / "tui.ansi").write_bytes(terminal.output)
            (evidence / "last-screen.txt").write_text(terminal.text + "\n")
            terminal.close()
        server.close()
        report.update(passed=passed, helper_calls=calls(home))
        (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def run(binary, evidence, native_source):
    bootstrap = (REPO / "docs/ultracode-bootstrap.txt").read_text()
    assert bootstrap.endswith("\n"), "bootstrap lost its trailing newline"
    assert native_source.read_text() == bootstrap, "native bootstrap source drifted from docs"
    assert bootstrap.encode() in binary.read_bytes(), "built native omits current complete bootstrap"
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uc-tui-") as temporary:
        root = Path(temporary)
        package = root / "package"
        package.mkdir()
        native = package / "codex"
        shutil.copy2(binary, native)
        host = binary.with_name("codex-code-mode-host")
        if host.exists():
            shutil.copy2(host, package / host.name)
        helper = package / "codex-plus-runtime"
        helper.write_text(helper_script(bootstrap))
        helper.chmod(0o700)
        for name, options in (("custom", {}), ("model", {"model_line": True}), ("empty", {"empty": True}),
                              ("unqualified", {"unqualified": True})):
            directory = root / name
            directory.mkdir()
            run_case(native, directory, evidence / name, bootstrap, **options)
    summary = dict(passed=True, fixture="helper stub transport, not stock-daemon acceptance",
        bootstrap_sha256=hashlib.sha256(bootstrap.encode()).hexdigest(),
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        source_equals_docs=True, binary_contains_complete_bootstrap=True,
        cases=["custom", "model", "empty", "unqualified"], model_turn_requests=0, evidence=str(evidence))
    (evidence / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=Path(".build/ultracode/tui"))
    parser.add_argument("--native-source", type=Path, default=REPO / ".build/release030/x86/source/codex-rs/tui/src/plus_ultracode_instructions.txt")
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve(), args.native_source.resolve())
