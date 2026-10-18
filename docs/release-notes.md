# Codex Plus 0.163.0-plus.0.4.1

This patch release fixes first-turn admission and Ultracode confirmation when
the daemon's history metadata is temporarily unavailable. The helper retries
only the exact unsupported turn/item history errors, up to four reads with
100 ms between attempts. It never resends accepted input or guesses a turn.
Unresolved mode confirmation shows `Ultracode unavailable` with a reason;
an explicit status check can recover once the history is readable. Confirmed
mode snapshots remain unchanged during steering. The frontend patch is unchanged.

It retains reviewed detached jobs, default-off Ultracode,
short shell topics and an optional weekly-reset countdown. It also retains the
parallel MCP/web activity-row fix: original Calling/Browsing rows settle by call
identity, interruption stops pending indicators, and late authoritative results
update retained history.
It retains official OpenAI Codex 0.163.0, upstream commit
`83086188ae0614bd882818ff1aa23f3194962012`, and Rust 1.99.1. Package manifests
declare coordinator protocol 5 and `execution_record_format: 1`. The shared daemon
remains OpenAI's binary; no daemon patch or native-database migration is added.
Source implementation, package qualification, publication and installation are
separate steps.

## Reviewed worker handoff

Native workers prepare scripts, declared inputs, resource effects, result checks
and cleanup for root review before expensive execution. Expected runs over
eight minutes, hardware trials and service operations return to the root before
launch. An allowed short check that overruns finishes in a bounded local wait;
eight minutes is a planning estimate, not an automatic kill or transfer.

The root freezes and reviews declared inputs, then launches a distinct execution
UUID within existing user authorization. Launch checks the exact active root,
effective unrestricted permissions, actual containment and frozen input/goal
binding. Read-only, workspace, managed-network and unknown contexts refuse.
Declared exclusive resource keys coordinate participating roots in one
`CODEX_HOME`; unmanaged workloads still require root planning.

## Durable execution and completion

Each executor stays pinned to its original package and records process identity,
logs, exit/signal and cleanup independently of its completion watch. TUI closure,
goal pause/clear and watch cancellation leave execution alone. Explicit job
cancellation remains available to a host operator while the daemon is down.
Repeated launch requests reconcile the same UUID; uncertain launch/result gaps
produce `unknown` and never permit automatic replay.

Restart continuity targets qualified unrestricted Linux user session/tmux
contexts with the stock daemon. It excludes host reboot and executor failure;
unqualified service/container lifetimes refuse launch. Snapshotting covers
declared inputs, not the whole host or mutable external services. See the
[handoff workflow](detached-jobs.md) for the exact contract and recovery controls.

The existing observer delivers completion under Plus goal/input and REPORT ONLY
barriers, including roots without an attached TUI. Detached jobs use the shared
three-row HUD budget with optional explicit stage text; refresh remains local,
read-only and free of model calls. Plus-owned goals and prior shell/worker/advisor
behavior are retained.

Native background shells also accept a short `# codex-plus-task: TOPIC` header
after their mode marker. The HUD shows this purpose rather than long command
arguments; unlabelled jobs use a compact fallback. Full commands remain in native
history. The topic is supplied at launch and needs no extra model request.

## Ultracode and status line

`/ultracode on|off|status` controls a default-off root mode with phased native
workers, sealed acceptance contracts, root-created worktrees and root-run checks.
These are workflow instructions, not a hard worker sandbox. Mode changes apply
to the next unadmitted turn; steering and compaction preserve the admitted
root/submission/turn/daemon-generation snapshot. The complete original startup
developer text is stored privately and restored on qualified cold loads.

Fresh qualified roots receive the bootstrap; qualified roots can resume it.
Legacy roots cannot acquire the mode merely by resuming: start a fresh qualified
root. The status line places mode beside model and effort, for example
`GPT-6-Astra high ultracode`, without adding a footer row.

Select **Weekly reset** in `/statusline` (`weekly-reset`) to show
`resets in 2d 5h`. Missing timestamps show `reset unknown`; expired ones show
`reset due`. The countdown uses existing quota data and local refresh without
extra model or quota requests.

## Cutover and qualification

The first protocol-3 to protocol-5 rollout requires a quiet checkpoint.
The earlier detached-only protocol-4 candidate was not released. Use
`quiet_checkpoint: true`, `executions: wait`, `session_restart: required` and
`daemon_restart: false` for this combined cutover with the unchanged stock daemon.
Maintenance fences new Plus launch/model admission while existing executors
continue; `admission_quiet` and `execution_quiet` are distinct. Fleet-config must
qualify executor process scans, immutable package retention, quiet checks and
monitor reconnection before rollout. Format 1 alone does not establish live
upgrade compatibility. Every rollback requires quiet coordination; no upgrade
kills or replays a job to pass its gate.

The new TUI must be loaded for these controls and displays. Fleet wrappers must
route both `job` and `ultracode` to the helper. The existing native-goal disabling
requirement remains. Rollback to 0.3.2 after execution or mode rows exist is not
qualified; retain the first qualified combined protocol-5 package as the rollback
floor until a later tested release explicitly retires it.
Fleet installation and global handoff instructions remain separate authorized
work. Voice remains omitted as in prior Plus packages.

Private feature evidence is recorded for [detached jobs](evidence/detached-jobs.json),
[Ultracode](evidence/ultracode.json), [shell topics](evidence/shell-topics.json),
[parallel tool rows](evidence/parallel-tool-rows.json) and
[weekly reset](evidence/weekly-reset.json). These feature checks do not qualify
complete combined archives; the companion release note records final qualification
on each architecture.
Qualification must record exact source/package identities, executed checks,
failures and limits on each target. Private official-daemon fixtures use isolated
homes and loopback providers; mock/prototype checks do not qualify unrun service
configurations, complete archives or upgrade paths. See the
[acceptance gates](detached-jobs.md#acceptance-gates) and
[release handoff](releasing.md#qualification-and-handoff). This candidate note
does not claim publication, installation or completion of unrun qualification.
