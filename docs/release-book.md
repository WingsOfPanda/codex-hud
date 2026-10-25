# Codex Plus release book

Run an authorized release as one continuing task. Passing a phase starts its next
authorized phase; normal observed waiting is not a blocker or a request for another
user “go.” Follow steps 1–6 and copy the [checklist](release-checklist.md) into its
evidence directory. For an interrupted release, first reconcile that
checklist with actual jobs and artifacts, then take the applicable
[recovery branch](#recovery). Continue from the earliest unmet exit check.

## Owners

| Owner | Owns | Returns |
| --- | --- | --- |
| Coordinator | Scope, freeze, x86 execution, acceptance and publication | Accepted matrix, note, handoff and verified tag |
| Independent native ARM session | Local readiness, native build/tests, package approval, compatibility/captures, cleanup and export | Complete native evidence and archive digest, or a concrete unresolved blocker |
| AP owner or pinned replay | Released pane classifier and stall-hash checks | Report bound to native captures and AP identity |
| Fleet-config | Pins, rollout approval, maintenance holds, installation and global settings | Per-host installed version, quiet checks and rollout result |

Discover current relay peers before dispatch. Send the native owner the pinned
recipe, inputs, resource needs, expected duration, log paths and acceptance checks.
SSH transfers this handoff and results; the independent native owner supervises
execution locally. Give it one conditional assignment through export, with named
successors after success, known failure and uncertain cleanup. It reviews each
generated managed proposal before launch; this review is not a request for user
permission already granted. The coordinator independently accepts the final return.
A completed phase alone is not the assignment's return boundary.

Resolve local package approval in that brief: either delegate the committed
`approve-package` decision against exact local bytes and cleanup, or reserve one
named coordinator gate. Never fabricate a “root accepted” flag to avoid a handoff.
Final shipping acceptance remains coordinator-owned in either case.

For an ordinary revision, delegate local package-native approval by default.
`prepare-attempt` performs that validation and writes
`outputs/compat-package-approval.json`; review and record its hash before launch.
Its generated `root_accepted_final_package: true` is a legacy label for that local
package/native-tests prerequisite, not independent coordinator shipping acceptance.
Do not hand-edit it. Reserving a coordinator gate must name a real decision and
acknowledge the additional transfer/review dependency.

Include a **plan superseded** successor. For a correction inside the requested
behavior and existing grant, the coordinator reviews its dependency impact and
issues the replacement plan/commit digests. The native owner verifies that update,
preserves retired preparation, reruns affected readiness checks and reviews a fresh
attempt without a new brief or user go. Existing running jobs retain their own
identity and cleanup obligations. Tests/fixtures can change the frozen product
commit and invalidate gates without changing the requested product contract;
escalate only a change outside that contract or the existing authority.

The brief must bind release/gates, product/tooling/verifier commits, host profile,
canonical previous package, commands, resources, deadlines, ownership, allowed
corrections, evidence paths and the exact conditions needing escalation. One
execution owner and observer handle each job. Native subagents remain distinct:
they hand expensive/hardware/service launches to their root under the
[reviewed-job boundary](detached-jobs.md#assignment-boundary).

Read the [0.4.8 retrospective](releases/0.163.0-plus.0.4.8-retrospective.md)
when changing this workflow; it separates observed failures from proposed fixes.

## Before a new release: sync normal checkouts

Run this sync only on an explicit sync request or as part of authorized new release
preparation. Each participating host's owner updates its ordinary `main` checkout
before step 1. This is not a timer or background sync.
For a status question, compare HEAD with remote `main` read-only and report any lag.
For a resume, preserve the existing plan's source/tooling pins and reconcile its
jobs first; a newer book does not refreeze an active release.

Confirm the checkout is owned by this task, is on `main`, is clean, and has no
active job or frozen evidence depending on its identity. Frozen product/tooling
worktrees stay untouched; fetch only from the normal checkout or a separate
unsealed clone. Then run from that normal checkout:

```sh
set -e
export GIT_OPTIONAL_LOCKS=0
test "$(git symbolic-ref --quiet --short HEAD)" = main
release_main_status=$(git -c core.fsmonitor=false status --porcelain=v1 --untracked-files=normal)
test -z "$release_main_status"
timeout 60 git fetch --no-tags origin refs/heads/main:refs/remotes/origin/main
git merge-base --is-ancestor HEAD refs/remotes/origin/main
git -c core.fsmonitor=false merge --ff-only refs/remotes/origin/main
release_main_status=$(git -c core.fsmonitor=false status --porcelain=v1 --untracked-files=normal)
test -z "$release_main_status"
release_main_remote=$(timeout 60 git ls-remote --exit-code origin refs/heads/main)
test "$(git rev-parse HEAD)" = "${release_main_remote%%[[:space:]]*}"
git rev-parse HEAD
```

A dirty, diverged, detached, wrong-branch or in-use checkout is preserved: report
the concrete condition and use an agreed clean checkout if preparation can
continue there. Never reset, stash, switch branches or force-update to make this
check pass. If remote `main` moves during comparison, recheck ownership and local
state before a bounded refresh; report a continuing mismatch instead of looping.
When remote access fails, continue local reads/preparation but mark sync unverified.

**Exit check:** each owner records the normal checkout path, matching full local
and remote commits, clean status and the release-book revision read. The coordinator
compares the x86 and native ARM owners' receipts: both must report the same full
`main` commit and release-book revision before selecting/freezing release inputs.
If the reports differ, reconcile the ordinary clones with the bounded sync above;
matching branch names or separate claims of being up to date are insufficient.

The coordinator then names the exact product/tooling revisions for dedicated
release worktrees.
Those frozen revisions are selected independently and need not equal synced `main`.
Unmerged improvements are not supplied by syncing `main`: merge them through their
reviewed PR first, or explicitly select and transfer their reviewed commit as
tooling. Before native launch, each owner's existing readiness receipt verifies
the shared plan's product, tooling, upstream and patch identities. Host paths,
architecture and profile digests may differ; the selected code revisions must
match. Include this in the existing owner handoff, without another per-phase
acknowledgement. Resumes continue with their original frozen pins.

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
| Helper-only revision | [Qualified binary reuse](releasing.md#native-binaries) only through a separately qualified path with its own accepted handoff producer; this native runner does not compose reused cross-attempt receipts. Otherwise select the full native path |
| Test-only diagnosis | [Saved native results](verification.md#saved-native-results) can verify a correction on the exact unchanged harness; that diagnostic does not qualify a new native-runner plan |
| New native runner | [Qualification status](release-protocol.md#qualification-status), then [local preparation](release-protocol.md#local-preparation); use only the operations its status permits |

A prior release's green result is evidence, not a launch recipe. The current
reference is [0.4.8 qualification](releases/0.163.0-plus.0.4.8/qualification.json).
Check the actual checkout and entry points: a clean but old `main` is not the latest
qualified release. Read the book and recipes from the selected tooling checkout.
Its acceptance report does not supply a portable, qualified replacement controller.
If the selected entry point or reviewed input closure is missing, record recipe
selection as blocked and complete that preparation; do not infer a fallback.

Record candidate product, upstream, patch, tooling and host-profile identities.
Finalize their combined freeze after step 2 readiness. Record the
commit supplying the packager/license generator using the
[provenance rule](release-protocol.md#ownership-and-inputs). Inspect and preserve
unrelated dirty work. Use a scratch Git index when real index metadata is sealed.
Even a status check can refresh the real index: compare its bytes and metadata
through the scratch-index helper, and preserve any discrepancy instead of
rewriting the seal or claiming unchanged qualification.
Keep dedicated product and executed-tooling checkouts untouched from native
preparation through compatibility; keep AP source/tooling untouched from AP
preparation through replay/composition. Do not edit or commit in them, copy the
working checklist into them, or let an IDE/shell prompt refresh their real indexes.
Keep checklists, notes, provisional inputs and later documentation commits in a
separate coordinator evidence directory or worktree. Retain those checkout paths
until every dependent job and verification has finished.
Use the supported `release_native.py verify --attempt ATTEMPT` for frozen native
attempts and `release_compat.py verify-ap --attempt AP_EVIDENCE` for prepared AP
contexts; both call the disposable-index implementation. Before freezing, use
`git --no-optional-locks -c core.fsmonitor=false` for necessary read-only Git checks
and set `GIT_OPTIONAL_LOCKS=0` in inspection shells, not by changing a frozen job's
reviewed environment. This guard complements full seal comparison. A direct
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

## 2. Prove stable readiness, then freeze

Resolve the stable prerequisites for the **whole** release on both hosts before
freezing the combined plan. An early check validates setup; it does not replace
later checks against the actual candidate binaries and captures.

| Prerequisite | Evidence before expensive native work |
| --- | --- |
| Child environment/imports | Profile Python/loader and exact module origins, required references such as `HOME`, actual fixture constructors and raw socket lengths |
| Compiler/library/license discovery | Packaging child's compiler command, real libcap pkg-config/header/library paths, offline Cargo metadata filtered to the native target, offline license inputs and [packaging preflight](release-protocol.md#packaging-preflight-prerequisite) |
| Package copies/reference | Actual source/scratch devices; independent copies across the real filesystem boundary; previous digest-pinned archive extracted privately with complete canonical manifest/mode/tree checks |
| UI/version contracts | All affected Rust, terminal and cursor predicates match intended behavior; version-transition tests use the actual bumped version; expected counts come from current discovery |
| Timed integration helper | Standard optimized helper; debug units do not establish launch/hash timing under integration deadlines |
| AP replay | Released source/bundle/registry pins, Node/dependency origins, exact Python and replay owner; confirm required AP installation with fleet before waiting for captures |
| Evidence/publication consumers | Producer-shaped success/failure cleanup proofs survive status/export/import/acceptance; supported command signatures; correct repository, credentials and release destination |

Use the existing [preflight checks](release-performance.md#run-the-preflight);
there is no single command that currently covers this entire table. Mark checks
that require new artifacts as explicit later gates. Missing checks are not passes.
Use the [field sources and allocation-free checks](release-protocol.md#before-allocating-an-attempt)
to compose all plan-bound inputs and check each native host plus the AP replay
host before the combined freeze. Save the returned scope and unchecked gates;
`ready: true` is prerequisite readiness, not release acceptance. A short lifecycle
rehearsal is optional and does not replace the production prerequisite check.
When UI/version predicates or evidence consumers change, run the concrete
[label inventory and producer-corpus checks](release-protocol.md#cheap-change-checks).
They need no full native rebuild. When unchanged, record the exact previously
accepted verifier/test evidence rather than adding a gate by habit.
For a changed fixture, reproduce its failing condition before refreezing: a
same-filesystem copy does not test the ARM `EXDEV` boundary, and an exact-match
synthetic result does not test timestamp round-trip differences.

After stable readiness, allocate short paths and capture volatile ownership
baselines close to the host's local review/launch. Do not age a baseline while
waiting for another host's preparation handshake. Both hosts still use the same
accepted combined plan. If a new unreadable login invalidates an unlaunched
preparation, preserve it, establish the exact identity, and create fresh normal
preparation; never rewrite the old baseline or exempt SSH processes as a class.

Repair and regression-test shared recipes before a new tooling freeze. Use the
[change-impact rules](release-protocol.md#changes-and-evidence-validity); keep
launched attempts immutable. Stage canonical previous packages outside attempts;
installed fleet packages may have intentionally removed write bits.

**Exit check:** every table row has a passed check or an explicit artifact-dependent
later gate; both stable profiles are ready and the combined plan is frozen.
Each native owner reviews its actual prepared proposal immediately before launch.
Rehearsals and setup checks do not qualify a package.

## 3. Execute native work

Follow [reviewed jobs](detached-jobs.md#agent-workflow). Review argv, environment,
input digest, resources, deadlines and cleanup before launch. Record exact execution
IDs and arm completion observation. Use the [wait workflow](shell-workflow.md) when
only registered work remains.

Compare frozen inputs in the generated proposal's order, then verify its complete
external closure; do not reconstruct numeric snapshot paths by sorting serialized
closure keys. Preserve schema types, including octal mode strings. Use existing
core verification instead of another ad-hoc inventory converter.
Send one launch receipt and one final result/blocker. A delayed relay snapshot
does not supersede a newer exact execution record or justify a duplicate launch,
watcher or status-only response loop.

Established independent recipes run in parallel within resource reservations.
If native recipe/fixture files changed since the last accepted execution tooling,
the coordinator may choose an x86-first cost check. The brief must name the exact
affected stage/test receipt, its hash, who sends it and a bounded wait/escalation
condition. This is a cost tradeoff, not a new authority gate or an unnamed wait;
without that declared dependency, ARM launches after its own review. Build the complete native
package defined by [releasing](releasing.md#native-binaries), retaining required
test binaries and hashes before shared Cargo outputs can be overwritten.

When native results and independent cleanup pass, the same independent native
owner prepares/reviews/launches compatibility and captures using its accepted local
package. The coordinator need not first download the archive or acknowledge the
phase unless the brief named a concrete decision. Native `run` already composes its
five stages; use the existing controller, not manual stage launches.

**Exit check:** required build/package stages passed and output identities match
the frozen inputs; cleanup is independently verified. Any failed/unknown stage
takes the recovery branch before dependent work. Reuse only through the helper-only
or test-only paths when their separate producer is qualified; the current native
composer does not accept their reused receipts. New-runner receipts and artifacts
never transfer between attempts. Cleanup resolution alone does not pass a gate.

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

The native owner returns after all assigned gates and cleanup, with one evidence
export and the archive transferred separately. Wait for the exact transfer's
completion before hashing or importing it; a growing file is not package corruption.
Use the same export/import route for local x86 evidence, without a network copy;
this gives AP replay and final acceptance an immutable imported context on both
architectures. Do not recreate native receipts in the coordinator directory.
Use supported core `accept` and `handoff` commands with the profile interpreter
and declared paths, rather than per-release scripts calling guessed private APIs.
See the [command sequence](release-protocol.md#publication-and-timing).

For frontend changes, run [terminal verification](verification.md) and the selected
native capture/AP recipe. The new runner's
[compatibility and AP commands](../scripts/release_recipes/compat/README.md)
bind native captures to the released classifier; replay-host architecture remains
distinct from capture architecture. AP installation prerequisites were checked in step 2.

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

Keep sealed acceptance/readiness files immutable; write later publication state
to a separate output. A mode-0400 evidence record is not a mutable status file.

Follow the [publication commands and record schemas](release-publication.md) for
both a new publication and an interrupted one. Complete this sequence within the
existing publication grant:

1. Bind the accepted product, companion commit, handoff and five files: two
   archives, two checksum files and `handoff.json`. Prepare the companion release
   body with tag-bound links; the packaged source note stays frozen.
2. Reconcile the remote tag and release. Create/push a missing tag at the reviewed
   companion commit; preserve any existing tag and resume the same release ID.
3. Upload a draft and resolve its tag/node to the exact database ID. A listing
   delay does not justify creating another release.
4. Verify the draft's remote tag commit, all five asset digests/sizes and the
   byte-identical downloaded handoff with the checked-in publication checker.
5. Publish that verified database ID. Read back public state, the tag and handoff;
   run the checker again without `--draft`.
6. Send fleet-config the public tag, URL and verification reference. Record main
   merge separately, then proceed to rollout and actual-session verification.

**Exit check:** the public release ID, tag and five assets match accepted evidence;
fleet-config has the verification reference. Record main merge separately.

## 6. Close rollout and measure

Fleet-config owns cutover. Provide fresh central Advisor in-flight checks when
requested, stop new work during the agreed switch and finish the active turn.
For qualified bidirectional Advisor mixes, drain calls involving each box during
its install; installing the central host requires draining calls through it. Use
release-specific mix evidence rather than a permanent fleet-wide pause rule.
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

## Traps recorded by release pilots

The 2026-10-24 0.4.9 pilot passed both native workloads on their first attempts,
but exposed operator traps. See its [pilot record](releases/0.163.0-plus.0.4.9/pilot-notes.md).

| Trap | Earliest preventive check |
| --- | --- |
| An ownership audit output outside its attempt is refused before auditing. | Keep manual audit output under `ATTEMPT/outputs`; inspect the selected CLI before launch. |
| Compatibility completion audit was invoked before `run` created its baseline, reporting pre-existing protected processes. | Use `verify-attempt` before launch; run the completion audit only at its documented lifecycle. |
| Private review confused the manifest envelope with its nested spec, and a boolean with a test count. | Inspect current output schemas and prepare acceptance commands before compilation; read counts from dedicated test records. |
| A separate checklist was only materialized at qualification close. | Copy the checklist before step 1 and update it alongside the execution journal. |

No older recovery or rollback guidance was removed: this pilot did not establish
that it is obsolete for every retained version or resumed attempt.

## Recovery

First identify the actual installed executor, active instructions and exact watch.
The candidate policy under test cannot authorize an older executor's continuation.
A failure pauses its dependency, not the entire authorized release.


| Observation | Branch and completion criterion |
| --- | --- |
| Preparation fails | Preserve it, diagnose and correct within scope; pass the affected setup check and continue with fresh reviewed preparation where required. |
| Product/test fails | Preserve the original result; review the change and invalidated gates under the selected path's reuse rules. |
| `RECOVERY REQUIRED` | Follow [bounded recovery](recovery.md) under current authority; keep affected claims until verified cleanup and continue independent authorized work. |
| Cleanup uncertain or observation lost | Inspect exact process/descendant/socket/source evidence through [status and cancellation](background-work.md#status-and-cancellation); retain unknowns until resolved. |
| `REPORT ONLY` | Report and inspect read-only; establish positive continuity under the actual installed policy before affected follow-ups. Legacy/manual/restart authority barriers retain their required renewed direction. |
| Footer says stale | Check actual execution/result and observer health; classify running, ended or unknown. Age alone authorizes no termination or replay. |

Passing a phase, diagnosing a known clean failure, or preparing the next reviewed
job does not itself require another user go. Escalate only a real missing grant,
changed product contract, unavailable access/resource or unresolved ownership/effect.
Respect new stop/pause/cancel instructions. Keep retries bounded by changed evidence
and a supported correction, not a blind retry count or repeated notifications.

The coordinator checks existing authorization before supported record resolution;
ask only for missing direction, naming the exact action and reason. The native
owner supplies cleanup proof. For the new runner, follow its
[recovery contract](release-protocol.md#failures-and-recovery); original failed
receipts remain failed. Record the earliest preventive check for the supported
cause in the owning recipe or reference doc, then return to the earliest unmet
exit check.

For adjacent timestamps or audit-only removal flags, follow the
[evidence comparison rules](recovery.md#evidence-comparison-rules) and exercise the
whole status/export/import/acceptance chain. Preserve both original records.

For a read-only coordinator defect discovered after successful qualification,
record the changed verifier commit separately from the product and executed
tooling commits. Review the impact and run affected verifier/transport tests
against the preserved real evidence. Rebuild only if the change invalidates a
product or execution gate; keep the original plan, receipts and failed checks.
Current `run-ap` uses the corrected verifier. The old frozen-0.4.8 adapter is a
specialized compatibility path, not a generic fallback; use its
[retained-tooling contract](../scripts/release_recipes/compat/README.md#retained-tooling-ap-receipt-adapter)
and keep the verifier checkout frozen through composition. Full actual-archive
and sidecar acceptance remains the completion criterion.

The target is zero **avoidable** user resumptions and duplicated work. Existing
CLIs are not a turnkey unattended publisher. Failed/interrupted/sealed native
attempts cannot transfer receipts/artifacts to a new attempt under the current
runner; normal verified Cargo cache reuse remains available. New cross-attempt
reuse needs implementation and qualification, not softer wording in this book.
This also applies to failed/interrupted compatibility: diagnose and verify cleanup,
then use a corrected, fresh reviewed native attempt; there is no supported
in-place compatibility retry. Budget for that cost, and exercise changed fixture
setup cheaply before launching native work.
