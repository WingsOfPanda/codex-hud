# Release protocol data, version 1

`scripts/release_protocol.py` is a data/evidence library. It does not build,
schedule, retry, run SSH, publish, install, or authorize actions. The owning
native recipe supplies observed host/tool identities and the existing managed
job's actual result. A plan is reviewed before that recipe executes anything.

The validators reject missing and unknown plan/profile fields. SHA-256 fields
are lowercase 64-digit hex; commits are full 40-digit hex. Hashes of JSON values
use the library's `digest`: sorted keys, compact ASCII JSON, and a final newline.
File hashes always cover the original bytes. Do not hash a reformatted manifest.

## Plan

Required fields (no implicit defaults):

| Field | Value |
| --- | --- |
| `schema_version` | `1` |
| `release`, `plus_version` | Package release and Plus version |
| `upstream` | `{version, tag, commit}`; ordinary revisions on the product source's pinned upstream only |
| `plus_source_commit`, `tooling_commit` | Independently pinned product and recipe revisions |
| `patch` | `{path, sha256}`; must match the product-source packager and patch bytes |
| `previous_release` | `{tag, handoff_sha256, targets}`; both target records contain `asset`, `sha256`, `manifest_sha256`, `tree_sha256` |
| `targets` | Both `x86_64` and `aarch64`: `{triple, profile_sha256, build_glibc}` |
| `gates` | Both architectures, each containing exactly the five gates below |
| `compat` | `{ap_version, fixture_ssh_destinations, recipe_revision: 1, previous_source_commit, stock_binary_sha256: {arch: sha256}, fixture_files: {relative_path: sha256}}` |
| `rollout` | The published handoff fields: `daemon_restart`, `quiet_checkpoint`, `executions`, `session_restart`, `agents_md`, `retain`, `notes` |
| `retention_floor` | A retained rollback release; the previous release must also be retained |
| `note` | Companion note below `docs/releases/` |

Gate specifications for each architecture:

```json
{
  "package": {"checks": ["archive-layout", "native-launch", "package-qualification"]},
  "native-tests": {"suites": {"helper": {"passed": 197, "allowed_ignored": {}}}},
  "compat": {"checks": ["setup-smokes", "managed-job", "rollback", "both-holds", "advisor", "old-tui-new-wrapper"]},
  "captures": {"checks": ["package-backed-screen", "cursor-states"]},
  "ap": {"checks": ["classifier-replay"]}
}
```

This is a shape example, not a qualified gate list: the reviewed release plan
must enumerate its actual required checks and suites. Changing the release data
does not create proof. There is no gate-count success inferred from exit zero.
`validate_plan(plan, product_source_root)` checks the existing packager constants,
patch and both Cargo manifest/lock versions; it does not replace those sources.

## Native profile and initialization

A profile has exactly `schema_version`, `host_id`, `hostname`, `architecture`,
`triple`, `build_root`, `short_root`, `tooling_root`, `source_root`,
`toolchain: {rustc, cargo, version}`, `python: {path, version, libpython}`, and
`executor_package_root`. Optional `system_tools` maps exact canonical executable
paths to SHA-256 pins. Optional `native_build` contains exactly
`upstream_repository`, `cargo_home`, `cargo_target_dir`, `v8_cache`,
`license_cache`, `official_binary` (absolute paths), `official_sha256`,
`cargo_jobs` and `test_threads` (positive integers). Native preparation requires
this build profile. Roots/tool paths are absolute; `libpython` is an
absolute path or null. The version strings are exact observed values, not ranges.

`init_attempt(plan, profile, product_source_root, observed=..., rehearsal=False)`
requires the actual recipe's observed `hostname`, `architecture`, `toolchain`,
`python`, `executor_package_root`, `tooling_commit`, and `plus_source_commit`.
They must match the profile and plan. Native recipes remain responsible for
collecting those observations from the actual host and validating checkout seals;
the pure core does not execute version commands or inspect a process tree.

Initialization creates a unique private directory at
`<build_root>/attempts/<release>-<architecture>-<random>/`, with `outputs/`,
`receipts/`, `attempt.json`, `plan.json`, and `profile.json`. Socket paths derive
from a separately reserved private `<short_root>/<two-alphanumeric-characters>`;
exclusive directory creation handles collisions. The native fixture constructor
preflight still checks real raw socket lengths before compiling. `socket_identity`
records `{path, boot_id, dev, ino, uid}` for exact final cleanup. Durable numbered
creation intents precede each mkdir; completed identities precede final metadata.
Initialization errors leave `initialization-failure.json` and all intent/result
records. Cleanup compares the recorded identity with the actual directory;
a path alone never authorizes deletion. `read_attempt` validates identity schema
without requiring the directory to remain after successful cleanup. Metadata
records rehearsal status permanently.

## Receipts

Stages have one fixed order: `preflight`, `frontend`, `runtime`, `package`,
`native-tests`, `compat`, `captures`, `ap`. The first three are optional build
evidence; all final five are required. No generic DAG or executable plan exists.

`write_receipt(attempt, stage, inputs=[...], executed=[...], outputs=[...],
evidence=..., execution=..., cleanup=...)` creates the original receipt exactly
once. Inputs, outputs, the report and cleanup report are relative regular files
inside the attempt. Executed files are absolute paths under pinned source/tooling
or executor roots, or the exact pinned interpreter/compiler/official/system tool;
`.build` scripts are refused. Attempt-local executables must be native ELF files
matching an exact prior-output file record. This also permits package copies of
accepted native files. The sole script exception is packaged `bin/codex-plus`:
it must match both the pinned product-source launcher and that package manifest's
file record/release/source/target. File records contain `sha256`, `size`, and four-digit octal `mode`.

Same-stage Cargo outputs can instead use `compiler_artifacts=<relative input>`:
an input JSON containing `architecture`, `plus_source_commit`, `tooling_commit`,
`target`, `source_sha256`, `compiler_sha256`, `files` and optional actual
`cargo_artifact`. `source_sha256` is `digest({upstream, patch,
plus_source_commit})` from the plan; the compiler hash must match the pinned
compiler, and each file record must match the executed ELF. Native source seals
and actual compiler-artifact capture remain the recipe's responsibility.

`execution` contains exactly `job_id`, `executor_package_root`, `executor_sha256`,
`architecture`, `plus_source_commit`, `tooling_commit`, `outcome`, `exit_code`,
`argv`, `cwd`, and `env`. Outcomes are `passed`, `failed`, or `unknown`; passing
requires a known zero exit. `cleanup` contains `outcome`, `owned_remaining`,
`unknown`, and `report`; its first three values must also appear in the raw JSON
cleanup report (process arrays normalize to their counts). Outcomes include
`clean`, `blocked`, `failed`, and `unknown`. Only `clean`, zero owned processes and zero unknown processes
qualify. A stopped/missing execution outcome never becomes success implicitly.

Every receipt binds the immutable attempt, input file records, actual executed
file records, command identity and preceding receipt hashes into `input_key`.
Outputs have their own file records. `status` rehashes local evidence and reports
missing/refused gates without modifying the attempt. For imports it separately
lists `unverified_external`: native artifact records are metadata, never claimed
as locally rehashed files.

Each gate report records target `architecture` and `plus_source_commit`. Named
check results have this shape, with every raw file included in receipt outputs:

```json
{"checks":{"rollback":{"outcome":"passed","evidence":{"outputs/compat/rollback.json":"<sha256 of exact raw bytes>"}}}}
```

The report is an assertion by the owning recipe plus raw evidence, not an
attestation. Root acceptance still reviews the recipe and the required checks.
`native-tests` instead supplies `suites: {name: {listed, stdout, logfile,
exit_code}}` pointing at independent dedicated files. Acceptance replays
`native-results.py` and compares passed counts and ignore reasons. Focused suites
may legitimately report filtered tests; the existing validator still reconciles
their independently listed and actually executed names.

The package report adds `archive` and `package_dir` relative paths. Acceptance
calls the **product source revision's** existing `package-release.py` verifier,
compares the plan identities, and checks the archive's exact regular-file bytes,
modes and inventory against that verified extracted tree. It never substitutes
a tooling revision's package constants for the chosen product's constants.

The captures report adds `package_sha256`, `capture_manifest`, and
`capture_manifest_sha256`. That manifest contains `architecture`,
`package_sha256`, and `files: {relative_path: file_record}`. The AP report binds
the same package and capture-manifest hashes and the required `ap_version`.
AP alone may pass a full `replay_profile` to `write_receipt`; execution then pins
its digest in `replay_profile_sha256` and uses its executor architecture/package.
The report/manifest architecture remains the captured package's architecture.
Thus replay on x86 of native ARM captures does not claim ARM AP execution.

## Transfer and acceptance

`export_attempt(attempt, destination)` preserves original receipt bytes, including
failed/unknown outcomes, and returns the evidence bundle's file record. Transfer
that digest separately. An exact `evidence-export.json` inventory lists included
receipt inputs, reports, logs, capture files and creation records, with explicit
`external_artifacts` descriptors for archives and native or extracted package
outputs. Source/cache trees and unreferenced files are not transported. Required
raw evidence cannot be omitted or classified as an external artifact. The
qualified archive is a separate attachment, fetched once.

Before export, the native owner performs final ownership cleanup and removes its
recorded socket directory. `seal_socket_cleanup(attempt, relative_report)` records
`socket-cleaned.json` only after that work. Its raw report must contain clean
`outcome`, empty/zero `owned_remaining` and `unknown`, the exact `socket_identity`,
and `socket_removed: true`. Export refuses a remaining socket path, missing proof,
or changed raw cleanup bytes. An optional separate `socket-removal.json` survives
an interruption between exact removal and the final report. Failed-stage forensic
exports remain permitted after verified final cleanup; failure receipts stay failed.

`import_attempt(bundle, destination, expected_sha256=...)` verifies the digest,
rejects links/path escapes/duplicate members and unexpected/missing raw evidence,
then installs a new private evidence directory with write bits removed. Original
native file modes remain recorded in unchanged receipts and the export manifest;
status validates the known read-only mode projection of transported raw files.
The import cannot be overwritten, exported as a native attempt, run, or resumed.
Native execution entrypoints use `require_native_attempt` to enforce that fence.

`accept_attempt(attempt, source_root=product_source_root, archive=actual_archive)`
requires the separate actual archive for imported evidence. It checks that file's
hash/size, extracts it privately once, replays the product-source package verifier
and compares the complete manifest/tree/modes with the native package inventory.
It then replays the raw test/capture evidence. External native/extracted records
remain native proof metadata, not a claim that their bytes were transferred or
executed on the coordinator. `make_handoff(plan, accepted_by_arch)` requires
both target records from acceptance, matching plan/protocol identities and no
rehearsal label, and emits the existing fleet handoff format. Publication and
fleet installation are separate authorized actions.

Failed receipts are permanent. Interruption before a later stage can preserve
and rehash earlier complete passed receipts; native execution skips those stages
after verifying cleanup and its source/index seals. An interrupted stage without
a complete receipt never counts as passed. There is no automatic failed-stage
retry or reuse: create a new attempt for changed work, preserve the old attempt,
and review selection of already accepted artifacts explicitly. A future explicit
reuse receipt must bind the original passed job/receipt, artifact hashes and
source/patch/toolchain/environment closure after verified final cleanup; copying
an old receipt into a new attempt does not establish that binding. This core refuses recovered
failed-job receipts; a cleanup resolution cannot convert a failed execution into
a passed one. Native source/index seals and cleanup ownership are enforced by
the native recipe, not a broad cleanup waiver in this library.

## Operator commands

The CLI only validates or moves evidence; it launches no release stages:

```sh
python3 -B scripts/release_protocol.py plan-check --plan PLAN --source-root PRODUCT_SOURCE
python3 -B scripts/release_protocol.py status --attempt ATTEMPT
python3 -B scripts/release_protocol.py export --attempt ATTEMPT --output EVIDENCE.tgz
python3 -B scripts/release_protocol.py import --bundle EVIDENCE.tgz --sha256 RECEIVED_DIGEST --output IMPORT
python3 -B scripts/release_protocol.py accept --attempt IMPORT --source-root PRODUCT_SOURCE --archive ARCHIVE --output ACCEPTED.json
python3 -B scripts/release_protocol.py handoff --plan PLAN --source-root PRODUCT_SOURCE --accept x86_64=X86_ACCEPTED.json --accept aarch64=ARM_ACCEPTED.json --output handoff.json
```

`--archive` may be omitted for a native attempt whose original archive is still
present. Every output path is exclusive; the CLI refuses to replace prior bytes.
The import's required `--sha256` comes through the owning session separately from
the bundle. Private validator loading avoids creating bytecode in sealed source.
