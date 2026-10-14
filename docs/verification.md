# Native HUD verification

Target: Codex 0.162.1, commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`, Linux x86_64,
Rust 1.99.0. The ordinary installed Codex and global configuration remain intact.

## Frontend release boundary

The default patch contains the HUD, workspace lock reconciliation, and frontend
guards that disable daemon updates from the TUI. The
app-server, protocol, transport, core, queue, state, and thread-store source trees
remain identical to pinned upstream. The receipt extension, including migration
0003 and its TUI request-field adjustment, is preserved in a separate
[experimental patch](experimental-daemon-receipts.md). Applying it after the
frontend patch reconstructs the historical combined source.

`scripts/build-native.sh` compares the source against the exact pinned tree plus
the frontend patch before compilation. It rejects old combined checkouts,
unrelated source edits, and unexpected nonignored files. Move a rejected checkout
aside to preserve it; the build script does not erase local changes.

Run its focused guard checks with:

```sh
python3 -m unittest discover -s tests -p test_build_native.py -v
```

The default recovery integration suite now selects seven stock-daemon scenarios.
Use `--scenario all` only with the historical experimental binary; that group
requires receipt support. Release acceptance must also check that the native
server omits `queueIdempotency`, rejects receipt RPCs, and creates only the two
upstream queue migrations. These checks run in private Codex homes.

The frontend-only release passed 33 HUD tests, seven build-guard checks, 13
recovery CLI selection checks, all 16 terminal smoke stages, and eight integration
cases against its unchanged upstream backend. Its private server omitted the
receipt capability, rejected both receipt methods, and created queue migrations
1 and 2 only. HUD refresh added no model calls; all fixture model requests stayed
on loopback. The primary agent ran these acceptance gates before replacing the
local `.build/codex`; the prior combined binary is preserved at
`.build/codex.before-frontend-release`. The shared official daemon was unchanged.
See [frontend-release.json](evidence/frontend-release.json) for hashes and logs.

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

These startup measurements describe the earlier HUD-only release. The recovery
build is recorded in [wake-recovery.json](evidence/wake-recovery.json); the newer
watcher build is recorded in [watcher-lessons.json](evidence/watcher-lessons.json).
The shell display build is recorded in [shell-hud.json](evidence/shell-hud.json).
The current frontend-only artifact is recorded in
[frontend-release.json](evidence/frontend-release.json).

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

## Historical experimental recovery

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

The stripped experimental recovery binary was 282,616,888 bytes. That receipt
extension is now [excluded from release builds](experimental-daemon-receipts.md).
These historical results do not establish the stock daemon's delivery guarantees
and are not instructions to replace the shared daemon.

## Watcher health and notification modes

The watcher changes passed 92 coordinator tests and 29 focused native HUD tests.
The primary agent also ran normal completion plus seven recovery cases against
the unmodified official 0.162.1 daemon: observer and daemon restarts, already
submitted callbacks, and ambiguous stock enqueue without blind retries. Results
discovered after restart are report-only; work positively observed still running
can later produce a live completion.

The rebuilt client passed 14 terminal smoke stages. They cover local health with
no active workers, waiting/stale/uncertain/report/idle transitions, remote
suppression, parent isolation, and draft editing in narrow terminals. The private
local fixture creates two initial loopback model turns to materialize its roots;
health changes produce zero further model calls or turns.

The short live notification also woke this real session through the stock daemon.
The native patch applies cleanly to the pinned source, and all non-TUI sections
are unchanged. The new local executable is installed at `.build/codex`; the prior
binary is kept as `.build/codex.before-watcher-lessons`. Existing CLI processes
need to be relaunched to display the health line. The shared daemon was untouched.
See [watcher-lessons.json](evidence/watcher-lessons.json) for hashes and reports.

## Background shell HUD

The shell display passed 33 focused native tests and 16 terminal smoke stages.
The original worker and watcher-health behavior is retained. New checks exercise
shell-only and mixed rows, the combined three-row cap, independent metadata
failures, complete pagination, command identity changes, and Unicode/narrow input.

A real finite command was started through a private local TUI and loopback
provider. After its main turn completed, the command remained visible without a
park registration, the prompt stayed editable, and completing the command removed
its row. The fixture used two model requests to start the command and finish the
turn; HUD monitoring added none. A separate health fixture uses two seed turns.
The full suite includes isolated split-tmux tests and remote-parent isolation.

The primary agent reran the native tests and full smoke suite, then separately
rechecked a final readiness-only fixture adjustment. The coordinator source and
its accepted 92-test behavior are unchanged. Native patch changes are confined to
the TUI; the shared stock daemon was not replaced. See
[shell-hud.json](evidence/shell-hud.json), the
[mixed display](evidence/shell-mixed-screen.txt), and the
[idle main turn with a running shell](evidence/local-shell-idle-screen.txt).
The installed client is `.build/codex`, with its predecessor preserved as
`.build/codex.before-shell-hud`. Relaunch the client to display shell rows.

## Managed shell policy

The [shell workflow](shell-workflow.md) is implemented by a private logging runner
and frontend-only rendering. Explicit command markers choose `auto`, `background`,
or `foreground`; unmarked commands keep existing behavior. Automatic mode uses a
local two-second timer, with recorded command duration used for completed replay.
The native protocol is unchanged.

Focused checks:

```sh
python3 -m unittest discover -s tests -p test_runner.py -v
# From the patched upstream codex-rs directory:
RUST_MIN_STACK=33554432 cargo +1.99.0 test --locked -p codex-tui --lib shell_policy --profile dev-small -j 12
```

The real terminal policy fixture uses the same pyte environment as the original
smoke suite. Supply an official binary to include helper/park integration against
the stock daemon, with private homes and loopback providers:

```sh
.venv/bin/python tests/test_shell_policy_integration.py .build/codex \
  --stock-daemon /path/to/official/codex \
  --evidence .build/shell-policy/acceptance
```

Interactive commands must be foreground or unmarked from launch. Stock replay
does not preserve stdin notifications, so a live fallback after input cannot
reliably override an original background marker on a later replay. Unknown
recorded duration keeps an `auto` command visible. Forced process-group kills
can leave partial logs without a final wrapper line; native terminal status is
the result evidence in that case.

Completed rows can remain until the next background-terminal snapshot, normally
about one second. Acceptance waits for canonical command completion before
checking the compact result, then bounds the row-removal wait separately. Stock
Ctrl-C preserves tracked background executions; the termination test uses `/stop`
and verifies the known child/grandchild processes exit and the partial log remains.

The primary agent's final acceptance passed 15 helper tests, 14 native policy
tests, 33 HUD tests, 53 command-lifecycle tests, six historical-transcript tests,
the original 16 terminal stages, and eight new policy cases. The new suite also
verified compact resume, two independent roots, process-group termination, and
one completion wake through an official stock daemon. All provider requests used
loopback fixtures. The installed local binary is `.build/codex`, with its previous
frontend release preserved at `.build/codex.before-shell-policy`. Relaunch the
client to use the policy display. See [shell-policy.json](evidence/shell-policy.json).

## Limits

- Live workers and the strip's placement were confirmed in the user's pane.
  Local token metadata was checked read-only; automated terminal lifecycle tests
  use controlled fixtures.
- This is a version-specific native build. Existing CLI processes must be
  relaunched to use it, and future Codex releases require rebasing/retesting.
- Counts include only active or waiting direct, non-archived spawned workers.
  At most three combined worker-group, standalone-worker, or shell rows appear,
  followed by `+N more` when needed.
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
  real daemons and notification outcomes, not exactly-once model/tool execution
  or recovery of shell/worker side effects. Experimental receipt results are
  identified separately. Subscription savings were not measured.

The handoff's original panel suite passes its 11 metadata/display tests here. Its
twelfth test concerns the rejected tmux updater and fails because the transferred
bundle lacks `tmux-status-backup.json`. That prototype remains unmodified and is
not used by this native implementation.
