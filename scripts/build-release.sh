#!/usr/bin/env bash
# Assemble a release from already stripped native outputs; never install it.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$root/scripts/package-release.py" "$@"
