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
   continue the current authorized task. For `RECOVERY REQUIRED`, follow
   [bounded job recovery](recovery.md). For `REPORT ONLY`, report and inspect
   read-only; keep independent authorized work moving while affected follow-ups
   remain subject to authority/cleanup checks and any legacy renewal barrier.
   Existing registrations keep watching remaining jobs. Register new work before
   ending the turn again if only background work remains.

The helper cannot force a model that keeps responding to become idle. Ending the
main turn is part of this agent workflow, recorded in this repository's AGENTS.md.
There are no global instruction changes. The stock daemon supports the workflow
with the delivery limits described below.

### Process selection in long sessions

For `--process`, registration first uses the stock daemon's live background
terminal mapping. If the process has already left that list, it checks command
items in the current parent turn before searching older history. An exact,
unique current-turn match takes precedence over an older reuse of the same
process ID. Finished work returns `already_finished` and needs immediate review,
without creating a completion callback.

If the current turn has no match, the existing historical lookup still requires
a unique command item. Pagination remains bounded at 32 pages of 100 items;
incomplete history, ambiguous matches, or RPC failures are not completion
evidence. Registration fails without partially arming a batch. This avoids
scanning thousands of unrelated old items for a command just completed in the
current turn; it does not turn a history limit into permission to guess.

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
model turn to report and diagnose. Ask for a concrete missing decision or authority
rather than a ritual confirmation; legacy report barriers still require explicit
renewal before affected follow-ups. Reports need not wait for the next manual prompt.

A continuous authorized execution watch uses `RECOVERY REQUIRED` for known
command results with unresolved cleanup, or genuinely unknown outcomes requiring
investigation. Known exits remain known. The recovery wake grants no replay or
resource-release permission; [recovery](recovery.md) defines the proof and scope.
The new ledger marker preserves conservative legacy report behavior and does not
override observer gaps, cancelled registrations or frozen callback text.

Notifications already submitted before the interruption keep their original
identity and frozen text during reconciliation. They cannot safely be relabelled
or blindly resent. The live/report split does not remove the stock daemon's
ambiguous-delivery window or add exactly-once model/tool execution.

## Watcher health in the HUD

Healthy waiting and clean idle state add no watcher row: existing worker and
background rows already represent the work. Pending delivery, uncertain wakes,
an unavailable observer, and restart reports remain visible even without active
workers. These notices share the HUD's six-row total limit with activity and
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
workers. Stop those separately if requested. A confirmed interruption of the
parent turn that registered a park also cancels that registration. A new user
message does not automatically mean cancellation: the current user request remains authoritative.
An already-started callback cannot be undone by cancelling its registration.

The stock daemon can briefly project a normally completing turn as `interrupted`
without an end timestamp. This alone does not authorize cancellation or a worker
completion callback. The coordinator retains that registration behind a durable
uncertainty gate. An ambiguous origin blocks new callback enqueue/start and
recovery resume until positive terminal evidence resolves it. Explicit
`park --cancel` remains immediate; previously cancelled work is never rearmed.

For an ambiguous interruption, the coordinator checks bounded local rollout
evidence for the exact session and turn. A completed event resolves the hold; an
aborted event confirms interruption. Missing, unreadable, mismatched, malformed,
or incomplete evidence remains unknown. This fallback depends on the stock
daemon's optional, unstable rollout path and must be rechecked on upstream bumps.
It reads only when needed and never sends conversation content to the model or
copies it into the ledger. `park --status` exposes unresolved evidence and
cancellation time/cause; the existing warning channel remains visible while
monitoring cannot safely decide. Reads are bounded to a 64 KiB session header
and a 2 MiB tail. Older events outside that window, concurrent file changes, or
later history rewrites can leave evidence unresolved; absence is not permission
to cancel, resume, or declare completion.

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
  For an ambiguous worker interruption, a verified daemon replacement plus a
  matching thread with no exact running task permits a `lost` report. An unreadable
  or contradictory runtime remains pending and retains the recovery generation;
  the same ambiguity on an unchanged daemon never proves loss.
- Recovery requires the root turn to have finished parking. Confirmed origin
  interruption cancels; ambiguous interruption remains gated across observer or
  daemon restart. Recovery never converts uncertainty into permission to resume.
- These guarantees preserve known and uncertain notification outcomes; they do not
  provide exactly-once delivery, model execution, or tool execution. A crash after
  input acceptance can leave that model turn interrupted. Host reboot, killed
  supervisors, and corrupted/lost storage are
  outside this qualification. Monitoring still expires after 24 hours.

## Stock daemon and delivery limits

The release uses the official stock daemon API and does not advertise the
experimental `queueIdempotency` capability. The historical daemon extension is
preserved as a [separate experimental patch](experimental-daemon-receipts.md);
the build script never applies it. The Rust coordinator retains its optional
receipt support so existing keyed ledger entries keep their provenance. Such
entries wait for receipt support if they cannot be reconciled from positive
consumed-message evidence; switching to stock does not make them safe to resend.

The ordinary HUD works with the stock daemon. On a stock daemon, the observer can
reconcile positive queue/history evidence and recover lost jobs after restart,
but ambiguous enqueue still requires manual inspection. Known queued entries get
bounded read-only reconciliation for temporarily delayed history. To retry that
inspection without blindly resubmitting a legacy callback:

```sh
/absolute/package/bin/codex-plus observe --parent SESSION_UUID
```

Do not delete the coordinator ledger to retry delivery: doing so removes duplicate
protection. The observer status records errors if its supervisor restart limit or
monitoring deadline is reached.

## Coordinator ownership

`rust-runtime/src/ledger.rs` owns the durable lifecycle: registration deduplication,
recovery classification, callback batching and frozen payloads, cancellation,
delivery transitions, and watcher health. Its `Store` operations check current
durable state; callers supply observed facts rather than write table state.

`rust-runtime/src/background.rs` validates RPC identities, gathers queue/history and
job evidence, and schedules observation and delivery. Process locks, deadlines,
backoff, and supervision stay there. `rpc.rs` owns transport; `evidence.rs` owns
the bounded, read-only exact-turn rollout fallback. Keep SQL inside the
ledger and test lifecycle behavior through its operations and observer outcomes;
old-schema migration fixtures may construct legacy tables directly.

The [glossary](../GLOSSARY.md) defines parks, watched jobs, and callbacks. Pending
interruption gates and cancellation provenance use additive coordinator SQLite
columns; no native daemon migration or rebuild is required.

## Cooperative package upgrades

Protocol-aware packaged coordinators follow the installer's adjacent `current`
symlink. The installer validates and stages an immutable package, then switches
that link. An observer finishes its current cycle, including any callback RPC
and its recorded result, before yielding. Its supervisor selects the current
compatible package and restarts itself from that concrete directory. No watched
job is stopped, no callback is recreated, and no daemon restart or model polling
is involved. The TUI can be exited and resumed normally; it does not perform the
observer transfer and need not stay open for it.

The detached-job candidate declares `coordinator_protocol: 4`; installed 0.3.2
uses protocol 3. The first transition is quiet. Automatic transfer
requires the same protocol, upstream identity, and CPU target on both sides.
The target must also have a strictly higher Plus `M.m.p` version, compared as
three numeric components. A lower or equal version in a different directory,
or a malformed release name, is refused with the existing upgrade warning.
Selecting the helper's own directory remains a no-op. Rollbacks always use a
quiet checkpoint; protocol compatibility does not qualify a live downgrade.
Protocol compatibility promises that either helper can safely use the shared
ledger, including callback identities, frozen messages and cancellation states.
An incompatible ledger or delivery change requires a new protocol and a quiet
rollout; an additive schema change alone does not establish compatibility.

Both supervisor and observer contenders recheck the selected package after
acquiring their process locks and before opening/migrating the ledger. A waiting
old contender therefore cannot resume old observation after a compatible switch.
The candidate's helper inventory is validated before execution. An invalid or
incompatible candidate leaves the working observer running and surfaces an
upgrade warning through watcher health. Development checkouts without a package
manifest and adjacent `current` link stay pinned to their source checkout.

Helpers keep their immutable source directory in cwd and in an internal
`--package-root` process argument. The argument retains the old package during
the small cwd-to-exec interval, so the installer's existing exe/cwd/cmdline scan
cannot remove a still-needed fallback. Successful exec updates both paths and
executes the selected native helper; failed exec restores the old
cwd and continues monitoring there with a warning. The same failed immutable
target is suppressed for that process lifetime; a different selected target or
a new process can retry. No fallback uses `previous` or another stale release.

Recovery semantics are unchanged: work positively observed still running by the
new observer can later produce LIVE COMPLETION. Completion discovered during
the transfer gap produces REPORT ONLY. Frozen callbacks retain their identity
and wording; an unresolved stock enqueue/ack gap remains uncertain and is not
blindly resent. A process crash during transfer follows ordinary recovery; a
cooperative transfer is not counted as an observer crash.

**Python-to-Rust cutover:** Python releases use protocol 1 and cannot transfer
live to Rust protocol 2. Hold new parks and reach a quiet checkpoint: no Python
or Rust helpers, armed parks, or open callbacks. Preserve the ledger and status
files, then switch the package. The ledger schema and status JSON remain version
1; existing callback identities, frozen text, and cancellations are retained.
The first new park uses the installed wrapper's current Rust helper. Compatible,
strictly newer protocol-2 releases can subsequently transfer while jobs run.
Restarting a TUI does not settle a registration or transfer an existing Python
observer. Historical releases before 0.1.0 also lack cooperative transfer.

Rollback to Python requires another quiet checkpoint. Keep the last qualified
Python package and its interpreter available until that rollback is retired;
the normal one-generation `previous` link alone does not retain it indefinitely.
Do not uninstall system Python as part of this runtime migration.

Fleet-config owns the package switch and rollout checks. This feature does not
download releases, enable automatic OpenAI updates, or use the historical
`scripts/handoff-observer.py` force-stop procedure.

Installer contract: preserve the existing directory layout and atomic `current`
switch after package verification. Rust process command lines are
`/absolute/package/bin/codex-plus-runtime supervise|observe --parent UUID`, with
the internal `--package-root /absolute/package/` argument. The trailing
slash makes the package visible to the existing cmdline retention check. The
ledger remains `$CODEX_HOME/codex-plus/background.sqlite`; table names and
callback states are unchanged. The existing quiet check still applies to the
bootstrap and incompatible updates. Fleet quiet/helper checks must recognize
both legacy Python command lines and the new native runtime roles. Package
retention continues to use executable, cwd, and command-line paths. Public
`advisor`, `park`, and `run` wrapper routes remain unchanged.

Run the focused protocol checks and real stock-daemon acceptance in private
homes before qualifying a release:

```sh
./scripts/build-runtime.sh
python3 tests/test_rust_runtime_upgrade.py \
  --runtime .build/codex-plus-runtime --evidence .build/rust-upgrade -v
```

The integration fixture seals distinct copies of the native helper executable;
it does not establish compatibility with legacy observers or replace native
per-target release qualification. It exercises real shell jobs, callback
consumption, crash recovery, competing supervisors, rapid package selection,
and preserved daemon/ledger identities. Native-worker continuity and explicit
queue/start acknowledgement boundaries also have focused coordinator tests.

## Verification

```sh
cargo +1.99.1 test --locked --manifest-path rust-runtime/Cargo.toml --lib
python3 tests/test_rust_runtime_acceptance.py \
  --runtime .build/codex-plus-runtime --evidence .build/rust-coordinator -v
python3 tests/test_rust_runtime_stock_integration.py \
  --runtime .build/codex-plus-runtime --stock-binary /path/to/official/codex \
  --evidence .build/rust-stock --scenario all
python3 tests/test_rust_runtime_stock_lifecycle.py \
  --runtime .build/codex-plus-runtime --stock-binary /path/to/official/codex \
  --evidence .build/rust-stock-lifecycle --scenario all
```

Registration regressions cover more than 3,200 unrelated historical items,
current-turn completion and process-ID reuse, batch lookup reuse, older-item
fallback, and fail-closed ownership, pagination and transport failures. The
official-daemon lifecycle suite also finishes a yielded shell before registering
it during the still-active parent turn: the result must be `already_finished`,
with no park, job, callback or extra model turn.

The preserved Python 0.2 interruption suite uses real native workers and a private official daemon.
It injects a selected RPC projection and, when needed, unavailable evidence to
make the race deterministic; it does not claim to control the daemon's actual
thread scheduling. It verifies exact-turn completion, genuine abort/cancel,
durable pending state after reopening SQLite, worker reuse, and one callback
without provider requests during monitoring. The recovery suite separately
exercises real daemon restarts. All provider fixtures stay on loopback.

Historical Python acceptance for the interruption fix passed 118 lifecycle tests, 18
evidence-reader tests, 11 interruption integration scenarios, all seven stock
recovery scenarios, and the normal completion integration. Packaging and launcher
checks passed too. The initial worker-restart failure exposed an overly broad
hold; verified daemon loss now reports UNKNOWN while unresolved same-generation
evidence stays pending. See [wake-interruption-fix.json](evidence/wake-interruption-fix.json)
for reports, source hashes, and the preserved failing cases. No deployed helper
or existing registration was changed by this acceptance.

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
