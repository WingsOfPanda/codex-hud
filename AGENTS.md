# Codex Plus

Native Codex client patches and a detached Python park/wake coordinator. Keep
client features compatible with the official stock daemon. The combined patch
also contains a staged daemon receipt extension; testing that extension is not
evidence of stock-daemon compatibility.

This repo owns source, builds, and acceptance evidence. Fleet installation,
version pins, update policy, and global configuration belong to `fleet-config`.

## Development

- Before changing park or callback behavior, read the domain terms in
  [GLOSSARY.md](GLOSSARY.md).
- Before changing native code, read [Build](README.md#build). Native source is
  delivered through `patches/`; regenerate the patch, including new files, after
  editing the pinned upstream checkout. Changes left only in an ignored build
  checkout or binary are not a source deliverable.
- Keep HUD refresh read-only, scoped to its session, and free of model calls.
  Completion delivery belongs in `codex_plus/`, separate from display. Preserve
  ordinary-terminal operation, pane isolation, and composer input while refreshing.
- For native changes, run the focused Rust HUD tests, terminal smoke checks, and
  clean patch-application check in [verification](docs/verification.md). For
  coordinator changes, run lifecycle tests and affected isolated integration cases
  from [background verification](docs/background-work.md#verification). Use an
  official daemon binary when claiming stock compatibility; fixtures use private
  `CODEX_HOME` directories and loopback providers.

## Working in this repo

Use `./bin/codex-plus` from the repository root, or its absolute path from another
working directory; do not assume the command is installed on PATH.

When dispatching related workers, use the explicit topic/batch naming rule in
[Worker groups](README.md#worker-groups).

For root sessions with only finite background work remaining, read
[the park workflow](docs/background-work.md#agent-workflow), register the exact
jobs, check the result, and end the main turn once monitoring is confirmed.
Review already-finished results immediately; failed registration is not a park.
Native subagents return results to their parent instead of parking themselves.

Handle coordinator notifications by kind:

- `LIVE COMPLETION`: inspect the referenced results and continue the current
  authorized task. End the turn again when only registered work remains.
- `REPORT ONLY`: report the outcomes and wait for renewed user direction before
  resuming the old follow-up actions; the registration reason is only context.

For cancellation, unavailable watcher health, or uncertain delivery, follow
[status and cancellation](docs/background-work.md#status-and-cancellation).
Use `./bin/codex-plus park --status` to inspect state. Preserve the ledger and
report unknown outcomes rather than blindly resending callbacks or rerunning jobs.
