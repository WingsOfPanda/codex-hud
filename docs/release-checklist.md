# Release checklist template

Copy into the release evidence directory. Follow the [release book](release-book.md).
Use `pending`, `running`, `passed`, `failed`, `blocked`, or `not applicable` with a
reason. Replace placeholders with actual evidence; an unchecked item is not a pass.

## Identity and scope

- Release / user-visible change:
- Coordinator / native x86 owner / native ARM owner / fleet peer:
- New-release sync: each owner's normal checkout / local and remote main SHAs /
  clean status / release-book revision (resume keeps existing frozen pins):
- Cross-host comparison: coordinator/x86 and native ARM receipts agree on one full
  main SHA and book revision; both readiness receipts bind the selected plan's
  product/tooling/upstream/patch identities:
- Product commit / upstream commit / patch SHA256:
- Executed tooling commit / coordinator verifier commit / AP adapter revision:
- Plan digest / host profile digests / coordinator-issued superseding-plan successor:
- Packager and license-generator supplying commit / child-environment identity:
- Previous and in-use versions / qualified rollback / proposed retain:
- Config changes and defaults / exact policy block hash or unchanged:
- Preparation / publication / merge / rollout authorization references:
- Grant scope (action/release or explicit broader scope) / recovery authorizer:
- Selected path / pinned recipe entry point / closure / applicable acceptance:
- New runner qualification status if selected / unresolved launch prerequisites:
- Actual installed executor, running TUI and active recovery-policy version:
- Independent native owner's end-to-end assignment / successful-phase successors:
- Which reviews the native owner performs locally / final coordinator return boundary:
- Exact escalation conditions and existing recovery/publication authority:
- Stable readiness on both hosts / volatile baseline-to-launch interval:
- Native `precheck` results on both hosts / AP `precheck-ap` result and remaining gates:
- Dedicated frozen checkout lifetime / separate mutable evidence directory:
- Completion/acceptance and next-proposal review commands prepared before launch,
  using inspected checked-in interfaces/schemas ([procedure](release-protocol.md#initialize-and-prepare-the-reviewed-run)):

## Acceptance matrix

Every result reference should identify its source/inputs, actual execution and
artifact hashes. Record x86 and ARM separately, even when one check replays both
architectures' evidence on the same host.

| Gate | Owner | x86 status + evidence | ARM status + evidence |
| --- | --- | --- | --- |
| Source/tooling/profile freeze and clean isolated workspace | | | |
| Actual interpreter/import/tool/compiler discovery | | | |
| Packaging child environment, offline license inputs and required early check | | | |
| Fixture sockets, permissions and package-backed helper setup | | | |
| Resource/exclusion check and cleanup preflight | | | |
| Future-phase inputs: canonical previous package, AP dependencies, publication destination | | | |
| Allocation-free native/AP readiness scopes and explicitly unchecked gates | | | |
| Producer-shaped status/export/import/acceptance success and failure paths | | | |
| Changed-label inventory: old-literal hits across Rust/terminal/cursor/AP, disposition per hit | | | |
| Managed proposal review / execution UUID / observation | | | |
| Frontend, code-mode host and helper | | | |
| License generation and complete archive | | | |
| Extracted manifest, modes, versions and native tests | | | |
| Rollback over new records and both-direction holds | | | |
| Actual old-TUI/new-helper mix and affected features | | | |
| Candidate terminal captures / cursor / busy pairs | | | |
| Released AP classifier and stall-hash replay | | | |
| Independent cleanup and original result review | | | |
| Coordinator acceptance / remaining limits | | | |

## Failure and reuse record

- Failed stage, exact error and original result/log:
- Supported cause and smallest repair:
- Cleanup state / report barrier / renewed direction if required:
- Exact recovery evidence and supported resolution:
- Preserved accepted artifacts and identity checks permitting reuse:
- Invalidated gates and why / next attempt identity:
- Old/new tooling diff / retained rehearsal evidence at original identity:
- One preventive check added before the next expensive run:
- Notification kind / actual observer-policy authority (not candidate policy):
- Next authorized action taken, or concrete missing evidence/decision and its owner:
- Product, fixture/executed-tooling, read-only-verifier or orchestration error:

Repeat this incident row for each problem encountered. Link the correction in the
owning recipe/reference rather than duplicating its procedure here.

| Problem and evidence | Verified cause or unresolved hypothesis | Corrective change / earliest preventive check | Check result | Measured interval and source, or not measured | Owner / follow-up |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## Publication and rollout

- [ ] Both archive/manifest/tree identities agree with qualification.
- [ ] Note and handoff cover limits, schema, mix, rollback, retention and restarts.
- [ ] Policy block is unchanged/null or byte-identical to the reviewed replacement.
- [ ] Exact GitHub release ID and tag/companion commit recorded.
- [ ] Five uploaded asset digests and downloaded handoff bytes verified.
- [ ] Published release rechecked; tag plus digest-verification reference sent to fleet-config.
- [ ] Main merge status recorded independently.
- [ ] Fleet reports installation/quiet checks/holds released for each host.
- [ ] Required TUI restart verified separately from installed package.

## Timing and next action

Record actual start/end and wall duration per phase. Separate compilation,
test-harness construction, test execution, transfer, recovery and approval wait;
account for owner review, command preparation and operator corrections separately
from user approval wait in completion-to-next-launch intervals;
mark concurrent phases. Record cold/warm/helper-only conditions and CPU/thread
settings. Include Cargo compiled/fresh counts for reuse experiments.

| Phase | Host / inputs | Start → end / duration | Overlap or wait reason | Evidence |
| --- | --- | --- | --- | --- |
| | | | | |

- Current blocker / owner / concrete next action:
- Exact registered jobs if waiting:
- Passed work that must not be repeated:
- Local owner waiting for a necessary dependency versus an avoidable acknowledgement:
- Unattributed gaps and overlapping intervals excluded from speed claims:
- Release completion evidence / open follow-up issues:
- Published / merged / installed / restarted status recorded separately:
- Next-release improvement and measured benefit, or not yet measured:
- Smooth-path measurement: first production attempt reaches export/accept on each
  architecture; no avoidable plan refreeze after init or coordinator acknowledgement
  between native launch and export; compatibility/AP preparation and acceptance pass
  first time. Record deviations and causes, never waive a real failure to meet this target.
