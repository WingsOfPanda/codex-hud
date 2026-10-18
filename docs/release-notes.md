# Codex Plus 0.163.0-plus.0.4.0 candidate

This candidate adds reviewed detached jobs to the Rust helper and native HUD.
It retains official OpenAI Codex 0.163.0, upstream commit
`83086188ae0614bd882818ff1aa23f3194962012`, and Rust 1.99.1. Package manifests
declare coordinator protocol 4 and `execution_record_format: 1`. The shared daemon
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

## Cutover and qualification

The first protocol-3 to protocol-4 rollout requires a quiet checkpoint.
Maintenance fences new Plus launch/model admission while existing executors
continue; `admission_quiet` and `execution_quiet` are distinct. Fleet-config must
qualify executor process scans, immutable package retention, quiet checks and
monitor reconnection before rollout. Format 1 alone does not establish live
upgrade compatibility. Every rollback requires quiet coordination; no upgrade
kills or replays a job to pass its gate.

The new TUI must be loaded for detached-job display. The existing native-goal
disabling requirement remains; stock daemon restart needs its own assessment.
Fleet installation and global handoff instructions remain separate authorized
work. Voice remains omitted as in prior Plus packages.

Qualification must record exact source/package identities, executed checks,
failures and limits on each target. Private official-daemon fixtures use isolated
homes and loopback providers; mock/prototype checks do not qualify unrun service
configurations, complete archives or upgrade paths. See the
[acceptance gates](detached-jobs.md#acceptance-gates) and
[release handoff](releasing.md#qualification-and-handoff). This candidate note
does not claim publication, installation or completion of unrun qualification.
