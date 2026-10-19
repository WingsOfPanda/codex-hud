# Codex Plus

Codex Plus combines the native Codex CLI, a worker HUD below its prompt
footer, and a `park` helper for background completion callbacks. Each client
tracks its own parent thread in ordinary terminals and split tmux panes.
Release builds patch the frontend and keep upstream daemon code unchanged.
The HUD and Rust completion coordinator work with the official stock daemon.

The 0.163.0-plus.0.4.2 update refines the native status footer, worker role
prefixes and status colors, places Plus goal status above input, and fixes
exact-turn admission readiness. See the
[release note](docs/releases/0.163.0-plus.0.4.2.md); native archive qualification
and identities are recorded separately in the release handoff.
It retains [reviewed detached jobs](docs/detached-jobs.md):
workers prepare expensive work, the root reviews and launches it, and local
executors preserve results across qualified TUI and stock-daemon restarts.
It retains [Plus-owned goals](docs/goals.md) and genuine Idle waiting. Plus goal
status appears in one blue row above the editable input; status and worker rows
remain below it. Terminals without color use readable plain text.
This checkout is not a fleet installation; fleet-config owns the quiet initial
protocol transition and later qualified upgrades.

The release includes default-off [Ultracode mode](docs/ultracode.md) for
root-led worker phases, short shell topics, the parallel MCP/web activity-row fix
and the optional weekly-reset countdown. It uses coordinator protocol 5 and
execution record format 1. Private feature qualification does not establish
complete archive acceptance; the companion release note records qualification
on each architecture.
The initial protocol-3 to protocol-5 fleet cutover requires quiet coordination
and a TUI restart, preserving the stock daemon and disabled native goals.

The patch targets **Codex 0.163.0**, upstream commit
`83086188ae0614bd882818ff1aa23f3194962012`.

## Run

After building, launch a new session or resume an existing one:

```sh
./bin/codex-plus
./bin/codex-plus resume SESSION_UUID
```

The launcher runs the patched native client. It does not replace `codex` on your
PATH or modify your global configuration. An already running CLI needs to be
exited and resumed with this launcher to display the new strip.

## Build

Requires Git, Python 3.11+, Rust 1.99.1, a C toolchain, pkg-config and OpenSSL
development headers. Linux sandboxed commands also require a compatible system
`bwrap` on PATH (its help must list `--as-pid-1` and `--perms`); the developer
build does not bundle sandbox or ripgrep resources. Install ripgrep on PATH for
search tools. Complete release packages supply these resources.

```sh
rustup toolchain install 1.99.1
./scripts/build-native.sh
```

The script fetches the pinned upstream source into `.build/source`, applies
`patches/codex-0.163.0-worker-hud.patch`, and builds `.build/codex`, the matching
`.build/codex-code-mode-host` beside it, and `.build/codex-plus-runtime`. Code Mode
requires that adjacent host. It uses upstream's pinned, checksum-verified V8
archive/bindings resolver and download cache. It refuses an
unexpected source revision or a checkout that differs from the exact release
patch, including old daemon modifications. The patch also reconciles the
release tag's workspace versions in `Cargo.lock`; external dependency versions
match the 0.163.0 tag. The first build downloads and compiles Codex's dependencies.
Preserve or move aside an existing checkout at another upstream revision before
building; the script refuses to overwrite it.

The client build explicitly targets the pinned toolchain's native host, overriding
Cargo's configured build target so its V8 artifacts match. `CARGO_TARGET_DIR`
is preserved, including relative paths and spaces; using an explicit native
target may populate a new subdirectory in an existing cache. Both scripts select
executables from successful Cargo build messages, including warm builds, instead
of guessing output paths. The helper-only build honors Cargo's configured target.

The historical receipt extension is preserved in
`patches/experimental/codex-0.162.1-daemon-receipts.patch` and is excluded from the
build script. See [experimental receipts](docs/experimental-daemon-receipts.md)
for its historical 0.162.1 scope and patch order. It is not ported to 0.163.0.

Builds use Cargo's optimized release profile (thin LTO and four codegen units).
Installation strips symbols; the original release binary stays in the build
directory. Historical 0.162.1 measurements showed the executable shrinking from
404.8 MB to 282.4 MB. Paired warm startup medians improved from 26.9 to 11.5 ms
for `--version` and 29.2 to 12.1 ms for `--help` (21 samples each). These do not
measure 0.163.0, UI, or model-response speed.

For complete distributable packages, follow [release building](docs/releasing.md).
The packager stages native x86_64 and ARM64 outputs with upstream runtime resources,
the native Rust helper, workflow docs, license notices, and integrity manifests. This
is separate from the developer binary above. Codex voice is intentionally omitted;
see the [release notes](docs/release-notes.md).

For helper-only development, run `./scripts/build-runtime.sh`. Helper source is
in `rust-runtime/`; `codex_plus/` remains the Python 0.2 reference for migration
tests and is excluded from packages. Installed commands use Bash and native
executables, with bundled SQLite and no Python interpreter or `libpython`.
Python 3.11+ remains a build/test dependency for packaging and fixtures.
The one-time Python-to-Rust rollout needs a
[quiet checkpoint](docs/background-work.md#cooperative-package-upgrades).

## Display behavior

The status footer puts model, the saved `ultracode on`/`ultracode off` setting,
working directory and active agent on the first row. Selected **Context remaining**,
**Weekly limit** and **Weekly reset**
items appear on a second row, retaining their configured order and styling.
When status line colors are enabled, model and Ultracode use cyan, directory
blue, branch peach, context green, weekly remaining yellow, weekly reset purple, and PR number
red. Active agent and separators stay neutral. Colors adapt to terminal contrast
and supported depth; other selected items retain theme accents.
The worker HUD follows both rows. A selection with only stats uses one row when
there is no agent label or readable mode projection; selections without stats
keep one row.
The activity strip uses up to six rows independently of the status footer.
Warnings, Advisor, worker groups, native shells, detached jobs and overflow share
this budget. Every nonempty activity category keeps at least one row, including
while typing or displaying a warning and Advisor. Advisor and overflow remain
separate rows. Two status rows plus six activity rows give at most eight nonempty
rows below the prompt; explicitly opened help/search can add a close/search row.
Small terminals may clip rows to preserve the editable input.

Select **Weekly reset** in `/statusline` to show `resets in 2d 5h`
(`weekly-reset` in `tui.status_line`). The countdown uses
Codex's reported weekly reset timestamp and the local clock; refreshing it makes
no model calls or extra quota requests. Missing timestamps show `reset unknown`;
an elapsed timestamp shows `reset due` until updated quota data arrives. This
item is optional and shares the stats row with the other selected stats.

The strip shows active and waiting direct workers without a count header:

```text
GPT-6.1-Sol (high) [worker] · implement fix · 1:20 · 25K tok
```

Rows start with the full reported model, reasoning effort, and agent role,
followed by a short task name, current-turn elapsed time, and current-turn reported
tokens. Routine active
state is implicit; approval/input waits and stale data remain explicit. Long names shorten
to leave room for metrics. Activity rows are dim; warnings use normal brightness.
Recognized GPT Sol/Astra names use display casing while preserving their version
and suffixes; custom and provider-prefixed identifiers remain intact. The model
comes from Codex's thread metadata, which is not per-turn execution telemetry.
Unknown model, effort, time, and token values show `?`; a missing or empty role
shows `[general]`. Custom roles retain their reported names with control characters
replaced by spaces. Finished, interrupted, failed, and idle workers disappear.
The strip hides once no activity
or warnings remain. Small terminals clip rows to preserve the prompt.

Native background shell commands belonging to this root session also appear in
the HUD, without a `park` registration:

```text
Background · python train.py · seen 12:34
```

Background rows show a compact command label and a `seen M:SS` timer, with no
model or token fields. The stock terminal API has no start timestamp: **seen
measures time observed by this client**, not the command's total runtime, and resets when the
client restarts or sees a new command identity.

Workers, background commands, Advisor status, warnings and `+N more` share a
six-row activity limit. At least one worker group, native shell and detached job
is shown when each category has activity. Warnings retain priority, and overflow
counts activity that does not fit. Automatic shortcut, queue and Escape hints
are hidden; their keys still work. Explicit `?` help, search, quit confirmation,
warnings and operational transcript state remain available. The working status
keeps the literal `esc to interrupt` indicator. AP integrations must use the
composer cursor and busy indicator; the old last-six-nonempty-lines detector is
incompatible with this expanded footer.
The generic `Background status unavailable` banner is omitted. Existing per-job
unknown/stale labels and detailed job status remain; display does not dismiss a
failure, resolve cleanup or rewrite execution records.
Worker and shell queries fail independently, so unavailable shell metadata does
not hide valid workers. A successful terminal-list refresh removes commands no
longer present; disappearance does not claim successful completion.

Elapsed time uses the latest turn's reliable timestamps. Explicit approval/input
flags produce waiting labels. An active runtime overrides an older completed
turn. Thinking and tool execution are not separately identifiable from this
metadata.

Read-only `thread/list` and `thread/turns/list` requests run locally in the
background. Turn item bodies are not requested. A local timer refreshes the UI;
it never starts, steers, or queues a model turn. A stalled or disconnected
metadata source shows stale cached rows or an unavailable warning. Refresh does not
capture keyboard input.

Standalone token counts use the local rollout's latest usage record for the exact
worker and displayed turn, through a bounded read-only tail scan. Reusing a worker
starts a new turn count; restarting the client preserves the recorded turn count.
Counts include input and output, with cached input already included in input.
They reflect recorded usage updates, not a per-token streaming counter or a
subscription cost. A reported zero displays `0`; missing, unreadable, unmatched,
or out-of-budget usage displays `?`, without falling back to lifetime totals.
Remote backends show `?`. Forked workers need a usage record for their own exact
thread and turn; inherited parent records cannot supply their count.
The release adds no native database migration and requires no shared-daemon
replacement. See [delivery limits](docs/background-work.md#stock-daemon-and-delivery-limits)
for crash recovery and ambiguous notification delivery.

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
GPT-6.1-Sol (high) [worker] · hud styling (3 agents) · 1:20 · 250K tok
```

The count includes only active/waiting members: 3 becomes 2, then 1, and the group
disappears at zero. Group rows retain their batch scope: tokens include lifetime
usage from all members of that explicit batch, including finished members, and
elapsed time starts at the earliest member's creation. Their counters use the local
backend's existing SQLite metadata through a read-only connection, respecting its
configured SQLite directory; forked group members remain unknown because their
lifetime totals can include inherited usage. Use a fresh batch for a new dispatch.
Standalone rows pair latest-turn elapsed time with that same turn's usage. Missing metrics remain
unknown. Model and effort come
from the remaining active/waiting members; differing known settings show `mixed`.
The role prefix also uses only active/waiting members: matching roles share their
name, differing known roles show `[mixed]`, and any missing or empty role shows
`[general]`. Finished members do not change the role prefix.

Different batches and parent threads stay separate. Untagged or malformed names
remain standalone rows. Grouping never guesses purpose from timing or similar
names and makes no model calls.
The six-row limit and `+N more` count groups, standalone workers, native shells and detached jobs.

## Astra advisor

From the main session, request an explicit, read-only second opinion:

```sh
./bin/codex-plus advisor --brief 'Review the proposed approach and its main risks.'
```

The helper selects the newest available numeric Astra model supporting `xhigh`,
then runs one isolated read-only consultation with the packaged native client.
Supply a self-contained brief: the advisor receives clipped Git status and diff
statistics, without the session transcript or diff contents. The footer shows
readiness, the current consultation, and the completed call count and duration;
refreshing it makes no model calls. Consultations needed before the next decision
stay foreground, with a live conversation row such as
`• Advisor on wake and cancellation fixes · 5:01`, then a completed or failed
topic summary. The model and effort remain in the footer. An explicit topic
header also identifies advisor wrapper scripts without relabeling unrelated jobs.
See [advisor usage and limits](docs/advisor.md) for the bounded local wait.

Fleet-config owns installed wrapper routing; `advisor` must reach the package's
helper alongside `park`, `run`, and `--version`. The repository launcher above
works without changing the installed fleet wrapper.

## Background completion

The [park workflow](docs/background-work.md) lets the main agent end its turn
while specific workers or finite background shell commands run. A local observer
wakes the idle parent on verified completion without periodic model polling.
The main agent must register the jobs and finish its turn; this does not pretend
that an active synchronous tool call is idle. The observer is separate from HUD
rendering. Supervised observers reconcile registered jobs after a daemon restart.
If notification delivery cannot be confirmed from the stock queue or history,
the coordinator displays uncertainty and does not blindly resend.

The HUD also shows local watcher health when a wake is uncertain or monitoring
is unavailable. Completion notices are short: `LIVE` continues the authorized
task after verification; `REPORT ONLY` reports results discovered after a restart
and waits for renewed direction. These features work with the stock daemon;
stronger native receipt guarantees remain a separate experiment outside releases.

## Shell execution policy

The [shell workflow](docs/shell-workflow.md) gives explicitly managed
noninteractive commands a two-second foreground budget. Use `codex-plus run --`
to capture complete output from launch and a command marker to select `auto`,
`background`, or `foreground`. Long managed commands keep progress in the HUD
and leave compact result records, including on resume. Interactive and unmarked
commands retain their ordinary display.

The runner's private logs remain until explicitly removed. Rendering does not
end model turns: agents still register finite remaining work with `park` and end
their turn. This policy uses the stock daemon and adds no background service or
global configuration changes. Fleet-config owns the shared workflow rollout.

## Verification

See [verification.md](docs/verification.md) for commands, measured results, and
remaining limits. Earlier 910b prototypes remain in the local, unversioned
`handoff/` archive; they are not part of this source snapshot or installed by
the launcher.

The native patch is version-specific. Updating the ordinary Codex installation
does not update this build; rebase and retest the patch before changing its
upstream version. Upstream Codex is [Apache-2.0 licensed](https://github.com/openai/codex/blob/rust-v0.163.0/LICENSE).
