# Codex Plus background work

The language of registered work and completion delivery.

## Language

**Park**:
A root session's registration of finite work whose results it is waiting for.
A registration can exist before the root has ended its turn.

**Watched job**:
A particular native command execution or direct-child worker turn included in
a park. A later execution or worker turn is a different job.

**Callback**:
A completion notification associated with watched jobs. It carries outcomes and
result references; delivery of the notification is distinct from execution of
the model's follow-up work.
_Avoid_: Job result

**Ledger**:
The durable account of parks, watched jobs, and callback delivery. It preserves
their identities and records what is known or uncertain across interruptions.
_Avoid_: Log
