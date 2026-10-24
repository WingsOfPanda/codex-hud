# 0.4.9 qualification checklist

Qualification-close snapshot, 2026-10-24. This checklist was materialized from the
running `STATE.json`, pilot journal and accepted receipts at qualification close;
it was not maintained as a separate checklist from launch. Next release should
instantiate it before step 1 as the book requires. This documentation correction
does not rerun or change any qualification receipt.

## Identity and scope

- Release: `0.163.0-plus.0.4.9`; release tooling/documentation, unchanged runtime
  source and frontend patch from 0.4.8. Full native path on both architectures.
- Coordinator/x86: this root on xjp_l20_2. ARM: independent `codex-plus-910b` root
  on ascend910b. Fleet installation: fleet-config.
- Both normal main clones synchronized cleanly to
  `2a25728ecd84936d4517d04792d22b6bc5665c08`, which supplies the book and execution
  tooling. Frozen/leased sources remained separate and untouched.
- Product: `651a1eb03a08d69af0fd1cc69252bdff2c8f4b27`; official upstream:
  `83086188ae0614bd882818ff1aa23f3194962012` (`rust-v0.163.0`). Patch SHA256:
  `4955f39559483ea63db01a0bce39245ed72915999b6449f165e8be1b9190f4ab`.
- Plan: `98a4eff113990a1889868893ab492b8e02b872e905990e03b04c9ef802ed1b7f`.
  Host profile digests and exact recipe inputs are in [plan.json](plan.json).
  No production plan superseded or receipts transferred between attempts.
- Packager/license generation and pinned child environments: native preflight,
  frontend/package receipts and outputs under each architecture. Rust 1.99.1 and
  Python 3.12.3; private fixture imports, libcap and system tools pinned.
- Previous/in-use/rollback/retain: 0.4.8. Installed executor and observed pilot
  TUIs: 0.4.8; protocol 5, record format 1, installed 0.4.8 recovery policy.
- No config change; `agents_md: null`. Existing global block stays unchanged.
- Authority: shared-release pilot preparation; direct user merge approval for
  preparation PRs #13/#14, now merged. Publication, release-companion main merge,
  rollout and restarts are pending their respective action grants/results.
- ARM brief delegates local readiness, native execution, package approval,
  compatibility/captures, cleanup and export. No coordinator acknowledgement
  between successful phases; final actual-artifact acceptance stays coordinator-owned.
- Escalation: missing grant, changed product scope, inaccessible resource, or
  unresolved ownership/effect. No production recovery or retry was needed.
- Readiness: native prechecks on both hosts and central AP precheck passed before
  allocation. Volatile resources/baselines checked near launch. Prepared completion
  commands were incomplete initially; the resulting review gaps and corrections
  are retained in [pilot notes](pilot-notes.md).
- Dedicated source/tooling checkouts remain frozen; mutable evidence stayed outside.
  Readiness did not substitute for any subsequent native/compatibility/AP gate.

## Acceptance matrix

Statuses bind the same product and plan. [Qualification](qualification.json) and
each architecture's acceptance record contain all receipt and artifact hashes.

| Gate | x86_64 | aarch64 |
| --- | --- | --- |
| Source/tooling/profile freeze and isolated workspace | Passed | Passed |
| Interpreter/import/compiler and packaging child environment | Passed | Passed |
| Offline license inputs, libcap and early package checks | Passed | Passed |
| Fixture sockets, permissions, package-backed setup | Passed | Passed |
| Resource/exclusion and cleanup preflight | Passed | Passed |
| Canonical previous package and AP dependencies | Passed | Passed |
| Native/AP allocation-free readiness | Passed; qualification remained separate | Passed; qualification remained separate |
| Status/export/import/actual-archive acceptance | Passed | Passed |
| Changed display labels | Not applicable: runtime/frontend unchanged | Not applicable: runtime/frontend unchanged |
| Managed proposal review and observer | Native `a3576188`; compat `19d722bd`; AP `f9224f8f` | Native `f00cc5bc`; compat `3ef68b1a`; central AP `3c9d46f4` |
| Frontend, code-mode host, helper, licenses and complete archive | Passed | Passed |
| Manifest/modes/versions and native tests | Accepted; raw bwrap host limitation retained | Passed |
| Helper / frontend executions / unique frontend names | 205 / 213 / 196 | 205 / 213 / 196 |
| Rollback, both-direction holds, actual 0.4.8 TUI/new-helper | Passed | Passed |
| Recovery cases / native cursor captures | 14 / 20 passed | 14 / 20 passed |
| Released AP classifier/stall replay | 23 tests, 2 busy pairs | 23 tests, 2 busy pairs; replay host x86 |
| Managed workload, cleanup and independent review | Exit 0 / cleanup 0; accepted | Exit 0 / cleanup 0; accepted |
| Coordinator final acceptance | Passed actual archive | Passed transferred actual archive |

Failure/reuse record: [pilot notes](pilot-notes.md) records cheap preparation,
operator-command and reporting corrections. No production workload replay or
post-init refreeze. X86 raw bwrap failure remains failed with its narrow reviewed
host disposition. No skipped paid-provider/real-SSH coverage is called passing.
Import external descriptors are metadata; final acceptance separately verifies
the actual archive. ARM physical cleanup was established by its native owner;
the coordinator reviewed transported cleanup proofs and immutable receipts.

## Publication and rollout

- [x] Both archive/manifest/tree identities agree with qualification.
- [x] Note and handoff cover limits, schema, old/new mix, rollback and retention.
- [x] Policy block unchanged (`agents_md: null`); no daemon restart.
- [x] Five local publication files prepared and their hashes/sizes verified.
- [ ] Exact GitHub release ID and tag/companion binding: publication pending.
- [ ] Uploaded assets and downloaded handoff: publication pending.
- [ ] Public release recheck and verified tag sent to fleet-config: pending.
- [ ] Release-companion main merge: pending; PRs #13/#14 are separate.
- [ ] Per-host installation, quiet checks and holds released: fleet rollout pending.
- [ ] Actual TUI restart verification: pending, separate from installation.

## Timing and next action

See [pilot notes](pilot-notes.md) for measured execution/review intervals and
compiled/fresh Cargo counts. Nested timings are not additive; transfer duration,
whole-release speedup and pure user-wait time were not measured. No controlled A/B.

All native, compatibility and AP jobs finished; no qualification job remains.
Preserve accepted archives/receipts; do not rebuild them for publication. Next:
review the concrete companion and five files, obtain missing publication/main-merge
grants, then use the existing publication sequence. Fleet-config owns rollout.
The pilot qualified both first production attempts without an intermediate ARM
permission stop, but operator/reporting mistakes and coordination gaps remain.
