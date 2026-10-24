#!/usr/bin/env python3
"""Verify fleet routing probes in an isolated home, without daemon or model work."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT_ID = "00000000-0000-4000-8000-000000000001"


def verify(command, evidence):
    with tempfile.TemporaryDirectory(prefix="release-operator-") as temporary:
        home = Path(temporary) / "home"
        env = dict(os.environ, CODEX_HOME=str(home))
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_CONFIG_OVERRIDES", None)

        def call(*args):
            result = subprocess.run([str(command), *args], env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, (args, result.returncode, result.stderr)
            return json.loads(result.stdout)

        jobs = call("job", "list", "--all")
        assert jobs == {"execution_record_format": 1, "packages": [], "schema": 1,
                        "unresolved": []}, jobs
        assert not home.exists(), "read-only job inventory created state"
        status = call("ultracode", "status", "--thread", ROOT_ID, "--json")
        expected = {"version": 1, "root": ROOT_ID, "availability": "unavailable",
                    "effective": None, "load": None, "pending": [], "instructions": "",
                    "requested": {"enabled": False, "policy_version": 1, "revision": 0},
                    "journal_directory": str(home / "codex-plus/ultracode" / ROOT_ID / "runs")}
        assert set(status) == set(expected) | {"unavailable_reason"}, status
        for key, value in expected.items():
            assert status[key] == value, (key, status[key], value)
        assert status["unavailable_reason"].startswith("Local daemon unavailable: ")
        assert status["unavailable_reason"].endswith("; no replay.")
        created = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
        assert created == ["codex-plus", "codex-plus/background.sqlite"], created
        report = {"passed": True, "command": str(command), "job": jobs,
                  "ultracode_stable_fields": {key: value for key, value in status.items()
                                               if key not in ("journal_directory", "unavailable_reason")},
                  "ultracode_files_created": created,
                  "status_is_filesystem_read_only": False,
                  "scope": "fresh private home; no daemon; no shared fleet state"}
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", type=Path, required=True,
                        help="codex-plus launcher or direct native runtime")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    verify(args.command.resolve(strict=True), args.evidence.resolve())
