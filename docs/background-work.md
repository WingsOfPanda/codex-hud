# Idle main session and completion wake

`codex-plus park` registers specific finite jobs with a local observer. After the
main agent ends its current turn, the main session is genuinely idle. The worker
HUD and native background-terminal UI can continue updating locally. A verified
completion queues one notification and starts it only when the parent is idle.
A supervisor restarts a crashed observer. Watcher health, short notifications,
and report-only restart handling run locally and work with the stock daemon.
The separately staged daemon extension supplies stronger delivery receipts;
these client features do not require activating it.

The observer uses local app-server RPCs, not model turns. A completion wake starts
a normal model turn so the main agent can review the result and continue. Other
workers may still use their models while the main session is idle.

## Agent workflow

1. Dispatch related workers with the [topic/batch naming rule](../README.md#worker-groups),
   or start a finite shell command with a short initial yield so its process ID
   returns while it runs. Continue any independent work first.
2. During the main agent's active turn, register exactly the remaining jobs:

   ```sh
   /home/liupan/Aerius/codex-plus/bin/codex-plus park \
     --worker /root/build__b1__compile \
     --process PROCESS_ID \
     --reason "Finish build and review the result"
   ```

   Repeat `--worker` or `--process` for multiple jobs. Use only applicable options;
   worker selectors accept a direct child's canonical path or thread UUID. The
   parent defaults to `CODEX_THREAD_ID`; outside an agent tool, supply `--parent`.
   Registration validates the parent and job identities. It does not wait for jobs.
3. If jobs are armed or already registered, finish the main turn with a short
   waiting notice. **Do not keep the turn open with sleeps, `wait_agent`, or
   repeated process polls.** Already-finished jobs should be reviewed immediately.
4. Read the notification kind. For `LIVE`, inspect the identified results and
   continue the current authorized task. For `REPORT ONLY`, report the outcomes
   and wait for renewed user direction before resuming the old follow-up work.
   Existing registrations keep watching remaining jobs. Register new work before
   ending the turn again if only background work remains.

The helper cannot force a model that keeps responding to become idle. Ending the
main turn is part of this agent workflow, recorded in this repository's AGENTS.md.
There are no global instruction changes. Full crash recovery requires the patched
shared daemon described below; an older daemon retains the conservative fallback.

## Shell display versus completion delivery

The HUD lists this root session's native background commands automatically through
`thread/backgroundTerminals/list`. Display does not require registering a watch,
and refreshes do not call a model or launch a turn. To wake the main session on
completion, register the command with `codex-plus park` before ending the turn.

A command's `seen` timer starts when the current client first observes its item
and process identity. It is not the total job runtime: the stock API supplies no
start timestamp. Stale snapshots show uncertainty; a missing running entry only
removes its display row. The coordinator still requires terminal evidence before
reporting successful completion.

## Live wakes and restart reports

Notifications carry a kind, a compact job/outcome summary, and a lookup reference
to the durable ledger. `codex-plus park --status --parent SESSION_UUID` returns
the complete identities for locating native command items and worker turns.
The standing response rules live in [AGENTS.md](../AGENTS.md); notifications do
not repeat the full workflow or include command output.

A fresh registration is eligible for `LIVE` completion. When an observer process
or daemon restarts, the first reliable observation of previously watched work
determines its treatment: a completed or lost job becomes `REPORT ONLY`; a job
positively observed still running can produce a later live completion. An RPC
error does not prove completion, and the recovery classification survives retries.
An old reason does not renew permission to carry out follow-up actions.
Reports use the same idle-session delivery path as live wakes: they may start a
model turn to report the result, then wait for the user. They are not held until
the next manual prompt.

Notifications already submitted before the interruption keep their original
identity and frozen text during reconciliation. They cannot safely be relabelled
or blindly resent. The live/report split does not remove the stock daemon's
ambiguous-delivery window or add exactly-once model/tool execution.

## Watcher health in the HUD

A compact line below the worker list shows waiting, pending delivery, an uncertain
wake, an unavailable observer, or a restart report ready for delivery. It remains
visible when no worker rows are active. Clean idle state hides the line. Waiting
uses dim text; delivery problems and restart reports use normal brightness.

The coordinator atomically writes a small private status file beside its ledger:
`$CODEX_HOME/codex-plus/SESSION_UUID.status.json`. The TUI reads it locally without
model calls and independently of worker-metadata RPCs. A waiting heartbeat older
than 15 seconds is shown as unavailable, rather than as proof that the job failed.
Uncertain/error/report states remain visible while unresolved, even if monitoring
has exited. A delivered report is no longer marked ready.

Missing status files hide the line; invalid or unreadable files show unavailable.
Remote-workspace clients do not display an unrelated local status file. Explicit
cancellation can clear unresolved local watcher status without deleting history
or stopping the actual job.

## Status and cancellation

```sh
bin/codex-plus park --status --parent SESSION_UUID
bin/codex-plus park --cancel --parent SESSION_UUID
```

Cancellation disarms callbacks; it does not kill the registered processes or
workers. Stop those separately if requested. Interrupting the parent turn that
registered a park also cancels that registration. A new user message does not
automatically mean cancellation: the current user request remains authoritative.
An already-started callback cannot be undone by cancelling its registration.

## Completion and delivery guarantees

- Worker jobs are tied to a specific child turn. Reusing a worker in a later turn
  is a new job. Parent ownership is checked before registration.
- Shell jobs are tied to the exact native item and process IDs. A process
  disappearing from the running list is not proof of completion; a canonical
  terminal item is required. Its originating turn can remain unknown until that
  item becomes available in history.
- Completions observed together can share one callback. Completed, failed, and
  interrupted outcomes are reported; success is never inferred from silence.
- The observer waits through an active parent or approval/input wait. It resumes
  an unloaded registered root only during verified daemon-generation recovery,
  after cancellation and queue checks. It does not steer active turns or bypass
  queued user input.
- Observer state lives under `$CODEX_HOME/codex-plus/` (normally
  `~/.codex/codex-plus/`). It contains job IDs, daemon identities, delivery records,
  the supplied reason, and the immutable callback text, without command output
  or copied conversations. The patched daemon adds its own receipt table to its
  existing queue database through a native migration.
- A process lock and durable identities prevent normal duplicate observers from
  delivering the same event twice. Registration does not silently rearm a cancelled
  or expired job. Monitoring is bounded to 24 hours per registration.
- With a daemon advertising `queueIdempotency`, each callback uses one durable
  key and immutable payload. Queue insertion and its receipt commit together.
  The daemon records durable input acceptance before allowing the callback to
  sample its model; retained queued input is reconciled against persisted history
  after a crash. Retries return the existing pending, consumed, or cancelled
  receipt. A start acknowledgement alone is not proof of durable delivery.
- Cancellation creates a receipt even if enqueue has not arrived, preventing a
  delayed request from reviving it. A consumed receipt remains consumed.
  Cancellation cannot undo hooks, prior model work, or every user-message artifact.
- An observer crash is restarted locally with bounded backoff (five retries).
  A daemon restart is detected using peer PID, process start time, and boot ID.
  Recovery loads only the explicitly registered root, checks cancellation and
  foreign queued input first, and reconciles the original job identities.
- A missing old-generation job is reported as `lost`, with outcome `UNKNOWN`.
  Canonical completed/failed/interrupted results take precedence. Shell commands
  and workers are never automatically rerun: an orphaned process may have survived,
  and side effects cannot safely be inferred or replayed.
- Recovery requires the root turn to have finished parking. An interrupted origin
  is conservatively cancelled because Codex does not distinguish every user
  interruption from daemon loss. This avoids reviving possibly cancelled work.
- These guarantees cover durable notification delivery, not exactly-once model or
  tool execution. A crash after input acceptance can leave that model turn
  interrupted. Host reboot, killed supervisors, corrupted/lost storage, and
  modifying keyed queue entries with older standalone writers are outside this
  qualification. Monitoring still expires after 24 hours.

## Patched daemon and compatibility

Both the client and daemon must use this repository's patched build for full
recovery. Merely relaunching the TUI does not replace an already-running shared
daemon. `initialize.queueIdempotency` is the explicit capability check; unknown
fields sent to an old server do not enable this behavior. Existing ambiguous
callbacks created through an old daemon are never retroactively assumed safe to
retry. A keyed callback waits if the daemon is downgraded.

### Activation on this machine

The complete candidate and preserved stock rollback package are local build
artifacts under `.build/wake-recovery/daemon-package/`. They include the verified
binary and the matching 0.162.1 runtime resources; a flat Rust binary alone is
not accepted by Codex's package installer. The candidate's GNU target and voice
manifest CLI hash match the compiled binary.

Activation changes the shared daemon for every Codex pane using this Codex home,
restarts it, and pins the selected local package. Running commands or workers may
be interrupted. Once that shared change is authorized:

```sh
CODEX_HOME=/home/liupan/.codex \
  /home/liupan/Aerius/codex-plus/.build/wake-recovery/daemon-package/candidate/bin/codex \
  app-server daemon update --from-cli --yes
```

The stock rollback package is preserved independently of the daemon installation:

```sh
CODEX_HOME=/home/liupan/.codex \
  /home/liupan/Aerius/codex-plus/.build/wake-recovery/daemon-package/rollback/bin/codex \
  app-server daemon update --from-cli --yes
```

Rollback also restarts the daemon and pins the stock package. Fleet policy keeps
automatic updates disabled and requires an approved version for manual updates;
plain `codex app-server daemon update` fetches latest and is not that procedure.
The separate ordinary CLI installation and model configuration are not modified
by this package selection.

After activation, verify the running executable hash and the daemon's
`initialize.queueIdempotency` response, then exercise one registered completion.
Package hashes, isolated upgrade/rollback validation, and activation status are
recorded in [wake-recovery.json](evidence/wake-recovery.json).

The ordinary HUD works with the stock daemon. On a stock daemon, the observer can
reconcile positive queue/history evidence and recover lost jobs after restart,
but ambiguous enqueue still requires manual inspection. Known queued entries get
bounded read-only reconciliation for temporarily delayed history. To retry that
inspection without blindly resubmitting a legacy callback:

```sh
cd /home/liupan/Aerius/codex-plus
python3 -m codex_plus.background observe --parent SESSION_UUID
```

Do not delete either receipt ledger to retry delivery: doing so removes duplicate
protection. The observer status records errors if its supervisor restart limit or
monitoring deadline is reached.

## Coordinator ownership

`codex_plus/ledger.py` owns the durable lifecycle: registration deduplication,
recovery classification, callback batching and frozen payloads, cancellation,
delivery transitions, and watcher health. Its `Store` operations check current
durable state; callers supply observed facts rather than write table state.

`codex_plus/background.py` validates RPC identities, gathers queue/history and
job evidence, and schedules observation and delivery. Process locks, deadlines,
backoff, and supervision stay there. `rpc.py` owns transport. Keep SQL inside the
ledger and test lifecycle behavior through its operations and observer outcomes;
old-schema migration fixtures may construct legacy tables directly.

The [glossary](../GLOSSARY.md) defines parks, watched jobs, and callbacks. The
ledger extraction retains the existing SQLite schema and status JSON, so it
requires no data conversion or native rebuild.

## Verification

```sh
python3 -m unittest discover -s tests -p test_background.py -v
python3 tests/test_background_integration.py .build/codex \
  --evidence .build/auto-resume-root-gate
python3 tests/test_background_recovery_integration.py .build/codex \
  --evidence .build/wake-recovery/main-gate
```

The initial acceptance run passed 37 coordinator tests and the normal integration
scenario; see [the initial evidence](evidence/auto-resume.json). A live test in the main
pane also passed: a finite shell completion woke the idle parent, the parent
returned to idle, and a Sol worker completion woke it again. Both callbacks
were recorded as delivered, and no registered jobs remained.

The integration test uses a real isolated Codex app server and a loopback mock
provider. It verifies an idle parent with a live finite shell command, no provider
requests during the idle interval, canonical completion, one follow-up turn, and
no duplicate wake after restarting the observer. Additional recovery acceptance uses isolated real daemons and loopback providers;
see the recovery evidence linked from [verification.md](verification.md).
The recovery acceptance passed 64 coordinator tests, 51 native tests, seven
real-daemon crash/restart scenarios, the normal integration test, and the HUD
terminal smoke test. See [recovery evidence](evidence/wake-recovery.json).
Production daemon activation is a separate step from these isolated checks.

The newer watcher acceptance passed 92 coordinator checks, 29 native HUD tests,
eight stock-daemon integration cases, and 14 native terminal stages. See
[watcher-lessons.json](evidence/watcher-lessons.json).

The ledger extraction retained those 92 coordinator checks and added six checks
for cancellation races, frozen callback identity, stock ambiguity, keyed receipt
requirements, and dispatch ordering. All 98 checks, eight stock-daemon integration
cases, and three staged receipt cases passed the primary agent's acceptance run.
An existing populated database retained its schema and status output and remained
readable by the previous implementation. See [ledger-depth.json](evidence/ledger-depth.json).
