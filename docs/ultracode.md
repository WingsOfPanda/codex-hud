# Ultracode

Ultracode is a Plus session mode for root-led, phased worker execution. It is
independent of reasoning effort and defaults off. The root plans and accepts
each phase using native workers, sealed acceptance evidence and existing
completion wakes. It adds no workflow scripting engine or model polling loop.

## Controls

Use `/ultracode on`, `/ultracode off` or `/ultracode status` in a qualified local
Plus root TUI. Remote workspaces, side conversations and unqualified forks are
outside this first version. The existing status line shows effective, pending or unavailable mode;
the activity strip keeps its three-row limit. Turning mode off preserves workers,
jobs and their result delivery. Native Ultra effort retains its upstream meaning.
The mode appears beside the model and effort, for example
`GPT-6-Astra high ultracode`. It prefers the model-with-reasoning item, falls back
to the model item, and appears only once when both are selected. A status line
without either model item gets a standalone mode label; an explicitly disabled
status line stays disabled. Pending and unavailable states remain explicit.

For operators, the corresponding helper is `codex-plus ultracode` with `on`,
`off`, or `status`, `--thread ROOT_UUID`, and optional `--json`. Models inspect
their own status; changing the setting is a user/TUI operation.

## When it takes effect

A mode request applies to the next unadmitted Plus-controlled turn. A queued
submission retains its admitted revision. Steering and compaction retain the
current turn's snapshot. The helper distinguishes requested state from the
effective snapshot tied to root, submission, turn and daemon generation.

Fresh roots receive a fixed startup instruction. Previously qualified roots can
resume using it. Older threads without that instruction remain unavailable:
stock `thread/resume` accepting new developer instructions does not prove those
instructions entered existing context. No shared-daemon modification or automatic
restart is involved. External clients bypassing Plus admission are outside this
effective-mode guarantee.

Plus stores the complete original startup developer instructions privately and
restores them on qualified cold loads. This preserves both the bootstrap and
existing custom instructions through later compaction. Status and HUD projections
exclude that private text. A resumed qualified root keeps this startup composition;
changed startup instructions require a newly qualified session.

## Worker procedure and recovery

The effective snapshot returns the [phase procedure](ultracode-policy.md).
Root-created worktrees, sealed contracts and root-run acceptance detect changed
assertions and unapproved fixtures, including ignored files. Explicit native
roles can override model and effort, so qualification records actual settings.
Leaf-only behavior and worktree use remain instructions, not separate sandboxes.

The root keeps JSON recovery evidence in the status response's
`journal_directory`. After restart it reconciles actual threads, commits, jobs
and worktrees; the journal does not automatically replay workers or executions.
Ordinary direct-child watches expire after 24 hours with local attention, without
a guaranteed model wake. Per-child completion may cause multiple useful root
wakes; unchanged work causes no observer model calls.

Workers hand back expected executions over eight minutes before launch. The root
uses [detached jobs](detached-jobs.md) only where the qualified commands are
installed. Unexpected overruns of permitted short checks may finish. Prepared
work is pending until execution acceptance passes.

## Qualification and rollout

The implementation has private x86 qualification recorded in
[the evidence](evidence/ultracode.json). Current installed Plus 0.3.2 does not
provide these controls. The four contract scenarios used genuine Codex workers;
provider/compaction tests use an official daemon and loopback responses, and
terminal transport tests explicitly identify their stubs.

Fleet rollout must route `ultracode` through its helper wrapper. Release notes
must identify the supported bootstrap paths, coordinator compatibility, worker
dry-run results and required TUI restart. Publishing, merging and installation
remain separate actions; this work changes no installed wrapper or global policy.

The new admission behavior uses coordinator protocol **5**, distinct from the
detached-job candidate's protocol 4; execution records remain format 1. The first
cutover needs a quiet checkpoint, including unresolved executions. Protocol 4
observers do not reserve these mode snapshots and cannot live-upgrade into this
contract. A release version and full fleet handoff are not selected by this change.
