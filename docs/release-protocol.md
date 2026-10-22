# Repeatable native release workflow

This workflow is under qualification. Unit tests and recorded publication replay
do not establish a successful composed native run. Keep the published 0.4.7
artifacts and their original evidence unchanged while rehearsing it.

## Ownership and inputs

The coordinator freezes the product source, reviews the release plan, builds on
x86, accepts evidence from both architectures, and prepares publication. The
native ARM session builds and tests locally and owns its cleanup. Fleet-config
owns installation, pins, global configuration, and the rollout approval.

Transfer a reviewed plan and pinned source/tooling revisions to the native owner;
return a hashed evidence bundle and the archive separately. Do not run an ARM
build through a nested SSH controller. Neither side edits executable recipes
inside an attempt to get a failing stage past its checks.

The [versioned data contract](../release/protocol-v1.md) defines plans, host
profiles and receipts. `plus_source_commit` identifies the shipped product;
`tooling_commit` identifies the checked-in recipes. A tooling repair does not
silently change the product freeze. Profiles bind the actual compiler, Python,
installed executor, local paths and bounded build concurrency.

## Local preparation

Run the checked-in tools from the pinned tooling checkout. The native owner
supplies the host profile and reviewed plan. These commands do not launch a job:

```sh
python3 -B scripts/release_native.py init --plan PLAN --profile PROFILE --rehearsal
python3 -B scripts/release_native.py prepare --attempt ATTEMPT --cleanup-hook HOOK
```

The returned attempt contains a normal managed job proposal. Review it and use
the installed `codex-plus job prepare/launch` workflow. The generated command
uses the existing executor and owned-child supervisor. Rehearsals run inexpensive
stage bodies, retain their real execution/cleanup records, and cannot qualify a
shipping handoff. Remove `--rehearsal` only for a reviewed native proof run.

The fixed build order is preflight, frontend, helper, package qualification, then
native tests. Compatibility and cursor capture run after package approval under
their own managed job. AP replay uses a pinned AP checkout and installed bundle;
it does not install AP. An x86 replay of ARM captures is recorded as x86 replay
of native ARM evidence, not execution of ARM binaries on x86.

## Failures and recovery

A failed or interrupted stage keeps its original receipt, log and managed
outcome. Status is read-only. Within a still-open attempt, the runner skips
verified passed receipts; final cleanup seals the attempt and prevents another
run. Failed receipts and interrupted stage starts also prevent replay. There is
no cross-attempt receipt or artifact reuse. Creating a new attempt is not
permission to waive a failed gate or repeat a job whose cleanup is unresolved.

A supervisor's nonzero exit is not proof that its subtree was cleaned. An
interrupted journal cannot be repaired by inventing a finished entry. For that
case, the root first independently verifies cleanup and records the explicit
resolution through the installed `codex-plus job resolve` workflow. The resolution
must include `release_recovery` bindings for the attempt, plan, journal, baseline
and exact socket identity, plus the reviewed subtree-cleanup evidence.

Then the native owner may run:

```sh
python3 -B scripts/release_ownership.py recover \
  --attempt ATTEMPT --execution EXECUTION_DIRECTORY --output ATTEMPT/outputs/recovery-cleanup.json
```

Recovery rechecks source/index seals, managed identity, recorded process absence
and a conservative ownership scan. A live owned process or an unreadable unknown
process still blocks. It sends no signals. Only the recorded socket directory
may be removed, after its boot/device/inode/owner still match. The recovery
receipt preserves the failed outcome; it permits forensic evidence retention,
not successful qualification of a failed build.

## Publication and timing

Publication remains a separate authorized operation. The read-only
`scripts/release_publish.py` checker binds the tag commit, exact GitHub release
ID, five uploaded assets and downloaded handoff bytes. A draft missing from a
list is not evidence that creation failed: resolve the existing tag/node to its
database ID before proceeding. Never create a duplicate release to resolve a
listing delay.

Measure build, test-harness, qualification, recovery and transfer durations
separately. The 0.4.7 ARM total of about 45 minutes already included its roughly
18-minute test-harness build. Additional CPU cores cannot fix orchestration,
fixture setup or cleanup mistakes. Change concurrency or cache reuse only after
measurements and corresponding identity checks justify it.

Before adding a new cache, compare two real attempts with the same plan, profile
and tooling and no intervening build in their shared Cargo target directory.
Record Cargo's compiled/fresh artifact counts, build durations and output hashes.
If the second attempt already reuses workspace outputs, keep the current layout.
If only workspace units rebuild, investigate the changing checkout paths and
mtimes; if dependencies and the helper rebuild too, investigate flags and the
environment first. A stable, verified checkout may be justified by that evidence;
in-place resume and a separate artifact cache are not prerequisites for a correct
fresh attempt. Claim fast retries only after measuring them on each architecture.

## Completion gate for this workflow

Before calling the workflow repeatable, retain evidence of actual managed
success and failure/recovery rehearsals on x86 and ARM, followed by a complete
non-publishable native qualification of the frozen product. Verify export/import,
native result counts, compatibility, captures, AP replay, and generated handoff.
For the next ordinary revision, the plan/version/note may change; release-specific
executable adapters must not be necessary. A newly required tooling fix gets its
own regression check and is reported as such.
