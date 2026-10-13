# codex-hud

A native worker strip inside each Codex CLI, below its shortcuts footer. Each client
tracks its own parent thread. It works in an ordinary terminal and independently
in split tmux panes, without a companion pane or a tmux status-line change.

The patch targets **Codex 0.162.1**, upstream commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`.

## Run

After building, launch a new session or resume an existing one:

```sh
./bin/codex-hud
./bin/codex-hud resume SESSION_UUID
```

The launcher runs the patched native client. It does not replace `codex` on your
PATH or modify your global configuration. An already running CLI needs to be
exited and resumed with this launcher to display the new strip.

## Build

Requires Git, a native Rust build environment, and Rust 1.99.0:

```sh
rustup toolchain install 1.99.0
./scripts/build-native.sh
```

The script fetches the pinned upstream source into `.build/source`, applies
`patches/codex-0.162.1-worker-hud.patch`, and builds `.build/codex`. It refuses an
unexpected source revision or conflicting patch. The patch also reconciles the
release tag's workspace versions in `Cargo.lock`; external dependency versions
are unchanged. The first build downloads and compiles Codex's dependencies.

Builds use Cargo's optimized release profile (thin LTO and four codegen units).
Installation strips symbols; the original release binary stays in the build
directory. The tested executable shrank from 404.8 MB to 282.4 MB. Paired warm
startup medians improved from 26.9 to 11.5 ms for `--version` and 29.2 to 12.1 ms
for `--help` (21 samples each). This does not measure UI or model-response speed.

## Display behavior

The strip shows active and waiting direct workers, up to three display rows:

```text
integration probe · active · 3:42 · 123.4K tok · Sol high
```

Rows start with a short task name, followed by state, elapsed time, cumulative
reported tokens, model, and actual reasoning effort. Long names shorten to leave
room for metrics. The summary uses normal brightness; worker rows are dim. Sol
and Astra are shown by family; other model identifiers remain visible. Unknown
values, including missing effort, show `?`. Additional groups or standalone rows
appear as `+N more`. Finished,
interrupted, failed, and idle workers disappear; the strip hides when no workers
are active or waiting. Small terminals clip rows to preserve the prompt.

Elapsed time uses the latest turn's reliable timestamps. Explicit approval/input
flags produce waiting labels. An active runtime overrides an older completed
turn. Thinking and tool execution are not separately identifiable from this
metadata.

Read-only `thread/list` and `thread/turns/list` requests run locally in the
background. Turn item bodies are not requested. A local timer refreshes the UI;
it never starts, steers, or queues a model turn. A stalled or disconnected
metadata source shows unavailable counts and stale cached rows. Refresh does not
capture keyboard input.

Token totals use the local backend's existing SQLite metadata through a read-only
connection, respecting its configured SQLite directory. They reflect recorded
usage updates, not a per-token streaming counter or a subscription cost. Zero
means no usage has been recorded yet. Remote backends, unavailable counters, and
known forked threads show `?`; fork totals can include inherited parent usage.
The worker HUD itself needs no database migration or daemon restart. Full
[background crash recovery](docs/background-work.md#patched-daemon-and-compatibility)
requires the patched daemon and its queue receipt migration.

## Worker groups

Give workers dispatched for the same purpose and batch a shared explicit label
in their `task_name`:

```text
hud_styling__b1__render
hud_styling__b1__test
hud_styling__b1__review
```

The format is `<topic>__<batch>__<member>`, with exactly three nonempty parts.
Use lowercase letters, digits, and single underscores within each part. Use a
fresh batch value for a new dispatch of that topic; keep it for follow-ups that
belong to the existing batch. Each member name remains unique.

These names produce one row:

```text
hud styling (3 agents) · active · 1:20 · 250K tok · Sol high
```

The count includes only active/waiting members: 3 becomes 2, then 1, and the group
disappears at zero. Tokens include recorded usage from all group members,
including finished members, so completion does not reduce the total. Group
elapsed time starts at the earliest member's creation; standalone rows keep their
latest-turn elapsed time. Missing metrics remain unknown. Model and effort come
from the remaining active/waiting members; differing known settings show `mixed`.

Different batches and parent threads stay separate. Untagged or malformed names
remain standalone rows. Grouping never guesses purpose from timing or similar
names and makes no model calls. The summary still counts individual agents;
the three-row limit and `+N more` count groups and standalone rows.

## Background completion

The [park workflow](docs/background-work.md) lets the main agent end its turn
while specific workers or finite background shell commands run. A local observer
wakes the idle parent on verified completion without periodic model polling.
The main agent must register the jobs and finish its turn; this does not pretend
that an active synchronous tool call is idle. The observer is separate from HUD
rendering. Full crash recovery uses durable receipts in the patched daemon,
supervised observers, and explicit recovery of registered jobs after a daemon
restart. Older daemons retain conservative delivery handling; see the workflow
for activation and compatibility limits.

## Verification

See [verification.md](docs/verification.md) for commands, measured results, and
remaining limits. Earlier 910b prototypes remain in the local, unversioned
`handoff/` archive; they are not part of this source snapshot or installed by
the launcher.

The native patch is version-specific. Updating the ordinary Codex installation
does not update this build; rebase and retest the patch before changing its
upstream version. Upstream Codex is [Apache-2.0 licensed](https://github.com/openai/codex/blob/rust-v0.162.1/LICENSE).
