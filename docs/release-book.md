# Codex Plus release book

Use this book to prepare, qualify, recover and publish a release. Start by copying
the [release checklist](release-checklist.md) into the release's evidence directory.
Keep one coordinator, one native execution owner per architecture, and one record
of accepted evidence. The objective is fewer failed attempts and repeated steps,
without weakening package or cleanup checks.

This book organizes the existing contracts; it does not introduce another runner.
Use [package building and handoff](releasing.md) for package details,
[native verification](verification.md) for client checks, and
[reviewed jobs](detached-jobs.md#agent-workflow) for execution.

## Current qualification boundary

As of 2026-10-23, 0.4.7 is published and fleet-config has reported installation on
all four hosts. Preserve its artifacts and accepted evidence.

The new [repeatable native workflow](release-protocol.md) is **under qualification**:
success rehearsals and private failure/interruption recovery passed on x86 and
ARM. Those rehearsals are not full builds. The first full x86 proof passed
preflight, frontend and helper stages, then failed during license generation
because the packaging subprocess could not find `rustc` on its PATH. Cleanup was
independently reconciled; the original failed result remains failed.

Before making that runner the standard path, fix and regression-test the packaging
environment, freeze corrected tooling, and pass full native qualification on both
architectures, compatibility, captures, AP replay and handoff generation. Its
[completion gate](release-protocol.md#completion-gate-for-this-workflow) is the
authority. Until then, use the reviewed recipes supported by the previous accepted
release, with fresh inputs and the checks below; do not improvise a new controller.

## Owners and handoffs

| Owner | Responsibility | Returns |
| --- | --- | --- |
| Coordinator | Scope, product/tooling freeze, x86 execution, evidence review, publication | Accepted matrix, note, handoff and verified release URL |
| Native ARM session | Local ARM preparation, build, tests and cleanup | Archive plus a separate hashed evidence bundle |
| AP owner or pinned replay | Actual released pane classifier and stall-hash checks | Report bound to the candidate's native captures and AP identity |
| Fleet-config | Pins, maintenance holds, installation, global configuration and rollback | Per-host installed version, quiet checks and rollout result |

Discover current relay peers before sending work. A native ARM handoff contains
the exact product and tooling commits, profile, recipe/input hashes, expected
duration, resource requirements, log locations, acceptance checks and stopping
point. The native owner verifies them before execution. SSH carries this handoff
and the results; it does not supervise a long nested remote build.

Keep implementation workers bounded: one named deliverable, one owner and a clear
return point. Finish an assignment when its deliverable or blocker is known.
Do not keep a worker producing status-only replies through the release.

## Standard sequence

### 1. Establish scope and freeze

Record the intended version and user-visible changes. Separate product changes,
tooling repairs, companion documentation and fleet settings. Record existing
authorization for preparation, publication, main merge and rollout separately;
reuse permission already granted for the same action and scope.

Freeze the product commit, upstream commit, frontend patch hash and tooling commit.
Record each architecture's host profile and installed executor identity. Inspect
dirty work and isolate the release from unrelated changes. Verify Git cleanliness
through a scratch index when index bytes or mtimes are sealed: even inspection
commands can refresh the real index.

Determine the actual previous/in-use versions with fleet-config. Qualify those
old-TUI/new-helper combinations and the proposed rollback target. Agree on config
keys/defaults, retention, restart needs and any exact global policy block early.

**Exit check:** the checklist identifies what will ship, who executes it, which
checks are required, and which source/tooling/host identities those checks bind.

### 2. Run cheap checks in the real environment

Before spending time compiling, exercise the actual staged recipe and fixtures:

- Verify all staged inputs and imports on the execution host, including helper
  scripts called indirectly. Pin the Python executable, loader and import origins.
- Verify compiler and system-tool discovery in every stage's environment,
  including packaging and license generation. An explicit Cargo path in the build
  stage does not make plain `rustc` discoverable by later subprocesses.
- Check the actual fixture socket paths and permissions with their constructors.
  Use private short paths; a calculated length alone is not a startup test.
- Validate official daemon, package-backed helpers, available native libraries,
  test discovery and result parsing. Keep host limitations explicit.
- Check free disk, memory, effective CPU limits and competing builds. Acquire the
  declared resource exclusions; a resource snapshot is not a reservation.
- Exercise owned-process cleanup and the exact cleanup-report representation.
  Preserve a baseline before launch and confirm no unexplained owned work exists.

Use [preflight guidance](release-performance.md) and the checked-in recipe rather
than pasting a new release-specific script. Repair a shared recipe first, add the
focused check that catches its failure, and freeze a new tooling revision before
launch. Do not edit scripts inside a running or sealed attempt.

**Exit check:** preparation passes on both hosts against the files and environment
the real job will use. A dry run with different paths is not this check.

### 3. Build locally and retain outputs once

Review the ordinary managed job proposal before launch. Record the execution UUID,
root, frozen input digest, logs, deadline and cleanup contract. Launch once and
register completion observation; do independent work or end the turn while waiting.
Use the [shell and wait workflow](shell-workflow.md), not repeated model polling.

For established recipes, run x86 and ARM independently in parallel when resources
permit. For a newly repaired shared recipe, validate the relevant x86 path before
committing both hosts to expensive runs. Independent ARM preparation can continue.

Build complete packages, including frontend, code-mode host, helper, resources and
license notices. Compile each required test harness once and retain its path and
hash before shared Cargo outputs can be overwritten. Record stable libtest output,
the independently enumerated tests, counts, failures and ignored reasons.

**Exit check:** native outputs and package inventory exist with verified identities;
workload and cleanup results are both known. Compilation success alone is not
package acceptance.

### 4. Qualify the extracted packages

Use private homes and the official stock daemon. Verify archive hash, complete
manifest, modes, tree and executable versions after extraction. For a trusted,
validated archive extracted into a private directory, preserve archive modes with
`tar --same-permissions`; retain private IPC permissions separately.

Run the required native and package-backed checks, rollback over candidate-written
records, maintenance holds in both directions, and the actual old-TUI/new-wrapper
mix. Include affected Advisor/Goal/Ultracode behavior. A pre-existing failure needs
the exact previous-release comparison under the same conditions and an explicit
reviewed limit; an old failure count is not an allowlist.

For frontend changes, capture the candidate on both architectures, including
ordinary and narrow panes, idle/busy/draft/overlay/form states, cursor position and
visibility, and time-separated busy captures. Replay the released AP classifier
and stall hash against those exact captures. Record where the replay ran separately
from the native architecture that produced them. Confirm fleet dependencies before
rollout; do not replace this evidence with synthetic fixtures alone.

**Exit check:** the coordinator verifies the artifacts and required acceptance
checks, including cleanup, with no unexplained skips. A worker's summary points to
evidence; it is not the final acceptance by itself.

### 5. Assemble and verify publication

Generate the note and handoff from accepted package identities. Follow the
[handoff contract](releasing.md#qualification-and-handoff); include retention,
schema/migration impact, rollback/mixed-version results, config changes, restart
requirements and any qualification limits. Use `agents_md: null` when unchanged;
otherwise include the exact reviewed full block.

The source commit in each manifest is the product that passed qualification. A
later companion-note/tag commit may differ; explain that distinction rather than
claiming the binaries were rebuilt from the companion commit.

Within publication authorization, upload the two archives, their two checksum
files and handoff. Verify all five assets against GitHub digests and download the
handoff for byte comparison. Use the exact release ID. If a newly created draft
is missing from a list, resolve its tag/node before retrying creation. After
publication, verify the public release again and send fleet-config the tag and
digest-verification notice.

**Exit check:** published assets match accepted artifacts. Main merge and fleet
installation remain separately tracked actions, not consequences of publication.

### 6. Close the rollout and record timing

Fleet-config owns the maintenance hold and installation. Supply fresh central
Advisor in-flight checks when requested, stop new consultations/work during the
agreed switch, and let the current turn finish. Retain the qualified rollback and
in-use packages. Do not change the shared daemon or global settings to unblock a
test or cutover.

Record fleet's result per host. Verify a restarted TUI's actual executable before
saying it runs the new frontend; changing `current` does not replace a live TUI.
Close the checklist with measured durations, accepted limits and the few concrete
changes needed before the next release.

## Failure decisions

| Observation | Next action | Evidence to retain |
| --- | --- | --- |
| Preparation or fixture setup fails | Repair the cause and repeat the affected cheap check before compiling | Original failure and regression result |
| Product or test fails | Diagnose against the frozen inputs and exact baseline; rerun affected gates after the fix | Original result, logs, source change and new binding |
| Workload ends but cleanup is uncertain | Investigate exact processes, descendants, socket and source seals; use supported resolution after proof and authorization | Original managed result plus separate recovery receipt |
| `REPORT ONLY` or lost observation | Report what is known and wait for renewed direction before old follow-ups | Ledger, known command receipt and uncertainty |
| Background row is stale | Check execution/result and observer health; distinguish running, ended and unknown | Last observation and actual process/result evidence |
| Publication listing is delayed | Inspect the existing exact release/tag identity | Release ID, asset inventory and downloaded hashes |

Staleness is not proof of a dead process. Never kill, relaunch, delete a record or
declare success based on age alone. Resolve an ended job only after independent
cleanup verification. A recovery receipt does not turn a failed build into a pass.
Preserve failed evidence even when a separately reviewed package can be accepted.

For the new runner, final cleanup seals an attempt; failed receipts and interrupted
starts also prevent replay. There is no cross-attempt receipt cache. Follow its
[recovery contract](release-protocol.md#failures-and-recovery), not an invented
resume command. Preserve accepted artifacts while identifying the smallest set
of checks invalidated by a change; reuse requires matching identities and an
existing supported acceptance path, not a filename or past green summary.

## Where to save time

1. Catch interpreter, socket, compiler-discovery, parser and cleanup defects in
   cheap composed checks before the build.
2. Build ARM on ARM under its local session. Transfer an archive once by digest
   and send compact evidence separately, without duplicate source/target trees.
3. Keep stable, verified Cargo caches and avoid simultaneous writers. Measure
   fresh/compiled artifacts before adding another cache or changing checkout layout.
4. Reuse unchanged qualified outputs only through an identity-checked supported
   path. A helper-only repack is not a measured full frontend build.
5. Freeze scope before qualification; run independent work in parallel; avoid
   repeated already-passed gates unless a change or unresolved concern invalidates them.
6. Use completion notifications and bounded worker assignments instead of repeated
   status turns. Report the blocker, owner and next action in plain language.

Record wall time for preflight, frontend, helper, packaging, test-harness build,
tests, compatibility/captures, AP replay, transfer, recovery, review/approval wait,
publication and rollout. Mark overlap: sum of stage times is not elapsed release
time. The historical 0.4.7 ARM total of about 45 minutes already includes its
roughly 18-minute harness build.

More cores are an experiment, not the default fix. Compare identical source,
toolchain, flags and cache conditions; record wall time, memory pressure and Cargo
freshness. For warm reuse, compare A1/A2 with no intervening build on that target
directory. Change concurrency only within measured CPU/memory limits. Report cold,
warm and helper-only results separately; do not promise whole-release speed from
a single fast stage.

## Evidence and maintenance

Keep the checklist, immutable input identities, original logs/results, acceptance
receipts and timing with each release. Archive durable evidence with the release
handoff; local `.build` paths are working references, not portable published proof.
The book records the procedure; per-release reports record versions and verdicts.

Local evidence behind this revision includes `.build/release047/release-ready.json`,
`.build/release047/publication/published-verification.json`, and under
`.build/release-protocol/`: `preparation/v3/`, `private-failures/`,
`private-failure-preparation/arm-return/`, and `fullproof-a1-recovery/verification.json`.
These paths are relative to the coordinator's main checkout, not this documentation
worktree. The original full-proof execution is
`5bfcf26a-35fe-41eb-b20f-9dda549cddab`; its failed result is intentionally preserved.

After an incident, add its supported cause and earliest useful preventive check
to the existing tooling or this book. Do not accumulate permanent per-release
adapters, blanket failure exceptions or another orchestration layer.
