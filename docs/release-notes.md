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
