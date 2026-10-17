# Codex 0.163.0 compatibility assessment

2026-10-16. Assessment only; no port, compilation, binary execution, package
assembly, publication, merge or installation was performed.

**Status: blocked for rollout.** Codex Plus needs a frontend/packaging port and
fresh qualification. No source finding warrants skipping this version outright.
The intended upstream-only candidate is `0.163.0-plus.0.1.0`, based on the
published Plus 0.1.0 source, preserving the current Plus behavior.

Baseline upstream: `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d` (0.162.1).
Candidate: `83086188ae0614bd882818ff1aa23f3194962012` (0.163.0 peeled commit).
The annotated tag object `129b8ec7ee6f4511557a4856279f529025c25af4` is not the
source commit. Current Plus release source/evidence is at `f8d277c`; its frozen
package source is `bb8af5ff64e13953e77969f62da16b9b2f3278a7`.

## Required changes

1. **Port the frontend patch.** Independent non-mutating checks reject 11 of
   33 files, or 13 individual hunks out of 219. Conflicts cover Cargo.lock,
   event dispatch, session lifecycle, chatwidget, daemon menu/tests and five
   snapshot deletions. Regenerate the workspace-version reconciliation against
   the new lockfile; otherwise applicable old hunks would still write 0.162.1.
   Preserve new retained-side-thread behavior instead of copying old context.
   [Source comparison](https://github.com/openai/codex/compare/rust-v0.162.1...rust-v0.163.0),
   [workspace identity](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/Cargo.toml#L161-L168).
2. **Block the new in-process daemon restart path.** `RunDaemonRestart` calls
   the daemon lifecycle code directly; it bypasses both the PATH wrapper and
   Plus's old CLI update guard. Keep the Plus menu read-only and guard the new
   confirmation/restart events, including direct-event tests with private stubs.
   [Restart dispatch](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app/event_dispatch.rs#L162-L176).
3. **Remove bundled-zsh assumptions.** Upstream no longer ships `codex-zsh` or
   its resource subtree. Our current provenance function fails on that missing
   manifest for both GNU targets. Remove zsh executable, download, license and
   qualification requirements; update upstream pins, literal tag expectations
   and the patch filename. Canonical layout version 1 and GNU x86/ARM targets
   remain supported. V8/ripgrep manifests and upstream toolchain pin are unchanged;
   no forced change to our explicit Rust 1.99 build policy was identified.
   [Canonical layout](https://github.com/openai/codex/blob/rust-v0.163.0/scripts/codex_package/layout.py#L33-L86),
   [required files](https://github.com/openai/codex/blob/rust-v0.163.0/scripts/codex_package/layout.py#L126-L154).

The experimental daemon receipt patch remains excluded, regardless of whether
it applies cleanly. This assessment does not justify changing the stock daemon.

## Coordinator and behavioral qualification

No Python protocol change is justified by the source comparison so far. The
used queue request shapes/handler, initialization, item/usage metadata and Unix
transport remain compatible. New thread read-state fields are additive and are
not callback-delivery receipts. The official daemon still lacks our experimental
idempotency/receipt extension, so ambiguous delivery must remain conservative.
[Queue implementation](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/app-server/src/request_processors/thread_queue_processor.rs),
[thread protocol](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs).

Source compatibility is not runtime qualification. In addition to the existing
unit, 11 interruption, seven stock recovery and nine cooperative scenarios:

- Exercise pending and yielded Code Mode shell work during interruption.
  `instant_interrupt` was already true in our 0.162.1 baseline; the relevant
  default change is `code_mode_interrupt`, false to true. Verify actual outcomes
  and cancellation ownership; do not treat missing jobs as successfully stopped.
  [Old defaults](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/features/src/lib.rs#L1152-L1163),
  [new defaults](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/features/src/lib.rs#L1195-L1212).
- Exercise a completed MAv2 worker evicted before observation, then rehydrated
  and reused with a new turn. Preserve exact thread/turn ownership and terminal
  evidence. [Residency](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core/src/agent/control/residency.rs#L35-L108).
- Rerun native HUD and shell acceptance with footer selection, `/config`, new
  status styling and terminal program status. Preserve the three-row total cap,
  AP's prompt within the last six nonempty lines, editable composer, pane
  isolation, compact shell history and zero model calls for display refresh.
  Clean patch application of these hooks does not establish rendered correctness.
  [Footer routing](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app/footer_selection.rs),
  [terminal program status](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/terminal_program_status.rs).
- Build fresh complete native packages on x86 and ARM, qualify against official
  stock 0.163.0 in private homes, and verify stdio app-server/Code Mode, wrapper
  routing, shell-policy behavior and AP readiness. Previous native executables
  cannot be reused across this upstream change. Keep voice excluded.

`app-server-daemon/src/settings.rs` is the same blob at both tags
(`49d89cfaba2dfece3aac1998ad48cb9b114d9abe`); `updater.autoUpdateEnabled` remains
supported. No removed zsh-fork configuration key was found in the local fleet
settings or local Codex configuration checked during assessment.

## Claude Code 2.1.301

No direct Codex Plus change is identified solely for the Claude Code update.
The installed `openai-codex` plugin still launches `codex app-server` over stdio;
advisor-router uses the same invocation. The fleet wrapper routes that command
to Plus, and the wrapper contract is unchanged. Claude's announced changes do
not identify a changed Codex interface. This is a source/interface assessment,
not an end-to-end Claude 2.1.301 runtime test; plugin/mod owners retain their
separate acceptance responsibility.
[Claude release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.301).

Local call sites inspected:
`~/.claude/plugins/cache/openai-codex/codex/1.0.6/scripts/lib/app-server.mjs:189-229`,
`~/Aerius/claude-mods/advisor-router/bin/codex-weekly.mjs:11`, and
`~/.local/bin/codex`. No changes to those files were made.

## Rollout requirements and gate

The upstream bump requires `daemon_restart: true`, `quiet_checkpoint: true`,
`session_restart: required`. No global park/shell Markdown change has been
identified (`agents_md: null`, subject to acceptance). Coordinator protocol 1
may remain if the contract stays unchanged, but the full upstream identity
check deliberately forbids cooperative transfer from 0.162.1 to 0.163.0.

Fleet-config must move the stock CLI, stock daemon and matching Plus package
together at a quiet time, including active turns outside park registrations.
Rollback requires the previous stock CLI/daemon pair and evidence that the old
daemon can open the newer state; that compatibility has not been verified here.
The weekly earliest dates supplied by fleet-config remain Codex 2026-10-19 and
Claude 2026-10-21; they do not constitute approval.

The explicit build gate in fleet-config `codex-plus/RELEASING.md` and the relay
requires the user's go before compilation/qualification. Publish, merge and
rollout are separate gates. No ready tag exists for this candidate.

## Evidence and sources

Primary source readers saved detailed local notes in
`.build/upstream-0163/{tui,packaging,protocol}.md`; the detached candidate tree is
`.build/upstream-0163/source`. The original patched source checkout is preserved.
Root independently reran the 33-file/219-hunk check (log
`ebda7f9a-7635-4690-bf78-4c07c65da860.log`) and reproduced the removed-zsh failure
for both targets (`93e83ef6-d40a-41ec-9133-33ea3d669599.log`). Logs are under
`~/.codex/codex-plus/logs/`. Protocol inspection checked ten unchanged files and
receipt absence; no new daemon was executed.

Initial discovery used web search, Tavily and AnySearch together. Tavily returned
no matching results; search snippets about older alpha issues were not treated as
stable-release evidence. Claims above use the actual release pages and tagged
source. Sources: [Codex release](https://github.com/openai/codex/releases/tag/rust-v0.163.0),
[exact Codex source](https://github.com/openai/codex/tree/83086188ae0614bd882818ff1aa23f3194962012),
[Claude release](https://github.com/anthropics/claude-code/releases/tag/v2.1.301),
and the specific source links beside each finding.
