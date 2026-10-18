# Plus Ultracode procedure, policy 1

Apply this procedure only when `ultracode status` returns an effective on snapshot
for this exact root turn and daemon generation. Record its revision with the work.
After compaction, read status again; requested state alone never changes the
current turn. Ordinary user direction and goal/report-only barriers take priority.

1. **Plan a bounded phase.** Use the smallest useful understand, change and verify
   phases, with the root deciding between them. Conversational replies and trivial
   mechanical edits stay inline. Research pieces have evidence criteria, not
   invented code tests. Record pieces, blocking edges, ownership and acceptance
   before dispatch. Keep a versioned JSON recovery journal in `journal_directory`:
   schema 1, root, request summary, mode revision, phase, pieces, dependencies,
   source/worktree identities, contracts and baseline, workers, jobs, acceptance,
   uncertainty and next action. Store references to large evidence; exclude secrets.
   This is a recovery record, not instructions to automatically replay work.
2. **Prepare independent acceptance.** For behavior changes, write meaningful
   contract tests and confirm they fail for the intended reason. Record commands,
   baseline test IDs, failures, skips and collected count. Seal contract copies and
   hashes outside worker-owned worktrees. Existing regression tests or a concrete
   acceptance command may be sufficient for other changes. Unavailable hardware
   means pending acceptance. Prepared implementation is not a passed execution.
3. **Prepare workspaces.** Create each piece's worktree at an explicit commit that
   contains its dependencies. Account for relevant dirty user work separately;
   preserve it. Brief workers with absolute paths and tool working directories:
   native children initially inherit the parent's cwd. Keep contracts, fixtures,
   configuration, existing tests and unrelated files protected unless the root has
   explicitly included their changes. Worktrees isolate Git state, not hardware,
   ports, services or databases. Keep them while workers or executions use them.
4. **Dispatch ready pieces.** Use explicit configured Sol model and effort: medium
   for routine bounded work; high for complex implementation, uncertain debugging
   and demanding review. Use a qualified role that preserves requested settings;
   record actual model/effort, since role overrides can replace spawn arguments.
   Give self-contained briefs with `fork_turns="none"`, owned paths, contract,
   constraints and done criteria. Workers are leaves and return design questions
   to the root. Use topic/batch names for the HUD. Respect the shared configured
   child cap and actual remaining capacity; do not create another scheduling loop.
5. **Hand back long executions.** Workers return commands expected to exceed eight
   minutes, hardware trials and service changes before launch. Include exact argv,
   cwd, inputs/environment references, resources/effects, duration, logs, success
   checks and cleanup. Unexpected overruns of permitted short checks may finish in
   one bounded local wait. The root reviews and launches with qualified installed
   `job prepare/launch` within existing permission; safe independent jobs may run
   concurrently. Without those installed commands, report the requested execution
   as pending. Keep execution UUIDs and frozen source identity in the journal.
6. **Wait without model polling.** Once independent root work is exhausted, register
   exact direct-child turns and finite shell jobs with `park`, verify registration,
   give a short notice and end the turn. Per-completion wakes may arrive separately.
   At capacity, leave undispatched pieces pending until a real completion changes
   capacity. Inspect uncertainty and preserve results; ordinary worker-watch expiry
   after 24 hours is local attention, not a guaranteed wake or permission to rearm
   the same expired job. Usage/connection errors do not authorize automatic restart.
7. **Accept at the root, in order.** Verify sealed contract hashes; inspect all
   protected/out-of-scope changes including relevant ignored and untracked files
   before executing tests; run each required contract by path (skips are not passes);
   compare baseline test IDs, failures, skips and counts; verify the piece's committed
   implementation. Worker reports are evidence; the root runs the acceptance gate.
   A deliberately shared bounded checkout may retain an inspected uncommitted diff.
   A wrong contract may be corrected and resealed by the root after rechecking the
   baseline. A failed worker return does not justify weakening acceptance.
8. **Integrate and review.** Merge accepted pieces in dependency order into a task
   integration branch, using `--no-ff --no-commit`; rerun affected contracts and
   regressions before committing the merge. Resolve conflicts or brief a bounded
   repair. Perform final root code review. Publishing, default-branch merging,
   installation and shared configuration retain their permission requirements.
   An advisor is root-only; its advice complements executable evidence.

On restart, reconcile journal references against actual threads, job records,
commits and worktrees. Report-only means report and wait for renewed direction.
On mode off, preserve existing work and completion delivery; use ordinary task
instructions for future work. Neither role labels nor these acceptance checks
are an enforced sandbox against arbitrary code running under the same user.
