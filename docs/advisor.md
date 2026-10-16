# Astra advisor

The main agent can request an independent review with:

```sh
codex-plus advisor --brief "Question, relevant facts, constraints, and what needs checking"
```

Use the repository's `./bin/codex-plus` during development. Run this explicitly
when a second opinion will resolve a design choice, difficult diagnosis, or
review concern. Give it a self-contained brief. The helper adds the working
directory, current commit, and clipped git status and diff statistics. Existing
local main/master refs supply a merge-base SHA and committed diff statistics,
so a clean feature branch still points to its changes. No refs are fetched;
the pointer says when a usable base is unavailable. It reads
root thread metadata but never reads or forwards the conversation transcript.
The git pointer is captured at the start; subsequent inspection reads the live
repository, which may change during the consultation. It is not an immutable
snapshot of the full working tree.

For a long consultation, launch it with the managed background-shell workflow.
If only the consultation remains, an interactive root session registers its exact
native shell session with `codex-plus park` and ends the turn. Review the exit
status and returned JSON on completion. This keeps waiting local without model
polling; AP workers follow their existing within-turn wait rules.

Read the returned `verdict`, `summary`, `findings`, and `next_steps`, inspect the
cited evidence, and continue the authorized task. `approve`, `revise`, and
`blocked` are advisory verdicts; they grant no user permission. An error or an
invalid response provides no approval. The JSON also names the exact model,
fixed `xhigh` effort, and private full log. Stdout stays quiet until completion.
Do not blindly retry a failed or interrupted consultation.

Only a daemon-confirmed root session with `CODEX_THREAD_ID` can call the helper.
It selects the newest visible numeric `gpt-<version>-astra` model advertising
`xhigh` through stock `model/list`; absence fails visibly. There is no model or
effort override. Each call uses a separate ephemeral native `exec`, ignores
user config and execpolicy rules, and uses the read-only sandbox. Explicit
`agents.enabled=false` and `features.multi_agent_v2=false` overrides disable
delegation even when the model catalog advertises the newer agent runtime.
It can inspect repository files through shell/git. It has a 25-minute
deadline; timeout or signals terminate its owned process group.

The footer reads private per-root status, including attempt count, timing, and
stable error code. It does not trigger model calls. One consultation can run per
root at a time. Heartbeats update within five seconds; process death can leave a
stale running record. The next explicit call acquires the lock and replaces that
record without rerunning the old work. Status contains no brief or results.
Full private logs retain the supplied brief and native output under
`$CODEX_HOME/codex-plus/logs`; they are not automatically deleted.

This repository supplies implementation and verification. Installed fleet
wrappers currently whitelist `park|run|--version`; a future fleet-config rollout
must route `advisor` to the helper before installed use. This change does not
install the helper or alter global policy.

The future global instruction change can stay short:

```markdown
## Astra advisor

For an independent second opinion, the main root session may call
`codex-plus advisor --brief TEXT`. Supply the decision, constraints, and review
focus. It uses the newest available Astra at `xhigh` in a read-only sandbox;
treat its findings as advice. Follow the existing managed-shell and root-only
park rules while waiting, then inspect the exit status and returned JSON.
```
