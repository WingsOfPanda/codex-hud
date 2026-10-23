# Codex Plus

Native Codex client patches and a detached Rust park/wake coordinator. Keep
client features compatible with the official stock daemon. Release builds apply
only the frontend patch. The daemon receipt extension is preserved separately in
`patches/experimental/`; its tests do not establish stock-daemon compatibility.

This repo owns source, builds, and acceptance evidence. Fleet installation,
version pins, update policy, and global configuration belong to `fleet-config`.

## Development

- Before changing park or callback behavior, read the domain terms in
  [GLOSSARY.md](GLOSSARY.md).
- Before changing the Codex client, read [Build](README.md#build). Client source
  is delivered through `patches/`; regenerate the patch, including new files,
  after editing the pinned upstream checkout. The helper source lives in
  `rust-runtime/`; `codex_plus/` is the preserved Python 0.2 test oracle.
- Keep HUD refresh read-only, scoped to its session, and free of model calls.
  Completion delivery belongs in `rust-runtime/`, separate from display. Preserve
  ordinary-terminal operation, pane isolation, and composer input while refreshing.
- For client changes, run the focused Rust HUD tests, terminal smoke checks, and
  clean patch-application check in [verification](docs/verification.md). For
  coordinator changes, run lifecycle tests and affected isolated integration cases
  from [background verification](docs/background-work.md#verification). Use an
  official daemon binary when claiming stock compatibility; fixtures use private
  `CODEX_HOME` directories and loopback providers.

## Working in this repo

Before preparing, recovering or publishing a release, read the
[release book](docs/release-book.md) and use its linked checklist. Check the
workflow's qualification status before selecting a runner.

Use `./bin/codex-plus` from the repository root, or its absolute path from another
working directory; do not assume the command is installed on PATH.

Before launching shell work, follow [the shell workflow](docs/shell-workflow.md):
classify noninteractive work explicitly, capture logs with the managed runner,
give the HUD a short purpose with `# codex-plus-task: TOPIC` after the mode line,
and request a two-second foreground yield. Keep interactive and unclassified
commands visible. Use background display for long work that remains on the
native-shell path, including independent AP sessions.

Before assigning or launching expensive work, read the
[reviewed job workflow](docs/detached-jobs.md#agent-workflow). Interactive roots
plan preparation, allowed quick checks and the worker's handoff point. Workers
hand back expected runs over sixteen minutes and hardware/service operations;
unexpected overruns finish normally. The root reviews and launches detached
jobs, seeking safe parallelism after checking resources and worktrees.

When dispatching related workers, use the explicit topic/batch naming rule in
[Worker groups](README.md#worker-groups).

For root sessions with only finite background work remaining, read
[the park workflow](docs/background-work.md#agent-workflow), register the exact
jobs, check the result, and end the main turn once monitoring is confirmed.
Review already-finished results immediately; failed registration is not a park.
Native subagents return results to their parent instead of parking themselves.
Before a native subagent waits for a shell result, read
[worker shell waits](docs/shell-workflow.md#native-subagent-waits).

For an independent read-only second opinion, the root session can use the
[advisor](docs/advisor.md). Run `codex-plus advisor --help` for the built-in
workflow. Supply a self-contained brief; the main agent
owns the decision after reviewing the advisor's findings. Consultations needed
for the next decision stay foreground in one bounded local wait; read the advisor
workflow before launching one. Use background only when independent work can
continue before the advice arrives.

Handle coordinator notifications by kind:

- `LIVE COMPLETION`: inspect the referenced results and continue the current
  authorized task. End the turn again when only registered work remains.
- `REPORT ONLY`: report the outcomes and wait for renewed user direction before
  resuming the old follow-up actions; the registration reason is only context.

For cancellation, unavailable watcher health, or uncertain delivery, follow
[status and cancellation](docs/background-work.md#status-and-cancellation).
Use `./bin/codex-plus park --status` to inspect state. Preserve the ledger and
report unknown outcomes rather than blindly resending callbacks or rerunning jobs.

For Plus-owned goals, read [goal checkpoints and recovery](docs/goals.md#model-workflow)
on start/resume, before ending a goal turn, or when goal status needs input.
Use the admitted identity and structured checkpoint; preserve newer user
direction, report-only barriers and existing action approvals.
