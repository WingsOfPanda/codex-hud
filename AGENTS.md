# Codex Plus

# Worker groups

When dispatching related workers, use the explicit topic and batch naming rule in
[README.md](README.md#worker-groups). Keep unrelated work in separate groups.

# Background work

When only finite delegated work or background shell commands remain, follow
[the park workflow](docs/background-work.md): register the exact jobs with
`codex-plus park`, then end
the main turn. Let completion callbacks resume the task; use model turns for
decisions and verification. Cancel registered callbacks when the user cancels
that work.
