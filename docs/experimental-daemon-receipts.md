# Experimental daemon receipts (excluded from releases)

The historical queue receipt extension is retained for source preservation and
isolated research. The release build applies only
`patches/codex-0.162.1-worker-hud.patch`; it never applies the patch in
`patches/experimental/`. Fleet releases use the official stock daemon.

## Source and patch order

On a separate checkout of upstream commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`, apply the frontend patch first, then
`patches/experimental/codex-0.162.1-daemon-receipts.patch`. Together they reconstruct
the former combined patch. The extension includes protocol, queue, core, state,
and receipt migration changes, plus the TUI request-field adjustment they require.
An experimental checkout is intentionally rejected by the release build script.

## Experimental guarantees and limits

The extension advertises `initialize.queueIdempotency`. A durable key and frozen
payload let the daemon return an existing pending, consumed, or cancelled receipt
instead of blindly inserting again. Cancellation can create a tombstone before
enqueue arrives. The coordinator requires a receipt for keyed delivery; a start
acknowledgement alone does not establish durable acceptance.

The Python helper retains capability-gated support to preserve existing ledger
provenance. Legacy ambiguous callbacks never become safe to retry just because a
server gains the extension. Keyed callbacks do not silently become unkeyed when
it disappears. Neither mode guarantees exactly-once model or tool execution.

The experimental code also puts its queue item ID in `turn_trigger`, which upstream
copies into provider request metadata. That behavior must be removed before any
future consideration of deployment. This source split preserves the experiment;
it does not approve or repair it for release.

## Historical evidence

[wake-recovery.json](evidence/wake-recovery.json) records the isolated native
receipt tests and package upgrade/rollback experiment. The `before-enqueue`,
`queue-restart`, and `native-replay` scenarios in
`tests/test_background_recovery_integration.py` require an experimental binary.
Stock release tests use the scenarios documented in
[background verification](background-work.md#verification).

Existing `.build/wake-recovery/daemon-package/` artifacts, if present, belong to
that earlier experiment. They are not release artifacts or fleet install inputs.
No shared-daemon activation is part of the frontend release workflow.
