# Codex Plus 0.163.0-plus.0.1.0

Upstream update based on OpenAI Codex `rust-v0.163.0`
(`83086188ae0614bd882818ff1aa23f3194962012`). The package manifest records the
exact Plus source revision, frontend patch hash, target, and artifact hashes.

Plus has an independent version, `0.1.0`. The combined release name is
`<upstream>-plus.<Plus version>`; an upstream-only update retains the Plus
version, for example `0.164.0-plus.0.1.0`. Earlier published names are unchanged.

This release supersedes `0.162.1-plus.0.1.0` and retains coordinator protocol
1: observers finish their current cycle before transferring to a validated,
strictly newer Plus package selected by the installer's `current` link. Watched
jobs continue and frozen callback identities and messages survive for compatible
helper updates. Protocol, upstream identity and target must match.
Lower, equal or invalid targets are refused with a stable watcher warning;
rollbacks require a quiet checkpoint. Failed exec keeps the old observer working.

Helpers pin their source directory in an internal `--package-root` argument,
preserving the installer's cleanup protection while cwd changes during exec.
The normal `codex`, `codex-plus park` and `codex-plus run` interfaces remain the
same. Transfer uses local checks and no model polling or release downloads.

**Upstream-bump requirement:** installing this release requires a quiet checkpoint
with new parks held and no helpers, armed parks or open callbacks remaining, plus
an official-daemon upgrade/restart through fleet-config. Even protocol-1 helpers
cannot transfer across different upstream identities. Later compatible helper
updates can proceed while watched jobs run. Older 0.0.4 observers also lack the
cooperative protocol; do not use the historical force-stop sidecar.

The wake reliability fixes remain: the Python coordinator preserves parks
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
wrapper. The `/daemon` menu is read-only; both update choices and the new
in-process restart action are blocked. The CLI also rejects any daemon-update
action handed off by the TUI before spawning a process. Fleet-config owns the
official-daemon upgrade to 0.163.0.

The release retains the per-session worker/shell HUD, local watcher health, explicit
park/wake coordination, and managed shell logging with a two-second foreground
display budget. Long managed commands retain compact results on resume. The
frontend patch leaves upstream daemon, protocol, and database implementations
unchanged; the experimental receipt extension is excluded.

## Runtime contents

Packages preserve upstream's canonical runtime layout: the native CLI, code-mode
host, digest-matched bubblewrap helper, bundled ripgrep, and package
metadata. They also contain the Python helper, `codex-plus` launcher, workflow
documentation, license notices, and a per-file integrity manifest.
Upstream 0.163.0 removes bundled zsh and the zsh-fork execution path.

**Codex voice is intentionally excluded.** There is no voice host, voice runtime,
voice subtree, or voice-license bundle. Voice functionality is not supported by
this release. Other runtime resources and their applicable notices remain included.

Targets are `x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu`. The build
glibc recorded in each manifest is a build baseline, not a measured minimum
runtime version. Qualification must run on both native architectures; the
companion release note records actual results and limits. Installers must check
dependencies on the destination host.

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

The Plus ledger schema and callback states are unchanged from 0.0.4. The upstream
upgrade requires the official-daemon restart and quiet checkpoint described above.
A newly selected observer retains the existing recovery policy: reconfirmed
running work can later produce LIVE
COMPLETION; completion discovered during the transfer gap produces REPORT ONLY.
Unavailable terminal evidence and ambiguous delivery remain unresolved rather
than triggering a blind resend. Previously cancelled callbacks stay cancelled.

The release handoff must include source and runtime acceptance evidence.
The native client is rebuilt against 0.163.0. Existing TUI sessions must exit and
resume through the new package to adopt the updated client; new parks use the
installed wrapper's current package.
Keep previous installations while any process still references them. Fleet-config
owns the package switch, compatibility checks, and bootstrap/rollback coordination.
