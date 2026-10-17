#!/bin/sh
# Run inside the root-owned filesystem isolation, with no Python files mounted.
set -eu
package=$1
export CODEX_HOME=$2
export CODEX_THREAD_ID=11111111-1111-4111-8111-111111111111
mkdir -p "$CODEX_HOME"
version=$("$package/bin/codex-plus" --version)
case "$version" in
  'codex-plus 0.163.0-plus.0.3.0 '*) ;;
  *) printf 'unexpected product version: %s\n' "$version" >&2; exit 1 ;;
esac
set +e
output=$("$package/bin/codex-plus" run -- /bin/sh -c 'test -z "$(cat)"; printf "native-only-stdout\n"; printf "native-only-stderr\n" >&2; exit 23')
result=$?
set -e
test "$result" = 23
printf '%s\n' "$output"
case "$output" in
  *native-only-stdout*native-only-stderr*'codex-plus result: exit=23 '*) ;;
  *) exit 1 ;;
esac
status=$("$package/bin/codex-plus" park --status)
case "$status" in
  *'"parent":"11111111-1111-4111-8111-111111111111"'*'"parks":[]'*) ;;
  *'"parks":[]'*'"parent":"11111111-1111-4111-8111-111111111111"'*) ;;
  *) printf 'unexpected private ledger: %s\n' "$status" >&2; exit 1 ;;
esac
test -f "$CODEX_HOME/codex-plus/background.sqlite"
printf 'native-only runner and ledger status passed\n'
