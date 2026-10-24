# 0.4.9 shared release-skill pilot

Both native targets passed final coordinator acceptance against actual archives
and AP sidecars. Publication, release-companion main merge, installation and
session restart have not run. Final archive transfer completed with exit 0 and
its exact digest verified; its duration and whole-release timing are not yet known.

## Frozen inputs and scope

- Product: `651a1eb03a08d69af0fd1cc69252bdff2c8f4b27`.
- Executed tooling: `2a25728ecd84936d4517d04792d22b6bc5665c08`.
- Plan: `98a4eff113990a1889868893ab492b8e02b872e905990e03b04c9ef802ed1b7f`.
- Frontend patch and runtime source match 0.4.8; the helper package version changes.
  Official daemon 0.163.0, coordinator protocol 5 and execution format 1 are retained.
- Rollback/retain is 0.4.8, `agents_md` is null, and no configuration change or
  daemon restart is required. Session restart is required to load the new packaged version.

The shared skill routed work through the project release book. Normal-main sync,
allocation-free native/AP prechecks, frozen product/tooling inputs and a native
ARM assignment through compatibility, cleanup and export were exercised. The ARM
owner continued without intermediate coordinator acknowledgement or permission
barriers. Preparation PRs #13/#14 were merged after the user's explicit direction;
that decision did not authorize publication or rollout.

## Observed qualification

| Check | x86_64 | aarch64 |
| --- | --- | --- |
| Native workload / managed cleanup | 0 / 0, first workload attempt | 0 / 0, first workload attempt |
| Helper tests | 205 passed | 205 passed |
| Frontend executions / unique names | 213 / 196 | 213 / 196 |
| Compatibility workload / managed cleanup | 0 / 0, first workload attempt | 0 / 0, first workload attempt |
| Compatibility gates / recovery scenarios / captures | 5 / 14 / 20 passed | 5 / 14 / 20 passed |
| AP replay tests / captures / busy pairs | 23 / 20 / 2 passed | 23 / 20 / 2 passed centrally on x86 |
| Final actual-archive acceptance | Passed | Passed |

The x86 raw seven-check package report retains six passes and one failed resources
probe. Official and Plus raw bwrap both failed under the same host UID-map denial;
both CLI read-only sandbox probes passed and enforced denied writes. Qualification
records this host limitation without changing the raw failure to a pass.

Advisor mix evidence is bidirectional local/stub coverage, including an actual
0.4.8 TUI/new 0.4.9 wrapper result. Real cross-host Advisor and paid providers are
outside this evidence. AP replay uses released 0.7.35 and invokes no live nudge.

## Corrections and lessons

No skill-induced runtime stop or conflicting project rule was observed. Operator
and reporting corrections still occurred; first-attempt workload success does not
make this a failure-free pilot:

- Host-wide `job list --all` refused model context. The root used its own inventory
  plus native-owner readiness. A summary's `not_required` cleanup interpretation
  was corrected against the implementation; no records were changed.
- A cheap current-version assertion and missing source note were corrected before
  native allocation. Original failure evidence was retained; the corrected source
  then passed 44 packaging and 125 release-tooling checks.
- X86 `release_ownership.py audit --output` initially pointed outside its attempt
  and was refused before auditing. Only the output path was corrected; init and
  preparation were not repeated. Keep audit reports inside the attempt.
- A premature x86 compatibility `audit_scope` call used its empty-baseline fallback
  and reported pre-existing protected processes. The committed `run` creates the
  short allocation and unreadable-process baseline before setup/gates. The root
  preserved the failed audit and used `verify-attempt` before managed launch;
  no baseline was fabricated and no ownership check was weakened.
- ARM private review checks initially read the manifest envelope as the nested
  frozen spec and treated boolean `focused_native_passed` as a count. Checks were
  corrected against actual schemas and dedicated test records, preserving the
  same prepared UUID, raw failures and production recipes. No prepare/launch
  replay was needed. Earlier readiness collector syntax errors were reporting errors.

Procedural corrections live in the
[release protocol](../../release-protocol.md#initialize-and-prepare-the-reviewed-run),
[compatibility recipe](../../../scripts/release_recipes/compat/README.md) and
[checklist](../../release-checklist.md). Prepare completion/acceptance and
next-proposal review commands from inspected checked-in interfaces before the
expensive launch; fill in run-produced values afterward. No new automation or
acceptance gate was added.

## Timing and limits

| Recorded interval | Seconds |
| --- | ---: |
| X86 native managed execution | 1232.473 |
| X86 native completion to compatibility launch | 1045.439 |
| X86 compatibility managed execution | 141.477 |
| X86 compatibility completion to AP launch | 815.621 |
| X86 AP managed execution | 18.039 |
| ARM native managed execution | 3683.772 |
| ARM native completion to compatibility launch | 620.028 |
| ARM compatibility managed execution | 186.106 |

Completion-to-launch gaps include owner review, mechanical checks/preparation and
operator corrections; they are not measurements of pure user approval wait.
Managed, stage and command intervals nest and must not be added. X86 frontend
Cargo reused 1161 fresh artifacts and compiled 153; ARM reused 1157 and compiled
153. These are cache-reusing observations, not a controlled cold/warm comparison
or an end-to-end speedup. Final transfer and whole-release timing remain unknown.

## Evidence boundary

The root's local `.build/release049-pilot` sources are `STATE.json`,
`pilot-record.md`, `final-acceptance/x86.json`, `final-acceptance/arm.json`, the x86
native/compatibility worker summaries, `arm-return/completion-receipt.json`,
`arm-return/reports/final/timing-record.json` and `ap-arm/replay-report.json`.
The ARM completion receipt predates central AP replay and correctly recorded AP
as pending then; the later replay report and final acceptance supply its outcome.

Portable [qualification](qualification.json), [handoff](handoff.json) and
[evidence inventory](evidence-inventory.json) are coordinator-owned outputs prepared
for publication. This note does not itself qualify an archive, authorize
publication/rollout or establish that the pilot has reached its final outcome.
