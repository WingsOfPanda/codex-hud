#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 "$root/scripts/build-artifacts.py" runtime "$root"
echo "Built $root/.build/codex-plus-runtime"
