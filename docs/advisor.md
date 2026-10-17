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

Keep a consultation **foreground by default** when its result is needed for the
next decision. Mark its managed shell command `foreground` and keep the root turn
open in one bounded Code Mode cell until the result or the helper's 25-minute
deadline. A pending tool call makes no interim model requests. The marker only
controls display; the local wait below keeps the consultation pending. The
advisor footer continues to show progress. To cancel the consultation, stop its
native terminal (`/stop`); the helper signals its owned consultation process group.
An interrupted turn alone does not establish that its command stopped; follow
[shell cancellation](shell-workflow.md#after-the-initial-yield).

The conversation shows the active consultation locally:

```text
● Advising using GPT-6-Astra · 0m59s
```

After the command finishes, that becomes `● Advised by GPT-6-Astra · 2m41s`
or an advisor-failure row. The main session then reviews the returned findings.
These are display rows, not model messages. The footer retains its model, effort,
call count and timing within the existing three-row activity budget.

Use this pattern with a self-contained brief. The 26-minute local budget and
27-minute outer yield leave margin for the helper's deadline and cleanup:

```js
// @exec: {"yield_time_ms": 1620000, "max_output_tokens": 2000}
const deadline = Date.now() + 1560000;
let result = await tools.exec_command({
  cmd: "# codex-plus-shell: foreground\ncodex-plus run -- codex-plus advisor --brief 'Question, facts, constraints, and review focus'",
  yield_time_ms: 1000, max_output_tokens: 2000, login: false
});
let output = result.output;
let log = output.match(/codex-plus log: (.+)/)?.[1];
while (result.session_id !== undefined && Date.now() < deadline) {
  result = await tools.write_stdin({
    session_id: result.session_id, chars: "",
    yield_time_ms: Math.min(60000, Math.max(1, deadline - Date.now())),
    max_output_tokens: 2000
  });
  output += result.output;
  log ??= output.match(/codex-plus log: (.+)/)?.[1];
}
text({state: result.session_id === undefined ? "finished" : "budget_expired",
      exit_code: result.exit_code, session_id: result.session_id, log, output});
```

Review the exit status and returned advisor JSON before taking the dependent
decision; use the managed log for any clipped output. If the outer tool yields a
cell ID, wait on that same cell. If the local budget expires, report the
still-pending consultation and its session/log rather
than treating it as completed. Where the harness cannot keep a bounded cell
pending, report that limitation and follow the user's direction.

Use background only when independent work can continue before the advice is
needed. In that case use the managed background-shell workflow, and register the
exact shell session with `codex-plus park` when only the consultation remains.
Keep the task pending until the result is reviewed; compare the reviewed commit
and working state with the current tree before applying advice delivered later.

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

This repository supplies implementation and verification. Fleet-config owns the
installed helper wrapper and global instructions. The wrapper must route
`advisor` to this package's helper before installed use.

Fleet-config's advisor instruction section:

```markdown
## Astra advisor

For an independent second opinion, the main root session may call
`codex-plus advisor --brief TEXT`. Supply the decision, constraints, and review
focus. It uses the newest available Astra at `xhigh` in a read-only sandbox;
treat its findings as advice. For advice needed before the next decision, use
`# codex-plus-shell: foreground` and keep the turn open in one bounded local
Code Mode cell with a 26-minute wait budget and 27-minute outer yield. Inspect
the exit status and JSON before continuing. Use background/park only when independent
work can continue before the advice is needed. Keep the task pending until the
consultation is reviewed.
```
