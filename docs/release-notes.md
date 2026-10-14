# Codex Plus 0.162.1-plus.1

First complete GNU Linux package release, based on OpenAI Codex `rust-v0.162.1`
(`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`). The package manifest records the
exact Plus source revision, frontend patch hash, target, and artifact hashes.

The release adds the per-session worker/shell HUD, local watcher health, explicit
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

The source and runtime acceptance evidence accompany the release handoff.
Existing processes keep their old executable and helper source until restarted;
keep previous installations while registered work still uses them.
