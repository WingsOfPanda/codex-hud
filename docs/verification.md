# Native HUD verification

Target: Codex 0.162.1, commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`, Linux x86_64,
Rust 1.99.0. The ordinary installed Codex and global configuration remain intact.

## Checks

From the patched upstream `codex-rs` directory:

```sh
cargo +1.99.0 build --locked -p codex-cli --bin codex --release -j 12
cargo +1.99.0 test --locked -p codex-tui --lib worker_hud --profile dev-small -j 12
```

All 19 focused Rust tests passed. They cover runtime state and
elapsed-time precedence, explicit waiting flags, per-client isolation, old reply
rejection, stale retention, known-zero counts, Unicode/narrow rendering, and
composer editing with both idle and active parent turns. The compiler reports
pre-existing deprecation and macro warnings outside the patch.

Row tests cover the three-row cap and overflow count, completion removal,
empty-strip collapse, task-first truncation, Sol/Astra and actual effort labels,
unknown models, and zero versus unavailable metrics. SQLite tests cover recorded
totals, missing/negative/fork counters, absent or invalid databases, and rejected
writes through the read-only connection. Remote terminal fixtures contain
misleading local counters to verify that those counters are never displayed as
remote worker usage.
Renderer buffer assertions check that the summary has normal brightness and
worker rows use the dim attribute.
Explicit-group checks cover shared topic/batch identity, shrinking active counts,
finished-member usage retention, separate batches, untagged workers, unknown
metrics, mixed settings, and group overflow.

From this repository, the real native launcher can be tested without a model or
production daemon:

```sh
python3 -m venv .venv
.venv/bin/pip install pyte==0.8.2
.venv/bin/python tests/native_smoke.py ./bin/codex-plus --evidence .build/acceptance
```

The fixture serves only loopback WebSocket connections and never forwards model
requests. It creates temporary Codex homes with a dummy credential. Tests exercise
two simultaneous ordinary PTYs and, when tmux is installed, a private tmux server
with two split panes. They verify different worker counts, draft editing while
metadata is delayed, completion removal and promotion, an empty strip after the
last completion, later reactivation, resizing, stale disconnect,
automatic reconnect, and isolation between panes. Every fixture asserts zero
`turn/start` and `turn/steer` requests. The private tmux server is removed after
the test. An injected turn-metadata RPC error must mark counts unavailable and
recover on the next successful snapshot.

The PTY harness uses pyte to interpret the actual screen and answer every cursor
position request, including requests after resize. It waits for the resized
frame before injecting the next draft characters.
The placement assertion checks that the worker strip appears below the shortcuts
footer; it fails against the earlier build that placed the strip above the prompt.

The captured report and terminal screens are in [evidence](evidence/). Complete
RPC captures and runtime logs are local build artifacts under `.build/acceptance`.

The packaged patch applies cleanly to the pinned upstream source and passes a
reverse-application check. External dependency entries in `Cargo.lock` are
identical to upstream. The launcher and build script pass `bash -n`.

## Optimized release

These startup measurements describe the earlier HUD-only release. The current
recovery build and its artifact hashes are recorded in
[wake-recovery.json](evidence/wake-recovery.json).

The optimized release executable passed the native terminal suite independently
before replacing `.build/codex`. A copy of the prior development executable is
retained as `.build/codex.dev-small-backup`. The installed binary is stripped;
the unstripped release output remains in the source build directory.

Paired warm-cache startup measurements used 21 samples per binary and command,
alternating their order. Size fell from 404,797,504 to 282,434,424 bytes (30.23%).
Median `--version` time fell from 26.921 to 11.507 ms; `--help` from 29.178 to
12.107 ms. These results do not measure TUI rendering, model response latency,
cold starts, or token savings. See [release-build.json](evidence/release-build.json),
[release-benchmark.json](evidence/release-benchmark.json), and
[release-acceptance.json](evidence/release-acceptance.json).

## Background recovery

The recovery release passed 64 coordinator tests, 51 native queue/protocol tests,
seven real-daemon crash/restart scenarios, the normal background integration, and
the native HUD smoke suite. The primary agent reran acceptance against the release
candidate. See [wake-recovery.json](evidence/wake-recovery.json) and the
[operational workflow](background-work.md).

The native suite requires `RUST_MIN_STACK=33554432` in this build environment.
The seven scenarios exercise shell loss, child loss, crashes before and after
enqueue, crash after consumption, pending queue persistence/replay, consumed
receipt replay, and cancellation tombstones across restart. Mock providers stay
on loopback and make no real model requests. A separate real-process regression
kills an observer and verifies that its supervisor replaces it.

The stripped recovery binary is 282,616,888 bytes. The prior optimized HUD binary
is retained as `.build/codex.release-before-recovery`. Full recovery additionally
requires activating the patched shared daemon; the running old daemon does not
gain capabilities from a TUI relaunch.

## Limits

- Live workers and the strip's placement were confirmed in the user's pane.
  Local token metadata was checked read-only; automated terminal lifecycle tests
  use controlled fixtures.
- This is a version-specific native build. Existing CLI processes must be
  relaunched to use it, and future Codex releases require rebasing/retesting.
- Counts include only active or waiting direct, non-archived spawned workers.
  At most three group or standalone rows appear, followed by `+N more` when needed.
  Finished workers reduce their group's active count; empty groups disappear.
  A healthy empty strip collapses. Stale/unavailable metadata
  remains visibly uncertain. Long names shorten before metrics; terminal height
  can clip rows. Modal dialogs temporarily replace the ordinary composer area;
  small terminals prioritize it.
- Grouping requires the explicit dispatch-name convention documented in README.
  Existing untagged workers remain separate. Tokens and group elapsed time retain
  finished members; unknown member metrics make the group metric unknown.
- Tokens are the cumulative counter recorded by the local backend. Remote
  backends, missing/error/negative counters, and known forked threads show `?`.
  Fork totals can inherit parent usage. Zero means no usage recorded yet, and
  token totals are not a subscription cost estimate.
- A full refresh is bounded to four seconds. More than 1,000 paginated workers,
  malformed pagination, or unavailable metadata show stale/unavailable counts.
  A failure reading any worker's latest turn also invalidates the whole snapshot;
  partial completion counts are never presented as complete.
- The full upstream test suite was not run. Recovery acceptance covers isolated
  real daemons and durable callback delivery, not exactly-once model/tool execution
  or recovery of shell/worker side effects. Production activation is recorded
  separately. Subscription savings were not measured.

The handoff's original panel suite passes its 11 metadata/display tests here. Its
twelfth test concerns the rejected tmux updater and fails because the transferred
bundle lacks `tmux-status-backup.json`. That prototype remains unmodified and is
not used by this native implementation.
