# Building complete release packages

Release packaging is separate from the developer-only CLI build. It uses the
pinned upstream package builder and includes every core runtime executable;
copying `.build/codex` alone is not a complete release.

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

The existing native build needs a C toolchain, pkg-config, OpenSSL development
headers and libcap development headers for bwrap. The normal V8 path uses pinned
prebuilt archive/bindings; a source-build fallback has different prerequisites.
System protoc is not required by the pinned build, which uses vendored protoc.

## Package assembly

Use a clean Plus source snapshot and preserve its full commit in the manifest.
Assembly can run centrally after the two native builds; it copies each target's
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

Hand fleet-config both archives, archive checksums, manifests, source revision,
release notes, and per-target acceptance results. Fleet-config owns deployment,
actual AP/PATH validation on each box, official-daemon maintenance, and rollback.
Publishing or installing a release is separate from building its candidates.
