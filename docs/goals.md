# Plus goals

Available in `0.163.0-plus.0.3.2`, coordinator protocol 3. This document specifies
the workflow; release qualification and fleet installation are separate checks.
See [verification](verification.md) for checks actually run.

## Use a goal

In a Plus interactive root session, `/goal` manages one persistent objective.
Use the goal menu to start, edit, pause, resume, complete or clear it. Completed
and cleared goals remain in history. Native subagents return results to their
parent; independent AP workers finish their brief within the turn.

The right-hand input indicator shows `Goal active`, `Goal waiting · N jobs`,
`Goal needs input`, `Goal paused` or `Goal complete`. Waiting means the daemon
reports the parent Idle while the local observer watches registered jobs. It is
not a held model/tool call. The goal takes precedence over the Plan label,
without changing Plan mode. It adds no activity rows; the worker/job/advisor
strip still occupies at most three rows. Completion remains indicated until the
next ordinary user message, not a synthetic callback or timer review.

Your input takes priority and does not automatically pause the objective. Pause
or clear stops future goal-driven continuation and leaves running jobs alive.
Results stay available. Esc separately interrupts a running turn; changing a
goal does not interrupt it. Already-admitted work can still execute. A new goal
has a new identity; old results cannot silently become the new goal's results.

Scheduled reviews are off by default. A goal may explicitly use a 3,600- or
7,200-second interval. Missed intervals combine into at most one eligible review.
Reviews never wake a paused, complete or needs-input goal, or poll healthy jobs.
There is no hard token/time budget; existing model/worker usage remains separate.

## Model workflow

Read the current goal instructions supplied by the client. The CLI is also
available from any repository through the installed `codex-plus` command:

```sh
codex-plus goal status --json
codex-plus goal instructions --json
codex-plus goal history --json
```

In a development checkout use `./bin/codex-plus`, or its absolute path. Set
`CODEX_THREAD_ID` to the current root when invoking outside a model tool; an
explicit `--thread ROOT_UUID` cannot redirect a session into another root.
Routine status and mutation results are compact. Read full retained history and
dispatch evidence explicitly with `goal history`; do not add it to every turn.

An admitted goal turn has a frozen goal ID, revision, exact turn ID and input
epoch. Admit the exact turn before registering its goal jobs; unrelated turns
are not enrolled automatically. Before ending that turn, submit one structured checkpoint:

```sh
codex-plus goal checkpoint --id GOAL_UUID --revision REVISION \
  --turn TURN_UUID --input-epoch EPOCH --checkpoint JSON --json
```

The checkpoint JSON declares one disposition:

- `continue`: a concrete next action and stable evidence references for progress.
- `wait`: the exact jobs already registered with `park`.
- `needs_input`: the reason user direction is required.
- `complete`: evidence that the objective's requirements are satisfied.

Use the JSON contract returned by `goal instructions`; ordinary prose is not a
checkpoint. A checkpoint is staged until positive evidence that its exact turn
ended successfully. Interrupted or failed turns cannot reuse a saved Continue.
Unrelated user turns do not count as missing-checkpoint failures. Revisions and
input epochs reject stale mutations; they do not authorize rereading new state
and overwriting the user's newer decision.

For finite background work, follow [the park workflow](background-work.md#agent-workflow):
register exact jobs, verify armed/already-registered results, checkpoint their
identities, then end the turn. An `already_finished` result needs immediate
review, not an empty wait. Registering work alone does not establish Idle.
Healthy local observation sends no model requests. Verified completion wakes the
parent through the existing stock queue path.

Each newly finished goal job can wake the parent while other jobs still run.
After reviewing that result, checkpoint the remaining wait and end the turn
again. Repeating the same wait does not repeat an already accepted completion
notice. The accepted notice records which positive results it actually carried;
pause or an invalidated, unsubmitted notice does not discard those results.

Missing checkpoints or two consecutive eligible finalized turns without progress
require input. Changed wording, token usage or tool execution alone does not
prove progress. The coordinator validates evidence structure and repeat identity;
its semantic relevance and completion quality remain agent/user judgments.
A timer cannot bypass this guard. Publication, merge and deployment still require
the user's existing approvals.

The model's shell must already have permission to write the private goal ledger.
Under read-only/Plan or workspace-only permissions, a checkpoint can be denied by
the sandbox. The coordinator then requires input instead of repeatedly invoking
the model or widening permissions. UI goal controls run through the local client;
their availability does not grant sandboxed shell writes. Automatic model-side
checkpointing under those restrictive policies is not established by this version.

## Recovery and uncertainty

State is stored in the existing protected
`$CODEX_HOME/codex-plus/background.sqlite`. It retains goals, revisions, exact
turn admissions, progress checkpoints and dispatch evidence. The local status
projection is read-only for the UI. At most one unresolved automatic goal
dispatch per root is allowed, including across replacement goals.

Closing the TUI does not pause a goal: its detached observer keeps watching.
Resuming the same thread restores its goal. A host reboot does not start a
scheduler service; start Codex again. Outcomes recovered across an unwatched
gap are report-only, with an explicit renewal barrier before old follow-up
actions resume. A report turn cannot clear the barrier by writing Continue.
Healthy proven observer continuity remains automatic.

The client records a submission identity before starting or steering an ordinary
user turn, then records its exact accepted turn receipt. Pending or uncertain
input blocks new automatic dispatch. A late receipt cannot clear a newer input
submission. This receipt is not permission to renew report-only work. On resume,
the client records the controlled load and asks `goal reconnect` to restart a
needed observer; repeated reconnects do not accumulate supervisor processes.
These are client/coordinator operations, not model polling instructions.

Queue/add can admit a turn before its acknowledgement. Pause/edit/clear and user
input invalidate work not yet submitted; they cannot promise to retract in-flight
or accepted work. Late acknowledgements update evidence without reactivating an
old goal. Independently, the stock managed daemon may resume an already-admitted
interrupted turn before Plus reconnects. Report-first ordering is not guaranteed
for that stock recovery. This exception does not permit new Plus continuations.

An acknowledged entry can leave the stock queue before its user message becomes
visible in history. The coordinator allows five seconds from the actual
acknowledgement/start transition for local metadata reconciliation, without
resubmitting or calling the model. Expiry or lost acknowledgement remains
uncertain. Recovery also blocks model goal mutations before a report is prepared;
only a positively accepted ordinary input at the current epoch or an explicit
host goal action can authorize renewed direction.

Unknown ownership, job outcome, isolation or delivery becomes `Goal needs input`
with a reason. Temporary transport errors retry locally. Preserve the ledger;
inspect goal and park status, reconcile positive evidence and ask for direction
before uncertain resends or reruns. No model polling is used as a health check.

## Stock-daemon isolation and rollout contract

The shared daemon remains OpenAI's stock binary. Custom goals require native
goals to be disabled and the root to have a controlled Plus loading history.
A false user config key or a disabled native goal RPC alone is insufficient.
Trusted project/profile layers and per-thread overrides matter.

Fleet-config owns the following rollout changes, on the separate rollout go:

1. Reach a verified quiet checkpoint and preserve the ledger and rollback package.
2. Preserve existing settings while adding `featureOverrides.goals=false` to
   `$CODEX_HOME/app-server-daemon/settings.json`. Also maintain the agreed native
   goal disable policy in user configuration. Stock daemon startup translates
   the settings override into `-c features.goals=false`, above project layers.
3. Restart the official daemon, preserving updater-disable policy. Verify its
   actual startup arguments and effective configuration, then restart Plus TUIs.
4. Extend the fleet wrapper's helper dispatch to include `goal`, install the new
   agent instructions only after the command exists, and check the native helper
   process roles as before.
5. Require protocol 3 and a quiet transition from protocol 2. Future compatible
   upgrades follow the existing higher-version cooperative transfer contract.

Goal ownership introduces two callback marker states: `goal_owned` means jobs
are still being observed, and `goal_settled` means those actual outcomes were
observed. Neither claims that a model callback was delivered. Fleet quiet checks
must treat only `goal_settled` as settled, in addition to the existing terminal
callback states, and count open `goal_dispatches` states `ready`, `submitting`,
`pending`, `starting`, `accepted` and `uncertain`. Helper process scans keep their
existing native roles. The old callback-only quiet query is insufficient.

The coordinator ties isolation to the peer's daemon generation, startup flag,
actual cwd configuration and controlled fresh/cold root load provenance. A
successful resume does not prove a changed override took effect on an already
loaded root. Unknown or conflicting loaded runtimes are refused. Plus controls
its own startup/resume overrides. Arbitrary same-user clients deliberately
re-enabling native runtime features are outside this ownership contract.
No model-based production probe is used to establish isolation.

Existing native goals are preserved, not silently cleared or paused. Capture a
reviewed objective before the native API becomes disabled, or provide its text
explicitly. Conversion requires user confirmation and creates a paused Plus goal.
It does not migrate native usage counters or approve old follow-up actions.

Rollback requires quiet dispatch, stopping observer/supervisor roles and lock
contenders, preserving unresolved delivery identities, and leaving custom goals
inert with readable history. Retain the last qualified rollback package explicitly,
beyond normal `previous` cleanup. Keep native goals disabled; re-enabling their
stored active records is a separate user decision. Old helpers must never receive
dispatchable goal callbacks. Do not downgrade a live goal observer.

## Proposed global instruction pointer

Fleet-config applies the exact approved block with the release; this repository
does not edit the live global file:

```markdown
For Plus-owned `/goal` work, read
[goal checkpoints and recovery](/home/liupan/.local/share/fleet-config/codex-plus/current/docs/goals.md#model-workflow)
on start/resume, before ending a goal turn, or when goal status needs input.
Use the admitted identity and structured checkpoint; while registered jobs run,
end the turn and let the local observer wake you. Preserve newer user direction,
report-only barriers and existing action approvals.
```
