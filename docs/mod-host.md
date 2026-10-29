# Codex mod host v1

Implementation contract for the shared-mod pilot. Trusted internal packages only;
this interface is not an OS sandbox. The official daemon remains unchanged.

## Configuration and pinning

Read optional `$CODEX_HOME/codex-plus/mods.json` once per TUI process:

```json
{"schema":1,"root":"/absolute/mods","node":"/absolute/node","enabled":["job-watch"]}
```

Absent file or empty enabled list means no mod work and unchanged UI. Use only
explicitly enabled safe IDs (ASCII letters/digits, dash, underscore; 1–64 chars).
Root and Node paths must be absolute. No installation, discovery-based
auto-enablement, updater or pruning belongs to the host.

Resolve `<root>/<id>/current` to a version directory under `<root>/<id>/`
once; keep that exact directory for this process. Check Node >=22.18.0 once.
A new TUI process (`codex resume` included) may select a new version; merely
changing threads in an existing TUI does not.

Each directory contains mod.json and SOURCE.json. SOURCE.json is:
`{"schema":1,"commit":"<40 hex>","files":{"mod.json":"<sha256>","host/view.mjs":"<sha256>",...}}`.
The file map covers all regular package payload files, excluding SOURCE.json.
Reject symlinks/nonregular entries, traversal and missing/extra payload files.
Verify listed hashes before execution. Receipts establish integrity within a
trusted same-user installation, not authenticity against malicious local code.

## Manifest

```json
{
  "id":"job-watch","version":"0.6.0","host_contract":1,
  "runtime":{"node":">=22.18.0"},
  "entry":"host/view.mjs",
  "provides":["view.rows"],"inputs":["session.identity"],
  "refresh":{"min_interval_s":15,"timeout_ms":5000},
  "ui":{"rows":3},
  "self_test":"--self-test"
}
```

v1 supports view.rows and session.identity only. Runtime accepts a minimum Node
version in the shown >=major.minor.patch form. Refresh floor is 15 seconds;
timeout is clamped to 100–5000 ms; requested rows to 1–6. Entry is a relative,
receipt-bound regular file. Optional declared access metadata is informational,
never a promise of enforcement. Version/ID must match the selected directory/ID.

## View protocol

Spawn configured Node with the absolute entrypoint directly, no shell. Send one
UTF-8 JSON object then close stdin. Request:
```json
{
  "host_contract":1,"op":"view","request_id":"<unique string>",
  "session":{"thread_id":"<uuid>","codex_home":"/absolute/home","tmux_pane":"%148"},
  "width":100,"rows":3,"now":1793260000000
}
```
tmux_pane is optional; do not infer or widen scope if absent. Root/session
identity is data for the package, never authority to write records or wake models.
now is Unix milliseconds, width terminal columns and rows the granted budget.

Response:
```json
{
  "host_contract":1,"request_id":"<same string>","status":"ok",
  "rows":[{"text":"One local job","severity":"normal","age_ms":1000,"share":0.5}],
  "overflow":0,"diagnostics":""
}
```

status is ok, empty, unavailable or error. rows is summary-first, ordered and
truncatable; empty means no rows. severity is normal, ok, warn or error.
age_ms (nonnegative integer) and share (finite 0..1 fraction) are optional
metadata, not required decorations. overflow defaults to zero; diagnostics is
optional and logged rather than rendered. Maximum 64 returned rows and 2048
UTF-8 bytes per text; cap stdout at 64 KiB and stderr at 16 KiB. Reject invalid
responses, unexpected request/contract identity, exit failure or output overflow.
Stderr alone with valid stdout and exit zero is not failure.

A `--self-test` invocation is optional installer/conformance work, not a refresh.
It exits zero on success; it does not need to return a view response.

## Native lifecycle and display

Each mod has independent pending request, child, cached result and failure
state. Never block the UI or fold it into existing watcher/advisor/goal reads.
At most one process per mod, with a global cap of two. Failures back off to
60 seconds. Use a new process group; cancel/kill/reap owned groups on timeout,
overflow, view change or exit. Inherit no NODE_OPTIONS/NODE_PATH; use a controlled
environment with HOME, locale, tmux context and fixed /usr/bin:/bin PATH.
Packages may not rely on model credentials being forwarded. This is still
trusted Node code and can access the user's files/network without a sandbox.

Reject replies for a prior native thread/request. Do not invoke for a remote
backend or missing primary thread. Logs and diagnostics must not mutate the
package or shared job stores.

Native notices/activity/composer keep existing priority and row limits. Mods
use remaining space in config order: one summary per mod before details, bounded
overflow if space permits. Never exceed the shared six-row budget. The host adds
the package ID and strips control/escape characters. With no room, omit mod
rows; logs retain diagnostics. Zero configured mods preserve existing output.

## Job-watch consumer scope

Package-owned Node collector: local marker records only, safe fs reads and
fixed tmux argv; no shell, SSH, AP, card reader, until/progress/show execution.
Pane records must match both owner and current pane; pane-less records may match
the current tmux owner. Missing tmux identity yields empty. Unsupported owned
record types produce an explicit unavailable indication, not fabricated state.
No records from another owner/live pane and no JOB_ALL, leader locks or writes.

Both production adapters execute shared classification, with host-specific
collection and formatting. Claude retains its history/golden behavior. Codex has
no delivery history; use only a valid matching terminal marker for age, and
never collapse unknown-age records. No wakes, Close action or goal integration.

## Acceptance and implementation ownership

Mods owner owns shared classification, entrypoint, manifest and semantic tests.
Codex owns native host, generic conformance suite and native acceptance.
No package names or domain readers in native code.

Build once, load job-watch plus a differently named non-job fixture, update and
rollback the fixture with unchanged native binary. Test output/timeout/grandchild
cleanup, stderr success, malformed/wrong-ID replies, config-order budget, no
overlap, thread switching, zero-mod regression, and zero provider requests.
Use small controlled local marker jobs, never an expensive build just to test
display. Prove no record commands or shared-store writes.

Compatible module updates need no native build; new capabilities may. Publication,
merge, fleet installation, automatic advancement and pruning remain outside this
development implementation.
