# Observer handoff for 0.162.1-plus.0.0.4

This is historical recovery tooling, not the normal rollout procedure. Review
found delivery-interruption and partial-stop risks; do not use it to upgrade
active observers during fleet rollout. Use a quiet checkpoint for legacy
helpers, or [cooperative package upgrades](background-work.md#cooperative-package-upgrades)
when both installed and candidate helpers support the protocol.

Installing a package does not replace helpers that already run Python code from
an older package. The helper is a Python interpreter running
`-m codex_plus.background supervise --parent UUID`, with an `observe` child.
Its executable is Python, **not** the package's `bin/codex`. Searching only for
native binaries or release directory names is insufficient.

The default is inspection only. Fleet rollout requires the user's fleet go and
coordination with the selected root's onsite owner. The owner must leave that
root loaded and idle and prevent new registrations for that root throughout the handoff.
Preserve the active GLM trial and its registration. Do not cancel, rearm, resend,
stop jobs, or restart the daemon to facilitate this procedure. Keep both package
directories immutable while their helpers run. Updating fleet pins or wrappers
belongs to fleet-config.

## Obtain the reviewed sidecar

The sidecar is source tooling; it is deliberately absent from the frozen package
payload. The release handoff must provide its **40-character source commit** and
**SHA-256**, separately from the package's source commit
`6b74025962c2526afcdec5411670393ebbf9deb3`. Download or copy the reviewed script
before invoking it. Never run a moving branch URL or assume that a package
contains the script. No additional release asset is necessary.

Run these commands in an onsite operator shell with `set -euo pipefail`, after filling the values from
the reviewed release handoff and the selected session's actual identity:

```bash
HANDOFF_SOURCE_COMMIT='REVIEWED_SIDECAR_SOURCE_COMMIT'
HANDOFF_SCRIPT_SHA256='REVIEWED_SIDECAR_SCRIPT_SHA256'
PARENT='SELECTED_ROOT_UUID'
CODEX_HOME_SELECTED='/absolute/path/to/selected/codex-home'
NEW_PACKAGE='/absolute/path/to/immutable/codex-plus-0.162.1-plus.0.0.4-TARGET'

[[ "$HANDOFF_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$HANDOFF_SCRIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]
umask 077
AUDIT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/codex-plus-handoff.XXXXXXXX")
curl --fail --location --proto '=https' --tlsv1.2 \
  "https://raw.githubusercontent.com/WingsOfPanda/codex-plus/$HANDOFF_SOURCE_COMMIT/scripts/handoff-observer.py" \
  --output "$AUDIT_ROOT/handoff-observer.py"
printf '%s  %s\n' "$HANDOFF_SCRIPT_SHA256" "$AUDIT_ROOT/handoff-observer.py" | sha256sum --check --strict -
```

Run the commands with shell error handling enabled (`set -euo pipefail`), so a
failed validation or checksum prevents invocation. A local source copy is also
valid after verifying the same supplied SHA-256. Verify the new package archive
and manifest against the published release handoff before selecting it.

## Inspect first

```bash
/usr/bin/python3 "$AUDIT_ROOT/handoff-observer.py" --inspect \
  --parent "$PARENT" --codex-home "$CODEX_HOME_SELECTED" --package "$NEW_PACKAGE"
```

Omitting `--inspect` has the same read-only behavior. Inspection reads `/proc`,
the ledger using SQLite `mode=ro`, the package helper hashes and the existing
health JSON. It does not import `Store`, migrate the ledger, query the daemon,
launch helpers, or signal processes. Do not substitute `park --status` for this
step: that command opens `Store` and can migrate an old schema.

The JSON lists only helpers matching **both** the exact parent UUID and selected
`CODEX_HOME`. Each helper must have the exact module argv, a Python executable,
matching package cwd/PYTHONPATH, matching helper source hashes, and Linux process
start ticks. A supervisor must own its detached session. Repeated PYTHONPATH
entries for that package and multiple verified same-runtime supervisor lock
contenders are supported. Mixed or foreign selected runtimes cause refusal;
do not guess which process to kill. Selected module invocations with Python flags
such as `-u` or `-I`, alternative parent syntax, or other unrecognized argv cause
refusal before any signal. Other root
sessions, homes, workers, terminals, and daemons are outside the signal target.

`needs_handoff: false` means no selected-parent pending ledger work. With no
selected helpers, leave it alone. Existing selected helpers are still replaced
by explicit handoff even without pending work, so an old helper cannot pick up
future registrations. No helper plus `needs_handoff: true` means monitoring is absent; it is a
supported recovery case after owner coordination. A stale heartbeat does not
prove that a watched job failed.

## Explicit handoff

Only after the user's fleet go and the onsite owner's idle/quiescence agreement:

```bash
/usr/bin/python3 "$AUDIT_ROOT/handoff-observer.py" --handoff \
  --parent "$PARENT" --codex-home "$CODEX_HOME_SELECTED" --package "$NEW_PACKAGE" \
  --evidence-dir "$AUDIT_ROOT/handoff"
```

The evidence directory must not already exist. The controller verifies the new
package's entire file/hash/mode inventory before any process control. Each file
may retain its recorded mode or have exactly its write bits removed, matching
fleet's read-only installation; other permission changes are refused. It checks
the exact root is loaded and idle using bounded read-only `thread/read` RPCs.
It checks the daemon generation again before launching; it never calls
`thread/resume`, registration, cancellation, or queue mutation RPCs itself.

It then:

1. Checks `/proc/locks` for the exact selected `parent.lock` file's device/inode
   and refuses an owner outside the identified observers before any signal.
   Stops only positively identified selected supervisors, using Linux pidfds and
   identity rechecks, before stopping selected observers. This prevents ordinary
   supervisor respawn. It sends SIGTERM to individual helpers, never a process
   group, and refuses to escalate if a helper does not stop within five seconds.
2. Rechecks the ledger identities for concurrent registration, then makes a
   consistent SQLite backup **after old helpers stop and before the new helper
   opens/migrates the ledger**. After stopping the old helpers, an explicit
   handoff opens the existing ledger with SQLite `mode=rw` and performs only a
   schema read, letting SQLite recover any hot rollback journal left by a stopped
   writer. This performs no logical SQL mutation or `Store` migration; default
   inspection remains strictly read-only. The backup uses SQLite's API, including committed
   WAL content; copying `background.sqlite` alone is not a valid backup.
3. Rechecks idle/root/daemon identity and launches the new supervisor directly
   with the selected `CODEX_HOME`, the immutable new package as both cwd and sole
   `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1`, and a new detached session. The
   supervisor launches its observer normally. Existing parks are reused.
4. Waits locally for at most 15 seconds for a fresh health heartbeat and both new
   helpers with the new package identity, with the new observer owning the exact
   selected `parent.lock` inode in `/proc/locks`, or a clean supervisor exit with fresh
   health. A clean exit can retain an unresolved stock delivery warning; it does
   not certify delivery. An `error` health state fails acceptance.
   If the lock owner appeared after the process scan, the controller identifies
   that PID directly through a pidfd and checks its runtime, start ticks, and new
   supervisor parent. A vanished owner is reconsidered within the existing local
   deadline; a positively foreign owner is refused.
5. Verifies the selected parent's original park/job identities and known job turns, every original
   callback id/client/idempotency flag and frozen message, and existing cancelled
   states. Newly resolved job turns, new completion callbacks, queue references,
   and legitimate delivery state advances are allowed. New park/job registrations
   for the selected parent fail acceptance; other roots may register normally.
   This detection supplements the owner's quiescence agreement,
   rather than providing a registration lock.

The retained private evidence contains `before.json`, `stopped.json`, a complete
`ledger.sqlite` backup, `after.json` on success, `new-supervisor.log`, and
`result.json` with process identities, package/source hashes, health, and phase.
On failure, `partial.json` records readable selected-parent current state. JSON
snapshots contain only the selected root; the consistent SQLite backup contains
all roots. Evidence includes frozen notification text; protect it as private data.
Successful handoff prints `phase: verified`. Preserve the audit directory and
report its path, selected root, stopped helper identities, new helper identities
or clean exit, fresh health, and ledger verification result to fleet-config.

If any step fails, stop rollout and inspect `result.json` and current read-only
inspection output. Old helpers may already be stopped or the new supervisor may
already be running: the recorded phase identifies that partial state. The tool
never restores its backup, restarts the old helper, re-registers jobs, or resends
callbacks automatically. Restoring old state after accepted input can duplicate
delivery. Do not rerun the handoff blindly or delete the ledger.

## What restart means

Previously watched jobs found completed after restart produce **REPORT ONLY**;
report those outcomes and wait for renewed direction. Work positively observed
still running after restart can later produce **LIVE COMPLETION**. Frozen
callbacks retain their original identity and text. Stock-daemon ambiguous
delivery stays unknown without positive queue/history evidence; neither a new
helper nor an empty queue authorizes a resend. Actual jobs keep running through
the helper replacement.

This procedure requires Linux `/proc` and Python with `pidfd_open` and
`pidfd_send_signal` (Python 3.11+ for the hashing helper). It intentionally refuses
unrecognized helper layouts; an onsite owner must investigate those separately.
Use the coherent system `/usr/bin/python3`: the new supervisor uses the
controller's `sys.executable`. This avoids an unrelated custom-Python loader or
`LD_LIBRARY_PATH` mismatch; no global Python configuration is changed.
The bounded local controller performs no model polling; normal observer
completion delivery can start a normal model turn.

## Private verification

```bash
python3 -m unittest discover -s tests -p test_observer_handoff.py -v

HANDOFF_OLD_PACKAGE=/path/to/immutable/plus.3 \
HANDOFF_NEW_PACKAGE=/path/to/immutable/plus.0.0.4 \
HANDOFF_OFFICIAL_DAEMON=/path/to/official/stock/codex \
HANDOFF_EVIDENCE=/path/to/new/private/evidence \
python3 -m unittest discover -s tests -p test_observer_handoff.py -v
```

The optional proof creates only private homes and loopback providers. It replaces
an actual old packaged supervisor/observer with the new packaged helper, recovers
an armed ledger with absent helpers, preserves an unrelated private trial,
cancelled rows and a frozen ambiguous callback, and checks live versus report-only
completion. It never opens the deployed ledger or signals deployed helpers.
