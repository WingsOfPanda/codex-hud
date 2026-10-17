# Post-0.3.0 audit fixes

Audit base: main `be2c90f6d1f5e4251d949fba8f3a2b32033194c9`.
Implementation branch: `fix/audit-030`. These source changes do not install,
publish, merge, change global instructions, or replace the official daemon.
The ledger schema and coordinator protocol remain unchanged.

Two GPT-6 Astra `xhigh` consultations reviewed the findings and implementation.
The initial review accepted all ten findings; the implementation review required
the developer build to select both actual upstream Cargo packages. The advisor
found no further actionable defect in the reviewed coordinator, child-lifetime,
result-validation or HUD changes. Advice is independent evidence, not an
authorization or a substitute for the checks below.

## Changes and regression boundaries

| Finding | Change | Regression |
| --- | --- | --- |
| 1: cancelled callback blocks later wakes | Batch selection and assignment use one immediate transaction; invalidated, never-dispatched legacy callbacks retire locally. | Two real Rust Store threads synchronize cancellation at the selection boundary; both transaction orders and legacy poison followed by a fresh wake. |
| 2: old uncertainty blocks fresh delivery | Only bounded consumed-history exhaustion becomes callback-local uncertainty. | 3,300 history items, uncertain/cancel-uncertain callbacks, preserved identity/payload and fresh delivery; transport failures still propagate. |
| 3: advisor survives cancellation | An acknowledged detached subreaper guard owns each consultation/Git probe through an owner lifeline and cleans adopted descendants. | TERM-resistant leaders, descendants and separate sessions under runner cancellation, owner/group SIGKILL, timeout and actual stock `/stop`; unrelated process groups survive. |
| 4: relative home switches ledgers | Resolve against the original cwd before package transitions, without canonicalizing symlinks. | Spaces and `..` across packaged observer/supervisor startup, failed exec and cooperative upgrade; symlink checks preserved. |
| 5: Git fsmonitor runs outside sandbox | Disable fsmonitor for pointer commands, without changing repository/global config. | Configured hook cannot write its outside marker; pointer and repository/index contents remain intact. |
| 6: upstream notices missing | Include explicit target-specific CLI/host production graphs, source provenance and checksum-pinned supplemental texts; fail visibly on missing notices. | Initial publisher-file gaps were resolved through [declaration-backed notice inputs](notice-resolution.md), with complete generation on both targets. |
| 7: build installs stale executable | Select unambiguous executables from successful Cargo compiler-artifact messages. | Real Cargo target/config paths with spaces, warm builds, stale artifacts and failed-build preservation. |
| 8: developer build lacks Code Mode host | Build and stage the actual CLI and host packages together, using upstream's pinned V8 resolver. | Two-package workspace, adjacent host selection, V8 checksum/cache checks and native Code Mode smoke. |
| 9: hidden completed workers are polled | Fetch turns only for active workers; retain inactive thread rows and metrics. | Zero inactive turn reads, one per active/waiting worker per refresh, completed group totals, transition to idle and three-row footer budget. |
| 10: valid Unicode result rejected | Bound result transport at 512 KiB independently of schema character limits. | Maximum Chinese/raw and escaped supplementary text, escapes, over-limit strings/files, symlinks and nonregular files. |

Stock enqueue/acknowledgement ambiguity remains uncertain and visible. None of
these changes permit blind callback resends or claim exactly-once model/tool
effects. The Python source remains the historical compatibility oracle.

## Verification

The primary agent rebuilt the optimized x86 Rust helper with Rust 1.99.1 and
ran the combined acceptance checks against that executable:

- 55 Rust library tests, 22 coordinator cases, 16 runner cases and 13 advisor
  regressions passed. Three Python-only runner mocks remain explicitly skipped.
- Seven upgrade cases passed, including relative-home package transitions.
- An official stock 0.163.0 executable passed background completion, seven
  advisor groups, the held 65-second foreground consultation, five native
  lifecycle scenarios, and actual native `/stop`. The installed 0.3.0 helper
  fails the same new `/stop` regression; the rebuilt helper passes.
- The rebuilt client passed all 22 native terminal smoke stages. Focused HUD
  tests and clean/reverse patch application, exact reconstruction, unchanged
  backend source and all 1,314 external lock entries were checked separately.
- Build and packaging unit checks exercise the corrected command/graph shapes;
  their final counts and command logs are recorded in the evidence file.

Fixtures use private homes and loopback providers. The x86 native client is a
`dev-small` candidate; its matching upstream 0.163.0 Code Mode host was reused
from a previously qualified package. This is not a newly built optimized client
archive or fresh ARM execution. Existing immutable release archives are unchanged.

Machine-readable results: [audit-fixes.json](evidence/audit-fixes.json).
Full local commands, logs, red/green regressions and advisor JSON live under
`.build/audit-fixes/`.

## Initial publisher-file gaps and their resolution

The initial collector covered 1,060 of 1,066 selected x86 production packages and
1,057 of 1,063 ARM packages with verified full texts. Both include the complete
cached `ring 0.17.14` license files verbatim. Six releases lack verified full
publisher texts in the examined release sources:

- `debugserver-types 0.5.0`
- `deno_core_icudata 0.77.0`
- `eventsource-stream 0.2.3`
- `fxhash 0.2.1`
- `io_tee 0.1.1`
- `linux-keyutils 0.2.4`

The follow-up verified their published license declarations, selected permitted
standard terms, and retained actual authorship/header evidence separately from
canonical license bodies. It also traced the embedded ICU data and Microsoft
schema to exact upstream sources and preserved their independent notices.
Unmerged additions and invented copyright statements are not used.

Fresh root acceptance passed 40 packaging tests and generated complete notice
inventories for all 1,066 x86 and 1,063 ARM packages. Warm reruns were identical
without downloads; actual ICU base64 decoding and package-staging provenance
passed. See [the resolution record](notice-resolution.md) and
[notice-resolution.json](evidence/notice-resolution.json). The initial
`audit-fixes.json` is retained as its earlier snapshot; the notice-generation
blocker recorded there is now resolved. New native archives and their target
qualification remain separate work.
