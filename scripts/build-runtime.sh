#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cargo +1.99.0 build --locked --release --manifest-path "$root/rust-runtime/Cargo.toml" \
    --target-dir "$root/.build/runtime-target"
install -m 755 "$root/.build/runtime-target/release/codex-plus-runtime" \
    "$root/.build/codex-plus-runtime.new"
mv -f "$root/.build/codex-plus-runtime.new" "$root/.build/codex-plus-runtime"
echo "Built $root/.build/codex-plus-runtime"
