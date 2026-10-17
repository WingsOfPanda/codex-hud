# Codex Plus 0.163.0-plus.0.3.0

The installed Plus helper is now Rust. `advisor`, `run`, `park`, `observe`, and
`supervise` use the native `bin/codex-plus-runtime` behind the existing Bash
launcher. Runtime packages contain no Python source, bytecode, interpreter, or
`libpython` dependency. System Python is left installed; packaging and test
fixtures still use Python 3.11+. The source `codex_plus/` tree remains the Python
0.2 reference oracle and is excluded from packages.

This candidate retains OpenAI Codex 0.163.0, commit
`83086188ae0614bd882818ff1aa23f3194962012`, and the frontend patch from merged
main. No daemon source, native protocol, or upstream database migration changes.
The combined release uses independent Plus version `0.3.0`; an upstream-only
update preserves that Plus version.

Builds now pin Rust 1.99.1 to match the approved fleet toolchain. Earlier
implementation evidence built with 1.99.0 is retained as history; it does not
qualify a 1.99.1 release artifact. Complete native-client archives still require
fresh builds and qualification with the new toolchain.

## Preserved behavior

Commands, exit codes, closed runner stdin, raw arguments, logging, cancellation,
root-only Astra advisor selection at xhigh, and read-only advisor isolation keep
their existing contracts. Child launches use kernel execution without an implicit
shell fallback. SQLite is bundled in the helper; only ordinary GNU runtime
libraries are required. Each target must pass native dependency checks.

The ledger path, schema version 1, status JSON version 1, callback identities,
frozen messages, cancellation provenance, and live/report-only recovery policy
remain unchanged. Stock-daemon enqueue acknowledgment ambiguity remains visible
and is not blindly resent. This migration does not promise exactly-once model
or tool execution, reboot persistence, or automatic job reruns.

The existing three-row activity limit remains shared by workers, background
commands, advisor status, alerts, and overflow. Healthy watcher waiting adds no
row. The merged foreground advisor UI is included: a locally timed conversation
row becomes completed or failed after the consult, while the footer retains
model, effort, count, and duration. No UI refresh invokes a model. Native client
sessions must restart to adopt that client display when upgrading from 0.2.0.

## Cutover and rollback

Rust coordinators advertise protocol 2. The first Python-to-Rust rollout requires
a quiet checkpoint: hold new parks and wait for no Python or Rust helpers, armed
parks, or open callbacks. Preserve the ledger; switch the package only after
that check. A TUI restart does not settle pending work or move old observers.
Fleet helper scans must recognize the new `codex-plus-runtime` process roles.
Public wrapper routing remains unchanged.

Compatible, strictly newer protocol-2 packages at the same upstream and target
can subsequently transfer cooperatively. Invalid targets and failed execution
leave the current observer running with a warning. Lower versions are refused.
All rollbacks require a quiet checkpoint. Retain the last qualified Python
package and its interpreter until the Python rollback is retired; ordinary
`previous` retention alone is insufficient after multiple upgrades.

With an existing stock 0.163.0 daemon, this Plus-only update needs no daemon
restart. Fleet-config owns installation, version pins, wrappers, global policy,
retention, and rollback. This source implementation does not install or restart
anything on the fleet. Build, publish, merge, and rollout remain separate gates.

## Package contents and qualification

Both GNU targets preserve upstream layout: native Codex, code-mode host,
digest-matched bubblewrap, ripgrep, metadata, and applicable licenses, plus the
Rust helper and launcher. Codex voice remains excluded. The helper has its own
locked dependency notices, standard-library notices, and source provenance,
covered by the file inventory and manifest. The recorded build glibc is a build
baseline, not a measured minimum supported version.

Tests use private Codex homes and loopback providers. Python may drive tests
outside a Python-free product filesystem. Native x86_64 and aarch64 results,
actual binaries, preserved fixture failures, and explicit limits belong in the
companion note/evidence; a source test does not qualify a complete release
archive. Full release archives and a publishable handoff require their own
per-target build and extracted-package acceptance.
