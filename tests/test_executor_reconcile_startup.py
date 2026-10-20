"""Real stock-native launch must not race its live executor's startup lock.

Run with --runtime PATH --stock-binary PATH --evidence NEW_DIRECTORY. The private
syscall gate preserves real flock results, widens the old overlap, and releases
the child promptly when corrected reconciliation omits that probe.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures import detached_jobs
from test_background_integration import wait


def case(runtime, stock, evidence, library, name, mode):
    directory = evidence / name
    directory.mkdir(mode=0o700)
    errors, observed = [], {}

    def release_startup():
        try:
            wait(lambda: (directory / "executor-ready").exists(),
                 "executor did not reach its first lock acquisition", timeout=12)
            # Fixed reconciliation returns without probing. Observe its actual
            # job.lock release, rather than inferring absence from a sleep.
            wait(lambda: (directory / "status-ready").exists() or
                 (directory / "reconcile-completed").exists(),
                 "launcher neither probed nor completed reconciliation", timeout=8)
            observed["live_executor_lock_probed"] = (directory / "status-ready").exists()
            if observed["live_executor_lock_probed"]:
                if mode == "overlap":
                    wait(lambda: (directory / "executor-blocked").exists(),
                         "old startup lock contention did not occur", timeout=5)
                (directory / "status-release").touch()
                wait(lambda: (directory / "launch-job-unlocked").exists(),
                     "reconciliation did not release job.lock", timeout=5)
            (directory / ("executor-release" if mode == "overlap" and
                observed["live_executor_lock_probed"] else "executor-go")).touch()
        except BaseException:
            errors.append(traceback.format_exc())

    original_driver = detached_jobs.NATIVE_DRIVER
    environment = dict(LD_PRELOAD=str(library), DIAG_ROLE="launcher",
                       DIAG_GATE=str(directory), DIAG_MODE=mode)
    injection = "import os;os.environ.update(" + repr(environment) + ");"
    detached_jobs.NATIVE_DRIVER = original_driver.replace(
        "responses=[]; prepared={}", injection + "responses=[]; prepared={}")
    fixture = detached_jobs.DetachedJobServer(runtime, stock, directory / "fixture")
    gate = threading.Thread(target=release_startup)
    gate.start()
    passed = False
    try:
        manifest, work = fixture.manifest()
        command = fixture.commands([
            ["job", "prepare", "--manifest", str(manifest), "--turn", "{turn}"],
            ["job", "launch", "--id", "{id}", "--review-digest", "{digest}",
             "--reason", "Review private executor startup regression"]])
        gate.join(timeout=15)
        assert not gate.is_alive() and not errors, errors
        responses = fixture.responses(command)
        assert len(responses) == 2 and all(row["returncode"] == 0 for row in responses), responses
        ident = responses[0]["output"]["id"]
        current = fixture.job(ident)
        if current["state"] not in ("unknown", "failed"):
            current = wait(lambda: (row := fixture.job(ident))["state"] == "running" and row,
                           "executor did not become running", timeout=8)
            assert current["workload"] is not None, current
            (work / "release").touch()
            current = wait(lambda: (row := fixture.job(ident))["result"] is not None and row,
                           "executor did not publish its result", timeout=8)
        fixture.finish(command)
        result = dict(name=name, mode=mode, root=fixture.parent, execution=ident,
            state=current["state"], error=current["error"], workload=current["workload"],
            processes=current["processes"], result=current["result"],
            prepare_count=1, launch_count=1, **observed)
        result["passed"] = (not result["live_executor_lock_probed"] and
            result["state"] == "completed" and result["result"]["exit_code"] == 0 and
            (work / "runs").read_text() == "run\n")
        passed = result["passed"]
        return result
    finally:
        for marker in ("status-release", "executor-release", "executor-go"):
            (directory / marker).touch(exist_ok=True)
        gate.join(timeout=15)
        fixture.close(passed)
        detached_jobs.NATIVE_DRIVER = original_driver


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--stock-binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    runtime, stock = args.runtime.resolve(strict=True), args.stock_binary.resolve(strict=True)
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700)
    library = evidence / "executor_reconcile_gate.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O2",
        str(Path(__file__).parent / "fixtures/executor_reconcile_gate.c"),
        "-o", str(library), "-ldl"], check=True, timeout=30)
    report = dict(passed=False, architecture=os.uname().machine, cases=[],
        runtime_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
        stock_sha256=hashlib.sha256(stock.read_bytes()).hexdigest(), remote_model_requests=0)
    original_temporary = tempfile.TemporaryDirectory
    tempfile.TemporaryDirectory = lambda *a, **kw: original_temporary(**(kw | {"prefix": ""}))
    try:
        for name, mode in (("overlap-one", "overlap"), ("serial-control", "serial"),
                           ("overlap-two", "overlap")):
            report["cases"].append(case(runtime, stock, evidence, library, name, mode))
        report["passed"] = all(row["passed"] for row in report["cases"])
    finally:
        tempfile.TemporaryDirectory = original_temporary
        (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
