# Rust runtime

Plus 0.3 replaces the installed Python helper with one native executable,
`bin/codex-plus-runtime`, behind the existing Bash launcher. It owns managed
shell execution, advisor consultations, registration, observation, supervision,
and cooperative upgrade. The 0.4.0 detached-job candidate adds reviewed execution
ownership without changing the official daemon. The TUI, daemon and executor
remain separate processes.
The preserved `codex_plus/` source is the Python 0.2 compatibility oracle;
packaging rejects Python source and bytecode in the product tree.

## Build and boundaries

Run `./scripts/build-runtime.sh` with Rust 1.99.1. The helper uses a separate
locked Cargo graph and bundled SQLite. Its runtime shared libraries are limited
to the GNU C/runtime libraries checked by the packager. It adds no TLS or SQLite
shared-library requirement. Python 3.11+ is used only by build/test tooling.

`ledger.rs` owns transactions and durable state. `background.rs` owns RPC
observation and scheduling; no transaction spans an RPC. `rpc.rs` bounds Unix
WebSocket messages and aggregate call deadlines. `evidence.rs` reads bounded
exact-turn evidence. `local.rs` owns private files and child execution.
`runner.rs` and `advisor.rs` preserve command I/O and cancellation contracts.
`upgrade.rs` validates the adjacent immutable package before re-execution.

Plus 0.3.2 added `goals.rs` for durable goal policy and
`goal_command.rs` for the root CLI, using this same store. Goal admission and
user mutations share a bounded local lock around queue submission; SQLite
transactions still do not span network calls. The [goal workflow](goals.md)
defines the protocol-3 cutover, pending user-input fence and recovery barrier.

The `0.163.0-plus.0.4.0` candidate uses coordinator protocol 4 and package
`execution_record_format: 1`. `execution_admission.rs` verifies the exact root/turn's
effective permissions and qualified Linux containment. `job_command.rs` binds
root review to current input/goal authority; `executions.rs` and its executor
module own immutable inputs, resource reservations, process identity and durable
outcomes. `maintenance.rs` fences new admission. Execution truth is separate
from the watch/callback ledger, so observation loss cannot authorize replay or
erase a result. The [handoff workflow](detached-jobs.md) owns the CLI and manifest
contract; this document does not duplicate their schema.

Native workers prepare expected runs over eight minutes and hand them to the
root before launch. An allowed quick check that overruns finishes in a bounded
local wait. Eight minutes is an estimate, not an execution deadline or transfer.

The migration preserves ledger schema and status JSON version 1, exact job and
callback identities, frozen payloads, uncertainty, and cancellation provenance.
Python Unicode validation is retained for reasons and compact labels.
Child launch uses `execve` without a shell fallback, including upgrade execution;
a malformed target therefore leaves the old observer alive with a warning.

## Qualification

Build the helper, then run:

```sh
cargo +1.99.1 test --locked --manifest-path rust-runtime/Cargo.toml --lib
python3 tests/test_rust_runtime_acceptance.py --runtime .build/codex-plus-runtime -v
python3 tests/test_rust_runtime_runner.py --runtime .build/codex-plus-runtime -v
python3 tests/test_rust_runtime_upgrade.py --runtime .build/codex-plus-runtime -v
python3 tests/test_rust_runtime_stock_integration.py \
  --runtime .build/codex-plus-runtime --stock-binary /absolute/official/codex \
  --evidence .build/rust-stock --scenario all
python3 tests/test_rust_runtime_stock_lifecycle.py \
  --runtime .build/codex-plus-runtime --stock-binary /absolute/official/codex \
  --evidence .build/rust-stock-lifecycle --scenario all
python3 tests/test_detached_jobs_stock.py \
  --runtime .build/codex-plus-runtime --stock-binary /absolute/official/codex \
  --evidence .build/detached-stock/completion --scenario completion
```

Python drivers run outside the tested product, reuse the 0.2 ledger oracle,
and assert effects through the native executable. Stock fixtures use the pinned
official 0.163.0 daemon with loopback providers. Reused Python-only runner mocks
are explicitly skipped; native black-box checks cover the shipped behavior.

Separate Python-free filesystem checks exercise the actual launcher/helper with
no interpreter or `libpython`. These functional filesystem fixtures do not
establish a production security boundary. Complete release archives still need
[extracted-package qualification](releasing.md#qualification-and-handoff).

Repeat detached-job acceptance for the affected scenarios and each target;
the [acceptance matrix](detached-jobs.md#acceptance-gates) includes real daemon
restarts, restricted admission, uncertain outcomes, monitoring and cancellation.
Private source/prototype results do not qualify an untested complete archive,
fleet service lifetime or live upgrade.

## Fleet contract

The original Rust migration was a protocol-2 bootstrap from Python protocol 1: use the
[quiet cutover and rollback procedure](background-work.md#cooperative-package-upgrades).
The shared daemon stays stock 0.163.0. Fleet helper scans must recognize both
`python3 -m codex_plus.background` and native `codex-plus-runtime` process roles.
Observer and supervisor roles retain a concrete
`--package-root /absolute/package/` pin.
The ledger location and callback-state names are unchanged. Wrapper commands and
global model workflow instructions need no syntax change for the migration.

Preserve the last qualified Python package and its interpreter explicitly until
that rollback is retired. Rust helpers do not transfer to older or protocol-1
packages; every rollback uses a quiet checkpoint. Build/test-only Python remains
permitted, and removing system Python is outside this change.

Custom goals require protocol 3, effective native-goal disabling and a stock
daemon restart at quiet cutover, despite keeping upstream 0.163.0 unchanged.
The wrapper adds `goal`; quiet checks also inspect goal dispatches and distinguish
active versus settled goal-owned callback markers. Preserve the goal ledger and
a qualified rollback package without dispatching its goal records through older
helpers. See the [goal rollout contract](goals.md#stock-daemon-isolation-and-rollout-contract).

Detached jobs require a quiet initial protocol-3 to protocol-4 cutover.
Fleet scans must additionally recognize the private `execution-executor` role,
its root/execution identity and leading package pin. Each executor keeps the
original package executable, cwd and open descriptors while the coordinator may
change packages. Retain old packages for running and unresolved executions;
executor record-format compatibility does not waive coordinator/upstream/target
checks or quiet rollback.

An operator maintenance hold blocks new Plus launch/model admission while
existing executions continue. Its `admission_quiet` result is separate from
`execution_quiet`, and neither replaces native/AP fleet quiet checks. Restricted
or unknown launch environments refuse admission; there is no host-service
sandbox bypass. Restart continuity is limited to qualified unrestricted user
session/tmux contexts and excludes reboot or executor failure. Installation,
global policy and fleet retention/reconnect support remain separately owned by
fleet-config; this source change does not activate them.
