## Codex Plus shell work and completion wakes

Apply this workflow in every repository, including independent AP sessions.

For finite noninteractive shell work, capture logs from launch:

```sh
# codex-plus-shell: auto
# codex-plus-task: Verify runtime tests
codex-plus run -- PROGRAM ARGUMENTS
```

Put the mode marker first, then a printable `# codex-plus-task: TOPIC` line
with a three-to-six-word purpose (at most 120 characters). Choose:
- `# codex-plus-shell: auto`: short or uncertain duration; request `yield_time_ms=2000`.
- `# codex-plus-shell: background`: known long work; request a short yield, such as `250`.
- `# codex-plus-shell: foreground`: explicitly requested foreground work.

Use an explicit shell for shell syntax: `codex-plus run -- bash -c 'COMMAND'`.
The runner closes stdin. Keep interactive or unclassified commands in ordinary
visible execution. For persistent services, check readiness with finite work.
On completion, inspect the result and give a concise outcome with its log path.

When a root session has only finite background work remaining, register it:

```sh
codex-plus park --process TOOL_SESSION_ID --worker CHILD_THREAD_ID --reason "Review results and continue the task"
```

Use only applicable options; repeat them for multiple jobs. Process IDs are native
shell-tool session IDs, not OS PIDs. Workers must be direct children, selected by
thread UUID or canonical path. Keep the reason within 240 printable characters.
AP workers finish their brief within the turn; parking is for interactive root sessions.
Native subagents return results to their parent.
Plan native-worker assignments around preparation, quick checks and a clear handoff.
Workers return expected runs over eight minutes, hardware trials, serving experiments
and service restarts before launch. The root reviews and launches them with
[reviewed detached jobs](/home/liupan/.local/share/fleet-config/codex-plus/current/docs/detached-jobs.md#agent-workflow),
using existing authorization and safe parallelism across worktrees/resources.
Eight minutes is an estimate: an allowed quick check that overruns finishes in
one bounded local wait. Before a permitted worker shell wait, read
[worker shell waits](/home/liupan/.local/share/fleet-config/codex-plus/current/docs/shell-workflow.md#native-subagent-waits).
AP workers use native shell work and finish within their turn; managed detached jobs are for interactive roots. Leave `job maintenance
enter/leave` to the host operator.

Confirm jobs are armed or already registered, give a short waiting notice, and
end the turn. Let the local observer monitor them. Review already-finished jobs
immediately. If registration fails, resolve or report the monitoring failure.

On `LIVE COMPLETION`, inspect results and continue the currently authorized task;
end the turn again when only registered work remains. On `REPORT ONLY`, report
outcomes and await renewed direction before resuming old follow-up actions.

For uncertain delivery, lost outcomes, or unavailable watcher health, inspect
`codex-plus park --status`, preserve the ledger, and report what remains unknown.
Use verified evidence before recovery; avoid blind callback resends or job reruns.

## Astra advisor

For an independent second opinion, the main root session may call
`codex-plus advisor --brief TEXT`. Supply the decision, constraints, and review
focus. It uses the newest available Astra at `xhigh` in a read-only sandbox;
treat its findings as advice. For advice needed before the next decision, use
`# codex-plus-shell: foreground` and keep the turn open in one bounded local
Code Mode cell with a 26-minute wait budget and 27-minute outer yield. Inspect
the exit status and JSON before continuing. Use background/park only when independent
work can continue before the advice is needed. Keep the task pending until the
consultation is reviewed.

## Plus-owned goals

For Plus-owned `/goal` work, read
[goal checkpoints and recovery](/home/liupan/.local/share/fleet-config/codex-plus/current/docs/goals.md#model-workflow)
on start/resume, before ending a goal turn, or when goal status needs input.
Use the admitted identity and structured checkpoint; while registered jobs run,
end the turn and let the local observer wake you. Preserve newer user direction,
report-only barriers and existing action approvals.
