# Codex Plus 0.163.0-plus.0.3.2

This version adds Plus-owned goals to the Rust coordinator and native client.
It retains OpenAI Codex 0.163.0, upstream commit
`83086188ae0614bd882818ff1aa23f3194962012`, and Rust 1.99.1. The shared daemon
remains OpenAI's binary, without daemon source or native-database migrations.
Implementation and test evidence are separate from publication and installation.

## Plus-owned goals

`/goal` manages a persistent objective for an interactive root session. The
coordinator schedules useful continuation and watches registered jobs locally.
While healthy jobs run, the main thread becomes genuinely Idle and accepts
input, with no repeated model check-ins. Shell/worker completion uses the stock
queue path. A compact goal state uses the existing right input indicator;
worker/job/advisor rows retain their three-row total limit.

Goal identity, revisions, exact-turn checkpoints, input epochs and dispatch
identities live in the existing protected Plus SQLite ledger. Pause, edit and
clear preserve running jobs/results and invalidate future continuation. Missing
checkpoints, repeated lack of evidence or uncertainty require input. Optional
one-hour/two-hour reviews default off, coalesce missed intervals and cannot poll
healthy jobs. Hard time/token budgets are outside this version.

See [the goal workflow](goals.md) for checkpoint, native-goal transition and
recovery semantics. Ending a goal turn is an agent instruction; registering a
job alone does not make the daemon Idle. Progress evidence relevance remains
agent/user judgment. Already-admitted work may execute after pause, including
stock interrupted-turn recovery before Plus reconnects. Queue acknowledgement
loss does not provide exactly-once model/tool side effects.

## Other fixes retained

Standalone worker tokens now match the displayed current turn's elapsed time,
rather than showing lifetime usage beside a short current-turn timer. Explicit
worker groups retain their batch lifetime scope. Exact current-turn process
lookup also precedes bounded older-history fallback when registering shells in
long sessions. Earlier callback/advisor cleanup and HUD fixes are retained.

## Rollout and rollback

The coordinator protocol changes from 2 to 3. A quiet checkpoint, official
daemon restart and TUI restart are required. Fleet-config must preserve settings
while disabling native goals effectively through the supported daemon startup
feature override, and route the `goal` helper command. Merely rebuilding the
client does not enable this workflow. Existing native goals remain stored;
copying a reviewed objective into a paused Plus goal requires explicit consent.

Preserve the last qualified rollback package and ledger. Rollback leaves Plus
goals inert and native goals disabled, and must not expose goal callbacks to an
older helper. It requires quiet coordination; live downgrade is unsupported.
Fleet-config owns the separately coordinated installation.
Voice remains omitted as in prior Plus packages.

## Qualification

See [custom-goals.json](evidence/custom-goals.json) for executed source/development
checks, exact development-artifact hashes, preserved failures and limits. Official-daemon
cases use private homes and loopback providers; wire fault and seeded recovery
cases are distinguished. Read-only/Plan checkpoint writes can be denied and then
require input; the client does not widen sandbox permissions. The companion
release note records native acceptance and exact hashes for both release archives.
