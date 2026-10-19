# Codex Plus goals — approved design and implementation

Current UI: [Plus goal status](goals.md#use-a-goal) now uses one blue row above
the editable input, replacing the right-footer placement discussed below. The
earlier design answers and qualification record remain historical evidence.

Status: Q1–Q18 agreed. The Rust coordinator and native client are implemented
for `0.163.0-plus.0.3.2`; source acceptance is recorded in
[verification](verification.md) and [the evidence record](evidence/custom-goals.json).
The companion release note records qualified archives. The official app-server
daemon binary stays unchanged; rollout changes its supported goal configuration.

## Current question register

Q1–Q18 have explicit approval. The user agreed to the consolidated design,
requested a final advisor review, and then answered “agree” to Q18, accepting
the stock-recovery exception. No product-policy questions remain open.

Fixed constraints: official daemon binary unchanged; Plus owns custom goal state and
continuation; genuine Idle and no model polling while registered work runs.
Design approval does not authorize publication, merging or fleet installation.

### Q1 — Work versus waiting

Should the main session continue immediately when useful work is available,
and become Idle while awaiting registered work or the user's decision?

Recommended: yes. Answer: **agreed**.

### Q2 — Goal ownership

Should each root session have one active custom goal, preserved when that
thread is resumed, with completed goals retained as history?

Recommended: yes. Answer: **agreed**.

### Q3 — First-version budgets

Should the first version focus on objectives, checkpoints and reliable waiting,
leaving hard token/time budgets out initially? Existing usage displays remain.

Recommended: yes. Answer: **agreed**. Do not claim native budget parity.

### Q4 — Completion authority

May the main agent mark a goal complete once its requirements are verified,
while preserving existing publication, merge and deployment approval gates?

Recommended: yes. Answer: **agreed**. Waiting for a user decision is explicit.

### Q5 — Familiar command

Should `/goal` inside Codex Plus control Plus-owned goals, rather than adding
`/plus-goal` alongside the upstream command?

Answer: **agreed**. Retain the familiar Plus-client command; the stock daemon
binary stays unchanged. Q8 approves disabling its native goal feature through
supported configuration at rollout.
Q14 defines the explicit transition for existing native goals.

### Q6 — Display

Should the custom goal use the existing right-aligned indicator in the input
area's footer/status line, with one compact state at a time and full details in
its goal menu?

Answer: **agreed**. Labels: `Goal active`, `Goal waiting ·
2 jobs`, `Goal needs input`, `Goal paused`, `Goal complete`. Waiting means an
enabled unfinished goal with an Idle parent. This adds no activity row or model
call. Q13 settles precedence and completed-state lifetime. Narrow displays
truncate within the existing surface rather than adding rows.

### Q7 — Scheduled reviews

Should scheduled model reviews be off by default, with optional one-hour or
two-hour reviews per goal? Completion events wake immediately; local health
checks do not call the model.

Answer: **agreed**. Scheduled reviews must not turn
healthy registered waiting into repeated model polling.

### Q8 — Native-goal isolation

Answer: **agreed**. Rollout sets `features.goals=false`, disabling native goals
for clients sharing that configuration. The official daemon binary is unchanged;
applying the setting safely requires a daemon restart. Saved native goals stay
untouched, and conversion requires an explicit choice. Plus controls its own
feature overrides so they cannot re-enable a competing native scheduler.

### Q9 — Continuation and no-progress handling

Answer: **agreed**. Each goal turn records a next action, exact registered jobs,
need for input, or completion evidence. Useful work continues immediately;
registered waiting stays Idle. Missing checkpoints or two consecutive turns
without meaningful progress become `Goal needs input`, not another automatic
continuation.

### Q10 — Pause and clear

Answer: **agreed**. Pause or clear suppresses future goal-driven continuation
without cancelling running jobs. Results are preserved; cancelling jobs is a
separate action. Accepted/in-flight wakes may still arrive and must respect
the newer goal state. Independent registrations are not implicitly cancelled.

### Q11 — Ordinary user input

Answer: **agreed**. User input takes priority. The goal remains enabled unless
the user pauses, replaces or clears it. Handle the instruction first, then
reassess the goal against the updated direction.

### Q12 — Closing and restarting

Answer: **agreed**. The detached coordinator keeps watching when the TUI closes;
closing the window does not pause the goal. Resuming the thread preserves its
goal. A host reboot requires starting Codex again; recovered results are reported
before Plus resumes old follow-up actions. Q18 excludes previously admitted
work resumed independently by the stock daemon. No reboot service is implied.

### Q13 — Indicator details

Answer: **agreed**. The goal indicator takes precedence over the Plan label in
the existing right-hand position without changing Plan mode. It has no separate
elapsed clock; job rows retain their durations. `Goal complete` remains until
the next ordinary user message; goal history stays accessible through `/goal`.

### Q14 — Existing native goals

Answer: **agreed**. Conversion requires explicit user confirmation. Copy the
reviewed objective into a paused Plus goal and preserve the native record.
There is no automatic migration or automatic native-goal re-enablement during
rollback. Native goal RPCs are unavailable with the feature disabled; obtain
the reviewed objective before cutover or from explicit user-provided text.

### Q15 — Changes during a running turn

Answer: **agreed**. Pause, edit or replacement invalidates future automatic
continuation without interrupting the current turn. Esc remains the separate
interrupt action. Replacement creates a new goal identity; old results stay
associated with the old goal. An accepted wake cannot be promised unsent.

### Q16 — Uncertain recovery

Answer: **agreed**. If ownership, outcome or delivery cannot be established
safely, show `Goal needs input` with the uncertainty explained. Retry temporary
connection failures locally. Do not blindly resend callbacks, rerun jobs or
wake the model repeatedly to check. Proven healthy recovery and normal live
completion remain automatic.

### Q17 — Missed scheduled reviews

Answer: **agreed**. Combine missed intervals into at most one eligible review,
with no catch-up burst. Scheduled reviews cannot wake a paused, complete or
needs-input goal, or a goal waiting on healthy jobs. A review never bypasses
the explicit-checkpoint and no-progress guards from Q9.

### Q18 — Stock restart recovery exception

Answer: **agreed**. The existing already-admitted/in-flight exception also
covers stock-daemon recovery. Plus gates its own future continuations after
an unwatched gap, but cannot retract interrupted work that the stock daemon
independently resumes before Plus reconnects. Do not promise report-first
ordering for that stock replay. The daemon binary remains unchanged.

## Consolidated implementation plan

Implement and verify in this order, preserving the existing release gates:

1. Extend the existing protected Rust store with goal identity, revision, state,
   checkpoint and continuation intent. Reuse queue delivery and reconciliation;
   keep one owner per root. Verify disposition, revision, two-turn no-progress,
   replacement/result retention and uncertain-delivery cases deterministically.
2. Add the root-only goal command and goal-aware coordinator scheduling. Extend
   helper lifetime to watch enabled goals after TUI exit. Verify genuine Idle,
   zero model requests during healthy waiting, shell/worker completion, input
   priority, pause/edit races and optional review coalescing with a fake clock.
3. Route the Plus `/goal` UI to the same mutation interface and read-only status
   snapshot, independently of native goal feature gates. Verify right-position
   labels, Plan precedence, narrow layouts, completion dismissal, composer input
   and the six-row activity/editable composer constraint.
4. Qualify native isolation using the official daemon and private homes across
   fresh starts, cold resumes and managed restarts. Control Plus overrides;
   refuse unsafe scheduling when isolation cannot be established. Cover observer
   restart, daemon restart, reboot-style report-only recovery, lost acknowledgements
   and deduplication without model polling. Existing probes are bounded evidence,
   not substitute acceptance for the implemented feature.
5. Update workflow documentation, global instruction proposal and fleet handoff
   contract. Require a goal-aware coordinator protocol bump and quiet cutover;
   qualify upgrade/rollback and both native targets. Include the already-tested
   worker-token alignment and long-history registration fixes in regression gates.

Fleet-config owns wrapper dispatch, global instructions, native-feature setting,
quiet checkpoint, stock-daemon restart and rollback policy. Preserve active work
until that separately authorized rollout. Build, publish, merge and installation
remain subject to their existing gates; this plan performs none of them.

## Purpose

Keep an objective persistent while allowing the main thread to become genuinely
Idle when only registered work remains. Codex Plus owns the goal and its wake
policy. It does not create a native Codex goal, toggle native goal status, change
collaboration mode, or write the daemon's databases.

## Goal ownership

One custom goal belongs to one interactive root thread. Persist its ID, revision,
objective, status and latest checkpoint in Plus-owned private SQLite state.
All mutations validate the expected goal ID/revision transactionally. Replacing
or pausing a goal invalidates stale continuation intents. Goal state survives a
client restart; this does not imply that the helper survives a host reboot.

Native and Plus goals are different features. Custom scheduling must establish
that the native runtime cannot also schedule this root, and explain any conflict
instead of changing native state silently. Q14 defines the explicit transition.
Do not silently migrate the iris session's goal.

## Initial interface

The Plus menu is `/goal`. The `codex-plus goal` command family provides start,
status, history, admission, checkpoint, pause, resume, edit, review, complete and
clear. [The workflow](goals.md) describes the implemented interface and recovery
limits. Native goal tools must not create a competing scheduler.

The compact goal indicator uses the existing right-aligned input-area
footer/status surface. Keep the six-row activity limit and composer visibility. Goal
status refresh is local and read-only; use the labels agreed in Q6.

## Measured native-runtime isolation facts

Seven private official-0.163.0 probe cases passed. A per-thread
`features.goals=false` override hides native goal tools and suppresses native
continuation while leaving the daemon's global Goals feature enabled and
`thread/goal/get` available. This does not clear stored native goal state.

The override works for fresh threads, unsubscribed Idle cache rebuilds and cold
resume with an explicit override. An already subscribed thread silently ignores
changed resume overrides. A later cold resume without the override re-enables
native goal tools: the override is not durable. The pre-implementation
coordinator's recovery resume supplied no override; the implemented feature
requires the stronger startup and loading-provenance contract in the workflow.
A successful resume response alone does not prove scheduler isolation.

Evidence: `.build/custom-goal-plan/feature-probe/report.json` and the executable
private probe beside it. These facts do not qualify native-goal migration or
arbitrary-client/reconnect races. Q8 chooses global configuration for isolation;
Q14 specifies explicit adoption. Qualify both before starting Plus scheduling.

A second private official-daemon probe confirmed that global
`features.goals=false` disables native tools and default native continuation for
fresh threads, cold resumes and managed-daemon restores. The saved native goal
row stayed unchanged. Managed and cold recovery each produced zero unsolicited
model requests during a 1.2-second observation window; this is a bounded test,
not long-duration qualification. Native goal RPCs return a disabled error.

This is a supported configuration change, not a daemon binary patch. It affects
other clients sharing that configuration and was tested after daemon restart,
not as a hot change to existing loaded runtimes. An explicit per-thread true
override re-enables native tools/runtime even when native goal RPCs are globally
disabled. Plus must therefore control its own overrides as well. Re-enabling the
feature later can reactivate stored native goals; disabling is not migration.
Evidence: `.build/custom-goal-plan/global-feature-probe/report.json`.
Q8 approves this rollout policy. No production configuration changed during
planning; fleet-config owns applying it at the rollout gate.

## Scheduling

1. Starting or explicitly resuming a goal supplies its current objective and
   checkpoint to an ordinary turn through the existing stock queue path.
2. The main session works normally. If only finite background work remains,
   `park` binds the observed work to the custom goal ID and records a waiting
   state before the main turn ends. A separate continuation intent carries the
   expected goal revision; editing a checkpoint does not erase job results.
3. While jobs remain running, only the local coordinator monitors them. The
   parent stays Idle and accepts ordinary input and idle-only commands. No model
   request is made just to ask whether a worker has finished.
4. Verified completion uses the existing completion-delivery path, carrying the
   current goal reference. The resumed main session reviews results and continues
   only if that goal is still current and authorized.
5. Pause, completion, replacement, a request for user input, or monitoring
   uncertainty suppresses autonomous goal continuation. Queued/accepted notices
   retain their existing cancellation and uncertain-delivery limits.

An optional one-hour or two-hour review interval can recover an unfinished goal
that became idle without registered work. It does not delay completion events.
When work is positively still running, the timer performs local health checks
rather than waking the model to poll it. No timer is configured until the user
selects it for an actual goal. Q9 requires explicit turn dispositions and stops
after two consecutive turns without meaningful progress. Missing checkpoints
require input rather than inferred continuation. Missed review intervals coalesce
under Q17; paused, needs-input and completed goals are ineligible. Reviews do
not override uncertainty or no-progress stops.

## First-version scope and limits

Reuse the Rust coordinator, private state, supervision, queue reconciliation,
callback identities, cancellation and restart evidence. Goal-bound delivery must
respect a newer goal revision or user pause; an old completion cannot restore an
old objective. Ambiguous enqueue/start outcomes remain visible and are not
blindly retried. A goal never expands permissions or approves deployment.

Do not claim parity with native goal token budgets or time accounting in the
first version. Ordinary model/worker usage remains available separately. Hard
budgets need a separate design and enforcement check. Monitoring recovery must
retain the existing distinction between live completions and report-only results;
unknown work is not rerun.

Require a new goal-aware coordinator protocol before release. Older helpers
must not ignore goal ownership during cooperative upgrades. Fleet-config
owns wrapper dispatch for the new command, the global instruction block, quiet
rollout, version pins and rollback. No installation is part of this proposal.

## Acceptance gate

Use private homes, the official daemon and loopback providers to prove:

- A custom goal stays logically active/waiting while the parent is actually Idle,
  with editable input, idle-only commands and no waiting-period model requests.
- Shell and direct-worker completion each wake the parent promptly; multiple jobs,
  duplicate completion events and mixed roots preserve ownership and order.
- Pause, edit, clear, replacement and submitted user input invalidate unsubmitted
  continuation intents transactionally. Queue/add can already admit a turn;
  requests in flight or accepted cannot be promised unsent. Late acknowledgements
  never reactivate a goal. Check the same transitions from both UI and CLI.
- Session/observer/daemon restarts and lost acknowledgements preserve checkpoints,
  callback identity and uncertainty without blind resends or job reruns.
- Optional hourly/two-hour reviews do not become a model-polling loop while work
  remains active, and stop when the goal is paused or complete.
- Existing worker-token alignment, long-history registration and advisor behavior
  require regression checks with the expanded six-row HUD. Exact release archives still
  require native x86_64 and aarch64 acceptance.

## Astra review — advice, not user decisions

GPT-6 Astra at xhigh independently read the proposal, probe evidence and source.
Verdict: revise before implementation. Full returned review and source log:
`.build/custom-goal-plan/advisor/review.json` and
`~/.codex/codex-plus/logs/21292c3a-96b5-4f7f-aea3-966a38b5864d.log`.

- Keep one authoritative Plus store and one observer per root. Reuse queue and
  reconciliation machinery. The UI reads a derived snapshot and uses the same
  mutation interface as CLI/model callers. Start with CLI commands and JSON;
  dynamic goal tools are not required for the initial design.
- Give every finalized goal turn an explicit disposition: continue with a next
  action and progress evidence; wait on exact jobs; request user input; complete
  with evidence. An hourly fallback does not replace immediate useful progress.
  The user approved this and the two-turn no-progress stop rule in Q9.
- Jobs belong to a stable goal ID. Dispatch intents carry revisions; stale
  intents can be invalidated without throwing away useful observations.
- Treat queue/add as the possible admission boundary, not only queue/start.
  Only unsubmitted work can be reliably suppressed by a later pause or edit.
- Extend observer lifetime deliberately: current supervision exits when no
  park/callback work remains and has a 24-hour bound. Saved goal state alone is
  not an unattended scheduler or reboot service.
- Qualify native-runtime isolation across all client loading paths and managed
  daemon recovery. The daemon may restore roots with default resume parameters
  and trigger Idle hooks before the helper repairs overrides. Keeping legacy
  Active native state on a custom root across that path is not yet qualified.
- Assess a goal-aware coordinator protocol change and a verified quiet handoff;
  older protocol-2 helpers cannot be assumed to enforce new ownership rules.

The advisor supported the familiar `/goal` command and existing right footer
position. The user subsequently approved Q5–Q18, including native-goal adoption,
edits during active turns, monitoring failure, missed review intervals and the
stock-recovery exception. The advisor's original review is
advice on that earlier proposal, not approval of implemented code.

## Final consolidated-plan Astra review

The user requested a second review after Q1–Q17 were agreed. GPT-6 Astra at
`xhigh` returned **revise before implementation** in approximately 7 minutes
35 seconds. It found the approach implementable with the unchanged stock daemon,
subject to the concrete contracts below. It did not approve implementation or
deployment. The review used read-only source inspection, not new test execution.

Reviewed HEAD: `c090a6ac7490249ed312251ee9f29408d37c5aa4`.
Reviewed plan SHA-256:
`0d2c0271f7d4593f66e5440ebf15d290068fecdde23aaa3d5ef1598eb0da1ab1`.
Frozen plan and full result:
`.build/custom-goal-plan/final-review/plan-reviewed.md` and `review.json`.
Advisor log:
`~/.codex/codex-plus/logs/348df264-7451-4e83-a3ab-bcce0fa8b838.log`.
This section was added after the reviewed snapshot; do not call it reviewed code.

### Required implementation contracts

- Bind each staged disposition to its exact root, goal identity, admitted
  revision, turn identity and input epoch. Finalize it once on positive terminal
  evidence; interrupted/failed turns cannot authorize a saved Continue. Ordinary
  unrelated turns are not goal turns and do not consume old checkpoints or count
  as progress failures. Record input epochs before supported UI submissions and
  steering, which can retain a turn ID. Stale model mutations fail rather than
  rereading new state and overwriting user intent.
- Allow at most one unresolved autonomous goal dispatch per root across goal
  replacements. Persist origin identity, frozen payload, client message ID,
  queue ID and known accepted turn. Reuse reconciliation, but do not let a ready
  timer or new-goal callback bypass an uncertain older dispatch. Admission begins
  at queue/add; late acknowledgements update delivery evidence, not goal state.
  Preserve independent park behavior.
- Establish effective native-goal disabling, not just a false user-file key.
  Profiles, trusted project layers and runtime overrides can override that key;
  default managed restoration reloads the saved cwd's configuration. Qualify
  supported startup/loading paths and conflicting settings. Tie admission to
  controlled daemon generation and loading provenance; a disabled goal RPC is
  not proof of a disabled per-thread runtime. Refuse unknown/conflicting loaded
  runtimes or lost provenance. Arbitrary same-user clients overriding true are
  outside enforceable Plus ownership; do not promise a stock API that proves
  effective per-thread features.
- Define deterministic progress checks around explicit evidence references,
  missing/malformed/repeated claims and once-per-eligible-turn accounting.
  Reworded prose, token use or mere tool execution alone does not prove progress.
  Semantic relevance and completion quality remain agent/user judgments; this
  is not a code-enforced semantic progress detector.
- Resume a safely validated unloaded parent even within the same daemon
  generation before eligible dispatch. Reconcile existing queue entries first;
  resume can trigger existing work. Goal-aware observation covers zero-job goals
  and still-running jobs from paused/replaced goals. Address both the observer's
  24-hour lifetime and separate registration deadlines without introducing a
  hidden goal budget. Stale/missing health, supervisor exhaustion and storage
  failures must visibly prevent autonomous scheduling when safety is unknown.
- Persist a report-only recovery barrier and report identity separately from
  delivered callbacks. A report turn cannot clear that barrier by writing
  Continue; renewed direction is required. Proven observer continuity remains
  automatic. Repeated restarts cannot repeat report actions.
- Require a new coordinator protocol and verified quiet cutover. Rollback
  quiesces both helper roles and contenders, preserves unresolved submissions
  without replay, leaves custom goals inert and retains readable history. Older
  helpers cannot see dispatchable goal callbacks. Keep native goals disabled and
  retain the last qualified fallback package explicitly with fleet-config.

### Additional acceptance requirements

Cover checkpoint-before-EOS crashes, steering/input races, unrelated turns,
already-finished or stale WaitJobs, completion between checkpoint and EOS,
same-generation unloading, conflicting config layers, uncertain old dispatch
across replacement and timer wake, health failures with zero jobs, repeated
report-only recovery, and clock jumps/coalescing. Publish waiting only once the
parent is actually Idle, not when registration merely succeeds. Goal pause must
not use park cancellation that would stop observing jobs.

Qualify root attribution and checkpoint CLI access under supported sandbox and
Plan workflows without expanding permissions. Supply custom goal instructions
across start/resume/compaction. Test all native command/status gates with native
Goals disabled, status projection compatibility, narrow layouts and AP geometry.
These were required tests at the time of the plan review; neither reviewer nor
root ran them during that review. See the implementation evidence for subsequent
executed checks and the release qualification still outstanding.

### Recovery guarantee — Q18 agreed

The stock managed daemon can resume previously interrupted work before any
client reconnects, independently of native Goals. Source inspection confirms
`app-server/src/request_processors/daemon_continuation.rs` constructs a
`daemon_recovery` continuation and calls `continue_turn_if_idle`; upstream
`app-server/tests/suite/v2/daemon_update_recovery.rs` explicitly expects recovery
provider requests before reconnect. Paths are relative to the pinned upstream
`codex-rs` tree. The earlier global-disable probe tested idle restoration, not
this interrupted-turn recovery path.

The user explicitly agreed to Q18 after this finding was presented. Q12 gates
future Plus continuations after an unwatched gap; it cannot retract already
admitted stock work that the daemon resumes independently. This extends the
existing in-flight limitation to restart recovery. Report-first ordering for
that stock replay is outside the agreed guarantee. No prevention mechanism for
that replay was established in this review; daemon binary patches remain excluded.
