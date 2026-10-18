# Combined 0.4.0 fleet answers

Frozen package source: `8d29e095a27e2a3815006e320edb0bbacd15bfd9`.
This sidecar describes the rollout contract; it does not install or modify fleet state.

## Identity and retention

The protocol-4 detached-only draft never shipped. The frozen source and current
packaged contracts use coordinator protocol 5, execution format 1, initial 3 → 5.
The same-protocol continuation rules apply from protocol 5 after qualification.

Set `rollout.retain` to `["0.163.0-plus.0.4.0"]` in this handoff. It retains the
first qualified combined release as the floor for later releases. `previous`
will naturally keep 0.3.2 initially, and in-use folders retain their existing
protection. Remove 0.3.1 from the explicit retain list only through fleet's approved
pin change. Neither 0.3.1 nor 0.3.2 is a qualified rollback after execution or mode
records exist. Retaining a folder is not approval to run it on the new ledger;
any rollback below 0.4.0 requires new qualification first.

## Quiet and Ultracode evidence

No additional Ultracode row count is a quiet predicate. `ultracode_roots` stores
requested settings and startup provenance; `ultracode_submissions` stores frozen
admission evidence; `ultracode_turns` stores immutable effective-mode bindings.
None is a runnable dispatch queue or can start/retry a model turn. In particular,
mode on may remain set while idle, and pending/uncertain evidence may outlive the
actual submission. Counting those as active work can permanently block upgrades.

Keep the existing actual-work gates: helpers, armed parks, open callbacks and goal
dispatches, unresolved executions, and owner-confirmed absence of active/queued
native/AP/user work outside the Plus maintenance fence. Preserve mode evidence;
do not delete it or resend work to satisfy quiet.

An optional diagnostic query, on a read-only SQLite connection, is:

```sql
SELECT root, submission, generation, state, turn
FROM ultracode_submissions
WHERE state IN ('pending', 'uncertain')
ORDER BY root, submission;
```

Run it only if `sqlite_master` contains that table; absence means no mode evidence.
Report rows for inspection, not as a new blocking count. The initial installed
protocol-3 release does not have these tables. A pending mode record alone cannot
replace the separate active-turn and queue checks.

## Fresh-home operator probes

Run outside model context, with a fresh private `CODEX_HOME` and no daemon.

`codex-plus job list --all` exits 0, creates no files and returns exactly:

```json
{"execution_record_format":1,"packages":[],"schema":1,"unresolved":[]}
```

`codex-plus ultracode --help` is a strictly no-write routing probe: exit 0, JSON
object with an `error` string containing the Usage text and actions `status`,
`on`, `off`, `provenance`, `load-instructions`. The key name is the helper's existing
help-wrapper shape, not a failure exit. It starts no helper or daemon/model work.

For semantic JSON status, use:

```sh
codex-plus ultracode status --thread 00000000-0000-4000-8000-000000000001 --json
```

It exits 0 with these stable fields:

```json
{
  "version":1,
  "root":"00000000-0000-4000-8000-000000000001",
  "availability":"unavailable",
  "effective":null,
  "load":null,
  "pending":[],
  "requested":{"enabled":false,"policy_version":1,"revision":0},
  "instructions":""
}
```

Also expect `journal_directory` under that private home's
`codex-plus/ultracode/<root>/runs`, and `unavailable_reason` starting with
`Local daemon unavailable:`. Do not compare the platform-specific error suffix.
This status action changes no requested mode, submission or dispatch and starts
no observer/model call, but it initializes `codex-plus/background.sqlite` in a
fresh home. It is not a filesystem-read-only probe. Use the help probe when
strictly zero writes are required; semantic status is appropriate in throwaway homes.

The repeatable release-side check is `verify-operator-probes.py` beside this
contract. Run it with `--command /path/to/codex-plus --evidence /private/report.json`
against each extracted architecture package; it verifies the exact fields and
filesystem footprint above. It is not shipped in the runtime package.

## Wrapper, existing sessions and configuration

Only `job` and `ultracode` are new fleet helper routes. Observe, supervise and
execution-executor remain internal roles invoked by concrete package paths.

Existing threads work normally after `codex resume` on 0.4.0, with Ultracode
unavailable until they have a qualified bootstrap. **Ultracode needs a new session**
for legacy roots. This is already in the packaged release note and must also be
in the companion note. Once qualified, that root can resume normally. Restart
alone enables the other client changes; it does not retrofit Ultracode history.

Ultracode uses native dispatch configuration, not a second helper-side parser:

- `agents.default_subagent_model`: native fallback model; currently gpt-6.1-sol.
- `agents.default_subagent_reasoning_effort`: fallback high; explicit medium/high
  choices remain available and role overrides can affect the actual result.
- `agents.max_concurrent_threads_per_session`: legacy cap 8 maps to eight child
  slots plus root in pinned V2 unless an explicit V2 cap overrides it.
- `agents.max_depth=1`: preserve the fleet setting, but it is **not enforced by V2**.
  Leaf-only workers are a workflow instruction, not a guaranteed runtime depth fence.

Mark the model/effort defaults and effective concurrency configuration as relevant;
do not treat max_depth as proof of V2 isolation. Worker acceptance records actual
model and effort.

Copy execution_record_format 1 and rollout.executions wait into staged pins.
For this incompatible first cutover, `job list --all` must show no unresolved
executions, in addition to all other quiet gates. The installed 0.3.2 wrapper lacks
job maintenance; use owner-confirmed quiet for the initial switch, or a separately
reviewed staged candidate for host operations. Do not invent support in the old CLI.
