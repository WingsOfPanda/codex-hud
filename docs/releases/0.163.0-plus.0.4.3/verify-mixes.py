#!/usr/bin/env python3
"""Native 0.4.2/0.4.3 sidecar gate; uses frozen private-stock fixtures.

Run only after both real, qualified, native packages are available. No installed
state is used. --stock-binary must be the independently qualified official daemon.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
import uuid

sys.dont_write_bytecode = True

OLD_IDENTITIES = {'0.4.2': {'commit': '483912b9ed2783cf239e62f3aec3922730979905',
           'manifests': {'x86_64': '2d72c614ea7e855f7a0732f0db82973aba14583f8ee19fe9d39f88994dae0d1b',
                         'aarch64': '9af40f41da92483d01bfa6a30aea4d64a8edcda16e4b881839996b4f44edf7ac'}}}
STOCK_SHAS = {
    "x86_64": "399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215",
    "aarch64": "8a96f66d059d653e68bfd5f3c79337e6f1d7d170d80bf705eb7d0249b34c0ece",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def write(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def native_elf(path):
    with path.open("rb") as stream:
        header = stream.read(20)
    assert header[:6] == b"\x7fELF\x02\x01", (path, "expected little-endian ELF64")
    machine = int.from_bytes(header[18:20], "little")
    assert machine == {"x86_64": 62, "aarch64": 183}[platform.machine()], (path, machine)


def package_identity(package, version, commit, manifest_sha256):
    manifest_path = package / "codex-plus.json"
    assert digest(manifest_path) == manifest_sha256, (package, "unqualified manifest SHA")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == 1
    assert manifest["release"] == f"0.163.0-plus.{version}", manifest["release"]
    assert manifest["plus_commit"] == commit, (package, "source commit mismatch")
    assert type(manifest["coordinator_protocol"]) is int and manifest["coordinator_protocol"] == 5
    assert type(manifest["execution_record_format"]) is int and manifest["execution_record_format"] == 1
    assert manifest["target"] == f"{platform.machine()}-unknown-linux-gnu"
    assert manifest["upstream"] == {"version": "0.163.0", "tag": "rust-v0.163.0",
                                    "commit": "83086188ae0614bd882818ff1aa23f3194962012"}
    actual = {}
    for path in sorted(package.rglob("*")):
        assert not path.is_symlink() and (path.is_file() or path.is_dir()), path
        if path.is_file() and path != manifest_path:
            actual[path.relative_to(package).as_posix()] = {
                "sha256": digest(path), "size": path.stat().st_size,
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}"}
    assert actual == manifest["files"], f"package inventory mismatch: {package}"
    tree = hashlib.sha256(canonical(actual).encode("ascii")).hexdigest()
    assert tree == manifest["tree_sha256"], package
    for name in ("bin/codex", "bin/codex-plus-runtime", "bin/codex-code-mode-host",
                 "codex-resources/bwrap"):
        native_elf(package / name)
    assert (package / "bin/codex-plus").is_file()
    return {"path": str(package), "manifest_sha256": digest(manifest_path),
            "tree_sha256": tree, "helper_sha256": actual["bin/codex-plus-runtime"]["sha256"],
            "manifest": manifest}


def distinct_packages(identities):
    assert identities["old"]["helper_sha256"] != identities["new"]["helper_sha256"], "same helper cannot prove two-version compatibility"
    assert identities["old"]["manifest_sha256"] != identities["new"]["manifest_sha256"], "same manifest cannot prove two-version compatibility"


def execution_record_bytes(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file() and path.suffix == ".json"}


def state_clock_bytes(raw):
    state = json.loads(raw)
    assert isinstance(state, dict) and "updated_at" in state, "execution state timestamp missing"
    clock = state["updated_at"]
    assert type(clock) in (int, float) and math.isfinite(clock), "execution state timestamp is not finite"
    # Refuse ambiguous keys: only the root clock may receive this allowance.
    assert raw.count(b'"updated_at"') == 1, "ambiguous execution state timestamp"
    match = re.search(rb'("updated_at":)(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)(?=[,}])', raw)
    assert match and json.loads(match[2]) == clock, "execution state timestamp format changed"
    return clock, raw[:match.start(2)] + b"<updated_at>" + raw[match.end(2):]


def validate_execution_record_transition(before, after, command=None):
    required = {"manifest.json", "state.json", "result.json", "observation.json"}
    assert required <= set(before), "required execution record missing"
    assert set(before) == set(after), "execution record file set changed"
    before_clock, before_state = state_clock_bytes(before["state.json"])
    after_clock, after_state = state_clock_bytes(after["state.json"])
    refresh = tuple(command or ()) in (("job", "maintenance", "enter"), ("job", "maintenance", "status"))
    for name in before:
        if name != "state.json" or not refresh:
            assert before[name] == after[name], f"execution record bytes changed outside maintenance allowance: {name}"
    if refresh:
        assert after_clock >= before_clock, "execution state timestamp moved backwards"
        assert before_state == after_state, "execution state bytes beyond updated_at changed"
        assert before["state.json"] == after["state.json"] or after_clock > before_clock, "timestamp format changed without clock advance"
    return {"timestamp_refresh_allowed": refresh, "timestamp_refreshed": after_clock != before_clock,
            "updated_at_before": before_clock, "updated_at_after": after_clock}


def source_identity(source, commit):
    assert re.fullmatch(r"[0-9a-f]{40}", commit), "source commit must be a full SHA"
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    assert actual == commit, (actual, commit)
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"], text=True)
    assert not dirty, (source, "frozen fixture source is dirty", dirty)
    return actual


def ledger_snapshot(home):
    database = home / "codex-plus/background.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        schema = [dict(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        tables, identities = {}, {}
        for entry in schema:
            if entry["type"] != "table" or entry["name"].startswith("sqlite_"):
                continue
            table = entry["name"]
            quoted = '"' + table.replace('"', '""') + '"'
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {quoted}")]
            tables[table] = sorted(rows, key=canonical)
            columns = list(connection.execute(f"PRAGMA table_info({quoted})"))
            keys = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
            identities[table] = [{key: row[key] for key in keys} for row in tables[table]]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        schema_value = {"sqlite_master": schema,
                        "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                        "application_id": connection.execute("PRAGMA application_id").fetchone()[0]}
    return {"schema": schema_value,
            "schema_sha256": hashlib.sha256(canonical(schema_value).encode()).hexdigest(),
            "tables": tables, "row_identities": identities}


def owned_processes(home):
    marker = f"CODEX_HOME={home}".encode()
    processes = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            if marker in (entry / "environ").read_bytes().split(b"\0"):
                processes.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return processes


def run(args):
    started = time.monotonic()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    evidence.chmod(0o700)
    report = {"passed": False, "checks": [], "architecture": platform.machine(),
              "external_provider_requests": 0, "remote_model_requests": 0,
              "old_version": args.expected_old_version, "rollback_floor": "0.4.2",
              "rollback_qualified": False,
              "scope": "quiet 0.4.3 to 0.4.2 rollback and adjacent-helper interoperability",
              "limitations": [
                  "Official daemon distribution provenance is a caller prerequisite; this gate records its SHA, native ELF, version and stock RPC boundary.",
                  "Quiet rollback and mixed helper/TUI wire contract only; no rollback with running executors or observer downgrade transfer.",
                  "No 0.3.x compatibility, native TUI interaction, all-command sweep, real provider, advisor or managed-run experiment.",
                  "Source/fixtures are reused unchanged; this sidecar is not a new schema migration."]}
    fixture = None
    identities = {}
    record_previous = None

    def check(name, **details):
        report["checks"].append(dict(name=name, **details))
        write(evidence / "gate.json", report)

    def deadline(signum, frame):
        raise TimeoutError("compatibility gate exceeded its 300-second execution budget")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(300)
    try:
        source = args.source.resolve(strict=True)
        source_commit = source_identity(source, args.source_commit)
        sys.path[:0] = [str(source / "tests"), str(source)]
        import fixtures.custom_goal_server as goal_fixture
        from fixtures.detached_jobs import DetachedJobServer
        from fixtures.upstream_0163 import message
        from test_background_integration import wait
        from test_detached_jobs_stock import completion
        from test_rust_runtime_stock_integration import stop_rust_observers

        fixture_files = ["tests/fixtures/custom_goal_server.py", "tests/fixtures/detached_jobs.py",
                         "tests/test_detached_jobs_stock.py", "tests/test_background_integration.py",
                         "tests/fixtures/upstream_0163.py", "docs/ultracode-bootstrap.txt",
                         "docs/ultracode-policy.md",
                         "docs/releases/0.163.0-plus.0.4.0/verify-operator-probes.py"]
        report["source"] = {"path": str(source), "commit": source_commit,
                            "files": {name: digest(source / name) for name in fixture_files}}
        report["gate_sha256"] = digest(Path(__file__).resolve())
        assert report["gate_sha256"] == args.gate_sha256, "reviewed gate input changed"
        old_identity = OLD_IDENTITIES[args.expected_old_version]
        qualification = (("old", args.old_package, args.expected_old_version, old_identity["commit"],
                          old_identity["manifests"][platform.machine()]),
                         ("new", args.new_package, args.expected_new_version, source_commit,
                          args.new_manifest_sha256))
        for label, package, version, commit, manifest_sha256 in qualification:
            identities[label] = package_identity(package.resolve(strict=True), version, commit, manifest_sha256)
        report["packages"] = identities
        distinct_packages(identities)
        policies = {label: (Path(identities[label]["path"]) / "docs/ultracode-policy.md").read_text()
                    for label in ("old", "new")}
        assert policies["new"] == (source / "docs/ultracode-policy.md").read_text()
        assert "expected to exceed sixteen\n   minutes" in policies["new"]
        assert "expected to exceed eight\n   minutes" in policies["old"]
        report["policy_snapshots"] = {"policy_version": 1,
            "package_policy_sha256": {label: hashlib.sha256(policy.encode()).hexdigest()
                                      for label, policy in policies.items()},
            "semantics": "reservation freezes its helper's text; readers and steering retain the admitted text"}
        binary = args.stock_binary.resolve(strict=True)
        native_elf(binary)
        stock_sha = digest(binary)
        assert stock_sha == STOCK_SHAS[platform.machine()], "official daemon SHA differs from qualified native 0.163.0 distribution"
        version = subprocess.check_output([str(binary), "--version"], text=True, timeout=10).strip()
        assert version == "codex-cli 0.163.0", version
        report["stock"] = {"path": str(binary), "sha256": stock_sha, "version": version}
        check("real native package identities, inventory hashes, protocol 5 and execution format 1")

        probes = runpy.run_path(str(source / fixture_files[-1]))
        probe_reports = {}
        for label in ("old", "new"):
            path = evidence / f"operator-probes-{label}.json"
            probes["verify"](Path(identities[label]["path"]) / "bin/codex-plus", path)
            probe_reports[label] = json.loads(path.read_text())
        for key in ("job", "ultracode_stable_fields", "ultracode_files_created"):
            assert probe_reports["old"][key] == probe_reports["new"][key], ("fresh operator probe drift", key)
        report["operator_probes"] = probe_reports
        check(f"fresh private operator JSON probes unchanged between {args.expected_old_version} and 0.4.3")

        # Keep the fixture's Unix socket under TMPDIR, with the shortest readable prefix.
        goal_fixture.tempfile = SimpleNamespace(TemporaryDirectory=lambda **unused:
            tempfile.TemporaryDirectory(prefix="s-", dir=tempfile.gettempdir()))
        fixture = f = DetachedJobServer(args.new_package.resolve() / "bin/codex-plus-runtime",
                                        binary, evidence / "fixture",
                                        config="features.apps=false\nfeatures.memories=false\n")
        assert len(os.fsencode(f.home / "app-server-control/app-server-control.sock")) < 104, "TMPDIR is too long for a private Unix socket"
        runtimes = {}
        entrypoints = {}
        for label in ("old", "new"):
            destination = f.root / label
            shutil.copytree(Path(identities[label]["path"]), destination)
            runtimes[label] = destination / "bin/codex-plus-runtime"
            assert digest(runtimes[label]) == identities[label]["helper_sha256"]
            assert digest(destination / "codex-plus.json") == identities[label]["manifest_sha256"]
            entrypoints[label] = ([str(runtimes[label])] if label == "old" else
                                 [str(destination / "bin/codex-plus")])
        f.runtime = runtimes["new"]
        f.env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
                     PYTHONDONTWRITEBYTECODE="1")
        instructions = "Private mixed 0.4.x compatibility fixture.\n\n" + (source / "docs/ultracode-bootstrap.txt").read_text()
        f.parent = f.rpc.call("thread/start", {"cwd": str(f.work),
            "developerInstructions": instructions, "model": "gpt-6.1-sol",
            "approvalPolicy": "never", "sandbox": "danger-full-access"})["thread"]["id"]
        f.provider.parent = f.parent
        f.report["parent"] = f.parent
        report["private_root"] = f.parent

        def cli(label, *values, model=False, stdin=None):
            nonlocal record_previous
            env = dict(f.env)
            if model:
                env["CODEX_THREAD_ID"] = f.parent
            before_records = None
            if record_previous is not None:
                before_records = execution_record_bytes(f.job_directory(ident))
                validate_execution_record_transition(record_previous, before_records)
            result = subprocess.run([*entrypoints[label], *map(str, values)], env=env,
                cwd=f.work, input=stdin, capture_output=True, text=True, timeout=20)
            record = dict(package=label, entrypoint=entrypoints[label], args=values, model=model, returncode=result.returncode,
                          stdout=result.stdout, stderr=result.stderr)
            report.setdefault("helper_calls", []).append(record)
            if before_records is not None:
                after_records = execution_record_bytes(f.job_directory(ident))
                transition = validate_execution_record_transition(before_records, after_records, values)
                report.setdefault("execution_record_calls", []).append({"package": label,
                    "arguments": list(map(str, values)), **transition,
                    "before_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in before_records.items()},
                    "after_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in after_records.items()},
                    "between_call_bytes_unchanged": True})
                record_previous = after_records
            assert result.returncode == 0, record
            return json.loads(result.stdout)

        def mode(label, action="status", *values, model=False, stdin=None):
            return cli(label, "ultracode", action, "--thread", f.parent, *values, "--json",
                       model=model, stdin=stdin)

        def goal(label, action="status", *values):
            return cli(label, "goal", action, "--thread", f.parent, *values, "--json")

        before = f.provider.count
        mode("new", "provenance", "--load-kind", "fresh", "--developer-instructions-stdin", stdin=instructions)
        mode("new", "on")
        assert f.provider.count == before, "mode qualification called provider"
        completion(f)
        execution = f.report["verified_results"][-1]
        ident = execution["id"]
        result_file = f.job_directory(ident) / "result.json"
        result_sha = digest(result_file)
        report["execution"] = {"id": ident, "result_sha256": result_sha, "record": execution}
        stop_rust_observers(f.home)
        wait(lambda: not f.coordinator_processes(), "private observers did not stop")
        assert f.delivered(), "supervisor must remain free until completion is delivered"
        # Completion must deliver before this fixture freezes observer admission.
        f.supervisor_lock = (f.home / "codex-plus" / f"{f.parent}.supervisor.lock").open("w")
        fcntl.flock(f.supervisor_lock, fcntl.LOCK_EX)
        report["quiet_lock_captured_after_completion"] = True
        check("0.4.3 actual native prepare/launch, durable execution result, one side effect, delivered idle callback")

        before = f.provider.count
        goal("new", "provenance", "--cwd", f.work, "--load-kind", "fresh")
        paused = goal("new", "start", "--objective", "Preserve 0.4.3 rows across quiet rollback", "--paused")
        assert paused["goal"]["state"] == "paused", paused
        assert f.provider.count == before, "qualification/paused goal called provider"
        new_schema = ledger_snapshot(f.home)["schema_sha256"]
        report["new_generated_schema_sha256"] = new_schema

        for reserver, settler in (("new", "old"), ("old", "new")):
            mode(reserver, "on")
            submission = str(uuid.uuid4())
            goal(reserver, "input", "--submission-id", submission)
            gate = f.provider.gate()
            f.provider.root_steps.append((message("mixed helper admitted turn"), gate))
            count = f.provider.count
            turn = f.rpc.call("turn/start", {"threadId": f.parent,
                "clientUserMessageId": submission,
                "input": [{"type": "text", "text": "Private mixed-version accepted input"}]})["turn"]["id"]
            wait(lambda: f.provider.count == count + 1, "mixed-version turn did not reach loopback provider")
            count = f.provider.count
            reserved_status = mode(reserver, model=True)
            expected = reserved_status["effective"]
            assert expected and expected["enabled"] is True and expected["submission"] == submission
            assert expected["turn"] == turn and expected["generation"] == f.rpc.generation
            assert expected["policy_version"] == 1 and reserved_status["instructions"] == policies[reserver]
            settled_status = mode(settler, model=True)
            assert settled_status["effective"] == expected
            assert settled_status["instructions"] == policies[reserver]
            goal(settler, "input-settle", "--submission-id", submission, "--accepted", "--turn", turn)
            mode(settler, "off")
            steer = str(uuid.uuid4())
            goal(settler, "input", "--submission-id", steer)
            f.rpc.call("turn/steer", {"threadId": f.parent, "expectedTurnId": turn,
                "clientUserMessageId": steer,
                "input": [{"type": "text", "text": "Steer preserves this admitted snapshot"}]})
            goal(reserver, "input-settle", "--submission-id", steer, "--accepted", "--turn", turn)
            for label in ("old", "new", "old", "new"):
                status = mode(label, model=True)
                assert status["effective"] == expected, status
                assert status["instructions"] == policies[reserver], "reader/steering rewrote admitted policy text"
                assert not status["requested"]["enabled"]
                assert all(row["submission"] not in (submission, steer) for row in status["pending"])
            assert f.provider.count == count, "helper control/read called provider"
            gate.set()
            assert f.terminal(turn)["status"] == "completed"
            wait(lambda: f.thread()["status"]["type"] in ("idle", "notLoaded"), "mixed turn did not settle")
            report.setdefault("turn_snapshots", []).append({"reserver": reserver, "settler": settler,
                "effective": expected, "steer_submission": steer,
                "policy_text_sha256": hashlib.sha256(policies[reserver].encode()).hexdigest(),
                "policy_handoff_threshold_minutes": 16 if reserver == "new" else 8,
                "provider_requests_from_reads_and_controls": 0})
            assert ledger_snapshot(f.home)["schema_sha256"] == new_schema
        report["mixed_entrypoints"] = {"old_tui_adjacent_helper": entrypoints["old"],
                                       "new_cli": entrypoints["new"]}
        check(f"{args.expected_old_version} TUI-adjacent helper and 0.4.3 CLI bidirectional accepted input/steering preserve exact admitted snapshots")

        def maintenance(label, action):
            return cli(label, "job", "maintenance", action)

        record_previous = execution_record_bytes(f.job_directory(ident))
        validate_execution_record_transition(record_previous, record_previous)
        report["execution_record_contract"] = {"schema": 1,
            "allowance": "only finite monotonic top-level state.updated_at during exact maintenance enter/status",
            "before_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in record_previous.items()}}
        for name, raw in record_previous.items():
            target = evidence / "execution-record-baseline" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

        # Reconcile settled accepted inputs before taking the quiet rollback baseline.
        normalized = maintenance("new", "enter")
        assert normalized["held"] and normalized["admission_quiet"] and normalized["execution_quiet"], normalized
        maintenance("new", "leave")
        assert not f.coordinator_processes()
        baseline = ledger_snapshot(f.home)
        execution_records = {str(path.relative_to(f.job_directory(ident))): digest(path)
                             for path in f.job_directory(ident).rglob("*")
                             if path.is_file() and path.suffix == ".json"}
        assert {"manifest.json", "state.json", "result.json", "observation.json"} <= set(execution_records), execution_records
        report["execution_record_sha256"] = execution_records
        assert baseline["schema_sha256"] == new_schema
        write(evidence / "ledger-before-rollback.json", baseline)
        assert baseline["tables"]["jobs"] and baseline["tables"]["callbacks"]
        assert baseline["tables"]["ultracode_turns"] and baseline["tables"]["ultracode_submissions"]
        assert baseline["tables"]["goals"]
        count = f.provider.count
        for label in ("old", "new", "old"):
            park = cli(label, "park", "--status", "--parent", f.parent)
            jobs = cli(label, "job", "list", "--parent", f.parent)
            status = cli(label, "job", "status", "--parent", f.parent, "--id", ident)
            assert status["id"] == ident and status["result"] == execution["result"], status
            assert any(row["id"] == ident for row in jobs), jobs
            assert any(row["execution"] == ident for row in park["jobs"]), park
            assert all(row["state"] == "delivered" for row in park["callbacks"]), park
            assert goal(label)["goal"]["id"] == paused["goal"]["id"]
            goal(label, "history")
            assert mode(label)["effective"] is None
            snapshot = ledger_snapshot(f.home)
            write(evidence / f"ledger-after-{label}-{len(report.get('read_passes', []))}.json", snapshot)
            assert snapshot == baseline, f"{label} read changed settled ledger rows/schema"
            assert digest(result_file) == result_sha
            report.setdefault("read_passes", []).append({"package": label,
                "schema_sha256": snapshot["schema_sha256"], "rows_unchanged": True})
        check(f"quiet {args.expected_old_version} reads 0.4.3 park/job/result/goal/Ultracode data with unchanged schema and every row")

        holds = []
        for entering, leaving in (("old", "new"), ("new", "old")):
            entered = maintenance(entering, "enter")
            assert entered["held"] and entered["admission_quiet"] and entered["execution_quiet"], entered
            observed = maintenance(leaving, "status")
            assert observed["id"] == entered["id"] and observed["held"], observed
            assert observed["admission_quiet"] and observed["execution_quiet"], observed
            left = maintenance(leaving, "leave")
            assert left["id"] == entered["id"] and left["held"] is False, left
            reread = maintenance(entering, "status")
            assert reread["id"] == entered["id"] and reread["held"] is False
            holds.append({"entering": entering, "leaving": leaving, "entered": entered,
                          "observed": observed, "left": left, "reread": reread})
        assert holds[0]["entered"]["id"] != holds[1]["entered"]["id"]
        report["holds"] = holds
        final = ledger_snapshot(f.home)
        write(evidence / "ledger-after-holds.json", final)
        assert final == baseline, "cross-version hold changed settled ledger rows/schema"
        assert digest(result_file) == result_sha
        final_records = execution_record_bytes(f.job_directory(ident))
        validate_execution_record_transition(record_previous, final_records)
        report["execution_record_contract"].update({
            "after_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in final_records.items()},
            "file_set_unchanged": True, "immutable_json_bytes_unchanged": True,
            "state_other_bytes_unchanged": True, "read_and_leave_bytes_unchanged": True,
            "between_call_bytes_unchanged": True,
            "timestamp_refreshes": sum(call["timestamp_refreshed"] for call in report["execution_record_calls"])})
        assert f.provider.count == count, "quiet reads or holds called provider"
        assert (f.work / "job/runs").read_text() == "run\n", "rollback replayed workload"
        f.quiet(.4)
        report["quiet_rollback"] = {"admission_quiet": True, "execution_quiet": True,
            "observer_processes": f.coordinator_processes(), "row_identities_unchanged": True,
            "schema_unchanged": True, "schema_sha256": final["schema_sha256"],
            "result_unchanged": True, "workload_runs": 1, "provider_requests_from_reads_and_holds": 0}
        assert not report["quiet_rollback"]["observer_processes"]
        check("bidirectional maintenance hold identity/leave, schema/rows/result preserved and no replay/provider calls")
        assert digest(binary) == stock_sha
        for label, package, version, commit, manifest_sha256 in qualification:
            assert package_identity(Path(identities[label]["path"]), version, commit, manifest_sha256) == identities[label]
        assert source_identity(source, args.source_commit) == source_commit
        assert {name: digest(source / name) for name in fixture_files} == report["source"]["files"]
        check("both qualified package inputs and official binary remain unchanged")
        report["passed"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        signal.alarm(0)
        if fixture is not None:
            report["loopback_provider_requests"] = fixture.provider.count
            try:
                fixture.close(report["passed"])
                end = time.monotonic() + 5
                while owned_processes(fixture.home) and time.monotonic() < end:
                    time.sleep(.05)
                survivors = owned_processes(fixture.home)
                if survivors:
                    # The exact private CODEX_HOME marker is this fixture's ownership boundary.
                    for pid in survivors:
                        try:
                            descriptor = os.pidfd_open(pid)
                            try:
                                if pid in owned_processes(fixture.home):
                                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                            finally:
                                os.close(descriptor)
                        except ProcessLookupError:
                            pass
                    end = time.monotonic() + 3
                    while owned_processes(fixture.home) and time.monotonic() < end:
                        time.sleep(.05)
                report["owned_survivors_after_cleanup"] = owned_processes(fixture.home)
                assert not report["owned_survivors_after_cleanup"], report["owned_survivors_after_cleanup"]
                report["owned_fixture_closed"] = True
                if record_previous is not None:
                    captured = evidence / "fixture/executions/v1" / fixture.parent / ident
                    validate_execution_record_transition(record_previous, execution_record_bytes(captured))
                    report["execution_record_contract"]["cleanup_capture_bytes_unchanged"] = True
            except BaseException:
                report["passed"] = False
                report["cleanup_error"] = traceback.format_exc()
        report["rollback_qualified"] = report["passed"] and args.expected_old_version == "0.4.2"
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write(evidence / "gate.json", report)
    print(json.dumps({"passed": report["passed"], "checks": report["checks"],
                      "evidence": str(evidence / "gate.json"), "error": report.get("error"),
                      "cleanup_error": report.get("cleanup_error")}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", "--old-package", dest="old_package", type=Path, required=True)
    parser.add_argument("--new", "--new-package", dest="new_package", type=Path, required=True)
    for flag in ("stock-binary", "source", "evidence"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--new-manifest-sha256", required=True)
    parser.add_argument("--gate-sha256", required=True)
    parser.add_argument("--expected-old-version", choices=tuple(OLD_IDENTITIES), default="0.4.2")
    parser.add_argument("--expected-new-version", choices=("0.4.3",), default="0.4.3")
    raise SystemExit(run(parser.parse_args()))
