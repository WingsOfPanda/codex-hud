# Codex Plus 0.163.0-plus.0.3.1

This Plus patch release fixes completion delivery and advisor process cleanup,
reduces inactive-worker HUD traffic, and shows the advisor's review topic in the
main conversation. It retains OpenAI Codex 0.163.0, upstream commit
`83086188ae0614bd882818ff1aa23f3194962012`, Rust 1.99.1 and the official stock
daemon. No daemon source or database migration is added.

## Completion and cancellation fixes

- Callback selection and assignment share one immediate transaction. Concurrent
  cancellation cannot leave a new ready callback blocking later completions;
  invalidated, never-dispatched callbacks from the older race retire locally.
- Exhausting a consumed-message history scan preserves that callback's uncertainty
  without blocking unrelated fresh wakes. Transport/protocol failures still
  propagate, and uncertain callbacks are not blindly resent.
- Relative `CODEX_HOME` paths resolve against the caller's original directory
  before detached startup or cooperative upgrade. Symlink protections remain.
- A detached native owner-lifetime guard cleans up advisor consultations and Git
  metadata probes even when `/stop` immediately kills the terminal group. It
  adopts and reaps descendants that created separate sessions, using Linux
  subreaper support and `/proc`. Unrelated process groups are preserved.
- Advisor pointer collection disables Git fsmonitor locally. Result files retain
  regular-file/no-follow and schema checks, with a separate 512 KiB transport
  bound that accommodates permitted Unicode text and JSON escaping.

The ledger remains schema 1 and the coordinator remains protocol 2. Callback
identities, frozen messages, cancellation provenance and LIVE/REPORT ONLY policy
are preserved. Stock enqueue/acknowledgement ambiguity remains visible; this
release does not promise exactly-once model/tool side effects or reboot recovery.

## Advisor and HUD

The main conversation shows the review topic and elapsed time:

```text
• Advisor on wake and cancellation fixes · 5:01
```

Completion or failure updates the same row. The model, effort and call count
remain in the footer. Direct advisor commands use a compact brief fallback.
An opaque wrapper such as `python3 consult.py` supplies a short topic using
`# codex-plus-advisor: TOPIC` immediately after the foreground shell directive;
unrelated commands are not guessed to be advisor work. See [advisor.md](advisor.md).

Only active workers require turn-history refreshes. Inactive thread rows remain
available for grouped token/dispatch totals, while completed hidden workers no
longer generate a turn-history request every second. UI refresh makes no model
calls. Workers, shell jobs, advisor status, warnings and overflow still share
three footer rows, preserving AP's prompt visibility window. Running TUIs must
restart to use these display changes.

## Build and package corrections

Developer builds select actual Cargo compiler-artifact paths instead of guessing
where target/config/environment settings wrote the binaries. The CLI and its
separate Code Mode host package are staged together, using upstream's pinned V8
archive/bindings resolver. Existing Cargo/V8 caches remain reusable.

Packages contain native Codex, the Code Mode host, digest-matched bwrap, ripgrep,
the Rust helper, launcher, workflow documents and full target-specific dependency
notices. Codex voice and Python runtime files remain excluded. Python is still a
build/test dependency; no system interpreter is removed.

Notice collection covers both the helper and CLI/host production graphs. Six
crates without separate publisher license files have explicit checksum-bound
declaration-backed resolutions. Canonical SPDX terms are labeled separately from
publisher-authored notices, with no guessed authors or years. Matching ICU data
notices, Microsoft's schema split grant and fxhash's actual Rust header remain
independent required inputs. Unknown missing licenses and invalid hashes still
fail assembly. See [notice resolution](notice-resolution.md).

## Upgrade and rollback

A compatible strictly higher protocol-2 release at the same upstream and target
can transfer observers cooperatively after their current cycle. No shared-daemon
restart is needed. The handoff records whether qualified upgrade acceptance
permits installation without a quiet checkpoint; every rollback still requires
one. Keep previous/in-use packages and retain the last Python 0.2.0 package until
fleet-config and the user explicitly retire that fallback after the corrected
Rust rollout and operational checks.

Fleet-config owns installation, pins, wrappers, global instructions and rollout.
This release changes no global instruction block; its documented topic header
is optional for direct commands and required for explicitly labeled wrappers.
Restart the TUI to pick up the new frontend; restarting it does not settle a
registration or force a helper handoff.

## Qualification

Complete archives are built and tested on their native GNU targets from one
frozen Plus commit. Tests use private Codex homes and loopback providers. The
companion note `docs/releases/0.163.0-plus.0.3.1.md` and `handoff.json` record exact
archive/manifest/tree hashes, build/cache timings, per-target checks and limits.
Earlier source/development evidence does not qualify these new release archives.
