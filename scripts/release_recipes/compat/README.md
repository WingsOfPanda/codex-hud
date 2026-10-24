These recipes are extracted from the accepted native-session-compat-v1 closure.
The five gates and private fixtures retain their existing logic: quiet rollback
with new records and both holds, nine stub advisor checks, the actual previous
TUI/new wrapper root result, twenty native cursor states, and packaged recovery
policy. The approved setup adapter uses cursor-controller.log outside the
exclusive inner cursor.log.

release_compat.prepare writes only bound JSON into an attempt. Executable files
stay here and their complete hashes are recorded in native.config.json. Source,
patch, release, previous package, stock binary and fixture pins come from the
validated plan/profile and accepted package inputs. Recipe revision 1 explicitly
refuses a different upstream or package protocol. The historical operator probe
path remains a fixed fixture API, independent of the candidate release.

Run setup with the returned argv plus --setup-only under the existing managed
executor. A full launch always repeats constructor/import/socket setup before
the five gates; a failed setup starts no gate. Neither mode removes evidence or
the short directory. The enclosing native runner owns cleanup, source seals,
stage receipts and evidence export. Short allocation identity is recorded before
baseline capture, including when that capture fails.

Canonical reports bind the dedicated raw gate reports, native cursor manifest,
integrity results and capture inventory. They do not perform AP replay. Captures
preserve the geometry-only helper metadata projection label and native package,
frontend patch, code-mode-host and source bindings. No external model is used.

`packaged-recovery-policy` runs the nine detached-job scenarios and five goal
scenarios listed in `release_compat.RECOVERY_CASES` against the actual packaged
new helper and pinned official daemon. The two old-reader cases additionally
execute the pinned previous package's helper. The plan must include the four
`RECOVERY_FIXTURES` files in `compat.fixture_files`; preparation freezes these
and every other declared fixture file in the managed input closure. All original
scenario deadlines and assertions remain in force. The stage has a 180-second
budget, with enclosing native/owned/workflow budgets of 900/930/1080 seconds;
the managed job retains its 1500-second deadline and separate cleanup.

The recovery checker reads and validates all fourteen hashed case reports at
the native runner and canonical export seams. It verifies new/old helper and
official daemon hashes, zero paid requests, private-home removal within the
owned short allocation, and exact recovery-versus-report-only policy receipts.
Each report hash is exported as dedicated evidence. Fixture homes inherit the
existing owned scope and supervisor; the enclosing independent cleanup audit
must pass before stage receipts can qualify the run.

Production composition lives in workflow.py and is exposed by the checked-in
release_compat.py CLI. After the native build job has completed and its final
socket cleanup is sealed, run with the profile's exact Python and loader:

```
python scripts/release_compat.py prepare-attempt --attempt ATTEMPT --old-package PREVIOUS_PACKAGE
```

This derives package-only approval through release_protocol.approve_package;
it does not require final release acceptance. It emits compat-job-proposal.json
for the existing managed executor. Review/prepare/launch that proposal through
the normal installed job commands. Its run-attempt command records compatibility
and captures receipts through the core receipt writer, including exact old/new
package execution provenance, the managed job identity and independent cleanup.
The native attempt's final socket stays removed; compatibility owns its separate
short allocation and removes it only after exact-identity ownership audit.

After importing native evidence on the coordinator, provide a replay profile
whose source_root is an existing clean AP checkout at the plan's source pin:

```
python scripts/release_compat.py prepare-ap --attempt NATIVE_IMPORT --coordinator AP_EVIDENCE --profile AP_PROFILE
```

The replay profile's ap_replay object has node, node_modules, installed_bundle
and installed_registry absolute paths. The plan's compat.ap_replay object has
source_commit, installed_plugin_commit and bundle_sha256 pins; compat.ap_version
pins the released version. Node needs an exact system_tools hash pin. Source,
bundle, installed registry, interpreter/loader and real dependency origins are
checked before proposal creation and again before and after execution. Missing
inputs are prerequisite failures; preparation installs nothing.

Review/prepare/launch ap-job-proposal.json through the same managed executor.
The checked-in observer and Vitest configuration run the existing 22 released
checks plus the one observer check, reconciling 20 captures and two busy hash
pairs. Only data is copied into AP_EVIDENCE. No source is generated, no nudge is
called, and no native import is modified. The resulting ap-receipt.json is a
sidecar supplied to core acceptance with --ap-receipt; its execution architecture
belongs to the replay profile and remains distinct from the capture target.

These commands prepare real production recipes. Unit composition checks use
synthetic subprocess results; they do not establish that native gates or AP
replay passed. A failed/unknown command retains its first start marker, receipt,
logs and cleanup outcome; it cannot be replayed in place.

### Retained-tooling AP receipt adapter

Current `run-ap` calls the corrected receipt verifier; use that normal route when
its selected recipe and AP matrix are qualified. The 0.4.8 release needed a
separate coordinator when its frozen execution tooling embedded the old
audit-versus-removal receipt check. For that reviewed compatibility case only,
`scripts/release_ap_coordinator.py` provides `prepare-leaf` and `compose`:

This adapter is qualified for original execution tooling `59d85b0`, 20 captures,
23 tests (22 released plus one observer), two busy pairs, and deadline/grace
600/90 seconds. Its current validation has those constants; a different matrix
needs reviewed code/tests, not a claim of generic fallback support.

```text
PYTHON -B VERIFIER/scripts/release_ap_coordinator.py prepare-leaf --coordinator AP_EVIDENCE
PYTHON -B VERIFIER/scripts/release_ap_coordinator.py compose --coordinator AP_EVIDENCE --execution EXECUTION_DIRECTORY --prepared-state PREPARED_STATE --audit INDEPENDENT_AUDIT --executor-review EXECUTOR_REVIEW
```

Preparation freezes a leaf proposal; it does not launch. Review/launch that
proposal once with the existing managed executor and retire the unlaunched
original proposal. The leaf calls the original pinned verification and supervised
replay APIs. Composition requires actual successful managed records, the real
guard journal, independently clean process/lock/group/session evidence and all AP
outputs. It preserves raw records and writes a distinct sidecar. It neither
repairs a failed replay nor transfers native receipts into a new plan. Keep the
adapter's verifier commit separate from the execution-tooling commit.

Freeze the verifier checkout HEAD and files from `prepare-leaf` through `compose`;
even an unrelated documentation commit changes its provenance. Edit elsewhere.
After actual completion, the owning root runs the original pinned
`release_compat.py cleanup-ap --attempt AP_EVIDENCE --output NEW_AUDIT` for an
independent read-only audit, preserving the job's first cleanup report. That root
also records the exact executor review required by `compose`: scope, job ID,
execution directory, state/result/audit hashes, exact identity/group/session
absence and a free executor lock. No current CLI creates that review file; it is
an explicit evidence-producing owner step, not inferred from exit zero. Follow
the adapter's current schema, then use core acceptance on the generated sidecar.

Compatibility short-directory deletion uses the common quarantine protocol,
with boot, owner and inode identity recorded at allocation. Its immutable proof
files live under compat/socket-*.json. Interrupted compatibility recovery is
unsupported in this increment; unresolved allocations block export and final
acceptance. Preserve the allocation and original failure for explicit review.
