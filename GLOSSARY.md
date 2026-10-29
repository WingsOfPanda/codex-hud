# Codex Plus

The language of registered work, completion delivery and shared mods.

## Language

**Park**:
A root session's registration of finite work whose results it is waiting for.
A registration can exist before the root has ended its turn.

**Watched job**:
A particular execution or direct-child worker turn included in a park.
A later execution or worker turn is a different job.

**Prepared job**:
A proposed command with its inputs, expected effects, result checks and cleanup,
ready for the root to review before execution.

**Execution**:
One authorized attempt to run a prepared job. Its outcome exists independently
of whether a session is currently watching it.

**Review receipt**:
The root's acceptance of a specific prepared job for its current task. Changes
to the reviewed inputs require a new review.

**Resource reservation**:
An execution's exclusive claim on a declared resource among participating jobs.
Unresolved execution or cleanup does not release the claim.

**Cleanup resolution**:
An evidence-backed record that an execution's cleanup obligations are settled.
It is distinct from the command's outcome.

**Recovery pass**:
A bounded investigation and reconciliation of a particular execution's outcome,
cleanup obligations and continuation authority.

**Maintenance hold**:
A pause in new launches and automated follow-up admission while existing
executions continue, used to coordinate a qualified upgrade.

**Callback**:
A completion notification associated with watched jobs. It carries outcomes and
result references; delivery of the notification is distinct from execution of
the model's follow-up work.
_Avoid_: Job result

**Ledger**:
The durable account of parks, watched jobs, and callback delivery. It preserves
their identities and records what is known or uncertain across interruptions.
_Avoid_: Log

## Shared mods

**Shared mod**:
An internally maintained extension with common behavior for Claude Code and
Codex Plus, whose available features may differ explicitly between hosts.

**Mod capability**:
A particular behavior a host supports for a shared mod. Support for one
capability does not imply support for the mod's other features.

**Mod host**:
The Codex Plus integration through which compatible mod packages provide their
supported behavior without a native release for each package.

**Mod package**:
A versioned, independently installable extension that declares the mod-host
capabilities it requires.
