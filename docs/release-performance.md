# Release performance: 0.4.4 investigation

The 0.4.4 release took 16 h 32 m from its first recorded detached job to
publication. This was not continuous compilation. It included fixture failures,
repeated test-harness compilation, product debugging, transfers, preparation,
reviews and a confirmed 7 h 21 m pause awaiting recovery authorization.

The useful comparison is the final helper-only ARM path: building the helper,
running its focused checks and assembling the package took 158.64 seconds.
That reused an unchanged, previously qualified frontend. It is not a cold-build
benchmark or a promise that every release takes three minutes.

## Recorded costs

| Stage | Observed time | Evidence |
| --- | ---: | --- |
| Initial ARM shipping frontend build | 33 m 37 s | `release044/arm-prep/remote-summary.json`, `frontend-build` |
| Initial x86 native build wrapper | 12 m 1 s | `release044/x86-prep/work/qualification-summary.json`, `native-build` |
| Initial ARM test-harness compilation/listing | 25 m 46 s | `release044/arm-prep/remote-summary.json`, `tui-list-bottom_pane` |
| Two subsequent ARM test-harness compilation/listings | 52 m 53 s combined | `arm-recovery2-prep` and `arm-parser-final-prep` summaries |
| Initial x86 full-test timeout | 90 m | `x86-prep/work/qualification-summary.json`, `native-full-scoped` |
| Later fixture-corrected full test run | 2 m 5 s | `native-fixed-full-review/full-result.json` |
| Oversized 521 MB evidence download | 21 m 19 s | `helper-fix-qualification-prep/arm/transfer.json` |
| Later evidence-only download | 24.54 s | `arm-tail-qualification-prep/transfer.json` |
| Final ARM helper-only preparation | 158.64 s | `pipe-final-arm-recovery/download/summary.json` |
| Final ARM helper build / unit stage / package assembly | 29.86 / 45.33 / 53.71 s | Same final ARM summary |
| Final x86 helper compilation | 9.60 s | Managed log `794bf584-71f9-42fa-b764-4a1b263349b3` |

Paths in the table are relative to `.build/release044/`, except where the prefix
is already shown. They describe retained evidence, not commands to rerun.
The corrected full test run had 5,809 passes, 82 reviewed baseline failures and
four ignored tests; it was not a fully passing suite.

These rows overlap and must not be added as elapsed release time. The 21
recorded detached jobs occupied a union of 3 h 49 m, while their individual
durations sum to 5 h 37 m. Worker and root shell work also ran outside those
intervals. The remaining time is not measured idle time.

Raw Cargo logs confirm that three ARM listing commands compiled the test
harness, in different upstream clone directories. Directory changes correlate
with recompilation; the exact Cargo fingerprint changes were not retained.
We cannot attribute all recompilation to the path change alone.

## What to change in the next release

1. **Preflight before expensive work.** Check the actual interpreter and module
   origins, every selected fixture's raw socket address, archive extraction
   modes and the exact staged input set. Import or bind failures belong before
   compilation and the test matrix, not after them.
2. **Keep the build checkout and cache stable.** Use one verified upstream
   checkout for a given source/patch/build configuration. Build a test executable
   once, record its Cargo-reported path and hash, and retain it before another
   build can replace cache outputs. A parser-only correction does not require
   compiling or executing those tests again.
3. **Use the existing helper-only path.** `build-runtime.sh` builds the helper;
   `build-release.sh --native-dir ...` assembles supplied binaries without
   compiling the frontend. Reuse requires matching source tree, patch,
   toolchain, target, features/flags, lockfile, resources and relevant build
   environment. Preserve the original compile origin. Unknown inputs require
   review; a changed helper still needs its affected native/package checks.
4. **Separate evidence from artifacts.** Routine evidence bundles contain
   reports, dedicated result files, logs and captures. Do not also include the
   native directory, extracted package, source clones and another archive copy.
   Qualify on the native host and fetch the accepted archive once by digest.
5. **Preserve completed work and truthful outcomes.** A failed run stays failed.
   For the existing packaging path, reuse only the binaries/results allowed by
   [native package requirements](releasing.md#native-binaries) and
   [saved native results](verification.md#saved-native-results). The new runner's
   [reuse contract](release-protocol.md#changes-and-evidence-validity) is stricter.
   Track fresh and retained work separately; historical durations are not fresh time.

## Cleanup and result handling

Run process cleanup independently of test and parser success. A failed test
must not automatically become "cleanup unknown", and a parser exception must
not skip the cleanup audit. Use exact owned-process identities and the qualified
supervisor. An unreadable same-user process is not automatically a test process:
the final release encountered an idle shared SSH ControlPersist connection.
Its transport ancestry and absence of test descendants were verified separately.

Use libtest's dedicated `--logfile` and an independently captured `--list` as
the result inventory. Pass an absolute logfile path: Cargo may run the test binary
from the crate directory rather than the caller's directory. Terminal rendering can concatenate ordinary text with
test rows; stripping ANSI escapes does not repair that ambiguity. The command
exit status, final counts and exact allowed ignored reasons must also agree.
Baseline failures require their existing separate review; they must not be
silently treated as passes by a parser or cache.

## Run the preflight

Use Python 3.11+ from the same environment that will run the selected fixtures.
Write a reviewed JSON input with `schema_version: 1` and at least one check:

```json
{
  "schema_version": 1,
  "python_path": ["/absolute/staged"],
  "imports": [{
    "module": "controller", "origin": "/absolute/staged/controller.py",
    "symbols": ["run"]
  }],
  "staged_inputs": {
    "root": "/absolute/staged",
    "files": {"controller.py": "REVIEWED_SHA256"}
  },
  "socket_paths": [{"name": "advisor", "path": "/absolute/planned/home/control.sock"}],
  "archive": {"path": "/absolute/package.tar.gz", "sha256": "REVIEWED_SHA256"}
}
```

Replace the example paths, symbol and digests with the frozen plan's actual
values; include every staged regular file and selected fixture socket path.
Omit unused check groups. Import and socket groups cannot be empty; an import's
`symbols` may be empty for an import-only check. Paths are absolute except the
staged file inventory's canonical relative names.

```sh
umask 077
/absolute/fixture-python -B scripts/release-preflight.py preflight-input.json \
  --scratch-parent /tmp --output /absolute/new-preflight-report.json
```

On ARM use a short private scratch parent under the approved build storage.
The output refuses overwrites and records each selected check's duration and
failure, plus the actual interpreter identity. Stage closure is checked before
imports; failed closure prevents importing unverified staged code. Imports are
trusted code execution, and resolving dotted names can import parent modules.
This is not a sandbox. Run one preflight per intended interpreter/environment.

Socket checks count the raw encoded address without normalizing `..`, inspect
the nearest existing destination parent, and bind in owned scratch space at the
same byte length (or the minimum scratch length for a shorter address). They do
not create the supplied destination or prove that a future fixture will bind
there. Keep each fixture's actual setup smoke check as well.

The optional archive check verifies the trusted digest, rejects unsafe members,
preserves file and directory modes despite `umask 077`, then invokes the existing
strict package verifier. It supports the verifier's current release only, not
arbitrary older donor packages. Archive checking is useful after assembly or
transfer; it is not a prerequisite for compiling a new package.

## Verification of the tooling change

The focused checks cover missing imports/symbols, wrong origins without executing
the wrong module, changed or incomplete staged inputs, raw 108-byte addresses,
scratch cleanup, restrictive-umask extraction and malformed archives. The result
validator checks missing/duplicate/failed results, nonzero exit status and exact
ignored reasons, including under Python optimization.

Root replay of preserved ARM goal-test evidence reproduced the old parser's
73-of-74 refusal. The new validator accepted all 74 dedicated results and the
original successful command receipt without rerunning native tests. Parsing is
not faster: the worker's saved-evidence microbenchmark measured roughly 0.19 ms
old versus 0.33 ms new. The benefit is preventing false failures and unnecessary
reruns. A reproduced 108-byte fixture address was rejected by the preflight CLI
in 0.055 seconds, before launching any build or test matrix.

Independent root acceptance passed 20 preflight tests, four result-validator
tests and 42 existing packaging tests. The real 0.4.4 x86 archive
(`1007f5d0cdf34ccc5debd7f7c42a43c38450e8a1fd3602c11552cd50ece463b6`)
also passed extraction and strict verification of all 52 files under `umask 077`.
That combined archive/import/scratch-socket preflight took 3.56 seconds locally.
Its report and the saved 74-test replay are retained under
`.build/build-speed-044/root-acceptance/`. These tools were tested on x86;
the saved test evidence came from ARM, but the tools were not rerun on ARM.

## Measurement limits

Record actual start/end and wall duration for preflight, frontend, helper,
packaging, harness construction, tests, compatibility/captures, AP replay, transfer,
recovery, review/approval wait, publication and rollout. Mark overlap and cache
conditions. The [0.4.7 qualification record](releases/0.163.0-plus.0.4.7/qualification.json)
records ARM native/package time of 2726.949489 seconds, including its 1055.888969-second
harness build; the latter is not extra time. Its 319.2385-second archive transfer
is separate. These stage observations exclude preparation/review/recovery gaps.

For warm-cache or CPU comparisons, use the
[controlled comparison method](release-protocol.md#publication-and-timing).

The retained records establish the costs above and show that the final scoped
helper path worked. They do not establish a new end-to-end release speedup,
network throughput, a cold-cache compiler benchmark, or complete accounting of
all agent/worker preparation time. Before/after checks for tooling improvements
must name the operation being measured and preserve those limits.

The detailed overlap-safe inventory and source references are retained in
`.build/build-speed-044/timing/timing.json`; its analysis script checks interval
arithmetic, actual execution records and fresh-versus-retained stage identities.
