# Native HUD verification

Target: Codex 0.163.0, commit
`83086188ae0614bd882818ff1aa23f3194962012`, Linux x86_64 and ARM64,
Rust 1.99.1. Keep the ordinary installed Codex and global configuration intact.

## Saved native results

Build the selected Rust test executable once in a stable checkout with the
required toolchain, profile, target and features. Capture Cargo's
`--message-format=json` output from `cargo test --no-run`, select the intended
`compiler-artifact` with `profile.test` and a non-null `executable`, and record
its absolute path, digest and build inputs. Do not guess the hashed filename or
select a binary from another target. Preserve it before another build can
replace it. Run each filter's list and tests from that same executable and
environment, recording the native command's actual exit status:

```sh
"$TEST_BINARY" "$FILTER" --list > "$EVIDENCE/list.txt" || exit 1
if "$TEST_BINARY" "$FILTER" --test-threads=8 --color never \
  --logfile "$EVIDENCE/results.libtest" > "$EVIDENCE/stdout.txt" 2>&1; then
  native_exit=0
else
  native_exit=$?
fi
# Perform and record owned-process cleanup independently of test/parser success.
python3 scripts/native-results.py --name "$FILTER" \
  --list "$EVIDENCE/list.txt" --logfile "$EVIDENCE/results.libtest" \
  --stdout "$EVIDENCE/stdout.txt" --exit-code "$native_exit"
```

Use a new evidence directory for each execution. The controller must handle a
nonzero command explicitly, including when using shell `set -e`; never skip
cleanup on failure. List failure aborts execution. Apply the same test-selection
flags to both invocations. Ignored tests require `--ignored-reasons FILE`, a
reviewed JSON object mapping exact test names to exact libtest reason strings
(an empty string for an ignored test without a reason). No ignored tests or
baseline failures are accepted implicitly.

The validator compares the independent list, dedicated result file, exit status
and stdout summary. It ignores terminal-rendered test rows because UI output can
join or obscure them. Keep raw inputs and command receipts: parser-only fixes can
be replayed offline without recompiling or executing tests. Such a replay proves
the recorded execution, not a new execution or a changed source tree.

## Expanded footer candidate gate

The prepared footer change raises the activity budget to six rows independently
of the two status rows. Automatic queue/shortcut/Escape hints disappear; explicit
help/search, warnings, operational state and `esc to interrupt` remain. A normal
footer has at most eight nonempty rows below the prompt; explicit help/search may
add its own row. This is incompatible with AP's former six-line readiness scan;
fleet rollout requires the cursor/busy-indicator integration gate.

[Unreleased candidate evidence](evidence/ui-043.json) records the built production
source separately from test-only companion commits, scoped native and terminal
results, AP's reported classifier replay, and the remaining release gates.

Run `tests/test_footer_rows_tui.py --source SOURCE --binary CANDIDATE --evidence NEW_DIRECTORY`
using the private native-smoke Python environment. The sealed contract covers
idle, busy draft, narrow busy draft and explicit help, requiring visible worker,
native-shell and detached-job categories plus separate Advisor/overflow rows,
exact hidden counts, editable input, and no model requests.
The revised contract also rejects the generic background-availability banner
while retaining the affected unknown/stale job. Invalid and removed projection
checks must not rewrite evidence or claim successful completion.
Run native worker-HUD,
composer footer/status-surface tests and the updated smoke/fixture suites too.
Reconstruct the frontend patch cleanly and verify the original daemon sources.
Source preparation and old-version red evidence do not establish candidate
acceptance. The release evidence below retains the limits verified at that time.

## Current release acceptance

### Morandi theme (development)

[Accepted development evidence](evidence/morandi-theme.json) binds the palette,
client binary, final patch, test-only corrections and native/terminal results.

Run `tests/test_morandi_tui.py --binary CANDIDATE --expect present --evidence
NEW_DIRECTORY` with the private native-smoke interpreter. It checks the actual
selected theme, live preview, cancellation, persistence, restart and zero
display-driven model requests. The prechange client must fail the present check.

Native `morandi`, `theme_picker`, `render::highlight`, `custom_terminal`,
`insert_history`, `style::contrast` and status-line style checks cover theme
resolution, both paint paths, preview invalidation, contrast, color capabilities,
selection, modifiers and unchanged behavior under the other themes. Preserve
exact test lists and libtest results. Test the compact command contract with
`tests/test_command_display_tui.py --theme morandi` as well: changing colors must
not change command execution, retained results, disclosure or model requests.

The palette is described in [theme behavior](themes.md). Native x86 development
verification does not establish ARM package qualification or fleet activation.

For the brighter palette, run `tests/test_morandi_command_colors_tui.py --binary
CANDIDATE --launcher MATCHING_HELPER --evidence NEW_DIRECTORY --color-level 2`
and repeat with level `3`. It executes the existing private success/failure
fixtures and checks actual command-dot colors against the Context item while
retaining the folding and raw-output checks. The previous palette must fail the
shared-green assertion. The candidate must have its matching `codex-plus-runtime`
adjacent to the client: `--launcher` selects the shell runner but does not change
the client's goal/advisor helper lookup. Add `--theme morandi` to the status-line PTY checks to
exercise the actual selected theme, including ANSI256/TrueColor, ANSI16 and
disabled-color cases; unthemed runs do not establish Morandi acceptance.

### Compact command display (development)

[Development evidence](evidence/compact-command-display.json) records the exact
candidate and patch identity, passing checks, reviewed fixture corrections, and
platform limits. Fleet installation and release qualification are separate.

Use `tests/test_command_display_tui.py BINARY --launcher MATCHING_HELPER
--evidence NEW_DIRECTORY` in the private smoke-test Python environment. Its nine
cases cover quick, unmarked, background, failed, exploring and running commands;
local expansion, narrow input, completion and restart; and equality of stored
tool results with model-visible results. The display must add no model requests.
Initial bytes that the stock backend does not stream remain available in the
final aggregate; live checks use a marker emitted after the activity is visible.

Check the optional status item with `tests/test_status_lines_tui.py --binary
BINARY --source PLUS_SOURCE --evidence NEW_DIRECTORY --case branch --compact
--colors on --color-level 3`, repeated for color levels 2 and 1, colors off,
and NO_COLOR. The first row must place `compact` after the branch, use a distinct
color when enabled, and keep statistics on the second row.

Native checks include `command_display`, `compact_display`, `shell_policy`,
`chatwidget::tests::exec_flow`, `exec_cell`, `transcript_view`, `native_history`,
and status-line picker/style/preview filters. Preserve the existing shell-policy,
native smoke, shell-topic and detached-HUD regression gates. Record exact test
lists and dedicated libtest results; snapshot updates require review against
the approved display contract. These development checks do not install or
qualify release packages on the fleet.

### Short background topics

[Source evidence](evidence/shell-topics.json) records 127 passing native filter
invocations (overlapping coverage), five topic PTY stages plus a real native
shell, all 23 terminal smoke stages and eight detached-HUD stages. The original
long-label reproduction failed before the change. Full commands remain in
history; display refresh adds no model requests. This frontend is not installed.

Use `tests/test_shell_topics_tui.py` with the smoke-test Python environment and
a complete private candidate (matching Code Mode host and Rust helper beside
the client). The test covers explicit labels, narrow editable input, reconnect,
legacy script-name fallback and row disappearance. A real native shell verifies
that its label survives transport while the full command remains in history.
Display refresh must add no model requests.

Run native `shell_policy`, `worker_hud`, `chatwidget::tests::exec_flow` and
`thread_transcript::tools::tests` filters, the full terminal smoke suite and the
detached HUD cases. Raw command controls must remain safe after label shortening;
an unrelated background shell must remain separate from an active advisor.
Regenerate and reconstruct the complete frontend patch before recording the
new candidate identity. These frontend checks do not require a helper rebuild.

### Reviewed detached jobs (development candidate)

The [job handoff workflow](detached-jobs.md) uses an independent execution store,
root admission, and the existing observer. Qualify the native helper against an
official stock daemon; a mock or detached-process prototype alone is insufficient.

[Source qualification](evidence/detached-jobs.json) passed on x86 and ARM: 173
Rust tests, 27 park tests, seven upgrade tests, six stock lifecycle scenarios,
32 goal scenarios and 22 detached-job scenarios on each architecture. The x86
client passed 45 HUD and four execution-HUD filter invocations (overlapping),
eight projection PTY cases, 23 terminal smoke stages and a real-job TUI reconnect.
Dedicated SSH login closure, admission after closure and both daemon restart
modes also passed with the production helper on both architectures. Packaging
checks passed 41 tests. Complete release archives and the new ARM TUI are not
qualified by these source checks.

```sh
cargo +1.99.1 test --locked --manifest-path rust-runtime/Cargo.toml --lib
python3 tests/test_detached_jobs_stock.py --runtime /absolute/candidate/runtime \
  --stock-binary /absolute/official/codex --scenario completion \
  --evidence .build/detached-completion
# Repeat for each SCENARIOS entry in that test. Use the smoke interpreter with pyte:
python3 tests/test_detached_job_tui.py --binary /absolute/candidate/codex \
  --runtime /absolute/candidate/runtime --stock-binary /absolute/official/codex \
  --evidence .build/detached-tui
python3 tests/test_detached_execution_hud_tui.py --binary /absolute/candidate/codex \
  --evidence .build/detached-hud
```

Cover actual graceful/abrupt daemon replacement, no-TUI wake, root input/goal
fences, restricted-context refusal, duplicate UUIDs, monitoring failure after
spawn, report-only recovery, cancellation/cleanup, exclusive resources and
maintenance. Unit fault boundaries cover missing identities and receipts;
continuous descendant output must not prevent cleanup. Verified cleanup may
allow maintenance without turning an unknown command outcome into success.

The real TUI test names its thread to isolate display from upstream automatic
title generation, reconnects during one execution and checks editable input,
one completion wake and no display requests. Projection-only fixtures separately
cover malformed/stale status and the three-row limit. Run existing park, goals,
upgrade and terminal suites as regressions. Synthetic same-payload package
switches test the mechanism, not arbitrary release compatibility or fleet pruning.
Complete per-target archives and fleet rollout require their separate gates.

### Parallel MCP and web activity

Source/development acceptance is recorded in
[parallel-tool-rows.json](evidence/parallel-tool-rows.json). Root checks passed
195 native filter invocations (overlapping coverage), all nine targeted PTY cases,
all 23 terminal smoke stages, both upstream UI scenarios and 53 source-guard,
packaging and fixture checks. This fix is not part of installed 0.3.2.

Overlapping tool calls must settle their original transcript rows by call ID,
including calls that finish out of order or after another activity replaces the
active row. After completion, neither `Calling` nor `Browsing the web` may remain
active, and each call's result appears once. Preserve success, failure, image
output and full transcript details. Interrupted calls settle without claiming
success; a late result updates the same retained row. If ordinary terminal
scrollback already printed the interruption, print the authoritative late result
as a correction without adding a second retained transcript entry.

Check both the owned transcript and ordinary terminal fallback. Pending terminal
history waits for settlement instead of permanently printing an unfinished row.
Cover mixed MCP/web calls, completion-only replay, unrelated shell output,
turn-end cleanup and composer editing, using private homes and loopback fixtures.
These checks do not require a model request or a daemon change.

```sh
# Use the native-smoke interpreter with pyte installed:
python3 tests/test_tool_activity_tui.py /absolute/candidate/codex \
  --evidence .build/tool-activity
```

The nine PTY cases cover completion, interruption and late results in owned,
terminal-owned and no-alternate-screen modes. Native filters `tool_activity_tests`
and `native_history::tests` cover the same call ownership and scrollback ordering.

### Plus-owned goals for 0.3.2

Use [the approved design](custom-goals.md) and [the goal workflow](goals.md).
The implementation evidence is [custom-goals.json](evidence/custom-goals.json);
it identifies the tested x86 frontend and optimized Rust helper separately from
release packages. Tests use private homes, an official stock 0.163.0 daemon and
loopback providers. They do not modify the installed daemon or global policy.

Root acceptance passed 112 optimized-runtime unit tests, all 31 goal scenarios,
both actual goal TUI cases, 27 park regressions, seven upgrade cases, six stock
lifecycle scenarios and all 23 terminal smoke stages. The focused native filters
passed 69 goal, 38 status, 42 HUD, 18 shell-policy, 53 execution-flow and six
transcript tests, with overlapping coverage. Packaging, launcher, source-guard
and terminal-fixture checks passed 40, 11, 11 and 18 tests respectively; both
upstream UI scenarios passed. Preserved failed runs and their fixes are recorded
in the evidence, including the final independent-callback/EOS regression.

Subsequent ARM release qualification exposed an acknowledged queue-to-history
visibility gap. Its deterministic regression went red before the fix; additional
root checks passed 120 Rust tests and the pre-report recovery-barrier stock case.
Goal fixtures wait for positive attribution rather than equating provider/Idle
visibility with coordinator acknowledgement. The advisor fixture retains its
narrow live checks and restores normal height for completed transcript history.
The release companion note records the final per-target archive reruns; the
earlier source evidence does not qualify those new archive bytes.

```sh
cargo +1.99.1 test --locked --release --manifest-path rust-runtime/Cargo.toml --lib
python3 tests/test_custom_goals_stock.py --runtime /absolute/candidate/runtime \
  --stock-binary /absolute/official/codex --evidence .build/goals --scenario all
# Use the native-smoke interpreter with pyte installed:
python3 tests/test_custom_goal_tui.py --binary /absolute/candidate/codex \
  --runtime /absolute/candidate/runtime --stock-binary /absolute/official/codex \
  --evidence .build/goal-tui --scenario all
```

Cover exact-turn dispositions and positive EOS, input priority/receipt races,
partial shell and worker completion, missing/repeated progress, pause/edit/clear,
replaced goals, queue ambiguity, report-only renewal, controlled isolation,
unloaded roots and actual TUI resume restarting observation. Check an independent
park callback racing goal EOS before admitting the next goal turn. Healthy job
waiting and display refresh must add no model requests. The right goal indicator
must preserve editable input, narrow layouts and the three-row activity budget.
Run the existing park, upgrade, stock lifecycle, terminal, packaging and focused
native goal/status/HUD/shell/transcript checks as regressions.

Wire fault fixtures and seeded crash windows are labeled separately from official
daemon cases. Restrictive read-only/Plan and workspace policies verify denied
checkpoint writes lead to NeedsInput, without widening permissions; they do not
establish automatic checkpointing under those policies. The approved stock
already-admitted recovery exception remains. Complete x86 and ARM archives need
their own native qualification before release; these source checks are not that.

### Worker time and token scopes

Standalone and grouped rows pair the latest turn's elapsed time with that exact turn's
reported usage. Regression fixtures reuse a worker with 342,740,535 lifetime
tokens and 679,013 current-turn tokens: the row must show `679K`, then reset
when its turn ID changes. Reloading the TUI must recover the same reported turn
count. Missing matching usage shows `?`; lifetime counters are not a fallback.
Cached input remains included in input rather than being added again.

Local reads are bounded, read-only, and match both thread and turn identity.
Check incomplete/malformed records, oversized tails, zero/negative values,
forks, and unavailable files. Remote fixtures include local rollout and SQLite
decoys and must still show unknown usage. Explicit groups show active/waiting
member counts, the longest current assignment and summed current-assignment
usage. Finished members contribute nothing; reusing a worker for a new turn
resets its metrics. Run `test_worker_roles_tui.py` against the candidate for old
thread/new turn, inherited-record rejection and completed-member regressions.
The terminal smoke also checks editable input, the six-activity-row budget and
zero model calls while these metrics refresh.

For detached work, run `test_detached_execution_hud_tui.py` and native
`execution_hud` checks: unknown outcomes keep `needs review` even after heartbeat
expiry. A valid optional `finished_at` freezes duration; missing/null end time
shows `?`. Live jobs retain ordinary running/stale handling. Runtime projection
checks require verified resolved-Unknown activity removal while retaining the
durable outcome, resource-conflict behavior and report-only barriers.

### Audit regression checks

The post-0.3.0 audit fixes are source changes, not a qualified release. In
addition to the helper checks below, run these against the rebuilt runtime:

```sh
python3 tests/test_rust_runtime_advisor_regressions.py \
  --runtime .build/codex-plus-runtime --evidence .build/advisor-regressions -v
# Use the native-smoke pyte interpreter and an official stock binary:
python3 tests/test_advisor_stop_integration.py --binary /absolute/official/codex \
  --runtime .build/codex-plus-runtime --evidence .build/advisor-stop
python3 -m unittest discover -s tests -p test_build_artifacts.py -v
```

These cover native group/owner kills, detached descendants, Git hook isolation,
bounded multilingual results, and Cargo's actual artifact locations. Coordinator
and upgrade suites include batching/cancellation on real Rust connections,
legacy stranded callbacks, bounded-history uncertainty, transport failure, and
relative homes across packaged startup and upgrade. The terminal smoke checks
zero inactive-worker turn reads while excluding completed group metrics.
See [the audit fix record](audit-fixes.md) for executed checks and remaining
release checks. [Notice resolution](notice-resolution.md) records the subsequent
40 packaging tests, complete x86/ARM production graphs, source bindings and
independent component-notice requirements. This does not replace target-specific
release checks.

### Release 0.3.0 baseline

For the Plus 0.3 helper migration, use the
[Rust runtime checks](rust-runtime.md#qualification). The Python 0.2 suites below
remain historical/oracle evidence, rather than proof of the installed runtime.
Native client acceptance remains valid only for the identical frontend patch;
complete 0.3 archives require their own extracted-package qualification.

Implementation acceptance passed 51 Rust unit tests, 19 coordinator cases,
16 runner cases (three Python-only mocks skipped), five upgrade cases, and five
official stock lifecycle scenarios on each native architecture. The x86 root
gate additionally passed 24 packaging, 11 launcher, and eight build-guard checks.
Both architectures passed a held 65-second foreground advisor consultation with
no polling turn or park. Python-free filesystem fixtures cover managed I/O and
SQLite status on both targets, observer crash recovery on x86, and official
background/advisor execution on ARM. ARM's stock/no-Python scenarios precede a
formatting-only final rebuild; unit, coordinator, and upgrade gates were rerun.
See [rust-runtime.json](evidence/rust-runtime.json) for exact artifact identities,
preserved fixture failures, source provenance, and untested fault boundaries.
Those implementation results used Rust 1.99.0. The current build pin follows
the fleet at 1.99.1; [rust-1.99.1.json](evidence/rust-1.99.1.json) records its
helper rebuild and qualification. Complete native-client release archives need
fresh 1.99.1 builds before publication.

The frontend patch changes TUI, CLI, and workspace lock reconciliation only;
backend and database source must match the pinned 0.163.0 tag. The historical
0.162.1 receipt extension is not part of this release and has not been ported.
Check clean and reverse patch application, exact-source reconstruction including
new files, and equality of external Cargo.lock entries with the 0.163.0 tag.

Run source-guard and packaging tests from this repository:

```sh
python3 -m unittest discover -s tests -p test_build_native.py -v
python3 -m unittest discover -s tests -p test_packaging.py -v
```

From the patched upstream `codex-rs` directory, build with `--locked` and run
the focused native tests (the environment may require `RUST_MIN_STACK=33554432`):

```sh
cargo +1.99.1 test --locked -p codex-tui --lib worker_hud --profile dev-small -j 12
cargo +1.99.1 test --locked -p codex-tui --lib shell_policy --profile dev-small -j 12
cargo +1.99.1 test --locked -p codex-tui --lib chatwidget::tests::exec_flow --profile dev-small -j 12
cargo +1.99.1 test --locked -p codex-tui --lib thread_transcript::tools::tests --profile dev-small -j 12
cargo +1.99.1 test --locked -p codex-tui --lib daemon_menu --profile dev-small -j 12
cargo +1.99.1 test --locked -p codex-cli --bin codex daemon_update_tests --profile dev-small -j 12
```

Also exercise the affected upstream footer selection, config panel, status-line
style, terminal-program status, session status/lifecycle, retained reply, and
disconnect tests. Verify terminal start/resume, PTY and pane isolation, compact
footer placement, composer editing, and zero model calls from HUD refresh with
`tests/native_smoke.py`. Use the [release procedure](releasing.md) for fresh
GNU builds and extracted-package qualification on each native architecture;
bundled zsh is no longer a package resource. Run coordinator lifecycle and
affected isolated integration gates from [background verification](background-work.md#verification)
against an official 0.163.0 daemon in private homes with loopback providers.

When reusing a Cargo target cache across upstream checkouts, set
`INSTA_WORKSPACE_ROOT` to the current checkout's `codex-rs` directory. Otherwise
Insta can resolve snapshots against a previously cached workspace; review snapshot
updates in the intended source tree before regenerating the frontend patch.

The companion release note and evidence record the checks actually run and any
remaining limits. The 0.162.1 results below are historical and do not qualify
the 0.163.0 port.

### Ultracode

Run `cargo +1.99.1 test --locked --manifest-path rust-runtime/Cargo.toml --lib`
for state/admission invariants. In the patched upstream checkout, use the TUI
filters `ultracode`, `worker_hud`, `plus_goal`, `slash_commands`, and `status_line`.
Run these private Python fixtures from the repository root:

- `tests/test_ultracode_bootstrap.py --binary OFFICIAL --evidence DIRECTORY`,
  for each combination of `--remote` and `--pressure`: complete startup text,
  compaction, cold load and the negative control without restored instructions.
- `tests/test_ultracode_admission.py --binary OFFICIAL --runtime RUNTIME --evidence DIRECTORY`:
  turn binding, lost ACK, steering, both observer callback paths and cold recovery.
- `tests/test_ultracode_tui.py --binary PATCHED --evidence DIRECTORY`: actual
  PTY controls and layout with explicitly identified helper/server stubs.
- `tests/test_ultracode_contracts.py`: scripted harness checks; its `prepare`,
  `accept`, `integrate` and `finish` commands run the separate real-worker trials.

Also run the ordinary native smoke, park, goal, lifecycle, execution and upgrade
regressions. Upgrade timing tests use a stripped helper, matching package shape;
an unstripped debug ELF may spend the timeout hashing debug data. Preserve the
original executable for diagnostics. See [recorded evidence](evidence/ultracode.json)
for the qualified paths and remaining release limits.

### Hidden navigation hints for Plus 0.3

The automatic agents/shortcuts hint is absent and consumes no separate row.
Left-arrow navigation and explicit `?` help still work. Status/context, shell
mode, side conversations, warnings, search, queue, and quit controls retain their
required space. Regression checks cover height, rendering, editable input and
help open/close geometry with the persistent status enabled and disabled.

Root acceptance checks the native status-surface, hidden-hint and HUD filters,
terminal fixture/parser tests, all terminal smoke stages and both upstream UI
scenarios. The smoke uses a matching private 0.163.0 code-mode host and explicitly
enables `code_mode_host` in the transcript fixture. This is development-candidate
qualification; complete release packages require freshly built target resources.
An existing halfwidth-character snapshot needed two expected Debug strings
updated for Rust 1.99.1; its rendered buffer and test assertions are unchanged.
See [footer-hints.json](evidence/footer-hints.json) for final counts, source proof,
preserved failures and artifact identities.

### Full model labels for Plus 0.1.1

The x86 development candidate passed 36 focused Rust HUD tests, 14 terminal
fixture tests, 13 packaging tests, seven source-guard tests, all 18 native
terminal smoke stages, and both 0.163 UI scenarios. Root acceptance checked the
actual candidate, clean/reverse patch application, exact source reconstruction,
and unchanged external lock dependencies. See
[full-model-labels.json](evidence/full-model-labels.json) for commands and hashes.

Rows retain reported model versions and suffixes, effort, grouped active counts,
metrics, dim styling, and the three-row budget including alerts/overflow. Terminal
checks cover the idle prompt in the last six nonempty lines, editing, resizing,
pane isolation, footer selection, and zero model calls from HUD refresh. Fixture
recognition accepts upstream's custom effort strings as well as known levels.
This is source/development acceptance; optimized x86 and ARM release packages
have not been built or qualified for this revision.

### Astra advisor candidate

The 0.2.0 source/development candidate passed 16 advisor, seven launcher,
15 packaging, 15 terminal-fixture, and seven native source-guard checks. Root
acceptance reran the advisor/HUD/shell-policy Rust filters (8/38/16 passing
invocations, with overlap), all 19 native terminal stages, both upstream UI
scenarios, and seven integration groups against official stock 0.163.0.
See [astra-advisor.json](evidence/astra-advisor.json) for hashes and logs.

```sh
python3 -m unittest discover -s tests -p test_advisor.py -v
python3 tests/test_advisor_integration.py OFFICIAL_BINARY --evidence EVIDENCE
```

The stock integration proves actual sandbox write denial, exact Astra/xhigh
requests, structured results, interruption/failure handling, ignored user config,
and absence of delegation tools in the provider-visible inventory. It reads
root metadata without the conversation. All providers are loopback fixtures;
no live consultation was used for implementation acceptance.

The native scenarios cover persistent readiness/count/duration, running elapsed,
error/stale/invalid files, pane isolation, conservative advisor-shell coalescing,
unrelated jobs, and the shared three-row/AP prompt budget. Refreshing the footer
adds no model requests. Source reconstruction and unchanged backend/external lock
dependencies passed. Optimized/ARM packages and fleet wrapper/global-policy
deployment remain separate work; this is not release qualification.

### Foreground advisor workflow

```sh
python3 tests/test_advisor_foreground_integration.py OFFICIAL_BINARY --evidence EVIDENCE
```

This uses the exact [documented wait cell](advisor.md), replacing only its command
with private fixture paths. A loopback advisor is held for 65 seconds. The root
must remain in progress with one model request while waiting, receive the result
on its second request, and create no park, queued callback or surviving terminal.
Advisor status must move from running to done with the selected Astra at xhigh.
The fixture verifies the long outer-yield request over 65 seconds; it is not a
27-minute endurance test. See [advisor-foreground.json](evidence/advisor-foreground.json).

### Advisor conversation rows

The native terminal smoke includes real Code Mode exec/wait cases for a held
advisor. A single conversation header must tick locally past the initial shell
yield, then settle to the completed or failed header without a second row. The
fixture holds status bytes constant while checking elapsed updates, verifies no
extra model request, edits and resizes the composer, and retains the footer's
three-row budget. The advisor itself is simulated; the TUI, shell and Code Mode
lifecycle are real.

The topic-row follow-up also covers an explicitly labeled Python wrapper:
`• Advisor on wake and cancellation fixes · 5:01`. Its marker follows the
foreground directive; the model and effort remain in the footer. Canonical
direct commands retain a compact brief fallback. Unrelated commands, malformed
labels, background/auto work and stale status must not acquire this row. The
same cell retains the topic on completion/failure, and narrow widths shorten the
topic before the elapsed time. See [advisor-topic.json](evidence/advisor-topic.json)
for the follow-up candidate, executed checks and deployment limits.

Root acceptance passed 21 terminal stages, 17 fixture tests and the focused Rust
filters: advisor 13, HUD 38, shell policy 18, exec flow 53 and historical tools 6
(filters overlap). Clean/reverse patch application, exact reconstruction, unchanged
backend and all 1,314 external Cargo packages passed. The timestamp regression
covers a process publishing status before its ItemStarted notification; current-turn
correlation replaces the incorrect notification-time cutoff. See
[advisor-transcript.json](evidence/advisor-transcript.json) for hashes, preserved
initial failures and limits. No native fleet runtime was installed by this work.

## Historical 0.162.1 verification

The following sections record work against commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d` and its historical release patches.

### Frontend release boundary

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

### Checks

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

### Optimized release

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

### Historical experimental recovery

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

### Watcher health and notification modes

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

### Background shell HUD

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

### Managed shell policy

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
  --stock-launcher /path/to/qualified-plus-package/bin/codex-plus \
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

### Compact activity rows

The compact layout removes the count header, puts model/role/effort before the
worker task, labels root shell jobs `Background`, and hides healthy watcher
waiting. Approval/input waits, stale metadata, and wake-delivery problems remain
visible. Activity, a combined warning, and overflow share three total footer rows.

Primary acceptance passed 35 focused Rust HUD tests, nine fixture checks, all
18 terminal stages, and eight shell-policy cases including independent panes,
process-group cancellation, and one completion callback through the official
0.162.1 daemon. The terminal suite verifies the idle composer remains within the
last six nonempty lines, including overflow and warnings, with no added model
calls from HUD refresh. The old six-worker layout failed this check with five
HUD rows. Clean/reverse patch application and exact source checks passed.

See [compact-rows.json](evidence/compact-rows.json) for the candidate hash and
reports. This acceptance covers a Linux x86_64 `dev-small` candidate; optimized
release builds, ARM qualification, and fleet installation are separate work.

### Limits

- Live workers and the strip's placement were confirmed in the user's pane.
  Local token metadata was checked read-only; automated terminal lifecycle tests
  use controlled fixtures.
- This is a version-specific native build. Existing CLI processes must be
  relaunched to use it, and future Codex releases require rebasing/retesting.
- Counts include only active or waiting direct, non-archived spawned workers.
  At most three total rows appear below the shortcuts footer, including worker
  groups, standalone workers, background commands, warnings, and `+N more`.
  The composer must remain within AP's last six nonempty lines at this limit.
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
