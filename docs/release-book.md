# Codex Plus release book

For a new release, follow steps 1–6 and copy the [checklist](release-checklist.md)
into its evidence directory. For an interrupted release, first reconcile that
checklist with actual jobs and artifacts, then take the applicable
[recovery branch](#recovery). Continue from the earliest unmet exit check.

## Owners

| Owner | Owns | Returns |
| --- | --- | --- |
| Coordinator | Scope, freeze, x86 execution, acceptance and publication | Accepted matrix, note, handoff and verified tag |
| Native ARM session | Local preparation, build, tests and cleanup | Archive and separate hashed evidence bundle |
| AP owner or pinned replay | Released pane classifier and stall-hash checks | Report bound to native captures and AP identity |
| Fleet-config | Pins, rollout approval, maintenance holds, installation and global settings | Per-host installed version, quiet checks and rollout result |

Discover current relay peers before dispatch. Send the native owner the pinned
recipe, inputs, resource needs, expected duration, log paths and acceptance checks.
SSH transfers this handoff and results; the native owner supervises execution
locally. Give each worker one bounded deliverable and finish its assignment when
the result or blocker is known.

## 1. Select the path and freeze inputs

Record the version, user-visible changes, owners and existing authorization for
preparation, publication, merge and rollout. Preserve the actual scope of each
grant: approval for one release/action does not imply another, while an explicitly
broader grant remains valid. Fleet-config owns its rollout approval. Name the
user/session that supplies any missing recovery direction; see [recovery](#recovery).

Select a path before scheduling work:

| Case | Read and record |
| --- | --- |
| Full frontend/product build | [Native package requirements](releasing.md#native-binaries), plus the recipe entry point, tooling commit, input closure and applicable acceptance evidence |
| Helper-only revision | [Qualified binary reuse](releasing.md#native-binaries); record original compile provenance and all matching identities |
| Test-only correction | [Saved native results](verification.md#saved-native-results); identify the unchanged executable and affected checks |
| New native runner | [Qualification status](release-protocol.md#qualification-status), then [local preparation](release-protocol.md#local-preparation); use only the operations its status permits |

A prior release's green result is evidence, not a launch recipe. The historical
reference is [0.4.7 qualification](releases/0.163.0-plus.0.4.7/qualification.json),
with product/fixture source `d25cc2470202edd3ed917fac93ad22f52e4e5a2d`.
Its acceptance report does not supply a portable, qualified replacement controller.
If the selected entry point or reviewed input closure is missing, record recipe
selection as blocked and complete that preparation; do not infer a fallback.

Freeze product, upstream, patch, tooling and host-profile identities. Record the
commit supplying the packager/license generator using the
[provenance rule](release-protocol.md#ownership-and-inputs). Inspect and preserve
unrelated dirty work. Use a scratch Git index when real index metadata is sealed.
Even a status check can refresh the real index: compare its bytes and metadata
through the scratch-index helper, and preserve any discrepancy instead of
rewriting the seal or claiming unchanged qualification.
Use `release_native.checkout_identity` for worker provenance checks too. A direct
`git status` after preparation replaced three real indexes during 0.4.8, despite
identical index bytes. Compare the full file inventory, index bytes and metadata;
retain the discrepancy and retire an unlaunched preparation whose seals changed.
Before an expensive build, run version-transition checks against the actual bumped
source: synthetic “next version” fixtures must derive a different version from it.
With fleet-config, establish actual in-use versions, rollback target, retention,
config defaults and restart requirements before choosing the qualification matrix.

**Exit check:** each architecture has a named recipe entry point, pinned input
closure, applicable acceptance evidence, owner and authorized scope. Every required
gate is listed in the checklist; unqualified workflow experiments are labeled as
such and cannot produce shipping acceptance.

## 2. Prove preparation before compiling

Run the selected recipe's actual setup on each host. Use
[preflight guidance](release-performance.md#run-the-preflight) for interpreter/import
origins, staged files, raw socket paths and private permissions. Include actual
fixture constructors, package-backed helpers, test discovery/result parsing,
resource exclusions and owned-process cleanup. Record host limitations explicitly.
Record the devices containing the source package and fixture scratch roots.
Copies between `/data` and `/tmp` must support different filesystems; test the
actual topology before a native launch. Do not require hard links for a private
package copy or assume that an atomic rename can cross devices.
When constructing a clean test environment, retain variables required by the
tests' explicit environment-reference contracts, including `HOME`. A missing
caller variable is a harness failure; preserve it and correct the child
environment before rerunning, without weakening the test.
Use the standard release-profile helper for timed native integration checks.
Debug unit tests remain useful, but unoptimized binary hashing can dominate ARM
launch time. Measure the actual path before changing a deadline; preserve the
debug failure and qualify the optimized helper with the unchanged assertions.

Check compiler discovery and offline license inputs in the packaging subprocess
environment, not only the build shell. For the new runner, the
[packaging prerequisite](release-protocol.md#packaging-preflight-prerequisite)
defines the early check that must pass before each native build.

Set native executable modes to their packaged modes before recording provenance;
private parent directories remain `0700`. Exercise the package failure-receipt path
as well as success: a mode mismatch must not obscure the original failed gate.
When HUD wording changes, update the exact footer assertions alongside it and
verify the changed contract on the native client before running the full matrix.
Run the affected native Rust filters too: a passing terminal fixture does not
verify their assertions. Keep an assertion-only correction distinct from a
production rendering change; neither permits rewriting a failed receipt.
Cursor-capture recipes have their own footer assertions. Replay their actual
checks against saved screen/cursor pairs when wording changes; update the exact
new contract rather than accepting both obsolete and current labels. Keep that
diagnostic replay separate from fresh candidate qualification.

Repair and regression-test a shared recipe before freezing a new tooling revision.
Use the [change-impact rule](release-protocol.md#changes-and-evidence-validity) to
decide which prior evidence remains applicable. Keep each launched attempt immutable.

Keep native preparation and launch close together. If a new unreadable host login
invalidates preparation before any managed job or workload exists, preserve that
attempt, establish the new process's exact identity, and use normal initialization
for a fresh before-work baseline. Never rewrite the old baseline or exempt SSH
processes as a class. A conditional local launch review must name the exact allowed
attempt-binding and baseline changes; any other difference needs review.

Stage the previous release from its digest-pinned archive into a private directory
outside the attempts, preserving archive modes. Verify its complete file inventory
before compatibility preparation. Installed fleet copies may have write bits
removed; do not change their modes or use them where canonical archive modes are
required. Check the reference independently on both architectures.

**Exit check:** the selected checks pass against the actual frozen files and child
environments on both hosts; all required tools, fixture setup and cleanup proofs
are present. Both targets have non-rehearsal prepared attempts and reviewed
proposals under the same combined plan before either build launches. A rehearsal
that skips a native dependency probe does not satisfy that prerequisite. Missing
checks are blockers, not implied passes.

## 3. Execute native work

Follow [reviewed jobs](detached-jobs.md#agent-workflow). Review argv, environment,
input digest, resources, deadlines and cleanup before launch. Record exact execution
IDs and arm completion observation. Use the [wait workflow](shell-workflow.md) when
only registered work remains.

Established independent recipes may run in parallel within resource reservations.
For a changed shared recipe, prove the affected x86 path before paying for the
full ARM run; ARM preparation can proceed independently. Build the complete native
package defined by [releasing](releasing.md#native-binaries), retaining required
test binaries and hashes before shared Cargo outputs can be overwritten.

**Exit check:** required build/package stages passed and output identities match
the frozen inputs; cleanup is independently verified. Any failed/unknown stage
takes the recovery branch before dependent work. A separately accepted historical
artifact requires an explicit supported acceptance decision; resolving cleanup
alone does not satisfy this gate.

## 4. Qualify the extracted packages

Validate the archive digest and member safety using the
[archive preflight](release-performance.md#run-the-preflight), then verify the
complete extracted manifest, modes, tree and native executables. Preserve trusted
archive modes with `tar --same-permissions`; private homes and socket directories
remain `0700` independently of package modes.

Execute the frozen native/package matrix with private homes and the official stock
daemon. Include rollback over new records, both-direction holds, actual in-use
old-TUI/new-helper mixes and affected Advisor/Goal/Ultracode behavior. Reconcile
libtest `--logfile`, independent `--list`, exit status, counts and ignored reasons
as specified in [result handling](release-performance.md#cleanup-and-result-handling).

For frontend changes, run [terminal verification](verification.md) and the selected
native capture/AP recipe. The new runner's
[compatibility and AP commands](../scripts/release_recipes/compat/README.md)
bind native captures to the released classifier; replay-host architecture remains
distinct from capture architecture. Confirm any required AP installation with fleet.

**Exit check:** the coordinator has verified every mandatory gate against the same
candidate identities and clean cleanup evidence. Each skipped/limited check has an
explicit reviewed disposition; required failures remain blockers. Worker summaries
and development-only captures cannot substitute for candidate acceptance.

## 5. Publish accepted artifacts

Generate the note and handoff from accepted identities using the
[handoff contract](releasing.md#qualification-and-handoff). Record product versus
companion/tag commit separately. Commit durable evidence under `docs/releases/`
with the release note; retain original logs privately where appropriate. Local
`.build` paths are working references, not portable published proof.

Within publication authorization, upload the two archives, two checksum files and
handoff. Verify all five GitHub asset digests and downloaded handoff bytes. Resolve
a draft's tag/node to its exact database ID and continue with that release; never
create a second release to work around listing delay. After public verification,
send fleet-config the tag plus the digest-verification reference.

**Exit check:** the public release ID, tag and five assets match accepted evidence;
fleet-config has the verification reference. Record main merge separately.

## 6. Close rollout and measure

Fleet-config owns cutover. Provide fresh central Advisor in-flight checks when
requested, stop new work during the agreed switch and finish the active turn.
Record its per-host results. Verify the actual executable of each required
restarted TUI; switching the installed package does not replace a running frontend.

Record [phase timings](release-performance.md#measurement-limits), overlap, cache
conditions and review/recovery waits in the checklist. For CPU/cache experiments,
use the [comparison method](release-protocol.md#publication-and-timing); report
measured improvement or “not measured,” not a whole-release estimate from one stage.

**Exit check:** every rollout target has an operator-confirmed result and released
hold, and required restarts have verified outcomes. Otherwise report rollout or
restart as pending while retaining the separate published-release status. Archive
the completed checklist and assign each remaining follow-up an owner. For every
problem encountered, complete the checklist's incident record and update the
owning recipe or linked guidance with the verified cause and earliest preventive
check. Keep unresolved hypotheses labeled as such; record lessons before closing
the release, without changing already published artifacts.

## Recovery

| Observation | Branch and completion criterion |
| --- | --- |
| Preparation fails | Repair the cause; pass the affected setup check before compilation. |
| Product/test fails | Preserve the original result; review the change and invalidated gates under the selected path's reuse rules. |
| `RECOVERY REQUIRED` | Follow [bounded recovery](recovery.md) under current authority; keep affected claims until verified cleanup and continue independent authorized work. |
| Cleanup uncertain or observation lost | Inspect exact process/descendant/socket/source evidence through [status and cancellation](background-work.md#status-and-cancellation); retain unknowns until resolved. |
| `REPORT ONLY` | Report known results and wait for explicit renewed user direction before old follow-ups, even if preparation was previously authorized. |
| Footer says stale | Check actual execution/result and observer health; classify running, ended or unknown. Age alone authorizes no termination or replay. |

The coordinator checks existing authorization before supported record resolution;
ask only for missing direction, naming the exact action and reason. The native
owner supplies cleanup proof. For the new runner, follow its
[recovery contract](release-protocol.md#failures-and-recovery); original failed
receipts remain failed. Record the earliest preventive check for the supported
cause in the owning recipe or reference doc, then return to the earliest unmet
exit check.

Compare both the raw managed result and the copy stored in state before recovery.
Their `finished_at` floats can differ by one representable step after a JSON
round trip. The recovery validator permits only that finite, adjacent-float
difference; every other result field must match. Preserve both original files,
record the validator revision, and keep cleanup resolution separate from package
acceptance. A larger timestamp difference or any outcome/identity difference
requires investigation, not normalization of the records.
Exercise recovery status, export and import with the same preserved pair too;
passing cleanup validation alone does not prove the forensic transport path.

An audit and a removal receipt answer different questions. The compatibility root
audit can report `socket_removed: false` because native final cleanup already
removed that socket. Accept this only with the exact socket identity, hash-bound
original removal receipt and cleanup seal, clean ownership audit, and current
path absence on the owning host. Test this producer-shaped case through status,
export, import and AP receipt acceptance; a synthetic `true` audit missed it in 0.4.8.

For a read-only coordinator defect discovered after successful qualification,
record the changed verifier commit separately from the product and executed
tooling commits. Review the impact and run affected verifier/transport tests
against the preserved real evidence. Rebuild only if the change invalidates a
product or execution gate; keep the original plan, receipts and failed checks.
If the old AP wrapper embeds the defective receipt writer, use the committed
`release_ap_coordinator.py` adapter: it runs the original replay with original
supervision, then validates actual managed results and independent cleanup before
writing a separate receipt. Its completion criterion is full acceptance with the
actual archive and AP sidecar; preparing an adapter is not AP qualification.
