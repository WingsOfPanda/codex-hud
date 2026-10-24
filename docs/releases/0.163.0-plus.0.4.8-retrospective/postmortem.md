# ARM 0.4.8 release retrospective

Review date: 2026-10-24. Owner: native ARM session on 910B. This is a read-only review of preserved evidence, not a new qualification run.

The ARM path eventually passed **418 native tests, all five package compatibility gates, nine Advisor checks, 20 cursor captures and 14 recovery cases**, with workload/cleanup exit 0 and independent ownership audits clear. The coordinator subsequently reported accepted AP replay (23 tests/two busy pairs), publication and five verified release assets; fleet-config reported the four-host rollout complete. Those last two statements are attributed relay evidence, not fresh remote verification in this review.

The largest preventable costs were late discovery of fixture/environment assumptions, repeated qualification under refrozen plans, and several serial review/transport boundaries. There is no evidence here that removing ownership checks or increasing timeouts would have been a valid shortcut. A single conditional native-owner brief, complete preflight under actual host conditions, and one reviewed verifier for the entire evidence lifecycle would remove routine stops without accepting failures.

## Evidence and method

All times below are Asia/Shanghai (+08:00), on **2026-10-23** unless stated otherwise. References E01–E54 resolve to absolute paths and fresh SHA256/size/mode/mtime records in [evidence-index.json](evidence-index.json). The appendix below gives the most important paths. [chronology.json](chronology.json) records exact local transcript lines/timestamps and selected messages. [timings.json](timings.json) contains managed envelopes, stage journal intervals, individual command durations and measured handoff boundaries.

Managed elapsed time uses stable `started_at` and the raw result's `finished_at`; later `state.updated_at` refreshes are not finish times. Stage intervals use paired nanosecond journal records. Command durations are nested within stages and must not be added to them. Cross-host work overlaps; the handoff table is **not a sum of lost time**. No pure human deliberation, network throughput or model-compute accounting is available.

Local files were read directly. iris-mem search returned `unavailable / daemon_deadline` after 5 seconds; no recalled memory substantiates this report. The built-in memory registry had no relevant hit. No build, test, trial, consultation, install, signal, cleanup, record resolution or sealed-checkout status operation was run.

## Chronological failures and blockers

| Time | Observation and supported cause | Resolution / remaining limit | Evidence |
| --- | --- | --- | --- |
| 10:41–11:11 | Coordinator was repairing packaging Rust discovery and preparing HUD/stale-job changes before sending source97. This was a preparation boundary, not an ARM compiler failure. | Native runtime/compiler/cache paths existing did not prove the packaging child's compiler discovery. Final native packaging preflight passed. Exact initial x86 failure/timing was not independently inspected here. | Transcript lines 1357, 1406; E25, E44 |
| 11:18 refusal; 11:26 correction | Real non-rehearsal prepare selected `/usr/lib/aarch64-linux-gnu/pkgconfig/libcap.pc`, but its content said `libdir=/usr/lib`. The committed probe then required absent `/usr/lib/libcap.so`. The actual AArch64 ELF/header existed. | Private copy changed **only** libdir to `/usr/lib/aarch64-linux-gnu`, with original PC/diff and PC/header/linker pins retained. Committed probe passed. No global symlink/install. Revised combined plan was required before using the changed profile. | E01, E02 |
| 11:38 coordinator update | X86 built an archive but an old footer fixture still expected obsolete wording instead of intentional `needs review`. Its cleanup was unresolved. ARM was explicitly stopped. | Coordinator later integrated the assertion and executable-mode/provenance repair. ARM did not independently reproduce this x86 incident. Earlier real setup rehearsals had not exercised the final changed gate. | Transcript lines 1547, 1753; E25 |
| During the14:33 assignment | An unrestricted offline Cargo metadata query tried to resolve uncached Windows `anstyle-wincon v3.0.11`. | Target-filtered native metadata succeeded. This was not proof that the ARM build needed a download or install. Future cache preflight must name the native target. | E53, E54 |
| 14:44 review | First helper unit run: **204/205**, exit101. I omitted `HOME` from an otherwise clean child environment; the test explicitly requested caller HOME through env_refs. | Approved HOME-only correction; all205 passed in fresh logs. No source fix was needed. The original failure remains. This was an executor-of-the-recipe mistake, not evidence of a runtime product defect. | E03, E04 |
| 15:05 review | First debug-helper `cleanup-recovery-success` failed at its **18-second driver publication checkpoint**: one failed, 13 unrun. No recovery-policy assertions were reached. | Initially unknown cause; stopped rather than lengthening a deadline. One read-only probe measured the same 75,556,144-byte helper's debug SHA loop at 9.628s (9.620 CPU seconds), versus sha256sum 0.07575s, same digest. | E05, E06 |
| 16:23–16:36 | Standard release-profile helper built in112.307s; all14 cases then passed in88.918s with unchanged deadlines. | Strong support for unoptimized hashing overhead as the timing bottleneck. The probe was single-shot, Rust-first, different implementations; it was not a controlled cache benchmark or a complete per-command causal trace. Keep hash validation/deadlines intact. | E06, E07, E08, E47 |
| 16:36 | Three optimized-case state/result `finished_at` values differed by exactly one representable float; original bytes were retained. | This was an early warning for a shared validator issue, not a reason to normalize records. It later blocked live release cleanup. | E08; transcript line2184 |
| 17:05 readiness; 17:48–18:00 root repair | ARM v2 prepared175 inputs and did **not launch**. X86 native Rust worker_hud failed55/56 because its expected grouped stale row omitted `longest`. | Assertion-only correction; production rendering unchanged. Root qualified the affected filters and sent v3. The v2 prepared attempt/socket remained held. | E48; transcript lines2286,2304 |
| 18:10–18:39 v3 | First actual ARM full native run: preflight/frontend/runtime passed. Archive was produced, but **package stage failed**: both `wrapper_check` and `stdio_check` hard-linked a package on /data (dev64513) into /tmp (dev64512), raising EXDEV. | Two `copytree(..., copy_function=os.link)` sites changed to normal copy in1458d9e. Later direct checks copied the preserved package from /data into fresh /tmp roots, asserted different devices and passed. No old qualification receipt was replaced. | E09, E10, E12, E16, E45 |
| 18:39 cleanup | v3's four journal pairs included the package's exit1. The committed journal verifier rejected nonzero terminal events, making `completed_local_supervision=false` and `unknown=[incomplete_guard_journal]`, despite no live owned processes. | Genuine cleanup-proof refusal; pairing alone did not satisfy that verifier. Raw failed workload/cleanup and archive stayed failed/preserved. Separate exact-identity recovery was required; physical absence did not make the failed gate pass. | E11, E12, E28 |
| 18:39–18:57 | Legacy REPORT ONLY was delivered; dependent actions required renewed user direction. | Root explicitly renewed recovery at18:57. This particular pause was required by the then-active barrier, not missing initiative or an invitation to blindly replay the command. | Transcript lines2446,2540 |
| 19:01–19:48 | Exact source/index/175-input, eight identity/group/session absence, free executor lock and conservative ownership checks passed. Recovery validator still rejected raw result vs state copy: only `finished_at` differed by **2.384185791015625e-7s**, exactly one ULP. | Separate committed f873 validator accepted only finite adjacent floats with every other result field exact. CQ removed by exact inode/quarantine path, supported job resolution and recovery seal completed. Failed verdict and raw result/archive unchanged. | E13, E14 |
| 19:48–19:57 | Physical cleanup resolved, but forensic protocol status/export still used another exact result equality and rejected the same preserved pair. | 1142193 shared the narrow comparator; status/export/import path repaired without repeating cleanup or changing receipts. Actual forensic export accepted; original failed package still rejected. | E14, E15, E27 |
| 19:48–19:53 | Coordinator found x86 compat had selected installed read-only047 instead of its canonical archive package. ARM had not launched compat. | ARM staged pinned047 archive privately, preserved archive modes, verified61 manifest files plus manifest (62 total), tree6c6c736d…; installed package untouched. Cross-device diagnostics and canonical staging completed. | E16, E17, E30; transcript line2540 |
| 20:03–20:23 | v4 c071 prepared, but strict prelaunch audit found new unreadable SSH identity PID814550/start251415257/hash86054903… after baseline capture. Process chain and tmux client identified unrelated `ia-910b` login. | No UUID/workload had been created. Exact conditional direction allowed one new normal init/prepare: old11 baseline entries matched; only this proven identity added; proposal changed only attempt paths. Old c071/VO preserved, no foreign signal or old-baseline rewrite. New c394 launched once. | E18–E20, E50 |
| 21:02–21:10 | While ARM v4 ran, x86 compat reached cursor gate and failed assertions expecting `unknown/stale` where048 rendered `needs review`. | Root correction replayed20 saved captures/264 checks and passed a diagnostic full five-gate run, including14 recovery cases. Product stayed05; tooling became59. These were root-reported results; ARM did not run the obsolete compat plan. | Transcript lines3188,3202 |
| 21:22–22:45 | v4 native passed418 tests. New immutable combined plan/product-tooling split then required v5 fresh native evidence; v4 receipts could not be transferred. | v5 re-executed native stages under product05/tooling59/plan733 and passed418. This repeat was expensive but consistent with the selected protocol. Earlier complete fixture qualification could have avoided the refreeze. | E21, E26, E33–E36, E42 |
| 23:16–23:32 | Authorized ARM compat preparation bound36 direct inputs and305-file closure. My extra review initially enumerated **sorted serialized closure keys** instead of generator insertion order used for numeric snapshot paths. | Reconstructed the original generator order and verified exact inputs; no input/source edit and no repeat prepare. This was a private reviewer assertion error, not a candidate or managed execution failure. | E23, E29, E38 |
| 23:32–23:35 actual run; 23:44 proof returned | Original59 compatibility passed all five gates,20 captures and14 recovery cases. Root audit correctly recorded `socket_removed:false` because the native socket had already been removed/sealed. Original final core wrongly required a fresh true flag. | Separate qualified655 verifier accepted false only by validating the exact earlier hash-bound native removal seal plus compat removal/audits. Execution stayed on59. No report edit or cleanup rerun. | E22, E27, E39–E43, E51–E52 |
| 23:38–23:43 review | My extra exported-tar check compared integer mode256 to the core's string `"0400"`, falsely rejecting attempt.json after a successful export. | Separate read-only check parsed octal mode and passed against the **existing export**; original extra-check log/script retained. No export replay or product change. | E24 |
| 23:34–23:44 messaging | Coordinator's “no proposal yet” snapshot arrived0.512s after the local armed-launch final reply; later “still running” acknowledgment arrived after verified completion. Completion callback was delivered after the active verification turn. | Read the exact UUID/markers and existing result; no duplicate launch/observer. Notification latency/stale remote snapshot is not evidence that workload or observer failed. | Transcript lines3740,3746,3917,3923,3941 |
| 23:56; Oct24 07:46 | Coordinator reported AP acceptance and publication; fleet-config later completed rollout under maintenance holds. | Native assignment closed. Running TUI remains an older pinned process until user restarts; installation alone does not prove frontend replacement. No native-owner install or main merge. | Transcript lines3957,3975,4009 |

**Correction to a tempting summary:** “An archive exists” did not mean v3 package acceptance passed. The package gate itself failed, its output fell back to `Stage produced no report`, and native-tests was never entered. The decisive failure remained visible in package-seven raw evidence. Keep this distinction in the release book and incident template.

## Measured work, review and waiting

### Native execution

These are managed **envelopes**, including orchestration and cleanup, not isolated compilation costs.

| Execution | Local start → final raw result | Elapsed | Outcome |
| --- | --- | ---: | --- |
| v3 7f9e… | 18:10:03.935 →18:39:22.675 | 1,758.740s (29m18.740s) | package failure; cleanup failure later separately resolved |
| v4 26f8… | 20:21:13.577 →21:22:36.764 | 3,683.187s (61m23.187s) | native0 / cleanup0 |
| v5 2098… | 21:45:57.161 →22:45:10.228 | 3,553.068s (59m13.068s) | native0 / cleanup0 |
| v5 compat c7c4… | 23:32:09.946 →23:35:20.011 | 190.065s (3m10.065s) | compatibility0 / cleanup0 |

Paired supervisor-journal stage envelopes, nested inside the rows above:

| Stage | v3 seconds | v4 seconds | v5 seconds |
| --- | ---: | ---: | ---: |
| Preflight | 10.312 | 10.266 | 10.310 |
| Frontend | 1,632.477 | 1,552.607 | 1,561.934 |
| Runtime | 26.846 | 26.440 | 1.993 |
| Package + package-backed tests | 72.339, failed | 368.423 | 374.431 |
| Native tests, including TUI harness compilation | unrun | 1,694.061 | 1,573.020 |

Actual frontend Cargo build commands took1,600.505s /1,521.924s /1,530.174s. The v4/v5 TUI harness build commands took1,611.721s /1,525.229s; these dominate their “native tests” stages, rather than the205 helper assertions (6.719s /7.065s). Do not describe the whole native-tests envelope as assertion execution time. The initial debug unit listing cost36.261s, failed unit command21.576s; HOME-corrected units13.000s and debug helper build3.632s; first failing private case25.635s. Optimized helper build112.307s and14 cases88.918s are separate helper-validation measurements. [E04–E08,E33–E36,E46–E47]

### Handoff / repair boundaries

Each row is an independently measured **elapsed boundary**. Work inside is not fully instrumented; none proves all elapsed seconds were human idle. Rows overlap remote work and must not be summed into a release-speed estimate.

| Boundary | Local interval | Elapsed | What can be said |
| --- | --- | ---: | --- |
| Private libcap correction returned → runtime assignment | 11:27:48.113→14:33:32.126 | 3h05m44.013s | Included x86 failure/recovery implementation and authorization; no ARM full build authorized. |
| HOME failure report → HOME-only approval | 14:46:34.216→14:54:56.863 | 8m22.647s | Review/authorization boundary after explicit stop-on-failure instruction. |
| Probe result → optimized-helper direction | 15:15:04.118→16:21:35.878 | 66m31.760s | No local build measured inside; remote diagnosis, queue and other work not separated. |
| v2 readiness → corrected v3 preparation | 17:05:11.293→18:00:03.106 | 54m51.813s | Cross-host stale Rust assertion repair; v2 never launched. |
| Legacy report delivered → renewed recovery | 18:39:23.665→18:57:21.205 | 17m57.540s | Required report barrier; includes local read-only diagnosis. |
| Validator refusal reported → repaired tooling direction | 19:01:06.902→19:41:11.441 | 40m04.539s | Root implementation/tests/delivery, not measured as pure wait. |
| v4 c071 readiness → reviewed launch direction | 20:03:51.001→20:06:28.440 | 2m37.439s | Routine proposal review was already short. |
| SSH blocker reported → conditional fresh-baseline grant | 20:11:43.117→20:14:04.942 | 2m21.825s | Concrete identity decision; subsequent local prepare/review still took work. |
| v5 native proof first returned → actual package/compat acceptance | 22:50:27.031→23:16:15.649 | 25m48.618s | Includes archive transfer/root acceptance; transport vs review not measured. |

Compat grant23:16:15.649 → first prepare intent23:23:45.206 was7m29.557s of local reading/checking/preparation; actual managed start23:32:09.946 was15m54.298s after the grant. This was **not another permission wait**. It included full closures, source/package checks and the private input-order mistake. Actual compat finished before23:35:21; final verification/export returned23:44:05.087. The remaining ~8m45s was the local review/export interval, including one extra-check error, not compat workload duration.

No controlled CPU-jobs/cache-layout A/B or network transfer benchmark exists. Resource availability alone does not prove that increasing jobs above16 would shorten the release.

## Repeated work and its boundaries

- Six real048 ARM attempt directories were created: initial libcap refusal8d06; v2 prepared/unlaunched04c7; v3 failed7340; v4 baseline-blocked/unlaunchedc071; v4 launchedc394; finalv5f321. The three unlaunched/refused preparations did **not** compile. Their sockets/evidence were deliberately preserved.
- There were **three** ARM full native managed executions, not six. v3 produced an archive but failed package qualification; v4 andv5 passed. Onlyv5 compatibility was launched on ARM. No original failed UUID was replayed.
- The same product05 was compiled/qualified in v4 andv5 because late cursor tooling repair changed the immutable combined plan. Current protocol allowed ordinary Cargo cache reuse, not cross-attempt stage/receipt/artifact adoption. Do not “optimize” this retrospectively by transferring receipts. [E26]
- Stable shared target/cache paths did not make frontend rebuilds disappear: v3 frontend Cargo artifacts reported197 compiled/1113 fresh; v4 andv5 each153 compiled/1157 fresh. Their upstream workspace absolute paths differed. This supports investigating path-sensitive freshness, but **does not independently establish every recompile's cause**. Helper build reused effectively in v5 (0.256s command versus24.747s v4). [E33,E34 and runtime detail]
- Despite identical product/patch, v4/v5 frontend and code-mode-host hashes differed; runtime hash was identicalc02c56f5…. Thus “same source” did not establish binary identity. Do not promise reproducible byte equality or substitute the prior archive without a qualified reuse/provenance path. [E31,E32]
- Repeating all205 HOME-corrected units, then rerunning the failed scenario with an optimized helper and continuing the other13, was an explicit fresh validation after diagnosis. It was not evidence tampering. Later packaged14-case recovery was also necessary: standalone helper acceptance did not qualify the shipping archive.
- Root rechecks, source/index sealing, exact input snapshots and independent cleanup were acceptance controls. The avoidable part was repeatedly hand-writing the same review glue and sending acknowledgments of already armed/finished work. Local review errors (input order and mode encoding) demonstrate the cost of that glue.
- A reported native success followed by archive transport/acceptance was distinct from actual compatibility authorization. Some pauses faithfully enforced the handoff's limited scope; a future complete brief must grant the conditional next phases in advance rather than relying on “continue” to override explicit stops.

## Already fixed versus remaining gaps

**Fixed and demonstrated during048:** private libcap correction/profile pin; HOME-corrected validation; release-profile integration helper; both EXDEV copy sites; narrow adjacent-float cleanup comparator and shared forensic comparator; intended footer/worker/cursor assertions; native0755 before provenance; canonical archive reference staging; separate qualified audit-only-socket verifier. Final native/compat outcomes and original failure preservation provide actual evidence, not just proposed patches. Some fixes live in host data or companion verifier tooling, not magically in every historical frozen source. Pin the correct revision next time. [E02,E04,E08,E14–E17,E21–E29]

**Already documented:** tooling59 release book explicitly covers HOME, debug hashing, real cross-device topology, native modes before provenance, HUD/Rust/cursor assertions, fresh exact SSH baseline, canonical previous archive, scratch Git indexes, and exercising recovery status/export/import. Repeating these paragraphs is not the main missing deliverable. Several need executable early checks and a complete ownership/authorization brief. [E25]

**Procedural/tooling gaps still worth addressing:**

1. Final changed fixtures were not all qualified before the expensive freeze. There were three separate stale UI assertions (footer, Rust grouped-worker row, cursor capture), then cross-device packaging assumptions. Add the affected Rust filters, actual saved-capture contract checks and actual host copy topology to the freeze's exit evidence.
2. Early one-ULP warnings did not immediately exercise the complete release cleanup/status/export/import chain. Add one shared preserved-pair regression covering every consumer and the actual raw failure verdict. Keep adjacency finite and exact elsewhere; do not expand tolerance.
3. “Supervisor journal completed” and “its child returned0” are conflated in the old audit. A paired exit1 triggered an “incomplete” diagnostic. Keep conservative recovery policy, but report **terminal failure** distinctly from **missing terminal evidence**; add a focused case proving a failed/reaped tree does not silently become success. This report does not authorize relaxing that invariant. [E11,E28]
4. The fallback `Stage produced no report` hides the direct fixture error in the top-level gate summary. Include the failing subgate/raw report path in the failed-stage receipt; preserve the original trace rather than replacing it with generic cleanup wording. [E09,E10]
5. Proposal input-order reconstruction and file-mode conversion should use one reviewed helper/contract. Compare each frozen entry to the existing ordered proposal inputs, then verify the independent full closure; do not regenerate snapshot indexes by sorting the serialized closure map. Preserve schema types (modes are octal strings); comparing only unordered sets is insufficient. Root/native reviewers should not implement separate ad hoc ordering logic.
6. Final verification must include “socket already removed, audit false, exact old removal seal valid” in its prequalified success path. Run this with real raw receipts and evidence transport before freeze; do not discover it after full qualification.
7. No benchmark proves stable-workspace adoption or higher concurrency is worthwhile. First use recorded Cargo artifact counts/timings; if a future authorized experiment is needed, change only workspace layout and compare matched conditions. Do not add a reusable-workspace protocol or cross-attempt receipt composer merely to finish this retrospective.
8. A stale relay snapshot caused unnecessary status replies. Use exact phase/attempt/execution/review digest and a single armed/completed receipt as the shared progress reference; do not treat acknowledgment as a phase gate.
9. Real-index metadata can change through `git status`. The coordinator reported this on x86 after qualification; ARM avoided it. Keep all sealed-worktree inspection on disposable indexes and preserve bytes **and metadata**. This was a cross-host reported incident, not an ARM reproduction. [E25; transcript3540]

## Smallest concrete prevention checks

These are proposed additions to the existing recipe/checklist, not a new controller, and none was executed in this review.

1. **Before allocating a real attempt:** validate profile under its exact child env; assert required HOME contract; resolve Python/import/compiler/pkgconf identities; run actual libcap PC/header/library probe; query offline Cargo metadata for the native target. Stage and full-inventory the previous archive in its canonical modes. Failure here is cheap and does not leave a short-root attempt to hold.
2. **Before expensive compilation:** run packaging compiler/license preflight with the packaging child's exact environment; prove the real /data→/tmp copy path and permissions; run the changed native Rust assertions and saved cursor/footer checks. Bootstrap package-based probes may use a pinned previous/saved package only as labeled diagnostics; the fresh candidate still runs all shipping gates.
3. **Before freeze acceptance:** require reviewed success and failed/recovered evidence to survive actual status/export/import, including adjacent timestamps, nonzero-but-terminal journal policy, and removed-native-socket audit false. Make the final export verifier commit an explicit handoff pin, distinct from execution tooling if needed.
4. **At local launch:** after the both-host preparation gate, recheck strict ownership/resources and exact frozen proposal/snapshots once. Keep init/prepare/review close; if a new unknown identity appears, stop unless its exact baseline transformation was already authorized. Never rewrite a sealed baseline or exempt all SSH.
5. **After completion:** verify actual named tests, archive/full manifests, input/source/index seals, all identities/groups/sessions and exact short/quarantine removal; then conditionally proceed to approved compatibility and export under the same brief. Register one observer per execution and end the turn; a status acknowledgment is not needed.
6. **For measurements:** append command/stage/managed timing fields and report/approval/transfer boundary timestamps to the existing checklist. Label nested and cross-host overlaps. Keep runtime failures, cleanup recovery and evidence transport as separate outcomes.

## Conditional native-owner brief for the next release

A ready-to-adapt brief is in [native-owner-brief.md](native-owner-brief.md). It explicitly scopes conditional prepare→launch→native verification→compat→export, with one-time review of immutable inputs, exact stop conditions, bounded ownership recovery and a clear owner for local package approval. It excludes AP, publishing, installation and arbitrary retries. **This is a recommendation for a future coordinator-issued grant, not authorization to start a new job now.**

The key delegation decision is package-native acceptance before compat. If the coordinator delegates the committed local `approve_package` decision against exact final bytes, the ARM owner can continue without waiting for a network transfer round trip; the coordinator still independently accepts the shipping release. If that decision is reserved to root, return the proof and wait only for that explicit decision. Do not set a “root accepted” flag merely to avoid a stop.

## Principal exact evidence paths

Prefix `R` below is `/data/liupan/codex-plus-build/release048`. The machine index expands all paths and pins full hashes.

- E01–E02: `R/preparation/preparation-refusal.json`; `R/preparation/libcap-correction-01/correction-evidence.json`.
- E03–E08: `R/recovery-validation/evidence/{failure-review,home-rerun-build,private-case-failure-review}.json`; `hash-timing-probe-01/result.json`; `optimized-01/evidence/{build-result,runtime-validation-receipt}.json`.
- E09–E12: `R/native-arm-v3/attempts/0.163.0-plus.0.4.8-aarch64-73408e93c95d2595/` `evidence/package-seven/report.json`, `outputs/package.json`, `outputs/package-cleanup.json`, `process-journal.jsonl`.
- E13: `R/preparation-v3/arm-recovery-review-01/resolution-validator-preview.json`.
- E14–E17: `R/timestamp-handoff/arm-recovery-f873e61/cleanup-recovery-receipt.json`, `diagnostics/diagnostic-receipt.json`, `canonical-reference/canonical-reference-receipt.json`; forensic export `R/timestamp-handoff/arm-export-1142193/forensic-export-receipt.json`.
- E18–E20: `R/preparation-v4/arm-launch-evidence/{ownership-preflight-blocker,foreign-tmux-client}.json`; `R/preparation-v4/arm-refresh-01/conditional-launch-validation.json`.
- E21: `R/preparation-v5/arm-native-return-01/native-verification-receipt.json`, SHA256 `60df46a8a68ad9c9e1ce22ba20984794ada6116820412c4e0f63d6282fe51c10`.
- E22: `R/preparation-v5/arm-compat-return-01/verification-receipt.json`, SHA256 `df2420fea1245da6e4b7871448ceb964d78b19e856befd2410dec6af0d42c3b8`.
- Final native+compat transport: `R/preparation-v5/arm-compat-return-01/native-compat-evidence.tar.gz`, SHA256 `c99494e3beb4620ee0ff4eff2c9ab45fbb8ef8aade044bc991f4332798b1a910` (1,689 included files; all116 capture files and14 recovery reports included).
- Shipping ARM archive: `R/native-arm-v5/attempts/0.163.0-plus.0.4.8-aarch64-f321897466a339cb/packages/codex-plus-0.163.0-plus.0.4.8-aarch64-unknown-linux-gnu.tar.gz`, accepted SHA256 `b0b91a747a73490e749d7e791e138010759ccc0ad04f1a37de0e30a7dfe0df08`.
- Selected transcript: `/home/liupan/.codex/sessions/2026/10/22/rollout-2026-10-22T15-28-24-01a16282-d649-7a32-b319-8243183e1422.jsonl`, immutable reviewed prefix through line4021; its byte length/hash and exact evidence lines are recorded in chronology.json. The live file can legitimately grow.

Not checked: independent x86/AP/publication/rollout records, restarted live frontend identity, pure human/transport timing, or a concurrency/cache A/B. No new qualification or release-book source edit was performed; this report supplies the native owner's evidence and smallest proposed changes to the coordinator.
