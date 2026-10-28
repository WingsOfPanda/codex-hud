# Codex 0.165.0 adaptation

2026-10-28. Development adaptation; full release qualification is pending.
Upstream is `360319d8dee62d87e2d59fc0c21805d79519cfed` (`rust-v0.165.0`).
The existing fleet release remains `0.163.0-plus.0.4.9`. No publication,
installation or shared-daemon change follows from this development branch.

## Source and build

The frontend patch follows upstream's extracted `app/user_turn_submission.rs`
path for Plus input admission and settlement. It retains upstream steering-race
handling and server permission choices, Plus daemon-update guards, compact
commands, Morandi, Goal/Ultracode and worker status behavior.

Clean/reverse application and full source reconstruction pass. Changes remain in
TUI/CLI source and workspace lock reconciliation; the stock backend and feature
definitions are unchanged. All 1,308 external lock entries match upstream; 167
workspace package versions are reconciled to 0.165.0. Rust remains 1.99.1.
Build/package provenance now selects V8 crate 152.2.0 and V8 15.2.124.1.
Native compilation and rendered terminal acceptance are separate pending gates.

## Stock daemon and effort switching

The inspected queue, resume and item APIs remain compatible with the helper's
calls. Upstream agent-mail interruption fixes do not replace Plus's explicit
queue submissions, idle checks or origin-cancellation rules. Empty final answers
must continue to be classified from turn state, not final-message text.
Attached forks and sampling APIs require no new Plus caller.

Upstream 0.165.0 makes `reasoning_effort_override` stable and default-on. Plus
does not override that flag. It is evaluated in the stock daemon's model client:
a 0.165 frontend connected to a stock 0.163 daemon does not automatically gain
the new default. Supported models and the OpenAI provider identity are required.

`tests/test_upstream_0165_effort_integration.py` checks the default through a
private official 0.165 daemon and loopback provider. It selects low, high, high,
then low; request-level effort stays low, configuration updates record changes,
and the prompt-cache key stays stable. This verifies the wire contract, not
actual server-side cache hits. No feature-enable override or paid provider is used.

Run stock background/advisor/foreground, lifecycle, custom-goal and Ultracode
checks against the verified official binary. The development identity fixture
retains the old 0.163 pin; matching a version string alone is insufficient.

## Release boundary

The current native/compatibility recipes deliberately reject 0.165.0. Their
same-upstream previous-package and old-TUI checks cannot establish a stock-daemon
version transition. Before enabling a new release path:

1. Complete native compilation, affected Rust and terminal tests on the candidate.
2. Select the paired stock 0.165 daemon/Plus cutover and rollback contract with
   fleet-config; preserve the old stock/Plus pair and test saved records privately.
   Do not weaken upstream identity checks to manufacture mixed-version acceptance.
3. Update and qualify native/compatibility recipe pins, captures and AP replay on
   x86 and ARM, including actual official binary identities on each architecture.
4. Obtain mods' app-server compatibility review and follow the release book's
   existing publication, merge and fleet rollout scopes.

The initial wider recipe test run exposed 15 expectations still tied to the
qualified 0.163 recipe. They remain unresolved adaptation work, not waived gates.
Keep previous release evidence and frozen checkouts unchanged.
