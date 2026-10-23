# Recovery within the authorized task

Status: **0.4.8 implementation candidate; not activated on the fleet**. Existing
legacy report barriers remain intact. Source tests do not qualify a release or
authorize installation; use the published handoff for actual compatibility results.

## Decision

An execution problem puts its dependency into recovery, not the whole task on
hold. The root investigates, performs verified recovery within existing user
authorization, and continues toward the requested outcome. Independent authorized
work continues. Ask the user for a concrete missing decision, access or authority,
not merely because cleanup needs investigation.

Scope: Codex Plus roots only. Claude job-watch rules, including “never invent a
recovery at fire time,” and the Claude `Still ask first` policy remain unchanged.

Keep three facts separate:

| Fact | Meaning |
| --- | --- |
| Command outcome | Finished successfully, failed, cancelled, or unknown |
| Cleanup state | Owned processes, resources and declared effects are settled or unresolved |
| Continuation authority | Follow-up remains within current user direction and valid admission |

A known exit stays known when cleanup fails. Cleanup resolution does not make a
failed or unknown command successful. A notification carries facts, not permission.

## Notification decisions

These visible labels are backed by exact root/job/turn and current
input/goal checks. Labels alone do not authorize actions.

| Situation | Delivery and root action |
| --- | --- |
| Known result, clean cleanup, live authorized watch | `LIVE COMPLETION`: review and continue, or diagnose the known failure. |
| Known result, unresolved cleanup, live authorized watch | `RECOVERY REQUIRED`: retain the exit/result and investigate cleanup; continue independent work. Do not call the command lost. |
| Unknown outcome, live authorized watch | `RECOVERY REQUIRED`: establish liveness/outcome and effects first; retain affected claims and prevent blind replay. |
| Observer gap or uncertain delivery/admission | `REPORT ONLY`: report and inspect read-only; establish continuity with positive evidence before affected follow-ups. An old registration reason does not grant authority. |
| User stopped, cancelled, paused or superseded the task | Respect that decision; no automatic resumption. The executor may finish its already-authorized cleanup. |
| Necessary action exceeds scope or evidence requires unavailable access | Ask one specific question naming the blocker; continue independent authorized work where possible. |

Classify jobs individually in a mixed batch. A live sibling cannot authorize a
cancelled or uncertain job. Preserve already-submitted callback text and IDs;
reconcile uncertain delivery instead of resending.

## One bounded recovery pass

1. **Check scope.** Read current user direction, goal state and exact admission.
   Existing authority persists within its actual scope; stale revisions/input
   epochs and paused/cancelled work do not authorize mutation.
   **Done:** identify the allowed actions and affected dependency, or name the
   missing authority. Cleanup uncertainty alone is not a request for new permission.

2. **Establish facts.** Inspect the original receipt/logs, exact boot/PID/start
   identities, locks, owned artifacts and declared effects. Recheck disappearing
   processes; classify foreign processes from evidence rather than blanket
   exclusions. Redact credential-bearing arguments and environment values.
   **Done:** distinguish still running, ended with known result, and genuinely
   unknown; list the remaining cleanup obligations.

3. **Recover within scope.** Observe live work or use its already-reviewed
   cancellation/cleanup policy. Independently verify ended-work cleanup. Perform
   authorized ownership-specific cleanup and record supported `job resolve`
   evidence only when every obligation is proven. Retain original results.
   Recovery mutations touch only this job's exact owned identities; foreign jobs,
   users' processes and unrelated tmux sessions/processes are outside this scope.
   **Done:** verified cleanup is recorded, or a specific obligation has an owner
   and bounded observation/escalation path. Keep claims until proof; a missing
   receipt or permission-denied read is not proof of absence.

4. **Resume useful work.** Reuse accepted artifacts under their existing contracts.
   For a known failure, reproduce/fix the cause and create a new reviewed execution
   within existing authority when needed. Successful work with failed cleanup needs
   cleanup recovery, not command replay. For unknown outcomes, establish that
   repeating effects is safe or obtain the missing decision; cleanup alone cannot
   establish this. Preserve and identify both attempts.
   **Done:** the next action has supporting evidence, resource checks and acceptance
   criteria. Publication/install gates and their authorization remain separate.

5. **Wait or escalate without spinning.** Register finite recovery work with the
   existing observer and end the turn while it runs. Another recovery action needs
   meaningful changed evidence, a supported correction or renewed user direction;
   heartbeat timestamps are not new evidence. An unchanged failure does not justify
   another retry.
   **Done:** an actual registered completion can advance the task, or a concise
   blocker names who can supply the missing evidence/decision. No status-only
   model loop or new scheduler is needed.

## Goal and runtime changes

Use existing goal `continue`/`wait` checkpoints for authorized recovery. Reserve
`needs_input` for a real decision or unavailable required authority, with its exact
reason. Preserve pause/edit/clear, input precedence, single-dispatch and successful
end-of-turn rules. Model-written Continue cannot bypass an authority barrier or
reactivate a superseded goal. Non-goal work still needs current root admission.

Implementation and acceptance cover these seams together:

- `background.rs`: retain known command outcomes in `execution_outcome`; distinguish
  live recovery from lost continuation authority. Unresolved cleanup alone should
  not impose a goal renewal barrier.
- `ledger.rs`: derive per-job notifications from outcome, cleanup and delivery
  evidence; preserve deduplication and frozen submissions. A mixed batch must not
  stop independent jobs merely because one requires recovery.
- Goal admission/checkpoints: admit recovery only under the current valid tuple;
  newer input, cancelled watches and ambiguous delivery keep their protections.
- Execution resolution/resource claims: retain proof and ownership checks. A
  diagnostic acknowledgement cannot release reservations. The observer itself
  never retries commands or performs speculative cleanup.
- Instructions/docs: replace blanket stop wording with this workflow. HUD refresh
  remains read-only; this proposal needs no new footer row or configuration switch.

The intended compatibility encoding keeps legacy-readable recovery jobs at
`report=1` and adds a default-zero SQLite `jobs.recovery_allowed` marker. Only
positively live observation can authorize the new recovery path. Old helpers
ignore the marker and remain conservative; legacy/manual/restart barriers retain
marker zero. Execution record JSON remains separate from this ledger addition.
The old-helper behavior, migration and frozen callback cases require actual
qualification; an additive column alone is not compatibility evidence.

## Global instruction activation

The [complete candidate block](proposals/recovery-agents-block.md) is the single
source for `rollout.agents_md`. **Install only with its qualified runtime.**
It replaces the LIVE COMPLETION / REPORT ONLY / uncertain-delivery paragraphs
(live lines 101–107 at fleet review), and explicitly limits recovery mutations to
this job's exact owned processes, files and records. Other roots' jobs, other
users' processes, and unrelated tmux sessions/processes are excluded.

Ship the installed recovery documentation with the decision table and procedure
before activating this block. Keep advisor and unrelated goal text byte-identical
unless implementation review identifies a necessary matching change. The handoff
must contain the complete reviewed block. The Plus-owned goals sentence about
newer user direction, report-only barriers and existing approvals stays unchanged
and remains true: this policy does not override an authority barrier.

## Acceptance cases

| Case | Required result |
| --- | --- |
| Exit 0, cleanup uncertain, live watch | One recovery wake, known exit retained, no replay, resolution only after proof. |
| Exit 1, cleanup uncertain, live watch | Diagnose/clean up without ritual renewal; preserve failure; review any corrected retry. |
| Unknown result with live owned process | Observe/reconcile; no duplicate launch or released claim. |
| Ended command, uncertain external effect | Evidence or a specific decision before repetition; cleanup does not prove replay safe. |
| Nonzero test exit after verified subtree cleanup | Test stays failed; cleanup can be clean; preserve the original diagnostic even if later reporting fails. |
| Foreign job/user/tmux process or unreadable identity | Read-only diagnosis may establish ownership; no mutation, signal, blanket waiver or claim release without this job's exact ownership proof. |
| Repeated identical observation | No duplicate wake/retry/model polling; meaningful changed evidence can advance the task. |
| Mixed live/recovery/cancelled jobs | Independent work proceeds; authority is never borrowed from a sibling. |
| Pause/edit/clear/newer input during recovery | Newer authority wins; late receipts cannot resurrect prior work. |
| Restart/unwatched gap/uncertain callback acknowledgement | Preserve delivery identity; report/reconcile without blind resend or authority renewal. |
| Verified resolved failure | Activity settles, original outcome remains, dependent work uses only verified available resources. |
| Quiet rollout/rollback | Unresolved cleanup still blocks quiet installation; qualify mixed-helper and backward-record behavior. |

Run focused classification/ledger/goal tests, then private official-daemon
success/failure/recovery cases natively on both architectures. Include true unknown
outcomes and superseded authority, not only successful known-exit cleanup. Verify
the instruction/runtime contract and old/new helper behavior together.

## Activation and current release

This implementation changes frozen 0.4.8 source and
requires a new candidate identity and affected qualification. Preserve published
0.4.7 artifacts and original receipts. Activation requirements from fleet review:

1. Fleet-config owns rollout approval, staging, quiet checks and lockstep. In the
   policy rollout commit, its RELEASING.md assigns verified resolution to the job's
   owner within that task's authority. Fleet-config never resolves another root's
   job or waives a failed quiet check. `executions: wait` still blocks unresolved
   cleanup.
2. On each box, install the qualified code first and the exact full block immediately
   afterward, in the same sitting. Never distribute the policy ahead of its code.
   `session_restart: required`: running TUIs retain old code/instructions until
   restart. Qualify the interim helper mix; legacy barriers remain conservative.
3. Quiet rollback restores both the retained package and its previous full block.
   The live 0.4.7 block is byte-identical to the committed
   [0.4.4 block](releases/0.163.0-plus.0.4.4/agents-block.md), SHA256
   `2e5f0b01f3ac15834b8da7c90e436b91e0cc81e3c5ac79d584c2d30603dd8a59`.
   Name this rollback policy in the handoff notes or an agreed artifact field;
   do not add an unreviewed handoff schema key. Qualify 0.4.7 reading new written
   recovery records/events conservatively, both-direction holds, and restored
   policy behavior. Until those gates pass, rollback is unqualified and the floor
   cannot move.
4. Keep Claude rules and configuration unchanged. This rollout applies only to
   Codex Plus roots and does not expand their permissions or job ownership.

For the current 0.4.8 incident, command failure is known and cleanup is unverified.
The intended future behavior is to inspect exact guard/process/resource evidence,
perform authorized verified reconciliation, fix the outdated footer assertion and
failed-stage reporting, apply the private ARM profile correction, and prepare both
targets before any new launch. A policy document or code change alone performs
none of that recovery and does not qualify the release.
