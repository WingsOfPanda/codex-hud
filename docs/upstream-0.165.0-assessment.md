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
X86 development compilation passed, followed by 19 focused Rust groups covering
324 unique tests. Native smoke (23 stages), Morandi, the footer and nine compact
display/color/status configurations passed. ARM corrected frontend and harness
compilation passed with cleanup exit 0. Its 19 Rust groups passed 349 executions
covering the same 324 unique test names as x86, with no ignored tests.

The first builds exposed three distinct port issues: the new optional
`ThreadListParams.attached_to_thread_id` needed an explicit `None`; upstream's
neutral compact failure label needed the retained Plus red `Failed (exit N)`
contract; and two tests needed real turn completion and the native generic
`Hide details` disclosure label. Original failures remain in the development
record. These fixes do not change the stock backend.

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

Private x86 stock background/advisor/foreground checks and 19 lifecycle,
custom-goal and Ultracode cases passed. The development identity fixture pins
official 0.163 and 0.165 binaries for x86 and ARM; a version string alone is
insufficient. ARM development acceptance passed all 39 logical private cases:
35 original passes, one corrected no-color invocation, and three cases using the
reviewed completion-wait fixture. These required 43 case processes in total;
the four original failed outcomes remain preserved. All case cleanup passed.

The ARM matrix exposed one omitted `--colors on` fixture argument and three
first-history-read failures (`list_turns is not supported yet`). The latter
occurred before observed turn completion; the removed private database prevents
identifying its exact original state. A test-only correction waits for the exact
completed-turn notification through metadata reads before asserting full history.
It does not suppress RPC errors or claim that completion guarantees persistence.
Root verification passed five regression tests and seven injected-error scenarios:
the original callers fail before completion, corrected callers pass, and persistent
Unsupported or other errors still fail. The three ARM rechecks passed with separate
fixture inputs and unchanged product binaries. Strict effort history/update/cache-key
checks and both old-TUI idle transitions passed; original evidence remains unchanged.
This closes development acceptance, not the release boundary below.

Private x86 diagnostics also passed old 0.163 reads and cold resume of tested
0.165 history, including effort-update items. Actual old 0.4.8 and 0.4.9 TUIs
survived an idle private daemon replacement, preserving their drafts and producing
one controlled response without replay; fresh old TUIs on 0.165 passed separately.
These checks do not cover managed-daemon snapshots, active jobs, memory-v2
lifecycle or a complete packaged rollback.

## Release boundary

The retained revision-1 native/compatibility recipes reject the cross-upstream
transition. Revision 2 binds the exact old/new stock identities and adds a paired
transition gate; it is under release qualification. Same-upstream previous-package
and old-TUI checks alone cannot establish a stock-daemon version transition.
Before claiming a qualified release:

1. Complete native compilation, affected Rust and terminal tests on the candidate.
2. Select the paired stock 0.165 daemon/Plus cutover and rollback contract with
   fleet-config; preserve the old stock/Plus pair and test saved records privately.
   Do not weaken upstream identity checks to manufacture mixed-version acceptance.
3. Update and qualify native/compatibility recipe pins, captures and AP replay on
   x86 and ARM, including actual official binary identities on each architecture.
4. Obtain mods' app-server compatibility review and follow the release book's
   existing publication, merge and fleet rollout scopes.

The initial wider recipe test run exposed 15 expectations tied to the qualified
0.163 recipe. Explicit legacy fixtures now use 14 files pinned to commit
`236d0438547e3c104c25118070321aa32384ddac`; current package/protocol tests retain
0.165 inputs. All 164 related checks passed, including explicit refusal of 0.165
at unqualified recipe boundaries. Production guards were not widened or mocked.

Fleet-config's existing contract requires evidence that the old stock daemon
reads newer state; it does not authorize a snapshot fallback. Its existing
handoff fields can express the paired switch and required session restart.
Retained stock identities currently require explicit operator notes, not an
invented handoff field. Do not claim a retained pair qualified from the diagnostic
checks above. Keep prior release evidence and frozen checkouts unchanged.
