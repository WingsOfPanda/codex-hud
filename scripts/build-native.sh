#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$root/.build/source"
commit=092d3acd6bec3e3a14bdc7e7a2810ab628ab759d
patch_file="$root/patches/codex-0.162.1-worker-hud.patch"

mkdir -p "$root/.build"
if [[ ! -d "$source_dir" ]]; then
    git clone --depth 1 --branch rust-v0.162.1 https://github.com/openai/codex.git "$source_dir"
fi
if [[ $(git -C "$source_dir" rev-parse HEAD) != "$commit" ]]; then
    echo "Unexpected source revision in $source_dir; refusing to overwrite it." >&2
    exit 1
fi
if git -C "$source_dir" apply --check "$patch_file"; then
    git -C "$source_dir" apply "$patch_file"
elif ! git -C "$source_dir" apply --reverse --check "$patch_file"; then
    echo "Source differs from the expected patch; preserve or remove $source_dir before rebuilding." >&2
    exit 1
fi
cd "$source_dir/codex-rs"
cargo +1.99.0 build --locked -p codex-cli --bin codex --release -j 12
install -s -m 755 target/release/codex "$root/.build/codex.new"
mv -f "$root/.build/codex.new" "$root/.build/codex"
echo "Built $root/bin/codex-hud"
