# Detached-job fleet contract

Combined detached-job/Ultracode contract for `0.163.0-plus.0.4.0`; not an installation instruction or
claim that release archives are qualified. Fleet-config owns its installer and
rollout. Use the release's qualification evidence before applying this contract.

## Identity and upgrade decision

Both `codex-plus.json` and the release's `handoff.json` carry
`coordinator_protocol: 5` and `execution_record_format: 1`. The latter describes
`codex-plus/executions/v1`, independently of the coordinator SQLite schema.
`rollout.executions` is either `wait` or `continue`:

| Change | Executions | Required conditions |
| --- | --- | --- |
| Initial protocol 3 → 5 | `wait` | Existing full quiet checkpoint; no unresolved executions |
| Higher Plus revision, same protocol, record format, upstream and target | `continue` only when qualified by that release | Maintenance admission quiet, owner-confirmed quiet for work outside the hold, retained executor packages, compatible observer reconnect |
| Protocol, record format or upstream change | `wait` | All executions resolved, no armed execution watches/open callbacks, full quiet checkpoint |
| Rollback | `wait` | Same strict quiet checks; a separately qualified rollback target |

A field value alone grants no compatibility. In particular, an OpenAI bump
waits; old-upstream observers are not qualified against a new daemon. Preserve
all existing native-goal isolation requirements. The detached-only protocol-4
candidate was unreleased. This initial combined rollout sets
`quiet_checkpoint: true`, `executions: wait`, `session_restart: required` and
`daemon_restart: false`, retaining the stock 0.163.0 daemon. The companion release note records final archive qualification on each architecture;
private feature tests alone are not archive acceptance.

For a `continue` rollout, armed execution watches and their supervisors are
expected. Do not require them to exit and do not merely ignore all execution
callbacks. `job maintenance enter`, then `job maintenance status`, must report
`held=true` and `admission_quiet=true`: queued, starting, uncertain or already
admitted work must be reconciled regardless of the callback's job kind. Held
ready notifications are durable but not yet admitted. Native workers/shells,
AP, user turns and other clients still need fleet's owner-confirmed quiet gate.
Existing executors keep running while `current` switches and compatible
observers reconnect. Verify package pins and monitoring, then explicitly leave
maintenance. A failed switch keeps the hold until the operator resolves it.

`execution_quiet` is separate: it requires no active or unresolved execution.
An explicitly verified cleanup resolution can make an UNKNOWN execution quiet
without inventing its missing exit status or renewing old follow-up authority.

## Operator queries and package retention

`codex-plus job list --all` is a read-only, box-wide operator query for the
selected `CODEX_HOME`, run without `CODEX_THREAD_ID`. It does not open/migrate
SQLite, contact the daemon, create files or reconcile execution state:

```json
{"schema":1,"execution_record_format":1,"unresolved":[{"root":"UUID","id":"UUID","state":"unknown","package":"/absolute/package"}],"packages":["/absolute/package"]}
```

Missing homes are empty. Missing/corrupt published records are errors, not a
quiet verdict. Package lists are not silently truncated. Read-only evidence can
be conservative; use explicit reconciliation/resolution before a quiet rollout.

Keep every package named by unresolved records, including a dead executor's
package, as well as every live executor package matched by exe/cwd/argv.
Executors use `codex-plus-runtime --package-root PACKAGE execution-executor
--parent ROOT --id UUID`; they never re-exec into the new package. Observers and
supervisors may transfer only through the qualified compatible upgrade path.
Do not delete a mismatched package while an execution still depends on it.

Rollback to 0.3.2 after execution or mode rows exist is **not qualified**. Retain the
first qualified combined protocol-5 release as the rollback floor until a later release
explicitly retires it. A settled row's backward compatibility must not be inferred
from an additive SQLite migration or from format-1 compatibility alone.

## Hold scope and operator control

The hold gates job prepare/launch/watch and the coordinator's automatic
queue-add, queue-start and thread-resume admissions. It does not prevent normal
park registration, explicit goal commands, advisor consultations, native
subagent/exec/AP work or user input. Cancellation and verified resolution remain
available. Fleet must obtain owner-confirmed quiet for the excluded work.

The hold is durable and deliberately has no owner PID, timeout or automatic
expiry. `job maintenance enter/leave` are operator-only host-shell actions;
status may be inspected by the model. After a crash the operator inspects the
hold, processes, ledger and queued work before leaving it. A held-state drift
line is appropriate; automatically clearing a stale-looking hold is not.

Host operators can cancel/resolve an orphan without a live root or daemon:

```sh
codex-plus job cancel --parent ROOT --id JOB
codex-plus job resolve --parent ROOT --id JOB --evidence /path/to/verified.json
```

Run these outside model context, with no `CODEX_THREAD_ID`. Model commands keep
fresh-root/input checks; agents must not unset the variable to bypass them.
Resolution evidence binds root and job, sets `cleanup_verified=true`, and gives
an `evidence` description. It cannot fabricate an exit or automatically rerun work.

## Storage and secrets

The candidate bounds each captured stdout/precondition/cleanup log to 64 MiB.
At the bound it keeps draining/discarding output, records incomplete logging and
continues the workload; output volume is not permission to kill or rerun it.
Input snapshots are separately bounded. Neither logs nor snapshots are
implicitly pruned with package installation.

Retention policy: retain execution records and outputs until result review,
cleanup reconciliation and all watch/callback delivery have settled. Operators
review storage monthly and before large trials. Settled executions older than
30 days may be archived to operator-chosen storage (for example `/data` on 910b)
and removed only with the user's archival/deletion authorization. Preserve a
verified archive and its root/job lookup before removal; retain unresolved jobs
regardless of age. This initial release supplies no automatic prune command.
Check the full manifest's snapshot/log budget and free space before large runs.

Secret values are absent from manifest/status/result JSON, but resolved references
are carried in the executor's `CODEX_PLUS_EXEC_REF_*` environment for its lifetime.
Same-UID processes or root may read them via `/proc`; this is not secret isolation
from that user. Fleet probes must not print process environments. Workloads can
also print their own secrets; logs remain private evidence, not public artifacts.

## Host qualification and instructions

Admission accepts only the canonical per-turn disabled permission profile plus
`danger-full-access`, and positively checked initial namespaces, unrestricted
security state and qualified user-session/tmux scopes. The stock fixture uses
exactly `sandbox_mode="danger-full-access"` and `approval_policy="never"` and
asserts the resulting turn context. Managed profiles are refused even when their
legacy sandbox field looks unrestricted. Policy drift causes an explicit refusal.

Both `session-N.scope` and user `tmux-spawn-*.scope` are in the inspected allow-list;
a scope name alone is not proof of survival. Current x86/910b production probes cover ordinary dedicated SSH connection
closure with live logind `KillUserProcesses=false`: the original session becomes
`closing`, the existing executor continues, another job launches in that closing
scope, and both survive graceful/abrupt private-daemon replacement and finish once.
Fleet may keep its SSH start pattern; record the scope and verify that live logind
property at rollout. A detached tmux session by itself does not establish a new
cgroup or stronger lifetime.

This qualifies the launching login closing, not an explicit session/cgroup/unit
termination, last-user-service shutdown, host reboot or executor crash. Those
remain outside the promise. Arbitrary service/container launch contexts refuse.
The additional probe is `tests/test_detached_job_session.py`; its controller must
use a dedicated SSH transport with connection multiplexing disabled, close it,
then confirm logind state from a separate connection before releasing the fixture.

AP Codex workers remain technically roots, but the workflow policy excludes
managed detached launches there: AP workers finish their brief within the turn.
The detached-job workflow is for interactive roots and their native workers;
it is not a hostile-same-UID or universal shell-tool prohibition.

Install the wrapper's `job` and `ultracode` routes before applying the new global
instructions. Load the new TUI; legacy roots without Ultracode's startup bootstrap
need a fresh qualified root, rather than merely resuming an older thread.
`docs/detached-jobs.md` is included in the candidate package DOCS list; use its
exact workflow block and preserve the current advisor/goal sections. No global
file or installed package is changed by this source implementation.
