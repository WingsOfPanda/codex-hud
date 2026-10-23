# Recovery within the authorized task

Status: **proposed for 0.4.8; not implemented or active**. This design does not
clear an existing report barrier, qualify a package, or authorize installation.

## Decision

An execution problem puts its dependency into recovery, not the whole task on
hold. The root investigates, performs verified recovery within existing user
authorization, and continues toward the requested outcome. Independent authorized
work continues. Ask the user for a concrete missing decision, access or authority,
not merely because cleanup needs investigation.

Keep three facts separate:

| Fact | Meaning |
| --- | --- |
| Command outcome | Finished successfully, failed, cancelled, or unknown |
| Cleanup state | Owned processes, resources and declared effects are settled or unresolved |
| Continuation authority | Follow-up remains within current user direction and valid admission |

A known exit stays known when cleanup fails. Cleanup resolution does not make a
failed or unknown command successful. A notification carries facts, not permission.

## Notification decisions

These are proposed visible labels, backed by exact root/job/turn and current
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

Review these implementation seams together:

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

Reuse existing records/state where possible. Before implementation freeze, document
any necessary event, ledger or protocol change from actual code. This proposal
does not assert migration-free or mixed-version compatibility.

## Proposed global instruction replacement

Fleet-config should review this replacement for the completion/recovery paragraphs
in the full release-owned AGENTS block. **Draft only; do not edit the live file now.**

```markdown
On `LIVE COMPLETION`, inspect results and continue the authorized task.
On `RECOVERY REQUIRED`, investigate the exact execution and cleanup state,
perform verified recovery within current authorization, and continue independent
work. Preserve failed outcomes and keep resource claims until release is proven.
A retry is a new reviewed execution after diagnosis; never blindly replay an
unknown command or infer cleanup from heartbeat age.

On `REPORT ONLY`, report the evidence and inspect it read-only. Continue only
independent work that is still authorized; resume affected follow-ups after
continuation authority and execution conditions are positively established.
Respect newer stop/pause/cancel instructions and admission/delivery uncertainty.
Ask for a concrete missing decision or authority, not a ritual confirmation.
Legacy report barriers still require explicit renewed user direction.

When only finite recovery work remains, register it with the existing observer
and end the turn. Repeated notifications without changed evidence do not justify
another retry or status-only model turn.
```

Ship the installed recovery documentation with the decision table and procedure
before activating this block. Keep advisor and unrelated goal text byte-identical
unless implementation review identifies a necessary matching change. The handoff
must contain the complete reviewed block, not just this excerpt.

## Acceptance cases

| Case | Required result |
| --- | --- |
| Exit 0, cleanup uncertain, live watch | One recovery wake, known exit retained, no replay, resolution only after proof. |
| Exit 1, cleanup uncertain, live watch | Diagnose/clean up without ritual renewal; preserve failure; review any corrected retry. |
| Unknown result with live owned process | Observe/reconcile; no duplicate launch or released claim. |
| Ended command, uncertain external effect | Evidence or a specific decision before repetition; cleanup does not prove replay safe. |
| Nonzero test exit after verified subtree cleanup | Test stays failed; cleanup can be clean; preserve the original diagnostic even if later reporting fails. |
| Foreign or unreadable process identity | No broad waiver, signal or claim release; bounded investigation with an explicit remaining blocker. |
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

This is a proposal to fleet-config. Implementation changes frozen 0.4.8 source and
requires a new candidate identity and affected qualification. Preserve published
0.4.7 artifacts and original receipts. Fleet-config owns pin/policy rollout and its
approval. Ship code, installed workflow and the exact global block together;
assess TUI/helper restarts and rollback before rollout. Keep legacy barriers
conservative during mixed operation instead of clearing them through new wording.

For the current 0.4.8 incident, command failure is known and cleanup is unverified.
The intended future behavior is to inspect exact guard/process/resource evidence,
perform authorized verified reconciliation, fix the outdated footer assertion and
failed-stage reporting, apply the private ARM profile correction, and prepare both
targets before any new launch. This proposal performs none of those actions and
does not claim the release is qualified.
