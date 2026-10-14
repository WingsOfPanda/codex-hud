# Idle main session and completion wake

`codex-plus park` registers specific finite jobs with a local observer. After the
main agent ends its current turn, the main session is genuinely idle. The worker
HUD and native background-terminal UI can continue updating locally. A verified
completion queues one notification and starts it only when the parent is idle.
A supervisor restarts a crashed observer. Watcher health, short notifications,
and report-only restart handling run locally and work with the stock daemon.
The release build keeps upstream daemon code unchanged; the historical receipt
extension is excluded from releases.

The observer uses local app-server RPCs, not model turns. A completion wake starts
a normal model turn so the main agent can review the result and continue. Other
workers may still use their models while the main session is idle.

## Agent workflow

1. Dispatch related workers with the [topic/batch naming rule](../README.md#worker-groups),
   or launch finite noninteractive shell work using [the shell workflow](shell-workflow.md).
   A short initial yield returns the native session ID while it runs. Continue
   any independent work first.
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
There are no global instruction changes. The stock daemon supports the workflow
with the delivery limits described below.

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

Healthy waiting and clean idle state add no watcher row: existing worker and
background rows already represent the work. Pending delivery, uncertain wakes,
an unavailable observer, and restart reports remain visible even without active
workers. These notices share the HUD's three-row total limit with activity and
overflow; delivery problems and restart reports use normal brightness.

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
  or copied conversations. The frontend release adds no native database migration.
- A process lock and durable identities prevent normal duplicate observers from
  delivering the same event twice. Registration does not silently rearm a cancelled
  or expired job. Monitoring is bounded to 24 hours per registration.
- On the stock daemon, positive queue or consumed-message evidence can reconcile
  delivery after interruption. A missing acknowledgement with no such evidence
  stays uncertain; absence alone does not authorize another enqueue. Cancellation
  removes a positively identified pending entry, but cannot retract an already
  accepted turn or prove that an ambiguous submission never arrived.
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
- These guarantees preserve known and uncertain notification outcomes; they do not
  provide exactly-once delivery, model execution, or tool execution. A crash after
  input acceptance can leave that model turn interrupted. Host reboot, killed
  supervisors, and corrupted/lost storage are
  outside this qualification. Monitoring still expires after 24 hours.

## Stock daemon and delivery limits

The release uses the official stock daemon API and does not advertise the
experimental `queueIdempotency` capability. The historical daemon extension is
preserved as a [separate experimental patch](experimental-daemon-receipts.md);
the build script never applies it. The Python coordinator retains its optional
receipt support so existing keyed ledger entries keep their provenance. Such
entries wait for receipt support if they cannot be reconciled from positive
consumed-message evidence; switching to stock does not make them safe to resend.

The ordinary HUD works with the stock daemon. On a stock daemon, the observer can
reconcile positive queue/history evidence and recover lost jobs after restart,
but ambiguous enqueue still requires manual inspection. Known queued entries get
bounded read-only reconciliation for temporarily delayed history. To retry that
inspection without blindly resubmitting a legacy callback:

```sh
cd /home/liupan/Aerius/codex-plus
python3 -m codex_plus.background observe --parent SESSION_UUID
```

Do not delete the coordinator ledger to retry delivery: doing so removes duplicate
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
  --evidence .build/frontend-recovery
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
That historical recovery acceptance used the experimental daemon extension;
it is not the release build or a deployment instruction.

The newer watcher acceptance passed 92 coordinator checks, 29 native HUD tests,
eight stock-daemon integration cases, and 14 native terminal stages. See
[watcher-lessons.json](evidence/watcher-lessons.json).

The ledger extraction retained those 92 coordinator checks and added six checks
for cancellation races, frozen callback identity, stock ambiguity, keyed receipt
requirements, and dispatch ordering. All 98 checks, eight stock-daemon integration
cases, and three staged receipt cases passed the primary agent's acceptance run.
An existing populated database retained its schema and status output and remained
readable by the previous implementation. See [ledger-depth.json](evidence/ledger-depth.json).
