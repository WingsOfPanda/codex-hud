"""Private cooperative upgrades against an unchanged official app-server daemon.

COOPERATIVE_OFFICIAL_DAEMON=/absolute/official/codex \
COOPERATIVE_EVIDENCE=.build/cooperative-upgrade \
python3 tests/test_cooperative_upgrade_integration.py

A/B/C are sealed copies of the same helper implementation with distinct source
markers and fixture-only gates. This proves coordinator protocol behavior, not a
native release build or compatibility with pre-protocol installed helpers.
"""

import hashlib
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest

from test_background_integration import REPO, RecordingClient, coordinator_state, stop, stop_observers, wait
from codex_plus.background import Store, register
from codex_plus.rpc import Client
from codex_plus.upgrade import package_command
from fixtures.cooperative_upgrade import UpgradeProvider, install


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_package(root, label):
    """Seal the actual code, including the instrumentation that executes."""
    package = root / label
    helpers = package / "codex_plus"
    helpers.mkdir(parents=True)
    for source in (REPO / "codex_plus").glob("*.py"):
        shutil.copy2(source, helpers / source.name)
    background = helpers / "background.py"
    text = background.read_text()
    hook = (f"UPGRADE_FIXTURE_LABEL = {label!r}\n"
            "if __name__ == '__main__':\n"
            "    from .upgrade_fixture import install\n"
            "    install(sys.modules[__name__], UPGRADE_FIXTURE_LABEL)\n\n")
    marker = "if __name__ == '__main__':"
    assert text.count(marker) == 1, "fixture must instrument the actual background entry point"
    background.write_text(text.replace(marker, hook + marker))
    (helpers / "upgrade_fixture.py").write_text(
        "import hashlib, json, os, sys, time\nfrom pathlib import Path\n\n" + inspect.getsource(install))
    files = {}
    for path in sorted(package.rglob("*")):
        if path.is_file():
            path.chmod(0o644)
            files[path.relative_to(package).as_posix()] = {
                "sha256": digest(path), "mode": "0644", "size": path.stat().st_size}
    manifest = {"schema_version": 1, "coordinator_protocol": 1,
                "release": f"0.162.1-plus.0.1.{ord(label) - ord('A')}", "plus_commit": label.lower() * 40,
                "upstream": {"version": "0.162.1", "tag": "private-stock-proof", "commit": "0" * 40},
                "target": "x86_64-unknown-linux-gnu", "files": files,
                "tree_sha256": hashlib.sha256(json.dumps(files, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()}
    (package / "codex-plus.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    for path in package.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    package.chmod(0o555)
    return package


class PrivateUpgrade:
    def __init__(self, test, mode, binary, evidence):
        self.test, self.mode, self.binary = test, mode, binary
        self.case = evidence / mode
        self.case.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temporary = tempfile.TemporaryDirectory(prefix="cu-")
        self.root = Path(self.temporary.name)
        self.home, self.workspace = self.root / "home", self.root / "work"
        self.home.mkdir(mode=0o700)
        self.workspace.mkdir()
        self.trace = self.root / "trace"
        self.trace.mkdir()
        self.packages = {label: make_package(self.root / "packages", label) for label in "ABC"}
        self.current = self.root / "packages/current"
        self.flip("A")
        self.releases = [self.workspace / f"release-{index}" for index in range(2)]
        self.runs = [self.workspace / f"runs-{index}" for index in range(2)]
        commands = [f"echo run >> {shlex.quote(str(runs))}; "
                    f"deadline=$((SECONDS+100)); while [ ! -e {shlex.quote(str(release))} ] && "
                    f"(( SECONDS < deadline )); do sleep .05; done; "
                    f"test -e {shlex.quote(str(release))} || exit 124; echo upgrade-complete-{index}"
                    for index, (release, runs) in enumerate(zip(self.releases, self.runs))]
        self.provider = UpgradeProvider(commands)
        self.env = {key: value for key, value in os.environ.items()
                    if not any(secret in key.upper() for secret in ("API_KEY", "AUTH_TOKEN", "BASE_URL"))}
        self.env.update(CODEX_HOME=str(self.home), PYTHONPATH=str(self.packages["A"]),
                        PYTHONDONTWRITEBYTECODE="1", RUST_LOG="warn", UPGRADE_TRACE=str(self.trace))
        (self.home / "config.toml").write_text(f"""model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
[model_providers.isolated]
name="loopback cooperative upgrade acceptance"
base_url="{self.provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
""")
        self.native = self.rpc = None
        self.helpers = []
        self.report = {"scenario": mode, "official_daemon": str(binary), "remote_model_requests": 0,
                       "package_kind": "same-code sealed source fixtures"}

    def flip(self, label):
        temporary = self.current.with_name(".next")
        temporary.symlink_to(self.packages[label].name)
        os.replace(temporary, self.current)

    def events(self):
        path = self.trace / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def seen(self, kind, package=None, **values):
        return [row for row in self.events() if row["kind"] == kind
                and (package is None or row["package"] == package)
                and all(row.get(key) == value for key, value in values.items())]

    def state(self):
        return coordinator_state(self.home)

    def native_identity(self):
        proc = Path(f"/proc/{self.native.pid}")
        status = self.binary.stat()
        client = Client(self.home / "app-server-control/app-server-control.sock")
        try:
            return {"pid": self.native.pid,
                    "start_ticks": (proc / "stat").read_text().rsplit(")", 1)[1].split()[19],
                    "generation": client.generation, "executable": str((proc / "exe").resolve()),
                    "binary_stat": [status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns]}
        finally:
            client.close()

    def start(self, point="cycle", crash=False, seed_history=True):
        with (self.case / "native.log").open("w") as log:
            self.native = subprocess.Popen([str(self.binary), "app-server", "--listen", "unix://"],
                env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        socket_path = self.home / "app-server-control/app-server-control.sock"
        wait(lambda: socket_path.exists(), "official daemon did not start")
        self.rpc = RecordingClient(socket_path)
        self.test.assertIsNot(self.rpc.initialize_result.get("queueIdempotency"), True)
        self.before_native = self.native_identity()
        self.parent = self.rpc.call("thread/start", {"model": "gpt-6.1-sol", "cwd": str(self.workspace),
            "approvalPolicy": "never", "sandbox": "danger-full-access"})["thread"]["id"]
        self.origin = self.rpc.call("turn/start", {"threadId": self.parent,
            "input": [{"type": "text", "text": "Run both finite commands, then park.", "text_elements": []}],
            "effort": "high"})["turn"]["id"]
        wait(lambda: self.provider.count == 2, "native commands did not yield")
        terminals = self.rpc.call("thread/backgroundTerminals/list", {"threadId": self.parent})["data"]
        self.test.assertEqual(len(terminals), 2, terminals)
        self.processes = [row["processId"] for row in terminals]
        store = Store(self.home)
        try:
            self.registration = register(store, self.rpc, self.parent, [], self.processes, "private cooperative upgrade")
        finally:
            store.close()
        if seed_history:
            with sqlite3.connect(self.home / "codex-plus/background.sqlite") as db:
                db.execute("INSERT INTO parks (id,parent,origin,reason,state,deadline) VALUES (?,?,?,?,?,?)",
                           ("cancel-park", self.parent, "cancel-origin", "private cancelled", "cancelled", time.time()+1000))
                db.execute("INSERT INTO jobs (id,park,kind,thread,item,process,status) VALUES (?,?,?,?,?,?,?)",
                           ("cancel-job", "cancel-park", "process", self.parent, "cancel-item", "cancel-process", "cancelled"))
                db.execute("INSERT INTO callbacks (id,parent,client,state,message,idempotent) VALUES (?,?,?,?,?,?)",
                           ("frozen-history", self.parent, "historic-client", "uncertain", "original frozen historical payload", 0))
        self.baseline = self.state()
        self.env.update(UPGRADE_POINT=point, UPGRADE_CRASH="1" if crash else "0")
        self.spawn("A", "supervise")
        self.provider.final_gate.set()
        wait(lambda: self.rpc.call("thread/read", {"threadId": self.parent, "includeTurns": False})["thread"]["status"]["type"] == "idle", "initial parent did not become idle")
        if point == "cycle":
            wait(lambda: self.seen("gate", "A"), "old observer did not reach cycle gate")
        self.report.update(parent=self.parent, origin=self.origin, registered_processes=self.processes)

    def spawn(self, package, action):
        with (self.case / f"{package}-{action}-{len(self.helpers)}.log").open("w") as log:
            process = subprocess.Popen(package_command(self.packages[package], action, self.parent),
                cwd=self.packages[package],
                env=dict(self.env, PYTHONPATH=str(self.packages[package])), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True)
        self.helpers.append(process)
        return process

    def release_gate(self):
        (self.trace / "release-gate").touch()

    def assert_no_takeover(self):
        # The held cycle still owns its lock while new supervisors contend.
        time.sleep(.15)
        self.test.assertFalse(self.seen("store_open", "B"))
        self.test.assertFalse(self.seen("store_open", "C"))
        self.test.assertIsNone(self.native.poll())

    def takeover(self, package="B", require_running=True):
        wait(lambda: self.seen("store_open", package, action="observe"), f"{package} observer never opened the ledger", timeout=25)
        if require_running:
            wait(lambda: self.seen("running", package), f"{package} did not reconfirm the real running command")
        self.test.assertNotEqual(self.seen("observer", "A")[0]["run"], self.seen("observer", package)[0]["run"])
        old_supervisor = self.seen("boot", "A", action="supervise")[0]["pid"]
        self.test.assertTrue(any(row["pid"] == old_supervisor
                                 for row in self.seen("boot", package, action="supervise")),
                             "supervisor did not reexec the selected package")
        if self.env.get("UPGRADE_CRASH") != "1":
            self.test.assertTrue(self.seen("observer_exit", "A", result=75), "old observer did not yield cooperatively")

    def callbacks(self):
        return [row for row in self.state()["callbacks"] if row["id"] != "frozen-history"]

    def receipts(self, callback):
        return [entry["item"] for entry in self.rpc.call("thread/items/list", {"threadId": self.parent})["data"]
                if entry.get("item", {}).get("type") == "userMessage"
                and entry["item"].get("clientId") == callback["client"]]

    def assert_delivered(self, count):
        wait(lambda: len([row for row in self.callbacks() if row["state"] == "delivered"]) == count,
             f"expected {count} delivered callbacks", timeout=25)
        rows = self.callbacks()
        self.test.assertEqual(len(rows), count, rows)
        for callback in rows:
            receipts = self.receipts(callback)
            self.test.assertEqual(len(receipts), 1, receipts)
            self.test.assertEqual(callback["message"], "\n".join(
                part["text"] for part in receipts[0]["content"] if part["type"] == "text"))
        return rows

    def frozen(self):
        callback = self.callbacks()[0]
        return {key: callback[key] for key in ("id", "client", "message", "idempotent")}

    def finish(self):
        self.test.assertIsNone(self.native.poll(), "upgrade stopped the official daemon")
        self.test.assertEqual(self.native_identity(), self.before_native)
        state = self.state()
        baseline_jobs = {row["id"]: row for row in self.baseline["jobs"]}
        self.test.assertEqual(set(baseline_jobs), {row["id"] for row in state["jobs"]})
        for job in state["jobs"]:
            old = baseline_jobs[job["id"]]
            for key in ("id", "park", "kind", "thread", "item", "process"):
                self.test.assertEqual(job[key], old[key], key)
            if old["status"] == "cancelled":
                self.test.assertEqual(job, old)
        for table, ident in (("callbacks", "frozen-history"), ("parks", "cancel-park")):
            before = next(row for row in self.baseline[table] if row["id"] == ident)
            after = next(row for row in state[table] if row["id"] == ident)
            keys = ("id", "parent", "client", "state", "queue", "message", "idempotent") if table == "callbacks" else before
            self.test.assertEqual({key: after[key] for key in keys}, {key: before[key] for key in keys})
        for path in self.runs:
            self.test.assertEqual(path.read_text().splitlines(), ["run"], "upgrade replayed a native command")
        for boot in self.seen("boot"):
            self.test.assertEqual(boot["source_sha256"],
                                  digest(self.packages[boot["package"]] / "codex_plus/background.py"))
        self.report.update(passed=True, daemon_before=self.before_native, daemon_after=self.native_identity(),
                           provider_requests=self.provider.count, ledger=state,
                           package_source_sha256={label: digest(path / "codex_plus/background.py")
                               for label, path in self.packages.items()})

    def close(self):
        if getattr(self, "closed", False):
            return
        self.closed = True
        self.provider.final_gate.set()
        self.release_gate()
        for release in self.releases:
            release.touch()
        stop_observers(self.home)
        for helper in self.helpers:
            stop(helper)
        if self.rpc:
            self.rpc.close()
        if self.native:
            stop(self.native)
        self.provider.close()
        self.report.setdefault("passed", False)
        if (self.home / "codex-plus/background.sqlite").exists():
            self.report.setdefault("ledger", self.state())
        self.report.setdefault("provider_requests", self.provider.count)
        if (self.trace / "events.jsonl").exists():
            shutil.copy2(self.trace / "events.jsonl", self.case / "events.jsonl")
        (self.case / "summary.json").write_text(json.dumps(self.report, indent=2) + "\n")
        # Read-only fixture packages are deliberately immutable while running.
        for path in (self.root / "packages").rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        self.temporary.cleanup()


class PackageFixture(unittest.TestCase):
    def test_distinct_markers_and_instrumentation_are_sealed(self):
        from codex_plus.upgrade import select_package

        with tempfile.TemporaryDirectory(prefix="upgrade-packages-") as temporary:
            root = Path(temporary)
            try:
                packages = {label: make_package(root, label) for label in "ABC"}
                self.assertEqual(len({digest(path / "codex_plus/background.py")
                                      for path in packages.values()}), 3)
                (root / "current").symlink_to("B")
                self.assertEqual(select_package(packages["A"]), packages["B"])
                manifest = json.loads((packages["B"] / "codex-plus.json").read_text())
                self.assertIn("codex_plus/upgrade_fixture.py", manifest["files"])
                (root / "current").unlink()
                (root / "current").symlink_to("C")
                self.assertEqual(select_package(packages["A"]), packages["C"])
            finally:
                for path in root.rglob("*"):
                    if path.is_dir() and not path.is_symlink():
                        path.chmod(0o755)


class CooperativeUpgrade(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(os.environ.get(key) for key in ("COOPERATIVE_OFFICIAL_DAEMON", "COOPERATIVE_EVIDENCE")):
            raise unittest.SkipTest("real-stock proof requires explicit COOPERATIVE_OFFICIAL_DAEMON and COOPERATIVE_EVIDENCE")
        cls.binary = Path(os.environ["COOPERATIVE_OFFICIAL_DAEMON"]).resolve(strict=True)
        cls.evidence = Path(os.environ["COOPERATIVE_EVIDENCE"]).resolve()
        cls.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        (cls.evidence / "daemon-sha256.json").write_text(json.dumps({"binary": str(cls.binary),
            "sha256": digest(cls.binary)}) + "\n")

    def case(self, mode):
        case = PrivateUpgrade(self, mode, self.binary, self.evidence)
        self.addCleanup(case.close)
        return case

    def test_running_upgrade_reconfirms_live_without_replay(self):
        case = self.case("running")
        case.start()
        case.flip("B")
        case.assert_no_takeover()
        case.release_gate()
        case.takeover()
        terminals = case.rpc.call("thread/backgroundTerminals/list", {"threadId": case.parent})["data"]
        self.assertEqual({row["processId"] for row in terminals}, set(case.processes))
        self.assertEqual(case.provider.count, 2, "upgrade invoked a model")
        case.releases[0].touch()
        self.assertIn("LIVE COMPLETION", case.assert_delivered(1)[0]["message"])
        case.releases[1].touch()
        case.assert_delivered(2)
        self.assertEqual(case.provider.count, 4)
        case.finish()

    def test_completed_during_handoff_gap_is_report_only(self):
        case = self.case("completed-gap")
        case.start()
        case.releases[0].touch()
        wait(lambda: len(case.rpc.call("thread/backgroundTerminals/list", {"threadId": case.parent})["data"]) == 1,
             "first command did not complete in old observer gap")
        case.flip("B")
        case.assert_no_takeover()
        case.release_gate()
        case.takeover()
        self.assertIn("REPORT ONLY", case.assert_delivered(1)[0]["message"])
        case.releases[1].touch()
        rows = case.assert_delivered(2)
        self.assertEqual(sum("LIVE COMPLETION" in row["message"] for row in rows), 1)
        case.finish()

    def test_callback_boundaries_finish_owned_request_before_upgrade(self):
        for point in ("before_enqueue", "after_enqueue", "after_start"):
            with self.subTest(point=point):
                case = self.case(point)
                try:
                    case.start(point)
                    wait(lambda: case.seen("running", "A"), "old observer did not verify running jobs")
                    case.releases[0].touch()
                    wait(lambda: case.seen("gate", "A"), f"old observer never reached {point}")
                    if point == "after_start":
                        consumed = case.seen("native_consumed", "A")
                        explicit = case.seen("explicit_start_ack", "A")
                        self.assertTrue(consumed or explicit, "no real native consumption/start evidence")
                        case.report["start_checkpoint"] = "native-consumed" if consumed else "explicit-start-ack"
                    frozen = case.frozen()
                    case.flip("B")
                    case.assert_no_takeover()
                    case.release_gate()
                    case.takeover()
                    case.assert_delivered(1)
                    self.assertEqual(case.frozen(), frozen)
                    case.releases[1].touch()
                    case.assert_delivered(2)
                    self.assertEqual(case.provider.count, 4)
                    case.finish()
                finally:
                    case.close()

    def test_rapid_selection_and_old_supervisor_contender_skip_stale_migration(self):
        case = self.case("rapid-A-B-C")
        case.start()
        case.flip("B")
        contender = case.spawn("A", "supervise")
        wait(lambda: len(case.seen("boot", "A", action="supervise")) == 2, "old contender did not start")
        case.flip("C")
        case.assert_no_takeover()
        case.release_gate()
        case.takeover("C")
        self.assertFalse(case.seen("store_open", "B"), "stale B migrated the ledger")
        case.releases[0].touch()
        case.assert_delivered(1)
        case.releases[1].touch()
        case.assert_delivered(2)
        wait(lambda: contender.poll() is not None, "old contender did not exit after final selected work", timeout=25)
        self.assertEqual(case.provider.count, 4)
        case.finish()

    def test_crash_after_enqueue_reconciles_one_real_callback(self):
        case = self.case("crash-after-enqueue")
        case.start("after_enqueue", crash=True)
        wait(lambda: case.seen("running", "A"), "old running observation")
        case.releases[0].touch()
        wait(lambda: case.seen("gate", "A"), "enqueue acknowledgement gate")
        frozen = case.frozen()
        case.flip("B")
        case.assert_no_takeover()
        case.release_gate()
        case.takeover()
        case.assert_delivered(1)
        self.assertEqual(case.frozen(), frozen)
        self.assertEqual(len(case.seen("crash", "A")), 1)
        case.releases[1].touch()
        case.assert_delivered(2)
        self.assertEqual(case.provider.count, 4)
        case.finish()

    def test_crash_before_enqueue_freezes_uncertainty_without_resend(self):
        case = self.case("crash-before-enqueue")
        case.start("before_enqueue", crash=True)
        wait(lambda: case.seen("running", "A"), "old running observation")
        case.releases[0].touch()
        wait(lambda: case.seen("gate", "A"), "pre-enqueue gate")
        frozen = case.frozen()
        case.flip("B")
        case.assert_no_takeover()
        case.release_gate()
        case.takeover()
        wait(lambda: case.callbacks()[0]["state"] == "uncertain", "unsent callback was not conservatively frozen")
        self.assertEqual(case.frozen(), frozen)
        self.assertFalse(case.receipts(case.callbacks()[0]))
        self.assertFalse(case.rpc.call("thread/queue/list", {"threadId": case.parent})["data"])
        case.releases[1].touch()
        wait(lambda: all(row["status"] in ("completed", "cancelled") for row in case.state()["jobs"]),
             "new observer did not record remaining real completion")
        wait(lambda: len([row for row in case.callbacks() if row["state"] == "delivered"]) == 1,
             "remaining real completion did not deliver its fresh callback")
        self.assertEqual(case.provider.count, 3, "uncertain callback was resent")
        self.assertEqual(case.callbacks()[0]["state"], "uncertain")
        self.assertFalse(case.receipts(case.callbacks()[0]), "uncertain frozen client reached native history")
        fresh = next(row for row in case.callbacks() if row["state"] == "delivered")
        self.assertEqual(len(case.receipts(fresh)), 1)
        self.assertEqual(case.frozen(), frozen)
        case.finish()

    def test_clean_completed_cycle_may_exit_without_new_observer(self):
        case = self.case("clean-complete")
        case.start("completed_cycle")
        wait(lambda: case.seen("running", "A"), "old running observation")
        for release in case.releases:
            release.touch()
        wait(lambda: case.seen("gate", "A"), "completed cycle did not settle")
        case.flip("B")
        case.assert_no_takeover()
        case.release_gate()
        # Same-cycle completion is a valid zero-work exit, even after current flips.
        wait(lambda: case.helpers[0].poll() is not None, "completed supervisor did not exit", timeout=25)
        self.assertEqual(case.helpers[0].returncode, 0)
        callbacks = case.callbacks()
        self.assertTrue(callbacks and all(row["state"] == "delivered" for row in callbacks))
        for callback in callbacks:
            self.assertEqual(len(case.receipts(callback)), 1)
        self.assertLessEqual(case.provider.count, 4)
        case.finish()


if __name__ == "__main__":
    unittest.main()
