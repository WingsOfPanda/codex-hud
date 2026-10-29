# Shared mods design

Status: implemented on the feature branch following the user's instruction to
finish the design and implement. Scoped acceptance is recorded in
[mod-host evidence](evidence/mod-host.json); release and rollout remain separate.

## Accepted scope

On 2026-10-29 the user accepted shared behavior with explicit host differences
and an unchanged official Codex daemon ("all agree"). The next acceptance
("all yes, go") selected job-watch display first, supported readers/snapshots
instead of record-supplied commands, and current-session/pane visibility.

The user then clarified the main requirement: one Codex Plus release adds a
**reusable mod host**. Installing or updating a compatible mod must require no
native source edit or rebuild. Job-watch is its first consumer, not a native
special case. See [ADR 0001](adr/0001-shared-mods-scope.md).

Existing systems retain notifications, job control and goals. A new host
capability may require a native release; each new package must not. Arbitrary
Claude engine/API compatibility is not promised.

## Ownership

- Codex Plus: reusable native host, generic conformance suite, native UI and
  process-lifecycle tests, and integration acceptance.
- Mods owner: packages, shared logic, Claude adapter, package semantic tests,
  entrypoints, contracts and mod releases on a claude-mods branch.
- Installer for the new shared package store: user decision. The mods session
  currently installs claude-mods under its user's authority. Fleet-config owns
  engine/Plus versions and shared configuration; it is not currently the mods
  installer.

The verified integration package is job-watch 0.6.1 from claude-mods commit
`fa5905d805eeffd9944dbf0b870522f63b75b6d5`, now merged into claude-mods main.
Remote main and the unchanged accepted payload were independently verified;
the owner's merge does not authorize or establish a fleet rollout.
The native host contains no job-watch-specific code or packaged copy.

## Proposed reusable host

Both the advisor and mods owner support a generic display/observation interface.
Names and wire spellings below remain draft engineering details.

1. Load only explicitly enabled IDs from a configured package root. A proposed
   layout is `~/.local/lib/mods/<id>/<version>/`, with a `current` link. Finding
   code on disk does not enable it.
2. Validate a generic manifest: ID/version, host contract, required capabilities,
   runtime requirement, relative entrypoint, refresh/output requests and source
   receipt. No job-watch names, paths or job schema belong in native code.
3. Resolve an immutable version directory and configured absolute Node executable
   once per TUI process, including a newly launched `codex resume`. Switching
   threads inside that process is not a package-update boundary.
4. Run a bounded child directly by argv, without a shell. One stdin JSON request
   carries the host contract, exact request ID, native thread identity, optional
   TUI pane identity, current time and granted display dimensions.
5. Require one bounded JSON response echoing contract and request ID, with
   ok/empty/unavailable/error status and generic summary/detail rows. Text and
   tone belong to this interface; job states, grouping and record parsing stay
   in the package. A fractional progress field is optional, not needed to prove
   the host. The host draws package identity and sanitizes terminal text.
6. Give each package independent request/process state, one invocation in flight,
   capped stdout/stderr, a deadline and failure backoff. Reap owned process
   groups before replacement. Reject late replies after session changes.
7. Preserve native activity and composer space. Mods share only remaining rows
   within the existing six-row budget, deterministically; no fixed slice may
   displace native notices or activity. With zero spare rows, a mod may be hidden.
   Diagnostics stay available outside the footer. Zero installed mods must leave
   current output unchanged.

The two reviewers favor summary-first rows with generic severity/text and
optional age/share metadata. Exact field semantics must be frozen together;
names alone are not a protocol. Stderr is capped diagnostic output: valid
stdout plus exit zero is not a failure merely because a runtime wrote a warning.
Include that case in conformance tests. A controlled executable search path is
needed for package-owned fixed tools such as tmux; clearing the environment
must not accidentally make those readers unusable.

The existing local HUD refresh pattern supplies asynchronous reads and exact
root/request gating. Do not add mod work to its combined watcher/advisor/goal
read: a slow mod must not make those projections unavailable. Existing
`helper_output` illustrates subprocess use but has uncapped buffering and only
direct-child kill behavior; it is not a complete mod runner.

## Trust and collection

A package is ordinary Node code with the user's OS authority. Declared
capabilities/access are compatibility and review metadata, not an enforced
filesystem/network/command sandbox. The proposed initial trust model is reviewed
internal packages, pinned by source identity and digest, with explicit enablement.
An enforced sandbox is outside this bounded implementation.

The implementation uses this trusted-internal-code model and configured external
Node for the bounded development scope. It grants no automatic installation
authority. The native host enforces lifecycle, output schema/size, display budget and
identity binding. Package code owns supported readers. Tests must prove that
the reviewed collector ignores command-bearing fields; JSON transport alone
does not provide that guarantee.

The mods owner suggested Node permission flags as optional protection. Their
coverage and compatibility have not been verified, so the design neither relies
on them nor claims they sandbox the package. An enforced sandbox would be a
separate implementation choice.

## First consumer: job-watch

The initial collector supports local marker records only. It uses filesystem
reads and a fixed tmux command
with argv; no record-supplied shell, SSH, arbitrary condition, progress or show
command. A missing identity never broadens record selection.

The existing `probe.sh` is not suitable unchanged: it executes record commands,
interpolates remote paths/names into shell strings, accepts SSH option-like host
values, and chooses the newest installed AP bundle. These are outside the
restricted collector; Claude's existing trusted-record policy is distinct.

The refined mods proposal shares classification through a pure
`entries(jobs, seen, budget)` function: state/attention order, parent
alert association, name/progress interpretation, summary counts and age handling.
Collection stays host-specific; the TSV parser is Claude-only when Codex uses a
separate Node collector. Claude footer/board and Codex view formatting must call
the same classification implementation; `seen` supplies the clock and optional
delivery history. The entry limit accepts the granted budget rather than
hard-code six for every host.

Acceptance exercises both real formatter paths through that shared function;
an import scan alone does not prove execution. Compare common classification
under equivalent inputs/history assumptions, and test intentional history-based
differences separately rather than requiring false cross-host parity. Keep
Claude golden output byte-identical under its original history inputs.

Claude keeps its existing delivery/history behavior and golden footer/board
output. Codex display has no wake ledger, Close action or knowledge that Claude
delivered a notification. Completion markers and old-record collapsing must
therefore be defined explicitly, not inferred from Claude's store.

The Codex formatter supplies no delivery history to the shared classification
function. It does not call Claude's footer with
empty delivery fields. For a valid terminal marker belonging to the current
record generation, marker mtime may supply outcome age; missing/unreliable age
must not cause automatic collapsing. Exact age/collapse behavior remains to
settle. Move pure helpers out of ledger dependencies so the host entry's import
closure excludes ledger and registration code. Static import checks complement
runtime no-write checks; neither replaces native host acceptance.

Generic `until` records cannot be evaluated by this collector. Remote and
unsupported records show unavailable/unsupported as appropriate; record
commands are never run to fill the gap. The current Codex records were reported
as completed `until` records, so the pilot needs a small controlled marker job
with real state transitions. An expensive build or remote workload is unnecessary.
Stalled means evidence exceeded a freshness threshold, not proven failure.

Job-watch's current record association uses tmux session/pane identifiers, not
native thread IDs. The owner proposes empty output outside tmux. Decide whether
same-owner shared records are included; another live pane's records must not
silently enter scope. Two TUIs on one thread may each refresh independently
because v1 performs no notification delivery or shared-state writes.

Proposed selection: pane-specific records must match both current owner and
pane, not pane ID alone; pane-less records may match the current owner if the
user chooses shared-owner visibility. This is a display scope rule, not leader
election. No JOB_ALL, lease, lock or wake ledger is needed for this collector.
The mods owner confirms current remote-until records provide no useful live
data under local-marker-only scope; this pilot initially proves the host and
local-job display, not existing cross-host release monitoring.

## Updates and compatibility

Proposed behavior:

- Publish versioned packages independently of native releases. Record exact
  source and artifact digests; never replace an existing version with new bytes.
  Integrity receipts are not cryptographic proof against a same-user writer.
- Codex owns a **generic conformance suite** runnable against any package:
  protocol, lifecycle, caps, failures and self-test behavior. Package-specific
  semantic tests remain in claude-mods. This supersedes the earlier proposal
  to make job-watch-specific consumer fixtures the generic host's release gate.
- The installer validates source/digest/runtime/capabilities and runs the pinned
  host conformance suite plus package tests before atomically advancing
  `current`. Producer metadata or a producer self-test alone is insufficient.
- Running sessions keep their exact version directories. New TUI processes load
  the selected version; rollback affects future processes. Recommend no hot
  reload and no pruning initially.
- Compatible updates should require no native rebuild or model-to-model
  acknowledgement. Installer ownership and unattended advancement require the
  user's decision. They need not block building the host prototype.
- Incompatible candidates leave the accepted version selected and explain the
  missing capability. Claude-only changes with unchanged shared behavior need
  no Codex update.

The mods owner proposes staging only an enumerated host entry/import closure,
excluding Claude hooks, skills and the old probe. Validate the selected closure
and digest the exact bytes tested. An identical host artifact can be skipped,
but a manifest version bump changes its digest even if code is unchanged. The
one-version-for-both-hosts proposal does not by itself establish a no-op digest
for Claude-only releases. Keep this optimization optional until artifact version
and content identity are defined; ordinary compatible updates remain valid.

There is no claude-mods CI today; its owner reports session-run tests before
merge. The conformance suite must actually run in the selected installation
workflow; merely committing it does not enforce a gate. Its version belongs to
the Codex host contract, not to independently changeable producer expectations.

If pruning is added later, retain every in-use version and rollback. Per-refresh
processes leave no open file between reads; a TUI-use marker would need boot
identity, PID and start time. Pruning and markers are deferred pilot work.

## Acceptance

- Build native once; record its digest. Install job-watch and an independently
  named, non-job fixture package through the same host. Add/update/rollback that
  package without native changes; no package-name/domain branches in Rust.
- Run common-code and semantic tests through both production adapters. Preserve
  Claude's golden footer/board behavior and existing job-watch tests.
- Isolate timeout, malformed/oversized output, wrong request ID, missing runtime
  and nonzero exit to one package. Verify owned-child cleanup, no overlap,
  responsive composer, session-switch rejection and bounded resource use.
- With several packages and native notices/activity, preserve native priorities
  and row limits. With no packages, existing UI is unchanged. Refresh makes zero
  model/provider requests and does not run for a remote backend without a
  separately supported integration.
- Adversarial records cannot execute command fields, SSH options or shell
  metacharacters; foreign session/pane records stay out. Verify job records,
  watcher ledgers and Claude stores are unchanged.
- In a real native TUI, display a small controlled job's running/completed/failed
  states, freshness behavior and explicit unsupported data. Prove process
  version pinning through update and rollback.

## Capability roadmap, not promised scope

Display-only job-watch and msg-relay status are plausible consumers of v1.
Interactive panes, commands, notifications, context operations, tool registration,
prompt interception and model/agent routing require additional host capabilities.
The current evidence does not establish that every Claude engine feature can
be supported by a client using an unchanged official Codex daemon. Assess each
capability when selected; do not label it impossible without that evidence.

## Implemented choices and remaining deployment decisions

The bounded implementation uses trusted internal packages, external Node,
failure isolation, process-pinned versions and no updater or pruning. Job-watch
uses local marker reads, current-owner/pane visibility and empty output outside
tmux. The exact interface is in [mod-host.md](mod-host.md).

Installer ownership, automatic advancement, release qualification and deployment
remain separate decisions. Neither reviewer advice nor this document grants
merge, publication or install authority. Remote readers, wakes and other mod
capabilities remain outside this implementation.

## Review evidence

Two Opus 5.5 / xhigh consultations returned `revise`: local results and
supplied-file hashes are in `.build/shared-mods/advisor-review-01/` and
`.build/shared-mods/advisor-review-02/`. Root verified inputs and relevant native,
ledger, footer and probe source. The mods owner supplied the matching generic-host
proposal; this document consolidates the reviews. Source review alone is not
runtime qualification; the evidence record names the native, terminal and
consumer checks that subsequently ran and their limits.
