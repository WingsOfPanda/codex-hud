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

Check compiler discovery and offline license inputs in the packaging subprocess
environment, not only the build shell. For the new runner, the
[packaging prerequisite](release-protocol.md#packaging-preflight-prerequisite)
defines the missing check and blocks another full proof until it passes.

Repair and regression-test a shared recipe before freezing a new tooling revision.
Use the [change-impact rule](release-protocol.md#changes-and-evidence-validity) to
decide which prior evidence remains applicable. Keep each launched attempt immutable.

**Exit check:** the selected checks pass against the actual frozen files and child
environments on both hosts; all required tools, fixture setup and cleanup proofs
are present. Missing checks are blockers, not implied passes.

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
