# Plus Ultracode: proposed design and staged plan

Status: implementation authorized, 2026-10-18, following Astra review. The user
authorized design corrections, implementation and private builds/tests. Publication,
merge, installation, fleet settings/pins and shared-daemon changes retain their
separate gates. This document records the intended contract, not completed tests.

Source baseline: Codex Plus `9a7474a` on `feat/detached-jobs`, pinned OpenAI
0.163.0 (`83086188ae0614bd882818ff1aa23f3194962012`). The installed helper is still
`0.163.0-plus.0.3.2`; detached jobs are implemented and privately qualified in the
0.4.0 candidate, not deployed. The Claude comparison comes from house-skills'
provided brief; its full runtime was not independently re-audited here. The
withdrawn dispatch-contract skill is not reinstated or invoked by this proposal.

## Recommendation

Build a **Plus-owned session mode, independent of effort**, with the root planning,
reviewing and accepting bounded phases through existing native workers. Start
with a guided procedure and executable evidence, not a JavaScript workflow engine.
Keep the official stock daemon and its permission boundaries.

The MVP is useful only if mode instructions survive compaction and reach both
ordinary turns and completion wakes. Make that a feasibility gate before UI or
orchestration implementation. Prefer a fixed startup developer bootstrap which
reads a turn-bound snapshot from the helper. Existing Plus goal instructions use
this startup path. Stock compaction rebuilds session developer instructions;
private probes must confirm the complete bootstrap survives. An injected toggle
message alone is insufficient, including with client-message retention enabled.

## User-facing contract

Proposed controls are `/ultracode on`, `/ultracode off`, and `/ultracode status`.
The default is off, stored per root thread rather than per directory or box.
New sessions, AP sessions and child threads do not inherit a separate on-setting
merely because they share a cwd. The mode is enabled only by user direction.

On means that substantive tasks use the smallest useful set of phases: understand,
change, verify, with design or review separated when needed. The root decides
between phases. Conversational turns and trivial mechanical edits stay inline;
there is no requirement to manufacture eight workers or all phases for every task.
For research-only phases, acceptance is a concrete evidence brief and root review,
not invented failing tests or meaningless commits.

Off removes the Plus phase procedure for future work; it does not cancel the user
request, kill workers/jobs or discard their results. Existing completion reporting
continues. Explicit interruption/cancellation remains separate. Busy-session mode
changes are requested for the next root turn; UI acknowledgement must distinguish
requested state from confirmed context delivery.

Neither toggle changes the selected model, effort, concurrency cap, permissions,
approval policy or native-goal configuration. In particular, upstream effort
`ultra` keeps its own upstream meaning when Plus mode is off. Off is not a promise
of single-agent operation: normal Codex/user/AGENTS delegation still applies.

Show `Ultracode` in the existing configurable status line when active, with pending
or unavailable state when synchronization is not established. Add no activity row.
Keep the current three-row worker/background/advisor budget, editable composer and
existing Plus goal indicator. `/ultracode status` can explain state and the active
phase's artifact location; a full run browser is deferred.

Mode on does not create an idle loop or hourly model check. It changes how already
authorized work is performed. Plus `/goal` separately governs goal continuation;
its identity, input-priority and report-only barriers remain authoritative.

## What pinned Codex already provides

| Finding | Consequence |
| --- | --- |
| V2 effort `ultra` selects a proactive delegation instruction unless configured/catalog text overrides it; children receive this mode too. | It is not a workflow runtime and is not the Plus toggle. |
| Current local `gpt-6.1-sol` catalog advertises Ultra and maps its inference effort to xhigh; the picker places Max/Ultra under Advanced Reasoning. | No new effort level is needed. This is source/catalog verification, not a live provider acceptance test. |
| The fleet's legacy `[agents]` cap of 8 resolves to V2 total 9, root included. Descendants share the same runtime budget. | Eight child slots across the tree, not eight per child. Idle eviction is not a pending-work queue. |
| V2 explicitly ignores `max_depth=1`, and spawn has no cwd, worktree or sandbox argument. | Leaf-only work and explicit workspace use are instructions in this MVP, not enforced isolation. |
| Parent permissions/cwd are reapplied after role configuration. | A native explorer/reviewer role is not an enforced read-only sandbox under a writable parent. |
| Ordinary `park --worker` watches direct children and exact turns for at most 24 hours. | Batch registration is usable, but it is neither indefinite monitoring nor a final-only phase barrier. |
| Native child completion does not itself wake an idle parent. | Keep the Plus completion observer; do not replace it with repeated model status checks. |
| Root `thread/inject_items` accepts trusted client items without starting a model turn. | An ACK establishes recording/queuing, not the effective mode of a turn; retained items share an eviction budget. |
| Session developer instructions are reconstructed during compaction; Plus already appends its goal bootstrap there. | Qualify an additive fixed bootstrap first, preserving existing instructions. |
| Role configuration is applied after requested spawn model/effort, including a configured default role. | Qualify a role compatible with the brief and record actual effective model/effort. |
| Loaded/busy/subscribed `thread/resume` ignores instruction/config replacement; standalone settings updates can supersede continuation. | Do not implement toggles by pretending resume reconfigures a live root or by replacing collaboration instructions. |

## Small implementation shape

One Plus mode module should own the interface for reading/requesting mode state,
composing its bounded instruction, and synchronizing that instruction with a root.
The TUI and completion coordinator use that same module; neither assembles a
separate policy string. Reuse existing private-file, root-identity, RPC and
permission/error handling. No new daemon, plugin loader or scripting engine.

Persist a small versioned root record under `CODEX_HOME/codex-plus/ultracode/`:
root identity, requested on/off, monotonic revision, policy version, and delivery
state/evidence. It is mode state, not a queue of commands. Unknown acknowledgement
or a mismatched revision must not be displayed as confirmed activation. Do not
blindly replay an older on-request after a newer off-request.

### Instruction delivery and admitted-turn identity

Qualify a fixed, root-scoped startup developer bootstrap first. It directs the
root to read the authoritative Ultracode snapshot at each turn and after
compaction. Preserve existing developer instructions. The returned snapshot
contains the complete policy for its frozen revision, rather than relying on an
old on/off message. Fresh and genuinely cold-resumed roots must establish loading
provenance. A loaded/subscribed root cannot acquire new startup instructions by
calling `thread/resume`; unsupported roots remain unavailable until a qualified
fresh/cold load. No automatic daemon restart or forced eviction.

Keep requested mode/revision separate from the effective snapshot. Freeze the
snapshot at a supported Plus submission admission, identified by root, submission
ID and daemon generation; bind the actual turn only with positive evidence.
Mode requests and admissions share serialization. An already admitted submission
keeps its snapshot even if queued; a change applies to the next unadmitted turn.
Steering and mid-turn compaction preserve the current turn's snapshot. Lost ACKs
remain unresolved until evidence identifies the outcome; they never authorize
blind replay or an activation claim. Existing goal input, cancellation, maintenance
and report-only fences retain precedence.

The supported admissions are the Plus TUI and Plus observer. External clients
which bypass those paths do not gain an automatic effective-mode guarantee.
Status must distinguish unavailable, requested/pending and admitted effective
state. Toggle/display/status reads do not initiate provider requests.

Native child policy travels in the root's self-contained spawn/followup brief.
Direct app-server injection/settings changes to V2 children are rejected. The
brief includes leaf ownership, workspace, model, effort, contract, reporting and
long-job handoff rules. A qualified role must preserve requested model/effort;
capture the child's actual settings as acceptance evidence. Neither the role nor
the brief enforces a separate sandbox or native V2 depth limit.

Other interfaces are fallback experiments, not MVP dependencies:

- `thread/inject_items` can queue into active work and its ACK is not an effective
  turn receipt. `retain_client_developer_messages` is off by default and shares
  a 64,000-token newest-first retention budget; older items may be dropped or
  truncated. Local compaction has a different reconstruction path. Enabling the
  flag alone cannot establish durable policy.
- `turn/start.additionalContext` emits only changed values and replaces the whole
  context map; it must preserve unrelated keys and does not alone prove compaction
  persistence.
- `multi_agent_mode_hint_text` is capped at 400 tokens, shared with children and
  cannot reliably reconfigure a loaded root. It is not the full phase procedure.

The feasibility gate covers manual, automatic mid-turn, repeated and budget-pressure
compaction on each advertised provider path; fresh, loaded and cold-resumed roots;
daemon restart; on/off ordering; steering versus new turns; user and observer
admissions; and lost acknowledgements. Unsupported paths remain unavailable.
If the fixed bootstrap fails, report the concrete gap before choosing a fallback.

## Phase procedure

1. **Plan the phase.** Record the requested outcome, bounded pieces, dependencies,
   file ownership and execution/resource needs. Use fresh self-contained worker
   briefs with explicit model and effort: configured Sol, medium for routine
   bounded work and high for difficult implementation/debugging/review. Preserve
   the existing high fallback; advisor remains root-only Astra xhigh.
2. **Fix the baseline and acceptance.** Choose meaningful behavioral contract
   tests, existing no-regression tests, or a concrete acceptance command. New
   behavioral tests must fail for the requested reason, not because of a broken
   fixture. Save contract copies/hashes outside worker-owned worktrees. Record
   command, test IDs, failures, skips and collection count. Unavailable hardware
   acceptance is pending qualification, not a manufactured red result.
3. **Prepare workspaces.** The root creates piece worktrees from explicit commits
   containing their dependencies. Account for relevant dirty working-tree state;
   HEAD alone is not a snapshot of it. Preserve user edits and identify baseline
   changes separately. Use a shared checkout only when explicitly chosen for one
   bounded piece with non-overlapping ownership; workers leave shared changes
   uncommitted. Isolated workers commit their own implementation, not contracts.
4. **Dispatch the ready pieces.** Respect actual remaining capacity, at most the
   configured eight child slots. Pass `fork_turns="none"` when supported. Native
   children retain their initial parent cwd: use tool `workdir` and absolute
   edit/read paths, not merely a command in the brief. The worktree must be within
   permitted roots; supply relevant workspace instructions. No worker delegation.
5. **Wait without polling the model.** When independent root work is exhausted,
   arm exact direct-child turns together with `park --worker`, confirm registration
   and end the turn. Do not loop on short `wait_agent` timeouts. Handle actual
   completion/error/input events; reconcile existing state before a capacity retry.
   At a full cap, keep the undispatched piece pending in the plan, not in a hidden
   automatic scheduler. A repeated failure after real capacity change is a blocker.
6. **Accept each code piece at the root.** In order: contract hashes unchanged;
   no unapproved protected/out-of-scope changes, including relevant ignored files;
   every required contract ran and passed; baseline tests are not lost, newly
   failing or unexpectedly skipped; implementation is committed on its piece
   branch (or remains an inspected shared-checkout diff). Inspect the actual
   filesystem, including untracked files: `git diff` alone is insufficient.
7. **Integrate and decide.** Merge accepted pieces into a task integration branch
   with `--no-ff --no-commit`, rerun affected contracts and required regressions,
   then commit. The root resolves conflicts or creates a bounded repair piece.
   Perform one final code review; additional model opinions complement evidence,
   not replace it. The default branch, publishing and deployment retain their
   existing approval gates. Release worktrees only after no worker or execution
   uses their cwd, inputs or outputs.

A protected-file failure is a failed return, not permission to weaken the test.
A genuinely wrong contract may be corrected by the root, independently rechecked
against the original baseline, and resealed. Two failed returns warrant a progress
or blocker report, not an invented user-permission gate for an in-scope repair.

These checks detect the documented assertion-edit and ignored-conftest cases;
they are not a security boundary against arbitrary same-UID code or proof that
passing tests cover every requirement. Baseline source/build provenance matters:
in this repo `.build/.../source` is editable upstream source, not disposable cache.
Worktrees isolate Git state, not GPUs, ports, databases, global configuration or
all test dependencies. Tests must use private state and appropriate resource claims.

### Completion behavior differs from Claude's script runtime

For the smallest MVP, retain existing event-driven completion delivery. Eight
children can produce multiple root wakes; registering them in one park does not
make it an all-finished barrier. Each wake can support real acceptance work on a
finished piece, followed by another idle wait. Worker reports should be concise
with artifact references, but native child messages still enter parent history.
There is no claim that intermediate data is hidden in script variables.

Record root wake/model-request counts and reported root/worker token usage in the
MVP dry runs. During a parked interval with unchanged work, the provider-request
count must remain unchanged. Compare event-driven wakes honestly; do not call
per-child wake overhead free or convert raw tokens into subscription dollars.

A final-only phase wake is a separate small follow-on: an explicitly frozen
fan-in group, with one completion notice when all required jobs settle and early
attention for failure/input/uncertainty. It needs tests with goals, partial results,
changed user input and recovery. If final-only delivery is a required MVP criterion,
move this follow-on into MVP scope rather than claiming current park provides it.

## Long execution handoff

Use the already agreed [detached-job workflow](detached-jobs.md), not a second
execution store or a tmux/job-watch requirement. Expected commands over eight
minutes, hardware trials, serving experiments and service restarts return to the
root before launch. Unexpected overruns of permitted short checks finish through
a bounded local wait; eight minutes is not an automatic termination deadline.

Workers return exact command/script, cwd, reviewed inputs/environment references,
resource effects, expected duration, logs/results, success checks and cleanup.
The root verifies and freezes inputs, then prepares/launches within the user's
existing authorization. Prefer safe parallelism. Attach exact execution UUIDs and
source identity to the piece. Prepared implementation is not accepted execution.

Use job commands only when a qualified deployed helper and launcher support them.
Do not hold up the independent 0.4.0 release to develop Ultracode. Without that
capability the worker returns the request and the long-execution acceptance stays
pending; do not pretend ordinary `run` has the same restart durability or convert
an OS PID into a native park session ID.

After completion, the root verifies the frozen result and current task authority.
Reuse a worker for a coherent correction to its own piece; otherwise start a fresh
worker with a concise brief. Check commit/worktree identity before either. Failed,
uncertain or report-only outcomes never cause blind replay of write work.

## Recovery records without an automatic workflow engine

Persist a versioned run journal separate from execution-record format 1. At minimum:
root/request identity and current direction, mode revision, phase/piece/dependency
list, source commits and dirty-state references, worktree paths, contract copies
and hashes, baseline/test commands and results, exact child thread/turn IDs,
execution UUIDs, acceptance state, pending questions and uncertainty.

The journal is evidence for the root, not an executable replay log. On TUI/daemon
restart, reconcile native threads, executions, branches and existing ledgers first.
Report what is known; preserve report-only barriers and newer direction. Resume
planning or redispatch only under current authority. Do not treat a recorded
`running` or `accepted` string as fresh proof. Native observed elapsed may reset;
execution truth and worktree retention do not depend on that timer.

This differs from automatic run resume: MVP does not replay a workflow script,
restart failed agents, recover a lost launch by retrying it, or automatically
advance phases after an unobserved gap. A later run engine would own those rules
and need its own schema/compatibility contract, not overload argv job manifests.

## Failure, limits and read-only review

Native usage-limit errors can terminate a child, while connection failures may
retry indefinitely under the current upstream default. Sampling retries are not
agent restarts. An empty wait result is not progress; running/elapsed metadata is
not proof that a child is healthy or stuck. No automatic stall restart or usage
reset wake is promised in MVP. Preserve partial work, report terminal failures,
and let the root decide bounded continuation; never replay the original brief
blindly. A stuck connection may require explicit operator interruption.

Ordinary worker watches have a 24-hour bound. MVP phases should be bounded work,
not overnight native-agent supervision. Surface expiry/uncertain monitoring and
keep the journal for recovery. Ordinary expiry publishes local attention, not a
guaranteed model wake, and the same expired job cannot simply be rearmed. Recovery
requires exact-turn reconciliation and status inspection or renewed interaction. Local attention timers and final-only
fan-in are possible follow-ons; model polling is not the fallback.

Native roles cannot enforce read-only permissions under a writable parent. For
an enforced independent review, use a separately launched read-only session/process
with delegation disabled and explicit cwd, following the existing root advisor
pattern, and qualify denied writes on the target. Root acceptance commands may
need writable test state. Do not claim an explorer label or a brief is sandboxing.
Hard per-worker cwd, sandbox and V2 depth enforcement are outside this native-worker
MVP; they would require a separately designed worker adapter or upstream support,
not a TUI-only spawn parameter the stock daemon does not understand.

## Staged plan and acceptance gates

| Stage | Authorized work | Exit evidence |
| --- | --- | --- |
| 0: stock feasibility | Private official0.163 loopback probes for fixed startup bootstrap, all supported compaction paths, admitted-turn revisions, steer/new-turn ordering, fresh/cold/loaded resume, observer wakes, role precedence, permissions/goals and cap/expiry/error behavior. | Complete bootstrap/policy persistence and exact effective-turn receipts; no retention setting assumed. Compaction or wake gaps block implementation claims. No production configuration change. |
| 1: mode only | Native per-thread control and existing-status-line indicator; one state/policy module shared by TUI and observer; no workflow engine. | On/off does not change effort, permissions or goal state; no model call merely for toggle/display; roots isolated; pending/failed synchronization visible; new and resumed roots qualified. |
| 2: phased procedure | Self-contained leaf briefs, explicit workspaces, contract/baseline records, root acceptance and integration; existing event-driven park. | Honest worker; altered assertion rejected; ignored autouse-conftest detected; two parallel pieces then dependent piece; dirty baseline; unauthorized paths; no lost/skipped checks; no default-branch change. Use Codex workers, not only the prior Claude dry runs. |
| 3: lifecycle integration | Connect qualified detached jobs, preserve worktree/job pins, input priority and goal checkpoints; qualify reconstruction without replay. | Before/after long-job handoff; unexpected overrun not killed; full capacity then completion; failure/usage-limit result; off with work in flight; restart and uncertain delivery; final-result verification remains pending until real evidence. |
| 4: release candidate | Root review, both architectures, complete package/launcher/global-policy checks and the three-row/AP readiness checks. | Immutable artifacts and explicit handoff requirements. Private builds are authorized; publish, merge and fleet rollout retain their separate user gates. No automatic version/pin bump. |

Behavioral probes, implementation and private qualification are authorized.
Record fresh evidence as each gate passes; authorization is not test evidence. Assess any
coordinator protocol change from altered admission semantics even if SQLite is
unchanged; never infer live-upgrade safety from a matching job record format.

Deferred beyond this MVP: Code Mode script orchestration; schema-typed native
child results; automatic whole-run resume; a detailed run browser/pause/restart;
extra capacity queues; saved runnable workflows; automatic stall restarts or
usage-limit pauses; enforced per-native-worker sandbox/depth; judge panels and
voting. Basic root review is not deferred.

## Answers to the brief's nine questions

1. Own Plus setting. Ultra is already offered by the picker when the model catalog
   advertises it; local Sol catalog does. Keep native Ultra behavior separate.
2. `non_code_mode_only=false` has not been behaviorally qualified here. Current
   collaboration tools remain direct calls. Script-cell concurrency/lifetime/wake
   experiments are deferred and unnecessary for this MVP.
3. Use dispatcher-created worktrees and explicit tool paths/cwd. Spawn has no
   such parameter; inherited sandbox restrictions still apply. No sandbox escape.
4. Grandchildren share the current eight-child budget; ordinary park cannot watch
   them directly. Leaf-only is instruction-based in MVP, not native depth enforcement.
5. 0.4.0 is committed/private-qualified, not released or installed. Develop the
   mode independently; gate long-job use on deployed capability. Reuse a coherent
   piece's worker, otherwise brief a fresh one.
6. Preserve the journal fields above as recovery evidence. Use a separate schema;
   execution-record format remains execution truth, not workflow state.
7. Usage limits are terminal errors; some connection retries are unbounded.
   Neither establishes Claude-style stall restart or quota pause/resume behavior.
8. Park can watch eight direct children together. It has per-completion wakes,
   a 24-hour bound and ambiguous-delivery limits, so it is not an unconditional
   durable all-finished promise. Qualify and disclose those differences.
9. Enforced read-only review is desirable, but native role settings do not provide
   it here. Use the qualified independent advisor/read-only adapter when needed;
   per-native-role enforcement remains deferred.

## Source notes and present limits

Source paths below refer to pinned OpenAI0.163.0, not unverified newer main:

- `core/src/session/multi_agents.rs:77-118` and
  `prompts/src/model_messages/multi_agent.rs:47`: Ultra selection and role scope.
- `protocol/src/openai_models/reasoning_effort.rs:10-35`, TUI
  `chatwidget/model_popups.rs:476-665`: inference resolution and advanced picker.
- `core/src/config/mod.rs:1666-1675,2813-2824`,
  `core/src/agent/control/residency.rs:176-262`: cap translation and eviction.
- `core/src/thread_spawn.rs:99-111`,
  `core/src/tools/handlers/multi_agents_tests.rs:2485-2535`: V2 depth behavior.
- `core/src/agent/child_config.rs:71-85,152-200`: inherited permissions and replacement.
- `app-server/src/request_processors/thread_processor.rs:4550-4615`,
  `thread_input.rs:8-36`: live resume limitations and V2 input ownership.
- `app-server-protocol/src/protocol/common.rs:888`,
  `core/src/session/inject.rs:69-113`: root injection and client provenance.
- `core/src/state/additional_context.rs:15-33`,
  `core/src/session/thread_settings.rs:33-42`: context-map and continuation caveats.
- `features/src/lib.rs:2070-2073`, `core/src/compact_remote_v2.rs:565-578`:
  developer retention is conditional and disabled by default.
- `core/src/context/world_state/multi_agent_mode.rs:15`: 400-token custom mode hint.
- `core/src/responses_retry.rs:93-138`, `core/src/session/turn.rs:1797-1802`:
  connection retries versus usage-limit termination.

Plus behavior and qualification are in [background work](background-work.md),
[detached jobs](detached-jobs.md), [fleet contract](detached-jobs-fleet.md),
[advisor](advisor.md), and [source evidence](evidence/detached-jobs.json).
The initial design used read-only source analysis. Astra subsequently reviewed
it (local evidence: `.build/ultracode/review/result.json`, verdict `revise`). This
revision incorporates retention-budget, admitted-turn, role-precedence, bootstrap
and expiry findings. Private implementation evidence is recorded separately in
[Ultracode qualification](evidence/ultracode.json). That evidence distinguishes
stock-daemon fixtures, native terminal tests and genuine worker trials. ARM release
builds, publication, installation and rollout are not established by these checks.
