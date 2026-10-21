# Building complete release packages

Release packaging is separate from the developer-only CLI build. It uses the
pinned upstream package builder and includes every core runtime executable;
copying `.build/codex` alone is not a complete release.

## Release identity

`UPSTREAM_VERSION` and `PLUS_VERSION` in `scripts/package-release.py` are
independent. The package name combines them as `<upstream>-plus.<Plus version>`,
for example `0.163.0-plus.0.1.0`. Preserve the Plus version across upstream-only
updates; increment its patch component for Plus fixes and its minor component
for Plus features. Every new combination still needs qualified target packages.
Existing published `plus.N` releases retain their original identities.

The current revision is `0.163.0-plus.0.4.6`: compact command display, the Morandi
theme, and advisor model diagnostics. Build the changed frontend and helper
natively on both architectures. Reuse verified caches and unchanged-stage
receipts only with exact source identity; do not reuse the previous frontend
binaries. Qualify quiet rollback to 0.4.5, both-direction holds, old-TUI/new-helper
operation, package-backed UI/AP captures and same-version cross-host advisor
success and caller-death cleanup. Mixed advisor helper versions must refuse the
changed worker identity. Drain consultations before switching, install central
host first, and finish the caller rollout before consulting. Use protocol 5,
execution format 1, no daemon restart, quiet checkpoint, drained executions,
session restart required and `agents_md: null`. The proposed retention floor is
0.4.5, subject to executed native qualification.

The preceding revision is `0.163.0-plus.0.4.5`, a timed release of build tooling
and documentation with a version-only helper rebuild. Use the unchanged
qualified 0.4.4 frontend only after verifying its exact binaries and compile
origin. Run fresh native package, helper and compatibility checks on both
targets; record retained frontend evidence separately. The source companion is
[0.4.5](releases/0.163.0-plus.0.4.5.md). Once qualified, propose 0.4.4 as the
rollback floor, no daemon restart, quiet checkpoint, drained executions and
`agents_md: null`. No global instruction change is part of this revision.

The preceding advisor revision is `0.163.0-plus.0.4.4`: coordinator protocol 5 and
package `execution_record_format: 1`, with the same pinned official 0.163.0 daemon.
The execution format describes durable executor records separately from
coordinator/upstream compatibility. These identities do not establish package
qualification, publication or installation.

For 0.4.4, require complete native qualification and actual 0.4.3/0.4.4
mixed-version checks. Use `quiet_checkpoint: true`,
`executions: wait`, `daemon_restart: false` and `session_restart: required` to
load the changed frontend and instructions. Propose 0.4.3 as the rollback floor,
subject to native quiet-rollback qualification, and preserve all packages still
referenced by processes or unresolved executions. Include the complete reviewed
shell/advisor/goal block from
`docs/releases/0.163.0-plus.0.4.4/agents-block.md` in `rollout.agents_md`.
Install the central Claude host first and drain all advisor consultations on
both sides before switching packages; command transport and global help must
be installed together. Do not create a qualified handoff or publish archive
hashes before the final source freeze and per-target qualification. The
[source companion note](releases/0.163.0-plus.0.4.4.md) lists the required gates;
the release handoff records their executed results separately.
The expanded footer requires a compatible AP release installed on every box
where AP drives Codex workers before fleet staging. Replay actual packaged
screen/cursor captures against AP's classifier; development captures alone do
not satisfy that final gate.

## Native binaries

Before expensive work, follow the [release preflight](release-performance.md#run-the-preflight)
with the exact interpreter, staged inputs and raw socket paths selected by the
qualification plan. Run it again on the remote host after staging. A passing
preflight checks setup only; it does not replace native qualification.

Build natively on each target with Rust 1.99.1, the pinned upstream source and only
the frontend patch. Use `--locked`, `--release`, and an explicit GNU target.
On 910b, keep the checkout, Rust homes, Cargo target directory, downloads,
`TMPDIR`, cache and outputs under `/data/liupan/codex-plus-build`. Install the
private toolchain with `--no-modify-path`; preserve global settings and running
services. Select bounded parallelism from current load and available memory.

The required build order is:

1. Build `codex-bwrap`'s `bwrap` binary for the target. Strip the copy to be shipped.
2. Set `CODEX_BWRAP_SHA256` to that stripped file's SHA-256 before building Codex.
3. Resolve the pinned V8 archive and bindings with upstream
   `scripts/codex_package/v8.py`. Preserve its checksum validation and pass both
   `RUSTY_V8_ARCHIVE` and `RUSTY_V8_SRC_BINDING_PATH` into the build.
4. Build `codex-cli`'s `codex` and `codex-code-mode-host` with that same environment.
   Strip the shipping copies; retain native logs and compiler/glibc versions.
5. Build the Plus helper with `./scripts/build-runtime.sh` from that same Plus
   source snapshot. Copy `.build/codex-plus-runtime` into the native directory.
6. Check the native ELF architecture, dependency resolution, and runtime behavior
   on the architecture that produced them. Do not execute ARM binaries on x86 to
   establish compatibility.

For helper-only revisions with identical upstream source, frontend patch, build
settings, and runtime resources, verified native binaries from the previous
qualified release may be reused. Record their original build provenance and
matching hashes, then repackage the new helper and qualify each complete archive
on its native host. Reuse does not establish compatibility with a different
upstream version or patch.

Keep the verified upstream checkout and Cargo target directory stable across
retries. Preserve source, toolchain, target, feature, lockfile and build-environment
identities with each binary. For test-only retries, use the already-built test
executable when those inputs are unchanged; see [saved native results](verification.md#saved-native-results).

The existing native build needs a C toolchain, pkg-config, OpenSSL development
headers and libcap development headers for bwrap. The normal V8 path uses pinned
prebuilt archive/bindings; a source-build fallback has different prerequisites.
System protoc is not required by the pinned build, which uses vendored protoc.

## Package assembly

Use a clean Plus source snapshot and preserve its full commit in the manifest.
Assemble and qualify each archive on its native build host using the same clean
Plus commit. This avoids transferring ARM binaries away and then returning the
archive for testing. Central assembly remains supported; it copies each target's
binaries and fetches pinned runtime resources without running foreign binaries.
Transfer reports, logs, captures and dedicated result files separately from the
shipping archive. Do not include source clones, Cargo output or extracted package
duplicates in routine evidence bundles. Fetch each accepted archive once and
verify its digest. Preserve passed stage receipts with their original identities;
rerun only stages whose inputs or required behavior changed, with that selection
reviewed explicitly. A failed run remains failed even when later work succeeds.
The native directory must contain `codex`, `codex-plus-runtime`,
`codex-code-mode-host`, and `bwrap`.

```sh
./scripts/build-release.sh \
  --source-dir /path/to/pinned-patched-upstream \
  --native-dir /path/to/native/x86_64-unknown-linux-gnu \
  --target x86_64-unknown-linux-gnu \
  --build-glibc 2.39 \
  --plus-commit FULL_PLUS_COMMIT \
  --output-dir .build/dual-release/packages
```

Repeat with the ARM target and its native binaries. The packager preserves the
canonical `bin/`, `codex-path/`, and `codex-resources/` layout. Codex voice is
deliberately omitted and declared in the manifest and [release notes](release-notes.md).
The 0.163.0 package contains ripgrep and bwrap; upstream removed bundled zsh.
The Plus manifest is separate from upstream's `codex-package.json`, whose version
remains the upstream version.

The package contains a Bash launcher and native Rust helper, with no Python
source, bytecode, interpreter, or `libpython` dependency. The build-time packager
still requires Python 3.11+, Cargo's locked offline dependency sources, and the
pinned toolchain's standard-library copyright file. It generates deterministic
notices and dependency/source provenance under `licenses/` for the helper and the
upstream CLI/Code Mode host, recorded in the manifest's `runtime` and `native`
objects and full file inventory. Cargo selects each explicit production root
with the shipping target and default features; dev-only and disabled optional
dependencies are excluded. Keep these graph inputs aligned with the native build.
Some published Cargo crates omit repository license files. The checked-in
`scripts/native-license-sources.json` pins their exact source revisions, crate
checksums, license URLs and text hashes. Assembly fetches those texts into the
output directory's `license-cache`; a warm cache needs no license downloads.
Missing cached Cargo sources or license texts fail assembly visibly. Updating
upstream dependencies requires refreshing these pins from the corresponding
published source and rerunning notice coverage checks.

Six crates without separate publisher license files have explicit,
checksum-bound declaration-backed resolutions. Their selected canonical SPDX
terms are labeled separately from publisher notices; authors and template
placeholders are not converted into invented copyright statements. Matching
Microsoft schema grants and the complete ICU data notice bundle are independent
required inputs. See [notice resolution](notice-resolution.md) for the source
evidence and checks. Unknown missing licenses, changed declarations, missing
component notices and invalid hashes still fail assembly. Existing qualified
archives are unchanged.

Helper use does not mutate the installed package.
Its native CLI branch retains ordinary stdin, argument, signal, and exit behavior.
`codex-plus --version` reports the Plus release and source revision; the native
`codex --version` continues to report the upstream version.

Manifest tree hashes cover every regular file except `codex-plus.json`, including
relative path, SHA-256, four-digit octal mode and size. Hash the ASCII encoding of
that file dictionary serialized by JSON with sorted keys and compact separators.
Symlinks and special files are rejected. The outer archive checksum covers the
manifest itself. See `scripts/package-release.py` for the exact hash recipe.

## Qualification and handoff

Test extracted packages, not only compiler output. Verify file/mode/tree hashes,
dependencies, versions and symlink launch from another directory. Exercise TUI
start/resume, `exec`, app-server stdio, code-mode and sandbox helper discovery,
managed shell behavior, and park/wake in isolated Codex homes with loopback
providers. Run ARM acceptance on 910b itself. Record unavailable host facilities
as explicit qualification limits; do not change host security policy to make a
test pass.

From the release after `0.162.1-plus.0.0.4`, attach `handoff.json` alongside both
qualified archives and their `.sha256` files. Use the following schema, matching
fleet-config's `codex-plus/RELEASING.md`, section 2, step 3:

```json
{
  "release": "<tag>",
  "plus_commit": "<the manifest's plus_commit>",
  "coordinator_protocol": 5,
  "execution_record_format": 1,
  "upstream": {"version": "<X>", "tag": "rust-v<X>", "commit": "<sha>"},
  "note": "docs/releases/<release>.md",
  "targets": {
    "x86_64": {"triple": "x86_64-unknown-linux-gnu", "asset": "<archive file name>", "sha256": "<hex>", "tree_sha256": "<hex>", "manifest_sha256": "<hex>"},
    "aarch64": {"triple": "aarch64-unknown-linux-gnu", "asset": "<archive file name>", "sha256": "<hex>", "tree_sha256": "<hex>", "manifest_sha256": "<hex>"}
  },
  "rollout": {
    "daemon_restart": false,
    "quiet_checkpoint": true,
    "executions": "wait",
    "session_restart": "none | optional | required",
    "agents_md": null,
    "notes": "<anything else the rollout must do or watch>"
  }
}
```

Fill identity and hash fields from the qualified manifests and archives, and
verify archive hashes against GitHub's asset digests before handing over. The
release tag may point to a later companion-note commit than `plus_commit`.
Copy `coordinator_protocol` exactly from both target manifests; the values must
agree. `manifest_sha256` hashes the exact bytes of each package's
`codex-plus.json`, including its identity fields. Fleet-config can pin that hash
and verify the installed manifest as well as its payload tree.
Choose exactly one `session_restart` value. Set `daemon_restart` for an upstream
bump; assess it explicitly for other changes. With `quiet_checkpoint: true`, a
box with live park helpers waits until no helper, armed park, or open callback
remains. Detached executors and unresolved execution/cleanup records also block
a quiet cutover or rollback; inspect them even when no process remains.
`agents_md` is the exact replacement park/shell Markdown block when it
changes, otherwise `null`. Put additional rollout requirements in `notes`, and
keep test results, failures, skipped checks, and accepted limits in the companion
note referenced by `note`.

For protocol-5 releases also copy `execution_record_format` from both manifests.
`rollout.executions` is `wait` for the initial cutover, incompatible changes,
OpenAI bumps and rollbacks. Qualified compatible higher revisions may use
`continue` with the [maintenance and retention contract](detached-jobs-fleet.md).
Do not apply the legacy zero-helper/zero-armed-park test to those execution
watches, or skip unresolved queue admission merely because it belongs to one.

When replacing the global block, use the current approved advisor section from
[advisor.md](advisor.md) and compare the complete replacement with fleet-config's
live policy. Preserve its foreground wait wording; copying an older release's
`agents_md` would restore the superseded background default. Published handoffs
remain immutable.

The current candidate manifests advertise `coordinator_protocol: 5` and
`execution_record_format: 1`.
Set `quiet_checkpoint: false` only when the installed observers and candidate
support the same protocol, upstream identity and target, the candidate's Plus
`M.m.p` version is strictly higher, and cooperative upgrade/recovery acceptance
has passed. Compare numeric version components, not release-name strings.
Every rollback requires `quiet_checkpoint: true`; live observers refuse a lower
or equal Plus version in another package directory and keep monitoring with a
warning. The first Python-to-Rust rollout from any protocol-1 release
requires `quiet_checkpoint: true`; incompatible protocol or upstream
changes also require quiet coordination. Keep previous/in-use immutable package
directories while any process still references them. The installer switches
`current`; it does not signal observers. A compatible helper upgrade follows the
link independently of the TUI. Set `session_restart` according to the actual
client change, rather than using a restart as an observer-handoff command.

The first combined detached-job/Ultracode rollout from protocol 3 requires a
quiet protocol-5 cutover. The detached-only protocol-4 candidate was unreleased.
Use `quiet_checkpoint: true`, `executions: wait`, `session_restart: required`
and `daemon_restart: false` with the unchanged stock 0.163.0 daemon.
An operator maintenance hold fences new Plus launch/model admissions
and reconciles known queued work. `admission_quiet` does not imply
`execution_quiet`: existing executors continue during the hold. Fleet quiet
checks must account for both, plus native/AP work outside the Plus ledger.
No process is killed or command replayed to make an upgrade quiet.

Fleet helper scans must recognize `codex-plus-runtime execution-executor`,
including its leading concrete `--package-root` pin, owning root and execution
UUID. Executors retain their original executable and package directory through
cwd/open descriptors; they do not follow `current` with the coordinator.
Retain those immutable packages while executions run or remain unresolved.
Later upgrades with running jobs require separately qualified executor-format,
coordinator, upstream and target compatibility plus fleet retention/reconnect
support. Do not treat format 1 alone as that qualification. Every rollback still
requires a quiet checkpoint.

The TUI must load the new frontend for detached-job display and Ultracode controls.
This candidate adds no daemon patch and retains the existing native-goal disabling
requirement. Fleet-config must route both `job` and `ultracode` through the helper
and owns process/quiet checks, package retention and global worker-handoff policy.
Legacy roots without the startup bootstrap need a fresh qualified root to use
Ultracode; resuming them alone does not qualify them. Rollback to 0.3.2 after
execution or mode rows exist is not qualified. Retain the first qualified combined
protocol-5 package as the rollback floor until explicitly retired by tested
qualification. Private feature evidence does not qualify complete combined
archives; record final qualification for each architecture in the companion note.
See the [reviewed handoff contract](detached-jobs.md): workers prepare expected
runs over sixteen minutes for root review; the estimate never imposes a timeout.

The earlier custom-goal cutover requires a quiet protocol-2 to protocol-3 transition,
`daemon_restart: true` and `session_restart: required`, even though the upstream
version remains 0.163.0. Follow the [goal isolation/rollback contract](goals.md#stock-daemon-isolation-and-rollout-contract):
native goals must be effectively disabled at stock daemon startup, and the fleet
wrapper must dispatch `goal` to the helper. The reviewed native objective must
be captured before native goal APIs are disabled. Include these requirements
and explicit rollback package retention in the handoff's rollout notes. Preserve
the existing updater-disable setting, shared daemon identity and live ledger.
No automatic native-goal conversion or re-enabling on rollback is permitted.
Fleet quiet checks must recognize settled goal callback markers and open goal
dispatches as specified in the linked contract; the pre-goal callback-only query
does not qualify a goal-aware cutover or rollback.

For the Python-to-Rust cutover, fleet helper scans must include native
`codex-plus-runtime supervise|observe` and legacy Python processes. Preserve
the ledger and explicitly retain the last Python package and interpreter for
a quiet rollback, beyond ordinary `previous` cleanup. No shared-daemon restart
is needed when both releases use upstream 0.163.0. See
[the cutover contract](background-work.md#cooperative-package-upgrades).

Once the release and its handoff asset are verified, message fleet-config only
the tag. Fleet-config runs `stage.py pin` to validate the asset digests and stage
`pins.next.json`, then `stage.py test` in throwaway homes on x86 and ARM. It owns
the rollout approval, deployment, AP/PATH checks, official-daemon maintenance,
and rollback. Build, publish, and merge retain their separate user gates. Do not
retrofit this asset into already published releases as part of this procedure.
