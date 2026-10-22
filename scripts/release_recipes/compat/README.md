These recipes are extracted from the accepted native-session-compat-v1 closure.
The four gates and private fixtures retain their existing logic: quiet rollback
with new records and both holds, nine stub advisor checks, the actual previous
TUI/new wrapper root result, and twenty native cursor states. The approved setup
adapter uses cursor-controller.log outside the exclusive inner cursor.log.

release_compat.prepare writes only bound JSON into an attempt. Executable files
stay here and their complete hashes are recorded in native.config.json. Source,
patch, release, previous package, stock binary and fixture pins come from the
validated plan/profile and accepted package inputs. Recipe revision 1 explicitly
refuses a different upstream or package protocol. The historical operator probe
path remains a fixed fixture API, independent of the candidate release.

Run setup with the returned argv plus --setup-only under the existing managed
executor. A full launch always repeats constructor/import/socket setup before
the four gates; a failed setup starts no gate. Neither mode removes evidence or
the short directory. The enclosing native runner owns cleanup, source seals,
stage receipts and evidence export. Short allocation identity is recorded before
baseline capture, including when that capture fails.

Canonical reports bind the dedicated raw gate reports, native cursor manifest,
integrity results and capture inventory. They do not perform AP replay. Captures
preserve the geometry-only helper metadata projection label and native package,
frontend patch, code-mode-host and source bindings. No external model is used.

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
