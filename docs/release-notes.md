# Codex Plus 0.163.0-plus.0.4.2 candidate

This patch candidate refines the native UI and exact-turn admission readiness.
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

Reviewed detached jobs, default-off Ultracode, shell topics, weekly-reset
countdown, parallel tool activity rows and Plus-owned goals are retained.
HUD refresh stays read-only and makes no model calls. Voice and a Python runtime
remain omitted. No new coordinator protocol or execution-record format is added.

## Qualification and rollout

Complete x86_64 and aarch64 native package qualification is pending. Earlier
feature evidence and tests do not qualify the final source snapshot, complete
archives or mixed 0.4.1/0.4.2 operation. Record final source identity, exact checks,
failures, skipped cases and accepted limits in the
[draft companion note](releases/0.163.0-plus.0.4.2.md).

Planned rollout requires `quiet_checkpoint: true`, `executions: wait`,
`daemon_restart: false` and `session_restart: required` to load the changed UI.
Retain qualified 0.4.1 as the rollback floor, plus all packages referenced by
processes or unresolved executions. Actual forward/rollback compatibility must
be tested; protocol 5 and format 1 alone are insufficient evidence. Preserve
disabled native goals and the existing global AGENTS instructions.

See [release packaging and handoff](releasing.md#qualification-and-handoff) for
per-target requirements. No publication, fleet staging or installation is claimed.
