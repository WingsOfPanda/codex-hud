# Share owned mod behavior while preserving the stock daemon

On 2026-10-29, the user agreed that our internally maintained mods may share
common behavior with explicit host-specific capability differences, while
Codex Plus keeps the official Codex daemon unchanged. We can adapt both our mod
code and the client integration, so universal emulation of third-party Claude
mods is unnecessary; behavior without a supported Codex integration remains
unavailable rather than being silently approximated. This settles scope, not
the runtime, interface design or authorization to implement.

The user subsequently clarified that Codex Plus must supply a reusable mod
host: new compatible mod packages must install/update without native changes.
Job-watch is its first consumer, not a hard-coded integration. New host
capabilities can require a native release; each new package must not.
