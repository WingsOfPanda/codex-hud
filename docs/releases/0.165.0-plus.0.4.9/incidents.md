# Release incidents and evidence scope

The final accepted product and tooling are `097993a191d89f3a4c07736493a143cd5b1ac348`.
Earlier attempts remain failed; none of their receipts was transferred into final acceptance.

| Finding | Correction and verification |
| --- | --- |
| A shell-policy fixture expected `Show less` for failed commands; the actual screen retained every output line and log path under `Hide details`. | Corrected expand/collapse/replay predicates. Focused regression and the complete retained-package shell diagnostic passed before a fresh production attempt. |
| A new protected SSH login blocked failed-job cleanup despite no owned survivors. | Temporary process-scoped read capabilities with the original audit UID and normal HOME let the unchanged strict audit read it. Baselines and foreign processes stayed untouched; supported resolution preserved raw failed outcomes. |
| Cursor capture and verification still hard-coded 0.163 after the recipe admitted 0.165. | Bound all three version checks to the exact validated upstream; both supported recipes and wrong-version refusal tested. |
| Preflight file-loading could not import a sibling protocol module under the production environment. | Added the selected tooling scripts directory to preflight imports; subprocess regression used the actual cwd, loader and Python environment. |
| Root review accidentally used a Python alias for AP verification, and initially treated native suite file references as direct counts. | Both reviews stopped before affected launch. The pinned interpreter and existing libtest parser completed the reviews; no workload replay or relaxed assertion. |
| Canonical read-only SQLite inspection refreshed four shared-memory file timestamps on ARM. | Original observations retained; all receipt-bound bytes, sizes and modes remained unchanged. No timestamp normalization or data waiver. |

The corrected full six-gate diagnostic passed before the final production freeze. Final native and compatibility jobs completed with workload0/cleanup0 on both architectures. All failures and cleanup proofs are retained privately; this directory contains final accepted evidence.

The release-book updates are in [PR19](https://github.com/WingsOfPanda/codex-plus/pull/19), separate from the frozen product. No new release driver was introduced.
