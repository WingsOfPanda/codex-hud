#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$root/.build/source"
commit=360319d8dee62d87e2d59fc0c21805d79519cfed
patch_file="$root/patches/codex-0.165.0-worker-hud.patch"

mkdir -p "$root/.build"
if [[ ! -d "$source_dir" ]]; then
    git clone --depth 1 --branch rust-v0.165.0 https://github.com/openai/codex.git "$source_dir"
fi
if [[ $(git -C "$source_dir" rev-parse HEAD) != "$commit" ]]; then
    echo "Unexpected source revision in $source_dir; refusing to overwrite it." >&2
    exit 1
fi
# Compare the entire source with HEAD plus the release patch, including new files.
expected_index=$(mktemp "$root/.build/source-index.XXXXXX")
rm -f "$expected_index"
trap 'rm -f "$expected_index"' EXIT
source_matches_index() {
    GIT_INDEX_FILE="$expected_index" git -C "$source_dir" diff --quiet -- &&
        [[ -z $(GIT_INDEX_FILE="$expected_index" git -C "$source_dir" ls-files --others --exclude-standard) ]]
}
GIT_INDEX_FILE="$expected_index" git -C "$source_dir" read-tree "$commit"
GIT_INDEX_FILE="$expected_index" git -C "$source_dir" apply --cached "$patch_file"
if ! source_matches_index; then
    GIT_INDEX_FILE="$expected_index" git -C "$source_dir" read-tree "$commit"
    if ! source_matches_index; then
        echo "Source differs from clean upstream or the frontend release patch; preserve or remove $source_dir before rebuilding." >&2
        exit 1
    fi
    git -C "$source_dir" apply "$patch_file"
fi
GIT_INDEX_FILE="$expected_index" git -C "$source_dir" read-tree "$commit"
GIT_INDEX_FILE="$expected_index" git -C "$source_dir" apply --cached "$patch_file"
if ! source_matches_index; then
    echo "Source differs from the expected patch; preserve or remove $source_dir before rebuilding." >&2
    exit 1
fi
python3 "$root/scripts/build-artifacts.py" native "$root" "$source_dir"
"$root/scripts/build-runtime.sh"
echo "Built $root/bin/codex-plus"
