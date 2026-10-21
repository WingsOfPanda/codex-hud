# Reviewed long-job handoff

Source implementation and private acceptance are recorded in
[the evidence](evidence/detached-jobs.json). This is part of the combined
`0.163.0-plus.0.4.0` candidate;
complete release archives, publication and installation remain separate gates.

## Agent workflow

These commands require the combined protocol-5 candidate. Until its quiet rollout,
exercise the candidate only with a private `CODEX_HOME`; keep live protocol-3
registrations on the installed helper. Building a binary is not a live migration.

1. Plan worker preparation and quick checks. Hand back expected runs over sixteen
   minutes and hardware/service operations before launch. If a permitted quick
   check unexpectedly takes longer, finish it in one bounded local wait.
2. Inspect the worker's script, declared inputs, resource claims, external
   dependencies, result checks and cleanup. Prefer safe parallel launches when
   worktrees, devices, services and ports permit them.
3. Freeze a proposal with `codex-plus job prepare --manifest PATH`. An explicit
   `--turn UUID` must match the active root turn. For an admitted goal turn,
   supply the exact `--goal UUID --revision N --input-epoch N` from goal status;
   an unrelated user turn does not implicitly join the goal.
4. Read the returned frozen manifest and `review_digest`. Execute only what was
   reviewed: `codex-plus job launch --id UUID --review-digest HASH --reason TEXT`.
   Launch returns an execution and observation receipt. Check both. A repeated
   execution UUID reconciles the same attempt; it is not a retry request.
5. Review an `already_finished` result immediately. Otherwise confirm observation
   is armed, checkpoint the exact returned watched-job IDs for an admitted goal,
   and end the main turn when only watched work remains. Automatic collection
   and completion delivery do not require periodic model checks.
6. On completion, inspect `job status --id UUID` and its result/log references.
   Verify the declared success criteria and cleanup before deciding the next
   action. A command's exit code alone is not proof of the user's whole objective.

If launch or observation returns an error **with an execution ID**, preserve it
and inspect that execution. It may already be running. `job watch --id UUID
--reason TEXT` explicitly recovers an unwatched execution in REPORT ONLY mode;
it does not replay work, rearm an explicitly cancelled watch or transfer it into
a new goal. Report-only and unknown results require reconciliation and renewed
direction before old follow-up actions resume.

`job list` and `job status --id UUID` retain durable execution truth after a
watch ends. `job cancel --id UUID` requests cancellation; check actual exit and
cleanup instead of treating the request as completion. `job resolve --id UUID
--evidence JSON_OR_PATH` records verified cleanup evidence for an unresolved
execution; it must not invent an exit status or authorize a rerun.

### Prepared manifest

The manifest is JSON with `schema: 1`, `topic`, `argv` and `cwd`. The executable
in `argv[0]` must be an absolute native program; invoke scripts with an explicit
absolute interpreter. Avoid implicit PATH resolution. Optional fields are:

- `inputs`: `{source: ABSOLUTE_PATH, path: SNAPSHOT_RELATIVE_PATH}` entries.
- `env`: explicit literal variables; `env_refs` and `secret_refs`: mappings from
  destination variable names to source environment names. Secret values are not
  copied into records. Unlisted environment is not inherited by the job.
- `external_dependencies`: reviewed `identity` text and optional structured
  `precondition` command. Checks and `cleanup` use `argv`, `cwd`, explicit
  environment and a finite `deadline_seconds`.
- `exclusive_keys`: resource names shared by participating roots in the same
  `CODEX_HOME`. Different homes and unmanaged workloads still need the root's
  resource check. A conflict returns immediately; it does not queue a launch.
- `expected_duration_seconds`: informational. `deadline_seconds` and
  `cancel_grace_seconds` are explicit reviewed termination policy. A deadline,
  precondition or cleanup requires a reviewed cancellation grace.

`{inputs}` expands to the immutable input snapshot, and `{execution}` to the
private execution directory, in supported arguments, cwd and literal variables.
Pass a stage-file path explicitly, for example
`env.STAGE_FILE = "{execution}/stage.json"`. A job may atomically write
`{"stage":"Loading model"}` there. Stage reporting is optional, bounded and
does not change the job's outcome.

## Assignment boundary

The interactive root plans each native worker assignment: preparation artifacts,
permitted quick checks, and the point at which the worker returns control.
Expected commands longer than sixteen minutes are handed back before launch.
Hardware trials, serving experiments and service restarts are handed back
regardless of estimated duration. Independent AP sessions retain their workflow.

Sixteen minutes is a planning guide, not a timeout. An allowed short command that
overruns finishes normally in one bounded local wait, without repeated model
polling, automatic transfer, cancellation or extra approval.

The worker returns an existing script or exact command, working directory,
environment and input references, expected duration, resource/service effects,
log/result paths, success checks and failure cleanup. The preparation result
does not claim that the experiment ran. The worker finishes its turn; the root
may later reuse it or assign another worker to analyze execution results.

## Review and launch

The root reviews a frozen description and its declared inputs, then launches
within the user's existing authorization. New authorization is required only
when the proposed action exceeds that scope. The review binds the root, its
originating turn, the input digest and any active admitted goal identity.
Newer user direction and invalid goal bindings invalidate an unconsumed review.

Declared scripts/configuration are snapshotted with their relative layout.
Executable/interpreter identity, arguments, working directory and explicit
environment are recorded. Secret references are resolved without putting secret
values in the manifest or status output. External services, datasets, container
images and worktrees have declared identities or launch preconditions. A script
hash does not promise a frozen whole machine or hermetic reproduction.

Only a daemon-confirmed root in a qualified unrestricted launch environment may
start a detached execution. Effective turn permissions and actual containment
must agree. Read-only, workspace, managed-network and unknown environments
refuse detached launch; no host service bypasses those restrictions. This is a
managed-launch contract, not a universal prohibition on stock shell tools.

Admission reads at most a 64 KiB session header and the latest 2 MiB of the
selected root's rollout. If that tail no longer contains the exact originating
turn context, launch refuses. Start a fresh turn and review fresh inputs; there
is no fallback to a profile label, global configuration or an unbounded scan.

The root actively seeks safe parallelism. Declared exclusive resource keys are
reserved across participating roots before launch. Conflicts return immediately
with evidence; there is no automatic scheduler queue or DAG engine. The root
still checks unmanaged workloads and dependencies outside these reservations.

## Execution and outcomes

Each execution has a durable UUID and versioned private job directory containing
the manifest, lifecycle records, logs, cancellation request and terminal result.
Execution truth is separate from park/watch records and the coordinator database
schema. Each executor remains pinned to the package that started it.

Launch intention, exclusive admission, executor/workload identities and result
publication are separate transitions. Repeated requests for an execution UUID
reconcile that execution instead of launching again. A crash in an unprovable
launch/result gap produces `unknown`, never permission to replay the command.
Identity includes host boot and process start identity; stale PIDs or process
groups are not cancellation targets.

The initial continuity guarantee targets TUI detach/resume and qualified stock
daemon restarts, including a killed daemon. Dedicated SSH login closure is also
qualified in the session-scope conditions in the fleet contract. It excludes
explicit scope termination, host reboot and failure
of the executor itself. Executor failure preserves evidence and unresolved
resource reservations for reconciliation; it does not promise recovery of an
exit status that was never recorded.

Execution estimates do not kill jobs. Explicit reviewed command/test deadlines
are honored. The existing watcher's 24-hour expiry must not discard detached
outcomes or silently abandon monitoring. Log completeness, exit/signal and
cleanup outcome are recorded before completion is announced. A logging failure
must not silently inherit the ordinary runner's cancel-on-output-error behavior.

## Cancellation, completion and recovery

TUI closure, goal pause/clear and cancellation of a watch leave executions alone.
Job cancellation is explicit and targets verified ownership. Cancellation
requested, process exit and cleanup complete are distinct facts. Containers and
services need their reviewed identity-specific cleanup; killing a process group
does not establish that those effects have been removed.

An explicit host operator may use `job cancel --parent ROOT --id JOB` or
`job resolve --parent ROOT --id JOB --evidence JSON_OR_FILE` from a shell without
`CODEX_THREAD_ID`. These controls use local execution identity and cleanup
evidence, so they remain available with the TUI closed or daemon unavailable.
This path is for explicit human control; agents must not clear the actor variable
to use it as an automatic fallback.
Model-context controls still require a fresh active root and current input
authority. Preparation, launch and watch retain their model-root admission gate.

The existing observer delivers results to the owning root under park/goal rules.
It can wake an authorized root without an attached TUI. It preserves newer input,
pause/revision/input-epoch checks and report-only recovery barriers. A result
alone does not renew permission to continue old actions. Failed or uncertain
executions do not rerun automatically; a retry is a new reviewed execution.

The local HUD reuses `Background · topic · elapsed`, with an optional stage
explicitly reported by the execution. Prepared workers disappear when they
finish, and executions appear after launch. Detached UUIDs remain distinct from
native terminal IDs and `/ps` controls. Refresh is read-only, root-scoped and
model-free, preserving composer input and the shared six-row budget. A terminal
result with unavailable monitoring or unresolved cleanup stays visibly unknown;
its durable exit receipt remains available. Settled activity projections disappear.

## Upgrade contract

The combined release uses coordinator protocol 5 and execution record format 1,
with a quiet initial protocol-3 to protocol-5 cutover. The earlier detached-only
protocol-4 candidate was unreleased.
Protocol-3 observers must not interpret detached records as native terminals.
Executor record-format compatibility is separate from coordinator protocol,
upstream and CPU compatibility. A stable job format alone waives none of the
existing upgrade or rollback gates.

A maintenance hold serializes with new execution admission and the coordinator's
automatic model-work admission. It reconciles already queued/accepted work before
declaring admission quiet. Existing executions continue; the old package remains
retained while an executor or unresolved execution needs it. Compatible tested
upgrades replace the TUI/coordinator and reconnect observation. Unsupported
changes wait instead of killing or replaying executions. Fleet-config owns the
installer, helper/quiet checks, global instruction rollout and deployment go.
Explicit goal/advisor commands, native/AP work and user input remain outside the
hold and need owner-confirmed quiet. The hold has no automatic expiry. See the
[fleet contract](detached-jobs-fleet.md) for operator queries, retention, rollback
and the `rollout.executions` decision matrix.

Verified explicit cleanup resolution can make an unknown execution quiescent;
it does not change its unknown command outcome or authorize old follow-up work.
Only a higher Plus version with matching protocol, upstream, CPU target and
execution format can be considered for cooperative transfer.

For the fleet's global instructions, replace the native-worker wait paragraph
with this block only after the package and `job` wrapper route exist:

```markdown
Plan native-worker assignments around preparation, quick checks and a clear handoff.
Workers return expected runs over sixteen minutes, hardware trials, serving experiments
and service restarts before launch. The root reviews and launches them with
[reviewed detached jobs](/home/liupan/.local/share/fleet-config/codex-plus/current/docs/detached-jobs.md#agent-workflow),
using existing authorization and safe parallelism across worktrees/resources.
Sixteen minutes is an estimate: an allowed quick check that overruns finishes in
one bounded local wait. AP workers use native shell work and finish within their
turn; managed detached jobs are for interactive roots. Leave `job maintenance
enter/leave` to the host operator.
```

## Acceptance gates

1. Private official-daemon lifetime and permission probes on x86 and ARM:
   TUI detach/resume, graceful stop, abrupt daemon death, same execution identity,
   continuing logs, durable result and one workload side effect. Refuse restricted
   and unknown environments before the detached workload starts.
2. Deterministic fault barriers around intent, spawn, exec, identity/ack, child
   exit and result commit. Cover duplicate requests, corrupt/partial writes,
   disk errors, executor loss, stale identity, cancellation/exit races, explicit
   deadlines, cleanup failure and changed inputs. Require known/unknown results
   and no replay.
3. Observer/supervisor loss, daemon generations, completion in an observation
   gap, duplicate delivery, root resume without TUI, newer input and goal state,
   maintenance races and simulated passage beyond 24 hours.
4. Independent and conflicting jobs across two roots; qualified package switches,
   incompatible/rollback refusal, old-package retention and failed upgrade recovery.
5. HUD identity/isolation, elapsed time, six rows, input/resize, no model calls
   from refresh, native regressions and clean frontend patch reconstruction.

Mock-only checks do not establish daemon-restart survival. Publication, merging
and fleet installation remain separate user decisions.
