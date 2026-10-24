# Repeatable native release workflow

Use this reference when the [release book](release-book.md) selects the new native
runner. Check its status before preparing or launching an attempt.

## Qualification status

The native workflow completed the published
[0.4.8 qualification](releases/0.163.0-plus.0.4.8/qualification.json) on both
architectures: product `05f883c228b18830ed93f691460715525b42a825`, executed
tooling `59d85b0fe4074ecc6376dc242e06943b3d3f15b4`, and later read-only
coordinator verifier `65585b488d927d0a86ed26149bb686401f13607b`.
Native tests, actual package inventories, compatibility/captures, AP replay and
the five published assets passed. Failure/interruption rehearsals are supporting
lifecycle evidence at their original identities, not relabeled full builds.

This establishes the exercised command chain, not a turnkey unattended release
launcher or a measured end-to-end speedup. A new product or tooling revision still
needs change-impact review and its required native gates. There is no generic
cross-attempt receipt/artifact transplantation or automatic failed-stage retry.

For the earlier compiler discovery, fixture, recovery and evidence-consumer
failures, use the [0.4.8 retrospective](releases/0.163.0-plus.0.4.8-retrospective.md).
Their raw results remain failed. The [release book](release-book.md) assigns one
continuous native-owner brief through compatibility and export; separate managed
proposal reviews remain mandatory but do not imply missing user permission.

### Packaging preflight prerequisite

The [checked-in packaging preflight](../scripts/release_recipes/native/packaging-preflight.py)
runs after the private upstream checkout is patched and before bwrap/frontend
compilation. It uses the real child environment to invoke the product notice
generator for both helper and upstream, including compiler versions, sysroot
`COPYRIGHT-library.html`, offline Cargo sources and cached license texts. Every
native run must pass it; the previous x86 result does not waive an ARM or changed
product check. `release-preflight.py` remains the separate import/socket/archive tool.

The failure was command discovery: `RUSTC` held an absolute compiler pin, but the
notice generator invokes plain `rustc +VERSION`. The recipe now supplies a small
adapter beside its existing Cargo adapter; only the exact pinned `+VERSION` is
stripped before executing the absolute compiler. The adapter shares the recipe
PATH across stages. Compiler commands already using absolute pins are unchanged;
the new tooling revision still invalidates a controlled old/new warm-cache timing
comparison. Focused regressions cover compiler/version mismatch, standard-library
notices, missing cached licenses and unavailable offline sources.

Root evidence: `.build/release048/root-acceptance/packaging-acceptance.json` and
managed logs `e9885058-4a38-4972-8836-19ea0fd82013` (33 checks) and
`f09c7c61-452a-4869-bdc2-a00455ed0519` (real preflight). The preflight took about
seven seconds and left product and patched-upstream file/index seals unchanged;
it did not compile or qualify a release package.

### Cheap change checks

For every changed user-visible literal, search its old text before freeze and
record a disposition for each hit (updated assertion, intentional negative test,
or unrelated use). Include product tests/patches, native Rust assertions, tooling
footer/HUD/cursor predicates, compatibility recipes and AP classifier tests:

```text
rg -n -F -- OLD_LITERAL PRODUCT/tests PRODUCT/patches TOOLING/scripts/release_recipes AP_SOURCE/src AP_SOURCE/tests
```

When verifier/recipe code changed since the last accepted revision, exercise the
existing producer-shaped corpus: the observed adjacent raw/state timestamp pair,
failed recovery that stays failed, and audit-only false with sealed prior socket
removal. From the selected tooling checkout run:

```text
PYTHON -B -m unittest discover -s tests -p test_release_protocol.py
PYTHON -B -m unittest discover -s tests -p test_release_ap_coordinator.py
PYTHON -B -m unittest discover -s tests -p test_release_compat.py
```

These cover status/export/import, preparation, AP composition/sidecar acceptance
and negative/tampering cases without rebuilding. When unchanged, record the exact
accepted verifier/test evidence instead of adding another gate by habit.

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

The runner invokes `scripts/package-release.py` and its license generator from
`profile.source_root` at `plus_source_commit`; the recipe and its child-environment
construction come from `profile.tooling_root` at `tooling_commit`. Record that
provenance with the plan. A change to a product-snapshot file refreezes the product;
an environment repair confined to the recipe changes tooling. Both require a
change-impact review; changing a label cannot preserve invalidated proof.

## Local preparation

Run the checked-in tools from the pinned tooling checkout. The native owner
supplies the host profile and reviewed plan. These commands do not launch a job:

- `PLAN` is reviewed JSON conforming to [Plan](../release/protocol-v1.md#plan).
- `PROFILE` is host-observed JSON conforming to
  [Native profile](../release/protocol-v1.md#native-profile-and-initialization).
  The native owner supplies real paths and pins; schema examples are not qualified
  host profiles. There is no universal ready-to-launch profile template.
- A runnable native profile additionally requires `native_build`,
  `python.fixture_sites`, exact `pyte` and `wcwidth` entries in
  `python.fixture_imports`, and all eight `release_native.SYSTEM_TOOLS` pins:
  `/usr/bin/git`, `/usr/bin/strip`, `/usr/bin/readelf`, `/usr/bin/ldd`,
  `/usr/bin/getconf`, `/usr/bin/tar`, `/bin/sh`, `/usr/bin/pkg-config`.
  Resolve each executable on that host and use its **canonical absolute path** as
  the `system_tools` key with its SHA256; aliases such as `/bin/sh` may resolve to
  another path. Fixture sites must exist and each import entry supplies the actual
  `origin` and SHA256. These are mandatory for this recipe even where the generic
  evidence schema permits omission. Verify them before allocating an attempt;
  `init` probes fixture imports and `prepare` checks the complete tool pins.
- `HOOK` is a path to a JSON file containing the command for the pinned
  [ownership helper](../scripts/release_ownership.py). Use this shape, replacing
  `PYTHON`, `TOOLING` and `PROFILE_PYTHON_ENV` with the exact absolute interpreter,
  tooling root and environment returned by `release_native.python_env(profile)`:

```text
{
  "argv": ["PYTHON", "-B", "TOOLING/scripts/release_ownership.py", "audit",
           "--attempt", "{attempt}", "--output", "{report}"],
  "cwd": "TOOLING",
  "env": PROFILE_PYTHON_ENV,
  "deadline_seconds": 180
}
```

Print `PROFILE_PYTHON_ENV` from the selected tooling, using the profile interpreter
and loader settings (the output is the hook's `env` object):

```sh
PYTHON -B -c 'import json,sys; sys.path.insert(0,sys.argv[1]+"/scripts"); import release_native as n; print(json.dumps(n.python_env(json.load(open(sys.argv[2]))),sort_keys=True))' TOOLING PROFILE
```

`{attempt}` and `{report}` are literal runner substitutions. This is data selecting
the existing helper, not a generated cleanup script. Preparation validates and
freezes the hook, captures the baseline, and derives final `cleanup` from `audit`.
For a manual `release_ownership.py audit`, keep `--output` inside `ATTEMPT`,
for example `ATTEMPT/outputs/prelaunch-audit.json`. An external output path is
refused before auditing; preserve that failure and correct the path.
Use the profile's exact Python and loader environment for the commands below;
`python3` is shorthand for that pinned interpreter, not an unverified PATH choice.

### Before allocating an attempt

Compose provisional JSON with all plan-bound inputs, then run these checks before
the combined freeze. Use `release_protocol.digest(value)` for canonical JSON pins;
plain file SHA256 is different. A profile mismatch reports the computed digest;
update the reviewed provisional plan, not a sealed attempt.

| Inputs | Owner and source of the values |
| --- | --- |
| Release, product/upstream commits and patch | Coordinator; actual bumped Cargo manifest/lock, product packager constants and patch bytes; `plan-check` validates them |
| Tooling commit and both target/profile digests | Coordinator plus native owners; clean dedicated checkouts and observed host profiles, hashed with the core's canonical digest |
| Previous tag/handoff/archive/manifest/tree and source commit | Published previous handoff plus complete canonical extracted inventory on each host; installed packages may have different modes |
| `compat.fixture_files`, recipe and gate lists | Selected product fixture bytes and selected tooling's `RECOVERY_FIXTURES`/`GATES`; include all four required recovery fixtures |
| Native suite names/counts/ignore reasons | Current test discovery and changed-test inventory; later actual binary list/log reconciliation is still required |
| Official binary pins and child environment | Each native owner; actual native official binary, Python/imports, compiler and system tools, including production libcap discovery |
| AP version/source/plugin/bundle, Node/dependencies | AP replay owner; released installed bundle/registry and matching source/dependency checkout on the replay host |
| Rollout/policy/retention/restart fields | Coordinator and fleet-config; actual in-use matrix and reviewed policy, preserving separate action grants |

On **each native host**, from its pinned tooling checkout:

```text
PYTHON -B TOOLING/scripts/release_protocol.py plan-check --plan PLAN --source-root PRODUCT
PYTHON -B TOOLING/scripts/release_compat.py precheck --plan PLAN --profile PROFILE --old-package CANONICAL_PREVIOUS --cleanup-hook HOOK
```

On the **AP replay host**, with its own profile/interpreter:

```text
AP_PYTHON -B TOOLING/scripts/release_compat.py precheck-ap --plan PLAN --profile AP_PROFILE
```

These commands report readiness to stdout. Save their JSON outside frozen
checkouts. They share the production validators and do not allocate an attempt,
socket or managed record; disposable Git scratch indexes are removed. The native
check probes production libcap even without a private override, validates the
canonical previous package and fixture closure, and verifies the exact cleanup
hook. AP readiness is separate: an ARM host does not need the central AP paths.
Neither result is a qualification receipt. Read `scope` and `not_checked`: notice
generation, new-binary counts/runtime, captures/AP replay, future cleanup and
publication/rollout still need their actual gates. These checks do not prove cache
completeness or replace the packaging preflight after patch application.

### Initialize and prepare the reviewed run

Before the expensive launch, prepare the commands for completion review, local
package approval, compatibility proposal review and final acceptance. Use the
selected checked-in interfaces and inspect their current input/output schemas;
leave only values produced by the run to fill in after completion. Record these
commands with the owner brief so mechanical preparation does not delay the next
authorized phase. This adds no new gate or automation.

After both native readiness checks and AP readiness pass, freeze the combined
plan and prepare near launch. Production commands omit `--rehearsal`:

```sh
python3 -B scripts/release_native.py init --plan PLAN --profile PROFILE
python3 -B scripts/release_native.py prepare --attempt ATTEMPT --cleanup-hook HOOK
```

The returned attempt contains a normal managed job proposal. Review it and use
the installed `codex-plus job prepare/launch` workflow. The generated command
uses the existing executor and owned-child supervisor. An explicitly selected
lifecycle rehearsal adds `--rehearsal` to `init`. Rehearsals run inexpensive
stage bodies, retain their real execution/cleanup records, and cannot qualify a
shipping handoff. They are not a substitute for readiness: rehearsal preparation
can skip system libcap discovery when no private override is supplied.

The fixed build order is preflight, frontend, helper, package qualification, then
native tests. Compatibility and cursor capture run after package approval under
their own managed job. AP replay uses a pinned AP checkout and installed bundle;
it does not install AP. An x86 replay of ARM captures is recorded as x86 replay
of native ARM evidence, not execution of ARM binaries on x86.

After package approval, follow the checked-in
[compatibility/capture and AP commands](../scripts/release_recipes/compat/README.md).
After export/import, use the core's
[acceptance and handoff commands](../release/protocol-v1.md#operator-commands).

## Changes and evidence validity

For each repair, record the old and new commits, reviewed diff, changed input
dependencies, and affected gates. Retain original receipts with their original
identities. Earlier rehearsal evidence may remain supporting lifecycle evidence
when the diff proves its behavior and dependencies unchanged; it is never relabeled
as a run of the new commit. A packaging-only environment repair needs its new
regression/preflight and full packaging proof. Changes to lifecycle, supervision,
receipt, cleanup or recovery behavior require the affected success/failure/
interruption scenarios. If dependency impact is uncertain, repeat that scenario.

The shipping acceptance validator still requires exact plan/receipt bindings;
this review does not override them. Separate the reuse paths:

| Path | Allowed reuse |
| --- | --- |
| Existing helper-only packaging | Only the unchanged native binaries and provenance permitted by [native package requirements](releasing.md#native-binaries) |
| Existing test-only correction | The exact unchanged harness/results under [saved native results](verification.md#saved-native-results) |
| New runner, new attempt | Cargo's normal verified cache reuse; no copying prior attempt artifacts or stage receipts into acceptance |

Historical `5bfcf26a` required a fresh attempt. Its passed frontend/helper
records remain diagnostic evidence, not transferable acceptance receipts.

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

Use the core CLI from the selected verifier checkout, with the exact profile
interpreter/loader. These are separate operations, not one automatic orchestrator:

```text
PYTHON -B TOOLING/scripts/release_protocol.py export --attempt ATTEMPT --output EVIDENCE
PYTHON -B TOOLING/scripts/release_protocol.py import --bundle EVIDENCE --sha256 OWNER_DIGEST --output IMPORT
PYTHON -B TOOLING/scripts/release_protocol.py accept --attempt IMPORT --source-root PRODUCT --archive ARCHIVE --ap-receipt AP_RECEIPT --output ACCEPTED
PYTHON -B TOOLING/scripts/release_protocol.py handoff --plan PLAN --source-root PRODUCT --accept x86_64=X86_ACCEPTED --accept aarch64=ARM_ACCEPTED --output HANDOFF
```

The native owner exports only after its assigned gates and cleanup. The
coordinator waits for the transfer's terminal result, verifies the independently
received digest, and imports read-only. The actual archive is supplied separately;
an evidence export alone cannot pass package acceptance. Use
[compatibility/AP preparation](../scripts/release_recipes/compat/README.md) between
import and full acceptance. Preserve separate execution-tooling and verifier
identities when a reviewed read-only correction is needed.

Publication remains a separate authorized operation. The read-only
`scripts/release_publish.py` checker binds the tag commit, exact GitHub release
ID, five uploaded assets and downloaded handoff bytes. A draft missing from a
list is not evidence that creation failed: resolve the existing tag/node to its
database ID before proceeding. Never create a duplicate release to resolve a
listing delay.

Use the [publication command sequence and input schemas](release-publication.md)
to collect the exact API records, verify the draft and then the public release.
The checker does not create/upload/publish or fetch those records itself.

Use [phase timing and measurement limits](release-performance.md#measurement-limits)
to distinguish execution, transfer and waits. Change concurrency or cache reuse
only after measurements and corresponding identity checks justify it.

Before adding a new cache, compare two real attempts with the same plan, profile
and tooling and no intervening build in their shared Cargo target directory.
Record Cargo's compiled/fresh artifact counts, build durations and output hashes.
If the second attempt already reuses workspace outputs, keep the current layout.
If only workspace units rebuild, investigate the changing checkout paths and
mtimes; if dependencies and the helper rebuild too, investigate flags and the
environment first. A stable, verified checkout may be justified by that evidence;
in-place resume and a separate artifact cache are not prerequisites for a correct
fresh attempt. Claim fast retries only after measuring them on each architecture.
Call the first and second comparable attempts A1 and A2. A run after a tooling,
PATH or profile change is not a controlled A2 comparison; label the difference and
possible cache invalidation. Reuse naturally required runs when comparable rather
than adding release gates solely to collect timings. For a CPU experiment, change
only concurrency within current resource limits and record wall time, memory
pressure and Cargo freshness; keep cold, warm and helper-only results separate.

## Completion gate for this workflow

Before calling the workflow repeatable, retain evidence of actual managed
success and failure/recovery rehearsals on x86 and ARM, followed by a complete
unpublished native qualification of the frozen product. Verify export/import,
native result counts, compatibility, captures, AP replay, and generated handoff.
Only after that acceptance review may separately authorized publication proceed.
For the next ordinary revision, the plan/version/note may change; release-specific
executable adapters must not be necessary. A newly required tooling fix gets its
own regression check and is reported as such.
