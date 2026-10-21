# Approved maintenance record contract

Root approved this narrow test correction after the strict failed gate and its
59-snapshot diagnostic identified existing 0.4.2/0.4.3 maintenance behavior.
No runtime behavior, source, package or prior evidence changes.

The quiet record window starts before initial maintenance normalization, after
the real execution and mixed input/steering scenarios have completed and the
private observers have stopped. Every JSON record under the execution directory
is included; manifest, result, state and observation are mandatory.

- Every command compares its before snapshot with the prior command's after
  snapshot using full byte equality. Final and cleanup captures do the same.
- Pure reads and maintenance leave require identical file sets and all bytes.
- Exact `job maintenance enter` and `job maintenance status` may refresh only
  the single top-level numeric `state.updated_at`. It must be finite and advance
  monotonically; unchanged state bytes are also allowed. Any other state byte,
  JSON formatting change, field mutation or file addition/deletion fails.
- Result, manifest, observation and every other JSON file remain byte-identical
  during all calls, including those maintenance controls.

Maintenance status reconciles durable terminal receipts and calls `save`, which
refreshes this timestamp. The maintenance module and `save`/`reconcile` functions
are identical to released 0.4.2. Status also reconciles when the hold is false.
This allowance is not applied to arbitrary reads or between-call changes.
The strict failure and full diagnostic trace remain in their original directories.

The existing seven named checks, two admitted policy snapshots, two hold
directions, exact SQLite rows/schema/identities, genuine candidate execution,
one workload run, zero control/read provider requests and cleanup remain required.
New-helper reservations retain sixteen-minute policy text; old-helper admitted
snapshots retain eight-minute text. Policy version stays 1.

`record-contract-checks.py` covers valid refreshes and forbidden other fields,
formatting, nonfinite/backwards timestamps, read/leave/between mutations and
new/deleted files. The full gate uses this same validator for each boundary.

## Additional native report fields

The original report fields and seven-check count remain. New fields are:

- `execution_record_calls`: ordered helper calls with package/argv, exact before
  and after hashes for every JSON file, timestamp allowance/refresh flags,
  before/after clock values and `between_call_bytes_unchanged`.
- `execution_record_contract`: `schema: 1`, allowance text, initial/final hashes,
  refresh count, and required true flags `file_set_unchanged`,
  `immutable_json_bytes_unchanged`, `state_other_bytes_unchanged`,
  `read_and_leave_bytes_unchanged`, `between_call_bytes_unchanged`,
  `cleanup_capture_bytes_unchanged`.
- `execution-record-baseline/`: exact initial bytes. Final bytes are retained
  in `fixture/executions/v1/ROOT/EXECUTION/`. `execution_record_sha256` retains
  the earlier post-normalization baseline hashes for compatibility with readers.

Root must review the corrected gate/hash and run independent native acceptance
on both architectures. A worker proof is separate evidence and does not complete
release qualification. The frozen publication verifier is untouched; its owner
must bind these reviewed fields and the new gate hash before publication.
