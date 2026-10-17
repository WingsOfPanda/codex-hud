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

## Native binaries

Build natively on each target with Rust 1.99.0, the pinned upstream source and only
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
5. Check the native ELF architecture, dependency resolution, and runtime behavior
   on the architecture that produced them. Do not execute ARM binaries on x86 to
   establish compatibility.

For helper-only revisions with identical upstream source, frontend patch, build
settings, and runtime resources, verified native binaries from the previous
qualified release may be reused. Record their original build provenance and
matching hashes, then repackage the new helper and qualify each complete archive
on its native host. Reuse does not establish compatibility with a different
upstream version or patch.

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
The native directory must contain `codex`, `codex-code-mode-host`, and `bwrap`.

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

The launcher suppresses Python bytecode writes for `run` and `park`, including
detached observers, so normal helper use does not mutate the installed package.
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
  "coordinator_protocol": 1,
  "upstream": {"version": "<X>", "tag": "rust-v<X>", "commit": "<sha>"},
  "note": "docs/releases/<release>.md",
  "targets": {
    "x86_64": {"triple": "x86_64-unknown-linux-gnu", "asset": "<archive file name>", "sha256": "<hex>", "tree_sha256": "<hex>", "manifest_sha256": "<hex>"},
    "aarch64": {"triple": "aarch64-unknown-linux-gnu", "asset": "<archive file name>", "sha256": "<hex>", "tree_sha256": "<hex>", "manifest_sha256": "<hex>"}
  },
  "rollout": {
    "daemon_restart": false,
    "quiet_checkpoint": true,
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
remains. `agents_md` is the exact replacement park/shell Markdown block when it
changes, otherwise `null`. Put additional rollout requirements in `notes`, and
keep test results, failures, skipped checks, and accepted limits in the companion
note referenced by `note`.

When replacing the global block, use the current approved advisor section from
[advisor.md](advisor.md) and compare the complete replacement with fleet-config's
live policy. Preserve its foreground wait wording; copying an older release's
`agents_md` would restore the superseded background default. Published handoffs
remain immutable.

For cooperative upgrades, manifests advertise `coordinator_protocol: 1`.
Set `quiet_checkpoint: false` only when the installed observers and candidate
support the same protocol, upstream identity and target, the candidate's Plus
`M.m.p` version is strictly higher, and cooperative upgrade/recovery acceptance
has passed. Compare numeric version components, not release-name strings.
Every rollback requires `quiet_checkpoint: true`; live observers refuse a lower
or equal Plus version in another package directory and keep monitoring with a
warning. The first rollout from `0.0.4` or older
still requires `quiet_checkpoint: true`; incompatible protocol or upstream
changes also require quiet coordination. Keep previous/in-use immutable package
directories while any process still references them. The installer switches
`current`; it does not signal observers. A compatible helper upgrade follows the
link independently of the TUI. Set `session_restart` according to the actual
client change, rather than using a restart as an observer-handoff command.

Once the release and its handoff asset are verified, message fleet-config only
the tag. Fleet-config runs `stage.py pin` to validate the asset digests and stage
`pins.next.json`, then `stage.py test` in throwaway homes on x86 and ARM. It owns
the rollout approval, deployment, AP/PATH checks, official-daemon maintenance,
and rollback. Build, publish, and merge retain their separate user gates. Do not
retrofit this asset into already published releases as part of this procedure.
