"""Private native terminal check: cached quota updates and a local countdown."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time

from fixtures.native_server import AppServer
from native_smoke import Terminal, assert_compact_hud, composer_ready, wait, worker_rows


class ResetServer(AppServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.quota(None)

    def reply(self, method, params):
        if method == "account/read":
            return {"result": {"account": {"type": "chatgpt", "email": None,
                                           "planType": "plus"}, "requiresOpenaiAuth": True}}
        if method == "account/rateLimits/read":
            return {"result": {"rateLimits": self.snapshot,
                               "rateLimitsByLimitId": {"codex": self.snapshot}}}
        result = super().reply(method, params)
        if method == "config/read":
            result["result"]["config"]["tui"] = {
                "status_line": ["weekly-limit", "weekly-reset"]}
        return result

    def quota(self, reset, *, weekly_primary=False):
        weekly = {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": reset}
        short = {"usedPercent": 10, "windowDurationMins": 300,
                 "resetsAt": int(time.time()) + 3600}
        self.snapshot = {
            "limitId": "codex", "primary": weekly if weekly_primary else short,
            "secondary": short if weekly_primary else weekly}


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    terminal = None
    stages = []
    with tempfile.TemporaryDirectory(prefix="weekly-reset-") as temporary:
        root = Path(temporary)
        home, workspace = root / "home", root / "workspace"
        home.mkdir(mode=0o700)
        workspace.mkdir()
        (home / "config.toml").write_text('model="gpt-6-sol"\n'
            'analytics.enabled=false\nfeatures.daemon_auto_start=false\n'
            'features.plugins=false\nsuppress_unstable_features_warning=true\n'
            '[tui]\nstatus_line=["weekly-limit","weekly-reset"]\n')
        server = ResetServer("00000000-0000-0000-0010-000000000001", workspace, 2)
        try:
            terminal = Terminal(binary, home, workspace, server.url)
            wait([terminal], lambda: composer_ready(terminal.text) and
                 "reset unknown" in terminal.text, "missing reset must be explicit", 25)
            stages.append("unknown timestamp")

            def refresh_quota():
                # The stock client uses full account reads for display, not sparse stream updates.
                before = sum(r["method"] == "account/rateLimits/read" for r in server.requests)
                os.write(terminal.master, b"\x15/status")
                wait([terminal], lambda: "› /status" in terminal.text,
                     "status command not entered")
                os.write(terminal.master, b"\r")
                wait([terminal], lambda: sum(r["method"] == "account/rateLimits/read"
                     for r in server.requests) > before, "explicit quota refresh missing")

            server.quota(int(time.time()) + 2 * 86400 + 5 * 3600 + 120)
            refresh_quota()
            wait([terminal], lambda: "resets in 2d 5h" in terminal.text and
                 "weekly 80% left" in terminal.text, "secondary weekly countdown missing")
            stages.append("weekly remaining plus exact compact countdown")
            os.write(terminal.master, b"reset-draft")
            wait([terminal], lambda: "reset-draft" in terminal.text, "countdown blocked input")
            wait([terminal], lambda: len(worker_rows(terminal.text)) == 2,
                 "worker metadata not ready for countdown layout check")
            assert_compact_hud(terminal.text, rows=2)
            server.workers = []
            server.quota(int(time.time()) + 3, weekly_primary=True)
            refresh_quota()
            os.write(terminal.master, b"reset-draft")
            wait([terminal], lambda: "resets in <1m" in terminal.text,
                 "primary weekly window was not selected")
            before = len(server.requests)
            wait([terminal], lambda: "reset due" in terminal.text,
                 "cached reset did not count down locally", 10)
            assert "reset-draft" in terminal.text
            assert not any(request["method"] == "account/rateLimits/read"
                           for request in server.requests[before:]), "countdown polled account"
            stages.append("swapped windows and local expiry without quota fetch")
            server.quota(None)
            refresh_quota()
            wait([terminal], lambda: "reset unknown" in terminal.text,
                 "missing reset retained obsolete deadline")
            os.write(terminal.master, b"\x15/statusline")
            wait([terminal], lambda: "› /statusline" in terminal.text,
                 "status-line command not entered")
            os.write(terminal.master, b"\r")
            wait([terminal], lambda: "Weekly reset" in terminal.text,
                 "weekly reset missing from status-line picker")
            stages.append("selectable status-line item")
            assert not server.errors, server.errors
            assert not any(request["method"] in ("turn/start", "turn/steer")
                           for request in server.requests)
        finally:
            if terminal:
                (evidence / "screen.txt").write_text(terminal.text)
                (evidence / "terminal.ansi").write_bytes(terminal.output)
                terminal.close()
            server.close()
            (evidence / "rpc.json").write_text(json.dumps(server.requests, indent=2) + "\n")
    report = {"passed": True, "binary": str(binary), "stages": stages,
              "fixture": "private protocol fixture; no provider", "model_calls": 0}
    (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve())
