# Codex Plus 0.162.1-plus.4

Wake reliability release, based on OpenAI Codex `rust-v0.162.1`
(`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`). The package manifest records the
exact Plus source revision, frontend patch hash, target, and artifact hashes.

This release supersedes `0.162.1-plus.3`. The Python coordinator preserves parks
and watched workers when the stock daemon briefly projects a normal completion
as an interruption. Durable uncertainty gates prevent premature cancellation or
delivery; bounded exact-turn evidence resolves the hold. Verified daemon loss
still produces an UNKNOWN report without rerunning work. Cancellation records
its time, cause, and evidence. Existing cancelled jobs remain cancelled.

The compact footer from plus.3 remains intact. Worker rows start with model, role, and
effort; root shell commands use the `Background` label. Redundant count headers
and healthy watcher-waiting rows are hidden. Approval/input waits, stale data,
and wake-delivery problems remain visible. All activity, alerts, and overflow
share three total rows, keeping the idle composer within AP's last six nonempty
lines. Group counts and metrics retain their existing meaning.

The daemon-boundary fixes from plus.2 remain intact. The launcher preserves
the caller's PATH, so commands inside `codex-plus run` use the fleet's `codex`
wrapper. The `/daemon` menu is read-only; both update choices are removed, and
the CLI rejects any daemon-update action handed off by the TUI before spawning
a process. Upstream remains at 0.162.1; no daemon replacement is required.

The release retains the per-session worker/shell HUD, local watcher health, explicit
park/wake coordination, and managed shell logging with a two-second foreground
display budget. Long managed commands retain compact results on resume. The
frontend patch leaves upstream daemon, protocol, and database implementations
unchanged; the experimental receipt extension is excluded.

## Runtime contents

Packages preserve upstream's canonical runtime layout: the native CLI, code-mode
host, digest-matched bubblewrap helper, bundled ripgrep and zsh, and package
metadata. They also contain the Python helper, `codex-plus` launcher, workflow
documentation, license notices, and a per-file integrity manifest.

**Codex voice is intentionally excluded.** There is no voice host, voice runtime,
voice subtree, or voice-license bundle. Voice functionality is not supported by
this release. Other runtime resources and their applicable notices remain included.

Targets are `x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu`. The build
glibc recorded in each manifest is a build baseline, not a measured minimum
runtime version. Qualification is on the Ubuntu 24.04/glibc 2.39 fleet; installers
must check dependencies on the destination host.

## Installation and behavior

Fleet-config owns installation, version pins, PATH wrappers, global workflow
instructions, and rollback. Building or extracting this package does not replace
the system's `codex` command or install/restart a shared daemon. The intended fleet
entry point remains `codex`; `codex-plus` exposes the helper and Plus release
identity, and official daemon maintenance uses a preserved stock CLI.

The Python helper needs Python 3. Native background commands are identified by
their Codex session/item IDs, not arbitrary operating-system PIDs. Agents must
register finite remaining work and end their turn to become idle. Stock delivery
ambiguity remains visible and is not blindly retried. Log retention is manual.
Ctrl-C interrupts the turn; `/stop` terminates its background terminals.

The coordinator ledger gains additive pending-state and cancellation-provenance
columns. No native daemon migration or replacement is needed. Already running
observers retain their old helper code; deployment and rollback must account for
them because older helpers do not enforce the new gates. Unavailable or out-of-
bounds terminal evidence remains visibly unresolved. The active GLM registration
and previously cancelled callbacks are not repaired or rearmed by this update.

The source and runtime acceptance evidence accompany the release handoff.
Existing processes keep their old executable and helper source until restarted;
keep previous installations while registered work still uses them.
