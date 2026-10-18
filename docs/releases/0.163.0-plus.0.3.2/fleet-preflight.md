# 0.3.2 fleet preflight answers

Checked against source freeze `103f92b0712ac63bb4a352fe3a60b34f01bbcd25`.
These are operator checks, not a Python dependency in the installed Plus package.

## Q1 — User configuration

Yes: `features.goals = false` in `~/.codex/config.toml`, equivalently
`goals = false` inside its existing `[features]` table. Merge the leaf rather
than creating a duplicate table. Keep the daemon's supported
`featureOverrides.goals=false` as well; a user-config value alone is insufficient.

## Q2 — Effective daemon configuration, without a model call

Use the existing read-only RPC client `codex_plus/rpc.py` from this source freeze.
For remote checks, copy that one file into the operator's temporary folder; it
uses only Python's standard library. `RPC_FILE` below is that file's path, not
the installed Rust package. Run for each relevant root working directory:

```sh
python3 - "$RPC_FILE" "$ROOT_CWD" <<'PY'
import json, os, runpy, sys
from pathlib import Path
Client = runpy.run_path(sys.argv[1])["Client"]
home = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))).resolve()
c = Client(home/"app-server-control/app-server-control.sock")
try:
    result = c.call("config/read", {"cwd": sys.argv[2], "includeLayers": True})
    pid = c.generation.split(":")[1]
    args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    overrides = []
    for i, arg in enumerate(args):
        value = args[i+1] if arg in (b"-c", b"--config") and i+1 < len(args) else (
            arg[len(b"--config="):] if arg.startswith(b"--config=") else b"")
        key, sep, value = value.partition(b"=")
        if sep and key.strip() == b"features.goals":
            overrides.append(value.strip().decode())
    effective = result["config"].get("features", {}).get("goals")
    print(json.dumps({"generation": c.generation, "cwd": sys.argv[2],
        "last_startup_goals_override": overrides[-1] if overrides else None,
        "effective_goals": effective,
        "configured_sqlite_home": result["config"].get("sqlite_home")}))
    assert overrides and overrides[-1] == "false" and effective is False
finally:
    c.close()
PY
```

Check the peer PID's executable against the fleet's pinned stock daemon too.
Matching the last parsed override avoids accepting an earlier false followed
by true. Do not print the full effective config: it may contain unrelated
private settings. This proves startup/config state, not previously loaded
thread runtime state. A controlled fresh/cold Plus load is still required.

## Q3 — Capture native goals read-only

At stock 0.163.0 the native database is `goals_1.sqlite` under the effective
SQLite home. That defaults to `CODEX_HOME`; `sqlite_home` configuration or the
daemon's `CODEX_SQLITE_HOME` can override it. Resolve that home first. Capture
all statuses, including complete records; the current native API may be disabled
after the change, but the records remain readable:

```sh
python3 - "$SQLITE_HOME/goals_1.sqlite" <<'PY'
import json, sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1]).resolve()
if not p.exists():
    print(json.dumps({"database": str(p), "exists": False, "goals": []}))
else:
    db = sqlite3.connect(p.as_uri()+"?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT * FROM thread_goals ORDER BY updated_at_ms,thread_id")]
        print(json.dumps({"database": str(p), "exists": True, "goals": rows}))
    finally:
        db.close()
PY
```

Do not clear/pause/update those records as part of capture. No native goal is
automatically converted; reviewed text can explicitly become a paused Plus goal.

Fleet-config reported a count-only `mode=ro` snapshot on 2026-10-18: zero native
goal rows on all four boxes, no configured `sqlite_home`, and no daemon
`CODEX_SQLITE_HOME`. It will repeat capture at rollout; this snapshot does not
waive the check.

## Q4–Q5 — Install/restart ordering and quiet

The proposed order is valid with a hold on new Codex work between installation
and restart: extended quiet + root Idle checks, native-goal capture, configuration,
install/wrapper, stock-daemon restart and read-only verification, global block,
then controlled TUI restart. The restart must use `codex-stock`.

A prematurely started 0.3.2 client cannot authorize autonomous custom goals
against a daemon lacking the required startup override. Isolation fails closed.
Paused objectives may still be stored; Resume can briefly record active before
the observer rejects isolation and requires input. This does not disable the
old daemon's native scheduler; keep the hold until restart and verification.

Yes, quiet includes **all active Codex turns** and owner coordination, not just
helper/ledger counts: TUIs, AP workers, stdio consumers and advisor consultations.
The helper quiet query alone does not prove those are idle. A stdio process may
be independent of the shared daemon, but the rollout should still hold new work
and account for those consumers; do not kill them by blanket process matching.

## Q6 — Retention

`0.163.0-plus.0.3.1` is the intended minimum rollback package. The release must
verify its behavior on a quiet, goal-populated ledger before claiming that
rollback qualified. Keep native goals disabled and Plus goals inert on rollback.

Python 0.2.0 is **not a supported goal-ledger rollback target**: its protocol-1
helper and pre-goal lifecycle have not been qualified against protocol-3 goal
state. It may remain an archive, but do not advertise it as operational rollback.
Removing its explicit retention is the user's fleet-config decision. No package
is removed by this source release.

## Q7–Q8 — Process roles and lock contenders

There is no new long-lived role. The roles remain `codex-plus-runtime observe`
and `supervise`, with optional leading `--package-root PACKAGE`. `park` is the
registration command. A lock contender is another process in those same roles
waiting for the per-root role lock, not a separately named executable or role.
Protocol-3 startup normally exits duplicate contenders instead of accumulating
them, but scans must still include old/pre-existing ones during cutover.

`goal reconnect` is a finite command; when observation is needed it launches a
`supervise` role, which starts `observe`. `goal` mutations can wait briefly on
the mutation lock. Hold new registrations/goal commands and let them settle;
do not treat unrelated managed shell jobs as coordinator processes to kill.

During release building, this root's own worker parks use its preserved
pre-goal development helper at `.build/codex-plus-runtime`, SHA-256
`10f9847459b8b7c0396e3772e91f88222c84d0ce9f099c00f4930369024ac709`.
It matches `docs/evidence/park-current-turn.json`: the current-turn lookup fix
with no ledger-schema or protocol change. It is not byte-identical to installed
0.3.1. Read-only inspection confirmed that the live ledger still contains only
`parks`, `callbacks`, and `jobs`; goal-aware qualification uses private homes.
These real-session helpers count against fleet quiet checks and must settle
before installation. Preserve the executable while they run; do not overwrite
it with protocol-3 code or kill/cancel them to manufacture a quiet checkpoint.

## Q9 — Fresh-home status probe

With neither `CODEX_THREAD_ID` nor `--thread`, `goal status --json` exits **1**:
`{"error":"set CODEX_THREAD_ID or pass --thread"}`. Use an explicit synthetic
root in a throwaway home and unset an inherited model root:

```sh
env -u CODEX_THREAD_ID CODEX_HOME="$TEST_HOME" codex-plus goal status \
  --thread 00000000-0000-4000-8000-000000000001 --json
```

It exits **0** with `version:1`, the supplied `root`, `goal:null`, `binding:null`,
`input_epoch:0`, `open_dispatches:[]`, `running_jobs:0`, `completion_pending:0`,
`completion_jobs:[]`, `report_barrier:0` and empty `instructions`. Pending-input
and attention fields are null. It initializes the private ledger but starts no
thread, helper or model call. Assert the documented subset rather than exact key
equality. Both exit paths were freshly exercised with optimized v9 before release.
