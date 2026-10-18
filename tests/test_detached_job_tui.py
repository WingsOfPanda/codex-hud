"""Actual detached execution across TUI reconnects on a private official daemon."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fixtures.detached_jobs import DetachedJobServer
from native_smoke import Terminal, assert_compact_hud, composer_ready, shell_rows, wait
from test_background_integration import wait as wait_server
from test_detached_jobs_stock import finished, launched, operations


def run(binary, runtime, stock, evidence):
    f = DetachedJobServer(runtime, stock, evidence)
    terminal = None
    attachment = 0
    passed = False
    topic = "detached-tui-job"
    label = f"Background · {topic} · "

    def capture(name):
        (evidence / f"{name}.txt").write_text(terminal.text + "\n")

    def save_terminal():
        (evidence / f"tui-{attachment}.ansi").write_bytes(terminal.output)
        (evidence / f"tui-{attachment}-last.txt").write_text(terminal.text + "\n")

    def elapsed():
        row = next(row for row in shell_rows(terminal.text) if row.startswith(label))
        match = re.search(r" · (\d+):(\d{2})$", row)
        assert match, row
        return int(match[1]) * 60 + int(match[2])

    def verify_executor():
        current = f.job(ident)
        assert current["state"] == "running" and current["executor"] == executor, current
        process = Path("/proc") / str(executor["pid"])
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        assert fields[0] != "Z" and int(fields[19]) == executor["start_ticks"]
        assert (process / "exe").resolve(strict=True) == f.runtime
        before = log.stat().st_size
        wait_server(lambda: log.stat().st_size > before, "workload log stopped across TUI reconnect")

    try:
        assert f.report["stock_version"] == "codex-cli 0.163.0", f.report
        manifest, directory = f.manifest(topic, deadline=120)
        # Leave time for PTY startup/resize; this remains an explicit fixture deadline.
        source = directory / "workload.py"
        script = source.read_text()
        assert "time.monotonic()+25" in script
        source.write_text(script.replace("time.monotonic()+25", "time.monotonic()+105"))
        command = f.commands(operations(manifest))
        ident, running = launched(f, command)
        executor = running["executor"]
        f.finish(command)
        wait_server(lambda: f.thread()["status"]["type"] == "idle", "origin root did not become idle")
        # Isolate display from stock's separate model-backed automatic title generation.
        f.rpc.call("thread/name/set", {"threadId": f.parent, "name": "detached-job-ui-acceptance"})
        log = f.job_directory(ident) / "stdout.log"
        before = f.provider.count

        attachment = 1
        terminal = Terminal(binary, f.home, f.work, parent=f.parent)
        wait([terminal], lambda: composer_ready(terminal.text) and
             any(row.startswith(label) for row in shell_rows(terminal.text)),
             "actual running detached execution absent from resumed TUI", 30)
        first_elapsed = elapsed()
        os.write(terminal.master, b"detached-editable-draft")
        wait([terminal], lambda: "detached-editable-draft" in terminal.text,
             "running detached row blocked the draft")
        replies = terminal.resize(12, 65)
        wait([terminal], lambda: terminal.resized_frame(replies) and
             "detached-editable-draft" in terminal.text and
             any(row.startswith(label) for row in shell_rows(terminal.text)),
             "narrow detached HUD lost row or draft")
        assert_compact_hud(terminal.text)
        capture("running-narrow-editable")
        replies = terminal.resize(32, 120)
        wait([terminal], lambda: terminal.resized_frame(replies), "normal resize did not settle")
        assert_compact_hud(terminal.text)
        assert f.provider.count == before, "TUI attach, draft or HUD refresh called the provider"
        save_terminal()
        terminal.close()
        terminal = None
        verify_executor()

        attachment = 2
        terminal = Terminal(binary, f.home, f.work, parent=f.parent)
        wait([terminal], lambda: composer_ready(terminal.text) and
             any(row.startswith(label) for row in shell_rows(terminal.text)),
             "reopened TUI did not show the existing execution", 30)
        assert elapsed() >= first_elapsed, "TUI reconnect reset durable execution elapsed time"
        os.write(terminal.master, b"reopened-editable-draft")
        wait([terminal], lambda: "reopened-editable-draft" in terminal.text,
             "reopened detached HUD blocked the draft")
        assert_compact_hud(terminal.text)
        capture("reopened-running-editable")
        verify_executor()
        assert f.provider.count == before, "TUI reopen or display refresh called the provider"

        (directory / "release").touch()
        result = finished(f, ident, directory)
        assert result["result"]["exit_code"] == 0
        wait_server(lambda: f.delivered(), "actual detached completion did not wake the root")
        wait_server(lambda: f.thread()["status"]["type"] == "idle", "completion wake did not settle")
        wait([terminal], lambda: composer_ready(terminal.text) and
             not any(row.startswith(label) for row in shell_rows(terminal.text)) and
             "reopened-editable-draft" in terminal.text,
             "completed execution row remained or completion erased the draft", 20)
        assert_compact_hud(terminal.text)
        assert f.provider.count == before + 1, "completion failed to produce exactly one wake"
        capture("completed-row-removed-draft-retained")
        f.report.update(scenario="detached-job-tui", ui_binary=str(binary),
            ui_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), execution=ident,
            executor=executor, first_elapsed=first_elapsed, display_model_requests=0,
            completion_model_requests=1, tui_attachments=2, final_state=f.state())
        passed = True
    except BaseException:
        f.report["failure"] = traceback.format_exc()
        raise
    finally:
        if terminal:
            save_terminal()
            terminal.close()
        f.close(passed)
    print(json.dumps(dict(passed=True, scenario="detached-job-tui", display_model_requests=0,
                          completion_model_requests=1, evidence=str(evidence / "report.json"))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--stock-binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.runtime.resolve(strict=True),
        args.stock_binary.resolve(strict=True), args.evidence.resolve())
