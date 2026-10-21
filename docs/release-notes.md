# Codex Plus 0.163.0-plus.0.4.5

This release adds build-time preflight and native-test result validation tools,
plus the measured release-performance guide. Preflight verifies selected Python
imports, staged input hashes, raw Unix socket path budgets and package archive
integrity. Native result validation uses dedicated libtest records, the selected
test list, captured exit status and exact ignored reasons instead of terminal
rendering. These Python tools run in the source checkout; the installed runtime
remains native and does not acquire a Python dependency.

The frontend patch, runtime implementation, advisor defaults, coordinator
protocol 5, execution format 1 and official Codex 0.163.0 daemon are unchanged.
The helper is rebuilt with the new package version. The existing global agent
instructions remain unchanged. No shared-daemon restart is needed.

The release run measures stage durations and records reused frontend artifacts
separately from fresh build and package checks. It is a helper-only release
measurement, not a cold frontend build or a controlled CPU-scaling benchmark.
See [the release companion](releases/0.163.0-plus.0.4.5.md) for qualification and
[the performance guide](release-performance.md) for the workflow and its limits.

# Codex Plus 0.163.0-plus.0.4.4

The default advisor is Claude Opus 5.5 at `xhigh`, run on the central host
`xjp-l20-2` through existing passwordless SSH, or locally when already on that
host. Each consultation is a fresh process with tools disabled and receives
only the brief, bounded Git metadata and explicitly attached context files.
The previous native Codex advisor is available through `--provider astra`;
there is no automatic model fallback or automatic retry.

`codex-plus advisor --help` contains the complete workflow. The new global
Advisor section points to that command and ships with the package, not before it.
Native status and transcript rendering accept the exact Claude model, display
`Opus 5.5 (xhigh)` after status is available, and recognize routing/context
options. Before the first call, readiness is neutral rather than inferred from
the Codex model catalog.

Protocol 5, execution-record format 1, and the official Codex 0.163.0 daemon
remain unchanged. No daemon restart or new SSH key is required. Native package
qualification, quiet rollback to 0.4.3, and both-direction maintenance holds
are release gates, not established by the development trials alone.
See [the source release note](releases/0.163.0-plus.0.4.4.md).

Install the central host before other callers. Drain advisor consultations on
both callers and the central host before switching packages. Restart TUIs to
load the Claude-aware frontend and global instructions. A running 0.4.3 TUI
does not understand Claude advisor status, even though shell commands may
already resolve to a newer helper. The qualified companion and handoff will
state the exact compatibility evidence and any remaining limits.

# Codex Plus 0.163.0-plus.0.4.3

Workers hand back commands expected to exceed sixteen minutes before launch.
Hardware trials, serving experiments and service changes still return to the
main session regardless of duration. This is a planning estimate, not a timeout.

The candidate reserves up to six activity rows, keeping at least one worker,
native shell and detached job visible when each exists. Warning, Advisor and
overflow remain distinct, and typing no longer shrinks the activity budget.
Automatic queue/shortcut/Escape hints are hidden while their keys, explicit help,
search, warnings and the working `esc to interrupt` indicator remain available.
With two status rows, the normal maximum is eight rows below the prompt. Fleet
rollout requires AP's cursor/busy-indicator integration instead of the previous
six-line readiness detector. Scoped Linux x86 acceptance is recorded in
[candidate evidence](evidence/ui-043.json). ARM and complete-package qualification
remain pending; published 0.4.2 evidence below is historical.
The generic background-availability banner is hidden; identified unknown/stale
jobs retain their labels, and execution/cleanup records remain authoritative.

The candidate also refreshes Ultracode status on local warm resume, retries
briefly empty initial item history within the existing bounded retry budget,
and retries the exact owned empty-rollout error during accepted-input settlement
without replaying input. Plus goals align to the upper-right above input, and
Git branches use a warm color distinct from the blue directory.

# Codex Plus 0.163.0-plus.0.4.2

This patch refines the native UI and exact-turn admission readiness.
It retains official Codex 0.163.0, upstream commit
`83086188ae0614bd882818ff1aa23f3194962012`, Rust 1.99.1, coordinator protocol 5
and `execution_record_format: 1`. The shared daemon remains OpenAI's binary.

## Changes

- Wait up to two seconds for an already admitted exact turn to become visible
  in history, including temporary missing-turn results and the exact unsupported
  history error. Never resend accepted input or infer another turn's admission.
- Show worker roles beside model and effort. Explicit groups show their shared
  role, `mixed` for differing known roles or `general` for missing role metadata.
- Split selected status items into identity and statistics rows, keeping
  model/Ultracode, directory and active agent separate from context and quota data.
- Give displayed status items distinct colors when status colors are enabled.
- Place Plus goal status in one blue row above the editable input. Status and
  worker rows remain below input; terminals without color retain readable text.
- Keep the prompt within AP's last six nonempty pane lines. With two status
  rows, shortcut and history-search hints temporarily reduce the activity strip
  to two rows, preserving the hidden activity count. Normal activity keeps its
  three-row limit.

Reviewed detached jobs, default-off Ultracode, shell topics, weekly-reset
countdown, parallel tool activity rows and Plus-owned goals are retained.
HUD refresh stays read-only and makes no model calls. Voice and a Python runtime
remain omitted. No new coordinator protocol or execution-record format is added.
Existing `tui.status_line` and `tui.status_line_use_colors` configuration remains
valid without changes; the new layout and palette introduce no config key.

## Qualification and rollout

Publication requires complete x86_64 and aarch64 native package qualification.
Earlier feature evidence does not qualify final archives or mixed 0.4.1/0.4.2
operation. The matching release handoff records final source and archive
identities, executed checks, failures, skipped cases and accepted limits.
The [source companion note](releases/0.163.0-plus.0.4.2.md) describes the scope
and qualification requirements; it is not an archive acceptance receipt.

Rollout requires `quiet_checkpoint: true`, `executions: wait`,
`daemon_restart: false` and `session_restart: required` to load the changed UI.
Retain qualified 0.4.1 as the rollback floor, plus all packages referenced by
processes or unresolved executions. Setting `retain` to
`["0.163.0-plus.0.4.1"]` makes 0.4.0 removable once no process or unresolved
execution references it. Actual forward/rollback compatibility must
be tested; protocol 5 and format 1 alone are insufficient evidence. Preserve
disabled native goals and the existing global AGENTS instructions.

Restart loads the new frontend and its adjacent helper. An older running TUI
keeps its old admission timing and reporting behavior even after the shell
wrapper selects 0.4.2. The final handoff must distinguish qualified 0.4.0/0.4.2
and 0.4.1/0.4.2 helper interoperability from simultaneous native TUI testing;
old 0.3.x mixes remain unqualified.

See [release packaging and handoff](releasing.md#qualification-and-handoff) for
per-target requirements. No publication, fleet staging or installation is claimed.
